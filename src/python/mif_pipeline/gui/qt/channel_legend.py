"""Compact channel controls shared by bounded fluorescence image views."""
from copy import deepcopy

from PySide6.QtCore import Qt, QEvent, Signal, QSignalBlocker
from PySide6.QtGui import QColor, QPixmap, QIcon
from PySide6.QtWidgets import QFrame, QVBoxLayout, QScrollArea, QWidget, QCheckBox, QToolButton, QHBoxLayout, QLabel, QSizePolicy, QGraphicsOpacityEffect


class ChannelLegend(QFrame):
    changed = Signal(object)
    geometry_changed = Signal()

    def __init__(self, viewport):
        super().__init__(viewport)
        self.setObjectName('image-channel-legend')
        self.setAccessibleName('Fluorescence channels')
        self.setStyleSheet('''QFrame#image-channel-legend {background:rgba(19,31,38,225);border:1px solid #60747b;border-radius:4px;}
            QScrollArea,QScrollArea>QWidget>QWidget {background:transparent;border:0;}
            QCheckBox {background:transparent;color:#a0afb4;spacing:5px;padding:0;}
            QCheckBox:checked {color:#f4f7f7;} QCheckBox:focus {outline:1px solid #68d5df;}
            QToolButton {background:transparent;color:#eff5f5;border:0;padding:1px;font-weight:600;}
            QToolButton:hover {background:rgba(255,255,255,35);}
            QToolButton:focus {border:1px solid #68d5df;}
            QToolButton:disabled {color:#65777c;}''')
        outer=QHBoxLayout(self);outer.setContentsMargins(7,5,4,5);outer.setSpacing(3)
        layout=QVBoxLayout();layout.setContentsMargins(0,0,0,0);layout.setSpacing(3);outer.addLayout(layout,1)
        heading=QHBoxLayout();heading.setContentsMargins(0,0,0,0);heading.setSpacing(4)
        self.title=QLabel('Channels',self);self.title.setStyleSheet('color:#eff5f5;background:transparent;')
        heading.addWidget(self.title);heading.addStretch()
        self.previous=QToolButton(self);self.previous.setArrowType(Qt.ArrowType.LeftArrow);self.previous.setAccessibleName('Previous channel labels')
        self.page_label=QLabel(self);self.page_label.setStyleSheet('color:#c3d0d4;background:transparent;')
        self.next=QToolButton(self);self.next.setArrowType(Qt.ArrowType.RightArrow);self.next.setAccessibleName('Next channel labels')
        layout.addLayout(heading)
        self._page=0
        self.previous.clicked.connect(lambda:self._turn_page(-1));self.next.clicked.connect(lambda:self._turn_page(1))
        self.scroll=QScrollArea(self);self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body=QWidget(self.scroll);self.rows=QVBoxLayout(self.body)
        self.rows.setContentsMargins(0,0,0,0);self.rows.setSpacing(2);self.rows.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll.setWidget(self.body);layout.addWidget(self.scroll)
        self.rail=QWidget(self);rail_layout=QVBoxLayout(self.rail);rail_layout.setContentsMargins(0,0,0,0);rail_layout.setSpacing(0)
        self.page_label.setAlignment(Qt.AlignmentFlag.AlignHCenter|Qt.AlignmentFlag.AlignVCenter)
        rail_layout.addWidget(self.page_label)
        opacity=QGraphicsOpacityEffect(self.rail);opacity.setOpacity(.8);self.rail.setGraphicsEffect(opacity)
        for button in (self.next,self.previous):
            button.setMinimumSize(0,0);button.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Expanding)
            button.setStyleSheet('min-height:0px;min-width:0px;padding:0px;')
            rail_layout.addWidget(button,1)
        outer.addWidget(self.rail)
        self.checks={};self._display=None;self._key=None;self._top=10
        viewport.installEventFilter(self)
        self.hide()

    def set_display(self, channels, descriptor, *, native_rgb=False):
        if native_rgb or descriptor is None or descriptor.get('mode')=='rgb':
            self.hide();return
        self._display=deepcopy(descriptor)
        key=(tuple((c['index'],c['label'],descriptor.get('colors',{}).get(str(c['index']),c.get('color','#ffffff'))) for c in channels),
             tuple(descriptor['channels']),descriptor.get('mode'))
        if key!=self._key:
            self._key=key
            while self.rows.count():
                old=self.rows.takeAt(0).widget();old.hide();old.deleteLater()
            self.checks={}
            selected=set(descriptor['channels'])
            for c in channels:
                index=c['index'];check=QCheckBox(c['label'],self.body);check.setFixedHeight(20)
                check.setProperty('fullChannelLabel',c['label'])
                check.setChecked(index in selected);check.setAccessibleName('Show '+c['label'])
                color=descriptor.get('colors',{}).get(str(index),c.get('color','#ffffff'))
                shown_color='#ffffff' if descriptor.get('mode')=='grayscale' and index in selected else color
                swatch=QPixmap(10,10);swatch.fill(QColor(shown_color));check.setIcon(QIcon(swatch))
                check.setToolTip(c['label']+' · '+('grayscale' if shown_color!=color else color))
                check.toggled.connect(lambda checked,i=index:self._toggle(i,checked))
                self.checks[index]=check;self.rows.addWidget(check)
        self.show();self._layout_rows()

    def _toggle(self,index,checked):
        channels=list(self._display['channels'])
        if checked and index not in channels:channels.append(index)
        channels=[c for c in self.checks if c in channels]
        if not checked:channels=[c for c in channels if c!=index]
        if not channels:
            with QSignalBlocker(self.checks[index]):self.checks[index].setChecked(True)
            return
        requested=deepcopy(self._display);requested['channels']=channels
        requested['mode']='composite' if len(channels)>1 else ('grayscale' if self._display['mode']=='grayscale' else 'colour')
        # The accepted display remains authoritative while a channel loads.
        with QSignalBlocker(self.checks[index]):self.checks[index].setChecked(index in self._display['channels'])
        self.changed.emit(requested)

    def _turn_page(self,step):
        self._page=max(0,min((len(self.checks)-1)//6,self._page+step));self._layout_rows()

    def _layout_rows(self):
        pages=max(1,(len(self.checks)+5)//6);self._page=min(self._page,pages-1)
        self.previous.setEnabled(self._page>0);self.next.setEnabled(self._page<pages-1)
        self.page_label.setText(f'{self._page+1}/{pages}')
        self.rail.setVisible(pages>1);self.page_label.setVisible(pages>1)
        shown=0;maximum=0
        for i,check in enumerate(self.checks.values()):
            visible=i//6==self._page;check.setVisible(visible)
            check.setText(check.property('fullChannelLabel'))
            if visible:
                check.ensurePolished()
                maximum=max(maximum,check.sizeHint().width());shown+=1
        self.body.setMinimumHeight(shown*22)
        self.rows.invalidate();self.rows.activate()
        rail_width=max(20,self.page_label.sizeHint().width()) if pages>1 else 0
        self.rail.setFixedWidth(rail_width)
        padding=13+(3+rail_width if pages>1 else 0)
        natural_width=max(self.title.sizeHint().width(),maximum)+padding+4
        width=min(190,natural_width,max(80,self.parentWidget().width()-20))
        available=max(0,width-padding)
        for check in self.checks.values():
            if not check.isHidden() and width<natural_width:
                full=check.property('fullChannelLabel')
                decoration=check.sizeHint().width()-check.fontMetrics().horizontalAdvance(full)
                check.setText(check.fontMetrics().elidedText(full,Qt.TextElideMode.ElideRight,max(0,available-decoration)))
        height=min(180,max(82 if pages>1 else 58,33+shown*23),max(58,self.parentWidget().height()-self._top-20))
        self.setFixedSize(width,height);self.move(10,self._top);self.raise_();self.geometry_changed.emit()

    def set_top(self,top):
        self._top=top;self._layout_rows()

    def eventFilter(self,watched,event):
        if event.type()==QEvent.Type.Resize:self._layout_rows()
        return super().eventFilter(watched,event)
