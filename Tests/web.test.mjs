import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {once} from 'node:events';
import http from 'node:http';
import vm from 'node:vm';
import crypto from 'node:crypto';
import {createServer, passwordHash, totp, writePrivate, validateControl, sixMonthsFrom} from '../Web/server.mjs';

const origin = 'https://virty.test';
const id = '00000000-0000-0000-0000-000000000001';
async function fixture(enrolled = true, bridgeHook) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'virty-web-test-')); fs.chmodSync(directory, 0o700);
  const password = 'public-disposable-test-password', secret = '3132333435363738393031323334353637383930';
  let time = 1800000000000;
  if (enrolled) writePrivate(path.join(directory, 'account.json'), JSON.stringify({version:1,username:'test',password:await passwordHash(password),totpSecret:secret,lastCounter:-1}));
  const calls = [];
  let server, url;
  async function start() {
    server = await createServer({state:directory,origin,proxyPeers:['127.0.0.1']}, {now:()=>time, bridge:async body=> {
      calls.push(body);
      if (bridgeHook) { const result = await bridgeHook(body); if (result) return result; }
      if (body.command === 'list') return {ok:true,vms:[{name:'macos',vmID:id,state:'running',guestOS:'macos'}]};
      if (body.command === 'screenshot') return {ok:true,png:Buffer.from('public-test-frame').toString('base64'),generation:'frame-test',width:1280,height:800,cached:true,updatedAt:'2026-10-10T21:57:05Z'};
      if (body.command === 'aliases') return {ok:true,aliases:['vm-login']};
      return {ok:true,status:'typed'};
    }});
    server.listen(0, '127.0.0.1'); await once(server, 'listening');
    url = 'http://127.0.0.1:' + server.address().port;
  }
  async function stop() { server.closeAllConnections(); await new Promise(resolve=>server.close(resolve)); }
  await start();
  let cookie = '', csrf = '';
  async function request(route, data, extra={}) {
    const payload = data === undefined ? undefined : JSON.stringify(data);
    const response = await new Promise((resolve, reject) => {
      const request = http.request(url + route, {method:data === undefined?'GET':'POST',headers:{host:'virty.test','x-forwarded-proto':'https','x-real-ip':'192.0.2.10',cookie,...(data === undefined?{}:{origin,'content-type':'application/json','x-csrf-token':csrf,'content-length':Buffer.byteLength(payload)}),...extra}}, incoming => {
        const parts=[]; incoming.on('data', part=>parts.push(part)); incoming.on('end',()=>resolve(new Response(Buffer.concat(parts),{status:incoming.statusCode,headers:Object.fromEntries(Object.entries(incoming.headers).map(([key,value])=>[key,Array.isArray(value)?value.join(','):value]))})));
      });
      request.on('error',reject); request.end(payload);
    });
    if (response.headers.get('set-cookie')) cookie = response.headers.get('set-cookie').split(';')[0];
    return response;
  }
  // Wait until the server has received a partial body or finished reading a
  // complete one. This makes revocation/queue tests independent of socket timing.
  async function beginRequest(route, data, partial=false) {
    const payload=JSON.stringify(data);
    const arrived=new Promise(resolve=>server.once('request', incoming=> {
      if (partial) resolve(); else incoming.once('end',()=>setImmediate(resolve));
    }));
    let outgoing;
    const response=new Promise((resolve,reject)=> {
      outgoing=http.request(url+route,{method:'POST',headers:{host:'virty.test','x-forwarded-proto':'https','x-real-ip':'192.0.2.10',cookie,origin,'content-type':'application/json','x-csrf-token':csrf,'content-length':Buffer.byteLength(payload)}},incoming=> {
        const parts=[];incoming.on('data',part=>parts.push(part));incoming.on('end',()=>resolve({status:incoming.statusCode,body:JSON.parse(Buffer.concat(parts))}));
      });
      outgoing.on('error',reject);
      if (partial) outgoing.write(payload.slice(0,1)); else outgoing.end(payload);
    });
    await arrived;
    return {response,finish:()=>outgoing.end(payload.slice(1)),destroy:()=>outgoing.destroy()};
  }
  async function session(extra={}) { const response=await request('/api/session',undefined,extra); const value=await response.json(); csrf=value.csrf; return value; }
  async function login(rememberMe=true) { await session(); assert.equal((await request('/auth/login',{username:'test',password,rememberMe})).status,200); await session(); }
  async function otp() { const response=await request('/auth/otp',{code:totp(secret,Math.floor(time/30000))}); assert.equal(response.status,200); await session(); return response; }
  return {directory, password, secret, calls, request, beginRequest, session, login, otp, advance:(ms=60000)=>time+=ms, cookie:()=>cookie, useCookie:value=>cookie=value,
    restart:async()=>{await stop(); await start();},
    close:async()=>{await stop(); fs.rmSync(directory,{recursive:true,force:true});}};
}

