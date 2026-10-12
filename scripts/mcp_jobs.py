"""Private, durable VM administration jobs; no credentials or MCP transport state."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
import uuid

from vm_registry import ROOT, Registry, atomic_write, private_directory, valid_name


def job_home():
    return Path(os.environ.get("VIRTY_MCP_HOME", ROOT / "runtime/mcp")) / "jobs"


@contextmanager
def locked():
    home = job_home()
    private_directory(home)
    fd = os.open(home / "jobs.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield home
    finally:
        os.close(fd)


def job_path(home, job_id):
    if not isinstance(job_id, str) or str(uuid.UUID(job_id)).upper() != job_id:
        raise ValueError("Invalid VM job UUID.")
    return home / (job_id + ".json")


def read(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("Job record must be an owned regular file with mode 0600.")
    return json.loads(path.read_text())


def write(path, record):
    record["updatedAt"] = time.time()
    atomic_write(path, json.dumps(record, sort_keys=True).encode())


def refresh(path, record):
    if record["state"] not in ("queued", "running"):
        return record
    pid = record.get("pid")
    alive = True
    if pid:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            alive = False
    elif time.time() - record["updatedAt"] > 30:
        alive = False
    if not alive:
        record.update(state="failed", error="VM job worker exited without a result. Inspect the VM list and private job log before retrying; partial runtime data may remain.")
        write(path, record)
    return record


def public(record):
    return {key: value for key, value in record.items() if key != "request"}


def start(request, kind="create"):
    valid_name(request["name"])
    if kind == "create":
        if request["name"] in Registry().read()["vms"]:
            raise ValueError("VM name already exists.")
        if request.get("setupAssistant") and request["guestOS"] != "macos":
            raise ValueError("Setup Assistant applies to macOS only; Linux already uses guest-managed setup.")
        if request["guestOS"] == "macos" and (request.get("disk") or any(key in request for key in ("cpus", "memoryGiB", "diskGiB"))):
            raise ValueError("Disk imports and resource options apply to Linux only; macOS currently uses 4 CPUs, 6 GiB RAM and a 64 GiB disk.")
        if not request.get("image") and not (request["guestOS"] == "linux" and request.get("disk")):
            raise ValueError("Supply a local macOS IPSW / ARM64 Linux ISO, or an installed Linux raw EFI disk.")
        for key in ("image", "disk"):
            if key in request:
                path = Path(request[key]).expanduser().resolve(strict=True)
                if not path.is_file():
                    raise ValueError("Installation media must be a regular file.")
                request[key] = str(path)
    elif kind in ("eject", "snapshot-create", "snapshot-restore", "snapshot-recover", "clone"):
        _, entry = Registry().entry(request["name"])
        if entry["id"] != request["vmID"]:
            raise ValueError("VM identity changed; refresh the VM list before retrying.")
        if kind == "clone":
            valid_name(request["newName"])
            if request["newName"] in Registry().read()["vms"]:
                raise ValueError("Clone name already exists.")
    else:
        raise ValueError("Unsupported VM job kind.")
    with locked() as home:
        for path in home.glob("*.json"):
            record = refresh(path, read(path))
            # Controller/broker builds share an output location. Serialize creation.
            if record["state"] in ("queued", "running") and kind == "create" and record.get("kind", "create") == "create":
                raise ValueError("Another VM creation is running; check its job before creating the next VM.")
            owned_names = {request["name"], request.get("newName")} - {None}
            active_names = {record["name"], record["request"].get("newName")} - {None}
            if record["state"] in ("queued", "running") and owned_names & active_names:
                raise ValueError("Another VM job owns this name; check its job status.")
        job_id = str(uuid.uuid4()).upper()
        path = job_path(home, job_id)
        record = {"ok": True, "jobId": job_id, "kind": kind, "state": "queued", "name": request["name"],
                  "createdAt": time.time(), "request": request}
        if "vmID" in request:
            record["sourceVMID"] = request["vmID"]
        write(path, record)
        try:
            # Detached from the MCP process, with no inherited protocol pipes.
            worker = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--run", job_id],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      start_new_session=True, close_fds=True)
            record["pid"] = worker.pid
            write(path, record)
        except OSError:
            record.update(state="failed", error="Could not start the VM job worker.")
            write(path, record)
            raise
        return public(record)


def status(job_id):
    with locked() as home:
        path = job_path(home, job_id)
        return public(refresh(path, read(path)))


def listing(limit=20):
    with locked() as home:
        records = [public(refresh(path, read(path))) for path in home.glob("*.json")]
        return {"ok": True, "jobs": sorted(records, key=lambda record: record["createdAt"], reverse=True)[:limit]}


def run(job_id):
    with locked() as home:
        path = job_path(home, job_id)
        record = read(path)
        if record["state"] != "queued":
            raise ValueError("VM job was already started.")
        record.update(state="running", pid=os.getpid())
        write(path, record)
    request = record["request"]
    if record.get("kind") in ("snapshot-create", "snapshot-restore", "snapshot-recover", "clone"):
        import vm_snapshots
        try:
            kind = record["kind"]
            if kind == "snapshot-create":
                result = vm_snapshots.create(request["name"], request["label"], request["vmID"])
            elif kind == "clone":
                result = vm_snapshots.clone(request["name"], request["newName"], request.get("snapshotID"), request["vmID"])
            elif kind == "snapshot-restore":
                result = vm_snapshots.restore(request["name"], request["snapshotID"], request["vmID"])
            else:
                result = vm_snapshots.recover(request["name"], request["vmID"])
            record.update(state="completed", result=result)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            record.update(state="failed", error=str(error))
        with locked():
            write(path, record)
        return
    if record.get("kind", "create") == "eject":
        import service
        try:
            changed = service.change_vm(Registry(), request["name"], eject=True, expected_id=request["vmID"])
            record.update(state="completed", result={"ok": True, "name": request["name"], "vmID": request["vmID"], "changed": changed})
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            record.update(state="failed", error=str(error))
        with locked():
            write(path, record)
        return
    args = [sys.executable, str(ROOT / "bin/virty"), "create", request["name"], "--os", request["guestOS"]]
    for key, flag in (("image", "--image"), ("disk", "--disk"), ("cpus", "--cpus"), ("memoryGiB", "--memory-gib"), ("diskGiB", "--disk-gib")):
        if key in request:
            args += [flag, str(request[key])]
    if not request.get("autostart", True):
        args.append("--no-autostart")
    if request.get("setupAssistant"):
        args.append("--setup-assistant")
    log = home / (job_id + ".log")
    try:
        fd = os.open(log, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as output:
            completed = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=output, stderr=output)
        # Build/installer progress goes only to the private local log. Last line is CLI JSON.
        with log.open("rb") as output:
            output.seek(max(0, log.stat().st_size - 65536))
            lines = output.read().splitlines()
        result = json.loads(lines[-1]) if lines else {}
        if completed.returncode or not result.get("ok"):
            raise ValueError(result.get("error", "Creation failed; inspect the private job log. Partial runtime data may remain."))
        record.update(state="completed", result=result)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        record.update(state="failed", error=str(error))
    with locked():
        write(path, record)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--run":
        raise SystemExit("This is an internal VM job worker; use virty-mcp.")
    run(sys.argv[2])
