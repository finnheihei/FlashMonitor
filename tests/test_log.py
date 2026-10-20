"""log.py：限流的异常日志、未捕获异常钩子、日志轮转。"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from flashmonitor import log as fmlog


class LogTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.now = [1000.0]
        patches = [
            mock.patch.object(fmlog, "_DIR", self.dir),
            mock.patch.object(fmlog, "_LAST", {}),
            mock.patch.object(fmlog, "_SUPPRESSED", {}),
            mock.patch.object(fmlog, "_clock", lambda: self.now[0]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def text(self) -> str:
        path = self.dir / "app.log"
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def boom(self, msg: str = "x") -> ValueError:
        try:
            raise ValueError(msg)
        except ValueError as exc:
            return exc

    def test_same_error_is_rate_limited(self) -> None:
        self.assertTrue(fmlog.log_exception("采集", self.boom("first")))
        for _ in range(30):
            self.assertFalse(fmlog.log_exception("采集", self.boom("again")))
        self.assertTrue(fmlog.log_exception("别处", self.boom()), "别的位置单独计")
        self.assertTrue(fmlog.log_exception("采集", KeyError("k")), "别的异常类型单独计")
        self.now[0] += 61
        self.assertTrue(fmlog.log_exception("采集", self.boom("later")))
        log = self.text()
        self.assertEqual(log.count("[采集] ValueError"), 2)
        self.assertIn("另有 30 次未记", log)
        self.assertIn("Traceback", log, "带完整调用栈")
        self.assertIn("raise ValueError(msg)", log)

    def test_current_exception_is_used_by_default(self) -> None:
        try:
            raise RuntimeError("in except block")
        except RuntimeError:
            self.assertTrue(fmlog.log_exception("here"))
        self.assertFalse(fmlog.log_exception("nothing"))
        self.assertIn("RuntimeError: in except block", self.text())

    def test_unconfigured_log_is_a_no_op(self) -> None:
        with mock.patch.object(fmlog, "_DIR", None):
            fmlog.app_log("hello")
            fmlog.log_exception("x", self.boom())
        self.assertEqual(self.text(), "")

    def test_rotation(self) -> None:
        with mock.patch.object(fmlog, "MAX_BYTES", 200):
            for i in range(20):
                fmlog.app_log(f"line {i} " + "x" * 20)
            fmlog.app_log("after rotation")
        self.assertTrue((self.dir / "app.log.1").is_file())
        self.assertLessEqual((self.dir / "app.log.1").stat().st_size, 400)
        self.assertIn("after rotation", self.text())
        self.assertLess((self.dir / "app.log").stat().st_size, 400)

    def test_excepthooks_log_uncaught_errors(self) -> None:
        saved = (sys.excepthook, threading.excepthook)
        self.addCleanup(lambda: (setattr(sys, "excepthook", saved[0]), setattr(threading, "excepthook", saved[1])))
        with mock.patch.object(sys, "excepthook", lambda *a: None), mock.patch.object(threading, "excepthook", lambda a: None):
            fmlog.install_excepthooks()
            exc = self.boom("main thread")
            sys.excepthook(type(exc), exc, exc.__traceback__)
            sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)

            def worker() -> None:
                raise OSError("in worker")

            t = threading.Thread(target=worker, name="采集线程")
            t.start()
            t.join()
        log = self.text()
        self.assertIn("[未捕获异常] ValueError: main thread", log)
        self.assertIn("[线程 采集线程] OSError: in worker", log)
        self.assertNotIn("KeyboardInterrupt", log)


if __name__ == "__main__":
    unittest.main()
