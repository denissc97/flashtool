"""Visual sequence editor: palette of step types, step tree with drag & drop, property form."""

from __future__ import annotations

import copy
import os
import re

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton, QScrollArea, QSplitter,
    QTableWidget, QTableWidgetItem, QTabWidget, QToolBar, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..app.project_io import HexInt, save_sequence, sequence_for_editing, validate_sequence_data
from ..diagdb.catalog import CatalogError, load_catalog
from ..sequence.loader import BUILTIN_DIR, describe_step, is_builtin, step_kind
from .forms import DictForm, F, Section

ROLE = Qt.ItemDataRole.UserRole
CONTAINER = "__container__"  # sections ("steps", "finally") and the else branch of an if


class _Ref:
    """Holds a step dict on a tree item. Qt would hand back a copy of a plain dict."""

    __slots__ = ("body",)

    def __init__(self, body: dict):
        self.body = body


def _body(item: QTreeWidgetItem) -> dict:
    ref = item.data(0, ROLE)
    return ref.body if isinstance(ref, _Ref) else {}

# (kind, label, description, default step body)
STEP_TYPES = [
    ("session", "Diagnostic session", "0x10 - default / programming / extended session", {"session": HexInt(0x03)}),
    ("security", "Security access", "0x27 seed & key", {"security": {"level": HexInt(0x01)}}),
    ("authenticate", "Authentication (certificate)", "0x29 APCE with the target's certificate provider",
     {"authenticate": {"challenge_client_length": 16}}),
    ("request", "Raw request", "Any UDS request as hex bytes; ${expressions} allowed", {"request": "22 F1 90"}),
    ("service", "Catalog service", "Named service from an ODX-D / YAML catalog", {"service": ""}),
    ("download", "Download block", "0x34 / 0x36 x N / 0x37 for one firmware block", {"download": "block"}),
    ("foreach", "For each block", "Repeat the inner steps for every firmware block",
     {"foreach": "block", "in": "image.blocks", "steps": []}),
    ("if", "Condition", "Run the inner steps only when an expression is true", {"if": "True", "steps": []}),
    ("tester_present", "Tester Present", "Start / stop periodic 3E 80", {"tester_present": True}),
    ("delay_ms", "Delay", "Wait a fixed time", {"delay_ms": 100}),
    ("wait_ecu", "Wait for ECU", "Poll until the ECU answers (after reset / boot)", {"wait_ecu": {"timeout_ms": 5000}}),
    ("reset", "ECU reset", "0x11 hard / key-off-on / soft reset", {"reset": HexInt(0x01)}),
    ("read_did", "Read DID", "0x22, value can be stored in a variable",
     {"read_did": {"did": HexInt(0xF189), "store": "value"}}),
    ("write_did", "Write DID", "0x2E", {"write_did": {"did": HexInt(0xF15A), "data": "00"}}),
    ("log", "Log message", "Write a line to the trace and the log", {"log": "message"}),
    ("script", "Custom action (Python)", "Run a Python function (coding, backup, checks); the ECU must allow it",
     {"script": "actions/custom_action.py", "function": "run"}),
]
TYPE_LABEL = {k: label for k, label, _, _ in STEP_TYPES}

PRESETS = {
    "Erase + download + check (all blocks)": {
        "foreach": "block", "in": "image.blocks", "on_fail": "retry", "retries": 1, "steps": [
            {"id": "erase", "request": "31 01 FF 00 ${addr_size(block.address_and_length_format, block.start, block.end - block.start)}",
             "expect_data": "71 01 FF 00 00"},
            {"id": "download", "download": "block"},
            {"id": "check_memory", "request": "31 01 02 02 ${u32(block.crc32)}", "expect_data": "71 01 02 02 00"},
        ]},
}

