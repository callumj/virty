# Virty

Virty manages named macOS and ARM64 Linux VMs on Apple silicon. Use its CLI,
[local MCP adapter](docs/mcp.md), or [authenticated web console](docs/web.md)
to control guest lifecycle, capture screenshots, send keyboard and mouse input,
and type secrets by alias from a host Keychain broker. It also supports
[cold checkpoints, rollback, and cloning](docs/snapshots.md).

## Requirements

- An Apple silicon Mac with a logged-in GUI user session.
- Apple developer tools providing Swift, Clang, and a macOS SDK; Python 3.
  The tested build environment is macOS 27.0.1, SDK 27.0, and Swift 6.4.
  Older host/toolchain combinations are not established as supported.
- Node.js 20+ for the web/MCP adapters and their checks.
- A compatible macOS restore IPSW or ARM64 Linux EFI installer/raw disk,
  plus space for installation media, guest disks, and restore workspace.

## Quickstart

From the repository root, build and check the application:

```sh
npm ci --ignore-scripts
./scripts/build.sh
./scripts/check.sh
```

Create a macOS guest from a local Apple restore image, then install the user
service and open its console:

```sh
./bin/virty create mac-dev --os macos --image /absolute/path/restore.ipsw \
  --setup-assistant
./scripts/service.py install
./bin/virty list
./bin/virty --vm mac-dev status
./bin/virty --vm mac-dev show
```

Complete Setup Assistant in the guest. Omit `--setup-assistant` for automatic
local-account provisioning, which requires macOS 27+. Interactive setup also
supports compatible macOS 26 images. The generated `vm-login` alias becomes
the guest password only if you use it in both password fields during setup.
See [macOS setup](docs/macos.md) or [Linux installation](docs/ubuntu.md).

The service starts enabled guests after host login and keeps their consoles
hidden until requested. Use `./bin/virty --help` for commands and
[installation](docs/installation.md) for PATH setup and host diagnostics.

## Important limitations

- Screenshot capture relies on private Apple APIs and may break with OS updates.
  Input targets the guest view; there is no host screen, input, or clipboard fallback.
- Guest text typing requires an ANSI US layout and printable ASCII. Observe the
  guest before retrying input; [uncertain typing is never automatically replayed](docs/input-delivery.md).
- Keep enrolled immutable `.build` binaries: broker credentials can be bound to
  their creating build. Use the documented [broker upgrade procedure](docs/secrets.md).
- A host GUI login is required after reboot. Service stop/restart can power off
  guests; save work and shut down in the guest first when orderly shutdown matters.
- Secrets are concealed from agent tool responses, not from arbitrary processes
  running as the host user or from a guest receiving the input. NAT does not isolate
  guests from the host or LAN. Checkpoints are local rollback points, not host backups.

[Browse the documentation](docs/README.md) for operation, architecture, and
contributor checks. This repository contains reusable code and documentation;
installation-specific inventories and runbooks belong in private operations records.

## License

Virty is licensed under [Zero-Clause BSD (0BSD)](LICENSE). Third-party
dependencies retain their own licenses.
