"""Read and write project and sequence files as plain data, for the visual editors.

The editors work on the raw YAML structure (dicts and lists) and save it back. Values
that engineers read in hex (CAN ids, addresses, DIDs, ...) stay in hex in the file.
"""

from __future__ import annotations

import copy
import os
import tempfile
from typing import Any

import yaml

from ..sequence.loader import BUILTIN_DIR, SequenceError, load_sequence
from .project import ProjectError, load_project


class HexInt(int):
    """An int written as 0x.. in YAML."""


HEX_KEYS = {
    # project
    "tx_id", "rx_id", "func_id", "padding", "stmin_rx", "nad", "functional_nad", "ecu_address", "tester_address",
    "functional_address", "activation_type", "base_address", "memory_address", "memory_size", "dfi", "alfid",
    "fill_gaps", "secret", "sid", "nrc", "security_level", "id", "wakeup_id", "identification",
    # sequence steps
    "session", "level", "did", "reset", "allow_nrc",
}


def _hex_representer(dumper: yaml.SafeDumper, value: HexInt) -> yaml.Node:
    width = 2 if value <= 0xFF else 4 if value <= 0xFFFF else 8
    return dumper.represent_scalar("tag:yaml.org,2002:int", f"0x{int(value):0{width}X}")


class _Dumper(yaml.SafeDumper):
    pass


_Dumper.add_representer(HexInt, _hex_representer)


def _hexify(value: Any, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {k: _hexify(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_hexify(v, key) for v in value]
    if key in HEX_KEYS and isinstance(value, int) and not isinstance(value, bool):
        return HexInt(value)
    return value


def dump_yaml(data: Any) -> str:
    return yaml.dump(_hexify(data), Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=120)


def load_yaml(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return _hexify(yaml.safe_load(f))


# -- projects ------------------------------------------------------------------
def new_project(name: str = "New project") -> dict:
    return {"name": name, "output_dir": "out", "targets": []}


def new_target(target_id: str, transport: str = "can") -> dict:
    t: dict = {"id": target_id, "name": target_id, "transport": transport, "simulate": True}
    if transport in ("can", "canfd"):
        t["can"] = {"backend": "virtual", "channel": f"sim-{target_id}", "bitrate": 500000,
                    "tx_id": HexInt(0x7E0), "rx_id": HexInt(0x7E8), "func_id": HexInt(0x7DF)}
        if transport == "canfd":
            t["can"].update(data_bitrate=2000000, tx_dl=64)
    elif transport == "lin":
        t["lin"] = {"backend": "virtual", "channel": f"sim-{target_id}", "baudrate": 19200, "nad": HexInt(0x01)}
    elif transport == "doip":
        t["doip"] = {"ecu_ip": "127.0.0.1", "ecu_address": HexInt(0x0010), "tester_address": HexInt(0x0E00)}
    t["firmware"] = []
    t["sequence"] = "uds_standard"
    return t


def validate_project_data(data: dict, path: str) -> None:
    """Raises ProjectError if `data` saved as `path` would not load (relative paths checked in place)."""
    folder = os.path.dirname(os.path.abspath(path))
    fd, probe = tempfile.mkstemp(suffix=".ftproj", prefix=".check-", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(dump_yaml(data))
        load_project(probe)
    finally:
        os.remove(probe)


def project_from_data(data: dict, path: str):
    """The Project that `data` would give once saved as `path` (raises ProjectError)."""
    folder = os.path.dirname(os.path.abspath(path))
    fd, probe = tempfile.mkstemp(suffix=".ftproj", prefix=".check-", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(dump_yaml(data))
        project = load_project(probe)
    finally:
        os.remove(probe)
    project.path = os.path.abspath(path)
    return project


def _check_editable(path: str) -> None:
    from .package import check_editable  # sealed packages are read-only

    check_editable(path)


def save_project(data: dict, path: str) -> None:
    _check_editable(path)
    validate_project_data(data, path)
    with open(path, "w", encoding="utf-8") as f:
        f.write("# FlashTool project - written by the FlashTool editor\n" + dump_yaml(data))


# -- sequences -----------------------------------------------------------------
def sequence_for_editing(path: str) -> tuple[dict, bool]:
    """(editable sequence, was_flattened). Templates with `extends` are resolved into one flat sequence."""
    path = os.path.abspath(path)
    raw = load_yaml(path) or {}
    if "extends" not in raw:
        raw.setdefault("name", os.path.splitext(os.path.basename(path))[0])
        raw.setdefault("variables", {})
        raw.setdefault("steps", [])
        raw.setdefault("finally", [])
        return raw, False
    seq = load_sequence(path, os.path.dirname(path))
    folder = os.path.dirname(path)
    flat = {
        "name": seq["name"],
        "description": seq.get("description", ""),
        "catalog": [_relative(c, folder) for c in seq.get("catalogs", [])],
        "variables": _hexify(seq["variables"]),
        "steps": _hexify(seq["steps"]),
        "block_sequences": {name: _hexify(steps) for name, steps in seq["block_sequences"].items()},
        "finally": _hexify(seq["finally"]),
    }
    if not flat["catalog"]:
        del flat["catalog"]
    return flat, True


def _relative(ref: Any, folder: str) -> Any:
    if isinstance(ref, dict):
        ref = dict(ref)
        ref["file"] = os.path.relpath(ref["file"], folder)
        return ref
    return os.path.relpath(str(ref), folder)


def validate_sequence_data(data: dict, path: str) -> dict:
    """Returns the resolved sequence; raises SequenceError (or yaml errors) when invalid."""
    folder = os.path.dirname(os.path.abspath(path))
    fd, probe = tempfile.mkstemp(suffix=".yaml", prefix=".check-", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(dump_yaml(data))
        return load_sequence(probe, folder)
    finally:
        os.remove(probe)


def save_sequence(data: dict, path: str) -> None:
    if os.path.dirname(os.path.abspath(path)) == os.path.abspath(BUILTIN_DIR):
        raise SequenceError("built-in templates are read-only; save the sequence in the project folder")
    _check_editable(path)
    validate_sequence_data(data, path)
    clean = {k: v for k, v in copy.deepcopy(data).items() if v not in (None, "", [], {}) or k in ("steps",)}
    with open(path, "w", encoding="utf-8") as f:
        f.write("# FlashTool sequence - written by the sequence editor\n" + dump_yaml(clean))


def list_templates(project_dir: str) -> list[str]:
    """Built-in template names and .yaml sequences in the project folder (relative names)."""
    names = sorted(os.path.splitext(f)[0] for f in os.listdir(BUILTIN_DIR) if f.endswith(".yaml"))
    local = []
    for f in sorted(os.listdir(project_dir)):
        if f.endswith((".yaml", ".yml")) and not f.startswith("."):
            try:
                with open(os.path.join(project_dir, f), encoding="utf-8") as fh:
                    head = yaml.safe_load(fh)
            except Exception:
                continue
            if isinstance(head, dict) and ("steps" in head or "extends" in head):
                local.append(f)
    return names + local


__all__ = [
    "HexInt", "dump_yaml", "load_yaml", "new_project", "new_target", "validate_project_data", "save_project",
    "sequence_for_editing", "validate_sequence_data", "save_sequence", "list_templates", "ProjectError",
    "SequenceError",
]
