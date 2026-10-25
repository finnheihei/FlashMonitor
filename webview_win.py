"""Windows desktop host: embed the local page in WebView2, no extra Python packages."""

from __future__ import annotations

import ctypes
import json
import os
import platform
import shutil
import struct
import sys
import zipfile
from ctypes import wintypes
from io import BytesIO
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen

WEBVIEW2_NUGET = "1.0.2849.39"
WM_APP = 0x8000
WM_FM_SHOW = WM_APP + 1
WM_FM_QUIT = WM_APP + 2
WM_FM_FALLBACK = WM_APP + 3

IID_IUNKNOWN = "{00000000-0000-0000-C000-000000000046}"
IID_ENV_DONE = "{4E8A3389-C9D8-4BD2-B6B5-124FEE6CC14D}"
IID_CTRL_DONE = "{6C4819F3-C9B7-4260-8127-C9F5BDE7F68C}"
IID_CONTROLLER2 = "{C979903E-D4CA-4228-92EB-47EE3FA96EAB}"
IID_SETTINGS9 = "{0528A73B-E92D-49F4-927A-E547DDDAA37D}"
IID_WEBMSG = "{57213F19-00E6-49FA-8E07-898EA01ECBD2}"

# 页面背景 #f3f5f8、边框 #e2e6ec（COLORREF 是 BGR），和 ui.html 的 --bg / --line 一致
PAGE_BG_RGB = (0xF3, 0xF5, 0xF8)
FRAME_BG = 0x00F8F5F3
FRAME_LINE = 0x00ECE6E2


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __init__(self, text: str = "00000000-0000-0000-0000-000000000000") -> None:
        super().__init__()
        raw = text.strip("{}").replace("-", "")
        self.Data1 = int(raw[0:8], 16)
        self.Data2 = int(raw[8:12], 16)
        self.Data3 = int(raw[12:16], 16)
        for i in range(8):
            self.Data4[i] = int(raw[16 + i * 2 : 18 + i * 2], 16)


def _guid_eq(left: GUID, right: GUID) -> bool:
    return bytes(left) == bytes(right)


def bundle_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def _arch() -> str:
    mach = platform.machine().lower()
    if mach in ("arm64", "aarch64"):
        return "arm64"
    if struct.calcsize("P") == 8:
        return "x64"
    return "x86"


def loader_cache_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        path = Path(local) / "FlashMonitor" / "native"
    else:
        path = bundle_dir() / "native"
    path.mkdir(parents=True, exist_ok=True)
    return path


def find_loader() -> Path | None:
    names = (
        bundle_dir() / "WebView2Loader.dll",
        bundle_dir() / "native" / "WebView2Loader.dll",
        loader_cache_dir() / "WebView2Loader.dll",
    )
    for path in names:
        if path.is_file() and path.stat().st_size > 10_000:
            return path
    return None


def _download_loader(dest: Path) -> Path:
    url = f"https://www.nuget.org/api/v2/package/Microsoft.Web.WebView2/{WEBVIEW2_NUGET}"
    req = Request(url, headers={"User-Agent": "FlashMonitor"})
    with urlopen(req, timeout=40) as resp:
        blob = resp.read()
    arch = _arch()
    wanted = (
        f"runtimes/win-{arch}/native/WebView2Loader.dll",
        f"build/native/{arch}/WebView2Loader.dll",
        "build/native/x64/WebView2Loader.dll" if arch == "x64" else "",
    )
    with zipfile.ZipFile(BytesIO(blob)) as zf:
        names = {n.replace("\\", "/") for n in zf.namelist()}
        pick = next((n for n in wanted if n and n in names), "")
        if not pick:
            raise FileNotFoundError("nupkg 里没有 WebView2Loader.dll")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(zf.read(pick))
    return dest


def ensure_loader() -> Path:
    found = find_loader()
    if found is not None:
        return found
    return _download_loader(loader_cache_dir() / "WebView2Loader.dll")


def prepare_loader_for_build() -> Path:
    src = ensure_loader()
    dest = Path(__file__).resolve().parent / "native" / "WebView2Loader.dll"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() != dest.resolve():
        shutil.copy2(src, dest)
    return dest


