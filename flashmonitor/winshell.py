"""和 Windows 外壳打交道：开机启动项、桌面 / 开始菜单快捷方式、单实例和唤起已有窗口。

开发实例（设置了 FLASHMONITOR_DATA_DIR）不改开机启动项和快捷方式。
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

from . import APP_ID, APP_NAME
from .paths import app_dir, entry_script, frozen, instance_tag, is_dev_instance, resource_dir
from .proc import no_window_flags, note_subprocess

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = APP_ID
LEGACY_RUN_NAMES = ("FenGuang",)
WM_FM_SHOW = 0x8001
ERROR_ALREADY_EXISTS = 183
ERROR_ACCESS_DENIED = 5
SYNCHRONIZE = 0x00100000


def mutex_name() -> str:
    return f"Local\\{APP_ID}.SingleInstance{instance_tag()}"


def window_class() -> str:
    return f"FlashMonitorWnd{instance_tag()}"


def tray_class() -> str:
    return f"{APP_ID}Tray{instance_tag()}"


def _python_launcher() -> str:
    """源码运行时用 pythonw.exe（没有控制台窗口）；找不到就用当前解释器。"""
    py = Path(sys.executable)
    helper = py.with_name("pythonw.exe")
    return str(helper if helper.is_file() else py)


def launch_command(*, autostart: bool, start_minimized: bool, open_browser: bool) -> str:
    """开机启动项里写的命令。"""
    flags = ["--gui"]
    if autostart or start_minimized:
        flags.append("--minimized")
    if autostart or not open_browser:
        flags.append("--no-browser")
    flag_s = " ".join(flags)
    if frozen():
        return f'"{sys.executable}" {flag_s}'
    return f'"{_python_launcher()}" "{entry_script()}" {flag_s}'


def autostart_enabled() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            for name in (RUN_VALUE_NAME, *LEGACY_RUN_NAMES):
                try:
                    val, _ = winreg.QueryValueEx(key, name)
                    if val:
                        return True
                except OSError:
                    continue
    except OSError:
        return False
    return False


def set_autostart(on: bool, command: str) -> None:
    if sys.platform != "win32":
        return
    if is_dev_instance():
        raise RuntimeError("开发实例（设置了 FLASHMONITOR_DATA_DIR）不改开机启动项")
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            for name in LEGACY_RUN_NAMES:
                try:
                    winreg.DeleteValue(key, name)
                except OSError:
                    pass
            if on:
                winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, command)
            else:
                try:
                    winreg.DeleteValue(key, RUN_VALUE_NAME)
                except OSError:
                    pass
    except OSError as exc:
        raise RuntimeError(f"无法写入开机启动项：{exc}") from exc


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _shell_folder(csidl: int) -> Path:
    buf = ctypes.create_unicode_buffer(260)
    hr = ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf)
    if hr:
        raise OSError(hr)
    return Path(buf.value)


def write_shortcut(lnk: Path, target: str, workdir: str, desc: str, *, args: str = "", icon: str = "") -> None:
    lnk.parent.mkdir(parents=True, exist_ok=True)
    icon_loc = icon or (target + ",0")
    cmd = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$sc = $ws.CreateShortcut({_ps_quote(str(lnk))}); "
        f"$sc.TargetPath = {_ps_quote(target)}; "
        f"$sc.Arguments = {_ps_quote(args)}; "
        f"$sc.WorkingDirectory = {_ps_quote(workdir)}; "
        f"$sc.Description = {_ps_quote(desc)}; "
        f"$sc.IconLocation = {_ps_quote(icon_loc)}; "
        "$sc.Save()"
    )
    note_subprocess()
    subprocess.run(
        ["powershell", "-NoProfile", "-STA", "-Command", cmd],
        check=False,
        capture_output=True,
        creationflags=no_window_flags(),
    )


def shortcut_launch() -> tuple[str, str, str]:
    """快捷方式的 (目标, 参数, 图标)。"""
    ico = resource_dir() / "icon.ico"
    icon = str(ico) if ico.is_file() else ""
    if frozen():
        exe = str(Path(sys.executable).resolve())
        return exe, "", icon or (exe + ",0")
    target = _python_launcher()
    args = f'"{entry_script()}" --gui --no-browser'
    return target, args, icon or (target + ",0")


def ensure_shortcuts() -> None:
    """桌面（打包版还有开始菜单）放一个快捷方式，顺便删掉旧版「分光」的。"""
    if sys.platform != "win32" or is_dev_instance():
        return
    target, args, icon = shortcut_launch()
    desc = f"{APP_NAME} — Clash 流量监视"
    try:
        folders = [_shell_folder(0x0010)]
        if frozen():
            folders.append(_shell_folder(0x0002))
    except OSError:
        return
    for folder in folders:
        try:
            (folder / "分光.lnk").unlink(missing_ok=True)
        except OSError:
            pass
        try:
            write_shortcut(folder / f"{APP_NAME}.lnk", target, str(app_dir()), desc, args=args, icon=icon)
        except OSError:
            pass


def activate_running_window() -> bool:
    """把已经在运行的实例的窗口唤到前台。"""
    if sys.platform != "win32":
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    hwnd = user32.FindWindowW(window_class(), None)
    if not hwnd and not is_dev_instance():
        hwnd = user32.FindWindowW(None, APP_NAME)
    if not hwnd:
        return False
    # SW_HIDE 的窗口不能靠 SW_RESTORE 唤起；还要发 WM_FM_SHOW 让 WebView 重新可见。
    user32.ShowWindow(hwnd, 1)
    user32.PostMessageW(hwnd, WM_FM_SHOW, 0, 0)
    user32.SetForegroundWindow(hwnd)
    return True


def claim_single_instance() -> tuple[bool, int]:
    """(是不是第一个实例, 互斥量句柄)。句柄要一直留着，进程退出时系统自动释放。"""
    if sys.platform != "win32":
        return True, 0
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel32.CreateMutexW(None, False, mutex_name())
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        activate_running_window()
        return False, int(handle or 0)
    return True, int(handle or 0)


def other_instance_running() -> bool:
    """有没有别的实例（同一个互斥量名）在运行。只查，不唤起它的窗口。"""
    if sys.platform != "win32":
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenMutexW(SYNCHRONIZE, False, mutex_name())
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return ctypes.get_last_error() == ERROR_ACCESS_DENIED  # 存在但打不开：也算在运行
