import copy
import json
import unittest
from urllib.request import Request

from flashmonitor.network_policy import (normalize_scope, scope_matches, condition, parse_condition,
    rule_matches, ordered_rules, trace_rules, preview, evidence_state)
from flashmonitor.network_inventory import collect_inventory, NetworkPolicyService, NoRedirect, pinned_reader
from flashmonitor.rule_drafts import DraftError
from flashmonitor.classify import clash_to_flow

APP = {'kind': 'process_path', 'value': r'C:\Apps\Browser\browser.exe'}
DOMAIN = {'kind': 'domain', 'value': 'chatgpt.com', 'include_subdomains': False}
BROWSER = {'kind': 'all', 'children': [APP, DOMAIN]}
FLOW = {'process': 'browser', 'proc_path': APP['value'], 'identity_verified': True,
        'host': 'chatgpt.com', 'network': 'tcp', 'outbound': 'proxy', 'destination_ip': '203.0.113.1', 'destination_port': 443}


def rule(kind, payload, target):
    return {'type': kind, 'payload': payload, 'proxy': target}


def inventory():
    return {'available': True, 'consistent': True, 'sampled_at': 100, 'revision': 'r1', 'mode': 'rule',
            'groups': {'GPT': {'type': 'Selector', 'members': ['node'], 'current': 'node', 'may_direct': False}},
            'targets': {'GPT': {'udp': True}},
            'rules': [rule('DomainSuffix', 'chatgpt.com', 'GPT'), rule('Match', '', 'DIRECT')]}


class ScopeTests(unittest.TestCase):
    def test_browser_requires_both_conditions(self):
        scope = normalize_scope(BROWSER)
        self.assertTrue(scope_matches(scope, FLOW))
        self.assertFalse(scope_matches(scope, {**FLOW, 'host': 'other.example'}))
        self.assertFalse(scope_matches(scope, {**FLOW, 'proc_path': r'C:\Other\browser.exe'}))
        self.assertIsNone(scope_matches(scope, {**FLOW, 'identity_verified': False}))

    def test_exact_suffix_boundary_and_case(self):
        exact = normalize_scope(DOMAIN)
        suffix = normalize_scope({**DOMAIN, 'include_subdomains': True})
        self.assertFalse(scope_matches(exact, {**FLOW, 'host': 'sub.chatgpt.com'}))
        self.assertTrue(scope_matches(suffix, {**FLOW, 'host': 'SUB.CHATGPT.COM.'}))
        self.assertFalse(scope_matches(suffix, {**FLOW, 'host': 'evilchatgpt.com'}))
        self.assertEqual(normalize_scope({'kind': 'domain', 'value': 'CHATGPT.COM.'})['value'], 'chatgpt.com')

    def test_path_is_full_identity_not_filename(self):
        scope = normalize_scope(APP)
        self.assertTrue(scope_matches(scope, {**FLOW, 'proc_path': r'c:\apps\browser\BROWSER.EXE'}))
        self.assertFalse(scope_matches(scope, {**FLOW, 'proc_path': r'D:\Apps\Browser\browser.exe'}))
        self.assertIsNone(scope_matches(scope, {**FLOW, 'identity_conflict': True}))

    def test_no_empty_or_expanding_scope(self):
        for raw in [{'kind': 'any', 'children': []}, {'kind': 'process_name', 'value': 'browser.exe'},
                    {'kind': 'not', 'children': [APP]}, {**APP, 'verified': True},
                    {**DOMAIN, 'include_subdomains': 'false'}]:
            with self.subTest(raw=raw), self.assertRaises(DraftError):
                normalize_scope(raw)

    def test_balanced_parentheses_in_application_paths_roundtrip_nested_rules(self):
        app = {**APP, 'value': r'C:\Program Files (x86)\Browser (Stable)\browser.exe'}
        scope = normalize_scope({'kind': 'all', 'children': [app, DOMAIN]})
        parsed = parse_condition(condition(scope))
        self.assertTrue(rule_matches(parsed, {**FLOW, 'proc_path': app['value']}))
        self.assertFalse(rule_matches(parsed, {**FLOW, 'proc_path': APP['value']}))
        for value in [r'C:\Apps (x86\browser.exe', r'C:\Apps)x86(\browser.exe',
                      r'C:\Apps (x,REJECT)\browser.exe', r'C:\Apps ("x86")\browser.exe',
                      r'C:\Apps (\..\x86)\browser.exe']:
            with self.subTest(value=value), self.assertRaises(DraftError):
                normalize_scope({**APP, 'value': value})

    def test_injection_path_domain_and_size(self):
        for value in ['relative.exe', 'C:relative.exe', r'\\.\device', 'C:\\x,REJECT.exe', 'C:\\x\n.exe', 'C:\\*.exe']:
            with self.subTest(value=value), self.assertRaises(DraftError):
                normalize_scope({**APP, 'value': value})
        for value in ['https://chatgpt.com', 'chatgpt.com:443', '127.0.0.1', '*.chatgpt.com', 'a\nb.com', 'a.com,REJECT']:
            with self.subTest(value=value), self.assertRaises(DraftError):
                normalize_scope({**DOMAIN, 'value': value})
        with self.assertRaises(DraftError):
            normalize_scope({'kind': 'any', 'children': [DOMAIN] * 65})

    def test_nested_logic_roundtrips_diagnostic_parser(self):
        scope = normalize_scope({'kind': 'all', 'children': [APP, {'kind': 'any', 'children': [DOMAIN, {'kind': 'domain', 'value': 'api.openai.com'}]}]})
        parsed = parse_condition(condition(scope))
        for host, expected in [('chatgpt.com', True), ('api.openai.com', True), ('evil.example', False)]:
            self.assertEqual(rule_matches(parsed, {**FLOW, 'host': host}), expected)


