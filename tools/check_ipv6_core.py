"""Optional existing FlClashCore integration, isolated named pipe/home and loopback only."""
import argparse
import ctypes
import json
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from ctypes import wintypes as W
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flashmonitor.ipv6_plans import reject_rule


class IsolatedCore:
    def __init__(self,binary,home):
        self.home=home;self.process=None;self.events=[];self.k=ctypes.WinDLL('kernel32',use_last_error=True)
        with socket.socket() as free:free.bind(('127.0.0.1',0));self.controller=free.getsockname()[1]
        self.k.CreateNamedPipeW.argtypes=[W.LPCWSTR,W.DWORD,W.DWORD,W.DWORD,W.DWORD,W.DWORD,W.DWORD,ctypes.c_void_p]
        self.k.CreateNamedPipeW.restype=W.HANDLE
        for name in ['ConnectNamedPipe','DisconnectNamedPipe','CloseHandle']:
            getattr(self.k,name).argtypes=[W.HANDLE]+([ctypes.c_void_p] if name=='ConnectNamedPipe' else [])
        self.k.ReadFile.argtypes=[W.HANDLE,ctypes.c_void_p,W.DWORD,ctypes.POINTER(W.DWORD),ctypes.c_void_p]
        self.k.WriteFile.argtypes=self.k.ReadFile.argtypes
        address=r'\\.\pipe\FlashMonitor-test-'+uuid.uuid4().hex
        self.pipe=self.k.CreateNamedPipeW(address,3|0x80000,8,1,65536,65536,1000,None)
        if self.pipe in (None,ctypes.c_void_p(-1).value):raise ctypes.WinError(ctypes.get_last_error())
        event=threading.Event();connected=[]
        def accept():connected.append(bool(self.k.ConnectNamedPipe(self.pipe,None)) or ctypes.get_last_error()==535);event.set()
        thread=threading.Thread(target=accept,daemon=True);thread.start()
        self.process=subprocess.Popen([str(binary),address],cwd=home,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
        if not event.wait(5) or not connected[0]:self.close();raise RuntimeError('isolated named-pipe connection failed')
        self.call('initClash',{'home-dir':str(home),'version':0})
    def read(self,count):
        out=b''
        while len(out)<count:
            buf=ctypes.create_string_buffer(count-len(out));done=W.DWORD()
            if not self.k.ReadFile(self.pipe,buf,len(buf),ctypes.byref(done),None) or not done.value:raise OSError('isolated IPC read failed')
            out+=buf.raw[:done.value]
        return out
    def call(self,method,args=None):
        request_id=uuid.uuid4().hex
        data=json.dumps({'id':request_id,'method':method,'arguments':args}).encode();buf=struct.pack('<I',len(data))+data;done=W.DWORD()
        if not self.k.WriteFile(self.pipe,buf,len(buf),ctypes.byref(done),None):raise ctypes.WinError(ctypes.get_last_error())
        while True:
            length=struct.unpack('<I',self.read(4))[0]
            if length>1024*1024:raise RuntimeError('unexpected integration response size')
            result=json.loads(self.read(length))
            if result.get('id')==request_id:break
            self.events.append(result)
        if result.get('error'):raise RuntimeError(str(result['error']))
        return result.get('result')
    def setup(self,rules,port=0,sniff=False,remote=None):
        cfg={'mixed-port':port,'port':0,'socks-port':0,'redir-port':0,'tproxy-port':0,'allow-lan':False,
             'bind-address':'127.0.0.1','mode':'rule','ipv6':True,'find-process-mode':'always','geo-auto-update':False,'log-level':'debug',
             'dns':{'enable':False},'tun':{'enable':False,'auto-route':False},'rules':rules,
             'proxies':[],'proxy-groups':[],'proxy-providers':{},'rule-providers':{},'external-controller':f'127.0.0.1:{self.controller}',
             'sniffer':{'enable':sniff,'override-destination':False,'sniff':{'HTTP':{'ports':['1-65535'],'override-destination':False}}}}
        if remote:cfg['proxies']=[{'name':'OriginalProxy','type':'http','server':'127.0.0.1','port':remote}]
        (self.home/'config.yaml').write_text(json.dumps(cfg),encoding='utf-8')
        result=self.call('setupConfig',{})
        if result:raise RuntimeError(result)
        # The compatible rule provider publishes asynchronously after setup returns.
        # Verify the live rule payload before sending a new connection.
        op=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        from flashmonitor.ipv6_native import canonical_rule, canonical_runtime
        expected=[canonical_rule(rule) for rule in rules if rule.startswith('AND,')]
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            try:
                with op.open(f'http://127.0.0.1:{self.controller}/rules',timeout=.3) as resp:live=json.load(resp)['rules']
                payloads=[canonical_runtime(r) for r in live]
                if len(live)==len(rules) and all(value in payloads for value in expected):return
            except (OSError,ValueError,KeyError):pass
            time.sleep(.02)
        raise RuntimeError('new rule provider not published before test deadline')
    def close(self):
        if getattr(self,'pipe',None):
            self.k.DisconnectNamedPipe(self.pipe);self.k.CloseHandle(self.pipe);self.pipe=None
        if self.process:
            try:self.process.wait(5)
            except subprocess.TimeoutExpired:self.process.terminate();self.process.wait(5)


class Echo:
    def __init__(self,family):
        self.sock=socket.socket(family);self.sock.bind(('::1' if family==socket.AF_INET6 else '127.0.0.1',0));self.sock.listen();self.sock.settimeout(.1)
        self.port=self.sock.getsockname()[1];self.stop=threading.Event();self.accepted=0
        def run():
            while not self.stop.is_set():
                try:c,_=self.sock.accept()
                except socket.timeout:continue
                except OSError:return
                self.accepted+=1
                with c:
                    c.settimeout(2)
                    try:c.sendall(c.recv(4096))
                    except OSError:pass
        self.thread=threading.Thread(target=run,daemon=True);self.thread.start()
    def close(self):self.stop.set();self.sock.close();self.thread.join(2)


def probe(proxy_port,target,domain=None,remote_name=None):
    with socket.create_connection(('127.0.0.1',proxy_port),timeout=3) as sock:
        sock.settimeout(3);sock.sendall(b'\x05\x01\x00');assert sock.recv(2)==b'\x05\x00'
        family=socket.AF_INET6 if ':' in target[0] else socket.AF_INET
        addr=(b'\x03'+bytes([len(remote_name)])+remote_name.encode()) if remote_name else (b'\x04'+socket.inet_pton(family,target[0])) if family==socket.AF_INET6 else (b'\x01'+socket.inet_aton(target[0]))
        sock.sendall(b'\x05\x01\x00'+addr+struct.pack('>H',target[1]))
        reply=sock.recv(64)
        if len(reply)<2 or reply[1]!=0:return False
        data=(f'GET / HTTP/1.1\r\nHost: {domain}\r\n\r\n'.encode() if domain else b'probe')
        sock.sendall(data)
        try:return sock.recv(4096)==data
        except OSError:return False


def check(binary,output):
    output.mkdir(parents=True,exist_ok=True);home=output/'isolated-home';home.mkdir(exist_ok=True)
    results=[];core=IsolatedCore(binary,home);core.events=[];v4=Echo(socket.AF_INET);v6=Echo(socket.AF_INET6)
    def passed(name):results.append(name);print('PASS '+name,flush=True)
    try:
        for s in [{'kind':'process_path','value':str(Path(sys.executable)),'include_subdomains':False},
                  {'kind':'process_path','value':r'C:\Program Files (x86)\demo.exe','include_subdomains':False},
                  {'kind':'domain','value':'selected.example','include_subdomains':False},
                  {'kind':'domain','value':'selected.example','include_subdomains':True}]:
            core.setup([reject_rule(s),'MATCH,DIRECT'])
        passed('installed core parses AND + process-path/domain/suffix + no-resolve IPv6 CIDR, including balanced path parentheses')
        with socket.socket() as free:free.bind(('127.0.0.1',0));port=free.getsockname()[1]
        core.setup(['MATCH,DIRECT'],port);core.call('startListener')
        core.call('startLog')
        assert probe(port,('::1',v6.port));assert probe(port,('127.0.0.1',v4.port))
        passed('isolated core baseline reaches IPv4 and IPv6 loopback; no TUN/DNS/system changes')
        rule=reject_rule({'kind':'process_path','value':str(Path(sys.executable)),'include_subdomains':False})
        core.setup([rule,'MATCH,DIRECT'],port)
        before=v6.accepted
        blocked=not probe(port,('::1',v6.port));core.call('getConnections')
        if not blocked:print('PROCESS_MATCH_DIAGNOSTIC '+json.dumps(core.events[-10:]),flush=True)
        assert blocked;assert v6.accepted==before
        assert probe(port,('127.0.0.1',v4.port))
        passed('selected actual Python process IPv6 target rejected, IPv4 retains original DIRECT route')
        core.setup([reject_rule({'kind':'process_name','value':'not-this-process.exe','include_subdomains':False}),'MATCH,DIRECT'],port)
        assert probe(port,('::1',v6.port))
        passed('unmatched process IPv6 remains on original route; no global IPv6 block')
        core.setup([reject_rule({'kind':'domain','value':'selected.example','include_subdomains':False}),'MATCH,DIRECT'],port,True)
        before=v6.accepted
        assert not probe(port,('::1',v6.port),'selected.example');assert v6.accepted==before
        assert probe(port,('::1',v6.port),'other.example')
        passed('sniffed selected domain with known IPv6 rejected; other domain still reaches IPv6')
        core.setup(['MATCH,DIRECT'],port)
        assert probe(port,('::1',v6.port))
        passed('removing only scoped rejection restores original IPv6 route')
        # Mock a proxy that accepts CONNECT and echoes payload; it never consults external DNS.
        remote=socket.socket();remote.bind(('127.0.0.1',0));remote.listen();remote.settimeout(3)
        calls=[]
        def serve_remote():
            for _ in range(2):
                c,_=remote.accept()
                with c:
                    c.settimeout(3);header=b''
                    while b'\r\n\r\n' not in header:header+=c.recv(4096)
                    calls.append(header.decode().split('\r\n')[0]);c.sendall(b'HTTP/1.1 200 OK\r\n\r\n')
                    payload=c.recv(4096);c.sendall(payload)
        worker=threading.Thread(target=serve_remote,daemon=True);worker.start()
        try:
            domain_rule=reject_rule({'kind':'domain','value':'selected.example','include_subdomains':False})
            core.setup([domain_rule,'DOMAIN,selected.example,OriginalProxy','MATCH,DIRECT'],port,remote=remote.getsockname()[1])
            assert probe(port,('127.0.0.1',443),remote_name='selected.example')
            core.setup([rule,'DOMAIN,selected.example,OriginalProxy','MATCH,DIRECT'],port,remote=remote.getsockname()[1])
            assert probe(port,('127.0.0.1',443),remote_name='selected.example')
            worker.join(4);assert len(calls)==2 and all('selected.example:443' in c for c in calls)
            passed('unresolved selected domain/process keeps original proxy with domain target; remote DNS address family is outside scoped IP rejection')
        finally:remote.close();worker.join(4)
    finally:
        core.close();v4.close();v6.close()
        (output/'report.json').write_text(json.dumps({'passed':results,'events':core.events,'network_scope':'isolated localhost only, no TUN/system proxy/DNS or real config changes'},indent=2),encoding='utf-8')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--binary',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();check(args.binary,args.output)
