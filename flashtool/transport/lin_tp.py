"""ISO 17987-2 transport (LIN-TP) with the tester as LIN master.

Requests go out in master request frames (0x3C), responses are collected by sending
slave response headers (0x3D). Every frame is NAD, PCI and up to 6 data bytes, unused
bytes 0xFF:

    SF  NAD 0x0L  d1..d6        L = 1..6
    FF  NAD 0x1H  LEN d1..d5    length = H << 8 | LEN, 7..4095
    CF  NAD 0x2N  d1..d6        N = 1, 2, ... 0xF, 0x0, 1 ...

There is no flow control: the master paces its own frames (`n_cs_ms`) and polls for
the slave's (`poll_interval_ms`).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from ..hal.backends import HardwareError
from ..hal.lin import DIAG_FRAME_LEN, LinMaster, create_lin_master, frame_time_max
from .base import Transport, TransportError

log = logging.getLogger(__name__)

MAX_PDU = 4095
SF_MAX = 6
FF_DATA = 5
CF_DATA = 6


@dataclass
class LinTpConfig:
    nad: int = 0x01  # node address of the ECU
    functional_nad: int = 0x7E
    baudrate: int = 19200
    n_cs_ms: float = 0  # pause between consecutive frames we send (LIN counterpart of STmin)
    poll_interval_ms: float = 10  # pause between 0x3D headers while the slave has nothing to send
    n_cr_ms: int = 1000  # max gap between received consecutive frames
    padding: int = 0xFF
    backend: str = "virtual"
    channel: str | int = "sim0"  # Vector: application channel of app_name
    app_name: str | None = "FlashTool"

    def validate(self) -> None:
        if not 0x01 <= self.nad <= 0x7D:
            raise TransportError(f"NAD 0x{self.nad:02X} out of range 0x01-0x7D (0x7E functional, 0x7F broadcast)")
        if not 0x01 <= self.functional_nad <= 0xFF or self.functional_nad == self.nad:
            raise TransportError(f"invalid functional NAD 0x{self.functional_nad:02X}")
        if not 1000 <= self.baudrate <= 20000:
            raise TransportError(f"LIN baudrate {self.baudrate} out of range 1000-20000 bit/s")
        if not 0 <= self.padding <= 0xFF:
            raise TransportError("padding must be 0x00..0xFF")
        _check_ms("n_cs_ms", self.n_cs_ms)
        _check_ms("poll_interval_ms", self.poll_interval_ms)
        if self.n_cr_ms <= 0:
            raise TransportError("n_cr_ms must be positive")


def _check_ms(name: str, value) -> float:
    try:
        ms = float(value)
    except (TypeError, ValueError):
        raise TransportError(f"{name} must be a number of milliseconds, got {value!r}") from None
    if ms < 0:
        raise TransportError(f"{name} must not be negative")
    return ms


# -- framing (also used by the simulated slave) ---------------------------------


def segment(nad: int, payload: bytes, padding: int = 0xFF) -> list[bytes]:
    """Split one PDU into 8-byte LIN-TP frames."""
    n = len(payload)
    if not 1 <= n <= MAX_PDU:
        raise TransportError(f"LIN-TP PDU must be 1..{MAX_PDU} bytes, got {n}")

    def pad(frame: bytes) -> bytes:
        return frame + bytes([padding]) * (DIAG_FRAME_LEN - len(frame))

    if n <= SF_MAX:
        return [pad(bytes([nad, n]) + payload)]
    frames = [bytes([nad, 0x10 | (n >> 8), n & 0xFF]) + payload[:FF_DATA]]
    sn = 1
    for offset in range(FF_DATA, n, CF_DATA):
        frames.append(pad(bytes([nad, 0x20 | sn]) + payload[offset : offset + CF_DATA]))
        sn = (sn + 1) & 0x0F
    return frames


class Reassembler:
    """Rebuilds one PDU from SF or FF + CFs. The caller filters frames by NAD first."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._buf: bytearray | None = None
        self._length = 0
        self._sn = 0

    @property
    def in_progress(self) -> bool:
        return self._buf is not None

    def feed(self, frame: bytes) -> bytes | None:
        """Return the PDU once complete. Raises TransportError on a sequence error."""
        kind, low = frame[1] >> 4, frame[1] & 0x0F
        if kind == 0x0:
            if not 1 <= low <= SF_MAX:
                return None  # invalid SF length: frame ignored
            if self.in_progress:
                log.warning("LIN-TP: single frame interrupts a segmented message")
                self.reset()
            return bytes(frame[2 : 2 + low])
        if kind == 0x1:
            length = low << 8 | frame[2]
            if length <= SF_MAX:
                return None  # would fit a SF: invalid FF, ignored
            if self.in_progress:
                log.warning("LIN-TP: first frame interrupts a segmented message")
            self._buf = bytearray(frame[3 : 3 + FF_DATA])
            self._length, self._sn = length, 1
            return None
        if kind == 0x2:
            if self._buf is None:
                return None  # unexpected CF
            if low != self._sn:
                expected = self._sn
                self.reset()
                raise TransportError(f"LIN-TP: consecutive frame with sequence number {low}, expected {expected}")
            self._buf += frame[2 : 2 + min(CF_DATA, self._length - len(self._buf))]
            self._sn = (self._sn + 1) & 0x0F
            if len(self._buf) >= self._length:
                pdu = bytes(self._buf)
                self.reset()
                return pdu
            return None
        return None  # unknown PCI type


