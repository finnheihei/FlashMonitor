"""程序图标（从 exe 取出存成 PNG）和文件描述（窗口里显示的程序名）。

按本地路径和资源版本缓存；有界后台队列提取，HTTP 请求只取已有文件。
"""

from __future__ import annotations

import ctypes
import hashlib
import functools
import queue
import re
import sys
import threading
import time
import unicodedata
from ctypes import wintypes
from pathlib import Path, PureWindowsPath
from collections import OrderedDict

from . import log as fmlog

ICON_RETRY = 30.0
ICON_WORKERS = 2
ICON_PENDING = 64
ICON_STATES = 512
MIN_PNG_BYTES = 32


def generic_icon_svg(name: str) -> bytes:
    """Six stable neutral marks; names are hashed and never embedded in SVG markup."""
    variants = (
        ('#e8edf8', '#667baf', '<rect x="9" y="9" width="14" height="14" rx="4"/><path d="M13 13h6v6h-6z"/>'),
        ('#e9f0ef', '#5a827b', '<path d="m16 7 9 9-9 9-9-9zM12 16h8M16 12v8"/>'),
        ('#f0ebf7', '#8974a6', '<rect x="8" y="8" width="6" height="6" rx="2"/><rect x="18" y="8" width="6" height="6" rx="2"/><rect x="8" y="18" width="6" height="6" rx="2"/><rect x="18" y="18" width="6" height="6" rx="2"/>'),
        ('#f5eee4', '#a48a63', '<path d="m16 7 9 5v8l-9 5-9-5v-8zM7 12l9 5 9-5M16 17v8"/>'),
        ('#e9eef2', '#718697', '<circle cx="16" cy="16" r="9"/><path d="m12 19 4-9 4 9M13 17h6"/>'),
        ('#f4e9ed', '#a17588', '<path d="M8 12h16M8 20h16"/><rect x="10" y="9" width="4" height="6" rx="2"/><rect x="18" y="17" width="4" height="6" rx="2"/>'),
    )
    value = 0
    for char in (name or 'app').strip().lower().removesuffix('.exe'):
        value = (value * 31 + ord(char)) & 0xffffffff
    bg, ink, mark = variants[value % len(variants)]
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width="32" height="32" aria-hidden="true">'
            f'<rect width="32" height="32" rx="8" fill="{bg}"/><g fill="none" stroke="{ink}" '
            f'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">{mark}</g></svg>').encode('utf-8')


def file_description(path: str) -> str:
    """exe 版本信息里的 FileDescription（例如 "Google Chrome"），读不到返回空串。"""
    if sys.platform != "win32" or not path or not Path(path).is_file():
        return ""
    try:
        version = ctypes.WinDLL("version")
        version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        version.GetFileVersionInfoW.restype = wintypes.BOOL
        version.VerQueryValueW.argtypes = [
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.UINT),
        ]
        version.VerQueryValueW.restype = wintypes.BOOL
        dummy = wintypes.DWORD()
        size = version.GetFileVersionInfoSizeW(path, ctypes.byref(dummy))
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(path, 0, size, buf):
            return ""
        lp = ctypes.c_void_p()
        llen = wintypes.UINT()
        if (
            not version.VerQueryValueW(buf, r"\VarFileInfo\Translation", ctypes.byref(lp), ctypes.byref(llen))
            or not lp.value
            or llen.value < 4
        ):
            return ""
        lang = ctypes.cast(lp, ctypes.POINTER(ctypes.c_ushort))
        query = rf"\StringFileInfo\{lang[0]:04X}{lang[1]:04X}\FileDescription"
        textp = ctypes.c_void_p()
        tlen = wintypes.UINT()
        if not version.VerQueryValueW(buf, query, ctypes.byref(textp), ctypes.byref(tlen)) or not textp.value:
            return ""
        return ctypes.wstring_at(textp.value).strip()[:80]
    except (OSError, ValueError, AttributeError):
        return ""


