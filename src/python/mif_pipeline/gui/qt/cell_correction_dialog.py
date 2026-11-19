"""Review returned cell edits across images on calibrated tissue fields."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import Signal, Qt, QRectF, QTimer, QSignalBlocker, QEvent
from PySide6.QtGui import QColor, QPainterPath, QPen
from PySide6.QtWidgets import QDialog, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton, QSizePolicy, QGraphicsItem, QGraphicsPathItem, QCheckBox, QComboBox
from shiboken6 import isValid

from .cell_image_field import CellImageFieldReview
from .researcher_widgets import ContentSizedButton
from .cell_correction_queue import PendingCorrectionList, CorrectionMarkers, operation_label


def _style_correction_outline(item, *, proposed, selected):
    """Keep comparison strokes readable over both bright and dark channels."""
    pen=QPen(QColor('#60efd3' if proposed else '#ffc857'),2.2 if selected else 1.4)
    pen.setCosmetic(True);pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    if not proposed:pen.setStyle(Qt.PenStyle.DashLine)
    item.setPen(pen)
    halo=getattr(item,'_correction_halo',None)
    if halo is None:
        halo=QGraphicsPathItem(item.path(),item)
        halo.setFlag(QGraphicsItem.GraphicsItemFlag.ItemStacksBehindParent)
        halo.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        halo.setBrush(Qt.BrushStyle.NoBrush)
        item._correction_halo=halo
    halo_pen=QPen(pen);halo_pen.setColor(QColor('#142128'));halo_pen.setWidthF(pen.widthF()+2.4)
    if not proposed:
        # Qt measures dashes in pen widths; preserve the same device-pixel gaps.
        halo_pen.setDashPattern([length*pen.widthF()/halo_pen.widthF() for length in pen.dashPattern()])
    halo.setPen(halo_pen)


def correction_bundle(bundle, preview, *, proposed):
    """Replace reviewed targets visible in this field; retain its exact pixels."""
    from mif_pipeline.datasets.geometry import wkb_polygon
    rows = bundle['metadata']['visible_cells']
    changes = preview.get('changes', [preview])
    by_id = {item['cell_id']: item for item in changes}
    targets = {i: by_id[row['cell_id']] for i, row in enumerate(rows) if row['cell_id'] in by_id}
    removed = {index for index, change in targets.items() if proposed and change['after'] is None}
    visible = [row for index, row in enumerate(rows) if index not in removed]
    indices_by_old = {old: new for new, old in enumerate(i for i in range(len(rows)) if i not in removed)}
    vertices, offsets, indices, kinds = [], [0], [], []
    for ring, (index, kind) in enumerate(zip(bundle['outline_cell_indices'], bundle['outline_kinds'])):
        if int(index) in targets:
            continue
        start, stop = bundle['outline_offsets'][ring:ring+2]
        points = bundle['outline_vertices_um'][start:stop]
        vertices.extend(points); offsets.append(len(vertices)); indices.append(indices_by_old[int(index)]); kinds.append(str(kind))
    frame = bundle['metadata']['source_frame']
    pixel_size = np.array([frame['level0_pixel_size_x_um'], frame['level0_pixel_size_y_um']])
    created=[]
    if proposed:
        metadata=bundle['metadata']
        prefix=next(iter(rows),metadata)
        left,top,right,bottom=metadata['field_um']
        for change in changes:
            if change['before'] is not None or change['after'] is None:continue
            shape=wkb_polygon(bytes.fromhex(change['after']['cell_geometry_wkb_hex']))
            polygons=[shape['coordinates']] if shape['type']=='Polygon' else shape['coordinates']
            points=np.concatenate([np.asarray(polygon[0])*pixel_size for polygon in polygons])
            low,high=points.min(axis=0),points.max(axis=0)
            if high[0]<left or low[0]>right or high[1]<top or low[1]>bottom:continue
            center=change.get('center_um',(low+high)/2)
            visible.append({**{key:prefix[key] for key in ('sample_id','project_entry_id','slide_id','section_id')},
                'cell_id':change['cell_id'],'x_um':float(center[0]),'y_um':float(center[1])})
            created.append((len(visible)-1,change))
    output_targets=[(indices_by_old[target],change) for target,change in targets.items() if target not in removed]+created
    for target, change in output_targets:
        geometry = change['after' if proposed else 'before']
        if geometry is None:
            continue
        for name, kind in (('cell_geometry_wkb_hex', 'cell'), ('nucleus_geometry_wkb_hex', 'nucleus')):
            if geometry[name] is None or (kind == 'cell' and bundle['metadata'].get('cell_boundary_source') == 'nuclear_outline'):
                continue
            shape = wkb_polygon(bytes.fromhex(geometry[name]))
            polygons = [shape['coordinates']] if shape['type'] == 'Polygon' else shape['coordinates']
            for polygon in polygons:
                for ring in polygon:
                    vertices.extend(np.asarray(ring)*pixel_size)
                    offsets.append(len(vertices)); indices.append(target); kinds.append(kind)
    metadata = {**bundle['metadata'], 'visible_cells': visible}
    if metadata.get('field_cell_count') is not None:
        metadata['field_cell_count'] += len(created)-len(removed)
    return {**bundle, 'metadata': metadata, 'outline_vertices_um': np.asarray(vertices).reshape(-1,2),
            'outline_offsets': np.asarray(offsets, dtype=np.int64),
            'outline_cell_indices': np.asarray(indices, dtype=np.int64), 'outline_kinds': np.asarray(kinds)}


class CellCorrectionDialog(QDialog):
    apply_requested = Signal()
    edit_again_requested = Signal()
    field_requested = Signal(object)

    def __init__(self,bundle,candidate,parent=None,*,identities=(),workflow_nodes=(),images=()):
        super().__init__(parent)
        self.setWindowTitle('Review cell corrections');self.setMinimumWidth(540);self.resize(1080,900)
        self._images=[{key:value for key,value in item.items() if key!='bundle'} | dict(
            field_context=item.get('field_context') or item.get('bundle',{}).get('metadata')) for item in images] or [
            dict(candidate=candidate,field_context=bundle['metadata'],identities=identities,
                artifact_ref_sha256=candidate.get('ticket',{}).get('segmentation_result_ref_sha256'),
                name=bundle['metadata'].get('sample_id','Selected image'))]
        if bundle is None:bundle=dict(metadata=self._images[0]['field_context'] or {})
        self._image_index=0
        self.candidate=candidate;preview=candidate['preview'];self.changes=preview.get('changes',[preview])
        self._selected=0;self._bundle=bundle;self._contours=[];self._field_revision=0;self._loading=False
        self._field_rect=QRectF();self._wide=None
        self._thumbnail_options=None
        self._display_options={}
        self._syncing_camera=False;self._arranging=False
        self._requested_bounds=None;self._requested_viewport=None
        self._resize_timer=QTimer(self);self._resize_timer.setSingleShot(True);self._resize_timer.setInterval(180)
        self._resize_timer.timeout.connect(self._request_resized_field)
        body=QVBoxLayout(self);body.setContentsMargins(20,18,20,18);body.setSpacing(12)
        self.title=QLabel('Review corrections',self)
        self.title.setProperty('stagePageHeading',True);body.addWidget(self.title)
        image_navigation=QHBoxLayout();image_navigation.setSpacing(8)
        self.previous_image=ContentSizedButton('‹ Previous image',self)
        self.previous_image.clicked.connect(lambda:self.images.setCurrentIndex(self.images.currentIndex()-1))
        self.images=QComboBox(self);self.images.setAccessibleName('Image with proposed corrections')
        self.images.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Fixed)
        self.images.setMinimumContentsLength(12);self.images.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        for item in self._images:self.images.addItem(item['name'],item['artifact_ref_sha256'])
        self.next_image=ContentSizedButton('Next image ›',self)
        self.next_image.clicked.connect(lambda:self.images.setCurrentIndex(self.images.currentIndex()+1))
        image_navigation.addWidget(self.previous_image);image_navigation.addWidget(self.images,1);image_navigation.addWidget(self.next_image)
        self.image_position=QLabel(self);self.image_position.setProperty('stageFieldCaption',True)
        image_navigation.addWidget(self.image_position);body.addLayout(image_navigation)
        self.previous_image.setEnabled(False);self.next_image.setEnabled(len(self._images)>1)
        self.image_position.setText(f'1 of {len(self._images)}')
        for widget in (self.previous_image,self.next_image,self.image_position):widget.setVisible(len(self._images)>1)
        self.images.currentIndexChanged.connect(self._change_image)
        navigation=QHBoxLayout()
        deleted=sum(c['after'] is None for c in self.changes)
        created=sum(c['before'] is None for c in self.changes)
        edited=sum(c['before'] is not None and c['after'] is not None for c in self.changes)
        parts=[f"{n} {label}{'s' if n!=1 else ''}" for n,label in ((edited,'outline edit'),(created,'new cell'),(deleted,'removal')) if n]
        self.change_summary=QLabel(' · '.join(parts),self)
        self.change_summary.setProperty('fieldHelp',True);navigation.addWidget(self.change_summary,1)
        body.addLayout(navigation)
        self.workspace=QWidget(self);self.workspace_layout=QGridLayout(self.workspace)
        self.workspace_layout.setContentsMargins(0,0,0,0);self.workspace_layout.setSpacing(14);body.addWidget(self.workspace,1)
        self.pending=PendingCorrectionList(self.workspace)
        self.pending.list.setSelectionMode(self.pending.list.SelectionMode.SingleSelection)
        self.pending_caption=QLabel('Corrections',self.pending);self.pending_caption.setProperty('stageFieldCaption',True)
        self.pending.layout().insertWidget(0,self.pending_caption)
        self.views=QWidget(self.workspace);self.views_layout=QHBoxLayout(self.views);self.views_layout.setContentsMargins(0,0,0,0);self.views_layout.setSpacing(10)
        self.current_field=CellImageFieldReview(self.views)
        self.field=CellImageFieldReview(self.views)
        self.field.view.viewport().installEventFilter(self)
        self.current_field.view.viewport().installEventFilter(self)
        policy=self.field.view.sizePolicy();policy.setHeightForWidth(False);policy.setVerticalPolicy(QSizePolicy.Policy.Expanding)
        self.field.view.setSizePolicy(policy);self.field.view.setMinimumHeight(240);self.field.view.setMaximumHeight(680)
        self.field.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Expanding)
        for control in (self.field.cell,self.field.channel,self.field.appearance,*self.field.findChildren(QPushButton)):control.hide()
        self.field.cells.setChecked(True);self.field.nuclei.setChecked(True)
        self.field.cells.toggled.connect(self._contour_visibility);self.field.nuclei.toggled.connect(self._contour_visibility)
        self.field.field_cell_selected.connect(self._focus_spatial)
        self.field.field_requested.connect(self._viewport_requested)
        self.field.display_changed.connect(self._display_changed)
        self.field.load_requested.connect(self._load_channels)
        self.views_layout.addWidget(self.current_field,1);self.views_layout.addWidget(self.field,1)
        self.current_field.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Expanding)
        policy=self.current_field.view.sizePolicy();policy.setHeightForWidth(False);policy.setVerticalPolicy(QSizePolicy.Policy.Expanding)
        self.current_field.view.setSizePolicy(policy);self.current_field.view.setMinimumHeight(180);self.current_field.view.setMaximumHeight(680)
        for control in (self.current_field.cell,self.current_field.channel,self.current_field.appearance,*self.current_field.findChildren(QPushButton)):control.hide()
        self.current_field.field_cell_selected.connect(self._focus_spatial)
        self.current_field.field_requested.connect(self._viewport_requested)
        headers={}
        for field,label in ((self.current_field,'Current'),(self.field,'Proposed')):
            header=QWidget(field);row=QHBoxLayout(header);row.setContentsMargins(0,0,0,0);row.setSpacing(8)
            caption=QLabel(label,header);caption.setProperty('stageFieldCaption',True)
            row.addWidget(caption);row.addStretch(1);field.layout().insertWidget(0,header)
            headers[field]=header
            if field is self.current_field:self.current_caption=caption
            else:self.proposed_caption=caption
            field.view.viewport_changed.connect(lambda source=field:self._sync_camera(source))
            field.view.zoom_changed.connect(lambda _scale,source=field:self._sync_camera(source))
        outline_controls=QHBoxLayout()
        for checkbox in (self.field.cells,self.field.nuclei):outline_controls.addWidget(checkbox)
        outline_controls.addStretch()
        body.addLayout(outline_controls)
        self.compare_current=QCheckBox('Show current outlines',headers[self.field])
        self.compare_current.setAccessibleName('Show current outlines on Proposed')
        self.compare_current.toggled.connect(self._contour_visibility)
        headers[self.field].layout().addWidget(self.compare_current)
        header_height=max(header.sizeHint().height() for header in headers.values())
        for header in headers.values():header.setMinimumHeight(header_height)
        self.legend=QLabel('<span style="color:#8a5700">– – Current outlines</span> &nbsp;&nbsp; <span style="color:#006b70">━━ Proposed outlines</span>',self)
        self.legend.setProperty('fieldHelp',True);outline_controls.addWidget(self.legend)
        self.status=QLabel(self);self.status.setProperty('fieldHelp',True);self.status.setWordWrap(True)
        actions=QHBoxLayout()
        self.edit_again=ContentSizedButton('Edit again…',self);self.edit_again.clicked.connect(self.edit_again_requested)
        self.edit_again.setVisible(any(any(op!='delete' for op in change.get('operations',[change.get('operation')]))
            for item in self._images for change in item['candidate']['preview'].get('changes',[item['candidate']['preview']])))
        actions.addWidget(self.edit_again);actions.addWidget(self.status,1)
        cancel=ContentSizedButton('Cancel',self);cancel.clicked.connect(self.reject);actions.addWidget(cancel)
        self.apply=ContentSizedButton('Accept corrections',self);self.apply.setProperty('actionRole','primary')
        actions.addWidget(self.apply);body.addLayout(actions)
        impacts=[item['candidate'].get('ticket',{}).get('revision_preview') for item in self._images]
        impacts=[impact for impact in impacts if impact is not None]
        impact=dict(impacts[0],affected_node_ids=tuple(dict.fromkeys(node for item in impacts
            for node in item['affected_node_ids']))) if impacts else None
        if impact is not None:
            from .revision_summary import _stage_results
            affected = set(impact['affected_node_ids'])
            nodes = [node for node in workflow_nodes if node.node_id in affected]
            names = [title for _, title in _stage_results(nodes)]
            notice = QLabel(self)
            notice.setProperty('fieldHelp', True)
            notice.setWordWrap(True)
            notice.setText("Results to recalculate · " + ", ".join(names) if names else
                           "Accepting these corrections recalculates the results that depend on these outlines.")
            notice.setToolTip(", ".join(sorted({sample for node in nodes for sample in node.sample_ids})))
            body.insertWidget(body.count() - 1, notice)
            self.apply.setText('Accept corrections and recalculate')
        self.apply.clicked.connect(self.apply_requested)
        self._set_entries(bundle,identities)
        self.pending.list.setCurrentRow(0)
        self.pending.focused.connect(self._select)
        self._markers=CorrectionMarkers(self.field,self.pending.focus_key)
        self._arrange()
        if 'pixels' in bundle:self.set_field(bundle)
        else:self.set_loading(True)
        self._update_summary()

    def _update_summary(self):
        deleted=sum(c['after'] is None for c in self.changes)
        created=sum(c['before'] is None for c in self.changes)
        edited=len(self.changes)-deleted-created
        self.change_summary.setText(' · '.join(f"{n} {label}{'s' if n!=1 else ''}"
            for n,label in ((edited,'outline edit'),(created,'new cell'),(deleted,'removal')) if n))
        count=sum(len(item['candidate']['preview'].get('changes',[item['candidate']['preview']])) for item in self._images)
        self.title.setText(f'Review corrections · {len(self._images)} images' if len(self._images)>1
            else f"Review corrections · {count} cell"+('s' if count!=1 else ''))
        self.apply.setToolTip('Accept the corrections from every image in this review.' if len(self._images)>1 else '')

    def _set_entries(self,bundle,identities):
        keys={tuple(identity)[-1]:tuple(identity) for identity in identities}
        rows=bundle['metadata'].get('visible_cells',[])
        for row in rows:keys[row['cell_id']]=self.field._cell_key(row)
        prefix=next((key[:-1] for key in keys.values() if isinstance(key,tuple) and len(key)==5),None)
        if prefix is None and all(bundle['metadata'].get(key) for key in ('sample_id','project_entry_id','slide_id','section_id')):
            prefix=tuple(bundle['metadata'][key] for key in ('sample_id','project_entry_id','slide_id','section_id'))
        if prefix is None:raise ValueError('Open these corrections from their exact image and section.')
        entries=[]
        for change in self.changes:
            center=change.get('center_um')
            if center is None:
                row=next(r for r in rows if r['cell_id']==change['cell_id']);center=[row['x_um'],row['y_um']]
            entries.append(dict(identity=keys.get(change['cell_id'],prefix+(change['cell_id'],)),
                operations=change.get('operations',[change.get('operation')]),center_um=center))
        self.pending.set_entries(entries)

    @property
    def artifact_ref_sha256(self):return self._images[self._image_index]['artifact_ref_sha256']

    def _change_image(self,index):
        if index<0 or index==self._image_index:return
        current=self._images[self._image_index]
        current.update(selected=self._selected,display_options=dict(self._display_options),
            correction_filter=next(name for name,button in self.pending.filters.items() if button.isChecked()))
        self.field._field_timer.stop();self.current_field._field_timer.stop();self._resize_timer.stop()
        self._requested_bounds=None;self._requested_viewport=None
        self._field_revision+=1
        self._image_index=index;image=self._images[index]
        self.candidate=image['candidate'];preview=self.candidate['preview'];self.changes=preview.get('changes',[preview])
        self._selected=image.get('selected',0);self._thumbnail_options=None
        self._clear_contours();self._markers.clear()
        self.field.clear();self.current_field.clear();self._field_rect=QRectF()
        self._bundle=dict(metadata=image['field_context'])
        self._display_options=dict(image.get('display_options',image['field_context'].get('display_options',{})))
        self.pending.filters[image.get('correction_filter','all')].setChecked(True)
        with QSignalBlocker(self.pending.list):
            self._set_entries(self._bundle,image.get('identities',()))
            self.pending.list.setCurrentRow(self._selected)
        self.previous_image.setEnabled(index>0);self.next_image.setEnabled(index<len(self._images)-1)
        self.image_position.setText(f'{index+1} of {len(self._images)}')
        self._wide=None;self._arrange();self._update_summary();self.request_selected_field()

    @property
    def selected_cell_id(self):return self.changes[self._selected]['cell_id']
    @property
    def field_revision(self):return self._field_revision

    def _select(self,entry):
        self._selected=next(i for i,c in enumerate(self.changes) if c['cell_id']==entry['identity'][-1])
        self._markers.select(entry['identity']);self._style_contours();self.request_selected_field()

    def _focus_spatial(self,row):
        key=self.field._cell_key(row)
        if any(entry['identity']==key for entry in self.pending.entries):self.pending.focus_key(key)
        self._style_context()

    def _request(self,bounds):
        # Keep the unexpanded view as the resize anchor; alternating wide and
        # tall windows must not progressively request larger tissue fields.
        self._requested_bounds=list(map(float,bounds))
        viewport=self.field.view.viewport()
        width,height=max(1,viewport.width()),max(1,viewport.height())
        self._requested_viewport=(width,height);self._resize_timer.stop()
        left,top,right,bottom=self._requested_bounds
        center_x,center_y=(left+right)/2,(top+bottom)/2
        field_width,field_height=right-left,bottom-top
        aspect=width/height
        if field_width/field_height<aspect:field_width=field_height*aspect
        else:field_height=field_width/aspect
        bounds=[center_x-field_width/2,center_y-field_height/2,
                center_x+field_width/2,center_y+field_height/2]
        scale=min(1.,1024/max(width,height))
        self._field_revision+=1;self.set_loading(True)
        self.field_requested.emit(dict(artifact_ref_sha256=self.artifact_ref_sha256,cell_id=self.selected_cell_id,
            field_um=bounds,revision=self.field_revision,
            viewport_size=[max(1,round(width*scale)),max(1,round(height*scale))],
            display_options=dict(self._display_options)))

    def _request_resized_field(self):
        if self._requested_bounds is None or not self.isVisible():return
        viewport=self.field.view.viewport()
        size=(max(1,viewport.width()),max(1,viewport.height()))
        self._size_field()
        self.field._field_timer.stop();self.current_field._field_timer.stop()
        if size==self._requested_viewport:return
        self._request(self._requested_bounds)

    def request_selected_field(self):
        change=self.changes[self._selected];center=self.pending.entries[self._selected]['center_um']
        left,top,right,bottom=self._bundle['metadata']['field_um']
        width,height=min(100.,max(40.,right-left)),min(80.,max(30.,bottom-top))
        bounds=[center[0]-width/2,center[1]-height/2,center[0]+width/2,center[1]+height/2]
        from mif_pipeline.datasets.geometry import wkb_polygon
        frame=self._bundle['metadata']['source_frame'];scale=np.array([frame['level0_pixel_size_x_um'],frame['level0_pixel_size_y_um']])
        for geometry in (change['before'],change['after']):
            if geometry is None:continue
            for encoded in geometry.values():
                if encoded is None:continue
                shape=wkb_polygon(bytes.fromhex(encoded));polygons=[shape['coordinates']] if shape['type']=='Polygon' else shape['coordinates']
                for polygon in polygons:
                    points=np.asarray(polygon[0])*scale;low,high=points.min(axis=0)-5.,points.max(axis=0)+5.
                    bounds=[min(bounds[0],low[0]),min(bounds[1],low[1]),max(bounds[2],high[0]),max(bounds[3],high[1])]
        self._request(bounds)

    def _viewport_requested(self,request):
        if not self._resize_timer.isActive():self._request(request['field_um'])
    def _display_changed(self,options):
        self._display_options=dict(options)
        if self.current_field.bundle is not None:
            self.current_field.options=dict(options)
            loaded=set(map(int,self._bundle['channel_indices']))
            if set(options.get('channels',()))<=loaded:
                self.current_field._draw_image();self.current_field.view.channel_legend.hide()
    def _load_channels(self,options):
        self._display_changed(options);self._viewport_requested(self.field.field_options())

    def set_loading(self,loading):
        self._loading=loading;self.apply.setEnabled(not loading);self.edit_again.setEnabled(not loading)
        self.status.setText('Loading selected field…' if loading else '')

    def _clear_contours(self):
        for item,_id,_kind,_proposed in self._contours:
            if isValid(item) and item.scene() is not None:item.scene().removeItem(item)
        self._contours=[]

    def set_field(self,bundle):
        self._syncing_camera=True
        self._clear_contours();self._markers.clear();self._bundle=bundle
        self._proposed=correction_bundle(bundle,self.candidate['preview'],proposed=True)
        self.field.view._fit=False;self.field.set_field_bundle(self._proposed)
        self.current_field.view._fit=False;self.current_field.set_field_bundle(correction_bundle(bundle,self.candidate['preview'],proposed=False))
        self.current_field.view.channel_legend.hide();self.current_field.cells.hide();self.current_field.nuclei.hide()
        self._display_options=dict(self.field.options)
        left,top,right,bottom=bundle['metadata']['field_um'];self._field_rect=QRectF(left,top,right-left,bottom-top)
        from mif_pipeline.datasets.geometry import wkb_polygon
        frame=bundle['metadata']['source_frame'];scale=np.array([frame['level0_pixel_size_x_um'],frame['level0_pixel_size_y_um']])
        visible={row['cell_id'] for row in bundle['metadata']['visible_cells']} | {row['cell_id'] for row in self._proposed['metadata']['visible_cells']}
        for change in self.changes:
            if change['cell_id'] not in visible:continue
            operations=change.get('operations',[change.get('operation')])
            kinds=('cell','nucleus') if any(op in {'delete','split','merge','add_cell'} for op in operations) else tuple('cell' if op=='cell_outline' else 'nucleus' for op in operations)
            if bundle['metadata'].get('cell_boundary_source')=='nuclear_outline':kinds=tuple(k for k in kinds if k!='cell')
            for proposed,geometry in ((False,change['before']),(True,change['after'])):
                if geometry is None:continue
                for kind in kinds:
                    encoded=geometry[kind+'_geometry_wkb_hex']
                    if encoded is None:continue
                    shape=wkb_polygon(bytes.fromhex(encoded));polygons=[shape['coordinates']] if shape['type']=='Polygon' else shape['coordinates']
                    path=QPainterPath();path.setFillRule(Qt.FillRule.OddEvenFill)
                    for polygon in polygons:
                        for ring in polygon:
                            points=np.asarray(ring)*scale;path.moveTo(*map(float,points[0]))
                            for point in points[1:]:path.lineTo(*map(float,point))
                            path.closeSubpath()
                    # Optional current dashes remain visible where the two paths coincide.
                    item=QGraphicsPathItem(path);item.setAcceptedMouseButtons(Qt.MouseButton.NoButton);item.setZValue(12 if proposed else 13)
                    self.field.view.scene().addItem(item);self._contours.append((item,change['cell_id'],kind,proposed))
        self._style_context();self._style_contours();self._contour_visibility()
        self._markers.show(self.pending.entries,self._proposed);self._markers.select(self.pending.entries[self._selected]['identity'])
        self._update_thumbnails()
        current=bundle['metadata'].get('field_cell_count')
        objects='nuclei' if bundle['metadata'].get('cell_boundary_source')=='nuclear_outline' else 'cells'
        if current is not None:self.field.field_counts.setText(f'{current:,} current {objects}')
        self.field.field_counts.setToolTip('Current accepted outlines intersecting this field.')
        self.current_field.field_counts.hide()
        self._syncing_camera=False
        self._size_field();self.set_loading(False)

    def _style_context(self):
        changed={change['cell_id']:change.get('operations',[change.get('operation')]) for change in self.changes}
        for key,item in self.field.view.items_by_id.items():
            item.setToolTip('')
            cell_id=item.data(0)[-1];kind=key.split('-')[0]
            edited=cell_id in changed and (kind+'_outline' in changed[cell_id] or bool(set(changed[cell_id]) & {'delete','split','merge','add_cell'}) or kind=='nucleus' and 'add_nucleus' in changed[cell_id])
            enabled=self.field.cells.isChecked() if kind=='cell' else self.field.nuclei.isChecked()
            item.setOpacity(0. if edited or not enabled else .4);item.setBrush(Qt.BrushStyle.NoBrush)
        for key,item in self.current_field.view.items_by_id.items():
            item.setToolTip('')
            cell_id=item.data(0)[-1];kind=key.split('-')[0]
            enabled=self.field.cells.isChecked() if kind=='cell' else self.field.nuclei.isChecked()
            edited=cell_id in changed and (kind+'_outline' in changed[cell_id] or bool(set(changed[cell_id]) & {'delete','split','merge','add_cell'}) or kind=='nucleus' and 'add_nucleus' in changed[cell_id])
            item.setOpacity((1. if edited else .4) if enabled else 0.)
            item.setBrush(Qt.BrushStyle.NoBrush)
            if edited:
                _style_correction_outline(item,proposed=False,selected=cell_id==self.selected_cell_id)

    def _style_contours(self):
        for item,id,_kind,proposed in self._contours:
            _style_correction_outline(item,proposed=proposed,selected=id==self.selected_cell_id)
        self._style_context()

    def _contour_visibility(self):
        for item,_id,kind,proposed in self._contours:
            compartment=self.field.cells.isChecked() if kind=='cell' else self.field.nuclei.isChecked()
            item.setVisible(compartment and (proposed or self.compare_current.isChecked()))
        self.legend.setVisible(self.compare_current.isChecked())
        self._style_context()

    def _update_thumbnails(self):
        """Use the displayed field only; queue previews never trigger extra reads."""
        source=self.field.view.image_item
        if source is None:return
        if self._thumbnail_options!=self._display_options:
            for index in range(self.pending.list.count()):
                self.pending.list.item(index).setData(Qt.ItemDataRole.DecorationRole,None)
            from copy import deepcopy
            self._thumbnail_options=deepcopy(self._display_options)
        for index,entry in enumerate(self.pending.entries):
            center=entry['center_um'];bounds=QRectF(center[0]-12,center[1]-12,24,24)
            if not self._field_rect.contains(bounds):continue
            local=source.mapFromScene(bounds).boundingRect().toAlignedRect()
            crop=source.pixmap().copy(local)
            if crop.isNull():continue
            self.pending.set_thumbnail(index,crop)

    def _arrange(self):
        wide=self.width()>=940
        stacked=self.width()<740
        arrangement=(wide,stacked)
        if arrangement==self._wide:return
        self._wide=arrangement;layout=self.workspace_layout
        self._arranging=True
        self.pending_caption.setVisible(wide)
        layout.removeWidget(self.pending);layout.removeWidget(self.views)
        if wide:
            layout.setRowStretch(0,1);layout.setRowStretch(1,0)
            self.pending.setFixedWidth(245);self.pending.list.setMinimumHeight(200);self.pending.list.setMaximumHeight(16777215)
            layout.addWidget(self.pending,0,0);layout.addWidget(self.views,0,1);layout.setColumnStretch(0,0);layout.setColumnStretch(1,1)
        else:
            layout.setRowStretch(0,0)
            self.pending.setMinimumWidth(0);self.pending.setMaximumWidth(16777215)
            rows=min(2,max(1,self.pending.list.count()))
            height=rows*max(50,self.pending.list.sizeHintForRow(0))+10
            self.pending.list.setMinimumHeight(height);self.pending.list.setMaximumHeight(height)
            layout.addWidget(self.pending,0,0,1,2);layout.addWidget(self.views,1,0,1,2);layout.setRowStretch(1,1)
        self.views_layout.setDirection(QHBoxLayout.Direction.TopToBottom if stacked else QHBoxLayout.Direction.LeftToRight)
        self.field.view.setMinimumHeight(170 if stacked else 240)
        self.current_field.view.setMinimumHeight(170 if stacked else 240)
        QTimer.singleShot(0,self._finish_arrange)

    def _finish_arrange(self):
        self._arranging=False;self._size_field()
        if self._requested_bounds is not None:self._resize_timer.start()

    def _sync_camera(self,source):
        if self._syncing_camera or self._arranging or self._resize_timer.isActive():return
        self._syncing_camera=True
        try:
            target=self.current_field if source is self.field else self.field
            target.view.setTransform(source.view.transform())
            target.view.centerOn(source.view.mapToScene(source.view.viewport().rect().center()))
            target._field_timer.stop()
        finally:self._syncing_camera=False

    def _size_field(self):
        if self._field_rect.isEmpty():return
        self._syncing_camera=True
        try:
            for field in (self.field,self.current_field):
                field.view.setSceneRect(self._field_rect);field.view.fitInView(self._field_rect,Qt.AspectRatioMode.KeepAspectRatio)
                field.location.hide();field._field_timer.stop()
            self.current_field.view.setTransform(self.field.view.transform());self.current_field.view.centerOn(self._field_rect.center())
            self.current_field._field_timer.stop()
        finally:self._syncing_camera=False

    def eventFilter(self,watched,event):
        if (event.type()==QEvent.Type.Resize
                and watched in (self.field.view.viewport(),self.current_field.view.viewport())
                and self._requested_bounds is not None):
            self.field._field_timer.stop();self.current_field._field_timer.stop();self._resize_timer.start()
        return super().eventFilter(watched,event)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,'pending'):
            self._arrange();self._size_field()
            if self._requested_bounds is not None:self._resize_timer.start()
    def showEvent(self,event):
        super().showEvent(event);self._size_field()
