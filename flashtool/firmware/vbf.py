"""VBF (Volvo Binary Format) firmware files (docs/ARCHITECTURE.md §4.3).

VBF is the download format of Volvo Cars, Geely, Polestar, Lynk & Co, Ford / Mazda and JLR.
There is no public specification. The layout below was cross-checked against these public
sources (read 2026-11); where they disagree the choice is explained in the code:

* EWYan/vbf-tool (Rust VBF generator) - src/vbf_parser/crc.rs, writer.rs, generator.rs:
  block CRC = CRC-16/IBM-3740 (= CCITT-FALSE) of the data, big endian; file_checksum =
  CRC-32/IEEE 802.3 over every byte of the binary section (address, length, data, CRC16).
  https://github.com/EWYan/vbf-tool
* AuRoN89/FTools VbfEditor (C++, Ford VBF unpack/repack) - VbfEditor/VbfFile.cpp: CRC-32 of
  everything after the header's closing brace must equal file_checksum; CRC_16_CCITTFALSE
  per block (a mismatch is only a warning there). https://github.com/AuRoN89/FTools
* jodojodo/vbf_parser, esaulenka/vbf_parser: block = BE u32 address, BE u32 length, data,
  BE u16 CRC16 (init 0xFFFF, poly 0x1021). https://github.com/esaulenka/vbf_parser
* consp/vbfdecode, gorgiaxx/vbf_extractor: header keys (erase, verification_block_*,
  sw_signature_dev, ...); the verification block table is one of the data blocks.
  https://github.com/consp/vbfdecode, https://github.com/gorgiaxx/vbf_extractor
* rafalslaby/vbf_parser: header grammar (C comments, nested {} lists).
  https://github.com/rafalslaby/vbf_parser
* "VBF(Versatile Binary Format)格式简介" (summary of the Volvo specification):
  key list, sw_part_type values, erase / call rules, DFI nibbles, file_checksum required.
  https://www.cnblogs.com/tridentj/p/17881005.html
* icanhack.nl "OEM update files" (Ford VBF 3.x): file_checksum is a CRC-32 over the binary
  section; block CRC-16 "over the (uncompressed) data".
  https://icanhack.nl/knowledge-base/reverse-engineering/oem-update-files/
* mk4-wiki.denkdose.de/artikel/vbf/start: VBF 2.2 example header (SBL with `call`).

File layout::

    vbf_version = 2.6;
    header {                      // C comments allowed in the header only
        sw_part_number = "32290001"; sw_version = "AA"; sw_part_type = EXE;
        data_format_identifier = 0x00; ecu_address = 0x1A01;
        erase = { { 0x00010000, 0x00020000 }, ... };
        file_checksum = 0x6A8DB4CC;
    }<binary section: blocks, immediately after the closing brace>
    block = u32 BE start address | u32 BE length | data | u16 BE CRC-16/CCITT-FALSE(data)

Mapping to the firmware model (one LogicalBlock per file by default):

    data blocks (file order)       -> segments
    data_format_identifier         -> data_format_identifier (data is sent as stored, never
                                      decompressed or decrypted)
    erase                          -> erase_ranges (no `erase` key: nothing is erased - SBLs)
    sw_signature / sw_signature_dev-> signature (+ metadata)
    sw_part_number, sw_version,
    sw_part_type                   -> metadata["own_idents"], sw_part_type -> metadata["type"]
    ecu_address, call, description,
    verification_block_*, ...      -> metadata
"""

from __future__ import annotations

import binascii
import os
import re
import struct
import zlib
from dataclasses import dataclass, field
from typing import Any

from .loaders import FirmwareError, FirmwareSpec, _merge_gaps
from .model import LogicalBlock, Segment

SUPPORTED_MAJOR_VERSIONS = (2, 3)  # 2.x: Volvo / Geely / JLR, 3.x: Ford
# sw_part_type is not checked against a list: the Volvo summary lists CARCFG, CUSTOM, DATA, EXE, SBL,
# SIGCFG, PBL, SSBL, TEST, FBL, CERT, PWD, APP, MAP, ESS, SIGNED, VARIANT_INFO and OEMs add their own.
_BYTES_KEYS = ("sw_signature", "sw_signature_dev", "verification_block_root_hash", "public_key_hash")


