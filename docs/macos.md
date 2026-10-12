# macOS guests

Build using the [quickstart](../README.md), then choose a compatible Apple
restore IPSW. Allow substantial free space for the image (potentially tens of
GiB), restore workspace, and sparse guest disk. The default guest has 4 CPUs
(or the image minimum), 6 GiB RAM (or the image minimum), a 64 GiB logical disk,
a 1280×800 display, USB keyboard/absolute pointer, and NAT networking.
There are no shared host folders or clipboard integration.

## Normal Setup Assistant

```sh
./bin/virty create mac-dev --os macos --image /absolute/path/restore.ipsw \
  --setup-assistant
./scripts/service.py install
./bin/virty --vm mac-dev show
```

This boots Apple's normal first-run setup on supported images, including macOS
26. Choose the local account in the guest; its username is initially unknown to
Virty. The generated VM-scoped `vm-login` alias becomes the password only if you
type it into both password fields during setup. Alternatively, choose a password
and [enroll it locally](secrets.md#associate-other-secrets-with-a-vm) later.
`provisioned` remains false because Virty did not automatically create the account.

## Automatic account provisioning

Omit `--setup-assistant` to create the local `virty` account automatically:

```sh
./bin/virty create mac-dev --os macos --image /absolute/path/restore.ipsw
```

This requires macOS 27+ and uses `VZMacGuestProvisioningOptions`. The broker
generates and stores the password under `vm-login`; no password is passed through
arguments or configuration files. Automatic login and Remote Login are disabled.

For the older single-runtime convenience workflow, `./scripts/create-vm.sh`
downloads Apple's latest supported image (or reuses `restore.ipsw`) and refuses
an existing VM. It uses automatic provisioning; service installation adopts that
runtime as `macos`. Prefer named creation for additional guests.

## First login and control

The service starts enabled guests with their consoles hidden. `start` is needed
only after a guest has deliberately been stopped. Capture the guest, focus its
password field, and type the enrolled alias:

```sh
./bin/virty --vm mac-dev screenshot --output runtime/login.png
# Inspect the screenshot and click the intended password field first.
./bin/virty --vm mac-dev type-secret --alias vm-login
./bin/virty --vm mac-dev key --chord return
./bin/virty --vm mac-dev screenshot --output runtime/desktop.png
```

Secret typing uses the focused field; Virty cannot prove it is a password field.
Enter is separate. A successful input response confirms event delivery, not
successful login. Refresh screenshots between UI transitions and supply
`--generation` from the observed frame to reject stale input.

See [console and keyboard behavior](console.md), [typing retries](input-delivery.md),
and [named lifecycle commands](multiple-vms.md). Keep enrolled immutable builds;
[secret storage](secrets.md#signing-and-legacy-owners) documents signing and migration.
For foreground development, stop the fleet before `./scripts/manager.sh serve`;
service stop can power off guests, so save work first.

## Compatibility

Tests on an Apple silicon macOS 27.0.1 host exercised hidden capture and guest
input on macOS 26 and 27 after host GUI login. Locked-host operation after login
worked; operation before GUI login and recovery across logout/reboot remain
unverified. The private console backend can change with OS updates.

Apple-service activation/APNs failures were observed on a macOS 27 guest but not
a fresh macOS 26 guest. Different identities and sampling periods prevent a
controlled comparison. Apple Account activation and cloud synchronization are
not guaranteed Virtualization.framework capabilities. Use disposable guests for
[contributor live checks](../CONTRIBUTING.md).
