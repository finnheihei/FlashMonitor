"""Read-only, pinned-controller inventory. Never return raw proxy configuration."""
from __future__ import annotations

import time
import json
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .network_policy import CAPABILITIES, fingerprint, ordered_rules, process_path, domain, preview
from .rule_drafts import DraftError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def pinned_reader(base, headers):
    url = urlsplit(base)
    if url.scheme != 'http' or url.hostname not in {'127.0.0.1', '::1', 'localhost'} or url.username or url.password or url.path not in {'', '/'} or url.query or url.fragment:
        raise ValueError('local_controller_required')
    opener = build_opener(ProxyHandler({}), NoRedirect())

    def read(path):
        if path not in {'/version', '/configs', '/rules', '/proxies'}:
            raise ValueError('read_endpoint_not_allowed')
        with opener.open(Request(base.rstrip('/') + path, headers=headers, method='GET'), timeout=2.0) as response:
            payload = response.read(4 * 1024 * 1024 + 1)
            if len(payload) > 4 * 1024 * 1024:
                raise ValueError('inventory_too_large')
            return json.loads(payload)
    return read


def collect_inventory(read, *, controller_id: str, clock=time.time) -> dict:
    """Repeated endpoint reads detect visible changes, not atomic core snapshot proof."""
    try:
        before = [read('/version'), read('/configs'), read('/rules')]
        proxies = read('/proxies').get('proxies')
        after = [read('/version'), read('/configs'), read('/rules')]
        if not isinstance(proxies, dict) or len(proxies) > 10000:
            raise ValueError()
        parsed = ordered_rules(after[2].get('rules'))
        rules = [{"type": r["type"], "payload": r["payload"], "proxy": r["target"]} for r in parsed]
        targets, groups = {}, {}
        edges = 0
        for name, item in proxies.items():
            if not isinstance(name, str) or not name or len(name) > 256 or not isinstance(item, dict):
                raise ValueError()
            targets[name] = {"udp": item.get('udp') if type(item.get('udp')) is bool else None}
            members = item.get('all')
            if isinstance(members, list) and item.get('type') in {'Selector', 'URLTest', 'Fallback', 'LoadBalance', 'Relay'} and name not in {'GLOBAL', 'DIRECT', 'REJECT'}:
                edges += len(members)
                if edges > 50000:
                    raise ValueError()
                if len(members) > 10000 or any(not isinstance(x, str) or len(x) > 256 for x in members):
                    raise ValueError()
                groups[name] = {"type": item['type'], "members": members,
                                "current": item.get('now') if isinstance(item.get('now'), str) and len(item['now']) <= 256 else None}

        memo = {}
        def may_direct(name, seen):
            if name == 'DIRECT':
                return True
            if name in seen or len(seen) > 32:
                return True  # Cyclic/overdeep selection is not a guarantee.
            if name not in memo:
                memo[name] = any(may_direct(n, seen | {name}) for n in groups.get(name, {}).get('members', []))
            return memo[name]

        for name, group in groups.items():
            group['may_direct'] = may_direct(name, set())
        facts = {"version": str(after[0].get('version', ''))[:80], "mode": after[1].get('mode'),
                 "ipv6": after[1].get('ipv6') if type(after[1].get('ipv6')) is bool else None,
                 "rules": rules, "groups": groups, "targets": targets, "controller_id": controller_id}
        return {**facts, "available": True, "consistent": before == after, "sampled_at": clock(),
                "revision": fingerprint(facts), "atomic_snapshot": False}
    except Exception:
        # Exceptions can contain URLs, headers, credentials or filesystem paths.
        return {"available": False, "consistent": False, "error": "无法读取一致的核心清单；未修改配置。"}


class NetworkPolicyService:
    """No executor parameter: browser-provided capability claims cannot enable writes."""
    def __init__(self, inventory, live, *, clock=time.time):
        self.inventory = inventory
        self.live = live
        self.clock = clock

    def _flows(self):
        state = self.live() or {}
        age = self.clock() - float(state.get('ts') or 0)
        return (state.get('flows') or []) if not state.get('error') and 0 <= age <= 15 else []

    def catalog(self):
        inv = self.inventory()
        apps, domains = {}, set()
        for flow in self._flows():
            if flow.get('identity_verified') is True and not flow.get('identity_conflict'):
                try:
                    path = process_path(flow.get('proc_path'))
                    apps[path.casefold()] = {"path": path, "label": str(flow.get('title') or flow.get('process') or path)[:256]}
                except DraftError:
                    pass
            try:
                domains.add(domain(flow.get('host')))
            except DraftError:
                pass
        return {"capabilities": dict(CAPABILITIES), "applications": list(apps.values()), "domains": sorted(domains),
                "inventory": {k: inv.get(k) for k in ('available', 'consistent', 'version', 'mode', 'revision', 'sampled_at', 'groups', 'error')},
                "rule_count": len(inv.get('rules', [])), "state": "not_started"}

    def preview(self, body):
        return preview(body, self.inventory(), self._flows(), now=self.clock())