test('request ID contract agrees with the manager grammar', () => {
  const cases=JSON.parse(fs.readFileSync(new URL('./request-id-cases.json',import.meta.url)));
  for (const command of ['type-text','type-secret','operation']) for (const item of cases) {
    const body={command,vmID:id,requestId:item.value,...(command==='operation'?{}:{generation:'frame-1',...(command==='type-text'?{text:'public'}:{alias:'vm-login'})})};
    if (item.valid) assert.doesNotThrow(()=>validateControl(body));
    else assert.throws(()=>validateControl(body),undefined,JSON.stringify(item));
  }
});
test('anonymous allocation bursts preserve ready and OTP browsers and admit an independent login', async () => {
  const f=await fixture(); try {
    await f.login(); await f.otp(); const ready=f.cookie();
    const durable=fs.readFileSync(path.join(f.directory,'sessions.json'),'utf8');
    await f.session({cookie:'','x-real-ip':'192.0.2.20'}); await f.login(); const pending=f.cookie();
    const pendingSession=await f.session();
    let admitted=0;
    for (let i=0;i<300;i++) {
      const response=await f.request('/api/session',undefined,{cookie:'','x-real-ip':'192.0.2.99'});
      assert.equal(response.status,i<8?200:429);
      if (response.status===200) admitted++;
    }
    assert.equal(admitted,8);
    // Diverse anonymous IPs fill/reclaim the independent anonymous budget.
    for (let i=0;i<300;i++) assert.equal((await f.request('/api/session',undefined,{cookie:'','x-real-ip':`198.51.${Math.floor(i/256)}.${i%256}`})).status,200);
    assert.equal(fs.readFileSync(path.join(f.directory,'sessions.json'),'utf8'),durable);
    assert.equal((await f.request('/api/vms',undefined,{cookie:ready})).status,200);
    assert.deepEqual(await f.session({cookie:pending}),pendingSession);
    await f.session({cookie:'','x-real-ip':'203.0.113.42'}); await f.login(); f.advance(); await f.otp();
    assert.equal((await f.request('/api/vms')).status,200);
    // The earlier password-verified browser still completes OTP after reclamation.
    f.useCookie(pending); await f.session(); f.advance(); await f.otp();
    assert.equal((await f.request('/api/vms')).status,200);
    assert.equal((await f.request('/api/vms',undefined,{cookie:ready})).status,200);
  } finally {await f.close();}
});
test('protected capacity is independent and failed promotion retains the anonymous session', async () => {
  const f=await fixture(); try {
    await f.login(); await f.otp(); const ready=f.cookie();
    const filename=path.join(f.directory,'sessions.json'), stored=JSON.parse(fs.readFileSync(filename));
    const first=stored.entries[0];
    for (let i=1;i<256;i++) stored.entries.push({...first,tokenHash:crypto.createHash('sha256').update(`fixture-${i}`).digest('hex')});
    writePrivate(filename,JSON.stringify(stored)); await f.restart();
    const pending=await f.session({cookie:'','x-real-ip':'203.0.113.99'}), cookie=f.cookie();
    assert.equal(pending.stage,'login');
    assert.equal((await f.request('/auth/login',{username:'test',password:f.password})).status,503);
    assert.equal(f.cookie(),cookie); assert.deepEqual(await f.session(),pending);
    assert.equal((await f.request('/api/vms',undefined,{cookie:ready})).status,200);
    assert.equal(JSON.parse(fs.readFileSync(filename)).entries.length,256);
  } finally {await f.close();}
});
test('anonymous budget replenishes and expires with a bounded client tracker', async () => {
  const f=await fixture(); try {
    const headers={cookie:'','x-real-ip':'192.0.2.77'};
    await f.session(headers); const original=f.cookie(), first=await f.session();
    for (let i=1;i<8;i++) assert.equal((await f.request('/api/session',undefined,headers)).status,200);
    assert.equal((await f.request('/api/session',undefined,headers)).status,429);
    // Looking up an existing cookie consumes no anonymous allocation budget.
    assert.deepEqual(await f.session({cookie:original}),first);
    f.advance(59999); assert.equal((await f.request('/api/session',undefined,headers)).status,429);
    f.advance(1); assert.equal((await f.request('/api/session',undefined,headers)).status,200);
    for (let i=1;i<8;i++) assert.equal((await f.request('/api/session',undefined,headers)).status,200);
    assert.equal((await f.request('/api/session',undefined,headers)).status,429);
    // More than the tracking capacity admits independent visitors and discards
    // old rate metadata instead of accumulating one record per source forever.
    for (let i=0;i<1100;i++) assert.equal((await f.request('/api/session',undefined,{cookie:'','x-real-ip':`198.18.${Math.floor(i/256)}.${i%256}`})).status,200);
    assert.equal((await f.request('/api/session',undefined,headers)).status,200);
    await f.session({cookie:'','x-real-ip':'192.0.2.78'}); const expiring=f.cookie(), pending=await f.session();
    f.advance(299999); assert.deepEqual(await f.session({cookie:expiring}),pending);
    f.advance(1); const renewed=await f.session({cookie:expiring});
    assert.equal(renewed.stage,'login'); assert.notEqual(renewed.csrf,pending.csrf); assert.notEqual(f.cookie(),expiring);
  } finally {await f.close();}
});
test('anonymous reclamation preserves account enrollment', async () => {
  const f=await fixture(false); try {
    await f.session();
    const code=fs.readFileSync(path.join(f.directory,'setup-code'),'utf8').trim();
    assert.equal((await f.request('/auth/setup',{code,username:'test',password:f.password})).status,200);
    const enrollment=f.cookie(), session=await f.session();
    const before=await (await f.request('/api/enrollment')).json();
    for (let i=0;i<300;i++) assert.equal((await f.request('/api/session',undefined,{cookie:'','x-real-ip':`198.19.${Math.floor(i/256)}.${i%256}`})).status,200);
    f.useCookie(enrollment); assert.deepEqual(await f.session(),session);
    assert.deepEqual(await (await f.request('/api/enrollment')).json(),before);
  } finally {await f.close();}
});
test('remembered expiry spans six calendar months, including month ends and leap years', () => {
  for (const [start,end] of [['2026-10-10T22:00:00Z','2027-04-10T22:00:00Z'],['2026-03-31T22:00:00Z','2026-09-30T22:00:00Z'],['2023-08-31T22:00:00Z','2024-02-29T22:00:00Z'],['2026-07-01T22:00:00Z','2027-01-01T22:00:00Z']]) assert.equal(sixMonthsFrom(Date.parse(start)),Date.parse(end));
});
const crossSiteNavigation = {'sec-fetch-site':'cross-site','sec-fetch-mode':'navigate','sec-fetch-dest':'document'};
test('cross-site page navigation loads the public shell without creating a session or admitting embedded requests', async () => {
  const f=await fixture(); try {
    for (const route of ['/','/login','/otp','/setup','/enroll','/?reload=1']) {
      const response=await f.request(route,undefined,crossSiteNavigation);
      assert.equal(response.status,200,route);
      assert.match(response.headers.get('content-type'),/^text\/html/);
      assert.match(await response.text(),/<!doctype html>/i);
      assert.equal(response.headers.get('set-cookie'),null);
      assert.equal(response.headers.get('cache-control'),'no-store');
      assert.equal(response.headers.get('x-frame-options'),'DENY');
      assert.match(response.headers.get('content-security-policy'),/frame-ancestors 'none'/);
    }
    assert.equal(f.cookie(),'');
    assert.equal(fs.existsSync(path.join(f.directory,'sessions.json')),false);
    assert.equal((await f.request('/api/vms')).status,401);
    for (const headers of [
      {'sec-fetch-site':'cross-site'},
      {...crossSiteNavigation,'sec-fetch-mode':'cors'},
      {...crossSiteNavigation,'sec-fetch-mode':''},
      ...['iframe','frame','object','embed','empty',''].map(dest=>({...crossSiteNavigation,'sec-fetch-dest':dest})),
      {...crossSiteNavigation,host:'evil.test'},
      {...crossSiteNavigation,'x-forwarded-proto':'http'},
    ]) assert.equal((await f.request('/',undefined,headers)).status,403,JSON.stringify(headers));
    for (const route of ['/app.js','/app.css','/unknown']) assert.equal((await f.request(route,undefined,crossSiteNavigation)).status,403,route);
    assert.equal((await f.request('/',{},crossSiteNavigation)).status,403);
    assert.deepEqual(f.calls,[]);
  } finally {await f.close();}
});
test('restored navigation with a withheld Strict cookie preserves the remembered login across web restart', async () => {
  const f=await fixture(); try {
    await f.login(); await f.otp();
    const remembered=f.cookie(), session=await f.session();
    const stored=fs.readFileSync(path.join(f.directory,'sessions.json'),'utf8');
    f.advance(90*24*60*60*1000); await f.restart();
    for (const cookie of ['',remembered]) {
      const response=await f.request('/',undefined,{...crossSiteNavigation,cookie});
      assert.equal(response.status,200);
      assert.equal(response.headers.get('set-cookie'),null);
      assert.equal(f.cookie(),remembered);
      assert.deepEqual(await f.session(),session);
    }
    assert.equal(fs.readFileSync(path.join(f.directory,'sessions.json'),'utf8'),stored);
    assert.equal((await f.request('/api/vms')).status,200);
  } finally {await f.close();}
});
test('cross-site APIs and auth remain refused even with a ready cookie, valid CSRF and navigation headers', async () => {
  const f=await fixture(); try {
    await f.login(); await f.otp();
    for (const headers of [crossSiteNavigation, {...crossSiteNavigation,'sec-fetch-mode':'cors','sec-fetch-dest':'empty'}]) {
      for (const route of ['/api/session','/api/enrollment','/api/vms',...['status','aliases','frame'].map(action=>'/api/vms/macos/'+action+'?vmID='+id)]) {
        const response=await f.request(route,undefined,headers);
        assert.equal(response.status,403,route);
        assert.deepEqual(await response.json(),{ok:false,error:'Cross-site request refused.'});
        assert.equal(response.headers.get('set-cookie'),null);
      }
      for (const route of ['/auth/login','/auth/otp','/auth/setup','/auth/logout','/api/vms/macos/control']) {
        assert.equal((await f.request(route,{command:'stop',vmID:id},headers)).status,403,route);
      }
    }
    assert.deepEqual(f.calls,[]);
    assert.equal((await f.session()).stage,'ready');
  } finally {await f.close();}
});
test('checkpoint endpoints require both login factors and enforce bounded identities', async () => {
  const f = await fixture(true, async body => ({ok:true,jobId:id,state:'queued',sourceVMID:body.vmID}));
  try {
    const route='/api/vms/macos/control', body={command:'snapshot-create',vmID:id,label:'before-upgrade'};
    await f.session(); assert.equal((await f.request(route,body)).status,401);
    await f.login(); assert.equal((await f.request(route,body)).status,401);
    await f.otp(); assert.equal((await f.request(route,body)).status,200);
    for (const command of ['snapshot-restore','snapshot-delete']) {
      assert.equal((await f.request(route,{command,vmID:id,snapshotID:id})).status,200);
      assert.equal((await f.request(route,{command,vmID:id})).status,400);
    }
    assert.equal((await f.request(route,{command:'clone',vmID:id,newName:'copy',snapshotID:id})).status,200);
    assert.equal((await f.request(route,{command:'snapshot-job',vmID:id,jobId:id})).status,200);
    assert.equal((await f.request(route,{...body,label:'../escape'})).status,400);
    assert.equal((await f.request(route,{...body,password:'forbidden'})).status,400);
    assert.equal((await f.request(route,body,{'x-csrf-token':'bad'})).status,403);
  } finally { await f.close(); }
});
test('remembered login persists across restarts and idle months, with hashed tokens and fixed expiry', async () => {
  const f=await fixture(); try {
    await f.login();
    assert.equal(fs.existsSync(path.join(f.directory,'sessions.json')),false);
    const response=await f.otp(), header=response.headers.get('set-cookie'), raw=f.cookie().split('=')[1];
    for (const attribute of ['Secure','HttpOnly','SameSite=Strict','Path=/','Expires=']) assert.ok(header.includes(attribute));
    const contents=fs.readFileSync(path.join(f.directory,'sessions.json'),'utf8'), stored=JSON.parse(contents);
    assert.equal(contents.includes(raw),false);
    assert.equal(contents.includes(f.password),false); assert.equal(contents.includes(f.secret),false);
    assert.equal(stored.entries.length,1);
    assert.equal(stored.entries[0].tokenHash,crypto.createHash('sha256').update(raw).digest('hex'));
    assert.equal(fs.statSync(path.join(f.directory,'sessions.json')).mode & 0o777,0o600);
    const remaining=stored.entries[0].expiresAt-stored.entries[0].created;
    assert.ok(header.includes('Max-Age='+Math.floor(remaining/1000)));
    const originalSession=await f.session();
    await f.restart(); assert.deepEqual(await f.session(),originalSession);
    f.advance(remaining-1); await f.restart();
    assert.equal((await f.request('/api/vms')).status,200);
    assert.equal(fs.readFileSync(path.join(f.directory,'sessions.json'),'utf8'),contents); // Requests do not renew expiry or write every frame.
    f.advance(1); assert.equal((await f.request('/api/vms')).status,401);
    assert.equal(JSON.parse(fs.readFileSync(path.join(f.directory,'sessions.json'),'utf8')).entries.length,0);
    await f.restart(); assert.equal((await f.session()).stage,'login');
  } finally {await f.close();}
});
test('logout revokes remembered cookies after restart and later OTP logins preserve other browsers', async () => {
  const f=await fixture(); try {
    await f.login(); await f.otp(); const old=f.cookie();
    // A second browser gets its own login and advances the account's replay counter.
    await f.request('/api/session',undefined,{cookie:''}); f.advance(); await f.login(); await f.otp(); const second=f.cookie();
    assert.equal(JSON.parse(fs.readFileSync(path.join(f.directory,'sessions.json'),'utf8')).entries.length,2);
    await f.restart(); assert.equal((await f.request('/api/vms',undefined,{cookie:old})).status,200);
    assert.equal((await f.request('/auth/logout',{})).status,200);
    await f.restart(); assert.equal((await f.request('/api/vms')).status,401);
    assert.equal((await f.request('/api/vms',undefined,{cookie:second})).status,401);
    assert.equal((await f.request('/api/vms',undefined,{cookie:old})).status,200);
    await f.session({cookie:old});
    assert.equal((await f.request('/auth/logout',{}, {cookie:old})).status,200);
    await f.restart(); assert.equal((await f.request('/api/vms',undefined,{cookie:old})).status,401);
  } finally {await f.close();}
});
test('pending OTP and unchecked logins are not persisted; short sessions retain idle and absolute limits', async () => {
  const f=await fixture(); try {
    await f.session();
    assert.equal((await f.request('/auth/login',{username:'test',password:f.password,rememberMe:'false'})).status,400);
    await f.login(); await f.restart(); assert.equal((await f.session()).stage,'login');
    assert.equal((await f.request('/auth/otp',{code:'123456'})).status,401);
    await f.login(); f.advance(300000); assert.equal((await f.session()).stage,'login');
    await f.login(false); await f.otp();
    assert.equal(fs.existsSync(path.join(f.directory,'sessions.json')),false);
    f.advance(15*60*1000); assert.equal((await f.request('/api/vms')).status,401);
    await f.login(false); await f.otp(); await f.restart(); assert.equal((await f.session()).stage,'login');
    f.advance(); await f.login(false); await f.otp();
    for (let hours=0; hours<8; hours++) for (let minutes=0; minutes<60; minutes+=10) { assert.equal((await f.request('/api/vms')).status,200); f.advance(10*60*1000); }
    assert.equal((await f.request('/api/vms')).status,401);
  } finally {await f.close();}
});
test('account replacement revokes stored sessions; unsafe or corrupt session files fail closed', async () => {
  const f=await fixture(); try {
    await f.login(); await f.otp();
    const accountPath=path.join(f.directory,'account.json'), sessionsPath=path.join(f.directory,'sessions.json');
    const account=JSON.parse(fs.readFileSync(accountPath,'utf8')); account.password=await passwordHash(f.password);
    writePrivate(accountPath,JSON.stringify(account));
    await f.restart(); assert.equal((await f.request('/api/vms')).status,401);
    assert.equal(JSON.parse(fs.readFileSync(sessionsPath,'utf8')).entries.length,0);
    const config={state:f.directory,origin,proxyPeers:['127.0.0.1']};
    fs.chmodSync(sessionsPath,0o644); await assert.rejects(createServer(config),/0600/); fs.chmodSync(sessionsPath,0o600);
    writePrivate(sessionsPath,'{broken'); await assert.rejects(createServer(config));
    writePrivate(sessionsPath,JSON.stringify({version:1,account:null,entries:[]}));
  } finally {await f.close();}
});
test('remembered login and logout report failure if durable session storage cannot be updated', async () => {
  const f=await fixture(); try {
    const sessionsPath=path.join(f.directory,'sessions.json');
    await f.login(); fs.mkdirSync(sessionsPath, {mode:0o700});
    const failed=await f.request('/auth/otp',{code:totp(f.secret,60000000)});
    assert.equal(failed.status,503); assert.equal(failed.headers.get('set-cookie'),null);
    assert.equal((await f.request('/api/vms')).status,401);
    fs.rmdirSync(sessionsPath); f.advance(); await f.login(); await f.otp(); const remembered=f.cookie();
    const stored=fs.readFileSync(sessionsPath); fs.unlinkSync(sessionsPath); fs.mkdirSync(sessionsPath, {mode:0o700});
    assert.equal((await f.request('/auth/logout',{})).status,503);
    assert.equal((await f.request('/api/vms')).status,200);
    fs.rmdirSync(sessionsPath); writePrivate(sessionsPath,stored);
    assert.equal((await f.request('/auth/logout',{})).status,200);
    await f.restart(); assert.equal((await f.request('/api/vms',undefined,{cookie:remembered})).status,401);
  } finally {await f.close();}
});
test('login UI remembers by default and sends the checkbox choice to both login and setup', async () => {
  const elements=new Map(), calls=[];
  const element=selector=> {
    if (!elements.has(selector)) elements.set(selector,{value:selector==='#username'?'test':selector==='#password'?'public-test-password':'public-setup-code',checked:false,addEventListener(event,callback){this[event]=callback;}});
    return elements.get(selector);
  };
  Object.defineProperty(element('#app'),'innerHTML',{set(value) {
    assert.match(value,/<label class="remember" for="remember">/);
    assert.match(value,/<input id="remember" type="checkbox" checked>/);
    element('#remember').checked=true;
  }});
  const context=vm.createContext({document:{querySelector:element},window:{addEventListener(){}},history:{replaceState(){}},
    clearInterval(){},clearTimeout(){},fetch:async(url,options)=>{
      if (url==='/api/session') return new Promise(()=>{});
      calls.push({url,body:JSON.parse(options.body)});
      return new Response(JSON.stringify({ok:true,next:'/otp'}));
    }});
  vm.runInContext(fs.readFileSync(new URL('../Web/app.js',import.meta.url),'utf8'),context);
  vm.runInContext("session={stage:'login',csrf:'public-csrf',setupRequired:false}; boot=async()=>{}; loginPage();",context);
  element('#remember').checked=false;
  await element('#login').submit({preventDefault(){}});
  assert.equal(calls[0].url,'/auth/login'); assert.equal(calls[0].body.rememberMe,false);
  vm.runInContext('loginPage(true)',context);
  await element('#login').submit({preventDefault(){}});
  assert.equal(calls[1].url,'/auth/setup'); assert.equal(calls[1].body.rememberMe,true);
});

