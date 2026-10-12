#!/usr/bin/env node
// Local stdio only. Stdout belongs exclusively to the MCP SDK.
import { McpServer } from '@modelcontextprotocol/server';
import { StdioServerTransport } from '@modelcontextprotocol/server/stdio';
import * as z from 'zod/v4';
import { spawn } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const name = z.string().regex(/^[a-z][a-z0-9_-]{0,47}$/).describe('Registered VM name from vm_list.');
const vmID = z.string().regex(/^[0-9A-F]{8}(?:-[0-9A-F]{4}){3}-[0-9A-F]{12}$/).describe('Immutable uppercase UUID from vm_list; protects against stale names.');
const target = { name, vmID };
const generation = z.string().min(1).max(128).describe('Display generation from the most recent vm_screenshot; refresh after changing the display.');
const requestId = z.string().regex(/^[a-zA-Z0-9-]{1,128}(?![\s\S])/).describe('Caller-chosen unique typing ID: 1–128 ASCII letters, digits or hyphens. Reuse only for the identical operation; check vm_typing_status after a lost response.');
const typing = { generation, requestId, delayMs: z.number().int().min(20).max(1000).default(80).describe('Delay per character, in milliseconds.') };
const localPath = z.string().min(1).max(4096).refine(value => !value.includes('\0')).describe('Local host file/directory path. No download URLs.');
const force = z.boolean().default(false).describe('True powers off immediately and can lose unsaved guest work. False requests guest shutdown.');

export function bridge(request) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.env.VIRTY_PYTHON || '/usr/bin/python3', [path.join(ROOT, 'scripts/mcp_bridge.py')], {
      cwd: ROOT, stdio: ['pipe', 'pipe', 'ignore']
    });
    const chunks = [];
    let size = 0;
    const timer = setTimeout(() => {
      child.kill('SIGTERM');
      reject(new Error('VM adapter timed out. Refresh vm_list / vm_status before retrying a change; use vm_typing_status for a typing request.'));
    }, 110_000);
    child.on('error', () => { clearTimeout(timer); reject(new Error('Could not launch the local VM adapter.')); });
    child.stdin.on('error', () => {});
    child.stdout.on('data', chunk => {
      size += chunk.length;
      if (size > 24 * 1024 * 1024) {
        child.kill('SIGTERM');
        clearTimeout(timer);
        reject(new Error('VM adapter response exceeded its size limit.'));
      } else chunks.push(chunk);
    });
    child.on('close', code => {
      clearTimeout(timer);
      if (code !== 0) return reject(new Error('VM adapter exited without a result. Refresh state before retrying.'));
      try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8'))); }
      catch { reject(new Error('VM adapter returned an invalid response.')); }
    });
    child.stdin.end(JSON.stringify(request));
  });
}

function result(value) {
  const { png, ...metadata } = value;
  const content = [{ type: 'text', text: JSON.stringify(metadata) }];
  if (metadata.ok && png !== undefined) {
    if (typeof png !== 'string' || png.length > 22 * 1024 * 1024 || !/^[A-Za-z0-9+/]+={0,2}$/.test(png) || !Buffer.from(png, 'base64').subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))) {
      return result({ ok: false, error: 'Manager returned an invalid PNG frame.' });
    }
    content.push({ type: 'image', data: png, mimeType: 'image/png' });
  }
  return { content, structuredContent: metadata, isError: !metadata.ok };
}

