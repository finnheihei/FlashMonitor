// Production summary/filter/render/polling logic with an in-memory DOM and API.
// Never opens a browser, contacts a controller, or changes network configuration.
const assert=require('node:assert/strict'),vm=require('node:vm');
const {script,translator}=require('./ui_locale.cjs');
const language=process.env.FM_TEST_LANGUAGE || 'zh-CN',tr=translator(language);
new vm.Script(script);
function source(name) {
  const start=script.search(new RegExp('(?:async )?function '+name+'\\('));assert.ok(start>=0,name);
  const tail=script.slice(start),end=tail.slice(1).search(/\n(?:async )?function /);
  return end<0?tail:tail.slice(0,end+1);
}
function element() {
  return {value:'',checked:false,hidden:false,children:[],dataset:{},style:{},attributes:{},scrollTop:0,
    classList:{toggle(){},contains:()=>false},setAttribute(key,value){this.attributes[key]=value;},
    focus(){document.activeElement=this;},hasAttribute(key){return Object.hasOwn(this.attributes,key);}};
}
const elements=new Map(),get=id=>{if(!elements.has(id)) elements.set(id,element());return elements.get(id);};
const handlers={},document={activeElement:null,documentElement:element(),getElementById:get,
  querySelector:get,addEventListener(type,handler){handlers[type]=handler;}};
const requests=[],pending=[];
const context=vm.createContext({console,Math,Date,Set,Map,Object,URLSearchParams,document,tr,
  UI_LANGUAGE:language,UI_LOCALE:language==='en'?'en-US':'zh-CN',filter:'rank',period:'7d',rankMode:'program',
  trafficScope:'all',rankSort:{key:null,direction:'desc'},processScope:'',connectionSite:'',connectionSubdomains:false,
  connMode:'live',leakOnly:false,connectionRoute:'',searchOpen:true,homeDimension:'site',selectedSite:'keep.example',
  liveStyle:'list',setPane:'general',homeScope:'all',homeScopeError:'',NAMES:{app:'Example App'},opened:new Set(),
  well:{sel:'app'},primaryViews:{},box:element(),mbox:element(),pbox:element(),
  FILTERS:[['overview'],['rank']],RANK_MODES:[['program',tr('程序')],['site',tr('网站')],['node',tr('节点')]],PERIODS:[['7d']],
  history:{replaceState(state,unused,hash){this.state=state;this.hash=hash;}},window:{scrollY:0},
  prefs:()=>({ignored:[],pinned:[]}),isIgnored:()=>false,isPinned:()=>false,iconImg:()=>'',SVG_CHEV:'',
  arrangeRows:rows=>rows,periodLabel:()=>tr('7 天'),loadingHtml:()=>'loading',staleData:()=>false,
  setHtml:(el,markup)=>{el.innerHTML=markup;},paintChrome(){},wellStop(){},drawChart(){},
  poll:{busy:false,again:false,force:false,timer:0},manualRefresh:{busy:false,generation:0},
  prefCommitVersion:0,prefOverrides:new Map(),pollError:'',collectError:'',snapshotAt:0,
  clearTimeout(){},schedulePoll(){},closingIds:new Set(),last:{rank:{},status:{}},
  api(url){requests.push(url);return new Promise(resolve=>pending.push(resolve));},
});
const run=code=>vm.runInContext(code,context);
for(const name of ['query','hit','labelOf','nameHtml','paintSearchContext','rankSearchActions','resultFilters','resultFilterRecovery',
  'byteValue','statTotal','statProxy','statDirect','statRejected','statUnknown','statOtherLabel','fmtBytes','pct','coverageLabel',
  'statValue','rankSortKey','sortRankRows','rankHeader','statsRows','siteType','moreButton','rankName','coverageNote',
  'renderRank','renderConns','tableWrap','splitBar','escapeHtml','flowHost','matchesSite','flowRouteKind','routeStatus','routePill',
  'paintContext','clearResultFilters','viewState','viewHash','rememberView','render','snapshotPath','mergeSnapshot','tick','resumePolling']) run(source(name));
const clickStart=script.indexOf('document.addEventListener("click",e=>{');
run(script.slice(clickStart,script.indexOf('(function bindChart()',clickStart)));
function clear() {handlers.click({target:{closest:()=>({hasAttribute:key=>key==='data-clear-filters'})}});}
const pack={rows:[{process:'app',download:1000,proxy_down:250,direct_down:650},
  {process:'direct',download:200,direct_down:200}],
  sites:{rows:[{site:'example.com',download:1000,proxy_down:250,direct_down:650}]},
  nodes:{rows:[{node:'Demo node',download:900}]}};
context.last={rank:{'7d':pack},status:{},flows:[],closed:[]};

// The selected scope controls the main number; remaining metrics still compare.
for(const [scope,label,value,comparisons] of [
  ['all','总用量',1200,[['代理',250],['直连',850]]],
  ['proxy','代理用量',250,[['直连',650],['合计',1000]]],
  ['direct','直连用量',850,[['代理',250],['合计',1200]]],
]) {
  context.trafficScope=scope;
  const rendered=run('renderRank()'),primary=rendered.match(/<div class="usage-primary">([\s\S]*?)<\/div>/)[1];
  assert.ok(primary.includes(tr(label)));assert.ok(primary.includes('<strong>'+run(`fmtBytes(${value})`)+'</strong>'));
  for(const [comparison,amount] of comparisons) assert.ok(rendered.includes(`<span class="metric-label">${tr(comparison)}</span><b>${run(`fmtBytes(${amount})`)}</b>`));
  assert.equal(run('rankSortKey()'),scope==='all'?'total':scope,'scope sort semantics remain unchanged');
}
context.rankMode='site';context.trafficScope='direct';
assert.ok(run('renderRank()').includes('<strong>'+run('fmtBytes(650)')+'</strong>'));
context.rankMode='node';
assert.ok(run('renderRank()').includes(tr('节点流量')));assert.ok(run('renderRank()').includes('<strong>'+run('fmtBytes(900)')+'</strong>'));
assert.equal(run('rankSortKey()'),'total');

