"""本机 HTTP 服务：页面和 JSON 接口，只监听 127.0.0.1。

路由在 ROUTES 表里，具体的事交给构造时传进来的 app（要实现的方法见 AppApi）。
请求处理里的异常写进 app.log、回 500，不影响后面的请求。

防别的网页借浏览器调接口（跨站请求、DNS rebinding）：
- Host 必须是 127.0.0.1:端口 或 localhost:端口；
- 带 Sec-Fetch-Site 的请求只接受 same-origin / none（同一台机器上别的端口的网页算 same-site，也拒绝）；
- /api/* 要带 X-FM-Token（每次启动随机生成，写在首页的 <meta name="fm-token">）；
  /api/icon 例外，因为 <img> 带不了自定义头；
- POST 带 Origin 时必须是本机地址；有请求体时必须是 application/json（跨站表单发不出这种请求）。
"""

from __future__ import annotations

import errno
import hmac
import json
import secrets
import socket
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Protocol
from urllib.parse import parse_qs

from . import log as fmlog
from .icons import generic_icon_svg
from .rule_drafts import DraftError

JSON_TYPE = "application/json; charset=utf-8"
MAX_BODY = 64 * 1024
# 拒绝请求后先把没读的请求体读掉再关连接（见 ApiHandler._drain_body）：最多读这么多、等这么久
DRAIN_LIMIT = 4 * MAX_BODY
DRAIN_SECONDS = 2.0
MAX_CLOSE_IDS = 200
TOKEN_HEADER = "X-FM-Token"
TOKEN_EXEMPT = frozenset({"/api/icon"})
ALLOWED_FETCH_SITES = frozenset({"same-origin", "none"})
PAGE_HEADERS = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
}


class AppApi(Protocol):
    def page_html(self) -> str: ...
    def snapshot(self, view: str, period: str) -> dict[str, Any]: ...
    def settings(self) -> dict[str, Any]: ...
    def refresh_data(self, body: dict[str, Any]) -> dict[str, Any]: ...
    def apply_settings(self, patch: dict[str, Any]) -> dict[str, Any]: ...
    def network_catalog(self) -> dict[str, Any]: ...
    def network_preview(self, body: dict[str, Any]) -> dict[str, Any]: ...
    def scoped_ipv6_catalog(self) -> dict[str, Any]: ...
    def scoped_ipv6_action(self, body: dict[str, Any]) -> dict[str, Any]: ...
    def rule_catalog(self) -> dict[str, Any]: ...
    def rule_context(self, process: str) -> dict[str, Any]: ...
    def rule_draft(self, body: dict[str, Any]) -> dict[str, Any]: ...
    def ipv4_context(self) -> dict[str, Any]: ...
    def ipv4_draft(self, body: dict[str, Any]) -> dict[str, Any]: ...
    def ipv6_catalog(self) -> dict[str, Any]: ...
    def ipv6_plan(self, body: dict[str, Any]) -> dict[str, Any]: ...
    def icon_png(self, name: str, version: str = "") -> bytes | None: ...
    def process_folder(self, body: dict[str, Any]) -> dict[str, Any]: ...
    def update_info(self) -> dict[str, Any]: ...
    def apply_update(self) -> dict[str, Any]: ...
    def diag(self) -> dict[str, Any]: ...
    def export_csv(self) -> bytes: ...
    def reset(self) -> dict[str, Any]: ...
    def open_data(self) -> bool: ...
    def open_install(self) -> bool: ...
    def open_github(self) -> bool: ...
    def test_clash(self) -> dict[str, Any]: ...
    def close_connections(self, ids: list[str]) -> int: ...
    def refresh_sub(self) -> None: ...
    def ack_new(self, events: list[dict[str, Any]]) -> None: ...


class RequestError(Exception):
    def __init__(self, code: int, message: str, reason: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.reason = reason


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True
    # Windows 上 SO_REUSEADDR 会让别的进程也能绑同一个端口、抢走连接；改用独占绑定
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, address: tuple[str, int], app: AppApi, *, token: str | None = None) -> None:
        self.app = app
        self.token = token or secrets.token_urlsafe(32)
        self._hits: dict[str, int] = {}
        self._hits_lock = threading.Lock()
        super().__init__(address, ApiHandler)
        port = self.port
        self.allowed_hosts = frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})
        self.allowed_origins = frozenset({f"http://127.0.0.1:{port}", f"http://localhost:{port}"})

    def count(self, key: str) -> None:
        with self._hits_lock:
            self._hits[key] = self._hits.get(key, 0) + 1

    def stats(self) -> dict[str, int]:
        """诊断用：处理过的请求数。"""
        with self._hits_lock:
            hits = dict(self._hits)
        return {"http_requests": sum(hits.values()), "snapshot_requests": hits.get("/api/snapshot", 0)}

    def server_bind(self) -> None:
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # 跳过 HTTPServer.server_bind 里的 getfqdn：反查 DNS 在 TUN / fake-ip 环境下可能卡很久
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, ConnectionError):
            return  # 页面关了、连接断了
        fmlog.log_exception("HTTP 连接", exc)


