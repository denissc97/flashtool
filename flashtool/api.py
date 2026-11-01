"""Python API for automation (CI/CD pipelines, test benches): the same jobs as the GUI and the CLI.

    from flashtool import api

    results = api.flash("project.ftproj", on_event=print)
    assert all(r.success for r in results.values())
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .app.job import run_group
from .app.manager import JobManager
from .app.project import Project, load_project, plan_groups
from .firmware.loaders import FirmwareSpec, load_firmware
from .firmware.model import FirmwareImage
from .sequence.events import Event, JobFinished

__all__ = ["Result", "flash", "load_project", "inspect", "Project", "FirmwareImage", "Event"]


@dataclass
class Result:
    target: str
    success: bool
    error: str | None
    report: str | None  # path of report.json (report.html next to it)
    duration_s: float


def flash(project_path: str, targets: list[str] | None = None, *,
          on_event: Callable[[Event], None] | None = None, parallel: bool = True) -> dict[str, Result]:
    """Flash the targets of a project (default: all) and wait for the results.

    parallel=True runs one process per bus, like the GUI; parallel=False runs the bus groups
    one after another in this process (simpler to debug).
    """
    project = load_project(project_path)
    ids = targets or [t.id for t in project.targets]
    for tid in ids:
        project.target(tid)  # unknown ids fail here
    results: dict[str, Result] = {}

    def handle(e: Event) -> None:
        if isinstance(e, JobFinished):
            results[e.job] = Result(e.job, e.success, e.error, e.report, e.duration_s)
        if on_event is not None:
            on_event(e)

    if parallel:
        manager = JobManager()
        manager.start(project.path, ids)
        try:
            for e in manager.wait_all():
                handle(e)
        finally:
            manager.shutdown()
    else:
        for group in plan_groups(project, ids):
            run_group(project.path, group, handle)
    return results


def inspect(files: list[str], base_address: int | None = None, base_dir: str = ".") -> FirmwareImage:
    """Load firmware files the way a flash job would (segments, blocks, hashes)."""
    return load_firmware([FirmwareSpec(file=f, base_address=base_address) for f in files], base_dir)