COMMON = Section("General", [
    F("id", "Id"),
    F("name", "Label"),
    F("functional", "Functional addressing", "bool"),
    F("group", "Once per bus", "choice", ["", "once"], tip="Several ECUs on one bus: run this step only once"),
    F("when", "Run only when", "expr", tip="Expression, e.g. fingerprint is not None"),
    F("on_fail", "On failure", "choice", ["", "abort", "continue", "retry"]),
    F("retries", "Retries", "int"),
])
RESPONSE = Section("Response", [
    F("timeout_ms", "Response timeout (ms)", "int"),
    F("allow_nrc", "Accepted NRCs", "hexlist"),
    F("expect_data", "Response must start with", "bytes"),
    F("store", "Store response in variable"),
])
TRANSPORT = Section("Transport for this step", [
    F("transport.stmin_rx", "STmin (CAN)", "hex", tip="0x00-0x7F ms, 0xF1-0xF9 = 100-900 µs"),
    F("transport.block_size", "Block size (CAN)", "int"),
    F("transport.stmin_tx_override_ms", "Override ECU STmin (ms)", "float"),
    F("transport.n_cs_ms", "Pause between frames (LIN, ms)", "float"),
])

KIND_FIELDS = {
    "session": [F("session", "Session", "hex", tip="0x01 default, 0x02 programming, 0x03 extended")],
    "security": [F("security.level", "Level (requestSeed)", "hex"), F("security.variant", "Variant")],
    "authenticate": [
        F("authenticate.check_configuration", "Check configuration (29 08)", "boolchoice"),
        F("authenticate.challenge_client_length", "Client challenge (bytes)", "int"),
        F("authenticate.sign", "Signed data", "choice", ["", "challenge_server", "challenge_server+challenge_client"]),
    ],
    "request": [F("request", "Request", "bytes", tip="Hex bytes, e.g. 31 01 FF 00 ${u32(block.crc32)}")],
    "download": [F("download", "Block", "expr"), F("max_block_length", "Max block length", "int")],
    "foreach": [F("foreach", "Loop variable"), F("in", "Over", "expr")],
    "if": [F("if", "Condition", "expr")],
    "tester_present": [F("tester_present", "Tester Present", "onoff")],
    "delay_ms": [F("delay_ms", "Delay (ms)", "int")],
    "wait_ecu": [F("wait_ecu.timeout_ms", "Timeout (ms)", "int"), F("wait_ecu.interval_ms", "Interval (ms)", "int"),
                 F("wait_ecu.request", "Probe request", "bytes")],
    "reset": [F("reset", "Reset type", "hex", tip="0x01 hard, 0x02 key off/on, 0x03 soft"),
              F("wait_ms", "Wait after reset (ms)", "int")],
    "read_did": [F("read_did.did", "DID", "hex"), F("read_did.store", "Store in variable")],
    "write_did": [F("write_did.did", "DID", "hex"), F("write_did.data", "Data", "bytes")],
    "log": [F("log", "Message")],
    "script": [F("script", "Python file", "file", file_filter="Python (*.py)"), F("function", "Function"),
               F("args", "Arguments", "params", tip="name=value; name=value – available as ctx.args")],
}
RESPONSE_KINDS = {"request", "service", "session", "reset", "security", "read_did", "write_did", "authenticate"}


