"""Flash jobs. One process per bus group (D-05), one thread per ECU inside it.

ECUs on the same physical bus share one bus handle and coordinate their functional
steps through GroupSync (docs/ARCHITECTURE.md §4.8).
"""

from __future__ import annotations

import datetime
import os
import platform
import threading
import time
import traceback
from contextlib import ExitStack
from dataclasses import asdict

import can

from .. import __version__
from ..diagdb.catalog import CatalogError, DiagCatalog, load_catalog
from ..firmware.loaders import FirmwareError, load_firmware
from ..firmware.model import FirmwareImage
from ..firmware.validate import validate_image
from ..hal.backends import HardwareError, create_can_bus
from ..security.broker import BrokeredProvider
from ..security.certs import CertificateError, TargetCredentials, create_certificate_provider
from ..security.seedkey import SeedKeyError, create_seedkey_provider
from ..sequence.engine import CancelToken, Engine
from ..sequence.events import Emit, JobFinished, JobStarted, LogEvent, Traffic
from ..sequence.expr import ExprError, render_value
from ..sequence.group import GroupSync
from ..sequence.loader import SequenceError, all_steps, block_sequence_key, load_sequence
from ..sim.ecu import SimConfig, SimulatedEcu
from ..transport.base import Transport, TransportError
from ..transport.doip import DoIpTransport
from ..transport.isotp_can import IsoTpCanTransport, cyclic_messages
from ..transport.lin_tp import LinTpTransport
from ..uds.client import UdsClient
from ..uds.trace import TraceEntry, TraceSink
from . import audit
from .project import Project, ProjectError, Target, load_project
from .report import write_report

SETUP_ERRORS = (ProjectError, FirmwareError, SequenceError, SeedKeyError, CertificateError, ExprError,
                TransportError, HardwareError)


class JobSetupError(Exception):
    pass


def prepare(project: Project, target: Target) -> tuple[FirmwareImage, dict, list[str]]:
    """Load and check everything that does not need the ECU. Raises JobSetupError."""
    if not target.firmware:
        raise JobSetupError(f"{target.id}: no firmware files")
    try:
        image = load_firmware(target.firmware, project.base_dir)
    except FirmwareError as exc:
        raise JobSetupError(str(exc)) from exc
    issues = validate_image(image)
    errors = [str(i) for i in issues if i.severity == "error"]
    if errors:
        raise JobSetupError("firmware validation failed:\n  " + "\n  ".join(errors))
    warnings = [str(i) for i in issues if i.severity == "warning"]
    try:
        sequence = load_sequence(target.sequence, project.base_dir)
    except SequenceError as exc:
        raise JobSetupError(str(exc)) from exc
    sequence["variables"].update(target.variables)
    sequence["catalog"] = _load_catalogs(sequence, target, project.base_dir)
    warnings += _check_block_sequences(image, sequence)
    if target.can is not None and target.can.stmin_tx_override_ms is not None:
        warnings.append(
            f"stmin_tx_override_ms={target.can.stmin_tx_override_ms}: the ECU's STmin is ignored "
            "(deviation from ISO 15765-2)"
        )
    return image, sequence, warnings


def _check_block_sequences(image: FirmwareImage, sequence: dict) -> list[str]:
    """Every block must have a block sequence when the template programs blocks by type."""
    if not any("program_block" in s for s in all_steps(sequence)):
        defined = sorted(sequence.get("block_sequences") or {})
        return [f"block sequences {', '.join(defined)} are not used: the sequence has no 'Program block' step"] \
            if defined else []
    warnings = []
    for block in image.blocks:
        key = block_sequence_key(sequence, block.type)
        if key is None:
            raise JobSetupError(f"block {block.name} has type {block.type}, but the sequence has no block sequence "
                                f"for {block.type} and no default one")
        if not sequence["block_sequences"][key]:
            warnings.append(f"block {block.name} (type {block.type}): block sequence '{key}' is empty, "
                            "nothing is downloaded for it")
    return warnings


