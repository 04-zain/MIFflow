"""Group existing fitted-classifier evidence without introducing new calculations."""

import csv
import re
import json
from dataclasses import replace
from io import StringIO
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QSignalBlocker, QSortFilterProxyModel, QTimer, QEvent, QRect
from PySide6.QtWidgets import (QBoxLayout, QComboBox, QGridLayout, QHeaderView, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QSizePolicy, QStyle, QTableView, QTabWidget, QToolButton, QVBoxLayout, QWidget)

from .models import FormTableModel
from .classifier_review_data import ReviewDataExport, read_artifact_rows, review_table, set_table
from .training_references import build_training_reference_summary, is_training_reference_table
from .classifier_recipe import recipe_sections, recipe_label as _recipe_label, recipe_value as _recipe_value
from .researcher_widgets import configure_disclosure
from .workbench import style_scientific_table, fit_scientific_columns, _fit_bounded_table_height


class _ReviewPages(QTabWidget):
    """Let the current evidence page determine the workspace size."""

    def __init__(self, parent):
        super().__init__(parent)
        self.tabBar().setObjectName('fitted-review-tabs')
        self.tabBar().setStyleSheet(
            'QTabBar#fitted-review-tabs::scroller { width: 48px; }'
            'QTabBar#fitted-review-tabs QToolButton { background: #edf4f3; '
            'border: 1px solid #c5d6d4; padding: 0; width: 24px; border-radius: 2px; }')
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.currentChanged.connect(self.updateGeometry)

    def _tab_frame_height(self):
        return self.tabBar().sizeHint().height() + 2 * self.style().pixelMetric(QStyle.PixelMetric.PM_DefaultFrameWidth)

    # QTabWidget normally reserves the largest page even when another tab is open.
    # Follow the visible page, including its wrapped/stacked content at this width.
    def sizeHint(self):
        size = super().sizeHint()
        if self.currentWidget() is not None:
            size.setHeight(self.heightForWidth(self.width()))
        return size

    def minimumSizeHint(self):
        size = super().minimumSizeHint()
        if self.currentWidget() is not None:
            frame = 2 * self.style().pixelMetric(QStyle.PixelMetric.PM_DefaultFrameWidth)
            size.setWidth(max(self.currentWidget().minimumSizeHint().width() + frame,
                              self.tabBar().minimumSizeHint().width()))
            size.setHeight(self.heightForWidth(self.width()))
        return size

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.updateGeometry()

    def event(self, event):
        result = super().event(event)
        if event.type() == QEvent.Type.LayoutRequest:
            self.updateGeometry()
        return result

    def heightForWidth(self, width):
        page = self.currentWidget()
        if page is None:
            return super().heightForWidth(width)
        frame = 2 * self.style().pixelMetric(QStyle.PixelMetric.PM_DefaultFrameWidth)
        height = page.heightForWidth(max(0, width - frame)) if page.hasHeightForWidth() else page.sizeHint().height()
        return height + self._tab_frame_height()


class _RecipeFacts(QWidget):
    """Compact selectable saved values; wrap in the page, without nested scrolling."""

    def __init__(self, identity, parent=None, *, label_stretch=4, value_stretch=5):
        super().__init__(parent)
        self.setObjectName(identity)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(20)
        self._grid.setVerticalSpacing(0)
        self._grid.setColumnStretch(0, label_stretch)
        self._grid.setColumnStretch(1, value_stretch)

    def set_rows(self, rows, *, classifiers=False):
        while self._grid.count():
            widget = self._grid.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        last_name = None
        index = 0
        for name, label, value in rows:
            if classifiers and name != last_name:
                heading = QLabel(name, self)
                heading.setTextFormat(Qt.TextFormat.PlainText)
                heading.setWordWrap(True)
                heading.setProperty('panelHeading', True)
                self._grid.addWidget(heading, index, 0, 1, 2)
                index += 1
                last_name = name
            for column, text in enumerate((label, str(value))):
                field = QLabel(text, self)
                if column == 1:
                    field.setAccessibleName(f'{label}: {value}')
                field.setTextFormat(Qt.TextFormat.PlainText)
                field.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse |
                                              Qt.TextInteractionFlag.TextSelectableByKeyboard)
                field.setWordWrap(True)
                field.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                field.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
                field.setStyleSheet('padding: 4px 0; border-bottom: 1px solid #dce5e5; color: ' +
                                   ('#526b72;' if column == 0 else '#172d35;'))
                self._grid.addWidget(field, index, column)
            index += 1
        self.updateGeometry()


