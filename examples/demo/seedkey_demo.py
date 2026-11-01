"""Seed&Key plugin for the built-in simulator.

A real project points `seed_key` at the OEM's library (provider: library) or at an
OEM-supplied Python plugin with the same `generate_key` signature.
"""

SECRET = 0x5A3C96E1


def generate_key(seed: bytes, level: int, variant: str) -> bytes:
    v = int.from_bytes(seed, "big") ^ SECRET
    v = ((v << 7) | (v >> 25)) & 0xFFFFFFFF
    return ((v + 0x1234) & 0xFFFFFFFF).to_bytes(4, "big")
