import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from flashmonitor.site_details import DetailPolicy, SiteDetails
from flashmonitor.store import TrafficStore
from flashmonitor.shutdown import Shutdown
from test_store import Clock, NOW, flow


class ConnectionFault:
    def __init__(self, conn, failure):self.conn,self.failure=conn,failure
    def __getattr__(self,key):return getattr(self.conn,key)
    def execute(self,sql,*args):
        if sql=='BEGIN IMMEDIATE':raise self.failure
        return self.conn.execute(sql,*args)


class DetailBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'program-sites.db';self.clock=Clock(NOW)
        self.detail=SiteDetails(self.path,clock=self.clock)
        self.addCleanup(lambda:self.detail.close())

    def add(self,index=0,at=None):
        self.detail.add(at or self.clock(), 'worker',f'{index}.example.com',(0,100,0,0))

    def test_seven_days_pruned_on_restart_and_lifetime_unchanged(self):
        store=TrafficStore(Path(self.tmp.name),clock=self.clock,monotonic=self.clock,site_details=self.detail)
        store.update([]);store.update([flow('a','worker',down=100,remote='day.example:443')]);store.close()
        self.clock.advance(8*86400)
        second=TrafficStore(Path(self.tmp.name),clock=self.clock,monotonic=self.clock,program_sites_enabled=True)
        try:
            self.assertEqual(second.site_details.query(),{})
            self.assertEqual(second.site_details.conn.execute('SELECT count(*) FROM slots').fetchone()[0],0)
            self.assertEqual(second.ranking('all')['download'],100)
            self.assertIn('未补造',second.site_details.note())
            self.assertEqual(second.site_details.policy.retention_days,7)
            self.assertEqual(second.db.slot_totals('procsite'),{})
        finally:second.close()

    def test_row_budget_evicts_oldest_and_persists_gap(self):
        self.detail.close();self.detail=SiteDetails(self.path,clock=self.clock,policy=DetailPolicy(max_rows=3))
        for i in range(2):self.add(i)
        self.detail.flush();self.clock.advance(900)
        for i in range(2,5):self.add(i)
        self.assertTrue(self.detail.flush())
        self.assertEqual(self.detail.conn.execute('SELECT count(*) FROM slots').fetchone()[0],3)
        self.assertEqual({json.loads(k)[1] for k in self.detail.query()},{f'{i}.example.com' for i in range(2,5)})
        self.detail.close();self.detail=SiteDetails(self.path,clock=self.clock)
        self.assertTrue(self.detail.partial);self.assertGreaterEqual(self.detail.dropped,2)

    def test_batch_larger_than_row_budget_is_bounded(self):
        self.detail.close();self.detail=SiteDetails(self.path,clock=self.clock,policy=DetailPolicy(max_rows=2))
        for i in range(10):self.add(i)
        self.assertTrue(self.detail.flush())
        self.assertEqual(self.detail.conn.execute('SELECT count(*) FROM slots').fetchone()[0],2)
        self.assertTrue(self.detail.partial)

    def test_pending_budget_includes_inflight(self):
        self.detail.close();self.detail=SiteDetails(self.path,clock=self.clock,policy=DetailPolicy(max_pending=2))
        self.add(0);self.add(1)
        entered=threading.Event();resume=threading.Event();conn=self.detail.conn
        class Slow:
            def __getattr__(self,key):return getattr(conn,key)
            def execute(self,sql,*args):
                if sql=='BEGIN IMMEDIATE':entered.set();resume.wait(2)
                return conn.execute(sql,*args)
        self.detail.conn=Slow();worker=threading.Thread(target=self.detail.flush);worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.add(2);self.add(0)
            self.assertLessEqual(len(self.detail._pending)+len(self.detail._inflight),2)
            self.assertTrue(self.detail.partial)
        finally:resume.set();worker.join(2);self.detail.conn=conn
        self.assertFalse(worker.is_alive())

    def test_real_sqlite_full_never_exceeds_file_budget(self):
        self.detail.close();self.detail=SiteDetails(self.path,clock=self.clock,policy=DetailPolicy(max_db_bytes=32768,max_rows=1000))
        for batch in range(8):
            for i in range(80):self.detail.add(self.clock(),'worker',f'{batch}-{i}.'+'x'*400,(0,100,0,0))
            self.assertTrue(self.detail.flush())
            self.assertLessEqual(self.path.stat().st_size,32768)
        self.assertTrue(self.detail.partial)
        self.assertEqual(self.detail.conn.execute('PRAGMA journal_mode').fetchone()[0],'delete')

    def test_locked_write_retains_pending_and_retry_is_not_double_counted(self):
        self.add();conn=self.detail.conn
        self.detail.conn=ConnectionFault(conn,sqlite3.OperationalError('locked'))
        self.assertFalse(self.detail.flush());self.assertEqual(len(self.detail._pending),1)
        self.assertEqual(sum(v[1] for v in self.detail.query().values()),100)
        with self.assertRaises(OSError):self.detail.close()
        self.detail.conn=conn;self.assertTrue(self.detail.flush())
        self.assertEqual(sum(v[1] for v in self.detail.query().values()),100)

    def test_corrupt_unknown_and_oversize_files_are_not_replaced(self):
        for content in (b'not a sqlite file',b'x'*(8*1024*1024+1)):
            other=Path(self.tmp.name)/'unknown.db';other.write_bytes(content)
            detail=SiteDetails(other,clock=self.clock)
            self.assertIsNone(detail.conn);self.assertTrue(detail.partial)
            self.assertEqual(other.read_bytes(),content);detail.close()

    def test_key_and_counter_limits_discard_optional_details(self):
        self.detail.add(self.clock(),'worker','x'*600,(0,100,0,0))
        self.detail.add(self.clock(),'worker','example.com',(0,2**63,0,0))
        self.assertEqual(self.detail.query(),{});self.assertEqual(self.detail.dropped,2)

    def test_reset_hides_old_rows_and_retains_new_delta(self):
        self.add();self.detail.flush();self.detail.reset()
        self.assertEqual(self.detail.query(),{})
        self.add(1);self.detail.flush()
        self.assertEqual({json.loads(k)[1] for k in self.detail.query()},{'1.example.com'})

    def test_tail_after_final_flush_refuses_close_and_retries(self):
        self.add();real=self.detail.flush
        def tail():
            result=real();self.add(1);return result
        with patch.object(self.detail,'flush',side_effect=tail):
            with self.assertRaises(OSError):self.detail.close()
        self.assertFalse(self.detail._closed)
        self.detail.close()
        reopened=SiteDetails(self.path,clock=self.clock)
        try:self.assertEqual(sum(v[1] for v in reopened.query().values()),200)
        finally:reopened.close()

    def test_read_error_does_not_break_lifetime_ranking(self):
        store=TrafficStore(Path(self.tmp.name),clock=self.clock,monotonic=self.clock,site_details=self.detail)
        try:
            store.update([]);store.update([flow('a','worker',down=100)]);store.flush()
            conn=self.detail.conn
            class BrokenRead:
                def __getattr__(self,key):return getattr(conn,key)
                def execute(self,sql,*args):
                    if 'GROUP BY name' in sql:raise sqlite3.OperationalError('read IO error')
                    return conn.execute(sql,*args)
            self.detail.conn=BrokenRead()
            try:
                self.assertEqual(store.ranking('all')['download'],100)
                self.assertIn('读取失败',self.detail.note())
            finally:self.detail.conn=conn
        finally:store.close()

    def test_detail_write_failure_refuses_exit_after_main_total_saved(self):
        store=TrafficStore(Path(self.tmp.name),clock=self.clock,monotonic=self.clock,site_details=self.detail)
        store.update([]);store.update([flow('a','worker',down=100,remote='example.com:443')])
        conn=self.detail.conn;self.detail.conn=ConnectionFault(conn,sqlite3.OperationalError('disk IO error'))
        release=Mock();shutdown=Shutdown(threading.Event(),lambda:[],store,release)
        try:
            self.assertFalse(shutdown.finish(1));self.assertIn('写盘失败',shutdown.error)
            release.assert_not_called();self.assertIsNotNone(store.db.conn)
            self.assertEqual(store.ranking('all')['download'],100)
            self.detail.conn=conn;self.assertTrue(shutdown.finish(1))
            second=TrafficStore(Path(self.tmp.name),clock=self.clock,monotonic=self.clock,program_sites_enabled=True)
            try:
                self.assertEqual(second.ranking('all')['download'],100)
                self.assertEqual(sum(v[1] for v in second.site_details.query().values()),100)
            finally:second.close()
        finally:self.detail.conn=conn if not self.detail._closed else None;store.close()
