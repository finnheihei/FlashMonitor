import json
import re
import sqlite3
import unittest
from pathlib import Path


class BridgeSchemaContractTests(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).resolve().parents[1]
        self.db=sqlite3.connect(':memory:');self.addCleanup(self.db.close)
        self.db.executescript((root/'tests/fixtures/flclash_0_8_98_schema.sql').read_text(encoding='utf-8'))
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('INSERT INTO profiles(id,label,url,overwrite_type,auto_update_duration_millis,auto_update,selected_map,unfold_set) VALUES(123,"fixture","","standard",0,0,"{}","[]")')
        dart=(root/'integrations/flclash_direct_bridge/overlay/lib/bridge/journal.dart').read_text(encoding='utf-8')
        migration=re.search("const bridgeJournalMigration = '''(.*?)'''",dart,re.S)[1]
        self.db.executescript(migration);self.db.execute('PRAGMA user_version=4')
    def insert(self,rule_id,operation):
        key=f'123_{rule_id}_added'
        self.db.execute('INSERT INTO rules VALUES(?,"AND","scoped","REJECT",NULL,NULL,0,0)',(rule_id,))
        self.db.execute('INSERT INTO profile_rule_mapping VALUES(?,123,?,"added","Zz")',(key,rule_id))
        self.db.execute('INSERT INTO monitor_bridge_operations VALUES(?,123,"persisted","hash",?,?)',
                        (operation,json.dumps({'rule_id':str(rule_id),'mapping_id':key}),json.dumps({'state':'persisted'})))
    def test_schema4_journal_same_db_canonical_text_and_exact_large_id(self):
        with self.db:self.insert(9007199254741024,'a'*32)
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0],4)
        key=self.db.execute('SELECT id FROM profile_rule_mapping').fetchone()[0]
        self.assertEqual(key,'123_9007199254741024_added')
        self.assertEqual(json.loads(self.db.execute('SELECT owned_json FROM monitor_bridge_operations').fetchone()[0])['rule_id'],'9007199254741024')
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(),[])
    def test_journal_failure_rolls_back_native_rule_and_mapping_together(self):
        with self.db:self.insert(2,'a'*32)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db:self.insert(3,'a'*32)
        self.assertEqual(self.db.execute('SELECT id FROM rules').fetchall(),[(2,)])
        self.assertEqual(self.db.execute('SELECT rule_id FROM profile_rule_mapping').fetchall(),[(2,)])
        self.assertEqual(self.db.execute('SELECT count(*) FROM monitor_bridge_operations').fetchone()[0],1)
    def test_native_foreign_key_failure_cannot_leave_a_journal(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db:
                self.db.execute('INSERT INTO profile_rule_mapping VALUES("123_99_added",123,99,"added","Zz")')
                self.db.execute('INSERT INTO monitor_bridge_operations VALUES("op",123,"persisted","hash","[]","{}")')
        self.assertEqual(self.db.execute('SELECT count(*) FROM monitor_bridge_operations').fetchone()[0],0)
