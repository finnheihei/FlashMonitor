"""采集：每秒一次的 Clash 采样、每两秒一次的连接分类汇总，以及提醒、导出和写盘线程。

- 采样线程（clash_sampler）：GET /connections → TrafficStore.update()，算速率和累计；
- 实时流量（traffic_reader）：/traffic 流给出整机上下行；
- 采集线程（poller）：本机连接表 + 最近一次 Clash 数据 → 分类、合并 → LiveState；
- 写盘线程（store_flusher）：每 10 秒 TrafficStore.flush()。
每个线程的循环体都接住异常写日志，不会因为一次坏数据就停掉（0.8.0 的采样线程会）。
"""

from __future__ import annotations

import csv
import io
import json
import threading
import time
from typing import Any, Callable, Iterable, Mapping
from urllib.request import Request

from . import APP_VERSION
from . import log as fmlog
from .classify import attach_original_process, clash_to_flow, classify, iface_kind
from .clash import ClashClient
from .netinfo import CmdlineCache, FileMemo, RouteCache, as_list, process_names, route_addrs, tcp_connections, windows_proxy_info
from .parsing import DEFAULT_MIXED_PORTS, as_int
from .process_identity import IdentityReader, ServiceCache, enrich
from .store import TrafficStore
from .views import EMPTY_LEAKS, LiveState, RankCache

CMD_PROCESSES = frozenset({"node", "python", "pythonw", "pwsh", "powershell", "cmd", "bash", "java", "ruby", "dotnet"})
PATH_ORDER = {"proxy": 0, "leak": 1, "direct": 2, "tun": 3, "sysproxy": 4, "local": 5}
POLL_SECONDS = 2.0
FLUSH_SECONDS = 10.0
SYSPROXY_NOTE = "进了系统代理端口，最终直连还是走节点要 Clash 连接接口才能看"