class _GdiplusStartupInput(ctypes.Structure):
    _fields_ = [
        ("GdiplusVersion", ctypes.c_uint32),
        ("DebugEventCallback", ctypes.c_void_p),
        ("SuppressBackgroundThread", ctypes.c_int),
        ("SuppressExternalCodecs", ctypes.c_int),
    ]


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def extract_icon_png(src: str, dest: Path) -> bool:
    """把 exe 的第一个图标存成 PNG。成功返回 True。"""
    if sys.platform != "win32" or not src or (src != "@generic" and (not local_executable(src) or not Path(src).is_file())):
        return False
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        gdiplus = ctypes.WinDLL("gdiplus")
        shell32.ExtractIconExW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.c_int,
            ctypes.POINTER(wintypes.HANDLE),
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.UINT,
        ]
        shell32.ExtractIconExW.restype = wintypes.UINT
        user32.DestroyIcon.argtypes = [wintypes.HANDLE]
        user32.DestroyIcon.restype = wintypes.BOOL
        ulong_ptr = ctypes.c_size_t
        gdiplus.GdiplusStartup.argtypes = [ctypes.POINTER(ulong_ptr), ctypes.POINTER(_GdiplusStartupInput), ctypes.c_void_p]
        gdiplus.GdiplusShutdown.argtypes = [ulong_ptr]
        gdiplus.GdipCreateBitmapFromHICON.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
        gdiplus.GdipSaveImageToFile.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(_GUID), ctypes.c_void_p]
        gdiplus.GdipDisposeImage.argtypes = [ctypes.c_void_p]

        large = wintypes.HANDLE()
        small = wintypes.HANDLE()
        if src == "@generic":
            class StockInfo(ctypes.Structure):
                _fields_ = [("size", wintypes.DWORD), ("icon", wintypes.HANDLE), ("index", ctypes.c_int),
                            ("location", wintypes.WCHAR * 260), ("resource", ctypes.c_int)]
            stock = StockInfo(); stock.size = ctypes.sizeof(stock)
            shell32.SHGetStockIconInfo.argtypes = [ctypes.c_int, wintypes.UINT, ctypes.POINTER(StockInfo)]
            shell32.SHGetStockIconInfo.restype = ctypes.c_long
            n = shell32.SHGetStockIconInfo(2, 0x100, ctypes.byref(stock)) == 0
            large.value = stock.icon
        else:
            n = shell32.ExtractIconExW(src, 0, ctypes.byref(large), ctypes.byref(small), 1)
            # SHGetFileInfoW supplies a stock Windows icon when the exe has none.
            # Keep that absence observable so the UI can show its stable fallback.
        icons = [h for h in {large.value, small.value} if h]
        hicon = large.value or small.value
        try:
            if not n or not hicon:
                return False
            token = ulong_ptr()
            inp = _GdiplusStartupInput(1, None, False, False)
            if int(gdiplus.GdiplusStartup(ctypes.byref(token), ctypes.byref(inp), None)) != 0:
                return False
            try:
                bitmap = ctypes.c_void_p()
                if int(gdiplus.GdipCreateBitmapFromHICON(hicon, ctypes.byref(bitmap))) != 0 or not bitmap.value:
                    return False
                try:
                    png = _GUID(0x557CF406, 0x1A04, 0x11D3, (ctypes.c_ubyte * 8)(0x9A, 0x73, 0x00, 0x00, 0xF8, 0x1E, 0xF3, 0x2E))
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if int(gdiplus.GdipSaveImageToFile(bitmap, str(dest), ctypes.byref(png), None)) != 0:
                        return False
                finally:
                    gdiplus.GdipDisposeImage(bitmap)
            finally:
                gdiplus.GdiplusShutdown(token.value)
        finally:
            for h in icons:
                user32.DestroyIcon(h)
        return dest.is_file() and dest.stat().st_size > MIN_PNG_BYTES
    except (OSError, ValueError, AttributeError):
        return False


def icon_key(name: str) -> str:
    raw = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())[:80]
    return raw or "app"


@functools.lru_cache(maxsize=1)
def _drive_api():
    api = ctypes.WinDLL('kernel32')
    api.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    return api


def local_executable(src: str) -> bool:
    if (not isinstance(src, str) or len(src)>32768 or
            any(unicodedata.category(ch) in {'Cc','Cf','Cs'} for ch in src)):
        return False
    path = PureWindowsPath(src)
    if (not path.is_absolute() or not path.name.lower().endswith('.exe') or
            (src.startswith('\\\\') and not src.startswith('\\\\?\\')) or path.drive.casefold().startswith('\\\\?\\unc')):
        return False
    if sys.platform == 'win32':
        api = _drive_api()
        if api.GetDriveTypeW(path.anchor) == 4:
            return False
    return True