// Tags describe only conditions that actually affect this page/dimension.
context.processScope='app';context.connectionSite='example.com';context.connectionSubdomains=true;
context.leakOnly=true;context.connectionRoute='unknown';context.connMode='closed';get('q').value=' Demo <node> ';
assert.equal(run('resultFilters().map(item=>item[0]).join()'),'search');
context.rankMode='site';assert.equal(run('resultFilters().map(item=>item[0]).join()'),'search,traffic');
context.rankMode='program';assert.equal(run('resultFilters().map(item=>item[0]).join()'),'search,program,traffic');
run('paintContext()');assert.ok(get('context-tools').innerHTML.includes('Demo &lt;node&gt;'));assert.ok(!get('context-tools').innerHTML.includes('Demo <node>'));
assert.ok(get('context-tools').innerHTML.includes(tr('查看程序、网站与节点用了多少流量')));
assert.ok(get('context-tools').innerHTML.includes('data-clear-scope'));
let empty=run('renderRank()');assert.ok(empty.includes(tr('没有匹配的结果，请调整搜索或筛选。')));assert.ok(empty.includes('data-clear-filters'));
context.rankSort={key:'proxy',direction:'asc'};get('show-ignored').checked=true;clear();
assert.equal(run('resultFilters().length'),0);assert.equal(context.period,'7d');assert.equal(context.rankMode,'program');
assert.equal(context.homeDimension,'site');assert.equal(context.selectedSite,'keep.example');assert.equal(context.rankSort.key,'proxy');
assert.equal(context.trafficScope,'all');assert.equal(context.processScope,'');assert.equal(context.connectionSite,'');
assert.equal(context.connectionSubdomains,false);assert.equal(context.leakOnly,false);assert.equal(context.connectionRoute,'');
assert.equal(get('q').value,'');assert.equal(document.activeElement,get('context-tools'));assert.equal(requests.length,0);
assert.equal(get('show-ignored').checked,true,'clearing filters preserves the show-ignored display preference');
assert.ok(get('main').innerHTML.includes('data-rank-row="app"'));assert.equal(context.history.state.search,'');

// Truly empty source data and filtered-out results must have different messages.
context.filter='conns';context.connMode='live';context.processScope='missing';
context.rows=[{process:'app',remote:'example.com:443',outbound:'proxy'}];
empty=run('renderConns(rows)');assert.ok(empty.includes(tr('没有匹配的连接')));assert.ok(empty.includes('data-clear-filters'));
empty=run('renderConns([])');assert.ok(empty.includes(tr('没有连接')));assert.ok(!empty.includes(tr('没有匹配的连接')));
context.processScope='';context.connectionSite='missing.example';assert.ok(run('renderConns(rows)').includes(tr('没有匹配的连接')));
context.connectionSite='';context.leakOnly=true;assert.ok(run('renderConns(rows)').includes(tr('没有匹配的连接')));
context.leakOnly=false;context.connectionRoute='unknown';assert.ok(run('renderConns(rows)').includes(tr('没有匹配的连接')));
context.connectionRoute='';assert.ok(!run('renderConns([])').includes('data-clear-filters'));
context.connMode='closed';empty=run('renderConns([])');assert.ok(empty.includes(tr('暂无最近结束的连接')));
assert.ok(empty.includes(tr('清除筛选并查看实时连接')));

(async()=>{
  // Reset during an outstanding closed-snapshot request must queue a live fetch.
  context.processScope='missing';context.connectionSite='missing.example';context.connectionSubdomains=true;
  context.leakOnly=true;context.connectionRoute='unknown';context.trafficScope='proxy';get('q').value='not found';
  assert.equal(run('resultFilters().map(item=>item[0]).join()'),'search,program,site,state');
  run('paintContext()');assert.ok(get('context-tools').innerHTML.includes(tr('（含子域名）')));
  context.last.flows=[{process:'cached-live',remote:'cached.example:443',outbound:'direct'}];
  const oldRequest=run('tick(true)');assert.equal(requests[0],'/api/snapshot?view=closed');
  clear();assert.equal(context.connMode,'live');assert.equal(context.trafficScope,'all');
  assert.equal(context.poll.again,true);assert.equal(context.poll.force,true);
  assert.ok(get('main').innerHTML.includes('cached-live'));assert.equal(context.history.state.connMode,'live');
  assert.equal(context.history.hash,'#conns/live');assert.equal(run('resultFilters().length'),0);
  pending.shift()({closed:[{process:'old-closed',remote:'closed.example:443'}]});await oldRequest;
  assert.equal(requests[1],'/api/snapshot?view=conns');
  assert.ok(!get('main').innerHTML.includes('old-closed'),'late closed response never renders as live data');
  pending.shift()({flows:[{process:'fresh-live',remote:'live.example:443',outbound:'proxy'}]});
  await Promise.resolve();await Promise.resolve();
  assert.ok(get('main').innerHTML.includes('fresh-live'));assert.ok(!get('main').innerHTML.includes('old-closed'));
  assert.equal(context.poll.busy,false);assert.equal(requests.length,2);
  context.filter='rank';context.last.rank['7d']={rows:[]};
  assert.ok(run('renderRank()').includes(tr('这个时段还没有流量记录。')));
  console.log(`PASS filter recovery (${language}): scoped summaries, effective tags, distinct empty states, one-click reset, live snapshot queue`);
})().catch(error=>{console.error(error);process.exitCode=1;});
