"""Identity and QQ proofs for the Endless Elevator web client."""

from contextlib import contextmanager
import hashlib
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time


SESSION_TTL = 7 * 24 * 60 * 60
# SQLite's existing expiry column is required. Use the end of year 9999 for remembered sessions.
REMEMBERED_EXPIRES_AT = 253_402_300_799
CHALLENGE_TTL = 10 * 60
_PURPOSE_PREFIX = {"bind": "EEB-", "change": "EEC-", "reset": "EER-"}
_USERNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,31}")
_QQ_NUMBER = re.compile(r"[1-9][0-9]{4,13}")
_SESSION = re.compile(r"EES-[A-Za-z0-9_-]{43}")
_CHALLENGE = re.compile(r"EE[BCR]-[A-Za-z0-9_-]{24}")
_PASSWORD_WORKERS = threading.BoundedSemaphore(4)
_DUMMY_SALT = b"elevator-auth-v1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS web_accounts (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    username_key TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    qq_id TEXT UNIQUE,
    qq_group TEXT,
    nickname TEXT,
    CHECK ((qq_id IS NULL AND qq_group IS NULL) OR
           (qq_id IS NOT NULL AND qq_group IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS web_sessions (
    token_hash BLOB PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES web_accounts(id) ON DELETE CASCADE,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS web_sessions_account ON web_sessions(account_id);
CREATE INDEX IF NOT EXISTS web_sessions_expiry ON web_sessions(expires_at);
CREATE TABLE IF NOT EXISTS web_challenges (
    code_hash BLOB PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES web_accounts(id) ON DELETE CASCADE,
    target_qq TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('bind', 'change', 'reset')),
    password_hash TEXT,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS web_challenges_account ON web_challenges(account_id, purpose);
CREATE INDEX IF NOT EXISTS web_challenges_expiry ON web_challenges(expires_at);
CREATE TABLE IF NOT EXISTS web_rate_limits (
    bucket TEXT NOT NULL,
    key_hash BLOB NOT NULL,
    attempts INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    PRIMARY KEY (bucket, key_hash)
);
CREATE INDEX IF NOT EXISTS web_rate_expiry ON web_rate_limits(expires_at);
"""


class AuthError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def _derive(password: bytes, salt: bytes) -> bytes:
    if not _PASSWORD_WORKERS.acquire(timeout=2):
        raise AuthError(429, "busy", "登录服务繁忙，请稍后再试")
    try:
        return hashlib.scrypt(password, salt=salt, n=32768, r=8, p=1, maxmem=64 * 1024 * 1024)
    finally:
        _PASSWORD_WORKERS.release()


def _password_bytes(password: str) -> bytes:
    if isinstance(password, str) and 8 <= len(password) <= 128:
        try:
            return password.encode("utf-8")
        except UnicodeEncodeError:
            pass
    raise AuthError(400, "invalid_password", "密码长度须为 8–128 个字符")


def _new_password(password: str) -> str:
    encoded = _password_bytes(password)
    salt = secrets.token_bytes(16)
    return f"scrypt-v1${salt.hex()}${_derive(encoded, salt).hex()}"


def _verify_password(password: bytes, stored: str | None) -> bool:
    if stored is None:
        _derive(password, _DUMMY_SALT)
        return False
    _, salt, expected = stored.split("$")
    return secrets.compare_digest(_derive(password, bytes.fromhex(salt)), bytes.fromhex(expected))


def _username(value: str) -> str:
    if isinstance(value, str) and _USERNAME.fullmatch(value.strip()):
        return value.strip()
    raise AuthError(400, "invalid_username", "账号须为 3–32 位字母、数字、下划线、点或短横线，并以字母或数字开头")


def _qq_number(value: str, message: str) -> str:
    candidate = str(value).strip()
    if _QQ_NUMBER.fullmatch(candidate):
        return candidate
    raise AuthError(400, "invalid_qq", message)


def _account(row: sqlite3.Row) -> dict:
    return {
        "username": row["username"],
        "qq_id": row["qq_id"],
        "qq_group": row["qq_group"],
        "nickname": row["nickname"],
    }


class AuthStore:
    """Persistent browser accounts, restricted QQ proofs and revocable sessions."""

    def __init__(self, db_path: Path | str, *, clock=time.time):
        self.path = Path(db_path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._database() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(_SCHEMA)

    @contextmanager
    def _database(self, *, write=False):
        db = sqlite3.connect(self.path, timeout=8)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                if write:
                    db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    def _now(self) -> int:
        return int(self.clock())

    def _rate_limit(self, operation: str, source: str, identifier: str, source_limit: int, account_limit: int):
        now = self._now()
        targets = [(operation + ":account", identifier, account_limit)]
        if source:
            targets.append((operation + ":source", source, source_limit))
        with self._database(write=True) as db:
            db.execute("DELETE FROM web_rate_limits WHERE expires_at <= ?", (now,))
            increments = []
            for bucket, target, limit in targets:
                key = hashlib.sha256(target[:512].encode("utf-8", errors="replace")).digest()
                row = db.execute(
                    "SELECT attempts FROM web_rate_limits WHERE bucket=? AND key_hash=?",
                    (bucket, key),
                ).fetchone()
                if row is not None and row["attempts"] >= limit:
                    raise AuthError(429, "rate_limited", "尝试次数过多，请稍后再试")
                increments.append((bucket, key, now + CHALLENGE_TTL))
            db.executemany(
                """INSERT INTO web_rate_limits(bucket, key_hash, attempts, expires_at) VALUES (?, ?, 1, ?)
                   ON CONFLICT (bucket, key_hash) DO UPDATE SET attempts=attempts+1""",
                increments,
            )

    @staticmethod
    def _read_session(db, token: str | None, now: int):
        row = None
        if isinstance(token, str) and _SESSION.fullmatch(token):
            row = db.execute(
                """SELECT a.*, s.expires_at AS session_expires_at
                   FROM web_sessions s JOIN web_accounts a ON a.id=s.account_id
                   WHERE s.token_hash=? AND s.expires_at>?""",
                (_digest(token), now),
            ).fetchone()
        if row is None:
            raise AuthError(401, "sign_in_required", "请登录后继续")
        return row

    @staticmethod
    def _make_session(db, row: sqlite3.Row, now: int, *, remember: bool = False):
        token = "EES-" + secrets.token_urlsafe(32)
        expires = REMEMBERED_EXPIRES_AT if remember else now + SESSION_TTL
        db.execute("DELETE FROM web_sessions WHERE expires_at <= ?", (now,))
        db.execute(
            "INSERT INTO web_sessions(token_hash, account_id, expires_at) VALUES (?, ?, ?)",
            (_digest(token), row["id"], expires),
        )
        return {"account": _account(row), "session": token, "expires_at": expires}

    @staticmethod
    def _make_challenge(db, purpose: str, now: int, account_id: int | None, qq_id=None, password_hash=None):
        code = _PURPOSE_PREFIX[purpose] + secrets.token_urlsafe(18)
        expires = now + CHALLENGE_TTL
        db.execute("DELETE FROM web_challenges WHERE expires_at <= ?", (now,))
        if account_id is not None:
            if purpose == "bind":
                db.execute("DELETE FROM web_challenges WHERE account_id=? AND purpose='bind'", (account_id,))
            db.execute(
                """INSERT INTO web_challenges(code_hash, account_id, target_qq, purpose, password_hash, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (_digest(code), account_id, qq_id, purpose, password_hash, expires),
            )
        return {"code": code, "purpose": purpose, "expires_at": expires}

    def register(self, username: str, password: str, qq_id: str, source: str = "") -> dict:
        username = _username(username)
        qq_id = _qq_number(qq_id, "请输入要绑定的 QQ 号")
        self._rate_limit("register", source, username.lower(), 12, 4)
        stored = _new_password(password)
        with self._database(write=True) as db:
            now = self._now()
            try:
                row = db.execute(
                    "INSERT INTO web_accounts(username, username_key, password_hash) VALUES (?, ?, ?) RETURNING *",
                    (username, username.lower(), stored),
                ).fetchone()
            except sqlite3.IntegrityError:
                raise AuthError(409, "username_taken", "这个账号名已被使用") from None
            response = self._make_session(db, row, now)
            response["challenge"] = self._make_challenge(db, "bind", now, row["id"], qq_id)
            return response

    def login(self, username: str, password: str, source: str = "", *,
              remember: bool = False, login_type: str = "auto") -> dict:
        if login_type not in ("auto", "account", "qq") or not isinstance(remember, bool):
            raise AuthError(400, "invalid_login_options", "登录选项无效")
        invalid = AuthError(401, "invalid_credentials", "账号、QQ 号或密码错误")
        try:
            key = _username(username).lower()
            encoded = _password_bytes(password)
        except AuthError:
            self._rate_limit("login", source, "", 35, 10)
            raise invalid from None
        username_key = key if login_type != "qq" else None
        qq_id = key if login_type != "account" and _QQ_NUMBER.fullmatch(key) else None
        with self._database() as db:
            candidates = db.execute(
                "SELECT id, username_key, password_hash FROM web_accounts WHERE username_key=? OR qq_id=?",
                (username_key, qq_id),
            ).fetchall()
        rate_key = candidates[0]["username_key"] if len(candidates) == 1 else key
        self._rate_limit("login", source, rate_key, 35, 10)
        if not candidates:
            _verify_password(encoded, None)
            raise invalid
        verified = [row for row in candidates if _verify_password(encoded, row["password_hash"])]
        if not verified:
            raise invalid
        if len(verified) > 1:
            raise AuthError(409, "ambiguous_login", "该号码同时匹配注册账号和 QQ 号。请选择一种方式登录。")
        row = verified[0]
        with self._database(write=True) as db:
            # A password may have changed while scrypt was running.
            current = db.execute("SELECT * FROM web_accounts WHERE id=? AND password_hash=?",
                                 (row["id"], row["password_hash"])).fetchone()
            if current is None:
                raise invalid
            return self._make_session(db, current, self._now(), remember=remember)

    def get_session(self, token: str | None) -> dict:
        return self.get_session_info(token)["account"]

    def get_session_info(self, token: str | None) -> dict:
        with self._database() as db:
            row = self._read_session(db, token, self._now())
        return {"account": _account(row), "expires_at": row["session_expires_at"]}

    def logout(self, token: str | None):
        if isinstance(token, str) and _SESSION.fullmatch(token):
            with self._database(write=True) as db:
                db.execute("DELETE FROM web_sessions WHERE token_hash=?", (_digest(token),))

    def issue_bind(self, token: str, qq_id: str, source: str = "") -> dict:
        qq_id = _qq_number(qq_id, "请输入要绑定的 QQ 号")
        account = self.get_session(token)
        self._rate_limit("bind", source, account["username"].lower(), 24, 8)
        with self._database(write=True) as db:
            now = self._now()
            row = self._read_session(db, token, now)
            if row["qq_id"]:
                raise AuthError(409, "already_bound", "此网页账号已经绑定 QQ")
            return self._make_challenge(db, "bind", now, row["id"], qq_id)

    def issue_change(self, token: str, current_password: str, new_password: str, source: str = "") -> dict:
        with self._database() as db:
            row = self._read_session(db, token, self._now())
        if not row["qq_id"]:
            raise AuthError(409, "bind_required", "请先绑定 QQ")
        self._rate_limit("change", source, row["username_key"], 20, 6)
        current = _password_bytes(current_password)
        if not _verify_password(current, row["password_hash"]):
            raise AuthError(401, "invalid_credentials", "当前密码有误")
        pending = _new_password(new_password)
        with self._database(write=True) as db:
            now = self._now()
            authorized = self._read_session(db, token, now)
            return self._make_challenge(db, "change", now, authorized["id"], authorized["qq_id"], pending)

    def issue_reset(self, username: str, new_password: str, source: str = "") -> dict:
        username = _username(username)
        self._rate_limit("reset", source, username.lower(), 20, 5)
        pending = _new_password(new_password)
        with self._database(write=True) as db:
            row = db.execute("SELECT * FROM web_accounts WHERE username_key=? AND qq_id IS NOT NULL",
                             (username.lower(),)).fetchone()
            if row is None:
                # Decoys match the appearance and hashing cost of a real request.
                return self._make_challenge(db, "reset", self._now(), None)
            return self._make_challenge(db, "reset", self._now(), row["id"], row["qq_id"], pending)

    def confirm(self, code: str, qq_id: str, source: str = "", *, group_id: str = "", nickname: str = "") -> dict:
        """Consume a code after the QQ adapter authenticates the sender and group."""
        qq_id = _qq_number(qq_id, "未取得有效的发送者 QQ 号")
        group_id = _qq_number(group_id, "请在已启用的 QQ 群发送密钥")
        self._rate_limit("confirm", source, qq_id, 80, 12)
        bad = AuthError(400, "invalid_challenge", "密钥无效、已过期或与你的 QQ 号不匹配")
        if not isinstance(code, str) or not _CHALLENGE.fullmatch(code.strip()):
            raise bad
        with self._database(write=True) as db:
            now = self._now()
            row = db.execute(
                """SELECT c.*, a.qq_id AS bound_qq, a.username FROM web_challenges c
                   JOIN web_accounts a ON a.id=c.account_id
                   WHERE c.code_hash=? AND c.target_qq=? AND c.expires_at>?""",
                (_digest(code.strip()), qq_id, now),
            ).fetchone()
            if row is None:
                raise bad
            account_id = row["account_id"]
            if row["purpose"] == "bind":
                if row["bound_qq"] is not None:
                    raise bad
                duplicate = db.execute("SELECT 1 FROM web_accounts WHERE qq_id=?", (qq_id,)).fetchone()
                if duplicate:
                    raise AuthError(409, "qq_taken", "这个 QQ 已绑定网页账号，可用原账号登录或重置密码")
                display_name = " ".join(nickname.split())[:40] or None
                db.execute("UPDATE web_accounts SET qq_id=?, qq_group=?, nickname=? WHERE id=?",
                           (qq_id, group_id, display_name, account_id))
            else:
                if row["bound_qq"] != qq_id or row["password_hash"] is None:
                    raise bad
                db.execute("UPDATE web_accounts SET password_hash=? WHERE id=?", (row["password_hash"], account_id))
                db.execute("DELETE FROM web_sessions WHERE account_id=?", (account_id,))
            db.execute("DELETE FROM web_challenges WHERE account_id=?", (account_id,))
            return {"purpose": row["purpose"], "username": row["username"]}
