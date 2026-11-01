"""Serves a SimulatedEcu over ISO-TP on a python-can bus (normally `virtual`)."""

from __future__ import annotations

import queue
import threading
import time

import can
import isotp

from .ecu import SimulatedEcu


class CanEcuServer:
    def __init__(
        self,
        ecu: SimulatedEcu,
        *,
        channel: str = "sim0",
        interface: str = "virtual",
        rx_id: int = 0x7E0,
        tx_id: int = 0x7E8,
        func_id: int | None = 0x7DF,
        extended_id: bool = False,
        fd: bool = False,
        tx_dl: int = 8,
        padding: int | None = 0xCC,
        stmin: int = 0,
        block_size: int = 0,
    ):
        self.ecu = ecu
        self.func_id = func_id
        self.extended_id = extended_id
        kwargs = {"receive_own_messages": False} if interface == "virtual" else {}
        self.bus = can.Bus(interface=interface, channel=channel, **kwargs)
        self._functional: queue.Queue[bytes] = queue.Queue()
        self.notifier = can.Notifier(self.bus, [self._on_frame], timeout=0.1)
        mode = isotp.AddressingMode.Normal_29bits if extended_id else isotp.AddressingMode.Normal_11bits
        self.stack = isotp.NotifierBasedCanStack(
            self.bus,
            self.notifier,
            address=isotp.Address(mode, txid=tx_id, rxid=rx_id),
            params={
                "stmin": stmin,
                "blocksize": block_size,
                "tx_padding": padding,
                "tx_data_length": tx_dl,
                "tx_data_min_length": 8 if padding is not None else None,
                "can_fd": fd,
                "max_frame_size": 0xFFFF,
            },
        )
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sim-can-ecu", daemon=True)

    def _on_frame(self, msg: can.Message) -> None:
        wake_id = self.ecu.cfg.wakeup_id
        if self.ecu.cfg.sleep_timeout_ms and (wake_id is None or msg.arbitration_id == wake_id):
            self.ecu.wake()
        if self.func_id is None or msg.arbitration_id != self.func_id or msg.is_extended_id != self.extended_id:
            return
        data = bytes(msg.data)
        if not data or data[0] >> 4 != 0:  # single frames only
            return
        length = data[0] & 0x0F
        payload = data[1 : 1 + length] if length else data[2 : 2 + data[1]]
        self._functional.put(payload)

    def start(self) -> CanEcuServer:
        self.stack.start()
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self.stack.stop()
        self.notifier.stop()
        self.bus.shutdown()

    def _run(self) -> None:
        while not self._stop.is_set():
            req = self.stack.recv(block=True, timeout=0.01)
            if req is not None:
                self._respond(bytes(req), functional=False)
            try:
                while True:
                    self._respond(self._functional.get_nowait(), functional=True)
            except queue.Empty:
                pass

    def _respond(self, req: bytes, functional: bool) -> None:
        for delay, resp in self.ecu.handle(req, functional):
            if delay:
                time.sleep(delay)
            self.stack.send(resp)

    def __enter__(self) -> CanEcuServer:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
