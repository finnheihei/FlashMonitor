"""版本号只写一处；构建和发布脚本先跑测试、再生成 versioninfo、最后打包。"""

from __future__ import annotations

import io
import re
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import gen_versioninfo  # noqa: E402

from flashmonitor import APP_VERSION  # noqa: E402


def run(*args: str) -> int:
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return gen_versioninfo.main(list(args))


class VersionTest(unittest.TestCase):
    def test_versioninfo_is_generated_from_app_version(self) -> None:
        self.assertEqual(run("--check"), 0, "versioninfo.txt 过期了：python tools/gen_versioninfo.py")
        text = (ROOT / "versioninfo.txt").read_text(encoding="utf-8")
        self.assertIn(f"u'FileVersion', u'{APP_VERSION}'", text)
        major, minor, patch = (int(x) for x in APP_VERSION.split("."))
        self.assertIn(f"filevers=({major}, {minor}, {patch}, 0)", text)

    def test_tag_check(self) -> None:
        self.assertEqual(run("--tag", f"v{APP_VERSION}"), 0)
        self.assertEqual(run("--tag", f"refs/tags/v{APP_VERSION}"), 0)
        self.assertEqual(run("--tag", "v0.0.1"), 1)

    def test_check_ignores_line_endings_but_not_content(self) -> None:
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "versioninfo.txt"
            with mock.patch.object(gen_versioninfo, "OUT", out):
                self.assertEqual(run("--check"), 1, "文件不存在")
                out.write_bytes(gen_versioninfo.render().replace("\r\n", "\n").encode("utf-8"))
                self.assertEqual(run("--check"), 0, "LF 签出也算最新")
                out.write_bytes(gen_versioninfo.render("0.0.1").encode("utf-8"))
                self.assertEqual(run("--check"), 1, "版本号不一致")
                self.assertEqual(run(), 0)
                self.assertEqual(run("--check"), 0)

    def test_cli_runs_on_a_cp1252_console(self) -> None:
        # GitHub 的 Windows runner：stdout 是管道，编码 cp1252（严格模式），输出中文会让发布流程在第一步就失败
        import os
        import subprocess

        env = dict(os.environ, PYTHONIOENCODING="cp1252", PYTHONDONTWRITEBYTECODE="1")
        env.pop("PYTHONUTF8", None)
        script = str(ROOT / "tools" / "gen_versioninfo.py")
        for args, code in ((["--tag", f"v{APP_VERSION}"], 0), (["--tag", "refs/tags/v0.0.1"], 1), (["--check"], 0), (["--help"], 0)):
            p = subprocess.run([sys.executable, script, *args], cwd=str(ROOT), env=env, capture_output=True, timeout=60)
            self.assertEqual(p.returncode, code, (args, p.stderr[-400:]))
            self.assertNotIn(b"Traceback", p.stderr, args)

    def test_no_other_hardcoded_versions(self) -> None:
        pattern = re.compile(r"(APP_VERSION|__version__)\s*=\s*['\"]")
        generated = {"tests", "output", "build", "dist", ".git", ".planning"}
        hits = [p.relative_to(ROOT) for p in ROOT.rglob("*.py") if p.relative_to(ROOT).parts[0] not in generated and pattern.search(p.read_text(encoding="utf-8"))]
        self.assertEqual([str(h) for h in hits], [str(Path("flashmonitor") / "__init__.py")])


class BuildScriptsTest(unittest.TestCase):
    def order(self, text: str, *needles: str) -> list[int]:
        return [text.find(n) for n in needles]

    def test_build_cmd_tests_first(self) -> None:
        text = (ROOT / "build.cmd").read_text(encoding="utf-8")
        pos = self.order(text, "unittest discover", "gen_versioninfo.py", "PyInstaller --noconfirm")
        self.assertTrue(all(p >= 0 for p in pos), pos)
        self.assertEqual(pos, sorted(pos), "顺序：测试 → versioninfo → 打包")
        self.assertIn("pyinstaller==", text, "PyInstaller 版本固定")

    def test_release_workflow_tests_first(self) -> None:
        text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        pos = self.order(text, "gen_versioninfo.py --tag", "unittest discover", "gen_versioninfo.py\n", "PyInstaller --noconfirm")
        self.assertTrue(all(p >= 0 for p in pos), pos)
        self.assertEqual(pos, sorted(pos), "顺序：检查 tag → 测试 → versioninfo → 打包")
        self.assertIn("pyinstaller==", text)

    def test_pyinstaller_arguments_match(self) -> None:
        def args(text: str) -> str:
            m = re.search(r"PyInstaller (--noconfirm[^\r\n]*)", text)
            self.assertIsNotNone(m)
            return m.group(1).strip()

        build = args((ROOT / "build.cmd").read_text(encoding="utf-8"))
        release = args((ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"))
        self.assertEqual(build, release, "本地打包和发布打包用同一组参数")
        for needle in ("--add-data \"ui.html;.\"", "--version-file versioninfo.txt", "monitor.py"):
            self.assertIn(needle, build)


if __name__ == "__main__":
    unittest.main()
