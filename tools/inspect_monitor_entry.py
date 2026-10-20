"""Read-only rendered check of the actual local Monitor server, never posts or changes prefs."""
import argparse
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright


async def inspect(output,phase):
    output.mkdir(parents=True,exist_ok=True);blocked=[];errors=[]
    async with async_playwright() as p:
        b=await p.chromium.launch(channel='msedge',headless=True,args=['--disable-background-networking'])
        ctx=await b.new_context(viewport=dict(width=1100,height=750),service_workers='block')
        async def readonly(route):
            r=route.request
            if r.method!='GET' or not r.url.startswith('http://127.0.0.1:8787/'):
                blocked.append({'method':r.method,'path':r.url.split('?')[0]});await route.abort();return
            await route.continue_()
        await ctx.route('**/*',readonly);page=await ctx.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
        try:
            await page.goto('http://127.0.0.1:8787/?desk=1')
            await page.wait_for_function('last && !poll.busy')
            await page.screenshot(path=str(output/f'{phase}-home.png'))
            direct=page.locator('#context-tools [data-ipv6-entry]')
            result={'phase':phase,'direct_entry_count':await direct.count(),'page_errors':errors}
            if phase=='before':
                await page.locator('#view-menu summary').click();await page.locator('#btn-drafts').click()
                await page.locator('#btn-ipv4-drafts').click();await page.wait_for_function('!ipv4Busy && ipv4Data')
                result['old_entry_visible']=await page.locator('#ipv4-ipv6').is_visible()
                result['old_entry_label']=await page.locator('#ipv4-ipv6').inner_text()
                await page.screenshot(path=str(output/'before-hidden-entry.png'))
            else:
                assert await direct.is_visible()
                assert 'IPv6 限制方案' in await direct.inner_text()
                await direct.click();await page.wait_for_function('!ipv6Busy && ipv6Data')
                assert await page.locator('#ipv6-kind').is_visible()
                assert await page.locator('#ipv6-apply').count()==0
                assert await page.locator('#ipv6-title').inner_text()=='IPv6 限制方案'
                await page.screenshot(path=str(output/'after-picker.png'))
                result['applications']=await page.locator('#ipv6-app option').count()-1
                result['automatic_submit_available']=False
                result['backend_capabilities']=await page.evaluate('ipv6Data.capabilities || null')
                result['legacy_drafts']=await page.evaluate('ipv6Data.plans.filter(p=>!p.persistent?.target_root).length')
                if await page.locator('[data-ipv6-view]').count():
                    await page.locator('[data-ipv6-view]').first.click();await page.wait_for_timeout(500)
                    result['saved_continue_summary']=await page.locator('#ipv6-summary').inner_text()
                    result['saved_continue_error']=await page.locator('#ipv6-error').inner_text()
                    await page.screenshot(path=str(output/'saved-continue-readonly.png'))
                await page.locator('#ipv6-close').click();await page.locator('#view-menu summary').click()
                menu=page.locator('#view-menu [data-ipv6-entry]')
                result['menu_entry_visible']=await menu.is_visible()
                await page.screenshot(path=str(output/'after-menu.png'))
                await page.locator('#view-menu summary').click();await page.locator('#filters button').nth(1).click()
                await page.wait_for_selector('[data-usage-page=ipv6]');await page.locator('[data-usage-page=ipv6]').click()
                await page.wait_for_function("usagePage==='ipv6' && ipv6Data && !banLoading")
                result['usage_ban_page_visible']=await page.locator('#main [data-ipv6-entry]').is_visible()
                await page.screenshot(path=str(output/'usage-ban-page.png'))
                await page.locator('#filters button').first.click();await page.locator('#context-tools [data-connections]').click()
                await page.wait_for_function("filter==='conns' && !poll.busy")
                result['connection_headers']=await page.locator('#main th').all_text_contents()
                await page.screenshot(path=str(output/'connection-detail.png'))
            result['blocked_non_read_requests']=blocked
            (output/f'{phase}-report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(result,ensure_ascii=False))
        finally:await b.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--phase',choices=['before','after'],required=True)
    args=parser.parse_args();asyncio.run(inspect(args.output,args.phase))
