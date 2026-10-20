"""本机网络和进程信息：连接表、进程名、命令行、路由表（带缓存）、系统代理设置。

连接表 / 进程名 / 命令行优先走 native_net（Win32 API），失败时退回 netstat / tasklist / PowerShell。
路由表仍用 PowerShell 取（一次约 3 秒），缓存 30 秒：过期后先用旧的、后台刷新；
TUN 开关变了或冒出没见过的本机 IP 时标记失效，下一次读取同步刷新（同一原因 5 秒内只触发一次）。
"""

from __future__ import annotations

import ipaddress
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from . import log as fmlog
from . import native_net
from .parsing import parse_netstat_text, parse_tasklist_csv, proxy_server_ports, shorten_cmd
from .proc import powershell, run_hidden
from .process_identity import query_process as native_net_identity

ROUTE_TTL = 30.0
INVALIDATE_GAP = 5.0
CMDLINE_TTL = 45.0
CMDLINE_BATCH = 30

ROUTES_SCRIPT = r"""
$tun = Get-NetAdapter | Where-Object { $_.Name -eq 'FlClash' -or $_.InterfaceDescription -match 'Meta Tunnel|Wintun' } | Select-Object -First 1
$ip4 = $null
if ($tun) { $ip4 = (Get-NetIPAddress -InterfaceIndex $tun.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue | Select-Object -First 1).IPAddress }
$defaults = Get-NetRoute | Where-Object { $_.DestinationPrefix -in @('0.0.0.0/0','::/0') } |
  Select-Object DestinationPrefix, NextHop, InterfaceAlias, RouteMetric, InterfaceMetric
$routes = Get-NetRoute -AddressFamily IPv4 -ErrorAction SilentlyContinue |
  Where-Object { $_.DestinationPrefix -notmatch '^(224\.|255\.255\.255\.255)' } |
  Select-Object DestinationPrefix, NextHop, InterfaceAlias
$addrs = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
  Select-Object IPAddress, InterfaceAlias
[pscustomobject]@{
  tunName = $tun.Name
  tunStatus = [string]$tun.Status
  tunDesc = $tun.InterfaceDescription
  tunIp = $ip4
  defaults = @($defaults)
  routes = @($routes)
  addrs = @($addrs)
} | ConvertTo-Json -Depth 4 -Compress
"""


# ---------- 子进程实现（native 的后备） ----------


def netstat_connections() -> list[dict[str, Any]]:
    return parse_netstat_text(run_hidden(["netstat", "-ano"], timeout=8))


def tasklist_names() -> dict[int, str]:
    return parse_tasklist_csv(run_hidden(["tasklist", "/fo", "csv", "/nh"], timeout=8))


def powershell_cmdlines(pids: Iterable[int]) -> dict[int, str]:
    ids = ",".join(str(int(p)) for p in pids)
    if not ids:
        return {}
    script = (
        f"$ids = @({ids}); "
        "Get-CimInstance Win32_Process | Where-Object { $ids -contains $_.ProcessId } | "
        "Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress"
    )
    raw = powershell(script, timeout=8).strip()
    try:
        data = json.loads(raw) if raw else []
    except ValueError:
        return {}
    rows = [data] if isinstance(data, dict) else data if isinstance(data, list) else []
    out: dict[int, str] = {}
    for rec in rows:
        if not isinstance(rec, dict):
            continue
        try:
            pid = int(rec.get("ProcessId") or 0)
        except (TypeError, ValueError):
            continue
        out[pid] = str(rec.get("CommandLine") or "")
    return out


tcp_connections = native_net.Fallback("GetExtendedTcpTable", native_net.tcp_connections, netstat_connections)
process_names = native_net.Fallback("Toolhelp32", native_net.process_names, tasklist_names)
_raw_cmdlines = native_net.Fallback("NtQueryInformationProcess", native_net.cmdlines, powershell_cmdlines)


def fetch_routes() -> dict[str, Any]:
    raw = powershell(ROUTES_SCRIPT, timeout=15)
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return {"error": raw[:300]}
    return data if isinstance(data, dict) else {}


def as_list(value: Any) -> list[dict[str, Any]]:
    """PowerShell 的 ConvertTo-Json 只有一项时会给对象而不是数组。"""
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return []


def route_addrs(data: dict[str, Any] | None) -> dict[str, str]:
    """{本机 IPv4: 网卡别名}"""
    return {
        str(a.get("IPAddress")): str(a.get("InterfaceAlias") or "")
        for a in as_list((data or {}).get("addrs"))
        if a.get("IPAddress")
    }


