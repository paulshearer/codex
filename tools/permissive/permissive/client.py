"""Signed API client. Admission signatures do not limit task/stream duration."""
import json
import os

from aiohttp import ClientSession, ClientTimeout
from yarl import URL
from .auth import Signer, client_key_path
from .credentials import default_home
from .privacy import validate_transport


class SignedSession:
    def __init__(self, home=None, private_key=None, scope="api", **options):
        variable = "PERMISSIVE_PRIVATE_KEY" if scope == "api" else "PERMISSIVE_BROWSER_PRIVATE_KEY"
        self.signer = Signer(private_key or os.environ.get(variable) or client_key_path(home or default_home(), scope))
        options.setdefault("timeout", ClientTimeout(total=None, connect=15, sock_read=None))
        self.headers = options.pop("headers", {})
        self.session = ClientSession(**options)

    async def __aenter__(self):
        await self.session.__aenter__()
        return self

    async def __aexit__(self, *args):
        return await self.session.__aexit__(*args)

    def _prepare(self, method, target, options):
        headers = {**self.headers, **options.pop("headers", {})}
        validate_transport(target)
        try:
            target = URL(target)
        except (ValueError, TypeError):
            raise ValueError("Invalid API connection URL") from None
        params = options.pop("params", None)
        if params is not None:
            target = target.extend_query(params)
        body = options.pop("data", None)
        value = options.pop("json", None)
        if value is not None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        if isinstance(body, str):
            body = body.encode("utf-8")
        if body is not None:
            if not isinstance(body, bytes):
                raise TypeError("Signed request bodies must be bytes or JSON")
            options["data"] = body
            headers.setdefault("Content-Type", "application/octet-stream")
        if method.upper() in {"POST", "PUT", "PATCH"}:
            headers.setdefault("Content-Type", "application/octet-stream")
        options["headers"] = self.signer.headers(method, str(target), body or b"", headers)
        # Redirects change the signed target; callers must sign the new request.
        options["allow_redirects"] = False
        return target

    def request(self, method, target, **options):
        target = self._prepare(method, target, options)
        return self.session.request(method, target, **options)

    def get(self, target, **options):
        return self.request("GET", target, **options)

    def post(self, target, **options):
        return self.request("POST", target, **options)

    def patch(self, target, **options):
        return self.request("PATCH", target, **options)

    def ws_connect(self, target, **options):
        target = self._prepare("GET", target, options)
        options.pop("allow_redirects")
        return self.session.ws_connect(target, **options)
