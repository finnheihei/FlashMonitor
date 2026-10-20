"""Local-only process rule drafts. This module never writes to a proxy controller."""
from __future__ import annotations
import json
import threading
import unicodedata
from pathlib import Path, PureWindowsPath
from typing import Any


class DraftError(ValueError):
    pass


def token(value: Any, *, executable: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value)>240 or value!=value.strip():
        raise DraftError("名称为空、过长或包含首尾空格")
    if any(unicodedata.category(c) in {"Cc","Cf","Cs","Zl","Zp"} or c=="," for c in value):
        raise DraftError("规则名称不能包含逗号或控制字符")
    if executable and (not value.lower().endswith(".exe") or any(c in '<>:"/\\|?*' for c in value)):
        raise DraftError("缺少可靠的 Windows 可执行文件名，不能生成规则")
    return value


def identity(flows: list[dict], process: str) -> tuple[str, str]:
    matches=[f for f in flows if f.get("process")==process]
    names={}
    for f in matches:
        raw=f.get("rule_exe") or (PureWindowsPath(f["proc_path"]).name if f.get("proc_path") else "")
        try: name=token(raw,executable=True)
        except DraftError: return "", "当前连接缺少可靠可执行文件名；不能用显示名补上 .exe"
        names[name.casefold()]=name
    if len(names)!=1:
        return "", "未获取当前进程身份，或该条目对应多个可执行文件"
    return next(iter(names.values())), ""


def snippet(exe: str, target: str) -> str:
    rule="PROCESS-NAME,"+token(exe,executable=True)+","+token(target)
    return "rules:\n  - "+json.dumps(rule,ensure_ascii=False)+"\n"


class RuleDrafts:
    def __init__(self, path: Path):
        self.path=path
        self.lock=threading.RLock()

    def read(self) -> dict:
        if not self.path.exists(): return {"revision":0,"drafts":{}}
        try:
            value=json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value,dict) or type(value.get("revision")) is not int or not isinstance(value.get("drafts"),dict): raise ValueError()
            for process,d in value["drafts"].items():
                token(process);token(d["exe"],executable=True);token(d["target"])
                if d["action"] not in ("direct","proxy"): raise ValueError()
            return value
        except (ValueError,KeyError,TypeError) as exc:
            raise DraftError("草稿文件损坏，已保留原文件；请先检查数据目录") from exc

    def catalog(self) -> dict:
        with self.lock:
            data=self.read()
            return {"revision":data["revision"], "drafts":[
                {"process":process, "draft":dict(draft), "snippet":snippet(draft["exe"],draft["target"])}
                for process,draft in sorted(data["drafts"].items())], "applied":False}

    def view(self, process: str) -> dict:
        with self.lock:
            value=self.read()
            return {"revision":value["revision"],"draft":value["drafts"].get(process)}

    def check(self, process: str, exe: str, target: str) -> None:
        for key,d in self.read()["drafts"].items():
            if key!=process and d["exe"].casefold()==exe.casefold():
                raise DraftError("同一可执行文件已有草稿（重复或冲突），请从原条目编辑")

    def save(self, process: str, exe: str, action: str, target: str, revision: int) -> dict:
        token(process);token(exe,executable=True);token(target)
        if action not in ("direct","proxy") or (action=="direct" and target!="DIRECT"):
            raise DraftError("规则动作无效")
        with self.lock:
            self.check(process,exe,target)
            data=self.read()
            self._revision(data,revision)
            data["drafts"][process]={"exe":exe,"action":action,"target":target}
            self._write(data)
            return self.view(process)

    def delete(self, process: str, revision: int) -> dict:
        token(process)
        with self.lock:
            data=self.read();self._revision(data,revision)
            data["drafts"].pop(process,None)
            self._write(data)
            return self.view(process)

    @staticmethod
    def _revision(data, revision):
        if type(revision) is not int or revision!=data["revision"]:
            raise DraftError("草稿已在其他窗口修改，请关闭后重新打开")

    def _write(self,data):
        data["revision"]+=1
        self.path.parent.mkdir(parents=True,exist_ok=True)
        tmp=self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            raise DraftError("草稿保存失败，原草稿未改变；请检查数据目录权限") from exc

    def export(self,process: str,revision: int) -> str:
        with self.lock:
            data=self.read();self._revision(data,revision)
            draft=data["drafts"].get(process)
            if not draft: raise DraftError("没有已保存草稿可导出")
            return ("# Flash Monitor 草稿 — 未应用，不是完整配置文件\n"
                    "# 人工核对实际进程名、目标策略组及规则顺序后，在 FlClash 支持的规则/覆写编辑器中添加。\n"
                    "# 不要用本片段替换完整配置；当前版本的具体导入步骤尚未验证。\n"
                    "# 保存到 FlClash 并由其应用后，验证新连接；此导出不会改网络或断开连接。\n"
                    "# 跟随原规则仅删除 Monitor 草稿，不会删除 FlClash 中已有规则。\n"
                    +snippet(draft["exe"],draft["target"]))
