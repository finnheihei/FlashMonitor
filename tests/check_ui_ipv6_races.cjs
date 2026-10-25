// Exercise the production IPv6 dialog with an isolated DOM and deferred API.
// Never contacts a controller or applies a network configuration.
const assert=require('node:assert/strict'),vm=require('node:vm');
const {script,translator}=require('./ui_locale.cjs');
const language=process.env.FM_TEST_LANGUAGE || 'zh-CN',tr=translator(language);
new vm.Script(script);
function source(name) {
  const start=script.search(new RegExp('(?:async )?function '+name+'\\('));assert.ok(start>=0,name);
  const tail=script.slice(start),end=tail.search(/\r?\n\}/);assert.ok(end>0,name);
  return tail.slice(0,end)+'\n}';
}
function session({supported=false}={}) {
  const nodes=new Map(),requests=[],pending=[];
  function element(id) {
    if(!nodes.has(id)) nodes.set(id,{id,value:'',checked:false,hidden:false,disabled:false,inert:false,open:false,textContent:'',attributes:{},options:[],
      setAttribute(key,value){this.attributes[key]=String(value);},focus(){},
      replaceChildren(...options){this.options=options;this.value=options[0]?.value || '';},add(option){this.options.push(option);}});
    return nodes.get(id);
  }
  const context=vm.createContext({console,URL,tr,Option:function(label,value){this.label=label;this.value=value;},
    document:{activeElement:null,getElementById:element},connectionEl:id=>element('connection-'+id),
    connectionFocus:null,connectionProcess:'',connectionOpenSequence:1,connectionLoading:false,
    connectionFixedScope:{kind:'domain',value:'old.example',include_subdomains:false},connectionApplications:[],
    scopedIPv6Catalog:{supported,revision:'original',entries:[]},scopedIPv6Busy:false,scopedIPv6Sequence:0,scopedIPv6Error:'',scopedIPv6Reads:0,
    connectionDialog:{open:true,close(){this.open=false;},showModal(){this.open=true;}},
    closeViewMenu(){},closeProgramMenu(){},
    api(url,options={}) {requests.push({url,...options});return new Promise((resolve,reject)=>pending.push({resolve,reject,body:options.body}));},
  });
  const run=code=>vm.runInContext(code,context);
  for(const name of ['escapeHtml','connectionCapture','connectionLabel','paintConnectionTargets','openConnectionSettings','closeConnectionSettings',
    'scopedIPv6ScopeKey','scopedIPv6Coverage','scopedIPv6Related','scopedIPv6Current','scopedIPv6Controls','renderScopedIPv6','loadScopedIPv6','scopedIPv6Action']) run(source(name));
  const refreshStart=script.indexOf("document.getElementById('scoped-ipv6-refresh').onclick=async()=>{");
  run(script.slice(refreshStart,script.indexOf("document.getElementById('scoped-ipv6-list').onclick",refreshStart)));
  run('paintConnectionTargets()');
  return {context,run,element,requests,pending,posts:()=>requests.filter(r=>r.body)};
}
const catalog=(revision='current')=>({supported:true,revision,entries:[]});
const flush=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
(async()=>{
  // Close/reopen with the same target is a new session too. A late old read can
  // neither apply to the new dialog nor clear its loading state.
  for(const target of ['new.example','old.example']) {
    const s=session(),oldAction=s.run("scopedIPv6Action('enable')");
    assert.equal(s.context.connectionLoading,true);
    assert.equal(s.element('scoped-ipv6-enable').disabled,true);
    assert.equal(s.element('connection-close').disabled,false,'a read-only retry remains dismissible');
    await s.run("scopedIPv6Action('enable')");assert.equal(s.requests.length,1,'duplicate click does not start another retry');
    s.run('closeConnectionSettings()');assert.equal(s.context.connectionDialog.open,false);
    const reopened=s.run(`openConnectionSettings({kind:'domain',value:'${target}',include_subdomains:false})`);
    s.pending[0].resolve(catalog('old'));await oldAction;
    assert.equal(s.context.connectionLoading,true,'old completion must not unlock the new dialog');
    s.pending[1].resolve(catalog('new'));await reopened;
    assert.equal(s.posts().length,0);
    assert.equal(s.context.scopedIPv6Catalog.revision,'new');
  }

  // Original reproduction: the new target is ready before the old retry ends.
  {
    const s=session(),oldAction=s.run("scopedIPv6Action('enable')");s.run('closeConnectionSettings()');
    const reopened=s.run("openConnectionSettings({kind:'domain',value:'new.example'})");
    s.pending[1].resolve(catalog('new'));await reopened;
    s.pending[0].resolve({supported:false,error:'old request failed'});await oldAction;
    assert.equal(s.posts().length,0);assert.equal(s.context.scopedIPv6Error,'');
  }

  // A scope edit in the same dialog also requires a new explicit action.
  {
    const s=session(),action=s.run("scopedIPv6Action('enable')");
    s.element('connection-subdomains').checked=true;
    s.pending[0].resolve(catalog());await action;
    assert.equal(s.posts().length,0);
    assert.equal(s.context.scopedIPv6Error,tr('对象已更改，请重新确认操作。'));
    assert.equal(s.element('scoped-ipv6-enable').disabled,false);
  }

  // A valid retry submits its captured scope exactly once; after POST has been
  // sent, a close/open attempt cannot disguise or redirect that operation.
  {
    const s=session(),action=s.run("scopedIPv6Action('enable')");
    s.pending[0].resolve(catalog('ready'));await flush();
    assert.equal(s.posts().length,1);
    assert.deepEqual(JSON.parse(JSON.stringify(s.posts()[0].body)),{operation:'enable',revision:'ready',scope:{kind:'domain',value:'old.example',include_subdomains:false}});
    assert.equal(s.context.scopedIPv6Busy,true);s.run('closeConnectionSettings()');
    assert.equal(s.context.connectionDialog.open,true);
    await s.run("openConnectionSettings({kind:'domain',value:'new.example'})");
    assert.equal(s.run('connectionCapture().value'),'old.example');
    s.pending[1].resolve({...catalog('applied'),entries:[{id:'saved',scope:{kind:'domain',value:'old.example',include_subdomains:false},current:true,applied:true,state:'enabled'}]});
    await action;assert.equal(s.context.scopedIPv6Busy,false);
    assert.equal(s.element('scoped-ipv6-enable').attributes['aria-checked'],'true');
  }

  // A submitted failure rechecks the actual saved state and enables a retry.
  {
    const s=session({supported:true}),action=s.run("scopedIPv6Action('enable')");
    s.pending[0].reject(Error('write failed'));await flush();
    assert.equal(s.pending.length,2);s.pending[1].resolve(catalog('recovered'));await action;
    assert.equal(s.context.scopedIPv6Busy,false);assert.equal(s.context.scopedIPv6Error,'write failed');
    assert.equal(s.element('scoped-ipv6-enable').disabled,false);
  }

  // Refresh may reconcile saved entries, but not after its dialog was replaced.
  {
    const s=session({supported:true}),refresh=s.element('scoped-ipv6-refresh').onclick();
    s.run('closeConnectionSettings()');const reopened=s.run("openConnectionSettings({kind:'domain',value:'new.example'})");
    s.pending[1].resolve({...catalog('new'),entries:[{id:'new',scope:{kind:'domain',value:'new.example'},current:true,state:'pending'}]});await reopened;
    s.pending[0].resolve(catalog('old'));await refresh;
    assert.equal(s.posts().length,0,'a stale refresh must not reconcile the new session');
  }
  // A parent domain's rule remains visible when this exact child has no own rule.
  {
    const s=session({supported:true});
    s.context.connectionFixedScope={kind:'domain',value:'api.example.com',include_subdomains:false};
    s.context.scopedIPv6Catalog={...catalog(),entries:[{id:'parent',current:true,applied:true,state:'enabled',scope:{kind:'domain',value:'example.com',include_subdomains:true}}]};
    s.run('scopedIPv6Controls()');
    assert.equal(s.element('scoped-ipv6-enable').attributes['aria-checked'],'false');
    assert.equal(s.element('scoped-ipv6-state').textContent,tr('本条未开启 · 其他规则仍在限制'));
    assert.equal(s.element('scoped-ipv6-coverage').hidden,false);
    assert.ok(s.element('scoped-ipv6-coverage').innerHTML.includes('example.com'));
    assert.ok(s.element('scoped-ipv6-coverage').innerHTML.includes(tr('查看相关规则')));
    s.context.scopedIPv6Catalog.entries[0].applied=false;s.run('scopedIPv6Controls()');
    assert.equal(s.element('scoped-ipv6-state').textContent,tr('本条未开启 · 相关规则待核实'));
    assert.equal(s.run("scopedIPv6Coverage({kind:'domain',value:'example.com',include_subdomains:true},{kind:'domain',value:'badexample.com'})"),0);
    assert.equal(s.run("scopedIPv6Coverage({kind:'all',children:[{kind:'domain',value:'example.com',include_subdomains:true},{kind:'process_path',value:'C:/app.exe'}]},{kind:'domain',value:'api.example.com'})"),1,'combined rule affects only some connections');
    assert.equal(s.run("scopedIPv6Coverage({kind:'any',children:[{kind:'domain',value:'example.com',include_subdomains:true},{kind:'domain',value:'other.example'}]},{kind:'domain',value:'api.example.com'})"),2);
    if(language==='en') assert.ok(!/[\u3400-\u9fff]/.test(s.element('scoped-ipv6-coverage').innerHTML));
  }
  console.log(`PASS IPv6 races (${language}): retry cancellation, session isolation, scope edits, duplicate clicks, committed action recovery, stale refresh`);
})().catch(error=>{console.error(error);process.exitCode=1;});
