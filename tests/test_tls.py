"""
Unit tests for networkusb.tls.

Tests cover:
- Certificate generation (creates files, correct PEM format)
- Idempotency (re-calling generate_self_signed returns same files)
- Fingerprint format (32 uppercase hex pairs, colon-separated)
- Fingerprint stability (same cert → same fingerprint)
- known_hosts save / load / update / missing-host
- SSL context factories return correct verify modes
"""

from __future__ import annotations

import ssl

import pytest

from networkusb.tls import (
    fingerprint_from_der,
    generate_self_signed,
    get_fingerprint,
    load_known_fingerprint,
    make_client_ssl_context,
    make_server_ssl_context,
    save_known_fingerprint,
)

# ---------------------------------------------------------------------------
# Certificate generation
# ---------------------------------------------------------------------------


def test_generate_creates_pem_files(tmp_path):
    cert_path, key_path = generate_self_signed(tmp_path)

    assert cert_path.exists()
    assert key_path.exists()
    assert cert_path.read_bytes().startswith(b"-----BEGIN CERTIFICATE-----")
    assert key_path.read_bytes().startswith(b"-----BEGIN RSA PRIVATE KEY-----")


def test_generate_key_permissions(tmp_path):
    _, key_path = generate_self_signed(tmp_path)
    mode = oct(key_path.stat().st_mode & 0o777)
    assert mode == "0o600", f"key.pem should be owner-read-only, got {mode}"


def test_generate_idempotent(tmp_path):
    """Calling twice returns the same cert content."""
    cert1, _key1 = generate_self_signed(tmp_path)
    content1 = cert1.read_bytes()

    cert2, _key2 = generate_self_signed(tmp_path)
    content2 = cert2.read_bytes()

    assert content1 == content2, "Should not regenerate when files already exist"


def test_generate_in_nested_dir(tmp_path):
    """cert_dir is created automatically if it doesn't exist."""
    nested = tmp_path / "a" / "b" / "c"
    assert not nested.exists()
    cert_path, _ = generate_self_signed(nested)
    assert nested.exists()
    assert cert_path.exists()


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------


def test_fingerprint_format(tmp_path):
    cert_path, _ = generate_self_signed(tmp_path)
    fp = get_fingerprint(cert_path)

    # SHA-256 = 32 bytes = 64 hex chars, formatted as "XX:XX:...:XX" (32 pairs)
    parts = fp.split(":")
    assert len(parts) == 32, f"Expected 32 groups, got {len(parts)}"
    for part in parts:
        assert len(part) == 2, f"Each group should be 2 chars, got {part!r}"
        assert part == part.upper(), "Fingerprint should be uppercase"
        int(part, 16)  # must be valid hex — raises ValueError if not


def test_fingerprint_stable(tmp_path):
    """Same cert file → same fingerprint every time."""
    cert_path, _ = generate_self_signed(tmp_path)
    fp1 = get_fingerprint(cert_path)
    fp2 = get_fingerprint(cert_path)
    assert fp1 == fp2


def test_fingerprint_from_der_matches_file(tmp_path):
    """fingerprint_from_der should agree with get_fingerprint (file-based)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.x509 import load_pem_x509_certificate

    cert_path, _ = generate_self_signed(tmp_path)
    pem = cert_path.read_bytes()
    cert = load_pem_x509_certificate(pem)
    der = cert.public_bytes(serialization.Encoding.DER)

    assert fingerprint_from_der(der) == get_fingerprint(cert_path)


def test_different_certs_different_fingerprints(tmp_path):
    """Two independently generated certs should have different fingerprints."""
    dir1 = tmp_path / "c1"
    dir2 = tmp_path / "c2"
    cert1, _ = generate_self_signed(dir1)
    cert2, _ = generate_self_signed(dir2)
    assert get_fingerprint(cert1) != get_fingerprint(cert2)


# ---------------------------------------------------------------------------
# known_hosts: save / load
# ---------------------------------------------------------------------------


@pytest.fixture()
def patched_known_hosts(tmp_path, monkeypatch):
    """Redirect KNOWN_HOSTS_PATH to a temporary location."""
    import networkusb.tls as tls_module
    fake_path = tmp_path / "known_hosts"
    monkeypatch.setattr(tls_module, "KNOWN_HOSTS_PATH", fake_path)
    return fake_path


def test_known_hosts_save_and_load(patched_known_hosts):
    save_known_fingerprint("192.168.1.10", 8721, "AA:BB:CC:DD")
    result = load_known_fingerprint("192.168.1.10", 8721)
    assert result == "AA:BB:CC:DD"


def test_known_hosts_missing_host(patched_known_hosts):
    assert load_known_fingerprint("10.0.0.1", 8721) is None


def test_known_hosts_missing_file(patched_known_hosts):
    # File does not exist yet
    assert not patched_known_hosts.exists()
    assert load_known_fingerprint("anything", 9999) is None


def test_known_hosts_update_existing(patched_known_hosts):
    """Saving a new fingerprint for the same host replaces the old one."""
    save_known_fingerprint("host.local", 8721, "OLD:FP")
    save_known_fingerprint("host.local", 8721, "NEW:FP")

    result = load_known_fingerprint("host.local", 8721)
    assert result == "NEW:FP"

    # Only one entry for this host
    lines = [
        l for l in patched_known_hosts.read_text().splitlines()
        if "host.local:8721" in l
    ]
    assert len(lines) == 1


def test_known_hosts_multiple_hosts(patched_known_hosts):
    """Different hosts coexist in the same known_hosts file."""
    save_known_fingerprint("host-a.local", 8721, "FP:A")
    save_known_fingerprint("host-b.local", 8721, "FP:B")
    save_known_fingerprint("host-c.local", 9000, "FP:C")

    assert load_known_fingerprint("host-a.local", 8721) == "FP:A"
    assert load_known_fingerprint("host-b.local", 8721) == "FP:B"
    assert load_known_fingerprint("host-c.local", 9000) == "FP:C"
    assert load_known_fingerprint("host-d.local", 8721) is None


def test_known_hosts_ignores_comments(patched_known_hosts):
    """Lines starting with # are ignored."""
    patched_known_hosts.parent.mkdir(parents=True, exist_ok=True)
    patched_known_hosts.write_text(
        "# this is a comment\n"
        "10.0.0.1:8721 REAL:FP\n"
    )
    assert load_known_fingerprint("10.0.0.1", 8721) == "REAL:FP"
    assert load_known_fingerprint("# this", 8721) is None


# ---------------------------------------------------------------------------
# SSL context factories
# ---------------------------------------------------------------------------


def test_server_ssl_context(tmp_path):
    cert_path, key_path = generate_self_signed(tmp_path)
    ctx = make_server_ssl_context(cert_path, key_path)
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2


def test_client_ssl_context_no_verify():
    ctx = make_client_ssl_context()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2
