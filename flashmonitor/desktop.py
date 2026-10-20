"""WebView2 用不了时的后备窗口：Edge 的 --app 无标签窗口。"""

from __future__ import annotations

import ctypes
import os
import subprocess
import time
import webbrowser
from ctypes import wintypes
from pathlib import Path
from urllib.request import OpenerDirector, ProxyHandler, build_opener

from . import APP_NAME


def find_edge() -> Path | None:
    roots = []
    for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        raw = os.environ.get(key)
        if raw:
            roots.append(Path(raw))
    for root in roots:
        for rel in (
            Path("Microsoft") / "Edge" / "Application" / "msedge.exe",
            Path("Microsoft") / "Edge Beta" / "Application" / "msedge.exe",
        ):
            p = root / rel
            if p.is_file():
                return p
    return None


def wait_http(url: str, timeout: float = 8.0, opener: OpenerDirector | None = None) -> None:
    """等本机页面能打开（最多 timeout 秒）。不走代理：设了 HTTP_PROXY 时 urllib 会把 127.0.0.1 也发给代理。"""
    opener = opener or build_opener(ProxyHandler({}))
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with opener.open(url, timeout=0.6) as resp:
                resp.read(64)
            return
        except Exception:  # noqa: BLE001 - 还没起来
            time.sleep(0.12)


class DesktopShell:
    """Edge --app 无标签窗口，当作本机桌面端。"""

    def __init__(self, url: str, *, profile_dir: Path, title: str = APP_NAME, icon_path: Path | None = None) -> None:
        self.url = url
        self.title = title
        self.icon_path = icon_path
        self.profile_dir = Path(profile_dir)
        self.proc: subprocess.Popen[bytes] | None = None
        self.hwnd = 0
        self._edge = find_edge()

    @property
    def available(self) -> bool:
        return self._edge is not None and self._edge.is_file()

    def _enum_top_windows(self) -> list[int]:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        handles: list[int] = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @WNDENUMPROC
        def cb(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd):
                handles.append(int(hwnd))
            return True

        self._enum_cb = cb
        user32.EnumWindows(cb, 0)
        return handles

    def _window_title(self, hwnd: int) -> str:
        user32 = ctypes.WinDLL("user32")
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        n = user32.GetWindowTextLengthW(hwnd) + 1
        buf = ctypes.create_unicode_buffer(max(n, 2))
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW(hwnd, buf, len(buf))
        return buf.value or ""

    def _apply_icon(self, hwnd: int) -> None:
        ico = self.icon_path
        if ico is None or not ico.is_file() or not hwnd:
            return
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
        WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1
        user32.LoadImageW.argtypes = [
            wintypes.HINSTANCE,
            wintypes.LPCWSTR,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        user32.LoadImageW.restype = wintypes.HANDLE
        small = user32.LoadImageW(None, str(ico), IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
        big = user32.LoadImageW(None, str(ico), IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
        if small:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, small)
        if big:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, big)

    def _window_class(self, hwnd: int) -> str:
        user32 = ctypes.WinDLL("user32")
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int
        user32.GetClassNameW(hwnd, buf, 256)
        return buf.value or ""

    def _window_rect(self, hwnd: int) -> tuple[int, int]:
        user32 = ctypes.WinDLL("user32")
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        return int(rect.right - rect.left), int(rect.bottom - rect.top)

    def _is_app_frame(self, hwnd: int) -> bool:
        cls = self._window_class(hwnd)
        if "Chrome_WidgetWin" not in cls:
            return False
        w, h = self._window_rect(hwnd)
        return w >= 480 and h >= 320

    def _looks_ours(self, hwnd: int) -> bool:
        if not self._is_app_frame(hwnd):
            return False
        title = self._window_title(hwnd)
        if not title:
            return True
        keys = (self.title, "Flash Monitor", "127.0.0.1")
        return any(k in title for k in keys)

    def _pick_new_hwnd(self, before: set[int]) -> int:
        for h in self._enum_top_windows():
            if h in before:
                continue
            if self._looks_ours(h):
                return h
        return 0

    def alive(self) -> bool:
        if self.proc is not None and self.proc.poll() is None:
            return True
        if self.hwnd:
            try:
                if ctypes.windll.user32.IsWindow(self.hwnd):
                    return True
            except Exception:
                return False
        return False

    def show(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        if self.alive() and self.hwnd and user32.IsWindow(self.hwnd):
            SW_RESTORE = 9
            user32.ShowWindow(self.hwnd, SW_RESTORE)
            user32.SetForegroundWindow(self.hwnd)
            return
        self._launch()

    def _launch(self) -> None:
        if not self.available or self._edge is None:
            webbrowser.open(self.url)
            return
        wait_http(self.url)
        before = set(self._enum_top_windows())
        profile = self.profile_dir
        profile.mkdir(parents=True, exist_ok=True)
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        self.proc = subprocess.Popen(
            [
                str(self._edge),
                f"--app={self.url}",
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-sync",
                "--disable-features=TranslateUI,msSmartScreenProtection",
                "--window-size=1120,820",
            ],
            cwd=str(profile),
            creationflags=flags,
        )
        deadline = time.time() + 12
        hwnd = 0
        while time.time() < deadline:
            time.sleep(0.12)
            hwnd = self._pick_new_hwnd(before)
            if hwnd:
                break
        if not hwnd:
            hwnd = self._pick_new_hwnd(set())
        self.hwnd = hwnd
        if hwnd:
            ctypes.windll.user32.SetWindowTextW(hwnd, self.title)
            self._apply_icon(hwnd)

    def close(self) -> None:
        if self.hwnd:
            try:
                ctypes.windll.user32.PostMessageW(self.hwnd, 0x0010, 0, 0)
            except Exception:
                pass
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:
                pass
        self.hwnd = 0
        self.proc = None
