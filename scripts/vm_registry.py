"""Owned, atomic named-VM catalog. Contains metadata only, never credentials."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]


def private_directory(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("Runtime directory must be owned by this user with mode 0700.")


def atomic_write(path, data):
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def vm_record(home):
    home = Path(home)
    info = home.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("VM runtime must be an owned directory with mode 0700.")
    record = json.loads((home / "vm.json").read_text())
    if not isinstance(record, dict) or not isinstance(record.get("id"), str):
        raise ValueError("Invalid VM record.")
    if str(uuid.UUID(record["id"])).upper() != record["id"]:
        raise ValueError("Invalid VM UUID.")
    if record.get("guestOS", "macos") not in ("macos", "linux"):
        raise ValueError("Unsupported guest OS.")
    return record


def valid_name(name):
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", name):
        raise ValueError("VM names must start with a lowercase letter and contain 1–48 lowercase letters, digits, underscores or hyphens.")
    return name


class Registry:
    def __init__(self, directory=None):
        self.home = Path(directory or os.environ.get("VIRTY_REGISTRY_HOME", ROOT / "runtime/registry")).expanduser().resolve()
        self.path = self.home / "vms.json"

    def read(self):
        if not self.path.exists():
            return {"version": 1, "default": None, "vms": {}}
        private_directory(self.home)
        info = self.path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("VM registry must be an owned regular file with mode 0600.")
        data = json.loads(self.path.read_text())
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("vms"), dict) or "default" not in data:
            raise ValueError("Unsupported VM registry.")
        identities, homes = set(), set()
        for name, entry in data["vms"].items():
            valid_name(name)
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not isinstance(entry.get("home"), str):
                raise ValueError("Invalid registry entry.")
            if str(uuid.UUID(entry["id"])).upper() != entry["id"] or entry["guestOS"] not in ("macos", "linux"):
                raise ValueError("Invalid registry VM identity or OS.")
            if not Path(entry["home"]).is_absolute() or type(entry["enabled"]) is not bool or type(entry["autostart"]) is not bool:
                raise ValueError("Invalid registry runtime or startup policy.")
            if entry["id"] in identities or entry["home"] in homes:
                raise ValueError("Duplicate VM UUID or runtime directory.")
            identities.add(entry["id"])
            homes.add(entry["home"])
        if data["default"] is not None and (not isinstance(data["default"], str) or data["default"] not in data["vms"]):
            raise ValueError("Registry default VM is missing.")
        return data

    @contextlib.contextmanager
    def edit(self):
        private_directory(self.home)
        fd = os.open(self.home / "registry.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            data = self.read()
            yield data
            atomic_write(self.path, json.dumps(data, indent=2, sort_keys=True).encode())
        finally:
            os.close(fd)

    def entry(self, name=None):
        data = self.read()
        selected = name or data["default"]
        if selected not in data["vms"]:
            raise ValueError("Unknown VM; use virty list or register/create a named VM.")
        entry = data["vms"][selected]
        record = vm_record(entry["home"])
        if record["id"] != entry["id"] or record.get("guestOS", "macos") != entry["guestOS"]:
            raise ValueError("VM runtime identity differs from its registry entry; refusing routing.")
        return selected, entry

    @contextlib.contextmanager
    def administer(self, name=None):
        selected, entry = self.entry(name)
        fd = os.open(Path(entry["home"]) / "admin.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("Another administration operation owns this VM.") from None
            # Recheck after acquiring the lock, before any lifecycle/configuration change.
            current_name, current = self.entry(selected)
            if current != entry:
                raise ValueError("VM catalog changed; retry the administration operation.")
            yield current_name, current
        finally:
            os.close(fd)

    def register(self, name, home, autostart=True):
        valid_name(name)
        home = Path(home).expanduser().resolve()
        record = vm_record(home)
        with self.edit() as data:
            if name in data["vms"] or any(e["id"] == record["id"] or e["home"] == str(home) for e in data["vms"].values()):
                raise ValueError("VM name, UUID or runtime is already registered.")
            data["vms"][name] = {"id": record["id"], "home": str(home), "guestOS": record.get("guestOS", "macos"),
                                  "enabled": True, "autostart": autostart}
            if data["default"] is None:
                data["default"] = name
        return record["id"]

    def set_policy(self, name, **values):
        if set(values) - {"enabled", "autostart"} or any(type(v) is not bool for v in values.values()):
            raise ValueError("Invalid VM policy.")
        with self.edit() as data:
            if name not in data["vms"]:
                raise ValueError("Unknown VM.")
            data["vms"][name].update(values)

    def rename(self, name, replacement):
        valid_name(replacement)
        with self.edit() as data:
            if name not in data["vms"] or replacement in data["vms"]:
                raise ValueError("Unknown source or occupied destination VM name.")
            data["vms"][replacement] = data["vms"].pop(name)
            if data["default"] == name:
                data["default"] = replacement

    def set_default(self, name):
        with self.edit() as data:
            if name not in data["vms"]:
                raise ValueError("Unknown VM.")
            data["default"] = name

    def unregister(self, name):
        with self.edit() as data:
            if name not in data["vms"] or data["vms"][name]["enabled"]:
                raise ValueError("Disable the VM before unregistering it.")
            del data["vms"][name]
            if data["default"] == name:
                data["default"] = next(iter(sorted(data["vms"])), None)
