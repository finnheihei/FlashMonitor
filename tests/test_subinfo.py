"""subinfo.py：机场账期的缓存、刷新和读 FlClash 当前订阅。"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from flashmonitor.subinfo import MIN_CACHED_USED, SubInfo, flclash_current_profile

GB = 1024**3


def make_flclash(root: Path, *, info: dict | None, url: str = "https://sub.example/x", pid: int = 3) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "shared_preferences.json").write_text(
        json.dumps({"flutter.config": json.dumps({"currentProfileId": pid})}), encoding="utf-8"
    )
    conn = sqlite3.connect(root / "database.sqlite")
    conn.execute("CREATE TABLE profiles (id INTEGER, label TEXT, url TEXT, subscription_info TEXT)")
    conn.execute("INSERT INTO profiles VALUES (?, ?, ?, ?)", (pid, "我的机场", url, json.dumps(info) if info else None))
    conn.commit()
    conn.close()
    return root


class SubInfoTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.now = [1_800_000_000.0]
        self.url = ""
        self.fetched: list[str] = []
        self.live: dict | None = {"upload": 1 * GB, "download": 9 * GB, "total": 100 * GB, "expire": 1_900_000_000}

    def fetch(self, url: str):
        self.fetched.append(url)
        if isinstance(self.live, Exception):
            raise self.live
        return self.live

    def make(self, flclash: Path | None = None) -> SubInfo:
        return SubInfo(
            self.dir / "subinfo.json",
            sub_url=lambda: self.url,
            flclash_dir=flclash or self.dir / "no-flclash",
            fetch=self.fetch,
            spawn=lambda fn: fn(),
            clock=lambda: self.now[0],
        )

    def test_live_info_is_used_and_cached_on_disk(self) -> None:
        self.url = "https://sub.example/me"
        s = self.make()
        s.schedule(force=True)
        pub = s.public()
        self.assertEqual((pub["used"], pub["total"], pub["source"]), (10 * GB, 100 * GB, "live"))
        self.assertEqual(self.fetched, ["https://sub.example/me"])
        s2 = self.make()
        s2.schedule()
        self.assertEqual(s2.public()["used"], 10 * GB, "重启后先显示磁盘里的")
        self.assertEqual(len(self.fetched), 1, "缓存没过期就不再请求")
        self.now[0] += 301
        s2.schedule()
        self.assertEqual(len(self.fetched), 2)

    def test_flclash_profile_url_and_cache(self) -> None:
        fl = make_flclash(self.dir / "fl", info={"upload": 2 * GB, "download": 3 * GB, "total": 50 * GB, "expire": 0})
        self.live = None  # 订阅地址连不上：用 FlClash 缓存的
        s = self.make(fl)
        s.schedule(force=True)
        self.assertEqual(self.fetched, ["https://sub.example/x"])
        pub = s.public()
        self.assertEqual((pub["label"], pub["used"], pub["source"]), ("我的机场", 5 * GB, "cache"))

    def test_tiny_cached_numbers_are_hidden(self) -> None:
        fl = make_flclash(self.dir / "fl", info={"upload": 1, "download": MIN_CACHED_USED - 10, "total": 50 * GB})
        self.live = None
        s = self.make(fl)
        s.schedule(force=True)
        self.assertIsNone(s.public())

    def test_failed_refresh_does_not_get_stuck(self) -> None:
        self.url = "https://sub.example/me"
        self.live = RuntimeError("boom")
        s = self.make()
        s.schedule(force=True)
        s.schedule(force=True)
        self.assertEqual(len(self.fetched), 2, "0.8.0 在这里卡住，之后再也不刷新")
        self.live = {"upload": 1, "download": 2, "total": 3, "expire": 0}
        s.schedule(force=True)
        self.assertEqual(s.public()["total"], 3)

    def test_profile_reader_edge_cases(self) -> None:
        self.assertIsNone(flclash_current_profile(self.dir / "missing"))
        fl = make_flclash(self.dir / "fl", info=None)
        self.assertEqual(flclash_current_profile(fl), {"label": "我的机场", "url": "https://sub.example/x", "cached": None})
        (fl / "shared_preferences.json").write_text("{bad", encoding="utf-8")
        self.assertIsNone(flclash_current_profile(fl))


if __name__ == "__main__":
    unittest.main()
