"""Backend-only session adapter. No discovery files, credentials or SQL fallback."""
from __future__ import annotations
import hashlib
import json
import re
import threading
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from urllib.request import Request
from urllib.error import HTTPError
from .clash import local_opener
from .ipv6_plans import normalize_ipv6_subject, reject_rule, subjects
from .rule_drafts import DraftError


PROTOCOL=1
REQUIRED=('native_transaction','coordinated_mutations','strict_load_preserves_old',
          'parser_side_effects_audited','ordered_receipts','client_lifecycle_tested')


@dataclass(frozen=True)
class LoopbackSession:
    endpoint:str
    secret:str=field(repr=False)
    instance_id:str
    root_identity:str
    peer_verified:bool=False

    def __post_init__(self):
        url=urlsplit(self.endpoint)
        if (url.scheme!='http' or url.hostname!='127.0.0.1' or not url.port or url.path!='/bridge' or
                url.username is not None or url.password is not None or url.query or url.fragment or
                not re.fullmatch(r'[a-f0-9]{64}',self.secret)):
            raise DraftError('invalid_private_bridge_session')

    def call(self,action,body):
        if not self.peer_verified:raise DraftError('private_bootstrap_not_verified')
        payload=json.dumps({'action':action,'params':body}).encode()
        if len(payload)>65536:raise DraftError('bridge_request_too_large')
        request=Request(self.endpoint,data=payload,headers={'Authorization':'Bearer '+self.secret,'Content-Type':'application/json'},method='POST')
        try:
            with local_opener().open(request,timeout=5) as response:data=response.read(65537)
        except HTTPError as response:
            with response:data=response.read(65537)
        if len(data)>65536:raise DraftError('bridge_response_too_large')
        return json.loads(data)


