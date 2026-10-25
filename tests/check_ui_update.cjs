// Run the production update flow with an in-memory DOM and API; no downloads/restarts.
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
const nodes=new Map(['btn-update','update-install','update-dialog','update-status','update-error','update-notes'].map(id=>[id,{
  id,textContent:'',hidden:false,disabled:false,dataset:{},classList:{toggle(){}},open:false,
  showModal(){this.open=true;},close(){this.open=false;},
}]));
const requests=[],toasts=[],timers=[];
const newer={current:'1.0.0',latest:'1.1.0',newer:true,notes:'Release notes <script>literal text</script>',install_supported:true};
const context=vm.createContext({tr,updateInfo:null,updateState:{busy:false,phase:'',error:'',restarting:false},
  settingsCache:{frozen:true},filter:'overview',document:{getElementById:id=>nodes.get(id)},render(){},
  flashToast:message=>toasts.push(message),setTimeout:fn=>timers.push(fn),location:{reload(){throw Error('must not restart');}},
  api:async(path,options={})=>{requests.push({path,options});return {...newer};},
});
for (const name of ['updateInstallError','updateErrorText','paintUpdate','doUpdate','installUpdate','peekUpdate']) vm.runInContext(source(name),context);
const run=code=>vm.runInContext(code,context);
(async()=>{
  const aboutButton=nodes.get('btn-update');
  nodes.delete('btn-update');
  await run('doUpdate()');
  assert.equal(nodes.get('update-dialog').open,true);
  assert.equal(requests.length,1);
  assert.equal(requests[0].options.body,undefined,'checking never downloads');
  assert.equal(nodes.get('update-install').hidden,false);
  assert.equal(nodes.get('update-notes').textContent,newer.notes,'release text is not inserted as HTML');
  assert.equal(timers.length,0);
  nodes.set('btn-update',aboutButton);run('paintUpdate()');
  assert.equal(aboutButton.textContent,tr('有新版本'),'about button reflects a background check when the page opens');

  context.error='更新前保存失败：disk full；采集已恢复，可重试';
  if(process.env.FM_TEST_LANGUAGE==='en') assert.ok(!/[\u3400-\u9fff]/.test(run('updateErrorText(error)')),'failure recovery must be translated');
  context.error='未能启动更新重启：launch denied；程序累计采集已恢复，但网站明细暂不可用：disk full';
  if(process.env.FM_TEST_LANGUAGE==='en') assert.ok(!/[\u3400-\u9fff]/.test(run('updateErrorText(error)')),'partial recovery must be translated');

  let release;
  context.api=(path,options)=>{requests.push({path,options});return new Promise(resolve=>{release=resolve;});};
  const install=run('installUpdate()');
  assert.equal(nodes.get('update-install').disabled,true);
  await run('installUpdate()');
  await run('doUpdate()');
  assert.equal(requests.length,2,'double click and reopening do not duplicate installation');
  assert.deepEqual(Object.keys(requests[1].options.body),[]);
  release({ok:false,error:'Save failed; collection continues'});await install;
  assert.equal(context.updateState.restarting,false);
  assert.equal(nodes.get('update-install').disabled,false,'failed install can be retried');
  assert.ok(nodes.get('update-error').textContent.includes('Save failed'));
  assert.equal(timers.length,0);

  context.api=async()=>({ok:true,restarting:false});
  await run('installUpdate()');
  assert.equal(context.updateState.restarting,false,'success without restart confirmation is insufficient');
  assert.equal(timers.length,0);

  context.api=async()=>({...newer,install_supported:false,install_error:'Source mode: use GitHub'});
  await run('doUpdate()');
  assert.equal(nodes.get('update-install').hidden,true);
  context.api=()=>{throw Error('source mode must not POST');};
  await run('installUpdate()');
  assert.ok(nodes.get('update-error').textContent.includes('Source mode'));

  context.updateInfo={...newer};context.settingsCache={frozen:false};run('paintUpdate()');
  assert.equal(nodes.get('update-install').hidden,true,'source-mode fallback applies before metadata support');
  if(process.env.FM_TEST_LANGUAGE==='en') assert.ok(!/[\u3400-\u9fff]/.test(nodes.get('update-error').textContent));

  context.settingsCache={frozen:true};context.api=async()=>({ok:true,restarting:true,latest:'1.1.0'});
  await run('installUpdate()');
  assert.equal(context.updateState.restarting,true);
  assert.equal(nodes.get('update-install').disabled,true);
  assert.equal(nodes.get('update-status').textContent,tr('已确认保存，正在重启…'));
  assert.equal(timers.length,1);
  console.log('PASS update flow: background check without about button, release preview, explicit install, failure retry, restart confirmation, source guard');
})().catch(error=>{console.error(error);process.exitCode=1;});
