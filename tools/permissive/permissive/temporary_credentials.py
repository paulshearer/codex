"""Volatile website credentials. No persistent master key or public value-read API.

Clients seal to a single-use, two-minute RSA key. Accepted secrets are re-encrypted
under independent AES keys held only by this process. Metadata is workspace scoped.
The browser worker is trusted to redeem and fill; model-facing results contain IDs.
"""

import base64
import json
import secrets
import time
import uuid
from .website_scope import website_origin, website_site, allows_origin

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def identifier(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("Invalid credential scope or identifier")
    return value


def decode(value, maximum):
    if not isinstance(value, str) or len(value) > maximum * 2:
        raise ValueError("Invalid encrypted credential")
    raw = base64.b64decode(value, validate=True)
    if len(raw) > maximum:
        raise ValueError("Invalid encrypted credential")
    return raw


def validate_envelope(body):
    if not isinstance(body, dict) or set(body) != {'workspaceId', 'uploadId', 'wrappedKey', 'iv', 'ciphertext'}:
        raise ValueError("Expected an encrypted credential envelope")
    identifier(body['workspaceId'])
    identifier(body['uploadId'])
    if len(decode(body['wrappedKey'], 256)) != 256 or len(decode(body['iv'], 12)) != 12:
        raise ValueError("Invalid encrypted credential envelope")
    if len(decode(body['ciphertext'], 60000)) < 16:
        raise ValueError("Invalid encrypted credential envelope")


class TemporaryCredentials:
    def __init__(self, clock=time.monotonic, wall=time.time):
        self.clock, self.wall = clock, wall
        self.pending, self.entries = {}, {}

    def prune(self):
        now = self.clock()
        for ident, entry in list(self.pending.items()):
            if entry['deadline'] <= now:
                del self.pending[ident]
        for ident, entry in list(self.entries.items()):
            if entry['deadline'] is not None and entry['deadline'] <= now:
                self.remove(ident)

    def prepare(self, workspace):
        identifier(workspace)
        self.prune()
        if len(self.pending) >= 16 or len(self.entries) >= 100:
            raise ValueError("Temporary credential capacity reached; revoke an entry or wait two minutes")
        ident = str(uuid.uuid4())
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.pending[ident] = {'workspace': workspace, 'key': key, 'deadline': self.clock() + 120}
        public = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        return {'uploadId': ident, 'publicKey': base64.b64encode(public).decode(),
                'expiresAt': int(self.wall() + 120), 'algorithm': 'RSA-OAEP-256/A256GCM'}

    def accept(self, body):
        validate_envelope(body)
        workspace, ident = identifier(body['workspaceId']), identifier(body['uploadId'])
        self.prune()
        pending = self.pending.pop(ident, None)
        if pending is None or pending['workspace'] != workspace:
            raise ValueError("Credential handoff expired; open a new secure handoff")
        if len(self.entries) >= 100:
            raise ValueError("Temporary credential capacity reached")
        clear = None
        try:
            key = pending['key'].decrypt(decode(body['wrappedKey'], 256), padding.OAEP(
                mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
            if len(key) != 32:
                raise ValueError()
            iv = decode(body['iv'], 12)
            if len(iv) != 12:
                raise ValueError()
            clear = bytearray(AESGCM(key).decrypt(iv, decode(body['ciphertext'], 60000), (ident + '\n' + workspace).encode()))
            payload = json.loads(clear)
            if not isinstance(payload, dict) or set(payload) - {'label', 'origin', 'username', 'password', 'lifetime', 'hours'}:
                raise ValueError()
            for name, limit in [('label', 80), ('username', 2048), ('password', 8192)]:
                if not isinstance(payload.get(name), str) or not 0 < len(payload[name]) <= limit:
                    raise ValueError()
            if not payload['label'].strip() or any(ord(c) < 32 for c in payload['label']):
                raise ValueError()
            origin = website_origin(payload['origin'])
            lifetime, hours = payload.get('lifetime'), payload.get('hours')
            if lifetime not in {'session', 'hours'} or (lifetime == 'hours' and (type(hours) is not int or not 1 <= hours <= 168)):
                raise ValueError()
            expires = self.wall() + hours * 3600 if lifetime == 'hours' else None
            metadata = {'id': ident, 'workspaceId': workspace, 'label': payload['label'].strip(),
                        'origin': origin, 'lifetime': lifetime, 'expiresAt': int(expires) if expires else None}
            # Do not retain the client's envelope or its handoff private key.
            storage_key, nonce = bytearray(secrets.token_bytes(32)), secrets.token_bytes(12)
            secret = json.dumps({'username': payload['username'], 'password': payload['password']}).encode()
            self.entries[ident] = {'metadata': metadata, 'key': storage_key, 'iv': nonce,
                'ciphertext': AESGCM(bytes(storage_key)).encrypt(nonce, secret, ident.encode()),
                'deadline': self.clock() + hours * 3600 if expires else None}
            return dict(metadata)
        except Exception:
            # Crypto/parser errors must never include submitted values.
            raise ValueError("Invalid encrypted credential; check the fields and start a new handoff") from None
        finally:
            if clear is not None:
                clear[:] = b'\0' * len(clear)

    def list(self, workspace):
        identifier(workspace)
        self.prune()
        return {'data': [{**entry['metadata'], 'site': website_site(entry['metadata']['origin'])} for entry in self.entries.values()
                         if entry['metadata']['workspaceId'] == workspace]}

    def redeem(self, ident, workspace, url, form_origins=None):
        self.prune()
        entry = self.entries.get(ident)
        if entry is None or entry['metadata']['workspaceId'] != workspace:
            raise ValueError("Credential expired or was revoked; request a new secure handoff")
        # Sibling sign-in hosts are allowed only inside the same PSL registration,
        # including PRIVATE boundaries (e.g. separate github.io tenants).
        origin = website_origin(url)
        approved = entry['metadata']['origin']
        if not allows_origin(approved, origin):
            raise ValueError('Credential is restricted to a different HTTPS website or port')
        if form_origins is None:
            form_origins = [origin]
        if not isinstance(form_origins, list) or not 1 <= len(form_origins) <= 32:
            raise ValueError('Invalid sign-in form destinations')
        destinations = list(dict.fromkeys(website_origin(item) for item in form_origins))
        if any(not allows_origin(approved, item) for item in destinations):
            raise ValueError('Sign-in form submits outside the approved HTTPS website')
        raw = AESGCM(bytes(entry['key'])).decrypt(entry['iv'], entry['ciphertext'], ident.encode())
        expires = entry['metadata']['expiresAt']
        # A pending browser/extension call cannot hold a usable fill indefinitely.
        return {**json.loads(raw), 'origin': origin, 'formOrigins': destinations,
                'expiresAt': min(expires, int(self.wall() + 15)) if expires else int(self.wall() + 15)}

    def remove(self, ident, workspace=None):
        entry = self.entries.get(ident)
        if entry is not None and (workspace is None or entry['metadata']['workspaceId'] == workspace):
            entry['key'][:] = b'\0' * len(entry['key'])
            del self.entries[ident]

    def clear_workspace(self, workspace):
        for ident, entry in list(self.entries.items()):
            if entry['metadata']['workspaceId'] == workspace:
                self.remove(ident)
        for ident, entry in list(self.pending.items()):
            if entry['workspace'] == workspace:
                del self.pending[ident]

    def close(self):
        for ident in list(self.entries):
            self.remove(ident)
        self.pending.clear()
