"""Clash / mihomo 外部控制器：连接列表、配置、断开连接，外加采样线程最近一次的结果。

连本机控制器一律不走代理：设了 HTTP_PROXY / HTTPS_PROXY 环境变量时，urllib 默认会把 127.0.0.1
也发给代理（只认 NO_PROXY），密钥就会跟着请求头一起发过去。
"""

from __future__ import annotations

import json
import hashlib
import errno
import threading
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import OpenerDirector, ProxyHandler, Request, build_opener

from .parsing import as_int

DEFAULT_BASE = "http://127.0.0.1:9090"
FALLBACK_BASES = ("http://127.0.0.1:9090", "http://127.0.0.1:9091", "http://127.0.0.1:9097")
CONFIG_TTL = 20.0
LISTEN_KEYS = ("mixed-port", "port", "socks-port", "redir-port", "tproxy-port")
LINK_ERRORS = {
    "auth_failed": "认证失败，请检查 Clash API 密钥是否与客户端一致。",
    "connection_refused": "连接被拒绝，请确认客户端已运行、外部控制器已开启，并检查端口。",
    "timeout": "连接超时，请检查控制器地址和网络状态后重试。",
    "invalid_response": "该地址未返回有效的 Clash 连接列表，请确认填写的是外部控制器地址，而非代理端口。",
    "http_error": "控制器返回 HTTP 错误，请检查客户端状态及外部控制器地址。",
    "network_error": "网络连接失败，请检查地址、网络权限及客户端状态。",
    "invalid_address": "外部控制器地址无效，请填写完整的 http:// 或 https:// 地址及有效端口。",
}


def _connection_payload(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict) or "connections" not in data:
        return None
    conns = data.get("connections")
    if conns is None:
        conns = []
    if not isinstance(conns, list):
        return None
    return {
        "connections": conns,
        "uploadTotal": as_int(data.get("uploadTotal")),
        "downloadTotal": as_int(data.get("downloadTotal")),
    }


def _diagnostic_base(base: str) -> str:
    """Show only the origin; URLs and exceptions may contain credentials."""
    try:
        parts = urlsplit(base)
        host, port = parts.hostname or "", parts.port
        if parts.scheme not in ("http", "https") or not host or (port is not None and port < 1):
            return ""
        if any(ch.isspace() or ord(ch) < 32 for ch in host):
            return ""
        if ":" in host:
            host = f"[{host}]"
        return f"{parts.scheme}://{host}" + (f":{port}" if port is not None else "")
    except ValueError:
        return ""


def _link_failure(exc: Exception) -> dict[str, Any]:
    """Classify failures without returning exception text or response content."""
    status = None
    if isinstance(exc, HTTPError):
        status = exc.code
        if status in (401, 403):
            code = "auth_failed"
        elif status in (404, 405):
            code = "invalid_response"
        else:
            code = "http_error"
    elif isinstance(exc, json.JSONDecodeError):
        code = "invalid_response"
    else:
        reason = exc.reason if isinstance(exc, URLError) else exc
        error_number = getattr(reason, "errno", None)
        winerror = getattr(reason, "winerror", None)
        if isinstance(reason, TimeoutError) or error_number in (errno.ETIMEDOUT, 10060) or winerror == 10060:
            code = "timeout"
        elif isinstance(reason, ConnectionRefusedError) or error_number in (errno.ECONNREFUSED, 10061) or winerror == 10061:
            code = "connection_refused"
        else:
            code = "network_error"
    result = {"error_code": code, "error": LINK_ERRORS[code]}
    if status is not None:
        result["http_status"] = status
    return result


def local_opener() -> OpenerDirector:
    return build_opener(ProxyHandler({}))


def valid_connection_id(cid: str) -> bool:
    if not cid or len(cid) > 80 or "/" in cid or "\\" in cid or ".." in cid:
        return False
    return all(ch.isalnum() or ch in "-_" for ch in cid)


