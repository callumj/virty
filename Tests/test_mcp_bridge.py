from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import mcp_bridge
import mcp_jobs
from vm_registry import Registry
from test_registry import make_vm


class MCPBridgeTests(unittest.TestCase):
    def test_request_id_contract(self):
        cases = json.loads((mcp_bridge.ROOT / 'Tests/request-id-cases.json').read_text())
        for command in ('type-text', 'type-secret', 'operation'):
            body = {'command': command, 'name': 'linux', 'vmID': '00000000-0000-0000-0000-000000000001'}
            if command != 'operation':
                body.update(generation='frame-1', **({'text': 'public'} if command == 'type-text' else {'alias': 'vm-login'}))
            for case in cases:
                with self.subTest(command=command, requestId=case['value']):
                    request = {**body, 'requestId': case['value']}
                    if case['valid']:
                        self.assertEqual(mcp_bridge.validate(request), command)
                    else:
                        with self.assertRaises(ValueError):
                            mcp_bridge.validate(request)

    def test_scroll_rejects_missing_stale_capabilities_and_unbounded_values(self):
        body = {'command':'scroll','name':'linux','vmID':'00000000-0000-0000-0000-000000000001',
                'x':100,'y':200,'deltaY':3,'generation':'frame-1'}
        with patch.object(mcp_bridge.web_bridge, 'handle', return_value={'ok':True}) as send:
            self.assertTrue(mcp_bridge.handle(body)['ok'])
            for extra in ({'deltaY':11}, {'deltaY':True}, {'deltaY':0}, {'x':-1}, {'generation':''}, {'value':'forbidden'}):
                with self.assertRaises(ValueError):
                    mcp_bridge.handle({**body, **extra})
            for key in ('vmID','generation','x','deltaY'):
                with self.assertRaises((ValueError, TypeError, AttributeError)):
                    mcp_bridge.handle({k:v for k,v in body.items() if k != key})
            send.assert_called_once_with(body)

    def test_setup_assistant_is_a_bounded_creation_policy(self):
        body = {'command': 'create', 'name': 'mac', 'guestOS': 'macos', 'image': '/public-test.ipsw', 'setupAssistant': True}
        with patch.object(mcp_jobs, 'start', return_value={'ok': True}) as start:
            self.assertTrue(mcp_bridge.handle(body)['ok'])
            self.assertTrue(start.call_args.args[0]['setupAssistant'])
            for value in ('true', 1, None):
                with self.assertRaisesRegex(ValueError, 'boolean'):
                    mcp_bridge.handle({**body, 'setupAssistant': value})

    def test_snapshot_requests_are_bounded_and_use_durable_jobs(self):
        target = {'name': 'linux', 'vmID': '00000000-0000-0000-0000-000000000001'}
        requests = [('snapshot-create', {'label': 'baseline'}), ('snapshot-restore', {'snapshotID': target['vmID']}),
                    ('snapshot-recover', {}), ('clone', {'newName': 'copy', 'snapshotID': target['vmID']})]
        with patch.object(mcp_jobs, 'start', return_value={'ok': True, 'jobId': target['vmID']}) as start:
            for command, extra in requests:
                body = {**target, 'command': command, **extra}
                self.assertTrue(mcp_bridge.handle(body)['ok'])
                self.assertEqual(start.call_args.kwargs['kind'], command)
                for key, value in (('password', 'forbidden'), ('snapshotID', '../escape')):
                    with self.assertRaises(ValueError):
                        mcp_bridge.handle({**body, key: value})
        with patch.object(mcp_bridge.vm_snapshots, 'listing', return_value={'ok': True, 'snapshots': []}) as listing:
            mcp_bridge.handle({**target, 'command': 'snapshot-list'})
            listing.assert_called_once_with(target['name'], target['vmID'])

    def test_rejects_secret_and_arbitrary_process_capabilities(self):
        for command in ('read', 'enroll', 'export', 'restore', 'generate', 'shell', 'quit'):
            with self.assertRaisesRegex(ValueError, 'Unsupported'):
                mcp_bridge.handle({'command': command})
        with self.assertRaisesRegex(ValueError, 'Unexpected'):
            mcp_bridge.handle({'command': 'list', 'value': 'forbidden'})
        with self.assertRaisesRegex(ValueError, 'Unexpected'):
            mcp_bridge.handle({'command': 'create', 'name': 'mac', 'guestOS': 'macos', 'password': 'forbidden'})
        with self.assertRaisesRegex(ValueError, 'Missing'):
            mcp_bridge.handle({'command': 'type-secret', 'name': 'mac', 'vmID': '00000000-0000-0000-0000-000000000001', 'alias': 'vm-login'})

    def test_identity_checked_under_lock_and_unregister_retains_data(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            root = Path(directory)
            registry = Registry(root / 'registry')
            home, record = make_vm(root, 1)
            sibling, _ = make_vm(root, 2)
            registry.register('mac', home)
            registry.register('linux', sibling)
            body = {'name': 'mac', 'vmID': record['id']}
            with patch.dict(os.environ, {'VIRTY_REGISTRY_HOME': str(registry.home)}), patch.dict(mcp_bridge.web_bridge.cli, {'wait_disabled': Mock()}) as cli:
                for command in ('rename', 'default', 'autostart', 'enable', 'disable', 'unregister', 'eject'):
                    request = {**body, 'command': command, 'vmID': '00000000-0000-0000-0000-000000000003'}
                    if command == 'rename':
                        request['newName'] = 'desktop'
                    if command == 'autostart':
                        request['enabled'] = False
                    with self.assertRaisesRegex(ValueError, 'identity changed'):
                        mcp_bridge.handle(request)
                    self.assertTrue(registry.entry('mac')[1]['enabled'])
                renamed = mcp_bridge.handle({**body, 'command': 'rename', 'newName': 'desktop'})
                self.assertEqual(renamed['vmID'], record['id'])
                removed = mcp_bridge.handle({**body, 'name': 'desktop', 'command': 'unregister'})
                self.assertTrue(removed['dataRetained'])
                self.assertTrue((home / 'vm.json').exists())
                self.assertEqual(list(registry.read()['vms']), ['linux'])
                self.assertTrue(registry.entry('linux')[1]['enabled'])

    def test_register_rejects_wrong_uuid(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            root = Path(directory)
            home, _ = make_vm(root, 1)
            with patch.dict(os.environ, {'VIRTY_REGISTRY_HOME': str(root / 'registry')}):
                with self.assertRaisesRegex(ValueError, 'identity'):
                    mcp_bridge.handle({'command': 'register', 'name': 'mac', 'vmID': '00000000-0000-0000-0000-000000000002', 'runtime': str(home)})
                self.assertEqual(Registry().read()['vms'], {})


class CreationJobsTests(unittest.TestCase):
    def test_setup_assistant_survives_detached_creation_and_rejects_linux(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            root = Path(directory)
            image = root / 'restore.ipsw'; image.write_bytes(b'public-test-image')
            env = {'VIRTY_REGISTRY_HOME': str(root / 'registry'), 'VIRTY_MCP_HOME': str(root / 'mcp')}
            with patch.dict(os.environ, env), patch.object(mcp_jobs.subprocess, 'Popen', return_value=Mock(pid=os.getpid())) as spawn:
                with self.assertRaisesRegex(ValueError, 'macOS only'):
                    mcp_jobs.start({'name': 'linux', 'guestOS': 'linux', 'image': str(image), 'setupAssistant': True})
                spawn.assert_not_called()
                job = mcp_jobs.start({'name': 'mac', 'guestOS': 'macos', 'image': str(image), 'setupAssistant': True})
                def create(args, **kwargs):
                    self.assertIn('--setup-assistant', args)
                    kwargs['stdout'].write(b'{"ok":true,"name":"mac"}\n')
                    return Mock(returncode=0)
                with patch.object(mcp_jobs.subprocess, 'run', side_effect=create):
                    mcp_jobs.run(job['jobId'])
                self.assertEqual(mcp_jobs.status(job['jobId'])['state'], 'completed')

    def test_clone_reserves_both_names_and_job_dispatch_keeps_source_identity(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            root = Path(directory)
            registry = Registry(root / 'registry')
            home, record = make_vm(root, 1, 'linux')
            registry.register('linux', home)
            env = {'VIRTY_REGISTRY_HOME': str(registry.home), 'VIRTY_MCP_HOME': str(root / 'mcp')}
            with patch.dict(os.environ, env), patch.object(mcp_jobs.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
                request = {'name': 'linux', 'vmID': record['id'], 'newName': 'copy'}
                job = mcp_jobs.start(request, kind='clone')
                self.assertEqual(job['sourceVMID'], record['id'])
                image = root / 'installer.iso'; image.write_bytes(b'public media')
                with self.assertRaisesRegex(ValueError, 'owns this name'):
                    mcp_jobs.start({'name': 'copy', 'guestOS': 'linux', 'image': str(image)})
                with patch.object(mcp_bridge.vm_snapshots, 'clone', return_value={'ok': True, 'name': 'copy'}) as clone:
                    mcp_jobs.run(job['jobId'])
                    clone.assert_called_once_with('linux', 'copy', None, record['id'])
                self.assertEqual(mcp_jobs.status(job['jobId'])['state'], 'completed')
    def test_jobs_serialize_builds_and_recover_missing_worker(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            root = Path(directory)
            image = root / 'installer.iso'
            image.write_bytes(b'public-test-image')
            env = {'VIRTY_MCP_HOME': str(root / 'mcp'), 'VIRTY_REGISTRY_HOME': str(root / 'registry')}
            with patch.dict(os.environ, env), patch.object(mcp_jobs.subprocess, 'Popen', return_value=Mock(pid=os.getpid())) as spawn:
                job = mcp_jobs.start({'name': 'linux', 'guestOS': 'linux', 'image': str(image), 'autostart': False})
                self.assertNotIn('request', job)
                self.assertTrue(spawn.call_args.kwargs['start_new_session'])
                self.assertEqual(spawn.call_args.kwargs['stdout'], subprocess.DEVNULL)
                with self.assertRaisesRegex(ValueError, 'Another VM creation'):
                    mcp_jobs.start({'name': 'next', 'guestOS': 'linux', 'image': str(image)})
                path = mcp_jobs.job_path(mcp_jobs.job_home(), job['jobId'])
                record = mcp_jobs.read(path)
                record['pid'] = None
                record['updatedAt'] = time.time() - 60
                path.write_text(json.dumps(record))
                status = mcp_jobs.status(job['jobId'])
                self.assertEqual(status['state'], 'failed')
                self.assertNotIn('request', status)
                self.assertEqual(mcp_jobs.listing()['jobs'][0]['jobId'], job['jobId'])
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                with self.assertRaises(ValueError):
                    mcp_jobs.status('../../escape')

    def test_worker_captures_build_progress_and_publishes_final_json(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            root = Path(directory)
            image = root / 'installer.iso'
            image.write_bytes(b'public-test-image')
            with patch.dict(os.environ, {'VIRTY_MCP_HOME': str(root / 'mcp'), 'VIRTY_REGISTRY_HOME': str(root / 'registry')}), patch.object(mcp_jobs.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
                job = mcp_jobs.start({'name': 'linux', 'guestOS': 'linux', 'image': str(image), 'autostart': False})
                def create(args, **kwargs):
                    self.assertIn('--no-autostart', args)
                    self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
                    kwargs['stdout'].write(b'build progress\n{"ok":true,"name":"linux","vmID":"00000000-0000-0000-0000-000000000001"}\n')
                    return Mock(returncode=0)
                with patch.object(mcp_jobs.subprocess, 'run', side_effect=create):
                    mcp_jobs.run(job['jobId'])
                status = mcp_jobs.status(job['jobId'])
                self.assertEqual(status['state'], 'completed')
                self.assertEqual(status['result']['name'], 'linux')
                self.assertEqual((mcp_jobs.job_home() / (job['jobId'] + '.log')).stat().st_mode & 0o777, 0o600)
                with self.assertRaisesRegex(ValueError, 'already started'):
                    mcp_jobs.run(job['jobId'])

    def test_macos_resource_options_rejected_before_spawn(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            with patch.dict(os.environ, {'VIRTY_REGISTRY_HOME': str(Path(directory) / 'registry')}), patch.object(mcp_jobs.subprocess, 'Popen') as spawn:
                with self.assertRaisesRegex(ValueError, 'Linux only'):
                    mcp_jobs.start({'name': 'mac', 'guestOS': 'macos', 'image': '/test.ipsw', 'cpus': 2})
                spawn.assert_not_called()

    def test_detached_ejection_job_finishes_after_request_process_exits(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            root = Path(directory)
            registry = Registry(root / 'registry')
            home, record = make_vm(root, 1, 'linux')
            record['installMedia'] = '/public-test/installer.iso'
            (home / 'vm.json').write_text(json.dumps(record))
            (home / 'disk.img').write_bytes(b'retain-this-disk')
            registry.register('linux', home)
            registry.set_policy('linux', enabled=False)
            environment = {**os.environ, 'VIRTY_REGISTRY_HOME': str(registry.home), 'VIRTY_MCP_HOME': str(root / 'mcp')}
            response = subprocess.run([sys.executable, str(mcp_bridge.ROOT / 'scripts/mcp_bridge.py')],
                                      input=json.dumps({'command': 'eject', 'name': 'linux', 'vmID': record['id']}),
                                      env=environment, capture_output=True, text=True, timeout=5)
            self.assertEqual(response.returncode, 0, response.stderr)
            job = json.loads(response.stdout)
            self.assertEqual(job['state'], 'queued')
            with patch.dict(os.environ, environment):
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    result = mcp_jobs.status(job['jobId'])
                    if result['state'] not in ('queued', 'running'):
                        break
                    time.sleep(0.05)
                self.assertEqual(result['state'], 'completed', result)
            self.assertNotIn('installMedia', json.loads((home / 'vm.json').read_text()))
            self.assertEqual((home / 'disk.img').read_bytes(), b'retain-this-disk')
            self.assertFalse(registry.entry('linux')[1]['enabled'])


if __name__ == '__main__':
    unittest.main()
