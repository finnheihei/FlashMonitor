#!/usr/bin/env python3
"""Flash Monitor：FlClash / mihomo TUN 本机流量监视，127.0.0.1，无第三方依赖。"""

from __future__ import annotations

import argparse
import csv
import io
import ipaddress
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

APP_NAME = "Flash Monitor"
APP_ID = "FlashMonitor"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8787
ACTUAL_PORT = LISTEN_PORT


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    if frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def app_dir() -> Path:
    if frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


CLASH_CONFIG = Path(os.environ.get("APPDATA", "")) / "com.follow" / "clash" / "config.yaml"
CLASH_API = "http://127.0.0.1:9090/connections"
DATA_DIR = app_dir() / "data"
TRAFFIC_LOG = DATA_DIR / "connections.jsonl"
PROGRAMS_FILE = DATA_DIR / "programs.json"
SLOTS_FILE = DATA_DIR / "slots.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
SEEN_FILE = DATA_DIR / "seen.json"
RUN_VALUE_NAME = APP_ID
LEGACY_RUN_NAMES = ("FenGuang",)
FAKE_IP_NETS = (
    ipaddress.ip_network("198.18.0.0/15"),
)
PRIVATE_HINT = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
)
CORE_NAMES = {
    "flclashcore",
    "flclash",
    "flclashhelperservice",
    "clash-meta",
    "mihomo",
    "verge-mihomo",
    "clash-verge",
    "clash verge",
}
BROWSERS = {
    "msedge",
    "chrome",
    "chromium",
    "firefox",
    "brave",
    "opera",
    "vivaldi",
    "iexplore",
    "msedgewebview2",
    "360chrome",
    "qqbrowser",
    "sogouexplorer",
}
CORE_DNS_IPS = {
    "1.1.1.1",
    "1.0.0.1",
    "8.8.8.8",
    "8.8.4.4",
    "223.5.5.5",
    "223.6.6.6",
    "119.29.29.29",
    "114.114.114.114",
    "1.12.12.21",
    "120.53.53.53",
}

def load_page() -> str:
    return (resource_dir() / "ui.html").read_text(encoding="utf-8")


DEFAULT_SETTINGS = {
    "autostart": False,
    "open_browser": True,
    "start_minimized": False,
    "close_to_tray": True,
    "clash_api": "http://127.0.0.1:9090",
    "clash_secret": "",
    "retention_days": 40,
    "quota_total_mb": 0,
    "quota_program_mb": 0,
}


class AppSettings:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = dict(DEFAULT_SETTINGS)
        self.error = ""
        self._load()

    def _load(self) -> None:
        if self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self.data.update({k: raw[k] for k in DEFAULT_SETTINGS if k in raw})
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                pass
        self.data["autostart"] = autostart_enabled()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: self.data.get(k, v) for k, v in DEFAULT_SETTINGS.items()}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def apply(self, patch: dict[str, Any]) -> None:
        if "open_browser" in patch:
            self.data["open_browser"] = bool(patch["open_browser"])
        if "start_minimized" in patch:
            self.data["start_minimized"] = bool(patch["start_minimized"])
        if "clash_api" in patch:
            url = str(patch["clash_api"] or "").strip().rstrip("/")
            if url.startswith("http://") or url.startswith("https://"):
                self.data["clash_api"] = url
        if "retention_days" in patch:
            try:
                days = int(patch["retention_days"])
            except (TypeError, ValueError):
                days = 40
            self.data["retention_days"] = max(7, min(days, 365))
        if "clash_secret" in patch:
            self.data["clash_secret"] = str(patch.get("clash_secret") or "").strip()
        if "close_to_tray" in patch:
            self.data["close_to_tray"] = bool(patch["close_to_tray"])
        if "quota_total_mb" in patch:
            try:
                self.data["quota_total_mb"] = max(0, min(int(patch["quota_total_mb"]), 10_000_000))
            except (TypeError, ValueError):
                self.data["quota_total_mb"] = 0
        if "quota_program_mb" in patch:
            try:
                self.data["quota_program_mb"] = max(0, min(int(patch["quota_program_mb"]), 10_000_000))
            except (TypeError, ValueError):
                self.data["quota_program_mb"] = 0
        self.error = ""
        try:
            if "autostart" in patch:
                set_autostart(bool(patch["autostart"]))
            elif autostart_enabled() and ("start_minimized" in patch or "open_browser" in patch):
                set_autostart(True)
        except RuntimeError as exc:
            self.error = str(exc)
        self.data["autostart"] = autostart_enabled()
        self.save()

    def public(self) -> dict[str, Any]:
        return {
            **{k: self.data.get(k, v) for k, v in DEFAULT_SETTINGS.items()},
            "autostart": autostart_enabled(),
            "data_dir": str(DATA_DIR),
            "page": f"http://{LISTEN_HOST}:{ACTUAL_PORT}/",
            "launch": launch_command(autostart=True),
            "error": getattr(self, "error", "") or "",
        }


def launch_command(*, autostart: bool = False) -> str:
    flags = ["--gui"]
    if autostart or SETTINGS.data.get("start_minimized"):
        flags.append("--minimized")
    if autostart or not SETTINGS.data.get("open_browser", True):
        flags.append("--no-browser")
    flag_s = " ".join(flags)
    if frozen():
        return f'"{sys.executable}" {flag_s}'
    py = Path(sys.executable)
    helper = py.with_name("pythonw.exe")
    exe = str(helper if helper.is_file() else py)
    script = str(Path(__file__).resolve())
    return f'"{exe}" "{script}" {flag_s}'


def autostart_enabled() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            for name in (RUN_VALUE_NAME, *LEGACY_RUN_NAMES):
                try:
                    val, _ = winreg.QueryValueEx(key, name)
                    if val:
                        return True
                except OSError:
                    continue
    except OSError:
        return False
    return False


def set_autostart(on: bool) -> None:
    if sys.platform != "win32":
        return
    import winreg

    access = winreg.KEY_SET_VALUE
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, access) as key:
            for name in LEGACY_RUN_NAMES:
                try:
                    winreg.DeleteValue(key, name)
                except OSError:
                    pass
            if on:
                winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, launch_command(autostart=True))
            else:
                try:
                    winreg.DeleteValue(key, RUN_VALUE_NAME)
                except OSError:
                    pass
    except OSError as exc:
        raise RuntimeError(f"无法写入开机启动项：{exc}") from exc


SETTINGS = AppSettings(SETTINGS_FILE)

def _run(cmd: list[str], timeout: float = 12.0) -> str:
    creation = 0
    if sys.platform == "win32":
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    p = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=creation,
    )
    return p.stdout or ""


def _ps(script: str, timeout: float = 12.0) -> str:
    return _run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        timeout=timeout,
    )


def load_proxy_ports(config: Path) -> set[int]:
    ports: set[int] = set()
    if not config.is_file():
        return ports
    in_proxies = False
    for raw in config.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.rstrip()
        if line.startswith("proxies:"):
            in_proxies = True
            continue
        if in_proxies and (line.startswith("proxy-groups:") or line.startswith("rules:")):
            break
        if in_proxies:
            m = re.search(r"(?:^|\s)port:\s*['\"]?(\d+)", line)
            if m:
                ports.add(int(m.group(1)))
    return ports


_CMD_CACHE: dict[int, tuple[float, str]] = {}


def shorten_cmd(cmd: str) -> str:
    cmd = " ".join(str(cmd or "").split())
    if len(cmd) <= 180:
        return cmd
    return cmd[:80] + " … " + cmd[-80:]


def lookup_cmdlines(pids: set[int]) -> dict[int, str]:
    now = time.monotonic()
    out: dict[int, str] = {}
    missing: list[int] = []
    for pid in pids:
        if pid <= 0:
            continue
        hit = _CMD_CACHE.get(pid)
        if hit and now - hit[0] < 45:
            if hit[1]:
                out[pid] = hit[1]
        else:
            missing.append(pid)
    if not missing:
        return out
    ids = ",".join(str(p) for p in missing[:30])
    script = (
        f"$ids = @({ids}); "
        "Get-CimInstance Win32_Process | Where-Object { $ids -contains $_.ProcessId } | "
        "Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress"
    )
    raw = _ps(script, timeout=8).strip()
    rows: list[Any] = []
    try:
        data = json.loads(raw) if raw else []
        if isinstance(data, dict):
            rows = [data]
        elif isinstance(data, list):
            rows = data
    except json.JSONDecodeError:
        rows = []
    found: set[int] = set()
    for rec in rows:
        if not isinstance(rec, dict):
            continue
        try:
            pid = int(rec.get("ProcessId") or 0)
        except (TypeError, ValueError):
            continue
        cmd = shorten_cmd(str(rec.get("CommandLine") or ""))
        _CMD_CACHE[pid] = (now, cmd)
        found.add(pid)
        if cmd:
            out[pid] = cmd
    for pid in missing:
        if pid not in found:
            _CMD_CACHE[pid] = (now, "")
    return out


