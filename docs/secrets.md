# Secret broker

New VMs use a separate, locally ad-hoc-signed secret broker. Its immutable
executable owns file-based login-Keychain items; the VM controller obtains a
value through an anonymous subprocess pipe for provisioning or keyboard typing.
The agent-facing CLI/control socket returns only operation status and opaque
aliases. It exposes no enrollment, read, export, or restore operation.

The broker runs on demand in the logged-in host user's session. It has no
listener and needs no additional LaunchAgent. The persistent VM manager remains
`gui/<uid>/com.virty.manager`. Provisioned data-protection signing and unattended host reboot recovery remain
separate work; Linux guests and MCP use the same broker interface.

## Setup and routine updates

```sh
./scripts/build.sh
./bin/virty-secrets init
./scripts/create-vm.sh  # a fresh disposable VM; refuses an existing VM
./scripts/service.py install
./bin/virty-secrets status
```

`create-vm.sh` initializes the broker automatically, generates the VM-scoped
`vm-login` item, and restores the guest. No password is passed to the shell.
The broker owner is recorded in mode-0600 `runtime/secrets/broker.json` inside
a mode-0700 directory. `VIRTY_BROKER_HOME` selects another private directory;
use the same setting for setup, service installation, and administration.

Broker builds are retained as `.build/broker-<digest>/virty-secret-broker`.
`.build/virty-secret-broker` points to the latest build, but the enrolled owner
record pins an absolute immutable executable and its SHA-256 hash. Building new
code does not switch that owner. The manager and administration CLI check the
record/file permissions and hash before invoking it.

After changing only the controller, build and deploy it with:

```sh
./scripts/build.sh
./scripts/service.py upgrade
```

This verifies the new manager's code signature, stops the service/guest, updates
the controller executable in VM metadata, and restarts it. If startup fails,
it restores the previous controller and starts it again. It requires a
broker-owned VM; legacy manager-owned credentials are not silently migrated.
The broker owner and guest password remain independent of controller builds.

After changing broker code:

```sh
./scripts/build.sh
./bin/virty-secrets upgrade
```

The administration CLI generates a temporary 256-bit wrapping passphrase in
memory. The old broker exports an encrypted bundle through an anonymous pipe;
the new broker decrypts it, creates items in a fresh namespace, and verifies
every value by reading it back. Only then is the owner record atomically
switched. No wrapping key, plaintext archive, or secret value is written to a
temporary file or printed. The CLI never offers a plaintext read command.

Old owner records are retained under `runtime/secrets/history`, and their
executables/Keychain items remain intact. A failed/interrupted import leaves
nonsecret target metadata under `runtime/secrets/pending`; the active owner
stays unchanged until publication. A caught item-write/read failure attempts
to remove items created in that target namespace. A killed process or failed
publication can leave orphaned target items; retain its pending record for
inspection rather than treating it as a successful upgrade.

```sh
./bin/virty-secrets rollback --instance PREVIOUS-OWNER-UUID
```

Rollback verifies all items in the retained owner before switching back. It
returns the catalog at that handoff; later enrollments/removals are not copied
back. Preserve both binaries until the current owner and backups are verified.

## Encrypted backup and restore

```sh
./bin/virty-secrets export --output runtime/secrets/backup.virty
./bin/virty-secrets restore --input runtime/secrets/backup.virty
```

The local terminal asks for a hidden passphrase, with confirmation during
export. Export refuses an existing output file and creates the encrypted file
with mode 0600. Restore uses the latest broker build, or an explicit `--binary`,
and publishes a fresh namespace only after verification. Backup recovery does
not require the former executable to exist. Keep the passphrase separately;
the application does not store or recover it.

