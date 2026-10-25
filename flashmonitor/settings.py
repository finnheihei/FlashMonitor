"""设置：settings.json 的读写和校验，以及给页面看的版本。

给页面的版本不含 Clash 密钥和订阅地址原文，只告诉页面"有没有设置"和订阅的主机名。
开机启动、保存后要做的事（比如刷新机场账期）由构造参数传进来。
"""

from __future__ import annotations

import ipaddress
import json
import re
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from . import log as fmlog
from .connection_draft import normalize_draft
from .rule_drafts import DraftError

DEFAULT_IGNORE = [
    "FlashMonitor",
    "msedgewebview2",
    "wetype_server",
    "MpDefenderCoreService",
    "SearchHost",
    "TextInputHost",
    "StartMenuExperienceHost",
    "ShellExperienceHost",
    "RuntimeBroker",
    "conhost",
    "ctfmon",
    "sihost",
    "taskhostw",
    "dwm",
]

DEFAULT_SETTINGS: dict[str, Any] = {
    "autostart": False,
    "open_browser": False,
    "start_minimized": False,
    "close_to_tray": True,
    "clash_api": "http://127.0.0.1:9090",
    "clash_secret": "",
    "retention_days": 40,
    "quota_total_mb": 0,
    "quota_program_mb": 0,
    "sub_url": "",
    "show_airport": True,
    "notify_new_app": False,
    "notify_leak": True,
    "notify_quota": True,
    "notify_hide": False,
    "show_new_banner": True,
    "last_filter": "overview",
    "last_period": "today",
    "home_scope": "all",
    "connection_draft": None,
    "ignored": [],
    "pinned": [],
}
SECRET_KEYS = ("clash_secret", "sub_url")
FILTERS = {"overview", "rank", "lanes", "conns", "closed", "settings"}
PERIODS = {"1h", "today", "7d", "30d", "all"}
NOTIFY_KEYS = ("notify_new_app", "notify_leak", "notify_quota", "notify_hide", "show_new_banner")
MAX_NAMES = 80
MAX_QUOTA_MB = 10_000_000
URL_ERRORS = {
    "clash_api": "外部控制器地址无效，请填写完整的 http:// 或 https:// 地址，并检查主机名和端口。",
    "sub_url": "订阅地址无效，请填写完整的 http:// 或 https:// 地址，并检查主机名和端口（少于 2000 字符）；清除订阅请使用清除按钮。",
}


def as_name_list(value: Any) -> list[str]:
    """去重（不分大小写）、去空，最多 80 个。"""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        name = str(item or "").strip()
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        out.append(name)
        if len(out) >= MAX_NAMES:
            break
    return out


def url_hint(url: str) -> str:
    """只留协议和主机名（订阅地址的令牌通常在路径或参数里）。"""
    try:
        parts = urlsplit(str(url or "").strip())
        host = parts.hostname or ""
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not host:
        return ""
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}"


def _validated_url(value: Any, *, subscription: bool) -> str:
    """Validate before changing settings; never include a possibly secret URL in errors."""
    if not isinstance(value, str):
        raise ValueError("URL must be text")
    url = value.strip()
    if subscription and not url:
        return ""
    if not url or (subscription and len(url) >= 2000):
        raise ValueError("Invalid URL length")
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url) or "\\" in url:
        raise ValueError("Invalid URL characters")
    parts = urlsplit(url)
    host = parts.hostname
    port = parts.port  # Raises for a malformed or out-of-range port.
    if parts.scheme not in ("http", "https") or not host:
        raise ValueError("HTTP(S) URL with host required")
    if parts.username is not None or parts.password is not None or "#" in url:
        raise ValueError("Unsupported credentials or fragment")
    if parts.netloc.endswith(":") or port == 0 or (not subscription and "?" in url):
        raise ValueError("Invalid controller address or port")
    if ":" in host:
        ipaddress.IPv6Address(host)
    elif re.fullmatch(r"[0-9.]+", host) and "." in host:
        ipaddress.IPv4Address(host)
    else:
        ascii_host = host.encode("idna").decode("ascii").rstrip(".")
        if len(ascii_host) > 253 or not all(
            re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
            for label in ascii_host.split(".")
        ):
            raise ValueError("Invalid hostname")
    return url if subscription else url.rstrip("/")


def _clamp_int(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(int(value), hi))
    except (TypeError, ValueError):
        return default


