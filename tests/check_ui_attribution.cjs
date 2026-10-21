// Logic regressions for UI attribution; runs without a browser or a live controller.
// node tests/check_ui_attribution.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '..', 'ui.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];
function source(name) {
  const start = script.indexOf('function ' + name + '(');
  assert.ok(start >= 0, name);
  const tail = script.slice(start);
  const end = tail.slice(1).search(/\n(?:async )?function /);
  return end < 0 ? tail : tail.slice(0, end + 1);
}
const context = vm.createContext({
  tr:require('./ui_locale.cjs').translator(process.env.FM_TEST_LANGUAGE || 'zh-CN'),UI_LOCALE:'en-US',
  console, period:'today', rankMode:'program',trafficScope:'proxy',rankSort:{key:null,direction:'desc'},processScope:'',
  homeScope:'all',query:()=>'',hit:()=>true,isIgnored:()=>false,isPinned:()=>false,
  arrangeRows:rows=>rows,labelOf:(p,title)=>title || p,iconImg:()=>'',SVG_CHEV:'',
  opened:new Set(),periodLabel:()=>'今天',loadingHtml:()=>'loading',
  last:{flows:[],rank:{}},
});
for(const name of ['byteValue','statTotal','statProxy','statDirect','statRejected','statUnknown','statOtherLabel',
  'fmtBytes','pct','coverageLabel','statValue','rankSortKey','sortRankRows','rankHeader','statsRows','siteType',
  'moreButton','rankName','coverageNote','renderRank','tableWrap','splitBar','escapeHtml','flowHost','matchesSite','liveSites']) {
  vm.runInContext(source(name),context);
}
const evaluate=code=>vm.runInContext(code,context);
assert.equal(evaluate("flowHost({remote:'[2001:db8::12]:443'})"),'2001:db8::12');
assert.equal(evaluate("flowHost({remote:'2001:db8::12'})"),'2001:db8::12');
assert.equal(evaluate("matchesSite({remote:'api.EXAMPLE.com:443'},'example.com',true)"),true);
assert.equal(evaluate("matchesSite({remote:'badexample.com:443'},'example.com',true)"),false);
assert.equal(evaluate("matchesSite({remote:'api.example.com:443'},'example.com',false)"),false);
const programs=[
  {process:'direct',download:9000,direct_down:9000,proxy_down:0},
  {process:'small',download:8000,direct_down:7990,proxy_down:10},
  {process:'proxy',download:7000,direct_down:0,proxy_down:7000},
];
context.pack={rows:programs};
assert.equal(evaluate('statsRows(pack).map(r=>r.process).join(",")'),'proxy,small');
evaluate('rankSort={key:"direct",direction:"asc"}');
assert.equal(evaluate('statsRows(pack).map(r=>r.process).join(",")'),'proxy,small');
evaluate('trafficScope="all";rankSort={key:null,direction:"desc"}');
assert.equal(evaluate('statsRows(pack).map(r=>r.process).join(",")'),'direct,small,proxy');
context.last={rank:{today:{rows:programs,sites:{rows:[{site:'shared.example',download:50,proxy_down:50}],
  program_details:{'shared.example':{rows:[{process:'browser',download:30,proxy_down:30},{process:'editor',download:20,proxy_down:20}],
    coverage:{since:1,retention_limited:true},note:'Limited detail'}}}}}};
evaluate('rankMode="site";opened.add("rank:site:shared.example")');
const rendered=evaluate('renderRank()');
assert.ok(rendered.includes('rank:site:shared.example:browser'));
assert.ok(rendered.includes('rank:site:shared.example:editor'));
assert.ok(rendered.includes('data-site-process="browser"'));
assert.ok(rendered.includes('data-site-connections="shared.example"'));
assert.ok(rendered.includes('data-program-menu="editor"'));
assert.ok(rendered.includes('Limited detail'));
context.last.flows=[{process:'browser',remote:'shared.example:443',download:25,upload:5,outbound:'proxy'},
  {process:'editor',remote:'shared.example:443',download:40,upload:0,outbound:'direct'},
  {process:'other',remote:'other.example:443',download:10,upload:0,outbound:'unknown'}];
context.flowRouteKind=f=>f.outbound;
assert.equal(evaluate('liveSites()[0].programs.size'),2);
assert.equal(evaluate('statTotal(liveSites()[0])'),70);
evaluate('homeScope="proxy"');
assert.equal(evaluate('liveSites().length'),1);
assert.equal(evaluate('statTotal(liveSites()[0])'),70);
assert.equal(evaluate('liveSites()[0].programs.size'),2);
console.log('PASS UI attribution: host matching, proxy sorting, inverse drilldown, route filtering');
