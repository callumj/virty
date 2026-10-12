# ARM64 Linux desktop guests

Virty boots native ARM64 EFI Linux with virtio storage/network/graphics and USB
keyboard/pointer input. Guest account setup is handled by the distribution's
installer. An x86 ISO cannot boot through this ARM64 virtualization path.

## Create and install

Obtain a supported ARM64 desktop installer from the distribution's official
source and verify its published checksum. For Ubuntu, see the
[official release directory](https://cdimage.ubuntu.com/ubuntu/releases/).
Keep installer images and guest disks in ignored local storage.

```sh
virty create linux-dev --os linux --image /absolute/path/installer.iso \
  --cpus 2 --memory-gib 4 --disk-gib 32
virty --vm linux-dev status
virty --vm linux-dev screenshot --output artifacts/linux-installer.png
```

Use screenshots and scoped input to complete the installer. Blank disks receive
a broker-held `vm-login` alias; type it into the installer password and confirmation
fields without retrieving it. Match the guest keyboard layout to ANSI US before
using managed typing. Do not enroll unrelated development or host credentials.

After installation, shut down the guest normally, verify it is stopped, and
remove the installer through the supported operation:

```sh
virty --vm linux-dev eject
virty --vm linux-dev start
```

Ejection restarts only the selected worker; its enabled/autostart policy controls
whether the installed guest starts automatically. Confirm state before issuing
another start. The next boot should use the installed EFI disk. Focus its login
field and send the same VM-scoped alias, followed by a separate Return.

An existing installed raw ARM64 EFI disk can instead be imported with
`virty create linux-import --os linux --disk /absolute/path/disk.raw`.
The imported account remains guest-owned; enroll its credential locally if
managed login is desired. See [named VM operation](multiple-vms.md).

## Validation scope

An Ubuntu ARM64 Desktop installation has been exercised through native MCP:
installer interaction, opaque alias entry into both password fields, installed
disk boot after ISO ejection, graphical login, browser navigation and hidden
framebuffer capture while the host remained locked after GUI login. Vertical
scrolling and Home/End/Page Up/Page Down were also exercised. Horizontal wheel
behavior remains unresolved and is not exposed.

Disposable guest checks exercised cold checkpoint/clone login, independent
identities, a changed disk marker, and restoring a desktop after disabling its
display manager. These findings are evidence for the tested configuration, not
a guarantee across distributions or host OS updates. Keep exact installation
versions, identities, job IDs and retained guest state in private deployment
records. Do not rerun destructive acceptance tests on a personal guest.

See [console behavior](console.md), [checkpoint semantics](snapshots.md), and
[MCP workflow](mcp.md) for protocols and recovery requirements.
