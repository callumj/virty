# Guest console capture and input

Capture reads the VM framebuffer while its console is hidden. Host-locked
operation after GUI login has been exercised on macOS and Linux guests; host
logout and operation before GUI login remain unverified.

## Keyboard typing

Text and secret typing require an ANSI US guest keyboard layout and support
printable ASCII. Unsupported characters are rejected before delivery. Physical
modifiers produce uppercase and punctuation. Typing defaults to 80 ms per
character; `--delay-ms` accepts 20–1000 ms subject to a 60-second total limit.
Use `type-text` only for public text and `type-secret` for enrolled aliases.

Typing reserves input ownership and blocks screenshots and competing inputs.
Status and cancellation remain available. A disconnected client does not cancel
delivery. Check the recorded operation ID before retrying; see
[delivery and retry retention](input-delivery.md). Input success means events were
sent to the guest view, not that an application accepted them or login succeeded.
Native system-key capture remains disabled.

## Scrolling and navigation

Virty supports positioned vertical wheel input and
Home, End, Page Up and Page Down. The manager, CLI, MCP, and web
console expose the same controls. Scroll requires the immutable VM identity, a
recent display generation, screenshot coordinates and a nonzero integer
`deltaY` from -10 to 10. The manager moves only the guest pointer without
clicking, then calls its own `VZVirtualMachineView.scrollWheel(with:)`. Existing
typing exclusivity and stale-generation checks apply. The web UI coalesces
wheel deltas and clears pending gestures on disconnect, VM change, busy input
or generation change. It also maps navigation keys and supplies four buttons.

