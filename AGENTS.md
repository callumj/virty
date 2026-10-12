# Virty contributor and agent guidance

## Repository boundary

This repository owns reusable application code, tests, architecture, setup
instructions and sanitized compatibility/security findings. Keep deployment
inventory, private domains/IPs, host paths, account activity, enrolled owner IDs,
release receipts and operational logs in a separate private operations repository.
Use synthetic identifiers and example domains in public documentation. Keep
repository migration history and release receipts in private operations records.

For deployment work, discover the host-local mapping with:

```sh
git config --local --get virty.deploymentRepository
git config --local --get virty.deploymentDocument
```

Read the indicated repository's guidance and canonical topic before changing
runtime state. Validate both repositories and actual runtime identities; source
HEAD is not proof of deployment. Update and commit each repository's own facts.
If the mapping is unavailable, continue code-only work and obtain the missing
context before deployment. A private checkout must never be required for builds,
tests or normal use. Set these keys with `git config --local` for each checkout
using its host-local operations path and topic; verify the target Git remote.
Do not commit mapping values, import legacy history, or rewrite shared history
as part of documentation cleanup.

Use Git for source and project documentation. Preserve unrelated local changes.
When installed, run `codex-repo-sync start` before tracked changes and `finish`
after the scoped commit; otherwise inspect/fetch upstream and push safely without
rewriting shared history. Do not change established remotes without instruction.

## Architecture and identity

- Prefer Swift and Virtualization.framework. Keep lifecycle/configuration,
  console/input, secret storage, protocol, CLI, web and MCP adapters separate.
- Named VMs have immutable UUIDs, validated against registry and runtime records.
  Directory UUIDs are storage labels, not VM identities. Rename/unregister retain
  disks and credentials. Read [named VMs](docs/multiple-vms.md).
- macOS automatic provisioning requires macOS 27+. `--setup-assistant` opts into
  normal first-run setup on supported images, including older macOS. Unknown
  usernames in interactive setup are not precreated accounts. Linux uses ARM64
  EFI and guest-managed account setup. Read [macOS](docs/macos.md) and
  [Linux](docs/ubuntu.md).
- The Aqua fleet supervises one worker per enabled VM. Workers use pinned
  immutable managers, not the newest build symlink. Service stop/restart and
  upgrades can power off guests; verify scope and save work first. A normal stop
  request is not proof that the guest completed shutdown. Read [service](docs/service.md).

## Console and input invariants

- `_VZVNCServer` is isolated in `Sources/PrivateConsole.m`; capability-check it.
  Keep LoopbackBindings inside the signed app. Listener confinement and reserved
  client-port admission must pass before Apple's RFB code sees any client.
- Keep one authenticated capture connection alive. Last-client reconnect and
  malformed external RFB traffic triggered Apple observer assertions. Protocol/I/O
  failure requires worker restart; do not silently reconnect or expose credentials.
- Preserve verified pixels across partial/empty/idle updates; invalidate coverage
  on resize/start. Never return an initial or incomplete frame as cached pixels.
- Do not use `_takeScreenshotWithCompletionHandler:`; tested hosts returned no
  image and asserted. Symbol presence alone does not establish functionality.
- No host screen/input or clipboard fallback. Native events target only the guest
  view. Scroll uses unposted Quartz line-wheel events; do not assume an AppKit
  window association. Only bounded vertical deltaY is exposed; visible direction
  follows guest natural scrolling. Read [console](docs/console.md).
- Input requires a current generation and exclusive ownership. Broker lookup must
  run off-main with deadline/cancellation; reserve ownership before suspension
  and recheck state/identity/generation before injection. Keep status/cancel available.
- Typing IDs are permanent, stored in the private indexed `operations/` ledger.
  Preserve it and the legacy `operations.json` migration source; snapshots must
  not roll either back. Downgrade to an old-ledger controller is unsupported.
  Never replay uncertain delivery automatically. Read [input delivery](docs/input-delivery.md).

## Secrets and recovery

- Agents invoke secrets only by VM-scoped opaque alias. Enrollment/export/restore
  remain local administration; no value-returning tool schema. Arbitrary host-user
  processes and compromised guests are outside the secret-concealment guarantee.
- Fresh ad-hoc builds use an immutable login-Keychain broker; unavailable items
  fail without authentication UI. Provisioned data-protection access groups need
  valid signing authorization. Preserve enrolled binaries and owner history.
- Broker upgrades use explicit encrypted handoff, verified before publication.
  Never replace an enrolled owner in place. A self-signed certificate alone did
  not preserve cross-build Keychain access. Read [secrets](docs/secrets.md).
  Broker writes preflight the shared 1,000-entry limit;
  existing oversized catalogs must remain enumerable and removable.
- Cold snapshots require stopped guests and detached installers. Preserve
  `maintenance.json`, `snapshot-policy.json`, transaction files and scoped secrets
  until recovery succeeds. New operations require supportsSnapshotPolicyRecovery.
  Clones get new hardware/registry identities but retain guest accounts/hostname.
  Boot-file hashing is streamed with a 256 MiB bound; snapshots are not host backups.
  Read [snapshots](docs/snapshots.md).
- Do not commit runtime state, credentials, VM media/disks, screenshots, logs,
  generated evidence, dependencies or build products. Do not enroll development
  credentials into a guest without separate authorization.

## Adapters and verification

- MCP uses the official SDK and strict Zod schemas; stdout is protocol only.
  Screenshots use native image blocks without duplicated base64 text. Targeted
  tools require name and immutable UUID; input also requires generation. No model
  dependency is needed. Detached jobs can outlive clients; inspect records before
  retrying and never remove a running job's records. Read [MCP](docs/mcp.md).
- Web VM endpoints require password plus OTP, trusted proxy/origin and CSRF
  checks. Keep account/session files private. Broker values do not cross the web
  adapter; manually supplied passwords are transient and use a native secure
  dialog outside a form. Keep installation-specific network/account values out
  of defaults. Read [web](docs/web.md).
- Run `npm ci --ignore-scripts` before MCP checks. Manager changes require
  `scripts/build.sh` and `scripts/check.sh`. Socket/Virtualization checks may need
  the intended host runtime context outside the command sandbox.
- `Tests/live_checks.py` types public text and requires a disposable guest with
  Terminal at an empty prompt. `Tests/broker_live_checks.py` is opt-in and creates
  only disposable Keychain items. Never substitute a personal VM for a destructive
  fixture or restart production merely to validate documentation.
- Record reusable results/limitations here; put exact host/release/live evidence
  in operations. Verify docs links and boundary violations for documentation edits.
