# VM checkpoints and cloning

Virty saves cold checkpoints of installed macOS and ARM64 Linux VMs. A checkpoint
contains the sparse disk, virtual machine identifier, EFI variables or macOS
hardware model/auxiliary storage, VM configuration, and a matching copy of all
managed secret aliases. Shut down the guest normally and detach installer media
before saving, restoring, or cloning its current state. These operations refuse
a running/busy guest; they do not force it off. No memory/suspend state is saved.

Files use macOS `clonefile` on the same APFS volume. Unchanged blocks are shared;
later writes are independent. There is no full-copy fallback that could inflate
a sparse disk. The VM runtime volume must support file cloning. Checkpoints are
local rollback points, not independent backups against host/disk loss. Disk sizes
and small boot-file SHA-256 hashes are validated; the large disk has no full-content
checksum. Guest shutdown quality determines filesystem consistency.

## CLI

```sh
virty --vm linux-test stop
# Wait for status.state == stopped; confirm guest shutdown if prompted.
virty --vm linux-test status
virty --vm linux-test snapshot create before-upgrade
virty --vm linux-test start

# Later, discard changes since the checkpoint, including managed-secret changes.
virty --vm linux-test stop
virty --vm linux-test status
virty --vm linux-test snapshot restore before-upgrade
virty --vm linux-test start

virty --vm linux-test snapshot list
virty --vm linux-test snapshot delete before-upgrade

# Clone current state while stopped, or a checkpoint while the source runs.
virty --vm linux-test clone linux-copy
virty --vm linux-test clone linux-copy --snapshot before-upgrade
virty --vm linux-copy start
```

Checkpoint labels follow VM-name syntax and are unique per VM. Restore/delete
also accept the immutable uppercase checkpoint UUID. A VM rename preserves its
checkpoints. Restore preserves the VM UUID, current controller executable, startup
policy and input-operation ledger, and starts its new worker with the guest
stopped. Its next screenshot has a new generation. Save important current changes
as another checkpoint before restoring an older one.