The wheel event is constructed with Apple's
[CGEvent scroll initializer](https://developer.apple.com/documentation/coregraphics/cgevent/init%28scrollwheelevent2source%3Aunits%3Awheelcount%3Awheel1%3Awheel2%3Awheel3%3A%29),
line units, a private event source and cleared modifier flags. It is converted
to an `NSEvent` and never posted to the host. Quartz-created `NSEvent`s have no
AppKit window association in the host test; guest pointer positioning uses the
existing VM view coordinate conversion, without host screen coordinates.
Navigation key codes were checked against the installed SDK's CarbonEvents.h.

Positive `deltaY` sends downward wheel input; negative sends upward. Visible
movement follows guest preferences and app behavior. A macOS test guest
interpreted positive as upward, consistent with macOS
[Natural scrolling](https://support.apple.com/guide/mac-help/mh29222/mac);
its preference was not changed or independently inspected. Agents should test
a small delta and compare the next frame before choosing a direction. Distance
per step also differs across apps. Horizontal wheel input did not move a
horizontal Ubuntu fixture despite advertised guest HWHEEL capability. Its cause
remains unresolved, so horizontal input is rejected by the delivered schemas.

## Backend

`Sources/PrivateConsole.m` isolates `_VZVNCServer` and
`_VZVNCAuthenticationSecurityConfiguration`, capability-checking the selectors
used before creating a server. The server attaches to this worker's VM only.
Its random, ephemeral password is generated inside the manager, retained only
for that process, and never enters VM configuration, arguments, environment,
logs, CLI/MCP responses or a public URL. It is independent of the broker-held
guest password. The internal client negotiates authenticated RFB 3.8 and raw
32-bit frames, then encodes complete frames as PNG. There is no guest helper,
Screen Recording permission, host capture fallback or clipboard integration.

The private server's port-only initializer binds a wildcard address by default.
`Sources/LoopbackBindings.c` therefore runs as a separately linked, app-bundled
dylib using dyld interposition. Before starting the server, the manager enables
the restriction and verifies that a wildcard test bind becomes loopback. Explicit
IP binds in that worker are thereafter limited to loopback; non-loopback
addresses are refused. This affects this manager process, not other apps, the
host firewall or the VM's NAT network service. Creation, installation and restore
metadata commands do not enable the restriction.

Before any RFB connection, the actual server socket must match its port, a
loopback address, and Darwin's TCP listening state. A failed check stops the
server and returns an error. The tested server listens on IPv6 `::1`; its client
connects only there. `SO_ACCEPTCONN` is not supported by the tested Darwin
socket implementation; listener validation uses `TCP_CONNECTION_INFO` instead.
Descriptor tracking reclaims closed, unbound and non-socket entries and avoids
duplicate descriptor numbers. Its 64-entry limit applies to simultaneous tracked
binds, not lifetime bind count; capacity exhaustion refuses the new bind before
creating an untracked listener. Kernel address and listening-state checks remain
mandatory after descriptor reuse.
Interposition from the main executable alone did not work, so the restriction
is a dylib sealed inside the signed immutable app bundle. The manager must keep
that bundled library with its executable.

Only the manager's own client sockets are admitted to this listener. The
restriction library reserves each client's IPv6 loopback source port before
connect, rejects unregistered IP peers in the socket accept path, and records
successful admission for the internal client to verify before reading RFB.
The reservation is revoked when that connection closes. Other local processes
cannot reuse the reserved port while the socket is open. This is an internal
console transport, not a general VNC endpoint; authentication is still required
for admitted connections. The separate Unix control socket is unaffected.

This admission gate also avoids a tested Apple failure: after an invalid RFB
authentication, reconnecting caused `_VZVirtualMachineAccessor addAccessorObserver:`
to assert and terminate the worker. Moving private-server callbacks to the VM's
queue alone did not resolve it. A synthetic VM test then confirmed three
non-manager clients were disconnected before any RFB greeting, with the process
remaining alive. Do not remove the admission gate or hand out VNC credentials.

Disconnecting the last authenticated client between captures also triggered the
same Apple assertion during a later reconnect. The worker therefore keeps one
authenticated connection open across screenshots and boot display changes.
Protocol or I/O failure marks that channel unavailable and requires a worker
restart; it does not repeatedly reconnect into the fragile accessor lifecycle.

The client validates protocol version, authentication, configured dimensions,
rectangle bounds, encoding, byte/message limits and full initial pixel coverage.
It retains the verified framebuffer as [RFB specifies](https://www.rfc-editor.org/rfc/rfc6143.html#section-7.5.3):
later partial or empty updates preserve unchanged pixels. A resize clears that
coverage and requires a complete new frame. Discarding unchanged pixels on each
capture caused a timeout against Apple's partial updates even for nonincremental
requests. For an idle page, the server can send no message at all. After 300 ms
without a new message, capture returns the already verified framebuffer with
`cached: true` and its last pixel `updatedAt` timestamp. No initial or resized
frame can use that path before full coverage; EOF, malformed or truncated
messages remain errors. Retry a pending UI transition if its pixels have not
arrived. Persistent/partial/empty/idle and truncated-message fixtures cover
these distinctions. Each
frame request has a ten-second overall I/O deadline. Bell messages are ignored;
bounded incoming clipboard messages are discarded, and no clipboard message
is sent. A temporary boot mode of 1280×720 was observed before the configured
1280×800 login screen. Capture returns an explicit size mismatch during this
transition; retry after the guest finishes changing modes.

Keyboard and pointer events still go directly to `VZVirtualMachineView`. The
existing paced typing, alias lookup, cancellation, request ledger and stale
generation checks remain in place. Native MCP capture defaults to leaving host
console visibility unchanged; `show: true` remains available for local viewing.

This backend uses undocumented Apple APIs and dyld interposition. The actual
host test is macOS 27.0.1 build 26A434 on Apple silicon, SDK 27.0 / Swift 6.4.
An OS update can remove these classes or change their behavior; capability or
listener failures return errors rather than switching to host desktop capture.
A macOS guest and an
[Ubuntu desktop installation](ubuntu.md) validated framebuffer
capture, input and broker login while the host stayed locked and console hidden.
Synthetic configuration/listener tests cover both guest platforms.

The private server usage was checked against
[Tart's implementation](https://github.com/cirruslabs/tart/blob/main/Sources/tart/VNC/FullFledgedVNC.swift)
on 2026-10-10. The dyld section layout follows
[Apple's interposing header](https://github.com/apple-oss-distributions/dyld/blob/main/include/mach-o/dyld-interposing.h).
Those examples do not establish listener confinement; the socket checks and
runtime tests here do.

## Checks

`scripts/build.sh` builds and signs the bundled restriction library and manager
as an immutable app. `scripts/check.sh` adds complete-frame/PNG protocol fixtures,
persistent-session and display-resize fixtures,
and real local IPv4/IPv6 confinement tests, including refusal of a non-loopback
bind and verification that a bound-but-not-listening socket is insufficient.
Run `scripts/check.sh` in the intended host runtime context; sandbox restrictions
can prevent disposable socket fixtures from binding. Keep actual host identities,
release receipts and saved evidence in private deployment records.
