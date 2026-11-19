"""Display an authenticated paired H&E field at its returned physical spacing."""
import json
import numpy as np
from PySide6.QtCore import QEvent, Qt, QSignalBlocker
from PySide6.QtGui import QImage, QPixmap, QTransform
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel, QSlider, QGraphicsPixmapItem


class CellAlignmentView:
    """Rendering only; the controller authenticates and owns the native session."""
    def _init_alignment(self):
        self._alignment_available=False
        self._alignment_data=None
        self._alignment_item=None
        self._alignment_pending=None
        self._alignment_previous=None
        self._fluorescence_choice=None
        self._alignment_status=QLabel(self)
        self._alignment_status.setWordWrap(True);self._alignment_status.hide()
        self.layout().insertWidget(1,self._alignment_status)
        self._alignment_opacity=QWidget(self.view.viewport())
        self._alignment_opacity.setStyleSheet('background: rgba(255,255,255,225); color: #173d47;')
        row=QHBoxLayout(self._alignment_opacity);row.setContentsMargins(7,4,7,4)
        row.addWidget(QLabel('H&E',self._alignment_opacity))
        self.alignment_opacity=QSlider(Qt.Orientation.Horizontal,self._alignment_opacity)
        self.alignment_opacity.setRange(0,100);self.alignment_opacity.setValue(50)
        self.alignment_opacity.setAccessibleName('H&E overlay opacity')
        self.alignment_opacity.setFixedWidth(100);row.addWidget(self.alignment_opacity)
        self.alignment_opacity.valueChanged.connect(self._update_alignment_layers)
        self._alignment_opacity.adjustSize();self._alignment_opacity.hide()
        self.view.viewport().installEventFilter(self)

    def alignment_context(self):
        if self.bundle is None or 'centers_um' not in self.bundle:
            return None
        metadata=json.loads(str(self.bundle['metadata_json']))
        center=tuple(map(float,self.bundle['centers_um'][self.cell_index]))
        left,right,bottom,top=map(float,self.bundle['extents_um'][self.cell_index])
        return dict(sample_id=self.options.get('sample_id',self.options.get('image_label')),
            source_frame=dict(metadata['source_frame']),cell_id=self.selected_cell_id(),center_um=list(center),
            field_um=[center[0]+left,center[1]+top,center[0]+right,center[1]+bottom],
            viewport_size=[self.view.viewport().width(),self.view.viewport().height()])

    def alignment_field_identity(self):
        context=self.alignment_context()
        return None if context is None else (context['sample_id'],context['source_frame']['image_sha256'],
            context.get('cell_id'),*context['field_um'])

    def _alignment_scene_origin_um(self):
        """Cell crops draw relative to their actual saved cell centre."""
        return self.bundle['centers_um'][self.cell_index]

    def _alignment_context_updated(self):
        identity=self.alignment_field_identity()
        if identity!=self._alignment_previous:
            if self._alignment_previous is not None and identity is not None and identity[:2]!=self._alignment_previous[:2]:
                self.set_alignment_available(False)
            self._alignment_previous=identity
            self._alignment_data=None;self._alignment_pending=None
            if self._alignment_item is not None:self._alignment_item.hide()
        self.alignment_context_changed.emit(self.alignment_context())
        self._update_alignment_layers()

    def set_alignment_available(self,available):
        self._alignment_available=bool(available)
        if self.bundle is not None:
            self._set_channel_choices();self._draw_image()

    def _alignment_choices(self):
        if self._alignment_available:
            self.channel.addItem('H&E','he')
            self.channel.addItem('Fluorescence + H&E','fluorescence_he')

    def _alignment_fluorescence_choice(self,selected):
        if selected not in ('he','fluorescence_he'):
            self._fluorescence_choice=selected
            return selected
        if self._fluorescence_choice in self.options['channels'] or self._fluorescence_choice=='composite':
            return self._fluorescence_choice
        return 'composite' if len(self.options['channels'])>1 else self.options['channels'][0]

    def set_alignment_image(self,identity,rgb_chw,origin_um,pixel_size_um,metadata=None):
        if identity!=self.alignment_field_identity() or not self._alignment_available:
            return False
        values=np.asarray(rgb_chw)
        if values.ndim!=3 or values.shape[0]!=3 or max(values.shape[1:])>2048 or values.nbytes>48*1024**2:
            raise ValueError('The aligned H&E field must contain three bounded RGB channels.')
        spacing=tuple(map(float,pixel_size_um));origin=tuple(map(float,origin_um))
        if len(spacing)!=2 or min(spacing)<=0 or not all(np.isfinite((*spacing,*origin))):
            raise ValueError('The aligned H&E field needs its exact physical origin and spacing.')
        valid=np.all(np.isfinite(values),axis=0)
        rgba=np.zeros((*values.shape[1:],4),dtype=np.uint8)
        rgba[...,:3]=np.rint(np.clip(np.nan_to_num(values).transpose(1,2,0),0,255)).astype(np.uint8)
        rgba[...,3]=valid.astype(np.uint8)*255
        image=QImage(rgba.data,rgba.shape[1],rgba.shape[0],rgba.strides[0],QImage.Format.Format_RGBA8888).copy()
        if self._alignment_item is None:
            self._alignment_item=QGraphicsPixmapItem();self.view.scene().addItem(self._alignment_item)
            self._alignment_item.setZValue(1);self._alignment_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self._alignment_item.setPixmap(QPixmap.fromImage(image))
        scene_origin=self._alignment_scene_origin_um()
        self._alignment_item.setPos(origin[0]-scene_origin[0],origin[1]-scene_origin[1])
        self._alignment_item.setTransform(QTransform.fromScale(*spacing))
        self._alignment_data=dict(pixels=values,origin_um=origin,pixel_size_um=spacing,metadata=dict(metadata or {}))
        self._alignment_status.hide()
        self._alignment_pending=None;self._update_alignment_layers()
        return True

    def _update_alignment_layers(self,*_):
        if not hasattr(self,'_alignment_opacity'):
            return
        mode=self.channel.currentData()
        active=self._alignment_available and mode in ('he','fluorescence_he')
        ready=active and self._alignment_data is not None
        if not active or ready:self._alignment_status.hide()
        if self.view.image_item is not None:self.view.image_item.setOpacity(0. if ready and mode=='he' else 1.)
        if self._alignment_item is not None:
            self._alignment_item.setVisible(ready)
            self._alignment_item.setOpacity(self.alignment_opacity.value()/100 if mode=='fluorescence_he' else 1.)
        self._alignment_opacity.setVisible(ready and mode=='fluorescence_he')
        self._position_alignment_opacity()
        if active and not ready:
            identity=self.alignment_field_identity()
            if identity is not None and identity!=self._alignment_pending:
                self._alignment_pending=identity
                self._alignment_status.setText('Loading aligned H&E…');self._alignment_status.show()
                self.alignment_requested.emit({**self.alignment_context(),'mode':mode})

    def set_alignment_failure(self,message):
        self._alignment_pending=None
        with QSignalBlocker(self.channel):
            index=self.channel.findData(self._alignment_fluorescence_choice(self.channel.currentData()))
            self.channel.setCurrentIndex(max(0,index))
        self._draw_image()
        self._alignment_status.setText(str(message));self._alignment_status.show()

    def _position_alignment_opacity(self):
        self._alignment_opacity.move(max(0,self.view.viewport().width()-self._alignment_opacity.width()-10),
            max(0,self.view.viewport().height()-self._alignment_opacity.height()-10))

    def _clear_alignment(self):
        self._alignment_data=None;self._alignment_item=None;self._alignment_pending=None;self._alignment_previous=None
        self._alignment_opacity.hide();self.alignment_context_changed.emit(None)
        self._alignment_status.hide()

    def hideEvent(self,event):
        self.alignment_context_changed.emit(None)
        super().hideEvent(event)

    def eventFilter(self,watched,event):
        if watched is self.view.viewport() and event.type()==QEvent.Type.Resize and hasattr(self,'_alignment_opacity'):
            self._position_alignment_opacity()
        return super().eventFilter(watched,event)
