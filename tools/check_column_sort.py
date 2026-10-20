"""Column-sort UI and pure numeric regression; all requests intercepted, installed Edge."""
import argparse
import asyncio
import copy
import json
from pathlib import Path
from check_ui import Fixture
from playwright.async_api import async_playwright


class SortFixture(Fixture):
    def __init__(self):
        super().__init__()
        self.rows=[
            dict(process='Giga',download=2*1024**3,upload=0,proxy_down=1024**3,proxy_up=0),
            dict(process='Small',download=900*1024,upload=0,proxy_down=800*1024,proxy_up=0),
            dict(process='Mega',download=2*1024**2,upload=0,proxy_down=0,proxy_up=0),
            dict(process='UploadOnly',download=0,upload=5*1024**2,proxy_down=0,proxy_up=4*1024**2),
            dict(process='TieB',download=1000,upload=0,proxy_down=100,proxy_up=0),
            dict(process='TieA',download=1000,upload=0,proxy_down=100,proxy_up=0),
            dict(process='Missing'),dict(process='Zero',download=0,upload=0),
            dict(process='Invalid',download='bad',upload=-5),
        ]
        self.empty=False

    def snapshot(self,view,period):
        data=super().snapshot(view,period)
        if view=='rank':
            pack=data['rank'][period]
            pack['rows']=[] if self.empty else copy.deepcopy(self.rows)
            pack['sites']['rows']=[dict(site=r['process']+'.example',download=r.get('download'),upload=r.get('upload'),proxy_total=r.get('proxy_down',0)+r.get('proxy_up',0)) for r in self.rows]
            pack['nodes']['rows']=[dict(node=r['process'],download=r.get('download'),upload=r.get('upload')) for r in self.rows]
        return data


