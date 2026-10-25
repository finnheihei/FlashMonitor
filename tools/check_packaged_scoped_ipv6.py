"""Packaged Monitor + private core + real Edge clicks, all traffic localhost-only."""
import argparse
import asyncio
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from check_ipv6_core import Echo, IsolatedCore, probe
from connection_test_helpers import open_connection
from test_ipv6_plans import fixture
from flashmonitor.clash import ClashClient
from flashmonitor.scoped_ipv6 import GENERAL_KEYS, RuntimeController
from playwright.async_api import async_playwright


async def check(executable, binary, output):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    run = output / ('isolated-' + uuid.uuid4().hex[:8])
    roaming = run / 'roaming'
    home = roaming / 'com.follow' / 'clash'
    home.mkdir(parents=True)
    fixture(home)
    with sqlite3.connect(home / 'database.sqlite') as db:
        db.execute('DELETE FROM profile_rule_mapping')
        db.execute('DELETE FROM rules')
    data = run / 'monitor-data'
    data.mkdir()
    core = IsolatedCore(binary.resolve(), home)
    v4, v6 = Echo(socket.AF_INET), Echo(socket.AF_INET6)
    process = None
    passed, errors = [], []

    def free_port():
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            return sock.getsockname()[1]

    def ok(message):
        passed.append(message)
        print('PASS ' + message, flush=True)

    try:
        port = free_port()
        core.setup(['MATCH,DIRECT'], port, True)
        core.call('startListener')
        control_base = f'http://127.0.0.1:{core.controller}'
        client = ClashClient(lambda: control_base, lambda: '', fallbacks=())
        controller = RuntimeController(client)
        cfg = json.loads((home / 'config.yaml').read_text(encoding='utf-8'))
        general = controller.snapshot(control_base)['general']
        for key in GENERAL_KEYS:
            if key in general and key not in cfg:
                cfg[key] = general[key]
        original = json.dumps(cfg).encode()
        (home / 'config.yaml').write_bytes(original)
        prefs_path = home / 'shared_preferences.json'
        prefs = json.loads(prefs_path.read_text(encoding='utf-8'))
        config = json.loads(prefs['flutter.config'])
        config['overrideDns'] = True
        config.setdefault('patchClashConfig', {})['dns'] = cfg['dns']
        prefs['flutter.config'] = json.dumps(config)
        prefs_path.write_text(json.dumps(prefs), encoding='utf-8')
        old_draft = {'version': 1, 'kind': 'application', 'applications': [r'C:\Missing\old1.exe', r'C:\Missing\old2.exe'],
                     'domains': [], 'target': 'missing-group', 'block_ipv6': True, 'block_quic': False}
        (data / 'settings.json').write_text(json.dumps({'clash_api': control_base, 'connection_draft': old_draft}), encoding='utf-8')
        monitor_port = free_port()
        env = {**os.environ, 'APPDATA': str(roaming), 'FLASHMONITOR_DATA_DIR': str(data)}
        probe_home = run / 'Program Files (x86)' / 'IPv6Probe'
        probe_home.mkdir(parents=True)
        probe_exe = probe_home / 'python.exe'
        shutil.copy2(Path(sys.base_prefix) / 'python.exe', probe_exe)
        for dll in Path(sys.base_prefix).glob('*.dll'):
            shutil.copy2(dll, probe_home / dll.name)

        def application_probe(host, target_port):
            code = ('import json,sys;sys.path.insert(0,sys.argv[1]);'
                    'from check_ipv6_core import probe;'
                    'print(json.dumps(probe(int(sys.argv[2]),(sys.argv[3],int(sys.argv[4])))))')
            result = subprocess.run([str(probe_exe), '-c', code, str(ROOT / 'tools'), str(port), host, str(target_port)],
                env={**env, 'PYTHONHOME': sys.base_prefix}, capture_output=True, text=True, timeout=10,
                check=True, creationflags=subprocess.CREATE_NO_WINDOW)
            return json.loads(result.stdout)

        process = subprocess.Popen([str(executable.resolve()), '--no-gui', '--no-browser', '--port', str(monitor_port)],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        base = f'http://127.0.0.1:{monitor_port}'
        opener = build_opener(ProxyHandler({}))
        deadline = time.monotonic() + 25
        while True:
            try:
                with opener.open(base, timeout=.5) as response:
                    assert b'connection-picker' in response.read()
                break
            except OSError:
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError('isolated packaged Monitor did not start')
                await asyncio.sleep(.2)
        assert probe(port, ('::1', v6.port), 'selected.example')
        assert probe(port, ('127.0.0.1', v4.port), 'selected.example')
        async with async_playwright() as p:
            browser = await p.chromium.launch(channel='msedge', headless=True, args=['--disable-background-networking'])
            context = await browser.new_context(viewport={'width': 900, 'height': 680}, service_workers='block')

            async def guard(route):
                if not route.request.url.startswith(base + '/'):
                    await route.abort()
                else:
                    await route.continue_()

            await context.route('**/*', guard)
            page = await context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.goto(base + '/?desk=1')
            await open_connection(page)
            await page.wait_for_function('!connectionLoading')
            assert await page.locator('#scoped-ipv6-enable').is_enabled()
            assert await page.locator('#connection-confirm, #gpt-dialog').count() == 0
            await page.locator('#connection-kind').select_option('domain')
            await page.locator('#connection-domain').fill('selected.example')
            await page.locator('#scoped-ipv6-enable').click()
            await page.wait_for_function('!scopedIPv6Busy')
            assert await page.locator('#scoped-ipv6-state').inner_text() == '已开启'
            assert not probe(port, ('::1', v6.port), 'selected.example')
            assert probe(port, ('127.0.0.1', v4.port), 'selected.example')
            assert probe(port, ('::1', v6.port), 'other.example')
            await page.screenshot(path=str(output / 'packaged-enabled.png'))
            ok('packaged app real click reaches private core: selected IPv6 blocked; IPv4 and other site IPv6 retained')
            await page.locator('#scoped-ipv6-enable').click()
            await page.wait_for_function('!scopedIPv6Busy')
            assert await page.locator('#scoped-ipv6-state').inner_text() == '未开启'
            assert probe(port, ('::1', v6.port), 'selected.example')
            assert (home / 'config.yaml').read_bytes() == original
            assert json.loads((data / 'settings.json').read_text(encoding='utf-8'))['connection_draft'] == old_draft
            ok('packaged switch restores real IPv6; original source and old diagnostic draft are unchanged')

            await page.locator('#connection-close').click()
            await page.evaluate("scope=>openConnectionSettings(scope,document.getElementById('btn-settings'))",
                {'kind': 'domain', 'value': 'selected.example', 'include_subdomains': True})
            await page.wait_for_function('!connectionLoading')
            assert await page.locator('#connection-picker').is_hidden()
            assert await page.locator('#connection-subdomains').is_visible()
            assert await page.locator('#connection-subdomains').is_checked()
            assert probe(port, ('::1', v6.port), 'api.selected.example')
            await page.locator('#scoped-ipv6-enable').click()
            await page.wait_for_function('!scopedIPv6Busy')
            assert await page.locator('#scoped-ipv6-state').inner_text() == '已开启'
            assert not probe(port, ('::1', v6.port), 'selected.example')
            assert not probe(port, ('::1', v6.port), 'api.selected.example')
            assert probe(port, ('127.0.0.1', v4.port), 'api.selected.example')
            assert probe(port, ('::1', v6.port), 'other.example')
            assert probe(port, ('::1', v6.port), 'notselected.example')
            await page.screenshot(path=str(output / 'packaged-site-enabled.png'))
            ok('contextual site switch blocks apex and subdomain IPv6, preserving IPv4 and unrelated domains')
            await page.locator('#scoped-ipv6-enable').click()
            await page.wait_for_function('!scopedIPv6Busy')
            assert await page.locator('#scoped-ipv6-state').inner_text() == '未开启'
            assert probe(port, ('::1', v6.port), 'selected.example')
            assert probe(port, ('::1', v6.port), 'api.selected.example')
            ok('contextual site switch restores both apex and subdomain IPv6')

            await page.locator('#connection-subdomains').uncheck()
            await page.locator('#scoped-ipv6-enable').click()
            await page.wait_for_function('!scopedIPv6Busy')
            assert await page.locator('#scoped-ipv6-state').inner_text() == '已开启'
            assert not probe(port, ('::1', v6.port), 'selected.example')
            assert probe(port, ('::1', v6.port), 'api.selected.example')
            await page.locator('#scoped-ipv6-enable').click()
            await page.wait_for_function('!scopedIPv6Busy')
            assert probe(port, ('::1', v6.port), 'selected.example')
            ok('unchecking subdomains changes the actual core match to the exact domain and still restores')

            await page.locator('#connection-close').click()
            assert application_probe('::1', v6.port)
            await page.evaluate("scope=>openConnectionSettings(scope,document.getElementById('btn-settings'))",
                {'kind': 'process_path', 'value': str(probe_exe.resolve())})
            await page.wait_for_function('!connectionLoading')
            assert await page.locator('#connection-subdomains').is_hidden()
            await page.locator('#scoped-ipv6-enable').click()
            await page.wait_for_function('!scopedIPv6Busy')
            assert await page.locator('#scoped-ipv6-state').inner_text() == '已开启'
            assert not application_probe('::1', v6.port)
            assert application_probe('127.0.0.1', v4.port)
            assert probe(port, ('::1', v6.port))
            await page.screenshot(path=str(output / 'packaged-app-enabled.png'))
            await page.locator('#scoped-ipv6-enable').click()
            await page.wait_for_function('!scopedIPv6Busy')
            assert await page.locator('#scoped-ipv6-state').inner_text() == '未开启'
            assert application_probe('::1', v6.port)
            assert (home / 'config.yaml').read_bytes() == original
            assert json.loads((data / 'settings.json').read_text(encoding='utf-8'))['connection_draft'] == old_draft
            ok('actual process under Program Files (x86) blocks and restores IPv6 through packaged UI; IPv4 and other processes remain reachable')
            assert not errors, errors
            await browser.close()
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(5)
        core.close()
        v4.close()
        v6.close()
        (output / 'report.json').write_text(json.dumps({'passed': passed, 'page_errors': errors,
            'scope': 'isolated APPDATA + Monitor data + private core + localhost only; no daily-client changes'}, indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path, required=True)
    parser.add_argument('--core', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(check(args.exe, args.core, args.output))
