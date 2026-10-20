"""server.py：路由、状态码、请求体处理；用端口 0 起真服务器，app 换成记录调用的假对象。"""

from __future__ import annotations

import errno
import json
import socket
import sys
import threading
import unittest
import urllib.error
import urllib.request
from typing import Any

from flashmonitor.server import MAX_BODY, ApiServer, bind_server, port_unavailable


class FakeApp:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.close_ok = 1
        self.icon: bytes | None = b"\x89PNG fake"
        self.fail_snapshot = False

    def _rec(self, *args: Any) -> None:
        self.calls.append(args)

    def page_html(self) -> str:
        return "<html><head></head><body>页面</body></html>"

    def snapshot(self, view: str, period: str) -> dict:
        self._rec("snapshot", view, period)
        if self.fail_snapshot:
            raise RuntimeError("boom")
        return {"view": view, "period": period}

    def settings(self) -> dict:
        return {"clash_api": "http://127.0.0.1:9090"}

    def apply_settings(self, patch: dict) -> dict:
        self._rec("apply_settings", patch)
        return {"saved": patch}

    def icon_png(self, name: str) -> bytes | None:
        self._rec("icon", name)
        return self.icon

    def update_info(self) -> dict:
        return {"ok": True, "newer": False}

    def apply_update(self) -> dict:
        self._rec("apply_update")
        return {"ok": False, "error": "no asset"}

    def diag(self) -> dict:
        return {"app": "Flash Monitor"}

    def export_csv(self) -> bytes:
        return "\ufefflane,process\r\n".encode("utf-8")

    def reset(self) -> None:
        self._rec("reset")

    def open_data(self) -> bool:
        self._rec("open_data")
        return True

    def open_install(self) -> bool:
        self._rec("open_install")
        return False

    def open_github(self) -> bool:
        self._rec("open_github")
        return True

    def test_clash(self) -> dict:
        return {"ok": True, "ms": 3}

    def close_connections(self, ids: list[str]) -> int:
        self._rec("close", ids)
        return self.close_ok

    def refresh_sub(self) -> None:
        self._rec("refresh_sub")

    def ack_new(self, events: list[dict]) -> None:
        self._rec("ack_new", events)


class ServerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.app = FakeApp()
        self.srv = ApiServer(("127.0.0.1", 0), self.app)
        self.base = f"http://127.0.0.1:{self.srv.port}"
        self.thread = threading.Thread(target=lambda: self.srv.serve_forever(poll_interval=0.05), daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(5)

    def request(self, method: str, path: str, body: Any = None, headers: dict | None = None) -> tuple[int, dict, bytes]:
        data = None
        hdrs = {"X-FM-Token": self.srv.token, **(headers or {})}
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        hdrs = {k: v for k, v in hdrs.items() if v is not None}  # 传 None 表示不带这个头
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=hdrs)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=10) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def get_json(self, path: str) -> tuple[int, Any]:
        code, _, body = self.request("GET", path)
        return code, json.loads(body)

    def post_json(self, path: str, body: Any = None) -> tuple[int, Any]:
        code, _, raw = self.request("POST", path, {} if body is None else body)
        return code, json.loads(raw)