class RuleTests(unittest.TestCase):
    def test_nested_not_and_camelcase_inventory(self):
        rows = ordered_rules([rule('AND', '((PROCESS-PATH,' + APP['value'] + '),(NOT,((DOMAIN,other.example))))', 'GPT')])
        self.assertTrue(rule_matches(rows[0]['condition'], FLOW))
        self.assertFalse(rule_matches(rows[0]['condition'], {**FLOW, 'host': 'other.example'}))

    def test_unknown_rule_before_match_prevents_false_certainty(self):
        for kind, payload in [('GeoSite', 'openai'), ('RuleSet', 'custom'), ('Unsupported', 'x'), ('AND', 'broken')]:
            result = trace_rules(ordered_rules([rule(kind, payload, 'GPT'), rule('Match', '', 'DIRECT')]), FLOW, {})
            self.assertEqual(result['certainty'], 'unknown')
            self.assertEqual(result['unknown_before'], [0])

    def test_unknown_later_does_not_hide_known_first_match(self):
        result = trace_rules(ordered_rules([rule('Domain', 'chatgpt.com', 'GPT'), rule('GeoSite', 'openai', 'DIRECT')]), FLOW, {})
        self.assertEqual((result['certainty'], result['index']), ('observed_metadata_match', 0))

    def test_udp_disabled_falls_through_not_leak_free(self):
        rules = ordered_rules([rule('Domain', 'chatgpt.com', 'GPT'), rule('Match', '', 'DIRECT')])
        trace = trace_rules(rules, {**FLOW, 'network': 'udp'}, {'GPT': {'udp': False}})
        self.assertEqual((trace['target'], trace['skipped']), ('DIRECT', [0]))
        self.assertEqual(trace_rules(rules, {**FLOW, 'network': 'udp'}, {})['certainty'], 'unknown')

    def test_pass_continues(self):
        trace = trace_rules(ordered_rules([rule('Match', '', 'PASS'), rule('Match', '', 'REJECT')]), FLOW, {})
        self.assertEqual((trace['target'], trace['skipped']), ('REJECT', [0]))

    def test_unknown_protocol_cannot_certify_udp_limited_target(self):
        rules = ordered_rules([rule('Domain', 'chatgpt.com', 'GPT'), rule('Match', '', 'DIRECT')])
        for network in ['', None, 'invalid', True]:
            for udp in [False, None]:
                with self.subTest(network=network, udp=udp):
                    trace = trace_rules(rules, {**FLOW, 'network': network}, {'GPT': {'udp': udp}})
                    self.assertEqual((trace['certainty'], trace['target'], trace['unknown_before']), ('unknown', 'DIRECT', [0]))
                    self.assertIsNone(rule_matches(parse_condition('NETWORK,udp'), {**FLOW, 'network': network}))
        missing = dict(FLOW); missing.pop('network')
        self.assertEqual(trace_rules(rules, missing, {})['certainty'], 'unknown')
        self.assertEqual(trace_rules(rules, FLOW, {'GPT': {'udp': False}})['certainty'], 'observed_metadata_match')
        self.assertEqual(trace_rules(rules, missing, {'GPT': {'udp': True}})['target'], 'GPT')

    def test_real_connection_port_controls_quic_guard(self):
        guard = parse_condition('AND,((DOMAIN,chatgpt.com),(NETWORK,udp),(DST-PORT,443))')
        for port, expected in [(443, True), ('443', True), (8443, False), (None, None), ('', None),
                               (True, None), ('443.0', None), (65536, None), (0, None)]:
            with self.subTest(port=port):
                converted = clash_to_flow({'metadata': {'host': 'chatgpt.com', 'network': 'udp', 'destinationPort': port}})
                self.assertIs(rule_matches(guard, converted), expected)
        converted = clash_to_flow({'metadata': {'host': 'chatgpt.com', 'network': 'udp'}})
        self.assertIsNone(rule_matches(guard, converted))
        converted = clash_to_flow({'metadata': {'host': 'chatgpt.com', 'network': 'tcp', 'destinationPort': 443}})
        self.assertFalse(rule_matches(guard, converted))

    def test_conflicting_process_name_is_unknown(self):
        node = parse_condition('PROCESS-NAME,browser.exe')
        flow = {**FLOW, 'rule_exe': 'browser.exe'}
        self.assertTrue(rule_matches(node, flow))
        self.assertIsNone(rule_matches(node, {**flow, 'identity_conflict': True}))
        self.assertFalse(rule_matches(node, {**flow, 'rule_exe': 'other.exe'}))

    def test_ipv6_and_fake_ip_are_distinct(self):
        node = parse_condition('IP-CIDR6,::/0,no-resolve')
        self.assertTrue(rule_matches(node, {**FLOW, 'destination_ip': '2001:db8::1'}))
        self.assertFalse(rule_matches(node, FLOW))
        self.assertIsNone(rule_matches(node, {**FLOW, 'destination_ip': '198.18.1.1'}))

    def test_malformed_deep_large_rules_are_unknown_or_rejected(self):
        self.assertEqual(parse_condition('AND,((DOMAIN,a.com)')['type'], 'UNKNOWN')
        self.assertEqual(parse_condition('x'*8193)['type'], 'UNKNOWN')
        with self.assertRaises(DraftError):
            ordered_rules([rule('Match', '', 'DIRECT')] * 10001)


