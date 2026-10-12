"""Stopped-VM APFS checkpoints. Secret values never enter this process."""
from contextlib import contextmanager
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import stat
import subprocess
import time
import uuid

from vm_registry import ROOT, Registry, atomic_write, private_directory, valid_name, vm_record
import secret_broker
import service

COMMON = ("disk.img", "machine-id.bin", "vm.json")


def identifier(value):
    if not isinstance(value, str) or str(uuid.UUID(value)).upper() != value:
        raise ValueError("Supply a canonical uppercase snapshot UUID.")
    return value


def new_id():
    return str(uuid.uuid4()).upper()


def files(record):
    return COMMON + (("efi.bin",) if record.get("guestOS", "macos") == "linux" else
                     ("hardware-model.bin", "auxiliary-storage.bin"))


def regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("Snapshot files must be owned regular files with mode 0600; symlinks are refused.")
    return info


def read_json(path):
    regular(path)
    if path.stat().st_size > 65536:
        raise ValueError("Snapshot metadata exceeds its size bound.")
    return json.loads(path.read_bytes())


def write_json(path, value):
    atomic_write(path, json.dumps(value, indent=2, sort_keys=True).encode())
    sync_directory(path.parent)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def copy_file(source, target):
    """An APFS copy-on-write clone preserves sparse disks and shares unchanged blocks."""
    regular(source)
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    clone = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True).clonefile
    clone.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int)
    clone.restype = ctypes.c_int
    if clone(os.fsencode(source), os.fsencode(target), 0):
        error = ctypes.get_errno()
        raise OSError(error, "APFS file cloning failed; keep VM runtimes and snapshots on the same cloning-capable volume")
    os.chmod(target, 0o600)
    regular(target)
    descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def copy_state(source, target, record):
    private_directory(target)
    manifest = {}
    for filename in files(record):
        src, dst = source / filename, target / filename
        copy_file(src, dst)
        size = regular(dst).st_size
        row = {"size": size}
        if filename != "disk.img":
            if size > 256 * 1024 * 1024:
                raise ValueError("VM boot metadata exceeds its size bound.")
            row["sha256"] = file_hash(dst)
        manifest[filename] = row
    return manifest


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for part in iter(lambda: file.read(65536), b""):
            digest.update(part)
    return digest.hexdigest()


def broker_call(operation, **values):
    home = Path(os.environ.get("VIRTY_BROKER_HOME", ROOT / "runtime/secrets"))
    with secret_broker.ownership(home):
        return secret_broker.request(secret_broker.read_owner(home), operation, **values)


def copy_secrets(source, target, replace=False):
    return broker_call("copy-vm", sourceVM=source, targetVM=target, replace=replace)["aliases"]


def remove_secrets(vm_id):
    broker_call("remove-vm", vmID=vm_id)


def check_secrets(checkpoint):
    aliases = sorted(item["alias"] for item in broker_call("list")["entries"]
                     if item["vmID"] == checkpoint["snapshotID"])
    if aliases != checkpoint["aliases"]:
        raise ValueError("Snapshot secret checkpoint is missing or changed; restore its broker backup first.")


def snapshot_home(home):
    root = home / "snapshots"
    private_directory(root)
    return root


def snapshot_metadata(metadata, vm_id, snapshot_id):
    identifier(snapshot_id)
    if metadata.get("version") != 1 or metadata.get("snapshotID") != snapshot_id or metadata.get("vmID") != vm_id:
        raise ValueError("Snapshot identity differs from its VM or directory.")
    if snapshot_id == vm_id:
        raise ValueError("Snapshot secret identity must differ from its live VM.")
    valid_name(metadata.get("label"))
    if not isinstance(metadata.get("aliases"), list) or any(not isinstance(alias, str) for alias in metadata["aliases"]):
        raise ValueError("Invalid snapshot alias metadata.")
    return metadata


def deletion_marker(root, snapshot_id):
    return root / (".deleting-" + identifier(snapshot_id) + ".json")


