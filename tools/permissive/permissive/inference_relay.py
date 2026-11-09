"""Host-side, fixed-destination inference relay for an isolated guest."""

import argparse
import asyncio
import datetime
import ipaddress
import json
from pathlib import Path
import re
import ssl
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, web

from .privacy import INFERENCE_ENDPOINT, http_server_options, validate_inference_endpoint
from .vm_runtime import atomic_json


MAX_REQUEST_BYTES = 64 * 1024 * 1024
MAX_JSON_DEPTH = 64
MAX_JSON_ITEMS = 100_000
MAX_MEDIA_PARTS = 4096
MAX_INFLIGHT_REQUESTS = 4
REQUEST_UPLOAD_TIMEOUT = 30
_JSON_DELIMITERS = re.compile(rb'["\\{}\[\],]')
_OPAQUE_FILE_ID = re.compile(r'[A-Za-z0-9_-]{1,256}\Z')
_INLINE_BASE64 = re.compile(r'[A-Za-z0-9+/\r\n]*={0,2}\Z')
_MEDIA_URL_FIELDS = ('image_url', 'file_url', 'audio_url', 'video_url')
_MEDIA_SOURCE_FIELDS = _MEDIA_URL_FIELDS + ('file_id', 'file_data')
_MEDIA_TYPES = frozenset(('input_image', 'image_url', 'input_file', 'file',
                          'input_audio', 'audio_url', 'input_video', 'video_url'))


def _invalid_media():
    # Never include a caller's URL, file name, or content in diagnostics.
    return web.HTTPBadRequest(text='Inference media requires inline data or an uploaded file identifier')


def _inline_data_url(value):
    if not isinstance(value, str) or not value.startswith('data:'):
        return False
    comma = value.find(',', 5, 261)
    # The data scheme has no network authority. Bound the header without
    # decoding/copying an image payload, which the inference backend handles.
    return comma >= 5 and all(32 < ord(char) < 127 for char in value[5:comma])


def _validate_media_item(item):
    for field in _MEDIA_URL_FIELDS:
        if field not in item:
            continue
        value = item[field]
        # Chat-compatible image_url forms use {url, detail}; Responses uses
        # image_url directly. Neither form can ask the backend to fetch a URL.
        if isinstance(value, dict):
            value = value.get('url')
        if not _inline_data_url(value):
            raise _invalid_media()
    if 'file_id' in item:
        value = item['file_id']
        if not isinstance(value, str) or not _OPAQUE_FILE_ID.fullmatch(value):
            raise _invalid_media()
    if 'file_data' in item:
        value = item['file_data']
        if not isinstance(value, str) or not value or not (
                _inline_data_url(value) or _INLINE_BASE64.fullmatch(value)):
            raise _invalid_media()


def validate_request_media(payload):
    """Inspect structured input content, not prompt strings or tool schemas.

    Native Responses requests put media in message.content and in structured
    function_call_output.output arrays. Chat-compatible messages/file wrappers
    are covered as well. Tool arguments and text remain opaque strings.
    """
    pending = [payload.get('input'), payload.get('messages')]
    media_parts = 0
    while pending:
        node = pending.pop()
        if isinstance(node, list):
            pending.extend(node)
        elif isinstance(node, dict):
            kind = node.get('type')
            if (isinstance(kind, str) and kind in _MEDIA_TYPES) or any(field in node for field in _MEDIA_SOURCE_FIELDS):
                media_parts += 1
                if media_parts > MAX_MEDIA_PARTS:
                    raise web.HTTPRequestEntityTooLarge(max_size=MAX_MEDIA_PARTS, actual_size=media_parts,
                                                        text='Too many inference media parts')
                _validate_media_item(node)
            # Only protocol content containers are traversed. In particular,
            # tool JSON schemas/metadata and strings containing JSON are data.
            for field in ('input', 'messages', 'content', 'output', 'file'):
                child = node.get(field)
                if isinstance(child, (dict, list)):
                    pending.append(child)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def _invalid_json_constant(value):
    raise ValueError('Non-JSON numeric constant')


def validate_response_request(body):
    """Bound parsing before allocating a JSON object graph, then check media."""
    if len(body) > MAX_REQUEST_BYTES:
        raise web.HTTPRequestEntityTooLarge(max_size=MAX_REQUEST_BYTES, actual_size=len(body),
                                            text='Inference request exceeds the body limit')
    depth = items = 0
    in_string = False
    escaped_position = -1
    # Base64 images contain none of these delimiters, so the regex skips their
    # large byte strings in C. The iterator never allocates a token list.
    for token in _JSON_DELIMITERS.finditer(body):
        char, position = token[0], token.start()
        if in_string:
            if position == escaped_position:
                escaped_position = -1
                continue
            escaped_position = -1
            if char == b'\\':
                escaped_position = position + 1
            elif char == b'"':
                in_string = False
        elif char == b'"':
            in_string = True
        elif char in (b'{', b'['):
            depth += 1
            items += 1
        elif char in (b'}', b']'):
            depth -= 1
        elif char == b',':
            items += 1
        if depth > MAX_JSON_DEPTH or items > MAX_JSON_ITEMS:
            raise web.HTTPRequestEntityTooLarge(max_size=MAX_JSON_ITEMS, actual_size=items,
                                                text='Inference JSON exceeds its structural budget')
    try:
        payload = json.loads(body.decode('utf-8'), object_pairs_hook=_unique_object,
                             parse_constant=_invalid_json_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise web.HTTPBadRequest(text='Inference request must be a JSON object') from None
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(text='Inference request must be a JSON object')
    validate_request_media(payload)


def initialize_tls(binding_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    path = Path(binding_path).resolve(strict=True)
    binding = json.loads(path.read_text(encoding='utf-8-sig'))
    root = path.parent / 'relay-tls'
    root.mkdir(parents=True, exist_ok=True)
    certificate, private = root / 'certificate.pem', root / 'private-key.pem'
    if not certificate.exists() or not private.exists():
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.datetime.now(datetime.timezone.utc)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'permissive-gateway.internal')])
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now-datetime.timedelta(minutes=5)).not_valid_after(now+datetime.timedelta(days=3650))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName('permissive-gateway.internal'),
                    x509.IPAddress(ipaddress.ip_address(binding['gatewayIp']))]), critical=False)
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                .sign(key, hashes.SHA256()))
        private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    binding['relayTls'] = {'certificate': str(certificate), 'key': str(private)}
    binding.setdefault('relayListenUrl', 'https://' + binding['gatewayIp'] + ':18445')
    atomic_json(path, binding)
    return {'certificatePath': str(certificate)}


