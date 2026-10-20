import time
import unittest
from pathlib import Path
import test_ipv6_native as native_tests
from flashmonitor.ipv6_flow import IPv6Flow
from flashmonitor.ipv6_plans import IPv6Plans, build_plan, inspect_flclash, digest, subject_id
from flashmonitor.rule_drafts import DraftError
from test_ipv6_plans import subject


class FlowTests(unittest.TestCase):
    compile=native_tests.NativeTests.compile
    edit=native_tests.NativeTests.edit
    save_native=native_tests.NativeTests.save_native
    def setUp(self):
        native_tests.NativeTests.setUp(self);self.plans=IPv6Plans(Path(self.tmp.name)/'plans.json')
        self.flow=IPv6Flow(self.plans,self.native,self.control,lambda s:s['kind']=='process_name' or Path(s['value']).is_file())
    def legacy(self):
        path=self.root/'ChatGPT.exe';path.write_bytes(b'fixture')
        plan=build_plan(subject('process_path',str(path)),self.control.snapshot(),inspect_flclash(self.root))
        plan['schema_version']=1;plan['persistent'].pop('target_root');plan['persistent']['profile_id']=123
        self.plans.operate({'operation':'save','revision':0,'fingerprint':digest(plan)},plan)
        return plan
    def begin(self,resolved,**extra):return self.flow.begin({'operation':'begin','context_token':resolved['context_token'],'idempotency_key':resolved['operation_id'],**extra})
    def test_legacy_missing_root_int_profile_rebound_and_begin_once(self):
        old=self.legacy();r=self.flow.resolve({'operation':'resolve','id':old['id']})
        self.assertTrue(r['legacy_rebound']);self.assertEqual(r['plan']['persistent']['profile_id'],'123')
        before=(self.root/'database.sqlite').read_bytes()
        first=self.begin(r);baseline=self.native._read(first['plan']);again=self.begin(r)
        self.assertEqual(first,again);self.assertEqual(self.plans.read()['revision'],2)
        self.assertEqual(self.native._read(first['plan']),baseline)
        self.assertEqual(self.plans.read()['plans'][old['id']]['schema_version'],2)
        self.assertEqual(before,(self.root/'database.sqlite').read_bytes())
    def test_true_other_root_does_not_migrate(self):
        old=self.legacy();old['persistent']['target_root']=str(self.root/'other')
        self.plans.operate({'operation':'save','revision':1,'fingerprint':digest(old)},old)
        with self.assertRaisesRegex(DraftError,'target_changed'):self.flow.resolve({'operation':'resolve','id':old['id']})
        self.assertFalse(self.native.ledger.exists())
    def test_case_and_separator_root_differences_not_false_conflict(self):
        old=self.legacy();old['persistent']['target_root']=str(self.root).upper().replace('\\','/')
        self.plans.operate({'operation':'save','revision':1,'fingerprint':digest(old)},old)
        self.assertIn('context_token',self.flow.resolve({'operation':'resolve','id':old['id']}))
    def test_profile_change_requires_explicit_confirmation(self):
        old=self.legacy();old['persistent']['profile_id']=999
        self.plans.operate({'operation':'save','revision':1,'fingerprint':digest(old)},old)
        r=self.flow.resolve({'operation':'resolve','id':old['id']});self.assertTrue(r['changes'])
        with self.assertRaisesRegex(DraftError,'confirmation'):self.begin(r)
        self.assertFalse(self.native.ledger.exists());self.begin(r,confirm_changes=True)
    def test_stale_token_revision_and_unknown_fields_do_not_begin(self):
        r=self.flow.resolve({'operation':'resolve','subject':subject()})
        with self.assertRaisesRegex(DraftError,'未知字段'):self.begin(r,target_root=str(self.root))
        self.flow.tokens[r['context_token']]['created']=time.monotonic()-301
        with self.assertRaisesRegex(DraftError,'expired'):self.begin(r)
        r=self.flow.resolve({'operation':'resolve','subject':subject()})
        p=r['plan'];self.plans.operate({'operation':'save','revision':0,'fingerprint':digest(p)},p)
        with self.assertRaisesRegex(DraftError,'revision_changed'):self.begin(r)
        self.assertFalse(self.native.ledger.exists())
    def test_unavailable_saved_path_not_widened_to_process_name(self):
        old=self.legacy();Path(old['subject']['value']).unlink()
        with self.assertRaisesRegex(DraftError,'subject_unavailable'):self.flow.resolve({'operation':'resolve','id':old['id']})
        self.assertFalse(self.native.ledger.exists())
    def test_existing_recovery_record_prevents_legacy_migration(self):
        old=self.legacy()
        self.flow.executor=type('Recovery',(),{'_read':lambda self,k:{'state':'recovery_required'}})()
        with self.assertRaisesRegex(DraftError,'recovery_required'):self.flow.resolve({'operation':'resolve','id':old['id']})
        self.assertEqual(self.plans.read()['plans'][old['id']]['schema_version'],1)
    def test_multi_object_single_rule_roundtrip_and_visible_undo(self):
        selection={'kind':'selection','subjects':[subject('process_name','a.exe'),subject('domain','b.example')]}
        r=self.flow.resolve({'operation':'resolve','subject':selection});p=r['plan'];self.begin(r)
        self.assertIn('(OR,',p['rule']);self.plan=p;self.save_native()
        self.compile([p['rule'],'DOMAIN,existing.example,DIRECT','MATCH,DIRECT'])
        self.assertEqual(self.native.verify(p)['state'],'runtime_verified')
        guide=self.native.undo_instructions(p)
        self.assertEqual(guide['fields']['content'],p['change']['content'])
        self.assertIn('无需查数据库ID',guide['steps'][0])
        self.assertEqual(subject_id(selection),p['id'])
    def test_compiling_snapshot_changes_yield_applying_not_persistent_conflict(self):
        self.native.prepare(self.plan);self.save_native();self.compile([self.plan['rule'],'DOMAIN,existing.example,DIRECT','MATCH,DIRECT'])
        original=self.control.runtime_rules
        def changing():
            result=original();self.edit('UPDATE rules SET content="changed.example" WHERE id=1');return result
        self.control.runtime_rules=changing
        self.assertEqual(self.native.verify(self.plan)['state'],'applying')
        self.control.runtime_rules=original
        self.assertEqual(self.native.verify(self.plan)['state'],'configuration_changed')