def crc16_ccitt_false(data: bytes) -> int:
    """CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, not reflected, no final XOR (check 0x29B1)."""
    return binascii.crc_hqx(data, 0xFFFF)


def crc32(data: bytes) -> int:
    """CRC-32/IEEE 802.3 (zlib): poly 0x04C11DB7 reflected, init and final XOR 0xFFFFFFFF (check 0xCBF43926)."""
    return zlib.crc32(data) & 0xFFFFFFFF


class HexValue(int):
    """A `0x..` header value. Keeps its digit count so hashes and signatures keep leading zero bytes."""

    digits: int

    def __new__(cls, digits: str) -> HexValue:
        obj = super().__new__(cls, int(digits, 16))
        obj.digits = len(digits)
        return obj

    def as_bytes(self) -> bytes:
        return int(self).to_bytes((self.digits + 1) // 2, "big")


@dataclass
class VbfDataBlock:
    address: int
    data: bytes
    crc16: int  # as stored in the file
    crc16_ok: bool  # stored CRC16 == CRC16 of the stored bytes
    offset: int  # file offset of the block (its address field)


@dataclass
class VbfFile:
    version: str
    header: dict[str, Any]
    blocks: list[VbfDataBlock]
    binary_offset: int  # file offset of the first byte after the header's closing brace
    file_checksum: int
    unchecked_crc16: list[int] = field(default_factory=list)  # addresses, see parse_vbf

    def _str(self, key: str) -> str | None:
        value = self.header.get(key)
        return None if value is None else str(value)

    @property
    def sw_part_number(self) -> str | None:
        return self._str("sw_part_number")

    @property
    def sw_version(self) -> str | None:
        return self._str("sw_version")

    @property
    def sw_part_type(self) -> str | None:
        return self._str("sw_part_type")

    @property
    def data_format_identifier(self) -> int:
        return int(self.header.get("data_format_identifier", 0))

    @property
    def erase(self) -> list[tuple[int, int]] | None:
        """None when the header has no `erase` key."""
        return _erase_list(self.header["erase"], "VBF") if "erase" in self.header else None

    @property
    def description(self) -> str | None:
        value = self.header.get("description")
        if value is None:
            return None
        return "\n".join(str(v) for v in value) if isinstance(value, list) else str(value)


# -- header -----------------------------------------------------------------------------------------
_TOKEN = re.compile(
    rb"(?P<ws>[ \t\r\n\v\f]+)"
    rb"|(?P<comment>//[^\n]*|/\*.*?\*/)"
    rb'|(?P<str>"[^"]*")'
    rb"|(?P<hex>0[xX][0-9A-Fa-f]+)"
    rb"|(?P<num>[0-9]+(?:\.[0-9]+)*)"
    rb"|(?P<id>[A-Za-z_][A-Za-z0-9_]*)"
    rb"|(?P<punct>[={};,])",
    re.S,
)


class _Header:
    """Tokenizer + recursive-descent parser for `vbf_version = x; header { key = value; ... }`.

    Works on the raw bytes and stops exactly after the closing brace of `header`, so the
    binary section is never decoded. A `}` inside a comment or a string does not close it.
    """

    def __init__(self, data: bytes, label: str):
        self.data, self.label = data, label
        self.pos = 3 if data.startswith(b"\xef\xbb\xbf") else 0  # scan position (skip a UTF-8 BOM)
        self.end = 0  # end of the last consumed token
        self._ahead: tuple[str, str, int, int] | None = None

    def _scan(self) -> tuple[str, str, int, int]:
        while True:
            if self.pos >= len(self.data):
                raise FirmwareError(f"{self.label}: VBF header is not closed (missing '}}' after 'header {{')")
            m = _TOKEN.match(self.data, self.pos)
            if m is None:
                if self.data.startswith(b"/*", self.pos):
                    raise FirmwareError(f"{self.label}: unterminated /* comment in the VBF header")
                raise FirmwareError(f"{self.label}: unexpected byte 0x{self.data[self.pos]:02X} in the VBF header "
                                    f"at offset {self.pos} (header not closed?)")
            self.pos = m.end()
            if m.lastgroup not in ("ws", "comment"):
                return str(m.lastgroup), m.group().decode("latin-1"), m.start(), m.end()

    def peek(self) -> tuple[str, str]:
        if self._ahead is None:
            self._ahead = self._scan()
        return self._ahead[0], self._ahead[1]

    def take(self) -> tuple[str, str, int]:
        token = self._ahead if self._ahead is not None else self._scan()
        self._ahead = None
        self.end = token[3]
        return token[0], token[1], token[2]

    def expect(self, text: str, what: str | None = None) -> None:
        _, got, offset = self.take()
        if got != text:
            raise FirmwareError(f"{self.label}: expected {what or repr(text)} at offset {offset}, found {got!r}")

    def value(self) -> Any:
        kind, text, offset = self.take()
        if kind == "punct" and text == "{":
            return self.list_()
        if kind == "str":
            return self.data[offset + 1 : self.end - 1].decode("utf-8", errors="replace")
        if kind == "hex":
            return HexValue(text[2:])
        if kind == "num":
            return int(text) if text.isdigit() else text
        if kind == "id":
            return text
        raise FirmwareError(f"{self.label}: expected a value at offset {offset}, found {text!r}")

    def list_(self) -> list:
        items: list = []
        if self.peek() == ("punct", "}"):
            self.take()
            return items
        while True:
            items.append(self.value())
            _, text, offset = self.take()
            if text == "}":
                return items
            if text != ",":
                raise FirmwareError(f"{self.label}: expected ',' or '}}' at offset {offset}, found {text!r}")
            if self.peek() == ("punct", "}"):  # trailing comma
                self.take()
                return items

    def parse(self) -> tuple[str, dict[str, Any], int]:
        if not self.data[self.pos : self.pos + 4096].lstrip().startswith(b"vbf_version"):
            raise FirmwareError(f"{self.label}: not a VBF file (must start with 'vbf_version = ...;')")
        kind, text, _ = self.take()
        if text != "vbf_version":
            raise FirmwareError(f"{self.label}: not a VBF file (must start with 'vbf_version = ...;')")
        self.expect("=")
        kind, version, offset = self.take()
        if kind != "num":
            raise FirmwareError(f"{self.label}: invalid vbf_version {version!r}")
        self.expect(";")
        self.expect("header", "'header {'")
        self.expect("{")
        header: dict[str, Any] = {}
        while True:
            kind, key, offset = self.take()
            if kind == "punct" and key == "}":
                return version, header, self.end
            if kind != "id":
                raise FirmwareError(f"{self.label}: expected a header key at offset {offset}, found {key!r}")
            self.expect("=", f"'=' after '{key}'")
            value = self.value()
            self.expect(";", f"';' after the value of '{key}'")
            if key in header:
                raise FirmwareError(f"{self.label}: header key '{key}' is defined twice")
            header[key] = value


def parse_header(data: bytes, label: str = "VBF") -> tuple[str, dict[str, Any], int]:
    """(vbf_version, header values, offset of the binary section)."""
    return _Header(data, label).parse()


# -- whole file -------------------------------------------------------------------------------------
def _int(header: dict, key: str, label: str, bits: int) -> int | None:
    value = header.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or not 0 <= value < (1 << bits):
        raise FirmwareError(f"{label}: '{key}' must be a {bits}-bit number, got {value!r}")
    return int(value)


def _erase_list(value: Any, label: str) -> list[tuple[int, int]]:
    if not isinstance(value, list):
        raise FirmwareError(f"{label}: 'erase' must be a list of {{ start, length }} pairs")
    if len(value) == 2 and all(isinstance(v, int) for v in value):  # single pair without the outer braces
        value = [value]
    out = []
    for item in value:
        if not (isinstance(item, list) and len(item) == 2 and all(isinstance(v, int) for v in item)):
            raise FirmwareError(f"{label}: 'erase' entries must be {{ start, length }} pairs, got {item!r}")
        start, length = int(item[0]), int(item[1])
        if not (0 <= start < 1 << 32 and 0 < length < 1 << 32 and start + length <= 1 << 32):
            raise FirmwareError(f"{label}: invalid erase range 0x{start:X} + 0x{length:X}")
        out.append((start, length))
    return out


def _as_bytes(value: Any, key: str, label: str) -> bytes:
    if isinstance(value, HexValue):
        return value.as_bytes()
    if isinstance(value, str):
        text = re.sub(r"\s", "", value)
        text = text[2:] if text.lower().startswith("0x") else text
        try:
            return bytes.fromhex(text if len(text) % 2 == 0 else "0" + text)
        except ValueError:
            pass
    if isinstance(value, list) and all(isinstance(v, int) and 0 <= v <= 0xFF for v in value):
        return bytes(value)
    raise FirmwareError(f"{label}: '{key}' is not a hex value: {value!r}")


def parse_vbf(data: bytes, label: str = "VBF") -> VbfFile:
    """Parse and check a VBF file. Raises FirmwareError on any format or checksum problem.

    Block CRC16s are checked against the stored bytes. For compressed / encrypted data
    (data_format_identifier != 0) Ford documents the CRC16 over the *uncompressed* data,
    which cannot be checked without decompressing: such a mismatch is not an error (the
    stored bytes are still covered by file_checksum) and the block address is listed in
    `unchecked_crc16`.
    """
    version, header, offset = parse_header(data, label)
    major = version.split(".")[0]
    if not major.isdigit() or int(major) not in SUPPORTED_MAJOR_VERSIONS:
        raise FirmwareError(f"{label}: VBF version {version} is not supported "
                            f"(supported: {', '.join(f'{v}.x' for v in SUPPORTED_MAJOR_VERSIONS)})")

    stored = _int(header, "file_checksum", label, 32)
    if stored is None:
        raise FirmwareError(f"{label}: the VBF header has no file_checksum")
    computed = crc32(data[offset:])
    if computed != stored:
        raise FirmwareError(f"{label}: file_checksum mismatch: header says 0x{stored:08X}, binary section "
                            f"gives 0x{computed:08X} - the file is corrupted or was modified")

    dfi = _int(header, "data_format_identifier", label, 8) or 0
    for key in ("ecu_address", "call", "verification_block_start", "verification_block_length"):
        _int(header, key, label, 32)
    if "erase" in header:
        _erase_list(header["erase"], label)
    for key in _BYTES_KEYS:
        if key in header:
            _as_bytes(header[key], key, label)

    blocks: list[VbfDataBlock] = []
    unchecked: list[int] = []
    pos = offset
    while pos < len(data):
        index = len(blocks)
        if len(data) - pos < 10:
            raise FirmwareError(f"{label}: truncated data block #{index} at file offset 0x{pos:X} "
                                f"({len(data) - pos} byte(s) left, a block needs at least 10)")
        address, length = struct.unpack_from(">II", data, pos)
        start, end = pos + 8, pos + 8 + length
        if end + 2 > len(data):
            raise FirmwareError(f"{label}: data block #{index} at 0x{address:08X} claims {length} bytes but only "
                                f"{max(len(data) - start - 2, 0)} remain in the file")
        if length == 0:
            raise FirmwareError(f"{label}: data block #{index} at 0x{address:08X} is empty")
        if address + length > 1 << 32:
            raise FirmwareError(f"{label}: data block #{index} at 0x{address:08X} + {length} passes 4 GiB")
        payload = bytes(data[start:end])
        (crc,) = struct.unpack_from(">H", data, end)
        ok = crc16_ccitt_false(payload) == crc
        if not ok:
            if dfi == 0:
                raise FirmwareError(f"{label}: CRC16 mismatch in data block #{index} at 0x{address:08X}: stored "
                                    f"0x{crc:04X}, data gives 0x{crc16_ccitt_false(payload):04X}")
            unchecked.append(address)
        blocks.append(VbfDataBlock(address, payload, crc, ok, pos))
        pos = end + 2
    if not blocks:
        raise FirmwareError(f"{label}: the VBF file has no data blocks")
    return VbfFile(version=version, header=header, blocks=blocks, binary_offset=offset, file_checksum=stored,
                   unchecked_crc16=unchecked)


def read_vbf(path: str) -> VbfFile:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as exc:
        raise FirmwareError(f"{os.path.basename(path)}: {exc}") from exc
    return parse_vbf(data, os.path.basename(path))


# -- firmware model ---------------------------------------------------------------------------------
def _plain(value: Any) -> Any:
    """Header value as plain JSON-able data (hex values become ints, byte strings stay hex text)."""
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, int):
        return int(value)
    return value


