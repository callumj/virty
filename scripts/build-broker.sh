#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
digest=$(cat Broker/* Sources/KeychainCompat.* scripts/build-broker.sh | shasum -a 256 | cut -c 1-16)
build_dir=".build/broker-$digest"
if [ ! -f "$build_dir/.complete" ]; then
  mkdir -p "$build_dir"
  xcrun clang -fobjc-arc -Wall -Wextra -Werror -c Sources/KeychainCompat.m -o "$build_dir/KeychainCompat.o"
  xcrun clang -fobjc-arc -Wall -Wextra -Werror -c Broker/CryptoCompat.m -o "$build_dir/CryptoCompat.o"
  xcrun swiftc -swift-version 5 -warnings-as-errors \
    -module-cache-path /tmp/virty-swift-module-cache -import-objc-header Broker/CryptoCompat.h \
    Broker/*.swift "$build_dir/KeychainCompat.o" "$build_dir/CryptoCompat.o" -o "$build_dir/virty-secret-broker"
  codesign --force --sign - --identifier com.virty.secret-broker "$build_dir/virty-secret-broker"
  codesign --verify --strict "$build_dir/virty-secret-broker"
  touch "$build_dir/.complete"
fi
ln -sfn "broker-$digest/virty-secret-broker" .build/virty-secret-broker
printf '%s\n' "Built immutable secret broker $digest. Existing owners are not switched automatically."
