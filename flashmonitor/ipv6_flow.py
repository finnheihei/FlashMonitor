"""Server-bound assisted flow. Writes Monitor data only, never client configuration."""
import os
import threading
import time
import uuid
from .ipv6_plans import build_plan, digest, inspect_flclash, normalize_ipv6_subject, subject_id, subject_label, subjects
from .ipv6_native import canonical_rule, canonical_runtime, document_rules
from .rule_drafts import DraftError


def root_key(value):
    # Match NativeVerifier's resolved root, including Windows 8.3 path aliases.
    return os.path.normcase(os.path.realpath(str(value)))


class IPv6Flow:
    def __init__(self,plans,native,controller,available,executor=None):
        self.plans=plans;self.native=native;self.controller=controller;self.available=available;self.executor=executor
        self.tokens={};self.lock=threading.RLock()
    def resolve(self,body):
        with self.lock:
            if set(body)-{'operation','id','subject'}:raise DraftError('resolve：未知字段')
            data=self.plans.read();old=data['plans'].get(body.get('id')) if isinstance(body.get('id'),str) else None
            if body.get('id') and not old:raise DraftError('方案不存在')
            scope=normalize_ipv6_subject(body.get('subject') if 'subject' in body else old['subject'] if old else None)
            key=subject_id(scope);old=old or data['plans'].get(key)
            if body.get('id') and old['id']!=key:raise DraftError('subject_changed：选择对象与旧方案不同')
            for s in subjects(scope):
                if s['kind']!='domain' and not self.available(s):raise DraftError('subject_unavailable：程序身份当前无法核实；保留原选择，不扩大为同名程序')
            meta=inspect_flclash(self.native.root);core=self.controller.snapshot()
            if not meta.get('available'):raise DraftError('client_unavailable：无法核实当前FlClash配置，保留原方案')
            changes=[]
            if old:
                previous=old.get('persistent',{})
                if previous.get('target_root') and root_key(previous['target_root'])!=root_key(self.native.root):raise DraftError('target_changed：原方案属于其他客户端目录，未迁移')
                if previous.get('profile_id') is not None and str(previous['profile_id'])!=meta['profile_id']:changes.append('profile_changed：当前配置与原方案不同')
                if old.get('core',{}).get('controller_revision') and old['core']['controller_revision']!=core.get('controller_revision'):changes.append('controller_changed：控制器上下文已变化')
                if self.executor:
                    record=self.executor._read(old['id'])
                    if record and record.get('state') not in {'rolled_back','revoked'}:raise DraftError('recovery_required：原实验操作仍待恢复，不能迁移草稿')
                if self.native.status(old)['state'] not in {'not_started','revoked_verified'}:
                    if changes:raise DraftError('profile_changed：活动原生操作须先恢复原配置核验')
                    record=self.native._read(old)
                    if not record.get('target_root') or record.get('rule')!=old['rule']:raise DraftError('recovery_required：原生记录关联不完整，保留原基线')
                    return {'schema_version':2,'capability':'assisted_readonly','resume_id':old['id'],'plan':old,
                            'summary':subject_label(scope),'changes':[],'native':self.native.status(old)}
            plan=build_plan(scope,core,meta)
            if plan['blockers']:raise DraftError('capability_unavailable：'+'；'.join(plan['blockers']))
            generated=document_rules((self.native.root/'config.yaml').read_text(encoding='utf-8'))
            runtime=self.controller.runtime_rules()
            if [canonical_rule(x) for x in generated]!=[canonical_runtime(x) for x in runtime]:raise DraftError('controller_changed：生成配置与当前核心规则不一致，等待客户端应用后重试')
            if inspect_flclash(self.native.root).get('source_revision')!=meta['source_revision'] or self.controller.snapshot()!=core:raise DraftError('applying：客户端正在更新，保留选择并稍后重试')
            token=uuid.uuid4().hex;operation=uuid.uuid4().hex;now=time.monotonic()
            self.tokens={k:v for k,v in self.tokens.items() if now-v['created']<300}
            if len(self.tokens)>=128:raise DraftError('上下文过多，请稍后重试')
            self.tokens[token]={'plan':plan,'revision':data['revision'],'operation':operation,'created':now,'changes':changes}
            return {'schema_version':2,'capability':'assisted_readonly','context_token':token,'operation_id':operation,
                    'plan':plan,'summary':subject_label(scope),'changes':changes,
                    'legacy_rebound':bool(old and not old.get('persistent',{}).get('target_root'))}
    def begin(self,body):
        with self.lock,self.plans.lock,self.native.lock:
            if set(body)-{'operation','context_token','idempotency_key','confirm_changes'}:raise DraftError('begin：未知字段')
            token=body.get('context_token');context=self.tokens.get(token) if isinstance(token,str) else None
            if not context or time.monotonic()-context['created']>300:raise DraftError('context_expired：对象仍保留，正在重新核验上下文')
            if body.get('idempotency_key')!=context['operation']:raise DraftError('操作标识不匹配')
            if context.get('response'):return context['response']
            if context['changes'] and body.get('confirm_changes') is not True:raise DraftError('target_confirmation_required：'+'；'.join(context['changes']))
            plan=context['plan'];meta=inspect_flclash(self.native.root)
            if any(s['kind']!='domain' and not self.available(s) for s in subjects(plan['subject'])):raise DraftError('subject_unavailable：程序身份已变化，原选择保留')
            if meta.get('source_revision')!=plan['persistent']['source_revision'] or self.controller.snapshot()!=plan['core']:
                raise DraftError('context_changed：配置已变化，保留对象并重新核验')
            if self.plans.read()['revision']!=context['revision']:raise DraftError('revision_changed：另一窗口已更新方案，请重新核验当前对象')
            existing=self.native._read(plan)
            guide=self.native.prepare(plan,operation_id=context['operation'])
            try:self.plans.operate({'operation':'save','revision':context['revision'],'fingerprint':digest(plan)},plan)
            except Exception:
                # Only discard a freshly created, never verified Monitor baseline.
                # No client operation has been exposed to the caller yet.
                if not existing and self.native.status(plan)['state']=='awaiting_client_save':self.native.forget(plan)
                raise
            response={**guide,'id':plan['id'],'plan':plan,'operation_id':context['operation'],
                      'schema_version':2,'capability':'assisted_readonly'}
            context['response']=response;return response