PORT_TRIES = 20
_PORT_UNAVAILABLE = {errno.EADDRINUSE, errno.EACCES, 10048, 10013}  # 10013：Hyper-V 等保留的端口段


def port_unavailable(exc: OSError) -> bool:
    return getattr(exc, "winerror", None) in _PORT_UNAVAILABLE or exc.errno in _PORT_UNAVAILABLE


def bind_server(host: str, port: int, app: AppApi, *, tries: int = PORT_TRIES) -> ApiServer:
    """从 port 开始往后试 tries 个端口，被占用或被系统保留就换下一个；都不行让系统随便给一个。"""
    for candidate in range(max(1, port), min(port + tries, 65536)):
        try:
            return ApiServer((host, candidate), app)
        except OSError as exc:
            if not port_unavailable(exc):
                raise
    return ApiServer((host, 0), app)


class ApiHandler(BaseHTTPRequestHandler):
    server: ApiServer
    timeout = 30  # 30 秒内不把请求发完就断开，不占着线程
    query: dict[str, str]

    def log_message(self, fmt: str, *args: Any) -> None:
        return  # 访问日志没用；出错的请求会进 app.log

    # ---------- 回应 ----------

    def send_bytes(
        self,
        code: int,
        body: bytes,
        ctype: str | None,
        *,
        cache: str = "no-store",
        headers: dict[str, str] | None = None,
    ) -> None:
        self._responded = True
        self.send_response(code)
        if ctype:
            self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, code: int, data: Any) -> None:
        self.send_bytes(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), JSON_TYPE)

    # ---------- 请求 ----------

    def read_json(self) -> dict[str, Any]:
        """读 JSON 请求体（最多 64 KB）；没有请求体返回 {}。"""
        raw_len = self.headers.get("Content-Length")
        try:
            n = int(raw_len or 0)
        except ValueError:
            raise RequestError(400, "Content-Length 不对") from None
        if n < 0:
            raise RequestError(400, "Content-Length 不对")
        if n > MAX_BODY:
            self.close_connection = True
            raise RequestError(413, "请求体太大")
        raw = self.rfile.read(n) if n else b""
        self._body_consumed = True
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise RequestError(400, "请求体不是 JSON") from None
        return data if isinstance(data, dict) else {}

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _guard(self, method: str, path: str) -> None:
        """路由之前的统一检查；不通过就抛 RequestError。"""
        srv = self.server
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in srv.allowed_hosts:
            raise RequestError(403, "只接受发往本机地址的请求", "host")
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if site and site not in ALLOWED_FETCH_SITES:
            raise RequestError(403, "拒绝别的网页发来的请求", "site")
        if path.startswith("/api/") and path not in TOKEN_EXEMPT:
            got = (self.headers.get(TOKEN_HEADER) or "").encode("utf-8")
            if not hmac.compare_digest(got, srv.token.encode("utf-8")):
                raise RequestError(403, "页面令牌不对，刷新页面后再试", "token")
        if method == "POST":
            origin = self.headers.get("Origin")
            if origin is not None and origin.strip().lower() not in srv.allowed_origins:
                raise RequestError(403, "拒绝别的网页发来的请求", "origin")
            length = (self.headers.get("Content-Length") or "0").strip()
            if length not in ("", "0"):
                ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                if ctype != "application/json":
                    raise RequestError(415, "请求体必须是 JSON", "content-type")

    def _drain_body(self) -> None:
        """拒绝请求时请求体往往还没读。这时直接关连接，晚到的请求体会让 Windows 回 RST，
        客户端连刚发出的错误回应都读不到（比如页面令牌过期时的 403 reason=token）。先读掉再关。"""
        if self._body_consumed:
            return
        try:
            left = min(int(self.headers.get("Content-Length") or 0), DRAIN_LIMIT)
        except ValueError:
            return
        deadline = time.monotonic() + DRAIN_SECONDS
        try:
            while left > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.connection.settimeout(remaining)
                chunk = self.rfile.read1(min(left, 65536))
                if not chunk:
                    break
                left -= len(chunk)
        except OSError:
            pass
        self._body_consumed = True

    def _dispatch(self, method: str) -> None:
        self._responded = False
        self._body_consumed = False
        path, _, query = self.path.partition("?")
        self.query = {k: v[-1] for k, v in parse_qs(query, keep_blank_values=True).items()}
        route = ROUTES.get((method, path))
        if route is None and method == "GET" and (path == "/" or path.startswith("/index")):
            route = _page
        self.server.count(path if route is not None else "(other)")
        try:
            self._guard(method, path)
            if route is None:
                raise RequestError(404, "没有这个接口")
            route(self)
        except RequestError as exc:
            self.close_connection = True  # 请求体可能没读完
            if not self._responded:
                body: dict[str, Any] = {"ok": False, "error": exc.message}
                if exc.reason:
                    body["reason"] = exc.reason
                self.send_json(exc.code, body)
            self._drain_body()
        except ConnectionError:
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001
            fmlog.log_exception(f"{method} {path}", exc)
            if not self._responded:
                self.send_json(500, {"ok": False, "error": "内部错误，详情在数据目录的 app.log"})
            self.close_connection = True
            self._drain_body()


