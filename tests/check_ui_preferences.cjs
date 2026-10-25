// Preference failures must not hide/pin apps until the backend confirms saving.
const assert=require('node:assert/strict'),vm=require('node:vm');
const {script,translator}=require('./ui_locale.cjs');
const language=process.env.FM_TEST_LANGUAGE || 'zh-CN',tr=translator(language);
new vm.Script(script);
function source(name) {
  const start=script.search(new RegExp('(?:async )?function '+name+'\\('));
  assert.ok(start>=0,name);
  const tail=script.slice(start),end=tail.slice(1).search(/\n(?:async )?function /);
  return end<0?tail:tail.slice(0,end+1);
}
const buttons=[{dataset:{ignore:'browser'},disabled:false},{dataset:{pin:'browser'},disabled:false}];
const toasts=[],calls=[];
const context=vm.createContext({tr,prefPending:new Set(),prefOverrides:new Map(),prefSaveChain:Promise.resolve(),prefCommitVersion:0,
  last:{prefs:{ignored:[],pinned:[]}},settingsCache:{ignored:[],pinned:[]},
  document:{querySelectorAll:()=>buttons},render(){},tick(){},flashToast:message=>toasts.push(message),
});
for(const name of ['prefs','isIgnored','isPinned','settingErrorText','savePref']) vm.runInContext(source(name),context);
const run=code=>vm.runInContext(code,context);
const reconcileStart=script.indexOf('    for(const [id,item] of prefOverrides)');
const reconcileEnd=script.indexOf('    if (d && !d.error)',reconcileStart);
assert.ok(reconcileStart>0 && reconcileEnd>reconcileStart);
function snapshot(prefs) {
  context.last={prefs};context.d=context.last;context.requestedPrefsVersion=context.prefCommitVersion;
  run(script.slice(reconcileStart,reconcileEnd));
}
(async()=>{
  // Both a business rejection and a transport failure preserve confirmed data.
  for(const key of ['ignored','pinned']) for(const transport of [false,true]) {
    const beforeCache=context.settingsCache,version=context.prefCommitVersion;
    context.api=async(url,options)=>{
      calls.push(options.body);
      if(transport) throw Error('offline');
      return {ignored:[],pinned:[],error:'设置没存上：disk full'};
    };
    context.patch={[key]:true};await run("savePref('browser',patch)");
    assert.equal(run("isIgnored('browser')"),false);
    assert.equal(run("isPinned('browser')"),false);
    assert.equal(context.settingsCache,beforeCache);
    assert.equal(context.prefCommitVersion,version);
    assert.equal(context.prefOverrides.size,0);
    assert.ok(toasts.at(-1).includes(transport?'offline':'disk full'));
    if(language==='en') assert.ok(!/[\u3400-\u9fff]/.test(toasts.at(-1)),'dynamic failure prefix must be translated');
    assert.equal(context.prefPending.size,0);assert.ok(buttons.every(button=>!button.disabled));
    snapshot({ignored:[],pinned:[]});
    assert.equal(run("isIgnored('browser') || isPinned('browser')"),false);
  }

  // Retry succeeds; an older snapshot must not undo that confirmed save.
  context.api=async()=>({ignored:['browser'],pinned:[],error:''});
  await run("savePref('browser',{ignored:true})");
  assert.equal(run("isIgnored('browser')"),true);
  snapshot({ignored:[],pinned:[]});
  assert.equal(run("isIgnored('browser')"),true);
  const savedOverride=context.prefOverrides.get('browser:ignored');

  // A failed reversal keeps the previous success even while snapshots lag.
  context.api=async()=>({ignored:['browser'],pinned:[],error:'设置没存上：disk full'});
  await run("savePref('browser',{ignored:false})");
  assert.equal(run("isIgnored('browser')"),true);
  assert.equal(context.prefOverrides.get('browser:ignored'),savedOverride);
  snapshot({ignored:['browser'],pinned:[]});
  assert.equal(context.prefOverrides.size,0);
  assert.equal(run("isIgnored('browser')"),true);

  // A settled failure releases the shared queue so the next app can save.
  let release;
  context.api=(url,options)=>{
    calls.push(options.body);
    if(options.body.process==='browser') return new Promise(resolve=>{release=()=>resolve({ignored:['browser'],pinned:[],error:'disk full'});});
    return Promise.resolve({ignored:['browser'],pinned:['editor'],error:''});
  };
  const first=run("savePref('browser',{ignored:false})"),next=run("savePref('editor',{pinned:true})");
  await new Promise(resolve=>setImmediate(resolve));
  assert.ok(buttons.every(button=>button.disabled));
  release();await first;await next;
  assert.equal(run("isPinned('editor')"),true);
  assert.equal(context.prefPending.size,0);
  console.log('PASS preferences ('+language+'): rejected writes, transport failures, retries, prior committed state, snapshot convergence, queue recovery');
})().catch(error=>{console.error(error);process.exitCode=1;});
