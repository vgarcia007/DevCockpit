const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const code = fs.readFileSync(process.argv[2], 'utf8');
const syncCode = code.slice(0, code.indexOf('\n(() => {'));
const restoreCode = code.slice(code.indexOf('if (viewRestore) {'), code.indexOf('const releaseNodes'));
const restoreKey = 'cockpit-view-update-position';
const url = 'http://localhost:7777/issues?repo=One&q=bug#results';
const now = Date.now();
function environment(saved, href=url, blocked=false) {
  const values = new Map(saved ? [[restoreKey, JSON.stringify(saved)]] : []);
  const eventHandlers = {};
  const intervals = [];
  const storage = {
    getItem: key => { if (blocked) throw Error('Storage disabled'); return values.get(key) || null; },
    setItem: (key, value) => { if (blocked) throw Error('Storage disabled'); values.set(key, value); },
    removeItem: key => { if (blocked) throw Error('Storage disabled'); values.delete(key); },
  };
  const makeElement = () => ({textContent:'', hidden:true, dataset:{}, addEventListener(name, callback) {this[name]=callback;}});
  const sources = ['github','otrs','zabbix'].map(id=>({id, name:id, state:'healthy', running:false,
    last_success:'baseline', next_sync_at:new Date(now+60000).toISOString(), details:'Last sync successful'}));
  const rows = sources.map(source=>Object.assign(makeElement(), {dataset:{syncSource:source.id},
    value:makeElement(), details:makeElement(), querySelector(selector) {return selector==='.sync-details' ? this.details : this.value;}}));
  const sidebar = {scrollTop:120};
  const notice = makeElement();
  const button = makeElement();
  const marker = {dataset:{lastSuccess:'baseline', otrsLastSuccess:'baseline', zabbixRevision:'baseline', serverTime:new Date(now).toISOString()}};
  const nodes = {'sync-label':marker, 'view-update-notice':notice, 'view-update-button':button,
    'sync-sources-data':{textContent:JSON.stringify(sources)}};
  const location = {href, pathname:'/issues', reload() {state.reloads++;}};
  const state = {reloads:0, restoreCalls:0, eventHandlers, selectedText:'User-selected text', focus:'search input', sources, rows, sidebar, notice, button, values};
  const data = {server_time:new Date(now).toISOString(), sources, last_success:'baseline',
    otrs_last_success:'baseline', zabbix_revision:'baseline', running:false};
  const context = vm.createContext({URL, Map, Date, JSON, Number, Math, location, sessionStorage:storage,
    document:{getElementById:id=>nodes[id], querySelectorAll:selector=>selector==='[data-sync-source]' ? rows : [],
      querySelector:selector=>selector==='.sidebar-nav' ? sidebar : null, documentElement:{style:{scrollBehavior:''}}},
    window:{scrollX:12, scrollY:700, scrollTo(x,y) {state.restored=[x,y];state.restoreCalls++;}, location,
      addEventListener(name, callback) {eventHandlers[name]=callback;}},
    setInterval:callback=>intervals.push(callback), requestAnimationFrame:callback=>callback(),
    fetch:async()=> {if(state.offline) throw Error('Offline'); return {ok:true, json:async()=>data};},
  });
  vm.runInContext(syncCode, context);
  state.poll=intervals[0];
  state.data=data;
  state.context=context;
  return state;
}
(async () => {
  const state=environment();
  await state.poll();
  assert.equal(state.notice.hidden,true);
  for(const key of ['last_success','otrs_last_success','zabbix_revision']) {
    state.data[key]='new revision';
    await state.poll();
    assert.equal(state.notice.hidden,false);
    assert.equal(state.reloads,0);
    assert.equal(state.selectedText,'User-selected text');
    assert.equal(state.focus,'search input');
  }
  state.data.sources[1].state='error';
  await state.poll();
  assert.equal(state.rows[1].value.textContent,'Error');
  state.offline=true;
  await state.poll();
  assert.equal(state.rows[0].value.textContent,'Unavailable');
  assert.equal(state.notice.hidden,false);
  assert.equal(state.reloads,0);
  state.values.set('cockpit-auto-filter-focus','old filter position');
  state.button.click();
  assert.equal(state.reloads,1);
  assert.equal(state.values.has('cockpit-auto-filter-focus'),false);
  const saved=JSON.parse(state.values.get(restoreKey));
  assert.equal(saved.url,url);
  assert.equal(saved.scrollY,700);
  const restored=environment(saved);
  vm.runInContext(restoreCode,restored.context);
  assert.deepEqual(restored.restored,[12,700]);
  assert.equal(restored.sidebar.scrollTop,120);
  assert.equal(restored.values.has(restoreKey),false);
  restored.eventHandlers.wheel();
  restored.eventHandlers.load();
  assert.equal(restored.restoreCalls,1); // Late loading must not override fresh user interactions.
  for(const [position,href] of [[{...saved,savedAt:now-11000},url], [saved,url+'&other=1'],
    [{...saved,scrollY:'invalid'},url]]) {
    const ignored=environment(position,href);
    vm.runInContext(restoreCode,ignored.context);
    assert.equal(ignored.restored,undefined);
  }
  const noStorage=environment(null,url,true);
  noStorage.button.click();
  assert.equal(noStorage.reloads,1);
  console.log('View update regression checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
