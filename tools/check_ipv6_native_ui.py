"""Isolated half-manual UI lifecycle; all API requests intercepted, client edits synthetic."""
import argparse
import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from connection_test_helpers import open_legacy
from playwright.async_api import async_playwright
from check_ipv6_ui import IPv6Fixture
from flashmonitor.ipv6_native import NativeVerifier, canonical_rule
from flashmonitor.ipv6_flow import IPv6Flow
from flashmonitor.ipv6_plans import build_plan, inspect_flclash, digest


class NativeFixture(IPv6Fixture):
    def __init__(self):
        super().__init__();self.root=Path(self.rule_tmp.name)
        self.rules=['DOMAIN,existing.example,DIRECT','MATCH,DIRECT']
        self.compile(self.rules)
        outer=self
        class ReadOnlyCore:
            def snapshot(self):return outer.rule_api.ctx.clash.ipv6_inventory()
            def runtime_rules(self):return [{'type':t,'payload':p,'proxy':target} for t,p,target in map(canonical_rule,outer.rules)]
        self.rule_api.ctx.ipv6_native=NativeVerifier(self.root,self.root/'monitor-ledger',ReadOnlyCore())
        native=self.rule_api.ctx.ipv6_native
        self.rule_api.ctx.ipv6_flow=IPv6Flow(self.rule_api.ctx.ipv6_plans,native,native.controller,
            lambda s:s['kind']=='process_name' or Path(s['value']).is_file())
        self.operations=[]
    async def route(self,route):
        if route.request.method=='POST':
            body=route.request.post_data_json;self.operations.append(body.get('operation'))
            if body.get('operation')=='resolve' and body.get('subject',{}).get('value')=='slow.example':await asyncio.sleep(.5)
        await super().route(route)
    def compile(self,rules):
        self.rules=list(rules);(self.root/'config.yaml').write_text(json.dumps({'rules':rules}),encoding='utf-8')
    def edit(self,sql,args=()):
        with closing(sqlite3.connect(self.root/'database.sqlite')) as c:c.execute(sql,args);c.commit()
    def native_save(self,plan):
        self.edit('INSERT INTO rules VALUES(22,"AND",?,"REJECT",NULL,NULL,0,0)',(plan['change']['content'],))
        self.edit('INSERT INTO profile_rule_mapping VALUES("123_22_added",123,22,"added","Zz")')


