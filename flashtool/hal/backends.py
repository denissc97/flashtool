"""Hardware abstraction: creates python-can buses and lists available channels.

Vector VN devices go through python-can's `vector` backend (XL Driver Library),
which python-can supports on Windows only. On Linux use `socketcan`; for tests and
for running on macOS use `virtual`.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

import can

SUPPORTED_BACKENDS = ("vector", "socketcan", "virtual", "pcan", "kvaser")


class HardwareError(Exception):
    pass


@dataclass
class CanHardware:
    backend: str = "virtual"
    channel: str | int = "sim0"
    app_name: str | None = "FlashTool"  # Vector: name in Vector Hardware Config
    serial: int | None = None  # Vector: pick a device by serial number
    extra: dict = field(default_factory=dict)  # passed through to can.Bus


def create_can_bus(hw: CanHardware, *, bitrate: int, fd: bool, data_bitrate: int | None) -> can.BusABC:
    if hw.backend not in SUPPORTED_BACKENDS:
        raise HardwareError(f"Unknown CAN backend '{hw.backend}' (supported: {', '.join(SUPPORTED_BACKENDS)})")
    if hw.backend == "vector" and sys.platform != "win32":
        raise HardwareError(
            "The Vector backend needs the Vector XL Driver Library, which python-can supports on Windows only. "
            "Use 'socketcan' on Linux or 'virtual' with the simulator."
        )
    kwargs: dict = {"interface": hw.backend, "channel": hw.channel}
    if hw.backend == "virtual":
        kwargs["receive_own_messages"] = False
    else:
        kwargs["bitrate"] = bitrate
        if fd:
            kwargs["fd"] = True
            if data_bitrate:
                kwargs["data_bitrate"] = data_bitrate
    if hw.backend == "vector":
        if hw.app_name:
            kwargs["app_name"] = hw.app_name
        if hw.serial is not None:
            kwargs["serial"] = hw.serial
    kwargs.update(hw.extra)
    try:
        return can.Bus(**kwargs)
    except Exception as exc:
        raise HardwareError(f"Cannot open {hw.backend} channel {hw.channel}: {exc}") from exc


def list_channels(backends: tuple[str, ...] = ("vector", "socketcan", "virtual")) -> list[dict]:
    """Channels python-can can detect; backends that are not installed are skipped."""
    found: list[dict] = []
    for backend in backends:
        try:
            configs = can.detect_available_configs(interfaces=[backend])
        except Exception:
            continue
        found.extend(dict(c) for c in configs)
    return found
