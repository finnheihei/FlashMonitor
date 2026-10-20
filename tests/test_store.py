"""TrafficStore 测试。

TrafficStoreBehaviourTest 只调用公开方法，描述 0.8.0 原有的统计口径（已用同一套用例跑过旧实现）。
以后换存储实现，这些用例一行都不改。
TrafficStoreFixesTest 是新的写盘语义和顺手修掉的问题，旧实现不满足。
MigrationTest 把 0.8.0 格式的 JSON 导进 SQLite，和直接按 JSON 算出来的排行逐项对照。
"""

from __future__ import annotations

import json
import math
import random
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from flashmonitor import store as store_mod
from flashmonitor.db import DB_NAME, TrafficDB
from flashmonitor.store import TrafficStore


def local(y: int, mo: int, d: int, h: int, mi: int) -> float:
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


NOW = local(2026, 9, 29, 15, 7)
DAY = 86400.0

# (时间, 程序, 上传, 下载, 出站, 节点, 远端)。各项合计互不相等，排序不会因并列而不稳定。
EVENTS = [
    (NOW - 50 * DAY, "chrome", 1, 1000, "proxy", "HK-01", "www.google.com:443"),  # A：超出 40 天保留
    (NOW - 35 * DAY, "chrome", 2, 2000, "direct", "", "www.baidu.com:443"),  # B
    (NOW - 20 * DAY, "Code", 3, 3000, "proxy", "JP-02", "api.github.com:443"),  # C
    (NOW - 3 * DAY, "steam", 4, 4000, "direct", "", "steamcdn-a.akamaihd.net:443"),  # D
    (local(2026, 9, 28, 23, 50), "chrome", 5, 5000, "proxy", "HK-01", "github.com:443"),  # E：昨天
    (local(2026, 9, 29, 13, 50), "Code", 6, 6000, "direct", "", "pypi.org:443"),  # F：13:45 时段
    (local(2026, 9, 29, 14, 2), "chrome", 7, 7000, "proxy", "JP-02", "www.google.com:443"),  # G：14:00 时段
    (local(2026, 9, 29, 15, 6), "Telegram", 11, 11000, "proxy", "SG-03", "t.me:443"),  # H
]


class Clock:
    def __init__(self, t: float) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def flow(
    cid: str,
    proc: str = "chrome",
    up: int = 0,
    down: int = 0,
    outbound: str = "proxy",
    node: str = "HK-01",
    remote: str = "www.google.com:443",
) -> dict[str, Any]:
    node = node if outbound == "proxy" else ""
    return {
        "id": cid,
        "process": proc,
        "upload": up,
        "download": down,
        "outbound": outbound,
        "node": node,
        "remote": remote,
        "path": outbound if outbound in ("proxy", "direct") else "tun",
        "inbound": "tun",
        "route": node or "DIRECT",
        "note": f"TUN · {node or 'DIRECT'}",
        "start": "2026-09-29T07:00:00.000Z",
    }


def new_factory(data_dir, clock, mono, *, retention=40, ignored=(), describe=None, on_icon=None):
    names = {str(n).lower() for n in ignored}
    return TrafficStore(
        Path(data_dir),
        retention_days=lambda: retention,
        ignored=lambda: set(names),
        describe=describe,
        on_icon=on_icon,
        clock=clock,
        monotonic=mono,
    )


def proc_rows(pack: dict[str, Any]) -> list[tuple]:
    return [
        (r["process"], r["upload"], r["download"], r["proxy_up"], r["proxy_down"], r["direct_up"], r["direct_down"])
        for r in pack["rows"]
    ]


def node_rows(pack: dict[str, Any]) -> list[tuple]:
    return [(r["node"], r["upload"], r["download"]) for r in pack["nodes"]["rows"]]


def site_rows(pack: dict[str, Any]) -> list[tuple]:
    return [(r["site"], r["upload"], r["download"], r["proxy_down"], r["direct_down"]) for r in pack["sites"]["rows"]]


def without_speed(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: without_speed(v) for k, v in obj.items() if k not in {"up_speed", "down_speed"}}
    if isinstance(obj, list):
        return [without_speed(v) for v in obj]
    return obj


