import sqlite3
import tempfile
import unittest
from pathlib import Path

from elevator_web_game import rules


class RushSettingsStorage(unittest.TestCase):
    def test_old_preferences_get_safe_defaults_and_keep_player_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "game.sqlite3"
            with rules.Store(path).transaction() as db:
                db.execute("INSERT INTO players(user_id,nickname) VALUES('101','旧玩家')")
                db.execute("DROP TABLE rush_preferences")
                db.execute("""CREATE TABLE rush_preferences (
                               user_id TEXT PRIMARY KEY REFERENCES players(user_id),
                               floors INTEGER NOT NULL CHECK(floors IN (3,5,10)),
                               skip_valuable INTEGER NOT NULL CHECK(skip_valuable IN (0,1)))""")
                db.execute("INSERT INTO rush_preferences VALUES('101',10,1)")

            with rules.Store(path).transaction() as db:
                self.assertEqual(tuple(db.execute(
                    "SELECT floors,skip_valuable,event_mode,auto_elite FROM rush_preferences"
                ).fetchone()), (10, 1, "pause", 0))
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("UPDATE rush_preferences SET event_mode='unknown'")
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("UPDATE rush_preferences SET auto_elite=2")
                db.execute("UPDATE rush_preferences SET event_mode='random',auto_elite=1")

            with rules.Store(path).read() as db:
                self.assertEqual(tuple(db.execute(
                    "SELECT floors,skip_valuable,event_mode,auto_elite FROM rush_preferences"
                ).fetchone()), (10, 1, "random", 1))
