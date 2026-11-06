import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import db


class StateUpgradeTests(unittest.TestCase):
    def test_existing_cursors_pause_and_history_survive_initialization(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT NOT NULL)")
                original = [
                    ("paused", "1"),
                    ("tg_source_last_id:123", "456"),
                    ("tg_house_last_id:789", "234"),
                    ("last_uid", "331842"),
                ]
                conn.executemany("INSERT INTO kv VALUES (?, ?)", original)
                conn.execute("CREATE TABLE stats_daily(date TEXT PRIMARY KEY, total INTEGER)")
                conn.execute("INSERT INTO stats_daily VALUES ('2026-11-06', 74)")
            with patch.object(db, "DB_PATH", path):
                db.init_db()
                self.assertTrue(db.get_paused())
                self.assertEqual(db.get_tg_source_last_id("123"), 456)
                self.assertEqual(db.get_tg_house_last_id("789"), 234)
                with sqlite3.connect(path) as conn:
                    self.assertEqual(conn.execute("SELECT k,v FROM kv ORDER BY k").fetchall(), sorted(original))
                db.set_tg_source_last_id("123", 457)
                db.set_tg_house_last_id("789", 235)
                self.assertEqual(db.get_tg_source_last_id("123"), 457)
                self.assertEqual(db.get_tg_house_last_id("789"), 235)
                with sqlite3.connect(path) as conn:
                    self.assertEqual(conn.execute("SELECT * FROM stats_daily").fetchall(), [("2026-11-06", 74)])


if __name__ == "__main__":
    unittest.main()