def _load_catalogs(sequence: dict, target: Target, base_dir: str) -> DiagCatalog | None:
    refs = list(sequence.get("catalogs") or []) + list(target.catalog)
    used = [s["service"] for s in all_steps(sequence) if "service" in s]
    if not refs:
        if used:
            raise JobSetupError(f"sequence uses named services ({', '.join(used)}) but no catalog is configured")
        return None
    catalog = DiagCatalog()
    try:
        for ref in refs:
            catalog.merge(load_catalog(ref, base_dir))
        for name in used:
            catalog.get(name)  # fail before connecting
    except CatalogError as exc:
        raise JobSetupError(str(exc)) from exc
    return catalog


class SharedCanBus:
    """One bus handle + notifier for every ECU of a group."""

    def __init__(self, targets: list[Target]):
        first = targets[0].can
        assert first is not None
        for t in targets[1:]:
            c = t.can
            if c is None or (c.bitrate, c.fd, c.data_bitrate if c.fd else None) != (
                first.bitrate, first.fd, first.data_bitrate if first.fd else None
            ):
                raise JobSetupError(f"targets on {first.hardware.backend}:{first.hardware.channel} "
                                    "must use the same bitrate / CAN FD settings")
        self.bus = create_can_bus(first.hardware, bitrate=first.bitrate, fd=first.fd, data_bitrate=first.data_bitrate)
        self.notifier = can.Notifier(self.bus, [], timeout=0.1)
        unique = {(int(e["id"]), str(e.get("data", "")), float(e.get("period_ms", 100))): e
                  for t in targets for e in (t.can.cyclic if t.can else [])}
        try:
            self.tasks = [self.bus.send_periodic(m, p) for m, p in cyclic_messages(list(unique.values()), first.fd)]
        except TransportError as exc:
            self.notifier.stop()
            self.bus.shutdown()
            raise JobSetupError(str(exc)) from exc

    def close(self) -> None:
        for task in self.tasks:
            task.stop()
        self.notifier.stop()
        self.bus.shutdown()


def _resolve(path: str | None, base_dir: str) -> str | None:
    if path is None or os.path.isabs(path):
        return path
    return os.path.join(base_dir, path)


def create_sim(project: Project, target: Target) -> SimulatedEcu | None:
    """The built-in simulated ECU of a target with `simulate: true`."""
    if not target.simulate:
        return None
    sim_cfg = dict(target.simulator)
    sim_cfg["auth_trust_anchor"] = _resolve(sim_cfg.get("auth_trust_anchor"), project.base_dir)
    return SimulatedEcu(SimConfig.from_dict(sim_cfg))


def _make_transport(project: Project, target: Target, stack: ExitStack, run_dir: str,
                    sim: SimulatedEcu | None, shared: SharedCanBus | None) -> Transport:
    if target.transport in ("can", "canfd"):
        cfg = target.can
        assert cfg is not None
        if cfg.bus_log and not os.path.isabs(cfg.bus_log):
            cfg.bus_log = os.path.join(run_dir, cfg.bus_log)
        if sim is not None:
            if cfg.hardware.backend != "virtual":
                raise JobSetupError("simulate: true needs can.backend: virtual")
            from ..sim.can_server import CanEcuServer

            stack.enter_context(CanEcuServer(
                sim, channel=str(cfg.hardware.channel), rx_id=cfg.tx_id, tx_id=cfg.rx_id, func_id=cfg.func_id,
                extended_id=cfg.extended_id, fd=cfg.fd, tx_dl=cfg.tx_dl, padding=cfg.padding,
            ))
        if shared is not None:
            return IsoTpCanTransport(cfg, bus=shared.bus, notifier=shared.notifier)
        return IsoTpCanTransport(cfg)
    if target.transport == "lin":
        lin = target.lin
        assert lin is not None
        if sim is not None:
            if lin.backend != "virtual":
                raise JobSetupError("simulate: true needs lin.backend: virtual")
            from ..sim.lin_server import LinEcuServer

            stack.enter_context(LinEcuServer(sim, channel=str(lin.channel), nad=lin.nad,
                                             functional_nad=lin.functional_nad, padding=lin.padding))
        return LinTpTransport(lin)
    cfg = target.doip
    assert cfg is not None
    if sim is not None:
        from ..sim.doip_server import DoIpEcuServer

        server = stack.enter_context(DoIpEcuServer(
            sim, host="127.0.0.1", port=0, ecu_address=cfg.ecu_address, functional_address=cfg.functional_address
        ))
        cfg.ecu_ip, cfg.tcp_port = "127.0.0.1", server.port
    return DoIpTransport(cfg)


