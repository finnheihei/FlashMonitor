import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from flashmonitor.app import MonitorApi
from flashmonitor.ipv6_plans import IPv6Plans, build_plan, inspect_flclash, merge_rules, reject_rule, undo_rules
from flashmonitor.rule_drafts import DraftError
from test_server import ServerTestBase
from flashmonitor.clash import ClashClient


def subject(kind='domain',value='Example.COM',sub=False):return {'kind':kind,'value':value,'include_subdomains':sub}
CORE={'available':True,'meta':True,'version':'1.10.0','mode':'rule','ipv6':False,'find_process_mode':'always','rules_revision':'old'}


def fixture(root):
    (root/'profiles').mkdir()
    (root/'shared_preferences.json').write_text(json.dumps({'flutter.config':json.dumps({'currentProfileId':123,'davProps':{'password':'PRIVATE'},'patchClashConfig':{'secret':'PRIVATE'}})}),encoding='utf-8')
    (root/'config.yaml').write_text('dns:\n  enable: true\n  ipv6: false\n  enhanced-mode: "fake-ip"\nrules:\n - MATCH,DIRECT\n',encoding='utf-8')
    (root/'profiles'/'123.yaml').write_text('secret: PRIVATE\nrules:\n - MATCH,DIRECT\n',encoding='utf-8')
    conn=sqlite3.connect(root/'database.sqlite')
    conn.executescript((Path(__file__).parent/'fixtures'/'flclash_0_8_98_schema.sql').read_text(encoding='utf-8'))
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('INSERT INTO profiles(id,label,url,overwrite_type,auto_update_duration_millis,auto_update,selected_map,unfold_set) VALUES(123,"fixture","https://example.com/PRIVATE","standard",0,0,"{}","[]")')
    conn.execute('INSERT INTO rules VALUES(1,"DOMAIN","existing.example","DIRECT",NULL,NULL,0,0)')
    conn.execute('INSERT INTO profile_rule_mapping VALUES("123_1_added",123,1,"added","a0")')
    conn.commit();conn.close()


