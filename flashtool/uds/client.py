"""UDS client (ISO 14229-1) with explicit control over timing and addressing."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from ..transport.base import Transport
from .nrc import SUBFUNCTION_SERVICES, nrc_name, service_name
from .trace import TraceSink


@dataclass
class UdsTiming:
    p2_ms: int = 150  # P2client: wait for the first response
    p2_star_ms: int = 5000  # P2*client: wait after NRC 0x78
    max_pending: int = 500  # max consecutive 0x78 responses for one request
    busy_retries: int = 3  # repeats on NRC 0x21 busyRepeatRequest
    busy_delay_ms: int = 50
    tester_present_s: float = 2.0
    adopt_session_timing: bool = True  # use P2/P2* from the 0x50 response if longer
    session_timing_margin_ms: int = 50


class UdsError(Exception):
    pass


class UdsTimeout(UdsError):
    pass


class NegativeResponse(UdsError):
    def __init__(self, sid: int, nrc: int, data: bytes):
        self.sid, self.nrc, self.data = sid, nrc, data
        super().__init__(f"{service_name(sid)}: NRC 0x{nrc:02X} {nrc_name(nrc)}")


class UnexpectedResponse(UdsError):
    pass


@dataclass
class Response:
    data: bytes

    @property
    def positive(self) -> bool:
        return self.data[0] != 0x7F

    @property
    def nrc(self) -> int | None:
        return None if self.positive else self.data[2]


def _lv(data: bytes) -> bytes:
    """2-byte length + value, as used by service 0x29."""
    if len(data) > 0xFFFF:
        raise ValueError("0x29 parameter longer than 65535 bytes")
    return len(data).to_bytes(2, "big") + data


def _read_lv(data: bytes, what: str) -> tuple[bytes, bytes]:
    if len(data) < 2 or len(data) < 2 + int.from_bytes(data[:2], "big"):
        raise UnexpectedResponse(f"Authentication: truncated {what}")
    n = int.from_bytes(data[:2], "big")
    return data[2 : 2 + n], data[2 + n :]


def encode_address_and_size(alfid: int, address: int, size: int) -> bytes:
    addr_len, size_len = alfid & 0x0F, (alfid >> 4) & 0x0F
    return bytes([alfid]) + address.to_bytes(addr_len, "big") + size.to_bytes(size_len, "big")


class UdsClient:
    def __init__(self, transport: Transport, timing: UdsTiming | None = None, trace: TraceSink | None = None):
        self.transport = transport
        self.timing = timing or UdsTiming()
        self.trace = trace or TraceSink()
        self.p2_ms = self.timing.p2_ms
        self.p2_star_ms = self.timing.p2_star_ms
        self._lock = threading.RLock()

    # -- core ------------------------------------------------------------
    def request(
        self,
        payload: bytes,
        *,
        functional: bool = False,
        timeout_ms: int | None = None,
        allow_nrc: tuple[int, ...] | list[int] = (),
        expect_response: bool | None = None,
    ) -> Response | None:
        """Send one request and wait for its final response.

        Returns None when no response is expected (suppressPosRspMsgIndicationBit set,
        or expect_response=False) and none arrived. Raises NegativeResponse for an NRC
        that is not in `allow_nrc`.
        """
        payload = bytes(payload)
        if not payload:
            raise ValueError("empty UDS request")
        sid = payload[0]
        suppressed = sid in SUBFUNCTION_SERVICES and len(payload) > 1 and bool(payload[1] & 0x80)
        if expect_response is None:
            expect_response = not suppressed and (not functional or self.transport.functional_responses)

        busy = 0
        with self._lock:
            while True:
                self.transport.flush_rx()
                self.trace.record("TX", payload, service_name(sid), functional)
                self.transport.send(payload, functional=functional)
                resp = self._wait_response(sid, timeout_ms, expect_response)
                if resp is None:
                    return None
                if resp.positive or resp.nrc in allow_nrc:
                    return resp
                if resp.nrc == 0x21 and busy < self.timing.busy_retries:
                    busy += 1
                    time.sleep(self.timing.busy_delay_ms / 1000)
                    continue
                raise NegativeResponse(sid, resp.nrc, resp.data)

    def _wait_response(self, sid: int, timeout_ms: int | None, expect_response: bool) -> Response | None:
        timeout = (timeout_ms if timeout_ms is not None else self.p2_ms) / 1000
        pending = 0
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            data = self.transport.recv(max(remaining, 0)) if remaining > 0 else None
            if data is None:
                if not expect_response and pending == 0:
                    return None
                raise UdsTimeout(f"{service_name(sid)}: no response within {timeout * 1000:.0f} ms")
            if len(data) >= 3 and data[0] == 0x7F and data[1] == sid:
                nrc = data[2]
                if nrc == 0x78:
                    pending += 1
                    self.trace.record("RX", data, "responsePending")
                    if pending > self.timing.max_pending:
                        raise UdsTimeout(f"{service_name(sid)}: more than {self.timing.max_pending} x NRC 0x78")
                    timeout = self.p2_star_ms / 1000
                    deadline = time.monotonic() + timeout
                    expect_response = True
                    continue
                self.trace.record("RX", data, f"NRC 0x{nrc:02X} {nrc_name(nrc)}")
                return Response(data)
            if data and data[0] == sid + 0x40:
                self.trace.record("RX", data)
                return Response(data)
            self.trace.record("RX", data, "unexpected, ignored")

    def tester_present_nowait(self, functional: bool = True) -> bool:
        """3E 80 without waiting. Skipped while another request is in progress."""
        if not self._lock.acquire(blocking=False):
            return False
        try:
            self.trace.record("TX", b"\x3e\x80", "TesterPresent", functional)
            self.transport.send(b"\x3e\x80", functional=functional)
            return True
        finally:
            self._lock.release()

    # -- services --------------------------------------------------------
    def diagnostic_session_control(self, session: int, *, functional: bool = False, **kw) -> Response | None:
        resp = self.request(bytes([0x10, session]), functional=functional, **kw)
        if resp is not None and resp.positive and len(resp.data) >= 6 and self.timing.adopt_session_timing:
            d = resp.data
            margin = self.timing.session_timing_margin_ms
            self.p2_ms = max(self.timing.p2_ms, ((d[2] << 8) | d[3]) + margin)
            self.p2_star_ms = max(self.timing.p2_star_ms, ((d[4] << 8) | d[5]) * 10 + margin)
        return resp

    def ecu_reset(self, reset_type: int = 0x01, **kw) -> Response | None:
        return self.request(bytes([0x11, reset_type]), **kw)

    def security_access_seed(self, level: int, **kw) -> bytes:
        resp = self._positive(self.request(bytes([0x27, level]), **kw), 0x27)
        if resp.data[1] != level:
            raise UnexpectedResponse(f"SecurityAccess: seed for level 0x{resp.data[1]:02X}, expected 0x{level:02X}")
        return resp.data[2:]

    def security_access_key(self, level: int, key: bytes, **kw) -> None:
        self._positive(self.request(bytes([0x27, level]) + key, **kw), 0x27)

    def routine_control(self, sub: int, rid: int, data: bytes = b"", **kw) -> Response:
        resp = self._positive(self.request(bytes([0x31, sub]) + rid.to_bytes(2, "big") + data, **kw), 0x31)
        if resp.positive and resp.data[2:4] != rid.to_bytes(2, "big"):
            raise UnexpectedResponse(f"RoutineControl: response for RID {resp.data[2:4].hex()}, expected {rid:04X}")
        return resp

    def request_download(self, dfi: int, alfid: int, address: int, size: int, **kw) -> int:
        """Return maxNumberOfBlockLength (includes SID and block sequence counter)."""
        resp = self._positive(
            self.request(bytes([0x34, dfi]) + encode_address_and_size(alfid, address, size), **kw), 0x34
        )
        nlen = resp.data[1] >> 4
        if nlen == 0 or len(resp.data) < 2 + nlen:
            raise UnexpectedResponse("RequestDownload: malformed lengthFormatIdentifier")
        return int.from_bytes(resp.data[2 : 2 + nlen], "big")

    def transfer_data(self, bsc: int, data: bytes, **kw) -> Response:
        resp = self._positive(self.request(bytes([0x36, bsc]) + data, **kw), 0x36)
        if len(resp.data) < 2 or resp.data[1] != bsc:
            raise UnexpectedResponse(f"TransferData: block sequence counter mismatch (sent {bsc:02X})")
        return resp

    def request_transfer_exit(self, data: bytes = b"", **kw) -> Response:
        return self._positive(self.request(bytes([0x37]) + data, **kw), 0x37)

    def read_data_by_identifier(self, did: int, **kw) -> bytes:
        resp = self._positive(self.request(bytes([0x22]) + did.to_bytes(2, "big"), **kw), 0x22)
        if resp.data[1:3] != did.to_bytes(2, "big"):
            raise UnexpectedResponse(f"ReadDataByIdentifier: response for DID {resp.data[1:3].hex()}")
        return resp.data[3:]

    def write_data_by_identifier(self, did: int, data: bytes, **kw) -> None:
        self._positive(self.request(bytes([0x2E]) + did.to_bytes(2, "big") + data, **kw), 0x2E)

    # -- Authentication 0x29 (ISO 14229-1:2020) ----------------------------
    def authentication_configuration(self, **kw) -> int:
        """29 08 -> authenticationReturnParameter (0x02 = APCE, 0x03/0x04 = ACR)."""
        resp = self._positive(self.request(b"\x29\x08", **kw), 0x29)
        return resp.data[2]

    def verify_certificate_unidirectional(
        self, certificate: bytes, challenge_client: bytes = b"", communication_configuration: int = 0x00, **kw
    ) -> tuple[int, bytes, bytes]:
        """29 01 -> (returnParameter, challengeServer, ephemeralPublicKeyServer)."""
        req = (bytes([0x29, 0x01, communication_configuration]) + _lv(certificate) + _lv(challenge_client))
        d = self._positive(self.request(req, **kw), 0x29).data
        ret, rest = d[2], d[3:]
        challenge, rest = _read_lv(rest, "challengeServer")
        eph = _read_lv(rest, "ephemeralPublicKeyServer")[0] if rest else b""
        return ret, challenge, eph

    def proof_of_ownership(self, proof: bytes, ephemeral_public_key: bytes = b"", **kw) -> tuple[int, bytes]:
        """29 03 -> (returnParameter, sessionKeyInfo)."""
        d = self._positive(self.request(b"\x29\x03" + _lv(proof) + _lv(ephemeral_public_key), **kw), 0x29).data
        session_key = _read_lv(d[3:], "sessionKeyInfo")[0] if len(d) > 3 else b""
        return d[2], session_key

    def deauthenticate(self, **kw) -> int:
        return self._positive(self.request(b"\x29\x00", **kw), 0x29).data[2]

    @staticmethod
    def _positive(resp: Response | None, sid: int) -> Response:
        if resp is None:
            raise UdsTimeout(f"{service_name(sid)}: no response")
        return resp