def snapshot_entries(home, vm_id):
    # Deletion metadata lives outside the tree that rmtree removes. The marker
    # remains authoritative even when the folder or its snapshot.json is gone.
    root = snapshot_home(home)
    deletions = {}
    for marker in root.glob(".deleting-*.json"):
        snapshot_id = marker.name[len(".deleting-"):-len(".json")]
        metadata = snapshot_metadata(read_json(marker), vm_id, snapshot_id)
        if metadata.get("deleting") is not True:
            raise ValueError("Invalid snapshot deletion journal.")
        deletions[snapshot_id] = metadata
    rows = []
    for folder in root.iterdir():
        if folder.name.startswith(".") or folder.name in deletions:
            continue
        identifier(folder.name)
        private_directory(folder)
        metadata = snapshot_metadata(read_json(folder / "snapshot.json"), vm_id, folder.name)
        rows.append((folder, metadata))
    rows.extend((root / snapshot_id, metadata) for snapshot_id, metadata in deletions.items())
    return rows


def load_snapshot(home, vm_id, selector, deleting=False, missing=False):
    matches = []
    for folder, metadata in snapshot_entries(home, vm_id):
        if selector in (folder.name, metadata["label"]):
            matches.append((folder, metadata))
    if not matches and missing:
        # A completed UUID deletion may be retried after its final response was
        # lost. Do not touch a broker scope when no durable checkpoint exists.
        identifier(selector)
        if selector == vm_id:
            raise ValueError("Snapshot secret identity must differ from its live VM.")
        return None, None
    if len(matches) != 1:
        raise ValueError("Unknown or ambiguous snapshot; list this VM's snapshots first.")
    folder, metadata = matches[0]
    if metadata.get("deleting") and not deleting:
        raise ValueError("Snapshot deletion was interrupted; retry its deletion.")
    return folder, metadata


def verify_snapshot(folder, metadata):
    record = read_json(folder / "vm.json")
    if record.get("id") != metadata["vmID"] or record.get("keychain") != "broker" or record.get("guestOS", "macos") != metadata["guestOS"]:
        raise ValueError("Snapshot VM record identity is invalid.")
    if set(metadata["files"]) != set(files(record)):
        raise ValueError("Snapshot file manifest is invalid.")
    for filename, row in metadata["files"].items():
        path = folder / filename
        if regular(path).st_size != row["size"]:
            raise ValueError("Snapshot file size changed; refusing restore.")
        if filename != "disk.img" and (row["size"] > 256 * 1024 * 1024 or file_hash(path) != row["sha256"]):
            raise ValueError("Snapshot boot metadata changed; refusing restore.")
    check_secrets(metadata)
    return record


def listing(name, expected_id=None):
    with Registry().administer(name) as (_, entry):
        expected(entry, expected_id)
        home = Path(entry["home"])
        rows = []
        for _, metadata in snapshot_entries(home, entry["id"]):
            rows.append({key: metadata[key] for key in ("snapshotID", "label", "createdAt", "guestOS", "aliases")})
            rows[-1]["state"] = "deleting" if metadata.get("deleting") else "ready"
        return {"ok": True, "name": name, "vmID": entry["id"], "snapshots": sorted(rows, key=lambda row: row["createdAt"], reverse=True),
                "recoveryRequired": any((home / filename).exists() for filename in ("maintenance.json", "snapshot-policy.json"))}


def expected(entry, expected_id):
    if expected_id is not None and entry["id"] != expected_id:
        raise ValueError("VM identity changed; refresh the VM list before retrying.")


