#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
VIRTY_RUNTIME="${VIRTY_HOME:-$PWD/runtime/vm}"
export VIRTY_HOME="$VIRTY_RUNTIME"
if [ -f "$VIRTY_RUNTIME/vm.json" ]; then
  manager=$(python3 - "$VIRTY_RUNTIME/vm.json" <<'PY'
import json
import sys
with open(sys.argv[1]) as file:
    print(json.load(file)["managerExecutable"])
PY
)
else
  manager=$(python3 - "$PWD/.build/Virty.app/Contents/MacOS/virty-manager" <<'PY'
from pathlib import Path
import sys
print(Path(sys.argv[1]).resolve())
PY
)
fi
if [ ! -x "$manager" ]; then
  printf '%s\n' 'The VM owner build is missing. Keep .build after enrollment; see docs/macos.md.' >&2
  exit 1
fi
exec "$manager" "$@"
