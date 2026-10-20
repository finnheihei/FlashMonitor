"""Monitor-owned IPv6 runtime overlays, with durable intent and verified recovery.

No FlClash DB/source writes. Full reloads require an exact generated/runtime
baseline; reconciliation never overwrites an unknown or partially changed core.
"""
from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .ipv6_native import canonical_rule, canonical_runtime, document_rules
from .ipv6_plans import digest, inspect_flclash
from .network_policy import condition, describe, leaves, normalize_scope
from .rule_drafts import DraftError
from .reload_guard import configuration_guard

LIMIT = 8 * 1024 * 1024
GENERAL_KEYS = ('port', 'socks-port', 'mixed-port', 'redir-port', 'tproxy-port', 'allow-lan',
                'bind-address', 'mode', 'log-level', 'ipv6', 'find-process-mode', 'interface-name',
                'tcp-concurrent', 'unified-delay', 'keep-alive-interval')
NOTICE = ('只屏蔽经过核心且已识别 IPv6 目标的新连接；IPv4 保留原分流。'
          '不保证自动回退 IPv4，无法控制代理远端解析或绕过核心的连接。'
          '设置由 Monitor 保存并在运行期间维护；客户端重载后可能短暂失去限制。'
          '应用和恢复会重新加载核心配置，可能短暂影响连接。')


def block_rule(scope):
    return f'AND,(({condition(scope)}),(IP-CIDR6,::/0,no-resolve)),REJECT'


def _expression(scope):
    if 'children' in scope:
        kind = 'OR' if scope['kind'] == 'any' else 'AND'
        sep = '||' if kind == 'OR' else '&&'
        return '(' + kind + ',(' + sep.join(_expression(c) for c in scope['children']) + '))'
    typ = 'ProcessPath' if scope['kind'] == 'process_path' else 'DomainSuffix' if scope['include_subdomains'] else 'Domain'
    return '(' + typ + ',' + scope['value'] + ')'


def managed_row(scope):
    return ('and', '(' + _expression(scope).casefold() + '&&(ipcidr,::/0))', 'REJECT')


def overlay(text, entries):
    """Insert only rule lines; preserve every original byte outside that insertion."""
    rules = [block_rule(e['scope']) for e in entries]
    if text.lstrip().startswith('{'):
        data = json.loads(text)
        data['rules'] = rules + document_rules(text)
        return json.dumps(data, ensure_ascii=False)
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip() == 'rules:']
    if len(starts) != 1:
        raise DraftError('生成配置的 rules 列表格式未知，未改动核心')
    newline = '\r\n' if '\r\n' in text else '\n'
    first = next((line for line in lines[starts[0] + 1:] if line.strip() and not line.lstrip().startswith('#')), '')
    match = re.match(r'^([ \t]*)- ', first)
    if not match:
        raise DraftError('生成配置的 rules 列表缩进格式未知，未改动核心')
    lines[starts[0] + 1:starts[0] + 1] = [match[1] + '- ' + json.dumps(r, ensure_ascii=False) + newline for r in rules]
    return ''.join(lines)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RuntimeController:
    def __init__(self, client):
        self.client = client
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())

    def endpoint(self):
        base = self.client.base_urls()[0]
        url = urlsplit(base)
        if (url.scheme != 'http' or url.hostname not in {'127.0.0.1', '::1'} or
                not url.port or url.username or url.password or url.path not in {'', '/'} or url.query or url.fragment):
            raise DraftError('IPv6 屏蔽只支持本机 IP 地址的控制器')
        return base.rstrip('/')

    def request(self, base, path, payload=None):
        if self.endpoint() != base:
            raise DraftError('控制器地址已变化，停止操作')
        req = Request(base + path, headers={**self.client.headers(), 'Content-Type': 'application/json'},
                      data=json.dumps(payload).encode() if payload is not None else None,
                      method='PUT' if payload is not None else 'GET')
        try:
            with self.opener.open(req, timeout=5) as response:
                data = response.read(LIMIT + 1)
        except HTTPError as exc:
            exc.close()
            raise DraftError('核心拒绝加载（HTTP ' + str(exc.code) + '），请检查客户端版本和控制器权限') from None
        if len(data) > LIMIT:
            raise DraftError('核心响应过大，停止操作')
        return json.loads(data) if data else None

    def snapshot(self, base):
        version = self.request(base, '/version')
        general = self.request(base, '/configs')
        rules = self.request(base, '/rules')['rules']
        proxies = self.request(base, '/proxies')['proxies']
        if not isinstance(version, dict) or version.get('meta') is not True or general.get('mode') != 'rule':
            raise DraftError('需要已开启规则模式的 mihomo / Meta 核心')
        if not isinstance(rules, list) or len(rules) > 20000:
            raise DraftError('核心规则列表格式未知或过大')
        if any((r.get('extra') or {}).get('disabled') for r in rules):
            raise DraftError('存在临时禁用规则，重载可能改变行为，请先在客户端核对')
        selections = {name: {'members': p['all'], 'current': p['now']}
                      for name, p in proxies.items() if p.get('type') == 'Selector'
                      and isinstance(p.get('all'), list) and p.get('now') in p['all']}
        return {'version': version, 'general': general, 'rules': [canonical_runtime(r) for r in rules],
                'selections': selections}

    def restore_selections(self, base, selections):
        current = self.snapshot(base)['selections']
        if set(current) != set(selections) or any(current[k]['members'] != s['members'] for k, s in selections.items()):
            raise DraftError('代理组成员发生变化，停止恢复旧选择，请在客户端核对')
        for name, selected in selections.items():
            if current[name]['current'] != selected['current']:
                self.request(base, '/proxies/' + quote(name, safe=''), {'name': selected['current']})

    def reload(self, base, text):
        self.request(base, '/configs?force=false', {'payload': text})
        self.client.invalidate_config_cache()


