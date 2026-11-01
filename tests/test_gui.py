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
        assert win.fw_editor.preview.topLevelItemCount() == 3  # bcm: bootloader + application + calibration
        assert "download" in win._step_items and "read_boot_version" in win._step_items
        assert "boot_activate" in win._step_items and "cal_erase" in win._step_items  # block sequences
        assert [win.cards["bcm"].fw_rows.itemAt(i).layout().itemAt(1).widget().text() for i in range(3)] \
            == ["BOOT #0", "APP #1", "CAL #2"]  # block type and logical block index
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
        assert win.fw_editor.form.data["file"] == "boot.hex"
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
        assert entry == {"file": str(new), "name": "application", "type": "APP"}  # outside the project: absolute path
        assert win.cards["ecu1"].combos[0].currentData() == str(new)
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


# -- one-click release switching -----------------------------------------------------------
def test_release_candidates_newest_first_and_pattern(tmp_path):
    import time

    from flashtool.gui.project_editor import newer_release, release_candidates

    old = make_hex(tmp_path / "app_v1.hex", [(0x1000, b"\x01" * 16)])
    time.sleep(0.02)
    make_hex(tmp_path / "other.hex", [(0x1000, b"\x02" * 16)])
    time.sleep(0.02)
    new = make_hex(tmp_path / "app_v2.hex", [(0x1000, b"\x03" * 16)])
    (tmp_path / "notes.txt").write_text("not firmware")
    entry = {"file": "app_v1.hex"}
    assert [os.path.basename(p) for p in release_candidates(entry, str(tmp_path))] == ["app_v2.hex", "other.hex",
                                                                                      "app_v1.hex"]
    entry["release_pattern"] = "app_*.hex"
    assert [os.path.basename(p) for p in release_candidates(entry, str(tmp_path))] == ["app_v2.hex", "app_v1.hex"]
    assert newer_release(entry, str(tmp_path)) == str(new)
    assert newer_release({"file": str(new), "release_pattern": "app_*.hex"}, str(tmp_path)) is None
    assert old

    # a file named after another block type is not offered as a release of this block
    time.sleep(0.02)
    make_hex(tmp_path / "boot_v9.hex", [(0x0, b"\x04" * 16)])
    assert "boot_v9.hex" not in [os.path.basename(p) for p in release_candidates({"file": "app_v1.hex"}, str(tmp_path))]
    assert newer_release({"file": "app_v2.hex"}, str(tmp_path)) is None
    boot = [os.path.basename(p) for p in release_candidates({"file": "fw.hex", "type": "BOOT"}, str(tmp_path))]
    assert boot[0] == "boot_v9.hex" and "app_v2.hex" not in boot and "other.hex" in boot


def test_card_switches_release_in_one_click(app, tmp_path):
    import time

    win = MainWindow()
    try:
        _project_with_ecu(win, tmp_path)
        card = win.cards["ecu1"]
        assert not card.latest_buttons[0].isVisibleTo(card)  # app.hex is the newest file in the folder
        time.sleep(0.02)
        v2 = make_hex(tmp_path / "proj" / "app_v2.hex", [(0x10000, b"\x55" * 4096)])
        win._refresh_firmware_rows("ecu1")
        card = win.cards["ecu1"]
        assert card.latest_buttons[0].isVisibleTo(card)
        card.latest_buttons[0].click()  # one click: take the newest build
        assert win.raw["targets"][0]["firmware"][0] == {"file": "app_v2.hex", "name": "application", "type": "APP"}
        card = win.cards["ecu1"]
        assert card.combos[0].currentData() == str(v2) and not card.latest_buttons[0].isVisibleTo(card)
        labels = [card.combos[0].itemText(i) for i in range(card.combos[0].count())]
        assert labels[0].startswith("app_v2.hex") and labels[-1] == "Browse…"
        back = card.combos[0].findData(str(tmp_path / "proj" / "app.hex"))
        card.combos[0].activated.emit(back)  # pick the previous build from the list
        assert win.raw["targets"][0]["firmware"][0]["file"] == "app.hex"
        assert win.dirty and win.save_project()
    finally:
        win.manager.shutdown()
        win.dirty = False
        win.close()


# -- ECU tools page --------------------------------------------------------------------
def _wait(app, cond, timeout=10.0):
    import time

    end = time.time() + timeout
    while not cond() and time.time() < end:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    return cond()


def test_ecu_tools_page(app):
    win = MainWindow(os.path.join(DEMO_DIR, "demo.ftproj"))
    tools = win.tools
    try:
        win.show_page("tools")
        tools.ecu.setCurrentIndex(tools.ecu.findData("gateway"))  # simulated gateway sleeps until woken up
        assert "0x500" in tools.wake_info.text() and "before every flash" in tools.wake_info.text()
        tools.toggle_connection()
        assert _wait(app, lambda: tools.session is not None and not tools._busy)
        tools.wake_btn.click()
        assert _wait(app, lambda: not tools._busy)
        tools.keep_btn.click()
        assert _wait(app, lambda: not tools._busy) and tools.session.keep_alive_running
        tools.read_btn.click()
        assert _wait(app, lambda: not tools._busy and tools.table.rowCount() > 0)
        values = {tools.table.item(r, 0).text(): tools.table.item(r, 2).text() for r in range(tools.table.rowCount())}
        assert values["F190"] == "WFLASHTOOLSIM0001" and values["F195"] == "1.4.2"
        tools.did_edit.setText("F18C")
        tools.read_one()
        assert _wait(app, lambda: not tools._busy and tools.table.item(tools.table.rowCount() - 1, 0).text() == "F18C")
        tools.release(["gateway"])  # flashing the gateway frees the connection
        assert tools.session is None and tools.state.text() == "Disconnected"
    finally:
        tools.shutdown()
        win.manager.shutdown()
        win.close()


