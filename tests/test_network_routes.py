import json
import time
from types import SimpleNamespace

from test_server import ServerTestBase
from flashmonitor.app import MonitorApi
from tests.test_network_policy import BROWSER, FLOW, inventory


class NetworkRoutes(ServerTestBase):
    def setUp(self):
        super().setUp()
        self.srv.app = MonitorApi(SimpleNamespace(
            clash=SimpleNamespace(network_inventory=lambda: {**inventory(), 'sampled_at': time.time()}),
            live=SimpleNamespace(get=lambda: {'ts': time.time(), 'flows': [FLOW]})))

    def test_catalog_and_preview_real_http_routes(self):
        code, _, raw = self.request('GET', '/api/network-policy')
        self.assertEqual(code, 200)
        self.assertFalse(json.loads(raw)['capabilities']['apply'])
        code, _, raw = self.request('POST', '/api/network-preview', {'scope': BROWSER, 'target': 'GPT'})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)['state'], 'preview_only')

    def test_auth_and_no_write_route(self):
        for method, path in [('GET', '/api/network-policy'), ('POST', '/api/network-preview')]:
            code, _, _ = self.request(method, path, headers={'X-FM-Token': None})
            self.assertEqual(code, 403)
        for path in ['/api/network-apply', '/api/network-undo']:
            code, _, _ = self.request('POST', path, {})
            self.assertEqual(code, 404)
        code, _, _ = self.request('POST', '/api/network-preview', {'scope': BROWSER, 'target': 'GPT', 'operation': 'apply'})
        self.assertEqual(code, 400)
