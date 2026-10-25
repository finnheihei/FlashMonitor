// Execute the production reset flow against a mock API; never clear user statistics.
const assert=require('node:assert/strict'),vm=require('node:vm');
const {script,translator}=require('./ui_locale.cjs');
const tr=translator(process.env.FM_TEST_LANGUAGE || 'zh-CN');
new vm.Script(script);
function source(name) {
  const start=script.search(new RegExp('(?:async )?function '+name+'\\('));
  assert.ok(start>=0,name);
  const tail=script.slice(start),end=tail.slice(1).search(/\n(?:async )?function /);
  return end<0?tail:tail.slice(0,end+1);
}
const toasts=[],notices={hidden:true,contains:()=>false,querySelector:()=>null};
let requests=0,confirms=0,refreshes=0,noticeHtml='';
const context=vm.createContext({tr,resetBusy:false,last:{status:{storage:{healthy:true}}},
  opened:new Set(['browser']),confirm:()=>{confirms++;return true;},render(){},
  api:async()=>{requests++;return {ok:true,persisted:true,pending:false};},
  flashToast:text=>toasts.push(text),tick:()=>{refreshes++;},filter:'settings',
  document:{getElementById:()=>notices},hold:{pending:new Map()},
  patchLive:(_node,html)=>{noticeHtml=html;},
});
for(const name of ['escapeHtml','resetTotals','bindNoticeGroup','paintNotices']) vm.runInContext(source(name),context);
const run=code=>vm.runInContext(code,context);
(async()=>{
  context.confirm=()=>false;
  await run('resetTotals()');assert.equal(requests,0);assert.equal(refreshes,0);
  context.confirm=()=>{confirms++;return true;};
  let release;
  context.api=()=>{requests++;return new Promise(resolve=>release=resolve);};
  const first=run('resetTotals()');
  assert.equal(context.resetBusy,true);
  await run('resetTotals()');assert.equal(requests,1);assert.equal(confirms,1);
  const pendingError='清零已在当前会话生效，但尚未全部写入磁盘；请保持程序运行，系统会自动重试。';
  release({ok:false,persisted:false,pending:true,error:pendingError});await first;
  assert.equal(context.resetBusy,false);assert.equal(context.last.status.storage.reset_pending,true);
  assert.equal(context.opened.size,0);assert.equal(toasts.at(-1),tr(pendingError));
  assert.ok(!toasts.includes(tr('已清零')),'pending disk writes never show success');
  await run('resetTotals()');assert.equal(requests,1,'pending reset cannot erase new traffic');
  run('paintNotices()');assert.equal(notices.hidden,false);
  assert.ok(noticeHtml.includes(tr('清零尚未全部保存，请保持程序运行；系统会自动重试。')));
  if(process.env.FM_TEST_LANGUAGE==='en') assert.ok(!/[\u3400-\u9fff]/.test(noticeHtml));

  // A fresh persisted snapshot clears the durable pending notice and permits a new reset.
  context.last.status.storage={healthy:true,reset_pending:false};run('paintNotices()');
  assert.equal(notices.hidden,true);
  context.opened.add('browser');
  const deniedError='正在退出或准备更新，未清零；请稍后重试。';
  context.api=async()=>({ok:false,persisted:false,pending:false,busy:true,error:deniedError});
  await run('resetTotals()');assert.equal(context.opened.size,1,'rejected reset preserves view state');
  assert.equal(toasts.at(-1),tr('清零失败 ')+tr(deniedError));
  if(process.env.FM_TEST_LANGUAGE==='en') assert.ok(!/[\u3400-\u9fff]/.test(toasts.at(-1)));

  context.api=async()=>({ok:true});await run('resetTotals()');
  assert.ok(toasts.at(-1).includes(tr('清零结果未确认，请检查统计状态后再试。')));
  context.api=async()=>{throw Error('offline');};await run('resetTotals()');
  assert.equal(context.resetBusy,false);assert.equal(context.opened.size,1);
  assert.ok(toasts.at(-1).includes('offline'));

  context.api=async()=>({ok:true,persisted:true,pending:false});await run('resetTotals()');
  assert.equal(toasts.at(-1),tr('已清零'));assert.equal(context.opened.size,0);
  console.log('PASS reset: durable acknowledgement, persistent pending notice, repeat guard, rejected and network failures');
})().catch(error=>{console.error(error);process.exitCode=1;});
