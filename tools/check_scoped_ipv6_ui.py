"""Isolated Edge flow using the real service and a synthetic controller."""
import argparse
import asyncio
import json
import sys
import time
import threading
from types import SimpleNamespace
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from check_gpt_network import GPTFixture
from connection_test_helpers import open_connection
from test_scoped_ipv6 import Controller
from flashmonitor.app import MonitorApi
from flashmonitor.classify import site_key
from flashmonitor.rule_drafts import DraftError
from flashmonitor.scoped_ipv6 import ScopedIPv6
from playwright.async_api import async_playwright


class Fixture(GPTFixture):
    def __init__(self):
        super().__init__()
        self.root = Path(self.rule_tmp.name)
        text = json.dumps({'mode': 'rule', 'ipv6': True, 'find-process-mode': 'always', 'rules': ['MATCH,DIRECT']})
        (self.root / 'config.yaml').write_text(text, encoding='utf-8')
        self.controller = Controller(text)
        self.meta = {'available': True, 'profile_id': 'test', 'source_revision': 'test'}
        self.scoped = ScopedIPv6(self.root, self.root / 'scoped.json', self.controller,
                                 inspect=lambda: self.meta.copy())
        self.actions = []
        self.delay = 0
        self.requests = []
        self.identity_available = True
        self.settings['connection_draft'] = {'version': 1, 'kind': 'application',
            'applications': [r'C:\Missing\old1.exe', r'C:\Missing\old2.exe'], 'domains': [],
            'target': 'stale-group', 'block_ipv6': True, 'block_quic': False}
        self.live_state = lambda: {'ts': time.time(), 'flows': [
            {'process': 'msedge', 'title': 'Edge', 'proc_path': r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
             'identity_verified': True}]} if self.identity_available else {'ts': time.time(), 'flows': []}
        self.monitor = MonitorApi(SimpleNamespace(scoped_ipv6=self.scoped,
            live=SimpleNamespace(get=lambda: self.live_state()), stop=threading.Event()))

    def snapshot(self, view, period):
        data = super().snapshot(view, period)
        if view == 'rank':
            data['rank'][period]['sites']['rows'] = [dict(site=site_key('api.selected.example'),
                download=1000, upload=0, proxy_total=1000, direct_total=0)]
        return data

    async def route(self, route):
        self.requests.append((route.request.method, urlparse(route.request.url).path))
        if urlparse(route.request.url).path == '/api/scoped-ipv6':
            try:
                if route.request.method == 'POST':
                    body = route.request.post_data_json
                    self.actions.append(body)
                    if self.delay:
                        await asyncio.sleep(self.delay)
                    result = self.scoped.operate(body)
                else:
                    result = self.monitor.scoped_ipv6_catalog()
                await route.fulfill(json=result)
            except DraftError as error:
                await route.fulfill(status=400, json={'error': str(error)})
        else:
            await super().route(route)


