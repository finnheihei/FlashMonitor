"""SQLite 存储：数据目录下的 flashmonitor.db（WAL 模式）。

只开一个连接，所有访问都经过 self.lock（可重入，调用方可以把几次读写包在一起）。
写入都在 write() 的一个事务里完成：要么全部生效，要么全部回滚。
文件损坏时挪到一边另建新库；实在打不开（被锁、没权限）就退到内存库，这次运行的数据不落盘，并写日志。
"""

from __future__ import annotations

import sqlite3
import json
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from . import log as fmlog

SCHEMA_VERSION = "1"
DB_NAME = "flashmonitor.db"
KINDS = ("proc", "node", "site", "procsite")
EXTRA_INTS = ("unknown_up", "unknown_down", "rejected_up", "rejected_down")
EXTRA_KINDS = tuple(f"{kind}_{route}" for kind in ("proc", "site") for route in ("unknown", "rejected"))
PROGRAM_INTS = ("upload", "download", "proxy_up", "proxy_down", "direct_up", "direct_down", "conns", "last")
PROGRAM_TEXTS = ("last_node", "last_cmd", "last_path", "title")
SITE_INTS = PROGRAM_INTS

SCHEMA = f"""
-- New classifications are kept separately: old counters are never reinterpreted.
CREATE TABLE IF NOT EXISTS outbound_extra (
    scope TEXT NOT NULL,
    name TEXT NOT NULL,
    site TEXT NOT NULL DEFAULT '',
    {", ".join(f"{c} INTEGER NOT NULL DEFAULT 0" for c in EXTRA_INTS)},
    PRIMARY KEY(scope, name, site)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

-- 15 分钟时段合计。kind：proc 程序 / node 节点 / site 网站；
-- slot：时段开始的 unix 秒（按本地时间对齐到 15 分钟）。
-- 节点只有代理流量，上 / 下行记在 proxy_up / proxy_down。
CREATE TABLE IF NOT EXISTS slot_traffic (
    kind TEXT NOT NULL,
    slot INTEGER NOT NULL,
    name TEXT NOT NULL,
    proxy_up INTEGER NOT NULL DEFAULT 0,
    proxy_down INTEGER NOT NULL DEFAULT 0,
    direct_up INTEGER NOT NULL DEFAULT 0,
    direct_down INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (kind, slot, name)
) WITHOUT ROWID;

-- 每个程序的累计，不受保留天数影响
CREATE TABLE IF NOT EXISTS programs (
    name TEXT PRIMARY KEY,
    {", ".join(f"{c} INTEGER NOT NULL DEFAULT 0" for c in PROGRAM_INTS)},
    {", ".join(f"{c} TEXT NOT NULL DEFAULT ''" for c in PROGRAM_TEXTS)}
) WITHOUT ROWID;

-- 每个程序流量最大的若干网站
CREATE TABLE IF NOT EXISTS program_sites (
    program TEXT NOT NULL,
    site TEXT NOT NULL,
    {", ".join(f"{c} INTEGER NOT NULL DEFAULT 0" for c in SITE_INTS)},
    PRIMARY KEY (program, site)
) WITHOUT ROWID;

-- Existing tables only. Program/site time detail is opt-in, never created by default.
-- 出现过的程序（新程序提醒用）
CREATE TABLE IF NOT EXISTS seen (
    name TEXT PRIMARY KEY
) WITHOUT ROWID;
"""

PAIR_SCHEMA = """
CREATE TABLE IF NOT EXISTS program_site_slots (
    slot INTEGER NOT NULL,
    name TEXT NOT NULL,
    proxy_up INTEGER NOT NULL DEFAULT 0,
    proxy_down INTEGER NOT NULL DEFAULT 0,
    direct_up INTEGER NOT NULL DEFAULT 0,
    direct_down INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (slot, name)
) WITHOUT ROWID;
"""

_UPSERT_SLOT = """
INSERT INTO slot_traffic (kind, slot, name, proxy_up, proxy_down, direct_up, direct_down)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (kind, slot, name) DO UPDATE SET
    proxy_up = proxy_up + excluded.proxy_up,
    proxy_down = proxy_down + excluded.proxy_down,
    direct_up = direct_up + excluded.direct_up,
    direct_down = direct_down + excluded.direct_down
"""
_PROGRAM_COLS = ("name", *PROGRAM_INTS, *PROGRAM_TEXTS)
_UPSERT_PAIR_SLOT = """
INSERT INTO program_site_slots VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(slot, name) DO UPDATE SET
proxy_up=proxy_up+excluded.proxy_up, proxy_down=proxy_down+excluded.proxy_down,
direct_up=direct_up+excluded.direct_up, direct_down=direct_down+excluded.direct_down
"""
_UPSERT_PROGRAM = (
    f"INSERT INTO programs ({', '.join(_PROGRAM_COLS)}) VALUES ({', '.join('?' * len(_PROGRAM_COLS))}) "
    f"ON CONFLICT (name) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in _PROGRAM_COLS[1:])}"
)
_SITE_COLS = ("program", "site", *SITE_INTS)
_INSERT_SITE = f"INSERT INTO program_sites ({', '.join(_SITE_COLS)}) VALUES ({', '.join('?' * len(_SITE_COLS))})"

