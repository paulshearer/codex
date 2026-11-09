"""Verified TLS for the native agent's local inference connection."""

import datetime
import ipaddress
import os
from pathlib import Path
import ssl
import tempfile

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


class LocalTLS:
    """A task-scoped certificate, trusted only by that task's native child."""

    def __init__(self, home):
        directory = Path(home) / "native-tls"
        directory.mkdir(parents=True, exist_ok=True)
        self._temporary = tempfile.TemporaryDirectory(prefix="task-", dir=directory)
        try:
            root = Path(self._temporary.name)
            self.certificate = root / "certificate.pem"
            server_certificate = root / "server-certificate.pem"
            private = root / "private-key.pem"
            authority_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            now = datetime.datetime.now(datetime.timezone.utc)
            authority = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Codex Permissive task CA")])
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Codex Permissive local inference")])
            ca = (
                x509.CertificateBuilder().subject_name(authority).issuer_name(authority)
                .public_key(authority_key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(minutes=5))
                .not_valid_after(now + datetime.timedelta(days=30))
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(authority_key.public_key()), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(authority_key.public_key()), critical=False)
                .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                    key_encipherment=False, data_encipherment=False, key_agreement=False,
                    key_cert_sign=True, crl_sign=True, encipher_only=None, decipher_only=None), critical=True)
                .sign(authority_key, hashes.SHA256())
            )
            certificate = (
                x509.CertificateBuilder().subject_name(name).issuer_name(authority)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(minutes=5))
                .not_valid_after(now + datetime.timedelta(days=30))
                .add_extension(x509.SubjectAlternativeName([
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                    x509.DNSName("localhost"),
                ]), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(authority_key.public_key()), critical=False)
                .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                    key_encipherment=True, data_encipherment=False, key_agreement=False,
                    key_cert_sign=False, crl_sign=False, encipher_only=None, decipher_only=None), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .sign(authority_key, hashes.SHA256())
            )
            self.certificate.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
            server_certificate.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
            # Keep the private key on disk only while OpenSSL loads it. The
            # temporary directory inherits the owning user's profile ACL.
            descriptor = os.open(private, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(key.private_bytes(serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            try:
                self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                self.context.minimum_version = ssl.TLSVersion.TLSv1_2
                self.context.load_cert_chain(server_certificate, private)
            finally:
                private.unlink(missing_ok=True)
                server_certificate.unlink(missing_ok=True)
        except BaseException:
            self.close()
            raise

    def environment(self, environment):
        result = dict(environment)
        # Codex adds this certificate to its normal trust roots. It takes
        # precedence over SSL_CERT_FILE without replacing upstream TLS trust.
        for key in list(result):
            if key.upper() == "CODEX_CA_CERTIFICATE":
                del result[key]
        result["CODEX_CA_CERTIFICATE"] = str(self.certificate)
        return result

    def close(self):
        self._temporary.cleanup()
