"""Paired-image controls for the selected sample; edits go to the page model.

The widget consumes the current presentation model and emits researcher actions through its
controller. Expensive native or scientific operations use managed jobs, while the Qt thread
retains the selected form and its response identity."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from PySide6.QtCore import QRect, QSignalBlocker, QSize, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QPushButton,
    QToolButton,
    QSizePolicy,
    QStyle,
    QStyleOptionComboBox,
    QVBoxLayout,
    QWidget,
)

from ..manifest_draft import SampleDraft

_DETAIL_FIELDS = (
    ("moving_reference_path", "Image file"),
    ("moving_reference_modality", "Image type"),
    ("moving_reference_id", "Paired image name"),
)

_CHOICE_FIELDS = ("moving_reference_modality",)
_IMAGE_TYPES = (
    ("H&E stained image", "he"),
    ("Brightfield image", "brightfield"),
    ("Fluorescence image", "fluorescence"),
    ("Other image", "other"),
)
VALIDATION_GROUP_HELP = (
    "Samples in the same processing group can share classifier training and pooled "
    "phenotyping fits. Per-slide phenotyping fits each slide separately."
)


# ==============================================================================
#                                 Sample detail
# ==============================================================================


class _EditableFieldComboBox(QComboBox):
    """Reserve the full native height of the embedded text field."""

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        line_edit = self.lineEdit()
        if line_edit is None:
            return hint
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        option.rect = QRect(0, 0, hint.width(), hint.height())
        edit_rect = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox,
            option,
            QStyle.SubControl.SC_ComboBoxEditField,
            self,
        )
        return QSize(
            hint.width(), hint.height() + max(0, line_edit.sizeHint().height() - edit_rect.height())
        )


class SampleDetailEditor(QWidget):
    """Render one immutable sample and emit literal field edits only."""

    field_edited = Signal(str, object)
    browse_requested = Signal(str)
    inspect_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        self._main_layout = layout
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(7)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        heading = QHBoxLayout()
        heading.setContentsMargins(0, 0, 0, 0)
        heading.setSpacing(12)
        self._intro = QLabel("Paired image", self)
        self._intro.setObjectName("sample-detail-intro")
        self._intro.setProperty("panelHeading", True)
        self._intro.setTextFormat(Qt.TextFormat.PlainText)
        self._intro.setWordWrap(True)
        heading.addWidget(self._intro)
        self._subject = QLabel(self)
        self._subject.setObjectName("sample-detail-subject")
        self._subject.setProperty("choiceHelp", True)
        self._subject.setTextFormat(Qt.TextFormat.PlainText)
        self._subject.setWordWrap(True)
        self._subject.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._subject.setMinimumWidth(0)
        self._subject.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        heading.addWidget(self._subject, 1)
        layout.addLayout(heading)
        self._fields: dict[str, QLineEdit | QComboBox] = {}
        self._field_errors: dict[str, QLabel] = {}
        self._field_labels: dict[str, QLabel] = {}
        reference_panel = QWidget(self)
        self._reference_panel = reference_panel
        reference_panel.setObjectName("sample-detail-reference-section")
        reference_layout = QVBoxLayout(reference_panel)
        reference_layout.setContentsMargins(0, 0, 0, 6)
        reference_form = self._new_form()
        reference_layout.addLayout(reference_form)
        layout.addWidget(reference_panel)
        self._sample_present = False

        for field, label in _DETAIL_FIELDS:
            target_form = reference_form
            if field in _CHOICE_FIELDS:
                editor = _EditableFieldComboBox(self)
                editor.setEditable(True)
                editor.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
                editor.setSizeAdjustPolicy(
                    QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
                )
                editor.setMinimumContentsLength(12)
                editor.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                editor.lineEdit().setObjectName(f"sample-detail-{field}-input")
                editor.lineEdit().setAccessibleName(label)
                if field == "moving_reference_modality":
                    for title, token in _IMAGE_TYPES:
                        editor.addItem(title, token)
                    editor.setEditText("")
                    editor.lineEdit().setPlaceholderText("Choose the staining or image type")
                editor.activated.connect(
                    lambda _index, field=field: self.field_edited.emit(
                        field, self.field_value(field)
                    )
                )
                editor.lineEdit().editingFinished.connect(
                    lambda field=field: self.field_edited.emit(field, self.field_value(field))
                )
            else:
                editor = QLineEdit(self)
                editor.editingFinished.connect(
                    lambda field=field, editor=editor: self.field_edited.emit(field, editor.text())
                )
            editor.setObjectName(f"sample-detail-{field}")
            editor.setAccessibleName(label)
            self._fields[field] = editor
            if field in {"source_mif_path", "moving_reference_path"}:
                row = QWidget(self)
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(0, 0, 0, 0)
                self._path_layout = row_layout
                row_layout.addWidget(editor, 1)
                browse = QPushButton("Browse", row)
                browse.setObjectName(f"sample-detail-browse-{field}")
                browse.setAccessibleName(f"Browse {label}")
                if field == "moving_reference_path":
                    self._reference_browse = browse
                    editor.setPlaceholderText("Choose the matching image file")
                    editor.textChanged.connect(lambda _text: self._refresh_reference_browse())
                browse.clicked.connect(
                    lambda _checked=False, field=field: self.browse_requested.emit(field)
                )
                row_layout.addWidget(browse)
                if field == "moving_reference_path":
                    remove = QPushButton("Remove", row)
                    remove.setObjectName("sample-detail-remove-moving_reference_path")
                    remove.setAccessibleName("Remove paired image")
                    remove.clicked.connect(
                        lambda: self.field_edited.emit("moving_reference_path", "")
                    )
                    row_layout.addWidget(remove)
                    self._reference_remove = remove
                # The section heading labels the paired file. Keep its control
                # row level with the Samples search instead of repeating it.
                target_form.addRow(row)
                label_widget = None
            else:
                target_form.addRow(label, editor)
                label_widget = target_form.labelForField(editor)
            if label_widget is not None:
                assert isinstance(label_widget, QLabel)
                label_widget.setObjectName(f"sample-detail-label-{field}")
                label_font = QFont(label_widget.font())
                label_font.setBold(True)
                label_widget.setFont(label_font)
                self._field_labels[field] = label_widget
            self._add_error(target_form, field)
            if field == "moving_reference_path":
                image_row = QWidget(reference_panel)
                image_layout = QHBoxLayout(image_row)
                image_layout.setContentsMargins(0, 2, 0, 8)
                self._image_summary = QLabel(image_row)
                self._image_summary.setObjectName("sample-detail-image-summary")
                self._image_summary.setProperty("choiceHelp", True)
                self._image_summary.setTextFormat(Qt.TextFormat.PlainText)
                self._image_summary.setWordWrap(True)
                self._image_summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                image_layout.addWidget(self._image_summary, 1)
                self._image_check = QPushButton("Check image", image_row)
                self._image_check.setObjectName("sample-detail-check-image")
                self._image_check.clicked.connect(self.inspect_requested.emit)
                image_layout.addWidget(self._image_check, alignment=Qt.AlignmentFlag.AlignTop)
                reference_form.addRow(image_row)
        from ..paired_reference import MODES

        self._paired_mode = QComboBox(self)
        self._paired_mode.setObjectName("sample-detail-paired-mode")
        self._paired_mode.setAccessibleName("Use paired image")
        for caption, value in MODES:
            self._paired_mode.addItem(caption, value)
        self._paired_mode.activated.connect(
            lambda _index: self.field_edited.emit(
                "paired_reference_mode", self._paired_mode.currentData()
            )
        )
        self._paired_options = QToolButton(self)
        self._paired_options.setText("Use with mIF")
        self._paired_options.setObjectName("sample-detail-paired-options")
        self._paired_options.setCheckable(True)
        from .researcher_widgets import configure_disclosure

        configure_disclosure(self._paired_options)
        reference_form.addRow(self._paired_options)
        self._paired_options_panel = QWidget(self)
        options_layout = QVBoxLayout(self._paired_options_panel)
        options_layout.setContentsMargins(0, 0, 0, 0)
        use_hint = QLabel("Compare this image beside mIF, or align it for an overlay.", self)
        use_hint.setWordWrap(True)
        use_hint.setProperty("choiceHelp", True)
        options_layout.addWidget(use_hint)
        options_layout.addWidget(self._paired_mode)
        reference_form.addRow(self._paired_options_panel)
        self._paired_options_panel.hide()
        self._paired_options.toggled.connect(self._paired_options_panel.setVisible)

        self._error = QLabel("", self)
        self._error.setObjectName("sample-detail-field-error")
        self._error.setWordWrap(True)
        layout.addWidget(self._error)
        self.set_sample(None)

    @staticmethod
    def _new_form() -> QFormLayout:
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        form.setVerticalSpacing(6)
        return form

    def readable_width(self) -> int:
        """Leave room for a useful filename beside the native file actions."""
        editor = self._fields["moving_reference_path"]
        filename = editor.fontMetrics().horizontalAdvance("paired-section-01.ome.tif") + 24
        return (filename + self._reference_browse.sizeHint().width()
                + self._reference_remove.sizeHint().width() + 2 * self._path_layout.spacing())

    def field_value(self, field: str) -> str:
        editor = self._fields[field]
        assert isinstance(editor, QComboBox)
        if field == "moving_reference_modality" and editor.currentIndex() >= 0:
            token = editor.currentData()
            if token is not None and editor.currentText() == editor.itemText(editor.currentIndex()):
                return str(token)
        return editor.currentText()

    def _add_error(self, form: QFormLayout, field: str) -> None:
        error = QLabel("", self)
        error.setObjectName(f"sample-detail-error-{field}")
        error.setWordWrap(True)
        error.hide()
        form.addRow(error)
        self._field_errors[field] = error

    def set_sample(self, sample: SampleDraft | None) -> None:
        """Refresh controls without publishing a second copy of sample state."""
        enabled = sample is not None
        self._sample_present = enabled
        from ..paired_reference import paired_reference_mode

        with QSignalBlocker(self._paired_mode):
            self._paired_mode.setCurrentIndex(
                max(0, self._paired_mode.findData(paired_reference_mode(sample)))
            )
        complete = bool(
            sample
            and sample.moving_reference_path
            and sample.moving_reference_id
            and sample.moving_reference_modality
        )
        self._paired_mode.setEnabled(complete)
        self._paired_options.setVisible(complete)
        if not complete:
            self._paired_options.setChecked(False)
        self._intro.setText("Paired image" if sample else "Select a sample to add a paired image.")
        self._subject.setText(f"{sample.sample_id} · Subject: {sample.subject_id}" if sample else "")
        self._subject.setVisible(enabled)
        self._reference_panel.setEnabled(enabled)
        self._reference_panel.setVisible(enabled)
        has_path = bool(sample and sample.moving_reference_path)
        for field, editor in self._fields.items():
            value = getattr(sample, field) if sample else None
            with QSignalBlocker(editor):
                text = "" if value is None else str(value)
                if isinstance(editor, QComboBox):
                    with QSignalBlocker(editor.lineEdit()):
                        choice = (
                            editor.findData(text)
                            if field == "moving_reference_modality" and text
                            else -1
                        )
                        if choice >= 0:
                            editor.setCurrentIndex(choice)
                        else:
                            editor.setEditText(text)
                else:
                    editor.setText(text)
            editor.setEnabled(enabled)
            editor.setToolTip("")
            if field == "moving_reference_path" and value is not None:
                editor.setToolTip(str(value))
            elif field == "moving_reference_id":
                editor.setToolTip("Name used for this paired image in the analysis.")
            if field != "moving_reference_path":
                editor.setVisible(has_path)
                self._field_labels[field].setVisible(has_path)
        self._refresh_reference_browse()
        for error in self._field_errors.values():
            error.clear()
            error.hide()
        self._error.clear()
        if not has_path:
            self.set_image_inspection("Add an H&E, brightfield or fluorescence image from the same tissue.")
        else:
            self.set_image_inspection("Saved image selection" if sample.moving_reference_source_image_id else
                                      "Check the file to identify its image.", can_check=True,
                                      source_image_id=sample.moving_reference_source_image_id)

    def _refresh_reference_browse(self) -> None:
        has_path = bool(self._fields["moving_reference_path"].text().strip())
        self._reference_browse.setText("Change…" if has_path else "Choose file…")
        self._reference_browse.setAccessibleName(
            "Change paired image" if has_path else "Add paired image"
        )
        self._reference_remove.setEnabled(has_path and self._sample_present)
        self._reference_remove.setVisible(has_path)

    def set_image_inspection(self, message: str, *, can_check=False, failed=False,
                             source_image_id: str | None = None) -> None:
        """Show inspection facts while the source's literal ID remains model-owned."""
        from .theme import WORKFLOW_PALETTE

        self._image_summary.setText(message)
        self._image_summary.setToolTip(f"Image ID: {source_image_id}" if source_image_id else "")
        warning = WORKFLOW_PALETTE["warning"]
        self._image_summary.setStyleSheet(
            f"color: {warning}; background: {WORKFLOW_PALETTE['warning_surface']}; "
            f"border-left: 3px solid {warning}; padding: 8px 10px;" if failed else ""
        )
        self._image_check.setText("Check again" if failed else "Check image")
        self._image_check.setVisible(can_check)
        self._image_check.setEnabled(can_check and self._sample_present)

    def show_field_error(self, field: str, message: str) -> None:
        self._error.clear()
        inline = self._field_errors.get(field)
        if inline is not None:
            inline.setText(message)
            inline.show()
        else:
            self._error.setText(message)
        target = self._fields.get(field)
        if target is not None:
            target.setToolTip(message)
            target.setFocus()


__all__ = ["SampleDetailEditor"]
