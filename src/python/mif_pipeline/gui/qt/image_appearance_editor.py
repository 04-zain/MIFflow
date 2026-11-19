"""Edit channel appearance using the same bounded overview as region review."""
from __future__ import annotations

import json
import math
from pathlib import Path
from urllib.parse import unquote, urlparse

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import (QWidget, QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QComboBox, QTableWidget, QHeaderView, QCheckBox, QLineEdit, QPushButton, QColorDialog)
from .figure_preview import _FigureView
from .jobs import JobAccess
from mif_pipeline.gui.image_appearance import load_image_appearance, save_image_appearance


class ImageAppearanceEditor(QWidget):
    """Collect one per-image display recipe; applying emits validated choices."""
    requested = Signal(object)
    apply_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._image_metadata = None
        self._appearance_rows = {}
        self._updating_appearance = False
        self._previous_appearance_mode = 'grayscale'
        self._composite_channels = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        row = QHBoxLayout()
        row.addWidget(QLabel('Display', self))
        self.appearance_mode = QComboBox(self)
        for label, mode in [('Grayscale', 'grayscale'), ('Colour', 'colour'), ('Composite', 'composite')]:
            self.appearance_mode.addItem(label, mode)
        row.addWidget(self.appearance_mode)
        row.addStretch()
        layout.addLayout(row)
        self.appearance_table = QTableWidget(0, 4, self)
        self.appearance_table.setHorizontalHeaderLabels(['Channel', 'Colour', 'Display minimum', 'Display maximum'])
        self.appearance_table.setAccessibleName('Image channel appearance')
        self.appearance_table.verticalHeader().hide()
        self.appearance_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.appearance_table.setMinimumHeight(160)
        self.appearance_table.setStyleSheet(
            'QTableWidget::item { padding: 0; }'
            'QCheckBox { padding: 0 6px; }'
            'QLineEdit { border: 1px solid transparent; border-radius: 0; '
            'padding: 0 9px; background: transparent; }'
            'QLineEdit[activeTableRow="true"], QCheckBox[activeTableRow="true"] '
            '{ background: #d5ebe5; }'
            'QLineEdit:focus { border-color: #006b70; background: #ffffff; }'
        )
        layout.addWidget(self.appearance_table)
        self.image_status = QLabel('', self)
        self.image_status.setWordWrap(True)
        self.image_status.hide()
        layout.addWidget(self.image_status)
        actions = QHBoxLayout()
        self.appearance_auto = QPushButton('Auto contrast', self)
        self.appearance_auto.setToolTip('Use the 1st and 99.5th percentiles of the image overview for the selected channels.')
        self.appearance_auto.clicked.connect(self._auto_contrast)
        actions.addWidget(self.appearance_auto)
        actions.addStretch()
        self.apply = QPushButton('Apply appearance', self)
        self.apply.setProperty('actionRole', 'primary')
        self.apply.clicked.connect(self._apply_appearance)
        actions.addWidget(self.apply)
        layout.addLayout(actions)
        self.appearance_mode.currentIndexChanged.connect(self._appearance_mode_changed)

    def set_image(self, metadata, descriptor):
        if self._image_metadata is None or self._image_metadata.get('image_uri') != metadata.get('image_uri'):
            self._composite_channels = None
        self._image_metadata = metadata
        self._populate_appearance(metadata, descriptor)
        self.image_status.hide()

    def _appearance_mode_changed(self, *_):
        if self._updating_appearance:
            return
        mode = self.appearance_mode.currentData()
        checked = [index for index, row in self._appearance_rows.items() if row[0].isChecked()]
        if self._previous_appearance_mode == 'composite' and mode != 'composite':
            self._composite_channels = checked
        if mode == 'composite' and self._previous_appearance_mode != 'composite' and self._composite_channels is not None:
            for index, row in self._appearance_rows.items():
                row[0].setChecked(index in self._composite_channels)
        if mode in {'grayscale', 'colour'}:
            keep = checked[0] if checked else None
            for index, row in self._appearance_rows.items():
                row[0].setChecked(index == keep)
        for row in self._appearance_rows.values():
            for widget in row:
                widget.setEnabled(mode != 'rgb')
        self.appearance_auto.setEnabled(mode != 'rgb')
        self._previous_appearance_mode = mode

    def _appearance_checked(self, index, checked):
        if checked and not self._updating_appearance and self.appearance_mode.currentData() != 'composite':
            for other, row in self._appearance_rows.items():
                if other != index:
                    row[0].setChecked(False)

    def _choose_channel_color(self, index):
        button = self._appearance_rows[index][1]
        selected = QColorDialog.getColor(QColor(button.property('channelColor')), self, 'Channel colour')
        if selected.isValid():
            self._set_channel_color(button, selected.name())

    @staticmethod
    def _set_channel_color(button, value):
        button.setProperty('channelColor', value)
        button.setText(value)
        color = QColor(value)
        # QColor's 16-bit lightness puts saturated blue slightly above .5.
        components = [color.redF(), color.greenF(), color.blueF()]
        linear = [c/12.92 if c <= .04045 else ((c+.055)/1.055)**2.4 for c in components]
        luminance = sum(c*w for c, w in zip(linear, (.2126, .7152, .0722)))
        foreground = '#111111' if luminance > .179 else '#ffffff'
        button.setStyleSheet(f'background-color: {value}; color: {foreground};')

    def _populate_appearance(self, metadata, descriptor):
        """Present exact stored limits; blank limits request automatic contrast once."""
        self._updating_appearance = True
        try:
            if metadata.get('native_rgb') and self.appearance_mode.findData('rgb') < 0:
                self.appearance_mode.addItem('Native RGB', 'rgb')
            elif not metadata.get('native_rgb'):
                rgb_index = self.appearance_mode.findData('rgb')
                if rgb_index >= 0:
                    self.appearance_mode.removeItem(rgb_index)
            self.appearance_mode.setCurrentIndex(self.appearance_mode.findData(descriptor['mode']))
            self.appearance_table.setRowCount(len(metadata['channels']))
            self._appearance_rows.clear()
            for row_number, channel in enumerate(metadata['channels']):
                index, key = channel['index'], str(channel['index'])
                check = QCheckBox(channel['label'], self.appearance_table)
                check.setChecked(index in descriptor['channels'])
                check.toggled.connect(lambda checked, i=index: self._appearance_checked(i, checked))
                color = QPushButton(self.appearance_table)
                color.setFixedSize(110, 30)
                self._set_channel_color(color, descriptor.get('colors', {}).get(key, channel['color']))
                color.clicked.connect(lambda _checked=False, i=index: self._choose_channel_color(i))
                low, high = QLineEdit(self.appearance_table), QLineEdit(self.appearance_table)
                for editor in (low, high):
                    editor.setPlaceholderText('Automatic')
                    editor.installEventFilter(self)
                limits = descriptor.get('limits', {}).get(key)
                if limits is not None:
                    low.setText(format(limits[0], '.12g')); high.setText(format(limits[1], '.12g'))
                    # Keep unedited values exact despite readable text formatting.
                    low.setProperty('exactValue', limits[0]); low.setProperty('originalText', low.text())
                    high.setProperty('exactValue', limits[1]); high.setProperty('originalText', high.text())
                self._appearance_rows[index] = (check, color, low, high)
                for column, widget in enumerate((check, color, low, high)):
                    self.appearance_table.setCellWidget(row_number, column, widget)
            row_height = max(38, self.appearance_table.fontMetrics().height() + 18)
            self.appearance_table.verticalHeader().setDefaultSectionSize(row_height)
            self.appearance_table.setMinimumHeight(max(160,
                self.appearance_table.horizontalHeader().sizeHint().height()
                + min(3, len(metadata['channels'])) * row_height
                + 2 * self.appearance_table.frameWidth()))
            self.appearance_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
            self.appearance_table.setColumnWidth(1, 118)
        finally:
            self._updating_appearance = False
        self._previous_appearance_mode = descriptor['mode']
        if descriptor['mode'] == 'composite':
            self._composite_channels = list(descriptor['channels'])
        self._appearance_mode_changed()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.FocusIn and isinstance(watched, QLineEdit):
            for row in self._appearance_rows.values():
                active = watched in row[2:]
                for widget in (row[0], *row[2:]):
                    if widget.property('activeTableRow') != active:
                        widget.setProperty('activeTableRow', active)
                        widget.style().unpolish(widget)
                        widget.style().polish(widget)
                        widget.update()
        return super().eventFilter(watched, event)

    def _appearance_values(self):
        mode = self.appearance_mode.currentData()
        channels, colors, limits = [], {}, {}
        for index, (check, color, low, high) in self._appearance_rows.items():
            key = str(index)
            if check.isChecked():
                channels.append(index)
            colors[key] = color.property('channelColor')
            if low.text().strip() or high.text().strip():
                try:
                    pair = [editor.property('exactValue') if editor.text() == editor.property('originalText')
                            else float(editor.text()) for editor in (low, high)]
                except (ValueError, TypeError):
                    raise ValueError('Enter both display limits, or leave both blank for automatic contrast.')
                if not all(math.isfinite(v) for v in pair) or pair[1] <= pair[0]:
                    raise ValueError('Each display maximum must exceed its minimum.')
                limits[key] = pair
        if mode == 'rgb':
            channels = [0, 1, 2]
        if not channels:
            raise ValueError('Select at least one channel to display.')
        if mode in {'grayscale', 'colour'} and len(channels) > 1:
            raise ValueError('Select one channel, or choose Composite to combine channels.')
        return dict(mode=mode, channels=channels, colors=colors, limits=limits,
                    channel_ids=[channel.get('id') for channel in self._image_metadata['channels']])

    def _apply_appearance(self):
        try:
            descriptor = self._appearance_values()
        except ValueError as error:
            self.image_status.setText(str(error)); self.image_status.show()
            return
        self.apply_requested.emit(descriptor)

    def _auto_contrast(self):
        for check, _color, low, high in self._appearance_rows.values():
            if check.isChecked():
                low.clear(); high.clear()
        try:
            descriptor = self._appearance_values()
        except ValueError as error:
            self.image_status.setText(str(error)); self.image_status.show()
            return
        self.requested.emit(descriptor)


