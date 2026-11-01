"""Loaders for BIN / Intel HEX / Motorola S-record / VBF files and PDX / ODX-F containers."""

from __future__ import annotations

import os
from dataclasses import dataclass

import bincopy

from .model import FirmwareImage, LogicalBlock, Segment, SourceFile, normalize_block_type, sha256_file

HEX_EXT = {".hex", ".ihex", ".ihx"}
SREC_EXT = {".s19", ".s28", ".s37", ".srec", ".mot", ".sx"}
BIN_EXT = {".bin", ".bin_"}
ODX_EXT = {".pdx", ".odx-f", ".odx"}
VBF_EXT = {".vbf"}


class FirmwareError(Exception):
    pass


@dataclass
class FirmwareSpec:
    """One `firmware:` entry of a project."""

    file: str
    name: str | None = None  # block name (PDX / ODX-F: prefix "<name>.<DATABLOCK>")
    type: str | None = None  # block type (BOOT / APP / CAL / DATA / ...); default: from the file, else APP
    # logical block index: a number, or one per block when the file gives several ([1, 2]; null = not set).
    # Default: ODX-F LOGICAL-BLOCK-INDEX.
    block_index: int | list[int | None] | None = None
    base_address: int | None = None  # required for .bin
    # Explicit values below win over the file. None = not set: 0x00 / 0x44 for plain files,
    # ENCRYPT-COMPRESS-METHOD / ADDRESS-LENGTH + SIZE-LENGTH for ODX-F.
    dfi: int | None = None  # dataFormatIdentifier for 0x34
    alfid: int | None = None  # addressAndLengthFormatIdentifier for 0x34 / erase
    memory_address: int | None = None
    memory_size: int | None = None  # ODX-F default: UNCOMPRESSED-SIZE of compressed blocks
    erase: list[tuple[int, int]] | None = None
    fill_gaps: int | None = None  # fill byte used when merging close segments
    max_gap: int = 0  # merge segments separated by <= max_gap bytes
    split: str = "file"  # "file": one block per file, "segments": one block per segment (not ODX)
    session: str | None = None  # ODX-F SESSION short name (needed when the file has several)
    release_dir: str | None = None  # where new builds of this file appear (GUI release picker only)
    release_pattern: str | None = None  # file name filter for the release picker, e.g. "app_*.hex"

    @classmethod
    def from_dict(cls, d: dict) -> FirmwareSpec:
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(d) - known
        if unknown:
            raise FirmwareError(f"Unknown firmware option(s): {', '.join(sorted(unknown))}")
        d = dict(d)
        index = d.get("block_index")
        values = index if isinstance(index, list) else [index]
        if not all(v is None or (isinstance(v, int) and not isinstance(v, bool) and v >= 0) for v in values):
            raise FirmwareError(f"block_index must be a number, or a list with one number per block, got {index!r}")
        if d.get("erase") is not None:
            d["erase"] = [(int(a), int(n)) for a, n in d["erase"]]
        return cls(**d)


