import json
import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from flashtool.gui.main_window import MainWindow, _step_id  # noqa: E402

from .conftest import DEMO_DIR  # noqa: E402

pytestmark = [pytest.mark.req("REQ-09")]


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_opens_demo_project(app):
    win = MainWindow(os.path.join(DEMO_DIR, "demo.ftproj"))
    try:
        assert len(win.cards) == 5 and not win.empty.isVisibleTo(win)
        assert win.fw_editor.preview.topLevelItemCount() == 2  # bcm: application + calibration
        assert "download" in win._step_items and "read_boot_version" in win._step_items
        assert win.flash_all_btn.isEnabled() and not win.stop_btn.isEnabled()
    finally:
        win.manager.shutdown()
        win.close()


def test_step_id_from_event_path():
    assert _step_id("blocks[app]/erase[0]/erase_region") == ("erase_region", "app / 0")
    assert _step_id("finally/comm_on") == ("comm_on", "")


def test_template_editor_validates(app):
    win = MainWindow(os.path.join(DEMO_DIR, "demo.ftproj"))
    try:
        assert "extends: uds_standard" in win.tpl_edit.toPlainText()
        assert win._validate_template()
        win.tpl_edit.setPlainText("extends: uds_standard\npatch:\n  - remove: no_such_step\n")
        assert not win._validate_template() and "no_such_step" in win.tpl_status.text()
        assert not [f for f in os.listdir(DEMO_DIR) if f.endswith(".check.yaml")]
    finally:
        win.manager.shutdown()
        win.close()


# -- visual editors ---------------------------------------------------------------
from flashtool.app.job import run_job  # noqa: E402
from flashtool.app.project import load_project  # noqa: E402
from flashtool.gui.sequence_editor import SequenceEditor, _body  # noqa: E402
from flashtool.sequence.loader import load_sequence  # noqa: E402

from .conftest import SEEDKEY_PLUGIN, make_hex  # noqa: E402


def _set(form, path, text):
    """Type into a form field like a user would."""
    from PySide6.QtWidgets import QComboBox, QLineEdit

    w = form.widget(path)
    if isinstance(w, QComboBox):
        w.setCurrentText(text)
        if w.isEditable():
            w.lineEdit().editingFinished.emit()
    else:
        edit = w if isinstance(w, QLineEdit) else w.findChild(QLineEdit)
        edit.setText(text)
        edit.editingFinished.emit()


