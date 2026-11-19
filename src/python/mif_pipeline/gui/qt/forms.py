"""Render the current stage's typed settings and collect the researcher's response.

Controls are created from presentation-neutral form records. Stage-specific visibility and
navigation retain editable drafts while the final action validates only the applicable
inputs. Training procedures directly control scalar/grid fields; detector and reference
imports wait for their current source checks. Scientific acceptance occurs in the
corresponding stage service."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
from html import escape
import math
import re
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, QSignalBlocker, Qt, Signal
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QAbstractButton,
    QBoxLayout,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QLayout,
    QSizePolicy,
    QPlainTextEdit,
    QPushButton,
    QTableView,
    QTableWidget,
    QTabBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from mif_pipeline.gui.interactions import (
    ControlKind,
    FormControl,
    FormModel,
    _is_batch_clustering_form,
    _decode_json_value,
    _json_editable_value,
    submit_form_values,
    applicable_control_names,
)

from .measurement_widgets import IntensityMeasurements
from .classifier_channels import ClassifierChannels
from .clustering_features import ClusteringFeatures
from .supervised_features import SupervisedFeatures
from .kronos_marker_chooser import KronosMarkerAssignment
from .models import FormTableModel
from .cluster_decisions import ClusterDecisionModel
from .channel_table import ChannelAssignmentTable, ComponentAssignmentTable, ChannelCycleEditor, AssignmentTableTools, AssignmentTableSurface
from .acquisition_organization import AcquisitionOrganizationEditor
from .pixel_feature_editor import PixelFeatureEditor
from .researcher_widgets import ContentSizedButton
from ..terminology import choice_label, control_label, field_label
from ..method_guidance import method_guidance
from ..control_guidance import control_help
from .workbench import style_scientific_table, fit_scientific_columns, _fit_bounded_table_height
from .object_editor import ObjectEditor, ObjectEditorDraft
from .secondary_setup import SecondaryDefinitionEditor
from .primary_count_bounds import PrimaryClassCountBounds
from .error_copy import diagnostic_details, field_validation_summary, operation_error_summary

LocalPathChooser = Callable[[FormControl], str | None]
_MISSING = object()


# ==============================================================================
#                                     Forms
# ==============================================================================


def apply_widget_accessibility(root: QWidget, route_id: str) -> None:
    """Expose owner-assigned stable IDs and names to assistive technology."""

    for widget in root.findChildren(QWidget):
        if isinstance(widget, QLabel) and widget.objectName() == f"{route_id}-title":
            widget.setAccessibleName(f"{widget.text()} heading")
            widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        if not isinstance(
            widget,
            (QAbstractButton, QComboBox, QLineEdit, QPlainTextEdit, QTableView, QTableWidget),
        ):
            continue
        # Internal Qt table children have framework-owned IDs and behavior.
        if widget.objectName().startswith("qt_"):
            continue
        if not widget.objectName():
            continue
        if not widget.accessibleName():
            if isinstance(widget, QAbstractButton):
                widget.setAccessibleName(widget.text().replace("&", ""))
            else:
                widget.setAccessibleName(widget.objectName().replace("-", " "))
        if isinstance(widget, (QTableView, QTableWidget)) and not widget.accessibleDescription():
            widget.setAccessibleDescription("Use arrow keys to inspect rows and columns.")


def _display_json(value: object) -> str:
    return json.dumps(
        _json_editable_value(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def display_action_label(action: str) -> str:
    """Render an offered action for people without changing its submitted ID."""

    labels = {
        "review_supervised_classifier": "Accept cell assignments",
        "select_analysis_rois": "Select regions in QuPath",
        "edit_analysis_rois": "Edit regions in QuPath",
        "review_analysis_rois": "Review selected regions",
        "accept_analysis_rois": "Use these regions",
        "use_whole_tissue": "Use whole tissue",
        "accept_semantic_region": "Meets criterion",
        "reject_semantic_region": "Does not meet criterion",
    }
    if action in labels:
        return labels[action]
    special_tokens = {"roi": "ROI", "qupath": "QuPath", "mif": "mIF"}
    label = " ".join(special_tokens.get(token, token) for token in action.split("_"))
    return label[:1].upper() + label[1:]


class _StructuredValueModel(QAbstractTableModel):
    """Small editable model for declared mapping and ordered-sequence controls."""

    def __init__(
        self,
        control: FormControl,
        parent: QObject | None = None,
        *,
        plain_text: bool = False,
        locked: bool = False,
        display_labels: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(parent)
        self._control = control
        self._kind = control.kind
        self._plain_text = plain_text
        self._locked = locked
        self._display_labels = dict(display_labels or {})
        self._undo_rows = []
        self._redo_rows = []
        value = _json_editable_value(control.value)
        if self._kind in {ControlKind.INTEGER_MAPPING, ControlKind.OBJECT_MAPPING}:
            supplied = value if isinstance(value, dict) else {}
            keys = tuple(control.choices) or tuple(supplied)
            self._rows: list[list[object]] = [
                [key, supplied[key] if key in supplied else _MISSING] for key in keys
            ]
        else:
            items = value if isinstance(value, list) else []
            self._rows = [[item] for item in items]

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return 2 if self._kind in {ControlKind.INTEGER_MAPPING, ControlKind.OBJECT_MAPPING} else 1

    def data(
        self,
        index: QModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if not index.isValid():
            return None
        value = self._rows[index.row()][index.column()]
        if role == Qt.ItemDataRole.UserRole:
            return value
        if role == Qt.ItemDataRole.DisplayRole and isinstance(value, str) and self.columnCount() == 1:
            return self._display_labels.get(value, value)
        if role in {Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole}:
            if value is _MISSING:
                return ""
            if index.column() == 0 and self.columnCount() == 2:
                return str(value)
            if self._kind is ControlKind.INTEGER_MAPPING:
                return str(value)
            if self._plain_text:
                return str(value)
            return _display_json(value)
        return None

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            if self.columnCount() == 2:
                return ("Name", "Value")[section] if section < 2 else None
            return "Value" if section == 0 else None
        return section + 1

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        result = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if not self._locked and (self.columnCount() == 1 or index.column() == 1):
            result |= Qt.ItemFlag.ItemIsEditable
        return result

    def setData(
        self,
        index: QModelIndex,
        value: object,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        if role != Qt.ItemDataRole.EditRole or not self.flags(index) & Qt.ItemFlag.ItemIsEditable:
            return False
        try:
            decoded = self._decode_cell(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        self._remember()
        self._rows[index.row()][index.column()] = decoded
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.DisplayRole, role])
        return True

    def _decode_cell(self, value: object) -> object:
        if self._kind is ControlKind.INTEGER_MAPPING:
            if isinstance(value, bool):
                raise ValueError("integer mapping values must be integers")
            return int(value)
        if self._kind is ControlKind.OBJECT_SEQUENCE and self._control.choices:
            if value in self._control.choices:
                return value
            matches = [key for key, label in self._display_labels.items() if label == value]
            if len(matches) == 1:
                return matches[0]
            decoded = _decode_json_value(self._control.name, value)
            if decoded not in self._control.choices:
                raise ValueError("Choose one of the offered values.")
            return decoded
        return (
            str(value).strip()
            if self._plain_text
            else _decode_json_value(self._control.name, value)
        )

    def append_value(self, value: object) -> int:
        if self._control.choices and value in self.raw_value():
            raise ValueError(f"{self._display_labels.get(value, value)} is already selected.")
        self._remember()
        row = len(self._rows)
        self.beginInsertRows(QModelIndex(), row, row)
        self._rows.append([value])
        self.endInsertRows()
        return row

    def remove_row(self, row: int) -> bool:
        if not 0 <= row < len(self._rows):
            return False
        self._remember()
        self.beginRemoveRows(QModelIndex(), row, row)
        del self._rows[row]
        self.endRemoveRows()
        return True

    def remove_rows(self, rows):
        selected = set(rows)
        if not selected:
            return
        self._remember()
        self.beginResetModel()
        self._rows = [row for index, row in enumerate(self._rows) if index not in selected]
        self.endResetModel()

    def move_row(self, row: int, offset: int) -> int | None:
        target = row + offset
        if not 0 <= row < len(self._rows) or not 0 <= target < len(self._rows):
            return None
        self._remember()
        self.beginResetModel()
        self._rows[row], self._rows[target] = self._rows[target], self._rows[row]
        self.endResetModel()
        return target

    def restore_value(self, value: object) -> None:
        """Restore a same-form draft with its exact sequence or declared keys."""
        from dataclasses import replace

        temporary = _StructuredValueModel(
            replace(self._control, value=value), plain_text=self._plain_text, locked=self._locked
        )
        self.beginResetModel()
        self._rows = temporary._rows
        self._undo_rows.clear()
        self._redo_rows.clear()
        self.endResetModel()
        temporary.deleteLater()

    def _remember(self):
        self._undo_rows.append([row.copy() for row in self._rows])
        del self._undo_rows[:-20]
        self._redo_rows.clear()

    def undo(self):
        """Restore the previous table draft without changing submitted settings."""
        if self._locked or not self._undo_rows:
            return
        self._redo_rows.append(self._rows)
        self.beginResetModel()
        self._rows = self._undo_rows.pop()
        self.endResetModel()

    def redo(self):
        if self._locked or not self._redo_rows:
            return
        self._undo_rows.append(self._rows)
        self.beginResetModel()
        self._rows = self._redo_rows.pop()
        self.endResetModel()

    def paste_values(self, text: str) -> int:
        """Append a complete, validated clipboard batch as one reversible edit.

        A sequence accepts one entry per line or a JSON array. Closed choices
        accept their recorded value or an unambiguous displayed label. Numeric
        and structured entries use the form's finite JSON decoder. No row is
        changed when any entry is invalid.
        """
        if self._locked or self._kind is not ControlKind.OBJECT_SEQUENCE:
            raise ValueError("This table does not accept additional rows.")
        text = text.strip()
        if not text:
            raise ValueError("Copy one value per line, then paste the rows here.")
        if not self._plain_text and text.startswith("["):
            values = _decode_json_value(self._control.name, text)
            if not isinstance(values, (tuple, list)):
                raise ValueError("Paste a JSON array or one value per line.")
        else:
            values = []
            for line in text.splitlines():
                if "\t" in line:
                    raise ValueError("Paste one column of values at a time.")
                value = line.strip()
                if not value:
                    raise ValueError("A pasted row is blank. Enter a value in every row.")
                if self._plain_text or self._control.choices:
                    values.append(value)
                else:
                    values.append(_decode_json_value(self._control.name, value))
        decoded = []
        for value in values:
            if self._control.choices:
                if value not in self._control.choices:
                    matches = [key for key, label in self._display_labels.items() if label == value]
                    if len(matches) == 1:
                        value = matches[0]
                    else:
                        try:
                            value = _decode_json_value(self._control.name, value)
                        except ValueError:
                            pass
                if value not in self._control.choices:
                    raise ValueError(f"Unknown choice: {value}. Use one of the offered values.")
                if value in self.raw_value() or value in decoded:
                    raise ValueError(f"{self._display_labels.get(value, value)} is already selected.")
            decoded.append(value)
        if not decoded:
            raise ValueError("The clipboard does not contain any rows.")
        self._remember()
        first = len(self._rows)
        self.beginInsertRows(QModelIndex(), first, first + len(decoded) - 1)
        self._rows.extend([value] for value in decoded)
        self.endInsertRows()
        return first

    def raw_value(self) -> object:
        if self._kind is ControlKind.INTEGER_MAPPING:
            return {str(key): value for key, value in self._rows if value is not _MISSING}
        if self._kind is ControlKind.OBJECT_MAPPING:
            return {
                str(key): value
                for key, value in self._rows
                if value is not _MISSING
            }
        return [row[0] for row in self._rows]


class FormRenderer(QWidget):
    """Render one form and delegate decoding and validation to shared owners."""

    submission_ready = Signal(str, object)
    cancelled = Signal()
    reference_back_requested = Signal(str)
    acquisition_cycle_review_requested = Signal()
    page_changed = Signal()
    reference_image_selected = Signal(str)
    feature_inspection_requested = Signal(int)
    tissue_authoring_requested = Signal(str)
    tissue_source_selected = Signal(str)
    tissue_source_invalidated = Signal()
    secondary_marker_requested = Signal(str, object, object)
    secondary_definition_back_requested = Signal()
    kronos_cell_preview_requested = Signal(object, object)

    def __init__(
        self,
        *,
        path_chooser: LocalPathChooser | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._path_chooser = path_chooser or self._choose_local_path
        self.native_feature_inspection_available = True
        self.secondary_back_caption = "Back"
        self._form: FormModel | None = None
        self.navigation_widget = None
        self._stage_pages = None
        self._pixel_feature_summaries = {}
        self._stage1_rows = {}
        self._stage1_actions = None
        self._measurement_channel_table = None
        self._editors: dict[str, QWidget] = {}
        self._errors: dict[str, QLabel] = {}
        self._validation_help = {}
        self._models: list[QAbstractTableModel] = []
        self._tissue_rows: dict[str, tuple[QLabel, QWidget]] = {}
        self._tissue_buttons: dict[str, QPushButton] = {}
        self._tissue_step = 0
        self._tissue_source_ready = False
        self._tissue_observation_pending = False
        self._tissue_return_to_regions = False
        self._primary_rows: dict[str, tuple[QLabel, QWidget]] = {}
        self._primary_buttons: dict[str, QPushButton] = {}
        self._training_plan_button: QPushButton | None = None
        self._primary_step = 0
        self._secondary_rows: dict[str, tuple[QLabel, QWidget]] = {}
        self._fit_review_rows: dict[str, tuple[QLabel, QWidget]] = {}
        self._supervised_rows: dict[str, tuple[QLabel, QWidget]] = {}
        self._supervised_buttons: dict[str, QPushButton] = {}
        self._supervised_step = 0
        self._marker_rows: dict[str, tuple[QLabel, QWidget]] = {}
        self._marker_review_rows: dict[str, tuple[QLabel, QWidget]] = {}
        self._marker_buttons: dict[str, QPushButton] = {}
        self._marker_step = 0
        self._marker_revisit_requested = None
        self._marker_combinations = None
        self._segmentation_rows: dict[str, tuple[QLabel, QWidget]] = {}
        self._segmentation_buttons = {}
        self._segmentation_previous_method = None
        self._segmentation_channel_selections = {}
        self._clustering_rows: dict[str, tuple[QLabel, QWidget]] = {}
        self._clustering_buttons: dict[str, QPushButton] = {}
        self._clustering_step = 0
        self._clustering_setup = None
        self._cluster_review = None
        self._measurement_rows: dict[str, tuple[QLabel, QWidget]] = {}
        self._measurement_buttons: dict[str, QPushButton] = {}
        self._measurement_step = 0
        self._unmixing_rows = {}
        self._alignment_rows = {}
        self._unmixing_buttons = {}
        self._unmixing_step = 0
        self._layout = QVBoxLayout(self)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._layout.setContentsMargins(16, 14, 16, 18)
        self._layout.setSpacing(16)
        self._layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)

    def set_form(self, form: FormModel) -> None:
        """Replace the displayed stage form with controls for its current request.

        The renderer creates typed editors, guidance and tables from the supplied form
        model, connects change events and sets the applicable navigation state. The training
        procedure determines scalar/grid controls directly. Optional or hidden stage values
        are not presented as additional required researcher inputs."""

        if not isinstance(form, FormModel):
            raise TypeError("Qt form renderer requires a FormModel")
        if getattr(self, '_marker_preparation_visit', None) is not None:
            self._marker_preparation_visit.close()
        self._clear()
        self._form = form
        self._validation_help = {}
        self._library_declaration_rows = {}
        self._spectral_table = None
        self._spectral_table_layout = None
        self._spectral_table_error = None
        self._library_manual_declarations = None
        self._spectral_step = 0
        self._spectral_opened = False
        self._spectral_choices = None
        self._spectral_source_row = None
        self._spectral_editor_panel = None
        self._spectral_layout_narrow = None
        self._spectral_source_name = None
        self._spectral_declarations = None
        self._spectral_declaration_grid = None
        self._spectral_recorded_declarations = {}
        self._spectral_source_actions = []
        self._spectral_buttons = []
        self._spectral_back = None
        self._spectral_return = None
        self._spectral_outer_back = None
        self._kronos_vocabulary = {}
        self._kronos_sampling_updates = []
        self._kronos_previews = {}
        if any("_kronos_" in control.name for control in form.controls):
            from mif_pipeline.orchestration.setup.kronos_setup import marker_authoring_snapshot
            # Authoring suggestions contain no model weights or execution authority.
            # Missing local files must not block the alternative measured-feature method.
            try:
                self._kronos_vocabulary = marker_authoring_snapshot(setup=form.context.get("kronos_setup"))
            except (OSError, ValueError, KeyError):
                self._kronos_vocabulary = {}
        self.navigation_widget = None
        self._stage_pages = None
        self._pixel_feature_summaries = {}
        self.setObjectName(f"form-{form.form_id}")
        self._stage1_rows = {}
        self._stage1_actions = None
        self._stage1_source_table = None
        self._stage1_source_panel = None
        self._reference_band_panel = None
        self._af_control_review_panel = None
        self._target_af_groups_panel = None
        self._reference_navigation_layout = None
        self._af_continue_button = None
        self._af_control_reviewed = False
        self._af_control_band_step = False
        self._reference_extraction_panel = None
        self._reference_regions_panel = None

        heading = QLabel(form.title, self)
        heading.setObjectName(f"form-{form.form_id}-title")
        heading.setProperty("formHeading", True)
        font = QFont(heading.font())
        font.setBold(True)
        heading.setFont(font)
        self._heading = heading
        self._layout.addWidget(heading)
        message = QLabel(form.message, self)
        self._message = message
        message.setWordWrap(True)
        message.setProperty("stageDescription", True)
        message.setVisible(bool(form.message))
        if form.context.get("adapter") == "batch_reference_start":
            message.setObjectName("unmixing-recorded-image-status")
            message.setProperty("statusBanner", True)
            message.setContentsMargins(16, 10, 14, 10)
            message.setMinimumHeight(message.fontMetrics().height() + 20)
        self._layout.addWidget(message)
        if form.context.get("reference_region_workspace") is not None:
            from .reference_regions import ReferenceRegionsWorkspace
            if form.context['reference_region_workspace'].get('assignment'):
                from .reference_control_assignment import ReferenceControlAssignmentWorkspace as ReferenceRegionsWorkspace
            elif form.context['reference_region_workspace'].get('source_review'):
                from .reference_control_assignment import ReferenceControlSourceWorkspace as ReferenceRegionsWorkspace
            elif form.context['reference_region_workspace'].get('unified'):
                from .unified_reference_regions import UnifiedReferenceRegionsWorkspace as ReferenceRegionsWorkspace
            elif form.context['reference_region_workspace'].get('control_sets_enabled'):
                from .af_control_regions import AfControlRegionsWorkspace as ReferenceRegionsWorkspace

            self._reference_regions_panel = ReferenceRegionsWorkspace(
                form.context["reference_region_workspace"], {c.name: c.value for c in form.controls}, self)
            self._layout.addWidget(self._reference_regions_panel)
        if form.context.get("target_af_groups") is not None:
            from .target_af_groups import TargetAfGroupsPanel
            self._target_af_groups_panel = TargetAfGroupsPanel(form.context["target_af_groups"], self)
            self._layout.addWidget(self._target_af_groups_panel)
        if form.context.get("spectral_review") is not None:
            from .spectral_review import SpectralReviewPanel

            self._layout.addWidget(SpectralReviewPanel(form.context["spectral_review"], self))
        if form.context.get("target_af_review") is not None:
            from .target_af_review import TargetAfReviewPanel
            review = form.context['target_af_review']
            if review.get('components'):
                from .af_control_review import AfControlSpectrumPanel
                af_panel = AfControlSpectrumPanel(review, self, target=True)
            else:
                af_panel = TargetAfReviewPanel(review, self)
            if form.context.get("spectral_review") is not None:
                disclosure = QToolButton(self)
                disclosure.setObjectName("target-af-evidence-disclosure")
                disclosure.setText("Autofluorescence regions and background details")
                disclosure.setCheckable(True)
                from .researcher_widgets import configure_disclosure
                configure_disclosure(disclosure)
                disclosure.toggled.connect(af_panel.setVisible)
                self._layout.addWidget(disclosure)
                af_panel.hide()
            self._layout.addWidget(af_panel)
        if form.context.get("extraction_review") is not None:
            from .extraction_review import ExtractionReviewPanel

            self._reference_extraction_panel = ExtractionReviewPanel(form.context["extraction_review"], self)
            self._reference_extraction_panel.set_values({c.name: c.value for c in form.controls
                if c.name in {"included_roi_ids", "excluded_roi_ids"}})
            self._layout.addWidget(self._reference_extraction_panel)
        if form.context.get("band_review") is not None and form.context.get("reference_workspace"):
            from .reference_bands import ReferenceBandsPanel
            if form.context.get('af_control_set_review'):
                from .af_control_review import AfControlSpectrumPanel
                self._af_control_review_panel = AfControlSpectrumPanel(form.context['af_control_set_review'], self)
                self._layout.addWidget(self._af_control_review_panel)
            self._reference_band_panel = ReferenceBandsPanel(form.context["band_review"], self)
            self._reference_band_panel.set_values({c.name: c.value for c in form.controls
                if c.name in {"signal_measurement_id", "dark_measurement_ids"}})
            self._layout.addWidget(self._reference_band_panel)
        self._form_error_summary = QLabel("", self)
        self._form_error_summary.setObjectName(f"form-{form.form_id}-error-summary")
        self._form_error_summary.setWordWrap(True)
        self._form_error_summary.setProperty("validationMessage", True)
        self._form_error_summary.setTextFormat(Qt.TextFormat.PlainText)
        self._form_error_summary.setTextInteractionFlags(
            Qt.TextInteractionFlag.LinksAccessibleByMouse | Qt.TextInteractionFlag.LinksAccessibleByKeyboard)
        self._form_error_summary.linkActivated.connect(self._focus_validation_field)
        self._form_error_summary.hide()
        self._layout.addWidget(self._form_error_summary)
        self._form_error_details_toggle = QToolButton(self)
        self._form_error_details_toggle.setObjectName(f"form-{form.form_id}-error-details-toggle")
        self._form_error_details_toggle.setText("Details")
        self._form_error_details_toggle.setCheckable(True)
        self._form_error_details_toggle.hide()
        self._layout.addWidget(self._form_error_details_toggle)
        self._form_error_details = QLabel("", self)
        self._form_error_details.setObjectName(f"form-{form.form_id}-error-details")
        self._form_error_details.setWordWrap(True)
        self._form_error_details.setTextFormat(Qt.TextFormat.PlainText)
        self._form_error_details.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._form_error_details.hide()
        self._layout.addWidget(self._form_error_details)
        self._form_error_details_toggle.toggled.connect(self._form_error_details.setVisible)

        adapter = form.context.get("adapter")
        step_title = {
            "measurement_setup": "Choose channels",
            "segmentation_setup": "Segmentation settings",
            "segmentation_options": "Segmentation settings",
            "tissue_setup": "Choose detector",
            "marker_setup": "Choose fitting scope",
        }.get(adapter)
        if step_title is not None:
            self._setup_heading = QLabel(step_title, self)
            self._setup_heading.setObjectName(f"{adapter.replace('_', '-')}-step-heading")
            self._setup_heading.setProperty("panelHeading", True)
            heading_font = QFont(self._setup_heading.font())
            heading_font.setBold(True)
            self._setup_heading.setFont(heading_font)
            self._layout.addWidget(self._setup_heading)
        elif _is_batch_clustering_form(form):
            self._setup_heading = QLabel("Choose clustering scope", self)
            self._setup_heading.setObjectName("clustering-setup-step-heading")
            self._setup_heading.setProperty("panelHeading", True)
            heading_font = QFont(self._setup_heading.font())
            heading_font.setBold(True)
            self._setup_heading.setFont(heading_font)
            self._layout.addWidget(self._setup_heading)

        channel_table = (
            ChannelAssignmentTable(form, self)
            if adapter == "batch_stage1"
            and any(t.table_id == "stage1_channel_rows" for t in form.tables)
            else None
        )
        if channel_table is not None:
            channel_panel = QWidget(self)
            channel_panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
            channel_layout = QVBoxLayout(channel_panel)
            channel_layout.setContentsMargins(0, 0, 0, 0)
            channel_layout.setSpacing(8)
            channel_tools = AssignmentTableTools(channel_table, channel_panel)
            edit_hint = QLabel("Click or press F2 to edit", channel_tools)
            edit_hint.setProperty("choiceHelp", True)
            channel_tools.actions.insertWidget(0, edit_hint)
            channel_layout.addWidget(AssignmentTableSurface(channel_table, channel_tools.discovery, channel_panel))
            channel_layout.addWidget(channel_tools)
            self._layout.addWidget(channel_panel)
        if adapter == "batch_reference_start":
            from .image_state import MeasurementBandMapping

            self._unmixing_gate = QFrame(self)
            self._unmixing_gate.setObjectName("unmixing-state-gate")
            self._unmixing_gate.setProperty("workbenchSection", True)
            gate_layout = QVBoxLayout(self._unmixing_gate)
            gate_layout.setContentsMargins(18, 16, 18, 18)
            gate_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
            gate_layout.setSpacing(14)
            self._layout.removeWidget(message)
            message.setContentsMargins(0, 0, 0, 0)
            message.setMinimumHeight(0)
            gate_layout.addWidget(message)
            self._layout.addWidget(self._unmixing_gate)
            self._measurement_band_panel = MeasurementBandMapping(
                form.context.get("measurement_band_mapping", {}), self)
            self._measurement_band_cycles = self._measurement_band_panel.cyclic
            self._layout.addWidget(self._measurement_band_panel)
            self._unmixing_back = ContentSizedButton("Back: image state", self)
            self._unmixing_back.clicked.connect(lambda: self._set_unmixing_step(0))
            self._layout.addWidget(self._unmixing_back, alignment=Qt.AlignmentFlag.AlignLeft)
        self._reference_settings_panel = None
        reference_rows = []
        reference_groups = form.context.get("reference_settings_groups", {})
        if adapter == "tissue_setup":
            self._tissue_hint = QLabel("", self)
            self._tissue_hint.setObjectName("tissue-setup-page-help")
            self._tissue_hint.setWordWrap(True)
            self._tissue_hint.setProperty("choiceHelp", True)
            self._layout.addWidget(self._tissue_hint)
        if adapter == "marker_setup":
            self._marker_tabs = QTabBar(self)
            self._marker_tabs.setObjectName("stage-page-tabs")
            self._marker_tabs.setExpanding(False)
            for title in ("01  Markers", "02  Fitting scope", "03  Marker method"):
                self._marker_tabs.addTab(title)
            self._marker_tabs.tabBarClicked.connect(lambda index: self._marker_go_to_step(index - 1))
            self._layout.addWidget(self._marker_tabs)
            self._marker_progress = QLabel("Fitting scope  →  Marker method", self)
            self._marker_progress.setObjectName("marker-setup-progress")
            self._marker_progress.setProperty("choiceHelp", True)
            self._layout.addWidget(self._marker_progress)
            self._marker_selector = QComboBox(self)
            self._marker_selector.setObjectName("marker-setup-marker")
            self._marker_selector.setAccessibleName("Marker to configure")
            for marker in form.context["stage_result"]["resume_context"]["canonical_marker_ids"]:
                self._marker_selector.addItem(marker, marker)
            self._marker_selector.currentIndexChanged.connect(self._apply_marker_visibility)
            self._marker_cards = {}
            self._marker_grids = {}
            for step, title in ((-1, "Select markers and measurements"), (0, "Choose the threshold scope"), (1, "")):
                card = QFrame(self)
                card.setObjectName("marker-setup-card")
                card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
                card.setStyleSheet("QFrame#marker-setup-card { background: white; border: 1px solid #dbe4e5; border-radius: 12px; }")
                card_layout = QVBoxLayout(card)
                card_layout.setContentsMargins(22, 20, 22, 20)
                card_layout.setSpacing(14)
                if title:
                    heading = QLabel(title, card)
                    heading.setProperty("stagePageHeading" if step == 0 else "panelHeading", True)
                    heading.setWordWrap(True)
                    card_layout.addWidget(heading)
                    description = QLabel(
                        "Choose the intensity measurement that represents each marker’s staining in a cell."
                        if step == -1 else
                        "Pooling requires comparable marker intensities across slides in the processing group.",
                        card,
                    )
                    description.setProperty("fieldHelp", True)
                    description.setWordWrap(True)
                    card_layout.addWidget(description)
                    if step == 0:
                        card.setStyleSheet("QFrame#marker-setup-card { background: transparent; border: none; }")
                if step == 1:
                    card_layout.addWidget(self._marker_selector)
                grid = QGridLayout()
                grid.setHorizontalSpacing(24)
                grid.setVerticalSpacing(6)
                grid.setColumnStretch(0, 1)
                grid.setColumnStretch(1, 1)
                card_layout.addLayout(grid)
                self._marker_cards[step] = card
                self._marker_grids[step] = grid
                self._layout.addWidget(card)
        tissue_training = bool(form.context.get("primary") or form.context.get("secondary"))
        single_tissue_fit_review = (
            adapter == "training_fit_review"
            and form.context["stage_result"]["stage"] in {
                "primary_classifier_training_review", "secondary_classifier_training_review"
            }
            and len(form.context["stage_result"]["resume_context"]["classifiers"]) == 1
        )
        paged_training = tissue_training or form.context.get("supervised")
        paged_measurements = adapter == "measurement_setup"
        paged_segmentation = adapter in {"segmentation_setup", "segmentation_options"}
        measurement_channels = None
        self._measurement_intensity_grid = None
        if paged_measurements:
            from .channel_table import MeasurementChannelTable
            from .measurement_channel_grid import MeasurementChannelGrid

            measurement_channels = MeasurementChannelTable(form, self)
            self._measurement_intensity_grid = MeasurementChannelGrid(form, self)
        self._measurement_channel_table = measurement_channels
        self._subject_allocations = {}
        self._tissue_classifier_choices = {}
        self._reference_plan = None
        self._supervised_reference_panel = None
        if paged_training and form.context.get("development_cv_policy"):
            from .subject_allocation import SubjectAllocation

            self._subject_allocations = {
                index: SubjectAllocation(form, index, self)
                for index, _ in enumerate(sorted(form.context["resume_context"]["candidate_entry_ids"]))
            }
        if paged_training or paged_measurements or paged_segmentation:
            from .stage_form_pages import StageFormPages, SettingsPage, training_page, advanced_training_control

            self._stage_pages = StageFormPages(self._before_training_page_change, self, kronos_vocabulary=self._kronos_vocabulary)
            if form.context.get("secondary"):
                records = form.context["resume_context"]["secondary_output_classes_by_candidate_id"]
                self._stage_pages.set_candidates([
                    tuple(item["display_name"] for item in records[key]["structures"])
                    for key in sorted(form.context["resume_context"]["candidate_entry_ids"])
                ], back_caption=self.secondary_back_caption)
                self._stage_pages.candidate_changed.connect(self._secondary_candidate_changed)
                self._stage_pages.definition_back_requested.connect(self.secondary_definition_back_requested)
            self.navigation_widget = self._stage_pages.navigation
            self._stage_pages.page_changed.connect(self._training_page_changed)
            self._layout.addWidget(self._stage_pages)
        controls = QFormLayout() if adapter != "alignment_setup" and not (paged_training or paged_measurements or paged_segmentation or _is_batch_clustering_form(form)) else None
        if controls is not None:
            if adapter == "batch_stage1" and channel_table is not None:
                controls.setContentsMargins(0, 0, 0, 0)
            controls.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            controls.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
            controls.setVerticalSpacing(8)
            controls.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
            if form.context.get("spectral_input"):
                controls.setAlignment(Qt.AlignmentFlag.AlignTop)
        advanced_fields = set()
        if paged_segmentation:
            advanced_fields = {"tile_size_px", "padding_px", "workers"}
        elif adapter == "batch_reference_start":
            advanced_fields = {control.name for control in form.controls if control.name not in {"spectral_state_override", "solver"} and not control.name.startswith(("calibration_", "measurement_band_component_"))}
        elif adapter == "alignment_setup":
            main_fields = {"alignment_method", "alignment_fixed_channel", "alignment_moving_channel", "alignment_local_refinement"}
            advanced_fields = {control.name for control in form.controls if control.name not in main_fields}
        elif adapter == "training_fit_review":
            advanced_fields = {control.name for control in form.controls if control.name.endswith((
                "_rtrees_min_sample_count", "_rtrees_active_var_count", "_rtrees_termination_epsilon",
                "_rtrees_calculate_variable_importance", "_svm_max_iterations", "_svm_termination_epsilon"))}
        advanced_controls = QFormLayout() if advanced_fields and not paged_segmentation else None
        if advanced_controls is not None:
            advanced_controls.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
            advanced_controls.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        ordered_controls = form.controls
        if paged_training:
            # Keep the study procedure ahead of its optional tuning fields,
            # even when the native model's controls precede it in the schema.
            ordered_controls = sorted(form.controls, key=lambda control: (
                -1 if control.name.endswith("_model_method") else
                0 if control.name.endswith(("_procedure", "_intent")) else
                1 if control.name.endswith("_cv_folds") else
                2 if control.name.endswith("_cv_graph_widths") else 3
            ))
        if paged_segmentation:
            ordered_controls = sorted(form.controls, key=lambda control: 0 if control.name == "method" else 1 if control.name == "scope" else 2)
        for control in ordered_controls:
            if control.name == 'target_af_groups' and self._target_af_groups_panel is not None:
                continue
            if control.name == 'af_control_set_review_sha256s' and self._af_control_review_panel is not None:
                continue
            if self._reference_regions_panel is not None and self._reference_regions_panel.owns_control(control.name):
                panel = self._reference_regions_panel
                if control.name in panel.validation_editors:
                    self._editors[control.name] = panel.validation_editors[control.name]
                    self._errors[control.name] = panel.field_errors[control.name]
                continue
            if (self._reference_band_panel is not None and control.name in {
                    "signal_measurement_id", "dark_measurement_ids"}):
                continue
            if (self._reference_extraction_panel is not None and control.name in {
                    "included_roi_ids", "excluded_roi_ids"}):
                continue
            if control.name == "spectral_csv" and form.context.get("spectral_input"):
                continue
            if control.name == "excluded_roi_ids":
                continue
            editor, field = self._build_editor(control)
            if adapter == "batch_stage1":
                field.layout().setContentsMargins(0, 0, 0, 0)
            error = QLabel("", self)
            error.setObjectName(f"error-{control.name}")
            error.setWordWrap(True)
            error.setProperty("validationMessage", True)
            error.setTextFormat(Qt.TextFormat.PlainText)
            error.hide()
            field.layout().addWidget(error)
            in_channel_table = channel_table is not None and channel_table.owns_control(
                control.name
            )
            help_text = (
                ""
                if ((tissue_training or adapter == "tissue_setup") and self.native_feature_inspection_available and control.name.endswith("_image_features"))
                or in_channel_table or control.name in {"solver", "consensus_roi_review", "spectral_state_override"}
                or (adapter == "batch_stage1" and control.name in {"nuclear_stain", "component_values"})
                or (tissue_training and ("_patient_" in control.name or control.name.endswith("_test_subjects")))
                or (control.name == "library_locator" and form.context.get("spectral_input"))
                or (paged_training and form.context.get("development_cv_policy")
                    and control.name.endswith("_procedure"))
                or paged_measurements
                or (paged_segmentation and control.name == "method")
                or (paged_training and control.name == "classifier_source")
                or (single_tissue_fit_review and control.name.endswith("_selected"))
                or (_is_batch_clustering_form(form) and control.name in {"clustering_mode", "method", "feature_ids"})
                or (form.context.get("supervised") and control.name == "supervised_0_feature_ids")
                or re.fullmatch(r"supervised_\d+_kronos_channel_ids", control.name)
                or (paged_training and control.name.endswith('_kronos_patch_size'))
                or field.findChild(QWidget, "choice-help-" + control.name) is not None
                else (control_help(control.name, control.help_text) or "")
            )
            if paged_training and control.name.endswith("_cv_folds"):
                help_text = ""
            if paged_training and control.name.endswith("_rtrees_active_var_count"):
                help_text = "Each split compares a random subset of features. Its default size is the square root of the number of classifier inputs, rounded to the nearest integer."
            if tissue_training and control.name.endswith("_resolution"):
                from mif_pipeline.gui.kronos_setup import tissue_original_resolution_caption
                context = form.context["resume_context"]
                index = int(control.name.split("_")[1])
                candidate = sorted(context["candidate_entry_ids"])[index]
                original = tissue_original_resolution_caption(context, candidate)
                help_text = original[0] if original else ""
            if paged_training and control.name.endswith(("_pca_variance", "_pca_normalize", "_normalization")) and "_kronos_" not in control.name:
                help_text = ""
            if help_text:
                help_label = QLabel(help_text, self)
                help_label.setObjectName(f"form-help-{control.name}")
                help_label.setWordWrap(True)
                help_label.setProperty("fieldHelp", True)
                field.layout().addWidget(help_label)
                if control.name == "measurement_convention" and form.context.get("spectral_input"):
                    from ..reference_copy import REFERENCE_CORRECTION_HELP
                    def update_reference_help(_index=0, combo=editor, text=help_label):
                        text.setText(REFERENCE_CORRECTION_HELP.get(combo.currentData(),
                            "Select the correction applied before the reference spectra were normalized."))
                        combo.setAccessibleDescription(text.text())
                    editor.currentIndexChanged.connect(update_reference_help)
                    if len(control.choices) == 1:
                        editor.setCurrentIndex(editor.findData(control.choices[0]))
                        editor.hide()
                    update_reference_help()
            label = QLabel(control_label(control.name, control.label), self)
            label.setObjectName(f"form-label-{control.name}")
            label_font = QFont(label.font())
            label_font.setWeight(QFont.Weight.Medium)
            label.setFont(label_font)
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            label.setBuddy(editor)
            editor.setAccessibleName(control_label(control.name, control.label))
            if control.name == "phenotyping_population":
                label.hide()
                label.setMaximumHeight(0)
            if help_text:
                editor.setAccessibleDescription(help_text)
            if (help_text and isinstance(editor, QComboBox)
                    and re.fullmatch(r"(?:supervised|training)_\d+_normalization", control.name)):
                from ..control_guidance import CLASSIFIER_SCALING_HELP

                def update_scaling_help(_index=0, combo=editor, text=help_label):
                    selected_help = CLASSIFIER_SCALING_HELP.get(combo.currentData(), "")
                    text.setText(selected_help)
                    text.setVisible(bool(selected_help))
                    combo.setAccessibleDescription(selected_help)

                editor.currentIndexChanged.connect(update_scaling_help)
                update_scaling_help()
            if (single_tissue_fit_review and control.name.endswith("_selected")
                    and control.kind is ControlKind.CHECKBOX):
                editor.setText("Adjust fit parameters")
                label.setText("")
                label.setMaximumHeight(0)
            target_controls = (
                advanced_controls
                if advanced_controls is not None
                and control.name in advanced_fields
                else controls
            )
            if _is_batch_clustering_form(form):
                # The clustering workbench mounts these unchanged form editors
                # after their complete catalog and weight rows are available.
                pass
            elif control.name == "option_value" and self._reference_regions_panel is not None:
                label.hide()
                field.hide()
                self._reference_regions_panel.set_option_editor(editor)
            elif control.name == "option_value" and (self._target_af_groups_panel is not None
                    or form.context.get("target_af_combined_review")
                    or form.context.get("reference_workspace")
                    or (form.context.get("spectral_review") is not None
                        and form.context.get("selected_option") in {
                            "accept_control_target_af", "accept_final_run_basis"})):
                label.hide()
                field.hide()
            elif control.name == "confidence_threshold" and form.context.get("resume_context", {}).get("phase") == "prediction_review":
                label.hide()
                field.hide()
            elif adapter == "batch_reference_start" and control.name == "spectral_state_override":
                from .image_state import ImageStateChoices

                label.setWordWrap(True)
                field.layout().setContentsMargins(0, 0, 0, 0)
                field.layout().removeWidget(editor)
                editor.hide()
                field.layout().insertWidget(0, ImageStateChoices(editor, field))
                self._unmixing_gate.layout().addWidget(label)
                self._unmixing_gate.layout().addWidget(field)
            elif adapter == "batch_reference_start" and control.name.startswith("measurement_band_component_"):
                index = int(control.name.removeprefix("measurement_band_component_"))
                label.setParent(self._measurement_band_panel)
                label.hide()
                self._measurement_band_panel.add_control(index, field, editor)
            elif adapter == "batch_stage1" and control.name == "channel_content":
                content_row = QWidget(self)
                content_row.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
                content_layout = QHBoxLayout(content_row)
                content_layout.setContentsMargins(0, 0, 0, 0)
                content_layout.setSpacing(12)
                content_layout.addWidget(label)
                content_layout.addWidget(field, 1)
                position = self._layout.indexOf(channel_panel) if channel_table is not None else self._layout.count()
                self._layout.insertWidget(position, content_row)
            elif adapter == "batch_acquisition_organization" and control.name == "acquisition_organization":
                label.hide()
                target_controls.addRow(field)
            elif adapter == "batch_stage1" and control.name == "channel_cycle_values":
                label.hide()
                if channel_table is not None:
                    self._layout.insertWidget(self._layout.indexOf(channel_panel), field)
                else:
                    target_controls.addRow(field)
            elif (
                control.name == "option_value"
                and field.findChild(QWidget, "spectral-source-choices") is not None
            ):
                label.hide()
                if (form.context.get("selected_option") is not None
                        and form.context.get("stage_result", {}).get("resume_context", {})
                        .get("service_context", {}).get("operation_state", {})
                        .get("checkpoint") == "basis_source"):
                    field.hide()
                else:
                    target_controls.addRow(field)
            elif control.name == "library_locator" and form.context.get("spectral_input"):
                label.hide()
                target_controls.addRow(field)
            elif self._spectral_declaration_grid is not None and control.name in {
                    "normalization", "measurement_convention"}:
                label.setText("Normalization of supplied spectra" if control.name == "normalization"
                              else "Corrections already applied")
                column = 1 if control.name == "measurement_convention" else 0
                row, span = 0, 1
                self._spectral_declaration_grid.addWidget(label, row, column, 1, span)
                self._spectral_declaration_grid.addWidget(field, row + 1, column, 1, span)
                recorded = QLabel(field)
                recorded.setObjectName("spectral-recorded-" + control.name)
                recorded.setTextFormat(Qt.TextFormat.PlainText)
                recorded.setWordWrap(True)
                recorded.hide()
                field.layout().insertWidget(0, recorded)
                field.layout().setAlignment(Qt.AlignmentFlag.AlignTop)
                self._spectral_recorded_declarations[control.name] = recorded
            elif control.name == "consensus_roi_review":
                label.hide()
                target_controls.addRow(field)
            elif control.name == "cluster_decisions":
                label.hide()
                help_label = field.findChild(QLabel, "form-help-cluster_decisions")
                if help_label is not None:
                    field.layout().removeWidget(help_label)
                    self._cluster_review.profile_layout.addWidget(help_label)
                target_controls.addRow(field)
            elif control.name in reference_groups:
                reference_rows.append((control.name, label, field))
            elif in_channel_table:
                label.hide()
                label.deleteLater()
                channel_table.add_control(control.name, field, editor)
            elif adapter == "batch_stage1" and control.name == "nuclear_stain":
                label.hide()
                target_controls.addRow(field)
            elif adapter == "batch_stage1" and control.kind is ControlKind.CHECKBOX:
                editor.setText(control.label)
                # Stage-one visibility refresh retains this hidden label.
                label.hide()
                target_controls.addRow(field)
            elif adapter == "marker_combinations":
                label.hide()
                field.hide()
            elif adapter == "marker_setup":
                if control.name == "selected_marker_ids":
                    step, row, column, span = -1, 0, 0, 2
                    label.hide()
                elif control.name == "marker_fit_scope":
                    step, row, column, span = 0, 0, 0, 2
                elif control.name == "marker_composite_names":
                    step, row, column, span = 2, 0, 0, 2
                else:
                    suffix = next((key for key in ("nimbus_magnification", "nimbus_checkpoint_path", "nimbus_score_path", "nimbus_cutoff", "nimbus_source", "lower_min", "lower_max", "upper_min", "upper_max", "feature", "method") if control.name.endswith("_" + key)), "method")
                    row, column, span = {"feature": (0, 0, 1), "method": (2, 0, 1),
                        "lower_min": (6, 0, 1), "lower_max": (6, 1, 1),
                        "upper_min": (8, 0, 1), "upper_max": (8, 1, 1), "nimbus_source": (4, 0, 1), "nimbus_cutoff": (4, 1, 1),
                        "nimbus_magnification": (10, 0, 2), "nimbus_checkpoint_path": (6, 0, 2), "nimbus_score_path": (6, 0, 2)}[suffix]
                    step = 1
                    if suffix == "feature":
                        label.setText("Cell measurement")
                self._marker_grids[step].addWidget(label, row, column, 1, span)
                self._marker_grids[step].addWidget(field, row + 1, column, 1, span)
            elif paged_measurements and control.name.startswith("shape-"):
                definition = SettingsPage("shape", "Cell and nuclear shape", "Select the shape measurements to extract for each cell. These are available for review, export and supervised phenotyping.", 2, 1)
                editor.setText(control.label)
                label.setText("")
                label.setMaximumHeight(0)
                field.layout().setContentsMargins(0, 0, 0, 0)
                field.layout().setSpacing(0)
                self._stage_pages.add_control(definition, control.name, label, field, compact=True)
            elif paged_measurements:
                channel_index = int(control.name.split("_")[1])
                channel = form.tables[0].rows[channel_index]
                if control.name.endswith(("_selected", "_role")):
                    definition = SettingsPage("channels", "Channels to measure", "Select the channels to measure and check their roles. Choose the cellular regions and intensity measurements on the next page.", 0, 0)
                    if control.name.endswith("_role"):
                        label.setText("Measurement role")
                    else:
                        label.setText(control.label.removeprefix("Measure "))
                        editor.setText("Include channel")
                else:
                    definition = SettingsPage("intensities", "Channel intensity measurements", "Choose one cellular region and the intensity measurements for each channel.", 1, 1)
                    label.setText("Cell region" if control.name.endswith("_compartment") else "Intensity measurements")
                self._stage_pages.add_control(
                    definition, control.name, label, field, compact=True,
                    table=measurement_channels if definition.key == "channels" else self._measurement_intensity_grid,
                )
            elif paged_training:
                if control.name.endswith(("_cv_folds", "_rtrees_active_var_count")):
                    from .automatic_training_value import AutomaticTrainingValue
                    automatic = AutomaticTrainingValue(editor, folds=control.name.endswith("_cv_folds"),
                                                       grid="_grid_" in control.name)
                    automatic.setObjectName("automatic-" + control.name)
                    row = editor.parentWidget().layout()
                    row.addWidget(automatic)
                    allocation = self._subject_allocations.get(int(control.name.split("_")[1]))
                    if control.name.endswith("_cv_folds") and allocation is not None:
                        allocation.fold_choice = automatic
                if control.name.endswith("_model_method"):
                    from .tissue_classifier_choices import TissueClassifierChoices
                    label.setText("")
                    label.setMaximumHeight(0)
                    field.layout().removeWidget(editor)
                    chooser = TissueClassifierChoices(editor, field, cells=bool(form.context.get("supervised")))
                    self._tissue_classifier_choices[int(control.name.split("_")[1])] = chooser
                    field.layout().insertWidget(0, chooser)
                if control.name == "classifier_source":
                    from .classifier_source_choices import ClassifierSourceChoices
                    label.setText("")
                    label.setMaximumHeight(0)
                    field.layout().removeWidget(editor)
                    field.layout().insertWidget(0, ClassifierSourceChoices(
                        editor, field, allow_skip=bool(form.context.get("remote_execution"))
                    ))
                if form.context.get("supervised") and control.name == "supervised_0_feature_ids":
                    # The page heading already names this complete input selector.
                    label.setText("")
                    label.setMaximumHeight(0)
                if tissue_training and self.native_feature_inspection_available and control.name.endswith("_image_features"):
                    from .pixel_feature_summary import PixelFeatureSummary
                    index = int(control.name.split("_")[1])
                    summary = PixelFeatureSummary(field, training=True)
                    context = form.context["resume_context"]
                    candidate = sorted(context["candidate_entry_ids"])[index]
                    summary.set_preview_images(context["candidate_entry_ids"][candidate], context["candidate_participants"])
                    summary.inspect_requested.connect(lambda index=index: self.feature_inspection_requested.emit(index))
                    self._pixel_feature_summaries[index] = summary
                    editor.hide()
                    label.setText("")
                    label.setMaximumHeight(0)
                    field.layout().addWidget(summary)
                definition = training_page(
                    control.name,
                    supervised=bool(form.context.get("supervised")),
                    candidates=tuple(
                        ", ".join(row["display_name"] for row in form.context["resume_context"]["secondary_output_classes_by_candidate_id"][key]["structures"])
                        if form.context.get("secondary") else key
                        for key in sorted(form.context["resume_context"]["candidate_entry_ids"])
                    ),
                    primary=bool(form.context.get("primary")), secondary=bool(form.context.get("secondary")),
                )
                if control.kind is ControlKind.CHECKBOX:
                    editor.setText(control.label)
                    label.setText("")
                    label.setMaximumHeight(0)
                self._stage_pages.add_control(
                    definition, control.name, label, field,
                    classifier_choices=(self._tissue_classifier_choices[int(control.name.split("_")[1])]
                                        if control.name.endswith("_procedure") and int(control.name.split("_")[1]) in self._tissue_classifier_choices else None),
                    compact=(control.kind in {ControlKind.INTEGER, ControlKind.NUMBER}
                             or control.name.endswith("_kronos_patch_size")
                             or "_role_" in control.name or "_class_" in control.name
                             or (tissue_training and ("_grid_" in control.name or control.name.endswith(("_svm_kernel", "_procedure"))))),
                    advanced=advanced_training_control(control.name, primary=bool(form.context.get("primary")),
                                                       supervised=bool(form.context.get("supervised")), secondary=bool(form.context.get("secondary"))),
                    table=(self._subject_allocations.get(int(control.name.split("_")[1]))
                           if control.name.startswith("training_") and (
                               "_patient_" in control.name or control.name.endswith("_test_subjects")
                               or control.name.endswith("_cv_folds"))
                           else None),
                )
            elif paged_segmentation:
                if control.name == "method":
                    from .segmentation_method_choices import SegmentationMethodChoices
                    definition = SettingsPage("segmentation-method", "Method", "Choose the outlines to detect from the staining in your images.", 0)
                    label.setText("")
                    label.setMaximumHeight(0)
                    field.layout().insertWidget(0, SegmentationMethodChoices(editor, field))
                else:
                    definition = SettingsPage("segmentation-settings", "Settings", "", 1)
                if control.kind is ControlKind.CHECKBOX:
                    editor.setText(control.label)
                    label.setText("")
                    label.setMaximumHeight(0)
                self._stage_pages.add_control(definition, control.name, label, field,
                    compact=control.name in {"channel_2", "channel_3", "tile_size_px", "padding_px"},
                    advanced=control.name in advanced_fields)
            elif adapter == "alignment_setup":
                # Alignment fields are shown and hidden as the route changes.
                # Keep each caption and its wrapping field in the parent vertical
                # layout, which recalculates their full height at the current width.
                label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
                if target_controls is not None:
                    target_controls.addRow(label, field)
                else:
                    self._layout.addWidget(label)
                    self._layout.addWidget(field)
            else:
                label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
                target_controls.addRow(label)
                target_controls.addRow(field)
            self._editors[control.name] = editor
            self._errors[control.name] = error
            if adapter == "batch_stage1":
                self._stage1_rows[control.name] = (label, field)
            if adapter == "alignment_setup":
                label.setWordWrap(True)
                label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
                self._alignment_rows[control.name] = (label, field)
            if adapter == "batch_reference_start":
                self._unmixing_rows[control.name] = (label, field)
            if form.context.get("adapter") == "tissue_setup":
                self._tissue_rows[control.name] = (label, field)
                if control.name == "tissue_image_features" and self.native_feature_inspection_available:
                    from .pixel_feature_summary import PixelFeatureSummary
                    summary = PixelFeatureSummary(field)
                    summary.inspect_requested.connect(lambda: self.feature_inspection_requested.emit(-1))
                    self._pixel_feature_summaries[-1] = summary
                    editor.hide()
                    label.setText("")
                    label.setMaximumHeight(0)
                    field.layout().addWidget(summary)
            if form.context.get("primary"):
                self._primary_rows[control.name] = (label, field)
            if form.context.get("secondary"):
                self._secondary_rows[control.name] = (label, field)
            if adapter == "training_fit_review" and control.name.startswith("settings_"):
                self._fit_review_rows[control.name] = (label, field)
            if form.context.get("supervised"):
                self._supervised_rows[control.name] = (label, field)
            if form.context.get("spectral_input") and control.name in {
                    "normalization", "measurement_convention"}:
                self._library_declaration_rows[control.name] = (label, field)
            if adapter == "batch_stage1" and control.name == "component_values":
                self._stage1_component_label = label
            if adapter == "marker_setup":
                self._marker_rows[control.name] = (label, field)
            if adapter == "marker_gates":
                self._marker_review_rows[control.name] = (label, field)
            if form.context.get("adapter") in {"segmentation_setup", "segmentation_options"}:
                self._segmentation_rows[control.name] = (label, field)
            if _is_batch_clustering_form(form):
                self._clustering_rows[control.name] = (label, field)
            if form.context.get("adapter") == "measurement_setup":
                self._measurement_rows[control.name] = (label, field)
        if adapter == "batch_stage1":
            self._stage1_source_table = channel_table
            self._stage1_source_panel = channel_panel if channel_table is not None else None
            self._stage1_component_field = self._editors.get("component_values")
            cycle_editor = self._editors.get("channel_cycle_values")
            if cycle_editor is not None:
                cycle_editor.review_state_changed.connect(self._apply_stage1_visibility)
            if cycle_editor is not None and self._stage1_component_field is not None:
                cycle_editor.changed.connect(lambda: self._stage1_component_field.set_cycles(
                    [row["cycle"] for row in cycle_editor.value() if row["cycle"] is not None]))
            self._editors["channel_content"].currentIndexChanged.connect(self._apply_stage1_visibility)
            if self._stage1_component_field is not None:
                self._stage1_component_field.changed.connect(self._refresh_nuclear_stain)
            for name, editor in self._editors.items():
                if re.fullmatch(r"stage1_\d+_component_name", name):
                    editor.textChanged.connect(self._refresh_nuclear_stain)
            self._editors["nuclear_stain"].currentIndexChanged.connect(self._normalize_nuclear_marker)
            self._apply_stage1_visibility()
        if channel_table is not None:
            channel_table.finish_tab_order()
        if paged_measurements:
            note = QLabel("If performing phenotyping by marker-threshold or unsupervised methods, include Mean or Median", self)
            note.setWordWrap(True)
            note.setProperty("fieldHelp", True)
            self._stage_pages.add_summary("intensities", note)
            from mif_pipeline.gui.measurement_setup import segmentation_measurement_note
            boundary_source = form.context.get("stage_result", {}).get("resume_context", {}).get("cell_boundary_source")
            note = QLabel(segmentation_measurement_note(boundary_source) if boundary_source else "Nuclear shape measurements require a segmented nucleus.", self)
            note.setWordWrap(True)
            note.setProperty("fieldHelp", True)
            self._stage_pages.add_summary("shape", note)
        if controls is not None:
            if adapter == "tissue_setup":
                card = QFrame(self)
                card.setObjectName("tissue-setup-settings")
                card.setProperty("stageFormPage", True)
                card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
                card_layout = QVBoxLayout(card)
                card_layout.setContentsMargins(24, 22, 24, 24)
                card_layout.setSpacing(12)
                self._setup_heading.setProperty("panelHeading", False)
                self._setup_heading.setProperty("stagePageHeading", True)
                self._tissue_hint.setProperty("stagePageDescription", True)
                card_layout.addWidget(self._setup_heading)
                card_layout.addWidget(self._tissue_hint)
                card_layout.addLayout(controls)
                self._layout.addWidget(card)
            elif adapter in {"segmentation_setup", "segmentation_options", "training_output_review"} or _is_batch_clustering_form(form):
                card = QFrame(self)
                card.setObjectName("stage-settings-card")
                card.setStyleSheet("QFrame#stage-settings-card { background: white; border: 1px solid #dbe4e5; border-radius: 12px; }")
                self._settings_card_layout = QVBoxLayout(card)
                self._settings_card_layout.setContentsMargins(22, 20, 22, 20)
                self._settings_card_layout.setSpacing(14)
                if adapter != "training_output_review":
                    self._setup_heading.setStyleSheet("color: #173d47; font-size: 21px; font-weight: 600;")
                    self._settings_card_layout.addWidget(self._setup_heading)
                self._settings_card_layout.addLayout(controls)
                self._layout.addWidget(card)
            else:
                self._layout.addLayout(controls)
        if adapter == "alignment_setup":
            self._alignment_help = QLabel(self)
            self._alignment_help.setWordWrap(True)
            self._layout.addWidget(self._alignment_help)
            self._alignment_advanced = QToolButton(self)
            self._alignment_advanced.setObjectName("alignment-advanced")
            self._alignment_advanced.setText("Advanced settings")
            self._alignment_advanced.setCheckable(True)
            self._alignment_advanced.toggled.connect(self._apply_alignment_visibility)
            self._layout.addWidget(self._alignment_advanced, alignment=Qt.AlignmentFlag.AlignLeft)
            if advanced_controls is not None:
                self._layout.addLayout(advanced_controls)
        if reference_rows:
            from .reference_settings import ReferenceSettingsPanel

            self._reference_settings_panel = ReferenceSettingsPanel(
                reference_rows, reference_groups, self
            )
            preparation = form.context.get('reference_region_workspace', {}).get('preparation_role')
            if preparation in {'control', 'stained'} and self._reference_regions_panel is not None:
                self._reference_regions_panel.attach_settings(self._reference_settings_panel)
            else:
                self._layout.addWidget(self._reference_settings_panel)
        if adapter == "batch_reference_start":
            self._unmixing_next = ContentSizedButton("Next: unmixing settings", self)
            self._unmixing_next.setObjectName("unmixing-settings-next")
            self._unmixing_next.setProperty("actionRole", "primary")
            self._unmixing_next.clicked.connect(lambda: self._set_unmixing_step(1))
            self._unmixing_gate.layout().addWidget(
                self._unmixing_next, alignment=Qt.AlignmentFlag.AlignLeft
            )
            self._unmixing_advanced = QToolButton(self)
            self._unmixing_advanced.setObjectName("unmixing-advanced")
            self._unmixing_advanced.setText("Advanced settings")
            self._unmixing_advanced.setCheckable(True)
            self._unmixing_advanced.toggled.connect(self._apply_unmixing_visibility)
            self._layout.addWidget(self._unmixing_advanced)
            if advanced_controls is not None:
                self._layout.addLayout(advanced_controls)

        if paged_segmentation:
            self._segmentation_advanced = QToolButton(self)
            self._segmentation_advanced.setObjectName("segmentation-setup-advanced")
            self._segmentation_advanced.setText("Tile processing")
            self._segmentation_advanced.setCheckable(True)
            self._segmentation_advanced.toggled.connect(self._apply_segmentation_visibility)
            self._stage_pages.add_advanced_toggle(self._segmentation_advanced)
            self._editors["method"].currentIndexChanged.connect(self._apply_segmentation_visibility)
            self._editors["cell_expansion_um"].textChanged.connect(self._apply_segmentation_visibility)
            self._editors["limit_cell_expansion"].toggled.connect(self._apply_segmentation_visibility)
            self._setup_heading.hide()
            self._message.hide()

        if _is_batch_clustering_form(form):
            from .clustering_setup import ClusteringSetup

            self._clustering_setup = ClusteringSetup(
                form, self._editors, self._clustering_rows, self._setup_heading, self
            )
            self._clustering_advanced = self._clustering_setup.advanced
            self._clustering_setup.step_requested.connect(self._clustering_go_to_step)
            self._layout.addWidget(self._clustering_setup)
            self._message.hide()
            navigation = QWidget(self)
            navigation.setObjectName("stage-form-navigation")
            navigation_row = QHBoxLayout(navigation)
            navigation_row.setContentsMargins(0, 4, 0, 4)
            navigation_row.setSpacing(10)
            self._clustering_back = ContentSizedButton("Back", self)
            self._clustering_back.setObjectName("clustering-setup-back")
            self._clustering_back.clicked.connect(self._clustering_previous)
            navigation_row.addWidget(self._clustering_back)
            self._clustering_position = QLabel(navigation)
            self._clustering_position.setWordWrap(True)
            navigation_row.addWidget(self._clustering_position, 1)
            self._clustering_next_button = ContentSizedButton("Next", self)
            self._clustering_next_button.setProperty("actionRole", "primary")
            self._clustering_next_button.setObjectName("clustering-setup-next")
            self._clustering_next_button.clicked.connect(self._clustering_next)
            navigation_row.addWidget(self._clustering_next_button)
            self.navigation_widget = navigation
            self._layout.addWidget(navigation)
            scope = self._editors.get("clustering_mode")
            if isinstance(scope, QComboBox):
                scope.currentIndexChanged.connect(self._apply_clustering_visibility)
            method = self._editors.get("method")
            if isinstance(method, QComboBox):
                method.currentIndexChanged.connect(self._apply_clustering_visibility)

        if form.context.get("primary"):
            self._primary_advanced = QToolButton(self)
            self._primary_advanced.setObjectName("primary-plan-advanced")
            self._primary_advanced.setText("Advanced settings")
            self._primary_advanced.setCheckable(True)
            self._primary_advanced.toggled.connect(self._apply_primary_visibility)
            self._stage_pages.add_advanced_toggle(self._primary_advanced)

        if form.context.get("supervised"):
            self._supervised_advanced = QToolButton(self)
            self._supervised_advanced.setObjectName("supervised-plan-advanced")
            self._supervised_advanced.setText("Advanced settings")
            self._supervised_advanced.setCheckable(True)
            self._supervised_advanced.toggled.connect(self._apply_supervised_visibility)
            self._stage_pages.add_advanced_toggle(self._supervised_advanced)

        if form.context.get("secondary"):
            structures = form.context["resume_context"]["secondary_output_classes_by_candidate_id"]
            for index, candidate_id in enumerate(sorted(structures)):
                record = structures[candidate_id]
                scope = QLabel("Detection area: "
                    + ", ".join(parent["display_name"] for parent in record["eligible_parents"]), self)
                scope.setObjectName(f"secondary-training-scope-{index}")
                scope.setTextFormat(Qt.TextFormat.PlainText)
                scope.setWordWrap(True)
                self._stage_pages.add_summary(f"{index}-model", scope, before_controls=True)
            self._secondary_advanced = QToolButton(self)
            self._secondary_advanced.setObjectName("secondary-plan-advanced")
            self._secondary_advanced.setText("Advanced settings")
            self._secondary_advanced.setCheckable(True)
            self._secondary_advanced.toggled.connect(self._apply_secondary_visibility)
            self._stage_pages.add_advanced_toggle(self._secondary_advanced)

        if adapter == "training_fit_review" and self._fit_review_rows:
            self._fit_review_advanced = QToolButton(self)
            self._fit_review_advanced.setObjectName("training-fit-review-advanced")
            self._fit_review_advanced.setText("Advanced settings")
            self._fit_review_advanced.setCheckable(True)
            self._fit_review_advanced.toggled.connect(self._apply_fit_review_visibility)
            self._layout.addWidget(self._fit_review_advanced)
            if advanced_controls is not None:
                self._layout.addLayout(advanced_controls)

        self._cv_summaries = {}
        self._training_search_errors = {}
        if form.context.get("development_cv_policy"):
            for name in self._editors:
                match = re.fullmatch(r"training_(\d+)_procedure", name)
                if match:
                    summary = QLabel("", self)
                    summary.setObjectName("training-grid-summary-" + match.group(1))
                    summary.setWordWrap(True)
                    summary.setProperty("choiceHelp", True)
                    self._cv_summaries[int(match.group(1))] = summary
                    panel_key = match.group(1) + ("-model" if tissue_training else "-method")
                    panel = self._stage_pages.tissue_parameters.get(panel_key) if self._stage_pages is not None else None
                    if panel is not None:
                        panel.add_summary(summary)
                    elif tissue_training:
                        self._tissue_classifier_choices[int(match.group(1))].layout().addWidget(summary)
                    elif self._stage_pages is not None:
                        self._stage_pages.add_summary(panel_key, summary)
                    else:
                        self._layout.addWidget(summary)

        if self._subject_allocations:
            for index, allocation in self._subject_allocations.items():
                self._stage_pages.add_summary(f"{index}-subjects", allocation)
                allocation.bind_values(lambda: self._raw_values(draft=True))
                allocation.changed.connect(self._refresh_training_search)
        if self._subject_allocations and tissue_training:
            from .subject_allocation import ReferencePlanSummary

            self._reference_plan = ReferencePlanSummary(
                self._subject_allocations, lambda: self._raw_values(draft=True), self,
            )
            self._reference_plan.image_selected.connect(self.reference_image_selected)
            self._stage_pages.add_summary("references", self._reference_plan, before_controls=True)
            if form.context.get("secondary"):
                self._reference_plan.selected_index = self._stage_pages.candidate_index
            self._reference_plan.refresh()

        if adapter == "marker_setup":
            from .form_navigation import FormNavigation

            navigation = FormNavigation(self)
            self._marker_back = ContentSizedButton("Back", self)
            self._marker_back.setObjectName("marker-setup-back")
            self._marker_back.clicked.connect(lambda: self._set_marker_step(max(-1, self._marker_step - 1)))
            navigation.addWidget(self._marker_back)
            self._marker_next_button = ContentSizedButton("Next: marker method", self)
            self._marker_next_button.setProperty("actionRole", "primary")
            self._marker_next_button.setObjectName("marker-setup-next")
            self._marker_next_button.clicked.connect(self._marker_next)
            navigation.addWidget(self._marker_next_button)
            self.navigation_widget = navigation
        if adapter == "marker_combinations":
            from .marker_combinations import MarkerCombinationEditor
            self._marker_combinations = MarkerCombinationEditor(
                form.context["stage_result"]["resume_context"]["canonical_marker_ids"], self)
            initial = {control.name: control.value for control in form.controls}
            self._marker_combinations.set_value(initial.get("marker_composite_names") or (),
                [{"phenotype_id": name, "marker_states": states}
                 for name, states in form.context.get("initial_marker_states", {}).items()])
            self._layout.addWidget(self._marker_combinations)

        if form.context.get("adapter") == "tissue_setup":
            from .form_navigation import FormNavigation

            navigation = FormNavigation(self)
            back = ContentSizedButton("Back", self)
            back.setObjectName("tissue-setup-back")
            back.setProperty("actionRole", "secondary")
            back.clicked.connect(lambda: self._set_tissue_step(max(0, self._tissue_step - 1)))
            navigation.addWidget(back)
            next_button = ContentSizedButton("Next: image detail", self)
            next_button.setProperty("actionRole", "primary")
            next_button.setObjectName("tissue-setup-next")
            next_button.clicked.connect(self._tissue_next)
            navigation.addWidget(next_button)
            self.navigation_widget = navigation
            self._tissue_create = ContentSizedButton("Create detector in QuPath", self)
            self._tissue_create.setObjectName("tissue-create-detector")
            self._tissue_create.setProperty("actionRole", "primary")
            self._tissue_create.clicked.connect(
                lambda: self.tissue_authoring_requested.emit(
                    self._editors["detector_source"].currentData()
                )
            )
            navigation.addWidget(self._tissue_create)
            self._tissue_back = back
            self._tissue_next_button = next_button

        region_projection = form.context.get("region_review")
        if region_projection is not None:
            from .region_review import RegionDecisionReview
            self._region_review = RegionDecisionReview(region_projection, self,
                allow_exclusion=any(control.name == "excluded_roi_ids" for control in form.controls))
            if region_projection.get("kind") != "secondary":
                self._layout.addWidget(self._region_review)

        from .training_references import build_training_reference_summary, is_training_reference_table
        from .training_fit_summary import build_training_fit_summary

        fit_summary = build_training_fit_summary(form, self)
        reference_summary = None if fit_summary is not None else build_training_reference_summary(form, self)
        if adapter == "supervised_references":
            from .reference_cell_annotations import ReferenceCellAnnotations
            self._supervised_reference_panel = ReferenceCellAnnotations(form.context["resume_context"], self)
            self._message.hide()
            self._layout.addWidget(self._supervised_reference_panel)
        if fit_summary is not None:
            position = next((i for i in range(self._layout.count()) if self._layout.itemAt(i).layout() is controls), self._layout.count())
            self._layout.insertWidget(position, fit_summary)
            if self._fit_review_rows and fit_summary.settings_panel is not None:
                settings_layout = fit_summary.settings_panel.layout()
                # Keep the existing retrain fields with the fitted settings they amend.
                for item in (controls, advanced_controls):
                    if item is not None:
                        self._layout.removeItem(item)
                        settings_layout.insertLayout(settings_layout.count() - 1, item)
                self._layout.removeWidget(self._fit_review_advanced)
                settings_layout.insertWidget(settings_layout.count() - 1, self._fit_review_advanced)
        structure_review = None
        if form.context.get("reason") == "semantic_region_decision_required":
            from .structure_review import StructureCriterionReview

            position = next((i for i in range(self._layout.count()) if self._layout.itemAt(i).layout() is controls), self._layout.count())
            image_review = self._region_review if region_projection is not None and region_projection.get("kind") == "secondary" else None
            if image_review is not None:
                self._layout.removeItem(controls)
                self._heading.hide()
                self._message.hide()
            structure_review = StructureCriterionReview(form, self, image_review=image_review,
                controls_layout=controls if image_review is not None else None,
                subtype_editor=self._editors.get("subtype_choice") if image_review is not None else None,
                candidate_editor=self._editors.get("candidate_roi_id") if image_review is not None else None)
            self._layout.insertWidget(position, structure_review)
        if form.context.get("resume_context", {}).get("phase") == "prediction_review":
            from .supervised_prediction_review import SupervisedPredictionReview
            self._heading.hide()
            self._message.hide()
            self._layout.addWidget(SupervisedPredictionReview(form.context["resume_context"],
                self._editors.get("confidence_threshold"), self))
        for table in form.tables:
            if self._supervised_reference_panel is not None and table.table_id == "reference_images":
                continue
            if self._reference_regions_panel is not None and table.table_id == "native_reference_regions":
                continue
            if self._cluster_review is not None and table.table_id in self._cluster_review.consumed_tables:
                continue
            if table.table_id == "supervised_prediction_summary":
                continue
            if structure_review is not None and table.table_id in structure_review.consumed_tables:
                continue
            if fit_summary is not None and table.table_id in fit_summary.consumed_tables:
                continue
            if reference_summary is not None and is_training_reference_table(table.table_id):
                if table.table_id == "training_fit_classifiers":
                    self._layout.addWidget(reference_summary)
                continue
            if table.table_id == "stage1_component_rows":
                continue
            if channel_table is not None and table.table_id == "stage1_channel_rows":
                continue
            label = QLabel(table.title or field_label(table.table_id), self)
            label.setProperty("panelHeading", True)
            table_font = QFont(label.font())
            table_font.setBold(True)
            label.setFont(table_font)
            self._layout.addWidget(label)
            view = QTableView(self)
            if table.table_id == "primary_region_counts":
                from .primary_count_review import PrimaryCountReviewModel
                model = PrimaryCountReviewModel(table, view)
            else:
                model = FormTableModel(table, view)
            view.setModel(model)
            view.setObjectName(f"form-{form.form_id}-{table.table_id}-table")
            view.setAccessibleName(label.text())
            label.setBuddy(view)
            view.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
            style_scientific_table(view)
            fit_scientific_columns(view)
            _fit_bounded_table_height(view)
            if table.table_id == "primary_region_counts":
                from .primary_count_review import prepare_count_table
                prepare_count_table(view, table)
                _fit_bounded_table_height(view)
            if table.table_id == "registration_checks":
                from PySide6.QtWidgets import QHeaderView
                view.horizontalHeader().setSectionResizeMode(len(table.columns) - 1, QHeaderView.ResizeMode.Stretch)
                view.setFixedHeight(min(360, view.horizontalHeader().sizeHint().height()
                    + sum(view.rowHeight(row) for row in range(model.rowCount()))
                    + 2 * view.frameWidth() + 8))
            self._models.append(model)
            if paged_measurements:
                label.hide()
                toggle = QToolButton(self)
                toggle.setText("Channel metadata")
                toggle.setCheckable(True)
                from .researcher_widgets import configure_disclosure
                configure_disclosure(toggle)
                toggle.toggled.connect(view.setVisible)
                view.hide()
                self._stage_pages.add_summary("channels", toggle)
                self._stage_pages.add_summary("channels", view)
            else:
                self._layout.addWidget(view)

        if self.navigation_widget is not None:
            self._layout.addWidget(self.navigation_widget)

        # Alignment has several long, independent alternatives. Stack them so
        # enlarged text never forces the entire form beyond the viewport.
        area_choice = form.context.get("reason") == "analysis_domain_choice_required"
        count_review = form.context.get("reason") == "primary_structure_count_review_required"
        region_actions = form.context.get("reason") in {
            "tissue_section_review_required", "analysis_domain_rois_required", "analysis_domain_review_required",
        }
        if region_actions or count_review:
            from .wrapping_layout import WrappingLayout
            actions = WrappingLayout(None)
            actions.setSpacing(10)
        else:
            actions = QVBoxLayout() if adapter == "alignment_setup" or area_choice else QHBoxLayout()
        if adapter == "batch_stage1":
            self._stage1_actions = actions
        deferred_abort = None
        for action in form.allowed_actions:
            if count_review and action in {"review_classifier", "retrain_classifier"}:
                # Older requests offered these actions, but neither navigates
                # away from the count checkpoint. Use Workflow or QuPath.
                continue
            if action == 'review_supervised_classifier' and form.context.get('resume_context', {}).get('phase') == 'prediction_review':
                # The cutoff toolbar owns the assignment action and selected scope.
                continue
            if (adapter == "alignment_setup" or form.context.get("reason") == "registration_review_required") and action in {"side_by_side", "skip_moving_reference"}:
                continue
            # The review page owns the single native editor action. This protocol
            # action only repeats that prompt; retain its ID in the owner model.
            if form.context.get("reason") == "analysis_domain_rois_required" and action == "open_qupath":
                continue
            button = ContentSizedButton(
                (
                    "Refresh detectors"
                    if action == "refresh_tissue_detectors"
                    else (
                        "Configure"
                        if action == "configure_segmentation"
                        and form.context.get("adapter") in {"segmentation_setup", "segmentation_options"}
                        else (
                            "Change settings"
                            if action == "retrain_classifier" and adapter == "training_fit_review"
                            else (
                                "Accept fitted classifier"
                                if action == "accept"
                                and adapter == "training_fit_review"
                                and "retrain_classifier" in form.allowed_actions
                                else (
                                    "Save and next"
                                    if action
                                    in {
                                        "accept",
                                        "accept_unlabeled_clusters",
                                        "select_training_plan",
                                        "configure_segmentation",
                                        "select_tissue_detector",
                                        "select_tissue_native_level",
                                        "import_tissue_detector",
                                    }
                                    else (
                                        "Align images"
                                        if action == "import_warpy_transform"
                                        and adapter == "alignment_setup"
                                        else (
                                            "Compare without alignment"
                                            if action == "side_by_side"
                                            and adapter == "alignment_setup"
                                            else (
                                                "Continue without a paired image"
                                                if action == "skip_moving_reference"
                                                and adapter == "alignment_setup"
                                                else display_action_label(action)
                                            )
                                        )
                                    )
                                )
                            )
                        )
                    )
                ),
                self,
            )
            if form.context.get("reason") == "tissue_section_review_required":
                if action == "edit":
                    button.setText("Load saved edits")
                    button.setToolTip("Read outline edits saved in QuPath, then review them before accepting.")
                elif action == "repeat":
                    button.setText("Run detection again")
                    button.setToolTip("Create new candidate outlines using the same detector settings.")
            if form.context.get("resume_context", {}).get("schema_version") == "semantic-region-interaction/v2":
                if action == "accept_semantic_region":
                    button.setText("Accept and next")
                elif action == "reject_semantic_region":
                    button.setText("Reject and next")
                elif action == "select_semantic_region":
                    button.setText("Open region")
            if form.context.get("reason") in {
                "tissue_section_review_required", "analysis_domain_choice_required",
                "analysis_domain_rois_required", "analysis_domain_review_required",
            }:
                from .region_review import region_action_label
                button.setText(region_action_label(form.context["reason"], action, button.text()))
            if adapter == "training_output_review" and action == "retrain_classifier":
                button.setText("Save settings and fit")
            if adapter == "secondary_definition" and action == "accept":
                button.setText("Save structures")
            if adapter == "supervised_references" and action == "annotate_supervised_training":
                button.setText("Freeze references and fit")
            if adapter == "batch_reference_operation" and action == "accept":
                button.setText("Start unmixing" if form.context.get("starts_unmixing")
                               else "Next: review spectra" if form.context.get("selected_option") == "phenoimager_motif_profile"
                               else "Save and next")
            if action == 'accept' and form.context.get('reference_region_workspace'):
                workspace = form.context['reference_region_workspace']
                if workspace.get('preparation_role'):
                    button.setText('Review AF spectra' if workspace['preparation_role'] == 'control'
                                   else 'Assign AF controls')
                elif workspace.get('assignment'):
                    button.setText('Choose extraction bands')
            if action == "accept" and self._target_af_groups_panel is not None:
                button.setText("Update spectral preview")
                button.setEnabled(self._target_af_groups_panel.can_continue)
                self._target_af_groups_panel.changed.connect(
                    lambda button=button: button.setEnabled(self._target_af_groups_panel.can_continue))
            if action == 'accept' and form.context.get('reference_region_workspace', {}).get('source_review'):
                panel = self._reference_regions_panel
                def update_source_action(button=button, panel=panel):
                    button.setText(panel.action_label)
                    button.setEnabled(panel.can_continue)
                update_source_action()
                panel.changed.connect(update_source_action)
            elif action == "accept" and form.context.get("spectral_review") is not None and form.context.get("selected_option") in {"accept_control_target_af", "accept_final_run_basis"}:
                button.setText("Start unmixing" if form.context.get("starts_unmixing") else "Use these spectral references")
            if action == "accept" and form.context.get("reference_workspace"):
                button.setText({"accept_signal_dark_measurements": "Extract reference spectra",
                    "accept_extraction_qc": "Compare included regions", "retry_extraction": "Repeat extraction review",
                    "accept_fluorophore_consensus": "Use fluorophore spectrum"}[form.context["selected_option"]])
            if (action == 'accept' and form.context.get('target_af_combined_review')
                    and form.context.get('selected_option') != 'accept_control_target_af'):
                button.hide()
            button.setObjectName(f"form-{form.form_id}-action-{action}")
            if count_review:
                button.setText({
                    "accept_all_primary_structures": "Keep all regions",
                    "exclude_analysis_domain": "Exclude analysis region",
                }[action])
            if self._spectral_choices is not None:
                self._spectral_buttons.append(button)
            if action == "select_training_plan":
                self._training_plan_button = button
            button.setProperty("formAction", True)
            button.setProperty(
                "actionRole",
                (
                    "secondary"
                    if (
                        (count_review and action != "accept_all_primary_structures")
                        or (action == "retrain_classifier" and adapter == "training_fit_review")
                        or (form.context.get("reason") == "tissue_section_review_required" and action in {"edit", "repeat"})
                        or (adapter == "alignment_setup" and (not form.context.get("remote_execution") or action != "import_warpy_transform"))
                    )
                    else "primary"
                ),
            )
            if self._spectral_choices is not None or adapter == "secondary_definition" or count_review:
                # ContentSizedButton.setText may already have polished this action.
                # Apply the assigned role after setting the visible action label.
                button.style().unpolish(button)
                button.style().polish(button)
            if adapter == "marker_combinations" and action == "define_marker_composite_rules":
                button.setText("Apply phenotype rules")
            if form.context.get("reason") == "phenotyping_mode_required" and action == "select_phenotyping_mode":
                button.setText("Continue")
            button.clicked.connect(lambda _checked=False, selected=action: self._submit(selected))
            if (adapter in {"marker_setup", "marker_gates", "marker_combinations"}
                    or form.context.get("reason") == "phenotyping_mode_required") and action == "abort":
                button.setText("Abort phenotyping")
                button.setStyleSheet("QPushButton { background: transparent; color: #8c4141; border: none; padding: 8px 10px; }")
                deferred_abort = button
            elif area_choice:
                card = QFrame(self)
                card.setObjectName("analysis-area-choice-card")
                card.setFrameShape(QFrame.Shape.StyledPanel)
                card_layout = QVBoxLayout(card)
                card_layout.setContentsMargins(16, 12, 16, 12)
                title, detail = {
                    "use_whole_tissue": (
                        "Whole accepted sections",
                        "Analyze all tissue inside the accepted section outlines. No additional drawing is needed.",
                    ),
                    "select_analysis_rois": (
                        "Draw regions within sections",
                        "Draw or select regions in QuPath, then review their outlines before accepting.",
                    ),
                }[action]
                heading = QLabel(title, card)
                heading.setProperty("panelHeading", True)
                heading.setWordWrap(True)
                card_layout.addWidget(heading)
                description = QLabel(detail, card)
                description.setWordWrap(True)
                card_layout.addWidget(description)
                button.setText("Use whole sections" if action == "use_whole_tissue" else "Choose drawn regions")
                card_layout.addWidget(button, alignment=Qt.AlignmentFlag.AlignLeft)
                actions.addWidget(card)
            elif adapter == "alignment_setup":
                actions.addWidget(button, alignment=Qt.AlignmentFlag.AlignLeft)
            else:
                actions.addWidget(button)
            if (not form.context.get("saved_tissue_settings")
                    and adapter not in {"batch_reference_start", "batch_reference_operation", "secondary_definition"}
                    and not (action == "select_training_plan" and form.context.get("secondary"))
                    and action in {
                "accept",
                "accept_unlabeled_clusters",
                "select_training_plan",
                "configure_segmentation",
                "select_tissue_detector",
                "import_tissue_detector",
            }):
                from .paired_action import SavePauseButton

                pause = SavePauseButton(
                    button,
                    lambda _checked=False, chosen=action: self._submit(
                        chosen, continue_after_save=False
                    ),
                    self,
                )
                pause.setObjectName(f"form-{form.form_id}-pause-{action}")
                actions.addWidget(pause)
            if adapter == "batch_reference_start":
                self._unmixing_buttons[action] = button
            if form.context.get("adapter") == "tissue_setup":
                self._tissue_buttons[action] = button
                if action == "refresh_tissue_detectors":
                    button.setProperty("actionRole", "secondary")
                    button.style().unpolish(button)
                    button.style().polish(button)
            if form.context.get("primary"):
                self._primary_buttons[action] = button
            if form.context.get("supervised"):
                self._supervised_buttons[action] = button
            if adapter in {"marker_setup", "marker_gates", "marker_combinations"}:
                self._marker_buttons[action] = button
                if action == "abort":
                    button.setProperty("actionRole", "secondary")
            if paged_segmentation:
                self._segmentation_buttons[action] = button
            if form.context.get("adapter") == "measurement_setup":
                self._measurement_buttons[action] = button
            if _is_batch_clustering_form(form):
                if action == "configure_clustering":
                    button.setText("Apply settings")
                self._clustering_buttons[action] = button
        if adapter == "marker_combinations":
            editor = self._marker_combinations
            accept = self._marker_buttons.get("define_marker_composite_rules")
            back = ContentSizedButton("Back: phenotype rules", self)
            back.clicked.connect(lambda: editor.set_reviewing(False))
            next_button = ContentSizedButton("Next: review assignments", self)
            next_button.setProperty("actionRole", "primary")
            def review_assignments():
                from mif_pipeline.gui.marker_combinations import submit_marker_combination_form
                try:
                    definition = submit_marker_combination_form(form, "define_marker_composite_rules", self._raw_values())
                except (TypeError, ValueError) as exc:
                    self._present_error(str(exc), "define_marker_composite_rules")
                    return
                editor.start_review(definition["phenotype_definition"])
            next_button.clicked.connect(review_assignments)
            actions.insertWidget(0, back)
            actions.insertWidget(1, next_button)
            def update_assignment_page():
                self._heading.setText("Review phenotype assignments" if editor.reviewing else "Define phenotype combinations")
                self._heading.setVisible(editor.reviewing)
                back.setVisible(editor.reviewing)
                next_button.setVisible(not editor.reviewing)
                if accept is not None:
                    accept.setVisible(editor.reviewing)
                    accept.setText("Accept phenotype assignments")
                self.page_changed.emit()
            editor.page_changed.connect(update_assignment_page)
            update_assignment_page()
        if adapter == "secondary_definition":
            editor = self._editors["definitions"]
            accept = self.findChild(QPushButton, f"form-{form.form_id}-action-accept")
            back = ContentSizedButton("Back", self)
            back.setObjectName("secondary-back")
            back.clicked.connect(editor.back)
            next_button = ContentSizedButton("Next: count limits", self)
            next_button.setObjectName("secondary-next")
            next_button.setProperty("actionRole", "primary")
            next_button.clicked.connect(editor.next_page)
            done = ContentSizedButton("Done: return to structures", self)
            done.setObjectName("secondary-done")
            done.setProperty("actionRole", "primary")
            done.clicked.connect(editor.done)
            for index, button in enumerate((back, next_button, done)):
                actions.insertWidget(index, button)
            def sync_secondary_actions():
                self.findChild(QLabel, "form-label-definitions").hide()
                reason = editor.save_block_reason()
                structure_list = editor.page == "overview"
                preparing = editor.page == "preparation"
                choose_arrangement = structure_list and editor.needs_preparation()
                accept.setEnabled(not reason and not choose_arrangement)
                accept.setToolTip(reason)
                accept.setVisible(preparing or (structure_list and editor.has_structures() and not choose_arrangement))
                accept.setText("Save and continue" if preparing else "Save structures")
                self._message.setVisible(structure_list and bool(form.message))
                self._heading.setText(editor.page_title())
                back.setText("Back to structures" if preparing else "Back")
                back.setVisible(not structure_list)
                next_button.setVisible(choose_arrangement or editor.page in {"detection", "counts"})
                next_button.setEnabled(not choose_arrangement or not reason)
                next_button.setToolTip(reason if choose_arrangement else "")
                next_button.setText({"overview": "Next: prepare classifiers", "detection": "Next: count limits"}
                                    .get(editor.page, "Next: measurements"))
                done.setVisible(editor.page == "review")
            editor.page_changed.connect(sync_secondary_actions)
            editor.changed.connect(sync_secondary_actions)
            editor.skip_requested.connect(accept.click)
            editor.page_changed.connect(self.page_changed.emit)
            sync_secondary_actions()
        source_workspace = form.context.get('reference_region_workspace') or {}
        source_options_exposed = (source_workspace.get('source_review') and
            all(option in {'accept_shared_control_preparation', 'review_control_sets',
                           source_workspace.get('revision_option')}
                for option in next((control.choices for control in form.controls
                                    if control.name == 'option_value'), ())))
        if form.context.get('reference_preparation_revisit'):
            back = ContentSizedButton('Back', self)
            back.setObjectName('reference-preparation-back')
            back.setProperty('actionRole', 'secondary')
            back.clicked.connect(lambda: self.reference_back_requested.emit('reference_preparation_revisit'))
            actions.insertWidget(0, back)
        elif form.context.get('reference_local_return_option'):
            back = ContentSizedButton('Back', self)
            back.setObjectName('reference-local-back')
            back.setProperty('actionRole', 'secondary')
            back.clicked.connect(lambda: self._editors['option_value'].setCurrentIndex(
                self._editors['option_value'].findData(form.context['reference_local_return_option'])))
            actions.insertWidget(0, back)
        elif adapter == "batch_reference_operation" and form.context.get("back_option"):
            back = ContentSizedButton(form.context.get("back_label", "Back: spectral references"), self)
            back.setObjectName("reference-checkpoint-back")
            back.setProperty("actionRole", "secondary")
            back.clicked.connect(lambda: self.reference_back_requested.emit(form.context["back_option"]))
            actions.insertWidget(0, back)
            self._spectral_outer_back = back
        elif source_workspace.get('source_review') and source_workspace.get('revision_option'):
            # This is the source review's only annotation-revision route. Keep
            # its guarded revision signal, alongside the ordinary continuation.
            actions.insertWidget(0, self._reference_regions_panel.spectrum.edit_regions_button)
        elif adapter == "batch_reference_operation" and self._target_af_groups_panel is not None:
            from PySide6.QtWidgets import QMenu
            other_sources = QMenu(self)
            editor = self._editors['option_value']
            labels = form.context.get('choice_labels', {}).get('option_value', {})
            for option in next(c.choices for c in form.controls if c.name == 'option_value'):
                if option != 'review_reference_control_groups':
                    other_sources.addAction(labels.get(option, option),
                        lambda value=option: editor.setCurrentIndex(editor.findData(value)))
            if not other_sources.isEmpty():
                change_source = ContentSizedButton('Other AF source…', self)
                change_source.setMenu(other_sources)
                actions.insertWidget(0, change_source)
        elif (adapter == "batch_reference_operation" and form.context.get("selected_option") is not None
              and not form.context.get("reference_workspace")
              and not form.context.get("reference_regions_available")
              and not source_options_exposed):
            back = ContentSizedButton("Back: reference choices", self)
            back.setObjectName("reference-choices-back")
            back.setProperty("actionRole", "secondary")
            back.clicked.connect(lambda: self._editors["option_value"].setCurrentIndex(-1))
            actions.insertWidget(0, back)
            self._spectral_outer_back = back
        if (form.context.get("reference_workspace")
                and not (form.context.get("reference_preparation_progress")
                         or form.context.get("reference_component_progress"))):
            # Preparation overview already exposes the offered region and band
            # revisions. Keep this fallback only for forms without that route.
            from PySide6.QtWidgets import QMenu
            labels = form.context.get("choice_labels", {}).get("option_value", {})
            options = next(c.choices for c in form.controls if c.name == "option_value")
            menu = QMenu(self) if "retry_extraction" in options else None
            for index, option in enumerate(form.context.get("reference_navigation_options", ())):
                if menu is not None and option == "reselect_reference_rois":
                    menu.addAction(labels[option], lambda value=option: self.reference_back_requested.emit(value))
                    continue
                label = ("Back: reference regions" if self._reference_band_panel is not None
                         and option == "reselect_reference_rois" else labels[option])
                button = ContentSizedButton(label, self)
                button.setObjectName("reference-navigate-" + option)
                button.setProperty("actionRole", "secondary")
                button.clicked.connect(lambda _checked=False, value=option: self.reference_back_requested.emit(value))
                actions.insertWidget(index, button)
            if menu is not None:
                menu.addAction("Repeat extraction review", lambda: self._editors["option_value"].setCurrentIndex(
                    self._editors["option_value"].findData("retry_extraction")))
                more = QToolButton(self)
                more.setText("Review options")
                more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
                more.setMenu(menu)
                actions.addWidget(more)
        if self._spectral_choices is not None:
            self._spectral_back = ContentSizedButton("Back: library source", self)
            self._spectral_back.setObjectName("spectral-editor-back")
            self._spectral_back.setProperty("actionRole", "secondary")
            self._spectral_back.clicked.connect(lambda: self._set_spectral_page(0))
            actions.insertWidget(0, self._spectral_back)
            self._spectral_return = ContentSizedButton("Return to table", self)
            self._spectral_return.setObjectName("spectral-return-table")
            self._spectral_return.setProperty("actionRole", "secondary")
            self._spectral_return.clicked.connect(lambda: self._set_spectral_page(1))
            actions.addWidget(self._spectral_return)
        if adapter not in {"batch_reference_start", "batch_reference_operation"}:
            cancel = ContentSizedButton("Cancel", self)
            cancel.setObjectName(f"form-{form.form_id}-cancel")
            cancel.clicked.connect(self.cancelled.emit)
            actions.addWidget(cancel)
        if self._spectral_choices is not None:
            actions.insertStretch(2, 1)
        elif self._stage_pages is not None and self._stage_pages.classifier_overview is not None:
            overview_back = ContentSizedButton(self.secondary_back_caption, self)
            overview_back.setObjectName("secondary-classifiers-back")
            overview_back.clicked.connect(self.secondary_definition_back_requested)
            actions.insertWidget(0, overview_back)
            pages = self._stage_pages
            pages.page_changed.connect(lambda: overview_back.setVisible(pages.current_key == "classifiers"))
            overview_back.setVisible(pages.current_key == "classifiers")
            actions.removeWidget(self._training_plan_button)
            actions.addStretch(1)
            actions.addWidget(self._training_plan_button)
        elif not region_actions and not count_review:
            reference_workspace = form.context.get('reference_region_workspace') or {}
            af_task = (reference_workspace.get('preparation_role') in {'control', 'stained'}
                or reference_workspace.get('source_review') or reference_workspace.get('assignment')
                or self._target_af_groups_panel is not None)
            continuation = (self.findChild(QPushButton, f"form-{form.form_id}-action-accept")
                            if af_task or self._reference_band_panel is not None
                            or form.context.get('consensus_review')
                            or self._reference_extraction_panel is not None
                            or form.context.get('reference_source_overview') else None)
            if continuation is not None:
                if self._reference_extraction_panel is not None:
                    actions.removeWidget(continuation)
                    actions.addWidget(continuation)
                actions.insertStretch(actions.indexOf(continuation), 1)
            else:
                actions.addStretch(1)
        if deferred_abort is not None:
            actions.addWidget(deferred_abort)
        if adapter == "secondary_definition":
            self._editors["definitions"].layout().addLayout(actions)
        elif adapter == "tissue_setup":
            while actions.count():
                item = actions.takeAt(0)
                if item.widget() is not None:
                    self.navigation_widget.addWidget(item.widget())
        elif structure_review is not None and region_projection is not None and region_projection.get("kind") == "secondary":
            structure_review.add_decision_actions(actions)
        elif form.context.get("reference_workspace"):
            from .wrapping_layout import WrappingLayout
            reference_footer = (self._reference_band_panel is not None
                                or self._reference_extraction_panel is not None
                                or form.context.get('consensus_review'))
            navigation = QHBoxLayout() if reference_footer else WrappingLayout(None)
            self._reference_navigation_layout = navigation
            if self._af_control_review_panel is not None:
                self._af_continue_button = self.findChild(
                    QPushButton, f'form-{form.form_id}-action-accept')
                self._af_review_back = self.findChild(QPushButton, 'reference-navigate-reselect_reference_rois')
                self._af_has_region_back = self._af_review_back is not None
                if self._af_review_back is None:
                    self._af_review_back = ContentSizedButton('Back: AF control spectrum', self)
                    self._af_review_back.setProperty('actionRole', 'secondary')
                    actions.insertWidget(0, self._af_review_back)
                else:
                    self._af_review_back.clicked.disconnect()
                self._af_review_back.clicked.connect(lambda: self._set_af_control_step(False)
                    if self._af_control_band_step else self.reference_back_requested.emit('reselect_reference_rois'))
            while actions.count():
                item = actions.takeAt(0)
                if item.widget() is not None:
                    navigation.addWidget(item.widget())
                elif reference_footer and item.spacerItem() is not None:
                    navigation.addItem(item.spacerItem())
            if self._reference_band_panel is not None:
                if self._reference_settings_panel is not None:
                    self._layout.removeWidget(self._reference_settings_panel)
                if self._af_control_review_panel is not None:
                    self._reference_band_panel.add_review_actions(self._reference_settings_panel, QHBoxLayout())
                    self._layout.addLayout(navigation)
                    self._set_af_control_step(False)
                else:
                    self._reference_band_panel.add_review_actions(self._reference_settings_panel, navigation)
            elif form.context.get("consensus_review"):
                if self._reference_settings_panel is not None:
                    self._layout.removeWidget(self._reference_settings_panel)
                self._editors["consensus_roi_review"].add_review_actions(
                    self._reference_settings_panel, navigation)
            else:
                self._layout.addLayout(navigation)
            actions.deleteLater()
        else:
            self._layout.addLayout(actions)
        if self._library_declaration_rows:
            library_editor = self._editors.get("library_locator")
            if library_editor is not None:
                library_editor.textChanged.connect(self._apply_library_declarations)
            self._apply_library_declarations()
            self._set_spectral_page(self._spectral_step)
        if adapter == "alignment_setup":
            self._editors["alignment_method"].currentIndexChanged.connect(
                self._apply_alignment_visibility
            )
            if "alignment_local_refinement" in self._editors:
                self._editors["alignment_local_refinement"].toggled.connect(
                    self._apply_alignment_visibility
                )
            self._apply_alignment_visibility()
        if adapter == "batch_reference_start":
            self._editors["spectral_state_override"].currentIndexChanged.connect(
                lambda: self._set_unmixing_step(0)
            )
            self._editors["solver"].currentIndexChanged.connect(self._apply_unmixing_visibility)
            if "calibration_correction_mode" in self._editors:
                self._editors["calibration_correction_mode"].currentIndexChanged.connect(self._apply_unmixing_visibility)
            self._set_unmixing_step(0)
        if form.context.get("adapter") == "tissue_setup":
            source = self._editors["detector_source"]
            level = self._editors["native_level_selection_mode"]
            assert isinstance(source, QComboBox) and isinstance(level, QComboBox)
            source.currentIndexChanged.connect(self._tissue_source_mode_changed)
            level.currentIndexChanged.connect(self._apply_tissue_visibility)
            self._editors["native_resolution_index"].currentIndexChanged.connect(
                self._apply_tissue_visibility
            )
            self._apply_tissue_visibility()
        if form.context.get("primary") or form.context.get("secondary"):
            refresh = self._apply_primary_visibility if form.context.get("primary") else self._apply_secondary_visibility
            for editor in self._editors.values():
                if isinstance(editor, PixelFeatureEditor):
                    editor.changed.connect(refresh)
        if form.context.get("primary"):
            self._primary_step = 0
            source_editor = self._editors.get("classifier_source")
            if source_editor is not None:
                source_editor.currentIndexChanged.connect(self._refresh_training_search)
            saved_editor = self._editors.get("saved_classifier_export")
            if saved_editor is not None:
                summary = QLabel(self)
                summary.setObjectName("saved-classifier-description")
                summary.setWordWrap(True)
                self._primary_rows["saved_classifier_export"][1].layout().addWidget(summary)
                saved_editor.editingFinished.connect(self._describe_saved_classifier)
                saved_editor.textChanged.connect(self._refresh_training_search)
            for name, editor in self._editors.items():
                if name.startswith("primary_") and "_class_" in name and name.endswith(("_name", "_id")):
                    assert isinstance(editor, QLineEdit)
                    editor.textChanged.connect(self._apply_primary_visibility)
                elif isinstance(editor, ClassifierChannels):
                    editor.changed.connect(self._apply_primary_visibility)
                elif name.endswith("_ignore_policy") and isinstance(editor, QComboBox):
                    editor.currentIndexChanged.connect(self._apply_primary_visibility)
                elif name.endswith("_boundary_strategy") and isinstance(editor, QComboBox):
                    editor.currentIndexChanged.connect(self._apply_primary_visibility)
                elif name.endswith(
                    (
                        "_model_method",
                        "_svm_kernel",
                        "_assignment_mode",
                        "_intent",
                        "_channel",
                        "_procedure",
                        "_class_count",
                    )
                ) and isinstance(editor, QComboBox):
                    editor.currentIndexChanged.connect(self._apply_primary_visibility)
            self._apply_primary_visibility()
        if form.context.get("supervised"):
            for name, editor in self._editors.items():
                if isinstance(editor, QComboBox):
                    editor.currentIndexChanged.connect(self._apply_supervised_visibility)
                elif name.endswith("_kronos_channel_ids") and isinstance(editor, ClassifierChannels):
                    editor.changed.connect(self._apply_supervised_visibility)
            self._supervised_step = 0
            self._apply_supervised_visibility()
        if form.context.get("secondary"):
            for name, editor in self._editors.items():
                if isinstance(editor, ClassifierChannels):
                    editor.changed.connect(self._refresh_training_search)
                if name.endswith(
                    (
                        "_model_method",
                        "_svm_kernel",
                        "_boundary_strategy",
                        "_assignment_mode",
                        "_intent",
                        "_procedure",
                    )
                ) and isinstance(editor, QComboBox):
                    editor.currentIndexChanged.connect(self._apply_secondary_visibility)
            self._apply_secondary_visibility()
        if adapter == "training_fit_review" and self._fit_review_rows:
            for name, editor in self._editors.items():
                if (
                    name.startswith("settings_")
                    and name.endswith("_selected")
                    and isinstance(editor, QCheckBox)
                ):
                    editor.toggled.connect(self._apply_fit_review_visibility)
                elif (
                    name.startswith("settings_")
                    and name.endswith("_svm_kernel")
                    and isinstance(editor, QComboBox)
                ):
                    editor.currentIndexChanged.connect(self._apply_fit_review_visibility)
            self._apply_fit_review_visibility()
        if form.context.get("development_cv_policy"):
            for name, editor in self._editors.items():
                if name.startswith(("training_", "supervised_")):
                    signal = (
                        editor.textChanged
                        if isinstance(editor, QLineEdit)
                        else editor.currentIndexChanged if isinstance(editor, QComboBox) else None
                    )
                    if signal is not None:
                        signal.connect(self._refresh_training_search)
            if "reference_annotations_complete" in self._editors:
                self._editors["reference_annotations_complete"].toggled.connect(
                    self._refresh_training_search
                )
            self._refresh_training_search()
        if adapter == "marker_setup":
            from .marker_gates import MarkerSetupWorkbench
            from .phenotyping_scope import FitScopeChoices
            from mif_pipeline.orchestration.batch_phenotyping.marker_settings import marker_settings_locked
            marker_context = form.context["stage_result"]["resume_context"]
            settings_locked = marker_settings_locked(marker_context)
            scope_choices = FitScopeChoices(self._editors["marker_fit_scope"], self._marker_cards[0],
                stacked=True, descriptions={
                    "pooled": "Fit one threshold per marker using cells from all slides in a processing group.",
                    "per_slide": "Fit each marker separately on each slide using the cells from that slide.",
                })
            self._marker_scope_choices = {key: card.button for key, card in scope_choices.cards.items()}
            self._marker_cards[0].layout().insertWidget(2, scope_choices)
            self._editors["marker_fit_scope"].currentIndexChanged.connect(self._apply_marker_visibility)
            self._marker_workbench = MarkerSetupWorkbench(self, self._marker_cards[1])
            self._marker_cards[1].layout().addWidget(self._marker_workbench)
            self._layout.insertWidget(self._layout.indexOf(self._marker_cards[1]) + 1, self._marker_workbench.advanced_area)
            self._marker_step = 1 if settings_locked or marker_context.get("initial_marker_settings") else -1
            self._editors["selected_marker_ids"].setEnabled(not settings_locked)
            for name, editor in self._editors.items():
                if name.endswith(("_method", "_nimbus_source", "_feature")):
                    editor.currentIndexChanged.connect(self._apply_marker_visibility)
            self._apply_marker_visibility()
        if form.context.get("adapter") in {"segmentation_setup", "segmentation_options"}:
            self._apply_segmentation_visibility()
        if form.context.get("adapter") == "measurement_setup":
            for name, editor in self._editors.items():
                if name.endswith("_selected"):
                    editor.toggled.connect(self._apply_measurement_visibility)
            self._setup_heading.hide()
            self._apply_measurement_visibility()
        if _is_batch_clustering_form(form):
            self._set_clustering_step(self._clustering_setup.first_step)
        from .researcher_widgets import configure_disclosure

        for toggle in self.findChildren(QToolButton):
            if "advanced" in toggle.objectName():
                configure_disclosure(toggle)
        if any(form.context.get(kind) for kind in ("primary", "secondary", "supervised")):
            callback = self._apply_supervised_visibility if form.context.get("supervised") else (
                self._apply_primary_visibility if form.context.get("primary") else self._apply_secondary_visibility)
            for editor in self._editors.values():
                if isinstance(editor, KronosMarkerAssignment):
                    editor.changed.connect(callback)
        self._add_kronos_sampling_summaries()
        if adapter == "batch_stage1":
            self._apply_stage1_visibility()
        from .reference_sources import install_reference_source_overview
        install_reference_source_overview(self, form)
        apply_widget_accessibility(self, f"form-{form.form_id}")

    def _add_kronos_sampling_summaries(self) -> None:
        """Show each selected crop's physical dimensions beside its sampling controls.

        We read the draft fields without changing them. Cell images use independent
        X/Y sampling; tissue patches use the classifier's shared pixel size. Empty
        or invalid sampling leaves a request to complete those fields, while the
        existing form validation remains responsible for accepting the settings.
        """
        if self._stage_pages is None:
            return
        for name, size_editor in self._editors.items():
            if not name.endswith("_kronos_patch_size"):
                continue
            prefix = name.removesuffix("patch_size")
            cell = prefix.startswith("supervised_")
            index = prefix.split("_")[1]
            x = self._editors.get(prefix + "pixel_x_um" if cell else f"training_{index}_resolution")
            y = self._editors.get(prefix + "pixel_y_um") if cell else x
            if not isinstance(x, QLineEdit) or not isinstance(y, QLineEdit):
                continue
            label = QLabel(self._stage_pages)
            label.setObjectName(prefix + "physical_crop")
            label.setWordWrap(True)
            label.setProperty("fieldHelp", True)
            label.setContentsMargins(0, 2, 0, 0)
            context = self._form.context['resume_context']
            native_sampling = context.get('supervised_options', {}).get('recommended_pixel_size_um') if cell else None
            native_group = cell and len(context['candidate_entry_ids'][sorted(context['candidate_entry_ids'])[int(index)]]) > 1
            original_resolution = None
            if not cell:
                from mif_pipeline.gui.kronos_setup import (
                    tissue_original_resolution_caption, tissue_original_sampling_default,
                )
                candidate_id = sorted(context['candidate_entry_ids'])[int(index)]
                original_resolution = tissue_original_resolution_caption(context, candidate_id)
                default_resolution = tissue_original_sampling_default(context, candidate_id)
                if default_resolution is not None:
                    self._kronos_sampling_updates.append(self._offer_kronos_image_resolution(
                        x, self._editors[f'training_{index}_model_method'], default_resolution,
                        self.findChild(QComboBox, f'training-resolution-training_{index}_resolution'),
                    ))
            self._stage_pages.add_summary(f"{index}-kronos-model", label,
                sampling=(size_editor, x, y), native_sampling=native_sampling,
                native_group=native_group, original_resolution=original_resolution)

            def update(*_args, size=size_editor, horizontal=x, vertical=y, output=label, cell=cell):
                try:
                    side = int(size.currentData())
                    dx, dy = float(horizontal.text()), float(vertical.text())
                    valid = all(math.isfinite(v) and v > 0 for v in (dx, dy))
                except (TypeError, ValueError):
                    valid = False
                if not valid:
                    output.setText("Enter a positive pixel size for each axis." if cell
                                   else "Enter a positive pixel size.")
                    return
                output.setText(f"{side * dx:.4g} × {side * dy:.4g} µm")

            size_editor.currentIndexChanged.connect(update)
            x.textChanged.connect(update)
            if y is not x:
                y.textChanged.connect(update)
            self._kronos_sampling_updates.append(update)
            update()
            if cell:
                from .kronos_cell_preview import KronosCellPreview
                context = self._form.context['resume_context']
                candidate = sorted(context['candidate_entry_ids'])[int(index)]
                images = [(entry, context['candidate_participants'][entry]['sample_id'])
                          for entry in context['candidate_entry_ids'][candidate]]
                preview = KronosCellPreview(images, self._stage_pages)
                preview.requested.connect(self.kronos_cell_preview_requested)
                self._kronos_previews[index] = preview
                self._stage_pages.add_kronos_preview(f'{index}-kronos-model', preview)
                channels = self._editors[prefix + 'channel_ids']
                labels = self._form.context.get('choice_labels', {}).get(prefix + 'channel_ids', {})

                def refresh_preview(*_args, panel=preview, size=size_editor, horizontal=x,
                                    vertical=y, channels=channels, labels=labels):
                    panel.set_parameters(size.currentData(), horizontal.text(), vertical.text(),
                                         channels.value(), labels)

                size_editor.currentIndexChanged.connect(refresh_preview)
                x.textChanged.connect(refresh_preview)
                y.textChanged.connect(refresh_preview)
                channels.changed.connect(refresh_preview)
                self._kronos_sampling_updates.append(refresh_preview)
                refresh_preview()

    @staticmethod
    def _offer_kronos_image_resolution(editor, method, resolution, preset):
        """Manage only the blank draft and the untouched suggestion for this candidate."""
        managed_text = '' if not editor.text() else None

        def edited(*_args):
            nonlocal managed_text
            managed_text = None

        def text_changed(text):
            if text != managed_text:
                edited()

        def selected(*_args):
            nonlocal managed_text
            if editor.text() != managed_text or editor.property('kronosSamplingRestored'):
                return
            desired = str(resolution) if str(method.currentData()).startswith('kronos_') else ''
            if managed_text != desired:
                managed_text = desired
                editor.setText(desired)

        editor.textEdited.connect(edited)
        editor.textChanged.connect(text_changed)
        if preset is not None:
            preset.currentIndexChanged.connect(edited)
            preset.activated.connect(edited)
        method.currentIndexChanged.connect(selected)
        selected()
        return selected

    def _set_unmixing_step(self, step: int) -> None:
        state = self._editors["spectral_state_override"].currentData()
        self._unmixing_step = 1 if step == 1 and state == "raw" else 0
        self._apply_unmixing_visibility()
        self.page_changed.emit()

    def _apply_unmixing_visibility(self, *_args) -> None:
        if self._form is None or self._form.context.get("adapter") != "batch_reference_start":
            return
        state = self._editors["spectral_state_override"].currentData()
        settings = self._unmixing_step == 1 and state == "raw"
        self._editors["spectral_state_override"].setToolTip(self._editors["spectral_state_override"].currentText())
        self._message.setText(self._form.context.get("copy_state_message", self._form.message)
                              if state in {"unmixed", "isolated"} else self._form.message)
        self._heading.setText("Choose the unmixing method" if settings else "Confirm image status")
        self._message.setVisible(not settings)
        self._message.updateGeometry()
        advanced = settings and self._unmixing_advanced.isChecked()
        self._unmixing_gate.setVisible(not settings)
        self._measurement_band_panel.setVisible(state == "isolated")
        self._unmixing_back.setVisible(settings)
        self._unmixing_next.setVisible(state == "raw" and not settings)
        self._unmixing_advanced.setVisible(settings)
        for name, (label, field) in self._unmixing_rows.items():
            visible = (
                not settings
                if name == "spectral_state_override"
                else settings if name == "solver" else advanced
            )
            if name.startswith("calibration_"):
                mode_editor = self._editors.get("calibration_correction_mode")
                mode = mode_editor.currentData() if mode_editor is not None else None
                inventory = self._form.context.get("calibration_request") or {"missing": []}
                if name == "calibration_correction_mode":
                    visible = settings
                elif name == "calibration_source":
                    visible = settings and (mode == "full" or (mode == "exposure_only" and any(
                        row["field"] == "exposure_seconds" for row in inventory["missing"]
                    )))
                else:
                    row = inventory["missing"][int(name.removeprefix("calibration_"))]
                    visible = settings and (mode == "full" or (mode == "exposure_only" and row["field"] == "exposure_seconds"))
            if name.startswith("measurement_band_component_"):
                label.hide()
                field.setVisible(state == "isolated")
                continue
            if name in {"nnls_tolerance", "nnls_max_iterations"}:
                visible = visible and self._editors["solver"].currentData() == "nnls"
            label.setVisible(visible)
            field.setVisible(visible)
        for button in self._unmixing_buttons.values():
            button.setVisible(settings or state in {"unmixed", "isolated"})
            button.setText("Save and next")

    def _apply_alignment_visibility(self) -> None:
        """

        Shows the parameters used by the selected registration route and local-stage choice.

        Manual registration collects its settings in our native registration window. Automatic registration
        always shows the image channels and local-refinement checkbox. The advanced
        section shows global limits, and the B-spline fields appear only when
        refinement is enabled. Hidden unfinished values are ignored by the form decoder.

        """

        if self._form is None or self._form.context.get("adapter") != "alignment_setup":
            return
        remote = self._form.context.get("remote_execution", False)
        automatic = remote and self._editors["alignment_method"].currentData() == "automatic"
        advanced = self._alignment_advanced.isChecked()
        refine = remote and self._editors["alignment_local_refinement"].isChecked()
        required = {"alignment_fixed_channel", "alignment_moving_channel", "alignment_local_refinement"}
        local_fields = {
            "alignment_bspline_max_dimension", "alignment_bspline_iterations",
            "alignment_grid_spacing_um", "alignment_bending_weight", "alignment_maximum_displacement_um",
        }
        for name, (label, field) in self._alignment_rows.items():
            visible = name == "alignment_method" or (
                automatic and (advanced or name in required) and (name not in local_fields or refine)
            )
            label.setVisible(visible)
            field.setVisible(visible)
        selected_method = self._editors["alignment_method"].currentData()
        self._alignment_help.setText(
            "Set the starting position, then fit a global affine from the image signals. Matching landmarks can add a local thin-plate spline (TPS) if needed."
            if selected_method == "manual" else
            "Fit a global affine from the image signals, with an optional search across starting orientations. Inspect the overlay, then keep the affine or add local B-spline refinement."
        )
        self._alignment_help.setVisible(not remote)
        self._alignment_advanced.setVisible(automatic)
        button = self.findChild(QPushButton, "form-alignment_setup-action-import_warpy_transform")
        if button is not None:
            button.setText("Run automatic registration" if automatic else "Continue with saved alignment")

    def _measurement_next(self) -> None:
        errors = {}
        selected = [
            name
            for name, editor in self._editors.items()
            if name.endswith("_selected") and isinstance(editor, QCheckBox) and editor.isChecked()
        ]
        if not selected:
            name = "channel_0_selected"
            errors[name] = "Select at least one channel."
        for name in selected:
            role_name = name.removesuffix("_selected") + "_role"
            role = self._editors[role_name]
            assert isinstance(role, QComboBox)
            if role.currentData() is None:
                errors[role_name] = "Choose this channel's role."
        if not self._present_field_errors(errors):
            return
        self._set_measurement_step(1)

    def _set_measurement_step(self, step: int) -> None:
        self._measurement_step = step
        self._apply_measurement_visibility()

    def _apply_measurement_visibility(self, *_args) -> None:
        if self._form is None or self._form.context.get("adapter") != "measurement_setup":
            return
        self._setup_heading.setText(
            "Choose channels" if self._measurement_step == 0 else "Choose cell measurements"
        )
        for name, (label, field) in self._measurement_rows.items():
            if name.startswith("shape-"):
                label.setVisible(True)
                field.setVisible(True)
                continue
            first = name.endswith(("_selected", "_role"))
            visible = True if self._stage_pages is not None else first == (self._measurement_step == 0)
            if not first:
                selected = self._editors[name.rsplit("_", 1)[0] + "_selected"]
                visible = visible and isinstance(selected, QCheckBox) and selected.isChecked()
            label.setVisible(visible)
            field.setVisible(visible)
        if self._measurement_intensity_grid is not None:
            self._measurement_intensity_grid.set_included(
                int(name.split("_")[1]) for name, editor in self._editors.items()
                if name.startswith("channel_") and name.endswith("_selected") and editor.isChecked())
        self._sync_training_pages()

    def _apply_segmentation_visibility(self, *_args: object) -> None:
        if self._form is None or self._form.context.get("adapter") not in {"segmentation_setup", "segmentation_options"}:
            return
        advanced = self._segmentation_advanced.isChecked()
        method = self._editors["method"].currentData() if "method" in self._editors else "instanseg"
        previous = self._segmentation_previous_method
        if previous != method:
            editors = [self._editors[f"channel_{index}"] for index in (1, 2, 3)]
            if all(isinstance(editor, QComboBox) for editor in editors):
                if previous is not None:
                    self._segmentation_channel_selections[previous] = [editor.currentData() for editor in editors]
                    defaults = [self._form.context.get("nuclear_channel_index") or "", "", ""] if method == "stardist" else ["", "", ""]
                    for editor, value in zip(editors, self._segmentation_channel_selections.get(method, defaults)):
                        editor.setCurrentIndex(max(0, editor.findData(value)))
                elif method == "stardist" and not editors[0].currentData():
                    editors[0].setCurrentIndex(max(0, editors[0].findData(self._form.context.get("nuclear_channel_index"))))
            self._segmentation_previous_method = method
        main_fields = {"scope", "method", "channel_1", "channel_2", "channel_3", "pixel_size_um", "cell_expansion_um", "limit_cell_expansion", "cell_constrain_scale", "probability_threshold", "flow_threshold", "cellprob_threshold"}
        allowed = {"scope", "method", "tile_size_px", "padding_px"}
        if method == "instanseg":
            allowed.add("workers")
        else:
            allowed |= {"channel_1", "pixel_size_um"}
            allowed |= ({"cell_expansion_um", "limit_cell_expansion", "probability_threshold"} if method == "stardist" else
                        {"channel_2", "channel_3", "flow_threshold", "cellprob_threshold"})
        try:
            expansion = float(self._editors["cell_expansion_um"].text()) > 0
        except ValueError:
            expansion = False
        self._editors["limit_cell_expansion"].setEnabled(method == "stardist" and expansion)
        if method == "stardist" and expansion and self._editors["limit_cell_expansion"].isChecked():
            allowed.add("cell_constrain_scale")
        for name, (label, field) in self._segmentation_rows.items():
            visible = name in allowed and (name in main_fields or advanced)
            label.setVisible(visible and name != "limit_cell_expansion")
            field.setVisible(visible)
        if "channel_1" in self._segmentation_rows:
            self._segmentation_rows["channel_1"][0].setText("Nuclear channel" if method == "stardist" else "Primary cell channel")
        descriptions = {
            "method": {"instanseg": "Detects nuclei and cell outlines.",
                "cellpose": "Detects cell outlines. Nuclear outlines are unavailable.",
                "stardist": "Detects nuclear outlines. Expansion can estimate surrounding cell regions."}[method],
            "channel_1": "Select the nuclear stain." if method == "stardist" else "Choose a cell-boundary or cytoplasmic signal.",
        }
        for name, text in descriptions.items():
            help_label = self.findChild(QLabel, "form-help-" + name)
            if help_label is not None:
                help_label.setText(text)
            self._editors[name].setAccessibleDescription(text)

        self._stage_pages._pages["segmentation-settings"].findChild(QLabel).setText({"instanseg": "InstanSeg settings", "cellpose": "Cellpose-SAM settings", "stardist": "StarDist settings"}[method])
        self._sync_training_pages()

    def _apply_clustering_visibility(self, *_args: object) -> None:
        if self._form is None or not _is_batch_clustering_form(self._form):
            return
        if self._clustering_setup is not None:
            self._clustering_setup.refresh(self._clustering_step)

    def _set_clustering_step(self, step: int) -> None:
        self._clustering_step = step
        self._setup_heading.setText(("Choose how to fit slides", "Choose a method",
                                     "Choose marker measurements", "Set method parameters")[step])
        self._clustering_back.setVisible(step > self._clustering_setup.first_step)
        self._clustering_position.setText(f"Page {step + 1} of 4")
        self._clustering_next_button.setVisible(step < 3)
        self._clustering_next_button.setText(("Next: method", "Next: measurements",
                                               "Next: settings", "")[step])
        for button in self._clustering_buttons.values():
            button.setVisible(step == 3)
        self._apply_clustering_visibility()
        self.page_changed.emit()

    def _clustering_previous(self) -> None:
        self._set_clustering_step(self._clustering_step - 1)

    def _clustering_go_to_step(self, target: int) -> None:
        """Keep the same required choices when navigating by the step strip."""
        if target <= self._clustering_step:
            self._set_clustering_step(target)
            return
        while self._clustering_step < target:
            previous = self._clustering_step
            self._clustering_next()
            if previous == self._clustering_step:
                self._apply_clustering_visibility()
                return

    def _clustering_next(self) -> None:
        if not self._validate_current_fields():
            return
        if self._clustering_step == 0:
            values = self._raw_values()
            mode = values.get(
                "clustering_mode", self._form.context["resume_context"].get("clustering_mode")
            )
            if mode not in {"per_slide", "pooled"}:
                self._show_control_error(
                    "clustering_mode",
                    "Choose a clustering scope.",
                    self._editors["clustering_mode"],
                )
                return
        elif self._clustering_step == 2 and not self._raw_values().get("feature_ids"):
            self._show_control_error(
                "feature_ids", "Select at least one cell measurement.", self._editors["feature_ids"]
            )
            return
        for name in self._clustering_rows:
            self._errors[name].clear()
            self._errors[name].hide()
        self._set_clustering_step(self._clustering_step + 1)

    def _before_training_page_change(self, previous, target) -> bool:
        """Retain label/input checks before advancing into classifier fitting."""
        source = self._editors.get("classifier_source")
        if source is not None and source.currentData() in {"saved", "skip"}:
            return True
        errors = self._validate_current_fields(present=False) if target.order > previous.order else {}
        if self._form.context.get("development_cv_policy"):
            self._refresh_training_search()
            pages = self._stage_pages
            for index, (name, message) in self._training_search_errors.items():
                if pages.classifier_overview is not None and index != pages.candidate_index:
                    continue
                owner = pages._fields[name][0]
                leaving_invalid_page = previous.key == owner and target.order > previous.order
                entering_final_page = target.key == pages._available[-1] and target.key != owner
                if leaving_invalid_page or entering_final_page:
                    errors[name] = message
        if (self._form.context.get("adapter") == "measurement_setup"
                and previous.key == "intensities" and target.order > previous.order):
            for name, included in self._editors.items():
                if not name.startswith("channel_") or not name.endswith("_selected") or not included.isChecked():
                    continue
                prefix = name.removesuffix("_selected")
                region = self._editors[prefix + "_compartment"]
                statistics = self._editors[prefix + "_statistics"]
                if region.currentData() is None:
                    errors[prefix + "_compartment"] = "Choose a cell region."
                selected = statistics.value()
                if not selected:
                    errors[prefix + "_statistics"] = "Select at least one intensity measurement."
        if self._form.context.get("secondary") and previous.key.endswith("-channels") and target.order > previous.order:
            index = int(previous.key.split("-", 1)[0])
            name = f"secondary_{index}_channel_ids"
            if not self._editors[name].value():
                errors[name] = "Select at least one image channel."
        if self._form.context.get("primary") and target.order > previous.order:
            kind = (
                "class" if previous.key.endswith("-labels")
                else "channels" if previous.key.endswith("-channels") else None
            )
            if kind is not None:
                errors.update(self._primary_inputs_valid(kind, present=False))
            if previous.key.endswith("-region-counts"):
                prefix = "primary_" + previous.key.split("-", 1)[0]
                bounds = self._editors.get(prefix + "_class_bounds")
                if isinstance(bounds, PrimaryClassCountBounds):
                    errors.update(bounds.field_errors(prefix))
        if self._form.context.get("supervised") and target.order > previous.order:
            kind = previous.key.split("-", 1)[-1]
            if kind in {"labels", "features", "kronos-model", "kronos-panel"}:
                errors.update(self._supervised_inputs_valid(kind, present=False))
        if not self._present_field_errors(errors):
            return False
        if previous.phase == 0 and target.phase == 1:
            if self._form.context.get("adapter") == "measurement_setup":
                self._measurement_step = 0
                self._measurement_next()
                return self._measurement_step == 1
            if self._form.context.get("primary"):
                self._primary_step = 0
                self._primary_next()
                return self._primary_step == 1
            if self._form.context.get("supervised"):
                self._supervised_step = 0
                self._supervised_next()
                return self._supervised_step == 1
        return True

    def _training_page_changed(self) -> None:
        """Update final actions and let the host reset scrolling on page changes."""
        pages = self._stage_pages
        if pages is None:
            return
        if pages.current_key == "classifiers":
            self._present_field_errors({}, focus_first=False)
        # Keep feedback space stable while editing, not across settings pages.
        # A message still describing an invalid field must remain available.
        for feedback in (*self._errors.values(), self._form_error_summary):
            if not feedback.text():
                policy = feedback.sizePolicy()
                policy.setRetainSizeWhenHidden(False)
                feedback.setSizePolicy(policy)
                feedback.setMinimumHeight(0)
        if self._form.context.get("primary"):
            self._primary_step = pages.current_phase
        if self._form.context.get("supervised"):
            self._supervised_step = pages.current_phase
        if self._form.context.get("adapter") == "measurement_setup":
            self._measurement_step = pages.current_phase
        self._update_training_page_actions()
        if pages.classifier_overview is None and pages.at_last_page and self._training_search_errors:
            self._present_field_errors(dict(self._training_search_errors.values()), focus_first=False)
        self.page_changed.emit()

    def _update_training_page_actions(self) -> None:
        """Show submission only on the final page, including a detached footer."""
        pages = self._stage_pages
        page = pages.current_key.split("-", 1)[-1] if pages.current_key else ""
        for kind in ("primary", "secondary", "supervised"):
            if self._form.context.get(kind):
                applicable = page in {"method", "model", "fit", "subjects", "review", "kronos-model", "kronos-panel"}
                applicable |= (
                    kind == "primary" and page == "labels"
                    and not self._form.context.get("development_cv_policy")
                )
                applicable |= kind in {"primary", "secondary"} and page in {"references", "image-features"}
                applicable |= kind == "supervised" and page == "labels"
                if kind == "supervised" and page == "model":
                    applicable = False
                getattr(self, f"_{kind}_advanced").setVisible(applicable)
        if self._form.context.get("adapter") in {"segmentation_setup", "segmentation_options"}:
            self._segmentation_advanced.setVisible(pages.at_last_page)
            for button in self._segmentation_buttons.values():
                button.setVisible(pages.at_last_page)
        for action in self.findChildren(QPushButton):
            if action.property("formAction"):
                action.setVisible(pages.at_last_page)
        if pages.classifier_overview is not None:
            overview = pages.current_key == "classifiers"
            self._heading.setText("Prepare classifiers" if overview else self._form.title)
            self._message.hide()
        elif self._form.context.get("secondary"):
            self._message.setVisible(bool(self._form.message) and pages.current_key != "references")
        if self._training_plan_button is not None:
            self._training_plan_button.setVisible(pages.current_key == "classifiers"
                                                 if pages.classifier_overview is not None else pages.at_last_page)
        for button in self._measurement_buttons.values():
            button.setVisible(pages.at_last_page and pages.current_phase == 1)

    def _sync_training_pages(self) -> None:
        """Apply conditional field visibility before recalculating page navigation."""
        if self._stage_pages is not None:
            for update in self._kronos_sampling_updates:
                update()
            if self._form.context.get("primary") or self._form.context.get("secondary"):
                for name in self._editors:
                    match = re.fullmatch(r"training_(\d+)_resolution", name)
                    if match is not None:
                        index = match.group(1)
                        method_editor = self._editors[f"training_{index}_model_method"]
                        method = method_editor.currentData()
                        destination = "kronos-model" if str(method).startswith("kronos_") else "image-features"
                        self._stage_pages.move_control(name, f"{index}-{destination}")
                        scaling = f"training_{index}_normalization"
                        if scaling in self._editors:
                            scaling_page = "model" if str(method).startswith("kronos_") else "feature-preparation"
                            self._stage_pages.move_control(scaling, f"{index}-{scaling_page}")
            self._refresh_pixel_feature_summaries()
            self._stage_pages.sync()
            self._update_training_page_actions()

    def _refresh_pixel_feature_summaries(self):
        from mif_pipeline.gui.pixel_image_features import image_features_from_values
        if not self._pixel_feature_summaries:
            return
        values = self._raw_values(draft=True)
        for index, summary in self._pixel_feature_summaries.items():
            try:
                if index == -1:
                    summary.set_channel_count(1)
                    summary.set_recipe(image_features_from_values(values, "tissue"))
                    continue
                kind = "primary" if self._form.context.get("primary") else "secondary"
                summary.set_channel_count(len(values[f"{kind}_{index}_channel_ids"]))
                # Preserve the conventional recipe while KRONOS is selected.
                current = dict(values, **{f"training_{index}_model_method": "rtrees"})
                summary.set_recipe(image_features_from_values(current, f"training_{index}"),
                                   sampling=values.get(f"training_{index}_resolution"))
            except ValueError as error:
                summary.summary.setText(str(error).split(": ", 1)[-1])

    def feature_inspection_values(self, index, entry_id=None):
        """Project the current image recipe and selected channels without fitting settings."""
        import math
        from mif_pipeline.gui.pixel_image_features import image_features_from_values
        if index not in self._pixel_feature_summaries:
            raise ValueError("Open the classifier's Image preparation page.")
        if index == -1:
            setup = self.tissue_authoring_setup()
            row = next(row for row in self._form.context["tissue_image_context"]["native_pyramid_inventory"]
                       if row["native_resolution_index"] == setup["resolution_index"])
            return dict(entry_id=entry_id, classifier_index=-1,
                        channel_indices=[setup["channel_index"]],
                        pixel_size_um=[row["x_um_per_px"], row["y_um_per_px"]],
                        downsample=row["x_downsample"], pixel_features=setup["image_features"])
        context = self._form.context["resume_context"]
        candidate = sorted(context["candidate_entry_ids"])[index]
        entries = context["candidate_entry_ids"][candidate]
        entry_id = self._pixel_feature_summaries[index].preview_entry or entry_id
        if entry_id not in entries:
            entry_id = entries[0]
        values = self._raw_values(draft=True)
        prefix = f"training_{index}"
        if str(values[prefix + "_model_method"]).startswith("kronos_"):
            raise ValueError("Image filters are available with random forest or SVM channel inputs.")
        recipe = image_features_from_values(values, prefix)
        try:
            resolution = float(values[prefix + "_resolution"])
        except (ValueError, TypeError):
            raise ValueError("Enter a positive training pixel size before inspecting features.")
        if not math.isfinite(resolution) or resolution <= 0:
            raise ValueError("Enter a positive training pixel size before inspecting features.")
        # Resolve indices from this entry's full accepted panel, not the selected subset.
        participant = next((row for row in context["application_participants"]
                            if row["project_entry_id"] == entry_id), None)
        channels = (participant["channels"] if participant is not None else
                    context["available_channels_by_candidate_id"][candidate])
        kind = "primary" if self._form.context.get("primary") else "secondary"
        selected = set(values[f"{kind}_{index}_channel_ids"])
        indices = ([i for i, row in enumerate(channels) if row["channel_id"] in selected]
                   if kind == "primary" else [original for row, original in zip(
                       context["available_channels_by_candidate_id"][candidate],
                       context["channel_indices_by_candidate_id"][candidate]) if row["channel_id"] in selected])
        if not indices or len(indices) != len(selected):
            raise ValueError("Choose available image channels before inspecting features.")
        return dict(entry_id=entry_id, classifier_index=index, channel_indices=indices,
                    pixel_size_um=resolution, pixel_features=recipe)

    def apply_pixel_features(self, index, recipe):
        """Return one checked native recipe to the same existing form bindings."""
        from mif_pipeline.orchestration.batch_training.qupath_feature_settings import PixelImageFeatures
        checked = PixelImageFeatures.from_mapping(recipe).to_mapping()
        if index not in self._pixel_feature_summaries:
            return
        prefix = "tissue" if index == -1 else f"training_{index}"
        self.restore_values({prefix + "_image_features": checked})
        self._refresh_pixel_feature_summaries()

    def _primary_inputs_valid(self, selected_kind: str | None = None, *, present=True):
        """Validate class names and selected channel IDs before continuing."""
        values = self._raw_values(draft=True)
        errors = {}
        for name, value in values.items():
            if re.fullmatch(r"primary_\d+_channel_ids", name):
                if selected_kind in {None, "channels"} and not value:
                    errors[name] = "Select at least one image channel."
                continue
            match = re.fullmatch(r"(primary_\d+)_class_count", name)
            if match is None or selected_kind not in {None, "class"}:
                continue
            prefix = match.group(1)
            if value is None:
                errors[name] = "Choose the number of classes."
                continue
            if value == "1" and values.get(prefix + "_ignore_policy") != "annotated":
                policy = prefix + "_ignore_policy"
                errors[policy] = (
                    "With one class, choose Learn Ignore from annotated regions "
                    "and annotate Ignore examples."
                )
            for row in range(int(value)):
                row_name = f"{prefix}_class_{row}_name"
                if not str(values.get(row_name) or "").strip():
                    errors[row_name] = "Enter a class label."
        return self._present_field_errors(errors) if present else errors

    def _primary_next(self) -> None:
        if self._primary_inputs_valid():
            self._set_primary_step(1)

    def _supervised_next(self) -> None:
        if self._supervised_inputs_valid():
            self._set_supervised_step(1)

    def _supervised_inputs_valid(self, selected_kind: str | None = None, *, present=True):
        """Keep missing labels or inputs on the page where they are chosen."""
        values = self._raw_values(draft=True)
        errors = {}
        if selected_kind in {None, "labels"}:
            classes = values.get("supervised_0_class_ids") or ()
            if (len(classes) < 2 or len(set(classes)) != len(classes)
                    or any(not isinstance(name, str) or not name.strip() for name in classes)):
                name = "supervised_0_class_ids"
                errors[name] = "Enter at least two distinct class names matching the QuPath annotation labels."
        required = []
        if str(values.get("supervised_0_model_method", "")).startswith("kronos_"):
            if selected_kind in {None, "kronos-panel"}:
                required.append(("supervised_0_kronos_channel_ids", "Select the image channels to encode."))
            if selected_kind in {None, "kronos-model"}:
                for axis in ("x", "y"):
                    name = f"supervised_0_kronos_pixel_{axis}_um"
                    try:
                        value = float(values.get(name))
                        valid = math.isfinite(value) and value > 0
                    except (TypeError, ValueError):
                        valid = False
                    if not valid:
                        errors[name] = f"Enter a positive {axis.upper()} physical sampling size."
        elif selected_kind in {None, "features"}:
            required.append(("supervised_0_feature_ids", "Select at least one cell measurement."))
        for name, message in required:
            if not values.get(name):
                errors[name] = message
        return self._present_field_errors(errors) if present else errors

    def _marker_next(self) -> None:
        values = self._raw_values()
        if self._marker_step == -1:
            if not values.get("selected_marker_ids"):
                self._show_control_error("selected_marker_ids", "Select at least one marker.", self._editors["selected_marker_ids"])
                return
            self._set_marker_step(0)
            return
        if self._marker_step == 0:
            if values.get("marker_fit_scope") not in {"pooled", "per_slide"}:
                self._show_control_error("marker_fit_scope", "Choose Pooled or Per slide to continue.", self._editors["marker_fit_scope"])
                return
            self._marker_prepare_inputs = True
            try:
                self._submit("define_marker_composite_rules")
            finally:
                self._marker_prepare_inputs = False
            return
        if self._marker_step == 1:
            errors = self._validate_current_fields(present=False)
            context = self._form.context["stage_result"]["resume_context"]
            workflow = context.get("marker_workflow") or {}
            for marker in ((workflow["current_marker_id"],) if workflow else context["canonical_marker_ids"]):
                method = values.get(f"marker_{marker}_method")
                for suffix in ("feature", "method"):
                    field = f"marker_{marker}_{suffix}"
                    if not values.get(field):
                        errors[field] = "Choose a value for this marker."
                if method == "nimbus":
                    source = values.get(f"marker_{marker}_nimbus_source")
                    suffixes = () if source == "automatic" else ("nimbus_score_path",)
                    for suffix in suffixes:
                        field = f"marker_{marker}_{suffix}"
                        if not values.get(field):
                            errors[field] = "Choose the Nimbus input."
            if not self._present_field_errors(errors):
                return
            self._submit("define_marker_composite_rules")
            return
    def _set_marker_step(self, step: int) -> None:
        self._marker_step = step
        self._apply_marker_visibility()
        self.page_changed.emit()

    def _marker_go_to_step(self, target: int) -> None:
        if target <= self._marker_step:
            self._set_marker_step(target)
            return
        started_at_inputs = self._marker_step == -1
        self._apply_marker_visibility()
        self._marker_next()
        if started_at_inputs and target == 1 and self._marker_step == 0:
            self._marker_next()

    def _apply_marker_visibility(self, *_args) -> None:
        """Show one pre-fit decision at a time while retaining all marker settings.

        Method-specific fields are visible only for the selected marker and family.
        We keep edited values when navigating; the adapter excludes inapplicable bounds.
        """
        if self._form is None or self._form.context.get("adapter") != "marker_setup":
            return
        self._editors["selected_marker_ids"].refresh()
        titles = {-1: "Select markers and measurements", 0: "Choose fitting scope", 1: "Choose a marker method"}
        self._setup_heading.setText(titles[self._marker_step])
        self._heading.hide()
        self._message.hide()
        self._setup_heading.hide()
        for key, card in getattr(self, "_marker_scope_choices", {}).items():
            card.setChecked(key == self._editors["marker_fit_scope"].currentData())
        with QSignalBlocker(self._marker_tabs):
            self._marker_tabs.setCurrentIndex(self._marker_step + 1)
        for step, card in self._marker_cards.items():
            card.setVisible(step == self._marker_step)
        self._marker_selector.hide()
        marker = self._marker_selector.currentData()
        prefix = f"marker_{marker}_"
        selected = self._editors.get(prefix + "method")
        method = selected.currentData() if selected is not None else None
        for name, (label, field) in self._marker_rows.items():
            visible = False
            if self._marker_step == -1:
                visible = name == "selected_marker_ids"
            elif self._marker_step == 0:
                visible = False  # The shared scope cards provide this editor.
            elif self._marker_step == 1 and name.startswith(prefix):
                suffix = name.removeprefix(prefix)
                visible = suffix in {"feature", "method"}
                is_bound = suffix in {"lower_min", "lower_max", "upper_min", "upper_max"}
                visible |= is_bound and method in {"gmm", "gmm_separate", "gamma"}
                nimbus_source = self._editors[prefix + "nimbus_source"].currentData()
                visible |= suffix in {"nimbus_source", "nimbus_cutoff"} and method == "nimbus"
                visible |= suffix == "nimbus_score_path" and method == "nimbus" and nimbus_source == "scores"
                visible |= suffix in {"nimbus_checkpoint_path", "nimbus_magnification"} and method == "nimbus" and nimbus_source == "automatic"
                advanced = hasattr(self, "_marker_workbench") and self._marker_workbench.advanced.isChecked()
                if is_bound or suffix == "nimbus_checkpoint_path":
                    visible &= advanced
            label.setVisible(visible and not name.endswith(("_lower_min", "_lower_max", "_upper_min", "_upper_max")))
            field.setVisible(visible)
        self._marker_rows["selected_marker_ids"][0].hide()
        self._editors["marker_fit_scope"].hide()
        self._marker_rows["marker_fit_scope"][0].hide()
        from mif_pipeline.orchestration.batch_phenotyping.marker_settings import marker_settings_locked
        context = self._form.context["stage_result"]["resume_context"]
        workflow = context.get("marker_workflow") or {}
        settings_locked = marker_settings_locked(context)
        self._marker_tabs.setVisible(not settings_locked)
        self._marker_progress.setVisible(settings_locked)
        if settings_locked:
            count = len(self._form.context["stage_result"]["resume_context"]["canonical_marker_ids"])
            self._marker_progress.setText(f"Marker {len(workflow['completed']) + 1} of {count}")
        if hasattr(self, "_marker_workbench"):
            self._marker_workbench.refresh()
            self._marker_workbench.advanced_area.setVisible(self._marker_step == 1)
        self._marker_back.setVisible(self._marker_step > -1 and not settings_locked)
        self._marker_back.setText("Back")
        self._marker_next_button.setVisible(self._marker_step < 1 and not settings_locked)
        self._marker_next_button.setText("Next: fitting scope" if self._marker_step == -1 else "Next: marker method")
        empty_population = hasattr(self, "_marker_workbench") and self._marker_workbench.has_empty_selected_population()
        self._marker_next_button.setEnabled(not (self._marker_step == 1 and empty_population))
        scope_help = self.findChild(QLabel, "choice-help-marker_fit_scope")
        if scope_help is not None:
            text = method_guidance("marker_fit_scope", self._editors["marker_fit_scope"].currentData())
            scope_help.setText(text)
            scope_help.setVisible(bool(text))
        for action, button in self._marker_buttons.items():
            if action != "abort":
                button.setVisible(self._marker_step == 1)
                button.setText("Fit and review " + str(marker) if workflow else "Generate marker review")
                button.setEnabled(not empty_population)
                button.setToolTip("Change the cells selected for phenotyping before fitting." if empty_population else "")

    def _set_supervised_step(self, step: int) -> None:
        self._supervised_step = step
        self._apply_supervised_visibility()

    def _apply_supervised_visibility(self, *_args: object) -> None:
        if self._form is None or not self._form.context.get("supervised"):
            return
        method_editor = self._editors["supervised_0_model_method"]
        selected_method = method_editor.currentData()
        backend = "kronos" if selected_method.startswith("kronos_") else "measurements"
        method = selected_method.removeprefix("kronos_")
        advanced = self._supervised_advanced.isChecked()
        from mif_pipeline.gui.training_procedure import selected_procedure
        values = self._raw_values(draft=True)
        procedure = selected_procedure(values, 0)
        first_names = {"supervised_0_class_ids", "supervised_0_feature_ids",
                       "supervised_0_model_method"}
        selected_channels = values.get("supervised_0_kronos_channel_ids", [])
        channels = self._form.context["resume_context"]["supervised_options"].get("available_channels", ())
        for name, (label, field) in self._supervised_rows.items():
            first = name in first_names or "_kronos_" in name
            visible = True if self._stage_pages is not None else first == (self._supervised_step == 0)
            if name == "supervised_0_feature_ids":
                visible &= backend == "measurements"
            if "_kronos_" in name:
                visible &= backend == "kronos"
                if name.endswith(("_threads", "_memory_mib", "_divisor")) or (
                        self._stage_pages is None and name.endswith(("_pixel_x_um", "_pixel_y_um"))):
                    visible &= advanced
                panel = re.search(r"_panel_(\d+)_", name)
                if panel:
                    visible &= channels[int(panel.group(1))]["channel_id"] in selected_channels
            if name.startswith("supervised_") and not first:
                family = next((f for f in ("rtrees", "svm", "logistic_regression") if "_" + f + "_" in name), None)
                if family:
                    visible &= family == method
                    grid_fields = {"rtrees": ("max_depth", "min_sample_count", "active_var_count", "max_trees"),
                                   "svm": ("kernel", "c", "gamma"),
                                   "logistic_regression": ("learning_rate", "iterations", "regularization")}
                    main = any(name.endswith("_" + f) for f in grid_fields[family])
                    if main:
                        visible &= not procedure.cross_validation
                        if family == "logistic_regression" and not name.endswith("_regularization"):
                            visible &= advanced
                    else:
                        visible &= advanced
                    if name.endswith("_svm_gamma"):
                        visible &= values.get("supervised_0_svm_kernel") == "RBF"
                else:
                    visible &= name.endswith(("_normalization", "_pca_variance", "_pca_normalize")) or advanced
            if name.endswith("_seed"):
                visible &= advanced
            if "_additional_" in name:
                visible &= self._kronos_additional_marker_visible(name)
            visible = self._training_control_visible(name, visible, advanced=advanced)
            label.setVisible(visible)
            field.setVisible(visible)
        self._sync_training_pages()

    def _update_forest_feature_default(self, index, values):
        from .automatic_training_value import AutomaticTrainingValue
        from mif_pipeline.gui.pixel_image_features import image_features_from_values

        training = f"training_{index}"
        supervised = bool(self._form.context.get("supervised"))
        prefix = f"supervised_{index}" if supervised else training
        method = str(values.get(prefix + "_model_method"))
        count = None
        pending_label = "Default"
        if method.endswith("rtrees"):
            pending_label = "From input features"
            try:
                if method.startswith("kronos_") and not supervised:
                    # A graph-width search can give each RF a different input count.
                    widths = str(values.get(training + "_cv_graph_widths") or "").split(",")
                    from mif_pipeline.gui.training_procedure import selected_procedure
                    if selected_procedure(values, index).cross_validation and widths[0].strip():
                        count = int(widths[0]) if len(widths) == 1 else None
                    else:
                        count = int(values[training + "_kronos_width"])
                elif float(values.get(prefix + "_pca_variance") or 0) == 0:
                    if supervised:
                        count = 768 if method.startswith("kronos_") else len(values.get(prefix + "_feature_ids") or ())
                    else:
                        kind = "primary" if self._form.context.get("primary") else "secondary"
                        recipe = image_features_from_values(values, training)
                        per_channel = int(recipe["include_intensity"]) + sum(len(row["filters"]) for row in recipe["scales"])
                        count = len(values.get(f"{kind}_{index}_channel_ids") or ()) * per_channel
                else:
                    pending_label = "From retained PCs"
            except (KeyError, ValueError, TypeError):
                pass  # An incomplete input selection has no resolved feature count.
        for field in (prefix + "_rtrees_active_var_count", training + "_grid_rtrees_active_var_count"):
            combo = self.findChild(AutomaticTrainingValue, "automatic-" + field)
            if combo is not None:
                combo.set_feature_count(count, pending_label)

    def _refresh_training_search(self, *_args):
        if self._form is None or not self._form.context.get("development_cv_policy"):
            return
        if self._form.context.get("primary"):
            self._apply_primary_visibility()
        elif self._form.context.get("secondary"):
            self._apply_secondary_visibility()
        elif self._form.context.get("supervised"):
            self._apply_supervised_visibility()
        source = self._editors.get("classifier_source")
        if source is not None and source.currentData() in {"saved", "skip"}:
            self._training_search_errors = {}
            for summary in self._cv_summaries.values():
                summary.hide()
            if self._training_plan_button is not None:
                skipped = source.currentData() == "skip"
                self._training_plan_button.setText("Continue without primary classification" if skipped else "Use saved classifier")
                self._training_plan_button.setEnabled(skipped or bool(self._editors["saved_classifier_export"].text().strip()))
            return
        from mif_pipeline.gui.cv_authoring import grid_summary

        values = self._raw_values(draft=True)
        for allocation in self._subject_allocations.values():
            allocation.refresh()
        if self._reference_plan is not None:
            self._reference_plan.refresh()
        context = self._form.context["resume_context"]
        candidates = context["candidate_entry_ids"]
        participants = context["candidate_participants"]
        from mif_pipeline.gui.training_procedure import selected_procedure
        tissue_training = self._form.context.get("primary") or self._form.context.get("secondary")
        previous_errors = dict(self._training_search_errors.values())
        self._training_search_errors = {}
        for index, label in self._cv_summaries.items():
            self._update_forest_feature_default(index, values)
            prefix = f"training_{index}"
            entries = candidates[sorted(candidates)[index]]
            grouped = {}
            for entry in entries:
                grouped.setdefault(participants[entry]["subject_id"], []).append(entry)
            count = len(grouped)
            summary = None
            try:
                if values.get(prefix + "_assignment_mode", "manual") == "automatic":
                    if selected_procedure(values, index).independent_test:
                        count -= int(values.get(prefix + "_test_subjects", 0) or 0)
                else:
                    count = sum(
                        values.get(f"{prefix}_patient_{i}_role") == "training"
                        for i, _ in enumerate(sorted(grouped))
                    )
                if index in self._subject_allocations:
                    from mif_pipeline.gui.cv_authoring import TrainingSearchError

                    error = self._subject_allocations[index].validation_error()
                    if error:
                        raise TrainingSearchError(error, f"{prefix}_patient_0_role")
                summary = grid_summary(values, index, count, include_parameters=True)
                if summary["folds"]:
                    label.setText(
                        f"{summary['candidates']} candidates × {summary['folds']} subject folds + 1 final refit = {summary['fits']} fits."
                    )
                else:
                    label.clear()
            except (ValueError, TypeError) as error:
                name = getattr(error, "control_name", prefix + "_test_subjects")
                if name not in self._editors:
                    name = prefix + "_procedure"
                self._training_search_errors[index] = (name, str(error))
                label.setText(str(error))
            cross_validation = selected_procedure(values, index).cross_validation
            panel_key = f"{index}-model" if tissue_training else f"{index}-method"
            panel = self._stage_pages.tissue_parameters.get(panel_key) if self._stage_pages is not None else None
            if panel is not None:
                method_field = f"{prefix}_model_method" if tissue_training else f"supervised_{index}_model_method"
                panel.sync(values.get(method_field, "rtrees"), cross_validation,
                           (summary or {}).get("parameters", ()), tissue=tissue_training)
            elif tissue_training:
                self._tissue_classifier_choices[index].set_parameter_mode(cross_validation)
            label.setVisible(bool(label.text()))
        # Reconcile diagnostics already presented by a page boundary. Typing a
        # correction must also clear its field and summary link without erasing
        # an unrelated validation message or moving keyboard focus.
        active_errors = dict(self._training_search_errors.values())
        changed = {name for name, message in previous_errors.items()
                   if name in self._errors and self._errors[name].text() == message
                   and active_errors.get(name) != message}
        if changed:
            remaining = {name: error.text() for name, error in self._errors.items()
                         if error.text() and name not in changed}
            remaining.update((name, active_errors[name]) for name in changed if name in active_errors)
            self._present_field_errors(remaining, focus_first=False)
        confirmed = (self._editors["reference_annotations_complete"].isChecked()
                     if "reference_annotations_complete" in self._editors else True)
        # We retain the action itself while the review page moves its layout
        # into the fixed footer. Confirmation and parameter edits still update it.
        button = self._training_plan_button
        if button is not None:
            button.setText("Continue to cell annotations" if self._form.context.get("supervised") else "Freeze references and fit")
            channels_ready = (not self._form.context.get("secondary") or all(
                editor.value() for name, editor in self._editors.items()
                if re.fullmatch(r"secondary_\d+_channel_ids", name)))
            button.setEnabled(confirmed and channels_ready and not self._training_search_errors)
        self._secondary_candidate_status()
        if (self._stage_pages is not None and self._stage_pages.classifier_overview is None
                and self._stage_pages.at_last_page and self._training_search_errors):
            self._present_field_errors(dict(self._training_search_errors.values()), focus_first=False)

    def _set_primary_step(self, step: int) -> None:
        self._primary_step = step
        self._apply_primary_visibility()
        self._refresh_training_search()

    def _kronos_additional_marker_visible(self, name: str) -> bool:
        """Show metadata only for unknown selected identities, including DAPI remapping."""
        from mif_pipeline.compartment_analysis.kronos_marker_preparation import marker_key
        match = re.match(r"(.+_kronos)_panel_(\d+)_additional_", name)
        if match is None:
            return True
        prefix, number = match.group(1), int(match.group(2))
        marker_editor = self._editors[prefix + f"_panel_{number}_marker"]
        marker = marker_editor.value().strip()
        creating = bool(marker_editor.property("creatingAdditionalMarker"))
        if not marker and not creating:
            return False
        if prefix.startswith("supervised_"):
            if not self._editors[prefix.replace("_kronos", "_model_method")].currentData().startswith("kronos_"):
                return False
            channels = self._form.context["resume_context"]["supervised_options"]["available_channels"]
            selected = self._editors[prefix + "_channel_ids"].value()
            if channels[number]["channel_id"] not in selected:
                return False
        else:
            if self._editors[prefix.replace("_kronos", "_model_method")].currentData() not in {"kronos_rtrees", "kronos_svm"}:
                return False
            candidate = sorted(self._form.context["kronos_panels"])[int(prefix.split("_")[1])]
            channels = self._form.context["kronos_panels"][candidate]
        key = marker_key(marker)
        return creating or key not in self._kronos_vocabulary.get("markers", {})

    def _training_control_visible(self, name: str, visible: bool, *, advanced: bool) -> bool:
        if self._form.context.get("development_cv_policy") and "_output_label_" in name:
            return False
        match = re.match(r"training_(\d+)_", name)
        if match is None:
            return visible
        prefix = "training_" + match.group(1)

        def choice(suffix, default=None):
            editor = self._editors.get(prefix + suffix)
            return editor.currentData() if isinstance(editor, QComboBox) else default

        assignment = choice("_assignment_mode", "manual")
        if self._form.context.get("development_cv_policy"):
            from mif_pipeline.gui.training_procedure import selected_procedure

            procedure = selected_procedure(self._raw_values(draft=True), int(match.group(1)))
            intent = procedure.evaluation_intent
        else:
            procedure = None
            intent = choice("_intent")
        method = choice("_model_method", "rtrees")
        if self._form.context.get("supervised"):
            method = self._editors[f"supervised_{match.group(1)}_model_method"].currentData().removeprefix("kronos_")
        kronos = method in {"kronos_rtrees", "kronos_svm"}
        tuning = "grouped_cv" if procedure is not None and procedure.cross_validation else "fixed"
        page_visible = self._stage_pages is not None or (
            (not self._form.context.get("primary") or self._primary_step == 1)
            and (not self._form.context.get("supervised") or self._supervised_step == 1)
        )
        primary = bool(self._form.context.get("primary"))
        allocation = self._subject_allocations.get(int(match.group(1)))
        if (allocation is not None and len(allocation.subjects) == 1
                and ("_patient_" in name or name.endswith((
                    "_assignment_mode", "_test_subjects", "_validation_subjects", "_seed")))):
            # Role bindings stay inside the board’s hidden container. Keeping the
            # binding applicable also keeps the read-only one-subject board reachable.
            return visible and "_patient_" in name and (primary or bool(self._form.context.get("secondary")))
        core_visible = primary or bool(self._form.context.get("secondary")) or bool(self._form.context.get("supervised")) or advanced
        if (primary or self._form.context.get("secondary")) and name.endswith("_rtrees_active_var_count") and not advanced:
            return False
        if "_grid_" in name:
            if not page_visible or tuning != "grouped_cv" or not core_visible:
                return False
            if "_rtrees_" in name:
                return method in {"rtrees", "kronos_rtrees"}
            if "_logistic_regression_" in name:
                return method == "logistic_regression"
            if method not in {"svm", "kronos_svm"}:
                return False
            if name.endswith("_svm_gamma"):
                editor = self._editors.get(prefix + "_grid_svm_kernels")
                return isinstance(editor, QLineEdit) and "RBF" in [
                    part.strip() for part in editor.text().upper().split(",")
                ]
            return True
        if self._form.context.get("development_cv_policy"):
            rf_main = (
                "_rtrees_max_depth",
                "_rtrees_min_sample_count",
                "_rtrees_active_var_count",
                "_rtrees_max_trees",
            )
            svm_main = ("_svm_kernel", "_svm_c", "_svm_gamma")
            if name.endswith(rf_main):
                return (
                    page_visible and core_visible
                    and method in {"rtrees", "kronos_rtrees"}
                    and not (tuning == "grouped_cv")
                )
            if name.endswith(svm_main):
                return (
                    page_visible and core_visible
                    and method in {"svm", "kronos_svm"}
                    and not (tuning == "grouped_cv")
                    and (not name.endswith("_svm_gamma") or choice("_svm_kernel") == "RBF")
                )
        if self._form.context.get("development_cv_policy") and name.endswith(
            (
                "_maximum_samples",
                "_reweight",
                "_boundary_strategy",
            )
        ):
            visible &= advanced
        if self._stage_pages is None and (primary or self._form.context.get("secondary")) and name.endswith("_pca_normalize"):
            pca = self._editors.get(prefix + "_pca_variance")
            try:
                visible &= float(pca.text()) > 0
            except (AttributeError, ValueError):
                visible = False
        if name.endswith(("_cv_folds", "_cv_graph_widths")):
            if name.endswith("_cv_graph_widths"):
                return visible and tuning == "grouped_cv" and kronos and core_visible
            return page_visible and tuning == "grouped_cv" and core_visible
        if "_patient_" in name:
            return visible and assignment == "manual"
        if name.endswith("_validation_subjects"):
            return visible and assignment == "automatic" and intent != "training_only"
        if name.endswith("_test_subjects"):
            return visible and (allocation is not None or assignment == "automatic") and intent == "independent_performance"
        if "_image_" in name and name.startswith("training_"):
            if kronos:
                return False
            return visible
        if "_kronos_" in name:
            visible &= kronos
            if name.endswith("_kronos_width"):
                widths = self._editors.get(prefix + "_cv_graph_widths")
                use_grid = kronos and tuning == "grouped_cv" and widths is not None and bool(widths.text().strip())
                self._editors[name].setDisabled(use_grid)
            if name.endswith(
                (
                    "_width",
                    "_epochs",
                    "_batch",
                    "_learning_rate",
                    "_weight_decay",
                    "_threads",
                    "_memory_mib",
                    "_divisor",
                )
            ):
                visible &= advanced
            if "_additional_" in name:
                visible &= self._kronos_additional_marker_visible(name)
            if "_panel_" in name and (self._form.context.get("primary") or self._form.context.get("secondary")):
                channel_index = int(re.search(r"_panel_(\d+)_", name).group(1))
                candidate = list(sorted(self._form.context["kronos_panels"]))[int(match.group(1))]
                channel = self._form.context["kronos_panels"][candidate][channel_index][
                    "channel_id"
                ]
                kind = "primary" if self._form.context.get("primary") else "secondary"
                editor = self._editors.get(f"{kind}_{match.group(1)}_channel_ids")
                selected = editor.value() if isinstance(editor, ClassifierChannels) else ()
                visible &= channel in selected
            return visible
        if kronos and name.endswith(
            (
                "_maximum_samples",
                "_reweight",
                "_pca_variance",
                "_pca_normalize",
                "_boundary_strategy",
                "_boundary_thickness",
                "_boundary_label",
            )
        ):
            return False
        return visible

    def _secondary_candidate_changed(self, index):
        preview = self._editors.get("annotation_preview_candidate")
        if preview is not None:
            candidate = sorted(self._form.context["resume_context"]["candidate_entry_ids"])[index]
            preview.setCurrentIndex(preview.findData(candidate))
        if self._reference_plan is not None:
            self._reference_plan.selected_index = index
            self._reference_plan.refresh()
        self._secondary_candidate_status()

    def _secondary_candidate_status(self):
        if self._form is None or not self._form.context.get("secondary"):
            return
        pages = self._stage_pages
        for index in range(len(pages.candidate_labels)):
            channels = self._editors.get(f"secondary_{index}_channel_ids")
            count = len(channels.value()) if channels is not None else 0
            inputs = "Choose channels"
            if count:
                inputs = f"{count} channel" + ("s" if count != 1 else "") + " selected"
            if pages.candidate_issues:
                error = self._training_search_errors.get(index)
                message = "" if count else "Select image channels."
                if error:
                    message = error[1]
                method = self._editors[f"training_{index}_model_method"]
                pages.classifier_overview.set_state(index, method=method.currentText(), channel_count=count, issue=message)
            if index == pages.candidate_index:
                pages.candidate_status.setText(inputs)

    def _apply_secondary_visibility(self, *_args: object) -> None:
        if self._form is None or not self._form.context.get("secondary"):
            return
        advanced = self._secondary_advanced.isChecked()
        for name, (label, field) in self._secondary_rows.items():
            match = re.match(r"training_(\d+)_", name)
            prefix = f"training_{match.group(1)}" if match is not None else None
            method_editor = self._editors.get(f"{prefix}_model_method") if prefix else None
            kernel_editor = self._editors.get(f"{prefix}_svm_kernel") if prefix else None
            strategy_editor = self._editors.get(f"{prefix}_boundary_strategy") if prefix else None
            method = (
                method_editor.currentData() if isinstance(method_editor, QComboBox) else "rtrees"
            )
            kernel = (
                kernel_editor.currentData() if isinstance(kernel_editor, QComboBox) else "LINEAR"
            )
            visible = True
            if name.endswith("_seed") or "_output_label_" in name:
                visible = advanced
            elif "_rtrees_" in name:
                visible = method in {"rtrees", "kronos_rtrees"} and advanced
            elif "_svm_" in name:
                visible = method in {"svm", "kronos_svm"}
                if name.endswith(("_svm_max_iterations", "_svm_termination_epsilon")):
                    visible &= advanced
                elif name.endswith("_svm_gamma"):
                    visible &= kernel == "RBF"
            elif name.endswith(("_boundary_thickness", "_boundary_label")):
                visible = (
                    isinstance(strategy_editor, QComboBox)
                    and strategy_editor.currentData() == "classify" and advanced
                )
            visible = self._training_control_visible(name, visible, advanced=advanced)
            if name == "annotation_preview_candidate":
                visible = False  # Opening a classifier selects its annotation labels.
            label.setVisible(visible)
            field.setVisible(visible)
        self._sync_training_pages()
        self._secondary_candidate_status()

    def _apply_fit_review_visibility(self, *_args: object) -> None:
        if (
            self._form is None
            or self._form.context.get("adapter") != "training_fit_review"
            or not self._fit_review_rows
        ):
            return
        advanced = self._fit_review_advanced.isChecked()
        any_selected = False
        for name, (label, field) in self._fit_review_rows.items():
            match = re.match(r"settings_(\d+)_", name)
            if match is None:
                continue
            prefix = f"settings_{match.group(1)}"
            selector = self._editors.get(f"{prefix}_selected")
            selected = isinstance(selector, QCheckBox) and selector.isChecked()
            any_selected |= selected
            visible = name.endswith("_selected") or selected
            if visible and not name.endswith("_selected"):
                if name.endswith(
                    (
                        "_rtrees_min_sample_count",
                        "_rtrees_active_var_count",
                        "_rtrees_termination_epsilon",
                        "_rtrees_calculate_variable_importance",
                        "_svm_max_iterations",
                        "_svm_termination_epsilon",
                    )
                ):
                    visible = advanced
                elif name.endswith("_svm_gamma"):
                    kernel = self._editors.get(f"{prefix}_svm_kernel")
                    visible = isinstance(kernel, QComboBox) and kernel.currentData() == "RBF"
            label.setVisible(visible)
            field.setVisible(visible)
        self._fit_review_advanced.setVisible(any_selected)

    def _describe_saved_classifier(self) -> None:
        """Show the accepted labels and image channels beside the selected package."""
        label = self.findChild(QLabel, "saved-classifier-description")
        if label is None:
            return
        from pathlib import Path
        from mif_pipeline.orchestration.batch_training.accepted_model_reuse import read_native_classifier_export
        path = self._editors["saved_classifier_export"].text().strip()
        if not path:
            label.clear()
            return
        try:
            material = read_native_classifier_export(Path(path))
            settings = material.primary_settings
            if settings is None:
                raise ValueError("Choose an accepted primary tissue classifier.")
            names = {item["channel_id"]: item.get("name") or item["channel_id"]
                for row in self._form.context["resume_context"]["application_participants"]
                for item in row["channels"]}
            classes = "; ".join(item.display_name for item in settings.primary_classes)
            channels = "; ".join(names.get(channel, channel)
                                 for channel in settings.primary_classifier.channel_roles.values())
            label.setText(f"Classes: {classes}\nImage channels: {channels}")
        except (OSError, ValueError) as exc:
            label.setText(str(exc))

    def _apply_primary_visibility(self, *_args: object) -> None:
        if self._form is None or not self._form.context.get("primary"):
            return
        source = self._editors.get("classifier_source")
        saved = source is not None and source.currentData() == "saved"
        skipped = source is not None and source.currentData() == "skip"
        advanced = self._primary_advanced.isChecked()
        method_editor = self._editors.get("training_0_model_method")
        kernel_editor = self._editors.get("training_0_svm_kernel")
        method = method_editor.currentData() if isinstance(method_editor, QComboBox) else "rtrees"
        kernel = kernel_editor.currentData() if isinstance(kernel_editor, QComboBox) else "LINEAR"
        for name, editor in self._editors.items():
            if isinstance(editor, PrimaryClassCountBounds):
                prefix = name.removesuffix("_class_bounds")
                count = self._editors[prefix + "_class_count"].currentData()
                classes = []
                for row in range(int(count or 0)):
                    class_id = (
                        self._editors[f"{prefix}_class_{row}_id"].text().strip() or f"class-{row + 1}"
                    )
                    label = (
                        self._editors[f"{prefix}_class_{row}_name"].text().strip() or f"Class {row + 1}"
                    )
                    classes.append((class_id, label))
                editor.set_classes(classes)
        for name, (label, field) in self._primary_rows.items():
            stage_one = "_class_" in name or name.endswith(("_channel_ids", "_ignore_policy"))
            technical = (
                name.endswith("_id")
                or name.endswith("_classifier_id")
                or name.endswith("_training_regions")
                or name.endswith("_seed")
                or name.endswith("_review_policy")
                or "_rtrees_" in name
                or name.endswith(("_svm_max_iterations", "_svm_termination_epsilon"))
            )
            visible = True if self._stage_pages is not None else stage_one == (self._primary_step == 0)
            if technical:
                visible &= advanced
            if self._form.context.get("development_cv_policy") and (
                name.endswith("_classifier_id") or re.fullmatch(r"primary_\d+_class_\d+_id", name)
            ):
                visible = False
            if "_rtrees_" in name:
                visible &= method in {"rtrees", "kronos_rtrees"}
            elif "_svm_" in name:
                visible &= method in {"svm", "kronos_svm"}
                if name.endswith("_svm_gamma"):
                    visible &= kernel == "RBF"
            visible = self._training_control_visible(name, visible, advanced=advanced)
            if name == "classifier_source":
                visible = True
            elif name == "saved_classifier_export":
                visible = saved
            elif saved or skipped:
                visible = False
            label.setVisible(visible)
            field.setVisible(visible)
        self._sync_training_pages()

    def submission_for(self, action: str) -> dict[str, object] | None:
        form = self._form
        if form is None:
            return None
        if (action == 'accept' and self._af_control_review_panel is not None
                and (not self._af_control_band_step or not self._af_control_review_panel.can_continue)):
            return None
        if action == 'accept' and self._target_af_groups_panel is not None and not self._target_af_groups_panel.can_continue:
            return None
        self._clear_field_errors()
        self._form_error_details_toggle.setChecked(False)
        self._form_error_details_toggle.hide()
        self._form_error_details.clear()
        if not self._validate_current_fields(action=action):
            return None
        try:
            if form.context.get('adapter') == 'marker_setup':
                from mif_pipeline.gui.marker_setup import submit_marker_setup_form
                return submit_marker_setup_form(form, action, self._raw_values())
            if form.context.get('adapter') == 'marker_gates':
                from mif_pipeline.gui.presenters.marker_gates import submit_marker_gate
                return submit_marker_gate(form, action, self._raw_values())
            if form.context.get("adapter") == "secondary_definition":
                from mif_pipeline.gui.secondary_setup import submit_secondary_setup_form
                return submit_secondary_setup_form(form, action, self._raw_values())
            if form.context.get("adapter") == "tissue_setup":
                from mif_pipeline.gui.tissue_setup import submit_tissue_setup_form, decode_tissue_setup_values

                decode = decode_tissue_setup_values if form.context.get("saved_tissue_settings") else submit_tissue_setup_form
                return decode(form, action, self._raw_values())
            if form.context.get("adapter") == "segmentation_options":
                from mif_pipeline.gui.segmentation_setup import submit_segmentation_options_form
                return submit_segmentation_options_form(form, action, self._raw_values())
            if form.context.get("adapter") == "segmentation_setup":
                from mif_pipeline.gui.segmentation_setup import submit_segmentation_setup_form

                return submit_segmentation_setup_form(form, action, self._raw_values())
            if form.context.get("adapter") == "measurement_setup":
                from mif_pipeline.gui.measurement_setup import submit_measurement_setup_form

                return submit_measurement_setup_form(form, action, self._raw_values())
            return submit_form_values(form, action, self._raw_values())
        except (TypeError, ValueError) as exc:
            self._present_error(str(exc), action)
            return None

    def _submit(self, action: str, *, continue_after_save: bool = True) -> None:
        if self._advance_af_control_review(action):
            return
        self.last_submission_continuation = continue_after_save
        submission = self.submission_for(action)
        if submission is not None:
            self.submission_ready.emit(action, submission)

    def _advance_af_control_review(self, action):
        """Handle the local review step in both standalone and batch forms."""
        if (action == 'accept' and self._af_control_review_panel is not None
                and not self._af_control_review_panel.can_continue):
            return True
        if (action == 'accept' and self._af_control_review_panel is not None
                and not self._af_control_band_step):
            self._af_control_reviewed = True
            self._set_af_control_step(True)
            return True
        return False

    def _set_af_control_step(self, bands):
        """Review the real control spectrum before exposing extraction choices."""
        self._af_control_band_step = bands
        self._af_control_review_panel.setVisible(not bands)
        self._reference_band_panel.setVisible(bands)
        self._af_review_back.setVisible(bands or self._af_has_region_back)
        matched_negative = self._form.context['af_control_set_review'].get('review_kind') == 'matched_negative'
        self._af_review_back.setText(('Back: negative control spectrum' if matched_negative else 'Back: AF control spectrum')
                                    if bands else 'Back: reference regions')
        self._heading.setText(self._form.title if bands else
            'Review negative control spectra' if matched_negative else 'Review autofluorescence control spectra')
        self._message.setText(self._form.message if bands else
            'Inspect the matched negative-control spectrum, then choose channels for fluorophore extraction.'
            if matched_negative else 'Compare the combined control spectrum with its regional shapes. Return to the regions to change a control set.')
        action = self._af_continue_button
        if action is not None:
            action.setText('Extract reference spectra' if bands else 'Next: choose channels')
            action.setEnabled(self._af_control_review_panel.can_continue)
        self.page_changed.emit()

    def revisit_marker(self, marker: str) -> None:
        """Open saved preparation without changing the current request or its draft."""
        from .marker_preparation import open_marker_preparation
        open_marker_preparation(self, marker)

    def _tissue_source_mode_changed(self, _index: int) -> None:
        self._tissue_source_ready = False
        self._tissue_observation_pending = False
        self._tissue_return_to_regions = False
        self.tissue_source_invalidated.emit()
        self._apply_tissue_visibility()

    def tissue_authoring_setup(self):
        """Bind the detector input to this image's offered channel and stored level."""
        from mif_pipeline.gui.tissue_flow import validate_detail
        from mif_pipeline.gui.pixel_image_features import image_features_from_values

        values = self._raw_values()
        validate_detail(self._form, values)
        channels = self._form.context["tissue_image_context"]["channels"]
        return dict(channel_index=next(i for i, row in enumerate(channels)
                                       if row["channel_id"] == values["reference_channel_id"]),
                    resolution_index=int(values["native_resolution_index"]),
                    image_features=image_features_from_values(values, "tissue"))

    def _tissue_next(self) -> None:
        """Advance the tissue setup after validating the inputs required by the current page.

        The detector page checks the selected existing/imported detector and any pending
        file observation. The image-detail page checks the channel and native-resolution
        selection. Only a valid page transition advances
        the visible Tissue Regions step; entered values remain available when a field needs
        correction."""

        errors = self._validate_current_fields(present=False)
        if self._tissue_step == 1:
            from mif_pipeline.gui.tissue_flow import validate_detail
            if not self._present_field_errors(errors):
                return
            try:
                validate_detail(self._form, self._raw_values())
            except (ValueError, TypeError) as error:
                self._present_error(str(error), "select_tissue_detector")
                return
            for error in self._errors.values():
                error.clear()
                error.hide()
            self._form_error_summary.hide()
            self._set_tissue_step(2)
            return
        if self._tissue_step != 0:
            return
        mode = self._raw_values().get("detector_source")
        if mode in {"rf", "svm"}:
            pass
        elif mode == "saved":
            selected = self._raw_values().get("saved_classifier_name")
            if not selected:
                errors["saved_classifier_name"] = "Select a saved detector."
        elif mode == "import":
            if self._tissue_observation_pending or not self._tissue_source_ready:
                errors["source_path"] = "Choose the detector file and wait for its check to finish."
            if not self._raw_values().get("import_classifier_name", "").strip():
                errors["import_classifier_name"] = "Enter a name for this detector."
        else:
            errors["detector_source"] = "Choose a detector source."
        if not self._present_field_errors(errors):
            return
        for error in self._errors.values():
            error.clear()
            error.hide()
        self._set_tissue_step(1)

    def _set_tissue_step(self, step: int) -> None:
        self._tissue_step = step
        self._apply_tissue_visibility()

    def _apply_tissue_visibility(self, *_args: object) -> None:
        if self._form is None or self._form.context.get("adapter") != "tissue_setup":
            return
        from mif_pipeline.gui.tissue_flow import PAGE_TITLES, PAGE_HELP, show_field

        self._heading.hide()
        self._message.hide()
        self._setup_heading.setText(PAGE_TITLES[self._tissue_step])
        self._tissue_hint.setText(PAGE_HELP[self._tissue_step])
        values = self._raw_values()
        mode = values.get("detector_source")
        for name, (label, field) in self._tissue_rows.items():
            visible = show_field(name, self._tissue_step, values)
            label.setVisible(visible)
            field.setVisible(visible)
        self._tissue_back.setVisible(self._tissue_step > 0)
        preparing = mode in {"rf", "svm"}
        creating = mode == "threshold" or preparing
        self._tissue_create.setVisible(
            (self._tissue_step == 0 and mode == "threshold") or (self._tissue_step == 1 and preparing)
        )
        self._tissue_create.setText("Annotate and fit in QuPath" if preparing else "Create detector in QuPath")
        self._tissue_next_button.setVisible(
            (self._tissue_step == 0 and mode != "threshold") or (self._tissue_step == 1 and not creating)
        )
        if self._tissue_step == 1 and preparing:
            self._tissue_hint.setText("Choose the detection channel, image resolution and features. Then label Tissue and Background regions in QuPath.")
        elif self._tissue_step == 1 and mode in {"saved", "import"}:
            self._tissue_hint.setText("These image choices must match the channel and resolution stored in the detector. Create a new detector in QuPath to change them.")
        self._refresh_pixel_feature_summaries()
        self._tissue_next_button.setText(
            "Next: image detail" if self._tissue_step == 0 else "Next: tissue regions"
        )
        self._tissue_next_button.setEnabled(not self._tissue_observation_pending)
        for action, button in self._tissue_buttons.items():
            button.setVisible(
                (action == "refresh_tissue_detectors" and self._tissue_step == 0)
                or (
                    action == "select_tissue_detector"
                    and self._tissue_step == 2
                    and mode == "saved"
                )
                or (
                    action == "import_tissue_detector"
                    and self._tissue_step == 2
                    and mode == "import"
                )
            )
            button.setEnabled(
                not self._tissue_observation_pending
                and (action != "import_tissue_detector" or self._tissue_source_ready)
            )
        self.updateGeometry()

    def offer_tissue_authoring_result(self, result):
        from mif_pipeline.orchestration.tissue_authoring_draft import TissueAuthoringResult

        if (
            type(result) is not TissueAuthoringResult
            or self._form is None
            or self._form.context.get("adapter") != "tissue_setup"
        ):
            raise ValueError(
                "Open the current tissue-detector setup before choosing the saved draft."
            )
        controls = {control.name: control for control in self._form.controls}
        channels = controls["reference_channel_id"].choices
        levels = controls["native_resolution_index"].choices
        if result.channel_index >= len(channels) or str(result.resolution_index) not in levels:
            raise ValueError(
                "The detector draft uses image settings that are unavailable in this review."
            )
        context = self._form.context["tissue_image_context"]
        resources = {
            row["classifier_name"]: row["sha256"] for row in context["available_classifiers"]
        }
        name = result.classifier_name + ".json"
        matches = sorted(
            key for key, checksum in resources.items() if checksum == result.classifier_sha256
        )
        same = name if name in matches else matches[0] if matches else None
        for field, value in (
            ("detector_source", "saved" if same else "import"),
            ("reference_channel_id", channels[result.channel_index]),
            ("native_level_selection_mode", "configured_index"),
            ("native_resolution_index", str(result.resolution_index)),
        ):
            editor = self._editors[field]
            index = editor.findData(value)
            if index < 0:
                raise ValueError("The detector draft does not match the current image settings.")
            editor.setCurrentIndex(index)
        if same is not None:
            selected = self._editors["saved_classifier_name"]
            selected.setCurrentIndex(selected.findData(same))
            self._tissue_source_ready = False
            self._tissue_observation_pending = False
            self.tissue_source_invalidated.emit()
            self._tissue_step = 2
            self._apply_tissue_visibility()
            return
        self._editors["source_path"].setText(str(result.classifier_path))
        # A published detector remains immutable. Repeated draft edits keep the
        # same name; only a different final import receives a versioned resource.
        if name in controls["saved_classifier_name"].choices:
            name = result.classifier_name + "-" + result.classifier_sha256[:8] + ".json"
        self._editors["import_classifier_name"].setText(name)
        self._tissue_source_ready = False
        self._tissue_observation_pending = True
        # Every authored detector returns its channel and resolution, including
        # the threshold route. Carry those saved choices to the region settings.
        self._tissue_return_to_regions = True
        self.tissue_source_invalidated.emit()
        self._apply_tissue_visibility()
        self.tissue_source_selected.emit(str(result.classifier_path))

    def begin_tissue_source_check(self) -> None:
        """Block import until the current local detector has been checked."""
        self._tissue_source_ready = False
        self._tissue_observation_pending = True
        self._apply_tissue_visibility()

    def set_tissue_observation_result(
        self,
        *,
        success: bool,
        message: str = "",
    ) -> None:
        if self._form is None or self._form.context.get("adapter") != "tissue_setup":
            return
        self._tissue_observation_pending = False
        self._tissue_source_ready = success
        if self._tissue_return_to_regions:
            self._tissue_return_to_regions = False
            self._tissue_step = 2 if success else 0
        error = self._errors.get("source_path")
        if error is not None:
            error.setText("" if success else message)
            error.setVisible(not success and bool(message))
        self._apply_tissue_visibility()

    def capture_draft_values(self) -> dict[str, object]:
        values = self._raw_values(draft=True)
        visit = getattr(self, '_marker_preparation_visit', None)
        if visit is not None:
            values['__marker_preparation'] = {'marker': visit.marker, 'values': visit.editor.capture_draft_values()}
        values["__presentation_state"] = {
            "cycle_review_open": (self._editors["channel_cycle_values"].reviewing
                if "channel_cycle_values" in self._editors else False),
            "nuclear_stain_reviewed": (
                self._editors["nuclear_stain"].reviewed
                if self._form.context.get("adapter") == "batch_stage1" else None
            ),
            "spectral_table_opened": self._spectral_opened,
            "page": self._stage_pages.current_key if self._stage_pages is not None else None,
            "kronos_markers": self._stage_pages.capture_marker_state() if self._stage_pages is not None else {},
            "candidate_index": self._stage_pages.candidate_index if self._stage_pages is not None else None,
            "steps": {
                name: value
                for name, value in vars(self).items()
                if name.endswith("_step") and type(value) is int
            },
            "expanded": {
                toggle.objectName(): toggle.isChecked()
                for toggle in self.findChildren(QToolButton)
                if "advanced" in toggle.objectName()
            },
        }
        if self._stage_pages is not None and self._stage_pages.feature_preparation is not None:
            values["__presentation_state"]["supervised_representation"] = self._stage_pages.feature_preparation.capture()
        if self._stage_pages is not None:
            for key, panel in self._stage_pages.projection_pages.items():
                values["__presentation_state"][key] = panel.capture()
        return values

    def restore_values(self, values: Mapping[str, object]) -> None:
        """Restore a draft belonging to this exact form without submitting it."""
        from PySide6.QtCore import QSignalBlocker

        if self._target_af_groups_panel is not None and 'target_af_groups' in values:
            self._target_af_groups_panel.set_value(values['target_af_groups'])
        if self._reference_band_panel is not None:
            self._reference_band_panel.set_values(values)
        if self._af_control_review_panel is not None and 'af_control_set_review_sha256s' in values:
            expected = tuple(row['evidence_sha256'] for row in self._form.context['af_control_set_review']['sets'])
            self._af_control_reviewed = (self._af_control_review_panel.can_continue and bool(expected)
                and tuple(values['af_control_set_review_sha256s'] or ()) == expected)
            self._set_af_control_step(self._af_control_reviewed)
        if self._reference_extraction_panel is not None:
            self._reference_extraction_panel.set_values(values)
        if self._reference_regions_panel is not None:
            self._reference_regions_panel.set_values(values)

        if self._form.context.get("adapter") == "batch_stage1" and "channel_cycle_values" in values:
            table = self._editors.get("component_values")
            if table is not None:
                blocker = QSignalBlocker(table)
                table.set_cycles([row["cycle"] for row in values["channel_cycle_values"] if row["cycle"] is not None])
                del blocker

        if ("excluded_roi_ids" in values and self._form.context.get("region_review") is not None
                and any(control.name == "excluded_roi_ids" for control in self._form.controls)):
            self._region_review.restore_excluded_roi_ids(values["excluded_roi_ids"])

        for name, editor in self._editors.items():
            if name not in values:
                continue
            if self._reference_regions_panel is not None and self._reference_regions_panel.owns_control(name):
                continue
            value = values[name]
            if self._form.context.get("adapter") == "marker_setup":
                if name.endswith("_method") and value == "gmm" and values.get(name.removesuffix("method") + "gaussian_variance") == "unshared":
                    value = "gmm_separate"
                if name.endswith(("_lower_min", "_lower_max", "_upper_min", "_upper_max")) and values.get(name.rsplit("_", 2)[0] + "_constrain") is False:
                    value = ""
            if name.endswith("_nimbus_distill") and isinstance(editor, QComboBox) and type(value) is bool:
                value = "intensity_review" if value else "score_cutoff"
            blocker = QSignalBlocker(editor)
            if name in {"phenotyping_population", "selected_marker_ids"}:
                editor.set_value(value)
            elif isinstance(editor, (IntensityMeasurements, ComponentAssignmentTable, ChannelCycleEditor, AcquisitionOrganizationEditor, ClassifierChannels, PixelFeatureEditor, ClusteringFeatures, SupervisedFeatures)):
                editor.set_value(value)
            elif name == "nuclear_stain" and self._form.context.get("adapter") == "batch_stage1":
                editor.restore_choice(
                    value,
                    reviewed=values.get("__presentation_state", {}).get(
                        "nuclear_stain_reviewed", self._form.context.get("nuclear_stain_reviewed", False)
                    ),
                )
            elif isinstance(editor, KronosMarkerAssignment):
                editor.set_value(value)
            elif isinstance(editor, QComboBox):
                index = editor.findData(value)
                editor.setCurrentIndex(max(0, index))
                if self._form.context.get("adapter") == "marker_setup" and name.endswith("_method"):
                    self._marker_workbench._switch_limit_family(name.removesuffix("method"))
            elif isinstance(editor, QCheckBox) and type(value) is bool:
                editor.setChecked(value)
            elif isinstance(editor, QLineEdit):
                if re.fullmatch(r'training_\d+_resolution', name):
                    editor.setProperty('kronosSamplingRestored', True)
                editor.setText("" if value is None else str(value))
                automatic = self.findChild(QComboBox, "automatic-" + name)
                if automatic is not None:
                    automatic.sync()
                sampling = self.findChild(QComboBox, "training-resolution-" + name)
                if sampling is not None:
                    try:
                        preset = sampling.findData(float(value))
                    except (ValueError, TypeError):
                        preset = -1
                    with QSignalBlocker(sampling):
                        sampling.setCurrentIndex(preset if preset >= 0 else sampling.count() - 1)
                    editor.setVisible(preset < 0)
            elif isinstance(editor, PrimaryClassCountBounds) and isinstance(value, Mapping):
                if "rows" in value and isinstance(value.get("configured"), list):
                    editor.restore_draft(value)
                else:
                    editor.set_value(value)
            elif isinstance(editor, ObjectEditor) and type(value) is ObjectEditorDraft:
                editor.restore_draft(value)
            elif isinstance(editor, (ObjectEditor, SecondaryDefinitionEditor)) and isinstance(value, Mapping):
                editor.set_value(value)
            elif isinstance(editor, QTableView) and isinstance(
                editor.model(), (_StructuredValueModel, ClusterDecisionModel)
            ):
                editor.model().restore_value(value)
            elif isinstance(editor, QPlainTextEdit):
                editor.setPlainText(str(value or ""))
            del blocker
        for chooser in self._tissue_classifier_choices.values():
            chooser.sync()
        if self._form.context.get("primary") and "classifier_source" in self._editors:
            from .classifier_source_choices import ClassifierSourceChoices
            self.findChild(ClassifierSourceChoices).sync()
        if self._form.context.get("adapter") in {"segmentation_setup", "segmentation_options"}:
            # Restored channels belong to the restored method, not to a new
            # interactive method choice. Seed this before restoring disclosures.
            method = self._editors["method"].currentData()
            self._segmentation_previous_method = method
            self._segmentation_channel_selections[method] = [
                self._editors[f"channel_{index}"].currentData()
                for index in (1, 2, 3) if isinstance(self._editors[f"channel_{index}"], QComboBox)
            ]
            from .segmentation_method_choices import SegmentationMethodChoices
            self.findChild(SegmentationMethodChoices).sync()
        if self._form is not None and self._form.context.get("adapter") in {"marker_setup", "marker_combinations"}:
            if self._marker_combinations is not None:
                current = self._marker_combinations.value()
                self._marker_combinations.set_value(
                    values.get("marker_composite_names", current["marker_composite_names"]),
                    values.get("marker_composite_states", current["marker_composite_states"]))
        from ..method_guidance import method_guidance_points
        from .method_guidance import MethodGuidanceCard
        for name, editor in self._editors.items():
            if isinstance(editor, QComboBox):
                explanation = self.findChild(MethodGuidanceCard, "choice-help-" + name)
                if explanation is not None:
                    explanation.set_guidance(editor.currentText(), method_guidance_points(name, editor.currentData()))
        presentation = values.get("__presentation_state", {})
        if self._stage_pages is not None:
            for key, panel in self._stage_pages.projection_pages.items():
                if panel.target_name in values:
                    panel.restore(presentation.get(key))
        if self._stage_pages is not None and self._stage_pages.feature_preparation is not None:
            panel = self._stage_pages.feature_preparation
            if panel.target_name in values:
                panel.restore(presentation.get("supervised_representation"))
            elif "supervised_0_normalization" in values:
                panel.restore(panel.capture())
        for name, step in presentation.get("steps", {}).items():
            if name.endswith("_step") and name in vars(self) and type(step) is int:
                setattr(self, name, step)
        for toggle in self.findChildren(QToolButton):
            if toggle.objectName() in presentation.get("expanded", {}):
                toggle.setChecked(presentation["expanded"][toggle.objectName()])
        for name in (
            "unmixing",
            "tissue",
            "primary",
            "secondary",
            "supervised",
            "measurement",
            "segmentation",
            "fit_review",
            "marker",
            "clustering",
        ):
            getattr(self, f"_apply_{name}_visibility")()
        if self._stage_pages is not None:
            candidate_index = presentation.get("candidate_index")
            if (self._form.context.get("secondary") and type(candidate_index) is int
                    and 0 <= candidate_index < len(self._form.context["resume_context"]["candidate_entry_ids"])):
                self._stage_pages.select_candidate(candidate_index)
            self._stage_pages.select(presentation.get("page"), validate=False)
            self._stage_pages.restore_marker_state(presentation.get("kronos_markers", {}))
        if self._form is not None and _is_batch_clustering_form(self._form):
            self._set_clustering_step(self._clustering_step)
        if '__marker_preparation' in values:
            from .marker_preparation import open_marker_preparation
            draft = values['__marker_preparation']
            open_marker_preparation(self, draft['marker'])
            self._marker_preparation_visit.editor.restore_values(draft['values'])
        panel = self._editors.get("consensus_roi_review")
        if panel is not None and self._form.context.get("consensus_review"):
            if "consensus_roi_review" in values:
                panel.set_value(values["consensus_roi_review"])
        if self._spectral_table_layout is not None and {"library_locator", "spectral_csv"}.intersection(values):
            self._populate_spectral_table(self._editors["library_locator"].text(), draft=values.get("spectral_csv"))
            self._spectral_opened = presentation.get("spectral_table_opened", True)
            self._set_spectral_page(presentation.get("steps", {}).get("_spectral_step", 1))
        if self._form.context.get("adapter") == "batch_stage1":
            if presentation.get("cycle_review_open") and "channel_cycle_values" in self._editors:
                self._editors["channel_cycle_values"].open_review()
            self._apply_stage1_visibility()
        if self._form.context.get("reason") == "phenotyping_mode_required":
            self._phenotyping_method_choices.sync()
        if self._form.context.get("development_cv_policy"):
            self._refresh_training_search()
        self.updateGeometry()

    def restore_tissue_values(self, values: Mapping[str, object]) -> None:
        """Retain presentation edits after an owner-backed inventory refresh."""
        if self._form is None or self._form.context.get("adapter") != "tissue_setup":
            return
        for name, editor in self._editors.items():
            value = values.get(name)
            if isinstance(editor, QComboBox):
                index = editor.findData(value)
                editor.setCurrentIndex(index if index >= 0 else 0)
            elif isinstance(editor, QCheckBox) and type(value) is bool:
                editor.setChecked(value)
            elif isinstance(editor, QLineEdit) and value is not None:
                editor.setText(str(value))
        self._tissue_source_ready = False
        self._apply_tissue_visibility()

    def _build_editor(self, control: FormControl) -> tuple[QWidget, QWidget]:
        field = QWidget(self)
        layout = QVBoxLayout(field)
        field.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        layout.setContentsMargins(0, 0, 0, 10)
        layout.setSpacing(8)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        if control.name == "selected_marker_ids" and self._form.context.get("adapter") == "marker_setup":
            from .marker_inputs import MarkerInputs
            editor = MarkerInputs(self._form.context["stage_result"]["resume_context"], self._editors, control.value, field)
            layout.addWidget(editor)
            return editor, field
        if control.name == "supervised_0_feature_ids" and self._form.context.get("supervised"):
            layout.setAlignment(Qt.AlignmentFlag.AlignTop)
            editor = SupervisedFeatures(self._form.context["resume_context"]["supervised_options"]["available_features"], control.value, field)
            layout.addWidget(editor)
            return editor, field
        if control.name == "feature_ids" and _is_batch_clustering_form(self._form):
            editor = ClusteringFeatures(self._form.context["resume_context"]["available_features"], control.value, field)
            layout.addWidget(editor)
            return editor, field
        if control.name == "nuclear_stain" and self._form.context.get("adapter") == "batch_stage1":
            from .nuclear_stain import NuclearStainSection
            section = NuclearStainSection(control.value,
                reviewed=self._form.context.get("nuclear_stain_reviewed", False), parent=field)
            layout.addWidget(section)
            return section.choice, field
        if control.name == "phenotyping_population":
            from .marker_gates import PopulationSelectionEditor
            context = self._form.context["resume_context"]
            editor = PopulationSelectionEditor(context["population_options"], control.value, field)
            layout.addWidget(editor)
            return editor, field
        if control.name.endswith("_image_features"):
            editor = PixelFeatureEditor(field)
            editor.set_value(control.value)
            layout.addWidget(editor)
            return editor, field
        if control.name == "acquisition_organization" and self._form.context.get("adapter") == "batch_acquisition_organization":
            editor = AcquisitionOrganizationEditor(self._form.context["organization_owner"], control.value, field)
            layout.addWidget(editor)
            return editor, field
        if control.name == "channel_cycle_values" and self._form.context.get("adapter") == "batch_stage1":
            editor = ChannelCycleEditor(self._form.context["stage1_channel_rows"], control.value,
                self._form.context.get("source_channel_cycles"), field)
            editor.source_series_index = self._form.context["stage_result"]["resume_context"]["service_context"]["source_series_index"]
            layout.addWidget(editor)
            return editor, field
        if control.name == "component_values" and self._form.context.get("adapter") in {"batch_stage1", "stage1_components"}:
            editor = ComponentAssignmentTable(control.value, field,
                af_retained=self._form.context["component_af_retained"],
                cycles=self._form.context.get("component_cycles", ()))
            layout.addWidget(editor)
            return editor, field
        if control.name == "consensus_roi_review" and self._form.context.get("consensus_review"):
            from .consensus_review import ConsensusReviewPanel

            editor = ConsensusReviewPanel(self._form.context["consensus_review"], field)
            layout.addWidget(editor)
            return editor, field
        if re.fullmatch(r"(?:primary|secondary)_\d+_channel_ids|supervised_\d+_kronos_channel_ids", control.name):
            editor = ClassifierChannels(
                control.choices, self._form.context.get("choice_labels", {}).get(control.name, {}),
                control.value, parent=field,
                preserve_order="_kronos_" in control.name,
                presentation='kronos' if "_kronos_" in control.name else 'checklist',
            )
            layout.addWidget(editor)
            return editor, field
        category = re.search(r"_kronos_panel_\d+_additional_(compartment|family)$", control.name)
        if (self._form is not None and self._form.context.get("adapter") == "measurement_setup"
                and control.name.endswith("_statistics")):
            editor = IntensityMeasurements(control.value, parent=field)
            layout.addWidget(editor)
        elif re.search(r"_kronos_panel_\d+_marker$", control.name):
            editor = KronosMarkerAssignment(control.value, field)
            layout.addWidget(editor)
        elif control.kind is ControlKind.CHOICE or category:
            choices = (self._kronos_vocabulary.get("compartments" if category.group(1) == "compartment" else "families", ())
                       if category else control.choices)
            editor = QComboBox(field)
            editor.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            editor.setMinimumContentsLength(12)
            editor.addItem("", None)
            labels = (
                self._form.context.get("choice_labels", {}).get(control.name, {})
                if self._form is not None
                else {}
            )
            for choice in choices:
                title = (f"{choice} × {choice}" if control.name.endswith("_kronos_patch_size")
                         else labels.get(choice, choice_label(control.name, choice)))
                editor.addItem(title, choice)
            if isinstance(control.value, str) and control.value in choices:
                editor.setCurrentIndex(choices.index(control.value) + 1)
            is_spectral_source = (
                control.name == "option_value"
                and self._form is not None
                and self._form.context.get("adapter") == "batch_reference_operation"
                and self._form.context.get("stage_result", {})
                .get("resume_context", {})
                .get("service_context", {})
                .get("operation_state", {})
                .get("checkpoint")
                in {"basis_source", "control_strategy"}
            )
            if control.name == "mode" and self._form.context.get("reason") == "phenotyping_mode_required":
                from .phenotyping_method_choices import PhenotypingMethodChoices
                self._phenotyping_method_choices = PhenotypingMethodChoices(editor, field)
                layout.addWidget(self._phenotyping_method_choices)
            elif is_spectral_source:
                from .reference_choices import SpectralSourceChoices

                editor.hide()
                layout.addWidget(SpectralSourceChoices(editor, field))
            else:
                layout.addWidget(editor)
            if (control.name == "native_resolution_index" and (
                    self._form.context.get("reason") == "tissue_native_level_required"
                    or self._form.context.get("adapter") == "tissue_setup")):
                if (self._form.context.get("reason") == "tissue_native_level_required"
                        and len(choices) == 1):
                    editor.setEnabled(False)
                warning = QLabel("Full resolution uses more memory and may take substantially longer.", field)
                warning.setWordWrap(True)
                warning.setProperty("muted", True)
                warning.setObjectName("tissue-full-resolution-warning")
                warning.setVisible(editor.currentData() == "0")
                editor.currentIndexChanged.connect(
                    lambda _index, target=warning, selected=editor:
                    target.setVisible(selected.currentData() == "0"))
                layout.addWidget(warning)
            if (any(method_guidance(control.name, value) for value in control.choices)
                    and not (control.name == "mode" and self._form.context.get("reason") == "phenotyping_mode_required")
                    and not ((self._form.context.get("primary") or self._form.context.get("secondary")) and control.name.endswith("_model_method"))):
                from .method_guidance import MethodGuidanceCard
                from ..method_guidance import method_guidance_points
                explanation = MethodGuidanceCard(field)
                explanation.setObjectName("choice-help-" + control.name)
                # Wrapped guidance must use the current viewport width. A fixed
                # minimum from the initial narrow layout leaves large blank rows.
                field.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
                layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
                layout.setAlignment(Qt.AlignmentFlag.AlignTop)

                def update_help(_index=0, combo=editor, label=explanation, name=control.name):
                    label.set_guidance(combo.currentText(), method_guidance_points(name, combo.currentData()))

                editor.currentIndexChanged.connect(update_help)
                layout.addWidget(explanation)
                update_help()
        elif control.kind is ControlKind.CHECKBOX:
            editor = QCheckBox(field)
            editor.setChecked(control.value is True)
            layout.addWidget(editor)
        elif control.kind in {
            ControlKind.INTEGER_MAPPING,
            ControlKind.OBJECT_MAPPING,
            ControlKind.OBJECT_SEQUENCE,
        }:
            editor = QTableView(field)
            plain = self._form is not None and control.name in self._form.context.get(
                "plain_text_sequences", ()
            )
            locked = self._form is not None and control.name in self._form.context.get(
                "locked_sequences", ()
            )
            if control.name == "cluster_decisions":
                model = ClusterDecisionModel(control.choices, self._form.context["cluster_counts"],
                                             control.value, editor)
            else:
                model = _StructuredValueModel(
                    control, editor, plain_text=plain, locked=locked,
                    display_labels=self._form.context.get("choice_labels", {}).get(control.name, {}) if self._form else {},
                )
            editor.setModel(model)
            editor.setMaximumHeight(180)
            self._models.append(model)
            if isinstance(model, ClusterDecisionModel):
                from .cluster_review import ClusterReview
                self._cluster_review=ClusterReview(self._form,editor,model,field)
                layout.addWidget(self._cluster_review)
            else:
                layout.addWidget(editor)
            if control.kind is ControlKind.OBJECT_SEQUENCE:
                from PySide6.QtWidgets import QHeaderView

                layout.setAlignment(Qt.AlignmentFlag.AlignTop)
                style_scientific_table(editor)
                editor.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
                if model.columnCount() == 1:
                    editor.horizontalHeader().hide()
                editor.setMinimumHeight(38)
                editor.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
                editor.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
                def resize_sequence(*_args, table=editor):
                    table.resizeRowsToContents()
                    header = 0 if table.horizontalHeader().isHidden() else table.horizontalHeader().sizeHint().height()
                    height = header + sum(table.rowHeight(row) for row in range(table.model().rowCount())) + 2 * table.frameWidth() + 8
                    table.setFixedHeight(min(180, max(38, height)))
                for signal in (model.rowsInserted, model.rowsRemoved, model.modelReset):
                    signal.connect(resize_sequence)
                resize_sequence()
                if not locked:
                    self._add_sequence_controls(control, editor, model, layout)
        elif control.kind is ControlKind.JSON_OBJECT:
            secondary = self._form is not None and self._form.context.get("adapter") == "secondary_definition"
            primary_counts = bool(re.fullmatch(r"primary_\d+_class_bounds", control.name))
            if primary_counts:
                editor = PrimaryClassCountBounds(field)
            elif secondary:
                editor = SecondaryDefinitionEditor(self._form.context["definition_context"], field)
                editor.secondary_marker_requested.connect(self.secondary_marker_requested)
            else:
                editor = ObjectEditor(field)
            initial = {} if control.value is None else control.value
            editor.set_value(initial)
            if not secondary and not primary_counts:
                editor.setMinimumHeight(210)
            layout.addWidget(editor)
        elif (control.name == "rationale"
                and self._form.context.get("reason") in {
                    "semantic_region_decision_required", "secondary_structure_selection_required"}):
            editor = QPlainTextEdit(field)
            editor.setPlainText("" if control.value is None else str(control.value))
            editor.setTabChangesFocus(True)
            editor.setFixedHeight(editor.fontMetrics().lineSpacing() * 4 + 28)
            layout.addWidget(editor)
        else:
            row = QWidget(field)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            editor = QLineEdit(row)
            editor.setText("" if control.value is None else str(control.value))
            if ((self._form.context.get("primary") or self._form.context.get("secondary"))
                    and re.fullmatch(r"training_\d+_resolution", control.name)):
                sampling = QComboBox(row)
                sampling.setObjectName("training-resolution-" + control.name)
                sampling.setAccessibleName("Training pixel size presets")
                for value in (0.5, 1.0, 2.0, 4.0, 8.0, 16.0):
                    sampling.addItem(f"{value:g} µm/pixel", value)
                sampling.addItem("Custom…", None)
                editor.setPlaceholderText("µm/pixel")
                try:
                    selected = sampling.findData(float(control.value))
                except (ValueError, TypeError):
                    selected = -1
                sampling.setCurrentIndex(selected if selected >= 0 else sampling.count() - 1)

                def select_sampling(_index, choice=sampling, target=editor):
                    value = choice.currentData()
                    target.setVisible(value is None)
                    if value is not None:
                        target.setText(f"{value:g}")
                    else:
                        target.setFocus()
                        target.selectAll()

                sampling.currentIndexChanged.connect(select_sampling)

                def edit_sampling(_text, choice=sampling):
                    with QSignalBlocker(choice):
                        choice.setCurrentIndex(choice.count() - 1)

                editor.textEdited.connect(edit_sampling)
                row_layout.addWidget(sampling)
                editor.setVisible(sampling.currentData() is None)
            row_layout.addWidget(editor)
            if control.kind is ControlKind.LOCAL_FILE:
                if self._form is not None and self._form.context.get("adapter") == "tissue_setup":
                    editor.setReadOnly(True)
                browse = ContentSizedButton(
                    (
                        "Browse detector…"
                        if control.name == "source_path"
                        and self._form.context.get("adapter") == "tissue_setup"
                        else "Import library file…"
                        if control.name == "library_locator" and self._form.context.get("spectral_input")
                        else "Browse…"
                    ),
                    row,
                )
                browse.setObjectName(f"{self.objectName()}-browse-{control.name}")
                browse.setAccessibleName(f"Browse for {control.label}")
                browse.clicked.connect(
                    lambda _checked=False, selected=control, target=editor: self._browse(
                        selected, target
                    )
                )
                row_layout.addWidget(browse)
            layout.addWidget(row)
            if control.name == "library_locator" and self._form.context.get("spectral_input"):
                editor.setReadOnly(True)
                editor.hide()
                layout.setSpacing(20)
                layout.setAlignment(Qt.AlignmentFlag.AlignTop)
                self._spectral_source_row = row
                row.setObjectName("spectral-source-toolbar")
                self._spectral_source_name = QLabel(row)
                self._spectral_source_name.setTextFormat(Qt.TextFormat.PlainText)
                self._spectral_source_name.setWordWrap(True)
                self._spectral_source_name.setMinimumWidth(0)
                self._spectral_source_name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum)
                self._spectral_source_name.setProperty("fieldCaption", True)
                row_layout.insertWidget(0, self._spectral_source_name, 1)
                browse.setText("Replace file…")
                self._spectral_choices = QWidget(field)
                choice_content = QVBoxLayout(self._spectral_choices)
                choice_content.setContentsMargins(0, 0, 0, 0)
                choice_content.setSpacing(0)
                for title, description, text, name, callback in (
                    ("Import a library or CSV", "Saved libraries (.miflib.zip or JSON) retain their recorded imaging settings. CSV values can be edited.",
                     "Choose file…", "spectral-import-file", lambda: self._browse(control, editor)),
                    ("Enter reference values", "Use a table matched to this image’s acquired bands and components.",
                     "Enter spectra", "spectral-enter-values", self._enter_spectral_table),
                ):
                    source = QWidget(self._spectral_choices)
                    content = QGridLayout(source)
                    content.setContentsMargins(0, 18, 0, 18)
                    content.setHorizontalSpacing(28)
                    content.setVerticalSpacing(8)
                    heading = QLabel(title, source)
                    heading.setProperty("panelHeading", True)
                    content.addWidget(heading, 0, 0)
                    description_label = QLabel(description, source)
                    description_label.setWordWrap(True)
                    description_label.setProperty("choiceHelp", True)
                    content.addWidget(description_label, 1, 0)
                    action = ContentSizedButton(text, source)
                    action.setObjectName(name)
                    action.setProperty("actionRole", "primary" if name == "spectral-import-file" else "secondary")
                    action.clicked.connect(callback)
                    content.addWidget(action, 0, 1, 2, 1, Qt.AlignmentFlag.AlignVCenter)
                    content.setColumnStretch(0, 1)
                    self._spectral_source_actions.append((content, action))
                    choice_content.addWidget(source)
                    separator = QFrame(self._spectral_choices)
                    separator.setFrameShape(QFrame.Shape.HLine)
                    separator.setStyleSheet("background: #dce4e4; border: none; min-height: 1px; max-height: 1px;")
                    choice_content.addWidget(separator)
                inventory = self._form.context["spectral_input"]
                axes = QWidget(self._spectral_choices)
                axis_layout = QVBoxLayout(axes)
                axis_layout.setContentsMargins(0, 20, 0, 8)
                axis_layout.setSpacing(8)
                for title, names in (("Acquired bands · table rows", inventory["band_names"]),
                                     ("Components · table columns", inventory["component_names"])):
                    caption = QLabel(f"{title} ({len(names)})", axes)
                    caption.setProperty("fieldCaption", True)
                    axis_layout.addWidget(caption)
                    names_label = QLabel(", ".join(names) if names else "Read from the imported table", axes)
                    names_label.setTextFormat(Qt.TextFormat.PlainText)
                    names_label.setWordWrap(True)
                    axis_layout.addWidget(names_label)
                choice_content.addWidget(axes)
                layout.insertWidget(0, self._spectral_choices)
                self._spectral_editor_panel = QFrame(field)
                self._spectral_editor_panel.setObjectName("spectral-table-panel")
                self._spectral_table_layout = QVBoxLayout(self._spectral_editor_panel)
                self._spectral_table_layout.setContentsMargins(0, 0, 0, 0)
                self._spectral_table_layout.setSpacing(12)
                self._spectral_table_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
                layout.removeWidget(row)
                self._spectral_table_layout.addWidget(row)
                layout.addWidget(self._spectral_editor_panel)
                self._spectral_table_error = QLabel(field)
                self._spectral_table_error.setWordWrap(True)
                self._spectral_table_error.setProperty("validationMessage", True)
                layout.addWidget(self._spectral_table_error)
                editor.textChanged.connect(self._populate_spectral_table)
                self._populate_spectral_table(editor.text())
                self._spectral_declarations = QFrame(field)
                declaration_layout = QVBoxLayout(self._spectral_declarations)
                declaration_layout.setContentsMargins(0, 0, 0, 0)
                declaration_layout.setSpacing(12)
                declaration_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
                declaration_heading = QLabel("Reference preparation", self._spectral_declarations)
                declaration_heading.setObjectName("spectral-declaration-heading")
                declaration_heading.setProperty("panelHeading", True)
                declaration_layout.addWidget(declaration_heading)
                self._spectral_declaration_grid = QGridLayout()
                self._spectral_declaration_grid.setHorizontalSpacing(24)
                self._spectral_declaration_grid.setVerticalSpacing(6)
                self._spectral_declaration_grid.setColumnStretch(0, 1)
                self._spectral_declaration_grid.setColumnStretch(1, 1)
                declaration_layout.addLayout(self._spectral_declaration_grid)
                layout.addWidget(self._spectral_declarations)
        editor.setObjectName(f"control-{control.name}")
        return editor, field

    def _add_sequence_controls(
        self,
        control: FormControl,
        view: QTableView,
        model: _StructuredValueModel,
        layout: QVBoxLayout,
    ) -> None:
        from .form_navigation import FormNavigation

        row = QWidget(view.parentWidget())
        controls = QHBoxLayout(row)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(8)
        if control.choices:
            candidate: QWidget = QComboBox(row)
            candidate.addItem("Choose an item…", None)
            labels = (
                self._form.context.get("choice_labels", {}).get(control.name, {})
                if self._form is not None
                else {}
            )
            for choice in control.choices:
                candidate.addItem(labels.get(choice, choice), choice)
            candidate.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            candidate.setMinimumContentsLength(16)
            candidate.setObjectName(f"sequence-choice-{control.name}")
        else:
            candidate = QLineEdit(row)
            plain = self._form is not None and control.name in self._form.context.get(
                "plain_text_sequences", ()
            )
            placeholders = (
                self._form.context.get("plain_text_sequence_placeholders", {})
                if self._form is not None
                else {}
            )
            candidate.setPlaceholderText(
                placeholders.get(control.name, "Enter text") if plain else "JSON value"
            )
            candidate.setObjectName(f"sequence-value-{control.name}")
        controls.addWidget(candidate)
        candidate.setAccessibleName(f"Entry to add to {control.label}")
        candidate.setAccessibleDescription(control.help_text or "")

        add = ContentSizedButton("Add", row)
        add.setObjectName(f"sequence-add-{control.name}")
        add.clicked.connect(
            lambda _checked=False: self._append_sequence_value(control, view, model, candidate)
        )
        controls.addWidget(add)
        if isinstance(candidate, QLineEdit):
            candidate.returnPressed.connect(add.click)
        layout.addWidget(row)
        actions = FormNavigation(view.parentWidget())
        for label, name, offset in (
            ("Remove selected", "remove", None),
            ("Move up", "up", -1),
            ("Move down", "down", 1),
        ):
            button = ContentSizedButton(label, row)
            button.setObjectName(f"sequence-{name}-{control.name}")
            if offset is None:
                button.clicked.connect(
                    lambda _checked=False: self._remove_sequence_value(view, model)
                )
            else:
                button.clicked.connect(
                    lambda _checked=False, movement=offset: self._move_sequence_value(
                        view, model, movement
                    )
                )
            actions.addWidget(button)
        paste = ContentSizedButton("Paste rows", row)
        paste.setObjectName(f"sequence-paste-{control.name}")
        paste.setToolTip("Paste one value per line or a JSON array. Ctrl+V works when the table has focus.")

        def paste_rows():
            try:
                first = model.paste_values(QApplication.clipboard().text())
            except (TypeError, ValueError) as error:
                self._show_control_error(control.name, str(error), view)
                return
            self._errors[control.name].clear()
            self._errors[control.name].hide()
            view.selectRow(first)

        paste.clicked.connect(paste_rows)
        actions.addWidget(paste)
        undo = ContentSizedButton("Undo", row)
        undo.setObjectName(f"sequence-undo-{control.name}")
        undo.setToolTip("Undo the last table edit (Ctrl+Z).")
        undo.clicked.connect(model.undo)
        actions.addWidget(undo)
        layout.addWidget(actions)

        def copy_rows():
            rows = sorted({index.row() for index in view.selectionModel().selectedRows()})
            values = model.raw_value()
            QApplication.clipboard().setText("\n".join(
                str(values[index]) if model._plain_text else _display_json(values[index])
                for index in rows
            ))

        for key, callback in (
            (QKeySequence.StandardKey.Paste, paste_rows),
            (QKeySequence.StandardKey.Copy, copy_rows),
            (QKeySequence.StandardKey.Undo, model.undo),
            (QKeySequence.StandardKey.Redo, model.redo),
            (QKeySequence(Qt.Key.Key_Delete), lambda: self._remove_sequence_value(view, model)),
        ):
            shortcut = QShortcut(QKeySequence(key), view)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(callback)

    def _append_sequence_value(
        self,
        control: FormControl,
        view: QTableView,
        model: _StructuredValueModel,
        candidate: QWidget,
    ) -> None:
        try:
            if isinstance(candidate, QComboBox):
                value = candidate.currentData()
                if value is None:
                    raise ValueError("No item is selected. Choose an item before adding it.")
            else:
                assert isinstance(candidate, QLineEdit)
                if self._form is not None and control.name in self._form.context.get(
                    "plain_text_sequences", ()
                ):
                    value = candidate.text().strip()
                    if not value:
                        raise ValueError(
                            "The annotation label is blank. Enter a label before adding it."
                        )
                else:
                    value = _decode_json_value(control.name, candidate.text())
            row = model.append_value(value)
        except (TypeError, ValueError) as exc:
            detail = str(exc)
            if detail.startswith(("No item is selected.", "The annotation label is blank.")):
                self._show_control_error(control.name, detail, candidate)
            else:
                self._present_error(detail, "add")
                self._show_control_error(control.name, field_validation_summary(
                    detail, control.label, field_name=control.name), candidate)
                self._form_error_details_toggle.show()
            return
        self._errors[control.name].clear()
        candidate.setAccessibleDescription(control.help_text or "")
        view.selectRow(row)

    @staticmethod
    def _remove_sequence_value(
        view: QTableView,
        model: _StructuredValueModel,
    ) -> None:
        rows = sorted({index.row() for index in view.selectionModel().selectedRows()})
        model.remove_rows(rows)
        if rows and model.rowCount():
            view.selectRow(min(rows[0], model.rowCount() - 1))

    @staticmethod
    def _move_sequence_value(
        view: QTableView,
        model: _StructuredValueModel,
        offset: int,
    ) -> None:
        target = model.move_row(view.currentIndex().row(), offset)
        if target is not None:
            view.selectRow(target)

    def reveal_control(self, name: str) -> None:
        """Open the owning page and disclosure without replacing an edited value.

        Validation supplies an exact control name. We select its existing page
        and, when it is a hidden optional parameter, expand Advanced settings so
        its error and corrective input remain visible in the scrolling form.
        """
        channel_table = getattr(self, "_stage1_source_table", None)
        if channel_table is not None and channel_table.owns_control(name):
            channel_table.reveal(name)
        if self._measurement_channel_table is not None:
            self._measurement_channel_table.reveal(name)
        if getattr(self, "_measurement_intensity_grid", None) is not None:
            self._measurement_intensity_grid.reveal(name)
        if self._stage_pages is not None and name in self._stage_pages._fields:
            if self._stage_pages._fields[name][2].isHidden():
                for kind in ("primary", "secondary", "supervised"):
                    if self._form.context.get(kind):
                        getattr(self, f"_{kind}_advanced").setChecked(True)
            if name in self._segmentation_rows and self._segmentation_rows[name][1].isHidden():
                self._segmentation_advanced.setChecked(True)
            self._stage_pages.reveal(name)
        if name in self._marker_rows:
            if name == "selected_marker_ids":
                self._set_marker_step(-1)
            elif name == "marker_fit_scope":
                self._set_marker_step(0)
            elif name == "marker_composite_names":
                self._set_marker_step(2)
            else:
                markers = self._form.context["stage_result"]["resume_context"]["canonical_marker_ids"]
                marker = next((marker for marker in markers if name.startswith(f"marker_{marker}_")), None)
                if marker is not None:
                    self._marker_selector.setCurrentIndex(self._marker_selector.findData(marker))
                    self._set_marker_step(1)
                    if self._marker_rows[name][1].isHidden():
                        self._marker_workbench.advanced.setChecked(True)
        if name in self._alignment_rows and self._alignment_rows[name][1].isHidden():
            self._alignment_advanced.setChecked(True)
        if name in self._unmixing_rows and name != "spectral_state_override":
            self._set_unmixing_step(1)
            if self._unmixing_rows[name][1].isHidden():
                self._unmixing_advanced.setChecked(True)
        if self._form is not None and self._form.context.get("adapter") == "tissue_setup":
            from mif_pipeline.gui.tissue_flow import DETECTOR_FIELDS, DETAIL_FIELDS

            step = 0 if name in DETECTOR_FIELDS else 1 if name in DETAIL_FIELDS else 2
            self._set_tissue_step(step)
        if self._reference_settings_panel is not None and name in self._form.context.get(
            "reference_settings_groups", {}
        ):
            self._reference_settings_panel.reveal(name)

    def _show_control_error(
        self,
        name: str,
        message: str,
        focus: QWidget,
    ) -> None:
        self._present_field_errors({name: message}, focus=focus)

    def _clear_field_errors(self):
        """Clear feedback without removing the compact space it already occupied."""
        channel_table = getattr(self, "_stage1_source_table", None)
        if channel_table is not None:
            channel_table.set_field_errors({})
        for name, error in self._errors.items():
            if error.isVisible():
                error.setMinimumHeight(max(error.minimumHeight(), error.heightForWidth(error.width())))
            error.clear()
            error.hide()
            if name in self._validation_help:
                self._editors[name].setAccessibleDescription(self._validation_help.pop(name))
        summary = self._form_error_summary
        if summary.isVisible():
            summary.setMinimumHeight(max(summary.minimumHeight(), summary.heightForWidth(summary.width())))
        summary.clear()
        summary.hide()

    def _focus_validation_field(self, name, focus=None):
        if name not in self._editors:
            return
        if self._reference_regions_panel is not None and self._reference_regions_panel.owns_control(name):
            self._reference_regions_panel.reveal(name)
            return
        channel_table = getattr(self, "_stage1_source_table", None)
        if channel_table is not None and channel_table.owns_control(name):
            channel_table.reveal(name)
            return
        focus = focus or self._editors[name]
        if not focus.isVisibleTo(self):
            self.reveal_control(name)
        grid = getattr(self, "_measurement_intensity_grid", None)
        if grid is not None:
            focus = grid.validation_focus(name) or focus
        focus.setFocus(Qt.FocusReason.OtherFocusReason)

    def _present_field_errors(self, errors, *, focus=None, focus_first=True):
        """Show all field messages together; focus once after the complete update."""
        self._clear_field_errors()
        self._form_error_details_toggle.setChecked(False)
        self._form_error_details_toggle.hide()
        self._form_error_details.hide()
        errors = {name: message for name, message in errors.items() if name in self._errors}
        if not errors:
            return True
        controls = {control.name: control for control in self._form.controls}
        for name, message in errors.items():
            error, editor = self._errors[name], self._editors[name]
            policy = error.sizePolicy(); policy.setRetainSizeWhenHidden(True); error.setSizePolicy(policy)
            error.setMinimumHeight(max(error.minimumHeight(), error.fontMetrics().lineSpacing()))
            error.setText(message)
            error.show()
            self._validation_help[name] = editor.accessibleDescription()
            editor.setAccessibleDescription("\n".join(filter(None, (self._validation_help[name], message))))
        summary = self._form_error_summary
        policy = summary.sizePolicy(); policy.setRetainSizeWhenHidden(True); summary.setSizePolicy(policy)
        summary.setMinimumHeight(max(summary.minimumHeight(), summary.fontMetrics().lineSpacing()))
        summary.setTextFormat(Qt.TextFormat.RichText)
        summary_errors = {name: message for name, message in errors.items()
                          if self._reference_regions_panel is None or not self._reference_regions_panel.owns_control(name)}
        if summary_errors:
            links = ' · '.join(f'<a href="{escape(name, quote=True)}">{escape(control_label(name, controls[name].label))}</a>'
                               for name in summary_errors)
            summary.setText(f'Check {len(summary_errors)} field' + ('s' if len(summary_errors) != 1 else '') + ': ' + links)
            summary.show()
        else:
            policy = summary.sizePolicy()
            policy.setRetainSizeWhenHidden(False)
            summary.setSizePolicy(policy)
            summary.hide()
        channel_table = getattr(self, "_stage1_source_table", None)
        if channel_table is not None:
            channel_table.set_field_errors(errors)
        if focus_first:
            self._focus_validation_field(next(iter(errors)), focus)
        return False

    def _validate_current_fields(self, *, action=None, present=True):
        """Collect current-page editor errors; stage owners retain final scientific checks."""
        from .form_validation import control_errors
        names = set(self._editors)
        if action is not None:
            names &= applicable_control_names(self._form, action)
        channel_table = getattr(self, "_stage1_source_table", None)
        controls = tuple(control for control in self._form.controls if control.name in names
                         and (self._editors[control.name].isVisibleTo(self)
                              or (channel_table is not None and channel_table.isVisibleTo(self)
                                  and channel_table.owns_control(control.name)))
                         and self._editors[control.name].isEnabled())
        errors = control_errors(controls, self._raw_values(draft=True))
        if self._stage_pages is not None:
            for panel in self._stage_pages.projection_pages.values():
                if panel.target_name in names and (action is not None or panel.isVisibleTo(self)):
                    error = panel.validation_error()
                    if error:
                        errors[panel.target_name] = error
        if self._stage_pages is not None and self._stage_pages.feature_preparation is not None:
            panel = self._stage_pages.feature_preparation
            if panel.target_name in names and (action is not None or panel.isVisibleTo(self)):
                error = panel.validation_error()
                if error:
                    errors[panel.target_name] = error
        if self._reference_regions_panel is not None:
            errors = {name: message for name, message in errors.items()
                      if not self._reference_regions_panel.owns_control(name)}
            errors.update({name: message for name, message in self._reference_regions_panel.validation_errors().items()
                           if name in names})
        return self._present_field_errors(errors) if present else errors

    def _apply_stage1_visibility(self):
        content = self._editors["channel_content"].currentData()
        cycle_editor = self._editors.get("channel_cycle_values")
        reviewing = cycle_editor is not None and cycle_editor.reviewing
        if "channel_cycle_values" in self._editors:
            self._editors["channel_cycle_values"].channel_content = content
        self._heading.setText("Assign cycles" if reviewing else self._form.title)
        self._message.setVisible(not reviewing and bool(self._form.message))
        if not reviewing:
            self._message.setText(
                "Review the components to separate from acquired measurement bands and assign markers where known."
                if content == "raw" else
                "Review the labels and marker assignments for each separated image channel."
                if content == "unmixed" else self._form.message)
        for name, (label, field) in self._stage1_rows.items():
            if name == "channel_cycle_values":
                label.hide()
                field.show()
            elif not re.fullmatch(r"stage1_\d+_(assignment|biomarker|component_name)", name):
                label.setVisible(not reviewing and name not in {"nuclear_stain", "replace_channel_names_with_biomarkers"})
                field.setVisible(not reviewing)
        if self._stage1_actions is not None:
            for index in range(self._stage1_actions.count()):
                widget = self._stage1_actions.itemAt(index).widget()
                if widget is not None:
                    widget.setVisible(not reviewing)
        if self._stage1_source_table is not None:
            self._stage1_source_panel.setVisible(not reviewing and content == "unmixed")
        if self._stage1_component_field is not None:
            self._stage1_component_field.parentWidget().setVisible(not reviewing and content == "raw")
            self._stage1_component_label.hide()
        self._refresh_nuclear_stain()
        self.page_changed.emit()

    def _refresh_nuclear_stain(self, *_):
        if getattr(self, "_nuclear_refreshing", False):
            return
        self._nuclear_refreshing = True
        try:
            from mif_pipeline.io.metadata.canonical import is_autofluorescence_component
            content = self._editors["channel_content"].currentData()
            choices = []
            if content == "raw" and self._stage1_component_field is not None:
                from mif_pipeline.io.metadata.cycles import split_cycle_component
                rows = self._stage1_component_field.value()
                names = [row["component_name"] for row in rows]
                choices = [(name, name, split_cycle_component(name)[0].casefold() == "dapi") for name in names
                    if name and names.count(name) == 1 and not is_autofluorescence_component(name)]
            elif content == "unmixed":
                cycles = {row["channel_id"]: row["cycle"]
                          for row in self._form.context.get("accepted_channel_cycles", ())}
                for index, row in enumerate(self._form.context["stage1_channel_rows"]):
                    component = self._editors[f"stage1_{index}_component_name"].text().strip()
                    if not component or is_autofluorescence_component(component):
                        continue
                    title = row["channel_name"] or component
                    label = title if title == component else f"{title} ({component})"
                    cycle = cycles.get(row["channel_id"])
                    if cycle is not None:
                        label += f" · cycle {cycle + 1}"
                    dapi = any(str(value or "").casefold() == "dapi"
                               for value in (component, row["fluorophore"], row["channel_name"]))
                    choices.append((row["channel_id"], label, dapi))
                labels = [label for _identity, label, _dapi in choices]
                choices = [(identity, f"{label} ({identity})" if labels.count(label) > 1 else label, dapi)
                           for identity, label, dapi in choices]
            self._editors["nuclear_stain"].refresh(content, choices)
            self._normalize_nuclear_marker()
        finally:
            self._nuclear_refreshing = False

    def _normalize_nuclear_marker(self, *_):
        selected = self._editors["nuclear_stain"].currentData()
        if self._editors["channel_content"].currentData() == "raw":
            field = self._stage1_component_field
            if field is not None:
                field.set_nuclear_stain(selected)
        elif self._stage1_source_table is not None:
            self._stage1_source_table.set_nuclear_stain(selected)

    def _raw_values(self, *, draft: bool = False) -> dict[str, object]:
        if not draft and getattr(self, '_marker_preparation_response', None) is not None:
            return self._marker_preparation_response
        if self._marker_revisit_requested is not None:
            return {"revisit_marker_id": self._marker_revisit_requested}
        channel_table = getattr(self, "_stage1_source_table", None)
        if channel_table is not None:
            channel_table.commit_pending()
        values: dict[str, object] = {}
        for name, editor in self._editors.items():
            if self._reference_regions_panel is not None and self._reference_regions_panel.owns_control(name):
                continue
            if not draft and "_kronos_panel_" in name and "_additional_" in name:
                if not self._kronos_additional_marker_visible(name):
                    continue
                if ((isinstance(editor, QComboBox) and editor.currentData() is None)
                        or (isinstance(editor, QLineEdit) and not editor.text().strip())):
                    continue
            if name in {"phenotyping_population", "selected_marker_ids"}:
                values[name] = editor.value()
            elif name == "consensus_roi_review" and self._form.context.get("consensus_review"):
                values[name] = editor.value(validate=not draft)
            elif isinstance(editor, PixelFeatureEditor):
                values[name] = editor.capture_draft()
            elif isinstance(editor, (IntensityMeasurements, ComponentAssignmentTable, ChannelCycleEditor, AcquisitionOrganizationEditor, ClassifierChannels, ClusteringFeatures, SupervisedFeatures)):
                values[name] = editor.value()
            elif isinstance(editor, (ObjectEditor, SecondaryDefinitionEditor, PrimaryClassCountBounds)):
                try:
                    values[name] = editor.capture_draft() if draft else editor.value()
                except ValueError as exc:
                    raise ValueError(f"{name}: {exc}") from exc
            elif isinstance(editor, KronosMarkerAssignment):
                values[name] = editor.value()
            elif isinstance(editor, QComboBox):
                values[name] = editor.currentData()
            elif isinstance(editor, QCheckBox):
                values[name] = editor.isChecked()
            elif isinstance(editor, QLineEdit):
                values[name] = editor.text()
            elif isinstance(editor, QPlainTextEdit):
                values[name] = editor.toPlainText()
            elif isinstance(editor, QTableView):
                model = editor.model()
                assert isinstance(model, (_StructuredValueModel, ClusterDecisionModel))
                values[name] = model.raw_value()
        if not draft and self._form is not None and _is_batch_clustering_form(self._form) and "feature_ids" in values:
            # Unselected weights cannot affect this fit. Keep unfinished edits
            # in the draft, while retaining valid offered values for the form
            # decoder's unused fields.
            selected = set(values["feature_ids"])
            initial = {control.name: control.value for control in self._form.controls}
            for index, feature in enumerate(self._form.context["resume_context"]["available_features"]):
                if feature["feature_id"] not in selected:
                    name = f"feature_weight_{index}"
                    values[name] = initial[name]
        if self._spectral_table is not None:
            if not draft and self._spectral_table_error.text():
                raise ValueError(self._spectral_table_error.text())
            editable = self._spectral_table.library is None
            values["spectral_csv"] = self._spectral_table.csv_text(validate=not draft) if editable else None
            if not draft and editable:
                values["library_locator"] = None
        if self._target_af_groups_panel is not None:
            values['target_af_groups'] = self._target_af_groups_panel.value(validate=not draft)
        if self._reference_band_panel is not None:
            values.update(self._reference_band_panel.values())
        if (self._af_control_review_panel is not None
                and self._form.context['af_control_set_review'].get('review_kind') != 'matched_negative'):
            values['af_control_set_review_sha256s'] = tuple(
                row['evidence_sha256'] for row in self._form.context['af_control_set_review']['sets']
            ) if self._af_control_reviewed else ()
        if self._reference_regions_panel is not None:
            values.update(self._reference_regions_panel.values())
        if (self._reference_extraction_panel is not None
                and any(c.name == "included_roi_ids" for c in self._form.controls)):
            values.update(self._reference_extraction_panel.values())
        if (self._form is not None and self._form.context.get("region_review") is not None
                and any(control.name == "excluded_roi_ids" for control in self._form.controls)):
            values["excluded_roi_ids"] = self._region_review.excluded_roi_ids()
        if self._form is not None and self._form.context.get("adapter") in {"marker_setup", "marker_combinations"}:
            if self._marker_combinations is not None:
                values.update(self._marker_combinations.value())
        if not draft and getattr(self, "_marker_prepare_inputs", False):
            values["preview_population"] = True
        if not draft and self._stage_pages is not None:
            for name in values:
                if not self._stage_pages.primary_row_selected(name):
                    values[name] = None
        return values

    def _present_error(self, message: str, action: str) -> None:
        form = self._form
        if form is None:
            return
        policy = self._form_error_summary.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self._form_error_summary.setSizePolicy(policy)
        self._form_error_summary.setMinimumHeight(max(
            self._form_error_summary.minimumHeight(), self._form_error_summary.fontMetrics().lineSpacing()))
        self._form_error_summary.setTextFormat(Qt.TextFormat.PlainText)
        self._form_error_details_toggle.setChecked(False)
        self._form_error_details_toggle.show()
        if form.context.get("supervised"):
            if "supervised_0_class_ids" in message or "supervised_0_feature_ids" in message:
                self._set_supervised_step(0)
            elif self._supervised_step == 1 and any(
                token in message
                for token in (
                    "normalization",
                    "pca",
                    "rtrees",
                    "max_depth",
                    "min_sample_count",
                    "max_trees",
                    "max_iterations",
                    "termination_epsilon",
                )
            ):
                self._supervised_advanced.setChecked(True)
        applicable = tuple(
            control
            for control in form.controls
            if control.name in self._editors
            and re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(control.name)}(?![A-Za-z0-9_])",
                message,
            )
            is not None
        )
        if not applicable and any(
            form.context.get(kind) for kind in ("primary", "secondary", "supervised")
        ):
            if form.context.get("secondary"):
                candidate_match = re.search(r"(?<![A-Za-z0-9_])training_(\d+):", message)
                prefix = f"training_{candidate_match.group(1)}" if candidate_match else None
            else:
                prefix = "training_0" if form.context.get("primary") else "supervised_0"
            method_editor = self._editors.get(f"{prefix}_model_method") if prefix else None
            method = (
                method_editor.currentData() if isinstance(method_editor, QComboBox) else "rtrees"
            )
            if form.context.get("supervised"):
                method = method.removeprefix("kronos_")
            aliases = (
                ("image_features", f"{prefix}_image_features"),
                ("pca_retained_variance", f"{prefix}_pca_variance"),
                ("pca_normalize", f"{prefix}_pca_normalize"),
                ("resolution_um_per_pixel", f"{prefix}_resolution"),
                ("maximum_training_samples", f"{prefix}_maximum_samples"),
                ("reweight_samples", f"{prefix}_reweight"),
                ("boundary_thickness_px", f"{prefix}_boundary_thickness"),
                ("boundary_class_id", f"{prefix}_boundary_label"),
                ("boundary_strategy", f"{prefix}_boundary_strategy"),
                ("gamma", f"{prefix}_svm_gamma"),
                ("max_iterations", f"{prefix}_svm_max_iterations"),
                (
                    "termination_epsilon",
                    f"{prefix}_{'svm' if method == 'svm' else 'rtrees'}_termination_epsilon",
                ),
                ("c", f"{prefix}_svm_c"),
                ("kernel", f"{prefix}_svm_kernel"),
                ("max_trees", f"{prefix}_rtrees_max_trees"),
                ("min_sample_count", f"{prefix}_rtrees_min_sample_count"),
                ("max_depth", f"{prefix}_rtrees_max_depth"),
                ("active_var_count", f"{prefix}_rtrees_active_var_count"),
                ("calculate_variable_importance", f"{prefix}_rtrees_calculate_variable_importance"),
                ("normalization", f"{prefix}_normalization"),
            )
            if prefix is not None:
                applicable = tuple(
                    control
                    for token, name in aliases
                    if re.search(rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])", message)
                    for control in form.controls
                    if control.name == name
                )
        if not applicable:
            summary = operation_error_summary(message) or (
                "This choice could not be accepted. Review the form, then try again. "
                "Open Details for the exact validation rule."
            )
            self._form_error_summary.setText(summary)
            self._form_error_summary.show()
            self._form_error_details.setText(diagnostic_details(message, summary))
            return
        control = next(
            (item for item in applicable if self._editors[item.name].isVisible()), applicable[0]
        )
        if form.context.get("secondary") and (
            "_rtrees_" in control.name
            or "_output_label_" in control.name
            or control.name.endswith(("_seed", "_svm_max_iterations", "_svm_termination_epsilon"))
        ):
            self._secondary_advanced.setChecked(True)
        summary = field_validation_summary(message, control.label, field_name=control.name)
        self._present_field_errors({
            item.name: field_validation_summary(message, item.label, field_name=item.name)
            for item in applicable
        }, focus_first=False)
        self._form_error_details_toggle.show()
        self._form_error_details.setText(diagnostic_details(message, summary))
        if form.context.get("adapter") in {"segmentation_setup", "segmentation_options"} and control.name != "scope":
            self._segmentation_advanced.setChecked(True)
        if _is_batch_clustering_form(form):
            step = (
                0
                if control.name in {"clustering_mode"}
                else 1 if control.name == "method" else 2 if control.name == "feature_ids" else 3
            )
            self._set_clustering_step(step)
            if step == 3 and control.name not in {"k", "resolution", "flowsom_clusters"}:
                self._clustering_advanced.setChecked(True)
        editor = self._editors[control.name]
        if not editor.isVisibleTo(self):
            self.reveal_control(control.name)
        if self._marker_combinations is not None and control.name == "marker_composite_names":
            self._marker_combinations.error.setText(summary)
            self._marker_combinations.error.show()
            self._marker_combinations.rules.setFocus(Qt.FocusReason.OtherFocusReason)
        elif editor.isVisible() or (_is_batch_clustering_form(form) and control.name in {"clustering_mode", "method"}):
            self._focus_validation_field(control.name)

    def _browse(self, control: FormControl, editor: QLineEdit) -> None:
        selected = self._path_chooser(control)
        if selected:
            if self._form is not None and self._form.context.get("adapter") == "tissue_setup":
                from mif_pipeline.gui.tissue_setup import normalized_tissue_source_path

                normalized = normalized_tissue_source_path(selected)
                self._tissue_source_ready = False
                self._tissue_observation_pending = True
                self.tissue_source_invalidated.emit()
                editor.setText(normalized)
                destination = self._editors.get("import_classifier_name")
                if isinstance(destination, QLineEdit) and not destination.text().strip():
                    destination.setText(Path(normalized).name)
                self._apply_tissue_visibility()
                self.tissue_source_selected.emit(normalized)
                return
            selected_path = str(Path(selected).expanduser().resolve())
            if control.name == "library_locator" and self._spectral_choices is not None:
                if selected_path == editor.text():
                    self._populate_spectral_table(selected_path)
                else:
                    editor.setText(selected_path)
                return
            editor.setText(selected_path)
            if control.name == "saved_classifier_export":
                self._describe_saved_classifier()
            editor.setAccessibleDescription(control.help_text or "")
            error = self._errors.get(control.name)
            if error is not None:
                error.clear()

    def _populate_spectral_table(self, path, *, draft=None):
        """Replace this page's table from one bounded file or its retained local draft.

        A failed import leaves the prior table visible but blocks submission
        until the researcher selects a readable library or clears the import.
        """
        from .curated_table import SpectralTableWidget

        inventory = self._form.context["spectral_input"]
        parent = self._spectral_table_layout.parentWidget()
        try:
            if draft is not None:
                table = SpectralTableWidget.from_text(draft, inventory, parent, draft=True)
            elif path.strip():
                table = SpectralTableWidget.from_file(path.strip(), inventory, parent)
            else:
                table = SpectralTableWidget(inventory["band_names"], inventory["component_names"], parent)
        except (OSError, ValueError, TypeError) as error:
            self._spectral_table_error.setText(
                f"Could not open {Path(path).name}. Choose a readable library file."
                if isinstance(error, OSError) else str(error))
            self._spectral_table_error.setToolTip(str(error))
            self._spectral_table_error.show()
            return
        self._spectral_table_error.clear()
        self._spectral_table_error.hide()
        if self._spectral_table is not None:
            self._spectral_table_layout.removeWidget(self._spectral_table)
            self._spectral_table.hide()
            self._spectral_table.deleteLater()
        self._spectral_table = table
        table.findChild(QLabel, "spectral-table-title").hide()
        self._spectral_table_layout.addWidget(table)
        self._apply_library_declarations()
        self._set_spectral_page(1 if path.strip() or draft is not None else self._spectral_step)

    def _enter_spectral_table(self):
        """Resume editable values, or start an editable table after a saved library.

        Going Back retains the table and its acquisition declarations. Selecting
        manual entry from a read-only library explicitly starts a new table.
        """
        if self._spectral_table is not None and self._spectral_table.library is not None:
            self._editors["library_locator"].clear()
        elif self._spectral_table_error.text():
            # A failed replacement must not discard the previous editable draft.
            from PySide6.QtCore import QSignalBlocker
            editor = self._editors["library_locator"]
            with QSignalBlocker(editor):
                editor.clear()
            self._spectral_table_error.clear()
            self._spectral_table_error.hide()
            if self._spectral_table is None:
                self._populate_spectral_table("")
        self._set_spectral_page(1)

    def _set_spectral_page(self, step):
        """Switch local entry pages without submitting or replacing a saved result."""
        if self._spectral_choices is None:
            return
        self._spectral_step = 1 if step else 0
        editing = bool(self._spectral_step)
        self._spectral_opened = self._spectral_opened or editing
        self._spectral_choices.setVisible(not editing)
        self._spectral_editor_panel.setVisible(editing)
        self._spectral_source_row.setVisible(editing)
        if self._spectral_declarations is not None:
            self._spectral_declarations.setVisible(editing and
                (self._spectral_table is None or self._spectral_table.library is None))
        if self._spectral_table is not None:
            self._spectral_table.setVisible(editing)
        locator = self._editors.get("library_locator")
        path = "" if locator is None else locator.text().strip()
        filename = Path(path).name if path else "Entered reference values"
        width = max(120, self._spectral_source_name.width())
        self._spectral_source_name.setText(self._spectral_source_name.fontMetrics().elidedText(
            filename, Qt.TextElideMode.ElideMiddle, width))
        self._spectral_source_name.setAccessibleName(filename)
        self._spectral_source_name.setToolTip(path)
        replace_button = self._spectral_source_row.findChild(QPushButton)
        if replace_button is not None:
            replace_button.setText("Replace file…" if path else "Import file…")
        for label, field in self._library_declaration_rows.values():
            label.setVisible(editing)
            field.setVisible(editing)
        for button in self._spectral_buttons:
            button.setVisible(editing)
        if self._spectral_back is not None:
            self._spectral_back.setVisible(editing)
        if self._spectral_outer_back is not None:
            self._spectral_outer_back.setVisible(not editing)
        if self._spectral_return is not None:
            self._spectral_return.setVisible(not editing and self._spectral_opened)
        if self._form.context.get("adapter") == "batch_reference_operation":
            self._heading.setText("Review spectral library" if editing else "Import or enter a spectral library")
            self._message.setText("" if editing else "Choose the reference source for this image.")
            self._message.setVisible(not editing)
        self._layout_spectral_controls()
        self.page_changed.emit()
        self.updateGeometry()

    def resizeEvent(self, event):
        """Reflow the library choices and declarations within the current form width."""
        super().resizeEvent(event)
        if getattr(self, "_spectral_choices", None) is not None:
            self._layout_spectral_controls()
            locator = self._editors.get("library_locator")
            path = "" if locator is None else locator.text().strip()
            filename = Path(path).name if path else "Entered reference values"
            self._spectral_source_name.setText(self._spectral_source_name.fontMetrics().elidedText(
                filename, Qt.TextElideMode.ElideMiddle, max(120, self._spectral_source_name.width())))

    def _layout_spectral_controls(self):
        """Stack narrow library forms while retaining the existing editors and values."""
        narrow = self.width() < 860
        if self._spectral_layout_narrow == narrow or self._spectral_declaration_grid is None:
            return
        self._spectral_layout_narrow = narrow
        for content, action in self._spectral_source_actions:
            content.removeWidget(action)
            content.addWidget(action, 2 if narrow else 0, 0 if narrow else 1,
                              1 if narrow else 2, 1, Qt.AlignmentFlag.AlignLeft if narrow else Qt.AlignmentFlag.AlignVCenter)
        grid = self._spectral_declaration_grid
        for index, name in enumerate(("normalization", "measurement_convention")):
            pair = self._library_declaration_rows.get(name)
            if pair is None:
                continue
            label, field = pair
            grid.removeWidget(label)
            grid.removeWidget(field)
            if narrow:
                row, column, span = index * 2, 0, 1
            else:
                row, column, span = 0, index, 1
            grid.addWidget(label, row, column, 1, span)
            grid.addWidget(field, row + 1, column, 1, span)
            editor = self._editors.get(name)
            if isinstance(editor, QComboBox):
                editor.setMinimumWidth(0)
                editor.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        grid.setColumnStretch(1, 0 if narrow else 1)
        self.updateGeometry()

    def _apply_library_declarations(self, *_args):
        """Display typed-library declarations read-only below its unchanged values.

        Editable tables need explicit numerical and acquisition declarations.
        Typed libraries keep the complete saved metadata used by the backend.
        """
        table = self._spectral_table
        library = None if table is None else table.library
        recorded = {}
        names = ("normalization", "measurement_convention")
        if all(name in self._editors for name in names):
            if library is not None and self._library_manual_declarations is None:
                self._library_manual_declarations = {
                    name: self._editors[name].currentData() if isinstance(self._editors[name], QComboBox)
                    else self._editors[name].text() for name in names}
            elif library is None and self._library_manual_declarations is not None:
                for name, value in self._library_manual_declarations.items():
                    editor = self._editors[name]
                    if isinstance(editor, QComboBox):
                        editor.setCurrentIndex(max(0, editor.findData(value)))
                    else:
                        editor.setText(value)
                self._library_manual_declarations = None
        if library is not None:
            recorded["normalization"] = {"unit_l1": "l1", "unit_l2_spectral_shape_v1": "l2"}[library.normalization_convention]
            modes = tuple(dict.fromkeys(item.correction_mode for item in library.quantitative_signature.measurements))
            recorded["measurement_convention"] = ({"full": "acquisition_normalized", "exposure_only": "exposure_normalized",
                                                   "as_stored": "stored_calibrated"}.get(modes[0]) if len(modes) == 1 else None)
        if self._spectral_declarations is not None:
            self._spectral_declarations.setVisible(bool(self._spectral_step) and library is None)
            self._spectral_declarations.findChild(QLabel, "spectral-declaration-heading").setText(
                "Recorded reference preparation" if library is not None else "Reference preparation")
        for name, (label, field) in self._library_declaration_rows.items():
            label.setVisible(bool(self._spectral_step))
            field.setVisible(bool(self._spectral_step))
            editor = self._editors.get(name)
            if editor is None:
                continue
            editor.setEnabled(library is None)
            help_label = field.findChild(QLabel, "form-help-" + name)
            if help_label is not None:
                help_label.setVisible(library is None and name != "normalization")
                if library is None and name == "measurement_convention":
                    from ..reference_copy import REFERENCE_CORRECTION_HELP
                    help_label.setText(REFERENCE_CORRECTION_HELP.get(editor.currentData(),
                        "Select the correction applied before the reference spectra were normalized."))
            if library is not None and isinstance(editor, QComboBox):
                index = editor.findData(recorded.get(name))
                editor.setPlaceholderText("Recorded per acquired band" if recorded.get(name) is None else str(recorded[name]).replace("_", " "))
                editor.setCurrentIndex(index)
            fixed = self._spectral_recorded_declarations.get(name)
            if fixed is not None:
                read_only = library is not None or editor.count() == 2
                if library is not None:
                    from ..reference_copy import SPECTRAL_CHOICE_LABELS
                    fixed.setText(SPECTRAL_CHOICE_LABELS[name].get(recorded.get(name), "Recorded per acquired band"))
                else:
                    fixed.setText(editor.currentText())
                fixed.setVisible(read_only)
                editor.setVisible(not read_only)

    def _choose_local_path(self, control: FormControl) -> str | None:
        if (
            self._form is not None and self._form.context.get("adapter") in {"segmentation_setup", "segmentation_options"}
        ):
            selected = QFileDialog.getExistingDirectory(self, control.label)
        elif control.name == "library_locator":
            selected, _filter = QFileDialog.getOpenFileName(
                self,
                "Select spectral library",
                "",
                "Spectral libraries (*.miflib.zip *.json *.csv);;Portable libraries (*.miflib.zip *.json);;CSV (*.csv)",
            )
        elif "output" in control.name:
            selected, _filter = QFileDialog.getSaveFileName(self, control.label)
        else:
            selected, _filter = QFileDialog.getOpenFileName(self, control.label)
        return selected or None

    def _clear(self) -> None:
        self._unmixing_rows.clear()
        self._alignment_rows.clear()
        self._unmixing_buttons.clear()
        self._unmixing_step = 0
        self._measurement_rows.clear()
        self._measurement_buttons.clear()
        self._measurement_step = 0
        self._segmentation_rows.clear()
        self._segmentation_previous_method = None
        self._segmentation_channel_selections.clear()
        self._clustering_rows.clear()
        self._clustering_setup = None
        self._cluster_review = None
        self._segmentation_buttons.clear()
        self._clustering_buttons.clear()
        self._clustering_step = 0
        self._primary_rows.clear()
        self._primary_buttons.clear()
        self._training_plan_button = None
        self._primary_step = 0
        self._secondary_rows.clear()
        self._fit_review_rows.clear()
        self._supervised_rows.clear()
        self._supervised_buttons.clear()
        self._supervised_step = 0
        self._marker_revisit_requested = None
        self._marker_rows.clear()
        self._marker_review_rows.clear()
        self._marker_buttons.clear()
        self._marker_step = 0
        self._marker_combinations = None
        self._tissue_rows.clear()
        self._tissue_buttons.clear()
        self._tissue_step = 0
        self._tissue_source_ready = False
        self._tissue_observation_pending = False
        self._tissue_return_to_regions = False
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            child_layout = item.layout()
            if child_layout is not None:
                self._delete_layout(child_layout)
        self._editors.clear()
        self._errors.clear()
        self._models.clear()

    def _delete_layout(self, layout: object) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            child = item.layout()
            if child is not None:
                self._delete_layout(child)
