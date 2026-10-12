import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from vm_registry import Registry
import service

FAKE = r'''
import json, os, socket
from pathlib import Path
home = Path(os.environ["VIRTY_HOME"])
vm_id = json.loads((home / "vm.json").read_text())["id"]
address = home / "control.sock"
address.unlink(missing_ok=True)
listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
listener.bind(str(address))
listener.listen(8)
state = "stopped"
while True:
    peer, _ = listener.accept()
    with peer:
        message = json.loads(peer.makefile("rb").readline())
        command = message["command"]
        result = {"ok": True}
        if message.get("vmID", vm_id) != vm_id:
            result = {"ok": False, "error": "VM identity mismatch"}
        elif command == "status":
            result.update(vmID=vm_id, state=state, inputBusy=False)
        elif command == "start":
            state = "running"
        elif command == "stop":
            state = "stopped"
        elif command == "type-text":
            (home / "received.txt").write_text(message["text"])
        peer.sendall(json.dumps(result).encode() + b"\n")
        if command == "quit":
            break
'''


def make_vm(root, index, guest="macos"):
    home = root / ("vm" + str(index))
    home.mkdir(mode=0o700)
    owner = home / "manager"
    owner.write_text("#!" + sys.executable + "\n" + FAKE)
    owner.chmod(0o700)
    record = {"id": f"00000000-0000-0000-0000-{index:012d}", "guestOS": guest, "keychain": "broker",
              "installed": True, "managerExecutable": str(owner)}
    (home / "vm.json").write_text(json.dumps(record))
    return home, record


