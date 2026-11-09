"""Local companion management boundaries without changing tasks or Serve."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'windows/mobile-companion.ps1'
POWERSHELL = shutil.which('powershell.exe')


@unittest.skipUnless(POWERSHELL, 'Windows PowerShell is required')
class MobileWindowsTests(unittest.TestCase):
    def run_script(self, home, action='Status', extra=()):
        return subprocess.run([POWERSHELL, '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(SCRIPT),
            '-Action', action, '-HomeDirectory', str(home), '-PythonPath', sys.executable,
            '-SourcePath', str(ROOT), *extra], capture_output=True, text=True, timeout=30)

    def test_disabled_status_and_ensure_are_local_and_do_not_enable_access(self):
        with tempfile.TemporaryDirectory(prefix='companion-script-') as temporary:
            home = Path(temporary)
            status = self.run_script(home)
            self.assertEqual(status.returncode, 0, status.stderr)
            first = json.loads(status.stdout)
            self.assertFalse(first['enabled'])
            self.assertFalse(first['running'])
            ensured = self.run_script(home, 'Ensure')
            self.assertEqual(ensured.returncode, 0, ensured.stderr)
            self.assertEqual(json.loads(ensured.stdout), {'enabled': False})
            second = json.loads(self.run_script(home).stdout)
            self.assertEqual(first['installationId'], second['installationId'])
            self.assertFalse((home / 'auth/authorized-keys.json').exists())

    def test_serve_ownership_refuses_collisions_and_public_exposure(self):
        with tempfile.TemporaryDirectory() as temporary:
            def literal(value):
                return "'" + str(value).replace("'", "''") + "'"
            code = '. ' + literal(SCRIPT) + ' -Action Status -HomeDirectory ' + literal(temporary) + \
                ' -PythonPath ' + literal(sys.executable) + ' -SourcePath ' + literal(ROOT) + ' | Out-Null; ' + """
                $none=Test-OwnedServe ([pscustomobject]@{}) 'https://host.test.ts.net:9444';
                $owned='{"TCP":{"9444":{"HTTPS":true}},"Web":{"host.test.ts.net:9444":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:8790"}}}}}'|ConvertFrom-Json;
                $yes=Test-OwnedServe $owned 'https://host.test.ts.net:9444';
                $owned.Web.'host.test.ts.net:9444'.Handlers|Add-Member -NotePropertyName '/v1' -NotePropertyValue @{Proxy='http://127.0.0.1:9999'};
                $mount=$false; try {Test-OwnedServe $owned 'https://host.test.ts.net:9444'|Out-Null} catch {$mount=$true};
                $owned.Web.'host.test.ts.net:9444'.Handlers.PSObject.Properties.Remove('/v1');
                $owned.Web.'host.test.ts.net:9444'.Handlers.'/'.Proxy='http://127.0.0.1:9999';
                $collision=$false; try {Test-OwnedServe $owned 'https://host.test.ts.net:9444'|Out-Null} catch {$collision=$true};
                $public='{"AllowFunnel":{"host.test.ts.net:9444":true}}'|ConvertFrom-Json;
                $funnel=$false; try {Test-OwnedServe $public 'https://host.test.ts.net:9444'|Out-Null} catch {$funnel=$true};
                $forward='{"TCP":{"9444":{"TCPForward":"127.0.0.1:9999"}}}'|ConvertFrom-Json;
                $tcp=$false; try {Test-OwnedServe $forward 'https://host.test.ts.net:9444'|Out-Null} catch {$tcp=$true};
                $fg='{"Foreground":{"session":{"TCP":{"9444":{"HTTPS":true}}}}}'|ConvertFrom-Json;
                $foreground=$false; try {Test-OwnedServe $fg 'https://host.test.ts.net:9444'|Out-Null} catch {$foreground=$true};
                @{none=$none;owned=$yes;collision=$collision;funnel=$funnel;mount=$mount;tcp=$tcp;foreground=$foreground}|ConvertTo-Json -Compress
                """
            result = subprocess.run([POWERSHELL, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', code],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {'none': False, 'owned': True, 'collision': True, 'funnel': True, 'mount': True, 'tcp': True, 'foreground': True})

    def test_device_identifier_is_not_a_process_argument_capability(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.run_script(temporary, 'Revoke', ('-DeviceId', 'bad; Stop-Process'))
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('Stop-Process :', result.stderr)

    def test_existing_service_requires_explicit_exact_loopback_connection(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            cases = [
                {'config': {'mode': 'existing-service', 'origin': 'http://127.0.0.1:8789'}, 'allowed': True},
                {'config': {'mode': 'existing-service', 'origin': 'http://127.0.0.1:8791'}, 'allowed': False},
                {'config': {'mode': 'existing-service', 'origin': 'https://example.com'}, 'allowed': False},
                {'config': {'mode': 'automatic', 'origin': 'http://127.0.0.1:8789'}, 'allowed': False},
                {'config': {'mode': ['existing-service'], 'origin': 'http://127.0.0.1:8789'}, 'allowed': False},
                {'config': {'mode': 'existing-service', 'origin': 'http://127.0.0.1:8789', 'extra': True}, 'allowed': False},
                {'raw': '{invalid', 'allowed': False},
            ]
            (directory / 'cases.json').write_text(json.dumps(cases), encoding='utf-8')
            literal = lambda value: "'" + str(value).replace("'", "''") + "'"
            code = '. ' + literal(SCRIPT) + ' -Action Status -HomeDirectory ' + literal(directory / 'home') + \
                ' -PythonPath ' + literal(sys.executable) + ' -SourcePath ' + literal(ROOT) + ' | Out-Null; ' + \
                '$testDirectory=' + literal(directory) + '; ' + """
                $missing=Get-ExistingServiceOptIn $testDirectory 'http://127.0.0.1:8789/';
                $results=@();
                foreach ($case in (Get-Content (Join-Path $testDirectory 'cases.json') -Raw|ConvertFrom-Json)) {
                    $raw=if ($case.raw) {$case.raw} else {$case.config|ConvertTo-Json -Compress};
                    $raw|Set-Content -LiteralPath (Join-Path $testDirectory 'client-connection.json');
                    $allowed=$false;
                    try {$allowed=Get-ExistingServiceOptIn $testDirectory 'http://127.0.0.1:8789/'} catch {};
                    $results+=($allowed -eq $case.allowed);
                };
                @{missing=$missing;allCasesPassed=(@($results|Where-Object {!$_}).Count -eq 0)}|ConvertTo-Json -Compress
                """
            result = subprocess.run([POWERSHELL, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', code],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {'missing': False, 'allCasesPassed': True})

    def test_scheduled_runner_drains_native_stderr_without_exposing_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            module = directory / 'source/permissive'
            module.mkdir(parents=True)
            (module / '__init__.py').write_text('', encoding='utf-8')
            (module / 'mobile.py').write_text(
                "import sys\nprint('private child stdout')\nprint('private child stderr', file=sys.stderr)\n", encoding='utf-8')
            result = subprocess.run([POWERSHELL, '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(SCRIPT),
                '-Action', 'Run', '-HomeDirectory', str(directory / 'home'), '-PythonPath', sys.executable,
                '-SourcePath', str(directory / 'source')], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), '')
            self.assertEqual(result.stderr.strip(), '')

    def test_bound_companion_home_is_shared_by_both_launch_contexts(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            package, logical, physical = directory / 'package', directory / 'logical', directory / 'physical'
            package.mkdir()
            (package / 'companion-home.json').write_text(json.dumps({
                'version': 1, 'logicalHome': str(logical), 'physicalHome': str(physical)}), encoding='utf-8')
            first = self.run_script(logical, extra=('-PackageDirectory', str(package)))
            second = self.run_script(physical, extra=('-PackageDirectory', str(package)))
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(json.loads(first.stdout)['installationId'], json.loads(second.stdout)['installationId'])
            self.assertTrue((physical / 'companion/state.sqlite').is_file())
            self.assertFalse((logical / 'companion').exists())
            unrelated = self.run_script(directory / 'unrelated', extra=('-PackageDirectory', str(package)))
            self.assertNotEqual(unrelated.returncode, 0)


if __name__ == '__main__':
    unittest.main()
