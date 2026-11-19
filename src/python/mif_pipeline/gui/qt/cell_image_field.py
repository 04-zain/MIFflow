"""Continuous tissue inspection using the shared cell image view and appearance editor."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QEvent, QPointF, QRectF, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainterPath, QPen, QPixmap, QTransform
from PySide6.QtWidgets import QGraphicsItem, QGraphicsPixmapItem, QLabel, QPushButton, QToolTip

from .cell_image_review import CellImageReview
from .region_review import _OutlineView, _RegionOutlineItem, _RegionOverview


class _FieldLocation(_RegionOverview):
    """The tissue review locator keeps the same image and moves the current field."""
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            inverse, valid = self._transform.inverted()
            if valid:
                self.review.view.centerOn(inverse.map(event.position()))
                self.review._schedule_field()
                event.accept()
                return
        super().mousePressEvent(event)


class _FieldView(_OutlineView):
    """Report the image aspect height before a scroll area chooses its width."""

    def __init__(self, parent):
        super().__init__(parent)
        policy = self.sizePolicy()
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def heightForWidth(self, width):
        field = self.parentWidget()
        return (380 if field._full_rect.isEmpty() else
                max(380, min(760, round(width*field._full_rect.height()/field._full_rect.width()))))


class CellImageFieldReview(CellImageReview):
    """Request bounded pixels and saved contours as the researcher pans and zooms.

    The image and contours use absolute canonical micrometres. The whole-image
    grid holds exact selected and measured counts; it never substitutes sampled
    cell identities for a complete spatial count. The parent owns asynchronous
    reads and rejects responses from an earlier interval or viewport.
    """
    field_requested = Signal(object)
    field_cell_selected = Signal(object)
    _view_class = _FieldView

    def __init__(self, parent=None):
        self._updating = True
        self._full_rect = QRectF()
        self._raw_bundle = None
        self._metadata = {}
        self._by_id = {}
        self._selected_id = None
        self._density_item = None
        self._density_selected = None
        self._density_measured = None
        self._density_factor = 1
        self._press_position = None
        self._dense_index = -1
        super().__init__(parent)
        # Loading a field must not resize it and schedule another field read.
        self.layout().removeWidget(self._alignment_status)
        self._alignment_status.setParent(self.view.viewport())
        self._alignment_status.setStyleSheet('QLabel { background: rgba(255,255,255,225); color: #173d47; padding: 5px 7px; }')
        self._alignment_status.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.cell.hide()
        self.view.setMinimumHeight(380)
        self.view.setMaximumHeight(760)
        self.view.setMouseTracking(True)
        self.view.viewport().installEventFilter(self)
        self.view.setAccessibleName('Measured cells in tissue')
        self.view.setAccessibleDescription('Click the spatial density to zoom in. Drag to pan; Control and scroll zooms. Select a cell outline to inspect its saved measurement.')
        self.view.inspected.connect(self._inspect_cell)
        self.view.zoom_changed.connect(self._schedule_field)
        self.view.viewport_changed.connect(self._schedule_field)
        self._field_timer = QTimer(self)
        self._field_timer.setSingleShot(True)
        self._field_timer.setInterval(250)
        self._field_timer.timeout.connect(self._emit_field)
        self.location = _FieldLocation(self, self.view.viewport())
        self.location.setMinimumSize(0, 0)
        self.location.setMaximumSize(160, 110)
        self.location.resize(160, 110)
        self.location.hide()
        self.field_counts=QLabel(self.view.viewport())
        self._inspection_note = ''
        self._inspection_note_tooltip = ''
        self.field_counts.setStyleSheet('QLabel { background: rgba(255,255,255,225); color: #173d47; padding: 5px 7px; }')
        self.field_counts.setWordWrap(True)
        self.field_counts.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.field_counts.setAccessibleName('Selected cells in the visible field')
        self.field_counts.hide()
        self.view.channel_legend.geometry_changed.connect(self._position_location)
        self.dense = QPushButton('Inspect dense area', self)
        self.dense.clicked.connect(self.next_dense_area)
        controls = self.layout().itemAt(self.layout().count()-1).layout()
        for button in self.findChildren(QPushButton):
            if button.text() == 'Fit view':
                button.setText('Fit tissue')
                controls.insertWidget(controls.indexOf(button), self.dense)
        self._updating = False

    def selected_cell_id(self):
        row = self._by_id.get(self._selected_id)
        return row['cell_id'] if row is not None else self._selected_id

    def _cell_key(self, row):
        keys = ('sample_id', 'project_entry_id', 'slide_id', 'section_id', 'cell_id')
        return tuple(row[key] for key in keys) if all(key in row for key in keys) else row['cell_id']

    def selected_cell_identity(self):
        row = self._by_id.get(self._selected_id)
        keys = ('sample_id','project_entry_id','slide_id','section_id','cell_id')
        return tuple(row[key] for key in keys) if row is not None and all(key in row for key in keys) else None

    def alignment_context(self):
        """An authenticated tissue field needs no selected or invented cell."""
        frame = self._metadata.get('source_frame')
        image = self._metadata.get('region_image_identity')
        if self._raw_bundle is None or frame is None or not image or image[0] is None:
            return None
        return dict(sample_id=image[0], source_frame=dict(frame),
                    field_um=list(map(float, self._metadata['field_um'])),
                    viewport_size=[self.view.viewport().width(), self.view.viewport().height()])

    def _alignment_scene_origin_um(self):
        """Tissue pixels and outlines use absolute canonical micrometres."""
        return (0., 0.)

    def clear(self):
        self._field_timer.stop()
        self._raw_bundle = None
        self._by_id = {}
        self._full_rect = QRectF()
        self._selected_id = None
        self._density_item = None
        self._density_selected = None
        self._density_measured = None
        self._density_factor = 1
        self._dense_index = -1
        self.dense.setText('Inspect dense area')
        self.view.overview_image_item = None
        self.view.overview_density_item = None
        self.location.hide()
        self.field_counts.hide()
        super().clear()

    def set_field_bundle(self, bundle):
        """Display one authenticated read-only field without altering its pixel values."""
        first = self._raw_bundle is None
        self._updating = True
        self._field_timer.stop()
        centre = self.view.mapToScene(self.view.viewport().rect()).boundingRect().center()
        try:
            self._raw_bundle = bundle
            self._metadata = dict(bundle['metadata'])
            boundary = self._metadata.get('cell_boundary_source')
            self.cells.setText({'estimated_nuclear_expansion': 'Estimated cell regions', 'mixed': 'Cell regions'}.get(boundary, 'Cell outline'))
            self.cells.setVisible(boundary != 'nuclear_outline')
            self.nuclei.setVisible(boundary != 'learned_cell')
            if boundary != getattr(self, '_outline_boundary_source', None):
                self.cells.setChecked(boundary != 'nuclear_outline')
                self.nuclei.setChecked(boundary != 'learned_cell')
                self._outline_boundary_source = boundary
            if self._metadata.get('review_kind') == 'segmentation' and self._review_channels is None:
                self._review_channels=list(self._metadata['review_channels'])
            self.options = dict(self._metadata.get('display_options', self._metadata.get('options', {})))
            self.options.setdefault('channels', list(map(int,bundle['channel_indices'])))
            self._by_id = {self._cell_key(row): row for row in self._metadata.get('visible_cells', ())}
            extent = tuple(map(float,bundle['grid_extent_um']))
            self._full_rect = QRectF(extent[0],extent[3],extent[1]-extent[0],extent[2]-extent[3])
            self._update_field_height()
            # The inherited renderer consumes one field; no synthetic cell ID is created.
            self.bundle = dict(pixels=np.asarray(bundle['pixels'])[None],
                channel_indices=bundle['channel_indices'],channel_names=bundle['channel_names'],
                extents_um=np.asarray(bundle['extent_um'])[None],cell_ids=np.asarray([],dtype='U1'))
            self.cell_index = 0
            self._set_channel_choices()
            self._draw_image()
            for item in self.view.items_by_id.values():
                self.view.scene().removeItem(item)
            self.view.items_by_id.clear()
            rows = self._metadata.get('visible_cells', ())
            for ring, cell_index in enumerate(bundle['outline_cell_indices']):
                row = rows[int(cell_index)]
                start,stop = bundle['outline_offsets'][ring:ring+2]
                vertices = bundle['outline_vertices_um'][start:stop]
                if not len(vertices):
                    continue
                kind = str(bundle['outline_kinds'][ring])
                path = QPainterPath();path.moveTo(*map(float,vertices[0]))
                for point in vertices[1:]:
                    path.lineTo(*map(float,point))
                path.closeSubpath()
                if self._metadata.get('review_kind') == 'segmentation':
                    left,right,bottom,top=map(float,bundle['extent_um'])
                    clip=QPainterPath();clip.addRect(QRectF(left,top,right-left,bottom-top));path=path.intersected(clip)
                item = _RegionOutlineItem(path)
                plain = self._metadata.get('review_kind') == 'segmentation'
                selected = row.get('selected', plain)
                color = QColor('#ffd48a' if kind=='nucleus' else '#60d9c1') if plain else QColor('#ffd48a' if kind=='nucleus' else '#ffb547') if selected else QColor('#b9c2c5')
                color.setAlpha(190 if selected and kind=='nucleus' else 255 if selected else 85)
                pen = QPen(color,1.2);pen.setCosmetic(True);item.setPen(pen)
                item.setData(0,self._cell_key(row));item.setData(1,color)
                item.setData(2,QColor(0,0,0,0));item.setData(3,False)
                item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
                item.setToolTip(row['cell_id'] if plain else f"Saved value: {row['value']!r}\n{row['cell_id']}")
                item.setZValue(5 if kind=='cell' else 6)
                self.view.scene().addItem(item)
                self.view.items_by_id[f'{kind}-{ring}'] = item
            self._show_outlines()
            field_extent=tuple(map(float,bundle['extent_um']))
            field_rect=QRectF(field_extent[0],field_extent[3],field_extent[1]-field_extent[0],field_extent[2]-field_extent[3])
            if self._metadata.get('review_kind') != 'segmentation' and (first or (field_rect.width()>=self._full_rect.width()*.99 and field_rect.height()>=self._full_rect.height()*.99)):
                source=self.view.image_item
                overview=QGraphicsPixmapItem(source.pixmap())
                overview.setPos(source.pos());overview.setTransform(source.transform())
                self.view.overview_image_item=overview
            if first:
                if self._metadata.get('review_kind') == 'segmentation':
                    self.view._fit=False
                    self.view.fitInView(field_rect,Qt.AspectRatioMode.KeepAspectRatio)
                    self.view.centerOn(field_rect.center())
                else:
                    self.view.reset_view()
            else:
                self.view.centerOn(centre)
            self._draw_density()
            self._highlight_selected()
            plain = self._metadata.get('review_kind') == 'segmentation'
            self._refresh_field_counts()
            self._position_location()
            self.dense.setVisible(not plain)
            self.dense.setEnabled(not plain and bool(np.any(bundle.get('grid_selected', ()))))
        finally:
            self._updating=False
        self._alignment_context_updated()
        self._appearance_ready()

    def set_inspection_note(self, text='', tooltip=''):
        """Show the active inspection's population in the field-count badge."""
        self._inspection_note = text
        self._inspection_note_tooltip = tooltip
        if self._metadata:
            self._refresh_field_counts()
            self._position_location()

    def _refresh_field_counts(self):
        metadata = self._metadata
        if metadata.get('review_kind') == 'segmentation':
            count = metadata.get('field_cell_count')
            objects = 'nuclei' if metadata.get('cell_boundary_source') == 'nuclear_outline' else 'cells'
            text = f'{count:,} {objects}' if count is not None else 'Zoom in for outlines'
            tip = 'Detected outlines intersecting the current field.'
            if self._inspection_note:
                text = self._inspection_note
                tip = self._inspection_note_tooltip
        else:
            n, total = metadata.get('field_selected', 0), metadata.get('field_measured', 0)
            population = metadata.get('population_label', 'measured cells')
            text = f'{n:,} of {total:,} {population} match · {100*n/total:.3g}%' if total else f'0 {population}'
            tip = 'Counts use cell centres inside the current field and the current review population.'
        self.field_counts.setText(text)
        self.field_counts.setToolTip(tip)

    def _draw_image(self):
        updating=self._updating;self._updating=True
        try:
            centre=self.view.mapToScene(self.view.viewport().rect()).boundingRect().center()
            if self._metadata.get('review_kind') == 'segmentation' and 'overview_pixels' in (self._raw_bundle or {}):
                detail = self.bundle
                try:
                    self.bundle = {**detail, 'pixels':self._raw_bundle['overview_pixels'][None], 'extents_um':np.asarray(self._raw_bundle['grid_extent_um'])[None]}
                    super()._draw_image()
                    source = self.view.image_item
                    overview = QGraphicsPixmapItem(source.pixmap())
                    overview.setPos(source.pos());overview.setTransform(source.transform())
                    self.view.overview_image_item = overview
                finally:
                    self.bundle = detail
            super()._draw_image()
            if not self._full_rect.isEmpty():
                self.view.setSceneRect(self._full_rect)
                if self._raw_bundle is not None:
                    self.view.centerOn(centre)
        finally:
            self._updating=updating

    def _draw_density(self):
        """Smooth the overview while keeping exact tile counts for inspection."""
        from mif_pipeline.cell_analysis.measurement_spatial_plots import aggregate_count_grid, density_rgba
        if self._metadata.get('review_kind') == 'segmentation':
            return
        selected=np.asarray(self._raw_bundle['grid_selected'])
        screen=self.view.mapFromScene(self._full_rect).boundingRect()
        cell_pixels=min(screen.width()/selected.shape[1],screen.height()/selected.shape[0])
        factor=1
        while cell_pixels*factor<10 and all(size%(factor*2)==0 for size in selected.shape):
            factor*=2
        self._density_factor=factor
        self._density_selected=selected=aggregate_count_grid(selected,factor)
        self._density_measured=aggregate_count_grid(self._raw_bundle['grid_measured'],factor)
        rgba=density_rgba(self._raw_bundle['grid_selected'])
        image=QImage(rgba.data,rgba.shape[1],rgba.shape[0],rgba.strides[0],QImage.Format.Format_RGBA8888).copy()
        if self._density_item is None:
            self._density_item=self.view.scene().addPixmap(QPixmap.fromImage(image))
            self._density_item.setZValue(2)
            self._density_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        else:
            self._density_item.setPixmap(QPixmap.fromImage(image))
        self._density_item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self._density_item.setPos(self._full_rect.topLeft())
        self._density_item.setTransform(QTransform.fromScale(self._full_rect.width()/image.width(),self._full_rect.height()/image.height()))
        self._density_item.setVisible(not self._metadata.get('outline_detail_available',False))
        overview=QGraphicsPixmapItem(self._density_item.pixmap())
        overview.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        overview.setPos(self._density_item.pos());overview.setTransform(self._density_item.transform())
        self.view.overview_density_item=overview

    def _inspect_cell(self, identities):
        if self._updating or not identities:
            return
        identity=identities[0]
        if identity in self._by_id:
            self._selected_id=identity
            self._highlight_selected()
            self.field_cell_selected.emit(dict(self._by_id[identity]))

    def _highlight_selected(self):
        self.view._syncing=True
        try:
            with QSignalBlocker(self.view.scene()):
                for item in self.view.items_by_id.values():
                    item.setSelected(item.data(0)==self._selected_id)
            self.view._selection_changed()
            for item in self.view.items_by_id.values():
                if item.isSelected():
                    pen=QPen(QColor('#ff6b6b'),2.2);pen.setCosmetic(True);item.setPen(pen)
                    item.setBrush(QColor(255,107,107,22))
        finally:
            self.view._syncing=False

    def _schedule_field(self,*_):
        if self._updating or self._raw_bundle is None:
            return
        self._position_location()
        self._draw_density()
        self._field_timer.start()

    def field_options(self):
        bounds=self.view.mapToScene(self.view.viewport().rect()).boundingRect().intersected(self._full_rect)
        return dict(field_um=[bounds.left(),bounds.top(),bounds.right(),bounds.bottom()],
            viewport_size=[min(1024,self.view.viewport().width()),min(1024,self.view.viewport().height())])

    def _emit_field(self):
        if self._raw_bundle is not None:
            self.field_requested.emit(self.field_options())

    def _position_location(self):
        if self._full_rect.isEmpty():
            self.location.hide();self.field_counts.hide()
            return
        viewport=self.view.viewport();legend=self.view.channel_legend
        # Reserve the lower-left scale bar and the existing H&E opacity control.
        self.field_counts.setWordWrap(False)
        natural=self.field_counts.sizeHint()
        self.field_counts.setWordWrap(True)
        width=min(natural.width(),max(100,viewport.width()-130))
        self.field_counts.resize(width,max(natural.height(),self.field_counts.heightForWidth(width)))
        opacity=getattr(self,'_alignment_opacity',None)
        bottom=opacity.geometry().top()-6 if opacity is not None and opacity.isVisible() else viewport.height()-10
        self.field_counts.move(max(10,viewport.width()-self.field_counts.width()-10),max(10,bottom-self.field_counts.height()))
        self.field_counts.setVisible(self._raw_bundle is not None)
        self.field_counts.raise_()
        right=max(10,viewport.width()-160-10)
        narrow=legend.isVisible() and right<legend.geometry().right()+10
        top=legend.geometry().bottom()+8 if narrow else 10
        available=max(0,self.field_counts.geometry().top()-top-8)
        scale=min(1.,available/110.,max(0,viewport.width()-20)/160.)
        self.location.resize(round(160*scale),round(110*scale))
        self.location.move(max(10,viewport.width()-self.location.width()-10),top)
        bounds=self.view.mapToScene(self.view.viewport().rect()).boundingRect()
        zoomed=bounds.width()<self._full_rect.width()*.9 or bounds.height()<self._full_rect.height()*.9
        self.location.setVisible(zoomed and scale>0)
        self.location.raise_()
        self.location.update()
        self._position_alignment_status()

    def _position_alignment_opacity(self):
        super()._position_alignment_opacity()
        if hasattr(self,'field_counts'):
            self._position_location()

    def _position_alignment_status(self):
        label, legend = self._alignment_status, self.view.channel_legend
        width = min(340, max(80, self.view.viewport().width()-20))
        label.resize(width, max(label.sizeHint().height(), label.heightForWidth(width)))
        label.move(10, legend.geometry().bottom()+8 if legend.isVisible() else 10)
        label.raise_()

    def _update_alignment_layers(self):
        super()._update_alignment_layers()
        self._position_alignment_status()

    def set_alignment_failure(self, message):
        super().set_alignment_failure(message)
        self._position_alignment_status()

    def _update_field_height(self):
        self.view.updateGeometry()
        self.layout().invalidate()
        self.updateGeometry()

    def _show_outlines(self):
        super()._show_outlines()
        for item in self.view.items_by_id.values():
            item.setAcceptedMouseButtons(Qt.MouseButton.LeftButton if item.opacity() else Qt.MouseButton.NoButton)

    def next_dense_area(self):
        """Visit fields containing the most matching cells, with stable tie ordering."""
        if self._raw_bundle is None:
            return
        counts=np.asarray(self._raw_bundle['grid_selected'])
        positions=np.argwhere(counts>0)
        if not len(positions):
            return
        from mif_pipeline.cell_analysis.measurement_spatial import neighborhood_counts
        height=min(self._full_rect.height(),max(100.,self._full_rect.height()/counts.shape[0]*16))
        width=height*self.view.viewport().width()/max(1,self.view.viewport().height())
        radius_y=max(1,min(counts.shape[0],round(height/self._full_rect.height()*counts.shape[0]/2)))
        radius_x=max(1,min(counts.shape[1],round(width/self._full_rect.width()*counts.shape[1]/2)))
        totals=neighborhood_counts(counts,radius_y,radius_x)
        order=sorted(positions.tolist(),key=lambda ij:(-int(totals[tuple(ij)]),ij[0],ij[1]))
        candidates=[]
        for y,x in order:
            if all(((x-cx)/radius_x)**2+((y-cy)/radius_y)**2>=1 for cy,cx in candidates):
                candidates.append((y,x))
            if len(candidates)>=32:
                break
        self._dense_index=(self._dense_index+1)%len(candidates)
        y,x=candidates[self._dense_index]
        cx=self._full_rect.left()+(x+.5)*self._full_rect.width()/counts.shape[1]
        cy=self._full_rect.top()+(y+.5)*self._full_rect.height()/counts.shape[0]
        self.view._fit=False
        self.view.fitInView(QRectF(cx-width/2,cy-height/2,width,height),Qt.AspectRatioMode.KeepAspectRatio)
        self.view.centerOn(cx,cy)
        self.dense.setText('Next dense area')
        self._schedule_field()

    def eventFilter(self, watched,event):
        if watched is self.view.viewport() and self._raw_bundle is not None:
            if event.type()==QEvent.Type.Resize:
                self._update_field_height();self._position_location();self._schedule_field()
            elif event.type()==QEvent.Type.MouseButtonPress and event.button()==Qt.MouseButton.LeftButton:
                self._press_position=event.position()
            elif event.type()==QEvent.Type.MouseButtonRelease and event.button()==Qt.MouseButton.LeftButton:
                if self._press_position is not None and (event.position()-self._press_position).manhattanLength()<4 and not self._metadata.get('outline_detail_available',False):
                    point=self.view.mapToScene(event.position().toPoint())
                    self.view.centerOn(point);self.view.zoom_by(2.5);self._schedule_field()
                self._press_position=None
            elif event.type()==QEvent.Type.MouseMove and not self._metadata.get('outline_detail_available',False) and self._metadata.get('review_kind') != 'segmentation':
                point=self.view.mapToScene(event.position().toPoint())
                if self._full_rect.contains(point):
                    selected=self._density_selected;measured=self._density_measured
                    x=min(selected.shape[1]-1,int((point.x()-self._full_rect.left())/self._full_rect.width()*selected.shape[1]))
                    y=min(selected.shape[0]-1,int((point.y()-self._full_rect.top())/self._full_rect.height()*selected.shape[0]))
                    population=self._metadata.get('population_label','measured cells')
                    n,total=int(selected[y,x]),int(measured[y,x]);fraction=f'{100*n/total:.6g}%' if total else f'no {population}'
                    QToolTip.showText(event.globalPosition().toPoint(),f'X {point.x():.6g} µm · Y {point.y():.6g} µm\n{n:,} selected / {total:,} {population} ({fraction})',self.view)
        return super().eventFilter(watched,event)
