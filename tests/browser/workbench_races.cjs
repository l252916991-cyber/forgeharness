// Execute the shipped page script with controlled fetch ordering. No npm packages.
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');
const {webcrypto, randomUUID} = require('node:crypto');
const html = readFileSync(join(__dirname, '../../src/forgeharness/applications/commerce/workbench.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const tick = () => new Promise(resolve => setImmediate(resolve));
// Poll on the event loop until the page reaches a state; the deadline only bounds a broken page.
async function until(condition, ms=10000){const end=Date.now()+ms;while(!condition()&&Date.now()<end)await tick();}
const reply = (data, status=200) => ({status, ok:status<400, json:async()=>data});

function harness({deferInitial=false, deferDigest=false}={}) {
  const elements = new Map(), storage = new Map(), deferred = new Map(), calls = [];
  let account = 'A', digestResolve;
  function element(id) {
    if(!elements.has(id)) elements.set(id, {
      value:'',textContent:'',checked:false,fields:new Map(),handlers:{},
      addEventListener(name,handler){this.handlers[name]=handler;},
      querySelectorAll(){return [];}, reset(){this.fields=new Map();}
    });
    return elements.get(id);
  }
  function defer(path) {
    let resolve, reject;
    const promise = new Promise((yes,no)=>{resolve=yes;reject=no;});
    const entry={promise,resolve:(data,status=200)=>resolve(reply(data,status)),reject};
    deferred.set(path,entry);return entry;
  }
  const initialPending=deferInitial?defer('/identity/me'):null;
  const context = vm.createContext({
    document:{getElementById:element},AbortController,TextEncoder,Uint8Array,
    sessionStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},
    crypto:{randomUUID,subtle:{digest:(...args)=>deferDigest?new Promise(resolve=>{digestResolve=resolve;}):webcrypto.subtle.digest(...args)}},
    FormData:class{constructor(form){this.fields=new Map(form.fields);}get(key){return this.fields.get(key);}},
    fetch:async(path,options)=>{
      calls.push({path,options});
      if(deferred.has(path)){const entry=deferred.get(path);deferred.delete(path);return entry.promise;}
      if(path==='/identity/login') account=JSON.parse(options.body).email;
      if(path==='/identity/invitations/token-B/accept') account='B';
      if(path==='/identity/me')return reply({user_id:account,workspace_id:'shop',csrf:'csrf-'+account,display_name:account,roles:['customer']});
      if(path==='/identity/logout')return reply(null,204);
      if(path==='/commerce/consult')return reply({id:'turn-'+account,status:'succeeded',output:'answer-'+account});
      return reply({});
    }
  });
  vm.runInContext(script,context,{filename:'workbench-page.js'});
  return {
    element,storage,calls,defer,initialPending,
    click:id=>element(id).onclick(),
    submit:(id,fields={})=>{element(id).fields=new Map(Object.entries(fields));return element(id).handlers.submit({preventDefault(){},currentTarget:element(id)});},
    resolveDigest:()=>digestResolve(new Uint8Array(32).buffer),
    changeSession:()=>element('session').handlers.change(),
    eval:code=>vm.runInContext(code,context)
  };
}

async function loginB(h){await h.submit('login',{email:'B',password:'not-a-real-password',workspace:'shop'});}
function noOldData(h){for(const id of ['result','diagnostics','invitation'])assert(!h.element(id).textContent.includes('PRIVATE-A'));}

async function main(){
  let count=0;
  for(const button of ['history','tickets','knowledge','inspect','events']){
    const h=harness();await tick();h.element('turn').value='old-turn';
    const path=button==='history'?'/harness/sessions/'+h.element('session').value+'/history':
      ['tickets','knowledge'].includes(button)?'/commerce/'+button:
      '/harness/sessions/'+h.element('session').value+'/turns/old-turn/'+button;
    const pending=h.defer(path),old=h.click(button);await tick();await loginB(h);
    pending.resolve({output:'PRIVATE-A'});await old;
    assert(h.element('identity').textContent.startsWith('B'));noOldData(h);count++;
  }
  {
    const h=harness();await tick();const pending=h.defer('/commerce/consult');
    h.element('query').value='PRIVATE-A';const old=h.submit('consult');
    // The real SHA256 runs on libuv's thread pool, so a fixed tick count races a loaded machine.
    await until(()=>h.calls.some(c=>c.path==='/commerce/consult'));
    assert(h.calls.some(c=>c.path==='/commerce/consult'));await loginB(h);
    pending.resolve({id:'PRIVATE-A',output:'PRIVATE-A'});await old;
    assert.equal(h.element('turn').value,'');noOldData(h);count++;
  }
  {
    const h=harness({deferDigest:true});await tick();h.element('query').value='PRIVATE-A';
    const old=h.submit('consult');await tick();await loginB(h);h.resolveDigest();await old;
    assert(!h.calls.some(c=>c.path==='/commerce/consult'));
    assert(!h.storage.has('fh-workbench-request'));noOldData(h);count++;
  }
  {
    const h=harness();await tick();const pending=h.defer('/identity/invitations');
    const old=h.submit('invite',{email:'invite-A',role:'support'});await tick();
    await h.submit('accept',{token:'token-B',name:'B',password:'not-a-real-password'});
    pending.resolve({invitation_token:'PRIVATE-A'});await old;noOldData(h);count++;
  }
  {
    const h=harness();await tick();const path='/harness/sessions/'+h.element('session').value+'/history';
    const pending=h.defer(path),old=h.click('history');await tick();await h.click('logout');
    pending.reject(new Error('PRIVATE-A'));await old;noOldData(h);
    assert.equal(h.element('identity').textContent,'尚未登录');count++;
  }
  {
    const h=harness();await tick();h.element('turn').value='old-turn';
    const path='/harness/sessions/'+h.element('session').value+'/turns/old-turn/inspect';
    const pending=h.defer(path),old=h.click('inspect');await tick();
    h.element('session').value='other-session';h.changeSession();pending.resolve({output:'PRIVATE-A'});
    await old;noOldData(h);count++;
  }
  {
    const h=harness();await tick();h.element('turn').value='old-turn';
    const pending=h.defer('/identity/approval'),old=h.submit('approve',{email:'approver',password:'secret'});
    await tick();await loginB(h);pending.resolve({approval_token:'PRIVATE-A'});await old;
    assert(!h.calls.some(c=>c.path.endsWith('/approve')||c.path.endsWith('/resume')));
    noOldData(h);count++;
  }
  {
    const h=harness();await tick();h.element('query').value='PRIVATE-A';
    await h.submit('consult');await h.submit('consult');
    let posts=h.calls.filter(c=>c.path==='/commerce/consult').map(c=>JSON.parse(c.options.body));
    assert.equal(posts[0].request_id,posts[1].request_id);
    assert(!h.storage.get('fh-workbench-request').includes('PRIVATE-A'));
    await h.click('new-request');await h.submit('consult');
    posts=h.calls.filter(c=>c.path==='/commerce/consult').map(c=>JSON.parse(c.options.body));
    assert.notEqual(posts[1].request_id,posts[2].request_id);count++;
  }
  {
    const h=harness();await tick();const pending=h.defer('/identity/login');
    const first=h.submit('login',{email:'B',password:'secret',workspace:'shop'});await tick();
    await h.submit('login',{email:'C',password:'secret',workspace:'shop'});
    assert.equal(h.calls.filter(c=>c.path==='/identity/login').length,1);
    pending.resolve({});await first;count++;
  }
  {
    const h=harness({deferInitial:true});assert.equal(h.calls[0].path,'/identity/me');
    await tick();await loginB(h);
    h.initialPending.resolve({user_id:'PRIVATE-A',workspace_id:'shop',csrf:'old',display_name:'PRIVATE-A',roles:[]});
    await tick();assert(h.element('identity').textContent.startsWith('B'));
    assert.equal(h.eval('userId'),'B');count++;
  }
  console.log(JSON.stringify({page:'workbench',race_and_idempotency_cases:count,passed:true}));
}
main().catch(error=>{console.error(error);process.exitCode=1;});
