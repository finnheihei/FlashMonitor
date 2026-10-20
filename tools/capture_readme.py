"""Capture the unchanged production UI with synthetic, fully intercepted API data."""
import argparse, asyncio, copy, time
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from playwright.async_api import async_playwright
ROOT=Path(__file__).resolve().parents[1]

class Fixture:
    def __init__(self):
        self.settings=dict(version="0.9.13",last_period="today",ignored=[],pinned=[],show_airport=True,close_to_tray=True,license="MIT")
    def snapshot(self, view: str, period: str) -> dict:
        programs = [("msedge", "Edge", 1800000, 320000), ("steam", "Steam", 0, 8200000), ("Code", "Visual Studio Code", 450000, 0), ("Discord", "Discord", 180000, 0), ("OneDrive", "OneDrive", 0, 120000)]
        factor = {"today": 800, "all": 12000, "1h": 120, "7d": 4200, "30d": 8000}[period]
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
                   sub={},live=dict(eaters=eaters,down=11070000,up=0,proxy_down=2430000,direct_down=8640000))
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


    async def route(self, route):
        u=urlparse(route.request.url)
        if u.hostname!="127.0.0.1" or u.port!=18799:
            return await route.abort()
        if u.path=="/":
            return await route.fulfill(content_type="text/html; charset=utf-8",body=(ROOT/"ui.html").read_text(encoding="utf-8"))
        data={}
        if u.path=="/api/snapshot":
            q=parse_qs(u.query)
            data=self.snapshot(q.get("view",["overview"])[0],q.get("period",["today"])[0])
            data["status"]["version"]="0.9.13"
            for f in data.get("flows",[]):
                f["proc_path"]="C:/Program Files/Demo/"+f["process"]+".exe"
            pack=data.get("rank",{}).get(data["period"])
            if pack and "sites" in pack:
                pack["sites"]["program_details"]={}
                for row in pack["rows"]:
                    for site in row["sites"]:
                        key=site["site"]+":443"
                        pack["sites"]["program_details"][key]=dict(rows=[{**site,"process":row["process"],"title":row["title"]}])
        elif u.path=="/api/settings": data=self.settings
        elif u.path=="/api/update": data=dict(current="0.9.13",latest="0.9.13",newer=False)
        elif u.path=="/api/scoped-ipv6":
            data=dict(supported=True,revision="demo",entries=[],applications=[dict(process="msedge",title="Edge",label="Edge",path=r"C:\Program Files\Demo\msedge.exe")])
        elif u.path=="/api/close": data=dict(closed=len(route.request.post_data_json.get("ids",[])))
        else:
            return await route.fulfill(status=404,json={})
        await route.fulfill(json=data)

async def main(output,channel):
    output.mkdir(parents=True,exist_ok=True)
    errors=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch(**({"channel":channel} if channel else {}),headless=True)
        context=await browser.new_context(viewport=dict(width=1280,height=820),device_scale_factor=1,locale="zh-CN",service_workers="block",reduced_motion="reduce")
        await context.route("**/*",Fixture().route)
        page=await context.new_page()
        page.on("pageerror",lambda e:errors.append(str(e)))
        async def shot(name):
            await page.wait_for_function("!poll.busy")
            await page.wait_for_timeout(350)
            await page.screenshot(path=str(output/name))
        await page.goto("http://127.0.0.1:18799/?desk=1")
        await page.wait_for_selector("[data-program=msedge]")
        await shot("01-live.png")
        await page.locator("[data-program=msedge]").click()
        await shot("02-program.png")
        await page.evaluate("openConnectionForProcess('msedge',document.getElementById('btn-settings'))")
        await page.wait_for_function("!connectionLoading && scopedIPv6Catalog")
        await shot("02-ipv6.png")
        await page.locator("#connection-close").click()
        await page.locator("[data-connections=msedge]").click()
        await shot("03-connections.png")
        await page.locator("#filters button").nth(1).click()
        await shot("04-usage.png")
        await page.locator("#rank-modes button").nth(1).click()
        await page.wait_for_function("!poll.busy")
        await page.locator("[data-fold]").first.click()
        await shot("05-sites.png")
        await page.locator("#filters button").nth(0).click()
        await page.locator("#view-menu summary").click()
        await page.locator("#view-menu [data-live-style=balls]").click()
        await page.wait_for_selector("#well-canvas")
        await page.wait_for_timeout(1500)
        await shot("06-balls.png")
        assert not errors,errors
        await browser.close()
    print("Captured 7 real UI images with demo data; no real backend started.")

if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--output",type=Path,default=ROOT/"docs/media/screenshots")
    parser.add_argument("--channel",default="")
    args=parser.parse_args()
    asyncio.run(main(args.output,args.channel))
