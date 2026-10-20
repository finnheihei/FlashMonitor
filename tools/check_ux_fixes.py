"""Focused UX regression; installed Edge, intercepted requests, temporary drafts only."""
import argparse
import asyncio
import json
import time
from pathlib import Path
from connection_test_helpers import open_catalog, open_rule

from check_ui import Fixture
from playwright.async_api import async_playwright


class ChangingFixture(Fixture):
    def __init__(self):
        super().__init__()
        self.frame = 0
        self.exited = False

    def snapshot(self, view, period):
        data = super().snapshot(view, period)
        if self.exited:
            data['flows'] = []
            data['live']['eaters'] = []
            return data
        if 'flows' in data:
            lane = ['proxy', 'direct', 'unknown'][self.frame % 3]
            for f in data['flows']:
                if f['process'] == 'msedge':
                    f.update(path='tun', outbound=lane, remote='same.example:443', proc_path=r'C:\Browser\msedge.exe')
            data['flows'] += [
                dict(process='worker', title='Worker', rule_exe='worker.exe', proc_path=path,
                     path=lane, outbound=lane, remote='worker.example:443', n=1)
                for path,lane in [(r'C:\A\worker.exe','proxy'), (r'D:\B\worker.exe','direct')]
            ]
        for p in data['live']['eaters']:
            if p['process'] == 'msedge': p['down'] = 1000 + self.frame * 2000
        return data


