"""Visual editing of a project target: properties and firmware files."""

from __future__ import annotations

import datetime
import fnmatch
import os
from typing import Callable

from PySide6.QtCore import QMimeData, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QPushButton, QScrollArea,
    QSplitter, QTableWidget, QTableWidgetItem, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..app.project_io import list_templates, new_target
from ..firmware.loaders import FirmwareError, FirmwareSpec, detect_format, load_firmware, odx_sessions
from ..firmware.model import sha256_file
from ..firmware.validate import validate_image
from .forms import DictForm, F, Section

FIRMWARE_FILTER = "Firmware (*.hex *.ihex *.s19 *.s28 *.s37 *.srec *.mot *.bin *.pdx *.odx-f *.odx);;All files (*)"


def is_firmware_file(path: str) -> bool:
    try:
        detect_format(path)
        return True
    except FirmwareError:
        return False


def firmware_paths(mime: QMimeData) -> list[str]:
    """Local firmware files in a drag & drop payload (e.g. from Finder / Explorer)."""
    return [u.toLocalFile() for u in mime.urls() if u.isLocalFile() and is_firmware_file(u.toLocalFile())]


class DropTable(QTableWidget):
    """Table that accepts firmware files dropped from the file manager. Emits (paths, row under the mouse)."""

    files_dropped = Signal(list, int)

    def __init__(self, rows: int, cols: int, parent=None):
        super().__init__(rows, cols, parent)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)

    def dragEnterEvent(self, event) -> None:
        if firmware_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        self.dragEnterEvent(event)

    def dropEvent(self, event) -> None:
        paths = firmware_paths(event.mimeData())
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        self.files_dropped.emit(paths, self.rowAt(event.position().toPoint().y()))


def put_release_file(entries: list, path: str, project_dir: str, index: int | None,
                     choose_session: Callable[[str, list[str]], str | None]) -> str:
    """Load a release file: replace entries[index] keeping its options, or append when index is None.

    Returns a one-line description for the log.
    """
    rel = os.path.relpath(path, project_dir)
    stored = path if rel.startswith("..") else rel
    kind = detect_format(path)
    replacing = index is not None and 0 <= index < len(entries)
    if not replacing:
        entry = {"file": stored}
        entries.append(entry)
        action = "added"
    else:
        entry = entries[index]
        action = f"replaced {os.path.basename(str(entry.get('file', '')))} with"
        entry["file"] = stored
    if kind == "odx":
        sessions = [s.label for s in odx_sessions(path)]
        if entry.get("session") not in sessions:
            if len(sessions) == 1 or not replacing:  # new file: the session is chosen in the form
                entry.pop("session", None)
            else:  # new release no longer has the session used so far
                chosen = choose_session(os.path.basename(path), sessions)
                if chosen:
                    entry["session"] = chosen
                else:
                    entry.pop("session", None)
    else:
        entry.pop("session", None)
    if kind == "binary":
        entry.setdefault("base_address", 0)
    else:
        entry.pop("base_address", None)
    return f"{action} {os.path.basename(path)} ({describe_file(path)})"


def entry_path(entry: dict, project_dir: str) -> str:
    path = str(entry.get("file", ""))
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(project_dir, path))


def release_candidates(entry: dict, project_dir: str, history: list[str] | tuple = (), limit: int = 30) -> list[str]:
    """Files a new release of `entry` can be picked from, newest first, then recently used ones.

    Looks in the entry's `release_dir` (default: the folder of the current file) for files matching
    `release_pattern` (default: the same firmware type as the current file).
    """
    current = entry_path(entry, project_dir)
    folder = entry.get("release_dir")
    folder = (folder if os.path.isabs(folder) else os.path.join(project_dir, folder)) if folder else os.path.dirname(current)
    pattern = entry.get("release_pattern")
    try:
        kind = detect_format(current)
    except FirmwareError:
        kind = None
    found = []
    try:
        names = os.listdir(folder)
    except OSError:
        names = []
    for name in names:
        path = os.path.join(folder, name)
        if name.startswith(".") or not os.path.isfile(path):
            continue
        if pattern:
            if not fnmatch.fnmatch(name, pattern):
                continue
        else:
            try:
                if detect_format(path) != kind:
                    continue
            except FirmwareError:
                continue
        found.append(path)
    found.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    found = found[:limit]
    for path in history:
        if path not in found and os.path.isfile(path):
            found.append(path)
    return found


