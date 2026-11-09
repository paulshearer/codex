"""Ed25519 HTTP Message Signatures; public-key authorization and durable replay checks.

This application's strict RFC 9421 profile signs @method, @path, @query,
content-digest, and any content-type/idempotency-key/last-event-id fields.
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import time
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from .credentials import default_home, protected

OPTIONAL_FIELDS = ("content-type", "idempotency-key", "last-event-id")
BASE_FIELDS = ("@method", "@path", "@query", "content-digest")
PROFILE = re.compile(r'^permissive=(\((?:"[@a-z-]+" ?)+\));created=([0-9]{1,12});expires=([0-9]{1,12});nonce="([A-Za-z0-9_-]{22,64})";keyid="([0-9a-f]{64})";alg="ed25519"$')


def fingerprint(key):
    return hashlib.sha256(key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).hexdigest()


def public_key(value):
    if not isinstance(value, (str, bytes)):
        raise ValueError("Invalid public key")
    raw = value.encode() if isinstance(value, str) else value
    key = serialization.load_ssh_public_key(raw) if raw.startswith(b"ssh-ed25519 ") else serialization.load_pem_public_key(raw)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("Only Ed25519 public keys are supported")
    return key


def registry_path(home):
    return Path(home) / "auth" / "authorized-keys.json"


def registry(home):
    path = registry_path(home)
    if not path.exists():
        return {"version": 1, "keys": []}
    if path.stat().st_size > 256 * 1024:
        raise ValueError("Public-key registry is too large")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("keys"), list) or len(value["keys"]) > 256:
        raise ValueError("Invalid public-key registry")
    seen = set()
    for item in value["keys"]:
        if not isinstance(item, dict) or not isinstance(item.get("scopes"), list):
            raise ValueError("Invalid authorized public key")
        key = public_key(item["publicKey"])
        if item["id"] != fingerprint(key) or item["id"] in seen or not item["scopes"] or not set(item["scopes"]) <= {"api", "browser"}:
            raise ValueError("Invalid authorized public key")
        seen.add(item["id"])
    return value


def write_registry(home, value):
    path = registry_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(8))
    try:
        with os.fdopen(os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def authorize(home, value, name, scopes=("api",)):
    key = public_key(value)
    if not name.strip() or len(name) > 120 or not set(scopes) <= {"api", "browser"} or not scopes:
        raise ValueError("A name and valid scopes are required")
    current = registry(home)
    ident = fingerprint(key)
    entry = {"id": ident, "name": name, "scopes": list(dict.fromkeys(scopes)),
             "publicKey": key.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()}
    current["keys"] = [item for item in current["keys"] if item["id"] != ident] + [entry]
    write_registry(home, current)
    return entry


def revoke(home, ident):
    current = registry(home)
    remaining = [item for item in current["keys"] if item["id"] != ident]
    if len(remaining) == len(current["keys"]):
        raise ValueError("Public key was not installed")
    current["keys"] = remaining
    write_registry(home, current)


def client_key_path(home, scope="api"):
    # Resolving this path also exposes Codex's redirected AppData target. Hash
    # the configured absolute Windows path instead, identically in both views.
    identity = os.path.abspath(str(home)) if os.name == "nt" else str(Path(home).resolve())
    instance = hashlib.sha256(os.path.normcase(identity).encode()).hexdigest()[:16]
    # Store-packaged Codex redirects AppData, but an Edge native host does not.
    # USERPROFILE is shared by both processes, so their signing identity agrees.
    root = (Path(os.environ.get("USERPROFILE", Path.home())) / ".codex-permissive-clients"
            if os.name == "nt" else Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share")) / "CodexPermissiveClients")
    return root / instance / scope / "private-key.dpapi"


def migrate_local_clients(home):
    if os.name != "nt":
        return
    for scope in ("api", "browser"):
        target = client_key_path(home, scope)
        legacy_instance = hashlib.sha256(os.path.normcase(str(Path(home).resolve())).encode()).hexdigest()[:16]
        legacy_root = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share")) / "CodexPermissiveClients"
        legacy = legacy_root / legacy_instance / scope
        if not (legacy / "private-key.dpapi").is_file():
            legacy = legacy_root / target.parent.parent.name / scope
        private = legacy / "private-key.dpapi"
        public = legacy / "public-key.pub"
        if target.exists() or not private.is_file() or not public.is_file():
            continue
        # Copy the existing encrypted identity; retain legacy files for live clients.
        # Provisioning never restores revoked authorization or exports plaintext.
        target.parent.mkdir(parents=True, exist_ok=True)
        for source, destination in ((public, target.with_name("public-key.pub")), (private, target)):
            try:
                descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                continue
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(source.read_bytes())


def generate(private_path, public_path):
    private_path, public_path = Path(private_path), Path(public_path)
    if private_path.exists() or public_path.exists():
        raise ValueError("Key output files already exist")
    key = Ed25519PrivateKey.generate()
    encoded = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    if private_path.suffix == ".dpapi":
        encoded = protected(encoded)
    private_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(private_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as handle:
        handle.write(encoded)
    with os.fdopen(os.open(public_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as handle:
        handle.write(key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH) + b"\n")
    return {"keyId": fingerprint(key.public_key()), "privateKeyPath": str(private_path), "publicKeyPath": str(public_path)}


def initialize_local(home, ensure=False):
    # The verification service never generates/loads private keys. This explicit
    # local-owner provisioning command registers the separate desktop/browser clients.
    migrate_local_clients(home)
    if ensure and registry_path(home).exists():
        registry(home)
        return {"initialized": True}
    result = {}
    for scope in ("api", "browser"):
        path = client_key_path(home, scope)
        pub = path.with_name("public-key.pub")
        if not path.exists():
            generate(path, pub)
        signer = Signer(path)
        pem = signer.key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        result[scope] = authorize(home, pem, "Local " + scope + " client", (scope,))
    return result


def digest(body):
    return "sha-256=:" + base64.b64encode(hashlib.sha256(body).digest()).decode() + ":"


def signature_base(method, target, headers, fields, parameters):
    parsed = urlsplit(target)
    values = {"@method": method.upper(), "@path": parsed.path or "/", "@query": "?" + parsed.query}
    values.update(headers)
    return ("\n".join('"' + field + '": ' + values[field].strip() for field in fields)
            + '\n"@signature-params": ' + parameters).encode("ascii")


class Signer:
    def __init__(self, path):
        path = Path(path)
        value = path.read_bytes()
        if path.suffix == ".dpapi":
            value = protected(value, decrypt=True)
        password = os.environ.get("PERMISSIVE_KEY_PASSWORD")
        password = password.encode() if password else None
        self.key = (serialization.load_ssh_private_key(value, password) if value.startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----")
                    else serialization.load_pem_private_key(value, password))
        if not isinstance(self.key, Ed25519PrivateKey):
            raise ValueError("Only Ed25519 signing keys are supported")
        self.key_id = fingerprint(self.key.public_key())

    def headers(self, method, target, body=b"", headers=None, created=None, nonce=None, lifetime=60):
        values = {key.lower(): value for key, value in (headers or {}).items()}
        values["content-digest"] = digest(body)
        fields = [*BASE_FIELDS, *(field for field in OPTIONAL_FIELDS if field in values)]
        created = int(time.time()) if created is None else created
        nonce = secrets.token_urlsafe(24) if nonce is None else nonce
        parameters = '(' + ' '.join('"' + field + '"' for field in fields) + f');created={created};expires={created + lifetime};nonce="{nonce}";keyid="{self.key_id}";alg="ed25519"'
        signed = self.key.sign(signature_base(method, target, values, fields, parameters))
        values["signature-input"] = "permissive=" + parameters
        values["signature"] = "permissive=:" + base64.b64encode(signed).decode() + ":"
        return values


class Authenticator:
    def __init__(self, home):
        self.home = Path(home)
        registry(home)  # Invalid public-key configuration fails closed at startup.
        directory = self.home / "auth"; directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(directory / "replay.sqlite")
        self.db.execute("CREATE TABLE IF NOT EXISTS seen (key_id TEXT NOT NULL, nonce TEXT NOT NULL, expires INTEGER NOT NULL, PRIMARY KEY(key_id,nonce))")

    def verify(self, method, target, body, headers):
        required = ["signature-input", "signature", "content-digest"]
        for field in [*required, *OPTIONAL_FIELDS]:
            if len(headers.getall(field, [])) != (1 if field in required or field in headers else 0):
                raise ValueError("Missing or duplicate signature field")
        match = PROFILE.fullmatch(headers["signature-input"])
        if not match:
            raise ValueError("Unsupported signature profile")
        field_list, created, expires, nonce, key_id = match.groups()
        created, expires, now = int(created), int(expires), int(time.time())
        if created > now + 30 or expires < now or not 0 < expires - created <= 120:
            raise ValueError("Expired or invalid signature lifetime")
        fields = re.findall(r'"([@a-z-]+)"', field_list)
        expected = [*BASE_FIELDS, *(field for field in OPTIONAL_FIELDS if field in headers)]
        if fields != expected or not hmac.compare_digest(headers["content-digest"], digest(body)):
            raise ValueError("Signature coverage or content digest mismatch")
        keys = registry(self.home)["keys"]
        entry = next((item for item in keys if item["id"] == key_id), None)
        scope = "browser" if urlsplit(target).path.startswith("/v1/browser/") else "api"
        if entry is None or scope not in entry["scopes"]:
            raise ValueError("Public key is not authorized for this endpoint")
        signature = re.fullmatch(r"permissive=:([A-Za-z0-9+/]{86}==):", headers["signature"])
        if signature is None:
            raise ValueError("Invalid Ed25519 signature encoding")
        public_key(entry["publicKey"]).verify(base64.b64decode(signature[1], validate=True), signature_base(method, target, {key.lower(): value for key, value in headers.items()}, fields, headers["signature-input"].removeprefix("permissive=")))
        with self.db:
            self.db.execute("DELETE FROM seen WHERE expires<?", (now,))
            try:
                self.db.execute("INSERT INTO seen VALUES (?,?,?)", (key_id, nonce, expires))
            except sqlite3.IntegrityError:
                raise ValueError("Request signature has already been used") from None
        return key_id

    def close(self):
        self.db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="codex-permissive auth")
    parser.add_argument("--home", type=Path, default=default_home())
    sub = parser.add_subparsers(dest="operation", required=True)
    init = sub.add_parser("init-local"); init.add_argument("--ensure", action="store_true")
    generate_parser = sub.add_parser("generate")
    generate_parser.add_argument("--private-key", required=True); generate_parser.add_argument("--public-key", required=True)
    install = sub.add_parser("add"); install.add_argument("public_key", type=Path); install.add_argument("--name", required=True); install.add_argument("--scope", choices=("api", "browser"), default="api")
    remove = sub.add_parser("revoke"); remove.add_argument("key_id")
    sub.add_parser("list")
    sign = sub.add_parser("sign"); sign.add_argument("--private-key"); sign.add_argument("--scope", choices=("api", "browser"), default="api")
    args = parser.parse_args(argv)
    try:
        if args.operation == "init-local": result = initialize_local(args.home, args.ensure)
        elif args.operation == "generate": result = generate(args.private_key, args.public_key)
        elif args.operation == "add": result = authorize(args.home, args.public_key.read_bytes(), args.name, (args.scope,))
        elif args.operation == "revoke": revoke(args.home, args.key_id); result = {"revoked": args.key_id}
        elif args.operation == "list": result = registry(args.home)
        else:
            request = json.load(sys.stdin)
            variable = "PERMISSIVE_PRIVATE_KEY" if args.scope == "api" else "PERMISSIVE_BROWSER_PRIVATE_KEY"
            signer = Signer(args.private_key or os.environ.get(variable) or client_key_path(args.home, args.scope))
            result = signer.headers(request["method"], request["target"], base64.b64decode(request.get("body", ""), validate=True), request.get("headers"))
        print(json.dumps(result))  # Metadata/signatures only; never private key material.
    except (OSError, ValueError, KeyError, InvalidSignature) as error:
        print("Authentication operation failed: " + str(error), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
