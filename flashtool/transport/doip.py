"""DoIP transport (ISO 13400-2) over the operating system's network stack.

Vector VN56xx interfaces are used through their network adapter, like any NIC.
"""

from __future__ import annotations

from dataclasses import dataclass

from doipclient import DoIPClient

from .base import Transport, TransportError


@dataclass
class DoIpConfig:
    ecu_ip: str = "127.0.0.1"
    ecu_address: int = 0x0010
    tester_address: int = 0x0E00
    functional_address: int = 0xE400
    tcp_port: int = 13400
    activation_type: int | None = 0x00
    protocol_version: int = 0x02
    use_tls: bool = False
    ack_timeout_s: float = 2.0


# doipclient waits on its TCP socket with the socket timeout (2 s by default), not with
# the timeout passed to the call, so the socket gets a short poll interval instead.
_POLL_S = 0.02


class DoIpTransport(Transport):
    name = "doip"

    def __init__(self, config: DoIpConfig):
        self.config = config
        self._client: DoIPClient | None = None

    def open(self) -> None:
        c = self.config
        try:
            self._client = DoIPClient(
                c.ecu_ip,
                c.ecu_address,
                tcp_port=c.tcp_port,
                activation_type=c.activation_type,
                protocol_version=c.protocol_version,
                client_logical_address=c.tester_address,
                use_secure=c.use_tls,
            )
        except (OSError, ConnectionError) as exc:
            raise TransportError(f"DoIP connection to {c.ecu_ip}:{c.tcp_port} failed: {exc}") from exc
        self._socket_timeout(_POLL_S)

    def _socket_timeout(self, seconds: float) -> None:
        assert self._client is not None
        self._client._tcp_sock.settimeout(seconds)

    def flush_rx(self) -> None:
        if self._client is None:
            return
        self._socket_timeout(0.0005)
        try:
            while self.recv(0.0005) is not None:
                pass
        finally:
            self._socket_timeout(_POLL_S)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def send(self, payload: bytes, functional: bool = False) -> None:
        if self._client is None:
            raise TransportError("transport is not open")
        target = self.config.functional_address if functional else self.config.ecu_address
        try:
            self._client.send_diagnostic_to_address(target, bytearray(payload), timeout=self.config.ack_timeout_s)
        except (OSError, TimeoutError) as exc:
            raise TransportError(f"DoIP send failed: {exc}") from exc

    def recv(self, timeout: float) -> bytes | None:
        if self._client is None:
            raise TransportError("transport is not open")
        try:
            # doipclient treats timeout=0 as "wait forever"
            return bytes(self._client.receive_diagnostic(timeout=max(timeout, 0.001)))
        except TimeoutError:
            return None

    @property
    def max_payload(self) -> int:
        return 0xFFFF

    def describe(self) -> dict:
        c = self.config
        return {
            "type": "doip",
            "ecu_ip": c.ecu_ip,
            "tcp_port": c.tcp_port,
            "ecu_address": hex(c.ecu_address),
            "tester_address": hex(c.tester_address),
            "functional_address": hex(c.functional_address),
            "tls": c.use_tls,
        }