class StoreTestBase(unittest.TestCase):
    factory = staticmethod(new_factory)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.clock = Clock(NOW)
        self.mono = Clock(10_000.0)
        self._stores: list[Any] = []
        self._n = 0

    def tearDown(self) -> None:
        for s in self._stores:
            close = getattr(s, "close", None)
            if callable(close):
                close()
        self._tmp.cleanup()

    def make(self, **kw: Any) -> Any:
        s = self.factory(self.dir, self.clock, self.mono, **kw)
        self._stores.append(s)
        return s

    def probe(self) -> TrafficDB:
        """另开一个连接直接看库里有什么；tearDown 时和 store 一起关掉。"""
        db = TrafficDB(self.dir / DB_NAME)
        self._stores.append(db)
        return db

    def persist(self, s: Any) -> None:
        flush = getattr(s, "flush", None)
        if callable(flush):
            flush()

    def tick(self, seconds: float = 1.0) -> None:
        self.clock.advance(seconds)
        self.mono.advance(seconds)

    def inject(self, s: Any, when: float, proc: str, up: int, down: int, outbound: str, node: str, remote: str) -> None:
        """在 when 时刻开一条连接、记满字节，再马上结束它。"""
        self.clock.t = when
        self.mono.advance(1.0)
        self._n += 1
        s.update([flow(f"x{self._n}", proc, up, down, outbound, node, remote)])
        s.update([])

    def seeded(self, **kw: Any) -> Any:
        s = self.make(**kw)
        self.clock.t = EVENTS[0][0] - 60
        s.update([])
        for ev in EVENTS:
            self.inject(s, *ev)
        self.clock.t = NOW
        return s

    def write_json(self, name: str, payload: Any) -> None:
        (self.dir / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def write_log(self, records: list[dict[str, Any]]) -> None:
        text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
        (self.dir / "connections.jsonl").write_text(text, encoding="utf-8")


class TrafficStoreBehaviourTest(StoreTestBase):
    def test_first_poll_primes_without_counting(self) -> None:
        s = self.make()
        s.update([flow("a", up=500, down=9000)])
        self.assertEqual(s.program_rows(), [])
        self.tick()
        s.update([flow("a", up=600, down=9500)])
        rows = s.program_rows()
        self.assertEqual(
            [(r["process"], r["upload"], r["download"], r["proxy_up"], r["proxy_down"]) for r in rows],
            [("chrome", 100, 500, 100, 500)],
        )

    def test_new_connection_after_prime_counts_all_bytes(self) -> None:
        s = self.make()
        s.update([])
        s.update([flow("b", up=50, down=700, outbound="direct")])
        row = s.program_rows()[0]
        self.assertEqual((row["upload"], row["download"], row["direct_up"], row["direct_down"]), (50, 700, 50, 700))
        self.assertEqual((row["proxy_up"], row["proxy_down"]), (0, 0))

    def test_counter_going_backwards_is_clamped(self) -> None:
        s = self.make()
        s.update([flow("a", up=600, down=9500)])
        s.update([flow("a", up=100, down=100)])
        self.assertEqual(s.program_rows(), [])
        s.update([flow("a", up=200, down=300)])
        row = s.program_rows()[0]
        self.assertEqual((row["upload"], row["download"]), (100, 200))

    def test_clash_core_is_not_a_program(self) -> None:
        s = self.make()
        s.update([])
        s.update([flow("k", "FlClashCore", 1000, 1000), flow("m", "mihomo", 5, 5, "direct")])
        self.assertEqual(s.program_rows(), [])
        today = s.ranking("today")
        self.assertEqual((today["rows"], today["nodes"]["rows"]), ([], []))
        self.assertEqual(s.live_view()["eaters"], [])

    def test_closed_connection_is_counted_and_logged(self) -> None:
        s = self.make()
        s.update([])
        s.update([flow("a", up=10, down=20)])
        self.clock.advance(5)
        s.update([])
        closed = s.recent_closed()
        self.assertEqual(len(closed), 1)
        want = {
            "id": "a",
            "process": "chrome",
            "remote": "www.google.com:443",
            "inbound": "tun",
            "outbound": "proxy",
            "node": "HK-01",
            "route": "HK-01",
            "note": "TUN · HK-01",
            "upload": 10,
            "download": 20,
            "start": "2026-09-29T07:00:00.000Z",
            "end": int(NOW + 5),
            "closed": True,
            "path": "proxy",
        }
        self.assertEqual(closed[0], want)
        row = s.program_rows()[0]
        self.assertEqual(row["conns"], 1)
        self.assertEqual(row["sites"][0]["conns"], 1)
        lines = (self.dir / "connections.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(x) for x in lines], [want])
        self.assertEqual(s.logged, 1)
        self.persist(s)
        self.assertEqual(self.make().logged, 1)

    def test_split_last_node_and_sites(self) -> None:
        s = self.make()
        s.update([])
        s.update(
            [
                flow("a", "chrome", 100, 1000, "proxy", "HK-01", "www.google.com:443"),
                flow("b", "chrome", 10, 50, "direct", "", "www.baidu.com:443"),
                flow("c", "chrome", 1, 2, "proxy", "JP-02", "mail.google.com:443"),
            ]
        )
        row = s.program_rows()[0]
        self.assertEqual(
            (row["upload"], row["download"], row["proxy_up"], row["proxy_down"], row["direct_up"], row["direct_down"]),
            (111, 1052, 101, 1002, 10, 50),
        )
        self.assertEqual(row["last_node"], "JP-02")
        sites = [(x["site"], x["upload"], x["download"], x["proxy_down"], x["direct_down"]) for x in row["sites"]]
        self.assertEqual(sites, [("google.com", 101, 1002, 1002, 0), ("baidu.com", 10, 50, 0, 50)])

    def test_unknown_outbound_is_explicit_in_periods_and_lifetime(self) -> None:
        s = self.make()
        s.update([])
        s.update([flow("u", "curl", 30, 300, "unknown", "", "example.com:80")])
        row = s.program_rows()[0]
        self.assertEqual((row["upload"], row["download"], row["proxy_down"], row["direct_down"]), (30, 300, 0, 0))
        self.assertEqual(proc_rows(s.ranking("today")), [("curl", 30, 300, 0, 0, 0, 0)])
        self.assertEqual(proc_rows(s.ranking("all")), [("curl", 30, 300, 0, 0, 0, 0)])
        for period in ('today', 'all'):
            self.assertEqual(s.ranking(period)['rows'][0]['unknown_down'], 300)
        lanes = s.lanes()
        self.assertEqual((lanes["proxy"]["rows"], lanes["direct"]["rows"]), ([], []))

    def test_period_windows(self) -> None:
        s = self.seeded()
        self.persist(s)
        self.assertEqual(
            proc_rows(s.ranking("1h")),
            [("Telegram", 11, 11000, 11, 11000, 0, 0), ("chrome", 7, 7000, 7, 7000, 0, 0)],
        )
        self.assertEqual(
            proc_rows(s.ranking("today")),
            [("Telegram", 11, 11000, 11, 11000, 0, 0), ("chrome", 7, 7000, 7, 7000, 0, 0), ("Code", 6, 6000, 0, 0, 6, 6000)],
        )
        self.assertEqual(
            proc_rows(s.ranking("7d")),
            [
                ("chrome", 12, 12000, 12, 12000, 0, 0),
                ("Telegram", 11, 11000, 11, 11000, 0, 0),
                ("Code", 6, 6000, 0, 0, 6, 6000),
                ("steam", 4, 4000, 0, 0, 4, 4000),
            ],
        )
        self.assertEqual(
            proc_rows(s.ranking("30d")),
            [
                ("chrome", 12, 12000, 12, 12000, 0, 0),
                ("Telegram", 11, 11000, 11, 11000, 0, 0),
                ("Code", 9, 9000, 3, 3000, 6, 6000),
                ("steam", 4, 4000, 0, 0, 4, 4000),
            ],
        )
        everything = s.ranking("all")
        self.assertEqual(
            proc_rows(everything),
            [
                ("chrome", 15, 15000, 13, 13000, 2, 2000),
                ("Telegram", 11, 11000, 11, 11000, 0, 0),
                ("Code", 9, 9000, 3, 3000, 6, 6000),
                ("steam", 4, 4000, 0, 0, 4, 4000),
            ],
        )
        self.assertEqual(
            (
                everything["upload"],
                everything["download"],
                everything["proxy_upload"],
                everything["proxy_download"],
                everything["direct_upload"],
                everything["direct_download"],
                everything["since"],
            ),
            (39, 39000, 27, 27000, 12, 12000, 0),
        )
        self.assertEqual(s.ranking("today")["since"], int(local(2026, 9, 29, 0, 0)))
        self.assertEqual(s.ranking("1h")["since"], int(NOW - 3600))

    def test_node_and_site_rankings(self) -> None:
        s = self.seeded()
        self.persist(s)
        self.assertEqual(node_rows(s.ranking("today")), [("SG-03", 11, 11000), ("JP-02", 7, 7000)])
        self.assertEqual(node_rows(s.ranking("7d")), [("SG-03", 11, 11000), ("JP-02", 7, 7000), ("HK-01", 5, 5000)])
        self.assertEqual(node_rows(s.ranking("30d")), [("SG-03", 11, 11000), ("JP-02", 10, 10000), ("HK-01", 5, 5000)])
        # A 的时段已按 40 天清掉，所以累计节点里 HK-01 只剩 E
        self.assertEqual(node_rows(s.ranking("all")), [("SG-03", 11, 11000), ("JP-02", 10, 10000), ("HK-01", 5, 5000)])
        self.assertEqual(
            site_rows(s.ranking("today")),
            [("t.me", 11, 11000, 11000, 0), ("google.com", 7, 7000, 7000, 0), ("pypi.org", 6, 6000, 0, 6000)],
        )
        self.assertEqual(
            site_rows(s.ranking("all")),
            [
                ("t.me", 11, 11000, 11000, 0),
                ("github.com", 8, 8000, 8000, 0),
                ("google.com", 7, 7000, 7000, 0),
                ("pypi.org", 6, 6000, 0, 6000),
                ("akamaihd.net", 4, 4000, 0, 4000),
                ("baidu.com", 2, 2000, 0, 2000),
            ],
        )
        nodes = s.ranking("30d")["nodes"]
        self.assertEqual((nodes["upload"], nodes["download"]), (26, 26000))
        self.assertEqual([r["rank"] for r in nodes["rows"]], [1, 2, 3])
        self.assertEqual(nodes["rows"][0]["share"], round(100.0 * 11011 / 26026, 1))
        site = s.ranking("today")["sites"]["rows"][0]
        self.assertEqual(
            set(site),
            {"site", "upload", "download", "proxy_up", "proxy_down", "direct_up", "direct_down", "proxy_total", "direct_total", "unknown_up", "unknown_down", "rejected_up", "rejected_down", "rank", "share"},
        )

    def test_unknown_period_means_last_hour(self) -> None:
        s = self.seeded()
        self.assertEqual(s.ranking("bogus"), s.ranking("1h"))

    def test_rank_row_details(self) -> None:
        s = self.seeded(describe=lambda path: "Google Chrome" if path.endswith("chrome.exe") else "")
        s.remember_origin("chrome", "chrome.exe   --type=renderer", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
        s.remember_origin("FlClashCore", "core", r"C:\x\FlClashCore.exe")
        s.remember_origin("nobody", "x", r"C:\x\nobody.exe")
        pack = s.ranking("all")
        chrome = pack["rows"][0]
        self.assertEqual(chrome["process"], "chrome")
        self.assertEqual(chrome["cmd"], "chrome.exe --type=renderer")
        self.assertEqual(chrome["path"], r"C:\Program Files\Google\Chrome\Application\chrome.exe")
        self.assertEqual(chrome["title"], "Google Chrome")
        self.assertEqual([r["rank"] for r in pack["rows"]], [1, 2, 3, 4])
        self.assertEqual(chrome["share"], round(100.0 * 15015 / 39039, 1))
        self.assertEqual(chrome["proxy_share"], round(100.0 * 13013 / 27027, 1))
        self.assertEqual(chrome["proxy_total"], 13013)
        self.assertEqual([x["site"] for x in chrome["sites"]], ["google.com", "github.com", "baidu.com"])
        self.assertEqual({r["process"] for r in s.program_rows()}, {"chrome", "Code", "steam", "Telegram"})
        self.assertEqual(s.program_rows()[0]["title"], "Google Chrome")

    def test_site_list_is_capped_at_40(self) -> None:
        s = self.make()
        s.update([])
        for i in range(1, 46):
            s.update([flow(f"s{i}", "chrome", i, 100 * i, "proxy", "HK-01", f"www.site{i}.com:443")])
        sites = s.program_rows()[0]["sites"]
        self.assertEqual({x["site"] for x in sites}, {f"site{i}.com" for i in range(6, 46)})
        top = s.ranking("all")["rows"][0]["sites"]
        self.assertEqual([x["site"] for x in top], [f"site{i}.com" for i in range(45, 33, -1)])

    def test_lanes(self) -> None:
        s = self.make()
        s.update([])
        s.update(
            [
                flow("a", "chrome", 100, 1000, "proxy", "HK-01", "www.google.com:443"),
                flow("b", "chrome", 10, 50, "direct", "", "www.baidu.com:443"),
                flow("c", "Code", 5, 5, "direct", "", "pypi.org:443"),
            ]
        )
        lanes = s.lanes()
        proxy = lanes["proxy"]
        self.assertEqual((proxy["upload"], proxy["download"]), (100, 1000))
        self.assertEqual(
            [(r["process"], r["upload"], r["download"], r["node"]) for r in proxy["rows"]],
            [("chrome", 100, 1000, "HK-01")],
        )
        self.assertEqual([(x["site"], x["upload"], x["download"]) for x in proxy["rows"][0]["sites"]], [("google.com", 100, 1000)])
        direct = lanes["direct"]
        self.assertEqual((direct["upload"], direct["download"]), (15, 55))
        self.assertEqual([(r["process"], r["upload"], r["download"]) for r in direct["rows"]], [("chrome", 10, 50), ("Code", 5, 5)])
        self.assertEqual([x["site"] for x in direct["rows"][0]["sites"]], ["baidu.com"])
        self.assertEqual(direct["rows"][0]["conns"], 0)

    def test_new_program_notices(self) -> None:
        s = self.make(ignored={"Steam"})
        s.update([])
        s.update([flow("a", "chrome", 1, 1), flow("b", "steam", 1, 1)])
        self.assertEqual([x["process"] for x in s.recent_new()], ["chrome"])
        self.assertEqual(s.recent_new()[0]["ts"], int(NOW))
        s.ack_new("chrome")
        self.assertEqual(s.recent_new(), [])
        s.update([flow("a", "chrome", 2, 2), flow("b", "steam", 2, 2), flow("c", "Code", 1, 1)])
        self.assertEqual([x["process"] for x in s.recent_new()], ["Code"])
        s.ack_new()
        self.assertEqual(s.recent_new(), [])
        self.persist(s)
        s2 = self.make(ignored={"Steam"})
        s2.update([])
        s2.update([flow("d", "chrome", 1, 1), flow("e", "Telegram", 1, 1), flow("f", "steam", 1, 1)])
        self.assertEqual([x["process"] for x in s2.recent_new()], ["Telegram"])

    def test_reset_clears_totals_but_keeps_log(self) -> None:
        s = self.seeded()
        for _ in range(3):
            s.record_history()
        s.reset()
        self.assertEqual(s.program_rows(), [])
        for period in store_mod.PERIODS:
            pack = s.ranking(period)
            self.assertEqual((pack["rows"], pack["nodes"]["rows"], pack["sites"]["rows"]), ([], [], []), period)
        self.assertEqual((s.recent_closed(), s.history_rows(), s.recent_new()), ([], [], []))
        self.assertEqual(len((self.dir / "connections.jsonl").read_text(encoding="utf-8").splitlines()), len(EVENTS))
        s.update([])
        s.update([flow("z", "chrome", 3, 30)])
        self.assertEqual(proc_rows(s.ranking("today")), [("chrome", 3, 30, 3, 30, 0, 0)])

    def test_persistence_roundtrip(self) -> None:
        s = self.seeded()
        self.persist(s)
        s2 = self.make()
        for period in store_mod.PERIODS:
            self.assertEqual(without_speed(s2.ranking(period)), without_speed(s.ranking(period)), period)
        self.assertEqual(without_speed(s2.lanes()), without_speed(s.lanes()))
        self.assertEqual(s2.logged, len(EVENTS))

    def test_retention_applies_on_load(self) -> None:
        s = self.seeded()
        self.persist(s)
        s7 = self.make(retention=7)
        self.assertEqual(
            proc_rows(s7.ranking("30d")),
            [
                ("chrome", 12, 12000, 12, 12000, 0, 0),
                ("Telegram", 11, 11000, 11, 11000, 0, 0),
                ("Code", 6, 6000, 0, 0, 6, 6000),
                ("steam", 4, 4000, 0, 0, 4, 4000),
            ],
        )
        self.assertEqual(len(s7.ranking("all")["rows"]), 4)
        self.assertEqual(s7.ranking("all")["rows"][0]["upload"], 15)

    def test_legacy_programs_without_sites_are_hydrated_from_log(self) -> None:
        t1 = local(2026, 9, 29, 10, 0)
        t2 = local(2026, 9, 29, 11, 0)
        self.write_json(
            "programs.json",
            {
                "programs": {
                    "chrome": {
                        "upload": 30,
                        "download": 3000,
                        "proxy_up": 10,
                        "proxy_down": 1000,
                        "direct_up": 20,
                        "direct_down": 2000,
                        "conns": 2,
                        "last": int(t2),
                        "last_node": "HK-01",
                        "last_cmd": "",
                        "last_path": "",
                    }
                }
            },
        )
        self.write_json(
            "slots.json",
            {
                "slots": {
                    store_mod.slot_key(t1): {"chrome": {"proxy_up": 10, "proxy_down": 1000, "direct_up": 0, "direct_down": 0}},
                    store_mod.slot_key(t2): {"chrome": {"proxy_up": 0, "proxy_down": 0, "direct_up": 20, "direct_down": 2000}},
                }
            },
        )
        self.write_log(
            [
                {"process": "chrome", "remote": "github.com:443", "outbound": "proxy", "node": "HK-01", "upload": 10, "download": 1000, "end": int(t1)},
                {"process": "chrome", "remote": "www.baidu.com:443", "outbound": "direct", "node": "", "upload": 20, "download": 2000, "end": int(t2)},
                {"process": "Code", "remote": "pypi.org:443", "outbound": "direct", "node": "", "upload": 5, "download": 50, "end": int(t2)},
            ]
        )
        s = self.make()
        rows = s.program_rows()
        self.assertEqual([r["process"] for r in rows], ["chrome"])
        sites = [(x["site"], x["upload"], x["download"], x["proxy_down"], x["direct_down"], x["conns"]) for x in rows[0]["sites"]]
        self.assertEqual(sites, [("baidu.com", 20, 2000, 0, 2000, 1), ("github.com", 10, 1000, 1000, 0, 1)])
        today = s.ranking("today")
        self.assertEqual(proc_rows(today), [("chrome", 30, 3000, 10, 1000, 20, 2000)])
        self.assertEqual(node_rows(today), [("HK-01", 10, 1000)])
        self.assertEqual(
            site_rows(today),
            [("baidu.com", 20, 2000, 0, 2000), ("github.com", 10, 1000, 1000, 0), ("pypi.org", 5, 50, 0, 50)],
        )
        self.assertEqual(s.logged, 3)

    def test_missing_slots_file_is_rebuilt_from_log(self) -> None:
        old = NOW - 10 * DAY
        today = local(2026, 9, 29, 9, 30)
        self.write_json(
            "programs.json",
            {
                "programs": {
                    "chrome": {
                        "upload": 999,
                        "download": 99999,
                        "proxy_up": 999,
                        "proxy_down": 99999,
                        "direct_up": 0,
                        "direct_down": 0,
                        "conns": 9,
                        "last": 1,
                        "sites": {"google.com": {"upload": 999, "download": 99999, "proxy_up": 999, "proxy_down": 99999}},
                    }
                }
            },
        )
        self.write_log(
            [
                {"process": "chrome", "remote": "www.google.com:443", "outbound": "proxy", "node": "HK-01", "upload": 1, "download": 100, "end": int(old)},
                {"process": "chrome", "remote": "www.google.com:443", "outbound": "proxy", "node": "JP-02", "upload": 2, "download": 200, "end": int(today)},
                {"process": "curl", "remote": "example.com:80", "outbound": "direct", "node": "", "upload": 3, "download": 300, "start": "2000-01-01T00:00:00Z"},
            ]
        )
        s = self.make()
        self.assertEqual(proc_rows(s.ranking("today")), [("chrome", 2, 200, 2, 200, 0, 0)])
        self.assertEqual(proc_rows(s.ranking("30d")), [("chrome", 3, 300, 3, 300, 0, 0)])
        self.assertEqual(node_rows(s.ranking("30d")), [("JP-02", 2, 200), ("HK-01", 1, 100)])
        self.assertEqual(proc_rows(s.ranking("all")), [("chrome", 999, 99999, 999, 99999, 0, 0)])

    def test_old_slots_file_without_nodes_and_sites(self) -> None:
        t = local(2026, 9, 29, 12, 0)
        self.write_json("programs.json", {"programs": {}})
        self.write_json(
            "slots.json",
            {"slots": {store_mod.slot_key(t): {"chrome": {"proxy_up": 7, "proxy_down": 70, "direct_up": 0, "direct_down": 0}}}},
        )
        self.write_log(
            [{"process": "chrome", "remote": "t.me:443", "outbound": "proxy", "node": "SG-03", "upload": 7, "download": 70, "end": int(t)}]
        )
        today = self.make().ranking("today")
        self.assertEqual(proc_rows(today), [("chrome", 7, 70, 7, 70, 0, 0)])
        self.assertEqual(node_rows(today), [("SG-03", 7, 70)])
        self.assertEqual(site_rows(today), [("t.me", 7, 70, 70, 0)])

    def test_speed_is_smoothed(self) -> None:
        s = self.make()
        self.tick()
        s.update([flow("a", "chrome", 10, 1000)])
        self.tick()
        s.update([flow("a", "chrome", 110, 3000), flow("b", "steam", 0, 0, "direct")])
        alpha = 1.0 - math.exp(-1.0 / 1.2)
        r = s.rates()
        self.assertAlmostEqual(r["up"], alpha * 100, places=6)
        self.assertAlmostEqual(r["down"], alpha * 2000, places=6)
        self.assertAlmostEqual(r["proxy_up"], alpha * 100, places=6)
        self.assertAlmostEqual(r["proxy_down"], alpha * 2000, places=6)
        self.assertAlmostEqual(r["direct_down"], 0.0, places=6)
        row = s.ranking("today")["rows"][0]
        self.assertAlmostEqual(row["down_speed"], alpha * 2000, places=6)
        self.assertAlmostEqual(s.lanes()["proxy"]["rows"][0]["down_speed"], alpha * 2000, places=6)
        s.decay_speed(0.5)
        self.assertAlmostEqual(s.rates()["down"], alpha * 1000, places=6)

    def test_live_view(self) -> None:
        s = self.make(ignored={"steam"})
        self.tick()
        s.update([])
        self.tick()
        s.update(
            [
                flow("a", "chrome", 100, 1000),
                flow("b", "steam", 5, 5, "direct"),
                flow("c", "Code", 0, 0, "direct"),
                flow("k", "FlClashCore", 7, 7, "direct"),
            ]
        )
        alpha = 1.0 - math.exp(-1.0 / 1.2)
        live = s.live_view()
        self.assertEqual([(e["process"], e["conns"]) for e in live["eaters"]], [("chrome", 1), ("Code", 1)])
        chrome = live["eaters"][0]
        self.assertAlmostEqual(chrome["down"], alpha * 1000, places=6)
        self.assertAlmostEqual(chrome["proxy_down"], alpha * 1000, places=6)
        self.assertAlmostEqual(chrome["up"], alpha * 100, places=6)
        self.assertEqual(live["eaters"][1]["down"], 0.0)
        # 总速率含被忽略的程序和 Clash 内核自己的连接
        self.assertAlmostEqual(live["down"], alpha * 1012, places=6)
        self.assertAlmostEqual(live["direct_down"], alpha * 12, places=6)

    def test_rates_prefer_wire_then_totals(self) -> None:
        s = self.make()
        s.set_wire_speed(111, 222)
        r = s.rates()
        self.assertEqual((r["up"], r["down"]), (111.0, 222.0))
        self.mono.advance(3.0)
        self.assertEqual(s.rates()["up"], 0.0)
        s.note_totals(1000, 5000)
        self.mono.advance(1.0)
        s.note_totals(2000, 9000)
        a = 1.0 - math.exp(-1.0)
        r = s.rates()
        self.assertAlmostEqual(r["up"], a * 1000, places=6)
        self.assertAlmostEqual(r["down"], a * 4000, places=6)
        self.mono.advance(1.0)
        s.note_totals(10, 10)
        self.assertAlmostEqual(s.rates()["up"], (1 - a) * a * 1000, places=6)

    def test_closed_list_and_history_are_trimmed(self) -> None:
        s = self.make()
        s.update([])
        s.update([flow(f"c{i}", "chrome", 1, 1) for i in range(401)])
        s.update([])
        self.assertEqual(len(s.recent_closed(limit=1000)), 250)
        self.assertEqual(len(s.recent_closed()), 120)
        self.assertEqual(s.logged, 401)
        self.assertEqual(s.program_rows()[0]["conns"], 401)
        for _ in range(250):
            self.clock.advance(2)
            s.record_history()
        hist = s.history_rows()
        self.assertEqual(len(hist), 240)
        self.assertEqual(hist[-1]["t"], self.clock.t)
        self.assertEqual(set(hist[0]), {"t", "up", "down", "proxy_up", "proxy_down", "direct_up", "direct_down", "unknown_up", "unknown_down", "rejected_up", "rejected_down"})


class TrafficStoreFixesTest(StoreTestBase):
    def test_nothing_is_written_until_flush(self) -> None:
        s = self.make()
        self.assertFalse(s.flush())
        s.update([])
        s.update([flow("a", "chrome", 5, 5)])
        db = self.probe()
        self.assertEqual((db.load_programs(), db.count_slots(), db.load_seen()), ({}, 0, set()))
        self.assertTrue(s.flush())
        self.assertFalse(s.flush())
        self.assertEqual(db.load_programs()["chrome"]["upload"], 5)
        self.assertEqual(db.load_seen(), {"chrome"})
        self.assertEqual(db.slot_totals("proc"), {"chrome": (5, 5, 0, 0)})
        self.assertEqual(db.slot_totals("node"), {"HK-01": (5, 5, 0, 0)})
        self.assertEqual(db.slot_totals("site"), {"google.com": (5, 5, 0, 0)})
        self.assertFalse((self.dir / "programs.json").exists(), "不再写 JSON")

    def test_failed_flush_keeps_changes_without_double_counting(self) -> None:
        s = self.make()
        s.update([])
        s.update([flow("a", "chrome", 5, 5)])
        with mock.patch.object(s.db, "write", side_effect=sqlite3.OperationalError("disk I/O error")):
            self.assertFalse(s.flush())
        self.assertEqual(proc_rows(s.ranking("today")), [("chrome", 5, 5, 5, 5, 0, 0)])
        s.update([flow("a", "chrome", 7, 9)])
        self.assertTrue(s.flush())
        s2 = self.make()
        self.assertEqual(proc_rows(s2.ranking("today")), [("chrome", 7, 9, 7, 9, 0, 0)])
        self.assertEqual(node_rows(s2.ranking("today")), [("HK-01", 7, 9)])
        self.assertEqual(s2.program_rows()[0]["upload"], 7)

    def test_rankings_are_exact_while_a_flush_is_writing(self) -> None:
        s = self.make()
        s.update([])
        s.update([flow("a", "chrome", 5, 500), flow("b", "Code", 1, 100, "direct")])
        real_write = s.db.write
        started = threading.Event()

        def slow_write(**kw: Any) -> None:
            started.set()
            time.sleep(0.3)
            real_write(**kw)

        want = [("chrome", 5, 500, 5, 500, 0, 0), ("Code", 1, 100, 0, 0, 1, 100)]
        with mock.patch.object(s.db, "write", side_effect=slow_write):
            t = threading.Thread(target=s.flush)
            t.start()
            self.assertTrue(started.wait(5))
            seen = []
            while t.is_alive():
                seen.append(proc_rows(s.ranking("today")))
                s.update([flow("a", "chrome", 5, 500), flow("b", "Code", 1, 100, "direct")])
            t.join()
        seen.append(proc_rows(s.ranking("today")))
        self.assertTrue(all(rows == want for rows in seen), seen)

    def test_reset_survives_restart(self) -> None:
        s = self.seeded()
        s.reset()
        s2 = self.make()
        for period in store_mod.PERIODS:
            pack = s2.ranking(period)
            self.assertEqual((pack["rows"], pack["nodes"]["rows"], pack["sites"]["rows"]), ([], [], []), period)
        self.assertEqual(s2.program_rows(), [])
        self.assertEqual(s2.logged, len(EVENTS))

    def test_failed_reset_hides_old_data_and_is_retried(self) -> None:
        s = self.seeded()
        s.flush()
        with mock.patch.object(s.db, "write", side_effect=sqlite3.OperationalError("disk I/O error")):
            s.reset()
            s.update([])
            s.update([flow("z", "chrome", 3, 30)])
            self.assertEqual(proc_rows(s.ranking("all")), [("chrome", 3, 30, 3, 30, 0, 0)])
            self.assertEqual(node_rows(s.ranking("30d")), [("HK-01", 3, 30)])
        self.assertTrue(s.flush())
        s2 = self.make()
        self.assertEqual(proc_rows(s2.ranking("30d")), [("chrome", 3, 30, 3, 30, 0, 0)])
        self.assertEqual(node_rows(s2.ranking("all")), [("HK-01", 3, 30)])

    def test_expired_slots_are_hidden_then_deleted(self) -> None:
        s = self.make(retention=7)
        s.update([])
        self.inject(s, NOW - 3 * DAY, "chrome", 1, 100, "proxy", "HK-01", "t.me:443")
        self.clock.t = NOW
        s.flush()
        db = self.probe()
        self.assertEqual(db.count_slots(), 3)
        self.clock.t = NOW + 5 * DAY
        self.assertEqual(s.ranking("30d")["rows"], [], "过了保留期立即不算")
        self.assertEqual(s.ranking("30d")["nodes"]["rows"], [])
        self.assertEqual(proc_rows(s.ranking("all")), [("chrome", 1, 100, 1, 100, 0, 0)], "累计不受保留期影响")
        self.assertTrue(s.flush())
        self.assertEqual(db.count_slots(), 0)

    def test_programs_rebuilt_from_log_keep_event_times(self) -> None:
        self.write_log(
            [
                {"process": "chrome", "remote": "www.google.com:443", "outbound": "proxy", "node": "HK-01", "upload": 1, "download": 100, "end": int(NOW - 10 * DAY)},
                {"process": "chrome", "remote": "www.google.com:443", "outbound": "proxy", "node": "HK-01", "upload": 2, "download": 200, "end": int(NOW - 2 * 3600)},
            ]
        )
        s = self.make()
        self.assertEqual(s.ranking("1h")["rows"], [])
        self.assertEqual(proc_rows(s.ranking("today")), [("chrome", 2, 200, 2, 200, 0, 0)])
        self.assertEqual(proc_rows(s.ranking("all")), [("chrome", 3, 300, 3, 300, 0, 0)])
        self.assertEqual(s.program_rows()[0]["conns"], 2)

    def test_bad_log_records_are_skipped(self) -> None:
        good = {"process": "chrome", "remote": "t.me:443", "outbound": "proxy", "node": "SG-03", "upload": 2, "download": 20, "end": int(NOW - 60)}
        lines = [
            json.dumps(good),
            "{not json",
            json.dumps({"process": "x", "upload": 1, "download": 1, "start": "0001-01-01T00:00:00Z"}),
            json.dumps({"process": "y", "upload": 1, "download": 1, "end": 1e30}),
            json.dumps([1, 2, 3]),
            json.dumps(good),
        ]
        (self.dir / "connections.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        s = self.make()
        self.assertEqual(proc_rows(s.ranking("1h"))[0], ("chrome", 4, 40, 4, 40, 0, 0))
        self.assertEqual(s.logged, 6)

    def test_title_survives_restart(self) -> None:
        s = self.seeded(describe=lambda path: "Telegram Desktop")
        s.remember_origin("Telegram", "", r"C:\T\Telegram.exe")
        s.flush()
        self.assertEqual(self.make().ranking("all")["rows"][1]["title"], "Telegram Desktop")

    def test_origin_is_persisted_without_new_traffic(self) -> None:
        # 0.8.0 只在之后又有流量时才顺带写盘
        s = self.seeded()
        s.flush()
        s.remember_origin("steam", "steam.exe   -silent", r"C:\Steam\steam.exe")
        self.assertTrue(s.flush())
        row = next(r for r in self.make().ranking("all")["rows"] if r["process"] == "steam")
        self.assertEqual((row["cmd"], row["path"]), ("steam.exe -silent", r"C:\Steam\steam.exe"))
        s.remember_origin("steam", "steam.exe -silent", r"C:\Steam\steam.exe")
        self.assertFalse(s.flush(), "没有变化就不写盘")

    def test_describe_is_cached_and_icons_requested(self) -> None:
        calls: list[str] = []
        icons: list[tuple[str, str]] = []

        def describe(path: str) -> str:
            calls.append(path)
            return "Chrome"

        s = self.make(describe=describe, on_icon=lambda n, p: icons.append((n, p)))
        s.update([])
        s.update([flow("a", "chrome", 1, 1)])
        for _ in range(5):
            s.remember_origin("chrome", "", r"C:\c\chrome.exe")
        self.assertEqual(calls, [r"C:\c\chrome.exe"])
        self.assertIn(("chrome", r"C:\c\chrome.exe"), icons)
        icons.clear()
        s.live_view()
        self.assertEqual(icons, [("chrome", r"C:\c\chrome.exe")])

    def test_log_rotates(self) -> None:
        with mock.patch.object(store_mod, "LOG_ROTATE_BYTES", 300):
            s = self.make()
            s.update([])
            for i in range(6):
                s.update([flow(f"r{i}", "chrome", 1, 1)])
                s.update([])
        self.assertTrue((self.dir / "connections.jsonl.old").is_file())
        self.assertTrue((self.dir / "connections.jsonl").is_file())
        self.assertEqual(s.logged, 6)

    def test_concurrent_update_query_and_flush(self) -> None:
        s = self.make()
        s.update([])
        errors: list[BaseException] = []
        stop = threading.Event()

        def guard(fn):
            def run() -> None:
                try:
                    fn()
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            return run

        def updater() -> None:
            for i in range(300):
                s.update([flow(f"u{i % 50}", f"p{i % 7}", i, i * 10, "proxy" if i % 2 else "direct", "HK-01", f"h{i % 9}.com:443")])
            stop.set()

        def reader() -> None:
            while not stop.is_set():
                for period in store_mod.PERIODS:
                    s.ranking(period)
                s.lanes()
                s.live_view()

        def flusher() -> None:
            while not stop.is_set():
                s.flush()

        threads = [threading.Thread(target=guard(f)) for f in (updater, reader, reader, flusher)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertFalse(any(t.is_alive() for t in threads), "线程没有结束（可能死锁）")
        self.assertEqual(errors, [])
        s.flush()
        self.assertEqual(without_speed(self.make().ranking("all")), without_speed(s.ranking("all")))

    def test_close_checkpoints_and_is_idempotent(self) -> None:
        s = self.seeded()
        s.close()
        s.close()
        self.assertFalse((self.dir / (DB_NAME + "-wal")).exists(), "正常关闭后 WAL 合回主文件")
        self.assertFalse(s.flush(), "关了以后 flush 什么都不做")
        with self.assertRaises(RuntimeError):
            s.update([flow("late", "chrome", 1, 1)])  # 协调器先等待线程；关闭后拒绝写入
        self.assertEqual(s.ranking("1h")["rows"], [], "关闭后不读数据库，也不接收新的时段增量")
        self.assertTrue(s.ranking("all")["rows"], "已保存的累计内存仍可读")
        again = self.make()
        self.assertEqual(len(again.ranking("all")["rows"]), 4)

    def test_time_helpers(self) -> None:
        t = local(2026, 9, 29, 14, 44)
        self.assertEqual(store_mod.slot_key(t), "2026-09-29T14:30")
        self.assertEqual(store_mod.slot_ts("2026-09-29T14:30"), local(2026, 9, 29, 14, 30))
        self.assertEqual(store_mod.event_ts({"end": 1_800_000_000}), 1_800_000_000.0)
        self.assertEqual(store_mod.event_ts({"start": "2026-09-29T00:00:00Z"}), 1790640000.0)
        self.assertEqual(store_mod.event_ts({"start": "bad"}, 5.0), 5.0)
        self.assertEqual(store_mod.event_ts({"start": "0001-01-01T00:00:00Z"}, 5.0), 5.0)
        self.assertEqual(store_mod.event_ts({"end": 1e30}, 5.0), 5.0)
        self.assertEqual(store_mod.period_since("x", NOW), (NOW - 3600, "1h"))

    def test_slot_start_matches_legacy_slot_keys(self) -> None:
        rng = random.Random(5)
        stamps = [NOW + rng.uniform(-400 * DAY, 400 * DAY) for _ in range(3000)]
        stamps += [local(2026, 9, 29, 14, 45) + d for d in (-1, 0, 0.5, 899.9, 900)]
        for ts in stamps:
            self.assertEqual(store_mod.slot_start(ts), int(store_mod.slot_ts(store_mod.slot_key(ts))), ts)


def oracle_rankings(data_dir: Path, period: str, now: float, retention: int = 40) -> dict[str, list[tuple]]:
    """直接按 0.8.0 的口径从 programs.json / slots.json 算排行（和存储实现无关的对照）。"""
    programs = json.loads((data_dir / "programs.json").read_text(encoding="utf-8"))["programs"]
    slots = json.loads((data_dir / "slots.json").read_text(encoding="utf-8"))
    cutoff = now - retention * 86400
    since, period = store_mod.period_since(period, now)

    def visible(key: str) -> bool:
        try:
            start = time.mktime(time.strptime(key, "%Y-%m-%dT%H:%M"))
        except ValueError:
            return False
        return start >= cutoff and (period == "all" or start + 900 > since)

    def total(field: str, keys: tuple[str, ...]) -> dict[str, list[int]]:
        out: dict[str, list[int]] = {}
        for key, recs in slots[field].items():
            if not visible(key):
                continue
            for name, rec in recs.items():
                row = out.setdefault(name, [0] * len(keys))
                for i, k in enumerate(keys):
                    row[i] += int(rec.get(k) or 0)
        return out

    split = ("proxy_up", "proxy_down", "direct_up", "direct_down")
    procs = total("slots", split)
    if period == "all":
        merged = {n: [int(r.get(k) or 0) for k in split] for n, r in programs.items()}
        for n, v in procs.items():
            merged.setdefault(n, v)
        procs = merged
    rows = sorted((n, v[0] + v[2], v[1] + v[3], *v) for n, v in procs.items() if any(v))
    nodes = sorted((n, *v) for n, v in total("nodes", ("up", "down")).items() if any(v))
    sites = sorted((n, v[0] + v[2], v[1] + v[3], v[1], v[3]) for n, v in total("sites", split).items() if any(v))
    return {"rows": rows, "nodes": nodes, "sites": sites}


class MigrationTest(StoreTestBase):
    def synth_legacy(self, seed: int = 11) -> None:
        """造一份 0.8.0 格式的数据：60 天、若干程序 / 节点 / 网站，外加几条坏键。"""
        rng = random.Random(seed)
        procs = ["chrome", "Code", "steam", "Telegram", "python", "Weixin", "curl"]
        nodes = ["HK-01", "JP-02", "SG-03"]
        sites = ["google.com", "github.com", "t.me", "pypi.org", "baidu.com", "akamaihd.net"]
        slots: dict[str, dict] = {"slots": {}, "nodes": {}, "sites": {}}
        first = store_mod.slot_start(NOW - 60 * DAY)
        for slot in range(first, int(NOW), 900):
            if rng.random() < 0.7:
                continue
            key = store_mod.slot_key(slot)
            for field, names, keys in (
                ("slots", procs, ("proxy_up", "proxy_down", "direct_up", "direct_down")),
                ("nodes", nodes, ("up", "down")),
                ("sites", sites, ("proxy_up", "proxy_down", "direct_up", "direct_down")),
            ):
                bucket = slots[field].setdefault(key, {})
                for name in rng.sample(names, rng.randint(1, len(names))):
                    bucket[name] = {k: rng.choice((0, rng.randint(1, 10**9))) for k in keys}
        slots["slots"]["garbage-key"] = {"chrome": {"proxy_up": 5}}
        slots["nodes"]["2026-13-45T99:99"] = {"HK-01": {"up": 5}}
        programs = {}
        for name in procs[:-1]:  # curl 只在时段数据里有
            vals = {k: rng.randint(0, 10**11) for k in ("proxy_up", "proxy_down", "direct_up", "direct_down")}
            programs[name] = {
                **vals,
                "upload": vals["proxy_up"] + vals["direct_up"],
                "download": vals["proxy_down"] + vals["direct_down"],
                "conns": rng.randint(0, 5000),
                "last": int(NOW),
                "last_node": rng.choice(nodes),
                "last_cmd": f"{name}.exe --flag",
                "last_path": rf"C:\Apps\{name}.exe",
                "sites": {s: {"upload": 1, "download": 2, "proxy_up": 1, "proxy_down": 2, "direct_up": 0, "direct_down": 0, "conns": 1, "last": 1} for s in rng.sample(sites, 3)},
            }
        self.write_json("programs.json", {"programs": programs, "updated": int(NOW)})
        self.write_json("slots.json", {**slots, "updated": int(NOW)})
        self.write_json("seen.json", {"names": sorted(procs + ["ghost"])})
        self.write_log([{"process": "chrome", "remote": "t.me:443", "outbound": "proxy", "node": "HK-01", "upload": 1, "download": 1, "end": int(NOW - 60)}])

    def store_rankings(self, s: Any, period: str) -> dict[str, list[tuple]]:
        pack = s.ranking(period)
        return {
            "rows": sorted(proc_rows(pack)),
            "nodes": sorted(node_rows(pack)),
            "sites": sorted(site_rows(pack)),
        }

    def test_migrated_rankings_match_json_oracle(self) -> None:
        self.synth_legacy()
        s = self.make()
        for period in store_mod.PERIODS:
            want = oracle_rankings(self.dir, period, NOW)
            self.assertTrue(want["rows"] and want["nodes"] and want["sites"], period)
            self.assertEqual(self.store_rankings(s, period), want, period)
        rows = {r["process"]: r for r in s.ranking("all")["rows"]}
        self.assertEqual((rows["Code"]["cmd"], rows["Code"]["path"]), ("Code.exe --flag", r"C:\Apps\Code.exe"))

    def test_migration_happens_once_and_keeps_old_files(self) -> None:
        self.synth_legacy()
        before = {p.name: p.read_bytes() for p in self.dir.iterdir()}
        s = self.make()
        first = {p: self.store_rankings(s, p) for p in store_mod.PERIODS}
        s.update([])
        s.update([flow("n1", "chrome", 10, 10)])
        s.close()
        s2 = self.make()
        second = {p: self.store_rankings(s2, p) for p in store_mod.PERIODS}
        for period in store_mod.PERIODS:
            up_before = next((r[1] for r in first[period]["rows"] if r[0] == "chrome"), 0)
            up_after = next((r[1] for r in second[period]["rows"] if r[0] == "chrome"), 0)
            self.assertEqual(up_after - up_before, 10, f"{period}：旧数据没有被重复导入")
            nodes_before = {r[0]: r[1:] for r in first[period]["nodes"]}
            nodes_after = {r[0]: r[1:] for r in second[period]["nodes"]}
            hk = nodes_before.pop("HK-01", (0, 0))
            self.assertEqual(nodes_after.pop("HK-01"), (hk[0] + 10, hk[1] + 10), period)
            self.assertEqual(nodes_after, nodes_before, period)
        db = self.probe()
        self.assertEqual(db.meta("schema"), store_mod.SCHEMA_VERSION)
        self.assertEqual(db.meta("migrated_from"), "programs.json,slots.json,seen.json")
        for name, blob in before.items():
            if name.endswith(".json"):
                self.assertEqual((self.dir / name).read_bytes(), blob, f"{name} 不应被改动")

    def test_seen_is_migrated(self) -> None:
        self.synth_legacy()
        s = self.make()
        s.update([])
        s.update([flow("a", "ghost", 1, 1), flow("b", "brand-new", 1, 1)])
        self.assertEqual([x["process"] for x in s.recent_new()], ["brand-new"])

    def test_failed_first_write_retries_with_marker(self) -> None:
        self.synth_legacy()
        with mock.patch.object(TrafficDB, "write", side_effect=sqlite3.OperationalError("disk full")):
            s = self.make()
            self.assertEqual(self.store_rankings(s, "7d"), oracle_rankings(self.dir, "7d", NOW))
        self.assertIsNone(self.probe().meta("schema"))
        self.assertTrue(s.flush())
        s2 = self.make()
        self.assertEqual(self.store_rankings(s2, "30d"), oracle_rankings(self.dir, "30d", NOW))

    def test_corrupt_database_is_rebuilt_from_legacy_files(self) -> None:
        self.synth_legacy()
        (self.dir / DB_NAME).write_bytes(b"this is not a sqlite database" * 50)
        s = self.make()
        self.assertEqual(self.store_rankings(s, "today"), oracle_rankings(self.dir, "today", NOW))
        self.assertTrue(any(p.name.startswith(DB_NAME + ".corrupt-") for p in self.dir.iterdir()))


if __name__ == "__main__":
    unittest.main()
