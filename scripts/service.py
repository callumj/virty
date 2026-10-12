#!/usr/bin/env python3
"""Aqua LaunchAgent installer/supervisor. It never reads the guest's secrets."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import runpy
import signal
import stat
import subprocess
import sys
import threading
import time
sys.path.insert(0, str(Path(__file__).resolve().parent))
from vm_registry import Registry

ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.virty.manager"
STATUS_TIMEOUT = 5
LIFECYCLE_TIMEOUT = 3
STARTUP_TIMEOUT = 30
LAUNCHCTL = "/bin/launchctl"
call = runpy.run_path(str(ROOT / "bin/virty"))["call"]


def note(message):
    print(message, flush=True)


def record_for(home):
    if (home / "maintenance.json").exists():
        raise ValueError("Snapshot maintenance is pending; use virty snapshot recover before starting this VM.")
    attributes = home.stat()
    if attributes.st_uid != os.getuid() or stat.S_IMODE(attributes.st_mode) != 0o700:
        raise ValueError("VM runtime directory must belong to this user and have mode 0700.")
    record = json.loads((home / "vm.json").read_text())
    if not record.get("installed"):
        raise ValueError("Restore the guest before installing its service.")
    executable = Path(record["managerExecutable"])
    if not executable.is_absolute() or not os.access(executable, os.X_OK):
        raise ValueError("The pinned manager executable is missing; preserve its enrolled .build directory.")
    return record


def request(home, command, timeout=STATUS_TIMEOUT, **values):
    return call({"command": command, **values}, home, timeout=timeout)


def manager_status(home, timeout=STATUS_TIMEOUT):
    try:
        result = request(home, "status", timeout=timeout)
    except (OSError, ValueError):
        return None
    return result if result.get("ok") else None


def atomic_write(path, data):
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(data)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def job_definition(home, fleet=False):
    return {
        "Label": LABEL,
        "ProgramArguments": ["/usr/bin/python3", str(Path(__file__).resolve()),
                             "run-fleet" if fleet else "run", "--registry" if fleet else "--home", str(home)],
        "WorkingDirectory": str(ROOT),
        "EnvironmentVariables": {"VIRTY_BROKER_HOME": os.environ.get("VIRTY_BROKER_HOME", str(ROOT / "runtime/secrets")),
                                 "PYTHONUNBUFFERED": "1", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        "LimitLoadToSessionType": "Aqua",
        "AssociatedBundleIdentifiers": [LABEL],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 15,
        "ExitTimeOut": 30,
        "ProcessType": "Interactive",
        "Umask": 0o077,
        "StandardOutPath": str(home / "service.stdout.log"),
        "StandardErrorPath": str(home / "service.stderr.log"),
    }


def service_target():
    return f"gui/{os.getuid()}/{LABEL}"


def launchctl(*arguments, check=True):
    result = subprocess.run([LAUNCHCTL, *arguments], capture_output=True, text=True)
    if check and result.returncode:
        raise ValueError(f"launchctl {' '.join(arguments)} failed: {result.stderr.strip()}")
    return result


def job_status():
    result = launchctl("print", service_target(), check=False)
    if result.returncode:
        return {"loaded": False}
    value = {"loaded": True}
    for field in ("state", "pid", "runs", "last exit code"):
        match = re.search(r"^\s*" + re.escape(field) + r" = (.+)$", result.stdout, re.MULTILINE)
        if match:
            value[field.replace(" ", "_")] = int(match[1]) if match[1].lstrip("-").isdigit() else match[1]
    return value


def alive(pid):
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def shutdown_manager(home, expected_id, child=None):
    """Cancel delivery, stop this guest, and let its owner exit normally."""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status = manager_status(home, timeout=min(STATUS_TIMEOUT, deadline - time.monotonic()))
        if status is None:
            if child is None or child.poll() is not None:
                return
            time.sleep(0.1)
            continue
        if status.get("vmID") != expected_id:
            raise ValueError("Refusing to stop a manager for a different VM.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if status.get("inputBusy"):
            request(home, "cancel", timeout=min(STATUS_TIMEOUT, remaining))
        elif status["state"] in ("stopped", "error"):
            result = request(home, "quit", timeout=min(STATUS_TIMEOUT, remaining))
            if not result.get("ok"):
                raise ValueError("Manager refused to quit.")
        elif status["state"] == "running":
            result = request(home, "stop", timeout=min(STATUS_TIMEOUT, remaining), force=True)
            if not result.get("ok"):
                raise ValueError("Manager refused to stop the guest.")
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))
    if child is None:
        raise ValueError("Existing manager did not stop; no process was killed.")
    raise ValueError("Managed child did not finish its shutdown.")


def run_worker(home):
    os.umask(0o077)
    record = record_for(home)
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    with open(home / "service.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if manager_status(home) is not None:
            raise ValueError("A manager already owns the VM. Use service.py install for a controlled takeover.")
        child = subprocess.Popen([record["managerExecutable"], "serve"],
                                 env={**os.environ, "VIRTY_HOME": str(home),
                                      "VIRTY_BROKER_HOME": os.environ.get("VIRTY_BROKER_HOME", str(ROOT / "runtime/secrets"))})
        ready = False
        missed_checks = 0
        start_attempted = False
        try:
            atomic_write(home / "service.json", json.dumps({"supervisorPID": os.getpid(), "managerPID": child.pid,
                                                          "managerExecutable": record["managerExecutable"], "vmID": record["id"], "ready": False}).encode())
            note(f"Manager launched (PID {child.pid}); console starts hidden.")
            deadline = time.monotonic() + STARTUP_TIMEOUT
            while not stopping.is_set():
                if child.poll() is not None:
                    note(f"Manager exited (code {child.returncode}); launchd will recover the job.")
                    return 1
                remaining = deadline - time.monotonic()
                if not ready and remaining <= 0:
                    raise ValueError(f"Manager did not become ready within {STARTUP_TIMEOUT} seconds.")
                # Startup waits must also leave room for the 15-second shutdown
                # phase and bounded child termination within the fleet's 25 seconds.
                status = manager_status(home, timeout=STATUS_TIMEOUT if ready else min(LIFECYCLE_TIMEOUT, remaining))
                if stopping.is_set():
                    break
                if status is not None:
                    missed_checks = 0
                    if status.get("vmID") != record["id"]:
                        raise ValueError("Unexpected VM on the control socket.")
                    if not ready:
                        autostart = os.environ.get("VIRTY_AUTOSTART", "1") == "1" and not (home / "snapshot-policy.json").exists()
                        if status["state"] == "error":
                            raise ValueError("Guest could not initialize.")
                        if status["state"] == "stopped" and autostart and not start_attempted:
                            # A lost reply does not prove start failed. Reserve one
                            # attempt, then reconcile state within the same deadline.
                            start_attempted = True
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise ValueError("Guest could not initialize before the startup deadline.")
                            if stopping.is_set():
                                break
                            try:
                                result = request(home, "start", timeout=min(LIFECYCLE_TIMEOUT, remaining))
                            except (OSError, ValueError):
                                note("Startup reply unavailable; checking state without retrying start.")
                            else:
                                if not result.get("ok"):
                                    raise ValueError("Guest startup failed; check the manager log.")
                            stopping.wait(0.1)
                            continue
                        if status["state"] != "running" and not (status["state"] == "stopped" and not autostart and not start_attempted):
                            stopping.wait(0.1)
                            continue
                        ready = True
                        atomic_write(home / "service.json", json.dumps({"supervisorPID": os.getpid(), "managerPID": child.pid,
                                     "managerExecutable": record["managerExecutable"], "vmID": record["id"], "ready": True}).encode())
                        note("Worker ready. CLI stop leaves the guest stopped until the next start or worker restart.")
                    elif status["state"] == "error":
                        raise ValueError("Guest entered an error state; recovering the manager.")
                else:
                    missed_checks += 1
                    if not ready and time.monotonic() > deadline:
                        raise ValueError(f"Manager did not become ready within {STARTUP_TIMEOUT} seconds.")
                    if ready and missed_checks >= 3:
                        raise ValueError("Manager stopped responding; recovering the job.")
                stopping.wait(1)
            return 0
        finally:
            if child.poll() is None:
                note("Stopping this service's manager and disposable guest.")
                try:
                    shutdown_manager(home, record["id"], child)
                except (OSError, ValueError):
                    note("Controlled shutdown unavailable; terminating this service's child.")
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=3)


def fleet_job(path, registry):
    if path.is_symlink():
        raise ValueError("Refusing a symlinked LaunchAgent.")
    if not path.exists():
        return None
    info = path.stat()
    job = plistlib.loads(path.read_bytes())
    arguments = job.get("ProgramArguments", [])
    prefix = ["/usr/bin/python3", str(Path(__file__).resolve())]
    if info.st_uid != os.getuid() or job.get("Label") != LABEL or arguments[:2] != prefix:
        raise ValueError("Existing LaunchAgent belongs to another installation; refusing takeover.")
    if arguments == job_definition(registry.home, fleet=True)["ProgramArguments"]:
        return "fleet"
    if len(arguments) == 5 and arguments[2:4] == ["run", "--home"]:
        return Path(arguments[4])
    raise ValueError("Existing LaunchAgent uses a different registry or unknown configuration.")


def run_fleet(registry):
    from vm_registry import private_directory
    private_directory(registry.home)
    os.umask(0o077)
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    workers, retry_at = {}, {}
    fd = os.open(registry.home / "service.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while not stopping.is_set():
            try:
                catalog = registry.read()
                wanted = {entry["id"]: (name, entry) for name, entry in catalog["vms"].items() if entry["enabled"]}
            except (OSError, ValueError, KeyError) as error:
                # A damaged catalog must not stop healthy guests or redirect commands.
                note(f"Registry unavailable; existing workers retained: {error}")
                stopping.wait(5)
                continue
            for vm_id, worker in list(workers.items()):
                if vm_id not in wanted and worker["child"].poll() is None and not worker["stopping"]:
                    worker["child"].terminate()
                    worker["stopping"] = True
                if worker["child"].poll() is not None:
                    note(f"Worker {vm_id} exited ({worker['child'].returncode}).")
                    del workers[vm_id]
                    retry_at[vm_id] = time.monotonic() + (15 if vm_id in wanted else 0)
            for vm_id, (name, entry) in wanted.items():
                if vm_id in workers or time.monotonic() < retry_at.get(vm_id, 0):
                    continue
                try:
                    if registry.entry(name)[1] != entry:
                        continue
                    home = Path(entry["home"])
                    record_for(home)
                    descriptors = [os.open(home / log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
                                   for log in ("service.stdout.log", "service.stderr.log")]
                    try:
                        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "run", "--home", str(home)],
                                                 env={**os.environ, "VIRTY_VM_NAME": name, "VIRTY_AUTOSTART": "1" if entry["autostart"] else "0"},
                                                 stdout=descriptors[0], stderr=descriptors[1])
                    finally:
                        for descriptor in descriptors:
                            os.close(descriptor)
                    workers[vm_id] = {"child": child, "home": str(home), "stopping": False}
                    note(f"Worker {name} launched (PID {child.pid}).")
                except (OSError, ValueError, KeyError) as error:
                    note(f"Cannot launch {name}: {error}")
                    retry_at[vm_id] = time.monotonic() + 15
            atomic_write(registry.home / "service.json", json.dumps({"supervisorPID": os.getpid(),
                         "workers": {key: {"pid": value["child"].pid, "home": value["home"]} for key, value in workers.items()}}).encode())
            stopping.wait(0.5)
        return 0
    finally:
        # Signal all workers first so guests shut down independently in parallel.
        for worker in workers.values():
            if worker["child"].poll() is None:
                worker["child"].terminate()
        for worker in workers.values():
            try:
                worker["child"].wait(timeout=25)
            except subprocess.TimeoutExpired:
                raise ValueError("A VM worker did not finish shutdown; inspect its runtime logs.")
        os.close(fd)


def stop_fleet(registry, legacy=None):
    previous = job_status()
    pids = [previous.get("pid")]
    entries = list(registry.read()["vms"].values())
    if isinstance(legacy, Path):
        entries.append({"home": str(legacy)})
    for entry in entries:
        metadata = Path(entry["home"]) / "service.json"
        if metadata.exists():
            owner = json.loads(metadata.read_text())
            pids.extend([owner.get("managerPID"), owner.get("supervisorPID")])
    launchctl("disable", service_target())
    if previous["loaded"]:
        launchctl("bootout", service_target())
        deadline = time.monotonic() + 30
        while any(alive(pid) for pid in pids):
            if time.monotonic() > deadline:
                raise ValueError("Service unloaded but workers have not exited.")
            time.sleep(0.1)


def wait_vm_ready(entry):
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        status = manager_status(Path(entry["home"]))
        if status and status.get("vmID") == entry["id"] and status["state"] in (("running",) if entry["autostart"] else ("running", "stopped")):
            metadata = Path(entry["home"]) / "service.json"
            if metadata.exists() and json.loads(metadata.read_bytes()).get("ready"):
                return
        time.sleep(0.2)
    raise ValueError("VM did not become ready; inspect its service logs.")


def start_fleet(path, registry):
    if fleet_job(path, registry) != "fleet":
        raise ValueError("Install the named-VM service first.")
    launchctl("enable", service_target())
    if not job_status()["loaded"]:
        launchctl("bootstrap", f"gui/{os.getuid()}", str(path))
    launchctl("kickstart", service_target())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        metadata = registry.home / "service.json"
        if metadata.exists() and json.loads(metadata.read_text()).get("supervisorPID") == job_status().get("pid"):
            break
        time.sleep(0.1)
    else:
        raise ValueError("Fleet supervisor did not become ready.")
    entries = [(name, entry) for name, entry in registry.read()["vms"].items() if entry["enabled"]]
    failures = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        checks = [(name, pool.submit(wait_vm_ready, entry)) for name, entry in entries]
        for name, check in checks:
            try:
                check.result()
            except (OSError, ValueError) as error:
                failures.append(f"{name}: {error}")
    if failures:
        raise ValueError("Fleet remains available; some VMs could not start: " + "; ".join(failures))


def change_vm(registry, name, eject=False, expected_id=None):
    with registry.administer(name) as (selected, entry):
        if expected_id is not None and entry["id"] != expected_id:
            raise ValueError("VM identity changed; refresh the VM list before retrying.")
        return change_vm_locked(registry, selected, entry, eject)


def change_vm_locked(registry, name, entry, eject):
    home = Path(entry["home"])
    record = record_for(home)
    if record.get("keychain") != "broker":
        raise ValueError("Manager changes require broker-owned credentials; preserve the legacy owner.")
    if eject:
        if record.get("guestOS") != "linux" or not record.get("installMedia"):
            raise ValueError("This VM has no Linux installer media attached.")
        record.pop("installMedia")
    else:
        executable = (ROOT / ".build/Virty.app/Contents/MacOS/virty-manager").resolve(strict=True)
        result = subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(executable)], capture_output=True)
        if result.returncode:
            raise ValueError("New manager signature verification failed.")
        if str(executable) == record["managerExecutable"]:
            return False
        record["managerExecutable"] = str(executable)
    previous = (home / "vm.json").read_bytes()
    was_enabled = entry["enabled"]
    registry.set_policy(name, enabled=False)
    wait_disabled = runpy.run_path(str(ROOT / "bin/virty"))["wait_disabled"]
    try:
        wait_disabled(entry)
        atomic_write(home / "vm.json", json.dumps(record, indent=2, sort_keys=True).encode())
        registry.set_policy(name, enabled=was_enabled)
        if was_enabled and job_status()["loaded"]:
            wait_vm_ready(entry)
    except (OSError, ValueError, subprocess.SubprocessError):
        registry.set_policy(name, enabled=False)
        wait_disabled(entry)
        atomic_write(home / "vm.json", previous)
        registry.set_policy(name, enabled=was_enabled)
        if was_enabled and job_status()["loaded"]:
            wait_vm_ready(entry)
        raise ValueError("VM change failed; previous configuration restored.") from None
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["render", "install", "start", "stop", "restart", "upgrade", "eject", "status", "uninstall", "run", "run-fleet"])
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--home", type=Path, help="Legacy runtime to adopt, or standalone worker runtime")
    selector.add_argument("--vm", help="Named VM for upgrade/eject (default: registry default)")
    parser.add_argument("--registry", type=Path, help="Registry directory")
    args = parser.parse_args()
    registry = Registry(args.registry)
    path = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
    try:
        if args.command == "run":
            home = (args.home or Path(os.environ.get("VIRTY_HOME", ROOT / "runtime/vm"))).expanduser().resolve()
            return run_worker(home)
        if args.command == "run-fleet":
            return run_fleet(registry)
        if args.command == "render":
            from vm_registry import private_directory
            private_directory(registry.home)
            output = registry.home / "launchd.plist"
            atomic_write(output, plistlib.dumps(job_definition(registry.home, fleet=True)))
            note(f"Rendered {output}")
            return 0
        if sys.platform != "darwin":
            raise ValueError("The service requires a macOS GUI login session.")
        installed = fleet_job(path, registry)
        if args.command == "status":
            print(json.dumps({"installed": bool(installed), "mode": "legacy" if isinstance(installed, Path) else installed,
                              "target": service_target(), "launchd": job_status(), "registry": str(registry.path),
                              "vms": runpy.run_path(str(ROOT / "bin/virty"))["list_vms"](registry.home)["vms"]}, sort_keys=True))
            return 0
        if args.command == "install":
            from vm_registry import private_directory
            private_directory(registry.home)
            home = args.home or (installed if isinstance(installed, Path) else ROOT / "runtime/vm")
            home = home.expanduser().resolve()
            if (home / "vm.json").exists() and not any(e["home"] == str(home) for e in registry.read()["vms"].values()):
                record = record_for(home)
                name = record.get("guestOS", "macos")
                if name in registry.read()["vms"]:
                    name += "-" + record["id"][:8].lower()
                registry.register(name, home)
            path.parent.mkdir(parents=True, exist_ok=True)
            if job_status()["loaded"]:
                if not installed:
                    raise ValueError("The launchd label is occupied without this service's installed plist; refusing takeover.")
                stop_fleet(registry, installed)
            for entry in registry.read()["vms"].values():
                record_for(Path(entry["home"]))
                shutdown_manager(Path(entry["home"]), entry["id"])
            for log in ("service.stdout.log", "service.stderr.log"):
                descriptor = os.open(registry.home / log, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                os.close(descriptor)
            atomic_write(path, plistlib.dumps(job_definition(registry.home, fleet=True)))
            start_fleet(path, registry)
            note(f"Installed and started {service_target()} for named VMs.")
        elif not installed:
            raise ValueError("The Virty service is not installed.")
        elif args.command == "start":
            start_fleet(path, registry)
            note("Service started.")
        elif args.command in ("stop", "uninstall"):
            stop_fleet(registry, installed)
            if args.command == "uninstall":
                path.unlink()
            note("Service stopped" + (" and uninstalled." if args.command == "uninstall" else "; automatic startup disabled until start."))
        elif args.command == "restart":
            stop_fleet(registry, installed)
            start_fleet(path, registry)
            note("Service restarted.")
        elif args.command in ("upgrade", "eject"):
            name = args.vm
            if args.home:
                matches = [n for n, e in registry.read()["vms"].items() if e["home"] == str(args.home.expanduser().resolve())]
                if not matches:
                    raise ValueError("Register this runtime before changing its manager.")
                name = matches[0]
            changed = change_vm(registry, name, eject=args.command == "eject")
            note("VM updated; sibling VMs and broker owner retained." if changed else "Manager already uses the latest build.")
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"Service error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
