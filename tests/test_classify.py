"""classify.py：连接分类、Clash 连接转换、站点归并。"""

from __future__ import annotations

import unittest

from flashmonitor.classify import (
    attach_original_process,
    chain_outbound,
    clash_to_flow,
    classify,
    host_of,
    iface_kind,
    longest_prefix,
    parse_ip,
    site_key,
)

ROUTES = [
    {"DestinationPrefix": "0.0.0.0/0", "NextHop": "0.0.0.0", "InterfaceAlias": "FlClash"},
    {"DestinationPrefix": "0.0.0.0/0", "NextHop": "192.168.1.1", "InterfaceAlias": "WLAN"},
    {"DestinationPrefix": "192.168.1.0/24", "NextHop": "0.0.0.0", "InterfaceAlias": "WLAN"},
    {"DestinationPrefix": "203.0.113.0/24", "NextHop": "192.168.1.1", "InterfaceAlias": "WLAN"},
    {"DestinationPrefix": "120.0.0.0/8", "NextHop": "0.0.0.0", "InterfaceAlias": "aTrust"},
]
ROUTES_NO_TUN = [
    {"DestinationPrefix": "0.0.0.0/0", "NextHop": "192.168.1.1", "InterfaceAlias": "WLAN"},
]


def conn(remote_ip: str, remote_port: int = 443, local_ip: str = "192.168.1.10", local_port: int = 50000) -> dict:
    return {
        "proto": "TCP",
        "local_ip": local_ip,
        "local_port": local_port,
        "remote_ip": remote_ip,
        "remote_port": remote_port,
        "pid": 100,
    }


def run(c: dict, proc: str = "chrome", *, routes=ROUTES, proxy_ports=frozenset({8443}), local_iface="WLAN",
        tun_up=True, default_is_tun=True, mixed=frozenset({7890})) -> tuple[str, str, str]:
    return classify(c, proc, routes, set(proxy_ports), local_iface, tun_up, default_is_tun, set(mixed))


class ClassifyTest(unittest.TestCase):
    def test_default_route_through_tun(self) -> None:
        self.assertEqual(run(conn("8.8.4.4")), ("tun", "WLAN", "已进 TUN"))

    def test_fake_ip_local_address_is_tun(self) -> None:
        c = conn("1.2.3.4", local_ip="198.18.0.1")
        self.assertEqual(run(c), ("tun", "FlClash", "已进 TUN"))

    def test_fake_ip_inside_tun_is_internal(self) -> None:
        c = conn("198.18.0.9", local_ip="198.18.0.1")
        self.assertEqual(run(c)[0], "internal")

    def test_fake_ip_remote(self) -> None:
        self.assertEqual(run(conn("198.18.3.4")), ("tun", "FlClash", "Fake-IP，已进 TUN"))
        self.assertEqual(run(conn("198.18.3.4"), "FlClashCore")[0], "internal")

    def test_lan_is_local(self) -> None:
        self.assertEqual(run(conn("192.168.1.20", 445)), ("local", "WLAN", "局域网 / 本机"))

    def test_loopback_non_proxy_port_is_local(self) -> None:
        self.assertEqual(run(conn("127.0.0.1", 9090, local_ip="127.0.0.1"), local_iface="")[0], "local")

    def test_loopback_mixed_port_is_sysproxy(self) -> None:
        c = conn("127.0.0.1", 7890, local_ip="127.0.0.1")
        self.assertEqual(run(c, local_iface=""), ("sysproxy", "loopback", "走 mixed-port 系统代理"))

    def test_empty_mixed_ports_fall_back_to_7890(self) -> None:
        c = conn("127.0.0.1", 7890, local_ip="127.0.0.1")
        self.assertEqual(run(c, mixed=frozenset())[0], "sysproxy")

    def test_more_specific_route_leaks_when_tun_on(self) -> None:
        self.assertEqual(run(conn("203.0.113.5")), ("leak", "WLAN", "更具体路由绕过 TUN"))

    def test_more_specific_route_is_bypass_when_tun_off(self) -> None:
        got = run(conn("203.0.113.5"), tun_up=False, default_is_tun=False)
        self.assertEqual(got, ("bypass", "WLAN", "直出（未开 TUN）"))

    def test_wifi_default_route_leaks_when_tun_on(self) -> None:
        got = run(conn("8.8.4.4"), routes=ROUTES_NO_TUN, default_is_tun=False)
        self.assertEqual(got, ("leak", "WLAN", "未走 TUN 的公网连接"))

    def test_corporate_vpn(self) -> None:
        self.assertEqual(run(conn("120.1.2.3")), ("leak", "WLAN", "公司 VPN 旁路（aTrust）"))
        self.assertEqual(run(conn("120.1.2.3"), tun_up=False)[0], "bypass")
        self.assertEqual(run(conn("8.8.4.4"), local_iface="aTrust")[0], "leak")

    def test_core_dns_proxy_and_direct(self) -> None:
        self.assertEqual(run(conn("8.8.8.8", 53), "FlClashCore"), ("dns", "WLAN", "Clash 自己的 DNS"))
        self.assertEqual(run(conn("45.1.2.3", 8443), "FlClashCore"), ("proxy", "WLAN", "节点隧道 :8443"))
        self.assertEqual(run(conn("45.1.2.3", 443), "mihomo"), ("direct", "WLAN", "规则直连出口"))

    def test_ipv6(self) -> None:
        v6 = conn("2001:db8::1", local_ip="2001:db8::5")
        self.assertEqual(run(v6), ("leak", "WLAN", "IPv6 未进 TUN，可能漏出"))
        self.assertEqual(run(v6, tun_up=False), ("bypass", "WLAN", "IPv6 直出（未开 TUN）"))
        self.assertEqual(run(v6, "FlClashCore"), ("direct", "WLAN", "内核 IPv6 出口"))
        self.assertEqual(run(v6, local_iface="")[1], "v6")
        self.assertEqual(run(conn("fe80::1", local_ip="fe80::2")), ("local", "v6-local", "本机 IPv6"))
        self.assertEqual(run(conn("::1", 5000, local_ip="::1"))[0], "local")
        self.assertEqual(run(conn("::1", 7890, local_ip="::1"))[0], "sysproxy")

    def test_unparseable_remote(self) -> None:
        self.assertEqual(run(conn("not-an-ip")), ("local", "?", "无法解析地址"))


