"""测试 / 演示用的假 Clash 外部控制器。

提供 GET /connections、GET /configs、GET /traffic（每秒一行 JSON 的流）和 DELETE /connections/<id>。
连接数保持恒定，每条连接按各自速率增长，并按 churn 比例关闭、补新，用来产生稳定可重复的负载。

单独运行（给开发实例演示用）：python tests/fake_clash.py --port 19090
"""

from __future__ import annotations

import argparse
import json
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PROCS = [
    ("chrome", r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    ("msedge", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    ("Code", r"C:\Users\dev\AppData\Local\Programs\Microsoft VS Code\Code.exe"),
    ("steam", r"C:\Program Files (x86)\Steam\steam.exe"),
    ("Weixin", r"C:\Program Files\Tencent\Weixin\Weixin.exe"),
    ("python", r"C:\Python311\python.exe"),
    ("node", r"C:\Program Files\nodejs\node.exe"),
    ("Telegram", r"C:\Users\dev\AppData\Roaming\Telegram Desktop\Telegram.exe"),
]
HOSTS = [
    "www.google.com",
    "github.com",
    "api.github.com",
    "objects.githubusercontent.com",
    "www.youtube.com",
    "i.ytimg.com",
    "www.bilibili.com",
    "cdn.jsdelivr.net",
    "update.code.visualstudio.com",
    "www.baidu.com",
    "weixin.qq.com",
    "steamcdn-a.akamaihd.net",
    "pypi.org",
    "registry.npmjs.org",
    "t.me",
    "news.sina.com.cn",
]
NODES = ["HK-01", "JP-02", "SG-03", "US-04"]


class FakeClash:
    def __init__(self, conns: int = 120, seed: int = 7, churn: float = 0.03) -> None:
        self.rng = random.Random(seed)
        self.lock = threading.Lock()
        self.live: dict[str, dict[str, Any]] = {}
        self.seq = 0
        self.up_total = 0
        self.down_total = 0
        self.rate_up = 0.0
        self.rate_down = 0.0
        self.target = conns
        self.churn = churn
        self.at = time.monotonic()
        self.deleted: list[str] = []
        self.hits: dict[str, int] = {}
        self.tun_enable = False
        for _ in range(conns):
            self._spawn()

    def _spawn(self) -> None:
        self.seq += 1
        proc, path = self.rng.choice(PROCS)
        host = self.rng.choice(HOSTS)
        proxied = self.rng.random() < 0.6
        tun = self.rng.random() < 0.7
        cid = f"fake-{self.seq:06d}"
        self.live[cid] = {
            "id": cid,
            "metadata": {
                "network": "tcp",
                "type": "Tun" if tun else "HTTPS",
                "sourceIP": "198.18.0.1" if tun else "127.0.0.1",
                "sourcePort": str(40000 + self.seq % 20000),
                "host": host,
                "destinationIP": f"203.0.113.{self.seq % 250 + 1}",
                "destinationPort": "443",
                "process": proc + ".exe",
                "processPath": path,
            },
            "chains": [self.rng.choice(NODES), "Proxy"] if proxied else ["DIRECT"],
            "rule": "MATCH" if proxied else "GeoSite",
            "rulePayload": "" if proxied else "cn",
            "upload": 0,
            "download": 0,
            "start": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "_up": self.rng.uniform(100, 40_000),
            "_down": self.rng.uniform(1_000, 600_000),
        }

    def advance(self) -> None:
        with self.lock:
            now = time.monotonic()
            dt = min(max(now - self.at, 0.0), 5.0)
            self.at = now
            up = down = 0
            for c in self.live.values():
                du = int(c["_up"] * dt)
                dd = int(c["_down"] * dt)
                c["upload"] += du
                c["download"] += dd
                up += du
                down += dd
            self.up_total += up
            self.down_total += down
            if dt > 0:
                self.rate_up = up / dt
                self.rate_down = down / dt
            closing = [cid for cid in sorted(self.live) if self.rng.random() < self.churn * dt]
            for cid in closing:
                self.live.pop(cid, None)
            while len(self.live) < self.target:
                self._spawn()

    def payload(self) -> dict[str, Any]:
        self.advance()
        with self.lock:
            conns = [{k: v for k, v in c.items() if not k.startswith("_")} for c in self.live.values()]
            return {
                "downloadTotal": self.down_total,
                "uploadTotal": self.up_total,
                "connections": conns,
                "memory": 0,
            }

    def configs(self) -> dict[str, Any]:
        return {
            "mixed-port": 7890,
            "port": 0,
            "socks-port": 0,
            "redir-port": 0,
            "tproxy-port": 0,
            "tun": {"enable": self.tun_enable},
            "mode": "rule",
        }

    def close(self, cid: str) -> bool:
        with self.lock:
            self.deleted.append(cid)
            return self.live.pop(cid, None) is not None

    def hit(self, key: str) -> None:
        with self.lock:
            self.hits[key] = self.hits.get(key, 0) + 1


def make_handler(fake: FakeClash, secret: str, stopping: threading.Event) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # 安静
            return

        def _authorized(self) -> bool:
            if not secret:
                return True
            if self.headers.get("Authorization") == f"Bearer {secret}":
                return True
            self._json(401, {"message": "Unauthorized"})
            return False

        def _json(self, code: int, data: Any) -> None:
            body = json.dumps(data).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            fake.hit("GET " + path)
            if not self._authorized():
                return
            if path == "/connections":
                self._json(200, fake.payload())
            elif path == "/configs":
                self._json(200, fake.configs())
            elif path == "/version":
                self._json(200, {"version": "fake", "meta": True})
            elif path == "/traffic":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                try:
                    while not stopping.is_set():
                        fake.advance()
                        line = json.dumps({"up": int(fake.rate_up), "down": int(fake.rate_down)}) + "\n"
                        self.wfile.write(line.encode("utf-8"))
                        self.wfile.flush()
                        stopping.wait(1.0)
                except OSError:
                    return
            else:
                self._json(404, {"message": "not found"})

        def do_DELETE(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            fake.hit("DELETE " + path.rsplit("/", 1)[0])
            if not self._authorized():
                return
            if path.startswith("/connections/"):
                fake.close(path.rsplit("/", 1)[1])
                self.send_response(204)
                self.end_headers()
                return
            self._json(404, {"message": "not found"})

    return Handler


class FakeClashServer:
    """后台线程里跑的假控制器；with 语句结束时关闭。"""

    def __init__(self, fake: FakeClash | None = None, *, port: int = 0, secret: str = "") -> None:
        self.fake = fake or FakeClash()
        self.stopping = threading.Event()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(self.fake, secret, self.stopping))
        self.httpd.daemon_threads = True
        self.port = int(self.httpd.server_address[1])
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="fake-clash", daemon=True)

    def __enter__(self) -> FakeClashServer:
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    def start(self) -> FakeClashServer:
        return self.__enter__()

    def stop(self) -> None:
        self.stopping.set()
        self.httpd.shutdown()
        self.httpd.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description="假 Clash 外部控制器")
    parser.add_argument("--port", type=int, default=19090)
    parser.add_argument("--conns", type=int, default=120)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--secret", default="")
    args = parser.parse_args()
    srv = FakeClashServer(FakeClash(conns=args.conns, seed=args.seed), port=args.port, secret=args.secret)
    print(f"fake clash on {srv.base}", flush=True)
    try:
        srv.httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.stopping.set()
        srv.httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