export function createServer({ call = bridge } = {}) {
  const server = new McpServer({ name: 'virty', version: '1.3.0' }, {
    instructions: 'Virty controls named macOS and ARM64 Linux VMs on this Mac. Begin with vm_list. Always pass its name and vmID. Get vm_screenshot before sending input and reuse its generation. Screenshots contain only the guest framebuffer and leave host console visibility unchanged by default. The private-console backend supports a locked host after GUI login; retry temporary boot display-mode mismatches. vm_type_text is for public text; use vm_secret_aliases then vm_type_secret for managed credentials. Never put passwords in tool arguments. Creation, installer ejection, snapshot save/restore/recovery and cloning run in durable jobs; poll vm_job_status. Snapshots require a stopped installed VM with installer detached. Restore includes saved managed secrets, leaves the guest stopped, and preserves its UUID. Clone generates new VM/hardware/MAC identities and independently scoped copies of managed credentials; guest accounts and hostname remain copied. Unregister retains disks, snapshots and credentials.'
  });
  const queues = new Map();
  const power = new Set();
  function queued(id, action) {
    const prior = queues.get(id) || Promise.resolve();
    const next = prior.catch(() => {}).then(action);
    queues.set(id, next);
    next.finally(() => { if (queues.get(id) === next) queues.delete(id); }).catch(() => {});
    return next;
  }
  function tool(toolName, description, fields, command, { read = false, destructive = false, idempotent = false, bypass = false, powerCommand = false, handler } = {}) {
    server.registerTool(toolName, {
      description, inputSchema: z.strictObject(fields),
      annotations: { readOnlyHint: read, destructiveHint: destructive, idempotentHint: idempotent, openWorldHint: false }
    }, async args => {
      const perform = () => handler ? handler(args) : call({ command, ...args });
      try {
        let value;
        if (powerCommand) {
          if (power.has(args.vmID)) return result({ ok: false, error: 'Another power command is active for this VM.' });
          power.add(args.vmID);
          try {
            // Drain prior input, then leave screenshots/input available for shutdown confirmation.
            await (queues.get(args.vmID) || Promise.resolve()).catch(() => {});
            value = await perform();
          } finally { power.delete(args.vmID); }
        } else value = args.vmID && !bypass ? await queued(args.vmID, perform) : await perform();
        return result(value);
      } catch (error) {
        return result({ ok: false, error: error.message, ...(args.requestId ? { requestId: args.requestId } : {}) });
      }
    });
  }

  tool('vm_list', 'List named VMs, immutable UUIDs, OS, default and startup policies, and current state. Start here.', {}, 'list', { read: true, idempotent: true });
  tool('vm_status', 'Get guest state, console availability and active input status for one named VM.', target, 'status', { read: true, idempotent: true, bypass: true });
  tool('vm_create', 'Create a named macOS or ARM64 Linux VM using local media. Returns a durable background job immediately; poll vm_job_status. macOS installs its IPSW and automatically creates the local account by default; set setupAssistant true to retain normal interactive setup, including Apple Account sign-in. Linux installer/account setup remains guest-managed. Linux resource limits: CPUs 1–32, RAM 1–128 GiB, blank disk 4–1024 GiB. macOS currently uses 4 CPUs, 6 GiB RAM and 64 GiB disk; omit resource options. Existing raw Linux disks are copied.', {
    name, guestOS: z.enum(['macos', 'linux']), image: localPath.optional(), disk: localPath.optional(),
    cpus: z.number().int().min(1).max(32).optional(), memoryGiB: z.number().int().min(1).max(128).optional(),
    diskGiB: z.number().int().min(4).max(1024).optional(), autostart: z.boolean().default(true),
    setupAssistant: z.boolean().optional().describe('macOS only: retain Setup Assistant instead of automatic local-account provisioning')
  }, 'create');
  tool('vm_job_status', 'Inspect a VM creation, installer, snapshot or cloning job by its uppercase job UUID. Completed creation/cloning includes the new VM identity; failed jobs may leave partial runtime data or a recovery journal. Jobs survive MCP disconnects.', {
    jobId: vmID.describe('Uppercase job UUID returned by a background VM operation.')
  }, 'job-status', { read: true, idempotent: true });
  tool('vm_jobs', 'List recent VM creation/installer/snapshot/cloning jobs, newest first. Use after losing a job-start response; inspect the matching job before retrying. Returns metadata and outcomes, never logs or credentials.', {
    limit: z.number().int().min(1).max(100).default(20)
  }, 'jobs', { read: true, idempotent: true });
  tool('vm_register', 'Register an existing private VM runtime directory under a name. Supply its vm.json UUID. Rejects duplicate names, UUIDs and directories.', {
    ...target, runtime: localPath, autostart: z.boolean().default(true)
  }, 'register');
  tool('vm_rename', 'Rename a registered VM while preserving its UUID, disks and credentials. Refresh vm_list afterward.', { ...target, newName: name }, 'rename');
  tool('vm_set_default', 'Set the default VM used by the CLI. MCP calls still require an explicit name and UUID.', target, 'default', { idempotent: true });
  tool('vm_snapshot_list', 'List this VM’s disk/boot/managed-secret checkpoints and whether interrupted maintenance needs recovery.', target, 'snapshot-list', { read: true, idempotent: true });
  tool('vm_snapshot_create', 'Checkpoint a stopped VM using APFS copies of its disk, boot state and managed secrets. Detach installer media first. Returns a durable job; poll vm_job_status. Leaves the guest stopped.', { ...target, label: name.describe('Unique checkpoint label, e.g. before-upgrade.') }, 'snapshot-create');
  tool('vm_snapshot_restore', 'Roll back a stopped VM’s disk, boot state and managed secrets to a checkpoint. Discards changes since that checkpoint. Returns a durable job; poll vm_job_status, then start the guest explicitly and refresh its screenshot.', { ...target, snapshotID: vmID.describe('Checkpoint UUID from vm_snapshot_list.') }, 'snapshot-restore', { destructive: true });
  tool('vm_snapshot_delete', 'Permanently remove one checkpoint and its saved managed-secret aliases. Does not change the current guest.', { ...target, snapshotID: vmID.describe('Checkpoint UUID from vm_snapshot_list.') }, 'snapshot-delete', { destructive: true });
  tool('vm_snapshot_recover', 'Recover interrupted checkpoint maintenance from its retained journal. An incomplete rollback returns to the state before the attempted restore; a committed restore is retained. Returns a durable job and leaves the guest stopped.', target, 'snapshot-recover', { destructive: true });
  tool('vm_clone', 'Copy a stopped VM into a new named VM, with new VM/hardware/MAC identity and independently scoped copies of its managed secrets. Optionally clone a checkpoint while the source runs. Guest accounts, hostname and guest-level identifiers remain copied. Returns a durable job. Clone autostart is off.', { ...target, newName: name.describe('Unused name for the new clone.'), snapshotID: vmID.describe('Optional checkpoint UUID from vm_snapshot_list; otherwise copy current stopped state.').optional() }, 'clone');
  tool('vm_set_autostart', 'Choose whether this guest automatically boots when its worker starts. Does not change its current power state.', { ...target, enabled: z.boolean() }, 'autostart', { idempotent: true });
  tool('vm_enable', 'Enable the named worker in the fleet. A running fleet picks it up; guest boot follows its autostart policy.', target, 'enable', { idempotent: true });
  tool('vm_disable', 'Disable and stop this worker. Cancels input and powers off its guest, which can lose unsaved work. Retains runtime and credentials.', target, 'disable', { destructive: true, idempotent: true });
  tool('vm_unregister', 'Disable/stop this worker and remove its name from the registry. Can lose unsaved guest work. Retains disks, runtime directory and broker credentials; does not permanently delete them.', target, 'unregister', { destructive: true });
  tool('vm_eject_installer', 'Detach a Linux installer ISO by stopping and restarting only its worker. Returns a durable job; poll vm_job_status. Cancels input and powers off the guest; save work first. Preserves disks, credentials and startup policy.', target, 'eject', { destructive: true });
  tool('vm_start', 'Boot a stopped guest. Requires an enabled, available worker; use vm_enable / vm_fleet_start if offline.', target, 'start', { powerCommand: true });
  tool('vm_stop', 'Request guest shutdown. The guest may ask for confirmation; use screenshots and input. force=true immediately powers off and may lose unsaved work.', { ...target, force }, 'stop', { destructive: true, powerCommand: true });
  tool('vm_restart', 'Request shutdown, wait up to 30 seconds, then boot. Screenshots/input remain available for guest confirmation. Does not automatically force shutdown. Cancel active typing first.', { ...target, force }, 'restart', { destructive: true, powerCommand: true });
  tool('vm_show_console', 'Show/unminimize only this VM console for local viewing on the host. Screenshots do not require a shown console with the private-console backend.', target, 'show', { idempotent: true });
  tool('vm_hide_console', 'Hide this VM console. The private-console backend continues delivering screenshots and guest input.', target, 'hide', { idempotent: true });
  tool('vm_screenshot', 'Return the guest console as a native PNG image block plus dimensions and display generation. Leaves host console visibility unchanged by default; show=true opens it for local viewing. No image file is written. The private-console backend supports a locked host after GUI login. cached/updatedAt identify retained pixels when no changes arrived; retry pending UI or boot display-mode transitions.', {
    ...target, show: z.boolean().default(false)
  }, 'screenshot', { idempotent: true, handler: async ({ show, ...args }) => {
    if (show) {
      const shown = await call({ command: 'show', ...args });
      if (!shown.ok) return shown;
    }
    return call({ command: 'screenshot', ...args });
  } });
  tool('vm_click', 'Click a guest pixel coordinate using a recent screenshot generation. Coordinates refer to the returned image dimensions.', {
    ...target, x: z.number().int().min(0).max(8192), y: z.number().int().min(0).max(8192), generation
  }, 'click');
  tool('vm_scroll', 'Scroll at a guest screenshot pixel coordinate, without clicking. Vertical deltaY is nonzero wheel steps from -10 to 10: positive sends downward wheel input, negative upward. Guest natural-scrolling settings may reverse the visible direction; test a small delta and inspect the next frame. Horizontal scrolling is not supported. Guest apps determine distance per step. Refresh the screenshot afterward; do not automatically retry a lost response.', {
    ...target, x: z.number().int().min(0).max(8192), y: z.number().int().min(0).max(8192), generation,
    deltaY: z.number().int().min(-10).max(10).refine(value => value !== 0).describe("Vertical wheel steps; positive downward input, negative upward. Guest natural scrolling may reverse visible direction.")
  }, 'scroll');
  tool('vm_key', 'Send a guest key or chord: return, tab, home, end, pageup, pagedown, cmd+l or ctrl+cmd+q, using a recent screenshot generation. Navigation keys act on the guest’s focused control.', {
    ...target, chord: z.string().min(1).max(64), generation
  }, 'key');
  tool('vm_type_text', 'Type public text through guest keyboard events. Never supply credentials here. Returns a typing operation; poll vm_typing_status. Caller-chosen requestId prevents duplicate delivery on identical retries.', {
    ...target, text: z.string().min(1).max(4096).describe('Public text only; never a password or secret.'), ...typing
  }, 'type-text', { idempotent: true });
  tool('vm_secret_aliases', 'List opaque managed-secret aliases for this VM. Returns no secret values. Enrollment stays in the local virty-secrets administration tool.', target, 'aliases', { read: true, idempotent: true });
  tool('vm_type_secret', 'Ask the manager to slowly type a managed secret into the focused guest field by opaque alias. Values stay out of MCP. Focus the intended password field first. Uses keyboard events, not either clipboard. Poll vm_typing_status.', {
    ...target, alias: z.string().regex(/^[a-zA-Z0-9_.-]{1,64}$/), ...typing
  }, 'type-secret', { idempotent: true });
  tool('vm_cancel_typing', 'Cancel the active typing operation and release held guest keys. Delivered immediately, even during another console/power call.', target, 'cancel', { idempotent: true, bypass: true });
  tool('vm_typing_status', 'Check a typing request by ID, including after a lost response. Returns delivery status, never a secret value.', { ...target, requestId }, 'operation', { read: true, idempotent: true, bypass: true });
  tool('vm_fleet_status', 'Check whether the host user’s fleet LaunchAgent is loaded/running. Use vm_list for individual worker state.', {}, 'fleet-status', { read: true, idempotent: true });
  tool('vm_fleet_start', 'Start the already installed Aqua fleet LaunchAgent in this host user session. Enables available named workers; guests boot according to autostart policy.', {}, 'fleet-start', { idempotent: true });
  return server;
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  if (process.argv.length > 2) {
    console.error('Virty MCP takes no arguments. Use bin/virty-mcp --help for setup.');
    process.exitCode = 2;
  } else {
    await createServer().connect(new StdioServerTransport());
  }
}
