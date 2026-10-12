import test from 'node:test';
import assert from 'node:assert/strict';
import { Client, InMemoryTransport } from '@modelcontextprotocol/client';
import { StdioClientTransport } from '@modelcontextprotocol/client/stdio';
import { createServer } from '../MCP/server.mjs';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { createServer as socketServer } from 'node:net';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import fs from 'node:fs';
import {execFileSync} from 'node:child_process';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const ID = '00000000-0000-0000-0000-000000000001';
const args = { name: 'linux', vmID: ID };
const PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=';

async function connected(t, call, version = '2025-11-25') {
  const server = createServer({ call });
  const client = new Client({ name: 'virty-tests', version: '1.0.0' }, { supportedProtocolVersions: [version] });
  const [a, b] = InMemoryTransport.createLinkedPair();
  await server.connect(a);
  await client.connect(b);
  t.after(async () => { await client.close(); await server.close(); });
  return client;
}

test('MCP request IDs obey the shared manager contract before dispatch', async t => {
  const requests=[];
  const client=await connected(t,async request=>{requests.push(request); return {ok:true};});
  const cases=JSON.parse(fs.readFileSync(path.join(ROOT,'Tests/request-id-cases.json')));
  for (const [tool,extra] of [['vm_type_text',{generation:'frame-1',text:'public'}],['vm_type_secret',{generation:'frame-1',alias:'vm-login'}],['vm_typing_status',{}]]) for (const item of cases) {
    const before=requests.length;
    const result=await client.callTool({name:tool,arguments:{...args,...extra,requestId:item.value}});
    assert.equal(result.isError,!item.valid,`${tool} ${JSON.stringify(item)}`);
    assert.equal(requests.length,before+(item.valid?1:0));
  }
});

for (const version of ['2025-11-25', '2025-06-18', '2024-11-05']) {
  test(`SDK negotiation, schemas and native images (${version})`, async t => {
    const requests = [];
    const client = await connected(t, async request => {
      requests.push(request);
      return request.command === 'screenshot' ? { ok: true, png: PNG, width: 1, height: 1, generation: 'frame-1', cached: true, updatedAt: '2026-10-10T21:50:00Z' } : { ok: true, vms: [] };
    }, version);
    assert.equal(client.getServerVersion().name, 'virty');
    const tools = (await client.listTools()).tools;
    assert.equal(tools.length, 35);
    assert.deepEqual(tools.map(tool => tool.name), (await client.listTools()).tools.map(tool => tool.name));
    assert.ok(tools.every(tool => tool.inputSchema.additionalProperties === false));
    assert.equal(tools.find(tool => tool.name === 'vm_secret_aliases').annotations.readOnlyHint, true);
    assert.equal(tools.find(tool => tool.name === 'vm_unregister').annotations.destructiveHint, true);
    assert.equal(tools.find(tool => tool.name === 'vm_snapshot_restore').annotations.destructiveHint, true);
    assert.ok(!tools.some(tool => /enroll|export|read_secret|shell/.test(tool.name)));
    const shot = await client.callTool({ name: 'vm_screenshot', arguments: args });
    assert.equal(shot.isError, false);
    assert.deepEqual(shot.content[1], { type: 'image', data: PNG, mimeType: 'image/png' });
    assert.deepEqual(JSON.parse(shot.content[0].text), shot.structuredContent);
    assert.equal(shot.structuredContent.generation, 'frame-1');
    assert.equal(shot.structuredContent.cached, true);
    assert.equal(shot.structuredContent.updatedAt, '2026-10-10T21:50:00Z');
    assert.ok(!shot.content[0].text.includes(PNG));
    assert.ok(!Object.hasOwn(shot.structuredContent, 'png'));
    assert.deepEqual(requests.map(request => request.command), ['screenshot']);
    await client.callTool({ name: 'vm_screenshot', arguments: { ...args, show: true } });
    assert.deepEqual(requests.slice(-2).map(request => request.command), ['show', 'screenshot']);
    const before = requests.length;
    for (const arguments_ of [{ name: 'linux' }, { ...args, secret: 'forbidden' }]) {
      const error = await client.callTool({ name: 'vm_screenshot', arguments: arguments_ });
      assert.equal(error.isError, true);
    }
    const invalidSecret = await client.callTool({ name: 'vm_type_secret', arguments: { ...args, alias: 'vm-login', generation: 'frame-1', requestId: 'once', value: 'forbidden' } });
    assert.equal(invalidSecret.isError, true);
    assert.equal(requests.length, before);
  });
}

