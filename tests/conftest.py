import json
import os
import random
import textwrap

import bincopy
import pytest

DEMO_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "examples", "demo")
SEEDKEY_PLUGIN = os.path.join(DEMO_DIR, "seedkey_demo.py")


def make_hex(path, segments):
    bf = bincopy.BinFile()
    for address, data in segments:
        bf.add_binary(data, address=address)
    with open(path, "w") as f:
        f.write(bf.as_ihex())
    return path


@pytest.fixture
def rand():
    return random.Random(1234)


@pytest.fixture
def project_dir(tmp_path, rand):
    """A project folder with firmware, the demo seed&key plugin and a project writer."""
    make_hex(tmp_path / "app.hex", [(0x10000, rand.randbytes(20000)), (0x20000, rand.randbytes(3000))])
    (tmp_path / "seedkey.py").write_text(open(SEEDKEY_PLUGIN).read())

    def write(targets_yaml: str, name: str = "test.ftproj") -> str:
        path = tmp_path / name
        path.write_text("name: test\noutput_dir: out\ntargets:\n" + textwrap.indent(textwrap.dedent(targets_yaml), "  "))
        return str(path)

    write.dir = tmp_path
    return write


# -- requirement traceability (tools/traceability.py) -----------------------
_results: list[dict] = []


def pytest_addoption(parser):
    parser.addoption("--trace-json", help="write requirement -> test results to this JSON file")


def pytest_runtest_logreport(report):
    if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
        reqs = sorted({r for m in getattr(report, "req_markers", []) for r in m})
        _results.append({"test": report.nodeid, "outcome": report.outcome, "reqs": reqs})


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    outcome.get_result().req_markers = [m.args for m in item.iter_markers("req")]


def pytest_sessionfinish(session):
    path = session.config.getoption("--trace-json")
    if path:
        with open(path, "w") as f:
            json.dump(_results, f, indent=1)
