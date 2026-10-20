import hashlib
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from flashmonitor.ipv6_bridge import DirectBridge, LoopbackSession, REQUIRED
from flashmonitor.ipv6_plans import build_plan
from flashmonitor.rule_drafts import DraftError
from test_ipv6_plans import CORE, subject


class FakeVerifiedSession:
    def __init__(self):
        self.peer_verified=True;self.instance_id='a'*32;self.root_identity='root-fixture';self.calls=[];self.receipts={};self.drop=False
        self.facts={'protocol':1,'schema_version':4,'instance_id':self.instance_id,'root_identity':self.root_identity,
                    'profile_id':'9007199254741023','revision':'rev-1','profile_mode':'standard','mode':'rule',**dict.fromkeys(REQUIRED,True)}
    def call(self,action,body):
        self.calls.append((action,body))
        if action=='capabilities':return dict(self.facts)
        operation=body['operation_id']
        if action=='prepare':
            from flashmonitor.ipv6_plans import reject_rule
            scope={'kind':'selection','subjects':body['subjects']}
            rule_hash=hashlib.sha256(reject_rule(scope).encode()).hexdigest()
            self.receipts[operation]={'operation_id':operation,'profile_id':body['profile_id'],'instance_id':self.instance_id,
                'state':'prepared','rule_id':'9007199254741024','mapping_id':body['profile_id']+'_9007199254741024_added',
                'persistent_rule_hash':rule_hash,'compiled_rule_hash':rule_hash,'runtime_rule_hash':rule_hash,
                'nonrules_before':'fixed','nonrules_after':'fixed','unmanaged_before':'old','unmanaged_after':'old','ordered_verified':True}
            return {'operation_id':operation,'profile_id':body['profile_id'],'rule_hash':rule_hash,'revision':body['expected_revision'],'token':'b'*64}
        if action=='commit':
            self.receipts[operation]['state']='verified'
            if self.drop:self.drop=False;raise TimeoutError('reply lost after durable commit')
        elif action=='undo':self.receipts[operation].update(state='revoked',owned_absent=True)
        return dict(self.receipts[operation])


class BridgeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.session=FakeVerifiedSession();self.bridge=DirectBridge(self.session)
        self.plan=build_plan(subject(),CORE,{'available':True,'persistence':'profile-added-rules','profile_id':'9007199254741023'})
    def prepare(self):return self.bridge.prepare(self.plan)['operation_id']
    def test_no_session_no_available_and_no_fallback(self):
        bridge=DirectBridge();self.assertFalse(bridge.capabilities()['available'])
        with self.assertRaises(DraftError):bridge.prepare(self.plan)
    def test_private_transport_default_denies_and_does_not_expose_secret(self):
        session=LoopbackSession('http://127.0.0.1:1234/bridge','b'*64,'a'*32,'root-fixture')
        self.assertNotIn('b'*64,repr(session));self.assertFalse(DirectBridge(session).capabilities()['available'])
        with self.assertRaises(DraftError):session.call('capabilities',{})
        for endpoint in ['http://example.com/bridge','http://127.0.0.1:1234/bridge?x=1','http://user@127.0.0.1:1234/bridge']:
            with self.assertRaises(DraftError):LoopbackSession(endpoint,'b'*64,'a'*32,'root')
    def test_each_client_acceptance_gate_is_mandatory(self):
        for name in REQUIRED:
            with self.subTest(name=name):
                self.session.facts[name]=False
                with self.assertRaises(DraftError):self.prepare()
                self.session.facts[name]=True
        self.assertEqual({x[0] for x in self.session.calls},{'capabilities'})
    def test_identity_schema_mode_and_peer_fail_before_prepare(self):
        for name,value in [('instance_id','wrong'),('root_identity','wrong'),('schema_version',3),('mode','global'),('profile_mode','script')]:
            old=self.session.facts[name];self.session.facts[name]=value
            with self.assertRaises(DraftError):self.prepare()
            self.session.facts[name]=old
        self.session.peer_verified=False
        with self.assertRaises(DraftError):self.prepare()
    def test_profile_change_refused_before_client_prepare(self):
        self.session.facts['profile_id']='123'
        with self.assertRaisesRegex(DraftError,'profile_changed'):self.prepare()
        self.assertEqual({x[0] for x in self.session.calls},{'capabilities'})
    def test_commit_ordered_receipt_large_string_ids_and_precise_undo(self):
        operation=self.prepare();self.assertTrue(self.bridge.commit(operation)['applied'])
        self.assertEqual(self.bridge.status(operation)['state'],'verified')
        self.assertEqual(self.bridge.undo(operation)['state'],'revoked')
        self.assertFalse(self.bridge.undo(operation)['applied'])
        self.assertNotIn('token',str(self.bridge.status(operation)))
    def test_lost_commit_reply_queries_same_operation_never_repeats_write(self):
        operation=self.prepare();self.session.drop=True
        self.assertEqual(self.bridge.commit(operation)['state'],'unknown')
        self.assertEqual(self.bridge.commit(operation)['state'],'verified')
        self.assertEqual(sum(x[0]=='commit' for x in self.session.calls),1)
    def test_reconnect_to_other_instance_refused_before_write(self):
        operation=self.prepare();self.session.instance_id='c'*32;self.session.facts['instance_id']=self.session.instance_id
        with self.assertRaises(DraftError):self.bridge.commit(operation)
        self.assertFalse(any(x[0]=='commit' for x in self.session.calls))
    def test_runtime_or_unmanaged_mismatch_cannot_be_reported_applied(self):
        for field in ['runtime_rule_hash','mapping_id','nonrules_after','unmanaged_after','ordered_verified']:
            operation=self.prepare();self.session.receipts[operation][field]='changed'
            with self.subTest(field=field),self.assertRaises(DraftError):self.bridge.commit(operation)
            self.assertEqual(self.bridge.pending[operation]['outcome'],'unknown')
    def test_undo_cannot_succeed_without_absence_receipt(self):
        operation=self.prepare();self.bridge.commit(operation)
        original=self.session.call
        def missing(action,body):
            result=original(action,body)
            if action=='undo':result.pop('owned_absent')
            return result
        self.session.call=missing
        with self.assertRaisesRegex(DraftError,'outcome_unknown'):self.bridge.undo(operation)
    def test_invalid_commit_reply_never_causes_a_second_commit(self):
        operation=self.prepare();original=self.session.call
        def invalid(action,body):
            result=original(action,body)
            return [] if action=='commit' else result
        self.session.call=invalid
        with self.assertRaisesRegex(DraftError,'outcome_unknown'):self.bridge.commit(operation)
        self.assertEqual(self.bridge.commit(operation)['state'],'verified')
        self.assertEqual(sum(action=='commit' for action,_ in self.session.calls),1)
    def test_loopback_contract_roundtrip_with_synthetic_client(self):
        fake=self.session
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                if self.headers.get('Authorization')!='Bearer '+'b'*64 or self.headers.get('Origin'):
                    self.send_response(403);self.end_headers();return
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                result=fake.call(body['action'],body['params']);payload=json.dumps(result).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def cleanup():server.shutdown();server.server_close();thread.join(2)
        self.addCleanup(cleanup)
        session=LoopbackSession(f'http://127.0.0.1:{server.server_port}/bridge','b'*64,fake.instance_id,fake.root_identity,peer_verified=True)
        bridge=DirectBridge(session);operation=bridge.prepare(self.plan)['operation_id']
        self.assertTrue(bridge.commit(operation)['applied'])
        self.assertEqual(bridge.undo(operation)['state'],'revoked')
