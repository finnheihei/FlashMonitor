"""collector.py：提醒、导出、合并同类连接，以及 collect() 的整体口径（系统信息全部换成假的）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flashmonitor import collector as col
from flashmonitor.collector import Collector, build_alerts, export_csv, leak_summary, quota_alerts
from flashmonitor.store import TrafficStore
from flashmonitor.views import LiveState, RankCache

MB = 1024 * 1024

ROUTES = {
    "tunName": "FlClash",
    "tunStatus": "Up",
    "tunIp": "198.18.0.1",
    "defaults": [{"DestinationPrefix": "0.0.0.0/0", "NextHop": "0.0.0.0", "InterfaceAlias": "FlClash"}],
    "routes": [
        {"DestinationPrefix": "0.0.0.0/0", "NextHop": "0.0.0.0", "InterfaceAlias": "FlClash"},
        {"DestinationPrefix": "203.0.113.0/24", "NextHop": "192.168.1.1", "InterfaceAlias": "WLAN"},
    ],
    "addrs": {"IPAddress": "192.168.1.10", "InterfaceAlias": "WLAN"},
}
CONNS = [
    {"proto": "TCP", "local_ip": "192.168.1.10", "local_port": 50001, "remote_ip": "203.0.113.5", "remote_port": 443, "pid": 11},
    {"proto": "TCP", "local_ip": "192.168.1.10", "local_port": 50002, "remote_ip": "192.168.1.20", "remote_port": 445, "pid": 12},
    {"proto": "TCP", "local_ip": "192.168.1.10", "local_port": 50003, "remote_ip": "8.8.8.8", "remote_port": 53, "pid": 1},
    {"proto": "TCP", "local_ip": "198.18.0.1", "local_port": 40001, "remote_ip": "198.18.0.9", "remote_port": 443, "pid": 1},
    {"proto": "TCP", "local_ip": "127.0.0.1", "local_port": 40002, "remote_ip": "127.0.0.1", "remote_port": 7890, "pid": 13},
]
NAMES = {1: "FlClashCore", 11: "steam", 12: "svchost", 13: "python"}


def clash_conn(cid: str, proc: str, host: str, chain: str, up: int, down: int, src_port: int = 40002, typ: str = "Tun") -> dict:
    return {
        "id": cid,
        "metadata": {
            "type": typ,
            "sourceIP": "127.0.0.1" if typ == "HTTPS" else "198.18.0.1",
            "sourcePort": str(src_port),
            "host": host,
            "destinationPort": "443",
            "process": proc + ".exe",
            "processPath": rf"C:\Apps\{proc}.exe",
        },
        "chains": [chain],
        "rule": "MATCH",
        "rulePayload": "",
        "upload": up,
        "download": down,
    }


class FakeClash:
    def __init__(self, payload: dict | None) -> None:
        self._payload = payload
        self.published = None

    def recent_payload(self):
        return None

    def payload(self):
        return self._payload

    def configs(self):
        return {"mixed_ports": {7890}, "tun_enable": True}

    def sampled_recently(self):
        return False


class FakeRoutes:
    def __init__(self) -> None:
        self.notes: list = []

    def note_tun(self, enabled):
        self.notes.append(("tun", enabled))

    def note_local_ips(self, ips):
        self.notes.append(("ips", sorted(set(ips))))

    def get(self):
        return ROUTES


class FakeMemo:
    def get(self):
        return {7890}


class FakeCmdlines:
    def __init__(self) -> None:
        self.asked: list = []

    def get(self, pids, **kwargs):
        self.asked.append(set(pids))
        return {13: "python -m http.server"}


class HelpersTest(unittest.TestCase):
    def test_leak_summary(self) -> None:
        flows = [
            {"path": "leak", "process": "steam", "n": 3, "note": "a", "remote": "x"},
            {"path": "leak", "process": "steam"},
            {"path": "leak", "process": "Code", "n": 1},
            {"path": "proxy", "process": "chrome", "n": 9},
        ]
        s = leak_summary(flows)
        self.assertEqual(s["count"], 5)
        self.assertEqual([(r["process"], r["n"]) for r in s["rows"]], [("steam", 4), ("Code", 1)])

    def test_quota_alerts(self) -> None:
        today = {"upload": 50 * MB, "download": 60 * MB, "rows": [{"process": "a", "upload": 30 * MB, "download": 0}, {"process": "b", "upload": 1, "download": 1}]}
        self.assertEqual(quota_alerts(today, total_mb=0, program_mb=0), [])
        got = quota_alerts(today, total_mb=100, program_mb=20)
        self.assertEqual([(a["kind"], a["process"]) for a in got], [("total", ""), ("program", "a")])
        self.assertEqual(quota_alerts(today, total_mb=111, program_mb=0), [])

    def test_build_alerts(self) -> None:
        leaks = {"rows": [{"process": "steam", "n": 2}, {"process": "Ignored", "n": 5}]}
        news = [{"process": f"app{i}", "ts": i} for i in range(8)] + [{"process": "ignored", "ts": 9}]
        today = {"upload": 200 * MB, "download": 0, "rows": [{"process": "ignored", "upload": 200 * MB, "download": 0}]}
        got = build_alerts(today, leaks, news, skip={"ignored"}, show_new_banner=True, total_mb=100, program_mb=100)
        self.assertEqual(got[0], {"kind": "leak", "count": 2, "names": ["steam"]})
        self.assertEqual([a["process"] for a in got if a["kind"] == "new"], ["app3", "app4", "app5", "app6", "app7"])
        self.assertEqual([a["kind"] for a in got[-1:]], ["total"])
        quiet = build_alerts({}, {"rows": []}, news, skip=set(), show_new_banner=False, total_mb=0, program_mb=0)
        self.assertEqual(quiet, [])

    def test_export_csv(self) -> None:
        lanes = {"proxy": {"rows": [{"process": "chrome", "upload": 1, "download": 2, "sites": [{"site": "a.com", "upload": 1, "download": 2}]}]}, "direct": {"rows": []}}
        text = export_csv(lanes).decode("utf-8-sig")
        self.assertEqual(text.splitlines(), ["lane,process,site,upload,download", "proxy,chrome,,1,2", "proxy,chrome,a.com,1,2"])

    def test_collapse(self) -> None:
        base = {"path": "proxy", "inbound": "tun", "outbound": "proxy", "node": "HK", "process": "chrome", "pid": 0, "remote": "a:443", "route": "HK", "note": "n"}
        flows = [
            {**base, "id": "1", "upload": 1, "download": 10},
            {**base, "id": "2", "upload": 2, "download": 20},
            {**base, "process": "Code", "id": "3", "upload": 0, "download": 99},
            {**base, "path": "local", "outbound": "", "process": "svchost", "upload": 0, "download": 0},
        ]
        out = Collector._collapse(flows)
        self.assertEqual([(f["process"], f["n"]) for f in out], [("Code", 1), ("chrome", 2), ("svchost", 1)])
        chrome = out[1]
        self.assertEqual((chrome["ids"], chrome["upload"], chrome["download"], chrome["note"]), (["1", "2"], 3, 30, "n ×2"))
        self.assertEqual(out[2]["ids"], [])


class CollectTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = TrafficStore(Path(self._tmp.name))
        self.addCleanup(self.store.close)
        patches = [
            mock.patch.object(col, "tcp_connections", lambda: list(CONNS)),
            mock.patch.object(col, "process_names", lambda: dict(NAMES)),
            mock.patch.object(col, "windows_proxy_info", lambda: (True, {7890})),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.routes = FakeRoutes()
        self.cmdlines = FakeCmdlines()

    def collector(self, payload) -> Collector:
        return Collector(
            store=self.store,
            clash=FakeClash(payload),
            ranks=RankCache(self.store.ranking),
            live=LiveState(),
            settings=lambda: {"show_new_banner": True},
            ignored=lambda: set(),
            proxy_ports={8443},
            listen_ports=FakeMemo(),
            routes=self.routes,
            cmdlines=self.cmdlines,
            identities=col.IdentityReader(query=lambda pid:dict(status='verified',created=1,path='C:\\Apps\\'+NAMES.get(pid,'unknown')+'.exe')),
        )

    def test_without_clash_uses_connection_table(self) -> None:
        state = self.collector(None).collect()
        status = state["status"]
        self.assertEqual((status["clash_api"], status["tun_up"], status["mode"], status["leak_n"]), (False, True, "both", 1))
        self.assertEqual(status["default_v4"], "FlClash → 0.0.0.0")
        paths = sorted((f["process"], f["path"]) for f in state["flows"])
        self.assertEqual(paths, [("FlClashCore", "dns"), ("python", "sysproxy"), ("steam", "leak"), ("svchost", "local")])
        self.assertEqual(state["counts"]["leak"], 1)
        self.assertEqual(state["leaks"]["rows"][0]["process"], "steam")
        self.assertIn(("tun", True), self.routes.notes)
        self.assertIn(("ips", ["127.0.0.1", "192.168.1.10", "198.18.0.1"]), self.routes.notes)

    def test_with_clash_prefers_api_flows_and_keeps_leaks(self) -> None:
        payload = {
            "connections": [
                clash_conn("a", "chrome", "www.google.com", "HK-01", 10, 100),
                clash_conn("b", "chrome", "www.google.com", "HK-01", 5, 50),
                clash_conn("c", "python", "pypi.org", "DIRECT", 1, 1, src_port=40002, typ="HTTPS"),
                "garbage",
            ],
            "uploadTotal": 1000,
            "downloadTotal": 9000,
        }
        c = self.collector(payload)
        state = c.collect()
        status = state["status"]
        self.assertEqual((status["clash_api"], status["nodes"], status["upload_total"]), (True, ["HK-01"], 1000))
        rows = sorted((f["process"], f["path"], f.get("n")) for f in state["flows"])
        self.assertEqual(
            rows,
            [("FlClashCore", "dns", 1), ("chrome", "proxy", 2), ("python", "direct", 1), ("steam", "leak", 1), ("svchost", "local", 1)],
        )
        py = next(f for f in state["flows"] if f["process"] == "python")
        self.assertEqual((py["pid"], py["cmd"], py["inbound"]), (13, "python -m http.server", "sysproxy"))
        self.assertEqual(self.cmdlines.asked, [{13}])
        self.assertEqual(state["counts"]["proxy"], 2)
        # 第一次只是记下字节数（不计入），第二次开始累计
        payload["connections"][0]["download"] = 300
        c.collect()
        self.assertEqual(self.store.program_rows()[0]["download"], 200)
        alerts = c.with_alerts(state)["alerts"]
        self.assertEqual(alerts[0]["kind"], "leak")


if __name__ == "__main__":
    unittest.main()
