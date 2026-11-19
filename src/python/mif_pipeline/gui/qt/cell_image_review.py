"""Inspect bounded saved cell pixels and vector outlines in the shared image view."""
from __future__ import annotations

import json
from copy import deepcopy
import numpy as np
from PySide6.QtCore import Qt, QSignalBlocker, Signal, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QComboBox, QCheckBox, QPushButton, QDialog, QDoubleSpinBox, QLabel, QMessageBox

from .region_review import _OutlineView, _RegionOutlineItem
from .cell_alignment_view import CellAlignmentView


class CellImageReview(CellAlignmentView, QWidget):
    """Display existing gallery arrays without reading images or changing measurements.

    The image and paths share micrometres relative to the selected cell centre.
    Channel changes reuse the bounded pixels; cell/nucleus visibility affects
    only vector paths. Figure export remains with the existing gallery renderer.
    """
    cell_selected = Signal(int)
    appearance_requested = Signal()
    display_changed = Signal(object)
    load_requested = Signal(object)
    alignment_context_changed = Signal(object)
    alignment_requested = Signal(object)
    _view_class = _OutlineView

    def __init__(self, parent=None):
        super().__init__(parent)
        self.bundle = None
        self.options = {}
        self.cell_index = 0
        self._appearance_pending = None
        self._appearance_seen_uri = None
        self._review_channels = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        toolbar = QHBoxLayout()
        self.cell = QComboBox(self)
        self.cell.setAccessibleName('Cell to inspect')
        self.cell.currentIndexChanged.connect(self.set_cell_index)
        toolbar.addWidget(self.cell)
        self.channel = QComboBox(self)
        self.channel.setAccessibleName('Displayed cell image channel')
        self.channel.currentIndexChanged.connect(lambda _index: self._draw_image())
        toolbar.addWidget(self.channel, 1)
        self.appearance = QPushButton('Image appearance…', self)
        self.appearance.clicked.connect(self.appearance_requested)
        self.appearance.hide()
        layout.addLayout(toolbar)
        self.view = self._view_class(self)
        self.view.channel_legend.changed.connect(self._apply_channel_display)
        # Cell crops retain their native sample values. At high magnification,
        # display those samples directly instead of blurring between pixels.
        self.view.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform,False)
        self.view.setAccessibleName('Selected cell, staining and saved outlines')
        self.view.setMinimumHeight(300)
        self.view.setMaximumHeight(420)
        layout.addWidget(self.view)
        controls = QHBoxLayout()
        self.cells = QCheckBox('Cell outline', self)
        self.nuclei = QCheckBox('Nuclear outline', self)
        for checkbox in (self.cells, self.nuclei):
            checkbox.setChecked(True)
            checkbox.toggled.connect(self._show_outlines)
            controls.addWidget(checkbox)
        controls.addStretch()
        fit = QPushButton('Fit view', self)
        fit.clicked.connect(self.view.reset_view)
        controls.addWidget(fit)
        layout.addLayout(controls)
        self._init_alignment()

    def clear(self):
        self._clear_alignment()
        self.bundle = None
        self._appearance_pending = None
        self._appearance_seen_uri = None
        self._review_channels = None
        self.view.channel_legend.hide()
        self.view.items_by_id.clear()
        self.view.labels.clear()
        self.view.scene().clear()
        self.view.image_item = None

    def set_bundle(self, bundle, options, index=0):
        """Receive the existing NPZ arrays and exact resolved display settings."""
        previous_id = self.selected_cell_id()
        self.bundle = bundle
        self.options = dict(options)
        if self._review_channels is None:self._review_channels=list(options['channels'])
        with QSignalBlocker(self.cell):
            self.cell.clear()
            for number, identity in enumerate(bundle['cell_ids']):
                self.cell.addItem(f'Cell {number + 1}', str(identity))
                self.cell.setItemData(number, str(identity), Qt.ItemDataRole.ToolTipRole)
        self.cell.setVisible(len(bundle['cell_ids']) > 1)
        self._set_channel_choices()
        self.set_cell_index(index, reset=previous_id != str(bundle['cell_ids'][index]))
        self._appearance_ready()

    def selected_cell_id(self):
        return str(self.bundle['cell_ids'][self.cell_index]) if self.bundle is not None else None

    def _set_channel_choices(self):
        previous = self.channel.currentData()
        with QSignalBlocker(self.channel):
            self.channel.clear()
            available = set(map(int, self.bundle['channel_indices']))
            selected = [int(c) for c in self.options['channels'] if int(c) in available]
            if len(selected) > 1:
                self.channel.addItem('Composite', 'composite')
            for channel, name in zip(self.bundle['channel_indices'], self.bundle['channel_names']):
                if int(channel) in selected:
                    self.channel.addItem(str(name), int(channel))
            self._alignment_choices()
            prior = self.channel.findData(previous)
            default = self.channel.findData('composite') if self.options.get('channel_display') == 'colour' else -1
            self.channel.setCurrentIndex(prior if prior >= 0 else max(0, default if default >= 0 else self.channel.findData(selected[0])))

    def set_display_options(self, options):
        """Apply colour/brightness to already loaded channels without another image read."""
        if self.bundle is None:
            return
        selected = set(map(int, options['channels']))
        if not selected.issubset(set(map(int, self.bundle['channel_indices']))):
            raise ValueError('Load the newly selected channels before displaying them.')
        self.options = dict(options)
        self._set_channel_choices()
        self._draw_image()

    def _appearance_context(self):
        metadata=getattr(self,'_metadata',{})
        uri=self.options.get('image_uri') or metadata.get('image_uri')
        widget=self
        while widget is not None:
            path=getattr(widget,'image_appearance_path',None)
            if path is not None:return path,uri
            widget=widget.parentWidget()
        return None,uri

    def _display_descriptor(self):
        selected=self._alignment_fluorescence_choice(self.channel.currentData())
        return dict(mode='composite' if selected=='composite' else self.options.get('channel_display','grayscale'),
            channels=list(self.options['channels']) if selected=='composite' else [selected],
            colors=dict(self.options.get('channel_colors',{})),limits=dict(self.options.get('limits',{})))

    def _update_channel_legend(self):
        if self.bundle is None:return
        channels=self.options.get('available_channels') or getattr(self,'_metadata',{}).get('available_channels')
        if channels is None:
            channels=[dict(index=int(i),label=str(n)) for i,n in zip(self.bundle['channel_indices'],self.bundle['channel_names'])]
        if self._review_channels is None:self._review_channels=list(self.options['channels'])
        by_index={c['index']:c for c in channels}
        channels=[by_index[i] for i in self._review_channels if i in by_index]
        from mif_pipeline.orchestration.saved_cell_gallery import channel_color
        descriptor=self._display_descriptor()
        if any(str(c['index']) not in descriptor['colors'] or str(c['index']) not in descriptor['limits'] for c in channels):
            from mif_pipeline.gui.image_appearance import load_image_appearance
            path,uri=self._appearance_context()
            saved=load_image_appearance(path,uri) if path is not None and uri else None
            if saved:
                descriptor['colors']={**saved.get('colors',{}),**descriptor['colors']}
                descriptor['limits']={**saved.get('limits',{}),**descriptor['limits']}
        for c in channels:
            descriptor['colors'].setdefault(str(c['index']),channel_color(
                {**c,'color':c['color'].lstrip('#')} if isinstance(c.get('color'),str) else c,c['index']))
        self.view.channel_legend.set_display(channels,descriptor,native_rgb=self.channel.currentData()=='he')

    def _save_display(self):
        from mif_pipeline.gui.image_appearance import save_image_appearance
        path,uri=self._appearance_context()
        if path is not None and uri:
            try:
                save_image_appearance(path,uri,self._display_descriptor())
            except OSError as error:
                QMessageBox.warning(self,'Image appearance','Could not save image appearance: '+str(error))

    def _apply_channel_display(self,display,*,save=True,selection_changed=False):
        if self.bundle is None:return
        options={**self.options,'channels':list(display['channels']),'channel_colors':dict(display['colors']),
            'limits':dict(display['limits']),'channel_display':'grayscale' if display['mode']=='grayscale' else 'colour',
            'merged_panel':display['mode']=='composite'}
        if not set(display['channels']).issubset(set(map(int,self.bundle['channel_indices']))):
            self._appearance_pending=(self._appearance_context()[1],deepcopy(display),save,selection_changed)
            self.load_requested.emit(options);return
        indices=list(map(int,self.bundle['channel_indices']))
        for channel in display['channels']:
            if str(channel) not in options['limits']:
                values=self.bundle['pixels'][:,indices.index(channel)];values=values[np.isfinite(values)]
                low,high=np.percentile(values,[1,99]) if values.size else (0.,1.)
                options['limits'][str(channel)]=[float(low),float(high if high>low else low+1)]
        if selection_changed:self._review_channels=list(display['channels'])
        self.set_display_options(options)
        with QSignalBlocker(self.channel):self.channel.setCurrentIndex(max(0,self.channel.findData('composite' if display['mode']=='composite' else display['channels'][0])))
        self._draw_image();self.display_changed.emit(dict(self.options))
        if save:self._save_display()

    def _appearance_ready(self):
        path,uri=self._appearance_context()
        pending=self._appearance_pending
        if pending is not None and pending[0]==uri and list(self.options['channels'])==pending[1]['channels']:
            self._appearance_pending=None
            self._apply_channel_display({**pending[1],'limits':dict(self.options['limits'])},save=pending[2],selection_changed=pending[3])
        if path is not None and uri and self._appearance_seen_uri!=uri:
            from mif_pipeline.gui.image_appearance import load_image_appearance
            self._appearance_seen_uri=uri
            saved=load_image_appearance(path,uri)
            if saved is not None:
                permitted=self._review_channels or self.options['channels']
                visible=[c for c in permitted if c in saved.get('channels',())]
                if visible:
                    restored={**saved,'channels':visible,'mode':'composite' if len(visible)>1 else ('grayscale' if saved.get('mode')=='grayscale' else 'colour')}
                    QTimer.singleShot(0,self,lambda:self._apply_channel_display(restored,save=False) if self._appearance_context()[1]==uri else None)
        self._update_channel_legend()

    def open_appearance(self, *, available_channels=None, edit_field=False):
        """Edit the already loaded channels with the shared image-appearance editor."""
        if self.bundle is None:
            return
        from .image_appearance_editor import ImageAppearanceEditor
        dialog = QDialog(self)
        dialog.setWindowTitle('Image appearance')
        dialog.resize(690, 350)
        layout = QVBoxLayout(dialog)
        editor = ImageAppearanceEditor(dialog)
        layout.addWidget(editor)
        channels = [dict(index=int(index), label=str(name),
                         color=self.options.get('channel_colors', {}).get(str(index), '#ffffff'))
                    for index, name in zip(self.bundle['channel_indices'], self.bundle['channel_names'])]
        if available_channels is not None:
            loaded = {channel['index']: channel for channel in channels}
            channels = [loaded.get(channel['index'], {**channel, 'color': channel.get('color', '#ffffff')}) for channel in available_channels]
        metadata = dict(channels=channels, image_uri='loaded-cell-crops')
        displayed=self._alignment_fluorescence_choice(self.channel.currentData())
        mode = 'composite' if displayed == 'composite' else self.options.get('channel_display', 'grayscale')
        selected = self.options['channels'] if mode == 'composite' else [displayed]
        descriptor = dict(mode=mode, channels=selected,
                          colors=dict(self.options.get('channel_colors', {})), limits=dict(self.options['limits']))
        editor.set_image(metadata, descriptor)
        editor.appearance_auto.setToolTip('Use the 1st and 99th percentiles across the loaded cell crops.')

        field = None
        if edit_field:
            row = QHBoxLayout()
            row.addWidget(QLabel('Field height' if 'field_aspect' in self.options else 'Field width and height', dialog))
            field = QDoubleSpinBox(dialog)
            field.setRange(.1, 10000)
            field.setSuffix(' µm')
            field.setValue(self.options.get('crop_um', 40))
            row.addWidget(field)
            row.addStretch()
            layout.insertLayout(0, row)

        def apply(display):
            crop_um = field.value() if field is not None else self.options.get('crop_um', 40)
            if not set(display['channels']).issubset(set(map(int, self.bundle['channel_indices']))) or crop_um != self.options.get('crop_um', 40):
                self._appearance_pending=(self._appearance_context()[1],deepcopy(display),True,True)
                self.load_requested.emit({**self.options, 'channels': display['channels'], 'limits': display['limits'],
                    'channel_colors': display['colors'], 'crop_um': crop_um,
                    'channel_display': 'grayscale' if display['mode'] == 'grayscale' else 'colour',
                    'merged_panel': display['mode'] == 'composite'})
                dialog.accept()
                return
            limits = dict(self.options['limits'])
            indices = list(map(int, self.bundle['channel_indices']))
            for channel in display['channels']:
                key = str(channel)
                if key in display['limits']:
                    limits[key] = display['limits'][key]
                else:
                    values = self.bundle['pixels'][:, indices.index(channel)]
                    values = values[np.isfinite(values)]
                    low, high = np.percentile(values, [1, 99]) if values.size else (0., 1.)
                    limits[key] = [float(low), float(high if high > low else low + max(abs(low)*.01, 1.))]
            options = {**self.options, 'channels': display['channels'], 'limits': limits,
                       'channel_colors': display['colors'],
                       'channel_display': 'grayscale' if display['mode'] == 'grayscale' else 'colour',
                       'merged_panel': display['mode'] == 'composite'}
            self._review_channels=list(display['channels'])
            self.set_display_options(options)
            with QSignalBlocker(self.channel):
                self.channel.setCurrentIndex(max(0, self.channel.findData('composite' if display['mode'] == 'composite' else display['channels'][0])))
            self._draw_image()
            self.display_changed.emit(dict(self.options))
            self._save_display()
            editor.set_image(metadata, {**display, 'limits': limits})

        editor.requested.connect(apply)
        editor.apply_requested.connect(apply)
        close = QPushButton('Close', dialog)
        close.clicked.connect(dialog.accept)
        layout.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        dialog.open()
        self._appearance_dialog = dialog
        return dialog

    def set_cell_index(self, index, *, reset=True):
        if self.bundle is None or not 0 <= index < len(self.bundle['cell_ids']):
            return
        self.cell_index = index
        with QSignalBlocker(self.cell):
            self.cell.setCurrentIndex(index)
        for item in self.view.items_by_id.values():
            self.view.scene().removeItem(item)
        self.view.items_by_id.clear()
        for ring, cell in enumerate(self.bundle['outline_cell_indices']):
            if int(cell) != index:
                continue
            start, stop = self.bundle['outline_offsets'][ring:ring+2]
            vertices = self.bundle['outline_vertices_um'][start:stop]
            if not len(vertices):
                continue
            kind = str(self.bundle['outline_kinds'][ring])
            path = QPainterPath()
            path.moveTo(*map(float, vertices[0]))
            for point in vertices[1:]:
                path.lineTo(*map(float, point))
            path.closeSubpath()
            item = _RegionOutlineItem(path)
            color = QColor('#ffe052' if kind == 'nucleus' else '#22d3ee')
            pen = QPen(color, 1.4)
            pen.setCosmetic(True)
            item.setPen(pen)
            item.setData(1, color)
            item.setData(2, QColor(0, 0, 0, 0))
            item.setData(3, False)
            item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            item.setZValue(5)
            self.view.scene().addItem(item)
            self.view.items_by_id[f'{kind}-{ring}'] = item
        self._alignment_context_updated()
        self._draw_image()
        self._show_outlines()
        if reset:
            self.view.reset_view()
        self.cell_selected.emit(index)

    def _draw_image(self):
        if self.bundle is None or self.channel.currentIndex() < 0:
            return
        selected = self._alignment_fluorescence_choice(self.channel.currentData())
        channels = self.options['channels'] if selected == 'composite' else [selected]
        indices = list(map(int, self.bundle['channel_indices']))
        rgb = np.zeros((*self.bundle['pixels'].shape[2:], 3), dtype=float)
        valid = np.ones(rgb.shape[:2], dtype=bool)
        for channel in channels:
            pixels = self.bundle['pixels'][self.cell_index, indices.index(int(channel))]
            low, high = self.options['limits'][str(channel)]
            values = np.clip((pixels - low) / (high - low), 0, 1)
            valid &= np.isfinite(values)
            color = QColor(self.options.get('channel_colors', {}).get(str(channel), '#ffffff'))
            weights = (color.redF(), color.greenF(), color.blueF()) if selected == 'composite' or self.options.get('channel_display') == 'colour' else (1., 1., 1.)
            rgb += np.nan_to_num(values)[..., None] * weights
        rgb = np.clip(rgb, 0, 1)
        rgb[~valid] = (218/255, 221/255, 225/255)
        pixels = np.ascontiguousarray(np.rint(rgb * 255).astype(np.uint8))
        image = QImage(pixels.data, pixels.shape[1], pixels.shape[0], pixels.strides[0], QImage.Format.Format_RGB888).copy()
        left, right, bottom, top = map(float, self.bundle['extents_um'][self.cell_index])
        self.view.set_image(image, dict(width_um=right-left, height_um=bottom-top,
            origin_x_um=left, origin_y_um=top))
        self._update_alignment_layers()
        self._update_channel_legend()

    def _show_outlines(self):
        for key, item in self.view.items_by_id.items():
            visible = self.nuclei.isChecked() if key.startswith('nucleus') else self.cells.isChecked()
            item.setOpacity(1. if visible else 0.)
        self.view.viewport().update()
