"""Checks run before any communication with the ECU (fail fast)."""

from __future__ import annotations

from dataclasses import dataclass

from .model import FirmwareImage


@dataclass
class Issue:
    severity: str  # "error" | "warning"
    block: str
    message: str

    def __str__(self) -> str:
        return f"[{self.severity}] {self.block}: {self.message}"


def _fits(value: int, nbytes: int) -> bool:
    return 0 <= value < (1 << (8 * nbytes))


def validate_image(image: FirmwareImage) -> list[Issue]:
    issues: list[Issue] = []
    if not image.blocks:
        issues.append(Issue("error", "-", "firmware image is empty"))

    for block in image.blocks:
        alfid = block.address_and_length_format
        addr_len, size_len = alfid & 0x0F, (alfid >> 4) & 0x0F
        if not (1 <= addr_len <= 8 and 1 <= size_len <= 8):
            issues.append(Issue("error", block.name, f"invalid ALFID 0x{alfid:02X}"))
            continue
        if block.size == 0:
            issues.append(Issue("error", block.name, "block has no data"))
        overridden = block.memory_address is not None or block.memory_size is not None
        if overridden and len(block.segments) != 1:
            issues.append(Issue("error", block.name,
                                "memory_address / memory_size overrides need a single-segment block"))
        for address, size, _ in block.download_regions():
            if not _fits(address, addr_len):
                issues.append(Issue("error", block.name,
                                    f"address 0x{address:X} does not fit in {addr_len} byte(s) (ALFID 0x{alfid:02X})"))
            if not _fits(size, size_len):
                issues.append(Issue("error", block.name,
                                    f"size {size} does not fit in {size_len} byte(s) (ALFID 0x{alfid:02X})"))
        if block.data_format_identifier != 0 and block.memory_size is None:
            issues.append(Issue("warning", block.name,
                                f"DFI 0x{block.data_format_identifier:02X} (compressed/encrypted) without memory_size: "
                                "0x34 will announce the transferred size"))

    regions = sorted(
        ((s.address, s.end, b.name) for b in image.blocks for s in b.segments), key=lambda r: r[0]
    )
    for (a0, e0, n0), (a1, e1, n1) in zip(regions, regions[1:]):
        if a1 < e0:
            issues.append(Issue("error", n1, f"overlaps {n0} at 0x{a1:X}..0x{min(e0, e1):X}"))
    return issues
