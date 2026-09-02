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


def prepare_process() -> None:
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
        on_first_hide: Callable[[], None] | None = None,
        on_quit: Callable[[], None] | None = None,
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
        self._first_hide = False
        self._dll = None
        self._handlers: list[ComHandler] = []
        self._wndproc = None
        self._class = "FlashMonitorWnd"
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
        wc.hbrBackground = ctypes.windll.gdi32.CreateSolidBrush(0x00F3F3F3)
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
            w = max(int(800 * scale), int(data.get("w") or w))
            h = max(int(560 * scale), int(data.get("h") or h))
            x = int(data["x"])
            y = int(data["y"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            sw = ctypes.windll.user32.GetSystemMetrics(0)
            sh = ctypes.windll.user32.GetSystemMetrics(1)
            if sw > 0 and sh > 0:
                x = max(0, (sw - w) // 2)
                y = max(0, (sh - h) // 2)
        return w, h, x, y

    def _save_box(self) -> None:
        if not self.hwnd:
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
            self._resize()
            com_call(self.webview, 5, ctypes.HRESULT, [wintypes.LPCWSTR], self.url)
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
        # put_IsStatusBarEnabled = 10, put_AreDevToolsEnabled = 12
        com_call(ptr, 10, ctypes.HRESULT, [wintypes.BOOL], False)
        com_call(ptr, 12, ctypes.HRESULT, [wintypes.BOOL], False)
        com_call(ptr, 2, wintypes.ULONG, [])

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
                com_call(self.controller, 4, ctypes.HRESULT, [wintypes.BOOL], True)
                self._resize()
            return
        self.shell.show()

    def _hide_window(self) -> None:
        self._save_box()
        self._hidden = True
        if self.hwnd:
            ctypes.windll.user32.ShowWindow(self.hwnd, 0)
        if self.controller:
            try:
                com_call(self.controller, 4, ctypes.HRESULT, [wintypes.BOOL], False)
            except Exception:
                pass
        if self.shell is not None:
            import ctypes as ct

            if getattr(self.shell, "hwnd", 0):
                try:
                    ct.windll.user32.ShowWindow(self.shell.hwnd, 0)
                except Exception:
                    pass
        if not self._first_hide:
            self._first_hide = True
            if self.on_first_hide:
                try:
                    self.on_first_hide()
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
        self._save_box()
        self._teardown_webview()
        if self.shell is not None:
            try:
                self.shell.close()
            except Exception:
                pass
            self.shell = None
        if self.on_quit:
            try:
                self.on_quit()
            except Exception:
                pass
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
        if msg == WM_SIZE or msg == WM_WINDOWPOSCHANGED:
            self._resize()
            if self.controller:
                try:
                    com_call(self.controller, 23, ctypes.HRESULT, [])
                except Exception:
                    pass
        elif msg == WM_MOVE:
            if self.controller:
                try:
                    com_call(self.controller, 23, ctypes.HRESULT, [])
                except Exception:
                    pass
        elif msg == WM_GETMINMAXINFO:
            info = ctypes.cast(lparam, ctypes.POINTER(MINMAXINFO)).contents
            scale = dpi_scale()
            info.ptMinTrackSize.x = int(800 * scale)
            info.ptMinTrackSize.y = int(560 * scale)
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
                        if not self._first_hide:
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
