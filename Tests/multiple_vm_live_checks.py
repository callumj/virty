#!/usr/bin/env python3
"""Opt-in: two running disposable guests, shown consoles, first VM at empty Terminal prompt."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import runpy
import sys
from pathlib import Path
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from vm_registry import Registry
call = runpy.run_path(str(ROOT / "bin/virty"))["call"]

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--vm", required=True, help="VM with guest Terminal focused at empty prompt")
parser.add_argument("--other", required=True, help="Other VM with its console shown")
args = parser.parse_args()
registry = Registry()
_, first = registry.entry(args.vm)
_, other = registry.entry(args.other)
assert first["id"] != other["id"], "Select two distinct VMs"


def request(entry, command, **values):
    return call({"command": command, "vmID": entry["id"], **values}, entry["home"])


first_status = request(first, "status")
other_status = request(other, "status")
assert first_status["state"] == other_status["state"] == "running"
assert first_status["consoleVisible"] and other_status["consoleVisible"]
assert not call({"command": "status", "vmID": first["id"]}, other["home"])["ok"]
assert not call({"command": "status", "vmID": 7}, other["home"])["ok"]
assert not request(other, "screenshot", generation=first_status["generation"])["ok"]
assert request(first, "screenshot")["ok"] and request(other, "screenshot")["ok"]
operation_id = str(uuid.uuid4())
with ThreadPoolExecutor(max_workers=1) as pool:
    delivery = pool.submit(request, first, "type-text", text="# Virty concurrent check", delayMs=200, requestId=operation_id)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if request(first, "status")["inputBusy"]:
            break
        time.sleep(0.02)
    else:
        raise AssertionError("Typing never became busy")
    assert not request(first, "screenshot")["ok"]
    assert request(other, "status")["state"] == "running"
    assert request(other, "screenshot")["ok"], "Sibling capture must remain available during typing"
    assert request(first, "cancel")["ok"]
    result = delivery.result(timeout=10)
    assert not result["ok"], result
assert request(first, "key", chord="ctrl+u")["ok"]
assert request(first, "operation", requestId=operation_id)["status"] == "uncertain"
assert not request(other, "operation", requestId=operation_id)["ok"], "Operation IDs must stay VM-scoped"
assert request(other, "status")["generation"] == other_status["generation"]
print("Live multi-VM checks passed: UUID/generation refusal, isolated input/capture/cancel and VM-scoped operation ledger.")
