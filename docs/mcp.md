# Local MCP adapter

Virty's stdio MCP server returns guest screenshots as native PNG image content,
alongside small JSON metadata containing dimensions and display generation. It
connects to the same named workers as the CLI and web console. It has no network
listener or separate LaunchAgent; the MCP client starts it on demand. The fleet
and web LaunchAgents continue running independently.

## Installation

Node.js 20+ and the existing Python/macOS host installation are required:

```sh
npm ci --ignore-scripts
./bin/virty-mcp --help
codex mcp add virty -- /absolute/path/to/virty/bin/virty-mcp
```

In the new server's section of `~/.codex/config.toml`, allow time for local worker
administration and guest shutdown:

```toml
[mcp_servers.virty]
command = "/absolute/path/to/virty/bin/virty-mcp"
tool_timeout_sec = 120
```

Keep the checkout at that path. `virty mcp` also starts the adapter; a
`~/.local/bin/virty-mcp` link uses the existing shell PATH installation. Both
entry points run the installed SDK directly, without fetching packages at
startup. New client sessions load this configuration; an already connected
session may need to reconnect. No model service, API key, HTTP authentication,
or Caddy route is involved. The local client and adapter run as the host user.
See [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

## Tools

All VM-targeted calls require both `name` and its immutable uppercase `vmID`.
There is no implicit default routing in MCP. JSON schemas reject extra fields.
Annotations mark read-only calls and operations that can discard guest work;
client approval policies still apply.

| Tools | Behavior |
| --- | --- |
| `vm_list`, `vm_status` | Named inventory, identity, policies and runtime state. |
| `vm_create`, `vm_jobs`, `vm_job_status` | Create from local macOS IPSW, ARM64 Linux ISO, or copied Linux raw EFI disk; discover and poll durable jobs. |
| `vm_register`, `vm_rename`, `vm_set_default` | Register a private existing runtime with its UUID, rename it, or select the CLI default. |
| `vm_set_autostart`, `vm_enable`, `vm_disable`, `vm_unregister` | Set guest boot policy, enable/disable workers, or remove a registry entry. Disable/unregister cancel input and power off the guest. Unregister **retains** runtime, disks and broker items. |
| `vm_start`, `vm_stop`, `vm_restart` | Guest power control. Shutdown is graceful by default; force requires an explicit `force: true`. |
| `vm_eject_installer` | Background job that stops/restarts the selected Linux worker without its installer ISO, preserving disks, credentials and startup policy. |
| `vm_show_console`, `vm_hide_console`, `vm_screenshot` | Console visibility and native PNG image results; screenshot leaves visibility unchanged unless `show: true` is requested. |
| `vm_click`, `vm_scroll`, `vm_key`, `vm_type_text` | Guest pixel clicks, positioned wheel scrolling, keys/chords and paced public typing tied to a recent display generation. |
| `vm_secret_aliases`, `vm_type_secret` | Alias metadata and manager-to-guest secret typing. No secret value enters MCP. |
| `vm_cancel_typing`, `vm_typing_status` | Cancel input or check an operation by caller-chosen request ID, including after a lost response. |
| `vm_fleet_status`, `vm_fleet_start` | Inspect/start the already installed host-user fleet LaunchAgent. |
| `vm_snapshot_list`, `vm_snapshot_create`, `vm_snapshot_restore`, `vm_snapshot_delete`, `vm_snapshot_recover` | Cold disk/boot/secret checkpoints, rollback and interrupted-maintenance recovery. Mutations require the guest stopped; save/restore/recover return durable jobs. See [snapshots.md](snapshots.md). |
| `vm_clone` | Clone stopped current state or a checkpoint into a new named VM with new hardware identity and independently scoped secret aliases; returns a durable job. |

Creation currently uses macOS's fixed 4 CPUs / 6 GiB RAM / 64 GiB disk. Omit
resource options for macOS. Linux accepts CPU/RAM/blank-disk options; actual host
and framework limits still apply. Local media paths are required; MCP does not
download installation images. Linux account setup remains guest-managed.
For macOS, `vm_create` accepts `setupAssistant: true` to retain Apple's normal
first-run setup and Apple Account sign-in instead of automatically creating the
local account.
The VM's `accountSetup` status distinguishes this mode from automatic setup;
`provisioned` remains false. A generated `vm-login` alias is only the guest's
password if you use it in both local-account password fields during setup.

Permanent live-VM disk/credential deletion, guest shell execution, arbitrary host
commands, clipboard access and dragging are not exposed. Secret
enrollment, backup, restore and broker migration remain local `virty-secrets`
administration operations. This is the same host-user trust boundary described
in [secret storage](secrets.md); it does not protect secrets from a compromised
guest or arbitrary host-process access.

## Agent workflow

1. Call `vm_list`, then use that VM's `name` and `vmID` on each targeted call.
2. Call `vm_screenshot`. Inspect the returned image and retain its `generation`.
   Image bytes occur only in an MCP image block, not in text/structured JSON.
3. Send clicks/scrolls/keys using that generation. Refresh the screenshot after actions
   and whenever a display-generation error occurs.
4. For credentials, focus the intended guest password field, list aliases, then
   call `vm_type_secret` with the alias and a unique `requestId`. Never place a
   credential in `vm_type_text`, arguments, prompts or files.
5. Poll `vm_typing_status` until completion. If delivery loses its response,
   check that same ID before retrying the identical request. Never choose a new
   ID just to retry an uncertain delivery. Cancel remains available immediately.

Screenshot/input requests are sequenced per VM within a connection. Power
commands refuse overlap, and named administration locks also exclude competing
MCP connections. Restart drains queued input then leaves capture/input available
for guest shutdown confirmation. Typing exclusivity and request deduplication
remain enforced by the manager for all clients.

`vm_scroll` takes `x`/`y` in screenshot pixels and integer `deltaY`
wheel steps from -10 to 10, excluding zero. Positive Y sends downward wheel input;
negative sends upward. Guest natural-scrolling settings may reverse the visible
direction: a macOS test needed negative values to move down, while default
Ubuntu used positive values. Horizontal scrolling is not supported. The manager moves the guest
pointer to that coordinate without clicking, then sends a wheel event directly
to its VM view. Guest apps determine the distance per step. No host event is
posted. Refresh the frame afterward; a lost response must not be automatically
retried, since wheel movement is not idempotent.

`vm_key` supports `home`, `end`, `pageup` and `pagedown`, including modifier chords
such as `shift+end`. Their behavior depends on the focused guest control.

Capture uses an authenticated private VM console connection confined to IPv6
loopback inside the worker. It works with the console hidden and the host screen
locked after GUI login in the macOS validation described in [console.md](console.md).
No host display is captured. `show: true` is optional local viewing. A temporary
guest boot display mode can differ from the configured resolution; retry after
that transition. `cached: true` means no changed pixels arrived during the bounded
wait; `updatedAt` identifies the last received pixel update. Retry a pending UI
transition. The [Ubuntu desktop test](ubuntu.md) also exercised Linux
framebuffer capture, installer input and installed-disk broker login with the
host locked and console hidden. Operation before host GUI login remains unverified.

## Durable jobs

Creation and installer ejection return a job UUID immediately. A detached Python
worker continues after the MCP adapter/client exits. `vm_job_status` reports
`queued`, `running`, `completed` or `failed`; `vm_jobs` lets a new connection find
the job after losing its initial response. Completed creation includes the new
VM UUID and runtime path. Use `vm_list` to inspect worker readiness afterward.

Owned 0700 `runtime/mcp/jobs` holds 0600 JSON records and private creation logs.
Records contain public configuration/outcomes; logs and requests are never
returned by MCP. Creation builds are serialized through this job catalog because
they share the build output. Per-VM administration locks protect installer
changes. Direct CLI creation is outside the MCP creation queue.

There is no automatic job cancellation or retry. A killed worker is reported
failed once its disappearance is observed. Failed/interrupted creation can leave
an unregistered runtime; inspect the private log and inventory before retrying
or cleaning up. Ejection uses the existing service rollback path. Jobs survive
client disconnects, not host reboots; retained records make interrupted work
visible. Completed records/logs stay local until explicitly cleaned up.

## Implementation and checks

`MCP/server.mjs` uses the official TypeScript SDK's `McpServer` and stdio
transport. Dependencies and their lockfile are committed; `node_modules` is
ignored. The SDK version is 2.3.1. Tests negotiate its advertised
2025-11-25, 2025-06-18 and 2024-11-05 protocol versions, rather than assuming the
latest specification is supported by a client. See the
[SDK repository](https://github.com/modelcontextprotocol/typescript-sdk) and
[MCP tools specification](https://modelcontextprotocol.io/specification/2026-07-28/server/tools).

The bounded Python bridge reuses the existing control protocol and alias-only
web backend, adds registry administration, and rechecks UUIDs inside the
administration lock. Stdout is exclusively SDK protocol; subprocess progress
cannot corrupt it.

Tests cover SDK negotiation, schema validation, direct PNG content without
base64 text duplication, frame/UUID refusal, retry IDs, concurrent power/input,
registry retention, durable-job failures, and real stdio → Python → disposable
Unix worker delivery. A detached installer job is tested after its initiating
request process exits, using only synthetic disabled VM metadata and a public
test disk. No validation guest is created by these tests.


Reconnect existing clients after adapter changes; each process retains the code
it loaded. Keep host-specific MCP registrations in your operations repository.
