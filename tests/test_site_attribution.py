"""Website-to-program attribution must retain its own honest time coverage."""
import tempfile
import unittest
from pathlib import Path

from flashmonitor.store import TrafficStore
from test_store import Clock, NOW, flow


class SiteAttributionTests(unittest.TestCase):
    def test_inverse_programs_keep_route_bytes_and_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            clock=Clock(NOW)
            store=TrafficStore(Path(directory),clock=clock,monotonic=clock,program_sites_enabled=True)
            try:
                store.update([])
                store.update([flow('a','browser',down=100,remote='shared.example:443'),
                              flow('b','editor',down=70,outbound='direct',remote='shared.example:443'),
                              flow('c','hidden',down=30,outbound='unknown',remote='shared.example:443')])
                pack=store.ranking('today')['sites']
                site=pack['rows'][0]
                attribution=pack['program_details'][site['site']]
                self.assertEqual(site['download'],200)
                details={r['process']:r for r in attribution['rows']}
                self.assertEqual(set(details),{'browser','editor'})
                self.assertEqual(details['browser']['proxy_down'],100)
                self.assertEqual(details['editor']['direct_down'],70)
                self.assertEqual(attribution['coverage']['measured_outbounds'],['proxy','direct'])
                store.flush()
            finally:
                store.close()
            reopened=TrafficStore(Path(directory),clock=clock,monotonic=clock,program_sites_enabled=True)
            try:
                self.assertEqual(reopened.ranking('today')['sites']['program_details'][site['site']]['rows'],attribution['rows'])
            finally:
                reopened.close()

    def test_expired_details_are_not_filled_from_lifetime_programs(self):
        with tempfile.TemporaryDirectory() as directory:
            clock=Clock(NOW)
            store=TrafficStore(Path(directory),clock=clock,monotonic=clock,program_sites_enabled=True)
            try:
                store.update([])
                store.update([flow('old','browser',down=200,remote='shared.example:443')])
                store.flush()
                clock.advance(9*86400)
                store.update([])
                store.update([flow('new','editor',down=50,remote='shared.example:443')])
                store.flush()
                pack=store.ranking('all')
                site=pack['sites']['rows'][0]
                attribution=pack['sites']['program_details'][site['site']]
                self.assertEqual(site['download'],250)
                self.assertEqual([(r['process'],r['download']) for r in attribution['rows']],[('editor',50)])
                self.assertTrue(attribution['coverage']['retention_limited'])
                self.assertFalse(attribution['coverage']['lifetime'])
                self.assertGreater(attribution['coverage']['since'],NOW)
            finally:
                store.close()

    def test_disabled_detail_explicitly_reports_empty_attribution(self):
        with tempfile.TemporaryDirectory() as directory:
            store=TrafficStore(Path(directory),program_sites_enabled=False)
            try:
                store.update([])
                store.update([flow('a','browser',down=100,remote='shared.example:443')])
                pack=store.ranking('today')['sites']
                site=pack['rows'][0]
                attribution=pack['program_details'][site['site']]
                self.assertEqual(attribution['rows'],[])
                self.assertFalse(attribution['coverage']['enabled'])
                self.assertEqual(site['download'],100)
            finally:
                store.close()
