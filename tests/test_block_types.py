"""Logical blocks with a type (BOOT / APP / CAL / DATA) and one block sequence per type."""

import os

import pytest

from flashtool.app.job import JobSetupError, prepare, run_job
from flashtool.app.project import load_project
from flashtool.firmware.loaders import FirmwareError, FirmwareSpec, load_firmware
from flashtool.firmware.model import normalize_block_type
from flashtool.firmware.validate import validate_image
from flashtool.sequence.expr import evaluate
from flashtool.sequence.loader import SequenceError, all_steps, block_sequence_key, flatten, load_sequence

from .conftest import DEMO_DIR, make_hex

pytestmark = [pytest.mark.req("REQ-15"), pytest.mark.req("REQ-04")]


def ids(steps):
    return [s.get("id") for _, s in flatten(steps)]


@pytest.fixture
def blocks_dir(project_dir, rand):
    d = project_dir.dir
    make_hex(d / "boot.hex", [(0x0000, rand.randbytes(4096))])
    make_hex(d / "cal.hex", [(0x80000, rand.randbytes(2048))])
    make_hex(d / "data.hex", [(0x90000, rand.randbytes(1024))])
    return project_dir


# -- firmware --------------------------------------------------------------------------------------
def test_normalize_block_type():
    assert normalize_block_type("calibration") == "CAL"
    assert normalize_block_type("CODE") == "APP" and normalize_block_type("EXE") == "APP"
    assert normalize_block_type("fbl") == "BOOT"
    assert normalize_block_type("sbl") == "SBL"  # unknown types are kept
    assert normalize_block_type("") is None and normalize_block_type(None) is None


def test_type_and_index_from_project_entry(blocks_dir):
    image = load_firmware([FirmwareSpec(file="boot.hex", type="boot"), FirmwareSpec(file="app.hex"),
                           FirmwareSpec(file="cal.hex", type="CAL", block_index=2)], str(blocks_dir.dir))
    assert [(b.type, b.index) for b in image.blocks] == [("BOOT", None), ("APP", None), ("CAL", 2)]
    assert image.types == ["BOOT", "APP", "CAL"]


def test_type_from_odx_and_vbf_files():
    pdx = load_firmware([FirmwareSpec(file="body_sw.pdx", session="FULL")], DEMO_DIR)
    assert [(b.name, b.type, b.metadata["type"]) for b in pdx.blocks] == [("APP", "APP", "CODE"),
                                                                           ("CAL", "DATA", "DATA")]
    overridden = load_firmware([FirmwareSpec(file="body_sw.pdx", session="CAL_ONLY", type="CAL")], DEMO_DIR)
    assert overridden.blocks[0].type == "CAL"
    assert load_firmware([FirmwareSpec(file="seat_sw.vbf")], DEMO_DIR).blocks[0].type == "DATA"


def test_block_index_checks(blocks_dir):
    base = str(blocks_dir.dir)
    image = load_firmware([FirmwareSpec(file="app.hex", block_index=1),
                           FirmwareSpec(file="cal.hex", block_index=1)], base)
    assert any("logical block index 1" in str(i) for i in validate_image(image))
    with pytest.raises(FirmwareError, match="block_index needs one block per file"):
        load_firmware([FirmwareSpec(file="app.hex", block_index=1, split="segments")], base)
    with pytest.raises(FirmwareError, match="need a single block"):
        load_firmware([FirmwareSpec(file="body_sw.pdx", session="FULL", block_index=1)], DEMO_DIR)


def test_of_type_expression(blocks_dir):
    image = load_firmware([FirmwareSpec(file="boot.hex", type="BOOT"), FirmwareSpec(file="app.hex"),
                           FirmwareSpec(file="cal.hex", type="CAL")], str(blocks_dir.dir))
    names = {"image": image}
    assert [b.name for b in evaluate("of_type(image.blocks, 'boot', 'CAL')", names)] == ["boot", "cal"]
    assert evaluate("'BOOT' in image.types", names) is True
    assert evaluate("image.blocks[2].type == 'CAL'", names) is True


# -- templates -------------------------------------------------------------------------------------
TEMPLATE = """
extends: uds_standard
name: typed
variables: {activate_boot_rid: 0xFF02}
block_sequences:
  BOOT:
    - {id: boot_erase, request: "31 01 FF 00 ${addr_size(0x44, block.start, block.size)}", expect_data: "71 01 FF 00 00"}
    - {id: boot_download, download: block}
    - {id: boot_check, request: "31 01 02 02 ${u32(block.crc32)}", expect_data: "71 01 02 02 00"}
    - {id: boot_activate, request: "31 01 ${u16(activate_boot_rid)}", expect_data: "71 01 FF 02 00"}
    - {id: boot_reset, reset: 0x01}
    - {id: boot_wait, wait_ecu: {timeout_ms: 3000}}
    - {id: boot_ext, session: 0x03}
    - {id: boot_prog, session: 0x02}
    - {id: boot_security, security: {level: 0x01}}
  calibration:
    - {id: cal_erase, request: "31 01 FF 00 ${u8(block.index)}", expect_data: "71 01 FF 00 00"}
    - {id: cal_download, download: block}
    - {id: cal_check, request: "31 01 02 02 ${u32(block.crc32)}", expect_data: "71 01 02 02 00"}
  DATA: []
patch:
  - update: download          # step of the inherited default block sequence
    with: {max_block_length: 258}
"""


