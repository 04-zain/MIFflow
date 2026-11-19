"""Editable Qt sample rows backed only by immutable manifest drafts.

The widget consumes the current presentation model and emits researcher actions through its
controller. Expensive native or scientific operations use managed jobs, while the Qt thread
retains the selected form and its response identity."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import csv
import io
import re
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel,
    QEvent,
    QModelIndex,
    QObject,
    QSignalBlocker,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QApplication,
    QBoxLayout,
    QCheckBox,
    QComboBox,
    QCompleter,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QScrollArea,
    QSizePolicy,
    QStyledItemDelegate,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from mif_pipeline.io import CanonicalIngestionError, SourceSeriesInventory, bind_source_series_selection
from mif_pipeline.io.aligned_collection import is_aligned_source_collection

from ..manifest_draft import ManifestDraft, ManifestDraftValidation, SampleDraft
from ..presenters.plan import SAMPLE_COLUMNS
from .forms import apply_widget_accessibility
from .error_copy import diagnostic_details, field_validation_summary
from .sample_detail import SampleDetailEditor, VALIDATION_GROUP_HELP
from .workbench import PageHeading, style_scientific_table
from .researcher_widgets import ContentSizedButton
from .action_bar import WorkflowActionBar

SamplePathChooser = Callable[[str, Path | None], Path | str | None]
ManifestSaveChooser = Callable[[], Path | str | None]
ImagesChooser = Callable[[], tuple[Path, ...]]
_PATH_COLUMNS = frozenset({"source_mif_path", "moving_reference_path"})
_GROUP_COLUMNS = {
    "validation_domain_id": (
        "Processing group",
        VALIDATION_GROUP_HELP,
    ),
}
_COLUMN_LABELS = {
    "enabled": "Enabled",
    "sample_id": "Sample name",
    "slide_id": "Slide ID",
    "subject_id": "Subject ID",
    "source_mif_path": "mIF image",
    "source_image_index": "Image index",
    "source_series_index": "Series index",
    "moving_reference_path": "Paired image",
    "moving_reference_modality": "Paired-image modality",
    "moving_reference_id": "Paired image name",
    "moving_reference_source_image_id": "Image ID within file",
}
_SOURCE_INDEX_COLUMNS = frozenset({"source_image_index", "source_series_index"})


@dataclass(frozen=True)
class _PairedImageIssue:
    message: str
    can_retry: bool = True


# ==============================================================================
#                                    Samples
# ==============================================================================


def _paired_label(path: Path, samples: list[SampleDraft], row: int) -> str:
    stem = path.stem
    if stem.lower().endswith(".ome"):
        stem = stem[:-4]
    base = (re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-_")[:73] or "image") + "-paired"
    used = {sample.moving_reference_id for index, sample in enumerate(samples) if index != row}
    label, suffix = base, 2
    while label in used:
        label = f"{base}-{suffix}"
        suffix += 1
    return label


_OPTIONAL_COLUMNS = frozenset(
    {
        "validation_domain_id",
        "moving_reference_path",
        "moving_reference_modality",
        "moving_reference_id",
        "moving_reference_source_image_id",
    }
)

_LABEL_COLUMNS = ("sample_id", "subject_id", "validation_domain_id")
_LABEL_FIELDS = frozenset(("enabled", *_LABEL_COLUMNS))
_BULK_FIELDS = ("subject_id", "validation_domain_id")


class _LabelSuggestionDelegate(QStyledItemDelegate):
    """Use ordinary table cells and create only the active cell's editor."""

    def createEditor(self, parent, option, index):
        if SAMPLE_COLUMNS[index.column()] == "sample_id":
            editor = QLineEdit(parent)
            editor.setStyleSheet(
                "QLineEdit { border: 1px solid #006b70; border-radius: 0; "
                "padding: 0 9px; min-height: 0; }"
            )
            return editor
        editor = QComboBox(parent)
        editor.setEditable(True)
        # A table row has less height than a form. In particular, the combo's
        # child line edit must not inherit the form's own border and padding.
        editor.setStyleSheet(
            "QComboBox { border: 1px solid #006b70; border-radius: 0; "
            "padding: 0 26px 0 9px; min-height: 0; }"
            "QComboBox QLineEdit { border: none; padding: 0; min-height: 0; background: transparent; }"
        )
        editor.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        model = index.model()
        values = {
            model.data(model.index(row, index.column()), Qt.ItemDataRole.EditRole)
            for row in range(model.rowCount())
        }
        if SAMPLE_COLUMNS[index.column()] == "validation_domain_id":
            values.add(model.draft.validation_domain_id)
        editor.addItems(sorted((value for value in values if value), key=str.casefold))
        editor.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        editor.completer().setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        return editor

    def setEditorData(self, editor, index):
        line = editor.lineEdit() if isinstance(editor, QComboBox) else editor
        line.setText(index.data(Qt.ItemDataRole.EditRole) or "")
        line.selectAll()

    def setModelData(self, editor, model, index):
        text = editor.currentText() if isinstance(editor, QComboBox) else editor.text()
        model.setData(index, text, Qt.ItemDataRole.EditRole)

    def updateEditorGeometry(self, editor, option, index):
        editor.setGeometry(option.rect)

    def eventFilter(self, editor, event):
        if event.type() == QEvent.Type.KeyPress and event.matches(QKeySequence.StandardKey.Paste):
            text = QApplication.clipboard().text()
            if "\t" in text or "\n" in text or "\r" in text:
                # A click now focuses a text editor. Keep spreadsheet blocks
                # on the table's atomic paste path instead of flattening them
                # into one label; plain text still pastes at the caret.
                self.commitData.emit(editor)
                self.closeEditor.emit(editor, QStyledItemDelegate.EndEditHint.NoHint)
                self.parent().paste_labels_requested.emit()
                return True
        return super().eventFilter(editor, event)


class _SampleListView(QTableView):
    """Show a few sample rows without reserving empty table space."""

    paste_labels_requested = Signal()

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        index = self.indexAt(event.position().toPoint())
        # Modified clicks and drags belong to spreadsheet selection. A plain
        # click opens a field without installing widgets for every sample.
        if (
            event.button() == Qt.MouseButton.LeftButton
            and event.modifiers() == Qt.KeyboardModifier.NoModifier
            and self.editTriggers() != QTableView.EditTrigger.NoEditTriggers
            and self.isEnabled()
            and index.isValid()
            and SAMPLE_COLUMNS[index.column()] in _LABEL_FIELDS - {"enabled"}
            and len(self.selectionModel().selectedIndexes()) == 1
        ):
            self.edit(index)

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        model = self.model()
        rows = [row for row in range(model.rowCount()) if not self.isRowHidden(row)][:8] if model is not None else []
        height = (
            (self.horizontalHeader().height() if not self.horizontalHeader().isHidden() else 0)
            + sum(self.rowHeight(row) for row in rows)
            + 2 * self.frameWidth()
        )
        return QSize(hint.width(), height)