def _credentials(project: Project, target: Target, broker) -> TargetCredentials | None:
    cfg = target.certificates
    if cfg.provider == "none":
        return None
    if broker is not None:
        broker.register(target.id, asdict(cfg), project.base_dir)
        provider = BrokeredProvider(broker, target.id)
    else:
        provider = create_certificate_provider(cfg, project.base_dir)
    return TargetCredentials(provider, cfg.ecu or target.id, cfg.role)


def run_target(project: Project, target: Target, emit: Emit, cancel=None, *, broker=None,
               group: GroupSync | None = None, shared: SharedCanBus | None = None) -> bool:
    """Run one ECU end to end. Every outcome is reported through `emit` and the report."""
    t0 = time.monotonic()
    started = datetime.datetime.now().astimezone()
    report: dict = {
        "tool": f"FlashTool {__version__} (Python {platform.python_version()}, {platform.system()})",
        "project": project.name, "target": target.id, "started": started.isoformat(timespec="seconds"),
        "success": False, "warnings": [],
    }
    if group is not None:
        report["bus_group"] = group.members
    stamp = started.strftime("%Y%m%d-%H%M%S")
    run_dir = os.path.join(project.output_dir, target.id, stamp)
    error: str | None = None
    success = False
    engine_ran = False
    try:
        os.makedirs(run_dir, exist_ok=True)
        image, sequence, warnings = prepare(project, target)
        report["warnings"] = warnings
        report["firmware"] = {
            "total_size": image.total_size,
            "sources": [asdict(s) for s in image.sources],
            "blocks": [{"name": b.name, "start": b.start, "end": b.end, "size": b.size,
                        "dfi": b.data_format_identifier, "alfid": b.address_and_length_format,
                        "crc32": b.crc32, "segments": len(b.segments)} for b in image.blocks],
        }
        report["sequence"] = {"name": sequence["name"], "sources": sequence["sources"],
                              "catalogs": sequence["catalog"].sources if sequence["catalog"] else [],
                              "variables": {k: _plain(v) for k, v in sequence["variables"].items()}}
        emit(JobStarted(target.id, target.name, {
            "run_dir": run_dir, "blocks": len(image.blocks), "total_size": image.total_size,
            "sequence": sequence["name"], "transport": target.transport, "simulate": target.simulate,
            "bus_group": group.members if group else None,
        }))
        for w in warnings:
            emit(LogEvent(target.id, "warning", w))

        with ExitStack() as stack, open(os.path.join(run_dir, "trace.txt"), "w", encoding="utf-8") as trace_file:
            seedkey = create_seedkey_provider(target.seed_key, project.base_dir)
            if seedkey is not None:
                stack.callback(seedkey.close)
                report["seed_key"] = seedkey.describe()
            credentials = _credentials(project, target, broker)
            if credentials is not None:
                report["certificates"] = {"provider": target.certificates.provider, "role": credentials.role,
                                          "ecu": credentials.ecu}
            sim = create_sim(project, target)
            transport = _make_transport(project, target, stack, run_dir, sim, shared)
            report["transport"] = transport.describe()

            def on_trace(entry: TraceEntry) -> None:
                emit(Traffic(target.id, entry.t, entry.direction, entry.data, entry.note, entry.functional))

            trace = TraceSink(trace_file, on_trace)
            trace.note(f"FlashTool {__version__} - {project.name} / {target.id} - {started.isoformat()}")
            transport.open()
            stack.callback(transport.close)
            if target.wakeup.get("before_flash"):
                from .tools import wake_up

                emit(LogEvent(target.id, "info", wake_up(transport, target.wakeup)))
            client = UdsClient(transport, target.uds, trace)
            variables = {k: render_value(v, sequence["variables"]) for k, v in sequence["variables"].items()}
            engine = Engine(job=target.id, client=client, transport=transport, image=image, variables=variables,
                            seedkey=seedkey, emit=emit, cancel=CancelToken(cancel), credentials=credentials,
                            group=group, catalog=sequence["catalog"], allow_scripts=target.allow_custom_actions)
            engine_ran = True
            success, error = engine.run(sequence)
            report["steps"] = [asdict(r) for r in engine.records]
            report["timing"] = {"p2_ms": client.p2_ms, "p2_star_ms": client.p2_star_ms}
    except (JobSetupError, *SETUP_ERRORS) as exc:
        success, error = False, str(exc)
    except Exception as exc:  # report unexpected failures instead of losing them
        success, error = False, f"internal error: {exc!r}"
        report["traceback"] = traceback.format_exc()
    finally:
        if group is not None and not engine_ran:  # never reached the engine: do not block the others
            group.leave("main", target.id)
            group.leave("finally", target.id)

    duration = time.monotonic() - t0
    report.update(success=success, error=error, duration_s=duration)
    report_path = write_report(run_dir, report)
    if project.audit_log:
        try:
            audit.append(project.audit_log, {
                "time": started.isoformat(timespec="seconds"), "project": project.name,
                "target": target.id, "success": success, "error": error,
                "files": [{"path": s["path"], "sha256": s["sha256"]} for s in report.get("firmware", {}).get("sources", [])],
                "certificates": report.get("certificates"), "report": report_path,
            })
        except Exception as exc:
            emit(LogEvent(target.id, "error", f"audit log not written: {exc}"))
    emit(JobFinished(target.id, success, duration, error, report_path))
    return success


