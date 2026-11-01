"""Periodic Tester Present (3E 80) on its own thread."""

from __future__ import annotations

import threading

from .client import UdsClient


class TesterPresent:
    def __init__(self, client: UdsClient, period_s: float = 2.0, functional: bool = True):
        self.client = client
        self.period_s = period_s
        self.functional = functional
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="tester-present", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self.period_s):
            try:
                self.client.tester_present_nowait(self.functional)
            except Exception:
                # A failing tester present must not kill the job; the next request will report the error.
                pass
