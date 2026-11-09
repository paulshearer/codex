import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import subprocess

from permissive.browser_settings import EXTENSION_ID, installed_profiles, open_installed_profile, setup_installed_profile, browser_catalog, browser_settings
from permissive.browser_bridge import BrowserBridge
import test_workspaces


class InstalledProfilesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.local = Path(self.temporary.name)
        self.root = self.local / 'Microsoft/Edge/User Data'
        self.root.mkdir(parents=True)
        self.cache = {'Default': {'name': 'Paul', 'shortcut_name': 'Personal', 'user_name': 'personal@example.com'},
                      'Profile 1': {'name': 'Paul', 'user_name': 'work@example.com'},
                      'Profile 2': {'name': 'Paul', 'user_name': 'other@example.com'},
                      '../escape': {'name': 'Unsafe'}, 'Removed': {'name': 'Removed'}}
        for directory in ['Default', 'Profile 1', 'Profile 2']:
            (self.root / directory).mkdir()
        (self.root / 'Local State').write_text(json.dumps({'profile': {'info_cache': self.cache}}))
        self.environment = patch.dict('os.environ', {'LOCALAPPDATA': str(self.local)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        executable = self.local / 'Microsoft/Edge/Application/msedge.exe'
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b'fixture')

    def test_discovery_disambiguates_accounts_and_keeps_stable_ids(self):
        profiles = installed_profiles('msedge')
        self.assertEqual([p['directory'] for p in profiles], ['Default', 'Profile 1', 'Profile 2'])
        self.assertEqual(profiles[0]['name'], 'Personal')
        self.assertEqual(profiles[1]['account'], 'work@example.com')
        self.assertEqual(len({p['id'] for p in profiles}), 3)
        self.assertEqual(profiles, installed_profiles('msedge'))
        bridge = BrowserBridge()
        bridge.connections['edge'] = object()
        bridge.metadata['edge'] = {'browser': 'msedge', 'profileId': 'extension-uuid', 'localProfileId': profiles[1]['id']}
        catalog = browser_catalog(self.local, bridge)
        actual = next(p for p in catalog['data'] if p['id'] == 'msedge')['installedProfiles']
        self.assertEqual([p['connected'] for p in actual], [False, True, False])
        browser_settings({'browser': 'msedge', 'browserProfile': profiles[1]['id'], 'browserMode': 'attached'})
        with self.assertRaises(ValueError):
            browser_settings({'browser': 'msedge', 'browserProfile': profiles[1]['id'], 'browserMode': 'dedicated'})

    def test_connect_opens_exact_profile_without_browser_automation_flags(self):
        ident = installed_profiles('msedge')[1]['id']
        with patch('permissive.browser_settings.subprocess.Popen') as launch:
            setup = open_installed_profile('msedge', ident)
            command = launch.call_args.args[0]
            self.assertEqual(command[1:], ['--profile-directory=Profile 1', 'edge://extensions/'])
            self.assertTrue(setup['setupRequired'])
            (self.root / 'Profile 1/Secure Preferences').write_text(json.dumps({'extensions': {'settings': {EXTENSION_ID: {'state': 1}}}}))
            setup = open_installed_profile('msedge', ident)
            command = launch.call_args.args[0]
            self.assertEqual(command[1], '--profile-directory=Profile 1')
            self.assertIn('options.html#localProfileId=' + ident, command[2])
            self.assertFalse(setup['setupRequired'])
            with self.assertRaises(ValueError):
                open_installed_profile('msedge', '../Default')
            self.assertEqual(launch.call_count, 2)

    def test_missing_or_corrupt_profile_metadata_is_tolerated(self):
        (self.root / 'Local State').write_text('partial json')
        self.assertEqual(installed_profiles('msedge'), [])
        self.assertEqual(installed_profiles('chromium'), [])

    def test_automatic_setup_uses_only_discovered_profile_and_packaged_extension(self):
        extension = self.local / 'package/browser/extension'
        extension.mkdir(parents=True)
        helper = self.local / 'package/native/browser-extension-setup.exe'
        helper.parent.mkdir(); helper.write_bytes(b'fixture')
        profile_id = installed_profiles('msedge')[1]['id']
        with patch('permissive.browser_settings.extension_directory', return_value=extension), patch('permissive.browser_settings.subprocess.run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, '{"extensionLoaded":true,"paired":true}', '')
            report = setup_installed_profile('msedge', profile_id)
            self.assertTrue(report['paired'])
            arguments = run.call_args.args[0]
            self.assertEqual(arguments[0], str(helper))
            self.assertEqual(arguments[2:5], ['Profile 1', str(extension), profile_id])
            self.assertEqual(run.call_args.kwargs['timeout'], 90)
            self.assertEqual(run.call_args.kwargs['stdin'], subprocess.DEVNULL)
            with self.assertRaises(ValueError): setup_installed_profile('msedge', '../other')
            self.assertEqual(run.call_count, 1)
            run.return_value = subprocess.CompletedProcess([], 0,
                'Opening in existing browser session.\n{"enabledDeveloperMode":true,"extensionLoaded":true,"paired":true}\n', '')
            self.assertTrue(setup_installed_profile('msedge', profile_id)['paired'])
            for output in ['', 'Opening in existing browser session.\n', '{broken json}', '[]', '{}',
                           '{"extensionLoaded":"true","paired":"true"}', '{"error":123}',
                           '{"extensionLoaded":true,"paired":true}\nUnexpected trailing output']:
                with self.subTest(output=output):
                    run.return_value = subprocess.CompletedProcess([], 0, output, '')
                    with self.assertRaisesRegex(ValueError, 'could not finish'):
                        setup_installed_profile('msedge', profile_id)
            run.return_value = subprocess.CompletedProcess([], 1, '{"error":"Policy prevented setup"}', '')
            with self.assertRaisesRegex(ValueError, 'Policy prevented'): setup_installed_profile('msedge', profile_id)
            run.side_effect = subprocess.TimeoutExpired([], 90)
            with self.assertRaisesRegex(ValueError, 'timed out'): setup_installed_profile('msedge', profile_id)

    @unittest.skipUnless(os.name == 'nt', 'Windows service handle regression')
    def test_automatic_setup_does_not_inherit_an_invalid_service_stdin_handle(self):
        import ctypes
        extension = self.local / 'package/browser/extension'
        extension.mkdir(parents=True)
        helper = self.local / 'package/native/browser-extension-setup.exe'
        helper.parent.mkdir(); helper.write_bytes(b'fixture')
        profile_id = installed_profiles('msedge')[1]['id']
        run_subprocess = subprocess.run

        def harmless_json_helper(arguments, **kwargs):
            return run_subprocess([sys.executable, '-c', 'print(\'{"extensionLoaded":true,"paired":true}\')'], **kwargs)

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetStdHandle.argtypes = [ctypes.c_ulong]
        kernel.GetStdHandle.restype = ctypes.c_void_p
        kernel.SetStdHandle.argtypes = [ctypes.c_ulong, ctypes.c_void_p]
        kernel.SetStdHandle.restype = ctypes.c_int
        original = kernel.GetStdHandle(ctypes.c_ulong(-10))
        try:
            self.assertTrue(kernel.SetStdHandle(ctypes.c_ulong(-10), ctypes.c_void_p(0xDEADBEEF)))
            with patch('permissive.browser_settings.extension_directory', return_value=extension), patch(
                    'permissive.browser_settings.subprocess.run', side_effect=harmless_json_helper):
                self.assertTrue(setup_installed_profile('msedge', profile_id)['paired'])
        finally:
            kernel.SetStdHandle(ctypes.c_ulong(-10), original)


class ProfileConnectApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_workspaces.WorkspaceTests.asyncSetUp
    asyncTearDown = test_workspaces.WorkspaceTests.asyncTearDown
    api = test_workspaces.WorkspaceTests.api

    async def test_connect_endpoint_is_signed_and_validates_request(self):
        with patch('permissive.service.open_installed_profile', return_value={'setupRequired': True}) as opened:
            result = await self.api('POST', '/v1/browsers/connect', {'browser': 'msedge', 'profileId': 'local-fixture'})
            self.assertTrue(result['setupRequired'])
            opened.assert_called_once_with('msedge', 'local-fixture')
            response = await self.client.post('/v1/browsers/connect', json={'browser': 'msedge', 'profileId': 'local-fixture'})
            self.assertEqual(response.status, 401)
            response = await self.client.post('/v1/browsers/connect', json={'browser': 'msedge', 'profileId': 'fixture', 'directory': 'arbitrary'}, headers=self.headers)
            self.assertEqual(response.status, 400)

    async def test_automatic_setup_requires_explicit_confirmation_and_reports_connection(self):
        with patch('permissive.service.setup_installed_profile', return_value={'paired': True}) as setup:
            for confirmation in [False, 'true', 1, None]:
                response = await self.client.post('/v1/browsers/setup', json={'browser': 'msedge', 'profileId': 'local-fixture', 'confirmed': confirmation}, headers=self.headers)
                self.assertEqual(response.status, 400)
            setup.assert_not_called()
            self.service.browser.connections['fixture'] = object()
            self.service.browser.metadata['fixture'] = {'browser': 'msedge', 'localProfileId': 'local-fixture'}
            try:
                result = await self.api('POST', '/v1/browsers/setup', {'browser': 'msedge', 'profileId': 'local-fixture', 'confirmed': True})
                self.assertTrue(result['connected'])
                setup.assert_called_once_with('msedge', 'local-fixture')
            finally:
                self.service.browser.connections.clear()
            response = await self.client.post('/v1/browsers/connect', json={'browser': 'msedge'}, headers=self.headers)
            self.assertEqual(response.status, 400)
