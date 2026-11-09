import asyncio
import base64
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import uuid

from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from permissive.temporary_credentials import TemporaryCredentials, website_origin
from permissive.service import Service
from permissive.mobile import allowed
from permissive.guest_bridge import route
from signed_client import SignedTestClient, provision
from test_service import FakeRuntime


def seal(prepared, workspace, **overrides):
    key, iv = AESGCM.generate_key(bit_length=256), b'123456789012'
    payload = {'label': 'Work', 'origin': 'https://example.com', 'username': 'fixture-user',
               'password': 'fixture-password-秘密', 'lifetime': 'hours', 'hours': 4, **overrides}
    public = serialization.load_der_public_key(base64.b64decode(prepared['publicKey']))
    wrapped = public.encrypt(key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    data = AESGCM(key).encrypt(iv, json.dumps(payload).encode(), (prepared['uploadId'] + '\n' + workspace).encode())
    return {'workspaceId': workspace, 'uploadId': prepared['uploadId'],
            'wrappedKey': base64.b64encode(wrapped).decode(), 'iv': base64.b64encode(iv).decode(), 'ciphertext': base64.b64encode(data).decode()}


class TemporaryCredentialTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.
        self.vault = TemporaryCredentials(clock=lambda: self.now, wall=lambda: 1000 + self.now)
        self.workspace = str(uuid.uuid4())

    def add(self, **overrides):
        return self.vault.accept(seal(self.vault.prepare(self.workspace), self.workspace, **overrides))

    def test_fresh_keys_ciphertext_only_and_revoke_zeroes_key(self):
        first, second = self.add(), self.add()
        entries = self.vault.entries
        self.assertNotEqual(entries[first['id']]['key'], entries[second['id']]['key'])
        self.assertNotIn('fixture-password', repr(entries))
        self.assertNotIn('fixture-user', json.dumps(self.vault.list(self.workspace)))
        self.assertFalse(self.vault.pending)
        self.assertEqual(self.vault.redeem(first['id'], self.workspace, 'https://example.com')['password'], 'fixture-password-秘密')
        key = entries[first['id']]['key']
        self.vault.remove(first['id'], str(uuid.uuid4()))
        self.assertIn(first['id'], entries)
        self.vault.remove(first['id'], self.workspace)
        self.assertEqual(key, bytearray(32))
        with self.assertRaises(ValueError): self.vault.redeem(first['id'], self.workspace, 'https://example.com')

    def test_scope_https_site_and_port(self):
        entry = self.add()
        for origin in ['https://example.com.evil.com', 'https://notexample.com', 'http://example.com', 'https://example.com:444', 'https://example.com@evil.com', 'https://example.com/path']:
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                self.vault.redeem(entry['id'], self.workspace, origin)
        with self.assertRaises(ValueError): self.vault.redeem(entry['id'], str(uuid.uuid4()), 'https://example.com')
        self.assertEqual(website_origin('https://EXAMPLE.com:443/'), 'https://example.com')
        self.assertEqual(self.vault.redeem(entry['id'], self.workspace, 'https://login.example.com')['origin'], 'https://login.example.com')

    def test_related_sign_in_subdomains_and_form_destinations(self):
        entry = self.add(origin='https://www.aa.com')
        result = self.vault.redeem(entry['id'], self.workspace, 'https://login.aa.com', ['https://login.aa.com', 'https://auth.aa.com'])
        self.assertEqual(result['origin'], 'https://login.aa.com')
        self.assertEqual(result['formOrigins'], ['https://login.aa.com', 'https://auth.aa.com'])
        self.assertEqual(self.vault.list(self.workspace)['data'][0]['site'], 'aa.com')
        for destination in ['https://aa.com.evil.com', 'http://login.aa.com', 'https://login.aa.com:8443']:
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                self.vault.redeem(entry['id'], self.workspace, 'https://login.aa.com', [destination])

    def test_psl_private_tenants_wildcards_exceptions_and_exact_internal_hosts(self):
        from permissive.website_scope import allows_origin, registrable_domain
        for a,b in [('www.company.co.uk','login.company.co.uk'), ('alice.github.io','login.alice.github.io'), ('www.ck','login.www.ck')]:
            self.assertTrue(allows_origin('https://' + a, 'https://' + b))
        for a,b in [('alice.github.io','bob.github.io'),('company.co.uk','other.co.uk'), ('a.foo.ck','b.foo.ck'),
                    ('localhost','login.localhost'),('host.internal','login.host.internal'),('127.0.0.1','127.0.0.2'),
                    ('[::1]','[::2]'),('co.uk','login.co.uk')]:
            self.assertFalse(allows_origin('https://' + a, 'https://' + b), (a,b))
        self.assertIsNone(registrable_domain('foo.ck'))
        self.assertEqual(registrable_domain('a.foo.ck'), 'a.foo.ck')
        self.assertEqual(website_origin('https://bücher.de'), 'https://xn--bcher-kva.de')
        self.assertTrue(allows_origin('https://bücher.de', 'https://login.xn--bcher-kva.de'))

    def test_expiry_restart_session_and_pending_expiry(self):
        timed, session = self.add(hours=1), self.add(lifetime='session')
        self.now += 3600
        with self.assertRaises(ValueError): self.vault.redeem(timed['id'], self.workspace, 'https://example.com')
        self.assertEqual(len(self.vault.list(self.workspace)['data']), 1)
        self.assertIsNone(session['expiresAt'])
        fresh_process = TemporaryCredentials()
        self.assertEqual(fresh_process.list(self.workspace)['data'], [])
        pending = self.vault.prepare(self.workspace)
        envelope = seal(pending, self.workspace)
        self.now += 120
        with self.assertRaises(ValueError): self.vault.accept(envelope)
        self.vault.close()
        self.assertFalse(self.vault.entries)

    def test_tampering_replay_and_invalid_lifetimes_fail_closed(self):
        envelope = seal(self.vault.prepare(self.workspace), self.workspace)
        self.vault.accept(envelope)
        with self.assertRaises(ValueError): self.vault.accept(envelope)
        for field, value in [('ciphertext', 'AAAA'), ('iv', 'AAAA'), ('workspaceId', str(uuid.uuid4()))]:
            altered = seal(self.vault.prepare(self.workspace), self.workspace)
            altered[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError): self.vault.accept(altered)
        for duration in [0, -1, True, '4', 169]:
            with self.subTest(duration=duration), self.assertRaises(ValueError): self.add(hours=duration)

    @unittest.skipUnless(shutil.which('node'), 'Node required for real Web Crypto interoperability')
    def test_web_crypto_envelope_is_accepted_by_python(self):
        prepared = self.vault.prepare(self.workspace)
        helper = (Path(__file__).parents[1] / 'desktop/credential-envelope.mjs').as_uri()
        script = "import {sealCredential} from " + json.dumps(helper) + ";let raw='';for await (const chunk of process.stdin) raw+=chunk; const {prepared,workspace}=JSON.parse(raw);process.stdout.write(JSON.stringify(await sealCredential(prepared,workspace,{label:'Web',origin:'https://example.com',username:'unicode-用户',password:'secret-秘密',lifetime:'session'})));"
        result = subprocess.run(['node', '--input-type=module', '-e', script], input=json.dumps({'prepared': prepared, 'workspace': self.workspace}), text=True, capture_output=True, check=True)
        envelope = json.loads(result.stdout)
        self.assertNotIn('secret-秘密', result.stdout)
        saved = self.vault.accept(envelope)
        self.assertEqual(self.vault.redeem(saved['id'], self.workspace, 'https://example.com')['username'], 'unicode-用户')


class CredentialApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.signer = provision(self.home)
        self.browser_signer = provision(self.home, 'browser')
        self.service = Service(self.home, FakeRuntime())
        self.client = SignedTestClient(TestServer(self.service.app()), self.signer)
        await self.client.start_server()
        self.headers = {'X-Test-Sign': '1'}
        self.workspace = self.service.store.workspace_for_directory(str(self.home), 'dedicated')['id']

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def test_authenticated_handoff_browser_access_and_no_disk_secrets(self):
        response = await self.client.post('/v1/credentials/prepare', json={'workspaceId': self.workspace})
        self.assertEqual(response.status, 401)
        response = await self.client.post('/v1/credentials/prepare', json={'workspaceId': self.workspace}, headers=self.headers)
        prepared = await response.json()
        response = await self.client.post('/v1/credentials', json=seal(prepared, self.workspace), headers=self.headers)
        self.assertEqual(response.status, 200, await response.text())
        saved = await response.json()
        self.assertNotIn('password', saved)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        task, _ = self.service.store.submit({'workspaceId': self.workspace, 'workingDirectory': str(self.home), 'prompt': 'Sign in', 'browserMode': 'dedicated'})
        self.service.store.update(task['id'], status='running')
        body = {'taskId': task['id'], 'operation': 'fill', 'credentialId': saved['id'], 'url': 'https://example.com'}
        response = await self.client.post('/v1/browser/credentials', json=body, headers=self.headers)
        self.assertEqual(response.status, 401)  # API keys cannot redeem.
        self.client.signer = self.browser_signer
        response = await self.client.post('/v1/browser/credentials', json={**body, 'url': 'https://elsewhere.example'}, headers=self.headers)
        self.assertEqual(response.status, 400)
        response = await self.client.post('/v1/browser/credentials', json=body, headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['password'], 'fixture-password-秘密')
        response = await self.client.post('/v1/browser/credentials', json={**body, 'url':'https://login.example.com', 'formOrigins':['https://login.example.com','https://auth.example.com']}, headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['origin'], 'https://login.example.com')
        response = await self.client.post('/v1/browser/credentials', json={**body, 'formOrigins':['https://outside.example']}, headers=self.headers)
        self.assertEqual(response.status, 400)
        self.service.store.update(task['id'], status='completed')
        response = await self.client.post('/v1/browser/credentials', json=body, headers=self.headers)
        self.assertEqual(response.status, 400)
        self.client.signer = self.signer
        response = await self.client.delete('/v1/credentials/' + saved['id'] + '?workspaceId=' + self.workspace, headers=self.headers)
        self.assertEqual(response.status, 200)
        for path in self.home.rglob('*'):
            if path.is_file():
                self.assertNotIn(b'fixture-password', path.read_bytes(), str(path))

    async def test_host_proxies_encrypted_envelopes_and_stable_scope_only(self):
        calls = []
        class Transport:
            async def request(_, method, path, body):
                calls.append((method, path, body))
                return {'proxied': True}
        self.service.runtime.role = 'controller'
        self.service.runtime.transport = Transport()
        body = seal(TemporaryCredentials().prepare(self.workspace), self.workspace)
        response = await self.client.post('/v1/credentials', json=body, headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(calls[-1], ('POST', '/v1/credentials', body))
        self.assertFalse(self.service.credentials.entries)
        self.assertFalse(self.service.credentials.pending)
        response = await self.client.post('/v1/credentials', json={'workspaceId': self.workspace, 'username': 'never-stage', 'password': 'never-stage'}, headers=self.headers)
        self.assertEqual(response.status, 400)
        self.assertEqual(len(calls), 1)
        # Neither phone nor PowerShell Direct can call the worker's secret-read route.
        self.assertFalse(allowed('POST', '/v1/browser/credentials'))
        from permissive.guest_bridge import BridgeError
        with self.assertRaises(BridgeError): route({'method': 'POST', 'path': '/v1/browser/credentials', 'body': {}})


if __name__ == '__main__': unittest.main()
