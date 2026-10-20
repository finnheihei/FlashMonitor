import json
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
from flashmonitor.ipv6_executor import IPv6Executor, key_before, transform_document
from flashmonitor.ipv6_plans import build_plan, digest, inspect_flclash
from flashmonitor.rule_drafts import DraftError
from test_ipv6_plans import fixture, CORE, subject
from flashmonitor.app import MonitorApi
from flashmonitor.ipv6_plans import IPv6Plans
from test_server import ServerTestBase


class FakeControl:
    def __init__(self):self.facts=dict(CORE);self.rule=None;self.fail=0;self.calls=[];self.after_reload=None
    def snapshot(self):return dict(self.facts)
    def contains(self,rule):return self.rule==rule
    def reload(self,path):
        self.calls.append(path)
        if self.fail:self.fail-=1;raise OSError('reload refused')
        text=path.read_text(encoding='utf-8')
        self.rule=next((line[5:] for line in text.splitlines() if line.startswith('  - "AND,')),None)
        if text.lstrip().startswith('{'):self.rule=next((r for r in json.loads(text)['rules'] if r.startswith('AND,')),None)
        else:
            for line in text.splitlines():
                if line.strip().startswith('- "AND,'):self.rule=json.loads(line.strip()[2:])
        self.facts['rules_revision']='applied' if self.rule else 'old'
        if self.after_reload:self.after_reload()
    def wait(self,rule,present,timeout=5):
        if self.contains(rule)!=present:raise DraftError('publication failed')


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)/'clash';self.root.mkdir();fixture(self.root)
        self.control=FakeControl();self.plan=build_plan(subject(),self.control.snapshot(),inspect_flclash(self.root))
        self.grant={'target_root':str(self.root.resolve()),'fingerprint':digest(self.plan),'profile_id':123,'subject_id':self.plan['id'],'allow_reload':True}
        self.executor=IPv6Executor(self.root,Path(self.tmp.name)/'backups',self.control,self.grant,experimental=True)
    def count(self):
        conn=sqlite3.connect(self.root/'database.sqlite')
        try:return conn.execute('SELECT count(*) FROM rules').fetchone()[0]
        finally:conn.close()
    def test_exact_grant_required_before_any_write(self):
        self.executor.consent=None
        with self.assertRaises(DraftError):self.executor.apply(self.plan)
        self.assertEqual(self.count(),1);self.assertFalse(self.executor.backups.exists());self.assertEqual(self.control.calls,[])
    def test_persistence_backup_idempotence_and_revoke(self):
        original=(self.root/'config.yaml').read_bytes()
        self.assertTrue(self.executor.apply(self.plan)['applied']);self.assertEqual(self.count(),2)
        self.assertEqual((self.root/'config.yaml').read_bytes(),original)
        record=self.executor._read(self.plan['id']);self.assertTrue(Path(record['backup_database']).exists())
        calls=len(self.control.calls);self.assertTrue(self.executor.apply(self.plan)['duplicate']);self.assertEqual(len(self.control.calls),calls)
        self.assertFalse(self.executor.revoke(self.plan)['applied']);self.assertEqual(self.count(),1);self.assertFalse(self.control.contains(self.plan['rule']))
        self.assertTrue(self.executor.revoke(self.plan)['duplicate'])
    def test_reload_failure_rolls_back_owned_rows_and_original_core(self):
        self.control.fail=1
        with self.assertRaises(DraftError):self.executor.apply(self.plan)
        self.assertEqual(self.count(),1);self.assertEqual(self.executor.status(self.plan['id'])['state'],'rolled_back')
        self.assertEqual(len(self.control.calls),2)
    def test_failed_restore_preserves_recovery_record_and_retry(self):
        self.control.fail=2
        with self.assertRaises(DraftError):self.executor.apply(self.plan)
        self.assertEqual(self.executor.status(self.plan['id'])['state'],'persistent_removed_runtime_pending')
        self.assertFalse(self.executor.recover(self.plan)['applied']);self.assertEqual(self.count(),1)
    def test_subscription_conflict_no_writes_then_explicit_reapproved_reconcile(self):
        self.executor.apply(self.plan)
        (self.root/'profiles'/'123.yaml').write_text('rules:\n - MATCH,Other\n',encoding='utf-8')
        with self.assertRaises(DraftError):self.executor.revoke(self.plan)
        self.control.rule=None;self.control.facts['rules_revision']='old'
        self.assertEqual(self.executor.status(self.plan['id'])['state'],'configuration_changed')
        fresh=build_plan(subject(),self.control.snapshot(),inspect_flclash(self.root))
        self.executor.consent={**self.grant,'fingerprint':digest(fresh)}
        self.assertTrue(self.executor.reconcile(fresh)['applied']);self.assertEqual(self.count(),2)
        self.assertFalse(self.executor.revoke(fresh)['applied']);self.assertEqual(self.count(),1)
    def test_external_owned_row_edit_is_never_deleted(self):
        self.executor.apply(self.plan);record=self.executor._read(self.plan['id'])
        conn=sqlite3.connect(self.root/'database.sqlite');conn.execute('UPDATE rules SET content="external" WHERE id=?',(record['rule_id'],));conn.commit();conn.close()
        with self.assertRaises(DraftError):self.executor.apply(self.plan)
        with self.assertRaises(DraftError):self.executor.revoke(self.plan)
        self.assertEqual(self.count(),2)
    def test_preexisting_duplicate_not_claimed(self):
        conn=sqlite3.connect(self.root/'database.sqlite');conn.execute('UPDATE rules SET rule_action="AND",content=?,rule_target="REJECT" WHERE id=1',(self.plan['change']['content'],));conn.commit();conn.close()
        self.plan=build_plan(subject(),self.control.snapshot(),inspect_flclash(self.root));self.executor.consent={**self.grant,'fingerprint':digest(self.plan)}
        with self.assertRaises(DraftError):self.executor.apply(self.plan)
        self.assertEqual(self.count(),1)
    def test_runtime_mode_change_not_claimed_applied_or_overwritten(self):
        self.executor.apply(self.plan);self.control.facts['mode']='global'
        self.assertEqual(self.executor.status(self.plan['id'])['state'],'configuration_changed')
        before=len(self.control.calls)
        with self.assertRaises(DraftError):self.executor.revoke(self.plan)
        self.assertEqual(len(self.control.calls),before)
    def test_corrupt_backup_refuses_revoke_before_sql_or_reload(self):
        self.executor.apply(self.plan);record=self.executor._read(self.plan['id'])
        Path(record['backup_config']).write_text('mode: global\nrules:\n - MATCH,DIRECT\n',encoding='utf-8')
        before=len(self.control.calls)
        with self.assertRaises(DraftError):self.executor.revoke(self.plan)
        self.assertEqual(self.count(),2);self.assertEqual(len(self.control.calls),before)
    def test_document_and_fractional_order_preserve_original(self):
        text='mode: rule\nrules:\n  - MATCH,DIRECT\ndns:\n  enable: false\n'
        changed=transform_document(text,self.plan['rule'])
        self.assertEqual(transform_document(changed,self.plan['rule'],True),text)
        self.assertEqual(key_before('a0'),'Zz');self.assertLess(key_before('Zz'),'Zz')
        self.assertEqual(key_before('b00'),'az')


