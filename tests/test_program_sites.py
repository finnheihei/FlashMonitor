import tempfile
import unittest
from pathlib import Path
from test_store import Clock, NOW, flow
from flashmonitor.store import TrafficStore, SLOT_SECONDS


class ProgramSitesTests(unittest.TestCase):
    def test_scoped_periods_persistence_unknown_and_no_double_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock=Clock(NOW)
            store=TrafficStore(Path(tmp),clock=clock,monotonic=clock,program_sites_enabled=True)
            try:
                store.update([flow('a','worker',down=0,remote='day.example:443'),flow('b','other',down=0,remote='other.example:443')])
                store.update([flow('a','worker',down=100,remote='day.example:443'),flow('b','other',down=200,remote='other.example:443')])
                before=store.ranking('today')
                worker=next(r for r in before['rows'] if r['process']=='worker')
                self.assertEqual([(s['site'],s['download']) for s in worker['sites']],[('day.example',100)])
                self.assertEqual(worker['download'],100)
                store.flush();self.assertEqual(store.ranking('today')['download'],before['download'])
                self.assertEqual(next(r for r in store.ranking('today')['rows'] if r['process']=='worker')['sites'],worker['sites'])
                clock.advance(2*86400)
                store.update([flow('c','worker',down=0,outbound='direct',remote='203.0.113.5:443'),flow('d','worker',down=0,outbound='direct',remote='?')])
                store.update([flow('c','worker',down=50,outbound='direct',remote='203.0.113.5:443'),flow('d','worker',down=20,outbound='direct',remote='?')])
                today=store.ranking('today')['rows'][0]
                self.assertEqual(today['download'],70)
                self.assertEqual({s['site'] for s in today['sites']},{'203.0.113.5','未知'})
                week=next(r for r in store.ranking('7d')['rows'] if r['process']=='worker')
                self.assertEqual(sum(s['download'] for s in week['sites']),week['download'])
                self.assertEqual({s['site'] for s in week['sites']},{'day.example','203.0.113.5','未知'})
                self.assertTrue(all('other.example'!=s['site'] for s in week['sites']))
            finally:store.close()
            second=TrafficStore(Path(tmp),clock=clock,monotonic=clock,program_sites_enabled=True)
            try:
                week=next(r for r in second.ranking('7d')['rows'] if r['process']=='worker')
                self.assertEqual(sum(s['download'] for s in week['sites']),170)
                clock.advance(8*86400);second.flush()
                self.assertEqual(second.site_details.query(),{})
                self.assertEqual(second.ranking('all')['download'],370)
            finally:second.close()

    def test_legacy_lifetime_not_fabricated_as_period_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=TrafficStore(Path(tmp),clock=Clock(NOW),program_sites_enabled=True)
            try:
                store._add('worker',0,300,'proxy',host='legacy.example',with_slots=False)
                store._add_slot('worker',0,300,'proxy',int(NOW)//SLOT_SECONDS*SLOT_SECONDS)
                all_row=store.ranking('all')['rows'][0]
                self.assertEqual(all_row['sites'][0]['site'],'legacy.example')
                today=store.ranking('today')['rows'][0]
                self.assertEqual(today['sites'],[])
                self.assertEqual(today['download'],300)
                self.assertGreater(today['sites_since'],0)
            finally:store.close()