@contextmanager
def worker_locks(home):
    descriptors = []
    try:
        for name in ("service.lock", "manager.lock"):
            fd = os.open(home / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            descriptors.append(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("A worker still owns this VM; no snapshot files were changed.") from None
        yield
    finally:
        for fd in descriptors:
            os.close(fd)


def set_policy(registry, name, **values):
    registry.set_policy(name, **values)
    sync_directory(registry.home)


def snapshot_capabilities(home, record, status=None):
    if status is None:
        probe = subprocess.run([record["managerExecutable"], "capabilities"], env={**os.environ, "VIRTY_HOME": str(home)},
                               capture_output=True, timeout=10)
        if probe.returncode:
            raise ValueError("Upgrade this VM's controller with service.py upgrade before using snapshots.")
        status = json.loads(probe.stdout)
    if not status.get("supportsSnapshots") or not status.get("supportsSnapshotPolicyRecovery"):
        raise ValueError("Upgrade this VM's controller with service.py upgrade before using snapshots or completing policy recovery.")


def restore_policy(registry, name, entry):
    # Relaunch the worker with its guest stopped, then restore its future startup policy.
    set_policy(registry, name, enabled=entry["enabled"], autostart=False)
    if entry["enabled"] and service.job_status()["loaded"]:
        service.wait_vm_ready({**entry, "autostart": False})
        status = service.manager_status(Path(entry["home"]))
        if not status or status.get("vmID") != entry["id"] or status.get("state") != "stopped":
            raise ValueError("Snapshot worker must be ready with its guest stopped before restoring startup policy.")
    set_policy(registry, name, enabled=entry["enabled"], autostart=entry["autostart"])


def validate_journal(journal, entry):
    if journal.get("version") != 1 or journal.get("vmID") != entry["id"]:
        raise ValueError("Maintenance journal has an invalid VM identity.")
    for key in ("transactionID", "checkpointID", "backupSecretID"):
        identifier(journal[key])
    scopes = {journal[key] for key in ("transactionID", "checkpointID", "backupSecretID")}
    if entry["id"] in scopes or len(scopes) != 3:
        raise ValueError("Maintenance identities must be distinct from each other and the live VM.")
    policy = journal["policy"]
    if policy.get("id") != entry["id"] or policy.get("home") != entry["home"] or any(type(policy.get(key)) is not bool for key in ("enabled", "autostart")):
        raise ValueError("Invalid maintenance startup policy.")
    return policy


def policy_journal(home, entry):
    completion = home / "snapshot-policy.json"
    if not completion.exists():
        return None
    journal = read_json(completion)
    validate_journal(journal, entry)
    if journal.get("phase") != "policy":
        raise ValueError("Invalid snapshot policy completion phase.")
    marker = home / "maintenance.json"
    if marker.exists():
        maintenance = read_json(marker)
        validate_journal(maintenance, entry)
        if {**maintenance, "phase": "policy"} != journal:
            raise ValueError("Conflicting snapshot recovery journals; preserve both for inspection.")
    return journal


def complete_policy(registry, name, entry, journal):
    home = Path(entry["home"])
    policy = validate_journal(journal, entry)
    completion = home / "snapshot-policy.json"
    existing = policy_journal(home, entry)
    journal = {**journal, "phase": "policy"}
    if existing is not None and existing != journal:
        raise ValueError("Conflicting snapshot recovery journals; preserve both for inspection.")
    # Publish recovery state before releasing the disk-maintenance boot refusal.
    # Workers may launch while this journal exists, but suppress autostart; all
    # explicit start commands also refuse it until completion is durable.
    write_json(completion, journal)
    set_policy(registry, name, enabled=False, autostart=False)
    runpy.run_path(str(ROOT / "bin/virty"))["wait_disabled"](entry)
    with worker_locks(home):
        (home / "maintenance.json").unlink(missing_ok=True)
        sync_directory(home)
    # Legacy interrupted maintenance may be recovered before its controller is
    # upgraded. Finish disk recovery but keep it disabled with this journal so
    # service.py upgrade can replace the old controller safely, then recover.
    snapshot_capabilities(home, vm_record(home), service.manager_status(home))
    restore_policy(registry, name, policy)
    completion.unlink()
    sync_directory(home)


@contextmanager
def cold(registry, name, entry, operation):
    home = Path(entry["home"])
    record = vm_record(home)
    if record.get("keychain") != "broker" or not record.get("installed") or record.get("installMedia"):
        raise ValueError("Snapshots require an installed broker-backed VM with its installer detached.")
    marker = home / "maintenance.json"
    if marker.exists() or (home / "snapshot-policy.json").exists():
        raise ValueError("Snapshot maintenance is pending; use virty snapshot recover.")
    journal = {"version": 1, "vmID": entry["id"], "operation": operation, "phase": "locked", "policy": entry,
               "transactionID": new_id(), "checkpointID": new_id(), "backupSecretID": new_id()}
    write_json(marker, journal)
    policy_started = False
    try:
        status = service.manager_status(home)
        if status and (status.get("vmID") != entry["id"] or status.get("state") != "stopped" or status.get("inputBusy")):
            raise ValueError("Shut down this guest before snapshotting, restoring or cloning; no force power-off was performed.")
        snapshot_capabilities(home, record, status)
        policy_started = True
        set_policy(registry, name, enabled=False)
        runpy.run_path(str(ROOT / "bin/virty"))["wait_disabled"](entry)
        with worker_locks(home):
            yield home, record, journal
    except BaseException:
        # Applying/committed transactions need explicit recovery; never boot mixed files.
        if journal["phase"] == "locked":
            if policy_started:
                complete_policy(registry, name, entry, journal)
            else:
                marker.unlink()
                sync_directory(home)
        raise
    else:
        complete_policy(registry, name, entry, journal)


def cleanup_transaction(home, journal):
    remove_secrets(journal["backupSecretID"])
    transaction = home / (".snapshot-transaction-" + journal["transactionID"])
    if transaction.exists():
        shutil.rmtree(transaction)


def create(name, label, expected_id=None):
    valid_name(label)
    registry = Registry()
    with registry.administer(name) as (_, entry):
        expected(entry, expected_id)
        home = Path(entry["home"])
        for _, metadata in snapshot_entries(home, entry["id"]):
            if metadata["label"] == label:
                raise ValueError("Snapshot label already exists; choose a new label.")
        with cold(registry, name, entry, "create") as (home, record, journal):
            snapshot_id = journal["checkpointID"]
            pending = snapshot_home(home) / (".pending-" + snapshot_id)
            # A killed creation leaves this journal/checkpoint for explicit cleanup.
            journal["phase"] = "creating"
            write_json(home / "maintenance.json", journal)
            try:
                manifest = copy_state(home, pending, record)
                aliases = copy_secrets(entry["id"], snapshot_id)
                metadata = {"version": 1, "snapshotID": snapshot_id, "vmID": entry["id"], "label": label,
                            "createdAt": time.time(), "guestOS": record.get("guestOS", "macos"), "files": manifest, "aliases": aliases}
                write_json(pending / "snapshot.json", metadata)
                os.rename(pending, snapshot_home(home) / snapshot_id)
                sync_directory(snapshot_home(home))
                journal["phase"] = "locked"
            except Exception:
                remove_secrets(snapshot_id)
                shutil.rmtree(pending, ignore_errors=True)
                journal["phase"] = "locked"
                raise
        return {"ok": True, "name": name, "vmID": entry["id"], "snapshotID": snapshot_id, "label": label, "state": "stopped"}


def restore(name, selector, expected_id=None):
    registry = Registry()
    with registry.administer(name) as (_, entry):
        expected(entry, expected_id)
        home = Path(entry["home"])
        folder, metadata = load_snapshot(home, entry["id"], selector)
        record = verify_snapshot(folder, metadata)
        with cold(registry, name, entry, "restore") as (home, current, journal):
            transaction = home / (".snapshot-transaction-" + journal["transactionID"])
            private_directory(transaction)
            journal["phase"] = "preparing"
            write_json(home / "maintenance.json", journal)
            try:
                copy_state(home, transaction / "before", current)
                copy_secrets(entry["id"], journal["backupSecretID"])
                copy_state(folder, transaction / "after", record)
                # Disk state rolls back; operational controller upgrades remain in place.
                record["managerExecutable"] = current["managerExecutable"]
                write_json(transaction / "after/vm.json", record)
                journal["phase"] = "applying"
                write_json(home / "maintenance.json", journal)
                apply_state(transaction / "after", home, record)
                copy_secrets(metadata["snapshotID"], entry["id"], replace=True)
                journal["phase"] = "committed"
                write_json(home / "maintenance.json", journal)
                cleanup_transaction(home, journal)
                journal["phase"] = "locked"
            except Exception:
                if journal["phase"] == "applying":
                    rollback_transaction(home, entry["id"], journal)
                elif journal["phase"] == "preparing":
                    cleanup_transaction(home, journal)
                else:
                    raise
                journal["phase"] = "locked"
                raise
        return {"ok": True, "name": name, "vmID": entry["id"], "snapshotID": metadata["snapshotID"], "restored": True, "state": "stopped"}


def apply_state(source, home, record):
    # Keep vm.json present throughout. Persistent input ledgers are never rolled back.
    for filename in sorted(files(record), key=lambda name: name == "vm.json"):
        os.replace(source / filename, home / filename)
    sync_directory(home)


def rollback_transaction(home, vm_id, journal):
    transaction = home / (".snapshot-transaction-" + journal["transactionID"])
    before = transaction / "before"
    original = read_json(before / "vm.json")
    # Keep the backup intact until recovery fully succeeds, so recovery is repeatable.
    retry = transaction / "recovery"
    shutil.rmtree(retry, ignore_errors=True)
    copy_state(before, retry, original)
    apply_state(retry, home, original)
    copy_secrets(journal["backupSecretID"], vm_id, replace=True)
    journal["phase"] = "rolledback"
    write_json(home / "maintenance.json", journal)
    cleanup_transaction(home, journal)


def recover(name, expected_id=None):
    registry = Registry()
    with registry.administer(name) as (_, entry):
        expected(entry, expected_id)
        home = Path(entry["home"])
        marker = home / "maintenance.json"
        completion = policy_journal(home, entry)
        if completion is not None:
            complete_policy(registry, name, entry, completion)
            return {"ok": True, "name": name, "vmID": entry["id"], "recovered": True, "state": "stopped"}
        if not marker.exists():
            return {"ok": True, "name": name, "vmID": entry["id"], "recovered": False}
        journal = read_json(marker)
        validate_journal(journal, entry)
        set_policy(registry, name, enabled=False)
        runpy.run_path(str(ROOT / "bin/virty"))["wait_disabled"](entry)
        with worker_locks(home):
            phase = journal["phase"]
            if phase == "applying":
                rollback_transaction(home, entry["id"], journal)
            elif phase in ("preparing", "committed", "rolledback"):
                cleanup_transaction(home, journal)
            elif phase == "creating":
                pending = snapshot_home(home) / (".pending-" + journal["checkpointID"])
                if not (snapshot_home(home) / journal["checkpointID"]).exists():
                    remove_secrets(journal["checkpointID"])
                    shutil.rmtree(pending, ignore_errors=True)
            elif phase != "locked":
                raise ValueError("Unknown maintenance phase; preserve this journal for inspection.")
        complete_policy(registry, name, entry, journal)
        return {"ok": True, "name": name, "vmID": entry["id"], "recovered": True, "state": "stopped"}


def delete(name, selector, expected_id=None):
    with Registry().administer(name) as (_, entry):
        expected(entry, expected_id)
        folder, metadata = load_snapshot(Path(entry["home"]), entry["id"], selector, deleting=True, missing=True)
        if metadata is None:
            return {"ok": True, "name": name, "vmID": entry["id"], "snapshotID": selector, "deleted": True}
        metadata["deleting"] = True
        marker = deletion_marker(folder.parent, metadata["snapshotID"])
        write_json(marker, metadata)
        remove_secrets(metadata["snapshotID"])
        if folder.exists():
            private_directory(folder)
            shutil.rmtree(folder)
        sync_directory(folder.parent)
        marker.unlink()
        sync_directory(folder.parent)
        return {"ok": True, "name": name, "vmID": entry["id"], "snapshotID": metadata["snapshotID"], "deleted": True}


def clone(name, new_name, selector=None, expected_id=None):
    valid_name(new_name)
    registry = Registry()
    with registry.administer(name) as (_, entry):
        expected(entry, expected_id)
        if new_name in registry.read()["vms"]:
            raise ValueError("Clone name already exists.")
        source = Path(entry["home"])
        if selector:
            folder, metadata = load_snapshot(source, entry["id"], selector)
            record = verify_snapshot(folder, metadata)
            return clone_state(registry, new_name, folder, record, metadata["snapshotID"])
        with cold(registry, name, entry, "clone") as (source, record, _):
            return clone_state(registry, new_name, source, record, entry["id"])


def clone_state(registry, name, source, source_record, secret_id):
    home = ROOT / "runtime/vms" / new_id()
    vm_id = new_id()
    private_directory(home)
    record = {**source_record, "id": vm_id}
    write_json(home / "clone.json", {"version": 1, "name": name, "vmID": vm_id, "sourceVMID": source_record["id"], "state": "building"})
    registered = False
    try:
        copy_state(source, home, source_record)
        record["managerExecutable"] = str((ROOT / ".build/Virty.app/Contents/MacOS/virty-manager").resolve(strict=True))
        write_json(home / "vm.json", record)
        completed = subprocess.run([record["managerExecutable"], "clone-identity"],
                                   env={**os.environ, "VIRTY_HOME": str(home)}, capture_output=True, timeout=30)
        if completed.returncode:
            raise ValueError("Could not create or validate the clone's new virtual hardware identity.")
        copy_secrets(secret_id, vm_id)
        registry.register(name, home, autostart=False)
        registered = True
        sync_directory(registry.home)
    except Exception:
        if registered:
            raise ValueError("Clone is registered but its registry directory could not be synced; inspect the VM list before retrying.") from None
        remove_secrets(vm_id)
        shutil.rmtree(home)
        raise
    (home / "clone.json").unlink()
    return {"ok": True, "name": name, "vmID": vm_id, "guestOS": record.get("guestOS", "macos"), "state": "stopped", "autostart": False}