class RegistryTests(unittest.TestCase):
    def test_names_identity_default_and_retained_runtime(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            registry = Registry(root / "catalog")
            first, record = make_vm(root, 1)
            second, _ = make_vm(root, 2, "linux")
            registry.register("mac", first)
            registry.register("linux", second, False)
            with registry.administer("mac"):
                with self.assertRaisesRegex(ValueError, "administration operation"):
                    with registry.administer("mac"):
                        pass
                with registry.administer("linux"):
                    pass
            with self.assertRaises(ValueError):
                registry.register("duplicate", first)
            with self.assertRaises(ValueError):
                registry.register("../escape", second)
            registry.rename("mac", "desktop")
            self.assertEqual(registry.entry()[0], "desktop")
            self.assertEqual(registry.entry()[1]["id"], record["id"])
            registry.set_default("linux")
            with self.assertRaises(ValueError):
                registry.unregister("linux")
            registry.set_policy("linux", enabled=False)
            registry.unregister("linux")
            self.assertTrue((second / "vm.json").exists())
            self.assertEqual(registry.entry()[0], "desktop")
            self.assertEqual(registry.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(registry.home.stat().st_mode & 0o777, 0o700)
            record["id"] = "00000000-0000-0000-0000-000000000003"
            (first / "vm.json").write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError, "identity"):
                registry.entry("desktop")

    def test_rejects_ambiguous_default_and_nonprivate_catalog(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            registry = Registry(root / "catalog")
            home, _ = make_vm(root, 1)
            registry.register("mac", home)
            with registry.edit() as data:
                data["default"] = "missing"
            with self.assertRaisesRegex(ValueError, "default"):
                registry.read()
            registry.path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "0600"):
                registry.read()

    def test_failed_named_upgrade_restores_only_selected_vm(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            registry = Registry(root / "catalog")
            first, _ = make_vm(root, 1)
            second, _ = make_vm(root, 2, "linux")
            registry.register("mac", first)
            registry.register("linux", second)
            original = (first / "vm.json").read_bytes()
            sibling = (second / "vm.json").read_bytes()
            latest = root / ".build/Virty.app/Contents/MacOS/virty-manager"
            latest.parent.mkdir(parents=True)
            latest.touch(mode=0o700)
            with patch.object(service, "ROOT", root), patch.object(service.subprocess, "run", return_value=Mock(returncode=0)), \
                    patch.object(service.runpy, "run_path", return_value={"wait_disabled": Mock()}), \
                    patch.object(service, "job_status", return_value={"loaded": True}), \
                    patch.object(service, "wait_vm_ready", side_effect=[ValueError("Startup failed"), None]):
                with self.assertRaisesRegex(ValueError, "previous configuration restored"):
                    service.change_vm(registry, "mac")
            self.assertEqual((first / "vm.json").read_bytes(), original)
            self.assertEqual((second / "vm.json").read_bytes(), sibling)
            self.assertTrue(registry.entry("mac")[1]["enabled"])
            self.assertTrue(registry.entry("linux")[1]["enabled"])


    def test_eject_retains_disk_and_sibling_configuration(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            registry = Registry(root / "catalog")
            first, record = make_vm(root, 1, "linux")
            second, _ = make_vm(root, 2)
            record["installMedia"] = "/test/installer.iso"
            (first / "vm.json").write_text(json.dumps(record))
            (first / "disk.img").write_bytes(b"retained-disk")
            registry.register("linux", first)
            registry.register("mac", second)
            sibling = (second / "vm.json").read_bytes()
            with patch.object(service.runpy, "run_path", return_value={"wait_disabled": Mock()}), \
                    patch.object(service, "job_status", return_value={"loaded": False}):
                self.assertTrue(service.change_vm(registry, "linux", eject=True))
            self.assertNotIn("installMedia", json.loads((first / "vm.json").read_text()))
            self.assertEqual((first / "disk.img").read_bytes(), b"retained-disk")
            self.assertEqual((second / "vm.json").read_bytes(), sibling)
            self.assertTrue(registry.entry("linux")[1]["enabled"])


class FleetTests(unittest.TestCase):
    def test_concurrent_named_routing_stop_recovery_and_disable(self):
        with tempfile.TemporaryDirectory(prefix="vf-", dir="/tmp") as directory:
            root = Path(directory)
            registry = Registry(root / "catalog")
            mac, mac_record = make_vm(root, 1)
            linux, _ = make_vm(root, 2, "linux")
            registry.register("mac", mac)
            registry.register("linux", linux, autostart=False)
            env = {**os.environ, "VIRTY_REGISTRY_HOME": str(registry.home)}
            env.pop("VIRTY_HOME", None)
            fleet = subprocess.Popen([sys.executable, str(ROOT / "scripts/service.py"), "run-fleet", "--registry", str(registry.home)],
                                     env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            def wait_for(predicate, timeout=10):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if predicate():
                        return
                    time.sleep(0.05)
                self.fail("Fleet did not reach expected state")
            def status(home):
                return service.manager_status(home) or {}
            def cli(*arguments):
                result = subprocess.run([sys.executable, str(ROOT / "bin/virty"), *arguments], env=env,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return json.loads(result.stdout)
            try:
                wait_for(lambda: status(mac).get("state") == "running" and status(linux).get("state") == "stopped")
                linux_pid = json.loads((linux / "service.json").read_text())["managerPID"]
                cli("--vm", "linux", "start")
                cli("--vm", "linux", "type-text", "--text", "linux-only")
                cli("--vm", "mac", "type-text", "--text", "mac-only")
                self.assertEqual((linux / "received.txt").read_text(), "linux-only")
                self.assertEqual((mac / "received.txt").read_text(), "mac-only")
                self.assertEqual(len(cli("list")["vms"]), 2)
                cli("--vm", "mac", "stop", "--force")
                self.assertEqual(status(linux)["state"], "running")
                old_pid = json.loads((mac / "service.json").read_text())["managerPID"]
                os.kill(old_pid, signal.SIGKILL)
                wait_for(lambda: status(mac).get("state") == "running", timeout=22)
                self.assertNotEqual(json.loads((mac / "service.json").read_text())["managerPID"], old_pid)
                self.assertEqual(json.loads((linux / "service.json").read_text())["managerPID"], linux_pid)
                catalog = registry.path.read_bytes()
                registry.path.write_text("[]")
                time.sleep(0.8)
                self.assertEqual(status(mac)["state"], "running")
                self.assertEqual(status(linux)["state"], "running")
                registry.path.write_bytes(catalog)
                cli("rename", "linux", "server")
                self.assertEqual(cli("--vm", "server", "status")["state"], "running")
                cli("disable", "mac")
                self.assertEqual(status(linux)["state"], "running")
                cli("unregister", "mac")
                self.assertTrue((mac / "vm.json").exists())
                self.assertEqual(cli("list")["vms"][0]["name"], "server")
                fleet.terminate()
                output, error = fleet.communicate(timeout=15)
                self.assertEqual(fleet.returncode, 0, output + error)
                self.assertFalse(service.alive(linux_pid))
            finally:
                if fleet.poll() is None:
                    fleet.terminate()
                    fleet.communicate(timeout=30)


if __name__ == "__main__":
    unittest.main()
