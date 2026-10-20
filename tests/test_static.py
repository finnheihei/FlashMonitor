"""粗粒度静态检查（不依赖 pyflakes）：

- 整个文件里从没绑定过、也不是内置名的名字 —— 拆模块时最容易漏的错；
- 导入了却从没用过的名字（行尾写 `# noqa: F401` 可豁免）。
"""

from __future__ import annotations

import ast
import builtins
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE_NAMES = {"__file__", "__name__", "__doc__", "__spec__", "__builtins__"}


def source_files() -> list[Path]:
    files = [ROOT / "monitor.py", ROOT / "webview_win.py"]
    for pattern in ("flashmonitor/*.py", "tools/*.py", "tests/*.py"):
        files.extend(sorted(ROOT.glob(pattern)))
    return [f for f in files if f.is_file()]


def _bound(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
    return names


def undefined_names(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    known = _bound(tree) | set(dir(builtins)) | MODULE_NAMES
    found = {
        (n.id, n.lineno)
        for n in ast.walk(tree)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in known
    }
    return sorted(found, key=lambda x: x[1])


def unused_imports(path: Path) -> list[tuple[str, int]]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    tree = ast.parse(text, filename=str(path))
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        end = getattr(node, "end_lineno", node.lineno) or node.lineno
        if any("noqa: F401" in lines[i - 1] for i in range(node.lineno, end + 1)):
            continue
        for alias in node.names:
            name = (alias.asname or alias.name).split(".")[0]
            if name not in used:
                out.append((name, node.lineno))
    return out


class StaticNamesTest(unittest.TestCase):
    def test_no_undefined_names(self) -> None:
        problems = {str(p.relative_to(ROOT)): undefined_names(p) for p in source_files()}
        problems = {k: v for k, v in problems.items() if v}
        self.assertEqual(problems, {}, "这些名字没有定义")

    def test_no_unused_imports(self) -> None:
        problems = {}
        for p in source_files():
            if p.name == "__init__.py":
                continue
            bad = unused_imports(p)
            if bad:
                problems[str(p.relative_to(ROOT))] = bad
        self.assertEqual(problems, {}, "这些导入没有用到")


if __name__ == "__main__":
    unittest.main()