def _check_general(text, general):
    # A full PUT must not reset a user's controller PATCH to stale file values.
    data = json.loads(text) if text.lstrip().startswith('{') else None
    for key in GENERAL_KEYS:
        if key not in general:
            continue
        if data is not None:
            if key not in data:
                raise DraftError('生成配置缺少可核对的 ' + key + '，停止重载')
            value = data[key]
        else:
            hits = re.findall(r'^' + re.escape(key) + r':[ \t]*([^\r\n]*)', text, re.M)
            if len(hits) != 1:
                raise DraftError('生成配置缺少可核对的 ' + key + '，停止重载')
            raw = hits[0].strip()
            try:
                value = json.loads(raw)
            except ValueError:
                value = raw[1:-1].replace("''", "'") if raw.startswith("'") and raw.endswith("'") else raw
        if value != general[key]:
            raise DraftError('运行设置与生成文件不一致（' + key + '），请先由客户端应用当前设置')
    source_tun = data.get('tun', {}) if data is not None else {}
    if data is None:
        block = re.search(r'^tun:[ \t]*\r?\n((?:[ \t].*\r?\n|\r?\n)*)', text + '\n', re.M)
        if block:
            for key, raw in re.findall(r'^  ([a-z-]+):[ \t]*([^\r\n]*)', block[1], re.M):
                try:
                    source_tun[key] = json.loads(raw)
                except ValueError:
                    source_tun[key] = raw.strip("'\"")
    actual_tun = general.get('tun') or {}
    for key in ('enable', 'device', 'stack', 'auto-route', 'auto-detect-interface', 'mtu', 'strict-route'):
        if key in source_tun and key in actual_tun:
            expected, actual = source_tun[key], actual_tun[key]
            if key == 'stack' and isinstance(expected, str) and isinstance(actual, str):
                expected, actual = expected.casefold(), actual.casefold()
            if expected != actual:
                raise DraftError('TUN 运行设置与生成文件不一致（' + key + '），停止重载')


