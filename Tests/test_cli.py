import importlib.machinery
import importlib.util
import json
import base64
import io
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

loader = importlib.machinery.SourceFileLoader("virty_cli", str(Path(__file__).resolve().parents[1] / "bin/virty"))
spec = importlib.util.spec_from_loader(loader.name, loader)
cli = importlib.util.module_from_spec(spec)
loader.exec_module(cli)


class ScreenshotOutputTest(unittest.TestCase):
    image = b"public-fixture-pixels"

    def invoke(self, output):
        with patch.object(cli.sys, 'argv', ['virty', '--home', str(output.parent), 'screenshot', '--output', str(output)]), \
             patch.object(cli, 'call', return_value={'ok': True, 'png': base64.b64encode(self.image).decode()}), \
             patch.object(cli.sys, 'stdout', new_callable=io.StringIO) as stdout:
            status = cli.main()
            return status, json.loads(stdout.getvalue())

    def test_new_and_public_existing_files_become_private(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            output = Path(directory) / 'frame.png'
            for existing in (False, True):
                if existing:
                    output.write_bytes(b'old-public'); output.chmod(0o644)
                status, result = self.invoke(output)
                self.assertEqual(status, 0, result)
                self.assertEqual(output.read_bytes(), self.image)
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                self.assertEqual(result['output'], str(output.parent.resolve() / output.name))

    def test_replaces_final_links_without_writing_their_public_targets(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            root = Path(directory)
            target = root / 'public.png'; target.write_bytes(b'untouched'); target.chmod(0o644)
            for kind in ('symlink', 'dangling-symlink', 'hardlink'):
                output = root / kind
                if kind == 'hardlink':
                    os.link(target, output)
                else:
                    output.symlink_to(target if kind == 'symlink' else root / 'absent')
                status, result = self.invoke(output)
                self.assertEqual(status, 0, result)
                self.assertFalse(output.is_symlink())
                self.assertEqual(output.read_bytes(), self.image)
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                self.assertNotEqual(output.stat().st_ino, target.stat().st_ino)
                self.assertEqual(target.read_bytes(), b'untouched')
                self.assertEqual(target.stat().st_mode & 0o777, 0o644)
                self.assertFalse((root / 'absent').exists())

    def test_failed_write_and_replace_preserve_original_and_remove_temporary(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            root = Path(directory)
            target = root / 'public.png'; target.write_bytes(b'untouched'); target.chmod(0o644)
            output = root / 'frame.png'; output.symlink_to(target)
            for operation in ('fsync', 'replace'):
                with patch.object(cli.os, operation, side_effect=OSError('fixture I/O failure')):
                    status, result = self.invoke(output)
                self.assertEqual(status, 1, result)
                self.assertTrue(output.is_symlink())
                self.assertEqual(target.read_bytes(), b'untouched')
                self.assertEqual(target.stat().st_mode & 0o777, 0o644)
                self.assertEqual(sorted(p.name for p in root.iterdir()), ['frame.png', 'public.png'])

    def test_partial_write_failure_keeps_original_private_temporary_unpublished(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            root = Path(directory)
            output = root / 'frame.png'; output.write_bytes(b'untouched'); output.chmod(0o644)
            real_fdopen = cli.os.fdopen
            def failing_writer(fd, mode):
                file = real_fdopen(fd, mode)
                self.assertEqual(os.fstat(fd).st_mode & 0o777, 0o600)
                class Writer:
                    def __enter__(self):
                        return self
                    def __exit__(self, *args):
                        file.close()
                    def fileno(self):
                        return file.fileno()
                    def write(self, data):
                        file.write(data[:4]); file.flush()
                        raise OSError('fixture partial write failure')
                return Writer()
            with patch.object(cli.os, 'fdopen', side_effect=failing_writer):
                status, result = self.invoke(output)
            self.assertEqual(status, 1, result)
            self.assertEqual(output.read_bytes(), b'untouched')
            self.assertEqual(output.stat().st_mode & 0o777, 0o644)
            self.assertEqual(list(root.iterdir()), [output])

    def test_invalid_image_does_not_modify_destination(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            output = Path(directory) / 'frame.png'; output.write_bytes(b'untouched')
            with patch.object(cli, 'call', return_value={'ok': True, 'png': 'invalid-base64'}):
                with patch.object(cli.sys, 'argv', ['virty', '--home', directory, 'screenshot', '--output', str(output)]), \
                     patch.object(cli.sys, 'stdout', new_callable=io.StringIO):
                    self.assertEqual(cli.main(), 1)
            self.assertEqual(output.read_bytes(), b'untouched')


class CLITransportTest(unittest.TestCase):
    def test_worker_shutdown_eof_waits_for_ownership_locks(self):
        with tempfile.TemporaryDirectory(prefix="virty-stop-", dir="/tmp") as directory:
            with patch.object(cli, 'call', side_effect=ValueError('Closing socket returned EOF')):
                cli.wait_disabled({'id':'public-test-id','home':directory})
            self.assertTrue((Path(directory)/'manager.lock').exists())
            self.assertTrue((Path(directory)/'service.lock').exists())

    def test_fragmented_response_and_alias_request(self):
        with tempfile.TemporaryDirectory(prefix="virty-", dir="/tmp") as directory:
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(Path(directory) / "control.sock"))
            listener.listen(1)
            received = []

            def serve():
                with listener.accept()[0] as peer:
                    request = bytearray()
                    while not request.endswith(b"\n"):
                        request.extend(peer.recv(8))
                    received.append(json.loads(request))
                    for fragment in [b'{"ok":', b'true,"status":', b'"typed"}\n']:
                        peer.sendall(fragment)

            worker = threading.Thread(target=serve)
            worker.start()
            operation = {"command": "type-secret", "alias": "vm-login", "requestId": "test-operation"}
            self.assertEqual(cli.call(operation, directory), {"ok": True, "status": "typed"})
            worker.join(timeout=2)
            listener.close()
            self.assertEqual(received, [operation])
            self.assertNotIn("password", received[0])


if __name__ == "__main__":
    unittest.main()
