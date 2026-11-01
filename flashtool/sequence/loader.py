"""Template loading with inheritance (`extends`) and explicit patch operations."""

from __future__ import annotations

import copy
import os
from typing import Any

import yaml

BUILTIN_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")

STEP_KINDS = (
    "request", "service", "session", "security", "authenticate", "download", "foreach", "if", "tester_present",
    "delay_ms", "wait_ecu", "reset", "read_did", "write_did", "log",
)
COMMON_KEYS = {
    "id", "name", "functional", "timeout_ms", "allow_nrc", "expect_response", "expect_data",
    "on_fail", "retries", "store", "when", "transport", "group",
}
EXTRA_KEYS = {
    "foreach": {"in", "steps"},
    "if": {"steps", "else"},
    "reset": {"wait_ms"},
    "download": {"max_block_length"},
    "service": {"params"},
}
ON_FAIL = ("abort", "continue", "retry")


class SequenceError(Exception):
    pass


def step_kind(step: dict) -> str:
    kinds = [k for k in STEP_KINDS if k in step]
    if len(kinds) != 1:
        found = ", ".join(kinds) or "none"
        raise SequenceError(f"step {step.get('id', '?')}: exactly one step type expected, found {found}")
    return kinds[0]


def child_lists(step: dict) -> list[list]:
    return [step[k] for k in ("steps", "else") if isinstance(step.get(k), list)]


def _resolve_path(ref: str, base_dir: str) -> str:
    candidates = []
    if os.path.isabs(ref):
        candidates.append(ref)
    else:
        candidates.append(os.path.join(base_dir, ref))
        candidates.append(os.path.join(BUILTIN_DIR, ref))
    for c in list(candidates):
        if not c.endswith((".yaml", ".yml")):
            candidates.append(c + ".yaml")
    for c in candidates:
        if os.path.isfile(c):
            return os.path.abspath(c)
    raise SequenceError(f"sequence template '{ref}' not found (looked in {base_dir} and built-in templates)")


def _catalog_refs(value: Any, base_dir: str) -> list:
    """Template `catalog:` entries with paths made absolute (relative to the template)."""
    refs = value if isinstance(value, list) else ([value] if value else [])
    out = []
    for ref in refs:
        if isinstance(ref, dict):
            ref = dict(ref)
            if ref.get("file") and not os.path.isabs(ref["file"]):
                ref["file"] = os.path.join(base_dir, ref["file"])
        elif not os.path.isabs(str(ref)):
            ref = os.path.join(base_dir, str(ref))
        out.append(ref)
    return out


def template_path(ref: str, base_dir: str = ".") -> str:
    """Absolute path of a template reference (project-relative file or built-in name)."""
    return _resolve_path(ref, base_dir)


def is_builtin(path: str) -> bool:
    return os.path.dirname(os.path.abspath(path)) == os.path.abspath(BUILTIN_DIR)


def load_sequence(ref: str, base_dir: str = ".", _chain: tuple[str, ...] = ()) -> dict:
    """Load a template and resolve its `extends` chain into one flat sequence."""
    path = _resolve_path(ref, base_dir)
    if path in _chain:
        raise SequenceError(f"circular extends: {' -> '.join(_chain + (path,))}")
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise SequenceError(f"{path}: template must be a mapping")

    if "extends" in data:
        seq = load_sequence(data["extends"], os.path.dirname(path), _chain + (path,))
        seq["name"] = data.get("name", seq["name"])
        seq["description"] = data.get("description", seq.get("description", ""))
        seq["variables"].update(data.get("variables") or {})
        if "steps" in data:
            seq["steps"] = copy.deepcopy(data["steps"])
        if "finally" in data:
            seq["finally"] = copy.deepcopy(data["finally"])
        for op in data.get("patch") or []:
            _apply_patch(seq, op, path)
        seq["sources"].append(path)
        seq["catalogs"] += _catalog_refs(data.get("catalog"), os.path.dirname(path))
    else:
        seq = {
            "name": data.get("name", os.path.basename(path)),
            "description": data.get("description", ""),
            "variables": dict(data.get("variables") or {}),
            "steps": copy.deepcopy(data.get("steps") or []),
            "finally": copy.deepcopy(data.get("finally") or []),
            "sources": [path],
            "catalogs": _catalog_refs(data.get("catalog"), os.path.dirname(path)),
        }
        if "patch" in data:
            raise SequenceError(f"{path}: 'patch' needs 'extends'")
    validate_sequence(seq)
    return seq