class ClashClient:
    def __init__(
        self,
        base: Callable[[], str],
        secret: Callable[[], str],
        *,
        opener: OpenerDirector | None = None,
        clock: Callable[[], float] = time.monotonic,
        fallbacks: tuple[str, ...] = FALLBACK_BASES,
    ) -> None:
        self._base = base
        self._secret = secret
        self.opener = opener or local_opener()
        self._clock = clock
        self._fallbacks = tuple(fallbacks)
        self._lock = threading.Lock()
        self._base_ok = ""
        self._cfg: tuple[float, dict[str, Any]] | None = None
        self._sample: tuple[float, dict[str, Any]] | None = None

    # ---------- 地址 ----------

    @property
    def base_ok(self) -> str:
        with self._lock:
            return self._base_ok

    def remember_base(self, url: str) -> None:
        base = url[: -len("/connections")] if url.endswith("/connections") else url.rstrip("/")
        with self._lock:
            self._base_ok = base

    def base_urls(self) -> tuple[str, ...]:
        """设置里的地址在前，再试几个常见端口；上次连上的那个排第一。"""
        base = str(self._base() or DEFAULT_BASE).rstrip("/")
        urls = [base]
        for extra in self._fallbacks:
            if extra not in urls:
                urls.append(extra)
        ok = self.base_ok
        if ok and ok in urls:
            urls.remove(ok)
            urls.insert(0, ok)
        return tuple(urls)

    def headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        secret = str(self._secret() or "").strip()
        if secret:
            headers["Authorization"] = "Bearer " + secret
        return headers

    def _get_json(self, url: str, timeout: float) -> Any:
        req = Request(url, headers=self.headers())
        with self.opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    # ---------- 接口 ----------

    def payload(self) -> dict[str, Any] | None:
        """GET /connections；都连不上返回 None。"""
        for i, base in enumerate(self.base_urls()):
            url = base + "/connections"
            try:
                data = self._get_json(url, 2.0 if i == 0 else 0.5)
            except Exception:  # noqa: BLE001 - 连不上 / 超时 / 不是 JSON：换下一个地址
                continue
            payload = _connection_payload(data)
            if payload is None:
                continue
            self.remember_base(url)
            return payload
        return None

    def invalidate_config_cache(self) -> None:
        """Forget local read cache only; never reload or mutate the controller."""
        with self._lock:
            self._cfg = None

    def configs(self) -> dict[str, Any]:
        """GET /configs 里的监听端口和 TUN 开关，缓存 20 秒。"""
        now = self._clock()
        with self._lock:
            hit = self._cfg
        if hit is not None and now - hit[0] < CONFIG_TTL:
            return {"mixed_ports": set(hit[1]["mixed_ports"]), "tun_enable": hit[1]["tun_enable"]}
        mixed: set[int] = set()
        tun_enable: bool | None = None
        for base in self.base_urls():
            try:
                data = self._get_json(base + "/configs", 1.5)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(data, dict):
                continue
            for key in LISTEN_KEYS:
                try:
                    n = int(data.get(key) or 0)
                except (TypeError, ValueError):
                    continue
                if 1 <= n <= 65535:
                    mixed.add(n)
            tun = data.get("tun")
            if isinstance(tun, dict):
                tun_enable = bool(tun.get("enable"))
            elif isinstance(tun, bool):
                tun_enable = tun
            break
        with self._lock:
            self._cfg = (now, {"mixed_ports": sorted(mixed), "tun_enable": tun_enable})
        return {"mixed_ports": mixed, "tun_enable": tun_enable}

    def rule_inventory(self) -> dict[str, Any]:
        """Read-only metadata for local drafts; pin every read to the same controller."""
        base=self.base_urls()[0]
        result={"groups":[],"rules":[],"groups_error":"","rules_error":""}
        try:
            data=self._get_json(base+"/proxies",2.0)
            proxies=data["proxies"]
            if not isinstance(proxies,dict): raise ValueError()
            result["groups"]=[name for name,item in proxies.items()
                              if isinstance(item,dict) and isinstance(item.get("all"),list)
                              and item.get("type") in {"Selector","URLTest","Fallback","LoadBalance","Relay"}
                              and name not in {"GLOBAL","DIRECT","REJECT"}]
        except Exception:
            result["groups_error"]="无法读取策略组；指定策略组草稿暂不可保存"
        try:
            data=self._get_json(base+"/rules",2.0)
            if not isinstance(data.get("rules"),list): raise ValueError()
            result["rules"]=[{"exe":r.get("payload",""),"target":r.get("proxy","")}
                             for r in data["rules"] if isinstance(r,dict) and r.get("type") in {"ProcessName","PROCESS-NAME"}]
        except Exception:
            result["rules_error"]="现有规则读取失败，未检查冲突"
        return result

    def close_connection(self, cid: str) -> bool:
        cid = str(cid or "").strip()
        if not valid_connection_id(cid):
            return False
        for base in self.base_urls():
            url = f"{base}/connections/{quote(cid, safe='-')}"
            try:
                req = Request(url, method="DELETE", headers=self.headers())
                with self.opener.open(req, timeout=2.0) as resp:
                    resp.read()
                return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def network_inventory(self) -> dict[str, Any]:
        from .network_inventory import collect_inventory, pinned_reader
        base = self.base_urls()[0]
        try:
            read = pinned_reader(base, self.headers())
        except ValueError:
            return {'available': False, 'error': 'GPT 网络诊断仅支持本机控制器。'}
        return collect_inventory(read, controller_id=hashlib.sha256(base.encode()).hexdigest())

    def ipv6_inventory(self) -> dict[str, Any]:
        """Pinned read-only capability snapshot; never expose raw rules or controller secrets."""
        base=self.base_urls()[0]
        try:
            version=self._get_json(base+'/version',2.0)
            cfg=self._get_json(base+'/configs',2.0)
            rules=self._get_json(base+'/rules',2.0)['rules']
            if not isinstance(rules,list) or not isinstance(cfg,dict) or not isinstance(version,dict):raise ValueError()
            v=str(version.get('version',''))
            if not v or len(v)>80 or any(ord(c)<32 for c in v):raise ValueError()
            facts={'version':v,'meta':version.get('meta') is True,'mode':cfg.get('mode'),
                   'ipv6':cfg.get('ipv6') if type(cfg.get('ipv6')) is bool else None,
                   'find_process_mode':cfg.get('find-process-mode'),
                   'tun_enabled':bool((cfg.get('tun') or {}).get('enable')),'rule_count':len(rules)}
            stable_rules=[{k:r.get(k) for k in ('type','payload','proxy','size')} for r in rules if isinstance(r,dict)]
            facts['rules_revision']=hashlib.sha256(json.dumps(stable_rules,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            facts['controller_revision']=hashlib.sha256(base.encode()).hexdigest()
            return {'available':True,**facts}
        except Exception:
            return {'available':False,'error':'无法核实核心版本/规则状态；未修改核心'}

    def test_link(self) -> dict[str, Any]:
        """Test the configured address first and retain each fallback's diagnosis."""
        t0 = time.perf_counter()
        configured = str(self._base() or DEFAULT_BASE).rstrip("/")
        bases = [configured, *(base for base in self.base_urls() if base != configured)]
        attempts: list[dict[str, Any]] = []
        for i, base in enumerate(bases):
            display_base = _diagnostic_base(base)
            attempt: dict[str, Any] = {"base": display_base, "ok": False}
            if not display_base:
                attempt.update(error_code="invalid_address", error=LINK_ERRORS["invalid_address"])
                attempts.append(attempt)
                continue
            try:
                payload = _connection_payload(self._get_json(base + "/connections", 2.0 if i == 0 else 0.5))
            except Exception as exc:  # noqa: BLE001 - report safe categories, then try the next address
                attempt.update(_link_failure(exc))
            else:
                if payload is not None:
                    self.remember_base(base)
                    attempts.append({"base": display_base, "ok": True})
                    return {
                        "ok": True,
                        "ms": int((time.perf_counter() - t0) * 1000),
                        "connections": len(payload["connections"]),
                        "base": display_base,
                        "attempts": attempts,
                    }
                attempt.update(error_code="invalid_response", error=LINK_ERRORS["invalid_response"])
            attempts.append(attempt)
        # Keep the configured address's diagnosis primary; the UI can also show
        # all fallback failures, rather than masking a bad secret with a dead port.
        return {
            **attempts[0],
            "ms": int((time.perf_counter() - t0) * 1000),
            "attempts": attempts,
        }

    # ---------- 采样线程最近一次的结果 ----------

    def publish(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._sample = (self._clock(), payload)

    def recent_payload(self, max_age: float = 1.5) -> dict[str, Any] | None:
        """采样线程 max_age 秒内拿到的 /connections；采集时直接复用，不再多请求一次。"""
        with self._lock:
            hit = self._sample
        if hit is not None and self._clock() - hit[0] < max_age:
            return hit[1]
        return None

    def sampled_recently(self, window: float = 1.8) -> bool:
        return self.recent_payload(window) is not None