class ScopedIPv6:
    def __init__(self, root, path, controller, available=None, inspect=None):
        self.root = Path(root).resolve()
        self.path = Path(path)
        self.controller = controller
        self.available = available or (lambda scope: True)
        self.inspect = inspect or (lambda: inspect_flclash(self.root))
        self.lock = threading.RLock()
        self.health = {}

    def _read(self):
        if not self.path.exists():
            return {'version': 1, 'revision': 0, 'profiles': {}}
        try:
            if self.path.stat().st_size > 1024 * 1024:
                raise ValueError()
            state = json.loads(self.path.read_text(encoding='utf-8'))
            if state['version'] != 1 or type(state['revision']) is not int or not isinstance(state['profiles'], dict):
                raise ValueError()
            for record in state['profiles'].values():
                for e in record['entries'] + (record.get('pending') or {}).get('after', []):
                    if normalize_scope(e['scope']) != e['scope'] or digest(e['scope'])[:24] != e['id']:
                        raise ValueError()
            return state
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise DraftError('IPv6 屏蔽记录无法读取，原文件保留；请检查数据目录') from exc

    def _save(self, state):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix('.tmp')
            with temp.open('w', encoding='utf-8') as stream:
                json.dump(state, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            temp.replace(self.path)
        except OSError as exc:
            raise DraftError('IPv6 操作记录保存失败；请检查磁盘和权限，再重新核实当前状态') from exc

    def _context(self):
        meta = self.inspect()
        if not meta.get('available'):
            raise DraftError('无法核实 FlClash 当前配置，请先启动并应用客户端配置')
        base = self.controller.endpoint()
        key = digest([str(self.root), base, str(meta['profile_id'])])[:24]
        path = self.root / 'config.yaml'
        if path.stat().st_size > LIMIT:
            raise DraftError('生成配置过大')
        text = path.read_text(encoding='utf-8')
        rows = [canonical_rule(rule) for rule in document_rules(text)]
        snap = self.controller.snapshot(base)
        _check_general(text, snap['general'])
        snap['configuration_guard'] = configuration_guard(self.root, text, meta)
        return key, meta, base, text, rows, snap

    def _same(self, context):
        key, meta, base, text, rows, snap = context
        now = self._context()
        if now != context:
            raise DraftError('配置正在变化，未重载；请稍后重试')
        return key, meta, base, text, rows, snap

    def _view(self, state, key=None, snap=None, rows=None):
        records = []
        for rec_key, record in state['profiles'].items():
            current = rec_key == key
            active = current and snap['rules'] == [managed_row(e['scope']) for e in record['entries']] + rows
            health = self.health.get(rec_key, {})
            status = 'out_of_scope' if not current else 'unknown' if record.get('pending') else 'enabled' if active and record['entries'] else 'pending'
            for e in record['entries']:
                records.append({**e, 'profile': rec_key, 'current': current, 'state': status,
                                'applied': status == 'enabled', 'error': health.get('error', '')})
            if record.get('pending'):
                records.append({'id': 'pending', 'profile': rec_key, 'current': current,
                                'label': '上次操作结果待核实', 'state': status, 'applied': False})
        return {'revision': state['revision'], 'entries': records, 'notice': NOTICE,
                'supported': key is not None, 'profile': key, 'checked_at': time.time()}

    def catalog(self):
        with self.lock:
            state = self._read()
            try:
                key, _, _, _, rows, snap = self._context()
                return self._view(state, key, snap, rows)
            except Exception as exc:
                return {**self._view(state), 'error': str(exc) if isinstance(exc, DraftError) else '核心不可用，当前限制尚未核实'}

    def _resolve_pending(self, state, key, snap, base):
        record = state['profiles'][key]
        pending = record.get('pending')
        if not pending:
            return
        signature = digest(snap['rules'])
        if signature == pending['expected'] and digest(snap['general']) == pending['general']:
            if pending.get('configuration_guard') and snap['configuration_guard'] != pending['configuration_guard']:
                raise DraftError('DNS 或其他配置已变化，停止确认旧操作；请重新核实当前客户端设置')
            selections = pending.get('selections', {})
            if snap.get('selections', {}) != selections:
                self.controller.restore_selections(base, selections)
                restored_context = self._context()
                after = restored_context[5]
                if (digest(after['rules']) != signature or after['general'] != snap['general'] or
                        restored_context[0] != key or restored_context[2] != base or
                        after['configuration_guard'] != snap['configuration_guard'] or
                        after.get('selections', {}) != selections):
                    raise DraftError('代理组选择或规则尚未核实，保留操作记录，请重新核实')
            record['entries'] = pending['after']
        elif signature != pending['before']:
            raise DraftError('上次重载结果与当前配置不一致，请恢复原客户端配置后重新核实')
        record.pop('pending')
        state['revision'] += 1
        self._save(state)

    def operate(self, body):
        with self.lock:
            if not isinstance(body, dict) or body.get('operation') not in {'enable', 'disable', 'reconcile'}:
                raise DraftError('IPv6 屏蔽操作无效')
            op = body['operation']
            allowed = {'operation', 'revision', 'scope'} if op == 'enable' else {'operation', 'revision', 'id'} if op == 'disable' else {'operation', 'revision'}
            if set(body) != allowed:
                raise DraftError('IPv6 屏蔽参数无效')
            state = self._read()
            if type(body.get('revision')) is not int or body['revision'] != state['revision']:
                raise DraftError('设置已被其他操作修改，请刷新后重试')
            context = self._context()
            key, meta, base, text, rows, snap = context
            record = state['profiles'].get(key)
            if record and record.get('pending'):
                self._resolve_pending(state, key, snap, base)
                return self.catalog()
            if record is None:
                record = {'entries': []}
                state['profiles'][key] = record
            before = record['entries']
            after = copy.deepcopy(before)
            if op == 'enable':
                scope = normalize_scope(body.get('scope'))
                for leaf in leaves(scope):
                    if leaf['kind'] == 'process_path' and not self.available(leaf):
                        raise DraftError('所选应用身份无法核实，请先运行该应用并重新检查')
                if any(l['kind'] == 'process_path' for l in leaves(scope)) and snap['general'].get('find-process-mode') not in {'strict', 'always'}:
                    raise DraftError('请先在客户端开启进程识别，再屏蔽此应用的 IPv6')
                entry = {'id': digest(scope)[:24], 'scope': scope, 'label': describe(scope)}
                if entry not in after:
                    after.append(entry)
                if len(after) > 32 or len(state['profiles']) > 64:
                    raise DraftError('每个配置最多保存 32 项限制，最多 64 个配置')
            elif op == 'disable':
                after = [e for e in before if e['id'] != body.get('id')]
                if len(after) == len(before):
                    raise DraftError('当前配置中没有此限制，请刷新')
            owned = [managed_row(e['scope']) for e in before]
            if snap['rules'] not in (rows, owned + rows):
                raise DraftError('当前运行规则与客户端生成配置不一致，未覆盖；请先在客户端重新应用配置')
            if any(managed_row(e['scope']) in rows for e in after):
                raise DraftError('客户端中已有同文规则，不接管原规则；请先核对原配置')
            expected = [managed_row(e['scope']) for e in after] + rows
            if snap['rules'] == expected:
                if before != after:
                    record['entries'] = after
                    state['revision'] += 1
                    self._save(state)
                self.health.pop(key, None)
                return self.catalog()
            candidate = overlay(text, after)
            if configuration_guard(self.root, candidate, meta) != snap['configuration_guard']:
                raise DraftError('候选配置改变了 DNS 或原有非规则设置，未重载')
            self._same(context)
            # Write-ahead intent permits recovery even if the HTTP reply is lost.
            record['pending'] = {'after': after, 'before': digest(snap['rules']),
                                 'expected': digest(expected), 'general': digest(snap['general']),
                                 'configuration_guard': snap['configuration_guard'],
                                 'selections': snap.get('selections', {})}
            state['revision'] += 1
            self._save(state)
            error = None
            try:
                # Recheck after the durable journal write: no stale candidate may
                # cross a concurrent client DNS/profile/configuration change.
                self._same(context)
                self.controller.reload(base, candidate)
            except Exception as exc:
                error = exc
            deadline = time.monotonic() + 3
            while True:
                try:
                    current_context = self._context()
                    if current_context[0] != key or current_context[2] != base:
                        raise DraftError('控制器或配置已切换，停止核实旧重载')
                    current = current_context[5]
                    if current['rules'] == expected and current['general'] == snap['general']:
                        self._resolve_pending(state, key, current, base)
                        self.health.pop(key, None)
                        return self.catalog()
                    if error and current['rules'] == snap['rules']:
                        self._resolve_pending(state, key, current, base)
                        raise DraftError(str(error) if isinstance(error, DraftError) else '核心未接受变更，原规则已保留') from error
                except DraftError:
                    raise
                except Exception:
                    pass
                if time.monotonic() >= deadline:
                    raise DraftError('重载结果暂未核实；记录已保存，请点击“重新核实”，不会盲目重复写入')
                time.sleep(.1)

    def tick(self):
        with self.lock:
            state = self._read()
            if not any(r['entries'] or r.get('pending') for r in state['profiles'].values()):
                return
            key = None
            try:
                key, _, _, _, rows, snap = self._context()
                if key not in state['profiles']:
                    return
                record = state['profiles'][key]
                if not record.get('pending') and snap['rules'] == [managed_row(e['scope']) for e in record['entries']] + rows:
                    self.health.pop(key, None)
                    return
                self.operate({'operation': 'reconcile', 'revision': state['revision']})
            except Exception as exc:
                if key:
                    self.health[key] = {'error': str(exc) if isinstance(exc, DraftError) else '核心暂不可用，等待重新核实'}

    def start(self, stop):
        def maintain():
            while not stop.wait(5):
                try:
                    self.tick()
                except Exception:
                    pass  # catalog reports unreadable stores; never replace them.
        threading.Thread(target=maintain, daemon=True, name='scoped-ipv6').start()
