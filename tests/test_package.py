"""Project packages `.ftpack` (flashtool/app/package.py)."""

import json
import os
import shutil
import stat
import zipfile

import pytest

from flashtool.app.job import run_job
from flashtool.app.package import (
    MANIFEST,
    PackageError,
    check_editable,
    export_pack,
    import_pack,
    is_sealed,
    read_manifest,
    verify_pack,
)
from flashtool.cli import main
from flashtool.firmware.loaders import FirmwareSpec, load_firmware

from .conftest import DEMO_DIR, SEEDKEY_PLUGIN, make_hex
from .test_vbf import build_vbf

pytestmark = [pytest.mark.req("REQ-10")]

TEMPLATE = """# local OEM template
name: local OEM
extends: uds_standard
catalog: services.yaml
patch:
  - insert_after: prog_session
    steps:
      - {id: read_session, service: ReadActiveSession}
"""
CATALOG = 'services:\n  ReadActiveSession: "22 F1 86"\n'


def flash(project, *targets):
    for tid in targets:
        events = []
        ok = run_job(project, tid, events.append)
        assert ok, f"{tid}: {events[-1].error}"


def rewrite_zip(src, dst, edit=lambda name, data: data, add=None):
    """Copy a zip; `edit` returns new bytes or None to drop the member."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for info in zin.infolist():
            data = edit(info.filename, zin.read(info.filename))
            if data is not None:
                zout.writestr(info.filename, data)
        for name, data in (add or {}).items():
            zout.writestr(name, data)
    return str(dst)


@pytest.fixture
def local_project(project_dir, rand):
    """Two CAN targets: HEX + local template with a catalog, and a VBF file."""
    folder = project_dir.dir
    (folder / "oem.yaml").write_text(TEMPLATE)
    (folder / "services.yaml").write_text(CATALOG)
    (folder / "fw").mkdir()
    vbf = build_vbf([(0x10000, rand.randbytes(3000)), (0x30000, rand.randbytes(99))])
    (folder / "fw" / "body.vbf").write_bytes(vbf)
    return project_dir("""
    - id: hex_ecu
      simulate: true
      can: {backend: virtual, channel: t-pack-hex, tx_id: 0x7E0, rx_id: 0x7E8}
      firmware: [app.hex]
      sequence: oem.yaml
      seed_key: {provider: python, path: seedkey.py}
    - id: vbf_ecu
      simulate: true
      can: {backend: virtual, channel: t-pack-vbf}
      firmware: [{file: fw/body.vbf, name: body}]
      sequence: oem
      seed_key: {provider: python, path: seedkey.py}
    """, name="local.ftproj")


@pytest.fixture
def demo_copy(tmp_path):
    folder = tmp_path / "demo"
    shutil.copytree(DEMO_DIR, folder, ignore=shutil.ignore_patterns("out", "__pycache__"))
    return folder


# --- export / import ---------------------------------------------------------------------------------
def test_round_trip_runs_on_simulator(local_project, tmp_path):
    pack = export_pack(local_project, str(tmp_path / "out" / "local.ftpack"))
    manifest = read_manifest(pack)
    assert manifest["tool_version"] and manifest["created"] and manifest["project"] == {
        "name": "test", "file": "local.ftproj"}
    files = {e["path"]: e for e in manifest["files"]}
    assert set(files) == {"local.ftproj", "app.hex", "fw/body.vbf", "oem.yaml", "services.yaml", "seedkey.py"}
    assert files["fw/body.vbf"]["kind"] == "firmware" and files["oem.yaml"]["kind"] == "sequence"
    assert files["seedkey.py"]["targets"] == files["services.yaml"]["targets"] == ["hex_ecu", "vbf_ecu"]
    assert not manifest["sealed"] and "seal" not in manifest and manifest["outputs"] == ["out", "out/audit.jsonl"]
    with zipfile.ZipFile(pack) as z:  # nothing to rewrite: files are copied byte for byte
        assert z.read("local.ftproj") == open(local_project, "rb").read()
        assert z.read("oem.yaml").decode() == TEMPLATE
    assert verify_pack(pack) == []

    project = import_pack(pack, str(tmp_path / "unpacked"))
    assert project == str(tmp_path / "unpacked" / "local.ftproj")
    assert os.path.isfile(tmp_path / "unpacked" / MANIFEST)
    flash(project, "hex_ecu", "vbf_ecu")
    assert verify_pack(str(tmp_path / "unpacked")) == []  # reports and audit log are not edits


def test_demo_project_round_trip(demo_copy, tmp_path):
    pack = export_pack(str(demo_copy / "demo.ftproj"), str(tmp_path / "demo.ftpack"), include_private_keys=True)
    manifest = read_manifest(pack)
    kinds = {e["path"]: e["kind"] for e in manifest["files"]}
    assert kinds == {
        "demo.ftproj": "project", "boot.hex": "firmware", "app.hex": "firmware", "cal.s19": "firmware", "body_sw.pdx": "firmware",
        "gateway.bin": "firmware", "seat_sw.vbf": "firmware", "oem_demo.yaml": "sequence", "oem_cert.yaml": "sequence",
        "demo_ecu.odx-d": "catalog", "demo_catalog.yaml": "catalog", "seedkey_demo.py": "seed_key",
        "pki/tester.pem": "certificate", "pki/tester.key": "private_key", "pki/root_ca.pem": "trust_anchor",
    }
    assert manifest["private_keys_included"] and manifest["excluded"] == []
    project = import_pack(pack, str(tmp_path / "unpacked"))
    flash(project, "bcm", "door", "seat", "adas")  # CAN + OEM template + catalogs, PDX, LIN + VBF, DoIP + certs


def test_private_keys_are_left_out_by_default(demo_copy, tmp_path):
    pack = export_pack(str(demo_copy / "demo.ftproj"), str(tmp_path / "demo.ftpack"))
    with zipfile.ZipFile(pack) as z:
        assert "pki/tester.key" not in z.namelist() and "pki/tester.pem" in z.namelist()
    manifest = read_manifest(pack)
    assert not manifest["private_keys_included"]
    (excluded,) = manifest["excluded"]
    assert excluded["path"] == "pki/tester.key" and excluded["kind"] == "private_key"
    assert excluded["targets"] == ["adas"] and "private key not included" in excluded["note"]
    assert any(n.startswith("pki/tester.key: private key not included") for n in manifest["notes"])

    project = import_pack(pack, str(tmp_path / "unpacked"))
    assert "key: pki/tester.key" in open(project).read()  # reference kept: the user supplies the key
    events = []
    assert not run_job(project, "adas", events.append)
    assert "tester.key" in events[-1].error
    shutil.copy(demo_copy / "pki" / "tester.key", tmp_path / "unpacked" / "pki" / "tester.key")
    flash(project, "adas")


def test_external_files_go_under_external(tmp_path, rand):
    from .test_odx_flash import HAND_ODX_F

    shared = tmp_path / "shared"
    for sub in ("fw", "seq", "cat", "odx"):
        (shared / sub).mkdir(parents=True)
    make_hex(shared / "fw" / "app.hex", [(0x20000, rand.randbytes(4000))])
    (shared / "seq" / "base.yaml").write_text("extends: uds_standard\ncatalog: ../cat/services.yaml\n")
    (shared / "cat" / "services.yaml").write_text(CATALOG)
    (shared / "seedkey.py").write_text(open(SEEDKEY_PLUGIN).read())
    (shared / "odx" / "FL_HAND.odx-f").write_text(HAND_ODX_F)  # ODX-F with its DATAFILE next to it
    srec = __import__("bincopy").BinFile()
    srec.add_binary(rand.randbytes(256), address=0x18000)
    (shared / "odx" / "app.s19").write_text(srec.as_srec())

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "oem.yaml").write_text("extends: ../shared/seq/base.yaml\npatch:\n  - insert_after: prog_session\n"
                                   "    steps: [{id: rs, service: ReadActiveSession}]\n")
    (proj / "p.ftproj").write_text(f"""name: external
