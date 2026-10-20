"""Second-stage UI: process identity explanations, icon URLs and ignore persistence."""
import argparse
import asyncio
import copy
import json
from pathlib import Path
from urllib.parse import urlparse
from check_ui import Fixture
from playwright.async_api import async_playwright


class IdentityFixture(Fixture):
    def __init__(self, source=None):
        super().__init__();self.source=source
        self.settings['ignored']=['msedge','Unknown "app"']
        self.pref_calls=0;self.pref_failure=False
        self.folder_calls=0;self.sites_empty=False;self.sites_budget=False

    def snapshot(self,view,period):
        data=super().snapshot(view,period)
        if view=='rank' and self.sites_empty:
            for row in data['rank'][period]['rows']:row['sites']=[]
        if view=='rank' and self.sites_budget:
            for row in data['rank'][period]['rows']:
                row['sites']=row['sites'][:1]
                row['sites_note']='网站明细仅保留 7 天，最多 20,000 行 / 8 MiB；已因预算丢失部分明细，未补造更早记录。'
        if view=='overview':
            data['flows'] += [dict(process='svchost',proc_path=r'C:\Windows\System32\svchost.exe',identity_verified=True,
                                  identity_created=123,identity_source='本机有限权限查询',icon_version='host-ready',
                                  services=['Dnscache','OtherService'],service_note='宿主服务列表（不代表本连接由某个服务发起）',path='local',outbound='direct')]
            data['flows'] += [dict(process='denied',rule_exe='denied.exe',identity_verified=False,
                                  identity_reason='进程访问受限',outbound='unknown')]
            for f in data['flows']:
                if f['process']=='msedge':f.update(proc_path=r'C:\Browser\msedge.exe',identity_verified=True,icon_version='edge-ready')
        return data

    async def route(self,route):
        request=route.request;path=urlparse(request.url).path
        if path=='/api/icon':
            await route.fulfill(content_type='image/svg+xml',body='<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"><rect x="2" y="2" width="16" height="16" rx="3" fill="#c5ced9"/></svg>');return
        if path=='/' and self.source:
            await route.fulfill(content_type='text/html',body=self.source.read_text(encoding='utf-8'));return
        if path=='/api/process-folder':
            self.folder_calls+=1
            assert request.post_data_json=={'process':'msedge','path':r'C:\Browser\msedge.exe'}
            await route.fulfill(content_type='application/json',body='{"opened":true}');return
        if path=='/api/settings' and request.method=='POST' and 'process' in request.post_data_json:
            self.pref_calls+=1;await asyncio.sleep(.25)
            if self.pref_failure:
                await route.fulfill(status=500,content_type='application/json',body=json.dumps({'error':'模拟保存失败'}));return
            body=request.post_data_json;name=body['process']
            for key in ['ignored','pinned']:
                if key in body:
                    names=[n for n in self.settings.get(key,[]) if n.lower()!=name.lower()]
                    if body[key]:names.append(name)
                    self.settings[key]=names
            await route.fulfill(content_type='application/json',body=json.dumps(copy.deepcopy(self.settings)));return
        await super().route(route)


