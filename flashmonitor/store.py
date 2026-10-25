"""流量累计：每个程序 / 节点 / 网站的累计值和 15 分钟时段合计，外加实时速率。

数据在数据目录的 flashmonitor.db（SQLite）。内存里只有每个程序的累计（数量不多）和还没写盘的时段增量；
flush() 在一个事务里把增量累加进库（写盘线程每 10 秒一次，退出时再来一次）。
时段排行 = 库里的合计 + 还没写盘的增量；超过保留天数的时段查询时就不算，库里的旧行每小时删一次。
库是新建的时候，把 0.8.0 的 programs.json / slots.json / seen.json 导入进来（缺哪部分就从
connections.jsonl 重建哪部分），导入的数据和迁移标记在同一个事务里写库，旧文件保留不动。
结束的连接逐条追加到 connections.jsonl（50 MB 轮转成 .jsonl.old）。
依赖（时钟、设置、图标、文件描述）都由构造参数传入，便于测试。
"""

from __future__ import annotations

import functools
import json
import math
import sqlite3
import threading
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import log as fmlog
from .classify import CORE_NAMES, DIRECT_OUT, site_key, host_of
from .db import DB_NAME, KINDS, EXTRA_KINDS, EXTRA_INTS, PROGRAM_TEXTS, SCHEMA_VERSION, TrafficDB
from .parsing import as_int, shorten_cmd
from .icons import local_executable
from .site_details import SiteDetails

SLOT_SECONDS = 15 * 60
PRUNE_EVERY = 3600.0
MAX_SITES = 40
RANK_SITES = 12
RANK_SITE_ROWS = 80
MAX_CLOSED = 400
KEEP_CLOSED = 250
HISTORY_LEN = 240
MAX_NEW_APPS = 12
LOG_ROTATE_BYTES = 50 * 1024 * 1024
UNKNOWN = "未知"
PERIODS = ("1h", "today", "7d", "30d", "all")
SPLIT_KEYS = ("proxy_up", "proxy_down", "direct_up", "direct_down")
_TS_MAX = 10_000_000_000  # 2286 年；更大的时间戳当成坏数据

OUTBOUNDS = ("proxy", "direct", "unknown", "rejected")
ALL_SPLIT_KEYS = SPLIT_KEYS + EXTRA_INTS

def outbound_kind(value: str) -> str:
    return value if value in OUTBOUNDS else "unknown"

SlotKey = tuple[str, int, str]  # (kind, slot, name)


def event_ts(rec: dict[str, Any], default: float | None = None) -> float:
    """连接记录的时间：优先结束时间，其次开始时间；都没有或是坏值就用 default（缺省为现在）。"""
    end = rec.get("end")
    if isinstance(end, (int, float)) and 1_000_000_000 < end < _TS_MAX:
        return float(end)
    start = rec.get("start") or ""
    if isinstance(start, str) and start:
        try:
            ts = datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp()
            if 0 < ts < _TS_MAX:
                return ts
        except (ValueError, OverflowError, OSError):
            pass
    return time.time() if default is None else default


def slot_key(ts: float) -> str:
    """0.8.0 slots.json 用的时段键（本地时间，15 分钟对齐）。"""
    t = time.localtime(ts)
    minute = t.tm_min - (t.tm_min % 15)
    return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}T{t.tm_hour:02d}:{minute:02d}"


@functools.lru_cache(maxsize=65536)
def slot_ts(key: str) -> float:
    """时段键 → 该时段开始的 unix 时间。strptime 很慢，结果缓存。"""
    return time.mktime(time.strptime(key, "%Y-%m-%dT%H:%M"))


_SLOT_MEMO: tuple[float, float, int] = (0.0, 0.0, 0)


def slot_start(ts: float) -> int:
    """ts 所在 15 分钟时段开始的 unix 秒（和 slot_ts(slot_key(ts)) 相同，但快得多）。"""
    global _SLOT_MEMO
    lo, hi, slot = _SLOT_MEMO
    if lo <= ts < hi:
        return slot
    t = time.localtime(ts)
    slot = int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min - t.tm_min % 15, 0, 0, 0, -1)))
    _SLOT_MEMO = (float(slot), float(slot + SLOT_SECONDS), slot)
    return slot


def period_since(period: str, now: float) -> tuple[float, str]:
    """排行时段的起点；不认识的时段按近 1 小时。"""
    if period == "today":
        lt = time.localtime(now)
        return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)), period
    if period == "7d":
        return now - 7 * 86400, period
    if period == "30d":
        return now - 30 * 86400, period
    if period == "all":
        return 0.0, period
    return now - 3600, "1h"


def count_lines(path: Path) -> int:
    if not path.is_file():
        return 0
    n = 0
    tail = b"\n"
    try:
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                n += chunk.count(b"\n")
                tail = chunk[-1:]
    except OSError:
        return 0
    return n if tail == b"\n" else n + 1


def iter_log(path: Path):
    """逐条读 connections.jsonl；坏行跳过。"""
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                yield rec


def _blank_site() -> dict[str, Any]:
    return {
        "upload": 0,
        "download": 0,
        "proxy_up": 0,
        "proxy_down": 0,
        "direct_up": 0,
        "direct_down": 0,
        **{k: 0 for k in EXTRA_INTS},
        "conns": 0,
        "last": 0,
    }


def _blank_program() -> dict[str, Any]:
    row = _blank_site()
    row["sites"] = {}
    for key in PROGRAM_TEXTS:
        row[key] = ""
    return row


