"""Main window (PySide6). Talks only to the application layer (JobManager, project, prepare)."""

from __future__ import annotations

import os
import re
import sys
from collections import deque
from dataclasses import dataclass, field

from PySide6.QtCore import QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QBrush, QColor, QDesktopServices, QFont, QKeySequence, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFileDialog, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QMainWindow,
    QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
    QTabWidget, QToolBar, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .. import __version__
from ..app.job import JobSetupError, prepare
from ..app.manager import JobManager
from ..app.project import Project, ProjectError, load_project
from ..app.project_io import load_yaml, new_project, new_target, project_from_data, save_project
from ..hal.backends import list_channels
from ..sequence import events as ev
from ..sequence.loader import SequenceError, describe_step, flatten, is_builtin, load_sequence, template_path
from .project_editor import DropTable, FirmwareEditor, TargetEditor, put_release_file
from .sequence_editor import SequenceEditor

STATUS_COLORS = {
    "ok": "#2f7d5b", "failed": "#b42318", "warning": "#b7791f", "skipped": "#8e9aa6", "running": "#3b5b92",
    "shared": "#2b7a8c",
}
TRACE_LINES = 20000


@dataclass
class TargetState:
    status: str = "idle"
    done: int = 0
    total: int = 0
    speed: float = 0.0
    current: str = ""
    report: str | None = None
    error: str | None = None
    steps: dict[str, tuple[str, str]] = field(default_factory=dict)  # step id -> (status, info)
    trace: deque = field(default_factory=lambda: deque(maxlen=TRACE_LINES))


def _step_id(path: str) -> tuple[str, str]:
    """'blocks[app]/erase[0]/erase_region' -> ('erase_region', 'app / 0')."""
    items = re.findall(r"\[([^\]]+)\]", path)
    last = re.sub(r"\[[^\]]*\]", "", path).split("/")[-1]
    return last, " / ".join(items)


