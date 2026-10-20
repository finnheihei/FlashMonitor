"""Isolated UI for read-only IPv6 change previews; no real controller/config access."""
import argparse
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlparse
from unittest.mock import patch
from check_ui import Fixture
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from test_ipv6_plans import fixture as metadata_fixture, CORE
from flashmonitor.ipv6_plans import IPv6Plans


class IPv6Fixture(Fixture):
    def __init__(self):
        super().__init__();root=Path(self.rule_tmp.name);metadata_fixture(root)
        self.rule_api.ctx.ipv6_plans=IPv6Plans(root/'plans.json');self.changed=False;self.exited=False;self.writes=[]
        self.rule_api.ctx.clash.ipv6_inventory=lambda:{**CORE,'rules_revision':'changed' if self.changed else 'old'}
        self.flclash=patch('flashmonitor.app.flclash_dir',return_value=root);self.flclash.start()
    def snapshot(self,view,period):
        data=super().snapshot(view,period)
        if self.exited:data['flows']=[];data['live']['eaters']=[]
        return data
    async def route(self,route):
        path=urlparse(route.request.url).path
        if route.request.method=='POST':self.writes.append(path)
        if path=='/api/ipv6-plans':data=self.rule_api.ipv6_catalog()
        elif path=='/api/ipv6-plan':
            try:data=self.rule_api.ipv6_plan(route.request.post_data_json)
            except ValueError as exc:
                await route.fulfill(status=400,content_type='application/json',body=json.dumps({'error':str(exc)}));return
        else:await super().route(route);return
        await route.fulfill(content_type='application/json',body=json.dumps(data))


async def check(output):
    from check_ipv6_native_ui import check as flow_check
    await flow_check(output)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True);asyncio.run(check(args.output))
