import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from flashmonitor.app import MonitorApi
from flashmonitor.ipv6_native import NativeVerifier, canonical_rule
from flashmonitor.ipv6_plans import IPv6Plans, build_plan, inspect_flclash, digest
from flashmonitor.rule_drafts import DraftError
from test_ipv6_plans import fixture, CORE, subject
from test_server import ServerTestBase


class ReadOnlyControl:
    def __init__(self):self.rules=['DOMAIN,existing.example,DIRECT','MATCH,DIRECT'];self.mode='rule'
    def snapshot(self):return {**CORE,'mode':self.mode}
    def runtime_rules(self):
        return [{'type':t,'payload':p,'proxy':target} for t,p,target in map(canonical_rule,self.rules)]


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'client';self.root.mkdir();fixture(self.root)
        self.control=ReadOnlyControl();self.compile(self.control.rules)
        self.plan=build_plan(subject(),self.control.snapshot(),inspect_flclash(self.root))
        self.native=NativeVerifier(self.root,Path(self.tmp.name)/'ledger',self.control)
    def compile(self,rules):
        self.control.rules=list(rules)
        (self.root/'config.yaml').write_text(json.dumps({'rules':rules}),encoding='utf-8')
    def edit(self,sql,args=()):
        with closing(sqlite3.connect(self.root/'database.sqlite')) as c:
            c.execute('PRAGMA foreign_keys=ON');c.execute(sql,args);c.commit()
    def save_native(self):
        self.edit('INSERT INTO rules VALUES(22,"AND",?,"REJECT",NULL,NULL,0,0)',(self.plan['change']['content'],))
        self.edit('INSERT INTO profile_rule_mapping VALUES("123_22_added",123,22,"added","Zz")')
    def client_bytes(self):return {p.name:p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
    def test_complete_save_verify_undo_stages_without_network_writes(self):
        self.assertEqual(self.native.prepare(self.plan)['state'],'awaiting_client_save')
        self.assertEqual(self.native.verify(self.plan)['state'],'awaiting_client_save')
        self.save_native();self.assertEqual(self.native.verify(self.plan)['state'],'persisted_unverified')
        self.compile([self.plan['rule'],'DOMAIN,existing.example,DIRECT','MATCH,DIRECT'])
        before=self.client_bytes();r=self.native.verify(self.plan)
        self.assertEqual(r['state'],'runtime_verified');self.assertFalse(r['applied']);self.assertFalse(r['behavior_verified'])
        self.assertEqual(self.native.undo_instructions(self.plan)['rule_id'],'22')
        self.assertEqual(self.client_bytes(),before)
        self.edit('DELETE FROM profile_rule_mapping WHERE id="123_22_added"');self.edit('DELETE FROM rules WHERE id=22')
        self.assertEqual(self.native.verify(self.plan)['state'],'persistent_removed_runtime_pending')
        self.compile(['DOMAIN,existing.example,DIRECT','MATCH,DIRECT'])
        self.assertEqual(self.native.verify(self.plan)['state'],'revoked_verified')
    def test_prepare_and_verify_are_read_only_and_repeat_does_not_reset_baseline(self):
        before=self.client_bytes();self.native.prepare(self.plan)
        original=self.native._read(self.plan);self.native.prepare(self.plan)
        self.assertEqual(original,self.native._read(self.plan));self.native.verify(self.plan)
        self.assertEqual(before,self.client_bytes())
    def test_second_active_native_plan_rejected_and_original_baseline_preserved(self):
        self.native.prepare(self.plan)
        other=build_plan(subject(value='other.example'),self.control.snapshot(),inspect_flclash(self.root))
        with self.assertRaisesRegex(DraftError,'另一个'):self.native.prepare(other)
        self.assertEqual(len(list(self.native.ledger.glob('*.json'))),1)
    def test_preexisting_identical_native_rule_not_claimed(self):
        self.save_native();self.plan=build_plan(subject(),self.control.snapshot(),inspect_flclash(self.root))
        with self.assertRaisesRegex(DraftError,'不接管'):self.native.prepare(self.plan)
    def test_other_rule_change_or_mode_change_persisted_as_conflict(self):
        self.native.prepare(self.plan);self.save_native();self.compile([self.plan['rule'],'DOMAIN,existing.example,DIRECT','MATCH,DIRECT'])
        self.control.mode='global'
        self.assertEqual(self.native.verify(self.plan)['state'],'configuration_changed')
        self.assertEqual(self.native.status(self.plan)['state'],'configuration_changed')
        self.control.mode='rule';self.edit('UPDATE rules SET content="external.example" WHERE id=1')
        with self.assertRaises(DraftError):self.native.undo_instructions(self.plan)
    def test_replacement_same_text_id_never_adopted(self):
        self.native.prepare(self.plan);self.save_native();self.compile([self.plan['rule'],'DOMAIN,existing.example,DIRECT','MATCH,DIRECT'])
        self.native.verify(self.plan)
        self.edit('DELETE FROM profile_rule_mapping WHERE rule_id=22');self.edit('DELETE FROM rules WHERE id=22')
        self.edit('INSERT INTO rules VALUES(23,"AND",?,"REJECT",NULL,NULL,0,0)',(self.plan['change']['content'],))
        self.edit('INSERT INTO profile_rule_mapping VALUES("123_23_added",123,23,"added","Zz")')
        self.assertEqual(self.native.verify(self.plan)['state'],'configuration_changed')
        self.assertEqual(self.native._read(self.plan)['client_rule_id'],'22')
    def test_profile_changed_does_not_issue_undo_instructions(self):
        self.native.prepare(self.plan);self.save_native();self.compile([self.plan['rule'],'DOMAIN,existing.example,DIRECT','MATCH,DIRECT']);self.native.verify(self.plan)
        prefs=self.root/'shared_preferences.json';data=json.loads(prefs.read_text(encoding='utf-8'));state=json.loads(data['flutter.config']);state['currentProfileId']=999;data['flutter.config']=json.dumps(state);prefs.write_text(json.dumps(data),encoding='utf-8')
        self.assertEqual(self.native.verify(self.plan)['state'],'out_of_scope')
        with self.assertRaises(DraftError):self.native.undo_instructions(self.plan)
    def test_owned_row_provider_or_order_edit_not_silently_adopted(self):
        self.native.prepare(self.plan);self.save_native();self.compile([self.plan['rule'],'DOMAIN,existing.example,DIRECT','MATCH,DIRECT']);self.native.verify(self.plan)
        self.edit('UPDATE profile_rule_mapping SET "order"="Zy" WHERE id="123_22_added"')
        self.assertEqual(self.native.verify(self.plan)['state'],'configuration_changed')
        self.edit('UPDATE profile_rule_mapping SET "order"="Zz" WHERE id="123_22_added"')
        self.edit('UPDATE rules SET rule_provider="external" WHERE id=22')
        with self.assertRaises(DraftError):self.native.undo_instructions(self.plan)


class NativeRoutes(ServerTestBase):
    def setUp(self):
        super().setUp();self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'client';self.root.mkdir();fixture(self.root)
        self.control=ReadOnlyControl()
        (self.root/'config.yaml').write_text(json.dumps({'rules':self.control.rules}),encoding='utf-8')
        self.plans=IPv6Plans(Path(self.tmp.name)/'plans.json');self.native=NativeVerifier(self.root,Path(self.tmp.name)/'ledger',self.control)
        self.srv.app=MonitorApi(SimpleNamespace(ipv6_plans=self.plans,ipv6_native=self.native,clash=SimpleNamespace(ipv6_inventory=self.control.snapshot)))
        p=patch('flashmonitor.app.flclash_dir',return_value=self.root);p.start();self.addCleanup(p.stop)
        self.plan=build_plan(subject(),self.control.snapshot(),inspect_flclash(self.root))
        self.plans.operate({'operation':'save','revision':0,'fingerprint':digest(self.plan)},self.plan)
    def test_api_guide_export_verify_delete_pending_and_invalid_scope(self):
        body={'revision':1,'id':self.plan['id']}
        code,_,raw=self.request('POST','/api/ipv6-plan',{**body,'operation':'native_prepare'})
        self.assertEqual(code,200);self.assertEqual(json.loads(raw)['fields']['content'],self.plan['change']['content'])
        code,_,_=self.request('POST','/api/ipv6-plan',{'operation':'save','revision':1,'subject':subject(),'fingerprint':digest(self.plan)})
        self.assertEqual(code,400)
        self.assertEqual(self.plans.read()['plans'][self.plan['id']],self.plan)
        code,_,raw=self.request('POST','/api/ipv6-plan',{**body,'operation':'native_export'})
        self.assertEqual(code,200);self.assertIn(self.plan['rule'],json.loads(raw)['text'])
        code,_,raw=self.request('POST','/api/ipv6-plan',{**body,'operation':'native_verify'})
        self.assertEqual(code,200);self.assertFalse(json.loads(raw)['applied'])
        code,_,raw=self.request('GET','/api/ipv6-plans')
        self.assertTrue(json.loads(raw)['native_supported'])
        self.assertEqual(json.loads(raw)['plans'][0]['native']['state'],'awaiting_client_save')
        code,_,_=self.request('POST','/api/ipv6-plan',{**body,'operation':'native_prepare','revision':0});self.assertEqual(code,400)
        code,_,_=self.request('POST','/api/ipv6-plan',{**body,'operation':'native_prepare','id':[]});self.assertEqual(code,400)
        code,_,_=self.request('POST','/api/ipv6-plan',{**body,'operation':'delete'});self.assertEqual(code,200)
        self.assertEqual(list(self.native.ledger.glob('*.json')),[])