def parse_tasklist() -> dict[int, str]:
    out = _run(["tasklist", "/fo", "csv", "/nh"], timeout=8)
    names: dict[int, str] = {}
    reader = csv.reader(io.StringIO(out))
    for row in reader:
        if len(row) < 2:
            continue
        name = row[0].removesuffix(".exe")
        pid_s = row[1].replace(",", "").strip()
        if pid_s.isdigit():
            names[int(pid_s)] = name
    return names


def parse_netstat() -> list[dict[str, Any]]:
    out = _run(["netstat", "-ano"], timeout=8)
    rows: list[dict[str, Any]] = []
    for line in out.splitlines():
        s = line.strip()
        if not s.upper().startswith("TCP"):
            continue
        parts = s.split()
        if len(parts) < 5:
            continue
        proto, local, remote, state, pid_s = parts[0], parts[1], parts[2], parts[3], parts[-1]
        if state.upper() != "ESTABLISHED" or not pid_s.isdigit():
            continue
        lip, lport = split_endpoint(local)
        rip, rport = split_endpoint(remote)
        if not rip or rip in {"0.0.0.0", "*"}:
            continue
        rows.append(
            {
                "proto": proto,
                "local_ip": lip,
                "local_port": lport,
                "remote_ip": rip,
                "remote_port": rport,
                "pid": int(pid_s),
            }
        )
    return rows


def split_endpoint(ep: str) -> tuple[str, int]:
    ep = ep.strip()
    if ep.startswith("["):
        host, _, port = ep[1:].partition("]")
        return host, int(port[1:] or 0)
    if ep.count(":") == 1:
        host, _, port = ep.rpartition(":")
        return host, int(port or 0)
    # IPv6 without brackets: last colon is port if numeric
    host, _, port = ep.rpartition(":")
    try:
        return host.strip("[]"), int(port)
    except ValueError:
        return ep, 0


def parse_ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def in_nets(ip: ipaddress._BaseAddress, nets: tuple[ipaddress._BaseNetwork, ...]) -> bool:
    return any(ip in n for n in nets)


