"""ISO 15765-2 transport over CAN / CAN FD (python-can + can-isotp)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import can
import isotp

from ..hal.backends import CanHardware, create_can_bus
from .base import Transport, TransportError

log = logging.getLogger(__name__)

FD_DLC_SIZES = (8, 12, 16, 20, 24, 32, 48, 64)


def cyclic_messages(entries: list, fd: bool) -> list[tuple[can.Message, float]]:
    """(message, period in s) for the `cyclic` configuration, e.g. network management keeping the bus awake."""
    out = []
    for e in entries:
        try:
            data = bytes.fromhex(str(e.get("data", "")).replace(" ", ""))
            period = float(e.get("period_ms", 100)) / 1000
            msg = can.Message(arbitration_id=int(e["id"]), data=data, is_extended_id=bool(e.get("extended_id", False)),
                              is_fd=fd and len(data) > 8)
        except (KeyError, ValueError, TypeError) as exc:
            raise TransportError(f"invalid cyclic message {e}: {exc}") from exc
        if period <= 0:
            raise TransportError(f"cyclic message 0x{int(e['id']):X}: period_ms must be > 0")
        out.append((msg, period))
    return out


def _check_stmin(value: int) -> int:
    """STmin as the raw ISO 15765-2 byte: 0x00-0x7F = 0-127 ms, 0xF1-0xF9 = 100-900 us."""
    if not (0 <= value <= 0x7F or 0xF1 <= value <= 0xF9):
        raise TransportError(f"invalid STmin 0x{value:02X} (allowed 0x00-0x7F ms, 0xF1-0xF9 = 100-900 us)")
    return value


@dataclass
class CanTpConfig:
    tx_id: int = 0x7E0
    rx_id: int = 0x7E8
    func_id: int | None = 0x7DF
    extended_id: bool = False
    bitrate: int = 500_000
    fd: bool = False
    data_bitrate: int | None = 2_000_000
    bitrate_switch: bool = True
    tx_dl: int = 8  # 8 for classic CAN, 8..64 for CAN FD
    padding: int | None = 0xCC
    stmin_rx: int = 0  # STmin we send in our flow control frames
    block_size: int = 0  # BS we send in our flow control frames
    stmin_tx_override_ms: float | None = None  # ignore the ECU's STmin (ISO deviation, opt-in)
    n_bs_ms: int = 1000
    n_cr_ms: int = 1000
    wftmax: int = 0
    max_frame_size: int = 4095
    bus_log: str | None = None  # .blf / .asc / .log file with raw CAN traffic
    cyclic: list = field(default_factory=list)  # [{id, data, period_ms, extended_id}] sent while connected
    hardware: CanHardware = field(default_factory=CanHardware)

    def validate(self) -> None:
        _check_stmin(self.stmin_rx)
        if not 0 <= self.block_size <= 0xFF:
            raise TransportError("block_size must be 0..255")
        if self.fd:
            if self.tx_dl not in FD_DLC_SIZES:
                raise TransportError(f"tx_dl must be one of {FD_DLC_SIZES} for CAN FD")
        elif self.tx_dl != 8:
            raise TransportError("tx_dl must be 8 for classic CAN (set fd: true for larger frames)")


# Transport-level parameters a sequence step may override (e.g. only during download).
_PARAM_MAP = {
    "stmin_rx": "stmin",
    "block_size": "blocksize",
    "stmin_tx_override_ms": "override_receiver_stmin",
}


class IsoTpCanTransport(Transport):
    name = "can"

    def __init__(self, config: CanTpConfig, bus: can.BusABC | None = None, notifier: can.Notifier | None = None):
        """`bus` + `notifier` given: share them with other ECUs on the same bus (owned by the caller)."""
        config.validate()
        self.config = config
        self._external_bus = bus
        self._external_notifier = notifier
        self.bus: can.BusABC | None = None
        self._notifier: can.Notifier | None = None
        self._stack: isotp.NotifierBasedCanStack | None = None
        self._bus_logger: can.Listener | None = None
        self._cyclic_tasks: list = []
        self.last_error: Exception | None = None

    # -- lifecycle -------------------------------------------------------
    def open(self) -> None:
        c = self.config
        self.bus = self._external_bus or create_can_bus(
            c.hardware, bitrate=c.bitrate, fd=c.fd, data_bitrate=c.data_bitrate
        )
        if c.bus_log:
            self._bus_logger = can.Logger(c.bus_log)
        if self._external_notifier is not None:
            self._notifier = self._external_notifier
            if self._bus_logger is not None:
                self._notifier.add_listener(self._bus_logger)
        else:
            self._notifier = can.Notifier(self.bus, [self._bus_logger] if self._bus_logger else [], timeout=0.1)
        mode = isotp.AddressingMode.Normal_29bits if c.extended_id else isotp.AddressingMode.Normal_11bits
        address = isotp.Address(mode, txid=c.tx_id, rxid=c.rx_id)
        params = {
            "stmin": c.stmin_rx,
            "blocksize": c.block_size,
            "override_receiver_stmin": (c.stmin_tx_override_ms / 1000.0) if c.stmin_tx_override_ms is not None else None,
            "tx_padding": c.padding,
            "tx_data_length": c.tx_dl,
            "tx_data_min_length": 8 if c.padding is not None else None,
            "can_fd": c.fd,
            "bitrate_switch": c.fd and c.bitrate_switch,
            "rx_flowcontrol_timeout": c.n_bs_ms,
            "rx_consecutive_frame_timeout": c.n_cr_ms,
            "wftmax": c.wftmax,
            "max_frame_size": c.max_frame_size,
            "blocking_send": True,
        }
        self._stack = isotp.NotifierBasedCanStack(
            self.bus, self._notifier, address=address, params=params, error_handler=self._on_error
        )
        self._stack.start()
        if self._external_bus is None:  # on a shared bus the owner of the bus sends them once
            self._cyclic_tasks = [self.bus.send_periodic(m, p) for m, p in cyclic_messages(c.cyclic, c.fd)]

    def close(self) -> None:
        for task in self._cyclic_tasks:
            task.stop()
        self._cyclic_tasks = []
        if self._stack is not None:
            self._stack.stop()
            self._stack = None
        if self._notifier is not None:
            if self._external_notifier is None:
                self._notifier.stop()  # also stops (and closes) the bus log listener
            elif self._bus_logger is not None:
                self._notifier.remove_listener(self._bus_logger)
                self._bus_logger.stop()
            self._notifier = None
        self._bus_logger = None
        if self.bus is not None and self._external_bus is None:
            self.bus.shutdown()
        self.bus = None

    def _on_error(self, error: Exception) -> None:
        self.last_error = error
        log.warning("ISO-TP: %s", error)

    # -- data ------------------------------------------------------------
    def send(self, payload: bytes, functional: bool = False) -> None:
        if self._stack is None:
            raise TransportError("transport is not open")
        if functional:
            self._send_functional(payload)
            return
        try:
            self._stack.send(payload, send_timeout=max(5.0, len(payload) / 1000))
        except isotp.BlockingSendFailure as exc:
            raise TransportError(f"ISO-TP send failed: {self.last_error or exc}") from exc

    def _send_functional(self, payload: bytes) -> None:
        """Functional requests are single frames (ISO 15765-2), sent directly on the bus."""
        c = self.config
        if c.func_id is None:
            raise TransportError("no functional CAN id configured (func_id)")
        if len(payload) <= 7:
            frame = bytes([len(payload)]) + payload
        elif c.fd and len(payload) <= c.tx_dl - 2:
            frame = bytes([0x00, len(payload)]) + payload
        else:
            raise TransportError(f"functional request of {len(payload)} bytes does not fit in a single frame")
        if c.padding is not None:
            target = 8 if len(frame) <= 8 else next(n for n in FD_DLC_SIZES if n >= len(frame))
            frame = frame + bytes([c.padding]) * (target - len(frame))
        msg = can.Message(
            arbitration_id=c.func_id,
            is_extended_id=c.extended_id,
            data=frame,
            is_fd=c.fd and len(frame) > 8,
            bitrate_switch=c.fd and c.bitrate_switch and len(frame) > 8,
        )
        assert self.bus is not None
        self.bus.send(msg)

    def recv(self, timeout: float) -> bytes | None:
        if self._stack is None:
            raise TransportError("transport is not open")
        data = self._stack.recv(block=timeout > 0, timeout=timeout if timeout > 0 else None)
        return bytes(data) if data is not None else None

    # -- params ----------------------------------------------------------
    def set_params(self, **overrides) -> dict:
        unknown = set(overrides) - set(_PARAM_MAP)
        if unknown:
            raise TransportError(f"unknown CAN transport parameter(s): {', '.join(sorted(unknown))}")
        previous = {}
        for key, value in overrides.items():
            previous[key] = getattr(self.config, key)
            if key == "stmin_rx":
                _check_stmin(int(value))
            setattr(self.config, key, value)
            if self._stack is not None:
                iso_value = value
                if key == "stmin_tx_override_ms":
                    iso_value = None if value is None else float(value) / 1000.0
                self._stack.params.set(_PARAM_MAP[key], iso_value)
        if self._stack is not None:
            self._stack.load_params()
        return previous

    @property
    def max_payload(self) -> int:
        return self.config.max_frame_size

    def describe(self) -> dict:
        c = self.config
        d = {
            "type": "can-fd" if c.fd else "can",
            "backend": c.hardware.backend,
            "channel": c.hardware.channel,
            "bitrate": c.bitrate,
            "tx_id": hex(c.tx_id),
            "rx_id": hex(c.rx_id),
            "func_id": hex(c.func_id) if c.func_id is not None else None,
            "stmin_rx": c.stmin_rx,
            "block_size": c.block_size,
            "tx_dl": c.tx_dl,
        }
        if c.fd:
            d["data_bitrate"] = c.data_bitrate
        if c.stmin_tx_override_ms is not None:
            d["stmin_tx_override_ms"] = c.stmin_tx_override_ms
        return d
