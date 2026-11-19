"""Present accepted outputs, their saved locations and optional analysis actions.

The page reads only the supplied result tables and bound routes. Selecting a sample changes
its displayed locations; the analysis-wide file actions retain the paths supplied by the
controller. Supporting records are disclosed on request. Saved figure requests retain
their selected result identity; exporting copies their prepared bytes without calculation.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from pathlib import Path
import re

from PySide6.QtCore import QModelIndex, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..interactions import FormTable
from ..successor_services import BoundSuccessorRoutes, SuccessorRouteStatus
from .forms import apply_widget_accessibility
from .models import FormTableModel
from .researcher_widgets import ContentSizedButton, ElidedComboBox, configure_disclosure
from .workbench import (
    PageHeading,
    _fit_bounded_table_height,
    fit_scientific_columns,
    style_scientific_table,
)

# ==============================================================================
#                                    Results
# ==============================================================================


class _LocationLabel(QLabel):
    """Show a filename within the available width, retaining its full path."""

    def set_location(self, path: object) -> None:
        """Replace the visible name and its exact copy/accessible location together."""
        self._filename = "Not available for this sample" if path is None else Path(str(path)).name
        self.setToolTip("" if path is None else str(path))
        self.setAccessibleDescription(self.toolTip())
        self._fit_name()

    def _fit_name(self) -> None:
        name = getattr(self, "_filename", "")
        self.setText(self.fontMetrics().elidedText(
            name, Qt.TextElideMode.ElideMiddle, max(0, self.contentsRect().width())
        ))

    def resizeEvent(self, event: object) -> None:
        self._fit_name()
        super().resizeEvent(event)


class _ResultTableModel(FormTableModel):
    """Name result locations by their scientific role while retaining exact cell values."""

    def __init__(
        self, table: FormTable, parent: QWidget, *, path_label: str,
        compact_locations: bool = False,
    ) -> None:
        """Retain the original table and substitute only its displayed path heading."""
        super().__init__(table, parent)
        self._path_label = path_label
        self._compact_locations = compact_locations

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        """Shorten the main location display while keeping full values for copy."""
        value = super().data(index, role)
        if (index.isValid() and self._columns[index.column()] == "sample_ids"
                and role in {Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole}):
            samples = super().data(index, Qt.ItemDataRole.UserRole)
            if isinstance(samples, (tuple, list)):
                return ", ".join(str(sample) for sample in samples)
        if (self._compact_locations and role == Qt.ItemDataRole.DisplayRole
                and index.isValid() and value
                and self._columns[index.column()] in {"processed_image_path", "path"}):
            return Path(str(value)).name
        return value

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        """Use the result's role for location columns and the shared labels elsewhere."""
        if (
            orientation == Qt.Orientation.Horizontal
            and role == Qt.ItemDataRole.DisplayRole
            and 0 <= section < len(self._columns)
            and self._columns[section] == "path"
        ):
            return self._path_label
        return super().headerData(section, orientation, role)