class IPv6PlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        fixture(self.root);self.store=IPv6Plans(self.root/'pending.json');self.core=Mock()
        self.core.ipv6_inventory.return_value=dict(CORE)
        self.api=MonitorApi(SimpleNamespace(ipv6_plans=self.store,clash=self.core))
        self.patch=patch('flashmonitor.app.flclash_dir',return_value=self.root);self.patch.start();self.addCleanup(self.patch.stop)
    def body(self,op='preview',**extra):return {'operation':op,'revision':0,'subject':subject(),**extra}

    def test_precise_scope_and_no_dns_resolution_rule(self):
        self.assertEqual(reject_rule(subject()),'AND,((DOMAIN,example.com),(IP-CIDR6,::/0,no-resolve)),REJECT')
        self.assertIn('DOMAIN-SUFFIX,example.com',reject_rule(subject(sub=True)))
        self.assertIn('PROCESS-PATH,C:\\Program Files (x86)\\app.exe',reject_rule(subject('process_path',r'C:\Program Files (x86)\app.exe')))
        self.assertIn('PROCESS-NAME,app.exe',reject_rule(subject('process_name','app.exe')))
        for value in ['example.com,REJECT','example.com\n']:
            with self.assertRaises(DraftError):reject_rule(subject(value=value))
        with self.assertRaises(DraftError):reject_rule(subject('process_path',r'C:\Apps)\app.exe'))

    def test_metadata_read_only_sanitized_and_source_updates_detected(self):
        before={p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        p=inspect_flclash(self.root)
        self.assertTrue(p['available']);self.assertEqual(p['dns']['enhanced-mode'],'fake-ip')
        self.assertEqual(p['added_rules'],1);self.assertNotIn('PRIVATE',json.dumps(p))
        self.assertEqual(before,{p:p.read_bytes() for p in before})
        (self.root/'profiles'/'123.yaml').write_text('rules:\n - MATCH,Proxy\n')
        self.assertNotEqual(p['source_revision'],inspect_flclash(self.root)['source_revision'])

    def test_preview_save_offline_export_delete_never_apply(self):
        p=self.api.ipv6_plan(self.body())
        self.assertFalse(p['applied']);self.assertFalse(p['can_apply']);self.assertEqual(p['route'],'preserve')
        self.assertFalse(self.store.path.exists());self.assertIn('fake-ip',' '.join(p['warnings']))
        saved=self.api.ipv6_plan(self.body('save',fingerprint=p['fingerprint']))
        self.assertEqual(len(saved['plans']),1)
        reopened=IPv6Plans(self.store.path)
        exported=reopened.operate({'operation':'export','revision':1,'id':p['id']})
        self.assertNotIn('PRIVATE',exported['text']);self.assertFalse(json.loads(exported['text'])['applied'])
        reopened.operate({'operation':'delete','revision':1,'id':p['id']})
        self.assertEqual(reopened.catalog()['plans'],[])
        self.assertEqual({c[0] for c in self.core.mock_calls},{'ipv6_inventory'})

    def test_changed_core_config_or_revision_requires_new_preview(self):
        p=self.api.ipv6_plan(self.body())
        self.core.ipv6_inventory.return_value={**CORE,'rules_revision':'changed'}
        with self.assertRaises(DraftError):self.api.ipv6_plan(self.body('save',fingerprint=p['fingerprint']))
        self.assertFalse(self.store.path.exists())
        self.core.ipv6_inventory.return_value=CORE
        self.api.ipv6_plan(self.body('save',fingerprint=p['fingerprint']))
        with self.assertRaises(DraftError):self.api.ipv6_plan(self.body())

    def test_unverified_script_and_global_modes_are_explicit_blockers(self):
        state=inspect_flclash(self.root)
        p=build_plan(subject(),{**CORE,'mode':'global'},{**state,'persistence':'unsupported-overwrite-chain'})
        self.assertEqual(len(p['blockers']),2)
        self.assertFalse(p['can_apply'])
        p=build_plan(subject(),{'available':False},{'available':False})
        self.assertGreaterEqual(len(p['blockers']),3)

    def test_merge_and_exact_undo_preserve_old_routes_and_preexisting_identical_rule(self):
        original=['DOMAIN,ad.example,REJECT','DOMAIN,other.example,Proxy','MATCH,DIRECT'];rule=reject_rule(subject())
        changed,owned=merge_rules(original,rule)
        self.assertEqual(changed[1:],original);self.assertEqual(undo_rules(changed,owned),original)
        repeated,owned2=merge_rules(changed,rule)
        self.assertEqual(repeated,changed);self.assertEqual(owned2,[])
        with self.assertRaises(DraftError):undo_rules(['MATCH,Proxy']+changed,owned)

    def test_apply_and_write_failure_leave_real_config_untouched(self):
        before=(self.root/'config.yaml').read_bytes()
        with self.assertRaises(DraftError):self.api.ipv6_plan(self.body('apply'))
        p=self.api.ipv6_plan(self.body())
        with patch.object(Path,'replace',side_effect=OSError('write denied')):
            with self.assertRaises(DraftError):self.api.ipv6_plan(self.body('save',fingerprint=p['fingerprint']))
        self.assertFalse(self.store.path.exists());self.assertEqual((self.root/'config.yaml').read_bytes(),before)
    def test_rule_hit_counters_do_not_invalidate_configuration_fingerprint(self):
        rules=[{'type':'Domain','payload':'example.com','proxy':'DIRECT','size':-1,'extra':{'hitCount':1}}]
        client=ClashClient(lambda:'http://127.0.0.1:9090',lambda:'',fallbacks=())
        def get(url,timeout):
            if url.endswith('/version'):return {'version':'1.10.0','meta':True}
            if url.endswith('/configs'):return {'mode':'rule','ipv6':False,'tun':{'enable':False}}
            return {'rules':rules}
        with patch.object(client,'_get_json',side_effect=get):
            before=client.ipv6_inventory();rules[0]['extra']['hitCount']=99
            self.assertEqual(before,client.ipv6_inventory())
            rules[0]['proxy']='REJECT'
            self.assertNotEqual(before['rules_revision'],client.ipv6_inventory()['rules_revision'])


class IPv6Routes(ServerTestBase):
    def setUp(self):
        super().setUp();self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.plans=IPv6Plans(Path(self.tmp.name)/'plans.json')
        self.srv.app=MonitorApi(SimpleNamespace(ipv6_plans=self.plans))
    def test_auth_and_apply_rejection(self):
        code,_,_=self.request('GET','/api/ipv6-plans',headers={'X-FM-Token':None});self.assertEqual(code,403)
        code,_,raw=self.request('GET','/api/ipv6-plans');self.assertEqual(code,200);self.assertEqual(json.loads(raw)['plans'],[])
        code,_,_=self.request('POST','/api/ipv6-plan',{'operation':'apply','revision':0});self.assertEqual(code,400)
        self.assertFalse(self.plans.path.exists())
