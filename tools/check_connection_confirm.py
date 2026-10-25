"""Confirm local draft against real AppSettings, with all browser traffic intercepted."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse

from check_gpt_network import GPTFixture
from connection_test_helpers import open_connection
from flashmonitor.app import MonitorApi
from flashmonitor.settings import AppSettings
from playwright.async_api import async_playwright
from tests.test_network_policy import APP


class ConfirmFixture(GPTFixture):
    def __init__(self):
        super().__init__();self.delay=0;self.disk_failure=False;self.http_failure=False;self.bad_receipt=False;self.read_failure=False
        self.calls=[];self.save_bodies=[];self.receipts=[]
        self.settings_path=Path(self.rule_tmp.name)/'settings.json'
        self.store=AppSettings(self.settings_path,on_saved=self.calls.append)
        self.store.apply({'clash_secret':'synthetic-existing-secret','sub_url':'https://synthetic.example/?token=existing'})
        self.calls.clear()
        self.ipv4_store.operate({'operation':'save','revision':0,'subject':{'kind':'domain','value':'retained.example','include_subdomains':False}})
        self.retained=self.ipv4_store.path.read_bytes()

    async def route(self,route):
        req=route.request;u=urlparse(req.url)
        if u.path=='/api/settings':
            if req.method=='GET':
                await route.fulfill(status=503 if self.read_failure else 200,json={'error':'synthetic read failure'} if self.read_failure else self.store.public());return
            self.posts.append(u.path);body=req.post_data_json;self.save_bodies.append(body)
            if self.delay:await asyncio.sleep(self.delay)
            if self.http_failure:await route.fulfill(status=503,json={'error':'synthetic save unavailable'});return
            if self.bad_receipt is True:await route.fulfill(json=self.store.public());return
            api=MonitorApi(SimpleNamespace(settings=self.store,settings_public=self.store.public))
            if self.disk_failure:
                with patch.object(Path,'replace',side_effect=PermissionError('synthetic disk denied: '+str(self.settings_path))):data=api.apply_settings(body)
            else:data=api.apply_settings(body)
            if self.bad_receipt=='after_commit':data.pop('connection_draft_saved',None)
            self.receipts.append({'saved':data.get('connection_draft_saved'),'network_applied':data.get('network_applied')})
            await route.fulfill(json=data);return
        if u.path in ['/api/network-apply','/api/network-undo','/api/ipv4-draft']:
            await route.abort();raise AssertionError('unexpected mutation '+u.path)
        await super().route(route)


async def check(out):
    f=ConfirmFixture();passed=[];errors=[]
    def ok(name):passed.append(name);print('PASS '+name,flush=True)
    async with async_playwright() as p:
        browser=await p.chromium.launch(channel='msedge',headless=True,args=['--disable-background-networking'])
        ctx=await browser.new_context(viewport={'width':1100,'height':800},service_workers='block');await ctx.route('**/*',f.route)
        page=await ctx.new_page();page.set_default_timeout(12000);page.on('pageerror',lambda e:errors.append(str(e)))
        async def opened():
            await open_connection(page)
            await page.wait_for_function("!document.getElementById('connection-confirm').disabled && document.getElementById('gpt-target').options.length>1")
        async def confirm():
            await page.locator('#connection-confirm').click();await page.wait_for_function('!connectionSaving')
        async def assert_open_error():
            assert await page.locator('#connection-dialog').evaluate('e=>e.open');assert await page.locator('#gpt-error').is_visible()
        try:
            await page.goto('http://127.0.0.1:18799/?desk=1');await page.wait_for_selector('#filters button');await opened()
            assert '保存草稿' in await page.locator('#connection-confirm').inner_text()
            before=f.settings_path.read_bytes();await confirm();await assert_open_error();assert f.settings_path.read_bytes()==before
            await page.locator('#gpt-domains').fill('https://invalid.example/?token=not-saved');await page.locator('#gpt-target').select_option('GPT');await confirm();await assert_open_error()
            assert await page.locator('#gpt-domains').input_value()=='https://invalid.example/?token=not-saved'
            assert f.settings_path.read_bytes()==before
            ok('bottom confirm is explicit local draft; empty and invalid URL validation preserve inputs and disk')

            await page.locator('#gpt-domains').fill('CHATGPT.COM');await page.locator('[data-gpt-domain="0"]').check();await page.locator('#gpt-ipv6').check();f.delay=.6;count=len(f.save_bodies)
            await page.locator('#connection-confirm').click(click_count=3);await page.wait_for_function('connectionSaving')
            assert await page.locator('#connection-close').is_disabled();await page.keyboard.press('Escape');assert await page.locator('#connection-dialog').evaluate('e=>e.open')
            await page.wait_for_function("!document.getElementById('connection-dialog').open")
            assert len(f.save_bodies)==count+1
            disk=json.loads(f.settings_path.read_text(encoding='utf-8'));saved=disk['connection_draft']
            assert saved['domains']==[{'value':'chatgpt.com','include_subdomains':True}] and saved['block_ipv6'] is True
            assert saved['applications']==[] and saved['target']=='GPT'
            assert f.receipts[-1]=={'saved':True,'network_applied':False};assert not f.calls
            f.delay=0;before=f.settings_path.read_bytes();await opened()
            assert await page.locator('#gpt-domains').input_value()=='chatgpt.com';assert await page.locator('[data-gpt-domain="0"]').is_checked();assert await page.locator('#gpt-ipv6').is_checked()
            assert '已恢复' in await page.locator('#connection-state').inner_text()
            ok('triple click commits once; pending close/Escape blocked; actual disk success receipt closes and restores normalized draft')

            count=len(f.save_bodies);await page.locator('#gpt-domains').fill('unsaved.example');await page.locator('#connection-close').click();await opened();assert await page.locator('#gpt-domains').input_value()=='chatgpt.com'
            await page.locator('#gpt-domains').fill('escape-unsaved.example');await page.keyboard.press('Escape');assert f.settings_path.read_bytes()==before
            await page.reload();await page.wait_for_selector('#filters button');f.store=AppSettings(f.settings_path,on_saved=f.calls.append);await opened();assert await page.locator('#gpt-domains').input_value()=='chatgpt.com'
            assert len(f.save_bodies)==count and f.settings_path.read_bytes()==before
            ok('close and Escape do not submit; reload plus new AppSettings instance restore only confirmed disk content')

            await page.locator('#gpt-domains').fill('retry.example');f.disk_failure=True;await confirm();await assert_open_error()
            assert await page.locator('#gpt-domains').input_value()=='retry.example';assert f.settings_path.read_bytes()==before;assert f.store.data['connection_draft']==saved
            await page.set_viewport_size({'width':480,'height':320});await page.locator('#connection-confirm').click(trial=True);await page.screenshot(path=str(out/'failure-480x320.png'));await page.set_viewport_size({'width':1100,'height':800})
            f.disk_failure=False;await confirm();await page.wait_for_function('!connectionDialog.open');assert AppSettings(f.settings_path).data['connection_draft']['domains'][0]['value']=='retry.example'
            ok('real file replacement failure retains window/input, previous disk and memory; explicit retry succeeds')

            await opened();before=f.settings_path.read_bytes();await page.locator('#gpt-domains').fill('not-acknowledged.example');f.bad_receipt=True;await confirm();await assert_open_error();assert f.settings_path.read_bytes()==before
            f.bad_receipt=False;f.http_failure=True;await confirm();await assert_open_error();assert await page.locator('#gpt-domains').input_value()=='not-acknowledged.example';assert f.settings_path.read_bytes()==before
            f.http_failure=False;f.bad_receipt='after_commit';await confirm();await assert_open_error()
            assert '未确认' in await page.locator('#connection-state').inner_text()
            assert AppSettings(f.settings_path).data['connection_draft']['domains'][0]['value']=='not-acknowledged.example'
            f.bad_receipt=False;await page.locator('#connection-close').click();await opened()
            assert await page.locator('#gpt-domains').input_value()=='not-acknowledged.example';await page.locator('#connection-close').click()
            ok('missing/failed or lost-after-commit receipt cannot falsely close/report success; reopen reads actual committed state')

            await opened();await page.locator('#gpt-kind').select_option('application');await page.locator('#gpt-apps input').first.check();await confirm();await page.wait_for_function('!connectionDialog.open')
            assert AppSettings(f.settings_path).data['connection_draft']['applications']==[APP['value']]
            f.state['flows']=f.state['flows'][1:];await opened();assert await page.locator('#gpt-apps input:disabled:checked').count()==1;assert await page.locator('#gpt-preview').is_disabled()
            assert await page.locator('#gpt-apps input:not([disabled]):checked').count()==0
            await page.locator('[data-gpt-remove]').click();await confirm();await assert_open_error()
            await page.locator('#gpt-apps input:not([disabled])').first.check();await confirm();await page.wait_for_function('!connectionDialog.open')
            assert AppSettings(f.settings_path).data['connection_draft']['applications']==[r'D:\Other\browser.exe']
            ok('restored missing identity is visibly pending, never substituted; removal validates, explicit replacement persists exact chosen path')

            await page.keyboard.press('2');await page.wait_for_function("filter==='rank' && !poll.busy");await page.locator('[data-fold="rank:msedge"]').click();await page.locator('[data-site-row="msedge.proxy.example.com"] [data-connection-domain]').click();await page.wait_for_function("!document.getElementById('connection-confirm').disabled")
            assert await page.locator('#gpt-kind').input_value()=='domain';assert await page.locator('#gpt-domains').input_value()=='msedge.proxy.example.com';assert await page.locator('#gpt-apps input:checked').count()==0
            await page.locator('#connection-close').click();await opened();assert await page.locator('#gpt-kind').input_value()=='application';assert await page.locator('#gpt-apps input:checked').input_value()==r'D:\Other\browser.exe'
            for width,height in [(1100,800),(480,640),(640,360),(480,320)]:
                await page.set_viewport_size({'width':width,'height':height});await page.locator('#connection-confirm').click(trial=True)
                assert await page.locator('#connection-dialog').evaluate('e=>e.scrollWidth<=e.clientWidth');await page.screenshot(path=str(out/f'confirm-{width}x{height}.png'))
            await page.locator('#connection-close').click()
            ok('different context retains its own scope without overwriting saved choice; generic reopening restores it; confirm reachable at four viewport sizes')

            await page.set_viewport_size({'width':1100,'height':800});f.read_failure=True;await open_connection(page);await page.wait_for_selector('#gpt-error:not([hidden])')
            assert await page.locator('#connection-confirm').is_disabled();assert await page.locator('#gpt-refresh').is_enabled();f.read_failure=False;await page.locator('#gpt-refresh').click();await page.wait_for_function("!document.getElementById('connection-confirm').disabled")
            assert await page.locator('#gpt-apps input:checked').input_value()==r'D:\Other\browser.exe'
            assert await page.locator('#ipv4-dialog,[data-connection-mode]').count()==0
            assert f.ipv4_store.path.read_bytes()==f.retained
            assert all(set(body)=={'connection_draft'} for body in f.save_bodies)
            assert 'synthetic-existing-secret' not in json.dumps(f.save_bodies) and 'https://synthetic.example' not in json.dumps(f.save_bodies)
            disk=json.loads(f.settings_path.read_text(encoding='utf-8'));assert disk['clash_secret']=='synthetic-existing-secret' and disk['sub_url']=='https://synthetic.example/?token=existing'
            assert not f.calls and set(f.posts)=={'/api/settings'};assert not errors,errors
            ok('failed draft read blocks confirmation and can retry; no credentials/new network operations/IPv4 UI, retained IPv4 bytes unchanged')
        finally:
            (out/'report.json').write_text(json.dumps({'passed':passed,'page_errors':errors,'settings_post_count':len(f.save_bodies),'receipts':f.receipts,'posts':f.posts,'callbacks':len(f.calls),'retained_ipv4_sha256':hashlib.sha256(f.retained).hexdigest(),'retained_ipv4_unchanged':f.ipv4_store.path.read_bytes()==f.retained,'requests_intercepted':True,'real_user_data_modified':False},ensure_ascii=False,indent=2),encoding='utf-8')
            await browser.close();f.rule_tmp.cleanup()


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path,required=True);args=ap.parse_args();args.output.mkdir(parents=True,exist_ok=True);asyncio.run(check(args.output))
