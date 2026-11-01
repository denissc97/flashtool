import json
import os
import shutil
import struct
import subprocess
import sys

import pytest

from flashtool.app import audit
from flashtool.app.manager import JobManager
from flashtool.app.project import ProjectError, load_project
from flashtool.security.seedkey import (
    BridgeSeedKey, LibrarySeedKey, PythonSeedKey, SeedKeyConfig, SeedKeyError, library_bitness,
)
from flashtool.sequence.events import JobFinished
from flashtool.sim.ecu import demo_key
from flashtool.transport.base import TransportError
from flashtool.transport.isotp_can import CanTpConfig, IsoTpCanTransport

from .conftest import SEEDKEY_PLUGIN

pytestmark = [pytest.mark.req("REQ-06"), pytest.mark.req("REQ-07"), pytest.mark.req("REQ-08"), pytest.mark.req("REQ-10")]


SEED = bytes.fromhex("11223344")


def test_python_plugin_matches_simulator():
    assert PythonSeedKey(SEEDKEY_PLUGIN).compute_key(SEED, 1, "") == demo_key(SEED)


def test_bridge_process_round_trip():
    target = SeedKeyConfig(provider="python", path=SEEDKEY_PLUGIN)
    bridge = BridgeSeedKey(sys.executable, target)
    try:
        assert bridge.compute_key(SEED, 1) == demo_key(SEED)
    finally:
        bridge.close()


def test_pe_bitness_detection(tmp_path):
    def pe(machine):
        head = bytearray(0x200)
        head[:2] = b"MZ"
        struct.pack_into("<I", head, 0x3C, 0x80)
        head[0x80:0x84] = b"PE\0\0"
        struct.pack_into("<H", head, 0x84, machine)
        return bytes(head)

    (tmp_path / "x86.dll").write_bytes(pe(0x014C))
    (tmp_path / "x64.dll").write_bytes(pe(0x8664))
    assert library_bitness(str(tmp_path / "x86.dll")) == 32
    assert library_bitness(str(tmp_path / "x64.dll")) == 64
    with pytest.raises(SeedKeyError, match="32-bit"):
        LibrarySeedKey(str(tmp_path / "x86.dll"))


C_KEYGEN = r"""
#include <string.h>
int GenerateKeyEx(const unsigned char* seed, unsigned int seedSize, const unsigned int level,
                  const char* variant, unsigned char* key, unsigned int maxKey, unsigned int* keySize) {
    if (seedSize > maxKey) return 1;
    for (unsigned int i = 0; i < seedSize; i++) key[i] = (unsigned char)(seed[i] ^ (0xA0 + level));
    *keySize = seedSize;
    return 0;
}
"""


@pytest.mark.skipif(shutil.which("cc") is None or sys.platform == "win32", reason="needs a C compiler")
def test_native_library_keygen(tmp_path):
    src = tmp_path / "keygen.c"
    src.write_text(C_KEYGEN)
    lib = tmp_path / ("keygen.dylib" if sys.platform == "darwin" else "keygen.so")
    subprocess.run(["cc", "-shared", "-fPIC", "-o", str(lib), str(src)], check=True)
    key = LibrarySeedKey(str(lib)).compute_key(SEED, 1)
    assert key == bytes(b ^ 0xA1 for b in SEED)


def test_audit_detects_tampering(tmp_path):
    log = str(tmp_path / "audit.jsonl")
    audit.append(log, {"target": "a", "success": True})
    audit.append(log, {"target": "b", "success": False})
    assert audit.verify(log)[:2] == (True, 2)
    lines = open(log).read().splitlines()
    first = json.loads(lines[0])
    first["success"] = False
    lines[0] = json.dumps(first, sort_keys=True)
    open(log, "w").write("\n".join(lines) + "\n")
    ok, _, problem = audit.verify(log)
    assert not ok and "modified" in problem


@pytest.mark.parametrize("stmin", [0x80, 0xF0, 0xFA, 0xFF])
def test_invalid_stmin_rejected(stmin):
    with pytest.raises(TransportError, match="STmin"):
        IsoTpCanTransport(CanTpConfig(stmin_rx=stmin))


@pytest.mark.parametrize("stmin", [0x00, 0x7F, 0xF1, 0xF9])
def test_valid_stmin_accepted(stmin):
    IsoTpCanTransport(CanTpConfig(stmin_rx=stmin))


def test_classic_can_needs_8_byte_frames():
    with pytest.raises(TransportError, match="tx_dl"):
        IsoTpCanTransport(CanTpConfig(tx_dl=64))


def test_project_rejects_unknown_options(project_dir):
    path = project_dir("""
    - id: a
      can: {backend: virtual, stmin: 3}
      firmware: [app.hex]
    """)
    with pytest.raises(ProjectError, match="stmin"):
        load_project(path)


def test_canfd_target_defaults_to_64_byte_frames(project_dir):
    path = project_dir("""
    - id: a
      transport: canfd
      can: {backend: virtual}
      firmware: [app.hex]
    """)
    t = load_project(path).targets[0]
    assert t.can.fd and t.can.tx_dl == 64


def test_parallel_jobs_in_separate_processes(project_dir):
    path = project_dir("""
    - id: ecu_a
      simulate: true
      can: {backend: virtual, channel: p-a}
      firmware: [app.hex]
      seed_key: {provider: python, path: seedkey.py}
    - id: ecu_b
      transport: doip
      simulate: true
      firmware: [app.hex]
      seed_key: {provider: python, path: seedkey.py}
    """)
    manager = JobManager()
    manager.start(path, ["ecu_a", "ecu_b"])
    try:
        finished = {e.job: e for e in manager.wait_all() if isinstance(e, JobFinished)}
    finally:
        manager.shutdown()
    assert set(finished) == {"ecu_a", "ecu_b"}
    assert all(e.success for e in finished.values()), {k: v.error for k, v in finished.items()}
    assert audit.verify(os.path.join(project_dir.dir, "out", "audit.jsonl"))[:2] == (True, 2)
