// UI behavior without a live controller: never disconnects the user's traffic.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const script=fs.readFileSync(path.join(__dirname,'..','ui.html'),'utf8').split('<script>')[1].split('</script>')[0];
new vm.Script(script);
function source(name) {
  const start=script.search(new RegExp('(?:async )?function '+name+'\\('));
  assert.ok(start>=0,name);
  const tail=script.slice(start),end=tail.slice(1).search(/\n(?:async )?function /);
  return end<0?tail:tail.slice(0,end+1);
}
const calls=[],toasts=[];
let stale=false;
const q={value:'browser'},body={scrollTop:80};
const context=vm.createContext({
  console,URLSearchParams,Math,Set,Map,closingIds:new Set(),failedIconUrls:new Set(),
  last:{status:{clash_api:true},flows:[],ts:30},staleData:()=>stale,
  render:()=>{},tick:()=>{},flashToast:text=>toasts.push(text),
  api:async (url,{body})=>{calls.push({url,ids:[...body.ids]});return {closed:body.ids.length};},
  filter:'overview',homeDimension:'site',liveStyle:'list',processScope:'',connMode:'live',
  closeViewMenu:()=>{},rememberView:()=>{},viewState:()=>({}),
  history:{pushState:()=>{}},window:{scrollTo:()=>{}},
  document:{querySelector:selector=>selector==='.body'?body:null,getElementById:()=>q},
  location:{hash:''},PERIODS:[],RANK_MODES:[],
});
for(const name of ['genericIconSvg','programIconUrl','iconImg','escapeHtml','connectionIds','disconnectButton','closeConnections','viewHash','applyHash','setLiveStyle']) vm.runInContext(source(name),context);
const run=code=>vm.runInContext(code,context);
(async()=>{
  assert.equal(run("genericIconSvg('  Worker.EXE ')"),run("genericIconSvg('worker')"));
  assert.equal(new Set(['a','b','c','d','e','f'].map(p=>run(`genericIconSvg('${p}')`))).size,6);
  assert.ok(!run('genericIconSvg(\'<script>alert(1)</script>\')').includes('<script>'));
  context.last.flows=[{process:'worker',proc_path:'C:\\worker.exe',icon_version:'abc-pending'}];
  assert.equal(run("programIconUrl('worker')"),'');
  assert.ok(run("iconImg('worker')").includes('<svg'));
  context.last.flows[0].icon_version='abc-ready';
  assert.ok(run("programIconUrl('worker')").includes('abc-ready'));
  context.last.flows.push({process:'worker',proc_path:'D:\\worker.exe',icon_version:'def-ready'});
  assert.equal(run("programIconUrl('worker')"),'');
  assert.equal(run("connectionIds([{ids:['a','b','a']},{id:'c'},{id:'x',path:'leak'},{ids:[],id:'b'},{id:'bad,id'},{id:4}]).join(',')"),'a,b,c');
  assert.ok(run("disconnectButton([{path:'leak',id:'x'}])").includes('disabled'));
  assert.ok(run("disconnectButton([{id:'a'}])").includes('断开连接 · 1'));
  stale=true;await run("closeConnections(['a'])");assert.equal(calls.length,0);stale=false;
  context.last.status.clash_api=false;await run("closeConnections(['a'])");assert.equal(calls.length,0);context.last.status.clash_api=true;
  await run('closeConnections([])');assert.equal(calls.length,0);
  context.closingIds.add('a');await run("closeConnections(['a','b'])");assert.equal(calls.length,0);context.closingIds.clear();
  await run("closeConnections(['a','a','b'])");assert.deepEqual(calls.pop().ids,['a','b']);assert.equal(context.closingIds.size,0);
  context.ids=Array.from({length:405},(_,i)=>'id-'+i);await run('closeConnections(ids)');
  assert.deepEqual(calls.splice(0).map(c=>c.ids.length),[200,200,5]);
  assert.equal(toasts.at(-1),'已断开 405 条连接');
  let request=0;
  context.api=async()=>{if(++request===2)throw new Error('timeout');return {closed:198};};
  await run('closeConnections(ids)');
  assert.equal(toasts.at(-1),'已断开 198/405 条，其余断开失败：timeout');
  assert.equal(context.closingIds.size,0);
  let release;
  context.api=()=>new Promise(resolve=>{release=resolve;});
  const pending=run("closeConnections(['a'])");assert.equal(context.closingIds.has('a'),true);
  await run("closeConnections(['a'])");release({closed:1});await pending;assert.equal(context.closingIds.size,0);
  run("setLiveStyle('balls')");assert.equal(context.liveStyle,'balls');assert.equal(q.value,'browser');assert.equal(context.homeDimension,'site');
  assert.ok(run('viewHash()').includes('style=balls'));
  run("setLiveStyle('list','program')");assert.equal(context.liveStyle,'list');assert.equal(context.homeDimension,'program');assert.equal(q.value,'browser');
  context.location.hash='#overview?dimension=site&style=balls';run('applyHash()');assert.equal(context.liveStyle,'balls');assert.equal(context.homeDimension,'site');
  context.location.hash='#overview';run('applyHash()');assert.equal(context.liveStyle,'list');
  console.log('PASS UI controls: stable icons, safe scopes, batched disconnect, retries, view navigation');
})().catch(error=>{console.error(error);process.exitCode=1;});
