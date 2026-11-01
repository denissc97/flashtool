"""Coordination of several ECUs flashed on the same bus (docs/ARCHITECTURE.md §4.8).

A step marked `group: once` (typically the functional 10 03 / 85 02 / 28 03 before
programming and 28 00 / 85 01 / 10 01 after it) runs once for the whole bus, after
every ECU still in the same phase has reached it. An ECU that aborts leaves the
phase, so the others never wait for it.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable


class GroupError(Exception):
    pass


class GroupSync:
    PHASES = ("main", "finally")

    def __init__(self, members: list[str], timeout_s: float = 600.0):
        self.members = list(members)
        self.timeout_s = timeout_s
        self._cond = threading.Condition()
        self._active = {phase: set(members) for phase in self.PHASES}
        self._arrived: dict[tuple[str, str], set[str]] = {}
        self._executor: dict[tuple[str, str], str] = {}
        self._outcome: dict[tuple[str, str], tuple[bool, Any]] = {}

    def leave(self, phase: str, member: str) -> None:
        with self._cond:
            self._active[phase].discard(member)
            self._cond.notify_all()

    def run_once(self, phase: str, step_id: str, member: str, fn: Callable[[], Any]) -> tuple[bool, Any]:
        """Returns (executed_here, result). Re-raises the executor's failure as GroupError for the others."""
        key = (phase, step_id)
        deadline = time.monotonic() + self.timeout_s
        with self._cond:
            self._arrived.setdefault(key, set()).add(member)
            self._cond.notify_all()
            while not self._active[phase] <= self._arrived[key]:
                if not self._cond.wait(timeout=max(0.0, deadline - time.monotonic())):
                    missing = ", ".join(sorted(self._active[phase] - self._arrived[key]))
                    raise GroupError(f"timeout waiting for {missing} at shared step '{step_id}'")
            executor = self._executor.setdefault(key, member)
        if executor == member:
            try:
                result = fn()
            except Exception as exc:
                with self._cond:
                    self._outcome[key] = (False, f"{type(exc).__name__}: {exc}")
                    self._cond.notify_all()
                raise
            with self._cond:
                self._outcome[key] = (True, result)
                self._cond.notify_all()
            return True, result
        with self._cond:
            while key not in self._outcome:
                if not self._cond.wait(timeout=max(0.0, deadline - time.monotonic())):
                    raise GroupError(f"timeout waiting for {executor} to run shared step '{step_id}'")
            ok, value = self._outcome[key]
        if not ok:
            raise GroupError(f"shared step '{step_id}' failed on {executor}: {value}")
        return False, value
