"""Inspect one navigable accepted tissue field against a proposed KRONOS patch."""
from __future__ import annotations

import math
import numpy as np

from PySide6.QtCore import Qt, Signal, QRectF, QTimer
from PySide6.QtGui import QColor, QPainterPath, QPen
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QGraphicsRectItem,
    QToolButton, QSizePolicy, QMenu, QPushButton,
)

from .theme import WORKFLOW_PALETTE
from .cell_image_review import CellImageReview
from .region_review import _OutlineView, _InspectedCellOutline
from .researcher_widgets import ContentSizedButton, configure_disclosure


class _TissueView(_OutlineView):
    """Keep the prepared image's full physical extent available while panning."""
    image_extent = None

    def set_image(self, image, metadata):
        first = self.image_item is None
        center = self.mapToScene(self.viewport().rect().center())
        super().set_image(image, metadata)
        if self.image_extent is not None:
            self.setSceneRect(self.image_extent)
            if not first:
                self.centerOn(center)


class _TissueImage(CellImageReview):
    """Use the established channel renderer, legend and appearance persistence."""
    _view_class = _TissueView

    def __init__(self, parent):
        super().__init__(parent)
        self.cell.hide()
        self.cells.hide()
        self.nuclei.hide()
        self.channel.hide()
        self.layout().setSpacing(0)
        self.view.setFixedHeight(360)
        self.view.setAccessibleName('Accepted tissue cells and proposed KRONOS patch')
        self.view.setAccessibleDescription('Click a cell to inspect its patch. Drag to pan; Control and scroll zooms.')
        self.fit = next(button for button in self.findChildren(QPushButton) if button.text() == 'Fit view')
        self.fit.setText('Fit image')
        self.more = QToolButton(self)
        self.more.setText('⋯')
        self.more.setAccessibleName('Image options')
        menu = QMenu(self.more)
        menu.addAction('Image appearance…', self.open_appearance)
        self.more.setMenu(menu)
        self.more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)

    def set_cell_index(self, index, *, reset=True):
        # The bounded frame is a single image, not a gallery selection. Cell
        # contours remain independent clickable items in absolute micrometres.
        self.cell_index = 0
        self.cell.hide()
        self._draw_image()

    def _update_channel_legend(self):
        # Appearance edits change the visible stains. All encoder inputs remain
        # available for the next edit, including channels outside this read.
        if self.options.get('available_channels'):
            self._review_channels = [channel['index'] for channel in self.options['available_channels']]
        super()._update_channel_legend()

    def open_appearance(self, **kwargs):
        return super().open_appearance(available_channels=self.options.get('available_channels'), **kwargs)


