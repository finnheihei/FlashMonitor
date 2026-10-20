"""Release regressions for reliable route and traffic attribution (no live controller)."""
import csv
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from flashmonitor.clash import ClashClient
from flashmonitor.classify import clash_to_flow, host_of
from flashmonitor.collector import export_csv
from flashmonitor.store import TrafficStore


class ReleaseReadinessTest(unittest.TestCase):
    def test_error_object_is_not_an_empty_connection_snapshot(self):
        client = ClashClient(lambda: 'http://127.0.0.1:19090', lambda: '', fallbacks=())
        client._get_json = Mock(return_value={'message': 'temporarily unavailable'})
        self.assertIsNone(client.payload())
        self.assertEqual(client.base_ok, '')

    def test_explicit_empty_or_null_connections_remain_valid(self):
        client = ClashClient(lambda: 'http://127.0.0.1:19090', lambda: '', fallbacks=())
        for connections in ([], None):
            client._get_json = Mock(return_value={'connections': connections, 'uploadTotal': 0, 'downloadTotal': 0})
            self.assertEqual(client.payload()['connections'], [])

    def test_error_response_then_recovery_does_not_count_connection_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TrafficStore(Path(directory))
            try:
                store.update([])
                client = ClashClient(lambda: 'http://127.0.0.1:19090', lambda: '', fallbacks=())
                conn = {'id': 'one', 'metadata': {'process': 'app.exe', 'host': 'example.com'}, 'chains': ['node']}
                client._get_json = Mock(side_effect=[
                    {'connections': [{**conn, 'download': 100}]},
                    {'message': 'temporarily unavailable'},
                    {'connections': [{**conn, 'download': 120}]},
                ])
                for _ in range(3):
                    payload = client.payload()
                    if payload is not None:
                        store.update([clash_to_flow(row) for row in payload['connections']])
                self.assertEqual(store.ranking('all')['download'], 120)
                self.assertEqual(store.recent_closed(), [])
            finally:
                store.close()

    def test_program_site_summary_keeps_proxy_leader_before_and_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TrafficStore(Path(directory))
            try:
                store.update([])
                flows = [{'id': str(i), 'process': 'browser', 'remote': f'host-{i}.example:443',
                          'download': 10000 + i, 'outbound': 'direct'} for i in range(60)]
                flows.append({'id': 'proxy', 'process': 'browser', 'remote': 'proxy-leader.example:443',
                              'download': 9000, 'outbound': 'proxy'})
                store.update(flows)
                self.assertIn('proxy-leader.example', [row['site'] for row in store.ranking('all')['rows'][0]['sites']])
                store.close()
                store = TrafficStore(Path(directory))
                row = store.ranking('all')['rows'][0]
                self.assertIn('proxy-leader.example', [site['site'] for site in row['sites']])
                self.assertEqual(row['proxy_total'], 9000)
            finally:
                store.close()

    def test_invalid_leaf_is_not_classified_as_a_proxy_node(self):
        for leaf in (None, 123, {}, False):
            with self.subTest(leaf=leaf):
                flow = clash_to_flow({'chains': [leaf, 'DIRECT']})
                self.assertEqual(flow['outbound'], 'unknown')
                self.assertEqual(flow['node'], '')

    def test_ipv6_destination_preserves_address_without_port_in_site(self):
        flow = clash_to_flow({'metadata': {'destinationIP': '2001:db8::1', 'destinationPort': '443'}})
        self.assertEqual(flow['remote'], '[2001:db8::1]:443')
        self.assertEqual(host_of(flow['remote']), '2001:db8::1')

    def test_proxy_leader_survives_large_direct_only_sites(self):
        sites = {f'direct-{i}.example': {'direct_down': 10000 + i} for i in range(100)}
        sites['proxy-leader.example'] = {'proxy_down': 9000}
        rows = TrafficStore._site_rows(sites)['rows']
        proxy_rows = sorted(rows, key=lambda row: row['proxy_total'], reverse=True)
        self.assertEqual(proxy_rows[0]['site'], 'proxy-leader.example')
        self.assertEqual(proxy_rows[0]['proxy_total'], 9000)

    def test_direct_leader_survives_large_proxy_only_sites(self):
        sites = {f'proxy-{i}.example': {'proxy_down': 10000 + i} for i in range(100)}
        sites['direct-leader.example'] = {'direct_down': 9000}
        rows = TrafficStore._site_rows(sites)['rows']
        self.assertIn('direct-leader.example', [row['site'] for row in rows])

    def test_site_summary_stays_bounded_and_unique(self):
        sites = {f'host-{i}.example': {'proxy_down': i, 'direct_down': 1000 - i} for i in range(1000)}
        pack = TrafficStore._site_rows(sites)
        self.assertLessEqual(len(pack['rows']), 240)
        self.assertEqual(len({row['site'] for row in pack['rows']}), len(pack['rows']))
        self.assertEqual(pack['download'], 1000000)

    def test_backend_csv_escapes_formula_cells(self):
        lanes = {'proxy': {'rows': [{'process': '=1+1', 'upload': 1, 'download': 2,
                                    'sites': [{'site': '@bad', 'upload': 1, 'download': 2}]}]}}
        rows = list(csv.reader(io.StringIO(export_csv(lanes).decode('utf-8-sig'))))
        self.assertEqual(rows[1][1], "'=1+1")
        self.assertEqual(rows[2][2], "'@bad")
        self.assertEqual(rows[1][-2:], ['1', '2'])


if __name__ == '__main__':
    unittest.main()
