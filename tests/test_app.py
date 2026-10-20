"""app.py / winshell.py：导入无副作用、开机启动命令格式不变、AppContext 接线、--once 的保护。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flashmonitor import paths, winshell

ROOT = Path(__file__).resolve().parent.parent
MODULES = [
    "flashmonitor",
    "flashmonitor.app",
    "flashmonitor.classify",
    "flashmonitor.clash",
    "flashmonitor.collector",
    "flashmonitor.db",
    "flashmonitor.desktop",
    "flashmonitor.icons",
    "flashmonitor.log",
    "flashmonitor.native_net",
    "flashmonitor.netinfo",
    "flashmonitor.parsing",
    "flashmonitor.paths",
    "flashmonitor.proc",
    "flashmonitor.server",
    "flashmonitor.settings",
    "flashmonitor.store",
    "flashmonitor.subinfo",
    "flashmonitor.tray",
    "flashmonitor.updater",
    "flashmonitor.views",
    "flashmonitor.winshell",
]


class ImportTest(unittest.TestCase):
    def test_importing_the_package_touches_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for name in ("local", "roaming", "cwd"):
                (base / name).mkdir()
            env = dict(
                os.environ,
                LOCALAPPDATA=str(base / "local"),
                APPDATA=str(base / "roaming"),
                FLASHMONITOR_DATA_DIR=str(base / "data"),
                PYTHONPATH=str(ROOT),
                PYTHONDONTWRITEBYTECODE="1",
            )
            code = "import importlib, sys\nfor m in sys.argv[1:]:\n    importlib.import_module(m)\nprint('ok')"
            p = subprocess.run([sys.executable, "-c", code, *MODULES], cwd=str(base / "cwd"), env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr)
            left = sorted(str(q.relative_to(base)) for q in base.rglob("*"))
            self.assertEqual(left, ["cwd", "local", "roaming"], "导入时建了文件或目录")


class LaunchCommandTest(unittest.TestCase):
    def test_source_run_command_matches_0_8_0_format(self) -> None:
        py = Path(sys.executable)
        exe = py.with_name("pythonw.exe") if py.with_name("pythonw.exe").is_file() else py
        want = f'"{exe}" "{ROOT / "monitor.py"}" --gui --minimized --no-browser'
        self.assertEqual(winshell.launch_command(autostart=True, start_minimized=False, open_browser=True), want)
        self.assertEqual(
            winshell.launch_command(autostart=False, start_minimized=False, open_browser=True),
            f'"{exe}" "{ROOT / "monitor.py"}" --gui',
        )
        self.assertEqual(
            winshell.launch_command(autostart=False, start_minimized=True, open_browser=False),
            f'"{exe}" "{ROOT / "monitor.py"}" --gui --minimized --no-browser',
        )

    def test_frozen_command(self) -> None:
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "executable", r"C:\Apps\Flash Monitor\FlashMonitor.exe"):
            self.assertEqual(
                winshell.launch_command(autostart=True, start_minimized=False, open_browser=False),
                r'"C:\Apps\Flash Monitor\FlashMonitor.exe" --gui --minimized --no-browser',
            )

    def test_instance_names(self) -> None:
        with mock.patch.dict(os.environ, {paths.DATA_DIR_ENV: ""}):
            self.assertEqual(winshell.mutex_name(), "Local\\FlashMonitor.SingleInstance")
            self.assertEqual(winshell.window_class(), "FlashMonitorWnd")
            self.assertEqual(winshell.tray_class(), "FlashMonitorTray")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {paths.DATA_DIR_ENV: tmp}):
            self.assertTrue(winshell.window_class().startswith("FlashMonitorWnd.dev-"))


class AppContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        env = mock.patch.dict(os.environ, {paths.DATA_DIR_ENV: str(base / "data"), "APPDATA": str(base / "roaming")})
        env.start()
        self.addCleanup(env.stop)
        from flashmonitor.app import AppContext, MonitorApi

        self.ctx = AppContext(paths.data_home())
        self.addCleanup(self.ctx.close_store)
        self.api = MonitorApi(self.ctx)

    def test_wiring(self) -> None:
        self.assertEqual(self.api.snapshot("rank", "7d"), {"error": "warming", "view": "rank", "period": "7d", "manual_refresh_supported": True})
        self.ctx.live.set(self.ctx.collector.with_alerts({"ts": 1, "status": {"clash_api": False}, "flows": [], "counts": {}, "leaks": {"count": 0, "rows": []}}))
        snap = self.api.snapshot("overview", "")
        self.assertEqual((snap["view"], snap["status"]["clash_api"]), ("overview", False))
        self.assertTrue(self.api.export_csv().startswith("\ufefflane".encode("utf-8")))
        diag = self.api.diag()
        self.assertEqual((diag["dev_instance"], diag["data_dir"]), (True, str(paths.data_home())))
        self.api.reset()

    def test_settings_hide_secrets_and_dev_refuses_autostart(self) -> None:
        pub = self.api.apply_settings({"clash_secret": "top-secret-xyz", "sub_url": "https://s.example/sub?token=abc123"})
        text = json.dumps(pub, ensure_ascii=False)
        self.assertNotIn("top-secret-xyz", text)
        self.assertNotIn("abc123", text)
        self.assertEqual(pub["page"], "http://127.0.0.1:8787/")
        if sys.platform == "win32":
            pub = self.api.apply_settings({"autostart": True})
            self.assertIn("开发实例", pub["error"])
        self.assertEqual(self.ctx.clash.headers()["Authorization"], "Bearer top-secret-xyz", "Clash 请求里照样带密钥")

    def test_update_refused_in_dev_instance(self) -> None:
        self.assertIn("开发实例", self.api.apply_update()["error"])


class OnceGuardTest(unittest.TestCase):
    def test_once_refuses_to_migrate_while_old_version_runs(self) -> None:
        from flashmonitor import app

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            (data / "programs.json").write_text("{}", encoding="utf-8")
            hooks = (sys.excepthook, __import__("threading").excepthook)
            try:
                with mock.patch.dict(os.environ, {paths.DATA_DIR_ENV: str(data)}), mock.patch.object(
                    winshell, "other_instance_running", lambda: True
                ), mock.patch("sys.stderr") as err:
                    self.assertEqual(app.main(["--once"]), 2)
            finally:
                sys.excepthook, __import__("threading").excepthook = hooks
            self.assertFalse((data / "flashmonitor.db").exists())
            self.assertIn("另一个 Flash Monitor 正在运行", "".join(str(c.args[0]) for c in err.write.call_args_list))


class ConsoleTest(unittest.TestCase):
    def test_hint_survives_narrow_console_encoding(self) -> None:
        # stdout 是 cp1252 的管道（比如重定向到文件的英文系统）：中文提示不能让程序起不来
        import io

        from flashmonitor import app

        raw = io.BytesIO()
        narrow = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
        with mock.patch("sys.stdout", narrow):
            app._say("Flash Monitor  http://127.0.0.1:8787/")
            app._say("仅本机可访问。Ctrl+C 退出。")
        self.assertEqual(raw.getvalue().decode("cp1252").splitlines(), ["Flash Monitor  http://127.0.0.1:8787/", "???????Ctrl+C ???"])
        with mock.patch("sys.stdout", None):
            app._say("pythonw / 打包版没有控制台")  # 不抛异常


if __name__ == "__main__":
    unittest.main()
