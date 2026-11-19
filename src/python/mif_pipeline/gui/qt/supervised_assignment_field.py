"""One selected image's predicted cells in the shared continuous field viewer."""
from PySide6.QtCore import QTimer, Signal, QRectF, Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel
from .cell_image_field import CellImageFieldReview


class SupervisedAssignmentField(QWidget):
    requested = Signal()

    def __init__(self,parent=None):
        super().__init__(parent)
        self.revision = 0
        self._query = None
        self._accepted_query = None
        self._display, self._viewport = {}, {}
        self._focus = None
        self._channels = []
        layout = QVBoxLayout(self); layout.setContentsMargins(0,0,0,0)
        self.status = QLabel(self);self.status.setWordWrap(True);layout.addWidget(self.status)
        self.cell_view = CellImageFieldReview(self);layout.addWidget(self.cell_view)
        self.value = QLabel(self);self.value.setWordWrap(True);layout.addWidget(self.value);self.value.hide()
        self._timer = QTimer(self);self._timer.setSingleShot(True);self._timer.setInterval(200)
        self._timer.timeout.connect(self.requested)
        self.cell_view.field_requested.connect(self._pan)
        self.cell_view.field_cell_selected.connect(self._inspect)
        self.cell_view.appearance_requested.connect(lambda:self.cell_view.open_appearance(available_channels=self._channels))
        self.cell_view.display_changed.connect(self._display_changed)
        self.cell_view.load_requested.connect(self._load)
        self.hide()

    def invalidate(self):
        self.revision += 1
        self._timer.stop()
        self.cell_view.setEnabled(False)
        self.value.hide()

    def set_query(self,settings):
        query = {key:value for key,value in settings.items() if key not in {'revision','offset','page_size','plot_scope'}}
        if query == self._query:
            self.cell_view.setEnabled(self._accepted_query == query)
            return
        changed_image = self._query is None or self._query['image_identity'] != query['image_identity']
        self._query = query
        if changed_image:
            self.cell_view.clear();self._display,self._viewport = {},{}
        self._focus = None
        self.show();self._schedule()

    def _schedule(self):
        self.revision += 1
        self.status.setText('Loading predicted cells in this image…')
        self._timer.start()

    def _pan(self,options):
        # Showing the selected-cell details can resize the same physical field.
        # Keep its pending explicit selection; genuine navigation replaces it.
        if self._focus is not None and options.get('field_um') == self._viewport.get('field_um'):
            self._viewport = dict(options)
            return
        self._viewport = dict(options);self._focus=None;self._schedule()

    def _display_changed(self,options):
        self._display = dict(options)

    def _load(self,options):
        self._display = dict(options);self._schedule()

    def focus_cell(self,row):
        self._focus = dict(prediction_sha256=row['prediction_sha256'],cell_id=row['cell_id'])
        self._inspect(row);self._schedule()

    def options(self):
        if self._query is None:
            return None
        viewport = self.cell_view.view.viewport()
        return {**self._display,**self._viewport,**self._query,'prediction_cohort':True,
            'channel_display':self._display.get('channel_display','colour'),
            'merged_panel':self._display.get('merged_panel',True),
            'viewport_size':[max(1,min(1024,viewport.width())),max(1,min(1024,viewport.height()))],
            **({'focus_cell':self._focus} if self._focus else {})}

    def set_bundle(self,bundle):
        self._channels = bundle['metadata']['available_channels']
        self.cell_view.set_field_bundle(bundle)
        if self._focus is not None:
            left,top,right,bottom = bundle['metadata']['field_um']
            rect = QRectF(left,top,right-left,bottom-top)
            self.cell_view.view._fit = False
            self.cell_view.view.fitInView(rect,Qt.AspectRatioMode.KeepAspectRatio)
            self.cell_view.view.centerOn(rect.center())
            row = next((row for row in self.cell_view._by_id.values()
                if row['cell_id'] == self._focus['cell_id']
                and row['prediction_sha256'] == self._focus['prediction_sha256']),None)
            if row is not None:
                self.cell_view._selected_id = self.cell_view._cell_key(row)
                self.cell_view._highlight_selected();self._inspect(row)
            self.cell_view._field_timer.stop()
        self._display = dict(self.cell_view.options)
        self._viewport = self.cell_view.field_options()
        for item in self.cell_view.view.items_by_id.values():
            row = self.cell_view._by_id[item.data(0)]
            item.setToolTip(f"{row['class_id']} · score {row['confidence']:.6g}\n{row['cell_id']}")
        self.cell_view.setEnabled(True);self.status.clear();self._focus=None
        self._accepted_query = dict(self._query)

    def set_error(self,message):
        self.status.setText(message);self.cell_view.setEnabled(False)

    def _inspect(self,row):
        reference = row.get('reference_split','deployment')
        suffix = f' · {reference.capitalize()} reference cell' if reference != 'deployment' else ''
        self.value.setText(f"{row['class_id']} · score {row['confidence']:.6g} · {row['outcome']}{suffix}")
        self.value.setToolTip(' · '.join(str(row.get(key,'')) for key in
            ('sample_id','project_entry_id','slide_id','section_id','cell_id')))
        self.value.show()
