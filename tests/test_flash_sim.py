"""End-to-end flashing against the built-in simulated ECU (CAN, CAN FD, DoIP)."""

import json
import threading

import pytest

from flashtool.app import audit
from flashtool.app.job import run_job
from flashtool.firmware.loaders import FirmwareSpec, load_firmware
from flashtool.security.seedkey import PythonSeedKey
from flashtool.sequence.engine import CancelToken, Engine
from flashtool.sequence.events import JobFinished, Progress, StepFinished
from flashtool.sequence.loader import load_sequence
from flashtool.sim.can_server import CanEcuServer
from flashtool.sim.doip_server import DoIpEcuServer
from flashtool.sim.ecu import SimConfig, SimulatedEcu
from flashtool.transport.doip import DoIpConfig, DoIpTransport
from flashtool.transport.isotp_can import CanTpConfig, IsoTpCanTransport
from flashtool.uds.client import UdsClient

from .conftest import SEEDKEY_PLUGIN

pytestmark = [pytest.mark.req("REQ-02"), pytest.mark.req("REQ-03"), pytest.mark.req("REQ-10"), pytest.mark.req("REQ-11"), pytest.mark.req("REQ-12"), pytest.mark.req("REQ-13")]


def flash(transport, image, sequence="uds_standard", **engine_kw):
    events = []
    transport.open()
    try:
        client = UdsClient(transport)
        seq = load_sequence(sequence)
        engine = Engine(job="t", client=client, transport=transport, image=image, variables=seq["variables"],
                        seedkey=PythonSeedKey(SEEDKEY_PLUGIN), emit=events.append, **engine_kw)
        ok, err = engine.run(seq)
    finally:
        transport.close()
    return ok, err, engine, events


def assert_written(ecu, image):
    for block in image.blocks:
        for seg in block.segments:
            assert ecu.read_memory(seg.address, len(seg.data)) == seg.data


@pytest.fixture
def image(project_dir):
    return load_firmware([FirmwareSpec(file="app.hex")], str(project_dir.dir))


def test_can_classic(image):
    ecu = SimulatedEcu()
    with CanEcuServer(ecu, channel="t-can"):
        cfg = CanTpConfig()
        cfg.hardware.channel = "t-can"
        ok, err, engine, events = flash(IsoTpCanTransport(cfg), image)
    assert ok, err
    assert_written(ecu, image)
    assert ecu.session == 0x01  # post-programming returned to the default session
    progress = [e for e in events if isinstance(e, Progress)]
    assert progress[-1].done == image.total_size


def test_can_fd_29bit_64_byte_frames(image):
    ecu = SimulatedEcu()
    with CanEcuServer(ecu, channel="t-fd", rx_id=0x18DA10F1, tx_id=0x18DAF110, func_id=0x18DB33F1,
                      extended_id=True, fd=True, tx_dl=64):
        cfg = CanTpConfig(tx_id=0x18DA10F1, rx_id=0x18DAF110, func_id=0x18DB33F1, extended_id=True, fd=True, tx_dl=64)
        cfg.hardware.channel = "t-fd"
        ok, err, _, _ = flash(IsoTpCanTransport(cfg), image)
    assert ok, err
    assert_written(ecu, image)


def test_doip(image):
    ecu = SimulatedEcu()
    with DoIpEcuServer(ecu, port=0) as server:
        ok, err, _, _ = flash(DoIpTransport(DoIpConfig(tcp_port=server.port)), image)
    assert ok, err
    assert_written(ecu, image)


def test_transfer_fault_retries_block_from_erase(image):
    ecu = SimulatedEcu(SimConfig(faults=[SimConfig.from_dict({"faults": [{"sid": 0x36, "nrc": 0x72, "at": 3}]}).faults[0]]))
    with CanEcuServer(ecu, channel="t-fault"):
        cfg = CanTpConfig()
        cfg.hardware.channel = "t-fault"
        ok, err, engine, _ = flash(IsoTpCanTransport(cfg), image)
    assert ok, err
    assert len(ecu.erased) == 2  # erased again on retry
    assert any(r.status == "failed" and r.path.endswith("download") for r in engine.records)
    assert_written(ecu, image)


