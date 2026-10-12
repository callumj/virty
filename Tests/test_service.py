import importlib.util
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("virty_service", ROOT / "scripts/service.py")
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)

# A disposable socket peer exercises supervision without requiring a real VM.
FAKE_MANAGER = r'''
import json, os, socket, sys, time, threading
from pathlib import Path
home = Path(os.environ["VIRTY_HOME"])
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
        with open(home / "requests.jsonl", "a") as log:
            log.write(json.dumps(message) + "\n")
        result = {"ok": True}
        if command == "status":
            result.update(vmID="service-test", state=state, inputBusy=(home / "busy").exists())
        elif command == "start":
            mode_path = home / "start-mode"
            mode = mode_path.read_text() if mode_path.exists() else "normal"
            if mode == "delay":
                time.sleep(6)
            if mode == "pending-reply":
                state = "starting"
                # Keep this socket open while the fake manager continues serving
                # responsive status requests about its stalled framework start.
                pending = peer.dup()
                threading.Thread(target=lambda: (time.sleep(40), pending.close()), daemon=True).start()
                continue
            if mode == "failed":
                state = "error"
                result = {"ok": False, "error": "fixture startup failure"}
            else:
                state = "running"
            if mode == "lost-reply":
                continue
        elif command == "stop":
            state = "stopped"
        elif command == "cancel":
            (home / "busy").unlink(missing_ok=True)
        try:
            peer.sendall(json.dumps(result).encode() + b"\n")
        except BrokenPipeError:
            pass
        if command == "quit":
            break
'''