def _v4_unicast(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return isinstance(addr, ipaddress.IPv4Address) and not addr.is_unspecified


class RouteCache:
    def __init__(
        self,
        fetch: Callable[[], dict[str, Any]] = fetch_routes,
        *,
        ttl: float = ROUTE_TTL,
        min_gap: float = INVALIDATE_GAP,
        clock: Callable[[], float] = time.monotonic,
        spawn: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._fetch = fetch
        self._ttl = ttl
        self._min_gap = min_gap
        self._clock = clock
        self._spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True, name="routes").start())
        self._lock = threading.Lock()
        self._fetch_lock = threading.Lock()
        self._data: dict[str, Any] | None = None
        self._at = 0.0
        self._stale = False
        self._refreshing = False
        self._last_invalidate = float("-inf")
        self._tun: bool | None = None
        self._asked: set[str] = set()
        self.fetches = 0

    def get(self) -> dict[str, Any]:
        with self._lock:
            data = self._data
            sync = data is None or self._stale
            background = not sync and not self._refreshing and self._clock() - self._at >= self._ttl
            if background:
                self._refreshing = True
        if sync:
            return self._refresh()
        if background:
            self._spawn(self._background)
        return data  # type: ignore[return-value]

    def _background(self) -> None:
        try:
            self._refresh()
        finally:
            with self._lock:
                self._refreshing = False

    def _refresh(self) -> dict[str, Any]:
        with self._fetch_lock:
            with self._lock:
                # 等锁期间别的线程可能已经刷新过了
                if self._data is not None and not self._stale and self._clock() - self._at < self._ttl:
                    return self._data
            try:
                data = self._fetch()
            except Exception as exc:  # noqa: BLE001 - 取不到就沿用旧的
                fmlog.log_exception("读取路由表", exc)
                data = None
            with self._lock:
                self.fetches += 1
                if isinstance(data, dict):
                    self._data = data
                elif self._data is None:
                    self._data = {}
                self._at = self._clock()
                self._stale = False
                return self._data

    def invalidate(self) -> bool:
        """标记失效，下一次 get() 同步重取；min_gap 秒内只生效一次。"""
        now = self._clock()
        with self._lock:
            if now - self._last_invalidate < self._min_gap:
                return False
            self._last_invalidate = now
            self._stale = True
            return True

    def note_tun(self, enabled: bool | None) -> None:
        """Clash 的 TUN 开关变了：路由表多半也变了。"""
        if enabled is None:
            return
        with self._lock:
            prev = self._tun
            self._tun = enabled
        if prev is not None and prev != enabled:
            self.invalidate()

    def note_local_ips(self, ips: Iterable[str]) -> None:
        """连接表里出现路由缓存没有的本机 IPv4（换了网络、VPN 上线）就重取一次；同一个 IP 只问一次。"""
        with self._lock:
            if self._data is None:
                return
            known = set(route_addrs(self._data))
            unknown = {ip for ip in ips if _v4_unicast(ip)} - known - self._asked
            if not unknown:
                return
            if len(self._asked) > 256:
                self._asked.clear()
            self._asked |= unknown
        self.invalidate()


class CmdlineCache:
    """PID + 创建时间绑定的命令行缓存；缺少创建时间不复用缓存。"""

    def __init__(
        self,
        lookup: Callable[[Iterable[int]], dict[int, str]] = _raw_cmdlines,
        *,
        ttl: float = CMDLINE_TTL,
        clock: Callable[[], float] = time.monotonic,
        identity_lookup: Callable[[int], dict] = native_net_identity,
    ) -> None:
        self._lookup = lookup
        self._identity_lookup = identity_lookup
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict[tuple[int, int], tuple[float, str]] = {}

    def get(self, pids: Iterable[int], *, generations: dict[int, int] | None = None) -> dict[int, str]:
        now = self._clock()
        out: dict[int, str] = {}
        missing: list[int] = []
        with self._lock:
            for pid in sorted({int(p) for p in pids if int(p) > 0}):
                generation = (generations or {}).get(pid, 0)
                hit = self._cache.get((pid, generation)) if generation else None
                if hit and now - hit[0] < self._ttl:
                    if hit[1]:
                        out[pid] = hit[1]
                else:
                    missing.append(pid)
        if not missing:
            return out
        batch = missing[:CMDLINE_BATCH]
        try:
            found = self._lookup(batch)
        except Exception as exc:  # noqa: BLE001
            fmlog.log_exception("读取进程命令行", exc)
            found = {}
        with self._lock:
            for pid in batch:
                cmd = shorten_cmd(found.get(pid, ""))
                generation = (generations or {}).get(pid, 0)
                if generation:
                    # The command lookup may race an exit/reuse; verify after querying too.
                    try:
                        info = self._identity_lookup(pid)
                    except (OSError, ValueError, AttributeError):
                        continue
                    if info.get("status") != "verified" or info.get("created") != generation:
                        continue
                    self._cache[(pid, generation)] = (now, cmd)
                if cmd:
                    out[pid] = cmd
            if len(self._cache) > 2048:
                self._cache = {p: v for p, v in self._cache.items() if now - v[0] < self._ttl}
        return out


class FileMemo:
    """文件内容解析结果按 (修改时间, 大小) 缓存；文件没变就不重读。"""

    def __init__(self, path: Path, parse: Callable[[Path], Any]) -> None:
        self.path = Path(path)
        self._parse = parse
        self._key: tuple[int, int] | None | bool = False
        self._value: Any = None
        self._lock = threading.Lock()

    def get(self) -> Any:
        try:
            st = self.path.stat()
            key: tuple[int, int] | None = (st.st_mtime_ns, st.st_size)
        except OSError:
            key = None
        with self._lock:
            if key != self._key:
                self._value = self._parse(self.path)
                self._key = key
            return self._value


def windows_proxy_info() -> tuple[bool, set[int]]:
    """系统代理是否开启，以及指向本机的代理端口。"""
    if sys.platform != "win32":
        return False, set()
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            try:
                enabled = bool(winreg.QueryValueEx(key, "ProxyEnable")[0])
            except OSError:
                enabled = False
            try:
                server = str(winreg.QueryValueEx(key, "ProxyServer")[0] or "")
            except OSError:
                server = ""
    except OSError:
        return False, set()
    return enabled, proxy_server_ports(server)
