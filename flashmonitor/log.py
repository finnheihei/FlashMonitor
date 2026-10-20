"""app.log：启动记录和限流的异常日志。configure() 之前所有调用都是空操作。"""

from __future__ import annotations

import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

MAX_BYTES = 512_000
RATE_WINDOW = 60.0

_LOCK = threading.Lock()
_DIR: Path | None = None
_LAST: dict[str, float] = {}
_SUPPRESSED: dict[str, int] = {}
_clock = time.monotonic


def configure(log_dir: Path | None) -> None:
    global _DIR
    _DIR = log_dir


def log_path() -> Path | None:
    return _DIR / "app.log" if _DIR is not None else None


def app_log(msg: str) -> None:
    path = log_path()
    if path is None:
        return
    line = datetime.now().strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n"
    with _LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
            if path.stat().st_size > MAX_BYTES:
                bak = path.with_suffix(".log.1")
                bak.unlink(missing_ok=True)
                path.replace(bak)
        except OSError:
            pass


def log_exception(where: str, exc: BaseException | None = None) -> bool:
    """记下完整调用栈。同一位置、同一类异常 60 秒内只记一次，返回这次是否写了日志。"""
    if exc is None:
        exc = sys.exc_info()[1]
    if exc is None:
        return False
    key = f"{where}|{type(exc).__name__}"
    now = _clock()
    with _LOCK:
        last = _LAST.get(key)
        if last is not None and now - last < RATE_WINDOW:
            _SUPPRESSED[key] = _SUPPRESSED.get(key, 0) + 1
            return False
        _LAST[key] = now
        skipped = _SUPPRESSED.pop(key, 0)
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip()
    extra = f"（此前 {int(RATE_WINDOW)} 秒内同类异常另有 {skipped} 次未记）" if skipped else ""
    app_log(f"[{where}] {type(exc).__name__}: {exc}{extra}\n{tb}")
    return True


def install_excepthooks() -> None:
    """主线程和后台线程里没接住的异常都写进 app.log（打包版没有控制台）。"""
    prev_sys = sys.excepthook
    prev_thread = threading.excepthook

    def sys_hook(tp, value, tb):  # type: ignore[no-untyped-def]
        if not issubclass(tp, KeyboardInterrupt):
            log_exception("未捕获异常", value)
        prev_sys(tp, value, tb)

    def thread_hook(args):  # type: ignore[no-untyped-def]
        if args.exc_type is not SystemExit:
            name = args.thread.name if args.thread is not None else "?"
            log_exception(f"线程 {name}", args.exc_value)
        prev_thread(args)

    sys.excepthook = sys_hook
    threading.excepthook = thread_hook
