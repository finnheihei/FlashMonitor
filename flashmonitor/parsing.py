"""纯文本 / 数值解析：版本号、订阅流量头、netstat / tasklist 输出、Clash 配置端口。无副作用。"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Any

DEFAULT_MIXED_PORTS = frozenset({7890})

_LISTEN_PORT_RE = re.compile(
    r"^(mixed-port|port|socks-port|redir-port|tproxy-port):\s*['\"]?(\d+)",
    re.I,
)
_PROXY_PORT_RE = re.compile(r"(?:^|\s)port:\s*['\"]?(\d+)")
_LOOPBACK_PROXY_RE = re.compile(r"(?:127\.0\.0\.1|localhost|\[::1\]):(\d{1,5})", re.I)


def as_int(v: Any) -> int:
    if v is None or v is False:
        return 0
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        try:
            return int(float(v.strip() or 0))
        except ValueError:
            return 0
    if isinstance(v, dict):
        for k in ("Value", "value", "Int64", "int64"):
            if k in v:
                return as_int(v[k])
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _port(text: str) -> int:
    try:
        return int(text or 0)
    except ValueError:
        return 0


def split_endpoint(ep: str) -> tuple[str, int]:
    ep = ep.strip()
    if ep.startswith("["):
        host, _, port = ep[1:].partition("]")
        return host, _port(port[1:])
    if ep.count(":") == 1:
        host, _, port = ep.rpartition(":")
        return host, _port(port)
    # 不带方括号的 IPv6：最后一个冒号后是数字才算端口
    host, _, port = ep.rpartition(":")
    try:
        return host.strip("[]"), int(port)
    except ValueError:
        return ep, 0


def shorten_cmd(cmd: str) -> str:
    cmd = " ".join(str(cmd or "").split())
    if len(cmd) <= 180:
        return cmd
    return cmd[:80] + " … " + cmd[-80:]


def parse_version(text: str) -> tuple[int, int, int]:
    raw = (text or "").strip().lstrip("vV")
    parts: list[int] = []
    for bit in raw.split("."):
        digits = ""
        for ch in bit:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits or 0))
        if len(parts) == 3:
            break
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def parse_subscription_userinfo(header: str) -> dict[str, int] | None:
    text = (header or "").strip()
    if not text:
        return None
    out = {"upload": 0, "download": 0, "total": 0, "expire": 0}
    found = False
    for part in text.replace(",", ";").split(";"):
        if "=" not in part:
            continue
        key, raw = part.split("=", 1)
        key = key.strip().lower()
        if key not in out:
            continue
        try:
            out[key] = int(float(raw.strip().strip('"')))
            found = True
        except ValueError:
            continue
    return out if found else None


def parse_tasklist_csv(text: str) -> dict[int, str]:
    """`tasklist /fo csv /nh` 输出 → {pid: 进程名（去掉 .exe）}。"""
    names: dict[int, str] = {}
    for row in csv.reader(io.StringIO(text or "")):
        if len(row) < 2:
            continue
        name = row[0].removesuffix(".exe")
        pid_s = row[1].replace(",", "").strip()
        if pid_s.isdigit():
            names[int(pid_s)] = name
    return names


def parse_netstat_text(text: str) -> list[dict[str, Any]]:
    """`netstat -ano` 输出 → 已建立的 TCP 连接。"""
    rows: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
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


def proxy_server_ports(server: str) -> set[int]:
    """注册表 ProxyServer 字符串里指向本机的端口。"""
    ports: set[int] = set()
    for m in _LOOPBACK_PROXY_RE.finditer(server or ""):
        n = int(m.group(1))
        if 1 <= n <= 65535:
            ports.add(n)
    return ports


def load_proxy_ports(config: Path) -> set[int]:
    """Clash 配置 proxies: 段里节点的端口（识别内核连节点的隧道）。"""
    ports: set[int] = set()
    if not config.is_file():
        return ports
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ports
    in_proxies = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("proxies:"):
            in_proxies = True
            continue
        if in_proxies and (line.startswith("proxy-groups:") or line.startswith("rules:")):
            break
        if in_proxies:
            m = _PROXY_PORT_RE.search(line)
            if m:
                ports.add(int(m.group(1)))
    return ports


def parse_listen_ports(text: str) -> set[int]:
    """Clash 配置顶层的 mixed-port / port / socks-port 等（加上默认 7890）。"""
    ports = set(DEFAULT_MIXED_PORTS)
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith(("proxies:", "proxy-groups:", "rules:")):
            break
        m = _LISTEN_PORT_RE.match(line)
        if not m:
            continue
        n = int(m.group(2))
        if 1 <= n <= 65535:
            ports.add(n)
    return ports


def load_listen_ports(config: Path) -> set[int]:
    if not config.is_file():
        return set(DEFAULT_MIXED_PORTS)
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set(DEFAULT_MIXED_PORTS)
    return parse_listen_ports(text)


def fmt_speed_text(n: float) -> str:
    n = float(n or 0)
    if n < 1:
        return "0"
    units = ["B", "KB", "MB", "GB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    body = str(int(round(n))) if i == 0 else f"{n:.1f}"
    return f"{body} {units[i]}/s"