class RouteHelpersTest(unittest.TestCase):
    def test_longest_prefix_prefers_specific_route(self) -> None:
        ip = parse_ip("203.0.113.9")
        self.assertEqual(longest_prefix(ip, ROUTES)["DestinationPrefix"], "203.0.113.0/24")

    def test_longest_prefix_tie_keeps_first(self) -> None:
        self.assertEqual(longest_prefix(parse_ip("8.8.8.8"), ROUTES)["InterfaceAlias"], "FlClash")

    def test_longest_prefix_skips_garbage(self) -> None:
        routes = [{"DestinationPrefix": "bad"}, {"DestinationPrefix": "x/8"}, {}]
        self.assertIsNone(longest_prefix(parse_ip("8.8.8.8"), routes))

    def test_iface_kind(self) -> None:
        self.assertEqual(iface_kind("FlClash"), "tun")
        self.assertEqual(iface_kind("Meta Tunnel"), "tun")
        self.assertEqual(iface_kind("aTrust"), "vpn")
        self.assertEqual(iface_kind("WLAN 2"), "wifi")
        self.assertEqual(iface_kind("以太网"), "nic")
        self.assertEqual(iface_kind("Ethernet 3"), "nic")
        self.assertEqual(iface_kind(""), "other")


class ClashFlowTest(unittest.TestCase):
    def test_chain_outbound(self) -> None:
        self.assertEqual(chain_outbound(["DIRECT"]), ("direct", "DIRECT"))
        self.assertEqual(chain_outbound(["REJECT"]), ("rejected", "REJECT"))
        self.assertEqual(chain_outbound(["REJECT-DROP"]), ("rejected", "REJECT-DROP"))
        self.assertEqual(chain_outbound(["PASS"]), ("unknown", "PASS"))
        self.assertEqual(chain_outbound(["HK-01", "Proxy"]), ("proxy", "HK-01"))
        self.assertEqual(chain_outbound([]), ("unknown", ""))

    def test_system_proxy_connection(self) -> None:
        flow = clash_to_flow(
            {
                "id": "abc",
                "metadata": {
                    "type": "HTTPS",
                    "sourceIP": "127.0.0.1",
                    "sourcePort": "5000",
                    "host": "www.google.com",
                    "destinationIP": "142.250.1.1",
                    "destinationPort": "443",
                    "process": "chrome.exe",
                    "processPath": "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
                },
                "chains": ["HK-01", "Proxy"],
                "rule": "MATCH",
                "rulePayload": "",
                "upload": 100,
                "download": "2000",
                "start": "2026-09-29T12:00:00Z",
            }
        )
        self.assertEqual(flow["inbound"], "sysproxy")
        self.assertEqual((flow["outbound"], flow["node"], flow["path"]), ("proxy", "HK-01", "proxy"))
        self.assertEqual(flow["process"], "chrome")
        self.assertEqual(flow["remote"], "www.google.com:443")
        self.assertEqual(flow["note"], "HTTPS · HK-01 · MATCH")
        self.assertEqual((flow["upload"], flow["download"]), (100, 2000))
        self.assertEqual(flow["local"], "127.0.0.1:5000")

    def test_tun_direct_connection(self) -> None:
        flow = clash_to_flow(
            {
                "id": "x",
                "metadata": {
                    "type": "Tun",
                    "sourceIP": "198.18.0.1",
                    "destinationIP": "1.2.3.4",
                    "destinationPort": "80",
                    "processPath": "C:\\Users\\a\\Code.exe",
                },
                "chains": ["DIRECT"],
            }
        )
        self.assertEqual((flow["inbound"], flow["outbound"], flow["path"], flow["node"]), ("tun", "direct", "direct", ""))
        self.assertEqual(flow["process"], "Code")
        self.assertEqual(flow["remote"], "1.2.3.4:80")

    def test_missing_process_and_host(self) -> None:
        flow = clash_to_flow({"metadata": {}, "chains": []})
        self.assertEqual((flow["process"], flow["remote"], flow["path"]), ("?", "?", "tun"))

    def test_malformed_fields_do_not_raise(self) -> None:
        for bad in (
            {"metadata": "garbage", "chains": 5},
            {"metadata": None, "chains": "DIRECT"},
            {"metadata": [], "chains": None, "upload": "x", "download": {"Value": 3}},
        ):
            flow = clash_to_flow(bad)
            self.assertEqual(flow["outbound"], "unknown", bad)
        self.assertEqual(clash_to_flow({"metadata": [], "download": {"Value": 3}})["download"], 3)