# ---------- 路由 ----------


def inject_token(html: str, token: str) -> str:
    meta = f'<meta name="fm-token" content="{token}">'
    lower = html.lower()
    at = lower.find("<head>")
    if at < 0:
        return meta + html
    at += len("<head>")
    return html[:at] + "\n" + meta + html[at:]


def _page(h: ApiHandler) -> None:
    html = inject_token(h.server.app.page_html(), h.server.token)
    h.send_bytes(200, html.encode("utf-8"), "text/html; charset=utf-8", headers=PAGE_HEADERS)


def _snapshot(h: ApiHandler) -> None:
    h.send_json(200, h.server.app.snapshot(h.query.get("view", ""), h.query.get("period", "")))


def _settings_get(h: ApiHandler) -> None:
    h.send_json(200, h.server.app.settings())


def _network_catalog(h: ApiHandler) -> None:
    h.send_json(200, h.server.app.network_catalog())


def _refresh_data(h: ApiHandler) -> None:
    try:
        result = h.server.app.refresh_data(h.read_json())
    except DraftError as exc:
        raise RequestError(400, str(exc)) from exc
    h.send_json(200, result)


def _network_preview(h: ApiHandler) -> None:
    try:
        result = h.server.app.network_preview(h.read_json())
    except DraftError as exc:
        raise RequestError(400, str(exc)) from exc
    h.send_json(200, result)


def _rule_catalog(h: ApiHandler) -> None:
    try:
        result=h.server.app.rule_catalog()
    except DraftError as exc:
        raise RequestError(400,str(exc)) from exc
    h.send_json(200,result)


def _rule_context(h: ApiHandler) -> None:
    try:
        result=h.server.app.rule_context(h.query.get("process", ""))
    except DraftError as exc:
        raise RequestError(400,str(exc)) from exc
    h.send_json(200,result)


def _rule_draft(h: ApiHandler) -> None:
    try:
        result=h.server.app.rule_draft(h.read_json())
    except DraftError as exc:
        raise RequestError(400,str(exc)) from exc
    h.send_json(200,result)


def _ipv4_context(h: ApiHandler) -> None:
    try:
        result = h.server.app.ipv4_context()
    except DraftError as exc:
        raise RequestError(400, str(exc)) from exc
    h.send_json(200, result)


def _ipv4_draft(h: ApiHandler) -> None:
    try:
        result = h.server.app.ipv4_draft(h.read_json())
    except DraftError as exc:
        raise RequestError(400, str(exc)) from exc
    h.send_json(200, result)


def _ipv6_catalog(h: ApiHandler) -> None:
    try:result=h.server.app.ipv6_catalog()
    except DraftError as exc:raise RequestError(400,str(exc)) from exc
    h.send_json(200,result)


def _ipv6_plan(h: ApiHandler) -> None:
    try:result=h.server.app.ipv6_plan(h.read_json())
    except DraftError as exc:raise RequestError(400,str(exc)) from exc
    h.send_json(200,result)


def _scoped_ipv6(h: ApiHandler) -> None:
    try:
        result = (h.server.app.scoped_ipv6_action(h.read_json()) if h.command == 'POST'
                  else h.server.app.scoped_ipv6_catalog())
    except DraftError as exc:
        raise RequestError(400, str(exc)) from exc
    h.send_json(200, result)


def _icon(h: ApiHandler) -> None:
    name = (h.query.get("p") or "").strip()
    if not name:
        h.send_bytes(404, b"", None, cache="no-store")
        return
    body = (h.server.app.icon_png(name, h.query['v']) if h.query.get('v') else h.server.app.icon_png(name)) if name else None
    if not body:
        h.send_bytes(200, generic_icon_svg(name), "image/svg+xml", cache="no-store")
        return
    h.send_bytes(200, body, "image/png", cache="no-store")


