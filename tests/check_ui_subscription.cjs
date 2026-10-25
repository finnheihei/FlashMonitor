// Production subscription rendering and DOM patches, without a browser or controller.
const assert=require('node:assert/strict'),vm=require('node:vm');
const {script,translator}=require('./ui_locale.cjs');
const language=process.env.FM_TEST_LANGUAGE || 'zh-CN',tr=translator(language);
new vm.Script(script);
function source(name) {
  const start=script.search(new RegExp('(?:async )?function '+name+'\\('));
  assert.ok(start>=0,name);
  const tail=script.slice(start),end=tail.slice(1).search(/\n(?:async )?function |\ndocument\./);
  return end<0?tail:tail.slice(0,end+1);
}
// Only the DOM operations used by patchLive are needed; no implementation is mocked.
class Element {
  constructor(name,text) {this.nodeName=name.toUpperCase();this.nodeType=name==='#text'?3:1;this.nodeValue=text;this.childNodes=[];this.attrs=new Map();}
  get attributes(){return [...this.attrs].map(([name,value])=>({name,value}));}
  get dataset(){return Object.fromEntries([...this.attrs].filter(([k])=>k.startsWith('data-')).map(([k,v])=>[k.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase()),v]));}
  setAttribute(k,v){this.attrs.set(k,String(v));} getAttribute(k){return this.attrs.get(k) ?? null;}
  hasAttribute(k){return this.attrs.has(k);} removeAttribute(k){this.attrs.delete(k);}
  get id(){return this.getAttribute('id');}
  get open(){return this.hasAttribute('open');} set open(v){v?this.setAttribute('open',''):this.removeAttribute('open');}
  get disabled(){return this.hasAttribute('disabled');} set disabled(v){v?this.setAttribute('disabled',''):this.removeAttribute('disabled');}
  get children(){return this.childNodes.filter(n=>n.nodeType===1);}
  get firstChild(){return this.childNodes[0] || null;}
  get nextSibling(){return this.parentNode?.childNodes[this.parentNode.childNodes.indexOf(this)+1] || null;}
  get isConnected(){return this===main || this===hero || !!this.parentNode?.isConnected;}
  get classList(){return {contains:c=>(this.getAttribute('class') || '').split(' ').includes(c),toggle:(c,on)=>{const all=new Set((this.getAttribute('class') || '').split(' ').filter(Boolean));on?all.add(c):all.delete(c);this.setAttribute('class',[...all].join(' '));}};}
  contains(n){while(n){if(n===this)return true;n=n.parentNode;}return false;}
  append(...nodes){for(const n of nodes)this.insertBefore(n,null);}
  insertBefore(n,before){n.remove();const i=before?this.childNodes.indexOf(before):this.childNodes.length;this.childNodes.splice(i,0,n);n.parentNode=this;}
  remove(){if(this.parentNode){const siblings=this.parentNode.childNodes;siblings.splice(siblings.indexOf(this),1);this.parentNode=null;}}
  replaceWith(n){this.parentNode.insertBefore(n,this);this.remove();}
  cloneNode(deep){const n=new Element(this.nodeName,this.nodeValue);n.nodeType=this.nodeType;n.attrs=new Map(this.attrs);if(deep)n.append(...this.childNodes.map(c=>c.cloneNode(true)));return n;}
  matches(s){return s.startsWith('.')?this.classList.contains(s.slice(1)):s.startsWith('#')?this.id===s.slice(1):s.startsWith('[')?this.hasAttribute(s.slice(1,-1)):this.nodeName===s.toUpperCase();}
  querySelectorAll(s){return this.children.flatMap(c=>[...(c.matches(s)?[c]:[]),...c.querySelectorAll(s)]);}
  querySelector(s){return this.querySelectorAll(s)[0] || null;}
  focus(){document.activeElement=this;}
  set innerHTML(html){
    for(const child of [...this.childNodes])child.remove();
    const stack=[this];
    for(const token of html.match(/<[^>]*>|[^<]+/g) || []) {
      if(token.startsWith('</')){stack.pop();continue;}
      if(token.startsWith('<')) {
        const tag=token.match(/^<([\w-]+)/)[1],node=new Element(tag);
        const attrs=token.slice(tag.length+1,-1);
        for(const [,key,value] of attrs.matchAll(/([^\s=/>]+)(?:="([^"]*)")?/g))node.setAttribute(key,value || '');
        stack.at(-1).append(node);
        if(!/^(input|br|img|hr)$/i.test(tag) && !token.endsWith('/>'))stack.push(node);
      } else stack.at(-1).append(new Element('#text',token));
    }
  }
}
const main=new Element('main'),hero=new Element('section');main.setAttribute('id','main');hero.setAttribute('id','ov-hero');
const document={activeElement:null,createElement:name=>new Element(name),getElementById:id=>id==='main'?main:id==='ov-hero'?hero:main.querySelector('#'+id),querySelectorAll:s=>[...main.querySelectorAll(s),...hero.querySelectorAll(s)]};
const calls=[],timers=[],toasts=[];
const now=Date.UTC(2026,9,23),GB=1024**3;
class Clock extends Date {static now(){return now;}}
const context=vm.createContext({
  tr,UI_LOCALE:language==='en'?'en-US':'zh-CN',Date:Clock,console,document,Node:{TEXT_NODE:3,ELEMENT_NODE:1},
  last:{sub:{label:'Example <plan>',used:25*GB,total:100*GB,expire:(now+3*86400000)/1000},live:{}},settingsCache:null,
  prefs:()=>({show_airport:true}),SVG_REFRESH:'',hold:{btn:null},menuAnchor:null,programMenu:{hidden:true},
  well:{sel:''},liveStyle:'list',visibleHomePrograms:()=>[],rememberView:()=>{},wellStop:()=>{},wellStart:()=>{},
  ensureWellPage:()=>{},syncWellBalls:()=>{},paintWellSide:()=>{},livePrograms:()=>[],staleData:()=>false,
  liveSplit:()=>({pd:0,tot:0}),renderLiveList:()=>'<div>Programs</div>',
  setHtml:(el,html)=>{if(el){el.innerHTML=html;el._html=html;}},
  api:async(url,options)=>{calls.push({url,options});return {ok:true};},
  setTimeout:(fn,ms)=>{timers.push({fn,ms});},tick:force=>calls.push({tick:force}),flashToast:message=>toasts.push(message),
});
for(const name of ['byteValue','fmtBytes','fmtQuota','fmtSpeed','pct','unitParts','bigNum','escapeHtml','airportOn','airportData','airportStatus','airportExpiry','subscriptionSummary','airportCell','patchLiveNode','patchLive','paintSubscription','liveSummary','paintOverview','paintHero','refreshSub'])vm.runInContext(source(name),context);
const run=code=>vm.runInContext(code,context);
(async()=>{
  const summary=run('subscriptionSummary()'),compact=summary.split('</summary>')[0];
  assert.ok(compact.includes(tr('剩余')+' <b>75.00 GB</b>'));
  assert.ok(compact.includes(tr('已用')+' <b>25.00 GB</b>'));
  assert.ok(compact.includes(tr`${3} 天后到期`));
  assert.ok(compact.includes('Example &lt;plan&gt;'));
  assert.equal((summary.match(/class="cell air"/g) || []).length,1,'expanded details reuse one airport card');
  assert.equal((summary.match(/data-refresh-sub/g) || []).length,1);
  run('paintOverview(true)');
  const slot=document.getElementById('live-subscription'),details=slot.querySelector('details'),button=slot.querySelector('button');
  assert.ok(details);assert.equal(details.open,false);
  details.open=true;button.focus();
  context.last.sub.used=30*GB;run('paintOverview(false)');
  assert.equal(slot.querySelector('details'),details);assert.equal(details.open,true);
  assert.equal(slot.querySelector('button'),button);assert.equal(document.activeElement,button);
  assert.ok(slot._html.includes('70.00 GB'),'poll updates data while details stay open');
  details.open=false;context.last.sub.used=31*GB;run('paintOverview(false)');assert.equal(details.open,false,'user collapse is retained');
  context.settingsCache={show_airport:false};run('paintOverview(true)');assert.equal(slot.childNodes.length,0);
  run('paintHero(true)');assert.ok(hero.classList.contains('no-air'));assert.ok(!hero._html.includes('class="cell air"'));
  context.settingsCache={show_airport:true};context.last.sub=null;run('paintOverview(true)');assert.equal(slot.childNodes.length,0);
  const snapshot=context.last;context.last=null;run('paintHero(true)');assert.ok(hero.classList.contains('no-air'));assert.ok(!hero._html.includes('class="cell air"'));context.last=snapshot;
  context.last.sub={used:0,total:0};assert.equal(run('subscriptionSummary()'),'');
  context.last.sub={used:5*GB,total:0};
  assert.ok(run('subscriptionSummary()').includes('5.00 GB'));assert.ok(!run('subscriptionSummary()').split('</summary>')[0].includes(tr('剩余')));
  context.last.sub={used:110*GB,total:100*GB,expire:(now-86400000)/1000};
  assert.ok(run('subscriptionSummary()').includes(tr('已到期')));assert.ok(run('subscriptionSummary()').includes(tr('剩余')+' <b>0</b>'));
  assert.ok(run('airportCell()').includes('meter full'));assert.ok(!run('subscriptionSummary()').includes('-10.0'));
  context.last.sub={used:NaN,total:Infinity};assert.equal(run('subscriptionSummary()'),'');
  context.last.sub={used:5*GB,total:20*GB,expire:'invalid'};assert.ok(!run('subscriptionSummary()').includes('Invalid Date'));
  context.liveStyle='balls';run('paintOverview(true)');
  assert.equal((hero._html.match(/class="cell air"/g) || []).length,1);assert.ok(!hero._html.includes('subscription-summary'));assert.equal(hero.classList.contains('no-air'),false);
  // Explicit refresh is shared with the hero; identical data must not leave it disabled.
  context.liveStyle='list';run('paintOverview(true)');
  const current=slot.querySelector('button'),currentDetails=slot.querySelector('details');currentDetails.open=true;current.focus();context.button=current;
  let release;context.api=(url,options)=>{calls.push({url,options});return new Promise(resolve=>release=resolve);};
  const pending=run('refreshSub(button)');assert.equal(current.disabled,true);
  run('paintSubscription()');assert.equal(slot.querySelector('button'),current);assert.equal(current.disabled,true);
  await run('refreshSub(button)');assert.equal(calls.filter(c=>c.url).length,1,'concurrent refreshes are coalesced');
  release({ok:true});await pending;assert.equal(current.disabled,true);assert.equal(currentDetails.open,true);
  assert.equal(timers.at(-1).ms,1200);timers.at(-1).fn();assert.equal(calls.at(-1).tick,true);
  assert.equal(calls[0].url,'/api/refresh-sub');assert.deepEqual(Object.keys(calls[0].options.body),[]);
  context.last.sub.status='error';run('paintSubscription()');assert.equal(current.disabled,false);
  context.api=async()=>{throw Error('offline');};await run('refreshSub(button)');
  assert.equal(current.disabled,false);assert.equal(toasts.at(-1),tr('刷新账期失败：')+'offline');
  context.api=async()=>({error:'busy'});await run('refreshSub(button)');assert.equal(current.disabled,false);assert.equal(toasts.at(-1),tr('刷新账期失败：')+'busy');
  context.api=async()=>({ok:true});await run('refreshSub(button)');assert.equal(current.disabled,true);assert.equal(currentDetails.open,true);
  context.last.sub={status:'error',updated_at:0};
  const failed=run('subscriptionSummary()');
  assert.ok(failed.includes('data-refresh-sub'));assert.ok(failed.includes(tr('重试')));
  assert.ok(failed.includes(tr('无法读取用量，请检查订阅地址或稍后重试。')));
  assert.ok(!failed.includes('75.00 GB'));assert.ok(!failed.includes('<summary>'),'first failure exposes retry without expanding');
  context.last.sub={status:'loading',updated_at:0};assert.match(run('airportCell()'),/data-refresh-sub="1" disabled/);
  context.last.sub={used:5*GB,total:20*GB,status:'error',updated_at:now/1000};
  const stale=run('subscriptionSummary()');assert.ok(stale.includes(tr('刷新失败，显示上次数据')));assert.ok(stale.includes(tr('更新于 ')));
  context.last.sub.status='cached';assert.ok(run('subscriptionSummary()').includes(tr('缓存数据')));
  if(language==='en') assert.ok(!/[\u3400-\u9fff]/.test(run('subscriptionSummary()')));
  console.log('PASS UI subscription: shared list/balls quota, expiry, visibility, polling disclosure/focus, refresh retry and errors ('+language+')');
})().catch(error=>{console.error(error);process.exitCode=1;});
