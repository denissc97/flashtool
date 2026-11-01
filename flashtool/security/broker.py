"""CredentialBroker: one process that owns all certificate providers (docs/ARCHITECTURE.md §4.7.2).

Flash job processes receive a proxy (proxies are picklable and reconnect on their
own). Keys stay in the broker process, and requests from parallel jobs, e.g. to a
certificate service, are serialised.
"""

from __future__ import annotations

import multiprocessing as mp
import threading
from multiprocessing.managers import BaseManager

from .certs import CertConfig, CertificateError, CertificateProvider, create_certificate_provider


class CredentialBroker:
    def __init__(self) -> None:
        self._providers: dict[str, CertificateProvider] = {}
        self._lock = threading.Lock()
        self._log: list[dict] = []

    def register(self, key: str, cfg: dict, base_dir: str) -> dict:
        with self._lock:
            if key not in self._providers:
                provider = create_certificate_provider(CertConfig(**cfg), base_dir)
                if provider is None:
                    raise CertificateError("certificate provider is 'none'")
                self._providers[key] = provider
            return self._providers[key].describe()

    def certificate_chain(self, key: str, ecu: str, role: str) -> list[bytes]:
        with self._lock:
            self._log.append({"op": "certificate_chain", "key": key, "ecu": ecu, "role": role})
            return self._provider(key).certificate_chain(ecu, role)

    def sign(self, key: str, ecu: str, role: str, data: bytes) -> bytes:
        with self._lock:
            self._log.append({"op": "sign", "key": key, "ecu": ecu, "role": role, "bytes": len(data)})
            return self._provider(key).sign(ecu, role, data)

    def usage(self) -> list[dict]:
        with self._lock:
            return list(self._log)

    def _provider(self, key: str) -> CertificateProvider:
        if key not in self._providers:
            raise CertificateError(f"no certificate provider registered for '{key}'")
        return self._providers[key]


class BrokerManager(BaseManager):
    pass


BrokerManager.register("CredentialBroker", CredentialBroker)


def start_broker() -> tuple[BrokerManager, object]:
    """Start the broker process; returns (manager, broker proxy)."""
    manager = BrokerManager(ctx=mp.get_context("spawn"))
    manager.start()
    return manager, manager.CredentialBroker()  # type: ignore[attr-defined]


class BrokeredProvider(CertificateProvider):
    """Used inside a job process: forwards every call to the broker."""

    def __init__(self, broker, key: str):
        self._broker, self._key = broker, key

    def certificate_chain(self, ecu: str, role: str) -> list[bytes]:
        return self._broker.certificate_chain(self._key, ecu, role)

    def sign(self, ecu: str, role: str, data: bytes) -> bytes:
        return self._broker.sign(self._key, ecu, role, data)

    def describe(self) -> dict:
        return {"provider": "broker", "key": self._key}