class SiteKeyTest(unittest.TestCase):
    def test_host_of(self) -> None:
        self.assertEqual(host_of("[2001:db8::1]:443"), "2001:db8::1")
        self.assertEqual(host_of("example.com:443"), "example.com")
        self.assertEqual(host_of("example.com"), "example.com")
        self.assertEqual(host_of("?"), "")
        self.assertEqual(host_of(""), "")

    def test_site_key(self) -> None:
        self.assertEqual(site_key("api.github.com:443"), "github.com")
        self.assertEqual(site_key("www.baidu.com.cn"), "baidu.com.cn")
        self.assertEqual(site_key("a.b.example.co.uk:443"), "example.co.uk")
        self.assertEqual(site_key("x.y.gov.hk"), "y.gov.hk")
        self.assertEqual(site_key("1.2.3.4:80"), "1.2.3.4")
        self.assertEqual(site_key("Example.COM."), "example.com")
        self.assertEqual(site_key("localhost"), "localhost")
        self.assertEqual(site_key("[2001:db8::1]:443"), "2001:db8::1")
        self.assertEqual(site_key(""), "")


class AttachOriginalProcessTest(unittest.TestCase):
    def test_core_direct_rows_are_attributed_to_app(self) -> None:
        flows = [
            {"path": "direct", "process": "FlClashCore", "remote": "1.2.3.4:443", "route": "WLAN", "pid": 1, "local": "192.168.1.10:1"},
            {"path": "tun", "process": "chrome", "remote": "1.2.3.4:443", "pid": 10, "local": "198.18.0.1:5000", "inbound": "tun"},
            {"path": "direct", "process": "FlClashCore", "remote": "5.6.7.8:443", "route": "WLAN", "pid": 1, "local": "x"},
            {"path": "local", "process": "svchost", "remote": "192.168.1.1:53", "pid": 3, "local": "y"},
            {"path": "direct", "process": "mihomo", "remote": "9.9.9.9:443", "route": "WLAN", "pid": 2, "local": "z"},
            {"path": "leak", "process": "steam", "remote": "9.9.9.9:8080", "pid": 11, "local": "w", "inbound": ""},
        ]
        out = attach_original_process(flows)
        procs = sorted((f["process"], f["path"], f["remote"]) for f in out)
        self.assertEqual(
            procs,
            [
                ("chrome", "direct", "1.2.3.4:443"),
                ("steam", "leak", "9.9.9.9:8080"),
                ("svchost", "local", "192.168.1.1:53"),
            ],
        )
        chrome = next(f for f in out if f["process"] == "chrome")
        self.assertEqual((chrome["note"], chrome["route"], chrome["pid"]), ("Clash 直连（唯一端点关联）", "WLAN", 10))
        steam = next(f for f in out if f["process"] == "steam")
        self.assertEqual(steam["inbound"], "")

    def test_ambiguous_or_different_port_never_claims_direct(self):
        core = {"path":"direct", "process":"FlClashCore", "remote":"1.2.3.4:443", "route":"WLAN", "pid":1, "local":"core"}
        app = {"path":"tun", "process":"app", "remote":"1.2.3.4:443", "pid":2, "local":"app"}
        for rows in ([core, app, {**app, 'pid':3, 'process':'other'}],
                     [core, {**core, 'local':'second'}, app],
                     [core, {**app, 'remote':'1.2.3.4:8443'}]):
            before = [dict(r) for r in rows]
            result = attach_original_process(rows)
            self.assertTrue(result)
            self.assertTrue(all(r['path'] == 'tun' for r in result))
            self.assertEqual(rows, before)

    def test_reject_is_not_a_direct_or_proxy_flow(self):
        for action in ('REJECT', 'REJECT-DROP'):
            row = clash_to_flow({'chains':[action], 'metadata':{'type':'Tun'}})
            self.assertEqual((row['outbound'],row['path'],row['node']), ('rejected','rejected',''))


if __name__ == "__main__":
    unittest.main()
