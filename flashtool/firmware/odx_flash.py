"""PDX / ODX-F firmware loading (docs/ARCHITECTURE.md §4.3).

The ODX-F model (ASAM MCD-2D 2.2, category FLASH) is parsed with `odxtools`. Done here
because odxtools does not cover it for flashing:

* Only the FLASH documents of a PDX are parsed: an unrelated ODX-D problem must not block
  flashing, and big diagnostic databases are slow. ECU-MEM-CONNECTORS are dropped before
  parsing because their LAYER-REFs point into ODX-D layers that are not loaded.
* EXTERN-FLASHDATA bytes are read directly from the PDX zip / the folder of the .odx-f:
  odxtools' `Flashdata.dataset` decodes data files as UTF-8 text and reads BINARY files as
  hex text, which breaks raw binary data files.
* INTEL-HEX / MOTOROLA-S / BINARY decoding uses bincopy, like the other loaders.

One LogicalBlock per DATABLOCK-REF of the selected SESSION (in SESSION order):

    DATABLOCK SHORT-NAME                       -> name
    SEGMENT SOURCE-START-ADDRESS + data        -> segments (address + TARGET-ADDR-OFFSET)
    ENCRYPT-COMPRESS-METHOD (SEGMENT/FLASHDATA)-> data_format_identifier
    UNCOMPRESSED-SIZE (compressed, 1 segment)  -> memory_size
    FLASHDATA SIZE-LENGTH / ADDRESS-LENGTH     -> address_and_length_format
    SECURITY FW-SIGNATURE                      -> signature
    TYPE, OWN-IDENTS, FW-CHECKSUM, SECURITY-METHOD, ... -> metadata

The tool never decompresses or decrypts: segment data is sent exactly as stored.
"""

from __future__ import annotations

import os
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree

import bincopy
from odxtools.database import Database
from odxtools.datablock import Datablock
from odxtools.dataformatselection import DataformatSelection
from odxtools.encryptcompressmethod import EncryptCompressMethod
from odxtools.externflashdata import ExternFlashdata
from odxtools.flashdata import Flashdata
from odxtools.internflashdata import InternFlashdata
from odxtools.negoffset import NegOffset
from odxtools.posoffset import PosOffset
from odxtools.security import Security
from odxtools.session import Session
from odxtools.validityfor import ValidityFor

from .loaders import FirmwareError
from .model import LogicalBlock, Segment

FILL_BYTE = 0xFF  # used for addresses inside a SEGMENT that the HEX / S-record data does not cover


@dataclass
class OdxSession:
    name: str  # SESSION short name
    ecu_mem: str  # short name of the ECU-MEM holding the session
    label: str  # name to use in `session:` (qualified "ECU-MEM/SESSION" only when needed)
    datablocks: list[str]


@dataclass
class OdxContent:
    session: OdxSession
    sessions: list[OdxSession]
    blocks: list[LogicalBlock]
    data_files: list[str] = field(default_factory=list)  # files read from disk besides `path`


def list_sessions(path: str) -> list[OdxSession]:
    try:
        with _Container(path) as container:
            return [s for s, _ in _sessions(container, _database(container))]
    except (zipfile.BadZipFile, OSError) as exc:
        raise FirmwareError(f"{os.path.basename(path)}: {exc}") from exc


def load_odx(path: str, session: str | None = None) -> OdxContent:
    """Read the blocks of one ODX-F SESSION from a .pdx archive or a standalone .odx-f file."""
    try:
        with _Container(path) as container:
            sessions = _sessions(container, _database(container))
            info, sess = _select(container.label, sessions, session)
            cache: dict[int, object] = {}
            blocks = [_block(container, info, sess, db, cache) for db in sess.datablocks]
            return OdxContent(session=info, sessions=[s for s, _ in sessions], blocks=blocks,
                              data_files=list(dict.fromkeys(container.disk_files)))
    except (zipfile.BadZipFile, OSError) as exc:
        raise FirmwareError(f"{os.path.basename(path)}: {exc}") from exc


# --- container: PDX zip or standalone ODX file -------------------------------------------------


