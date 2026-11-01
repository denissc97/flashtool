"""Features matching vFlash: cyclic CAN messages, custom actions, Python API."""

import json

import can
import pytest

from flashtool import api
from flashtool.app.job import run_job
from flashtool.sequence.events import JobFinished, LogEvent
from flashtool.transport.base import TransportError
from flashtool.transport.isotp_can import CanTpConfig, IsoTpCanTransport, cyclic_messages

pytestmark = [pytest.mark.req("REQ-04"), pytest.mark.req("REQ-02")]


def test_cyclic_messages_are_sent_while_connected():
    cfg = CanTpConfig(cyclic=[{"id": 0x500, "data": "01 02", "period_ms": 10}])
    cfg.hardware.channel = "cyc-1"
    listener = can.Bus(interface="virtual", channel="cyc-1")
    transport = IsoTpCanTransport(cfg)
    transport.open()
    try:
        frames = [listener.recv(0.5) for _ in range(3)]
    finally:
        transport.close()
    assert all(f is not None and f.arbitration_id == 0x500 and bytes(f.data) == b"\x01\x02" for f in frames)
    while listener.recv(0.05) is not None:  # drain what was sent before close
        pass
    assert listener.recv(0.1) is None  # stopped with the transport
    listener.shutdown()


def test_invalid_cyclic_message():
    with pytest.raises(TransportError, match="period_ms"):
        cyclic_messages([{"id": 0x10, "data": "00", "period_ms": 0}], fd=False)


ACTION = '''
def run(ctx):
    version = ctx.read_did(0xF189)
    ctx.vars["boot"] = version
    ctx.log(f"custom action read {version!r} with arg {ctx.args['tag']}")
    resp = ctx.request("22 F1 90")
    return {"vin_response": resp}
'''

TEMPLATE = """extends: uds_standard
patch:
  - insert_after: prog_session
    steps:
      - {id: backup, script: actions/backup.py, args: {tag: "${u16(0xBEEF)}"}}
"""


def _project(project_dir, allow: bool) -> str:
    (project_dir.dir / "actions").mkdir()
    (project_dir.dir / "actions" / "backup.py").write_text(ACTION)
    (project_dir.dir / "seq.yaml").write_text(TEMPLATE)
    return project_dir(f"""
    - id: ecu1
      simulate: true
      can: {{backend: virtual, channel: ca-bus, cyclic: [{{id: 0x500, data: "00", period_ms: 50}}]}}
      firmware: [app.hex]
      sequence: seq.yaml
      allow_custom_actions: {str(allow).lower()}
      seed_key: {{provider: python, path: seedkey.py}}
    """)


def test_custom_action_runs_when_allowed(project_dir):
    path = _project(project_dir, allow=True)
    events = []
    assert run_job(path, "ecu1", events.append), events[-1].error
    assert any(isinstance(e, LogEvent) and "custom action read" in e.message and "BE EF" not in e.message
               for e in events)
    report = json.load(open(events[-1].report))
    step = next(s for s in report["steps"] if s["path"] == "backup")
    assert step["status"] == "ok" and len(step["detail"]["sha256"]) == 64
    assert step["detail"]["result"]["vin_response"].startswith("62 F1 90")


def test_custom_action_blocked_without_opt_in(project_dir):
    path = _project(project_dir, allow=False)
    events = []
    assert not run_job(path, "ecu1", events.append)
    assert "allow_custom_actions" in events[-1].error


def test_python_api(project_dir):
    path = project_dir("""
    - id: a
      simulate: true
      can: {backend: virtual, channel: api-a}
      firmware: [app.hex]
      seed_key: {provider: python, path: seedkey.py}
    """)
    seen = []
    results = api.flash(path, on_event=seen.append, parallel=False)
    assert results["a"].success and results["a"].report.endswith("report.json")
    assert any(isinstance(e, JobFinished) for e in seen)
    image = api.inspect(["app.hex"], base_dir=str(project_dir.dir))
    assert image.total_size == 23000
