# Contributing

Start with the [architecture](docs/architecture.md) and [agent guidance](AGENTS.md).
Keep changes focused and preserve existing local work. Build and test from the
repository root in the supported macOS host environment:

```sh
npm ci --ignore-scripts
./scripts/build.sh
./scripts/check.sh
```

The check suite covers native components, Python lifecycle/transport/recovery,
web authentication, and MCP contracts. Socket and Virtualization.framework
checks may require the intended host context outside a command sandbox.
For documentation-only changes, check local links, examples against the CLI,
repository boundaries, and `git diff --check`; no service restart is needed.

Live checks are opt-in. `Tests/live_checks.py` types public text and requires a
disposable guest with Terminal at an empty prompt. `Tests/broker_live_checks.py`
creates disposable Keychain items in an unlocked login session. Never substitute
a personal VM or existing credentials for test fixtures. The signing diagnostic
is described in [secret storage](docs/secrets.md#signing-and-legacy-owners).

Document user-visible behavior and recovery limits with the relevant change.
Use synthetic identities and example domains. Do not commit credentials, runtime
state, guest media, screenshots, logs, build products, or installation-specific
inventories. Private deployment records are separate from product documentation.
