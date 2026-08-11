"""
TLS utilities: self-signed certificate generation, SHA-256 fingerprinting,
SSL context factories, and certificate pinning via known_hosts file.

known_hosts format (one entry per line):
    <host>:<port> <SHA256_FINGERPRINT_UPPERCASE_COLON_SEPARATED>

Example:
    192.168.1.10:8721 AA:BB:CC:DD:...
"""

from __future__ import annotations

import hashlib
import ipaddress
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509 import load_pem_x509_certificate
from cryptography.x509.oid import NameOID

# Default path for bridge's certificate pinning database
KNOWN_HOSTS_PATH = Path.home() / ".config" / "usbmuxd-bridge" / "known_hosts"

_CERT_VALIDITY_DAYS = 3650  # ~10 years


def generate_self_signed(cert_dir: Path) -> tuple[Path, Path]:
    """
    Generate a self-signed RSA-2048 certificate + private key in *cert_dir*.

    If cert.pem and key.pem already exist in *cert_dir*, returns their paths
    without regenerating (idempotent).

    Returns:
        (cert_path, key_path)
    """
    cert_dir = cert_dir.expanduser()
    cert_dir.mkdir(parents=True, exist_ok=True)

    cert_path = cert_dir / "cert.pem"
    key_path = cert_dir / "key.pem"

    if cert_path.exists() and key_path.exists():
        return cert_path, key_path

    # --- Generate RSA private key ---
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    # --- Build certificate ---
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "usbmuxd-agent"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "NetworkUSB"),
        ]
    )
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=_CERT_VALIDITY_DAYS))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                    x509.IPAddress(ipaddress.IPv4Address("0.0.0.0")),
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        .sign(private_key, hashes.SHA256())
    )

    # --- Write to disk ---
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)  # owner read-only

    return cert_path, key_path


def get_fingerprint(cert_path: Path) -> str:
    """
    Return the SHA-256 fingerprint of a PEM certificate.

    Format: uppercase hex pairs separated by colons, e.g.
        ``AA:BB:CC:DD:...`` (32 pairs = 95 characters total)
    """
    cert_path = cert_path.expanduser()
    cert = load_pem_x509_certificate(cert_path.read_bytes())
    der = cert.public_bytes(serialization.Encoding.DER)
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def fingerprint_from_der(der: bytes) -> str:
    """Compute SHA-256 fingerprint directly from raw DER certificate bytes."""
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


# ---------------------------------------------------------------------------
# SSL context factories
# ---------------------------------------------------------------------------


def make_server_ssl_context(cert_path: Path, key_path: Path) -> ssl.SSLContext:
    """
    Create an SSL context for the agent (TLS server).

    Enforces TLS 1.2+ minimum.
    """
    cert_path = cert_path.expanduser()
    key_path = key_path.expanduser()

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert_path), str(key_path))
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def make_client_ssl_context() -> ssl.SSLContext:
    """
    Create an SSL context for the bridge (TLS client).

    Certificate verification is intentionally disabled because we use
    certificate pinning (fingerprint comparison) instead of a CA chain.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


# ---------------------------------------------------------------------------
# Certificate pinning: known_hosts
# ---------------------------------------------------------------------------


def load_known_fingerprint(host: str, port: int) -> str | None:
    """
    Look up the stored fingerprint for *host:port* in the known_hosts file.

    Returns None if the host has not been seen before or the file doesn't exist.
    """
    key = f"{host}:{port}"
    known_hosts = KNOWN_HOSTS_PATH
    if not known_hosts.exists():
        return None

    for line in known_hosts.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=1)
        if len(parts) == 2 and parts[0] == key:
            return parts[1].strip()

    return None


def save_known_fingerprint(host: str, port: int, fingerprint: str) -> None:
    """
    Persist *fingerprint* for *host:port* in the known_hosts file.

    Replaces any existing entry for the same host:port.
    """
    known_hosts = KNOWN_HOSTS_PATH
    known_hosts.parent.mkdir(parents=True, exist_ok=True)

    key = f"{host}:{port}"
    lines: list[str] = []

    if known_hosts.exists():
        for line in known_hosts.read_text(encoding="utf-8").splitlines():
            parts = line.strip().split(maxsplit=1)
            if parts and parts[0] == key:
                continue  # will be replaced
            lines.append(line)

    lines.append(f"{key} {fingerprint}")
    known_hosts.write_text("\n".join(lines) + "\n", encoding="utf-8")
