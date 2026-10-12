#!/usr/bin/env python3
"""Bounded web adapter: named controls and alias metadata, never secret values."""
import json
import os
from pathlib import Path
import runpy
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from vm_registry import Registry
import secret_broker

cli = runpy.run_path(str(ROOT / "bin/virty"))


def handle(body):
    if not isinstance(body, dict):
        raise ValueError("Request must be an object.")
    command = body.get("command")
    if command == "list":
        result = cli["list_vms"]()
        for row in result["vms"]:
            row.pop("home", None)
        return result
    name, entry = Registry().entry(body.get("name"))
    if body.get("vmID") != entry["id"]:
        raise ValueError("VM identity changed; refresh the VM list.")
    if command in {"snapshot-list", "snapshot-create", "snapshot-restore", "snapshot-delete", "snapshot-recover", "clone", "snapshot-job"}:
        import mcp_bridge
        import mcp_jobs
        if command == "snapshot-job":
            if set(body) != {"command", "name", "vmID", "jobId"}:
                raise ValueError("Unexpected job status field.")
            result = mcp_jobs.status(body["jobId"])
            if result.get("name") != name or result.get("sourceVMID") != entry["id"]:
                raise ValueError("Job identity differs from the selected VM.")
            return result
        return mcp_bridge.handle(body)
    if command == "aliases":
        broker_home = Path(os.environ.get("VIRTY_BROKER_HOME", ROOT / "runtime/secrets"))
        with secret_broker.ownership(broker_home):
            owner = secret_broker.read_owner(broker_home)
            items = secret_broker.request(owner, "list")["entries"]
        return {"ok": True, "aliases": sorted(item["alias"] for item in items if item["vmID"] == entry["id"])}
    allowed = {"status", "show", "hide", "start", "stop", "restart", "screenshot", "click", "scroll", "key", "type-text", "type-secret", "cancel", "operation"}
    if command not in allowed:
        raise ValueError("Unsupported web control.")
    if command in {"click", "scroll", "key", "type-text", "type-secret"} and not isinstance(body.get("generation"), str):
        raise ValueError("Connect to the console before sending input.")
    fields = {
        "stop": {"force"}, "restart": {"force"}, "click": {"x", "y", "generation"},
        "scroll": {"x", "y", "deltaY", "generation"},
        "key": {"chord", "generation"}, "type-text": {"text", "delayMs", "requestId", "generation"},
        "type-secret": {"alias", "delayMs", "requestId", "generation"}, "operation": {"requestId"},
    }.get(command, set())
    if set(body) - {"command", "name", "vmID"} - fields:
        raise ValueError("Unexpected control field.")
    if command == "scroll":
        for key, low, high in (("x", 0, 8192), ("y", 0, 8192), ("deltaY", -10, 10)):
            if type(body.get(key)) is not int or not low <= body[key] <= high:
                raise ValueError("Invalid scroll coordinates or deltas.")
        if body["deltaY"] == 0:
            raise ValueError("Scroll deltaY must be nonzero.")
    request = {key: body[key] for key in fields if key in body}
    request.update(command=command, vmID=entry["id"])
    if command == "restart":
        status = cli["call"]({"command": "status", "vmID": entry["id"]}, entry["home"])
        if status.get("inputBusy"):
            raise ValueError("Cancel active input before restarting.")
        if status.get("state") != "stopped":
            stopped = cli["call"]({"command": "stop", "force": body.get("force", False), "vmID": entry["id"]}, entry["home"])
            if not stopped.get("ok"):
                return stopped
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                status = cli["call"]({"command": "status", "vmID": entry["id"]}, entry["home"])
                if status.get("state") == "stopped":
                    break
                time.sleep(0.25)
            else:
                raise ValueError("Guest has not shut down. Use force power off only if you accept losing unsaved work.")
        request = {"command": "start", "vmID": entry["id"]}
    return cli["call"](request, entry["home"])


if __name__ == "__main__":
    try:
        raw = sys.stdin.buffer.read(32769)
        if len(raw) > 32768:
            raise ValueError("Request too large.")
        result = handle(json.loads(raw))
    except (OSError, ValueError, KeyError, TypeError) as error:
        result = {"ok": False, "error": str(error)}
    print(json.dumps(result))
