"""Resolve real legacy intents read-only; never begin or write native/client state."""
import argparse
import hashlib
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flashmonitor.clash import ClashClient
from flashmonitor.ipv6_executor import ControllerReload, IPv6Executor
from flashmonitor.ipv6_flow import IPv6Flow
from flashmonitor.ipv6_native import NativeVerifier
from flashmonitor.ipv6_plans import IPv6Plans
from flashmonitor.app import flclash_dir


def check(data_dir,output):
    settings=json.loads((data_dir/'settings.json').read_text(encoding='utf-8'))
    client=ClashClient(lambda:settings.get('clash_api','http://127.0.0.1:9090'),lambda:settings.get('clash_secret',''),fallbacks=())
    class ReadOnlyController(ControllerReload):
        def reload(self,*args):raise AssertionError('readonly probe cannot reload')
    control=ReadOnlyController(client);root=flclash_dir();plans=IPv6Plans(data_dir/'ipv6-plans.json')
    native=NativeVerifier(root,data_dir/'ipv6-native',control)
    flow=IPv6Flow(plans,native,control,lambda s:Path(s['value']).is_file(),IPv6Executor(root,data_dir/'ipv6-backups',control))
    tracked=[plans.path]+list(native.ledger.glob('*.json'))
    before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in tracked}
    cases=[]
    for plan in plans.read()['plans'].values():
        if plan['persistent'].get('target_root'):continue
        case={'object':Path(plan['subject']['value']).name,'schema_before':plan.get('schema_version')}
        try:
            result=flow.resolve({'operation':'resolve','id':plan['id']})
            case.update(resolved=True,legacy_rebound=result.get('legacy_rebound'),schema_resolved=result['plan']['schema_version'],
                        root_bound=bool(result['plan']['persistent'].get('target_root')),
                        profile_id_is_string=isinstance(result['plan']['persistent'].get('profile_id'),str),
                        token_bound=bool(result.get('context_token')),capability=result['capability'])
        except ValueError as exc:case.update(resolved=False,error_code=str(exc).split('：')[0])
        cases.append(case)
    unchanged=before=={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in tracked}
    assert unchanged and set(native.ledger.glob('*.json'))==set(tracked[1:])
    report={'scope':'real metadata/core GET only; resolve memory tokens only; no begin/prepare/verify',
            'cases':cases,'monitor_data_unchanged':unchanged,'client_written':False,'ledger_written':False}
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--data-dir',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();check(args.data_dir,args.output)