class PreviewTests(unittest.TestCase):
    def request(self, **extra):
        return {'scope': BROWSER, 'target': 'GPT', **extra}

    def test_preview_is_read_only_and_does_not_mutate_inputs(self):
        inv = inventory(); original = copy.deepcopy(inv)
        p = preview(self.request(), inv, [FLOW], now=100)
        self.assertEqual(inv, original)
        self.assertEqual(p['state'], 'preview_only')
        self.assertEqual(p['evidence'], {'persisted': False, 'loaded': False, 'connection_verified': False})
        self.assertFalse(p['capabilities']['apply'])
        self.assertEqual(len(p['proposed_rules']), 1)

    def test_guards_are_scoped_ordered_and_off_by_default(self):
        p = preview(self.request(block_ipv6=True, block_quic=True), inventory(), [FLOW], now=100)
        self.assertEqual(len(p['proposed_rules']), 3)
        self.assertIn('IP-CIDR6,::/0,no-resolve', p['proposed_rules'][0])
        self.assertIn('(NETWORK,udp),(DST-PORT,443)', p['proposed_rules'][1])
        for line in p['proposed_rules']:
            self.assertIn('DOMAIN,chatgpt.com', line); self.assertIn('PROCESS-PATH,' + APP['value'], line)
        for line in p['proposed_rules'][:2]:
            self.assertFalse(rule_matches(parse_condition(line.rsplit(',', 1)[0]), {**FLOW, 'host': 'other.example', 'destination_ip': '2001:db8::1', 'network': 'udp'}))

    def test_arbitrary_writes_and_forged_capabilities_rejected(self):
        for extra in [{'operation': 'apply'}, {'capabilities': {'apply': True}}, {'dns': {}}, {'force_ipv4': True}, {'block_ipv6': 'yes'}]:
            with self.subTest(extra=extra), self.assertRaises(DraftError):
                preview(self.request(**extra), inventory(), [FLOW], now=100)

    def test_unavailable_or_invalid_target_rejected(self):
        for target in ['DIRECT', 'GLOBAL', 'missing', 'GPT,REJECT', 'GPT\n']:
            with self.subTest(target=target), self.assertRaises(DraftError):
                preview({**self.request(), 'target': target}, inventory(), [FLOW], now=100)
        with self.assertRaises(DraftError):
            preview(self.request(), {'available': False}, [FLOW], now=100)

    def test_stale_changed_wrong_mode_and_unverified_identity_blocked(self):
        inv = inventory(); inv.update(consistent=False, mode='global', sampled_at=0)
        p = preview(self.request(), inv, [], now=100)
        self.assertEqual(set(p['blockers']), {'native_writer_unavailable', 'not_rule_mode', 'inventory_stale_or_changed', 'process_identity_unverified'})

    def test_mixed_domain_flags_and_selection_stable(self):
        scope = {'kind': 'any', 'children': [DOMAIN, {'kind': 'domain', 'value': 'openai.com', 'include_subdomains': True}]}
        first = preview({'scope': scope, 'target': 'GPT'}, inventory(), [], now=100)
        scope['children'].reverse()
        second = preview({'scope': scope, 'target': 'GPT'}, inventory(), [], now=100)
        self.assertEqual(first['policy_id'], second['policy_id'])
        self.assertIn('DOMAIN,chatgpt.com', first['proposed_rules'][0])
        self.assertIn('DOMAIN-SUFFIX,openai.com', first['proposed_rules'][0])


