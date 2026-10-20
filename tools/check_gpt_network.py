"""Isolated browser acceptance; every request intercepted, no real client started."""
import argparse
import asyncio
import json
import time
from pathlib import Path
from connection_test_helpers import open_gpt
from urllib.parse import urlparse

from playwright.async_api import async_playwright
from check_ui import Fixture
from flashmonitor.network_inventory import NetworkPolicyService
from flashmonitor.rule_drafts import DraftError
from tests.test_network_policy import APP, FLOW, inventory


class GPTFixture(Fixture):
    def __init__(self):
        super().__init__()
        self.delay = 0
        self.unavailable = False
        self.bodies = []
        self.state = {'ts': time.time(), 'flows': [FLOW, {**FLOW, 'process': 'other-browser', 'proc_path': r'D:\Other\browser.exe'}]}
        self.service = NetworkPolicyService(self.inventory, lambda: self.state)
        self.posts = []

    def inventory(self):
        if self.unavailable:
            return {'available': False, 'error': 'synthetic unavailable'}
        return {**inventory(), 'sampled_at': time.time()}

    async def route(self, route):
        req = route.request
        url = urlparse(req.url)
        if url.hostname != '127.0.0.1' or url.port != 18799:
            await route.abort(); return
        if req.method == 'POST':
            self.posts.append(url.path)
        if url.path == '/api/network-policy':
            # Even an advertised writer cannot enable the UI action.
            data = self.service.catalog()
            data['capabilities']['apply'] = True
            await route.fulfill(json=data)
        elif url.path == '/api/network-preview':
            body = req.post_data_json
            self.bodies.append(body)
            try:
                data = self.service.preview(body)
            except DraftError as exc:
                await route.fulfill(status=400, json={'error': str(exc)}); return
            if self.delay:
                await asyncio.sleep(self.delay)
            await route.fulfill(json=data)
        else:
            await super().route(route)


async def check(output):
    fixture = GPTFixture(); passed = []; errors = []
    def ok(message):
        passed.append(message); print('PASS ' + message, flush=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel='msedge', headless=True, args=['--disable-background-networking'])
        ctx = await browser.new_context(viewport={'width': 1100, 'height': 800}, service_workers='block')
        await ctx.route('**/*', fixture.route)
        page = await ctx.new_page(); page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            await page.goto('http://127.0.0.1:18799/?desk=1')
            await open_gpt(page)
            await page.wait_for_function('gptCatalog && !gptBusy')
            assert await page.locator('#gpt-apply').is_disabled()
            assert await page.locator('#gpt-preview').is_disabled()
            assert not await page.locator('#gpt-ipv6').is_checked()
            assert not await page.locator('#gpt-quic').is_checked()
            ok('capability unavailable is honest; forged advertised apply flag cannot enable action; options default off')

            await page.locator('#gpt-kind').select_option('browser')
            await page.locator('#gpt-apps input').first.check()
            await page.locator('#gpt-domains').fill('chatgpt.com\nopenai.com')
            await page.locator('[data-gpt-domain="1"]').check()
            await page.locator('#gpt-target').select_option('GPT')
            await page.locator('#gpt-preview').click()
            await page.wait_for_function('!gptBusy')
            body=fixture.bodies[-1]
            assert body['scope']['kind']=='all'
            assert body['scope']['children'][0]==APP
            domains=body['scope']['children'][1]['children']
            assert [d['include_subdomains'] for d in domains]==[False,True]
            assert '本次仅预览，未改变网络，无需撤销' in await page.locator('#gpt-result').inner_text()
            assert await page.locator('#gpt-apply').is_disabled()
            ok('browser AND exact path/domain OR tree reaches actual backend preview, mixed exact/suffix preserved')

            await page.locator('#gpt-domains').fill('chatgpt.com\nopenai.com\nnew.example')
            assert await page.locator('[data-gpt-domain="1"]').is_checked()
            assert not await page.locator('[data-gpt-domain="2"]').is_checked()
            await page.locator('#gpt-ipv6').check(); await page.locator('#gpt-quic').check()
            await page.locator('#gpt-preview').click(); await page.wait_for_function('!gptBusy')
            text=await page.locator('#gpt-result').inner_text()
            assert '不保证应用回退 IPv4' in text and 'WebRTC' in text
            assert fixture.bodies[-1]['block_ipv6'] and fixture.bodies[-1]['block_quic']
            ok('scope edits preserve suffix intent; IPv6 and UDP/443 warnings reflect explicit options')

            fixture.delay=0.6
            await page.locator('#gpt-preview').click()
            await page.wait_for_timeout(80)
            await page.locator('#gpt-domains').fill('changed.example')
            await page.wait_for_timeout(750)
            assert await page.locator('#gpt-result').inner_text()==''
            await page.locator('#gpt-preview').click(); await page.wait_for_timeout(80)
            await page.locator('#connection-close').click(); await page.wait_for_timeout(750)
            assert not await page.locator('#gpt-dialog').is_visible()
            fixture.delay=0
            ok('slow response cannot overwrite changed selection or reopen closed modal')

            await open_gpt(page)
            await page.wait_for_function('!gptBusy')
            await page.locator('#gpt-target').select_option('GPT')
            fixture.unavailable=True
            await page.locator('#gpt-refresh').click(); await page.wait_for_function('!gptBusy')
            assert await page.locator('#gpt-preview').is_disabled()
            assert await page.locator('#gpt-apply').is_disabled()
            assert await page.locator('#gpt-target').input_value()=='GPT'
            fixture.unavailable=False
            await page.locator('#gpt-refresh').click(); await page.wait_for_function('!gptBusy')
            assert await page.locator('#gpt-target').input_value()=='GPT'
            ok('unified entry works; unavailable backend fails closed and preserves target for recheck')
            await page.locator('#gpt-kind').select_option('domain');await page.locator('#gpt-domains').fill('chatgpt.com')
            await page.locator('#gpt-preview').click();await page.wait_for_function('!gptBusy')

            for width,height in [(480,640),(1100,800)]:
                await page.set_viewport_size({'width':width,'height':height})
                assert await page.locator('#gpt-dialog').evaluate('e=>e.scrollWidth<=e.clientWidth')
                actions=page.locator('#connection-diagnostics > .rule-actions')
                await actions.scroll_into_view_if_needed()
                rect=await actions.bounding_box()
                assert rect['y']+rect['height']<=height
                await page.screenshot(path=str(output/f'preview-{width}.png'))
            assert not errors,errors
            assert set(fixture.posts)<={'/api/network-preview'}, fixture.posts
            ok('480px folded diagnostics actions remain reachable; no script errors or unintended network-write requests')
        finally:
            (output/'report.json').write_text(json.dumps({'passed':passed,'page_errors':errors,'posts':fixture.posts,'requests_intercepted':True,'real_client_written':False},ensure_ascii=False,indent=2),encoding='utf-8')
            await browser.close();fixture.rule_tmp.cleanup()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    asyncio.run(check(args.output))