class _Container:
    def __init__(self, path: str):
        self.path = path
        self.label = os.path.basename(path)
        self.dir = os.path.dirname(os.path.abspath(path))
        is_zip = zipfile.is_zipfile(path)
        if not is_zip and path.lower().endswith(".pdx"):
            raise FirmwareError(f"{self.label}: not a PDX (zip) archive")
        self.zip = zipfile.ZipFile(path) if is_zip else None
        self.disk_files: list[str] = []

    def __enter__(self) -> _Container:
        return self

    def __exit__(self, *exc) -> None:
        if self.zip is not None:
            self.zip.close()

    def flash_documents(self) -> list[tuple[str, ElementTree.Element]]:
        if self.zip is None:
            root = _parse_xml(self.label, self.path)
            return [(self.label, root)] if _category_tag(root) == "FLASH" else []
        docs = []
        for info in self.zip.infolist():
            name = info.filename
            suffix = posixpath.splitext(name)[1].lower()
            if info.is_dir() or not suffix.startswith(".odx"):
                continue
            with self.zip.open(info) as f:
                category = _peek_category(f)
            if category is None and suffix == ".odx-f":
                category = "FLASH"  # let the full parse report what is wrong with it
            if category == "FLASH":
                with self.zip.open(info) as f:
                    docs.append((name, _parse_xml(name, f)))
        return docs

    def read_datafile(self, name: str, latebound: bool, what: str) -> bytes:
        if self.zip is not None:
            member = self._zip_member(name)
            if member is not None:
                return self.zip.read(member)
        if self.zip is None or latebound:
            # Standalone .odx-f: data files live next to it. LATEBOUND-DATAFILE in a PDX: the
            # file is supplied after the PDX was built, also expected next to it.
            for candidate in (name, posixpath.basename(name.replace("\\", "/"))):
                disk = os.path.join(self.dir, *candidate.replace("\\", "/").split("/"))
                if os.path.isfile(disk):
                    self.disk_files.append(disk)
                    with open(disk, "rb") as f:
                        return f.read()
        where = "in the PDX" if self.zip is not None and not latebound else f"next to {self.label}"
        raise FirmwareError(f"{self.label}: {what}: DATAFILE '{name}' not found {where}")

    def _zip_member(self, name: str) -> str | None:
        want = posixpath.normpath(name.replace("\\", "/")).lstrip("/")
        names = [n for n in self.zip.namelist() if not n.endswith("/")]
        for match in (lambda n: n == want, lambda n: n.lower() == want.lower(),
                      lambda n: posixpath.basename(n).lower() == posixpath.basename(want).lower()):
            hits = [n for n in names if match(n)]
            if len(hits) == 1:
                return hits[0]
        return None


def _parse_xml(label: str, source) -> ElementTree.Element:
    try:
        return ElementTree.parse(source).getroot()
    except ElementTree.ParseError as exc:
        raise FirmwareError(f"{label}: invalid XML: {exc}") from exc


def _category_tag(root: ElementTree.Element) -> str | None:
    children = list(root)
    return children[0].tag if children else None


def _peek_category(stream) -> str | None:
    """Tag of the ODX category element (2nd element) without parsing the whole document."""
    try:
        for depth, (_, elem) in enumerate(ElementTree.iterparse(stream, events=("start",))):
            if depth == 1:
                return elem.tag
    except ElementTree.ParseError:
        pass
    return None


def _database(container: _Container) -> Database:
    docs = container.flash_documents()
    if not docs:
        raise FirmwareError(f"{container.label}: no ODX-F (FLASH) document found")
    db = Database(use_weakrefs=False)
    for name, root in docs:
        for flash in root.iterfind("FLASH"):
            for connectors in flash.findall("ECU-MEM-CONNECTORS"):
                flash.remove(connectors)
        try:
            db.add_odx_xml_tree(root)
        except Exception as exc:  # odxtools raises OdxError, KeyError, ValueError, ...
            raise FirmwareError(f"{container.label}: {name}: {exc}") from exc
    try:
        db.refresh()
    except Exception as exc:
        raise FirmwareError(f"{container.label}: ODX-F references cannot be resolved: {exc}") from exc
    return db


# --- sessions --------------------------------------------------------------------------------------


def _sessions(container: _Container, db: Database) -> list[tuple[OdxSession, Session]]:
    found = [(ecu_mem.short_name, s) for flash in db.flashs for ecu_mem in flash.ecu_mems
             for s in ecu_mem.mem.sessions]
    if not found:
        raise FirmwareError(f"{container.label}: the ODX-F contains no SESSION")
    names = [s.short_name for _, s in found]
    return [
        (OdxSession(name=s.short_name, ecu_mem=em,
                    label=s.short_name if names.count(s.short_name) == 1 else f"{em}/{s.short_name}",
                    datablocks=[d.short_name for d in s.datablocks]), s)
        for em, s in found
    ]


