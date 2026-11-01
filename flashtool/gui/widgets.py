"""Building blocks of the main window: ECU cards and the step duration chart."""

from __future__ import annotations

import os

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
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


def pill(text: str, kind: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("pill", kind)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return label


class EcuCard(QFrame):
    """One ECU on the Flash page. Accepts release files dropped from the file manager."""

    flash_clicked = Signal(str)
    release_clicked = Signal(str)
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

        self.files = QLabel()
        self.files.setProperty("role", "mono")
        self.files.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.files.setWordWrap(True)
        self.release_btn = QPushButton("Change release…")
        self.release_btn.setProperty("kind", "ghost")
        self.release_btn.clicked.connect(lambda: self.release_clicked.emit(self.target_id))
        fw = QHBoxLayout()
        self.fw_icon = QLabel()
        fw.addWidget(self.fw_icon)
        fw.addWidget(self.files, 1)
        fw.addWidget(self.release_btn)

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
        if t.firmware:
            names = [os.path.basename(f.file) + (f"  [{f.session}]" if f.session else "") for f in t.firmware]
            self.files.setText("   ·   ".join(names))
            self.files.setToolTip("\n".join(f.file for f in t.firmware))
        else:
            self.files.setText("No firmware yet – drop a .hex / .s19 / .bin / .pdx / .vbf file here")

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
        self.detail.setText("   ·   ".join(parts) if parts else "Drop a new release file here to replace the firmware")
        self.flash_btn.setEnabled(not running)
        self.release_btn.setEnabled(not running)
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
