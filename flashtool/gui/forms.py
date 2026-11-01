"""Schema-driven form that edits a dict in place (dotted paths, empty field = key removed)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLineEdit, QPushButton, QVBoxLayout,
    QWidget,
)

from ..app.project_io import HexInt


@dataclass
class F:
    path: str
    label: str
    kind: str = "text"  # text | int | float | hex | bool | choice | combo | file | expr | bytes | hexlist | params | onoff | boolchoice
    choices: list[str] | Callable[[dict], list[str]] = field(default_factory=list)
    tip: str = ""
    file_filter: str = "All files (*)"


@dataclass
class Section:
    title: str
    fields: list[F]
    visible: Callable[[dict], bool] | None = None


def get_path(data: dict, path: str) -> Any:
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def set_path(data: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    cur = data
    if value is None:
        chain = []
        for part in parts[:-1]:
            if not isinstance(cur.get(part), dict):
                return
            chain.append((cur, part))
            cur = cur[part]
        cur.pop(parts[-1], None)
        for parent, key in reversed(chain):  # drop dicts that became empty
            if parent[key] == {}:
                del parent[key]
        return
    for part in parts[:-1]:
        if not isinstance(cur.get(part), dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value


def _number(text: str, kind: str) -> Any:
    if "${" in text:
        return text  # expression, evaluated when the sequence runs
    if kind == "float":
        return float(text)
    value = int(text, 0)
    return HexInt(value) if kind == "hex" else value


def to_text(value: Any, kind: str) -> str:
    if value is None:
        return ""
    if kind == "hex" and isinstance(value, int) and not isinstance(value, bool):
        return f"0x{value:02X}"
    if kind == "hexlist" and isinstance(value, list):
        return ", ".join(f"0x{v:02X}" if isinstance(v, int) else str(v) for v in value)
    if kind == "cyclic" and isinstance(value, list):
        return "; ".join(f"0x{int(m['id']):X}={m.get('data', '')} @{m.get('period_ms', 100)}" for m in value)
    if kind == "params" and isinstance(value, dict):
        return "; ".join(f"{k}={to_text(v, 'hex' if isinstance(v, int) else 'text')}" for k, v in value.items())
    return str(value)


def parse(text: str, kind: str) -> Any:
    """Field text -> stored value; None removes the key. Raises ValueError for invalid input."""
    text = text.strip()
    if text == "":
        return None
    if kind in ("int", "float", "hex"):
        return _number(text, kind)
    if kind == "hexlist":
        return [_number(t.strip(), "hex") for t in text.split(",") if t.strip()]
    if kind == "cyclic":  # "0x500=00 01 @100; 0x501=FF @1000"
        msgs = []
        for part in text.split(";"):
            if not part.strip():
                continue
            ident, _, rest = part.partition("=")
            data, _, period = rest.partition("@")
            bytes.fromhex(data.replace(" ", ""))  # validates
            msgs.append({"id": HexInt(int(ident.strip(), 0)), "data": data.strip(),
                         "period_ms": int(period.strip() or 100)})
        return msgs
    if kind == "params":
        out = {}
        for part in text.split(";"):
            if not part.strip():
                continue
            if "=" not in part:
                raise ValueError(f"'{part.strip()}' must be name=value")
            k, v = part.split("=", 1)
            v = v.strip()
            try:
                out[k.strip()] = _number(v, "hex") if v.lower().startswith("0x") else int(v)
            except ValueError:
                out[k.strip()] = v
        return out
    return text


class DictForm(QWidget):
    """Edits `data` in place. `changed` fires after every accepted edit."""

    changed = Signal()

    def __init__(self, sections: list[Section], base_dir: Callable[[], str] | None = None, parent=None):
        super().__init__(parent)
        self.sections = sections
        self.base_dir = base_dir or os.getcwd
        self.data: dict = {}
        self._widgets: dict[str, tuple[F, QWidget]] = {}
        self._boxes: list[tuple[Section, QGroupBox]] = []
        self._loading = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        for section in sections:
            box = QGroupBox(section.title)
            form = QFormLayout(box)
            for f in section.fields:
                widget = self._make(f)
                if f.tip:
                    widget.setToolTip(f.tip)
                form.addRow(f.label, widget)
            layout.addWidget(box)
            self._boxes.append((section, box))
        layout.addStretch(1)

    # -- widgets -------------------------------------------------------------
    def _make(self, f: F) -> QWidget:
        if f.kind == "bool":
            w = QCheckBox()
            w.toggled.connect(lambda _=None, f=f: self._edited(f))
            self._widgets[f.path] = (f, w)
            return w
        if f.kind in ("choice", "combo", "onoff", "boolchoice"):
            w = QComboBox()
            w.setEditable(f.kind == "combo")
            if f.kind == "combo":
                w.lineEdit().editingFinished.connect(lambda f=f: self._edited(f))
                w.activated.connect(lambda _=None, f=f: self._edited(f))
            else:
                w.currentIndexChanged.connect(lambda _=None, f=f: self._edited(f))
            self._widgets[f.path] = (f, w)
            return w
        edit = QLineEdit()
        edit.setPlaceholderText({"hex": "0x..", "expr": "expression", "bytes": "hex bytes, ${expr} allowed",
                                 "cyclic": "0x500=00 01 @100; 0x501=FF @1000",
                                 "hexlist": "0x22, 0x31", "params": "name=value; name=value"}.get(f.kind, ""))
        edit.editingFinished.connect(lambda f=f: self._edited(f))
        self._widgets[f.path] = (f, edit)
        if f.kind not in ("file", "dir"):
            return edit
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(edit)
        browse = QPushButton("…")
        browse.setFixedWidth(32)
        browse.clicked.connect(lambda _=None, f=f, edit=edit: self._browse(f, edit))
        row.addWidget(browse)
        return holder

    def _browse(self, f: F, edit: QLineEdit) -> None:
        base = self.base_dir()
        if f.kind == "dir":
            path = QFileDialog.getExistingDirectory(self, f.label, base)
        else:
            path, _ = QFileDialog.getOpenFileName(self, f.label, base, f.file_filter)
        if not path:
            return
        rel = os.path.relpath(path, base)
        edit.setText(path if rel.startswith("..") else rel)
        self._edited(f)

    def _choices(self, f: F) -> list[str]:
        return list(f.choices(self.data) if callable(f.choices) else f.choices)

    # -- data <-> widgets ------------------------------------------------------
    def set_data(self, data: dict) -> None:
        self.data = data
        self._loading = True
        try:
            for path, (f, w) in self._widgets.items():
                value = get_path(data, path)
                if isinstance(w, QCheckBox):
                    w.setChecked(bool(value))
                elif isinstance(w, QComboBox):
                    w.clear()
                    choices = self._choices(f)
                    if f.kind == "onoff":
                        choices = ["on", "off"]
                        text = "on" if value in (True, "on") else "off"
                    elif f.kind == "boolchoice":
                        choices = ["", "true", "false"]
                        text = "" if value is None else ("true" if value else "false")
                    else:
                        text = to_text(value, f.kind)
                    w.addItems(choices)
                    if text not in choices:
                        w.addItem(text)
                    w.setCurrentText(text)
                else:
                    w.setText(to_text(value, f.kind))
                    w.setStyleSheet("")
            self._update_visibility()
        finally:
            self._loading = False

    def _edited(self, f: F) -> None:
        if self._loading:
            return
        _, w = self._widgets[f.path]
        if isinstance(w, QCheckBox):
            value: Any = True if w.isChecked() else None
        elif isinstance(w, QComboBox):
            text = w.currentText()
            if f.kind == "onoff":
                value = text == "on"
            elif f.kind == "boolchoice":
                value = {"true": True, "false": False}.get(text)
            else:
                value = text or None
        else:
            try:
                value = parse(w.text(), f.kind)
            except ValueError as exc:
                w.setStyleSheet("border: 1px solid #b42318")
                w.setToolTip(f"Invalid value: {exc}")
                return
            w.setStyleSheet("")
            w.setToolTip(f.tip)
        if get_path(self.data, f.path) == value:
            return
        set_path(self.data, f.path, value)
        self._update_visibility()
        self.changed.emit()

    def _update_visibility(self) -> None:
        for section, box in self._boxes:
            box.setVisible(section.visible is None or bool(section.visible(self.data)))

    def widget(self, path: str) -> QWidget:
        return self._widgets[path][1]