def newer_release(entry: dict, project_dir: str) -> str | None:
    """The newest candidate when it is newer than the file in use, else None."""
    current = entry_path(entry, project_dir)
    candidates = release_candidates(entry, project_dir)
    if not candidates or os.path.abspath(candidates[0]) == os.path.abspath(current):
        return None
    try:
        return candidates[0] if os.path.getmtime(candidates[0]) > os.path.getmtime(current) else None
    except OSError:
        return candidates[0]


def describe_file(path: str) -> str:
    st = os.stat(path)
    when = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
    return f"{st.st_size} bytes, {when}, SHA-256 {sha256_file(path)[:16]}…"


def _transport(d: dict) -> str:
    return d.get("transport", "can")


def target_sections(project_dir: Callable[[], str]) -> list[Section]:
    def templates(_d: dict) -> list[str]:
        try:
            return list_templates(project_dir())
        except OSError:
            return ["uds_standard"]

    return [
        Section("ECU", [
            F("id", "Id"),
            F("name", "Name"),
            F("transport", "Bus", "choice", ["can", "canfd", "lin", "doip"]),
            F("simulate", "Use the built-in simulated ECU", "bool",
              tip="No hardware needed: the ECU is simulated on a virtual bus / localhost"),
            F("sequence", "Sequence", "combo", templates,
              tip="Built-in template name or a .yaml sequence in the project folder"),
            F("allow_custom_actions", "Allow custom actions (Python)", "bool",
              tip="Sequences may run Python scripts ('Custom action' steps). Off by default."),
        ]),
        Section("CAN / CAN FD", [
            F("can.backend", "Interface", "choice", ["virtual", "vector", "socketcan", "pcan", "kvaser"]),
            F("can.channel", "Channel"),
            F("can.app_name", "Vector application name", tip="Application in Vector Hardware Config"),
            F("can.bitrate", "Bitrate", "int"),
            F("can.data_bitrate", "Data bitrate (FD)", "int"),
            F("can.tx_dl", "Frame length (8..64)", "int"),
            F("can.tx_id", "Request id (tester → ECU)", "hex"),
            F("can.rx_id", "Response id (ECU → tester)", "hex"),
            F("can.func_id", "Functional id", "hex"),
            F("can.extended_id", "29-bit ids", "bool"),
            F("can.padding", "Padding byte", "hex"),
            F("can.stmin_rx", "STmin we request", "hex", tip="0x00-0x7F ms, 0xF1-0xF9 = 100-900 µs"),
            F("can.block_size", "Block size we request", "int"),
            F("can.stmin_tx_override_ms", "Override ECU STmin (ms)", "float",
              tip="Deviation from ISO 15765-2, reported as a warning"),
            F("can.bus_log", "CAN log file (.blf/.asc)"),
            F("can.cyclic", "Cyclic messages", "cyclic",
              tip="Sent while flashing, e.g. network management: 0x500=00 01 @100 (id=data @period ms; …)"),
        ], lambda d: _transport(d) in ("can", "canfd")),
        Section("LIN", [
            F("lin.backend", "Interface", "choice", ["virtual", "vector"]),
            F("lin.channel", "Channel"),
            F("lin.baudrate", "Baudrate", "int"),
            F("lin.nad", "NAD", "hex"),
            F("lin.functional_nad", "Functional NAD", "hex"),
            F("lin.n_cs_ms", "Pause between frames (ms)", "float"),
            F("lin.poll_interval_ms", "Poll interval (ms)", "float"),
            F("lin.n_cr_ms", "N_Cr timeout (ms)", "float"),
        ], lambda d: _transport(d) == "lin"),
        Section("DoIP", [
            F("doip.ecu_ip", "ECU / gateway IP"),
            F("doip.tcp_port", "TCP port", "int"),
            F("doip.ecu_address", "ECU logical address", "hex"),
            F("doip.tester_address", "Tester logical address", "hex"),
            F("doip.functional_address", "Functional address", "hex"),
            F("doip.activation_type", "Routing activation type", "hex"),
            F("doip.use_tls", "TLS", "bool"),
        ], lambda d: _transport(d) == "doip"),
        Section("Wake-up and identification", [
            F("wakeup.frames", "Wake-up frames", "cyclic",
              tip="CAN frames sent to wake the ECU, e.g. 0x500=01 (default: the cyclic messages). LIN: wake-up signal"),
            F("wakeup.repeat", "Repeat", "int"),
            F("wakeup.interval_ms", "Interval (ms)", "int"),
            F("wakeup.wait_ms", "Wait after wake-up (ms)", "int"),
            F("wakeup.before_flash", "Wake up before every flash", "bool"),
            F("identification", "Identification DIDs", "hexlist",
              tip="Read by ECU tools → Read identification (default: VIN, part numbers, SW/HW versions…)"),
        ]),
        Section("UDS timing", [
            F("uds.p2_ms", "P2 (ms)", "int"),
            F("uds.p2_star_ms", "P2* (ms)", "int"),
            F("uds.tester_present_s", "Tester Present period (s)", "float"),
        ]),
        Section("Seed & Key (0x27)", [
            F("seed_key.provider", "Provider", "choice", ["", "python", "library", "bridge"]),
            F("seed_key.path", "Plugin / library", "file",
              file_filter="Seed&Key (*.py *.dll *.so *.dylib);;All files (*)"),
            F("seed_key.variant", "Variant"),
            F("seed_key.options", "Options (GenerateKeyExOpt)"),
            F("seed_key.bridge_python", "32-bit Python (bridge)", "file"),
        ]),
        Section("Certificates (0x29)", [
            F("certificates.provider", "Provider", "choice", ["", "file", "python"]),
            F("certificates.cert", "Tester certificate", "file", file_filter="Certificates (*.pem *.der *.crt);;All files (*)"),
            F("certificates.key", "Private key", "file", file_filter="Keys (*.key *.pem *.der);;All files (*)"),
            F("certificates.path", "Provider plugin (.py)", "file", file_filter="Python (*.py)"),
            F("certificates.role", "Role"),
            F("certificates.ecu", "ECU identity"),
        ]),
        Section("Simulated ECU", [
            F("simulator.secret", "Seed&Key secret", "hex"),
            F("simulator.erase_time_ms", "Erase time (ms)", "int"),
            F("simulator.auth_trust_anchor", "Root CA for 0x29", "file"),
            F("simulator.require_authentication", "Require 0x29", "bool"),
            F("simulator.sleep_timeout_ms", "Falls asleep after (ms)", "int",
              tip="Asleep until woken up; stays awake while the wake-up id is seen"),
            F("simulator.wakeup_id", "Wake-up / keep-alive id", "hex"),
        ], lambda d: bool(d.get("simulate"))),
    ]