class ExecutorRoutes(ServerTestBase):
    def setUp(self):
        super().setUp();self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'clash';self.root.mkdir();fixture(self.root)
        self.control=FakeControl();self.plans=IPv6Plans(Path(self.tmp.name)/'plans.json')
        self.executor=IPv6Executor(self.root,Path(self.tmp.name)/'backups',self.control,experimental=True)
        self.srv.app=MonitorApi(SimpleNamespace(ipv6_plans=self.plans,ipv6_executor=self.executor,clash=SimpleNamespace(ipv6_inventory=self.control.snapshot)))
        p=patch('flashmonitor.app.flclash_dir',return_value=self.root);p.start();self.addCleanup(p.stop)
    def test_saved_scope_grant_apply_catalog_revoke_and_delete_guard(self):
        body={'operation':'preview','revision':0,'subject':subject()}
        code,_,raw=self.request('POST','/api/ipv6-plan',body);self.assertEqual(code,200);preview=json.loads(raw)
        code,_,_=self.request('POST','/api/ipv6-plan',{**body,'operation':'save','fingerprint':preview['fingerprint']});self.assertEqual(code,200)
        action={'operation':'apply','revision':1,'id':preview['id']}
        code,_,_=self.request('POST','/api/ipv6-plan',action);self.assertEqual(code,400);self.assertFalse(self.executor.backups.exists())
        plan=self.plans.read()['plans'][preview['id']]
        self.executor.consent={'target_root':str(self.root.resolve()),'fingerprint':digest(plan),'profile_id':123,'subject_id':plan['id'],'allow_reload':True}
        code,_,raw=self.request('POST','/api/ipv6-plan',action);self.assertEqual(code,200);self.assertTrue(json.loads(raw)['applied'])
        code,_,raw=self.request('GET','/api/ipv6-plans');self.assertTrue(json.loads(raw)['plans'][0]['execution']['applied'])
        code,_,_=self.request('POST','/api/ipv6-plan',{**action,'operation':'delete'});self.assertEqual(code,400)
        code,_,raw=self.request('POST','/api/ipv6-plan',{**action,'operation':'revoke'});self.assertEqual(code,200);self.assertFalse(json.loads(raw)['applied'])
        code,_,_=self.request('POST','/api/ipv6-plan',{**action,'operation':'delete'});self.assertEqual(code,200)
