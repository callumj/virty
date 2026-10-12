"""Disk/secret consistency, copy isolation and interrupted-maintenance recovery."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import tempfile
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from vm_registry import Registry
import vm_snapshots as snapshots


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir='/tmp'))).resolve()
        self.home = self.root / 'vm'
        self.home.mkdir(mode=0o700)
        self.record = {'id': '00000000-0000-0000-0000-000000000001', 'guestOS': 'linux', 'keychain': 'broker',
                       'installed': True, 'managerExecutable': '/public-test/manager', 'macAddress': '02:00:00:00:00:01'}
        snapshots.write_json(self.home / 'vm.json', self.record)
        for name in ('disk.img', 'machine-id.bin', 'efi.bin'):
            (self.home / name).write_bytes(b'original-' + name.encode())
            (self.home / name).chmod(0o600)
        self.registry = Registry(self.root / 'registry')
        self.registry.register('linux', self.home, autostart=True)
        self.stack.enter_context(patch.dict(os.environ, {'VIRTY_REGISTRY_HOME': str(self.registry.home)}))
        self.status = {'ok': True, 'state': 'stopped', 'vmID': self.record['id'], 'inputBusy': False,
                       'supportsSnapshots': True, 'supportsSnapshotPolicyRecovery': True}
        self.stack.enter_context(patch.object(snapshots.service, 'manager_status', side_effect=lambda _: self.status))
        self.stack.enter_context(patch.object(snapshots.service, 'job_status', return_value={'loaded': False}))
        self.stack.enter_context(patch.object(snapshots.runpy, 'run_path', return_value={'wait_disabled': Mock()}))
        self.secrets = {self.record['id']: {'vm-login': 'public-test-old-password'}}
        self.stack.enter_context(patch.object(snapshots, 'broker_call', side_effect=self.broker))

    def broker(self, operation, **values):
        if operation == 'copy-vm':
            source, target = values['sourceVM'], values['targetVM']
            if self.secrets.get(target) and not values['replace']:
                raise ValueError('Secret copy target must be empty.')
            self.secrets[target] = dict(self.secrets.get(source, {}))
            return {'ok': True, 'aliases': sorted(self.secrets[target])}
        if operation == 'remove-vm':
            self.secrets.pop(values['vmID'], None)
            return {'ok': True}
        return {'ok': True, 'entries': [{'vmID': vm, 'alias': alias} for vm, aliases in self.secrets.items() for alias in aliases]}

    def test_disk_and_password_rollback_keeps_identity_policy_and_input_ledger(self):
        saved = snapshots.create('linux', 'before-upgrade', self.record['id'])
        self.assertNotIn('public-test-old-password', json.dumps(saved))
        (self.home / 'disk.img').write_bytes(b'changed disk')
        self.secrets[self.record['id']] = {'vm-login': 'public-test-new-password', 'later-alias': 'public'}
        current = {**self.record, 'managerExecutable': '/public-test/new-manager'}
        snapshots.write_json(self.home / 'vm.json', current)
        (self.home / 'operations.json').write_text('{"already-delivered":"typed"}')
        operation = self.home / 'operations' / 'ab' / ('ab' + '0' * 62 + '.json')
        operation.parent.mkdir(parents=True)
        operation.write_text('{"already-delivered":"typed"}')
        restored = snapshots.restore('linux', saved['snapshotID'])
        self.assertEqual(restored['state'], 'stopped')
        self.assertEqual((self.home / 'disk.img').read_bytes(), b'original-disk.img')
        self.assertEqual(self.secrets[self.record['id']], {'vm-login': 'public-test-old-password'})
        self.assertEqual(json.loads((self.home / 'vm.json').read_bytes())['managerExecutable'], '/public-test/new-manager')
        self.assertEqual((self.home / 'operations.json').read_text(), '{"already-delivered":"typed"}')
        self.assertEqual(operation.read_text(), '{"already-delivered":"typed"}')
        self.assertTrue(self.registry.entry('linux')[1]['enabled'])
        self.assertTrue(self.registry.entry('linux')[1]['autostart'])
        self.assertFalse((self.home / 'maintenance.json').exists())
        self.assertEqual(snapshots.listing('linux')['snapshots'][0]['snapshotID'], saved['snapshotID'])
        snapshots.delete('linux', saved['snapshotID'])
        self.assertNotIn(saved['snapshotID'], self.secrets)
        self.assertEqual(snapshots.listing('linux')['snapshots'], [])

    def test_running_busy_old_manager_and_stale_identity_refused_without_disable(self):
        for status in ({**self.status, 'state': 'running'}, {**self.status, 'inputBusy': True},
                       {**self.status, 'supportsSnapshotPolicyRecovery': False}, {**self.status, 'supportsSnapshots': False}):
            self.status = status
            with self.assertRaises(ValueError):
                snapshots.create('linux', 'rejected')
            self.assertTrue(self.registry.entry('linux')[1]['enabled'])
            self.assertFalse((self.home / 'maintenance.json').exists())
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            snapshots.create('linux', 'rejected', '00000000-0000-0000-0000-000000000002')
        self.assertEqual(self.secrets, {self.record['id']: {'vm-login': 'public-test-old-password'}})

    def test_macos_auxiliary_storage_can_exceed_32_mib(self):
        home = self.root / 'mac'; home.mkdir(mode=0o700)
        record = {**self.record, 'guestOS': 'macos', 'id': '00000000-0000-0000-0000-000000000002'}
        snapshots.write_json(home / 'vm.json', record)
        for name in ('disk.img', 'machine-id.bin', 'hardware-model.bin', 'auxiliary-storage.bin'):
            (home / name).write_bytes(b'public-mac-boot-state'); (home / name).chmod(0o600)
        os.truncate(home / 'auxiliary-storage.bin', 32 * 1024 * 1024 + 24576)
        self.registry.register('mac', home, autostart=False)
        self.status = {**self.status, 'vmID': record['id']}
        self.secrets[record['id']] = {'vm-login': 'public-mac-password'}
        saved = snapshots.create('mac', 'baseline')
        snapshots.restore('mac', saved['snapshotID'])
        self.assertEqual((home / 'auxiliary-storage.bin').stat().st_size, 32 * 1024 * 1024 + 24576)

    def test_failed_secret_restore_restores_pre_attempt_disk_and_password(self):
        saved = snapshots.create('linux', 'baseline')
        (self.home / 'disk.img').write_bytes(b'new disk worth keeping')
        self.secrets[self.record['id']]['vm-login'] = 'public-current-password'
        original = snapshots.copy_secrets
        def fail_once(source, target, replace=False):
            if source == saved['snapshotID'] and replace:
                raise ValueError('Injected broker failure')
            return original(source, target, replace)
        with patch.object(snapshots, 'copy_secrets', side_effect=fail_once):
            with self.assertRaisesRegex(ValueError, 'Injected'):
                snapshots.restore('linux', saved['snapshotID'])
        self.assertEqual((self.home / 'disk.img').read_bytes(), b'new disk worth keeping')
        self.assertEqual(self.secrets[self.record['id']]['vm-login'], 'public-current-password')
        self.assertFalse((self.home / 'maintenance.json').exists())

    def test_interrupted_restore_stays_disabled_and_recovery_is_repeatable(self):
        saved = snapshots.create('linux', 'baseline')
        (self.home / 'disk.img').write_bytes(b'valuable current disk')
        self.secrets[self.record['id']]['vm-login'] = 'public-current-password'
        real = snapshots.apply_state
        def interrupted(source, home, record):
            os.replace(source / 'disk.img', home / 'disk.img')
            raise KeyboardInterrupt('simulated process interruption')
        with patch.object(snapshots, 'apply_state', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                snapshots.restore('linux', saved['snapshotID'])
        self.assertFalse(self.registry.entry('linux')[1]['enabled'])
        self.assertTrue(snapshots.listing('linux')['recoveryRequired'])
        self.assertEqual((self.home / 'disk.img').read_bytes(), b'original-disk.img')
        with patch.object(snapshots, 'copy_secrets', side_effect=ValueError('temporary broker failure')):
            with self.assertRaises(ValueError):
                snapshots.recover('linux')
        self.assertTrue((self.home / 'maintenance.json').exists())
        self.assertTrue(snapshots.recover('linux')['recovered'])
        self.assertFalse(snapshots.recover('linux')['recovered'])
        self.assertEqual((self.home / 'disk.img').read_bytes(), b'valuable current disk')
        self.assertEqual(self.secrets[self.record['id']]['vm-login'], 'public-current-password')
        self.assertTrue(self.registry.entry('linux')[1]['autostart'])

    def test_metadata_tampering_symlink_and_missing_checkpoint_secrets_refused(self):
        saved = snapshots.create('linux', 'baseline')
        folder = self.home / 'snapshots' / saved['snapshotID']
        self.secrets.pop(saved['snapshotID'])
        with self.assertRaisesRegex(ValueError, 'secret checkpoint'):
            snapshots.restore('linux', saved['snapshotID'])
        self.secrets[saved['snapshotID']] = dict(self.secrets[self.record['id']])
        (folder / 'efi.bin').write_bytes(b'changed metadata')
        with self.assertRaisesRegex(ValueError, 'changed'):
            snapshots.restore('linux', saved['snapshotID'])
        (self.home / 'efi.bin').unlink()
        (self.home / 'efi.bin').symlink_to(folder / 'efi.bin')
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            snapshots.create('linux', 'symlink-refused')
        self.assertFalse((self.home / 'maintenance.json').exists())

    def test_clone_from_checkpoint_copies_secrets_without_copying_runtime_ledgers(self):
        operation = self.home / 'operations' / 'ab' / ('ab' + '0' * 62 + '.json')
        operation.parent.mkdir(parents=True)
        operation.write_text('{"already-delivered":"typed"}')
        saved = snapshots.create('linux', 'baseline')
        self.assertFalse((self.home / 'snapshots' / saved['snapshotID'] / 'operations').exists())
        self.status = {**self.status, 'state': 'running'}
        manager = self.root / '.build/Virty.app/Contents/MacOS/virty-manager'
        manager.parent.mkdir(parents=True)
        manager.write_text('public test binary')
        def new_identity(argv, **kwargs):
            home = Path(kwargs['env']['VIRTY_HOME'])
            (home / 'machine-id.bin').write_bytes(b'new-independent-machine')
            record = json.loads((home / 'vm.json').read_bytes())
            record['macAddress'] = '02:00:00:00:00:02'
            snapshots.write_json(home / 'vm.json', record)
            return Mock(returncode=0)
        with patch.object(snapshots, 'ROOT', self.root), patch.object(snapshots.subprocess, 'run', side_effect=new_identity):
            clone = snapshots.clone('linux', 'copy', saved['snapshotID'])
        entry = self.registry.entry('copy')[1]
        home = Path(entry['home'])
        self.assertNotEqual(clone['vmID'], self.record['id'])
        self.assertFalse(entry['autostart'])
        self.assertEqual(self.secrets[clone['vmID']], self.secrets[saved['snapshotID']])
        self.assertEqual((home / 'disk.img').read_bytes(), b'original-disk.img')
        self.assertEqual((home / 'machine-id.bin').read_bytes(), b'new-independent-machine')
        self.assertFalse((home / 'operations.json').exists())
        self.assertFalse((home / 'operations').exists())
        (home / 'disk.img').write_bytes(b'clone changes')
        self.assertEqual((self.home / 'disk.img').read_bytes(), b'original-disk.img')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            snapshots.clone('linux', 'copy', saved['snapshotID'])

    def test_deletion_interruptions_keep_other_checkpoints_accessible(self):
        survivor = snapshots.create('linux', 'survivor')
        for stage in ('before-journal', 'after-journal', 'before-secrets', 'after-secrets',
                      'metadata-removed', 'before-directory-removal', 'directory-removed',
                      'before-journal-removal', 'after-journal-removal'):
            with self.subTest(stage=stage):
                saved = snapshots.create('linux', 'delete-' + stage)
                folder = self.home / 'snapshots' / saved['snapshotID']
                marker = snapshots.deletion_marker(folder.parent, saved['snapshotID'])
                real_write, real_remove = snapshots.write_json, snapshots.remove_secrets
                real_rmtree, real_unlink = shutil.rmtree, Path.unlink
                def write(path, value):
                    if path == marker and stage == 'before-journal':
                        raise KeyboardInterrupt(stage)
                    real_write(path, value)
                    if path == marker and stage == 'after-journal':
                        raise KeyboardInterrupt(stage)
                def remove(vm_id):
                    if vm_id == saved['snapshotID'] and stage == 'before-secrets':
                        raise KeyboardInterrupt(stage)
                    real_remove(vm_id)
                    if vm_id == saved['snapshotID'] and stage == 'after-secrets':
                        raise KeyboardInterrupt(stage)
                def rmtree(path, *args, **kwargs):
                    if path == folder:
                        if stage == 'metadata-removed':
                            (folder / 'snapshot.json').unlink()
                            raise KeyboardInterrupt(stage)
                        if stage == 'before-directory-removal':
                            for child in folder.iterdir():
                                child.unlink()
                            raise KeyboardInterrupt(stage)
                    real_rmtree(path, *args, **kwargs)
                    if path == folder and stage == 'directory-removed':
                        raise KeyboardInterrupt(stage)
                def unlink(path, *args, **kwargs):
                    if path == marker and stage == 'before-journal-removal':
                        raise KeyboardInterrupt(stage)
                    result = real_unlink(path, *args, **kwargs)
                    if path == marker and stage == 'after-journal-removal':
                        raise KeyboardInterrupt(stage)
                    return result
                with patch.object(snapshots, 'write_json', side_effect=write), \
                        patch.object(snapshots, 'remove_secrets', side_effect=remove), \
                        patch.object(snapshots.shutil, 'rmtree', side_effect=rmtree), \
                        patch.object(Path, 'unlink', unlink):
                    with self.assertRaises(KeyboardInterrupt):
                        snapshots.delete('linux', saved['snapshotID'])
                rows = {row['snapshotID']: row for row in snapshots.listing('linux')['snapshots']}
                self.assertEqual(rows[survivor['snapshotID']]['state'], 'ready')
                if stage == 'after-journal-removal':
                    self.assertNotIn(saved['snapshotID'], rows)
                else:
                    self.assertEqual(rows[saved['snapshotID']]['state'], 'ready' if stage == 'before-journal' else 'deleting')
                self.assertTrue(snapshots.restore('linux', survivor['snapshotID'])['restored'])
                snapshots.delete('linux', saved['snapshotID'] if stage == 'after-journal-removal' else saved['label'])
                self.assertFalse(folder.exists())
                self.assertFalse(marker.exists())
                self.assertNotIn(saved['snapshotID'], self.secrets)
                self.assertEqual(self.secrets[self.record['id']], {'vm-login': 'public-test-old-password'})
                self.assertIn(survivor['snapshotID'], self.secrets)

    def test_deletion_refuses_stale_vm_checkpoint_and_live_secret_identities(self):
        saved = snapshots.create('linux', 'baseline')
        folder = self.home / 'snapshots' / saved['snapshotID']
        metadata = snapshots.read_json(folder / 'snapshot.json')
        marker = snapshots.deletion_marker(folder.parent, saved['snapshotID'])
        for change in ({'vmID': snapshots.new_id()}, {'snapshotID': snapshots.new_id()}):
            snapshots.write_json(marker, {**metadata, **change, 'deleting': True})
            with patch.object(snapshots, 'remove_secrets') as remove:
                with self.assertRaisesRegex(ValueError, 'identity'):
                    snapshots.delete('linux', saved['snapshotID'])
                remove.assert_not_called()
            self.assertTrue(folder.exists())
        marker.unlink()
        with patch.object(snapshots, 'remove_secrets') as remove:
            with self.assertRaisesRegex(ValueError, 'identity changed'):
                snapshots.delete('linux', saved['snapshotID'], snapshots.new_id())
            remove.assert_not_called()
        live_marker = snapshots.deletion_marker(folder.parent, self.record['id'])
        snapshots.write_json(live_marker, {**metadata, 'snapshotID': self.record['id'], 'deleting': True})
        with patch.object(snapshots, 'remove_secrets') as remove:
            with self.assertRaisesRegex(ValueError, 'live VM'):
                snapshots.delete('linux', self.record['id'])
            remove.assert_not_called()

    def test_policy_completion_interruptions_restore_all_saved_policies(self):
        # Fail both sides of every policy write and every durable handoff stage.
        stages = ('before-completion', 'after-completion', 'before-suppression', 'after-suppression',
                  'worker-stop', 'maintenance-unlink', 'after-maintenance-unlink',
                  'before-enable', 'after-enable', 'readiness', 'before-final-policy',
                  'after-final-policy', 'completion-unlink', 'after-completion-unlink')
        completion, maintenance = self.home / 'snapshot-policy.json', self.home / 'maintenance.json'
        for enabled in (False, True):
            for autostart in (False, True):
                for stage in stages:
                    if stage == 'readiness' and not enabled:
                        continue
                    with self.subTest(enabled=enabled, autostart=autostart, stage=stage):
                        self.registry.set_policy('linux', enabled=enabled, autostart=autostart)
                        real_write, real_policy, real_unlink = snapshots.write_json, Registry.set_policy, Path.unlink
                        writes = 0
                        def write(path, value):
                            if path == completion and stage == 'before-completion':
                                raise KeyboardInterrupt(stage)
                            real_write(path, value)
                            if path == completion and stage == 'after-completion':
                                raise KeyboardInterrupt(stage)
                        def policy(registry, name, **values):
                            nonlocal writes
                            writes += 1
                            phase = {2: 'suppression', 3: 'enable', 4: 'final-policy'}.get(writes)
                            if stage == 'before-' + str(phase):
                                raise KeyboardInterrupt(stage)
                            real_policy(registry, name, **values)
                            if stage == 'after-' + str(phase):
                                raise KeyboardInterrupt(stage)
                        def unlink(path, *args, **kwargs):
                            phase = {maintenance: 'maintenance-unlink', completion: 'completion-unlink'}.get(path)
                            if stage == phase:
                                raise KeyboardInterrupt(stage)
                            result = real_unlink(path, *args, **kwargs)
                            if stage == 'after-' + str(phase):
                                raise KeyboardInterrupt(stage)
                            return result
                        stops = 0
                        def wait_disabled(_):
                            nonlocal stops
                            stops += 1
                            if stage == 'worker-stop' and stops == 2:
                                raise KeyboardInterrupt(stage)
                        def ready(_):
                            self.assertFalse(maintenance.exists())
                            self.assertTrue(completion.exists())
                            self.assertFalse(self.registry.entry('linux')[1]['autostart'])
                            if stage == 'readiness':
                                raise KeyboardInterrupt(stage)
                        with patch.object(snapshots, 'write_json', side_effect=write), \
                                patch.object(Registry, 'set_policy', policy), patch.object(Path, 'unlink', unlink), \
                                patch.object(snapshots.runpy, 'run_path', return_value={'wait_disabled': wait_disabled}), \
                                patch.object(snapshots.service, 'job_status', return_value={'loaded': True}), \
                                patch.object(snapshots.service, 'wait_vm_ready', side_effect=ready):
                            with self.assertRaises(KeyboardInterrupt):
                                snapshots.create('linux', 'policy-' + snapshots.new_id().lower())
                        pending = stage != 'after-completion-unlink'
                        self.assertEqual(snapshots.listing('linux')['recoveryRequired'], pending)
                        with patch.object(snapshots.service, 'job_status', return_value={'loaded': True}), \
                                patch.object(snapshots.service, 'wait_vm_ready') as wait_ready:
                            self.assertEqual(snapshots.recover('linux')['recovered'], pending)
                            if pending and enabled:
                                wait_ready.assert_called_once()
                            else:
                                wait_ready.assert_not_called()
                        self.assertFalse(snapshots.recover('linux')['recovered'])
                        restored = self.registry.entry('linux')[1]
                        self.assertEqual((restored['enabled'], restored['autostart']), (enabled, autostart))
                        self.assertFalse(maintenance.exists())
                        self.assertFalse(completion.exists())
                        self.assertEqual(self.status['state'], 'stopped')

    def test_policy_journal_identity_and_conflict_refused_before_mutation(self):
        with patch.object(snapshots, 'restore_policy', side_effect=OSError('policy write failed')):
            with self.assertRaisesRegex(OSError, 'policy write failed'):
                snapshots.create('linux', 'baseline')
        completion = self.home / 'snapshot-policy.json'
        journal = snapshots.read_json(completion)
        self.assertTrue(snapshots.listing('linux')['recoveryRequired'])
        with self.assertRaisesRegex(ValueError, 'maintenance is pending'):
            snapshots.create('linux', 'blocked')
        for changed in ({**journal, 'vmID': snapshots.new_id()},
                        {**journal, 'policy': {**journal['policy'], 'home': str(self.root)}},
                        {**journal, 'policy': {**journal['policy'], 'autostart': 1}}):
            snapshots.write_json(completion, changed)
            with patch.object(Registry, 'set_policy') as policy:
                with self.assertRaises(ValueError):
                    snapshots.recover('linux')
                policy.assert_not_called()
        snapshots.write_json(completion, journal)
        snapshots.write_json(self.home / 'maintenance.json', {**journal, 'phase': 'committed', 'transactionID': snapshots.new_id()})
        with patch.object(Registry, 'set_policy') as policy:
            with self.assertRaisesRegex(ValueError, 'Conflicting'):
                snapshots.recover('linux')
            policy.assert_not_called()
        snapshots.write_json(self.home / 'maintenance.json', {**journal, 'phase': 'committed'})
        self.assertTrue(snapshots.recover('linux')['recovered'])
        self.assertFalse(snapshots.recover('linux')['recovered'])

    def test_policy_readiness_failure_keeps_journal_and_autostart_suppressed(self):
        with patch.object(snapshots.service, 'job_status', return_value={'loaded': True}), \
                patch.object(snapshots.service, 'wait_vm_ready', side_effect=ValueError('not ready')):
            with self.assertRaisesRegex(ValueError, 'not ready'):
                snapshots.create('linux', 'baseline')
        self.assertTrue((self.home / 'snapshot-policy.json').exists())
        self.assertFalse((self.home / 'maintenance.json').exists())
        self.assertFalse(self.registry.entry('linux')[1]['autostart'])
        self.assertTrue(snapshots.recover('linux')['recovered'])

    def test_legacy_recovery_finishes_disk_cleanup_but_waits_for_controller_upgrade(self):
        saved = snapshots.create('linux', 'baseline')
        (self.home / 'disk.img').write_bytes(b'valuable current disk')
        def interrupted(source, home, record):
            os.replace(source / 'disk.img', home / 'disk.img')
            raise KeyboardInterrupt('interrupted legacy restore')
        with patch.object(snapshots, 'apply_state', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                snapshots.restore('linux', saved['snapshotID'])
        self.status.pop('supportsSnapshotPolicyRecovery')
        with self.assertRaisesRegex(ValueError, 'Upgrade this VM'):
            snapshots.recover('linux')
        self.assertEqual((self.home / 'disk.img').read_bytes(), b'valuable current disk')
        self.assertFalse((self.home / 'maintenance.json').exists())
        self.assertTrue((self.home / 'snapshot-policy.json').exists())
        policy = self.registry.entry('linux')[1]
        self.assertEqual((policy['enabled'], policy['autostart']), (False, False))
        # Repeat safely before the upgrade; do not accidentally enable an old worker.
        with self.assertRaisesRegex(ValueError, 'Upgrade this VM'):
            snapshots.recover('linux')
        self.status['supportsSnapshotPolicyRecovery'] = True
        self.assertTrue(snapshots.recover('linux')['recovered'])
        self.assertFalse(snapshots.recover('linux')['recovered'])
        self.assertTrue(self.registry.entry('linux')[1]['autostart'])

    def test_offline_capability_probe_requires_policy_recovery_support(self):
        self.status = None
        for supported in (False, True):
            with self.subTest(supported=supported):
                capabilities = {'supportsSnapshots': True, 'supportsSnapshotPolicyRecovery': supported}
                with patch.object(snapshots.subprocess, 'run', return_value=Mock(returncode=0, stdout=json.dumps(capabilities))):
                    if supported:
                        snapshots.create('linux', 'offline')
                    else:
                        with self.assertRaisesRegex(ValueError, 'Upgrade this VM'):
                            snapshots.create('linux', 'offline')
                        self.assertFalse((self.home / 'maintenance.json').exists())
                        self.assertFalse((self.home / 'snapshot-policy.json').exists())
                        self.assertTrue(self.registry.entry('linux')[1]['enabled'])

    def test_final_policy_directory_sync_failure_keeps_completion_recoverable(self):
        real_sync = snapshots.sync_directory
        policy_syncs = 0
        def sync(path):
            nonlocal policy_syncs
            if path == self.registry.home:
                policy_syncs += 1
                if policy_syncs == 4:
                    raise OSError('registry directory fsync failed')
            real_sync(path)
        with patch.object(snapshots, 'sync_directory', side_effect=sync):
            with self.assertRaisesRegex(OSError, 'fsync failed'):
                snapshots.create('linux', 'baseline')
        self.assertTrue((self.home / 'snapshot-policy.json').exists())
        # Also fail recovery readiness repeatedly; final startup policy remains
        # suppressed and the original saved policy survives every attempt.
        with patch.object(snapshots.service, 'job_status', return_value={'loaded': True}), \
                patch.object(snapshots.service, 'wait_vm_ready', side_effect=ValueError('not ready')):
            for _ in range(2):
                with self.assertRaisesRegex(ValueError, 'not ready'):
                    snapshots.recover('linux')
                self.assertTrue((self.home / 'snapshot-policy.json').exists())
                self.assertFalse(self.registry.entry('linux')[1]['autostart'])
        self.assertTrue(snapshots.recover('linux')['recovered'])
        self.assertFalse(snapshots.recover('linux')['recovered'])
        self.assertTrue(self.registry.entry('linux')[1]['autostart'])

    def test_disable_policy_write_interruption_retains_original_policy(self):
        real_policy = Registry.set_policy
        for persisted in (False, True):
            with self.subTest(persisted=persisted):
                def fail(registry, name, **values):
                    if persisted:
                        real_policy(registry, name, **values)
                    raise KeyboardInterrupt('interrupted first policy write')
                with patch.object(Registry, 'set_policy', fail), \
                        patch.object(snapshots, 'complete_policy', side_effect=KeyboardInterrupt('process exited')):
                    with self.assertRaises(KeyboardInterrupt):
                        snapshots.create('linux', 'before-disable')
                self.assertTrue((self.home / 'maintenance.json').exists())
                saved = snapshots.read_json(self.home / 'maintenance.json')['policy']
                self.assertEqual((saved['enabled'], saved['autostart']), (True, True))
                self.assertTrue(snapshots.recover('linux')['recovered'])
                self.assertFalse(snapshots.recover('linux')['recovered'])
                self.assertTrue(self.registry.entry('linux')[1]['enabled'])


if __name__ == '__main__':
    unittest.main()