A clone gets a new registry UUID, framework machine identifier and locally
administered MAC address. Apple requires concurrent macOS instances to have
[distinct machine identifiers](https://developer.apple.com/documentation/virtualization/vzmacmachineidentifier).
The installed guest contents remain copied: login password, hostname, OS machine
ID, guest SSH keys, browser accounts and application sessions are not regenerated.
The matching secret aliases are copied into the clone's own VM scope. Deleting
or changing one VM's alias does not change its sibling. Clones have autostart off
and do not copy checkpoints, worker logs, sockets or operation ledgers. Customize
guest-level identities inside the guest when the workload needs that.

## MCP and web console

The stdio adapter exposes `vm_snapshot_list`, `vm_snapshot_create`,
`vm_snapshot_restore`, `vm_snapshot_delete`, `vm_snapshot_recover` and `vm_clone`.
Every call needs name plus VM UUID; checkpoint restore/delete need a checkpoint
UUID. Save/restore/current-state cloning require a stopped guest. Checkpoint
deletion does not require stopping it. Save/restore/recovery/cloning return durable jobs; use `vm_job_status` or
`vm_jobs` after reconnecting. Restore/delete/recovery have destructive annotations.
There are no secret-value, enrollment, export or arbitrary host-file endpoints.

The authenticated web console has a **Checkpoints & clones** panel with save,
restore, delete, clone, refresh and interrupted-maintenance recovery. Restore and
delete require an explicit browser confirmation. Job polling is scoped to the
selected source VM UUID. Changing selection does not redirect an in-flight job.
Existing password/OTP, CSRF and six-month remembered-session checks still apply.
If the browser disconnects, inspect the retained job with MCP/CLI before retrying.

## Secret ownership and upgrades

Checkpoint secrets remain in the active broker's login Keychain namespace under
the checkpoint UUID; snapshot JSON records aliases only. Broker `copy-vm` and
`remove-vm` operations return metadata only; plaintext stays inside the signed
broker. Restore replaces the VM's alias set with the saved set, removing aliases
enrolled later. A clone inherits the copied guest password rather than generating
a password that the copied account would not recognize.

Broker migration/export includes checkpoint-scoped items. Preserve the active
broker owner and immutable build along with the VM runtime. A file-only copy of
the snapshots directory cannot restore missing Keychain entries; use the encrypted
broker backup described in [secrets.md](secrets.md) for host-loss recovery.
Broker owner rollback can omit checkpoints created after that owner handoff;
missing checkpoint aliases cause restore to fail before replacing guest files.

Existing workers need a controller that advertises `supportsSnapshots` and the
new broker protocol:

```sh
./scripts/build.sh
virty-secrets upgrade
./scripts/service.py upgrade --vm NAME
```

The broker handoff is encrypted and verified; retained old builds are preserved.
Legacy manager-owned credentials are refused. The agent-facing CLI/MCP/web never
receives secret values, including during copies and rollback.

## Interrupted operations

Per-VM administration and worker locks prevent concurrent disk access. Before
maintenance, the worker is disabled and must be stopped. A private
`maintenance.json` journal prevents worker/start commands from booting incomplete
state. Restore prepares copy-on-write backups of the current files and secrets
before replacing anything. A normal failure restores that pre-attempt state.
If recovery fails or a process is interrupted, the VM remains disabled and its
journal/backups remain local. Recover before starting it:

```sh
virty --vm NAME snapshot recover
```

Recovery is repeatable. An incomplete restore returns to its pre-attempt state;
a committed restore only finishes cleanup. Interrupted creation removes its
unpublished checkpoint, or preserves an already published checkpoint. Policy is
restored after recovery, with the guest stopped. Input ledgers are never rolled
back, so previously delivered typing IDs cannot be replayed automatically.

Policy completion uses a separate private `snapshot-policy.json` journal. It is
durably written before removing `maintenance.json`, after disk and secret cleanup.
The enabled/autostart policy is first suppressed and synced to the registry
directory. A worker may then launch with the guest stopped, while automatic and
explicit starts remain refused until completion. The saved policy and worker
readiness are restored before removing the completion journal. Listing reports
recovery required for either journal; a failed readiness wait retains the saved
policy and keeps autostart suppressed. Recover also accepts the matching pair of
journals left by an interrupted handoff and refuses conflicting identities.

New cold operations require a controller advertising
`supportsSnapshotPolicyRecovery`; upgrade the controller before using them.
Recovery of older `maintenance.json` journals still repairs the disk and secret
state. If that controller lacks the new capability, recovery leaves the VM
disabled with `snapshot-policy.json` retained. Upgrade that named VM, then finish:

```sh
./scripts/build.sh
./scripts/service.py upgrade --vm NAME
virty --vm NAME snapshot recover
```

Checkpoint deletion writes `snapshots/.deleting-<checkpoint UUID>.json` outside
the checkpoint tree before removing its broker scope and files. The independent
tombstone retains the original VM/checkpoint identity and label even if deletion
removes `snapshot.json` or the entire directory. Other checkpoints remain
listable/restorable. Retry deletion by label or UUID until cleanup completes;
UUID retries after a lost successful response are idempotent and do not remove
an unrecorded broker scope. Preserve pending tombstones for recovery.

Detached jobs survive client disconnects, not a host reboot. Reboot-interrupted
records/journals need inspection and recovery after normal host login. Failed or
killed clone jobs may leave an unregistered runtime/Keychain scope, like interrupted
VM creation; `clone.json` identifies its source, name and target UUID. Inspect the
job/inventory and receipt before retrying or removing partial data. Do not delete
job records while their workers run.

## Validation

`scripts/check.sh` covers disk/password rollback, new-alias removal, current
controller/policy/ledger retention, clone write isolation and independent identity,
running/busy/old-worker refusal, missing secret checkpoints, boot-file tampering,
symlink refusal, failed secret restoration, interrupted recovery, job name
reservation, MCP schemas and web job identity. `Tests/broker_live_checks.py` adds
real disposable Keychain copy/replace/remove checks and cross-build migration.

Isolated fixtures cover interrupted deletion, stale identities, policy and journal
write failures, conflicting handoffs, and legacy controller upgrade recovery.
They use temporary files and fake broker/service implementations; live power-loss
recovery requires separate disposable-guest validation. Workers must advertise
`supportsSnapshotPolicyRecovery` before new cold operations are accepted.
