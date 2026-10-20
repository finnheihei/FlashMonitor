"""连接分类：判断一条连接走代理、直连、TUN、系统代理还是漏网。纯函数，无副作用。"""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any

from .parsing import DEFAULT_MIXED_PORTS, as_int

FAKE_IP_NETS = (ipaddress.ip_network("198.18.0.0/15"),)
PRIVATE_HINT = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
)
CORE_NAMES = frozenset(
    {
        "flclashcore",
        "flclash",
        "flclashhelperservice",
        "clash-meta",
        "mihomo",
        "verge-mihomo",
        "clash-verge",
        "clash verge",
    }
)
CORE_DNS_IPS = frozenset(
    {
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
)
SYS_TYPES = frozenset({"HTTP", "HTTPS", "SOCKS", "SOCKS5", "MIXED", "HTTPCONNECT"})
DIRECT_OUT = frozenset({"DIRECT", "REJECT", "PASS", "REJECT-DROP"})


def is_core(name: str) -> bool:
    return (name or "").lower() in CORE_NAMES


def parse_ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def in_nets(ip: ipaddress._BaseAddress, nets: tuple[ipaddress._BaseNetwork, ...]) -> bool:
    return any(ip in n for n in nets)


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


def classify(
    conn: dict[str, Any],
    proc: str,
    routes: list[dict[str, str]],
    proxy_ports: set[int],
    local_iface: str,
    tun_up: bool,
    default_is_tun: bool,
    mixed_ports: set[int],
) -> tuple[str, str, str]:
    """返回 (path, route_label, note)。"""
    rip = parse_ip(conn["remote_ip"])
    core = is_core(proc)
    rport = conn["remote_port"]
    lip = parse_ip(conn["local_ip"])
    mixed = mixed_ports or DEFAULT_MIXED_PORTS

    if conn["remote_ip"] in {"127.0.0.1", "::1"} and rport in mixed:
        return "sysproxy", "loopback", "走 mixed-port 系统代理"
    if isinstance(rip, ipaddress.IPv6Address):
        if rip.is_loopback or rip.is_link_local:
            return "local", "v6-local", "本机 IPv6"
        if core:
            return "direct", local_iface or "v6-core", "内核 IPv6 出口"
        if tun_up:
            return "leak", local_iface or "v6", "IPv6 未进 TUN，可能漏出"
        return "bypass", local_iface or "v6", "IPv6 直出（未开 TUN）"
    if not isinstance(rip, ipaddress.IPv4Address):
        return "local", "?", "无法解析地址"

    if isinstance(lip, ipaddress.IPv4Address) and in_nets(lip, FAKE_IP_NETS):
        if in_nets(rip, FAKE_IP_NETS):
            return "internal", "FlClash", "Wintun 内部会话"
        return "tun", "FlClash", "已进 TUN"

    if in_nets(rip, FAKE_IP_NETS):
        if core:
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
        if tun_up:
            return "leak", label, "公司 VPN 旁路（aTrust）"
        return "bypass", label, "公司 VPN（未开 TUN）"

    if core and rport in proxy_ports:
        return "proxy", label, f"节点隧道 :{rport}"
    if core:
        if conn["remote_ip"] in CORE_DNS_IPS:
            return "dns", label, "Clash 自己的 DNS"
        return "direct", label, "规则直连出口"

    # Windows 常把套接字显示成 WLAN 地址，并不代表绕过了 TUN。
    # 只有开了 TUN 且「更具体的非 TUN 路由」才算漏出。
    if kind in {"wifi", "nic"} and plen > 0:
        if tun_up:
            return "leak", label, "更具体路由绕过 TUN"
        return "bypass", label, "直出（未开 TUN）"
    if kind == "tun" or bound == "tun" or (tun_up and default_is_tun):
        return "tun", label, "已进 TUN"
    if kind in {"wifi", "nic"}:
        if tun_up:
            return "leak", label, "未走 TUN 的公网连接"
        return "bypass", label, "直出（未开 TUN）"
    if tun_up:
        return "tun", label, "已进 TUN"
    return "bypass", label, "直出（未开 TUN）"


def attach_original_process(flows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only associate a unique TUN/application and core pair at the exact remote endpoint.

    Multiple candidates, different ports, and known bypass flows cannot establish ownership.
    This is a socket-table association; the controller's process metadata remains preferred.
    """
    tun_by_remote: dict[str, list[int]] = {}
    core_by_remote: dict[str, list[int]] = {}
    for i, f in enumerate(flows):
        proc = f["process"].lower()
        if f["path"] == "tun" and proc not in CORE_NAMES:
            tun_by_remote.setdefault(f["remote"], []).append(i)
        elif f["path"] == "direct" and proc in CORE_NAMES:
            core_by_remote.setdefault(f["remote"], []).append(i)

    used: set[int] = set()
    rewritten: list[dict[str, Any]] = []

    for remote, core_idxs in core_by_remote.items():
        sample = flows[core_idxs[0]]
        app_idxs = list(tun_by_remote.get(remote) or [])
        app_idxs = [i for i in app_idxs if i not in used]
        used.update(core_idxs)
        if len(app_idxs) != 1 or len(core_idxs) != 1:
            # 对不上应用就把内核行丢掉，避免直连列表全是 FlClashCore
            continue
        for i in app_idxs:
            used.add(i)
            a = flows[i]
            rewritten.append(
                {
                    **a,
                    "path": "direct",
                    "inbound": a.get("inbound") or "tun",
                    "outbound": "direct",
                    "node": "",
                    "process": a["process"],
                    "pid": a["pid"],
                    "local": a["local"],
                    "remote": remote,
                    "route": sample["route"],
                    "note": "Clash 直连（唯一端点关联）",
                    "attribution": "unique_remote_endpoint",
                }
            )

    out = list(rewritten)
    for i, f in enumerate(flows):
        if i not in used:
            out.append(f)
    return out


def chain_outbound(chains: list[str]) -> tuple[str, str]:
    """Clash 的 chains[0] 是最终出站：节点名，或 DIRECT / REJECT。"""
    if not chains:
        return "unknown", ""
    leaf = chains[0]
    if leaf.upper() == "DIRECT":
        return "direct", leaf
    if leaf.upper() in {"REJECT", "REJECT-DROP"}:
        return "rejected", leaf
    if leaf.upper() == "PASS" or not leaf.strip():
        return "unknown", leaf
    return "proxy", leaf


def clash_to_flow(conn: dict[str, Any]) -> dict[str, Any]:
    # 字段类型不对时按空处理：一条坏数据不该让这一秒的所有连接都算不上
    meta = conn.get("metadata")
    if not isinstance(meta, dict):
        meta = {}
    raw_chains = conn.get("chains")
    # A malformed leaf is unknown, never a node named "None" / "False".
    # Do not filter invalid entries: doing so would promote a parent group to leaf.
    chains = raw_chains if isinstance(raw_chains, list) and all(isinstance(c, str) for c in raw_chains) else []
    typ = str(meta.get("type") or "").upper()
    src = str(meta.get("sourceIP") or "")
    if typ in SYS_TYPES or src in {"127.0.0.1", "::1"}:
        inbound = "sysproxy"
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
        remote = f"[{remote}]:{dport}" if ":" in remote and not remote.startswith("[") else f"{remote}:{dport}"
    rule = " ".join(x for x in (str(conn.get("rule") or ""), str(conn.get("rulePayload") or "")) if x)
    note = " · ".join(x for x in (typ, f"{via or outbound}", rule) if x)
    path = outbound if outbound in {"proxy", "direct", "rejected"} else inbound
    return {
        "id": str(conn.get("id") or ""),
        "path": path,
        "inbound": inbound,
        "outbound": outbound,
        "node": via if outbound == "proxy" else "",
        "process": proc,
        "pid": 0,
        "proc_path": str(meta.get("processPath") or ""),
        "rule_exe": Path(raw_proc.replace("\\", "/")).name if raw_proc.lower().endswith(".exe") else "",
        "local": f"{meta.get('sourceIP', '')}:{meta.get('sourcePort', '')}",
        "remote": remote,
        "host": host,
        "destination_ip": dip,
        "destination_port": int(dport) if dport.isascii() and dport.isdigit() and len(dport) <= 5 and 1 <= int(dport) <= 65535 else None,
        "network": str(meta.get("network") or ""),
        "route": via or typ,
        "note": note,
        "upload": as_int(conn.get("upload")),
        "download": as_int(conn.get("download")),
        "start": str(conn.get("start") or ""),
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
