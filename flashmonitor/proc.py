"""子进程调用（不弹窗口）和本进程资源计数；/api/diag 用这些数字对比优化前后。"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from typing import Any

_LOCK = threading.Lock()
_SUBPROCESS_CALLS = 0


def note_subprocess() -> None:
    global _SUBPROCESS_CALLS
    with _LOCK:
        _SUBPROCESS_CALLS += 1


def subprocess_calls() -> int:
    with _LOCK:
        return _SUBPROCESS_CALLS


def no_window_flags() -> int:
    if sys.platform != "win32":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run_hidden(cmd: list[str], timeout: float = 12.0) -> str:
    note_subprocess()
    p = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=no_window_flags(),
    )
    return p.stdout or ""


def powershell(script: str, timeout: float = 12.0) -> str:
    return run_hidden(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        timeout=timeout,
    )


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


def io_counters() -> dict[str, int]:
    """本进程累计 I/O（GetProcessIoCounters）。写字节数基本就是写盘量。"""
    if sys.platform != "win32":
        return {}
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.GetProcessIoCounters.argtypes = [wintypes.HANDLE, ctypes.POINTER(_IoCounters)]
        k32.GetProcessIoCounters.restype = wintypes.BOOL
        c = _IoCounters()
        if not k32.GetProcessIoCounters(k32.GetCurrentProcess(), ctypes.byref(c)):
            return {}
    except (OSError, AttributeError):
        return {}
    return {
        "io_read_bytes": int(c.ReadTransferCount),
        "io_write_bytes": int(c.WriteTransferCount),
        "io_write_ops": int(c.WriteOperationCount),
    }


def process_stats() -> dict[str, Any]:
    return {
        "cpu_seconds": round(time.process_time(), 3),
        **io_counters(),
        "subprocess_calls": subprocess_calls(),
        "threads": threading.active_count(),
    }
