"""Isolated, deterministic UI preview. No controller or real user data is accessed.

Run with: python tools/preview_ui.py --port 18799
The browser can select ?scenario=empty or ?scenario=offline on the preview URL.
"""
import argparse
import json
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from flashmonitor.store import TrafficStore


class Preview:
    def __init__(self, directory):
        self.store = TrafficStore(Path(directory), program_sites_enabled=True)
        self.settings = dict(version='1.0.0-preview', ignored=[], pinned=[], home_scope='all',
                             last_period='today', show_airport=False, license='MIT')
        self.scenario = ''
        self.flows = []
        samples = [('msedge', 'Microsoft Edge', 'video.example', 'proxy', 5.7),
                   ('msedge', 'Microsoft Edge', 'docs.example', 'direct', .8),
                   ('steam', 'Steam', 'cdn.example', 'proxy', 3.2),
                   ('steam', 'Steam', 'download.example', 'direct', 10.1),
                   ('Code', 'Visual Studio Code', 'api.example', 'proxy', .83),
                   ('Code', 'Visual Studio Code', 'docs.example', 'proxy', .12),
                   ('cloud', 'Cloud Drive', 'storage.example', 'direct', 2.3),
                   ('worker', 'Background Worker', '[2001:db8::1]', 'unknown', .04)]
        self.store.update([])
        for index, (process, title, host, lane, gb) in enumerate(samples):
            self.flows.append(dict(id=str(index), ids=[str(index)], process=process, title=title,
                                   host=host.strip('[]'), remote=host+':443', path=lane, outbound=lane,
                                   download=int(gb*1024**3), upload=int(gb*1024**2), n=1,
                                   node='Tokyo' if lane=='proxy' else '', note='示例规则',
                                   rule_exe=process+'.exe'))
        self.store.update(self.flows)
        self.store.flush()

    def snapshot(self, view, period):
        flows=[] if self.scenario=='empty' else self.flows
        eaters=[]
        for process in dict.fromkeys(f['process'] for f in flows):
            own=[f for f in flows if f['process']==process]
            proxy=sum(f['download'] for f in own if f['outbound']=='proxy')/10000
            direct=sum(f['download'] for f in own if f['outbound']=='direct')/10000
            eaters.append(dict(process=process,title=own[0]['title'],down=proxy+direct,up=proxy/30,
                               proxy_down=proxy,proxy_up=proxy/30,direct_down=direct,direct_up=0,conns=len(own)))
        pack=self.store.ranking(period)
        if self.scenario=='empty':
            pack=dict(rows=[],sites=dict(rows=[]),nodes=dict(rows=[]))
        live={k:sum(r.get(k,0) for r in eaters) for k in ['down','up','proxy_down','proxy_up','direct_down','direct_up']}
        return dict(view=view,ts=time.time(),status=dict(clash_api=True,tun_up=True,mode='tun',version='1.0.0-preview'),
                    prefs=self.settings,alerts=[],sub={},flows=flows,closed=[],live={**live,'eaters':eaters},
                    rank={period:pack},manual_refresh_supported=False,history=[])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=18799)
    args=parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        preview=Preview(directory)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def send(self, data, status=200, content_type='application/json; charset=utf-8'):
                payload=data.encode('utf-8') if isinstance(data,str) else json.dumps(data,ensure_ascii=False).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type',content_type)
                self.send_header('Content-Length',str(len(payload)))
                self.send_header('Cache-Control','no-store')
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                url=urlsplit(self.path)
                query=parse_qs(url.query)
                if url.path=='/':
                    preview.scenario=query.get('scenario',[''])[0]
                    return self.send((ROOT/'ui.html').read_text(encoding='utf-8'),content_type='text/html; charset=utf-8')
                if url.path=='/api/settings':
                    return self.send(preview.settings)
                if url.path=='/api/snapshot':
                    if preview.scenario=='offline':
                        return self.send(dict(error='预览：后台暂时不可用'),503)
                    return self.send(preview.snapshot(query.get('view',['overview'])[0],query.get('period',['today'])[0]))
                if url.path=='/api/update':
                    return self.send(dict(newer=False))
                return self.send(dict(error='Preview endpoint unavailable'),404)

            def do_POST(self):
                if self.path!='/api/settings':
                    return self.send(dict(error='Preview does not change network settings'),405)
                body=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
                preview.settings.update({k:v for k,v in body.items() if k in {'home_scope','last_period'}})
                return self.send(preview.settings)

        server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
        print(f'Isolated preview: http://127.0.0.1:{args.port}',flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()
            preview.store.close()


if __name__=='__main__':
    main()
