"""FlClash native-save assistance. Reads client DB/config/core; never submits changes."""
from __future__ import annotations
import ipaddress
import json
import re
import sqlite3
import time
import uuid
import threading
from functools import wraps
from contextlib import closing
from pathlib import Path
from .ipv6_plans import digest, inspect_flclash, subjects
from .rule_drafts import DraftError


def document_rules(text):
    if text.lstrip().startswith('{'):
        rows=json.loads(text).get('rules')
        if not isinstance(rows,list) or not all(isinstance(r,str) for r in rows):raise DraftError('生成规则格式未知')
        return rows
    lines=text.splitlines();starts=[i for i,l in enumerate(lines) if l.rstrip()=='rules:']
    if len(starts)!=1:raise DraftError('生成规则格式未知')
    out=[]
    for line in lines[starts[0]+1:]:
        if line.strip() and not line[:1].isspace() and not line.startswith('- ') and not line.startswith('#'):break
        value=line.strip()
        if not value or value.startswith('#'):continue
        if not value.startswith('- '):raise DraftError('生成规则包含未知结构')
        value=value[2:].strip()
        if value.startswith('"'):value=json.loads(value)
        elif value.startswith("'") and value.endswith("'"):value=value[1:-1].replace("''","'")
        out.append(value)
    return out


def canonical_rule(rule):
    parts=rule.split(',');typ=parts[0].strip().upper()
    if typ=='AND':
        m=re.fullmatch(r'AND,\((.+),\(IP-CIDR6,::/0,no-resolve\)\),([^,]+)',rule)
        if not m:raise DraftError('未知复合规则，不能自动核实生成/运行顺序')
        aliases={'PROCESS-PATH':'ProcessPath','PROCESS-NAME':'ProcessName','DOMAIN':'Domain','DOMAIN-SUFFIX':'DomainSuffix'}
        scope=m[1];group=scope.startswith('(OR,(') and scope.endswith('))')
        text=scope[5:-2] if group else scope
        matches=list(re.finditer(r'\((PROCESS-PATH|PROCESS-NAME|DOMAIN|DOMAIN-SUFFIX),(.+?)\)(?=,\(|$)',text))
        if not matches or ','.join(x[0] for x in matches)!=text:raise DraftError('未知对象组合，不能核实')
        nodes=[f'({aliases[x[1]]},{x[2]})' for x in matches]
        expression='(OR,('+'||'.join(nodes)+'))' if group else nodes[0]
        return ('and',f'({expression}&&(IPCIDR,::/0))'.casefold(),m[2])
    if typ in {'OR','NOT','SUB-RULE'}:raise DraftError('未知复合规则，需人工核对')
    if typ=='MATCH':return ('match','',parts[1].strip())
    if len(parts)<3:raise DraftError('规则格式未知')
    payload=parts[1].strip();name=typ.replace('-','').casefold()
    if typ in {'IP-CIDR','IP-CIDR6'}:name='ipcidr';payload=str(ipaddress.ip_network(payload,strict=False))
    return (name,payload.casefold(),parts[2].strip())


def canonical_runtime(row):
    return (str(row.get('type','')).replace('-','').casefold(),str(row.get('payload','')).replace(' && ','&&').replace(' || ','||').casefold(),str(row.get('proxy','')))


def serialized(method):
    @wraps(method)
    def run(self,*args,**kwargs):
        with self.lock:return method(self,*args,**kwargs)
    return run


