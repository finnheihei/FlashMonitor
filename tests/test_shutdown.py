import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from flashmonitor.shutdown import Shutdown
from flashmonitor.store import TrafficStore
from flashmonitor.collector import Collector
from test_store import flow


class ShutdownTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)
        self.store=TrafficStore(self.path)
        self.stop=threading.Event();self.threads=[];self.release=Mock()
        self.coordinator=Shutdown(self.stop,lambda:self.threads,self.store,self.release)
        self.store.update([]);self.store.update([flow('a','worker',down=100)])

    def test_wait_for_final_sample_then_flush_without_duplicate(self):
        def producer():
            self.stop.wait();time.sleep(.02)
            self.store.update([flow('a','worker',down=150)])
        thread=threading.Thread(target=producer,name='last-sample');self.threads=[thread];thread.start()
        self.assertTrue(self.coordinator.finish(1))
        self.assertTrue(self.coordinator.finish(.01))
        self.release.assert_called_once()
        reopened=TrafficStore(self.path)
        try:self.assertEqual(reopened.program_rows()[0]['download'],150)
        finally:reopened.close()

    def test_producer_timeout_refuses_exit_and_keeps_db_until_retry(self):
        gate=threading.Event()
        thread=threading.Thread(target=lambda:gate.wait(2),name='blocked-sample');self.threads=[thread];thread.start()
        try:
            started=time.monotonic();self.assertFalse(self.coordinator.finish(.02))
            self.assertLess(time.monotonic()-started,.5)
            self.assertIn('blocked-sample',self.coordinator.error)
            self.assertIsNotNone(self.store.db.conn);self.release.assert_not_called()
        finally:gate.set();thread.join(2)
        self.assertTrue(self.coordinator.finish(1))

    def test_write_failure_keeps_pending_stats_and_can_retry(self):
        with patch.object(self.store.db,'write',side_effect=sqlite3.OperationalError('disk full')):
            self.assertFalse(self.coordinator.finish(1))
            self.assertIn('写盘失败',self.coordinator.error)
            self.assertFalse(self.store._closed);self.assertIsNotNone(self.store.db.conn)
            self.assertEqual(self.store.ranking('all')['download'],100)
            self.release.assert_not_called()
        self.assertTrue(self.coordinator.finish(1))
        reopened=TrafficStore(self.path)
        try:self.assertEqual(reopened.ranking('all')['download'],100)
        finally:reopened.close()

    def test_blocked_final_write_uses_one_worker_and_stays_open(self):
        gate=threading.Event();started=threading.Event();real=self.store.db.write
        def slow(**kwargs):started.set();gate.wait(2);return real(**kwargs)
        with patch.object(self.store.db,'write',side_effect=slow):
            try:
                self.assertFalse(self.coordinator.finish(.02));self.assertTrue(started.wait(1))
                writer=self.coordinator._writer
                self.assertFalse(self.coordinator.finish(.02));self.assertIs(writer,self.coordinator._writer)
                self.assertFalse(self.store._closed);self.release.assert_not_called()
            finally:gate.set()
            self.assertTrue(self.coordinator.finish(1))

    def test_memory_database_cannot_be_reported_as_durable_exit(self):
        self.store.db.persistent=False
        self.assertFalse(self.coordinator.finish(1))
        self.assertIn('仅在内存',self.coordinator.error)
        self.assertIsNotNone(self.store.db.conn)
        self.release.assert_not_called()
        self.store.db.persistent=True
        self.assertTrue(self.coordinator.finish(1))

    def test_busy_final_wal_checkpoint_refuses_exit_until_reader_releases(self):
        self.store.flush()
        reader=sqlite3.connect(self.store.db.path)
        reader.execute('BEGIN');reader.execute('SELECT * FROM programs').fetchall()
        self.store.db.conn.execute('PRAGMA busy_timeout=1')
        self.store.update([flow('a','worker',down=150)])
        try:
            self.assertFalse(self.coordinator.finish(1))
            self.assertIn('检查点未完成',self.coordinator.error)
            self.assertIsNotNone(self.store.db.conn);self.release.assert_not_called()
        finally:reader.rollback();reader.close()
        self.assertTrue(self.coordinator.finish(1))
        reopened=TrafficStore(self.path)
        try:self.assertEqual(reopened.ranking('all')['download'],150)
        finally:reopened.close()

    def test_update_arriving_between_flush_and_close_is_not_discarded(self):
        gate=threading.Event();tail_done=threading.Event();real=self.store.flush
        def flushing():
            result=real();gate.set();self.assertTrue(tail_done.wait(2));return result
        def tail():
            gate.wait(2);self.store.update([flow('a','worker',down=150)]);tail_done.set()
        thread=threading.Thread(target=tail);thread.start()
        with patch.object(self.store,'flush',side_effect=flushing):
            with self.assertRaises(OSError):self.store.close()
        thread.join(2);self.assertFalse(self.store._closed)
        self.store.close()
        with self.assertRaises(RuntimeError):self.store.update([flow('a','worker',down=999)])
        reopened=TrafficStore(self.path)
        try:self.assertEqual(reopened.ranking('all')['download'],150)
        finally:reopened.close()

    def test_concurrent_shutdown_request_does_not_deadlock(self):
        gate=threading.Event()
        producer=threading.Thread(target=lambda:gate.wait(2),name='producer');producer.start();self.threads=[producer]
        result=[];closer=threading.Thread(target=lambda:result.append(self.coordinator.finish(1)));closer.start()
        self.assertTrue(self.stop.wait(1))
        self.assertFalse(self.coordinator.finish(.01))
        gate.set();closer.join(2);producer.join(2)
        self.assertEqual(result,[True])

    def tearDown(self):
        for thread in self.threads:thread.join(2)
        self.store.close()


