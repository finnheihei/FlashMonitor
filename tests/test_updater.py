"""updater.py：发行版解析、下载校验（本地假服务器）、替换脚本（真跑，路径里有空格 / & / 括号 / 中文）。"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.request import ProxyHandler, build_opener

from flashmonitor import updater
from flashmonitor.updater import (
    UPDATE_SCRIPT,
    Updater,
    UpdateError,
    download_verified,
    launch_update_script,
    parse_release,
    write_update_script,
)

BLOB = os.urandom(64 * 1024) * 20  # 1.25 MB
SHA = hashlib.sha256(BLOB).hexdigest()
OPENER = build_opener(ProxyHandler({}))


def release(tag: str = "v0.9.1", **asset: Any) -> dict:
    base = {
        "name": "FlashMonitor.exe",
        "browser_download_url": "https://example.invalid/FlashMonitor.exe",
        "size": len(BLOB),
        "digest": "sha256:" + SHA,
    }
    base.update(asset)
    return {
        "tag_name": tag,
        "name": f"Flash Monitor {tag}",
        "body": "notes",
        "html_url": "https://github.com/x/y/releases/tag/" + tag,
        "assets": [{"name": "install.ps1", "browser_download_url": "https://x/install.ps1"}, base],
    }


class ParseReleaseTest(unittest.TestCase):
    def test_fields(self) -> None:
        info = parse_release(release(), current="0.9.0", repo="x/y")
        self.assertEqual((info["tag"], info["latest"], info["newer"]), ("v0.9.1", "0.9.1", True))
        self.assertEqual((info["exe_url"], info["exe_size"], info["exe_sha256"]), ("https://example.invalid/FlashMonitor.exe", len(BLOB), SHA))
        self.assertFalse(parse_release(release("v0.9.0"), current="0.9.0", repo="x/y")["newer"])
        self.assertFalse(parse_release(release("v0.8.9"), current="0.9.0", repo="x/y")["newer"])

    def test_digest_variants(self) -> None:
        for digest, want in (
            ("SHA256:" + SHA.upper(), SHA),
            ("sha512:" + "a" * 128, ""),
            ("sha256:xyz", ""),
            (None, ""),
        ):
            self.assertEqual(parse_release(release(digest=digest), current="0", repo="x/y")["exe_sha256"], want, digest)

    def test_missing_asset_and_garbage(self) -> None:
        data = release()
        data["assets"] = [{"name": "other.zip"}, "junk"]
        info = parse_release(data, current="0.9.0", repo="x/y")
        self.assertEqual((info["exe_url"], info["exe_size"], info["exe_sha256"]), ("", 0, ""))
        with self.assertRaises(UpdateError):
            parse_release(["not", "a", "dict"], current="0", repo="x/y")


class BlobServer:
    def __init__(self) -> None:
        self.body = BLOB
        self.hits = 0
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                outer.hits += 1
                if self.path.startswith("/api"):
                    body = json.dumps(outer.release).encode("utf-8")
                else:
                    body = outer.body
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.release = release()
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/FlashMonitor.exe"
        self.release["assets"][1]["browser_download_url"] = self.url
        threading.Thread(target=lambda: self.httpd.serve_forever(poll_interval=0.05), daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class DownloadTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.srv = BlobServer()
        self.addCleanup(self.srv.close)
        self.dest = self.dir / "FlashMonitor.exe.new"

    def leftovers(self) -> list[str]:
        return sorted(p.name for p in self.dir.iterdir())

    def test_good_download(self) -> None:
        got = download_verified(self.srv.url, self.dest, expected_size=len(BLOB), expected_sha256=SHA, opener=OPENER)
        self.assertEqual((got, self.dest.read_bytes() == BLOB), (SHA, True))
        self.assertEqual(self.leftovers(), ["FlashMonitor.exe.new"])

    def test_wrong_digest_is_rejected_and_old_file_kept(self) -> None:
        self.dest.write_bytes(b"old version")
        with self.assertRaisesRegex(UpdateError, "SHA256"):
            download_verified(self.srv.url, self.dest, expected_size=len(BLOB), expected_sha256="0" * 64, opener=OPENER)
        self.assertEqual(self.dest.read_bytes(), b"old version")
        self.assertEqual(self.leftovers(), ["FlashMonitor.exe.new"], "临时文件删了")

    def test_size_checks(self) -> None:
        with self.assertRaisesRegex(UpdateError, "不完整"):
            download_verified(self.srv.url, self.dest, expected_size=len(BLOB) + 1, opener=OPENER)
        with self.assertRaisesRegex(UpdateError, "太小"):
            download_verified(self.srv.url, self.dest, opener=OPENER, min_bytes=len(BLOB) + 1)
        with self.assertRaisesRegex(UpdateError, "大得不正常"):
            download_verified(self.srv.url, self.dest, opener=OPENER, max_bytes=len(BLOB) // 2)
        self.assertEqual(self.leftovers(), [])

    def test_no_digest_only_checks_size(self) -> None:
        self.assertEqual(download_verified(self.srv.url, self.dest, expected_size=len(BLOB), opener=OPENER), SHA)

    def test_network_error_cleans_up(self) -> None:
        with self.assertRaises(OSError):
            download_verified("http://127.0.0.1:9/nothing", self.dest, opener=OPENER, timeout=2)
        self.assertEqual(self.leftovers(), [])

    def make_updater(self, **kw: Any) -> tuple[Updater, list]:
        restarts: list = []
        up = Updater(
            repo="x/y",
            current="0.9.0",
            install_path=lambda: self.dir / "FlashMonitor.exe",
            on_restart=lambda: restarts.append(1),
            opener=OPENER,
            survive_wait=0.0,
            **kw,
        )
        return up, restarts

    def test_updater_rejects_tampered_download_without_touching_install(self) -> None:
        (self.dir / "FlashMonitor.exe").write_bytes(b"current exe")
        self.srv.body = BLOB[:-1] + b"\x00"  # 大小一样，内容被改了一个字节
        up, restarts = self.make_updater()
        with mock.patch.object(updater, "fetch_latest", lambda repo, current, opener=None: parse_release(self.srv.release, current=current, repo=repo)), \
             mock.patch.object(updater, "launch_update_script") as launch:
            result = up.apply()
        self.assertFalse(result["ok"])
        self.assertIn("SHA256", result["error"])
        self.assertEqual((self.dir / "FlashMonitor.exe").read_bytes(), b"current exe")
        self.assertEqual(self.leftovers(), ["FlashMonitor.exe"])
        launch.assert_not_called()
        self.assertEqual(restarts, [])

    def test_updater_happy_path_launches_script(self) -> None:
        up, restarts = self.make_updater()
        with mock.patch.object(updater, "fetch_latest", lambda repo, current, opener=None: parse_release(self.srv.release, current=current, repo=repo)), \
             mock.patch.object(updater, "launch_update_script") as launch:
            result = up.apply()
        self.assertTrue(result["ok"], result)
        self.assertEqual((self.dir / "FlashMonitor.exe.new").read_bytes(), BLOB)
        self.assertTrue((self.dir / updater.SCRIPT_NAME).is_file())
        kw = launch.call_args.kwargs
        self.assertEqual((kw["src"], kw["dst"], kw["wait_pid"]), (self.dir / "FlashMonitor.exe.new", self.dir / "FlashMonitor.exe", os.getpid()))
        for _ in range(50):
            if restarts:
                break
            time.sleep(0.02)
        self.assertEqual(restarts, [1])

    def test_updater_refusal_and_missing_asset(self) -> None:
        up, _ = self.make_updater(refuse=lambda: "开发实例不做在线更新")
        self.assertEqual(up.apply(), {"ok": False, "error": "开发实例不做在线更新"})
        up, _ = self.make_updater()
        data = release()
        data["assets"] = []
        with mock.patch.object(updater, "fetch_latest", lambda repo, current, opener=None: parse_release(data, current=current, repo=repo)):
            self.assertIn("还没有 FlashMonitor.exe", up.apply()["error"])

    def test_latest_is_cached(self) -> None:
        calls: list = []

        def fake(repo: str, current: str, opener: Any = None) -> dict:
            calls.append(1)
            return parse_release(release(), current=current, repo=repo)

        now = [0.0]
        up = Updater(repo="x/y", current="0.9.0", install_path=lambda: self.dir / "x.exe", clock=lambda: now[0])
        with mock.patch.object(updater, "fetch_latest", fake):
            up.latest()
            up.latest()
            now[0] = 91
            up.latest()
            up.latest(force=True)
        self.assertEqual(len(calls), 3)


class ScriptTest(unittest.TestCase):
    def test_script_is_ascii_and_has_no_paths(self) -> None:
        UPDATE_SCRIPT.encode("ascii")
        for needle in (":\\", "Users", "LOCALAPPDATA"):
            self.assertNotIn(needle, UPDATE_SCRIPT)
        for var in ("FM_WAIT_PID", "FM_SRC", "FM_DST", "FM_WORK"):
            self.assertIn(f"%{var}%", UPDATE_SCRIPT)
        self.assertIn('start "" /d "%WORK%" "%DST%" --gui', UPDATE_SCRIPT)

    @unittest.skipUnless(sys.platform == "win32", "Windows 的 cmd 脚本")
    def test_script_replaces_file_in_tricky_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "Flash Monitor & (更新) ^ !test"
            work.mkdir()
            src = work / "FlashMonitor.exe.new"
            dst = work / "FlashMonitor.exe"
            src.write_bytes(b"new version")
            dst.write_bytes(b"old version")
            script = write_update_script(work / updater.SCRIPT_NAME)
            # Hold the old process until explicitly released, independent of CI startup speed.
            waiter = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"],
                                      stdin=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
            proc = None
            try:
                proc = launch_update_script(script, wait_pid=waiter.pid, src=src, dst=dst, work=work, extra_env={"FM_NO_START": "1"})
                time.sleep(1.5)
                self.assertIsNone(waiter.poll(), "the old process is still alive")
                self.assertEqual(dst.read_bytes(), b"old version", "do not replace while the old process is alive")
                waiter.stdin.close()
                waiter.wait(10)
                proc.wait(30)
                self.assertEqual(dst.read_bytes(), b"new version")
                self.assertFalse(src.exists())
                self.assertFalse(script.exists(), "script cleans itself up after success")
            finally:
                if not waiter.stdin.closed:
                    waiter.stdin.close()
                if waiter.poll() is None:
                    waiter.terminate()
                waiter.wait(10)
                if proc is not None:
                    try:
                        proc.wait(5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(5)



if __name__ == "__main__":
    unittest.main()
