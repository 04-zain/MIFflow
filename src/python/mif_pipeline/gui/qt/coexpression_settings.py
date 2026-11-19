"""Choose a small marker subset for the existing saved co-expression figure."""
from PySide6.QtWidgets import QPushButton
from .classifier_channels import ClassifierChannels


class CoexpressionSettings(ClassifierChannels):
    def __init__(self, settings, parent=None):
        markers = settings['markers']
        super().__init__(markers, {marker: marker for marker in markers}, (), parent)
        self.search.setPlaceholderText('Find a marker…')
        self.search.setAccessibleName('Find markers for co-expression')
        self.setAccessibleName('Markers for saved co-expression')
        self.findChild(QPushButton, 'classifier-channels-select-all').hide()

    def _changed(self):
        self.summary.setText(f'{len(self.value())} selected · Choose 2–8 markers')
        self.changed.emit()

    def options(self):
        markers = self.value()
        if not 2 <= len(markers) <= 8:
            raise ValueError('Choose two to eight markers for co-expression.')
        return {'markers': markers}

    def set_resolved(self, options):
        # The normal Results revision check already binds these same selections.
        pass
