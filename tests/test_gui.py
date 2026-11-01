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
        assert win.table.rowCount() == 5
        assert win.fw_editor.preview.topLevelItemCount() == 2  # bcm: application + calibration
        assert "download" in win._step_items and "read_boot_version" in win._step_items
        assert win.act_flash_all.isEnabled() and not win.act_stop.isEnabled()
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
        win.table.selectRow(0)
        win._on_select()
        assert win.fw_editor.form.data["file"] == "app.hex"
        win.table.selectRow(1)  # door: firmware from the PDX
        win._on_select()
        assert win.fw_editor.form.data["file"] == "body_sw.pdx"
        assert win.fw_editor.form.widget("session").currentText() == "CAL_ONLY"
    finally:
        win.manager.shutdown()
        win.close()
