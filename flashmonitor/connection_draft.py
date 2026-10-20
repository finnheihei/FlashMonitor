"""Strict local diagnosis selections; never core configuration or credentials."""
from __future__ import annotations

import json

from .network_policy import domain, process_path, safe_text
from .rule_drafts import DraftError

FIELDS = {'version', 'kind', 'applications', 'domains', 'target', 'block_ipv6', 'block_quic'}


def normalize_draft(raw):
    if not isinstance(raw, dict) or set(raw) != FIELDS or type(raw['version']) is not int or raw['version'] != 1:
        raise DraftError('诊断草稿字段或版本无效')
    kind = raw['kind']
    if not isinstance(kind, str) or kind not in {'application', 'domain', 'browser'}:
        raise DraftError('请选择有效的诊断范围')
    for key in ('block_ipv6', 'block_quic'):
        if type(raw[key]) is not bool:
            raise DraftError('兼容选项必须为明确的勾选值')
    if not isinstance(raw['applications'], list) or not isinstance(raw['domains'], list) or len(raw['applications']) > 32 or len(raw['domains']) > 32:
        raise DraftError('应用和网站各最多保存32项')
    apps, domains, seen_apps, seen_domains = [], [], set(), {}
    try:
        for value in raw['applications']:
            path = process_path(value)
            if path.casefold() not in seen_apps:
                seen_apps.add(path.casefold()); apps.append(path)
    except DraftError as exc:
        raise DraftError('应用路径无效，请选择完整路径') from exc
    for item in raw['domains']:
        if not isinstance(item, dict) or set(item) != {'value', 'include_subdomains'} or type(item['include_subdomains']) is not bool:
            raise DraftError('网站及子域名选择无效')
        try:
            value = domain(item['value'])
        except DraftError as exc:
            raise DraftError('网站格式无效，请填写域名，不要填写网址、端口或凭据') from exc
        sub = item['include_subdomains']
        if value in seen_domains and seen_domains[value] != sub:
            raise DraftError('同一网站的子域名选择冲突')
        if value not in seen_domains:
            seen_domains[value] = sub; domains.append({'value': value, 'include_subdomains': sub})
    if kind == 'application' and (not apps or domains) or kind == 'domain' and (not domains or apps) or kind == 'browser' and (not apps or not domains):
        raise DraftError('请完整选择当前范围，不能保存隐藏的其他范围')
    try:
        target = safe_text(raw['target'], 256).strip()
        if not target:
            raise DraftError('empty')
    except DraftError as exc:
        raise DraftError('请选择要用于诊断的代理组') from exc
    result = {'version': 1, 'kind': kind, 'applications': apps, 'domains': domains,
              'target': target, 'block_ipv6': raw['block_ipv6'], 'block_quic': raw['block_quic']}
    try:
        size = len(json.dumps(result, ensure_ascii=False).encode('utf-8'))
    except UnicodeError as exc:
        raise DraftError('草稿包含不支持的字符') from exc
    if size > 32768:
        raise DraftError('诊断草稿过大，请减少应用或网站')
    return result
