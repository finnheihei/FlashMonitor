"""程序入口：main() 创建所有对象（AppContext），起后台线程、HTTP 服务、窗口和托盘。

导入这个模块不做任何事；读写文件、注册表、网络都从 main() 开始。
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
import webbrowser
from pathlib import Path
from pathlib import PureWindowsPath
from typing import Any

from . import APP_NAME, APP_VERSION, GITHUB_REPO
from . import log as fmlog
from . import winshell
from .clash import ClashClient
from .collector import Collector
from .db import DB_NAME
from .desktop import DesktopShell, wait_http
from .icons import IconCache, file_description, local_executable
from .netinfo import FileMemo
from .parsing import load_listen_ports, load_proxy_ports
from .paths import data_home, frozen, install_exe_path, is_dev_instance, migrate_legacy_data, resource_dir, state_home
from .proc import process_stats
from .server import ApiServer, bind_server
from .rule_drafts import RuleDrafts, DraftError, identity, snippet, token
from .ipv4_drafts import IPv4Drafts, normalize_subject, subject_id
from .ipv6_plans import IPv6Plans, build_plan, digest, inspect_flclash
from .ipv6_executor import IPv6Executor, ControllerReload
from .ipv6_native import NativeVerifier
from .ipv6_flow import IPv6Flow
from .scoped_ipv6 import RuntimeController, ScopedIPv6
from .network_inventory import NetworkPolicyService
from .process_identity import query_process
from .shutdown import Shutdown
from .settings import AppSettings
from .store import PERIODS, TrafficStore
from .subinfo import SubInfo
from .tray import TrayPulse, WinTray
from .updater import Updater
from .views import LiveState, RankCache, build_all, build_view

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8787
GITHUB_URL = f"https://github.com/{GITHUB_REPO}"


def flclash_dir() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "com.follow" / "clash"


def load_page() -> str:
    return (resource_dir() / "ui.html").read_text(encoding="utf-8")


def _open_folder(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)  # noqa: S606 - 固定的本机目录
        return True
    except OSError:
        return False


class AppContext:
    """一次运行里的所有对象。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.rule_drafts = RuleDrafts(self.data_dir / "rule-drafts.json")
        self.ipv4_drafts = IPv4Drafts(self.data_dir / "ipv4-drafts.json")
        self.ipv6_plans = IPv6Plans(self.data_dir / "ipv6-plans.json")
        self.port = LISTEN_PORT
        self.stop = threading.Event()
        self.httpd: ApiServer | None = None
        self.shell: Any = None
        self.mutex = 0  # 单实例互斥量句柄：进程活着就一直留着
        self.settings = AppSettings(
            self.data_dir / "settings.json",
            autostart_enabled=winshell.autostart_enabled,
            set_autostart=self.set_autostart,
            on_saved=self._after_settings_saved,
        )
        self.icons = IconCache(self.data_dir / "icons")
        self.store = TrafficStore(
            self.data_dir,
            retention_days=self.retention_days,
            ignored=self.settings.ignored_set,
            on_icon=self.icons.schedule,
            describe=file_description,
            program_sites_enabled=True,
        )
        self.clash = ClashClient(self._setting_text("clash_api"), self._setting_text("clash_secret"))
        self.ipv6_executor = IPv6Executor(flclash_dir(),self.data_dir/'ipv6-backups',ControllerReload(self.clash),self._ipv6_consent)
        self.ipv6_native = NativeVerifier(flclash_dir(),self.data_dir/'ipv6-native',ControllerReload(self.clash))
        self.ipv6_flow = IPv6Flow(self.ipv6_plans,self.ipv6_native,self.ipv6_native.controller,self._ipv6_subject_available,self.ipv6_executor)
        self.scoped_ipv6 = ScopedIPv6(flclash_dir(), self.data_dir / 'scoped-ipv6.json',
                                     RuntimeController(self.clash), self._ipv6_subject_available)
        self.subinfo = SubInfo(
            self.data_dir / "subinfo.json",
            sub_url=self._setting_text("sub_url"),
            flclash_dir=flclash_dir(),
        )
        self.live = LiveState()
        self.ranks = RankCache(self.store.ranking)
        clash_config = flclash_dir() / "config.yaml"
        self.collector = Collector(
            store=self.store,
            clash=self.clash,
            ranks=self.ranks,
            live=self.live,
            settings=lambda: self.settings.data,
            ignored=self.settings.ignored_set,
            proxy_ports=load_proxy_ports(clash_config),
            listen_ports=FileMemo(clash_config, load_listen_ports),
            on_poll=self.subinfo.schedule,
            icon_version=self.icons.version,
        )
        self.shutdown = Shutdown(self.stop, lambda: self.collector.threads, self.store, self.icons.close)
        self._shutdown_in_progress = False
        self._lifecycle_lock = threading.RLock()
        self._update_sample_locked = False
        self.updater = Updater(
            repo=GITHUB_REPO,
            current=APP_VERSION,
            install_path=install_exe_path,
            refuse=self._update_refused,
            prepare_restart=self.prepare_update_restart,
            cancel_restart=self.cancel_update_restart,
            on_restart=self.shutdown_after_update,
        )

    # ---------- 设置相关 ----------

    def _setting_text(self, key: str):  # noqa: ANN202 - 返回一个取值函数
        return lambda: str(self.settings.data.get(key) or "")

    def retention_days(self) -> int:
        try:
            return int(self.settings.data.get("retention_days") or 40)
        except (TypeError, ValueError):
            return 40

    def launch_command(self, *, autostart: bool = False) -> str:
        data = self.settings.data
        return winshell.launch_command(
            autostart=autostart,
            start_minimized=bool(data.get("start_minimized")),
            open_browser=bool(data.get("open_browser", False)),
        )

    def set_autostart(self, on: bool) -> None:
        winshell.set_autostart(on, self.launch_command(autostart=True))

    def _after_settings_saved(self, keys: set[str]) -> None:
        if "sub_url" in keys:
            self.subinfo.schedule(force=True)

    def _ipv6_consent(self):
        """Exact scope file supplied only after explicit approval; no HTTP grant endpoint."""
        path=self.data_dir/'ipv6-consent.json'
        try:
            if path.stat().st_size>16384:return None
            return json.loads(path.read_text(encoding='utf-8'))
        except (OSError,ValueError):return None

    def _ipv6_subject_available(self,subject):
        state=self.live.get() or {}
        fresh=not state.get('error') and time.time()-float(state.get('ts') or 0)<=15
        if subject['kind']=='process_path':
            try:
                if Path(subject['value']).is_file():return True
            except OSError:pass
        if fresh:
            field='proc_path' if subject['kind']=='process_path' else 'rule_exe'
            return any(not f.get('identity_conflict') and str(f.get(field,'')).casefold()==subject['value'].casefold() for f in state.get('flows') or [])
        return False

    def page_url(self) -> str:
        return f"http://{LISTEN_HOST}:{self.port}/"

    def settings_public(self) -> dict[str, Any]:
        """设置页要的全部内容：设置本身（不含密钥原文）+ 程序信息。"""
        return {
            **self.settings.public(),
            "data_dir": str(self.data_dir),
            "page": self.page_url(),
            "launch": self.launch_command(autostart=True),
            "version": APP_VERSION,
            "frozen": frozen(),
            "install_dir": str(install_exe_path().parent),
            "github": GITHUB_URL,
            "license": "MIT",
        }

    def product_diag(self) -> dict[str, Any]:
        status = self.live.status()
        return {
            "app": APP_NAME,
            "version": APP_VERSION,
            "frozen": frozen(),
            "page": self.page_url(),
            "data_dir": str(self.data_dir),
            "install_dir": str(install_exe_path().parent),
            "clash_api": self.settings.data.get("clash_api"),
            "clash_ok": bool(status.get("clash_api")),
            "mode": status.get("mode") or "off",
            "github": GITHUB_URL,
            "platform": sys.platform,
            "python": sys.version.split()[0],
            "dev_instance": is_dev_instance(),
            **process_stats(),
            **self.icons.stats(),
            **(self.httpd.stats() if self.httpd is not None else {}),
        }

    # ---------- 更新 / 退出 ----------

    @staticmethod
    def _update_refused() -> str:
        if is_dev_instance():
            return "开发实例（设置了 FLASHMONITOR_DATA_DIR）不做在线更新"
        if not frozen():
            return "源码运行不支持在线安装更新，请在 GitHub 获取源码或安装包"
        return ""

    def close_store(self, timeout: float = 8.0) -> bool:
        """Refuse exit on pending producers/save failure; keep memory and DB available for retry."""
        with self._lifecycle_lock:
            ok = self.shutdown.finish(timeout)
        if not ok:
            fmlog.app_log(self.shutdown.error or '退出正在处理中')
        return ok

    def prepare_update_restart(self) -> str:
        """Freeze producers and confirm final persistence without stopping recoverable workers."""
        if not self._lifecycle_lock.acquire(blocking=False):
            return "正在退出或准备更新，请稍后重试"
        if self.stop.is_set() or self._shutdown_in_progress:
            self._lifecycle_lock.release()
            return "正在退出或准备更新，请稍后重试"
        self._shutdown_in_progress = True
        if not self.collector._sample_lock.acquire(timeout=8.0):
            self._shutdown_in_progress = False
            self._lifecycle_lock.release()
            return "采集任务仍在进行，未启动更新；请稍后重试"
        self._update_sample_locked = True
        try:
            if self.store.close() is False:
                raise OSError("最终保存未确认")
        except Exception as exc:
            recovery_error = self.cancel_update_restart()
            return "更新前保存失败：" + str(exc) + ("；" + recovery_error if recovery_error else "；采集已恢复，可重试")
        return ""

    def cancel_update_restart(self) -> str:
        """Reopen a successfully closed store after launch failure, keeping live accounting baselines."""
        if not self._update_sample_locked:
            return ""
        error = ""
        try:
            # Keep the TrafficStore object: collector, ranks and tray retain references to it.
            with self.store._flush_lock, self.store.db.lock:
                if self.store.db.conn is None:
                    # Preserve the DB object/lock: a reader may already be waiting on it.
                    self.store.db.conn = self.store.db._connect(str(self.store.db.path))
                details = self.store.site_details
                if details is not None and details._closed:
                    with details._io, details._lock:
                        reopened = type(details)(details.path, policy=details.policy, clock=details.clock)
                        details.conn = reopened.conn
                        details.error, details.partial = reopened.error, reopened.partial
                        details.since, details.dropped = reopened.since, reopened.dropped
                        details._closed = False
                        if details.conn is None:
                            error = "程序累计采集已恢复，但网站明细暂不可用：" + details.error
                with self.store.lock:
                    self.store._closed = False
            self.ranks.invalidate()
        except Exception as exc:
            error = "采集尚未恢复，请检查数据目录后重启：" + str(exc)
            fmlog.log_exception("更新取消后恢复采集", exc)
        finally:
            self._shutdown_in_progress = False
            self._update_sample_locked = False
            self.collector._sample_lock.release()
            self._lifecycle_lock.release()
        return error

    def shutdown_after_update(self) -> None:
        """Commit only after save and helper launch; the calling thread owns the sample lock."""
        if not self._update_sample_locked or not self.store._closed:
            raise RuntimeError("尚未确认更新前保存，未退出")
        ready = threading.Event()
        worker = threading.Thread(target=lambda: (ready.wait(), self._exit_after_update()), daemon=True, name="update-restart")
        worker.start()
        self.stop.set()
        self.shutdown.complete = True
        self._update_sample_locked = False
        self.collector._sample_lock.release()
        self._lifecycle_lock.release()
        ready.set()

    def _exit_after_update(self) -> None:
        time.sleep(0.5)  # Allow the installing request to return its confirmed result.
        try:
            self.icons.close()
        except Exception as exc:
            fmlog.log_exception("更新退出图标清理", exc)
        if self.shell is not None:
            try:
                self.shell.close()
            except Exception:  # noqa: BLE001
                pass
        self.stop.set()
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.2)
        os._exit(0)

    # ---------- 运行 ----------

    def start_background(self) -> None:
        self.subinfo.schedule(force=True)
        self.collector.start_threads(self.stop)
        self.scoped_ipv6.start(self.stop)