def test_build_project_and_sequence_visually(app, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    make_hex(tmp_path / "app.hex", [(0x10000, bytes(range(256)) * 40)])
    (tmp_path / "seedkey.py").write_text(open(SEEDKEY_PLUGIN).read())
    project_path = str(tmp_path / "visual.ftproj")

    win = MainWindow()
    try:
        win.create_project(project_path)
        win.add_target("can")
        win.fw_editor.add_files([str(tmp_path / "app.hex")])
        form = win.target_editor.form
        _set(form, "name", "Visual ECU")
        _set(form, "can.tx_id", "0x7E3")
        _set(form, "seed_key.provider", "python")
        _set(form, "seed_key.path", "seedkey.py")
        assert win.dirty and win.save_project()

        # the file keeps CAN ids in hex and loads as a normal project
        text = open(project_path).read()
        assert "tx_id: 0x07E3" in text and "file: app.hex" in text
        target = load_project(project_path).targets[0]
        assert target.name == "Visual ECU" and target.can.tx_id == 0x7E3

        # sequence: start from the standard one, add a log step after prog_session
        seq = SequenceEditor(str(tmp_path))
        top = seq.tree.topLevelItem(0)
        prog = next(top.child(i) for i in range(top.childCount()) if top.child(i).text(0) == "prog_session")
        seq.tree.setCurrentItem(prog)
        item = seq.add_step("log")
        _set(seq._forms["log"], "log", "built in the visual editor")
        assert item.text(2) == "log: built in the visual editor"
        seq.move(-1)
        seq.move(1)
        assert seq.validate()
        seq.path = str(tmp_path / "visual_seq.yaml")
        assert seq.save()
        steps = [s["id"] for s in load_sequence("visual_seq.yaml", str(tmp_path))["steps"]]
        assert steps.index("log_1") == steps.index("prog_session") + 1

        # the main window offers the saved sequence for the ECU (answered yes above)
        win._sequence_saved("ecu1", seq.path)
        assert win.save_project()
        assert load_project(project_path).targets[0].sequence == "visual_seq.yaml"
        seq.dirty = False
        seq.close()

        events = []
        assert run_job(project_path, "ecu1", events.append), events[-1].error
        assert any(getattr(e, "message", "") == "built in the visual editor" for e in events)
    finally:
        win.manager.shutdown()
        win.dirty = False
        win.close()


def test_pdx_sessions_offered_in_firmware_editor(app, tmp_path):
    import shutil

    shutil.copy(os.path.join(DEMO_DIR, "body_sw.pdx"), tmp_path)
    win = MainWindow()
    try:
        win.create_project(str(tmp_path / "p.ftproj"))
        win.add_target("can")
        win.fw_editor.add_files([str(tmp_path / "body_sw.pdx")])
        combo = win.fw_editor.form.widget("session")
        assert [combo.itemText(i) for i in range(combo.count())] == ["", "FULL", "CAL_ONLY"]
        assert "several" in win.fw_editor.status.text() or "session" in win.fw_editor.status.text().lower()
        _set(win.fw_editor.form, "session", "CAL_ONLY")
        assert win.fw_editor.preview.topLevelItemCount() == 1
    finally:
        win.manager.shutdown()
        win.dirty = False
        win.close()


def test_invalid_step_value_is_rejected(app, tmp_path):
    seq = SequenceEditor(str(tmp_path))
    try:
        item = seq.add_step("session")
        _set(seq._forms["session"], "session", "0xZZ")
        assert _body(item)["session"] == 0x03  # unchanged
        _set(seq._forms["session"], "session", "0x02")
        assert _body(item)["session"] == 0x02
    finally:
        seq.dirty = False
        seq.close()


def test_firmware_form_follows_the_selected_ecu(app):
    win = MainWindow(os.path.join(DEMO_DIR, "demo.ftproj"))
    try:
        win.select_target("bcm")
        assert win.fw_editor.form.data["file"] == "app.hex"
        win.select_target("door")  # firmware from the PDX
        assert win.fw_editor.form.data["file"] == "body_sw.pdx"
        assert win.fw_editor.form.widget("session").currentText() == "CAL_ONLY"
    finally:
        win.manager.shutdown()
        win.close()


# -- new release files ---------------------------------------------------------------
def _project_with_ecu(win, tmp_path):
    (tmp_path / "proj").mkdir()
    make_hex(tmp_path / "proj" / "app.hex", [(0x10000, b"\x11" * 4096)])
    (tmp_path / "proj" / "seedkey.py").write_text(open(SEEDKEY_PLUGIN).read())
    path = str(tmp_path / "proj" / "r.ftproj")
    win.create_project(path)
    win.add_target("can")
    win.fw_editor.add_files([str(tmp_path / "proj" / "app.hex")])
    _set(win.fw_editor.form, "name", "application")
    _set(win.target_editor.form, "seed_key.provider", "python")
    _set(win.target_editor.form, "seed_key.path", "seedkey.py")
    assert win.save_project()
    return path


def test_new_release_replaces_the_file_and_keeps_its_options(app, tmp_path):
    import hashlib

    win = MainWindow()
    try:
        path = _project_with_ecu(win, tmp_path)
        (tmp_path / "releases").mkdir()
        new = make_hex(tmp_path / "releases" / "app_v2.hex", [(0x10000, b"\x22" * 8192)])
        win.cards["ecu1"].files_dropped.emit("ecu1", [str(new)])  # dropped from the file manager on the card
        entry = win.raw["targets"][0]["firmware"][0]
        assert entry == {"file": str(new), "name": "application"}  # outside the project: absolute path
        assert "app_v2.hex" in win.cards["ecu1"].files.text()
        assert "app_v2.hex" in win.fw_editor.file_info.text() and "SHA-256" in win.fw_editor.file_info.text()
        assert win.dirty and win.save_project()

        events = []
        assert run_job(path, "ecu1", events.append), events[-1].error
        report = json.load(open(events[-1].report))
        assert report["firmware"]["sources"][0]["sha256"] == hashlib.sha256(open(new, "rb").read()).hexdigest()
        assert report["firmware"]["blocks"][0]["name"] == "application"
    finally:
        win.manager.shutdown()
        win.dirty = False
        win.close()


def test_release_pdx_without_the_previous_session_asks(app, tmp_path, monkeypatch):
    import shutil

    from PySide6.QtWidgets import QInputDialog

    asked = []
    monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **k: asked.append(a[3]) or ("FULL", True))
    shutil.copy(os.path.join(DEMO_DIR, "body_sw.pdx"), tmp_path)
    entries = [{"file": "old.pdx", "session": "RELEASE_1", "dfi": 0}]
    from flashtool.gui.project_editor import put_release_file

    put_release_file(entries, str(tmp_path / "body_sw.pdx"), str(tmp_path), 0, lambda n, s: asked.append(s) or "FULL")
    assert entries == [{"file": "body_sw.pdx", "session": "FULL", "dfi": 0}]
    assert asked == [["FULL", "CAL_ONLY"]]


