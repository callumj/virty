"""Opt-in logged-in-host checks using only disposable broker items, never VM secrets."""
import base64
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import secret_broker as broker


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def denied(work):
    try:
        work()
    except (OSError, ValueError):
        return
    raise RuntimeError("Expected broker operation to be denied.")


def compile_variant(folder):
    # An altered fixed error string changes the signed code while preserving its protocol.
    source = folder / "main.swift"
    source.write_text((ROOT / "Broker/main.swift").read_text().replace(
        "Broker request failed; no secret details returned.", "Variant broker request failed; no secret details returned."))
    objects = []
    for name, path in (("compat", ROOT / "Sources/KeychainCompat.m"), ("crypto", ROOT / "Broker/CryptoCompat.m")):
        output = folder / (name + ".o")
        subprocess.run(["xcrun", "clang", "-fobjc-arc", "-c", str(path), "-o", str(output)], check=True)
        objects.append(str(output))
    executable = folder / "second-broker"
    subprocess.run(["xcrun", "swiftc", "-swift-version", "5", "-warnings-as-errors",
                    "-module-cache-path", "/tmp/virty-swift-module-cache", "-import-objc-header",
                    str(ROOT / "Broker/CryptoCompat.h"), str(ROOT / "Broker/Archive.swift"),
                    str(ROOT / "Broker/Store.swift"), str(source), *objects, "-o", str(executable)], check=True)
    subprocess.run(["codesign", "--force", "--sign", "-", "--identifier", "com.virty.secret-broker", str(executable)], check=True)
    subprocess.run(["codesign", "--verify", "--strict", str(executable)], check=True)
    return executable


