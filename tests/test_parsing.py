"""parsing.py：版本号、订阅流量头、netstat / tasklist 输出、Clash 配置端口。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from flashmonitor.parsing import (
    as_int,
    fmt_speed_text,
    load_listen_ports,
    load_proxy_ports,
    parse_listen_ports,
    parse_netstat_text,
    parse_subscription_userinfo,
    parse_tasklist_csv,
    parse_version,
    proxy_server_ports,
    shorten_cmd,
    split_endpoint,
)

NETSTAT = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1234
  TCP    192.168.1.10:50000     142.250.1.1:443        ESTABLISHED     5678
  TCP    [::1]:5000             [::1]:6000             ESTABLISHED     42
  TCP    [2001:db8::5]:50001    [2001:db8::1]:443      ESTABLISHED     77
  TCP    [fe80::1%12]:445       [fe80::2%12]:50200     ESTABLISHED     4
  TCP    127.0.0.1:7000         127.0.0.1:7890         TIME_WAIT       0
  TCP    10.0.0.2:1             0.0.0.0:0              ESTABLISHED     9
  UDP    0.0.0.0:5353           *:*                                    999
"""

CONFIG = """\
mixed-port: 7897
port: 7891
socks-port: '7892'
redir-port: 0
allow-lan: false
proxies:
  - name: a
    type: ss
    port: 8443
  - {name: b, server: x.example, port: 443, type: vmess}
proxy-groups:
  - name: g
    port: 1
rules:
  - MATCH,g
"""


class VersionTest(unittest.TestCase):
    def test_parse_version(self) -> None:
        self.assertEqual(parse_version("v1.2.3"), (1, 2, 3))
        self.assertEqual(parse_version("0.8"), (0, 8, 0))
        self.assertEqual(parse_version("1.2.3.4"), (1, 2, 3))
        self.assertEqual(parse_version("v1.2.3-rc1"), (1, 2, 3))
        self.assertEqual(parse_version(""), (0, 0, 0))
        self.assertEqual(parse_version("garbage"), (0, 0, 0))

    def test_ordering(self) -> None:
        self.assertGreater(parse_version("1.10.0"), parse_version("1.9.9"))
        self.assertGreater(parse_version("v0.9.0"), parse_version("0.8.0"))
        self.assertEqual(parse_version("V0.9"), parse_version("0.9.0"))


class SubscriptionTest(unittest.TestCase):
    def test_standard_header(self) -> None:
        got = parse_subscription_userinfo("upload=1234; download=5678; total=10737418240; expire=1767225600")
        self.assertEqual(got, {"upload": 1234, "download": 5678, "total": 10737418240, "expire": 1767225600})

    def test_variants(self) -> None:
        self.assertEqual(parse_subscription_userinfo("upload=1,download=2")["download"], 2)
        self.assertEqual(parse_subscription_userinfo('Upload="12"; total=1.5e3')["upload"], 12)
        self.assertEqual(parse_subscription_userinfo("total=1.5e3")["total"], 1500)

    def test_empty_or_garbage(self) -> None:
        self.assertIsNone(parse_subscription_userinfo(""))
        self.assertIsNone(parse_subscription_userinfo("foo=bar; baz"))
        self.assertIsNone(parse_subscription_userinfo("upload=abc"))


class EndpointTest(unittest.TestCase):
    def test_split_endpoint(self) -> None:
        self.assertEqual(split_endpoint("1.2.3.4:80"), ("1.2.3.4", 80))
        self.assertEqual(split_endpoint("[::1]:443"), ("::1", 443))
        self.assertEqual(split_endpoint("[fe80::1%12]:445"), ("fe80::1%12", 445))
        self.assertEqual(split_endpoint("fe80::1:443"), ("fe80::1", 443))
        self.assertEqual(split_endpoint("*:*"), ("*", 0))
        self.assertEqual(split_endpoint("[::]:0"), ("::", 0))


class NetstatTest(unittest.TestCase):
    def test_only_established_tcp(self) -> None:
        rows = parse_netstat_text(NETSTAT)
        got = [(r["local_ip"], r["local_port"], r["remote_ip"], r["remote_port"], r["pid"]) for r in rows]
        self.assertEqual(
            got,
            [
                ("192.168.1.10", 50000, "142.250.1.1", 443, 5678),
                ("::1", 5000, "::1", 6000, 42),
                ("2001:db8::5", 50001, "2001:db8::1", 443, 77),
                ("fe80::1%12", 445, "fe80::2%12", 50200, 4),
            ],
        )
        self.assertTrue(all(r["proto"] == "TCP" for r in rows))

    def test_empty(self) -> None:
        self.assertEqual(parse_netstat_text(""), [])


class TasklistTest(unittest.TestCase):
    def test_parse(self) -> None:
        text = (
            '"System Idle Process","0","Services","0","8 K"\n'
            '"chrome.exe","1234","Console","1","100,000 K"\n'
            '"Code.exe","2,345","Console","1","1 K"\n'
            '"bad"\n'
        )
        self.assertEqual(parse_tasklist_csv(text), {0: "System Idle Process", 1234: "chrome", 2345: "Code"})


class ConfigPortsTest(unittest.TestCase):
    def test_listen_ports(self) -> None:
        self.assertEqual(parse_listen_ports(CONFIG), {7890, 7897, 7891, 7892})
        self.assertEqual(parse_listen_ports(""), {7890})

    def test_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.yaml"
            cfg.write_text(CONFIG, encoding="utf-8")
            self.assertEqual(load_listen_ports(cfg), {7890, 7897, 7891, 7892})
            self.assertEqual(load_proxy_ports(cfg), {8443, 443})
            missing = Path(tmp) / "none.yaml"
            self.assertEqual(load_listen_ports(missing), {7890})
            self.assertEqual(load_proxy_ports(missing), set())

    def test_proxy_server_ports(self) -> None:
        self.assertEqual(proxy_server_ports("127.0.0.1:7890"), {7890})
        self.assertEqual(proxy_server_ports("http=localhost:7891;https=[::1]:7892;ftp=10.0.0.1:21"), {7891, 7892})
        self.assertEqual(proxy_server_ports("127.0.0.1:99999"), set())
        self.assertEqual(proxy_server_ports(""), set())


class MiscTest(unittest.TestCase):
    def test_as_int(self) -> None:
        self.assertEqual(as_int(None), 0)
        self.assertEqual(as_int(False), 0)
        self.assertEqual(as_int(True), 1)
        self.assertEqual(as_int(" 12.7 "), 12)
        self.assertEqual(as_int("x"), 0)
        self.assertEqual(as_int({"Value": "5"}), 5)
        self.assertEqual(as_int([]), 0)

    def test_shorten_cmd(self) -> None:
        self.assertEqual(shorten_cmd("  a   b  "), "a b")
        long = "x" * 300
        out = shorten_cmd(long)
        self.assertEqual(len(out), 80 + 3 + 80)
        self.assertIn(" … ", out)

    def test_fmt_speed(self) -> None:
        self.assertEqual(fmt_speed_text(0), "0")
        self.assertEqual(fmt_speed_text(500), "500 B/s")
        self.assertEqual(fmt_speed_text(1536), "1.5 KB/s")
        self.assertEqual(fmt_speed_text(5 * 1024 * 1024), "5.0 MB/s")


if __name__ == "__main__":
    unittest.main()
