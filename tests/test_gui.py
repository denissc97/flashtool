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
        assert win.fw_tree.topLevelItemCount() == 2  # bcm: application + calibration
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
