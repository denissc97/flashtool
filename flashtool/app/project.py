"""Flash project files (`*.ftproj`, YAML) - docs/ARCHITECTURE.md §4.9."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml

from ..firmware.loaders import FirmwareSpec
from ..hal.backends import CanHardware
from ..security.certs import CertConfig
from ..security.seedkey import SeedKeyConfig
from ..transport.doip import DoIpConfig
from ..transport.isotp_can import CanTpConfig
from ..transport.lin_tp import LinTpConfig
from ..uds.client import UdsTiming

TRANSPORTS = ("can", "canfd", "doip", "lin")


class ProjectError(Exception):
    pass


@dataclass
class Target:
    id: str
    name: str
    transport: str
    can: CanTpConfig | None
    doip: DoIpConfig | None
    lin: LinTpConfig | None
    uds: UdsTiming
    firmware: list[FirmwareSpec]
    sequence: str
    variables: dict
    seed_key: SeedKeyConfig
    certificates: CertConfig = field(default_factory=CertConfig)
    catalog: list = field(default_factory=list)  # paths or {file, layer} - diagnostic catalogs
    allow_custom_actions: bool = False  # sequences may run Python custom actions ('script' steps)
    wakeup: dict = field(default_factory=dict)  # {frames: [{id, data}], repeat, interval_ms, wait_ms, before_flash}
    identification: list = field(default_factory=list)  # DIDs for "Read identification" (default: ISO 14229 set)
    simulate: bool = False
    simulator: dict = field(default_factory=dict)


@dataclass
class Project:
    path: str
    name: str
    output_dir: str
    audit_log: str | None
    targets: list[Target]

    @property
    def base_dir(self) -> str:
        return os.path.dirname(os.path.abspath(self.path))

    def target(self, target_id: str) -> Target:
        for t in self.targets:
            if t.id == target_id:
                return t
        raise ProjectError(f"no target '{target_id}' in {self.path}")


def _build(cls, data: dict | None, where: str, **extra):
    data = dict(data or {})
    known = set(cls.__dataclass_fields__)
    unknown = set(data) - known
    if unknown:
        raise ProjectError(f"{where}: unknown option(s) {', '.join(sorted(unknown))}")
    data.update(extra)
    return cls(**data)


def _can_config(raw: dict, fd: bool, where: str) -> CanTpConfig:
    raw = dict(raw or {})
    hw_keys = {"backend", "channel", "app_name", "serial", "extra"}
    hw = _build(CanHardware, {k: raw.pop(k) for k in list(raw) if k in hw_keys}, f"{where}.can")
    raw.setdefault("fd", fd)
    if raw["fd"]:
        raw.setdefault("tx_dl", 64)
    return _build(CanTpConfig, raw, f"{where}.can", hardware=hw)


def _target(raw: dict, index: int) -> Target:
    if not isinstance(raw, dict):
        raise ProjectError(f"targets[{index}] must be a mapping")
    tid = str(raw.get("id") or f"target{index + 1}")
    where = f"target '{tid}'"
    known = {"id", "name", "transport", "can", "doip", "lin", "uds", "firmware", "sequence", "variables",
             "seed_key", "certificates", "catalog", "allow_custom_actions", "wakeup", "identification",
             "simulate", "simulator"}
    unknown = set(raw) - known
    if unknown:
        raise ProjectError(f"{where}: unknown option(s) {', '.join(sorted(unknown))}")
    transport = raw.get("transport", "can")
    if transport not in TRANSPORTS:
        raise ProjectError(f"{where}: transport must be one of {TRANSPORTS}")
    firmware = raw.get("firmware") or []  # may be empty while the project is edited; checked before flashing
    try:
        specs = [FirmwareSpec.from_dict(f if isinstance(f, dict) else {"file": f}) for f in firmware]
    except Exception as exc:
        raise ProjectError(f"{where}: {exc}") from exc
    return Target(
        id=tid,
        name=str(raw.get("name", tid)),
        transport=transport,
        can=_can_config(raw.get("can"), transport == "canfd", where) if transport in ("can", "canfd") else None,
        doip=_build(DoIpConfig, raw.get("doip"), f"{where}.doip") if transport == "doip" else None,
        lin=_build(LinTpConfig, raw.get("lin"), f"{where}.lin") if transport == "lin" else None,
        uds=_build(UdsTiming, raw.get("uds"), f"{where}.uds"),
        firmware=specs,
        sequence=str(raw.get("sequence", "uds_standard")),
        variables=dict(raw.get("variables") or {}),
        seed_key=_build(SeedKeyConfig, raw.get("seed_key"), f"{where}.seed_key"),
        certificates=_build(CertConfig, raw.get("certificates"), f"{where}.certificates"),
        catalog=raw["catalog"] if isinstance(raw.get("catalog"), list) else ([raw["catalog"]] if raw.get("catalog") else []),
        allow_custom_actions=bool(raw.get("allow_custom_actions", False)),
        wakeup=dict(raw.get("wakeup") or {}),
        identification=[int(d) for d in raw.get("identification") or []],
        simulate=bool(raw.get("simulate", False)),
        simulator=dict(raw.get("simulator") or {}),
    )


def bus_key(target: Target) -> tuple:
    """Targets with the same key share one physical bus and are flashed by one process."""
    if target.can is not None:
        return ("can", target.can.hardware.backend, str(target.can.hardware.channel))
    if target.lin is not None:
        return ("lin", target.lin.backend, str(target.lin.channel))
    return (target.transport, target.id)


def plan_groups(project: Project, target_ids: list[str]) -> list[list[str]]:
    groups: dict[tuple, list[str]] = {}
    for tid in target_ids:
        groups.setdefault(bus_key(project.target(tid)), []).append(tid)
    return list(groups.values())


def load_project(path: str) -> Project:
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except OSError as exc:
        raise ProjectError(f"cannot read project: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ProjectError(f"{path}: invalid YAML: {exc}") from exc
    targets_raw = data.get("targets")
    if targets_raw is None:
        targets_raw = []
    if not isinstance(targets_raw, list):
        raise ProjectError(f"{path}: 'targets' must be a list")
    targets = [_target(t, i) for i, t in enumerate(targets_raw)]
    ids = [t.id for t in targets]
    if len(set(ids)) != len(ids):
        raise ProjectError(f"{path}: duplicate target ids")
    base = os.path.dirname(os.path.abspath(path))
    out = data.get("output_dir", "out")
    audit = data.get("audit_log", os.path.join(out, "audit.jsonl"))
    return Project(
        path=os.path.abspath(path),
        name=str(data.get("name", os.path.splitext(os.path.basename(path))[0])),
        output_dir=out if os.path.isabs(out) else os.path.join(base, out),
        audit_log=None if audit is None else (audit if os.path.isabs(audit) else os.path.join(base, audit)),
        targets=targets,
    )
