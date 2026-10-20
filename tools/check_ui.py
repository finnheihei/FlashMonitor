"""Optional Edge UI regression check. Requires an already installed Playwright.

All browser requests are intercepted; no application backend or Clash is started.
Run: python tools/check_ui.py --output <temporary evidence directory>
"""

import argparse
import asyncio
import copy
import json
import tempfile
import time
import sys
from types import SimpleNamespace
from pathlib import Path
from connection_test_helpers import open_rule
from urllib.parse import parse_qs, urlparse

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from flashmonitor.app import MonitorApi
from flashmonitor.rule_drafts import RuleDrafts, DraftError
from flashmonitor.ipv4_drafts import IPv4Drafts


class Fixture:
    def __init__(self) -> None:
        self.settings = dict(version="0.9.0", last_period="today", ignored=[], pinned=[], show_airport=True,
                             clash_api="http://127.0.0.1:19090", close_to_tray=True, license="MIT")
        self.failure = False
        self.close_failure = True
        self.save_failure = False
        self.close_calls = 0
        self.test_calls = 0
        self.closed_count = None
        self.rule_failure=False
        self.rule_context_failure=False
        self.rule_saves=0
        self.rule_tmp=tempfile.TemporaryDirectory()
        self.rule_store=RuleDrafts(Path(self.rule_tmp.name)/"rule-drafts.json")
        self.ipv4_store=IPv4Drafts(Path(self.rule_tmp.name)/"ipv4-drafts.json")
        self.rule_api=MonitorApi(SimpleNamespace(rule_drafts=self.rule_store,
            ipv4_drafts=self.ipv4_store,
            live=SimpleNamespace(get=lambda:self.snapshot("overview","today")),
            clash=SimpleNamespace(rule_inventory=lambda:dict(groups=["Demo group","Group 'quoted'"],rules=[dict(exe="msedge.exe",target="Other group")],groups_error="",rules_error=""))))

    def snapshot(self, view: str, period: str) -> dict:
        programs = [("msedge", "Edge", 1000, 300), ("steam", "Steam", 0, 2000), ("python", "Python", 10, 0)]
        factor = {"today": 10, "all": 100, "1h": 1, "7d": 30, "30d": 60}[period]
        eaters, flows, rows = [], [], []
        for name, title, proxy, direct in programs:
            eaters.append(dict(process=name, title=title, down=proxy+direct, up=0, proxy_down=proxy,
                               direct_down=direct, proxy_up=0, direct_up=0, conns=2*sum(bool(n) for n in (proxy,direct))))
            sites = []
            for lane, amount in (("proxy", proxy), ("direct", direct)):
                if not amount:
                    continue
                host = name+"."+lane+".example.com"
                flows.append(dict(process=name, title=title, rule_exe=name+".exe", host=host, remote=host+":443", path=lane, outbound=lane,
                                  note="模拟规则", node="Demo node" if lane=="proxy" else "", n=2,
                                  ids=[name+lane+"1", name+lane+"2"], upload=0, download=amount))
                sites.append(dict(site=host, upload=0, download=amount*factor,
                                  proxy_down=amount*factor if lane=="proxy" else 0, proxy_up=0,
                                  direct_down=amount*factor if lane=="direct" else 0, direct_up=0))
            rows.append(dict(process=name,title=title,upload=0,download=(proxy+direct)*factor,
                             proxy_down=proxy*factor,proxy_up=0,direct_down=direct*factor,direct_up=0,
                             sites=sites,sites_period=period,sites_since=int(time.time())-86400))
        flows.append(dict(process="BackupHelper",title="Backup Helper",remote="backup.example.com:443",
                          path="leak",outbound="direct",note="模拟绕过 TUN",n=4,ids=[],download=0,upload=0))
        total = sum(r["download"] for r in rows)
        proxy = sum(r["proxy_down"] for r in rows)
        pack = dict(rows=rows,download=total,upload=0,proxy_download=proxy,proxy_upload=0,
                    direct_download=total-proxy,direct_upload=0,
                    nodes=dict(rows=[dict(node="Demo node",download=proxy,upload=0)],download=proxy,upload=0),
                    sites=dict(rows=[dict(site=f["remote"],download=f["download"]*factor,upload=0,
                                          proxy_total=f["download"]*factor if f["outbound"]=="proxy" else 0,
                                          direct_total=f["download"]*factor if f["outbound"]=="direct" else 0)
                                     for f in flows if f["path"]!="leak"],download=total,upload=0))
        out = dict(view=view,period=period,ts=time.time(),status=dict(clash_api=True,tun_up=True,mode="tun",version="0.9.0",nodes=["Demo node"]),
                   prefs=copy.deepcopy(self.settings),alerts=[dict(kind="leak",count=4,names=["BackupHelper"])],
                   sub={},live=dict(eaters=eaters,down=3310,up=0,proxy_down=1010,direct_down=2300))
        if view=="overview":
            out.update(flows=flows,counts=dict(proxy=4,direct=4,leak=4),history=[dict(t=time.time()-i*2,proxy_down=1010,direct_down=2300,up=0) for i in range(20)],
                       rank={"today":{**{k:v for k,v in pack.items() if k not in ("rows","nodes","sites")},"programs":len(rows)}})
        elif view=="rank":
            out["rank"]={period:pack}
        elif view=="conns":
            out["flows"]=flows
        elif view=="closed":
            out["closed"]=[{**f,"end":time.time()-20} for f in flows if f["path"]!="leak"]
        return out

    async def route(self, route) -> None:
        request=route.request
        url=urlparse(request.url)
        if url.hostname!="127.0.0.1" or url.port!=18799:
            await route.abort()
            return
        path=url.path
        if path=="/":
            await route.fulfill(content_type="text/html; charset=utf-8",body=(ROOT/"ui.html").read_text(encoding="utf-8"))
            return
        code,data=200,{}
        if path=="/api/ipv4-drafts":
            data=self.rule_api.ipv4_context()
        elif path=="/api/ipv4-draft":
            try: data=self.rule_api.ipv4_draft(request.post_data_json)
            except DraftError as exc: code,data=400,dict(error=str(exc))
        elif path=="/api/rule-drafts":
            data=self.rule_api.rule_catalog()
        elif path=="/api/rule-context":
            if self.rule_context_failure:
                code,data=500,dict(error="模拟读取失败")
            else:
                data=self.rule_api.rule_context(parse_qs(url.query)["process"][0])
        elif path=="/api/rule-draft":
            body=request.post_data_json
            if body["operation"]=="save":
                self.rule_saves+=1
                await asyncio.sleep(0.3)
            if self.rule_failure:
                code,data=500,dict(error="模拟草稿保存失败")
            else:
                try: data=self.rule_api.rule_draft(body)
                except DraftError as exc: code,data=400,dict(error=str(exc))
        elif path=="/api/settings":
            if request.method=="POST":
                if self.save_failure:
                    code,data=500,{"error":"模拟保存失败"}
                else:
                    self.settings.update(request.post_data_json)
            if code==200:
                data=copy.deepcopy(self.settings)
        elif path=="/api/snapshot":
            if self.failure:
                code,data=503,{"error":"模拟后台断线"}
            else:
                query=parse_qs(url.query)
                data=self.snapshot(query.get("view",["overview"])[0],query.get("period",["today"])[0])
        elif path=="/api/update":
            data=dict(current="0.9.0",latest="0.9.0",newer=False)
        elif path=="/api/close":
            self.close_calls+=1
            await asyncio.sleep(0.4)
            code,data=(500,{"error":"模拟断开失败"}) if self.close_failure else (200,{"closed":self.closed_count if self.closed_count is not None else len(request.post_data_json["ids"])})
        elif path=="/api/test-clash":
            self.test_calls+=1
            await asyncio.sleep(0.4)
            data=dict(ok=False,error="模拟接入失败")
        else:
            code=404
        await route.fulfill(status=code,content_type="application/json",body=json.dumps(data,ensure_ascii=False))