def _find(lists: list[list], step_id: str) -> tuple[list, int] | None:
    for lst in lists:
        for i, step in enumerate(lst):
            if isinstance(step, dict) and step.get("id") == step_id:
                return lst, i
            for sub in child_lists(step) if isinstance(step, dict) else []:
                found = _find([sub], step_id)
                if found:
                    return found
    return None


def _as_steps(value: Any, where: str) -> list:
    if isinstance(value, dict):
        return [copy.deepcopy(value)]
    if isinstance(value, list):
        return copy.deepcopy(value)
    raise SequenceError(f"{where}: expected a step or a list of steps")


def _deep_merge(dst: dict, src: dict) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = copy.deepcopy(v)


def _apply_patch(seq: dict, op: dict, path: str) -> None:
    if not isinstance(op, dict):
        raise SequenceError(f"{path}: patch entries must be mappings")
    lists = [seq["steps"], seq["finally"]]
    kinds = [k for k in ("remove", "replace", "insert_after", "insert_before", "update", "append") if k in op]
    if len(kinds) != 1:
        raise SequenceError(f"{path}: patch entry needs exactly one operation, got {op}")
    kind = kinds[0]
    if kind == "append":
        seq["steps"].extend(_as_steps(op["append"], f"{path}: append"))
        return
    target = op[kind]
    found = _find(lists, target)
    if found is None:
        raise SequenceError(f"{path}: patch {kind}: no step with id '{target}'")
    lst, i = found
    where = f"{path}: patch {kind} {target}"
    if kind == "remove":
        del lst[i]
    elif kind == "replace":
        lst[i : i + 1] = _as_steps(op.get("with"), where)
    elif kind == "insert_after":
        lst[i + 1 : i + 1] = _as_steps(op.get("steps"), where)
    elif kind == "insert_before":
        lst[i:i] = _as_steps(op.get("steps"), where)
    elif kind == "update":
        changes = op.get("with")
        if not isinstance(changes, dict):
            raise SequenceError(f"{where}: 'with' must be a mapping")
        _deep_merge(lst[i], changes)


def validate_sequence(seq: dict) -> None:
    seen: set[str] = set()

    def walk(steps: list, where: str) -> None:
        if not isinstance(steps, list):
            raise SequenceError(f"{where}: steps must be a list")
        for step in steps:
            if not isinstance(step, dict):
                raise SequenceError(f"{where}: each step must be a mapping, got {step!r}")
            kind = step_kind(step)
            sid = step.get("id")
            if sid is not None:
                if sid in seen:
                    raise SequenceError(f"duplicate step id '{sid}'")
                seen.add(sid)
            allowed = COMMON_KEYS | {kind} | EXTRA_KEYS.get(kind, set())
            unknown = set(step) - allowed
            if unknown:
                raise SequenceError(f"step {sid or kind}: unknown key(s) {', '.join(sorted(unknown))}")
            if step.get("group", "once") != "once":
                raise SequenceError(f"step {sid or kind}: group must be 'once'")
            if step.get("on_fail", "abort") not in ON_FAIL:
                raise SequenceError(f"step {sid or kind}: on_fail must be one of {ON_FAIL}")
            if kind == "foreach" and ("in" not in step or "steps" not in step):
                raise SequenceError(f"step {sid or kind}: foreach needs 'in' and 'steps'")
            if kind == "if" and "steps" not in step:
                raise SequenceError(f"step {sid or kind}: if needs 'steps'")
            for sub in child_lists(step):
                walk(sub, sid or kind)

    walk(seq["steps"], "steps")
    walk(seq["finally"], "finally")


def _fmt(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return str(value).lower()
    if isinstance(value, int):
        return f"0x{value:02X}"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {_fmt(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_fmt(v) for v in value) + "]"
    return str(value)


def describe_step(step: dict) -> str:
    """One-line definition of a step (integers in hex), for display."""
    return ", ".join(f"{k}: {_fmt(v)}" for k, v in step.items() if k not in ("id", "steps", "else"))


def flatten(steps: list, depth: int = 0) -> list[tuple[int, dict]]:
    """(depth, step) pairs in document order, for display."""
    out: list[tuple[int, dict]] = []
    for step in steps:
        out.append((depth, step))
        for sub in child_lists(step):
            out.extend(flatten(sub, depth + 1))
    return out