class KronosCellPreview(QWidget):
    requested = Signal(object, object)

    def __init__(self, images, parent=None):
        super().__init__(parent)
        self.setObjectName('kronos-example-cell')
        self.revision = 0
        self._parameters = self._result = self._selected_cell = self._patch = None
        self._channel_ids, self._labels, self._cell_items = [], {}, []
        self._busy = self._installing = False
        self._pending_field = self._last_requested_field = None
        self._display_channel_ids = self._pending_options = None
        self._field_timer = QTimer(self)
        self._field_timer.setSingleShot(True)
        self._field_timer.setInterval(180)
        self._field_timer.timeout.connect(self.request)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.toggle = QToolButton(self)
        self.toggle.setText('Inspect cell patches')
        self.toggle.setCheckable(True)
        configure_disclosure(self.toggle)
        layout.addWidget(self.toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        self.body = QWidget(self)
        self.body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        body = QVBoxLayout(self.body)
        body.setContentsMargins(0, 10, 0, 0)
        body.setSpacing(8)
        source = QHBoxLayout()
        source.setSpacing(6)
        self.previous, self.next = QToolButton(self.body), QToolButton(self.body)
        self.previous.setArrowType(Qt.ArrowType.LeftArrow)
        self.previous.setAccessibleName('Previous image')
        self.next.setArrowType(Qt.ArrowType.RightArrow)
        self.next.setAccessibleName('Next image')
        self.image = QComboBox(self.body)
        self.image.setAccessibleName('Source image for cell inspection')
        self.image.setMinimumContentsLength(1)
        self.image.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        for identity, label in images:
            self.image.addItem(label, identity)
        self.image_name = QLabel(self.image.currentText(), self.body)
        self.image_name.setWordWrap(True)
        self.image_name.setProperty('fieldHelp', True)
        for control in (self.previous, self.image, self.next):
            control.setVisible(len(images) > 1)
            source.addWidget(control, 1 if control is self.image else 0)
        self.image_name.setVisible(len(images) == 1)
        source.addWidget(self.image_name, 1)
        self.previous.clicked.connect(lambda: self.image.setCurrentIndex(self.image.currentIndex()-1))
        self.next.clicked.connect(lambda: self.image.setCurrentIndex(self.image.currentIndex()+1))
        self.image.currentIndexChanged.connect(self._source_changed)
        body.addLayout(source)
        self.viewer = _TissueImage(self.body)
        source.addWidget(self.viewer.fit)
        source.addWidget(self.viewer.more)
        self._show_image(False)
        self.viewer.load_requested.connect(self._load_channels)
        self.viewer.view.viewport_changed.connect(self._queue_field)
        self.viewer.view.zoom_changed.connect(self._queue_field)
        self.viewer.view.height_changed.connect(self._queue_field)
        body.addWidget(self.viewer)
        self.status = QLabel(self.body)
        self.status.setWordWrap(True)
        self.status.setProperty('fieldHelp', True)
        body.addWidget(self.status)
        self.load = ContentSizedButton('Try again', self.body)
        self.load.clicked.connect(self.request)
        self.load.hide()
        body.addWidget(self.load, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.body)
        self.body.hide()
        self.toggle.toggled.connect(self._opened)
        self._update_navigation()
        self.set_parameters(None, None, None, (), {})

    def _show_image(self, visible):
        for widget in (self.viewer, self.viewer.fit, self.viewer.more):
            widget.setVisible(visible)

    def _set_status(self, text, *, color=None, tooltip=''):
        self.status.setStyleSheet(f'color: {color}; font-weight: bold;' if color else '')
        self.status.setToolTip(tooltip)
        self.status.setText(text)

    def _update_navigation(self):
        self.previous.setEnabled(self.image.currentIndex() > 0)
        self.next.setEnabled(self.image.currentIndex() < self.image.count()-1)

    def _opened(self, opened):
        self.body.setVisible(opened)
        if opened and self._result is None and not self._busy:
            self.request()
        elif not opened:
            self._field_timer.stop()

    def set_parameters(self, patch_size, dx, dy, channels, labels):
        try:
            values = (int(patch_size), float(dx), float(dy))
            self._parameters = values if all(math.isfinite(value) and value > 0 for value in values) else None
        except (TypeError, ValueError):
            self._parameters = None
        changed = list(channels) != self._channel_ids
        self._channel_ids, self._labels = list(channels), dict(labels)
        if changed and (self._result is not None or self._busy):
            self._invalidate_source()
        if self._selected_cell is not None:
            self._draw_patch()
        elif not channels:
            self._set_status('Select image channels to inspect cells.')
        elif self._parameters is None:
            self._set_status('Enter a positive sampling resolution for both axes.')
        elif self._result is None and not self._busy:
            self._set_status('')
        if self.toggle.isChecked() and self._result is None and not self._busy:
            self.request()

    def _requested_channels(self):
        return self._display_channel_ids or self._channel_ids[:6]

    def _load_channels(self, options):
        if self._installing or self._result is None:
            return
        by_index = {channel['index']: channel['canonical_id'] for channel in self.viewer.options.get('available_channels', ())}
        self._display_channel_ids = [by_index[index] for index in options['channels']]
        self._pending_options = dict(options)
        self._pending_field = self._visible_field()
        self._busy = False
        self.request()

    def _source_changed(self, *_):
        self._update_navigation()
        self._invalidate_source()
        if self.toggle.isChecked():
            self.request()

    def _invalidate_source(self):
        self.revision += 1
        self._field_timer.stop()
        self._busy = False
        self._result = self._selected_cell = self._patch = None
        self._pending_field = self._last_requested_field = None
        self._display_channel_ids = self._pending_options = None
        self._cell_items.clear()
        self._installing = True
        try:
            self.viewer.clear()
            self.viewer.view.image_extent = None
            self._show_image(False)
        finally:
            self._installing = False
        self._set_status('')
        self.load.hide()

    def _visible_field(self):
        if self._result is None:
            return None
        extent = self._result['image_extent_um']
        image = QRectF(extent[0], extent[1], extent[2]-extent[0], extent[3]-extent[1])
        field = self.viewer.view.mapToScene(self.viewer.view.viewport().rect()).boundingRect().intersected(image)
        if field.isEmpty():
            return None
        return [float(v) for v in (field.left(), field.top(), field.right(), field.bottom())]

    def _queue_field(self, *_):
        if self._installing or self._result is None or not self.toggle.isChecked():
            return
        field = self._visible_field()
        previous = self._pending_field or self._last_requested_field
        if field is None or (previous is not None and
                            np.allclose(field, previous, rtol=0, atol=.05)):
            return
        self.revision += 1  # Supersede an in-flight read immediately, before debounce.
        self._busy = False
        self._pending_field = field
        self._field_timer.start()

    def request(self):
        channels = self._requested_channels()
        if self._parameters is None or not channels or self._busy:
            return
        self._field_timer.stop()
        self.revision += 1
        side, dx, dy = self._parameters
        settings = dict(project_entry_id=self.image.currentData(), channel_ids=channels,
            available_channel_ids=list(self._channel_ids),
            patch_size=side, pixel_size_um=[dx, dy], request_key=self.revision,
            viewport_size=[max(260, self.width()), self.viewer.view.height()])
        field = self._pending_field or self._visible_field() or (self._result['field_um'] if self._result is not None else None)
        if field is not None:
            settings['field_um'] = field
        self._last_requested_field = field
        self._busy = True
        self.load.hide()
        if self._result is None:
            self._set_status('Loading tissue…')
        self.requested.emit(settings, self)

    def set_error(self, message):
        self._busy = False
        self._set_status(message)
        self.load.show()

    def set_result(self, result):
        if (result['project_entry_id'] != self.image.currentData() or
                (result.get('request_key') is not None and result['request_key'] != self.revision)):
            return
        previous = self._pending_options or (self.viewer.options if self._result is not None else {})
        first = self._result is None
        selected_id = (self._selected_cell['section_id'], self._selected_cell['cell_id']) if self._selected_cell is not None else None
        options = dict(result['display_options'])
        for key in ('limits', 'channel_colors'):
            options[key] = {**options.get(key, {}), **previous.get(key, {})}
        if previous and set(previous['channels']).issubset(set(map(int, result['bundle']['channel_indices']))):
            options['channels'] = previous['channels']
            options['channel_display'] = previous.get('channel_display', 'colour')
        self._installing = True
        try:
            self._result = result
            extent = result['image_extent_um']
            self.viewer.view.image_extent = QRectF(extent[0], extent[1], extent[2]-extent[0], extent[3]-extent[1])
            self.viewer.set_bundle({**result['bundle'], 'cell_ids': np.asarray([result['project_entry_id']])}, options)
            self._show_image(True)
            if first:
                x0, y0, x1, y1 = result['field_um']
                field = QRectF(x0, y0, x1-x0, y1-y0)
                self.viewer.view._fit = False
                self.viewer.view._focused_field = field
                self.viewer.view.fitInView(field, Qt.AspectRatioMode.KeepAspectRatio)
                self.viewer.view._base_scale = self.viewer.view.transform().m11()
            self._install_cells(result['bundle'], result['cells'])
            self._selected_cell = next((cell for cell in result['cells'] if (cell['section_id'], cell['cell_id']) == selected_id), None)
            self._draw_patch()
        finally:
            self._installing = False
        self._busy = False
        self._pending_options = None
        self._pending_field = None
        self._last_requested_field = self._visible_field()
        self.image_name.setText(result['image_label'])
        self.load.hide()

    def _install_cells(self, bundle, cells):
        for item, _ in self._cell_items:
            self.viewer.view.scene().removeItem(item)
        self._cell_items.clear()
        # A compound path preserves each polygon's holes when clicking inside it.
        paths = {}
        for ring, index in enumerate(bundle['outline_cell_indices']):
            start, stop = bundle['outline_offsets'][ring:ring+2]
            points = bundle['outline_vertices_um'][start:stop]
            if not len(points):
                continue
            kind = str(bundle['outline_kinds'][ring])
            path = paths.setdefault((int(index), kind), QPainterPath())
            path.setFillRule(Qt.FillRule.OddEvenFill)
            path.moveTo(*map(float, points[0]))
            for point in points[1:]:
                path.lineTo(*map(float, point))
            path.closeSubpath()
        for (index, kind), path in paths.items():
            item = _InspectedCellOutline(path, lambda selected=index: self._inspect_cell(selected))
            color = QColor('#7ee3cf' if kind == 'cell' else '#b9dfed')
            color.setAlpha(180)
            item.setData(1, color)
            pen = QPen(color, 1.); pen.setCosmetic(True); item.setPen(pen)
            item.setZValue(4)
            item.setToolTip('Cell ' + str(cells[index]['cell_id']))
            self.viewer.view.scene().addItem(item)
            self._cell_items.append((item, index))

    def _inspect_cell(self, index):
        self._selected_cell = self._result['cells'][index]
        self._draw_patch()

    def _draw_patch(self):
        cell = self._selected_cell
        for item, index in self._cell_items:
            selected = cell is not None and self._result['cells'][index] is cell
            pen = QPen(QColor('#ffffff') if selected else item.data(1), 2. if selected else 1.)
            pen.setCosmetic(True); item.setPen(pen)
        if self._patch is not None:
            self._patch.hide()
        if cell is None:
            self._set_status(self._result.get('message') or ('Click a cell to inspect its patch.' if self._result['cells'] else 'No accepted cells in this field.'))
            return
        if self._parameters is None:
            self._set_status('Enter a positive sampling resolution for both axes.')
            return
        if cell.get('nucleus_center_um') is None:
            self._set_status('This cell has no accepted nucleus to centre the patch.')
            return
        side, dx, dy = self._parameters
        width, height = side*dx, side*dy
        x, y = cell['nucleus_center_um']
        if not all(math.isfinite(v) for v in (width, height)):
            self._set_status('Enter a finite physical patch size.')
            return
        if self._patch is None:
            self._patch = QGraphicsRectItem()
            pen = QPen(QColor('#ffffff'), 1.5, Qt.PenStyle.DashLine)
            pen.setCosmetic(True); self._patch.setPen(pen)
            self._patch.setZValue(9)
            self._patch.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            self.viewer.view.scene().addItem(self._patch)
        self._patch.setRect(QRectF(x-width/2, y-height/2, width, height))
        self._patch.show()
        left, top, right, bottom = cell['cell_bounds_um']
        fits = left >= x-width/2 and right <= x+width/2 and top >= y-height/2 and bottom <= y+height/2
        self._set_status('Selected cell fits inside the patch.' if fits else 'Selected cell extends beyond the patch.',
                         color=WORKFLOW_PALETTE['primary' if fits else 'warning'], tooltip='Cell ' + cell['cell_id'])