def _load_webview2() -> ctypes.WinDLL:
    path = ensure_loader()
    folder = str(path.parent)
    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(folder)
    dll = ctypes.WinDLL(str(path))
    dll.CreateCoreWebView2EnvironmentWithOptions.restype = ctypes.HRESULT
    dll.CreateCoreWebView2EnvironmentWithOptions.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    dll.GetAvailableCoreWebView2BrowserVersionString.restype = ctypes.HRESULT
    dll.GetAvailableCoreWebView2BrowserVersionString.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_wchar_p),
    ]
    return dll


def runtime_installed(dll: ctypes.WinDLL) -> bool:
    ver = ctypes.c_wchar_p()
    hr = dll.GetAvailableCoreWebView2BrowserVersionString(None, ctypes.byref(ver))
    ok = int(hr) >= 0 and bool(ver.value)
    if ver:
        ctypes.windll.ole32.CoTaskMemFree(ver)
    return ok


def com_call(obj: int, index: int, restype, argtypes, *args):
    if not obj:
        raise OSError("null COM pointer")
    vtbl_ptr = ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p))[0]
    fn_ptr = ctypes.cast(vtbl_ptr, ctypes.POINTER(ctypes.c_void_p))[index]
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return proto(fn_ptr)(obj, *args)


def query_interface(obj: int, iid: str) -> int:
    if not obj:
        return 0
    guid = GUID(iid)
    out = ctypes.c_void_p()
    hr = com_call(
        obj,
        0,
        ctypes.HRESULT,
        [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)],
        ctypes.byref(guid),
        ctypes.byref(out),
    )
    if int(hr) < 0 or not out.value:
        return 0
    return int(out.value)


def com_release(obj: int) -> None:
    if not obj:
        return
    try:
        com_call(obj, 2, wintypes.ULONG, [])
    except Exception:
        pass


class ComHandler:
    """IUnknown + Invoke(HRESULT, IUnknown*) completion handler."""

    def __init__(self, iid: str, callback: Callable[[int, int], None]) -> None:
        self._ref = 1
        self._iid = GUID(iid)
        self._unk = GUID(IID_IUNKNOWN)
        self._callback = callback
        HRESULT = ctypes.HRESULT
        QI = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(self._qi)
        AddRef = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(self._addref)
        Release = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(self._release)
        Invoke = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, HRESULT, ctypes.c_void_p)(self._invoke)

        class Vtbl(ctypes.Structure):
            _fields_ = [
                ("QueryInterface", type(QI)),
                ("AddRef", type(AddRef)),
                ("Release", type(Release)),
                ("Invoke", type(Invoke)),
            ]

        class Obj(ctypes.Structure):
            _fields_ = [("lpVtbl", ctypes.POINTER(Vtbl))]

        self._vtbl = Vtbl(QI, AddRef, Release, Invoke)
        self._obj = Obj(ctypes.pointer(self._vtbl))
        self._keep = (QI, AddRef, Release, Invoke, self._vtbl, self._obj)

    @property
    def pointer(self) -> int:
        return ctypes.addressof(self._obj)

    def _qi(self, this, riid, ppv):
        iid = riid.contents
        if _guid_eq(iid, self._unk) or _guid_eq(iid, self._iid):
            ppv[0] = this
            self._addref(this)
            return 0
        ppv[0] = None
        return 0x80004002

    def _addref(self, _this):
        self._ref += 1
        return self._ref

    def _release(self, _this):
        self._ref = max(0, self._ref - 1)
        return self._ref

    def _invoke(self, _this, hr, obj):
        try:
            self._callback(int(hr), int(obj or 0))
        except Exception:
            return 0x80004005
        return 0


