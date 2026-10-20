"""Bounded graceful shutdown: stop producers, wait, save, then release resources."""
from __future__ import annotations
import threading
import time


class Shutdown:
    def __init__(self, stop, threads, store, release=lambda: None):
        self.stop, self.threads, self.store, self.release = stop, threads, store, release
        self._lock = threading.Lock()
        self._writer = None
        self._done = threading.Event()
        self._write_error = ''
        self.complete = False
        self.error = ''

    def _save(self):
        try:
            if self.store.close() is False:
                raise OSError('存储拒绝关闭，最终保存未确认')
        except Exception as exc:
            self._write_error = str(exc) or type(exc).__name__
        finally:
            self._done.set()

    def finish(self, timeout=8.0):
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if self.complete:
                return True
            self.stop.set()
            deadline = time.monotonic() + max(0, timeout)
            for thread in list(self.threads()):
                if thread is not threading.current_thread():
                    thread.join(max(0, deadline-time.monotonic()))
            pending = [t.name for t in self.threads() if t.is_alive()]
            if pending:
                self.error = '退出未完成：已有采集任务尚未结束（'+', '.join(pending)+'）。统计保留，稍后重试退出。'
                return False
            if self._writer is None or self._done.is_set() and self._write_error:
                self._done.clear(); self._write_error = ''
                self._writer = threading.Thread(target=self._save, name='final-store-save', daemon=True)
                self._writer.start()
            self._done.wait(max(0, deadline-time.monotonic()))
            if not self._done.is_set():
                self.error = '退出未完成：最终写盘仍在进行。进程与待写统计保留，请稍后重试。'
                return False
            if self._write_error:
                self.error = '退出未完成：采集已暂停，最终写盘失败，待写统计仍在内存。请检查数据目录/磁盘后重试：'+self._write_error
                return False
            try:
                self.release()
            except Exception as exc:
                # Stats are already durable; cleanup failure cannot invalidate successful saving.
                self.error = '统计已保存，辅助资源清理未完整完成：'+str(exc)
            else:
                self.error = ''
            self.complete = True
            return True
        except Exception as exc:
            self.error = '退出未完成：停止/保存流程出错，进程和统计保留等待重试：'+str(exc)
            return False
        finally:
            self._lock.release()
