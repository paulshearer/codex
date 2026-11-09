import subprocess
import sys
import unittest


class NativeBrowserHostTests(unittest.TestCase):
    def test_service_disconnect_exits_even_while_browser_stdin_remains_open(self):
        # Actual process teardown is essential: cancelling to_thread alone cannot
        # stop a pipe read, and asyncio.run waits for its default executor.
        script = '''
import asyncio
import permissive.browser_bridge as bridge
class Socket:
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    def __aiter__(self): return self
    async def __anext__(self):
        await asyncio.sleep(.2)
        raise StopAsyncIteration
    async def close(self): pass
class Session:
    def __init__(self, **args): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    def ws_connect(self, *args, **kwargs): return Socket()
bridge.SignedSession = Session
asyncio.run(bridge.native_host())
'''
        child = subprocess.Popen([sys.executable, '-B', '-c', script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            child.wait(timeout=5)
            self.assertEqual(child.returncode, 0, child.stderr.read().decode())
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            for stream in (child.stdin, child.stdout, child.stderr): stream.close()
