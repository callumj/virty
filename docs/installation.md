# Installation and host diagnostics

See the [quickstart](../README.md) for requirements, build commands, and first
VM creation. Run commands from the repository root as the logged-in host user.
No model API or API key is needed. Web requires Node.js 20+ without npm packages;
MCP and its checks need the dependencies installed by `npm ci --ignore-scripts`.

## Commands on PATH

Optionally link the entry points into a user-local command directory:

```sh
mkdir -p "$HOME/.local/bin"
ln -s "$PWD/bin/virty" "$HOME/.local/bin/virty"
ln -s "$PWD/bin/virty-secrets" "$HOME/.local/bin/virty-secrets"
ln -s "$PWD/bin/virty-web" "$HOME/.local/bin/virty-web"
ln -s "$PWD/bin/virty-mcp" "$HOME/.local/bin/virty-mcp"
```

The links follow this checkout; keep it at the same path. Inspect an existing
command before replacing it. `virty-secrets` is local administration; agents
request typing through `virty` using opaque aliases.

For fish, run `fish_add_path "$HOME/.local/bin"`. For zsh, put the following in
`~/.zshenv`; for bash, use `~/.bashrc` and your login startup file
(`~/.bash_profile`, or `~/.profile` if no bash profile exists):

```sh
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) export PATH="$HOME/.local/bin:$PATH" ;;
esac
```

Open a new shell and check `command -v virty` and `virty --help`.

## Host capability probe

```sh
swiftc -module-cache-path /tmp/virty-swift-module-cache \
  scripts/check-host.swift -o /tmp/virty-host-probe
/tmp/virty-host-probe
```

This read-only probe reports framework and private runtime symbols. It does not
start a VM, open a listener, access credentials, or inject input. Symbol presence
is not proof of working capture/input. It exits nonzero when virtualization is
unavailable or the host is not ARM64. A command sandbox can hide virtualization
support; use the manager's intended host runtime context.

## Build and runtime locations

`.build/Virty.app` points to the latest immutable manager build; each VM pins
its installed executable. Building does not upgrade a running worker. Keep
credential-owning broker and legacy manager builds intact; follow
[service upgrades](service.md) and [broker upgrades](secrets.md).

New runtimes live in `runtime/vms/<directory UUID>`. Service installation can
adopt an existing `runtime/vm` as `macos`; see [existing installations](multiple-vms.md#existing-installation).
Runtime state, media, screenshots, logs, and build products remain local and
ignored by Git. Installing or restarting the service affects its managed fleet;
read [service operation](service.md) before changing an existing installation.
