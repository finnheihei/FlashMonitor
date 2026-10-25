"""Update preparation and rollback using isolated stores and mocked executable operations."""
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from flashmonitor import app, updater
from flashmonitor.app import AppContext, MonitorApi
from flashmonitor.collector import Collector
from flashmonitor.shutdown import Shutdown
from flashmonitor.store import TrafficStore
from flashmonitor.updater import Updater
from webview_win import FlashDesktop
from test_store import flow


class UpdateLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.ctx = AppContext.__new__(AppContext)
        self.ctx.stop = threading.Event()
        self.ctx._shutdown_in_progress = False
        self.ctx._lifecycle_lock = threading.RLock()
        self.ctx._update_sample_locked = False
        self.ctx.store = TrafficStore(self.path, program_sites_enabled=True)
        self.addCleanup(self.ctx.store.close)
        self.ctx.collector = SimpleNamespace(_sample_lock=threading.RLock(), threads=[])
        self.ctx.icons = Mock()
        self.ctx.ranks = Mock()
        self.ctx.shutdown = Shutdown(self.ctx.stop, lambda: [], self.ctx.store)
        self.ctx.store.update([])
        self.ctx.store.update([flow('a', 'demo', down=100)])

    def make_updater(self):
        return Updater(repo='demo/test', current='1.0', install_path=lambda:self.path/'FlashMonitor.exe',
                       prepare_restart=self.ctx.prepare_update_restart,
                       cancel_restart=self.ctx.cancel_update_restart,
                       on_restart=Mock())

    def test_failed_final_save_keeps_collection_and_can_retry(self):
        with patch.object(self.ctx.store.db, 'write', side_effect=sqlite3.OperationalError('disk full')):
            error = self.ctx.prepare_update_restart()
        self.assertIn('更新前保存失败', error)
        self.assertIn('采集已恢复', error)
        self.assertFalse(self.ctx.stop.is_set())
        self.assertFalse(self.ctx._shutdown_in_progress)
        self.assertFalse(self.ctx._update_sample_locked)
        self.assertFalse(self.ctx.store._closed)
        self.ctx.store.update([flow('a', 'demo', down=150)])
        self.assertEqual(self.ctx.store.ranking('all')['download'], 150)
        self.assertEqual(self.ctx.prepare_update_restart(), '')
        self.assertEqual(self.ctx.cancel_update_restart(), '')

    def test_launch_failure_reopens_same_store_and_preserves_live_baseline(self):
        up = self.make_updater()
        original = self.ctx.store
        original_db, original_details = original.db, original.site_details
        release = dict(newer=True, latest='1.1', tag='v1.1', exe_url='https://example.invalid/file', exe_sha256='a'*64)
        with patch.object(up, 'latest', return_value=release), \
             patch.object(updater, 'download_verified', return_value='a'*64), \
             patch.object(updater, 'file_survived', return_value=True), \
             patch.object(updater, 'write_update_script', return_value=self.path/'mock.cmd'), \
             patch.object(updater, 'launch_update_script', side_effect=OSError('launch denied')):
            result = up.apply()
        self.assertFalse(result['ok'])
        self.assertNotIn('restarting', result)
        self.assertIn('采集已恢复', result['error'])
        self.assertIs(self.ctx.store, original)
        self.assertIs(self.ctx.store.db, original_db)
        self.assertIs(self.ctx.store.site_details, original_details)
        self.assertIsNotNone(self.ctx.store.db.conn)
        self.assertFalse(self.ctx.store.site_details._closed)
        self.assertFalse(self.ctx.stop.is_set())
        self.assertFalse(self.ctx._shutdown_in_progress)
        self.assertFalse(self.ctx.shutdown.complete)
        self.ctx.store.update([flow('a', 'demo', down=150)])
        self.ctx.store.flush()
        self.assertEqual(self.ctx.store.ranking('all')['download'], 150)
        up._on_restart.assert_not_called()

    def test_busy_checkpoint_failure_keeps_store_available(self):
        with patch.object(self.ctx.store.db, 'checkpoint', side_effect=sqlite3.OperationalError('reader busy')):
            error = self.ctx.prepare_update_restart()
        self.assertIn('采集已恢复', error)
        self.assertFalse(self.ctx.store._closed)
        self.assertFalse(self.ctx.stop.is_set())
        self.ctx.store.update([flow('a', 'demo', down=160)])
        self.assertEqual(self.ctx.store.ranking('all')['download'], 160)

    def test_details_reopen_failure_reports_partial_recovery_and_keeps_totals(self):
        details = self.ctx.store.site_details
        self.assertEqual(self.ctx.prepare_update_restart(), '')
        def unavailable(reopened, *args, **kwargs):
            reopened.conn = None
            reopened.error, reopened.partial = 'details unavailable', True
            reopened.since, reopened.dropped = details.since, details.dropped
        with patch.object(type(details), '__init__', unavailable):
            error = self.ctx.cancel_update_restart()
        self.assertIn('程序累计采集已恢复，但网站明细暂不可用', error)
        self.assertIs(self.ctx.store.site_details, details)
        self.assertFalse(self.ctx.store._closed)
        self.assertFalse(self.ctx.stop.is_set())
        self.ctx.store.update([flow('a', 'demo', down=170)])
        self.ctx.store.flush()
        self.assertEqual(self.ctx.store.ranking('all')['download'], 170)

    def test_preparation_blocks_other_sampler_until_rollback(self):
        self.assertEqual(self.ctx.prepare_update_restart(), '')
        acquired = []
        def sample():
            ok = self.ctx.collector._sample_lock.acquire(timeout=.05)
            acquired.append(ok)
            if ok:self.ctx.collector._sample_lock.release()
        thread = threading.Thread(target=sample)
        thread.start();thread.join(1)
        self.assertEqual(acquired, [False])
        self.assertEqual(self.ctx.cancel_update_restart(), '')
        thread = threading.Thread(target=sample)
        thread.start();thread.join(1)
        self.assertEqual(acquired, [False, True])

    def test_source_and_dev_update_capabilities(self):
        with patch.object(app, 'is_dev_instance', return_value=False), patch.object(app, 'frozen', return_value=False):
            self.assertIn('源码运行', AppContext._update_refused())
            ctx = SimpleNamespace(_update_refused=AppContext._update_refused, updater=Mock())
            ctx.updater.latest.return_value = {'ok':True,'newer':True}
            result = MonitorApi(ctx).update_info()
            self.assertFalse(result['install_supported'])
            self.assertIn('源码运行', result['install_error'])
        with patch.object(app, 'is_dev_instance', return_value=True):
            self.assertIn('开发实例', AppContext._update_refused())

    def test_normal_exit_cannot_close_store_during_prepared_update(self):
        self.assertEqual(self.ctx.prepare_update_restart(), '')
        finished=threading.Event()
        self.ctx.shutdown.finish=Mock(return_value=True)
        worker=threading.Thread(target=lambda:(self.ctx.close_store(),finished.set()))
        worker.start()
        self.assertFalse(finished.wait(.05))
        self.ctx.shutdown.finish.assert_not_called()
        self.assertEqual(self.ctx.cancel_update_restart(), '')
        worker.join(1)
        self.assertTrue(finished.is_set())

    def test_snapshot_preserves_page_during_closed_store_preparation(self):
        self.assertEqual(self.ctx.prepare_update_restart(), '')
        with patch.object(app,'build_view') as build:
            result=MonitorApi(self.ctx).snapshot('rank','today')
        self.assertTrue(result['busy']);self.assertIn('error',result)
        build.assert_not_called()
        self.assertEqual(self.ctx.cancel_update_restart(), '')
        self.ctx.ranks.invalidate.assert_called()

    def test_snapshot_preserves_page_after_restart_committed(self):
        self.assertEqual(self.ctx.prepare_update_restart(), '')
        with patch.object(app.threading, 'Thread') as worker:
            self.ctx.shutdown_after_update()
        worker.return_value.start.assert_called_once()
        self.assertTrue(self.ctx.shutdown.complete)
        self.assertFalse(self.ctx._update_sample_locked)
        with patch.object(app, 'build_view') as build:
            self.assertTrue(MonitorApi(self.ctx).snapshot('rank', 'today')['busy'])
        build.assert_not_called()

    def test_snapshot_does_not_read_during_other_lifecycle_operation(self):
        results = []
        with self.ctx._lifecycle_lock, patch.object(app, 'build_view') as build:
            reader = threading.Thread(target=lambda:results.append(MonitorApi(self.ctx).snapshot('rank', 'today')))
            reader.start();reader.join(1)
            self.assertFalse(reader.is_alive())
            build.assert_not_called()
        self.assertTrue(results[0]['busy'])

    def test_manual_refresh_does_not_read_during_other_lifecycle_operation(self):
        results = []
        self.ctx.collector.refresh = Mock()
        api = MonitorApi(self.ctx)
        with self.ctx._lifecycle_lock:
            reader = threading.Thread(target=lambda:results.append(api.refresh_data({'period':'today'})))
            reader.start();reader.join(1)
            self.assertFalse(reader.is_alive())
        self.assertFalse(results[0]['ok'])
        self.assertTrue(results[0]['busy'])
        self.ctx.collector.refresh.assert_not_called()
        self.assertTrue(api._refresh_lock.acquire(blocking=False))
        api._refresh_lock.release()


