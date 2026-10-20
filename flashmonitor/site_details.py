"""Optional bounded domain counters, isolated from lifetime traffic and the main DB."""
from __future__ import annotations
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

FORMAT = 'FlashMonitor.site-detail.v1'
SLOT_SECONDS = 900
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS slots(slot INTEGER NOT NULL,name TEXT NOT NULL,
pu INTEGER NOT NULL,pd INTEGER NOT NULL,du INTEGER NOT NULL,dd INTEGER NOT NULL,
PRIMARY KEY(slot,name)) WITHOUT ROWID;
"""
UPSERT = """INSERT INTO slots VALUES(?,?,?,?,?,?) ON CONFLICT(slot,name) DO UPDATE SET
pu=pu+excluded.pu,pd=pd+excluded.pd,du=du+excluded.du,dd=dd+excluded.dd"""


@dataclass(frozen=True)
class DetailPolicy:
    retention_days: int = 7
    max_rows: int = 20000
    max_db_bytes: int = 8 * 1024 * 1024
    max_pending: int = 2000
    max_key_bytes: int = 512

    def __post_init__(self):
        if self.retention_days not in {7,30} or min(self.max_rows,self.max_pending,self.max_key_bytes)<=0 or self.max_db_bytes<16384:
            raise ValueError('invalid site-detail policy')


class SiteDetails:
    def __init__(self, path: Path, *, policy=DetailPolicy(), clock=time.time):
        self.path, self.policy, self.clock = Path(path), policy, clock
        self._lock, self._io = threading.RLock(), threading.RLock()
        self._pending, self._inflight = {}, {}
        self._reset_pending = False
        self._closed = False
        self.partial, self.error = False, ''
        self.since, self.dropped = int(clock()), 0
        self.conn = None
        try:
            existed = self.path.exists() and self.path.stat().st_size > 0
            if existed and self.path.stat().st_size > policy.max_db_bytes:
                raise OSError('既有明细库超过硬预算，未清理或改写')
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=1, isolation_level=None, check_same_thread=False)
            self.conn = conn
            if existed:
                marker = conn.execute("SELECT value FROM meta WHERE key='format'").fetchone()
                if not marker or marker[0] != FORMAT:
                    raise ValueError('明细文件格式未知，未覆盖')
            conn.execute('PRAGMA journal_mode=DELETE')
            conn.execute('PRAGMA synchronous=FULL')
            conn.execute('PRAGMA temp_store=MEMORY')
            page_size = conn.execute('PRAGMA page_size').fetchone()[0]
            cap = policy.max_db_bytes // page_size
            if conn.execute(f'PRAGMA max_page_count={cap}').fetchone()[0] > cap:
                raise OSError('明细库不能满足硬容量上限')
            conn.executescript(SCHEMA)
            conn.execute("INSERT OR IGNORE INTO meta VALUES('format',?)",(FORMAT,))
            conn.execute("INSERT OR IGNORE INTO meta VALUES('since',?)",(str(self.since),))
            meta = dict(conn.execute('SELECT key,value FROM meta'))
            self.since = int(meta['since'])
            self.partial = meta.get('partial') == '1'
            self.dropped = int(meta.get('dropped','0'))
        except (OSError,ValueError,sqlite3.Error) as exc:
            self.error = '网站明细暂不可用（程序总累计继续保存）：'+str(exc)
            self.partial = True
            if self.conn is not None:
                self.conn.close();self.conn = None

    def _gap(self, count=1):
        with self._lock:
            self.partial = True
            self.dropped += count

    def add(self, at, program, site, values):
        key = json.dumps([program,site],ensure_ascii=False,separators=(',',':'))
        item = (int(at)//SLOT_SECONDS*SLOT_SECONDS,key)
        with self._lock:
            if self._closed:
                raise RuntimeError('site detail is closed')
            if self.conn is None:
                return
            if len(key.encode('utf-8')) > self.policy.max_key_bytes:
                self._gap();return
            old = self._pending.get(item)
            if old is None and len(self._pending)+len(self._inflight) >= self.policy.max_pending:
                self._gap();return
            total = [a+b for a,b in zip(old or (0,0,0,0),values)]
            if any(v<0 or v>2**63-1 for v in total):
                self._gap();return
            self._pending[item] = total

    def _trim(self, reserve=0):
        conn = self.conn
        count = conn.execute('SELECT count(*) FROM slots').fetchone()[0]
        remove = max(0,count+reserve-self.policy.max_rows)
        if remove:
            conn.execute('DELETE FROM slots WHERE (slot,name) IN (SELECT slot,name FROM slots ORDER BY slot,name LIMIT ?)',(remove,))
            self._gap(remove)

    def _write_meta(self):
        self.conn.executemany('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                              [('partial','1' if self.partial else '0'),('dropped',str(self.dropped))])

    def flush(self):
        with self._io:
            if self._closed or self.conn is None:
                return True
            with self._lock:
                batch = self._pending;self._pending={};self._inflight=batch
                reset = self._reset_pending
            cutoff = self.clock()-self.policy.retention_days*86400
            rows = [(slot,name,*vals) for (slot,name),vals in batch.items() if slot>=cutoff]
            if len(rows)>self.policy.max_rows:
                self._gap(len(rows)-self.policy.max_rows)
                rows=sorted(rows,key=lambda r:(r[0],r[1]))[-self.policy.max_rows:]
            # Retry physical SQLITE_FULL once after discarding a bounded set of oldest detail rows.
            for attempt in range(2):
                try:
                    self.conn.execute('BEGIN IMMEDIATE')
                    if reset:self.conn.execute('DELETE FROM slots')
                    self.conn.execute('DELETE FROM slots WHERE slot < ?',(cutoff,))
                    keys = [(r[0],r[1]) for r in rows]
                    existing = {key for key in keys if self.conn.execute('SELECT 1 FROM slots WHERE slot=? AND name=?',key).fetchone()}
                    self._trim(len(keys)-len(existing))
                    self.conn.executemany(UPSERT,rows)
                    self._trim()
                    self._write_meta()
                    self.conn.execute('COMMIT')
                    with self._lock:
                        self._inflight={}
                        self._reset_pending=False
                    self.error=''
                    return True
                except sqlite3.Error as exc:
                    if self.conn.in_transaction:self.conn.execute('ROLLBACK')
                    if getattr(exc,'sqlite_errorcode',None)==sqlite3.SQLITE_FULL:
                        self._gap(len(rows))
                        # Main cumulative DB is never part of this transaction or pruning.
                        try:
                            self.conn.execute('DELETE FROM slots WHERE (slot,name) IN (SELECT slot,name FROM slots ORDER BY slot,name LIMIT ?)',
                                              (max(1,self.policy.max_rows//4),))
                        except sqlite3.Error:
                            pass
                        if attempt==0:
                            continue
                        with self._lock:self._inflight={}
                        try:self._write_meta()
                        except sqlite3.Error:pass
                        self.error='网站明细达到容量上限，已舍弃部分明细；程序总累计不变。'
                        return True
                    self.error='网站明细写盘失败，待写记录保留：'+str(exc)
                    with self._lock:
                        for key,vals in batch.items():
                            old=self._pending.get(key)
                            if old is None and len(self._pending)>=self.policy.max_pending:
                                self._gap();continue
                            total=[a+b for a,b in zip(old or (0,0,0,0),vals)]
                            if any(v>2**63-1 for v in total):
                                self._gap();continue
                            self._pending[key]=total
                        self._inflight={}
                    return False
            return False

    def query(self, after=None):
        with self._io:
            cutoff=self.clock()-self.policy.retention_days*86400
            threshold=max(cutoff,after+1 if after is not None else cutoff)
            out={}
            if self.conn is not None and not self._closed and not self._reset_pending:
                try:
                    out={name:list(values) for name,*values in self.conn.execute(
                        'SELECT name,sum(pu),sum(pd),sum(du),sum(dd) FROM slots WHERE slot>=? GROUP BY name',(threshold,))}
                except sqlite3.Error as exc:
                    self._gap()
                    self.error='网站明细读取失败，程序总累计继续可用：'+str(exc)
            with self._lock:
                for src in (self._pending,self._inflight):
                    for (slot,name),values in src.items():
                        if slot>=threshold:out[name]=[a+b for a,b in zip(out.get(name,(0,0,0,0)),values)]
            return out

    def note(self):
        text=f'网站明细仅保留 {self.policy.retention_days} 天，最多 {self.policy.max_rows:,} 行 / {self.policy.max_db_bytes//1024//1024} MiB；仅主要明细，不代表全部程序流量。'
        text+=' 未补造启用前的历史记录。'
        text+=f' 明细记录起点：{time.strftime("%Y-%m-%d",time.localtime(self.since))}。'
        if self.partial:text+=' 已因预算或不可用丢失部分明细。'
        if self.error:text+=' '+self.error
        return text

    def reset(self):
        with self._io:
            with self._lock:
                if self._closed:raise RuntimeError('site detail is closed')
                self._pending={}
                self._reset_pending=True

    def close(self):
        with self._io:
            if self._closed:return
            if not self.flush():raise OSError(self.error)
            with self._lock:
                if self._pending:
                    raise OSError('网站明细存在并发待写数据，保留等待重试')
                if self.conn is not None:self.conn.close();self.conn=None
                self._closed=True
