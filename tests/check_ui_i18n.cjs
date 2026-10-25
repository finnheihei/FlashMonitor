const assert=require('node:assert/strict'),vm=require('node:vm');
const {localeContext,translator,script}=require('./ui_locale.cjs');
new vm.Script(script);
const en=translator(null),zh=translator('zh-CN');
assert.equal(en('实时'),'Live');assert.equal(en('用量'),'Usage');
assert.equal(zh('实时'),'实时');assert.equal(zh('屏蔽 IPv6'),'屏蔽 IPv6');
assert.equal(en('<button title="返回列表">返回列表</button>'),'<button title="Back to list">Back to list</button>');
const userValue='代理 · 中文节点 <script> & 未知';
assert.equal(en`网站 ${userValue}`,'Websites '+userValue);
assert.equal(zh`网站 ${userValue}`,'网站 '+userValue);
assert.equal(en('unrecognized text / 中文自定义内容'),'unrecognized text / 中文自定义内容');
assert.equal(vm.runInContext('UI_LANGUAGE',localeContext('invalid')),'en');
assert.equal(vm.runInContext('UI_LOCALE',localeContext('zh-CN')),'zh-CN');
assert.equal(vm.runInContext('Object.keys(UI_MESSAGES).filter(key=>/[\\u3400-\\u9fff]/.test(tr(key))).length',localeContext('en')),0);
assert.equal(vm.runInContext('UI_LANGUAGE',localeContext(null,{localStorage:{getItem(){throw Error('blocked');}}})),'en');
const state={fm:true,filter:'rank',expanded:['rank:site:中文.example'],search:'中文',scroll:123,rankSort:{key:'proxy',direction:'desc'}};
let stored,reloads=0;
const history={state:{...state},replaceState(next){this.state=next;}};
const context=localeContext('en',{
  history,localStorage:{getItem:()=>'en',setItem:(key,value)=>{stored=[key,value];}},
  document:{querySelector:()=>null},filter:'rank',rememberView:()=>{},
  location:{hash:'#rank?period=today',reload:()=>reloads++},confirm:()=>true,
});
vm.runInContext('switchUiLanguage()',context);
assert.equal(reloads,1);assert.deepEqual(stored,['flashmonitor.ui.language','zh-CN']);
assert.deepEqual(history.state.expanded,state.expanded);assert.equal(history.state.search,'中文');assert.equal(history.state.scroll,123);
assert.equal(history.state.fmLanguageSwitch,true);
assert.equal(vm.runInContext('UI_LANGUAGE',localeContext('en',{history})),'zh-CN');
const next=localeContext('zh-CN',{history:{state:{fmLanguageSwitch:true,uiLanguage:'en'}}});
assert.equal(vm.runInContext('UI_LANGUAGE',next),'en');
// Storage denial must not prevent the immediate switch or corrupt the view.
context.localStorage.setItem=()=>{throw Error('blocked');};
vm.runInContext('switchUiLanguage()',context);assert.equal(reloads,2);
context.filter='settings';context.confirm=()=>false;
vm.runInContext('switchUiLanguage()',context);assert.equal(reloads,2);
// Static translation runs before data arrives and must not alter input values.
const text={nodeValue:'正在启动…',parentElement:{closest:()=>false}};
const code={nodeValue:'const label="实时";',parentElement:{closest:()=>true}};
const field={value:'中文.example',attributes:{placeholder:'搜索程序或网站'},hasAttribute(k){return k in this.attributes;},getAttribute(k){return this.attributes[k];},setAttribute(k,v){this.attributes[k]=v;}};
const root={querySelectorAll:()=>[field]};
context.NodeFilter={SHOW_TEXT:4};context.document.createTreeWalker=()=>{const nodes=[text,code];return {nextNode:()=>nodes.shift()};};context.root=root;
vm.runInContext('translateStaticUi(root)',context);
assert.equal(text.nodeValue,'Starting…');assert.equal(code.nodeValue,'const label="实时";');
assert.equal(field.attributes.placeholder,'Search apps or websites');assert.equal(field.value,'中文.example');
assert.ok(script.includes('const restoreLanguageView=!!history.state?.fmLanguageSwitch'));
assert.ok(script.includes('if(restoreSavedView || restoreLanguageView) {'));
console.log('PASS localization: English default, Chinese, template data safety, persistence, view preservation, storage denial, unsaved form guard, static attributes');
