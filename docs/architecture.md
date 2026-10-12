# Architecture and trust boundaries

Virty runs Apple Virtualization.framework on an Apple silicon host. macOS guests
use Apple restore images and hardware identities; Linux guests use ARM64 EFI,
virtio devices, and guest-managed account setup. Both use NAT networking.

## Components

| Component | Responsibility |
| --- | --- |
| Registry and fleet (`scripts/vm_registry.py`, `scripts/service.py`) | Names, immutable UUID validation, startup policy, and independent worker supervision in a host-user Aqua session. |
| Manager (`Sources/`) | Owns one VM, its lifecycle, control socket, guest view, display generation, and input ledger. Each runtime pins an immutable executable. |
| Console (`Sources/PrivateConsole.m`, `Sources/LoopbackBindings.c`) | Capability-checked private capture transport, listener confinement, and client admission. Native input goes directly to the guest view. |
| Secret broker (`Broker/`, `scripts/secret_broker.py`) | Owns VM-scoped login-Keychain items in a separately pinned executable; provides explicit encrypted backup and cross-build handoff. |
| CLI, web, and MCP (`bin/`, `Web/`, `MCP/`) | Adapt lifecycle and input requests to the same workers, preserving identity and generation checks. |
| Checkpoints (`scripts/vm_snapshots.py`) | Coordinate stopped-guest disk/boot/secret copies and journaled recovery without rolling back the typing ledger. |

Names and directory labels are not VM identities. Routing validates registry and
runtime UUIDs. A per-VM ownership lock prevents competing managers, and
administration locks prevent overlapping configuration changes.

## Capture and input

Capture uses Apple's private `_VZVNCServer` through one persistent authenticated
connection confined to loopback. Its implementation and behavior are not stable
public API contracts. Reconnecting after transport failure has triggered Apple
assertions, so a failed transport requires worker restart. There is no host
capture fallback. See [console internals](console.md).

Input is exclusive and tied to the current display generation. Secret access
runs off the main actor with deadline/cancellation, followed by identity and
state revalidation. Accepted typing IDs persist indefinitely; lost responses
must be resolved through operation lookup, not blind replay. See
[delivery and retry retention](input-delivery.md).

## Trust and persistence

The local control socket is owner-only and verifies peer user identity. This
does not isolate mutually untrusted processes running as that same user. Broker
values cross private process pipes into the controller for typing, but are not
returned by agent tools. A guest can observe any input it receives. NAT allows
ordinary network access, including to the host and LAN.

The web adapter adds password/OTP authentication, session checks, trusted proxy
and origin enforcement, and CSRF protection. MCP runs locally over stdio with
strict schemas. See [web security and setup](web.md) and [MCP contracts](mcp.md).

Keep credential owners, operation ledgers, and interrupted-maintenance journals
until their documented recovery succeeds. Checkpoints are local rollback points,
not independent host backups. Detailed procedures live in [secrets](secrets.md)
and [snapshots](snapshots.md).