def _select(label: str, sessions: list[tuple[OdxSession, Session]],
            wanted: str | None) -> tuple[OdxSession, Session]:
    available = ", ".join(s.label for s, _ in sessions)
    if wanted is None:
        if len(sessions) == 1:
            return sessions[0]
        raise FirmwareError(f"{label} has {len(sessions)} ODX-F sessions, choose one with 'session': {available}")
    hits = [x for x in sessions if wanted in (x[0].name, x[0].label, f"{x[0].ecu_mem}/{x[0].name}")]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise FirmwareError(f"{label}: no ODX-F session '{wanted}' (available: {available})")
    raise FirmwareError(f"{label}: session '{wanted}' is ambiguous, use one of: {available}")


# --- datablock -> LogicalBlock ---------------------------------------------------------------------


def _block(container: _Container, info: OdxSession, session: Session, db: Datablock,
           cache: dict[int, object]) -> LogicalBlock:
    what = f"DATABLOCK {db.short_name}"
    fd = db.flashdata
    if fd is None:
        raise FirmwareError(f"{container.label}: {what} has no FLASHDATA-REF")
    fmt = fd.dataformat.selection
    if fmt not in (DataformatSelection.INTEL_HEX, DataformatSelection.MOTOROLA_S, DataformatSelection.BINARY):
        user = fd.dataformat.user_selection
        raise FirmwareError(f"{container.label}: {what}: DATAFORMAT {fmt.value}"
                            f"{f' ({user})' if user else ''} is not supported")
    label = f"{container.label}: {what}"

    methods = [seg.encrypt_compress_method or fd.encrypt_compress_method for seg in db.segments] \
        or [fd.encrypt_compress_method]
    dfis = {_dfi(m, label) for m in methods}
    if len(dfis) != 1:
        raise FirmwareError(f"{label}: segments use different ENCRYPT-COMPRESS-METHODs; "
                            "one block has a single dataFormatIdentifier")
    (dfi,) = dfis
    compressed = dfi != 0

    key = id(fd)
    if key not in cache:
        cache[key] = _decode(container, fd, fmt, what)
    payload = cache[key]

    fill = 0
    if fmt is DataformatSelection.BINARY:
        segs = _binary_segments(payload, db, compressed, label)
    else:
        segs, fill = _hex_segments(payload, db, compressed, label)
        if fill and compressed:
            raise FirmwareError(f"{label}: compressed/encrypted data has {fill} byte(s) of gaps "
                                "inside its SEGMENTS; filling them would corrupt the data")

    offset = _target_offset(db)
    if offset:
        segs = [Segment(s.address + offset, s.data) for s in segs]
        if any(s.address < 0 for s in segs):
            raise FirmwareError(f"{label}: TARGET-ADDR-OFFSET gives a negative address")

    memory_size = None
    if compressed and len(db.segments) == 1 and db.segments[0].uncompressed_size is not None:
        memory_size = db.segments[0].uncompressed_size

    meta: dict = {
        "source": container.path,
        "format": "odx",
        "session": info.name,
        "ecu_mem": info.ecu_mem,
        "datablock": db.short_name,
        "type": db.data_type,
        "logical_block_index": db.logical_block_index,
        "own_idents": {oi.short_name: _plain(oi.ident_value.value) for oi in db.own_idents},
        "flashdata": fd.short_name,
        "dataformat": fmt.value,
    }
    if isinstance(fd, ExternFlashdata):
        meta["datafile"] = fd.datafile.value.strip()
    if any(m is not None for m in methods):
        meta["encrypt_compress_method"] = next(m.value_raw.strip() for m in methods if m is not None)
    if any(s.uncompressed_size is not None for s in db.segments):
        meta["uncompressed_sizes"] = [s.uncompressed_size for s in db.segments]
    if offset:
        meta["target_addr_offset"] = offset
    if fill:
        meta["fill_bytes"] = fill

    signature = None
    if db.securities:
        primary = next((s for s in db.securities if s.fw_signature is not None), db.securities[0])
        if primary.fw_signature is not None:
            signature = _as_bytes(primary.fw_signature, f"{label}: FW-SIGNATURE")
        for key_name, value in (("security_method", primary.security_method),
                                ("fw_checksum", primary.fw_checksum)):
            if value is not None:
                meta[key_name] = _sub_elem(value)
        meta["securities"] = [_security(s) for s in db.securities]
    if session.securities:
        # Session-level SECURITYS cover the whole session, not this block: metadata only.
        meta["session_securities"] = [_security(s) for s in session.securities]

    return LogicalBlock(
        name=db.short_name,
        segments=segs,
        data_format_identifier=dfi,
        address_and_length_format=_alfid(fd, label),
        memory_size=memory_size,
        signature=signature,
        metadata=meta,
    )