def firmware_sections(project_dir: Callable[[], str]) -> list[Section]:
    def ext(d: dict) -> str:
        try:
            return detect_format(str(d.get("file", "")))
        except FirmwareError:
            return ""

    def sessions(d: dict) -> list[str]:
        path = str(d.get("file", ""))
        path = path if os.path.isabs(path) else os.path.join(project_dir(), path)
        try:
            return [""] + [s.label for s in odx_sessions(path)]
        except Exception:
            return [""]

    return [
        Section("File", [
            F("file", "File", "file", file_filter=FIRMWARE_FILTER),
            F("name", "Block name"),
        ]),
        Section("New releases", [
            F("release_dir", "Release folder", "dir",
              tip="Where new builds of this file appear (default: the folder of the current file)"),
            F("release_pattern", "File name filter", tip="e.g. app_*.hex (default: same file type)"),
        ]),
        Section("Binary file", [F("base_address", "Load address", "hex")], lambda d: ext(d) == "binary"),
        Section("PDX / ODX-F", [F("session", "Session", "choice", sessions)], lambda d: ext(d) == "odx"),
        Section("RequestDownload (0x34)", [
            F("dfi", "Data format (compression/encryption)", "hex", tip="0x00 = plain data"),
            F("alfid", "Address/length format", "hex", tip="0x44 = 4-byte address, 4-byte size"),
            F("memory_address", "Memory address override", "hex"),
            F("memory_size", "Memory size override", "hex", tip="e.g. the uncompressed size"),
        ]),
        Section("Segments", [
            F("max_gap", "Merge segments closer than (bytes)", "int"),
            F("fill_gaps", "Fill byte for merged gaps", "hex"),
            F("split", "Blocks", "choice", ["", "file", "segments"], tip="one block per file or per segment"),
        ], lambda d: ext(d) in ("ihex", "srec")),
    ]