class UpdateOrderingTests(unittest.TestCase):
    def test_prepare_exception_runs_rollback_and_never_launches(self):
        cancel=Mock(return_value='')
        up=Updater(repo='demo/test',current='1',install_path=lambda:Path('mock.exe'),
                   prepare_restart=Mock(side_effect=OSError('save failed')),cancel_restart=cancel)
        release=dict(newer=True,latest='2',exe_url='https://example.invalid/file',exe_sha256='a'*64)
        with patch.object(up,'latest',return_value=release), patch.object(updater,'download_verified',return_value='a'*64), \
             patch.object(updater,'file_survived',return_value=True), patch.object(updater,'write_update_script'), \
             patch.object(updater,'launch_update_script') as launch:
            result=up.apply()
        self.assertFalse(result['ok']);cancel.assert_called_once();launch.assert_not_called()

    def test_prepare_failure_does_not_launch_or_restart(self):
        up = Updater(repo='demo/test',current='1',install_path=lambda:Path('mock.exe'),
                     prepare_restart=Mock(return_value='save failed'),on_restart=Mock())
        release = dict(newer=True,latest='2',exe_url='https://example.invalid/file',exe_sha256='a'*64)
        with patch.object(up,'latest',return_value=release), patch.object(updater,'download_verified',return_value='a'*64), \
             patch.object(updater,'file_survived',return_value=True), patch.object(updater,'write_update_script'), \
             patch.object(updater,'launch_update_script') as launch:
            result=up.apply()
        self.assertEqual(result, {'ok':False,'error':'save failed'})
        launch.assert_not_called();up._on_restart.assert_not_called()

    def test_success_orders_save_before_launch_before_commit(self):
        order=[]
        up=Updater(repo='demo/test',current='1',install_path=lambda:Path('mock.exe'),
                   prepare_restart=lambda:order.append('save') or '',on_restart=lambda:order.append('commit'))
        release=dict(newer=True,latest='2',exe_url='https://example.invalid/file',exe_sha256='a'*64)
        with patch.object(up,'latest',return_value=release), patch.object(updater,'download_verified',return_value='a'*64), \
             patch.object(updater,'file_survived',return_value=True), patch.object(updater,'write_update_script'), \
             patch.object(updater,'launch_update_script',side_effect=lambda *a,**k:order.append('launch')):
            result=up.apply()
        self.assertEqual(order,['save','launch','commit'])
        self.assertTrue(result['ok']);self.assertTrue(result['restarting'])

    def test_latest_changed_to_older_release_never_downloads(self):
        up=Updater(repo='demo/test',current='2',install_path=lambda:Path('mock.exe'))
        with patch.object(up,'latest',return_value={'newer':False}), patch.object(updater,'download_verified') as download:
            self.assertFalse(up.apply()['ok'])
        download.assert_not_called()


