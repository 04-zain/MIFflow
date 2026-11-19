"""Review retained grouped-CV tuning evidence with the shared confusion renderer."""
import json
from html import escape
from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import QBoxLayout, QComboBox, QLabel, QTabWidget, QToolButton, QVBoxLayout, QWidget
from .classifier_review_data import read_artifact_rows, review_table, set_table
from .researcher_widgets import configure_disclosure


class ClassifierCrossValidationPanel(QWidget):
    def __init__(self, parent=None, *, labels=None):
        super().__init__(parent)
        from .training_fit_summary import ClassifierEvaluationPanel
        self.setObjectName('classifier-cross-validation-review')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)
        self.heading = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        selected_model = QWidget(self)
        selected_layout = QVBoxLayout(selected_model)
        selected_layout.setContentsMargins(0, 0, 0, 0)
        selected_layout.setSpacing(5)
        self.model_summary = QLabel(self)
        self.model_summary.setWordWrap(True)
        self.model_summary.setTextFormat(Qt.TextFormat.RichText)
        selected_layout.addWidget(self.model_summary)
        self.summary = QLabel(self)
        self.summary.setWordWrap(True)
        selected_layout.addWidget(self.summary)
        self.heading.addWidget(selected_model, 1)
        self.details_toggle = QToolButton(self)
        self.details_toggle.setObjectName('classifier-cv-selection-details')
        self.details_toggle.setText('Parameter combinations and folds')
        self.details_toggle.setCheckable(True)
        configure_disclosure(self.details_toggle)
        self.heading.addWidget(self.details_toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addLayout(self.heading)
        self.error = QLabel('The cross-validation details could not be opened. Reopen this review to try again.', self)
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)
        self.selection_details = QWidget(self)
        details_layout = QVBoxLayout(self.selection_details)
        details_layout.setContentsMargins(0, 0, 0, 8)
        self.candidates = review_table('cv-candidate-scores', self.selection_details)
        self._candidate_keys = ()
        details_layout.addWidget(self.candidates)
        self.details = QTabWidget(self.selection_details)
        details_layout.addWidget(self.details)
        layout.addWidget(self.selection_details)
        self.selection_details.hide()
        self.details_toggle.toggled.connect(self.selection_details.setVisible)
        parameters = QWidget(self)
        content = QVBoxLayout(parameters)
        self.choice = QComboBox(self)
        self.choice.setAccessibleName('Parameter combination to inspect')
        self.choice.currentIndexChanged.connect(self._show_parameters)
        content.addWidget(self.choice)
        self.parameters = review_table('cv-candidate-parameters', self)
        content.addWidget(self.parameters)
        self.details.addTab(parameters, 'Parameter combinations')
        self.folds = review_table('cv-subject-folds', self)
        self.details.addTab(self.folds, 'Subject folds')
        self.matrix = ClassifierEvaluationPanel(self, labels=labels, tuning=True)
        self.matrix.body.layout().setContentsMargins(0, 4, 0, 0)
        self.matrix.choice.currentIndexChanged.connect(self._show_selection_summary)
        layout.addWidget(self.matrix)
        self.hide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.heading.setDirection(QBoxLayout.Direction.TopToBottom if event.size().width() < 900
                                  else QBoxLayout.Direction.LeftToRight)

    def _show_selection_summary(self):
        """Show the selected estimator settings and recorded subject-weighted score."""
        group = self.matrix.choice.currentData()
        if group is None or not hasattr(self, '_scores'):
            return
        scores = [row for row in self._scores if row['classifier'] == group[0]]
        combinations = {row['candidate'] for row in scores}
        selected = [row for row in scores if row['selected'] == 'True']
        if not selected:
            self.model_summary.clear()
            self.summary.clear()
            return
        count = len(combinations)
        key = (group[0], selected[0]['candidate'])
        parameters = {row['setting']: json.loads(row['value_json']) for row in self._parameters
                      if (row['classifier'], row['candidate']) == key}
        unit = next((rows[0]['observation_unit'] for scope, rows in self.matrix._rows_by_scope.items()
                     if scope[0] == group[0]), 'pixels or patches')
        name, values = _selected_model_summary(parameters, unit)
        combination = f" · Combination {key[1]} of {count}" if count > 1 else ''
        self.model_summary.setText(f'<b>{escape(name + combination)}</b>'
            + (' · ' + escape(' · '.join(values)) if values else ''))
        self.summary.setText(f"Subject-weighted macro F1 {float(selected[0]['selection_score']):.3f}")
        self.details_toggle.setText(f"{count} parameter combination{'s' if count != 1 else ''} · {len(selected)} folds")

    def set_artifacts(self, artifacts):
        self.error.hide()
        self.candidates.show()
        self.details.show()
        try:
            return self._load_artifacts(artifacts)
        except (OSError, ValueError, KeyError, TypeError):
            self.candidates.hide()
            self.details.hide()
            self.matrix.hide()
            self.error.show()
            self.show()
            return False

    def _load_artifacts(self, artifacts):
        self._parameters = read_artifact_rows(artifacts, 'classifier-cv-parameters')
        folds = read_artifact_rows(artifacts, 'classifier-cv-folds')
        scores = read_artifact_rows(artifacts, 'classifier-cross-validation')
        self._scores = scores
        if not self._parameters or not self.matrix.set_artifacts(artifacts):
            self.hide()
            return False
        classifier_ids = tuple(dict.fromkeys(row['classifier'] for row in self._parameters))
        labels = {identity: ('Classifier ' + str(i + 1)) for i, identity in enumerate(classifier_ids)}
        multiple = len(classifier_ids) > 1
        grouped = {}
        for row in scores:
            grouped.setdefault((row['classifier'], row['candidate']), []).append(row)
        self._candidate_keys = tuple(grouped)
        set_table(self.candidates,
            (*(('Classifier',) if multiple else ()), 'Combination', 'Selected', 'Subject-weighted\nmacro F1', 'Fold scores'),
            ((*((labels[key[0]],) if multiple else ()), key[1], 'Yes' if rows[0]['selected'] == 'True' else '',
              f"{float(rows[0]['selection_score']):.3f}", ' · '.join(f"{float(row['fold_score']):.3f}" for row in rows))
             for key, rows in grouped.items()))
        self.candidates.selectionModel().currentRowChanged.connect(self._inspect_candidate)
        set_table(self.folds, (*(('Classifier',) if multiple else ()), 'Subject', 'Held-out fold'),
            ((*((labels[row['classifier']],) if multiple else ()), row['subject_id'], row['fold']) for row in folds))
        old = self.choice.currentData()
        choices = tuple(dict.fromkeys((row['classifier'], row['candidate']) for row in self._parameters))
        selected = next((i for i, key in enumerate(choices) if any(
            (row['classifier'], row['candidate']) == key and row['selected'] == 'True' for row in self._parameters)), 0)
        with QSignalBlocker(self.choice):
            self.choice.clear()
            for key in choices:
                chosen = any((row['classifier'], row['candidate']) == key and row['selected'] == 'True' for row in self._parameters)
                self.choice.addItem((labels[key[0]] + ' · ' if multiple else '') + 'Combination ' + key[1] +
                                    (' · selected' if chosen else ''), key)
            self.choice.setCurrentIndex(choices.index(old) if old in choices else selected)
        self._show_parameters()
        self.choice.setVisible(len(choices) > 1)
        self._show_selection_summary()
        self.show()
        return True

    def _inspect_candidate(self, current, previous):
        if not current.isValid() or not 0 <= current.row() < len(self._candidate_keys):
            return
        key = self._candidate_keys[current.row()]
        for index in range(self.choice.count()):
            if self.choice.itemData(index) == key:
                self.choice.setCurrentIndex(index)
                self.details.setCurrentIndex(0)
                break

    def _show_parameters(self):
        from .classifier_recipe import recipe_value
        key = self.choice.currentData()
        if key in self._candidate_keys:
            with QSignalBlocker(self.candidates.selectionModel()):
                self.candidates.selectRow(self._candidate_keys.index(key))
        values = {}
        for row in self._parameters:
            values.setdefault((row['classifier'], row['setting']), set()).add(row['value_json'])
        varied = {field for (classifier, field), options in values.items() if classifier == key[0] and len(options) > 1} if key else set()
        unit = next((rows[0]['observation_unit'] for group, rows in self.matrix._rows_by_scope.items()
                     if key and group[0] == key[0]), 'pixels or patches')
        parameters = {row['setting'].removeprefix('native_configuration.'): json.loads(row['value_json'])
                      for row in self._parameters if (row['classifier'], row['candidate']) == key}
        labels = {
            'rtrees.max_depth': 'Maximum tree depth',
            'rtrees.max_trees': 'Maximum trees',
            'rtrees.min_sample_count': f'Minimum {unit} per node',
            'rtrees.active_var_count': 'Features considered per split',
            'rtrees.calculate_variable_importance': 'Calculate feature importance',
            'rtrees.termination_epsilon': 'Out-of-bag error threshold',
            'svm.kernel': 'Kernel',
            'svm.c': 'C',
            'svm.max_iterations': 'Maximum iterations',
            'svm.termination_epsilon': 'SVM termination tolerance',
            'logistic_regression.regularization': 'Regularization',
            'logistic_regression.learning_rate': 'Learning rate',
            'logistic_regression.iterations': 'Iterations',
            'graph.hidden_width': 'GNN feature dimension',
        }
        if parameters.get('svm.kernel') == 'RBF':
            labels['svm.gamma'] = 'RBF gamma'
        varied = {field.removeprefix('native_configuration.') for field in varied}
        fields = sorted(parameters.keys() & labels.keys(), key=lambda field: (field not in varied, field))
        set_table(self.parameters, ('Setting', 'Value'), (
            (labels[field], recipe_value(parameters[field])) for field in fields))


