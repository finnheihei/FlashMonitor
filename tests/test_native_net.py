"""native_net.py：直接读 Win32 连接表 / 进程表 / 命令行，以及失败时的回退。"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import unittest
from unittest import mock

from flashmonitor import native_net
from flashmonitor.native_net import Fallback

WINDOWS = sys.platform == "win32"


def loopback_pair(family: int, host: str) -> tuple[socket.socket, socket.socket, socket.socket]:
    server = socket.socket(family, socket.SOCK_STREAM)
    server.bind((host, 0))
    server.listen(1)
    client = socket.socket(family, socket.SOCK_STREAM)
    client.connect(server.getsockname()[:2])
    peer, _ = server.accept()
    return server, client, peer


def ipv6_loopback_available() -> bool:
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            s.bind(("::1", 0))
        return True
    except OSError:
        return False


@unittest.skipUnless(WINDOWS, "只在 Windows 上有这些 API")
class NativeWindowsTest(unittest.TestCase):
    def check_pair(self, family: int, host: str) -> None:
        server, client, peer = loopback_pair(family, host)
        try:
            cport = client.getsockname()[1]
            sport = server.getsockname()[1]
            rows = native_net.tcp_connections()
            me = os.getpid()
            got = {(r["local_ip"], r["local_port"], r["remote_ip"], r["remote_port"], r["pid"]) for r in rows}
            self.assertIn((host, cport, host, sport, me), got)
            self.assertIn((host, sport, host, cport, me), got)
            self.assertTrue(all(r["proto"] == "TCP" for r in rows))
            self.assertTrue(all(isinstance(r["local_port"], int) and 0 <= r["local_port"] <= 65535 for r in rows))
        finally:
            for s in (peer, client, server):
                s.close()

    def test_ipv4_loopback_connection_is_listed(self) -> None:
        self.check_pair(socket.AF_INET, "127.0.0.1")

    @unittest.skipUnless(ipv6_loopback_available(), "没有 IPv6 回环")
    def test_ipv6_loopback_connection_is_listed(self) -> None:
        self.check_pair(socket.AF_INET6, "::1")

    def test_listening_sockets_are_not_connections(self) -> None:
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        try:
            port = server.getsockname()[1]
            self.assertFalse(any(r["local_port"] == port for r in native_net.tcp_connections()))
        finally:
            server.close()

    def test_agrees_with_netstat_on_our_pair(self) -> None:
        from flashmonitor.netinfo import netstat_connections

        server, client, peer = loopback_pair(socket.AF_INET, "127.0.0.1")
        try:
            want = ("127.0.0.1", client.getsockname()[1], "127.0.0.1", server.getsockname()[1], os.getpid())
            key = lambda r: (r["local_ip"], r["local_port"], r["remote_ip"], r["remote_port"], r["pid"])  # noqa: E731
            self.assertIn(want, {key(r) for r in native_net.tcp_connections()})
            self.assertIn(want, {key(r) for r in netstat_connections()})
        finally:
            for s in (peer, client, server):
                s.close()

    def test_process_names_include_this_process(self) -> None:
        names = native_net.process_names()
        self.assertTrue(names[os.getpid()].lower().startswith("python"), names.get(os.getpid()))
        self.assertEqual([n for n in names.values() if n.endswith(".exe")], [])
        self.assertEqual(names.get(4), "System")

    def test_cmdline_of_child_process(self) -> None:
        marker = "fm-cmdline-marker-%d" % os.getpid()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", marker])
        try:
            deadline = time.monotonic() + 10
            cmd = ""
            while time.monotonic() < deadline and marker not in cmd:
                cmd = native_net.process_cmdline(child.pid)
                time.sleep(0.05)
            self.assertIn(marker, cmd)
            self.assertIn("time.sleep(30)", cmd)
            self.assertEqual(native_net.cmdlines([child.pid])[child.pid], cmd)
        finally:
            child.kill()
            child.wait(10)
        self.assertEqual(native_net.process_cmdline(child.pid), "")

    def test_cmdline_of_unreadable_pids_is_empty(self) -> None:
        self.assertEqual(native_net.process_cmdline(0), "")
        self.assertEqual(native_net.process_cmdline(-5), "")
        self.assertEqual(native_net.process_cmdline(4), "")  # System：拿不到句柄
        self.assertEqual(native_net.process_cmdline(0x7FFFFFF0), "")


class FallbackTest(unittest.TestCase):
    def test_switches_to_fallback_and_retries_later(self) -> None:
        now = [0.0]
        calls: list[str] = []

        def native(x: int) -> str:
            calls.append("native")
            if now[0] < 400:
                raise OSError("nope")
            return f"native {x}"

        def fallback(x: int) -> str:
            calls.append("fallback")
            return f"fallback {x}"

        f = Fallback("测试", native, fallback, clock=lambda: now[0])
        with mock.patch.object(native_net.fmlog, "log_exception") as log:
            self.assertEqual(f(1), "fallback 1")
            self.assertEqual(f(2), "fallback 2")
            self.assertFalse(f.native_ok)
            now[0] = 500
            self.assertEqual(f(3), "native 3")
        self.assertEqual(calls, ["native", "fallback", "fallback", "native"])
        self.assertEqual(log.call_count, 1)

    def test_native_success_never_calls_fallback(self) -> None:
        f = Fallback("测试", lambda: 1, lambda: self.fail("不该调用"))
        self.assertEqual([f() for _ in range(3)], [1, 1, 1])

    @unittest.skipIf(WINDOWS, "非 Windows 上 native 直接失败")
    def test_non_windows_raises(self) -> None:
        with self.assertRaises(OSError):
            native_net.tcp_connections()


if __name__ == "__main__":
    unittest.main()