function consoleUI() {
  const elements = new Map(), events = new Map(), calls = [];
  const element = selector => {
    if (!elements.has(selector)) elements.set(selector, {value:'', dataset:{},
      classList:{add(){},remove(){},toggle(){}}, addEventListener(event, callback){this[event]=callback;},
      showModal(){this.open=true;},close(){this.open=false;},replaceChildren(){this.innerHTML='';},focus(){}});
    return elements.get(selector);
  };
  const context = vm.createContext({document:{querySelector:element,querySelectorAll:()=>[]},
    window:{addEventListener:(event, callback)=>events.set(event,callback)}, clearInterval(){},clearTimeout(){},
    fetch:async()=>new Promise(()=>{})});
  vm.runInContext(fs.readFileSync(new URL('../Web/app.js',import.meta.url),'utf8'),context);
  context.capture = (command, values) => {
    assert.equal(element('#guest-secret').value, '', 'clear before any delivery attempt');
    assert.equal(element('#guest-input-dialog').innerHTML, '', 'remove secure input before delivery');
    calls.push({command,...values});
  };
  vm.runInContext("session={stage:'ready',username:'test'}; connected=true; generation='public-generation'; checkpointPanel=()=>{}; renderSelection=()=>{}; typing=capture; dashboard();",context);
  return {element, context, calls, events};
}