class IconCache:
    """Path + resource stamp cache, 2 workers, 64 outstanding jobs; fetch never extracts."""
    def __init__(self, root: Path, *, extract=extract_icon_png, clock=time.monotonic, spawn=None):
        self.root, self._extract, self._clock, self._spawn = Path(root), extract, clock, spawn
        self._lock = threading.Lock()
        self._state, self._latest = OrderedDict(), OrderedDict()
        self._pending, self._workers = set(), []
        self._queue = queue.Queue(maxsize=ICON_PENDING)
        self._stop = threading.Event()
        self.extractions = 0

    @staticmethod
    def resource_key(src):
        if src != '@generic' and not local_executable(src):
            return ''
        try:
            stat = Path(src).stat() if src != '@generic' else None
            stamp = (stat.st_mtime_ns, stat.st_size) if stat else (0, 0)
        except (OSError, ValueError):
            stamp = (0, 0)
        return hashlib.sha256(('native-v2:'+str(PureWindowsPath(src)).casefold()+repr(stamp)).encode()).hexdigest()[:32]

    def path_for(self, name, src=''):
        key = self.resource_key(src) if src else self._latest.get(name, '')
        return self.root / (key+'.png' if key else 'unverified.png')

    def _valid(self, path):
        try:
            return path.stat().st_size > MIN_PNG_BYTES
        except OSError:
            return False

    def _worker(self):
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=.2)
            except queue.Empty:
                continue
            try:
                self._run(*job)
            finally:
                self._queue.task_done()

    def _run(self, key, src):
        try:
            ok = bool(self._extract(src, self.root/(key+'.png')))
        except Exception as exc:
            fmlog.log_exception('提取程序图标', exc)
            ok = False
        with self._lock:
            self.extractions += 1
            self._pending.discard(key)
            self._state[key] = (self._clock()+ICON_RETRY, ok)
            self._state.move_to_end(key)
            while len(self._state) > ICON_STATES:
                self._state.popitem(last=False)

    def schedule(self, process, src):
        if not process or not src or self._stop.is_set():
            return
        key = self.resource_key(src)
        if not key:
            return
        dest = self.root/(key+'.png')
        with self._lock:
            self._latest[process] = key
            self._latest.move_to_end(process)
            while len(self._latest) > ICON_STATES:
                self._latest.popitem(last=False)
            if key in self._pending:
                return
            state = self._state.get(key)
            if state and (state[1] and self._valid(dest) or self._clock() < state[0]):
                return
            if self._valid(dest):
                self._state[key] = (self._clock()+ICON_RETRY, True)
                while len(self._state) > ICON_STATES:
                    self._state.popitem(last=False)
                return
            if len(self._pending) >= ICON_PENDING:
                return
            self._pending.add(key)
        if self._spawn is not None:
            self._spawn(lambda: self._run(key, src))
        else:
            with self._lock:
                if not self._workers:
                    self._workers = [threading.Thread(target=self._worker, name='icon-worker', daemon=True) for _ in range(ICON_WORKERS)]
                    for worker in self._workers:
                        worker.start()
            self._queue.put_nowait((key, src))

    def fetch(self, process, src):
        if not process:
            return None
        self.schedule(process, src)
        key = self.resource_key(src) if src else ''
        with self._lock:
            ready = key and key not in self._pending and self._state.get(key, (0,False))[1]
        if ready and self._valid(self.path_for(process, src)):
            return self.path_for(process, src)
        self.schedule('@generic', '@generic')
        generic = self.path_for('@generic', '@generic')
        with self._lock:
            generic_key = self.resource_key('@generic')
            ready = generic_key not in self._pending and self._state.get(generic_key, (0,False))[1]
        return generic if ready and self._valid(generic) else None

    def version(self, src):
        key = self.resource_key(src)
        with self._lock:
            return key + ('-ready' if self._state.get(key, (0,False))[1] else
                          '-generic' if self._state.get(self.resource_key('@generic'), (0,False))[1] else '-pending')

    def stats(self):
        with self._lock:
            return {'icon_workers':len(self._workers),'icon_pending':len(self._pending),
                    'icon_states':len(self._state),'icon_extractions':self.extractions}

    def close(self):
        self._stop.set()
        for worker in self._workers:
            worker.join(timeout=1)
