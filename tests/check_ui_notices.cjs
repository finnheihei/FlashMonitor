// Production notice rendering and acknowledgement, with no browser, controller or tray.
const assert=require('node:assert/strict'),vm=require('node:vm');
const {script,translator}=require('./ui_locale.cjs');
new vm.Script(script);
function source(name) {
  const start=script.search(new RegExp('(?:async )?function '+name+'\\('));
  assert.ok(start>=0,name);
  const tail=script.slice(start),end=tail.slice(1).search(/\n(?:async )?function /);
  return end<0?tail:tail.slice(0,end+1);
}
function fixture(language) {
  let html='',nodes=[],now=new Date(2026,9,23,15),blocked=false;
  const stored=new Map(),handlers={},requests=[];
  let context;
  const make=(attribute,extra={})=>({attribute,isConnected:true,...extra,
    hasAttribute(name){return name===this.attribute;},
    closest(selector){return selector===`[${this.attribute}]`?this:null;},
    focus(){context.document.activeElement=this;},
  });
  const box={hidden:true,contains:node=>nodes.includes(node),
    querySelector:selector=>nodes.find(node=>selector===`[${node.attribute}]`) || null,
    addEventListener:(event,handler)=>{handlers[event]=handler;},
  };
  const fallback=make('data-fallback');
  context=vm.createContext({tr:translator(language),newAckBusy:false,newAckError:'',displayedNew:[],acknowledgedNew:new Set(),
    Date:class extends Date {constructor(...args){super(...(args.length?args:[now.getTime()]));}},
    last:{status:{storage:{healthy:true}},prefs:{},alerts:[]},filter:'overview',
    prefs:()=>context.last.prefs,labelOf:name=>name,fmtBytes:value=>`${value || 0} B`,
    localStorage:{getItem:key=>{if(blocked) throw Error('blocked');return stored.get(key) || null;},setItem:(key,value)=>{if(blocked) throw Error('blocked');stored.set(key,value);}},
    document:{activeElement:null,getElementById:id=>id==='quota'?box:fallback,querySelector:selector=>selector==='#filters button.on'?fallback:box.querySelector(selector)},
    hold:{pending:new Map()},tick(){},openLeaks(){},
    api:async(url,options)=>{requests.push({url,body:options.body});return {ok:true};},
    patchLive:(_box,value)=>{
      html=value;
      // Deliberately replace nodes: the notice renderer must also recover focus
      // when a conditional sibling makes the production patch replace a control.
      for(const node of nodes) node.isConnected=false;
      nodes=[];
      const group=value.match(/<details\b([^>]*data-notice-group[^>]*)>/);
      if(group) {
        nodes.push(make('data-notice-group',{open:/\bopen\b/.test(group[1]),dataset:{noticeDay:group[1].match(/data-notice-day="([^"]+)"/)[1]}}));
        nodes.push(make('data-notice-summary'),make('data-collapse-notices'));
        if(/<button[^>]*data-ack-new/.test(value)) nodes.push(make('data-ack-new'));
      }
    },
  });
  const run=code=>vm.runInContext(code,context);
  run(script.match(/function newEventKey[^\n]*/)[0]);
  run(script.match(/const NOTICE_STATE_KEY=[\s\S]*?let noticeState=null;/)[0]);
  for(const name of ['escapeHtml','noticeLocalDay','noticeDayState','saveNoticeState','noticeEventKey','collapseNotices','bindNoticeGroup','paintNotices']) run(source(name));
  const clickStart=script.indexOf('document.getElementById("quota").addEventListener("click"');
  const clickEnd=script.indexOf('document.getElementById("quota").addEventListener("keydown"',clickStart);
  run(script.slice(clickStart,clickEnd));
  return {context,run,box,stored,requests,tr:context.tr,get html(){return html;},get group(){return box.querySelector('[data-notice-group]');},
    nextDay(){now=new Date(2026,9,24,0,0,1);},blockStorage(){blocked=true;},
    paint(){run('paintNotices()');},
    collapse(){box.querySelector('[data-collapse-notices]').onclick();},
    toggle(open){this.group.open=open;this.group.ontoggle();},
    reload(){for(const node of nodes) node.isConnected=false;nodes=[];run('noticeState=null');this.paint();},
    ack(){return handlers.click({target:box.querySelector('[data-ack-new]')});},
  };
}
async function check(language) {
  const f=fixture(language),c=f.context;
  const first={kind:'new',process:'browser',ts:1},second={kind:'new',process:'editor',ts:2};
  const total={kind:'total',used:100,limit:50,stats_day:'2026-10-23'};
  const program={kind:'program',process:'browser',used:60,limit:50,stats_day:'2026-10-23'};
  c.last.alerts=[first,second,total,program,{...program}];f.paint();
  assert.equal(f.group.open,true);
  assert.ok(f.html.includes(f.tr`今日 ${4} 项提醒`),'count actual events, not category rows or duplicates');
  assert.equal((f.html.match(/<details/g) || []).length,1);
  assert.equal(c.displayedNew.length,2);
  if(language==='en') assert.ok(!/[\u3400-\u9fff]/.test(f.html),'summary, actions and body are translated');
  f.box.querySelector('[data-ack-new]').focus();f.paint();
  assert.equal(c.document.activeElement,f.box.querySelector('[data-ack-new]'),'refresh preserves acknowledgement focus');
  f.collapse();
  assert.equal(f.group.open,false);
  assert.equal(c.document.activeElement,f.box.querySelector('[data-notice-summary]'));
  assert.equal(JSON.parse([...f.stored.values()][0]).collapsed,true);
  f.paint();f.reload();assert.equal(f.group.open,false,'today collapse survives polling and reload');
  assert.ok(!f.html.includes('class="notice-new-count"'));

  c.last.alerts.push({kind:'new',process:'<script>alert(1)</script>',ts:3});f.paint();
  assert.equal(f.group.open,false,'new alerts do not reopen a collapsed group');
  assert.ok(f.html.includes(f.tr`今日 ${5} 项提醒`));
  assert.ok(f.html.includes(f.tr`新增 ${1} 项提醒`));
  assert.ok(!f.html.includes('<script>alert(1)</script>'));
  f.reload();assert.ok(f.html.includes(f.tr`新增 ${1} 项提醒`),'unseen count survives reload');
  f.toggle(true);f.paint();assert.equal(f.group.open,true,'explicit expansion survives polling');
  assert.ok(!f.html.includes('class="notice-new-count"'));
  f.toggle(false);assert.ok(!f.html.includes('class="notice-new-count"'),'viewed arrivals are no longer marked new');

  c.last.status.storage={healthy:false,pending:true,reset_pending:true};f.paint();
  assert.ok(f.html.indexOf('data-live-key="notice-storage"')<f.html.indexOf('<details'));
  assert.ok(f.html.includes(f.tr('清零尚未全部保存，请保持程序运行；系统会自动重试。')));
  assert.equal(f.group.open,false,'persistent error is visible outside the collapsed group');
  c.filter='settings';f.paint();assert.equal(f.group,null);assert.equal(f.box.hidden,false);assert.equal(c.displayedNew.length,0);
  c.filter='overview';f.paint();assert.equal(f.group.open,false);
  f.nextDay();f.paint();assert.equal(f.group.open,true,'a new local date restores the normal expanded view');

  // An ACK owns the displayed identities, even when another alert arrives during it.
  c.last.status.storage={healthy:true};c.last.alerts=[first,second];f.paint();
  f.box.querySelector('[data-ack-new]').focus();
  let release;
  c.api=async(url,options)=>{f.requests.push({url,body:options.body});return new Promise(resolve=>{release=resolve;});};
  const pending=f.ack();assert.equal(c.newAckBusy,true);
  const later={kind:'new',process:'later',ts:4};c.last.alerts.push(later);f.paint();
  release({ok:true});await pending;
  assert.equal(f.requests[0].body.events.length,2);
  assert.equal(c.newAckBusy,false);assert.equal(c.displayedNew.length,1);assert.equal(c.displayedNew[0].process,'later');
  assert.ok(f.html.includes(f.tr`今日 ${1} 项提醒`));

  // A failure remains readable even if the user collapses while awaiting ACK.
  let reject;
  c.api=()=>new Promise((_,fail)=>{reject=fail;});
  const failed=f.ack();f.collapse();reject(Error('offline'));await failed;
  assert.equal(f.group.open,false);assert.equal(c.displayedNew.length,1);
  assert.ok(f.html.indexOf('data-live-key="notice-ack-error"')<f.html.indexOf('<details'));
  assert.ok(f.html.includes('offline'));f.toggle(true);
  c.api=async()=>({ok:true});await f.ack();assert.equal(f.box.hidden,true);

  c.last.alerts=[{kind:'new',process:'hidden',ts:5}];c.last.prefs.show_new_banner=false;f.paint();assert.equal(f.box.hidden,true);
  c.last.alerts.push(total);f.paint();assert.ok(f.html.includes(f.tr`今日 ${1} 项提醒`));

  const returning=fixture(language);
  returning.context.last.alerts=[total];returning.paint();returning.collapse();
  returning.context.last.alerts=[];returning.paint();assert.equal(returning.box.hidden,true);
  returning.reload();assert.equal(returning.box.hidden,true);
  returning.context.last.alerts=[{...total,used:200}];returning.paint();
  assert.equal(returning.group.open,false,'absence of all alerts does not erase the daily collapse');
  assert.ok(!returning.html.includes('class="notice-new-count"'),'usage updates and returning known keys are not new events');
  returning.context.last.alerts.push(first);returning.paint();
  assert.ok(returning.html.includes(returning.tr`新增 ${1} 项提醒`));
  returning.reload();assert.equal(returning.group.open,false);
  assert.ok(returning.html.includes(returning.tr`新增 ${1} 项提醒`));
  returning.toggle(true);returning.toggle(false);
  returning.context.last.alerts=[{...first,ts:2}];returning.paint();
  assert.ok(returning.html.includes(returning.tr`新增 ${1} 项提醒`),'a newer event for the same app is new');

  const blocked=fixture(language);blocked.blockStorage();blocked.context.last.alerts=[first];blocked.paint();blocked.collapse();blocked.paint();
  assert.equal(blocked.group.open,false,'storage denial must not break in-session collapse');
  blocked.nextDay();blocked.paint();assert.equal(blocked.group.open,true);
  console.log('PASS notices ('+language+'): grouped counts, persistent daily collapse, unseen arrivals, expansion/focus, day rollover, visible errors, ACK races and storage denial');
}
(async()=>{for(const language of ['zh-CN','en']) await check(language);})().catch(error=>{console.error(error);process.exitCode=1;});
