import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {promisify} from 'node:util';

export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const scrypt = promisify(crypto.scrypt);
const token = () => crypto.randomBytes(32).toString('base64url');
const hash = value => crypto.createHash('sha256').update(value).digest('hex');
const equal = (a, b) => typeof a === 'string' && typeof b === 'string' && Buffer.byteLength(a) === Buffer.byteLength(b) && crypto.timingSafeEqual(Buffer.from(a), Buffer.from(b));
const COOKIE = '__Host-virty';
const documentPaths = new Set(['/','/login','/otp','/setup','/enroll']);
const idleMs = 15 * 60 * 1000, absoluteMs = 8 * 60 * 60 * 1000;
const pendingMs = 5 * 60 * 1000;
// Anonymous entries are reclaimable; password-verified and ready sessions have
// an independent budget and are never evicted to admit an anonymous visitor.
const anonymousLimit = 128, protectedLimit = 256;
const allocationWindowMs = 60 * 1000, allocationPerIP = 8, allocationIPs = 1024;

export function sixMonthsFrom(time) {
  const date = new Date(time), day = date.getUTCDate();
  date.setUTCDate(1);
  date.setUTCMonth(date.getUTCMonth() + 6);
  const lastDay = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 0)).getUTCDate();
  date.setUTCDate(Math.min(day, lastDay));
  return date.getTime();
}