def _process_folder(h: ApiHandler) -> None:
    try:
        result = h.server.app.process_folder(h.read_json())
    except DraftError as exc:
        raise RequestError(400, str(exc)) from exc
    h.send_json(200, result)


def _update_get(h: ApiHandler) -> None:
    h.send_json(200, h.server.app.update_info())


def _diag(h: ApiHandler) -> None:
    h.send_json(200, h.server.app.diag())


def _export_csv(h: ApiHandler) -> None:
    body = h.server.app.export_csv()
    h.send_bytes(
        200,
        body,
        "text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=tun-split.csv"},
    )


def _reset(h: ApiHandler) -> None:
    h.read_json()
    h.send_json(200, h.server.app.reset())


def _settings_post(h: ApiHandler) -> None:
    h.send_json(200, h.server.app.apply_settings(h.read_json()))


def _opener(action: Callable[[AppApi], bool]) -> Callable[[ApiHandler], None]:
    def route(h: ApiHandler) -> None:
        h.read_json()
        ok = bool(action(h.server.app))
        h.send_json(200 if ok else 500, {"ok": ok})

    return route


def _test_clash(h: ApiHandler) -> None:
    h.read_json()
    h.send_json(200, h.server.app.test_clash())


def _close(h: ApiHandler) -> None:
    body = h.read_json()
    ids = body.get("ids")
    if not isinstance(ids, list):
        one = body.get("id")
        ids = [one] if one else []
    ids = [str(x) for x in ids[:MAX_CLOSE_IDS] if x]
    closed = int(h.server.app.close_connections(ids)) if ids else 0
    h.send_json(200 if closed else 502, {"ok": closed > 0, "closed": closed})


def _refresh_sub(h: ApiHandler) -> None:
    h.read_json()
    h.server.app.refresh_sub()
    h.send_json(200, {"ok": True})


def _ack_new(h: ApiHandler) -> None:
    body = h.read_json()
    events=body.get("events")
    if not isinstance(events,list) or not 1<=len(events)<=80:
        raise RequestError(400,"请确认已展示的程序提示批次")
    for item in events:
        if (not isinstance(item,dict) or not isinstance(item.get("process"),str)
                or not item["process"].strip() or len(item["process"])>240
                or type(item.get("ts")) is not int or item["ts"]<0):
            raise RequestError(400,"程序提示身份无效，请刷新后重试")
    h.server.app.ack_new(events)
    h.send_json(200, {"ok": True})


def _update_post(h: ApiHandler) -> None:
    h.read_json()
    result = h.server.app.apply_update()
    h.send_json(200 if result.get("ok") else 502, result)


ROUTES: dict[tuple[str, str], Callable[[ApiHandler], None]] = {
    ("GET", "/api/snapshot"): _snapshot,
    ("GET", "/api/settings"): _settings_get,
    ("GET", "/api/network-policy"): _network_catalog,
    ("POST", "/api/network-preview"): _network_preview,
    ("POST", "/api/refresh"): _refresh_data,
    ("GET", "/api/rule-drafts"): _rule_catalog,
    ("GET", "/api/rule-context"): _rule_context,
    ("POST", "/api/rule-draft"): _rule_draft,
    ("GET", "/api/ipv4-drafts"): _ipv4_context,
    ("POST", "/api/ipv4-draft"): _ipv4_draft,
    ("GET", "/api/ipv6-plans"): _ipv6_catalog,
    ("POST", "/api/ipv6-plan"): _ipv6_plan,
    ("GET", "/api/scoped-ipv6"): _scoped_ipv6,
    ("POST", "/api/scoped-ipv6"): _scoped_ipv6,
    ("POST", "/api/process-folder"): _process_folder,
    ("GET", "/api/icon"): _icon,
    ("GET", "/api/update"): _update_get,
    ("GET", "/api/diag"): _diag,
    ("GET", "/api/export.csv"): _export_csv,
    ("POST", "/api/reset"): _reset,
    ("POST", "/api/settings"): _settings_post,
    ("POST", "/api/open-data"): _opener(lambda app: app.open_data()),
    ("POST", "/api/open-install"): _opener(lambda app: app.open_install()),
    ("POST", "/api/open-github"): _opener(lambda app: app.open_github()),
    ("POST", "/api/test-clash"): _test_clash,
    ("POST", "/api/close"): _close,
    ("POST", "/api/refresh-sub"): _refresh_sub,
    ("POST", "/api/ack-new"): _ack_new,
    ("POST", "/api/update"): _update_post,
}
