"""Application paths; no implicit cross-directory data migration."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

from . import APP_ID

DATA_DIR_ENV = "FLASHMONITOR_DATA_DIR"


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def project_root() -> Path:
    """源码运行时的项目根目录（monitor.py、ui.html 所在目录）。"""
    return Path(__file__).resolve().parent.parent


def resource_dir() -> Path:
    if frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return project_root()


def app_dir() -> Path:
    if frozen():
        return Path(sys.executable).resolve().parent
    return project_root()


def entry_script() -> Path:
    """源码运行时开机启动项和快捷方式指向的脚本。"""
    return project_root() / "monitor.py"


def dev_data_dir() -> Path | None:
    raw = (os.environ.get(DATA_DIR_ENV) or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def is_dev_instance() -> bool:
    """设置了 FLASHMONITOR_DATA_DIR：用独立数据目录，不碰开机启动项和快捷方式。"""
    return dev_data_dir() is not None


def instance_tag() -> str:
    """开发实例的互斥量 / 窗口类名后缀；正式实例为空串。"""
    dev = dev_data_dir()
    if dev is None:
        return ""
    digest = hashlib.sha1(str(dev).lower().encode("utf-8")).hexdigest()[:10]
    return f".dev-{digest}"


def _local_app_home() -> Path | None:
    local = os.environ.get("LOCALAPPDATA")
    if sys.platform == "win32" and local:
        return Path(local) / APP_ID
    return None


def data_home() -> Path:
    dev = dev_data_dir()
    if dev is not None:
        return dev
    home = _local_app_home()
    if home is not None:
        return home / "data"
    return app_dir() / "data"


def state_home() -> Path:
    """WebView2 配置、窗口位置这类本机状态；开发实例放在自己的数据目录里。"""
    dev = dev_data_dir()
    if dev is not None:
        return dev
    home = _local_app_home()
    if home is not None:
        return home
    return data_home()


def install_exe_path() -> Path:
    if frozen():
        return Path(sys.executable).resolve()
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / APP_ID / "FlashMonitor.exe"
    return app_dir() / "FlashMonitor.exe"


def migrate_legacy_data(dest: Path) -> None:
    """Compatibility hook: startup never imports another directory's data.

    A larger old log does not establish which settings or database the user
    intended to keep. Copying JSON beside SQLite can mix unrelated histories
    and overwrite current settings.

    Cross-directory migration is an explicit offline workflow in
    installer/Migrate-Data.ps1: Preview, confirmed Apply into an empty target,
    and hash-checked Rollback. Run it before starting the application.
    Store's existing in-place JSON-to-SQLite conversion remains unchanged.

    Keep the callable for startup compatibility, but do no filesystem reads
    or writes, including mkdir. Data owners create their own directories.
    """
    return None
