"""Serves a SimulatedEcu as a LIN slave node on a VirtualLinBus (ISO 17987-2 transport).

Requests are reassembled from master request frames (0x3C); responses are queued as
LIN-TP frames and handed out one per slave response header (0x3D). Replies that the
ECU delays (NRC 0x78 during erase, ...) only become available when their time is due.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque

from ..hal.lin import VirtualLinBus
from ..transport.base import TransportError
from ..transport.lin_tp import SF_MAX, Reassembler, segment
from .ecu import SimulatedEcu

log = logging.getLogger(__name__)


class LinEcuServer:
    """A LIN slave node with a UDS bootloader.

    Real slaves do not answer functionally addressed requests (several slaves would
    collide on 0x3D); they only process them. `respond_to_functional=True` answers anyway,
    for sequences written for CAN that expect a response to e.g. a functional 10 03.
    """

    def __init__(
        self,
        ecu: SimulatedEcu,
        *,
        channel: str = "sim0",
        bus: VirtualLinBus | None = None,
        nad: int = 0x01,
        functional_nad: int = 0x7E,
        padding: int = 0xFF,
        respond_to_functional: bool = False,
    ):
        self.ecu = ecu
        self.bus = bus or VirtualLinBus.get(channel)
        self.nad = nad
        self.functional_nad = functional_nad
        self.padding = padding
        self.respond_to_functional = respond_to_functional
        self._lock = threading.Lock()
        self._rx = Reassembler()
        self._requests: queue.Queue[tuple[int, bytes, bool]] = queue.Queue()
        self._scheduled: deque[tuple[float, bytes]] = deque()  # (due time, response PDU)
        self._frames: deque[bytes] = deque()  # response frames ready for 0x3D
        self._generation = 0  # bumped by every new physical request; older replies are dropped
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sim-lin-ecu", daemon=True)

    # -- LinSlave ----------------------------------------------------------
    def on_master_request(self, data: bytes) -> None:
        nad, kind = data[0], data[1] >> 4
        if nad == self.functional_nad:
            length = data[1] & 0x0F
            if kind == 0x0 and 1 <= length <= SF_MAX:  # functional requests are single frames only
                with self._lock:
                    self._requests.put((self._generation, bytes(data[2 : 2 + length]), True))
            return
        if nad != self.nad:
            return  # another node, or the go-to-sleep command (NAD 0x00)
        with self._lock:
            if kind in (0x0, 0x1):
                # A new request discards the response still pending for the previous one.
                self._generation += 1
                self._scheduled.clear()
                self._frames.clear()
            try:
                req = self._rx.feed(data)
            except TransportError as exc:
                log.warning("sim LIN: %s; request dropped", exc)
                return
            if req is not None:
                self._requests.put((self._generation, req, False))

    def on_wakeup(self) -> None:
        self.ecu.wake()

    def slave_response(self) -> bytes | None:
        with self._lock:
            now = time.monotonic()
            while self._scheduled and self._scheduled[0][0] <= now:
                _, resp = self._scheduled.popleft()
                self._frames.extend(segment(self.nad, resp, self.padding))
            return self._frames.popleft() if self._frames else None

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> LinEcuServer:
        self._thread.start()
        self.bus.attach(self)
        return self

    def stop(self) -> None:
        self.bus.detach(self)
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                generation, req, functional = self._requests.get(timeout=0.05)
            except queue.Empty:
                continue
            replies = self.ecu.handle(req, functional)
            if functional and not self.respond_to_functional:
                continue
            with self._lock:
                if generation != self._generation:
                    continue  # superseded by a newer physical request
                due = time.monotonic()
                for delay, resp in replies:
                    due += delay
                    self._scheduled.append((due, resp))

    def __enter__(self) -> LinEcuServer:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
