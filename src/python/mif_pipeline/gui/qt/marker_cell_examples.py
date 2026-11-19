"""Explicit image inspection of the current marker gate with shared gallery controls."""
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QComboBox, QLabel, QPushButton
from .cell_image_review import CellImageReview



class MarkerCellExamples(QDialog):
    requested = Signal()

    def __init__(self, images, parent=None, *, title='Inspect marker cells', embedded=False):
        super().__init__(parent)
        self._embedded = embedded
        if embedded:
            self.setWindowFlags(Qt.WindowType.Widget)
        self.setWindowTitle(title)
        if not embedded:
            self.resize(1050,850)
        self._display_options = None
        self._plots = ()
        layout=QVBoxLayout(self)
        if embedded:
            layout.setContentsMargins(0,0,0,0)
        self.image=QComboBox(self)
        for row in images:
            self.image.addItem(' · '.join(dict.fromkeys((row['sample_id'],row['slide_id']))),
                               [row[k] for k in ('sample_id','project_entry_id','slide_id')])
        self.image.setAccessibleName('Image for cell examples')
        self.image.setVisible(self.image.count()>1);layout.addWidget(self.image)
        self.status=QLabel('Select Refresh cells to load the cell examples.',self)
        self.status.setWordWrap(True);layout.addWidget(self.status)
        self.cell_view=CellImageReview(self);layout.addWidget(self.cell_view,1)
        self.view=self.cell_view.view
        row=QHBoxLayout();layout.addLayout(row)
        self.refresh=QPushButton('Refresh cells',self);self.refresh.clicked.connect(self.requested);row.addWidget(self.refresh)
        self.appearance=self.cell_view.appearance
        self.cell_view.appearance_requested.connect(self._open_appearance)
        self.cell_view.display_changed.connect(self._appearance_changed)
        self.cell_view.load_requested.connect(self._load_appearance)
        self.appearance.hide()
        row.addStretch()
        self.export=QPushButton('Export figure…',self);self.export.clicked.connect(lambda:self._export(False));row.addWidget(self.export)
        self.data=QPushButton('Export data…',self);self.data.clicked.connect(lambda:self._export(True));row.addWidget(self.data)
        for b in (self.export,self.data):b.setEnabled(False)
        if not embedded:
            close=QPushButton('Close',self);close.clicked.connect(self.close);row.addWidget(close)
        self.image.currentIndexChanged.connect(self._image_changed)

    def _image_changed(self):
        self._display_options = None
        self._plots=();self.cell_view.clear()
        self.appearance.hide()
        self.status.setText('Select Refresh cells to inspect this image.')
        self.refresh.setEnabled(True)
        for b in (self.export,self.data):b.setEnabled(False)

    def _open_appearance(self):
        self.cell_view.open_appearance(available_channels=self._display_options['available_channels'], edit_field=True)

    def _appearance_changed(self, options):
        self._display_options = dict(options)

    def _load_appearance(self, options):
        self._display_options = dict(options)
        self.requested.emit()

    def options(self):
        result={'image_identity':self.image.currentData(),'channel_display':'colour','merged_panel':True}
        if self._display_options is not None:
            result.update({key:self._display_options[key] for key in
                ('crop_um','channels','limits','channel_colors','channel_display','merged_panel',
                 'field_aspect','pyramid_level') if key in self._display_options})
            if 'field_aspect' in result:
                viewport=self.cell_view.view.viewport()
                result['field_aspect']=max(.25,min(4.,viewport.width()/max(1,viewport.height())))
        return result

    def set_busy(self):
        self.refresh.setEnabled(False);self.status.setText('Loading cell images…')

    def set_error(self,message):
        self.refresh.setEnabled(True);self.status.setText(message)
        for button in (self.export,self.data):button.setEnabled(False)

    def set_plots(self, plots):
        if not plots:
            self.cell_view.clear()
            self.set_error('No cell images are available for this selection.')
            return
        self._plots=tuple(plots)
        options=plots[0].display_options
        self._display_options = dict(options)
        import numpy as np
        archive=next(Path(p.path) for p in plots if Path(p.path).suffix=='.npz')
        with np.load(archive,allow_pickle=False) as data:
            bundle={key:data[key].copy() for key in data.files}
        self.cell_view.set_bundle(bundle,options)
        if 'review_caption' in options:
            self.status.setText(options['review_caption'])
        else:
            quantity='Nimbus score' if options['source']=='nimbus' else 'Intensity'
            self.status.setText(f"{options['marker_id']} · {quantity} cutoff: {options['cutoff']:.6g}. Near-cutoff, lowest and highest values; shared display limits across cells.")
        self.refresh.setEnabled(True)
        for b in (self.export,self.data):b.setEnabled(True)

    def _export(self,data_only):
        from .figure_export import _choose_path, _save_bytes, _DATA_FORMATS, _FORMATS
        from PySide6.QtWidgets import QMessageBox
        from mif_pipeline.cell_analysis.gallery_data import gallery_data_from_bundle
        from mif_pipeline.cell_analysis.gallery_plots import render_cell_gallery
        options={**self._display_options, 'include_pixels':True,
                 'show_cell_outlines':self.cell_view.cells.isChecked(),
                 'show_nuclear_outlines':self.cell_view.nuclei.isChecked()}
        basename=options.get('export_basename','marker-cell-examples')
        mode=self.cell_view.channel.currentData()
        if mode in ('he','fluorescence_he'):
            options['displayed_channel']=self.cell_view._alignment_fluorescence_choice(mode)
            from mif_pipeline.cell_analysis.aligned_cell_plots import aligned_cell_figure,aligned_cell_npz
            from .figure_export import export_figure
            alignment=self.cell_view._alignment_data
            if alignment is None:
                QMessageBox.information(self,'H&E is loading','Wait for the aligned H&E field to finish loading, then export it.')
                return
            args=(self.cell_view.bundle,self.cell_view.cell_index,alignment,options,mode,self.cell_view.alignment_opacity.value()/100)
            if not data_only:
                export_figure(self,aligned_cell_figure(*args),basename+'-aligned.svg')
            else:
                path=_choose_path(self,basename+'-aligned.npz',{'Aligned field pixels and outlines (*.npz)':'npz'})
                if path is not None:
                    try:_save_bytes(aligned_cell_npz(*args),path)
                    except (ValueError,OSError) as error:QMessageBox.warning(self,'Export not saved',str(error))
            return
        path=_choose_path(self,basename+('.csv' if data_only else '.svg'), _DATA_FORMATS if data_only else _FORMATS)
        if path is None:return
        try:
            data=gallery_data_from_bundle(self.cell_view.bundle,channels=options['channels'])
            outputs=render_cell_gallery(data,options,data_only=data_only)
            payload=next(payload for _identity,_title,fmt,payload in outputs if fmt==path.suffix.lstrip('.').lower())
            _save_bytes(payload,path)
        except (ValueError,OSError,StopIteration) as error:
            QMessageBox.warning(self,'Export not saved',str(error))