class RoutesTest(ServerTestBase):
    def test_page(self) -> None:
        for path in ("/", "/index.html"):
            code, headers, body = self.request("GET", path)
            self.assertEqual(code, 200)
            self.assertTrue(headers["Content-Type"].startswith("text/html"))
            self.assertIn("页面", body.decode("utf-8"))
            self.assertEqual(headers["Cache-Control"], "no-store")

    def test_snapshot_passes_view_and_period(self) -> None:
        code, data = self.get_json("/api/snapshot?view=rank&period=7d")
        self.assertEqual((code, data), (200, {"view": "rank", "period": "7d"}))
        code, data = self.get_json("/api/snapshot")
        self.assertEqual(data, {"view": "", "period": ""})

    def test_settings(self) -> None:
        self.assertEqual(self.get_json("/api/settings"), (200, {"clash_api": "http://127.0.0.1:9090"}))
        code, data = self.post_json("/api/settings", {"retention_days": 30})
        self.assertEqual((code, data), (200, {"saved": {"retention_days": 30}}))

    def test_icon(self) -> None:
        code, headers, body = self.request("GET", "/api/icon?p=%E5%BE%AE%E4%BF%A1%25x")
        self.assertEqual((code, headers["Content-Type"], body), (200, "image/png", b"\x89PNG fake"))
        self.assertIn(("icon", "微信%x"), self.app.calls, "只解码一次")
        self.app.icon = None
        code, headers, body = self.request("GET", "/api/icon?p=nothing")
        self.assertEqual((code, headers["Cache-Control"], headers["Content-Type"]), (200, "no-store", "image/svg+xml"))
        self.assertIn(b'<svg', body)
        code, _, _ = self.request("GET", "/api/icon")
        self.assertEqual(code, 404)

    def test_export_csv(self) -> None:
        code, headers, body = self.request("GET", "/api/export.csv")
        self.assertEqual(code, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertTrue(body.startswith("\ufeff".encode("utf-8")))

    def test_simple_posts(self) -> None:
        self.assertEqual(self.post_json("/api/reset"), (200, {"ok": True}))
        self.assertEqual(self.post_json("/api/open-data"), (200, {"ok": True}))
        self.assertEqual(self.post_json("/api/open-install"), (500, {"ok": False}))
        self.assertEqual(self.post_json("/api/refresh-sub"), (200, {"ok": True}))
        self.assertEqual(self.post_json("/api/ack-new", {"events": [{"process": "chrome", "ts": 1}]}), (200, {"ok": True}))
        self.assertEqual(self.post_json("/api/test-clash"), (200, {"ok": True, "ms": 3}))
        self.assertEqual(self.post_json("/api/update"), (502, {"ok": False, "error": "no asset"}))
        names = [c[0] for c in self.app.calls]
        self.assertEqual(names, ["reset", "open_data", "open_install", "refresh_sub", "ack_new", "apply_update"])

    def test_open_github_calls_app_and_ignores_parameters(self) -> None:
        self.assertEqual(self.post_json("/api/open-github", {"url": "https://evil.example"}), (200, {"ok": True}))
        self.assertEqual(self.app.calls, [("open_github",)])

    def test_close_connections(self) -> None:
        self.assertEqual(self.post_json("/api/close", {"ids": ["a", "b", ""]}), (200, {"ok": True, "closed": 1}))
        self.assertEqual(self.post_json("/api/close", {"id": "c"}), (200, {"ok": True, "closed": 1}))
        self.app.close_ok = 0
        self.assertEqual(self.post_json("/api/close", {"ids": ["x"]}), (502, {"ok": False, "closed": 0}))
        self.assertEqual(self.post_json("/api/close", {}), (502, {"ok": False, "closed": 0}))
        many = [str(i) for i in range(500)]
        self.post_json("/api/close", {"ids": many})
        self.assertEqual(len(self.app.calls[-1][1]), 200)
        self.assertEqual([c[1] for c in self.app.calls[:2]], [["a", "b"], ["c"]])

    def test_unknown_paths_and_methods(self) -> None:
        self.assertEqual(self.get_json("/api/nope")[0], 404)
        self.assertEqual(self.post_json("/api/snapshot")[0], 404)
        self.assertEqual(self.get_json("/api/reset")[0], 404)
        code, _, _ = self.request("PUT", "/api/reset", {})
        self.assertEqual(code, 501)

    def test_handler_errors_become_500_and_server_keeps_running(self) -> None:
        self.app.fail_snapshot = True
        code, data = self.get_json("/api/snapshot")
        self.assertEqual(code, 500)
        self.assertFalse(data["ok"])
        self.app.fail_snapshot = False
        self.assertEqual(self.get_json("/api/snapshot?view=conns")[0], 200)

    def test_request_counters(self) -> None:
        self.get_json("/api/snapshot")
        self.get_json("/api/snapshot?view=rank")
        self.get_json("/api/settings")
        self.get_json("/api/nope-1")
        self.get_json("/api/nope-2")
        self.assertEqual(self.srv.stats(), {"http_requests": 5, "snapshot_requests": 2})
        self.assertEqual(set(self.srv._hits), {"/api/snapshot", "/api/settings", "(other)"}, "未知路径不单独记")


class BodyTest(ServerTestBase):
    def test_bad_json_is_rejected(self) -> None:
        code, _, body = self.request("POST", "/api/settings", b"{not json")
        self.assertEqual(code, 400)
        self.assertEqual([c[0] for c in self.app.calls], [])

    def test_non_object_json_means_empty(self) -> None:
        self.assertEqual(self.post_json("/api/settings", [1, 2]), (200, {"saved": {}}))

    def test_too_large_body(self) -> None:
        code, _, _ = self.request("POST", "/api/settings", b"{" + b" " * (MAX_BODY + 10) + b"}")
        self.assertEqual(code, 413)
        self.assertEqual(self.app.calls, [])

    def test_empty_body(self) -> None:
        code, _, raw = self.request("POST", "/api/reset", b"")
        self.assertEqual((code, json.loads(raw)), (200, {"ok": True}))


class SecurityTest(ServerTestBase):
    def raw(self, request: bytes) -> tuple[int, bytes]:
        """直接写原始请求（测试不带 Host 之类 urllib 做不到的情况）。"""
        with socket.create_connection(("127.0.0.1", self.srv.port), timeout=10) as s:
            s.sendall(request)
            chunks = []
            while True:
                part = s.recv(65536)
                if not part:
                    break
                chunks.append(part)
        head, _, body = b"".join(chunks).partition(b"\r\n\r\n")
        return int(head.split()[1]), body

    def reason(self, raw: bytes) -> str:
        try:
            return json.loads(raw).get("reason", "")
        except ValueError:
            return ""

    def test_rejected_request_whose_body_arrives_late_still_gets_the_answer(self) -> None:
        # 浏览器 / http.client 常把请求头和请求体分两次发。被拒的请求体还没到服务器就关了连接的话，
        # 晚到的请求体会让 Windows 回 RST，客户端连 403 都读不到（页面令牌过期就没法自动刷新）
        import time

        port = self.srv.port

        def split_post(path: str, body: bytes, extra: str = "") -> int:
            head = (
                f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-FM-Token: {self.srv.token}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n{extra}\r\n"
            ).encode("ascii")
            with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
                s.sendall(head)
                time.sleep(0.2)
                s.sendall(body)
                time.sleep(0.2)
                data = b""
                while True:
                    part = s.recv(65536)
                    if not part:
                        break
                    data += part
            return int(data.split()[1])

        self.assertEqual(split_post("/api/reset", b'{"a": 1}', "Origin: http://evil.example\r\n"), 403)
        self.assertEqual(split_post("/api/reset", b'{"a": 1}'), 200)
        self.assertEqual(split_post("/api/nope", b'{"a": 1}'), 404)
        self.assertEqual(split_post("/api/settings", b"{" + b" " * (MAX_BODY + 10) + b"}"), 413)
        self.assertEqual([c[0] for c in self.app.calls], ["reset"], "只有合法的那一个到了 app")

    def test_host_must_be_local(self) -> None:
        port = self.srv.port
        for host in (f"evil.example:{port}", "evil.example", "127.0.0.1", f"127.0.0.1:{port + 1}", f"127.0.0.2:{port}", f"localhost.:{port}", f"[::1]:{port}"):
            for method, path in (("GET", "/"), ("GET", "/api/snapshot"), ("GET", "/api/icon?p=x"), ("POST", "/api/reset")):
                code, _, raw = self.request(method, path, {} if method == "POST" else None, headers={"Host": host})
                self.assertEqual((code, self.reason(raw)), (403, "host"), f"{method} {path} Host={host}")
        self.assertEqual(self.app.calls, [], "被拒的请求一个都没到 app")
        for host in (f"127.0.0.1:{port}", f"localhost:{port}", f"LOCALHOST:{port}"):
            code, _, _ = self.request("GET", "/api/snapshot", headers={"Host": host})
            self.assertEqual(code, 200, host)

    def test_missing_host_is_rejected(self) -> None:
        code, body = self.raw(f"GET /api/snapshot HTTP/1.0\r\n{'X-FM-Token'}: {self.srv.token}\r\n\r\n".encode())
        self.assertEqual((code, self.reason(body)), (403, "host"))

    def test_cross_site_fetches_are_rejected(self) -> None:
        for site in ("cross-site", "same-site", "Cross-Site"):
            for method, path in (("GET", "/"), ("GET", "/api/settings"), ("GET", "/api/icon?p=chrome"), ("POST", "/api/reset")):
                code, _, raw = self.request(method, path, {} if method == "POST" else None, headers={"Sec-Fetch-Site": site})
                self.assertEqual((code, self.reason(raw)), (403, "site"), f"{method} {path} {site}")
        self.assertEqual(self.app.calls, [])
        for site in ("same-origin", "none"):
            self.assertEqual(self.request("POST", "/api/reset", {}, headers={"Sec-Fetch-Site": site})[0], 200, site)
            self.assertEqual(self.request("GET", "/", headers={"Sec-Fetch-Site": site})[0], 200, site)

    def test_token_is_required_for_api(self) -> None:
        for token in (None, "", "wrong", self.srv.token[:-1], self.srv.token + "x"):
            for method, path in (("GET", "/api/snapshot"), ("GET", "/api/settings"), ("GET", "/api/diag"), ("GET", "/api/export.csv"), ("POST", "/api/reset"), ("POST", "/api/settings")):
                code, _, raw = self.request(method, path, {} if method == "POST" else None, headers={"X-FM-Token": token})
                self.assertEqual((code, self.reason(raw)), (403, "token"), f"{method} {path} token={token!r}")
        self.assertEqual(self.app.calls, [])
        code, _, _ = self.request("GET", "/api/icon?p=chrome", headers={"X-FM-Token": None})
        self.assertEqual(code, 200, "<img> 带不了自定义头，图标不要令牌")
        code, _, _ = self.request("GET", "/", headers={"X-FM-Token": None})
        self.assertEqual(code, 200, "首页不要令牌（令牌就在首页里）")

    def test_page_carries_token_and_anti_framing_headers(self) -> None:
        code, headers, body = self.request("GET", "/", headers={"X-FM-Token": None})
        html = body.decode("utf-8")
        self.assertIn(f'<meta name="fm-token" content="{self.srv.token}">', html)
        self.assertLess(html.index("fm-token"), html.index("</head>"))
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        other = ApiServer(("127.0.0.1", 0), FakeApp())
        try:
            self.assertNotEqual(other.token, self.srv.token, "每次启动都不一样")
            self.assertGreaterEqual(len(self.srv.token), 40)
        finally:
            other.server_close()

    def test_post_origin_must_be_local(self) -> None:
        port = self.srv.port
        for origin in ("http://evil.example", "null", f"http://127.0.0.1:{port + 1}", f"https://127.0.0.1:{port}", f"http://localhost.:{port}"):
            code, _, raw = self.request("POST", "/api/reset", {}, headers={"Origin": origin})
            self.assertEqual((code, self.reason(raw)), (403, "origin"), origin)
        self.assertEqual(self.app.calls, [])
        for origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            self.assertEqual(self.request("POST", "/api/reset", {}, headers={"Origin": origin})[0], 200, origin)
        self.assertEqual(self.request("GET", "/api/snapshot", headers={"Origin": "http://evil.example"})[0], 200, "GET 不看 Origin（靠令牌和 Sec-Fetch-Site）")

    def test_post_body_must_be_json(self) -> None:
        for ctype in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x", None, "application/jsonx"):
            code, _, raw = self.request("POST", "/api/settings", b'{"retention_days": 7}', headers={"Content-Type": ctype})
            self.assertEqual((code, self.reason(raw)), (415, "content-type"), str(ctype))
        self.assertEqual(self.app.calls, [])
        code, _, _ = self.request("POST", "/api/settings", b'{"retention_days": 7}', headers={"Content-Type": "application/json; charset=utf-8"})
        self.assertEqual(code, 200)
        code, _, _ = self.request("POST", "/api/reset", b"", headers={"Content-Type": None})
        self.assertEqual(code, 200, "没有请求体时不看 Content-Type")
        body = b'{"retention_days": 7}'
        req = (
            f"POST /api/settings HTTP/1.0\r\nHost: 127.0.0.1:{self.srv.port}\r\nX-FM-Token: {self.srv.token}\r\n"
            f"Content-Length: {len(body)}\r\n\r\n"
        ).encode() + body
        code, raw = self.raw(req)
        self.assertEqual((code, self.reason(raw)), (415, "content-type"), "有请求体却不说类型")


class PortFallbackTest(unittest.TestCase):
    def free_run(self, n: int) -> int:
        """找一段连续 n 个空闲端口的起点。"""
        for _ in range(50):
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            base = s.getsockname()[1]
            s.close()
            if base + n >= 65535:
                continue
            probes = []
            try:
                for p in range(base, base + n):
                    t = socket.socket()
                    t.bind(("127.0.0.1", p))
                    probes.append(t)
                return base
            except OSError:
                continue
            finally:
                for t in probes:
                    t.close()
        self.skipTest("找不到连续的空闲端口")

    def test_skips_busy_ports(self) -> None:
        base = self.free_run(4)
        busy = []
        try:
            for p in (base, base + 1):
                s = socket.socket()
                s.bind(("127.0.0.1", p))
                s.listen(1)
                busy.append(s)
            srv = bind_server("127.0.0.1", base, FakeApp())
            try:
                self.assertEqual(srv.port, base + 2)
                self.assertEqual(srv.allowed_hosts, {f"127.0.0.1:{base + 2}", f"localhost:{base + 2}"})
            finally:
                srv.server_close()
        finally:
            for s in busy:
                s.close()

    def test_all_busy_falls_back_to_system_port(self) -> None:
        base = self.free_run(3)
        busy = []
        try:
            for p in range(base, base + 3):
                s = socket.socket()
                s.bind(("127.0.0.1", p))
                s.listen(1)
                busy.append(s)
            srv = bind_server("127.0.0.1", base, FakeApp(), tries=3)
            try:
                self.assertNotIn(srv.port, range(base, base + 3))
                self.assertGreater(srv.port, 0)
            finally:
                srv.server_close()
        finally:
            for s in busy:
                s.close()

    def test_other_errors_are_raised(self) -> None:
        with self.assertRaises(OSError):
            bind_server("203.0.113.1", 8787, FakeApp(), tries=2)  # 不是本机地址：不是"端口被占用"，不该吞掉

    def test_port_unavailable(self) -> None:
        e = OSError(10048, "in use")
        e.winerror = 10048
        self.assertTrue(port_unavailable(e))
        self.assertTrue(port_unavailable(OSError(errno.EADDRINUSE, "x")))
        self.assertTrue(port_unavailable(OSError(errno.EACCES, "x")))
        self.assertFalse(port_unavailable(OSError(errno.ENOENT, "x")))


@unittest.skipUnless(sys.platform == "win32", "Windows 的端口独占")
class ExclusiveBindTest(unittest.TestCase):
    def test_second_bind_on_same_port_fails(self) -> None:
        first = ApiServer(("127.0.0.1", 0), FakeApp())
        try:
            s = socket.socket()
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with self.assertRaises(OSError):
                s.bind(("127.0.0.1", first.port))
            s.close()
            with self.assertRaises(OSError):
                ApiServer(("127.0.0.1", first.port), FakeApp())
        finally:
            first.server_close()


if __name__ == "__main__":
    unittest.main()
