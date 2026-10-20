"""netinfo.py：路由缓存的过期 / 失效规则、命令行缓存、文件解析缓存。"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from flashmonitor.netinfo import CmdlineCache, FileMemo, RouteCache, as_list, route_addrs


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def routes(*ips: str, tag: str = "") -> dict:
    return {"tag": tag, "addrs": [{"IPAddress": ip, "InterfaceAlias": "WLAN"} for ip in ips], "routes": []}


class RouteCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.results: list[dict] = []
        self.calls = 0
        self.spawned: list = []

    def fetch(self) -> dict:
        self.calls += 1
        return self.results.pop(0)

    def cache(self) -> RouteCache:
        return RouteCache(self.fetch, ttl=30, min_gap=5, clock=self.clock, spawn=self.spawned.append)

    def test_first_get_is_synchronous_then_cached(self) -> None:
        self.results = [routes("192.168.1.10", tag="a")]
        c = self.cache()
        self.assertEqual(c.get()["tag"], "a")
        self.clock.t += 29
        self.assertEqual(c.get()["tag"], "a")
        self.assertEqual((self.calls, self.spawned), (1, []))

    def test_expired_data_is_served_while_refreshing_in_background(self) -> None:
        self.results = [routes(tag="a"), routes(tag="b")]
        c = self.cache()
        c.get()
        self.clock.t += 31
        self.assertEqual(c.get()["tag"], "a")
        self.assertEqual(c.get()["tag"], "a")
        self.assertEqual(len(self.spawned), 1, "只起一个后台刷新")
        self.spawned.pop()()
        self.assertEqual(c.get()["tag"], "b")
        self.assertEqual(self.calls, 2)

    def test_invalidate_forces_sync_refresh_but_is_rate_limited(self) -> None:
        self.results = [routes(tag="a"), routes(tag="b"), routes(tag="c")]
        c = self.cache()
        c.get()
        self.assertTrue(c.invalidate())
        self.assertEqual(c.get()["tag"], "b")
        self.clock.t += 1
        self.assertFalse(c.invalidate())
        self.assertEqual(c.get()["tag"], "b")
        self.clock.t += 5
        self.assertTrue(c.invalidate())
        self.assertEqual(c.get()["tag"], "c")

    def test_tun_toggle_invalidates(self) -> None:
        self.results = [routes(tag="a"), routes(tag="b")]
        c = self.cache()
        c.get()
        c.note_tun(None)
        c.note_tun(False)
        c.note_tun(False)
        self.assertEqual(c.get()["tag"], "a")
        c.note_tun(True)
        self.assertEqual(c.get()["tag"], "b")

    def test_unknown_local_ip_invalidates_once(self) -> None:
        self.results = [routes("192.168.1.10", tag="a"), routes("192.168.1.10", tag="b"), routes(tag="c")]
        c = self.cache()
        c.get()
        c.note_local_ips(["192.168.1.10", "::1", "fe80::1%3", "0.0.0.0", "garbage"])
        self.assertEqual(c.get()["tag"], "a", "已知 IP 和 IPv6 不触发")
        c.note_local_ips(["10.8.0.2"])
        self.assertEqual(c.get()["tag"], "b")
        self.clock.t += 10
        c.note_local_ips(["10.8.0.2"])
        self.assertEqual(c.get()["tag"], "b", "同一个陌生 IP 只问一次")
        self.assertEqual(self.calls, 2)

    def test_failed_fetch_keeps_old_data(self) -> None:
        def flaky() -> dict:
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("powershell 挂了")
            return routes(tag=str(self.calls))

        c = RouteCache(flaky, ttl=30, min_gap=5, clock=self.clock, spawn=lambda fn: fn())
        self.assertEqual(c.get()["tag"], "1")
        c.invalidate()
        self.assertEqual(c.get()["tag"], "1")
        self.clock.t += 31
        c.get()
        self.assertEqual(c.get()["tag"], "3")

    def test_first_fetch_failure_gives_empty_dict(self) -> None:
        c = RouteCache(lambda: (_ for _ in ()).throw(RuntimeError("x")), clock=self.clock, spawn=lambda fn: fn())
        self.assertEqual(c.get(), {})

    def test_concurrent_first_get_fetches_once(self) -> None:
        gate = threading.Event()

        def slow() -> dict:
            self.calls += 1
            gate.wait(5)
            return routes(tag="x")

        c = RouteCache(slow, clock=time.monotonic)
        out: list[dict] = []
        threads = [threading.Thread(target=lambda: out.append(c.get())) for _ in range(4)]
        for t in threads:
            t.start()
        time.sleep(0.1)
        gate.set()
        for t in threads:
            t.join(5)
        self.assertEqual([d["tag"] for d in out], ["x"] * 4)
        self.assertEqual(self.calls, 1)

    def test_helpers(self) -> None:
        self.assertEqual(as_list({"a": 1}), [{"a": 1}])
        self.assertEqual(as_list([{"a": 1}, 3, None]), [{"a": 1}])
        self.assertEqual(as_list(None), [])
        self.assertEqual(route_addrs({"addrs": {"IPAddress": "1.2.3.4", "InterfaceAlias": "X"}}), {"1.2.3.4": "X"})
        self.assertEqual(route_addrs(None), {})


class CmdlineCacheTest(unittest.TestCase):
    def test_cache_and_batching(self) -> None:
        clock = Clock()
        asked: list[list[int]] = []

        def lookup(pids):
            pids = list(pids)
            asked.append(pids)
            return {p: f"cmd {p}   --x" for p in pids if p % 2 == 0}

        generations = {pid:pid+100 for pid in range(1,200)}
        c = CmdlineCache(lookup, ttl=45, clock=clock, identity_lookup=lambda pid:dict(status='verified',created=generations[pid]))
        self.assertEqual(c.get({2, 3, 0, -1}, generations=generations), {2: "cmd 2 --x"})
        self.assertEqual(c.get([2, 3], generations=generations), {2: "cmd 2 --x"})
        self.assertEqual(asked, [[2, 3]], "查不到的也缓存")
        clock.t += 46
        c.get([3], generations=generations)
        self.assertEqual(asked[-1], [3])
        many = list(range(100, 200))
        c.get(many, generations=generations)
        self.assertEqual(len(asked[-1]), 30)

    def test_lookup_errors_are_contained(self) -> None:
        def boom(pids):
            raise OSError("x")

        self.assertEqual(CmdlineCache(boom).get([5]), {})


class FileMemoTest(unittest.TestCase):
    def test_reparses_only_when_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            parsed: list[str] = []

            def parse(p: Path) -> str:
                text = p.read_text(encoding="utf-8") if p.is_file() else "<missing>"
                parsed.append(text)
                return text

            memo = FileMemo(path, parse)
            self.assertEqual(memo.get(), "<missing>")
            self.assertEqual(memo.get(), "<missing>")
            path.write_text("a", encoding="utf-8")
            self.assertEqual(memo.get(), "a")
            self.assertEqual(memo.get(), "a")
            path.write_text("bb", encoding="utf-8")
            os.utime(path, ns=(time.time_ns(), time.time_ns() + 10**9))
            self.assertEqual(memo.get(), "bb")
            path.unlink()
            self.assertEqual(memo.get(), "<missing>")
            self.assertEqual(parsed, ["<missing>", "a", "bb", "<missing>"])


if __name__ == "__main__":
    unittest.main()
