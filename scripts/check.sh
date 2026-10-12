#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
mkdir -p .build
xcrun swiftc -swift-version 5 -warnings-as-errors \
  -module-cache-path /tmp/virty-swift-module-cache \
  Sources/Support.swift Tests/VMRecordTests.swift -o .build/vm-record-tests
.build/vm-record-tests
xcrun swiftc -swift-version 5 -warnings-as-errors \
  -module-cache-path /tmp/virty-swift-module-cache \
  Sources/Support.swift Sources/GuestInput.swift Tests/KeyboardTests.swift -o .build/keyboard-tests
.build/keyboard-tests
xcrun swiftc -swift-version 5 -warnings-as-errors \
  -module-cache-path /tmp/virty-swift-module-cache \
  Sources/Support.swift Sources/ConsoleFrameDecoder.swift Tests/ConsoleTests.swift -o .build/console-tests
.build/console-tests
xcrun swiftc -swift-version 5 -warnings-as-errors \
  -module-cache-path /tmp/virty-swift-module-cache \
  Sources/Support.swift Sources/BrokerClient.swift Tests/BrokerClientTests.swift -o .build/broker-client-tests
.build/broker-client-tests
xcrun swiftc -swift-version 5 -warnings-as-errors \
  -module-cache-path /tmp/virty-swift-module-cache \
  Sources/Support.swift Sources/OperationLedger.swift Tests/OperationLedgerTests.swift -o .build/operation-ledger-tests
.build/operation-ledger-tests
xcrun swiftc -swift-version 5 -warnings-as-errors \
  -module-cache-path /tmp/virty-swift-module-cache \
  Sources/Support.swift Sources/ControlSocket.swift Tests/ControlSocketTests.swift -o .build/control-socket-tests
.build/control-socket-tests
mkdir -p .build/check-loopback
xcrun clang -dynamiclib -Wall -Wextra -Werror Sources/LoopbackBindings.c \
  -install_name @rpath/libVirtyLoopback.dylib -o .build/check-loopback/libVirtyLoopback.dylib
codesign --force --sign - .build/check-loopback/libVirtyLoopback.dylib
xcrun clang -Wall -Wextra -Werror Tests/LoopbackTests.c -L .build/check-loopback -lVirtyLoopback \
  -Wl,-rpath,@executable_path/check-loopback -o .build/loopback-tests
.build/loopback-tests
xcrun clang -fobjc-arc -Wall -Wextra -Werror -c Broker/CryptoCompat.m -o .build/archive-crypto.o
xcrun swiftc -swift-version 5 -warnings-as-errors \
  -module-cache-path /tmp/virty-swift-module-cache -import-objc-header Broker/CryptoCompat.h \
  Broker/Archive.swift Tests/ArchiveTests.swift .build/archive-crypto.o -o .build/archive-tests
.build/archive-tests
xcrun clang -fobjc-arc -Wall -Wextra -Werror -c Sources/KeychainCompat.m -o .build/broker-keychain-compat.o
xcrun swiftc -swift-version 5 -warnings-as-errors \
  -module-cache-path /tmp/virty-swift-module-cache -import-objc-header Broker/CryptoCompat.h \
  Broker/Archive.swift Broker/Store.swift Tests/BrokerStoreTests.swift \
  .build/archive-crypto.o .build/broker-keychain-compat.o -o .build/broker-store-tests
.build/broker-store-tests
python3 -m unittest discover -s Tests -p 'test_*.py'
node --check Web/server.mjs
node --check Web/app.js
node --test Tests/web.test.mjs
node --check MCP/server.mjs
node --test Tests/mcp.test.mjs
git diff --check
