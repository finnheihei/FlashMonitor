"""Isolated UI acceptance for scope editing and explicit traffic classification.

All browser requests are fulfilled by synthetic fixtures. Writes are restricted to
TemporaryDirectory data and the requested evidence directory; no real app is used.
"""
import argparse
import asyncio
import json
from pathlib import Path
from connection_test_helpers import open_legacy
from urllib.parse import urlparse
from playwright.async_api import async_playwright
from check_ipv6_native_ui import NativeFixture
from flashmonitor.ipv6_plans import build_plan, inspect_flclash, digest


class ScopeFixture(NativeFixture):
    def __init__(self):
        super().__init__()
        self.storage = dict(persistent=True, healthy=True, pending=False, last_error='')
        self.flow_enabled = True

    def snapshot(self, view, period):
        data = super().snapshot(view, period)
        data['status']['storage'] = self.storage.copy()
        if 'flows' in data:
            for name, lane in [('UnknownOnly', 'unknown'), ('RejectedOnly', 'rejected')]:
                data['flows'].append(dict(process=name, title=name, outbound=lane, path=lane,
                                          remote='test.invalid:443', n=1))
            for flow in data['flows']:
                if flow['process'] == 'msedge':
                    flow.update(proc_path=str(self.root/'Browser A'/'same.exe'), identity_verified=True)
        for pack in data.get('rank', {}).values():
            pack['coverage'] = dict(source='clash_connections', scope='observed', period=period,
                                    since=1700000000, lifetime=True, retention_limited=False)
            for dimension in ['nodes', 'sites']:
                pack.setdefault(dimension, {})['coverage'] = dict(pack['coverage'], lifetime=False, retention_limited=True)
            if view == 'rank':
                pack['rows'] = [
                    dict(process='UnknownOnly', download=1000, upload=0, unknown_down=1000),
                    dict(process='RejectedOnly', download=2000, upload=0, rejected_down=2000),
                    dict(process='ProxyOnly', download=3000, upload=0, proxy_down=3000),
                    dict(process='DirectOnly', download=4000, upload=0, direct_down=4000),
                ]
        return data

    async def route(self, route):
        path = urlparse(route.request.url).path
        if path == '/api/ipv4-drafts':
            data = self.rule_api.ipv4_context()
            data['applications'] += [
                {'subject': {'kind': 'process_path', 'value': str(self.root/'Browser B'/'same.exe'),
                             'include_subdomains': False}, 'process': 'other-browser', 'title': 'Same Browser'},
            ]
            await route.fulfill(json=data)
        elif path == '/api/ipv6-plans' and not self.flow_enabled:
            data = self.rule_api.ipv6_catalog()
            data['capabilities']['flow_supported'] = False
            await route.fulfill(json=data)
        else:
            await super().route(route)


