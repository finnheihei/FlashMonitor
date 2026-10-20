"""Limited-information process identities; read-only, bounded and PID-reuse safe."""
from __future__ import annotations
import ctypes
import functools
import sys
import time
from ctypes import wintypes
from pathlib import PureWindowsPath

REASONS = {"no_pid": "没有可靠关联的 PID", "denied": "进程访问受限", "exited": "进程已退出或不存在",
           "failed": "进程查询失败", "pending": "身份补查等待下一轮", "reused": "PID 已复用",
           "conflict": "进程名称或路径与本机身份冲突", "core": "核心提供的路径，未由本机验证"}


@functools.lru_cache(maxsize=1)
def _process_api():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    api.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    return api


def query_process(pid: int) -> dict:
    result = {"pid": pid, "created": 0, "path": "", "status": "failed"}
    if sys.platform != "win32" or pid <= 0:
        return result
    api = _process_api()
    handle = api.OpenProcess(0x1000, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        return {**result, "status": "denied" if error == 5 else "exited" if error == 87 else "failed"}
    try:
        created, exited, kernel, user = [wintypes.FILETIME() for _ in range(4)]
        if not api.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
            return result
        result["created"] = (created.dwHighDateTime << 32) | created.dwLowDateTime
        code = wintypes.DWORD()
        if not api.GetExitCodeProcess(handle, ctypes.byref(code)):
            return result
        if code.value != 259:
            return {**result, "status": "exited"}
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if not api.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return {**result, "status": "denied" if ctypes.get_last_error() == 5 else "failed"}
        return {**result, "path": buf.value, "status": "verified"}
    finally:
        api.CloseHandle(handle)


def creation_seconds(created):
    return created / 10_000_000 - 11644473600 if created else 0


class IdentityReader:
    """Revalidate creation on every requested PID, bounded work per sample (no PID-only cache)."""
    def __init__(self, query=query_process, batch=32):
        self.query, self.batch, self.cursor = query, batch, 0

    def get(self, pids):
        ids = sorted({int(p) for p in pids if int(p) > 0})
        if not ids:
            return {}
        start = self.cursor % len(ids)
        ordered = ids[start:] + ids[:start]
        self.cursor = (start + self.batch) % len(ids)
        out = {pid: {"status": "pending", "path": "", "created": 0} for pid in ids}
        for pid in ordered[:self.batch]:
            try:
                out[pid] = self.query(pid)
            except (OSError, ValueError, AttributeError):
                out[pid] = {"status": "failed", "path": "", "created": 0}
        return out


def enrich(flows, identities, sampled_at):
    for flow in flows:
        pid = int(flow.get("pid") or 0)
        info = identities.get(pid, {})
        status = info.get("status", "no_pid")
        if not pid and flow.get("identity_reason") == "本机端点关联存在冲突":
            status = "conflict"
        path = info.get("path", "")
        created = info.get("created", 0)
        if status == "verified":
            expected = str(flow.get("process") or "").casefold().removesuffix(".exe")
            actual = PureWindowsPath(path).stem.casefold()
            core_path = str(flow.get("proc_path") or "")
            if creation_seconds(created) > sampled_at:
                status = "reused"
            elif expected not in {"", "?", actual, f"pid:{pid}"} or (core_path and PureWindowsPath(core_path) != PureWindowsPath(path)):
                status = "conflict"
            else:
                flow.update(proc_path=path, rule_exe=PureWindowsPath(path).name,
                            identity_created=created, identity_verified=True,
                            identity_source="本机有限权限查询", identity_reason="", identity_at=sampled_at)
                continue
        # Core paths remain attributed to the core, never disguised as a verified local identity.
        if flow.get("proc_path"):
            status = "core" if status in {"no_pid", "pending"} else status
            flow["identity_source"] = "核心元数据"
        else:
            flow["identity_source"] = ""
        flow.update(identity_verified=False, identity_reason=REASONS.get(status, REASONS["failed"]), identity_at=sampled_at)
        if status in {"reused", "conflict"}:
            flow["identity_conflict"] = True


class _ServiceStatus(ctypes.Structure):
    _fields_ = [(name, wintypes.DWORD) for name in ("type", "state", "controls", "winerr", "specific", "checkpoint", "hint", "pid", "flags")]


class _ServiceEntry(ctypes.Structure):
    _fields_ = [("name", wintypes.LPWSTR), ("display", wintypes.LPWSTR), ("status", _ServiceStatus)]


def query_services():
    if sys.platform != "win32":
        return {"services": {}, "error": "服务枚举不可用"}
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    api.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    api.OpenSCManagerW.restype = wintypes.HANDLE
    api.CloseServiceHandle.argtypes = [wintypes.HANDLE]
    api.EnumServicesStatusExW.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD), wintypes.LPCWSTR]
    handle = api.OpenSCManagerW(None, None, 4)
    if not handle:
        return {"services": {}, "error": "服务枚举访问受限或不可用"}
    try:
        out, resume = {}, wintypes.DWORD()
        for _ in range(8):
            buf = ctypes.create_string_buffer(256 * 1024)
            needed, count = wintypes.DWORD(), wintypes.DWORD()
            ok = api.EnumServicesStatusExW(handle, 0, 0x30, 3, buf, len(buf), ctypes.byref(needed), ctypes.byref(count), ctypes.byref(resume), None)
            if not ok and ctypes.get_last_error() != 234:
                return {"services": out, "error": "服务枚举未完整成功"}
            rows = ctypes.cast(buf, ctypes.POINTER(_ServiceEntry))
            for i in range(min(count.value, len(buf) // ctypes.sizeof(_ServiceEntry))):
                row = rows[i]
                if row.status.pid:
                    out.setdefault(int(row.status.pid), []).append(str(row.name))
            if ok:
                return {"services": out, "error": ""}
        return {"services": out, "error": "服务枚举达到上限"}
    finally:
        api.CloseServiceHandle(handle)


class ServiceCache:
    def __init__(self, query=query_services, clock=time.monotonic, ttl=60, verify=query_process):
        self.query, self.clock, self.ttl = query, clock, ttl
        self.verify = verify
        self.at, self.data = float('-inf'), {"services": {}, "error": "尚未枚举"}
        self.generations = {}

    def annotate(self, flows):
        hosts = [f for f in flows if f.get("identity_verified") and PureWindowsPath(f.get("proc_path", "")).name.casefold() == "svchost.exe"]
        if not hosts:
            return
        generations = {f["pid"]: f["identity_created"] for f in hosts}
        if self.clock() - self.at >= self.ttl or any(self.generations.get(pid) != gen for pid, gen in generations.items()):
            try:
                self.data = self.query()
            except (OSError, ValueError, AttributeError):
                self.data = {"services": {}, "error": "服务枚举失败"}
            self.at = self.clock()
            self.generations = generations
        checked = {}
        for flow in hosts:
            key = (flow['pid'], flow['identity_created'])
            if key not in checked:
                try:
                    checked[key] = self.verify(flow["pid"])
                except (OSError, ValueError, AttributeError):
                    checked[key] = {}
            current = checked[key]
            if current.get("created") != flow["identity_created"] or current.get("status") != "verified":
                flow["services"] = []
                flow["service_note"] = "宿主已变化或不可访问，服务列表未关联"
                continue
            # PID service mapping is a fresh inventory, not attribution of the individual connection.
            flow["services"] = list(self.data["services"].get(flow["pid"], []))[:64]
            flow["service_note"] = "宿主服务列表（不代表本连接由某个服务发起）" if flow["services"] else "未取得服务列表，不能据此判断没有服务"
            if self.data.get("error"):
                flow["service_note"] += "；" + self.data["error"]
