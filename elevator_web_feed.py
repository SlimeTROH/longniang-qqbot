"""Queue web game activity in the game transaction for QQ delivery."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

HIGH_VALUE = 30_000
RECORD_FLOOR = 8
MAX_DELIVERY_AGE = 86400
SCHEMA = """
CREATE TABLE IF NOT EXISTS elevator_web_announcements(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    next_at INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    signal_owner TEXT,
    signal_expires_at INTEGER
)
"""


def _ensure_outbox(db):
    db.execute(SCHEMA)
    columns = {row[1] for row in db.execute("PRAGMA table_info(elevator_web_announcements)")}
    if "signal_owner" not in columns:
        db.execute("ALTER TABLE elevator_web_announcements ADD COLUMN signal_owner TEXT")
    if "signal_expires_at" not in columns:
        db.execute("ALTER TABLE elevator_web_announcements ADD COLUMN signal_expires_at INTEGER")


class GameFeed:
    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        self._content = Path(__file__).resolve().parent / "plugins/endless_elevator/content.json"
        self._definitions = None

    def _catalog(self):
        if self._definitions is None:
            self._definitions = json.loads(self._content.read_text(encoding="utf-8"))
        return self._definitions

    def _inventory(self, db, uid):
        return {
            (row[0], row[1], row[2]): row[3] for row in db.execute(
                "SELECT item_id,quality,value,quantity FROM inventory WHERE user_id=?", (uid,)
            )
        }

    def _archive(self, db, uid):
        return dict(db.execute("SELECT item_id,count FROM collections WHERE user_id=?", (uid,)))

    @staticmethod
    def _signal(db, uid):
        row = db.execute("SELECT group_id, expires_at FROM signals WHERE owner_id=?", (uid,)).fetchone()
        return tuple(row) if row else None

    @staticmethod
    def _enqueue(db, group, body, *, signal_owner=None, signal_expires_at=None):
        _ensure_outbox(db)
        now = int(time.time())
        db.execute(
            "INSERT INTO elevator_web_announcements(group_id,body,created_at,next_at,signal_owner,signal_expires_at) "
            "VALUES(?,?,?,?,?,?)",
            (group, body, now, now, signal_owner, signal_expires_at),
        )

    def _season_record(self, db, uid, group):
        row = db.execute(
            "SELECT month, best_extract_floor, best_extract_value FROM season_stats "
            "WHERE user_id=? ORDER BY month DESC LIMIT 1", (uid,)
        ).fetchone()
        if row is None:
            return (0, 0), (0, 0)
        other = db.execute(
            "SELECT s.best_extract_floor,s.best_extract_value FROM season_stats s JOIN group_active_users g "
            "ON g.month=s.month AND g.user_id=s.user_id WHERE s.month=? AND g.group_id=? AND s.user_id<>? "
            "ORDER BY s.best_extract_floor DESC,s.best_extract_value DESC LIMIT 1", (row[0], group, uid)
        ).fetchone()
        return (row[1], row[2]), tuple(other) if other else (0, 0)

    def before(self, db, uid, group, action):
        player = db.execute("SELECT extracts FROM players WHERE user_id=?", (uid,)).fetchone()
        if player is None:
            return None
        run = db.execute("SELECT payload FROM active_runs WHERE user_id=?", (uid,)).fetchone()
        if run is None:
            return None
        run = json.loads(run[0])
        if run.get("sim") or run.get("prophecy"):
            return None
        personal, group_record = self._season_record(db, uid, group)
        last_gear = db.execute("SELECT coalesce(max(equip_id),0) FROM equipment WHERE user_id=?", (uid,)).fetchone()[0]
        return {"extracts": player[0], "inventory": self._inventory(db, uid), "archive": self._archive(db, uid),
                "personal": personal, "group_record": group_record, "run": run, "last_gear": last_gear,
                "signal": self._signal(db, uid) if action in ("flare", "use") else None}

    def after(self, db, uid, group, action, before):
        if before is None:
            return
        name, extracts = db.execute("SELECT nickname,extracts FROM players WHERE user_id=?", (uid,)).fetchone()
        if action in ("flare", "use"):
            signal = self._signal(db, uid)
            if signal and signal[0] == group and signal != before["signal"]:
                run = before["run"]
                map_name = self._catalog()["maps"][run["map"]]["name"]
                enemy = run["event"]["enemy"]
                display = " ".join(str(name).split())[:32]
                body = (f"🚨 无尽电梯 · {display} 从网页发出求援\n"
                        f"{map_name} F{run['floor']} · {enemy['name']}（剩 {enemy['hp']:,} HP）\n"
                        "在群里发「支援」即可帮忙。信号有效期 3 分钟。")
                self._enqueue(db, group, body, signal_owner=uid, signal_expires_at=signal[1])
        content = self._catalog()
        definitions = content["items"]
        now_owned = self._archive(db, uid)
        previous = before["archive"]
        lines = []
        for key in content["expedition"]["rainbow"]:
            added = now_owned.get(key, 0) - previous.get(key, 0)
            if added > 0:
                item_name = definitions[key]["name"]
                event = f"解锁永久虹彩技能「{item_name}」" if key not in previous else f"再次获得虹彩「{item_name}」"
                lines.append(f"🌈 {event}，已入仓 ×{added}")
        if not lines and extracts <= before["extracts"]:
            return
        if extracts > before["extracts"]:
            found = []
            for base, count in db.execute(
                    "SELECT base_id,count(*) FROM equipment WHERE user_id=? AND equip_id>? AND rarity='red' GROUP BY base_id",
                    (uid, before["last_gear"])):
                if base in content["equipment"]:
                    lines.append(f"🟥 带出红装「{content['equipment'][base]['name']}」×{count}")
            for (key, quality, value), count in self._inventory(db, uid).items():
                rarity = definitions.get(key, {}).get("rarity")
                added = count - before["inventory"].get((key, quality, value), 0)
                if key in definitions and added > 0 and (rarity == "red" or value >= HIGH_VALUE):
                    found.append((value, definitions[key]["name"], added))
            found.sort(reverse=True)
            if found:
                shown = [f"{label} ×{count}（单价 {value:,} 金）" for value, label, count in found[:3]]
                lines.append("💎 成功带出 " + "、".join(shown) + (f" 等 {len(found)} 种" if len(found) > 3 else ""))
            personal, _ = self._season_record(db, uid, group)
            if personal[0] >= RECORD_FLOOR and personal[0] > before["personal"][0]:
                group_best = personal > max(before["personal"], before["group_record"])
                title = "刷新本群本月撤离纪录" if group_best else "刷新个人本月撤离层数纪录"
                lines.append(f"🏆 {title} · F{personal[0]} · 带出 {personal[1]:,} 金")
            elif personal[0] >= RECORD_FLOOR and personal > max(before["personal"], before["group_record"]):
                lines.append(f"🏆 刷新本群本月同层撤离估值纪录 · F{personal[0]} · 带出 {personal[1]:,} 金")
        if lines:
            map_name = content["maps"].get(before["run"]["map"], {}).get("name", "无尽电梯")
            display = " ".join(str(name).split())[:32]
            body = "\n".join([f"🛗 无尽电梯 · {map_name} · {display}", *lines])
            self._enqueue(db, group, body)

    def claim(self):
        if not self.path.is_file():
            return None
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='elevator_web_announcements'").fetchone():
                return None
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='elevator_maintenance'").fetchone() and db.execute(
                "SELECT 1 FROM elevator_maintenance WHERE id=1"
            ).fetchone():
                return None
            _ensure_outbox(db)
            now = int(time.time())
            db.execute(
                """DELETE FROM elevator_web_announcements AS notice WHERE created_at<? OR attempts>=4
                   OR (notice.signal_owner IS NOT NULL AND NOT EXISTS (
                       SELECT 1 FROM signals WHERE owner_id=notice.signal_owner AND group_id=notice.group_id
                       AND expires_at=notice.signal_expires_at AND expires_at>?
                   ))""",
                (now-MAX_DELIVERY_AGE, now),
            )
            row = db.execute(
                "SELECT id,group_id,body,attempts FROM elevator_web_announcements WHERE next_at<=? "
                "ORDER BY signal_owner IS NULL,id LIMIT 1",
                (now,),
            ).fetchone()
            if row:
                db.execute("UPDATE elevator_web_announcements SET next_at=?,attempts=attempts+1 WHERE id=?", (now+120,row[0]))
            return row

    def acknowledge(self, entry_id):
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("DELETE FROM elevator_web_announcements WHERE id=?", (entry_id,))
