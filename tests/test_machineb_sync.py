import hashlib
import tempfile
import unittest
from pathlib import Path


class MemoryTransport:
    def __init__(self):
        self.files = {}
        self.calls = []
        self.corrupt = False

    def remote_file_exists(self, path):
        self.calls.append(('exists', path))
        return path in self.files

    def remote_sha256(self, path):
        self.calls.append(('hash', path))
        return hashlib.sha256(self.files[path]).hexdigest()

    def ensure_directories(self, paths):
        self.calls.append(('mkdir', list(paths)))

    def upload(self, local, remote):
        self.calls.append(('upload', remote))
        self.files[remote] = b'corrupt' if self.corrupt else Path(local).read_bytes()


class ProbeSyncTests(unittest.TestCase):
    def test_profiles_and_unknown_custom_bat(self):
        import fetch_probe
        full = fetch_probe.build_probe_sync_plan('C:/tools/run_susi_full_probe.bat')
        self.assertEqual(len(full), 5)
        self.assertEqual({Path(x['local_path']).name for x in full}, {
            'run_susi_full_probe.bat', 'susi_board_probe.ps1', 'susi_gpio_probe.ps1',
            'susi_i2c_smbus_probe.ps1', 'susi_vga_backlight_probe.ps1'})
        self.assertEqual(len(fetch_probe.build_probe_sync_plan('C:/tools/run_susi_spd_idx_probe.bat')), 2)
        with self.assertRaisesRegex(ValueError, 'unsupported.*BAT'):
            fetch_probe.build_probe_sync_plan('C:/tools/custom.bat')

    def test_ssh_sync_precedes_execution_and_failure_stops_probe(self):
        import fetch_probe
        from unittest.mock import patch
        calls = []
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(fetch_probe, 'sync_files', side_effect=lambda *a: (calls.append('sync') or {'uploaded_count': 5, 'skipped_count': 0, 'verified_count': 5})), \
                patch.object(fetch_probe, '_run', side_effect=lambda *a: calls.append('run')):
            fetch_probe.fetch_probe_ssh('example.invalid', 'user', 'C:/tools/run_susi_full_probe.bat', 'C:/report', Path(tmp)/'report')
        self.assertEqual(calls, ['sync', 'run', 'run'])
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(fetch_probe, 'sync_files', side_effect=RuntimeError('sync failed')), \
                patch.object(fetch_probe, '_run') as run:
            with self.assertRaisesRegex(RuntimeError, 'sync failed'):
                fetch_probe.fetch_probe_ssh('example.invalid', 'user', 'C:/tools/run_susi_full_probe.bat', 'C:/report', Path(tmp)/'report')
            run.assert_not_called()

    def test_winrm_sync_precedes_execution_and_failure_stops_probe(self):
        import fetch_probe
        import subprocess
        from unittest.mock import patch
        calls = []
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(fetch_probe, 'sync_files', side_effect=lambda *a: (calls.append('sync') or {'uploaded_count': 5, 'skipped_count': 0, 'verified_count': 5})), \
                patch.object(fetch_probe, '_run_powershell', side_effect=lambda *a: (calls.append('run') or subprocess.CompletedProcess([], 0, 'report', ''))):
            out = Path(tmp) / 'report'
            fetch_probe.fetch_probe_winrm('192.168.100.20', 'C:/tools/run_susi_full_probe.bat', 'C:/report', out, 'other-user', None)
            self.assertEqual(out.read_text(), 'report')
        self.assertEqual(calls, ['sync', 'run'])
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(fetch_probe, 'sync_files', side_effect=RuntimeError('sync failed')), \
                patch.object(fetch_probe, '_run_powershell') as run:
            with self.assertRaisesRegex(RuntimeError, 'sync failed'):
                fetch_probe.fetch_probe_winrm('192.168.100.20', 'C:/tools/run_susi_full_probe.bat', 'C:/report', Path(tmp)/'report', None, None)
            run.assert_not_called()

    def test_winrm_upload_binary_and_quotes_with_session_cleanup(self):
        import fetch_probe
        import base64
        import subprocess
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / 'tool.ps1'
            payload = b'\x00\xff\r\nbinary'
            local.write_bytes(payload)
            with patch.object(fetch_probe, '_run_powershell', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
                fetch_probe.WinrmPowerShellTransport('192.168.100.20', 'tester', 'secret').upload(local, "C:\\tools\\O'Brien.ps1")
            script = run.call_args.args[0]
            self.assertIn(base64.b64encode(payload).decode(), script)
            self.assertIn("O''Brien.ps1", script)
            self.assertIn('finally { Remove-PSSession', script)
        with self.assertRaises(ValueError):
            fetch_probe.WinrmPowerShellTransport('172.22.13.25', None, None)

    def test_ssh_empty_target_then_repeat_uses_real_sync_without_touching_reports(self):
        import fetch_probe
        from unittest.mock import patch
        remote_bat = 'C:/Users/tester/Desktop/suto/V7/run_susi_full_probe.bat'
        transport = MemoryTransport()
        transport.files['C:\\Users\\tester\\Desktop\\suto\\V7\\old.log'] = b'preserve'
        expected = fetch_probe.build_probe_sync_plan(remote_bat)

        def execute(command):
            for item in expected:
                self.assertEqual(transport.files[item['remote_path']], Path(item['local_path']).read_bytes())
            self.assertEqual(command[0], 'ssh' if 'cmd /c' in command[-1] else 'scp')

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(fetch_probe, 'SshPowerShellTransport', return_value=transport), \
                patch.object(fetch_probe, '_run', side_effect=execute):
            fetch_probe.fetch_probe_ssh('example.invalid', 'tester', remote_bat, 'C:/report', Path(tmp)/'report')
            upload_count = sum(c[0] == 'upload' for c in transport.calls)
            self.assertEqual(upload_count, 5)
            fetch_probe.fetch_probe_ssh('example.invalid', 'tester', remote_bat, 'C:/report', Path(tmp)/'report')
            self.assertEqual(sum(c[0] == 'upload' for c in transport.calls), upload_count)
        self.assertEqual(transport.files['C:\\Users\\tester\\Desktop\\suto\\V7\\old.log'], b'preserve')

    def test_probe_sync_reports_counts(self):
        import fetch_probe
        from unittest.mock import patch
        result = {'uploaded_count': 2, 'skipped_count': 3, 'verified_count': 5}
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(fetch_probe, 'sync_files', return_value=result), \
                patch.object(fetch_probe, '_run'), \
                patch('builtins.print') as output:
            fetch_probe.fetch_probe_ssh('example.invalid', 'other-user', 'C:/Users/other-user/Desktop/suto/V7/run_susi_full_probe.bat', 'C:/report', Path(tmp)/'report')
        text = '\n'.join(str(call) for call in output.call_args_list)
        self.assertIn('uploaded=2', text)
        self.assertIn('skipped=3', text)
        self.assertIn('verified=5', text)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.local = Path(self.tmp.name) / 'tool.ps1'
        self.local.write_bytes(b'\x00\xfftool\r\n')
        self.item = {'local_path': str(self.local), 'remote_path': 'C:\\tools\\tool.ps1', 'kind': 'tool'}
        self.transport = MemoryTransport()

    def test_missing_changed_identical_and_unrelated_preserved(self):
        from machineb_sync import sync_files
        self.transport.files['C:\\reports\\keep.txt'] = b'keep'
        first = sync_files([self.item], self.transport)
        self.assertEqual(first['uploaded_count'], 1)
        self.assertTrue(first['artifacts'][0]['hash_verified'])
        second = sync_files([self.item], self.transport)
        self.assertEqual(second['skipped_count'], 1)
        self.assertEqual(second['uploaded_count'], 0)
        self.local.write_bytes(b'updated')
        third = sync_files([self.item], self.transport)
        self.assertEqual(third['uploaded_count'], 1)
        self.assertEqual(self.transport.files['C:\\reports\\keep.txt'], b'keep')

    def test_preflight_all_inputs_before_any_transport_call(self):
        from machineb_sync import sync_files, SyncError
        missing = dict(self.item, local_path=str(self.local.parent / 'missing'), remote_path='C:\\tools\\missing')
        with self.assertRaises(SyncError) as caught:
            sync_files([self.item, missing], self.transport)
        self.assertEqual(caught.exception.phase, 'local_preflight')
        self.assertEqual(self.transport.calls, [])

    def test_casefold_collision_rejected_before_writes(self):
        from machineb_sync import sync_files, SyncError
        with self.assertRaises(SyncError) as caught:
            sync_files([self.item, dict(self.item, remote_path='c:/TOOLS/TOOL.ps1')], self.transport)
        self.assertEqual(caught.exception.phase, 'plan_conflict')
        self.assertEqual(self.transport.calls, [])

    def test_post_upload_mismatch_fails(self):
        from machineb_sync import sync_files, SyncError
        self.transport.corrupt = True
        with self.assertRaises(SyncError) as caught:
            sync_files([self.item], self.transport)
        self.assertEqual(caught.exception.phase, 'hash_mismatch')
        self.assertEqual(caught.exception.remote_path, self.item['remote_path'])
