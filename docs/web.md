# Web console

Virty provides an HTTPS-proxied web console with password and OTP authentication.
A separate per-user Aqua LaunchAgent, `com.virty.web`, serves it; stopping that
job does not stop VM workers. Configure your own origin, bind address and trusted
proxy before enrolling an account.

## First account setup

Run this in your own interactive terminal on the Mac:

```sh
virty-web setup
```

It opens the setup page and displays a one-time local setup code. Enter that
code, choose a username and password of at least 12 characters, then add the
shown key to your authenticator app as a time-based, six-digit account. The
next page verifies a code before saving the account. The bootstrap code is
removed after successful enrollment. Setup codes are refused in noninteractive
agent calls; no default password or preconfigured authenticator exists.

Subsequent login requires username/password, followed by OTP on a separate
page. VM metadata, frames, aliases, and controls stay unavailable until both
steps succeed. Authenticator codes use [RFC 6238](https://www.rfc-editor.org/rfc/rfc6238)
with SHA-1, 30-second steps, six digits, and a one-step clock allowance. A used
counter cannot be accepted again, including after a web-service restart.

The account is local to Virty. Passwords use salted scrypt (N=65536, r=8, p=1).
The authenticator seed and password hash persist in the host user's owned
0700 `runtime/web` directory, in a 0600 `account.json`. These web credentials
are separate from the guest Keychain broker and must stay out of Git and Agents.
**Remember me for six months** is checked by default on login and setup. After
password and OTP succeed, the browser receives a persistent cookie valid for
six calendar months, including across browser and web-service restarts. There
is no inactivity timeout for remembered sessions. Month-end expiry clamps to
the last day of the destination month; use of the console does not extend it.
Clearing browser cookies or signing out ends that browser's remembered login.

Remembered sessions persist atomically in owned 0600 `runtime/web/sessions.json`.
Only hashes of the random login tokens are stored, alongside the username,
CSRF token and creation/expiry times. No password, OTP seed, raw login token,
or guest secret is copied into this file. Sessions bind to the account's
credentials; changing the password hash, username or authenticator seed revokes
all remembered browsers after restart. Updating the OTP replay counter does
not revoke other browsers. Signing out durably revokes the current session.
If session storage cannot be updated, login/logout report failure.
Controls waiting for their request body or a per-VM queue revalidate the session
before dispatch. Logout, token rotation or expiry rejects that pending work;
an operation already dispatched to the VM may still finish. Logout cannot undo
guest input or a power operation already issued. Password hashing also rechecks
the session before completing an authentication transition.

Uncheck the option for the earlier short session: 15 minutes without requests
or eight hours total, held only in memory and cleared on service restart.
Password/OTP/setup sessions are always memory-only and expire after five
minutes. Tokens rotate at each successful authentication step; only a fully
authenticated remembered session is persisted.
Cookies are Secure, HttpOnly, SameSite=Strict, host-only, and use the `__Host-`
prefix. POSTs require the configured Origin plus a per-session CSRF token.
Login/setup/OTP attempts are bounded per trusted proxy client address and
globally; OTP also limits failures per pending session. See the [OWASP session](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)
and [CSRF](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)
guidance used for those boundaries.

## VM controls

The **Checkpoints & clones** panel saves stopped-VM disk/boot/secret checkpoints,
restores or deletes a selected checkpoint, and creates a new named clone.
Current-state copies/restores require a stopped guest; cloning a saved checkpoint
can run while the source guest runs. Restore/delete display browser confirmations.
Long operations return durable jobs, polled with the selected source VM UUID.
Interrupted maintenance exposes a recovery action. See [snapshots.md](snapshots.md)
for retention, copied guest identities, Keychain ownership and recovery limits.

Select a named VM from the list and press **Connect**. This reads the worker's
framebuffer and refreshes screenshots about once per second, including while
the host screen is locked after login. Connecting or disconnecting does not
change the host console window's visibility. Click the
screen to position the guest pointer and focus keyboard control. Ordinary
characters, navigation keys, and supported modifier chords are sent through
the existing manager. Home, End, Page Up and Page Down are also available as
buttons. Scroll a mouse wheel or trackpad vertically over the screen to scroll the guest
pane beneath the pointer without clicking. Pixel/line/page browser wheel deltas
are converted into bounded wheel steps and coalesced; disconnected or unavailable
screens cannot send them. The guest's natural-scrolling preference can reverse
the visible direction; no guest preference is changed. The keyboard assumes ANSI US. Dragging, audio, and video
streaming are not implemented.

Managed secrets are listed as aliases for the selected VM UUID. Focus the
password field before pressing **Send**. The manager retrieves and types the
value; neither the web backend nor browser receives it. Delivery has an opaque
operation ID; **Check delivery** inspects it and **Cancel typing** interrupts
it. A timeout or uncertain delivery must be inspected before another attempt.
The public-text field is for nonsecret text. For a password supplied directly
in the browser, focus the guest's password field and choose **Type password…**
to open the temporary native masked entry dialog. Enter the value, then choose
**Type password** or press Enter. Cancel or Escape discards it. The browser
clears and removes this entry as soon
as it is sent, including failed or uncertain delivery, and on disconnect,
VM changes, sign-out and page exit. It types through the existing HTTPS and
manager keyboard path without pressing Enter, using either clipboard, or
enrolling a managed alias. The supplied value is held transiently by the
browser/backend/manager; it is not saved in web state or the manager's delivery
ledger, which retains only operation metadata. Guest input recording remains
possible within the previously documented trust boundary.

Guest password entry uses that explicit dialog. The resting console contains no
password input, including hidden inputs. The dialog creates a native password
input only while open, marks it with `autocomplete="off"` and
[`data-1p-ignore`](https://www.1password.dev/web/compatible-website-design#ignore-offers-to-save-or-fill-specific-fields),
and removes it on send, cancel, disconnect, VM change, sign-out or page exit.
The guest entry is not wrapped in a form and uses button/Enter handlers rather
than form submission; Safari otherwise offered to save the guest value as a
Virty login. The actual Virty login/setup forms keep their normal autofill hints.
Safari can still show its native AutoFill icon inside the explicitly opened
secure dialog; no password control is present during ordinary console use.

Keep native secure input: CSS masking can expose plaintext accessibility values.
Actual third-party password-manager behavior needs separate validation.

Start, stop, and restart target only the selected VM. Stop asks the guest for
shutdown. Restart waits up to 30 seconds for shutdown before starting it again;
it reports failure if the guest has not stopped. Keep the console connected to
confirm shutdown inside the guest when prompted. Screenshots and input remain
available during this wait; concurrent power requests for that VM are refused.
The confirmation dialog has
an explicit force-power-off option for guests that cannot shut down normally;
that option can lose unsaved work. No automatic force fallback exists.

The backend validates both the named registry entry and immutable VM UUID on
every command. Input also includes the display generation from the last frame.
The adapter has no enrollment, export, secret-read, arbitrary command, or host
path endpoint. Alias listing uses only the broker's metadata-only list operation.
Capture uses the manager's authenticated, loopback-confined private console
connection, including with the host locked after GUI login and the VM hidden
in the tested configuration; see [console.md](console.md). The UI reports
unavailable frames, retries automatically and disables input until a valid
frame arrives. Reboot-before-login remains unverified.

The frame endpoint preserves display generation and dimensions in response
headers. `X-Frame-Cached` and `X-Frame-Updated-At` carry the worker's retained-pixel
flag and last pixel-update timestamp when supplied. An unchanged guest page can
return retained pixels normally; protocol or incomplete-frame errors still fail.

## Service administration

Node.js 20 or newer is required; no npm packages are needed. For a same-host
TLS proxy, install with explicit site configuration (replace the example origin):

```sh
./bin/virty-web install --origin https://console.example.com \
  --bind 127.0.0.1 --proxy 127.0.0.1
virty-web status
virty-web restart
virty-web stop
virty-web start
```

New installations require `--origin` and `--proxy`; `--bind` defaults to loopback.
Reinstallation retains an existing configuration rather than overwriting it with
defaults or command-line values. Change that private configuration deliberately
when moving a service to another address or proxy.

Installation writes the owned web config and
`~/Library/LaunchAgents/com.virty.web.plist`. The job uses the installed Node
executable, `Web/server.mjs`, and private `runtime/web/config.json`. Config
contains the canonical HTTPS origin, LAN bind address/port, trusted proxy peer,
and state directory. Endpoints reject a different peer, Host, or forwarded
scheme. The reverse proxy owns TLS. If it is on another machine, choose an appropriate
bind address and protect the proxy-to-backend HTTP hop with network policy.
Configure the proxy to replace forwarded headers and avoid logging sensitive
request data. The web service does not log requests, passwords,
OTP codes, public input, aliases, or screenshots; logs contain startup state.

Cross-site requests are refused except for a top-level GET navigation to the
public HTML shell (`/`, `/login`, `/otp`, `/setup`, `/enroll`). The exception
requires both `Sec-Fetch-Mode: navigate` and `Sec-Fetch-Dest: document`; it does
not cover APIs, authentication POSTs, assets, or iframe/embed requests. This
lets an inbound link or restored Safari tab load the page without creating or
rotating a session. SameSite=Strict cookies, password/OTP, exact Origin and CSRF
checks still protect account and VM operations. Fetch Metadata describes these
navigation headers in the [W3C specification](https://www.w3.org/TR/fetch-metadata/#sec-fetch-dest-header).

`Web/server.mjs` serves the same-origin UI, checks authentication, validates
control schemas, and invokes `scripts/web_bridge.py` over anonymous pipes.
The adapter routes directly to named Unix sockets and validates registry identity.
Per-VM command queues and short-lived frame coalescing avoid simultaneous web
capture/input; cancel and operation checks can interrupt or inspect typing.
Restart drains earlier input without holding the queue during its shutdown wait.
VM-manager errors still fail explicitly. Screenshots remain in browser/backend
memory, with no-store response headers.

Web source changes need `virty-web restart` for backend changes; static assets
are served from the checkout and never cached. Manager and broker builds remain
pinned and do not need upgrades for web-only changes. Keep `runtime/web` local
across Git pulls. To revoke every remembered browser without resetting the
account, stop the web job, delete `runtime/web/sessions.json` as the host owner,
then start it. Remove this file the same way if corrupt session state prevents
startup; permissions and malformed records fail closed.
To reset a lost web login, stop this web job, remove its local `account.json`
and `sessions.json` as the host owner, start it, and run setup again. This clears web
access; it does not change any guest credential.

Run `scripts/check.sh` in the intended host context. It includes RFC vectors,
login-stage and OTP-replay tests, CSRF/proxy refusal, first-run enrollment,
remembered-session restart/revocation/expiry tests, protected frames,
metadata-only alias handling, scoped restart, and input-schema checks.
Socket-binding tests require an unsandboxed host context.


Anonymous session allocation has a separate 128-entry reclaimable budget and
is limited to eight allocations per trusted client IP per minute. OTP, enrollment
and authenticated sessions have an independent 256-entry budget and are never
evicted for anonymous admission. Existing valid sessions bypass allocation.