test('console mounts native secure input only after opening the guest entry dialog', () => {
  const f=consoleUI(), markup=f.element('#app').innerHTML;
  assert.doesNotMatch(markup, /type="password"/);
  assert.match(markup, /<dialog id="guest-input-dialog"[^>]*><\/dialog>/);
  f.element('#open-guest-input').onclick();
  const dialog=f.element('#guest-input-dialog');
  assert.equal(dialog.open,true);
  assert.doesNotMatch(dialog.innerHTML, /<form|type="submit"/);
  assert.match(dialog.innerHTML, /<input id="guest-secret" type="password"[^>]*autocomplete="off"[^>]*data-1p-ignore/);
  f.element('#guest-secret').value='public-test';
  f.element('#guest-input-cancel').onclick();
  assert.equal(f.element('#guest-secret').value,''); assert.equal(dialog.innerHTML,''); assert.equal(dialog.open,false);
  vm.runInContext('connected=false',f.context);f.element('#open-guest-input').onclick(); assert.equal(dialog.innerHTML,'');
});

test('guest entry preserves exact one-time delivery and clears on failure, disconnect and page exit', () => {
  const f=consoleUI(), field=f.element('#guest-secret'), dialog=f.element('#guest-input-dialog');
  const text='  public-disposable-test !  ';
  f.element('#open-guest-input').onclick();field.value=text;
  const send=f.element('#guest-input-send').onclick;send();send();
  assert.equal(f.calls.length,1);
  assert.equal(f.calls[0].command,'type-text'); assert.equal(f.calls[0].text,text); assert.equal(f.calls[0].delayMs,80);
  vm.runInContext("typing=()=>{throw new Error('public-test-failure');}",f.context);
  f.element('#open-guest-input').onclick();field.value=text;
  assert.throws(()=>f.element('#guest-input-send').onclick(),/public-test-failure/);
  assert.equal(field.value,'');assert.equal(dialog.innerHTML,'');
  f.element('#open-guest-input').onclick();field.value=text;
  vm.runInContext('disconnect()',f.context);assert.equal(field.value,'');assert.equal(dialog.innerHTML,'');
  vm.runInContext("connected=true;generation='public-generation'",f.context);
  f.element('#open-guest-input').onclick();field.value=text;f.events.get('pagehide')();
  assert.equal(field.value,'');assert.equal(dialog.innerHTML,'');
});

