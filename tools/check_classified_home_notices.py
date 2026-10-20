"""Isolated notice/navigation regression with explicit unclassified program display."""
import argparse
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright
from check_home_notices import NoticeFixture


async def check(output):
    f=NoticeFixture()
    results=[];errors=[]
    def passed(name):
        results.append(name);print('PASS '+name,flush=True)
    async with async_playwright() as p:
        b=await p.chromium.launch(channel='msedge',headless=True,args=['--disable-background-networking'])
        ctx=await b.new_context(viewport={'width':960,'height':640},service_workers='block')
        await ctx.route('**/*',f.route)
        page=await ctx.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
        async def tick():
            await page.wait_for_function('!poll.busy')
            await page.evaluate('tick(true)')
            await page.wait_for_function('!poll.busy')
        async def ack_done():
            await page.wait_for_function('!newAckBusy')
        try:
            first={'process':'FirstApp','ts':100};second={'process':'SecondApp','ts':101}
            f.pending=[first,second]
            await page.goto('http://127.0.0.1:18799/?desk=1')
            await page.wait_for_selector('[data-ack-new]')
            await page.locator('[data-ack-new]').click(click_count=3)
            await ack_done()
            assert len(f.ack_calls)==1 and f.ack_calls[-1]==[first,second]
            assert await page.locator('[data-ack-new]').count()==0
            f.stale_news=[first,second]
            await tick()
            assert await page.locator('[data-ack-new]').count()==0
            f.stale_news=None
            passed('one request for triple click; successful ACK hides immediately despite repeated stale snapshots')

            f.pending=[{'process':'SlowApp','ts':200}]
            await tick()
            f.ack_started.clear();f.ack_gate=asyncio.Event()
            await page.locator('[data-ack-new]').click()
            await f.ack_started.wait()
            later={'process':'NewArrival','ts':201}
            f.pending.append(later)
            await tick()
            f.ack_gate.set();await ack_done();f.ack_gate=None
            assert f.pending==[later],f.pending
            assert 'NewArrival' in await page.locator('#quota').inner_text()
            await page.screenshot(path=str(output/'01-new-arrival-during-ack.png'))
            passed('new event arriving during slow ACK survives exact batch confirmation')

            f.ack_failure=True
            await page.locator('[data-ack-new]').focus()
            await page.keyboard.press('Enter');await ack_done()
            assert '确认失败' in await page.locator('#quota').inner_text()
            assert await page.locator('[data-ack-new]').is_enabled()
            assert await page.locator('[data-ack-new]').evaluate('e=>e===document.activeElement')
            await page.screenshot(path=str(output/'02-ack-failure.png'))
            f.ack_failure=False
            count=len(f.ack_calls)
            await page.keyboard.press('Space');await ack_done()
            assert len(f.ack_calls)==count+1 and not f.pending
            assert await page.locator('#filters button.on').evaluate('e=>e===document.activeElement')
            passed('failed ACK has visible retry feedback; Enter/Space, one request and returned focus')

            late={'process':'InFlightOld','ts':300};f.pending=[late]
            await tick()
            f.snapshot_started.clear();f.snapshot_gate=asyncio.Event()
            await page.evaluate('tick(false)')
            await f.snapshot_started.wait()
            await page.locator('[data-ack-new]').click();await ack_done()
            assert not f.pending
            f.snapshot_gate.set();f.snapshot_gate=None
            await page.wait_for_function('!poll.busy')
            assert await page.locator('[data-ack-new]').count()==0
            await page.reload();await page.wait_for_selector('[data-program]')
            assert await page.locator('[data-ack-new]').count()==0
            passed('snapshot requested before ACK and returned after ACK cannot resurrect notice; reload stays clear')

            await page.locator('#filters button').nth(1).click()
            await page.wait_for_function("filter==='rank' && !poll.busy")
            assert await page.locator('#context-tools').is_hidden()
            assert await page.locator('#context-tools [data-back]').count()==0
            assert await page.locator('#context-tools h2').count()==0
            await page.locator('#filter-summary').click()
            await page.locator('#periods button').nth(1).click()
            await page.wait_for_function('!poll.busy')
            assert await page.locator('#filters button').nth(1).get_attribute('aria-current')=='page'
            await page.screenshot(path=str(output/'03-clean-cumulative.png'))
            await page.locator('#filters button').first.click()
            await page.wait_for_selector('[data-program=msedge]')
            await page.locator('[data-program=msedge]').click()
            await page.locator('[data-stats-process=msedge]').click()
            await page.wait_for_function("filter==='rank' && !poll.busy")
            assert '程序：' in await page.locator('#context-tools').inner_text()
            assert await page.locator('[data-clear-scope]').is_visible()
            await page.locator('#filters button').first.click()
            await page.wait_for_selector('[data-connections=msedge]')
            await page.locator('[data-connections=msedge]').click()
            await page.wait_for_function("filter==='conns' && !poll.busy")
            assert await page.locator('[data-back]').is_visible()
            await page.locator('[data-back]').click()
            await page.wait_for_function("filter==='overview' && !poll.busy")
            passed('cumulative no redundant title/back/space; period stays selected; scoped statistics and connection return remain')
            # Preference request serialization and failure feedback are independent of route policy.
            f.scope_started.clear();f.scope_gate=asyncio.Event()
            await page.locator('[data-home-scope=proxy]').click()
            await f.scope_started.wait()
            await page.locator('[data-home-scope=all]').click()
            f.scope_gate.set();f.scope_gate=None
            await page.wait_for_function('!homeScopeSaving')
            assert f.scope_writes[-2:]==['proxy','all'],f.scope_writes
            assert f.settings['home_scope']=='all'
            f.save_failure=True
            await page.locator('[data-home-scope=proxy]').click()
            await page.wait_for_function('!homeScopeSaving')
            assert await page.locator('[data-home-retry]').is_visible()
            f.save_failure=False
            await page.locator('[data-home-retry]').click()
            await page.wait_for_function('!homeScopeSaving')
            assert f.settings['home_scope']=='proxy'
            await page.reload();await page.wait_for_selector('[data-program]')
            assert await page.evaluate('homeScope')=='proxy'
            await page.locator('[data-home-scope=all]').focus()
            await page.keyboard.press('Enter')
            await page.wait_for_function('!homeScopeSaving')
            assert await page.locator('[data-home-scope=all]').evaluate('e=>e===document.activeElement')
            await page.set_viewport_size({'width':640,'height':360})
            assert await page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
            assert await page.locator('[data-home-scope=all]').is_visible()
            passed('home preference requests serialize, failure can retry, reload restores choice, keyboard and narrow controls')
            f.scope_case=True
            await page.locator('[data-home-scope=proxy]').click()
            await page.wait_for_function('!homeScopeSaving')
            await tick()
            expected={'msedge','python','IdleProxy','ProxyUnknown','ProxyLeak'}
            names=set(await page.locator('[data-program]').evaluate_all('buttons=>buttons.map(b=>b.dataset.program)'))
            assert names==expected,names
            assert '混合' in await page.locator('[data-program=msedge] .route-label').inner_text()
            assert '部分未知' in await page.locator('[data-program=ProxyUnknown] .route-label').inner_text()
            assert '绕过 TUN' in await page.locator('[data-program=ProxyLeak] .route-label').inner_text()
            assert '代理' in await page.locator('[data-program=IdleProxy] .route-label').inner_text()
            await page.screenshot(path=str(output/'04-proxy-home.png'))
            await page.locator('#view-menu summary').click()
            await page.locator('[data-live-style=balls]').click()
            await page.wait_for_function("liveStyle==='balls' && !poll.busy")
            ball_names=set(await page.evaluate("well.balls.filter(b=>!b.dead).map(b=>wellPid(b))"))
            assert ball_names==expected,ball_names
            assert await page.evaluate("well.balls.some(b=>!b.dead && wellPid(b)==='IdleProxy' && b.lane==='proxy')")
            await page.locator('[data-home-scope=all]').click()
            await page.wait_for_function('!homeScopeSaving')
            all_names=set(await page.evaluate("well.balls.filter(b=>!b.dead).map(b=>wellPid(b))"))
            unclassified=set(await page.locator('[data-unclassified-process]').evaluate_all('els=>els.map(e=>e.dataset.unclassifiedProcess)'))
            assert 'UnknownOnly' not in all_names and 'UnknownOnly' in unclassified, (all_names,unclassified)
            assert 'steam' in all_names and 'BackupHelper' in all_names,all_names
            await page.locator('#filters button').first.click()
            await page.wait_for_function("liveStyle==='list' && !poll.busy")
            await page.locator('[data-program=steam]').click()
            await page.locator('[data-home-scope=proxy]').click()
            await page.wait_for_function('!homeScopeSaving')
            assert await page.locator('.program-detail').count()==0
            f.no_proxy=True
            await tick()
            assert '当前没有使用代理' in await page.locator('#live-programs').inner_text()
            await page.locator('[data-home-scope=all]').click()
            await page.wait_for_function('!homeScopeSaving')
            assert await page.locator('[data-program=steam]').count()==1
            await page.keyboard.press('/')
            await page.locator('#q').fill('no-matching-program')
            assert '没有匹配' in await page.locator('#live-programs').inner_text()
            await page.keyboard.press('Escape')
            f.no_programs=True
            await tick()
            assert '当前没有活跃程序' in await page.locator('#live-programs').inner_text()
            passed('proxy includes confirmed mixed/partial-unknown/leak and idle; excludes pure unknown/direct; balls/list agree, empty/search and hidden detail are correct')
            assert not errors,errors
        finally:
            await page.screenshot(path=str(output/'last-state.png'))
            (output/'report.json').write_text(json.dumps({'passed':results,'page_errors':errors,'ack_requests':len(f.ack_calls)},ensure_ascii=False,indent=2),encoding='utf-8')
            await b.close();f.rule_tmp.cleanup()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    asyncio.run(check(args.output))