class TrainingFitSummary(QWidget):
    """One workspace for saved evidence, image inspection and review exports."""

    inspect_predictions_requested = Signal()

    def __init__(self, form, parent=None):
        super().__init__(parent)
        self.setObjectName("training-fit-evidence")
        self.setAccessibleName("Fitted classifier review workspace")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._form = form
        self._supervised = form.context['stage_result']['stage'] == 'supervised_phenotype_training_review'
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        rows = form.context["stage_result"]["resume_context"]["classifiers"]
        self.exports = ReviewDataExport(form, self)
        self.exports.setMinimumHeight(36)
        self.exports.setStyleSheet(
            'QToolButton { padding: 6px 22px 6px 12px; background: white; '
            'border: 1px solid #bdcfd2; border-radius: 5px; }'
            'QToolButton:hover { background: #edf5f4; }'
            'QToolButton::menu-indicator { subcontrol-origin: padding; '
            'subcontrol-position: right center; right: 7px; }')
        self.inspect = QPushButton("Inspect selected image in QuPath", self)
        self.inspect.setObjectName("classifier-inspect-predictions")
        self.inspect.clicked.connect(self._inspect_fitting_image)
        self._inspection_available = False
        self._image_request = None
        self._error_request = None
        self.inspect.setEnabled(False)
        self.inspect.setVisible(not self._supervised)
        secondary = form.context["stage_result"]["stage"] == "secondary_classifier_training_review"
        self.inspection_help = QLabel(
            "Review the images and subjects used to fit the cell classifier."
            if self._supervised else
            "Compare the predicted structures with the staining and reference annotations in QuPath. Check for missed structures and tissue incorrectly included within their outlines."
            if secondary else "Compare the fitted classifier’s predictions with the staining and reference annotations in QuPath.", self)
        self.inspection_help.setWordWrap(True)
        self.export_status = QLabel(self)
        self.export_status.setWordWrap(True)
        self.export_status.hide()
        self.exports.exported.connect(self._exported)
        layout.addWidget(self.export_status)
        self.pages = _ReviewPages(self)
        self._settings_fit_timer = QTimer(self)
        self._settings_fit_timer.setSingleShot(True)
        self._settings_fit_timer.timeout.connect(self._fit_settings_tables)
        self.pages.currentChanged.connect(self._schedule_settings_fit)
        layout.addWidget(self.pages)
        export_actions = QHBoxLayout()
        export_actions.setContentsMargins(0, 4, 0, 0)
        export_actions.addStretch()
        export_actions.addWidget(self.exports)
        layout.addLayout(export_actions)
        self.references = build_training_reference_summary(form, self)
        self.image_review = self.references
        self.references.inspection_actions.addWidget(self.inspect)
        self.inspect.setToolTip(self.inspection_help.text())
        self.inspection_help.hide()
        self._parents_by_classifier = {}
        self.references.image_selected.connect(self._reference_image_selected)
        self._set_development_images(())
        self.pages.addTab(self.references, "Images and references")
        self.consumed_tables = {table.table_id for table in form.tables if is_training_reference_table(table.table_id)}
        self.evaluation_panel = None
        self._evaluation_tables = None
        self.cv_panel = None
        self._cv_fallback = None
        evaluation = [table for table in form.tables if table.table_id.startswith("cv_candidates_") or table.table_id in {"validation_results", "test_results"}]
        if evaluation and self._supervised:
            from .supervised_fit_review import SupervisedCellEvaluation
            panel = QWidget(self)
            content = QVBoxLayout(panel)
            self.evaluation_panel = SupervisedCellEvaluation(panel)
            self.evaluation_panel.inspect_cell_requested.connect(self._inspect_error_region)
            self.evaluation_panel.subject_selected.connect(self.references.select_subject)
            self.references.subject_selected.connect(self.evaluation_panel.select_subject)
            content.addWidget(self.evaluation_panel)
            self._evaluation_tables = self._tables([table for table in evaluation
                                                    if table.table_id in {'validation_results', 'test_results'}])
            content.addWidget(self._evaluation_tables)
            candidates = [table for table in evaluation if table.table_id.startswith('cv_candidates_')]
            if candidates:
                content.addWidget(self._tables(candidates))
            self.pages.addTab(panel, 'Evaluation')
            self.pages.setCurrentWidget(panel)
        elif evaluation:
            panel = QWidget(self)
            content = QVBoxLayout(panel)
            content.setContentsMargins(0, 0, 0, 0)
            metrics = [table for table in evaluation if table.table_id in {"validation_results", "test_results"}]
            cv = [table for table in evaluation if table not in metrics]
            if metrics:
                self.evaluation_panel = ClassifierEvaluationPanel(panel, labels={row['classifier_id']:
                    {item['class_id']: item['name'] for item in (row.get('training_observations') or {}).get('classes', ())}
                    for row in rows})
                self.evaluation_panel.inspect_region_requested.connect(self._inspect_error_region)
                content.addWidget(self.evaluation_panel)
                self._evaluation_tables = self._tables(metrics)
                content.addWidget(self._evaluation_tables)
                self.evaluation_panel.subject_selected.connect(self.references.select_subject)
                self.references.subject_selected.connect(self.evaluation_panel.select_subject)
            if cv:
                from .classifier_cross_validation import ClassifierCrossValidationPanel
                cv_page = QWidget(self) if metrics else panel
                cv_content = QVBoxLayout(cv_page) if metrics else content
                cv_content.setContentsMargins(0, 0, 0, 0)
                self.cv_panel = ClassifierCrossValidationPanel(cv_page, labels={row['classifier_id']:
                    {item['class_id']: item['name'] for item in (row.get('training_observations') or {}).get('classes', ())}
                    for row in rows})
                self.cv_panel.matrix.inspect_region_requested.connect(self._inspect_error_region)
                self.cv_panel.matrix.subject_selected.connect(self.references.select_subject)
                self.references.subject_selected.connect(self.cv_panel.matrix.select_subject)
                cv_content.addWidget(self.cv_panel)
                self._cv_fallback = self._tables(cv)
                cv_content.addWidget(self._cv_fallback)
            self.pages.addTab(panel, "Evaluation" if metrics else "Cross-validation")
            if metrics and cv:
                self.pages.addTab(cv_page, "Cross-validation")
            self.pages.setCurrentWidget(panel)
        settings = [table for table in form.tables if table.table_id.startswith("selected_parameters_")]
        self.settings_panel = QWidget(self)
        settings_layout = QVBoxLayout(self.settings_panel)
        settings_layout.setContentsMargins(16, 14, 16, 14)
        settings_layout.setSpacing(12)
        self.settings_columns = QBoxLayout(QBoxLayout.Direction.TopToBottom)
        self.settings_columns.setSpacing(28)
        settings_layout.addLayout(self.settings_columns)
        preparation = QWidget(self.settings_panel)
        preparation_layout = QVBoxLayout(preparation)
        preparation_layout.setContentsMargins(0, 0, 0, 0)
        preparation_layout.setSpacing(4)
        self.recipe_heading = QLabel('Cell inputs and preparation' if self._supervised else 'Image inputs and preparation', preparation)
        self.recipe_heading.setProperty('panelHeading', True)
        preparation_layout.addWidget(self.recipe_heading)
        self.recipe = _RecipeFacts('classifier-saved-recipe', preparation)
        preparation_layout.addWidget(self.recipe)
        self.recipe_unavailable = QLabel('The saved input settings are unavailable for this review.', preparation)
        self.recipe_unavailable.setWordWrap(True)
        preparation_layout.addWidget(self.recipe_unavailable)
        preparation_layout.addStretch()
        self.settings_columns.addWidget(preparation, 3)
        parameters = QWidget(self.settings_panel)
        parameter_layout = QVBoxLayout(parameters)
        parameter_layout.setContentsMargins(0, 0, 0, 0)
        parameter_layout.setSpacing(4)
        families = {'rtrees': 'Random forest', 'svm': 'Support vector machine',
                    'logistic_regression': 'Logistic regression'}
        for table in settings:
            self.consumed_tables.add(table.table_id)
            index = int(table.table_id.removeprefix('selected_parameters_'))
            classifier = rows[index]
            family = next((label for name, label in families.items()
                           if name in classifier['estimator_settings']), 'Classifier parameters')
            parameter_heading = QLabel(family if len(settings) == 1 else
                f"{table.title.removeprefix('Fitted parameters: ')} · {family}", parameters)
            parameter_heading.setWordWrap(True)
            parameter_heading.setProperty('panelHeading', True)
            parameter_layout.addWidget(parameter_heading)
            selection = classifier.get('model_selection')
            source = (f"Candidate {selection['selected_candidate_index'] + 1} selected by cross-validation"
                      if selection else 'Parameters used for this fit')
            provenance = QLabel(source, parameters)
            provenance.setWordWrap(True)
            provenance.setStyleSheet('color: #526b72; padding-bottom: 3px;')
            parameter_layout.addWidget(provenance)
            unit = ('cells' if self._supervised else 'patches' if 'kronos' in
                    classifier['estimator_settings']['configuration_schema'] else 'pixels')
            facts = _RecipeFacts('fit-evidence-' + table.table_id, parameters,
                                 label_stretch=5, value_stretch=4)
            facts.set_rows(('', f'Minimum {unit} per node' if row.values[0] == 'Minimum samples per node' else row.values[0],
                            'Automatic' if row.row_id == 'active_var_count' and row.values[1] == 0 else row.values[1])
                           for row in table.rows)
            parameter_layout.addWidget(facts)
        self.graph_heading = QLabel('Spatial graph training', parameters)
        self.graph_heading.setProperty('panelHeading', True)
        self.graph_heading.hide()
        self.graph_settings = _RecipeFacts('classifier-graph-settings', parameters)
        self.graph_settings.hide()
        parameter_layout.addWidget(self.graph_heading)
        parameter_layout.addWidget(self.graph_settings)
        parameter_layout.addStretch()
        self.settings_columns.addWidget(parameters, 2)
        self.recipe_toggle = QToolButton(self.settings_panel)
        self.recipe_toggle.setObjectName('classifier-saved-recipe-disclosure')
        self.recipe_toggle.setText('Complete saved settings')
        self.recipe_toggle.setCheckable(True)
        configure_disclosure(self.recipe_toggle)
        self.recipe_toggle.hide()
        settings_layout.addWidget(self.recipe_toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        self.recipe_details = review_table('classifier-saved-recipe-details', self.settings_panel)
        self.recipe_details.setWordWrap(True)
        self.recipe_details.hide()
        self.recipe_toggle.toggled.connect(self.recipe_details.setVisible)
        self.recipe_toggle.toggled.connect(self._schedule_settings_fit)
        self.recipe_details.horizontalHeader().sectionResized.connect(self._schedule_settings_fit)
        settings_layout.addWidget(self.recipe_details)
        settings_layout.addStretch()
        self.pages.addTab(self.settings_panel, "Model and settings")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.settings_columns.setDirection(QBoxLayout.Direction.LeftToRight if event.size().width() >= 940
                                          else QBoxLayout.Direction.TopToBottom)
        if event.size().width() != event.oldSize().width():
            self._schedule_settings_fit()

    def _schedule_settings_fit(self, *_args):
        self._settings_fit_timer.start(0)

    def _fit_settings_tables(self):
        # Wrapped row heights depend on the columns after the tab's layout settles.
        if self.pages.currentWidget() is self.settings_panel:
            for table in self.settings_panel.findChildren(QTableView):
                if table.model() is not None:
                    table.setTextElideMode(Qt.TextElideMode.ElideNone)
                    _fit_bounded_table_height(table)
                    # Account for scientific-table padding: Qt's delegate can
                    # otherwise elide a wrapped label in a single-height row.
                    metrics = table.fontMetrics()
                    for row in range(table.model().rowCount()):
                        height = max((metrics.boundingRect(
                            QRect(0, 0, max(1, table.columnWidth(col) - 24), 100000),
                            Qt.TextFlag.TextWordWrap, str(table.model().index(row, col).data())).height() + 12
                            for col in range(table.model().columnCount())), default=0)
                        table.setRowHeight(row, max(table.rowHeight(row), height))
                    height = (table.horizontalHeader().height() + sum(table.rowHeight(row)
                              for row in range(table.model().rowCount())) + 2 * table.frameWidth() + 8)
                    table.setFixedHeight(min(280, height))

    def _exported(self, name):
        self.export_status.setText("Exported " + name)
        self.export_status.show()

    def set_inspection_available(self, available):
        self._inspection_available = available and not self._supervised
        self.inspect.setEnabled(self._inspection_available and self._image_request is not None)
        if self.evaluation_panel is not None:
            self.evaluation_panel.set_inspection_available(available)
        if self.cv_panel is not None:
            self.cv_panel.matrix.set_inspection_available(available)

    def _inspect_fitting_image(self):
        self._error_request = None
        self.inspect_predictions_requested.emit()

    def _inspect_error_region(self, request):
        self._error_request = request
        self.inspect_predictions_requested.emit()

    def take_error_review_request(self):
        request, self._error_request = self._error_request, None
        return request

    def selected_image_review(self):
        return dict(self._image_request) if self._image_request is not None else None

    def _set_development_images(self, allocations):
        self.references.set_allocation(allocations)

    def _reference_image_selected(self, row):
        self._image_request = ({'project_entry_id': row['project_entry_id'], 'classifier_name': row['classifier_name']}
            if row and row['split'] == 'training' and row.get('classifier_name') and not self._supervised else None)
        self.inspect.setEnabled(self._inspection_available and self._image_request is not None)
        if self._image_request is not None:
            self.inspect.setAccessibleName('Inspect fitted predictions for ' + row['sample_id'] + ' in QuPath')
            self.inspect.setToolTip('Open ' + row['sample_id'] + ' in QuPath. ' + self.inspection_help.text())
        else:
            self.inspect.setAccessibleName('Inspect selected image in QuPath')
            self.inspect.setToolTip('Select a training image to inspect the fitted classifier’s predictions in QuPath.')

    def set_evaluation_artifacts(self, artifacts):
        try:
            self.exports.set_artifacts(artifacts)
            allocations = read_artifact_rows(artifacts, "classifier-allocation")
            self.references.set_folds(read_artifact_rows(artifacts, "classifier-cv-folds"))
            self._set_development_images(allocations)
            recipes = read_artifact_rows(artifacts, "classifier-recipe")
            self._parents_by_classifier = {
                item['classifier']: ', '.join('Analysis region' if parent == 'AnalysisDomain' else parent
                    for parent in json.loads(item['value_json']))
                for item in recipes if item['setting'] == 'eligible_parent_classes'
            }
            self.graph_settings.hide()
            self.graph_heading.hide()
            if recipes:
                names = {row.row_id: row.values[0] for table in self._form.tables
                         if table.table_id == "training_fit_classifiers" for row in table.rows}
                units = {row['classifier_id']: 'cells' if self._supervised else
                         'patches' if 'kronos' in row['estimator_settings']['configuration_schema'] else 'pixels'
                         for row in self._form.context['stage_result']['resume_context']['classifiers']}
                display, graph = recipe_sections(recipes, names, units)
                self.recipe.set_rows(display, classifiers=len(names) > 1)
                self.graph_settings.set_rows(graph, classifiers=len(names) > 1)
                self.graph_settings.setVisible(bool(graph))
                self.graph_heading.setVisible(bool(graph))
                set_table(self.recipe_details, (*(('Classifier',) if len(names) > 1 else ()), 'Saved field', 'Exact value (JSON)'),
                          ((*((names.get(row['classifier'], row['classifier']),) if len(names) > 1 else ()),
                            row['setting'], row['value_json']) for row in recipes))
            self.recipe.setVisible(bool(recipes))
            self.recipe_unavailable.setVisible(not bool(recipes))
            self.recipe_toggle.setVisible(bool(recipes))
            self.recipe_details.setVisible(bool(recipes) and self.recipe_toggle.isChecked())
        except (OSError, ValueError, KeyError) as error:
            self.export_status.setText("Some saved review details could not be opened. Reopen this review to try again.")
            self.export_status.show()
        if self.cv_panel is not None:
            self._cv_fallback.setVisible(not self.cv_panel.set_artifacts(artifacts))
        if self.evaluation_panel is not None:
            available = self.evaluation_panel.set_artifacts(artifacts)
            self._evaluation_tables.setVisible(not available)

    def _tables(self, tables, *, headings=True):
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        for table in tables:
            self.consumed_tables.add(table.table_id)
            if headings:
                title = ("Cross-validation candidates" if table.table_id.startswith("cv_candidates_") else
                         "Independent-test metrics" if table.table_id == "test_results" else
                         "Validation metrics" if table.table_id == "validation_results" else table.title)
                label = QLabel(title, panel)
                label.setProperty("panelHeading", True)
                label.setWordWrap(True)
                layout.addWidget(label)
            view = QTableView(panel)
            view.setObjectName("fit-evidence-" + table.table_id)
            view.setAccessibleName(table.title or table.table_id.replace("_", " "))
            display = table
            if table.table_id.startswith('selected_parameters_'):
                index = int(table.table_id.removeprefix('selected_parameters_'))
                classifier = self._form.context['stage_result']['resume_context']['classifiers'][index]
                unit = ('cells' if self._supervised else
                        'patches' if 'kronos' in classifier['estimator_settings']['configuration_schema'] else 'pixels')
                display = replace(table, rows=tuple(replace(row, values=(
                    f'Minimum {unit} per node' if row.values[0] == 'Minimum samples per node' else row.values[0],
                    'Automatic' if row.row_id == 'active_var_count' and row.values[1] == 0 else row.values[1],
                    *row.values[2:])) for row in table.rows))
            view.setModel(FormTableModel(display, view))
            view.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
            style_scientific_table(view)
            fit_scientific_columns(view)
            if len(table.columns) == 2:
                view.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
                view.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
                view.setWordWrap(True)
                view.horizontalHeader().sectionResized.connect(self._schedule_settings_fit)
            _fit_bounded_table_height(view)
            layout.addWidget(view)
        layout.addStretch()
        return panel


def build_training_fit_summary(form, parent=None):
    if (form.form_id != "training_fit_review"
            or form.context["stage_result"]["stage"] not in {
                "primary_classifier_training_review", "secondary_classifier_training_review",
                "supervised_phenotype_training_review"}):
        return None
    return TrainingFitSummary(form, parent)




class _ReferenceLocationSort(QSortFilterProxyModel):
    """Sort displayed locations while retaining their exact saved source-row identity."""

    def __init__(self, parent, rows, agreement_column):
        super().__init__(parent)
        self._rows = rows
        self._agreement_column = agreement_column
        self.setSortRole(Qt.ItemDataRole.UserRole)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if index.column() == self._agreement_column and role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            value = super().data(index, Qt.ItemDataRole.UserRole)
            if value is None:
                return 'Not recorded'
            if role == Qt.ItemDataRole.DisplayRole:
                return f'{value:.1%}'
            row = self._rows[self.mapToSource(index).row()]
            unit = row.get('sample_unit') or 'samples'
            total, ignored = row.get('declared_sample_count'), row.get('ignored_sample_count')
            if row.get('sample_membership') == 'sample_center':
                return (f'{value:.1%} of {int(total):,} out-of-fold {unit} with centres in this reference matched its class. '
                        'Overlapping references can contain the same samples.')
            return (f'{value:.1%} of {int(total):,} evaluated {unit} matched the reference class. '
                    f'{int(ignored):,} ignored samples are outside this denominator.')
        return super().data(index, role)


class ClassifierEvaluationPanel(QWidget):
    """Link selectable saved confusion counts, subjects and reference locations."""

    subject_selected = Signal(str, str)
    inspect_region_requested = Signal(dict)

    def __init__(self, parent=None, *, labels=None, tuning=False):
        super().__init__(parent)
        self.setObjectName("classifier-evaluation-panel")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._class_labels = labels or {}
        self._tuning = tuning
        self._artifact_name = "classifier-cv-confusion-data" if tuning else "classifier-confusion-data"
        self._figure = None
        self._rows_by_scope = {}
        self._columns = ()
        self._selected_scope = None
        self._selected_pair = None
        self._comparison_keys = ()
        self._locations = ()
        self._classifier_names = {}
        self._visible_locations = ()
        self._inspection_available = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.error = QLabel("The saved evaluation could not be opened. Reopen this review to try again.", self)
        self.error.setWordWrap(True)
        self.error.hide()
        outer.addWidget(self.error)
        self.body = QWidget(self)
        outer.addWidget(self.body)
        layout = QVBoxLayout(self.body)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        toolbar = QHBoxLayout()
        self.choice = QComboBox(self)
        self.choice.setObjectName("classifier-evaluation-scope")
        self.choice.setAccessibleName("Classifier and evaluation split")
        self.choice.currentIndexChanged.connect(self._group_changed)
        toolbar.addWidget(self.choice, 1)
        self.export_figure = QPushButton("Export figure…", self)
        self.export_figure.clicked.connect(self._export_figure)
        toolbar.addWidget(self.export_figure)
        self.export_data = QPushButton("Export data…", self)
        self.export_data.clicked.connect(self._export_data)
        toolbar.addWidget(self.export_data)
        layout.addLayout(toolbar)
        self.matrix_title = QLabel(self)
        self.matrix_title.setProperty('panelHeading', True)
        self.matrix_title.setWordWrap(True)
        layout.addWidget(self.matrix_title)
        self.overview = QWidget(self)
        self.overview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.overview_layout = QBoxLayout(QBoxLayout.Direction.TopToBottom, self.overview)
        self.overview_layout.setContentsMargins(0, 0, 0, 0)
        self.overview_layout.setSpacing(18)
        matrix_panel = QWidget(self)
        matrix_layout = QVBoxLayout(matrix_panel)
        matrix_layout.setContentsMargins(0, 0, 0, 0)
        matrix_layout.setSpacing(8)
        from .classifier_confusion_matrix import ClassifierConfusionMatrix
        self.matrix = ClassifierConfusionMatrix(self)
        self.matrix.pair_selected.connect(self._pair_selected)
        matrix_layout.addWidget(self.matrix)
        matrix_layout.addStretch()
        self.overview_layout.addWidget(matrix_panel, 6)
        subjects_panel = QWidget(self)
        subjects_layout = QVBoxLayout(subjects_panel)
        subjects_layout.setContentsMargins(0, 0, 0, 0)
        subjects_layout.setSpacing(self.matrix.layout().spacing())
        heading = QLabel('Subjects', self)
        heading.setStyleSheet('color: #172d35; font-weight: 600;')
        subjects_layout.addWidget(heading)
        self.subjects = review_table("classifier-subject-comparison", self)
        subjects_layout.addWidget(self.subjects)
        self.metrics = QLabel(self)
        self.metrics.setWordWrap(True)
        self.metrics.setTextFormat(Qt.TextFormat.PlainText)
        subjects_layout.addWidget(self.metrics)
        subjects_layout.addStretch()
        self.overview_layout.addWidget(subjects_panel, 5)
        layout.addWidget(self.overview)
        layout.addSpacing(6)
        self.selection = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.selection_summary = QLabel(self)
        self.selection_summary.setProperty('panelHeading', True)
        self.selection_summary.setWordWrap(True)
        self.selection.addWidget(self.selection_summary, 1)
        selection_actions = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.selection_actions = selection_actions
        self.clear_selection = QPushButton('Clear matrix selection', self)
        self.clear_selection.setToolTip('Show all reference classes and predictions for the selected subject and evaluation split.')
        self.clear_selection.setObjectName('classifier-clear-confusion-selection')
        self.clear_selection.clicked.connect(self._clear_pair)
        self.clear_selection.hide()
        selection_actions.addWidget(self.clear_selection)
        self.inspect_region = QPushButton("Inspect reference in QuPath", self)
        self.inspect_region.clicked.connect(self._inspect_region)
        selection_actions.addWidget(self.inspect_region)
        selection_actions.addStretch()
        self.selection.addLayout(selection_actions)
        layout.addLayout(self.selection)
        self.location_panel = QWidget(self)
        self.location_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        locations = QVBoxLayout(self.location_panel)
        locations.setContentsMargins(0, 0, 0, 0)
        locations.setSpacing(8)
        self.locations = review_table("classifier-evaluated-reference-locations", self)
        self.locations.setWordWrap(True)
        self._location_fit_timer = QTimer(self)
        self._location_fit_timer.setSingleShot(True)
        self._location_fit_timer.timeout.connect(self._fit_location_rows)
        self.locations.horizontalHeader().sectionResized.connect(lambda *_: self._location_fit_timer.start(0))
        locations.addWidget(self.locations)
        self.empty_locations = QLabel(self)
        self.empty_locations.setWordWrap(True)
        locations.addWidget(self.empty_locations)
        self.location_note = QLabel('Bounds give x, y and width × height in original-image pixels. Inspect opens the saved reference region.', self)
        self.location_note.setWordWrap(True)
        self.location_note.setProperty('choiceHelp', True)
        locations.addWidget(self.location_note)
        layout.addWidget(self.location_panel)
        self.location_panel.hide()
        self.note = QLabel(self)
        self.note.setWordWrap(True)
        self.note.setProperty('choiceHelp', True)
        self.note.hide()
        layout.addWidget(self.note)
        self.export_status = QLabel(self)
        self.export_status.setWordWrap(True)
        self.export_status.setTextFormat(Qt.TextFormat.PlainText)
        self.export_status.hide()
        layout.addWidget(self.export_status)
        self.hide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        direction = QBoxLayout.Direction.LeftToRight if event.size().width() >= 940 else QBoxLayout.Direction.TopToBottom
        if self.overview_layout.direction() != direction:
            self.overview_layout.setDirection(direction)
        self.selection.setDirection(QBoxLayout.Direction.TopToBottom if event.size().width() < 940
                                    else QBoxLayout.Direction.LeftToRight)
        self.selection_actions.setDirection(QBoxLayout.Direction.TopToBottom if event.size().width() < 600
                                            else QBoxLayout.Direction.LeftToRight)
        self._location_fit_timer.start(0)

    def _fit_location_rows(self):
        if self.locations.model() is not None:
            _fit_bounded_table_height(self.locations)

    def _pair_selected(self, pair):
        self._selected_pair = pair
        self._refresh_comparison()

    def _clear_pair(self):
        self._selected_pair = None
        self._refresh_comparison()

    def set_artifacts(self, artifacts):
        prior_group, prior_subject = self.choice.currentData(), self._selected_scope
        self.error.hide()
        self.body.show()
        self._rows_by_scope = {}
        self._locations = ()
        artifact = next((item for item in artifacts if item.artifact_id == self._artifact_name + "/csv"
                         and item.status == "available"), None)
        if artifact is None:
            self.hide()
            return False
        try:
            self._locations = read_artifact_rows(artifacts,
                "classifier-cv-reference-locations" if self._tuning else "classifier-evaluation-locations")
            if self._tuning:
                self._locations = tuple({**row, '_votes': json.loads(row['declared_label_counts_json']),
                    'reference_name': self._class_name(row['classifier'], row['reference_class'])}
                    for row in self._locations)
            self._classifier_names = {} if self._tuning else {(row['classifier'], row['project_entry_id']): row['classifier_name']
                for row in read_artifact_rows(artifacts, 'classifier-allocation') if row.get('classifier_name')}
            with Path(artifact.path).open(newline="", encoding="utf-8") as stream:
                reader = csv.DictReader(stream)
                self._columns = tuple(reader.fieldnames or ())
                for row in reader:
                    key = (row["classifier"], row["split"], row["subject_id"])
                    self._rows_by_scope.setdefault(key, []).append(row)
            groups = tuple(dict.fromkeys(key[:2] for key in self._rows_by_scope))
            with QSignalBlocker(self.choice):
                self.choice.clear()
                for key in groups:
                    row = self._rows_by_scope[(*key, '')][0]
                    label = re.sub(r' · candidate \d+$', '', row['classifier_label']) if self._tuning else row['classifier_label']
                    self.choice.addItem(f"{label} · {'Out-of-fold' if self._tuning else 'Independent test' if key[1] == 'test' else 'Validation'}", key)
                self.choice.setCurrentIndex(next((i for i, group in enumerate(groups) if group == prior_group), 0))
            self.setVisible(bool(groups))
            self._group_changed()
            if prior_subject in self._rows_by_scope:
                self.select_subject(prior_subject[0], prior_subject[2])
            return bool(groups)
        except (OSError, KeyError, TypeError, ValueError):
            self.body.hide()
            self.error.show()
            self.show()
            return False

    def _class_name(self, classifier, key):
        return self._class_labels.get(classifier, {}).get(key, key)

    def _group_changed(self):
        group = self.choice.currentData()
        if group is None:
            return
        pooled = self._rows_by_scope[(*group, '')]
        recorded_unit = pooled[0]["observation_unit"]
        self._unit = "regions" if recorded_unit == "annotation_majority" else recorded_unit
        self._selected_pair = None
        self._selected_scope = (*group, '')
        self.choice.setToolTip('Out-of-fold predictions: each subject was evaluated by the model trained without its fold.'
                               if self._tuning else '')
        self._refresh_comparison()

    def _refresh_comparison(self):
        group = self.choice.currentData()
        if group is None:
            return
        pair = self._selected_pair
        keys, values = [], []
        for key, rows in self._rows_by_scope.items():
            if key[:2] != group:
                continue
            count = sum(int(row['count']) for row in rows)
            errors = sum(int(row['count']) for row in rows if row['reference_class'] != row['predicted_class'])
            selected = sum(int(row['count']) for row in rows if pair == (row['reference_class'], row['predicted_class'])) if pair else None
            first = rows[0]
            keys.append(key)
            values.append((key[2] or 'All subjects', count, errors,
                f"{float(first['macro_f1']):.3f}", *((selected,) if pair else ())))
        self._comparison_keys = tuple(keys)
        set_table(self.subjects, ('Subject', self._unit.capitalize(), 'Errors', 'Macro F1', *(('Pair count',) if pair else ())), values)
        self.subjects.horizontalHeader().setMinimumSectionSize(45)
        self.subjects.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, self.subjects.model().columnCount()):
            self.subjects.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.subjects.selectionModel().currentRowChanged.connect(self._comparison_selected)
        self.clear_selection.setVisible(pair is not None)
        selected = keys.index(self._selected_scope) if self._selected_scope in keys else 0
        if keys:
            self.subjects.selectRow(selected)

    def _comparison_selected(self, current, _previous):
        if not current.isValid():
            return
        self._selected_scope = self._comparison_keys[current.row()]
        self._select_scope()
        self.subject_selected.emit(self._selected_scope[0], self._selected_scope[2])

    def select_subject(self, classifier, subject):
        group = self.choice.currentData()
        if group is None or group[0] != classifier:
            index = next((i for i in range(self.choice.count()) if self.choice.itemData(i)[0] == classifier), -1)
            if index < 0:
                return
            self.choice.setCurrentIndex(index)
            group = self.choice.currentData()
        scope = (*group, subject)
        if scope not in self._rows_by_scope:
            return
        if scope not in self._comparison_keys:
            self._clear_pair()
        if scope in self._comparison_keys:
            self.subjects.selectRow(self._comparison_keys.index(scope))

    def _select_scope(self, *_args):
        self.export_status.clear()
        self.export_status.hide()
        rows = self._rows_by_scope.get(self._selected_scope)
        if not rows:
            return
        labels = tuple(dict.fromkeys(row["reference_class"] for row in rows))
        first = rows[0]
        self.matrix.set_counts(labels, tuple(self._class_name(first['classifier'], label) for label in labels),
                               rows, self._unit, self._selected_pair)
        if self._figure is not None:
            self._figure.clear()
            self._figure = None
        count = sum(int(row['count']) for row in rows)
        unit = 'reference regions' if self._unit == 'regions' else self._unit
        subject = first['subject_id'] or 'All subjects'
        self.matrix_title.setText(f'{subject} · {count:,} {unit}')
        self.metrics.setText(' · '.join(f"{label} {float(first[name]):.3f}" for name, label in (
            ('accuracy', 'Accuracy'), ('macro_precision', 'Macro precision'), ('macro_recall', 'Macro recall'))))
        if self._selected_pair is None:
            self.selection_summary.setText('Reference regions' if self._unit == 'regions' else
                'Out-of-fold references' if self._tuning and self._locations else
                'Select a matrix entry to compare that class pair across subjects.')
        else:
            reference, predicted = self._selected_pair
            selected = next(row for row in rows if (row['reference_class'], row['predicted_class']) == self._selected_pair)
            self.selection_summary.setText(f"Reference: {self._class_name(first['classifier'], reference)}\n"
                f"Predicted: {self._class_name(first['classifier'], predicted)} · {int(selected['count']):,} {unit}")
        if self._tuning:
            self.note.setText('Reference locations were not saved with this evaluation.')
            self.note.setVisible(not self._locations and self._unit != 'regions')
        self._refresh_locations()

    def _exportable_figure(self):
        """Build the existing publication figure only when it is requested for export."""
        if self._figure is None:
            from mif_pipeline.compartment_analysis.evaluation_plots import build_classifier_confusion_figure
            rows = self._rows_by_scope[self._selected_scope]
            labels = tuple(dict.fromkeys(row['reference_class'] for row in rows))
            counts = {(row['reference_class'],row['predicted_class']):int(row['count']) for row in rows}
            first = rows[0]
            self._figure = build_classifier_confusion_figure(
                classifier_label=first['classifier_label'], split=first['split'], patient_count=int(first['subject_count']),
                class_ids=tuple(self._class_name(first['classifier'],label) for label in labels),
                confusion_matrix=tuple(tuple(counts[reference,predicted] for predicted in labels) for reference in labels),
                observation_unit=self._unit)
        return self._figure

    def set_inspection_available(self, available):
        self._inspection_available = bool(available)
        self.inspect_region.setEnabled(self._inspection_available and bool(self._visible_locations))

    def _refresh_locations(self):
        prior = self.selected_location_request()
        old_model = self.locations.model()
        old_column = self.locations.horizontalHeader().sortIndicatorSection()
        old_heading = old_model.headerData(old_column, Qt.Orientation.Horizontal) if old_model is not None else None
        if old_heading is not None:
            old_heading = old_heading.replace('\n', ' ')
        old_order = self.locations.horizontalHeader().sortIndicatorOrder()
        classifier, split, subject = self._selected_scope
        pair = self._selected_pair
        self._visible_locations = tuple(row for row in self._locations
            if row['classifier'] == classifier and row['split'] == split
            and (not subject or row['subject_id'] == subject)
            and (pair is None or (row['reference_class'] == pair[0] and row['_votes'][pair[1]] > 0)
                 if self._tuning else pair is None or pair == (row['reference_class'], row['predicted_class'])))
        self.location_panel.setVisible(self._unit == 'regions' or bool(self._locations))
        agreement_available = any('reference_agreement' in row for row in self._locations)
        if self._tuning:
            columns = ('Image', *(('Subject',) if not subject else ()), 'Fold',
                       *(('Reference',) if pair is None else ()),
                       *((f'{self._unit.capitalize()} in selected pair',) if pair is not None else ()),
                       'Reference bounds (px)', 'Reference agreement')
            values = ((row['sample_id'], *((row['subject_id'],) if not subject else ()), int(row['fold']),
                       *((row['reference_name'],) if pair is None else ()),
                       *((row['_votes'][pair[1]],) if pair is not None else ()),
                       f"({float(row['x']):g}, {float(row['y']):g}) · {float(row['width']):g} × {float(row['height']):g}",
                       float(row['reference_agreement']) if row.get('reference_agreement') else None)
                      for row in self._visible_locations)
        else:
            columns = ('Image', *(('Subject',) if not subject else ()),
                       *(('Reference', 'Recorded prediction') if pair is None else ()), 'Reference bounds (px)',
                       *(('Reference agreement',) if agreement_available else ()))
            values = ((row['sample_id'], *((row['subject_id'],) if not subject else ()),
                       *((row['reference_name'], row['predicted_name']) if pair is None else ()),
                       f"({float(row['x']):g}, {float(row['y']):g}) · {float(row['width']):g} × {float(row['height']):g}",
                       *((float(row['reference_agreement']) if row.get('reference_agreement') else None,) if agreement_available else ()))
                      for row in self._visible_locations)
        self.locations.setSortingEnabled(False)
        headings = tuple(heading.replace(' in selected pair', ' in\nselected pair')
                         .replace('Reference bounds (px)', 'Reference bounds\n(px)')
                         .replace('Reference agreement', 'Reference\nagreement')
                         .replace('Recorded prediction', 'Recorded\nprediction') for heading in columns)
        set_table(self.locations, headings, values,
                  identities=(row['annotation_uuid'] for row in self._visible_locations))
        source = self.locations.model()
        proxy = _ReferenceLocationSort(self.locations, self._visible_locations,
            columns.index('Reference agreement') if agreement_available else -1)
        proxy.setSourceModel(source)
        source.setParent(proxy)
        self.locations.setModel(proxy)
        header = self.locations.horizontalHeader()
        header.setMinimumSectionSize(50)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for index, heading in enumerate(columns):
            if heading == 'Reference bounds (px)':
                header.setSectionResizeMode(index, QHeaderView.ResizeMode.ResizeToContents)
            elif heading in ('Fold', 'Reference agreement') or heading.endswith(' in selected pair'):
                header.setSectionResizeMode(index, QHeaderView.ResizeMode.Fixed)
                width = max(self.locations.fontMetrics().horizontalAdvance(line) for line in headings[index].splitlines()) + 36
                header.resizeSection(index, width)
        self._location_fit_timer.start(0)
        self.locations.setSortingEnabled(True)
        column = columns.index(old_heading) if old_heading in columns else len(columns)-1 if agreement_available else 0
        self.locations.sortByColumn(column, old_order if old_heading in columns else Qt.SortOrder.AscendingOrder)
        self.locations.selectionModel().currentRowChanged.connect(self._location_selected)
        self.locations.setVisible(bool(self._visible_locations))
        self.empty_locations.setVisible(not self._visible_locations)
        self.empty_locations.setText('No reference regions match the selected class pair for this subject.' if self._locations
            else 'Reference-region positions were not saved with this evaluation.')
        self.inspect_region.setVisible(bool(self._locations))
        self.location_note.setVisible(bool(self._locations))
        units = {row.get('sample_unit') for row in self._visible_locations if row.get('sample_unit')}
        agreement_unit = next(iter(units)) if len(units) == 1 else 'pixels or patches'
        self.location_note.setText((f'Reference agreement is the percentage of evaluated {agreement_unit} assigned to the region’s reference class.\n'
            if agreement_available else '') + 'Bounds give the region’s position (x, y) and size (width × height) in original image pixels.')
        self.location_note.setToolTip('Reference agreement excludes ignored samples.' if agreement_available else '')
        if self._visible_locations:
            previous = next((i for i, row in enumerate(self._visible_locations)
                             if prior and all(row[key] == prior[key] for key in
                                 ('project_entry_id', 'subject_id', 'annotation_uuid'))), None)
            target = proxy.mapFromSource(source.index(previous, 0)).row() if previous is not None else 0
            self.locations.selectRow(target)
        self.inspect_region.setEnabled(self._inspection_available and bool(self._visible_locations))

    def _location_selected(self, current, _previous):
        self.inspect_region.setEnabled(self._inspection_available and current.isValid())

    def selected_location_request(self):
        current = self.locations.currentIndex()
        if not current.isValid():
            return None
        model = self.locations.model()
        index = model.mapToSource(current) if isinstance(model, QSortFilterProxyModel) else current
        if index.row() >= len(self._visible_locations):
            return None
        row = self._visible_locations[index.row()]
        from .classifier_review_data import recorded_region_request
        if self._tuning:
            total = int(row['declared_sample_count'])
            pair = self._selected_pair
            if pair is None:
                description = f"{row['_votes'][row['reference_class']]:,} of {total:,} {row['sample_unit']} match reference"
            else:
                description = f"{row['_votes'][pair[1]]:,} of {total:,} {row['sample_unit']} as {self._class_name(row['classifier'], pair[1])}"
            request = recorded_region_request({**row,
                'predicted_name': f"Out-of-fold · fold {row['fold']} · {description}"})
            if row.get('classifier_name'):
                request.update(classifier_id=row['classifier'], classifier_name=row['classifier_name'],
                               candidate=int(row['candidate']) - 1, fold=int(row['fold']) - 1)
            return request
        request = recorded_region_request(row)
        classifier_name = self._classifier_names.get((row['classifier'], row['project_entry_id']))
        if not self._tuning and classifier_name:
            request.update(classifier_id=row['classifier'], classifier_name=classifier_name)
        return request

    def _inspect_region(self):
        request = self.selected_location_request()
        if request is not None and self._inspection_available:
            self.inspect_region_requested.emit(request)


    def selected_data(self):
        """Preserve selected saved values, labels and absent-class fractions verbatim."""
        output = StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=self._columns)
        writer.writeheader()
        writer.writerows(self._rows_by_scope[self._selected_scope])
        return output.getvalue().encode("utf-8")

    def _export_name(self, suffix):
        """Name the selected classifier, split and subject without path separators."""
        scope = self._selected_scope
        classifier = self._rows_by_scope[scope][0]["classifier_label"]
        name = classifier + "-" + scope[1] + "-" + (scope[2] or "all-subjects") + "-confusion"
        return (re.sub(r"[^\w.-]+", "-", name).strip(".-") or "classifier-confusion") + suffix

    def _export_figure(self):
        from .figure_export import export_figure
        destination = export_figure(self, self._exportable_figure(), self._export_name(".svg"))
        if destination is not None:
            self.export_status.setText("Exported " + destination.name)
            self.export_status.show()

    def _export_data(self):
        from .figure_export import _choose_path, _save_bytes
        path = _choose_path(self, self._export_name(".csv"), {"CSV table (*.csv)": "csv"})
        if path is None:
            return
        try:
            _save_bytes(self.selected_data(), path)
            self.export_status.setText("Exported " + path.name)
            self.export_status.show()
        except OSError as error:
            QMessageBox.warning(self, "Data not exported", f"Could not save {path.name}.\n\n{error}")
