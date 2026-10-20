"""End-to-end temporary FlClash DB + existing independent core. No real profile writes."""
import argparse
import json
import socket
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from check_ipv6_core import IsolatedCore, Echo, probe
from test_ipv6_plans import fixture
from flashmonitor.clash import ClashClient
from flashmonitor.ipv6_plans import build_plan, digest, inspect_flclash
from flashmonitor.ipv6_executor import IPv6Executor, ControllerReload
from flashmonitor.rule_drafts import DraftError


def check(binary,output):
    output.mkdir(parents=True,exist_ok=True);root=output/'temporary-clash';root.mkdir(exist_ok=True)
    fixture(root);core=IsolatedCore(binary,root);echo=Echo(socket.AF_INET6);results=[]
    def passed(name):results.append(name);print('PASS '+name,flush=True)
    try:
        with socket.socket() as free:free.bind(('127.0.0.1',0));port=free.getsockname()[1]
        core.setup(['DOMAIN,existing.example,DIRECT','MATCH,DIRECT'],port);core.call('startListener')
        client=ClashClient(lambda:f'http://127.0.0.1:{core.controller}',lambda:'',fallbacks=())
        control=ControllerReload(client)
        s={'kind':'process_path','value':str(Path(sys.executable)),'include_subdomains':False}
        plan=build_plan(s,control.snapshot(),inspect_flclash(root));grant={'target_root':str(root.resolve()),'fingerprint':digest(plan),'profile_id':123,'subject_id':plan['id'],'allow_reload':True}
        executor=IPv6Executor(root,output/'backups',control,grant,experimental=True)
        assert probe(port,('::1',echo.port))
        now=control.snapshot()
        if now!=plan['core']:print('SNAPSHOT_DIFFERENCE '+json.dumps({k:[plan['core'].get(k),v] for k,v in now.items() if plan['core'].get(k)!=v}),flush=True)
        assert executor.apply(plan)['applied'];assert not probe(port,('::1',echo.port))
        assert executor.apply(plan)['duplicate']
        passed('persistent transaction + backup + actual PUT reload + published rule blocks selected IPv6; duplicate apply is idempotent')
        assert not executor.revoke(plan)['applied'];assert probe(port,('::1',echo.port))
        passed('actual revoke removes owned rule and reloads original routing; IPv6 loopback restored')
        plan=build_plan(s,control.snapshot(),inspect_flclash(root));executor.consent={**grant,'fingerprint':digest(plan)}
        real=control.reload;failed=[False]
        def refusal(path):
            if not failed[0]:failed[0]=True;raise OSError('injected candidate reload refusal')
            real(path)
        control.reload=refusal
        try:executor.apply(plan);raise AssertionError('failure expected')
        except DraftError:pass
        control.reload=real
        assert executor.status(plan['id'])['state']=='rolled_back';assert probe(port,('::1',echo.port))
        passed('injected reload failure restores persistent rows and actual core; no leftover IPv6 block')
        (root/'profiles'/'123.yaml').write_text('rules:\n - MATCH,Updated\n',encoding='utf-8')
        calls=[];real=control.reload
        def counted(path):calls.append(path);return real(path)
        control.reload=counted
        try:executor.apply(plan);raise AssertionError('conflict expected')
        except DraftError:pass
        assert calls==[];assert probe(port,('::1',echo.port))
        passed('subscription source change rejects stale consent before write/reload; original core remains usable')
    finally:
        core.close();echo.close()
        (output/'report.json').write_text(json.dumps({'passed':results,'real_config_written':False,'scope':'temporary database/home, independent loopback core'},ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--binary',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();check(args.binary,args.output)
