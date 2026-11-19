"""Link a fitted cluster and exact embedding cell to continuous tissue context."""
from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QBoxLayout, QLabel, QMenu

from .cell_image_field import CellImageFieldReview
from .researcher_widgets import ContentSizedButton


class ClusterImageField(CellImageFieldReview):
    def __init__(self, parent=None):
        super().__init__(parent)
        controls = self.layout().itemAt(self.layout().count()-1).layout()
        widgets = []
        while controls.count():
            item = controls.takeAt(0)
            if item.widget() is not None:widgets.append(item.widget())
        toggles, actions = QHBoxLayout(), QHBoxLayout()
        actions.addStretch()
        for widget in widgets:
            (toggles if widget in (self.cells,self.nuclei) else actions).addWidget(widget)
        toggles.addStretch()
        controls.addLayout(toggles);controls.addLayout(actions)
        self._field_controls = controls
        self._adapt_controls()

    def _adapt_controls(self):
        self._field_controls.setDirection(QBoxLayout.Direction.TopToBottom if self.width()<540 else QBoxLayout.Direction.LeftToRight)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        self._adapt_controls()

    def set_field_bundle(self, bundle):
        super().set_field_bundle(bundle)
        for item in self.view.items_by_id.values():
            row = self._by_id[item.data(0)]
            item.setToolTip(f'{row["cluster_id"]}\nCell {row["cell_id"]}')
        self.view.setAccessibleName('Selected cluster in tissue')
        self.view.setAccessibleDescription('Zoom into the spatial count overlay, inspect dense areas, or select an individual cell outline.')


