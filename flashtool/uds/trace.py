"""Timestamped record of every UDS request/response."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, TextIO


@dataclass
class TraceEntry:
    t: float  # seconds since trace start
    direction: str  # "TX" | "RX" | "--"
    data: bytes
    note: str = ""
    functional: bool = False

    def format(self) -> str:
        hexdata = self.data.hex(" ").upper()
        if len(hexdata) > 120:
            hexdata = f"{hexdata[:120]} … ({len(self.data)} bytes)"
        addr = " F" if self.functional else "  "
        note = f"  ; {self.note}" if self.note else ""
        return f"{self.t:10.4f} {self.direction}{addr} {hexdata}{note}"


class TraceSink:
    def __init__(self, file: TextIO | None = None, listener: Callable[[TraceEntry], None] | None = None):
        self._file = file
        self._listener = listener
        self._t0 = time.monotonic()
        self._lock = threading.Lock()

    def record(self, direction: str, data: bytes, note: str = "", functional: bool = False) -> None:
        entry = TraceEntry(time.monotonic() - self._t0, direction, bytes(data), note, functional)
        with self._lock:
            if self._file is not None:
                self._file.write(entry.format() + "\n")
            if self._listener is not None:
                self._listener(entry)

    def note(self, text: str) -> None:
        self.record("--", b"", text)
