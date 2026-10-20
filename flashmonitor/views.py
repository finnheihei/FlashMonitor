"""按页面组装 /api/snapshot 的数据。

采集线程每 2 秒把最新结果放进 LiveState（连接列表、计数、状态、提醒）。请求来了只取当前页面要用的部分：
分流、已结束、速率历史直接从 TrafficStore 现取，排行按时段缓存 3 秒（RankCache）。
0.8.0 每 2 秒把五个时段的排行、所有程序明细都算一遍，整包（本机约 1 MB）发给前端。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from .store import PERIODS

VIEWS = ("overview", "rank", "lanes", "conns", "closed", "settings")
RANK_TTL = 3.0
TOTAL_KEYS = ("upload", "download", "proxy_upload", "proxy_download", "direct_upload", "direct_download", "since")
EMPTY_LEAKS: dict[str, Any] = {"count": 0, "rows": []}


def normalize_view(view: Any) -> str:
    text = str(view or "").strip().lower()
    return text if text in VIEWS else "overview"


def normalize_period(period: Any) -> str:
    text = str(period or "").strip()
    return text if text in PERIODS else "today"


class RankCache:
    """每个时段的排行缓存 ttl 秒；同一时段同时来的请求只算一次。invalidate() 之后立即重算。"""

    def __init__(
        self,
        compute: Callable[[str], dict[str, Any]],
        *,
        ttl: float = RANK_TTL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._compute = compute
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._busy: dict[str, threading.Lock] = {}
        self._cache: dict[str, tuple[float, int, dict[str, Any]]] = {}
        self._gen = 0
        self.computes = 0

    def _fresh(self, period: str) -> dict[str, Any] | None:
        hit = self._cache.get(period)
        if hit and hit[1] == self._gen and self._clock() - hit[0] < self._ttl:
            return hit[2]
        return None

    def get(self, period: str) -> dict[str, Any]:
        """返回的 dict 是共享的，只读。"""
        period = normalize_period(period)
        with self._lock:
            hit = self._fresh(period)
            if hit is not None:
                return hit
            busy = self._busy.setdefault(period, threading.Lock())
        with busy:
            with self._lock:
                hit = self._fresh(period)
                if hit is not None:
                    return hit
                gen = self._gen
            value = self._compute(period)
            with self._lock:
                self.computes += 1
                if gen == self._gen:
                    self._cache[period] = (self._clock(), gen, value)
            return value

    def invalidate(self) -> None:
        with self._lock:
            self._gen += 1
            self._cache.clear()


class LiveState:
    """采集线程最近一次的结果。get() 拿到的 dict 是共享的，只读。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, Any] | None = None

    def set(self, data: dict[str, Any]) -> None:
        with self._lock:
            self._data = data

    def get(self) -> dict[str, Any] | None:
        with self._lock:
            return self._data

    def status(self) -> dict[str, Any]:
        status = (self.get() or {}).get("status")
        return status if isinstance(status, dict) else {}

    def alerts(self) -> list[dict[str, Any]]:
        alerts = (self.get() or {}).get("alerts")
        return list(alerts) if isinstance(alerts, list) else []


def rank_totals(pack: dict[str, Any]) -> dict[str, Any]:
    """只要合计和程序数，不要明细行（总览页的"今天"）。"""
    out = {k: pack.get(k, 0) for k in TOTAL_KEYS}
    out["programs"] = len(pack.get("rows") or [])
    out.update({k: pack[k] for k in ("coverage", "unknown_upload", "unknown_download", "rejected_upload", "rejected_download") if k in pack})
    return out