targets:
  - id: ecu1
    simulate: true
    can: {{backend: virtual, channel: t-pack-ext, tx_id: 0x7E0, rx_id: 0x7E8}}
    firmware: [../shared/fw/app.hex]
    sequence: oem.yaml
    seed_key: {{provider: python, path: {shared / 'seedkey.py'}}}
  - id: ecu2
    simulate: true
    can: {{backend: virtual, channel: t-pack-odx}}
    firmware: [{{file: {shared / 'odx' / 'FL_HAND.odx-f'}}}]
    seed_key: {{provider: python, path: ../shared/seedkey.py}}
""")
    pack = export_pack(str(proj / "p.ftproj"), str(tmp_path / "p.ftpack"))
    paths = {e["path"]: e["kind"] for e in read_manifest(pack)["files"]}
    assert paths == {
        "p.ftproj": "project", "oem.yaml": "sequence", "external/fw/app.hex": "firmware",
        "external/seq/base.yaml": "sequence", "external/cat/services.yaml": "catalog",
        "external/seedkey.py": "seed_key", "external/odx/FL_HAND.odx-f": "firmware",
        "external/odx/app.s19": "firmware-data",
    }
    with zipfile.ZipFile(pack) as z:
        project_text = z.read("p.ftproj").decode()
        assert "tx_id: 0x07E0" in project_text  # hex values stay hex when the project is rewritten
        assert "- external/fw/app.hex" in project_text and "file: external/odx/FL_HAND.odx-f" in project_text
        assert project_text.count("path: external/seedkey.py") == 2  # absolute and ../ references
        assert "extends: external/seq/base.yaml" in z.read("oem.yaml").decode()
        # relative layout between external files is kept: base.yaml is copied unchanged
        assert z.read("external/seq/base.yaml") == (shared / "seq" / "base.yaml").read_bytes()

    shared.rename(tmp_path / "shared_gone")  # the unpacked project must not use the originals
    project = import_pack(pack, str(tmp_path / "elsewhere" / "unpacked"))
    image = load_firmware([FirmwareSpec(file="external/odx/FL_HAND.odx-f")], os.path.dirname(project))
    assert [s.path for s in image.sources][1].endswith("external/odx/app.s19")
    flash(project, "ecu1", "ecu2")


def test_missing_referenced_file_is_an_error(project_dir, tmp_path):
    path = project_dir("""
    - id: ecu1
      can: {backend: virtual, channel: x}
      firmware: [app.hex]
      seed_key: {provider: python, path: missing.py}
    """)
    with pytest.raises(PackageError, match="seed_key file not found"):
        export_pack(path, str(tmp_path / "x.ftpack"))
    assert not os.path.exists(tmp_path / "x.ftpack")


# --- verification ----------------------------------------------------------------------------------
def test_tampering_is_detected(local_project, tmp_path):
    pack = export_pack(local_project, str(tmp_path / "p.ftpack"))
    changed = rewrite_zip(pack, tmp_path / "changed.ftpack",
                          lambda n, d: d.replace(b":", b";", 1) if n == "app.hex" else d)
    assert verify_pack(changed) == ["changed file: app.hex"]
    removed = rewrite_zip(pack, tmp_path / "removed.ftpack", lambda n, d: None if n == "oem.yaml" else d)
    assert verify_pack(removed) == ["missing file: oem.yaml"]
    added = rewrite_zip(pack, tmp_path / "added.ftpack", add={"extra.py": b"print('hi')"})
    assert verify_pack(added) == ["added file: extra.py"]
    unsafe = rewrite_zip(pack, tmp_path / "unsafe.ftpack", add={"../evil.py": b"x"})
    assert "unsafe path in the zip: '../evil.py'" in verify_pack(unsafe)
    assert verify_pack(str(tmp_path / "nothing.ftpack")) == [f"not found: {tmp_path / 'nothing.ftpack'}"]

    with pytest.raises(PackageError, match="changed file: app.hex"):
        import_pack(changed, str(tmp_path / "dest"))
    assert not os.path.exists(tmp_path / "dest")

    project = import_pack(pack, str(tmp_path / "unpacked"))  # an unsealed folder can be edited...
    with open(os.path.join(os.path.dirname(project), "oem.yaml"), "a") as f:
        f.write("# edited\n")
    open(os.path.join(os.path.dirname(project), "notes.txt"), "w").close()
    assert verify_pack(os.path.dirname(project)) == ["changed file: oem.yaml"]  # ...but changes are reported


def test_import_refuses_non_empty_destination(local_project, tmp_path):
    pack = export_pack(local_project, str(tmp_path / "p.ftpack"))
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "keep.txt").write_text("x")
    with pytest.raises(PackageError, match="not an empty folder"):
        import_pack(pack, str(dest))
    assert os.listdir(dest) == ["keep.txt"]
    import_pack(pack, str(tmp_path / "empty_ok"))


def test_sealed_pack(local_project, tmp_path):
    pack = export_pack(local_project, str(tmp_path / "p.ftpack"), sealed=True)
    manifest = read_manifest(pack)
    assert manifest["sealed"] is True and len(manifest["seal"]) == 64
    assert is_sealed(pack) and not is_sealed(local_project)
    assert verify_pack(pack) == []

    # the manifest itself is covered: editing a hash in it breaks the seal
    def edit_manifest(name, data):
        if name != MANIFEST:
            return data
        m = json.loads(data)
        m["files"][1]["size"] += 1
        return json.dumps(m).encode()
    assert f"{MANIFEST} was modified (seal broken)" in verify_pack(
        rewrite_zip(pack, tmp_path / "m.ftpack", edit_manifest))

    def unseal(name, data):  # flipping the flag but keeping the seal is detected too
        return json.dumps(dict(json.loads(data), sealed=False)).encode() if name == MANIFEST else data
    assert verify_pack(rewrite_zip(pack, tmp_path / "u.ftpack", unseal)) == [f"{MANIFEST} was modified (seal broken)"]

    project = import_pack(pack, str(tmp_path / "sealed"))
    folder = os.path.dirname(project)
    assert is_sealed(project) and is_sealed(folder) and is_sealed(os.path.join(folder, "fw"))
    assert is_sealed(os.path.join(folder, "oem.yaml")) and not is_sealed(str(tmp_path))
    with pytest.raises(PackageError, match="sealed pack"):
        check_editable(project)
    assert not os.stat(project).st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    with pytest.raises(PackageError, match="only be exported sealed"):
        export_pack(project, str(tmp_path / "unsealed.ftpack"))
    export_pack(project, str(tmp_path / "resealed.ftpack"), sealed=True)

    flash(project, "hex_ecu")  # running writes reports + audit log: still verifies
    assert verify_pack(folder) == []
    open(os.path.join(folder, "extra.yaml"), "w").close()
    os.chmod(os.path.join(folder, "services.yaml"), 0o644)
    os.remove(os.path.join(folder, "services.yaml"))
    assert sorted(verify_pack(folder)) == ["added file: extra.yaml", "missing file: services.yaml"]
    with pytest.raises(PackageError, match="sealed project was modified"):  # no re-sealing of edits
        export_pack(project, str(tmp_path / "resealed2.ftpack"), sealed=True)


# --- CLI ---------------------------------------------------------------------------------------------
def test_cli_pack_unpack_verify(demo_copy, tmp_path, capsys):
    out = str(tmp_path / "demo.ftpack")
    assert main(["pack", str(demo_copy / "demo.ftproj"), "-o", out, "--sealed"]) == 0
    text = capsys.readouterr().out
    assert "SEALED" in text and "note: pki/tester.key: private key not included" in text
    assert main(["verify-pack", out]) == 0
    assert "OK: 14 file(s) match the manifest" in capsys.readouterr().out
    assert main(["unpack", out, str(tmp_path / "u")]) == 0
    assert "(sealed, read-only)" in capsys.readouterr().out
    assert main(["unpack", out, str(tmp_path / "u")]) == 2
    assert "not an empty folder" in capsys.readouterr().err

    bad = rewrite_zip(out, tmp_path / "bad.ftpack", lambda n, d: None if n == "cal.s19" else d)
    assert main(["verify-pack", bad]) == 1
    assert "FAIL: missing file: cal.s19" in capsys.readouterr().out

    assert main(["pack", str(demo_copy / "demo.ftproj"), "-o", str(tmp_path / "k.ftpack"),
                 "--include-private-keys"]) == 0
    assert "pki/tester.key  (private_key" in capsys.readouterr().out
