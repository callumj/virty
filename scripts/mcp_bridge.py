#!/usr/bin/env python3
"""MCP's bounded local adapter: controls, catalog administration and creation jobs."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from vm_registry import Registry, valid_name, vm_record
import mcp_jobs
import web_bridge
import service
import vm_snapshots


FIELDS = {
    "list": set(), "create": {"name", "guestOS", "image", "disk", "cpus", "memoryGiB", "diskGiB", "autostart", "setupAssistant"},
    "job-status": {"jobId"}, "jobs": {"limit"}, "register": {"name", "vmID", "runtime", "autostart"},
    "rename": {"newName"}, "default": set(), "autostart": {"enabled"},
    "enable": set(), "disable": set(), "unregister": set(), "eject": set(),
    "status": set(), "show": set(), "hide": set(), "start": set(), "stop": {"force"}, "restart": {"force"},
    "screenshot": set(), "click": {"x", "y", "generation"}, "key": {"chord", "generation"},
    "scroll": {"x", "y", "deltaY", "generation"},
    "type-text": {"text", "delayMs", "requestId", "generation"},
    "type-secret": {"alias", "delayMs", "requestId", "generation"},
    "aliases": set(), "cancel": set(), "operation": {"requestId"},
    "fleet-status": set(), "fleet-start": set(),
    "snapshot-list": set(), "snapshot-create": {"label"}, "snapshot-restore": {"snapshotID"},
    "snapshot-delete": {"snapshotID"}, "snapshot-recover": set(), "clone": {"newName", "snapshotID"},
}
UNTARGETED = {"list", "create", "job-status", "jobs", "register", "fleet-status", "fleet-start"}


def validate(body):
    if not isinstance(body, dict) or body.get("command") not in FIELDS:
        raise ValueError("Unsupported MCP request.")
    command = body["command"]
    allowed = {"command"} | FIELDS[command] | (set() if command in UNTARGETED else {"name", "vmID"})
    if set(body) - allowed:
        raise ValueError("Unexpected MCP request field.")
    if command not in UNTARGETED or command in ("create", "register"):
        valid_name(body.get("name"))
    if command not in UNTARGETED or command == "register":
        value = body.get("vmID")
        if not isinstance(value, str) or str(uuid.UUID(value)).upper() != value:
            raise ValueError("Supply the canonical VM UUID from vm_list.")
    for key in ("autostart", "enabled", "force", "setupAssistant"):
        if key in body and type(body[key]) is not bool:
            raise ValueError("Policy and force fields must be booleans.")
    for key, low, high in (("cpus", 1, 32), ("memoryGiB", 1, 128), ("diskGiB", 4, 1024),
                           ("x", 0, 8192), ("y", 0, 8192), ("deltaY", -10, 10), ("delayMs", 20, 1000), ("limit", 1, 100)):
        if key in body and (type(body[key]) is not int or not low <= body[key] <= high):
            raise ValueError("Numeric field is outside its supported range.")
    for key, limit in (("image", 4096), ("disk", 4096), ("runtime", 4096), ("text", 4096), ("chord", 64), ("generation", 128)):
        if key in body and (not isinstance(body[key], str) or not 1 <= len(body[key]) <= limit or "\x00" in body[key]):
            raise ValueError("Invalid string field.")
    for key, pattern in (("alias", r"[a-zA-Z0-9_.-]{1,64}"), ("requestId", r"[a-zA-Z0-9-]{1,128}")):
        if key in body and (not isinstance(body[key], str) or not re.fullmatch(pattern, body[key])):
            raise ValueError("Invalid alias or typing request ID.")
    required = {"create": {"guestOS"}, "job-status": {"jobId"}, "register": {"runtime"}, "rename": {"newName"},
                "autostart": {"enabled"}, "click": {"x", "y", "generation"}, "key": {"chord", "generation"},
                "scroll": {"x", "y", "deltaY", "generation"},
                "type-text": {"text", "requestId", "generation"}, "type-secret": {"alias", "requestId", "generation"},
                "operation": {"requestId"}}.get(command, set())
    required |= {"snapshot-create": {"label"}, "snapshot-restore": {"snapshotID"},
                 "snapshot-delete": {"snapshotID"}, "clone": {"newName"}}.get(command, set())
    if not required <= set(body):
        raise ValueError("Missing required MCP request field.")
    if command == "scroll" and body["deltaY"] == 0:
        raise ValueError("Scroll deltaY must be nonzero.")
    if command == "create" and body["guestOS"] not in ("macos", "linux"):
        raise ValueError("Guest OS must be macos or linux.")
    if "newName" in body:
        valid_name(body["newName"])
    if "label" in body:
        valid_name(body["label"])
    if "snapshotID" in body:
        vm_snapshots.identifier(body["snapshotID"])
    return command


def identity(body, entry):
    if body["vmID"] != entry["id"]:
        raise ValueError("VM identity changed; refresh vm_list before retrying.")


def handle(body):
    command = validate(body)
    if command == "create":
        return mcp_jobs.start({key: value for key, value in body.items() if key != "command"})
    if command == "job-status":
        return mcp_jobs.status(body["jobId"])
    if command == "jobs":
        return mcp_jobs.listing(body.get("limit", 20))
    if command == "fleet-status":
        return {"ok": True, **service.job_status()}
    if command == "fleet-start":
        # An existing installed Aqua LaunchAgent is required; no installation or root daemon.
        result = subprocess.run([sys.executable, str(ROOT / "scripts/service.py"), "start"],
                                capture_output=True, timeout=60)
        if result.returncode:
            raise ValueError("Fleet start failed; use service.py status in the host login session.")
        return {"ok": True, **service.job_status()}
    if command == "register":
        record = vm_record(Path(body["runtime"]).expanduser().resolve(strict=True))
        if record["id"] != body["vmID"]:
            raise ValueError("Runtime identity differs from the supplied UUID.")
        vm_id = Registry().register(body["name"], body["runtime"], body.get("autostart", True))
        return {"ok": True, "name": body["name"], "vmID": vm_id}
    if command in {"snapshot-create", "snapshot-restore", "snapshot-recover", "clone"}:
        return mcp_jobs.start({key: value for key, value in body.items() if key != "command"}, kind=command)
    if command == "snapshot-list":
        return vm_snapshots.listing(body["name"], body["vmID"])
    if command == "snapshot-delete":
        return vm_snapshots.delete(body["name"], body["snapshotID"], body["vmID"])
    if command in {"rename", "default", "autostart", "enable", "disable", "unregister", "eject"}:
        registry = Registry()
        # check expected UUID again *inside* the administration lock.
        if command == "eject":
            # Worker verifies expected identity again inside service.change_vm's lock.
            return mcp_jobs.start({"name": body["name"], "vmID": body["vmID"]}, kind="eject")
        with registry.administer(body["name"]) as (name, entry):
            identity(body, entry)
            if command == "rename":
                registry.rename(name, body["newName"])
                name = body["newName"]
            elif command == "default":
                registry.set_default(name)
            elif command == "autostart":
                registry.set_policy(name, autostart=body["enabled"])
            else:
                registry.set_policy(name, enabled=command == "enable")
                if command != "enable":
                    web_bridge.cli["wait_disabled"](entry)
                if command == "unregister":
                    registry.unregister(name)
            return {"ok": True, "name": name, "vmID": entry["id"], "command": command,
                    **({"dataRetained": True} if command == "unregister" else {})}
    if command in {"start", "stop", "restart"}:
        # Cross-connection power commands cannot race catalog/configuration changes.
        with Registry().administer(body["name"]) as (_, entry):
            identity(body, entry)
            return web_bridge.handle(body)
    return web_bridge.handle(body)


if __name__ == "__main__":
    try:
        raw = sys.stdin.buffer.read(32769)
        if len(raw) > 32768:
            raise ValueError("MCP bridge request too large.")
        response = handle(json.loads(raw))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        response = {"ok": False, "error": str(error)}
    print(json.dumps(response))