test('macOS creation accepts only a boolean Setup Assistant option', async t => {
  const requests = [];
  const client = await connected(t, async request => { requests.push(request); return {ok:true,jobId:ID,state:'queued'}; });
  const creation = {name:'mac',guestOS:'macos',image:'/public-test.ipsw',setupAssistant:true};
  assert.equal((await client.callTool({name:'vm_create',arguments:creation})).isError,false);
  assert.equal(requests[0].setupAssistant,true);
  for (const extra of [{setupAssistant:'true'},{password:'forbidden'}]) {
    assert.equal((await client.callTool({name:'vm_create',arguments:{...creation,...extra}})).isError,true);
  }
  assert.equal(requests.length,1);
});

test('checkpoint and cloning tools enforce identity, strict fields and destructive annotations', async t => {
  const requests = [];
  const client = await connected(t, async request => { requests.push(request); return {ok:true,jobId:ID,state:'queued'}; });
  for (const [tool, extra] of [['vm_snapshot_create',{label:'before-upgrade'}],['vm_snapshot_restore',{snapshotID:ID}],['vm_snapshot_delete',{snapshotID:ID}],['vm_snapshot_recover',{}],['vm_clone',{newName:'copy',snapshotID:ID}]]) {
    assert.equal((await client.callTool({name:tool,arguments:{...args,...extra}})).isError,false);
    assert.equal((await client.callTool({name:tool,arguments:{...args,...extra,password:'forbidden'}})).isError,true);
    assert.equal((await client.callTool({name:tool,arguments:{name:args.name,...extra}})).isError,true);
  }
  assert.deepEqual(requests.map(request=>request.command),['snapshot-create','snapshot-restore','snapshot-delete','snapshot-recover','clone']);
  assert.equal((await client.callTool({name:'vm_clone',arguments:{...args,newName:'../bad'}})).isError,true);
  assert.equal((await client.callTool({name:'vm_snapshot_restore',arguments:{...args,snapshotID:'wrong'}})).isError,true);
});

test('power wait leaves console/cancel available and refuses overlapping power commands', async t => {
  let release;
  let entered;
  const started = new Promise(resolve => { entered = resolve; });
  const wait = new Promise(resolve => { release = resolve; });
  const requests = [];
  const client = await connected(t, async request => {
    requests.push(request.command);
    if (request.command === 'restart') { entered(); await wait; }
    return request.command === 'screenshot' ? { ok: true, png: PNG, generation: 'frame-1' } : { ok: true };
  });
  const restart = client.callTool({ name: 'vm_restart', arguments: args });
  await started;
  assert.equal((await client.callTool({ name: 'vm_stop', arguments: args })).isError, true);
  assert.equal((await client.callTool({ name: 'vm_screenshot', arguments: args })).content[1].type, 'image');
  assert.equal((await client.callTool({ name: 'vm_cancel_typing', arguments: args })).isError, false);
  release();
  await restart;
  assert.deepEqual(requests, ['restart', 'screenshot', 'cancel']);
});

test('lost typing response preserves caller retry ID; no invalid image leaks into text', async t => {
  const client = await connected(t, async request => {
    if (request.command === 'screenshot') return { ok: true, png: 'not-a-png' };
    throw new Error('Connection lost');
  });
  const response = await client.callTool({ name: 'vm_type_secret', arguments: { ...args, generation: 'frame-1', requestId: 'stable-id', alias: 'vm-login' } });
  assert.equal(response.isError, true);
  assert.equal(response.structuredContent.requestId, 'stable-id');
  const shot = await client.callTool({ name: 'vm_screenshot', arguments: { ...args, show: false } });
  assert.equal(shot.isError, true);
  assert.ok(!JSON.stringify(shot).includes('not-a-png'));
});

