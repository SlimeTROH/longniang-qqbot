"""Pause gameplay, snapshot SQLite, notify active explorers, and restore game timers."""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from elevator_web_game import rules


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("pause", "resume", "status"))
    args = parser.parse_args()
    path = ROOT / "state" / "endless_elevator.sqlite3"
    if not path.is_file():
        raise SystemExit("Game database missing")
    store = rules.Store(path)
    with store.read() as db:
        db.execute("BEGIN IMMEDIATE")
        now = int(time.time())
        row = db.execute("SELECT started_at FROM elevator_maintenance WHERE id=1").fetchone()
        if args.action == "status":
            print("PAUSED" if row else "OPEN")
            return
        if args.action == "resume":
            if not row:
                print("ALREADY_OPEN")
                return
            start = row[0]
            duration = max(0, now - start)
            db.execute("UPDATE active_runs SET updated_at=updated_at+? WHERE updated_at>?",
                       (duration, start-rules.RUN_TIMEOUT))
            for table, column in (("relics","expires_at"), ("relics","reserved_until"), ("signals","expires_at"),
                                  ("group_buffs","expires_at"), ("ai_orders","expires_at")):
                db.execute(f"UPDATE {table} SET {column}={column}+? WHERE {column}>?", (duration,start))
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='elevator_web_announcements'").fetchone():
                db.execute("UPDATE elevator_web_announcements SET signal_expires_at=signal_expires_at+?,"
                           " created_at=created_at+? WHERE signal_expires_at>?", (duration,duration,start))
            db.execute("DELETE FROM elevator_maintenance WHERE id=1")
            resumed_players = list(db.execute("SELECT group_id,user_id FROM active_runs WHERE updated_at>? ORDER BY group_id,user_id",
                                              (now-rules.RUN_TIMEOUT,)))
            db.commit()
            print("RESUMED", duration, flush=True)
            if resumed_players:
                backup = ROOT / "backups" / ("elevator-paused-" + datetime.fromtimestamp(start).strftime("%Y%m%d-%H%M%S"))
                asyncio.run(notify(resumed_players, backup, resumed=True))
            print("RESUME_NOTICE_COMPLETE",len(resumed_players))
            return
        start = row[0] if row else now
        db.execute("INSERT OR IGNORE INTO elevator_maintenance(id,started_at) VALUES(1,?)", (start,))
        players = list(db.execute("SELECT group_id,user_id FROM active_runs WHERE updated_at>? ORDER BY group_id,user_id",
                                  (start-rules.RUN_TIMEOUT,)))
        db.commit()
    backup = ROOT / "backups" / ("elevator-paused-" + datetime.fromtimestamp(start).strftime("%Y%m%d-%H%M%S"))
    backup.mkdir(parents=True, exist_ok=True)
    for source in (path, ROOT / "state" / "elevator_web_auth.sqlite3"):
        if not source.exists():
            continue
        destination = backup/source.name
        if not destination.exists():
            pending = destination.with_suffix(".partial")
            with closing(sqlite3.connect(source.resolve().as_uri()+"?mode=ro",uri=True)) as live, \
                 closing(sqlite3.connect(pending)) as saved:
                live.backup(saved)
                if saved.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("SQLite backup verification failed")
            pending.replace(destination)
    print("PAUSED_SNAPSHOT",backup, "ACTIVE",len(players), flush=True)
    if players:
        asyncio.run(notify(players, backup))
    print("ACTIVE_NOTICE_COMPLETE",len(players))


async def notify(players, backup, *, resumed=False):
    from qqctl import OneBot, ok

    groups = defaultdict(list)
    for group_id, user in players:
        groups[("group", group_id) if str(group_id).isdigit() else ("private", user)].append(user)
    sent_log = backup/("reopened.txt" if resumed else "notified.txt")
    sent = set(sent_log.read_text().splitlines()) if sent_log.exists() else set()
    text = ("无尽电梯维护结束，你的探险已恢复。刷新网页或在群里发「电梯」可继续本局。"
            if resumed else "无尽电梯开始版本维护。你当前的探险已保存，探索计时暂停，维护结束后可继续。")
    async with OneBot() as bot:
        for (kind, target), users in groups.items():
            marker = f"{kind}-{target}"
            if marker in sent:
                continue
            message = ([{"type":"at","data":{"qq":str(uid)}} for uid in users] if kind=="group" else [])
            message.append({"type":"text","data":{"text":" "+text}})
            response = await bot.call(f"send_{kind}_msg", **{("group_id" if kind=="group" else "user_id"):int(target)},
                                      message=message)
            if not ok(response):
                raise RuntimeError("QQ maintenance notice delivery failed")
            with sent_log.open("a",encoding="utf-8") as log:
                log.write(marker+"\n")


if __name__ == "__main__":
    main()
