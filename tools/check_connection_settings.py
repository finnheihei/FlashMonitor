"""IPv4 withdrawal regression: all requests intercepted, seeded drafts stay byte-identical."""
import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import urlparse
ROOT=Path(__file__).resolve().parent.parent
sys.path[:0]=[str(ROOT),str(ROOT/'tools')]
from check_gpt_network import GPTFixture
from connection_test_helpers import open_connection, open_catalog
from tests.test_network_policy import APP
from playwright.async_api import async_playwright

class Fixture(GPTFixture):
    def __init__(self):
        super().__init__();self.ambiguous=False;self.exited=False
        self.state['flows'][0]['process']='msedge'
        for subject in [{**APP,'include_subdomains':False},{'kind':'domain','value':'saved.example','include_subdomains':True}]:
            self.ipv4_store.operate({'operation':'save','revision':self.ipv4_store.catalog()['revision'],'subject':subject})
        self.rule_store.save('msedge','msedge.exe','direct','DIRECT',0)
        self.retained=self.ipv4_store.path.read_bytes();self.requests=[]
    def snapshot(self,view,period):
        d=super().snapshot(view,period)
        if 'flows' in d:
            d['flows']=[f for f in d['flows'] if f['process']!='msedge']+[{**self.state['flows'][0],'title':'Edge','rule_exe':'msedge.exe','path':'proxy','outbound':'proxy','n':1,'download':1000}]
            if self.ambiguous:d['flows'].append({**d['flows'][-1],'proc_path':r'D:\Other\browser.exe'})
            if self.exited:d['flows']=[f for f in d['flows'] if f['process']!='msedge']
        return d
    async def route(self,route):
        req=route.request;path=urlparse(req.url).path;self.requests.append({'method':req.method,'path':path})
        if path in ['/api/ipv4-draft','/api/network-apply','/api/network-undo']:
            await route.abort();raise AssertionError('retired or write endpoint requested: '+path)
        await super().route(route)

