"""Companion device authorization. Private phone keys never leave Android Keystore."""

import base64
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.parse import urlsplit
import uuid

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

from .auth import digest


BASE_FIELDS = ("@method", "@authority", "@path", "@query", "content-digest", "permissive-instance-id")
OPTIONAL_FIELDS = ("content-type", "idempotency-key", "last-event-id")
PROFILE = re.compile(r'^permissive-mobile=(\((?:"[@a-z-]+" ?)+\));created=([0-9]{1,12});expires=([0-9]{1,12});nonce="([A-Za-z0-9_-]{22,64})";keyid="([0-9a-f]{64})";alg="ecdsa-p256-sha256"$')
SIGNATURE = re.compile(r'^permissive-mobile=:([A-Za-z0-9+/]{86}==):$')
TOKEN = re.compile(r'^[A-Za-z0-9_-]{43}$')
EXISTING_SERVICE_UPSTREAM = "http://127.0.0.1:8789"


def origin(value):
    """Serve HTTPS origins only, without paths, credentials, or implicit ports."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or parsed.port != 9444 or not host.endswith(".ts.net")
                or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host)
                or len(host) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split("."))
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError()
        return "https://" + host + ":9444"
    except (TypeError, ValueError):
        raise ValueError("A Tailscale HTTPS origin on port 9444 is required") from None


def upstream_origin(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError()
        return "http://127.0.0.1:" + str(parsed.port)
    except (TypeError, ValueError):
        raise ValueError("The upstream must be an HTTP IPv4 loopback origin") from None


def validate_configuration(public_origin, upstream, port=8790, existing_service=False):
    public_origin, upstream = origin(public_origin), upstream_origin(upstream)
    if type(port) is not int or not 1024 <= port <= 65535 or port == urlsplit(upstream).port:
        raise ValueError("Invalid companion port")
    if type(existing_service) is not bool or existing_service and upstream != EXISTING_SERVICE_UPSTREAM:
        raise ValueError("Existing-service access requires the existing loopback Windows service")
    return public_origin, upstream, port, existing_service


def public_key(value):
    if not isinstance(value, str) or len(value) > 512:
        raise ValueError("Invalid mobile public key")
    try:
        encoded = base64.b64decode(value, validate=True)
        key = serialization.load_der_public_key(encoded)
    except (ValueError, TypeError):
        raise ValueError("Invalid mobile public key") from None
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("A P-256 mobile public key is required")
    canonical = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    if encoded != canonical:
        raise ValueError("Canonical SPKI public key encoding is required")
    return key, canonical, hashlib.sha256(canonical).hexdigest()


def signature_base(method, authority, target, headers, fields, parameters):
    parsed = urlsplit(target)
    values = {"@method": method, "@authority": authority, "@path": parsed.path or "/", "@query": "?" + parsed.query}
    values.update({key.lower(): value for key, value in headers.items()})
    return ("\n".join('"' + field + '": ' + values[field].strip() for field in fields)
            + '\n"@signature-params": ' + parameters).encode("ascii")


class MobileState:
    def __init__(self, home):
        self.root = Path(home) / "companion"
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "state.sqlite", timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY CHECK(id=1), installation_id TEXT NOT NULL,
                origin TEXT, upstream TEXT, port INTEGER NOT NULL DEFAULT 8790,
                enabled INTEGER NOT NULL DEFAULT 0,
                existing_service INTEGER NOT NULL DEFAULT 0 CHECK(existing_service IN (0,1)));
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY, public_key BLOB NOT NULL, name TEXT NOT NULL,
                created_at INTEGER NOT NULL, revoked_at INTEGER);
            CREATE TABLE IF NOT EXISTS invitations (
                id TEXT PRIMARY KEY, secret_hash BLOB NOT NULL, expires INTEGER NOT NULL,
                consumed_by TEXT, consumed_at INTEGER);
            CREATE TABLE IF NOT EXISTS nonces (
                device_id TEXT NOT NULL, nonce TEXT NOT NULL, expires INTEGER NOT NULL,
                PRIMARY KEY(device_id, nonce));
        """)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if "existing_service" not in {row["name"] for row in self.db.execute("PRAGMA table_info(settings)")}:
                self.db.execute("ALTER TABLE settings ADD COLUMN existing_service INTEGER NOT NULL DEFAULT 0 CHECK(existing_service IN (0,1))")
            self.db.execute("INSERT OR IGNORE INTO settings(id,installation_id) VALUES(1,?)", (str(uuid.uuid4()),))

    def close(self):
        self.db.close()

    def settings(self):
        row = self.db.execute("SELECT * FROM settings WHERE id=1").fetchone()
        if type(row["existing_service"]) is not int or row["existing_service"] not in (0, 1):
            raise ValueError("Invalid existing-service setting")
        return {"installationId": row["installation_id"], "origin": row["origin"], "upstream": row["upstream"],
                "port": row["port"], "enabled": bool(row["enabled"]), "existingService": bool(row["existing_service"])}

    def configure(self, public_origin, upstream, port=8790, *, existing_service=False):
        public_origin, upstream, port, existing_service = validate_configuration(public_origin, upstream, port, existing_service)
        with self.db:
            self.db.execute("UPDATE settings SET origin=?,upstream=?,port=?,enabled=1,existing_service=? WHERE id=1",
                            (public_origin, upstream, port, int(existing_service)))
            self.db.execute("DELETE FROM invitations")
        return self.status()

    def status(self):
        return {**self.settings(), "devices": self.devices()}

    def devices(self):
        return [{"deviceId": row["id"], "deviceName": row["name"], "createdAt": row["created_at"],
                 "revokedAt": row["revoked_at"], "revoked": row["revoked_at"] is not None}
                for row in self.db.execute("SELECT id,name,created_at,revoked_at FROM devices ORDER BY created_at,id")]

    def active(self, ident):
        return self.settings()["enabled"] and self.db.execute(
            "SELECT 1 FROM devices WHERE id=? AND revoked_at IS NULL", (ident,)).fetchone() is not None

    def revoke(self, ident):
        with self.db:
            result = self.db.execute("UPDATE devices SET revoked_at=COALESCE(revoked_at,?) WHERE id=?", (int(time.time()), ident))
            if not result.rowcount:
                raise ValueError("Unknown companion device")
        return {"revoked": True, "deviceId": ident}

    def disable(self):
        with self.db:
            self.db.execute("UPDATE settings SET enabled=0 WHERE id=1")
            self.db.execute("DELETE FROM invitations")
        return self.status()

    def invite(self):
        settings = self.settings()
        if not settings["enabled"] or not settings["origin"]:
            raise ValueError("Configure the companion before pairing")
        secret, ident, expires = secrets.token_urlsafe(32), str(uuid.uuid4()), int(time.time()) + 300
        with self.db:
            # Replacing an unscanned invitation is explicit; consumed receipts remain
            # until expiry so a response lost in transit can be retried safely.
            self.db.execute("DELETE FROM invitations WHERE consumed_by IS NULL OR expires<?", (int(time.time()),))
            self.db.execute("INSERT INTO invitations(id,secret_hash,expires) VALUES(?,?,?)",
                            (ident, hashlib.sha256(secret.encode("ascii")).digest(), expires))
        return {"version": 1, "origin": settings["origin"], "installationId": settings["installationId"],
                "invitationId": ident, "secret": secret, "expiresAt": expires}

    def identity(self, ident):
        row = self.db.execute("SELECT name FROM devices WHERE id=? AND revoked_at IS NULL", (ident,)).fetchone()
        if not row or not self.settings()["enabled"]:
            raise ValueError("Device is not authorized")
        settings = self.settings()
        return {"installationId": settings["installationId"], "deviceId": ident, "deviceName": row["name"],
                "origin": settings["origin"], "protocolVersion": 1,
                "capabilities": ["existing-workspaces", "chats", "tasks", "attachments", "artifacts"]}

    def verify(self, method, authority, target, body, headers, candidate=None):
        settings = self.settings()
        if not settings["enabled"] or not settings["origin"]:
            raise ValueError("Companion is disabled")
        expected_authority = urlsplit(settings["origin"]).netloc
        if authority.lower() != expected_authority:
            raise ValueError("Wrong companion authority")
        required = ("signature-input", "signature", "content-digest", "permissive-instance-id")
        for field in (*required, *OPTIONAL_FIELDS):
            if len(headers.getall(field, [])) != (1 if field in required or field in headers else 0):
                raise ValueError("Missing or duplicate signature field")
        match = PROFILE.fullmatch(headers["signature-input"])
        if not match:
            raise ValueError("Unsupported mobile signature profile")
        field_list, created, expires, nonce, ident = match.groups()
        created, expires, now = int(created), int(expires), int(time.time())
        if created > now + 30 or expires < now or not 0 < expires - created <= 120:
            raise ValueError("Invalid mobile signature lifetime")
        fields = re.findall(r'"([@a-z-]+)"', field_list)
        expected = [*BASE_FIELDS, *(field for field in OPTIONAL_FIELDS if field in headers)]
        if (fields != expected or headers["permissive-instance-id"] != settings["installationId"]
                or not hmac.compare_digest(headers["content-digest"], digest(body))):
            raise ValueError("Mobile signature coverage mismatch")
        if candidate is None:
            row = self.db.execute("SELECT public_key FROM devices WHERE id=? AND revoked_at IS NULL", (ident,)).fetchone()
            if not row:
                raise ValueError("Device is not authorized")
            key = serialization.load_der_public_key(bytes(row["public_key"]))
        else:
            key, _, candidate_id = candidate
            if ident != candidate_id:
                raise ValueError("Mobile public key mismatch")
        encoded = SIGNATURE.fullmatch(headers["signature"])
        if not encoded:
            raise ValueError("Invalid mobile signature encoding")
        raw = base64.b64decode(encoded[1], validate=True)
        if len(raw) != 64:
            raise ValueError("Invalid mobile signature length")
        r, s = int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
        key.verify(utils.encode_dss_signature(r, s), signature_base(method, expected_authority, target, headers,
                   fields, headers["signature-input"].removeprefix("permissive-mobile=")), ec.ECDSA(hashes.SHA256()))
        with self.db:
            self.db.execute("DELETE FROM nonces WHERE expires<?", (now,))
            try:
                self.db.execute("INSERT INTO nonces VALUES(?,?,?)", (ident, nonce, expires))
            except sqlite3.IntegrityError:
                raise ValueError("Mobile signature replay") from None
        return ident

    def enroll(self, value, method, authority, target, body, headers):
        if (not isinstance(value, dict) or value.keys() != {"invitationId", "secret", "publicKey", "deviceName"}
                or not isinstance(value["secret"], str) or not TOKEN.fullmatch(value["secret"])
                or not isinstance(value["invitationId"], str) or len(value["invitationId"]) > 64
                or not isinstance(value["deviceName"], str) or not value["deviceName"].strip()
                or len(value["deviceName"]) > 120 or any(ord(c) < 32 for c in value["deviceName"])):
            raise ValueError("Invalid enrollment")
        invitation = self.db.execute("SELECT * FROM invitations WHERE id=?", (value["invitationId"],)).fetchone()
        if (not invitation or invitation["expires"] < int(time.time())
                or not hmac.compare_digest(bytes(invitation["secret_hash"]), hashlib.sha256(value["secret"].encode("ascii")).digest())):
            raise ValueError("Invalid enrollment invitation")
        candidate = public_key(value["publicKey"])
        ident = self.verify(method, authority, target, body, headers, candidate=candidate)
        try:
            self.db.execute("BEGIN IMMEDIATE")
            invitation = self.db.execute("SELECT * FROM invitations WHERE id=?", (value["invitationId"],)).fetchone()
            if not invitation or invitation["expires"] < int(time.time()) or not self.settings()["enabled"]:
                raise ValueError("Invalid enrollment invitation")
            if invitation["consumed_by"] is not None:
                if invitation["consumed_by"] != ident or not self.active(ident):
                    raise ValueError("Enrollment invitation was already consumed")
                created = False
            else:
                now = int(time.time())
                self.db.execute("INSERT INTO devices VALUES(?,?,?,?,NULL) ON CONFLICT(id) DO UPDATE SET name=excluded.name,revoked_at=NULL",
                                (ident, candidate[1], value["deviceName"].strip(), now))
                self.db.execute("UPDATE invitations SET consumed_by=?,consumed_at=? WHERE id=?", (ident, now, value["invitationId"]))
                created = True
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return self.identity(ident), created
