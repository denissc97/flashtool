"""Building blocks of the main window: ECU cards and the step duration chart."""

from __future__ import annotations

import datetime
import os
from typing import Callable

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from .project_editor import firmware_paths
from .theme import icon, repolish, tokens

BUS_LABEL = {"can": "CAN", "canfd": "CAN FD", "lin": "LIN", "doip": "DoIP"}
STATUS_TEXT = {"idle": "Ready", "starting": "Starting", "running": "Flashing", "passed": "Passed", "failed": "Failed"}


class ElidedLabel(QLabel):
    """Label that ends long text with … instead of cutting it (full text in the tooltip)."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(60)

    def setText(self, text: str) -> None:
        self._full = text
        self.setToolTip(text)
        self._elide()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        super().setText(self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, self.width()))


class ReleaseCombo(QComboBox):
    """Picks the file of one firmware entry. Re-reads the release folder each time it opens."""

    chosen = Signal(str)  # path, or "" for "Browse…"

    def __init__(self, provider: Callable[[], list[str]], parent=None):
        super().__init__(parent)
        self.provider = provider
        self.current = ""
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(18)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.activated.connect(self._activated)

    def refill(self, current: str) -> None:
        self.current = os.path.abspath(current)
        paths = [os.path.abspath(p) for p in self.provider()]
        if self.current not in paths:
            paths.insert(0, self.current)
        newest = paths[0] if paths else None
        self.blockSignals(True)
        self.clear()
        for path in paths:
            try:
                when = datetime.datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M")
            except OSError:
                when = "missing"
            label = f"{os.path.basename(path)}    {when}" + ("    · newest" if path == newest and len(paths) > 1 else "")
            self.addItem(label, path)
            self.setItemData(self.count() - 1, path, Qt.ItemDataRole.ToolTipRole)
        self.addItem("Browse…", "")
        self.setCurrentIndex(paths.index(self.current))
        self.setToolTip(self.current)
        self.blockSignals(False)

    def showPopup(self) -> None:
        self.refill(self.current)  # new builds show up without reopening the project
        super().showPopup()

    def _activated(self, index: int) -> None:
        path = self.itemData(index)
        if path != self.current:
            self.chosen.emit(path or "")
        if not path:
            self.setCurrentIndex(max(0, self.findData(self.current)))


def pill(text: str, kind: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("pill", kind)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return label


class EcuCard(QFrame):
    """One ECU on the Flash page. Accepts release files dropped from the file manager."""

    flash_clicked = Signal(str)
    release_clicked = Signal(str)
    release_chosen = Signal(str, int, str)  # target id, firmware entry index, path ("" = browse)
    configure_clicked = Signal(str)
    report_clicked = Signal(str)
    files_dropped = Signal(str, list)
    selected = Signal(str)

    def __init__(self, target_id: str, parent=None):
        super().__init__(parent)
        self.target_id = target_id
        self.setObjectName("card")
        self.setAcceptDrops(True)
        self.setMinimumWidth(460)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self.title = ElidedLabel()
        self.title.setProperty("role", "h2")
        self.ident = QLabel()
        self.ident.setProperty("role", "muted")
        self.bus = pill("", "badge")
        self.sim = pill("Simulated", "badge")
        self.status = pill("Ready", "idle")
        top = QHBoxLayout()
        top.addWidget(self.title, 1)
        top.addWidget(self.ident)
        top.addWidget(self.bus)
        top.addWidget(self.sim)
        top.addWidget(self.status)

        self.files = QLabel()  # shown when the ECU has no firmware yet
        self.files.setProperty("role", "muted")
        self.files.setWordWrap(True)
        self.release_btn = QPushButton("Add file…")
        self.release_btn.setProperty("kind", "ghost")
        self.release_btn.clicked.connect(lambda: self.release_clicked.emit(self.target_id))
        self.fw_icon = QLabel()
        empty_row = QHBoxLayout()
        empty_row.addWidget(self.fw_icon)
        empty_row.addWidget(self.files, 1)
        empty_row.addWidget(self.release_btn)
        self.combos: list[ReleaseCombo] = []
        self.latest_buttons: list[QPushButton] = []
        self.fw_rows = QVBoxLayout()
        self.fw_rows.setSpacing(6)
        fw = QVBoxLayout()
        fw.setSpacing(6)
        fw.addLayout(self.fw_rows)
        fw.addLayout(empty_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.detail = QLabel("")
        self.detail.setProperty("role", "muted")
        self.detail.setMinimumWidth(10)

        self.report_btn = QPushButton("Report")
        self.report_btn.clicked.connect(lambda: self.report_clicked.emit(self.target_id))
        self.config_btn = QPushButton("Configure")
        self.config_btn.clicked.connect(lambda: self.configure_clicked.emit(self.target_id))
        self.flash_btn = QPushButton("Flash")
        self.flash_btn.setProperty("kind", "primary")
        self.flash_btn.clicked.connect(lambda: self.flash_clicked.emit(self.target_id))
        bottom = QHBoxLayout()
        bottom.addWidget(self.detail, 1)
        bottom.addWidget(self.report_btn)
        bottom.addWidget(self.config_btn)
        bottom.addWidget(self.flash_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        layout.addLayout(top)
        layout.addLayout(fw)
        layout.addWidget(self.progress)
        layout.addLayout(bottom)
        self.refresh_icons()

    def refresh_icons(self) -> None:
        self.fw_icon.setPixmap(icon("doc", tokens()["muted"], 16).pixmap(16, 16))
        self.flash_btn.setIcon(icon("play", "#ffffff", 14))
        self.release_btn.setIcon(icon("upload", tokens()["accent"], 14))

    def set_target(self, t) -> None:
        self.title.setText(t.name)
        self.ident.setText(t.id if t.name != t.id else "")
        self.bus.setText(BUS_LABEL.get(t.transport, t.transport))
        self.sim.setVisible(t.simulate)
        self.files.setText("No firmware yet – drop a .hex / .s19 / .bin / .pdx / .vbf file here")
        self.files.setVisible(not t.firmware)
        self.fw_icon.setVisible(not t.firmware)

    def set_firmware(self, rows: list[dict], editable: bool) -> None:
        """One release picker per firmware entry: {index, path, provider, newer, session, blocks}.

        `blocks`: (type, logical block index) of each block the entry gives.
        """
        while self.fw_rows.count():
            item = self.fw_rows.takeAt(0)
            if item.layout() is not None:
                while item.layout().count():
                    w = item.layout().takeAt(0).widget()
                    if w is not None:
                        w.deleteLater()
        self.combos, self.latest_buttons = [], []
        self.release_btn.setVisible(not rows and editable)
        tags: list[QLabel] = []
        for row in rows:
            line = QHBoxLayout()
            ico = QLabel()
            ico.setPixmap(icon("doc", tokens()["muted"], 16).pixmap(16, 16))
            combo = ReleaseCombo(row["provider"])
            combo.refill(row["path"])
            combo.setProperty("editable", editable)
            combo.setEnabled(editable)
            combo.chosen.connect(lambda path, i=row["index"]: self.release_chosen.emit(self.target_id, i, path))
            line.addWidget(ico)
            for kind, index in dict.fromkeys(row.get("blocks") or []):  # which block sequence runs, which block
                tag = pill(kind if index is None else f"{kind} #{index}", "badge")
                tags.append(tag)
                tag.setToolTip(f"Block type {kind}: programmed with the {kind} block sequence of the template "
                               "(or the default one)" + ("" if index is None else f"\nLogical block index {index}"))
                line.addWidget(tag)
            line.addWidget(combo, 1)
            if row.get("session"):
                line.addWidget(pill(row["session"], "badge"))
            latest = QPushButton("Use latest")
            latest.setProperty("kind", "primary")
            latest.setIcon(icon("upload", "#ffffff", 14))
            newer = row.get("newer")
            latest.setVisible(bool(newer) and editable)
            if newer:
                latest.setToolTip(f"Newer build: {os.path.basename(newer)}")
                latest.clicked.connect(lambda _=False, i=row["index"], p=newer: self.release_chosen.emit(self.target_id, i, p))
            line.addWidget(latest)
            self.fw_rows.addLayout(line)
            self.combos.append(combo)
            self.latest_buttons.append(latest)
        for tag in tags:  # same width for BOOT / APP / CAL: the release pickers line up
            tag.ensurePolished()
        width = max((tag.sizeHint().width() for tag in tags), default=0)
        for tag in tags:
            tag.setFixedWidth(width)

    def set_state(self, st, running: bool) -> None:
        kind = {"starting": "running"}.get(st.status, st.status)
        if self.status.property("pill") != kind or self.status.text() != STATUS_TEXT.get(st.status, st.status):
            self.status.setText(STATUS_TEXT.get(st.status, st.status))
            self.status.setProperty("pill", kind if kind in ("idle", "running", "passed", "failed") else "idle")
            repolish(self.status)
            self.progress.setProperty("state", kind)
            repolish(self.progress)
        self.progress.setValue(int(1000 * st.done / st.total) if st.total else (1000 if st.status == "passed" else 0))
        parts = []
        if st.total:
            parts.append(f"{st.done // 1024} / {st.total // 1024} KB")
        if st.speed and st.status == "running":
            parts.append(f"{st.speed / 1024:.1f} KB/s")
            if st.total > st.done:
                parts.append(f"~{(st.total - st.done) / st.speed:.0f} s left")
        if st.status in ("failed",) and st.error:
            parts.append(st.error.splitlines()[0][:140])
        elif st.current:
            parts.append(st.current)
        self.detail.setText("   ·   ".join(parts) if parts else "Ready")
        self.flash_btn.setEnabled(not running)
        self.release_btn.setEnabled(not running)
        for combo in self.combos:
            combo.setEnabled(not running and combo.property("editable") is not False)
        self.report_btn.setEnabled(bool(st.report))

    def set_selected(self, selected: bool) -> None:
        if self.property("selected") != selected:
            self.setProperty("selected", selected)
            repolish(self)

    # -- interaction -------------------------------------------------------------------
    def mousePressEvent(self, event) -> None:
        self.selected.emit(self.target_id)
        super().mousePressEvent(event)

    def dragEnterEvent(self, event) -> None:
        if firmware_paths(event.mimeData()):
            event.acceptProposedAction()
            self.setProperty("drop", True)
            repolish(self)
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self.setProperty("drop", False)
        repolish(self)

    def dropEvent(self, event) -> None:
        self.setProperty("drop", False)
        repolish(self)
        paths = firmware_paths(event.mimeData())
        if paths:
            event.acceptProposedAction()
            self.files_dropped.emit(self.target_id, paths)


class DurationChart(QWidget):
    """Horizontal bars: where the time of a flash run went (top-level steps)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: list[tuple[str, float, str]] = []  # (label, seconds, status)
        self.setMinimumHeight(120)

    def set_rows(self, rows: list[tuple[str, float, str]]) -> None:
        self.rows = rows
        self.setMinimumHeight(max(120, 26 * len(rows) + 16))
        self.update()

    def paintEvent(self, event) -> None:
        t = tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.rows:
            p.setPen(QColor(t["muted"]))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Select a run to see where the time went")
            return
        label_w, value_w = 210, 80
        width = max(10, self.width() - label_w - value_w - 24)
        longest = max(s for _, s, _ in self.rows) or 1
        font = QFont(self.font())
        if font.pointSizeF() > 0:
            font.setPointSizeF(font.pointSizeF() * 0.95)
        p.setFont(font)
        for i, (label, seconds, status) in enumerate(self.rows):
            y = 8 + i * 26
            p.setPen(QColor(t["text"]))
            p.drawText(QRectF(8, y, label_w - 12, 20), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                       label)
            color = {"failed": t["fail"], "warning": t["warn"], "shared": t["muted"]}.get(status, t["accent"])
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(t["surface2"]))
            p.drawRoundedRect(QRectF(label_w, y + 3, width, 14), 4, 4)
            p.setBrush(QColor(color))
            p.drawRoundedRect(QRectF(label_w, y + 3, max(2.0, width * seconds / longest), 14), 4, 4)
            p.setPen(QColor(t["muted"]))
            p.drawText(QRectF(label_w + width + 8, y, value_w, 20), Qt.AlignmentFlag.AlignVCenter,
                       f"{seconds:.2f} s")