def test_wrong_key_fails_and_still_runs_post_programming(image):
    ecu = SimulatedEcu(SimConfig(secret=0x11111111))
    with CanEcuServer(ecu, channel="t-key"):
        cfg = CanTpConfig()
        cfg.hardware.channel = "t-key"
        ok, err, engine, _ = flash(IsoTpCanTransport(cfg), image)
    assert not ok and "0x35" in err
    paths = [r.path for r in engine.records]
    assert "finally/default_session" in paths
    assert not ecu.written and ecu.session == 0x01


def test_cancel_stops_transfer(image):
    ecu = SimulatedEcu()
    cancel = threading.Event()
    with CanEcuServer(ecu, channel="t-cancel"):
        cfg = CanTpConfig()
        cfg.hardware.channel = "t-cancel"

        def emit_and_cancel(e):
            if isinstance(e, Progress):
                cancel.set()

        transport = IsoTpCanTransport(cfg)
        transport.open()
        try:
            seq = load_sequence("uds_standard")
            engine = Engine(job="t", client=UdsClient(transport), transport=transport, image=image,
                            variables=seq["variables"], seedkey=PythonSeedKey(SEEDKEY_PLUGIN),
                            emit=emit_and_cancel, cancel=CancelToken(cancel))
            ok, err = engine.run(seq)
        finally:
            transport.close()
    assert not ok and err == "cancelled by user"
    assert not ecu.written


def test_step_transport_override_is_restored(image):
    ecu = SimulatedEcu()
    with CanEcuServer(ecu, channel="t-stmin"):
        cfg = CanTpConfig(stmin_rx=5)
        cfg.hardware.channel = "t-stmin"
        transport = IsoTpCanTransport(cfg)
        seen = []
        original = transport.set_params

        def spy(**kw):
            seen.append(dict(kw))
            return original(**kw)

        transport.set_params = spy
        ok, err, _, _ = flash(transport, image, sequence=str(_oem_with_stmin(image)))
    assert ok, err
    assert seen == [{"stmin_rx": 0xF1}, {"stmin_rx": 5}]
    assert cfg.stmin_rx == 5


def _oem_with_stmin(image, _cache={}):
    import os
    import tempfile

    if "path" not in _cache:
        d = tempfile.mkdtemp()
        path = os.path.join(d, "stmin.yaml")
        with open(path, "w") as f:
            f.write("extends: uds_standard\npatch:\n  - update: download\n    with: {transport: {stmin_rx: 0xF1}}\n")
        _cache["path"] = path
    return _cache["path"]


def test_run_job_writes_report_and_audit(project_dir):
    path = project_dir("""
    - id: ecu1
      transport: can
      simulate: true
      can: {backend: virtual, channel: t-job, stmin_tx_override_ms: 0}
      firmware: [app.hex]
      seed_key: {provider: python, path: seedkey.py}
    """)
    events = []
    assert run_job(path, "ecu1", events.append)
    finished = events[-1]
    assert isinstance(finished, JobFinished) and finished.success
    report = json.load(open(finished.report))
    assert report["success"] and report["firmware"]["blocks"][0]["size"] == 23000
    assert any("deviation from ISO 15765-2" in w for w in report["warnings"])
    assert any(s["path"] == "blocks[app]/download" and s["status"] == "ok" for s in report["steps"])
    ok, count, _ = audit.verify(str(project_dir.dir / "out" / "audit.jsonl"))
    assert ok and count == 1


def test_run_job_rejects_invalid_firmware_before_connecting(project_dir):
    path = project_dir("""
    - id: ecu1
      simulate: true
      can: {backend: virtual, channel: t-bad}
      firmware: [{file: app.hex, alfid: 0x12}]
    """)
    events = []
    assert not run_job(path, "ecu1", events.append)
    assert "does not fit" in events[-1].error
    assert not any(isinstance(e, StepFinished) for e in events)
