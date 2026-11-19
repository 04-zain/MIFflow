"""Compact navigation for one image inside an all-images review."""
from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import QWidget, QHBoxLayout, QComboBox, QCompleter, QSizePolicy

from .researcher_widgets import ContentSizedButton


class CohortImageNavigation(QWidget):
    image_changed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('cohort-image-navigation')
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.previous = ContentSizedButton('←', self)
        self.previous.setAccessibleName('Previous image')
        self.previous.setToolTip('Previous image in analysis order')
        layout.addWidget(self.previous)
        self.images = QComboBox(self)
        self.images.setAccessibleName('Image to inspect, with matching percentage for that image')
        self.images.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.images.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.images.setMinimumContentsLength(12)
        self.images.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.images.setMaxVisibleItems(12)
        layout.addWidget(self.images, 1)
        self.next = ContentSizedButton('→', self)
        self.next.setAccessibleName('Next image')
        self.next.setToolTip('Next image in analysis order')
        layout.addWidget(self.next)
        self.previous.clicked.connect(lambda: self.images.setCurrentIndex(self.images.currentIndex()-1))
        self.next.clicked.connect(lambda: self.images.setCurrentIndex(self.images.currentIndex()+1))
        self.images.currentIndexChanged.connect(self._changed)
        self.hide()

    def current_key(self):
        return self.images.currentData()

    def set_images(self, rows):
        previous = self.current_key()
        if len(rows) > 1 and not self.images.isEditable():
            self.images.setEditable(True)
            self.images.lineEdit().setPlaceholderText('Find an image…')
            self.images.completer().setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
            self.images.completer().setFilterMode(Qt.MatchFlag.MatchContains)
            self.images.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        with QSignalBlocker(self.images):
            self.images.clear()
            for row in rows:
                if 'included_cells' not in row:
                    self.images.addItem(row['label'], row['key'])
                    self.images.setItemData(self.images.count() - 1, row['label'], Qt.ItemDataRole.ToolTipRole)
                    continue
                total, matching = row['included_cells'], row.get('matching_cells')
                percentage = 'counting…' if matching is None else (f'{100*matching/total:.3g}% match' if total else '—')
                self.images.addItem(f"{row['label']} · {percentage}", row['key'])
                detail = 'Matching percentage is being calculated.' if matching is None else (
                    f'{matching:,} of {total:,} included cells in this image match the current selection.' if total else
                    'No included cells in this image; a matching percentage is not defined.')
                self.images.setItemData(self.images.count()-1,detail,Qt.ItemDataRole.ToolTipRole)
            index = self.images.findData(previous)
            if index >= 0:
                self.images.setCurrentIndex(index)
        self.setVisible(self.images.count() > 1)
        self._buttons()

    def _buttons(self):
        index = self.images.currentIndex()
        self.previous.setEnabled(index > 0)
        self.next.setEnabled(0 <= index < self.images.count()-1)

    def _changed(self):
        self._buttons()
        self.image_changed.emit(self.current_key())