test('guest entry supports Enter and Escape without submitting a login form or retaining input', () => {
  const f=consoleUI(), field=f.element('#guest-secret'), dialog=f.element('#guest-input-dialog');
  f.element('#open-guest-input').onclick(); field.value='public-test';
  let prevented=false;
  field.onkeydown({key:'Enter',isComposing:true,preventDefault(){prevented=true;}});
  assert.equal(f.calls.length,0); assert.equal(dialog.open,true);
  field.onkeydown({key:'Enter',isComposing:false,preventDefault(){prevented=true;}});
  assert.equal(prevented,true); assert.equal(f.calls.length,1); assert.equal(field.value,''); assert.equal(dialog.innerHTML,'');
  f.element('#open-guest-input').onclick();field.value='public-cancelled';dialog.oncancel();
  assert.equal(field.value,'');assert.equal(dialog.innerHTML,'');assert.equal(f.calls.length,1);
  f.element('#open-guest-input').onclick();field.value='public-unavailable';
  vm.runInContext('generation=undefined',f.context);f.element('#guest-input-send').onclick();
  assert.equal(field.value,'');assert.equal(dialog.innerHTML,'');assert.equal(f.calls.length,1);
  assert.match(f.element('#notice').textContent,/Guest input is unavailable/);
});

test('TOTP matches RFC 6238 SHA-1 vectors, including time beyond 2038', () => {
  const secret = Buffer.from('12345678901234567890').toString('hex');
  for (const [seconds, code] of [[59,'94287082'],[1111111109,'07081804'],[1111111111,'14050471'],[1234567890,'89005924'],[2000000000,'69279037'],[20000000000,'65353130']]) assert.equal(totp(secret,Math.floor(seconds/30),8),code);
});
test('password is only the first stage; OTP replay, CSRF, and proxy bypass are refused', async () => {
  const f=await fixture(); try {
    assert.equal((await f.request('/api/vms')).status,401);
    await f.session();
    assert.equal((await f.request('/auth/otp',{code:'123456'})).status,401);
    assert.equal((await f.request('/auth/login',{username:'test',password:'wrong'})).status,401);
    await f.login(); assert.equal((await f.session()).stage,'otp');
    assert.equal((await f.request('/api/vms')).status,401);
    assert.equal((await f.request('/api/enrollment')).status,401);
    await f.otp(); assert.equal((await f.session()).stage,'ready');
    assert.equal((await f.request('/api/vms')).status,200);
    assert.equal((await f.request('/auth/logout',{}, {origin:'https://evil.test'})).status,403);
    assert.equal((await f.request('/auth/logout',{}, {'x-csrf-token':'bad'})).status,403);
    assert.equal((await f.request('/api/vms',undefined,{'x-forwarded-proto':'http'})).status,403);
    assert.equal((await f.request('/api/vms',undefined,{host:'evil.test'})).status,403);
    await f.request('/auth/logout',{}); await f.login();
    assert.equal((await f.request('/auth/otp',{code:totp(f.secret,60000000)})).status,401);
    f.advance(); await f.otp();
  } finally {await f.close();}
});
test('first-run account enrollment needs the local code and a verified authenticator', async () => {
  const f=await fixture(false); try {
    assert.equal((await f.session()).setupRequired,true);
    assert.equal((await f.request('/auth/setup',{username:'new',password:f.password,code:'wrong'})).status,401);
    const code=fs.readFileSync(path.join(f.directory,'setup-code'),'utf8');
    assert.equal((await f.request('/auth/setup',{username:'new',password:f.password,code})).status,200);
    assert.equal((await f.session()).stage,'enroll');
    assert.equal((await f.request('/api/vms')).status,401);
    const details=await (await f.request('/api/enrollment')).json(); assert.match(details.secret,/^[A-Z2-7]{32}$/);
    assert.equal(fs.existsSync(path.join(f.directory,'account.json')),false);
    // Decode the authenticator key without accessing the server's pending session.
    const alphabet='ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'; const bits=[...details.secret].map(c=>alphabet.indexOf(c).toString(2).padStart(5,'0')).join('');
    const secret=Buffer.from(bits.match(/.{8}/g).map(byte=>parseInt(byte,2))).toString('hex');
    assert.equal((await f.request('/auth/otp',{code:totp(secret,60000000)})).status,200);
    assert.equal((await f.session()).stage,'ready');
    assert.equal(fs.statSync(path.join(f.directory,'account.json')).mode & 0o777,0o600);
    assert.equal(fs.existsSync(path.join(f.directory,'setup-code')),false);
    assert.equal((await f.request('/api/enrollment')).status,401);
    assert.equal((await f.request('/auth/setup',{username:'new',password:f.password,code})).status,401);
    await f.restart(); assert.equal((await f.session()).stage,'ready');
  } finally {await f.close();}
});
test('authenticated frames and alias controls retain VM identity without secret retrieval', async () => {
  const f=await fixture(); try {
    await f.login(); await f.otp();
    const frame=await f.request('/api/vms/macos/frame?vmID='+id);
    assert.equal(frame.headers.get('content-type'),'image/png'); assert.equal(frame.headers.get('x-display-generation'),'frame-test');
    assert.equal(frame.headers.get('x-frame-cached'),'true'); assert.equal(frame.headers.get('x-frame-updated-at'),'2026-10-10T21:57:05Z');
    assert.equal(await frame.text(),'public-test-frame');
    assert.deepEqual(f.calls, [{command:'screenshot',name:'macos',vmID:id}]);
    const response=await f.request('/api/vms/macos/control',{command:'type-secret',vmID:id,alias:'vm-login',generation:'frame-test',requestId:'public-test-id',delayMs:80});
    assert.equal(response.status,200); const result=await response.json(); assert.equal('value' in result,false);
    assert.deepEqual(f.calls.at(-1),{command:'type-secret',vmID:id,alias:'vm-login',generation:'frame-test',requestId:'public-test-id',delayMs:80,name:'macos'});
    assert.equal((await f.request('/api/vms/macos/control',{command:'read',vmID:id,alias:'vm-login'})).status,400);
    assert.equal((await f.request('/api/vms/macos/control',{command:'type-secret',vmID:id,alias:'vm-login',generation:'frame-test',requestId:'public-test-id',value:'forbidden'})).status,400);
    f.advance(); f.advance(); // Session remains valid before its idle limit.
    assert.equal((await f.request('/api/vms')).status,200);
  } finally {await f.close();}
});
test('web Connect and Disconnect leave host console visibility alone; unavailable frames disable input and retry', async () => {
  const elements = new Map(), calls = [], timers = [];
  const element = selector => {
    if (!elements.has(selector)) {
      const classes = new Set();
      elements.set(selector, {textContent:'',value:'vm-login',dataset:{},disabled:false,close(){},replaceChildren(){},
        classList:{add:value=>classes.add(value),remove:value=>classes.delete(value),contains:value=>classes.has(value)}});
    }
    return elements.get(selector);
  };
  let unavailable = false;
  const context = vm.createContext({
    document:{querySelector:element,querySelectorAll:()=>[]}, window:{addEventListener(){}},
    URL:{createObjectURL:()=> 'blob:public-frame',revokeObjectURL(){}},
    setTimeout:callback=>timers.push(callback),clearTimeout(){},clearInterval(){},
    fetch:async (url, options) => {
      if (url === '/api/session') return new Promise(()=>{}); // Keep bootstrap outside this dashboard test.
      calls.push({url,body:options?.body ? JSON.parse(options.body) : undefined});
      if (url.includes('/frame?')) return unavailable
        ? new Response(JSON.stringify({ok:false,error:'Guest is booting.'}),{status:409})
        : new Response('public-frame',{headers:{'X-Display-Generation':'frame-test','X-Display-Width':'1280','X-Display-Height':'800'}});
      return new Response(JSON.stringify({ok:true}));
    }
  });
  vm.runInContext(fs.readFileSync(new URL('../Web/app.js',import.meta.url),'utf8'), context);
  vm.runInContext(`session={stage:'ready',csrf:'public-test-csrf'}; selected={name:'macos',vmID:'${id}',state:'running'};`, context);
  await vm.runInContext('connect()',context);
  assert.equal(calls.length,1); assert.match(calls[0].url,/\/frame\?vmID=/);
  assert.equal(element('#screen').classList.contains('hidden'),false);
  assert.equal(element('#send-text').disabled,false);
  await vm.runInContext("enqueueInput({command:'key',chord:'return'})",context);
  assert.equal(calls.at(-1).body.generation,'frame-test');
  unavailable = true;
  await vm.runInContext('refreshFrame(epoch)',context);
  assert.equal(element('#screen').classList.contains('hidden'),true);
  assert.equal(element('#send-text').disabled,true);
  assert.equal(element('#console-status').textContent,'Guest is booting.');
  assert.match(element('#placeholder-text').textContent,/retry automatically/);
  assert.ok(timers.length >= 2);
  unavailable = false;
  await vm.runInContext('refreshFrame(epoch)',context);
  assert.equal(element('#send-text').disabled,false);
  vm.runInContext('disconnect()',context);
  assert.equal(element('#send-text').disabled,true);
  assert.ok(calls.every(call=>!['show','hide'].includes(call.body?.command)));
});
test('input schema rejects ambiguous identities and unsafe values', () => {
  for (const value of [{command:'__proto__',vmID:id},{command:'stop',vmID:id,force:'true'},{command:'click',vmID:id,generation:'test',x:NaN,y:0},{command:'key',vmID:id,chord:'return'},{command:'type-text',vmID:id,generation:'test',text:'test',requestId:'a/b'}]) assert.throws(()=>validateControl(value));
});
test('scroll controls require both factors and bounded identity-linked input', async () => {
  const f=await fixture();
  const body={command:'scroll',vmID:id,generation:'frame-test',x:500,y:300,deltaY:3};
  try {
    await f.login();
    assert.equal((await f.request('/api/vms/macos/control',body)).status,401);
    await f.otp();
    assert.equal((await f.request('/api/vms/macos/control',body)).status,200);
    assert.deepEqual(f.calls.at(-1),{...body,name:'macos'});
    const before=f.calls.length;
    for (const extra of [{deltaY:11},{deltaY:true},{deltaY:0},{deltaX:0.5},{x:-1},{generation:undefined},{password:'forbidden'}]) {
      assert.equal((await f.request('/api/vms/macos/control',{...body,...extra})).status,400);
    }
    assert.equal(f.calls.length,before);
  } finally {await f.close();}
});
test('console maps navigation keys and coalesces bounded wheel input at guest coordinates', async () => {
  const f=consoleUI(), sent=[], timers=[];
  f.context.setTimeout=callback=>{timers.push(callback);return timers.length;};
  f.context.send=value=>sent.push({...value});
  vm.runInContext("selected={inputBusy:false}; enqueueInput=send;",f.context);
  const image=f.element('#screen');
  image.classList.contains=()=>false; image.naturalWidth=1280;image.naturalHeight=800;
  image.getBoundingClientRect=()=>({left:100,top:50,width:640,height:400});
  let prevented=0;
  const wheel={clientX:350,clientY:200,deltaMode:0,deltaY:12,preventDefault(){prevented++;}};
  for (let i=0;i<10;i++) f.element('#console').wheel(wheel);
  timers.at(-1)();
  assert.equal(prevented,10);
  assert.deepEqual(sent.pop(),{command:'scroll',x:500,y:300,deltaY:3});
  f.element('#console').wheel({...wheel,deltaY:100000});timers.at(-1)();
  assert.equal(sent.pop().deltaY,10);
  for (const [key,chord] of [['Home','home'],['End','end'],['PageUp','pageup'],['PageDown','pagedown']]) {
    f.element('#console').keydown({key,preventDefault(){}});
    assert.deepEqual(sent.pop(),{command:'key',chord});
  }
  f.element('#console').wheel({...wheel,deltaY:120});
  vm.runInContext("generation='new-generation'",f.context);timers.at(-1)();
  assert.equal(sent.length,0);
  f.element('#console').wheel({...wheel,deltaY:120});
  vm.runInContext('inputActive=true',f.context);timers.at(-1)();
  assert.equal(sent.length,0);
  vm.runInContext('inputActive=false',f.context);
  f.element('#console').wheel(wheel);
  vm.runInContext('disconnect()',f.context);timers.at(-1)();
  assert.equal(sent.length,0);
  f.element('#console').wheel(wheel);
  assert.equal(prevented,14);
});
test('restart permits guest shutdown confirmation while refusing duplicate power operations', async () => {
  let began, finish;
  const started = new Promise(resolve => began = resolve), completion = new Promise(resolve => finish = resolve);
  const f = await fixture(true, async body => { if (body.command === 'restart') { began(); await completion; return {ok:true}; } });
  let restart;
  try {
    await f.login(); await f.otp();
    restart = f.request('/api/vms/macos/control', {command:'restart',vmID:id,force:false});
    await started;
    assert.equal((await f.request('/api/vms/macos/frame?vmID='+id)).status,200);
    assert.equal((await f.request('/api/vms/macos/control',{command:'key',vmID:id,generation:'frame-test',chord:'return'})).status,200);
    for (const command of ['start','stop','restart']) assert.equal((await f.request('/api/vms/macos/control',{command,vmID:id})).status,409);
    finish(); assert.equal((await restart).status,200);
    assert.equal((await f.request('/api/vms/macos/control',{command:'start',vmID:id})).status,200);
  } finally { finish(); if (restart) await restart; await f.close(); }
});

