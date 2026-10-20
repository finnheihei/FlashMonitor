"""机场账期（已用 / 总量 / 到期）：订阅地址返回的 Subscription-Userinfo 头，或 FlClash 缓存的那份。

订阅地址优先用设置里填的原始订阅，其次是 FlClash 当前配置的订阅地址。请求订阅时不走代理
（和 0.8.0 相同）。结果缓存 5 分钟，并存一份到数据目录的 subinfo.json，重启后先显示旧的。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.request import ProxyHandler, Request, build_opener

from . import log as fmlog
from .parsing import parse_subscription_userinfo

TTL = 300.0
MIN_CACHED_USED = 10 * 1024 * 1024  # FlClash 缓存里的数字太小多半是没刷新过，不显示


def public_info(info: Mapping[str, Any], *, label: str, source: str) -> dict[str, Any]:
    upload = max(0, int(info.get("upload") or 0))
    download = max(0, int(info.get("download") or 0))
    total = max(0, int(info.get("total") or 0))
    return {
        "label": label,
        "upload": upload,
        "download": download,
        "total": total,
        "used": upload + download,
        "expire": max(0, int(info.get("expire") or 0)),
        "source": source,
    }


def _userinfo_from(raw: Mapping[str, Any]) -> dict[str, int] | None:
    return parse_subscription_userinfo(
        f"upload={raw.get('upload') or 0}; download={raw.get('download') or 0}; "
        f"total={raw.get('total') or 0}; expire={raw.get('expire') or 0}"
    )


def header_userinfo(headers: Any) -> dict[str, int] | None:
    if headers is None:
        return None
    raw = headers.get("Subscription-Userinfo") or headers.get("subscription-userinfo")
    return parse_subscription_userinfo(str(raw or ""))


def fetch_userinfo(url: str) -> dict[str, int] | None:
    if not url.startswith("http://") and not url.startswith("https://"):
        return None
    headers = {"User-Agent": "clash.meta", "Accept": "*/*"}
    opener = build_opener(ProxyHandler({}))
    for method in ("HEAD", "GET"):
        try:
            req = Request(url, method=method, headers=headers)
            with opener.open(req, timeout=12.0) as resp:
                info = header_userinfo(resp.headers)
                if method == "GET":
                    try:
                        resp.read(256)
                    except Exception:  # noqa: BLE001
                        pass
            if info and (info["upload"] or info["download"] or info["total"]):
                return info
        except Exception:  # noqa: BLE001 - 订阅地址连不上：试下一种方法 / 用缓存
            continue
    return None


def flclash_current_profile(flclash_dir: Path) -> dict[str, Any] | None:
    """FlClash 当前配置的名称、订阅地址和它缓存的账期（只读打开它的数据库）。"""
    prefs = flclash_dir / "shared_preferences.json"
    db = flclash_dir / "database.sqlite"
    if not prefs.is_file() or not db.is_file():
        return None
    try:
        blob = json.loads(prefs.read_text(encoding="utf-8"))
        cfg = json.loads(blob.get("flutter.config") or "{}")
        pid = int(cfg.get("currentProfileId") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    if not pid:
        return None
    try:
        conn = sqlite3.connect(f"file:{db.resolve().as_posix()}?mode=ro", uri=True, timeout=1.0)
        try:
            row = conn.execute(
                "SELECT label, url, subscription_info FROM profiles WHERE id = ?",
                (pid,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if not row:
        return None
    label, url, raw_info = row
    cached = None
    if raw_info:
        try:
            parsed = json.loads(raw_info) if isinstance(raw_info, str) else raw_info
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            cached = _userinfo_from(parsed)
    return {"label": str(label or ""), "url": str(url or ""), "cached": cached}


class SubInfo:
    def __init__(
        self,
        cache_file: Path,
        *,
        sub_url: Callable[[], str],
        flclash_dir: Path,
        fetch: Callable[[str], dict[str, int] | None] = fetch_userinfo,
        spawn: Callable[[Callable[[], None]], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cache_file = Path(cache_file)
        self._sub_url = sub_url
        self.flclash_dir = Path(flclash_dir)
        self._fetch = fetch
        self._spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True, name="subinfo").start())
        self._clock = clock
        self._lock = threading.Lock()
        self._ts = 0.0
        self._data: dict[str, Any] | None = None
        self._fetching = False

    def public(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._data) if isinstance(self._data, dict) else None

    def _load_disk(self) -> dict[str, Any] | None:
        if not self.cache_file.is_file():
            return None
        try:
            raw = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(raw, dict):
            return None
        info = _userinfo_from(raw)
        if not info:
            return None
        return public_info(info, label=str(raw.get("label") or ""), source=str(raw.get("source") or "cache"))

    def _save_disk(self, data: Mapping[str, Any]) -> None:
        payload = {
            "label": data.get("label") or "",
            "upload": int(data.get("upload") or 0),
            "download": int(data.get("download") or 0),
            "total": int(data.get("total") or 0),
            "expire": int(data.get("expire") or 0),
            "source": data.get("source") or "",
            "ts": int(self._clock()),
        }
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.cache_file)
        except OSError:
            pass

    def refresh(self) -> dict[str, Any] | None:
        profile = flclash_current_profile(self.flclash_dir)
        label = str((profile or {}).get("label") or "")
        cached = (profile or {}).get("cached") if profile else None
        url = str(self._sub_url() or "").strip() or str((profile or {}).get("url") or "")
        live = self._fetch(url) if url else None
        info = live or cached
        if not info:
            return None
        source = "live" if live else "cache"
        data: dict[str, Any] | None = public_info(info, label=label, source=source)
        if source != "live" and int(data.get("used") or 0) < MIN_CACHED_USED:
            data = None
        with self._lock:
            self._ts = self._clock()
            self._data = data
        if data:
            self._save_disk(data)
        return data

    def _refresh_guarded(self) -> None:
        # 0.8.0 里刷新抛异常时"正在刷新"一直不复位，之后就再也不刷新机场账期
        try:
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            fmlog.log_exception("刷新机场账期", exc)
        finally:
            with self._lock:
                self._fetching = False

    def schedule(self, *, force: bool = False) -> None:
        """需要时在后台刷新（5 分钟一次；force 立即刷新）。第一次先读磁盘缓存。"""
        now = self._clock()
        with self._lock:
            if self._data is None:
                disk = self._load_disk()
                if disk:
                    self._data = disk
                    self._ts = now
            fresh = self._data is not None and now - self._ts < TTL
            if (fresh and not force) or self._fetching:
                return
            self._fetching = True
        self._spawn(self._refresh_guarded)