async def check(output):
    fixture = ScopeFixture()
    for name in ['Browser A', 'Browser B']:
        path = fixture.root/name/'same.exe'
        path.parent.mkdir()
        path.write_bytes(b'synthetic executable identity')
    scope = {'kind': 'selection', 'subjects': [
        {'kind': 'domain', 'value': 'exact.example', 'include_subdomains': False},
        {'kind': 'domain', 'value': 'tree.example', 'include_subdomains': True},
    ]}
    plan = build_plan(scope, fixture.rule_api.ctx.clash.ipv6_inventory(), inspect_flclash(fixture.root))
    fixture.rule_api.ctx.ipv6_plans.operate({'operation': 'save', 'revision': 0, 'fingerprint': digest(plan)}, plan)
    passed, errors = [], []
    def ok(name):
        passed.append(name)
        print('PASS '+name, flush=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel='msedge', headless=True, args=['--disable-background-networking'])
        context = await browser.new_context(viewport={'width': 1000, 'height': 760}, service_workers='block',
                                            permissions=['clipboard-read', 'clipboard-write'])
        await context.route('**/*', fixture.route)
        page = await context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        async def ready():
            await page.wait_for_function('!ipv6Busy && ipv6Data')
        async def resolved():
            await page.wait_for_function('ipv6Context && !ipv6Busy')
        async def tick():
            await page.wait_for_function('!poll.busy')
            await page.evaluate('tick(true)')
            await page.wait_for_function('!poll.busy')
        try:
            await page.goto('http://127.0.0.1:18799/?desk=1')
            await page.wait_for_function('last && !poll.busy')
            await open_legacy(page)
            await ready()
            assert '不支持在 Monitor 直接保存并生效' in await page.locator('#ipv6-capability').inner_text()
            assert '本地草稿，未生效' in await page.locator('#ipv6-list').inner_text()
            await page.locator('[data-ipv6-view]').click()
            await resolved()
            values = await page.evaluate('ipv6PickerSubject()')
            assert {s['value']: s['include_subdomains'] for s in values['subjects']} == {'exact.example': False, 'tree.example': True}
            await page.locator('#ipv6-domain').fill('exact.example，tree.example；new.example')
            await resolved()
            values = await page.evaluate('ipv6PickerSubject()')
            assert {s['value']: s['include_subdomains'] for s in values['subjects']} == {'exact.example': False, 'tree.example': True, 'new.example': False}
            assert '任一匹配' in await page.locator('#ipv6-summary').inner_text()
            await page.get_by_role('checkbox', name='exact.example 包含子域名', exact=True).check()
            await resolved()
            assert all(s['include_subdomains'] for s in (await page.evaluate('ipv6PickerSubject()'))['subjects'][:2])
            await page.screenshot(path=str(output/'domain-ranges.png'))
            ok('mixed exact/suffix domain scopes survive edit, Chinese separators and adding a domain; only explicit checkbox changes widen scope')

            await page.locator('#ipv6-kind').select_option('application')
            await page.locator('#ipv6-app-search').fill('same.exe')
            labels = await page.locator('#ipv6-app-choices').inner_text()
            assert 'Browser A' in labels and 'Browser B' in labels
            choices = page.locator('#ipv6-app-choices input[type=checkbox]')
            assert await choices.count() == 2
            await choices.nth(0).check()
            await choices.nth(1).check()
            await resolved()
            assert len((await page.evaluate('ipv6PickerSubject()'))['subjects']) == 2
            await page.locator('#ipv6-app-search').fill('does-not-exist')
            assert len((await page.evaluate('ipv6PickerSubject()'))['subjects']) == 2
            await page.locator('#ipv6-app-search').fill('')
            await page.screenshot(path=str(output/'application-paths.png'))
            ok('checkbox application selection distinguishes equal filenames by full path; search does not drop hidden selections')

            await page.locator('#ipv6-close').click()
            await page.locator('#connection-close').click()
            await page.locator('[data-program=msedge]').click(button='right')
            await page.locator('[data-menu-action=connection]').click()
            await page.wait_for_function('!gptBusy')
            selected_path=await page.locator('#gpt-apps input:checked').input_value()
            assert selected_path==str(fixture.root/'Browser A'/'same.exe')
            await open_legacy(page)
            await ready()
            await page.locator('#ipv6-app-choices input').first.check()
            await resolved()
            selected = await page.evaluate('ipv6PickerSubject()')
            assert selected['kind'] == 'process_path' and selected['value'] == str(fixture.root/'Browser A'/'same.exe')
            assert await page.locator('#ipv6-app-choices input:checked').count() == 1
            ok('program context opens exact GPT application; old IPv6 picker remains accessible without retired IPv4 flow')

            await page.locator('#ipv6-close').click()
            await page.locator('#connection-close').click()
            await page.locator('#filters button').nth(1).click()
            await page.wait_for_function("filter==='rank' && !poll.busy")
            await open_legacy(page)
            await ready()
            assert await page.locator('#ipv6-dialog').is_visible()
            ok('advanced saved IPv6 entry opens the existing manager within unified connection settings')

            await page.locator('[data-ipv6-view]').click()
            await resolved()
            await page.locator('#ipv6-start').click()
            await page.wait_for_function('ipv6Context?.resume_id && !ipv6Busy')
            current = fixture.rule_api.ctx.ipv6_plans.catalog()['plans'][0]
            fixture.native_save(current)
            fixture.compile([current['rule'], 'DOMAIN,existing.example,DIRECT', 'MATCH,DIRECT'])
            await page.wait_for_function("document.getElementById('ipv6-native-status').textContent.includes('IPv6规则已启用')")
            assert '上次核验：' in await page.locator('#ipv6-list').inner_text()
            assert await page.locator('[data-ipv6-delete]').count() == 0
            await page.locator('#ipv6-domain').fill('exact.example,tree.example,new.example')
            await resolved()
            assert await page.locator('#ipv6-start').is_disabled()
            assert '不能追加第二个方案' in await page.locator('#ipv6-active-note').inner_text()
            count = fixture.operations.count('begin')
            await page.evaluate('ipv6Start()')
            assert fixture.operations.count('begin') == count
            await page.locator('[data-ipv6-view]').click()
            await resolved()
            await page.wait_for_function("!document.getElementById('ipv6-undo').hidden")
            assert await page.locator('#ipv6-start').is_enabled()
            await page.locator('#ipv6-native').evaluate('el=>el.open=true')
            assert not await page.locator('#ipv6-rule-export').is_visible()
            await page.locator('#ipv6-rule-export').locator('..').locator('summary').click()
            assert await page.locator('#ipv6-rule-export').is_visible()
            ok('active range blocks additions before begin, verified records show timestamp and hide delete, same range resumes; TXT is optional advanced detail')
            for width, height in [(640, 360), (480, 320)]:
                await page.set_viewport_size({'width': width, 'height': height})
                assert await page.locator('#ipv6-dialog').evaluate('e=>e.scrollWidth<=e.clientWidth')
                rect = await page.locator('#ipv6-dialog > .rule-actions').bounding_box()
                assert rect['y']+rect['height'] <= height
            await page.screenshot(path=str(output/'scope-480.png'))
            ok('scope editor retains original compact modal and visible footer at 480px')
            await page.locator('#ipv6-close').click()

            await page.set_viewport_size({'width': 1000, 'height': 760})
            await page.locator('#connection-close').click()
            await tick()
            metrics = await page.evaluate("[statDirect({download:1000}),statUnknown({download:1000}),statDirect({download:2000,rejected_down:2000}),statRejected({download:2000,rejected_down:2000})]")
            assert metrics == [0, 1000, 0, 2000]
            text = await page.locator('#main').inner_text()
            assert '未知' in text and '拒绝' in text and '程序累计保留至清零' in text
            for key, first in [('proxy', 'ProxyOnly'), ('direct', 'DirectOnly'), ('total', 'DirectOnly')]:
                await page.locator('[data-rank-sort='+key+']').click()
                assert await page.locator('[data-rank-row]').first.get_attribute('data-rank-row') == first
                await tick()
                assert await page.locator('[data-rank-row]').first.get_attribute('data-rank-row') == first
            await page.locator('#filter-summary').click()
            await page.locator('#sort').select_option('direct')
            assert await page.locator('[data-rank-row]').count() == 1
            assert await page.locator('[data-rank-row]').get_attribute('data-rank-row') == 'DirectOnly'
            await page.locator('#sort').select_option('all')
            await page.evaluate("rankMode='site';render(true)")
            assert '累计不代表全部历史' in await page.locator('[data-stat-coverage]').inner_text()
            note = await page.evaluate("coverageLabel({period:'today',since:1700000000,top_only:true,measured_outbounds:['proxy','direct'],enabled:false})")
            assert '仅主要网站' in note and '未知和拒绝计入程序总量' in note and '分时明细未启用' in note
            ok('explicit direct/unknown/rejected counters drive summary, sorting and filtering; retention-limited site totals are labeled')

            await page.locator('#filters button').first.click()
            await tick()
            assert '未知' in await page.locator('[data-program=UnknownOnly]').inner_text()
            assert '已拒绝' in await page.locator('[data-program=RejectedOnly]').inner_text()
            await page.evaluate("gotoFilter('overview','',{style:'balls'})")
            await tick()
            assert await page.evaluate("well.balls.filter(b=>['UnknownOnly','RejectedOnly'].includes(wellPid(b))).length") == 0
            assert '仅含未知或拒绝连接' in await page.locator('#well-unclassified').inner_text()
            await page.locator('#well-unclassified [data-live-style=list]').click()
            await tick()
            await page.locator('[data-home-scope=proxy]').click()
            await page.wait_for_function('!homeScopeSaving')
            assert await page.locator('[data-program=UnknownOnly]').count() == 0
            assert await page.locator('[data-program=RejectedOnly]').count() == 0
            fixture.storage.update(healthy=True, pending=True)
            await tick()
            assert await page.locator('#quota .note.warn').count() == 0
            fixture.storage.update(healthy=False, pending=True, last_error='统计保存失败：synthetic fixture')
            await tick()
            assert '统计保存失败' in await page.locator('#quota').inner_text()
            await tick()
            assert await page.locator('#quota .note.warn').count() == 1
            fixture.storage.update(healthy=True, pending=False, last_error='')
            await tick()
            assert '统计保存失败' not in await page.locator('#quota').inner_text()
            fixture.storage.update(healthy=False, detail_healthy=False, last_error='网站明细暂不可用，程序累计状态单独保存。')
            await tick()
            assert '网站明细暂不可用' in await page.locator('#quota').inner_text()
            assert '统计保存失败' not in await page.locator('#quota').inner_text()
            fixture.storage.update(healthy=True, detail_healthy=True, last_error='')
            ok('unknown/rejected routes stay distinct and out of proxy filter; storage failure is a stable banner and clears on recovery')

            fixture.flow_enabled = False
            await open_legacy(page)
            await ready()
            assert '未提供可用的设置流程' in await page.locator('#ipv6-capability').inner_text()
            assert await page.locator('#ipv6-start').is_disabled()
            assert not errors, errors
            ok('missing backend capability never exposes a working-looking direct-save action; no page errors')
        finally:
            (output/'report.json').write_text(json.dumps(dict(passed=passed, page_errors=errors,
                operations=fixture.operations, real_client_written=False, requests_intercepted=True), ensure_ascii=False, indent=2), encoding='utf-8')
            await browser.close()
            fixture.flclash.stop()
            fixture.rule_tmp.cleanup()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    asyncio.run(check(args.output))
