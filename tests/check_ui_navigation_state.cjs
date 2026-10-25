// Exercise production navigation and event handlers with an isolated DOM/history.
// No browser, backend, or user settings are accessed.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {translator}=require('./ui_locale.cjs');
const testLanguage=process.env.FM_TEST_LANGUAGE || 'zh-CN';
const translate=translator(testLanguage);
const html=fs.readFileSync(path.join(__dirname,'..','ui.html'),'utf8');
const script=html.split('<script>')[1].split('</script>')[0];
new vm.Script(script);
const copy=value=>value==null?value:JSON.parse(JSON.stringify(value));
function source(name) {
  const start=script.search(new RegExp('(?:async )?function '+name+'\\('));
  assert.ok(start>=0,name);
  const tail=script.slice(start),end=tail.slice(1).search(/\n(?:async )?function /);
  return end<0?tail:tail.slice(0,end+1);
}
function createSession({hash='',state=null,lastPeriod='today'}={}) {
  const events=new Map(),frames=[];
  function element() {
    return {value:'',checked:false,children:[],dataset:{},open:false,
      classList:{toggle(){}},addEventListener(type,handler){this[type]=handler;},
      appendChild(child){this.children.push(child);},focus(){document.activeElement=this;},
      blur(){document.activeElement=null;},contains(){return false;}};
  }
  const elements=Object.fromEntries(['q','main','show-ignored','filters','brand-ver','view-menu'].map(id=>[id,element()]));
  const body={scrollTop:0};
  const document={activeElement:null,documentElement:{classList:{contains:()=>true}},
    getElementById:id=>elements[id],createElement:element,
    querySelector:selector=>selector==='.body'?body:null,querySelectorAll:()=>[]};
  const location={hash};
  const entries=[{state:copy(state),hash}],history={state:copy(state),
    replaceState(next,unused,nextHash){this.state=copy(next);location.hash=nextHash;entries[entries.length-1]={state:this.state,hash:nextHash};},
    pushState(next,unused,nextHash){this.state=copy(next);location.hash=nextHash;entries.push({state:this.state,hash:nextHash});}};
  const window={scrollY:0,scrollTo(x,y){this.scrollY=y;},addEventListener(type,handler){events.set(type,handler);}};
  const context=vm.createContext({console,URLSearchParams,Set,Map,Object,Math,document,location,history,window,
    tr:translate,UI_LANGUAGE:testLanguage,filter:'overview',period:'today',rankMode:'program',trafficScope:'proxy',
    processScope:'',connMode:'live',leakOnly:false,connectionRoute:'',liveStyle:'list',setPane:'general',
    homeDimension:'program',selectedSite:'',connectionSite:'',connectionSubdomains:false,homeScope:'all',
    opened:new Set(),rankSort:{key:null,direction:'desc'},well:{sel:''},searchOpen:false,primaryViews:{},
    FILTERS:[['overview',translate('实时')],['rank',translate('用量')]],
    PERIODS:[['1h'],['today'],['7d'],['30d'],['all']],RANK_MODES:[['program'],['site'],['node']],
    pbox:element(),settingsCache:null,closeViewMenu:()=>{},render:()=>{},tick:()=>{},peekUpdate:()=>{},
    loadSettings:async()=>{},flashToast:()=>{},requestAnimationFrame:callback=>frames.push(callback),
    api:async url=>{assert.equal(url,'/api/settings');return {last_period:lastPeriod,home_scope:'all'};},
    connectionDialog:{open:false},ruleDialog:{open:false},draftLibrary:{open:false},ipv6Dialog:{open:false},programMenu:{hidden:true},
  });
  const run=code=>vm.runInContext(code,context);
  for(const name of ['loadSettings','returnConnections','chooseRankMode','viewState','viewHash','rememberView','gotoPrimaryView','gotoFilter','openConnections','applyHash','applyViewState','restoreViewPosition','restoreView']) run(source(name));
  run(script.slice(script.indexOf('const box = document.getElementById("filters");'),script.indexOf('const RANK_MODES =')));
  run(script.split('\n').find(line=>line.startsWith('document.getElementById("q").addEventListener("input"')));
  run(script.split('\n').find(line=>line.startsWith('window.addEventListener("pagehide"')));
  run(script.slice(script.lastIndexOf('window.addEventListener("keydown",e=>{'),script.indexOf('(async function boot()')));
  const flush=()=>{while(frames.length) frames.shift()();};
  return {context,run,elements,body,history,location,entries,events,flush,
    async boot(){await run(script.slice(script.indexOf('(async function boot()')));flush();},
    async tab(id){const index=id==='overview'?0:1;await elements.filters.children[index].onclick();flush();},
    async key(key){events.get('keydown')({key,target:{tagName:'BODY'},defaultPrevented:false,preventDefault(){}});await Promise.resolve();flush();},
    input(value){elements.q.value=value;context.searchOpen=true;elements.q.input();},
    state(){return copy(run('viewState()'));}};
}
(async()=>{
  const fresh=createSession();await fresh.boot();await fresh.tab('rank');
  assert.equal(fresh.context.period,'today');assert.equal(fresh.context.rankMode,'program');assert.equal(fresh.context.trafficScope,'proxy');
  const savedPreference=createSession({lastPeriod:'7d'});await savedPreference.boot();await savedPreference.tab('rank');
  assert.equal(savedPreference.context.period,'7d','first Usage visit honors the saved time period');

  const session=createSession();await session.boot();await session.tab('rank');
  session.run("period='30d';rankMode='site';trafficScope='all';rankSort={key:'direct',direction:'asc'};opened.add('rank:site:example.com')");
  session.elements['show-ignored'].checked=true;session.input('example');session.body.scrollTop=240;
  await session.tab('overview');
  assert.equal(session.elements.q.value,'','Live has its own search');
  session.run("homeDimension='site';liveStyle='balls';selectedSite='live.example';well.sel='browser'");
  session.input('live.example');session.body.scrollTop=90;
  await session.tab('rank');
  assert.equal(session.context.period,'30d');assert.equal(session.context.rankMode,'site');assert.equal(session.context.trafficScope,'all');
  assert.deepEqual(copy(session.context.rankSort),{key:'direct',direction:'asc'});
  assert.equal(session.elements.q.value,'example');assert.equal(session.context.searchOpen,true);
  assert.equal(session.elements['show-ignored'].checked,true);assert.equal(session.body.scrollTop,240);
  assert.equal(session.context.opened.has('rank:site:example.com'),true);
  const entryCount=session.entries.length;await session.tab('rank');
  assert.equal(session.entries.length,entryCount,'clicking the active tab does not reset or add history');
  await session.key('1');
  assert.equal(session.context.homeDimension,'site');assert.equal(session.context.liveStyle,'balls');
  assert.equal(session.context.selectedSite,'live.example');assert.equal(session.context.well.sel,'browser');
  assert.equal(session.elements.q.value,'live.example');assert.equal(session.body.scrollTop,90);
  await session.key('2');assert.equal(session.context.period,'30d');assert.equal(session.elements.q.value,'example');

  // Reload carries both tabs, including input changed without navigating away.
  session.input('edited.example');session.body.scrollTop=310;session.events.get('pagehide')();
  const reloaded=createSession({state:session.history.state,hash:session.location.hash});await reloaded.boot();
  assert.equal(reloaded.context.period,'30d');assert.equal(reloaded.context.rankMode,'site');
  assert.equal(reloaded.elements.q.value,'edited.example');assert.equal(reloaded.body.scrollTop,310);
  assert.equal(reloaded.context.opened.has('rank:site:example.com'),true);
  await reloaded.key('1');assert.equal(reloaded.elements.q.value,'live.example');assert.equal(reloaded.context.liveStyle,'balls');
  await reloaded.key('2');assert.equal(reloaded.elements.q.value,'edited.example');

  // Explicit detail navigation still applies its parameters and clears search.
  await reloaded.run("gotoFilter('rank','',{process:'msedge',period:'today',rankMode:'program',traffic:'all'})");
  assert.equal(reloaded.context.processScope,'msedge');assert.equal(reloaded.context.period,'today');
  assert.equal(reloaded.context.rankMode,'program');assert.equal(reloaded.context.trafficScope,'all');assert.equal(reloaded.elements.q.value,'');
  await reloaded.run("gotoFilter('conns','',{site:'example.com',process:'msedge',subdomains:true})");
  assert.equal(reloaded.context.connectionSite,'example.com');assert.equal(reloaded.context.connectionSubdomains,true);
  assert.equal(reloaded.context.processScope,'msedge');assert.equal(reloaded.elements.q.value,'');
  const previous=reloaded.entries.at(-2);
  reloaded.history.state=copy(previous.state);reloaded.location.hash=previous.hash;
  reloaded.run('restoreView()');reloaded.flush();
  assert.equal(reloaded.context.filter,'rank');assert.equal(reloaded.context.processScope,'msedge');
  assert.equal(reloaded.context.period,'today');assert.equal(reloaded.context.trafficScope,'all');
  await reloaded.tab('overview');assert.equal(reloaded.elements.q.value,'live.example');
  await reloaded.tab('rank');assert.equal(reloaded.context.processScope,'msedge');
  const deep=createSession({hash:'#rank?period=1h&dimension=node&traffic=direct',lastPeriod:'30d'});await deep.boot();
  assert.equal(deep.context.period,'1h');assert.equal(deep.context.rankMode,'node');assert.equal(deep.context.trafficScope,'direct');

  // The pagehide saver must not overwrite the language selected just before reload.
  const nextLanguage=testLanguage==='en'?'zh-CN':'en';
  reloaded.history.state={...reloaded.history.state,fmLanguageSwitch:true,uiLanguage:nextLanguage};
  reloaded.events.get('pagehide')();
  assert.equal(reloaded.history.state.uiLanguage,nextLanguage);assert.equal(reloaded.history.state.fmLanguageSwitch,true);
  const en=translator('en');assert.equal(en('有代理流量'),'With proxy traffic');assert.equal(en('有直连流量'),'With direct traffic');
  assert.ok(html.includes('<option value="proxy">有代理流量</option>'));
  assert.ok(html.includes('<option value="direct">有直连流量</option>'));
  // A deep-linked usage page is the first browser entry: the app still knows its origin.
  const detail=createSession({hash:'#rank?period=today'});await detail.boot();
  detail.run("processScope='browser';rankSort={key:'direct',direction:'asc'};opened.add('rank:program:browser')");
  detail.input('browser');detail.body.scrollTop=185;
  detail.run("chooseRankMode('program')");
  assert.equal(detail.context.processScope,'browser');assert.equal(detail.context.rankSort.key,'direct');
  assert.equal(detail.elements.q.value,'browser');
  await detail.run("gotoFilter('conns','',{site:'example.com',process:'browser'})");
  assert.equal(detail.entries.length,2);
  const detailReload=createSession({hash:detail.location.hash,state:detail.history.state});await detailReload.boot();
  detailReload.run('returnConnections()');detailReload.flush();
  assert.equal(detailReload.context.filter,'rank');assert.equal(detailReload.context.processScope,'browser');
  assert.equal(detailReload.elements.q.value,'browser');assert.equal(detailReload.body.scrollTop,185);
  assert.equal(detailReload.context.rankSort.direction,'asc');assert.ok(detailReload.context.opened.has('rank:program:browser'));
  const standalone=createSession({hash:'#conns/live'});await standalone.boot();
  standalone.run('returnConnections()');standalone.flush();assert.equal(standalone.context.filter,'overview');
  console.log(`PASS navigation (${testLanguage}): per-tab state, keyboard, reload, saved default, explicit details/deep links, language reload, filter labels`);
})().catch(error=>{console.error(error);process.exitCode=1;});
