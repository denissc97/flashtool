"""Certificate providers for Authentication 0x29 (docs/ARCHITECTURE.md §4.7.2).

A provider returns the tester certificate chain and signs challenges. Private keys
never leave the provider: callers only ever get signatures.
"""

from __future__ import annotations

import importlib.util
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa


class CertificateError(Exception):
    pass


@dataclass
class CertConfig:
    provider: str = "none"  # none | file | python
    cert: str | None = None  # file: tester certificate (PEM or DER)
    key: str | None = None  # file: private key (PEM or DER)
    chain: list[str] = field(default_factory=list)  # file: intermediate certificates
    key_password_env: str | None = None  # file: env variable holding the key password
    path: str | None = None  # python: plugin module (e.g. an adapter for ZenZefi)
    role: str = ""  # passed to the provider (e.g. "supplier", "development")
    ecu: str = ""  # ECU identity passed to the provider; defaults to the target id
    rsa_padding: str = "pss"  # pss | pkcs1v15
    extra: dict = field(default_factory=dict)


def load_certificate(data: bytes) -> x509.Certificate:
    try:
        if data.lstrip().startswith(b"-----BEGIN"):
            return x509.load_pem_x509_certificate(data)
        return x509.load_der_x509_certificate(data)
    except ValueError as exc:
        raise CertificateError(f"invalid certificate: {exc}") from exc


def to_der(data: bytes) -> bytes:
    return load_certificate(data).public_bytes(serialization.Encoding.DER)


def describe_certificate(der: bytes) -> dict:
    cert = load_certificate(der)
    return {
        "subject": cert.subject.rfc4514_string(),
        "issuer": cert.issuer.rfc4514_string(),
        "serial": f"{cert.serial_number:X}",
        "not_after": cert.not_valid_after_utc.isoformat(),
    }


def sign_with_key(key, data: bytes, rsa_padding: str = "pss") -> bytes:
    if isinstance(key, ec.EllipticCurvePrivateKey):
        return key.sign(data, ec.ECDSA(hashes.SHA256()))
    if isinstance(key, ed25519.Ed25519PrivateKey):
        return key.sign(data)
    if isinstance(key, rsa.RSAPrivateKey):
        pad = (padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH)
               if rsa_padding == "pss" else padding.PKCS1v15())
        return key.sign(data, pad, hashes.SHA256())
    raise CertificateError(f"unsupported key type {type(key).__name__}")


class CertificateProvider(ABC):
    @abstractmethod
    def certificate_chain(self, ecu: str, role: str) -> list[bytes]:
        """DER certificates, tester certificate first."""

    @abstractmethod
    def sign(self, ecu: str, role: str, data: bytes) -> bytes: ...

    def describe(self) -> dict:
        return {"provider": type(self).__name__}


class FileCertificateProvider(CertificateProvider):
    """Certificate and key from files. Meant for development and simulators."""

    def __init__(self, cert: str, key: str, chain: list[str] | None = None,
                 key_password: bytes | None = None, rsa_padding: str = "pss"):
        try:
            with open(cert, "rb") as f:
                self._chain = [to_der(f.read())]
            for path in chain or []:
                with open(path, "rb") as f:
                    self._chain.append(to_der(f.read()))
            with open(key, "rb") as f:
                raw = f.read()
        except OSError as exc:
            raise CertificateError(str(exc)) from exc
        try:
            if raw.lstrip().startswith(b"-----BEGIN"):
                self._key = serialization.load_pem_private_key(raw, key_password)
            else:
                self._key = serialization.load_der_private_key(raw, key_password)
        except (ValueError, TypeError) as exc:
            raise CertificateError(f"cannot load private key {key}: {exc}") from exc
        self._rsa_padding = rsa_padding
        self.cert_path = cert

    def certificate_chain(self, ecu: str, role: str) -> list[bytes]:
        return list(self._chain)

    def sign(self, ecu: str, role: str, data: bytes) -> bytes:
        return sign_with_key(self._key, data, self._rsa_padding)

    def describe(self) -> dict:
        return {"provider": "file", "cert": self.cert_path}


class PythonCertificateProvider(CertificateProvider):
    """Plugin module with `certificate_chain(ecu, role) -> list[bytes]` and `sign(ecu, role, data) -> bytes`.

    This is where an adapter to a certificate service (e.g. ZenZefi) plugs in.
    """

    def __init__(self, path: str):
        spec = importlib.util.spec_from_file_location(f"flashtool_certs_{abs(hash(path))}", path)
        if spec is None or spec.loader is None:
            raise CertificateError(f"cannot load certificate plugin {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for name in ("certificate_chain", "sign"):
            if not callable(getattr(module, name, None)):
                raise CertificateError(f"{path} must define {name}()")
        self._module, self.path = module, path

    def certificate_chain(self, ecu: str, role: str) -> list[bytes]:
        return [to_der(bytes(c)) for c in self._module.certificate_chain(ecu, role)]

    def sign(self, ecu: str, role: str, data: bytes) -> bytes:
        return bytes(self._module.sign(ecu, role, bytes(data)))

    def describe(self) -> dict:
        return {"provider": "python", "path": self.path}


def create_certificate_provider(cfg: CertConfig, base_dir: str = ".") -> CertificateProvider | None:
    def resolve(p: str | None, what: str) -> str:
        if not p:
            raise CertificateError(f"certificate provider '{cfg.provider}' needs '{what}'")
        return p if os.path.isabs(p) else os.path.join(base_dir, p)

    if cfg.provider == "none":
        return None
    if cfg.provider == "file":
        password = os.environ.get(cfg.key_password_env, "").encode() if cfg.key_password_env else None
        return FileCertificateProvider(resolve(cfg.cert, "cert"), resolve(cfg.key, "key"),
                                       [resolve(c, "chain") for c in cfg.chain], password or None, cfg.rsa_padding)
    if cfg.provider == "python":
        return PythonCertificateProvider(resolve(cfg.path, "path"))
    if cfg.provider == "zenzefi":
        raise CertificateError(
            "the ZenZefi adapter needs the ZenZefi API documentation (open question Q-10); "
            "until then use provider 'python' with an adapter module"
        )
    raise CertificateError(f"unknown certificate provider '{cfg.provider}'")


class TargetCredentials:
    """What the sequence engine sees: certificates and signatures for one ECU."""

    def __init__(self, provider: CertificateProvider, ecu: str, role: str):
        self.provider, self.ecu, self.role = provider, ecu, role

    def certificate_chain(self) -> list[bytes]:
        chain = self.provider.certificate_chain(self.ecu, self.role)
        if not chain:
            raise CertificateError("certificate provider returned no certificate")
        return chain

    def sign(self, data: bytes) -> bytes:
        return self.provider.sign(self.ecu, self.role, data)
