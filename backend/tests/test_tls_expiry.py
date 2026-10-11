"""TLS expiry metrics on /metrics (exposition.py, GMAN_TLS_WATCH_FILES): files made with cryptography in tmp_path."""
import os
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from backend.app import exposition
from backend.tests.test_alerts import find, samples
from backend.tests.test_api import client  # noqa: F401  (fixture)

NOW = datetime.now(timezone.utc).replace(microsecond=0)
PEM = serialization.Encoding.PEM


def name(cn, ou=None):
    attributes = [x509.NameAttribute(NameOID.ORGANIZATION_NAME, "GMan pilot")]
    if ou:
        attributes.append(x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, ou))
    return x509.Name([*attributes, x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def certificate(subject, issuer, key, signer, days, ca=False):
    return (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(NOW - timedelta(days=400)).not_valid_after(NOW + timedelta(days=days))
            .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
            .sign(signer, hashes.SHA256()))


def crl(issuer, signer, days):
    return (x509.CertificateRevocationListBuilder().issuer_name(issuer)
            .last_update(NOW - timedelta(days=400)).next_update(NOW + timedelta(days=days))
            .sign(signer, hashes.SHA256()))


@pytest.fixture
def pki(tmp_path):
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = name("GMan pilot CA")
    ca = certificate(ca_name, ca_name, ca_key, ca_key, 1825, ca=True)
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    portal = certificate(name("gman.example.org", "server"), ca_name, leaf_key, ca_key, 20)
    expired = certificate(name("old.example.org", "server"), ca_name, leaf_key, ca_key, -3)
    files = {
        "ca.pem": ca.public_bytes(PEM),
        # Leaf followed by its chain, as a corporate CA delivers fullchain.pem.
        "portal.pem": portal.public_bytes(PEM) + ca.public_bytes(PEM),
        "old.pem": expired.public_bytes(PEM),
        "crl.pem": crl(ca_name, ca_key, 5).public_bytes(PEM),
        "crl-expired.der": crl(ca_name, ca_key, -1).public_bytes(serialization.Encoding.DER),
        "bad.pem": b"-----BEGIN CERTIFICATE-----\nnot base64\n-----END CERTIFICATE-----\n",
        "empty.pem": b"",
        "portal-key.pem": leaf_key.private_bytes(PEM, serialization.PrivateFormat.PKCS8,
                                                 serialization.NoEncryption()),
    }
    for file, data in files.items():
        (tmp_path / file).write_bytes(data)
    exposition._tls_cache.clear()
    return {"dir": tmp_path, "ca": ca, "portal": portal, "expired": expired}


def watch(monkeypatch, pki, *files):
    paths = [str(pki["dir"] / file) for file in files]
    monkeypatch.setenv("GMAN_TLS_WATCH_FILES", " , ".join(paths) + ",")
    return paths


def test_certificate_and_crl_expiry_are_exposed(client, monkeypatch, pki):
    ca, portal, old, crl_pem, crl_der = watch(monkeypatch, pki, "ca.pem", "portal.pem", "old.pem", "crl.pem",
                                              "crl-expired.der")
    data = samples(client.get("/metrics").text)

    cert = "gman_tls_cert_not_after_timestamp_seconds"
    ca_subject = "CN=GMan pilot CA,O=GMan pilot"
    assert find(data, cert, file=ca, subject=ca_subject) == (NOW + timedelta(days=1825)).timestamp()
    # A bundle exposes every certificate it holds.
    assert find(data, cert, file=portal, subject="CN=gman.example.org,OU=server,O=GMan pilot") == (
        NOW + timedelta(days=20)).timestamp()
    assert find(data, cert, file=portal, subject=ca_subject) == (NOW + timedelta(days=1825)).timestamp()
    # Expired files are still reported: the alert compares the timestamp with time().
    assert find(data, cert, file=old) == (NOW - timedelta(days=3)).timestamp()

    next_update = "gman_tls_crl_next_update_timestamp_seconds"
    assert find(data, next_update, file=crl_pem, issuer=ca_subject) == (NOW + timedelta(days=5)).timestamp()
    assert find(data, next_update, file=crl_der) == (NOW - timedelta(days=1)).timestamp()
    for path in (ca, portal, old, crl_pem, crl_der):
        assert find(data, "gman_tls_file_error", file=path) == 0
    assert not any(n == cert and ("file", crl_pem) in labels for n, labels in data)


def test_unreadable_files_and_keys_are_errors(client, monkeypatch, pki):
    opened = []
    real_open = open
    monkeypatch.setattr("builtins.open", lambda path, *a, **k: opened.append(str(path)) or real_open(path, *a, **k))
    bad, empty, missing, key = watch(monkeypatch, pki, "bad.pem", "empty.pem", "missing.pem", "portal-key.pem")
    data = samples(client.get("/metrics").text)
    for path in (bad, empty, missing, key):
        assert find(data, "gman_tls_file_error", file=path) == 1
    # The private key is refused by name and never read.
    assert key not in opened
    assert not any(n.startswith("gman_tls_cert") or n.startswith("gman_tls_crl") for n, _ in data)


def test_parsed_files_are_cached_until_replaced(client, monkeypatch, pki):
    (path,) = watch(monkeypatch, pki, "crl.pem")
    calls = []
    real_parse = exposition.parse_tls
    monkeypatch.setattr(exposition, "parse_tls", lambda data: calls.append(1) or real_parse(data))
    client.get("/metrics")
    client.get("/metrics")
    assert len(calls) == 1
    # Replaced in place (cp keeps the inode of the bind mount): the new nextUpdate is read.
    ca_key = ec.generate_private_key(ec.SECP256R1())
    newer = crl(name("GMan pilot CA"), ca_key, 365).public_bytes(PEM)
    with open(path, "wb") as handle:
        handle.write(newer)
    stat = os.stat(path)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    data = samples(client.get("/metrics").text)
    assert len(calls) == 2
    assert find(data, "gman_tls_crl_next_update_timestamp_seconds", file=path) == (
        NOW + timedelta(days=365)).timestamp()
    # A file that turns bad is reported as an error, not served from the cache.
    os.remove(path)
    assert find(samples(client.get("/metrics").text), "gman_tls_file_error", file=path) == 1


def test_nothing_is_watched_by_default(client, monkeypatch):
    monkeypatch.delenv("GMAN_TLS_WATCH_FILES", raising=False)
    assert "gman_tls_file_error{" not in client.get("/metrics").text
