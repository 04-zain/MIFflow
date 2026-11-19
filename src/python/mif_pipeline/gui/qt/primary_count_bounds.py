"""Edit detected-region count rules using the configured biological class labels."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from PySide6.QtCore import QRegularExpression, Qt
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QLineEdit, QVBoxLayout, QWidget

_LIMIT_EDITOR_STYLE = (
    "QLineEdit { background: transparent; border: 1px solid transparent; "
    "border-radius: 3px; padding: 6px 8px; } "
    "QLineEdit:hover { background: #f2f7f7; } "
    "QLineEdit:focus { background: #e9f4f3; border-color: #00838a; }"
)

_LIMIT_HELP = (
    "An analysis region with a count below a required minimum is excluded from further processing.",
    "A count above a maximum requires review. Keep all classified regions or exclude the analysis region.",
)


class _InvalidLimit(ValueError):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def _bounds(label, minimum, maximum):
    from mif_pipeline.pipeline_core.config import StructureCountBounds

    try:
        minimum = int(minimum)
        if minimum < 0:
            raise ValueError
    except (TypeError, ValueError):
        raise _InvalidLimit("minimum", f"Enter a whole-number minimum of 0 or more for {label}.") from None
    if maximum is not None and str(maximum).strip():
        try:
            maximum = int(maximum)
            if maximum < 1:
                raise ValueError
        except (TypeError, ValueError):
            raise _InvalidLimit("maximum", f"Enter a positive whole-number maximum for {label}, or leave it blank.") from None
        if maximum < minimum:
            raise _InvalidLimit("maximum", f"Enter a maximum of at least {minimum} for {label}, or leave it blank.")
    else:
        maximum = None
    return StructureCountBounds(minimum=minimum, maximum=maximum)


class PrimaryClassCountBounds(QWidget):
    """Keep class IDs in the saved mapping and present labels with integer fields.

    Reducing the selected class count retains hidden rules in the form draft.
    Only rules for currently selected classes enter the submitted mapping.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._totals = None
        self._classes = ()
        self._drafts = {}
        self._configured = set()
        self._rows = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self._overall = self._section(layout, "Total regions")
        total = QLabel("All tissue classes combined", self)
        total.setContentsMargins(0, 10, 6, 10)
        self._overall.addWidget(total, 1, 0)
        divider = QFrame(self)
        divider.setFixedHeight(1)
        divider.setStyleSheet("background: #dbe4e5; border: none;")
        self._overall.addWidget(divider, 2, 0, 1, 3)
        layout.addSpacing(10)
        self._grid = self._section(layout, "Regions by tissue class")

    def _section(self, layout, title):
        heading = QLabel(title, self)
        heading.setProperty("panelHeading", True)
        layout.addWidget(heading)
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(0)
        for column, stretch in enumerate((2, 1, 1)):
            grid.setColumnStretch(column, stretch)
        for column, text in enumerate(("Minimum", "Maximum"), 1):
            label = QLabel(text, self)
            label.setProperty("stageFieldCaption", True)
            label.setContentsMargins(8, 0, 0, 10)
            label.setToolTip(_LIMIT_HELP[column - 1])
            grid.addWidget(label, 0, column)
        layout.addLayout(grid)
        return grid

    def set_total_fields(self, fields):
        """Use the same editable-column layout for totals and individual classes."""
        self._totals = tuple(caption.buddy() for _name, caption, _field in fields)
        minimum, maximum = self._totals
        for column, (name, caption, field) in enumerate(fields):
            editor = caption.buddy()
            editor.setAccessibleName("Minimum total regions" if column == 0 else "Maximum total regions")
            editor.setToolTip(_LIMIT_HELP[column])
            editor.setAccessibleDescription(_LIMIT_HELP[column])
            caption.clear()
            caption.setMaximumHeight(0)
            caption.hide()
            field.layout().insertWidget(0, caption)
            help_text = field.findChild(QLabel, "form-help-" + name)
            if help_text is not None:
                help_text.hide()
            field.layout().setContentsMargins(0, 0, 0, 0)
            field.layout().setSpacing(6)
            editor.setMinimumWidth(80)
            editor.setStyleSheet(_LIMIT_EDITOR_STYLE)
            editor.setValidator(QRegularExpressionValidator(QRegularExpression(r"\d*"), editor))
            self._overall.addWidget(field, 1, column + 1, alignment=Qt.AlignmentFlag.AlignVCenter)
        maximum.setPlaceholderText("No maximum")
        QWidget.setTabOrder(minimum, maximum)

    def _remember_rows(self):
        for class_id, (minimum, maximum) in self._rows.items():
            self._drafts[class_id] = {"minimum": minimum.text(), "maximum": maximum.text()}

    def set_classes(self, classes: Sequence[tuple[str, str]]):
        classes = tuple(classes)
        if classes == self._classes:
            return
        self._remember_rows()
        self._classes = classes
        self._rebuild()

    def set_value(self, value: Mapping[str, object]):
        self._drafts = {key: dict(bounds) for key, bounds in value.items()}
        self._configured = set(value)
        self._rebuild()

    def restore_draft(self, value: Mapping[str, object]):
        self._configured = set(value["configured"])
        self._drafts = {key: dict(bounds) for key, bounds in value["rows"].items()}
        self._rebuild()

    def capture_draft(self):
        self._remember_rows()
        return {"configured": sorted(self._configured),
                "rows": {key: dict(bounds) for key, bounds in self._drafts.items()}}

    def _rebuild(self):
        for index in reversed(range(self._grid.count())):
            if self._grid.getItemPosition(index)[0] >= 1:
                widget = self._grid.takeAt(index).widget()
                widget.hide()
                widget.deleteLater()
        self._rows = {}
        previous = self._totals[1] if self._totals else None
        for row, (class_id, label) in enumerate(self._classes, 1):
            caption = QLabel(label, self)
            caption.setTextFormat(Qt.TextFormat.PlainText)
            caption.setWordWrap(True)
            caption.setContentsMargins(0, 10, 6, 10)
            self._grid.addWidget(caption, row * 2 - 1, 0, alignment=Qt.AlignmentFlag.AlignVCenter)
            bounds = self._drafts.get(class_id, {"minimum": 0, "maximum": None})
            editors = []
            for column, kind in enumerate(("minimum", "maximum"), 1):
                editor = QLineEdit(self)
                editor.setMinimumWidth(80)
                editor.setObjectName(f"primary-count-{class_id}-{kind}")
                editor.setAccessibleName(f"{label}: {kind} detected regions")
                editor.setToolTip(_LIMIT_HELP[column - 1])
                editor.setAccessibleDescription(_LIMIT_HELP[column - 1])
                editor.setValidator(QRegularExpressionValidator(QRegularExpression(r"\d*"), editor))
                value = bounds[kind]
                editor.setText("" if value is None else str(value))
                if kind == "maximum":
                    editor.setPlaceholderText("No maximum")
                editor.setStyleSheet(_LIMIT_EDITOR_STYLE)
                self._grid.addWidget(editor, row * 2 - 1, column)
                if previous is not None:
                    QWidget.setTabOrder(previous, editor)
                previous = editor
                editors.append(editor)
            caption.setBuddy(editors[0])
            self._rows[class_id] = tuple(editors)
            line = QFrame(self)
            line.setFixedHeight(1)
            line.setStyleSheet("background: #dbe4e5; border: none;")
            self._grid.addWidget(line, row * 2, 0, 1, 3)
        self.updateGeometry()

    def field_errors(self, prefix):
        errors = {}
        if self._totals is not None:
            try:
                _bounds("All classes", *(editor.text() for editor in self._totals))
            except _InvalidLimit as exc:
                errors[f"{prefix}_count_{exc.kind}"] = str(exc)
        try:
            self.value()
        except _InvalidLimit as exc:
            errors[f"{prefix}_class_bounds"] = str(exc)
        return errors

    def value(self):
        self._remember_rows()
        values = {}
        for class_id, label in self._classes:
            row = self._drafts[class_id]
            try:
                bounds = _bounds(label, row["minimum"], row["maximum"])
            except _InvalidLimit as exc:
                self.setFocusProxy(self._rows[class_id][0 if exc.kind == "minimum" else 1])
                raise
            if class_id in self._configured or bounds.minimum != 0 or bounds.maximum is not None:
                values[class_id] = bounds.to_mapping(secondary=False)
        return values
