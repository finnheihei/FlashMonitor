// Usage/search/menu/export regressions against production functions and handlers.
// Runs entirely in memory; never opens a browser or accesses a live controller.
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const language=process.env.FM_TEST_LANGUAGE || 'zh-CN';
const tr=require('./ui_locale.cjs').translator(language);
const html=fs.readFileSync(path.join(__dirname,'..','ui.html'),'utf8');
const script=html.split('<script>')[1].split('</script>')[0];
new vm.Script(script);
function source(name) {
  const start=script.search(new RegExp('(?:async )?function '+name+'\\('));assert.ok(start>=0,name);
  const tail=script.slice(start),end=tail.slice(1).search(/\n(?:async )?function /);
  return end<0?tail:tail.slice(0,end+1);
}
const handlers={},routes=[],downloads=[];
let ignored=['background'],focus='',remembered=0;
function element(id='') {
  return {value:'',checked:false,hidden:false,attributes:{},dataset:{},style:{},isConnected:true,
    setAttribute(key,value){this.attributes[key]=value;},focus(){focus=id;},
    getBoundingClientRect(){return {right:400,bottom:100};},addEventListener(type,handler){handlers[id+':'+type]=handler;},
    hasAttribute(key){return Object.hasOwn(this.attributes,key);}};
}
function button(markup) {
  const out=element('button');
  for(const [,key,value] of markup.matchAll(/([\w-]+)(?:="([^"]*)")?/g)) {
    out.attributes[key]=value || '';
    if(key.startsWith('data-')) out.dataset[key.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=value || '';
  }
  return out;
}
const elements=Object.fromEntries(['q','show-ignored','context-tools','feedback','btn-export'].map(id=>[id,element(id)]));
const menu=element('menu');menu.hidden=true;menu.offsetWidth=210;menu.offsetHeight=100;menu.querySelector=()=>element('menu-first');
const document={getElementById:id=>elements[id],addEventListener(type,handler){handlers['document:'+type]=handler;},
  createElement(){return {click(){downloads.at(-1).filename=this.download;}};}};
const context=vm.createContext({console,Blob,Set,Map,Math,Date,Number,Object,document,programMenu:menu,
  tr,UI_LOCALE:language==='en'?'en-US':'zh-CN',period:'today',rankMode:'program',trafficScope:'all',rankSort:{key:null,direction:'desc'},
  filter:'rank',processScope:'',connMode:'live',connectionSite:'',connectionSubdomains:false,leakOnly:false,connectionRoute:'',
  homeScope:'all',homeDimension:'program',homeScopeError:'',liveStyle:'list',selectedSite:'',searchOpen:true,
  RANK_MODES:[['program',tr('程序')],['site',tr('网站')],['node',tr('节点')]],
  NAMES:{Weixin:tr('微信'),WeChatAppEx:tr('微信')},opened:new Set(),last:{rank:{}},
  prefs:()=>({ignored,pinned:[]}),isPinned:()=>false,iconImg:()=>'',nameHtml:p=>p,SVG_CHEV:'',
  periodLabel:()=>tr('今天'),loadingHtml:()=>'loading',staleData:()=>false,
  setHtml:(el,markup)=>{el.innerHTML=markup;},rememberView:()=>{remembered++;},setTimeout:()=>{},
  URL:{createObjectURL(blob){downloads.push({blob});return 'blob:test';},revokeObjectURL(){}},
  menuProgram:'',menuPath:'',menuSite:'',menuSiteSubdomains:false,menuAnchor:null,innerWidth:1280,innerHeight:820,
  connectionDialog:{open:false},ruleDialog:{open:false},draftLibrary:{open:false},ipv6Dialog:{open:false},processIdentities:()=>[],
  gotoFilter:(...args)=>{routes.push(args);},
});
const run=code=>vm.runInContext(code,context);
for(const name of ['query','hit','labelOf','isIgnored','paintSearchContext','chooseRankMode','rankSearchActions','resultFilters','resultFilterRecovery',
  'byteValue','statTotal','statProxy','statDirect','statRejected','statUnknown','statOtherLabel','fmtBytes','pct','coverageLabel',
  'statValue','rankSortKey','sortRankRows','rankHeader','arrangeRows','statsRows','siteType','moreButton','rankName',
  'coverageNote','renderRank','tableWrap','splitBar','escapeHtml','paintContext','clearResultScope','openSiteMenu','closeProgramMenu','openConnections']) run(source(name));
run(source('openProgramMenu').split("\ndocument.getElementById('main').addEventListener")[0]);
context.render=()=>run('paintSearchContext()');
const clickStart=script.indexOf('document.addEventListener("click",e=>{');
run(script.slice(clickStart,script.indexOf('(function bindChart()',clickStart)));
const menuStart=script.indexOf("programMenu.addEventListener('click',async event=>{");
run(script.slice(menuStart,script.indexOf("document.addEventListener('click',event=>",menuStart)));
const exportStart=script.indexOf('document.getElementById("btn-export").onclick');
run(script.slice(exportStart,script.indexOf('function query()',exportStart)));
const click=b=>handlers['document:click']({target:{closest:()=>b}});
const metric=(process,title,total)=>({process,title,upload:0,download:total,proxy_down:total,sites:[],sites_period:'today'});
const pack={rows:[{...metric('msedge','Edge',100),sites:[{site:'example.com',download:100,proxy_down:100}]},metric('background','Background',900)],
  coverage:{since:1700000000},
  sites:{rows:[{site:'example.com',download:1200,proxy_down:1200}],coverage:{since:1700000000},
    program_details:{'example.com':{rows:[metric('msedge','Edge',100),metric('background','Background',900)],note:'Partial attribution'}}},
  nodes:{rows:[{node:'Demo',download:1200,upload:0}],coverage:{since:1700000000}}};
context.pack=pack;context.last={rank:{today:pack}};

// Ignoring hides program rows only; incomplete attribution must not reduce totals.
assert.equal(run('statsRows(pack).map(r=>r.process).join()'),'msedge');
assert.equal(run('statsRows(pack).reduce((n,r)=>n+statTotal(r),0)'),100);
context.rankMode='site';context.opened.add('rank:site:example.com');
let rendered=run('renderRank()');
assert.equal(run('statsRows(pack)[0].download'),1200);
assert.ok(rendered.includes('data-ignored-app'));
assert.ok(rendered.includes(tr('网站与节点用量包含已忽略程序的流量。')));
assert.ok(rendered.includes('data-site-row="background"'));
context.rankMode='node';assert.equal(run('statsRows(pack)[0].download'),1200);
assert.ok(run('renderRank()').includes('data-ignore-coverage'));
context.rankMode='program';elements['show-ignored'].checked=true;
assert.equal(run('statsRows(pack).length'),2);assert.ok(run('renderRank()').includes('data-ignored-app'));
elements['show-ignored'].checked=false;ignored=[];
assert.equal(run('statsRows(pack).length'),2);assert.ok(!run('renderRank()').includes('data-ignored-app'));
ignored=['background'];

// Labels describe the actual search scope; empty results can switch dimension.
for(const [mode,label] of [['program','搜索程序名称或进程名'],['site','搜索网站域名或 IP'],['node','搜索节点名称']]) {
  context.rankMode=mode;run('paintSearchContext()');
  assert.equal(elements.q.placeholder,tr(label));assert.equal(elements.q.attributes['aria-label'],tr(label));
}
context.filter='overview';run('paintSearchContext()');assert.equal(elements.q.placeholder,tr('搜索程序或网站'));
context.filter='rank';context.rankMode='program';elements.q.value='example.com';
assert.equal(run('statsRows(pack).length'),0);rendered=run('renderRank()');
assert.ok(rendered.includes('data-search-dimension="site"'));assert.ok(rendered.includes('data-clear-search'));
click(button('data-search-dimension="site"'));
assert.equal(context.rankMode,'site');assert.equal(elements.q.value,'example.com');assert.equal(run('statsRows(pack).length'),1);
assert.equal(elements.q.placeholder,tr('搜索网站域名或 IP'));assert.equal(focus,'q');
click(button('data-clear-search'));assert.equal(elements.q.value,'');

// Clearing a scope must preserve the other scope, query and route restrictions.
context.filter='conns';context.connectionSite='example.com';context.connectionSubdomains=true;context.processScope='msedge';
context.leakOnly=true;context.connectionRoute='unknown';elements.q.value=':443';run('paintContext()');
assert.ok(elements['context-tools'].innerHTML.includes('data-clear-scope'));
assert.ok(elements['context-tools'].innerHTML.includes('data-clear-site'));
click(button('data-clear-scope'));
assert.equal(context.processScope,'');assert.equal(context.connectionSite,'example.com');assert.equal(context.connectionSubdomains,true);
assert.equal(elements.q.value,':443');assert.equal(context.leakOnly,true);assert.equal(context.connectionRoute,'unknown');assert.equal(focus,'context-tools');
run('paintContext()');assert.ok(elements['context-tools'].innerHTML.includes('data-clear-site'));
context.processScope='msedge';click(button('data-clear-site'));
assert.equal(context.connectionSite,'');assert.equal(context.connectionSubdomains,false);assert.equal(context.processScope,'msedge');assert.equal(elements.q.value,':443');

// A site's menu and name in an app detail navigate to the same process/site.
context.filter='rank';context.rankMode='program';context.processScope='';elements.q.value='';context.opened.add('rank:program:msedge');
rendered=run('renderRank()');
const nameMarkup=rendered.match(/<button[^>]*data-site-connections="example.com"[^>]*>/)[0];
const menuMarkup=rendered.match(/<button[^>]*data-site-menu="example.com"[^>]*>/)[0];
click(button(nameMarkup));click(button(menuMarkup));
(async()=>{
  await handlers['menu:click']({target:{closest:()=>({dataset:{menuAction:'site-connections'}})}});
  assert.equal(JSON.stringify(routes[0]),JSON.stringify(routes[1]));
  assert.equal(routes[1][2].process,'msedge');assert.equal(routes[1][2].subdomains,true);
  click(button(run("moreButton('', 'example.com', false)")));
  await handlers['menu:click']({target:{closest:()=>({dataset:{menuAction:'site-connections'}})}});
  assert.equal(routes.at(-1)[2].process,'');assert.equal(routes.at(-1)[2].subdomains,false);

  // The inverse drilldown must keep the website when opening a program menu.
  context.rankMode='site';context.opened.add('rank:site:example.com');rendered=run('renderRank()');
  const programName=rendered.match(/<button[^>]*data-site-connections="example.com"[^>]*data-site-process="msedge"[^>]*>/)[0];
  const programMenu=rendered.match(/<button[^>]*data-program-menu="msedge"[^>]*>/)[0];
  click(button(programName));const inverseName=routes.at(-1);
  click(button(programMenu));
  await handlers['menu:click']({target:{closest:()=>({dataset:{menuAction:'connections'}})}});
  assert.equal(JSON.stringify(routes.at(-1)),JSON.stringify(inverseName));
  assert.equal(routes.at(-1)[2].site,'example.com');assert.equal(routes.at(-1)[2].subdomains,true);
  click(button(run("moreButton('msedge', '', false, 'exact.example')")));
  await handlers['menu:click']({target:{closest:()=>({dataset:{menuAction:'connections'}})}});
  assert.equal(routes.at(-1)[2].site,'exact.example');assert.equal(routes.at(-1)[2].subdomains,false);
  click(button(run("moreButton('msedge')")));
  await handlers['menu:click']({target:{closest:()=>({dataset:{menuAction:'connections'}})}});
  assert.equal(routes.at(-1)[2].process,'msedge');assert.equal(routes.at(-1)[2].site,undefined,'ordinary program menus retain all websites');

  // Export real CSV blobs: distinguish duplicate labels, preserve filter, quote safely.
  ignored=[];context.filter='rank';context.rankMode='program';context.trafficScope='all';context.processScope='';
  context.last={rank:{today:{rows:[metric('Weixin','',1024),metric('WeChatAppEx','',2048),metric('=Formula','Danger "app"',512)],coverage:{since:1700000000}}}};
  elements['btn-export'].onclick();
  let csv=await downloads.at(-1).blob.text();
  assert.ok(csv.includes('"'+tr('显示名称')+'","'+tr('进程名')+'"'));
  assert.ok(csv.includes('"'+tr('微信')+'","Weixin"'));assert.ok(csv.includes('"'+tr('微信')+'","WeChatAppEx"'));
  assert.ok(csv.includes('"'+tr('代理（字节）')+'"'));assert.ok(csv.includes('"'+tr('合计（字节）')+'"'));
  assert.ok(csv.includes('"Danger ""app""","\'=Formula"'));
  assert.ok(csv.includes('2023-11-14T22:13:20.000Z'));
  assert.match(downloads.at(-1).filename,/^FlashMonitor-program-today-all-\d{4}-\d{2}-\d{2}T/);
  elements.q.value='WeChatAppEx';elements['btn-export'].onclick();csv=await downloads.at(-1).blob.text();
  assert.equal(csv.split('\r\n').length,2);assert.ok(csv.includes('WeChatAppEx'));assert.ok(!csv.includes('Weixin'));
  elements.q.value='';context.last={rank:{today:pack}};context.rankMode='site';elements['btn-export'].onclick();csv=await downloads.at(-1).blob.text();
  assert.ok(csv.includes('"example.com","1200"'));assert.ok(!csv.includes(tr('进程名')));
  context.rankMode='node';context.trafficScope='direct';elements['btn-export'].onclick();csv=await downloads.at(-1).blob.text();
  assert.ok(csv.includes('"'+tr('下载（字节）')+'"'));assert.ok(csv.includes('"Demo","1200","0","1200"'));
  assert.ok(downloads.at(-1).filename.includes('-node-today-all-'));
  assert.ok(remembered>0);
  context.rankMode='program';context.processScope='';context.trafficScope='all';context.rankSort={key:null,direction:'desc'};elements.q.value='';
  context.prefs=()=>({ignored:[],pinned:['tiny']});context.isPinned=p=>p==='tiny';
  context.pinPack={rows:[metric('tiny','Tiny',100),metric('huge','Huge',1000000)]};
  assert.equal(run('statsRows(pinPack).map(r=>r.process).join()'),'huge,tiny');
  assert.equal(run('arrangeRows(pinPack.rows).map(r=>r.process).join()'),'tiny,huge','live pin ordering remains available');
  console.log(`PASS usage scope (${language}): ignore coverage, independent filters, scoped menus, search recovery, CSV identity/units/dates/escaping`);
})().catch(error=>{console.error(error);process.exitCode=1;});