def _copy_program(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["sites"] = {s: dict(v) for s, v in (row.get("sites") or {}).items()}
    return item


def _sort_by_traffic(rows: list[dict[str, Any]]) -> None:
    rows.sort(key=lambda r: -(int(r.get("download") or 0) + int(r.get("upload") or 0)))


def _traffic_leaders(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Bound a summary without losing proxy/direct leaders to the total-traffic cut.

    Keep the union of each metric's leaders, in total-traffic order (at most 3*limit).
    This remains a summary, not a complete searchable history.
    """
    ordered = sorted(rows, key=lambda r: -(int(r.get("download") or 0) + int(r.get("upload") or 0)))
    chosen = {id(row) for row in ordered[:limit]}
    for kind in ("proxy", "direct"):
        def amount(row: dict[str, Any]) -> int:
            return int(row.get(kind + "_up") or 0) + int(row.get(kind + "_down") or 0)
        leaders = sorted((row for row in rows if amount(row) > 0), key=amount, reverse=True)[:limit]
        chosen.update(id(row) for row in leaders)
    return [row for row in ordered if id(row) in chosen]


class TrafficStore:
    """按程序累加上下行和时段流量；重启后从数据目录读回。"""

    def __init__(
        self,
        data_dir: Path,
        *,
        retention_days: Callable[[], int] | None = None,
        ignored: Callable[[], set[str]] | None = None,
        on_icon: Callable[[str, str], None] | None = None,
        describe: Callable[[str], str] | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        program_sites_enabled: bool = False,
        site_details: SiteDetails | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "connections.jsonl"
        self.programs_path = self.data_dir / "programs.json"
        self.slots_path = self.data_dir / "slots.json"
        self.seen_path = self.data_dir / "seen.json"
        self._retention_days = retention_days or (lambda: 40)
        self._ignored = ignored or (lambda: set())
        self._on_icon = on_icon or (lambda name, path: None)
        self._describe = describe or (lambda path: "")
        self._now = clock
        self._mono = monotonic
        # 锁的顺序：_flush_lock → db.lock → lock。update() 只拿 lock，不等磁盘。
        self.lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._desc_cache: dict[str, str] = {}
        self.live: dict[str, dict[str, Any]] = {}
        self.last_bytes: dict[str, tuple[int, int]] = {}
        self.closed: list[dict[str, Any]] = []
        self.programs: dict[str, dict[str, Any]] = {}
        self.seen: set[str] = set()
        self.new_apps: list[dict[str, Any]] = []
        self.history: list[dict[str, float]] = []
        self.speed = {k: 0.0 for k in ("up", "down", *ALL_SPLIT_KEYS)}
        self.proc_speed: dict[str, dict[str, float]] = {}
        self.total_speed = {"up": 0.0, "down": 0.0}
        self.wire_speed = {"up": 0.0, "down": 0.0, "at": 0.0}
        self._tick_at = self._mono()
        self._primed = False
        self._tot_up: int | None = None
        self._tot_down: int | None = None
        self._tot_at = 0.0
        self._tot_ready = False
        # 还没写库的改动
        self._pending: dict[SlotKey, list[int]] = {}
        self._inflight: dict[SlotKey, list[int]] = {}
        self._dirty_programs: set[str] = set()
        self._seen_new: set[str] = set()
        self._meta_pending: dict[str, str] = {}
        self._pending_reset = False
        self._inflight_reset = False
        self._reset_gen = 0
        self._closed = False
        self._last_flush_error = ''
        self._booting = True
        self.site_details = site_details or (SiteDetails(self.data_dir / 'program-sites.db', clock=clock) if program_sites_enabled else None)
        self._program_sites_enabled = self.site_details is not None
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.logged = count_lines(self.path)
        self.db = TrafficDB(self.data_dir / DB_NAME)
        if self.db.migrated():
            self.programs = self.db.load_programs()
            self.seen = self.db.load_seen() | set(self.programs)
        else:
            self._import_legacy()
        self._booting = False
        self._pair_since = self.site_details.since if self.site_details else 0
        self._next_prune = self._now()  # 启动时的这次 flush 顺带清掉过期时段
        self.flush()

    # ---------- 旧数据导入（只在库是新建的时候） ----------

    def _import_legacy(self) -> None:
        sources: list[str] = []
        if self._load_programs_file():
            sources.append("programs.json")
            if any(not rec.get("sites") for rec in self.programs.values()):
                self._hydrate_sites_from_log()
        elif self.path.is_file():
            self._programs_from_log()
            sources.append("connections.jsonl")
        found = self._load_slots_file()
        if found:
            sources.append("slots.json")
        missing = {k for k in KINDS if k not in found and k != 'procsite'}
        if missing and self.path.is_file():
            self._slots_from_log(missing)
            if "connections.jsonl" not in sources:
                sources.append("connections.jsonl")
        if self._load_seen_file():
            sources.append("seen.json")
        self.seen.update(self.programs)
        self._dirty_programs = set(self.programs)
        self._seen_new = set(self.seen)
        self._meta_pending = {
            "schema": SCHEMA_VERSION,
            "migrated_at": str(int(self._now())),
            "migrated_from": ",".join(sources) or "-",
        }

    def _load_programs_file(self) -> bool:
        if not self.programs_path.is_file():
            return False
        try:
            data = json.loads(self.programs_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        raw = data.get("programs") if isinstance(data, dict) else data
        if not isinstance(raw, dict):
            return False
        for name, rec in raw.items():
            if not isinstance(rec, dict):
                continue
            row = _blank_program()
            for k in row:
                if k == "sites":
                    continue
                if k in PROGRAM_TEXTS:
                    row[k] = str(rec.get(k) or "")
                else:
                    row[k] = as_int(rec.get(k))
            row["sites"] = self._read_site_map(rec.get("sites"))
            self.programs[str(name)] = row
        return True

    @staticmethod
    def _read_site_map(raw: Any) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        if not isinstance(raw, dict):
            return out
        for name, srec in raw.items():
            if not isinstance(srec, dict):
                continue
            row = _blank_site()
            for k in row:
                row[k] = as_int(srec.get(k))
            out[str(name)] = row
        return out

    def _programs_from_log(self) -> None:
        """没有 programs.json：按连接记录重建每个程序的累计（时段数据另算）。"""
        try:
            for rec in iter_log(self.path):
                self._add(
                    str(rec.get("process") or UNKNOWN),
                    as_int(rec.get("upload")),
                    as_int(rec.get("download")),
                    str(rec.get("outbound") or ""),
                    closed=True,
                    host=str(rec.get("remote") or ""),
                    node=str(rec.get("node") or ""),
                    ts=event_ts(rec, self._now()),
                    with_slots=False,
                )
        except OSError:
            pass

    def _hydrate_sites_from_log(self) -> None:
        """旧版 programs.json 没有网站明细：从连接记录补上。"""
        need = {n for n, rec in self.programs.items() if not rec.get("sites")}
        try:
            for rec in iter_log(self.path):
                proc = str(rec.get("process") or "")
                if proc not in need:
                    continue
                self._add_site(
                    proc,
                    as_int(rec.get("upload")),
                    as_int(rec.get("download")),
                    str(rec.get("outbound") or ""),
                    str(rec.get("remote") or ""),
                    closed=True,
                    ts=event_ts(rec, self._now()),
                )
        except OSError:
            pass

    def _load_slots_file(self) -> set[str]:
        """导入 slots.json，返回文件里有的部分（proc / node / site）；没有文件或读不了返回空集合。"""
        if not self.slots_path.is_file():
            return set()
        try:
            data = json.loads(self.slots_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        if not isinstance(data, dict):
            return set()
        found: set[str] = set()
        for field, kind in (("slots", "proc"), ("nodes", "node"), ("sites", "site")):
            by_key = data.get(field)
            if not isinstance(by_key, dict):
                continue
            found.add(kind)
            for key, recs in by_key.items():
                if not isinstance(recs, dict):
                    continue
                try:
                    slot = int(slot_ts(str(key)))
                except (ValueError, OverflowError, OSError):
                    continue
                for name, rec in recs.items():
                    if not isinstance(rec, dict):
                        continue
                    if kind == "node":
                        vals = (as_int(rec.get("up")), as_int(rec.get("down")), 0, 0)
                    else:
                        vals = tuple(as_int(rec.get(k)) for k in SPLIT_KEYS)
                    if any(vals):
                        self._pend(kind, slot, str(name), *vals)
        return found

    def _slots_from_log(self, kinds: set[str]) -> None:
        try:
            for rec in iter_log(self.path):
                slot = slot_start(event_ts(rec, self._now()))
                up = as_int(rec.get("upload"))
                down = as_int(rec.get("download"))
                outbound = str(rec.get("outbound") or "")
                if "proc" in kinds:
                    self._add_slot(str(rec.get("process") or UNKNOWN), up, down, outbound, slot)
                if "node" in kinds:
                    self._add_node_slot(str(rec.get("node") or ""), up, down, slot)
                if "site" in kinds:
                    self._add_site_slot(str(rec.get("remote") or ""), up, down, outbound, slot)
        except OSError:
            pass

    def _load_seen_file(self) -> bool:
        if not self.seen_path.is_file():
            return False
        try:
            raw = json.loads(self.seen_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        names = raw.get("names") if isinstance(raw, dict) else raw
        if not isinstance(names, list):
            return False
        self.seen.update(str(n) for n in names if n)
        return True

    # ---------- 写库 ----------

    def _retention_cutoff(self, now: float) -> float:
        try:
            days = int(self._retention_days())
        except (TypeError, ValueError):
            days = 40
        days = max(7, min(days, 365))
        return now - days * 86400

    def flush(self) -> bool:
        ok = self._flush_main()
        if self.site_details is not None:
            self.site_details.flush()
        return ok

    def _flush_main(self) -> bool:
        """把内存里的改动写库。没有改动返回 False；写失败也返回 False，改动留着下次再写。"""
        with self._flush_lock:
            if self._closed:
                return False
            with self.lock:
                now = self._now()
                prune_due = now >= self._next_prune
                if not (
                    self._pending
                    or self._dirty_programs
                    or self._seen_new
                    or self._meta_pending
                    or self._pending_reset
                    or prune_due
                ):
                    return False
                cutoff = self._retention_cutoff(now)
                batch = self._pending
                self._pending = {}
                self._inflight = batch
                names = self._dirty_programs
                self._dirty_programs = set()
                programs = {n: _copy_program(self.programs[n]) for n in names if n in self.programs}
                seen_new = self._seen_new
                self._seen_new = set()
                meta = self._meta_pending
                self._meta_pending = {}
                reset = self._pending_reset
                self._pending_reset = False
                self._inflight_reset = reset
                gen = self._reset_gen
            rows = [(k, s, n, *v) for (k, s, n), v in batch.items() if s >= cutoff and any(v)]
            ok = True
            with self.db.lock:
                try:
                    self.db.write(
                        reset=reset,
                        programs=programs,
                        slot_rows=rows,
                        seen=seen_new,
                        prune_before=cutoff if prune_due else None,
                        meta=meta,
                    )
                except (sqlite3.Error, OSError) as exc:
                    ok = False
                    self._last_flush_error = str(exc) or type(exc).__name__
                    fmlog.log_exception("写数据库", exc)
                with self.lock:
                    self._inflight = {}
                    self._inflight_reset = False
                    if ok:
                        self._last_flush_error = ''
                        if prune_due:
                            self._next_prune = now + PRUNE_EVERY
                    else:
                        self._meta_pending = {**meta, **self._meta_pending}
                        self._seen_new |= seen_new
                        if self._reset_gen == gen:
                            for (kind, slot, name), vals in batch.items():
                                self._pend(kind, slot, name, *vals)
                            self._dirty_programs |= names
                            self._pending_reset = self._pending_reset or reset
            return ok

    def close(self) -> None:
        """写最后一次、关数据库（WAL 合回主文件）。可以重复调用；关了以后 flush 什么都不做。"""
        with self._flush_lock:
            if self._closed:
                return
        self.flush()
        with self._flush_lock:
            with self.db.lock:
                with self.lock:
                    if self._closed:
                        return
                    if not self.db.persistent:
                        raise OSError('累计数据库仅在内存中，不能确认持久保存；请恢复数据目录后保留进程并导出统计')
                    if self._last_flush_error or self._pending or self._dirty_programs or self._seen_new or self._meta_pending or self._pending_reset:
                        raise OSError('最终刷盘未成功，记录保留等待重试：'+(self._last_flush_error or '存在并发待写数据'))
                    self._closed = True
                try:
                    self.db.checkpoint()
                    if self.site_details is not None:
                        self.site_details.close()
                    self.db.close()
                except Exception:
                    with self.lock:
                        self._closed = False
                    raise

    def _append_log(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        with self._log_lock:
            try:
                if self.path.is_file() and self.path.stat().st_size > LOG_ROTATE_BYTES:
                    bak = self.path.with_suffix(".jsonl.old")
                    if bak.exists():
                        bak.unlink()
                    self.path.replace(bak)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(text)
            except OSError:
                return
        with self.lock:
            self.logged += len(rows)

    # ---------- 累加（调用方持 self.lock，或在构造阶段） ----------

    def _pend(self, kind: str, slot: int, name: str, a: int, b: int, c: int = 0, d: int = 0) -> None:
        key = (kind, slot, name)
        cur = self._pending.get(key)
        if cur is None:
            self._pending[key] = [a, b, c, d]
        else:
            cur[0] += a
            cur[1] += b
            cur[2] += c
            cur[3] += d

    def _note_process(self, name: str) -> None:
        if self._booting or name.lower() in CORE_NAMES:
            return
        if name.lower() in self._ignored():
            if name not in self.seen:
                self.seen.add(name)
                self._seen_new.add(name)
            return
        if name in self.seen:
            return
        self.seen.add(name)
        self._seen_new.add(name)
        self.new_apps.append({"process": name, "ts": int(self._now())})
        self.new_apps = self.new_apps[-MAX_NEW_APPS:]

    def _add_site(
        self,
        process: str,
        up: int,
        down: int,
        outbound: str,
        host: str,
        closed: bool = False,
        ts: float | None = None,
    ) -> None:
        site = site_key(host)
        if not site:
            return
        up = max(up, 0)
        down = max(down, 0)
        row = self.programs.setdefault(process, _blank_program())
        self._dirty_programs.add(process)
        srow = row["sites"].setdefault(site, _blank_site())
        srow["upload"] += up
        srow["download"] += down
        if outbound == "proxy":
            srow["proxy_up"] += up
            srow["proxy_down"] += down
        else:
            kind = outbound_kind(outbound)
            srow[kind + "_up"] = int(srow.get(kind + "_up") or 0) + up
            srow[kind + "_down"] = int(srow.get(kind + "_down") or 0) + down
        if closed:
            srow["conns"] += 1
        srow["last"] = int(self._now() if ts is None else ts)
        sites = row["sites"]
        if len(sites) > MAX_SITES:
            keep = _traffic_leaders([{"site": name, **rec} for name, rec in sites.items()], MAX_SITES)
            row["sites"] = {item["site"]: sites[item["site"]] for item in keep}

    def _add_slot(self, process: str, up: int, down: int, outbound: str, slot: int) -> None:
        name = process.strip() or UNKNOWN
        if name.lower() in CORE_NAMES:
            return
        up = max(int(up), 0)
        down = max(int(down), 0)
        if not up and not down:
            return
        if outbound == "proxy":
            self._pend("proc", slot, name, up, down, 0, 0)
        elif outbound == "direct":
            self._pend("proc", slot, name, 0, 0, up, down)
        else:
            self._pend("proc_" + outbound_kind(outbound), slot, name, up, down)

    def _add_node_slot(self, node: str, up: int, down: int, slot: int) -> None:
        node = (node or "").strip()
        if not node or node.upper() in DIRECT_OUT:
            return
        up = max(int(up), 0)
        down = max(int(down), 0)
        if not up and not down:
            return
        self._pend("node", slot, node, up, down)

    def _add_site_slot(self, host: str, up: int, down: int, outbound: str, slot: int) -> None:
        site = site_key(host)
        if not site:
            return
        up = max(int(up), 0)
        down = max(int(down), 0)
        if not up and not down:
            return
        if outbound == "proxy":
            self._pend("site", slot, site, up, down, 0, 0)
        elif outbound == "direct":
            self._pend("site", slot, site, 0, 0, up, down)
        else:
            self._pend("site_" + outbound_kind(outbound), slot, site, up, down)

    def _add(
        self,
        process: str,
        up: int,
        down: int,
        outbound: str,
        closed: bool = False,
        host: str = "",
        node: str = "",
        ts: float | None = None,
        with_slots: bool = True,
    ) -> None:
        name = process.strip() or UNKNOWN
        if name.lower() in CORE_NAMES:
            return
        up = max(up, 0)
        down = max(down, 0)
        if up == 0 and down == 0 and not closed:
            return
        now = self._now() if ts is None else ts
        is_new = name not in self.programs
        row = self.programs.setdefault(name, _blank_program())
        self._dirty_programs.add(name)
        if is_new:
            self._note_process(name)
        row["upload"] += up
        row["download"] += down
        if outbound == "proxy":
            row["proxy_up"] += up
            row["proxy_down"] += down
            if node:
                row["last_node"] = node
        else:
            kind = outbound_kind(outbound)
            row[kind + "_up"] = int(row.get(kind + "_up") or 0) + up
            row[kind + "_down"] = int(row.get(kind + "_down") or 0) + down
        if closed:
            row["conns"] += 1
        row["last"] = int(now)
        self._add_site(name, up, down, outbound, host, closed=closed, ts=now)
        if with_slots and (up or down):
            slot = slot_start(now)
            self._add_slot(name, up, down, outbound, slot)
            if outbound == "proxy":
                self._add_node_slot(node, up, down, slot)
            self._add_site_slot(host, up, down, outbound, slot)
            domain = host_of(host)
            if (not domain or any(ch in '/\\?#@' or unicodedata.category(ch) in {'Cc','Cf','Cs'} for ch in domain)):
                domain = ''
            if self.site_details is not None and outbound in {"proxy", "direct"}:
                values = (up, down, 0, 0) if outbound == 'proxy' else (0, 0, up, down)
                self.site_details.add(slot, name, site_key(domain) or '未知', values)

    def _closed_row(self, flow: dict[str, Any], now: float) -> dict[str, Any]:
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
            "end": int(now),
            "closed": True,
            "path": flow.get("path") or "",
        }

    def update(self, flows: list[dict[str, Any]]) -> None:
        """喂一轮 Clash 连接快照：按字节差累加，消失的连接记为结束。"""
        seen: set[str] = set()
        acc = {k: 0 for k in ("up", "down", *ALL_SPLIT_KEYS)}
        per: dict[str, list[int]] = {}
        ended: list[dict[str, Any]] = []
        with self.lock:
            if self._closed:
                raise RuntimeError('存储已关闭，拒绝后续采样写入')
            now = self._now()
            primed = self._primed
            for f in flows:
                cid = str(f.get("id") or "")
                if not cid:
                    continue
                seen.add(cid)
                up = as_int(f.get("upload"))
                down = as_int(f.get("download"))
                prev = self.last_bytes.get(cid)
                if prev is None:
                    du = up if primed else 0
                    dd = down if primed else 0
                else:
                    du = up - prev[0]
                    dd = down - prev[1]
                self.last_bytes[cid] = (up, down)
                if du or dd:
                    proc = str(f.get("process") or UNKNOWN)
                    ob = outbound_kind(str(f.get("outbound") or ""))
                    self._add(proc, du, dd, ob, host=str(f.get("remote") or ""), node=str(f.get("node") or ""), ts=now)
                    if du > 0:
                        acc["up"] += du
                    if dd > 0:
                        acc["down"] += dd
                    if ob in OUTBOUNDS:
                        if du > 0:
                            acc[f"{ob}_up"] += du
                        if dd > 0:
                            acc[f"{ob}_down"] += dd
                        slot = per.setdefault(f"{proc}|{ob}", [0, 0])
                        slot[0] += max(du, 0)
                        slot[1] += max(dd, 0)
                self.live[cid] = dict(f)
            for cid in [k for k in self.live if k not in seen]:
                rec = self._closed_row(self.live.pop(cid), now)
                self.last_bytes.pop(cid, None)
                self._add(
                    rec["process"],
                    0,
                    0,
                    rec.get("outbound") or "",
                    closed=True,
                    host=rec.get("remote") or "",
                    node=str(rec.get("node") or ""),
                    ts=now,
                )
                self.closed.append(rec)
                ended.append(rec)
            if len(self.closed) > MAX_CLOSED:
                self.closed = self.closed[-KEEP_CLOSED:]
            mono = self._mono()
            dt = max(mono - self._tick_at, 0.2)
            self._tick_at = mono
            alpha = min(1.0, max(0.2, 1.0 - math.exp(-dt / 1.2)))
            inst = {k: v / dt for k, v in acc.items()}
            self.speed = {
                k: (1.0 - alpha) * float(self.speed.get(k) or 0) + alpha * float(inst.get(k) or 0) for k in inst
            }
            new_proc: dict[str, dict[str, float]] = {}
            for key, pair in per.items():
                old = self.proc_speed.get(key) or {}
                new_proc[key] = {
                    "up": (1.0 - alpha) * float(old.get("up") or 0) + alpha * (pair[0] / dt),
                    "down": (1.0 - alpha) * float(old.get("down") or 0) + alpha * (pair[1] / dt),
                }
            for key, old in self.proc_speed.items():
                if key in new_proc:
                    continue
                up = (1.0 - alpha) * float(old.get("up") or 0)
                down = (1.0 - alpha) * float(old.get("down") or 0)
                if up >= 1 or down >= 1:
                    new_proc[key] = {"up": up, "down": down}
            self.proc_speed = new_proc
            self._primed = True
        self._append_log(ended)

    def set_wire_speed(self, up: int, down: int) -> None:
        with self.lock:
            self.wire_speed = {
                "up": float(max(as_int(up), 0)),
                "down": float(max(as_int(down), 0)),
                "at": self._mono(),
            }

    def note_totals(self, up_total: int, down_total: int) -> None:
        up_total = max(as_int(up_total), 0)
        down_total = max(as_int(down_total), 0)
        now = self._mono()
        with self.lock:
            if self._tot_up is not None and self._tot_at:
                dt = max(now - self._tot_at, 0.2)
                du = up_total - int(self._tot_up)
                dd = down_total - int(self._tot_down or 0)
                if du < 0 or dd < 0:
                    inst_up = inst_down = 0.0
                else:
                    inst_up = du / dt
                    inst_down = dd / dt
                alpha = min(1.0, max(0.2, 1.0 - math.exp(-dt / 1.0)))
                self.total_speed["up"] = (1.0 - alpha) * float(self.total_speed["up"]) + alpha * inst_up
                self.total_speed["down"] = (1.0 - alpha) * float(self.total_speed["down"]) + alpha * inst_down
                self._tot_ready = True
            self._tot_up = up_total
            self._tot_down = down_total
            self._tot_at = now

    def decay_speed(self, factor: float = 0.55) -> None:
        with self.lock:
            self.speed = {k: float(v) * factor for k, v in self.speed.items()}
            self.total_speed = {k: float(v) * factor for k, v in self.total_speed.items()}
            self.proc_speed = {
                k: {"up": float(v.get("up") or 0) * factor, "down": float(v.get("down") or 0) * factor}
                for k, v in self.proc_speed.items()
            }

    def reset(self) -> dict[str, Any]:
        """清零并报告是否持久保存；写入失败保留清零标记，由后续 flush 重试。"""
        with self.lock:
            if self._closed:
                return {"ok": False, "persisted": False, "pending": False,
                        "error": "统计库已关闭，未清零。"}
            if not self.db.persistent:
                return {"ok": False, "persisted": False, "pending": False,
                        "error": "统计数据库仅在内存中，暂不能持久清零；未修改当前统计。"}
        current = self._reset_result()
        if current["pending"]:
            # 重复请求只查询上一轮清零的状态，保留期间采集到的新流量。
            return current
        if self.site_details is not None:
            self.site_details.reset()
        with self.lock:
            self.programs = {}
            self.closed = []
            self.history = []
            self.new_apps = []
            self._pending = {}
            self._dirty_programs = set()
            self._pending_reset = True
            self._reset_gen += 1
        self.flush()
        return self._reset_result()

    def _reset_result(self) -> dict[str, Any]:
        health = self.storage_health()
        pending = health["reset_pending"]
        result: dict[str, Any] = {"ok": not pending, "persisted": not pending, "pending": pending}
        if pending:
            result["error"] = (
                "清零已在当前会话生效，但网站明细暂不可用，尚未全部持久保存；请保持程序运行并检查数据目录。"
                if self.site_details is not None and self.site_details.conn is None else
                "清零已在当前会话生效，但尚未全部写入磁盘；请保持程序运行，系统会自动重试。"
            )
        return result

    # ---------- 程序信息 ----------

    def _title_for(self, path: str) -> str:
        if not local_executable(path):
            return ''
        hit = self._desc_cache.get(path)
        if hit is None:
            try:
                hit = str(self._describe(path) or "")
            except Exception:
                hit = ""
            if len(self._desc_cache) > 512:
                self._desc_cache.clear()
            self._desc_cache[path] = hit
        return hit

    def remember_origin(self, process: str, cmd: str = "", path: str = "") -> None:
        """记下程序最近的命令行、可执行文件路径和文件描述（窗口里显示的名字）。"""
        name = process.strip()
        if not name or name.lower() in CORE_NAMES:
            return
        title = self._title_for(path) if path else ""
        with self.lock:
            row = self.programs.get(name)
            if row is None:
                return
            changed = False
            if cmd:
                short = shorten_cmd(cmd)
                if row.get("last_cmd") != short:
                    row["last_cmd"] = short
                    changed = True
            if path:
                if row.get("last_path") != path:
                    row["last_path"] = path
                    changed = True
                if title and row.get("title") != title:
                    row["title"] = title
                    changed = True
            if changed:
                self._dirty_programs.add(name)
        if path:
            self._on_icon(name, path)

    def program_path(self, name: str) -> str:
        with self.lock:
            return str((self.programs.get(name) or {}).get("last_path") or "")

    def recent_new(self) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.new_apps)

    def ack_new_events(self, events: list[dict[str, Any]]) -> None:
        """Remove exactly the displayed batch; later arrivals and duplicate ACKs are safe."""
        keys={(x["process"],x["ts"]) for x in events}
        with self.lock:
            self.new_apps=[x for x in self.new_apps if (x.get("process"),x.get("ts")) not in keys]

    def ack_new(self, process: str = "") -> None:
        with self.lock:
            if process:
                self.new_apps = [x for x in self.new_apps if x.get("process") != process]
            else:
                self.new_apps = []

    # ---------- 查询 ----------

    def _slot_aggs(self, after: float | None, cutoff: float) -> dict[str, dict[str, list[int]]]:
        """三类时段数据按名字合计：库里的 + 还没写库的，每个增量正好算一次。

        先拿 db.lock：flush 提交和清空 _inflight 都在 db.lock 里，所以读到的库和内存是同一时刻的。
        """
        out: dict[str, dict[str, list[int]]] = {k: {} for k in (*KINDS, *EXTRA_KINDS)}
        with self.db.lock:
            with self.lock:
                use_db = not (self._pending_reset or self._inflight_reset or self._closed)
                extra = [
                    (kind, name, tuple(vals))
                    for src in (self._pending, self._inflight)
                    for (kind, slot, name), vals in src.items()
                    if slot >= cutoff and (after is None or slot > after)
                ]
            if use_db:
                try:
                    for kind in (*KINDS, *EXTRA_KINDS):
                        for name, vals in self.db.slot_totals(kind, after=after, min_slot=cutoff).items():
                            out[kind][name] = list(vals)
                except sqlite3.Error as exc:
                    fmlog.log_exception("读数据库", exc)
        for kind, name, vals in extra:
            cur = out[kind].get(name)
            if cur is None:
                out[kind][name] = list(vals)
            else:
                for i in range(4):
                    cur[i] += vals[i]
        if self.site_details is not None:
            out['procsite'] = self.site_details.query(after)
        return out

    def ranking(self, period: str) -> dict[str, Any]:
        now = self._now()
        since, period = period_since(period, now)
        cutoff = self._retention_cutoff(now)
        aggs = self._slot_aggs(None if period == "all" else since - SLOT_SECONDS, cutoff)
        procs = self._classified_aggs(aggs, "proc")
        scoped_sites: dict[str, list[dict[str, Any]]] = {}
        for pair, values in aggs['procsite'].items():
            try:
                program, site = json.loads(pair)
            except (ValueError, TypeError):
                continue
            pup, pdown, dup, ddown = values
            scoped_sites.setdefault(program, []).append({'site':site,'upload':pup+dup,'download':pdown+ddown,
                'proxy_up':pup,'proxy_down':pdown,'direct_up':dup,'direct_down':ddown})
        if period == "all":
            with self.lock:
                merged = {name: {k: int(rec.get(k) or 0) for k in (*ALL_SPLIT_KEYS, "upload", "download")} for name, rec in self.programs.items()}
            for name, rec in procs.items():
                merged.setdefault(name, rec)
            procs = merged
        rows: list[dict[str, Any]] = []
        tot_up = tot_down = proxy_up = proxy_down = direct_up = direct_down = 0
        for name, rec in procs.items():
            pup = int(rec.get("proxy_up") or 0)
            pdown = int(rec.get("proxy_down") or 0)
            dup = int(rec.get("direct_up") or 0)
            ddown = int(rec.get("direct_down") or 0)
            extras = {k: int(rec.get(k) or 0) for k in EXTRA_INTS}
            up = int(rec.get('upload', pup + dup + extras['unknown_up'] + extras['rejected_up']))
            down = int(rec.get('download', pdown + ddown + extras['unknown_down'] + extras['rejected_down']))
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
                    "direct_total": dup + ddown,
                    **extras,
                }
            )
            tot_up += up
            tot_down += down
            proxy_up += pup
            proxy_down += pdown
            direct_up += dup
            direct_down += ddown
        _sort_by_traffic(rows)
        whole = max(tot_up + tot_down, 1)
        proxy_whole = max(proxy_up + proxy_down, 1)
        for i, row in enumerate(rows, 1):
            row["rank"] = i
            row["share"] = round(100.0 * (row["download"] + row["upload"]) / whole, 1)
            row["proxy_share"] = round(100.0 * int(row["proxy_total"]) / proxy_whole, 1)
        icons: list[tuple[str, str]] = []
        with self.lock:
            speeds = dict(self.proc_speed)
            for row in rows:
                rec = self.programs.get(row["process"]) or {}
                row["cmd"] = str(rec.get("last_cmd") or "")
                row["path"] = str(rec.get("last_path") or "")
                row["title"] = str(rec.get("title") or "")
                row["up_speed"] = sum(float((speeds.get(f"{row['process']}|{kind}") or {}).get("up") or 0) for kind in OUTBOUNDS)
                row["down_speed"] = sum(float((speeds.get(f"{row['process']}|{kind}") or {}).get("down") or 0) for kind in OUTBOUNDS)
                site_rows = ([{"site": host, **srec} for host, srec in (rec.get("sites") or {}).items()]
                             if period == 'all' else scoped_sites.get(row['process'], []))
                _sort_by_traffic(site_rows)
                row["sites"] = _traffic_leaders(site_rows, RANK_SITES)
                row['sites_coverage'] = self._coverage(period, now, lifetime=period == 'all', detail=True)
                row['sites_coverage']['top_only'] = True
                row['sites_coverage']['measured_outbounds'] = list(OUTBOUNDS) if period == 'all' else ['proxy', 'direct']
                row['sites_coverage']['enabled'] = period == 'all' or self._program_sites_enabled
                row['sites_since'] = 0 if period == 'all' else self._pair_since
                row['sites_period'] = period if period == 'all' or self._program_sites_enabled else None
                row['sites_note'] = ('累计网站仅为主要网站摘要。' if period == 'all' else (self.site_details.note() if self.site_details else '网站分时记录未启用。') + ' 分时网站明细只含已确定代理或直连的字节；未知与拒绝仅计入应用及全局网站统计。')
                if row["path"]:
                    icons.append((row["process"], row["path"]))
        for name, path in icons:
            self._on_icon(name, path)
        nodes = {name: {"up": vals[0], "down": vals[1]} for name, vals in aggs["node"].items()}
        sites = self._classified_aggs(aggs, "site")
        site_pack = self._site_rows(sites)
        # Inverse attribution uses the bounded time-series detail, never lifetime
        # program summaries (whose time window differs from global site totals).
        by_site: dict[str, list[dict[str, Any]]] = {}
        visible_sites = {row['site'] for row in site_pack['rows']}
        titles = {row['process']: row['title'] for row in rows}
        for process, details in scoped_sites.items():
            for detail in details:
                if detail['site'] not in visible_sites:
                    continue
                by_site.setdefault(detail['site'], []).append({
                    **{k: v for k, v in detail.items() if k != 'site'},
                    'process': process, 'title': titles.get(process, '')})
        pair_coverage = {**self._coverage(period, now, detail=True),
                         'enabled': self._program_sites_enabled,
                         'measured_outbounds': ['proxy', 'direct'], 'top_only': True}
        pair_note = ((self.site_details.note() if self.site_details else '网站分时记录未启用。')
                     + ' 仅列出已保存的程序明细，可能不覆盖网站总量；仅含已确定代理或直连的字节。')
        program_details: dict[str, dict[str, Any]] = {}
        for site_row in site_pack['rows']:
            attributed = by_site.get(site_row['site'], [])
            _sort_by_traffic(attributed)
            program_details[site_row['site']] = {
                'rows': _traffic_leaders(attributed, RANK_SITES),
                'coverage': dict(pair_coverage), 'note': pair_note}
        site_pack['program_details'] = program_details
        return {
            "upload": tot_up,
            "download": tot_down,
            "proxy_upload": proxy_up,
            "proxy_download": proxy_down,
            "direct_upload": direct_up,
            "direct_download": direct_down,
            **{k: sum(r[k] for r in rows) for k in EXTRA_INTS},
            **{k.replace('_up', '_upload').replace('_down', '_download'): sum(r[k] for r in rows) for k in EXTRA_INTS},
            "since": int(since),
            "coverage": self._coverage(period, now, lifetime=period == 'all'),
            "rows": rows,
            "nodes": {**self._node_rows(nodes), 'coverage': {**self._coverage(period, now), 'identity': 'node', 'measured_outbounds': ['proxy']}},
            "sites": {**site_pack, 'coverage': {**self._coverage(period, now), 'identity': 'site', 'top_only': True}},
        }

    @staticmethod
    def _classified_aggs(aggs: dict, scope: str) -> dict[str, dict[str, int]]:
        result = {name: dict(zip(SPLIT_KEYS, values)) for name, values in aggs[scope].items()}
        for kind in ('unknown', 'rejected'):
            for name, values in aggs[scope + '_' + kind].items():
                row = result.setdefault(name, {})
                row[kind + '_up'], row[kind + '_down'] = values[:2]
        return result

    def _coverage(self, period: str, now: float, *, lifetime: bool = False, detail: bool = False) -> dict[str, Any]:
        requested, _ = period_since(period, now)
        cutoff = self._retention_cutoff(now)
        if detail and self.site_details is not None:
            cutoff = max(self._pair_since, now - self.site_details.policy.retention_days * 86400)
        return {'source': 'clash_connections', 'scope': 'observed', 'period': period,
                'since': 0 if lifetime else int(max(requested, cutoff)),
                'lifetime': lifetime, 'retention_limited': not lifetime and cutoff > requested,
                'slot_seconds': SLOT_SECONDS, 'complete': False, 'measured_outbounds': list(OUTBOUNDS),
                'identity': 'process_name', 'historical_classification_recomputed': False}

    def storage_health(self) -> dict[str, Any]:
        with self.lock:
            reset_pending = bool(self._pending_reset or self._inflight_reset)
            pending = bool(self._pending or self._inflight or self._dirty_programs or self._seen_new
                           or self._meta_pending or self._pending_reset or self._inflight_reset)
            persistent = self.db.persistent
            failed = bool(self._last_flush_error)
        detail_failed = bool(self.site_details is not None and self.site_details.error)
        if self.site_details is not None:
            with self.site_details._lock:
                reset_pending = reset_pending or self.site_details._reset_pending
                pending = pending or bool(self.site_details._pending or self.site_details._inflight or self.site_details._reset_pending)
        return {'persistent': persistent, 'healthy': persistent and not failed and not detail_failed,
                'pending': pending, 'reset_pending': bool(reset_pending),
                'last_error': ('统计暂存内存，尚未写入磁盘。' if not persistent else
                               '统计写盘失败，待写数据保留。' if failed else
                               '网站明细暂不可用，程序累计状态单独保存。' if detail_failed else ''),
                'detail_healthy': not detail_failed}

    @staticmethod
    def _node_rows(agg: dict[str, dict[str, int]]) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        tot = 0
        for name, rec in agg.items():
            up = int(rec.get("up") or 0)
            down = int(rec.get("down") or 0)
            if not up and not down:
                continue
            rows.append({"node": name, "upload": up, "download": down})
            tot += up + down
        _sort_by_traffic(rows)
        whole = max(tot, 1)
        for i, row in enumerate(rows, 1):
            row["rank"] = i
            row["share"] = round(100.0 * (row["download"] + row["upload"]) / whole, 1)
        return {"upload": sum(r["upload"] for r in rows), "download": sum(r["download"] for r in rows), "rows": rows}

    @staticmethod
    def _site_rows(agg: dict[str, dict[str, int]]) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        tot = 0
        for name, rec in agg.items():
            pup = int(rec.get("proxy_up") or 0)
            pdown = int(rec.get("proxy_down") or 0)
            dup = int(rec.get("direct_up") or 0)
            ddown = int(rec.get("direct_down") or 0)
            extras = {k: int(rec.get(k) or 0) for k in EXTRA_INTS}
            up = int(rec.get('upload', pup + dup + extras['unknown_up'] + extras['rejected_up']))
            down = int(rec.get('download', pdown + ddown + extras['unknown_down'] + extras['rejected_down']))
            if not up and not down:
                continue
            rows.append(
                {
                    "site": name,
                    "upload": up,
                    "download": down,
                    "proxy_up": pup,
                    "proxy_down": pdown,
                    "direct_up": dup,
                    "direct_down": ddown,
                    "proxy_total": pup + pdown,
                    "direct_total": dup + ddown,
                    **extras,
                }
            )
            tot += up + down
        _sort_by_traffic(rows)
        whole = max(tot, 1)
        for i, row in enumerate(rows, 1):
            row["rank"] = i
            row["share"] = round(100.0 * (row["download"] + row["upload"]) / whole, 1)
        return {
            "upload": sum(r["upload"] for r in rows),
            "download": sum(r["download"] for r in rows),
            "rows": _traffic_leaders(rows, RANK_SITE_ROWS),
        }

    def record_history(self) -> None:
        rec = {"t": float(self._now()), **{k: float(v) for k, v in self.rates().items()}}
        with self.lock:
            self.history.append(rec)
            if len(self.history) > HISTORY_LEN:
                self.history = self.history[-HISTORY_LEN:]

    def history_rows(self) -> list[dict[str, float]]:
        with self.lock:
            return list(self.history)

    def rates(self) -> dict[str, float]:
        with self.lock:
            out = dict(self.speed)
            now = self._mono()
            wire_at = float(self.wire_speed.get("at") or 0)
            if wire_at and now - wire_at < 2.5:
                out["up"] = float(self.wire_speed.get("up") or 0)
                out["down"] = float(self.wire_speed.get("down") or 0)
            elif self._tot_ready:
                out["up"] = float(self.total_speed.get("up") or 0)
                out["down"] = float(self.total_speed.get("down") or 0)
            return out

    def recent_closed(self, limit: int = 120) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.closed[-limit:])

    def program_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        with self.lock:
            for name, rec in self.programs.items():
                item = {k: v for k, v in rec.items() if k != "sites"}
                item["process"] = name
                site_rows = [{"site": host, **srec} for host, srec in (rec.get("sites") or {}).items()]
                _sort_by_traffic(site_rows)
                item["sites"] = site_rows
                rows.append(item)
        _sort_by_traffic(rows)
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
                        sites.append({"site": s["site"], "upload": su, "download": sd, "conns": s.get("conns") or 0})
                _sort_by_traffic(sites)
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
                        "title": p.get("title") or "",
                        "up_speed": float(sp.get("up") or 0),
                        "down_speed": float(sp.get("down") or 0),
                        "sites": sites,
                    }
                )
                tot_up += up
                tot_down += down
            _sort_by_traffic(items)
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

    def live_view(self) -> dict[str, Any]:
        rates = self.rates()
        skip = {str(n).lower() for n in self._ignored()}
        bag: dict[str, dict[str, Any]] = {}
        icons: dict[str, str] = {}

        def entry(name: str, titles: dict[str, str]) -> dict[str, Any]:
            row = bag.get(name)
            if row is None:
                row = bag[name] = {
                    "process": name,
                    "up": 0.0,
                    "down": 0.0,
                    "proxy_down": 0.0,
                    "direct_down": 0.0,
                    "proxy_up": 0.0,
                    "direct_up": 0.0,
                    **{k: 0.0 for k in EXTRA_INTS},
                    "conns": 0,
                    "title": titles.get(name, ""),
                }
            return row

        with self.lock:
            spd = dict(self.speed)
            titles = {n: str(rec.get("title") or "") for n, rec in self.programs.items()}
            paths = {n: str(rec.get("last_path") or "") for n, rec in self.programs.items()}
            for key, rec in self.proc_speed.items():
                name, _, kind = key.partition("|")
                if name.lower() in skip or name.lower() in CORE_NAMES:
                    continue
                row = entry(name, titles)
                up = float(rec.get("up") or 0)
                down = float(rec.get("down") or 0)
                row["up"] = float(row["up"]) + up
                row["down"] = float(row["down"]) + down
                if kind == "proxy":
                    row["proxy_down"] = float(row["proxy_down"]) + down
                    row["proxy_up"] = float(row["proxy_up"]) + up
                elif kind in OUTBOUNDS:
                    row[kind + "_down"] = float(row[kind + "_down"]) + down
                    row[kind + "_up"] = float(row[kind + "_up"]) + up
                if paths.get(name):
                    icons[name] = paths[name]
            for rec in self.live.values():
                name = str(rec.get("process") or "")
                if not name or name.lower() in skip or name.lower() in CORE_NAMES:
                    continue
                row = entry(name, titles)
                row["conns"] = int(row.get("conns") or 0) + 1
                if paths.get(name):
                    icons[name] = paths[name]
        for name, path in icons.items():
            self._on_icon(name, path)
        eaters = sorted(
            bag.values(),
            key=lambda r: (-(float(r["up"]) + float(r["down"])), -int(r.get("conns") or 0)),
        )
        eaters = [r for r in eaters if float(r["up"]) + float(r["down"]) >= 64 or int(r.get("conns") or 0)]
        return {
            "up": float(rates.get("up") or 0),
            "down": float(rates.get("down") or 0),
            "proxy_down": float(spd.get("proxy_down") or 0),
            "direct_down": float(spd.get("direct_down") or 0),
            "proxy_up": float(spd.get("proxy_up") or 0),
            "direct_up": float(spd.get("direct_up") or 0),
            **{k: float(spd.get(k) or 0) for k in EXTRA_INTS},
            "eaters": eaters,
        }
