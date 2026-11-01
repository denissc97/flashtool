"""Starts flash jobs as separate processes and collects their events (docs/ARCHITECTURE.md §4.8).

One process per bus group; targets on the same CAN channel share a process. A
CredentialBroker process is started when a target uses certificates.
"""

from __future__ import annotations

import multiprocessing as mp
import queue as queue_mod
from collections.abc import Iterator

from ..security.broker import start_broker
from ..sequence.events import Event, JobFinished
from .job import _process_entry
from .project import load_project, plan_groups


class JobManager:
    def __init__(self) -> None:
        self._ctx = mp.get_context("spawn")
        self.events = self._ctx.Queue()
        self._procs: list[tuple[mp.Process, object, list[str]]] = []
        self._finished: set[str] = set()
        self._started: set[str] = set()
        self._broker_manager = None
        self._broker = None

    def start(self, project_path: str, target_ids: list[str]) -> list[list[str]]:
        """Start the targets; returns the bus groups (one process each)."""
        busy = [t for t in target_ids if t in self.running]
        if busy:
            raise RuntimeError(f"already running: {', '.join(busy)}")
        project = load_project(project_path)
        groups = plan_groups(project, target_ids)
        if self._broker is None and any(project.target(t).certificates.provider != "none" for t in target_ids):
            self._broker_manager, self._broker = start_broker()
        for ids in groups:
            cancel = self._ctx.Event()
            proc = self._ctx.Process(
                target=_process_entry, args=(project_path, ids, self.events, cancel, self._broker),
                name=f"flashjob-{'+'.join(ids)}", daemon=True,
            )
            proc.start()
            self._procs.append((proc, cancel, ids))
            self._started.update(ids)
            self._finished.difference_update(ids)
        return groups

    def cancel(self, target_id: str | None = None) -> None:
        """Cancel a target (its whole bus group) or everything."""
        for _, cancel, ids in self._procs:
            if target_id is None or target_id in ids:
                cancel.set()

    @property
    def running(self) -> list[str]:
        return sorted(self._started - self._finished)

    def poll(self, timeout: float = 0.1) -> Iterator[Event]:
        """Yield queued events; also reports jobs whose process died without a JobFinished."""
        try:
            ev = self.events.get(timeout=timeout) if timeout > 0 else self.events.get_nowait()
            while True:
                if isinstance(ev, JobFinished):
                    self._finished.add(ev.job)
                yield ev
                ev = self.events.get_nowait()
        except queue_mod.Empty:
            pass
        for proc, _, ids in self._procs:
            if not proc.is_alive() and proc.exitcode not in (None, 0):
                for tid in ids:
                    if tid not in self._finished:
                        self._finished.add(tid)
                        yield JobFinished(tid, False, 0.0, f"job process exited with code {proc.exitcode}")

    def wait_all(self) -> Iterator[Event]:
        while self.running:
            yield from self.poll(0.1)

    def credential_usage(self) -> list[dict]:
        return self._broker.usage() if self._broker is not None else []

    def shutdown(self) -> None:
        self.cancel()
        for proc, _, _ in self._procs:
            proc.join(timeout=5)
            if proc.is_alive():
                proc.terminate()
        if self._broker_manager is not None:
            self._broker_manager.shutdown()
            self._broker_manager = self._broker = None