async def check(output):
    fixture = Fixture()
    passed, errors = [], []

    def ok(message):
        passed.append(message)
        print('PASS ' + message, flush=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel='msedge', headless=True, args=['--disable-background-networking'])
        context = await browser.new_context(viewport={'width': 1100, 'height': 820}, service_workers='block')
        await context.route('**/*', fixture.route)
        page = await context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            await page.goto('http://127.0.0.1:18799/?desk=1')
            await open_connection(page)
            await page.wait_for_function('scopedIPv6Catalog && !connectionLoading')
            assert await page.locator('#connection-dialog').get_by_text('更多设置').count() == 0
            assert await page.locator('#connection-confirm, #gpt-dialog, #gpt-target').count() == 0
            assert await page.locator('#scoped-ipv6-enable').is_enabled()
            await page.locator('#scoped-ipv6-enable').click()
            assert '请先选择应用' in await page.locator('#scoped-ipv6-state').inner_text()
            assert not fixture.actions
            ok('removed diagnostic/draft/legacy controls; stale saved draft cannot disable the switch; empty choice explains itself')

            await page.locator('#connection-app').select_option(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')
            await page.locator('#scoped-ipv6-enable').click()
            await page.wait_for_function('!scopedIPv6Busy')
            assert await page.locator('#scoped-ipv6-enable').get_attribute('aria-checked') == 'true'
            await page.locator('#scoped-ipv6-enable').focus()
            await page.keyboard.press('Space')
            await page.wait_for_function('!scopedIPv6Busy')
            assert not fixture.scoped.catalog()['entries']
            ok('selected application switch clicks through to the real service and restores with keyboard')

            await page.locator('#connection-close').click()
            await page.evaluate("openConnectionForProcess('msedge',document.getElementById('btn-settings'))")
            await page.wait_for_function('!connectionLoading')
            assert await page.locator('#connection-target').inner_text() == 'Edge'
            assert await page.locator('#connection-picker').is_hidden()
            assert await page.locator('#scoped-ipv6-enable').is_enabled()
            await page.screenshot(path=str(output / 'app-ready.png'))
            await page.locator('#scoped-ipv6-enable').click()
            await page.wait_for_function('!scopedIPv6Busy')
            assert fixture.actions[-1]['scope'] == {'kind':'process_path','value':r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'}
            await page.locator('#scoped-ipv6-enable').click()
            await page.wait_for_function('!scopedIPv6Busy')
            ok('contextual app entry contains only current app and actionable switch; no repeat selection')

            await page.locator('#connection-close').click()
            fixture.identity_available = False
            await page.evaluate("openConnectionForProcess('missing',document.getElementById('btn-settings'))")
            await page.wait_for_function('!connectionLoading')
            assert await page.locator('#scoped-ipv6-enable').is_enabled()
            before = len(fixture.actions)
            await page.locator('#scoped-ipv6-enable').click()
            assert '未识别' in await page.locator('#scoped-ipv6-state').inner_text()
            assert len(fixture.actions) == before
            assert await page.locator('#scoped-ipv6-refresh').is_visible()
            fixture.identity_available = True
            await page.locator('#connection-close').click()
            await open_connection(page)
            await page.locator('#connection-kind').select_option('domain')
            await page.locator('#connection-domain').fill('selected.example')
            await page.locator('#scoped-ipv6-enable').click()
            await page.wait_for_function('!scopedIPv6Busy')
            assert await page.locator('#scoped-ipv6-state').inner_text() == '已开启'
            ok('unidentified application gives explicit retry guidance; domain switch works without diagnostic groups')

            await page.locator('#connection-domain').fill('other.example')
            assert await page.locator('#scoped-ipv6-enable').get_attribute('aria-checked') == 'false'
            await page.locator('#connection-domain').fill('https://selected.example/path')
            assert await page.locator('#scoped-ipv6-enable').get_attribute('aria-checked') == 'false'
            await page.locator('#connection-domain').fill('SELECTED.EXAMPLE.')
            assert await page.locator('#scoped-ipv6-enable').get_attribute('aria-checked') == 'true'
            await page.locator('#scoped-ipv6-enable').click()
            await page.wait_for_function('!scopedIPv6Busy')
            assert not fixture.scoped.catalog()['entries']
            ok('exact selected scope controls its own rule; case and trailing dot match but invalid URL does not')

            await page.locator('#connection-domain').fill('selected.example')
            await page.locator('#connection-subdomains').check()
            await page.locator('#scoped-ipv6-enable').click()
            await page.wait_for_function('!scopedIPv6Busy')
            assert fixture.actions[-1]['scope']['include_subdomains'] is True
            await page.locator('#connection-domain').fill('other.example')
            await page.locator('#scoped-ipv6-enable').click()
            await page.wait_for_function('!scopedIPv6Busy')
            await page.locator('#scoped-ipv6-saved > summary').click()
            await page.locator('[data-scoped-ipv6-remove]').click()
            await page.wait_for_function('!scopedIPv6Busy')
            assert len(fixture.scoped.catalog()['entries']) == 1
            await page.locator('#scoped-ipv6-enable').click()
            await page.wait_for_function('!scopedIPv6Busy')
            assert not fixture.scoped.catalog()['entries']
            ok('suffix choice explicit; closing another saved scope preserves the current one')

            await page.locator('#connection-close').click()
            await page.evaluate("rankMode='site';gotoFilter('rank')")
            await page.locator('[data-connection-domain="selected.example"]').click()
            await page.wait_for_function('!connectionLoading')
            assert await page.locator('#connection-picker').is_hidden()
            assert await page.locator('#connection-target').inner_text() == 'selected.example'
            assert await page.locator('#connection-subdomains').is_visible()
            assert await page.locator('#connection-subdomains').is_checked()
            await page.screenshot(path=str(output / 'site-ready.png'))
            await page.locator('#scoped-ipv6-enable').click()
            await page.wait_for_function('!scopedIPv6Busy')
            assert fixture.actions[-1]['scope'] == {'kind':'domain','value':'selected.example','include_subdomains':True}
            assert 'DOMAIN-SUFFIX,selected.example' in json.loads(fixture.controller.text)['rules'][0]
            await page.locator('#scoped-ipv6-enable').click()
            await page.wait_for_function('!scopedIPv6Busy')
            assert not fixture.scoped.catalog()['entries']
            ok('actual grouped-site statistics entry defaults to visible subdomain coverage and toggles on/off')

            await page.locator('#connection-subdomains').uncheck()
            await page.locator('#scoped-ipv6-enable').click()
            await page.wait_for_function('!scopedIPv6Busy')
            assert fixture.actions[-1]['scope']['include_subdomains'] is False
            await page.locator('#connection-subdomains').check()
            assert await page.locator('#scoped-ipv6-enable').get_attribute('aria-checked') == 'false'
            assert len(fixture.scoped.catalog()['entries']) == 1
            await page.locator('#connection-subdomains').uncheck()
            await page.locator('#scoped-ipv6-refresh').evaluate('e=>e.click()')
            await page.wait_for_function('!scopedIPv6Reads')
            assert not await page.locator('#connection-subdomains').is_checked()
            assert await page.locator('#scoped-ipv6-enable').get_attribute('aria-checked') == 'true'
            await page.locator('#scoped-ipv6-enable').click()
            await page.wait_for_function('!scopedIPv6Busy')
            assert not fixture.scoped.catalog()['entries']
            ok('contextual website allows exact scope; refresh retains choice and scope changes cannot remove another rule')

            fixture.controller.fail = True
            await page.locator('#scoped-ipv6-enable').click()
            await page.wait_for_function('!scopedIPv6Busy')
            assert '拒绝' in await page.locator('#scoped-ipv6-state').inner_text()
            assert not fixture.scoped.catalog()['entries']
            fixture.controller.fail = False
            fixture.delay = .5
            await page.locator('#scoped-ipv6-enable').click()
            await page.wait_for_function('scopedIPv6Busy')
            assert await page.locator('#connection-close').is_disabled()
            assert await page.locator('#connection-subdomains').is_disabled()
            await page.keyboard.press('Escape')
            assert await page.locator('#connection-dialog').evaluate('e=>e.open')
            await page.wait_for_function('!scopedIPv6Busy')
            fixture.delay = 0
            ok('real failed mutation keeps prior state; only active mutation locks the switch and closing')

            await page.set_viewport_size({'width':480,'height':420})
            assert await page.locator('#connection-dialog').evaluate('e=>e.scrollWidth<=e.clientWidth+2')
            await page.screenshot(path=str(output / 'compact.png'))
            assert not errors, errors
            assert not any(path in ('/api/network-policy','/api/network-preview') for _,path in fixture.requests)
            assert all(path == '/api/scoped-ipv6' for method,path in fixture.requests if method == 'POST')
            ok('compact layout fits; no diagnostic inventory, draft-save or unrelated mutation requests')
        finally:
            (output / 'report.json').write_text(json.dumps({'passed': passed, 'page_errors': errors,
                'actions': fixture.actions, 'real_network_modified': False}, ensure_ascii=False, indent=2), encoding='utf-8')
            await browser.close()
            fixture.rule_tmp.cleanup()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    asyncio.run(check(args.output))