def administration(home, arguments, passphrase=None):
    descriptors = []
    try:
        if passphrase is not None:
            reader, writer = os.pipe()
            os.write(writer, passphrase.encode() + b"\n")
            os.close(writer)
            descriptors.append(reader)
            arguments = [*arguments, "--passphrase-fd", str(reader)]
        result = subprocess.run([sys.executable, str(ROOT / "bin/virty-secrets"), "--home", str(home), *arguments],
                                pass_fds=descriptors, capture_output=True, timeout=30)
        response = json.loads(result.stdout)
        require(result.returncode == 0 and response.get("ok"), "Administration CLI failed")
        return response
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def main():
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix="virty-broker-check-", dir="/tmp") as directory:
        folder = Path(directory)
        second_binary = compile_variant(folder)
        home = folder / "state"
        owners = {}
        try:
            broker.initialize(home)
            first = broker.read_owner(home)
            owners[first["instance"]] = first
            vm_a, vm_b = (str(uuid.uuid4()).upper() for _ in range(2))
            values = [secrets.token_hex(24), secrets.token_hex(24)]
            for vm, value in zip((vm_a, vm_b), values):
                broker.request(first, "put", vmID=vm, alias="vm-login", value=value)
            checkpoint, copied = (str(uuid.uuid4()).upper() for _ in range(2))
            require(broker.request(first, "copy-vm", sourceVM=vm_a, targetVM=checkpoint, replace=False)["aliases"] == ["vm-login"], "Checkpoint copy leaked or lost metadata")
            broker.request(first, "copy-vm", sourceVM=checkpoint, targetVM=copied, replace=False)
            require(broker.request(first, "read", vmID=copied, alias="vm-login")["value"] == values[0], "Copied value differed")
            denied(lambda: broker.request(first, "copy-vm", sourceVM=vm_b, targetVM=copied, replace=False))
            denied(lambda: broker.request(first, "copy-vm", sourceVM=vm_a, targetVM=vm_a, replace=True))
            broker.request(first, "put", vmID=copied, alias="later-alias", value="disposable-public-value")
            broker.request(first, "copy-vm", sourceVM=vm_b, targetVM=copied, replace=True)
            require(broker.request(first, "read", vmID=copied, alias="vm-login")["value"] == values[1], "Replacement value differed")
            require([row["alias"] for row in broker.request(first, "list")["entries"] if row["vmID"] == copied] == ["vm-login"], "Replacement retained later aliases")
            for temporary_vm in (checkpoint, copied):
                broker.request(first, "remove-vm", vmID=temporary_vm)
                broker.request(first, "remove-vm", vmID=temporary_vm)
            denied(lambda: broker.request(first, "put", vmID=vm_a, alias="vm-login", value=values[0]))
            denied(lambda: broker.request(first, "read", vmID=str(uuid.uuid4()).upper(), alias="vm-login"))
            impostor = broker.candidate(second_binary)
            impostor["instance"] = first["instance"]
            denied(lambda: broker.request(impostor, "read", vmID=vm_a, alias="vm-login"))
            passphrase = secrets.token_hex(32)
            archive = broker.request(first, "export", passphrase=passphrase)["archive"]
            archive_file = folder / "backup.virty"
            broker.write_archive(archive_file, archive)
            require(archive_file.stat().st_mode & 0o777 == 0o600, "Backup permissions failed")
            require(all(value.encode() not in archive_file.read_bytes() for value in values), "Backup contained plaintext")
            denied(lambda: broker.write_archive(archive_file, archive))
            administration(home, ["export", "--output", str(folder / "cli-backup.virty")], passphrase)
            # A failed restore keeps the published owner. Pending metadata allows cleanup.
            with broker.ownership(home):
                denied(lambda: broker.restore_archive(home, archive, "wrong-passphrase-value", second_binary))
            require(broker.read_owner(home)["instance"] == first["instance"], "Failed restore changed owner")
            envelope = json.loads(base64.b64decode(archive))
            ciphertext = bytearray(base64.b64decode(envelope["sealed"]))
            ciphertext[-1] ^= 1
            envelope["sealed"] = base64.b64encode(ciphertext).decode()
            tampered = base64.b64encode(json.dumps(envelope).encode()).decode()
            with broker.ownership(home):
                denied(lambda: broker.restore_archive(home, tampered, passphrase, second_binary))
            result = broker.upgrade(home, second_binary)
            require(result["upgraded"] and result["restored"] == 2, "Cross-build upgrade failed")
            second = broker.read_owner(home)
            owners[second["instance"]] = second
            require(first["sha256"] != second["sha256"], "Test brokers were not distinct")
            for vm, value in zip((vm_a, vm_b), values):
                require(broker.request(second, "read", vmID=vm, alias="vm-login")["value"] == value, "Restored value differed")
            # Old owner remains usable, and restore does not replace a nonempty namespace.
            broker.request(first, "verify")
            denied(lambda: broker.request(second, "restore", archive=archive, passphrase=passphrase))
            # Exercise a durable manual backup restore into a new namespace.
            administration(home, ["restore", "--input", str(archive_file), "--binary", first["executable"]], passphrase)
            third = broker.read_owner(home)
            owners[third["instance"]] = third
            require(broker.request(third, "read", vmID=vm_b, alias="vm-login")["value"] == values[1], "Manual restore failed")
            administration(home, ["rollback", "--instance", second["instance"]])
            require(broker.read_owner(home)["instance"] == second["instance"], "Owner rollback failed")
            # Backup recovery must work even when the former executable is missing.
            broken = {**second, "executable": str(folder / "missing-broker")}
            broker.atomic_json(home / "broker.json", broken)
            administration(home, ["restore", "--input", str(archive_file), "--binary", first["executable"]], passphrase)
            fourth = broker.read_owner(home)
            owners[fourth["instance"]] = fourth
            require(broker.request(fourth, "read", vmID=vm_a, alias="vm-login")["value"] == values[0], "Missing-owner backup recovery failed")
            # Restore publishes the broken receipt too; retain the actual old executable for cleanup.
            broker.atomic_json(home / "history" / (second["instance"] + ".json"), second)
            print("Broker live checks passed: scoped aliases, cross-build denial/migration, encrypted backup/restore, tampering, duplicate refusal, and owner retention.")
        finally:
            for category in ("history", "pending"):
                for path in (home / category).glob("*.json"):
                    metadata = broker.read_record(path, verify_binary=False)
                    owner = owners.get(metadata["instance"]) or broker.validate_owner(metadata)
                    owners[owner["instance"]] = owner
            for owner in owners.values():
                for entry in broker.request(owner, "list")["entries"]:
                    broker.request(owner, "remove", **entry)
                require(not broker.request(owner, "list")["entries"], "Disposable item cleanup failed")
            print("Disposable broker items removed; no VM owner was accessed.")


if __name__ == "__main__":
    main()
