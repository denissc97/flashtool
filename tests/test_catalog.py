import json
import os
import shutil

import pytest

from flashtool.app.job import JobSetupError, prepare, run_job
from flashtool.app.project import load_project
from flashtool.diagdb.catalog import CatalogError, load_catalog
from flashtool.sequence.events import StepFinished

from .conftest import DEMO_DIR

pytestmark = [pytest.mark.req("REQ-04")]

ODX_D = os.path.join(DEMO_DIR, "demo_ecu.odx-d")
YAML_CATALOG = os.path.join(DEMO_DIR, "demo_catalog.yaml")


def test_odx_d_catalog_encodes_requests():
    catalog = load_catalog(ODX_D)
    service = catalog.get("ReadBootVersion")
    assert service.encoder() == bytes.fromhex("22 F1 89")
    assert catalog.sources[0].endswith("#DemoECU")


def test_odx_layer_must_exist():
    with pytest.raises(CatalogError, match="no diagnostic layer 'Other'"):
        load_catalog({"file": ODX_D, "layer": "Other"})


def test_yaml_catalog_and_suggestions():
    catalog = load_catalog(YAML_CATALOG)
    assert catalog.get("Fingerprint_Write").request == "2E F1 5A ${data}"
    with pytest.raises(CatalogError, match="did you mean Fingerprint_Write"):
        catalog.get("Fingerprint_Writ")


def test_cdd_gives_a_clear_message(tmp_path):
    (tmp_path / "ecu.cdd").write_text("<CANDELA/>")
    with pytest.raises(CatalogError, match="Q-08"):
        load_catalog("ecu.cdd", str(tmp_path))


def _project(project_dir, template: str):
    for f in (ODX_D, YAML_CATALOG):
        shutil.copy(f, project_dir.dir)
    (project_dir.dir / "seq.yaml").write_text(template)
    return project_dir("""
    - id: ecu1
      simulate: true
      can: {backend: virtual, channel: cat-bus}
      firmware: [app.hex]
      sequence: seq.yaml
      seed_key: {provider: python, path: seedkey.py}
    """)


def test_named_services_in_a_flash(project_dir):
    path = _project(project_dir, """extends: uds_standard
catalog: [demo_ecu.odx-d, demo_catalog.yaml]
patch:
  - insert_after: security
    steps:
      - {id: version, service: ReadBootVersion, store: version, expect_data: "62 F1 89"}
      - {id: fp, service: Fingerprint_Write, params: {data: "${u16(0xBEEF)}"}}
""")
    events = []
    assert run_job(path, "ecu1", events.append), events[-1].error
    report = json.load(open(events[-1].report))
    fp = next(s for s in report["steps"] if s["path"] == "fp")
    assert fp["detail"]["request"] == "2E F1 5A BE EF"
    assert any(c.endswith("demo_catalog.yaml") for c in report["sequence"]["catalogs"])
    assert [e.status for e in events if isinstance(e, StepFinished) and e.path == "version"] == ["ok"]


def test_unknown_service_fails_before_connecting(project_dir):
    path = _project(project_dir, """extends: uds_standard
catalog: demo_catalog.yaml
patch:
  - append: {id: x, service: DoesNotExist}
""")
    project = load_project(path)
    with pytest.raises(JobSetupError, match="DoesNotExist"):
        prepare(project, project.targets[0])
