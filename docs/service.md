# Background service

The macOS controller runs as a per-user Aqua LaunchAgent named
`com.virty.manager`. It starts after the host user logs in and runs without a
terminal or agent session. It is configured to start at subsequent GUI logins;
operation before host login and host reboot recovery are not verified. Capture
and input with the host locked after login passed the [console test](console.md).
Apple's [agent execution-context guidance](https://developer.apple.com/library/archive/technotes/tn2083/_index.html)
describes the GUI-session distinction. The installed `launchctl` and
`launchd.plist` manual pages were used for the current configuration.

## Install and control

The service manages [concurrent named VMs](multiple-vms.md), supervising workers
independently from `runtime/registry/vms.json`. Installation adopts an existing
legacy `runtime/vm` as `macos`.

The owner-only control socket accepts up to 32 concurrent connections, including
requests awaiting a handler or response. Incomplete requests have an absolute
five-second read deadline and cannot block other connections or the main actor.
Response writes have a separate five-second deadline; lifecycle handlers keep
their own operation deadlines. Excess connections close immediately. This bounds
resource usage, not availability against arbitrary code running as the host user.

Create or register a guest before installing:

```sh
./bin/virty create mac-dev --os macos --image /absolute/path/restore.ipsw \
  --setup-assistant
./scripts/service.py install
./scripts/service.py status
./bin/virty status
```

Installation creates a mode-0600 plist in
`~/Library/LaunchAgents/com.virty.manager.plist`, then enables and bootstraps
it into the current user's GUI domain. It performs a controlled takeover of an
existing manual manager for this VM. Run as the normal logged-in user.

```sh
./scripts/service.py stop
./scripts/service.py start
./scripts/service.py restart
./scripts/service.py upgrade  # deploy a rebuilt controller on a broker-backed VM
./scripts/service.py uninstall
```

`stop` disables automatic startup and unloads the job. `start` enables it again.
`restart` unloads and reloads it. `uninstall` removes only the LaunchAgent plist;
the VM, enrolled manager, Keychain item, and local evidence remain intact.
`render` writes the configuration to ignored `runtime/registry/launchd.plist` for
inspection without installing it. `install --home` adopts an existing private
VM runtime. `upgrade --vm NAME` changes only the selected worker. Service
start/stop/restart/uninstall apply to all enabled entries.

## Runtime behavior

The foreground Python fleet supervisor is owned by `launchd`. Each VM worker launches the exact
signed executable pinned in `vm.json`, waits for its private control socket, and
starts the guest once. Its child owns Virtualization.framework, the optional
console, and a client of the secret broker. The supervisor never retrieves or
enrolls a password. The broker runs on demand through anonymous pipes.

`KeepAlive` recovers the fleet supervisor. It recovers individual workers after
manager exit, a VM error or three consecutive failed health checks.
Launches are throttled to 15 seconds. There is no credential retry or automatic
replay of a typing operation.

An intentional `./bin/virty --vm NAME stop` leaves that guest stopped while its manager
remains available; use `./bin/virty --vm NAME start` to boot it again. Quitting the manager
causes worker recovery, starting the guest if its autostart policy is enabled. Use `service.py stop` when you
want the complete service to remain down.

The VM starts with its console hidden. Screenshots and guest input work in that
state with the [private console backend](console.md), including while the host
screen is locked after GUI login in the macOS test. Use `./bin/virty show` for
local viewing and `./bin/virty hide` to hide it again. A host-user GUI session
is still required by the service design.

During service shutdown, the supervisor cancels active typing, powers off the
guest using the manager's stop API, and quits the manager. A failed
shutdown eventually terminates only its own child. This is power-off rather
than saved-state suspend; shut down inside the guest first if you need an orderly
guest OS shutdown. Runtime files and logs remain local and ignored:

```text
runtime/registry/service.json
runtime/registry/service.lock
runtime/registry/service.stdout.log
runtime/registry/service.stderr.log
runtime/vms/<directory UUID>/service.json
runtime/vms/<directory UUID>/service.lock
runtime/vms/<directory UUID>/service.stdout.log
runtime/vms/<directory UUID>/service.stderr.log
```

## Signing and persistence

A provisioning profile is not required for this LaunchAgent. The controller's
ad-hoc signature carries its virtualization entitlement; fresh guests store
credentials in the separately pinned broker. The supervisor and CLI can change
independently. Use `service.py upgrade` to deploy a controller build and
`virty-secrets upgrade` to migrate to a changed broker. Keep enrolled broker
builds and the runtime directories at their recorded paths. The installed plist
references this checkout's supervisor.

The intended data protection Keychain access group still requires properly
provisioned signing. The current encrypted broker handoff uses the file-based
login Keychain; see [broker operation](secrets.md). Legacy manager-owned VMs
require an explicit migration or replacement.


## Deployment records

Keep installation-specific hosts, releases, broker owners, inventories, and live
results in private operations records. Source HEAD and the latest build symlink
do not establish which immutable executable a VM uses.
