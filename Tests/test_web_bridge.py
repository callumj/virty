import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import web_bridge


class WebBridgeTests(unittest.TestCase):
    def test_scroll_is_scoped_and_bounded_before_delivery(self):
        body = {'command':'scroll','name':'macos','vmID':self.identity,'generation':'frame-1',
                'x':100,'y':200,'deltaY':3}
        from unittest.mock import Mock
        call = Mock(return_value={'ok':True})
        with patch.object(web_bridge.Registry, 'entry', return_value=('macos',self.entry)), patch.dict(web_bridge.cli, {'call':call}):
            self.assertTrue(web_bridge.handle(body)['ok'])
            call.assert_called_once_with({k:v for k,v in body.items() if k != 'name'},self.entry['home'])
            for extra in ({'deltaY':11},{'deltaY':True},{'deltaY':0},{'x':-1},{'generation':None},{'vmID':'other'}):
                with self.assertRaises(ValueError):
                    web_bridge.handle({**body,**extra})
            self.assertEqual(call.call_count,1)

    def test_snapshot_job_status_is_scoped_to_selected_name_and_identity(self):
        import mcp_jobs
        from unittest.mock import Mock
        body = {'command': 'snapshot-job', 'name': 'macos', 'vmID': '00000000-0000-0000-0000-000000000001', 'jobId': '00000000-0000-0000-0000-000000000001'}
        with patch.object(web_bridge.Registry, 'entry', return_value=('macos', self.entry)), patch.object(mcp_jobs, 'status', return_value={'ok': True, 'name': 'macos', 'sourceVMID': self.identity}) as status:
            self.assertTrue(web_bridge.handle(body)['ok'])
            status.return_value['sourceVMID'] = 'another-vm'
            with self.assertRaisesRegex(ValueError, 'identity differs'):
                web_bridge.handle(body)
            with self.assertRaisesRegex(ValueError, 'Unexpected'):
                web_bridge.handle({**body, 'path': '/private'})

    def setUp(self):
        self.identity = '00000000-0000-0000-0000-000000000001'
        self.entry = {'id': self.identity, 'home': '/private/disposable-test-vm'}

    def test_mismatched_identity_never_delivers(self):
        with patch.object(web_bridge.Registry, 'entry', return_value=('macos', self.entry)), patch.dict(web_bridge.cli, {'call': lambda *args: self.fail('Control was delivered')}):
            with self.assertRaisesRegex(ValueError, 'identity changed'):
                web_bridge.handle({'command': 'start', 'name': 'macos', 'vmID': 'different'})

    def test_secret_aliases_are_filtered_and_never_read(self):
        from contextlib import nullcontext
        entries = [{'vmID': self.identity, 'alias': 'vm-login'}, {'vmID': 'another-vm', 'alias': 'other'}]
        with patch.object(web_bridge.Registry, 'entry', return_value=('macos', self.entry)), patch.object(web_bridge.secret_broker, 'ownership', return_value=nullcontext()), patch.object(web_bridge.secret_broker, 'read_owner', return_value={'instance': 'test'}), patch.object(web_bridge.secret_broker, 'request', return_value={'entries': entries}) as request:
            result = web_bridge.handle({'command': 'aliases', 'name': 'macos', 'vmID': self.identity})
            self.assertEqual(result, {'ok': True, 'aliases': ['vm-login']})
            request.assert_called_once_with({'instance': 'test'}, 'list')

    def test_restart_stops_then_starts_only_the_selected_identity(self):
        from unittest.mock import Mock
        call = Mock(side_effect=[{'ok': True, 'state': 'running'}, {'ok': True, 'shutdownRequested': True}, {'ok': True, 'state': 'stopped'}, {'ok': True, 'state': 'running'}])
        with patch.object(web_bridge.Registry, 'entry', return_value=('linux', self.entry)), patch.dict(web_bridge.cli, {'call': call}):
            result = web_bridge.handle({'command': 'restart', 'name': 'linux', 'vmID': self.identity, 'force': False})
            self.assertEqual(result['state'], 'running')
            self.assertEqual([item.args[0]['command'] for item in call.call_args_list], ['status', 'stop', 'status', 'start'])
            self.assertTrue(all(item.args[0]['vmID'] == self.identity for item in call.call_args_list))
            self.assertFalse(call.call_args_list[1].args[0]['force'])

    def test_secret_retrieval_and_enrollment_are_not_web_commands(self):
        with patch.object(web_bridge.Registry, 'entry', return_value=('macos', self.entry)):
            for command in ('read', 'enroll', 'export', 'restore', 'generate', 'quit', 'create'):
                with self.assertRaisesRegex(ValueError, 'Unsupported'):
                    web_bridge.handle({'command': command, 'name': 'macos', 'vmID': self.identity})
            with self.assertRaisesRegex(ValueError, 'Unexpected'):
                web_bridge.handle({'command': 'type-secret', 'name': 'macos', 'vmID': self.identity, 'generation': 'test', 'alias': 'vm-login', 'requestId': 'test', 'value': 'forbidden'})


if __name__ == '__main__':
    unittest.main()
