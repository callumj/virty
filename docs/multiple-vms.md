# Named and concurrent VMs

Virty tracks named macOS and ARM64 Linux VMs in an owned, private registry.
A single Aqua LaunchAgent supervises a separate worker process for each enabled
VM. Each worker owns its disk, machine identity, control socket, console window,
input queue, display generation, operation ledger and instance lock. A failed
worker restarts independently; a stopped guest stays stopped until explicitly
started or its worker restarts.

The shared secret broker scopes every alias by VM UUID. Names may change without
moving a disk or changing credential ownership. Named control requests include
the expected UUID, which the worker checks before handling even a status request.
The CLI also verifies the runtime record against the registry before routing.
This does not change the documented host-user-process trust boundary.
Administration changes acquire a per-VM lock; conflicting changes to the same
VM fail as busy while different VMs can be administered independently.

## Existing installation

```sh
./scripts/build.sh
./scripts/service.py install
./scripts/service.py upgrade --vm macos
./bin/virty list
./bin/virty --vm macos status
```

Installing the service adopts the old `runtime/vm` as `macos`, preserving its UUID,
disk, pinned manager and broker. It replaces only this repository's existing
`com.virty.manager` LaunchAgent, in the same user's Aqua session. It does not
create a root daemon. `upgrade` then updates only the selected broker-backed
worker, with configuration rollback if startup fails. Legacy credential owners
cannot be upgraded through this path.

The registry is `runtime/registry/vms.json` (0600 in a 0700 directory). New VM
runtimes live under `runtime/vms/<directory UUID>`. The directory UUID is only a
storage label; the VM's authoritative identity is `vm.json`'s `id`. Set
`VIRTY_REGISTRY_HOME` for a separate catalog. Set `VIRTY_BROKER_HOME` only if the
same broker location is also configured for the service. Runtime metadata,
images, disks, logs, screenshots and build products remain ignored by Git.

## Create, register and select

```sh
# Restore another macOS 27+ VM from a local Apple restore image.
./bin/virty create mac-test --os macos --image /absolute/path/restore.ipsw

# Use normal Setup Assistant, including Apple Account sign-in (macOS 26 supported).
./bin/virty create personal-mac --os macos --image /absolute/path/restore.ipsw \
  --setup-assistant

# Create a Linux VM with an ARM64 EFI installer attached and a blank raw disk.
./bin/virty create linux-dev --os linux --image /absolute/path/installer.iso \
  --cpus 2 --memory-gib 2 --disk-gib 16

# Or copy an already-installed ARM64 Linux raw EFI disk into a new runtime.
./bin/virty create linux-import --os linux --disk /absolute/path/disk.raw

# Track an existing Virty runtime without moving or changing it.
./bin/virty register another --home /absolute/path/private-runtime
./bin/virty default macos
./bin/virty --vm linux-dev show
./bin/virty --vm linux-dev screenshot --output runtime/linux.png
./bin/virty --vm linux-dev key --chord return
```

The service discovers new enabled entries automatically. `--no-autostart` on
create/register leaves a worker available with its guest stopped. The default VM
is explicit and persistent; commands without `--vm` target it. `--home` or
`VIRTY_HOME` remains an explicit directory override for compatibility. Unknown
names and changed runtime identities fail instead of falling back to the default.
`list` reports each VM's identity, OS, runtime, startup policy and live state.
Failed or interrupted creation can leave an unregistered private runtime for
inspection; it never overwrites an existing registered VM.

macOS normally creates the local `virty` account automatically. With
`--setup-assistant`, it instead boots Apple's normal first-run setup, and never
applies automatic provisioning on later starts. Choose the local account in the
guest; its username is initially unknown to Virty. Creation still generates a
VM-scoped `vm-login` alias, which becomes the account password only if you send
it into both password fields during setup. You can instead choose your own
password and enroll it locally later. `provisioned` reports Virty's automatic
provisioning, so it stays false in this mode even after you finish setup.

Linux uses a generic platform, persistent EFI variable store, virtio disk/network/
entropy/graphics, USB keyboard and absolute pointer, and NAT. Only native ARM64
EFI guests are supported; this is not x86 emulation. Installer media is mounted
read-only as USB mass storage. Linux account setup is performed inside the guest;
there is no distribution-specific unattended installer. `installed` in its VM
record means the worker has a bootable configuration, not that an installer has
finished; `provisioned` remains false because Virty does not provision Linux
accounts.

