"""IPv4 intent drafts only. No DNS, controller or network operations."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import threading
import unicodedata
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit

from .rule_drafts import DraftError, token

STATUS = {"applied": False, "execution": "unverified", "route": "preserve"}
WARNINGS = [
    "未应用：仅保存 IPv4 配置意图，不改变连接或原代理/直连路线。",
    "执行方式未验证；IPv4 优先或过滤 AAAA 不等于真正强制 IPv4。",
    "严格执行时，没有可用 IPv4 的目标将无法连接，不保证自动回落。",
    "尚未检查内核完整规则顺序；绕过内核的连接不在当前能力范围内。",
]
# Conservative offline guard, not a complete public suffix database.
PUBLIC_SUFFIXES = {"co.uk", "org.uk", "ac.uk", "com.cn", "net.cn", "org.cn",
                   "com.au", "net.au", "co.jp", "co.nz", "com.br", "co.in"}


def _text(value, limit=1024):
    if not isinstance(value, str) or not value or len(value) > limit or value != value.strip():
        raise DraftError("对象不能为空、过长或包含首尾空格")
    if any(unicodedata.category(c) in {"Cc", "Cf", "Cs", "Zl", "Zp"} or c == "," for c in value):
        raise DraftError("对象不能包含逗号或控制字符")
    return value


def normalize_subject(value):
    if not isinstance(value, dict) or set(value) != {"kind", "value", "include_subdomains"}:
        raise DraftError("对象字段无效")
    kind, raw, sub = value["kind"], _text(value["value"]), value["include_subdomains"]
    if type(sub) is not bool:
        raise DraftError("子域名选项无效")
    if kind == "process_path":
        path = PureWindowsPath(raw)
        if sub or not path.is_absolute() or not path.drive.endswith(":") or not path.name.lower().endswith(".exe"):
            raise DraftError("请选择完整 Windows 可执行文件路径")
        if any(c in '<>"|?*' for c in raw) or ":" in raw[2:] or ".." in path.parts:
            raise DraftError("可执行文件路径无效")
        normalized = str(path)
    elif kind == "process_name":
        if sub:
            raise DraftError("应用不能包含子域名选项")
        normalized = token(raw, executable=True)
    elif kind == "domain":
        if "://" in raw:
            try:
                url = urlsplit(raw)
                if url.scheme.lower() not in {"http", "https"} or url.username is not None or url.password is not None:
                    raise ValueError()
                # Validate even when the port is ignored for matching.
                url.port
                raw = url.hostname or ""
            except ValueError as exc:
                raise DraftError("请输入域名或有效的 HTTP/HTTPS 网址") from exc
        raw = raw.rstrip(".")
        try:
            ipaddress.ip_address(raw.strip("[]"))
        except ValueError:
            pass
        else:
            raise DraftError("网站匹配需要域名，不能使用 IP 地址")
        try:
            normalized = raw.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise DraftError("域名格式无效") from exc
        labels = normalized.split(".")
        if (len(normalized) > 253 or len(labels) < 2 or
                any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", x) for x in labels) or
                not re.search(r"[a-z]", labels[-1])):
            raise DraftError("请输入完整域名，不支持通配符、路径或端口")
        if sub and normalized in PUBLIC_SUFFIXES:
            raise DraftError("不能将公共后缀作为包含子域名的匹配对象")
    else:
        raise DraftError("请选择应用或网站对象")
    return {"kind": kind, "value": normalized, "include_subdomains": sub}


def subject_id(subject):
    key = json.dumps({**subject, "value": subject["value"].casefold()}, sort_keys=True)
    return hashlib.sha256(key.encode()).hexdigest()[:24]


def describe(subject):
    kind = subject["kind"]
    return {"process_path": "仅此可执行文件路径", "process_name": "所有同名可执行文件",
            "domain": "域名及其子域名" if subject["include_subdomains"] else "仅此域名"}[kind]


class IPv4Drafts:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()

    def read(self):
        if not self.path.exists():
            return {"schema_version": 1, "revision": 0, "drafts": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or set(data) != {"schema_version", "revision", "drafts"} or
                    data["schema_version"] != 1 or type(data["schema_version"]) is not int or
                    type(data["revision"]) is not int or data["revision"] < 0 or
                    not isinstance(data["drafts"], dict) or len(data["drafts"]) > 256):
                raise ValueError()
            for key, draft in data["drafts"].items():
                if not isinstance(draft, dict) or set(draft) != {"subject", "desired_family", "route"}:
                    raise ValueError()
                subject = normalize_subject(draft["subject"])
                if (subject != draft["subject"] or subject_id(subject) != key or
                        draft["desired_family"] != "ipv4" or draft["route"] != "preserve"):
                    raise ValueError()
            return data
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise DraftError("IPv4 草稿无法读取；原文件保留，请检查数据目录") from exc

    def catalog(self):
        with self.lock:
            data = self.read()
            return {**STATUS, "revision": data["revision"], "drafts": [
                {"id": key, **draft, "match": describe(draft["subject"])}
                for key, draft in sorted(data["drafts"].items())]}

    @staticmethod
    def _revision(data, revision):
        if type(revision) is not int or revision != data["revision"]:
            raise DraftError("草稿已变化，请刷新后重试")

    def operate(self, body):
        if not isinstance(body, dict) or body.get("operation") not in {"preview", "save", "delete", "export"}:
            raise DraftError("仅支持 IPv4 草稿预览、保存、删除和意图导出")
        if body.get("route", "preserve") != "preserve" or body.get("desired_family", "ipv4") != "ipv4":
            raise DraftError("当前仅支持保留原路线的 IPv4 意图草稿")
        with self.lock:
            data = self.read()
            self._revision(data, body.get("revision"))
            operation = body["operation"]
            if operation in {"delete", "export"}:
                key = body.get("id")
                if not isinstance(key, str) or key not in data["drafts"]:
                    raise DraftError("草稿不存在，请刷新")
                if operation == "export":
                    return {**STATUS, "text": json.dumps({"format": "FlashMonitor IPv4 intent draft",
                        "schema_version": 1, **STATUS, "notice": "未应用；不是可导入代理核心的配置",
                        "draft": data["drafts"][key]}, ensure_ascii=False, indent=2)}
                del data["drafts"][key]
                self._write(data)
                return self.catalog()
            subject = normalize_subject(body.get("subject"))
            key = subject_id(subject)
            draft = {"subject": subject, "desired_family": "ipv4", "route": "preserve"}
            warnings = list(WARNINGS)
            if subject["kind"] == "process_name":
                warnings.append("按文件名匹配会影响不同路径的所有同名程序。")
            if subject["kind"] == "domain":
                warnings.append("网址路径和端口不参与匹配；第三方域名、CDN、缓存或自带 DNS 不会自动扩张为本站。")
                if subject["include_subdomains"]:
                    warnings.append("包含根域及子域；请确认范围不是公共后缀。当前仅离线检查常见公共后缀。")
                overlaps = [d["subject"]["value"] for k, d in data["drafts"].items() if k != key and
                    d["subject"]["kind"] == "domain" and self._overlaps(subject, d["subject"])]
                if overlaps:
                    warnings.append("与已保存网站草稿范围重叠（相同 IPv4 意图）：" + "、".join(overlaps))
            if operation == "save":
                if key not in data["drafts"] and len(data["drafts"]) >= 256:
                    raise DraftError("最多保存 256 个 IPv4 草稿")
                data["drafts"][key] = draft
                self._write(data)
            return {**STATUS, "revision": data["revision"], "id": key, "draft": draft,
                    "match": describe(subject), "warnings": warnings}

    @staticmethod
    def _overlaps(a, b):
        return (a["value"] == b["value"] or
                (a["include_subdomains"] and b["value"].endswith("." + a["value"])) or
                (b["include_subdomains"] and a["value"].endswith("." + b["value"])))

    def _write(self, data):
        data["revision"] += 1
        tmp = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            raise DraftError("IPv4 草稿保存失败；原文件未改变，请检查数据目录权限") from exc
