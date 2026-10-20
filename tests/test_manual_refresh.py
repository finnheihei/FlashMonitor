"""Fresh controller reads and sampling order; only temporary stores/local HTTP."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from flashmonitor.app import MonitorApi
from flashmonitor.clash import ClashClient
from flashmonitor.rule_drafts import DraftError
from tests import test_collector as fixtures
from tests.test_network_policy import inventory
from tests.test_server import ServerTestBase


class RefreshTest(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.CollectTest('test_without_clash_uses_connection_table')
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.payload={'connections':[fixtures.clash_conn('fresh','chrome','fresh.example','HK',10,100)],'uploadTotal':10,'downloadTotal':100}
        self.collector=self.fixture.collector(self.payload)
        self.collector.clash.invalidate_config_cache=Mock()
        self.collector.clash.publish=Mock()
        self.collector.clash.network_inventory=Mock(return_value={**inventory(),'targets':{'GPT':{},'HK':{},'DIRECT':{}}})
        self.ctx=SimpleNamespace(collector=self.collector,store=self.fixture.store,ranks=self.collector.ranks,
            stop=threading.Event(),settings=SimpleNamespace(ui_prefs=lambda:{}),subinfo=SimpleNamespace(public=lambda:{}))
        self.api=MonitorApi(self.ctx)

    def test_forced_sample_bypasses_cache_and_counts_only_deltas(self):
        self.collector.clash.recent_payload=Mock(return_value={'connections':[]})
        self.collector.clash.sampled_recently=lambda:True
        self.collector.ranks.get('all')
        first=self.api.refresh_data({'period':'7d'})
        self.assertTrue(first['ok']);self.assertEqual(first['snapshot']['rank']['all']['download'],0)
        self.assertIn('7d',first['snapshot']['rank']);self.assertEqual(first['catalog']['inventory']['nodes'],['HK'])
        self.assertFalse(self.collector.clash.recent_payload.called)
        self.collector.clash.invalidate_config_cache.assert_called_once()
        self.collector.clash.publish.assert_called_once_with(self.payload)
        again=self.api.refresh_data({'period':'7d'})
        self.assertEqual(again['snapshot']['rank']['all']['download'],0)
        self.payload['connections'][0]['download']=202
        delta=self.api.refresh_data({'period':'all'})
        self.assertEqual(delta['snapshot']['rank']['all']['download'],102)
        self.assertTrue(delta['snapshot']['manual_refresh_supported'])

    def test_inventory_failure_preserves_live_and_retry_releases_lock(self):
        previous={'flows':[{'id':'previous'}]};self.collector.live.set(previous)
        self.collector.clash.network_inventory.return_value={'available':False}
        with patch('flashmonitor.app.fmlog.log_exception'):
            result=self.api.refresh_data({'period':'all'})
        self.assertFalse(result['ok']);self.assertIs(self.collector.live.get(),previous)
        self.collector.clash.network_inventory.return_value=inventory()
        self.assertTrue(self.api.refresh_data({'period':'all'})['ok'])

    def test_connection_failure_preserves_live(self):
        previous={'flows':[{'id':'previous'}]};self.collector.live.set(previous)
        self.collector.clash._payload=None
        with patch('flashmonitor.app.fmlog.log_exception'):
            self.assertFalse(self.api.refresh_data({'period':'all'})['ok'])
        self.assertIs(self.collector.live.get(),previous)
        self.collector.clash.network_inventory.assert_not_called()

    def test_busy_single_flight_and_shutdown(self):
        entered=threading.Event();release=threading.Event();original=self.collector.refresh
        def slow():
            entered.set();release.wait(3);return original()
        self.collector.refresh=Mock(side_effect=slow);results=[]
        t=threading.Thread(target=lambda:results.append(self.api.refresh_data({'period':'all'})));t.start()
        self.assertTrue(entered.wait(2))
        self.assertTrue(self.api.refresh_data({'period':'all'})['busy']);self.assertEqual(self.collector.refresh.call_count,1)
        release.set();t.join(4);self.assertFalse(t.is_alive());self.assertTrue(results[0]['ok'])
        self.ctx.stop.set();self.assertFalse(self.api.refresh_data({'period':'all'})['ok'])
        self.assertEqual(self.collector.refresh.call_count,1)

    def test_refresh_and_normal_sample_are_serialized(self):
        entered=threading.Event();release=threading.Event();published=[];original=self.collector._collect
        def slow(*,fresh=False):
            published.append('refresh' if fresh else 'normal')
            if fresh:entered.set();release.wait(3)
            return original(fresh=fresh)
        self.collector._collect=slow
        t=threading.Thread(target=self.collector.refresh);t.start();self.assertTrue(entered.wait(2))
        normal=threading.Thread(target=self.collector.collect);normal.start()
        self.assertFalse(self.collector._sample_lock.acquire(blocking=False))
        self.assertEqual(published,['refresh']);release.set();t.join(4);normal.join(4)
        self.assertEqual(published,['refresh','normal'])

    def test_poller_error_publication_cannot_overwrite_a_newer_refresh(self):
        entered=threading.Event();release=threading.Event();stop=threading.Event()
        def fail():
            stop.set();raise RuntimeError('synthetic old poll error')
        def slow_log(*args):
            entered.set();release.wait(3)
        self.collector.collect=fail
        with patch('flashmonitor.collector.fmlog.log_exception',side_effect=slow_log):
            poller=threading.Thread(target=lambda:self.collector.poller(stop));poller.start()
            self.assertTrue(entered.wait(2))
            refresh=threading.Thread(target=self.collector.refresh);refresh.start()
            self.assertFalse(self.collector._sample_lock.acquire(blocking=False))
            release.set();poller.join(4);refresh.join(4)
        self.assertFalse(poller.is_alive());self.assertFalse(refresh.is_alive())
        self.assertNotIn('error',self.collector.live.get())

    def test_strict_parameters_and_no_reset_or_controller_writer(self):
        for body in ({},{'period':'bad'},{'period':'all','reset':True},{'period':[]}):
            with self.subTest(body=body),self.assertRaises(DraftError):self.api.refresh_data(body)
        with patch.object(self.fixture.store,'reset',side_effect=AssertionError('reset forbidden')),patch.object(self.fixture.store,'flush',side_effect=AssertionError('flush forbidden')):
            self.assertTrue(self.api.refresh_data({'period':'all'})['ok'])

    def test_config_cache_is_invalidated_without_write(self):
        client=ClashClient(lambda:'http://127.0.0.1:19090',lambda:'',fallbacks=())
        with patch.object(client,'_get_json',side_effect=[{'mixed-port':7890},{'mixed-port':7891}]) as get:
            self.assertEqual(client.configs()['mixed_ports'],{7890});client.configs();self.assertEqual(get.call_count,1)
            client.invalidate_config_cache();self.assertEqual(client.configs()['mixed_ports'],{7891});self.assertEqual(get.call_count,2)


class RefreshRouteTest(ServerTestBase):
    def setUp(self):
        super().setUp()
        def refresh(body):
            if body!={'period':'all'}:raise DraftError('bad period')
            self.app.calls.append(('refresh',body));return {'ok':True,'snapshot':{}}
        self.app.refresh_data=refresh

    def test_authenticated_post_and_malformed_input(self):
        self.assertEqual(self.post_json('/api/refresh',{'period':'all'})[0],200)
        self.assertEqual(self.app.calls,[('refresh',{'period':'all'})])
        self.assertEqual(self.post_json('/api/refresh',{'period':'bad'})[0],400)
        self.assertEqual(self.post_json('/api/refresh',[])[0],400)
        self.assertEqual(self.request('GET','/api/refresh')[0],404)

    def test_token_and_origin_are_required(self):
        for headers in ({'X-FM-Token':None},{'Origin':'https://evil.example'}):
            self.assertEqual(self.request('POST','/api/refresh',{'period':'all'},headers)[0],403)
        self.assertEqual(self.app.calls,[])
