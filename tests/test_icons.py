"""icons.py：图标缓存的去重、冷却期和按需提取。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from flashmonitor.icons import MIN_PNG_BYTES, IconCache, extract_icon_png, file_description, icon_key, generic_icon_svg

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


class IconCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "icons"
        self.clock = Clock()
        self.calls: list[tuple[str, Path]] = []
        self.result = True
        self.spawned: list = []

    def extract(self, src: str, dest: Path) -> bool:
        self.calls.append((src, dest))
        if self.result:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(PNG)
        return self.result

    def cache(self) -> IconCache:
        return IconCache(self.root, extract=self.extract, clock=self.clock, spawn=self.spawned.append)

    def run_spawned(self) -> None:
        while self.spawned:
            self.spawned.pop(0)()

    def test_schedules_once_and_remembers_success(self) -> None:
        c = self.cache()
        for _ in range(50):
            c.schedule("chrome", r"C:\chrome.exe")
        self.assertEqual(len(self.spawned), 1, "取的过程中不重复起线程")
        self.run_spawned()
        for _ in range(50):
            c.schedule("chrome", r"C:\chrome.exe")
        self.assertEqual((len(self.spawned), len(self.calls)), (0, 1))
        self.assertEqual(c.fetch("chrome", r"C:\chrome.exe"), c.path_for("chrome"))

    def test_failures_wait_for_cooldown(self) -> None:
        self.result = False
        c = self.cache()
        c.schedule("uwp", r"C:\x.exe")
        self.run_spawned()
        for _ in range(20):
            c.schedule("uwp", r"C:\x.exe")
            self.assertIsNone(c.fetch("uwp", r"C:\x.exe"))
        self.assertEqual(len(self.calls), 1)
        self.clock.t += 601
        self.result = True
        c.schedule("uwp", r"C:\x.exe")
        self.run_spawned()
        self.assertEqual(c.fetch("uwp", r"C:\x.exe"), c.path_for("uwp"))
        self.assertEqual(sum(src == r"C:\x.exe" for src, _ in self.calls), 2)

    def test_existing_file_is_used_without_extracting(self) -> None:
        c = self.cache()
        self.root.mkdir(parents=True)
        c.path_for("Code", r"C:\Code.exe").write_bytes(PNG)
        c.schedule("Code", r"C:\Code.exe")
        self.assertEqual(c.fetch("Code", r"C:\Code.exe"), c.path_for("Code"))
        self.assertEqual((self.spawned, self.calls), ([], []))

    def test_tiny_file_counts_as_missing(self) -> None:
        c = self.cache()
        self.root.mkdir(parents=True)
        c.path_for("bad", r"C:\bad.exe").write_bytes(b"x" * MIN_PNG_BYTES)
        self.assertIsNone(c.fetch("bad", r"C:\bad.exe"))
        self.assertEqual(self.calls, [], "HTTP fetch never extracts synchronously")
        self.run_spawned()
        self.assertEqual(c.fetch("bad", r"C:\bad.exe"), c.path_for("bad"))
        self.assertEqual(sum(src == r"C:\bad.exe" for src, _ in self.calls), 1)

    def test_nothing_to_do_without_name_or_source(self) -> None:
        c = self.cache()
        c.schedule("", r"C:\a.exe")
        c.schedule("a", "")
        self.assertIsNone(c.fetch("", r"C:\a.exe"))
        self.assertIsNone(c.fetch("nosrc", ""))
        self.assertEqual(len(self.spawned), 1, "unknown source schedules only generic fallback")
        self.assertEqual(self.calls, [])

    def test_extractor_exceptions_are_contained(self) -> None:
        def boom(src: str, dest: Path) -> bool:
            raise RuntimeError("GDI+ 挂了")

        c = IconCache(self.root, extract=boom, clock=self.clock, spawn=lambda fn: fn())
        c.schedule("x", r"C:\x.exe")
        self.assertIsNone(c.fetch("x", r"C:\x.exe"))

    def test_icon_key(self) -> None:
        self.assertEqual(icon_key("微信 Weixin"), "_Weixin")
        self.assertEqual(icon_key("   "), "app")
        self.assertEqual(icon_key("a/b\\c"), "a_b_c")

    def test_generic_icons_are_stable_varied_and_do_not_embed_names(self) -> None:
        self.assertEqual(generic_icon_svg(' Worker.EXE '), generic_icon_svg('worker'))
        self.assertEqual(len({generic_icon_svg(name) for name in 'abcdef'}), 6)
        self.assertNotIn(b'<script>', generic_icon_svg('<script>alert(1)</script>'))
        self.assertIn(b'<svg', generic_icon_svg(''))


@unittest.skipUnless(sys.platform == "win32", "需要 Windows")
class WindowsIconTest(unittest.TestCase):
    def test_extracts_python_icon_and_description(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "sub" / "py.png"
            self.assertTrue(extract_icon_png(sys.executable, dest))
            self.assertEqual(dest.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertIn("Python", file_description(sys.executable))
        self.assertFalse(extract_icon_png(r"C:\definitely\missing.exe", Path(tmp) / "x.png"))
        self.assertEqual(file_description(r"C:\definitely\missing.exe"), "")


if __name__ == "__main__":
    unittest.main()
