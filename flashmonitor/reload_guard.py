"""Read-only guards for FlClash-generated configuration reloads.

Only the conservative YAML subset emitted by the client is accepted. Unknown
syntax is a reason to refuse a full reload, never to guess at DNS settings.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .ipv6_plans import digest
from .rule_drafts import DraftError

LIMIT = 8 * 1024 * 1024


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError('duplicate key')
        obj[key] = value
    return obj


def _scalar(text):
    if text in {'null', '~', ''}:
        return None
    if text.startswith(('"', '[', '{')) or text in {'true', 'false'} or re.fullmatch(r'-?\d+', text):
        return json.loads(text, object_pairs_hook=_unique_object)
    if text.startswith("'") and text.endswith("'"):
        return text[1:-1].replace("''", "'")
    if text[:1] in "'\"[{!&*|>" or ' #' in text:
        raise ValueError('unsupported scalar')
    return text


def section(text, name):
    if text.lstrip().startswith('{'):
        return json.loads(text, object_pairs_hook=_unique_object).get(name)
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith(name + ':')]
    if len(starts) > 1:
        raise ValueError('duplicate section')
    if not starts:
        return None
    start = starts[0]
    inline = lines[start][len(name) + 1:].strip()
    rows = []
    for line in lines[start + 1:]:
        if line and not line[0].isspace() and not line.startswith('#'):
            break
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if '\t' in line:
            raise ValueError('tab indentation')
        rows.append((len(line) - len(line.lstrip()), line.lstrip()))
    if inline:
        if rows:
            raise ValueError('mixed section')
        return _scalar(inline)
    if not rows:
        return None

    def parse(index, indent):
        sequence = rows[index][1].startswith('- ')
        result = [] if sequence else {}
        while index < len(rows) and rows[index][0] == indent:
            line = rows[index][1]
            if sequence:
                if not line.startswith('- '):
                    raise ValueError('mixed collection')
                result.append(_scalar(line[2:].strip()))
                index += 1
            else:
                match = re.fullmatch(r'(.*?):(?:\s+(.*)|$)', line)
                if not match:
                    raise ValueError('unsupported mapping')
                key, raw = _scalar(match[1]), (match[2] or '').strip()
                if not isinstance(key, str) or key in result:
                    raise ValueError('duplicate or invalid key')
                index += 1
                if not raw and index < len(rows) and rows[index][0] > indent:
                    value, index = parse(index, rows[index][0])
                else:
                    value = _scalar(raw)
                result[key] = value
            if index < len(rows) and rows[index][0] > indent:
                raise ValueError('unsupported nesting')
        return result, index

    result, end = parse(0, rows[0][0])
    if end != len(rows):
        raise ValueError('inconsistent indentation')
    return result


def _equivalent(actual, expected, path=()):
    # Go's generated config represents empty collections as null; the Flutter
    # preferences model serializes the same empty DNS lists/maps as [] / {}.
    if actual is None and expected in ([], {}):
        return True
    if isinstance(expected, dict):
        return (isinstance(actual, dict) and (not path or set(actual) == set(expected))
                and all(k in actual and _equivalent(actual[k], v, path + (k,)) for k, v in expected.items()))
    return type(actual) is type(expected) and actual == expected


def configuration_guard(root, text, meta):
    """Compare DNS intent to generated DNS and fingerprint every non-rule byte."""
    try:
        dns = section(text, 'dns')
        hosts = section(text, 'hosts')
        prefs_path = Path(root) / 'shared_preferences.json'
        intent = None
        if prefs_path.exists() or meta.get('schema_verified'):
            if prefs_path.stat().st_size > 1024 * 1024:
                raise ValueError('preferences too large')
            prefs = json.loads(json.loads(prefs_path.read_text(encoding='utf-8'))['flutter.config'])
            if str(prefs['currentProfileId']) != str(meta['profile_id']):
                raise ValueError('profile changed')
            if type(prefs.get('overrideDns')) is not bool:
                raise ValueError('unknown DNS override mode')
            if prefs['overrideDns']:
                expected = prefs['patchClashConfig']['dns']
                if not isinstance(expected, dict):
                    raise ValueError('invalid DNS preferences')
            else:
                source = Path(root) / 'profiles' / (str(meta['profile_id']) + '.yaml')
                if source.stat().st_size > LIMIT:
                    raise ValueError('profile too large')
                expected = section(source.read_text(encoding='utf-8'), 'dns')
            if expected is not None and not _equivalent(dns, expected):
                raise DraftError('DNS 设置尚未同步到生成配置，未重载；请先在 FlClash 应用当前 DNS 设置')
            intent = digest([prefs['overrideDns'], expected])
        if text.lstrip().startswith('{'):
            non_rules = json.loads(text)
            non_rules.pop('rules', None)
        else:
            lines = text.splitlines(keepends=True)
            starts = [i for i, line in enumerate(lines) if line.rstrip() == 'rules:']
            if len(starts) != 1:
                raise ValueError('unknown rules section')
            start = starts[0]
            end = start + 1
            while end < len(lines):
                line = lines[end]
                if line.strip() and not line[:1].isspace() and not line.startswith(('- ', '#')):
                    break
                end += 1
            non_rules = ''.join(lines[:start] + lines[end:])
        return {'dns': digest(dns), 'hosts': digest(hosts), 'intent': intent, 'non_rules': digest(non_rules)}
    except DraftError:
        raise
    except (OSError, ValueError, TypeError, KeyError, IndexError, RecursionError) as exc:
        raise DraftError('无法可靠核对 DNS 与完整配置，未重载；请先在 FlClash 重新应用配置') from exc