export function privateDirectory(directory) {
  fs.mkdirSync(directory, {recursive: true, mode: 0o700});
  const info = fs.lstatSync(directory);
  if (!info.isDirectory() || info.uid !== process.getuid() || (info.mode & 0o777) !== 0o700) throw new Error('Web state must be an owned directory with mode 0700.');
}
export function readPrivate(filename) {
  const info = fs.lstatSync(filename);
  if (!info.isFile() || info.uid !== process.getuid() || (info.mode & 0o777) !== 0o600) throw new Error('Web state files must be owned regular files with mode 0600.');
  return fs.readFileSync(filename, 'utf8');
}
export function writePrivate(filename, value) {
  const temporary = filename + '.' + token();
  const fd = fs.openSync(temporary, fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_WRONLY, 0o600);
  try { fs.writeFileSync(fd, value); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
  try { fs.renameSync(temporary, filename); } finally { if (fs.existsSync(temporary)) fs.unlinkSync(temporary); }
}
export async function passwordHash(password, salt = crypto.randomBytes(16).toString('hex')) {
  const digest = await scrypt(password, salt, 32, {N: 65536, r: 8, p: 1, maxmem: 128 * 1024 * 1024});
  return {salt, digest: digest.toString('hex')};
}
export function totp(secret, counter, digits = 6) {
  const buffer = Buffer.alloc(8); buffer.writeBigUInt64BE(BigInt(counter));
  const digest = crypto.createHmac('sha1', Buffer.from(secret, 'hex')).update(buffer).digest();
  const offset = digest[digest.length - 1] & 15;
  return String((digest.readUInt32BE(offset) & 0x7fffffff) % (10 ** digits)).padStart(digits, '0');
}
export function base32(hex) {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
  let bits = '', output = '';
  for (const byte of Buffer.from(hex, 'hex')) bits += byte.toString(2).padStart(8, '0');
  for (let i = 0; i < bits.length; i += 5) output += alphabet[parseInt(bits.slice(i, i + 5).padEnd(5, '0'), 2)];
  return output;
}
function fail(status, message) { throw Object.assign(new Error(message), {status}); }
function text(value, label, max = 256) {
  if (typeof value !== 'string' || value.length < 1 || value.length > max) fail(400, `Invalid ${label}.`);
  return value;
}
function username(value) {
  if (!/^[a-zA-Z0-9_.@-]{1,64}$/.test(text(value, 'username', 64))) fail(400, 'Invalid username.');
  return value;
}
function cookieValue(request) {
  const cookies = (request.headers.cookie || '').split(';').map(item => item.trim()).filter(item => item.startsWith(COOKIE + '='));
  return cookies.length === 1 ? cookies[0].slice(COOKIE.length + 1) : '';
}
async function body(request) {
  if (request.headers['content-type'] !== 'application/json' || request.headers['transfer-encoding']) fail(400, 'Send a JSON request with a content length.');
  const length = Number(request.headers['content-length']);
  if (!Number.isSafeInteger(length) || length < 2 || length > 32768) fail(413, 'Request exceeds the size limit.');
  const parts = []; let size = 0;
  for await (const part of request) { size += part.length; if (size > length) fail(413, 'Request exceeds the size limit.'); parts.push(part); }
  let value;
  try { value = JSON.parse(Buffer.concat(parts).toString()); } catch { fail(400, 'Invalid JSON.'); }
  if (!value || Array.isArray(value) || typeof value !== 'object') fail(400, 'Request must be an object.');
  return value;
}
export function bridge(request) {
  return new Promise((resolve, reject) => {
    const child = spawn('/usr/bin/python3', [path.join(ROOT, 'scripts/web_bridge.py')], {stdio: ['pipe', 'pipe', 'ignore'], env: {...process.env, PYTHONDONTWRITEBYTECODE: '1'}});
    const parts = []; let size = 0;
    const timeout = setTimeout(() => { child.kill(); reject(new Error('Control timed out. Inspect its operation ID before retrying typing.')); }, 95000);
    child.on('error', error => { clearTimeout(timeout); reject(error); });
    child.stdout.on('data', part => { size += part.length; if (size > 24 * 1024 * 1024) child.kill(); else parts.push(part); });
    child.on('close', code => {
      clearTimeout(timeout);
      if (code !== 0 || size > 24 * 1024 * 1024) return reject(new Error('VM adapter unavailable.'));
      try { resolve(JSON.parse(Buffer.concat(parts).toString())); } catch { reject(new Error('Invalid VM adapter response.')); }
    });
    child.stdin.on('error', () => {});
    child.stdin.end(JSON.stringify(request));
  });
}
const controlFields = {
  show: [], hide: [], start: [], stop: ['force'], restart: ['force'], cancel: [], operation: ['requestId'],
  click: ['x', 'y', 'generation'], key: ['chord', 'generation'],
  scroll: ['x', 'y', 'deltaY', 'generation'],
  'type-text': ['text', 'delayMs', 'requestId', 'generation'],
  'type-secret': ['alias', 'delayMs', 'requestId', 'generation'],
  'snapshot-list': [], 'snapshot-create': ['label'], 'snapshot-restore': ['snapshotID'],
  'snapshot-delete': ['snapshotID'], 'snapshot-recover': [], clone: ['newName', 'snapshotID'], 'snapshot-job': ['jobId'],
};
export function validateControl(value) {
  if (!(value.command in controlFields) || Object.getPrototypeOf(controlFields[value.command]) !== Array.prototype) fail(400, 'Unsupported control.');
  if (Object.keys(value).some(key => !['command', 'vmID', ...controlFields[value.command]].includes(key))) fail(400, 'Unexpected control field.');
  if (!/^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$/.test(text(value.vmID, 'VM identity', 36))) fail(400, 'Invalid VM identity.');
  if (['click', 'scroll', 'key', 'type-text', 'type-secret'].includes(value.command)) text(value.generation, 'display generation', 128);
  if ('force' in value && typeof value.force !== 'boolean') fail(400, 'Force must be a boolean.');
  if (['click', 'scroll'].includes(value.command) && (!Number.isSafeInteger(value.x) || !Number.isSafeInteger(value.y) || value.x < 0 || value.y < 0 || value.x > 8192 || value.y > 8192)) fail(400, 'Invalid pointer coordinates.');
  if (value.command === 'scroll' && (!Number.isSafeInteger(value.deltaY) || Math.abs(value.deltaY) > 10 || !value.deltaY)) fail(400, 'Scroll deltaY must be nonzero, from -10 to 10.');
  if (value.command === 'key') text(value.chord, 'key chord', 64);
  if (['type-text', 'type-secret', 'operation'].includes(value.command) && !/^[a-zA-Z0-9-]{1,128}(?![\s\S])/.test(text(value.requestId, 'operation ID', 128))) fail(400, 'Invalid operation ID.');
  if (value.command === 'type-text') text(value.text, 'public text', 4096);
  if (value.command === 'type-secret' && !/^[a-zA-Z0-9_.-]{1,64}$/.test(text(value.alias, 'secret alias', 64))) fail(400, 'Invalid secret alias.');
  if ('delayMs' in value && (!Number.isSafeInteger(value.delayMs) || value.delayMs < 20 || value.delayMs > 1000)) fail(400, 'Typing delay must be 20–1000 ms.');
  for (const key of ['label', 'newName']) {
    if ((key === 'label' && value.command === 'snapshot-create') || (key === 'newName' && value.command === 'clone')) {
      if (!/^[a-z][a-z0-9_-]{0,47}$/.test(text(value[key], key, 48))) fail(400, 'Use a lowercase name with letters, digits, underscores or hyphens.');
    }
  }
  for (const key of ['snapshotID', 'jobId']) {
    if (key in value || (key === 'snapshotID' && ['snapshot-restore', 'snapshot-delete'].includes(value.command)) || (key === 'jobId' && value.command === 'snapshot-job')) {
      if (!/^[0-9A-F]{8}(?:-[0-9A-F]{4}){3}-[0-9A-F]{12}$/.test(text(value[key], key, 36))) fail(400, 'Invalid checkpoint or job identity.');
    }
  }
}

export async function createServer(config, options = {}) {
  privateDirectory(config.state);
  const origin = new URL(config.origin);
  if (origin.protocol !== 'https:' || origin.origin !== config.origin) throw new Error('Configure a canonical HTTPS origin.');
  if (!Array.isArray(config.proxyPeers) || !config.proxyPeers.length) throw new Error('Configure the trusted Caddy peer address.');
  const authPath = path.join(config.state, 'account.json');
  const setupPath = path.join(config.state, 'setup-code');
  const sessionsPath = path.join(config.state, 'sessions.json');
  let auth = fs.existsSync(authPath) ? JSON.parse(readPrivate(authPath)) : null;
  if (auth && (auth.version !== 1 || typeof auth.username !== 'string' || !/^[a-zA-Z0-9_.@-]{1,64}$/.test(auth.username) || !/^[0-9a-f]{40}$/.test(auth.totpSecret) || !/^[0-9a-f]{64}$/.test(auth.password?.digest) || !/^[0-9a-f]{32}$/.test(auth.password?.salt) || !Number.isSafeInteger(auth.lastCounter))) throw new Error('Invalid web account state.');
  let setupHash;
  if (!auth) {
    if (!fs.existsSync(setupPath)) writePrivate(setupPath, token());
    setupHash = hash(readPrivate(setupPath).trim());
  } else if (fs.existsSync(setupPath)) fs.unlinkSync(setupPath);
  const sessions = new Map(), attempts = new Map(), allocations = new Map(), queues = new Map(), captures = new Map(), power = new Set();
  const call = options.bridge || bridge, now = options.now || Date.now;
  let hashing = 0;
  function saveAuth() { writePrivate(authPath, JSON.stringify(auth) + '\n'); }
  // OTP replay counters change on login; credentials changing revoke every remembered browser.
  function accountFingerprint() { return auth ? hash(JSON.stringify([auth.username, auth.password.salt, auth.password.digest, auth.totpSecret])) : null; }
  function saveSessions() {
    const entries = [...sessions].filter(([, session]) => session.stage === 'ready' && session.rememberMe)
      .map(([tokenHash, session]) => ({tokenHash, user: session.user, csrf: session.csrf, created: session.created, expiresAt: session.expiresAt}));
    writePrivate(sessionsPath, JSON.stringify({version: 1, account: accountFingerprint(), entries}) + '\n');
  }
  if (fs.existsSync(sessionsPath)) {
    if (fs.lstatSync(sessionsPath).size > 128 * 1024) throw new Error('Invalid remembered session state.');
    const stored = JSON.parse(readPrivate(sessionsPath));
    if (stored?.version !== 1 || !Array.isArray(stored.entries) || stored.entries.length > 256 || !(stored.account === null || (typeof stored.account === 'string' && /^[0-9a-f]{64}$/.test(stored.account)))) throw new Error('Invalid remembered session state.');
    if (auth && stored.account === accountFingerprint()) {
      for (const entry of stored.entries) {
        if (!entry || typeof entry.tokenHash !== 'string' || !/^[0-9a-f]{64}$/.test(entry.tokenHash) || sessions.has(entry.tokenHash) || entry.user !== auth.username || typeof entry.csrf !== 'string' || !/^[a-zA-Z0-9_-]{43}$/.test(entry.csrf) || !Number.isSafeInteger(entry.created) || entry.created < 0 || entry.expiresAt !== sixMonthsFrom(entry.created)) throw new Error('Invalid remembered session state.');
        if (entry.expiresAt > now()) sessions.set(entry.tokenHash, {stage: 'ready', user: entry.user, csrf: entry.csrf, created: entry.created, expiresAt: entry.expiresAt, seen: now(), failures: 0, rememberMe: true});
      }
    }
    // Remove expired entries and credentials belonging to a reset account.
    saveSessions();
  }
  function rate(ip) {
    const time = now();
    for (const [key, item] of attempts) if (time - item.start > 600000) attempts.delete(key);
    for (const [key, limit] of [[ip, 8], ['all', 80]]) {
      const item = attempts.get(key) || {start: time, count: 0};
      if (item.count >= limit) fail(429, 'Too many attempts. Try again in ten minutes.');
      item.count++; attempts.set(key, item);
    }
  }
  function expire() {
    let changed = false;
    for (const [key, session] of sessions) {
      if (now() >= session.expiresAt || (!session.rememberMe && now() - session.seen >= idleMs)) {
        sessions.delete(key); changed ||= session.stage === 'ready' && session.rememberMe;
      }
    }
    if (changed) saveSessions();
  }
  function sessionFor(request) { expire(); return sessions.get(hash(cookieValue(request))); }
  function requireCurrentSession(request, expected) {
    if (!expected || sessionFor(request) !== expected) fail(401, 'Session expired or revoked. Sign in again.');
  }
  function removeSession(key) {
    const session = sessions.get(key);
    sessions.delete(key);
    if (session?.stage === 'ready' && session.rememberMe) {
      try { saveSessions(); } catch (error) { sessions.set(key, session); throw error; }
    }
  }
  function allocateAnonymous(ip) {
    const time = now();
    for (const [key, item] of allocations) if (time - item.start >= allocationWindowMs) allocations.delete(key);
    let item = allocations.get(ip);
    if (!item) {
      // Tracking is bounded too. A new independent client must remain able to
      // begin login when many different clients have visited the public page.
      if (allocations.size >= allocationIPs) allocations.delete(allocations.keys().next().value);
      item = {start: time, count: 0};
      allocations.set(ip, item);
    }
    if (item.count >= allocationPerIP) fail(429, 'Too many new sessions. Try again in a minute.');
    item.count++;
  }
  function newSession(response, stage = 'login', user = undefined, enrollment = undefined, rememberMe = false, replacing) {
    expire();
    const anonymous = [...sessions].filter(([key, session]) => key !== replacing && session.stage === 'login');
    const protectedCount = [...sessions].filter(([key, session]) => key !== replacing && session.stage !== 'login').length;
    if (stage === 'login') {
      if (anonymous.length >= anonymousLimit) sessions.delete(anonymous[0][0]);
    } else if (protectedCount >= protectedLimit) fail(503, 'Session capacity reached. Try later.');
    const created = now(), expiresAt = stage === 'ready' ? (rememberMe ? sixMonthsFrom(created) : created + absoluteMs) : created + pendingMs;
    const raw = token(), key = hash(raw), session = {stage, user, enrollment, rememberMe, csrf: token(), created, expiresAt, seen: created, failures: 0};
    const previous = sessions.get(replacing);
    sessions.delete(replacing);
    sessions.set(key, session);
    if ((stage === 'ready' && rememberMe) || (previous?.stage === 'ready' && previous.rememberMe)) {
      try { saveSessions(); } catch (error) {
        sessions.delete(key);
        if (previous) sessions.set(replacing, previous);
        throw error;
      }
    }
    response.setHeader('Set-Cookie', `${COOKIE}=${raw}; Secure; HttpOnly; SameSite=Strict; Path=/; Max-Age=${Math.floor((expiresAt - created) / 1000)}; Expires=${new Date(expiresAt).toUTCString()}`);
    return session;
  }
  function rotate(request, response, stage, user, enrollment, rememberMe) {
    return newSession(response, stage, user, enrollment, rememberMe, hash(cookieValue(request)));
  }
  async function queue(id, work) {
    const previous = queues.get(id) || Promise.resolve();
    const next = previous.catch(() => {}).then(work); queues.set(id, next);
    try { return await next; } finally { if (queues.get(id) === next) queues.delete(id); }
  }
  function verifyCode(secret, code, lastCounter) {
    if (!/^\d{6}$/.test(code)) return null;
    const counter = Math.floor(now() / 30000);
    for (const candidate of [counter, counter - 1, counter + 1]) if (candidate >= 0 && candidate > lastCounter && equal(totp(secret, candidate), code)) return candidate;
    return null;
  }
  function json(response, status, value) { response.writeHead(status, {'Content-Type': 'application/json; charset=utf-8'}); response.end(JSON.stringify(value)); }
  const server = http.createServer(async (request, response) => {
    response.setHeader('Cache-Control', 'no-store');
    response.setHeader('X-Content-Type-Options', 'nosniff');
    response.setHeader('Referrer-Policy', 'no-referrer');
    response.setHeader('X-Frame-Options', 'DENY');
    response.setHeader('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'");
    response.setHeader('Permissions-Policy', 'camera=(), microphone=(), geolocation=()');
    try {
      const peer = request.socket.remoteAddress?.replace(/^::ffff:/, '');
      if (!config.proxyPeers.includes(peer) || request.headers.host !== origin.host || request.headers['x-forwarded-proto'] !== 'https') fail(403, 'Use the configured HTTPS web address.');
      const url = new URL(request.url, config.origin), pathname = url.pathname;
      const documentRequest = request.method === 'GET' && documentPaths.has(pathname);
      // Restored tabs and inbound links can be cross-site navigations. Only the
      // public HTML shell gets this exception; APIs, assets and POSTs never do.
      const documentNavigation = documentRequest && request.headers['sec-fetch-mode'] === 'navigate' && request.headers['sec-fetch-dest'] === 'document';
      if (request.headers['sec-fetch-site'] === 'cross-site' && !documentNavigation) fail(403, 'Cross-site request refused.');
      if (!['GET', 'POST'].includes(request.method)) fail(405, 'Method not allowed.');
      let session = sessionFor(request);
      const clientIP = request.headers['x-real-ip'] || peer;
      if (request.method === 'GET' && pathname === '/api/session') {
        if (!session) { allocateAnonymous(clientIP); session = newSession(response); }
        session.seen = now();
        return json(response, 200, {ok: true, stage: session.stage, csrf: session.csrf, username: session.user, setupRequired: !auth});
      }
      if (documentRequest || request.method === 'GET' && ['/app.js','/app.css'].includes(pathname)) {
        const filename = pathname === '/app.js' ? 'app.js' : pathname === '/app.css' ? 'app.css' : 'index.html';
        response.writeHead(200, {'Content-Type': filename.endsWith('.js') ? 'text/javascript; charset=utf-8' : filename.endsWith('.css') ? 'text/css; charset=utf-8' : 'text/html; charset=utf-8'});
        return response.end(fs.readFileSync(path.join(ROOT, 'Web', filename)));
      }
      if (request.method === 'POST') {
        if (request.headers.origin !== config.origin || !session || !equal(request.headers['x-csrf-token'], session.csrf)) fail(403, 'Request verification failed. Reload the page.');
        const data = await body(request);
        requireCurrentSession(request, session);
        if (pathname === '/auth/logout') {
          removeSession(hash(cookieValue(request)));
          response.setHeader('Set-Cookie', `${COOKIE}=; Secure; HttpOnly; SameSite=Strict; Path=/; Max-Age=0`);
          return json(response, 200, {ok: true});
        }
        if (pathname === '/auth/login' || pathname === '/auth/setup') {
          rate(clientIP);
          if (hashing >= 2) fail(429, 'Another login is being checked. Try again shortly.');
          const user = username(data.username), password = text(data.password, 'password', 256);
          if ('rememberMe' in data && typeof data.rememberMe !== 'boolean') fail(400, 'Remember me must be a boolean.');
          const rememberMe = data.rememberMe ?? true;
          if (pathname === '/auth/setup') {
            if (auth || !equal(hash(text(data.code, 'setup code', 128)), setupHash)) fail(401, 'Invalid setup code.');
            if (password.length < 12) fail(400, 'Use a password of at least 12 characters.');
            hashing++;
            let passwordValue; try { passwordValue = await passwordHash(password); } finally { hashing--; }
            requireCurrentSession(request, session);
            const enrollment = {password: passwordValue, totpSecret: crypto.randomBytes(20).toString('hex')};
            rotate(request, response, 'enroll', user, enrollment, rememberMe);
            return json(response, 200, {ok: true, next: '/enroll'});
          }
          if (!auth) fail(503, 'Complete local account setup first.');
          hashing++;
          let computed; try { computed = await passwordHash(password, auth.password.salt); } finally { hashing--; }
          requireCurrentSession(request, session);
          if (user !== auth.username || !equal(computed.digest, auth.password.digest)) fail(401, 'Invalid username or password.');
          rotate(request, response, 'otp', user, undefined, rememberMe);
          return json(response, 200, {ok: true, next: '/otp'});
        }
        if (pathname === '/auth/otp') {
          if (!['otp', 'enroll'].includes(session.stage)) fail(401, 'Enter a valid password first.');
          rate(clientIP);
          const enrollment = session.stage === 'enroll';
          if (enrollment && auth) fail(409, 'Account setup already completed.');
          const secret = enrollment ? session.enrollment.totpSecret : auth.totpSecret;
          const counter = verifyCode(secret, text(data.code, 'OTP', 6), enrollment ? -1 : auth.lastCounter);
          if (counter === null) { session.failures++; if (session.failures >= 5) sessions.delete(hash(cookieValue(request))); fail(401, 'Invalid or already used code.'); }
          if (enrollment) auth = {version: 1, username: session.user, ...session.enrollment, lastCounter: counter};
          else auth.lastCounter = counter;
          saveAuth();
          if (enrollment) { sessions.clear(); saveSessions(); setupHash = undefined; if (fs.existsSync(setupPath)) fs.unlinkSync(setupPath); }
          rotate(request, response, 'ready', auth.username, undefined, session.rememberMe);
          return json(response, 200, {ok: true, next: '/'});
        }
        if (session.stage !== 'ready') fail(401, 'Complete password and OTP login first.');
        session.seen = now();
        const match = /^\/api\/vms\/([a-z][a-z0-9_-]{0,47})\/control$/.exec(pathname);
        if (!match) fail(404, 'Unknown API endpoint.');
        validateControl(data);
        const command = {...data, name: match[1]};
        const dispatch = () => {
          // Logout/expiry may happen while reading the body or waiting behind
          // another VM command. Only already dispatched work may finish afterward.
          requireCurrentSession(request, session);
          return call(command);
        };
        const isPower = ['start', 'stop', 'restart'].includes(data.command);
        if (isPower && power.has(data.vmID)) fail(409, 'A power operation is already in progress for this VM.');
        if (isPower) power.add(data.vmID);
        let result;
        try {
          if (data.command === 'restart') {
            // Drain earlier input, then allow frames and confirmation clicks while waiting for guest shutdown.
            await queue(data.vmID, async () => {});
            result = await dispatch();
          } else result = ['cancel', 'operation'].includes(data.command) ? await dispatch() : await queue(data.vmID, dispatch);
        } finally { if (isPower) power.delete(data.vmID); }
        captures.delete(data.vmID);
        return json(response, result.ok ? 200 : 409, result);
      }
      if (!session) fail(401, 'Sign in first.');
      if (pathname === '/api/enrollment') {
        if (session.stage !== 'enroll') fail(401, 'Account setup required.');
        const secret = base32(session.enrollment.totpSecret);
        return json(response, 200, {ok: true, secret, uri: `otpauth://totp/Virty:${encodeURIComponent(session.user)}?secret=${secret}&issuer=Virty&algorithm=SHA1&digits=6&period=30`});
      }
      if (session.stage !== 'ready') fail(401, 'Complete password and OTP login first.');
      session.seen = now();
      if (pathname === '/api/vms') {
        const result = await call({command: 'list'}); return json(response, result.ok ? 200 : 503, result);
      }
      const match = /^\/api\/vms\/([a-z][a-z0-9_-]{0,47})\/(status|aliases|frame)$/.exec(pathname);
      if (!match) fail(404, 'Unknown API endpoint.');
      const vmID = url.searchParams.get('vmID');
      if (!/^[0-9A-F-]{36}$/.test(vmID || '')) fail(400, 'Invalid VM identity.');
      const command = {command: match[2] === 'frame' ? 'screenshot' : match[2], name: match[1], vmID};
      if (match[2] !== 'frame') { const result = await call(command); return json(response, result.ok ? 200 : 409, result); }
      let capture = captures.get(vmID);
      if (!capture || now() - capture.created > 500) {
        capture = {created: now(), promise: queue(vmID, () => call(command))}; captures.set(vmID, capture);
      }
      const result = await capture.promise;
      if (!result.ok) { captures.delete(vmID); return json(response, 409, result); }
      response.writeHead(200, {'Content-Type': 'image/png', 'X-Display-Generation': result.generation, 'X-Display-Width': String(result.width), 'X-Display-Height': String(result.height),
        ...(typeof result.cached === 'boolean' ? {'X-Frame-Cached': String(result.cached)} : {}),
        ...(typeof result.updatedAt === 'string' ? {'X-Frame-Updated-At': result.updatedAt} : {})});
      return response.end(Buffer.from(result.png, 'base64'));
    } catch (error) {
      if (!response.headersSent) json(response, error.status || 503, {ok: false, error: error.status ? error.message : 'Web control unavailable. Check the local service.'});
      else response.end();
    }
  });
  server.headersTimeout = 8000; server.requestTimeout = 10000; server.timeout = 120000;
  server.maxConnections = 64; server.maxRequestsPerSocket = 100;
  return server;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.umask(0o077);
  const configFile = process.argv[2] || path.join(ROOT, 'runtime/web/config.json');
  const config = JSON.parse(readPrivate(configFile));
  const server = await createServer(config);
  server.listen(config.port, config.bind, () => console.log('Virty web service ready.'));
  const stop = () => { server.close(); setTimeout(() => process.exit(0), 3000).unref(); };
  process.on('SIGTERM', stop); process.on('SIGINT', stop);
}
