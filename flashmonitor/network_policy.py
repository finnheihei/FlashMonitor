"""Bounded GPT network previews. No client, database or network mutation exists here.

Rule analysis is three-valued: an unsupported predicate is UNKNOWN, never FALSE.
This is diagnostic evaluation of observed metadata, not a replacement Mihomo parser.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import ntpath
import re
import time
from typing import Any

from .rule_drafts import DraftError

MAX_NODES = 64
MAX_DEPTH = 8
MAX_RULES = 10000


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def safe_text(value: Any, limit: int = 1024, *, grammar: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise DraftError("invalid_text：值为空或过长")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise DraftError("invalid_text：不允许控制字符")
    if grammar and any(c in value for c in ',()"\''):
        raise DraftError("invalid_text：当前规则格式无法安全表示该值")
    return value


def process_path(value: Any) -> str:
    value = safe_text(value).replace("/", "\\")
    if any(c in value for c in ',"\''):
        raise DraftError("invalid_text：当前规则格式无法安全表示该值")
    drive, tail = ntpath.splitdrive(value)
    # Reject device paths, UNC and relative paths; never silently use process name.
    if not re.fullmatch(r"[A-Za-z]:", drive) or not tail.startswith("\\"):
        raise DraftError("invalid_path：需要带盘符的完整程序路径")
    if any(c in tail for c in '*?:<>|'):
        raise DraftError("invalid_path：不允许通配符或设备路径")
    value = ntpath.normpath(value)
    # Logic rules also use parentheses; balanced literal pairs preserve their structure.
    depth = 0
    for char in value:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if depth < 0:
            raise DraftError("invalid_path：应用路径括号必须成对")
    if depth:
        raise DraftError("invalid_path：应用路径括号必须成对")
    return value


def domain(value: Any) -> str:
    value = safe_text(value, 253, grammar=True).strip().rstrip(".")
    try:
        value = value.encode("idna").decode("ascii").lower()
        if len(value) > 253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", x)
                                       for x in value.split(".")) or "." not in value:
            raise ValueError()
        try:
            ipaddress.ip_address(value)
        except ValueError:
            return value
    except (UnicodeError, ValueError):
        pass
    raise DraftError("invalid_domain：请输入完整域名，不含网址路径、端口或 IP")


def normalize_scope(raw: Any) -> dict:
    count = 0

    def visit(node: Any, depth: int) -> dict:
        nonlocal count
        count += 1
        if count > MAX_NODES or depth > MAX_DEPTH or not isinstance(node, dict):
            raise DraftError("invalid_scope：范围过大或格式错误")
        kind = node.get("kind")
        if kind in {"any", "all"}:
            if set(node) != {"kind", "children"} or not isinstance(node["children"], list) or not node["children"]:
                raise DraftError("invalid_scope：组合条件不能为空")
            children = [visit(n, depth + 1) for n in node["children"]]
            unique = {fingerprint(n): n for n in children}
            children = [unique[k] for k in sorted(unique)]
            return children[0] if len(children) == 1 else {"kind": kind, "children": children}
        if kind == "process_path" and set(node) == {"kind", "value"}:
            return {"kind": kind, "value": process_path(node["value"])}
        if kind == "domain" and set(node) <= {"kind", "value", "include_subdomains"}:
            suffix = node.get("include_subdomains", False)
            if type(suffix) is not bool:
                raise DraftError("invalid_scope：子域选项必须是布尔值")
            return {"kind": kind, "value": domain(node.get("value")), "include_subdomains": suffix}
        raise DraftError("invalid_scope：仅支持精确程序路径、域名和 AND/OR 组合")

    return visit(raw, 0)


def leaves(scope: dict):
    if "children" in scope:
        for child in scope["children"]:
            yield from leaves(child)
    else:
        yield scope


def describe(scope: dict) -> str:
    kind = scope["kind"]
    if kind in {"any", "all"}:
        return "（" + (" 或 " if kind == "any" else " 且 ").join(map(describe, scope["children"])) + "）"
    if kind == "process_path":
        return "程序 " + scope["value"]
    return scope["value"] + ("（含子域）" if scope["include_subdomains"] else "（精确域名）")


def condition(scope: dict) -> str:
    kind = scope["kind"]
    if kind in {"any", "all"}:
        return ("OR" if kind == "any" else "AND") + ",(" + ",".join("(" + condition(n) + ")" for n in scope["children"]) + ")"
    return ("PROCESS-PATH" if kind == "process_path" else "DOMAIN-SUFFIX" if scope["include_subdomains"] else "DOMAIN") + "," + scope["value"]


def _combine(kind: str, values: list[bool | None]) -> bool | None:
    if kind == "any":
        return True if True in values else None if None in values else False
    return False if False in values else None if None in values else True


def scope_matches(scope: dict, flow: dict) -> bool | None:
    if scope["kind"] in {"any", "all"}:
        return _combine(scope["kind"], [scope_matches(n, flow) for n in scope["children"]])
    if scope["kind"] == "process_path":
        if flow.get("identity_verified") is not True or flow.get("identity_conflict") or not flow.get("proc_path"):
            return None
        return ntpath.normcase(ntpath.normpath(str(flow["proc_path"]))) == ntpath.normcase(scope["value"])
    host = flow.get("host")
    if not host:
        return None
    try:
        host = domain(host)
    except DraftError:
        return None
    return host == scope["value"] or (scope["include_subdomains"] and host.endswith("." + scope["value"]))


def _parts(text: str) -> list[str]:
    """Split only at depth zero; malformed parentheses are not guessed."""
    result, start, depth = [], 0, 0
    for i, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise ValueError("unbalanced")
        elif char == "," and depth == 0:
            result.append(text[start:i].strip()); start = i + 1
    if depth:
        raise ValueError("unbalanced")
    result.append(text[start:].strip())
    return result


ALIASES = {"DOMAIN": "DOMAIN", "DOMAINSUFFIX": "DOMAIN-SUFFIX", "DOMAINKEYWORD": "DOMAIN-KEYWORD",
           "PROCESSPATH": "PROCESS-PATH", "PROCESSNAME": "PROCESS-NAME", "IPCIDR": "IP-CIDR",
           "IPCIDR6": "IP-CIDR6", "DSTPORT": "DST-PORT", "MATCH": "MATCH", "AND": "AND", "OR": "OR",
           "NOT": "NOT", "NETWORK": "NETWORK", "GEOSITE": "GEOSITE", "RULESET": "RULE-SET"}


def parse_condition(text: str, depth: int = 0, budget: list | None = None) -> dict:
    budget = [MAX_NODES] if budget is None else budget
    budget[0] -= 1
    if depth > MAX_DEPTH or budget[0] < 0 or len(text) > 8192:
        return {"type": "UNKNOWN", "reason": "complexity_limit"}
    try:
        parts = _parts(text)
        name = ALIASES.get(parts[0].upper().replace("-", ""), parts[0].upper())
        if name in {"AND", "OR", "NOT"}:
            if len(parts) != 2 or not parts[1].startswith("(") or not parts[1].endswith(")"):
                raise ValueError()
            children = []
            for child in _parts(parts[1][1:-1]):
                if not child.startswith("(") or not child.endswith(")"):
                    raise ValueError()
                children.append(parse_condition(child[1:-1], depth + 1, budget))
            if not children or name == "NOT" and len(children) != 1:
                raise ValueError()
            return {"type": name, "children": children}
        if name == "MATCH" and len(parts) <= 2 and (len(parts) == 1 or not parts[1]):
            return {"type": name}
        if len(parts) not in {2, 3} or len(parts) == 3 and parts[2].lower() != "no-resolve":
            raise ValueError()
        return {"type": name, "value": parts[1]}
    except (ValueError, IndexError):
        return {"type": "UNKNOWN", "reason": "unparsed_condition"}


def rule_matches(node: dict, flow: dict) -> bool | None:
    kind, value = node["type"], node.get("value", "")
    if kind in {"AND", "OR", "NOT"}:
        results = [rule_matches(n, flow) for n in node["children"]]
        return (None if results[0] is None else not results[0]) if kind == "NOT" else _combine("all" if kind == "AND" else "any", results)
    if kind == "MATCH":
        return True
    if kind in {"DOMAIN", "DOMAIN-SUFFIX", "PROCESS-PATH"}:
        try:
            raw = {"kind": "process_path", "value": value} if kind == "PROCESS-PATH" else {"kind": "domain", "value": value, "include_subdomains": kind == "DOMAIN-SUFFIX"}
            return scope_matches(normalize_scope(raw), flow)
        except DraftError:
            return None
    if kind == "DOMAIN-KEYWORD":
        return value.lower() in str(flow["host"]).lower() if flow.get("host") else None
    if kind == "PROCESS-NAME":
        return str(flow.get("rule_exe", "")).casefold() == value.casefold() if flow.get("identity_verified") is True and not flow.get("identity_conflict") and flow.get("rule_exe") else None
    if kind == "NETWORK":
        network = str(flow.get("network", "")).lower()
        return network == value.lower() if network in {"tcp", "udp"} and value.lower() in {"tcp", "udp"} else None
    if kind == "DST-PORT":
        # Port ranges and absent metadata are deliberately opaque.
        port = flow.get("destination_port")
        if type(port) not in {int, str} or not re.fullmatch(r"[0-9]{1,5}", str(port)) or not 1 <= int(port) <= 65535:
            return None
        return int(port) == int(value) if re.fullmatch(r"[0-9]{1,5}", value) and 1 <= int(value) <= 65535 else None
    if kind in {"IP-CIDR", "IP-CIDR6"}:
        try:
            address = ipaddress.ip_address(flow.get("destination_ip", ""))
            if flow.get("fake_ip") or address in ipaddress.ip_network("198.18.0.0/15"):
                return None
            return address in ipaddress.ip_network(value, strict=False)
        except ValueError:
            return None
    # GEOSITE/RULE-SET require the exact installed dataset, not an online guess.
    return None


def ordered_rules(rows: Any) -> list[dict]:
    if not isinstance(rows, list) or len(rows) > MAX_RULES:
        raise DraftError("inventory_unavailable：规则清单无效或过大")
    result = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise DraftError("inventory_unavailable：规则清单格式错误")
        name = safe_text(row.get("type"), 100)
        payload = row.get("payload", "")
        if not isinstance(payload, str) or len(payload) > 8192 or any(ord(c) < 32 for c in payload):
            raise DraftError("inventory_unavailable：规则内容无效")
        target = safe_text(row.get("proxy"), 256)
        node = parse_condition(name + ("," + payload if payload else ""))
        result.append({"index": index, "type": name, "payload": payload, "target": target, "condition": node})
    return result


def trace_rules(rules: list[dict], flow: dict, targets: dict) -> dict:
    uncertain = []
    skipped = []
    network = str(flow.get("network", "")).lower()
    for row in rules:
        matches = rule_matches(row["condition"], flow)
        if matches is False:
            continue
        if matches is None:
            uncertain.append(row["index"])
            continue
        target = row["target"]
        # PASS continues evaluation; unsupported UDP can fall through to DIRECT.
        if target == "PASS" or (network == "udp" and targets.get(target, {}).get("udp") is False and target not in {"DIRECT", "REJECT", "REJECT-DROP"}):
            skipped.append(row["index"])
            continue
        # Missing/invalid protocol could be UDP: a TCP-only target cannot be
        # certified, and its possible DIRECT fallthrough must remain visible.
        if network != "tcp" and target not in {"REJECT", "REJECT-DROP", "DIRECT"} and targets.get(target, {}).get("udp") is not True:
            uncertain.append(row["index"])
            continue
        return {"certainty": "unknown" if uncertain else "observed_metadata_match", "index": row["index"],
                "target": target, "unknown_before": uncertain, "skipped": skipped}
    return {"certainty": "unknown" if uncertain else "no_match", "index": None, "target": None,
            "unknown_before": uncertain, "skipped": skipped}


CAPABILITIES = {"diagnose": True, "preview": True, "apply": False, "undo": False,
                "force_ipv4": False, "client_session_verified": False,
                "reason": "当前客户端尚无经过验收的原生写入通道；预览不会保存或应用网络配置。"}


def preview(request: dict, inventory: dict, flows: list[dict], *, now: float | None = None) -> dict:
    if not isinstance(request, dict) or set(request) - {"scope", "target", "block_ipv6", "block_quic"}:
        raise DraftError("invalid_request：不支持该操作或配置字段")
    scope = normalize_scope(request.get("scope"))
    target = safe_text(request.get("target"), 256, grammar=True)
    options = {}
    for key in ("block_ipv6", "block_quic"):
        options[key] = request.get(key, False)
        if type(options[key]) is not bool:
            raise DraftError("invalid_request：兼容性选项必须是布尔值")
    if not inventory.get("available"):
        raise DraftError("inventory_unavailable：无法读取一致的核心清单，未生成可应用配置")
    groups = inventory.get("groups", {})
    if target in {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "GLOBAL"} or target not in groups:
        raise DraftError("target_unavailable：请选择当前清单中的代理组")
    rules = ordered_rules(inventory.get("rules"))
    now = time.time() if now is None else now
    blockers = ["native_writer_unavailable"]
    if inventory.get("mode") != "rule":
        blockers.append("not_rule_mode")
    if inventory.get("consistent") is not True or now - inventory.get("sampled_at", 0) > 15 or now < inventory.get("sampled_at", 0):
        blockers.append("inventory_stale_or_changed")
    for leaf in leaves(scope):
        if leaf["kind"] == "process_path" and not any(scope_matches(leaf, f) is True for f in flows):
            blockers.append("process_identity_unverified")
    base = condition(scope)
    proposed = []
    if options["block_ipv6"]:
        proposed.append(f"AND,(({base}),(IP-CIDR6,::/0,no-resolve)),REJECT")
    if options["block_quic"]:
        proposed.append(f"AND,(({base}),(NETWORK,udp),(DST-PORT,443)),REJECT")
    proposed.append(f"{base},{target}")
    samples = []
    for f in flows[:500]:
        matched = scope_matches(scope, f)
        if matched is False:
            continue
        trace = trace_rules(rules, f, inventory.get("targets", {}))
        samples.append({"process": str(f.get("process", ""))[:256], "host": str(f.get("host", ""))[:253],
                        "scope_match": matched, "outbound": f.get("outbound") if f.get("outbound") in {"proxy", "direct", "unknown", "rejected"} else "unknown",
                        "network": str(f.get("network", ""))[:16], "trace": trace})
    risks = ["仅为变更预览，未保存、未加载，也未验证连接。",
             "候选规则拟置于现有规则之前；可能覆盖已有直连或拒绝规则，实际提交前必须复核顺序。",
             "诊断使用已观察元数据；未展开的 GEOSITE / RULE-SET 和未知条件保留为未知。",
             "代理组可能切换节点；本方案不改变共享组的选择，也不保证固定出口。"]
    if options["block_ipv6"]:
        risks.append("阻止核心识别到的 IPv6 目标连接，不保证应用回退 IPv4；代理服务器出口、fake-IP 或绕过核心的流量不在此保证内。")
    if options["block_quic"]:
        risks.append("仅阻止选定范围 UDP/443，可能影响 QUIC、语音或 WebRTC；不把 UDP 自动转换成 TCP。")
    if inventory.get("groups", {}).get(target, {}).get("may_direct"):
        risks.append("所选组可能回退或选择 DIRECT，不能作为无直连保证。")
    policy = {"scope": scope, "target": target, **options}
    return {"schema": 1, "state": "preview_only", "capabilities": dict(CAPABILITIES),
            "policy": policy, "scope_label": describe(scope), "policy_id": fingerprint(policy),
            "inventory_revision": inventory.get("revision"), "sampled_at": inventory.get("sampled_at"),
            "proposed_rules": proposed, "existing_rule_count": len(rules), "samples": samples,
            "inspected_flow_count": min(len(flows), 500), "total_flow_count": len(flows), "sample_truncated": len(flows) > 500,
            "blockers": sorted(set(blockers)), "risks": risks,
            "unchanged": ["global_dns", "global_ipv6", "node_transport", "tls_fingerprint", "shared_group_selection"],
            "evidence": {"persisted": False, "loaded": False, "connection_verified": False}}


def _finite_time(value: Any) -> bool:
    try:
        return type(value) in {int, float} and math.isfinite(value)
    except OverflowError:
        return False


def evidence_state(record: dict | None, *, trusted_session: bool = False, revision: str = "",
                   context: dict | None = None, now: float | None = None) -> str:
    """Fail-closed display projection for future bridge receipts; cannot execute writes."""
    if not record:
        return "not_started"
    if record.get("state") == "preview_only":
        return "preview_only"
    if not trusted_session or not revision or record.get("revision") != revision:
        return "unknown"
    context = context or {}
    for key in ("instance_id", "root_identity", "profile_id", "policy_id", "operation_id"):
        if not isinstance(context.get(key), str) or not context[key] or record.get(key) != context[key]:
            return "unknown"
    now = time.time() if now is None else now
    checked = record.get("checked_at")
    if not _finite_time(now) or not _finite_time(checked) or not 0 <= now - checked <= 15:
        return "unknown"
    if any(key in record and not _finite_time(record[key]) for key in ("applied_at", "connection_at")):
        return "unknown"
    state = record.get("state")
    if state in {"applying", "recovery_required", "unknown"}:
        return state
    if state == "revoked":
        return "revoked" if record.get("owned_records_removed") is True and record.get("runtime_absent") is True else "recovery_required"
    if state not in {"saved", "loaded", "verified"}:
        return "unknown"
    if record.get("persisted") is not True:
        return "unknown"
    if record.get("loaded") is not True:
        return "saved_not_loaded"
    observed, applied = record.get("connection_at"), record.get("applied_at")
    if record.get("connection_verified") is True and _finite_time(observed) and _finite_time(applied) and applied <= observed <= checked:
        return "connection_observed"
    return "loaded_awaiting_connection"