async def check(output):
    f=NativeFixture();passed=[];errors=[]
    app=f.root/'ChatGPT.exe';app.write_bytes(b'synthetic')
    old=build_plan({'kind':'process_path','value':str(app),'include_subdomains':False},f.rule_api.ctx.clash.ipv6_inventory(),inspect_flclash(f.root))
    old['schema_version']=1;old['persistent'].pop('target_root');old['persistent']['profile_id']=123
    f.rule_api.ctx.ipv6_plans.operate({'operation':'save','revision':0,'fingerprint':digest(old)},old)
    async with async_playwright() as p:
        browser=await p.chromium.launch(channel='msedge',headless=True,args=['--disable-background-networking'])
        ctx=await browser.new_context(viewport={'width':1000,'height':700},service_workers='block',permissions=['clipboard-read','clipboard-write'])
        await ctx.route('**/*',f.route);page=await ctx.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
        async def resolved():await page.wait_for_function('ipv6Context && !ipv6Busy')
        def ok(name):passed.append(name);print('PASS '+name,flush=True)
        try:
            await page.goto('http://127.0.0.1:18799/');await page.wait_for_function('last && !poll.busy')
            await open_legacy(page)
            await page.wait_for_function('ipv6Data && !ipv6Busy')
            await page.locator('[data-ipv6-view]').click();await resolved()
            assert 'ChatGPT.exe' in await page.locator('#ipv6-summary').inner_text()
            before=(f.root/'database.sqlite').read_bytes()
            await page.locator('#ipv6-start').click();await page.wait_for_function('ipv6Context?.resume_id && !ipv6Busy')
            current=f.rule_api.ctx.ipv6_plans.read()['plans'][old['id']]
            assert current['schema_version']==2 and current['persistent']['target_root']==str(f.root.resolve())
            assert (f.root/'database.sqlite').read_bytes()==before
            assert await page.evaluate('navigator.clipboard.readText()')==current['change']['content']
            assert await page.locator('#ipv6-save').count()==0 and await page.locator('#ipv6-refresh').count()==0
            ok('legacy missing-root/int-ID draft continues with one primary click, rebound context and automatic copy; no separate save/prepare')
            # No client row was saved, so deleting this pending intent is safe.
            await page.locator('[data-ipv6-delete]').click();await page.wait_for_function('ipv6Data.plans.length===0')
            await page.locator('#ipv6-kind').select_option('domain');await page.locator('#ipv6-domain').fill('slow.example')
            await page.wait_for_timeout(330);await page.locator('#ipv6-domain').fill('fast.example');await resolved()
            await page.wait_for_timeout(600)
            assert 'fast.example' in await page.locator('#ipv6-summary').inner_text()
            assert await page.evaluate('ipv6Context.plan.subject.value')=='fast.example'
            token=await page.evaluate('ipv6Context.context_token');f.rule_api.ctx.ipv6_flow.tokens[token]['created']-=301
            await page.locator('#ipv6-start').click();await resolved()
            assert 'fast.example' in await page.locator('#ipv6-domain').input_value()
            ok('slow A cannot overwrite B; expired begin refreshes server context while retaining selected object')
            await page.locator('#ipv6-kind').select_option('application');await page.locator('#ipv6-app').select_option(['msedge.exe','steam.exe']);await resolved()
            assert await page.evaluate('ipv6Context.plan.subject.subjects.length')==2
            await page.locator('#ipv6-start').click();await page.wait_for_function('ipv6Context?.resume_id && !ipv6Busy')
            plan=f.rule_api.ctx.ipv6_plans.catalog()['plans'][0]
            assert '(OR,' in plan['rule']
            await page.wait_for_function("document.getElementById('ipv6-native-status').textContent.includes('等待在FlClash保存')")
            f.native_save(plan)
            await page.wait_for_function("document.getElementById('ipv6-native-status').textContent.includes('等待客户端应用')")
            f.compile([plan['rule'],'DOMAIN,existing.example,DIRECT','MATCH,DIRECT'])
            await page.wait_for_function("document.getElementById('ipv6-native-status').textContent.includes('IPv6规则已启用')")
            ok('two selected applications form one OR rule; automatic readonly polling distinguishes save/apply/verified with no manual verify click')
            await page.locator('#ipv6-rule-export').locator('..').locator('summary').click()
            async with page.expect_download() as download:await page.locator('#ipv6-rule-export').click()
            file=await download.value;await file.save_as(output/'rule.txt');assert plan['rule'] in (output/'rule.txt').read_text(encoding='utf-8')
            await page.locator('#ipv6-close').click();await page.wait_for_timeout(300)
            count=f.operations.count('native_verify');await page.wait_for_timeout(2400);assert f.operations.count('native_verify')==count
            await page.evaluate("gotoFilter('rank')");await page.wait_for_function('!poll.busy')
            await open_legacy(page)
            await page.wait_for_function('ipv6Dialog.open && ipv6Data && !ipv6Busy');await page.locator('[data-ipv6-view]').click();await resolved()
            await page.wait_for_function("document.getElementById('ipv6-undo').hidden===false")
            ok('closing stops polling; advanced entry resumes same receipt and immediately rechecks current state')
            await page.locator('#ipv6-undo').click();await page.wait_for_function('!ipv6Busy')
            undo=await page.locator('#ipv6-native-undo').inner_text()
            assert 'msedge.exe' in undo and 'steam.exe' in undo and '完整内容' in undo
            assert 'ID 22' not in undo and '123_22_added' not in undo
            f.edit('DELETE FROM profile_rule_mapping WHERE id="123_22_added"');f.edit('DELETE FROM rules WHERE id=22')
            await page.evaluate('window.dispatchEvent(new Event("focus"))')
            await page.wait_for_function("document.getElementById('ipv6-native-status').textContent.includes('等待客户端应用撤销')")
            f.compile(['DOMAIN,existing.example,DIRECT','MATCH,DIRECT'])
            await page.wait_for_function("document.getElementById('ipv6-native-status').textContent.includes('限制已撤销')")
            ok('human-visible exact AND/content/REJECT guide replaces hidden database IDs; auto verifies grouped undo retaining unrelated rule')
            for width,height in [(640,360),(480,320)]:
                await page.set_viewport_size({'width':width,'height':height})
                assert await page.locator('#ipv6-dialog').evaluate('e=>e.scrollWidth<=e.clientWidth')
                rect=await page.locator('#ipv6-dialog > .rule-actions').bounding_box();assert rect['y']+rect['height']<=height
            await page.screenshot(path=str(output/'native-480.png'))
            assert all(path=='/api/ipv6-plan' for path in f.writes)
            assert not errors,errors
            ok('original compact modal, copy/export details and focus behavior remain; no client write/reload API invoked')
        finally:
            (output/'report.json').write_text(json.dumps({'passed':passed,'page_errors':errors,'operations':f.operations,'real_client_written':False},ensure_ascii=False,indent=2),encoding='utf-8')
            await browser.close();f.flclash.stop();f.rule_tmp.cleanup()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True);asyncio.run(check(args.output))