def _toggle(names: list[str], name: str, on: bool) -> list[str]:
    cur = as_name_list(names)
    if on:
        if name not in cur:
            cur.append(name)
        return cur
    return [x for x in cur if x.lower() != name.lower()]


def _without(names: Any, name: str) -> list[str]:
    return [x for x in as_name_list(names) if x.lower() != name.lower()]


class AppSettings:
    def __init__(
        self,
        path: Path,
        *,
        autostart_enabled: Callable[[], bool] | None = None,
        set_autostart: Callable[[bool], None] | None = None,
        on_saved: Callable[[set[str]], None] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self.draft_error = ""
        self.path = Path(path)
        self._autostart_enabled = autostart_enabled or (lambda: False)
        self._set_autostart = set_autostart or (lambda on: None)
        self._on_saved = on_saved or (lambda keys: None)
        self.data: dict[str, Any] = dict(DEFAULT_SETTINGS)
        self.error = ""
        self._load()

    def _load(self) -> None:
        raw: dict[str, Any] | None = None
        if self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    raw = loaded
                    self.data.update({k: loaded[k] for k in DEFAULT_SETTINGS if k in loaded})
                    self.data["ignored"] = as_name_list(loaded.get("ignored", self.data.get("ignored")))
                    self.data["pinned"] = as_name_list(loaded.get("pinned", self.data.get("pinned")))
            except (OSError, ValueError, TypeError):
                raw = None
        if raw is None or "ignored" not in raw:
            self.data["ignored"] = list(DEFAULT_IGNORE)
        if self.data.get("home_scope") not in ("all","proxy"): self.data["home_scope"]="all"
        if self.data.get("connection_draft") is not None:
            try:
                self.data["connection_draft"] = normalize_draft(self.data["connection_draft"])
            except DraftError:
                self.data["connection_draft"] = None
                self.draft_error = "已保存的诊断草稿格式无效，未自动改写文件"
        self.data["autostart"] = bool(self._autostart_enabled())

    def save(self) -> bool:
        with self._lock:
            return self._write({k: self.data.get(k, v) for k, v in DEFAULT_SETTINGS.items()})

    def _write(self, payload) -> bool:
        tmp = self.path.with_suffix(".json.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Autostart lives in the OS, not this file. Keeping a second copy
            # would require another disk write after changing the startup entry.
            stored = {key: value for key, value in payload.items() if key != "autostart"}
            tmp.write_text(json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
            return True
        except OSError as exc:
            fmlog.log_exception("保存设置", exc)
            self.error = f"设置没存上：{exc}"
            return False

    def apply(self, patch: dict[str, Any]) -> bool:
        with self._lock:
            if "connection_draft" in patch:
                if set(patch) != {"connection_draft"}:
                    self.error = "诊断草稿请单独确认，未保存任何修改"
                    return False
                try:
                    draft = normalize_draft(patch["connection_draft"])
                except DraftError as exc:
                    self.error = "草稿未保存：" + str(exc)
                    return False
                payload = {k: self.data.get(k, v) for k, v in DEFAULT_SETTINGS.items()}
                payload["connection_draft"] = draft
                self.error = ""
                if not self._write(payload):
                    return False
                self.data["connection_draft"] = draft
                self.draft_error = ""
                return True
            return self._apply(patch)

    def _apply(self, patch: dict[str, Any]) -> bool:
        # A bad URL rejects the entire patch, including unrelated settings and hooks.
        urls: dict[str, str] = {}
        for key in URL_ERRORS:
            if key in patch:
                try:
                    urls[key] = _validated_url(patch[key], subscription=key == "sub_url")
                except (ValueError, UnicodeError):
                    self.error = URL_ERRORS[key]
                    return False
        # Do not expose uncommitted settings to the collector or later saves.
        d = deepcopy(self.data)
        for key in ("open_browser", "start_minimized", "close_to_tray", "show_airport", *NOTIFY_KEYS):
            if key in patch:
                d[key] = bool(patch[key])
        if "clash_api" in patch:
            d["clash_api"] = urls["clash_api"]
        if "retention_days" in patch:
            d["retention_days"] = _clamp_int(patch["retention_days"], 7, 365, 40)
        if "clash_secret" in patch:
            d["clash_secret"] = str(patch.get("clash_secret") or "").strip()
        for key in ("quota_total_mb", "quota_program_mb"):
            if key in patch:
                d[key] = _clamp_int(patch[key], 0, MAX_QUOTA_MB, 0)
        if "sub_url" in patch:
            d["sub_url"] = urls["sub_url"]
        if "last_filter" in patch:
            val = str(patch.get("last_filter") or "overview")
            if val in FILTERS:
                d["last_filter"] = val
        if patch.get("home_scope") in ("all","proxy"):
            d["home_scope"]=patch["home_scope"]
        if "last_period" in patch:
            val = str(patch.get("last_period") or "today")
            if val in PERIODS:
                d["last_period"] = val
        if "process" not in patch:
            if isinstance(patch.get("ignored"), list):
                d["ignored"] = as_name_list(patch["ignored"])
            if isinstance(patch.get("pinned"), list):
                d["pinned"] = as_name_list(patch["pinned"])
        proc = str(patch.get("process") or "").strip()
        if proc:
            # 钉住和忽略互斥：钉住一个程序就把它从忽略名单拿掉，反过来也一样
            if isinstance(patch.get("ignored"), bool):
                on = patch["ignored"]
                d["ignored"] = _toggle(d.get("ignored"), proc, on)
                if on:
                    d["pinned"] = _without(d.get("pinned"), proc)
            if isinstance(patch.get("pinned"), bool):
                on = patch["pinned"]
                d["pinned"] = _toggle(d.get("pinned"), proc, on)
                if on:
                    d["ignored"] = _without(d.get("ignored"), proc)
        self.error = ""
        if not self._write({key: d.get(key, value) for key, value in DEFAULT_SETTINGS.items()}):
            return False
        # Keep existing references valid, and let hooks read only durable values.
        self.data.update(d)
        try:
            if "autostart" in patch:
                self._set_autostart(bool(patch["autostart"]))
            elif self._autostart_enabled() and ("start_minimized" in patch or "open_browser" in patch):
                self._set_autostart(True)
        except RuntimeError as exc:
            if set(patch) - {"autostart"}:
                self.error = f"其他设置已保存，开机启动项未更新：{exc}"
            else:
                self.error = str(exc)
        self.data["autostart"] = bool(self._autostart_enabled())
        try:
            self._on_saved(set(patch))
        except Exception as exc:  # noqa: BLE001 - 保存后的附带动作失败不影响设置本身
            fmlog.log_exception("设置保存后的处理", exc)
        return not self.error

    def public(self) -> dict[str, Any]:
        with self._lock:
            return self._public()

    def _public(self) -> dict[str, Any]:
        """给页面的设置：密钥和订阅地址只说有没有，不给原文。"""
        out = {k: self.data.get(k, v) for k, v in DEFAULT_SETTINGS.items() if k not in SECRET_KEYS}
        out["autostart"] = bool(self._autostart_enabled())
        out["clash_secret_set"] = bool(self.data.get("clash_secret"))
        out["sub_url_set"] = bool(self.data.get("sub_url"))
        out["sub_url_hint"] = url_hint(str(self.data.get("sub_url") or ""))
        out["connection_draft"] = deepcopy(out["connection_draft"])
        out["connection_draft_error"] = self.draft_error
        out["error"] = self.error or ""
        return out

    # ---------- 给别处用的小工具 ----------

    def ignored_set(self) -> set[str]:
        return {str(n).lower() for n in (self.data.get("ignored") or []) if n}

    def pinned(self) -> list[str]:
        return as_name_list(self.data.get("pinned"))

    def ui_prefs(self) -> dict[str, Any]:
        d = self.data
        return {
            "ignored": as_name_list(d.get("ignored")),
            "pinned": self.pinned(),
            "show_airport": bool(d.get("show_airport", True)),
            "notify_new_app": bool(d.get("notify_new_app", False)),
            "notify_leak": bool(d.get("notify_leak", True)),
            "notify_quota": bool(d.get("notify_quota", True)),
            "show_new_banner": bool(d.get("show_new_banner", True)),
            "home_scope": d.get("home_scope", "all"),
        }


def contains_secret(obj: Any, secrets: Iterable[str]) -> bool:
    """obj 序列化后是否含有任何一个 secret 原文（测试和自检用）。"""
    text = json.dumps(obj, ensure_ascii=False)
    return any(s and s in text for s in secrets)
