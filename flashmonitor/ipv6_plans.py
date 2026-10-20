"""Read-only FlClash linkage and scoped IPv6 rejection change previews. Never applies."""
from __future__ import annotations
import hashlib
import json
import re
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from .ipv4_drafts import normalize_subject, subject_id as single_subject_id
from .rule_drafts import DraftError

LIMITS = [
    '未应用：只拒绝经过核心且已识别为 IPv6 目标的选中对象；其余流量保留原分流。',
    '不保证自动回退 IPv4；不会限制代理服务器在远端解析域名后的地址族。',
    '使用 no-resolve，不为拒绝规则额外触发 DNS；未解析域名、fake-ip 和域名不可见可能不命中。',
    '已有连接不会自动断开或重建；只在后续新连接重新匹配规则时判断。',
    '进程身份或目标域名无法识别时不能保证命中；规则生成不等于覆盖所有应用连接。',
    '应用后需等待 /rules 确认新规则已发布，再验证新连接；配置更新返回成功不等于规则已发布。',
    '不启用或更改全局 IPv6、TUN、系统代理、DNS、防火墙或网卡设置。',
]


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def normalize_ipv6_subject(subject):
    if isinstance(subject,dict) and subject.get('kind')=='selection':
        if set(subject)!={'kind','subjects'} or not isinstance(subject['subjects'],list) or not 1<=len(subject['subjects'])<=32:
            raise DraftError('请选择1至32个应用或网站')
        rows={single_subject_id(s):s for s in map(normalize_subject,subject['subjects'])}
        ordered=[rows[k] for k in sorted(rows)]
        return ordered[0] if len(ordered)==1 else {'kind':'selection','subjects':ordered}
    return normalize_subject(subject)


def subjects(subject):return subject['subjects'] if subject['kind']=='selection' else [subject]
def subject_id(subject):
    s=normalize_ipv6_subject(subject)
    return digest({'kind':'selection','subjects':[{**x,'value':x['value'].casefold()} for x in s['subjects']]})[:24] if s['kind']=='selection' else single_subject_id(s)
def subject_label(subject):return '、'.join(s['value'] for s in subjects(subject))


def schema_compatible(conn):
    if conn.execute('PRAGMA user_version').fetchone()[0]!=3:return False
    for table,typ in [('profiles','INTEGER'),('rules','INTEGER'),('profile_rule_mapping','TEXT')]:
        fields={r[1]:r for r in conn.execute('PRAGMA table_info('+table+')')}
        if 'id' not in fields or fields['id'][2].upper()!=typ or fields['id'][5]!=1:return False
    fks={(r[3],r[2],r[4],r[6]) for r in conn.execute('PRAGMA foreign_key_list(profile_rule_mapping)')}
    if not {('profile_id','profiles','id','CASCADE'),('rule_id','rules','id','CASCADE')}<=fks:return False
    for key,p,r,s in conn.execute('SELECT id,profile_id,rule_id,scene FROM profile_rule_mapping'):
        expected=str(r) if p is None and s is None else f'{p}_{r}_{s}'
        if key!=expected:return False
    return True


def reject_rule(subject):
    s=normalize_ipv6_subject(subject)
    if s['kind']=='selection':
        parts=[reject_rule(row)[len('AND,('):-len(',(IP-CIDR6,::/0,no-resolve)),REJECT')] for row in s['subjects']]
        return 'AND,((OR,('+','.join(parts)+')),(IP-CIDR6,::/0,no-resolve)),REJECT'
    value=s['value']
    # Logic payloads interpret parentheses. Balanced path parentheses are supported;
    # reject ambiguous unbalanced names rather than widening to a process-name rule.
    depth=0
    for char in value:
        if char=='(':depth+=1
        elif char==')':depth-=1
        if depth<0:raise DraftError('应用路径括号不成对，无法可靠生成组合规则')
    if depth:raise DraftError('应用路径括号不成对，无法可靠生成组合规则')
    typ={'process_path':'PROCESS-PATH','process_name':'PROCESS-NAME','domain':'DOMAIN-SUFFIX' if s['include_subdomains'] else 'DOMAIN'}[s['kind']]
    return f'AND,(({typ},{value}),(IP-CIDR6,::/0,no-resolve)),REJECT'


