"""Choose one accepted reference image/role and label its cell classes in QuPath."""
from PySide6.QtCore import Qt, Signal, QSignalBlocker, QTimer
from PySide6.QtWidgets import (QComboBox, QHeaderView, QHBoxLayout, QLabel,
    QSizePolicy, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .researcher_widgets import ContentSizedButton
from .training_evidence import configure_evidence_table, fit_evidence_table


class ReferenceImageNavigator(QWidget):
    """Jump to an image or step through a multi-image annotation task."""
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.previous = ContentSizedButton('←', self)
        self.previous.setAccessibleName('Previous reference image')
        self.previous.setToolTip('Previous reference image')
        self.previous.setFixedWidth(36)
        layout.addWidget(self.previous)
        self.image = QComboBox(self)
        self.image.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.image.setMinimumContentsLength(12)
        self.image.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.image, 1)
        self.next = ContentSizedButton('→', self)
        self.next.setAccessibleName('Next reference image')
        self.next.setToolTip('Next reference image')
        self.next.setFixedWidth(36)
        layout.addWidget(self.next)
        self.position = QLabel(self)
        self.position.setProperty('fieldHelp', True)
        layout.addWidget(self.position)
        self.previous.clicked.connect(lambda: self.image.setCurrentIndex(max(0, self.image.currentIndex() - 1)))
        self.next.clicked.connect(lambda: self.image.setCurrentIndex(min(self.image.count() - 1, self.image.currentIndex() + 1)))
        self.image.currentIndexChanged.connect(self.refresh)

    def refresh(self, *_args):
        index, count = self.image.currentIndex(), self.image.count()
        self.previous.setEnabled(index > 0)
        self.next.setEnabled(0 <= index < count - 1)
        self.previous.setVisible(count > 1)
        self.next.setVisible(count > 1)
        self.position.setVisible(count > 1)
        self.position.setFixedWidth(self.position.fontMetrics().horizontalAdvance(f'{count} / {count}') + 2)
        self.position.setText(f'{index + 1} / {count}')


class ReferenceCellAnnotations(QWidget):
    reference_selected = Signal(object)
    annotation_requested = Signal()

    def __init__(self, context, parent=None):
        super().__init__(parent)
        self.setObjectName('reference-cell-annotations')
        self._rows = tuple(context['references'])
        self._available = False
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(lambda: fit_evidence_table(self.classes))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(14)
        title = QLabel('Reference annotations', self)
        title.setProperty('panelHeading', True)
        layout.addWidget(title)
        heading = QHBoxLayout()
        heading.setSpacing(12)
        heading.addWidget(QLabel('Image', self))
        self.navigator = ReferenceImageNavigator(self)
        self.image = self.navigator.image
        self.image.setObjectName('reference-cell-image')
        self.image.setAccessibleName('Image and reference role for cell annotation')
        self.image.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.image.setMinimumContentsLength(12)
        self.image.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        for row in self._rows:
            self.image.addItem(row['sample_id'] + (' · ' + self._role(row) if sum(
                other['sample_id'] == row['sample_id'] for other in self._rows) > 1 else ''), self._identity(row))
        self.navigator.refresh()
        heading.addWidget(self.navigator, 1)
        self.annotate = ContentSizedButton('Annotate cells in QuPath', self)
        self.annotate.setObjectName('reference-annotate-cells')
        self.annotate.setProperty('actionRole', 'primary')
        self.annotate.setEnabled(False)
        self.annotate.clicked.connect(self.annotation_requested)
        heading.addWidget(self.annotate)
        layout.addLayout(heading)
        self.metadata = QLabel(self)
        self.metadata.setWordWrap(True)
        self.metadata.setProperty('fieldHelp', True)
        layout.addWidget(self.metadata)
        self.classes = QTableWidget(0, 1, self)
        self.classes.setObjectName('reference-cell-classes')
        self.classes.setHorizontalHeaderLabels(('Cell type',))
        configure_evidence_table(self.classes)
        self.classes.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.classes.horizontalHeader().sectionResized.connect(self._schedule_fit)
        layout.addWidget(self.classes)
        self.availability = QLabel(self)
        self.availability.setWordWrap(True)
        layout.addWidget(self.availability)
        self.availability.hide()
        self.image.currentIndexChanged.connect(self._selected)
        self._selected()

    @staticmethod
    def _role(row):
        return {'training': 'Training', 'validation': 'Validation', 'test': 'Independent test'}[row['split']]

    @staticmethod
    def _identity(row):
        return (row['project_entry_id'], {'training_plan_id': row['training_plan_id'], 'split': row['split']})

    def _selected(self, *_args):
        index = self.image.currentIndex()
        if index < 0:
            return
        row = self._rows[index]
        self.metadata.setText('Subject: ' + row['subject_id'] + ' · ' + self._role(row))
        self.classes.setRowCount(len(row['class_ids']))
        for index, name in enumerate(row['class_ids']):
            self.classes.setItem(index, 0, QTableWidgetItem(name))
        self._schedule_fit()
        self.annotate.setAccessibleName('Annotate reference cells in ' + row['sample_id'])
        self.reference_selected.emit(self._identity(row))

    def select_reference(self, identity):
        index = next((index for index, row in enumerate(self._rows)
                      if isinstance(identity, (tuple, list)) and self._identity(row) == tuple(identity)), -1)
        if index >= 0 and index != self.image.currentIndex():
            with QSignalBlocker(self):
                self.image.setCurrentIndex(index)

    def set_annotation_available(self, available, reason=''):
        self._available = bool(available)
        self.annotate.setEnabled(self._available and bool(self._rows))
        self.availability.setText(reason)
        self.availability.setVisible(bool(reason))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._schedule_fit()

    def _schedule_fit(self, *_args):
        self._fit_timer.start(0)