test('real stdio SDK → Python → named Unix worker roundtrip', async t => {
  const temporary = await mkdtemp('/tmp/virty-mcp-');
  t.after(() => rm(temporary, { recursive: true, force: true }));
  // This worker delegates the ID decision to the actual Swift predicate used
  // by Controller, rather than accepting every adapter-valid ID unconditionally.
  const validator = path.join(temporary, 'validate-request-id');
  execFileSync('/usr/bin/xcrun', ['swiftc', '-swift-version', '5', '-warnings-as-errors',
    '-module-cache-path', path.join(temporary, 'swift-cache'),
    'Sources/Support.swift', 'Sources/OperationLedger.swift', 'Tests/OperationLedgerTests.swift', '-o', validator], {cwd:ROOT});
  const home = path.join(temporary, 'vm');
  const registry = path.join(temporary, 'registry');
  await mkdir(home, { mode: 0o700 });
  await mkdir(registry, { mode: 0o700 });
  await writeFile(path.join(home, 'vm.json'), JSON.stringify({ id: ID, guestOS: 'linux', keychain: 'broker' }), { mode: 0o600 });
  await writeFile(path.join(registry, 'vms.json'), JSON.stringify({ version: 1, default: 'linux', vms: { linux: { id: ID, home, guestOS: 'linux', enabled: true, autostart: false } } }), { mode: 0o600 });
  const requests = [];
  const sockets = new Set();
  const worker = socketServer(socket => {
    sockets.add(socket);
    socket.on('close', () => sockets.delete(socket));
    let buffer = '';
    socket.on('data', data => {
      buffer += data;
      if (!buffer.includes('\n')) return;
      const request = JSON.parse(buffer);
      requests.push(request);
      let response = { ok: true, vmID: ID };
      if (request.command === 'status') response = { ...response, state: 'running', inputBusy: false, consoleVisible: true };
      if (request.command === 'screenshot') response = { ...response, png: PNG, width: 1, height: 1, generation: 'frame-1' };
      if (['type-secret','type-text','operation'].includes(request.command)) {
        const valid = execFileSync(validator, ['--validate-request-id', request.requestId], {encoding:'utf8'}).trim() === 'true';
        response = valid ? { ...response, requestId: request.requestId, status: 'accepted' } : {ok:false,error:'Manager rejected request ID.'};
      }
      if (request.generation && request.generation !== 'frame-1') response = { ok: false, error: 'Stale display generation.' };
      socket.end(JSON.stringify(response) + '\n');
    });
  });
  await new Promise((resolve, reject) => { worker.once('error', reject); worker.listen(path.join(home, 'control.sock'), resolve); });
  t.after(async () => { for (const socket of sockets) socket.destroy(); await new Promise(resolve => worker.close(resolve)); });
  const transport = new StdioClientTransport({ command: path.join(ROOT, 'bin/virty-mcp'), env: { ...process.env, VIRTY_REGISTRY_HOME: registry }, stderr: 'pipe' });
  let stderr = '';
  transport.stderr.on('data', chunk => { stderr += chunk; });
  const client = new Client({ name: 'stdio-tests', version: '1.0.0' });
  t.after(() => client.close());
  await client.connect(transport);
  const list = (await client.callTool({ name: 'vm_list', arguments: {} })).structuredContent;
  assert.equal(list.vms[0].vmID, ID);
  assert.equal(list.vms[0].state, 'running');
  const shot = await client.callTool({ name: 'vm_screenshot', arguments: args });
  assert.equal(shot.content[1].data, PNG);
  const typed = await client.callTool({ name: 'vm_type_secret', arguments: { ...args, alias: 'vm-login', generation: shot.structuredContent.generation, requestId: 'delivery-1' } });
  assert.equal(typed.structuredContent.status, 'accepted');
  assert.deepEqual(requests.at(-1), { command: 'type-secret', vmID: ID, alias: 'vm-login', delayMs: 80, requestId: 'delivery-1', generation: 'frame-1' });
  for (const requestId of ['-', 'A'.repeat(128)]) {
    const accepted=await client.callTool({name:'vm_type_text',arguments:{...args,generation:'frame-1',requestId,text:'public'}});
    assert.equal(accepted.isError,false);
    assert.equal(accepted.structuredContent.status,'accepted');
  }
  const beforeIDs=requests.length;
  for (const requestId of ['task_1','task-1\n','é','A'.repeat(129)]) {
    assert.equal((await client.callTool({name:'vm_type_text',arguments:{...args,generation:'frame-1',requestId,text:'public'}})).isError,true);
  }
  assert.equal(requests.length,beforeIDs);
  const scroll = await client.callTool({name:'vm_scroll', arguments:{...args,x:0,y:0,deltaY:3,generation:'frame-1'}});
  assert.equal(scroll.isError, false);
  assert.deepEqual(requests.at(-1), {command:'scroll',vmID:ID,x:0,y:0,deltaY:3,generation:'frame-1'});
  assert.equal((await client.callTool({name:'vm_scroll',arguments:{...args,x:0,y:0,deltaY:3,generation:'stale'}})).isError,true);
  const beforeScroll = requests.length;
  for (const extra of [{deltaY:11},{deltaY:true},{deltaY:0},{generation:undefined},{x:-1},{password:'forbidden'}]) {
    assert.equal((await client.callTool({name:'vm_scroll',arguments:{...args,x:0,y:0,deltaY:3,generation:'frame-1',...extra}})).isError,true);
  }
  assert.equal(requests.length,beforeScroll);
  const stale = await client.callTool({ name: 'vm_click', arguments: { ...args, x: 0, y: 0, generation: 'stale' } });
  assert.equal(stale.isError, true);
  const count = requests.length;
  const wrong = await client.callTool({ name: 'vm_start', arguments: { ...args, vmID: '00000000-0000-0000-0000-000000000002' } });
  assert.equal(wrong.isError, true);
  assert.equal(requests.length, count);
  await client.close();
  assert.equal(stderr, '');
});