def run_group(project_path: str, target_ids: list[str], emit: Emit, cancel=None, broker=None) -> dict[str, bool]:
    """Flash targets that share one bus: one thread per ECU, shared functional steps run once."""
    try:
        project = load_project(project_path)
        targets = [project.target(t) for t in target_ids]
    except ProjectError as exc:
        for tid in target_ids:
            emit(JobFinished(tid, False, 0.0, str(exc), None))
        return {tid: False for tid in target_ids}

    if len(targets) == 1:
        return {targets[0].id: run_target(project, targets[0], emit, cancel, broker=broker)}
    if all(t.lin is not None for t in targets):
        # One LIN master polls the slaves in turn: slaves on the same channel are flashed one after another.
        emit(LogEvent(targets[0].id, "info", f"LIN channel shared by {', '.join(t.id for t in targets)}: sequential"))
        return {t.id: run_target(project, t, emit, cancel, broker=broker) for t in targets}

    group = GroupSync([t.id for t in targets])
    shared = None
    try:
        if all(t.can is not None for t in targets):
            shared = SharedCanBus(targets)
    except (JobSetupError, HardwareError) as exc:
        for t in targets:
            emit(JobFinished(t.id, False, 0.0, str(exc), None))
        return {t.id: False for t in targets}
    emit(LogEvent(targets[0].id, "info", f"bus group: {', '.join(group.members)} flashed together"))

    results: dict[str, bool] = {}

    def worker(t: Target) -> None:
        results[t.id] = run_target(project, t, emit, cancel, broker=broker, group=group, shared=shared)

    threads = [threading.Thread(target=worker, args=(t,), name=f"ecu-{t.id}") for t in targets]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    if shared is not None:
        shared.close()
    return results


def run_job(project_path: str, target_id: str, emit: Emit, cancel=None, broker=None) -> bool:
    return run_group(project_path, [target_id], emit, cancel, broker)[target_id]


def _plain(v):
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).hex(" ").upper()
    if isinstance(v, (int, float, str, bool)) or v is None:
        return v
    return str(v)


# -- process mode -------------------------------------------------------------
def _process_entry(project_path: str, target_ids: list[str], queue, cancel, broker=None) -> None:
    run_group(project_path, target_ids, queue.put, cancel, broker)