def test_block_sequences_extend_and_patch(tmp_path):
    (tmp_path / "typed.yaml").write_text(TEMPLATE)
    seq = load_sequence("typed.yaml", str(tmp_path))
    assert list(seq["block_sequences"]) == ["default", "BOOT", "CAL", "DATA"]  # 'calibration' -> CAL
    assert next(s for _, s in flatten(seq["block_sequences"]["default"]) if s["id"] == "download")[
        "max_block_length"] == 258
    assert block_sequence_key(seq, "CAL") == "CAL" and block_sequence_key(seq, "SBL") == "default"
    assert {"boot_activate", "cal_erase", "erase_region", "tester_present_off"} <= {s.get("id") for s in all_steps(seq)}

    (tmp_path / "child.yaml").write_text("extends: typed.yaml\nblock_sequences: {DATA: null, default: null}\n")
    child = load_sequence("child.yaml", str(tmp_path))
    assert list(child["block_sequences"]) == ["BOOT", "CAL"]  # null removes an inherited block sequence
    assert block_sequence_key(child, "APP") is None


@pytest.mark.parametrize("body, message", [
    ("block_sequences: {BOOT: [{id: p, program_block: block}]}\nsteps: []", "cannot program another block"),
    ("block_sequences: {BOOT: [{id: s, session: 0x03, group: once}]}\nsteps: []", "not allowed in a block sequence"),
    ("block_sequences: {BOOT: [{id: a, log: x}], CAL: [{id: a, log: y}]}\nsteps: []", "duplicate step id 'a'"),
    ("block_sequences: [1, 2]\nsteps: []", "must map a block type"),
    ("block_sequences: {boot: [], BOOT: []}\nsteps: []", "appears twice"),
])
def test_block_sequence_validation(tmp_path, body, message):
    (tmp_path / "bad.yaml").write_text(body)
    with pytest.raises(SequenceError, match=message):
        load_sequence("bad.yaml", str(tmp_path))


# -- flashing ------------------------------------------------------------------------------------------
TARGET = """
- id: ecu
  transport: can
  simulate: true
  can: {{ backend: virtual, channel: {channel} }}
  simulator:
    logical_blocks: [[2, 0x80000, 0x10000]]
    routines: [0xFF02]
  firmware:
    - {{ file: boot.hex, type: BOOT }}
    - {{ file: app.hex }}
    - {{ file: cal.hex, type: CAL, block_index: 2 }}
    - {{ file: data.hex, type: {data_type} }}
  sequence: {sequence}
  seed_key: {{ provider: python, path: seedkey.py }}
"""


def test_each_block_runs_the_sequence_of_its_type(blocks_dir):
    (blocks_dir.dir / "typed.yaml").write_text(TEMPLATE)
    path = blocks_dir(TARGET.format(channel="t-types", sequence="typed.yaml", data_type="DATA"))
    project = load_project(path)
    image, seq, warnings = prepare(project, project.target("ecu"))
    assert any("data" in w and "is empty" in w for w in warnings)  # DATA: [] downloads nothing

    events = []
    assert run_job(path, "ecu", events.append), events[-1].error
    finished = [e for e in events if type(e).__name__ == "StepFinished" and e.status == "ok"]
    paths = [e.path for e in finished]
    assert "blocks[boot]/program[BOOT]/boot_activate" in paths
    assert "blocks[app]/program[default]/download" in paths
    assert "blocks[cal]/program[CAL]/cal_erase" in paths
    assert not any(p.startswith("blocks[data]/program[") for p in paths)
    assert "blocks[data]/program" in paths
    logs = [e.message for e in events if type(e).__name__ == "LogEvent"]
    assert "Block cal (type CAL): block sequence 'CAL'" in logs


def test_missing_block_sequence_is_found_before_connecting(blocks_dir):
    (blocks_dir.dir / "only_boot.yaml").write_text(
        "extends: uds_standard\nblock_sequences: {default: null, BOOT: [{id: b, download: block}]}\n")
    path = blocks_dir(TARGET.format(channel="t-missing", sequence="only_boot.yaml", data_type="DATA"))
    project = load_project(path)
    with pytest.raises(JobSetupError, match="block app has type APP, but the sequence has no block sequence"):
        prepare(project, project.target("ecu"))


def test_unused_block_sequences_are_reported(blocks_dir):
    (blocks_dir.dir / "flat.yaml").write_text(
        "name: flat\nsteps: [{id: l, log: hi}]\nblock_sequences: {CAL: [{id: c, log: cal}]}\n")
    path = blocks_dir(TARGET.format(channel="t-flat", sequence="flat.yaml", data_type="DATA"))
    project = load_project(path)
    _, _, warnings = prepare(project, project.target("ecu"))
    assert any("block sequences CAL are not used" in w for w in warnings)


def test_demo_bcm_has_boot_app_and_cal():
    project = load_project(os.path.join(DEMO_DIR, "demo.ftproj"))
    image, seq, _ = prepare(project, project.target("bcm"))
    assert [(b.name, b.type, block_sequence_key(seq, b.type)) for b in image.blocks] == [
        ("bootloader", "BOOT", "BOOT"), ("application", "APP", "default"), ("calibration", "CAL", "CAL")]
