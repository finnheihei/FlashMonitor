import copy
import json
import tempfile
import unittest
from pathlib import Path

from flashmonitor.reload_guard import configuration_guard, section
from flashmonitor.rule_drafts import DraftError
from flashmonitor.scoped_ipv6 import overlay


DNS = {'enable': True, 'ipv6': False, 'listen': '127.0.0.1:7874',
       'nameserver-policy': {'+.chatgpt.com': 'https://dns.google/dns-query#RULES'},
       'proxy-server-nameserver': ['https://dns.alidns.com/dns-query']}


class ReloadGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.meta = {'profile_id': '123', 'schema_verified': True}
        self.prefs = {'currentProfileId': 123, 'overrideDns': True, 'patchClashConfig': {'dns': copy.deepcopy(DNS)}}
        self.write_prefs()
        self.cfg = {'dns': copy.deepcopy(DNS), 'hosts': {'example.com': '127.0.0.1'},
                    'rules': ['MATCH,DIRECT'], 'proxies': [{'name': 'node', 'server': '127.0.0.1'}]}

    def write_prefs(self):
        (self.root / 'shared_preferences.json').write_text(json.dumps({'flutter.config': json.dumps(self.prefs)}), encoding='utf-8')

    def guard(self, cfg=None):
        return configuration_guard(self.root, json.dumps(cfg or self.cfg), self.meta)

    def test_corrected_dns_is_preserved_when_only_ipv6_rules_change(self):
        text = json.dumps(self.cfg)
        candidate = overlay(text, [{'scope': {'kind': 'domain', 'value': 'example.com', 'include_subdomains': True}}])
        self.assertEqual(configuration_guard(self.root, candidate, self.meta), self.guard())
        for key in ['dns', 'hosts', 'proxies']:
            self.assertEqual(json.loads(candidate)[key], self.cfg[key])

    def test_stale_generated_dns_and_profile_change_refuse_reload(self):
        stale = copy.deepcopy(self.cfg)
        stale['dns']['nameserver-policy'] = None
        with self.assertRaisesRegex(DraftError, 'DNS 设置尚未同步'):
            self.guard(stale)
        conflicting = copy.deepcopy(self.cfg)
        conflicting['dns']['nameserver-policy']['chatgpt.com'] = 'https://old.example/dns-query'
        with self.assertRaisesRegex(DraftError, 'DNS 设置尚未同步'):
            self.guard(conflicting)
        self.prefs['currentProfileId'] = 456
        self.write_prefs()
        with self.assertRaises(DraftError):
            self.guard()

    def test_opaque_non_rule_change_and_dns_intent_change_are_detected(self):
        before = self.guard()
        for key, value in [('hosts', {}), ('proxies', []), ('sniffer', {'enable': True})]:
            cfg = copy.deepcopy(self.cfg)
            cfg[key] = value
            self.assertNotEqual(self.guard(cfg)['non_rules'], before['non_rules'])
        self.prefs['patchClashConfig']['dns']['listen'] = '127.0.0.1:1053'
        self.write_prefs()
        with self.assertRaises(DraftError):
            self.guard()

    def test_source_dns_checked_when_client_override_is_off(self):
        self.prefs['overrideDns'] = False
        self.write_prefs()
        (self.root / 'profiles').mkdir()
        source = self.root / 'profiles/123.yaml'
        source.write_text(json.dumps({'dns': DNS}), encoding='utf-8')
        self.guard()
        source.write_text(json.dumps({'dns': {**DNS, 'ipv6': True}}), encoding='utf-8')
        with self.assertRaises(DraftError):
            self.guard()

    def test_generated_yaml_dns_nested_maps_lists_and_empty_values(self):
        text = ('dns:\n  enable: true\n  ipv6: false\n  listen: "127.0.0.1:7874"\n'
                '  nameserver-policy:\n    +.chatgpt.com: "https://dns.google/dns-query#RULES"\n'
                '  proxy-server-nameserver:\n    - "https://dns.alidns.com/dns-query"\n'
                'hosts:\n  example.com: "127.0.0.1"\nrules:\n- MATCH,DIRECT\n')
        self.assertEqual(section(text, 'dns'), DNS)
        self.assertEqual(section('dns: ' + json.dumps(DNS) + '\nrules:\n- MATCH,DIRECT\n', 'dns'), DNS)
        before = configuration_guard(self.root, text, self.meta)
        candidate = overlay(text, [{'scope': {'kind':'domain','value':'example.com','include_subdomains':False}}])
        self.assertEqual(configuration_guard(self.root, candidate, self.meta), before)
        self.prefs['patchClashConfig']['dns']['fallback'] = []
        self.write_prefs()
        configuration_guard(self.root, text.replace('  ipv6: false', '  fallback: null\n  ipv6: false'), self.meta)

    def test_unknown_or_duplicate_dns_syntax_is_never_guessed(self):
        for text in ['dns: {}\ndns: {}\nrules:\n- MATCH,DIRECT\n',
                     'dns:\n  enable: true\n  enable: false\nrules:\n- MATCH,DIRECT\n',
                     'dns:\n  nameserver: *shared\nrules:\n- MATCH,DIRECT\n',
                     '{"dns":{},"dns":{},"rules":[]}',
                     'dns: &resolver\n  enable: true\nrules:\n- MATCH,DIRECT\n']:
            with self.subTest(text=text), self.assertRaises(DraftError):
                configuration_guard(self.root, text, self.meta)