def collect_routes() -> dict[str, Any]:
    script = r"""
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
    raw = _ps(script, timeout=15)
    try:
        return json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {"error": raw[:300]}


def longest_prefix(ip: ipaddress.IPv4Address, routes: list[dict[str, str]]) -> dict[str, str] | None:
    best_len = -1
    best = None
    addr = int(ip)
    for r in routes:
        prefix = (r.get("DestinationPrefix") or "").strip()
        if "/" not in prefix:
            continue
        net_s, plen_s = prefix.split("/", 1)
        try:
            plen = int(plen_s)
            net = ipaddress.IPv4Address(net_s)
        except ValueError:
            continue
        mask = (0xFFFFFFFF << (32 - plen)) & 0xFFFFFFFF if plen else 0
        if (addr & mask) == (int(net) & mask) and plen > best_len:
            best_len = plen
            best = r
    return best


def iface_kind(alias: str) -> str:
    a = (alias or "").lower()
    if "flclash" in a or "meta" in a or "wintun" in a:
        return "tun"
    if "atrust" in a or "sangfor" in a:
        return "vpn"
    if "wlan" in a or "wi-fi" in a or "wifi" in a:
        return "wifi"
    if "ethernet" in a or "以太网" in a:
        return "nic"
    return a or "other"


def clash_base_urls() -> tuple[str, ...]:
    base = str(SETTINGS.data.get("clash_api") or "http://127.0.0.1:9090").rstrip("/")
    urls = [base]
    for extra in ("http://127.0.0.1:9090", "http://127.0.0.1:9091", "http://127.0.0.1:9097"):
        if extra not in urls:
            urls.append(extra)
    return tuple(urls)


def clash_connection_urls() -> tuple[str, ...]:
    return tuple(base + "/connections" for base in clash_base_urls())


def clash_headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    secret = str(SETTINGS.data.get("clash_secret") or "").strip()
    if secret:
        headers["Authorization"] = "Bearer " + secret
    return headers


def try_clash_payload() -> dict[str, Any]:
    for url in clash_connection_urls():
        try:
            req = Request(url, headers=clash_headers())
            with urlopen(req, timeout=2.0) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            if isinstance(data, dict):
                return {
                    "connections": data.get("connections") or [],
                    "uploadTotal": int(data.get("uploadTotal") or 0),
                    "downloadTotal": int(data.get("downloadTotal") or 0),
                }
        except Exception:
            continue
    return {"connections": [], "uploadTotal": 0, "downloadTotal": 0}


def try_clash_connections() -> list[dict[str, Any]]:
    return try_clash_payload()["connections"]


def close_clash_connection(cid: str) -> bool:
    cid = str(cid or "").strip()
    if not cid or len(cid) > 80 or "/" in cid or "\\" in cid or ".." in cid:
        return False
    if not all(ch.isalnum() or ch in "-_" for ch in cid):
        return False
    for base in clash_base_urls():
        url = f"{base}/connections/{quote(cid, safe='-')}"
        try:
            req = Request(url, method="DELETE", headers=clash_headers())
            with urlopen(req, timeout=2.0) as resp:
                resp.read()
            return True
        except Exception:
            continue
    return False


_SYS_TYPES = {"HTTP", "HTTPS", "SOCKS", "SOCKS5", "MIXED", "HTTPCONNECT"}
_TUN_TYPES = {"TUN", "REDIR", "TPROXY"}
_DIRECT_OUT = {"DIRECT", "REJECT", "PASS", "REJECT-DROP"}


def chain_outbound(chains: list[str]) -> tuple[str, str]:
    """Clash chains[0] is the leaf outbound: node name, or DIRECT/REJECT."""
    if not chains:
        return "unknown", ""
    leaf = chains[0]
    if leaf.upper() in _DIRECT_OUT:
        return "direct", leaf
    return "proxy", leaf


def clash_to_flow(conn: dict[str, Any]) -> dict[str, Any]:
    meta = conn.get("metadata") or {}
    chains = [str(c) for c in (conn.get("chains") or [])]
    typ = str(meta.get("type") or "").upper()
    if typ in _SYS_TYPES:
        inbound = "sysproxy"
    elif typ in _TUN_TYPES:
        inbound = "tun"
    else:
        inbound = "tun"

    outbound, via = chain_outbound(chains)

    raw_proc = str(meta.get("process") or "") or str(meta.get("processPath") or "")
    proc = Path(raw_proc.replace("\\", "/")).name or raw_proc or "?"
    if proc.lower().endswith(".exe"):
        proc = proc[:-4]
    host = str(meta.get("host") or "").strip()
    dip = str(meta.get("destinationIP") or "").strip()
    dport = str(meta.get("destinationPort") or "").strip()
    remote = host or dip or "?"
    if dport:
        remote = f"{remote}:{dport}"
    rule = " ".join(
        x for x in (str(conn.get("rule") or ""), str(conn.get("rulePayload") or "")) if x
    )
    note = " · ".join(x for x in (typ, f"{via or outbound}", rule) if x)
    path = outbound if outbound in {"proxy", "direct"} else inbound
    start = str(conn.get("start") or "")
    return {
        "id": str(conn.get("id") or ""),
        "path": path,
        "inbound": inbound,
        "outbound": outbound,
        "node": via if outbound == "proxy" else "",
        "process": proc,
        "pid": 0,
        "proc_path": str(meta.get("processPath") or ""),
        "local": f"{meta.get('sourceIP', '')}:{meta.get('sourcePort', '')}",
        "remote": remote,
        "route": via or typ,
        "note": note,
        "upload": int(conn.get("upload") or 0),
        "download": int(conn.get("download") or 0),
        "start": start,
        "closed": False,
    }


def host_of(remote: str) -> str:
    remote = (remote or "").strip()
    if not remote or remote == "?":
        return ""
    if remote.startswith("["):
        return remote.split("]")[0].strip("[]")
    if remote.count(":") == 1:
        return remote.rsplit(":", 1)[0]
    return remote


def site_key(host: str) -> str:
    host = host_of(host).lower().rstrip(".")
    if not host:
        return ""
    if all(ch.isdigit() or ch == "." for ch in host):
        return host
    parts = [p for p in host.split(".") if p]
    if len(parts) < 2:
        return host
    if (
        len(parts) >= 3
        and parts[-1] in {"cn", "uk", "jp", "au", "br", "hk", "tw"}
        and parts[-2] in {"com", "net", "org", "gov", "edu", "co", "ac"}
    ):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def event_ts(rec: dict[str, Any]) -> float:
    end = rec.get("end")
    if isinstance(end, (int, float)) and end > 1_000_000_000:
        return float(end)
    start = rec.get("start") or ""
    if isinstance(start, str) and start:
        try:
            return datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return time.time()


def slot_key(ts: float | None = None) -> str:
    t = time.localtime(ts or time.time())
    minute = t.tm_min - (t.tm_min % 15)
    return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}T{t.tm_hour:02d}:{minute:02d}"


def slot_ts(key: str) -> float:
    return time.mktime(time.strptime(key, "%Y-%m-%dT%H:%M"))


class TrafficStore:
    """按连接落盘，并按程序累加上下行（重启后仍保留）。"""

    def __init__(self, path: Path, programs_path: Path, slots_path: Path) -> None:
        self.path = path
        self.programs_path = programs_path
        self.slots_path = slots_path
        self.live: dict[str, dict[str, Any]] = {}
        self.last_bytes: dict[str, tuple[int, int]] = {}
        self.closed: list[dict[str, Any]] = []
        self.programs: dict[str, dict[str, Any]] = {}
        self.slots: dict[str, dict[str, dict[str, int]]] = {}
        self.node_slots: dict[str, dict[str, dict[str, int]]] = {}
        self.site_slots: dict[str, dict[str, dict[str, int]]] = {}
        self.seen: set[str] = set()
        self.new_apps: list[dict[str, Any]] = []
        self._booting = True
        self.logged = 0
        self.lock = threading.Lock()
        self.speed = {
            "up": 0.0,
            "down": 0.0,
            "proxy_up": 0.0,
            "proxy_down": 0.0,
            "direct_up": 0.0,
            "direct_down": 0.0,
        }
        self.proc_speed: dict[str, dict[str, float]] = {}
        self._tick_at = time.monotonic()
        self.history: list[dict[str, float]] = []
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            self.logged = sum(1 for _ in path.open("r", encoding="utf-8", errors="replace"))
        self._load_programs()
        self._booting = False

    def _blank_site(self) -> dict[str, Any]:
        return {
            "upload": 0,
            "download": 0,
            "proxy_up": 0,
            "proxy_down": 0,
            "direct_up": 0,
            "direct_down": 0,
            "conns": 0,
            "last": 0,
        }

    def _blank(self) -> dict[str, Any]:
        row = self._blank_site()
        row["sites"] = {}
        row["last_node"] = ""
        row["last_cmd"] = ""
        row["last_path"] = ""
        return row

    def _read_site_map(self, rec: dict[str, Any]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        raw = rec.get("sites") or {}
        if not isinstance(raw, dict):
            return out
        for name, srec in raw.items():
            if not isinstance(srec, dict):
                continue
            row = self._blank_site()
            for k in row:
                row[k] = int(srec.get(k) or 0)
            out[str(name)] = row
        return out

    def _load_programs(self) -> None:
        if self.programs_path.is_file():
            try:
                data = json.loads(self.programs_path.read_text(encoding="utf-8"))
                raw = data.get("programs") if isinstance(data, dict) else data
                if isinstance(raw, dict):
                    for name, rec in raw.items():
                        if not isinstance(rec, dict):
                            continue
                        row = self._blank()
                        for k in row:
                            if k == "sites":
                                continue
                            if k in {"last_node", "last_cmd", "last_path"}:
                                row[k] = str(rec.get(k) or "")
                                continue
                            row[k] = int(rec.get(k) or 0)
                        row["sites"] = self._read_site_map(rec)
                        self.programs[str(name)] = row
                        self.seen.add(str(name))
                    self._hydrate_sites_from_log()
                    self._load_slots()
                    self._load_seen()
                    return
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                pass
        if self.path.is_file():
            try:
                with self.path.open("r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        rec = json.loads(line)
                        self._add(
                            str(rec.get("process") or "未知"),
                            int(rec.get("upload") or 0),
                            int(rec.get("download") or 0),
                            str(rec.get("outbound") or ""),
                            closed=True,
                            host=str(rec.get("remote") or ""),
                            node=str(rec.get("node") or ""),
                        )
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                pass
            self._save_programs()
        self._load_slots()
        self._load_seen()

    def _hydrate_sites_from_log(self) -> None:
        need = {n for n, rec in self.programs.items() if not rec.get("sites")}
        if not need or not self.path.is_file():
            return
        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    proc = str(rec.get("process") or "")
                    if proc not in need:
                        continue
                    self._add_site(
                        proc,
                        int(rec.get("upload") or 0),
                        int(rec.get("download") or 0),
                        str(rec.get("outbound") or ""),
                        str(rec.get("remote") or ""),
                        closed=True,
                    )
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return
        self._save_programs()

    def _save_programs(self) -> None:
        payload = {"programs": self.programs, "updated": int(time.time())}
        tmp = self.programs_path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.programs_path)
        except OSError:
            pass

    def _blank_slot(self) -> dict[str, int]:
        return {"proxy_up": 0, "proxy_down": 0, "direct_up": 0, "direct_down": 0}

    def _blank_node(self) -> dict[str, int]:
        return {"up": 0, "down": 0}

    def _read_slot_bucket(self, raw: Any, blank) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        if not isinstance(raw, dict):
            return out
        for name, rec in raw.items():
            if not isinstance(rec, dict):
                continue
            row = blank()
            for k in row:
                row[k] = int(rec.get(k) or 0)
            out[str(name)] = row
        return out

    def _load_seen(self) -> None:
        if SEEN_FILE.is_file():
            try:
                raw = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
                names = raw.get("names") if isinstance(raw, dict) else raw
                if isinstance(names, list):
                    self.seen.update(str(n) for n in names if n)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                pass
        self.seen.update(self.programs.keys())

    def _save_seen(self) -> None:
        try:
            SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = SEEN_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"names": sorted(self.seen)}, ensure_ascii=False), encoding="utf-8")
            tmp.replace(SEEN_FILE)
        except OSError:
            pass

    def _note_process(self, name: str) -> None:
        if self._booting or name.lower() in CORE_NAMES:
            return
        if name in self.seen:
            return
        self.seen.add(name)
        self.new_apps.append({"process": name, "ts": int(time.time())})
        self.new_apps = self.new_apps[-12:]
        self._save_seen()

    def remember_origin(self, process: str, cmd: str = "", path: str = "") -> None:
        name = process.strip()
        if not name or name.lower() in CORE_NAMES:
            return
        with self.lock:
            row = self.programs.get(name)
            if row is None:
                return
            if cmd:
                row["last_cmd"] = shorten_cmd(cmd)
            if path:
                row["last_path"] = path[-200:]

    def recent_new(self) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.new_apps)

    def ack_new(self, process: str = "") -> None:
        with self.lock:
            if process:
                self.new_apps = [x for x in self.new_apps if x.get("process") != process]
            else:
                self.new_apps = []

    def _load_slots(self) -> None:
        if self.slots_path.is_file():
            try:
                data = json.loads(self.slots_path.read_text(encoding="utf-8"))
                raw = data.get("slots") if isinstance(data, dict) else data
                if isinstance(raw, dict):
                    for hour, procs in raw.items():
                        bucket = self._read_slot_bucket(procs, self._blank_slot)
                        if bucket:
                            self.slots[str(hour)] = bucket
                if isinstance(data, dict):
                    nodes = data.get("nodes")
                    sites = data.get("sites")
                    if isinstance(nodes, dict):
                        for hour, recs in nodes.items():
                            bucket = self._read_slot_bucket(recs, self._blank_node)
                            if bucket:
                                self.node_slots[str(hour)] = bucket
                    if isinstance(sites, dict):
                        for hour, recs in sites.items():
                            bucket = self._read_slot_bucket(recs, self._blank_slot)
                            if bucket:
                                self.site_slots[str(hour)] = bucket
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                self.slots = {}
        if not self.slots:
            self._hydrate_slots_from_log()
        else:
            self._prune_slots()
        if not self.node_slots and not self.site_slots:
            self._hydrate_ns_from_log()

    def _hydrate_slots_from_log(self) -> None:
        if not self.path.is_file():
            return
        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    self._add_slot(
                        str(rec.get("process") or "未知"),
                        int(rec.get("upload") or 0),
                        int(rec.get("download") or 0),
                        str(rec.get("outbound") or ""),
                        ts=event_ts(rec),
                    )
                    self._add_node_slot(
                        str(rec.get("node") or ""),
                        int(rec.get("upload") or 0),
                        int(rec.get("download") or 0),
                        ts=event_ts(rec),
                    )
                    self._add_site_slot(
                        str(rec.get("remote") or ""),
                        int(rec.get("upload") or 0),
                        int(rec.get("download") or 0),
                        str(rec.get("outbound") or ""),
                        ts=event_ts(rec),
                    )
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return
        self._save_slots()

    def _hydrate_ns_from_log(self) -> None:
        if not self.path.is_file():
            return
        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    ts = event_ts(rec)
                    self._add_node_slot(
                        str(rec.get("node") or ""),
                        int(rec.get("upload") or 0),
                        int(rec.get("download") or 0),
                        ts=ts,
                    )
                    self._add_site_slot(
                        str(rec.get("remote") or ""),
                        int(rec.get("upload") or 0),
                        int(rec.get("download") or 0),
                        str(rec.get("outbound") or ""),
                        ts=ts,
                    )
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return
        self._save_slots()

    def _prune_slots(self) -> None:
        days = int(SETTINGS.data.get("retention_days") or 40)
        days = max(7, min(days, 365))
        cutoff = time.time() - days * 86400

        def drop_old(store: dict[str, dict]) -> None:
            gone = []
            for key in store:
                try:
                    if slot_ts(key) < cutoff:
                        gone.append(key)
                except ValueError:
                    gone.append(key)
            for key in gone:
                store.pop(key, None)

        drop_old(self.slots)
        drop_old(self.node_slots)
        drop_old(self.site_slots)

    def _save_slots(self) -> None:
        self._prune_slots()
        payload = {
            "slots": self.slots,
            "nodes": self.node_slots,
            "sites": self.site_slots,
            "updated": int(time.time()),
        }
        tmp = self.slots_path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.slots_path)
        except OSError:
            pass

    def _add_slot(
        self,
        process: str,
        up: int,
        down: int,
        outbound: str,
        ts: float | None = None,
    ) -> None:
        name = process.strip() or "未知"
        if name.lower() in CORE_NAMES:
            return
        up = max(int(up), 0)
        down = max(int(down), 0)
        if up == 0 and down == 0:
            return
        key = slot_key(ts)
        row = self.slots.setdefault(key, {}).setdefault(name, self._blank_slot())
        if outbound == "proxy":
            row["proxy_up"] += up
            row["proxy_down"] += down
        elif outbound == "direct":
            row["direct_up"] += up
            row["direct_down"] += down
        else:
            row["direct_up"] += up
            row["direct_down"] += down

    def _add_node_slot(self, node: str, up: int, down: int, ts: float | None = None) -> None:
        node = (node or "").strip()
        if not node or node.upper() in _DIRECT_OUT:
            return
        up = max(int(up), 0)
        down = max(int(down), 0)
        if not up and not down:
            return
        row = self.node_slots.setdefault(slot_key(ts), {}).setdefault(node, self._blank_node())
        row["up"] += up
        row["down"] += down

    def _add_site_slot(self, host: str, up: int, down: int, outbound: str, ts: float | None = None) -> None:
        site = site_key(host)
        if not site:
            return
        up = max(int(up), 0)
        down = max(int(down), 0)
        if not up and not down:
            return
        row = self.site_slots.setdefault(slot_key(ts), {}).setdefault(site, self._blank_slot())
        if outbound == "proxy":
            row["proxy_up"] += up
            row["proxy_down"] += down
        else:
            row["direct_up"] += up
            row["direct_down"] += down

    def ranking(self, period: str) -> dict[str, Any]:
        now = time.time()
        lt = time.localtime(now)
        if period == "today":
            since = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        elif period == "7d":
            since = now - 7 * 86400
        elif period == "30d":
            since = now - 30 * 86400
        elif period == "all":
            since = 0.0
        else:
            since = now - 3600
            period = "1h"
        agg: dict[str, dict[str, int]] = {}
        with self.lock:
            if period == "all":
                for name, rec in self.programs.items():
                    agg[name] = {
                        "proxy_up": int(rec.get("proxy_up") or 0),
                        "proxy_down": int(rec.get("proxy_down") or 0),
                        "direct_up": int(rec.get("direct_up") or 0),
                        "direct_down": int(rec.get("direct_down") or 0),
                    }
                for procs in self.slots.values():
                    for name, rec in procs.items():
                        if name in agg:
                            continue
                        agg[name] = {
                            "proxy_up": int(rec.get("proxy_up") or 0),
                            "proxy_down": int(rec.get("proxy_down") or 0),
                            "direct_up": int(rec.get("direct_up") or 0),
                            "direct_down": int(rec.get("direct_down") or 0),
                        }
            else:
                for key, procs in self.slots.items():
                    try:
                        start = slot_ts(key)
                    except ValueError:
                        continue
                    if start + 15 * 60 <= since:
                        continue
                    for name, rec in procs.items():
                        row = agg.setdefault(name, self._blank_slot())
                        for k in row:
                            row[k] += int(rec.get(k) or 0)
        rows: list[dict[str, Any]] = []
        tot_up = tot_down = 0
        proxy_up = proxy_down = 0
        direct_up = direct_down = 0
        for name, rec in agg.items():
            pup = int(rec.get("proxy_up") or 0)
            pdown = int(rec.get("proxy_down") or 0)
            dup = int(rec.get("direct_up") or 0)
            ddown = int(rec.get("direct_down") or 0)
            up = pup + dup
            down = pdown + ddown
            if not up and not down:
                continue
            rows.append(
                {
                    "process": name,
                    "upload": up,
                    "download": down,
                    "proxy_up": pup,
                    "proxy_down": pdown,
                    "direct_up": dup,
                    "direct_down": ddown,
                    "proxy_total": pup + pdown,
                }
            )
            tot_up += up
            tot_down += down
            proxy_up += pup
            proxy_down += pdown
            direct_up += dup
            direct_down += ddown
        rows.sort(key=lambda r: -(int(r["download"]) + int(r["upload"])))
        whole = max(tot_up + tot_down, 1)
        proxy_whole = max(proxy_up + proxy_down, 1)
        for i, row in enumerate(rows, 1):
            row["rank"] = i
            row["share"] = round(100.0 * (row["download"] + row["upload"]) / whole, 1)
            row["proxy_share"] = round(100.0 * int(row["proxy_total"]) / proxy_whole, 1)
        with self.lock:
            for row in rows:
                rec = self.programs.get(row["process"]) or {}
                row["cmd"] = str(rec.get("last_cmd") or "")
                row["path"] = str(rec.get("last_path") or "")
                sites = rec.get("sites") or {}
                site_rows = [{"site": host, **srec} for host, srec in sites.items()]
                site_rows.sort(
                    key=lambda r: -(int(r.get("download") or 0) + int(r.get("upload") or 0))
                )
                row["sites"] = site_rows[:12]
        return {
            "upload": tot_up,
            "download": tot_down,
            "proxy_upload": proxy_up,
            "proxy_download": proxy_down,
            "direct_upload": direct_up,
            "direct_download": direct_down,
            "since": int(since),
            "rows": rows,
            "nodes": self._rank_nodes(period, since),
            "sites": self._rank_sites(period, since),
        }

    def _rank_nodes(self, period: str, since: float) -> dict[str, Any]:
        with self.lock:
            items = list(self.node_slots.items())
        agg: dict[str, dict[str, int]] = {}
        for key, recs in items:
            if period != "all":
                try:
                    start = slot_ts(key)
                except ValueError:
                    continue
                if start + 15 * 60 <= since:
                    continue
            for name, rec in recs.items():
                row = agg.setdefault(name, self._blank_node())
                row["up"] += int(rec.get("up") or 0)
                row["down"] += int(rec.get("down") or 0)
        rows: list[dict[str, Any]] = []
        tot = 0
        for name, rec in agg.items():
            up = int(rec.get("up") or 0)
            down = int(rec.get("down") or 0)
            if not up and not down:
                continue
            rows.append({"node": name, "upload": up, "download": down})
            tot += up + down
        rows.sort(key=lambda r: -(int(r["download"]) + int(r["upload"])))
        whole = max(tot, 1)
        for i, row in enumerate(rows, 1):
            row["rank"] = i
            row["share"] = round(100.0 * (row["download"] + row["upload"]) / whole, 1)
        return {"upload": sum(r["upload"] for r in rows), "download": sum(r["download"] for r in rows), "rows": rows}

    def _rank_sites(self, period: str, since: float) -> dict[str, Any]:
        with self.lock:
            items = list(self.site_slots.items())
        agg: dict[str, dict[str, int]] = {}
        for key, recs in items:
            if period != "all":
                try:
                    start = slot_ts(key)
                except ValueError:
                    continue
                if start + 15 * 60 <= since:
                    continue
            for name, rec in recs.items():
                row = agg.setdefault(name, self._blank_slot())
                for k in row:
                    row[k] += int(rec.get(k) or 0)
        rows: list[dict[str, Any]] = []
        tot = 0
        for name, rec in agg.items():
            pup = int(rec.get("proxy_up") or 0)
            pdown = int(rec.get("proxy_down") or 0)
            dup = int(rec.get("direct_up") or 0)
            ddown = int(rec.get("direct_down") or 0)
            up = pup + dup
            down = pdown + ddown
            if not up and not down:
                continue
            rows.append(
                {
                    "site": name,
                    "upload": up,
                    "download": down,
                    "proxy_up": pup,
                    "proxy_down": pdown,
                    "direct_down": ddown,
                    "proxy_total": pup + pdown,
                }
            )
            tot += up + down
        rows.sort(key=lambda r: -(int(r["download"]) + int(r["upload"])))
        whole = max(tot, 1)
        for i, row in enumerate(rows, 1):
            row["rank"] = i
            row["share"] = round(100.0 * (row["download"] + row["upload"]) / whole, 1)
        return {
            "upload": sum(r["upload"] for r in rows),
            "download": sum(r["download"] for r in rows),
            "rows": rows[:80],
        }

    def _add_site(
        self,
        process: str,
        up: int,
        down: int,
        outbound: str,
        host: str,
        closed: bool = False,
    ) -> None:
        site = site_key(host)
        if not site:
            return
        row = self.programs.setdefault(process, self._blank())
        srow = row["sites"].setdefault(site, self._blank_site())
        srow["upload"] += max(up, 0)
        srow["download"] += max(down, 0)
        if outbound == "proxy":
            srow["proxy_up"] += max(up, 0)
            srow["proxy_down"] += max(down, 0)
        elif outbound == "direct":
            srow["direct_up"] += max(up, 0)
            srow["direct_down"] += max(down, 0)
        if closed:
            srow["conns"] += 1
        srow["last"] = int(time.time())
        sites = row["sites"]
        if len(sites) > 40:
            keep = sorted(
                sites.items(),
                key=lambda kv: -(int(kv[1].get("download") or 0) + int(kv[1].get("upload") or 0)),
            )[:40]
            row["sites"] = dict(keep)

    def _add(
        self,
        process: str,
        up: int,
        down: int,
        outbound: str,
        closed: bool = False,
        host: str = "",
        node: str = "",
    ) -> None:
        name = process.strip() or "未知"
        if name.lower() in CORE_NAMES:
            return
        if up < 0:
            up = 0
        if down < 0:
            down = 0
        if up == 0 and down == 0 and not closed:
            return
        is_new = name not in self.programs
        row = self.programs.setdefault(name, self._blank())
        if is_new:
            self._note_process(name)
        row["upload"] += up
        row["download"] += down
        if outbound == "proxy":
            row["proxy_up"] += up
            row["proxy_down"] += down
            if node:
                row["last_node"] = node
        elif outbound == "direct":
            row["direct_up"] += up
            row["direct_down"] += down
        if closed:
            row["conns"] += 1
        row["last"] = int(time.time())
        self._add_site(name, up, down, outbound, host, closed=closed)
        if up or down:
            self._add_slot(name, up, down, outbound)
            if outbound == "proxy":
                self._add_node_slot(node, up, down)
            self._add_site_slot(host, up, down, outbound)

    def _row(self, flow: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": flow.get("id") or "",
            "process": flow.get("process") or "",
            "remote": flow.get("remote") or "",
            "inbound": flow.get("inbound") or "",
            "outbound": flow.get("outbound") or "",
            "node": flow.get("node") or "",
            "route": flow.get("route") or "",
            "note": flow.get("note") or "",
            "upload": int(flow.get("upload") or 0),
            "download": int(flow.get("download") or 0),
            "start": flow.get("start") or "",
            "end": int(time.time()),
            "closed": True,
            "path": flow.get("path") or "",
        }

    def _append(self, row: dict[str, Any]) -> None:
        try:
            if self.path.is_file() and self.path.stat().st_size > 50 * 1024 * 1024:
                bak = self.path.with_suffix(".jsonl.old")
                if bak.exists():
                    bak.unlink()
                self.path.replace(bak)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            self.logged += 1
        except OSError:
            pass

    def update(self, flows: list[dict[str, Any]]) -> None:
        seen: set[str] = set()
        dirty = False
        acc = {"up": 0, "down": 0, "proxy_up": 0, "proxy_down": 0, "direct_up": 0, "direct_down": 0}
        per: dict[str, list[int]] = {}
        with self.lock:
            for f in flows:
                cid = str(f.get("id") or "")
                if not cid:
                    continue
                seen.add(cid)
                up = int(f.get("upload") or 0)
                down = int(f.get("download") or 0)
                prev = self.last_bytes.get(cid)
                if prev is None:
                    self.last_bytes[cid] = (up, down)
                else:
                    du = up - prev[0]
                    dd = down - prev[1]
                    if du or dd:
                        proc = str(f.get("process") or "未知")
                        ob = str(f.get("outbound") or "")
                        self._add(proc, du, dd, ob, host=str(f.get("remote") or ""), node=str(f.get("node") or ""))
                        dirty = True
                        if du > 0:
                            acc["up"] += du
                        if dd > 0:
                            acc["down"] += dd
                        if ob in {"proxy", "direct"}:
                            if du > 0:
                                acc[f"{ob}_up"] += du
                            if dd > 0:
                                acc[f"{ob}_down"] += dd
                            key = f"{proc}|{ob}"
                            slot = per.setdefault(key, [0, 0])
                            slot[0] += max(du, 0)
                            slot[1] += max(dd, 0)
                    self.last_bytes[cid] = (up, down)
                self.live[cid] = dict(f)
            gone = [k for k in self.live if k not in seen]
            for k in gone:
                rec = self._row(self.live.pop(k))
                self.last_bytes.pop(k, None)
                self._add(rec["process"], 0, 0, rec.get("outbound") or "", closed=True, host=rec.get("remote") or "", node=str(rec.get("node") or ""))
                self.closed.append(rec)
                self._append(rec)
                dirty = True
            if len(self.closed) > 400:
                self.closed = self.closed[-250:]
            now = time.monotonic()
            dt = max(now - self._tick_at, 0.4)
            self._tick_at = now
            self.speed = {k: v / dt for k, v in acc.items()}
            self.proc_speed = {k: {"up": v[0] / dt, "down": v[1] / dt} for k, v in per.items()}
            if dirty:
                self._save_programs()
                self._save_slots()

    def reset(self) -> None:
        with self.lock:
            self.programs = {}
            self.closed = []
            self.history = []
            self.slots = {}
            self.node_slots = {}
            self.site_slots = {}
            self.new_apps = []
            self._save_programs()
            self._save_slots()

    def record_history(self) -> None:
        with self.lock:
            rec = {"t": float(time.time()), **{k: float(v) for k, v in self.speed.items()}}
            self.history.append(rec)
            if len(self.history) > 240:
                self.history = self.history[-240:]

    def history_rows(self) -> list[dict[str, float]]:
        with self.lock:
            return list(self.history)

    def rates(self) -> dict[str, float]:
        with self.lock:
            return dict(self.speed)

    def recent_closed(self, limit: int = 120) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.closed[-limit:])

    def program_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        with self.lock:
            for name, rec in self.programs.items():
                item = {k: v for k, v in rec.items() if k != "sites"}
                item["process"] = name
                sites = rec.get("sites") or {}
                site_rows = [{"site": host, **srec} for host, srec in sites.items()]
                site_rows.sort(
                    key=lambda r: -(int(r.get("download") or 0) + int(r.get("upload") or 0))
                )
                item["sites"] = site_rows
                rows.append(item)
        rows.sort(key=lambda r: -(int(r.get("download") or 0) + int(r.get("upload") or 0)))
        return rows

    def lanes(self) -> dict[str, Any]:
        rows = self.program_rows()
        with self.lock:
            speeds = dict(self.proc_speed)
            rate = dict(self.speed)

        def one(up_k: str, down_k: str, kind: str) -> dict[str, Any]:
            items: list[dict[str, Any]] = []
            tot_up = tot_down = 0
            for p in rows:
                up = int(p.get(up_k) or 0)
                down = int(p.get(down_k) or 0)
                if not up and not down:
                    continue
                sites = []
                for s in p.get("sites") or []:
                    su = int(s.get(up_k) or 0)
                    sd = int(s.get(down_k) or 0)
                    if su or sd:
                        sites.append(
                            {
                                "site": s["site"],
                                "upload": su,
                                "download": sd,
                                "conns": s.get("conns") or 0,
                            }
                        )
                sites.sort(key=lambda r: -(int(r["download"]) + int(r["upload"])))
                sp = speeds.get(f"{p['process']}|{kind}") or {}
                items.append(
                    {
                        "process": p["process"],
                        "upload": up,
                        "download": down,
                        "conns": p.get("conns") or 0,
                        "node": p.get("last_node") or "",
                        "cmd": p.get("last_cmd") or "",
                        "path": p.get("last_path") or "",
                        "up_speed": float(sp.get("up") or 0),
                        "down_speed": float(sp.get("down") or 0),
                        "sites": sites,
                    }
                )
                tot_up += up
                tot_down += down
            items.sort(key=lambda r: -(int(r["download"]) + int(r["upload"])))
            return {
                "upload": tot_up,
                "download": tot_down,
                "up_speed": float(rate.get(f"{kind}_up") or 0),
                "down_speed": float(rate.get(f"{kind}_down") or 0),
                "rows": items,
            }

        return {
            "proxy": one("proxy_up", "proxy_down", "proxy"),
            "direct": one("direct_up", "direct_down", "direct"),
        }


STORE = TrafficStore(TRAFFIC_LOG, PROGRAMS_FILE, SLOTS_FILE)


def route_plen(route: dict[str, str] | None) -> int:
    if not route:
        return -1
    prefix = route.get("DestinationPrefix") or ""
    if "/" not in prefix:
        return -1
    try:
        return int(prefix.split("/", 1)[1])
    except ValueError:
        return -1


def classify(
    conn: dict[str, Any],
    proc: str,
    routes: list[dict[str, str]],
    proxy_ports: set[int],
    local_iface: str,
    tun_up: bool,
    default_is_tun: bool,
) -> tuple[str, str, str]:
    """Return (path, route_label, note)."""
    rip = parse_ip(conn["remote_ip"])
    proc_l = proc.lower()
    is_core = proc_l in CORE_NAMES
    rport = conn["remote_port"]
    lip = parse_ip(conn["local_ip"])

    if conn["remote_ip"] in {"127.0.0.1", "::1"} and rport == 7890:
        return "sysproxy", "loopback", "走 mixed-port 系统代理"
    if isinstance(rip, ipaddress.IPv6Address):
        if rip.is_loopback or rip.is_link_local:
            return "local", "v6-local", "本机 IPv6"
        if is_core:
            return "direct", local_iface or "v6-core", "内核 IPv6 出口"
        return "leak", local_iface or "v6", "IPv6 未进 TUN，可能漏出"
    if not isinstance(rip, ipaddress.IPv4Address):
        return "local", "?", "无法解析地址"

    if isinstance(lip, ipaddress.IPv4Address) and in_nets(lip, FAKE_IP_NETS):
        if in_nets(rip, FAKE_IP_NETS):
            return "internal", "FlClash", "Wintun 内部会话"
        return "tun", "FlClash", "已进 TUN"

    if in_nets(rip, FAKE_IP_NETS):
        if is_core:
            return "internal", "FlClash", "Wintun 内部会话"
        return "tun", "FlClash", "Fake-IP，已进 TUN"

    route = longest_prefix(rip, routes)
    alias = (route or {}).get("InterfaceAlias") or ""
    kind = iface_kind(alias)
    bound = iface_kind(local_iface) if local_iface else ""
    plen = route_plen(route)
    label = local_iface or alias or "?"

    if in_nets(rip, PRIVATE_HINT):
        return "local", label, "局域网 / 本机"

    if kind == "vpn" or bound == "vpn":
        return "leak", label, "公司 VPN 旁路（aTrust）"

    if is_core and rport in proxy_ports:
        return "proxy", label, f"节点隧道 :{rport}"
    if is_core:
        if conn["remote_ip"] in CORE_DNS_IPS:
            return "dns", label, "Clash 自己的 DNS"
        return "direct", label, "规则直连出口"

    # Windows 常把套接字显示成 WLAN 地址，并不代表绕过了 TUN。
    # 只有「更具体的非 TUN 路由」才算漏出。
    if kind in {"wifi", "nic"} and plen > 0:
        return "leak", label, "更具体路由绕过 TUN"
    if kind == "tun" or bound == "tun" or (tun_up and default_is_tun):
        return "tun", label, "已进 TUN"
    if kind in {"wifi", "nic"}:
        return "leak", label, "未走 TUN 的公网连接"
    return "tun", label, "已进 TUN"


def attach_original_process(flows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """DIRECT 出口是 FlClashCore 代发的，用相同远端把应用侧原进程贴回去。"""
    tun_by_remote: dict[str, list[int]] = {}
    core_by_remote: dict[str, list[int]] = {}
    for i, f in enumerate(flows):
        proc = f["process"].lower()
        if f["path"] in {"tun", "leak"} and proc not in CORE_NAMES:
            tun_by_remote.setdefault(f["remote"], []).append(i)
        elif f["path"] == "direct" and proc in CORE_NAMES:
            core_by_remote.setdefault(f["remote"], []).append(i)

    used: set[int] = set()
    rewritten: list[dict[str, Any]] = []

    for remote, core_idxs in core_by_remote.items():
        sample = flows[core_idxs[0]]
        app_idxs = list(tun_by_remote.get(remote) or [])
        if not app_idxs:
            ip = remote.rsplit(":", 1)[0]
            for other, idxs in tun_by_remote.items():
                if other.rsplit(":", 1)[0] == ip:
                    app_idxs.extend(idxs)
        app_idxs = [i for i in app_idxs if i not in used]
        used.update(core_idxs)
        if not app_idxs:
            # 对不上应用就把内核行丢掉，避免直连列表全是 FlClashCore
            continue
        for i in app_idxs:
            used.add(i)
            a = flows[i]
            rewritten.append(
                {
                    "path": "direct",
                    "inbound": a.get("inbound") or "tun",
                    "outbound": "direct",
                    "node": "",
                    "process": a["process"],
                    "pid": a["pid"],
                    "local": a["local"],
                    "remote": remote,
                    "route": sample["route"],
                    "note": "Clash 直连（原进程）",
                }
            )

    out = list(rewritten)
    for i, f in enumerate(flows):
        if i not in used:
            out.append(f)
    return out


def snapshot(proxy_ports: set[int]) -> dict[str, Any]:
    t0 = time.perf_counter()
    netinfo = collect_routes()
    names = parse_tasklist()
    conns = parse_netstat()
    payload = try_clash_payload()
    clash_conns = payload["connections"]

    routes = netinfo.get("routes") or []
    if isinstance(routes, dict):
        routes = [routes]
    addrs = netinfo.get("addrs") or []
    if isinstance(addrs, dict):
        addrs = [addrs]
    ip_to_iface = {
        str(a.get("IPAddress")): str(a.get("InterfaceAlias") or "")
        for a in addrs
        if a.get("IPAddress")
    }

    defaults = netinfo.get("defaults") or []
    if isinstance(defaults, dict):
        defaults = [defaults]
    v4def = next((d for d in defaults if d.get("DestinationPrefix") == "0.0.0.0/0"), None)
    default_v4 = ""
    if v4def:
        default_v4 = f"{v4def.get('InterfaceAlias')} → {v4def.get('NextHop')}"

    tun_up = str(netinfo.get("tunStatus") or "").lower() in {"up"}
    default_is_tun = bool(v4def) and iface_kind(str(v4def.get("InterfaceAlias") or "")) == "tun"

    flows = []
    nodes: list[str] = []
    for c in conns:
        proc = names.get(c["pid"]) or f"pid:{c['pid']}"
        local_iface = ip_to_iface.get(c["local_ip"], "")
        path, route, note = classify(
            c, proc, routes, proxy_ports, local_iface, tun_up, default_is_tun
        )
        remote = f"{c['remote_ip']}:{c['remote_port']}"
        if path == "internal":
            continue
        inbound = path if path in {"sysproxy", "tun", "leak", "local"} else ""
        outbound = path if path in {"proxy", "direct"} else ("unknown" if path == "sysproxy" else "")
        note_out = note
        if path == "sysproxy":
            note_out = "进了 7890，最终直连还是走节点要 Clash 连接接口才能看"
        flows.append(
            {
                "path": path,
                "inbound": inbound,
                "outbound": outbound,
                "node": "",
                "process": proc,
                "pid": c["pid"],
                "local": f"{c['local_ip']}:{c['local_port']}",
                "remote": remote,
                "route": route,
                "note": note_out,
                "upload": 0,
                "download": 0,
                "closed": False,
            }
        )

    flows = attach_original_process(flows)
    if clash_conns:
        keep = [f for f in flows if f["path"] in {"leak", "local", "dns"}]
        api_flows = [clash_to_flow(c) for c in clash_conns]
        local_of = {f"{c['local_ip']}:{c['local_port']}": c["pid"] for c in conns}
        pids: set[int] = set()
        want_cmd = {"node", "python", "pythonw", "pwsh", "powershell", "cmd", "bash", "java", "ruby", "dotnet"}
        for f in api_flows:
            if not f.get("pid"):
                f["pid"] = int(local_of.get(str(f.get("local") or ""), 0) or 0)
            if f.get("pid") and str(f.get("process") or "").lower() in want_cmd:
                pids.add(int(f["pid"]))
        cmds = lookup_cmdlines(pids)
        for f in api_flows:
            cmd = cmds.get(int(f.get("pid") or 0), "")
            if cmd:
                f["cmd"] = cmd
        STORE.update(api_flows)
        for f in api_flows:
            STORE.remember_origin(str(f.get("process") or ""), str(f.get("cmd") or ""), str(f.get("proc_path") or ""))
        flows = api_flows + keep
        for f in api_flows:
            n = str(f.get("node") or "")
            if n and n not in nodes:
                nodes.append(n)

    order = {"proxy": 0, "leak": 1, "direct": 2, "tun": 3, "sysproxy": 4, "local": 5}
    counts = {
        "proxy": sum(1 for f in flows if f.get("outbound") == "proxy" or f["path"] == "proxy"),
        "direct": sum(1 for f in flows if f.get("outbound") == "direct" or f["path"] == "direct"),
        "tun": sum(1 for f in flows if f.get("inbound") == "tun" or f["path"] == "tun"),
        "leak": sum(1 for f in flows if f["path"] == "leak"),
        "sysproxy": sum(1 for f in flows if f.get("inbound") == "sysproxy" or f["path"] == "sysproxy"),
        "local": sum(1 for f in flows if f["path"] == "local"),
    }
    collapsed: dict[tuple, dict[str, Any]] = {}
    for f in flows:
        key = (
            f["path"],
            f.get("inbound", ""),
            f.get("outbound", ""),
            f.get("node", ""),
            f["process"],
            f["pid"],
            f["remote"],
            f["route"],
            f["note"],
        )
        item = collapsed.get(key)
        if item is None:
            ids = [str(f.get("id") or "")] if f.get("id") else []
            collapsed[key] = {**f, "n": 1, "ids": ids}
        else:
            item["n"] += 1
            item["upload"] = int(item.get("upload") or 0) + int(f.get("upload") or 0)
            item["download"] = int(item.get("download") or 0) + int(f.get("download") or 0)
            cid = str(f.get("id") or "")
            if cid:
                item.setdefault("ids", []).append(cid)
    flows = list(collapsed.values())
    for f in flows:
        if f["n"] > 1:
            f["note"] = f"{f['note']} ×{f['n']}"
    flows.sort(
        key=lambda f: (
            -(int(f.get("download") or 0) + int(f.get("upload") or 0)),
            order.get(f["path"], 9),
            f["process"],
        )
    )
    closed = STORE.recent_closed()
    lanes = STORE.lanes()
    live_n: dict[tuple[str, str], int] = {}
    for f in flows:
        ob = str(f.get("outbound") or "")
        if ob not in {"proxy", "direct"}:
            continue
        key = (str(f.get("process") or ""), ob)
        live_n[key] = live_n.get(key, 0) + int(f.get("n") or 1)
    for kind in ("proxy", "direct"):
        for row in lanes[kind]["rows"]:
            row["live"] = live_n.get((row["process"], kind), 0)
    rates = STORE.rates()
    STORE.record_history()
    rank = {
        "1h": STORE.ranking("1h"),
        "today": STORE.ranking("today"),
        "7d": STORE.ranking("7d"),
        "30d": STORE.ranking("30d"),
        "all": STORE.ranking("all"),
    }
    leaks = leak_summary(flows)
    return {
        "ts": int(time.time()),
        "ms": int((time.perf_counter() - t0) * 1000),
        "status": {
            "tun_up": tun_up,
            "tun_name": netinfo.get("tunName") or "FlClash",
            "tun_ip": netinfo.get("tunIp") or "",
            "default_v4": default_v4,
            "nodes": nodes[:4],
            "clash_api": bool(clash_conns),
            "proxy_ports": sorted(proxy_ports)[:12],
            "upload_total": payload.get("uploadTotal") or 0,
            "download_total": payload.get("downloadTotal") or 0,
            "logged": STORE.logged,
            "up_speed": rates.get("up") or 0,
            "down_speed": rates.get("down") or 0,
            "leak_n": leaks["count"],
        },
        "counts": counts,
        "flows": flows,
        "closed": closed,
        "programs": STORE.program_rows(),
        "lanes": lanes,
        "history": STORE.history_rows(),
        "rank": rank,
        "leaks": leaks,
        "alerts": build_alerts(rank["today"], leaks, STORE.recent_new()),
    }


def export_csv() -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["lane", "process", "site", "upload", "download"])
    lanes = STORE.lanes()
    for kind in ("proxy", "direct"):
        for p in (lanes.get(kind) or {}).get("rows") or []:
            w.writerow([kind, p.get("process") or "", "", int(p.get("upload") or 0), int(p.get("download") or 0)])
            for s in p.get("sites") or []:
                w.writerow(
                    [
                        kind,
                        p.get("process") or "",
                        s.get("site") or "",
                        int(s.get("upload") or 0),
                        int(s.get("download") or 0),
                    ]
                )
    return buf.getvalue().encode("utf-8-sig")


def leak_summary(flows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, dict[str, Any]] = {}
    for f in flows:
        if f.get("path") != "leak":
            continue
        name = str(f.get("process") or "?")
        rec = grouped.setdefault(
            name,
            {"process": name, "n": 0, "note": str(f.get("note") or ""), "remote": str(f.get("remote") or "")},
        )
        rec["n"] += int(f.get("n") or 1)
    rows = sorted(grouped.values(), key=lambda r: -int(r["n"]))
    return {"count": sum(int(r["n"]) for r in rows), "rows": rows[:12]}


def quota_alerts(today: dict[str, Any]) -> list[dict[str, Any]]:
    total_mb = int(SETTINGS.data.get("quota_total_mb") or 0)
    prog_mb = int(SETTINGS.data.get("quota_program_mb") or 0)
    alerts: list[dict[str, Any]] = []
    used_total = int(today.get("upload") or 0) + int(today.get("download") or 0)
    if total_mb > 0:
        limit = total_mb * 1024 * 1024
        if used_total >= limit:
            alerts.append(
                {
                    "kind": "total",
                    "process": "",
                    "used": used_total,
                    "limit": limit,
                }
            )
    if prog_mb > 0:
        limit = prog_mb * 1024 * 1024
        for row in today.get("rows") or []:
            used = int(row.get("upload") or 0) + int(row.get("download") or 0)
            if used >= limit:
                alerts.append(
                    {
                        "kind": "program",
                        "process": str(row.get("process") or ""),
                        "used": used,
                        "limit": limit,
                    }
                )
            if len(alerts) >= 8:
                break
    return alerts


def build_alerts(today: dict[str, Any], leaks: dict[str, Any], new_apps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    n = int(leaks.get("count") or 0)
    if n:
        names = [str(r.get("process") or "") for r in (leaks.get("rows") or [])[:4] if r.get("process")]
        alerts.append({"kind": "leak", "count": n, "names": names})
    for rec in new_apps[-6:]:
        alerts.append({"kind": "new", "process": str(rec.get("process") or ""), "ts": int(rec.get("ts") or 0)})
    alerts.extend(quota_alerts(today))
    return alerts[:12]


class Handler(BaseHTTPRequestHandler):
    cache: dict[str, Any] = {}
    lock = threading.Lock()

    def log_message(self, fmt: str, *args: Any) -> None:
        if args and str(args[0]).startswith("GET /api"):
            return
        stream = sys.stderr or sys.stdout
        if not stream:
            return
        stream.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, Any]:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/" or path.startswith("/index"):
            self._send(200, load_page().encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/snapshot":
            with self.lock:
                data = self.cache.get("snap") or {"error": "warming"}
            self._send(200, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return
        if path == "/api/settings":
            self._send(200, json.dumps(SETTINGS.public(), ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return
        if path == "/api/export.csv":
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", "attachment; filename=tun-split.csv")
            body = export_csv()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/reset":
            STORE.reset()
            with Handler.lock:
                snap = Handler.cache.get("snap")
                if isinstance(snap, dict):
                    snap = dict(snap)
                    snap["lanes"] = STORE.lanes()
                    snap["programs"] = []
                    snap["closed"] = []
                    snap["history"] = STORE.history_rows()
                    Handler.cache["snap"] = snap
            self._send(200, b'{"ok":true}', "application/json; charset=utf-8")
            return
        if path == "/api/settings":
            SETTINGS.apply(self._read_json_body())
            self._send(200, json.dumps(SETTINGS.public(), ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return
        if path == "/api/open-data":
            try:
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                os.startfile(DATA_DIR)  # noqa: S606
            except OSError:
                self._send(500, b'{"ok":false}', "application/json; charset=utf-8")
                return
            self._send(200, b'{"ok":true}', "application/json; charset=utf-8")
            return
        if path == "/api/close":
            body = self._read_json_body()
            ids = body.get("ids")
            if not isinstance(ids, list):
                one = body.get("id")
                ids = [one] if one else []
            ok = 0
            for cid in ids:
                if close_clash_connection(str(cid)):
                    ok += 1
            payload = json.dumps({"ok": ok > 0, "closed": ok}, ensure_ascii=False).encode("utf-8")
            self._send(200 if ok else 502, payload, "application/json; charset=utf-8")
            return
        if path == "/api/ack-new":
            body = self._read_json_body()
            STORE.ack_new(str(body.get("process") or ""))
            self._send(200, b'{"ok":true}', "application/json; charset=utf-8")
            return
        self.send_error(404)


def poller(ports: set[int], stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            snap = snapshot(ports)
            with Handler.lock:
                Handler.cache["snap"] = snap
        except Exception as exc:
            with Handler.lock:
                Handler.cache["snap"] = {"error": str(exc), "ts": int(time.time()), "flows": [], "counts": {}, "status": {}}
        stop.wait(2.0)


class WinTray:
    """Windows notify-icon on a hidden window; no third-party package."""

    ID_OPEN = 1001
    ID_SHOW = 1002
    ID_QUIT = 1003
    WM_TRAY = 0x8001

    def __init__(self, on_open, on_show, on_quit) -> None:
        self.on_open = on_open
        self.on_show = on_show
        self.on_quit = on_quit
        self.hwnd = None
        self._nid = None
        self._wndproc = None
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self._ready = threading.Event()
        self._thread.start()
        self._ready.wait(3)

    def _run(self) -> None:
        import ctypes
        from ctypes import wintypes

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
        shell32.ExtractIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT]
        shell32.ExtractIconW.restype = ctypes.c_void_p
        icon = shell32.ExtractIconW(hinst, sys.executable, 0) or 0
        if not icon:
            user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
            user32.LoadIconW.restype = wintypes.HANDLE
            icon = user32.LoadIconW(None, ctypes.c_void_p(32512))

        def popup_menu() -> None:
            menu = user32.CreatePopupMenu()
            user32.AppendMenuW(menu, 0, self.ID_OPEN, "打开页面")
            user32.AppendMenuW(menu, 0, self.ID_SHOW, "显示窗口")
            user32.AppendMenuW(menu, 0, self.ID_QUIT, "退出")
            user32.SetForegroundWindow(hwnd)
            pt = POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            cmd = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, pt.x, pt.y, 0, hwnd, None)
            user32.DestroyMenu(menu)
            user32.PostMessageW(hwnd, WM_COMMAND, cmd, 0)

        def handle_command(cmd: int) -> None:
            if cmd == self.ID_OPEN:
                self.on_open()
            elif cmd == self.ID_SHOW:
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
        self._class_name = f"{APP_ID}Tray"
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
            APP_NAME,
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
        nid.szTip = APP_NAME
        self._nid = nid
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        self._ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))

    def balloon(self, title: str, text: str) -> None:
        import ctypes
        from ctypes import wintypes  # noqa: F401

        if self._nid is None or not self.hwnd:
            return
        nid = self._nid
        nid.uFlags = 0x01 | 0x02 | 0x04 | 0x10
        nid.szInfoTitle = title[:63]
        nid.szInfo = text[:255]
        nid.dwInfoFlags = 1
        ctypes.windll.shell32.Shell_NotifyIconW(1, ctypes.byref(nid))

    def stop(self) -> None:
        import ctypes

        if self.hwnd:
            ctypes.windll.user32.PostMessageW(self.hwnd, 0x0010, 0, 0)  # WM_CLOSE
            ctypes.windll.user32.PostMessageW(self.hwnd, 0x0012, 0, 0)  # WM_QUIT


def run_gui(url: str, stop: threading.Event, httpd: ThreadingHTTPServer, minimized: bool = False) -> None:
    import tkinter as tk

    root = tk.Tk()
    root.title(APP_NAME)
    root.resizable(False, False)
    if not minimized:
        try:
            root.attributes("-topmost", True)
            root.after(400, lambda: root.attributes("-topmost", False))
        except tk.TclError:
            pass
    frm = tk.Frame(root, padx=18, pady=16)
    frm.pack()
    tk.Label(frm, text=f"{APP_NAME} 正在运行", font=("Segoe UI", 12, "bold")).pack(anchor="w")
    tk.Label(frm, text=url, fg="#1a5fb4").pack(anchor="w", pady=(2, 4))
    tk.Label(frm, text="关闭窗口会藏到托盘，采集不停。", fg="#6b6b6b").pack(anchor="w", pady=(0, 12))
    btns = tk.Frame(frm)
    btns.pack(anchor="w")
    balloon_once = {"done": False}

    def hop(fn):
        def wrapped(*_a, **_k) -> None:
            try:
                root.after(0, fn)
            except tk.TclError:
                fn()

        return wrapped

    def show_window() -> None:
        try:
            root.deiconify()
            root.lift()
            root.state("normal")
        except tk.TclError:
            pass

    def open_page() -> None:
        webbrowser.open(url)
        show_window()

    def hide_to_tray() -> None:
        try:
            root.withdraw()
        except tk.TclError:
            return
        if not balloon_once["done"]:
            balloon_once["done"] = True
            try:
                tray.balloon(f"{APP_NAME} 还在采集", "点右下角托盘图标可打开窗口。")
            except Exception:
                pass

    def on_close() -> None:
        if SETTINGS.data.get("close_to_tray", True):
            hide_to_tray()
        else:
            quit_app()

    def quit_app() -> None:
        stop.set()
        try:
            tray.stop()
        except Exception:
            pass
        threading.Thread(target=httpd.shutdown, daemon=True).start()
        try:
            root.destroy()
        except tk.TclError:
            pass

    tray = WinTray(on_open=hop(open_page), on_show=hop(show_window), on_quit=hop(quit_app))

    tk.Button(btns, text="打开页面", width=12, command=open_page).pack(side="left")
    tk.Button(btns, text="藏到托盘", width=12, command=hide_to_tray).pack(side="left", padx=(8, 0))
    tk.Button(btns, text="退出", width=12, command=quit_app).pack(side="left", padx=(8, 0))
    root.protocol("WM_DELETE_WINDOW", on_close)
    if minimized or SETTINGS.data.get("start_minimized"):
        root.after(80, hide_to_tray)
    root.mainloop()


def main() -> int:
    parser = argparse.ArgumentParser(description=f"{APP_NAME}：轻量监视 FlClash / mihomo TUN 分流")
    parser.add_argument("--port", type=int, default=LISTEN_PORT)
    parser.add_argument("--once", action="store_true", help="打印一次 JSON 后退出")
    parser.add_argument("--gui", action="store_true", help="弹出控制窗口（打包 exe 时默认开启）")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--minimized", action="store_true")
    args = parser.parse_args()
    if autostart_enabled():
        try:
            set_autostart(True)
        except RuntimeError:
            pass
        SETTINGS.data["autostart"] = True
    global ACTUAL_PORT
    ACTUAL_PORT = args.port
    ports = load_proxy_ports(CLASH_CONFIG)
    if args.once:
        print(json.dumps(snapshot(ports), ensure_ascii=False, indent=2))
        return 0
    stop = threading.Event()
    threading.Thread(target=poller, args=(ports, stop), daemon=True).start()
    httpd = ThreadingHTTPServer((LISTEN_HOST, args.port), Handler)
    url = f"http://{LISTEN_HOST}:{args.port}/"
    use_gui = frozen() or args.gui
    open_browser = bool(SETTINGS.data.get("open_browser", True)) and not args.no_browser
    minimized = bool(args.minimized or SETTINGS.data.get("start_minimized"))
    if use_gui:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        if open_browser:
            webbrowser.open(url)
        run_gui(url, stop, httpd, minimized=minimized)
        stop.set()
        try:
            httpd.shutdown()
        except Exception:
            pass
        return 0
    print(f"{APP_NAME}  {url}", flush=True)
    print("仅本机可访问。托盘图标可打开页面；Ctrl+C 退出。", flush=True)
    tray = None
    if sys.platform == "win32":
        try:
            tray = WinTray(
                on_open=lambda: webbrowser.open(url),
                on_show=lambda: webbrowser.open(url),
                on_quit=lambda: (stop.set(), threading.Thread(target=httpd.shutdown, daemon=True).start()),
            )
        except Exception:
            tray = None
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        stop.set()
        httpd.shutdown()
    finally:
        if tray is not None:
            try:
                tray.stop()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
