"""Scoped persistent IPv6 changes. No writes/reloads without an exact local consent."""
from __future__ import annotations
import json
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from urllib.request import Request
from .ipv6_plans import digest, inspect_flclash, reject_rule
from .rule_drafts import DraftError


def key_before(key):
    if key is None:return 'a0'
    digits='0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
    head=key[:1]
    if not head or head not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz':raise DraftError('附加规则顺序格式未知')
    size=ord(head)-ord('a')+2 if head>='a' else ord('Z')-ord(head)+2
    integer=key[:size]
    if len(integer)!=size or any(c not in digits for c in integer[1:]):raise DraftError('附加规则顺序格式未知')
    chars=list(integer[1:]);i=len(chars)-1
    while i>=0 and chars[i]=='0':chars[i]='z';i-=1
    if i>=0:chars[i]=digits[digits.index(chars[i])-1];return head+''.join(chars)
    if head=='a':return 'Zz'
    if head=='A':raise DraftError('规则顺序已达边界，需要在 FlClash 中重新排序')
    prev=chr(ord(head)-1)
    return prev+'z'*(size-2 if head>'a' else size)


def transform_document(text,rule,remove=False):
    """Modify only the rule list. Reject ambiguous YAML instead of rewriting other settings."""
    if text.lstrip().startswith('{'):
        data=json.loads(text);rules=data.get('rules')
        if not isinstance(rules,list):raise DraftError('配置规则列表无效')
        count=rules.count(rule)
        if remove:
            if count>1:raise DraftError('出现重复同文规则，无法确定撤销所有权')
            if count:rules.remove(rule)
        elif not count:rules.insert(0,rule)
        return json.dumps(data,ensure_ascii=False,indent=2)
    lines=text.splitlines(keepends=True);starts=[i for i,line in enumerate(lines) if line.rstrip()=='rules:']
    if len(starts)!=1:raise DraftError('只支持生成配置的独立 rules 列表；未知YAML结构不改写')
    start=starts[0]+1;end=start
    while end<len(lines) and (lines[end][:1].isspace() or not lines[end].strip() or lines[end].lstrip().startswith('#')):end+=1
    found=[]
    for i in range(start,end):
        value=lines[i].strip()
        if not value.startswith('- '):continue
        value=value[2:].strip()
        if value.startswith('"'):
            try:value=json.loads(value)
            except ValueError:raise DraftError('规则字符串格式未知')
        elif value.startswith("'") and value.endswith("'"):value=value[1:-1].replace("''", "'")
        if value==rule:found.append(i)
    if remove:
        if len(found)>1:raise DraftError('出现重复同文规则，无法确定撤销所有权')
        if found:lines.pop(found[0])
    elif not found:lines.insert(start,'  - '+json.dumps(rule,ensure_ascii=False)+'\n')
    return ''.join(lines)


class ControllerReload:
    def __init__(self,client):self.client=client;self.base=client.base_urls()[0]
    def snapshot(self):
        if self.client.base_urls()[0]!=self.base:raise DraftError('控制器地址变化，停止操作')
        return self.client.ipv6_inventory()
    def reload(self,path):
        if self.client.base_urls()[0]!=self.base:raise DraftError('控制器地址变化，拒绝重载')
        req=Request(self.base+'/configs?force=false',data=json.dumps({'path':str(path)}).encode(),
                    headers={**self.client.headers(),'Content-Type':'application/json'},method='PUT')
        with self.client.opener.open(req,timeout=5) as resp:resp.read(4096)
    def contains(self,rule):
        s=rule[len('AND,(('):].split('),(IP-CIDR6',1)[0]
        typ,value=s.split(',',1)
        aliases={'PROCESS-PATH':'ProcessPath','PROCESS-NAME':'ProcessName','DOMAIN':'Domain','DOMAIN-SUFFIX':'DomainSuffix'}
        expected=f'(({aliases[typ]},{value})&&(IPCIDR,::/0))'.casefold()
        data=self.client._get_json(self.base+'/rules',2)
        return any(r.get('type')=='AND' and r.get('proxy')=='REJECT' and
                   str(r.get('payload','')).replace(' && ','&&').casefold()==expected for r in data.get('rules',[]))
    def runtime_rules(self):
        if self.client.base_urls()[0]!=self.base:raise DraftError('控制器地址变化')
        return self.client._get_json(self.base+'/rules',2)['rules']
    def wait(self,rule,present,timeout=5):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if self.contains(rule)==present:return
            time.sleep(.05)
        raise DraftError('重载后规则发布未确认')