def application(binding, session_factory=ClientSession):
    upstream = binding.get('upstreamEndpoint', binding.get('inferenceEndpoint', INFERENCE_ENDPOINT)).rstrip('/')
    validate_inference_endpoint(upstream)
    # Disable decompression before request parsing: a compressed upload must
    # not expand beyond the body budget inside aiohttp's HTTP parser.
    app = web.Application(client_max_size=MAX_REQUEST_BYTES,
                          handler_args={'auto_decompress': False})
    slots = asyncio.Semaphore(MAX_INFLIGHT_REQUESTS)

    async def lifecycle(app):
        async with session_factory(timeout=ClientTimeout(total=None, connect=15, sock_read=None)) as client:
            app['upstream'] = client
            yield

    async def relay(request):
        if request.remote != binding['guestIp']:
            raise web.HTTPForbidden(text='This relay is bound to its guest')
        if request.query_string or (request.method, request.path) not in {
                ('POST', '/v1/responses'), ('GET', '/v1/models')}:
            raise web.HTTPNotFound(text='Unknown inference route')
        if slots.locked():
            raise web.HTTPTooManyRequests(text='Inference relay capacity is in use')
        async with slots:
            return await forward(request)

    async def forward(request):
        if request.headers.get('Content-Encoding', '').lower() not in {'', 'identity'}:
            raise web.HTTPUnsupportedMediaType(text='Inference uploads must be uncompressed JSON')
        if request.method == 'POST' and request.content_type != 'application/json':
            raise web.HTTPUnsupportedMediaType(text='Inference requests require application/json')
        try:
            body = await asyncio.wait_for(request.read(), timeout=REQUEST_UPLOAD_TIMEOUT)
        except asyncio.TimeoutError:
            raise web.HTTPRequestTimeout(text='Inference request upload timed out') from None
        if request.method == 'POST':
            await asyncio.to_thread(validate_response_request, body)
        elif body:
            raise web.HTTPBadRequest(text='Model listing does not accept a request body')
        headers = {'Content-Type': 'application/json'}
        # Upstream access is a host profile, never a guest-selected target/key.
        if binding.get('upstreamCredentialFile'):
            from .credentials import protected
            key = protected(Path(binding['upstreamCredentialFile']).read_bytes(), decrypt=True).decode()
            headers['Authorization'] = 'Bearer ' + key
        target = upstream + request.path.removeprefix('/v1')
        try:
            async with app['upstream'].request(request.method, target, data=body,
                    headers=headers, allow_redirects=False) as response:
                if 300 <= response.status < 400:
                    raise web.HTTPBadGateway(text='Inference redirects are not supported')
                stream = web.StreamResponse(status=response.status, headers={
                    'Content-Type': response.headers.get('Content-Type', 'application/json'),
                    'Cache-Control': 'no-store'})
                await stream.prepare(request)
                async for chunk in response.content.iter_any():
                    await stream.write(chunk)
                return stream
        except (OSError, asyncio.TimeoutError, ClientError):
            raise web.HTTPBadGateway(text='Inference upstream unavailable') from None

    app.cleanup_ctx.append(lifecycle)
    app.router.add_route('*', '/{path:.*}', relay)
    return app


async def start_relay(binding):
    url = urlsplit(binding['relayListenUrl'])
    if url.scheme != 'https' or url.hostname != binding['gatewayIp'] or url.port != 18445:
        raise ValueError('Relay must bind its configured guest gateway over TLS port 18445')
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(binding['relayTls']['certificate'], binding['relayTls']['key'])
    runner = web.AppRunner(application(binding), handler_cancellation=True, **http_server_options())
    await runner.setup()
    try:
        await web.TCPSite(runner, url.hostname, url.port, ssl_context=context).start()
    except BaseException:
        await runner.cleanup()
        raise
    return runner


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--deployment', type=Path, required=True)
    parser.add_argument('--initialize-tls', action='store_true')
    args = parser.parse_args()
    if args.initialize_tls:
        print(json.dumps(initialize_tls(args.deployment)))
        return
    binding = json.loads(args.deployment.read_text(encoding='utf-8-sig'))
    async def serve():
        runner = await start_relay(binding)
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()
    asyncio.run(serve())


if __name__ == '__main__':
    main()
