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
        self.assertEqual(s.public()["status"], "error")
        self.assertNotIn("used", s.public())

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

    def test_first_failure_and_retry_backoff(self):
        self.url = "https://sub.example/a"
        self.live = None
        s = self.make()
        s.schedule()
        self.assertEqual(s.public(), {"status": "error", "updated_at": 0})
        s.schedule()
        self.assertEqual(len(self.fetched), 1)
        self.now[0] += 301
        s.schedule()
        self.assertEqual(len(self.fetched), 2)
        s.schedule(force=True)
        self.assertEqual(len(self.fetched), 3)

    def test_same_source_failure_retains_timestamp_and_marks_cache(self):
        self.url = "https://sub.example/a"
        s = self.make()
        s.schedule()
        timestamp = s.public()["updated_at"]
        self.now[0] += 301
        self.live = None
        s.schedule()
        pub = s.public()
        self.assertEqual((pub["used"], pub["status"], pub["source"]), (10*GB, "error", "cache"))
        self.assertEqual(pub["updated_at"], timestamp)
        restored = self.make()
        restored.schedule()
        self.assertEqual(restored.public()["updated_at"], timestamp)
        self.assertEqual(restored.public()["status"], "error", "old disk data is not made fresh by restarting")

    def test_changed_source_clears_old_balance_before_fetch(self):
        self.url = "https://sub.example/a"
        s = self.make()
        s.schedule()
        jobs = []
        s._spawn = jobs.append
        self.url = "https://sub.example/b"
        s.schedule()
        self.assertEqual(s.public(), {"status": "loading", "updated_at": 0})
        self.live = None
        jobs.pop()()
        self.assertEqual(s.public(), {"status": "error", "updated_at": 0})
        restored = self.make()
        restored.schedule()
        self.assertNotIn("total", restored.public())
        self.url = ""
        s.schedule()
        self.assertIsNone(s.public())

    def test_manual_url_never_uses_other_flclash_profile(self):
        fl = make_flclash(self.dir / "fl", info={"upload": 2*GB, "download": 3*GB, "total": 50*GB})
        self.url = "https://different.example/private"
        self.live = None
        s = self.make(fl)
        s.schedule()
        self.assertNotIn("used", s.public())
        self.assertNotIn("label", s.public())

    def test_legacy_cache_is_not_attributed_to_current_source(self):
        (self.dir / "subinfo.json").write_text(json.dumps({"upload": GB, "total": 100*GB}), encoding="utf-8")
        self.url = "https://sub.example/a"
        self.live = None
        s = self.make()
        s.schedule()
        self.assertNotIn("used", s.public())

    def test_inflight_source_switch_discards_old_result_and_fetches_new(self):
        self.url = "https://sub.example/a"
        jobs = []
        s = self.make()
        s._spawn = jobs.append
        def fetch(url):
            self.fetched.append(url)
            if url.endswith('/a'):
                self.url = "https://sub.example/b"
                s.schedule(force=True)
                self.assertNotIn("used", s.public())
                return self.live
            return {"upload": 0, "download": GB, "total": 20*GB}
        s._fetch = fetch
        s.schedule()
        jobs.pop(0)()
        self.assertNotIn("used", s.public())
        self.assertFalse(s.cache_file.exists(), "discarded requests must not poison disk cache")
        self.assertEqual(len(jobs), 1)
        jobs.pop(0)()
        self.assertEqual(s.public()["total"], 20*GB)
        self.assertEqual(s.public()["status"], "ready")
        raw = s.cache_file.read_text(encoding="utf-8")
        self.assertNotIn(self.url, raw)
        self.assertNotIn("source_key", s.public())

    def test_unconfigured_does_not_fetch_and_spawn_failure_can_retry(self):
        s = self.make()
        s.schedule()
        self.assertIsNone(s.public())
        self.assertEqual(self.fetched, [])
        self.url = "https://sub.example/a"
        def fail_spawn(fn):
            raise RuntimeError("no thread")
        s._spawn = fail_spawn
        s.schedule()
        self.assertEqual(s.public()["status"], "error")
        s._spawn = lambda fn: fn()
        s.schedule(force=True)
        self.assertEqual(s.public()["status"], "ready")

    def test_profile_reader_edge_cases(self) -> None:
        self.assertIsNone(flclash_current_profile(self.dir / "missing"))
        fl = make_flclash(self.dir / "fl", info=None)
        self.assertEqual(flclash_current_profile(fl), {"label": "我的机场", "url": "https://sub.example/x", "cached": None})
        (fl / "shared_preferences.json").write_text("{bad", encoding="utf-8")
        self.assertIsNone(flclash_current_profile(fl))


if __name__ == "__main__":
    unittest.main()