class TargetEditor(QScrollArea):
    changed = Signal()

    def __init__(self, project_dir: Callable[[], str], parent=None):
        super().__init__(parent)
        self.form = DictForm(target_sections(project_dir), project_dir)
        self.form.changed.connect(self._on_change)
        self.setWidget(self.form)
        self.setWidgetResizable(True)

    def set_target(self, target: dict) -> None:
        self.form.set_data(target)

    def _on_change(self) -> None:
        t = self.form.data
        bus = t.get("transport", "can")
        section = "can" if bus in ("can", "canfd") else bus
        if section not in t:  # bus changed: start from the defaults of the new bus
            t[section] = new_target(str(t.get("id", "ecu")), bus)[section]
        for other in ("can", "lin", "doip"):
            if other != section:
                t.pop(other, None)
        self.form.set_data(t)
        self.changed.emit()


class FirmwareEditor(QWidget):
    """Firmware files of one target: list, per-file options and a preview of the resulting blocks."""

    changed = Signal()

    def __init__(self, project_dir: Callable[[], str], parent=None):
        super().__init__(parent)
        self.project_dir = project_dir
        self.entries: list = []

        self.table = DropTable(0, 3)
        self.table.setHorizontalHeaderLabels(["File", "Type", "Options"])
        self.table.setToolTip("Drop .hex / .s19 / .bin / .pdx files here (on a row to replace that file)")
        self.table.files_dropped.connect(self._dropped)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._on_select)

        buttons = QHBoxLayout()
        for text, slot in (("Add files…", self.add_files), ("Replace file…", self.replace_file),
                           ("Remove", self.remove), ("Up", lambda: self.move(-1)), ("Down", lambda: self.move(1))):
            b = QPushButton(text)
            b.clicked.connect(slot)
            buttons.addWidget(b)
        buttons.addStretch(1)

        self.form = DictForm(firmware_sections(project_dir), project_dir)
        self.form.changed.connect(self._on_form_change)
        form_scroll = QScrollArea()
        form_scroll.setWidget(self.form)
        form_scroll.setWidgetResizable(True)

        self.preview = QTreeWidget()
        self.preview.setHeaderLabels(["Block / segment", "Start", "End", "Bytes", "DFI", "ALFID", "CRC32",
                                      "Identification"])
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.file_info = QLabel("")
        self.file_info.setWordWrap(True)
        self.file_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.table)
        left_layout.addLayout(buttons)
        left_layout.addWidget(self.file_info)
        left_layout.addWidget(QLabel("Resulting blocks:"))
        left_layout.addWidget(self.preview)
        left_layout.addWidget(self.status)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(left)
        split.addWidget(form_scroll)
        split.setSizes([620, 380])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(split)

    # -- data --------------------------------------------------------------------
    def set_entries(self, entries: list) -> None:
        """`entries` is the target's `firmware` list, edited in place."""
        for i, e in enumerate(entries):  # normalise "app.hex" shorthand to {file: app.hex}
            if not isinstance(e, dict):
                entries[i] = {"file": str(e)}
        self.entries = entries
        self._fill()
        if entries:
            self.table.selectRow(0)
            self._on_select()  # row 0 may already be selected: no signal, but the entries changed
        else:
            self.table.clearSelection()
            self.form.set_data({})
            self.form.setEnabled(False)
        self.refresh_preview()

    def _fill(self) -> None:
        self.table.setRowCount(len(self.entries))
        for row, e in enumerate(self.entries):
            try:
                kind = detect_format(str(e.get("file", "")))
            except FirmwareError:
                kind = "?"
            opts = ", ".join(f"{k}={v:#x}" if isinstance(v, int) and not isinstance(v, bool) else f"{k}={v}"
                             for k, v in e.items() if k != "file")
            for col, text in enumerate((str(e.get("file", "")), kind, opts)):
                self.table.setItem(row, col, QTableWidgetItem(text))

    def _row(self) -> int:
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def _on_select(self) -> None:
        row = self._row()
        self.form.setEnabled(row >= 0)
        if row >= 0:
            self.form.set_data(self.entries[row])
        self._show_file_info(row)

    def _show_file_info(self, row: int) -> None:
        if row < 0 or row >= len(self.entries):
            self.file_info.setText("")
            return
        path = str(self.entries[row].get("file", ""))
        path = path if os.path.isabs(path) else os.path.join(self.project_dir(), path)
        try:
            self.file_info.setText(f"{os.path.basename(path)}: {describe_file(path)}")
        except OSError as exc:
            self.file_info.setText(f"{os.path.basename(path)}: {exc.strerror}")

    def _choose_session(self, name: str, sessions: list[str]) -> str | None:
        session, ok = QInputDialog.getItem(self, "ODX-F session", f"{name} has several sessions. Flash:", sessions, 0, False)
        return session if ok else None

    def _dropped(self, paths: list[str], row: int) -> None:
        if row >= 0 and len(paths) == 1:  # dropped on a file: new release of that file
            self.replace_file(paths[0], row)
        else:
            self.add_files(paths)

    def replace_file(self, path: str | None = None, row: int | None = None) -> None:
        """Swap the selected file for a new release, keeping its options (session, DFI, addresses)."""
        row = self._row() if row is None else row
        if row < 0:
            self.add_files([path] if path else None)
            return
        if path is None:
            current = str(self.entries[row].get("file", ""))
            start = os.path.dirname(current if os.path.isabs(current) else os.path.join(self.project_dir(), current))
            path, _ = QFileDialog.getOpenFileName(self, "New release of this file", start, FIRMWARE_FILTER)
            if not path:
                return
        put_release_file(self.entries, path, self.project_dir(), row, self._choose_session)
        self._fill()
        self.table.selectRow(row)
        self._on_select()
        self.refresh_preview()
        self.changed.emit()

    def _on_form_change(self) -> None:
        row = self._row()
        self._fill()
        if row >= 0:
            self.table.selectRow(row)
        self.refresh_preview()
        self.changed.emit()

    # -- actions -------------------------------------------------------------------
    def add_files(self, paths: list[str] | None = None) -> None:
        base = self.project_dir()
        if paths is None:
            paths, _ = QFileDialog.getOpenFileNames(self, "Add firmware files", base, FIRMWARE_FILTER)
        for path in paths:
            put_release_file(self.entries, path, base, None, self._choose_session)
        if paths:
            self._fill()
            self.table.selectRow(len(self.entries) - 1)
            self._on_select()
            self.refresh_preview()
            self.changed.emit()

    def remove(self) -> None:
        row = self._row()
        if row >= 0:
            del self.entries[row]
            self._fill()
            self.refresh_preview()
            self.changed.emit()

    def move(self, delta: int) -> None:
        row = self._row()
        new = row + delta
        if row < 0 or not 0 <= new < len(self.entries):
            return
        self.entries[row], self.entries[new] = self.entries[new], self.entries[row]
        self._fill()
        self.table.selectRow(new)
        self.refresh_preview()
        self.changed.emit()

    def refresh_preview(self) -> None:
        self.preview.clear()
        if not self.entries:
            self.status.setText("Add .hex, .s19, .bin, .pdx or .odx-f files.")
            return
        try:
            image = load_firmware([FirmwareSpec.from_dict(dict(e)) for e in self.entries], self.project_dir())
        except Exception as exc:
            self.status.setStyleSheet("color: #b42318")
            self.status.setText(str(exc))
            return
        for b in image.blocks:
            item = QTreeWidgetItem(self.preview, [
                b.name, f"0x{b.start:08X}", f"0x{b.end - 1:08X}", f"{b.size}",
                f"0x{b.data_format_identifier:02X}", f"0x{b.address_and_length_format:02X}", f"{b.crc32:08X}",
                ", ".join(f"{k}={v}" for k, v in (b.metadata.get("own_idents") or {}).items()),
            ])
            for s in b.segments:
                QTreeWidgetItem(item, ["segment", f"0x{s.address:08X}", f"0x{s.end - 1:08X}", f"{len(s.data)}"])
        issues = validate_image(image)
        if issues:
            self.status.setStyleSheet("color: #b42318" if any(i.severity == "error" for i in issues) else "color: #b7791f")
            self.status.setText("\n".join(str(i) for i in issues))
        else:
            self.status.setStyleSheet("color: #2f7d5b")
            self.status.setText(f"{len(image.blocks)} block(s), {image.total_size} bytes")
