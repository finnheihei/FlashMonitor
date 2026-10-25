"""在线更新：查 GitHub 最新发行版 → 下载 FlashMonitor.exe 并校验 → 本进程退出后替换、重启。

- 下载分块写临时文件，同时算 SHA256；大小或 SHA256 和发行版对不上就删掉临时文件、报错，不动原程序。
  GitHub 给每个发行版文件都算了 digest（"sha256:…"）；没有的老发行版只检查大小，并写日志。
- 替换靠一个小的 .cmd：等旧进程退出，move 新文件，再启动。路径都放在环境变量里传给它，
  脚本本身只有 ASCII：路径里有空格、&、括号、中文都不怕（0.8.0 把路径直接写进脚本并用 ASCII 保存，
  中文用户名的电脑上会直接写不出来）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.request import OpenerDirector, Request, build_opener

from . import APP_ID
from . import log as fmlog
from .parsing import as_int, parse_version
from .proc import no_window_flags, note_subprocess

ASSET_NAME = "flashmonitor.exe"
MIN_EXE_BYTES = 1_000_000
MAX_EXE_BYTES = 400 * 1024 * 1024
CHUNK = 256 * 1024
CACHE_TTL = 90.0
SCRIPT_NAME = "_fm_update.cmd"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

DEFENDER_HINT = (
    "Windows Defender 常把未签名的打包版误报成 Trojan:Win32/Bearfoos 并直接删除。"
    "到「Windows 安全中心 → 保护历史记录」找到 FlashMonitor.exe，选允许；"
    "或给 %LOCALAPPDATA%\\FlashMonitor 加排除项后再装。"
)

# Wait/replace failures retain the script, staged file and a diagnostic; never start the old executable.
UPDATE_SCRIPT = "\r\n".join(
    [
        "@echo off",
        "setlocal DisableDelayedExpansion",
        'set "WAIT_PID=%FM_WAIT_PID%"',
        'set "SRC=%FM_SRC%"',
        'set "DST=%FM_DST%"',
        'set "WORK=%FM_WORK%"',
        'set "DIAG=%DST%.update-error.txt"',
        # Wait on the process handle; tasklist's localized text is not a reliable liveness test.
        'powershell.exe -NoLogo -NoProfile -NonInteractive -Command "$ErrorActionPreference=\'Stop\'; try { $p=[Diagnostics.Process]::GetProcessById([int]$env:FM_WAIT_PID) } catch [ArgumentException] { exit 0 } catch { exit 2 }; try { if ($p.WaitForExit(120000)) { exit 0 }; exit 1 } catch { exit 2 } finally { $p.Dispose() }" >nul 2>nul',
        "if errorlevel 2 goto probe_failed",
        "if errorlevel 1 goto wait_failed",
        ":gone",
        "set /a MOVES=0",
        ":move",
        'powershell.exe -NoLogo -NoProfile -NonInteractive -Command "Start-Sleep -Seconds 1" >nul 2>nul',
        'move /y "%SRC%" "%DST%" >nul 2>&1',
        "if not errorlevel 1 goto start",
        "set /a MOVES+=1",
        "if %MOVES% lss 15 goto move",
        "goto move_failed",
        ":start",
        'if exist "%DIAG%" del "%DIAG%" >nul 2>&1',
        "if defined FM_NO_START goto cleanup",
        'if exist "%DST%" start "" /d "%WORK%" "%DST%" --gui',
        ":cleanup",
        '(goto) 2>nul & del "%~f0"',
        ":wait_failed",
        '> "%DIAG%" echo Update stopped: the previous process did not exit within 120 seconds. No executable was replaced or started.',
        "exit /b 1",
        ":probe_failed",
        '> "%DIAG%" echo Update stopped: the previous process could not be checked. No executable was replaced or started.',
        "exit /b 1",
        ":move_failed",
        '> "%DIAG%" echo Update stopped: executable replacement failed after 15 attempts. The staged file and update script were retained. No executable was started.',
        "exit /b 1",
        "",
    ]
)


class UpdateError(RuntimeError):
    pass


def _asset_sha256(asset: Mapping[str, Any]) -> str:
    digest = str(asset.get("digest") or "").strip().lower()
    if digest.startswith("sha256:") and _SHA256_RE.match(digest[7:]):
        return digest[7:]
    return ""


def parse_release(data: Any, *, current: str, repo: str) -> dict[str, Any]:
    """GitHub /releases/latest 的返回 → 页面和下载要用的字段。"""
    if not isinstance(data, dict):
        raise UpdateError("GitHub 返回无法识别")
    tag = str(data.get("tag_name") or "")
    asset: Mapping[str, Any] = {}
    for item in data.get("assets") or []:
        if isinstance(item, dict) and str(item.get("name") or "").lower() == ASSET_NAME:
            asset = item
            break
    return {
        "ok": True,
        "current": current,
        "tag": tag,
        "latest": tag.lstrip("vV") or tag,
        "name": str(data.get("name") or tag),
        "notes": str(data.get("body") or "")[:1200],
        "exe_url": str(asset.get("browser_download_url") or ""),
        "exe_size": max(0, as_int(asset.get("size"))),
        "exe_sha256": _asset_sha256(asset),
        "html_url": str(data.get("html_url") or f"https://github.com/{repo}/releases/latest"),
        "newer": parse_version(tag) > parse_version(current),
    }


def fetch_latest(repo: str, *, current: str, opener: OpenerDirector | None = None, timeout: float = 12.0) -> dict[str, Any]:
    req = Request(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": APP_ID},
    )
    with (opener or build_opener()).open(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    return parse_release(data, current=current, repo=repo)


def download_verified(
    url: str,
    dest: Path,
    *,
    expected_size: int = 0,
    expected_sha256: str = "",
    opener: OpenerDirector | None = None,
    timeout: float = 60.0,
    max_bytes: int = MAX_EXE_BYTES,
    min_bytes: int = MIN_EXE_BYTES,
) -> str:
    """下载到 dest：先写 dest.download，大小和 SHA256 都对才改名成 dest。返回实际的 SHA256。

    任何一步出错都删掉临时文件，dest 原来是什么还是什么。
    """
    dest = Path(dest)
    tmp = dest.with_name(dest.name + ".download")
    digest = hashlib.sha256()
    got = 0
    req = Request(url, headers={"User-Agent": APP_ID, "Accept": "application/octet-stream"})
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with (opener or build_opener()).open(req, timeout=timeout) as resp, tmp.open("wb") as fh:
            while True:
                block = resp.read(CHUNK)
                if not block:
                    break
                got += len(block)
                if got > max_bytes:
                    raise UpdateError("下载的文件大得不正常，已放弃")
                digest.update(block)
                fh.write(block)
        if got < min_bytes:
            raise UpdateError("下载文件太小，发行版可能还没打好包")
        if expected_size and got != expected_size:
            raise UpdateError(f"下载不完整（{got} / {expected_size} 字节），请重试")
        actual = digest.hexdigest()
        if expected_sha256 and not hmac.compare_digest(actual, expected_sha256.lower()):
            raise UpdateError("下载的文件和发行版的 SHA256 对不上，已放弃（可能下载出错或被篡改）")
        tmp.replace(dest)
        return actual
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def file_survived(path: Path, wait: float = 1.6, *, min_bytes: int = MIN_EXE_BYTES) -> bool:
    """等一会儿再看文件还在不在（Defender 常在写完后马上把未签名的 exe 删掉）。"""
    time.sleep(wait)
    try:
        return path.is_file() and path.stat().st_size >= min_bytes
    except OSError:
        return False


def write_update_script(path: Path) -> Path:
    path.write_text(UPDATE_SCRIPT, encoding="ascii", newline="")
    return path


def launch_update_script(
    script: Path,
    *,
    wait_pid: int,
    src: Path,
    dst: Path,
    work: Path,
    extra_env: Mapping[str, str] | None = None,
) -> subprocess.Popen:
    """在后台起替换脚本。路径走环境变量，命令行里只有脚本自己的路径（/s 加外层引号，& 之类也安全）。"""
    env = dict(os.environ)
    env.update(
        FM_WAIT_PID=str(int(wait_pid)),
        FM_SRC=str(src),
        FM_DST=str(dst),
        FM_WORK=str(work),
    )
    env.update(extra_env or {})
    comspec = os.environ.get("ComSpec") or "cmd.exe"
    note_subprocess()
    return subprocess.Popen(
        f'"{comspec}" /d /s /c ""{script}""',
        cwd=str(work),
        env=env,
        creationflags=no_window_flags(),
        close_fds=True,
    )


class Updater:
    """查询结果缓存 90 秒；同一时间只做一次更新。"""

    def __init__(
        self,
        *,
        repo: str,
        current: str,
        install_path: Callable[[], Path],
        refuse: Callable[[], str] | None = None,
        prepare_restart: Callable[[], str] | None = None,
        cancel_restart: Callable[[], str] | None = None,
        on_restart: Callable[[], None] | None = None,
        opener: OpenerDirector | None = None,
        survive_wait: float = 1.6,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.repo = repo
        self.current = current
        self._install_path = install_path
        self._refuse = refuse or (lambda: "")
        self._prepare_restart = prepare_restart or (lambda: "")
        self._cancel_restart = cancel_restart or (lambda: "")
        self._on_restart = on_restart or (lambda: None)
        self._opener = opener
        self._survive_wait = survive_wait
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: tuple[float, dict[str, Any]] | None = None

    def latest(self, *, force: bool = False) -> dict[str, Any]:
        now = self._clock()
        hit = self._cache
        if not force and hit is not None and now - hit[0] < CACHE_TTL:
            return dict(hit[1])
        info = fetch_latest(self.repo, current=self.current, opener=self._opener)
        self._cache = (now, info)
        return dict(info)

    def apply(self) -> dict[str, Any]:
        reason = self._refuse()
        if reason:
            return {"ok": False, "error": reason}
        if not self._lock.acquire(blocking=False):
            return {"ok": False, "error": "已经在更新"}
        try:
            info = self.latest(force=True)
            if not info.get("newer"):
                return {"ok": False, "error": "当前已是最新版本，无需安装更新"}
            url = str(info.get("exe_url") or "")
            if not url:
                return {"ok": False, "error": "最新发行版里还没有 FlashMonitor.exe，过一会儿再试"}
            if not info.get("exe_sha256"):
                fmlog.app_log(f"更新 {info.get('tag')}：发行版没有 SHA256 摘要，只检查文件大小")
            dest = self._install_path()
            staging = dest.with_name(dest.name + ".new")
            try:
                sha = download_verified(
                    url,
                    staging,
                    expected_size=int(info.get("exe_size") or 0),
                    expected_sha256=str(info.get("exe_sha256") or ""),
                    opener=self._opener,
                )
            except UpdateError as exc:
                fmlog.app_log(f"更新 {info.get('tag')} 下载被拒：{exc}")
                return {"ok": False, "error": str(exc)}
            if not file_survived(staging, self._survive_wait):
                return {"ok": False, "error": "新版本下载后被删了。" + DEFENDER_HINT}
            script = write_update_script(dest.with_name(SCRIPT_NAME))
            try:
                preparation_error = self._prepare_restart()
            except Exception as exc:
                recovery_error = self._cancel_restart()
                return {"ok": False, "error": "更新前保存失败：" + str(exc) +
                        ("；" + recovery_error if recovery_error else "；采集已恢复，可重试")}
            if preparation_error:
                return {"ok": False, "error": preparation_error}
            helper = None
            try:
                helper = launch_update_script(script, wait_pid=os.getpid(), src=staging, dst=dest, work=dest.parent)
                # This short callback commits the prepared shutdown and schedules exit.
                # Run it on the preparing thread so it can release its sample lock.
                self._on_restart()
            except Exception as exc:
                if helper is not None:
                    try:
                        helper.terminate()
                    except Exception:
                        pass
                recovery_error = self._cancel_restart()
                return {"ok": False, "error": "未能启动更新重启：" + str(exc) +
                        ("；" + recovery_error if recovery_error else "；采集已恢复，可重试")}
            fmlog.app_log(f"更新到 {info.get('tag')}：已下载并校验（sha256 {sha[:16]}…），退出后替换")
            return {"ok": True, "restarting": True, "latest": info.get("latest") or "", "path": str(dest)}
        finally:
            self._lock.release()