def detect_format(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in HEX_EXT:
        return "ihex"
    if ext in SREC_EXT:
        return "srec"
    if ext in BIN_EXT:
        return "binary"
    if ext in ODX_EXT:
        return "odx"
    if ext in VBF_EXT:
        return "vbf"
    raise FirmwareError(f"Unsupported firmware file type '{ext}' ({path})")


def _read_segments(spec: FirmwareSpec, path: str) -> list[Segment]:
    fmt = detect_format(path)
    bf = bincopy.BinFile()
    try:
        if fmt == "ihex":
            bf.add_ihex_file(path)
        elif fmt == "srec":
            bf.add_srec_file(path)
        elif fmt == "binary":
            if spec.base_address is None:
                raise FirmwareError(f"{os.path.basename(path)}: .bin files need 'base_address'")
            bf.add_binary_file(path, address=spec.base_address)
        else:
            raise FirmwareError(f"{os.path.basename(path)}: {fmt} files are loaded by load_firmware")
    except FirmwareError:
        raise
    except Exception as exc:  # bincopy raises its own error types
        raise FirmwareError(f"{os.path.basename(path)}: {exc}") from exc
    segs = [Segment(s.minimum_address, bytes(s.data)) for s in bf.segments]
    return _merge_gaps(segs, spec.max_gap, spec.fill_gaps) if spec.max_gap else segs


def _merge_gaps(segs: list[Segment], max_gap: int, fill: int | None) -> list[Segment]:
    fill_byte = bytes([0xFF if fill is None else fill])
    out: list[Segment] = []
    for seg in sorted(segs, key=lambda s: s.address):
        if out and 0 <= seg.address - out[-1].end <= max_gap:
            prev = out[-1]
            gap = seg.address - prev.end
            out[-1] = Segment(prev.address, prev.data + fill_byte * gap + seg.data)
        else:
            out.append(seg)
    return out


def _source(path: str) -> SourceFile:
    return SourceFile(path=path, sha256=sha256_file(path), size=os.path.getsize(path))


def _odx_flash():
    try:
        from . import odx_flash  # imports odxtools, only needed for PDX / ODX-F
    except ImportError as exc:
        if not (exc.name or "").startswith("odxtools"):
            raise
        raise FirmwareError(f"PDX/ODX-F loading needs the 'odxtools' package ({exc})") from exc
    return odx_flash


def _vbf():
    from . import vbf  # imports this module (FirmwareError, FirmwareSpec)

    return vbf


def odx_sessions(path: str) -> list:
    """The ODX-F sessions of a .pdx / .odx-f file (list of odx_flash.OdxSession)."""
    return _odx_flash().list_sessions(path)


def _load_odx(spec: FirmwareSpec, path: str) -> tuple[list[LogicalBlock], list[SourceFile]]:
    """One block per DATABLOCK of the selected ODX-F session; explicit spec values win."""
    odx_flash = _odx_flash()
    if spec.split != "file":
        raise FirmwareError(f"{os.path.basename(path)}: 'split' does not apply to PDX/ODX-F "
                            "(blocks are the DATABLOCKs of the session)")
    content = odx_flash.load_odx(path, spec.session)
    single = [k for k in ("memory_address", "memory_size", "erase") if getattr(spec, k) is not None]
    if single and len(content.blocks) != 1:
        raise FirmwareError(f"{os.path.basename(path)}: {', '.join(single)} need a single block, session "
                            f"'{content.session.label}' has {len(content.blocks)} DATABLOCKs")
    for block in content.blocks:
        if spec.name:
            block.name = f"{spec.name}.{block.name}"
        if spec.max_gap:
            block.segments = _merge_gaps(block.segments, spec.max_gap, spec.fill_gaps)
        if spec.dfi is not None:
            block.data_format_identifier = spec.dfi
        if spec.alfid is not None:
            block.address_and_length_format = spec.alfid
        if spec.memory_address is not None:
            block.memory_address = spec.memory_address
        if spec.memory_size is not None:
            block.memory_size = spec.memory_size
        if spec.erase is not None:
            block.erase_ranges = spec.erase
    return content.blocks, [_source(path)] + [_source(p) for p in content.data_files]


def _set_type(blocks: list[LogicalBlock], spec: FirmwareSpec) -> list[LogicalBlock]:
    """Explicit type / index of the project entry win over the file (ODX TYPE / LOGICAL-BLOCK-INDEX, VBF)."""
    indexes = spec.block_index
    name = os.path.basename(spec.file)
    if isinstance(indexes, list):
        if len(indexes) != len(blocks):
            raise FirmwareError(f"{name}: block_index has {len(indexes)} value(s), the file gives "
                                f"{len(blocks)} block(s) ({', '.join(b.name for b in blocks)})")
    elif indexes is not None:
        if len(blocks) != 1:
            raise FirmwareError(f"{name} gives {len(blocks)} blocks ({', '.join(b.name for b in blocks)}): "
                                "block_index needs one value per block, e.g. [1, 2]")
        indexes = [indexes]
    for i, block in enumerate(blocks):
        block.type = normalize_block_type(spec.type) or normalize_block_type(block.metadata.get("type")) or "APP"
        if indexes is not None and indexes[i] is not None:
            block.index = indexes[i]
        elif block.index is None and isinstance(block.metadata.get("logical_block_index"), int):
            block.index = block.metadata["logical_block_index"]
    return blocks


def load_firmware(specs: list[FirmwareSpec], base_dir: str = ".") -> FirmwareImage:
    blocks: list[LogicalBlock] = []
    sources: list[SourceFile] = []
    for spec in specs:
        path = spec.file if os.path.isabs(spec.file) else os.path.join(base_dir, spec.file)
        if not os.path.isfile(path):
            raise FirmwareError(f"Firmware file not found: {path}")
        fmt = detect_format(path)
        if fmt == "odx":
            odx_blocks, odx_sources = _load_odx(spec, path)
            blocks += _set_type(odx_blocks, spec)
            sources += odx_sources
            continue
        if spec.session is not None:
            raise FirmwareError(f"{os.path.basename(path)}: 'session' only applies to PDX/ODX-F files")
        if fmt == "vbf":
            blocks += _set_type(_vbf().load_vbf(spec, path), spec)
            sources.append(_source(path))
            continue
        segs = _read_segments(spec, path)
        if not segs:
            raise FirmwareError(f"{os.path.basename(path)} contains no data")
        sources.append(_source(path))
        stem = spec.name or os.path.splitext(os.path.basename(path))[0]
        groups = [[s] for s in segs] if spec.split == "segments" else [segs]
        blocks += _set_type([
            LogicalBlock(
                name=stem if len(groups) == 1 else f"{stem}#{i}",
                segments=group,
                data_format_identifier=0x00 if spec.dfi is None else spec.dfi,
                address_and_length_format=0x44 if spec.alfid is None else spec.alfid,
                memory_address=spec.memory_address,
                memory_size=spec.memory_size,
                erase_ranges=spec.erase,
                metadata={"source": path},
            )
            for i, group in enumerate(groups)
        ], spec)
    return FirmwareImage(blocks=blocks, sources=sources)
