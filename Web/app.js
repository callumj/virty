const app = document.querySelector('#app');
const powerActive = new Set();
let scrollTimer, pendingScroll;
const checkpointActive = new Set();
let checkpoints = [];
let session, selected, rows = [], generation, connected = false, frameURL, frameTimer, inventoryTimer, inputTail = Promise.resolve(), inputActive = false, epoch = 0;
const $ = selector => document.querySelector(selector);
const brand = '<div class="brand"><span class="mark">›_</span> Virty</div>';
async function api(url, data) {
  const response = await fetch(url, {method: data === undefined ? 'GET' : 'POST', headers: data === undefined ? {} : {'Content-Type': 'application/json', 'X-CSRF-Token': session.csrf}, body: data === undefined ? undefined : JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok || !result.ok) {
    if (response.status === 401 && session.stage === 'ready') { disconnect(); await boot(); }
    throw new Error(result.error || 'Request failed.');
  }
  return result;
}
async function boot() {
  clearInterval(inventoryTimer); clearTimeout(frameTimer);
  session = await api('/api/session');
  if (session.stage === 'ready') { history.replaceState(null, '', '/'); dashboard(); await inventory(); inventoryTimer = setInterval(() => inventory().catch(notice), 5000); }
  else if (session.stage === 'otp' || session.stage === 'enroll') await otpPage();
  else loginPage(location.pathname === '/setup' && session.setupRequired);
}
function loginPage(setup = false) {
  history.replaceState(null, '', setup ? '/setup' : '/login');
  app.innerHTML = `<div class="auth-wrap"><section class="auth">${brand}<div class="steps"><span class="active"></span><span></span></div><h1>${setup ? 'Set up your console' : 'Welcome back'}</h1><p class="muted">${setup ? 'Choose your login, then add an authenticator.' : 'Sign in to manage your virtual machines.'}</p><form id="login">${setup ? '<label for="setup-code">Local setup code</label><input id="setup-code" type="password" autocomplete="off" required><p class="muted">Run <code>virty-web setup</code> on your Mac to get this code.</p>' : ''}<label for="username">Username</label><input id="username" autocomplete="username" maxlength="64" required><label for="password">Password</label><input id="password" type="password" autocomplete="${setup ? 'new-password' : 'current-password'}" ${setup ? 'minlength="12"' : ''} maxlength="256" required><label class="remember" for="remember"><input id="remember" type="checkbox" checked>Remember me for six months</label><div class="error" id="error" role="alert"></div><button class="primary" type="submit">${setup ? 'Continue to authenticator setup' : 'Continue'}</button></form><div class="auth-footer muted">${session.setupRequired && !setup ? 'First visit? <button class="link" id="setup-link">Set up your account</button>' : 'Password + authenticator code'}</div></section></div>`;
  $('#setup-link')?.addEventListener('click', () => loginPage(true));
  $('#login').addEventListener('submit', async event => {
    event.preventDefault(); const button = $('#login button'); button.disabled = true; $('#error').textContent = '';
    try {
      const result = await api(setup ? '/auth/setup' : '/auth/login', {username: $('#username').value, password: $('#password').value, rememberMe: $('#remember').checked, ...(setup ? {code: $('#setup-code').value.trim()} : {})});
      $('#password').value = ''; history.replaceState(null, '', result.next); await boot();
    } catch (error) { $('#error').textContent = error.message; button.disabled = false; }
  });
}
async function otpPage() {
  const enrollment = session.stage === 'enroll';
  history.replaceState(null, '', enrollment ? '/enroll' : '/otp');
  app.innerHTML = `<div class="auth-wrap"><section class="auth">${brand}<div class="steps"><span class="active"></span><span class="active"></span></div><h1>${enrollment ? 'Add your authenticator' : 'Verify it’s you'}</h1><p class="muted">${enrollment ? 'Add a time-based account in your authenticator app using this setup key. Enter its six-digit code to finish.' : 'Enter the six-digit code from your authenticator app.'}</p>${enrollment ? '<div class="secret-key" id="totp-key"></div><div class="row"><button id="copy-key">Copy setup key</button><a id="authenticator-link">Open authenticator</a></div>' : ''}<form id="otp"><label for="code">Authenticator code</label><input id="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" placeholder="000000" required><div class="error" id="error" role="alert"></div><button class="primary" type="submit">${enrollment ? 'Finish setup' : 'Sign in'}</button></form><div class="auth-footer"><button class="link" id="back">Back to sign in</button></div></section></div>`;
  if (enrollment) {
    const info = await api('/api/enrollment'); $('#totp-key').textContent = info.secret; $('#authenticator-link').href = info.uri;
    $('#copy-key').onclick = async () => { await navigator.clipboard.writeText(info.secret); $('#copy-key').textContent = 'Copied'; };
  }
  $('#back').onclick = async () => { await api('/auth/logout', {}); await boot(); };
  $('#otp').onsubmit = async event => {
    event.preventDefault(); const button = $('#otp button'); button.disabled = true;
    try { await api('/auth/otp', {code: $('#code').value}); await boot(); }
    catch (error) { $('#error').textContent = error.message; button.disabled = false; $('#code').select(); }
  };
  $('#code').focus();
}
function dashboard() {
  app.innerHTML = `<header class="topbar">${brand}<div class="account"><span class="muted" id="account"></span><button id="logout">Sign out</button></div></header><div class="layout"><aside class="sidebar"><div class="sidebar-heading"><span class="eyebrow">Virtual machines</span><span class="count" id="count">0</span></div><nav class="vm-list" id="vm-list" aria-label="Virtual machines"></nav><p class="sidebar-note">Each VM has its own controls and managed secret aliases.</p></aside><section class="workspace"><div class="heading"><div><h1 id="vm-title">Your virtual machines</h1><div class="muted" id="vm-subtitle">Select a VM to get started.</div></div><div class="actions"><button id="start">Start</button><button id="stop">Stop</button><button id="restart">Restart</button></div></div><div class="notice hidden" id="notice" role="status"></div><div class="console-panel"><div class="console-toolbar"><div class="console-title">Console <span class="muted" id="display-size">Screen + keyboard</span></div><button id="connect" class="primary">Connect</button></div><div class="console" id="console" tabindex="0" role="application" aria-label="VM screen. Click to focus keyboard control."><img id="screen" class="hidden" alt="Selected VM screen" draggable="false"><div class="placeholder" id="placeholder"><strong id="placeholder-title">Console disconnected</strong><p id="placeholder-text">Connect to see and control the VM.</p></div></div><div class="console-footer"><span id="console-status">Not connected</span><span>Click screen to focus · Scroll over screen · ANSI US keyboard</span></div></div><div class="tools"><section class="panel"><h2>Managed secrets</h2><p>Focus a password field in the guest, then send its alias. The value stays with the manager.</p><label for="alias">Secret alias</label><div class="row"><select id="alias"><option>No aliases loaded</option></select><button id="send-secret">Send</button></div><div class="operation" id="operation"></div><div class="row"><button id="cancel-input">Cancel typing</button><button id="check-operation">Check delivery</button></div></section><section class="panel"><h2>Keyboard</h2><p>Use the screen keyboard, paste text, or type a hidden password.</p><div class="shortcuts"><button data-chord="return">Enter</button><button data-chord="tab">Tab</button><button data-chord="escape">Esc</button><button data-chord="home">Home</button><button data-chord="end">End</button><button data-chord="pageup">Page Up</button><button data-chord="pagedown">Page Down</button><button data-chord="cmd+l">⌘ L</button><button data-chord="ctrl+c">Ctrl C</button><button data-chord="ctrl+alt+delete">Ctrl Alt Del</button></div><label for="public-text">Public text</label><textarea id="public-text" placeholder="Text to type in the guest." maxlength="4096"></textarea><div class="row"><button id="send-text">Type text</button></div><div class="row"><button id="open-guest-input">Type password…</button></div><p>Focus the guest’s password field first, then open the masked entry dialog.</p></section></div></section></div><dialog id="guest-input-dialog" aria-labelledby="guest-input-title"></dialog><dialog id="power-dialog"><h2 id="power-title"></h2><p>Save your work in the guest before continuing.</p><label><input type="checkbox" id="force">Force power off immediately. Unsaved work may be lost.</label><div class="actions"><button id="power-cancel">Cancel</button><button id="power-confirm" class="danger">Continue</button></div></dialog>`;
  $('#account').textContent = session.username;
  checkpointPanel();
  $('#logout').onclick = async () => { disconnect(); await api('/auth/logout', {}); await boot(); };
  $('#connect').onclick = () => connected ? disconnect() : connect().catch(notice);
  $('#start').onclick = () => control({command: 'start'}).then(inventory).catch(notice);
  for (const command of ['stop', 'restart']) $('#' + command).onclick = () => powerDialog(command);
  $('#send-secret').onclick = () => typing('type-secret', {alias: $('#alias').value, delayMs: 80});
  $('#send-text').onclick = () => { const text = $('#public-text').value; if (text) typing('type-text', {text, delayMs: 20}); };
  $('#open-guest-input').onclick = guestInputDialog;
  $('#cancel-input').onclick = () => control({command: 'cancel'}).then(() => notice('Cancellation requested. Delivery may be partial.')).catch(notice);
  $('#check-operation').onclick = () => { const requestId = $('#operation').dataset.id; if (requestId) control({command: 'operation', requestId}).then(result => notice('Delivery status: ' + result.status)).catch(notice); };
  document.querySelectorAll('[data-chord]').forEach(button => button.onclick = () => enqueueInput({command: 'key', chord: button.dataset.chord}));
  $('#console').addEventListener('click', event => {
    $('#console').focus(); if (!connected || !generation || $('#screen').classList.contains('hidden')) return;
    const rect = $('#screen').getBoundingClientRect();
    const x = Math.floor((event.clientX - rect.left) / rect.width * $('#screen').naturalWidth), y = Math.floor((event.clientY - rect.top) / rect.height * $('#screen').naturalHeight);
    enqueueInput({command: 'click', x, y});
  });
  $('#console').addEventListener('wheel', scrollWheel, {passive:false});
  $('#console').addEventListener('keydown', event => {
    if (!connected || !generation || event.isComposing) return;
    event.preventDefault(); if (['Meta','Control','Shift','Alt','CapsLock'].includes(event.key)) return;
    const special = {Home:'home',End:'end',PageUp:'pageup',PageDown:'pagedown',Enter:'return',Tab:'tab',Escape:'escape',Backspace:'backspace',Delete:'delete',ArrowLeft:'left',ArrowRight:'right',ArrowUp:'up',ArrowDown:'down',' ':'space'};
    if (!event.metaKey && !event.ctrlKey && !event.altKey && event.key.length === 1 && event.key !== ' ') {
      enqueueInput({command:'type-text', text:event.key, delayMs:20, requestId:crypto.randomUUID()}); return;
    }
    const key = special[event.key] || (event.key.length === 1 ? event.key.toLowerCase() : null);
    if (!key) return;
    const modifiers = [event.ctrlKey && 'ctrl',event.metaKey && 'cmd',event.altKey && 'alt',event.shiftKey && 'shift'].filter(Boolean);
    enqueueInput({command:'key', chord:[...modifiers,key].join('+')});
  });
  renderSelection();
}
function scrollWheel(event) {
  if (!connected || !generation || selected?.inputBusy || inputActive || event.ctrlKey || !event.deltaY || $('#screen').classList.contains('hidden')) return;
  const image = $('#screen'), rect = image.getBoundingClientRect();
  const x = Math.floor((event.clientX - rect.left) / rect.width * image.naturalWidth);
  const y = Math.floor((event.clientY - rect.top) / rect.height * image.naturalHeight);
  if (x < 0 || y < 0 || x >= image.naturalWidth || y >= image.naturalHeight) return;
  event.preventDefault();
  // Coalesce trackpad fractions; keep one bounded gesture pending, never a wheel-event backlog.
  const scale = event.deltaMode === 1 ? 1 : event.deltaMode === 2 ? 10 : 1 / 40;
  if (!pendingScroll || pendingScroll.epoch !== epoch || pendingScroll.generation !== generation) pendingScroll = {x,y,epoch,generation,deltaY:0};
  Object.assign(pendingScroll, {x,y});
  pendingScroll.deltaY = Math.max(-10, Math.min(10, pendingScroll.deltaY + event.deltaY * scale));
  if (scrollTimer === undefined) scrollTimer = setTimeout(flushScroll, 40);
}
function flushScroll() {
  scrollTimer = undefined;
  const pending = pendingScroll;
  if (!pending || pending.epoch !== epoch || !connected || generation !== pending.generation || selected?.inputBusy || inputActive) { pendingScroll = undefined; return; }
  const steps = value => Math.trunc(value + Math.sign(value) * 1e-9);
  const deltaY = steps(pending.deltaY);
  if (!deltaY) return;
  pendingScroll = {...pending,deltaY:pending.deltaY-deltaY};
  enqueueInput({command:'scroll',x:pending.x,y:pending.y,deltaY});
}
function clearGuestInput() {
  const dialog = $('#guest-input-dialog');
  if (!dialog) return;
  if ($('#guest-secret')) $('#guest-secret').value = '';
  dialog.close(); dialog.replaceChildren();
}
function guestInputDialog() {
  if (!connected || !generation || selected?.inputBusy || inputActive) return;
  const dialog = $('#guest-input-dialog');
  // Only mount native secure input after an explicit request: an idle console is not a login form.
  dialog.innerHTML = '<div data-1p-ignore><h2 id="guest-input-title">Type password into the guest</h2><p id="guest-input-help">Focus the guest’s password field first. This value clears when sent or cancelled; it is not saved.</p><label for="guest-secret">Guest password</label><input id="guest-secret" type="password" autocomplete="off" data-1p-ignore autocapitalize="off" spellcheck="false" maxlength="4096" aria-describedby="guest-input-help" required><div class="actions"><button id="guest-input-cancel" type="button">Cancel</button><button id="guest-input-send" class="primary" type="button">Type password</button></div></div>';
  dialog.oncancel = clearGuestInput;
  dialog.onclose = () => { if (!dialog.open) { if ($('#guest-secret')) $('#guest-secret').value = ''; dialog.replaceChildren(); } };
  $('#guest-input-cancel').onclick = clearGuestInput;
  const send = () => {
    const text = $('#guest-secret').value;
    if (!text) return;
    clearGuestInput();
    if (!connected || !generation || selected?.inputBusy || inputActive) { notice(new Error('Guest input is unavailable. Reconnect and try again.')); return; }
    typing('type-text', {text, delayMs: 80});
  };
  $('#guest-input-send').onclick = send;
  $('#guest-secret').onkeydown = event => { if (event.key === 'Enter' && !event.isComposing) { event.preventDefault(); send(); } };
  dialog.showModal(); $('#guest-secret').focus();
}
function notice(value) {
  const element = $('#notice'); if (!element) return;
  element.textContent = value instanceof Error ? value.message : String(value); element.classList.remove('hidden'); element.classList.toggle('failure', value instanceof Error);
}
async function inventory() {
  const result = await api('/api/vms'); rows = result.vms;
  const nav = $('#vm-list'); if (!nav) return; nav.replaceChildren(); $('#count').textContent = rows.length;
  for (const row of rows) {
    const button = document.createElement('button'); button.className = 'vm-card' + (row.vmID === selected?.vmID ? ' selected' : '');
    const line = document.createElement('div'); line.className = 'vm-line';
    const name = document.createElement('span'); name.className = 'vm-name'; name.textContent = row.name;
    const state = document.createElement('span'); state.className = 'state ' + (row.state === 'running' ? 'running' : ''); state.textContent = row.state;
    line.append(name, state); const os = document.createElement('div'); os.className = 'vm-os'; os.textContent = (row.guestOS === 'macos' ? 'macOS' : 'Linux ARM64') + (row.default ? ' · Default' : '');
    button.append(line, os); button.onclick = () => selectVM(row); nav.append(button);
  }
  if (!selected && rows.length) await selectVM(rows.find(row => row.default) || rows[0]);
  else if (selected) {
    const current = rows.find(row => row.vmID === selected.vmID);
    if (!current || current.name !== selected.name) { disconnect(); selected = current; if (current) await aliases(); }
    else selected = current;
  }
  renderSelection();
}
async function selectVM(row) {
  if (selected?.vmID !== row.vmID) { disconnect(); selected = row; $('#operation').textContent = ''; delete $('#operation').dataset.id; await aliases(); await refreshCheckpoints(); }
  renderSelection();
  document.querySelectorAll('.vm-card').forEach(button => button.classList.toggle('selected', button.querySelector('.vm-name').textContent === row.name));
}
function renderSelection() {
  if (!$('#vm-title')) return;
  if (selected) { $('#vm-title').textContent = selected.name; $('#vm-subtitle').textContent = (selected.guestOS === 'macos' ? 'macOS' : 'Linux ARM64') + ' · ' + selected.state; }
  const running = selected?.state === 'running', busy = selected?.inputBusy || inputActive, powering = powerActive.has(selected?.vmID) || checkpointActive.has(selected?.vmID);
  $('#start').disabled = !selected || selected.state !== 'stopped' || busy || powering;
  $('#stop').disabled = !running || busy || powering; $('#restart').disabled = !selected || !['running','stopped'].includes(selected.state) || busy || powering;
  $('#connect').disabled = !running; $('#connect').textContent = connected ? 'Disconnect' : 'Connect';
  for (const id of ['send-secret','send-text','open-guest-input']) $('#' + id).disabled = !connected || !generation || busy || (id === 'send-secret' && !$('#alias').value);
  document.querySelectorAll('[data-chord]').forEach(button => button.disabled = !connected || !generation || busy);
  $('#cancel-input').disabled = !selected; $('#check-operation').disabled = !$('#operation').dataset.id;
  if ($('#checkpoint-create')) {
    const stopped = ['stopped', 'offline'].includes(selected?.state), chosen = $('#checkpoint-select').value;
    $('#checkpoint-create').disabled = !selected || !stopped || busy || powering;
    $('#checkpoint-restore').disabled = !selected || !stopped || !chosen || busy || powering;
    $('#checkpoint-delete').disabled = !selected || !chosen || powering;
    $('#checkpoint-clone').disabled = !selected || (!stopped && !$('#clone-source').value) || busy || powering;
    $('#checkpoint-refresh').disabled = !selected || powering;
    $('#checkpoint-recover').disabled = !selected || powering;
  }
  if (connected && !running) disconnect();
}
async function aliases() {
  const select = $('#alias'); select.replaceChildren();
  try { const result = await vmGet('aliases'); for (const alias of result.aliases) { const option = document.createElement('option'); option.value = alias; option.textContent = alias; select.append(option); } }
  catch (error) { notice(error); }
  if (!select.options.length) { const option = document.createElement('option'); option.value = ''; option.textContent = 'No managed secrets'; select.append(option); }
}
const vmGet = suffix => api(`/api/vms/${selected.name}/${suffix}?vmID=${selected.vmID}`);
async function control(values, vm = selected) {
  if (!vm) throw new Error('Select a VM first.');
  return api(`/api/vms/${vm.name}/control`, {vmID: vm.vmID, ...values});
}
function checkpointPanel() {
  const panel = document.createElement('section'); panel.className = 'panel';
  panel.innerHTML = '<h2>Checkpoints & clones</h2><p>Shut down the guest before saving or restoring. Checkpoints include its disk, boot state and managed secrets.</p><label for="checkpoint-label">New checkpoint label</label><div class="row"><input id="checkpoint-label" placeholder="before-upgrade" pattern="[a-z][a-z0-9_-]{0,47}" maxlength="48"><button id="checkpoint-create">Save checkpoint</button></div><label for="checkpoint-select">Saved checkpoint</label><select id="checkpoint-select"><option value="">No checkpoints</option></select><div class="row"><button id="checkpoint-restore">Restore</button><button id="checkpoint-delete">Delete checkpoint</button><button id="checkpoint-refresh">Refresh</button></div><label for="clone-name">New VM name</label><input id="clone-name" placeholder="ubuntu-experiment" maxlength="48"><label for="clone-source">Clone from</label><select id="clone-source"><option value="">Current stopped VM</option></select><div class="row"><button id="checkpoint-clone">Create clone</button><button id="checkpoint-recover" class="hidden">Recover interrupted operation</button></div><p id="checkpoint-status" role="status"></p>';
  $('.tools').append(panel);
  $('#checkpoint-refresh').onclick = () => refreshCheckpoints().catch(notice);
  $('#checkpoint-select').onchange = renderSelection; $('#clone-source').onchange = renderSelection;
  $('#checkpoint-create').onclick = () => checkpointOperation({command:'snapshot-create', label:$('#checkpoint-label').value.trim()});
  $('#checkpoint-clone').onclick = () => checkpointOperation({command:'clone', newName:$('#clone-name').value.trim(), ...($('#clone-source').value ? {snapshotID:$('#clone-source').value} : {})});
  $('#checkpoint-restore').onclick = () => {
    if (confirm('Restore this checkpoint? Changes to the VM disk and managed secrets since it was saved will be discarded.')) checkpointOperation({command:'snapshot-restore', snapshotID:$('#checkpoint-select').value});
  };
  $('#checkpoint-delete').onclick = () => {
    if (confirm('Permanently delete this checkpoint and its saved secret aliases? The current VM is preserved.')) checkpointOperation({command:'snapshot-delete', snapshotID:$('#checkpoint-select').value});
  };
  $('#checkpoint-recover').onclick = () => checkpointOperation({command:'snapshot-recover'});
}
async function refreshCheckpoints() {
  if (!selected) return;
  const vm = selected, result = await control({command:'snapshot-list'}, vm);
  if (selected?.vmID !== vm.vmID) return;
  checkpoints = result.snapshots;
  for (const id of ['checkpoint-select', 'clone-source']) {
    const select = $('#' + id), previous = select.value; select.replaceChildren();
    const first = document.createElement('option'); first.value = ''; first.textContent = id === 'clone-source' ? 'Current stopped VM' : 'Choose a checkpoint'; select.append(first);
    for (const item of checkpoints) {
      const option = document.createElement('option'); option.value = item.snapshotID;
      option.textContent = `${item.label} · ${new Date(item.createdAt * 1000).toLocaleString()}${item.state === 'deleting' ? ' · deletion incomplete' : ''}`;
      select.append(option);
    }
    if (checkpoints.some(item => item.snapshotID === previous)) select.value = previous;
  }
  $('#checkpoint-recover').classList.toggle('hidden', !result.recoveryRequired);
  renderSelection();
}
async function checkpointOperation(values) {
  const vm = selected; if (!vm || checkpointActive.has(vm.vmID)) return;
  checkpointActive.add(vm.vmID); renderSelection();
  try {
    let result = await control(values, vm);
    if (result.jobId) {
      const jobId = result.jobId;
      while (['queued','running'].includes(result.state)) {
        $('#checkpoint-status').textContent = `Working · job ${jobId}`;
        await new Promise(resolve => setTimeout(resolve, 1000));
        result = await control({command:'snapshot-job',jobId}, vm);
      }
      if (result.state !== 'completed') throw new Error(result.error || 'Checkpoint job failed. Refresh checkpoints to inspect recovery status.');
      result = result.result;
    }
    notice(values.command === 'clone' ? `Created ${result.name}. Start the clone when ready.` : 'Checkpoint operation complete. The guest is ready to start.');
    $('#checkpoint-status').textContent = '';
    if (selected?.vmID === vm.vmID) { await aliases(); await refreshCheckpoints(); }
    await inventory();
  } catch (error) { notice(error); if (selected?.vmID === vm.vmID) await refreshCheckpoints().catch(notice); }
  finally { checkpointActive.delete(vm.vmID); renderSelection(); }
}
async function connect() {
  if (!selected) return;
  connected = true; epoch++; $('#notice').classList.add('hidden'); renderSelection(); await refreshFrame(epoch);
}
function disconnect() {
  clearTimeout(scrollTimer); scrollTimer = undefined; pendingScroll = undefined;
  connected = false; generation = undefined; epoch++; clearTimeout(frameTimer);
  clearGuestInput();
  if (frameURL) { URL.revokeObjectURL(frameURL); frameURL = undefined; }
  $('#screen')?.classList.add('hidden'); $('#placeholder')?.classList.remove('hidden');
  if ($('#console-status')) $('#console-status').textContent = 'Not connected';
  if ($('#placeholder-title')) $('#placeholder-title').textContent = 'Console disconnected';
  if ($('#placeholder-text')) $('#placeholder-text').textContent = 'Connect to see and control the VM.';
  if ($('#connect')) $('#connect').textContent = 'Connect';
  renderSelection();
}
async function refreshFrame(currentEpoch) {
  if (!connected || epoch !== currentEpoch) return;
  try {
    if (inputActive) return;
    const response = await fetch(`/api/vms/${selected.name}/frame?vmID=${selected.vmID}`);
    if (!response.ok) { const error = await response.json(); if (response.status === 401) { disconnect(); await boot(); return; } throw new Error(error.error); }
    const blob = await response.blob(); if (epoch !== currentEpoch || !connected) return;
    const nextURL = URL.createObjectURL(blob), image = $('#screen');
    generation = response.headers.get('X-Display-Generation');
    image.onload = () => { if (frameURL) URL.revokeObjectURL(frameURL); frameURL = nextURL; };
    image.src = nextURL; image.classList.remove('hidden'); $('#placeholder').classList.add('hidden');
    $('#display-size').textContent = `${response.headers.get('X-Display-Width')} × ${response.headers.get('X-Display-Height')}`;
    $('#console-status').textContent = 'Connected · Clicks, scrolling and keyboard enabled'; renderSelection();
  } catch (error) {
    if (currentEpoch === epoch && $('#console-status')) {
      generation = undefined; $('#console-status').textContent = error.message;
      $('#screen').classList.add('hidden'); $('#placeholder').classList.remove('hidden');
      $('#placeholder-title').textContent = 'Screen unavailable';
      $('#placeholder-text').textContent = 'Waiting for the VM screen. Frames retry automatically.';
      renderSelection();
    }
  }
  finally { if (connected && epoch === currentEpoch) frameTimer = setTimeout(() => refreshFrame(currentEpoch), 800); }
}
function enqueueInput(values) {
  const vm = selected, display = generation, capturedEpoch = epoch;
  inputTail = inputTail.catch(() => {}).then(async () => {
    if (!connected || epoch !== capturedEpoch) return;
    inputActive = true; renderSelection();
    try { await control({...values, generation: display}, vm); }
    catch (error) { notice(error); if (values.requestId) recordOperation(values.requestId, 'Inspect delivery before retrying.'); }
    finally { inputActive = false; renderSelection(); }
  });
  return inputTail;
}
function recordOperation(id, message) { $('#operation').dataset.id = id; $('#operation').textContent = `${message} Operation ${id}`; renderSelection(); }
function typing(command, values) {
  const vm = selected, requestId = crypto.randomUUID(); recordOperation(requestId, 'Sending…');
  enqueueInput({command, ...values, requestId}).then(() => {
    if (selected?.vmID !== vm.vmID) return;
    control({command: 'operation', requestId}, vm).then(result => { if (selected?.vmID === vm.vmID) recordOperation(requestId, 'Delivery: ' + result.status + '.'); }).catch(notice);
  });
}
function powerDialog(command) {
  $('#power-title').textContent = `${command === 'stop' ? 'Stop' : 'Restart'} ${selected.name}?`; $('#force').checked = false;
  const dialog = $('#power-dialog'); dialog.showModal(); $('#power-cancel').onclick = () => dialog.close();
  $('#power-confirm').onclick = async () => {
    const force = $('#force').checked, vm = selected; dialog.close(); powerActive.add(vm.vmID);
    if (force) disconnect(); renderSelection(); notice(command === 'stop' ? 'Requesting shutdown…' : 'Restarting VM… Confirm shutdown in the guest if prompted.');
    try { const result = await control({command, force}, vm); notice(result.shutdownRequested ? 'Shutdown requested. The guest may need to confirm it.' : command === 'stop' ? 'VM stopped.' : 'VM restarted.'); await inventory(); }
    catch (error) { notice(error); }
    finally { powerActive.delete(vm.vmID); renderSelection(); }
  };
}
window.addEventListener('pagehide', () => { disconnect(); clearInterval(inventoryTimer); });
boot().catch(error => { app.replaceChildren(); const message = document.createElement('p'); message.className = 'loading'; message.textContent = error.message; app.append(message); });