class ClusterFieldReview(QWidget):
    requested = Signal()
    gallery_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.revision = 0
        self.show_image_summary = True
        self._selection = self._bundle = self._focus = None
        self._display, self._viewport, self._channels = {}, {}, []
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self.requested)
        layout = QVBoxLayout(self);layout.setContentsMargins(0,0,0,0);layout.setSpacing(10)
        self.status = QLabel(self);self.status.setWordWrap(True);layout.addWidget(self.status)
        self.cell_view = ClusterImageField(self);layout.addWidget(self.cell_view)
        self.cell_value = QLabel(self);self.cell_value.setWordWrap(True);self.cell_value.hide();layout.addWidget(self.cell_value)
        actions = QHBoxLayout()
        self.retry = ContentSizedButton('Retry tissue view', self);self.retry.hide();self.retry.clicked.connect(self.refresh)
        actions.addWidget(self.retry);actions.addStretch()
        self.export = ContentSizedButton('Export tissue view…', self)
        menu = QMenu(self.export);menu.addAction('Figure…',lambda:self._export(False));menu.addAction('Field data…',lambda:self._export(True))
        menu.addSeparator();self.gallery_action = menu.addAction('Representative cell gallery…',self.gallery_requested)
        self.export.setMenu(menu);self.export.setEnabled(False);actions.addWidget(self.export);layout.addLayout(actions)
        self.cell_view.field_requested.connect(self._field_changed)
        self.cell_view.field_cell_selected.connect(self._inspect_cell)
        self.cell_view.appearance_requested.connect(lambda:self.cell_view.open_appearance(available_channels=self._channels))
        self.cell_view.display_changed.connect(self._display_changed)
        self.cell_view.load_requested.connect(self._load_appearance)

    def configure(self, cluster, image, measurement=None, *, focus_cell=None):
        selection = dict(cluster_id=cluster,image_identity=list(image),profile_measurement=measurement)
        if selection == self._selection and focus_cell is None:
            return
        if self._selection is None or self._selection['image_identity'] != list(image):
            self._viewport, self._display = {}, {}
            self.cell_view.clear()
        self._selection, self._focus = selection, focus_cell
        self.refresh()

    def refresh(self, *, disable=True):
        if self._selection is None:
            return
        self.revision += 1
        self.cell_value.hide();self.retry.hide();self.export.setEnabled(False)
        if disable:self.cell_view.setEnabled(False)
        self.status.setText('Loading clustered cells in tissue…')
        self.status.show()
        self._timer.start()

    def _field_changed(self, options):
        self._viewport = dict(options)
        self.refresh(disable=False)

    def _display_changed(self, options):
        self._display = dict(options)

    def _load_appearance(self, options):
        self._display = dict(options)
        self.refresh()

    def options(self):
        if self._selection is None:
            return None
        viewport = self.cell_view.view.viewport()
        return {**self._display, **self._viewport, **self._selection,
            **({'focus_cell_id':self._focus} if self._focus is not None else {}),
            'viewport_size':[max(1,min(1024,viewport.width())),max(1,min(1024,viewport.height()))],
            'channel_display':self._display.get('channel_display','colour'), 'merged_panel':self._display.get('merged_panel',True)}

    def set_bundle(self, bundle):
        metadata = bundle['metadata'];self._channels = metadata['available_channels']
        self._bundle = bundle
        appearance=self._display
        self.cell_view.set_field_bundle(bundle);self._display = dict(self.cell_view.options)
        for key in ('channel_colors','limits'):
            self._display[key]={**appearance.get(key,{}),**self._display.get(key,{})}
        if metadata.get('focus_cell_id') is not None:
            left,top,right,bottom = metadata['field_um']
            self.cell_view._updating = True
            try:
                self.cell_view.view._fit = False
                self.cell_view.view.fitInView(QRectF(left,top,right-left,bottom-top),Qt.AspectRatioMode.KeepAspectRatio)
                self.cell_view.view.centerOn((left+right)/2,(top+bottom)/2)
            finally:self.cell_view._updating = False
            image = tuple(self._selection['image_identity']) if self._selection else None
            focused = [row for row in metadata['visible_cells']
                if row['cell_id'] == metadata['focus_cell_id'] and
                (image is None or tuple(row[key] for key in ('sample_id','project_entry_id','slide_id')) == image)]
            if len(focused) == 1:
                self.cell_view._inspect_cell([self.cell_view._cell_key(focused[0])])
            self.cell_view._position_location()
            self._viewport = self.cell_view.field_options()
            self._focus = None
        self.cell_view.setEnabled(True);self.export.setEnabled(True);self.retry.hide()
        n,total = metadata['total_selected'],metadata['total_measured']
        self.status.setText(f'{n:,} of {total:,} clustered cells in this image' + (f' ({100*n/total:.3g}%).' if total else '.'))
        self.status.setVisible(self.show_image_summary)

    def set_error(self, message):
        self.status.setText(message);self.status.show();self.cell_view.setEnabled(False);self.export.setEnabled(False);self.retry.show()

    def _inspect_cell(self, row):
        # The outline tooltip already gives this cell's exact ID and cluster.
        self.cell_value.clear();self.cell_value.hide()

    def _export(self, data_only):
        if self._bundle is None or not self.export.isEnabled():
            return
        from .figure_export import _choose_path, _save_bytes, _DATA_FORMATS, export_figure
        from mif_pipeline.cell_analysis.measurement_spatial_plots import field_figure,field_npz
        options = {**self.cell_view.options,'displayed_channel':self.cell_view.channel.currentData(),
            'show_cell_outlines':self.cell_view.cells.isChecked(),'show_nuclear_outlines':self.cell_view.nuclei.isChecked(),
            'density_grid_factor':self.cell_view._density_factor,'selected_cell_id':self.cell_view.selected_cell_id()}
        bundle = self._bundle
        mode = options['displayed_channel']
        if mode in ('he','fluorescence_he'):
            alignment = self.cell_view._alignment_data
            if alignment is None:
                return
            import numpy as np
            options.update(fluorescence_channel=self.cell_view._alignment_fluorescence_choice(mode),
                           alignment_opacity=self.cell_view.alignment_opacity.value()/100)
            bundle = {**bundle, 'metadata':{**bundle['metadata'],'aligned_reference':dict(alignment['metadata'])},
                      'aligned_reference_pixels':alignment['pixels'],
                      'aligned_reference_origin_um':np.asarray(alignment['origin_um']),
                      'aligned_reference_pixel_size_um':np.asarray(alignment['pixel_size_um'])}
        if not data_only:
            export_figure(self,field_figure(bundle,options),'cluster-tissue-field.svg');return
        formats = _DATA_FORMATS if self._bundle['metadata']['outline_detail_available'] else {k:v for k,v in _DATA_FORMATS.items() if v=='npz'}
        path = _choose_path(self,'cluster-tissue-field.npz',formats)
        if path is not None:
            if path.suffix.lower()=='.npz':
                metadata = {key:value for key,value in bundle['metadata'].items() if key not in {'measurement','lower','upper'}}
                metadata['selection_kind'] = 'cluster'
                metadata['visible_cells'] = [{key:value for key,value in row.items() if key!='value'} for row in metadata['visible_cells']]
                payload=field_npz({**bundle,'metadata':metadata},
                    {key:value for key,value in options.items() if key not in {'measurement','lower','upper'}})
            else:
                import csv,io
                stream=io.StringIO();fields=('sample_id','project_entry_id','slide_id','section_id','cell_id','cluster_id','selected','x_um','y_um')
                writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader()
                writer.writerows({key:row[key] for key in fields} for row in self._bundle['metadata']['visible_cells'])
                payload=stream.getvalue().encode()
            _save_bytes(payload,path)