class MessageHandler:
    """ICoreWebView2WebMessageReceivedEventHandler."""

    def __init__(self, callback: Callable[[str], None]) -> None:
        self._ref = 1
        self._iid = GUID(IID_WEBMSG)
        self._unk = GUID(IID_IUNKNOWN)
        self._callback = callback
        HRESULT = ctypes.HRESULT
        QI = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(self._qi)
        AddRef = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(self._addref)
        Release = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(self._release)
        Invoke = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(self._invoke)

        class Vtbl(ctypes.Structure):
            _fields_ = [
                ("QueryInterface", type(QI)),
                ("AddRef", type(AddRef)),
                ("Release", type(Release)),
                ("Invoke", type(Invoke)),
            ]

        class Obj(ctypes.Structure):
            _fields_ = [("lpVtbl", ctypes.POINTER(Vtbl))]

        self._vtbl = Vtbl(QI, AddRef, Release, Invoke)
        self._obj = Obj(ctypes.pointer(self._vtbl))
        self._keep = (QI, AddRef, Release, Invoke, self._vtbl, self._obj)

    @property
    def pointer(self) -> int:
        return ctypes.addressof(self._obj)

    def _qi(self, this, riid, ppv):
        iid = riid.contents
        if _guid_eq(iid, self._unk) or _guid_eq(iid, self._iid):
            ppv[0] = this
            self._addref(this)
            return 0
        ppv[0] = None
        return 0x80004002

    def _addref(self, _this):
        self._ref += 1
        return self._ref

    def _release(self, _this):
        self._ref = max(0, self._ref - 1)
        return self._ref

    def _invoke(self, _this, _sender, args):
        if not args:
            return 0
        raw = ctypes.c_wchar_p()
        try:
            hr = com_call(
                int(args),
                4,
                ctypes.HRESULT,
                [ctypes.POINTER(ctypes.c_wchar_p)],
                ctypes.byref(raw),
            )
        except Exception:
            return 0
        if int(hr) < 0 or not raw.value:
            return 0
        try:
            self._callback(raw.value)
        except Exception:
            return 0x80004005
        finally:
            ctypes.windll.ole32.CoTaskMemFree(raw)
        return 0


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MINMAXINFO(ctypes.Structure):
    _fields_ = [
        ("ptReserved", POINT),
        ("ptMaxSize", POINT),
        ("ptMaxPosition", POINT),
        ("ptMinTrackSize", POINT),
        ("ptMaxTrackSize", POINT),
    ]


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.UINT),
        ("flags", wintypes.UINT),
        ("showCmd", wintypes.UINT),
        ("ptMinPosition", POINT),
        ("ptMaxPosition", POINT),
        ("rcNormalPosition", wintypes.RECT),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def fit_window_box(w, h, x, y, work):
    """Keep the complete restored window in a real monitor's work area."""
    left, top, right, bottom = work
    w, h = min(w, right-left), min(h, bottom-top)
    return w, h, max(left, min(x, right-w)), max(top, min(y, bottom-h))


class MARGINS(ctypes.Structure):
    _fields_ = [
        ("cxLeftWidth", ctypes.c_int),
        ("cxRightWidth", ctypes.c_int),
        ("cyTopHeight", ctypes.c_int),
        ("cyBottomHeight", ctypes.c_int),
    ]