def merge_rules(original, rule, position=0):
    """Pure preview transform; own only newly inserted occurrences for precise undo."""
    if not isinstance(original,list) or not all(isinstance(r,str) for r in original):
        raise DraftError('原规则列表无效')
    if type(position) is not int or not 0<=position<=len(original):raise DraftError('规则插入位置无效')
    if rule in original:return list(original),[]
    return original[:position]+[rule]+original[position:],[{'position':position,'rule':rule}]


def undo_rules(current, inserted):
    out=list(current)
    for item in reversed(inserted):
        pos=item['position']
        if pos>=len(out) or out[pos]!=item['rule']:
            raise DraftError('规则已被移动或修改；拒绝按文本删除，需重新预览撤销')
        out.pop(pos)
    return out


def inspect_flclash(root):
    """Whitelist only; URLs, proxy passwords, secrets and subscription labels never escape."""
    root=Path(root)
    try:
        prefs_path=root/'shared_preferences.json'
        if prefs_path.stat().st_size>1024*1024:raise ValueError()
        state=json.loads(json.loads(prefs_path.read_text(encoding='utf-8'))['flutter.config'])
        profile=state['currentProfileId']
        if type(profile) is not int or profile<=0:raise ValueError()
        with closing(sqlite3.connect((root/'database.sqlite').as_uri()+'?mode=ro',uri=True,timeout=1)) as conn:
            conn.execute('PRAGMA query_only=ON')
            if not schema_compatible(conn):raise ValueError('unsupported schema')
            row=conn.execute('SELECT overwrite_type,script_id FROM profiles WHERE id=?',(profile,)).fetchone()
            if not row:raise ValueError()
            links=conn.execute('SELECT id,rule_id,scene,"order" FROM profile_rule_mapping WHERE profile_id=? OR profile_id IS NULL ORDER BY profile_id IS NULL,"order"',(profile,)).fetchall()
            added=conn.execute('SELECT r.id,r.rule_action,r.content,r.rule_target,r.rule_provider,r.sub_rule,r.no_resolve,r.src,m.profile_id,m.scene,m."order" FROM rules r JOIN profile_rule_mapping m ON r.id=m.rule_id WHERE m.profile_id=? OR m.profile_id IS NULL',(profile,)).fetchall()
        effective_path=root/'config.yaml';source_path=root/'profiles'/f'{profile}.yaml'
        if max(effective_path.stat().st_size,source_path.stat().st_size)>8*1024*1024:raise ValueError()
        effective=effective_path.read_bytes();source=source_path.read_bytes()
        text=effective.decode('utf-8');dns={}
        match=re.search(r'^dns:\s*\n((?:[ \t].*\n|\s*\n)*)',text,re.M)
        if match:
            for key in ['enable','ipv6','enhanced-mode']:
                hit=re.search(r'^  '+key+r':\s*([^\r\n]+)',match[1],re.M)
                if hit:
                    val=hit[1].strip().strip('"\'')
                    if val in {'true','false','fake-ip','redir-host','normal'}:dns[key]= {'true':True,'false':False}.get(val,val)
        return {'available':True,'schema_verified':True,'schema_version':3,'target_root':str(root.resolve()),'profile_id':str(profile),'overwrite_type':row[0],
                'has_script':row[1] is not None,'added_rules':sum(x[2]=='added' for x in links),
                'dns':dns,'source_revision':digest([profile,row,links,added,hashlib.sha256(effective).hexdigest(),hashlib.sha256(source).hexdigest()]),
                'persistence':'profile-added-rules' if row[0]=='standard' else 'unsupported-overwrite-chain'}
    except (OSError,ValueError,KeyError,TypeError,sqlite3.Error):
        return {'available':False,'error':'无法只读核实 FlClash 当前配置与持久覆写；未修改任何文件。'}


