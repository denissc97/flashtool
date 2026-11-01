"""Seed & Key providers for SecurityAccess 0x27 (docs/ARCHITECTURE.md §4.7.1).

The tool never contains an OEM algorithm: keys come from the OEM-supplied library
(Vector `GenerateKeyEx` / `GenerateKeyExOpt` interface) or from a Python plugin.
"""

from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import struct
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class SeedKeyError(Exception):
    pass


class SeedKeyProvider(ABC):
    @abstractmethod
    def compute_key(self, seed: bytes, level: int, variant: str = "") -> bytes: ...

    def close(self) -> None:
        pass

    def describe(self) -> dict:
        return {"provider": type(self).__name__}


@dataclass
class SeedKeyConfig:
    provider: str = "none"  # none | python | library | bridge
    path: str | None = None
    function: str = "generate_key"  # python plugin entry point
    variant: str = ""
    options: str | None = None  # library: use GenerateKeyExOpt with this options string
    max_key_size: int = 64
    bridge_python: str | None = None  # bridge: interpreter matching the library's bitness
    extra: dict = field(default_factory=dict)


# -- Python plugin --------------------------------------------------------
class PythonSeedKey(SeedKeyProvider):
    """Plugin module exposing `generate_key(seed: bytes, level: int, variant: str) -> bytes`."""

    def __init__(self, path: str, function: str = "generate_key"):
        spec = importlib.util.spec_from_file_location(f"flashtool_seedkey_{abs(hash(path))}", path)
        if spec is None or spec.loader is None:
            raise SeedKeyError(f"cannot load seed&key plugin {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self._fn = getattr(module, function, None)
        if not callable(self._fn):
            raise SeedKeyError(f"{path} has no function '{function}'")
        self.path = path

    def compute_key(self, seed: bytes, level: int, variant: str = "") -> bytes:
        key = self._fn(bytes(seed), int(level), variant)
        if not isinstance(key, (bytes, bytearray)):
            raise SeedKeyError("seed&key plugin must return bytes")
        return bytes(key)

    def describe(self) -> dict:
        return {"provider": "python", "path": self.path}


# -- Native library (Vector KeyGen interface) -----------------------------
def library_bitness(path: str) -> int | None:
    """32 or 64 for a Windows PE / ELF / Mach-O library, None if unknown."""
    with open(path, "rb") as f:
        head = f.read(4096)
    if head[:2] == b"MZ" and len(head) >= 0x40:
        pe = struct.unpack_from("<I", head, 0x3C)[0]
        if pe + 6 <= len(head) and head[pe : pe + 4] == b"PE\0\0":
            machine = struct.unpack_from("<H", head, pe + 4)[0]
            return {0x014C: 32, 0x8664: 64, 0xAA64: 64}.get(machine)
    if head[:4] == b"\x7fELF":
        return {1: 32, 2: 64}.get(head[4])
    if head[:4] in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"):
        return 64
    if head[:4] in (b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xce"):
        return 32
    return None


PROCESS_BITS = struct.calcsize("P") * 8


class LibrarySeedKey(SeedKeyProvider):
    """DLL (Windows) or .so (Linux) exporting GenerateKeyEx / GenerateKeyExOpt.

    VKeyGenResultEx GenerateKeyEx(const unsigned char* seed, unsigned int seedSize,
        const unsigned int securityLevel, const char* variant,
        unsigned char* key, unsigned int maxKeySize, unsigned int& keySize);
    GenerateKeyExOpt has an extra `const char* options` after `variant`.
    """

    def __init__(self, path: str, options: str | None = None, max_key_size: int = 64):
        bits = library_bitness(path)
        if bits is not None and bits != PROCESS_BITS:
            raise SeedKeyError(
                f"{os.path.basename(path)} is {bits}-bit but FlashTool runs {PROCESS_BITS}-bit: "
                "use provider 'bridge' with a matching interpreter"
            )
        loader = ctypes.WinDLL if sys.platform == "win32" and bits == 32 else ctypes.CDLL  # type: ignore[attr-defined]
        try:
            self._lib = loader(path)
        except OSError as exc:
            raise SeedKeyError(f"cannot load {path}: {exc}") from exc
        self.path, self.options, self.max_key_size = path, options, max_key_size
        u8p, uint, cstr = ctypes.POINTER(ctypes.c_ubyte), ctypes.c_uint, ctypes.c_char_p
        if options is not None:
            self._fn = self._lib.GenerateKeyExOpt
            self._fn.argtypes = [u8p, uint, uint, cstr, cstr, u8p, uint, ctypes.POINTER(uint)]
        else:
            self._fn = self._lib.GenerateKeyEx
            self._fn.argtypes = [u8p, uint, uint, cstr, u8p, uint, ctypes.POINTER(uint)]
        self._fn.restype = ctypes.c_int

    def compute_key(self, seed: bytes, level: int, variant: str = "") -> bytes:
        seed_buf = (ctypes.c_ubyte * len(seed)).from_buffer_copy(seed)
        key_buf = (ctypes.c_ubyte * self.max_key_size)()
        key_len = ctypes.c_uint(0)
        args: list = [seed_buf, len(seed), level, variant.encode()]
        if self.options is not None:
            args.append(self.options.encode())
        args += [key_buf, self.max_key_size, ctypes.byref(key_len)]
        result = self._fn(*args)
        if result != 0:
            raise SeedKeyError(f"{os.path.basename(self.path)} returned error {result}")
        return bytes(key_buf[: key_len.value])

    def describe(self) -> dict:
        return {"provider": "library", "path": self.path, "options": self.options}


# -- Bridge: library loaded in a helper process ---------------------------
class BridgeSeedKey(SeedKeyProvider):
    """Runs `python -m flashtool.security.seedkey_host` with another interpreter.

    Used for 32-bit DLLs from a 64-bit FlashTool; any provider config can be bridged.
    Protocol: one JSON object per line on stdin/stdout.
    """

    def __init__(self, python: str, target: SeedKeyConfig):
        cmd = [python, "-m", "flashtool.security.seedkey_host", json.dumps(target.__dict__)]
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [os.path.dirname(os.path.dirname(os.path.dirname(__file__))), env.get("PYTHONPATH", "")]
        )
        try:
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env
            )
        except OSError as exc:
            raise SeedKeyError(f"cannot start seed&key bridge '{python}': {exc}") from exc
        hello = self._read()
        if not hello.get("ready"):
            raise SeedKeyError(f"seed&key bridge failed: {hello.get('error', 'no handshake')}")
        self.python = python
        self.target = target

    def _read(self) -> dict:
        assert self._proc.stdout is not None
        line = self._proc.stdout.readline()
        if not line:
            err = self._proc.stderr.read() if self._proc.stderr else ""
            raise SeedKeyError(f"seed&key bridge exited: {err.strip()[-500:]}")
        return json.loads(line)

    def compute_key(self, seed: bytes, level: int, variant: str = "") -> bytes:
        assert self._proc.stdin is not None
        self._proc.stdin.write(json.dumps({"seed": seed.hex(), "level": level, "variant": variant}) + "\n")
        self._proc.stdin.flush()
        reply = self._read()
        if "error" in reply:
            raise SeedKeyError(f"seed&key bridge: {reply['error']}")
        return bytes.fromhex(reply["key"])

    def close(self) -> None:
        if self._proc.poll() is None:
            try:
                assert self._proc.stdin is not None
                self._proc.stdin.close()
                self._proc.wait(timeout=2)
            except Exception:
                self._proc.kill()

    def describe(self) -> dict:
        return {"provider": "bridge", "python": self.python, "target": self.target.provider, "path": self.target.path}


def create_seedkey_provider(cfg: SeedKeyConfig, base_dir: str = ".") -> SeedKeyProvider | None:
    def resolve(p: str | None) -> str:
        if not p:
            raise SeedKeyError(f"seed&key provider '{cfg.provider}' needs 'path'")
        return p if os.path.isabs(p) else os.path.join(base_dir, p)

    if cfg.provider == "none":
        return None
    if cfg.provider == "python":
        return PythonSeedKey(resolve(cfg.path), cfg.function)
    if cfg.provider == "library":
        return LibrarySeedKey(resolve(cfg.path), cfg.options, cfg.max_key_size)
    if cfg.provider == "bridge":
        if not cfg.bridge_python:
            raise SeedKeyError("provider 'bridge' needs 'bridge_python'")
        target_kind = cfg.extra.get("target", "library")
        target = SeedKeyConfig(provider=target_kind, path=resolve(cfg.path), function=cfg.function,
                               options=cfg.options, max_key_size=cfg.max_key_size)
        return BridgeSeedKey(cfg.bridge_python, target)
    raise SeedKeyError(f"unknown seed&key provider '{cfg.provider}'")
