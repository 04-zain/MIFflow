"""Mouse-accessible editing of arbitrary metadata, without scientific inference.

The widget consumes the current presentation model and emits researcher actions through its
controller. Expensive native or scientific operations use managed jobs, while the Qt thread
retains the selected form and its response identity."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import math
from dataclasses import dataclass

# ==============================================================================
#                                 Object editor
# ==============================================================================


@dataclass(frozen=True)
class ObjectEditorDraft:
    """Unvalidated widget contents retained only in the desktop session."""

    rows: tuple


from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..interactions import _json_editable_value
from .error_copy import diagnostic_details

_KINDS = ("Text", "Integer", "Number", "Boolean", "Empty", "Object", "List")


class ObjectEditor(QWidget):
    """A property tree; returned values still go through the owning validator."""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tree = QTreeWidget(self)
        self.tree.setHeaderLabels(("Field", "Type", "Value"))
        self.tree.setAccessibleName("Metadata fields")
        self.tree.setColumnWidth(0, 180)
        self.tree.setColumnWidth(1, 105)
        self.tree.itemChanged.connect(lambda *_: self.changed.emit())
        layout.addWidget(self.tree)
        actions = QHBoxLayout()
        for name, label, callback in (
            ("add", "Add field", self._add),
            ("remove", "Remove", self._remove),
            ("move-up", "Move up", lambda: self._move(-1)),
            ("move-down", "Move down", lambda: self._move(1)),
        ):
            button = QPushButton(label, self)
            button.setObjectName(f"object-{name}")
            button.clicked.connect(callback)
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        self._error = QLabel(self)
        self._error.setObjectName("object-error")
        self._error.setWordWrap(True)
        self._error.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self._error)
        self._error_details_toggle = QPushButton("Details", self)
        self._error_details_toggle.setObjectName("object-error-details-toggle")
        self._error_details_toggle.setCheckable(True)
        self._error_details_toggle.hide()
        layout.addWidget(self._error_details_toggle)
        self._error_details = QLabel(self)
        self._error_details.setObjectName("object-error-details")
        self._error_details.setWordWrap(True)
        self._error_details.setTextFormat(Qt.TextFormat.PlainText)
        self._error_details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._error_details.hide()
        layout.addWidget(self._error_details)
        self._error_details_toggle.toggled.connect(self._error_details.setVisible)

    def capture_draft(self) -> ObjectEditorDraft:
        def read(parent):
            result = []
            for index in range(parent.childCount()):
                item = parent.child(index)
                kind = item.data(1, Qt.ItemDataRole.UserRole)
                boolean = self.tree.itemWidget(item, 2).isChecked() if kind == "Boolean" else None
                result.append(
                    (item.text(0), kind, item.text(2), boolean, item.isExpanded(), read(item))
                )
            return tuple(result)

        return ObjectEditorDraft(read(self.tree.invisibleRootItem()))

    def restore_draft(self, draft: ObjectEditorDraft) -> None:
        if type(draft) is not ObjectEditorDraft:
            raise TypeError("Expected an object-editor draft")
        self.tree.clear()
        defaults = {
            "Empty": None,
            "Boolean": False,
            "Integer": 0,
            "Number": 0.0,
            "Text": "",
            "Object": {},
            "List": [],
        }

        def restore(parent, rows):
            for name, kind, text, boolean, expanded, children in rows:
                item = self._append(parent, name, defaults[kind])
                item.setText(2, text)
                if kind == "Boolean":
                    self.tree.itemWidget(item, 2).setChecked(boolean)
                restore(item, children)
                item.setExpanded(expanded)

        restore(self.tree.invisibleRootItem(), draft.rows)

    def set_value(self, value: object) -> None:
        value = _json_editable_value(value)
        if not isinstance(value, dict):
            raise ValueError("Metadata must be an object")
        self.tree.clear()
        for key, item in value.items():
            self._append(self.tree.invisibleRootItem(), key, item)
        self.tree.expandToDepth(0)
        self._error.clear()
        self._error_details_toggle.setChecked(False)
        self._error_details_toggle.hide()
        self._error_details.clear()

    def _append(self, parent: QTreeWidgetItem, name: str, value: object) -> QTreeWidgetItem:
        kind = (
            "Empty"
            if value is None
            else (
                "Boolean"
                if isinstance(value, bool)
                else (
                    "Integer"
                    if isinstance(value, int)
                    else (
                        "Number"
                        if isinstance(value, float)
                        else (
                            "Text"
                            if isinstance(value, str)
                            else (
                                "Object"
                                if isinstance(value, dict)
                                else "List" if isinstance(value, list) else None
                            )
                        )
                    )
                )
            )
        )
        if kind is None:
            raise ValueError("Unsupported metadata value")
        item = QTreeWidgetItem(parent, (str(name), "", ""))
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        types = QComboBox(self.tree)
        types.addItems(_KINDS)
        types.setCurrentText(kind)
        types.setAccessibleName(f"Type for {name}")
        self.tree.setItemWidget(item, 1, types)
        item.setData(1, Qt.ItemDataRole.UserRole, kind)
        types.currentTextChanged.connect(lambda new: self._change_kind(item, new))
        if kind in {"Object", "List"}:
            entries = value.items() if kind == "Object" else enumerate(value)
            for key, child in entries:
                self._append(item, str(key), child)
        elif kind == "Boolean":
            self._boolean(item, value)
        elif value is not None:
            item.setText(2, str(value))
        return item

    def _boolean(self, item: QTreeWidgetItem, value: bool) -> None:
        toggle = QCheckBox("Yes", self.tree)
        toggle.setChecked(value)
        toggle.setAccessibleName(f"Value for {item.text(0)}")
        toggle.toggled.connect(self.changed.emit)
        self.tree.setItemWidget(item, 2, toggle)

    def _change_kind(self, item: QTreeWidgetItem, kind: str) -> None:
        previous = item.data(1, Qt.ItemDataRole.UserRole)
        if (
            item.childCount()
            and QMessageBox.question(
                self,
                "Replace fields?",
                "Changing the type will remove the fields inside this entry.",
            )
            != QMessageBox.StandardButton.Yes
        ):
            combo = self.tree.itemWidget(item, 1)
            combo.blockSignals(True)
            combo.setCurrentText(previous)
            combo.blockSignals(False)
            return
        item.takeChildren()
        self.tree.removeItemWidget(item, 2)
        item.setData(1, Qt.ItemDataRole.UserRole, kind)
        item.setText(2, "0" if kind in {"Integer", "Number"} else "")
        if kind == "Boolean":
            self._boolean(item, False)
        self.changed.emit()

    def _read(self, item: QTreeWidgetItem) -> object:
        kind = item.data(1, Qt.ItemDataRole.UserRole)
        raw = item.text(2)
        if kind == "Object":
            return self._read_object(item)
        if kind == "List":
            return [self._read(item.child(i)) for i in range(item.childCount())]
        if kind == "Empty":
            return None
        if kind == "Boolean":
            return self.tree.itemWidget(item, 2).isChecked()
        if kind == "Integer":
            try:
                return int(raw)
            except ValueError as exc:
                raise ValueError(f"Enter a whole number for {item.text(0)}") from exc
        if kind == "Number":
            try:
                number = float(raw)
            except ValueError as exc:
                raise ValueError(f"Enter a number for {item.text(0)}") from exc
            if not math.isfinite(number):
                raise ValueError(f"Enter a finite number for {item.text(0)}")
            return number
        return raw

    def _read_object(self, item: QTreeWidgetItem) -> dict[str, object]:
        result = {}
        for index in range(item.childCount()):
            child = item.child(index)
            key = child.text(0)
            if key in result:
                raise ValueError(f"Duplicate field name: {key}")
            result[key] = self._read(child)
        return result

    def value(self) -> dict[str, object]:
        return self._read_object(self.tree.invisibleRootItem())

    def _add(self) -> None:
        parent = self.tree.currentItem() or self.tree.invisibleRootItem()
        if parent.data(1, Qt.ItemDataRole.UserRole) not in {"Object", "List", None}:
            parent = parent.parent() or self.tree.invisibleRootItem()
        names = {parent.child(i).text(0) for i in range(parent.childCount())}
        name = (
            str(parent.childCount())
            if parent.data(1, Qt.ItemDataRole.UserRole) == "List"
            else "field"
        )
        while name in names:
            name += "_"
        child = self._append(parent, name, "")
        parent.setExpanded(True)
        self.tree.setCurrentItem(child)
        self.changed.emit()

    def _remove(self) -> None:
        item = self.tree.currentItem()
        if item is None:
            return
        parent = item.parent() or self.tree.invisibleRootItem()
        parent.takeChild(parent.indexOfChild(item))
        self._renumber(parent)
        self.changed.emit()

    def _renumber(self, parent: QTreeWidgetItem) -> None:
        if parent.data(1, Qt.ItemDataRole.UserRole) == "List":
            for index in range(parent.childCount()):
                parent.child(index).setText(0, str(index))

    def _move(self, offset: int) -> None:
        item = self.tree.currentItem()
        if item is None:
            return
        parent = item.parent() or self.tree.invisibleRootItem()
        index = parent.indexOfChild(item)
        target = index + offset
        if not 0 <= target < parent.childCount():
            return
        try:
            entries = [
                (parent.child(i).text(0), self._read(parent.child(i)))
                for i in range(parent.childCount())
            ]
        except ValueError as exc:
            detail = str(exc)
            if detail.startswith("Enter a "):
                summary = f"{detail}. Correct this value, then try again."
            elif detail.startswith("Duplicate field name: "):
                name = detail.removeprefix("Duplicate field name: ")
                summary = f"Field name {name} is used twice. Rename one field, then try again."
            else:
                summary = "Could not move this field. Check its values, then try again. Open Details for the error."
            self._error.setText(summary)
            self._error_details.setText(diagnostic_details(detail, summary))
            self._error_details_toggle.setChecked(False)
            self._error_details_toggle.show()
            return
        entries.insert(target, entries.pop(index))
        parent.takeChildren()
        for key, child_value in entries:
            self._append(parent, key, child_value)
        self._renumber(parent)
        self.tree.setCurrentItem(parent.child(target))
        self._error.clear()
        self._error_details_toggle.setChecked(False)
        self._error_details_toggle.hide()
        self._error_details.clear()
        self.changed.emit()


__all__ = ["ObjectEditor"]
