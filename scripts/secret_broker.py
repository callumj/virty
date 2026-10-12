"""Local administration only; secret enrollment/export are absent from VM tools."""
import argparse
import base64
from contextlib import contextmanager
import fcntl
import getpass
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
LIMIT = 8 * 1024 * 1024


def private_directory(home):
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = home.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("Broker directory must belong to this user and have mode 0700.")


@contextmanager
def ownership(home):
    private_directory(home)
    descriptor = os.open(home / "broker.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def atomic_json(path, value):
    descriptor, temporary = tempfile.mkstemp(prefix=".broker-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as file:
            json.dump(value, file, indent=2, sort_keys=True)
            file.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def validate_metadata(record):
    if record.get("version") != 1 or str(uuid.UUID(record["instance"])).upper() != record["instance"]:
        raise ValueError("Invalid broker owner record.")
    executable = Path(record["executable"])
    if not executable.is_absolute() or len(record["sha256"]) != 64 or any(c not in "0123456789abcdef" for c in record["sha256"]):
        raise ValueError("Invalid broker executable metadata.")
    return record


def validate_owner(record):
    validate_metadata(record)
    executable = Path(record["executable"])
    info = executable.lstat()
    if not executable.is_absolute() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError("Broker executable must be an owned immutable file without group/other write permission.")
    if hashlib.sha256(executable.read_bytes()).hexdigest() != record["sha256"]:
        raise ValueError("Pinned broker executable changed; preserve the enrolled build.")
    if not os.access(executable, os.X_OK):
        raise ValueError("Pinned broker executable is not executable.")
    return record


def read_owner(home):
    return read_record(home / "broker.json")


def read_record(path, verify_binary=True):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("Broker owner record must be an owned regular file with mode 0600.")
    record = json.loads(path.read_text())
    return validate_owner(record) if verify_binary else validate_metadata(record)


def candidate(executable=None):
    path = Path(executable or ROOT / ".build/virty-secret-broker").expanduser().resolve(strict=True)
    return validate_owner({"version": 1, "instance": str(uuid.uuid4()).upper(),
                           "executable": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})


def request(owner, operation, **values):
    validate_owner(owner)
    body = json.dumps({"operation": operation, "instance": owner["instance"], **values}).encode()
    if len(body) > LIMIT * 2:
        raise ValueError("Broker request exceeds its size bound.")
    result = subprocess.run([owner["executable"]], input=body, capture_output=True, timeout=30)
    if len(result.stdout) > LIMIT * 2:
        raise ValueError("Broker response exceeds its size bound.")
    try:
        response = json.loads(result.stdout)
    except ValueError:
        raise ValueError("Broker exited without a valid response; preserve its owner record.") from None
    if result.returncode or not response.get("ok"):
        # The signed implementation supplies fixed errors without secret content.
        raise ValueError(response.get("error", "Broker failed without an error status."))
    return response


def remember(home, owner):
    history = home / "history"
    private_directory(history)
    atomic_json(history / (owner["instance"] + ".json"), owner)


def publish(home, old, new):
    if old:
        remember(home, old)
    atomic_json(home / "broker.json", new)


def initialize(home, executable=None):
    with ownership(home):
        if (home / "broker.json").exists():
            owner = read_owner(home)
            return {"initialized": False, "instance": owner["instance"]}
        owner = candidate(executable)
        request(owner, "verify")
        publish(home, None, owner)
        return {"initialized": True, "instance": owner["instance"]}


def restore_archive(home, archive, passphrase, executable=None):
    """Caller owns the administration lock. Switch only after verified restore."""
    old = read_record(home / "broker.json", verify_binary=False) if (home / "broker.json").exists() else None
    new = candidate(executable)
    # Save recoverable, nonsecret metadata before starting a possibly interrupted import.
    pending = home / "pending"
    private_directory(pending)
    pending_record = pending / (new["instance"] + ".json")
    atomic_json(pending_record, new)
    result = request(new, "restore", archive=archive, passphrase=passphrase)
    publish(home, old, new)
    pending_record.unlink()
    return {"restored": result["restored"], "instance": new["instance"],
            "previousInstance": old["instance"] if old else None}


def upgrade(home, executable=None):
    with ownership(home):
        old = read_owner(home)
        new = candidate(executable)
        if new["sha256"] == old["sha256"]:
            return {"upgraded": False, "instance": old["instance"]}
        # Anonymous pipes carry the temporary wrapping key. No plaintext archive/key file.
        passphrase = secrets.token_hex(32)
        archive = request(old, "export", passphrase=passphrase)["archive"]
        result = restore_archive(home, archive, passphrase, new["executable"])
        return {"upgraded": True, **result}


def passphrase_from(args, confirm=False):
    if args.passphrase_fd is not None:
        with os.fdopen(os.dup(args.passphrase_fd), "rb") as file:
            value = file.read(4098)
        value = value.removesuffix(b"\n")
        if len(value) > 4096:
            raise ValueError("Passphrase input exceeds its size bound.")
        return value.decode()
    if not sys.stdin.isatty():
        raise ValueError("Use a terminal prompt or --passphrase-fd; never put a passphrase in arguments.")
    value = getpass.getpass("Archive passphrase (at least 12 UTF-8 bytes): ")
    if confirm and value != getpass.getpass("Confirm archive passphrase: "):
        raise ValueError("Archive passphrases did not match.")
    return value


def write_archive(path, encoded):
    data = base64.b64decode(encoded, validate=True)
    if len(data) > LIMIT:
        raise ValueError("Archive exceeds its size bound.")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(data)
    except BaseException:
        Path(path).unlink(missing_ok=True)
        raise


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path(os.environ.get("VIRTY_BROKER_HOME", ROOT / "runtime/secrets")))
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "upgrade"):
        commands.add_parser(name).add_argument("--binary", type=Path)
    commands.add_parser("status")
    rollback = commands.add_parser("rollback")
    rollback.add_argument("--instance", required=True)
    for name in ("generate", "enroll", "remove"):
        sub = commands.add_parser(name)
        sub.add_argument("--vm", required=True)
        sub.add_argument("--alias", required=True)
    for name in ("export", "restore"):
        sub = commands.add_parser(name)
        sub.add_argument("--output" if name == "export" else "--input", type=Path, required=True)
        sub.add_argument("--passphrase-fd", type=int)
        if name == "restore":
            sub.add_argument("--binary", type=Path)
    args = parser.parse_args()
    home = args.home.expanduser().absolute()
    try:
        if args.command == "init":
            result = initialize(home, args.binary)
        elif args.command == "upgrade":
            result = upgrade(home, args.binary)
        else:
            passphrase = passphrase_from(args, confirm=args.command == "export") if args.command in ("export", "restore") else None
            value = None
            if args.command == "enroll":
                if not sys.stdin.isatty():
                    raise ValueError("Secret enrollment requires the local terminal's hidden prompt.")
                value = getpass.getpass("Secret value: ")
            with ownership(home):
                owner = read_owner(home) if args.command != "restore" and (home / "broker.json").exists() else None
                if args.command == "restore":
                    with args.input.open("rb") as file:
                        data = file.read(LIMIT + 1)
                    if len(data) > LIMIT:
                        raise ValueError("Archive exceeds its size bound.")
                    result = restore_archive(home, base64.b64encode(data).decode(), passphrase, args.binary)
                elif owner is None:
                    raise ValueError("Initialize the broker first.")
                elif args.command == "status":
                    result = {"owner": owner, **request(owner, "list")}
                elif args.command == "rollback":
                    instance = str(uuid.UUID(args.instance)).upper()
                    previous = read_record(home / "history" / (instance + ".json"))
                    if previous["instance"] != instance:
                        raise ValueError("History owner does not match its record name.")
                    request(previous, "verify")
                    publish(home, owner, previous)
                    result = {"instance": previous["instance"], "rolledBack": True}
                elif args.command == "export":
                    archive = request(owner, "export", passphrase=passphrase)["archive"]
                    write_archive(args.output, archive)
                    result = {"exported": True, "output": str(args.output)}
                else:
                    result = request(owner, "put" if args.command == "enroll" else args.command,
                                     vmID=str(uuid.UUID(args.vm)).upper(), alias=args.alias,
                                     **({"value": value} if value is not None else {}))
        print(json.dumps({"ok": True, **result}, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        # Arbitrary decoder/OS exception strings can contain input; report a fixed error.
        print(json.dumps({"ok": False, "error": "Broker administration failed. Inspect the current owner before retrying; check paths, credentials, and archive compatibility."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
