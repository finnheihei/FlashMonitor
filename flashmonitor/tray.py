"""托盘图标（隐藏窗口 + Shell_NotifyIcon，不依赖第三方库）和托盘上的速率提示、气泡提醒。"""

from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable, Mapping

from . import APP_NAME
from . import log as fmlog
from .parsing import fmt_speed_text


class WinTray:
    """Windows notify-icon on a hidden window; no third-party package."""

    ID_OPEN = 1001
    ID_SHOW = 1002
    ID_QUIT = 1003
    WM_TRAY = 0x8001

    def __init__(
        self,
        on_open,
        on_show,
        on_quit,
        *,
        class_name: str,
        title: str = APP_NAME,
        icon_path: Path | None = None,
    ) -> None:
        self.on_open = on_open
        self.on_show = on_show
        self.on_quit = on_quit
        self.title = title
        self.icon_path = icon_path
        self._class_name = class_name
        self.hwnd = None
        self._nid = None
        self._wndproc = None
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self._ready = threading.Event()
        self._thread.start()
        self._ready.wait(3)

    def _run(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        LRESULT = ctypes.c_ssize_t
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = LRESULT
        user32.PostQuitMessage.argtypes = [ctypes.c_int]
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostMessageW.restype = wintypes.BOOL
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HANDLE),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HANDLE),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HANDLE),
                ("szTip", ctypes.c_wchar * 128),
                ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD),
                ("szInfo", ctypes.c_wchar * 256),
                ("uVersion", wintypes.UINT),
                ("szInfoTitle", ctypes.c_wchar * 64),
                ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", ctypes.c_ubyte * 16),
                ("hBalloonIcon", wintypes.HANDLE),
            ]

        class POINT(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

        WM_DESTROY = 0x0002
        WM_COMMAND = 0x0111
        WM_LBUTTONUP = 0x0202
        WM_LBUTTONDBLCLK = 0x0203
        WM_RBUTTONUP = 0x0205
        NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
        NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
        TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100
        WS_POPUP = 0x80000000
        WS_EX_TOOLWINDOW = 0x00000080

        hinst = kernel32.GetModuleHandleW(None)
        IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
        user32.LoadImageW.argtypes = [
            wintypes.HINSTANCE,
            wintypes.LPCWSTR,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        user32.LoadImageW.restype = wintypes.HANDLE
        icon = 0
        ico = self.icon_path
        if ico is not None and ico.is_file():
            icon = user32.LoadImageW(None, str(ico), IMAGE_ICON, 16, 16, LR_LOADFROMFILE) or 0
        if not icon:
            shell32.ExtractIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT]
            shell32.ExtractIconW.restype = ctypes.c_void_p
            icon = shell32.ExtractIconW(hinst, sys.executable, 0) or 0
        if not icon:
            user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
            user32.LoadIconW.restype = wintypes.HANDLE
            icon = user32.LoadIconW(None, ctypes.c_void_p(32512))

        def popup_menu() -> None:
            menu = user32.CreatePopupMenu()
            user32.AppendMenuW(menu, 0, self.ID_OPEN, "打开窗口")
            user32.AppendMenuW(menu, 0, self.ID_QUIT, "退出 Flash Monitor")
            user32.SetForegroundWindow(hwnd)
            pt = POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            cmd = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, pt.x, pt.y, 0, hwnd, None)
            user32.DestroyMenu(menu)
            user32.PostMessageW(hwnd, WM_COMMAND, cmd, 0)

        def handle_command(cmd: int) -> None:
            if cmd in (self.ID_OPEN, self.ID_SHOW):
                self.on_show()
            elif cmd == self.ID_QUIT:
                self.on_quit()

        @WNDPROC
        def wndproc(hwnd, msg, wparam, lparam):
            if msg == self.WM_TRAY:
                if lparam in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self.on_show()
                    return 0
                if lparam == WM_RBUTTONUP:
                    popup_menu()
                    return 0
            elif msg == WM_COMMAND:
                handle_command(int(wparam) & 0xFFFF)
                return 0
            elif msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc = wndproc
        wc = WNDCLASSW()
        wc.lpfnWndProc = wndproc
        wc.hInstance = hinst
        wc.lpszClassName = self._class_name
        atom = user32.RegisterClassW(ctypes.byref(wc))
        if not atom and ctypes.get_last_error() not in (0, 1410):
            self._ready.set()
            return
        hwnd = user32.CreateWindowExW(
            WS_EX_TOOLWINDOW,
            self._class_name,
            self.title,
            WS_POPUP,
            0,
            0,
            0,
            0,
            None,
            None,
            hinst,
            None,
        )
        if not hwnd:
            self._ready.set()
            return
        self.hwnd = hwnd
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = self.WM_TRAY
        nid.hIcon = icon
        nid.szTip = self.title
        self._nid = nid
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        self._ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))

    def balloon(self, title: str, text: str) -> None:
        if self._nid is None or not self.hwnd:
            return
        nid = self._nid
        nid.uFlags = 0x01 | 0x02 | 0x04 | 0x10
        nid.szInfoTitle = title[:63]
        nid.szInfo = text[:255]
        nid.dwInfoFlags = 1
        ctypes.windll.shell32.Shell_NotifyIconW(1, ctypes.byref(nid))

    def set_tip(self, text: str) -> None:
        if self._nid is None or not self.hwnd:
            return
        nid = self._nid
        nid.uFlags = 0x04
        nid.szTip = (text or self.title)[:127]
        ctypes.windll.shell32.Shell_NotifyIconW(1, ctypes.byref(nid))

    def stop(self) -> None:
        if self.hwnd:
            ctypes.windll.user32.PostMessageW(self.hwnd, 0x0010, 0, 0)  # WM_CLOSE
            ctypes.windll.user32.PostMessageW(self.hwnd, 0x0012, 0, 0)  # WM_QUIT


