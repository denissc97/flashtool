"""Test PKI for the simulator: a root CA and a tester certificate. Never use outside tests."""

from __future__ import annotations

import datetime
import os

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "FlashTool test PKI"),
                      x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _cert(subject: str, issuer: str, pub, signing_key, ca: bool, days: int) -> x509.Certificate:
    now = datetime.datetime.now(datetime.timezone.utc)
    return (
        x509.CertificateBuilder()
        .subject_name(_name(subject))
        .issuer_name(_name(issuer))
        .public_key(pub)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .sign(signing_key, hashes.SHA256())
    )


def make_test_pki(out_dir: str, tester_cn: str = "FlashTool tester", days: int = 365) -> dict[str, str]:
    """Writes root_ca.pem, tester.pem, tester.key (EC P-256). Returns the paths."""
    os.makedirs(out_dir, exist_ok=True)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = _cert("FlashTool test root CA", "FlashTool test root CA", ca_key.public_key(), ca_key, True, days * 5)
    key = ec.generate_private_key(ec.SECP256R1())
    tester = _cert(tester_cn, "FlashTool test root CA", key.public_key(), ca_key, False, days)
    paths = {name: os.path.join(out_dir, name) for name in ("root_ca.pem", "tester.pem", "tester.key")}
    with open(paths["root_ca.pem"], "wb") as f:
        f.write(ca.public_bytes(serialization.Encoding.PEM))
    with open(paths["tester.pem"], "wb") as f:
        f.write(tester.public_bytes(serialization.Encoding.PEM))
    with open(paths["tester.key"], "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    return paths
