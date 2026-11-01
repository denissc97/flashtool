"""Simulated ECU with a UDS bootloader, used for development and tests without hardware.

Transport independent: `handle()` takes one request and returns the responses to
send, each with a delay (to emulate NRC 0x78 during long operations).
"""

from __future__ import annotations

import os
import threading
import time
import zlib
from dataclasses import dataclass, field

DEMO_SECRET = 0x5A3C96E1


def demo_key(seed: bytes, secret: int = DEMO_SECRET) -> bytes:
    """Key function of the simulator. Real ECUs use the OEM's algorithm."""
    v = int.from_bytes(seed, "big") ^ secret
    v = ((v << 7) | (v >> 25)) & 0xFFFFFFFF
    return ((v + 0x1234) & 0xFFFFFFFF).to_bytes(4, "big")


@dataclass
class Fault:
    """Return `nrc` for the `at`-th request of service `sid` (1-based), `times` times in a row."""

    sid: int
    nrc: int
    at: int = 1
    times: int = 1


@dataclass
class SimConfig:
    security_level: int = 0x01
    secret: int = DEMO_SECRET
    max_block_length: int = 0x0802  # 2048 data bytes per TransferData
    erase_time_ms: int = 150
    check_time_ms: int = 50
    pending_interval_ms: int = 40
    reset_time_ms: int = 200
    p2_server_ms: int = 50
    p2_star_server_ms: int = 5000
    memory: list[tuple[int, int]] = field(default_factory=lambda: [(0x0000_0000, 0x1_0000_0000)])
    require_erase: bool = True
    faults: list[Fault] = field(default_factory=list)
    auth_trust_anchor: str | None = None  # root CA (PEM/DER file) for Authentication 0x29
    require_authentication: bool = False  # programming needs 0x29 instead of 0x27
    sleep_timeout_ms: int | None = None  # asleep until woken up; falls asleep again without keep-alive
    wakeup_id: int | None = None  # CAN id that wakes / keeps the ECU awake (None: any frame)
    logical_blocks: list[tuple[int, int, int]] = field(default_factory=list)  # (index, address, size): erase by index
    routines: list[int] = field(default_factory=list)  # extra routine ids answered "correct" (e.g. activate boot)
    vin: str = "WFLASHTOOLSIM0001"
    sw_version: str = "BOOT-1.0"

    @classmethod
    def from_dict(cls, d: dict | None) -> SimConfig:
        d = dict(d or {})
        d["faults"] = [Fault(**f) for f in d.get("faults", [])]
        for key in ("memory", "logical_blocks"):
            if key in d:
                d[key] = [tuple(m) for m in d[key]]
        return cls(**d)


Reply = tuple[float, bytes]  # (delay in seconds, response)


def _nrc(sid: int, code: int) -> list[Reply]:
    return [(0.0, bytes([0x7F, sid, code]))]


