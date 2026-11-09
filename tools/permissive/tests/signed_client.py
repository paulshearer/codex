import json
from pathlib import Path
from aiohttp.test_utils import TestClient
from yarl import URL
from permissive.auth import Signer, authorize, generate


def provision(home, scope="api"):
    private = Path(home) / "test-clients" / (scope + ".pem")
    public = private.with_suffix(".pub")
    generate(private, public)
    authorize(home, public.read_bytes(), "Test " + scope, (scope,))
    return Signer(private)


class SignedTestClient(TestClient):
    def __init__(self, server, signer, **kwargs):
        super().__init__(server, **kwargs)
        self.signer = signer

    def sign(self, method, path, kwargs):
        headers = dict(kwargs.get("headers") or {})
        if headers.pop("X-Test-Sign", None):
            body = kwargs.pop("json", None)
            if body is not None:
                kwargs["data"] = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
            data = kwargs.get("data", b"")
            if data:
                headers.setdefault("Content-Type", "application/octet-stream")
            if method.upper() in {"POST", "PUT", "PATCH"}:
                headers.setdefault("Content-Type", "application/octet-stream")
            headers = self.signer.headers(method, str(path), data, headers)
        kwargs["headers"] = headers

    async def _request(self, method, path, **kwargs):
        path = URL(path)
        self.sign(method, path, kwargs)
        return await super()._request(method, path, **kwargs)

    async def _ws_connect(self, path, **kwargs):
        path = URL(path)
        self.sign("GET", path, kwargs)
        return await super()._ws_connect(path, **kwargs)
