"""Isolated Edge regression; intercepted traffic and temporary intent drafts only."""
import argparse
import asyncio
import json
from pathlib import Path
from connection_test_helpers import open_connection, open_catalog
from check_ui import Fixture
from playwright.async_api import async_playwright

LONG = '很长的程序名称 "应用" 🧩 ' + 'LongExecutableWithoutSpaces' * 8


class IPv4Fixture(Fixture):
    def __init__(self):
        super().__init__()
        self.exited = False

    def snapshot(self, view, period):
        data = super().snapshot(view, period)
        for p in data['live']['eaters']:
            if p['process'] == 'msedge': p['title'] = LONG
        for row in data.get('rank', {}).get(period, {}).get('rows', []):
            if row['process'] == 'msedge': row['title'] = LONG
        for f in data.get('flows', []) + data.get('closed', []):
            if f['process'] == 'msedge':
                f.update(title=LONG, proc_path=r'C:\Browser\msedge.exe')
        if view == 'overview' and not self.exited:
            data['flows'] += [dict(process='worker', proc_path=path, rule_exe='worker.exe',
                remote='worker.example:443', host='worker.example', path='direct', outbound='direct')
                for path in [r'C:\A\worker.exe', r'D:\B\worker.exe']]
        if self.exited:
            data['flows'] = []
            data['live']['eaters'] = []
        return data


async def check(output):
    fixture = IPv4Fixture()
    results, errors = [], []
    def passed(name):
        results.append(name); print('PASS ' + name, flush=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel='msedge', headless=True, args=['--disable-background-networking'])
        context = await browser.new_context(viewport=dict(width=1280, height=720), locale='zh-CN', service_workers='block')
        await context.route('**/*', fixture.route)
        page = await context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        async def tick():
            await page.wait_for_function('!poll.busy')
            await page.evaluate('tick(false)')
            await page.wait_for_function('!poll.busy')
        async def names(selector):
            metrics = await page.locator(selector).evaluate_all("""els=>els.map(e=>({
                text:e.textContent,title:e.title,width:e.clientWidth,scroll:e.scrollWidth,
                overflow:getComputedStyle(e).textOverflow,nowrap:getComputedStyle(e).whiteSpace}))""")
            assert metrics and any(m['text'] == LONG and m['title'].startswith(LONG) and
                                      m['scroll'] > m['width'] > 0 and m['overflow'] == 'ellipsis' and m['nowrap'] == 'nowrap'
                                      for m in metrics), metrics
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth'), 'page overflow'
        try:
            await page.goto('http://127.0.0.1:18799/?desk=1')
            await page.wait_for_selector('[data-program=msedge]')
            for width, height in [(1280,720),(820,600),(640,360)]:
                await page.set_viewport_size(dict(width=width,height=height))
                await names('[data-program=msedge] .name-text')
                button = page.locator('[data-program=msedge]')
                assert await button.get_attribute('aria-label') == LONG
                assert await button.locator('.route-label').is_visible()
                await button.hover()
                await page.screenshot(path=str(output/f'home-{width}.png'))
            # CSS zoom tests actual constrained layout, not just device pixel ratio.
            await page.set_viewport_size(dict(width=1280,height=720))
            for zoom in [1.5,2]:
                await page.evaluate('(z)=>document.body.style.zoom=z',zoom)
                await names('[data-program=msedge] .name-text')
            await page.evaluate("document.body.style.zoom=''")
            passed('home long name ellipsis, full hover/accessibility name, route visible; three sizes and 150/200% zoom')

            await page.locator('[data-program=msedge]').click()
            await page.locator('[data-program=msedge]').focus()
            await page.evaluate('window.nameHeld=document.activeElement')
            for _ in range(3): await tick()
            assert await page.evaluate('nameHeld===document.activeElement && nameHeld.isConnected')
            assert r'C:\Browser\msedge.exe' in await page.locator('.identity-list').inner_text()
            passed('full identity remains in detail and keyed polling preserves focused button')

            await page.locator('#filters button').nth(1).click()
            await page.wait_for_selector('[data-rank-row=msedge]')
            await names('[data-rank-row=msedge] .nm')
            await page.evaluate("filter='conns';render(true);tick(true)")
            await page.wait_for_selector('td.prog .name-text')
            await names('td.prog .name-text')
            await page.evaluate("connMode='closed';filter='conns';tick(true)")
            await page.wait_for_function('!poll.busy && last.closed')
            await names('td.prog .name-text')
            passed('cumulative, current connections and closed connections preserve full text with ellipsis')

            fixture.settings['ignored']=[LONG]
            await page.evaluate("gotoFilter('settings','general')")
            await page.wait_for_selector('.ign-list .name-text')
            await names('.ign-list .name-text')
            assert await page.locator('.ign-list button').is_visible()
            fixture.settings['ignored']=[]
            # Catalog display has the same complete hover text and does not guess identity.
            long_exe='LongExecutable'*12+'.exe'
            fixture.rule_store.save('long-name',long_exe,'direct','DIRECT',0)
            await open_catalog(page)
            await page.wait_for_selector('[data-saved-draft="long-name"]')
            catalog_name=page.locator('#draft-library-list .name-text')
            assert await catalog_name.get_attribute('title')==long_exe
            assert await catalog_name.inner_text()==long_exe
            assert await catalog_name.evaluate('e=>e.scrollWidth>e.clientWidth')
            await page.locator('[data-saved-draft="long-name"]').click()
            await page.wait_for_selector('#rule-actions:not([hidden])')
            assert await page.locator('#rule-dialog').evaluate('e=>e.scrollWidth<=e.clientWidth')
            await page.keyboard.press('Escape')
            await page.wait_for_selector('[data-saved-draft="long-name"]')
            await page.keyboard.press('Escape')
            await page.keyboard.press('Escape')
            fixture.rule_store.delete('long-name',1)
            passed('ignored list and persistent rule catalog preserve full hover text without hiding action buttons')

            await open_connection(page)
            assert await page.locator('#ipv4-dialog,[data-connection-mode]').count()==0
            assert '仅 IPv4' not in await page.locator('#connection-dialog').inner_text()
            assert await page.locator('#gpt-dialog').is_visible()
            passed('retired IPv4 surface has no editor/option; useful connection diagnosis remains')
            assert not errors,errors
        finally:
            await page.screenshot(path=str(output/'last-state.png'))
            (output/'report.json').write_text(json.dumps(dict(passed=results,page_errors=errors),ensure_ascii=False,indent=2),encoding='utf-8')
            await browser.close();fixture.rule_tmp.cleanup()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    asyncio.run(check(args.output))
