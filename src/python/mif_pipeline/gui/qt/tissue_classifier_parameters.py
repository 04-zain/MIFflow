"""Editable classifier settings and the combinations used in grouped validation."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QGridLayout, QHeaderView, QLabel, QTableWidget,
    QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)
from .researcher_widgets import configure_disclosure
from .workbench import _fit_bounded_table_height, style_scientific_table


class TissueClassifierParameters(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName('tissue-classifier-parameters')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(10)
        self.title = QLabel(self)
        self.title.setProperty('panelHeading', True)
        layout.addWidget(self.title)
        self.grid = QGridLayout()
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(22)
        self.grid.setVerticalSpacing(6)
        self.grid.setColumnStretch(0, 1)
        layout.addLayout(self.grid)
        self.rows = []
        self.cross_validation = False
        self._arrangement = None
        self._candidates = ()
        self.toggle = QToolButton(self)
        self.toggle.setText("Compare parameter combinations")
        self.toggle.setCheckable(True)
        configure_disclosure(self.toggle)
        self.table = QTableWidget(self)
        style_scientific_table(self.table)
        self.table.setAccessibleName("Parameter combinations compared in each fold")
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide()
        self.toggle.toggled.connect(self.table.setVisible)
        self.table.hide()
        self.toggle.hide()

    def add_control(self, name, caption, field):
        caption.setParent(self)
        field.setParent(self)
        field.layout().setContentsMargins(0, 0, 0, 0)
        help_label = field.findChild(QLabel, "form-help-" + name)
        if help_label is not None:
            field.layout().removeWidget(help_label)
            help_label.setParent(self)
            if name.endswith(("_svm_kernel", "_grid_svm_kernels")):
                kernel = caption.buddy()
                single = isinstance(kernel, QComboBox)

                def describe_kernel():
                    values = {kernel.currentData()} if single else {
                        key.strip().upper() for key in kernel.text().split(",")
                    }
                    if values == {"LINEAR"}:
                        text = "Fits a boundary from a weighted sum of the input features."
                    elif values == {"RBF"}:
                        text = "Uses distances between observations to fit a curved boundary."
                    else:
                        text = "Linear fits a straight boundary in feature space. RBF can fit a curved boundary."
                    help_label.setText(text)

                changed = kernel.currentIndexChanged if single else kernel.textChanged
                changed.connect(describe_kernel)
                describe_kernel()
        self.rows.append((caption, field, help_label))

    def add_summary(self, label):
        label.setContentsMargins(0, 6, 0, 0)
        self.layout().addWidget(label)
        self.layout().addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.layout().addWidget(self.table)

    def sync(self, method, cross_validation, candidates=(), *, tissue=True):
        family = {'rtrees': 'Random forest', 'svm': 'SVM', 'logistic_regression': 'Logistic regression'}.get(str(method).removeprefix('kronos_'), 'Classifier')
        graph = tissue and str(method).startswith("kronos_")
        self.title.setText("Graph and classifier parameters" if graph else f"{family} parameters")
        self.cross_validation = cross_validation
        self._arrange()
        candidates = candidates if cross_validation else ()
        if self._candidates == candidates:
            return
        self._candidates = candidates
        if len(candidates) < 2:
            self.toggle.setChecked(False)
            self.toggle.hide()
            return
        rows = [{key: value for settings in row.values() for key, value in settings.items()} for row in candidates]
        names = {'max_depth': 'Depth', 'min_sample_count': 'Minimum samples', 'active_var_count': 'Features per split',
                 'max_trees': 'Trees', 'kernel': 'Kernel', 'c': 'C', 'gamma': 'RBF gamma', 'hidden_width': 'Graph dimension',
                 'regularization': 'Regularization', 'learning_rate': 'Learning rate', 'iterations': 'Iterations'}
        keys = [key for key in rows[0] if any(row[key] != rows[0][key] for row in rows[1:])]
        self.table.setColumnCount(len(keys))
        self.table.setRowCount(len(rows))
        self.table.setHorizontalHeaderLabels([names[key] for key in keys])
        for i, row in enumerate(rows):
            for j, key in enumerate(keys):
                value = row[key]
                text = "Default" if key == "active_var_count" and value == 0 else "—" if value is None else str(value)
                self.table.setItem(i, j, QTableWidgetItem(text))
        _fit_bounded_table_height(self.table)
        self.toggle.show()

    def _arrange(self):
        active = tuple(row for row in self.rows if not row[1].isHidden())
        if self._arrangement == active:
            return
        self._arrangement = active
        while self.grid.count():
            self.grid.takeAt(0)
        for row in range(self.grid.rowCount()):
            self.grid.setRowMinimumHeight(row, 0)
        for _, field, help_label in self.rows:
            if help_label is not None:
                help_label.setVisible(not field.isHidden())
        for index, (caption, field, help_label) in enumerate(active):
            row = index * 4
            self.grid.addWidget(caption, row, 0, Qt.AlignmentFlag.AlignTop)
            self.grid.addWidget(field, row + 1, 0, Qt.AlignmentFlag.AlignTop)
            if help_label is not None:
                self.grid.addWidget(help_label, row + 2, 0, Qt.AlignmentFlag.AlignTop)
            if index + 1 < len(active):
                self.grid.setRowMinimumHeight(row + 3, 5)