class DesktopExitTests(unittest.TestCase):
    def test_refused_quit_does_not_destroy_ui(self):
        from webview_win import FlashDesktop
        desktop=FlashDesktop.__new__(FlashDesktop)
        desktop.on_quit=Mock(return_value=False);desktop._save_box=Mock();desktop._teardown_webview=Mock()
        desktop._quit()
        desktop._save_box.assert_not_called();desktop._teardown_webview.assert_not_called()

    def test_success_calls_guard_before_releasing_window_resources(self):
        from webview_win import FlashDesktop
        order=[];desktop=FlashDesktop.__new__(FlashDesktop)
        desktop.on_quit=lambda:order.append('saved') or True
        desktop._save_box=lambda:order.append('box');desktop._teardown_webview=lambda:order.append('release')
        desktop.shell=None;desktop.hwnd=0
        with patch('ctypes.windll.user32.PostQuitMessage') as quitting:desktop._quit()
        self.assertEqual(order,['saved','box','release']);quitting.assert_called_once()

    def test_collector_tracks_threads_and_does_not_double_start(self):
        collector=Collector.__new__(Collector);collector.threads=[];stop=threading.Event()
        for name in ['clash_sampler','traffic_reader','poller','store_flusher']:
            setattr(collector,name,lambda event:event.wait(2))
        try:
            collector.start_threads(stop);collector.start_threads(stop)
            self.assertEqual(len(collector.threads),4)
        finally:
            stop.set()
            for thread in collector.threads:thread.join(2)


class DisabledDetailsTests(unittest.TestCase):
    def test_default_does_not_create_or_write_detail_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=TrafficStore(Path(tmp))
            try:
                store.update([]);store.update([flow('x','worker',down=100,remote='example.com:443')]);store.flush()
                tables={r[0] for r in store.db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertNotIn('program_site_slots',tables)
                self.assertIsNone(store.db.meta('program_sites_since'))
                row=store.ranking('today')['rows'][0]
                self.assertEqual(row['sites'],[]);self.assertIsNone(row['sites_period'])
                self.assertIn('未启用',row['sites_note'])
                self.assertEqual(store.ranking('all')['download'],100)
            finally:store.close()

    def test_disabled_downgrade_preserves_existing_detail_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            first=TrafficStore(Path(tmp),program_sites_enabled=True)
            first.update([]);first.update([flow('x','worker',down=100,remote='example.com:443')]);first.close()
            from flashmonitor.db import PAIR_SCHEMA, DB_NAME
            conn=sqlite3.connect(Path(tmp)/DB_NAME)
            conn.executescript(PAIR_SCHEMA)
            conn.execute('INSERT INTO program_site_slots VALUES(?,?,?,?,?,?)',(int(time.time()),'legacy',0,100,0,0))
            conn.commit();conn.close()
            second=TrafficStore(Path(tmp))
            try:
                count=second.db.conn.execute('SELECT count(*) FROM program_site_slots').fetchone()[0]
                self.assertGreater(count,0)
                second.flush();self.assertEqual(second.db.conn.execute('SELECT count(*) FROM program_site_slots').fetchone()[0],count)
                self.assertEqual(second.ranking('all')['download'],100)
            finally:second.close()
