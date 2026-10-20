"""clash.py：外部控制器客户端（对着 tests/fake_clash.py 起的假控制器；不碰本机真的 Clash）。"""

from __future__ import annotations

import unittest

from fake_clash import FakeClash, FakeClashServer

from flashmonitor.clash import ClashClient, valid_connection_id


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


class ClashClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.srv = FakeClashServer(FakeClash(conns=5, seed=1), secret="s3cret").start()
        self.addCleanup(self.srv.stop)
        self.secret = "s3cret"
        self.base = self.srv.base
        self.clock = Clock()
        self.client = ClashClient(lambda: self.base, lambda: self.secret, clock=self.clock, fallbacks=())

    def test_payload(self) -> None:
        data = self.client.payload()
        self.assertEqual(len(data["connections"]), 5)
        self.assertIsInstance(data["uploadTotal"], int)
        self.assertEqual(self.client.base_ok, self.base)

    def test_wrong_secret_or_unreachable(self) -> None:
        self.secret = "nope"
        self.assertIsNone(self.client.payload())
        self.secret = "s3cret"
        self.base = "http://127.0.0.1:9"  # 没人监听
        self.assertIsNone(self.client.payload())
        self.assertFalse(self.client.test_link()["ok"])

    def test_base_urls_order_and_headers(self) -> None:
        c = ClashClient(lambda: "http://127.0.0.1:9097/", lambda: "", fallbacks=("http://127.0.0.1:9090", "http://127.0.0.1:9097"))
        self.assertEqual(c.base_urls(), ("http://127.0.0.1:9097", "http://127.0.0.1:9090"))
        c.remember_base("http://127.0.0.1:9090/connections")
        self.assertEqual(c.base_urls()[0], "http://127.0.0.1:9090")
        self.assertNotIn("Authorization", c.headers())
        self.assertEqual(self.client.headers()["Authorization"], "Bearer s3cret")

    def test_configs_are_cached(self) -> None:
        cfg = self.client.configs()
        self.assertEqual((cfg["mixed_ports"], cfg["tun_enable"]), ({7890}, False))
        self.srv.fake.tun_enable = True
        self.assertIs(self.client.configs()["tun_enable"], False, "20 秒内用缓存")
        self.clock.t += 21
        self.assertIs(self.client.configs()["tun_enable"], True)
        self.assertEqual(self.srv.fake.hits["GET /configs"], 2)

    def test_close_connection(self) -> None:
        cid = next(iter(self.srv.fake.live))
        self.assertTrue(self.client.close_connection(cid))
        self.assertEqual(self.srv.fake.deleted, [cid])
        for bad in ("", "../x", "a/b", "a b", "x" * 81, "a\\b"):
            self.assertFalse(self.client.close_connection(bad), bad)
        self.assertEqual(self.srv.fake.deleted, [cid])

    def test_test_link(self) -> None:
        res = self.client.test_link()
        self.assertEqual((res["ok"], res["connections"], res["base"]), (True, 5, self.base))

    def test_recent_payload(self) -> None:
        self.assertIsNone(self.client.recent_payload())
        self.client.publish({"connections": []})
        self.assertEqual(self.client.recent_payload(), {"connections": []})
        self.assertTrue(self.client.sampled_recently())
        self.clock.t += 2
        self.assertIsNone(self.client.recent_payload())
        self.assertFalse(self.client.sampled_recently())

    def test_valid_connection_id(self) -> None:
        self.assertTrue(valid_connection_id("abc-DEF_123"))
        self.assertFalse(valid_connection_id("a.b"))


if __name__ == "__main__":
    unittest.main()
