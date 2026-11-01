"""Main window (PySide6). Talks only to the application layer (JobManager, project, prepare)."""

from __future__ import annotations

import glob
import json
import os
import re
import sys
from collections import deque
from dataclasses import dataclass, field

from PySide6.QtCore import QEvent, QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QBrush, QColor, QDesktopServices, QFont, QKeySequence, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSplitter, QStackedWidget, QTableWidget, QTableWidgetItem, QTabWidget,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .. import __version__
from ..app.job import JobSetupError, prepare
from ..app.manager import JobManager
from ..app.project import Project, ProjectError, load_project
from ..app.project_io import load_yaml, new_project, new_target, project_from_data, save_project
from ..hal.backends import list_channels
from ..sequence import events as ev
from ..sequence.loader import (
    SequenceError, block_sequence_key, describe_step, flatten, is_builtin, load_sequence, template_path,
)
from .project_editor import (
    FirmwareEditor, TargetEditor, entry_block_types, entry_path, newer_release, put_release_file, release_candidates,
)
from .sequence_editor import SequenceEditor
from .theme import apply_theme, icon, tokens
from .tools_page import ToolsPage
from .widgets import BUS_LABEL, DurationChart, EcuCard

TRACE_LINES = 20000
FIRMWARE_FILTER = "Firmware (*.hex *.ihex *.s19 *.s28 *.s37 *.srec *.mot *.bin *.pdx *.odx-f *.odx *.vbf);;All files (*)"


def status_color(status: str) -> str:
    t = tokens()
    return {"ok": t["ok"], "failed": t["fail"], "warning": t["warn"], "skipped": t["muted"], "running": t["run"],
            "shared": t["muted"]}.get(status, t["text"])


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


def _page(title: str, subtitle: str = "") -> tuple[QWidget, QVBoxLayout, QHBoxLayout]:
    page = QWidget()
    page.setObjectName("page")
    layout = QVBoxLayout(page)
    layout.setContentsMargins(24, 20, 24, 16)
    layout.setSpacing(14)
    header = QHBoxLayout()
    titles = QVBoxLayout()
    t = QLabel(title)
    t.setProperty("role", "h1")
    titles.addWidget(t)
    if subtitle:
        s = QLabel(subtitle)
        s.setProperty("role", "muted")
        titles.addWidget(s)
    header.addLayout(titles)
    header.addStretch(1)
    layout.addLayout(header)
    return page, layout, header


