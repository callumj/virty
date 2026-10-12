"""Installation configuration is explicit and existing private state is retained."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import web_service


class WebServiceInstallationTests(unittest.TestCase):
    def install(self, root, arguments):
        state = root / 'web'
        original_umask = os.umask(0o077)
        try:
            with patch.object(web_service, 'STATE', state), \
                 patch.object(web_service, 'PLIST', root / 'agents/com.virty.web.plist'), \
                 patch.object(web_service.shutil, 'which', return_value='/usr/bin/node'), \
                 patch.object(web_service, 'launchctl') as launchctl, \
                 patch.object(sys, 'argv', ['virty-web', 'install', *arguments]), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result = web_service.main()
                return result, launchctl.call_count
        finally:
            os.umask(original_umask)

    def test_new_install_refuses_missing_site_configuration_without_launching(self):
        for arguments in ([], ['--origin', 'https://console.example.com'], ['--proxy', '127.0.0.1']):
            with self.subTest(arguments=arguments), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                result, calls = self.install(root, arguments)
                self.assertEqual((result, calls), (1, 0))
                self.assertFalse((root / 'web/config.json').exists())
                self.assertFalse((root / 'agents/com.virty.web.plist').exists())

    def test_new_install_uses_explicit_origin_proxy_and_loopback_bind(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result, calls = self.install(root, ['--origin', 'https://console.example.com', '--proxy', '127.0.0.1'])
            self.assertEqual(result, 0)
            self.assertGreater(calls, 0)
            config = json.loads((root / 'web/config.json').read_text())
            self.assertEqual(config, {'origin': 'https://console.example.com', 'bind': '127.0.0.1',
                                     'port': 8765, 'proxyPeers': ['127.0.0.1'], 'state': str(root / 'web')})

    def test_reinstall_preserves_existing_site_configuration_and_account(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'web').mkdir(mode=0o700)
            config = b'{"origin":"https://existing.example.com","bind":"192.0.2.10","port":8765,"proxyPeers":["192.0.2.20"]}\n'
            account = b'public fixture representing retained account data\n'
            (root / 'web/config.json').write_bytes(config)
            (root / 'web/account.json').write_bytes(account)
            result, calls = self.install(root, [])
            self.assertEqual(result, 0)
            self.assertGreater(calls, 0)
            self.assertEqual((root / 'web/config.json').read_bytes(), config)
            self.assertEqual((root / 'web/account.json').read_bytes(), account)


if __name__ == '__main__':
    unittest.main()