class _SampleTableModel(QAbstractTableModel):
    """Mutable Qt editing surface that republishes a frozen ManifestDraft."""

    def __init__(
        self,
        draft: ManifestDraft,
        changed: Callable[[ManifestDraft], None],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._draft = draft
        self._changed = changed
        self._samples = list(draft.samples)
        self._undo_drafts = []
        self._redo_drafts = []
        self._next_row_id = 1
        self._row_ids = [self._allocate_row_id() for _sample in self._samples]

    def _allocate_row_id(self) -> str:
        row_id = f"sample-row-{self._next_row_id}"
        self._next_row_id += 1
        return row_id

    @property
    def draft(self) -> ManifestDraft:
        return self._draft

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._samples)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(SAMPLE_COLUMNS)

    def data(
        self,
        index: QModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if not self._valid(index):
            return None
        field = SAMPLE_COLUMNS[index.column()]
        value = getattr(self._samples[index.row()], field)
        if field == "enabled" and role == Qt.ItemDataRole.CheckStateRole:
            return Qt.CheckState.Checked if value else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.UserRole:
            return value
        if role == Qt.ItemDataRole.ToolTipRole:
            if field == "validation_domain_id" and value is None:
                return f"Uses the analysis processing group: {self._draft.validation_domain_id}"
            return "" if value is None else str(value)
        if role in {Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole}:
            if field == "enabled":
                return bool(value) if role == Qt.ItemDataRole.EditRole else ""
            if field == "validation_domain_id" and value is None and role == Qt.ItemDataRole.DisplayRole:
                return f"{self._draft.validation_domain_id} (default)"
            if field == "source_mif_path" and value is not None and role == Qt.ItemDataRole.DisplayRole:
                if value.name.lower().endswith(".mif-source.json"):
                    return self._samples[index.row()].sample_id + " · aligned files"
                bound = self._samples[index.row()].source_image_index
                shared = sum(sample.source_mif_path == value for sample in self._samples) > 1
                return value.name + (f" · image {bound + 1}" if shared and bound is not None else "")
            return "" if value is None else str(value)
        return None

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if section < 0:
            return None
        if orientation == Qt.Orientation.Horizontal:
            if section >= len(SAMPLE_COLUMNS):
                return None
            field = SAMPLE_COLUMNS[section]
            if role == Qt.ItemDataRole.ToolTipRole:
                if field == "enabled":
                    return "Include this sample"
                if field == "subject_id":
                    return "Use the same Subject ID for samples from the same person or animal."
                return _GROUP_COLUMNS[field][1] if field in _GROUP_COLUMNS else None
            if role == Qt.ItemDataRole.DisplayRole:
                if field == "enabled":
                    return "Use"
                if field == "validation_domain_id":
                    return "Processing group"
                return (
                    _GROUP_COLUMNS[field][0] if field in _GROUP_COLUMNS else _COLUMN_LABELS[field]
                )
            return None
        if orientation == Qt.Orientation.Vertical:
            if role == Qt.ItemDataRole.DisplayRole and section < len(self._row_ids):
                return self._row_ids[section]
            return None
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not self._valid(index):
            return Qt.ItemFlag.NoItemFlags
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if SAMPLE_COLUMNS[index.column()] not in _SOURCE_INDEX_COLUMNS:
            flags |= Qt.ItemFlag.ItemIsEditable
        if SAMPLE_COLUMNS[index.column()] == "enabled":
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def setData(
        self,
        index: QModelIndex,
        value: object,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        if not self._valid(index):
            return False
        field = SAMPLE_COLUMNS[index.column()]
        if field in _SOURCE_INDEX_COLUMNS:
            return False
        if role == Qt.ItemDataRole.CheckStateRole and field == "enabled":
            decoded: object = value == Qt.CheckState.Checked.value or value == Qt.CheckState.Checked
        elif role == Qt.ItemDataRole.EditRole:
            try:
                decoded = self._decode(field, value)
            except (TypeError, ValueError):
                return False
        else:
            return False
        try:
            if field.startswith("moving_reference_") and decoded == getattr(
                self._samples[index.row()], field
            ):
                return True
            changes = {field: decoded}
            if field == "source_mif_path" and decoded != self._samples[index.row()].source_mif_path:
                changes.update(
                    source_image_index=None, source_series_index=None, source_file_sha256=None
                )
            if (
                field == "moving_reference_path"
                and decoded != self._samples[index.row()].moving_reference_path
            ):
                changes.update(
                    moving_reference_modality=None,
                    moving_reference_id=_paired_label(decoded, self._samples, index.row()),
                    moving_reference_source_image_id=None,
                    moving_reference_source_metadata={},
                )
            if (
                field == "moving_reference_path"
                and decoded
                and self._samples[index.row()].moving_reference_path is None
            ):
                from ..analysis_settings import copy_settings

                overrides = copy_settings(self._samples[index.row()].overrides)
                if overrides.get("registration", {}).get("mode") == "none":
                    overrides.pop("registration")
                changes["overrides"] = overrides
            updated = replace(self._samples[index.row()], **changes)
            if field.startswith("moving_reference_"):
                from ..paired_reference import refresh_paired_reference

                updated = refresh_paired_reference(updated)
        except (TypeError, ValueError):
            return False
        if updated == self._samples[index.row()]:
            return True
        if field in _LABEL_FIELDS:
            self._remember_labels()
        self._samples[index.row()] = updated
        if (
            field == "validation_domain_id"
            and decoded
            and decoded != self._draft.validation_domain_id
            and decoded not in self._draft.validation_domains
        ):
            self._draft = replace(
                self._draft,
                validation_domains=(*self._draft.validation_domains, decoded),
            )
        self._publish(label_edit=field in _LABEL_FIELDS)
        last_field = (
            "source_series_index"
            if field == "source_mif_path"
            else "moving_reference_source_image_id" if field == "moving_reference_path" else field
        )
        last_index = self.index(index.row(), SAMPLE_COLUMNS.index(last_field))
        self.dataChanged.emit(
            index,
            last_index,
            [
                Qt.ItemDataRole.DisplayRole,
                Qt.ItemDataRole.EditRole,
                Qt.ItemDataRole.CheckStateRole,
            ],
        )
        return True

    def _decode(self, field: str, value: object) -> object:
        if field == "enabled":
            if type(value) is bool:
                return value
            text = str(value).strip().casefold()
            if text in {"true", "1", "yes"}:
                return True
            if text in {"false", "0", "no"}:
                return False
            raise ValueError("enabled must be a boolean")
        text = "" if value is None else str(value).strip()
        if field in _OPTIONAL_COLUMNS and not text:
            return None
        if field in _PATH_COLUMNS:
            if not text:
                raise ValueError("source image path must not be blank")
            return Path(text).expanduser().resolve(strict=False)
        return text

    def _valid(self, index: QModelIndex) -> bool:
        return (
            index.isValid()
            and 0 <= index.row() < len(self._samples)
            and 0 <= index.column() < len(SAMPLE_COLUMNS)
        )

    def _publish(self, *, label_edit=False) -> None:
        # Source inspection, paired-image and row-structure changes invalidate
        # label undo so it cannot restore an earlier image binding or row list.
        if not label_edit:
            self._undo_drafts.clear()
            self._redo_drafts.clear()
        self._draft = replace(self._draft, samples=tuple(self._samples))
        self._changed(self._draft)

    def _remember_labels(self) -> None:
        self._undo_drafts.append(self._draft)
        del self._undo_drafts[:-20]
        self._redo_drafts.clear()

    def apply_labels(self, changes) -> None:
        """Apply a complete label edit once, retaining every source binding.

        Decode all supplied row/field values into temporary immutable samples
        before publishing. Invalid rows, non-label fields or undecodable values
        leave the draft untouched. The manifest owner still validates identity
        syntax, uniqueness and processing-group relationships on Validate/Save.
        """
        samples = list(self._samples)
        groups = list(self._draft.validation_domains)
        touched = []
        for row, field, value in changes:
            if field not in _LABEL_FIELDS or not 0 <= row < len(samples):
                raise ValueError("Paste labels into existing sample rows; source images are selected on Images.")
            decoded = self._decode(field, value)
            samples[row] = replace(samples[row], **{field: decoded})
            if field == "validation_domain_id" and decoded and decoded != self._draft.validation_domain_id and decoded not in groups:
                groups.append(decoded)
            touched.append(row)
        if samples == self._samples:
            return
        self._remember_labels()
        self._samples = samples
        self._draft = replace(self._draft, validation_domains=tuple(groups))
        self._publish(label_edit=True)
        self.dataChanged.emit(self.index(min(touched), 0), self.index(max(touched), self.columnCount() - 1))

    def undo_labels(self, *, redo=False) -> None:
        """Restore one label transaction; source/row changes clear this history."""
        source, target = (self._redo_drafts, self._undo_drafts) if redo else (self._undo_drafts, self._redo_drafts)
        if not source:
            return
        target.append(self._draft)
        self._draft = source.pop()
        self._samples = list(self._draft.samples)
        self._publish(label_edit=True)
        self.dataChanged.emit(self.index(0, 0), self.index(self.rowCount() - 1, self.columnCount() - 1))

    def append_blank(self) -> int:
        row = len(self._samples)
        row_id = self._allocate_row_id()
        placeholder = self._draft.output_root / f".{row_id}-select-source"
        sample = SampleDraft(
            enabled=False,
            sample_id="",
            slide_id="",
            subject_id="",
            validation_domain_id=None,
            source_mif_path=placeholder,
            moving_reference_path=None,
            moving_reference_modality=None,
        )
        self.beginInsertRows(QModelIndex(), row, row)
        self._samples.append(sample)
        self._row_ids.append(row_id)
        self.endInsertRows()
        self._publish()
        return row

    def append_images(self, paths: tuple[Path, ...]) -> int | None:
        """Name selected files without inferring subjects or image semantics."""
        seen = {sample.source_mif_path for sample in self._samples}
        used_names = {sample.sample_id for sample in self._samples}
        used_names.update(sample.slide_id for sample in self._samples)
        added = []
        for selected in paths:
            path = Path(selected).expanduser().resolve(strict=False)
            if path in seen:
                continue
            stem = path.stem
            if stem.lower().endswith(".ome"):
                stem = stem[:-4]
            base = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-_")[:80] or "sample"
            name, suffix = base, 2
            while name in used_names:
                name = f"{base}-{suffix}"
                suffix += 1
            used_names.add(name)
            seen.add(path)
            added.append(
                SampleDraft(
                    enabled=True,
                    sample_id=name,
                    slide_id=name,
                    subject_id="",
                    validation_domain_id=None,
                    source_mif_path=path,
                    moving_reference_path=None,
                    moving_reference_modality=None,
                )
            )
        if not added:
            return None
        first = len(self._samples)
        self.beginInsertRows(QModelIndex(), first, first + len(added) - 1)
        self._samples.extend(added)
        self._row_ids.extend(self._allocate_row_id() for _ in added)
        self.endInsertRows()
        self._publish()
        return first

    def duplicate(self, row: int) -> int | None:
        if not 0 <= row < len(self._samples):
            return None
        target = len(self._samples)
        self.beginInsertRows(QModelIndex(), target, target)
        self._samples.append(self._samples[row])
        self._row_ids.append(self._allocate_row_id())
        self.endInsertRows()
        self._publish()
        return target

    def remove(self, row: int) -> bool:
        if not 0 <= row < len(self._samples):
            return False
        self.beginRemoveRows(QModelIndex(), row, row)
        del self._samples[row]
        del self._row_ids[row]
        self.endRemoveRows()
        self._publish()
        return True

    def update_path(self, row: int, field: str, path: Path | str) -> bool:
        if field not in _PATH_COLUMNS or not 0 <= row < len(self._samples):
            return False
        column = SAMPLE_COLUMNS.index(field)
        return self.setData(self.index(row, column), str(path), Qt.ItemDataRole.EditRole)

    def clear_reference(self, row: int) -> bool:
        if not 0 <= row < len(self._samples):
            return False
        sample = self._samples[row]
        updated = replace(
            sample,
            moving_reference_path=None,
            moving_reference_modality=None,
            moving_reference_id=None,
            moving_reference_source_image_id=None,
            moving_reference_source_metadata={},
        )
        from ..paired_reference import refresh_paired_reference

        updated = refresh_paired_reference(updated)
        if updated == sample:
            return True
        self._samples[row] = updated
        self._publish()
        first = self.index(row, SAMPLE_COLUMNS.index("moving_reference_path"))
        last = self.index(row, SAMPLE_COLUMNS.index("moving_reference_source_image_id"))
        self.dataChanged.emit(first, last, [Qt.ItemDataRole.DisplayRole])
        return True

    def set_source_selection(
        self,
        row: int,
        image_index: int,
        series_index: int,
        source_file_sha256: str,
    ) -> bool:
        if not 0 <= row < len(self._samples):
            return False
        updated = replace(
            self._samples[row],
            source_image_index=image_index,
            source_series_index=series_index,
            source_file_sha256=source_file_sha256,
        )
        if updated == self._samples[row]:
            return True
        self._samples[row] = updated
        self._publish()
        first = self.index(row, SAMPLE_COLUMNS.index("source_image_index"))
        last = self.index(row, SAMPLE_COLUMNS.index("source_series_index"))
        self.dataChanged.emit(first, last, [Qt.ItemDataRole.DisplayRole])
        return True

    def bind_source_images(self, row, selections, source_file_sha256):
        """Replace a file row with exact, distinctly named image bindings."""
        original = self._samples[row]
        used = {item.sample_id for item in self._samples} | {item.slide_id for item in self._samples}
        bound = []
        for index, selection in enumerate(selections):
            name = original.sample_id
            if index:
                base = f"{original.sample_id[:65]}-image-{selection.source_image_index + 1}"
                name, suffix = base, 2
                while name in used:
                    name = f"{base}-{suffix}"
                    suffix += 1
                used.add(name)
            bound.append(replace(original, sample_id=name,
                slide_id=original.slide_id if index == 0 else name,
                source_image_index=selection.source_image_index,
                source_series_index=selection.source_series_index,
                source_file_sha256=source_file_sha256))
        self.beginResetModel()
        self._samples[row:row + 1] = bound
        self._row_ids[row:row + 1] = [self._row_ids[row], *(self._allocate_row_id() for _ in bound[1:])]
        self.endResetModel()
        self._publish()


class SamplesPage(QWidget):
    """Edit a ManifestDraft and emit requests for later managed work."""

    draft_changed = Signal(object)
    validate_requested = Signal(object)
    save_requested = Signal(object, object)
    source_series_inspection_requested = Signal(object, object)
    source_series_selection_requested = Signal(object, object, object)
    paired_image_inspection_requested = Signal(object, object)

    def __init__(
        self,
        *,
        draft: ManifestDraft,
        path_chooser: SamplePathChooser | None = None,
        images_chooser: ImagesChooser | None = None,
        manifest_save_chooser: ManifestSaveChooser | None = None,
        managed_setup_path: Path | None = None,
        native_operations=None,
        parent: QWidget | None = None,
    ) -> None:
        if type(draft) is not ManifestDraft:
            raise TypeError("Samples page requires a ManifestDraft")
        super().__init__(parent)
        self._path_chooser = path_chooser or self._choose_sample_path
        self._images_chooser = images_chooser or self._choose_images
        self._manifest_save_chooser = manifest_save_chooser or self._choose_manifest_path
        self._managed_setup_path = managed_setup_path
        # Keep an existing exclusion explicit without adding a second inclusion
        # decision for newly imported images, which are enabled by default.
        self._label_columns = (("enabled", *_LABEL_COLUMNS)
                               if any(not sample.enabled for sample in draft.samples)
                               else _LABEL_COLUMNS)
        self._native_operations = native_operations
        self._step = "images"
        self._locked = False
        self._committing_previous_row = False
        self._source_checks = {}
        self._source_inventories = {}
        self._source_choices = []
        self._source_dialog = None
        self._paired_checks = {}
        self._paired_requests = {}
        self._next_paired_request = 1
        self._paired_choices = []
        self._paired_dialog = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(16)
        layout.addWidget(
            PageHeading(
                "samples",
                "Samples",
                "Add your images, then assign subjects and processing groups.",
                self,
            )
        )
        self._plan_lock_notice = QLabel(
            "Plan prepared. This analysis setup is locked for this run.",
            self,
        )
        self._plan_lock_notice.setObjectName("samples-plan-lock")
        self._plan_lock_notice.setWordWrap(True)
        self._plan_lock_notice.hide()
        layout.addWidget(self._plan_lock_notice)

        body_scroll = QScrollArea(self)
        self._body_scroll = body_scroll
        body_scroll.setObjectName("samples-body-scroll")
        body_scroll.setWidgetResizable(True)
        body_scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        body_scroll.setMinimumHeight(0)
        body_widget = QWidget(body_scroll)
        self._editor_body = body_widget
        body_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        body_layout = QBoxLayout(QBoxLayout.Direction.TopToBottom, body_widget)
        self._body_layout = body_layout
        list_panel = QWidget(body_widget)
        self._list_panel = list_panel
        list_panel.setMinimumWidth(0)
        list_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        list_layout = QVBoxLayout(list_panel)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_frame = QFrame(list_panel)
        self._list_frame = list_frame
        list_frame.setObjectName("samples-list-frame")
        list_frame.setProperty("workbenchSection", True)
        list_frame_layout = QVBoxLayout(list_frame)
        list_frame_layout.setContentsMargins(1, 1, 1, 1)
        list_frame_layout.setSpacing(0)
        self._list_header = QWidget(list_frame)
        self._list_header.setObjectName("samples-list-header")
        list_header_layout = QVBoxLayout(self._list_header)
        list_header_layout.setContentsMargins(12, 10, 12, 12)
        list_header_layout.setSpacing(7)
        title_row = QHBoxLayout()
        list_title = QLabel("Imported images", self._list_header)
        self._list_title = list_title
        list_title.setObjectName("samples-list-heading")
        list_title.setProperty("panelHeading", True)
        title_row.addWidget(list_title, 1)
        self._list_count = QLabel(self._list_header)
        self._list_count.setObjectName("samples-list-count")
        self._list_count.setProperty("choiceHelp", True)
        title_row.addWidget(self._list_count)
        list_header_layout.addLayout(title_row)
        self._label_hint = QLabel("Click a field to edit. Shift-click to select cells, then paste spreadsheet rows or fill them with one value.", list_panel)
        self._label_hint.setWordWrap(True)
        self._label_hint.setProperty("choiceHelp", True)
        list_header_layout.addWidget(self._label_hint)
        self._search = QLineEdit(self._list_header)
        self._search.setObjectName("samples-filter")
        self._search.setPlaceholderText("Find a sample, subject or image…")
        self._search.setAccessibleName("Filter samples")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._filter_rows)
        list_header_layout.addWidget(self._search)
        list_frame_layout.addWidget(self._list_header)
        list_layout.addWidget(list_frame)
        detail_panel = QWidget(body_widget)
        self._detail_panel = detail_panel
        detail_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.addWidget(list_panel, 1)
        # Keep the existing 24-pixel gap while drawing a quiet separator within
        # it. The paired step shares the page surface on both sides.
        self._paired_separator = QWidget(body_widget)
        self._paired_separator.setObjectName("samples-paired-separator")
        self._separator_layout = QVBoxLayout(self._paired_separator)
        self._separator_layout.setSpacing(0)
        separator_line = QFrame(self._paired_separator)
        separator_line.setStyleSheet("background: #dce4e4; border: none;")
        self._separator_layout.addWidget(separator_line)
        body_layout.addWidget(self._paired_separator)
        body_layout.addWidget(detail_panel, 2)
        body_scroll.setWidget(body_widget)
        layout.addWidget(body_scroll, 1)

        self._empty = QLabel("No samples yet. Choose Add images to begin.", list_panel)
        self._empty.setObjectName("samples-empty")
        self._empty.setProperty("choiceHelp", True)
        self._empty.setWordWrap(True)
        self._empty.setContentsMargins(12, 8, 12, 12)
        list_frame_layout.addWidget(self._empty)
        self._empty.setText(
            "No samples yet. Choose Add images to begin." if not draft.samples else ""
        )
        self._empty.setVisible(not draft.samples)

        self._table = _SampleListView(list_panel)
        self._table.setObjectName("samples-table")
        self._model = _SampleTableModel(draft, self._accept_draft, self._table)
        self._table.setModel(self._model)
        self._table.paste_labels_requested.connect(self._paste_labels)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._show_label_context_menu)
        for field in ("sample_id", "subject_id", "validation_domain_id"):
            self._table.setItemDelegateForColumn(
                SAMPLE_COLUMNS.index(field), _LabelSuggestionDelegate(self._table)
            )
        style_scientific_table(self._table)
        self._table.setStyleSheet(
            "QTableView#samples-table { border: none; border-radius: 0; padding: 0; }"
        )
        self._table.setWordWrap(False)
        self._table.setAccessibleDescription("Click a sample label to edit, or press F2. Copy and paste spreadsheet cells with Ctrl+C and Ctrl+V. Use Shift to select a rectangle.")
        self._table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._table.verticalHeader().hide()
        self._table.selectionModel().currentRowChanged.connect(self._render_sample_detail)
        for column, field in enumerate(SAMPLE_COLUMNS):
            self._table.setColumnHidden(
                column,
                field
                not in {
                    "enabled",
                    "sample_id",
                    "validation_domain_id",
                    "source_mif_path",
                },
            )
        header = self._table.horizontalHeader()
        header.setStyleSheet("QHeaderView, QHeaderView::section { font-weight: 700; }")
        header.setFixedHeight(header.fontMetrics().lineSpacing() + 16)
        for field, label, minimum in (
            ("enabled", "Use", 72),
            ("sample_id", "Sample name", 130),
            ("validation_domain_id", "Processing group", 160),
        ):
            column = SAMPLE_COLUMNS.index(field)
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            self._table.setColumnWidth(
                column, max(minimum, header.fontMetrics().horizontalAdvance(label) + 24)
            )
        header.setSectionResizeMode(
            SAMPLE_COLUMNS.index("source_mif_path"), QHeaderView.ResizeMode.Stretch
        )
        self._table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self._table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._table.setMinimumWidth(0)
        self._table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._model.rowsInserted.connect(self._table.updateGeometry)
        self._model.rowsRemoved.connect(self._table.updateGeometry)
        self._table.setVisible(bool(draft.samples))
        list_frame_layout.addWidget(self._table)
        self._label_tools = QWidget(list_panel)
        label_tools = QVBoxLayout(self._label_tools)
        label_tools.setContentsMargins(0, 0, 0, 0)
        label_tools.setSpacing(10)
        self._selection_summary = QLabel(self)
        self._selection_summary.setProperty("choiceHelp", True)
        label_tools.addWidget(self._selection_summary)
        self._bulk_row = QWidget(self._label_tools)
        bulk = QHBoxLayout(self._bulk_row)
        bulk.setContentsMargins(0, 0, 0, 0)
        self._bulk_field = QComboBox(self)
        self._bulk_field.setObjectName("samples-bulk-field")
        self._bulk_field.setAccessibleName("Field to apply to selected samples")
        self._bulk_field.addItem("Set selected samples…", None)
        for field in _BULK_FIELDS:
            self._bulk_field.addItem(_GROUP_COLUMNS[field][0] if field in _GROUP_COLUMNS else _COLUMN_LABELS[field], field)
        self._bulk_field.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._bulk_field.setMinimumContentsLength(17)
        bulk.addWidget(self._bulk_field)
        self._bulk_value = QComboBox(self)
        self._bulk_value.setObjectName("samples-bulk-value")
        self._bulk_value.setAccessibleName("Value for selected samples")
        self._bulk_value.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._bulk_value.setMinimumContentsLength(10)
        bulk.addWidget(self._bulk_value, 1)
        self._bulk_apply = ContentSizedButton("Apply to selection", self)
        self._bulk_apply.setObjectName("samples-bulk-apply")
        self._bulk_apply.clicked.connect(self._apply_bulk_labels)
        bulk.addWidget(self._bulk_apply)
        label_tools.addWidget(self._bulk_row)
        self._bulk_field.currentIndexChanged.connect(self._refresh_bulk_values)
        self._table.selectionModel().selectionChanged.connect(self._update_selection)
        list_layout.addWidget(self._label_tools)
        for key, callback in ((QKeySequence.StandardKey.Copy, self._copy_labels), (QKeySequence.StandardKey.Paste, self._paste_labels), (QKeySequence.StandardKey.Undo, self._undo_labels), (QKeySequence.StandardKey.Redo, self._redo_labels)):
            history_action = key in (QKeySequence.StandardKey.Undo, QKeySequence.StandardKey.Redo)
            shortcut = QShortcut(QKeySequence(key), self if history_action else self._table)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut if history_action
                                else Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(callback)

        row_actions_widget = QWidget(list_panel)
        self._image_actions = row_actions_widget
        row_actions = QHBoxLayout(row_actions_widget)
        row_actions.setContentsMargins(0, 0, 0, 0)
        add_images = ContentSizedButton("Add images…", list_panel)
        add_images.setObjectName("samples-add-images")
        add_images.setToolTip("Select one or more source image files.")
        add_images.setProperty("actionRole", "primary")
        add_images.clicked.connect(self._add_images)
        row_actions.addWidget(add_images)
        group_images = ContentSizedButton("Group cycle files…", list_panel)
        self._group_images = group_images
        group_images.setObjectName("samples-group-aligned-files")
        group_images.setToolTip("Select two or more cycle files from the same tissue to group as one sample.")
        group_images.clicked.connect(self._group_aligned_files)
        ungroup_images = ContentSizedButton("Ungroup files", list_panel)
        self._ungroup_images = ungroup_images
        ungroup_images.setObjectName("samples-ungroup-files")
        ungroup_images.setToolTip("Restore a grouped sample's original file rows to change its selection or cycle order.")
        ungroup_images.clicked.connect(self._ungroup_aligned_files)
        for label, handler in (("Remove", self._remove),):
            button = ContentSizedButton(label, list_panel)
            button.setObjectName(f"samples-{label.lower()}")
            button.clicked.connect(handler)
            row_actions.addWidget(button)
        self._source_copy = ContentSizedButton("Copy path", list_panel)
        self._source_copy.setObjectName("samples-copy-source-path")
        self._source_copy.setToolTip("Copy the complete path of each selected image (Ctrl+C).")
        self._source_copy.clicked.connect(self._copy_source_paths)
        row_actions.addWidget(self._source_copy)
        row_actions.addStretch(1)
        row_actions.addWidget(group_images)
        row_actions.addWidget(ungroup_images)
        list_layout.addWidget(row_actions_widget)
        self._source_panel = QWidget(list_panel)
        source_layout = QHBoxLayout(self._source_panel)
        source_layout.setContentsMargins(0, 0, 0, 0)
        self._source_help = QLabel(self._source_panel)
        self._source_help.setWordWrap(True)
        self._source_help.setTextFormat(Qt.TextFormat.PlainText)
        source_layout.addWidget(self._source_help, 1)
        self._source_use = ContentSizedButton("Choose images…", self._source_panel)
        self._source_use.setObjectName("source-images-choose")
        self._source_use.clicked.connect(self._resolve_selected_source)
        source_layout.addWidget(self._source_use)
        self._source_panel.hide()
        list_layout.addWidget(self._source_panel)
        list_layout.addStretch(1)

        self._detail = SampleDetailEditor(detail_panel)
        self._detail.setObjectName("samples-detail")
        self._detail.field_edited.connect(self._edit_detail_field)
        self._detail.browse_requested.connect(self._browse_field)
        self._detail.inspect_requested.connect(self._request_paired_image_inspection)
        detail_layout.addWidget(self._detail)
        body_scroll.viewport().installEventFilter(self)

        detail_layout.addStretch(1)

        self._error = QLabel("", self)
        self._error.setObjectName("sample-field-error")
        self._error.setWordWrap(True)
        self._error.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self._error)
        self._error_details_toggle = QToolButton(self)
        self._error_details_toggle.setObjectName("sample-field-error-details-toggle")
        self._error_details_toggle.setText("Details")
        self._error_details_toggle.setCheckable(True)
        self._error_details_toggle.hide()
        layout.addWidget(self._error_details_toggle)
        self._error_details = QLabel("", self)
        self._error_details.setObjectName("sample-field-error-details")
        self._error_details.setWordWrap(True)
        self._error_details.setTextFormat(Qt.TextFormat.PlainText)
        self._error_details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._error_details.hide()
        layout.addWidget(self._error_details)
        self._error_details_toggle.toggled.connect(self._error_details.setVisible)

        actions = WorkflowActionBar(self)
        self._back_button = ContentSizedButton("Back: images", self)
        self._back_button.setObjectName("samples-back")
        self._back_button.clicked.connect(self._back_to_images)
        actions.add_primary(self._back_button)
        self._next_button = ContentSizedButton("Next: label samples", self)
        self._next_button.setObjectName("samples-next")
        self._next_button.setProperty("actionRole", "primary")
        self._next_button.clicked.connect(self._label_samples)
        actions.add_primary(self._next_button)
        save = ContentSizedButton("Save workspace file", self)
        save.setObjectName("samples-save-manifest")
        if managed_setup_path is not None:
            save.setText("Save analysis")
        save.clicked.connect(self._save)
        actions.add_utility(save)
        self._save_button = save
        layout.addWidget(actions)
        self._refresh_bulk_values()
        self._set_step("images")
        apply_widget_accessibility(self, "samples")

    def resizeEvent(self, event: object) -> None:
        super().resizeEvent(event)
        self._update_body_layout()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._body_scroll.viewport() and event.type() == QEvent.Type.Resize:
            self._update_body_layout()
        return super().eventFilter(watched, event)

    def _update_body_layout(self) -> None:
        if not hasattr(self, "_detail"):
            return
        margins = self._body_layout.contentsMargins()
        available = self._body_scroll.viewport().width() - margins.left() - margins.right()
        list_width = max(self._list_header.minimumSizeHint().width(),
                         self._search.fontMetrics().horizontalAdvance("Find sample or subject…") + 26)
        paired = self._step == "paired"
        side_by_side = paired and available >= list_width + self._detail.readable_width() + 24
        self._list_panel.setMinimumWidth(list_width if side_by_side else 0)
        self._detail_panel.setMinimumWidth(self._detail.readable_width() if side_by_side else 0)
        self._paired_separator.setVisible(paired)
        self._paired_separator.setMinimumSize(0, 0)
        self._paired_separator.setMaximumSize(16777215, 16777215)
        if side_by_side:
            self._paired_separator.setFixedWidth(24)
            self._separator_layout.setContentsMargins(11, 12, 12, 0)
        else:
            self._paired_separator.setFixedHeight(24)
            self._separator_layout.setContentsMargins(0, 11, 0, 12)
        self._body_layout.setSpacing(0 if paired else 6)
        self._body_layout.setDirection(QBoxLayout.Direction.LeftToRight if side_by_side
                                       else QBoxLayout.Direction.TopToBottom)

    def _set_step(self, step: str) -> None:
        self._advance_after_validation = False
        self._step = step if self.draft.samples else "images"
        editing = self._step != "images"
        labels = self._step == "labels"
        paired = self._step == "paired"
        self._list_frame.setStyleSheet(
            "QFrame#samples-list-frame { background: transparent; border: 1px solid transparent; border-radius: 0; }"
            if paired else ""
        )
        self._table.setStyleSheet(
            "QTableView#samples-table { border: none; border-radius: 0; padding: 0;"
            + (" background: transparent; alternate-background-color: transparent;" if paired else "")
            + " }"
        )
        self._update_body_layout()
        self.findChild(QLabel, "samples-information").setText({
            "images": "Add your images, then assign subjects and processing groups.",
            "labels": "Name your samples and assign subjects and processing groups.",
            "paired": "Add images of the same tissue for comparison or alignment with mIF.",
        }[self._step])
        self._detail_panel.setVisible(self._step == "paired")
        self._image_actions.setVisible(not editing)
        self._refresh_source_status()
        self._list_title.setText("Sample labels" if labels else "Samples" if editing else "Imported images")
        self._search.setPlaceholderText("Find sample or subject…" if editing else "Find sample, subject or image…")
        self._label_tools.setVisible(labels)
        self._label_hint.setVisible(labels)
        self._back_button.setVisible(editing)
        self._back_button.setText("Back: labels" if self._step == "paired" else "Back: images")
        self._next_button.setVisible(self._step != "paired")
        self._next_button.setText(
            "Next: paired images" if self._step == "labels" else "Next: label samples"
        )
        self._next_button.setEnabled(bool(self.draft.samples) and not self._locked and not self._source_checks)
        visible = set(self._label_columns) if labels else {"sample_id"} if editing else {"source_mif_path"}
        for column, field in enumerate(SAMPLE_COLUMNS):
            self._table.setColumnHidden(column, field not in visible)
            if field in visible:
                self._table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed if field == "enabled" else QHeaderView.ResizeMode.Stretch)
        self._table.setColumnWidth(SAMPLE_COLUMNS.index("enabled"), 50)
        if labels:
            group_column = SAMPLE_COLUMNS.index("validation_domain_id")
            header = self._table.horizontalHeader()
            header.setSectionResizeMode(group_column, QHeaderView.ResizeMode.Fixed)
            self._table.setColumnWidth(
                group_column,
                max(160, header.fontMetrics().horizontalAdvance("Processing group") + 28),
            )
        self._table.setSelectionBehavior(QTableView.SelectionBehavior.SelectItems if labels else QTableView.SelectionBehavior.SelectRows)
        self._table.horizontalHeader().setVisible(self._step != "paired")
        self._table.setSelectionMode(QTableView.SelectionMode.ContiguousSelection if labels else
            QTableView.SelectionMode.ExtendedSelection if self._step == "images" else QTableView.SelectionMode.SingleSelection)
        self._table.setEditTriggers((QTableView.EditTrigger.DoubleClicked | QTableView.EditTrigger.EditKeyPressed | QTableView.EditTrigger.AnyKeyPressed) if labels and not self._locked else QTableView.EditTrigger.NoEditTriggers)
        save = self._save_button
        save.setProperty("actionRole", "primary" if self._step == "paired" else "secondary")
        save.style().unpolish(save)
        save.style().polish(save)
        self._update_selection()

    def _label_samples(self) -> None:
        if self._locked or self._source_checks or not self.draft.samples or not self.commit_pending():
            return
        if self._step == "labels":
            self._advance_after_validation = True
            self.validate_requested.emit(self.draft)
            return
        self._set_step("labels")
        if self._selected_row() < 0:
            self._table.setCurrentIndex(self._model.index(0, SAMPLE_COLUMNS.index("sample_id")))

    def _back_to_images(self) -> None:
        if not self._locked and self.commit_pending():
            self._set_step("labels" if self._step == "paired" else "images")

    def _filter_rows(self, *_args) -> None:
        """Filter the visible sample rows without altering inclusion or row identity."""
        needle = self._search.text().casefold().strip()
        for row, sample in enumerate(self.draft.samples):
            text = " ".join(str(getattr(sample, field) or "") for field in (*_LABEL_FIELDS, "source_mif_path"))
            self._table.setRowHidden(row, needle not in text.casefold())
        if self._step == "paired" and (self._selected_row() < 0 or self._table.isRowHidden(self._selected_row())):
            first = next((row for row in range(self._model.rowCount())
                          if not self._table.isRowHidden(row)), None)
            self._table.setCurrentIndex(
                self._model.index(first, SAMPLE_COLUMNS.index("sample_id"))
                if first is not None else QModelIndex()
            )
        self._table.updateGeometry()
        self._update_selection()

    def _selected_label_rows(self):
        return sorted({index.row() for index in self._table.selectionModel().selectedIndexes()
                       if not self._table.isRowHidden(index.row())})

    def _select_all_labels(self) -> None:
        """Select the displayed samples for one bulk label assignment."""
        if self._step == "labels" and self.commit_pending():
            self._table.selectAll()

    def _show_label_context_menu(self, position) -> None:
        if self._step != "labels":
            return
        menu = QMenu(self._table)
        for label, key, callback, enabled in (
            ("Copy", QKeySequence.StandardKey.Copy, self._copy_labels, bool(self._selected_label_rows())),
            ("Paste", QKeySequence.StandardKey.Paste, self._paste_labels, not self._locked),
            ("Undo", QKeySequence.StandardKey.Undo, self._undo_labels, bool(self._model._undo_drafts) and not self._locked),
            ("Redo", QKeySequence.StandardKey.Redo, self._redo_labels, bool(self._model._redo_drafts) and not self._locked),
            ("Select all shown", QKeySequence.StandardKey.SelectAll, self._select_all_labels, True),
        ):
            action = menu.addAction(label)
            action.setShortcut(QKeySequence(key))
            action.setEnabled(enabled)
            action.triggered.connect(callback)
        menu.exec(self._table.viewport().mapToGlobal(position))
        menu.deleteLater()

    def _update_selection(self, *_args) -> None:
        self._refresh_cycle_actions()
        count = len(self._selected_label_rows())
        self._source_copy.setText("Copy paths" if count > 1 else "Copy path")
        self._source_copy.setEnabled(count > 0)
        shown = sum(not self._table.isRowHidden(row) for row in range(self._model.rowCount()))
        total = self._model.rowCount()
        self._list_count.setText(f"{shown} of {total}" if shown != total else f"{total} sample" + ("s" if total != 1 else ""))
        self._list_count.setAccessibleName(f"{shown} of {total} samples shown")
        if total:
            self._empty.setText("No samples match. Clear the search to show all samples.")
            self._empty.setVisible(not shown)
        self._table.setVisible(bool(shown))
        self._selection_summary.setText(f"{shown} of {self._model.rowCount()} samples shown · {count} selected")
        self._bulk_apply.setText(f"Apply to {count} sample" + ("s" if count != 1 else ""))
        self._bulk_apply.setEnabled(bool(count and self._bulk_field.currentData() and not self._locked))
        self._bulk_row.setVisible(count >= 2)

    def _refresh_cycle_actions(self) -> None:
        samples = tuple(self.draft.samples[row] for row in self._selected_label_rows())
        grouped = tuple(is_aligned_source_collection(sample.source_mif_path) for sample in samples)
        self._group_images.setEnabled(
            not self._locked and len(samples) >= 2 and not any(grouped)
            and not any((sample.sample_id, sample.source_mif_path) in self._source_checks
                        for sample in samples)
        )
        self._ungroup_images.setVisible(len(samples) == 1 and grouped[0])
        self._ungroup_images.setEnabled(not self._locked)

    def _refresh_bulk_values(self, *_args, preserve=False) -> None:
        """Offer existing labels while keeping the bulk assignment explicit."""
        field = self._bulk_field.currentData()
        previous = self._bulk_value.currentText() if preserve else ""
        with QSignalBlocker(self._bulk_value):
            self._bulk_value.clear()
            self._bulk_value.setEditable(True)
            values = [getattr(sample, field) for sample in self.draft.samples] if field else []
            if field == "validation_domain_id":
                values = [self.draft.validation_domain_id, *self.draft.validation_domains, *values]
            self._bulk_value.addItems(tuple(dict.fromkeys(str(value) for value in values if value)))
            self._bulk_value.setEditText(previous)
            self._bulk_value.lineEdit().setPlaceholderText("Enter or choose a value…")
            self._bulk_value.setEnabled(field is not None)
        self._update_selection()

    def _apply_bulk_labels(self) -> None:
        if self._locked or self._step != "labels" or not self.commit_pending():
            return
        field = self._bulk_field.currentData()
        if field not in _BULK_FIELDS:
            return
        value = self._bulk_value.currentText()
        try:
            self._model.apply_labels([(row, field, value) for row in self._selected_label_rows()])
        except (TypeError, ValueError) as error:
            self._error.setText(str(error))

    def _copy_labels(self) -> None:
        """Copy a selected rectangle with editable column captions as TSV headers."""
        if self._step == "images":
            self._copy_source_paths()
            return
        if self._step != "labels" or not self.commit_pending():
            return
        cells = [index for index in self._table.selectionModel().selectedIndexes()
                 if not self._table.isRowHidden(index.row()) and not self._table.isColumnHidden(index.column())]
        if not cells:
            return
        rows = sorted({index.row() for index in cells})
        columns = sorted({index.column() for index in cells})
        stream = io.StringIO()
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(str(self._model.headerData(column, Qt.Orientation.Horizontal)).replace("\n", " ") for column in columns)
        for row in rows:
            writer.writerow(self._model.data(self._model.index(row, column), Qt.ItemDataRole.EditRole) for column in columns)
        QApplication.clipboard().setText(stream.getvalue())

    def _copy_source_paths(self) -> None:
        """Keep complete source locations available when the table shows filenames."""
        rows = self._selected_label_rows()
        if self._step == "images" and rows:
            QApplication.clipboard().setText("\n".join(
                str(self.draft.samples[row].source_mif_path) for row in rows
            ))

    def _paste_labels(self) -> None:
        """Paste one complete TSV block into visible label rows as one draft edit.

        Optional headers choose label columns explicitly. Without headers, the
        current cell anchors the rectangle. Filtered rows are skipped, existing
        rows are never reordered, and a malformed block changes no sample.
        """
        if self._locked or self._step != "labels" or not self.commit_pending():
            return
        try:
            text = QApplication.clipboard().text()
            if not text.strip():
                raise ValueError("Copy sample labels from a spreadsheet, then paste into a selected cell.")
            block = list(csv.reader(io.StringIO(text), delimiter="\t", strict=True))
            width = len(block[0])
            if not width or any(len(row) != width for row in block):
                raise ValueError("Paste a rectangle with the same number of columns in every row.")
            selected = [index for index in self._table.selectionModel().selectedIndexes()
                        if not self._table.isRowHidden(index.row())
                        and not self._table.isColumnHidden(index.column())]
            if len(block) == 1 and width == 1 and len(selected) > 1:
                self._model.apply_labels([
                    (index.row(), SAMPLE_COLUMNS[index.column()], block[0][0])
                    for index in selected
                ])
                return
            aliases = {field.casefold(): field for field in SAMPLE_COLUMNS}
            aliases.update({label.casefold(): field for field, label in _COLUMN_LABELS.items()})
            aliases.update({"processing group": "validation_domain_id", "use": "enabled"})
            header = [aliases.get(value.strip().casefold()) for value in block[0]]
            if any(header):
                if any(field not in self._label_columns for field in header) or len(set(header)) != len(header):
                    raise ValueError("Paste Sample name, Subject ID or Processing group columns; each header must appear once.")
                fields, block = header, block[1:]
            else:
                column = self._table.currentIndex().column()
                visible = [field for index, field in enumerate(SAMPLE_COLUMNS) if not self._table.isColumnHidden(index) and index >= column]
                fields = visible[:width]
                if len(fields) != width:
                    raise ValueError("The pasted columns extend beyond the label table. Select the starting cell or include column headers.")
            start = self._selected_row()
            if start < 0 or self._table.isRowHidden(start):
                raise ValueError("Select a visible sample cell to choose where the pasted rows begin.")
            rows = [row for row in range(start, self._model.rowCount()) if not self._table.isRowHidden(row)]
            if not block or len(block) > len(rows):
                raise ValueError("Paste values for the existing visible samples. Add source images before adding sample rows.")
            self._model.apply_labels([(row, field, value) for row, values in zip(rows, block) for field, value in zip(fields, values)])
        except (csv.Error, TypeError, ValueError) as error:
            self._error.setText(str(error))

    def _undo_labels(self) -> None:
        if not self._locked and self._step == "labels" and self.commit_pending():
            self._model.undo_labels()

    def _redo_labels(self) -> None:
        if not self._locked and self._step == "labels" and self.commit_pending():
            self._model.undo_labels(redo=True)

    @property
    def draft(self) -> ManifestDraft:
        return self._model.draft

    def select_sample(self, sample_id: str) -> bool:
        """Focus the first sample matching a project-panel identity."""
        for row, sample in enumerate(self.draft.samples):
            if sample.sample_id == sample_id:
                if not self.commit_pending():
                    return False
                if self._step == "images" and not self._source_checks:
                    self._set_step("labels")
                self._table.setCurrentIndex(
                    self._model.index(row, SAMPLE_COLUMNS.index("sample_id"))
                )
                self._table.scrollTo(self._table.currentIndex())
                return self._selected_row() == row
        return False

    def load_cluster_samples(self, draft: ManifestDraft) -> None:
        """Install a verified saved sample list into an empty local display."""
        if (
            self._locked
            or self.draft.samples
            or (draft.batch_id, draft.output_root) != (self.draft.batch_id, self.draft.output_root)
        ):
            raise ValueError("Open an existing cluster analysis before adding local samples.")
        self._model.beginResetModel()
        self._model._draft = draft
        self._model._samples = list(draft.samples)
        self._model._row_ids = [self._model._allocate_row_id() for _ in draft.samples]
        self._model.endResetModel()
        self._prune_source_checks()
        self._accept_draft(draft)
        self.lock_for_plan()
        self._plan_lock_notice.setText("Samples opened from the saved cluster analysis.")

    def show_corrected_source(self, sample_id, selection) -> None:
        """Refresh one locked source display after the retained correction commits."""
        if not self._locked:
            raise ValueError("Source corrections belong to a saved analysis.")
        samples = tuple(replace(sample, source_mif_path=selection.source_identity.resolved_path,
            source_image_index=selection.source_image_index, source_series_index=selection.source_series_index,
            source_file_sha256=selection.source_identity.file_sha256) if sample.sample_id == sample_id else sample
            for sample in self.draft.samples)
        self._model.beginResetModel()
        self._model._samples = list(samples)
        self._model._draft = replace(self.draft, samples=samples)
        self._model.endResetModel()
        self._prune_source_checks()
        self._accept_draft(self.draft)

    def lock_for_plan(self, *, prepared: bool = True) -> None:
        """Keep published sample inputs read-only for this analysis."""
        self._locked = True
        self._editor_body.setEnabled(False)
        self._back_button.setEnabled(False)
        self._next_button.setEnabled(False)
        self._save_button.setEnabled(False)
        self._plan_lock_notice.setText(
            "Plan prepared. This analysis setup is locked for this run."
            if prepared
            else "This analysis setup is locked for this run. Return to Plan to continue."
        )
        self._plan_lock_notice.show()

    @property
    def source_selection_pending(self):
        return bool(self._source_checks) or any(
            not isinstance(value, SourceSeriesInventory) for value in self._paired_checks.values()
        )

    @property
    def field_error(self) -> str:
        return self._error.text()

    def _accept_draft(self, draft: ManifestDraft) -> None:
        self._empty.setText(
            "No samples yet. Choose Add images to begin." if not draft.samples else ""
        )
        self._empty.setVisible(not draft.samples)
        self._table.setVisible(bool(draft.samples))
        self._next_button.setEnabled(bool(draft.samples) and not self._locked)
        if not draft.samples:
            self._set_step("images")
        if not self._committing_previous_row:
            row = self._selected_row()
            self._detail.set_sample(draft.samples[row] if 0 <= row < len(draft.samples) else None)
        self._error.clear()
        self._error_details_toggle.setChecked(False)
        self._error_details_toggle.hide()
        self._error_details.clear()
        self._filter_rows()
        self._refresh_bulk_values(preserve=True)
        self._prune_source_checks()
        self._prune_paired_checks()
        self._refresh_paired_status()
        self.draft_changed.emit(draft)

    def _commit_pending_detail(self, row: int) -> bool:
        if self._locked or self._step != "paired":
            return True
        if not 0 <= row < len(self.draft.samples):
            return True
        pending = []
        sample = self.draft.samples[row]
        for field in SAMPLE_COLUMNS:
            if not field.startswith("moving_reference_"):
                continue
            editor = self._detail.findChild(QWidget, f"sample-detail-{field}")
            if isinstance(editor, QLineEdit):
                value = editor.text()
            elif isinstance(editor, QComboBox):
                value = self._detail.field_value(field)
            elif isinstance(editor, QCheckBox):
                value = editor.isChecked()
            else:
                continue
            current = getattr(sample, field)
            displayed = current if field == "enabled" else "" if current is None else str(current)
            if value == displayed:
                continue
            try:
                self._model._decode(field, value)
            except (TypeError, ValueError):
                message = f"Check {_COLUMN_LABELS.get(field, field)}."
                self._error.setText(message)
                self._detail.show_field_error(field, message)
                return False
            pending.append((field, value))
        path_replaced = any(
            field == "moving_reference_path"
            and value.strip()
            and self._model._decode(field, value) != sample.moving_reference_path
            for field, value in pending
        )
        for field, value in pending:
            if (
                path_replaced
                and field.startswith("moving_reference_")
                and field != "moving_reference_path"
            ):
                continue
            if field == "moving_reference_path" and not str(value).strip():
                accepted = self._model.clear_reference(row)
            else:
                accepted = self._model.setData(
                    self._model.index(row, SAMPLE_COLUMNS.index(field)), value
                )
            if not accepted:
                return False
            if field == "moving_reference_path" and path_replaced:
                self._request_paired_image_inspection(row)
        return True

    def _add(self) -> None:
        if not self.commit_pending():
            return
        row = self._model.append_blank()
        self._table.setCurrentIndex(self._model.index(row, 1))

    def _add_images(self) -> None:
        if self._locked or not self.commit_pending():
            return
        selected = tuple(self._images_chooser())
        if not selected:
            return
        row = self._model.append_images(selected)
        if row is not None:
            self._table.setCurrentIndex(self._model.index(row, 1))
            for sample in self.draft.samples[row:]:
                key = (sample.sample_id, sample.source_mif_path)
                self._source_checks[key] = "pending"
                self.source_series_inspection_requested.emit(*key)
            self._refresh_source_status()

    def _choose_images(self) -> tuple[Path, ...]:
        selected, _ = QFileDialog.getOpenFileNames(
            self,
            "Add images",
            "",
            "Whole-slide images (*.qptiff *.tif *.tiff *.svs *.ndpi *.vsi *.scn *.czi);;All files (*)",
        )
        return tuple(Path(path) for path in selected)

    def _selected_row(self) -> int:
        index = self._table.currentIndex()
        return index.row() if index.isValid() else -1

    def _group_aligned_files(self) -> None:
        if self._locked or not self.commit_pending():
            return
        rows = tuple(self._selected_label_rows())
        if len(rows) < 2:
            self._error.setText("Select at least two inspected cycle image rows to group.")
            return
        samples = tuple(self.draft.samples[row] for row in rows)
        if any(is_aligned_source_collection(sample.source_mif_path) for sample in samples):
            self._error.setText("Select original cycle file rows. Ungroup a grouped sample to change its files.")
            return
        keys = tuple((sample.sample_id, sample.source_mif_path) for sample in samples)
        if any(key in self._source_checks for key in keys):
            self._error.setText("Complete image inspection and image selection before grouping these files.")
            return
        if any(key not in self._source_inventories for key in keys):
            for key in keys:
                if key not in self._source_inventories:
                    self._source_checks[key] = "pending"
                    self.source_series_inspection_requested.emit(*key)
            self._refresh_source_status()
            self._error.setText("The selected images are being checked. Select the rows and group them when inspection finishes.")
            return
        from .aligned_collection import AlignedCollectionDialog
        dialog = AlignedCollectionDialog(samples, self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        original = self.draft
        def finished(result):
            if not result or self._locked:
                return
            try:
                if self.draft != original:
                    raise ValueError("The sample list changed. Select the files again.")
                name, order, cycles = dialog.choices()
                selected_rows = tuple(rows[i] for i in order)
                if dialog.alignment == "already_registered":
                    self._apply_aligned_group(selected_rows, name, cycles)
                else:
                    self._start_cycle_registration(selected_rows, name, cycles)
            except (ValueError, OSError) as error:
                self._error.setText(str(error))
        dialog.finished.connect(finished)
        dialog.open()

    def _start_cycle_registration(self, rows, name, cycles):
        from ..aligned_collection import collection_from_samples
        from .cycle_registration import CycleRegistrationDialog
        inventories = tuple(self._source_inventories[(self.draft.samples[row].sample_id,
            self.draft.samples[row].source_mif_path)] for row in rows)
        collection = collection_from_samples(self.draft, rows, name, cycles, inventories)
        original = self.draft
        dialog = CycleRegistrationDialog(collection,
            self.draft.output_root / self.draft.batch_id / "cycle_registration", self,
            native_operations=self._native_operations)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        def finished(result):
            if not result or self._locked:
                return
            try:
                if self.draft != original:
                    raise ValueError("The sample list changed during registration. Select the files again.")
                self._apply_aligned_group(rows, name, cycles, registration=dialog.registration)
            except (ValueError, OSError) as error:
                self._error.setText(str(error))
        dialog.finished.connect(finished)
        dialog.open()

    def _apply_aligned_group(self, rows, name, cycles, *, registration=None):
        from ..aligned_collection import group_aligned_samples
        inventories = tuple(self._source_inventories[(self.draft.samples[row].sample_id,
            self.draft.samples[row].source_mif_path)] for row in rows)
        draft, row = group_aligned_samples(self.draft, rows, name, cycles, inventories, registration=registration)
        self._model.beginResetModel()
        self._model._samples = list(draft.samples)
        self._model._row_ids = [self._model._allocate_row_id() for _ in draft.samples]
        self._model.endResetModel()
        self._model._publish()
        self._prune_source_checks()
        sample = draft.samples[row]
        key = (sample.sample_id, sample.source_mif_path)
        self._source_checks[key] = "pending"
        self._table.setCurrentIndex(self._model.index(row, SAMPLE_COLUMNS.index("source_mif_path")))
        self._error.clear()
        self.source_series_inspection_requested.emit(*key)
        self._refresh_source_status()

    def _ungroup_aligned_files(self):
        if self._locked or not self.commit_pending():
            return
        from ..aligned_collection import ungroup_aligned_sample
        rows = self._selected_label_rows()
        if len(rows) != 1 or not is_aligned_source_collection(self.draft.samples[rows[0]].source_mif_path):
            self._error.setText("Select one grouped sample to restore its original files.")
            return
        row = rows[0]
        try:
            original_count = len(self.draft.samples)
            draft = ungroup_aligned_sample(self.draft, row)
        except (ValueError, OSError) as error:
            self._error.setText(str(error))
            return
        count = len(draft.samples) - original_count + 1
        self._model.beginResetModel()
        self._model._samples = list(draft.samples)
        self._model._row_ids = [self._model._allocate_row_id() for _ in draft.samples]
        self._model.endResetModel()
        self._model._publish()
        self._prune_source_checks()
        self._error.clear()
        for sample in draft.samples[row:row + count]:
            key = (sample.sample_id, sample.source_mif_path)
            self._source_checks[key] = "pending"
            self.source_series_inspection_requested.emit(*key)
        self._table.setCurrentIndex(self._model.index(row, SAMPLE_COLUMNS.index("source_mif_path")))
        self._refresh_source_status()

    def _remove(self) -> None:
        if self._locked or not self.commit_pending():
            return
        row = self._selected_row()
        with QSignalBlocker(self._table.selectionModel()):
            self._model.remove(row)
            if self.draft.samples and self._selected_row() < 0:
                self._table.setCurrentIndex(
                    self._model.index(
                        min(row, len(self.draft.samples) - 1),
                        SAMPLE_COLUMNS.index("source_mif_path"),
                    )
                )
        self._render_sample_detail(self._table.currentIndex(), QModelIndex())
        self._prune_source_checks()

    def _duplicate(self) -> None:
        if not self.commit_pending():
            return
        row = self._model.duplicate(self._selected_row())
        if row is not None:
            self._table.setCurrentIndex(self._model.index(row, 1))

    def _edit_detail_field(self, field_name: str, raw_value: object) -> None:
        if self._locked:
            return
        row = self._selected_row()
        if field_name == "paired_reference_mode" and row >= 0:
            from ..paired_reference import configure_paired_reference

            try:
                self._model._samples[row] = configure_paired_reference(
                    self.draft.samples[row], raw_value
                )
                self._model._publish()
            except ValueError as error:
                self._error.setText(str(error))
            return
        if row < 0 or field_name not in SAMPLE_COLUMNS:
            return
        if field_name == "moving_reference_path" and not str(raw_value).strip():
            self._model.clear_reference(row)
            return
        index = self._model.index(row, SAMPLE_COLUMNS.index(field_name))
        previous = getattr(self.draft.samples[row], field_name)
        if not self._model.setData(index, raw_value, Qt.ItemDataRole.EditRole):
            label = self._model.headerData(index.column(), Qt.Orientation.Horizontal)
            message = f"Check {label}."
            self._error.setText(message)
            self._detail.show_field_error(field_name, message)
        elif field_name == "moving_reference_path" and previous != self.draft.samples[row].moving_reference_path:
            key = self._paired_check_key(row)
            if key is not None and key not in self._paired_checks:
                self._request_paired_image_inspection(row)

    def _browse_field(self, field: str) -> None:
        if self._locked:
            return
        row = self._selected_row()
        if row < 0 or field not in _PATH_COLUMNS:
            return
        current = getattr(self._model.draft.samples[row], field)
        selected = self._path_chooser(field, current)
        if selected is None:
            return
        self._edit_detail_field(field, str(selected))

    def _render_sample_detail(self, current: object, previous: object) -> None:
        if isinstance(previous, QModelIndex) and previous.isValid():
            # Selection already points at the next row. Keep rendering the
            # previous row until all of its pending controls can be committed.
            # Publishing a valid field must not replace the remaining editors
            # with the next sample before a later validation failure rolls back.
            self._committing_previous_row = True
            try:
                accepted = self._commit_pending_detail(previous.row())
                if not accepted:
                    with QSignalBlocker(self._table.selectionModel()):
                        self._table.setCurrentIndex(previous)
                    return
            finally:
                self._committing_previous_row = False
        row = current.row() if isinstance(current, QModelIndex) and current.isValid() else -1
        enabled = 0 <= row < len(self.draft.samples)
        self._detail.set_sample(self.draft.samples[row] if enabled else None)
        self._refresh_source_status()
        self._refresh_paired_status()

    def _paired_check_key(self, row=None):
        row = self._selected_row() if row is None else row
        if not 0 <= row < len(self.draft.samples):
            return None
        path = self.draft.samples[row].moving_reference_path
        return (self._model._row_ids[row], path) if path is not None else None

    def _prune_paired_checks(self) -> None:
        current = {self._paired_check_key(row) for row in range(len(self.draft.samples))}
        self._paired_checks = {key: value for key, value in self._paired_checks.items() if key in current}
        self._paired_requests = {request: key for request, key in self._paired_requests.items() if key in current}

    def _request_paired_image_inspection(self, row=None) -> None:
        """Inspect one paired file through the same managed reader as source images."""
        if self._locked:
            return
        key = self._paired_check_key(row)
        if key is None:
            return
        # Rechecking the same path supersedes its earlier request as well.
        self._paired_requests = {request: old_key for request, old_key in self._paired_requests.items()
                                 if old_key != key}
        request = f"paired-image-{self._next_paired_request}"
        self._next_paired_request += 1
        self._paired_requests[request] = key
        self._paired_checks[key] = "pending"
        self._refresh_paired_status()
        self.paired_image_inspection_requested.emit(request, key[1])

    def paired_inspection_is_current(self, request, source_path) -> bool:
        key = self._paired_requests.get(request)
        return bool(not self._locked and key is not None and key[1] == source_path
                    and any(self._paired_check_key(row) == key for row in range(len(self.draft.samples))))

    def paired_image_inspection_failed(self, request, source_path, message, *, can_retry=True) -> None:
        if not self.paired_inspection_is_current(request, source_path):
            return
        key = self._paired_requests.pop(request)
        self._paired_checks[key] = _PairedImageIssue(str(message), can_retry)
        self._refresh_paired_status()

    def set_paired_image_inventory(self, request, source_path, inventory) -> bool:
        """Auto-select a unique image or ask for one inspected native series."""
        if not self.paired_inspection_is_current(request, source_path):
            return False
        if not isinstance(inventory, SourceSeriesInventory):
            raise TypeError("paired image inspection requires a SourceSeriesInventory")
        if inventory.source_identity.resolved_path != source_path:
            self.paired_image_inspection_failed(request, source_path,
                "Image inspection returned a different file. Check this image again.")
            return False
        key = self._paired_requests[request]
        row = self._model._row_ids.index(key[0])
        sample = self.draft.samples[row]
        eligible = tuple(item for item in inventory.candidates if item.eligibility == "eligible")
        if not eligible:
            self.paired_image_inspection_failed(request, source_path,
                "No supported full-resolution image was found. Choose a two-dimensional image.", can_retry=False)
            return False
        if sample.moving_reference_source_image_id is not None:
            retained = tuple(item for item in eligible if item.image_id == sample.moving_reference_source_image_id)
            if len(retained) != 1:
                self.paired_image_inspection_failed(request, source_path,
                    "This file's image differs from the saved selection. Remove the paired image, then choose the file again to replace it.", can_retry=False)
                return False
            return self._apply_paired_image(request, source_path, inventory, retained[0].candidate_id)
        if len(eligible) == 1:
            return self._apply_paired_image(request, source_path, inventory, eligible[0].candidate_id)
        # Validate the retained inventory before presenting any selectable rows.
        try:
            for candidate in eligible:
                bind_source_series_selection(inventory, candidate_id=candidate.candidate_id)
        except (CanonicalIngestionError, TypeError, ValueError) as error:
            self.paired_image_inspection_failed(request, source_path, str(error))
            return False
        self._paired_checks[key] = _PairedImageIssue("Choose the image to pair with this sample.", False)
        self._paired_choices.append((request, source_path, inventory))
        self._refresh_paired_status()
        self._show_next_paired_choice()
        return False

    def _show_next_paired_choice(self) -> None:
        if self._paired_dialog is not None or self._locked:
            return
        while self._paired_choices:
            request, source_path, inventory = self._paired_choices.pop(0)
            if not self.paired_inspection_is_current(request, source_path):
                continue
            from .source_options import SourceImagesDialog
            dialog = SourceImagesDialog(inventory, self, single_selection=True)
            self._paired_dialog = dialog
            def finished(result, request=request, source_path=source_path, inventory=inventory, dialog=dialog):
                self._paired_dialog = None
                if self.paired_inspection_is_current(request, source_path):
                    choices = dialog.selected_candidate_ids() if result else ()
                    if len(choices) == 1:
                        self._apply_paired_image(request, source_path, inventory, choices[0])
                    else:
                        self.paired_image_inspection_failed(request, source_path,
                            "No image selected. Check this file again to choose an image.")
                dialog.deleteLater()
                self._show_next_paired_choice()
            dialog.finished.connect(finished)
            dialog.open()
            break

    def _apply_paired_image(self, request, source_path, inventory, candidate_id) -> bool:
        if not self.paired_inspection_is_current(request, source_path):
            return False
        key = self._paired_requests[request]
        row = self._model._row_ids.index(key[0])
        sample = self.draft.samples[row]
        from mif_pipeline.orchestration.batch_settled_review_revision import paired_reference_from_inventory
        from ..paired_reference import refresh_paired_reference
        try:
            paired = paired_reference_from_inventory(inventory,
                reference_id=sample.moving_reference_id or "paired-image", modality="other",
                candidate_id=candidate_id)
            metadata = dict(sample.moving_reference_source_metadata)
            selection = paired.source_metadata["source_series_selection"]
            if metadata.get("source_series_selection", selection) != selection:
                raise ValueError("This file differs from the saved selection. Remove the paired image and choose it again.")
            metadata.update(paired.source_metadata)
            updated = refresh_paired_reference(replace(sample,
                moving_reference_source_image_id=paired.source_image_id,
                moving_reference_source_metadata=metadata))
        except (CanonicalIngestionError, TypeError, ValueError) as error:
            self.paired_image_inspection_failed(request, source_path, str(error))
            return False
        self._paired_requests.pop(request)
        self._paired_checks[key] = inventory
        self._model._samples[row] = updated
        self._model._publish()
        self._refresh_paired_status()
        return True

    def _refresh_paired_status(self) -> None:
        if not hasattr(self, "_detail"):
            return
        key = self._paired_check_key()
        state = self._paired_checks.get(key)
        if state == "pending":
            self._detail.set_image_inspection("Reading image metadata…")
        elif isinstance(state, SourceSeriesInventory):
            image_id = self.draft.samples[self._selected_row()].moving_reference_source_image_id
            candidate = next(item for item in state.candidates if item.image_id == image_id and item.eligibility == "eligible")
            dimensions = (f" · {candidate.size_x:,} × {candidate.size_y:,} pixels"
                          if candidate.size_x and candidate.size_y else "")
            name = candidate.image_display_name if candidate.image_display_name != candidate.image_id else "Single image"
            self._detail.set_image_inspection(name + dimensions,
                                              source_image_id=candidate.image_id)
        elif isinstance(state, _PairedImageIssue):
            self._detail.set_image_inspection(state.message, can_check=state.can_retry, failed=True)

    def _prune_source_checks(self) -> None:
        current = {(sample.sample_id, sample.source_mif_path) for sample in self.draft.samples}
        self._source_checks = {key: value for key, value in self._source_checks.items() if key in current}
        self._source_choices = [key for key in self._source_choices if key in current]
        self._refresh_source_status()

    def _source_check_key(self):
        row = self._selected_row()
        if 0 <= row < len(self.draft.samples):
            sample = self.draft.samples[row]
            key = (sample.sample_id, sample.source_mif_path)
            if key in self._source_checks:
                return key
        return next(iter(self._source_checks), None)

    def _refresh_source_status(self) -> None:
        if not hasattr(self, "_source_panel"):
            return
        self._refresh_cycle_actions()
        key = self._source_check_key()
        value = self._source_checks.get(key)
        pending = sum(value == "pending" for value in self._source_checks.values())
        message = f"Reading image metadata… ({pending} remaining)" if pending else ""
        choose = isinstance(value, SourceSeriesInventory)
        failed = isinstance(value, str) and value != "pending"
        if choose:
            message = f"Choose images from {key[1].name}."
        elif failed:
            message = f"{key[1].name}: {value}"
        self._source_help.setText(message)
        self._source_use.setText("Choose images…" if choose else "Check image again")
        self._source_use.setVisible(choose or failed)
        self._source_panel.setVisible(self._step == "images" and bool(message))
        unresolved = bool(self._source_checks)
        if hasattr(self, "_next_button"):
            self._next_button.setEnabled(bool(self.draft.samples) and not self._locked and not unresolved)

    def inspect_unbound_sources(self) -> None:
        """Resume incomplete imports once; bound rows need no setup rescan."""
        for sample in self.draft.samples:
            key = (sample.sample_id, sample.source_mif_path)
            if sample.source_image_index is None and key not in self._source_checks:
                self._source_checks[key] = "pending"
                self.source_series_inspection_requested.emit(*key)
        self._refresh_source_status()

    def _request_source_series_inspection(self) -> None:
        row = self._selected_row()
        if self._locked or row < 0:
            return
        sample = self.draft.samples[row]
        key = (sample.sample_id, sample.source_mif_path)
        self._source_checks[key] = "pending"
        self._refresh_source_status()
        self.source_series_inspection_requested.emit(*key)

    def source_series_inspection_failed(self, sample_id, source_path, message) -> None:
        self._source_checks[(sample_id, source_path)] = "Image could not be read. " + message
        self._refresh_source_status()

    def set_source_series_inventory(self, sample_id, source_path, inventory) -> bool:
        """Auto-bind one eligible image; ask only when the source has several."""
        if not isinstance(inventory, SourceSeriesInventory):
            raise TypeError("source series result must be a SourceSeriesInventory")
        key = (sample_id, source_path)
        if self._locked or inventory.source_identity.resolved_path != source_path or not any(
            (sample.sample_id, sample.source_mif_path) == key for sample in self.draft.samples
        ):
            return False
        eligible = tuple(item for item in inventory.candidates if item.eligibility in {"eligible", "needs_organization"})
        self._source_inventories[key] = inventory
        if len(eligible) == 1:
            self._apply_source_images(key, inventory, (eligible[0].candidate_id,))
        elif eligible:
            self._source_checks[key] = inventory
            self._source_choices.append(key)
            self._show_next_source_choice()
        else:
            self.source_series_inspection_failed(sample_id, source_path, "No supported full-resolution fluorescence image was found.")
        self._refresh_source_status()
        return True

    def _resolve_selected_source(self) -> None:
        key = self._source_check_key()
        if self._locked or key is None:
            return
        row = next(i for i, sample in enumerate(self.draft.samples)
                   if (sample.sample_id, sample.source_mif_path) == key)
        self._table.setCurrentIndex(self._model.index(row, SAMPLE_COLUMNS.index("source_mif_path")))
        if isinstance(self._source_checks.get(key), SourceSeriesInventory):
            self._source_choices.insert(0, key)
            self._show_next_source_choice()
        else:
            self._request_source_series_inspection()

    def _show_next_source_choice(self) -> None:
        if self._source_dialog is not None or self._locked:
            return
        while self._source_choices:
            key = self._source_choices.pop(0)
            inventory = self._source_checks.get(key)
            if not isinstance(inventory, SourceSeriesInventory):
                continue
            from .source_options import SourceImagesDialog
            dialog = SourceImagesDialog(inventory, self)
            self._source_dialog = dialog
            row = next(i for i, sample in enumerate(self.draft.samples) if (sample.sample_id, sample.source_mif_path) == key)
            self._table.setCurrentIndex(self._model.index(row, SAMPLE_COLUMNS.index("source_mif_path")))
            def finished(result, key=key, inventory=inventory, dialog=dialog):
                self._source_dialog = None
                if result:
                    try:
                        self._apply_source_images(key, inventory, dialog.selected_candidate_ids())
                    except (TypeError, ValueError) as error:
                        self.source_series_inspection_failed(*key, str(error))
                dialog.deleteLater()
                self._refresh_source_status()
                self._show_next_source_choice()
            dialog.finished.connect(finished)
            dialog.open()
            break

    def _apply_source_images(self, key, inventory, candidate_ids) -> None:
        if self._locked or not candidate_ids:
            return
        row = next((i for i, sample in enumerate(self.draft.samples)
                    if (sample.sample_id, sample.source_mif_path) == key), None)
        if row is None:
            return
        # Validate every binding before changing any sample row.
        selections = tuple(bind_source_series_selection(inventory, candidate_id=value, allow_pending_organization=True) for value in candidate_ids)
        self._source_checks.pop(key, None)
        self._model.bind_source_images(row, selections, inventory.source_identity.file_sha256)
        for offset, candidate_id in enumerate(candidate_ids):
            self.source_series_selection_requested.emit(
                self.draft.samples[row + offset].sample_id, inventory.inventory_sha256, candidate_id
            )
        self._table.setCurrentIndex(self._model.index(row, SAMPLE_COLUMNS.index("source_mif_path")))
        self._refresh_source_status()

    def set_validation(self, validation: ManifestDraftValidation) -> None:
        if type(validation) is not ManifestDraftValidation:
            raise TypeError("Samples validation must be a ManifestDraftValidation")
        if validation.draft != self._model.draft:
            raise ValueError("Samples validation is stale for the current draft")
        if self._step != "labels" or self._locked:
            return
        if not self.commit_pending() or validation.draft != self.draft:
            return
        self._error.clear()
        self._error_details_toggle.setChecked(False)
        self._error_details_toggle.hide()
        self._error_details.clear()
        if not validation.errors:
            if self._advance_after_validation:
                self._set_step("paired")
            return
        self._search.clear()
        self._set_step(
            "paired" if validation.errors[0].field.startswith("moving_reference_") else "labels"
        )
        for error in validation.errors:
            if (
                error.row_index is not None
                and 0 <= error.row_index < self._model.rowCount()
                and error.field in SAMPLE_COLUMNS
            ):
                summary = field_validation_summary(
                    error.message,
                    _COLUMN_LABELS.get(error.field, "this sample"),
                    field_name=error.field,
                )
                self._error.setText(summary)
                self._error_details.setText(diagnostic_details(error.message, summary))
                self._error_details_toggle.show()
                index = self._model.index(
                    error.row_index,
                    SAMPLE_COLUMNS.index(error.field),
                )
                self._table.setCurrentIndex(index)
                self._table.scrollTo(
                    self._model.index(error.row_index, SAMPLE_COLUMNS.index("sample_id"))
                )
                if self._step == "paired":
                    self._detail.show_field_error(error.field, summary)
                else:
                    self._table.setFocus()
                return
            if error.row_index is not None and 0 <= error.row_index < self._model.rowCount():
                index = self._model.index(error.row_index, SAMPLE_COLUMNS.index("sample_id"))
                self._table.setCurrentIndex(index)
                self._table.scrollTo(index)
                summary = field_validation_summary(
                    error.message,
                    _COLUMN_LABELS.get(error.field, "this sample"),
                    field_name=error.field,
                )
                self._error.setText(summary)
                self._error_details.setText(diagnostic_details(error.message, summary))
                self._error_details_toggle.show()
                return
        if validation.errors:
            error = validation.errors[0]
            summary = field_validation_summary(
                error.message,
                _COLUMN_LABELS.get(error.field, "this analysis"),
                field_name=error.field,
            )
            self._error.setText(summary)
            self._error_details.setText(diagnostic_details(error.message, summary))
            self._error_details_toggle.show()

    def commit_pending(self) -> bool:
        """Accept visible edits before a save, plan, or close request."""
        focused = QApplication.focusWidget()
        if self._step == "labels" and focused is not None and focused is not self._table and self._table.isAncestorOf(focused):
            self._table.setFocus()
        if not self._commit_pending_detail(self._selected_row()):
            return False
        unresolved = next((key for key, value in self._paired_checks.items()
                           if not isinstance(value, SourceSeriesInventory)), None)
        if unresolved is not None:
            row = self._model._row_ids.index(unresolved[0])
            self._search.clear()
            self._set_step("paired")
            self._table.setCurrentIndex(self._model.index(row, SAMPLE_COLUMNS.index("sample_id")))
            self._refresh_paired_status()
            return False
        return True

    def _save(self) -> None:
        if not self.commit_pending():
            return
        if self._managed_setup_path is not None:
            self.save_requested.emit(self._model.draft, self._managed_setup_path)
            return
        selected = self._manifest_save_chooser()
        if selected is None or not str(selected).strip():
            return
        path = (selected if isinstance(selected, Path) else Path(selected)).expanduser()
        self.save_requested.emit(self._model.draft, path.resolve(strict=False))

    def _choose_sample_path(self, field: str, current: Path | None) -> Path | None:
        selected, _filter = QFileDialog.getOpenFileName(
            self,
            "Select paired image" if field == "moving_reference_path" else "Select local image",
            "" if current is None else str(current),
        )
        return Path(selected) if selected else None

    def _choose_manifest_path(self) -> Path | None:
        selected, _filter = QFileDialog.getSaveFileName(
            self,
            "Save workspace file",
            "",
            "JSON (*.json)",
        )
        return Path(selected) if selected else None


__all__ = ["SAMPLE_COLUMNS", "SamplesPage"]