Automation can supply the passphrase using `--passphrase-fd N` and an anonymous
pipe. Never pass it in command arguments or environment variables. Passphrases
must contain 12–4096 UTF-8 bytes. Archive version 1 uses PBKDF2-HMAC-SHA256
with 600,000 iterations and a fresh 16-byte salt, plus
[CryptoKit AES-256-GCM](https://developer.apple.com/documentation/cryptokit/aes/gcm)
with an authenticated format context. Archives carry UUIDs, aliases, and values
inside the ciphertext. Wrong keys, changed ciphertext, duplicate accounts,
invalid entries, unsupported versions, and oversized archives are rejected.
The restore primitive refuses a nonempty target namespace.

## Associate other secrets with a VM

The separate administration CLI can enroll a value through a hidden local
terminal prompt; it never accepts that value in an argument:

```sh
./bin/virty-secrets enroll --vm VM-UUID --alias example-login
./bin/virty-secrets generate --vm VM-UUID --alias example-generated
./bin/virty-secrets remove --vm VM-UUID --alias example-generated
./bin/virty type-secret --alias example-login
```

Each account is scoped to a canonical VM UUID and an opaque ASCII alias of
1–64 letters/digits, dots, underscores, or hyphens. Enrollment refuses an
existing alias. Values are bounded to 4096 UTF-8 bytes; the current guest input
backend separately accepts only printable ANSI US characters and enforces its
typing-duration limit. Secret typing targets the currently focused guest field.
Reusing a typing request ID for another alias is rejected.

The broker catalog supports at most 1,000 aliases across all VM and checkpoint
scopes. Enrollment and generation check capacity before adding anything;
checkpoint copy checks the entire final catalog before removing target aliases.
Replacement accounts for the target's old scope, so a copy that keeps or reduces
the total remains possible at the limit. A failed copy restores the previous
target even if that rollback returns a legacy namespace to more than 1,000
aliases. Archive import validates the same limit before mutation.

The broker keeps enumeration and removal available for already oversized
catalogs. Remove unused aliases/checkpoints to return within the limit before
adding items or exporting a bounded archive. Immutable enrolled older brokers
retain their original behavior until an explicit encrypted upgrade. If an older
owner already exceeds its enumeration limit, its existing known-alias `remove`
operation remains available: remove only a confirmed unused VM/alias using the
local administration CLI, then retry listing/export/upgrade. Do not replace its
owner executable or bypass retained-owner checks to access its items.

The `Tests/BrokerStoreTests.swift` regression fixture uses an injected
in-memory backend, without creating a Keychain store. It covers 999/1,000/1,001
entries, generation/enrollment, multi-alias copy preflight, target replacement,
write/read/delete failures, rollback into a previously oversized catalog,
oversized enumeration/cleanup and archive import boundaries.

## Signing and legacy owners

Ad-hoc Keychain access can bind to the creating executable's code hash. A tested
self-signed certificate with identical designated requirements did not preserve
unattended login-Keychain access across builds. Keep the enrolled immutable
broker executable; use encrypted handoff rather than replacing it in place.
An Apple-issued identity has not been established here as a substitute for that
handoff. Data-protection access groups require signing authorization through a
provisioning profile; self-declared restricted entitlements are insufficient.

Legacy manager-owned VMs may use `login` or `data-protection` storage. Their
`vm.json` pins the credential-owning manager, which `manager.sh` retains for
installation, serving, and probes. Preserve that build. These owners have no
broker export/handoff API; controller upgrade refuses silent migration. Moving
them requires explicit credential migration or a replacement guest.

The opt-in diagnostic `python3 scripts/check-local-signing.py` creates a
randomly scoped disposable login-Keychain item and temporary signing Keychain.
It requires an unlocked login session and developer tools; it does not read
existing Virty items. Exit 0 means replacement reads succeeded, 1 means the
candidate signing setup was unsupported, and 2 means the diagnostic failed.
It writes only metadata to ignored `.build/local-signing-report.json`. Check its
cleanup result; abrupt termination can interrupt cleanup. This is not an
enrollment or production migration procedure.

## Protection boundary and checks

Keychain authentication UI is disabled in the broker. Locked/unavailable items
produce errors. The current backend is the file-based login Keychain; the
broker does not add restricted data protection access-group entitlements or
widen existing ACLs. After host reboot, a normal host GUI login/unlock remains
the accepted requirement. Broker retrieval while the host screen was locked
after login passed the [live console test](console.md), including after manager
upgrades. Logout and reboot recovery remain untested.

Secrets exist in broker/controller process memory and the private pipes used
for delivery. This protects the agent's prompts, tool responses, ordinary
arguments, logs, and clipboard. It does not isolate an agent with arbitrary
host-user process access, or prevent a guest from recording delivered input.
Encrypted backups and all runtime/build evidence stay ignored by Git.

```sh
./scripts/check.sh
# Real Keychain test; disposable items only, in an unlocked login session:
python3 Tests/broker_live_checks.py
```

Normal checks cover archive authentication/bounds, keyboard behavior, CLI
transport, supervision, and controller-upgrade rollback. The opt-in host test
uses two differently signed broker binaries to demonstrate denial before
migration and successful encrypted handoff. It also checks VM scope,
non-overwrite behavior, CLI export/restore/rollback, missing-owner backup
recovery, tampered/wrong-key refusal, and cleanup of every disposable item.
