"""Project packages (`.ftpack`): a project and every file it needs in one zip.

The FlashTool counterpart of vFlash's .vflashpack / .vflashsealed. A pack holds:

* the project file (at the root of the zip),
* every file the project references: firmware (incl. PDX / VBF / ODX-F data files),
  sequence templates that are not built-in with their `extends` chain, `catalog:` files and
  `script:` actions, seed & key plugins / libraries, certificates, simulator trust anchors,
* `manifest.json`: tool version, creation time, project name, sha256 + size of every file.

Files inside the project folder keep their relative path; files outside it go under
`external/` (keeping their layout relative to each other) and the references to them are
rewritten, so the project works when the pack is unpacked into an empty folder.

Private keys (`certificates.key`) are left out unless explicitly requested; the manifest
says where to put them. A *sealed* pack is marked read-only: `is_sealed()` / `check_editable()`
tell editors to refuse changes, unpacked files are made read-only, and verification fails
on any changed, added or removed file. Sealing is an integrity lock, not a signature: there
is no key infrastructure, so anyone with FlashTool can build a new pack. Sign the .ftpack
with an external tool when authenticity matters.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import posixpath
import re
import stat
import tempfile
import zipfile
from dataclasses import dataclass, field
from typing import Any

from .. import __version__
from ..firmware.loaders import FirmwareError, load_firmware
from ..sequence.loader import SequenceError, is_builtin, template_path
from .project import ProjectError, load_project
from .project_io import dump_yaml, load_yaml

PACK_EXT = ".ftpack"
MANIFEST = "manifest.json"
FORMAT = "flashtool-pack"
FORMAT_VERSION = 1
EXTERNAL_DIR = "external"

PRIVATE_KEY_NOTE = ("private key not included (export with include_private_keys / --include-private-keys "
                    "to include it): copy it to this path, relative to the project folder, before flashing")


class PackageError(Exception):
    pass


# -- helpers ----------------------------------------------------------------------------------------
def _norm(path: str) -> str:
    return os.path.normpath(os.path.abspath(path))


def _inside(path: str, folder: str) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(folder)]) == os.path.normcase(folder)
    except ValueError:  # different drives
        return False


def _drive_name(drive: str) -> str:
    """'C:' -> 'C', '\\\\server\\share' -> 'server_share' (Windows drives of external files)."""
    return re.sub(r"[^A-Za-z0-9]+", "_", drive).strip("_") or "root"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _seal(manifest: dict) -> str:
    body = {k: v for k, v in manifest.items() if k != "seal"}
    return _sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())


def _safe_member(name: str) -> bool:
    if not name or name.startswith("/") or "\\" in name or ":" in name:
        return False
    parts = name.split("/")
    return all(p not in ("", ".", "..") for p in parts)


# -- collecting the files of a project ----------------------------------------------------------------
@dataclass
class _File:
    src: str  # absolute source path
    kind: str
    arc: str = ""  # path inside the pack
    content: bytes | None = None  # rewritten document; None = copy the source
    targets: set[str] = field(default_factory=set)
    owner: str | None = None  # firmware-data: the ODX-F file that references it


@dataclass
class _Ref:
    """A path written in a YAML document (project or template) that may need rewriting."""

    doc: str  # absolute path of the document
    container: Any  # dict or list holding the value
    key: Any
    target: str  # absolute path of the referenced file
    yaml_ext: bool = False  # template refs may omit '.yaml'


class _Collector:
    def __init__(self, project_path: str):
        self.project_path = project_path
        self.base = os.path.dirname(project_path)
        self.files: dict[str, _File] = {}
        self.refs: list[_Ref] = []
        self.docs: dict[str, Any] = {}  # abs path -> raw YAML data (rewritten in place)
        self.notes: list[str] = []
        self.project_content: bytes | None = None  # rewritten project file; None = copy it
        self.parents: dict[str, str] = {}  # template -> the (non built-in) template it extends

    def add(self, path: str, kind: str, target_id: str, owner: str | None = None) -> str:
        path = _norm(path)
        if path == self.project_path:
            raise PackageError(f"target '{target_id}' references the project file itself as {kind}")
        if kind != "private_key" and not os.path.isfile(path):
            raise PackageError(f"target '{target_id}': {kind} file not found: {path}")
        entry = self.files.get(path)
        if entry is None:
            entry = self.files[path] = _File(path, kind, owner=owner)
        elif kind == "private_key":  # a file used as a private key anywhere is treated as one
            entry.kind = kind
        entry.targets.add(target_id)
        return path

    def ref(self, doc: str, container: Any, key: Any, base: str, kind: str, target_id: str,
            resolved: str | None = None) -> str | None:
        """`resolved`: the file a template reference resolves to (its '.yaml' may be omitted)."""
        value = container[key]
        if not isinstance(value, str) or not value:
            return None
        path = resolved or (value if os.path.isabs(value) else os.path.join(base, value))
        path = self.add(path, kind, target_id)
        self.refs.append(_Ref(doc, container, key, path, resolved is not None))
        return path

    # -- project ---------------------------------------------------------------------------------
    def collect(self) -> None:
        try:
            project = load_project(self.project_path)
        except ProjectError as exc:
            raise PackageError(str(exc)) from exc
        self.project = project
        raw = load_yaml(self.project_path) or {}
        self.docs[self.project_path] = raw
        for raw_t, target in zip(raw.get("targets") or [], project.targets):
            self._target(raw_t, target)

    def _target(self, raw: dict, target) -> None:
        tid, doc, base = target.id, self.project_path, self.base
        for j, entry in enumerate(raw.get("firmware") or []):
            container, key = (raw["firmware"], j) if isinstance(entry, str) else (entry, "file")
            fw = self.ref(doc, container, key, base, "firmware", tid)
            if fw is None:
                continue
            try:  # ODX-F files can read data files next to them
                sources = load_firmware([target.firmware[j]], base).sources
            except FirmwareError as exc:
                self.notes.append(f"target '{tid}': {os.path.basename(fw)} could not be loaded ({exc}); "
                                  "files it references were not packed")
                continue
            for src in sources[1:]:
                self.add(src.path, "firmware-data", tid, owner=fw)

        seq_ref = str(raw.get("sequence", "uds_standard"))
        try:
            seq_path = template_path(seq_ref, base)
        except SequenceError as exc:
            raise PackageError(f"target '{tid}': {exc}") from exc
        if not is_builtin(seq_path):
            if "sequence" in raw:
                self.ref(doc, raw, "sequence", base, "sequence", tid, resolved=seq_path)
            else:  # default name resolved to a file in the project folder
                self.add(seq_path, "sequence", tid)
            self._template(seq_path, tid, ())

        self._catalogs(doc, raw, "catalog", base, tid)
        sk = raw.get("seed_key")
        if isinstance(sk, dict) and sk.get("provider") in ("python", "library", "bridge") and sk.get("path"):
            self.ref(doc, sk, "path", base, "seed_key", tid)
            native = sk["provider"] == "library" or (
                sk["provider"] == "bridge" and (sk.get("extra") or {}).get("target", "library") == "library")
            if native:
                self.notes.append(f"target '{tid}': only the seed&key library itself is packed, "
                                  "not the libraries it depends on")
            if sk.get("bridge_python"):
                self.notes.append(f"target '{tid}': seed_key.bridge_python ({sk['bridge_python']}) is an "
                                  "interpreter on this machine and is not packed")
        certs = raw.get("certificates")
        if isinstance(certs, dict):
            if certs.get("provider") == "file":
                for key in ("cert", "key"):
                    if certs.get(key):
                        self.ref(doc, certs, key, base, "private_key" if key == "key" else "certificate", tid)
                for i in range(len(certs.get("chain") or [])):
                    self.ref(doc, certs["chain"], i, base, "certificate", tid)
            elif certs.get("provider") == "python" and certs.get("path"):
                self.ref(doc, certs, "path", base, "certificate_plugin", tid)
        sim = raw.get("simulator")
        if isinstance(sim, dict) and sim.get("auth_trust_anchor"):
            self.ref(doc, sim, "auth_trust_anchor", base, "trust_anchor", tid)

    def _catalogs(self, doc: str, holder: dict, key: str, base: str, tid: str) -> None:
        value = holder.get(key)
        if not value:
            return
        if isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    self.ref(doc, item, "file", base, "catalog", tid)
                else:
                    self.ref(doc, value, i, base, "catalog", tid)
        elif isinstance(value, dict):
            self.ref(doc, value, "file", base, "catalog", tid)
        else:
            self.ref(doc, holder, key, base, "catalog", tid)

    # -- templates -------------------------------------------------------------------------------
    def _template(self, path: str, tid: str, chain: tuple[str, ...]) -> None:
        if path in chain:
            raise PackageError(f"circular extends: {' -> '.join(chain + (path,))}")
        if path in self.docs:  # already collected for another target
            for r in self.refs:
                if r.doc == path:
                    self.files[r.target].targets.add(tid)
            if path in self.parents:
                self._template(self.parents[path], tid, chain + (path,))
            return
        folder = os.path.dirname(path)
        try:
            raw = load_yaml(path)
        except Exception as exc:
            raise PackageError(f"cannot read sequence template {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise PackageError(f"{path}: template must be a mapping")
        self.docs[path] = raw
        self._catalogs(path, raw, "catalog", folder, tid)
        for section in ("steps", "finally", "patch", "block_sequences"):
            self._scripts(path, raw.get(section), folder, tid)
        if raw.get("extends"):
            try:
                parent = template_path(str(raw["extends"]), folder)
            except SequenceError as exc:
                raise PackageError(f"{path}: {exc}") from exc
            if not is_builtin(parent):
                self.ref(path, raw, "extends", folder, "sequence", tid, resolved=parent)
                self.parents[path] = parent
                self._template(parent, tid, chain + (path,))

    def _scripts(self, doc: str, value: Any, folder: str, tid: str) -> None:
        """`script:` custom actions (paths relative to the template)."""
        if isinstance(value, list):
            for v in value:
                self._scripts(doc, v, folder, tid)
        elif isinstance(value, dict):
            if isinstance(value.get("script"), str):
                path = value["script"] if os.path.isabs(value["script"]) else os.path.join(folder, value["script"])
                if os.path.isfile(path):
                    self.ref(doc, value, "script", folder, "script", tid)
                else:
                    self.notes.append(f"{os.path.basename(doc)}: script '{value['script']}' not found, not packed")
            for v in value.values():
                if isinstance(v, (list, dict)):
                    self._scripts(doc, v, folder, tid)

    # -- layout ------------------------------------------------------------------------------------
    def layout(self) -> str:
        """Assign pack paths; returns the pack path of the project file."""
        project_arc = os.path.basename(self.project_path)
        internal = {p: f for p, f in self.files.items() if _inside(p, self.base)}
        external = {p: f for p, f in self.files.items() if p not in internal}
        for p, f in internal.items():
            f.arc = os.path.relpath(p, self.base).replace(os.sep, "/")
        taken = {f.arc.lower() for f in internal.values()} | {project_arc.lower(), MANIFEST}
        prefix, n = EXTERNAL_DIR, 1
        while any(a == prefix or a.startswith(prefix + "/") for a in taken):
            n += 1
            prefix = f"{EXTERNAL_DIR}_{n}"
        by_drive: dict[str, list[str]] = {}
        for p in external:
            by_drive.setdefault(os.path.splitdrive(p)[0], []).append(p)
        for drive, paths in by_drive.items():
            common = os.path.commonpath([os.path.dirname(p) for p in paths])
            head = prefix if len(by_drive) == 1 else f"{prefix}/{_drive_name(drive)}"
            for p in paths:
                external[p].arc = f"{head}/{os.path.relpath(p, common).replace(os.sep, '/')}"
        seen: dict[str, str] = {}
        for f in self.files.values():
            if f.arc.lower() in seen or f.arc.lower() in (project_arc.lower(), MANIFEST):
                raise PackageError(f"two files would be stored as '{f.arc}' in the pack ({f.src})")
            seen[f.arc.lower()] = f.src
        for f in self.files.values():  # ODX-F data files are found relative to the ODX-F file
            if f.owner is not None:
                owner = self.files[f.owner]
                before = os.path.relpath(f.src, os.path.dirname(owner.src)).replace(os.sep, "/")
                after = posixpath.relpath(f.arc, posixpath.dirname(owner.arc) or ".")
                if before != after:
                    raise PackageError(f"{f.src} is read by {owner.src} through a relative path that cannot be "
                                       "kept in the pack; move both into the project folder")
        self.arcs = {self.project_path: project_arc, **{p: f.arc for p, f in self.files.items()}}
        return project_arc

    def rewrite(self) -> int:
        """Point references at the pack paths; returns the number of rewritten references."""
        changed: set[str] = set()
        count = 0
        for r in self.refs:
            doc_dir = posixpath.dirname(self.arcs[r.doc])
            target = self.arcs[r.target]
            value = str(r.container[r.key])
            if not os.path.isabs(value):
                where = posixpath.normpath(posixpath.join(doc_dir, value.replace("\\", "/")))
                if where == target or (r.yaml_ext and where + ".yaml" == target):
                    continue
            r.container[r.key] = posixpath.relpath(target, doc_dir or ".")
            changed.add(r.doc)
            count += 1
        for doc in changed:
            title = "project" if doc == self.project_path else "sequence"
            text = f"# FlashTool {title} - paths rewritten by `flashtool pack`\n" + dump_yaml(self.docs[doc])
            if doc == self.project_path:
                self.project_content = text.encode("utf-8")
            else:
                self.files[doc].content = text.encode("utf-8")
        return count


# -- export ---------------------------------------------------------------------------------------
def export_pack(project_path: str, out_path: str, sealed: bool = False, include_private_keys: bool = False) -> str:
    """Write `out_path` (.ftpack) with the project and every file it references. Returns its path."""
    project_path = _norm(project_path)
    if not os.path.isfile(project_path):
        raise PackageError(f"project not found: {project_path}")
    if is_sealed(project_path):
        if not sealed:
            raise PackageError("the project comes from a sealed pack: it can only be exported sealed")
        found = _pack_root(project_path)
        problems = verify_pack(found[0]) if found else []
        if problems:  # never re-seal a modified sealed project
            raise PackageError("the sealed project was modified:\n  " + "\n  ".join(problems))
    c = _Collector(project_path)
    c.collect()
    project_arc = c.layout()
    c.rewrite()

    excluded, entries = [], []
    blobs: list[tuple[str, bytes]] = []
    project_bytes = c.project_content
    if project_bytes is None:
        with open(project_path, "rb") as fh:
            project_bytes = fh.read()
    blobs.append((project_arc, project_bytes))
    entries.append({"path": project_arc, "kind": "project"})
    for f in sorted(c.files.values(), key=lambda f: f.arc):
        if f.kind == "private_key" and not include_private_keys:
            excluded.append({"path": f.arc, "kind": f.kind, "targets": sorted(f.targets), "note": PRIVATE_KEY_NOTE})
            continue
        if f.content is not None:
            data = f.content
        else:
            if not os.path.isfile(f.src):
                raise PackageError(f"{f.kind} file not found: {f.src}")
            with open(f.src, "rb") as fh:
                data = fh.read()
        blobs.append((f.arc, data))
        entries.append({"path": f.arc, "kind": f.kind, "targets": sorted(f.targets)})
    for entry, (_, data) in zip(entries, blobs):
        entry.update(sha256=_sha256(data), size=len(data))

    ignore = []
    for out in (c.project.output_dir, c.project.audit_log):
        if out and _inside(_norm(out), c.base):
            ignore.append(os.path.relpath(_norm(out), c.base).replace(os.sep, "/"))
        elif out:
            c.notes.append(f"output path outside the project folder kept as is: {out}")
    manifest: dict = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "tool": f"FlashTool {__version__}",
        "tool_version": __version__,
        "created": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "project": {"name": c.project.name, "file": project_arc},
        "sealed": bool(sealed),
        "private_keys_included": bool(include_private_keys),
        "files": entries,
        "excluded": excluded,
        "outputs": ignore,  # written by flash jobs in an unpacked folder; not part of the pack
        "notes": c.notes + [f"{e['path']}: {PRIVATE_KEY_NOTE}" for e in excluded],
    }
    if sealed:
        manifest["seal"] = _seal(manifest)

    out_path = _norm(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=PACK_EXT, prefix=".pack-", dir=os.path.dirname(out_path))
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr(MANIFEST, json.dumps(manifest, indent=1, ensure_ascii=False))
            for arc, data in blobs:
                z.writestr(arc, data)
        os.replace(tmp, out_path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return out_path


# -- reading ----------------------------------------------------------------------------------------
def read_manifest(pack_path: str) -> dict:
    """The manifest of a .ftpack file or of an unpacked folder (or a file inside it)."""
    if os.path.isfile(pack_path) and zipfile.is_zipfile(pack_path):
        try:
            with zipfile.ZipFile(pack_path) as z:
                return _parse_manifest(z.read(MANIFEST))
        except KeyError as exc:
            raise PackageError(f"{pack_path}: not a FlashTool pack (no {MANIFEST})") from exc
        except zipfile.BadZipFile as exc:
            raise PackageError(f"{pack_path}: {exc}") from exc
    found = _pack_root(pack_path)
    if found is None:
        raise PackageError(f"{pack_path}: not a FlashTool pack or unpacked pack folder")
    return found[1]


def _parse_manifest(raw: bytes) -> dict:
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackageError(f"invalid {MANIFEST}: {exc}") from exc
    if not (isinstance(manifest, dict) and manifest.get("format") == FORMAT
            and isinstance(manifest.get("files"), list)):
        raise PackageError(f"{MANIFEST} is not a FlashTool pack manifest")
    if manifest.get("format_version", 0) > FORMAT_VERSION:
        raise PackageError(f"pack format {manifest.get('format_version')} is newer than this FlashTool supports")
    return manifest


def _pack_root(path: str) -> tuple[str, dict] | None:
    """(folder, manifest) of the unpacked pack that contains `path` (a folder or a file in it)."""
    path = _norm(path)
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    while True:
        candidate = os.path.join(folder, MANIFEST)
        if os.path.isfile(candidate):
            try:
                with open(candidate, "rb") as f:
                    manifest = _parse_manifest(f.read())
            except (OSError, PackageError):
                manifest = None
            if manifest is not None:
                rel = os.path.relpath(path, folder).replace(os.sep, "/")
                listed = [str(e.get("path")) for e in manifest["files"]]
                if rel in (".", MANIFEST) or rel in listed or any(p.startswith(rel + "/") for p in listed):
                    return folder, manifest
        parent = os.path.dirname(folder)
        if parent == folder:
            return None
        folder = parent


def is_sealed(path: str) -> bool:
    """True for a sealed .ftpack, an unpacked sealed folder or any packed file inside one."""
    try:
        return bool(read_manifest(path).get("sealed"))
    except PackageError:
        return False


def check_editable(path: str) -> None:
    """Raises PackageError when `path` belongs to a sealed pack. Editors call this before saving."""
    if is_sealed(path):
        raise PackageError(f"{path} belongs to a sealed pack and cannot be modified")


# -- verification ---------------------------------------------------------------------------------
def _check_manifest(manifest: dict) -> list[str]:
    problems = []
    if (manifest.get("sealed") or "seal" in manifest) and manifest.get("seal") != _seal(manifest):
        problems.append(f"{MANIFEST} was modified (seal broken)")
    paths = [e.get("path") for e in manifest["files"]]
    for p in paths:
        if not isinstance(p, str) or not _safe_member(p) or p == MANIFEST:
            problems.append(f"unsafe path in {MANIFEST}: {p!r}")
    if len(set(paths)) != len(paths):
        problems.append(f"duplicate paths in {MANIFEST}")
    project_file = (manifest.get("project") or {}).get("file")
    if project_file not in paths:
        problems.append(f"{MANIFEST} does not list the project file")
    return problems


def _check_data(entry: dict, data: bytes | None) -> str | None:
    if data is None:
        return f"missing file: {entry['path']}"
    if len(data) != entry.get("size") or _sha256(data) != entry.get("sha256"):
        return f"changed file: {entry['path']}"
    return None


def verify_pack(pack_path: str) -> list[str]:
    """Problems found (empty list = OK). Works on a .ftpack file and on an unpacked folder."""
    if os.path.isdir(pack_path):
        return _verify_folder(pack_path)
    if not os.path.isfile(pack_path):
        return [f"not found: {pack_path}"]
    if not zipfile.is_zipfile(pack_path):
        found = _pack_root(pack_path)
        return _verify_folder(found[0]) if found else [f"{pack_path}: not a FlashTool pack"]
    try:
        with zipfile.ZipFile(pack_path) as z:
            names = [i.filename for i in z.infolist() if not i.is_dir()]
            if MANIFEST not in names:
                return [f"not a FlashTool pack (no {MANIFEST})"]
            try:
                manifest = _parse_manifest(z.read(MANIFEST))
            except PackageError as exc:
                return [str(exc)]
            problems = _check_manifest(manifest)
            if len(set(names)) != len(names):
                problems.append("the zip contains the same path twice")
            listed = {e.get("path") for e in manifest["files"]}
            for name in names:
                if not _safe_member(name):
                    problems.append(f"unsafe path in the zip: {name!r}")
                elif name != MANIFEST and name not in listed:
                    problems.append(f"added file: {name}")
            for entry in manifest["files"]:
                if not isinstance(entry.get("path"), str) or not _safe_member(entry["path"]):
                    continue
                data = z.read(entry["path"]) if entry["path"] in names else None
                problem = _check_data(entry, data)
                if problem:
                    problems.append(problem)
            return problems
    except (zipfile.BadZipFile, OSError, zipfile.LargeZipFile) as exc:
        return [f"{pack_path}: {exc}"]


def _verify_folder(folder: str) -> list[str]:
    path = os.path.join(folder, MANIFEST)
    if not os.path.isfile(path):
        return [f"{folder}: no {MANIFEST} (not an unpacked FlashTool pack)"]
    try:
        with open(path, "rb") as f:
            manifest = _parse_manifest(f.read())
    except (OSError, PackageError) as exc:
        return [str(exc)]
    problems = _check_manifest(manifest)
    listed = set()
    for entry in manifest["files"]:
        if not isinstance(entry.get("path"), str) or not _safe_member(entry["path"]):
            continue
        listed.add(entry["path"])
        file = os.path.join(folder, *entry["path"].split("/"))
        data = None
        if os.path.isfile(file):
            with open(file, "rb") as f:
                data = f.read()
        problem = _check_data(entry, data)
        if problem:
            problems.append(problem)
    if manifest.get("sealed"):
        # Flash jobs write reports / audit logs, Python writes bytecode caches: those are not edits.
        outputs = [o.rstrip("/") for o in manifest.get("outputs") or []]
        excluded = {e.get("path") for e in manifest.get("excluded") or []}
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                rel = os.path.relpath(os.path.join(root, name), folder).replace(os.sep, "/")
                if rel == MANIFEST or rel in listed or rel in excluded:
                    continue
                if any(rel == o or rel.startswith(o + "/") for o in outputs):
                    continue
                problems.append(f"added file: {rel}")
    return problems


# -- import ---------------------------------------------------------------------------------------
def import_pack(pack_path: str, dest_dir: str) -> str:
    """Verify `pack_path` and unpack it into the new or empty `dest_dir`. Returns the project file path."""
    problems = verify_pack(pack_path)
    if problems:
        raise PackageError("pack verification failed:\n  " + "\n  ".join(problems))
    if not zipfile.is_zipfile(pack_path):
        raise PackageError(f"{pack_path}: not a .ftpack file")
    dest_dir = _norm(dest_dir)
    if os.path.exists(dest_dir) and (not os.path.isdir(dest_dir) or os.listdir(dest_dir)):
        raise PackageError(f"destination {dest_dir} is not an empty folder")
    os.makedirs(dest_dir, exist_ok=True)
    with zipfile.ZipFile(pack_path) as z:
        raw_manifest = z.read(MANIFEST)
        manifest = _parse_manifest(raw_manifest)
        written = []
        for entry in manifest["files"]:
            data = z.read(entry["path"])
            problem = _check_data(entry, data)  # the zip could have changed since verify_pack
            if problem:
                raise PackageError(f"pack verification failed: {problem}")
            target = os.path.join(dest_dir, *entry["path"].split("/"))
            if not _inside(_norm(target), dest_dir):
                raise PackageError(f"unsafe path in the pack: {entry['path']}")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as f:
                f.write(data)
            written.append(target)
        manifest_path = os.path.join(dest_dir, MANIFEST)
        with open(manifest_path, "wb") as f:
            f.write(raw_manifest)
        written.append(manifest_path)
    if manifest.get("sealed"):
        for path in written:
            os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    return os.path.join(dest_dir, *manifest["project"]["file"].split("/"))


__all__ = [
    "PACK_EXT", "MANIFEST", "PackageError", "export_pack", "import_pack", "verify_pack", "read_manifest",
    "is_sealed", "check_editable",
]