class NCCALCSIZE_PARAMS(ctypes.Structure):
    _fields_ = [
        ("rgrc", wintypes.RECT * 3),
        ("lppos", ctypes.c_void_p),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


class EventRegistrationToken(ctypes.Structure):
    _fields_ = [("value", ctypes.c_int64)]


class COREWEBVIEW2_COLOR(ctypes.Structure):
    _fields_ = [
        ("A", ctypes.c_ubyte),
        ("R", ctypes.c_ubyte),
        ("G", ctypes.c_ubyte),
        ("B", ctypes.c_ubyte),
    ]


def prepare_process() -> None:
    os.environ.setdefault("WEBVIEW2_DEFAULT_BACKGROUND_COLOR", "FF%02X%02X%02X" % PAGE_BG_RGB)
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("finnheihei.FlashMonitor")
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


def dpi_scale() -> float:
    try:
        dpi = int(ctypes.windll.user32.GetDpiForSystem() or 0)
    except Exception:
        dpi = 0
    if dpi <= 0:
        try:
            hdc = ctypes.windll.user32.GetDC(None)
            dpi = int(ctypes.windll.gdi32.GetDeviceCaps(hdc, 88) or 96)
            ctypes.windll.user32.ReleaseDC(None, hdc)
        except Exception:
            dpi = 96
    return max(1.0, dpi / 96.0)


LRESULT = ctypes.c_ssize_t
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


class FlashDesktop:
    """Own Win32 window hosting WebView2. Edge --app is only a fallback."""

    def __init__(
        self,
        *,
        title: str,
        url: str,
        icon: Path | None,
        userdata: Path,
        placement_file: Path,
        fallback: Callable[[], Any] | None = None,
        should_hide: Callable[[], bool] | None = None,
        on_first_hide: Callable[[], bool] | None = None,
        on_quit: Callable[[], bool | None] | None = None,
        window_class: str = "FlashMonitorWnd",
    ) -> None:
        self.title = title
        self.url = url
        self.icon = icon
        self.userdata = userdata
        self.placement_file = placement_file
        self.fallback = fallback
        self.should_hide = should_hide
        self.on_first_hide = on_first_hide
        self.on_quit = on_quit
        self.hwnd = 0
        self.controller = 0
        self.webview = 0
        self.environment = 0
        self.shell = None
        self._using_webview = False
        self._hidden = False
        self._page_visible = True
        self._first_hide = False
        self._dll = None
        self._handlers: list[ComHandler] = []
        self._wndproc = None
        self._class = window_class
        self._class_buf = ctypes.create_unicode_buffer(self._class)
        self._icon_small = 0
        self._icon_big = 0
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)

    def close(self) -> None:
        self.request_quit()

    def request_quit(self) -> None:
        if self.hwnd:
            ctypes.windll.user32.PostMessageW(self.hwnd, WM_FM_QUIT, 0, 0)
        else:
            self._quit()

    def show(self) -> None:
        if self.hwnd:
            ctypes.windll.user32.PostMessageW(self.hwnd, WM_FM_SHOW, 0, 0)
        elif self.shell is not None:
            self.shell.show()

    def hide(self) -> None:
        self._hide_window()

    def run(self, minimized: bool = False) -> None:
        self._init_com()
        if not self._create_window():
            if self.fallback:
                self.shell = self.fallback()
                self.shell.show()
            self._pump()
            return
        if not self._start_webview():
            self._start_fallback()
        else:
            self._user32.SetTimer(self.hwnd, 2, 8000, None)
        if minimized:
            self._hide_window()
        else:
            self._show_window()
        self._pump()

    def _init_com(self) -> None:
        prepare_process()
        ctypes.windll.ole32.CoInitializeEx(None, 0x2)

    def _create_window(self) -> bool:
        user32 = self._user32
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = LRESULT
        user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
        user32.LoadCursorW.restype = wintypes.HANDLE

        @WNDPROC
        def wndproc(hwnd, msg, wparam, lparam):
            return self._wnd_proc(int(hwnd), int(msg), int(wparam), int(lparam))

        self._wndproc = wndproc
        hinst = kernel32.GetModuleHandleW(None)
        self._load_icons()
        CS_HREDRAW, CS_VREDRAW = 0x0002, 0x0001
        wc = WNDCLASSW()
        wc.style = CS_HREDRAW | CS_VREDRAW
        wc.lpfnWndProc = wndproc
        wc.hInstance = hinst
        wc.hIcon = self._icon_big or self._icon_small
        wc.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(32512))
        wc.hbrBackground = ctypes.windll.gdi32.CreateSolidBrush(FRAME_BG)
        wc.lpszClassName = self._class
        atom = user32.RegisterClassW(ctypes.byref(wc))
        if not atom and ctypes.get_last_error() not in (0, 1410):
            return False
        w, h, x, y = self._restore_box()
        WS_OVERLAPPEDWINDOW = 0x00CF0000
        hwnd = user32.CreateWindowExW(
            0,
            self._class,
            self.title,
            WS_OVERLAPPEDWINDOW,
            x,
            y,
            w,
            h,
            None,
            None,
            hinst,
            None,
        )
        if not hwnd:
            return False
        self.hwnd = int(hwnd)
        if self._icon_small:
            user32.SendMessageW(hwnd, 0x0080, 0, self._icon_small)
        if self._icon_big:
            user32.SendMessageW(hwnd, 0x0080, 1, self._icon_big)
        self._apply_frame()
        return True

    def _load_icons(self) -> None:
        if self.icon is None or not self.icon.is_file():
            return
        user32 = ctypes.windll.user32
        user32.LoadImageW.argtypes = [
            wintypes.HINSTANCE,
            wintypes.LPCWSTR,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        user32.LoadImageW.restype = wintypes.HANDLE
        IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
        self._icon_small = user32.LoadImageW(None, str(self.icon), IMAGE_ICON, 16, 16, LR_LOADFROMFILE) or 0
        self._icon_big = user32.LoadImageW(None, str(self.icon), IMAGE_ICON, 32, 32, LR_LOADFROMFILE) or 0

    def _restore_box(self) -> tuple[int, int, int, int]:
        scale = dpi_scale()
        CW = -2147483648
        w, h, x, y = int(1120 * scale), int(820 * scale), CW, CW
        try:
            data = json.loads(self.placement_file.read_text(encoding="utf-8"))
            w = max(int(960 * scale), int(data.get("w") or w))
            h = max(int(640 * scale), int(data.get("h") or h))
            x = int(data["x"])
            y = int(data["y"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            sw = ctypes.windll.user32.GetSystemMetrics(0)
            sh = ctypes.windll.user32.GetSystemMetrics(1)
            if sw > 0 and sh > 0:
                x = max(0, (sw - w) // 2)
                y = max(0, (sh - h) // 2)
        user32 = ctypes.windll.user32
        rect = wintypes.RECT(x, y, x+w, y+h)
        user32.MonitorFromRect.argtypes = [ctypes.POINTER(wintypes.RECT), wintypes.DWORD]
        user32.MonitorFromRect.restype = wintypes.HANDLE
        user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
        monitor = user32.MonitorFromRect(ctypes.byref(rect), 2)  # nearest monitor
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(info)
        if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            r = info.rcWork
            return fit_window_box(w, h, x, y, (r.left, r.top, r.right, r.bottom))
        return w, h, x, y

    def _save_box(self) -> None:
        if not self.hwnd:
            return
        # Minimized HWNDs report (-32000, -32000), not their normal position.
        # Preserve the last normal bounds for both minimized and maximized windows.
        if ctypes.windll.user32.IsIconic(self.hwnd) or ctypes.windll.user32.IsZoomed(self.hwnd):
            return
        rect = wintypes.RECT()
        if not ctypes.windll.user32.GetWindowRect(self.hwnd, ctypes.byref(rect)):
            return
        payload = {
            "x": int(rect.left),
            "y": int(rect.top),
            "w": int(rect.right - rect.left),
            "h": int(rect.bottom - rect.top),
        }
        try:
            self.placement_file.parent.mkdir(parents=True, exist_ok=True)
            self.placement_file.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            pass

    def _start_webview(self) -> bool:
        try:
            dll = _load_webview2()
        except Exception as exc:
            print(f"WebView2 loader: {exc}", file=sys.stderr)
            return False
        self._dll = dll
        if not runtime_installed(dll):
            print("WebView2 runtime not installed", file=sys.stderr)
            return False
        self.userdata.mkdir(parents=True, exist_ok=True)
        env_handler = ComHandler(IID_ENV_DONE, self._on_env)
        ctrl_handler = ComHandler(IID_CTRL_DONE, self._on_controller)
        self._handlers.extend((env_handler, ctrl_handler))
        hr = dll.CreateCoreWebView2EnvironmentWithOptions(
            None,
            str(self.userdata),
            None,
            env_handler.pointer,
        )
        if int(hr) < 0:
            print(f"CreateCoreWebView2EnvironmentWithOptions failed: 0x{int(hr) & 0xFFFFFFFF:08X}", file=sys.stderr)
            return False
        return True

    def _on_env(self, hr: int, env: int) -> None:
        if hr < 0 or not env or not self.hwnd:
            print(f"WebView2 environment failed: 0x{hr & 0xFFFFFFFF:08X}", file=sys.stderr)
            if self.hwnd:
                self._user32.PostMessageW(self.hwnd, WM_FM_FALLBACK, 0, 0)
            return
        self.environment = env
        com_call(env, 1, wintypes.ULONG, [])  # AddRef
        ctrl = self._handlers[1]
        result = com_call(
            env,
            3,
            ctypes.HRESULT,
            [wintypes.HWND, ctypes.c_void_p],
            self.hwnd,
            ctrl.pointer,
        )
        if int(result) < 0:
            print(f"CreateCoreWebView2Controller failed: 0x{int(result) & 0xFFFFFFFF:08X}", file=sys.stderr)
            self._user32.PostMessageW(self.hwnd, WM_FM_FALLBACK, 0, 0)

    def _on_controller(self, hr: int, controller: int) -> None:
        if hr < 0 or not controller:
            print(f"WebView2 controller failed: 0x{hr & 0xFFFFFFFF:08X}", file=sys.stderr)
            if self.hwnd:
                self._user32.PostMessageW(self.hwnd, WM_FM_FALLBACK, 0, 0)
            return
        try:
            self.controller = controller
            com_call(controller, 1, wintypes.ULONG, [])
            wv = ctypes.c_void_p()
            com_call(controller, 25, ctypes.HRESULT, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(wv))
            self.webview = int(wv.value or 0)
            if not self.webview:
                raise OSError("get_CoreWebView2 returned null")
            self._using_webview = True
            try:
                self._user32.KillTimer(self.hwnd, 2)
            except Exception:
                pass
            try:
                self._tune_settings()
            except Exception:
                pass
            try:
                self._bind_messages()
            except Exception:
                pass
            self._resize()
            if self._hidden or bool(self._user32.IsIconic(self.hwnd)):
                # 启动时直接藏到托盘：_hide_window 那会儿 controller 还没建好
                self._page_visible = True
                self._set_page_visible(False)
            url = self.url
            url += ("&" if "?" in url else "?") + "desk=1"
            com_call(self.webview, 5, ctypes.HRESULT, [wintypes.LPCWSTR], url)
        except Exception as exc:
            print(f"WebView2 setup: {exc}", file=sys.stderr)
            if self.hwnd:
                self._user32.PostMessageW(self.hwnd, WM_FM_FALLBACK, 0, 0)

    def _tune_settings(self) -> None:
        settings = ctypes.c_void_p()
        if com_call(self.webview, 3, ctypes.HRESULT, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(settings)) < 0:
            return
        ptr = int(settings.value or 0)
        if not ptr:
            return
        # put_IsWebMessageEnabled = 6, put_IsStatusBarEnabled = 10, put_AreDevToolsEnabled = 12
        com_call(ptr, 6, ctypes.HRESULT, [wintypes.BOOL], True)
        com_call(ptr, 10, ctypes.HRESULT, [wintypes.BOOL], False)
        com_call(ptr, 12, ctypes.HRESULT, [wintypes.BOOL], False)
        s9 = query_interface(ptr, IID_SETTINGS9)
        if s9:
            try:
                com_call(s9, 38, ctypes.HRESULT, [wintypes.BOOL], True)
            except Exception:
                pass
            com_release(s9)
        com_release(ptr)
        c2 = query_interface(self.controller, IID_CONTROLLER2)
        if c2:
            color = COREWEBVIEW2_COLOR(255, *PAGE_BG_RGB)
            try:
                com_call(c2, 27, ctypes.HRESULT, [COREWEBVIEW2_COLOR], color)
            except Exception:
                pass
            com_release(c2)

    def _bind_messages(self) -> None:
        if not self.webview:
            return
        handler = MessageHandler(self._on_web_message)
        self._handlers.append(handler)
        token = EventRegistrationToken()
        com_call(
            self.webview,
            34,
            ctypes.HRESULT,
            [ctypes.c_void_p, ctypes.POINTER(EventRegistrationToken)],
            handler.pointer,
            ctypes.byref(token),
        )

    def _on_web_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"cmd": raw}
        cmd = str(data.get("cmd") or "")
        user32 = ctypes.windll.user32
        if cmd == "ready":
            self._sync_chrome()
        elif cmd == "min" and self.hwnd:
            user32.ShowWindow(self.hwnd, 6)
        elif cmd == "max":
            self._toggle_max()
        elif cmd == "close" and self.hwnd:
            user32.PostMessageW(self.hwnd, 0x0010, 0, 0)
        elif cmd == "drag" and self.hwnd:
            user32.ReleaseCapture()
            user32.SendMessageW(self.hwnd, 0x00A1, 2, 0)

    def _toggle_max(self) -> None:
        if not self.hwnd:
            return
        user32 = ctypes.windll.user32
        if user32.IsZoomed(self.hwnd):
            user32.ShowWindow(self.hwnd, 9)
        else:
            user32.ShowWindow(self.hwnd, 3)

    def _post_json(self, payload: dict[str, Any]) -> None:
        if not self.webview:
            return
        try:
            com_call(self.webview, 32, ctypes.HRESULT, [wintypes.LPCWSTR], json.dumps(payload))
        except Exception:
            pass

    def _set_page_visible(self, visible: bool) -> None:
        """藏到托盘 / 最小化时把 WebView 设为不可见（省 CPU），并告诉页面停掉轮询。"""
        if self.controller:
            try:
                com_call(self.controller, 4, ctypes.HRESULT, [wintypes.BOOL], bool(visible))
            except Exception:
                pass
        if self._page_visible is not visible:
            self._page_visible = visible
            self._post_json({"visible": visible})

    def _sync_chrome(self) -> None:
        if not self.webview or not self.hwnd:
            return
        mx = bool(ctypes.windll.user32.IsZoomed(self.hwnd))
        self._post_json({"maximized": mx, "visible": bool(self._page_visible)})

    def _apply_frame(self) -> None:
        if not self.hwnd:
            return
        dwmapi = ctypes.WinDLL("dwmapi")
        hwnd = self.hwnd
        try:
            pref = ctypes.c_int(2)
            dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), 4)
        except Exception:
            pass
        try:
            border = ctypes.c_uint(FRAME_LINE)
            dwmapi.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(border), 4)
            caption = ctypes.c_uint(FRAME_BG)
            dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption), 4)
        except Exception:
            pass
        try:
            margins = MARGINS(0, 0, 1, 0)
            dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins))
        except Exception:
            pass

    def _frame_px(self, hwnd: int) -> int:
        user32 = self._user32
        try:
            user32.GetDpiForWindow.argtypes = [wintypes.HWND]
            user32.GetDpiForWindow.restype = wintypes.UINT
            dpi = int(user32.GetDpiForWindow(hwnd) or 96)
            user32.GetSystemMetricsForDpi.argtypes = [ctypes.c_int, wintypes.UINT]
            user32.GetSystemMetricsForDpi.restype = ctypes.c_int
            return max(1, int(user32.GetSystemMetricsForDpi(32, dpi) + user32.GetSystemMetricsForDpi(92, dpi)))
        except Exception:
            return max(8, int(8 * dpi_scale()))

    def _nccalcsize(self, hwnd: int, wparam: int, lparam: int) -> int:
        frame = self._frame_px(hwnd)
        if wparam:
            params = ctypes.cast(lparam, ctypes.POINTER(NCCALCSIZE_PARAMS)).contents
            r = params.rgrc[0]
            if self._user32.IsZoomed(hwnd):
                mi = MONITORINFO()
                mi.cbSize = ctypes.sizeof(MONITORINFO)
                try:
                    user32 = self._user32
                    user32.MonitorFromWindow.restype = wintypes.HANDLE
                    user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
                    user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
                    user32.GetMonitorInfoW.restype = wintypes.BOOL
                    mon = user32.MonitorFromWindow(hwnd, 2)
                    if mon and user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
                        r.left = mi.rcWork.left
                        r.top = mi.rcWork.top
                        r.right = mi.rcWork.right
                        r.bottom = mi.rcWork.bottom
                        return 0
                except Exception:
                    pass
            r.left += frame
            r.right -= frame
            r.bottom -= frame
            return 0
        rect = ctypes.cast(lparam, ctypes.POINTER(wintypes.RECT)).contents
        rect.left += frame
        rect.right -= frame
        rect.bottom -= frame
        return 0

    def _resize(self) -> None:
        if not self.controller or not self.hwnd:
            return
        rect = wintypes.RECT()
        ctypes.windll.user32.GetClientRect(self.hwnd, ctypes.byref(rect))
        com_call(self.controller, 6, ctypes.HRESULT, [ctypes.POINTER(wintypes.RECT)], ctypes.byref(rect))

    def _start_fallback(self) -> None:
        if self._using_webview or self.shell is not None or not self.fallback:
            return
        if self.hwnd:
            self._user32.ShowWindow(self.hwnd, 0)
            self._user32.SetTimer(self.hwnd, 1, 600, None)
        self.shell = self.fallback()
        try:
            self.shell.show()
        except Exception:
            pass

    def _show_window(self) -> None:
        self._hidden = False
        if self._using_webview or self.shell is None:
            ctypes.windll.user32.ShowWindow(self.hwnd, 1)  # SW_SHOWNORMAL
            ctypes.windll.user32.SetForegroundWindow(self.hwnd)
            if self.controller:
                self._set_page_visible(True)
                self._resize()
            return
        self.shell.show()

    def _hide_window(self) -> None:
        self._save_box()
        self._hidden = True
        if self.hwnd:
            ctypes.windll.user32.ShowWindow(self.hwnd, 0)
        self._set_page_visible(False)
        if self.shell is not None:
            import ctypes as ct

            if getattr(self.shell, "hwnd", 0):
                try:
                    ct.windll.user32.ShowWindow(self.shell.hwnd, 0)
                except Exception:
                    pass
        if not self._first_hide:
            if self.on_first_hide:
                try:
                    self._first_hide = self.on_first_hide() is True
                except Exception:
                    pass

    def _teardown_webview(self) -> None:
        if self.controller:
            try:
                com_call(self.controller, 24, ctypes.HRESULT, [])
            except Exception:
                pass
            try:
                com_call(self.controller, 2, wintypes.ULONG, [])
            except Exception:
                pass
        if self.webview:
            try:
                com_call(self.webview, 2, wintypes.ULONG, [])
            except Exception:
                pass
        if self.environment:
            try:
                com_call(self.environment, 2, wintypes.ULONG, [])
            except Exception:
                pass
        self.controller = 0
        self.webview = 0
        self.environment = 0
        self._using_webview = False

    def _quit(self) -> None:
        if self.on_quit:
            try:
                if self.on_quit() is False:
                    return
            except Exception as exc:
                if sys.stderr is not None:
                    print('退出未完成：'+str(exc), file=sys.stderr)
                return
        self._save_box()
        self._teardown_webview()
        if self.shell is not None:
            try:
                self.shell.close()
            except Exception:
                pass
            self.shell = None
        if self.hwnd:
            ctypes.windll.user32.DestroyWindow(self.hwnd)
            self.hwnd = 0
        ctypes.windll.user32.PostQuitMessage(0)

    def _wnd_proc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        user32 = self._user32
        WM_SIZE = 0x0005
        WM_MOVE = 0x0003
        WM_CLOSE = 0x0010
        WM_DESTROY = 0x0002
        WM_GETMINMAXINFO = 0x0024
        WM_WINDOWPOSCHANGED = 0x0047
        WM_NCCALCSIZE = 0x0083
        WM_ACTIVATE = 0x0006
        WM_NCACTIVATE = 0x0086
        if msg == WM_NCCALCSIZE:
            return self._nccalcsize(hwnd, wparam, lparam)
        if msg == WM_NCACTIVATE:
            return int(user32.DefWindowProcW(hwnd, msg, wparam, -1))
        if msg == WM_ACTIVATE:
            self._apply_frame()
        if msg == WM_SIZE or msg == WM_WINDOWPOSCHANGED:
            self._resize()
            if self.controller:
                try:
                    com_call(self.controller, 23, ctypes.HRESULT, [])
                except Exception:
                    pass
            if msg == WM_SIZE:
                if wparam == 1:  # SIZE_MINIMIZED
                    self._set_page_visible(False)
                elif wparam in (0, 2) and not self._hidden:  # SIZE_RESTORED / SIZE_MAXIMIZED
                    self._set_page_visible(True)
                self._sync_chrome()
        elif msg == WM_MOVE:
            if self.controller:
                try:
                    com_call(self.controller, 23, ctypes.HRESULT, [])
                except Exception:
                    pass
        elif msg == WM_GETMINMAXINFO:
            info = ctypes.cast(lparam, ctypes.POINTER(MINMAXINFO)).contents
            scale = dpi_scale()
            info.ptMinTrackSize.x = int(960 * scale)
            info.ptMinTrackSize.y = int(640 * scale)
            return 0
        elif msg == WM_CLOSE:
            hide = True if self.should_hide is None else bool(self.should_hide())
            if hide:
                self._hide_window()
                return 0
            self._quit()
            return 0
        elif msg == WM_DESTROY:
            ctypes.windll.user32.PostQuitMessage(0)
            return 0
        elif msg == WM_FM_SHOW:
            self._show_window()
            return 0
        elif msg == WM_FM_QUIT:
            self._quit()
            return 0
        elif msg == WM_FM_FALLBACK:
            self._start_fallback()
            return 0
        elif msg == 0x0113:  # WM_TIMER
            if wparam == 2 and not self._using_webview:
                self._user32.KillTimer(hwnd, 2)
                self._start_fallback()
            elif wparam == 1 and self.shell is not None and not self._using_webview:
                alive = True
                try:
                    alive = bool(self.shell.alive())
                except Exception:
                    alive = False
                if not alive:
                    hide = True if self.should_hide is None else bool(self.should_hide())
                    if hide:
                        if not self._hidden:
                            self._hide_window()
                    else:
                        self._quit()
            return 0
        return int(user32.DefWindowProcW(hwnd, msg, wparam, lparam))

    def _pump(self) -> None:
        user32 = self._user32
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


def webview_user_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        path = Path(local) / "FlashMonitor" / "webview2"
    else:
        path = Path.cwd() / "data" / "webview2"
    path.mkdir(parents=True, exist_ok=True)
    return path


def placement_path() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "FlashMonitor" / "window.json"
    return Path.cwd() / "data" / "window.json"