class ImageAppearanceDialog(QDialog):
    """Preview and save appearance for prepared images without rerunning analysis.

    We reuse one bounded reader while editing an image. The application job bridge
    performs pixel reads; only completed renderings can replace saved appearance.
    """
    saved = Signal(str)

    def __init__(self, images, store_path, jobs, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Image appearance')
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(960, 760)
        self._jobs, self._store = jobs, store_path
        self._job = None
        self._reader = None
        self._closing = False
        self._previous_display = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        header = QHBoxLayout()
        header.addWidget(QLabel('Image', self))
        self.images = QComboBox(self)
        for name, path in images:
            uri = str(path)
            local = Path(unquote(urlparse(uri).path)) if uri.startswith('file:') else Path(uri)
            self.images.addItem(name, local)
        header.addWidget(self.images, 1)
        layout.addLayout(header)
        self.preview = _FigureView(QPixmap(), self)
        self.preview.setMinimumHeight(240)
        layout.addWidget(self.preview, 3)
        self.editor = ImageAppearanceEditor(self)
        layout.addWidget(self.editor, 2)
        self.status = QLabel('Loading image…', self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        close = QPushButton('Close', self)
        close.clicked.connect(self.close)
        layout.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        self.editor.requested.connect(self._render)
        self.editor.apply_requested.connect(lambda descriptor: self._render(descriptor, save=True))
        self.images.currentIndexChanged.connect(self._load_image)
        self._jobs.job_succeeded.connect(self._ready)
        self._jobs.job_failed.connect(self._failed)
        self.finished.connect(self._finish)
        self._load_image()

    def _load_image(self, *_args):
        if self._reader is not None:
            self._reader.close()
            self._reader = None
        path = self.images.currentData()
        self._previous_display = load_image_appearance(self._store, path.as_uri()) or {}
        self._render(self._previous_display or None)

    def _render(self, descriptor, *, save=False):
        if self._job is not None or self._closing:
            return
        path = self.images.currentData()
        self.editor.setEnabled(False)
        self.images.setEnabled(False)
        self.status.setText('Loading image…')
        self._requested = descriptor
        self._save_requested = save
        def read():
            from mif_pipeline.io.processed_image import ProcessedImage
            from mif_pipeline.orchestration.region_review_image import render_prepared_overview, CHUNK_CACHE_BYTES
            if self._reader is None:
                self._reader = ProcessedImage(path, cache_bytes=CHUNK_CACHE_BYTES)
            attribute = 'region-display:' + json.dumps(descriptor) if descriptor else 'cluster_id'
            return render_prepared_overview(self._reader, attribute=attribute, image_uri=path.as_uri())
        try:
            self._job = self._jobs.submit('image_appearance', read, access=JobAccess.READ)
        except RuntimeError as error:
            self.status.setText(str(error))
            self.editor.setEnabled(True)
            self.images.setEnabled(True)

    def _ready(self, job_id, payload):
        if job_id != self._job:
            return
        self._job = None
        if self._closing:
            self._dispose()
            return
        image = QImage.fromData(payload, 'PNG')
        metadata = json.loads(image.text('mif-region-image'))
        display = metadata['display']
        known = dict(display)
        known['colors'] = {str(c['index']): c['color'] for c in metadata['channels']}
        known['colors'].update(self._previous_display.get('colors', {}))
        known['colors'].update((self._requested or {}).get('colors', {}))
        known['colors'].update(display['colors'])
        known['limits'] = {**self._previous_display.get('limits', {}), **(self._requested or {}).get('limits', {}), **display['limits']}
        known['channel_ids'] = [c.get('id') for c in metadata['channels']]
        self._previous_display = known
        self.editor.set_image(metadata, known)
        self.preview.scene().clear()
        self.preview.scene().addPixmap(QPixmap.fromImage(image))
        self.preview.setSceneRect(self.preview.scene().itemsBoundingRect())
        self.preview.reset_view()
        self.editor.setEnabled(True)
        self.images.setEnabled(True)
        if self._save_requested:
            try:
                save_image_appearance(self._store, metadata['image_uri'], known)
            except OSError as error:
                self.status.setText('Could not save image appearance: ' + str(error))
                return
            self.saved.emit(metadata['image_uri'])
        self.status.setText('')

    def _failed(self, job_id, error):
        if job_id != self._job:
            return
        self._job = None
        if self._closing:
            self._dispose()
            return
        self.editor.setEnabled(True)
        self.images.setEnabled(True)
        self.status.setText('Could not display this image. ' + error.message)

    def _finish(self, *_args):
        self._closing = True
        if self._job is None:
            self._dispose()

    def _dispose(self):
        if self._reader is not None:
            self._reader.close()
            self._reader = None
        self.deleteLater()