class MonitorApi:
    """server.py 要的操作。"""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._refresh_lock = threading.Lock()

    def page_html(self) -> str:
        return load_page()

    def snapshot(self, view: str, period: str) -> dict[str, Any]:
        c = self.ctx
        lifecycle = getattr(c, '_lifecycle_lock', None)
        busy = {'error': '正在退出或准备更新，请稍后重试', 'busy': True}
        if lifecycle is not None and not lifecycle.acquire(blocking=False):
            return busy
        try:
            shutdown = getattr(c, 'shutdown', None)
            if (getattr(c, '_update_sample_locked', False)
                    or getattr(c, '_shutdown_in_progress', False)
                    or shutdown is not None and shutdown.complete):
                return busy
            result = build_view(
                view,
                period,
                state=c.live.get(),
                store=c.store,
                ranks=c.ranks,
                prefs=c.settings.ui_prefs(),
                sub=c.subinfo.public(),
            )
            if shutdown is not None and shutdown.error:
                result['error'] = shutdown.error
            result['manual_refresh_supported'] = True
            return result
        finally:
            if lifecycle is not None:
                lifecycle.release()

    def refresh_data(self, body: dict[str, Any]) -> dict[str, Any]:
        if set(body) != {'period'} or body.get('period') not in PERIODS:
            raise DraftError('刷新参数无效')
        if not self._refresh_lock.acquire(blocking=False):
            return {'ok': False, 'busy': True, 'error': '正在刷新，请稍后'}
        lifecycle = getattr(self.ctx, '_lifecycle_lock', None)
        lifecycle_acquired = False
        try:
            if lifecycle is not None:
                lifecycle_acquired = lifecycle.acquire(blocking=False)
                if not lifecycle_acquired:
                    return {'ok': False, 'busy': True, 'error': '正在退出或准备更新，请稍后重试'}
            c = self.ctx
            if getattr(c, '_shutdown_in_progress', False) or getattr(c, 'stop', None) is not None and c.stop.is_set():
                return {'ok': False, 'error': 'Monitor 正在退出，未刷新'}
            state, inventory = c.collector.refresh()
            catalog = NetworkPolicyService(lambda: inventory, lambda: state).catalog()
            catalog['inventory']['nodes'] = sorted(n for n in inventory.get('targets', {})
                if n not in inventory.get('groups', {}) and n not in {'GLOBAL', 'DIRECT', 'REJECT', 'REJECT-DROP', 'PASS'})
            result = build_view('overview', 'today', state=state, store=c.store, ranks=c.ranks,
                prefs=c.settings.ui_prefs(), sub=c.subinfo.public())
            result['rank'].update({p: c.ranks.get(p) for p in dict.fromkeys(('all', body['period']))})
            result['closed'] = c.store.recent_closed()
            result['manual_refresh_supported'] = True
            return {'ok': True, 'snapshot': result, 'catalog': catalog, 'refreshed_at': time.time()}
        except Exception as exc:
            fmlog.log_exception('手动刷新', exc)
            return {'ok': False, 'error': '读取连接、用量或节点失败，已保留原页面数据。'}
        finally:
            if lifecycle_acquired:
                lifecycle.release()
            self._refresh_lock.release()

    def settings(self) -> dict[str, Any]:
        return self.ctx.settings_public()

    def apply_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        saved = self.ctx.settings.apply(patch)
        result = self.ctx.settings_public()
        if "connection_draft" in patch:
            result.update(connection_draft_saved=saved is True, network_applied=False)
        return result

    def network_catalog(self) -> dict[str, Any]:
        return NetworkPolicyService(self.ctx.clash.network_inventory, self.ctx.live.get).catalog()

    def network_preview(self, body: dict[str, Any]) -> dict[str, Any]:
        return NetworkPolicyService(self.ctx.clash.network_inventory, self.ctx.live.get).preview(body)

    def scoped_ipv6_catalog(self) -> dict[str, Any]:
        result = self.ctx.scoped_ipv6.catalog()
        live = getattr(self.ctx, 'live', None)
        state = (live.get() if live else {}) or {}
        fresh = not state.get('error') and 0 <= time.time() - float(state.get('ts') or 0) <= 15
        applications = {}
        for flow in state.get('flows') or [] if fresh else []:
            if not flow.get('proc_path') or flow.get('identity_conflict'):
                continue
            try:
                subject = normalize_subject({'kind': 'process_path', 'value': flow['proc_path'], 'include_subdomains': False})
            except DraftError:
                continue
            path = subject['value']
            available = flow.get('identity_verified') is True
            if not available:
                check = getattr(self.ctx, '_ipv6_subject_available', None)
                available = bool(check and check(subject))
            if not available:
                continue
            applications[path.casefold()] = {'path': path, 'label': str(flow.get('title') or flow.get('process') or PureWindowsPath(path).name),
                                             'process': str(flow.get('process') or '')}
        result['applications'] = list(applications.values())
        return result

    def scoped_ipv6_action(self, body: dict[str, Any]) -> dict[str, Any]:
        if self.ctx.stop.is_set() or getattr(self.ctx, '_shutdown_in_progress', False):
            raise DraftError('Monitor 正在退出，请重新打开后操作')
        return self.ctx.scoped_ipv6.operate(body)

    def rule_catalog(self) -> dict[str, Any]:
        return self.ctx.rule_drafts.catalog()

    def ipv4_context(self) -> dict[str, Any]:
        state = self.ctx.live.get() or {}
        fresh = not state.get("error") and time.time() - float(state.get("ts") or 0) <= 15
        applications, domains = {}, set()
        for flow in state.get("flows") or [] if fresh else []:
            if flow.get("identity_conflict"):
                continue
            path, exe = flow.get("proc_path"), flow.get("rule_exe")
            kind, value = ("process_path", path) if path else ("process_name", exe)
            if value:
                try:
                    subject = normalize_subject({"kind": kind, "value": value, "include_subdomains": False})
                    applications[subject_id(subject)] = {"subject": subject,
                        "process": str(flow.get("process") or ""), "title": str(flow.get("title") or "")}
                except DraftError:
                    pass
            if flow.get("host"):
                try:
                    domains.add(normalize_subject({"kind": "domain", "value": flow["host"],
                                                   "include_subdomains": False})["value"])
                except DraftError:
                    pass
        return {**self.ctx.ipv4_drafts.catalog(), "applications": list(applications.values()),
                "domains": sorted(domains), "identity_error": "" if fresh else "连接数据过期；应用选择暂不可用"}

    def ipv4_draft(self, body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise DraftError("草稿请求无效")
        if body.get("operation") in {"preview", "save"}:
            subject = normalize_subject(body.get("subject"))
            if subject["kind"] != "domain":
                context = self.ipv4_context()
                key = subject_id(subject)
                known = {subject_id(a["subject"]) for a in context["applications"]}
                saved = {d["id"] for d in context["drafts"]}
                if key not in known and key not in saved:
                    raise DraftError(context["identity_error"] or "应用身份已变化，请刷新后重新选择")
            body = {**body, "subject": subject}
        return self.ctx.ipv4_drafts.operate(body)

    def ipv6_catalog(self) -> dict[str, Any]:
        result=self.ctx.ipv6_plans.catalog();executor=getattr(self.ctx,'ipv6_executor',None)
        native=getattr(self.ctx,'ipv6_native',None)
        result['native_supported']=bool(native)
        result['capabilities']={'version':2,'assisted_readonly':bool(native),'gui_verified':False,'client_bridge_verified':False,
                                'flow_supported':bool(getattr(self.ctx,'ipv6_flow',None))}
        if native:
            for p in result['plans']:p['native']=native.status(p)
        if executor:
            for p in result['plans']:
                p['execution_authorized']=executor.eligible(p)
                p['execution']=executor.status(p['id'])
        return result

    def ipv6_plan(self, body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body,dict):raise DraftError('方案请求无效')
        if body.get('operation') in {'resolve','begin'}:
            flow=getattr(self.ctx,'ipv6_flow',None)
            if not flow:raise DraftError('半自动流程接口未加载')
            return getattr(flow,body['operation'])(body)
        if body.get('operation') in {'native_prepare','native_verify','native_undo','native_export'}:
            native=getattr(self.ctx,'ipv6_native',None);plans=self.ctx.ipv6_plans.read();key=body.get('id')
            if not native or not isinstance(key,str) or key not in plans['plans']:raise DraftError('请先保存本地方案')
            if type(body.get('revision')) is not int or body['revision']!=plans['revision']:raise DraftError('方案已变化，请刷新')
            plan=plans['plans'][key]
            if body['operation']=='native_export':
                return {'text':'# IPv6限制规则；不是完整配置，不要替换原配置。\n# 不保证回退IPv4；请在FlClash原生附加规则保存。\n'+plan['rule']+'\n'}
            return {'native_prepare':native.prepare,'native_verify':native.verify,'native_undo':native.undo_instructions}[body['operation']](plan)
        if body.get('operation') in {'apply','revoke','recover','reconcile'}:
            executor=getattr(self.ctx,'ipv6_executor',None)
            key=body.get('id');plans=self.ctx.ipv6_plans.read()
            if not executor or not isinstance(key,str) or key not in plans['plans']:raise DraftError('执行方案未就绪或尚未明确授权')
            if body.get('revision')!=plans['revision']:raise DraftError('方案已变化')
            return getattr(executor,body['operation'])(plans['plans'][key])
        plan=None
        if body.get('operation')=='delete':
            native=getattr(self.ctx,'ipv6_native',None)
            saved=self.ctx.ipv6_plans.read()['plans'].get(body.get('id'))
            if native and saved and native.status(saved)['state']!='not_started':
                if native.verify(saved)['state'] not in {'awaiting_client_save','revoked_verified'}:raise DraftError('原生规则尚未核实撤销，保留方案并先完成撤销核验')
            executor=getattr(self.ctx,'ipv6_executor',None)
            if executor and executor.status(body.get('id')).get('state') not in {'not_applied','revoked','rolled_back'}:raise DraftError('先撤销或恢复执行变更，再删除方案')
        if body.get('operation') in {'preview','save'}:
            subject=normalize_subject(body.get('subject'))
            if subject['kind']!='domain':
                context=self.ipv4_context()
                known={subject_id(a['subject']) for a in context['applications']} | {d['id'] for d in context['drafts']}
                saved={p['id'] for p in self.ctx.ipv6_plans.catalog()['plans']}
                if subject_id(subject) not in known | saved:raise DraftError('应用身份未核实，请刷新选择')
            plan=build_plan(subject,self.ctx.clash.ipv6_inventory(),inspect_flclash(flclash_dir()))
            if body.get('operation')=='save':
                native=getattr(self.ctx,'ipv6_native',None)
                previous=self.ctx.ipv6_plans.read()['plans'].get(plan['id'])
                if native and previous and native.status(previous)['state'] not in {'not_started','revoked_verified'}:
                    raise DraftError('此方案已进入原生保存流程，请先撤销并核验；不能覆盖原对象或配置基线')
        result=self.ctx.ipv6_plans.operate(body,plan)
        if body.get('operation')=='delete' and native and saved:native.forget(saved)
        if body.get('operation')=='preview':
            result['fingerprint']=digest(plan)
            executor=getattr(self.ctx,'ipv6_executor',None)
            result['execution_authorized']=bool(executor and executor.eligible(plan))
        return result

    def rule_context(self, process: str) -> dict[str, Any]:
        token(process)
        state=self.ctx.live.get() or {}
        exe,error=identity(state.get("flows") or [],process)
        if any(f.get("identity_conflict") for f in state.get("flows") or [] if f.get("process") == process):
            exe,error="","进程身份存在冲突，请等待重新采集"
        if state.get("error") or time.time()-float(state.get("ts") or 0)>15:
            exe,error="","连接数据过期；请等待重新采集后编辑"
        inventory=self.ctx.clash.rule_inventory()
        saved=self.ctx.rule_drafts.view(process)
        draft=saved["draft"]
        return {"process":process,"exe":exe,"identity_error":error,
                **saved,**inventory,"applied":False,
                "snippet":snippet(draft["exe"],draft["target"]) if draft else ""}

    def rule_draft(self, body: dict[str, Any]) -> dict[str, Any]:
        process=token(body.get("process"))
        operation=body.get("operation")
        if operation=="delete":
            return {**self.ctx.rule_drafts.delete(process,body.get("revision")),"applied":False}
        if operation=="export":
            return {"text":self.ctx.rule_drafts.export(process,body.get("revision")),"applied":False}
        if operation not in {"preview","save"}: raise DraftError("仅支持草稿预览、保存、删除和导出")
        context=self.rule_context(process)
        exe=context["exe"]
        if not exe: raise DraftError(context["identity_error"])
        if exe!=body.get("exe"): raise DraftError("进程身份已变化，请重新打开编辑器")
        action=body.get("action")
        target="DIRECT" if action=="direct" else token(body.get("target"))
        if action not in {"direct","proxy"}: raise DraftError("请选直连或指定策略组；跟随原规则仅删除草稿")
        if action=="proxy" and (context["groups_error"] or target not in context["groups"]):
            raise DraftError("策略组不存在或无法核实，请刷新编辑器后选择")
        self.ctx.rule_drafts.check(process,exe,target)
        warnings=["未应用：草稿不会改变实时连接状态。", "按可执行文件名匹配，会影响所有同名进程。",
                  "未检查完整规则顺序、域名/规则集遮蔽、当前模式和进程识别设置；人工应用前需核对。",
                  "绕过 TUN 的连接可能不经过内核，规则不能保证改变其路径；已有连接不会自动重连。"]
        if action=="proxy": warnings.append("策略组可能选择 DIRECT；指定组不保证一直走代理，且不会切换共享组。")
        if context["rules_error"]: warnings.append(context["rules_error"])
        for rule in context["rules"]:
            if str(rule["exe"]).casefold()==exe.casefold():
                warnings.append("已知重复：内核有相同进程目标规则。" if rule["target"]==target else "已知冲突：内核有该进程的不同目标规则，需人工核对优先级。")
        saved=self.ctx.rule_drafts.view(process)
        if operation=="save":
            saved=self.ctx.rule_drafts.save(process,exe,action,target,body.get("revision"))
        return {**saved,"snippet":snippet(exe,target),"warnings":list(dict.fromkeys(warnings)),"applied":False}

    def icon_png(self, name: str, version: str = "") -> bytes | None:
        flows = [f for f in (self.ctx.live.get() or {}).get("flows", []) if f.get("process") == name]
        paths = {str(f.get("proc_path")) for f in flows if f.get("proc_path") and not f.get("identity_conflict")}
        source = ""
        if version and version != 'generic' and not version.startswith('history-'):
            matches = [path for path in paths if self.ctx.icons.resource_key(path) == version.split('-')[0]]
            source = matches[0] if len(matches) == 1 else ""
        elif len(paths) == 1:
            source = next(iter(paths))
        elif not flows:
            source = self.ctx.store.program_path(name)
        # Never guess from a same-name cache or a truncated historical path.
        if not local_executable(source) or PureWindowsPath(source).stem.casefold() != name.casefold().removesuffix('.exe'):
            source = ""
        dest = self.ctx.icons.fetch(name, source)
        if dest is None or dest == self.ctx.icons.path_for('@generic', '@generic'):
            return None
        try:
            return dest.read_bytes()
        except OSError:
            return None

    def process_folder(self, body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise DraftError('目录请求无效')
        name, path = body.get('process'), body.get('path')
        if not isinstance(name, str) or not isinstance(path, str) or not local_executable(path):
            raise DraftError('需要合法本地可执行文件路径')
        state = self.ctx.live.get() or {}
        if state.get('error') or time.time()-float(state.get('ts') or 0)>15:
            raise DraftError('连接身份已过期，请刷新后再打开目录')
        candidates = [f for f in state.get('flows') or [] if f.get('process')==name and
            f.get('identity_verified') and not f.get('identity_conflict') and
            PureWindowsPath(f.get('proc_path') or '')==PureWindowsPath(path)]
        if not candidates:
            raise DraftError('此路径尚未由本机身份验证')
        valid = False
        for flow in candidates[:32]:
            current = query_process(int(flow.get('pid') or 0))
            if (current.get('status')=='verified' and current.get('created')==flow.get('identity_created') and
                    PureWindowsPath(current.get('path') or '')==PureWindowsPath(path)):
                valid = True
                break
        if not valid:
            raise DraftError('进程已退出、身份变化或访问受限，请刷新')
        directory = Path(path).parent
        if not directory.is_dir():
            raise DraftError('所在文件夹不存在或无法访问')
        try:
            os.startfile(str(directory))
        except OSError as exc:
            raise DraftError('无法打开所在文件夹') from exc
        return {'opened': True}

    def update_info(self) -> dict[str, Any]:
        install_error = self.ctx._update_refused()
        capability = {"install_supported": not bool(install_error), "install_error": install_error}
        try:
            return {**self.ctx.updater.latest(), "frozen": frozen(), **capability}
        except Exception as exc:  # noqa: BLE001 - 网络、GitHub 限流等，原样告诉页面
            return {"ok": False, "current": APP_VERSION, "frozen": frozen(), "error": str(exc), "newer": False, **capability}

    def apply_update(self) -> dict[str, Any]:
        try:
            return self.ctx.updater.apply()
        except Exception as exc:  # noqa: BLE001
            fmlog.log_exception("更新", exc)
            return {"ok": False, "error": str(exc)}

    def diag(self) -> dict[str, Any]:
        return self.ctx.product_diag()

    def export_csv(self) -> bytes:
        return self.ctx.collector.export_csv()

    def reset(self) -> dict[str, Any]:
        lifecycle = getattr(self.ctx, '_lifecycle_lock', None)
        busy = {"ok": False, "persisted": False, "pending": False, "busy": True,
                "error": "正在退出或准备更新，未清零；请稍后重试。"}
        if lifecycle is not None and not lifecycle.acquire(blocking=False):
            return busy
        try:
            if (getattr(self.ctx, '_shutdown_in_progress', False)
                    or getattr(self.ctx, 'stop', None) is not None and self.ctx.stop.is_set()):
                return busy
            result = self.ctx.store.reset()
            self.ctx.ranks.invalidate()
            return result
        finally:
            if lifecycle is not None:
                lifecycle.release()

    def open_data(self) -> bool:
        return _open_folder(self.ctx.data_dir)

    def open_install(self) -> bool:
        return _open_folder(install_exe_path().parent)

    def open_github(self) -> bool:
        return bool(webbrowser.open(GITHUB_URL))

    def test_clash(self) -> dict[str, Any]:
        return self.ctx.clash.test_link()

    def close_connections(self, ids: list[str]) -> int:
        return sum(1 for cid in ids if self.ctx.clash.close_connection(cid))

    def refresh_sub(self) -> None:
        self.ctx.subinfo.schedule(force=True)

    def ack_new(self, events: list[dict[str, Any]]) -> None:
        self.ctx.store.ack_new_events(events)


def _tray_pulse(ctx: AppContext, tray: WinTray) -> TrayPulse:
    return TrayPulse(tray, live_view=ctx.store.live_view, alerts=ctx.live.alerts, settings=lambda: ctx.settings.data)


def _notify_first_hide(ctx: AppContext, tray: WinTray | None) -> bool:
    """Submit the first-use explanation once; a failed notification remains retryable."""
    marker = ctx.data_dir / "tray-hint-submitted"
    if not ctx.settings.data.get("notify_hide") and marker.is_file():
        return True
    if tray is None or not getattr(tray, "hwnd", None) or getattr(tray, "_nid", None) is None:
        return False
    try:
        submitted = tray.balloon(
            f"{APP_NAME} 仍在后台采集",
            "单击右下角托盘图标重新打开；彻底退出请右键托盘图标，选择“退出 Flash Monitor”。",
        )
        if submitted is not True:
            return False
        # Records acceptance by Windows, not whether the user has seen a balloon.
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            temporary = marker.with_suffix(".tmp")
            temporary.write_text("submitted\n", encoding="utf-8")
            temporary.replace(marker)
        except OSError:
            pass
        return True
    except Exception:  # noqa: BLE001
        return False


def run_gui(ctx: AppContext, url: str, *, minimized: bool = False) -> None:
    from webview_win import FlashDesktop, prepare_process

    prepare_process()
    wait_http(url)
    icon = resource_dir() / "icon.ico"
    holder: dict[str, Any] = {"tray": None}

    def first_hide() -> bool:
        return _notify_first_hide(ctx, holder["tray"])

    def on_quit() -> bool:
        if not ctx._lifecycle_lock.acquire(blocking=False):
            return False
        try:
            if ctx.shutdown.complete:
                return True
            if ctx._shutdown_in_progress:
                return False
            ctx._shutdown_in_progress = True
        finally:
            ctx._lifecycle_lock.release()
        def finish():
            ok = ctx.close_store()
            tray = holder['tray']
            if ok:
                if tray is not None:
                    tray.stop()
                if ctx.httpd is not None:
                    ctx.httpd.shutdown()
                desktop.request_quit()
            else:
                ctx._shutdown_in_progress = False
                desktop.show()
                if tray is not None:
                    tray.balloon('退出未完成', ctx.shutdown.error)
        threading.Thread(target=finish, name='graceful-quit', daemon=True).start()
        return False

    desktop = FlashDesktop(
        title=APP_NAME,
        url=url,
        icon=icon,
        userdata=state_home() / "webview2",
        placement_file=state_home() / "window.json",
        window_class=winshell.window_class(),
        fallback=lambda: DesktopShell(url, profile_dir=state_home() / "window", icon_path=icon),
        should_hide=lambda: bool(ctx.settings.data.get("close_to_tray", True)),
        on_first_hide=first_hide,
        on_quit=on_quit,
    )
    ctx.shell = desktop
    tray = WinTray(desktop.show, desktop.show, desktop.request_quit, class_name=winshell.tray_class(), icon_path=icon)
    holder["tray"] = tray
    _tray_pulse(ctx, tray).start(ctx.stop)
    desktop.run(minimized=minimized)


def _raise_keyboard_interrupt(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt


def _wait_for_save(ctx: AppContext) -> None:
    """Fallback when the window has already closed unexpectedly: retain process on save failure."""
    last_error = ''
    while not ctx.close_store():
        if ctx.shutdown.error != last_error:
            last_error = ctx.shutdown.error
            _say(last_error)
        try:
            time.sleep(1)
        except KeyboardInterrupt:
            _say('待写统计尚未保存，拒绝静默退出。')


def _print_json(text: str) -> None:
    if sys.stdout is not None and not sys.stdout.isatty() and hasattr(sys.stdout, "buffer"):
        # 管道 / 文件：统一 UTF-8；否则中文 Windows 的 GBK 代码页遇到节点名里的 emoji 会直接抛异常
        sys.stdout.buffer.write((text + "\n").encode("utf-8"))
        sys.stdout.flush()
    else:
        print(text)


def _say(text: str) -> None:
    """控制台提示。编码不了的字符换成 ?（比如 stdout 是 cp1252 的管道），不能因为一行提示让程序起不来。"""
    stream = sys.stdout
    if stream is None:
        return
    try:
        print(text, file=stream, flush=True)
    except UnicodeEncodeError:
        enc = getattr(stream, "encoding", None) or "ascii"
        print(text.encode(enc, "replace").decode(enc, "replace"), file=stream, flush=True)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=f"{APP_NAME}：本机 Clash / mihomo 流量监视")
    parser.add_argument("--port", type=int, default=LISTEN_PORT)
    parser.add_argument("--once", action="store_true", help="打印一次 JSON 后退出")
    parser.add_argument("--gui", action="store_true", help="打开桌面窗口（Windows 默认开启）")
    parser.add_argument("--no-gui", action="store_true", help="只采集，不打开窗口")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--browser", action="store_true", help="另外用系统浏览器打开")
    parser.add_argument("--minimized", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    fmlog.install_excepthooks()
    mutex = 0
    if not args.once:
        first, mutex = winshell.claim_single_instance()
        if not first:
            return 0
    data_dir = data_home()
    if args.once and not (data_dir / DB_NAME).exists() and winshell.other_instance_running():
        # 旧版（写 JSON）还在跑：这时候迁移，它之后记下的流量就进不了新库
        print(
            "另一个 Flash Monitor 正在运行，数据还没迁移到新格式。先从托盘退出它再用 --once，"
            "或者设环境变量 FLASHMONITOR_DATA_DIR 指向数据的一份副本。",
            file=sys.stderr,
        )
        return 2
    migrate_legacy_data(data_dir)
    fmlog.configure(data_dir)
    fmlog.app_log(f"start v{APP_VERSION} frozen={frozen()} port={args.port} dev={is_dev_instance()}")
    ctx = AppContext(data_dir)
    ctx.mutex = mutex
    ctx.port = args.port
    if not args.once and not is_dev_instance():
        if winshell.autostart_enabled():
            try:
                ctx.set_autostart(True)  # 程序挪了位置时把启动项改成新路径
            except RuntimeError as exc:
                fmlog.app_log(f"刷新开机启动项失败：{exc}")
            ctx.settings.data["autostart"] = True
        try:
            winshell.ensure_shortcuts()
        except Exception as exc:  # noqa: BLE001
            fmlog.log_exception("创建快捷方式", exc)
    if args.once:
        # 调试用：不启动后台线程，也不在退出时写库（正在运行的实例可能在用同一个数据目录）
        state = ctx.collector.with_alerts(ctx.collector.collect())
        full = build_all(state=state, store=ctx.store, ranks=ctx.ranks, prefs=ctx.settings.ui_prefs(), sub=ctx.subinfo.public())
        _print_json(json.dumps(full, ensure_ascii=False, indent=2))
        return 0

    httpd = bind_server(LISTEN_HOST, args.port, MonitorApi(ctx))
    ctx.httpd = httpd
    ctx.port = httpd.port
    if ctx.port != args.port:
        fmlog.app_log(f"端口 {args.port} 被占用或被系统保留，改用 {ctx.port}")
    ctx.start_background()
    url = ctx.page_url()
    use_gui = (not args.no_gui) and (frozen() or args.gui or sys.platform == "win32")
    minimized = bool(args.minimized or ctx.settings.data.get("start_minimized"))
    if use_gui:
        threading.Thread(target=httpd.serve_forever, daemon=True, name="http").start()
        if args.browser:
            webbrowser.open(url)
        try:
            run_gui(ctx, url, minimized=minimized)
        finally:
            ctx.stop.set()
            try:
                httpd.shutdown()
            except Exception:  # noqa: BLE001
                pass
            _wait_for_save(ctx)
        return 0

    _say(f"{APP_NAME}  {url}")
    _say("仅本机可访问。托盘图标可打开页面；Ctrl+C 退出。")
    if hasattr(signal, "SIGBREAK"):
        # Ctrl+Break / 关控制台窗口：走和 Ctrl+C 一样的退出流程，退出前写盘
        signal.signal(signal.SIGBREAK, _raise_keyboard_interrupt)
    tray = None
    if sys.platform == "win32":
        try:
            tray = WinTray(
                lambda: webbrowser.open(url),
                lambda: webbrowser.open(url),
                lambda: (ctx.stop.set(), threading.Thread(target=httpd.shutdown, daemon=True).start()),
                class_name=winshell.tray_class(),
                icon_path=resource_dir() / "icon.ico",
            )
            _tray_pulse(ctx, tray).start(ctx.stop)
        except Exception as exc:  # noqa: BLE001
            fmlog.log_exception("托盘", exc)
            tray = None
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        ctx.stop.set()
    finally:
        ctx.stop.set()
        if tray is not None:
            try:
                tray.stop()
            except Exception:  # noqa: BLE001
                pass
        _wait_for_save(ctx)
    return 0