For a blank Linux disk, creation generates a broker-owned `vm-login` alias. Type
it into the installer's password and confirmation fields using separate
`type-secret` operations. For imported disks, enroll their existing credential
through the local administration interface in [secrets.md](secrets.md). No guest
password is accepted by `create` or by the agent-facing CLI.

After installation, detach its ISO:

```sh
./bin/virty --vm linux-dev eject
```

Ejection powers off only this guest, stops its worker, removes its media setting
and relaunches according to its enabled/autostart policy. Complete installation
and flush guest writes first. Other VMs continue running.

## Scrolling and navigation keys

Take a screenshot first and reuse its generation:

```sh
virty --vm linux-dev screenshot --output /tmp/linux-dev.png
virty --vm linux-dev scroll --x 700 --y 400 --delta-y 3 --generation GENERATION
virty --vm linux-dev key --chord pagedown --generation GENERATION
```

Coordinates use the screenshot's top-left origin. Scroll deltas are integer
wheel steps from -10 to 10, excluding zero. Positive Y sends downward wheel input;
negative sends upward. Guest natural scrolling may reverse the visible direction
(a macOS guest may need negative to move down; default Ubuntu uses positive).
Horizontal scrolling is not supported. The guest pointer moves without a click, allowing
targeted scrolling of nested panes. Keys `home`, `end`, `pageup` and `pagedown`
act on the guest's focused control. Check the next frame before another action.

## Lifecycle and retention

See [snapshots.md](snapshots.md) for cold checkpoints, disk/secret rollback and
independently identified clones. Unregister retains snapshots and their broker
items as well as the live runtime.

```sh
./bin/virty --vm linux-dev stop          # request guest shutdown
./bin/virty --vm linux-dev start
./bin/virty autostart linux-dev off      # next worker starts with guest stopped
./bin/virty disable linux-dev            # stop worker and guest; persistently disable
./bin/virty enable linux-dev             # make worker available again
./bin/virty rename linux-dev linux-work
./scripts/service.py upgrade --vm linux-work
./bin/virty unregister linux-work        # disable/stop, then forget name; retain data
```

Disable, upgrade, eject and service shutdown cancel active typing before stopping
the guest. Their worker shutdown uses immediate power-off if it is running; save
work first. Unregister retains the entire runtime and its broker items; it never
deletes a disk or credential. A renamed worker's console title adopts the new
name on its next restart, while CLI routing changes immediately.

`service.py start/stop/restart/uninstall` operate on the entire fleet. The existing
job uses `gui/<uid>/com.virty.manager`; per-VM operations use the
named CLI. Fleet logs are in `runtime/registry/service.*.log`; worker logs and
PID metadata stay in each VM runtime. `service.py status` reports the fleet and
inventory. Broker upgrade/export/restore still applies to its entire catalog.

## Limits and checks

Each worker uses its own authenticated, loopback-confined private console
connection for framebuffer capture. Its window can stay hidden; no host input,
shared clipboard or whole-desktop capture is used. Input serialization is per
VM, so typing in one does not lock its
siblings. macOS and Linux resources must fit the host; startup errors (including
any system VM limit) are isolated and reported through the affected worker's
logs. Hidden and locked-host macOS and Ubuntu desktop capture/input passed
through native MCP; see [ubuntu.md](ubuntu.md) for the Linux installation and
broker-login test. Reboot-before-host-login operation remains unverified.

`./scripts/check.sh` exercises catalog identity checks and retention, name/default
changes, concurrent workers, named input routing, independent stop/recovery,
policy changes and per-VM upgrade rollback, along with the existing keyboard,
archive and transport tests. Socket checks require the host execution context;
the command sandbox prohibits Unix socket binding.

With two disposable guests running, both consoles shown and the first guest's
Terminal focused at an empty prompt, run `python3 Tests/multiple_vm_live_checks.py
--vm macos --other linux`. It types/cancels only public test text and checks
UUID/generation refusal, independent capture during typing and scoped ledgers.

See [service.md](service.md) for reusable service administration.
