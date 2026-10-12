#!/usr/bin/env python3
"""Run against a running disposable guest with Terminal focused and an empty prompt.

This intentionally sends public test text to the guest. It never reads Keychain.
"""
import importlib.machinery
import importlib.util
from pathlib import Path
import threading
import time
import uuid

loader = importlib.machinery.SourceFileLoader("virty_cli", str(Path(__file__).resolve().parents[1] / "bin/virty"))
spec = importlib.util.spec_from_loader(loader.name, loader)
cli = importlib.util.module_from_spec(spec)
loader.exec_module(cli)


def request(command, **values):
    return cli.call({"command": command, **values})


def main():
    assert request("status")["state"] == "running"
    assert not request("click", x=-1, y=1)["ok"]
    assert not request("key", chord="invalid")["ok"]
    assert not request("click", x=1, y=1, generation="stale")["ok"]
    assert not request("type-secret", alias="unrelated-host-login", requestId=str(uuid.uuid4()))["ok"]
    assert not request("type-text", text="🔐", requestId=str(uuid.uuid4()))["ok"]
    assert not request("status")["inputBusy"]
    operation = str(uuid.uuid4())
    result = []

    def type_slowly():
        result.append(request("type-text", text="echo " + "x" * 100, delayMs=200, requestId=operation))

    thread = threading.Thread(target=type_slowly)
    thread.start()
    for _ in range(40):
        if request("status")["inputBusy"]:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("Typing did not become busy")
    assert not request("screenshot")["ok"]
    assert not request("click", x=640, y=500)["ok"]
    assert request("cancel")["ok"]
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert not result[0]["ok"]
    assert request("operation", requestId=operation)["status"] == "uncertain"
    replay = request("type-text", text="echo " + "x" * 100, delayMs=200, requestId=operation)
    assert not replay["ok"] and replay["replayed"] and replay["status"] == "uncertain"
    assert not request("status")["inputBusy"]
    assert request("key", chord="ctrl+u")["ok"]
    print("Live control checks passed: bounds, stale generation, unknown alias, unsupported input, serialization, cancel, and replay protection.")
    print("Cancelled operation ID:", operation)


if __name__ == "__main__":
    main()