class SimulatedEcu:
    def __init__(self, config: SimConfig | None = None):
        self.cfg = config or SimConfig()
        self.lock = threading.Lock()
        self.session = 0x01
        self.unlocked = False
        self.authenticated = False
        self._auth_cert = None
        self._auth_challenge: bytes | None = None
        self._trust_anchor = None
        if self.cfg.auth_trust_anchor:
            from cryptography import x509

            with open(self.cfg.auth_trust_anchor, "rb") as f:
                raw = f.read()
            self._trust_anchor = (x509.load_pem_x509_certificate(raw) if raw.lstrip().startswith(b"-----")
                                  else x509.load_der_x509_certificate(raw))
        self._seed: bytes | None = None
        self._bad_keys = 0
        self._download: dict | None = None
        self._since_check: list[bytes] = []
        self._checks_ok = 0
        self._checks_failed = 0
        self.erased: list[tuple[int, int]] = []
        self.written: list[tuple[int, bytes]] = []  # (address, data) of completed downloads
        self.dids: dict[int, bytes] = {
            0xF190: self.cfg.vin.encode(),
            0xF189: self.cfg.sw_version.encode(),
            0xF15A: bytes(9),
            0xF187: b"FT-0001-SIM",
            0xF18A: b"FlashTool Simulator",
            0xF18C: b"SIM000123",
            0xF191: b"HW-01",
            0xF195: b"1.4.2",
        }
        self.offline_until = 0.0
        self.awake_until = 0.0 if self.cfg.sleep_timeout_ms else float("inf")
        self.request_log: list[bytes] = []
        self._counts: dict[int, int] = {}

    # -- inspection helpers (tests) -----------------------------------------
    def read_memory(self, address: int, length: int) -> bytes:
        out = bytearray(b"\xff" * length)
        for addr, data in self.written:
            lo, hi = max(addr, address), min(addr + len(data), address + length)
            if lo < hi:
                out[lo - address : hi - address] = data[lo - addr : hi - addr]
        return bytes(out)

    @property
    def online(self) -> bool:
        now = time.monotonic()
        return now >= self.offline_until and now < self.awake_until

    def wake(self) -> None:
        """A wake-up or keep-alive frame was seen on the bus."""
        if self.cfg.sleep_timeout_ms:
            self.awake_until = time.monotonic() + self.cfg.sleep_timeout_ms / 1000

    # -- request handling ---------------------------------------------------
    def handle(self, req: bytes, functional: bool = False) -> list[Reply]:
        with self.lock:
            if not req or not self.online:
                return []
            self.request_log.append(bytes(req))
            sid = req[0]
            fault = self._fault(sid)
            if fault is not None:
                return _nrc(sid, fault)
            handler = getattr(self, f"_s{sid:02x}", None)
            if handler is None:
                return [] if functional else _nrc(sid, 0x11)
            replies = handler(req)
            if functional:
                # Functional requests: no NRC 0x11/0x12/0x31 (ISO 14229-1 §7.5)
                replies = [r for r in replies if not (r[1][0] == 0x7F and r[1][2] in (0x11, 0x12, 0x31, 0x7E, 0x7F))]
            return replies

    def _fault(self, sid: int) -> int | None:
        n = self._counts[sid] = self._counts.get(sid, 0) + 1
        for f in self.cfg.faults:
            if f.sid == sid and f.at <= n < f.at + f.times:
                return f.nrc
        return None

    @staticmethod
    def _suppress(req: bytes) -> bool:
        return len(req) > 1 and bool(req[1] & 0x80)

    def _positive(self, req: bytes, resp: bytes) -> list[Reply]:
        return [] if self._suppress(req) else [(0.0, resp)]

    def _pending(self, sid: int, total_ms: int, final: bytes) -> list[Reply]:
        interval = self.cfg.pending_interval_ms / 1000
        replies: list[Reply] = [(0.0, bytes([0x7F, sid, 0x78]))]
        elapsed = interval
        while elapsed < total_ms / 1000:
            replies.append((interval, bytes([0x7F, sid, 0x78])))
            elapsed += interval
        replies.append((interval, final))
        return replies

    # 0x10 DiagnosticSessionControl
    def _s10(self, req: bytes) -> list[Reply]:
        if len(req) != 2:
            return _nrc(0x10, 0x13)
        session = req[1] & 0x7F
        if session not in (0x01, 0x02, 0x03):
            return _nrc(0x10, 0x12)
        if session == 0x02 and self.session == 0x01:
            return _nrc(0x10, 0x22)
        if session != self.session:
            self.unlocked = False
            if session == 0x01:
                self.authenticated = False
            self._seed = None
            self._download = None
        self.session = session
        p2 = self.cfg.p2_server_ms.to_bytes(2, "big")
        p2s = (self.cfg.p2_star_server_ms // 10).to_bytes(2, "big")
        return self._positive(req, bytes([0x50, session]) + p2 + p2s)

    # 0x11 ECUReset
    def _s11(self, req: bytes) -> list[Reply]:
        if len(req) != 2 or req[1] & 0x7F not in (0x01, 0x02, 0x03):
            return _nrc(0x11, 0x12)
        replies = self._positive(req, bytes([0x51, req[1] & 0x7F]))
        self.session, self.unlocked, self._seed, self._download = 0x01, False, None, None
        self.authenticated = False
        self.offline_until = time.monotonic() + self.cfg.reset_time_ms / 1000
        return replies

    # 0x14 ClearDiagnosticInformation
    def _s14(self, req: bytes) -> list[Reply]:
        return [(0.0, b"\x54")]

    # 0x22 ReadDataByIdentifier
    def _s22(self, req: bytes) -> list[Reply]:
        if len(req) != 3:
            return _nrc(0x22, 0x13)
        did = int.from_bytes(req[1:3], "big")
        if did == 0xF186:
            return [(0.0, bytes([0x62]) + req[1:3] + bytes([self.session]))]
        if did not in self.dids:
            return _nrc(0x22, 0x31)
        return [(0.0, bytes([0x62]) + req[1:3] + self.dids[did])]

    # 0x27 SecurityAccess
    def _s27(self, req: bytes) -> list[Reply]:
        if len(req) < 2:
            return _nrc(0x27, 0x13)
        sub = req[1] & 0x7F
        if self.session == 0x01:
            return _nrc(0x27, 0x7F)
        if sub == self.cfg.security_level:
            if self.unlocked:
                return [(0.0, bytes([0x67, sub]) + bytes(4))]
            if self._bad_keys >= 3:
                return _nrc(0x27, 0x37)
            self._seed = os.urandom(4)
            return [(0.0, bytes([0x67, sub]) + self._seed)]
        if sub == self.cfg.security_level + 1:
            if self._seed is None:
                return _nrc(0x27, 0x24)
            expected = demo_key(self._seed, self.cfg.secret)
            self._seed = None
            if req[2:] != expected:
                self._bad_keys += 1
                return _nrc(0x27, 0x36 if self._bad_keys >= 3 else 0x35)
            self.unlocked = True
            self._bad_keys = 0
            return [(0.0, bytes([0x67, sub]))]
        return _nrc(0x27, 0x12)

    # 0x28 CommunicationControl
    def _s28(self, req: bytes) -> list[Reply]:
        if len(req) != 3:
            return _nrc(0x28, 0x13)
        if req[1] & 0x7F > 0x03:
            return _nrc(0x28, 0x12)
        return self._positive(req, bytes([0x68, req[1] & 0x7F]))

    # 0x2E WriteDataByIdentifier
    def _s2e(self, req: bytes) -> list[Reply]:
        if len(req) < 4:
            return _nrc(0x2E, 0x13)
        did = int.from_bytes(req[1:3], "big")
        if did != 0xF15A:
            return _nrc(0x2E, 0x31)
        if not self._access():
            return _nrc(0x2E, 0x33)
        self.dids[did] = bytes(req[3:])
        return [(0.0, bytes([0x6E]) + req[1:3])]

    # 0x31 RoutineControl
    def _s31(self, req: bytes) -> list[Reply]:
        if len(req) < 4:
            return _nrc(0x31, 0x13)
        sub, rid = req[1] & 0x7F, int.from_bytes(req[2:4], "big")
        if sub != 0x01:
            return _nrc(0x31, 0x12)
        head = bytes([0x71, sub]) + req[2:4]
        if rid == 0x0203:  # check programming preconditions
            return [(0.0, head + b"\x00")]
        if self.session != 0x02:
            return _nrc(0x31, 0x7F)
        if not self._access():
            return _nrc(0x31, 0x33 if not self.cfg.require_authentication else 0x34)
        if rid == 0xFF00:  # erase memory: ALFID + address + size, or a logical block index
            by_index = {i: (a, n) for i, a, n in self.cfg.logical_blocks}
            parsed = by_index.get(req[4]) if len(req) == 5 and by_index else self._parse_addr_size(req[4:])
            if parsed is None:
                return _nrc(0x31, 0x13)
            address, size = parsed
            if not self._in_memory(address, size):
                return _nrc(0x31, 0x31)
            self.erased.append((address, size))
            self._since_check = []
            self._download = None  # erasing aborts an interrupted transfer
            return self._pending(0x31, self.cfg.erase_time_ms, head + b"\x00")
        if rid == 0x0202:  # check memory: CRC32 over the data downloaded since the last check
            if len(req) != 8:
                return _nrc(0x31, 0x13)
            crc = zlib.crc32(b"".join(self._since_check)) & 0xFFFFFFFF
            ok = crc == int.from_bytes(req[4:8], "big") and bool(self._since_check)
            self._since_check = []
            if ok:
                self._checks_ok += 1
            else:
                self._checks_failed += 1
            return self._pending(0x31, self.cfg.check_time_ms, head + (b"\x00" if ok else b"\x01"))
        if rid == 0xFF01:  # check programming dependencies
            ok = self._checks_ok > 0 and self._checks_failed == 0
            return [(0.0, head + (b"\x00" if ok else b"\x01"))]
        if rid in self.cfg.routines:
            return self._pending(0x31, self.cfg.check_time_ms, head + b"\x00")
        return _nrc(0x31, 0x31)

    # 0x34 RequestDownload
    def _s34(self, req: bytes) -> list[Reply]:
        if self.session != 0x02:
            return _nrc(0x34, 0x7F)
        if not self._access():
            return _nrc(0x34, 0x33 if not self.cfg.require_authentication else 0x34)
        if len(req) < 3:
            return _nrc(0x34, 0x13)
        dfi = req[1]
        parsed = self._parse_addr_size(req[2:])
        if parsed is None:
            return _nrc(0x34, 0x13)
        address, size = parsed
        if self._download is not None:
            return _nrc(0x34, 0x22)
        if not self._in_memory(address, size):
            return _nrc(0x34, 0x31)
        if self.cfg.require_erase and dfi == 0x00 and not self._is_erased(address, size):
            return _nrc(0x34, 0x70)
        self._download = {"address": address, "size": size, "dfi": dfi, "data": bytearray(), "bsc": 1, "last": None}
        mbl = self.cfg.max_block_length
        return [(0.0, bytes([0x74, 0x20]) + mbl.to_bytes(2, "big"))]

    # 0x36 TransferData
    def _s36(self, req: bytes) -> list[Reply]:
        dl = self._download
        if dl is None:
            return _nrc(0x36, 0x24)
        if len(req) < 2 or len(req) > self.cfg.max_block_length:
            return _nrc(0x36, 0x13)
        bsc = req[1]
        if bsc == dl["last"]:  # repeated block: accept without storing again
            return [(0.0, bytes([0x76, bsc]))]
        if bsc != dl["bsc"]:
            return _nrc(0x36, 0x73)
        if dl["dfi"] == 0x00 and len(dl["data"]) + len(req) - 2 > dl["size"]:
            return _nrc(0x36, 0x71)
        dl["data"] += req[2:]
        dl["last"] = bsc
        dl["bsc"] = (bsc + 1) & 0xFF
        return [(0.0, bytes([0x76, bsc]))]

    # 0x37 RequestTransferExit
    def _s37(self, req: bytes) -> list[Reply]:
        dl = self._download
        if dl is None:
            return _nrc(0x37, 0x24)
        if dl["dfi"] == 0x00 and len(dl["data"]) != dl["size"]:
            return _nrc(0x37, 0x24)
        data = bytes(dl["data"])
        self.written.append((dl["address"], data))
        self._since_check.append(data)
        self._download = None
        return [(0.0, b"\x77")]

    # 0x29 Authentication (APCE, unidirectional)
    def _s29(self, req: bytes) -> list[Reply]:
        if len(req) < 2:
            return _nrc(0x29, 0x13)
        sub = req[1] & 0x7F
        if sub == 0x08:
            return [(0.0, b"\x69\x08\x02")]
        if sub == 0x00:
            self.authenticated, self._auth_cert, self._auth_challenge = False, None, None
            return [(0.0, b"\x69\x00\x10")]
        if self._trust_anchor is None:
            return _nrc(0x29, 0x12)
        if sub == 0x01:
            return self._verify_certificate(req)
        if sub == 0x03:
            return self._proof_of_ownership(req)
        return _nrc(0x29, 0x12)

    def _verify_certificate(self, req: bytes) -> list[Reply]:
        import datetime

        from cryptography import x509

        try:
            n = int.from_bytes(req[3:5], "big")
            cert_der = req[5 : 5 + n]
            cert = x509.load_der_x509_certificate(cert_der)
        except Exception:
            return _nrc(0x29, 0x54)
        now = datetime.datetime.now(datetime.timezone.utc)
        if not cert.not_valid_before_utc <= now <= cert.not_valid_after_utc:
            return _nrc(0x29, 0x50)
        try:
            cert.verify_directly_issued_by(self._trust_anchor)
        except Exception:
            return _nrc(0x29, 0x52)
        self._auth_cert = cert
        self._auth_challenge = os.urandom(32)
        return [(0.0, b"\x69\x01\x11" + len(self._auth_challenge).to_bytes(2, "big") + self._auth_challenge + b"\x00\x00")]

    def _proof_of_ownership(self, req: bytes) -> list[Reply]:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

        if self._auth_cert is None or self._auth_challenge is None:
            return _nrc(0x29, 0x24)
        n = int.from_bytes(req[2:4], "big")
        proof = req[4 : 4 + n]
        key, data = self._auth_cert.public_key(), self._auth_challenge
        self._auth_challenge = None
        try:
            if isinstance(key, ec.EllipticCurvePublicKey):
                key.verify(proof, data, ec.ECDSA(hashes.SHA256()))
            elif isinstance(key, ed25519.Ed25519PublicKey):
                key.verify(proof, data)
            elif isinstance(key, rsa.RSAPublicKey):
                try:
                    key.verify(proof, data, padding.PSS(padding.MGF1(hashes.SHA256()), padding.PSS.DIGEST_LENGTH),
                               hashes.SHA256())
                except InvalidSignature:
                    key.verify(proof, data, padding.PKCS1v15(), hashes.SHA256())
            else:
                return _nrc(0x29, 0x53)
        except InvalidSignature:
            return _nrc(0x29, 0x58)
        self.authenticated = True
        return [(0.0, b"\x69\x03\x12\x00\x00")]

    def _access(self) -> bool:
        if self.cfg.require_authentication:
            return self.authenticated
        return self.unlocked or self.authenticated

    # 0x3E TesterPresent
    def _s3e(self, req: bytes) -> list[Reply]:
        if len(req) != 2 or req[1] & 0x7F != 0x00:
            return _nrc(0x3E, 0x12)
        return self._positive(req, b"\x7e\x00")

    # 0x85 ControlDTCSetting
    def _s85(self, req: bytes) -> list[Reply]:
        if len(req) < 2 or req[1] & 0x7F not in (0x01, 0x02):
            return _nrc(0x85, 0x12)
        return self._positive(req, bytes([0xC5, req[1] & 0x7F]))

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _parse_addr_size(data: bytes) -> tuple[int, int] | None:
        if not data:
            return None
        alfid = data[0]
        alen, slen = alfid & 0x0F, alfid >> 4
        if alen == 0 or slen == 0 or len(data) != 1 + alen + slen:
            return None
        return int.from_bytes(data[1 : 1 + alen], "big"), int.from_bytes(data[1 + alen :], "big")

    def _in_memory(self, address: int, size: int) -> bool:
        return any(start <= address and address + size <= start + length for start, length in self.cfg.memory)

    def _is_erased(self, address: int, size: int) -> bool:
        return any(a <= address and address + size <= a + n for a, n in self.erased)