def _decode(container: _Container, fd: Flashdata, fmt: DataformatSelection, what: str):
    """bytes for BINARY, a bincopy.BinFile for INTEL-HEX / MOTOROLA-S."""
    label = f"{container.label}: FLASHDATA {fd.short_name}"
    if isinstance(fd, InternFlashdata):
        if fmt is DataformatSelection.BINARY:
            try:
                return bytes.fromhex(re.sub(r"\s", "", fd.data))
            except ValueError as exc:
                raise FirmwareError(f"{label}: DATA is not hex encoded: {exc}") from exc
        text = fd.data
    elif isinstance(fd, ExternFlashdata):
        raw = container.read_datafile(fd.datafile.value.strip(), fd.datafile.latebound_datafile, what)
        if fmt is DataformatSelection.BINARY:
            return raw
        text = raw.decode("latin-1")  # never fails; bad records are reported by bincopy
    else:
        raise FirmwareError(f"{label}: unknown FLASHDATA type {type(fd).__name__}")

    records = "\n".join(line for line in (re.sub(r"\s", "", x) for x in text.splitlines()) if line)
    bf = bincopy.BinFile()
    try:
        if fmt is DataformatSelection.INTEL_HEX:
            bf.add_ihex(records + "\n")
        else:
            bf.add_srec(records + "\n")
    except Exception as exc:  # bincopy raises its own error types
        raise FirmwareError(f"{label}: {exc}") from exc
    return bf


def _source_length(seg, compressed: bool) -> int | None:
    """Bytes the SEGMENT occupies in the FLASHDATA, None if only the rest of the data can tell."""
    if seg.source_end_address is not None:  # inclusive end address
        return seg.source_end_address - seg.source_start_address + 1
    if seg.compressed_size is not None:
        return seg.compressed_size
    if seg.uncompressed_size is not None and not compressed:
        return seg.uncompressed_size
    return None  # compressed, only UNCOMPRESSED-SIZE given


def _hex_segments(bf: bincopy.BinFile, db: Datablock, compressed: bool,
                  label: str) -> tuple[list[Segment], int]:
    if not db.segments:
        if db.filters:
            raise FirmwareError(f"{label}: FILTERS without SEGMENTS are not supported")
        segs = [Segment(s.minimum_address, bytes(s.data)) for s in bf.segments]
        if not segs:
            raise FirmwareError(f"{label}: the flash data is empty")
        return segs, 0
    out, fill = [], 0
    for seg in db.segments:
        start = seg.source_start_address
        length = _source_length(seg, compressed)
        if length is None:  # the contiguous data run starting at SOURCE-START-ADDRESS
            run = next((s for s in bf.segments if s.minimum_address <= start < s.maximum_address), None)
            if run is None:
                raise FirmwareError(f"{label}: no data at 0x{start:X} (SEGMENT {seg.short_name})")
            length = run.maximum_address - start
        if length <= 0:
            raise FirmwareError(f"{label}: SEGMENT {seg.short_name} has an end address before its start")
        # The segment is the file content in [start, end]; gaps inside are filled with FILL_BYTE.
        buf = bytearray([FILL_BYTE]) * length
        covered = 0
        for s in bf.segments:
            lo, hi = max(s.minimum_address, start), min(s.maximum_address, start + length)
            if lo < hi:
                buf[lo - start:hi - start] = s.data[lo - s.minimum_address:hi - s.minimum_address]
                covered += hi - lo
        if not covered:
            raise FirmwareError(f"{label}: no data in 0x{start:X}..0x{start + length - 1:X} "
                                f"(SEGMENT {seg.short_name})")
        fill += length - covered
        out.append(Segment(start, bytes(buf)))
    return out, fill


