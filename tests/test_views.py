"""views.py：每个页面只返回自己要的字段；排行缓存；非法参数的处理。"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from flashmonitor.store import TrafficStore
from flashmonitor.views import (
    LiveState,
    RankCache,
    build_all,
    build_view,
    lanes_with_live,
    normalize_period,
    normalize_view,
    rank_totals,
)

COMMON = {"view", "period", "ts", "ms", "status", "prefs", "alerts"}
PAGE_FIELDS = {
    "settings": COMMON,
    "overview": COMMON | {"live", "sub", "counts", "flows", "history", "rank"},
    "rank": COMMON | {"live", "sub", "rank"},
    "lanes": COMMON | {"live", "sub", "lanes"},
    "conns": COMMON | {"live", "sub", "flows"},
    "closed": COMMON | {"live", "sub", "closed"},
}


def flow(cid: str, proc: str, up: int, down: int, outbound: str = "proxy") -> dict:
    return {
        "id": cid,
        "process": proc,
        "upload": up,
        "download": down,
        "outbound": outbound,
        "node": "HK-01" if outbound == "proxy" else "",
        "remote": "www.google.com:443",
        "path": outbound,
        "inbound": "tun",
    }


class ViewsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = TrafficStore(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.store.close)
        self.store.update([])
        self.store.update([flow("a", "chrome", 10, 1000), flow("b", "Code", 5, 50, "direct")])
        self.store.update([flow("a", "chrome", 20, 2000)])  # b 结束
        self.store.record_history()
        self.calls: list[str] = []

        def compute(period: str) -> dict:
            self.calls.append(period)
            return self.store.ranking(period)

        self.ranks = RankCache(compute)
        self.state = {
            "ts": 123,
            "ms": 45,
            "status": {"clash_api": True, "mode": "tun", "up_speed": -1, "down_speed": -1},
            "counts": {"proxy": 2},
            "flows": [
                {"process": "chrome", "outbound": "proxy", "path": "proxy", "n": 2},
                {"process": "Code", "outbound": "direct", "path": "direct"},
            ],
            "leaks": {"count": 0, "rows": []},
            "alerts": [{"kind": "new", "process": "chrome", "ts": 1}],
        }

    def view(self, name: str, period: str = "today") -> dict:
        return build_view(name, period, state=self.state, store=self.store, ranks=self.ranks, prefs={"ignored": []}, sub=None)

    def test_each_page_gets_only_its_fields(self) -> None:
        for page, want in PAGE_FIELDS.items():
            data = self.view(page)
            self.assertEqual(set(data), want, page)
            self.assertNotIn("programs", data)
            json.dumps(data, ensure_ascii=False)

    def test_status_speeds_are_live(self) -> None:
        data = self.view("settings")
        self.assertGreaterEqual(data["status"]["up_speed"], 0)
        self.assertEqual(self.state["status"]["up_speed"], -1, "不改共享的采集结果")

    def test_overview_has_today_totals_only(self) -> None:
        today = self.view("overview")["rank"]
        self.assertEqual(set(today), {"today"})
        self.assertNotIn("rows", today["today"])
        self.assertEqual(today["today"]["proxy_download"], 2000)
        self.assertEqual(today["today"]["direct_download"], 50)
        self.assertEqual(today["today"]["programs"], 2)

    def test_rank_page_gets_only_requested_period(self) -> None:
        data = self.view("rank", "7d")
        self.assertEqual(set(data["rank"]), {"7d"})
        self.assertEqual([r["process"] for r in data["rank"]["7d"]["rows"]], ["chrome", "Code"])
        self.assertEqual(self.calls, ["7d"])

    def test_invalid_view_and_period(self) -> None:
        self.assertEqual(normalize_view("bogus"), "overview")
        self.assertEqual(normalize_view(" RANK "), "rank")
        self.assertEqual(normalize_period("5y"), "today")
        self.assertEqual(normalize_period(None), "today")
        data = self.view("rank", "5y")
        self.assertEqual((data["period"], set(data["rank"])), ("today", {"today"}))
        self.assertEqual(self.view("nope")["view"], "overview")

    def test_lanes_get_live_counts(self) -> None:
        lanes = self.view("lanes")["lanes"]
        self.assertEqual([(r["process"], r["live"]) for r in lanes["proxy"]["rows"]], [("chrome", 2)])
        self.assertEqual([(r["process"], r["live"]) for r in lanes["direct"]["rows"]], [("Code", 1)])

    def test_closed_page(self) -> None:
        closed = self.view("closed")["closed"]
        self.assertEqual([c["process"] for c in closed], ["Code"])

    def test_live_list_includes_programs_beyond_the_old_twelve_item_limit(self) -> None:
        expected = {f"program{i}" for i in range(15)}
        self.store.update([flow(name, name, 1, 100) for name in sorted(expected)])
        names = {row["process"] for row in self.view("overview")["live"]["eaters"]}
        self.assertTrue(expected <= names)

    def test_warming_and_error_states(self) -> None:
        warm = build_view("rank", "7d", state=None, store=self.store, ranks=self.ranks, prefs={}, sub=None)
        self.assertEqual(warm, {"error": "warming", "view": "rank", "period": "7d"})
        err = build_view("conns", "x", state={"error": "boom", "ts": 9}, store=self.store, ranks=self.ranks, prefs={}, sub=None)
        self.assertEqual((err["error"], err["flows"], err["view"]), ("boom", [], "conns"))

    def test_build_all_for_once(self) -> None:
        full = build_all(state=self.state, store=self.store, ranks=self.ranks, prefs={}, sub=None)
        self.assertEqual(set(full["rank"]), {"1h", "today", "7d", "30d", "all"})
        for key in ("status", "counts", "flows", "closed", "lanes", "history", "leaks", "alerts", "live", "prefs"):
            self.assertIn(key, full)
        self.assertNotIn("programs", full)

    def test_helpers(self) -> None:
        self.assertEqual(rank_totals({"upload": 1, "rows": [1]})["upload"], 1)
        self.assertEqual(lanes_with_live({}, []), {})


class RankCacheTest(unittest.TestCase):
    def test_ttl_and_invalidate(self) -> None:
        now = [0.0]
        calls: list[str] = []

        def compute(period: str) -> dict:
            calls.append(period)
            return {"period": period, "n": len(calls)}

        c = RankCache(compute, ttl=3.0, clock=lambda: now[0])
        self.assertEqual(c.get("today")["n"], 1)
        now[0] = 2.9
        self.assertEqual(c.get("today")["n"], 1)
        self.assertEqual(c.get("bogus")["n"], 1, "非法时段按今天")
        self.assertEqual(c.get("7d")["n"], 2)
        now[0] = 3.1
        self.assertEqual(c.get("today")["n"], 3)
        c.invalidate()
        self.assertEqual(c.get("today")["n"], 4)
        self.assertEqual(c.computes, 4)

    def test_concurrent_requests_compute_once(self) -> None:
        gate = threading.Event()
        calls: list[str] = []

        def compute(period: str) -> dict:
            calls.append(period)
            gate.wait(5)
            return {"period": period}

        c = RankCache(compute)
        out: list[dict] = []
        threads = [threading.Thread(target=lambda: out.append(c.get("30d"))) for _ in range(5)]
        for t in threads:
            t.start()
        time.sleep(0.1)
        gate.set()
        for t in threads:
            t.join(5)
        self.assertEqual(calls, ["30d"])
        self.assertEqual(len(out), 5)

    def test_invalidate_during_compute_is_not_cached(self) -> None:
        c: RankCache

        def compute(period: str) -> dict:
            c.invalidate()  # 算的过程中清零了
            return {"stale": True}

        c = RankCache(compute)
        c.get("today")
        self.assertEqual(c.computes, 1)
        c.get("today")
        self.assertEqual(c.computes, 2, "旧结果没有被缓存")


class LiveStateTest(unittest.TestCase):
    def test_accessors(self) -> None:
        s = LiveState()
        self.assertEqual((s.get(), s.status(), s.alerts()), (None, {}, []))
        s.set({"status": {"mode": "tun"}, "alerts": [{"kind": "leak"}]})
        self.assertEqual(s.status(), {"mode": "tun"})
        self.assertEqual(s.alerts(), [{"kind": "leak"}])
        s.set({"error": "x"})
        self.assertEqual((s.status(), s.alerts()), ({}, []))


if __name__ == "__main__":
    unittest.main()
