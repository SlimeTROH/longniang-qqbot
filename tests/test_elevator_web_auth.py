from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from elevator_web_auth import AuthError, AuthStore, CHALLENGE_TTL, SESSION_TTL


class ElevatorWebAuthTests(unittest.TestCase):
    pass_a = "first-password-2026"
    pass_b = "different-password-2026"
    pass_c = "recovered-password-2026"
    qq = "14656789"
    other_qq = "24656789"
    group = "34656789"

    def setUp(self):
        self.now = 1800000000
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "auth.sqlite3"
        self.store = AuthStore(self.path, clock=lambda: self.now)

    def confirm(self, challenge, qq=None):
        return self.store.confirm(challenge["code"], qq or self.qq, group_id=self.group, nickname="测试乘客")

    def bind(self, username="Elevator"):
        response = self.store.register(username, self.pass_a, self.qq)
        self.confirm(response["challenge"])
        return response["session"]

    def test_bound_identity_survives_reopen_and_code_replay_fails(self):
        response = self.store.register("Elevator", self.pass_a, self.qq)
        token, challenge = response["session"], response["challenge"]
        self.assertIsNone(self.store.get_session(token)["qq_id"])
        with self.assertRaisesRegex(AuthError, "密钥无效"):
            self.confirm(challenge, self.other_qq)
        self.assertEqual(self.confirm(challenge)["purpose"], "bind")
        with self.assertRaisesRegex(AuthError, "密钥无效"):
            self.confirm(challenge)
        reopened = AuthStore(self.path, clock=lambda: self.now)
        self.assertEqual(reopened.get_session(token), {
            "username": "Elevator", "qq_id": self.qq, "qq_group": self.group, "nickname": "测试乘客",
        })
        with self.assertRaisesRegex(AuthError, "已经绑定"):
            reopened.issue_bind(token, self.other_qq)
        login = reopened.login("eLEVaTor", self.pass_a)
        self.assertEqual(login["account"]["username"], "Elevator")
        reopened.logout(login["session"])
        with self.assertRaises(AuthError):
            reopened.get_session(login["session"])

    def test_only_one_account_can_bind_a_qq_even_under_concurrency(self):
        first = self.store.register("First", self.pass_a, self.qq)
        second = self.store.register("Second", self.pass_b, self.qq)
        with self.assertRaisesRegex(AuthError, "已被使用"):
            self.store.register("FiRST", self.pass_c, self.other_qq)

        def attempt(response):
            try:
                return self.confirm(response["challenge"])["purpose"]
            except AuthError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(attempt, (first, second))), ["bind", "qq_taken"])

    def test_change_requires_current_password_and_qq_then_revokes_sessions(self):
        primary = self.bind()
        secondary = self.store.login("Elevator", self.pass_a)["session"]
        with self.assertRaisesRegex(AuthError, "当前密码有误"):
            self.store.issue_change(primary, self.pass_c, self.pass_b)
        change = self.store.issue_change(primary, self.pass_a, self.pass_b)
        with self.assertRaisesRegex(AuthError, "密钥无效"):
            self.confirm(change, self.other_qq)
        self.assertEqual(self.store.get_session(primary)["qq_id"], self.qq)
        self.confirm(change)
        for session in (primary, secondary):
            with self.assertRaises(AuthError):
                self.store.get_session(session)
        with self.assertRaisesRegex(AuthError, "密码错误"):
            self.store.login("Elevator", self.pass_a)
        self.assertEqual(self.store.login("Elevator", self.pass_b)["account"]["qq_id"], self.qq)

    def test_reset_and_decoy_and_replay_across_password_changes(self):
        token = self.bind()
        real = self.store.issue_reset("Elevator", self.pass_c)
        decoy = self.store.issue_reset("Missing", self.pass_c)
        self.assertEqual(real.keys(), decoy.keys())
        self.assertEqual((real["purpose"], real["expires_at"]), (decoy["purpose"], decoy["expires_at"]))
        with self.assertRaisesRegex(AuthError, "密钥无效"):
            self.confirm(decoy)
        change = self.store.issue_change(token, self.pass_a, self.pass_b)
        self.assertEqual(self.confirm(real)["purpose"], "reset")
        with self.assertRaisesRegex(AuthError, "密钥无效"):
            self.confirm(change)
        with self.assertRaises(AuthError):
            self.store.get_session(token)
        self.assertEqual(self.store.login("ELEVATOR", self.pass_c)["account"]["qq_id"], self.qq)

    def test_expiry_reissue_and_hashed_storage(self):
        response = self.store.register("Elevator", self.pass_a, self.qq)
        token = response["session"]
        old = response["challenge"]
        self.now += CHALLENGE_TTL
        with self.assertRaisesRegex(AuthError, "密钥无效"):
            self.confirm(old)
        new = self.store.issue_bind(token, self.qq)
        with self.store._database() as connection:
            account = connection.execute("SELECT password_hash FROM web_accounts").fetchone()[0]
            session = connection.execute("SELECT token_hash FROM web_sessions").fetchone()[0]
            proof = connection.execute("SELECT code_hash FROM web_challenges").fetchone()[0]
        self.assertTrue(account.startswith("scrypt-v1$"))
        self.assertNotIn(self.pass_a, account)
        self.assertEqual((len(session), len(proof)), (32, 32))
        self.assertNotEqual(session, token.encode())
        self.assertNotEqual(proof, new["code"].encode())
        self.now += SESSION_TTL
        with self.assertRaisesRegex(AuthError, "请登录"):
            self.store.get_session(token)

    def test_limits_block_repeated_proofs_across_sources_until_window_expires(self):
        for attempt in range(12):
            with self.assertRaisesRegex(AuthError, "密钥无效"):
                self.store.confirm("bad", self.qq, "source" + str(attempt), group_id=self.group)
        with self.assertRaisesRegex(AuthError, "尝试次数过多"):
            self.store.confirm("bad", self.qq, "other-source", group_id=self.group)
        self.now += CHALLENGE_TTL
        with self.assertRaisesRegex(AuthError, "密钥无效"):
            self.store.confirm("bad", self.qq, "other-source", group_id=self.group)

    def test_a_blocked_source_does_not_consume_other_accounts_quota(self):
        for i in range(5):
            self.store._rate_limit("register", "blocked", f"used-{i}", 5, 2)
        for _ in range(2):
            with self.assertRaisesRegex(AuthError, "尝试次数过多"):
                self.store._rate_limit("register", "blocked", "target", 5, 2)
        self.store._rate_limit("register", "another-source", "target", 5, 2)


if __name__ == "__main__":
    unittest.main()
