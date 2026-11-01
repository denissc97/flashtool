"""Serves a SimulatedEcu as a DoIP entity on TCP (ISO 13400-2), for localhost tests."""

from __future__ import annotations

import socket
import struct
import threading
import time

from doipclient.client import Parser
from doipclient.messages import (
    AliveCheckRequest,
    AliveCheckResponse,
    DiagnosticMessage,
    DiagnosticMessageNegativeAcknowledgement,
    DiagnosticMessagePositiveAcknowledgement,
    RoutingActivationRequest,
    RoutingActivationResponse,
    payload_message_to_type,
)

from .ecu import SimulatedEcu


def pack_doip(message, protocol_version: int = 0x02) -> bytes:
    payload = message.pack()
    header = struct.pack(
        "!BBHL", protocol_version, 0xFF ^ protocol_version, payload_message_to_type[type(message)], len(payload)
    )
    return header + payload


class DoIpEcuServer:
    def __init__(
        self,
        ecu: SimulatedEcu,
        *,
        host: str = "127.0.0.1",
        port: int = 13400,
        ecu_address: int = 0x0010,
        functional_address: int = 0xE400,
    ):
        self.ecu = ecu
        self.ecu_address = ecu_address
        self.functional_address = functional_address
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._sock.listen(4)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> DoIpEcuServer:
        t = threading.Thread(target=self._accept_loop, name="sim-doip-accept", daemon=True)
        t.start()
        self._threads.append(t)
        return self

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2)
        self._sock.close()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (socket.timeout, OSError):
                continue
            t = threading.Thread(target=self._serve, args=(conn,), name="sim-doip-conn", daemon=True)
            t.start()
            self._threads.append(t)

    def _serve(self, conn: socket.socket) -> None:
        parser = Parser()
        conn.settimeout(0.2)
        tester: int | None = None
        with conn:
            while not self._stop.is_set():
                try:
                    data = conn.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not data:
                    return
                msg = parser.read_message(data)
                while msg is not None:
                    tester = self._handle(conn, msg, tester)
                    msg = parser.read_message(b"")

    def _handle(self, conn: socket.socket, msg, tester: int | None) -> int | None:
        if isinstance(msg, RoutingActivationRequest):
            tester = msg.source_address
            conn.sendall(pack_doip(RoutingActivationResponse(tester, self.ecu_address, 0x10)))
        elif isinstance(msg, AliveCheckRequest):
            conn.sendall(pack_doip(AliveCheckResponse(self.ecu_address)))
        elif isinstance(msg, DiagnosticMessage):
            target = msg.target_address
            if tester is None or msg.source_address != tester:
                conn.sendall(pack_doip(DiagnosticMessageNegativeAcknowledgement(target, msg.source_address, 0x02)))
                return tester
            if target not in (self.ecu_address, self.functional_address):
                conn.sendall(pack_doip(DiagnosticMessageNegativeAcknowledgement(target, tester, 0x03)))
                return tester
            conn.sendall(pack_doip(DiagnosticMessagePositiveAcknowledgement(target, tester, 0x00)))
            functional = target == self.functional_address
            for delay, resp in self.ecu.handle(bytes(msg.user_data), functional):
                if delay:
                    time.sleep(delay)
                conn.sendall(pack_doip(DiagnosticMessage(self.ecu_address, tester, bytearray(resp))))
        return tester

    def __enter__(self) -> DoIpEcuServer:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