SlotRow = tuple  # (kind, slot, name, proxy_up, proxy_down, direct_up, direct_down)


class TrafficDB:
    def __init__(self, path: Path, *, program_sites_enabled: bool = False) -> None:
        self.path = Path(path)
        self.program_sites_enabled = program_sites_enabled
        self.lock = threading.RLock()
        self.persistent = True
        self.conn: sqlite3.Connection | None = self._open()
        if program_sites_enabled:
            try:
                self.conn.executescript(PAIR_SCHEMA)
            except Exception:
                self.close()
                raise

    # ---------- 打开 / 关闭 ----------

    @staticmethod
    def _connect(target: str) -> sqlite3.Connection:
        conn = sqlite3.connect(target, timeout=5.0, isolation_level=None, check_same_thread=False)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(SCHEMA)
        except BaseException:
            conn.close()
            raise
        return conn

    def _move_aside(self) -> None:
        stamp = int(time.time())
        for suffix in ("", "-wal", "-shm"):
            src = Path(f"{self.path}{suffix}")
            if src.exists():
                try:
                    src.replace(Path(f"{self.path}.corrupt-{stamp}{suffix}"))
                except OSError:
                    pass

    def _open(self) -> sqlite3.Connection:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                return self._connect(str(self.path))
            except sqlite3.OperationalError:
                raise  # 被锁住、没权限之类：不动文件
            except sqlite3.DatabaseError as exc:
                fmlog.log_exception("数据库损坏，已另建新库", exc)
                self._move_aside()
                return self._connect(str(self.path))
        except (sqlite3.Error, OSError) as exc:
            fmlog.log_exception("打不开数据库，这次改用内存库", exc)
        self.persistent = False
        return self._connect(":memory:")

    def close(self) -> None:
        with self.lock:
            if self.conn is not None:
                try:
                    self.conn.close()
                finally:
                    self.conn = None

    def checkpoint(self) -> None:
        """Confirm final WAL checkpoint; surface disk/reader failures before exit."""
        with self.lock:
            busy, written, copied = self._db().execute('PRAGMA wal_checkpoint(FULL)').fetchone()
            if busy or written != copied:
                raise sqlite3.OperationalError('最终数据库检查点未完成（读取占用或写盘失败），保留进程等待重试')

    def _db(self) -> sqlite3.Connection:
        if self.conn is None:
            raise sqlite3.ProgrammingError("数据库已关闭")
        return self.conn

    # ---------- 读 ----------

    def meta(self, key: str) -> str | None:
        with self.lock:
            row = self._db().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def migrated(self) -> bool:
        return self.meta("schema") is not None

    def load_programs(self) -> dict[str, dict[str, Any]]:
        with self.lock:
            db = self._db()
            prows = db.execute(f"SELECT {', '.join(_PROGRAM_COLS)} FROM programs").fetchall()
            srows = db.execute(f"SELECT {', '.join(_SITE_COLS)} FROM program_sites").fetchall()
            extra_rows = db.execute('SELECT scope,name,site,' + ','.join(EXTRA_INTS) + ' FROM outbound_extra').fetchall()
        out: dict[str, dict[str, Any]] = {}
        n_int = len(PROGRAM_INTS)
        for rec in prows:
            row: dict[str, Any] = {c: int(v or 0) for c, v in zip(PROGRAM_INTS, rec[1 : 1 + n_int])}
            row.update({c: str(v or "") for c, v in zip(PROGRAM_TEXTS, rec[1 + n_int :])})
            row["sites"] = {}
            out[str(rec[0])] = row
        for rec in srows:
            prog = out.get(str(rec[0]))
            if prog is not None:
                prog["sites"][str(rec[1])] = {c: int(v or 0) for c, v in zip(SITE_INTS, rec[2:])}
        for scope, name, site, *values in extra_rows:
            program = out.get(name)
            if program is None:
                continue
            target = program if scope == 'proc' else program['sites'].get(site)
            if target is not None:
                target.update(dict(zip(EXTRA_INTS, values)))
        for program in out.values():
            for target in (program, *program['sites'].values()):
                for key in EXTRA_INTS:
                    target.setdefault(key, 0)
        return out

    def load_seen(self) -> set[str]:
        with self.lock:
            return {str(r[0]) for r in self._db().execute("SELECT name FROM seen")}

    def slot_totals(
        self, kind: str, *, after: float | None = None, min_slot: float | None = None
    ) -> dict[str, tuple[int, int, int, int]]:
        """某类时段数据按名字合计。after：只算 slot > after；min_slot：只算 slot >= min_slot。"""
        if kind == "procsite":
            if not self.program_sites_enabled:
                return {}
            sql = "SELECT name, SUM(proxy_up), SUM(proxy_down), SUM(direct_up), SUM(direct_down) FROM program_site_slots WHERE 1=1"
            params: list[Any] = []
        else:
            sql = (
                "SELECT name, SUM(proxy_up), SUM(proxy_down), SUM(direct_up), SUM(direct_down) "
                "FROM slot_traffic WHERE kind = ?"
            )
            params = [kind]
        if after is not None:
            sql += " AND slot > ?"
            params.append(after)
        if min_slot is not None:
            sql += " AND slot >= ?"
            params.append(min_slot)
        sql += " GROUP BY name"
        with self.lock:
            rows = self._db().execute(sql, params).fetchall()
        return {str(r[0]): (int(r[1] or 0), int(r[2] or 0), int(r[3] or 0), int(r[4] or 0)) for r in rows}

    def count_slots(self, before: float | None = None) -> int:
        sql = "SELECT COUNT(*) FROM slot_traffic"
        params: tuple[Any, ...] = ()
        if before is not None:
            sql += " WHERE slot < ?"
            params = (before,)
        with self.lock:
            return int(self._db().execute(sql, params).fetchone()[0])

    # ---------- 写 ----------

    def write(
        self,
        *,
        reset: bool = False,
        programs: dict[str, dict[str, Any]] | None = None,
        slot_rows: Iterable[SlotRow] = (),
        seen: Iterable[str] = (),
        prune_before: float | None = None,
        meta: dict[str, str] | None = None,
    ) -> None:
        """一个事务：可选清空 → 覆盖程序行（连同网站）→ 时段增量累加 → 记新程序 → 清过期 → 写 meta。"""
        with self.lock:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                if reset:
                    db.execute("DELETE FROM slot_traffic")
                    if self.program_sites_enabled:
                        db.execute("DELETE FROM program_site_slots")
                    db.execute("DELETE FROM outbound_extra")
                    db.execute("DELETE FROM programs")
                    db.execute("DELETE FROM program_sites")
                for name, row in (programs or {}).items():
                    db.execute(
                        _UPSERT_PROGRAM,
                        (
                            name,
                            *(int(row.get(c) or 0) for c in PROGRAM_INTS),
                            *(str(row.get(c) or "") for c in PROGRAM_TEXTS),
                        ),
                    )
                    db.execute("DELETE FROM outbound_extra WHERE name = ?", (name,))
                    extra_rows = [('proc', name, '', *(int(row.get(k) or 0) for k in EXTRA_INTS))]
                    extra_rows.extend(('site', name, site, *(int(s.get(k) or 0) for k in EXTRA_INTS))
                                      for site, s in (row.get('sites') or {}).items())
                    db.executemany('INSERT INTO outbound_extra VALUES(?,?,?,?,?,?,?)',
                                   [r for r in extra_rows if any(r[3:])])
                    db.execute("DELETE FROM program_sites WHERE program = ?", (name,))
                    sites = row.get("sites") or {}
                    if sites:
                        db.executemany(
                            _INSERT_SITE,
                            [(name, site, *(int(s.get(c) or 0) for c in SITE_INTS)) for site, s in sites.items()],
                        )
                rows = list(slot_rows)
                pairs = [tuple(row[1:]) for row in rows if row[0] == 'procsite']
                for row in pairs:
                    subject = json.loads(row[1])
                    if not isinstance(subject, list) or len(subject) != 2 or not all(isinstance(v, str) for v in subject):
                        raise ValueError('invalid program/site identity')
                db.executemany(_UPSERT_SLOT, [row for row in rows if row[0] != 'procsite'])
                if pairs and not self.program_sites_enabled:
                    raise ValueError('program/site detail is not enabled')
                if self.program_sites_enabled:
                    db.executemany(_UPSERT_PAIR_SLOT, pairs)
                db.executemany("INSERT OR IGNORE INTO seen (name) VALUES (?)", [(n,) for n in seen])
                if prune_before is not None:
                    db.execute("DELETE FROM slot_traffic WHERE slot < ?", (prune_before,))
                    if self.program_sites_enabled:
                        db.execute("DELETE FROM program_site_slots WHERE slot < ?", (prune_before,))
                for key, value in (meta or {}).items():
                    db.execute(
                        "INSERT INTO meta (key, value) VALUES (?, ?) "
                        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                        (key, str(value)),
                    )
                db.execute("COMMIT")
            except BaseException:
                try:
                    db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
