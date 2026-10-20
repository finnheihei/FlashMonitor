"""Regression for off-screen native windows and minimized placement persistence."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

@unittest.skipUnless(sys.platform == "win32", "native Windows host")
class WindowPlacementTests(unittest.TestCase):
    def test_offscreen_minimized_coordinates_are_clamped(self):
        from webview_win import fit_window_box
        self.assertEqual(fit_window_box(960,640,-32000,-32000,(0,0,1920,1040)),(960,640,0,0))

    def test_negative_secondary_monitor_is_preserved(self):
        from webview_win import fit_window_box
        self.assertEqual(fit_window_box(960,640,-1500,80,(-1920,0,0,1040)),(960,640,-1500,80))

    def test_oversized_window_fits_work_area(self):
        from webview_win import fit_window_box
        self.assertEqual(fit_window_box(2200,1500,900,800,(0,0,1280,720)),(1280,720,0,0))

    def test_minimized_or_maximized_window_does_not_overwrite_normal_bounds(self):
        from webview_win import FlashDesktop
        for minimized,maximized in [(True,False),(False,True)]:
            with self.subTest(minimized=minimized), tempfile.TemporaryDirectory() as tmp:
                desktop=FlashDesktop.__new__(FlashDesktop)
                desktop.hwnd=1
                desktop.placement_file=Path(tmp)/"window.json"
                desktop.placement_file.write_text("original")
                with patch("webview_win.ctypes.windll.user32") as user32:
                    user32.IsIconic.return_value=minimized
                    user32.IsZoomed.return_value=maximized
                    desktop._save_box()
                    user32.GetWindowRect.assert_not_called()
                self.assertEqual(desktop.placement_file.read_text(),"original")