test('partial-body controls are rejected after logout or session expiry', async () => {
  for (const revoke of ['logout','idle','absolute']) {
    const f=await fixture(); let pending;
    try {
      await f.login(revoke==='absolute'); await f.otp();
      pending=await f.beginRequest('/api/vms/macos/control',{command:'hide',vmID:id},true);
      if (revoke==='logout') assert.equal((await f.request('/auth/logout',{})).status,200);
      else f.advance(revoke==='idle'?16*60*1000:184*24*60*60*1000);
      pending.finish(); assert.equal((await pending.response).status,401);
      assert.equal(f.calls.length,0);
    } finally { pending?.destroy(); await f.close(); }
  }
});

test('queued controls and restart revalidate sessions while dispatched work finishes', async () => {
  for (const revoke of ['logout','idle']) {
    let began,finish;
    const started=new Promise(resolve=>began=resolve),completion=new Promise(resolve=>finish=resolve);
    const f=await fixture(true,async body=> { if (body.command==='type-text') {began();await completion;return {ok:true};} });
    let active; const waiting=[];
    try {
      await f.login(false); await f.otp();
      active=f.request('/api/vms/macos/control',{command:'type-text',vmID:id,generation:'frame-test',requestId:'queued-test',text:'public'});
      await started;
      for (const command of ['hide','restart']) waiting.push(await f.beginRequest('/api/vms/macos/control',{command,vmID:id}));
      if (revoke==='logout') assert.equal((await f.request('/auth/logout',{})).status,200);
      else f.advance(16*60*1000);
      finish(); assert.equal((await active).status,200);
      for (const item of waiting) assert.equal((await item.response).status,401);
      assert.deepEqual(f.calls.map(item=>item.command),['type-text']);
      // Rejected restart releases its power reservation for a new ready session.
      f.useCookie('');f.advance();await f.login();await f.otp();
      assert.equal((await f.request('/api/vms/macos/control',{command:'start',vmID:id})).status,200);
    } finally {finish();if(active) await active;for(const item of waiting) item.destroy();await f.close();}
  }
});
