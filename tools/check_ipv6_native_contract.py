"""Synthetic client-save/compile contract + actual isolated core, NOT a GUI acceptance."""
import argparse
import json
import socket
import sqlite3
import sys
import threading
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from check_ipv6_core import IsolatedCore, Echo, probe
from test_ipv6_plans import fixture
from flashmonitor.clash import ClashClient, local_opener
from flashmonitor.ipv6_executor import ControllerReload
from flashmonitor.ipv6_native import NativeVerifier
from flashmonitor.ipv6_plans import build_plan, inspect_flclash


def check(binary,output):
    output.mkdir(parents=True,exist_ok=False)
    root=output/'synthetic-client';root.mkdir();fixture(root)
    core=None;echo4=None;echo6=None;server=None;thread=None
    results=[];report={'native_gui_tested':False,'client_provider_drift_tested':False,
        'scope':'synthetic client transaction/compiler; existing independent core; loopback only',
        'real_client_written':False,'system_network_changed':False,'production_button_unlocked':False,'passed':results}
    def passed(name,**evidence):
        results.append({'name':name,**evidence});print('PASS '+name,flush=True)
    def execute_sql(statements):
        with closing(sqlite3.connect(root/'database.sqlite')) as c:
            c.execute('PRAGMA foreign_keys=ON')
            with c:
                for sql,args in statements:c.execute(sql,args)
    try:
        # Synthetic large client IDs, global + disabled mappings, unrelated rules.
        managed_id=9007199254741023;global_id=9007199254741024;disabled_id=9007199254741025
        execute_sql([
            ('INSERT INTO rules VALUES(?,"DOMAIN","global.example","DIRECT",NULL,NULL,0,0)',(global_id,)),
            ('INSERT INTO profile_rule_mapping VALUES(?,NULL,?,NULL,"a0")',(str(global_id),global_id)),
            ('INSERT INTO rules VALUES(?,"DOMAIN","disabled.example","DIRECT",NULL,NULL,0,0)',(disabled_id,)),
            ('INSERT INTO profile_rule_mapping VALUES(?,NULL,?,NULL,"a1")',(str(disabled_id),disabled_id)),
            ('INSERT INTO profile_rule_mapping VALUES(?,123,?,"disabled","a0")',(f'123_{disabled_id}_disabled',disabled_id)),
        ])
        unrelated=['DOMAIN,existing.example,DIRECT','DOMAIN,global.example,DIRECT']
        subscription=['DOMAIN,subscription-v1.example,DIRECT','MATCH,DIRECT']
        core=IsolatedCore(binary,root);echo4=Echo(socket.AF_INET);echo6=Echo(socket.AF_INET6)
        with socket.socket() as free:free.bind(('127.0.0.1',0));port=free.getsockname()[1]
        core.setup(unrelated+subscription,port);core.call('startListener')
        client=ClashClient(lambda:f'http://127.0.0.1:{core.controller}',lambda:'',fallbacks=())
        control=ControllerReload(client)
        plan=build_plan({'kind':'selection','subjects':[{'kind':'process_path','value':str(Path(sys.executable)),
                         'include_subdomains':False},{'kind':'domain','value':'selected.example','include_subdomains':False}]},control.snapshot(),inspect_flclash(root))
        verifier=NativeVerifier(root,output/'monitor-ledger',control)
        assert verifier.prepare(plan)['state']=='awaiting_client_save'
        assert verifier.verify(plan)['state']=='awaiting_client_save'
        assert probe(port,('::1',echo6.port))
        execute_sql([
            ('INSERT INTO rules VALUES(?,"AND",?,"REJECT",NULL,NULL,0,0)',(managed_id,plan['change']['content'])),
            ('INSERT INTO profile_rule_mapping VALUES(?,123,?,"added","Zz")',(f'123_{managed_id}_added',managed_id)),
        ])
        receipt=verifier.verify(plan);assert receipt['state']=='persisted_unverified'
        assert receipt['client_rule_id']==str(managed_id)
        passed('synthetic native save identified canonical TEXT mapping and string large ID; no premature runtime claim',state=receipt['state'])
        core.setup([plan['rule']]+unrelated+subscription,port)
        receipt=verifier.verify(plan);assert receipt['state']=='runtime_verified'
        assert receipt['applied'] is False and receipt['behavior_verified'] is False
        assert not probe(port,('::1',echo6.port)) and probe(port,('127.0.0.1',echo4.port))
        passed('isolated ordered runtime matches generated and persistent rules; selected IPv6 rejected, IPv4 retained',state=receipt['state'])
        # A real loopback download with changed bytes, but deliberately not claimed
        # as FlClash Profile.update()/provider invalidation/native compilation.
        document={'rules':subscription}
        class SubscriptionHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                body=json.dumps(document).encode();self.send_response(200)
                self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),SubscriptionHandler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        op=local_opener();url=f'http://127.0.0.1:{server.server_port}/test-profile'
        with op.open(url,timeout=2) as response:first=response.read()
        subscription=['DOMAIN,subscription-v2.example,DIRECT','MATCH,DIRECT'];document['rules']=subscription
        with op.open(url,timeout=2) as response:second=response.read()
        assert first!=second
        (root/'profiles'/'123.yaml').write_bytes(second)
        execute_sql([('UPDATE profiles SET last_update_date=2 WHERE id=123',())])
        core.setup([plan['rule']]+unrelated+subscription,port)
        receipt=verifier.verify(plan)
        assert receipt['state']=='runtime_verified' and receipt['subscription_refresh_verified'] is False
        assert [r.get('payload') for r in control.runtime_rules()][1:]==['existing.example','global.example','subscription-v2.example','']
        assert not probe(port,('::1',echo6.port))
        passed('simulated subscription change and recompile retains owned and unrelated rules; native refresh remains unverified',state=receipt['state'])
        before=(root/'database.sqlite').read_bytes();undo=verifier.undo_instructions(plan)
        assert before==(root/'database.sqlite').read_bytes() and undo['automatic_revoke'] is False
        assert undo['rule_id']==str(managed_id)
        execute_sql([
            ('DELETE FROM profile_rule_mapping WHERE id=?',(undo['mapping_key'],)),
            ('DELETE FROM rules WHERE id=?',(managed_id,)),
        ])
        assert verifier.verify(plan)['state']=='persistent_removed_runtime_pending'
        core.setup(unrelated+subscription,port)
        receipt=verifier.verify(plan);assert receipt['state']=='revoked_verified'
        assert probe(port,('::1',echo6.port))
        assert [r.get('payload') for r in control.runtime_rules()]==['existing.example','global.example','subscription-v2.example','']
        passed('synthetic differential native undo retains unrelated rules and refreshed subscription; actual IPv6 restored',state=receipt['state'])
        report['controller_port']=core.controller;report['mixed_port']=port
        report['tun_enabled']=control.snapshot().get('tun_enabled')
    finally:
        if server:server.shutdown();server.server_close();thread.join(2)
        if core:core.close()
        if echo4:echo4.close()
        if echo6:echo6.close()
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();check(args.binary,args.output)
