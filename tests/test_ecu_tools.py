"""ECU tools: wake-up, keep-alive, identification read-out (simulated sleeping ECUs)."""

import time

import pytest

from flashtool.app.job import run_job
from flashtool.app.project import load_project
from flashtool.app.tools import EcuSession, ToolError

pytestmark = [pytest.mark.req("REQ-02"), pytest.mark.req("REQ-09")]

SLEEPY_CAN = """
- id: gw
  simulate: true
  can: {{backend: virtual, channel: {channel}, cyclic: [{{id: 0x500, data: "01", period_ms: 50}}]}}
  wakeup: {{frames: [{{id: 0x500, data: "01"}}], wait_ms: 20{extra}}}
  simulator: {{sleep_timeout_ms: 300, wakeup_id: 0x500}}
  firmware: [app.hex]
  seed_key: {{provider: python, path: seedkey.py}}
"""


def _session(project_dir, yaml):
    project = load_project(project_dir(yaml))
    session = EcuSession(project, project.targets[0])
    session.open()
    return session


def test_sleeping_ecu_wake_up_and_identification(project_dir):
    session = _session(project_dir, SLEEPY_CAN.format(channel="tools-1", extra=""))
    try:
        with pytest.raises(ToolError, match="awake"):
            session.read_identification()
        assert "wake-up frame" in session.wake_up()
        rows = {r.did: r for r in session.read_identification()}
        assert rows[0xF190].text == "WFLASHTOOLSIM0001" and rows[0xF190].status == "ok"
        assert rows[0xF187].text == "FT-0001-SIM"
        assert rows[0xF197].status == "not supported" and "0x31" in rows[0xF197].detail
        assert session.read_did(0xF186).raw == b"\x01"
    finally:
        session.close()


def test_keep_alive_keeps_the_ecu_awake(project_dir):
    session = _session(project_dir, SLEEPY_CAN.format(channel="tools-2", extra=""))
    try:
        session.wake_up()
        assert session.keep_alive(True) == ["0x500 [01] every 50 ms"] and session.keep_alive_running
        time.sleep(0.6)  # twice the sleep timeout
        assert session.read_did(0xF190).status == "ok"
        session.keep_alive(False)
        time.sleep(0.5)
        assert session.read_did(0xF190).status == "no response"  # fell asleep again
    finally:
        session.close()


def test_lin_wake_up(project_dir):
    session = _session(project_dir, """
    - id: seat
      transport: lin
      simulate: true
      lin: {backend: virtual, channel: tools-lin, nad: 0x0A, poll_interval_ms: 1}
      simulator: {sleep_timeout_ms: 5000}
      firmware: [app.hex]
    """)
    try:
        assert session.read_did(0xF190).status == "no response"
        assert "LIN wake-up" in session.wake_up()
        assert session.read_did(0xF190).status == "ok"
        with pytest.raises(ToolError, match="CAN"):
            session.keep_alive(True)
    finally:
        session.close()


def test_flash_wakes_the_ecu_first(project_dir):
    path = project_dir(SLEEPY_CAN.format(channel="tools-3", extra=", before_flash: true"))
    events = []
    assert run_job(path, "gw", events.append), events[-1].error
    assert any("wake-up frame" in getattr(e, "message", "") for e in events)