def _binary_segments(data: bytes, db: Datablock, compressed: bool, label: str) -> list[Segment]:
    # BINARY flash data carries no addresses. ASSUMPTION (ODX 2.2 does not pin the layout down):
    # the SEGMENTs are stored back to back in the file, in SEGMENTS order, each taking its
    # source length (SOURCE-END-ADDRESS - SOURCE-START-ADDRESS + 1, else COMPRESSED-SIZE, else
    # UNCOMPRESSED-SIZE); SOURCE-START-ADDRESS only gives the address. A compressed last segment
    # with only UNCOMPRESSED-SIZE takes the rest of the file. The file length must match exactly,
    # so a file with another layout (e.g. a memory image with gaps) is rejected, not mis-flashed.
    if not db.segments:
        raise FirmwareError(f"{label}: BINARY flash data needs SEGMENTS (they give the addresses)")
    out, offset = [], 0
    for i, seg in enumerate(db.segments):
        length = _source_length(seg, compressed)
        if length is None:
            if i != len(db.segments) - 1:
                raise FirmwareError(f"{label}: SEGMENT {seg.short_name} needs SOURCE-END-ADDRESS or "
                                    "COMPRESSED-SIZE (only the last segment may take the rest of the file)")
            length = len(data) - offset
        if length <= 0 or offset + length > len(data):
            raise FirmwareError(f"{label}: BINARY data is {len(data)} bytes, SEGMENT {seg.short_name} "
                                f"needs bytes {offset}..{offset + length - 1} (segments laid out back to back)")
        out.append(Segment(seg.source_start_address, data[offset:offset + length]))
        offset += length
    if offset != len(data):
        raise FirmwareError(f"{label}: BINARY data is {len(data)} bytes but its SEGMENTS cover {offset} "
                            "(segments are expected back to back in the file)")
    return out


def _target_offset(db: Datablock) -> int:
    tao = db.target_addr_offset
    if isinstance(tao, PosOffset):
        return tao.positive_offset
    if isinstance(tao, NegOffset):
        return -tao.negative_offset
    return 0


def _dfi(method: EncryptCompressMethod | None, label: str) -> int:
    """ENCRYPT-COMPRESS-METHOD -> dataFormatIdentifier of 0x34 (the value is taken as the DFI)."""
    if method is None:
        return 0x00
    value = method.value
    if isinstance(value, (bytes, bytearray)):
        value = int.from_bytes(value, "big") if value else 0
    if not isinstance(value, int) or not 0 <= value <= 0xFF:
        raise FirmwareError(f"{label}: ENCRYPT-COMPRESS-METHOD '{method.value_raw.strip()}' "
                            "does not fit in a dataFormatIdentifier byte")
    return value


def _alfid(fd: Flashdata, label: str) -> int:
    if fd.address_length is None and fd.size_length is None:
        return 0x44
    addr = 4 if fd.address_length is None else fd.address_length
    size = 4 if fd.size_length is None else fd.size_length
    if not (1 <= addr <= 15 and 1 <= size <= 15):
        raise FirmwareError(f"{label}: ADDRESS-LENGTH {addr} / SIZE-LENGTH {size} do not fit an ALFID")
    return (size << 4) | addr


def _plain(value):
    """JSON-friendly value: byte fields become upper-case hex strings."""
    return bytes(value).hex().upper() if isinstance(value, (bytes, bytearray)) else value


def _sub_elem(value: ValidityFor) -> dict:
    return {"type": value.value_type.value, "value": _plain(value.value)}


def _security(sec: Security) -> dict:
    fields = (("security_method", sec.security_method), ("fw_signature", sec.fw_signature),
              ("fw_checksum", sec.fw_checksum), ("validity_for", sec.validity_for))
    return {k: _sub_elem(v) for k, v in fields if v is not None}


def _as_bytes(value: ValidityFor, label: str) -> bytes:
    v = value.value
    if isinstance(v, (bytes, bytearray)):  # A_BYTEFIELD
        return bytes(v)
    if isinstance(v, int):  # A_UINT32
        try:
            return v.to_bytes(4, "big")
        except OverflowError as exc:
            raise FirmwareError(f"{label}: {v} is not an A_UINT32") from exc
    text = str(v).strip()  # A_ASCIISTRING: hex text if it is hex, else the characters
    try:
        return bytes.fromhex(text)
    except ValueError:
        return text.encode("ascii", errors="replace")