def vbf_metadata(vbf: VbfFile, path: str) -> dict:
    h = vbf.header
    label = os.path.basename(path)
    meta: dict = {
        "source": path,
        "format": "vbf",
        "vbf_version": vbf.version,
        "type": vbf.sw_part_type,
        "own_idents": {k: str(h[k]) for k in ("sw_part_number", "sw_version", "sw_part_type") if k in h},
        "file_checksum": vbf.file_checksum,
        "crc16": [b.crc16 for b in vbf.blocks],
        "header": {k: _as_bytes(v, k, label).hex().upper() if k in _BYTES_KEYS else _plain(v) for k, v in h.items()},
    }
    for key in ("ecu_address", "call", "network", "frame_format", "sw_current_part_number", "sw_current_version"):
        if key in h:
            meta[key] = _plain(h[key])
    if vbf.description is not None:
        meta["description"] = vbf.description
    for key in _BYTES_KEYS:
        if key in h:
            meta[key] = _as_bytes(h[key], key, label).hex().upper()
    if "verification_block_start" in h:
        meta["verification_block"] = {
            "start": int(h["verification_block_start"]),
            "length": None if "verification_block_length" not in h else int(h["verification_block_length"]),
            "root_hash": meta.get("verification_block_root_hash"),
        }
    if vbf.unchecked_crc16:
        meta["crc16_unchecked"] = list(vbf.unchecked_crc16)
        meta["crc16_note"] = ("compressed/encrypted data: the block CRC16 does not match the stored bytes "
                              "(it is probably over the uncompressed data); file_checksum covers the stored bytes")
    return meta