class MainWindow(QMainWindow):
    PAGES = ("flash", "configure", "tools", "trace", "reports", "hardware")

    def __init__(self, project_path: str | None = None):
        super().__init__()
        self.resize(1360, 860)
        self.settings = QSettings("FlashTool", "FlashTool")
        self.project: Project | None = None
        self.raw: dict | None = None  # project file content, edited by the visual editors
        self.project_path: str | None = None
        self.dirty = False
        self.sealed = False  # project unpacked from a sealed .ftpack: flash only, no edits
        self.current_id: str | None = None
        self.cards: dict[str, EcuCard] = {}
        self.states: dict[str, TargetState] = {}
        self._seq_editor: SequenceEditor | None = None
        self._step_items: dict[str, QTreeWidgetItem] = {}
        self._pending_trace: list[tuple[str, str]] = []
        self._tpl_path: str | None = None
        self.manager = JobManager()
        self.theme_mode = str(self.settings.value("theme", "system"))
        app = QApplication.instance()
        if app is not None:
            apply_theme(app, self.theme_mode)

        self.stack = QStackedWidget()
        central = QWidget()
        row = QHBoxLayout(central)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self._build_sidebar())
        row.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        for build in (self._build_flash_page, self._build_configure_page, self._build_tools_page,
                      self._build_trace_page, self._build_reports_page, self._build_hardware_page):
            self.stack.addWidget(build())
        self._build_shortcuts()
        self.show_page("flash")
        self._update_title()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._pump_events)
        self.timer.start(50)
        self._apply_timer = QTimer(self, singleShot=True, interval=300)
        self._apply_timer.timeout.connect(self._apply_edits)

        path = project_path or self.settings.value("last_project")
        if path and os.path.isfile(str(path)):
            self.open_project(str(path))
        self._update_actions()

    # ======================================================================== layout
    def _build_sidebar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("sidebar")
        bar.setFixedWidth(214)
        layout = QVBoxLayout(bar)
        layout.setContentsMargins(14, 18, 14, 14)
        layout.setSpacing(4)
        name = QLabel("FlashTool")
        name.setProperty("role", "h1")
        version = QLabel(f"UDS flashing · v{__version__}")
        version.setProperty("role", "muted")
        layout.addWidget(name)
        layout.addWidget(version)
        layout.addSpacing(18)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav: dict[str, QPushButton] = {}
        for key, text, ico in (("flash", "Flash", "flash"), ("configure", "Configure", "config"),
                               ("tools", "ECU tools", "tools"), ("trace", "Trace && log", "trace"), ("reports", "Reports", "report"),
                               ("hardware", "Hardware", "chip")):
            b = QPushButton(text)
            b.setProperty("nav", True)
            b.setProperty("icon_name", ico)
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, k=key: self.show_page(k))
            self.nav_group.addButton(b)
            self.nav[key] = b
            layout.addWidget(b)
        layout.addStretch(1)

        project_label = QLabel("PROJECT")
        project_label.setProperty("role", "muted")
        layout.addWidget(project_label)
        self.side_buttons: dict[str, QPushButton] = {}
        for key, text, ico, slot in (("new", "New project…", "new", self._new_project),
                                     ("open", "Open project…", "open", self._choose_project),
                                     ("save", "Save project", "save", self.save_project),
                                     ("export", "Export package…", "package", self._export_pack),
                                     ("import", "Import package…", "package", self._import_pack)):
            b = QPushButton(text)
            b.setProperty("nav", True)
            b.setProperty("icon_name", ico)
            b.clicked.connect(slot)
            self.side_buttons[key] = b
            layout.addWidget(b)
        layout.addSpacing(10)
        self.theme_btn = QPushButton()
        self.theme_btn.setProperty("nav", True)
        self.theme_btn.clicked.connect(self.toggle_theme)
        layout.addWidget(self.theme_btn)
        self._refresh_sidebar_icons()
        return bar

    def _refresh_sidebar_icons(self) -> None:
        color = tokens()["sidebar_text"]
        for b in list(self.nav.values()) + list(self.side_buttons.values()):
            b.setIcon(icon(b.property("icon_name"), color, 18))
        dark = tokens()["bg"] == "#12161c"
        self.theme_btn.setText("Light mode" if dark else "Dark mode")
        self.theme_btn.setIcon(icon("sun" if dark else "moon", color, 18))

    def _build_flash_page(self) -> QWidget:
        page, layout, header = _page("Flash")
        self.project_title = header.itemAt(0).layout().itemAt(0).widget()
        self.project_path_label = QLabel("")
        self.project_path_label.setProperty("role", "muted")
        header.itemAt(0).layout().addWidget(self.project_path_label)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setProperty("kind", "danger")
        self.stop_btn.clicked.connect(self._stop)
        self.flash_all_btn = QPushButton("Flash all")
        self.flash_all_btn.setProperty("kind", "primary")
        self.flash_all_btn.clicked.connect(self._flash_all)
        header.addWidget(self.stop_btn)
        header.addWidget(self.flash_all_btn)

        self.summary = QLabel("")
        self.summary.setProperty("role", "muted")
        layout.addWidget(self.summary)

        self.cards_host = QWidget()
        self.cards_grid = QGridLayout(self.cards_host)
        self.cards_grid.setContentsMargins(0, 0, 0, 0)
        self.cards_grid.setSpacing(14)
        self.cards_grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.cards_scroll = QScrollArea()
        self.cards_scroll.setWidgetResizable(True)
        self.cards_scroll.setWidget(self.cards_host)
        self.cards_scroll.viewport().installEventFilter(self)

        self.empty = QFrame()
        self.empty.setObjectName("card")
        el = QVBoxLayout(self.empty)
        el.setContentsMargins(30, 30, 30, 30)
        msg = QLabel("No ECU in this project yet")
        msg.setProperty("role", "h2")
        hint = QLabel("Add an ECU, then drop its .hex / .s19 / .bin / .pdx / .vbf release file on the card.")
        hint.setProperty("role", "muted")
        add = QPushButton("Add ECU")
        add.setProperty("kind", "primary")
        add.setMenu(self._bus_menu())
        for w in (msg, hint, add):
            el.addWidget(w, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.empty)
        layout.addWidget(self.cards_scroll, 1)
        return page

    def _bus_menu(self) -> QMenu:
        menu = QMenu(self)
        for bus, label in (("can", "CAN"), ("canfd", "CAN FD"), ("lin", "LIN"), ("doip", "DoIP (Ethernet)")):
            menu.addAction(f"ECU on {label}", lambda b=bus: self.add_target(b))
        return menu

    def _build_configure_page(self) -> QWidget:
        page, layout, header = _page("Configure", "ECU settings, firmware files and the programming sequence")
        self.seq_editor_btn = QPushButton("Sequence editor…")
        self.seq_editor_btn.clicked.connect(self.open_sequence_editor)
        self.release_btn = QPushButton("Load release file…")
        self.release_btn.clicked.connect(lambda: self.load_release())
        header.addWidget(self.release_btn)
        header.addWidget(self.seq_editor_btn)

        left = QFrame()
        left.setObjectName("card")
        ll = QVBoxLayout(left)
        ll.setContentsMargins(10, 10, 10, 10)
        title = QLabel("ECUs")
        title.setProperty("role", "h2")
        ll.addWidget(title)
        self.ecu_list = QListWidget()
        self.ecu_list.currentItemChanged.connect(
            lambda cur, _prev: cur is not None and self.select_target(cur.data(Qt.ItemDataRole.UserRole)))
        ll.addWidget(self.ecu_list, 1)
        buttons = QHBoxLayout()
        self.add_btn = QPushButton("Add ECU")
        self.add_btn.setMenu(self._bus_menu())
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.clicked.connect(self.remove_target)
        buttons.addWidget(self.add_btn)
        buttons.addWidget(self.remove_btn)
        ll.addLayout(buttons)

        self.tabs = QTabWidget()
        self.target_editor = TargetEditor(self._project_dir)
        self.target_editor.changed.connect(self._mark_dirty)
        self.tabs.addTab(self.target_editor, "ECU settings")
        self.fw_editor = FirmwareEditor(self._project_dir)
        self.fw_editor.changed.connect(self._mark_dirty)
        self.tabs.addTab(self.fw_editor, "Firmware")

        seq_page = QWidget()
        sl = QVBoxLayout(seq_page)
        self.seq_tree = QTreeWidget()
        self.seq_tree.setHeaderLabels(["Step", "Definition", "Status", "Info"])
        self.seq_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        sl.addWidget(self.seq_tree)
        self.tabs.addTab(seq_page, "Sequence")

        yaml_page = QWidget()
        yl = QVBoxLayout(yaml_page)
        self.tpl_path_label = QLabel("")
        self.tpl_path_label.setProperty("role", "muted")
        self.tpl_edit = QPlainTextEdit()
        self.tpl_edit.setFont(self._mono())
        self.tpl_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.tpl_status = QLabel("")
        self.tpl_status.setWordWrap(True)
        tb = QHBoxLayout()
        self.btn_validate = QPushButton("Validate")
        self.btn_validate.clicked.connect(self._validate_template)
        self.btn_save = QPushButton("Save")
        self.btn_save.clicked.connect(self._save_template)
        self.btn_derive = QPushButton("New OEM template from this…")
        self.btn_derive.clicked.connect(self._derive_template)
        for b in (self.btn_validate, self.btn_save, self.btn_derive):
            tb.addWidget(b)
        tb.addStretch(1)
        yl.addWidget(self.tpl_path_label)
        yl.addWidget(self.tpl_edit, 1)
        yl.addLayout(tb)
        yl.addWidget(self.tpl_status)
        self.tabs.addTab(yaml_page, "Sequence YAML")

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(left)
        split.addWidget(self.tabs)
        split.setSizes([260, 900])
        layout.addWidget(split, 1)
        return page

    def _build_tools_page(self) -> QWidget:
        self.tools = ToolsPage(self._log, self._mono())
        return self.tools

    def _build_trace_page(self) -> QWidget:
        page, layout, header = _page("Trace & log", "UDS requests and responses per ECU, and the log of all jobs")
        self.trace_ecu = QComboBox()
        self.trace_ecu.currentIndexChanged.connect(lambda _i: self._show_trace(self.trace_ecu.currentData()))
        header.addWidget(QLabel("ECU"))
        header.addWidget(self.trace_ecu)
        self.trace_view = QPlainTextEdit(readOnly=True)
        self.trace_view.setFont(self._mono())
        self.trace_view.setMaximumBlockCount(TRACE_LINES)
        self.trace_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log_view = QPlainTextEdit(readOnly=True)
        self.log_view.setFont(self._mono())
        self.log_view.setMaximumBlockCount(5000)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.trace_view)
        split.addWidget(self.log_view)
        split.setSizes([520, 200])
        layout.addWidget(split, 1)
        return page

    def _build_reports_page(self) -> QWidget:
        page, layout, header = _page("Reports", "Every flash run with its result, and where the time went")
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._refresh_reports)
        self.open_report_btn = QPushButton("Open report")
        self.open_report_btn.setProperty("kind", "primary")
        self.open_report_btn.clicked.connect(self._open_selected_report)
        header.addWidget(refresh)
        header.addWidget(self.open_report_btn)
        self.runs = QTableWidget(0, 6)
        self.runs.setHorizontalHeaderLabels(["ECU", "Started", "Result", "Duration", "Bytes", "Error"])
        self.runs.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.runs.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.runs.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.runs.verticalHeader().setVisible(False)
        self.runs.setAlternatingRowColors(True)
        self.runs.horizontalHeader().setStretchLastSection(True)
        self.runs.itemSelectionChanged.connect(self._show_run)
        self.runs.doubleClicked.connect(lambda _i: self._open_selected_report())
        self.run_info = QLabel("")
        self.run_info.setProperty("role", "muted")
        self.chart = DurationChart()
        chart_scroll = QScrollArea()
        chart_scroll.setWidgetResizable(True)
        chart_scroll.setWidget(self.chart)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.runs)
        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addWidget(self.run_info)
        bl.addWidget(chart_scroll, 1)
        split.addWidget(bottom)
        split.setSizes([300, 420])
        layout.addWidget(split, 1)
        self._run_paths: list[str] = []
        return page

    def _build_hardware_page(self) -> QWidget:
        page, layout, header = _page("Hardware", "CAN interfaces found through python-can "
                                                 "(Vector XL on Windows, SocketCAN on Linux)")
        detect = QPushButton("Detect hardware")
        detect.setProperty("kind", "primary")
        detect.clicked.connect(self._detect_hw)
        header.addWidget(detect)
        self.hw_list = QPlainTextEdit(readOnly=True)
        self.hw_list.setFont(self._mono())
        layout.addWidget(self.hw_list, 1)
        return page

    def _build_shortcuts(self) -> None:
        for key, slot in (("Ctrl+N", self._new_project), ("Ctrl+O", self._choose_project), ("Ctrl+S", self.save_project),
                          ("Ctrl+L", lambda: self.load_release()), ("Ctrl+E", self.open_sequence_editor),
                          ("Ctrl+Shift+F", self._flash_all), ("Ctrl+F", lambda: self.flash(self.current_id)),
                          ("Esc", self._stop)):
            a = QAction(self)
            a.setShortcut(QKeySequence(key))
            a.triggered.connect(slot)
            self.addAction(a)

    @staticmethod
    def _mono() -> QFont:
        f = QFont("Menlo" if sys.platform == "darwin" else "Consolas" if sys.platform == "win32" else "Monospace")
        f.setStyleHint(QFont.StyleHint.Monospace)
        return f

    def show_page(self, name: str) -> None:
        self.stack.setCurrentIndex(self.PAGES.index(name))
        self.nav[name].setChecked(True)
        if name == "reports":
            self._refresh_reports()
        if name == "trace" and self.current_id:
            self._sync_trace_combo()

    def toggle_theme(self) -> None:
        dark = tokens()["bg"] == "#12161c"
        self.theme_mode = "light" if dark else "dark"
        self.settings.setValue("theme", self.theme_mode)
        apply_theme(QApplication.instance(), self.theme_mode)
        self._refresh_sidebar_icons()
        for card in self.cards.values():
            card.refresh_icons()
        self.chart.update()

    def eventFilter(self, obj, event) -> bool:
        if obj is self.cards_scroll.viewport() and event.type() == QEvent.Type.Resize:
            self._layout_cards()
        return super().eventFilter(obj, event)

    # ======================================================================== project
    def _project_dir(self) -> str:
        return os.path.dirname(self.project_path) if self.project_path else os.getcwd()

    def _update_title(self) -> None:
        name = (self.raw.get("name", "") if self.raw else "") + ("  (sealed, read-only)" if self.sealed else "")
        self.setWindowTitle(f"FlashTool {__version__}" + (f" – {name}{' *' if self.dirty else ''}" if name else ""))
        self.project_title.setText(f"{name}{'  •' if self.dirty else ''}" if name else "No project open")
        self.project_path_label.setText(self.project_path or "Open or create a project in the sidebar")

    def _choose_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open flash project", self._project_dir(),
                                              "Flash projects (*.ftproj *.yaml)")
        if path:
            self.open_project(path)

    def open_project(self, path: str, select: str | None = None) -> None:
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
        try:
            from ..app.package import is_sealed

            self.sealed = is_sealed(self.project_path)
        except Exception:
            self.sealed = False
        self.settings.setValue("last_project", project.path)
        self.states = {t.id: TargetState() for t in project.targets}
        self._rebuild_targets(select)
        self.statusBar().showMessage("Sealed package: read-only, ready to flash" if self.sealed
                                     else f"Opened {project.path}", 0 if self.sealed else 4000)

    def _rebuild_targets(self, select: str | None = None) -> None:
        """ECU list and cards from self.project (keeps the selection when possible)."""
        project = self.project
        assert project is not None
        ids = [t.id for t in project.targets]
        for tid in list(self.cards):
            if tid not in ids:
                self.cards.pop(tid).deleteLater()
        for t in project.targets:
            self.states.setdefault(t.id, TargetState())
            card = self.cards.get(t.id)
            if card is None:
                card = EcuCard(t.id)
                card.flash_clicked.connect(self.flash)
                card.release_clicked.connect(self.load_release)
                card.configure_clicked.connect(lambda tid: (self.select_target(tid), self.show_page("configure")))
                card.report_clicked.connect(self._open_report)
                card.files_dropped.connect(self.put_release)
                card.release_chosen.connect(self.choose_release)
                card.selected.connect(self.select_target)
                self.cards[t.id] = card
            card.set_target(t)
            self._refresh_firmware_rows(t.id)
            card.set_state(self.states[t.id], t.id in self.manager.running)
        self._layout_cards()
        self.ecu_list.blockSignals(True)
        self.ecu_list.clear()
        for t in project.targets:
            item = QListWidgetItem(f"{t.name}\n{BUS_LABEL.get(t.transport, t.transport)} · {t.id}")
            item.setData(Qt.ItemDataRole.UserRole, t.id)
            self.ecu_list.addItem(item)
        self.ecu_list.blockSignals(False)
        self._sync_trace_combo()
        self.tools.set_project(project, select or self.current_id)
        self.empty.setVisible(not ids)
        self.cards_scroll.setVisible(bool(ids))
        self._update_title()
        self._update_summary()
        target = select if select in ids else (self.current_id if self.current_id in ids else (ids[0] if ids else None))
        self.current_id = None
        if target:
            self.select_target(target)
        else:
            self.target_editor.setEnabled(False)
            self.fw_editor.setEnabled(False)
            self.seq_tree.clear()
        self._update_actions()

    def _layout_cards(self) -> None:
        if not hasattr(self, "cards_grid"):
            return
        width = self.cards_scroll.viewport().width()
        columns = max(1, width // 520)
        while self.cards_grid.count():
            self.cards_grid.takeAt(0)
        order = [t.id for t in self.project.targets] if self.project else []
        for i, tid in enumerate(order):
            if tid in self.cards:
                self.cards_grid.addWidget(self.cards[tid], i // columns, i % columns, Qt.AlignmentFlag.AlignTop)

    def select_target(self, target_id: str | None) -> None:
        if not target_id or self.project is None or target_id == self.current_id:
            return
        try:
            target = self.project.target(target_id)
        except ProjectError:
            return
        self.current_id = target_id
        for tid, card in self.cards.items():
            card.set_selected(tid == target_id)
        for i in range(self.ecu_list.count()):
            if self.ecu_list.item(i).data(Qt.ItemDataRole.UserRole) == target_id:
                self.ecu_list.blockSignals(True)
                self.ecu_list.setCurrentRow(i)
                self.ecu_list.blockSignals(False)
        raw_target = self._raw_target(target_id)
        editable = not self.manager.running and not self.sealed
        self.target_editor.setEnabled(editable)
        self.fw_editor.setEnabled(editable)
        if raw_target is not None:
            self.target_editor.set_target(raw_target)
            self.fw_editor.set_entries(raw_target.setdefault("firmware", []))
        self._show_sequence(target)
        self._show_template(target)
        self._sync_trace_combo()
        self._update_actions()

    def _raw_target(self, target_id: str) -> dict | None:
        for t in (self.raw or {}).get("targets", []):
            if str(t.get("id")) == target_id:
                return t
        return None

    def _mark_dirty(self) -> None:
        self.dirty = True
        self._update_title()
        self._apply_timer.start()

    def _apply_edits(self) -> None:
        """Re-validate the edited project; refresh cards, list and sequence preview (editors keep their data)."""
        if self.raw is None or self.project_path is None:
            return
        try:
            project = project_from_data(self.raw, self.project_path)
        except Exception as exc:
            self.statusBar().showMessage(f"Project not valid yet: {exc}")
            return
        self.statusBar().showMessage("Unsaved changes – Ctrl+S to save" if self.dirty else "", 6000)
        old_ids = [t.id for t in self.project.targets] if self.project else []
        self.project = project
        new_ids = [t.id for t in project.targets]
        if old_ids != new_ids:  # ECU added, removed or renamed
            selected = self.current_id if self.current_id in new_ids else None
            if selected is None and self.current_id in old_ids and len(old_ids) == len(new_ids):
                selected = new_ids[old_ids.index(self.current_id)]  # renamed
            self.current_id = None
            self._rebuild_targets(selected)
            return
        for t in project.targets:
            self.cards[t.id].set_target(t)
            self._refresh_firmware_rows(t.id)
            item = self.ecu_list.item(new_ids.index(t.id))
            item.setText(f"{t.name}\n{BUS_LABEL.get(t.transport, t.transport)} · {t.id}")
        if self.current_id:
            target = project.target(self.current_id)
            self._show_sequence(target)
            self._show_template(target)
        self.tools.set_project(project)
        self._update_actions()

    def save_project(self) -> bool:
        if self.raw is None or self.project_path is None:
            return False
        try:
            save_project(self.raw, self.project_path)
        except Exception as exc:
            QMessageBox.critical(self, "Project not saved", str(exc))
            return False
        self.dirty = False
        self.open_project(self.project_path, select=self.current_id)
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
        save_project(new_project(os.path.splitext(os.path.basename(path))[0]), path)
        self.dirty = False
        self.current_id = None
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
        self.select_target(f"ecu{n}")
        self.show_page("configure")
        self.tabs.setCurrentWidget(self.target_editor)

    def remove_target(self) -> None:
        tid = self.current_id
        if not tid or self.raw is None:
            return
        if QMessageBox.question(self, "Remove ECU", f"Remove '{tid}' from the project?") != QMessageBox.StandardButton.Yes:
            return
        self.raw["targets"] = [t for t in self.raw["targets"] if str(t.get("id")) != tid]
        self.current_id = None
        self._mark_dirty()
        self._apply_timer.stop()
        self._apply_edits()

    # ---------------------------------------------------------------- release files
    def load_release(self, target_id: str | None = None) -> None:
        tid = target_id or self.current_id
        if not tid or self.manager.running:
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, f"Release file(s) for {tid}", str(self.settings.value("last_release_dir", self._project_dir())),
            FIRMWARE_FILTER)
        if paths:
            self.settings.setValue("last_release_dir", os.path.dirname(paths[0]))
            self.put_release(tid, paths)

    def put_release(self, target_id: str, paths: list[str]) -> None:
        """New release for an ECU: each file replaces the matching firmware entry (options kept) or is added."""
        raw_target = self._raw_target(target_id)
        if raw_target is None or self.project is None or self.manager.running:
            return
        if self.sealed:
            QMessageBox.information(self, "Sealed package", "This project comes from a sealed package and is read-only.")
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
            self.statusBar().showMessage(f"{target_id}: {note}", 8000)
        self.states[target_id] = TargetState()
        self._mark_dirty()
        self._apply_timer.stop()
        self._apply_edits()
        if self.current_id == target_id:
            self.fw_editor.set_entries(entries)
        else:
            self.select_target(target_id)

    # one-click release switching on the cards
    def _history_key(self, target_id: str, index: int) -> str:
        import hashlib

        project = hashlib.sha1((self.project_path or "").encode()).hexdigest()[:12]
        return f"release_history/{project}/{target_id}/{index}"

    def _history(self, target_id: str, index: int) -> list[str]:
        value = self.settings.value(self._history_key(target_id, index), [])
        return [value] if isinstance(value, str) else list(value or [])

    def _refresh_firmware_rows(self, target_id: str) -> None:
        raw_target = self._raw_target(target_id)
        card = self.cards.get(target_id)
        if raw_target is None or card is None or self.project is None:
            return
        base = self.project.base_dir
        rows = []
        for i, entry in enumerate(raw_target.get("firmware") or []):
            if not isinstance(entry, dict):
                entry = {"file": str(entry)}
            rows.append({
                "index": i, "path": entry_path(entry, base), "session": entry.get("session"),
                "provider": lambda e=dict(entry), i=i: release_candidates(e, base, self._history(target_id, i)),
                "newer": newer_release(entry, base), "types": entry_block_types(entry, base),
            })
        card.set_firmware(rows, editable=not self.sealed and target_id not in self.manager.running)

    def choose_release(self, target_id: str, index: int, path: str) -> None:
        """A release picked on a card (one click); "" opens a file dialog for that entry."""
        raw_target = self._raw_target(target_id)
        if raw_target is None or self.project is None or self.manager.running or self.sealed:
            return
        entries = raw_target.setdefault("firmware", [])
        if not 0 <= index < len(entries):
            return
        if not path:
            current = entry_path(entries[index] if isinstance(entries[index], dict) else {"file": entries[index]},
                                 self.project.base_dir)
            path, _ = QFileDialog.getOpenFileName(self, f"Release for {target_id}", os.path.dirname(current),
                                                  FIRMWARE_FILTER)
            if not path:
                return
        try:
            note = put_release_file(entries, path, self.project.base_dir, index, self.fw_editor._choose_session)
        except Exception as exc:
            QMessageBox.warning(self, "Release file", f"{os.path.basename(path)}: {exc}")
            self._refresh_firmware_rows(target_id)
            return
        history = [p for p in self._history(target_id, index) if p != os.path.abspath(path)]
        self.settings.setValue(self._history_key(target_id, index), [os.path.abspath(path)] + history[:9])
        self._log(f"[{target_id}] {note}")
        self.statusBar().showMessage(f"{target_id}: {note}", 8000)
        self.states[target_id] = TargetState()
        self._mark_dirty()
        self._apply_timer.stop()
        self._apply_edits()
        if self.current_id == target_id:
            self.fw_editor.set_entries(entries)

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

    # ---------------------------------------------------------------- packages
    def _export_pack(self) -> None:
        if not self.project_path:
            return
        if self.dirty and not self.save_project():
            return
        try:
            from ..app import package
        except ImportError as exc:
            QMessageBox.warning(self, "Export package", f"Packages are not available: {exc}")
            return
        default = os.path.splitext(self.project_path)[0] + ".ftpack"
        path, _ = QFileDialog.getSaveFileName(self, "Export project package", default, "FlashTool packages (*.ftpack)")
        if not path:
            return
        sealed = QMessageBox.question(
            self, "Export package", "Seal the package (read-only, any change is detected)?") == QMessageBox.StandardButton.Yes
        try:
            out = package.export_pack(self.project_path, path, sealed=sealed)
        except Exception as exc:
            QMessageBox.critical(self, "Export package", str(exc))
            return
        self._log(f"Exported {out}")
        self.statusBar().showMessage(f"Package written: {out}", 8000)

    def _import_pack(self) -> None:
        try:
            from ..app import package
        except ImportError as exc:
            QMessageBox.warning(self, "Import package", f"Packages are not available: {exc}")
            return
        pack, _ = QFileDialog.getOpenFileName(self, "Import project package", self._project_dir(),
                                              "FlashTool packages (*.ftpack)")
        if not pack:
            return
        dest = QFileDialog.getExistingDirectory(self, "Empty folder to unpack into", os.path.dirname(pack))
        if not dest:
            return
        try:
            project_file = package.import_pack(pack, dest)
        except Exception as exc:
            QMessageBox.critical(self, "Import package", str(exc))
            return
        self.open_project(project_file)

    # ---------------------------------------------------------------- sequence views
    def open_sequence_editor(self) -> None:
        if not self.project or not self.current_id:
            QMessageBox.information(self, "FlashTool", "Select an ECU first.")
            return
        target = self.project.target(self.current_id)
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
                if self.current_id == target_id:
                    self.target_editor.set_target(raw_target)
                return
        self._apply_edits()

    def _show_sequence(self, target) -> None:
        self.seq_tree.clear()
        self._step_items.clear()
        try:
            image, seq, warnings = prepare(self.project, target)
        except JobSetupError as exc:
            QTreeWidgetItem(self.seq_tree, ["Not ready", str(exc)])
            return
        root_title = f"{seq['name']}  ({' ← '.join(os.path.basename(s) for s in reversed(seq['sources']))})"
        sections = [(root_title, seq["steps"])]
        sections += [(f"Block sequence {name}", steps) for name, steps in seq["block_sequences"].items()]
        for title, steps in sections + [("Always at the end", seq["finally"])]:
            top = QTreeWidgetItem(self.seq_tree, [title])
            parents = {0: top}
            for depth, step in flatten(steps):
                item = QTreeWidgetItem(parents[depth], [step.get("id", "?"), describe_step(step), "", ""])
                parents[depth + 1] = item
                self._step_items[step.get("id", "?")] = item
        if seq["block_sequences"]:
            blocks = QTreeWidgetItem(self.seq_tree, ["Blocks → block sequence"])
            for b in image.blocks:
                QTreeWidgetItem(blocks, [b.name, f"type {b.type} → {block_sequence_key(seq, b.type) or 'none'}"])
        self.seq_tree.expandAll()
        self.seq_tree.resizeColumnToContents(0)
        for sid, (status, info) in self.states[target.id].steps.items():
            self._paint_step(sid, status, info)
        for w in warnings:
            QTreeWidgetItem(self.seq_tree, ["warning", w])

    def _paint_step(self, sid: str, status: str, info: str) -> None:
        item = self._step_items.get(sid)
        if item is None:
            return
        item.setText(2, status)
        item.setText(3, info)
        item.setForeground(2, QBrush(QColor(status_color(status))))

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
        except Exception as exc:  # SequenceError, OSError, YAML syntax errors
            self.tpl_status.setStyleSheet(f"color: {tokens()['fail']}")
            self.tpl_status.setText(f"Invalid: {exc}")
            return False
        finally:
            if os.path.exists(probe):
                os.remove(probe)
        self.tpl_status.setStyleSheet(f"color: {tokens()['ok']}")
        self.tpl_status.setText(f"Valid: '{seq['name']}', {len(flatten(seq['steps']))} steps + "
                                f"{len(flatten(seq['finally']))} final steps, catalogs: {len(seq['catalogs'])}")
        return True

    def _save_template(self) -> None:
        if not self._tpl_path or is_builtin(self._tpl_path) or not self._validate_template():
            return
        with open(self._tpl_path, "w", encoding="utf-8") as f:
            f.write(self.tpl_edit.toPlainText())
        self._log(f"Saved {self._tpl_path}")
        self._apply_edits()

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
            f.write(f"name: {os.path.splitext(name)[0]}\nextends: {base}\n\nvariables: {{}}\n\npatch: []\n")
        QMessageBox.information(self, "FlashTool", f"Created {path}. Choose it as 'Sequence' in ECU settings.")

    # ---------------------------------------------------------------- trace
    def _sync_trace_combo(self) -> None:
        if self.project is None:
            return
        self.trace_ecu.blockSignals(True)
        self.trace_ecu.clear()
        for t in self.project.targets:
            self.trace_ecu.addItem(t.name, t.id)
        index = self.trace_ecu.findData(self.current_id)
        self.trace_ecu.setCurrentIndex(max(index, 0))
        self.trace_ecu.blockSignals(False)
        self._show_trace(self.trace_ecu.currentData())

    def _show_trace(self, target_id: str | None) -> None:
        if not target_id or target_id not in self.states:
            self.trace_view.clear()
            return
        self.trace_view.setPlainText("\n".join(self.states[target_id].trace))
        self.trace_view.moveCursor(QTextCursor.MoveOperation.End)

    # ======================================================================== jobs
    def flash(self, target_id: str | None) -> None:
        if target_id:
            self._start([target_id])

    def _flash_all(self) -> None:
        if self.project:
            self._start([t.id for t in self.project.targets])

    def _start(self, ids: list[str]) -> None:
        if not self.project or not ids:
            return
        if self.dirty and not self.save_project():  # jobs read the project file from disk
            return
        busy = [i for i in ids if i in self.manager.running]
        if busy:
            QMessageBox.information(self, "FlashTool", f"Already running: {', '.join(busy)}")
            return
        self.tools.release(ids)  # the job opens its own connection to the ECU
        for tid in ids:
            self.states[tid] = TargetState(status="starting")
        if self.current_id in ids:
            self._show_sequence(self.project.target(self.current_id))
        self.manager.start(self.project.path, ids)
        self._log(f"Started {', '.join(ids)}")
        self._refresh_cards(ids)
        self._update_actions()

    def _stop(self) -> None:
        if self.manager.running:
            self.manager.cancel()
            self._log("Cancel requested; post-programming steps will still run")

    def _open_report(self, target_id: str) -> None:
        report = self.states.get(target_id, TargetState()).report
        if report:
            QDesktopServices.openUrl(QUrl.fromLocalFile(report.replace("report.json", "report.html")))

    def _pump_events(self) -> None:
        touched: set[str] = set()
        finished = False
        for e in self.manager.poll(0.0):
            touched.add(e.job)
            self._handle(e)
            finished = finished or isinstance(e, ev.JobFinished)
        if self._pending_trace:
            shown = self.trace_ecu.currentData()
            lines = [line for tid, line in self._pending_trace if tid == shown]
            if lines:
                self.trace_view.appendPlainText("\n".join(lines))
            self._pending_trace.clear()
        if touched:
            self._refresh_cards(touched)
            self._update_actions()
        if finished:
            self._update_summary()
            if self.stack.currentIndex() == self.PAGES.index("reports"):
                self._refresh_reports()

    def _handle(self, e: ev.Event) -> None:
        st = self.states.setdefault(e.job, TargetState())
        if isinstance(e, ev.JobStarted):
            st.status, st.total = "running", e.info.get("total_size", 0)
            self._log(f"[{e.job}] {e.target}: {e.info.get('blocks')} block(s), {st.total} bytes, run dir {e.info.get('run_dir')}")
        elif isinstance(e, ev.StepStarted):
            st.current = e.label
            sid, info = _step_id(e.path)
            st.steps[sid] = ("running", info)
            if e.job == self.current_id:
                self._paint_step(sid, "running", info)
        elif isinstance(e, ev.StepFinished):
            sid, info = _step_id(e.path)
            detail = info + (f"  {e.error}" if e.error else "")
            st.steps[sid] = (e.status, detail)
            if e.job == self.current_id:
                self._paint_step(sid, e.status, detail)
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
            st.error, st.report, st.current = e.error, e.report, ""
            self._log(f"[{e.job}] {'PASSED' if e.success else 'FAILED'} in {e.duration_s:.2f} s"
                      + (f" - {e.error}" if e.error else ""))

    def _refresh_cards(self, ids) -> None:
        running = set(self.manager.running)
        for tid in ids:
            if tid in self.cards and tid in self.states:
                self.cards[tid].set_state(self.states[tid], tid in running)
        self._update_summary()

    def _update_summary(self) -> None:
        if not self.project:
            self.summary.setText("")
            return
        counts = {}
        for t in self.project.targets:
            s = self.states.get(t.id, TargetState()).status
            counts[s] = counts.get(s, 0) + 1
        parts = [f"{len(self.project.targets)} ECU(s)"]
        for key, label in (("running", "flashing"), ("starting", "starting"), ("passed", "passed"), ("failed", "failed")):
            if counts.get(key):
                parts.append(f"{counts[key]} {label}")
        self.summary.setText("   ·   ".join(parts) + "      Drop a release file on a card to replace its firmware.")

    def _update_actions(self) -> None:
        has_project = self.project is not None
        running = bool(self.manager.running)
        has_targets = bool(self.project and self.project.targets)
        self.flash_all_btn.setEnabled(has_targets and not running)
        self.stop_btn.setEnabled(running)
        for key in ("new", "open", "import"):
            self.side_buttons[key].setEnabled(not running)
        self.side_buttons["save"].setEnabled(self.raw is not None and not running and not self.sealed)
        self.side_buttons["export"].setEnabled(has_project and not running)
        editable = not running and not self.sealed
        self.add_btn.setEnabled(self.raw is not None and editable)
        self.remove_btn.setEnabled(bool(self.current_id) and editable)
        self.release_btn.setEnabled(bool(self.current_id) and editable)
        self.seq_editor_btn.setEnabled(bool(self.current_id))

    # ======================================================================== reports
    def _refresh_reports(self) -> None:
        self.runs.setRowCount(0)
        self._run_paths = []
        if not self.project:
            return
        rows = []
        for path in glob.glob(os.path.join(self.project.output_dir, "*", "*", "report.json")):
            try:
                with open(path, encoding="utf-8") as f:
                    r = json.load(f)
            except (OSError, ValueError):
                continue
            rows.append((r.get("started", ""), path, r))
        rows.sort(reverse=True)
        self.runs.setRowCount(len(rows))
        t = tokens()
        for i, (started, path, r) in enumerate(rows):
            ok = r.get("success")
            values = [r.get("target", ""), started.replace("T", "  ")[:20], "PASSED" if ok else "FAILED",
                      f"{r.get('duration_s', 0):.2f} s", str(r.get("firmware", {}).get("total_size", "")),
                      (r.get("error") or "").splitlines()[0] if r.get("error") else ""]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 2:
                    item.setForeground(QBrush(QColor(t["ok"] if ok else t["fail"])))
                self.runs.setItem(i, col, item)
            self._run_paths.append(path)
        self.runs.resizeColumnsToContents()
        self.chart.set_rows([])
        self.run_info.setText(f"{len(rows)} run(s) in {self.project.output_dir}" if rows else "No runs yet")

    def _selected_run(self) -> str | None:
        rows = self.runs.selectionModel().selectedRows() if self.runs.selectionModel() else []
        return self._run_paths[rows[0].row()] if rows and rows[0].row() < len(self._run_paths) else None

    def _show_run(self) -> None:
        path = self._selected_run()
        if not path:
            return
        with open(path, encoding="utf-8") as f:
            r = json.load(f)
        steps = [s for s in r.get("steps", []) if "/" not in s["path"] or
                 (s["path"].startswith("finally/") and s["path"].count("/") == 1)]
        self.chart.set_rows([(s["path"].replace("finally/", "end: "), s["duration_s"], s["status"]) for s in steps])
        total = r.get("firmware", {}).get("total_size", 0)
        transfer = sum(s["duration_s"] for s in r.get("steps", []) if s["kind"] == "download")
        speed = f", transfer {total / transfer / 1024:.1f} KB/s" if transfer else ""
        self.run_info.setText(f"{r.get('target')}: {r.get('duration_s', 0):.2f} s total, {total} bytes{speed}. "
                              f"Sequence '{r.get('sequence', {}).get('name', '')}'.")

    def _open_selected_report(self) -> None:
        path = self._selected_run()
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path.replace("report.json", "report.html")))

    # ======================================================================== misc
    def _detect_hw(self) -> None:
        channels = list_channels()
        self.hw_list.setPlainText(
            "\n".join(", ".join(f"{k}={v}" for k, v in c.items()) for c in channels)
            or "No channels detected. Vector VN needs Windows with the Vector driver setup; "
               "SocketCAN needs Linux. Simulated ECUs use the virtual bus."
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
        self.tools.shutdown()
        self.manager.shutdown()
        event.accept()


def run(project_path: str | None = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("FlashTool")
    win = MainWindow(project_path)
    win.show()
    return app.exec()