# -- block types and block sequences -------------------------------------------------------------
@pytest.mark.req("REQ-15")
def test_sequence_editor_block_sequences(app, tmp_path):
    seq = SequenceEditor(str(tmp_path))  # starts from the standard sequence: default block sequence only
    try:
        titles = [seq.tree.topLevelItem(i).text(0) for i in range(seq.tree.topLevelItemCount())]
        assert titles[1] == "Block sequence default"

        cal = seq.add_block_sequence("calibration")  # copy of the default one, ids made unique
        assert cal.text(0) == "Block sequence CAL"
        assert [cal.child(i).text(0) for i in range(cal.childCount())] == ["cal_erase", "cal_download",
                                                                           "cal_check_memory"]
        assert seq.step_area.widget() is seq._block_panel and "type CAL" in seq._block_info.text()
        boot = seq.add_block_sequence("BOOT", copy_default=False)
        titles = [seq.tree.topLevelItem(i).text(0) for i in range(seq.tree.topLevelItemCount())]
        assert titles[1:4] == ["Block sequence default", "Block sequence BOOT", "Block sequence CAL"]

        seq.tree.setCurrentItem(boot)
        seq.add_step("reset")  # steps added to the selected block sequence
        erase = cal.child(0)
        seq.tree.setCurrentItem(erase)
        _set(seq._forms["foreach"], "in", "[(block.index, 1)]")
        data = seq.collect()
        assert list(data["block_sequences"]) == ["default", "BOOT", "CAL"]
        assert data["block_sequences"]["BOOT"] == [{"id": "reset_1", "reset": 0x01}]
        assert seq.validate()

        seq.path = str(tmp_path / "typed.yaml")
        assert seq.save()
        loaded = load_sequence("typed.yaml", str(tmp_path))
        assert loaded["block_sequences"]["CAL"][0]["in"] == "[(block.index, 1)]"

        seq.remove_block_sequence(boot, confirm=False)
        assert "BOOT" not in seq.collect()["block_sequences"]

        preset = seq.add_preset("Program every block by its type")  # 'program' exists: renamed
        assert preset.child(0).text(0) == "program_2"
    finally:
        seq.dirty = False
        seq.close()


@pytest.mark.req("REQ-15")
def test_block_type_proposed_from_file_name(app, tmp_path):
    from flashtool.gui.project_editor import entry_block_types, guess_block_type, put_release_file

    assert [guess_block_type(n) for n in ("FBL_v3.hex", "cal.s19", "my_data.bin", "ecu_app.hex", "calendar.hex")] \
        == ["BOOT", "CAL", "DATA", "APP", None]
    make_hex(tmp_path / "boot_v1.hex", [(0, b"\x01" * 16)])
    entries: list = []
    put_release_file(entries, str(tmp_path / "boot_v1.hex"), str(tmp_path), None, lambda *_: None)
    assert entries == [{"file": "boot_v1.hex", "type": "BOOT"}]
    assert entry_block_types({"file": "body_sw.pdx", "session": "FULL"}, DEMO_DIR) == ["APP", "DATA"]
    assert entry_block_types({"file": "seat_sw.vbf", "type": "cal"}, DEMO_DIR) == ["CAL"]


@pytest.mark.req("REQ-15")
def test_logical_block_index_per_block(app, tmp_path):
    import shutil

    from flashtool.gui.forms import parse, to_text
    from flashtool.gui.project_editor import FirmwareEditor, entry_blocks

    assert parse("2", "numbers") == 2 and parse("0x03", "numbers") == 3
    assert parse("1, -, 4", "numbers") == [1, None, 4] and to_text([1, None, 4], "numbers") == "1, -, 4"
    with pytest.raises(ValueError):
        parse("-2", "numbers")

    shutil.copy(os.path.join(DEMO_DIR, "body_sw.pdx"), tmp_path / "body_sw.pdx")
    make_hex(tmp_path / "cal.hex", [(0x90000, b"\x07" * 64)])
    editor = FirmwareEditor(lambda: str(tmp_path))
    entries = [{"file": "body_sw.pdx", "session": "FULL"}, {"file": "cal.hex", "type": "CAL"}]
    editor.set_entries(entries)
    assert editor.preview.topLevelItemCount() == 3  # APP + CAL from the PDX, cal.hex

    # double-click in 'Logical block' and type the index: written per block of the entry
    def type_index(row, text):
        item = editor.preview.topLevelItem(row)
        item.setText(2, text)

    type_index(1, "2")  # PDX block CAL
    assert entries[0]["block_index"] == [None, 2]
    app.processEvents()
    type_index(0, "0x01")
    assert entries[0]["block_index"] == [1, 2]
    app.processEvents()
    type_index(2, "5")  # single block: a plain number
    assert entries[1]["block_index"] == 5
    app.processEvents()
    assert [editor.preview.topLevelItem(i).text(2) for i in range(3)] == ["1", "2", "5"]
    assert editor.form.data is entries[1] and editor.form.widget("block_index").text() == "5"
    assert entry_blocks(entries[0], str(tmp_path)) == [("APP", 1), ("DATA", 2)]
    assert editor.table.item(0, 2).text() == "APP #1, DATA #2 (from file)"

    type_index(2, "abc")  # rejected, the stored value stays
    assert entries[1]["block_index"] == 5 and "not a logical block index" in editor.status.text()
    app.processEvents()
    type_index(0, "")
    type_index(1, "-")
    app.processEvents()
    assert "block_index" not in entries[0]  # nothing set: back to the file's values
    editor.deleteLater()
    app.processEvents()