class FirstHideTests(unittest.TestCase):
    def test_first_hide_explains_background_collection_once_by_default(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path=Path(temporary.name)
        ctx=SimpleNamespace(data_dir=self.path,settings=SimpleNamespace(data={"notify_hide":False}))
        tray=SimpleNamespace(hwnd=1,_nid=object(),balloon=Mock(side_effect=[False,True]))
        self.assertFalse(app._notify_first_hide(ctx,tray))
        self.assertFalse((self.path/"tray-hint-submitted").exists())
        self.assertTrue(app._notify_first_hide(ctx,tray))
        self.assertTrue((self.path/"tray-hint-submitted").exists())
        self.assertTrue(app._notify_first_hide(ctx,tray))
        self.assertEqual(tray.balloon.call_count,2)
        self.assertIn("退出 Flash Monitor",tray.balloon.call_args.args[1])
        ctx.settings.data["notify_hide"]=True
        tray.balloon.side_effect=None;tray.balloon.return_value=True
        self.assertTrue(app._notify_first_hide(ctx,tray))
        self.assertEqual(tray.balloon.call_count,3)

    def test_first_hide_consumed_only_after_notification(self):
        desktop=FlashDesktop.__new__(FlashDesktop)
        desktop._save_box=Mock();desktop._set_page_visible=Mock()
        desktop.hwnd=0;desktop.shell=None;desktop._first_hide=False
        desktop.on_first_hide=Mock(side_effect=[False,True])
        desktop._hide_window();self.assertFalse(desktop._first_hide)
        desktop._hide_window();self.assertTrue(desktop._first_hide)
        desktop._hide_window();self.assertEqual(desktop.on_first_hide.call_count,2)

    def test_hidden_fallback_does_not_repeat_hide_when_notification_disabled(self):
        desktop=FlashDesktop.__new__(FlashDesktop)
        desktop._user32=Mock();desktop._using_webview=False
        desktop.shell=Mock();desktop.shell.alive.return_value=False
        desktop.should_hide=Mock(return_value=True)
        desktop._hidden=True;desktop._first_hide=False;desktop._hide_window=Mock()
        desktop._wnd_proc(0, 0x0113, 1, 0)
        desktop._hide_window.assert_not_called()


class UpdateScriptSafetyTests(unittest.TestCase):
    def test_failure_branches_stop_without_launch_or_cleanup(self):
        script=updater.UPDATE_SCRIPT
        self.assertIn('WaitForExit(120000)', script)
        self.assertIn('if errorlevel 2 goto probe_failed', script)
        self.assertIn('if errorlevel 1 goto wait_failed', script)
        self.assertIn('goto move_failed\r\n:start', script)
        for label in ('wait_failed', 'probe_failed', 'move_failed'):
            block=script.split(':'+label+'\r\n', 1)[1].split('\r\n:', 1)[0]
            self.assertIn('exit /b 1', block)
            self.assertIn('"%DIAG%" echo Update stopped:', block)
            self.assertNotIn('start ""', block)
            self.assertNotIn('del "%~f0"', block)


class QueuedCollectorTests(unittest.TestCase):
    def test_queued_workers_do_not_collect_after_stop(self):
        for method in ('poller','clash_sampler'):
            with self.subTest(method=method):
                entered,released=threading.Event(),threading.Event()
                class Gate:
                    def __enter__(self):entered.set();released.wait(1)
                    def __exit__(self,*args):return False
                collector=Collector.__new__(Collector)
                collector._sample_lock=Gate();collector.clash=Mock();collector._on_poll=Mock()
                stop=threading.Event()
                worker=threading.Thread(target=getattr(collector,method),args=(stop,))
                worker.start();self.assertTrue(entered.wait(1))
                stop.set();released.set();worker.join(1)
                self.assertFalse(worker.is_alive())
                collector.clash.payload.assert_not_called();collector._on_poll.assert_not_called()


if __name__=='__main__':unittest.main()