async def check(output: Path) -> None:
    fixture=Fixture()
    results=[]
    errors=[]
    def verified(name: str) -> None:
        results.append(name)
        print("PASS "+name,flush=True)
    async with async_playwright() as p:
        browser=await p.chromium.launch(channel="msedge",headless=True,args=["--disable-background-networking"])
        context=await browser.new_context(viewport=dict(width=1280,height=800),service_workers="block",locale="zh-CN")
        await context.route("**/*",fixture.route)
        page=await context.new_page()
        page.on("pageerror",lambda e:errors.append(str(e)))
        async def settle() -> None:
            await page.wait_for_function("!poll.busy")
            await page.wait_for_timeout(100)
        async def tick() -> None:
            await settle()
            await page.evaluate("tick(true)")
            await settle()
        async def nav(index: int) -> None:
            await page.locator("#filters button").nth(index).click()
            await settle()
        async def filters() -> None:
            assert await page.locator("#filter-options").is_visible()
        try:
            await page.goto("http://127.0.0.1:18799/?desk=1")
            await page.wait_for_selector("[data-program=BackupHelper]")
            assert await page.locator("#filters button").all_text_contents()==["实时","用量"]
            assert not await page.locator(".cell.air").count()
            assert await page.locator("#quota [data-open-leaks]").count()==0
            assert await page.locator("tr[data-context-process=msedge] .route-cell").inner_text()=="混合（代理 + 直连）"
            assert await page.locator("tr[data-context-process=steam] .route-cell").inner_text()=="直连"
            assert await page.locator("tr[data-context-process=python] .route-cell").inner_text()=="代理"
            assert "绕过 TUN（直连）" in await page.locator("tr[data-context-process=BackupHelper] .route-cell").inner_text()
            states=await page.evaluate("""() => {
                const saved=last.flows;
                const p={process:'test',conns:0,proxy_down:999,direct_down:999};
                const status=flows=>{last.flows=flows.map(f=>({process:'test',...f}));return liveRouteStatus(p).text;};
                const result=[status([]),status([{outbound:'unknown',path:'sysproxy'}]),
                    status([{outbound:'proxy'}]),status([{outbound:'direct'}]),
                    status([{outbound:'proxy'},{outbound:'direct'}]),
                    status([{outbound:'proxy'},{outbound:'unknown'}]),
                    status([{outbound:'proxy'},{outbound:'direct',path:'leak',n:2}])];
                last.flows=saved;return result;
            }""")
            assert states==["无连接","未知","代理","直连","混合（代理 + 直连）","代理 · 部分未知","混合（代理 + 直连） · 绕过 TUN 2 条"],states
            verified("connection-derived proxy/direct/mixed/unknown/idle/leak states; rate totals cannot misclassify")
            await page.screenshot(path=str(output/"01-live-list.png"))
            assert await page.locator("#well-side").count()==0
            assert await page.locator("#q").is_hidden()
            assert await page.locator("#filter-options").is_hidden()
            await page.locator("#view-menu summary").click()
            await page.keyboard.press("Escape")
            assert not await page.locator("#view-menu").evaluate("e=>e.open")
            assert await page.locator("#view-menu summary").evaluate("e=>e===document.activeElement")
            await page.keyboard.press("/")
            await page.locator("#q").fill("Edge")
            await page.keyboard.press("Escape")
            assert await page.locator("#q").is_hidden()
            assert await page.locator("#q").input_value()==""
            verified("minimal default, no default detail/filter/search, menu Escape focus and on-demand search")
            await nav(1)
            assert await page.evaluate("period")=="today"
            assert await page.locator("#filters button").nth(1).get_attribute("aria-current")=="page"
            await filters()
            await page.locator("#periods button").nth(1).click()
            await settle()
            assert await page.evaluate("period")=="today"
            assert await page.locator("#filters button").nth(1).get_attribute("aria-current")=="page"
            await nav(0)
            await nav(1)
            assert await page.evaluate("period")=="today"
            assert await page.locator("#sort").input_value()=="proxy"
            await page.screenshot(path=str(output/"08-cumulative.png"))
            await page.go_back()
            await settle()
            assert await page.evaluate("filter")=="overview"
            await page.go_forward()
            await settle()
            assert await page.evaluate("filter==='rank' && period==='today'")
            await nav(0)
            verified("usage defaults to today and proxy traffic; browser back/forward")
            await page.locator("[data-program=msedge]").click()
            await open_rule(page,'msedge')
            await page.wait_for_selector("#rule-form:not([hidden])")
            assert "msedge.exe" in await page.locator("#rule-identity").inner_text()
            await page.locator("#rule-preview-btn").click()
            await page.wait_for_function("!ruleEditor.busy")
            assert "PROCESS-NAME,msedge.exe,DIRECT" in await page.locator("#rule-preview").inner_text()
            assert "已知冲突" in await page.locator("#rule-notes").inner_text()
            await page.locator("#rule-save").click(click_count=3)
            await page.wait_for_function("!ruleEditor.busy")
            assert fixture.rule_saves==1
            assert "已保存本地草稿 · 未应用" in await page.locator("#rule-status").inner_text()
            async with page.expect_download() as draft_download:
                await page.locator("#rule-export").click()
            draft_artifact=await draft_download.value
            await draft_artifact.save_as(str(output/"process-rule-draft.yaml"))
            assert "未应用" in (output/"process-rule-draft.yaml").read_text(encoding="utf-8")
            await page.wait_for_function("!ruleEditor.busy")
            await page.locator("input[name=rule-action][value=proxy]").check()
            await page.locator("#rule-group").select_option("Group 'quoted'")
            await page.locator("#rule-cancel").click()
            assert await page.locator("[data-rule-edit=msedge]").evaluate("e=>e===document.activeElement")
            await open_rule(page,'msedge')
            await page.wait_for_selector("#rule-form:not([hidden])")
            assert await page.locator("input[name=rule-action][value=direct]").is_checked()
            await page.locator("input[name=rule-action][value=proxy]").check()
            await page.locator("#rule-group").select_option("Group 'quoted'")
            await page.locator("#rule-preview-btn").click()
            await page.wait_for_function("!ruleEditor.busy")
            fixture.rule_failure=True
            await page.locator("#rule-save").click()
            await page.wait_for_selector("#rule-error:not([hidden])")
            assert await page.locator("#rule-group").input_value()=="Group 'quoted'"
            assert fixture.rule_store.view("msedge")["draft"]["target"]=="DIRECT"
            fixture.rule_failure=False
            await page.locator("#rule-save").click()
            await page.wait_for_function("!ruleEditor.busy")
            await page.screenshot(path=str(output/"09-process-rule-draft.png"))
            await page.set_viewport_size(dict(width=768,height=512))
            await page.locator("#rule-cancel").focus()
            await page.keyboard.press("Shift+Tab")
            assert await page.evaluate("document.getElementById('rule-dialog').contains(document.activeElement)")
            assert await page.locator("#rule-dialog").evaluate("e=>e.scrollWidth<=e.clientWidth")
            await page.screenshot(path=str(output/"10-process-rule-draft-768.png"))
            await page.set_viewport_size(dict(width=1280,height=800))
            await page.locator("input[name=rule-action][value=follow]").check()
            await page.locator("#rule-save").click()
            await page.wait_for_function("!ruleEditor.busy")
            assert fixture.rule_store.view("msedge")["draft"] is None
            await page.keyboard.press("Escape")
            await page.locator("#connection-close").click()
            if not await page.locator("[data-connections=BackupHelper]").count():
                await page.locator("[data-program=BackupHelper]").click()
            await open_rule(page,'BackupHelper')
            await page.wait_for_selector("#rule-form:not([hidden])")
            assert await page.locator("#rule-preview-btn").is_disabled()
            assert await page.locator("#rule-save").is_disabled()
            await page.keyboard.press("Escape")
            fixture.rule_context_failure=True
            await open_rule(page,'BackupHelper')
            await page.wait_for_selector("#rule-error:not([hidden])")
            await page.keyboard.press("Escape")
            fixture.rule_context_failure=False
            await page.keyboard.press("Escape")
            verified("rule drafts preview/save/export, conflict, cancel/reopen, focus, failed save, follow/delete, missing identity and context failure")
            await page.locator("#status [data-open-leaks]").click()
            await settle()
            assert await page.locator("tbody tr").count()==1
            assert await page.locator("[data-close]").count()==0
            await page.locator("[data-back]").click()
            await page.wait_for_selector("[data-program=BackupHelper]")
            if not await page.locator("[data-connections=BackupHelper]").count():
                await page.locator("[data-program=BackupHelper]").click()
            await page.wait_for_selector("[data-connections=BackupHelper]")
            assert await page.evaluate("wellLeakN('BackupHelper')")==4
            assert await page.evaluate("""() => {
                last.flows.push({...last.flows.find(f=>f.path==='leak'),process:'msedge'});
                const count=livePrograms().find(p=>p.process==='msedge').conns;
                last.flows.pop();
                return count;
            }""")==8
            side=await page.locator("#well-side").inner_text()
            assert "msedge.proxy" not in side
            await page.locator("[data-connections=BackupHelper]").first.click()
            await settle()
            assert await page.locator("tbody tr").count()==1
            assert "4" in await page.locator(".sum").inner_text()
            await page.go_back()
            await settle()
            assert await page.evaluate("well.sel") == "BackupHelper"
            verified("native-only program detail, aggregated connection count, scoped drilldown and browser back")

            await page.locator("[data-program=msedge]").click()
            await page.locator("[data-stats-process=msedge]").click()
            await settle()
            assert await page.locator("tbody tr").count()==1
            assert "Edge" in await page.locator("tbody").inner_text()
            await page.go_back()
            await settle()
            await page.locator("[data-connections=msedge]").first.click()
            await settle()
            assert await page.locator("tbody tr").count()==2
            await page.locator("[data-conn-mode=closed]").click()
            await settle()
            assert await page.locator("tbody tr").count()==2
            assert await page.locator("[data-close]").count()==0
            await page.locator("[data-conn-mode=live]").click()
            await settle()
            await page.locator("[data-close]").first.click(click_count=3)
            await page.wait_for_timeout(600)
            assert fixture.close_calls==1
            assert "断开失败" in await page.locator("#toast").inner_text()
            fixture.close_failure=False
            fixture.closed_count=1
            await page.locator("[data-close]").first.click()
            await page.wait_for_timeout(600)
            assert "1/2" in await page.locator("#toast").inner_text()
            verified("live/recently-ended keep scope; triple disconnect sends once; failure and partial result shown")

            fixture.failure=True
            await tick()
            assert not await page.locator("#feedback").is_hidden()
            assert await page.locator("[data-close]:not(:disabled)").count()==0
            await page.screenshot(path=str(output/"02-stale-connections.png"))
            fixture.failure=False
            await tick()
            assert await page.locator("#feedback").is_hidden()
            verified("stale-data timestamp, disabled operations, automatic recovery")

            await nav(1)
            await settle()
            await filters()
            await page.locator("#periods button").nth(1).click()
            await settle()
            assert await page.evaluate("statsRows(last.rank[period]).reduce((n,r)=>n+statTotal(r),0)")==13100
            assert await page.locator("[data-fold]").count()==await page.locator('[data-rank-row]').count()
            await filters()
            await page.locator("#sort").select_option("direct")
            await page.wait_for_function("document.querySelector('thead .grow')?.textContent?.startsWith('程序') && document.querySelectorAll('tbody tr').length===2")
            assert await page.locator("tbody tr").count()==2
            await page.locator("#rank-modes button").nth(1).click()
            await page.wait_for_function("document.querySelector('thead .grow')?.textContent?.startsWith('网站') && document.querySelectorAll('tbody tr').length===2")
            assert await page.locator("tbody tr").count()==2
            await page.locator("#rank-modes button").nth(2).click()
            await page.wait_for_function("document.querySelector('thead .grow')?.textContent==='节点' && document.querySelector('[data-rank-sort=proxy]')===null")
            assert await page.locator("#sort").is_hidden()
            assert "经过代理节点" in await page.locator("#main").inner_text()
            await page.locator("#rank-modes button").first.click()
            await page.wait_for_function("document.querySelector('thead .grow')?.textContent?.startsWith('程序') && document.querySelector('[data-rank-sort=proxy]')!==null")
            await page.locator("#sort").select_option("all")
            await page.locator("#periods button").first.click()
            await settle()
            assert await page.evaluate("statsRows(last.rank[period]).reduce((n,r)=>n+statTotal(r),0)")==3310
            await page.locator("#periods button").last.click()
            await settle()
            await page.locator("[data-fold]").first.click()
            await page.wait_for_selector("tr.site")
            async with page.expect_download() as download:
                await page.locator("#btn-export").click()
            artifact=await download.value
            await artifact.save_as(str(output/"statistics.csv"))
            assert artifact.suggested_filename == "FlashMonitor-program-all-all.csv"
            exported=(output/"statistics.csv").read_text(encoding="utf-8-sig")
            assert '"Edge","100000","30000","0","0","130000"' in exported
            await page.screenshot(path=str(output/"03-statistics.png"))
            verified("program/site/node dimensions, direct filter, period totals, lifetime-only detail and current export")

            await page.locator("#btn-settings").click()
            await settle()
            await page.locator("[data-pane=connect]").click()
            await page.get_by_role("button",name="测试连接",exact=True).click(click_count=3)
            await page.wait_for_timeout(600)
            assert fixture.test_calls==1
            fixture.save_failure=True
            await page.locator("input[aria-label='Clash 密钥']").fill("DEMO_NOT_A_SECRET")
            await page.keyboard.press("Tab")
            await page.wait_for_timeout(100)
            assert await page.locator("input[aria-label='Clash 密钥']").input_value()=="DEMO_NOT_A_SECRET"
            fixture.save_failure=False
            verified("triple connection test sends once; failed secret save preserves new input")
            await page.wait_for_timeout(3300)

            await nav(0)
            await settle()
            await page.locator("#view-menu summary").click()
            await page.locator("#view-menu [data-live-style=balls]").click()
            await page.wait_for_selector("#well-canvas")
            await page.wait_for_timeout(600)
            await page.get_by_role("button",name="返回列表",exact=True).click()
            await page.locator("#context-tools [data-live-style=balls]").click()
            await page.keyboard.press("Escape")
            await page.wait_for_selector("[data-program=msedge]")
            await page.keyboard.press("Escape")
            await page.wait_for_selector("#well-side",state="detached")
            for width,height in [(960,640),(768,512)]:
                await page.set_viewport_size(dict(width=width,height=height))
                assert await page.locator("#btn-search").is_visible()
                await page.keyboard.press("/")
                assert await page.locator("#q").is_visible()
                await page.keyboard.press("Escape")
                assert await page.evaluate("document.querySelector('.caption').scrollWidth<=innerWidth")
                await page.screenshot(path=str(output/f"04-live-{width}.png"))
            await page.keyboard.press("Escape")
            await page.wait_for_selector("#well-side",state="detached")
            await page.locator("[data-program=msedge]").click()
            await page.wait_for_selector("#well-side")
            await page.locator("[data-program=msedge]").click()
            await page.wait_for_selector("#well-side",state="detached")
            await page.locator("[data-program=msedge]").focus()
            await page.keyboard.press("Enter")
            await page.wait_for_function("well.sel==='msedge'")
            await page.screenshot(path=str(output/"07-inline-detail-768.png"))
            await page.locator("[data-connections=msedge]").click()
            await settle()
            assert await page.evaluate("document.querySelector('.body').scrollWidth<=innerWidth")
            assert await page.locator("td.grow .host").first.evaluate("e=>e.clientWidth>40")
            await page.screenshot(path=str(output/"05-connections-768.png"))
            await nav(1)
            await settle()
            assert await page.evaluate("document.querySelector('.body').scrollWidth<=innerWidth")
            await page.screenshot(path=str(output/"06-statistics-768.png"))
            verified("optional balls/list switch; narrow-window search; keyboard program selection")

            fixture.failure=True
            await page.goto("http://127.0.0.1:18799/?desk=1")
            await page.wait_for_selector("#feedback:not([hidden])")
            assert "暂时无法读取" in await page.locator("#live-programs").inner_text()
            assert not errors,errors
            verified("initial backend failure and no uncaught page errors")
        finally:
            await page.screenshot(path=str(output/"last-state.png"))
            (output/"report.json").write_text(json.dumps(dict(passed=results,page_errors=errors,
                 disconnect_requests=fixture.close_calls,test_requests=fixture.test_calls),ensure_ascii=False,indent=2),encoding="utf-8")
            await browser.close()
            fixture.rule_tmp.cleanup()


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=Path(tempfile.gettempdir())/"flashmonitor-ui-check")
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    asyncio.run(check(args.output))


if __name__=="__main__":
    main()