async def check(output):
    results=[];errors=[]
    def passed(name):results.append(name);print('PASS '+name,flush=True)
    async with async_playwright() as p:
        browser=await p.chromium.launch(channel='msedge',headless=True,args=['--disable-background-networking'])
        # Reproduce against the actual stage-one source backup, with successful saved state.
        old=Path('C:/Users/finhe/AppData/Local/Temp/fm-identity-icons-backup-20261014/ui.html')
        before=IdentityFixture(old);context=await browser.new_context(service_workers='block')
        await context.route('**/*',before.route);page=await context.new_page()
        await page.goto('http://127.0.0.1:18799/?desk=1');await page.evaluate("gotoFilter('settings','general')")
        await page.wait_for_selector('[data-ignore=msedge]');await page.locator('[data-ignore=msedge]').click()
        await page.wait_for_function("settingsCache.ignored && !settingsCache.ignored.includes('msedge')")
        assert before.settings['ignored']==['Unknown "app"']
        assert await page.locator('[data-ignore=msedge]').count()==1
        passed('pre-fix reproduction: server saves cancellation but settings list remains unchanged')
        await context.close();before.rule_tmp.cleanup()

        fixture=IdentityFixture();context=await browser.new_context(viewport=dict(width=820,height=600),service_workers='block',permissions=['clipboard-read','clipboard-write'])
        await context.route('**/*',fixture.route);page=await context.new_page()
        page.on('pageerror',lambda e:errors.append(str(e)))
        async def idle():await page.wait_for_function('prefPending.size===0 && !poll.busy')
        try:
            await page.goto('http://127.0.0.1:18799/?desk=1');await page.evaluate("gotoFilter('settings','general')")
            await page.wait_for_selector('[data-ignore=msedge]')
            await page.locator('[data-ignore=msedge]').evaluate('e=>{e.click();e.click();e.click()}')
            await idle()
            assert fixture.pref_calls==1 and 'msedge' not in fixture.settings['ignored']
            assert await page.locator('[data-ignore=msedge]').count()==0
            assert await page.locator('[data-ignore]').count()==1
            await page.reload();await page.wait_for_selector('[data-ignore]')
            assert await page.locator('[data-ignore=msedge]').count()==0
            passed('cancel ignore updates list immediately, triple click sends once, other names remain; reload restores saved state')

            fixture.pref_failure=True
            target=page.locator('[data-ignore]').first
            await target.focus();await page.keyboard.press('Enter');await idle()
            assert await page.locator('[data-ignore]').count()==1
            assert '没存上' in await page.locator('#toast').inner_text()
            assert await target.is_enabled()
            fixture.pref_failure=False;await target.focus();await page.keyboard.press('Space');await idle()
            assert fixture.settings['ignored']==[] and await page.locator('[data-ignore]').count()==0
            passed('failed cancellation stays visible with error, enabled retry and Enter/Space operation; quoted name preserved')

            await page.locator('#filters button').first.click();await page.wait_for_selector('[data-program=svchost]')
            await page.locator('[data-program=svchost]').click()
            assert '承载多个服务' in await page.locator('.identity-list').inner_text()
            assert '不代表本连接' in await page.locator('.identity-list').inner_text()
            assert '本机有限权限查询' in await page.locator('.identity-list').inner_text()
            await page.locator('[data-program=denied]').click()
            assert '进程访问受限' in await page.locator('.identity-list').inner_text()
            assert '路径未提供' in await page.locator('[data-program=denied]').inner_text()
            src=await page.locator('[data-program=msedge] img').get_attribute('src')
            assert 'v=edge-ready' in src
            await page.screenshot(path=str(output/'identity-details.png'))
            passed('service host shared-service disclaimer, explicit denied identity and versioned icon URL; no fabricated path')

            await page.locator('[data-program=denied]').click(button='right')
            assert await page.locator('[data-menu-action=copy]').is_disabled()
            assert '本机验证' in await page.locator('[data-menu-action=copy]').get_attribute('title')
            await page.keyboard.press('Escape');assert await page.locator('#program-menu').is_hidden()
            assert await page.evaluate("well.sel==='denied'")
            await page.locator('[data-program=msedge]').focus();await page.keyboard.press('Shift+F10')
            assert await page.locator('#program-menu').is_visible()
            await page.keyboard.press('ArrowDown')
            assert await page.locator('#program-menu').evaluate('e=>e.contains(document.activeElement)')
            await page.screenshot(path=str(output/'program-menu.png'))
            await page.locator('[data-menu-action=copy]').click()
            assert await page.evaluate('navigator.clipboard.readText()')==r'C:\Browser\msedge.exe'
            await page.locator('[data-program=msedge]').click(button='right')
            await page.locator('[data-menu-action=folder]').click();assert fixture.folder_calls==1
            await page.locator('[data-program=msedge]').click(button='right')
            await page.locator('#filters button').first.click();assert await page.locator('#program-menu').is_hidden()
            await page.locator('[data-program=msedge]').click(button='right')
            await page.locator('[data-menu-action=connections]').click()
            await page.wait_for_function("filter==='conns' && processScope==='msedge'")
            passed('program right-click and Shift+F10 menu: disabled unknown path, keyboard navigation, clipboard, mocked safe folder and scoped connections')

            await page.evaluate("gotoFilter('rank','',{period:'all',rankMode:'program',process:'msedge'})")
            await page.wait_for_selector('[data-rank-row=msedge]')
            await page.locator('[data-rank-row=msedge] .nm').click()
            await page.wait_for_selector('[data-site-row]')
            assert await page.locator('[data-site-row]').count()==2
            for period in ['1h','today','7d','30d','all']:
                await page.evaluate('(p)=>{period=p;tick(true)}',period)
                await page.wait_for_function('(p)=>!poll.busy && last.rank?.[p]',arg=period)
                values=await page.evaluate("last.rank[period].rows.find(r=>r.process==='msedge')")
                assert sum(s['download'] for s in values['sites'])==values['download']
                assert await page.locator('[data-site-row]').count()==2
                await page.locator('[data-rank-sort=total]').click()
                await page.locator('[data-rank-sort=total]').click()
                labels=await page.locator('[data-site-row]').evaluate_all('rows=>rows.map(r=>r.dataset.siteRow)')
                assert labels==['msedge.direct.example.com','msedge.proxy.example.com'],labels
            fixture.sites_empty=True
            await page.screenshot(path=str(output/'program-websites.png'))
            await page.evaluate('tick(true)');await page.wait_for_function('!poll.busy')
            assert '没有匹配的网站历史记录' in await page.locator('#main').inner_text()
            assert await page.locator('[data-site-row]').count()==0
            fixture.sites_empty=False;await page.evaluate('tick(true)');await page.wait_for_function('!poll.busy')
            assert await page.locator('[data-site-row]').count()==2
            passed('click program to expand websites: five periods, totals unchanged, child sorting, refreshing and truthful empty/recovery')
            fixture.sites_budget=True
            await page.evaluate("period='30d';tick(true)");await page.wait_for_function('!poll.busy')
            assert await page.locator('[data-site-row]').count()==1
            text=await page.locator('#main').inner_text()
            assert '仅保留 7 天' in text and '20,000 行 / 8 MiB' in text and '丢失部分明细' in text
            row=await page.evaluate("last.rank[period].rows.find(r=>r.process==='msedge')")
            assert sum(s['download'] for s in row['sites'])<row['download']
            await page.screenshot(path=str(output/'detail-budget.png'))
            passed('30-day parent totals remain complete while seven-day bounded child detail shows explicit gaps')
            assert not errors,errors
        finally:
            (output/'report.json').write_text(json.dumps(dict(passed=results,page_errors=errors),ensure_ascii=False,indent=2),encoding='utf-8')
            await browser.close();fixture.rule_tmp.cleanup()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True);asyncio.run(check(args.output))
