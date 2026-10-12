#!/bin/sh
# Downloads Apple's restore image and creates one disposable guest. No password input.
set -eu
umask 077
cd "$(dirname "$0")/.."
VIRTY_RUNTIME="${VIRTY_HOME:-$PWD/runtime/vm}"
export VIRTY_HOME="$VIRTY_RUNTIME"
if [ -f "$VIRTY_RUNTIME/vm.json" ]; then
  printf '%s\n' 'VM already exists; refusing to replace it.' >&2
  exit 1
fi
./scripts/build.sh
./bin/virty-secrets init
metadata=$(./scripts/manager.sh latest)
image_url=$(printf '%s' "$metadata" | python3 -c 'import json,sys; print(json.load(sys.stdin)["url"])')
if [ ! -f "$VIRTY_RUNTIME/restore.ipsw" ]; then
  curl --fail --location --continue-at - --retry 3 \
    --output "$VIRTY_RUNTIME/restore.ipsw.partial" "$image_url"
  mv "$VIRTY_RUNTIME/restore.ipsw.partial" "$VIRTY_RUNTIME/restore.ipsw"
fi
# The separately pinned broker owns the login-Keychain item.
./scripts/manager.sh create "$VIRTY_RUNTIME/restore.ipsw" broker
./scripts/manager.sh install "$VIRTY_RUNTIME/restore.ipsw"
printf '%s\n' 'Guest restored. Install the background service with ./scripts/service.py install.'
