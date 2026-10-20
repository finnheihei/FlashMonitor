"""db.py：SQLite 表结构、事务、过滤、损坏 / 打不开时的处理。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flashmonitor.db import DB_NAME, TrafficDB


def program(**kw):
    row = {"upload": 0, "download": 0, "proxy_up": 0, "proxy_down": 0, "direct_up": 0, "direct_down": 0, "conns": 0, "last": 0}
    row.update(kw)
    return row


class TrafficDBTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / DB_NAME

    def open(self) -> TrafficDB:
        db = TrafficDB(self.path)
        self.addCleanup(db.close)
        return db

    def test_fresh_database(self) -> None:
        db = self.open()
        self.assertTrue(db.persistent)
        self.assertFalse(db.migrated())
        self.assertEqual((db.load_programs(), db.load_seen(), db.count_slots()), ({}, set(), 0))
        db.write(meta={"schema": "1"})
        self.assertTrue(db.migrated())
        self.assertEqual(db.meta("schema"), "1")
        self.assertIsNone(db.meta("nope"))

    def test_slot_upsert_accumulates_and_filters(self) -> None:
        db = self.open()
        db.write(slot_rows=[("proc", 900, "chrome", 1, 2, 3, 4), ("proc", 1800, "chrome", 10, 20, 30, 40), ("node", 900, "HK", 5, 6, 0, 0)])
        db.write(slot_rows=[("proc", 900, "chrome", 1, 1, 1, 1), ("proc", 900, "Code", 7, 0, 0, 0)])
        self.assertEqual(db.slot_totals("proc"), {"chrome": (12, 23, 34, 45), "Code": (7, 0, 0, 0)})
        self.assertEqual(db.slot_totals("proc", after=900), {"chrome": (10, 20, 30, 40)})
        self.assertEqual(db.slot_totals("proc", min_slot=1800), {"chrome": (10, 20, 30, 40)})
        self.assertEqual(db.slot_totals("proc", after=0, min_slot=901), {"chrome": (10, 20, 30, 40)})
        self.assertEqual(db.slot_totals("node"), {"HK": (5, 6, 0, 0)})
        self.assertEqual(db.slot_totals("site"), {})
        self.assertEqual(db.count_slots(), 4)
        self.assertEqual(db.count_slots(before=1800), 3)

    def test_program_rows_replace_sites(self) -> None:
        db = self.open()
        sites = {"a.com": program(upload=1), "b.com": program(download=2)}
        db.write(programs={"chrome": program(upload=5, last_node="HK", title="Chrome", sites=sites)}, seen=["chrome", "x"])
        db.write(programs={"chrome": program(upload=9, last_cmd="c", sites={"b.com": program(download=3)})}, seen=["x"])
        got = db.load_programs()
        self.assertEqual(set(got), {"chrome"})
        row = got["chrome"]
        self.assertEqual((row["upload"], row["last_node"], row["last_cmd"], row["title"]), (9, "", "c", ""))
        self.assertEqual(set(row["sites"]), {"b.com"})
        self.assertEqual(row["sites"]["b.com"]["download"], 3)
        self.assertEqual(db.load_seen(), {"chrome", "x"})

    def test_reset_and_prune(self) -> None:
        db = self.open()
        db.write(programs={"p": program(upload=1)}, slot_rows=[("proc", 100, "p", 1, 0, 0, 0), ("proc", 5000, "p", 1, 0, 0, 0)], seen=["p"])
        db.write(prune_before=1000)
        self.assertEqual(db.count_slots(), 1)
        db.write(reset=True, slot_rows=[("site", 7000, "s", 0, 0, 1, 1)])
        self.assertEqual(db.load_programs(), {})
        self.assertEqual(db.slot_totals("proc"), {})
        self.assertEqual(db.slot_totals("site"), {"s": (0, 0, 1, 1)})
        self.assertEqual(db.load_seen(), {"p"}, "清零不清已见过的程序")

    def test_failed_write_rolls_back_everything(self) -> None:
        db = self.open()
        with self.assertRaises(sqlite3.Error):
            db.write(programs={"p": program(upload=1)}, slot_rows=[("proc", 1, "p", 1, 2)], meta={"schema": "1"})
        self.assertEqual((db.load_programs(), db.count_slots()), ({}, 0))
        self.assertFalse(db.migrated())
        db.write(programs={"p": program(upload=1)})
        self.assertEqual(db.load_programs()["p"]["upload"], 1)

    def test_data_survives_reopen(self) -> None:
        db = TrafficDB(self.path)
        db.write(slot_rows=[("proc", 1, "p", 1, 2, 3, 4)], meta={"schema": "1"})
        db.close()
        again = self.open()
        self.assertTrue(again.migrated())
        self.assertEqual(again.slot_totals("proc"), {"p": (1, 2, 3, 4)})

    def test_corrupt_file_is_moved_aside(self) -> None:
        self.path.write_bytes(b"garbage, not sqlite" * 100)
        db = self.open()
        self.assertTrue(db.persistent)
        self.assertFalse(db.migrated())
        moved = [p for p in self.path.parent.iterdir() if p.name.startswith(DB_NAME + ".corrupt-")]
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0].read_bytes(), b"garbage, not sqlite" * 100)

    def test_unopenable_file_falls_back_to_memory_without_touching_it(self) -> None:
        self.path.write_bytes(b"")
        real = TrafficDB._connect

        def locked(target: str):
            if target != ":memory:":
                raise sqlite3.OperationalError("database is locked")
            return real(target)

        with mock.patch.object(TrafficDB, "_connect", side_effect=locked):
            db = self.open()
        self.assertFalse(db.persistent)
        db.write(slot_rows=[("proc", 1, "p", 1, 0, 0, 0)])
        self.assertEqual(db.slot_totals("proc"), {"p": (1, 0, 0, 0)})
        self.assertEqual([p.name for p in self.path.parent.iterdir()], [DB_NAME])

    def test_closed_database_raises(self) -> None:
        db = TrafficDB(self.path)
        db.close()
        db.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            db.load_seen()
        with self.assertRaises(sqlite3.ProgrammingError):
            db.write(seen=["x"])


if __name__ == "__main__":
    unittest.main()
