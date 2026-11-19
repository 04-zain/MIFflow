"""Choose one bounded field for saved band, component and reconstruction figures."""

from PySide6.QtCore import Qt, Signal, QSignalBlocker
from PySide6.QtWidgets import QCheckBox, QComboBox, QPushButton, QScrollArea, QSizePolicy, QSpinBox, QVBoxLayout, QWidget

from .form_sections import FormSection, label


class ReconstructionSettings(QWidget):
    changed = Signal()
    region_requested = Signal()

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self._settings = settings
        defaults = settings['defaults']
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        section = FormSection(
            'Region to compare',
            'Coordinates and sizes are pixels in the original image. '
            'X increases to the right and Y increases downward from the top-left corner. '
            f"The maximum region size is {settings['max_width']} × {settings['max_height']} pixels.",
            self,
        )
        layout.addWidget(section)
        self.choose_region = QPushButton('Choose region in QuPath…', self)
        self.choose_region.setObjectName('reconstruction-choose-region')
        self.choose_region.clicked.connect(self.region_requested)
        self.choose_region.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        section.body.insertWidget(2, self.choose_region, 0, Qt.AlignmentFlag.AlignLeft)
        self.x = self._integer(0, settings['size_x'] - 1, defaults['x'])
        self.y = self._integer(0, settings['size_y'] - 1, defaults['y'])
        self.width = self._integer(1, min(settings['max_width'], settings['size_x']), defaults['width'])
        self.height = self._integer(1, min(settings['max_height'], settings['size_y']), defaults['height'])
        section.field('x', 'Left (X)', self.x, '', column=0, span=1, prefix='reconstruction')
        section.field('y', 'Top (Y)', self.y, '', column=1, span=1, prefix='reconstruction')
        section.field('width', 'Width', self.width, '', column=0, span=1, prefix='reconstruction')
        section.field('height', 'Height', self.height, '', column=1, span=1, prefix='reconstruction')
        self.band = QComboBox(self)
        for band in settings['bands']:
            self.band.addItem(band['label'], band['index'])
        self.band.setCurrentIndex(self.band.findData(defaults['band_index']))
        self._band_field = section.field(
            'band', 'Acquired band', self.band,
            'The observed and reconstructed panels show this band. '
            'The relative residual and mean spectra use all acquired bands.',
            prefix='reconstruction',
        )
        self._matched_group = FormSection(
            'Panels to compare', 'Choose one to three entries in each group.', self)
        layout.addWidget(self._matched_group)
        self.acquired_bands = self._channels('Acquired bands', settings['bands'], defaults['band_indices'], column=0)
        self.components = self._channels('Unmixed components', settings['components'], defaults['component_indices'], column=1)
        self.details = label('', self)
        layout.addWidget(self.details)
        self.x.valueChanged.connect(self._changed)
        self.y.valueChanged.connect(self._changed)
        self.width.valueChanged.connect(self._changed)
        self.height.valueChanged.connect(self._changed)
        self.band.currentIndexChanged.connect(self._changed)
        self.set_figure('reconstruction-diagnostic')

    def _channels(self, title, choices, selected, *, column):
        from .classifier_channels import ClassifierChannels
        ids = [str(choice['index']) for choice in choices]
        labels = {str(choice['index']): choice['label'] for choice in choices}
        widget = ClassifierChannels(ids, labels, [str(i) for i in selected], self)
        widget.search.setPlaceholderText('Find an acquired band…' if column == 0 else 'Find an unmixed component…')
        widget.search.setAccessibleName('Find ' + title.lower())
        widget.setAccessibleName(title + ' for matched field')
        for check in widget.findChildren(QCheckBox):
            check.setToolTip(check.text())
        widget.findChild(QPushButton, 'classifier-channels-select-all').hide()
        scroll = widget.findChild(QScrollArea)
        if scroll is not None:
            scroll.setFixedHeight(min(scroll.height(), 130))
        self._matched_group.field('bands' if column == 0 else 'components', title, widget, '',
                                  column=column, span=1, prefix='matched-field')
        widget.changed.connect(self._changed)
        return widget

    def apply_region(self, region):
        """Fill the source-pixel fields only after the complete returned rectangle is valid."""
        if (not isinstance(region, dict) or set(region) != {'x', 'y', 'width', 'height'}
                or any(type(value) is not int for value in region.values())):
            raise ValueError('QuPath did not return a valid image rectangle.')
        x, y, width, height = (region[key] for key in ('x', 'y', 'width', 'height'))
        if (min(x, y) < 0 or not 1 <= width <= self._settings['max_width']
                or not 1 <= height <= self._settings['max_height']
                or x + width > self._settings['size_x'] or y + height > self._settings['size_y']):
            raise ValueError('Choose a smaller rectangle inside the source image.')
        blockers = [QSignalBlocker(getattr(self, key)) for key in region]
        for key, value in region.items():
            getattr(self, key).setValue(value)
        del blockers
        self._changed()

    def set_spectra_only(self, spectra_only):
        self.set_figure('reconstruction-spectra' if spectra_only else 'reconstruction-diagnostic')

    def set_figure(self, figure_id):
        """Keep the region and channel choices when switching between saved field views."""
        self._figure_id = figure_id
        matched = figure_id == 'acquired-components'
        self._band_field.setVisible(figure_id == 'reconstruction-diagnostic')
        self._matched_group.setVisible(matched)
        self.acquired_bands.setVisible(matched)
        self.components.setVisible(matched)
        self.details.clear()
        self.details.setVisible(figure_id != 'reconstruction-spectra')

    def _integer(self, minimum, maximum, value):
        editor = QSpinBox(self)
        editor.setRange(minimum, maximum)
        editor.setValue(value)
        editor.setSuffix(' px')
        return editor

    def _changed(self, *_):
        self.details.clear()
        self.changed.emit()

    def options(self):
        for name in ('x', 'y', 'width', 'height'):
            if not getattr(self, name).hasAcceptableInput():
                raise ValueError('Enter whole-pixel coordinates and sizes within the image bounds.')
        remaining_width = self._settings['size_x'] - self.x.value()
        remaining_height = self._settings['size_y'] - self.y.value()
        if self.width.value() > remaining_width:
            raise ValueError(f'Width must be at most {remaining_width} px at this Left (X) position.')
        if self.height.value() > remaining_height:
            raise ValueError(f'Height must be at most {remaining_height} px at this Top (Y) position.')
        region = {name: getattr(self, name).value() for name in ('x', 'y', 'width', 'height')}
        if self._figure_id == 'acquired-components':
            bands, components = self.acquired_bands.value(), self.components.value()
            if not 1 <= len(bands) <= 3 or not 1 <= len(components) <= 3:
                raise ValueError('Choose one to three acquired bands and one to three saved components.')
            return {**region, 'band_indices': [int(i) for i in bands],
                    'component_indices': [int(i) for i in components]}
        band_index = self.band.currentData()
        if band_index not in {band['index'] for band in self._settings['bands']}:
            raise ValueError('Choose an acquired band for the reconstruction comparison.')
        defaults = self._settings['defaults']
        return {
            'x': self.x.value(), 'y': self.y.value(),
            'width': self.width.value(), 'height': self.height.value(),
            'band_index': band_index,
            'intensity_min': defaults['intensity_min'],
            'intensity_max': defaults['intensity_max'],
            'residual_max': defaults['residual_max'],
        }

    def set_resolved(self, options):
        if self._figure_id == 'acquired-components':
            self.details.setText('Each panel uses independent display limits, recorded with the exported data.')
            return
        low, high = options.get('intensity_min'), options.get('intensity_max')
        if low is not None and high is not None:
            self.details.setText(
                f'Observed and reconstructed panels share display limits {low:.4g}–{high:.4g}.'
            )