class IPv6Executor:
    _root_locks={}
    _lock_guard=threading.Lock()
    def __init__(self,root,backup_dir,controller,consent=None,*,experimental=False):
        self.root=Path(root).resolve();self.backups=Path(backup_dir);self.controller=controller
        self.consent=consent;self.experimental=experimental
        with self._lock_guard:self.lock=self._root_locks.setdefault(str(self.root).casefold(),threading.RLock())
    def _grant(self,plan):
        if not self.experimental:raise DraftError('unsupported_client_adapter：外部SQL/核心PUT仅限隔离实验，真实环境请走FlClash原生附加规则保存与编译')
        grant=self.consent() if callable(self.consent) else self.consent
        if not isinstance(grant,dict) or grant.get('target_root')!=str(self.root) or grant.get('fingerprint')!=digest(plan) or str(grant.get('profile_id'))!=str(plan['persistent'].get('profile_id')) or grant.get('subject_id')!=plan['id'] or grant.get('allow_reload') is not True:
            raise DraftError('尚未确认此应用/网站、目标配置及重载影响；没有执行任何写入')
        if plan.get('blockers'):raise DraftError('方案仍有未解决的执行阻碍')
        if plan.get('rule')!=reject_rule(plan['subject']):raise DraftError('方案规则不匹配')
    def _path(self,key):
        if not isinstance(key,str) or len(key)!=24 or any(c not in '0123456789abcdef' for c in key):raise DraftError('方案标识无效')
        return self.backups/(key+'.json')
    def _read(self,key):
        p=self._path(key)
        return json.loads(p.read_text(encoding='utf-8')) if p.exists() else None
    def _save(self,record):
        self.backups.mkdir(parents=True,exist_ok=True);p=self._path(record['id']);tmp=p.with_suffix('.tmp')
        tmp.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8');tmp.replace(p)
    def _db(self):
        conn=sqlite3.connect((self.root/'database.sqlite').as_uri()+'?mode=rw',uri=True,timeout=1,isolation_level=None)
        conn.execute('PRAGMA foreign_keys=ON');return conn
    def eligible(self,plan):
        try:self._grant(plan);return True
        except DraftError:return False
    def _owned(self,conn,record):
        # Do not coerce an old integer mapping ID into a claimed client-owned key.
        # The canonical TEXT key and the whole persisted tuple must both match.
        mapping=record.get('mapping',[])
        if len(mapping)!=5 or type(record.get('rule_id')) is not int:return False
        expected_key=f"{record.get('profile_id')}_{record['rule_id']}_added"
        if (type(record.get('mapping_id')) is not str or record['mapping_id']!=expected_key or
                mapping[0]!=expected_key or type(mapping[1]) is not int or
                str(mapping[1])!=str(record.get('profile_id')) or
                mapping[2]!=record['rule_id'] or mapping[3]!='added'):return False
        expected=tuple(record['row']);row=conn.execute('SELECT id,rule_action,content,rule_target,rule_provider,sub_rule,no_resolve,src FROM rules WHERE id=?',(record['rule_id'],)).fetchone()
        link=conn.execute('SELECT id,profile_id,rule_id,scene,"order" FROM profile_rule_mapping WHERE id=?',(record['mapping_id'],)).fetchone()
        return row==expected and link==tuple(record['mapping'])
    def _scope(self,record,plan=None):
        if record.get('target_root')!=str(self.root):raise DraftError('操作记录目标目录不匹配，停止恢复')
        if plan and (str(record.get('profile_id'))!=str(plan['persistent'].get('profile_id')) or record.get('rule')!=plan.get('rule')):
            raise DraftError('操作记录配置或对象不匹配，停止恢复')
    def status(self,key):
        record=self._read(key)
        if not record:return {'state':'not_applied','applied':False}
        self._scope(record)
        state=record['state']
        if state=='applied':
            with closing(self._db()) as conn:
                if not self._owned(conn,record):state='persistence_conflict'
            if state=='applied' and inspect_flclash(self.root).get('source_revision')!=record.get('persistent_after'):state='configuration_changed'
            if state=='applied' and self.controller.snapshot()!=record.get('runtime_after'):state='configuration_changed'
            if state=='applied' and not self.controller.contains(record['rule']):state='needs_reload'
        return {'state':state,'applied':state=='applied','record':key}
    def apply(self,plan):
        with self.lock:
            self._grant(plan);old=self._read(plan['id'])
            if old:self._scope(old,plan)
            if old and old['state']=='applied':
                with closing(self._db()) as conn:
                    if not self._owned(conn,old):raise DraftError('已应用规则被外部修改，停止重复应用')
                if self.status(plan['id'])['state']=='applied':return {'applied':True,'duplicate':True}
                raise DraftError('持久规则存在但运行规则已变化；需重新核对配置')
            if old and old['state'] not in {'rolled_back','revoked'}:raise DraftError('存在未完成操作，先恢复或核对冲突')
            if self.backups.exists():
                for p in self.backups.glob('*.json'):
                    record=json.loads(p.read_text(encoding='utf-8'))
                    if record.get('id')!=plan['id'] and record.get('state') not in {'rolled_back','revoked'}:
                        raise DraftError('实验后端同一目标目录只允许一个活动或未完成方案；第二方案未备份、写入或重载')
            current=inspect_flclash(self.root)
            if not current.get('available') or current.get('source_revision')!=plan['persistent'].get('source_revision') or self.controller.snapshot()!=plan['core']:raise DraftError('配置/订阅/运行核心已变化，需重新预览确认')
            self.backups.mkdir(parents=True,exist_ok=True);operation=uuid.uuid4().hex
            cfg=self.root/'config.yaml';text=cfg.read_text(encoding='utf-8')
            candidate=transform_document(text,plan['rule'])
            backup=self.backups/(operation+'.yaml');backup.write_text(text,encoding='utf-8')
            dbbackup=self.backups/(operation+'.sqlite')
            source=sqlite3.connect(self.root/'database.sqlite');target=sqlite3.connect(dbbackup)
            try:source.backup(target)
            finally:source.close();target.close()
            record={'id':plan['id'],'rule':plan['rule'],'profile_id':current['profile_id'],'target_root':str(self.root),'state':'prepared',
                    'backup_config':str(backup),'backup_database':str(dbbackup),'operation':operation,'base_config_hash':digest(text),'restore_hash':digest(text)}
            record['core_before']=plan['core']
            conn=self._db()
            try:
                conn.execute('BEGIN IMMEDIATE')
                if inspect_flclash(self.root).get('source_revision')!=current['source_revision']:raise DraftError('备份期间配置发生变化')
                duplicate=conn.execute('SELECT r.id FROM rules r JOIN profile_rule_mapping m ON r.id=m.rule_id WHERE r.rule_action="AND" AND r.content=? AND r.rule_target="REJECT" AND m.profile_id=? AND m.scene="added"',(plan['change']['content'],current['profile_id'])).fetchone()
                if duplicate:raise DraftError('已有同文规则不归本工具所有，不重复写入或接管')
                first=conn.execute('SELECT "order" FROM profile_rule_mapping WHERE profile_id=? AND scene="added" ORDER BY "order" LIMIT 1',(current['profile_id'],)).fetchone()
                if first and first[0] is None:raise DraftError('已有规则缺少顺序，不能保证首位插入')
                rule_id=conn.execute('SELECT coalesce(max(id),0)+1 FROM rules').fetchone()[0]
                link_id=f"{current['profile_id']}_{rule_id}_added"
                row=[rule_id,'AND',plan['change']['content'],'REJECT',None,None,0,0]
                mapping=[link_id,int(current['profile_id']),rule_id,'added',key_before(first[0] if first else None)]
                record.update(rule_id=rule_id,mapping_id=link_id,row=row,mapping=mapping)
                self._save(record)
                conn.execute('INSERT INTO rules(id,rule_action,content,rule_target,rule_provider,sub_rule,no_resolve,src) VALUES(?,?,?,?,?,?,?,?)',row)
                conn.execute('INSERT INTO profile_rule_mapping(id,profile_id,rule_id,scene,"order") VALUES(?,?,?,?,?)',mapping)
                conn.commit();record['state']='persisted';self._save(record)
                self._reload(record,candidate)
                record['state']='applied';record['persistent_after']=inspect_flclash(self.root)['source_revision'];self._save(record);return {'applied':True,'record':record['id']}
            except Exception as exc:
                if conn.in_transaction:conn.rollback()
                if record.get('rule_id'):
                    try:self._rollback(record,text)
                    except Exception as recovery:
                        if record['state']!='persistent_removed_runtime_pending':record['state']='recovery_required'
                        record['error']=str(recovery);self._save(record)
                        raise DraftError('操作失败且恢复未确认；保留备份，禁止继续覆盖：'+str(recovery)) from exc
                raise DraftError('应用失败，已撤回本次变更：'+str(exc)) from exc
            finally:conn.close()
    def _reload(self,record,text):
        if digest((self.root/'config.yaml').read_text(encoding='utf-8'))!=record['base_config_hash']:raise DraftError('重载前生成配置变化，停止覆盖')
        if self.controller.snapshot()!=record['core_before']:raise DraftError('重载前运行配置变化，停止覆盖')
        path=self.root/('flashmonitor-ipv6-'+record['operation']+'.yaml')
        path.write_text(text,encoding='utf-8');record['candidate']=str(path);record['state']='reloading';self._save(record)
        record['candidate_hash']=digest(text);record['expected_runtime']=record['core_before'];self._save(record)
        self.controller.reload(path);record['expected_runtime']=self.controller.snapshot();self._save(record)
        self.controller.wait(record['rule'],True)
        record['runtime_after']=self.controller.snapshot()
    def _rollback(self,record,text):
        self._scope(record)
        if digest(text)!=record.get('restore_hash'):raise DraftError('备份校验不匹配，停止恢复')
        if digest((self.root/'config.yaml').read_text(encoding='utf-8'))!=record['base_config_hash']:raise DraftError('原配置变化，拒绝恢复旧快照')
        if self.controller.snapshot()!=record.get('runtime_after',record.get('expected_runtime',record['core_before'])):raise DraftError('运行规则或设置被外部修改，停止差量撤销')
        restore_text=text
        if record.get('candidate'):
            candidate=Path(record['candidate']).read_text(encoding='utf-8')
            if digest(candidate)!=record.get('candidate_hash'):raise DraftError('托管候选校验不匹配')
            restore_text=transform_document(candidate,record['rule'],remove=True)
        conn=self._db()
        try:
            conn.execute('BEGIN IMMEDIATE')
            if self._owned(conn,record):
                if conn.execute('SELECT count(*) FROM profile_rule_mapping WHERE rule_id=?',(record['rule_id'],)).fetchone()[0]!=1:raise DraftError('规则有新的外部引用，不能删除')
                conn.execute('DELETE FROM profile_rule_mapping WHERE id=?',(record['mapping_id'],));conn.execute('DELETE FROM rules WHERE id=?',(record['rule_id'],))
            elif (conn.execute('SELECT 1 FROM rules WHERE id=?',(record['rule_id'],)).fetchone() or
                  conn.execute('SELECT 1 FROM profile_rule_mapping WHERE id=? OR rule_id=?',(record['mapping_id'],record['rule_id'])).fetchone()):
                raise DraftError('本次规则或映射已被外部修改，停止恢复')
            conn.commit()
        finally:conn.close()
        record['state']='persistent_removed_runtime_pending';self._save(record)
        if record.get('candidate'):
            if digest((self.root/'config.yaml').read_text(encoding='utf-8'))!=record['base_config_hash']:raise DraftError('原配置变化，不盲目重载旧备份')
            now=self.controller.snapshot()
            if any(now.get(k)!=record['core_before'].get(k) for k in ['version','meta','mode','ipv6','find_process_mode','tun_enabled','controller_revision']):raise DraftError('网络设置被外部修改，不覆盖这些设置')
            restore=self.root/('flashmonitor-restore-'+record['operation']+'.yaml');restore.write_text(restore_text,encoding='utf-8')
            self.controller.reload(restore)
            record['runtime_after']=self.controller.snapshot();self._save(record)
            self.controller.wait(record['rule'],False)
        record['state']='rolled_back';self._save(record)
    def revoke(self,plan):
        with self.lock:
            self._grant(plan);record=self._read(plan['id'])
            if record:self._scope(record,plan)
            if not record or record['state'] in {'revoked','rolled_back'}:return {'applied':False,'duplicate':True}
            if record['state']!='applied':raise DraftError('操作状态未确认，需要恢复')
            if inspect_flclash(self.root).get('source_revision')!=record.get('persistent_after'):raise DraftError('订阅/持久配置变化，需重新预览撤销')
            if record.get('runtime_after')!=self.controller.snapshot():raise DraftError('运行配置已变化，需重新预览撤销')
            self._rollback(record,Path(record['backup_config']).read_text(encoding='utf-8'))
            record['state']='revoked';self._save(record);return {'applied':False,'state':'revoked'}
    def reconcile(self,plan):
        """Explicitly reapproved subscription refresh; never runs automatically."""
        with self.lock:
            self._grant(plan);record=self._read(plan['id'])
            if record:self._scope(record,plan)
            if not record or record['state']!='applied':raise DraftError('没有可重新核对的已应用规则')
            with closing(self._db()) as conn:
                if not self._owned(conn,record):raise DraftError('持久规则所有权变化')
            current=inspect_flclash(self.root)
            if current.get('source_revision')!=plan['persistent'].get('source_revision') or self.controller.snapshot()!=plan['core']:raise DraftError('配置再次变化，需要重新确认')
            text=(self.root/'config.yaml').read_text(encoding='utf-8')
            original=transform_document(text,record['rule'],remove=True)
            operation=uuid.uuid4().hex;backup=self.backups/(operation+'.yaml');backup.write_text(original,encoding='utf-8')
            record.update(operation=operation,backup_config=str(backup),base_config_hash=digest(text),restore_hash=digest(original),core_before=plan['core'],state='persisted')
            for key in ('candidate','candidate_hash','runtime_after','expected_runtime'):record.pop(key,None)
            self._save(record)
            try:
                self._reload(record,transform_document(text,record['rule']))
                record['state']='applied';record['persistent_after']=current['source_revision'];self._save(record)
                return {'applied':True,'state':'applied'}
            except Exception as exc:
                try:self._rollback(record,original)
                except Exception as recovery:
                    if record['state']!='persistent_removed_runtime_pending':record['state']='recovery_required'
                    record['error']=str(recovery);self._save(record)
                    raise DraftError('重新核对失败且恢复未确认：'+str(recovery)) from exc
                raise DraftError('重新核对失败，已撤回本次规则：'+str(exc)) from exc
    def recover(self,plan):
        with self.lock:
            self._grant(plan);record=self._read(plan['id'])
            if record:self._scope(record,plan)
            if not record:return {'applied':False,'state':'not_applied'}
            if record['state'] not in {'prepared','persisted','reloading','recovery_required','persistent_removed_runtime_pending'}:raise DraftError('当前没有未完成操作')
            self._rollback(record,Path(record['backup_config']).read_text(encoding='utf-8'))
            return {'applied':False,'state':'rolled_back'}
