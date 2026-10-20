"""Isolated manual refresh acceptance; every browser request is intercepted."""
import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlparse,parse_qs

from check_gpt_network import GPTFixture
from playwright.async_api import async_playwright


class RefreshFixture(GPTFixture):
    def __init__(self):
        super().__init__();self.generation=0;self.supported=True;self.refresh_delay=.4
        self.refresh_calls=[];self.failure_mode='';self.stale_started=asyncio.Event();self.stale_release=asyncio.Event();self.stale_once=False

    def snapshot(self,view,period):
        result=super().snapshot(view,period)
        result.update(manual_refresh_supported=self.supported,refresh_marker=self.generation)
        if not self.supported:result.pop('manual_refresh_supported')
        for flow in result.get('flows',[]):flow['host']='refresh-'+str(self.generation)+'.example'
        return result

    def receipt(self,period):
        result=self.snapshot('overview','today')
        result['rank'].update({p:self.snapshot('rank',p)['rank'][p] for p in set(('all',period))})
        result['closed']=self.snapshot('closed','today')['closed']
        catalog=self.service.catalog();catalog['inventory']['nodes']=['fresh-node-'+str(self.generation)]
        catalog['inventory']['groups']['fresh-group-'+str(self.generation)]={'type':'Selector','members':catalog['inventory']['nodes'],'current':catalog['inventory']['nodes'][0],'may_direct':False}
        return {'ok':True,'snapshot':result,'catalog':catalog}

    async def route(self,route):
        req=route.request;u=urlparse(req.url)
        if u.path=='/api/refresh':
            assert req.method=='POST';body=req.post_data_json;self.refresh_calls.append(body);self.posts.append(u.path)
            self.generation+=1
            data=self.receipt(body['period'])
            await asyncio.sleep(self.refresh_delay)
            if self.failure_mode=='http':await route.fulfill(status=503,json={'error':'synthetic controller unavailable'});return
            if self.failure_mode=='core':data={'ok':False,'error':'synthetic read failed'}
            if self.failure_mode=='incomplete':data['catalog']['inventory']['consistent']=False
            await route.fulfill(json=data);return
        if u.path=='/api/snapshot' and self.stale_once:
            self.stale_once=False;q=parse_qs(u.query);data=self.snapshot(q.get('view',['overview'])[0],q.get('period',['today'])[0])
            self.stale_started.set();await self.stale_release.wait();await route.fulfill(json=data);return
        await super().route(route)