class MainWindow(QMainWindow):
    COLS = ("ECU", "Transport", "Firmware", "Status", "Progress", "Speed", "Current step")
    C_ECU, C_BUS, C_FW, C_STATUS, C_PROGRESS, C_SPEED, C_STEP = range(7)

    def __init__(self, project_path: str | None = None):
        super().__init__()
        self.setWindowTitle(f"FlashTool {__version__}")
        self.resize(1280, 800)
        self.settings = QSettings("FlashTool", "FlashTool")
        self.project: Project | None = None
        self.raw: dict | None = None  # project file content, edited by the visual editors
        self.project_path: str | None = None
        self.dirty = False
        self._seq_editor: SequenceEditor | None = None
        self.manager = JobManager()
        self.states: dict[str, TargetState] = {}
        self._bars: dict[str, QProgressBar] = {}
        self._step_items: dict[str, QTreeWidgetItem] = {}
        self._pending_trace: list[tuple[str, str]] = []

        self._build_toolbar()
        self._build_body()
        self.statusBar().showMessage("Open a project (.ftproj) to start")

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._pump_events)
        self.timer.start(50)
        self._apply_timer = QTimer(self, singleShot=True, interval=300)
        self._apply_timer.timeout.connect(self._apply_edits)

        path = project_path or self.settings.value("last_project")
        if path and os.path.isfile(str(path)):
            self.open_project(str(path))

    # -- layout ------------------------------------------------------------
    def _build_toolbar(self) -> None:
        tb = QToolBar("Main")
        tb.setMovable(False)
        self.addToolBar(tb)

        def action(text, slot, shortcut=None, tip=None):
            a = QAction(text, self)
            a.triggered.connect(slot)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            if tip:
                a.setToolTip(tip)
            tb.addAction(a)
            return a

        self.act_new = action("New project…", self._new_project, "Ctrl+N")
        self.act_open = action("Open project…", self._choose_project, "Ctrl+O")
        self.act_save = action("Save project", self.save_project, "Ctrl+S")
        self.act_reload = action("Reload", self._reload, "Ctrl+R", "Re-read the project and firmware files")
        tb.addSeparator()
        add_menu = QMenu(self)
        for bus, label in (("can", "CAN"), ("canfd", "CAN FD"), ("lin", "LIN"), ("doip", "DoIP (Ethernet)")):
            add_menu.addAction(f"ECU on {label}", lambda b=bus: self.add_target(b))
        self.act_add = QAction("Add ECU", self)
        self.act_add.setMenu(add_menu)
        self.act_add.triggered.connect(lambda: self.add_target("can"))
        tb.addAction(self.act_add)
        self.act_remove = action("Remove ECU", self.remove_target)
        self.act_release = action("Load release file…", self.load_release, "Ctrl+L",
                                  "New .hex / .s19 / .bin / .pdx for the selected ECU (also: drop a file on its row)")
        self.act_seq = action("Sequence editor…", self.open_sequence_editor, "Ctrl+E",
                              "Build the selected ECU's programming sequence visually")
        tb.addSeparator()
        self.act_flash_all = action("Flash all", self._flash_all, "Ctrl+Shift+F", "Flash every target in parallel")
        self.act_flash_sel = action("Flash selected", self._flash_selected, "Ctrl+F")
        self.act_stop = action("Stop", self._stop, "Esc", "Cancel at the next safe point; post-programming still runs")
        tb.addSeparator()
        self.act_report = action("Open report", self._open_report, None, "Open the HTML report of the selected target")
        self._update_actions()

    def _build_body(self) -> None:
        split = QSplitter(Qt.Orientation.Vertical)

        self.table = DropTable(0, len(self.COLS))
        self.table.setToolTip("Drop a release file (.hex, .s19, .bin, .pdx) on an ECU to flash it next")
        self.table.files_dropped.connect(self._drop_on_ecu)
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.C_PROGRESS, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self.C_STEP, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._on_select)
        split.addWidget(self.table)

        self.tabs = QTabWidget()
        mono = QFont("Menlo" if sys.platform == "darwin" else "Consolas" if sys.platform == "win32" else "Monospace")
        mono.setStyleHint(QFont.StyleHint.Monospace)

        self.target_editor = TargetEditor(self._project_dir)
        self.target_editor.changed.connect(self._mark_dirty)
        self.tabs.addTab(self.target_editor, "ECU settings")

        self.fw_editor = FirmwareEditor(self._project_dir)
        self.fw_editor.changed.connect(self._mark_dirty)
        self.tabs.addTab(self.fw_editor, "Firmware")

        self.seq_tree = QTreeWidget()
        self.seq_tree.setHeaderLabels(["Step", "Definition", "Status", "Info"])
        self.seq_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tabs.addTab(self.seq_tree, "Sequence")

        editor = QWidget()
        ed_layout = QVBoxLayout(editor)
        self.tpl_path_label = QLabel("")
        self.tpl_path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.tpl_edit = QPlainTextEdit()
        self.tpl_edit.setFont(mono)
        self.tpl_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.tpl_status = QLabel("")
        self.tpl_status.setWordWrap(True)
        buttons = QHBoxLayout()
        self.btn_validate = QPushButton("Validate")
        self.btn_validate.clicked.connect(self._validate_template)
        self.btn_save = QPushButton("Save")
        self.btn_save.clicked.connect(self._save_template)
        self.btn_derive = QPushButton("New OEM template from this…")
        self.btn_derive.clicked.connect(self._derive_template)
        for b in (self.btn_validate, self.btn_save, self.btn_derive):
            buttons.addWidget(b)
        buttons.addStretch(1)
        ed_layout.addWidget(self.tpl_path_label)
        ed_layout.addWidget(self.tpl_edit)
        ed_layout.addLayout(buttons)
        ed_layout.addWidget(self.tpl_status)
        self.tabs.addTab(editor, "Sequence YAML")
        self._tpl_path: str | None = None

        self.trace_view = QPlainTextEdit(readOnly=True)
        self.trace_view.setFont(mono)
        self.trace_view.setMaximumBlockCount(TRACE_LINES)
        self.trace_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.tabs.addTab(self.trace_view, "UDS trace")

        self.log_view = QPlainTextEdit(readOnly=True)
        self.log_view.setFont(mono)
        self.log_view.setMaximumBlockCount(5000)
        self.tabs.addTab(self.log_view, "Log")

        hw = QWidget()
        hw_layout = QVBoxLayout(hw)
        self.hw_list = QPlainTextEdit(readOnly=True)
        self.hw_list.setFont(mono)
        refresh = QPushButton("Detect hardware")
        refresh.clicked.connect(self._detect_hw)
        hw_layout.addWidget(QLabel("CAN channels detected through python-can (Vector XL on Windows, SocketCAN on Linux):"))
        hw_layout.addWidget(self.hw_list)
        hw_layout.addWidget(refresh, alignment=Qt.AlignmentFlag.AlignLeft)
        self.tabs.addTab(hw, "Hardware")

        split.addWidget(self.tabs)
        split.setSizes([260, 540])
        self.setCentralWidget(split)

    # -- project -----------------------------------------------------------
    def _choose_project(self) -> None:
        start = os.path.dirname(self.project.path) if self.project else os.getcwd()
        path, _ = QFileDialog.getOpenFileName(self, "Open flash project", start, "Flash projects (*.ftproj *.yaml)")
        if path:
            self.open_project(path)

    def _reload(self) -> None:
        if self.project:
            self.open_project(self.project.path)

    def _project_dir(self) -> str:
        return os.path.dirname(self.project_path) if self.project_path else os.getcwd()

    def open_project(self, path: str, select_row: int = 0) -> None:
        if self.manager.running:
            QMessageBox.warning(self, "FlashTool", "Wait for the running jobs to finish before opening a project.")
            return
        if not self._ask_save_changes():
            return
        try:
            project = load_project(path)
            raw = load_yaml(path) or {}
        except (ProjectError, OSError) as exc:
            QMessageBox.critical(self, "Cannot open project", str(exc))
            return
        except Exception as exc:  # YAML syntax errors
            QMessageBox.critical(self, "Cannot open project", f"{path}: {exc}")
            return
        raw.setdefault("targets", [])
        self.project, self.raw, self.project_path, self.dirty = project, raw, os.path.abspath(path), False
        self.settings.setValue("last_project", project.path)
        self.states = {t.id: TargetState() for t in project.targets}
        self._fill_table(select_row)
        self.statusBar().showMessage(f"{project.path} – {len(project.targets)} ECU(s)")

    def _update_title(self) -> None:
        name = self.raw.get("name", "") if self.raw else ""
        self.setWindowTitle(f"FlashTool {__version__} – {name}{' *' if self.dirty else ''}")

    def _fill_table(self, select_row: int = 0) -> None:
        project = self.project
        assert project is not None
        self._update_title()
        self.table.blockSignals(True)
        self.table.setRowCount(len(project.targets))
        self._bars.clear()
        for row, t in enumerate(project.targets):
            self._set_row(row, t)
        self.table.blockSignals(False)
        if project.targets:
            self.table.selectRow(min(select_row, len(project.targets) - 1))
            self._on_select()
        else:
            self._show_empty()
        self._update_actions()

    def _show_empty(self) -> None:
        self.target_editor.setEnabled(False)
        self.fw_editor.setEnabled(False)
        self.seq_tree.clear()
        QTreeWidgetItem(self.seq_tree, ["No ECU yet: use Add ECU in the toolbar"])

    def _set_row(self, row: int, t) -> None:
        label = f"{t.name}  [{t.id}]" + ("  (simulated)" if t.simulate else "")
        self.table.setItem(row, self.C_ECU, QTableWidgetItem(label))
        self.table.item(row, self.C_ECU).setData(Qt.ItemDataRole.UserRole, t.id)
        self.table.setItem(row, self.C_BUS, QTableWidgetItem(self._transport_text(t)))
        files = ", ".join(os.path.basename(f.file) + (f" [{f.session}]" if f.session else "") for f in t.firmware)
        fw_item = QTableWidgetItem(files or "– drop a file here –")
        fw_item.setToolTip("\n".join(f.file for f in t.firmware) or "No firmware yet")
        self.table.setItem(row, self.C_FW, fw_item)
        for col in (self.C_STATUS, self.C_SPEED, self.C_STEP):
            if self.table.item(row, col) is None:
                self.table.setItem(row, col, QTableWidgetItem(""))
        bar = self.table.cellWidget(row, self.C_PROGRESS)
        if not isinstance(bar, QProgressBar):
            bar = QProgressBar()
            bar.setRange(0, 1000)
            bar.setFormat("%p%")
            self.table.setCellWidget(row, self.C_PROGRESS, bar)
        self._bars[t.id] = bar
        self.states.setdefault(t.id, TargetState())
        self._refresh_row(t.id)

    @staticmethod
    def _transport_text(t) -> str:
        if t.can is not None:
            c = t.can
            rate = f"{c.bitrate // 1000}k" + (f"/{(c.data_bitrate or 0) // 1000}k" if c.fd else "")
            return f"{'CAN FD' if c.fd else 'CAN'} {rate} {c.hardware.backend}:{c.hardware.channel}  STmin {c.stmin_rx}"
        if t.lin is not None:
            n = t.lin
            return f"LIN {n.baudrate} {n.backend}:{n.channel} NAD 0x{n.nad:02X}  N_Cs {n.n_cs_ms}"
        d = t.doip
        return f"DoIP {d.ecu_ip}:{d.tcp_port} ECU 0x{d.ecu_address:04X}" if d else t.transport

    def _selected_ids(self) -> list[str]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.table.item(r, self.C_ECU).data(Qt.ItemDataRole.UserRole) for r in rows]

    def _on_select(self) -> None:
        ids = self._selected_ids()
        if not ids or not self.project:
            return
        target = self.project.target(ids[0])
        raw_target = self._raw_target(target.id)
        self.target_editor.setEnabled(not self.manager.running)
        self.fw_editor.setEnabled(not self.manager.running)
        if raw_target is not None:
            self.target_editor.set_target(raw_target)
            self.fw_editor.set_entries(raw_target.setdefault("firmware", []))
        self._show_sequence(target)
        self._show_template(target)
        self._show_trace(target.id)
        self._update_actions()

    def _show_sequence(self, target) -> None:
        self.seq_tree.clear()
        self._step_items.clear()
        try:
            _, seq, warnings = prepare(self.project, target)
        except JobSetupError as exc:
            QTreeWidgetItem(self.seq_tree, ["Error", str(exc)])
            return
        root_title = f"{seq['name']}  ({' ← '.join(os.path.basename(s) for s in reversed(seq['sources']))})"
        for section in ("steps", "finally"):
            top = QTreeWidgetItem(self.seq_tree, [section if section == "finally" else root_title])
            parents = {0: top}
            for depth, step in flatten(seq[section]):
                item = QTreeWidgetItem(parents[depth], [step.get("id", "?"), describe_step(step), "", ""])
                parents[depth + 1] = item
                self._step_items[step.get("id", "?")] = item
            top.setExpanded(True)
        self.seq_tree.expandAll()
        self.seq_tree.resizeColumnToContents(0)
        for sid, (status, info) in self.states[target.id].steps.items():
            self._paint_step(sid, status, info)
        for w in warnings:
            QTreeWidgetItem(self.seq_tree, ["warning", w])

    # -- visual editing ---------------------------------------------------------
    def _raw_target(self, target_id: str) -> dict | None:
        for t in (self.raw or {}).get("targets", []):
            if str(t.get("id")) == target_id:
                return t
        return None

    def _selected_row(self) -> int:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return rows[0] if rows else 0

    def _mark_dirty(self) -> None:
        self.dirty = True
        self._update_title()
        self._apply_timer.start()

    def _apply_edits(self) -> None:
        """Re-validate the edited project; refresh the table and the sequence preview."""
        if self.raw is None or self.project_path is None:
            return
        try:
            project = project_from_data(self.raw, self.project_path)
        except Exception as exc:
            self.statusBar().setStyleSheet(f"color: {STATUS_COLORS['failed']}")
            self.statusBar().showMessage(f"Project not valid yet: {exc}")
            return
        self.statusBar().setStyleSheet("")
        self.statusBar().showMessage("Unsaved changes – Ctrl+S to save" if self.dirty else "")
        row = self._selected_row()
        self.project = project
        # refresh the table without rebuilding the editors (they hold the dicts being edited)
        self.table.blockSignals(True)
        self.table.setRowCount(len(project.targets))
        for r, t in enumerate(project.targets):
            self._set_row(r, t)
        self.table.selectRow(min(row, len(project.targets) - 1))
        self.table.blockSignals(False)
        ids = self._selected_ids()
        if ids:
            self._show_sequence(project.target(ids[0]))
            self._show_template(project.target(ids[0]))
        self._update_actions()

    def save_project(self) -> bool:
        if self.raw is None or self.project_path is None:
            return False
        try:
            save_project(self.raw, self.project_path)
        except Exception as exc:
            QMessageBox.critical(self, "Project not saved", str(exc))
            return False
        row = self._selected_row()
        self.dirty = False
        self.open_project(self.project_path, select_row=row)
        self._log(f"Saved {self.project_path}")
        return True

    def _ask_save_changes(self) -> bool:
        """True when it is fine to continue (saved, discarded or nothing to save)."""
        if not self.dirty:
            return True
        answer = QMessageBox.question(
            self, "Unsaved changes", "Save the changes to the project?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Save:
            return self.save_project()
        if answer == QMessageBox.StandardButton.Discard:
            self.dirty = False
            return True
        return False

    def _new_project(self) -> None:
        if not self._ask_save_changes():
            return
        path, _ = QFileDialog.getSaveFileName(self, "New flash project", self._project_dir(), "Flash projects (*.ftproj)")
        if not path:
            return
        if not path.endswith(".ftproj"):
            path += ".ftproj"
        self.create_project(path)

    def create_project(self, path: str) -> None:
        data = new_project(os.path.splitext(os.path.basename(path))[0])
        save_project(data, path)
        self.dirty = False
        self.open_project(path)

    def add_target(self, bus: str = "can") -> None:
        if self.raw is None:
            QMessageBox.information(self, "FlashTool", "Create or open a project first.")
            return
        used = {str(t.get("id")) for t in self.raw["targets"]}
        n = len(used) + 1
        while f"ecu{n}" in used:
            n += 1
        self.raw["targets"].append(new_target(f"ecu{n}", bus))
        self._mark_dirty()
        self._apply_timer.stop()
        self._apply_edits()
        self.table.selectRow(len(self.raw["targets"]) - 1)
        self._on_select()
        self.tabs.setCurrentWidget(self.target_editor)

    def remove_target(self) -> None:
        ids = self._selected_ids()
        if not ids or self.raw is None:
            return
        if QMessageBox.question(self, "Remove ECU", f"Remove '{ids[0]}' from the project?") != QMessageBox.StandardButton.Yes:
            return
        self.raw["targets"] = [t for t in self.raw["targets"] if str(t.get("id")) != ids[0]]
        self._bars.pop(ids[0], None)
        self._mark_dirty()
        self._apply_timer.stop()
        self._apply_edits()
        if not self.raw["targets"]:
            self.table.setRowCount(0)
            self._show_empty()
        else:
            self._on_select()

    def load_release(self) -> None:
        ids = self._selected_ids()
        if not ids or self.manager.running:
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, f"Release file(s) for {ids[0]}", self.settings.value("last_release_dir", self._project_dir()),
            "Firmware (*.hex *.ihex *.s19 *.s28 *.s37 *.srec *.mot *.bin *.pdx *.odx-f *.odx);;All files (*)")
        if paths:
            self.settings.setValue("last_release_dir", os.path.dirname(paths[0]))
            self.put_release(ids[0], paths)

    def _drop_on_ecu(self, paths: list[str], row: int) -> None:
        if row < 0 or self.manager.running or self.project is None:
            return
        self.table.selectRow(row)
        self.put_release(self.table.item(row, self.C_ECU).data(Qt.ItemDataRole.UserRole), paths)

    def put_release(self, target_id: str, paths: list[str]) -> None:
        """New release for an ECU: each file replaces the matching firmware entry (options kept) or is added."""
        raw_target = self._raw_target(target_id)
        if raw_target is None or self.project is None:
            return
        entries = raw_target.setdefault("firmware", [])
        for path in paths:
            index = self._entry_to_replace(target_id, entries, path)
            if index == -1:
                continue  # cancelled
            try:
                note = put_release_file(entries, path, self.project.base_dir, index, self.fw_editor._choose_session)
            except Exception as exc:
                QMessageBox.warning(self, "Release file", f"{os.path.basename(path)}: {exc}")
                continue
            self._log(f"[{target_id}] {note}")
        self.states[target_id] = TargetState()
        self._mark_dirty()
        self._apply_timer.stop()
        self._apply_edits()
        if self._selected_ids() == [target_id]:
            self.fw_editor.set_entries(entries)
            self.tabs.setCurrentWidget(self.fw_editor)

    def _entry_to_replace(self, target_id: str, entries: list, path: str) -> int | None:
        """None = add as a new file, -1 = cancelled, otherwise the index to replace."""
        if not entries:
            return None
        if len(entries) == 1:
            return 0
        choices = [f"Replace {os.path.basename(str(e.get('file', '')))}" for e in entries] + ["Add as an additional file"]
        choice, ok = QInputDialog.getItem(self, "Release file", f"{os.path.basename(path)} for {target_id}:",
                                          choices, 0, False)
        if not ok:
            return -1
        index = choices.index(choice)
        return None if index == len(entries) else index

    def open_sequence_editor(self) -> None:
        ids = self._selected_ids()
        if not self.project or not ids:
            QMessageBox.information(self, "FlashTool", "Select an ECU first.")
            return
        target = self.project.target(ids[0])
        try:
            path = template_path(target.sequence, self.project.base_dir)
        except SequenceError:
            path = None
        editor = SequenceEditor(self.project.base_dir, path)
        editor.saved.connect(lambda p, tid=target.id: self._sequence_saved(tid, p))
        editor.show()
        self._seq_editor = editor

    def _sequence_saved(self, target_id: str, path: str) -> None:
        raw_target = self._raw_target(target_id)
        if raw_target is None or self.project is None:
            return
        rel = os.path.relpath(path, self.project.base_dir)
        try:
            current = template_path(str(raw_target.get("sequence", "uds_standard")), self.project.base_dir)
        except SequenceError:
            current = None
        if current != os.path.abspath(path):
            answer = QMessageBox.question(self, "Use this sequence?",
                                          f"Use '{rel}' as the sequence of ECU '{target_id}'?")
            if answer == QMessageBox.StandardButton.Yes:
                raw_target["sequence"] = rel
                self._mark_dirty()
                if self._selected_ids() == [target_id]:
                    self.target_editor.set_target(raw_target)
                return
        self._apply_edits()

    # -- template editor ------------------------------------------------------
    def _show_template(self, target) -> None:
        try:
            path = template_path(target.sequence, self.project.base_dir)
        except SequenceError as exc:
            self._tpl_path = None
            self.tpl_path_label.setText(str(exc))
            self.tpl_edit.setPlainText("")
            return
        self._tpl_path = path
        builtin = is_builtin(path)
        with open(path, encoding="utf-8") as f:
            self.tpl_edit.setPlainText(f.read())
        self.tpl_edit.setReadOnly(builtin)
        self.btn_save.setEnabled(not builtin)
        self.tpl_path_label.setText(path + ("   (built-in template: read-only, derive an OEM template to change it)"
                                            if builtin else ""))
        self.tpl_status.setText("")

    def _validate_template(self) -> bool:
        if not self._tpl_path:
            return False
        folder = os.path.dirname(self._tpl_path)
        probe = os.path.join(folder, f".{os.path.basename(self._tpl_path)}.check.yaml")
        try:
            with open(probe, "w", encoding="utf-8") as f:
                f.write(self.tpl_edit.toPlainText())
            seq = load_sequence(probe, folder)
        except (SequenceError, OSError) as exc:
            self.tpl_status.setStyleSheet(f"color: {STATUS_COLORS['failed']}")
            self.tpl_status.setText(f"Invalid: {exc}")
            return False
        except Exception as exc:  # YAML syntax errors and similar
            self.tpl_status.setStyleSheet(f"color: {STATUS_COLORS['failed']}")
            self.tpl_status.setText(f"Invalid: {exc}")
            return False
        finally:
            if os.path.exists(probe):
                os.remove(probe)
        self.tpl_status.setStyleSheet(f"color: {STATUS_COLORS['ok']}")
        self.tpl_status.setText(f"Valid: '{seq['name']}', {len(flatten(seq['steps']))} steps + "
                                f"{len(flatten(seq['finally']))} final steps, catalogs: {len(seq['catalogs'])}")
        return True

    def _save_template(self) -> None:
        if not self._tpl_path or is_builtin(self._tpl_path) or not self._validate_template():
            return
        with open(self._tpl_path, "w", encoding="utf-8") as f:
            f.write(self.tpl_edit.toPlainText())
        self._log(f"Saved {self._tpl_path}")
        self._on_select()

    def _derive_template(self) -> None:
        if not self.project or not self._tpl_path:
            return
        name, ok = QInputDialog.getText(self, "New OEM template", "File name (in the project folder):",
                                        text="oem_template.yaml")
        if not ok or not name.strip():
            return
        path = os.path.join(self.project.base_dir, name.strip())
        if os.path.exists(path):
            QMessageBox.warning(self, "FlashTool", f"{path} already exists.")
            return
        base = os.path.splitext(os.path.basename(self._tpl_path))[0] if is_builtin(self._tpl_path) \
            else os.path.relpath(self._tpl_path, self.project.base_dir)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"name: {os.path.splitext(name)[0]}\nextends: {base}\n\nvariables: {{}}\n\npatch: []\n"
                    "#  - replace: security\n#    with: {id: authenticate, authenticate: {}}\n")
        QMessageBox.information(self, "FlashTool", f"Created {path}.\nSet 'sequence: {name.strip()}' on the target "
                                                   "in the project file to use it.")

    def _paint_step(self, sid: str, status: str, info: str) -> None:
        item = self._step_items.get(sid)
        if item is None:
            return
        item.setText(2, status)
        item.setText(3, info)
        color = STATUS_COLORS.get(status)
        if color:
            item.setForeground(2, QBrush(QColor(color)))

    def _show_trace(self, target_id: str) -> None:
        self.trace_view.setPlainText("\n".join(self.states[target_id].trace))
        self.trace_view.moveCursor(QTextCursor.MoveOperation.End)

    # -- jobs --------------------------------------------------------------
    def _flash_all(self) -> None:
        if self.project:
            self._start([t.id for t in self.project.targets])

    def _flash_selected(self) -> None:
        self._start(self._selected_ids())

    def _start(self, ids: list[str]) -> None:
        if not self.project or not ids:
            return
        if self.dirty and not self.save_project():  # jobs read the project file from disk
            return
        busy = [i for i in ids if i in self.manager.running]
        if busy:
            QMessageBox.information(self, "FlashTool", f"Already running: {', '.join(busy)}")
            return
        for tid in ids:
            self.states[tid] = TargetState(status="starting")
            self._refresh_row(tid)
        self._on_select()
        self.manager.start(self.project.path, ids)
        self._log(f"Started {', '.join(ids)}")
        self._update_actions()

    def _stop(self) -> None:
        if self.manager.running:
            self.manager.cancel()
            self._log("Cancel requested; post-programming steps will still run")

    def _open_report(self) -> None:
        ids = self._selected_ids()
        report = self.states[ids[0]].report if ids else None
        if report:
            QDesktopServices.openUrl(QUrl.fromLocalFile(report.replace("report.json", "report.html")))

    def _pump_events(self) -> None:
        touched: set[str] = set()
        for e in self.manager.poll(0.0):
            touched.add(e.job)
            self._handle(e)
        if self._pending_trace:
            selected = self._selected_ids()
            lines = [line for tid, line in self._pending_trace if selected and tid == selected[0]]
            if lines:
                self.trace_view.appendPlainText("\n".join(lines))
            self._pending_trace.clear()
        for tid in touched:
            self._refresh_row(tid)
        if touched:
            self._update_actions()

    def _handle(self, e: ev.Event) -> None:
        st = self.states.setdefault(e.job, TargetState())
        if isinstance(e, ev.JobStarted):
            st.status, st.total = "running", e.info.get("total_size", 0)
            self._log(f"[{e.job}] {e.target}: {e.info.get('blocks')} block(s), {st.total} bytes, run dir {e.info.get('run_dir')}")
        elif isinstance(e, ev.StepStarted):
            st.current = e.label
            sid, info = _step_id(e.path)
            st.steps[sid] = ("running", info)
            self._paint_if_selected(e.job, sid, "running", info)
        elif isinstance(e, ev.StepFinished):
            sid, info = _step_id(e.path)
            detail = info + (f"  {e.error}" if e.error else "")
            st.steps[sid] = (e.status, detail)
            self._paint_if_selected(e.job, sid, e.status, detail)
            if e.status in ("failed", "warning"):
                self._log(f"[{e.job}] {e.status}: {e.path} - {e.error}")
        elif isinstance(e, ev.Progress):
            st.done, st.total, st.speed = e.done, e.total, e.bytes_per_s
        elif isinstance(e, ev.Traffic):
            hexdata = e.data.hex(" ").upper()
            if len(hexdata) > 150:
                hexdata = f"{hexdata[:150]} … ({len(e.data)} bytes)"
            line = f"{e.t:10.4f} {e.direction}{' F' if e.functional else '  '} {hexdata}  {e.note}".rstrip()
            st.trace.append(line)
            self._pending_trace.append((e.job, line))
        elif isinstance(e, ev.LogEvent):
            self._log(f"[{e.job}] {e.level}: {e.message}")
        elif isinstance(e, ev.JobFinished):
            st.status = "passed" if e.success else "failed"
            st.error, st.report, st.current = e.error, e.report, e.error or ""
            self._log(f"[{e.job}] {'PASSED' if e.success else 'FAILED'} in {e.duration_s:.2f} s"
                      + (f" - {e.error}" if e.error else ""))

    def _paint_if_selected(self, job: str, sid: str, status: str, info: str) -> None:
        selected = self._selected_ids()
        if selected and selected[0] == job:
            self._paint_step(sid, status, info)

    def _refresh_row(self, tid: str) -> None:
        if not self.project:
            return
        row = next((r for r in range(self.table.rowCount())
                    if self.table.item(r, self.C_ECU).data(Qt.ItemDataRole.UserRole) == tid), None)
        if row is None:
            return
        st = self.states[tid]
        status_item = self.table.item(row, self.C_STATUS)
        status_item.setText(st.status.upper() if st.status in ("passed", "failed") else st.status)
        color = {"passed": STATUS_COLORS["ok"], "failed": STATUS_COLORS["failed"], "running": STATUS_COLORS["running"]}
        status_item.setForeground(QBrush(QColor(color.get(st.status, "#5b6875"))))
        self._bars[tid].setValue(int(1000 * st.done / st.total) if st.total else 0)
        self.table.item(row, self.C_SPEED).setText(f"{st.speed / 1024:.1f} KB/s" if st.speed else "")
        self.table.item(row, self.C_STEP).setText(st.current)

    def _update_actions(self) -> None:
        has_project = self.project is not None
        running = bool(self.manager.running)
        self.act_flash_all.setEnabled(has_project and not running)
        self.act_flash_sel.setEnabled(has_project and bool(self._selected_ids() if hasattr(self, "table") else []))
        self.act_stop.setEnabled(running)
        self.act_reload.setEnabled(has_project and not running)
        for a in (self.act_new, self.act_open):
            a.setEnabled(not running)
        self.act_save.setEnabled(self.raw is not None and not running)
        self.act_add.setEnabled(self.raw is not None and not running)
        self.act_remove.setEnabled(has_project and not running and bool(self._selected_ids() if hasattr(self, "table") else []))
        self.act_seq.setEnabled(has_project and bool(self._selected_ids() if hasattr(self, "table") else []))
        self.act_release.setEnabled(has_project and not running and bool(self._selected_ids() if hasattr(self, "table") else []))
        ids = self._selected_ids() if hasattr(self, "table") else []
        self.act_report.setEnabled(bool(ids and self.states.get(ids[0]) and self.states[ids[0]].report))

    def _detect_hw(self) -> None:
        channels = list_channels()
        self.hw_list.setPlainText(
            "\n".join(", ".join(f"{k}={v}" for k, v in c.items()) for c in channels)
            or "No channels detected. Vector VN needs Windows with the Vector driver setup; "
               "SocketCAN needs Linux. Simulated targets use the virtual bus."
        )

    def _log(self, text: str) -> None:
        self.log_view.appendPlainText(text)

    def closeEvent(self, event) -> None:
        if self.manager.running:
            answer = QMessageBox.question(
                self, "Jobs running",
                "Flash jobs are still running. Cancel them and quit? Post-programming steps will be attempted.",
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        if not self._ask_save_changes():
            event.ignore()
            return
        self.timer.stop()
        self.manager.shutdown()
        event.accept()


def run(project_path: str | None = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("FlashTool")
    win = MainWindow(project_path)
    win.show()
    return app.exec()
