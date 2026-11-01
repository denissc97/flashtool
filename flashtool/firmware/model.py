"""Source-independent firmware model (docs/ARCHITECTURE.md §4.3)."""

from __future__ import annotations

import hashlib
import zlib
from dataclasses import dataclass, field


@dataclass
class Segment:
    address: int
    data: bytes

    @property
    def end(self) -> int:
        """Exclusive end address."""
        return self.address + len(self.data)


@dataclass
class LogicalBlock:
    """One unit of erase + download.

    The tool never compresses or encrypts: `data` is sent exactly as stored in the
    file, and the 0x34 parameters come from configuration.
    """

    name: str
    segments: list[Segment]
    data_format_identifier: int = 0x00
    address_and_length_format: int = 0x44
    memory_address: int | None = None  # override of the 0x34 address (single segment only)
    memory_size: int | None = None  # override of the 0x34 size (single segment only)
    erase_ranges: list[tuple[int, int]] | None = None  # (address, length)
    signature: bytes | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def start(self) -> int:
        return min(s.address for s in self.segments)

    @property
    def end(self) -> int:
        return max(s.end for s in self.segments)

    @property
    def size(self) -> int:
        """Number of bytes that will be transferred."""
        return sum(len(s.data) for s in self.segments)

    @property
    def data(self) -> bytes:
        return b"".join(s.data for s in self.segments)

    @property
    def crc32(self) -> int:
        return zlib.crc32(self.data) & 0xFFFFFFFF

    @property
    def effective_erase_ranges(self) -> list[tuple[int, int]]:
        if self.erase_ranges is not None:
            return self.erase_ranges
        return [(self.start, self.end - self.start)]

    def download_regions(self) -> list[tuple[int, int, bytes]]:
        """(memory_address, memory_size, data) for each RequestDownload."""
        if self.memory_address is not None or self.memory_size is not None:
            seg = self.segments[0]
            address = self.memory_address if self.memory_address is not None else seg.address
            size = self.memory_size if self.memory_size is not None else len(seg.data)
            return [(address, size, seg.data)]
        return [(s.address, len(s.data), s.data) for s in self.segments]


@dataclass
class SourceFile:
    path: str
    sha256: str
    size: int


@dataclass
class FirmwareImage:
    blocks: list[LogicalBlock]
    sources: list[SourceFile] = field(default_factory=list)

    @property
    def total_size(self) -> int:
        return sum(b.size for b in self.blocks)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()
