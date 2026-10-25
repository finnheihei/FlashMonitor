// Exercise the production save/render/diagnostic functions without a network or browser.
const assert=require('node:assert/strict'),vm=require('node:vm');
const {script,translator}=require('./ui_locale.cjs');
new vm.Script(script);
const language=process.env.FM_TEST_LANGUAGE || 'zh-CN',tr=translator(language);
function source(name) {
  const start=script.search(new RegExp('(?:async )?function '+name+'\\('));
  assert.ok(start>=0,name);
  const tail=script.slice(start),end=tail.slice(1).search(/\n(?:async )?function /);
  return end<0?tail:tail.slice(0,end+1);
}
const nodes=new Map(),toasts=[],requests=[];
const decode=value=>value.replace(/&(?:amp|lt|gt|quot|#39);/g,key=>({'&amp;':'&','&lt;':'<','&gt;':'>','&quot;':'"','&#39;':"'"}[key]));
const main={id:'main',_html:'',
  contains:node=>!!node && nodes.get(node.id)===node,
  querySelectorAll:selector=>selector==='input[data-setting]'?[...nodes.values()].filter(node=>node.dataset.setting):[],
  set innerHTML(html) {
    this.writes=(this.writes || 0)+1;
    nodes.clear();
    for(const match of html.matchAll(/<(?:input|p|span)\b([^>]+)>/g)) {
      const attributes=Object.fromEntries([...match[1].matchAll(/([\w-]+)="([^"]*)"/g)].map(item=>[item[1],decode(item[2])]));
      if(!attributes.id) continue;
      const dataset=new Proxy(attributes['data-setting']?{setting:attributes['data-setting']}:{},{set(target,key,value){target[key]=String(value);return true;}});
      nodes.set(attributes.id,{id:attributes.id,value:attributes.value || '',textContent:'',hidden:match[1].includes(' hidden'),dataset,attributes,
        setAttribute(key,value){this.attributes[key]=value;},focus(){context.document.activeElement=this;}});
    }
  }
};
const initial={ignored:[],clash_api:'http://127.0.0.1:9090',clash_secret_set:true,sub_url_set:true,sub_url_hint:'https://saved.example',error:''};
const context=vm.createContext({
  tr,filter:'settings',setPane:'connect',settingsCache:{...initial},settingsNote:'',settingsInputRevision:0,settingsSaveTokens:new Map(),settingsSaveQueue:Promise.resolve(),settingsSaveStates:new Map(),clashConfigRevision:0,clashNote:'',clashTesting:false,
  document:{activeElement:null,getElementById:id=>id==='main'?main:nodes.get(id)},
  heldFor:()=>false,hold:{pending:new Map()},flashToast:message=>toasts.push(message),
  iconImg:()=>'',nameHtml:()=>'',tick:()=>{},
  api:async(url,options)=>{requests.push({url,body:options.body});return {...initial,error:'rejected'};},
});
for(const name of ['loadSettings','retrySetting','escapeHtml','settingInputFeedback','settingErrorText','invalidateClashTest','editSettingInput','saveSetting','saveSecret','renderSettings','setHtml','clashFailureText','clashTestSummary','prepareClashSettings','testClash']) vm.runInContext(source(name),context);
context.main=main;
context.render=()=>vm.runInContext('setHtml(main,renderSettings(),true)',context);
const run=code=>vm.runInContext(code,context);
function input(id,value) {const field=nodes.get(id);field.value=value;context.input=field;run('editSettingInput(input)');return field;}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  context.render();
  for(const prefix of ['设置没存上：','其他设置已保存，开机启动项未更新：']) {
    context.dynamicError=prefix+'access denied';
    assert.equal(run('settingErrorText(dynamicError)'),tr(prefix)+'access denied');
  }
  const message='订阅地址无效，请填写完整的 http:// 或 https:// 地址，并检查主机名和端口（少于 2000 字符）；清除订阅请使用清除按钮。';
  context.api=async(url,options)=>{requests.push({url,body:options.body});return {...initial,error:message};};
  input('sub-url','sub.example/no-scheme?token=private');
  await run("saveSecret('sub_url',input)");
  assert.equal(nodes.get('sub-url').value,'sub.example/no-scheme?token=private');
  assert.equal(nodes.get('sub-url').attributes['aria-invalid'],'true');
  assert.equal(nodes.get('sub-url-error').textContent,tr(message));
  if(language==='en') assert.ok(!/[\u3400-\u9fff]/.test(nodes.get('sub-url-error').textContent),'URL validation message must be translated');
  assert.equal(nodes.get('sub-url-error').hidden,false);
  assert.equal(context.settingsCache.sub_url_set,true);
  assert.equal(context.settingsNote,'');
  assert.ok(!main._html.includes('token=private'),'draft secret must not be serialized into rendered markup');

  // An unrelated form rerender must preserve both the failed input and its message.
  context.clashNote='diagnostic result';context.render();
  assert.equal(nodes.get('sub-url').value,'sub.example/no-scheme?token=private');
  assert.equal(nodes.get('sub-url-error').textContent,tr(message));

  context.api=async()=>({...initial,error:''});
  input('sub-url','https://new.example/subscribe?token=new-private');
  await run("saveSecret('sub_url',input)");
  assert.equal(nodes.get('sub-url').value,'');
  assert.equal(nodes.get('sub-url-error').hidden,true);
  assert.equal(context.settingsNote,tr('已保存'));
  assert.equal(context.settingsSaveStates.get('sub_url').value,undefined,'successful secret submissions are not retained for generic retries');

  input('clash-address','127.0.0.1:9097');
  context.api=async()=>({...initial,error:'Invalid controller URL'});
  await run("saveSetting('clash_api',input.value,false,input)");
  assert.equal(nodes.get('clash-address').value,'127.0.0.1:9097');
  assert.equal(nodes.get('clash-address-error').textContent,'Invalid controller URL');
  assert.equal(context.settingsCache.clash_api,initial.clash_api);

  context.api=async()=>{throw Error('network unavailable');};
  input('clash-secret','new-secret');
  await run("saveSecret('clash_secret',input)");
  assert.equal(nodes.get('clash-secret').value,'new-secret');
  assert.ok(nodes.get('clash-secret-error').textContent.includes('network unavailable'));
  assert.equal(toasts.length,1);

  // A successful older request cannot erase a newer edit.
  let release;
  context.api=()=>new Promise(resolve=>{release=resolve;});
  input('sub-url','https://first.example');
  const pending=run("saveSecret('sub_url',input)");
  input('sub-url','https://second.example');
  await flush();
  release({...initial});await pending;
  assert.equal(nodes.get('sub-url').value,'https://second.example');
  assert.equal(context.settingsNote,'','an older success must not label a newer draft saved');

  // Clicking Test connection can replace the form while a secret save is in flight.
  input('sub-url','https://committed.example?token=private');
  const beforeRender=nodes.get('sub-url');
  const saving=run("saveSecret('sub_url',input)");
  context.clashNote='new connection test';context.render();
  assert.notEqual(nodes.get('sub-url'),beforeRender);
  await flush();
  release({...initial});await saving;
  assert.equal(nodes.get('sub-url').value,'','successful response clears the current input, not a detached node');
  assert.equal(nodes.get('sub-url').dataset.dirty,undefined);
  assert.equal(context.settingsNote,tr('已保存'));

  input('sub-url','https://old.example');
  const rejected=run("saveSecret('sub_url',input)");
  input('sub-url','https://corrected.example');
  await flush();
  release({...initial,error:message});await rejected;
  assert.equal(nodes.get('sub-url-error').hidden,true,'an old rejection must not mark a corrected input invalid');
  assert.equal(context.settingsNote,'');

  // Only one write may reach the backend at a time, including across fields.
  let backend={...initial};const writes=[];
  context.api=(url,options)=>new Promise(resolve=>writes.push({patch:options.body,finish(){backend={...backend,...options.body};resolve({...backend});}}));
  input('clash-address','http://127.0.0.1:9091');
  const firstWrite=run("saveSetting('clash_api',input.value,false,input)");
  input('clash-address','http://127.0.0.1:9097');
  const lastWrite=run("saveSetting('clash_api',input.value,false,input)");
  const otherWrite=run("saveSetting('show_airport',false)");
  await flush();assert.equal(writes.length,1);
  writes[0].finish();await firstWrite;await flush();assert.equal(writes.length,2);
  writes[1].finish();await lastWrite;await flush();assert.equal(writes.length,3);
  writes[2].finish();await otherWrite;
  assert.equal(backend.clash_api,'http://127.0.0.1:9097');
  assert.equal(context.settingsCache.clash_api,backend.clash_api);
  assert.equal(context.settingsCache.show_airport,false);

  // A click after onchange waits for the address save and submits the unsaved
  // secret as well. The test must observe both new values, not the old backend.
  context.settingsSaveStates.clear();const actions=[];let finishAddress,finishSecret;
  context.api=(url,options)=>{
    if(url==='/api/settings') {
      actions.push(Object.keys(options.body)[0]);
      return new Promise(resolve=>{
        const finish=()=>{Object.assign(backend,options.body);resolve({...backend,clash_secret:undefined});};
        if('clash_api' in options.body) finishAddress=finish;else finishSecret=finish;
      });
    }
    actions.push('test');
    assert.equal(backend.clash_api,'http://127.0.0.1:9098');
    assert.equal(backend.clash_secret,'updated-secret');
    return Promise.resolve({ok:true,base:backend.clash_api,connections:2,ms:3});
  };
  input('clash-address','http://127.0.0.1:9098');
  const addressSave=run("saveSetting('clash_api',input.value,false,input)");
  input('clash-secret','updated-secret');
  const connectionTest=run('testClash()');
  await flush();assert.deepEqual(actions,['clash_api']);
  finishAddress();await addressSave;await flush();assert.deepEqual(actions,['clash_api','clash_secret']);
  finishSecret();await connectionTest;
  assert.deepEqual(actions,['clash_api','clash_secret','test']);
  assert.ok(context.clashNote.includes('9098'));
  assert.equal(nodes.get('clash-secret').value,'');

  // Failed address writes stop testing and focus the retained invalid input.
  let testCalls=0;
  context.api=async(url)=>{if(url==='/api/test-clash') ++testCalls;return {...backend,error:'Invalid controller URL'};};
  input('clash-address','invalid-address');
  await run('testClash()');
  assert.equal(testCalls,0);
  assert.equal(context.clashNote,tr('连接设置未保存，已停止测试。'));
  assert.equal(context.document.activeElement,nodes.get('clash-address'));
  assert.equal(nodes.get('clash-address').value,'invalid-address');
  assert.equal(nodes.get('clash-address-error').textContent,'Invalid controller URL');

  // A transport failure is also a failed save, not permission to test old data.
  context.api=async(url)=>{if(url==='/api/test-clash') ++testCalls;throw Error('offline');};
  input('clash-address','http://127.0.0.1:9099');
  await run('testClash()');assert.equal(testCalls,0);
  assert.ok(nodes.get('clash-address-error').textContent.includes('offline'));

  // A newer edit made while saving stops this test; no stale config is tested.
  let finishChanging;
  context.api=(url,options)=>{
    if(url==='/api/test-clash') {++testCalls;return Promise.resolve({ok:true,base:backend.clash_api});}
    return new Promise(resolve=>{finishChanging=()=>{Object.assign(backend,options.body);resolve({...backend,error:''});};});
  };
  input('clash-address','http://127.0.0.1:9100');const changedWhileSaving=run('testClash()');
  await flush();input('clash-address','http://127.0.0.1:9101');finishChanging();await changedWhileSaving;
  assert.equal(testCalls,0);assert.equal(nodes.get('clash-address').value,'http://127.0.0.1:9101');
  assert.equal(context.clashNote,tr('连接设置已更改，请重新测试。'));

  // A connection response belongs only to the config revision it tested.
  let finishTesting;
  context.api=async(url,options)=>{
    if(url==='/api/settings') {Object.assign(backend,options.body);return {...backend,error:''};}
    return new Promise(resolve=>{finishTesting=()=>resolve({ok:true,base:backend.clash_api,connections:1,ms:1});});
  };
  const staleTest=run('testClash()');await flush();
  input('clash-address','http://127.0.0.1:9102');
  assert.equal(context.clashNote,'','editing invalidates the displayed connection result immediately');
  finishTesting();await staleTest;
  assert.equal(context.clashNote,'','late test success must not label a newer edit connected');
  assert.equal(nodes.get('clash-address').value,'http://127.0.0.1:9102');
  assert.equal(context.clashTesting,false);

  // Erasing a rejected new secret abandons the draft and keeps the saved one;
  // it must not be confused with the explicit Clear action.
  context.api=async(url,options)=>{
    if(url==='/api/settings') {
      if('clash_secret' in options.body) return {...backend,error:'Secret could not be saved'};
      Object.assign(backend,options.body);return {...backend,error:''};
    }
    ++testCalls;return {ok:true,base:backend.clash_api,connections:1,ms:1};
  };
  input('clash-secret','rejected-secret');await run("saveSecret('clash_secret',input)");
  input('clash-secret','');await run('testClash()');
  assert.equal(testCalls,1);
  assert.ok(context.clashNote.startsWith(tr('已接通')));
  assert.equal(backend.clash_secret,'updated-secret','empty draft must not clear the saved secret');
  assert.equal(nodes.get('clash-secret-error').hidden,true);

  // An explicit Clear failure still blocks testing that requested config.
  await run("saveSetting('clash_secret','',false,document.getElementById('clash-secret'))");
  await run('testClash()');assert.equal(testCalls,1);
  assert.equal(context.document.activeElement,nodes.get('clash-secret'));
  assert.equal(context.clashNote,tr('连接设置未保存，已停止测试。'));

  // Reopening the form without its abandoned failed draft can test saved data.
  delete nodes.get('clash-secret').dataset.dirty;
  delete nodes.get('clash-secret').dataset.editRevision;
  context.render();await run('testClash()');
  assert.equal(testCalls,2);
  assert.ok(context.clashNote.startsWith(tr('已接通')));

  // Abandoning a draft cannot cancel a POST already sent. Wait for that POST
  // before testing whichever secret actually remains saved after it finishes.
  for(const saveFails of [false,true]) {
    const previousSecret=backend.clash_secret,newSecret='pending-secret-'+saveFails;
    const testedSecrets=[];let finishPendingSecret;
    context.api=(url,options)=>{
      if(url==='/api/settings') return new Promise(resolve=>{
        finishPendingSecret=()=>{
          if(!saveFails) Object.assign(backend,options.body);
          resolve({...backend,clash_secret:undefined,error:saveFails?'Secret could not be saved':''});
        };
      });
      testedSecrets.push(backend.clash_secret);
      return Promise.resolve({ok:true,base:backend.clash_api,connections:1,ms:1});
    };
    input('clash-secret',newSecret);
    const pendingSecretSave=run("saveSecret('clash_secret',input)");
    await flush();assert.equal(typeof finishPendingSecret,'function');
    input('clash-secret','');const pendingSecretTest=run('testClash()');
    await flush();assert.equal(testedSecrets.length,0,'test must wait for an abandoned secret POST still in flight');
    finishPendingSecret();await pendingSecretSave;await pendingSecretTest;
    assert.deepEqual(testedSecrets,[saveFails?previousSecret:newSecret]);
    assert.equal(nodes.get('clash-secret').value,'');
    assert.equal(nodes.get('clash-secret-error').hidden,true);
    assert.ok(context.clashNote.startsWith(tr('已接通')));
  }

  // An explicit test retries a completed failed save without requiring a
  // meaningless edit. This applies to addresses, new secrets and Clear.
  for(const scenario of [
    {key:'clash_api',id:'clash-address',draft:'http://127.0.0.1:9110',value:'http://127.0.0.1:9110',transport:false},
    {key:'clash_secret',id:'clash-secret',draft:'retry-secret',value:'retry-secret',transport:true},
    {key:'clash_secret',id:'clash-secret',draft:'',value:'',transport:false},
    {key:'clash_secret',id:'clash-secret',draft:'unsaved-secret',value:'',transport:false},
  ]) {
    context.settingsSaveStates.clear();context.settingsCache={...backend,error:''};
    main._html='';context.render();
    let saves=0;const tested=[],patches=[];
    context.api=async(url,options)=>{
      if(url==='/api/settings') {
        patches.push({...options.body});
        if(++saves===1) {
          if(scenario.transport) throw Error('temporarily offline');
          return {...backend,error:'设置没存上：Temporarily unable to save'};
        }
        Object.assign(backend,options.body);
        return {...backend,error:'',clash_secret_set:!!backend.clash_secret};
      }
      tested.push({address:backend.clash_api,secret:backend.clash_secret});
      return {ok:true,base:backend.clash_api,connections:1,ms:1};
    };
    input(scenario.id,scenario.draft);context.retryScenario=scenario;
    await run('saveSetting(retryScenario.key,retryScenario.value,false,input)');
    assert.equal(saves,1);assert.equal(tested.length,0);
    const revision=nodes.get(scenario.id).dataset.editRevision;
    await run('testClash()');
    assert.equal(saves,2,'unchanged failed edit must be submitted again');
    assert.equal(patches[1][scenario.key],scenario.value,'retry preserves explicit Clear instead of saving the visible draft');
    assert.equal(tested.length,1,'test runs only after the retry succeeds');
    assert.equal(tested[0][scenario.key==='clash_api'?'address':'secret'],scenario.value);
    assert.equal(nodes.get(scenario.id).dataset.editRevision,undefined,'successful retry clears the dirty DOM state');
    assert.equal(nodes.get(scenario.id+'-error').hidden,true);
    assert.ok(context.clashNote.startsWith(tr('已接通')));
    assert.ok(revision!==undefined);
  }

  const attempts=[{base:'http://127.0.0.1:9097',ok:false,error_code:'auth_failed'},
    {base:'http://127.0.0.1:9090',ok:false,error_code:'connection_refused'}];
  context.result={ok:false,error_code:'auth_failed',attempts};
  const summary=run('clashTestSummary(result)');
  for(const attempt of attempts) assert.ok(summary.includes(attempt.base));
  assert.ok(summary.includes(tr('认证失败，请检查 Clash 密钥是否与客户端一致。')));
  if(language==='en') assert.ok(!/[\u3400-\u9fff]/.test(summary),'diagnostics must be translated');
  for(const code of ['timeout','invalid_response','http_error','network_error','invalid_address']) {
    context.result={error_code:code};assert.ok(run('clashFailureText(result)').length>10);
  }
  context.result={ok:true,base:'http://127.0.0.1:9090',connections:3,ms:12,attempts:[attempts[0],{base:'http://127.0.0.1:9090',ok:true}]};
  assert.ok(run('clashTestSummary(result)').includes(tr('已接通')));
  assert.ok(run('clashTestSummary(result)').includes(tr('认证失败，请检查 Clash 密钥是否与客户端一致。')));
  // Every settings pane must render even when the dynamic update button is absent.
  context.settingsAdvancedOpen=false;context.resetBusy=false;context.last={status:{}};
  context.updateInfo=null;context.updateState={busy:false,restarting:false};context.MARK_SVG=()=>'';
  context.settingsCache={...initial};context.settingsNote='';context.clashNote='';
  for(const pane of ['general','notify','connect','data','about']) {
    context.setPane=pane;
    const markup=run('renderSettings()');
    assert.ok(markup.includes('id="set-form"'));
    if(language==='en') assert.ok(!/[\u3400-\u9fff]/.test(markup),pane+' untranslated text: '+(markup.match(/[\u3400-\u9fff][^<>]*/g) || []).join(', '));
  }
  context.setPane='data';context.settingsAdvancedOpen=true;
  assert.match(run('renderSettings()'),/id="settings-advanced" open/,'maintenance disclosure survives rerender');
  context.setPane='about';context.updateState={busy:true,phase:'check',restarting:false};
  assert.match(run('renderSettings()'),/id="btn-update"[^>]*disabled/,'about reflects an in-flight update check');
  // Ordinary switches restore the confirmed value and retain a local retry.
  context.setPane='general';context.settingsCache={...initial,close_to_tray:true};
  context.api=async()=>{throw Error('offline');};
  await run("saveSetting('close_to_tray',false)");
  assert.equal(context.settingsCache.close_to_tray,true);
  let form=run('renderSettings()');
  assert.match(form,/<input type="checkbox" checked onchange="saveSetting\('close_to_tray'/);
  assert.ok(form.includes('data-setting-error="close_to_tray"'));assert.ok(form.includes('offline'));
  context.render();assert.ok(main._html.includes('offline'),'error survives unrelated renders');
  const previousWrites=main.writes;await run("saveSetting('close_to_tray',false)");
  assert.ok(main.writes>previousWrites,'repeating the same failure still restores a user-toggled checkbox');
  context.api=async()=>({...initial,close_to_tray:false});
  await run("retrySetting('close_to_tray')");
  assert.equal(context.settingsCache.close_to_tray,false);assert.ok(!run('renderSettings()').includes('data-setting-error="close_to_tray"'));
  // No editable defaults while the first settings request is unresolved or failed.
  context.settingsCache=null;context.setPane='connect';let finishRead;
  context.api=()=>new Promise((resolve,reject)=>{finishRead={resolve,reject};});
  const loading=run('loadSettings()');
  assert.ok(!run('renderSettings()').includes('<input'));
  assert.ok(run('renderSettings()').includes(tr('正在读取设置…')));
  finishRead.reject(Error('unavailable'));await loading;
  form=run('renderSettings()');assert.ok(!form.includes('<input'));assert.ok(form.includes('role="alert"'));assert.ok(form.includes('onclick="loadSettings()"'));
  context.api=async()=>({...initial});await run('loadSettings()');
  assert.ok(run('renderSettings()').includes('id="clash-address"'));assert.equal(run('loadSettings.error'),'');
  console.log('PASS settings feedback: retained edits, translations, serialized saves, retry and diagnostic attempts, all panes, maintenance disclosure');
})().catch(error=>{console.error(error);process.exitCode=1;});
