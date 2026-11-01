"""Bus-independent transport interface (docs/ARCHITECTURE.md §4.1)."""

from __future__ import annotations

from abc import ABC, abstractmethod


class TransportError(Exception):
    pass


class Transport(ABC):
    """Carries complete UDS PDUs. Implementations: ISO-TP on CAN, DoIP, (LIN-TP)."""

    name = "transport"
    # False when nodes never answer functionally addressed requests (LIN: several slaves
    # answering one 0x3D header would collide), so the client does not wait for a response.
    functional_responses = True

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def send(self, payload: bytes, functional: bool = False) -> None: ...

    @abstractmethod
    def recv(self, timeout: float) -> bytes | None:
        """Return one complete PDU, or None on timeout."""

    def set_params(self, **overrides) -> dict:
        """Change transport parameters (e.g. STmin) at runtime.

        Returns the previous values so the caller can restore them.
        """
        if overrides:
            raise TransportError(f"{self.name} does not support parameters {sorted(overrides)}")
        return {}

    def flush_rx(self) -> None:
        while self.recv(0) is not None:
            pass

    @property
    def max_payload(self) -> int:
        return 4095

    def describe(self) -> dict:
        return {"type": self.name}

    def __enter__(self) -> Transport:
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()