def leak_summary(flows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
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


def quota_alerts(today: Mapping[str, Any], *, total_mb: int, program_mb: int) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    used_total = int(today.get("upload") or 0) + int(today.get("download") or 0)
    if total_mb > 0:
        limit = total_mb * 1024 * 1024
        if used_total >= limit:
            alerts.append({"kind": "total", "process": "", "used": used_total, "limit": limit})
    if program_mb > 0:
        limit = program_mb * 1024 * 1024
        for row in today.get("rows") or []:
            used = int(row.get("upload") or 0) + int(row.get("download") or 0)
            if used >= limit:
                alerts.append({"kind": "program", "process": str(row.get("process") or ""), "used": used, "limit": limit})
            if len(alerts) >= 8:
                break
    return alerts


def build_alerts(
    today: Mapping[str, Any],
    leaks: Mapping[str, Any],
    new_apps: list[dict[str, Any]],
    *,
    skip: set[str],
    show_new_banner: bool,
    total_mb: int,
    program_mb: int,
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    leak_n = 0
    names: list[str] = []
    for row in leaks.get("rows") or []:
        name = str(row.get("process") or "")
        if name.lower() in skip:
            continue
        leak_n += int(row.get("n") or 0)
        if name and len(names) < 4:
            names.append(name)
    if leak_n:
        alerts.append({"kind": "leak", "count": leak_n, "names": names})
    if show_new_banner:
        for rec in new_apps[-6:]:
            name = str(rec.get("process") or "")
            if name.lower() in skip:
                continue
            alerts.append({"kind": "new", "process": name, "ts": int(rec.get("ts") or 0)})
    for item in quota_alerts(today, total_mb=total_mb, program_mb=program_mb):
        name = str(item.get("process") or "")
        if name and name.lower() in skip:
            continue
        alerts.append(item)
    return alerts[:12]


def export_csv(lanes: Mapping[str, Any]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    def text_cell(value: Any) -> str:
        value = str(value or "")
        return "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r", "\n")) else value
    w.writerow(["lane", "process", "site", "upload", "download"])
    for kind in ("proxy", "direct"):
        for p in (lanes.get(kind) or {}).get("rows") or []:
            w.writerow([kind, text_cell(p.get("process")), "", int(p.get("upload") or 0), int(p.get("download") or 0)])
            for s in p.get("sites") or []:
                w.writerow([kind, text_cell(p.get("process")), text_cell(s.get("site")), int(s.get("upload") or 0), int(s.get("download") or 0)])
    return buf.getvalue().encode("utf-8-sig")


def _int_setting(data: Mapping[str, Any], key: str) -> int:
    try:
        return int(data.get(key) or 0)
    except (TypeError, ValueError):
        return 0


class Collector:
    def __init__(
        self,
        *,
        store: TrafficStore,
        clash: ClashClient,
        ranks: RankCache,
        live: LiveState,
        settings: Callable[[], Mapping[str, Any]],
        ignored: Callable[[], set[str]],
        proxy_ports: set[int],
        listen_ports: FileMemo,
        routes: RouteCache | None = None,
        cmdlines: CmdlineCache | None = None,
        identities: IdentityReader | None = None,
        services: ServiceCache | None = None,
        icon_version: Callable[[str], str] | None = None,
        on_poll: Callable[[], None] | None = None,
    ) -> None:
        self.store = store
        self.clash = clash
        self.ranks = ranks
        self.live = live
        self._settings = settings
        self._ignored = ignored
        self.proxy_ports = set(proxy_ports)
        self.listen_ports = listen_ports
        self.routes = routes or RouteCache()
        self.cmdlines = cmdlines or CmdlineCache()
        self.identities = identities or IdentityReader()
        self.services = services or ServiceCache()
        self.icon_version = icon_version or (lambda path: "")
        self._on_poll = on_poll or (lambda: None)
        self.threads: list[threading.Thread] = []
        self._sample_lock = threading.RLock()

    # ---------- 一轮采集 ----------

    def collect(self, *, fresh: bool = False) -> dict[str, Any]:
        with self._sample_lock:
            return self._collect(fresh=fresh)

    def _collect(self, *, fresh: bool = False) -> dict[str, Any]:
        """本机连接表 + Clash 连接 → 分类、合并；顺带把 Clash 的字节数喂给 TrafficStore。"""
        t0 = time.perf_counter()
        sampled_at = time.time()
        store = self.store
        conns = tcp_connections()
        names = process_names()
        payload = self.clash.payload() if fresh else self.clash.recent_payload()
        if payload is None and not fresh:
            payload = self.clash.payload()
        if fresh and payload is None:
            raise RuntimeError("无法重新读取连接列表")
        clash_ok = payload is not None
        if payload is None:
            payload = {"connections": [], "uploadTotal": 0, "downloadTotal": 0}
        clash_conns = [c for c in payload.get("connections") or [] if isinstance(c, dict)]
        clash_cfg = self.clash.configs()
        win_proxy, win_ports = windows_proxy_info()
        mixed_ports = self.listen_ports.get() | set(clash_cfg.get("mixed_ports") or []) | win_ports | set(DEFAULT_MIXED_PORTS)

        # 路由表有缓存：TUN 开关变了、冒出没见过的本机 IP 时先让它失效，这次就用新的
        self.routes.note_tun(clash_cfg.get("tun_enable"))
        self.routes.note_local_ips(c["local_ip"] for c in conns)
        netinfo = self.routes.get()
        routes = as_list(netinfo.get("routes"))
        ip_to_iface = route_addrs(netinfo)
        defaults = as_list(netinfo.get("defaults"))
        v4def = next((d for d in defaults if d.get("DestinationPrefix") == "0.0.0.0/0"), None)
        default_v4 = f"{v4def.get('InterfaceAlias')} → {v4def.get('NextHop')}" if v4def else ""
        adapter_up = str(netinfo.get("tunStatus") or "").lower() in {"up"}
        tun_up = adapter_up or clash_cfg.get("tun_enable") is True
        default_is_tun = bool(v4def) and iface_kind(str(v4def.get("InterfaceAlias") or "")) == "tun"

        flows: list[dict[str, Any]] = []
        nodes: list[str] = []
        for c in conns:
            proc = names.get(c["pid"]) or f"pid:{c['pid']}"
            local_iface = ip_to_iface.get(c["local_ip"], "")
            path, route, note = classify(c, proc, routes, self.proxy_ports, local_iface, tun_up, default_is_tun, mixed_ports)
            if path in {"internal", "bypass"}:
                continue
            flows.append(
                {
                    "path": path,
                    "inbound": path if path in {"sysproxy", "tun", "leak", "local"} else "",
                    "outbound": path if path in {"proxy", "direct"} else ("unknown" if path == "sysproxy" else ""),
                    "node": "",
                    "process": proc,
                    "pid": c["pid"],
                    "local": f"{c['local_ip']}:{c['local_port']}",
                    "remote": f"{c['remote_ip']}:{c['remote_port']}",
                    "route": route,
                    "note": SYSPROXY_NOTE if path == "sysproxy" else note,
                    "upload": 0,
                    "download": 0,
                    "closed": False,
                }
            )

        flows = attach_original_process(flows)
        if clash_ok:
            keep = [f for f in flows if f["path"] in {"leak", "local", "dns"}]
            api_flows = [clash_to_flow(c) for c in clash_conns]
            local_of: dict[str, set[int]] = {}
            for c in conns:
                local_of.setdefault(f"{c['local_ip']}:{c['local_port']}", set()).add(c["pid"])
            pids: set[int] = set()
            for f in api_flows:
                if not f.get("pid"):
                    candidates = local_of.get(str(f.get("local") or ""), set())
                    f["pid"] = next(iter(candidates)) if len(candidates) == 1 else 0
                    if len(candidates) > 1:
                        f["identity_reason"] = "本机端点关联存在冲突"
                if f.get("pid") and str(f.get("process") or "").lower() in CMD_PROCESSES:
                    pids.add(int(f["pid"]))
            identity_data = self.identities.get({int(f.get("pid") or 0) for f in api_flows + keep})
            enrich(api_flows + keep, identity_data, sampled_at)
            self.services.annotate(api_flows + keep)
            generations = {pid: info.get("created", 0) for pid, info in identity_data.items() if info.get("status") == "verified"}
            safe_pids = {int(f["pid"]) for f in api_flows if f.get("identity_verified")}
            cmds = self.cmdlines.get(pids & safe_pids, generations=generations)
            for f in api_flows:
                cmd = cmds.get(int(f.get("pid") or 0), "")
                if cmd:
                    f["cmd"] = cmd
            if fresh or not self.clash.sampled_recently():
                store.update(api_flows)
                store.note_totals(as_int(payload.get("uploadTotal")), as_int(payload.get("downloadTotal")))
                if fresh:
                    self.clash.publish(payload)
            for f in api_flows:
                if not f.get("identity_conflict"):
                    store.remember_origin(str(f.get("process") or ""), str(f.get("cmd") or ""), str(f.get("proc_path") or ""))
            flows = api_flows + keep
            for f in api_flows:
                n = str(f.get("node") or "")
                if n and n not in nodes:
                    nodes.append(n)
        else:
            enrich(flows, self.identities.get({int(f.get("pid") or 0) for f in flows}), sampled_at)
            self.services.annotate(flows)

        counts = {
            "proxy": sum(1 for f in flows if f.get("outbound") == "proxy" or f["path"] == "proxy"),
            "direct": sum(1 for f in flows if f.get("outbound") == "direct" or f["path"] == "direct"),
            "tun": sum(1 for f in flows if f.get("inbound") == "tun" or f["path"] == "tun"),
            "leak": sum(1 for f in flows if f["path"] == "leak"),
            "sysproxy": sum(1 for f in flows if f.get("inbound") == "sysproxy" or f["path"] == "sysproxy"),
            "local": sum(1 for f in flows if f["path"] == "local"),
        }
        versions = {}
        for flow in flows:
            src = str(flow.get("proc_path") or "") if not flow.get("identity_conflict") else ""
            if src not in versions:
                versions[src] = self.icon_version(src or '@generic')
            flow["icon_version"] = versions[src]
        flows = self._collapse(flows)
        # 已结束、分流、排行不在这里算：按页面现取（views.py）
        store.record_history()
        leaks = leak_summary(flows)
        sysproxy_on = bool(win_proxy) or any(f.get("inbound") == "sysproxy" or f.get("path") == "sysproxy" for f in flows)
        if tun_up and sysproxy_on:
            mode = "both"
        elif tun_up:
            mode = "tun"
        elif sysproxy_on:
            mode = "sysproxy"
        else:
            mode = "off"
        rates = store.rates()
        return {
            "ts": int(time.time()),
            "ms": int((time.perf_counter() - t0) * 1000),
            "status": {
                "tun_up": tun_up,
                "sysproxy": sysproxy_on,
                "mode": mode,
                "tun_name": netinfo.get("tunName") or "FlClash",
                "tun_ip": netinfo.get("tunIp") or "",
                "default_v4": default_v4,
                "nodes": nodes[:4],
                "clash_api": clash_ok,
                "version": APP_VERSION,
                "proxy_ports": sorted(self.proxy_ports)[:12],
                "mixed_ports": sorted(mixed_ports)[:8],
                "upload_total": payload.get("uploadTotal") or 0,
                "download_total": payload.get("downloadTotal") or 0,
                "logged": store.logged,
                "up_speed": rates.get("up") or 0,
                "down_speed": rates.get("down") or 0,
                "leak_n": leaks["count"],
            },
            "counts": counts,
            "flows": flows,
            "leaks": leaks,
        }

    @staticmethod
    def _collapse(flows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """同一程序到同一远端、同一条路的多条连接合成一行（×n）。"""
        collapsed: dict[tuple, dict[str, Any]] = {}
        for f in flows:
            key = (
                f["path"],
                f.get("inbound", ""),
                f.get("outbound", ""),
                f.get("node", ""),
                f["process"],
                f.get("rule_exe", ""),
                f.get("proc_path", ""),
                f.get("identity_created", 0),
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
        out = list(collapsed.values())
        for f in out:
            if f["n"] > 1:
                f["note"] = f"{f['note']} ×{f['n']}"
        out.sort(
            key=lambda f: (
                -(int(f.get("download") or 0) + int(f.get("upload") or 0)),
                PATH_ORDER.get(f["path"], 9),
                f["process"],
            )
        )
        return out

    def with_alerts(self, state: dict[str, Any]) -> dict[str, Any]:
        """采集结果加上提醒（漏网 / 新程序 / 日流量）；托盘气泡和页面黄条都用它。"""
        data = self._settings()
        tun_up = bool((state.get("status") or {}).get("tun_up"))
        leaks = state.get("leaks") if tun_up else None
        state["alerts"] = build_alerts(
            self.ranks.get("today"),
            leaks or EMPTY_LEAKS,
            self.store.recent_new(),
            skip=self._ignored(),
            show_new_banner=bool(data.get("show_new_banner", True)),
            total_mb=_int_setting(data, "quota_total_mb"),
            program_mb=_int_setting(data, "quota_program_mb"),
        )
        return state

    def export_csv(self) -> bytes:
        return export_csv(self.store.lanes())

    # ---------- 后台线程 ----------

    def refresh(self):
        """Fresh read-only controller data; serialize with normal sampling and publication."""
        with self._sample_lock:
            self.clash.invalidate_config_cache()
            state = self._collect(fresh=True)
            inventory = self.clash.network_inventory()
            if not inventory.get('available') or not inventory.get('consistent'):
                raise RuntimeError('节点清单读取失败或正在变化')
            self.ranks.invalidate()
            state = self.with_alerts(state)
            self.live.set(state)
            return state, inventory

    def poller(self, stop: threading.Event) -> None:
        while not stop.is_set():
            with self._sample_lock:
                try:
                    self._on_poll()
                    self.live.set(self.with_alerts(self.collect()))
                except Exception as exc:  # noqa: BLE001
                    fmlog.log_exception("采集", exc)
                    self.live.set({"error": str(exc), "ts": int(time.time())})
            stop.wait(POLL_SECONDS)

    def clash_sampler(self, stop: threading.Event) -> None:
        while not stop.is_set():
            t0 = time.monotonic()
            try:
                with self._sample_lock:
                    payload = self.clash.payload()
                    if payload is None:
                        self.store.decay_speed()
                    else:
                        flows = [clash_to_flow(c) for c in payload.get("connections") or [] if isinstance(c, dict)]
                        self.store.update(flows)
                        self.store.note_totals(as_int(payload.get("uploadTotal")), as_int(payload.get("downloadTotal")))
                        self.clash.publish(payload)
            except Exception as exc:  # noqa: BLE001 - 0.8.0 在这里抛一次异常线程就永久停了
                fmlog.log_exception("clash_sampler", exc)
            stop.wait(max(0.15, 1.0 - (time.monotonic() - t0)))

    def traffic_reader(self, stop: threading.Event) -> None:
        while not stop.is_set():
            connected = False
            try:
                bases = self.clash.base_urls()
            except Exception as exc:  # noqa: BLE001
                fmlog.log_exception("traffic_reader", exc)
                bases = ()
            for base in bases:
                if stop.is_set():
                    return
                try:
                    req = Request(base + "/traffic", headers=self.clash.headers())
                    with self.clash.opener.open(req, timeout=2.0) as resp:
                        self.clash.remember_base(base)
                        connected = True
                        while not stop.is_set():
                            line = resp.readline()
                            if not line:
                                break
                            data = json.loads(line.decode("utf-8", "replace"))
                            if isinstance(data, dict):
                                self.store.set_wire_speed(as_int(data.get("up")), as_int(data.get("down")))
                except Exception:  # noqa: BLE001 - 连不上 / 断开：换下一个地址
                    continue
            if not connected:
                stop.wait(1.5)

    def store_flusher(self, stop: threading.Event) -> None:
        """每 10 秒把累计写盘；stop 之后再写最后一次。"""
        while not stop.wait(FLUSH_SECONDS):
            try:
                self.store.flush()
            except Exception as exc:  # noqa: BLE001
                fmlog.log_exception("store_flusher", exc)
        try:
            self.store.flush()
        except Exception as exc:  # noqa: BLE001
            fmlog.log_exception("store_flusher", exc)

    def start_threads(self, stop: threading.Event) -> None:
        if any(thread.is_alive() for thread in self.threads):
            return
        for target, name in (
            (self.clash_sampler, "clash-sample"),
            (self.traffic_reader, "clash-traffic"),
            (self.poller, "collect"),
            (self.store_flusher, "store-flush"),
        ):
            thread = threading.Thread(target=target, args=(stop,), daemon=True, name=name)
            self.threads.append(thread)
            thread.start()
