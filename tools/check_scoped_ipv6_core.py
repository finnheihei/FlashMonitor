"""Exercise the actual overlay service against an isolated installed FlClash core."""
import argparse
import json
import socket
import socketserver
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_ipv6_core import Echo, IsolatedCore, probe
from flashmonitor.clash import ClashClient
from flashmonitor.scoped_ipv6 import GENERAL_KEYS, RuntimeController, ScopedIPv6


def check(binary, output):
    output = output.resolve()
    binary = binary.resolve()
    output.mkdir(parents=True, exist_ok=True)
    home = output / 'home'
    home.mkdir(exist_ok=True)
    core = IsolatedCore(binary, home)
    v4, v6 = Echo(socket.AF_INET), Echo(socket.AF_INET6)
    passed = []

    def ok(message):
        passed.append(message)
        print('PASS ' + message, flush=True)

    try:
        with socket.socket() as free:
            free.bind(('127.0.0.1', 0))
            port = free.getsockname()[1]
        core.setup(['MATCH,DIRECT'], port, True)
        core.call('startListener')
        base = f'http://127.0.0.1:{core.controller}'
        client = ClashClient(lambda: base, lambda: '', fallbacks=())
        runtime = RuntimeController(client)
        cfg = json.loads((home / 'config.yaml').read_text(encoding='utf-8'))
        general = runtime.snapshot(base)['general']
        for key in GENERAL_KEYS:
            if key in general and key not in cfg:
                cfg[key] = general[key]
        original = json.dumps(cfg).encode()
        (home / 'config.yaml').write_bytes(original)
        service = ScopedIPv6(home, output / 'scoped-ipv6.json', RuntimeController(client),
                             inspect=lambda: {'available': True, 'profile_id': 'isolated', 'source_revision': 'test'})

        def action(operation='enable', **fields):
            return service.operate({'operation': operation, 'revision': service._read()['revision'], **fields})

        assert probe(port, ('::1', v6.port)) and probe(port, ('127.0.0.1', v4.port))
        app = {'kind': 'process_path', 'value': sys.executable}
        entry = action(scope=app)['entries'][0]
        assert entry['applied']
        assert not probe(port, ('::1', v6.port))
        assert probe(port, ('127.0.0.1', v4.port))
        ok('one-click service PUT verified: selected process IPv6 blocked; IPv4 still reaches original DIRECT route')
        action('disable', id=entry['id'])
        assert probe(port, ('::1', v6.port))
        ok('service one-click undo restores IPv6 with original config bytes unchanged')

        domain = {'kind': 'domain', 'value': 'selected.example', 'include_subdomains': False}
        entry = action(scope=domain)['entries'][0]
        assert not probe(port, ('::1', v6.port), 'selected.example')
        assert probe(port, ('::1', v6.port), 'other.example')
        assert probe(port, ('127.0.0.1', v4.port), 'selected.example')
        ok('exact site blocks selected IPv6 only; other site IPv6 and selected IPv4 remain reachable')
        action('disable', id=entry['id'])

        scope = {'kind': 'all', 'children': [app, {'kind': 'any', 'children': [domain, {**domain, 'value': 'suffix.example', 'include_subdomains': True}]}]}
        entry = action(scope=scope)['entries'][0]
        assert not probe(port, ('::1', v6.port), 'selected.example')
        assert not probe(port, ('::1', v6.port), 'sub.suffix.example')
        assert probe(port, ('::1', v6.port), 'other.example')
        assert probe(port, ('127.0.0.1', v4.port), 'selected.example')
        ok('nested browser AND domain OR/suffix scope verified against actual engine; full rule order receipt matches')

        core.setup(['MATCH,DIRECT'], port, True)
        (home / 'config.yaml').write_bytes(original)
        assert not service.catalog()['entries'][0]['applied']
        service.tick()
        assert service.catalog()['entries'][0]['applied']
        assert not probe(port, ('::1', v6.port), 'selected.example')
        ok('client-style reload reported inactive, then reconciliation really reloads and restores blocking')
        action('disable', id=entry['id'])
        assert probe(port, ('::1', v6.port), 'selected.example')
        assert (home / 'config.yaml').read_bytes() == original
        ok('final undo preserves generated source, all original rules and general runtime settings')

        calls = []

        class Proxy(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.settimeout(3)
                header = b''
                while b'\r\n\r\n' not in header:
                    chunk = self.request.recv(4096)
                    if not chunk:
                        return
                    header += chunk
                calls.append(header.split(b'\r\n')[0].decode())
                self.request.sendall(b'HTTP/1.1 200 OK\r\n\r\n')
                payload = self.request.recv(4096)
                self.request.sendall(payload)

        remote = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Proxy)
        worker = threading.Thread(target=remote.serve_forever, daemon=True)
        worker.start()
        try:
            cfg = json.loads(original)
            cfg['profile'] = {'store-selected': False}
            cfg['proxies'] = [{'name': 'OriginalProxy', 'type': 'http', 'server': '127.0.0.1', 'port': remote.server_address[1]}]
            cfg['proxy-groups'] = [{'name': 'OriginalRoute', 'type': 'select', 'proxies': ['DIRECT', 'OriginalProxy']}]
            cfg['rules'] = ['IP-CIDR,127.0.0.1/32,OriginalRoute,no-resolve', 'MATCH,DIRECT']
            text = '\n'.join(key + ':\n' + '\n'.join('- ' + json.dumps(r) for r in value) if key == 'rules'
                             else key + ': ' + json.dumps(value) for key, value in cfg.items()) + '\n'
            (home / 'config.yaml').write_text(text, encoding='utf-8')
            service.controller.reload(base, text)
            service.controller.request(base, '/proxies/OriginalRoute', {'name': 'OriginalProxy'})
            assert probe(port, ('127.0.0.1', v4.port)) and len(calls) == 1
            entry = action(scope=app)['entries'][0]
            assert entry['applied']
            assert not probe(port, ('::1', v6.port))
            assert probe(port, ('127.0.0.1', v4.port)) and len(calls) == 2
            assert service.controller.snapshot(base)['selections']['OriginalRoute']['current'] == 'OriginalProxy'
            action('disable', id=entry['id'])
            assert probe(port, ('127.0.0.1', v4.port)) and len(calls) == 3
            assert service.controller.snapshot(base)['selections']['OriginalRoute']['current'] == 'OriginalProxy'
            ok('actual original IPv4 PROXY traffic and nondefault selector choice survive both enable and undo')
        finally:
            remote.shutdown()
            remote.server_close()
            worker.join(3)
    finally:
        core.close()
        v4.close()
        v6.close()
        (output / 'report.json').write_text(json.dumps({'passed': passed, 'scope': 'private core; localhost only; no real client/TUN/DNS/system proxy changes'}, indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    check(args.binary, args.output)