async def check(out):
    f=Fixture();passed=[];errors=[]
    def ok(name):passed.append(name);print('PASS '+name,flush=True)
    async with async_playwright() as p:
        browser=await p.chromium.launch(channel='msedge',headless=True,args=['--disable-background-networking'])
        ctx=await browser.new_context(viewport={'width':1100,'height':800},service_workers='block');await ctx.route('**/*',f.route)
        page=await ctx.new_page();page.set_default_timeout(12000);page.on('pageerror',lambda e:errors.append(str(e)))
        async def ready():await page.wait_for_function('gptCatalog && !gptBusy')
        async def absent():
            assert await page.locator('#ipv4-dialog,[id^=ipv4-],[data-ipv4-entry],[data-connection-mode]').count()==0
            text=await page.locator('body').inner_text();assert '仅 IPv4' not in text and 'IPv4 草稿' not in text
            assert await page.evaluate('typeof openIPv4Drafts')=='undefined'
        try:
            await page.goto('http://127.0.0.1:18799/?desk=1');await page.wait_for_selector('[data-program=msedge]');await absent()
            await open_connection(page);await ready();await absent()
            assert await page.locator('#gpt-dialog').is_visible() and await page.locator('dialog[open]').count()==1
            assert '只读诊断' in await page.locator('#connection-dialog').inner_text()
            assert await page.locator('#gpt-apply').is_disabled() and await page.locator('#gpt-apply').is_hidden()
            assert not any(r['path'].startswith('/api/ipv4') for r in f.requests)
            await page.locator('#connection-close').click();assert await page.locator('[data-connection-entry]').evaluate('e=>e===document.activeElement')
            await page.locator('#filters button').first.click()
            await page.wait_for_function("filter==='overview' && !poll.busy")
            ok('generic settings opens useful GPT diagnosis directly, no mode shell/IPv4 editor/hidden reentry/catalog fetch')
            await page.locator('[data-program=msedge]').click();trigger=page.locator('.program-detail [data-connection-process=msedge]');await trigger.click();await ready()
            assert await page.locator('#gpt-kind').input_value()=='application';assert await page.locator('#gpt-apps input:checked').count()==1;assert await page.locator('#gpt-apps input:checked').input_value()==APP['value']
            await page.locator('#gpt-target').select_option('GPT');await page.locator('#gpt-preview').click();await page.wait_for_function('!gptBusy');assert f.bodies[-1]['scope']==APP
            assert '未改变网络' in await page.locator('#gpt-result').inner_text()
            before=await page.evaluate('({filter,hash:location.hash,searchOpen})')
            for key in ['1','2','3','6','/']:
                await page.locator('#connection-close').focus();await page.keyboard.press(key);assert await page.evaluate('({filter,hash:location.hash,searchOpen})')==before
            for width,height in [(1100,800),(480,640),(640,360),(480,320)]:
                await page.set_viewport_size({'width':width,'height':height});await page.locator('#gpt-preview').click(trial=True)
                assert await page.locator('#connection-dialog').evaluate('e=>e.scrollWidth<=e.clientWidth');assert await page.locator('#connection-scope').evaluate('e=>e.clientHeight>=parseFloat(getComputedStyle(e).fontSize)');await page.screenshot(path=str(out/f'diagnosis-{width}x{height}.png'))
            await page.set_viewport_size({'width':1100,'height':800});await page.locator('#connection-close').click();assert await trigger.evaluate('e=>e===document.activeElement')
            ok('application exact context retains read-only preview, five modal shortcut guards, four viewport sizes and focus return')
            await page.locator('[data-program=msedge]').focus();await page.keyboard.press('Shift+F10');await page.locator('[data-menu-action=connection]').click();await ready();await absent();assert await page.locator('#gpt-apps input:checked').input_value()==APP['value'];await page.locator('#connection-close').click()
            await page.keyboard.press('2');await page.wait_for_function("filter==='rank' && !poll.busy");await absent()
            await page.locator('[data-rank-row=msedge] [data-connection-process]').click();await ready();assert await page.locator('#gpt-apps input:checked').input_value()==APP['value'];await page.locator('#connection-close').click()
            await page.locator('[data-fold="rank:msedge"]').click();await page.locator('[data-site-row="msedge.proxy.example.com"] [data-connection-domain]').click();await ready()
            assert await page.locator('#gpt-kind').input_value()=='domain';assert await page.locator('#gpt-domains').input_value()=='msedge.proxy.example.com';assert not await page.locator('[data-gpt-domain="0"]').is_checked()
            await page.locator('[data-gpt-domain="0"]').check();await page.locator('#gpt-refresh').click();await ready();assert await page.locator('[data-gpt-domain="0"]').is_checked();await page.locator('#connection-close').click()
            ok('right-click/Shift+F10, cumulative app and site shortcuts reach GPT directly; exact/suffix scope retained')
            for suffix in ['#view=ipv4','#filter=ipv4','#page=ipv4','#view=rank&page=ipv4','?mode=ipv4#ipv4-dialog']:
                await page.goto('http://127.0.0.1:18799/'+suffix);await page.wait_for_selector('#filters button');await absent();assert not await page.locator('#connection-dialog').evaluate('e=>e.open')
            ok('five old deep-link/query forms cannot expose or recreate the retired IPv4 flow')
            await open_catalog(page);await page.wait_for_selector('[data-saved-draft=msedge]');await page.locator('[data-saved-draft=msedge]').click();await page.wait_for_selector('#rule-actions:not([hidden])');assert await page.locator('#rule-export').is_enabled();assert await page.locator('#rule-save').is_hidden();await page.keyboard.press('Escape');await page.keyboard.press('Escape')
            f.exited=True;await page.reload();await page.wait_for_selector('#filters button');await absent()
            assert f.ipv4_store.path.read_bytes()==f.retained and len(f.ipv4_store.catalog()['drafts'])==2
            assert f.rule_store.catalog()['drafts'][0]['draft']['target']=='DIRECT'
            assert not any(r['path'].startswith('/api/ipv4') for r in f.requests)
            ok('seeded IPv4 draft file stays byte-identical across navigation/reload/exit; proxy/direct saved drafts remain usable')
            assert not errors,errors;assert set(f.posts)<= {'/api/network-preview'},f.posts
        finally:
            (out/'report.json').write_text(json.dumps({'passed':passed,'page_errors':errors,'requests':f.requests,'posts':f.posts,'retained_ipv4_sha256':hashlib.sha256(f.retained).hexdigest(),'retained_ipv4_unchanged':f.ipv4_store.path.read_bytes()==f.retained,'requests_intercepted':True,'real_state_modified':False},ensure_ascii=False,indent=2),encoding='utf-8')
            await browser.close();f.rule_tmp.cleanup()
if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path,required=True);a=ap.parse_args();a.output.mkdir(parents=True,exist_ok=True);asyncio.run(check(a.output))
