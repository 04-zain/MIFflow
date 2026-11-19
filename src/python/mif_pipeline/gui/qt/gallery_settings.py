"""Compact cell-gallery controls within the existing Results page."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QCheckBox, QLabel, QLineEdit, QPushButton, QMenu, QSizePolicy, QStyle, QStyleOptionButton, QStylePainter
from .form_sections import FormSection, label
from .researcher_widgets import ElidedComboBox


class _PhenotypeButton(QPushButton):
    """Keep selected phenotype text within the field; retain its full value."""

    def minimumSizeHint(self):
        size = super().minimumSizeHint()
        size.setWidth(0)
        return size

    def paintEvent(self, event):
        option = QStyleOptionButton()
        self.initStyleOption(option)
        width = self.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, self).width()
        width -= self.style().pixelMetric(QStyle.PixelMetric.PM_MenuButtonIndicator, option, self) + 8
        option.text = self.fontMetrics().elidedText(option.text, Qt.TextElideMode.ElideRight, max(0, width))
        painter = QStylePainter(self)
        painter.drawControl(QStyle.ControlElement.CE_PushButton, option)


class _ChannelCheckBox(QCheckBox):
    """Elide display text without changing channel names or checkbox behaviour."""

    def __init__(self, text, parent):
        super().__init__(text, parent)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setToolTip(text)
        self.setAccessibleName(text)

    def paintEvent(self, event):
        option = QStyleOptionButton()
        self.initStyleOption(option)
        width = self.style().subElementRect(QStyle.SubElement.SE_CheckBoxContents, option, self).width()
        option.text = self.fontMetrics().elidedText(option.text, Qt.TextElideMode.ElideRight, max(0, width))
        painter = QStylePainter(self)
        painter.drawControl(QStyle.ControlElement.CE_CheckBox, option)


class GallerySettings(QWidget):
    changed = Signal()

    def __init__(self, settings, parent=None, *, selection=True):
        super().__init__(parent)
        layout = QVBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0)
        section = FormSection('Cell examples', parent=self); layout.addWidget(section)
        self.phenotype = _PhenotypeButton('All phenotypes',self)
        phenotype_menu=QMenu(self.phenotype);self.phenotype.setMenu(phenotype_menu)
        self._phenotypes=[]
        for name in settings['phenotypes']:
            action=phenotype_menu.addAction(name);action.setCheckable(True)
            action.toggled.connect(self._phenotypes_changed);self._phenotypes.append(action)
        if selection and self._phenotypes: section.field('phenotype', 'Phenotypes', self.phenotype, 'Include any selected phenotype; no selection includes all.', prefix='gallery')
        else: self.phenotype.hide()
        self._measurements=tuple(settings.get('measurements',()))
        self._ranges=[]
        ranges=QWidget(self);self._range_layout=QVBoxLayout(ranges);self._range_layout.setContentsMargins(0,0,0,0)
        self.add_range=QPushButton('Add measurement range',ranges);self.add_range.clicked.connect(self._add_range)
        self._range_layout.addWidget(self.add_range)
        if selection and self._measurements:
            section.field('ranges','Measurement ranges',ranges,'Every range must match. Both limits are inclusive; a blank limit is unbounded.',prefix='gallery')
        else:ranges.hide()
        self.count = QSpinBox(self); self.count.setRange(1, 8); self.count.setValue(4)
        if selection: section.field('count', 'Number of cells', self.count, '', column=0, span=1, prefix='gallery')
        else: self.count.hide()
        self.seed = QSpinBox(self); self.seed.setRange(0, 2147483647)
        if selection: section.field('seed', 'Selection seed', self.seed, 'The same seed and accepted result select the same cells.', column=1, span=1, prefix='gallery')
        else: self.seed.hide()
        self.crop = QDoubleSpinBox(self); self.crop.setRange(.1, 10000); self.crop.setValue(40); self.crop.setSuffix(' µm')
        section.field('crop', 'Crop width and height', self.crop, '', prefix='gallery')
        self.ids = QPlainTextEdit(self); self.ids.setMaximumHeight(78); self.ids.setPlaceholderText('Optional: one cell ID per line')
        if selection: section.field('ids', 'Choose cell IDs', self.ids, 'Entered IDs replace the sampled selection (up to eight cells).', prefix='gallery')
        else: self.ids.hide()
        channels = FormSection('Channels and brightness', 'Choose up to six channels. Initial limits use the pooled 1st and 99th percentiles of the selected crops; each channel uses the same limits for every cell.', self)
        layout.addWidget(channels)
        self.editors = {}
        channels.grid.addWidget(label('Channel'), 0, 0)
        channels.grid.addWidget(label('Display minimum'), 0, 1)
        channels.grid.addWidget(label('Display maximum'), 0, 2)
        for row, channel in enumerate(settings['channels'], 1):
            checked = _ChannelCheckBox(channel['label'], self); checked.setChecked(row <= 3)
            low, high = QLineEdit(self), QLineEdit(self)
            for widget in (low, high): widget.setPlaceholderText('Automatic'); widget.setMaximumWidth(130)
            low.setAccessibleName(channel['label'] + ' display minimum')
            high.setAccessibleName(channel['label'] + ' display maximum')
            channels.grid.addWidget(checked, row, 0); channels.grid.addWidget(low, row, 1); channels.grid.addWidget(high, row, 2)
            self.editors[channel['index']] = (checked, low, high)
            checked.toggled.connect(self._selection_changed)
            low.textEdited.connect(self.changed); high.textEdited.connect(self.changed)
        display = FormSection('Panel display', parent=self); layout.addWidget(display)
        self.channel_display = QComboBox(self)
        self.channel_display.addItem('Grayscale', 'grayscale')
        self.channel_display.addItem('Colour', 'colour')
        display.field('mode', 'Channel display', self.channel_display, '', prefix='gallery')
        self.merged_panel = QCheckBox('Show merged colour panel', self)
        display.field('merged', '', self.merged_panel,
                      'Combine the selected channels using their colours and the same display limits.', prefix='gallery')
        self.channel_display.currentIndexChanged.connect(self.changed)
        self.merged_panel.toggled.connect(self.changed)
        self.details = label('', self); layout.addWidget(self.details)
        for widget in (self.count, self.seed, self.crop): widget.valueChanged.connect(self._selection_changed)
        self.ids.textChanged.connect(self._selection_changed)

    def _selection_changed(self, *_):
        self.details.clear(); self.changed.emit()

    def _phenotypes_changed(self,*_):
        names=[action.text() for action in self._phenotypes if action.isChecked()]
        self.phenotype.setText('All phenotypes' if not names else names[0] if len(names)==1 else f'{len(names)} phenotypes selected')
        self.phenotype.setToolTip('\n'.join(names))
        self.phenotype.setAccessibleDescription('\n'.join(names))
        self._selection_changed()

    def _add_range(self):
        row=QWidget(self);layout=QHBoxLayout(row);layout.setContentsMargins(0,0,0,0)
        measurement=ElidedComboBox(row)
        for item in self._measurements:
            measurement.addItem(item['measurement']+(f" ({item['units']})" if item.get('units') else ''),item['measurement'])
            measurement.setItemData(measurement.count()-1, measurement.itemText(measurement.count()-1), Qt.ItemDataRole.ToolTipRole)
        measurement.setAccessibleName('Measurement to filter')
        measurement.setToolTip(measurement.currentText())
        layout.addWidget(measurement,1)
        low,high=QLineEdit(row),QLineEdit(row)
        for editor,title in ((low,'Minimum'),(high,'Maximum')):
            editor.setPlaceholderText(title);editor.setAccessibleName('Inclusive measurement '+title.lower())
            editor.setMaximumWidth(130);editor.textChanged.connect(self._selection_changed);layout.addWidget(editor)
        remove=QPushButton('Remove',row);layout.addWidget(remove)
        record=(row,measurement,low,high);self._ranges.append(record)
        def discard():
            self._ranges.remove(record);self._range_layout.removeWidget(row);row.deleteLater()
            self.add_range.setEnabled(True);self._selection_changed()
        remove.clicked.connect(discard);measurement.currentIndexChanged.connect(self._selection_changed)
        self._range_layout.insertWidget(self._range_layout.count()-1,row)
        self.add_range.setEnabled(len(self._ranges)<8)
        self._selection_changed()

    def options(self):
        limits = {}
        channels = []
        for index, (checked, low, high) in self.editors.items():
            if checked.isChecked():
                channels.append(index)
                if low.text().strip() or high.text().strip():
                    try:
                        limits[str(index)] = [editor.property('resolvedValue')
                            if editor.text() == editor.property('resolvedText') else float(editor.text())
                            for editor in (low, high)]
                    except ValueError: raise ValueError('Enter both display limits as numbers, or leave both blank.')
        ranges=[]
        for _row,measurement,low,high in self._ranges:
            try:
                lower=float(low.text()) if low.text().strip() else None
                upper=float(high.text()) if high.text().strip() else None
            except ValueError:raise ValueError('Enter numeric measurement limits, or leave a limit blank.')
            ranges.append(dict(measurement=measurement.currentData(),lower=lower,upper=upper))
        return dict(phenotypes=[action.text() for action in self._phenotypes if action.isChecked()],
                    measurement_ranges=ranges,count=self.count.value(), seed=self.seed.value(),
                    channel_display=self.channel_display.currentData(), merged_panel=self.merged_panel.isChecked(),
                    crop_um=self.crop.value(), cell_ids=self.ids.toPlainText().split(), channels=channels, limits=limits)

    def set_resolved(self, options):
        for index, (_checked, low, high) in self.editors.items():
            limits = options['limits'].get(str(index))
            if limits is not None:
                for editor, value in zip((low, high), limits):
                    text = format(value, '.12g')
                    editor.setProperty('resolvedText', text)
                    editor.setProperty('resolvedValue', float(value))
                    editor.setText(text)
        total=options.get('selection_count')
        count=len(options['selected_cell_ids'])
        self.details.setText(f'{count:,} selected cells' if options.get('cell_ids') else
            f'{count:,} repeatable examples' + (f' from {total:,} matching cells' if total is not None else ''))
        self.details.setToolTip('Selected cell IDs:\n'+'\n'.join(options['selected_cell_ids']))