class SequenceEditor(QMainWindow):
    """Edits one sequence file. `saved(path)` fires after a successful save."""

    saved = Signal(str)

    def __init__(self, project_dir: str, path: str | None = None, parent=None):
        super().__init__(parent)
        self.project_dir = project_dir
        self.path: str | None = None
        self.data: dict = {}
        self.dirty = False
        self._services: list[str] = []
        self._forms: dict[str, DictForm] = {}
        self._building = False
        self.resize(1250, 780)

        self._build_toolbar()
        self._build_body()
        self._validate_timer = QTimer(self, singleShot=True, interval=400)
        self._validate_timer.timeout.connect(self.validate)
        if path:
            self.open(path)
        else:
            self.new_sequence(from_standard=True)

    # -- layout ------------------------------------------------------------------
    def _build_toolbar(self) -> None:
        tb = QToolBar("Sequence")
        tb.setMovable(False)
        self.addToolBar(tb)

        def act(text, slot, shortcut=None):
            a = QAction(text, self)
            a.triggered.connect(slot)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            tb.addAction(a)
            return a

        new_menu = QMenu(self)
        new_menu.addAction("From the standard UDS sequence", lambda: self.new_sequence(True))
        new_menu.addAction("Empty", lambda: self.new_sequence(False))
        new_btn = QAction("New", self)
        new_btn.setMenu(new_menu)
        new_btn.triggered.connect(lambda: self.new_sequence(True))
        tb.addAction(new_btn)
        act("Open…", self._choose_file, "Ctrl+O")
        act("Save", self.save, "Ctrl+S")
        act("Save as…", lambda: self.save(ask=True), "Ctrl+Shift+S")
        tb.addSeparator()
        add_menu = QMenu(self)
        for kind, label, _, _ in STEP_TYPES:
            add_menu.addAction(label, lambda k=kind: self.add_step(k))
        add_menu.addSeparator()
        for name in PRESETS:
            add_menu.addAction(name, lambda n=name: self.add_preset(n))
        add = QAction("Add step", self)
        add.setMenu(add_menu)
        add.triggered.connect(lambda: self.add_step("request"))
        tb.addAction(add)
        act("Duplicate", self.duplicate, "Ctrl+D")
        act("Delete", self.delete, "Del")
        act("Move up", lambda: self.move(-1), "Alt+Up")
        act("Move down", lambda: self.move(1), "Alt+Down")

    def _build_body(self) -> None:
        self.palette = QListWidget()
        for kind, label, desc, _ in STEP_TYPES:
            item = QListWidgetItem(label)
            item.setToolTip(desc)
            item.setData(ROLE, kind)
            self.palette.addItem(item)
        for name in PRESETS:
            item = QListWidgetItem(f"▸ {name}")
            item.setData(ROLE, f"preset:{name}")
            self.palette.addItem(item)
        self.palette.itemDoubleClicked.connect(self._palette_add)
        pal = QWidget()
        pal_layout = QVBoxLayout(pal)
        pal_layout.setContentsMargins(0, 0, 0, 0)
        pal_layout.addWidget(QLabel("Step types (double-click to add):"))
        pal_layout.addWidget(self.palette)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Step", "Type", "Definition"])
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.tree.itemSelectionChanged.connect(self._on_select)
        self.tree.model().rowsMoved.connect(lambda *_: self._mark_dirty())
        self.tree.model().rowsInserted.connect(lambda *_: None if self._building else self._mark_dirty())

        # right side: step properties, variables, catalogs, info
        self.side = QTabWidget()
        self.step_area = QScrollArea()
        self.step_area.setWidgetResizable(True)
        self.step_hint = QLabel("Select a step to edit it.")
        self.step_area.setWidget(self.step_hint)
        self.side.addTab(self.step_area, "Step")

        self.var_table = QTableWidget(0, 2)
        self.var_table.setHorizontalHeaderLabels(["Variable", "Value"])
        self.var_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.var_table.itemChanged.connect(self._vars_edited)
        var_page = QWidget()
        vl = QVBoxLayout(var_page)
        vl.addWidget(self.var_table)
        vb = QHBoxLayout()
        for text, slot in (("Add", self._var_add), ("Remove", self._var_remove)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            vb.addWidget(b)
        vb.addStretch(1)
        vl.addLayout(vb)
        vl.addWidget(QLabel("Values: 0x.. numbers, true/false, text, or ${expression}."))
        self.side.addTab(var_page, "Variables")

        self.cat_list = QListWidget()
        cat_page = QWidget()
        cl = QVBoxLayout(cat_page)
        cl.addWidget(QLabel("Diagnostic catalogs (named services):"))
        cl.addWidget(self.cat_list)
        cb = QHBoxLayout()
        for text, slot in (("Add…", self._cat_add), ("Remove", self._cat_remove)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            cb.addWidget(b)
        cb.addStretch(1)
        cl.addLayout(cb)
        self.services_label = QLabel("")
        self.services_label.setWordWrap(True)
        cl.addWidget(self.services_label)
        self.side.addTab(cat_page, "Catalogs")

        info = QWidget()
        il = QFormLayout(info)
        self.name_edit = QLineEdit()
        self.name_edit.editingFinished.connect(self._info_edited)
        self.desc_edit = QLineEdit()
        self.desc_edit.editingFinished.connect(self._info_edited)
        il.addRow("Name", self.name_edit)
        il.addRow("Description", self.desc_edit)
        self.side.addTab(info, "Sequence")

        self.status = QLabel("")
        self.status.setWordWrap(True)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(pal)
        split.addWidget(self.tree)
        split.addWidget(self.side)
        split.setSizes([220, 620, 410])
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(split, 1)
        layout.addWidget(self.status, 0)
        self.setCentralWidget(central)

    # -- file handling -------------------------------------------------------------
    def new_sequence(self, from_standard: bool) -> None:
        if not self._confirm_discard():
            return
        if from_standard:
            data, _ = sequence_for_editing(os.path.join(BUILTIN_DIR, "uds_standard.yaml"))
            data["name"] = "Custom programming sequence"
        else:
            data = {"name": "Custom programming sequence", "variables": {}, "steps": [], "finally": []}
        self._load(data, None)

    def _choose_file(self) -> None:
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open sequence", self.project_dir, "Sequences (*.yaml *.yml)")
        if path:
            self.open(path, confirm=False)

    def open(self, path: str, confirm: bool = True) -> None:
        if confirm and not self._confirm_discard():
            return
        try:
            data, flattened = sequence_for_editing(path)
        except Exception as exc:
            QMessageBox.critical(self, "Cannot open sequence", str(exc))
            return
        target = None if is_builtin(path) or flattened else path
        self._load(data, target)
        if is_builtin(path):
            self._set_status("Built-in template opened as a copy: use Save to store it in the project folder.", "warning")
        elif flattened:
            self._set_status("This template extends another one; the editor shows the complete sequence. "
                             "Saving writes a stand-alone copy.", "warning")

    def _load(self, data: dict, path: str | None) -> None:
        self.data = data
        self.data.setdefault("variables", {})
        self.data.setdefault("steps", [])
        self.data.setdefault("finally", [])
        self.path = path
        self._rebuild_tree()
        self._fill_vars()
        self._fill_catalogs()
        self.name_edit.setText(str(data.get("name", "")))
        self.desc_edit.setText(str(data.get("description", "")))
        self.dirty = False
        self._update_title()
        self.validate()

    def _confirm_discard(self) -> bool:
        if not self.dirty:
            return True
        answer = QMessageBox.question(self, "Unsaved changes", "Discard the changes to the current sequence?")
        return answer == QMessageBox.StandardButton.Yes

    def save(self, ask: bool = False) -> bool:
        data = self.collect()
        path = self.path
        if ask or path is None:
            default = os.path.join(self.project_dir, _file_name(str(data.get("name", "sequence"))))
            path, _ = QFileDialog.getSaveFileName(self, "Save sequence", default, "Sequences (*.yaml)")
            if not path:
                return False
            if not path.endswith((".yaml", ".yml")):
                path += ".yaml"
        try:
            save_sequence(data, path)
        except Exception as exc:
            self._set_status(f"Not saved: {exc}", "failed")
            QMessageBox.critical(self, "Sequence not saved", str(exc))
            return False
        self.path = path
        self.dirty = False
        self._update_title()
        self._set_status(f"Saved {path}", "ok")
        self.saved.emit(path)
        return True

    def _update_title(self) -> None:
        name = os.path.basename(self.path) if self.path else "unsaved sequence"
        self.setWindowTitle(f"Sequence editor – {name}{' *' if self.dirty else ''}")

    def closeEvent(self, event) -> None:
        if self._confirm_discard():
            event.accept()
        else:
            event.ignore()

    # -- tree <-> data -------------------------------------------------------------
    def _rebuild_tree(self) -> None:
        self._building = True
        self.tree.clear()
        root = self.tree.invisibleRootItem()
        root.setFlags(root.flags() & ~Qt.ItemFlag.ItemIsDropEnabled)
        for section, title in (("steps", "Steps"), ("finally", "Always at the end (also after errors)")):
            top = QTreeWidgetItem(self.tree, [title])
            top.setData(0, ROLE, _Ref({CONTAINER: section}))
            top.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDropEnabled)
            for step in self.data.get(section) or []:
                self._add_item(top, step)
            top.setExpanded(True)
        self.tree.expandAll()
        self.tree.resizeColumnToContents(0)
        self.tree.resizeColumnToContents(1)
        self._building = False

    def _add_item(self, parent: QTreeWidgetItem, step: dict, index: int | None = None) -> QTreeWidgetItem:
        body = {k: v for k, v in step.items() if k not in ("steps", "else")}
        item = QTreeWidgetItem()
        item.setData(0, ROLE, _Ref(body))
        kind = step_kind(step)
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled
        if kind in ("foreach", "if"):
            flags |= Qt.ItemFlag.ItemIsDropEnabled
        item.setFlags(flags)
        if index is None:
            parent.addChild(item)
        else:
            parent.insertChild(index, item)
        for child in step.get("steps") or []:
            self._add_item(item, child)
        if kind == "if":
            other = QTreeWidgetItem(item, ["else"])
            other.setData(0, ROLE, _Ref({CONTAINER: "else"}))
            other.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDropEnabled)
            for child in step.get("else") or []:
                self._add_item(other, child)
        self._label(item)
        item.setExpanded(True)
        return item

    def _label(self, item: QTreeWidgetItem) -> None:
        body = _body(item)
        try:
            kind = step_kind(body)
        except Exception:
            kind = "?"
        item.setText(0, str(body.get("id", kind)))
        item.setText(1, TYPE_LABEL.get(kind, kind))
        item.setText(2, describe_step(body) if kind != "?" else "")

    @staticmethod
    def _is_container(item: QTreeWidgetItem | None) -> bool:
        return item is not None and CONTAINER in _body(item)

    def _build_step(self, item: QTreeWidgetItem) -> dict:
        step = copy.deepcopy(_body(item))
        kind = step_kind(step)
        if kind in ("foreach", "if"):
            inner, other = [], None
            for i in range(item.childCount()):
                child = item.child(i)
                if self._is_container(child):
                    other = [self._build_step(child.child(j)) for j in range(child.childCount())]
                else:
                    inner.append(self._build_step(child))
            step["steps"] = inner
            if kind == "if" and other:
                step["else"] = other
        return step

    def collect(self) -> dict:
        """Current sequence as data (tree is the source of truth for the steps)."""
        data = {k: v for k, v in self.data.items() if k not in ("steps", "finally")}
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            section = _body(top)[CONTAINER]
            data[section] = [self._build_step(top.child(j)) for j in range(top.childCount())]
        ordered = {k: data[k] for k in ("name", "description", "catalog", "variables", "steps", "finally") if k in data}
        ordered.update({k: v for k, v in data.items() if k not in ordered})
        return ordered

    # -- step editing ----------------------------------------------------------------
    def _selected(self) -> QTreeWidgetItem | None:
        items = self.tree.selectedItems()
        return items[0] if items else None

    def _new_id(self, kind: str) -> str:
        used = set()

        def walk(item):
            body = _body(item)
            if "id" in body:
                used.add(body["id"])
            for i in range(item.childCount()):
                walk(item.child(i))

        walk(self.tree.invisibleRootItem())
        n = 1
        while f"{kind}_{n}" in used:
            n += 1
        return f"{kind}_{n}"

    def _insert(self, step: dict) -> QTreeWidgetItem:
        sel = self._selected()  # new step goes after the selected one, or at the end of a selected section
        if sel is None:
            parent, index = self.tree.topLevelItem(0), None
        elif self._is_container(sel):
            parent, index = sel, None
        else:
            parent, index = sel.parent(), sel.parent().indexOfChild(sel) + 1
        item = self._add_item(parent, step, index)
        self.tree.setCurrentItem(item)
        self._mark_dirty()
        return item

    def add_step(self, kind: str) -> QTreeWidgetItem:
        body = copy.deepcopy(next(d for k, _, _, d in STEP_TYPES if k == kind))
        if kind == "service" and self._services:
            body["service"] = self._services[0]
        return self._insert({"id": self._new_id(kind), **body})

    def add_preset(self, name: str) -> QTreeWidgetItem:
        step = copy.deepcopy(PRESETS[name])
        step["id"] = self._new_id("blocks")
        return self._insert(step)

    def _palette_add(self, item: QListWidgetItem) -> None:
        key = item.data(ROLE)
        if key.startswith("preset:"):
            self.add_preset(key[7:])
        else:
            self.add_step(key)

    def duplicate(self) -> None:
        sel = self._selected()
        if sel is None or self._is_container(sel):
            return
        step = self._build_step(sel)

        def fresh_ids(s: dict) -> None:
            if "id" in s:
                s["id"] = self._new_id(step_kind(s))
            for sub in (s.get("steps") or []) + (s.get("else") or []):
                fresh_ids(sub)

        fresh_ids(step)
        item = self._add_item(sel.parent(), step, sel.parent().indexOfChild(sel) + 1)
        self.tree.setCurrentItem(item)
        self._mark_dirty()

    def delete(self) -> None:
        sel = self._selected()
        if sel is None or self._is_container(sel):
            return
        sel.parent().removeChild(sel)
        self._mark_dirty()

    def move(self, delta: int) -> None:
        sel = self._selected()
        if sel is None or self._is_container(sel):
            return
        parent = sel.parent()
        index = parent.indexOfChild(sel)
        new = index + delta
        if not 0 <= new < parent.childCount() or self._is_container(parent.child(new)):
            return
        expanded = sel.isExpanded()
        parent.takeChild(index)
        parent.insertChild(new, sel)
        sel.setExpanded(expanded)
        self.tree.setCurrentItem(sel)
        self._mark_dirty()

    def _on_select(self) -> None:
        sel = self._selected()
        if sel is None or self._is_container(sel):
            self.step_area.takeWidget()
            self.step_hint = QLabel("Select a step to edit it, or add one from the palette.")
            self.step_area.setWidget(self.step_hint)
            return
        body = _body(sel)
        kind = step_kind(body)
        form = self._form_for(kind)
        form.set_data(body)
        if self.step_area.widget() is not form:
            self.step_area.takeWidget()
            self.step_area.setWidget(form)
        self.side.setCurrentIndex(0)

    def _form_for(self, kind: str) -> DictForm:
        if kind not in self._forms:
            fields = list(KIND_FIELDS.get(kind, []))
            if kind == "service":
                fields = [F("service", "Service", "combo", lambda _d: self._services),
                          F("params", "Parameters", "params", tip="name=value; name=value")]
            sections = [Section(TYPE_LABEL.get(kind, kind), fields), COMMON]
            if kind in RESPONSE_KINDS:
                sections.append(RESPONSE)
            if kind in ("download", "foreach", "request", "service"):
                sections.append(TRANSPORT)
            form = DictForm(sections, lambda: self.project_dir)
            form.changed.connect(self._step_edited)
            self._forms[kind] = form
        return self._forms[kind]

    def _step_edited(self) -> None:
        sel = self._selected()
        if sel is not None:
            self._label(sel)
        self._mark_dirty()

    # -- variables / catalogs / info -----------------------------------------------------
    def _fill_vars(self) -> None:
        self.var_table.blockSignals(True)
        variables = self.data.get("variables") or {}
        self.var_table.setRowCount(len(variables))
        for row, (name, value) in enumerate(variables.items()):
            self.var_table.setItem(row, 0, QTableWidgetItem(str(name)))
            self.var_table.setItem(row, 1, QTableWidgetItem(_value_text(value)))
        self.var_table.blockSignals(False)

    def _vars_edited(self, *_):
        variables = {}
        for row in range(self.var_table.rowCount()):
            name = self.var_table.item(row, 0)
            value = self.var_table.item(row, 1)
            if name and name.text().strip():
                variables[name.text().strip()] = _parse_value(value.text() if value else "")
        self.data["variables"] = variables
        self._mark_dirty()

    def _var_add(self) -> None:
        row = self.var_table.rowCount()
        self.var_table.insertRow(row)
        self.var_table.setItem(row, 0, QTableWidgetItem(f"var_{row + 1}"))
        self.var_table.setItem(row, 1, QTableWidgetItem("0x00"))
        self._vars_edited()

    def _var_remove(self) -> None:
        row = self.var_table.currentRow()
        if row >= 0:
            self.var_table.removeRow(row)
            self._vars_edited()

    def _catalog_refs(self) -> list:
        refs = self.data.get("catalog") or []
        return refs if isinstance(refs, list) else [refs]

    def _fill_catalogs(self) -> None:
        self.cat_list.clear()
        services: list[str] = []
        problems = []
        for ref in self._catalog_refs():
            text = f"{ref['file']}#{ref.get('layer')}" if isinstance(ref, dict) else str(ref)
            self.cat_list.addItem(text)
            try:
                services += sorted(load_catalog(ref, self._base_dir()).services)
            except CatalogError as exc:
                problems.append(str(exc))
        self._services = services
        self.services_label.setText(("Services: " + ", ".join(services) if services else "No services loaded.")
                                    + ("\n" + "\n".join(problems) if problems else ""))

    def _base_dir(self) -> str:
        return os.path.dirname(self.path) if self.path else self.project_dir

    def _cat_add(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Add diagnostic catalog", self._base_dir(),
                                              "Catalogs (*.odx-d *.odx *.pdx *.yaml *.yml);;All files (*)")
        if not path:
            return
        rel = os.path.relpath(path, self._base_dir())
        ref: str | dict = path if rel.startswith("..") else rel
        try:
            load_catalog(ref, self._base_dir())
        except CatalogError as exc:
            layers = re.search(r"'layer': (.+)$", str(exc))
            if not layers:
                QMessageBox.warning(self, "Catalog", str(exc))
                return
            names = [n.strip() for n in layers.group(1).split(",")]
            layer, ok = QInputDialog.getItem(self, "Diagnostic layer", "Layer:", names, 0, False)
            if not ok:
                return
            ref = {"file": ref, "layer": layer}
        self.data["catalog"] = self._catalog_refs() + [ref]
        self._fill_catalogs()
        self._mark_dirty()

    def _cat_remove(self) -> None:
        row = self.cat_list.currentRow()
        refs = self._catalog_refs()
        if 0 <= row < len(refs):
            del refs[row]
            self.data["catalog"] = refs
            self._fill_catalogs()
            self._mark_dirty()

    def _info_edited(self) -> None:
        name, desc = self.name_edit.text().strip(), self.desc_edit.text().strip()
        if (name, desc) != (self.data.get("name", ""), self.data.get("description", "")):
            self.data["name"] = name
            self.data["description"] = desc
            self._mark_dirty()

    # -- validation ------------------------------------------------------------------------
    def _mark_dirty(self) -> None:
        if self._building:
            return
        self.dirty = True
        self._update_title()
        self._validate_timer.start()

    def validate(self) -> bool:
        probe_path = self.path or os.path.join(self.project_dir, "unsaved.yaml")
        try:
            seq = validate_sequence_data(self.collect(), probe_path)
        except Exception as exc:
            self._set_status(f"Invalid: {exc}", "failed")
            return False
        n_steps = sum(1 for _ in _walk(seq["steps"]))
        self._set_status(f"Valid: {n_steps} steps, {sum(1 for _ in _walk(seq['finally']))} final steps", "ok")
        return True

    def _set_status(self, text: str, kind: str) -> None:
        color = {"ok": "#2f7d5b", "failed": "#b42318", "warning": "#b7791f"}[kind]
        self.status.setStyleSheet(f"color: {color}")
        self.status.setText(text)


def _walk(steps: list):
    for s in steps:
        yield s
        yield from _walk((s.get("steps") or []) + (s.get("else") or []))


def _file_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name.strip().lower()).strip("_") + ".yaml"


def _value_text(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, int):
        return f"0x{value:02X}"
    return str(value)


def _parse_value(text: str):
    text = text.strip()
    if text in ("", "null"):
        return None
    if text in ("true", "false"):
        return text == "true"
    try:
        return HexInt(int(text, 0)) if text.lower().startswith("0x") else int(text)
    except ValueError:
        return text