class NativeVerifier:
    _locks={}
    _guard=threading.Lock()
    def __init__(self,root,ledger,controller):
        self.root=Path(root).resolve();self.ledger=Path(ledger);self.controller=controller
        with self._guard:self.lock=self._locks.setdefault(str(self.root).casefold(),threading.RLock())
    def _path(self,plan):return self.ledger/(digest([str(self.root),str(plan['persistent']['profile_id']),plan['id']])[:24]+'.json')
    def _read(self,plan):
        p=self._path(plan)
        return json.loads(p.read_text(encoding='utf-8')) if p.exists() else None
    def _save(self,plan,r):
        self.ledger.mkdir(parents=True,exist_ok=True);p=self._path(plan);tmp=p.with_suffix('.tmp')
        tmp.write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8');tmp.replace(p)
    def _rows(self,profile):
        with closing(sqlite3.connect((self.root/'database.sqlite').as_uri()+'?mode=ro',uri=True)) as c:
            c.execute('PRAGMA query_only=ON')
            rows=c.execute('SELECT m.id,m.profile_id,r.id,m.scene,m."order",r.rule_action,r.content,r.rule_target,r.no_resolve,r.src,r.rule_provider,r.sub_rule FROM rules r JOIN profile_rule_mapping m ON m.rule_id=r.id WHERE (m.profile_id=? AND m.scene="added" OR m.profile_id IS NULL AND m.scene IS NULL) AND r.id NOT IN (SELECT rule_id FROM profile_rule_mapping WHERE profile_id=? AND scene="disabled") ORDER BY m.profile_id IS NULL,m."order"',(profile,profile)).fetchall()
        return rows
    def _match(self,plan,rows):
        payload=plan['change']['content']
        return [r for r in rows if r[1] is not None and str(r[1])==str(plan['persistent']['profile_id']) and r[3]=='added' and r[5]=='AND' and r[6]==payload and r[7]=='REJECT' and not r[8] and not r[9] and r[10] is None and r[11] is None]
    def instructions(self,plan):
        return {'adapter':'native_assisted','automatic_submit_supported':False,
                'profile_id':str(plan['persistent']['profile_id']),
                'fields':{'type':'AND','content':plan['change']['content'],'target':'REJECT'},
                'steps':['在 FlClash 进入 配置 → 此配置 → 覆写 → 标准 → 附加规则 → 新增。','选择AND，粘贴内容，目标REJECT；不要选全局规则，内层no-resolve不要变成外层参数。','保存后让Monitor只读核实落盘；查看FlClash预览并退出整个覆写页，由客户端正常编译/应用。','再次核验生成文件与运行规则；实际请求和订阅更新须另外验收。'],
                'warning':'辅助原生流程不等于Monitor已执行应用；真实客户端操作须先确认目标和影响。'}
    @serialized
    def prepare(self,plan,operation_id=None):
        if plan.get('blockers'):raise DraftError('方案仍有需处理的阻碍：'+'；'.join(plan['blockers']))
        if plan['persistent'].get('target_root')!=str(self.root):raise DraftError('方案目标目录不匹配')
        existing=self._read(plan)
        if existing and existing['state']!='revoked_verified':return {**self.instructions(plan),**self.status(plan)}
        if self.controller.snapshot()!=plan['core']:raise DraftError('运行核心已变化，请重新预览保存本地方案')
        if self.ledger.exists():
            for path in self.ledger.glob('*.json'):
                other=json.loads(path.read_text(encoding='utf-8'))
                if path!=self._path(plan) and other.get('target_root')==str(self.root) and other.get('state')!='revoked_verified':
                    raise DraftError('另一个原生方案尚未撤销并核验；本次未开始，请先完成原方案')
        meta=inspect_flclash(self.root)
        if not meta.get('available') or str(meta['profile_id'])!=str(plan['persistent']['profile_id']) or meta['overwrite_type']!='standard':raise DraftError('当前配置/结构不匹配')
        if meta['source_revision']!=plan['persistent']['source_revision']:raise DraftError('配置变化，请重新预览')
        rows=self._rows(meta['profile_id']);matches=self._match(plan,rows)
        if matches:raise DraftError('已有同文客户端规则，不接管；请在FlClash核对原规则')
        r={'subject_id':plan['id'],'profile_id':str(meta['profile_id']),'target_root':str(self.root),'state':'awaiting_client_save',
           'baseline_keys':[row[0] for row in rows],'baseline_hashes':[digest(row) for row in rows],
           'source_revision':meta['source_revision'],'rule':plan['rule'],'operation_id':operation_id or uuid.uuid4().hex,
           'ownership':'client-managed','automatic_revoke':False}
        self._save(plan,r);return {**self.instructions(plan),'state':r['state'],'applied':False}
    def status(self,plan):
        r=self._read(plan)
        return {'state':r['state'] if r else 'not_started','applied':False,
                'checked_at':r.get('checked_at') if r else None,
                'behavior_verified':False,'subscription_refresh_verified':False}
    @serialized
    def forget(self,plan):self._path(plan).unlink(missing_ok=True)
    def _result(self,plan,r,state):
        r['state']=state;r['checked_at']=time.time();self._save(plan,r)
        return {'state':state,'applied':False,'runtime_verified':state=='runtime_verified','ownership':'client-managed',
                'client_rule_id':r.get('client_rule_id'),'mapping_key':r.get('mapping_key'),'automatic_revoke':False,
                'checked_at':r['checked_at'],'behavior_verified':False,'subscription_refresh_verified':False}
    @serialized
    def verify(self,plan):
        r=self._read(plan)
        if not r:raise DraftError('先记录原生操作前的基线')
        if r.get('target_root')!=str(self.root) or r.get('rule')!=plan['rule']:raise DraftError('原生记录作用域不匹配')
        meta=inspect_flclash(self.root)
        if not meta.get('available') or str(meta.get('profile_id'))!=r['profile_id'] or meta.get('overwrite_type')!='standard':return self._result(plan,r,'out_of_scope')
        rows=self._rows(meta['profile_id']);matches=self._match(plan,rows)
        before=self.controller.snapshot()
        generated=document_rules((self.root/'config.yaml').read_text(encoding='utf-8'))
        runtime=self.controller.runtime_rules();facts=self.controller.snapshot()
        after=inspect_flclash(self.root)
        if after.get('source_revision')!=meta['source_revision'] or facts!=before:return self._result(plan,r,'applying')
        other=[row for row in rows if row not in matches]
        if [row[0] for row in other]!=r['baseline_keys'] or [digest(row) for row in other]!=r['baseline_hashes']:return self._result(plan,r,'configuration_changed')
        if (facts.get('available') is not True or facts.get('meta') is not True or facts.get('mode')!='rule' or
                any(x['kind'].startswith('process') for x in subjects(plan['subject'])) and facts.get('find_process_mode') not in {'strict','always'}):
            return self._result(plan,r,'configuration_changed')
        same=[canonical_rule(x) for x in generated]==[canonical_runtime(x) for x in runtime]
        managed=canonical_rule(plan['rule']);runtime_has=managed in [canonical_runtime(x) for x in runtime]
        if not matches:
            removed=bool(r.get('client_rule_id'))
            state='revoked_verified' if removed and plan['rule'] not in generated and not runtime_has and same else 'persistent_removed_runtime_pending' if removed else 'awaiting_client_save'
        elif len(matches)!=1 or matches[0][0] in r['baseline_keys']:
            state='ambiguous_client_rows'
        else:
            if r.get('client_rule_id') and str(matches[0][2])!=r['client_rule_id']:return self._result(plan,r,'configuration_changed')
            row=matches[0]
            if r.get('client_row_hash') and r['client_row_hash']!=digest(row):return self._result(plan,r,'configuration_changed')
            r['client_rule_id']=str(row[2]);r['mapping_key']=row[0];r['client_row_hash']=digest(row)
            if row[0]!=f'{row[1]}_{row[2]}_added':state='configuration_changed'
            elif rows[0]!=row:state='priority_unverified'
            elif plan['rule'] not in generated or not same or not runtime_has:state='persisted_unverified'
            elif generated[0]!=plan['rule']:state='priority_unverified'
            else:state='runtime_verified'
        return self._result(plan,r,state)
    @serialized
    def undo_instructions(self,plan):
        if self.verify(plan)['state']!='runtime_verified':raise DraftError('当前规则或配置尚未一致核实，停止撤销指引；请先核对FlClash')
        r=self._read(plan)
        if not r or not r.get('client_rule_id'):raise DraftError('未唯一核实原生新增规则，不能给出撤销目标')
        rows=self._rows(r['profile_id']);matches=self._match(plan,rows)
        if len(matches)!=1 or str(matches[0][2])!=r['client_rule_id'] or matches[0][0]!=r.get('mapping_key'):raise DraftError('客户端行被修改或归属不明确，停止撤销指引')
        with closing(sqlite3.connect((self.root/'database.sqlite').as_uri()+'?mode=ro',uri=True)) as c:
            if c.execute('SELECT count(*) FROM profile_rule_mapping WHERE rule_id=?',(r['client_rule_id'],)).fetchone()[0]!=1:raise DraftError('规则有外部引用，需人工核对')
        return {'profile_id':r['profile_id'],'rule_id':r['client_rule_id'],'mapping_key':r['mapping_key'],
                'fields':{'type':'AND','content':plan['change']['content'],'target':'REJECT'},
                'automatic_revoke':False,'steps':['在当前配置的附加规则中找到类型AND、目标REJECT且完整内容与下方一致的唯一一条规则，只删除此条；无需查数据库ID。','退出整个覆写页由FlClash重新编译，返回Monitor自动只读核验撤销。']}
