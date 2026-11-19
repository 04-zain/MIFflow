"""An interactive interval over an accepted measurement histogram."""
import math
import json
from PySide6.QtCore import Signal, Qt, QTimer, QSignalBlocker, QRectF
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from matplotlib.widgets import SpanSelector


class MeasurementDistributionReview(QWidget):
    changed = Signal()
    requested = Signal()
    inspect_requested = Signal(tuple)
    field_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._selector = None
        self._ids = ()
        self._accepted_options = None
        self._examples = ()
        self._gallery_options = {}
        self._available_channels = None
        self._interval = None
        self._units = ''
        self._range = None
        self._field_bundle = None
        self._field_viewport = {}
        self._region_view_key = None
        self._value_marker = None
        self._hover = None
        self._histogram = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.region_row = QWidget(self)
        regions = QHBoxLayout(self.region_row)
        regions.setContentsMargins(0, 0, 0, 0)
        regions.addWidget(QLabel('Region', self.region_row))
        self.region = QComboBox(self.region_row)
        self.region.setAccessibleName('Measurement region')
        self.region.setMinimumContentsLength(22)
        self.region.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.region.setToolTip('Filter cells by their saved region membership. Measurements use the original cell or nuclear outlines.')
        self.region.addItem('All areas', None)
        self.region.currentIndexChanged.connect(self._edited)
        regions.addWidget(self.region, 1)
        layout.addWidget(self.region_row)
        self.region_row.hide()
        self.phenotype_row = QWidget(self)
        phenotypes = QHBoxLayout(self.phenotype_row)
        phenotypes.setContentsMargins(0, 0, 0, 0)
        phenotypes.addWidget(QLabel('Phenotype', self.phenotype_row))
        self.phenotype = QComboBox(self.phenotype_row)
        self.phenotype.setAccessibleName('Accepted phenotype')
        self.phenotype.setMinimumContentsLength(22)
        self.phenotype.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.phenotype.addItem('All phenotypes', None)
        self.phenotype.currentIndexChanged.connect(self._edited)
        phenotypes.addWidget(self.phenotype, 1)
        layout.addWidget(self.phenotype_row)
        self.phenotype_row.hide()
        self.canvas = FigureCanvasQTAgg(Figure(figsize=(7, 3.4), layout='constrained'))
        self.canvas.setMinimumHeight(270)
        self.canvas.setMaximumHeight(300)
        self.canvas.setMinimumWidth(0)
        self.canvas.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.canvas.setAccessibleName('Measurement distribution and selected value range')
        self.canvas.setToolTip('Click a bar to select its value range, or drag an interval. Hover for the exact bin limits and count. Shift+Left/Right adjusts the lower limit; Ctrl+Left/Right adjusts the upper limit.')
        self.canvas.setAccessibleDescription(self.canvas.toolTip())
        self.bounds_axis, self.axis = self.canvas.figure.subplots(
            2, 1, sharex=True, gridspec_kw={'height_ratios': (1.5, 7), 'hspace': .04})
        self._bound_labels = ()
        self._count_label = None
        self.canvas.mpl_connect('draw_event', self._position_bound_labels)
        layout.addWidget(self.canvas)
        self.canvas.hide()
        hint = QLabel('Select a value range to locate its cells in the tissue. Zoom in and select an outline to inspect its saved value.', self)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.availability = QLabel(self)
        self.availability.setWordWrap(True)
        self.availability.setProperty('fieldHelp', True)
        layout.addWidget(self.availability)
        self.example_toolbar = QWidget(self)
        example_toolbar = QHBoxLayout(self.example_toolbar)
        example_toolbar.setContentsMargins(0, 0, 0, 0)
        self.example_caption = QLabel(self)
        self.example_caption.setWordWrap(True)
        example_toolbar.addWidget(self.example_caption, 1)
        layout.addWidget(self.example_toolbar)
        self.example_toolbar.hide()
        from .cell_image_field import CellImageFieldReview
        self.cell_view = CellImageFieldReview(self)
        self.cell_view.field_requested.connect(self._field_viewport_changed)
        self.cell_view.field_cell_selected.connect(self._inspect_field_cell)
        self.cell_view.appearance_requested.connect(self._open_appearance)
        self.cell_view.display_changed.connect(self._display_changed)
        self.cell_view.load_requested.connect(self._load_appearance)
        layout.addWidget(self.cell_view)
        self.cell_view.hide()
        self.example_exports = QWidget(self)
        exports = QHBoxLayout(self.example_exports)
        exports.setContentsMargins(0, 0, 0, 0)
        from PySide6.QtWidgets import QMenu
        self.export_examples = QPushButton('Export tissue view…', self.example_exports)
        export_menu = QMenu(self.export_examples)
        export_menu.addAction('Figure…', lambda: self._export_examples(False))
        export_menu.addAction('Field data…', lambda: self._export_examples(True))
        self.export_examples.setMenu(export_menu)
        exports.addStretch()
        exports.addWidget(self.export_examples)
        layout.addWidget(self.example_exports)
        self.example_exports.hide()
        self.canvas.mpl_connect('key_press_event', self._key_example)
        self.canvas.mpl_connect('button_press_event', self._histogram_pressed)
        self.canvas.mpl_connect('button_release_event', self._histogram_released)
        self.canvas.mpl_connect('motion_notify_event', self._histogram_hovered)
        self.canvas.mpl_connect('figure_leave_event', self._histogram_hovered)
        self._press_x = None

    def options(self):
        """Return exact selected bounds; the readable label does not round the request."""
        options = dict(zip(('lower', 'upper'), self._interval)) if self._interval is not None else {}
        if self.region.currentData() is not None:
            options['region'] = self.region.currentData()
        if self.phenotype.currentData() is not None:
            options['phenotype'] = self.phenotype.currentData()
        return options

    def _describe_interval(self):
        if not self._bound_labels:
            return
        limits = self._interval or (0, 0)
        notation = '.2e' if any(0 < abs(value) < .005 for value in limits) else '.2f'
        for label, value in zip(self._bound_labels, limits):
            label.set_visible(self._interval is not None)
            label.set_position((value, .45))
            label.set_text(format(value, notation))
        if self._interval is not None:
            lower, upper = self._interval
            self.canvas.setAccessibleDescription(
                self.canvas.toolTip() + f' Selected interval: {lower!r} to {upper!r} {self._units}. Both endpoints included.')

    def _position_bound_labels(self, event):
        """Keep labels on their boundary positions, separating close or edge labels."""
        if not self._bound_labels or self._interval is None:
            return
        bounds = self.bounds_axis.get_window_extent(event.renderer)
        positions = [self.bounds_axis.transData.transform((value, .45))[0] for value in self._interval]
        boxes = [label.get_window_extent(event.renderer) for label in self._bound_labels]
        widths = [box.width for box in boxes]
        alignments = ['left' if x - width / 2 < bounds.x0 else
                      'right' if x + width / 2 > bounds.x1 else 'center'
                      for x, width in zip(positions, widths)]
        def extent(x, width, alignment):
            return (x, x + width) if alignment == 'left' else (x - width, x) if alignment == 'right' else (x - width / 2, x + width / 2)
        left = extent(positions[0], widths[0], alignments[0])
        right = extent(positions[1], widths[1], alignments[1])
        overlap = left[1] + 6 > right[0]
        changed = False
        for i, label in enumerate(self._bound_labels):
            x, y, alignment = self._interval[i], .45, alignments[i]
            if overlap:
                x, alignment = sum(self._interval) / 2, 'center'
                y = .5 + (boxes[1].height + 4) / (2 * bounds.height) if i == 0 else .5 - (boxes[0].height + 4) / (2 * bounds.height)
            if label.get_ha() != alignment or label.get_position() != (x, y):
                label.set_ha(alignment); label.set_position((x, y)); changed = True
        if changed:
            self.canvas.draw_idle()

    def _edited(self):
        if self.options() == self._accepted_options:
            return
        self._accepted_options = None
        self._ids = ()
        if self._field_bundle is not None:
            self._field_viewport=self.cell_view.field_options()
            self.cell_view.setEnabled(False)
        self._field_bundle = None
        self.example_exports.setEnabled(False)
        self.example_caption.clear()
        if self._count_label is not None:
            self._count_label.set_text('Counting selected cells…')
        self.canvas.draw_idle()
        self.changed.emit()
        self.requested.emit()

    def _selected(self, lower, upper):
        if not math.isfinite(lower) or not math.isfinite(upper) or lower > upper:
            return
        if self._interval == (lower, upper):
            return
        self._interval = (lower, upper)
        self._describe_interval()
        self._edited()

    def set_resolved(self, values):
        previous_interval = self._interval
        with QSignalBlocker(self.region):
            self.region.clear()
            for item in values.get('region_choices') or [{'label':'All areas', 'selection':None}]:
                self.region.addItem(item['label'], item['selection'])
            self.region.setCurrentIndex(next((index for index in range(self.region.count())
                if self.region.itemData(index) == values.get('region')), 0))
        self.region_row.setVisible(self.region.count() > 1)
        with QSignalBlocker(self.phenotype):
            self.phenotype.clear()
            self.phenotype.addItem('All phenotypes', None)
            for label in values.get('phenotype_choices', ()):
                self.phenotype.addItem(label, label)
            self.phenotype.setCurrentIndex(max(0, self.phenotype.findData(values.get('phenotype'))))
        self.phenotype_row.setVisible(self.phenotype.count() > 1)
        if self._selector is not None:
            self._selector.disconnect_events()
        self.axis.clear()
        self.bounds_axis.clear()
        self.bounds_axis.set_ylim(-.2, 1.2)
        self.bounds_axis.set_axis_off()
        self._bound_labels = tuple(self.bounds_axis.text(0, .45, '',
            transform=self.bounds_axis.get_xaxis_transform(), ha='center', va='center') for _ in range(2))
        self._count_label = self.axis.text(.98, .97, '', transform=self.axis.transAxes,
            ha='right', va='top', bbox={'facecolor': 'white', 'alpha': .85, 'edgecolor': 'none', 'pad': 3})
        edges, counts = values['edges'], values['counts']
        self._range = (edges[0], edges[-1]) if counts else None
        if counts:
            self.axis.stairs(counts, edges, fill=True, color='#146b78', alpha=.85)
        else:
            self.axis.set_xticks([])
            self.axis.set_yticks([0])
        self._measurement=values['measurement']
        self._histogram=(tuple(edges),tuple(counts))
        self.axis.set_xlabel(values['measurement'] + (f" ({values['units']})" if values['units'] else ''), parse_math=False)
        self.axis.set_ylabel('Cells')
        self.axis.set_ylim(bottom=0)
        self.axis.spines[['top', 'right']].set_visible(False)
        self._selector = SpanSelector(self.axis, self._selected, 'horizontal', interactive=True,
            drag_from_anywhere=True, state_modifier_keys={'clear': ''},
            props={'facecolor': '#e6a23c', 'alpha': .25})
        self._selector.set_active(bool(counts))
        interval = values['interval']
        self._ids = tuple(interval['cell_ids']) if interval else ()
        self._examples = tuple(values.get('examples', ()))
        self._feature = values.get('feature', {})
        self._value_marker=None
        self._hover=self.axis.annotate('',xy=(0,0),xytext=(12,12),textcoords='offset points',
            bbox=dict(boxstyle='round,pad=.4',fc='white',ec='#88a8a6'),zorder=20)
        self._hover.set_visible(False)
        self._units = values['units'] or ''
        if interval is not None:
            self._interval = (interval['lower'], interval['upper'])
            self._selector.extents = (interval['lower'], interval['upper'])
            selected = f"Selected: {interval['count']:,} / {sum(counts):,} cells"
        else:
            self._interval = (previous_interval or (0., 1.)) if values.get('region') is not None or values.get('phenotype') is not None else None
            selected = 'No measured values are available.'
        self._describe_interval()
        self._accepted_options = self.options()
        self._count_label.set_text(selected)
        availability = f"Measured: {sum(counts):,} of {values['total']:,} cells"
        if values['missing'] or values['nonfinite']:
            availability += f" · Missing: {values['missing']:,} · Invalid: {values['nonfinite']:,}"
        self.availability.setText(availability)
        self.availability.setVisible(bool(values['missing'] or values['nonfinite']))
        self.canvas.show()
        self._match_interface_font(self.canvas.figure)
        self.canvas.draw_idle()
        if counts or values.get('region') is not None or values.get('phenotype') is not None:
            self.cell_view.show()
            QTimer.singleShot(0,self._request_field)
        else:
            self.clear_field()

    def _histogram_pressed(self,event):
        self._press_x=event.x if event.inaxes is self.axis and event.button==1 else None

    def _histogram_released(self,event):
        if self._press_x is not None and event.inaxes is self.axis and event.xdata is not None and abs(event.x-self._press_x)<3:
            import bisect
            edges,counts=self._histogram
            if counts and edges[0]<=event.xdata<=edges[-1]:
                index=min(len(counts)-1,max(0,bisect.bisect_right(edges,event.xdata)-1))
                self._selector.extents=(edges[index],edges[index+1])
                self._selected(edges[index],edges[index+1])
                self.canvas.draw_idle()
        self._press_x=None

    def _histogram_hovered(self,event):
        if self._hover is None:
            return
        if event.inaxes is not self.axis or event.xdata is None or not self._histogram[1]:
            if self._hover.get_visible():
                self._hover.set_visible(False);self.canvas.draw_idle()
            return
        import bisect
        edges,counts=self._histogram
        index=min(len(counts)-1,max(0,bisect.bisect_right(edges,event.xdata)-1))
        self._hover.xy=(event.xdata,event.ydata)
        self._hover.set_text(f'Bar: {edges[index]:.12g}–{edges[index+1]:.12g}\n{counts[index]:,} cells in this bar')
        right=event.xdata>(edges[0]+edges[-1])/2
        self._hover.set_position((-12,12) if right else (12,12));self._hover.set_ha('right' if right else 'left')
        self._hover.set_visible(True);self.canvas.draw_idle()

    def _field_viewport_changed(self,options):
        self._field_viewport=dict(options)
        self._request_field()

    def _request_field(self):
        """Retire the previous field export while replacement pixels are requested."""
        self._field_bundle = None
        self.example_exports.setEnabled(False)
        self.field_requested.emit()

    def _inspect_field_cell(self,row):
        value=row.get('value')
        self.example_caption.setText('Saved value: ' + (f'{value:.4g}' if value is not None and math.isfinite(value) else 'Unavailable')
            + (' · In selected range' if row['selected'] else ' · Outside selected range'))
        self.example_caption.setToolTip(f'Exact saved value: {value!r}\nCell {row["cell_id"]}')
        self.example_toolbar.show()
        if self._value_marker is not None:
            self._value_marker.remove();self._value_marker=None
        if value is not None and math.isfinite(value):
            self._value_marker=self.axis.axvline(value,color='#c75222',linewidth=1.6,zorder=8)
        self.canvas.draw_idle()

    def _match_interface_font(self, figure):
        """Match the embedded plot to its page; exports retain their own style."""
        from PySide6.QtGui import QFontInfo
        from matplotlib.text import Text
        from matplotlib.colors import to_rgba
        font = QFontInfo(self.font())
        background = self.palette().window().color().name()
        foreground = self.palette().windowText().color().name()
        figure.set_facecolor(background)
        for axis in figure.axes:
            axis.set_facecolor(background)
            axis.tick_params(colors=foreground)
            for spine in axis.spines.values():
                spine.set_color('#bdcccc')
        if self._count_label is not None:
            self._count_label.get_bbox_patch().set_facecolor(background)
        for text in figure.findobj(Text):
            text.set_fontfamily(font.family())
            text.set_fontsize(font.pointSizeF())
            if to_rgba(text.get_color()) == (0., 0., 0., 1.):
                text.set_color(foreground)

    def _display_changed(self, options):
        self._gallery_options = dict(options)

    def _key_example(self, event):
        if event.key not in {'shift+left', 'shift+right', 'ctrl+left', 'ctrl+right'} or self._interval is None or self._range is None:
            return
        lower, upper = self._interval
        step = (self._range[1] - self._range[0]) / 256
        shift = step if event.key.endswith('right') else -step
        if event.key.startswith('shift'):
            lower = min(upper, max(self._range[0], lower + shift))
        else:
            upper = max(lower, min(self._range[1], upper + shift))
        self._selector.extents = (lower, upper)
        self._selected(lower, upper)
        self.canvas.draw_idle()

    def field_options(self, settings, appearance=None):
        """Retain the exact range and appearance while requesting a different field."""
        self._available_channels = settings['channels']
        if not self._gallery_options:
            channels = [row['index'] for row in settings['channels'][:3]]
            target = self._feature.get('image_channel_index')
            if target is not None:
                nuclear = next((row['index'] for row in settings['channels']
                    if row['label'].split(' ')[0].casefold() in {'dapi','dna','hoechst'}), None)
                channels = list(dict.fromkeys([target] + ([nuclear] if nuclear is not None else [])))
            self.cell_view._review_channels=list(channels)
            self._gallery_options = dict(channels=channels,
                channel_display='colour', merged_panel=len(channels)>1, limits={})
            if appearance:
                visible=[c for c in channels if c in appearance.get('channels',())] or channels
                self._gallery_options.update(channels=visible,limits=dict(appearance.get('limits', {})),
                    channel_colors=dict(appearance.get('colors', {})),
                    channel_display='grayscale' if appearance.get('mode') == 'grayscale' and len(visible)==1 else 'colour',
                    merged_panel=len(visible)>1)
        viewport = self.cell_view.view.viewport()
        return {**self._gallery_options, 'measurement':self._measurement, **self.options(),
                'viewport_size':[min(1024,viewport.width()),min(1024,viewport.height())],
                **self._field_viewport}

    def _open_appearance(self):
        self.cell_view.open_appearance(available_channels=self._available_channels)

    def _load_appearance(self, options):
        self._gallery_options = dict(options)
        if self._field_bundle is not None:
            self._field_viewport=self.cell_view.field_options()
        self._request_field()

    def clear_field(self):
        self._field_bundle = None
        self.cell_view.clear()
        self.cell_view.hide()
        self.example_exports.hide()

    def set_field_bundle(self, bundle):
        first = self.cell_view._raw_bundle is None
        self._field_bundle=bundle
        self.cell_view.set_field_bundle(bundle)
        self._gallery_options=dict(self.cell_view.options)
        self.cell_view.setEnabled(True)
        self.cell_view.show()
        self.example_exports.setEnabled(True)
        self.example_exports.show()
        metadata = bundle['metadata']
        image = (metadata.get('source_frame', {}).get('image_sha256'),
                 tuple(metadata.get('region_image_identity', ())))
        region = metadata.get('display_options', {}).get('region')
        key = (image, json.dumps(region, sort_keys=True))
        previous, self._region_view_key = self._region_view_key, key
        if (key != previous or first) and (region is not None or previous is not None):
            bounds = metadata.get('region_bounds_um')
            if bounds is None and region is not None and previous is not None and previous[0] == image:
                return
            full = self.cell_view._full_rect
            if bounds is None:
                target = full
            else:
                left,top,right,bottom = bounds
                margin = max(5., max(right-left,bottom-top)*.04)
                target = QRectF(left-margin,top-margin,right-left+2*margin,bottom-top+2*margin).intersected(full)
            self.cell_view.view._fit = False
            self.cell_view.view.fitInView(target,Qt.AspectRatioMode.KeepAspectRatio)
            self.cell_view.view.centerOn(target.center())
            self._field_viewport = self.cell_view.field_options()
            self.cell_view._schedule_field()

    def _export_examples(self, data_only):
        """Export only the displayed field and its exact retained arrays on request."""
        if self._field_bundle is None:
            return
        from .figure_export import _choose_path, _save_bytes, _DATA_FORMATS, export_figure
        from mif_pipeline.cell_analysis.measurement_spatial_plots import field_figure, field_npz, field_cells_csv
        options={**self.cell_view.options,
            'displayed_channel':self.cell_view.channel.currentData(),
            'show_cell_outlines':self.cell_view.cells.isChecked(),
            'show_nuclear_outlines':self.cell_view.nuclei.isChecked(),
            'density_grid_factor':self.cell_view._density_factor,
            'selected_cell_id':self.cell_view.selected_cell_id()}
        if not data_only:
            export_figure(self,field_figure(self._field_bundle,options),'measurement-tissue-field.svg')
            return
        path=_choose_path(self,'measurement-tissue-field.npz',_DATA_FORMATS)
        if path is not None:
            payload=field_npz(self._field_bundle,options) if path.suffix.lower()=='.npz' else field_cells_csv(self._field_bundle)
            _save_bytes(payload,path)
