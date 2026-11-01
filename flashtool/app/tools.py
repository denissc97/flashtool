"""Interactive ECU tools outside of a flash job: wake-up, keep-alive, identification read-out."""

from __future__ import annotations

import time
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Callable

from ..transport.base import Transport, TransportError
from ..transport.isotp_can import IsoTpCanTransport
from ..transport.lin_tp import LinTpTransport
from ..uds.client import NegativeResponse, UdsClient, UdsError, UdsTimeout
from ..uds.nrc import nrc_name
from ..uds.trace import TraceEntry, TraceSink
from .project import Project, Target

# ISO 14229-1 Annex C identification DIDs
STANDARD_IDENTIFICATION = [
    (0xF190, "VIN"),
    (0xF187, "Spare part number"),
    (0xF18A, "System supplier"),
    (0xF18C, "ECU serial number"),
    (0xF18B, "Manufacturing date"),
    (0xF188, "ECU software number"),
    (0xF189, "ECU software version"),
    (0xF191, "ECU hardware number"),
    (0xF192, "Supplier hardware number"),
    (0xF193, "Supplier hardware version"),
    (0xF194, "Supplier software number"),
    (0xF195, "Supplier software version"),
    (0xF197, "System name"),
    (0xF19E, "ODX file"),
    (0xF15A, "Fingerprint"),
    (0xF186, "Active diagnostic session"),
]
DID_NAMES = dict(STANDARD_IDENTIFICATION)


class ToolError(Exception):
    pass


@dataclass
class IdentRow:
    did: int
    name: str
    status: str  # ok | not supported | no response | error
    raw: bytes | None = None
    detail: str = ""

    @property
    def text(self) -> str:
        if self.raw is None:
            return ""
        stripped = self.raw.rstrip(b"\x00\xff ")
        if stripped and all(32 <= b < 127 for b in stripped):
            return stripped.decode("ascii")
        return self.raw.hex(" ").upper()


def wake_up(transport: Transport, cfg: dict) -> str:
    """Wake the ECU: CAN wake-up / NM frames, or the LIN wake-up signal. Returns what was done."""
    wait_ms = float(cfg.get("wait_ms", 100))
    if isinstance(transport, IsoTpCanTransport):
        frames = cfg.get("frames") or transport.config.cyclic
        if not frames:
            raise ToolError("no wake-up frame configured (wakeup.frames or can.cyclic)")
        sent = transport.send_frames(frames, int(cfg.get("repeat", 3)), float(cfg.get("interval_ms", 10)))
        what = f"{sent} wake-up frame(s) " + ", ".join(f"0x{int(f['id']):X}" for f in frames)
    elif isinstance(transport, LinTpTransport):
        transport.wake_up()
        what = "LIN wake-up signal"
    else:
        raise ToolError("DoIP: waking the ECU needs the activation line of the Ethernet interface (hardware)")
    time.sleep(wait_ms / 1000)
    return f"Sent {what}, waited {wait_ms:.0f} ms"


class EcuSession:
    """A connection to one ECU for interactive work (one at a time per ECU)."""

    def __init__(self, project: Project, target: Target, trace: Callable[[TraceEntry], None] | None = None):
        self.project, self.target = project, target
        self._trace = trace
        self._stack: ExitStack | None = None
        self.transport: Transport | None = None
        self.client: UdsClient | None = None

    @property
    def connected(self) -> bool:
        return self.client is not None

    def open(self) -> str:
        from .job import _make_transport, create_sim  # same transport + simulator set-up as a flash job

        stack = ExitStack()
        try:
            transport = _make_transport(self.project, self.target, stack, self.project.output_dir,
                                        create_sim(self.project, self.target), None)
            if isinstance(transport, IsoTpCanTransport):
                transport.autostart_cyclic = False  # keep-alive is started on demand
            transport.open()
            stack.callback(transport.close)
        except Exception:
            stack.close()
            raise
        self._stack, self.transport = stack, transport
        self.client = UdsClient(transport, self.target.uds, TraceSink(None, self._trace))
        return f"Connected to {self.target.name} ({transport.describe().get('type')})"

    def close(self) -> None:
        if self._stack is not None:
            self._stack.close()
        self._stack = self.transport = self.client = None

    def _require(self) -> UdsClient:
        if self.client is None:
            raise ToolError("not connected")
        return self.client

    # -- network ----------------------------------------------------------------------
    def wake_up(self) -> str:
        self._require()
        return wake_up(self.transport, self.target.wakeup)

    def keep_alive(self, on: bool) -> list[str]:
        self._require()
        if not isinstance(self.transport, IsoTpCanTransport):
            raise ToolError("keep-alive frames are sent on CAN (LIN keeps slaves awake with its schedule)")
        if not on:
            self.transport.stop_cyclic()
            return []
        if not self.transport.config.cyclic:
            raise ToolError("no keep-alive frame configured (ECU settings → Cyclic messages)")
        return self.transport.start_cyclic()

    @property
    def keep_alive_running(self) -> bool:
        return isinstance(self.transport, IsoTpCanTransport) and self.transport.cyclic_running

    # -- identification ---------------------------------------------------------------
    def read_did(self, did: int) -> IdentRow:
        client = self._require()
        name = DID_NAMES.get(did, f"DID {did:04X}")
        try:
            return IdentRow(did, name, "ok", client.read_data_by_identifier(did))
        except NegativeResponse as exc:
            status = "not supported" if exc.nrc in (0x31, 0x11, 0x7F) else "error"
            return IdentRow(did, name, status, None, f"NRC 0x{exc.nrc:02X} {nrc_name(exc.nrc)}")
        except UdsTimeout:
            return IdentRow(did, name, "no response", None, "no response")
        except (UdsError, TransportError) as exc:
            return IdentRow(did, name, "error", None, str(exc))

    def read_identification(self, dids: list[int] | None = None) -> list[IdentRow]:
        dids = dids or self.target.identification or [d for d, _ in STANDARD_IDENTIFICATION]
        rows: list[IdentRow] = []
        silent = 0
        for did in dids:
            row = self.read_did(did)
            rows.append(row)
            silent = silent + 1 if row.status == "no response" else 0
            if silent == 2 and len(rows) == 2:
                raise ToolError("the ECU does not answer – is it awake? Try Wake up / Keep-alive")
        return rows
