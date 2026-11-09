"""Dedicated cancellation-safe inference lane; model containers remain independently managed."""

import argparse
import asyncio
import ipaddress
import json
import logging
from pathlib import Path
import re
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, web

MODEL = "/models/deepseek-v4-flash"
CLIENT = web.AppKey("client", ClientSession)


def http_server_options():
    # The gateway is deployed as a standalone file without the desktop package.
    logger = logging.getLogger("permissive.private_http")
    logger.disabled = True
    logger.propagate = False
    return {"access_log": None, "logger": logger, "debug": False}


def application(upstream, credential, maintenance, heartbeat=15):
    try:
        parsed = urlsplit(upstream)
        loopback = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback
        parsed.port  # Validate the port before admitting requests.
    except ValueError:
        raise ValueError("Gateway upstream must be a loopback HTTP origin") from None
    if not loopback or parsed.scheme != "http" or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("Gateway upstream must be a loopback HTTP origin")
    upstream = upstream.rstrip("/")
    slots = asyncio.Semaphore(2)
    app = web.Application(client_max_size=32 * 1024 * 1024, handler_args=http_server_options())

    async def lifecycle(app):
        app[CLIENT] = ClientSession(
            timeout=ClientTimeout(total=None, connect=15, sock_read=None)
        )
        yield
        await app[CLIENT].close()

    def backend_error(status):
        return {
            "type": "upstream_error",
            "code": str(status),
            "message": f"Local inference returned HTTP {status}",
        }

    async def proxy(request):
        if maintenance.exists():
            raise web.HTTPServiceUnavailable(text="DeepSeek is in maintenance")
        route = request.match_info.get("route", "health")
        if route not in {"health", "v1/models", "v1/responses", "v1/chat/completions"}:
            raise web.HTTPNotFound()
        headers = {
            "Authorization": "Bearer " + credential,
            "Content-Type": "application/json",
        }
        body = await request.json() if request.method == "POST" else None
        if body and body.get("model") != MODEL:
            raise web.HTTPBadRequest(text="Only the local DeepSeek model is available")
        if request.method == "GET" or not body.get("stream"):
            async with slots:
                async with app[CLIENT].request(
                    request.method, upstream + "/" + route, json=body, headers=headers, allow_redirects=False
                ) as response:
                    if response.status >= 300:
                        return web.json_response(
                            {"error": backend_error(response.status)},
                            status=response.status if response.status >= 400 else 502,
                        )
                    return web.Response(
                        status=response.status,
                        body=await response.read(),
                        headers={
                            "Content-Type": response.headers.get(
                                "Content-Type", "application/json"
                            )
                        },
                    )
        stream = web.StreamResponse(
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}
        )
        await stream.prepare(request)
        chunks = asyncio.Queue(maxsize=16)

        async def inference():
            try:
                async with slots:
                    async with app[CLIENT].post(
                        upstream + "/" + route, json=body, headers=headers, allow_redirects=False
                    ) as response:
                        if response.status >= 300:
                            await chunks.put(
                                (
                                    "event: error\ndata: "
                                    + json.dumps(
                                        {
                                            "type": "error",
                                            "error": backend_error(response.status),
                                        }
                                    )
                                    + "\n\n"
                                ).encode()
                            )
                        else:
                            async for chunk in response.content.iter_any():
                                await chunks.put(chunk)
            except Exception:
                await chunks.put(
                    (
                        "event: error\ndata: "
                        + json.dumps(
                            {
                                "type": "error",
                                "error": {
                                    "type": "connection_error",
                                    "message": "Local inference connection failed",
                                },
                            }
                        )
                        + "\n\n"
                    ).encode()
                )
            finally:
                await chunks.put(None)

        worker = asyncio.create_task(inference())
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(chunks.get(), heartbeat)
                except TimeoutError:
                    await stream.write(b": processing\n\n")
                    continue
                if chunk is None:
                    break
                await stream.write(chunk)
        finally:
            worker.cancel()
            # A cancelled producer must not block on a full queue during cleanup.
            while not chunks.empty():
                chunks.get_nowait()
            await asyncio.gather(worker, return_exceptions=True)
        return stream

    app.cleanup_ctx.append(lifecycle)
    app.router.add_route("*", "/{route:.*}", proxy)
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy-directory", type=Path, required=True)
    parser.add_argument("--upstream", default="http://127.0.0.1:8888")
    parser.add_argument("--port", type=int, default=8003)
    args = parser.parse_args()
    config = (args.proxy_directory / "nginx.conf").read_text()
    matches = re.findall(
        r'proxy_set_header\s+Authorization\s+"Bearer ([^"\r\n]+)";', config
    )
    if len(matches) != 1:
        raise SystemExit(
            "Expected one backend credential in the existing private proxy config"
        )
    web.run_app(
        application(args.upstream, matches[0], args.proxy_directory / "maintenance"),
        host="127.0.0.1",
        port=args.port,
        print=None,
        **http_server_options(),
        handler_cancellation=True,
    )


if __name__ == "__main__":
    main()
