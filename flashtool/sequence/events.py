"""Events emitted while a job runs (picklable, sent from job processes over IPC)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Event:
    job: str


@dataclass
class JobStarted(Event):
    target: str
    info: dict = field(default_factory=dict)


@dataclass
class StepStarted(Event):
    path: str
    kind: str
    label: str


@dataclass
class StepFinished(Event):
    path: str
    status: str  # ok | failed | skipped | warning
    duration_s: float
    error: str | None = None


@dataclass
class Progress(Event):
    block: str
    block_done: int
    block_total: int
    done: int
    total: int
    bytes_per_s: float


@dataclass
class LogEvent(Event):
    level: str  # info | warning | error
    message: str


@dataclass
class Traffic(Event):
    t: float
    direction: str
    data: bytes
    note: str
    functional: bool


@dataclass
class JobFinished(Event):
    success: bool
    duration_s: float
    error: str | None = None
    report: str | None = None


Emit = Callable[[Event], None]
