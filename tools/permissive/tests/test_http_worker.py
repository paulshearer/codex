import asyncio
import json
import unittest

from aiohttp import ClientSession, DummyCookieJar, web
from aiohttp.test_utils import TestServer
from permissive.http_worker import HttpWorker


class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        async def handler(request):
            self.requests.append((request.method, request.path))
            if request.path == '/login':
                self.assertEqual(await request.json(), {'password': 'fixture-only'})
                result = web.json_response({'token': 'fixture-sensitive-token', 'nested': {'password': 'hidden'}})
                result.set_cookie('session', 'fixture-cookie')
                return result
            if request.path == '/private':
                return web.json_response({'authorized': request.headers.get('Authorization') == 'Bearer fixture-sensitive-token',
                                          'cookie': request.cookies.get('session', '')})
            if request.path == '/large': return web.Response(text='x'*10000)
            if request.path == '/slow':
                await asyncio.sleep(.3)
                return web.json_response({'ok': True})
            if request.path == '/redirect': raise web.HTTPFound('/private?token=hidden')
            if request.path == '/null': return web.json_response(None)
            return web.json_response({'ok': True})
        self.servers = []
        for _ in range(2):
            app = web.Application(); app.router.add_route('*', '/{path:.*}', handler)
            server = TestServer(app); await server.start_server(); self.servers.append(server)
        self.session = ClientSession(cookie_jar=DummyCookieJar())
        self.worker = HttpWorker(self.session)

    async def asyncTearDown(self):
        await self.session.close()
        for server in self.servers: await server.close()

    def args(self, path, server=0, **kwargs):
        return {'url': str(self.servers[server].make_url(path)), **kwargs}

    async def call(self, path, **kwargs):
        return await self.worker.request(self.args(path, **kwargs))

    async def test_auth_references_and_cookies_are_scoped_to_exact_origin(self):
        result = await self.call('/login', method='POST', json={'password': 'fixture-only'})
        self.assertNotIn('fixture-sensitive-token', json.dumps(result))
        self.assertNotIn('hidden', json.dumps(result))
        reference = {**result['json']['token'], 'prefix': 'Bearer '}
        result = await self.call('/private', headers={'Authorization': reference})
        self.assertTrue(result['json']['authorized'])
        self.assertEqual(result['json']['cookie'], 'fixture-cookie')
        result = await self.call('/private', server=1)
        self.assertEqual(result['json']['cookie'], '')
        result = await self.worker.call(self.args('/private', server=1, headers={'Authorization': reference}))
        self.assertTrue(result['isError'])
        self.assertIn('not_started', str(result))

    async def test_bounds_redirects_and_mutation_timeout(self):
        result = await self.call('/large', maxResponseBytes=256)
        self.assertTrue(result['truncated']); self.assertEqual(len(result['text']), 256)
        result = await self.call('/redirect')
        self.assertEqual(result['status'], 302)
        self.assertNotIn('token=', result['headers']['location'])
        self.assertNotIn(('GET', '/private'), self.requests)
        result = await self.worker.call(self.args('/slow', method='POST', timeoutSeconds=.1))
        self.assertTrue(result['isError']); self.assertIn('unknown; inspect state', str(result))

    async def test_invalid_arguments_never_start_network_request(self):
        for extra in ({'headers': {'Content-Length': '100'}}, {'headers': {'X': 'a\r\nb'}},
                      {'headers': {'X': {'secretRef': 'unknown'}}}, {'timeoutSeconds': True},
                      {'maxResponseBytes': 2}, {'json': {}, 'body': 'x'}):
            self.assertTrue((await self.worker.call(self.args('/bad', **extra)))['isError'])
        self.assertEqual(self.requests, [])