async def check(out):
    f=RefreshFixture();checks=[];errors=[]
    def ok(name):checks.append(name);print('PASS '+name,flush=True)
    async with async_playwright() as p:
        browser=await p.chromium.launch(channel='msedge',headless=True,args=['--disable-background-networking'])
        context=await browser.new_context(viewport={'width':1100,'height':800},service_workers='block')
        await context.route('**/*',f.route)
        page=await context.new_page();page.set_default_timeout(12000);page.on('pageerror',lambda e:errors.append(str(e)))
        async def done():await page.wait_for_function('!manualRefresh.busy')
        async def pause():await page.evaluate('clearTimeout(poll.timer)')
        try:
            await page.goto('http://127.0.0.1:18799/?desk=1');await page.wait_for_function('last?.manual_refresh_supported===true');await pause()
            assert await page.locator('#btn-refresh').count()==1
            before=len(f.refresh_calls);await page.locator('#btn-refresh').click(click_count=3)
            await page.wait_for_function('manualRefresh.busy');assert await page.locator('#btn-refresh').is_disabled()
            await done();await pause();assert len(f.refresh_calls)==before+1
            assert await page.evaluate('last.refresh_marker')==1
            assert await page.evaluate('last.flows[0].host')=='refresh-1.example'
            assert await page.evaluate('gptCatalog.inventory.nodes')==['fresh-node-1']
            assert await page.locator('#refresh-feedback').is_hidden()
            await page.screenshot(path=str(out/'refresh-desktop.png'))
            ok('single header action; triple click sends once; fresh connections, cumulative rank and node inventory commit together')

            f.stale_once=True
            await page.evaluate('void tick(true)');await asyncio.wait_for(f.stale_started.wait(),3)
            await page.locator('#btn-refresh').click();await done();assert await page.evaluate('last.refresh_marker')==2
            f.stale_release.set();await page.wait_for_function('!poll.busy');await pause()
            assert await page.evaluate('last.refresh_marker')==2
            ok('older automatic response cannot overwrite newer manual refresh or its node inventory')

            for mode in ('http','core','incomplete'):
                await pause();before=await page.evaluate('JSON.stringify(last)');catalog=await page.evaluate('JSON.stringify(gptCatalog)')
                f.failure_mode=mode;await page.locator('#btn-refresh').click();await done();await pause()
                assert await page.evaluate('JSON.stringify(last)')==before
                assert await page.evaluate('JSON.stringify(gptCatalog)')==catalog
                assert await page.locator('#refresh-feedback').is_visible();assert '已保留' in await page.locator('#refresh-feedback').inner_text()
                assert await page.locator('#btn-refresh').is_enabled()
            f.failure_mode='';await page.locator('#btn-refresh').click();await done();await pause()
            assert await page.locator('#refresh-feedback').is_hidden()
            ok('HTTP, core and incomplete inventory failures retain old page and catalog; retry succeeds')

            await page.locator('#filters button').nth(1).click();await page.wait_for_function("filter==='rank' && !poll.busy");await pause()
            await page.locator('#filter-summary').click();await page.locator('#periods button').nth(3).click();await page.wait_for_function('!poll.busy');await pause()
            await page.locator('#btn-search').click();await page.locator('#q').fill('Edge')
            state=await page.evaluate('JSON.stringify([filter,period,rankMode,rankSort,document.getElementById("q").value])');post_count=len(f.posts)
            await page.locator('#btn-refresh').click();await done();await pause()
            assert await page.evaluate('JSON.stringify([filter,period,rankMode,rankSort,document.getElementById("q").value])')==state
            assert f.refresh_calls[-1]['period']==await page.evaluate('period')
            assert f.posts[post_count:]==['/api/refresh'],f.posts[post_count:]
            ok('manual refresh preserves current cumulative dimension, period, sorting and search')

            await page.locator('#q').fill('');f.refresh_delay=1.5
            await page.locator('#btn-refresh').click()
            await page.locator('#view-menu summary').click();await page.locator('[data-connection-entry]').click()
            await page.wait_for_function('!connectionLoading')
            await page.locator('#connection-kind').select_option('domain')
            await page.locator('#connection-domain').fill('unsaved.example')
            await page.locator('#connection-subdomains').check()
            await done()
            assert await page.locator('#connection-domain').input_value()=='unsaved.example'
            assert await page.locator('#connection-kind').input_value()=='domain'
            assert await page.locator('#connection-subdomains').is_checked()
            assert await page.locator('#scoped-ipv6-enable').is_enabled()
            await page.locator('#connection-close').click();await pause()
            ok('opening simplified connection dialog during refresh preserves site and suffix selection without disabling its switch')

            for width in (360,390,640,1100):
                await page.set_viewport_size({'width':width,'height':800})
                rect=await page.locator('#btn-refresh').bounding_box();search=await page.locator('#btn-search').bounding_box()
                assert rect['x']>=0 and rect['x']+rect['width']<=width,(width,rect,search)
                assert rect['x']+rect['width']<=search['x']+1 or rect['y']+rect['height']<=search['y']+1 or search['y']+search['height']<=rect['y']+1,(width,rect,search)
                assert await page.locator('#btn-refresh').get_attribute('aria-label')=='刷新'
            await page.set_viewport_size({'width':390,'height':800});await page.screenshot(path=str(out/'refresh-narrow.png'))
            ok('refresh control remains accessible and does not overlap search at 360/390/640/1100 widths')

            f.supported=False;await page.reload();await page.wait_for_function('last && !poll.busy');await pause()
            assert await page.locator('#btn-refresh').is_disabled();assert '不支持' in await page.locator('#btn-refresh').get_attribute('title')
            count=len(f.refresh_calls);await page.evaluate('refreshData()');assert len(f.refresh_calls)==count
            assert all(path in ('/api/refresh','/api/settings') for path in f.posts),f.posts
            assert not errors,errors
            ok('old runtime capability disables refresh; refresh causes no preference/reset/close/reload/group writes (navigation preference save is separate)')
        finally:
            (out/'report.json').write_text(json.dumps({'passed':checks,'errors':errors,'refresh_calls':f.refresh_calls,'posts':f.posts},ensure_ascii=False,indent=2),encoding='utf-8')
            await browser.close()

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path,required=True);args=ap.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    asyncio.run(check(args.output))