def build_plan(subject, core, persistent):
    s=normalize_ipv6_subject(subject);rule=reject_rule(s);warnings=list(LIMITS);blockers=[]
    if not core.get('available'):blockers.append('核心版本/完整规则状态未核实')
    elif core.get('meta') is not True:blockers.append('当前核心未确认支持 Meta 组合规则')
    if core.get('mode')!='rule':blockers.append('核心不是规则模式，当前规则不会可靠执行')
    if any(x['kind'].startswith('process') for x in subjects(s)) and core.get('find_process_mode') not in {'strict','always'}:blockers.append('核心进程识别未开启或未核实')
    if not persistent.get('available'):blockers.append(persistent.get('error','持久覆写未核实'))
    elif persistent.get('persistence')!='profile-added-rules':blockers.append('当前使用脚本/自定义覆写；不能覆盖原覆写链')
    if core.get('ipv6') is False:warnings.append('当前核心全局 IPv6 已关闭，本方案不会开启它，也不能据此声称新增拒绝已生效。')
    if any(x['kind']=='process_name' for x in subjects(s)):warnings.append('按文件名匹配影响所有同名程序，不限某一路径。')
    if persistent.get('dns',{}).get('enhanced-mode')=='fake-ip':warnings.append('当前 DNS 使用 fake-ip；IPv4 假地址或未解析真实 IPv6 的域名不满足此拒绝条件。')
    return {'format':'FlashMonitor scoped IPv6 rejection preview','schema_version':2,'applied':False,
            'id':subject_id(s),'subject':s,'rule':rule,'route':'preserve','core':core,'persistent':persistent,
            'placement':'当前配置的附加规则首位；保留所有旧规则及其相对顺序',
            'change':{'rule_action':'AND','content':rule[len('AND,'):-len(',REJECT')],'rule_target':'REJECT',
                      'scene':'added','profile_id':persistent.get('profile_id'),'position':0},
            'rollback':'只删除本次新增规则的对应映射/独立行；原已存在同文规则不归本方案所有；配置或顺序变化必须重新预览。',
            'subscription_update':'应存为该配置的独立附加规则，订阅刷新后重新编译；更换配置不会自动扩展范围。直接编辑生成的 config.yaml 会被重建覆盖。',
            'warnings':warnings,'blockers':blockers,'requires_scope_confirmation':True,'can_apply':False}


class IPv6Plans:
    def __init__(self,path):self.path=Path(path);self.lock=threading.RLock()
    def read(self):
        if not self.path.exists():return {'revision':0,'plans':{}}
        try:
            if self.path.stat().st_size>2*1024*1024:raise ValueError()
            data=json.loads(self.path.read_text(encoding='utf-8'))
            if set(data)!={'revision','plans'} or type(data['revision']) is not int or data['revision']<0 or not isinstance(data['plans'],dict) or len(data['plans'])>256:raise ValueError()
            for key,p in data['plans'].items():
                if key!=subject_id(normalize_ipv6_subject(p['subject'])) or p['applied'] is not False or p['can_apply'] is not False or p['rule']!=reject_rule(p['subject']):raise ValueError()
            return data
        except (OSError,ValueError,TypeError,KeyError) as exc:raise DraftError('IPv6 方案文件不可读，原文件保留') from exc
    def catalog(self):
        with self.lock:
            data=self.read();return {'revision':data['revision'],'plans':list(data['plans'].values()),'applied':False}
    def operate(self,body,plan=None):
        with self.lock:
            data=self.read()
            if type(body.get('revision')) is not int or body['revision']!=data['revision']:raise DraftError('方案已变化，请刷新后重试')
            op=body.get('operation')
            if op=='preview' and plan:return {**plan,'revision':data['revision']}
            if op=='save' and plan:
                if body.get('fingerprint')!=digest(plan):raise DraftError('核心/配置/对象已变化，需重新预览；未保存或应用')
                if plan['id'] not in data['plans'] and len(data['plans'])>=256:raise DraftError('最多保存256个方案')
                data['plans'][plan['id']]=plan
            elif op in {'delete','export'}:
                key=body.get('id')
                if not isinstance(key,str) or key not in data['plans']:raise DraftError('方案不存在')
                if op=='export':return {'text':json.dumps(data['plans'][key],ensure_ascii=False,indent=2),'applied':False}
                del data['plans'][key]
            else:raise DraftError('只支持预览、保存待确认方案、导出和删除；未开放实际应用')
            data['revision']+=1;tmp=self.path.with_suffix('.tmp')
            text=json.dumps(data,ensure_ascii=False,indent=2)
            if len(text.encode('utf-8'))>2*1024*1024:raise DraftError('方案文件达到2 MiB上限，未保存；请删除不需要的本地方案')
            try:
                self.path.parent.mkdir(parents=True,exist_ok=True)
                tmp.write_text(text,encoding='utf-8');tmp.replace(self.path)
            except OSError as exc:raise DraftError('方案保存失败，原文件保留') from exc
            return self.catalog()
