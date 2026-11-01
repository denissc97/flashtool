"""LIN targets in projects: functional steps without responses, slaves on one channel one after another."""

import json

import pytest

from flashtool.app.job import run_group, run_job
from flashtool.app.project import load_project, plan_groups
from flashtool.sequence.events import JobFinished, StepStarted

pytestmark = [pytest.mark.req("REQ-02"), pytest.mark.req("REQ-03")]

LIN_TARGET = """
- id: {id}
  transport: lin
  simulate: true
  lin: {{backend: virtual, channel: lin-1, nad: {nad}, poll_interval_ms: 1}}
  firmware: [app.hex]
  seed_key: {{provider: python, path: seedkey.py}}
"""


def test_lin_flash_with_the_standard_template(project_dir):
    path = project_dir(LIN_TARGET.format(id="seat", nad="0x0A"))
    events = []
    assert run_job(path, "seat", events.append), events[-1].error
    report = json.load(open(events[-1].report))
    assert report["transport"]["type"] == "lin" and int(str(report["transport"]["nad"]), 16) == 0x0A
    # the functional 10 03 got no response on LIN and the step still passed
    ext = next(s for s in report["steps"] if s["path"] == "ext_session")
    assert ext["status"] == "ok" and "response" not in ext["detail"]


def test_lin_slaves_on_one_channel_run_one_after_another(project_dir):
    path = project_dir(LIN_TARGET.format(id="seat", nad="0x0A") + LIN_TARGET.format(id="mirror", nad="0x0B"))
    project = load_project(path)
    assert plan_groups(project, ["seat", "mirror"]) == [["seat", "mirror"]]
    events = []
    assert run_group(path, ["seat", "mirror"], events.append) == {"seat": True, "mirror": True}
    order = [e.job for e in events if isinstance(e, (StepStarted, JobFinished))]
    first_mirror = order.index("mirror")
    assert "seat" not in order[first_mirror:]  # seat finished before mirror started