def process_label(name: str, title: str = "") -> str:
    if title:
        return title
    return {
        "msedge": "Edge",
        "chrome": "Chrome",
        "firefox": "Firefox",
        "msedgewebview2": "WebView",
        "Weixin": "微信",
        "WeChatAppEx": "微信",
        "JianyingPro": "剪映",
        "language_server": "Antigravity",
        "Cursor": "Cursor",
        "steam": "Steam",
        "python": "Python",
        "ssh": "SSH",
        "node": "Node.js",
        "MpDefenderCoreService": "Defender",
    }.get(name, name)


def tray_tip_text(live: Mapping[str, Any]) -> str:
    down = fmt_speed_text(float(live.get("down") or 0))
    up = fmt_speed_text(float(live.get("up") or 0))
    eaters = live.get("eaters") or []
    if eaters:
        top = eaters[0]
        name = process_label(str(top.get("process") or ""), str(top.get("title") or ""))
        lane = "走代理" if float(top.get("proxy_down") or 0) >= float(top.get("direct_down") or 0) else "走直连"
        return f"↓ {down}  ↑ {up} · {name} {lane}"
    return f"↓ {down}  ↑ {up}"


class TrayPulse:
    """每 2 秒更新托盘提示文字；有新提醒时弹一次气泡（一次最多一个，同一件事只弹一次）。"""

    def __init__(
        self,
        tray: Any,
        *,
        live_view: Callable[[], Mapping[str, Any]],
        alerts: Callable[[], list[dict[str, Any]]],
        settings: Callable[[], Mapping[str, Any]],
    ) -> None:
        self.tray = tray
        self._live_view = live_view
        self._alerts = alerts
        self._settings = settings
        self.tip = ""
        self.seen_new: set[str] = set()
        self.leak_shown = False
        self.quota_shown: set[str] = set()

    def _balloon(self, title: str, text: str) -> None:
        try:
            self.tray.balloon(title, text)
        except Exception as exc:  # noqa: BLE001
            fmlog.log_exception("托盘气泡", exc)

    def once(self) -> None:
        tip = tray_tip_text(self._live_view())
        if tip != self.tip:
            self.tip = tip
            try:
                self.tray.set_tip(tip)
            except Exception as exc:  # noqa: BLE001
                fmlog.log_exception("托盘提示", exc)
        settings = self._settings()
        alerts = self._alerts()
        for item in alerts:
            kind = item.get("kind")
            if kind == "new":
                name = str(item.get("process") or "")
                if name and name not in self.seen_new:
                    self.seen_new.add(name)
                    if settings.get("notify_new_app"):
                        self._balloon("新程序走网", f"{process_label(name)} 开始走网了")
                        return
                continue
            if kind == "leak" and settings.get("notify_leak", True) and not self.leak_shown:
                self.leak_shown = True
                self._balloon("漏网", f"{item.get('count') or 0} 条连接没进 TUN")
                return
            if kind == "total" and settings.get("notify_quota", True) and "total" not in self.quota_shown:
                self.quota_shown.add("total")
                self._balloon("日流量", "今天合计已经超过提醒值")
                return
            if kind == "program" and settings.get("notify_quota", True):
                key = str(item.get("process") or "")
                if key and key not in self.quota_shown:
                    self.quota_shown.add(key)
                    self._balloon("日流量", f"{process_label(key)} 今天已经超过提醒值")
                    return
        if not any(a.get("kind") == "leak" for a in alerts):
            self.leak_shown = False

    def start(self, stop: threading.Event, interval: float = 2.0) -> None:
        def loop() -> None:
            while not stop.is_set():
                try:
                    self.once()
                except Exception as exc:  # noqa: BLE001
                    fmlog.log_exception("托盘提示", exc)
                stop.wait(interval)

        threading.Thread(target=loop, daemon=True, name="tray-pulse").start()