async def check(output):
    f=SortFixture();errors=[];results=[]
    def passed(name):results.append(name);print('PASS '+name,flush=True)
    async with async_playwright() as p:
        b=await p.chromium.launch(channel='msedge',headless=True,args=['--disable-background-networking'])
        ctx=await b.new_context(viewport={'width':960,'height':640},service_workers='block')
        await ctx.route('**/*',f.route)
        page=await ctx.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
        async def tick():
            await page.wait_for_function('!poll.busy');await page.evaluate('tick(false)');await page.wait_for_function('!poll.busy')
        async def names():return await page.locator('[data-rank-row]').evaluate_all('rows=>rows.map(r=>r.dataset.rankRow)')
        def value(row,key):
            def byte(n):return n if isinstance(n,(int,float)) and n>0 else 0
            total=byte(row.get('download'))+byte(row.get('upload'))
            proxy=byte(row.get('proxy_down'))+byte(row.get('proxy_up'))
            return proxy if key=='proxy' else max(0,total-proxy) if key=='direct' else total
        def expected(key,asc):return [r['process'] for r in sorted(f.rows,key=lambda r:((1 if asc else -1)*value(r,key),r['process']))]
        try:
            await page.goto('http://127.0.0.1:18799/?desk=1')
            await page.wait_for_selector('[data-program]')
            await page.locator('#filters button').nth(1).click()
            await page.wait_for_selector('[data-rank-sort=proxy]')
            numeric=await page.evaluate("""() => {
                const previous=rankSort;
                const values=[null,undefined,NaN,Infinity,-1,0,'bad'].map(byteValue);
                const sample={download:30,upload:10,proxy_down:20,proxy_up:5};
                const rows=[{process:'B',download:1025},{process:'A',download:1025},{process:'C',download:1026},{process:'Missing'}];
                rankSort={key:'total',direction:'desc'};
                const desc=sortRankRows(rows,r=>r.process).map(r=>r.process);
                rankSort={key:'total',direction:'asc'};
                const asc=sortRankRows(rows.slice().reverse(),r=>r.process).map(r=>r.process);
                rankSort=previous;
                return {values,metrics:[statProxy(sample),statDirect(sample),statTotal(sample)],desc,asc,
                    sameFormatted:fmtBytes(1025)===fmtBytes(1026)};
            }""")
            assert numeric=={'values':[0]*7,'metrics':[25,15,40],'desc':['C','A','B','Missing'],
                             'asc':['Missing','A','B','C'],'sameFormatted':True},numeric
            passed('numeric unit cases: missing/nonfinite/negative zero, download+upload metrics, rounded-equal raw bytes and fixed ties')
            initial_summary=await page.locator('.sum').inner_text()
            for key in ['proxy','direct','total']:
                await page.locator(f'[data-rank-sort={key}]').click()
                assert await names()==expected(key,False),(key,await names(),expected(key,False))
                assert await page.locator(f'[data-rank-sort={key}]').evaluate("e=>e.closest('th').getAttribute('aria-sort')")=='descending'
                assert await page.locator(f'[data-rank-sort={key}] .sort-arrow').inner_text()=='↓'
                await page.keyboard.press('Enter')
                assert await names()==expected(key,True),(key,await names())
                assert await page.locator(f'[data-rank-sort={key}]').evaluate("e=>e.closest('th').getAttribute('aria-sort')")=='ascending'
                assert await page.locator(f'[data-rank-sort={key}] .sort-arrow').inner_text()=='↑'
                assert await page.locator('.sum').inner_text()==initial_summary
                assert await page.evaluate("trafficScope==='all' && homeScope==='all'")
            passed('proxy/direct/total descending then ascending, raw KB/MB/GB/upload values, missing/invalid zero and unchanged summary/filter')

            await page.evaluate("window.sortButton=document.activeElement")
            f.rows.reverse()
            await tick()
            assert await names()==expected('total',True)
            assert await page.evaluate('document.activeElement===sortButton && sortButton.isConnected')
            f.rows[0]['download']=3*1024**3
            await tick()
            assert await names()==expected('total',True)
            assert await page.evaluate('document.activeElement===sortButton && sortButton.isConnected')
            await page.reload();await page.wait_for_selector('[data-rank-row]')
            assert await names()==expected('total',True)
            assert await page.evaluate("rankSort.key==='total' && rankSort.direction==='asc'")
            passed('tie order stable on shuffled refresh, focused button survives updating sorted values, browser reload retains sorting')

            await page.locator('#filter-summary').click()
            for index in range(5):
                await page.locator('#periods button').nth(index).click()
                await page.wait_for_function('!poll.busy')
                assert await names()==expected('total',True)
            await page.locator('#sort').select_option('direct');await page.wait_for_function('!poll.busy')
            wanted=[n for n in expected('total',True) if value(next(r for r in f.rows if r['process']==n),'direct')>0]
            assert await names()==wanted
            await page.locator('[data-rank-sort=proxy]').click()
            assert await page.evaluate("trafficScope==='direct'")
            assert set(await names())==set(wanted)
            await page.locator('#sort').select_option('all');await page.wait_for_function('!poll.busy')
            await page.evaluate("gotoFilter('rank','',{process:'TieA'})")
            await page.wait_for_function('!poll.busy')
            assert await names()==['TieA']
            await page.locator('[data-clear-scope]').click()
            await page.wait_for_function('(n)=>document.querySelectorAll("[data-rank-row]").length===n',arg=len(f.rows))
            assert len(await names())==len(f.rows)
            passed('all cumulative periods and program scope retain sort; sorting changes order without changing traffic eligibility')

            if not await page.locator('#filter-options').evaluate('e=>e.open'): await page.locator('#filter-summary').click()
            await page.locator('#rank-modes button').nth(1).click();await page.wait_for_function('!poll.busy')
            await page.locator('[data-rank-sort=direct]').click()
            assert await names()==[n+'.example' for n in expected('direct',False)]
            await page.locator('#rank-modes button').nth(2).click();await page.wait_for_function('!poll.busy')
            assert await page.locator('[data-rank-sort=proxy]').count()==0
            await page.locator('[data-rank-sort=total]').click()
            assert await names()==expected('total',False)
            passed('website proxy/direct use aggregate bytes; node total uses download+upload without invented direct column')

            await page.locator('#rank-modes button').first.click();await page.wait_for_function('!poll.busy')
            await page.set_viewport_size({'width':640,'height':360})
            await page.locator('[data-rank-sort=proxy]').focus();await page.keyboard.press('Space')
            assert await names()==expected('proxy',False)
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            await page.screenshot(path=str(output/'01-sort-640.png'))
            f.empty=True;await tick()
            assert await page.locator('[data-rank-row]').count()==0
            assert '还没有流量' in await page.locator('#main').inner_text()
            f.empty=False;await tick()
            assert await names()==expected('proxy',False)
            passed('keyboard Space and low window layout; empty snapshot and recovery keep chosen sorting')

            if not await page.locator('#filter-options').evaluate('e=>e.open'):
                await page.locator('#filter-summary').click()
            await page.evaluate("rankSort={key:null,direction:'desc'};render(true)")
            for period_index in range(5):
                await page.locator('#periods button').nth(period_index).click()
                await page.wait_for_function('!poll.busy')
                for scope in ['all','proxy','direct']:
                    await page.locator('#sort').select_option(scope)
                    await page.wait_for_function('!poll.busy')
                    for key in ['proxy','direct','total']:
                        for ascending in [False,True]:
                            await page.locator(f'[data-rank-sort={key}]').click()
                            wanted=expected(key,ascending)
                            if scope!='all':
                                wanted=[n for n in wanted if value(next(r for r in f.rows if r['process']==n),scope)>0]
                            assert await names()==wanted,(period_index,scope,key,ascending,await names())
                            assert await page.evaluate('trafficScope')==scope
            passed('full 5-period x 3-traffic-scope x 3-column x 2-direction matrix')
            assert not errors,errors
        finally:
            await page.screenshot(path=str(output/'last-state.png'))
            (output/'report.json').write_text(json.dumps({'passed':results,'page_errors':errors},ensure_ascii=False,indent=2),encoding='utf-8')
            await b.close();f.rule_tmp.cleanup()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True);asyncio.run(check(args.output))