class ServiceTest(unittest.TestCase):
    def fake_startup(self, mode="normal", policy_completion=False):
        with tempfile.TemporaryDirectory(prefix="virty-service-start-", dir="/tmp") as directory:
            home = Path(directory); home.chmod(0o700)
            owner = home / "pinned-manager"
            owner.write_text("#!" + sys.executable + "\n" + FAKE_MANAGER); owner.chmod(0o700)
            (home / "vm.json").write_text(json.dumps({"installed": True, "id": "service-test", "managerExecutable": str(owner)}))
            (home / "start-mode").write_text(mode)
            if policy_completion:
                (home / "snapshot-policy.json").write_text('{"fixture":true}')
                self.assertEqual(service.record_for(home)["id"], "service-test")
            worker = subprocess.Popen([sys.executable, str(ROOT / "scripts/service.py"), "run", "--home", str(home)],
                                      env={**os.environ, "VIRTY_AUTOSTART": "1"}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 12
                metadata = None
                while time.monotonic() < deadline:
                    metadata_file = home / "service.json"
                    if metadata_file.exists():
                        metadata = json.loads(metadata_file.read_text())
                        if metadata["ready"]:
                            break
                    if worker.poll() is not None:
                        break
                    time.sleep(0.05)
                if mode == "failed":
                    output, error = worker.communicate(timeout=10)
                    self.assertNotEqual(worker.returncode, 0, output + error)
                    self.assertIn("Guest startup failed", error)
                    self.assertFalse(metadata["ready"])
                else:
                    self.assertIsNone(worker.poll(), "Supervisor exited before readiness")
                    self.assertTrue(metadata and metadata["ready"], "Supervisor did not acknowledge successful startup")
                    commands = [json.loads(line)["command"] for line in (home / "requests.jsonl").read_text().splitlines()]
                    self.assertEqual(commands.count("start"), 0 if policy_completion else 1)
                    self.assertNotIn("stop", commands); self.assertNotIn("quit", commands)
                    if policy_completion:
                        # Completion is removed after readiness. The already-ready
                        # worker still must not subsequently boot the stopped guest.
                        (home / "snapshot-policy.json").unlink()
                        time.sleep(1.2)
                        self.assertEqual(service.manager_status(home)["state"], "stopped")
                    else:
                        self.assertEqual(service.manager_status(home)["state"], "running")
                    worker.terminate(); output, error = worker.communicate(timeout=10)
                    self.assertEqual(worker.returncode, 0, output + error)
                self.assertFalse(service.alive(metadata["managerPID"]))
            finally:
                if worker.poll() is None:
                    worker.terminate(); worker.communicate(timeout=10)

    def test_six_second_startup_is_observed_running_before_ready(self):
        self.fake_startup("delay")

    def test_lost_start_reply_is_reconciled_without_resending_start(self):
        self.fake_startup("lost-reply")

    def test_failed_start_is_not_marked_ready_and_child_exits(self):
        self.fake_startup("failed")

    def test_policy_completion_suppresses_autostart_and_permits_stopped_readiness(self):
        self.fake_startup(policy_completion=True)

    def test_stop_during_pending_start_is_bounded_by_fleet_shutdown_window(self):
        with tempfile.TemporaryDirectory(prefix='virty-service-cancel-', dir='/tmp') as directory:
            home = Path(directory); home.chmod(0o700)
            owner = home / 'pinned-manager'
            owner.write_text('#!' + sys.executable + '\n' + FAKE_MANAGER); owner.chmod(0o700)
            (home / 'vm.json').write_text(json.dumps({'installed':True,'id':'service-test','managerExecutable':str(owner)}))
            (home / 'start-mode').write_text('pending-reply')
            worker = subprocess.Popen([sys.executable,str(ROOT / 'scripts/service.py'),'run','--home',str(home)],
                                      env={**os.environ,'VIRTY_AUTOSTART':'1'},stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    log = home / 'requests.jsonl'
                    if log.exists() and any(json.loads(line)['command']=='start' for line in log.read_text().splitlines()):
                        break
                    time.sleep(0.05)
                else:
                    self.fail('Fake manager did not begin pending start')
                metadata = json.loads((home / 'service.json').read_text())
                self.assertFalse(metadata['ready'])
                started = time.monotonic(); worker.terminate()
                output, error = worker.communicate(timeout=24)
                self.assertLess(time.monotonic()-started,24,output+error)
                self.assertEqual(worker.returncode,0,output+error)
                self.assertFalse(service.alive(metadata['managerPID']))
                commands = [json.loads(line)['command'] for line in log.read_text().splitlines()]
                self.assertEqual(commands.count('start'),1)
                self.assertFalse(json.loads((home / 'service.json').read_text())['ready'])
            finally:
                if worker.poll() is None:
                    worker.kill(); worker.communicate(timeout=5)
                    if 'metadata' in locals() and service.alive(metadata['managerPID']):
                        os.kill(metadata['managerPID'],15)

    def test_stop_during_initial_status_never_dispatches_start(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            home = Path(directory)
            class Event:
                stopped = False
                def is_set(self):
                    return self.stopped
                def set(self):
                    self.stopped = True
            stopping = Event()
            child = Mock(pid=999999, returncode=None); child.poll.return_value = None
            calls = [0]
            def status(*args, **kwargs):
                calls[0] += 1
                if calls[0] == 1:
                    return None
                # Simulate SIGTERM delivered while the initial worker status RPC
                # is waiting. Its successful response must not initiate a boot.
                stopping.set()
                return {'vmID':'service-test','state':'stopped'}
            original_umask = os.umask(0o077)
            try:
                with patch.object(service, 'record_for', return_value={'id':'service-test','managerExecutable':'/fixture'}), \
                     patch.object(service, 'manager_status', side_effect=status), \
                     patch.object(service, 'request') as request, patch.object(service.subprocess, 'Popen', return_value=child), \
                     patch.object(service.signal, 'signal'), patch.object(service.threading, 'Event', return_value=stopping), \
                     patch.object(service, 'shutdown_manager'), patch.dict(os.environ, {'VIRTY_AUTOSTART':'1'}):
                    self.assertEqual(service.run_worker(home),0)
                    request.assert_not_called()
                self.assertFalse(json.loads((home / 'service.json').read_text())['ready'])
                child.terminate.assert_called_once()
            finally:
                os.umask(original_umask)

    def test_stalled_startup_is_bounded_and_never_retries(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            home = Path(directory)
            clock = [0.0]
            class Event:
                def is_set(self):
                    return False
                def wait(self, seconds):
                    clock[0] += seconds
            child = Mock(pid=999999, returncode=None)
            child.poll.return_value = None
            def status(*args, **kwargs):
                if status.calls == 0:
                    status.calls += 1
                    return None
                self.assertLessEqual(kwargs['timeout'], service.STATUS_TIMEOUT)
                return {"vmID":"service-test", "state":"stopped"}
            status.calls = 0
            def start(*args, **kwargs):
                self.assertEqual(args[1], 'start')
                self.assertEqual(kwargs['timeout'], service.LIFECYCLE_TIMEOUT)
                clock[0] += kwargs['timeout']
                raise TimeoutError('fixture lost/stalled startup')
            original_umask = os.umask(0o077)
            try:
                with patch.object(service, 'record_for', return_value={'id':'service-test','managerExecutable':'/fixture'}), \
                     patch.object(service, 'manager_status', side_effect=status), \
                     patch.object(service, 'request', side_effect=start) as request, \
                     patch.object(service.subprocess, 'Popen', return_value=child), \
                     patch.object(service.signal, 'signal'), patch.object(service.threading, 'Event', Event), \
                     patch.object(service.time, 'monotonic', side_effect=lambda:clock[0]), \
                     patch.object(service, 'shutdown_manager', side_effect=ValueError('fixture forced cleanup')), \
                     patch.dict(os.environ, {'VIRTY_AUTOSTART':'1'}):
                    with self.assertRaisesRegex(ValueError, 'within 30 seconds'):
                        service.run_worker(home)
                    request.assert_called_once()
                self.assertLessEqual(clock[0], service.STARTUP_TIMEOUT + 0.11)
                self.assertFalse(json.loads((home / 'service.json').read_text())['ready'])
                child.terminate.assert_called_once(); child.wait.assert_called_once_with(timeout=3)
            finally:
                os.umask(original_umask)

    def test_ready_wait_requires_supervisor_acknowledgment_not_just_socket(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            home = Path(directory)
            entry = {'id': 'ready-test', 'home': str(home), 'autostart': False}
            (home / 'service.json').write_text(json.dumps({'ready': False}))
            def ready_after_first_wait(_):
                (home / 'service.json').write_text(json.dumps({'ready': True}))
            with patch.object(service, 'manager_status', return_value={'vmID': 'ready-test', 'state': 'stopped'}), patch.object(service.time, 'sleep', side_effect=ready_after_first_wait) as wait:
                service.wait_vm_ready(entry)
                wait.assert_called_once()

    def test_plist_preserves_paths_and_gui_scope(self):
        job = plistlib.loads(plistlib.dumps(service.job_definition(Path("/tmp/a vm"))))
        self.assertEqual(job["ProgramArguments"][-1], "/tmp/a vm")
        self.assertEqual(job["LimitLoadToSessionType"], "Aqua")
        self.assertTrue(job["KeepAlive"])
        self.assertTrue(job["RunAtLoad"])
        self.assertNotIn("UserName", job)
        self.assertNotIn("AbandonProcessGroup", job)

    def test_supervisor_uses_owner_and_controls_shutdown(self):
        with tempfile.TemporaryDirectory(prefix="virty-service-", dir="/tmp") as directory:
            home = Path(directory)
            os.chmod(home, 0o700)
            owner = home / "pinned-manager"
            owner.write_text("#!" + sys.executable + "\n" + FAKE_MANAGER)
            owner.chmod(0o700)
            (home / "vm.json").write_text(json.dumps({"installed": True, "id": "service-test", "managerExecutable": str(owner)}))
            self.assertEqual(service.record_for(home)["managerExecutable"], str(owner))
            worker = subprocess.Popen([sys.executable, str(ROOT / "scripts/service.py"), "run", "--home", str(home)],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    status = service.manager_status(home)
                    if status and status["state"] == "running":
                        break
                    time.sleep(0.05)
                else:
                    self.fail("Supervisor did not start the fake guest")
                metadata = json.loads((home / "service.json").read_text())
                self.assertEqual(metadata["supervisorPID"], worker.pid)
                self.assertEqual(metadata["managerExecutable"], str(owner))
                self.assertTrue(service.request(home, "stop", force=True)["ok"])
                time.sleep(1.2)
                self.assertEqual(service.manager_status(home)["state"], "stopped")
                self.assertTrue(service.request(home, "start")["ok"])
                (home / "busy").touch()
                worker.terminate()
                output, error = worker.communicate(timeout=10)
                self.assertEqual(worker.returncode, 0, output + error)
                self.assertFalse(service.alive(metadata["managerPID"]))
                commands = [json.loads(line)["command"] for line in (home / "requests.jsonl").read_text().splitlines()]
                self.assertLess(commands.index("cancel"), len(commands) - 1)
                self.assertEqual(commands[-1], "quit")
                self.assertIn("stop", commands[commands.index("cancel"):])
            finally:
                if worker.poll() is None:
                    worker.terminate()
                    worker.communicate(timeout=10)

    def test_rejects_nonprivate_runtime(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            home = Path(directory)
            os.chmod(home, 0o755)
            with self.assertRaisesRegex(ValueError, "0700"):
                service.record_for(home)

    def test_upgrade_requires_broker_owned_credentials(self):
        registry = Mock()
        with patch.object(service, "record_for", return_value={"keychain": "login"}):
            with self.assertRaisesRegex(ValueError, "broker-owned"):
                service.change_vm_locked(registry, "legacy", {"home": "/unused"}, False)
        registry.set_policy.assert_not_called()

    def test_recognizes_legacy_job_without_replacing_other_installation(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            registry = service.Registry(root / "catalog")
            path = root / "service.plist"
            path.write_bytes(plistlib.dumps(service.job_definition(root / "vm")))
            self.assertEqual(service.fleet_job(path, registry), root / "vm")
            path.write_bytes(plistlib.dumps(service.job_definition(registry.home, fleet=True)))
            self.assertEqual(service.fleet_job(path, registry), "fleet")
            job = service.job_definition(registry.home, fleet=True)
            job["ProgramArguments"][1] = "/different/repo/service.py"
            path.write_bytes(plistlib.dumps(job))
            with self.assertRaisesRegex(ValueError, "another installation"):
                service.fleet_job(path, registry)


if __name__ == "__main__":
    unittest.main()