# -- transport -----------------------------------------------------------------

# Transport-level parameters a sequence step may override.
_PARAMS = ("n_cs_ms", "poll_interval_ms")

# Extra time allowed for the master to report a slave response (driver / USB latency).
_POLL_MARGIN_S = 0.05


class LinTpTransport(Transport):
    """UDS over LIN. A pre-built `master` is opened and closed together with the transport."""

    name = "lin"
    functional_responses = False

    def __init__(self, config: LinTpConfig, master: LinMaster | None = None):
        config.validate()
        self.config = config
        self._external_master = master
        self.master: LinMaster | None = None
        self._lock = threading.RLock()  # keeps the frames of one PDU together

    # -- lifecycle -------------------------------------------------------
    def open(self) -> None:
        c = self.config
        master = self._external_master or create_lin_master(c.backend, c.channel, c.baudrate, c.app_name)
        master.open()
        self.master = master

    def close(self) -> None:
        if self.master is not None:
            self.master.close()
            self.master = None

    def _require_master(self) -> LinMaster:
        if self.master is None:
            raise TransportError("transport is not open")
        return self.master

    # -- data ------------------------------------------------------------
    def send(self, payload: bytes, functional: bool = False) -> None:
        master = self._require_master()
        c = self.config
        payload = bytes(payload)
        if functional and len(payload) > SF_MAX:
            raise TransportError(
                f"functional request of {len(payload)} bytes does not fit in a single frame (max {SF_MAX})"
            )
        frames = segment(c.functional_nad if functional else c.nad, payload, c.padding)
        gap = float(c.n_cs_ms) / 1000
        with self._lock:
            try:
                for index, frame in enumerate(frames):
                    if index and gap:
                        time.sleep(gap)
                    master.send_master_request(frame)
            except HardwareError as exc:
                raise TransportError(f"LIN send failed: {exc}") from exc

    def _poll(self, master: LinMaster) -> bytes | None:
        with self._lock:
            try:
                return master.poll_slave_response(frame_time_max(self.config.baudrate) + _POLL_MARGIN_S)
            except HardwareError as exc:
                raise TransportError(f"LIN receive failed: {exc}") from exc

    def recv(self, timeout: float) -> bytes | None:
        """Poll 0x3D until a complete PDU from our NAD arrives.

        `timeout` bounds the wait for the first frame; once a first frame has arrived the
        message is completed under N_Cr, like an ISO-TP stack finishing in the background.
        """
        master = self._require_master()
        c = self.config
        rx = Reassembler()
        deadline = time.monotonic() + timeout
        cf_deadline = 0.0
        while True:
            frame = self._poll(master)
            if frame is not None and frame[0] == c.nad:
                pdu = rx.feed(frame)  # TransportError on a wrong sequence number
                if pdu is not None:
                    return pdu
                if rx.in_progress:
                    cf_deadline = time.monotonic() + c.n_cr_ms / 1000
                    continue  # the next CF is usually ready right away
            elif frame is not None:
                log.debug("LIN-TP: ignoring frame from NAD 0x%02X", frame[0])
            now = time.monotonic()
            if rx.in_progress:
                if now >= cf_deadline:
                    raise TransportError(f"LIN-TP: no consecutive frame within N_Cr = {c.n_cr_ms} ms")
                limit = cf_deadline
            elif now >= deadline:
                return None
            else:
                limit = deadline
            time.sleep(max(0.0, min(float(c.poll_interval_ms) / 1000, limit - now)))

    # -- params ----------------------------------------------------------
    def set_params(self, **overrides) -> dict:
        unknown = set(overrides) - set(_PARAMS)
        if unknown:
            raise TransportError(f"unknown LIN transport parameter(s): {', '.join(sorted(unknown))}")
        for key, value in overrides.items():
            _check_ms(key, value)
        previous = {key: getattr(self.config, key) for key in overrides}
        for key, value in overrides.items():
            setattr(self.config, key, value)
        return previous

    @property
    def max_payload(self) -> int:
        return MAX_PDU

    def describe(self) -> dict:
        c = self.config
        return {
            "type": "lin",
            "backend": c.backend,
            "channel": c.channel,
            "baudrate": c.baudrate,
            "nad": hex(c.nad),
            "functional_nad": hex(c.functional_nad),
            "n_cs_ms": c.n_cs_ms,
            "poll_interval_ms": c.poll_interval_ms,
            "n_cr_ms": c.n_cr_ms,
        }
