"""Real core DNS preservation, with localhost DNS/traffic only."""
import argparse
import json
import socket
import socketserver
import struct
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from check_ipv6_core import Echo, IsolatedCore, probe
from flashmonitor.clash import ClashClient
from flashmonitor.rule_drafts import DraftError
from flashmonitor.scoped_ipv6 import GENERAL_KEYS, RuntimeController, ScopedIPv6


def check(binary, output):
    output = output.resolve(); output.mkdir(parents=True, exist_ok=True)
    home = output / 'home'; home.mkdir(exist_ok=True)
    class DNS(socketserver.BaseRequestHandler):
        def handle(self):
            packet, stream = self.request
            # Echo the question and supply a local deterministic A record.
            if len(packet) < 12:
                return
            response = packet[:2] + b'\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00' + packet[12:]
            response += b'\xc0\x0c\x00\x01\x00\x01' + struct.pack('>I', 300) + b'\x00\x04' + socket.inet_aton('203.0.113.44')
            stream.sendto(response, self.client_address)
    dns_server = socketserver.ThreadingUDPServer(('127.0.0.1', 0), DNS)
    worker = threading.Thread(target=dns_server.serve_forever, daemon=True); worker.start()
    core = IsolatedCore(binary.resolve(), home)
    v4, v6 = Echo(socket.AF_INET), Echo(socket.AF_INET6)
    passed = []
    try:
        with socket.socket() as free:
            free.bind(('127.0.0.1', 0)); port = free.getsockname()[1]
        core.setup(['MATCH,DIRECT'], port, True); core.call('startListener')
        base = f'http://127.0.0.1:{core.controller}'
        runtime = RuntimeController(ClashClient(lambda: base, lambda: '', fallbacks=()))
        cfg = json.loads((home / 'config.yaml').read_text(encoding='utf-8'))
        for key, value in runtime.snapshot(base)['general'].items():
            if key in GENERAL_KEYS and key not in cfg: cfg[key] = value
        upstream = f'udp://127.0.0.1:{dns_server.server_address[1]}'
        cfg['dns'] = {'enable': True, 'ipv6': False, 'listen': '127.0.0.1:0', 'nameserver': [upstream],
                      'nameserver-policy': {'+.chatgpt.com': upstream}}
        original = json.dumps(cfg)
        (home / 'config.yaml').write_text(original, encoding='utf-8'); runtime.reload(base, original)
        prefs = {'currentProfileId': 123, 'overrideDns': True, 'patchClashConfig': {'dns': cfg['dns']}}
        (home / 'shared_preferences.json').write_text(json.dumps({'flutter.config': json.dumps(prefs)}), encoding='utf-8')
        service = ScopedIPv6(home, output / 'state.json', runtime,
                            inspect=lambda: {'available': True, 'schema_verified': True, 'profile_id': '123'})
        def answers():
            return {r['data'] for r in runtime.request(base, '/dns/query?name=chatgpt.com&type=A').get('Answer', [])}
        def action(operation='enable', **fields):
            return service.operate({'operation': operation, 'revision': service._read()['revision'], **fields})
        assert answers() == {'203.0.113.44'}
        assert probe(port, ('::1', v6.port))
        entry = action(scope={'kind': 'process_path', 'value': sys.executable})['entries'][0]
        assert entry['applied'] and answers() == {'203.0.113.44'}
        assert not probe(port, ('::1', v6.port)) and probe(port, ('127.0.0.1', v4.port))
        passed.append('real enable preserves explicit DNS policy and IPv4 while rejecting selected IPv6')
        action('disable', id=entry['id'])
        assert answers() == {'203.0.113.44'} and probe(port, ('::1', v6.port))
        assert (home / 'config.yaml').read_text(encoding='utf-8') == original
        passed.append('real restore preserves DNS and original generated configuration')
        stale = {**cfg, 'dns': {**cfg['dns'], 'nameserver-policy': {}}}
        (home / 'config.yaml').write_text(json.dumps(stale), encoding='utf-8')
        try:
            action(scope={'kind': 'domain', 'value': 'selected.example'})
            raise AssertionError('stale DNS allowed')
        except DraftError as exc:
            assert 'DNS 设置尚未同步' in str(exc)
        assert runtime.snapshot(base)['rules'] == [('match', '', 'DIRECT')]
        assert answers() == {'203.0.113.44'}
        passed.append('stale DNS file is refused before PUT; running resolver and routes unchanged')
        for item in passed: print('PASS ' + item, flush=True)
    finally:
        core.close(); v4.close(); v6.close()
        dns_server.shutdown(); dns_server.server_close(); worker.join(3)
        (output / 'report.json').write_text(json.dumps({'passed': passed, 'scope': 'private core + localhost DNS/IPv4/IPv6 only'}, indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--core', type=Path, required=True); parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(); check(args.core, args.output)
