"""ECU tools page: connect to one ECU, wake it up, keep it awake, read its identification."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..app.project import Project, ProjectError
from ..app.tools import EcuSession, IdentRow
from ..uds.trace import TraceEntry
from .theme import repolish
from .widgets import pill


class _Bridge(QObject):
    """Brings results from the worker thread back to the GUI thread."""

    done = Signal(object, object)  # callback, result
    failed = Signal(str)
    trace = Signal(str)


def _frames_text(frames: list) -> str:
    return ", ".join(f"0x{int(f['id']):X} [{f.get('data', '')}]" + (f" @{f['period_ms']} ms" if "period_ms" in f else "")
                     for f in frames) or "none configured"


class ToolsPage(QWidget):
    def __init__(self, log: Callable[[str], None], mono: QFont, parent=None):
        super().__init__(parent)
        self.setObjectName("page")
        self._log = log
        self.project: Project | None = None
        self.session: EcuSession | None = None
        self._busy = False
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ecu-tools")
        self._bridge = _Bridge()
        self._bridge.done.connect(lambda cb, result: cb(result))
        self._bridge.failed.connect(self._failed)
        self._bridge.trace.connect(lambda line: self.trace.appendPlainText(line))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(14)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        title = QLabel("ECU tools")
        title.setProperty("role", "h1")
        sub = QLabel("Wake an ECU up, keep it awake and read its identification – without flashing")
        sub.setProperty("role", "muted")
        titles.addWidget(title)
        titles.addWidget(sub)
        header.addLayout(titles)
        header.addStretch(1)
        self.ecu = QComboBox()
        self.ecu.setMinimumWidth(240)
        self.ecu.currentIndexChanged.connect(lambda _i: self._ecu_changed())
        self.state = pill("Disconnected", "idle")
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setProperty("kind", "primary")
        self.connect_btn.clicked.connect(self.toggle_connection)
        for w in (QLabel("ECU"), self.ecu, self.state, self.connect_btn):
            header.addWidget(w, alignment=Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(header)

        # network card
        net = QFrame()
        net.setObjectName("card")
        nl = QVBoxLayout(net)
        nl.setContentsMargins(16, 14, 16, 14)
        h = QLabel("Network")
        h.setProperty("role", "h2")
        nl.addWidget(h)
        self.wake_info = QLabel()
        self.wake_info.setProperty("role", "muted")
        self.keep_info = QLabel()
        self.keep_info.setProperty("role", "muted")
        row = QHBoxLayout()
        self.wake_btn = QPushButton("Wake up")
        self.wake_btn.clicked.connect(self.wake_up)
        self.keep_btn = QPushButton("Start keep-alive")
        self.keep_btn.setCheckable(True)
        self.keep_btn.clicked.connect(self.toggle_keep_alive)
        row.addWidget(self.wake_btn)
        row.addWidget(self.wake_info, 1)
        nl.addLayout(row)
        row2 = QHBoxLayout()
        row2.addWidget(self.keep_btn)
        row2.addWidget(self.keep_info, 1)
        nl.addLayout(row2)
        layout.addWidget(net)

        # identification card
        ident = QFrame()
        ident.setObjectName("card")
        il = QVBoxLayout(ident)
        il.setContentsMargins(16, 14, 16, 14)
        top = QHBoxLayout()
        h2 = QLabel("Identification")
        h2.setProperty("role", "h2")
        top.addWidget(h2)
        top.addStretch(1)
        self.did_edit = QLineEdit()
        self.did_edit.setPlaceholderText("DID, e.g. F1A0")
        self.did_edit.setMaximumWidth(140)
        self.did_edit.returnPressed.connect(self.read_one)
        self.read_one_btn = QPushButton("Read DID")
        self.read_one_btn.clicked.connect(self.read_one)
        self.copy_btn = QPushButton("Copy")
        self.copy_btn.clicked.connect(self.copy_table)
        self.read_btn = QPushButton("Read identification")
        self.read_btn.setProperty("kind", "primary")
        self.read_btn.clicked.connect(self.read_identification)
        for w in (self.did_edit, self.read_one_btn, self.copy_btn, self.read_btn):
            top.addWidget(w)
        il.addLayout(top)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["DID", "Name", "Value", "Raw / status"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        il.addWidget(self.table, 1)

        self.trace = QPlainTextEdit(readOnly=True)
        self.trace.setFont(mono)
        self.trace.setMaximumBlockCount(5000)
        self.trace.setPlaceholderText("UDS traffic of this session")
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(ident)
        split.addWidget(self.trace)
        split.setSizes([420, 160])
        layout.addWidget(split, 1)
        self._update()

    # -- project / selection --------------------------------------------------------------
    def set_project(self, project: Project | None, select: str | None = None) -> None:
        current = self.ecu.currentData()
        self.project = project
        if self.session and (project is None or self.session.target.id not in [t.id for t in project.targets]):
            self.disconnect()
        self.ecu.blockSignals(True)
        self.ecu.clear()
        for t in project.targets if project else []:
            self.ecu.addItem(f"{t.name}  [{t.id}]", t.id)
        index = self.ecu.findData(select or current)
        self.ecu.setCurrentIndex(max(index, 0))
        self.ecu.blockSignals(False)
        self._ecu_changed()

    def _target(self):
        tid = self.ecu.currentData()
        if not self.project or not tid:
            return None
        try:
            return self.project.target(tid)
        except ProjectError:
            return None

    def _ecu_changed(self) -> None:
        if self.session and self.session.target.id != self.ecu.currentData():
            self.disconnect()
        t = self._target()
        if t is not None:
            wake = t.wakeup.get("frames") or (t.can.cyclic if t.can else [])
            if t.transport == "lin":
                self.wake_info.setText("LIN wake-up signal")
            elif t.transport == "doip":
                self.wake_info.setText("DoIP: needs the activation line of the interface")
            else:
                self.wake_info.setText(f"Sends {_frames_text(wake)}"
                                       + (" – also before every flash" if t.wakeup.get("before_flash") else ""))
            keep = t.can.cyclic if t.can else []
            self.keep_info.setText(f"Cyclic: {_frames_text(keep)}" if t.can else "CAN only")
        self._update()

    # -- background work ----------------------------------------------------------------------
    def _run(self, work: Callable[[], object], done: Callable[[object], None]) -> None:
        self._busy = True
        self._update()

        def task():
            try:
                result = work()
            except Exception as exc:  # shown in the page, never lost
                self._bridge.failed.emit(str(exc))
            else:
                self._bridge.done.emit(done, result)

        self._pool.submit(task)

    def _finish(self) -> None:
        self._busy = False
        self._update()

    def _failed(self, message: str) -> None:
        self._log(f"[tools] {message}")
        self.trace.appendPlainText(f"-- {message}")
        self._finish()

    def _on_trace(self, e: TraceEntry) -> None:
        self._bridge.trace.emit(e.format())

    # -- actions -------------------------------------------------------------------------------
    def toggle_connection(self) -> None:
        if self.session:
            self.disconnect()
            return
        t = self._target()
        if t is None:
            return
        session = EcuSession(self.project, t, self._on_trace)

        def done(message):
            self.session = session
            self._log(f"[tools] {message}")
            self.trace.appendPlainText(f"-- {message}")
            self._finish()

        self._run(session.open, done)

    def disconnect(self) -> None:
        if self.session is not None:
            session, self.session = self.session, None
            self._pool.submit(session.close).result(timeout=10)
            self.trace.appendPlainText("-- disconnected")
        self.keep_btn.setChecked(False)
        self._update()

    def release(self, target_ids: list[str]) -> None:
        """A flash is about to start: free the ECU (the job opens its own connection)."""
        if self.session and self.session.target.id in target_ids:
            self._log(f"[tools] disconnected from {self.session.target.id} for flashing")
            self.disconnect()

    def wake_up(self) -> None:
        if self.session:
            self._run(self.session.wake_up, lambda msg: (self.trace.appendPlainText(f"-- {msg}"), self._finish()))

    def toggle_keep_alive(self) -> None:
        if not self.session:
            self.keep_btn.setChecked(False)
            return
        on = self.keep_btn.isChecked()

        def done(frames):
            self.trace.appendPlainText(f"-- keep-alive {'on: ' + ', '.join(frames) if on else 'off'}")
            self._finish()

        def failed_guard():
            try:
                return self.session.keep_alive(on)
            except Exception:
                self._bridge.done.emit(lambda _r: self.keep_btn.setChecked(not on), None)
                raise

        self._run(failed_guard, done)

    def read_identification(self) -> None:
        if self.session:
            self.table.setRowCount(0)
            self._run(self.session.read_identification, self._show_rows)

    def read_one(self) -> None:
        if not self.session:
            return
        try:
            did = int(self.did_edit.text().strip().removeprefix("0x"), 16)
        except ValueError:
            self.did_edit.setStyleSheet("border: 1px solid #c93a3a")
            return
        self.did_edit.setStyleSheet("")
        self._run(lambda: [self.session.read_did(did)], lambda rows: self._show_rows(rows, append=True))

    def _show_rows(self, rows: list[IdentRow], append: bool = False) -> None:
        if not append:
            self.table.setRowCount(0)
        for r in rows:
            i = self.table.rowCount()
            self.table.insertRow(i)
            raw = r.raw.hex(" ").upper() if r.raw is not None else r.detail
            for col, value in enumerate((f"{r.did:04X}", r.name, r.text, raw)):
                item = QTableWidgetItem(value)
                if r.status != "ok":
                    item.setForeground(self.palette().placeholderText())
                self.table.setItem(i, col, item)
        self.table.resizeColumnToContents(0)
        self.table.resizeColumnToContents(1)
        self._finish()

    def copy_table(self) -> None:
        lines = ["\t".join(self.table.item(r, c).text() if self.table.item(r, c) else "" for c in range(4))
                 for r in range(self.table.rowCount())]
        QApplication.clipboard().setText("\n".join(["DID\tName\tValue\tRaw"] + lines))

    def _update(self) -> None:
        connected = self.session is not None
        busy = self._busy
        self.connect_btn.setText("Disconnect" if connected else "Connect")
        self.connect_btn.setEnabled(not busy and self._target() is not None)
        kind, text = ("running", "Working…") if busy else (("passed", "Connected") if connected else ("idle", "Disconnected"))
        if self.state.text() != text:
            self.state.setText(text)
            self.state.setProperty("pill", kind)
            repolish(self.state)
        for b in (self.wake_btn, self.keep_btn, self.read_btn, self.read_one_btn):
            b.setEnabled(connected and not busy)
        self.keep_btn.setText("Stop keep-alive" if self.keep_btn.isChecked() else "Start keep-alive")

    def shutdown(self) -> None:
        self.disconnect()
        self._pool.shutdown(wait=False)
