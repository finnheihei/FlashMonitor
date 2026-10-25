"""Reset acknowledgements reflect durable state without touching user data or sockets."""
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from flashmonitor.app import MonitorApi
from flashmonitor.server import _reset
from flashmonitor.store import TrafficStore
from test_store import flow


class ResetPersistenceTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.store = TrafficStore(self.path, program_sites_enabled=True)
        self.addCleanup(self.store.close)
        self.store.update([])
        self.store.update([flow('one', 'browser', down=100, remote='example.com:443')])
        self.store.flush()
        self.ctx = SimpleNamespace(store=self.store, ranks=Mock(),
                                   _lifecycle_lock=threading.RLock(),
                                   _shutdown_in_progress=False, stop=threading.Event())
        self.api = MonitorApi(self.ctx)

    def request_reset(self):
        responses = []
        handler = SimpleNamespace(read_json=lambda: {}, server=SimpleNamespace(app=self.api),
                                  send_json=lambda status, data: responses.append((status, data)))
        _reset(handler)
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0][0], 200)
        return responses[0][1]

    def disk_total(self):
        with closing(sqlite3.connect(self.store.db.path)) as db:
            return db.execute('SELECT coalesce(sum(download),0) FROM programs').fetchone()[0]

    def test_success_only_after_both_databases_persist_reset(self):
        result = self.request_reset()
        self.assertEqual(result, {'ok': True, 'persisted': True, 'pending': False})
        self.assertEqual(self.disk_total(), 0)
        self.assertEqual(self.store.site_details.query(), {})
        self.assertFalse(self.store.storage_health()['reset_pending'])
        self.ctx.ranks.invalidate.assert_called_once()

    def test_failed_main_write_reports_pending_and_retry_keeps_new_deltas(self):
        with patch.object(self.store.db, 'write', side_effect=sqlite3.OperationalError('disk full')):
            result = self.request_reset()
        self.assertFalse(result['ok'])
        self.assertFalse(result['persisted'])
        self.assertTrue(result['pending'])
        self.assertIn('尚未全部写入磁盘', result['error'])
        self.assertEqual(self.store.ranking('all')['download'], 0)
        self.assertEqual(self.disk_total(), 100)
        self.assertTrue(self.store.storage_health()['reset_pending'])

        # The live connection keeps its 100-byte baseline. Retry must preserve
        # the 50 bytes collected after the reset, not reset or recount them.
        self.store.update([flow('one', 'browser', down=150, remote='example.com:443')])
        self.assertEqual(self.store.ranking('all')['download'], 50)
        self.store.flush()
        self.assertEqual(self.disk_total(), 50)
        self.assertFalse(self.store.storage_health()['reset_pending'])
        self.assertFalse(self.store.storage_health()['pending'])

    def test_failed_detail_write_is_not_reported_as_complete(self):
        detail = self.store.site_details
        with patch.object(detail, '_write_meta', side_effect=sqlite3.OperationalError('detail locked')):
            result = self.request_reset()
        self.assertEqual(self.disk_total(), 0, 'the main database reset did commit')
        self.assertFalse(result['ok'])
        self.assertTrue(result['pending'])
        self.assertTrue(self.store.storage_health()['reset_pending'])
        with closing(sqlite3.connect(detail.path)) as db:
            self.assertEqual(db.execute('SELECT coalesce(sum(pd),0) FROM slots').fetchone()[0], 100)
        self.store.update([flow('one', 'browser', down=150, remote='example.com:443')])
        self.store.flush()
        self.assertFalse(self.store.storage_health()['reset_pending'])
        self.assertEqual(self.disk_total(), 50)
        self.assertEqual(sum(values[1] for values in detail.query().values()), 50)

    def test_duplicate_pending_reset_preserves_new_traffic_without_repeating_flush(self):
        with patch.object(self.store.db, 'write', side_effect=sqlite3.OperationalError('disk full')):
            first = self.request_reset()
        self.assertTrue(first['pending'])
        generation = self.store._reset_gen
        self.store.update([flow('one', 'browser', down=150, remote='example.com:443')])

        with patch.object(self.store, 'flush', wraps=self.store.flush) as flush:
            second = self.request_reset()
        self.assertEqual(second, first)
        flush.assert_not_called()
        self.assertEqual(self.store._reset_gen, generation)
        self.assertEqual(self.store.ranking('all')['download'], 50)
        self.assertEqual(self.disk_total(), 100)

        self.store.flush()
        self.assertEqual(self.disk_total(), 50)
        self.assertEqual(sum(values[1] for values in self.store.site_details.query().values()), 50)
        self.assertFalse(self.store.storage_health()['reset_pending'])

    def test_unavailable_details_report_pending_without_claiming_automatic_recovery(self):
        detail = self.store.site_details
        with patch.object(detail, 'conn', None):
            result = self.request_reset()
        self.assertFalse(result['ok'])
        self.assertTrue(result['pending'])
        self.assertIn('网站明细暂不可用', result['error'])
        self.assertNotIn('自动重试', result['error'])
        self.store.flush()
        self.assertFalse(self.store.storage_health()['reset_pending'])

    def test_memory_database_refuses_reset_without_losing_current_statistics(self):
        with patch.object(self.store.db, 'persistent', False):
            result = self.request_reset()
        self.assertFalse(result['ok'])
        self.assertFalse(result['pending'])
        self.assertIn('未修改当前统计', result['error'])
        self.assertEqual(self.store.ranking('all')['download'], 100)
        self.assertEqual(self.disk_total(), 100)

    def test_closed_database_refuses_reset(self):
        self.store.close()
        result = self.request_reset()
        self.assertFalse(result['ok'])
        self.assertFalse(result['pending'])
        self.assertIn('未清零', result['error'])
        self.assertEqual(self.disk_total(), 100)

    def test_update_or_exit_refuses_reset_before_mutation(self):
        for state in ('_shutdown_in_progress', 'stop'):
            with self.subTest(state=state):
                self.ctx._shutdown_in_progress = state == '_shutdown_in_progress'
                if state == 'stop':
                    self.ctx.stop.set()
                result = self.request_reset()
                self.assertFalse(result['ok'])
                self.assertTrue(result['busy'])
                self.assertFalse(result['pending'])
                self.assertEqual(self.store.ranking('all')['download'], 100)
        self.ctx.ranks.invalidate.assert_not_called()

    def test_other_lifecycle_request_prevents_concurrent_reset(self):
        self.ctx._lifecycle_lock.acquire()
        results = []
        worker = threading.Thread(target=lambda: results.append(self.api.reset()))
        try:
            worker.start()
            worker.join(1)
            self.assertFalse(worker.is_alive())
        finally:
            self.ctx._lifecycle_lock.release()
            worker.join(1)
        self.assertTrue(results[0]['busy'])
        self.assertEqual(self.store.ranking('all')['download'], 100)
        self.ctx.ranks.invalidate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
