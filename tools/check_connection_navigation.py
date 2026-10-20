"""Isolated navigation regression: real clicks, reused DOM actions and delayed snapshots."""
import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from check_ui import Fixture
from playwright.async_api import async_playwright


class NavigationFixture(Fixture):
    def __init__(self, source):
        super().__init__();self.source=source;self.gate=None;self.started=asyncio.Event();self.delayed=False
    async def route(self,route):
        url=urlparse(route.request.url)
        if url.hostname=='127.0.0.1' and url.port==18799:
            if url.path=='/' and self.source:
                await route.fulfill(content_type='text/html',body=self.source.read_text(encoding='utf-8'));return
            if url.path=='/api/snapshot' and self.gate and not self.delayed:
                self.delayed=True
                q=parse_qs(url.query);data=self.snapshot(q.get('view',['overview'])[0],q.get('period',['today'])[0])
                self.started.set();await self.gate.wait()
                await route.fulfill(content_type='application/json',body=json.dumps(data));return
        await super().route(route)


TRACE="""window.navTrace=[];
document.addEventListener('click',e=>{const b=e.target.closest('button');if(b) navTrace.push({phase:'capture',text:b.textContent,attrs:[...b.attributes].map(a=>a.name),hash:location.hash});},true);
window.addEventListener('click',e=>{const b=e.target.closest('button');if(b) navTrace.push({phase:'bubble',text:b.textContent,attrs:[...b.attributes].map(a=>a.name),hash:location.hash});});
window.addEventListener('popstate',()=>navTrace.push({phase:'popstate',hash:location.hash}));
"""


async def check(output,source,expect_bug):
    f=NavigationFixture(source);results=[];errors=[]
    def passed(name):results.append(name);print('PASS '+name,flush=True)
    async with async_playwright() as p:
        browser=await p.chromium.launch(channel='msedge',headless=True,args=['--disable-background-networking'])
        ctx=await browser.new_context(viewport=dict(width=1000,height=700),service_workers='block')
        await ctx.route('**/*',f.route);await ctx.add_init_script(TRACE)
        page=await ctx.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
        async def home():
            f.gate=None;f.delayed=False;f.started.clear()
            await page.goto('http://127.0.0.1:18799/')
            await page.wait_for_function('last?.flows && !poll.busy')
        async def connections():
            await page.wait_for_function("filter==='conns' && connMode==='live' && processScope==='' && !poll.busy")
            await page.wait_for_selector('#context-tools [data-conn-mode=live].on')
            assert await page.locator('#context-tools [data-back]').count()==1
            assert await page.locator('#main [data-close]').count()>0
        try:
            await home();await page.locator('#context-tools [data-connections]').click()
            if expect_bug:
                await page.wait_for_function("filter==='overview'")
                trace=await page.evaluate('navTrace')
                assert any(t['phase']=='capture' and 'data-connections' in t['attrs'] for t in trace)
                assert any(t['phase']=='bubble' and 'data-back' in t['attrs'] for t in trace),trace
                passed('pre-fix reproduced: all-connections button becomes back during its own click and returns to overview')
                await page.screenshot(path=str(output/'before.png'));return
            await connections()
            for _ in range(3):
                await page.evaluate('{tick(false)}');await page.wait_for_function('!poll.busy')
                assert await page.evaluate('filter')=='conns'
            passed('mouse all-connections stays in live connections through automatic refresh; one navigation per click')
            for key in ['Enter','Space']:
                await home();await page.locator('#context-tools [data-connections]').focus();await page.keyboard.press(key)
                await connections()
            passed('Enter and Space open connections without reinterpreting the focused button as back')
            await home();f.gate=asyncio.Event()
            await page.evaluate('{tick(false)}');await asyncio.wait_for(f.started.wait(),2)
            await page.locator('#context-tools [data-connections]').click()
            assert await page.evaluate('filter')=='conns'
            f.gate.set();await connections()
            passed('old overview snapshot returns after click: current view and connection toolbar remain live connections')
            await page.locator('#context-tools [data-conn-mode=closed]').click()
            await page.wait_for_function("connMode==='closed' && !poll.busy")
            await page.locator('#context-tools [data-conn-mode=live]').click();await connections()
            await page.locator('#context-tools [data-back]').click()
            await page.wait_for_function("connMode==='closed'")
            await page.go_back();await page.wait_for_function("filter==='conns' && connMode==='live'")
            await page.go_back();await page.wait_for_function("filter==='overview'")
            await page.go_forward();await connections()
            passed('live/closed modes and deliberate history back/forward each perform their intended action once')
            await home()
            await page.locator('[data-program=msedge]').click()
            await page.locator('#main [data-connections=msedge]').click()
            await page.wait_for_function("filter==='conns' && processScope==='msedge' && !poll.busy")
            assert await page.locator('#context-tools [data-clear-scope]').is_visible()
            await page.locator('#context-tools [data-clear-scope]').click()
            assert await page.evaluate('processScope')==''
            passed('program-scoped connections and clear-scope retain identity and navigation behavior')
            await page.reload();await connections()
            await page.screenshot(path=str(output/'after.png'))
            assert not errors,errors
            passed('reload keeps connection route; no page errors or unexpected document/window navigation')
        finally:
            if f.gate:f.gate.set()
            (output/'report.json').write_text(json.dumps(dict(passed=results,page_errors=errors,trace=await page.evaluate('navTrace')),ensure_ascii=False,indent=2),encoding='utf-8')
            await browser.close();f.rule_tmp.cleanup()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--source',type=Path);parser.add_argument('--expect-bug',action='store_true')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    asyncio.run(check(args.output,args.source,args.expect_bug))