class InventoryTests(unittest.TestCase):
    def data(self):
        return {'/version': {'version': 'fixed-fixture'}, '/configs': {'mode': 'rule', 'secret': 'hidden'},
                '/rules': {'rules': inventory()['rules']}, '/proxies': {'proxies': {'GPT': {'type': 'Selector', 'all': ['node', 'DIRECT'], 'now': 'node', 'udp': True},
                                                                              'node': {'type': 'ss', 'password': 'private', 'udp': True}}}}

    def test_inventory_reads_only_whitelist_and_redacts_secrets(self):
        data = self.data();calls=[]
        def read(path):
            calls.append(path);return copy.deepcopy(data[path])
        result=collect_inventory(read, controller_id='local-hash',clock=lambda:100)
        self.assertTrue(result['available']);self.assertTrue(result['consistent'])
        self.assertEqual(len(calls), 7)
        self.assertTrue(result['groups']['GPT']['may_direct'])
        self.assertNotIn('private', json.dumps(result));self.assertNotIn('hidden', json.dumps(result))
        self.assertFalse(result['atomic_snapshot'])

    def test_concurrent_rule_change_detected(self):
        data=self.data();count=0
        def read(path):
            nonlocal count
            if path=='/rules':
                count+=1
                return {'rules': data[path]['rules'][:count]}
            return data[path]
        self.assertFalse(collect_inventory(read,controller_id='x')['consistent'])

    def test_failure_is_sanitized(self):
        def read(path):raise ValueError('Bearer private-path')
        result=collect_inventory(read,controller_id='x')
        self.assertFalse(result['available']);self.assertNotIn('private',str(result))

    def test_cyclic_group_is_not_declared_no_direct(self):
        data=self.data();data['/proxies']['proxies']['GPT']['all']=['GPT']
        self.assertTrue(collect_inventory(data.__getitem__,controller_id='x')['groups']['GPT']['may_direct'])

    def test_catalog_only_verified_fresh_paths(self):
        state={'ts':100,'flows':[FLOW,{**FLOW,'proc_path':r'C:\Other\browser.exe','identity_verified':False}]}
        service=NetworkPolicyService(inventory,lambda:state,clock=lambda:100)
        self.assertEqual([x['path'] for x in service.catalog()['applications']],[APP['value']])
        state['ts']=0
        self.assertEqual(service.catalog()['applications'],[])
        self.assertIn('process_identity_unverified',service.preview({'scope':APP,'target':'GPT'})['blockers'])

    def test_redirect_cannot_forward_authorization(self):
        handler=NoRedirect()
        self.assertIsNone(handler.redirect_request(Request('http://127.0.0.1:1',headers={'Authorization':'Bearer test'}),None,302,'Found',{},'https://example.com'))
        for base in ['https://remote.example', 'http://user:pass@127.0.0.1', 'http://127.0.0.1/api', 'http://127.0.0.1?secret=x']:
            with self.assertRaises(ValueError):pinned_reader(base,{})
        with self.assertRaises(ValueError):pinned_reader('http://127.0.0.1:1',{})('/connections')


