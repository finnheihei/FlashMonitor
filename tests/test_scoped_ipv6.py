import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flashmonitor.app import MonitorApi
from flashmonitor.ipv6_native import canonical_rule
from flashmonitor.network_policy import normalize_scope
from flashmonitor.rule_drafts import DraftError
from flashmonitor.scoped_ipv6 import ScopedIPv6, block_rule, managed_row, overlay, RuntimeController, _check_general
from test_server import ServerTestBase

DOMAIN = {'kind': 'domain', 'value': 'selected.example', 'include_subdomains': False}
APP = {'kind': 'process_path', 'value': r'C:\Apps\browser.exe'}


class Controller:
    def __init__(self, text):
        self.base = 'http://127.0.0.1:19090'
        self.text = text
        self.fail = False
        self.lost_reply = False
        self.foreign = False
        self.calls = []

    def endpoint(self):
        return self.base

    def snapshot(self, base):
        cfg = json.loads(self.text)
        return {'version': {'meta': True, 'version': 'test'},
                'general': {k: cfg[k] for k in ('mode', 'ipv6', 'find-process-mode')},
                'rules': [canonical_rule(r) for r in cfg['rules']]}

    def reload(self, base, text):
        self.calls.append(text)
        if self.fail:
            raise DraftError('核心拒绝加载')
        self.text = text
        if self.foreign:
            data = json.loads(text)
            data['rules'].append('DOMAIN,foreign.example,DIRECT')
            self.text = json.dumps(data)
        if self.lost_reply:
            raise OSError('lost reply')


