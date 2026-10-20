"""Classification and persistence contracts; every database is temporary."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flashmonitor.classify import clash_to_flow
from flashmonitor.db import TrafficDB
from flashmonitor.store import TrafficStore
from flashmonitor.views import RankCache, build_view


class AccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1800000000
        self.root = Path(self.tmp.name)
        self.store = self.open()

    def open(self, **kwargs):
        result = TrafficStore(self.root, clock=lambda:self.now, **kwargs)
        self.addCleanup(result.close)
        return result

    def feed(self):
        self.store.update([])
        rows=[]
        for i, chain in enumerate((['DIRECT'],['node'],[],['PASS'],['REJECT'],['REJECT-DROP']), 1):
            rows.append(clash_to_flow({'id':str(i), 'upload':i, 'download':i*10, 'chains':chain,
                        'metadata':{'process':'app.exe','host':'example.com','type':'Tun'}}))
        self.store.update(rows)

    def assert_totals(self, store):
        for period in ('today','all'):
            pack=store.ranking(period)
            row=pack['rows'][0]
            self.assertEqual((row['upload'],row['download']), (21,210))
            self.assertEqual((row['proxy_down'],row['direct_down'],row['unknown_down'],row['rejected_down']), (20,10,70,110))
            self.assertEqual((pack['direct_download'],pack['unknown_download'],pack['rejected_download']), (10,70,110))
            site=pack['sites']['rows'][0]
            self.assertEqual((site['direct_down'],site['unknown_down'],site['rejected_down']), (10,70,110))
        self.assertGreater(store.live_view()['eaters'][0]['unknown_down'],0)

    def test_mixed_categories_survive_flush_without_double_counting(self):
        self.feed()
        self.assert_totals(self.store)
        self.store.flush()
        self.assert_totals(self.store)
        self.store.flush()
        self.assert_totals(self.store)

    def test_categories_persist_across_restart_and_reset(self):
        self.feed()
        self.store.close()
        reopened=self.open()
        for period in ('today','all'):
            row=reopened.ranking(period)['rows'][0]
            self.assertEqual((row['download'],row['direct_down'],row['unknown_down'],row['rejected_down']), (210,10,70,110))
        reopened.reset()
        reopened.close()
        after=self.open()
        self.assertEqual(after.ranking('all')['rows'],[])
        self.assertEqual(after.ranking('today')['rows'],[])

    def test_failed_flush_retains_categories_and_health_recovers(self):
        self.feed()
        with patch.object(self.store.db,'write',side_effect=sqlite3.OperationalError('private/path')):
            self.assertFalse(self.store.flush())
        self.assert_totals(self.store)
        health=self.store.storage_health()
        self.assertFalse(health['healthy'])
        self.assertTrue(health['pending'])
        self.assertNotIn('private',health['last_error'])
        self.store.flush()
        self.assertTrue(self.store.storage_health()['healthy'])
        self.assertFalse(self.store.storage_health()['pending'])
        self.assert_totals(self.store)

    def test_snapshot_exposes_health_even_on_collector_error(self):
        self.feed()
        self.store.db.persistent=False
        try:
            for state in ({'ts':1,'status':{}}, {'ts':1,'error':'offline'}):
                data=build_view('conns','today',state=state,store=self.store,
                                ranks=RankCache(self.store.ranking),prefs={},sub=None)
                self.assertFalse(data['status']['storage']['persistent'])
                self.assertFalse(data['status']['storage']['healthy'])
        finally:
            self.store.db.persistent=True

    def test_coverage_distinguishes_lifetime_and_retained_details(self):
        self.feed()
        pack=self.store.ranking('all')
        self.assertTrue(pack['coverage']['lifetime'])
        self.assertFalse(pack['coverage']['retention_limited'])
        for name in ('nodes','sites'):
            self.assertFalse(pack[name]['coverage']['lifetime'])
            self.assertTrue(pack[name]['coverage']['retention_limited'])
            self.assertEqual(pack[name]['coverage']['since'],self.now-40*86400)
        self.assertTrue(pack['rows'][0]['sites_coverage']['top_only'])

    def test_bounded_site_detail_does_not_misclassify_unknown(self):
        self.store.close()
        self.store=self.open(program_sites_enabled=True)
        self.feed()
        row=self.store.ranking('today')['rows'][0]
        site=row['sites'][0]
        self.assertEqual((site['download'],site['direct_down']), (30,10))
        self.assertEqual(row['sites_coverage']['measured_outbounds'],['proxy','direct'])
        self.assertEqual(row['sites_coverage']['since'],self.now)

    def test_legacy_counters_remain_unchanged(self):
        self.store.close()
        db=TrafficDB(self.root/'flashmonitor.db')
        db.write(programs={'old':{'upload':5,'download':80,'direct_up':5,'direct_down':80}},
                 slot_rows=[('proc',self.now,'old',0,0,5,80)])
        db.close()
        reopened=self.open()
        row=reopened.ranking('all')['rows'][0]
        self.assertEqual((row['direct_down'],row['unknown_down'],row['rejected_down']), (80,0,0))
        self.assertFalse(reopened.ranking('all')['coverage']['historical_classification_recomputed'])

    def test_extra_lifetime_records_roll_back_with_main_transaction(self):
        self.feed()
        self.store.flush()
        before=self.store.db.load_programs()
        with self.assertRaises(ValueError):
            self.store.db.write(programs={'app':{'upload':99,'unknown_up':99}},
                                slot_rows=[('procsite',1,'bad-json',1,0,0,0)])
        self.assertEqual(self.store.db.load_programs(),before)

    def test_old_database_opens_without_rewriting_legacy_rows(self):
        self.feed()
        self.store.close()
        with sqlite3.connect(self.root/'flashmonitor.db') as db:
            db.execute('DROP TABLE outbound_extra')
            old=db.execute('SELECT * FROM programs').fetchall()
            slots=db.execute('SELECT * FROM slot_traffic').fetchall()
        db.close()
        opened=TrafficDB(self.root/'flashmonitor.db')
        try:
            self.assertEqual(opened.conn.execute('SELECT * FROM programs').fetchall(),old)
            self.assertEqual(opened.conn.execute('SELECT * FROM slot_traffic').fetchall(),slots)
            self.assertEqual(opened.load_programs()['app']['unknown_down'],0)
        finally:
            opened.close()


if __name__ == '__main__':
    unittest.main()