def _selected_model_summary(parameters, unit):
    """Format the selected combination's applicable estimator settings for review."""
    families = {'rtrees': 'Selected random forest', 'svm': 'Selected SVM',
                'logistic_regression': 'Selected logistic regression'}
    family = next((name for name in families if any(name in key.split('.') for key in parameters)), None)
    if family is None:
        return 'Selected model', ()
    values = {key.split('.')[-1]: value for key, value in parameters.items() if family in key.split('.')}
    if family == 'rtrees':
        fields = (('max_trees', 'Maximum trees'), ('max_depth', 'Maximum depth'),
                  ('min_sample_count', f'Minimum {unit} per node'), ('active_var_count', 'Features per split'))
        if values.get('active_var_count') == 0:
            values['active_var_count'] = 'automatic'
    elif family == 'svm':
        fields = (('kernel', 'Kernel'), ('c', 'C'),
                  *((('gamma', 'Gamma'),) if values.get('kernel') == 'RBF' else ()),
                  ('max_iterations', 'Maximum iterations'), ('termination_epsilon', 'Tolerance'))
    else:
        fields = (('regularization', 'Regularization'), ('learning_rate', 'Learning rate'),
                  ('iterations', 'Iterations'))
    name = families[family]
    displayed = tuple(f'{label} {values[key]}' for key, label in fields
                      if key in values and values[key] is not None)
    if 'graph.hidden_width' in parameters:
        name = name.replace('Selected ', 'Selected KRONOS ', 1)
        displayed = (f"GNN feature dimension {parameters['graph.hidden_width']}", *displayed)
    return name, displayed
