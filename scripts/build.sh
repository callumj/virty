#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
./scripts/build-broker.sh
digest=$(cat Sources/* Resources/* scripts/build.sh | shasum -a 256 | cut -c 1-16)
build_dir=".build/$digest"
if [ -f "$build_dir/.complete" ] && [ -x "$build_dir/Virty.app/Contents/MacOS/virty-manager" ]; then
  printf '%s\n' "Reusing immutable manager build $digest."
else
mkdir -p "$build_dir/Virty.app/Contents/MacOS"
frameworks="$build_dir/Virty.app/Contents/Frameworks"
mkdir -p "$frameworks"
xcrun clang -dynamiclib -Wall -Wextra -Werror Sources/LoopbackBindings.c \
  -install_name @rpath/libVirtyLoopback.dylib -o "$frameworks/libVirtyLoopback.dylib"
codesign --force --sign - "$frameworks/libVirtyLoopback.dylib"
xcrun clang -fobjc-arc -c Sources/KeychainCompat.m -o "$build_dir/KeychainCompat.o"
xcrun clang -fobjc-arc -Wall -Wextra -Werror -c Sources/PrivateConsole.m -o "$build_dir/PrivateConsole.o"
xcrun swiftc -swift-version 5 -warnings-as-errors \
  -module-cache-path /tmp/virty-swift-module-cache \
  -import-objc-header Sources/ManagerBridging.h \
  Sources/*.swift "$build_dir/KeychainCompat.o" "$build_dir/PrivateConsole.o" \
  -L "$frameworks" -lVirtyLoopback -Xlinker -rpath -Xlinker @executable_path/../Frameworks \
  -o "$build_dir/Virty.app/Contents/MacOS/virty-manager"
cp Resources/Info.plist "$build_dir/Virty.app/Contents/Info.plist"
codesign --force --sign - --identifier com.virty.manager \
  --entitlements Resources/Virty.entitlements "$build_dir/Virty.app"
touch "$build_dir/.complete"
fi
# Keep enrolled builds intact: ad-hoc Keychain ACLs can bind to the code hash.
if [ -d .build/Virty.app ] && [ ! -L .build/Virty.app ]; then
  mv .build/Virty.app .build/initial-unversioned.app
fi
ln -sfn "$digest/Virty.app" .build/Virty.app
printf '%s\n' 'Built .build/Virty.app (local ad-hoc signing; separate secret broker available).'