def lanes_with_live(lanes: dict[str, Any], flows: list[dict[str, Any]]) -> dict[str, Any]:
    """给分流页的每个程序标上现在有几条连接在走这条线。"""
    live_n: dict[tuple[str, str], int] = {}
    for f in flows:
        ob = str(f.get("outbound") or "")
        if ob not in {"proxy", "direct"}:
            continue
        key = (str(f.get("process") or ""), ob)
        live_n[key] = live_n.get(key, 0) + int(f.get("n") or 1)
    for kind in ("proxy", "direct"):
        for row in (lanes.get(kind) or {}).get("rows") or []:
            row["live"] = live_n.get((row["process"], kind), 0)
    return lanes


def _status(state: dict[str, Any], store: Any) -> dict[str, Any]:
    rates = store.rates()
    status = dict(state.get("status") or {})
    status["up_speed"] = rates.get("up") or 0
    status["down_speed"] = rates.get("down") or 0
    if hasattr(store, "storage_health"):
        status["storage"] = store.storage_health()
    return status


def snapshot_alerts(state: dict[str, Any], store: Any, prefs: dict[str, Any]) -> list[dict[str, Any]]:
    """New-app alerts are read at response time, not from the collector's stale copy."""
    cached=[a for a in state.get("alerts") or [] if a.get("kind")!="new"]
    skip={str(name).lower() for name in prefs.get("ignored") or []}
    news=[]
    if prefs.get("show_new_banner",True):
        news=[{"kind":"new","process":str(x.get("process") or ""),"ts":int(x.get("ts") or 0)}
              for x in store.recent_new()[-6:] if str(x.get("process") or "").lower() not in skip]
    return ([a for a in cached if a.get("kind")=="leak"] + news
            + [a for a in cached if a.get("kind")!="leak"])[:12]


def build_view(
    view: str,
    period: str,
    *,
    state: dict[str, Any] | None,
    store: Any,
    ranks: RankCache,
    prefs: dict[str, Any],
    sub: dict[str, Any] | None,
) -> dict[str, Any]:
    """一个页面要的数据。字段名和 0.8.0 相同，只是不再附带别的页面的数据。"""
    view = normalize_view(view)
    period = normalize_period(period)
    head: dict[str, Any] = {"view": view, "period": period}
    if not state:
        return {"error": "warming", **head}
    if state.get("error"):
        return {"error": str(state["error"]), "ts": state.get("ts") or 0, "flows": [], "counts": {}, "status": _status(state, store), **head}
    out: dict[str, Any] = {
        **head,
        "ts": state.get("ts") or 0,
        "ms": state.get("ms") or 0,
        "status": _status(state, store),
        "prefs": prefs,
        "alerts": snapshot_alerts(state,store,prefs),
    }
    if view == "settings":
        return out
    out["live"] = store.live_view()
    out["sub"] = sub
    flows = state.get("flows") or []
    if view == "overview":
        out["counts"] = state.get("counts") or {}
        out["flows"] = flows
        out["history"] = store.history_rows()
        out["rank"] = {"today": rank_totals(ranks.get("today"))}
        return out
    if view == "rank":
        out["rank"] = {period: ranks.get(period)}
    elif view == "lanes":
        out["lanes"] = lanes_with_live(store.lanes(), flows)
    elif view == "conns":
        out["flows"] = flows
    elif view == "closed":
        out["closed"] = store.recent_closed()
    return out


def build_all(
    *,
    state: dict[str, Any],
    store: Any,
    ranks: RankCache,
    prefs: dict[str, Any],
    sub: dict[str, Any] | None,
) -> dict[str, Any]:
    """所有页面的数据合在一起（给 --once 调试用）。"""
    out = {k: state[k] for k in ("ts", "ms", "counts", "flows", "leaks", "alerts") if k in state}
    out["alerts"] = snapshot_alerts(state,store,prefs)
    out["status"] = _status(state, store)
    out["closed"] = store.recent_closed()
    out["lanes"] = lanes_with_live(store.lanes(), state.get("flows") or [])
    out["history"] = store.history_rows()
    out["rank"] = {p: ranks.get(p) for p in PERIODS}
    out["sub"] = sub
    out["live"] = store.live_view()
    out["prefs"] = prefs
    return out