class ResultsPage(QWidget):
    """Render immutable result references and emit only explicitly bound actions."""

    open_qupath_requested = Signal()
    open_plot_requested = Signal(object)
    reveal_path_requested = Signal(object)
    spatial_requested = Signal(object)
    patient_requested = Signal(object, object)
    saved_figure_requested = Signal(str, str, str)
    measurement_field_requested = Signal(str, object)
    saved_region_requested = Signal()
    supervised_review_requested = Signal(str, str)
    segmentation_review_requested = Signal(str, str)

    def __init__(self, *, parent: QWidget | None = None) -> None:
        """Build the output inventory, sample locations and initially closed records."""
        super().__init__(parent)
        self._models: dict[str, FormTableModel] = {}
        self._tables: dict[str, QTableView] = {}
        self._location_labels: dict[str, _LocationLabel] = {}
        self._location_copy_buttons: dict[str, QPushButton] = {}
        self._plot_path: Path | None = None
        self._reveal_path: Path | None = None
        self._bound_routes: BoundSuccessorRoutes | None = None
        self._saved_head: str | None = None
        self._saved_stages = ()
        self._scope_selections = {}
        self._selection_context = None
        self._saved_alignment_request = None
        self._saved_alignment_identity = None
        self._saved_plots = ()
        self._classifier_error_request = None
        self._classifier_location_rows = ()
        self._classifier_cell_request = None
        self.setStyleSheet(
            'QFrame#results-output-card { border-top: 3px solid #006b70; }'
            'QLabel#results-count { color: #006166; background: #e4f0ee; '
            'border-radius: 5px; padding: 5px 10px; font-weight: 600; }'
            'QLabel[resultsCaption="true"] { color: #486169; }'
            'QLabel[resultsLocation="true"] { color: #294148; '
            'background: #f3f6f5; border-radius: 4px; padding: 6px 9px; }'
            'QFrame#results-selected-locations { border-top: 1px solid #dce4e4; }'
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        scroll = QScrollArea(self)
        scroll.setObjectName("results-scroll-area")
        scroll.setWidgetResizable(True)
        content = QWidget(scroll)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(18)
        layout.addWidget(
            PageHeading(
                "results",
                "Results & export",
                "Review saved results and export figures or data.",
                content,
            )
        )
        self._empty = QLabel("No finished results are available for this analysis yet.", content)
        self._empty.setObjectName("results-empty")
        self._empty.setWordWrap(True)
        layout.addWidget(self._empty)

        self._stages = QFrame(content)
        self._stages.setProperty("workbenchSection", True)
        stage_layout = QVBoxLayout(self._stages)
        stage_layout.setContentsMargins(20, 18, 20, 18)
        selectors = QGridLayout()
        selectors.setHorizontalSpacing(20)
        selectors.setVerticalSpacing(7)
        self._stage_choice = ElidedComboBox(self._stages)
        self._stage_choice.setObjectName("results-stage-choice")
        self._sample_choice = ElidedComboBox(self._stages)
        self._sample_choice.setObjectName("results-sample-choice")
        for column, (title, widget) in enumerate((
            ("Completed stage", self._stage_choice),
            ("Sample or result", self._sample_choice),
        )):
            caption = self._caption(title, self._stages)
            caption.setBuddy(widget)
            widget.setAccessibleName(title)
            widget.setMinimumWidth(0)
            widget.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            selectors.addWidget(caption, 0, column)
            selectors.addWidget(widget, 1, column)
        selectors.setColumnStretch(0, 1)
        selectors.setColumnStretch(1, 1)
        stage_layout.addLayout(selectors)
        self._stage_choice.currentIndexChanged.connect(self._select_stage_group)
        self._sample_choice.currentIndexChanged.connect(self._select_stage)
        details = QWidget(self._stages)
        detail_layout = QVBoxLayout(details)
        detail_layout.setContentsMargins(0, 10, 0, 0)
        detail_layout.setSpacing(12)
        stage_layout.addWidget(details)
        self._stage_file = QWidget(self._stages)
        file_layout = QVBoxLayout(self._stage_file)
        file_layout.setContentsMargins(0, 0, 0, 0)
        location_row = QHBoxLayout()
        file_layout.addLayout(location_row)
        self._stage_location = _LocationLabel(self._stages)
        self._stage_location.setTextFormat(Qt.TextFormat.PlainText)
        self._stage_location.setMinimumWidth(0)
        self._stage_location.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self._stage_location.setProperty("resultsLocation", True)
        location_row.addWidget(self._stage_location, 1)
        self._stage_reveal = self._button("Show in folder", "results-stage-reveal", self._reveal_stage)
        self._stage_copy = self._button("Copy path", "results-stage-copy", self._copy_stage_path)
        location_row.addWidget(self._stage_reveal)
        location_row.addWidget(self._stage_copy)
        self._stage_file_note = self._caption("", self._stage_file)
        file_layout.addWidget(self._stage_file_note)
        self._stage_message = self._caption("", self._stages)
        self._stage_message.setObjectName("results-stage-message")
        detail_layout.addWidget(self._stage_message)
        self._figure_choice = ElidedComboBox(self._stages)
        self._figure_choice.setObjectName("results-saved-figure-choice")
        self._figure_choice.setAccessibleName("Figure or table")
        self._figure_caption = self._caption("Figure or table", self._stages)
        self._figure_caption.setBuddy(self._figure_choice)
        detail_layout.addWidget(self._figure_caption)
        self._figure_choice.currentIndexChanged.connect(self._select_saved_figure)
        detail_layout.addWidget(self._figure_choice)
        self._figure_detail = self._caption("", self._stages)
        detail_layout.addWidget(self._figure_detail)
        self._outline_palette_row = QWidget(self._stages)
        palette_layout = QHBoxLayout(self._outline_palette_row)
        palette_layout.setContentsMargins(0, 0, 0, 0)
        palette_layout.addWidget(QLabel('Outline palette', self._outline_palette_row))
        self._outline_palette = QComboBox(self._outline_palette_row)
        from mif_pipeline.cell_analysis.figure_style import OUTLINE_PALETTES
        for key, values in OUTLINE_PALETTES.items():
            self._outline_palette.addItem(values[0], key)
        palette_layout.addWidget(self._outline_palette)
        palette_layout.addStretch()
        self._outline_palette.currentIndexChanged.connect(self._gallery_changed)
        self._outline_palette_row.hide()
        detail_layout.addWidget(self._outline_palette_row)
        self._gallery_container = QWidget(self._stages)
        self._gallery_layout = QVBoxLayout(self._gallery_container)
        self._gallery_layout.setContentsMargins(0, 0, 0, 0)
        self._gallery_settings = None
        self._gallery_revision = 0
        detail_layout.addWidget(self._gallery_container)
        self._export_palette_row = QWidget(self._stages)
        export_palette_layout = QHBoxLayout(self._export_palette_row)
        export_palette_layout.setContentsMargins(0, 0, 0, 0)
        export_palette_layout.addWidget(QLabel('Export palette', self._export_palette_row))
        self._export_palette = QComboBox(self._export_palette_row)
        self._export_palette.setAccessibleName('Figure export palette')
        self._export_palette.setToolTip('Colors for the exported figure. The preview keeps its current colors.')
        from mif_pipeline.cell_analysis.figure_style import RESULT_EXPORT_PALETTES
        for name, values in RESULT_EXPORT_PALETTES.items():
            self._export_palette.addItem(values[0], name)
        export_palette_layout.addWidget(self._export_palette)
        export_palette_layout.addStretch(1)
        detail_layout.addWidget(self._export_palette_row)
        self._export_palette_row.hide()
        self._review_assignments = self._button("Review cell assignments", "results-review-assignments", self._open_saved_assignments)
        self._review_assignments.hide()
        detail_layout.addWidget(self._review_assignments, alignment=Qt.AlignmentFlag.AlignLeft)
        self._review_outlines = self._button("Review cell and nucleus outlines", "results-review-outlines", self._open_saved_outlines)
        self._review_outlines.hide()
        detail_layout.addWidget(self._review_outlines, alignment=Qt.AlignmentFlag.AlignLeft)
        from .wrapping_layout import WrappingLayout
        stage_action_host = QWidget(self._stages)
        stage_actions = WrappingLayout(stage_action_host)
        self._stage_open = self._button("Preview figure", "results-stage-open", lambda: self._request_saved_figure("open"))
        self._stage_export = self._button("Export figure…", "results-stage-export", lambda: self._request_saved_figure("export"))
        stage_actions.addWidget(self._stage_open)
        stage_actions.addWidget(self._stage_export)
        self._stage_data = self._button("Export data…", "results-stage-data", lambda: self._request_saved_figure("data"))
        stage_actions.addWidget(self._stage_data)
        self._stage_larger = self._button("Open larger view", "results-stage-larger", self._open_saved_preview)
        self._stage_larger.hide()
        stage_actions.addWidget(self._stage_larger)
        detail_layout.addWidget(stage_action_host)
        self._figure_status = self._caption("", self._stages)
        self._figure_status.setObjectName("results-saved-figure-status")
        detail_layout.addWidget(self._figure_status)
        from .classifier_review_data import review_table
        self._classifier_table_choice = QComboBox(self._stages)
        self._classifier_table_choice.setAccessibleName("Recorded classifier table")
        self._classifier_table_choice.currentIndexChanged.connect(self._show_classifier_table)
        detail_layout.addWidget(self._classifier_table_choice)
        self._classifier_table = review_table("saved-classifier-table", self._stages)
        self._classifier_table.setAccessibleDescription(
            "Select a row and press Ctrl+C to copy its displayed values. Export data retains full precision and identities.")
        detail_layout.addWidget(self._classifier_table)
        self._inspect_classifier_region = self._button("Inspect reference region in QuPath",
            "results-inspect-classifier-region", self._inspect_saved_classifier_region)
        detail_layout.addWidget(self._inspect_classifier_region, alignment=Qt.AlignmentFlag.AlignLeft)
        self._inspect_classifier_region.hide()
        self._classifier_table_choice.hide()
        self._classifier_table.hide()
        from .training_fit_summary import ClassifierEvaluationPanel
        from .classifier_cross_validation import ClassifierCrossValidationPanel
        self._classifier_evaluation = ClassifierEvaluationPanel(self._stages)
        self._classifier_evaluation.inspect_region_requested.connect(self._inspect_classifier_request)
        self._classifier_evaluation.set_inspection_available(True)
        detail_layout.addWidget(self._classifier_evaluation)
        self._classifier_cv = ClassifierCrossValidationPanel(self._stages)
        self._classifier_cv.matrix.inspect_region_requested.connect(self._inspect_classifier_request)
        self._classifier_cv.matrix.set_inspection_available(True)
        detail_layout.addWidget(self._classifier_cv)
        from .figure_preview import _FigureView
        self._preview = QWidget(details)
        preview_layout = QVBoxLayout(self._preview)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(8)
        self._preview_view = _FigureView(QPixmap(), self._preview)
        self._preview_view.setFixedHeight(470)
        preview_layout.addWidget(self._preview_view)
        preview_actions = QHBoxLayout()
        for text, action in (
            ("Zoom out", lambda: self._preview_view.zoom_by(1 / 1.2)),
            ("Zoom in", lambda: self._preview_view.zoom_by(1.2)),
            ("Fit figure", self._preview_view.reset_view),
        ):
            button = QPushButton(text, self._preview)
            button.clicked.connect(action)
            preview_actions.addWidget(button)
        preview_actions.addStretch()
        preview_layout.addLayout(preview_actions)
        detail_layout.addWidget(self._preview)
        self._stage_file_toggle = QToolButton(self._stages)
        self._stage_file_toggle.setText("Saved result file")
        self._stage_file_toggle.setCheckable(True)
        configure_disclosure(self._stage_file_toggle)
        self._stage_file_toggle.toggled.connect(self._stage_file.setVisible)
        detail_layout.addWidget(self._stage_file_toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        detail_layout.addWidget(self._stage_file)
        self._stage_file.hide()
        detail_layout.addStretch(1)
        self._preview.hide()
        self._stages.hide()
        layout.addWidget(self._stages)

        self._outputs = QFrame(content)
        self._outputs.setObjectName("results-output-card")
        self._outputs.setProperty("workbenchSection", True)
        output_layout = QVBoxLayout(self._outputs)
        output_layout.setContentsMargins(20, 18, 20, 18)
        output_layout.setSpacing(12)
        output_heading = QHBoxLayout()
        output_label = self._output_heading = QLabel("Accepted outputs", self._outputs)
        output_label.setObjectName("results-outputs-heading")
        output_label.setProperty("panelHeading", True)
        output_label.setWordWrap(True)
        output_heading.addWidget(output_label, 1)
        self._output_sample = QComboBox(self._outputs)
        self._output_sample.setAccessibleName("Accepted output sample")
        self._output_sample.setMinimumContentsLength(14)
        self._output_sample.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._output_sample.currentIndexChanged.connect(self._show_sample_locations)
        output_heading.addWidget(self._output_sample, 1)
        output_layout.addLayout(output_heading)

        locations = QFrame(self._outputs)
        locations.setObjectName("results-selected-locations")
        locations_layout = QVBoxLayout(locations)
        locations_layout.setContentsMargins(0, 0, 0, 0)
        locations_layout.setSpacing(9)
        path_grid = QGridLayout()
        path_grid.setHorizontalSpacing(16)
        path_grid.setVerticalSpacing(8)
        path_grid.setColumnStretch(1, 1)
        for row, (field, title) in enumerate(
            (
                ("processed_image_path", "Prepared OME-Zarr image"),
                ("path", "Analysis result"),
                ("cell-data", "Cell-data reference"),
            )
        ):
            caption = self._caption(title, locations)
            path = _LocationLabel(locations)
            path.setObjectName(f"results-location-{field}")
            path.setProperty("resultsLocation", True)
            path.setTextFormat(Qt.TextFormat.PlainText)
            path.setMinimumWidth(0)
            path.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            path.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
                | Qt.TextInteractionFlag.TextSelectableByKeyboard
            )
            path.setAccessibleName(title + " location")
            caption.setBuddy(path)
            path_grid.addWidget(caption, row, 0, Qt.AlignmentFlag.AlignVCenter)
            path_grid.addWidget(path, row, 1)
            copy = ContentSizedButton("Copy path", locations)
            copy.setObjectName(f"results-copy-{field}")
            copy.setAccessibleName("Copy " + title.lower() + " path")
            copy.clicked.connect(lambda _checked=False, field=field: self._copy_location(field))
            path_grid.addWidget(copy, row, 2)
            self._location_labels[field] = path
            self._location_copy_buttons[field] = copy
        locations_layout.addLayout(path_grid)
        self._data_help = self._caption(
            "The cell-data reference points to separate datasets. Keep those datasets available when opening the saved cell data.",
            locations,
        )
        self._data_help.setObjectName("results-data-help")
        locations_layout.addWidget(self._data_help)
        output_layout.addWidget(locations)
        self._outputs.hide()
        layout.addWidget(self._outputs)

        records = QWidget(content)
        record_layout = QVBoxLayout(records)
        record_layout.setContentsMargins(0, 0, 0, 0)
        record_layout.setSpacing(8)
        self._details_toggle = QToolButton(records)
        self._details_toggle.setObjectName("results-details-toggle")
        self._details_toggle.setText("Processing information")
        self._details_toggle.setCheckable(True)
        configure_disclosure(self._details_toggle)
        record_layout.addWidget(self._details_toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        self._details = QFrame(records)
        self._details.setObjectName("results-details")
        self._details.setProperty("workbenchSection", True)
        details_layout = QVBoxLayout(self._details)
        details_layout.setContentsMargins(16, 14, 16, 14)
        details_layout.setSpacing(12)
        detail_header = QHBoxLayout()
        record_label = QLabel("Records", self._details)
        self._record_choice = QComboBox(self._details)
        self._record_choice.setObjectName("results-record-choice")
        self._record_choice.setAccessibleName("Processing record type")
        record_label.setBuddy(self._record_choice)
        detail_header.addWidget(record_label)
        detail_header.addWidget(self._record_choice, 1)
        details_layout.addLayout(detail_header)
        self._record_stack = QStackedWidget(self._details)
        self._record_stack.setObjectName("results-record-stack")
        for table_id, heading in (
            ("domain-summaries", "Processing groups"),
            ("provenance", "Saved settings and result history"),
            ("subjects", "Sample and subject assignments"),
            ("spatial-handoffs", "Cell-data references"),
            ("records", "Output verification records"),
        ):
            table = self._new_table(table_id, self._record_stack)
            table.setAccessibleName(heading)
            self._record_choice.addItem(heading, table_id)
            self._record_stack.addWidget(table)
        self._record_choice.currentIndexChanged.connect(self._select_records)
        details_layout.addWidget(self._record_stack)
        self._details_toggle.toggled.connect(self._details.setVisible)
        self._details.hide()
        record_layout.addWidget(self._details)
        records.hide()
        self._records = records
        layout.addWidget(records)

        self._continuation = QFrame(content)
        self._continuation.setProperty("workbenchSection", True)
        continuation_layout = QGridLayout(self._continuation)
        continuation_layout.setContentsMargins(18, 14, 18, 14)
        continuation_layout.setHorizontalSpacing(16)
        continuation_layout.setVerticalSpacing(10)
        continuation_label = QLabel("Continue analysis", self._continuation)
        continuation_label.setProperty("panelHeading", True)
        continuation_layout.addWidget(continuation_label, 0, 0, 1, 2)
        self._spatial_status = self._caption("", self._continuation)
        self._spatial_status.setObjectName("results-spatial-status")
        self._patient_status = self._caption("", self._continuation)
        self._patient_status.setObjectName("results-patient-status")
        self._spatial = self._button("Run spatial analysis", "results-spatial", self._emit_spatial)
        self._patient = self._button("Run patient analysis", "results-patient", self._emit_patient)
        continuation_layout.addWidget(self._spatial_status, 1, 0)
        continuation_layout.addWidget(self._spatial, 1, 1)
        continuation_layout.addWidget(self._patient_status, 2, 0)
        continuation_layout.addWidget(self._patient, 2, 1)
        continuation_layout.setColumnStretch(0, 1)
        self._continuation.hide()
        layout.addWidget(self._continuation)
        layout.addStretch(1)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        action_bar = QWidget(self)
        action_bar.setObjectName("workbench-action-bar")
        action_layout = QHBoxLayout(action_bar)
        action_layout.setContentsMargins(24, 12, 24, 12)
        action_layout.setSpacing(10)
        action_scope = self._caption("Analysis files", action_bar)
        action_scope.setObjectName("results-action-scope")
        action_layout.addWidget(action_scope, 1)
        self._open_qupath = self._button(
            "Open QuPath", "results-open-qupath", self._emit_open_qupath
        )
        self._open_qupath.setProperty("actionRole", "primary")
        self._open_plot = self._button("Open plot", "results-open-plot", self._emit_open_plot)
        self._reveal = self._button("Show in folder", "results-reveal-path", self._emit_reveal_path)
        for button in (self._open_plot, self._reveal, self._open_qupath):
            action_layout.addWidget(button)
        for button in (
            self._open_qupath, self._open_plot, self._reveal, self._spatial, self._patient
        ):
            button.setEnabled(False)
            button.hide()
        self._action_bar = action_bar
        action_bar.hide()
        outer.addWidget(action_bar)

        self._copy_shortcut = QShortcut(QKeySequence.StandardKey.Copy, self)
        self._copy_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._copy_shortcut.activated.connect(self._copy_selected_cells)
        apply_widget_accessibility(self, "results")

    @staticmethod
    def _caption(text: str, parent: QWidget) -> QLabel:
        """Create wrapping secondary copy without interpreting paths as rich text."""
        label = QLabel(text, parent)
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setProperty("resultsCaption", True)
        return label

    def clear_results(self, message="Loading saved results…") -> None:
        """Remove prior-head figure and final-publication actions together."""
        self._classifier_error_request = None
        self._classifier_location_rows = ()
        self._classifier_cell_request = None
        self._classifier_evaluation.hide()
        self._classifier_cv.hide()
        self._inspect_classifier_region.hide()
        self._saved_head = None
        self._saved_stages = ()
        self._scope_selections.clear()
        self._selection_context = None
        self._saved_plots = ()
        self._stage_choice.clear()
        self._sample_choice.clear()
        self._stages.hide()
        self._bound_routes = None
        for widget in (self._outputs, self._records, self._continuation, self._action_bar):
            widget.hide()
        for button in (self._spatial, self._patient, self._open_qupath, self._open_plot):
            button.setEnabled(False)
        self._empty.setText(message)
        self._empty.setVisible(bool(message))

    @staticmethod
    def _stage_label(stage):
        """Distinguish saved result kinds within one scientific stage."""
        suffixes = {
            "Accepted classifiers": "Classifiers",
            "Primary tissue classification": "Regions",
            "Secondary tissue classification": "Regions",
            "Accepted marker gates": "Marker gates",
            "Marker phenotypes": "Marker phenotypes",
            "Unsupervised phenotyping": "Unsupervised phenotyping",
            "Accepted cluster labels": "Cluster labels",
            "Supervised cell phenotypes": "Supervised phenotypes",
        }
        return suffixes.get(stage.title, stage.title)

    @staticmethod
    def _figure_title(figure):
        """Replace the default internal classifier name only in displayed labels."""
        return re.sub(r"^primary-stage-settings(?= ·|$)", "Primary tissue classifier", figure.title)

    def set_saved_stages(self, snapshot) -> None:
        """Replace the inventory, retaining the selected result when still accepted."""
        self._remember_result_selection()
        previous_group = self._stage_choice.currentData(Qt.ItemDataRole.UserRole + 1)
        self._saved_head = snapshot.state_head_sha256
        self._saved_stages = snapshot.stages
        self._populate_stage_groups(previous_group)
        self._stages.setVisible(bool(snapshot.stages))
        self._empty.setText("No completed stages are available for this analysis yet.")
        self._empty.setVisible(not snapshot.stages and self._bound_routes is None)

    def _remember_result_selection(self) -> None:
        selected = self._selected_stage()
        if selected is not None and self._selection_context is not None:
            figure = self._figure_choice.currentData()
            self._scope_selections[self._selection_context] = (
                selected.artifact_ref_sha256, figure.figure_id if figure else None)

    @staticmethod
    def _additional_figure_group(figure):
        """Associate final-cell views with the scientific stage they describe."""
        if figure.figure_id.startswith("compartments/"):
            return "classification"
        if figure.figure_id.startswith("secondary-"):
            return "structures"
        if figure.figure_id.startswith("distribution/"):
            return "measurements"
        return "phenotyping"

    def _view_figures(self, stage):
        """Offer each named figure directly within its scientific stage."""
        if stage.stage_kind == "phenotype_handoff":
            group = self._stage_choice.currentData(Qt.ItemDataRole.UserRole + 1)
            return tuple(figure for figure in stage.figures
                         if self._additional_figure_group(figure) == group)
        return stage.figures

    def _populate_stage_groups(self, previous_group=None) -> None:
        from ..presenters.workflow import BY_KIND, STAGES
        definitions = {definition.key: definition for definition in STAGES}
        groups = {}
        for stage in self._saved_stages:
            definition = BY_KIND.get(stage.stage_kind)
            if stage.stage_kind == "phenotype_handoff":
                keys = tuple(dict.fromkeys(self._additional_figure_group(figure)
                                           for figure in stage.figures)) or ("phenotyping",)
            else:
                keys = (definition.key if definition else stage.stage_kind,)
            for key in keys:
                groups.setdefault(key, []).append(stage)
        ordered = [key for key in definitions if key in groups]
        ordered.extend(key for key in groups if key not in definitions)
        self._stage_choice.blockSignals(True)
        self._stage_choice.clear()
        selected_group = next((index for index, key in enumerate(ordered)
            if any(stage.stage_kind != "phenotype_handoff" for stage in groups[key])),
            ordered.index("phenotyping") if "phenotyping" in ordered else 0)
        for index, key in enumerate(ordered):
            stages = groups[key]
            title = definitions[key].title if key in definitions else stages[0].title
            self._stage_choice.addItem(title, tuple(stages))
            self._stage_choice.setItemData(index, key, Qt.ItemDataRole.UserRole + 1)
            self._stage_choice.setItemData(index, title, Qt.ItemDataRole.ToolTipRole)
            if key == previous_group:
                selected_group = index
        self._stage_choice.setCurrentIndex(selected_group if groups else -1)
        self._stage_choice.blockSignals(False)
        self._stage_choice.setToolTip(self._stage_choice.currentText())
        self._populate_stage_samples()

    def _select_stage_group(self, *_args) -> None:
        self._remember_result_selection()
        self._populate_stage_samples()

    def _populate_stage_samples(self) -> None:
        context = self._stage_choice.currentData(Qt.ItemDataRole.UserRole + 1)
        previous, figure = self._scope_selections.get(context, (None, None))
        selected_stage = self._selected_stage()
        samples_before = selected_stage.sample_ids if selected_stage is not None else ()
        stages = self._stage_choice.currentData() or ()
        self._sample_choice.blockSignals(True)
        self._sample_choice.clear()
        selected = next((index for index, stage in enumerate(stages)
                         if stage.sample_ids == samples_before), 0)
        kinds = {stage.title for stage in stages}
        for index, stage in enumerate(stages):
            samples = stage.sample_ids
            label = (samples[0] if len(samples) == 1 else
                     f"Shared result · {len(samples)} samples" if samples else "Analysis-wide result")
            if len(stages) > 1 and len(samples) > 1:
                label = f"{samples[0]} + {len(samples) - 1} more"
            if len(kinds) > 1:
                label += f" · {self._stage_label(stage)}"
            self._sample_choice.addItem(label, stage)
            self._sample_choice.setItemData(index, ", ".join(samples), Qt.ItemDataRole.ToolTipRole)
            if stage.artifact_ref_sha256 == previous:
                selected = index
        self._sample_choice.setCurrentIndex(selected if stages else -1)
        self._sample_choice.blockSignals(False)
        self._sample_choice.setToolTip(self._sample_choice.currentText())
        self._selection_context = context
        self._select_stage()
        if previous is not None and figure is not None:
            index = next((i for i in range(self._figure_choice.count())
                          if self._figure_choice.itemData(i).figure_id == figure), -1)
            if index >= 0:
                self._figure_choice.setCurrentIndex(index)

    def saved_results_failed(self, message: str) -> None:
        self._empty.setText(message)
        self._empty.show()

    def _selected_stage(self):
        return self._sample_choice.currentData()

    def _open_saved_assignments(self):
        stage = self._selected_stage()
        if stage is not None and stage.stage_kind == 'supervised_phenotype_inference' and self._saved_head is not None:
            self.supervised_review_requested.emit(stage.artifact_ref_sha256, self._saved_head)

    def _open_saved_outlines(self):
        stage = self._selected_stage()
        if stage is not None and stage.stage_kind == 'segmentation' and self._saved_head is not None:
            self.segmentation_review_requested.emit(stage.artifact_ref_sha256, self._saved_head)

    def _select_stage(self, *_args) -> None:
        self._saved_alignment_request = None
        stage = self._selected_stage()
        review = stage is not None and stage.stage_kind != "phenotype_handoff"
        self._review_assignments.setVisible(review and stage is not None and stage.stage_kind == 'supervised_phenotype_inference')
        self._review_outlines.setVisible(review and stage is not None and stage.stage_kind == 'segmentation')
        self._saved_alignment_identity = ((self._saved_head, stage.artifact_ref_sha256)
            if stage is not None and stage.reference_display is not None else None)
        self._sample_choice.setAccessibleDescription(", ".join(stage.sample_ids) if stage else "")
        path = stage.path if stage is not None else None
        self._stage_location.set_location(path)
        self._stage_reveal.setEnabled(path is not None)
        self._stage_copy.setEnabled(path is not None)
        self._stage_location.setVisible(path is not None)
        self._stage_reveal.setVisible(path is not None)
        self._stage_copy.setVisible(path is not None)
        self._stage_file_toggle.setVisible(path is not None)
        self._stage_file_toggle.setChecked(False)
        figures = self._view_figures(stage) if stage is not None else ()
        self._stage_message.setText(stage.message if stage is not None else "")
        self._stage_message.setVisible(bool(self._stage_message.text()) and not figures
            and stage.stage_kind != 'supervised_phenotype_inference')
        self._stage_file_note.setText(stage.message if stage is not None and figures else "")
        self._stage_file_note.setVisible(bool(self._stage_file_note.text()))
        self._figure_choice.blockSignals(True)
        self._figure_choice.clear()
        if stage is not None and stage.stage_kind == "phenotype_handoff":
            # Keep final summaries discoverable before the individual measurements.
            order = ("composition/", "compartments/", "profiles/", "coexpression/all",
                     "coexpression/pair/", "coexpression/selected", "coexpression/rules/",
                     "secondary-", "cell-gallery", "distribution/")
            figures = sorted(figures, key=lambda figure: next(
                (index for index, prefix in enumerate(order) if figure.figure_id.startswith(prefix)), len(order)))
        for figure in figures:
            self._figure_choice.addItem(self._figure_title(figure), figure)
            self._figure_choice.setItemData(self._figure_choice.count()-1, self._figure_title(figure), Qt.ItemDataRole.ToolTipRole)
        self._figure_choice.blockSignals(False)
        self._figure_choice.setToolTip(self._figure_choice.currentText())
        self._select_saved_figure()

    def _select_saved_figure(self, *_args) -> None:
        self._gallery_colors = None
        self._classifier_error_request = None
        self._classifier_location_rows = ()
        self._classifier_cell_request = None
        self._classifier_evaluation.hide()
        self._classifier_cv.hide()
        self._inspect_classifier_region.hide()
        self._classifier_table_choice.hide()
        self._classifier_table.hide()
        self._saved_plots = ()
        self._preview.hide()
        self._stage_larger.hide()
        figure = self._figure_choice.currentData()
        available = figure is not None
        self._figure_choice.setVisible(available)
        self._figure_caption.setVisible(available)
        self._outline_palette_row.setVisible(available and figure.figure_id in {"region-outlines", "secondary-outlines"})
        from mif_pipeline.cell_analysis.figure_style import supports_result_export_palette
        self._export_palette_row.setVisible(available and supports_result_export_palette(figure.figure_id))
        combinations = available and figure.figure_id.startswith('coexpression/') and not figure.figure_id.startswith('coexpression/rules/')
        self._preview_view.setFixedHeight(640 if combinations else 470)
        detail = figure.detail if available else ""
        if available and figure.figure_id.startswith("distribution/"):
            detail = ""
        if available and figure.figure_id.startswith("composition/"):
            detail = "Cell counts and percentages of all exported cells in this sample."
        if available and figure.figure_id.startswith("profiles/"):
            detail = "Arithmetic mean marker intensities by phenotype, on the saved measurement scale. Row labels show cell counts."
        if available and figure.figure_id.startswith("compartments/"):
            detail = "Phenotype percentages within each tissue compartment. Column labels show the compartment's cell count."
        if combinations:
            detail = "Cell counts and percentages among cells with known calls for all selected markers."
        if available and figure.figure_id == "classifier-recorded-tables":
            detail = "Model settings, training subjects and contributions, evaluation scores and reference locations."
        self._figure_detail.setText(detail)
        self._figure_detail.setToolTip(figure.detail if available else '')
        self._figure_detail.setVisible(bool(detail))
        self._figure_status.clear()
        self._figure_status.hide()
        from .reconstruction_settings import ReconstructionSettings
        stage = self._selected_stage()
        reconstruction_context = (self._saved_head, stage.artifact_ref_sha256) if stage is not None else None
        is_reconstruction = available and figure.figure_id in {"acquired-components", "reconstruction-diagnostic", "reconstruction-spectra"}
        reuse_reconstruction = (is_reconstruction and isinstance(self._gallery_settings, ReconstructionSettings)
            and getattr(self._gallery_settings, "result_identity", None) == reconstruction_context)
        if self._gallery_settings is not None and not reuse_reconstruction:
            self._gallery_layout.removeWidget(self._gallery_settings)
            self._gallery_settings.deleteLater()
            self._gallery_settings = None
        self._gallery_revision += 1
        if available and figure.figure_id == "cell-gallery":
            from .gallery_settings import GallerySettings
            self._gallery_settings = GallerySettings(figure.settings, self._gallery_container, selection=True)
            from ..image_appearance import load_image_appearance
            appearance = load_image_appearance(getattr(self, 'image_appearance_path', None),
                                              figure.settings.get('image_uri', ''))
            if appearance is not None:
                settings = self._gallery_settings
                for index, (checked, _low, _high) in settings.editors.items():
                    checked.setChecked(index in appearance.get('channels', ()))
                settings.channel_display.setCurrentIndex(settings.channel_display.findData(
                    'grayscale' if appearance.get('mode') == 'grayscale' else 'colour'))
                settings.merged_panel.setChecked(appearance.get('mode') == 'composite')
                for index, (_checked, low, high) in settings.editors.items():
                    limits = appearance.get('limits', {}).get(str(index))
                    if limits is not None:
                        low.setText(repr(limits[0])); high.setText(repr(limits[1]))
                self._gallery_colors = appearance.get('colors')
        elif available and figure.figure_id.startswith("distribution/"):
            from .measurement_distribution import MeasurementDistributionReview
            self._gallery_settings = MeasurementDistributionReview(self._gallery_container)
            self._gallery_settings.requested.connect(lambda: self._request_saved_figure("open"))
            self._gallery_settings.field_requested.connect(self._request_measurement_field)
        elif available and figure.figure_id == 'coexpression/selected':
            from .coexpression_settings import CoexpressionSettings
            self._gallery_settings = CoexpressionSettings(figure.settings, self._gallery_container)
        elif available and figure.figure_id == "umap-current":
            from .umap_review import UmapReviewControls
            self._gallery_settings = UmapReviewControls(self._gallery_container)
            self._gallery_settings.set_measurements(figure.settings["measurements"])
            for button in (self._gallery_settings.calculate, self._gallery_settings.open_view,
                           self._gallery_settings.export_view):
                button.hide()
        elif is_reconstruction:
            if not reuse_reconstruction:
                self._gallery_settings = ReconstructionSettings(figure.settings, self._gallery_container)
                self._gallery_settings.result_identity = reconstruction_context
                self._gallery_settings.region_requested.connect(self.saved_region_requested)
                self._gallery_settings.choose_region.setEnabled(not getattr(self, '_region_selection_busy', False))
            self._gallery_settings.set_figure(figure.figure_id)
        if self._gallery_settings is not None and not reuse_reconstruction:
            self._gallery_settings.changed.connect(self._gallery_changed)
            self._gallery_layout.addWidget(self._gallery_settings)
        self._gallery_container.setVisible(self._gallery_settings is not None)
        self._stage_data.setVisible(available and bool(figure.data_formats))
        self._stage_data.setEnabled(available and bool(figure.data_formats))
        tables = available and figure.figure_id in {"classifier-recorded-tables", "classified-region-counts", "secondary-structure-tables"}
        data_only = available and figure.figure_id == 'cluster-memberships'
        alignment = stage is not None and stage.reference_display is not None
        self._stage_open.setText("Inspect aligned images" if alignment else
                                "View recorded tables" if tables else "Preview figure")
        self._stage_open.setEnabled((available and not data_only) or alignment)
        self._stage_open.setVisible((available and not data_only) or alignment)
        self._stage_export.setEnabled(available and not tables and not data_only)
        self._stage_export.setVisible(available and not tables and not data_only)
        distribution=available and figure.figure_id.startswith('distribution/')
        self._stage_export.setText('Export distribution…' if distribution else 'Export figure…')
        self._stage_data.setText('Distribution data…' if distribution else 'Export data…')
        if available and figure.figure_id == 'coexpression/selected':
            self._gallery_changed()

    def _request_measurement_field(self):
        """Read one bounded tissue field for the current inclusive measurement range."""
        from .measurement_distribution import MeasurementDistributionReview
        if not isinstance(self._gallery_settings,MeasurementDistributionReview):
            return
        stage = self._selected_stage()
        gallery = next((figure for figure in stage.figures
                        if figure.figure_id == "cell-gallery"), None) if stage is not None else None
        if gallery is None:
            self._figure_status.setText("The accepted cells’ image is unavailable.")
            self._figure_status.show()
            return
        from ..image_appearance import load_image_appearance
        appearance = load_image_appearance(getattr(self, 'image_appearance_path', None),
                                          gallery.settings.get('image_uri', ''))
        options = self._gallery_settings.field_options(gallery.settings, appearance)
        self._measurement_field_revision=getattr(self,'_measurement_field_revision',0)+1
        self._figure_status.setText("Loading tissue field…")
        self._figure_status.show()
        identity = self.saved_figure_identity()
        self.measurement_field_requested.emit(identity[1],options)

    def measurement_field_identity(self):
        identity=self.saved_figure_identity()
        return (*identity,getattr(self,'_measurement_field_revision',0)) if identity is not None else None

    def set_measurement_field(self,identity,bundle):
        """A stale field cannot replace a newer range, viewport or saved result."""
        from .measurement_distribution import MeasurementDistributionReview
        if identity!=self.measurement_field_identity() or not isinstance(self._gallery_settings,MeasurementDistributionReview):
            return False
        self._gallery_settings.set_field_bundle(bundle)
        self._figure_status.hide()
        return True

    def measurement_field_failed(self,identity,message):
        if identity==self.measurement_field_identity():
            self._figure_status.setText(message)
            self._figure_status.show()

    def saved_figure_identity(self):
        stage, figure = self._selected_stage(), self._figure_choice.currentData()
        if stage is None or figure is None:
            return None
        identity = (self._saved_head, stage.artifact_ref_sha256, figure.figure_id)
        if self._classifier_cell_request is not None:
            return (*identity, tuple(self._classifier_cell_request[key] for key in
                ('classifier', 'split', 'project_entry_id', 'cell_id')))
        return (*identity, self._gallery_revision) if self._gallery_settings is not None or figure.figure_id == "region-outlines" else identity

    def set_region_selection_busy(self, busy):
        self._region_selection_busy = bool(busy)
        from .reconstruction_settings import ReconstructionSettings
        if isinstance(self._gallery_settings, ReconstructionSettings):
            self._gallery_settings.choose_region.setEnabled(not busy)

    def apply_saved_region(self, identity, region):
        """A native return updates only the unchanged figure and manual-field revision."""
        from .reconstruction_settings import ReconstructionSettings
        if identity != self.saved_figure_identity() or not isinstance(self._gallery_settings, ReconstructionSettings):
            return False
        if region is not None:
            self._gallery_settings.apply_region(region)
        return True

    def saved_figure_options(self, *, action='open'):
        if action == 'open' and self._classifier_cell_request is not None:
            return {'supervised_gallery_settings': dict(self._classifier_cell_request)}
        figure = self._figure_choice.currentData()
        if figure is not None and figure.figure_id in {"region-outlines", "secondary-outlines"}:
            return {"palette": self._outline_palette.currentData()}
        options = self._gallery_settings.options() if self._gallery_settings is not None else None
        if options is not None and figure.figure_id == "cell-gallery" and self._gallery_colors:
            options['channel_colors'] = self._gallery_colors
        if action == 'export' and not self._export_palette_row.isHidden() and self._export_palette.currentData() != 'default':
            options = {**(options or {}), 'export_palette': self._export_palette.currentData()}
        return options

    def _gallery_changed(self):
        self._figure_status.clear()
        self._figure_status.hide()
        self._preview.hide()
        self._stage_open.show()
        self._stage_larger.hide()
        self._gallery_revision += 1
        self._saved_plots = ()
        valid = True
        figure = self._figure_choice.currentData()
        if figure is not None and (figure.figure_id.startswith("distribution/")
                                   or figure.figure_id == 'coexpression/selected'):
            try:
                self.saved_figure_options()
            except ValueError:
                valid = False
        self._stage_open.setEnabled(valid)
        self._stage_export.setEnabled(valid)
        self._stage_data.setEnabled(valid)

    def _request_saved_figure(self, action: str) -> None:
        stage = self._selected_stage()
        if action == "open" and stage is not None and stage.reference_display is not None:
            self._saved_alignment_request = (*self.saved_alignment_review_identity(), dict(stage.reference_display))
            self.open_qupath_requested.emit()
            return
        try:
            options = self.saved_figure_options(action=action)
        except ValueError as error:
            self._figure_status.setText(str(error)); self._figure_status.show()
            return
        identity = self.saved_figure_identity()
        if identity is None:
            return
        if self._saved_plots and not (options or {}).get('export_palette') and not (options or {}).get('supervised_gallery_settings'):
            self._use_saved_figure(action)
        else:
            self._figure_status.setText("Preparing the saved figure…")
            self._figure_status.show()
            self._stage_open.setEnabled(False)
            self._stage_export.setEnabled(False)
            self._stage_data.setEnabled(False)
            self.saved_figure_requested.emit(identity[1], identity[2], action)

    def set_saved_figures(self, identity, plots, action: str) -> bool:
        if identity != self.saved_figure_identity():
            return False
        if action == 'open' and self._classifier_cell_request is not None:
            options = plots[0].display_options if plots else None
            if not options or options.get('supervised_request') != self._classifier_cell_request:
                self.saved_figure_failed(identity, 'The selected reference cell could not be opened.')
                return False
            request = options.get('qupath_review')
            if not request:
                self.saved_figure_failed(identity, 'The recorded cell geometry is unavailable.')
                return False
            self._classifier_cell_request = None
            self._inspect_classifier_region.setEnabled(True)
            self._stage_data.setEnabled(True)
            self._figure_status.hide()
            self._inspect_classifier_request(request)
            return True
        if action in {'export', 'data'}:
            available = tuple(plot for plot in plots if plot.status == 'available')
            self._stage_open.setEnabled(bool(available))
            self._stage_export.setEnabled(bool(available))
            self._stage_data.setEnabled(any(Path(plot.path).suffix in {'.csv', '.npz'} for plot in available))
            self._figure_status.setText('' if available else 'No saved figure is available for this result.')
            self._figure_status.setVisible(not available)
            if available:
                self._export_saved_plots(available, action)
            return True
        self._saved_plots = tuple(plot for plot in plots if plot.status == "available")
        if self._gallery_settings is not None and self._saved_plots:
            options = self._saved_plots[0].display_options
            if options is not None:
                self._gallery_settings.set_resolved(options)
        self._stage_open.setEnabled(bool(self._saved_plots))
        self._stage_export.setEnabled(bool(self._saved_plots))
        self._stage_data.setEnabled(any(Path(plot.path).suffix in {".csv", ".npz"} for plot in self._saved_plots))
        self._figure_status.setText("" if self._saved_plots else "No saved figure is available for this result.")
        self._figure_status.setVisible(not self._saved_plots)
        if self._saved_plots:
            self._use_saved_figure(action)
        return True

    def saved_figure_failed(self, identity, message: str) -> None:
        if identity == self.saved_figure_identity():
            self._classifier_cell_request = None
            self._inspect_classifier_region.setEnabled(self._classifier_table.currentIndex().isValid())
            self._figure_status.setText(message)
            self._figure_status.show()
            self._stage_open.setEnabled(True)
            self._stage_export.setEnabled(True)
            self._stage_data.setEnabled(True)

    def _show_classifier_table(self):
        self._classifier_error_request = None
        self._classifier_location_rows = ()
        self._classifier_cell_request = None
        self._classifier_evaluation.hide()
        self._classifier_cv.hide()
        self._inspect_classifier_region.hide()
        item = self._classifier_table_choice.currentData()
        if item is None:
            return
        from .classifier_review_data import read_artifact_rows
        labels = {}
        for row in read_artifact_rows(self._saved_plots, 'classifier-fitting-classes'):
            labels.setdefault(row['classifier'], {})[row['class_id']] = row['class_name']
        panel = (self._classifier_evaluation if item.artifact_id == 'classifier-confusion-data/csv' else
                 self._classifier_cv if item.artifact_id in {'classifier-cv-confusion-data/csv', 'classifier-cv-reference-locations/csv'} else None)
        if panel is not None:
            if panel is self._classifier_evaluation:
                panel._class_labels = labels
            else:
                panel.matrix._class_labels = labels
            available = panel.set_artifacts(self._saved_plots)
            if not available:
                self._figure_status.setText('The saved classifier details could not be opened. Reopen this result to try again.')
                self._figure_status.show()
            self._classifier_table.hide()
            return
        import csv
        from .classifier_review_data import set_table
        with Path(item.path).open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            columns = tuple(reader.fieldnames or ())
            rows = tuple(reader)
            if item.artifact_id == "classified-region-counts/csv":
                number = lambda value: f"{float(value):,.2f}" if value != '' else '—'
                visible = [(row['class_name'], int(row['region_count']), number(row['area_um2']),
                            f"{100*float(row['fraction_of_analysis_domain']):.2f}",
                            number(row['median_region_area_um2'])) for row in rows]
                if rows:
                    from math import fsum
                    visible.append(("All classes", int(rows[0]['total_regions']),
                                    f"{fsum(float(row['area_um2']) for row in rows):,.2f}",
                                    f"{100*fsum(float(row['fraction_of_analysis_domain']) for row in rows):.2f}", '—'))
                set_table(self._classifier_table,
                          ("Class", "Regions", "Area (µm²)", "% of analysis region", "Median area (µm²)"), visible)
            elif item.artifact_id == "classified-region-sizes/csv":
                set_table(self._classifier_table, ("Region", "Class", "Area (µm²)"),
                          ((index+1, row['class_name'], f"{float(row['area_um2']):,.2f}")
                           for index, row in enumerate(rows)))
            elif item.artifact_id.startswith('secondary-'):
                import json
                outcomes = read_artifact_rows(self._saved_plots, 'secondary-outcomes')
                numbers = {row['roi_id']: row['region_number'] for row in outcomes}
                if item.artifact_id == 'secondary-outcomes/csv':
                    labels = {'active_included': 'Included', 'review_rejected': 'Rejected',
                              'classifier_other': 'Classifier background', 'not_accepted': 'Not accepted',
                              'valid_not_selected_by_cap': 'Excluded by maximum count',
                              'audit_excluded_below_minimum': 'Below minimum count'}
                    columns = ('Region', 'Structure', 'Final outcome', 'Subtype', 'Final member cells', 'Area (µm²)', 'Observation')
                    visible = []
                    for r in rows:
                        included = r['final_disposition'] == 'active_included'
                        active = r['domain_status'] == 'active'
                        outcome = ('Analysis region excluded' if included and not active else
                                   labels.get(r['final_disposition'], r['final_disposition'].replace('_', ' ')))
                        visible.append((r['region_number'], r['structure_id'].replace('_', ' '), outcome,
                            r['subtype'] or '—', int(r['final_member_cells']) if included and active else '—',
                            f"{float(r['area_um2']):,.2f}", r['observation']))
                elif item.artifact_id == 'secondary-criteria/csv':
                    columns = ('Region', 'Criterion', 'Measurement', 'Value', 'Unit', 'Outcome')
                    visible = [(numbers.get(r['roi_id'], r['roi_id']), r['kind'].replace('_', ' '),
                        (json.loads(r['selector'] or '{}').get('measurement_id') or '—'),
                        r['value'] or '—', r['unit'] or '—', r['outcome']) for r in rows]
                elif item.artifact_id == 'secondary-count-decisions/csv':
                    columns = ('Structure', 'Observed', 'Minimum', 'Maximum', 'Included', 'Count outcome', 'Selection method')
                    visible = [(r['structure_id'].replace('_', ' '), r['observed_count'], r['minimum'],
                        r['maximum'] or 'No limit', r['included_count'], r['status'].replace('_', ' '),
                        r['selection_method'] or '—') for r in rows]
                else:
                    columns = ('Cell', 'Region', 'Subtype', 'Section')
                    visible = [(r['cell_id'], numbers.get(r['secondary_roi_id'], r['secondary_roi_id']),
                                r['subtype'] or '—', r['section_id']) for r in rows]
                set_table(self._classifier_table, columns, visible)
            else:
                set_table(self._classifier_table, tuple(column.replace('_', ' ').capitalize() for column in columns),
                          (tuple(row[column] for column in columns) for row in rows))
        if item.artifact_id in {"classifier-evaluation-locations/csv", "classifier-evaluation-cells/csv"}:
            self._classifier_location_rows = rows
            self._inspect_classifier_region.setText('Inspect reference cell in QuPath'
                if item.artifact_id == 'classifier-evaluation-cells/csv' else 'Inspect reference region in QuPath')
            self._inspect_classifier_region.show()
            self._inspect_classifier_region.setEnabled(False)
            self._classifier_table.selectionModel().currentRowChanged.connect(
                self._classifier_location_selected)
            if rows:
                self._classifier_table.selectRow(0)
        self._classifier_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self._classifier_table.horizontalHeader().setMaximumSectionSize(420)
        self._classifier_table.show()

    def _classifier_location_selected(self, current, _previous):
        self._classifier_cell_request = None
        self._inspect_classifier_region.setEnabled(current.isValid())
        self._stage_data.setEnabled(bool(self._saved_plots))

    def _inspect_saved_classifier_region(self):
        current = self._classifier_table.currentIndex()
        if not current.isValid() or current.row() >= len(self._classifier_location_rows):
            return
        row = self._classifier_location_rows[current.row()]
        if self._classifier_table_choice.currentData().artifact_id == 'classifier-evaluation-cells/csv':
            self._classifier_cell_request = {key: row[key] for key in
                ('classifier', 'split', 'project_entry_id', 'cell_id')}
            self._classifier_cell_request.update(channel_display='colour', merged_panel=True)
            self._inspect_classifier_region.setEnabled(False)
            self._request_saved_figure('open')
            return
        from .classifier_review_data import recorded_region_request
        self._inspect_classifier_request(recorded_region_request(row))

    def _inspect_classifier_request(self, request):
        self._classifier_error_request = request
        self.open_qupath_requested.emit()

    def classifier_error_review_request(self):
        request, self._classifier_error_request = self._classifier_error_request, None
        return request

    def saved_alignment_review_identity(self):
        """Identify the currently selected approved alignment without opening it."""
        return self._saved_alignment_identity

    def take_saved_alignment_review_request(self):
        """Consume an explicit inspection click for the still-selected result."""
        request, self._saved_alignment_request = self._saved_alignment_request, None
        if request is not None and request[:2] == self.saved_alignment_review_identity():
            return request
        return None

    def _use_saved_figure(self, action: str) -> None:
        if action in {'export', 'data'}:
            self._export_saved_plots(self._saved_plots, action)
            return
        figure = self._figure_choice.currentData()
        if figure is not None and figure.figure_id in {"classifier-recorded-tables", "classified-region-counts", "secondary-structure-tables"}:
            previous = self._classifier_table_choice.currentData()
            self._classifier_table_choice.blockSignals(True)
            self._classifier_table_choice.clear()
            for plot in self._saved_plots:
                if Path(plot.path).suffix == '.csv':
                    self._classifier_table_choice.addItem(plot.title, plot)
                    if previous is not None and previous.artifact_id == plot.artifact_id:
                        self._classifier_table_choice.setCurrentIndex(self._classifier_table_choice.count()-1)
            self._classifier_table_choice.blockSignals(False)
            self._classifier_table_choice.setVisible(self._classifier_table_choice.count() > 1)
            self._show_classifier_table()
            self._stage_open.hide()
        paths = {Path(plot.path).suffix.lower().lstrip("."): Path(plot.path) for plot in self._saved_plots}
        if figure is not None and figure.figure_id.startswith("distribution/"):
            self._preview.hide()
            self._stage_open.hide()
            self._stage_larger.hide()
        elif "png" in paths:
            pixmap = QPixmap(str(paths['png']))
            if not pixmap.isNull():
                scene = self._preview_view.scene()
                scene.clear()
                scene.addPixmap(pixmap).setTransformationMode(Qt.TransformationMode.SmoothTransformation)
                self._preview_view.setSceneRect(scene.itemsBoundingRect())
                self._preview.show()
                self._stage_open.hide()
                self._stage_larger.show()
                gallery = figure is not None and figure.figure_id == "cell-gallery"
                self._preview_view.setVerticalScrollBarPolicy(
                    Qt.ScrollBarPolicy.ScrollBarAlwaysOn if gallery else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                if gallery:
                    self._preview_view.fit_width()
                else:
                    self._preview_view.reset_view()

    def _export_saved_plots(self, plots, action):
        """Save prepared export bytes without replacing the current preview or camera."""
        from .figure_export import export_plot_files
        selected = self._classifier_table_choice.currentData() if action == 'data' else None
        if selected is not None and any(plot.path == selected.path for plot in plots):
            # The save dialog starts with the table being inspected; its other
            # prepared tables remain available through the existing selector.
            plots = (selected, *(plot for plot in plots if plot.path != selected.path))
        else:
            selected = None
        paths = {Path(plot.path).suffix.lower().lstrip('.'): Path(plot.path) for plot in plots}
        figure = self._figure_choice.currentData()
        name = re.sub(r"[^\w.-]+", "-", selected.title if selected is not None else self._figure_title(figure)).strip(".-") or "saved-result"
        destination = export_plot_files(self, paths, name + ('.csv' if action == 'data' else '.svg'),
            data_only=action == 'data', data_choices={plot.title: Path(plot.path) for plot in plots
                if Path(plot.path).suffix in {'.csv', '.npz'}})
        if destination is not None:
            self._figure_status.setText('Exported ' + destination.name)
            self._figure_status.show()

    def _open_saved_preview(self):
        path = next((Path(plot.path) for plot in self._saved_plots
                     if Path(plot.path).suffix.lower() == ".png"), None)
        if path is not None:
            self.open_plot_requested.emit(path)

    def _reveal_stage(self) -> None:
        stage = self._selected_stage()
        if stage is not None and stage.path is not None:
            self.reveal_path_requested.emit(stage.path)

    def _copy_stage_path(self) -> None:
        if self._stage_location.toolTip():
            QApplication.clipboard().setText(self._stage_location.toolTip())

    def _button(self, text: str, name: str, callback: object) -> QPushButton:
        """Connect a named action while reserving enough space for its visible label."""
        button = ContentSizedButton(text, self)
        button.setObjectName(name)
        button.clicked.connect(callback)  # type: ignore[arg-type]
        return button

    def _new_table(self, table_id: str, parent: QWidget) -> QTableView:
        """Keep result values selectable and copyable while forbidding edits."""
        table = QTableView(parent)
        table.setObjectName(f"results-{table_id}-table")
        style_scientific_table(table)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        table.setAccessibleDescription("Select cells and press Ctrl+C to copy their full values.")
        self._tables[table_id] = table
        return table

    def set_view(
        self,
        bound_routes: BoundSuccessorRoutes,
        *,
        saved_images_available: bool,
        plot_path: Path | None,
        reveal_path: Path | None,
    ) -> None:
        """Replace displayed records and actions with one bound authority snapshot.

        Plot and folder locations describe analysis-wide file actions, independent of the
        selected sample. They must be absolute paths. Successor availability and emitted
        inputs come only from ``bound_routes``; the page neither derives nor runs a service.
        A replacement resets sample selection and closes supporting records so stale detail
        cannot appear alongside the new result.
        """
        if type(bound_routes) is not BoundSuccessorRoutes:
            raise TypeError("Results page requires bound successor routes")
        for label, path in (
            ("plot", plot_path), ("reveal", reveal_path)
        ):
            if path is not None and (not isinstance(path, Path) or not path.is_absolute()):
                raise ValueError(f"{label} path must be an absolute Path")

        view = bound_routes.results_view
        self._bound_routes = bound_routes
        for field, label in self._location_labels.items():
            label.set_location(None)
            self._location_copy_buttons[field].setEnabled(False)
        self._plot_path = plot_path
        self._reveal_path = reveal_path
        self._replace_models(
            {
                "records": view.output_table,
                "domain-summaries": view.domain_summary_table,
                "provenance": view.provenance_table,
                "spatial-handoffs": view.spatial_handoff_table,
                "subjects": view.subject_table,
            }
        )
        count = len(view.output_table.rows)
        self._output_sample.blockSignals(True)
        self._output_sample.clear()
        sample_column = view.output_table.columns.index("sample_id")
        for row in view.output_table.rows:
            self._output_sample.addItem(str(row.values[sample_column]))
        self._output_sample.blockSignals(False)
        self._output_sample.setVisible(count > 1)
        self._output_heading.setText("Accepted outputs" +
            (f" · {self._output_sample.currentText()}" if count == 1 else ""))
        self._outputs.setVisible(count > 0)
        self._records.setVisible(count > 0)
        self._details_toggle.setChecked(False)
        self._record_choice.setCurrentIndex(0)
        self._select_records(0)
        self._empty.setVisible(count == 0)
        self._empty.setText(
            "" if count else "No finished results are available for this analysis yet."
        )
        if count:
            self._show_sample_locations(0)

        for button, path in (
            (self._open_plot, plot_path),
            (self._reveal, reveal_path),
        ):
            button.setEnabled(path is not None)
            button.setVisible(path is not None)
            button.setToolTip("" if path is None else f"Analysis location: {path}")
        self._open_qupath.setEnabled(saved_images_available)
        self._open_qupath.setVisible(saved_images_available)
        self._action_bar.setVisible(
            saved_images_available or plot_path is not None or reveal_path is not None
        )

        for button, status, route, spatial in (
            (self._spatial, self._spatial_status, bound_routes.spatial_route, True),
            (self._patient, self._patient_status, bound_routes.patient_route, False),
        ):
            button.setVisible(route.run_available)
            button.setEnabled(route.run_available)
            status.setText(self._route_status_text(route, spatial=spatial))
            status.setVisible(route.run_available)
        self._continuation.setVisible(
            bound_routes.spatial_route.run_available or bound_routes.patient_route.run_available
        )

    def _show_sample_locations(self, index: int) -> None:
        """Project exact saved locations for the current output row without changing routes."""
        if index < 0 or self._bound_routes is None:
            return
        view = self._bound_routes.results_view
        values = dict(zip(
            view.output_table.columns, view.output_table.rows[index].values, strict=True
        ))
        sample_id = values["sample_id"]
        handoff_columns = view.spatial_handoff_table.columns
        sample_column = handoff_columns.index("sample_id")
        path_column = handoff_columns.index("path")
        cell_path = next(
            (
                row.values[path_column]
                for row in view.spatial_handoff_table.rows
                if row.values[sample_column] == sample_id
            ),
            None,
        )
        values["cell-data"] = cell_path
        for field, label in self._location_labels.items():
            path = values.get(field)
            label.set_location(path)
            self._location_copy_buttons[field].setEnabled(path is not None)
        self._data_help.setVisible(cell_path is not None)

    def _copy_location(self, field: str) -> None:
        """Copy the complete currently selected location without opening a file."""
        path = self._location_labels[field].toolTip()
        if path:
            QApplication.clipboard().setText(path)

    def _select_records(self, index: int) -> None:
        """Fit the disclosed category to its bounded table without retaining empty panel space."""
        self._record_stack.setCurrentIndex(index)
        table = self._record_stack.currentWidget()
        if table is not None:
            self._record_stack.setFixedHeight(table.height())

    @staticmethod
    def _route_status_text(route: SuccessorRouteStatus, *, spatial: bool) -> str:
        """Describe optional routes without presenting file readiness as a runnable service."""
        if spatial:
            return {
                "not_ready": "Cell results are not ready for spatial analysis.",
                "handoff_ready": (
                    "Use the cell-data references with their linked datasets "
                    "in your own analysis environment."
                ),
                "service_available": "Analyse the cell data for all included samples.",
                "spatial_results_ready": "Cell results are not ready for spatial analysis.",
            }[route.state]
        return {
            "not_ready": "Patient analysis requires spatial results.",
            "spatial_results_ready": (
                "Spatial results are available for further analysis "
                "with the saved subject assignments."
            ),
            "service_available": "Analyse the spatial results using the saved subject assignments.",
            "handoff_ready": "Patient analysis requires spatial results.",
        }[route.state]

    def _replace_models(self, tables: dict[str, FormTable]) -> None:
        """Replace bounded display models, retaining full values in supporting records."""
        prior = tuple(self._models.values())
        self._models = {}
        for table_id, form_table in tables.items():
            table = self._tables[table_id]
            model = _ResultTableModel(
                form_table,
                table,
                path_label=(
                    "Cell-data reference" if table_id == "spatial-handoffs" else "Analysis result"
                ),
                compact_locations=table_id == "outputs",
            )
            table.setModel(model)
            fit_scientific_columns(table)
            _fit_bounded_table_height(table)
            self._models[table_id] = model
        for model in prior:
            model.deleteLater()

    def _copy_selected_cells(self) -> None:
        """Copy exact table values or a complete location, regardless of visual shortening."""
        focused = QApplication.focusWidget()
        if focused in self._location_labels.values():
            text = focused.toolTip()
            if text:
                QApplication.clipboard().setText(text)
            return
        table = next((item for item in (*self._tables.values(), self._classifier_table) if item.hasFocus()), None)
        if table is None:
            return
        indexes = sorted(
            (
                index for index in table.selectionModel().selectedIndexes()
                if not table.isColumnHidden(index.column())
            ),
            key=lambda item: (item.row(), item.column()),
        )
        if not indexes:
            return
        lines: list[str] = []
        current_row = indexes[0].row()
        values: list[str] = []
        for index in indexes:
            if index.row() != current_row:
                lines.append("\t".join(values))
                values = []
                current_row = index.row()
            value = index.data(Qt.ItemDataRole.UserRole)
            values.append("" if value is None else str(value))
        lines.append("\t".join(values))
        QApplication.clipboard().setText("\n".join(lines))

    def _emit_open_qupath(self) -> None:
        self._classifier_error_request = None
        if self._open_qupath.isEnabled():
            self.open_qupath_requested.emit()

    def _emit_open_plot(self) -> None:
        if self._plot_path is not None and self._open_plot.isEnabled():
            self.open_plot_requested.emit(self._plot_path)

    def _emit_reveal_path(self) -> None:
        if self._reveal_path is not None and self._reveal.isEnabled():
            self.reveal_path_requested.emit(self._reveal_path)

    def _emit_spatial(self) -> None:
        if (
            self._spatial.isEnabled()
            and self._bound_routes is not None
            and self._bound_routes.spatial_route.run_available
        ):
            self.spatial_requested.emit(self._bound_routes.spatial_handoffs)

    def _emit_patient(self) -> None:
        if (
            self._patient.isEnabled()
            and self._bound_routes is not None
            and self._bound_routes.patient_route.run_available
        ):
            self.patient_requested.emit(
                self._bound_routes.spatial_results,
                self._bound_routes.subject_id_by_sample,
            )


__all__ = ["ResultsPage"]