def test_release_for_an_ecu_with_several_files_asks_which_one(app, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    win = MainWindow()
    try:
        _project_with_ecu(win, tmp_path)
        make_hex(tmp_path / "proj" / "cal.hex", [(0x80000, b"\x33" * 256)])
        win.fw_editor.add_files([str(tmp_path / "proj" / "cal.hex")])
        new_cal = make_hex(tmp_path / "proj" / "cal_v2.hex", [(0x80000, b"\x44" * 256)])
        monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **k: ("Replace cal.hex", True))
        win.put_release("ecu1", [str(new_cal)])
        assert [e["file"] for e in win.raw["targets"][0]["firmware"]] == ["app.hex", "cal_v2.hex"]
    finally:
        win.manager.shutdown()
        win.dirty = False
        win.close()


# -- modern UI ---------------------------------------------------------------------
def test_theme_toggle_and_empty_project(app, tmp_path):
    from flashtool.gui.theme import tokens

    win = MainWindow()
    try:
        win.create_project(str(tmp_path / "empty.ftproj"))
        assert not win.cards and win.empty.isVisibleTo(win) and not win.flash_all_btn.isEnabled()
        before = tokens()["bg"]
        win.toggle_theme()
        assert tokens()["bg"] != before
        win.toggle_theme()
        assert tokens()["bg"] == before
    finally:
        win.manager.shutdown()
        win.close()


def test_reports_page_shows_runs_and_timing(app, tmp_path):
    win = MainWindow()
    try:
        path = _project_with_ecu(win, tmp_path)
        assert run_job(path, "ecu1", lambda e: None)
        win.show_page("reports")
        assert win.runs.rowCount() == 1 and win.runs.item(0, 2).text() == "PASSED"
        win.runs.selectRow(0)
        labels = [label for label, _, _ in win.chart.rows]
        assert "blocks" in labels and any(label.startswith("end:") for label in labels)
        assert "KB/s" in win.run_info.text()
    finally:
        win.manager.shutdown()
        win.dirty = False
        win.close()


def test_sealed_package_opens_read_only(app, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from flashtool.app.package import export_pack, import_pack

    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[1]))
    win = MainWindow()
    try:
        path = _project_with_ecu(win, tmp_path)
        pack = export_pack(path, str(tmp_path / "release.ftpack"), sealed=True)
        project_file = import_pack(pack, str(tmp_path / "unpacked"))
        win.open_project(project_file)
        assert win.sealed and not win.side_buttons["save"].isEnabled()
        assert not win.target_editor.isEnabled() and not win.cards["ecu1"].release_btn.isVisibleTo(win)
        win.put_release("ecu1", [str(tmp_path / "proj" / "app.hex")])
        assert shown == ["Sealed package"] and not win.dirty
        assert win.flash_all_btn.isEnabled()  # flashing stays possible
    finally:
        win.manager.shutdown()
        win.close()
