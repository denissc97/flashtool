import pytest

from flashtool.sequence.loader import SequenceError, flatten, load_sequence

pytestmark = [pytest.mark.req("REQ-03"), pytest.mark.req("REQ-04")]


def ids(steps):
    return [s.get("id") for _, s in flatten(steps)]


def test_builtin_standard_template():
    seq = load_sequence("uds_standard")
    assert ids(seq["steps"])[:3] == ["tester_present_on", "ext_session", "precondition_check"]
    assert "download" in ids(seq["steps"])
    assert ids(seq["finally"])[-1] == "tester_present_off"


def test_extends_and_patch(tmp_path):
    (tmp_path / "oem.yaml").write_text(
        """
extends: uds_standard
name: OEM
variables: {security_level: 0x11}
patch:
  - remove: precondition_check
  - replace: security
    with: {id: security_oem, security: {level: 0x11}}
  - insert_after: security_oem
    steps:
      - {id: fp, write_did: {did: 0xF15A, data: "01 02"}}
  - insert_before: prog_session
    steps:
      - {id: hello, log: hi}
  - update: download
    with: {transport: {stmin_rx: 0xF5}}
"""
    )
    seq = load_sequence("oem.yaml", str(tmp_path))
    order = ids(seq["steps"])
    assert "precondition_check" not in order and "security" not in order
    assert order.index("security_oem") + 1 == order.index("fp")
    assert order.index("hello") + 1 == order.index("prog_session")
    assert seq["variables"]["security_level"] == 0x11
    download = next(s for _, s in flatten(seq["steps"]) if s.get("id") == "download")
    assert download["transport"] == {"stmin_rx": 0xF5}
    assert seq["name"] == "OEM" and len(seq["sources"]) == 2


def test_patch_unknown_id(tmp_path):
    (tmp_path / "bad.yaml").write_text("extends: uds_standard\npatch:\n  - remove: nope\n")
    with pytest.raises(SequenceError, match="no step with id 'nope'"):
        load_sequence("bad.yaml", str(tmp_path))


def test_duplicate_ids(tmp_path):
    (tmp_path / "dup.yaml").write_text("steps:\n  - {id: a, log: x}\n  - {id: a, log: y}\n")
    with pytest.raises(SequenceError, match="duplicate"):
        load_sequence("dup.yaml", str(tmp_path))


def test_circular_extends(tmp_path):
    (tmp_path / "a.yaml").write_text("extends: b\n")
    (tmp_path / "b.yaml").write_text("extends: a\n")
    with pytest.raises(SequenceError, match="circular"):
        load_sequence("a", str(tmp_path))


def test_unknown_step_key(tmp_path):
    (tmp_path / "k.yaml").write_text("steps:\n  - {id: a, request: '10 01', retry: 3}\n")
    with pytest.raises(SequenceError, match="unknown key"):
        load_sequence("k.yaml", str(tmp_path))
