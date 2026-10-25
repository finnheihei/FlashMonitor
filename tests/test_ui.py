"""ui.html：页面脚本和样式里的一致性（不需要浏览器）。"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "ui.html").read_text(encoding="utf-8")
CSS = HTML.split("<style>", 1)[1].split("</style>", 1)[0]


def css_var(name: str) -> str:
    m = re.search(rf"--{name}:\s*(#[0-9a-fA-F]{{6}})", CSS)
    assert m, name
    return m.group(1).lower()


class UiConsistencyTest(unittest.TestCase):
    def test_native_frame_matches_page_colors(self) -> None:
        import webview_win

        bg = css_var("bg")
        line = css_var("line")
        rgb = tuple(int(bg[i : i + 2], 16) for i in (1, 3, 5))
        self.assertEqual(webview_win.PAGE_BG_RGB, rgb)
        bgr = lambda h: int(h[5:7] + h[3:5] + h[1:3], 16)  # noqa: E731 - #rrggbb → COLORREF 0x00bbggrr
        self.assertEqual(webview_win.FRAME_BG, bgr(bg))
        self.assertEqual(webview_win.FRAME_LINE, bgr(line))

    def test_canvas_colors_match_css(self) -> None:
        for lane, var in (("proxy", "proxy"), ("direct", "direct"), ("leak", "leak")):
            m = re.search(rf"{lane}: \{{ c: \"(#[0-9a-f]{{6}})\"", HTML)
            self.assertIsNotNone(m, lane)
            self.assertEqual(m.group(1), css_var(var), lane)

    def test_every_element_id_used_by_the_script_exists(self) -> None:
        script = HTML.split("<script>", 1)[1]
        markup = HTML.split("<script>", 1)[0]
        ids = set(re.findall(r'getElementById\("([a-z0-9-]+)"\)', script))
        defined = set(re.findall(r'id="([a-z0-9-]+)"', HTML))
        # 有些元素是脚本自己生成的（总览页、火花线），它们的 id 出现在脚本的模板字符串里
        missing = sorted(i for i in ids if i not in defined)
        self.assertEqual(missing, [], "脚本用到了页面里没有的元素")
        static = {"filters", "status", "q", "win-btns", "setup", "quota", "toast", "rank-modes", "periods", "sort", "btn-leak", "btn-export", "show-ignored", "chk-ignored", "main", "brand-ver"}
        self.assertEqual(sorted(i for i in static if f'id="{i}"' not in markup), [], "这些元素应该写在静态 HTML 里")

    def test_api_calls_go_through_the_helper(self) -> None:
        script = HTML.split("<script>", 1)[1]
        self.assertEqual(script.count("fetch("), 1, "只有 api() 里直接调 fetch（它负责带页面令牌）")


if __name__ == "__main__":
    unittest.main()
