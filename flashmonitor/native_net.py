"""用 Win32 API 直接读 TCP 连接表、进程名和进程命令行，替代每两秒启动 netstat / tasklist / PowerShell。

只在 Windows 上可用。系统调用失败抛 OSError，由 Fallback 退回原来的子进程实现（记一次日志，
5 分钟后再试 native）。进程已退出、没有权限这类"查不到"不算失败，返回空值。
"""

from __future__ import annotations

import ctypes
import socket
import sys
import threading
import time
from ctypes import wintypes
from typing import Any, Callable, Iterable

from . import log as fmlog

AF_INET = 2
AF_INET6 = 23
TCP_TABLE_OWNER_PID_CONNECTIONS = 4
MIB_TCP_STATE_ESTAB = 5
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_BAD_LENGTH = 24
TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_COMMAND_LINE_INFORMATION = 60
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
STATUS_BUFFER_OVERFLOW = 0x80000005
STATUS_BUFFER_TOO_SMALL = 0xC0000023
STATUS_INVALID_INFO_CLASS = 0xC0000003
STATUS_NOT_IMPLEMENTED = 0xC0000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
RETRY_NATIVE_AFTER = 300.0


class MIB_TCPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("dwState", wintypes.DWORD),
        ("dwLocalAddr", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("dwRemoteAddr", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


class MIB_TCP6ROW_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("ucLocalAddr", ctypes.c_ubyte * 16),
        ("dwLocalScopeId", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("ucRemoteAddr", ctypes.c_ubyte * 16),
        ("dwRemoteScopeId", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD),
        ("dwState", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),  # ULONG_PTR：指针宽度
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", ctypes.c_void_p),
    ]


_API: dict[str, Any] = {}
_API_LOCK = threading.Lock()


def _api() -> dict[str, Any]:
    """按需加载 DLL 并声明函数签名（用自己的 WinDLL 实例，不影响别处的 argtypes）。"""
    if sys.platform != "win32":
        raise OSError("只支持 Windows")
    with _API_LOCK:
        if _API:
            return _API
        iphlpapi = ctypes.WinDLL("iphlpapi")
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll")

        tcp = iphlpapi.GetExtendedTcpTable
        tcp.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.BOOL,
            wintypes.ULONG,
            ctypes.c_int,
            wintypes.ULONG,
        ]
        tcp.restype = wintypes.DWORD

        snap = k32.CreateToolhelp32Snapshot
        snap.argtypes = [wintypes.DWORD, wintypes.DWORD]
        snap.restype = wintypes.HANDLE
        first = k32.Process32FirstW
        first.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        first.restype = wintypes.BOOL
        nxt = k32.Process32NextW
        nxt.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        nxt.restype = wintypes.BOOL
        opn = k32.OpenProcess
        opn.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        opn.restype = wintypes.HANDLE
        close = k32.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL

        ntq = ntdll.NtQueryInformationProcess
        ntq.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
        ntq.restype = ctypes.c_ulong  # NTSTATUS，按无符号比较

        _API.update(tcp=tcp, snap=snap, first=first, next=nxt, open=opn, close=close, ntq=ntq)
        return _API


# ---------- TCP 连接表 ----------


def _port(dw: int) -> int:
    return socket.ntohs(int(dw) & 0xFFFF)


def _tcp_table(af: int, row_type: type[ctypes.Structure]) -> list[ctypes.Structure]:
    fn = _api()["tcp"]
    size = wintypes.DWORD(0)
    ret = fn(None, ctypes.byref(size), True, af, TCP_TABLE_OWNER_PID_CONNECTIONS, 0)
    buf = None
    for _ in range(8):
        if ret not in (0, ERROR_INSUFFICIENT_BUFFER):
            raise OSError(ret, f"GetExtendedTcpTable(af={af}) 失败")
        # 两次调用之间连接可能变多，多留一些余量
        buf = ctypes.create_string_buffer(max(int(size.value), 4) + 32 * ctypes.sizeof(row_type))
        size = wintypes.DWORD(len(buf))
        ret = fn(buf, ctypes.byref(size), True, af, TCP_TABLE_OWNER_PID_CONNECTIONS, 0)
        if ret == 0:
            break
    else:
        raise OSError(ret, "GetExtendedTcpTable 缓冲区一直不够")
    count = int(wintypes.DWORD.from_buffer(buf).value)
    offset = ctypes.sizeof(wintypes.DWORD)
    if offset + count * ctypes.sizeof(row_type) > len(buf):
        raise OSError("GetExtendedTcpTable 返回的条目数和缓冲区对不上")
    return list((row_type * count).from_buffer(buf, offset))


def _v6(raw: Any, scope: int) -> str:
    ip = socket.inet_ntop(socket.AF_INET6, bytes(raw))
    return f"{ip}%{scope}" if scope else ip


_WARNED: set[str] = set()


def _warn_once(key: str, exc: BaseException) -> None:
    if key not in _WARNED:
        _WARNED.add(key)
        fmlog.log_exception(key, exc)


def tcp_connections() -> list[dict[str, Any]]:
    """已建立的 TCP 连接（IPv4 + IPv6），字段和 parse_netstat_text 一样，顺序按本地地址排好。"""
    rows: list[dict[str, Any]] = []
    for r in _tcp_table(AF_INET, MIB_TCPROW_OWNER_PID):
        if r.dwState != MIB_TCP_STATE_ESTAB:
            continue
        rip = socket.inet_ntoa(int(r.dwRemoteAddr).to_bytes(4, "little"))
        if rip == "0.0.0.0":
            continue
        rows.append(
            {
                "proto": "TCP",
                "local_ip": socket.inet_ntoa(int(r.dwLocalAddr).to_bytes(4, "little")),
                "local_port": _port(r.dwLocalPort),
                "remote_ip": rip,
                "remote_port": _port(r.dwRemotePort),
                "pid": int(r.dwOwningPid),
            }
        )
    try:
        v6 = _tcp_table(AF_INET6, MIB_TCP6ROW_OWNER_PID)
    except OSError as exc:
        _warn_once("IPv6 连接表读取失败，只看 IPv4", exc)
        v6 = []
    for r in v6:
        if r.dwState != MIB_TCP_STATE_ESTAB:
            continue
        rip = _v6(r.ucRemoteAddr, int(r.dwRemoteScopeId))
        if rip == "::":
            continue
        rows.append(
            {
                "proto": "TCP",
                "local_ip": _v6(r.ucLocalAddr, int(r.dwLocalScopeId)),
                "local_port": _port(r.dwLocalPort),
                "remote_ip": rip,
                "remote_port": _port(r.dwRemotePort),
                "pid": int(r.dwOwningPid),
            }
        )
    return rows


# ---------- 进程 ----------


def process_names() -> dict[int, str]:
    """{pid: 进程名（去掉 .exe）}，和 tasklist 的名字一致。"""
    api = _api()
    for attempt in range(3):
        snap = api["snap"](TH32CS_SNAPPROCESS, 0)
        if snap and snap != INVALID_HANDLE_VALUE:
            break
        err = ctypes.get_last_error()
        if err != ERROR_BAD_LENGTH or attempt == 2:  # 文档：ERROR_BAD_LENGTH 时重试即可
            raise ctypes.WinError(err)
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        names: dict[int, str] = {}
        ok = api["first"](snap, ctypes.byref(entry))
        while ok:
            pid = int(entry.th32ProcessID)
            name = str(entry.szExeFile)
            if pid == 0 and name == "[System Process]":
                name = "System Idle Process"
            names[pid] = name.removesuffix(".exe")
            ok = api["next"](snap, ctypes.byref(entry))
        if not names:
            raise ctypes.WinError(ctypes.get_last_error())
        return names
    finally:
        api["close"](snap)


def process_cmdline(pid: int) -> str:
    """进程的完整命令行。进程没了或没有权限返回空串；系统不支持这种查询时抛 OSError。"""
    if pid <= 0:
        return ""
    api = _api()
    handle = api["open"](PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ""
    try:
        size = 2048
        for _ in range(4):
            buf = ctypes.create_string_buffer(size)
            need = wintypes.ULONG(0)
            status = int(api["ntq"](handle, PROCESS_COMMAND_LINE_INFORMATION, buf, size, ctypes.byref(need)))
            if status in (STATUS_INFO_LENGTH_MISMATCH, STATUS_BUFFER_OVERFLOW, STATUS_BUFFER_TOO_SMALL):
                size = max(int(need.value), size * 2)
                continue
            if status in (STATUS_INVALID_INFO_CLASS, STATUS_NOT_IMPLEMENTED):
                raise OSError(status, "这个系统不支持读取进程命令行（需要 Windows 8.1+）")
            if status & 0x80000000:
                return ""
            us = UNICODE_STRING.from_buffer(buf)
            start = ctypes.addressof(buf)
            if not us.Buffer or not us.Length:
                return ""
            if us.Buffer < start or us.Buffer + us.Length > start + size:
                return ""  # 不在我们给的缓冲区里：不去读
            return ctypes.wstring_at(us.Buffer, us.Length // 2)
        return ""
    finally:
        api["close"](handle)


def cmdlines(pids: Iterable[int]) -> dict[int, str]:
    """{pid: 原始命令行}；读不到的是空串。"""
    return {int(pid): process_cmdline(int(pid)) for pid in pids}


class Fallback:
    """先用 native；它抛异常就改用 fallback 并记一次日志，5 分钟后再试 native。"""

    def __init__(
        self,
        label: str,
        native: Callable[..., Any],
        fallback: Callable[..., Any],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.label = label
        self.native = native
        self.fallback = fallback
        self._clock = clock
        self._retry_at = 0.0
        self._failures = 0
        self._lock = threading.Lock()

    @property
    def native_ok(self) -> bool:
        return self._clock() >= self._retry_at

    def __call__(self, *args: Any) -> Any:
        if self.native_ok:
            try:
                return self.native(*args)
            except (OSError, AttributeError, ValueError) as exc:
                with self._lock:
                    self._failures += 1
                    self._retry_at = self._clock() + RETRY_NATIVE_AFTER
                    first = self._failures == 1
                if first:
                    fmlog.log_exception(f"{self.label} 不可用，改用子进程", exc)
        return self.fallback(*args)
