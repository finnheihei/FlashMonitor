"""Astra's real-DDL defects, using only temporary fixtures and in-memory core."""
import json
import sqlite3
import unittest
from pathlib import Path
from contextlib import closing
from flashmonitor.ipv6_executor import IPv6Executor
from flashmonitor.ipv6_plans import build_plan, digest, inspect_flclash
from flashmonitor.rule_drafts import DraftError
import test_ipv6_executor as executor_tests
from test_ipv6_plans import subject


class AstraRegressions(unittest.TestCase):
    setUp = executor_tests.ExecutorTests.setUp
    count = executor_tests.ExecutorTests.count

    def db_rows(self):
        with closing(sqlite3.connect(self.root/'database.sqlite')) as c:
            return (c.execute('SELECT * FROM rules ORDER BY id').fetchall(),
                    c.execute('SELECT * FROM profile_rule_mapping ORDER BY id').fetchall())

    def approve(self,plan):
        self.executor.consent={**self.grant,'fingerprint':digest(plan),'subject_id':plan['id']}

    def test_astra_real_text_pk_apply_status_json_roundtrip_revoke(self):
        before=self.db_rows()
        with closing(sqlite3.connect(self.root/'database.sqlite')) as c:
            field=next(r for r in c.execute('PRAGMA table_info(profile_rule_mapping)') if r[1]=='id')
            self.assertEqual((field[2],field[5]),('TEXT',1))
            self.assertEqual(len(c.execute('PRAGMA foreign_key_list(profile_rule_mapping)').fetchall()),2)
        self.executor.apply(self.plan)
        record=json.loads(json.dumps(self.executor._read(self.plan['id'])))
        self.assertEqual(record['mapping_id'],f"123_{record['rule_id']}_added")
        self.assertIsInstance(record['mapping_id'],str)
        self.assertEqual(self.executor.status(self.plan['id'])['state'],'applied')
        with closing(sqlite3.connect(self.root/'database.sqlite')) as c:self.assertTrue(self.executor._owned(c,record))
        self.executor.revoke(self.plan)
        self.assertEqual(self.db_rows(),before)

    def test_astra_first_reload_failure_removes_only_owned_rows(self):
        before=self.db_rows();self.control.fail=1
        with self.assertRaises(DraftError):self.executor.apply(self.plan)
        self.assertEqual(self.db_rows(),before)
        self.assertEqual(self.executor.status(self.plan['id'])['state'],'rolled_back')

    def test_astra_a_then_b_rejected_before_any_backup_sql_reload(self):
        self.executor.apply(self.plan)
        other=build_plan(subject(value='second.example'),self.control.snapshot(),inspect_flclash(self.root))
        self.approve(other)
        before=self.db_rows();files={p:p.read_bytes() for p in self.executor.backups.iterdir()};calls=len(self.control.calls)
        with self.assertRaisesRegex(DraftError,'第二方案'):self.executor.apply(other)
        self.assertEqual(self.db_rows(),before)
        self.assertEqual({p:p.read_bytes() for p in self.executor.backups.iterdir()},files)
        self.assertEqual(len(self.control.calls),calls)
        self.assertTrue(self.control.contains(self.plan['rule']))
        self.assertEqual(self.executor.status(self.plan['id'])['state'],'applied')
        self.approve(self.plan);self.executor.revoke(self.plan)
        other=build_plan(subject(value='second.example'),self.control.snapshot(),inspect_flclash(self.root));self.approve(other)
        self.assertTrue(self.executor.apply(other)['applied'])
        self.executor.revoke(other);self.assertEqual(self.count(),1)

    def test_unresolved_a_blocks_b_without_side_effects(self):
        self.control.fail=2
        with self.assertRaises(DraftError):self.executor.apply(self.plan)
        other=build_plan(subject(value='second.example'),self.control.snapshot(),inspect_flclash(self.root));self.approve(other)
        calls=len(self.control.calls)
        with self.assertRaisesRegex(DraftError,'第二方案'):self.executor.apply(other)
        self.assertEqual(len(self.control.calls),calls)

    def test_differential_undo_keeps_existing_rules_and_other_settings(self):
        text=(self.root/'config.yaml').read_text(encoding='utf-8')
        text=text.replace(' - MATCH,DIRECT',' - DOMAIN,untouched.example,Proxy\n - MATCH,DIRECT')
        (self.root/'config.yaml').write_text(text,encoding='utf-8')
        self.plan=build_plan(subject(),self.control.snapshot(),inspect_flclash(self.root));self.approve(self.plan)
        before=self.db_rows();self.executor.apply(self.plan);self.executor.revoke(self.plan)
        self.assertEqual(self.control.calls[-1].read_text(encoding='utf-8'),text)
        self.assertEqual((self.root/'config.yaml').read_text(encoding='utf-8'),text)
        self.assertEqual(self.db_rows(),before)

    def test_rules_only_external_change_stops_duplicate_revoke_and_recover(self):
        self.executor.apply(self.plan);self.control.facts['rules_revision']='external-only'
        before=self.db_rows();calls=len(self.control.calls)
        with self.assertRaises(DraftError):self.executor.apply(self.plan)
        with self.assertRaises(DraftError):self.executor.revoke(self.plan)
        record=self.executor._read(self.plan['id']);record['state']='recovery_required';self.executor._save(record)
        with self.assertRaises(DraftError):self.executor.recover(self.plan)
        self.assertEqual(self.db_rows(),before);self.assertEqual(len(self.control.calls),calls)

    def test_mapping_ownership_and_new_external_reference_prevent_deletion(self):
        self.executor.apply(self.plan);record=self.executor._read(self.plan['id'])
        changed=json.loads(json.dumps(record));changed['mapping_id']=record['rule_id']
        with closing(sqlite3.connect(self.root/'database.sqlite')) as c:
            self.assertFalse(self.executor._owned(c,changed))
            c.execute('INSERT INTO profile_rule_mapping VALUES(?,NULL,?,NULL,"a0")',(str(record['rule_id']),record['rule_id']))
            c.commit()
        before=self.db_rows()
        with self.assertRaisesRegex(DraftError,'外部引用'):self.executor._rollback(record,Path(record['backup_config']).read_text(encoding='utf-8'))
        self.assertEqual(self.db_rows(),before)

    def test_durable_removed_runtime_pending_is_recoverable(self):
        self.executor.apply(self.plan);self.control.fail=1
        with self.assertRaises(OSError):self.executor.revoke(self.plan)
        self.assertEqual(self.count(),1)
        self.assertTrue(self.control.contains(self.plan['rule']))
        self.assertEqual(self.executor.status(self.plan['id'])['state'],'persistent_removed_runtime_pending')
        self.executor.recover(self.plan)
        self.assertFalse(self.control.contains(self.plan['rule']))

    def test_real_schema_version_and_noncanonical_key_reject_before_backup(self):
        with closing(sqlite3.connect(self.root/'database.sqlite')) as c:
            c.execute('UPDATE profile_rule_mapping SET id="124"');c.commit()
        self.assertFalse(inspect_flclash(self.root)['available'])
        with self.assertRaises(DraftError):self.executor.apply(self.plan)
        self.assertFalse(self.executor.backups.exists());self.assertEqual(self.control.calls,[])

    def test_production_capability_gate_cannot_be_bypassed_by_consent(self):
        production=IPv6Executor(self.root,self.executor.backups,self.control,self.grant)
        with self.assertRaisesRegex(DraftError,'unsupported_client_adapter'):production.apply(self.plan)
        self.assertFalse(production.eligible(self.plan))
        self.assertEqual(self.count(),1);self.assertFalse(production.backups.exists());self.assertEqual(self.control.calls,[])

    def test_schema_version_and_operation_root_are_not_coerced(self):
        with closing(sqlite3.connect(self.root/'database.sqlite')) as c:
            c.execute('PRAGMA user_version=4')
        self.assertFalse(inspect_flclash(self.root)['available'])
        with self.assertRaises(DraftError):self.executor.apply(self.plan)
        self.assertFalse(self.executor.backups.exists())
        with closing(sqlite3.connect(self.root/'database.sqlite')) as c:c.execute('PRAGMA user_version=3')
        self.executor.apply(self.plan);record=self.executor._read(self.plan['id'])
        record['target_root']=str(self.root/'other');self.executor._save(record)
        before=self.db_rows();calls=len(self.control.calls)
        with self.assertRaisesRegex(DraftError,'目标目录'):self.executor.revoke(self.plan)
        self.assertEqual(self.db_rows(),before);self.assertEqual(len(self.control.calls),calls)

    def test_reconcile_failure_uses_new_runtime_revision_and_rolls_back(self):
        self.executor.apply(self.plan)
        (self.root/'profiles'/'123.yaml').write_text('rules:\n - MATCH,Other\n',encoding='utf-8')
        self.control.rule=None;self.control.facts['rules_revision']='refreshed'
        fresh=build_plan(subject(),self.control.snapshot(),inspect_flclash(self.root));self.approve(fresh)
        self.control.fail=1
        with self.assertRaises(DraftError):self.executor.reconcile(fresh)
        self.assertEqual(self.count(),1)
        self.assertEqual(self.executor.status(self.plan['id'])['state'],'rolled_back')