class EvidenceTests(unittest.TestCase):
    context = {key: key + '-fixture' for key in ('instance_id', 'root_identity', 'profile_id', 'policy_id', 'operation_id')}

    def state(self, receipt, revision='new'):
        return evidence_state(receipt, trusted_session=True, revision=revision, context=self.context, now=100)

    def test_no_boolean_or_stale_receipt_can_claim_success(self):
        receipt={**self.context,'state':'verified','checked_at':100,'revision':'new','persisted':True,'loaded':True,'connection_verified':True,'connection_at':99,'applied_at':98}
        self.assertEqual(evidence_state(receipt),'unknown')
        self.assertEqual(self.state(receipt,'old'),'unknown')
        self.assertEqual(self.state(receipt),'connection_observed')
        receipt['connection_at']=97
        self.assertEqual(self.state(receipt),'loaded_awaiting_connection')
        receipt['loaded']=False
        self.assertEqual(self.state(receipt),'saved_not_loaded')

    def test_cross_instance_profile_operation_or_stale_audit_is_unknown(self):
        receipt={**self.context,'state':'loaded','checked_at':100,'revision':'new','persisted':True,'loaded':True}
        for key in self.context:
            self.assertEqual(self.state({**receipt,key:'different'}),'unknown')
        for checked in [80,101,float('nan'),True]:
            self.assertEqual(self.state({**receipt,'checked_at':checked}),'unknown')
        self.assertEqual(self.state({**receipt,'state':'invented'}),'unknown')

    def test_undo_requires_durable_and_runtime_removal(self):
        r={**self.context,'checked_at':100,'state':'revoked','revision':'new','owned_records_removed':True}
        self.assertEqual(self.state(r),'recovery_required')
        r['runtime_absent']=True
        self.assertEqual(self.state(r),'revoked')

    def test_all_receipt_and_clock_times_must_be_finite(self):
        receipt = {**self.context, 'state': 'verified', 'checked_at': 100, 'revision': 'new',
                   'persisted': True, 'loaded': True, 'connection_verified': True, 'connection_at': 99, 'applied_at': 98}
        self.assertEqual(self.state(receipt), 'connection_observed')
        for bad in [float('inf'), float('-inf'), float('nan'), True, '99', 10**400]:
            for key in ['checked_at', 'applied_at', 'connection_at']:
                with self.subTest(key=key, bad=repr(bad)):
                    self.assertEqual(self.state({**receipt, key: bad}), 'unknown')
            self.assertEqual(evidence_state(receipt, trusted_session=True, revision='new', context=self.context, now=bad), 'unknown')
        for key in ['applied_at', 'connection_at']:
            missing = dict(receipt); missing.pop(key)
            self.assertEqual(self.state(missing), 'loaded_awaiting_connection')
        self.assertEqual(self.state({**receipt, 'connection_at': 101}), 'loaded_awaiting_connection')


class PreviewSampleCoverageTests(unittest.TestCase):
    def test_preview_reports_inspected_range_without_claiming_all_connections(self):
        flows = [{**FLOW, 'host': 'outside.example'} for _ in range(500)] + [FLOW]
        result = preview({'scope': BROWSER, 'target': 'GPT'}, inventory(), flows)
        self.assertEqual(result['inspected_flow_count'], 500)
        self.assertEqual(result['total_flow_count'], 501)
        self.assertTrue(result['sample_truncated'])
        self.assertEqual(result['samples'], [])
        short = preview({'scope': BROWSER, 'target': 'GPT'}, inventory(), [FLOW])
        self.assertEqual(short['inspected_flow_count'], 1)
        self.assertFalse(short['sample_truncated'])


if __name__ == '__main__':
    unittest.main()
