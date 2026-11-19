"""Qt rendering for one current immutable batch-plan projection.

The widget consumes the current presentation model and emits researcher actions through its
controller. Expensive native or scientific operations use managed jobs, while the Qt thread
retains the selected form and its response identity."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QDialog,
    QDialogButtonBox,
    QPlainTextEdit,
    QScrollArea,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..interactions import FormTable
from ..presenters.plan import BatchPlanView
from .forms import apply_widget_accessibility
from .models import FormTableModel
from .workbench import (
    PageHeading,
    _fit_bounded_table_height,
    style_scientific_table,
    fit_scientific_columns,
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_VISIBLE_SAMPLE_FIELDS = frozenset(
    {
        "enabled",
        "sample_id",
        "subject_id",
        "validation_domain_id",
        "source_mif_path",
        "moving_reference_path",
        "moving_reference_modality",
    }
)


# ==============================================================================
#                                      Plan
# ==============================================================================


@dataclass(frozen=True, slots=True)
class StartPlanIntent:
    """One explicit request to run the currently rendered plan digest."""

    plan_sha256: str

    def __post_init__(self) -> None:
        if type(self.plan_sha256) is not str or _SHA256.fullmatch(self.plan_sha256) is None:
            raise ValueError("start intent requires a lowercase plan SHA-256")


class _PlanSampleModel(FormTableModel):
    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if (
            role == Qt.ItemDataRole.DisplayRole
            and index.isValid()
            and self._columns[index.column()] == "enabled"
            and type(self._rows[index.row()][index.column()]) is bool
        ):
            return "Yes" if self._rows[index.row()][index.column()] else "No"
        return super().data(index, role)


class PlanPage(QWidget):
    """Display plan evidence without inspecting or executing batch state."""

    start_requested = Signal(object)
    open_home_setup_requested = Signal()
    compute_requested = Signal()

    def __init__(self, *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from .form_sections import FormSection, label
        from .researcher_widgets import ContentSizedButton, configure_disclosure

        self._current_plan = None
        self._start_pending = False
        self._models = {}
        self._tables = {}
        self._software_groups = []
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        scroll = QScrollArea(self)
        scroll.setObjectName("plan-scroll-area")
        scroll.setWidgetResizable(True)
        content = QWidget(scroll)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(22, 20, 22, 22)
        layout.setSpacing(18)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addWidget(
            PageHeading(
                "plan",
                "Plan",
                "Review samples and software before starting.",
                content,
            )
        )
        self._empty = label(
            "Prepare your samples to see the planned steps and setup checks.",
            content,
            name="plan-empty",
        )
        layout.addWidget(self._empty)
        self._overview = QWidget(content)
        self._overview.setObjectName("plan-overview")
        self._sample_summary = label("", self._overview, name="plan-sample-summary")
        self._readiness_summary = label("", self._overview, name="plan-readiness-summary")
        overview_row = QHBoxLayout(self._overview)
        overview_row.setContentsMargins(2, 0, 2, 0)
        overview_row.addWidget(self._sample_summary, 1)
        overview_row.addWidget(self._readiness_summary, 0)
        self._readiness_summary.setProperty("statusBadge", True)
        self._readiness_summary.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._overview.hide()
        self._sections = {}
        samples = FormSection("Samples", parent=content)
        samples.setObjectName("plan-samples-section")
        self._sections["samples"] = samples
        self._path_toggle = QToolButton(samples)
        self._path_toggle.setText("Show image locations")
        self._path_toggle.setObjectName("plan-show-image-paths")
        self._path_toggle.setCheckable(True)
        configure_disclosure(self._path_toggle)
        self._path_toggle.toggled.connect(self._toggle_image_paths)
        sample_heading = samples.body.takeAt(0).widget()
        sample_header = QHBoxLayout()
        sample_header.addWidget(sample_heading, 1)
        sample_header.addWidget(self._path_toggle)
        samples.body.insertLayout(0, sample_header)
        layout.addWidget(samples)
        from .computing_summary import ComputingSummary

        self.computing_summary = ComputingSummary(content)
        layout.insertWidget(2, self._overview)
        setup = FormSection("Software availability", parent=content)
        setup.setObjectName("plan-setup-section")
        self._sections["prerequisites"] = setup
        self._software_body = setup.body
        layout.addWidget(setup)
        layout.addWidget(self.computing_summary)
        table = self._new_table("samples", samples)
        table.setAccessibleName("Samples")
        samples.body.addWidget(table)
        samples.hide()
        setup.hide()
        self._warnings = label("", content, name="plan-warnings")
        self._warnings.hide()
        layout.addWidget(self._warnings)
        layout.addStretch(1)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        from .action_bar import WorkflowActionBar

        action_bar = WorkflowActionBar(self)
        self._start = ContentSizedButton("Start analysis", action_bar)
        self._start.setObjectName("start-plan")
        self._start.setProperty("actionRole", "primary")
        self._start.setEnabled(False)
        self._start.clicked.connect(self._emit_start)
        action_bar.add_primary(self._start)
        self._open_home_setup = ContentSizedButton("Software setup", action_bar)
        self._open_home_setup.setObjectName("plan-open-home-setup")
        self._open_home_setup.setProperty("actionRole", "secondary")
        self._open_home_setup.clicked.connect(self.open_home_setup_requested)
        self._open_home_setup.hide()
        action_bar.add_utility(self._open_home_setup)
        outer.addWidget(action_bar)
        apply_widget_accessibility(self, "plan")

    def set_computing(self, settings, workspace, *, valid=True):
        self.computing_summary.set_location(settings, workspace, valid=valid)

    def _toggle_image_paths(self, _checked=False):
        if self._current_plan is None:
            return
        for i, name in enumerate(self._current_plan.sample_table.columns):
            visible = name in _VISIBLE_SAMPLE_FIELDS or name == "value"
            if name in {
                "moving_reference_modality",
                "moving_reference_path",
            }:
                visible &= any(
                    row.values[i] not in (None, "") for row in self._current_plan.sample_table.rows
                )
            if name in {"source_mif_path", "moving_reference_path"}:
                visible = visible and self._path_toggle.isChecked()
            self._tables["samples"].setColumnHidden(i, not visible)
        table = self._tables["samples"]
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        fit_scientific_columns(table)
        if self._path_toggle.isChecked():
            # Keep label columns wide enough for their headings. Only paths
            # share the remaining space; their exact values are in tooltips.
            for i, name in enumerate(self._current_plan.sample_table.columns):
                if name in {"source_mif_path", "moving_reference_path"}:
                    header.setSectionResizeMode(i, QHeaderView.ResizeMode.Stretch)
        else:
            header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)

    def _update_overview(self, view):
        rows = [dict(zip(view.sample_table.columns, row.values)) for row in view.sample_table.rows]
        active = sum(item.get("enabled", True) is not False for item in rows)
        groups = len({item.get("validation_domain_id") for item in rows if item.get("enabled", True) is not False})
        self._sample_summary.setText(f"{active} sample{'s' if active != 1 else ''} · {groups} processing group{'s' if groups != 1 else ''}")
        self._readiness_summary.setText("Ready to start" if view.prerequisites_ready else "Setup needed")
        self._readiness_summary.setStyleSheet("" if view.prerequisites_ready else "color: #8a4b10; background: #fff1dc;")
        self._overview.show()

    def _new_table(self, table_id: str, parent: QWidget) -> QTableView:
        table = QTableView(parent)
        table.setObjectName(f"plan-{table_id}-table")
        style_scientific_table(table)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._tables[table_id] = table
        return table

    def set_plan(self, view: BatchPlanView, *, started: bool = False) -> None:
        if type(view) is not BatchPlanView:
            raise TypeError("Plan page requires a BatchPlanView")
        self._replace_models({"samples": view.sample_table})
        self._current_plan = view
        self._toggle_image_paths()
        self._update_overview(view)
        self._render_software(view)
        for section in self._sections.values():
            section.show()
        self._tables["samples"].show()
        self._empty.hide()
        self._warnings.setText("\n".join(view.warnings))
        self._warnings.setVisible(bool(view.warnings))
        self._start_pending = started
        self._start.setVisible(not started)
        self._start.setEnabled(view.prerequisites_ready and not started)
        if started:
            self.mark_started()
        self._open_home_setup.show()

    def _render_software(self, view):
        from .form_sections import label
        from .researcher_widgets import ContentSizedButton
        for widget in self._software_groups:
            self._software_body.removeWidget(widget)
            widget.deleteLater()
        self._software_groups = []
        rows = [dict(zip(view.prerequisite_table.columns, row.values)) for row in view.prerequisite_table.rows]
        groups = (
            ("Image reading", {"java_runtime", "bioformats_runtime"}),
            ("Cell segmentation", {"segmentation_method", "instanseg_extension", "instanseg_model", "cellpose_model", "stardist_model"}),
            ("Paired-image alignment", {"warpy_extension"}),
            ("Saved models", {"classifier_artifact"}),
        )
        for title, kinds in groups:
            selected = [row for row in rows if row["kind"] in kinds]
            if not selected:
                continue
            group = QWidget(self._sections["prerequisites"])
            group.setObjectName("plan-software-" + title.lower().replace(" ", "-"))
            box = QVBoxLayout(group)
            box.setContentsMargins(0, 5, 0, 5)
            box.setSpacing(7)
            box.addWidget(label(title, group, role="fieldCaption"))
            # Java and Bio-Formats share the same owner observation. Show their
            # complete evidence once when identical, retaining separate failures.
            if title == "Image reading" and len(selected) == 2 and all(
                selected[0][field] == selected[1][field] for field in ("state", "detail", "external_locator")
            ):
                selected = [{**selected[0], "requirement": "Java and Bio-Formats"}]
            for row in selected:
                item = QWidget(group)
                line = QHBoxLayout(item)
                line.setContentsMargins(0, 0, 0, 0)
                line.setSpacing(12)
                summary = QVBoxLayout()
                summary.setSpacing(3)
                title_text = row["requirement"]
                if row["kind"] == "segmentation_method":
                    title_text = str(row["detail"]).split(". ", 1)[0].removeprefix("Installed: ").rstrip(".")
                if row["scope"] == "sample":
                    title_text += " · " + row["sample_id"]
                elif row["scope"] == "validation_domain":
                    title_text += " · " + row["validation_domain_id"]
                summary.addWidget(label(title_text, item))
                line.addLayout(summary, 1)
                state = "Available" if row["state"] == "ready" else {"missing": "Missing", "incompatible": "Needs attention", "error": "Check failed"}.get(row["state"], row["state"])
                if row["kind"] in {"segmentation_method", "cellpose_model", "stardist_model"} and row["state"] == "ready":
                    state = "Installed"
                badge = label(state, item)
                badge.setProperty("statusBadge", True)
                if row["state"] != "ready":
                    badge.setStyleSheet("color: #8a4b10; background: #fff1dc;")
                badge.setToolTip("Installed model; the selected method is checked during segmentation setup." if row["kind"] == "segmentation_method" else "Required by this analysis.")
                line.addWidget(badge, 0)
                details = ContentSizedButton("Details", item)
                details.setObjectName("plan-check-details-" + row["kind"])
                details.clicked.connect(lambda _checked=False, row=row: self._show_software_details(row))
                line.addWidget(details)
                box.addWidget(item)
            self._software_body.addWidget(group)
            self._software_groups.append(group)
        if not rows:
            empty = label("No software requirements for this plan.", self._sections["prerequisites"], role="choiceHelp")
            self._software_body.addWidget(empty)
            self._software_groups.append(empty)

    def _show_software_details(self, row):
        dialog = QDialog(self)
        dialog.setWindowTitle(row["requirement"])
        dialog.resize(680, 380)
        layout = QVBoxLayout(dialog)
        details = [str(row["detail"])]
        if row["external_locator"]:
            details.append("Location: " + str(row["external_locator"]))
        details.append("Required for: " + ", ".join(row["required_by_node_kinds"]))
        text = QPlainTextEdit("\n\n".join(details), dialog)
        text.setReadOnly(True)
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dialog)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.open()

    def _replace_models(self, tables: dict[str, FormTable]) -> None:
        prior = tuple(self._models.values())
        self._models = {}
        for table_id, form_table in tables.items():
            table = self._tables[table_id]
            model = (_PlanSampleModel if table_id == "samples" else FormTableModel)(
                form_table, table
            )
            table.setModel(model)
            fit_scientific_columns(table)
            _fit_bounded_table_height(table)
            self._models[table_id] = model
        for model in prior:
            model.deleteLater()

    def mark_started(self) -> None:
        if self._current_plan is None:
            return
        self._start_pending = True
        self._start.hide()
        self._start.setEnabled(False)
        self._readiness_summary.setText("Analysis started")

    def clear_plan(self) -> None:
        self._empty.setText("Prepare your samples to see the planned work.")
        self._empty.show()
        self._current_plan = None
        self._overview.hide()
        for section in self._sections.values():
            section.hide()
        self._start_pending = False
        self._start.setEnabled(False)
        self._warnings.clear()
        self._warnings.hide()
        self._open_home_setup.hide()
        prior = tuple(self._models.values())
        self._models = {}
        for table in self._tables.values():
            table.setModel(None)
            table.hide()
        for model in prior:
            model.deleteLater()

    def _emit_start(self) -> None:
        if (
            self._current_plan is None
            or self._start_pending
            or not self._current_plan.prerequisites_ready
        ):
            return
        self._start_pending = True
        self._start.setEnabled(False)
        self.start_requested.emit(StartPlanIntent(self._current_plan.plan_sha256))


__all__ = ["PlanPage", "StartPlanIntent"]