class DirectBridge:
    def __init__(self,transport=None):
        self.transport=transport;self.lock=threading.RLock();self.pending={}
    def capabilities(self):
        if self.transport is None or not self.transport.peer_verified:
            return {'protocol':PROTOCOL,'available':False,'reason':'verified_session_unavailable'}
        result=self._call('capabilities',{})
        available=(type(result.get('protocol')) is int and result.get('protocol')==PROTOCOL and result.get('instance_id')==self.transport.instance_id and
                   result.get('root_identity')==self.transport.root_identity and result.get('schema_version')==4 and
                   result.get('profile_mode')=='standard' and result.get('mode')=='rule' and
                   all(result.get(k) is True for k in REQUIRED))
        return {'protocol':PROTOCOL,'available':available,'reason':'' if available else 'client_acceptance_incomplete',
                'profile_id':result.get('profile_id'),'revision':result.get('revision'),'instance_id':result.get('instance_id')}
    def _call(self,action,body):
        result=self.transport.call(action,body)
        if not isinstance(result,dict) or len(json.dumps(result).encode())>65536:raise DraftError('bridge_invalid_response')
        if result.get('error'):raise DraftError('bridge_'+str(result['error']))
        return result
    def prepare(self,plan):
        with self.lock:
            caps=self.capabilities()
            if not caps['available']:raise DraftError('bridge_unavailable：尚未连接已完整验收的客户端桥接')
            profile=str(plan['persistent']['profile_id'])
            if caps['profile_id']!=profile:raise DraftError('bridge_profile_changed')
            if len(self.pending)>=128:raise DraftError('bridge_operation_budget_exceeded')
            scope=normalize_ipv6_subject(plan['subject']);rule=reject_rule(scope)
            rule_hash=hashlib.sha256(rule.encode()).hexdigest();operation=uuid.uuid4().hex
            response=self._call('prepare',{'profile_id':profile,'subjects':subjects(scope),'expected_revision':caps['revision'],'operation_id':operation})
            if (response.get('operation_id')!=operation or response.get('rule_hash')!=rule_hash or
                    response.get('profile_id')!=profile or response.get('revision')!=caps['revision'] or
                    not re.fullmatch(r'[a-f0-9]{64}',str(response.get('token','')))):
                raise DraftError('bridge_scope_receipt_mismatch')
            self.pending[operation]={'token':response['token'],'revision':caps['revision'],'profile_id':profile,
                'rule_hash':rule_hash,'instance_id':caps['instance_id'],'root_identity':self.transport.root_identity,'outcome':'prepared'}
            return {'operation_id':operation,'state':'prepared','applied':False}
    def _receipt(self,operation,result):
        context=self.pending[operation]
        if result.get('operation_id')!=operation or result.get('profile_id')!=context['profile_id'] or result.get('instance_id')!=context['instance_id']:
            raise DraftError('bridge_operation_receipt_mismatch')
        state=result.get('state')
        if state not in {'prepared','persisted','applying','verified','unknown','recovery_required','rolled_back','durable_removed_runtime_pending','revoked'}:
            raise DraftError('bridge_unknown_state')
        if state=='verified':
            rule_id=result.get('rule_id')
            if (not isinstance(rule_id,str) or not rule_id.isdecimal() or not 0<int(rule_id)<2**63 or
                    result.get('mapping_id')!=f"{context['profile_id']}_{rule_id}_added" or
                    any(result.get(k)!=context['rule_hash'] for k in ('persistent_rule_hash','compiled_rule_hash','runtime_rule_hash')) or
                    result.get('nonrules_before')!=result.get('nonrules_after') or not result.get('nonrules_before') or
                    result.get('unmanaged_before')!=result.get('unmanaged_after') or not result.get('unmanaged_before') or
                    result.get('ordered_verified') is not True):
                raise DraftError('bridge_unverified_receipt')
        if state=='revoked' and (result.get('owned_absent') is not True or result.get('ordered_verified') is not True or
                                not result.get('unmanaged_before') or result.get('unmanaged_before')!=result.get('unmanaged_after')):
            raise DraftError('bridge_unverified_undo')
        context['outcome']=state
        return {'operation_id':operation,'state':state,'applied':state=='verified'}
    def commit(self,operation):
        with self.lock:
            context=self.pending.get(operation)
            if not context:raise DraftError('bridge_operation_not_found')
            if context['outcome']=='unknown':return self.status(operation)
            if context['outcome']!='prepared':return self.status(operation)
            caps=self.capabilities()
            if (not caps['available'] or caps['instance_id']!=context['instance_id'] or caps['profile_id']!=context['profile_id'] or
                    self.transport.root_identity!=context['root_identity'] or caps['revision']!=context['revision']):raise DraftError('bridge_session_or_capability_changed')
            try:
                return self._receipt(operation,self._call('commit',{'operation_id':operation,'token':context['token'],'expected_revision':context['revision']}))
            except (TimeoutError,ConnectionError,OSError):
                context['outcome']='unknown'
                return {'operation_id':operation,'state':'unknown','applied':False}
            except (DraftError,ValueError) as exc:
                context['outcome']='unknown'
                raise DraftError('bridge_outcome_unknown：回执未核实，只能按原操作查询') from exc
    def status(self,operation):
        with self.lock:
            if operation not in self.pending:raise DraftError('bridge_operation_not_found')
            caps=self.capabilities();context=self.pending[operation]
            if not caps['available'] or caps['instance_id']!=context['instance_id'] or caps['profile_id']!=context['profile_id'] or self.transport.root_identity!=context['root_identity']:raise DraftError('bridge_session_or_capability_changed')
            return self._receipt(operation,self._call('status',{'operation_id':operation}))
    def undo(self,operation):
        with self.lock:
            if operation not in self.pending:raise DraftError('bridge_operation_not_found')
            state=self.status(operation)['state']
            if state=='revoked':return {'operation_id':operation,'state':state,'applied':False}
            if state not in {'verified','durable_removed_runtime_pending'}:raise DraftError('bridge_undo_requires_known_ownership')
            try:return self._receipt(operation,self._call('undo',{'operation_id':operation}))
            except (TimeoutError,ConnectionError,OSError):
                self.pending[operation]['outcome']='unknown'
                return {'operation_id':operation,'state':'unknown','applied':False}
            except (DraftError,ValueError) as exc:
                self.pending[operation]['outcome']='unknown'
                raise DraftError('bridge_outcome_unknown：撤销回执未核实，只能按原操作查询') from exc