class ScopedIPv6Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.text = json.dumps({'mode': 'rule', 'ipv6': True, 'find-process-mode': 'always',
                                'rules': ['DOMAIN,selected.example,Proxy', 'MATCH,DIRECT']})
        (self.root / 'config.yaml').write_text(self.text, encoding='utf-8')
        self.controller = Controller(self.text)
        self.meta = {'available': True, 'profile_id': '123', 'source_revision': 'source'}
        self.service = ScopedIPv6(self.root, self.root / 'state.json', self.controller,
                                  lambda scope: True, inspect=lambda: dict(self.meta))

    def action(self, operation='enable', **fields):
        return self.service.operate({'operation': operation, 'revision': self.service._read()['revision'], **fields})

    def test_actual_candidate_apply_idempotent_and_undo_preserves_original(self):
        result = self.action(scope=DOMAIN)
        self.assertTrue(result['entries'][0]['applied'])
        self.assertEqual(json.loads(self.controller.text)['rules'], [block_rule(DOMAIN)] + json.loads(self.text)['rules'])
        self.action(scope=DOMAIN)
        self.assertEqual(len(self.controller.calls), 1)
        self.action('disable', id=result['entries'][0]['id'])
        self.assertEqual(json.loads(self.controller.text), json.loads(self.text))
        self.assertEqual(self.service.catalog()['entries'], [])
        self.assertEqual((self.root / 'config.yaml').read_text(encoding='utf-8'), self.text)

    def test_add_b_remove_b_keeps_a_and_no_original_rule_changes(self):
        first = self.action(scope=DOMAIN)['entries'][0]
        second = self.action(scope={**DOMAIN, 'value': 'other.example'})['entries'][1]
        left = self.action('disable', id=second['id'])['entries']
        self.assertEqual([e['id'] for e in left], [first['id']])
        self.assertEqual(json.loads(self.controller.text)['rules'][1:], json.loads(self.text)['rules'])

    def test_parenthesized_application_path_survives_save_reload_and_restore(self):
        app = {**APP, 'value': r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'}
        entry = self.action(scope=app)['entries'][0]
        self.assertTrue(entry['applied'])
        self.assertEqual(entry['scope'], app)
        self.assertEqual(json.loads(self.controller.text)['rules'][0], block_rule(app))
        self.service = ScopedIPv6(self.root, self.root / 'state.json', self.controller,
                                  inspect=lambda: dict(self.meta))
        self.assertTrue(self.service.catalog()['entries'][0]['applied'])
        self.action('disable', id=entry['id'])
        self.assertEqual(self.controller.text, self.text)
        self.assertEqual(self.service.catalog()['entries'], [])

    def test_unsafe_parenthesized_paths_never_reload_or_persist(self):
        for value in [r'C:\Apps (x86\browser.exe', r'C:\Apps )x86(\browser.exe',
                      r'C:\Apps (x,REJECT)\browser.exe']:
            with self.subTest(value=value), self.assertRaises(DraftError):
                self.action(scope={**APP, 'value': value})
        self.assertEqual(self.controller.calls, [])
        self.assertFalse(self.service.path.exists())

    def test_saved_intent_reapplies_after_client_reload_and_monitor_reopen(self):
        self.action(scope=DOMAIN)
        self.controller.text = self.text
        self.assertFalse(self.service.catalog()['entries'][0]['applied'])
        self.service = ScopedIPv6(self.root, self.root / 'state.json', self.controller,
                                  inspect=lambda: dict(self.meta))
        self.service.tick()
        self.assertTrue(self.service.catalog()['entries'][0]['applied'])

    def test_new_subscription_rules_rebased_for_same_profile(self):
        self.action(scope=DOMAIN)
        data = json.loads(self.text)
        data['rules'].insert(0, 'DOMAIN,new.example,Proxy')
        new = json.dumps(data)
        (self.root / 'config.yaml').write_text(new, encoding='utf-8')
        self.controller.text = new
        self.service.tick()
        self.assertEqual(json.loads(self.controller.text)['rules'][1:], data['rules'])
        self.assertTrue(self.service.catalog()['entries'][0]['applied'])

    def test_profile_or_endpoint_switch_never_applies_old_selection(self):
        self.action(scope=DOMAIN)
        self.controller.text = self.text
        self.meta['profile_id'] = '456'
        self.service.tick()
        self.assertEqual(len(self.controller.calls), 1)
        self.assertFalse(self.service.catalog()['entries'][0]['current'])
        self.meta['profile_id'] = '123'
        self.controller.base = 'http://127.0.0.1:19091'
        self.service.tick()
        self.assertEqual(len(self.controller.calls), 1)

    def test_foreign_runtime_rules_or_general_settings_prevent_overwrite(self):
        cfg = json.loads(self.text)
        cfg['rules'].insert(0, 'DOMAIN,foreign.example,Proxy')
        self.controller.text = json.dumps(cfg)
        with self.assertRaisesRegex(DraftError, '运行规则'):
            self.action(scope=DOMAIN)
        cfg = json.loads(self.text)
        cfg['ipv6'] = False
        self.controller.text = json.dumps(cfg)
        with self.assertRaisesRegex(DraftError, '运行设置'):
            self.action(scope=DOMAIN)
        self.assertEqual(self.controller.calls, [])

    def test_rejected_reload_removes_pending_intent_and_keeps_old_rule(self):
        self.action(scope=DOMAIN)
        before = self.controller.text
        self.controller.fail = True
        with self.assertRaisesRegex(DraftError, '拒绝'):
            self.action(scope={**DOMAIN, 'value': 'other.example'})
        self.assertEqual(self.controller.text, before)
        self.assertEqual(len(self.service.catalog()['entries']), 1)
        self.assertFalse(any(r.get('pending') for r in self.service._read()['profiles'].values()))

    def test_lost_reply_verified_without_duplicate_reload(self):
        self.controller.lost_reply = True
        self.assertTrue(self.action(scope=DOMAIN)['entries'][0]['applied'])
        self.service.tick()
        self.assertEqual(len(self.controller.calls), 1)

    def test_pending_after_crash_resolves_without_second_write(self):
        original_save = self.service._save
        calls = []

        def fail_after_reload(state):
            calls.append(1)
            if len(calls) > 1:
                raise OSError('disk unavailable')
            return original_save(state)

        with patch.object(self.service, '_save', side_effect=fail_after_reload):
            # The reload already happened; journal must survive completion failure.
            with patch('flashmonitor.scoped_ipv6.time.monotonic', side_effect=[0, 4]):
                with self.assertRaises(DraftError):
                    self.action(scope=DOMAIN)
        self.assertTrue(next(iter(self.service._read()['profiles'].values()))['pending'])
        result = self.action('reconcile')
        self.assertTrue(result['entries'][0]['applied'])
        self.assertEqual(len(self.controller.calls), 1)

    def test_stale_revision_bad_scope_and_forged_fields_rejected_before_write(self):
        self.action(scope=DOMAIN)
        for body in [
            {'operation': 'enable', 'revision': 0, 'scope': DOMAIN},
            {'operation': 'enable', 'revision': self.service._read()['revision'], 'scope': {'kind': 'all', 'children': []}},
            {'operation': 'enable', 'revision': self.service._read()['revision'], 'scope': DOMAIN, 'allow_reload': True},
        ]:
            with self.assertRaises(DraftError):
                self.service.operate(body)
        self.assertEqual(len(self.controller.calls), 1)

    def test_identity_failure_cannot_widen_to_process_name(self):
        self.service.available = lambda scope: False
        with self.assertRaisesRegex(DraftError, '身份'):
            self.action(scope=APP)
        self.assertEqual(self.controller.calls, [])

    def test_corrupt_store_preserved_and_no_runtime_write(self):
        self.service.path.write_text('{broken', encoding='utf-8')
        with self.assertRaises(DraftError):
            self.action(scope=DOMAIN)
        self.assertEqual(self.service.path.read_text(), '{broken')
        self.assertEqual(self.controller.calls, [])

    def test_source_change_during_preflight_refused(self):
        original = self.service._context
        calls = []

        def changing():
            result = original()
            calls.append(1)
            if len(calls) == 1:
                self.meta['source_revision'] = 'changed'
            return result

        with patch.object(self.service, '_context', side_effect=changing):
            with self.assertRaisesRegex(DraftError, '正在变化'):
                self.action(scope=DOMAIN)
        self.assertEqual(self.controller.calls, [])

    def test_dns_change_after_journal_write_cannot_send_stale_candidate(self):
        save = self.service._save
        def save_and_change(state):
            save(state)
            cfg = json.loads(self.text)
            cfg['dns'] = {'enable': True, 'nameserver-policy': {'+.chatgpt.com': 'https://dns.google/dns-query#RULES'}}
            (self.root / 'config.yaml').write_text(json.dumps(cfg), encoding='utf-8')
        with patch.object(self.service, '_save', side_effect=save_and_change):
            with self.assertRaisesRegex(DraftError, '正在变化'):
                self.action(scope=DOMAIN)
        self.assertEqual(self.controller.calls, [])

    def test_dns_change_during_reload_is_not_reported_as_success(self):
        reload = self.controller.reload
        def reload_and_change(base, text):
            reload(base, text)
            cfg = json.loads(self.text)
            cfg['dns'] = {'enable': True, 'nameserver-policy': {'+.chatgpt.com': 'https://dns.google/dns-query#RULES'}}
            (self.root / 'config.yaml').write_text(json.dumps(cfg), encoding='utf-8')
        with patch.object(self.controller, 'reload', side_effect=reload_and_change):
            with self.assertRaisesRegex(DraftError, 'DNS 或其他配置已变化'):
                self.action(scope=DOMAIN)
        self.assertTrue(next(iter(self.service._read()['profiles'].values()))['pending'])

    def test_repaired_dns_and_routes_survive_enable_reconcile_and_disable(self):
        cfg = json.loads(self.text)
        cfg['dns'] = {'enable': True, 'nameserver-policy': {'+.chatgpt.com': 'https://dns.google/dns-query#RULES'}}
        self.text = json.dumps(cfg)
        self.controller.text = self.text
        (self.root / 'config.yaml').write_text(self.text, encoding='utf-8')
        entry = self.action(scope=DOMAIN)['entries'][0]
        self.controller.text = self.text
        self.service.tick()
        self.assertTrue(self.service.catalog()['entries'][0]['applied'])
        self.action('disable', id=entry['id'])
        for candidate in self.controller.calls:
            self.assertEqual(json.loads(candidate)['dns'], cfg['dns'])
            self.assertEqual(json.loads(candidate)['rules'][-2:], cfg['rules'])

    def test_same_rule_from_client_not_owned(self):
        cfg = json.loads(self.text)
        cfg['rules'].insert(0, block_rule(DOMAIN))
        text = json.dumps(cfg)
        self.controller.text = text
        (self.root / 'config.yaml').write_text(text, encoding='utf-8')
        with self.assertRaisesRegex(DraftError, '同文规则'):
            self.action(scope=DOMAIN)
        self.assertEqual(self.controller.calls, [])

    def test_yaml_only_rule_insertion(self):
        text = 'mode: rule\r\nipv6: true\r\nrules:\r\n  - MATCH,DIRECT\r\ndns:\r\n  enable: true\r\n'
        entries = [{'scope': DOMAIN}]
        candidate = overlay(text, entries)
        self.assertEqual(candidate.replace('  - ' + json.dumps(block_rule(DOMAIN)) + '\r\n', ''), text)

    def test_tun_stack_case_alias_and_real_changes(self):
        _check_general('tun:\n  enable: true\n  stack: "mixed"\n', {'tun': {'enable': True, 'stack': 'Mixed'}})
        with self.assertRaisesRegex(DraftError, 'TUN'):
            _check_general('tun:\n  enable: true\n', {'tun': {'enable': False}})

    def test_idle_reconcile_does_not_change_revision_or_write_state(self):
        self.action(scope=DOMAIN)
        before = self.service.path.read_bytes()
        self.service.tick()
        self.service.tick()
        self.assertEqual(self.service.path.read_bytes(), before)
        self.assertEqual(len(self.controller.calls), 1)

    def test_remote_controller_and_nonlocal_redirect_inputs_rejected(self):
        for base in ['http://example.com:9090', 'http://127.0.0.1:9090/path', 'http://user@127.0.0.1:9090']:
            client = SimpleNamespace(base_urls=lambda: [base])
            with self.assertRaises(DraftError):
                RuntimeController(client).endpoint()

    def test_managed_single_and_nested_rule_encoding(self):
        self.assertEqual(canonical_rule(block_rule(DOMAIN)), managed_row(DOMAIN))
        scope = normalize_scope({'kind': 'all', 'children': [APP, DOMAIN]})
        self.assertIn('(and,(', managed_row(scope)[1])


class ScopedIPv6Routes(ServerTestBase):
    def setUp(self):
        super().setUp()
        self.calls = []
        service = SimpleNamespace(catalog=lambda: {'entries': []}, operate=self.action)
        self.srv.app = MonitorApi(SimpleNamespace(scoped_ipv6=service, stop=threading.Event()))

    def action(self, body):
        self.calls.append(copy.deepcopy(body))
        if body.get('operation') != 'enable':
            raise DraftError('invalid')
        return {'entries': [], 'supported': True}

    def test_routes_auth_csrf_and_validation(self):
        self.assertEqual(self.get_json('/api/scoped-ipv6')[0], 200)
        self.assertEqual(self.post_json('/api/scoped-ipv6', {'operation': 'enable'})[0], 200)
        self.assertEqual(self.post_json('/api/scoped-ipv6', {'operation': 'invalid'})[0], 400)
        for method in ['GET', 'POST']:
            self.assertEqual(self.request(method, '/api/scoped-ipv6', headers={'X-FM-Token': None})[0], 403)
        self.assertEqual(self.request('POST', '/api/scoped-ipv6', {}, {'Origin': 'https://foreign.example'})[0], 403)
        self.assertEqual(len(self.calls), 2)

    def test_catalog_targets_are_independent_of_diagnostic_draft_or_inventory(self):
        self.srv.app.ctx.live = SimpleNamespace(get=lambda: {'ts': time.time(), 'flows': [
            {'proc_path': APP['value'], 'process': 'browser', 'title': 'Browser', 'identity_verified': True},
            {'proc_path': r'C:\Unverified\other.exe', 'process': 'other', 'identity_verified': False},
            {'proc_path': APP['value'], 'process': 'conflict', 'identity_verified': True, 'identity_conflict': True},
        ]})
        code, catalog = self.get_json('/api/scoped-ipv6')
        self.assertEqual(code, 200)
        self.assertEqual(catalog['applications'], [{'path': APP['value'], 'process': 'browser', 'label': 'Browser'}])
        # Context intentionally has neither settings nor diagnostic controller.

    def test_target_path_can_be_verified_locally_and_stale_flows_are_not_guessed(self):
        self.srv.app.ctx.live = SimpleNamespace(get=lambda: {'ts': time.time(), 'flows': [
            {'proc_path': APP['value'], 'process': 'browser', 'identity_verified': False}]})
        self.srv.app.ctx._ipv6_subject_available = lambda subject: subject['value'] == APP['value']
        self.assertEqual(self.get_json('/api/scoped-ipv6')[1]['applications'][0]['path'], APP['value'])
        self.srv.app.ctx.live = SimpleNamespace(get=lambda: {'ts': time.time()-20, 'flows': [
            {'proc_path': APP['value'], 'process': 'browser', 'identity_verified': True}]})
        self.assertEqual(self.get_json('/api/scoped-ipv6')[1]['applications'], [])