async def check(output):
    fixture = ChangingFixture()
    results, errors = [], []
    def passed(name):
        results.append(name)
        print('PASS '+name, flush=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel='msedge', headless=True, args=['--disable-background-networking'])
        context = await browser.new_context(viewport=dict(width=960,height=640), service_workers='block', locale='zh-CN')
        await context.route('**/*', fixture.route)
        page = await context.new_page()
        page.on('pageerror', lambda e: errors.append(str(e)))
        async def tick():
            await page.wait_for_function('!poll.busy')
            await page.evaluate('tick(false)')
            await page.wait_for_function('!poll.busy')
        async def editor():
            await page.wait_for_selector('#rule-actions:not([hidden])')
        try:
            await page.goto('http://127.0.0.1:18799/?desk=1')
            await page.wait_for_selector('[data-program=msedge]')
            await page.locator('[data-program=msedge]').click()
            await page.locator('[data-program=msedge]').focus()
            await page.evaluate("window.heldButton=document.activeElement")
            started = time.monotonic()
            for i in range(16):
                fixture.frame = i
                await tick()
                state = await page.evaluate("""() => ({same:document.activeElement===window.heldButton && heldButton.isConnected,
                    row:heldButton.closest('tr').querySelector('td.num').textContent,
                    expected:fmtSpeed(last.live.eaters.find(p=>p.process==='msedge').down),
                    route:heldButton.querySelector('.route-label').textContent,
                    detail:document.querySelector('.dest-row .pill').textContent})""")
                assert state['same'], state
                assert state['expected'] in state['row'], state
                assert state['route'] == state['detail'] == ['代理','直连','未知'][i%3], state
                if i<15: await asyncio.sleep(2)
            assert time.monotonic()-started >= 30
            await page.screenshot(path=str(output/'01-focus-30-seconds.png'))
            passed('30 seconds: original focused button preserved while rates and route/detail change on every poll')

            # Shared route semantics including unknown ingress, partial unknown, mixed and leak.
            states = await page.evaluate("""() => {
                const saved=last.flows;
                const cases=[[],[{path:'tun',outbound:'unknown'}],[{path:'sysproxy',outbound:'unknown'}],
                  [{path:'proxy'}],[{outbound:'direct'}],[{outbound:'proxy'},{outbound:'direct'}],
                  [{outbound:'proxy'},{outbound:'unknown'}],[{path:'leak',outbound:'direct',n:2}],
                  [{outbound:'proxy'},{path:'leak',outbound:'direct',n:2},{outbound:'unknown'}]];
                const result=cases.map(flows=>{
                  last.flows=flows.map(f=>({process:'test',remote:'example:443',...f}));
                  const a=liveRouteStatus({process:'test',conns:0}).text;
                  const box=document.createElement('div');box.innerHTML=destRowHtml(overviewDests('test'));
                  const b=box.querySelector('.pill')?.textContent;
                  box.innerHTML=renderConns(last.flows);
                  const conn=[...box.querySelectorAll('td.out .pill')].map(p=>p.textContent);
                  return {a,b,conn};
                });last.flows=saved;return result;
            }""")
            assert states[0]['a']=='无连接'
            for state in states[1:]: assert state['a']==state['b'], state
            assert states[1]['conn']==states[2]['conn']==['未知'], states
            assert states[3]['conn']==['代理'] and states[4]['conn']==['直连']
            assert states[5]['conn']==['代理','直连']
            assert states[6]['conn']==['代理','未知']
            assert states[7]['conn']==['绕过 TUN（直连）2 条']
            assert states[8]['conn']==['代理','绕过 TUN（直连）2 条','未知']
            passed('route semantics agree across program, destination and connection views for all tested states')

            await page.locator('[data-program=worker]').click()
            assert '2 个身份来源' in await page.locator('[data-program=worker]').inner_text()
            assert await page.locator('.program-detail').evaluate("e=>e.previousElementSibling.querySelector('[data-program=worker]')!==null")
            identities = await page.locator('.identity-item').all_text_contents()
            assert len(identities)==2 and r'C:\A\worker.exe' in identities[0] and r'D:\B\worker.exe' in identities[1], identities
            assert '代理' in identities[0] and '直连' in identities[1]
            await page.locator('[data-program=worker]').scroll_into_view_if_needed()
            await page.screenshot(path=str(output/'02-distinct-paths.png'))
            passed('same executable name with two paths remains two explicit identities, with separate route evidence')

            await open_rule(page,'worker')
            await editor()
            await page.locator('#rule-preview-btn').click()
            await page.wait_for_function('!ruleEditor.busy')
            await page.locator('#rule-save').click()
            await page.wait_for_function('!ruleEditor.busy')
            assert fixture.rule_store.view('worker')['draft']['exe']=='worker.exe'
            await page.keyboard.press('Escape')
            await page.locator('#connection-close').click()
            fixture.exited = True
            await tick()
            assert await page.locator('[data-program=worker]').count()==0
            await page.reload()
            await page.wait_for_selector('#filters button')
            await open_catalog(page)
            await page.locator('[data-saved-draft=worker]').click()
            await editor()
            assert '保存时的可执行文件：worker.exe' in await page.locator('#rule-identity').inner_text()
            assert await page.locator('#rule-save').is_hidden()
            assert await page.locator('#rule-export').is_enabled()
            async with page.expect_download() as download:
                await page.locator('#rule-export').click()
            await (await download.value).save_as(str(output/'saved-after-exit.yaml'))
            assert 'PROCESS-NAME,worker.exe,DIRECT' in (output/'saved-after-exit.yaml').read_text(encoding='utf-8')
            await page.wait_for_function('!ruleEditor.busy')
            await page.screenshot(path=str(output/'03-saved-after-exit.png'))
            await page.locator('#rule-delete').click(click_count=3)
            await page.wait_for_function('!ruleEditor.busy')
            assert fixture.rule_store.view('worker')['draft'] is None
            await page.keyboard.press('Escape')
            await page.wait_for_function("document.getElementById('draft-library-list').textContent.includes('没有已保存')")
            assert await page.locator('#draft-library-close').evaluate('e=>e===document.activeElement')
            await page.keyboard.press('Escape')
            assert await page.locator('#btn-drafts').evaluate('e=>e===document.activeElement')
            await page.locator('#connection-close').click()
            passed('save, process exit, page reload, independent draft reopen/export/delete and keyboard return')

            fixture.exited = False
            await tick()
            await page.locator('[data-program=msedge]').click()
            await open_rule(page,'msedge')
            await editor()
            await page.locator('input[value=proxy]').check()
            await page.locator('#rule-group').select_option("Group 'quoted'")
            await page.locator('#rule-preview-btn').click()
            await page.wait_for_function('!ruleEditor.busy')
            for width,height in [(768,512),(640,360),(480,320)]:
                await page.set_viewport_size(dict(width=width,height=height))
                rect = await page.locator('#rule-actions').bounding_box()
                assert rect and rect['y']>=0 and rect['y']+rect['height']<=height, rect
                assert await page.locator('#rule-dialog').evaluate('e=>e.scrollWidth<=e.clientWidth')
                await page.locator('#rule-cancel').focus()
                await page.keyboard.press('Shift+Tab')
                assert await page.locator('#rule-dialog').evaluate('e=>e.contains(document.activeElement)')
                await page.keyboard.press('Tab')
                assert await page.locator('#rule-cancel').evaluate('e=>e===document.activeElement')
                await page.screenshot(path=str(output/f'04-dialog-{width}x{height}.png'))
            await page.keyboard.press('Escape')
            assert await page.locator('[data-rule-edit=msedge]').evaluate('e=>e===document.activeElement')
            await page.keyboard.press('Escape')
            await page.keyboard.press('Escape')
            assert await page.locator('[data-program=msedge]').evaluate('e=>e===document.activeElement')
            passed('fixed action area visible at 768x512, 640x360, 480x320; Tab trap, Escape and detail return')
            assert not errors, errors
        finally:
            await page.screenshot(path=str(output/'last-state.png'))
            (output/'report.json').write_text(json.dumps(dict(passed=results,page_errors=errors),ensure_ascii=False,indent=2),encoding='utf-8')
            await browser.close()
            fixture.rule_tmp.cleanup()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    asyncio.run(check(args.output))