def _signature(vbf: VbfFile, label: str) -> tuple[bytes | None, str | None]:
    """Production signature first; the development signature only when it is the only one."""
    for key in ("sw_signature", "sw_signature_dev"):
        if key in vbf.header:
            return _as_bytes(vbf.header[key], key, label), key
    return None, None


def _overlaps(a: tuple[int, int], seg: Segment) -> bool:
    return a[0] < seg.end and seg.address < a[0] + a[1]


def load_vbf(spec: FirmwareSpec, path: str) -> list[LogicalBlock]:
    """LogicalBlocks of one VBF file. Explicit spec values (dfi, alfid, erase, ...) win over the header."""
    label = os.path.basename(path)
    vbf = read_vbf(path)
    segments = [Segment(b.address, b.data) for b in vbf.blocks]
    dfi = vbf.data_format_identifier if spec.dfi is None else spec.dfi
    if spec.max_gap:
        if dfi != 0:
            raise FirmwareError(f"{label}: 'max_gap' cannot merge compressed/encrypted data (DFI 0x{dfi:02X})")
        segments = _merge_gaps(segments, spec.max_gap, spec.fill_gaps)
    erase = spec.erase if spec.erase is not None else (vbf.erase or [])
    signature, signature_key = _signature(vbf, label)
    stem = spec.name or os.path.splitext(os.path.basename(path))[0]

    def block(name: str, segs: list[Segment], erase_ranges: list[tuple[int, int]], meta: dict) -> LogicalBlock:
        if signature_key:
            meta["signature_source"] = signature_key
        return LogicalBlock(
            name=name,
            segments=segs,
            data_format_identifier=dfi,
            address_and_length_format=0x44 if spec.alfid is None else spec.alfid,
            memory_address=spec.memory_address,
            memory_size=spec.memory_size,
            erase_ranges=list(erase_ranges),
            signature=signature,
            metadata=meta,
        )

    if spec.split != "segments" or len(segments) == 1:
        return [block(stem, segments, erase, vbf_metadata(vbf, path))]
    # One block per VBF data block. Every erase range goes to the first block it overlaps (ranges
    # overlapping none to the first block), so a range is never erased after data was written into it.
    per_block: list[list[tuple[int, int]]] = [[] for _ in segments]
    for rng in erase:
        index = next((i for i, s in enumerate(segments) if _overlaps(rng, s)), 0)
        per_block[index].append(rng)
    out = []
    for i, seg in enumerate(segments):
        meta = vbf_metadata(vbf, path)
        meta["vbf_block_index"] = i
        out.append(block(f"{stem}#{i}", [seg], per_block[i], meta))
    return out
