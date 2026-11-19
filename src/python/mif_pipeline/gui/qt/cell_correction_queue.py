"""Spatially linked edits collected by image for one correction review."""
from PySide6.QtCore import Qt, Signal, QSize, QRectF, QSignalBlocker
from PySide6.QtGui import QColor, QPainter, QPen, QPalette, QIcon
from PySide6.QtWidgets import (
    QFrame, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QAbstractItemView, QToolButton, QButtonGroup,
    QStyledItemDelegate, QStyleOptionViewItem, QStyle, QGraphicsObject, QComboBox,
)
from shiboken6 import isValid
from .researcher_widgets import ContentSizedButton

_LABELS = {'cell_outline': 'Cell outline', 'nucleus_outline': 'Nuclear outline', 'delete': 'Delete cell',
           'split': 'Split cell', 'merge': 'Merge cells', 'add_cell': 'New cell', 'add_nucleus': 'New nuclear outline'}
_BADGE_CENTER_X = 29


def operation_label(operations):
    return 'Cell and nuclear outlines' if set(operations) == {'cell_outline', 'nucleus_outline'} else ' · '.join(_LABELS[op] for op in operations)


class _EditDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        if index.data(Qt.ItemDataRole.UserRole) is None:
            return index.data(Qt.ItemDataRole.SizeHintRole) or QSize(200, 44)
        return QSize(200, option.fontMetrics.height()*2+16)

    def paint(self, painter, option, index):
        appearance = QStyleOptionViewItem(option); self.initStyleOption(appearance,index)
        appearance.text = ''
        appearance.icon = QIcon()
        appearance.features &= ~QStyleOptionViewItem.ViewItemFeature.HasDecoration
        style = appearance.widget.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem,appearance,painter,appearance.widget)
        entry = index.data(Qt.ItemDataRole.UserRole)
        if entry is None:
            return
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        color = option.palette.color(QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text)
        painter.save(); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(color,1.2))
        target=QRectF(option.rect.left()+_BADGE_CENTER_X-22,option.rect.center().y()-22,44,44)
        badge = QRectF(target.center().x()-12,option.rect.center().y()-12,24,24)
        thumbnail=index.data(Qt.ItemDataRole.DecorationRole)
        if thumbnail is not None and not thumbnail.isNull():
            painter.drawPixmap(target.toRect(),thumbnail)
            badge=QRectF(target.left()+1,target.top()+1,18,18)
            painter.fillRect(badge,QColor('#173d47'));painter.setPen(QColor('white'))
            painter.drawText(badge,Qt.AlignmentFlag.AlignCenter,str(entry['number']))
        else:
            painter.drawEllipse(badge); painter.drawText(badge,Qt.AlignmentFlag.AlignCenter,str(entry['number']))
        left=option.rect.left()+61
        width=option.rect.right()-left-(40 if self.parent().itemWidget(self.parent().item(index.row())) else 8)
        font=option.font; font.setBold(True); painter.setFont(font); painter.setPen(color)
        painter.drawText(left,option.rect.top()+7+option.fontMetrics.ascent(),
            option.fontMetrics.elidedText(operation_label(entry['operations']),Qt.TextElideMode.ElideRight,width))
        font.setBold(False); painter.setFont(font)
        x,y=entry['center_um']
        painter.drawText(left,option.rect.top()+9+option.fontMetrics.height()+option.fontMetrics.ascent(),f'X {x:.1f} · Y {y:.1f} µm')
        painter.restore()


class PendingCorrectionList(QWidget):
    """Action groups locate edits; filtering never changes the submitted set."""
    focused = Signal(object)

    def __init__(self,parent=None, *, add_button=None):
        super().__init__(parent)
        self.entries=[]
        body=QVBoxLayout(self);body.setContentsMargins(0,0,0,0);body.setSpacing(6)
        self.filter_bar = QWidget(self)
        groups=QHBoxLayout(self.filter_bar);groups.setContentsMargins(0,0,0,0);groups.setSpacing(8)
        self.filters={};self._group=QButtonGroup(self);self._group.setExclusive(True)
        for key in ('all','outline','delete'):
            button=QToolButton(self);button.setAutoRaise(True);button.setCheckable(True)
            self._group.addButton(button);self.filters[key]=button;groups.addWidget(button)
            button.clicked.connect(lambda checked=False,key=key:self._filter(key))
        self.filters['all'].setChecked(True);groups.addStretch();body.addWidget(self.filter_bar)
        self.list=QListWidget(self);self.list.setAccessibleName('Pending corrections by action and tissue location')
        self.list.setItemDelegate(_EditDelegate(self.list));self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setStyleSheet('QListWidget { background: #f2f6f5; border: 1px solid #dce4e4; border-radius: 8px; padding: 4px; } '
            'QListWidget::item { border-radius: 6px; margin: 2px 0; } '
            'QListWidget::item:selected { background: #006b70; color: white; } '
            'QListWidget::item:hover:!selected { background: #e4eeec; }')
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.currentItemChanged.connect(self._focus)
        body.addWidget(self.list)
        self.add_item = None
        if add_button is not None:
            add_button.setStyleSheet(
                'QPushButton { background: transparent; color: #006b70; border: none;'
                ' padding: 6px 0; text-align: left; font-weight: 600; }'
                'QPushButton:hover, QPushButton:focus { background: #dcece8; }'
                'QPushButton:disabled { color: #647776; }'
                'QPushButton::menu-indicator { width: 0; }'
            )
            self.add_item = QListWidgetItem(self.list)
            self.add_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.add_item.setSizeHint(QSize(200, add_button.sizeHint().height() + 6))
            row = QWidget(self.list)
            layout = QHBoxLayout(row)
            self.add_row_layout = layout
            self.add_row_inset = _BADGE_CENTER_X - add_button.fontMetrics().horizontalAdvance('+') // 2
            layout.setContentsMargins(0, 2, 0, 2)
            layout.addWidget(add_button)
            self.list.setItemWidget(self.add_item, row)
            self.list.setStyleSheet(self.list.styleSheet() +
                ' QListWidget[emptyCollection="true"] { background: white; border: 1px solid #dce4e4; border-radius: 5px; padding: 0; }')
            self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)

    def set_entries(self,entries):
        current = self.list.currentItem()
        previous = current.data(Qt.ItemDataRole.UserRole) if current else None
        key = previous['identity'] if previous else None
        self.entries=[dict(item,number=i+1) for i,item in enumerate(entries)]
        blocker = QSignalBlocker(self.list)
        # Leave the action row in the list so its menu and keyboard focus survive a refresh.
        for i in reversed(range(self.list.count())):
            if self.list.item(i) is not self.add_item:
                self.list.takeItem(i)
        for i, entry in enumerate(self.entries):
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole,entry)
            item.setText(operation_label(entry['operations']))
            item.setToolTip(operation_label(entry['operations']))
            self.list.insertItem(i, item)
            if entry['identity']==key:self.list.setCurrentItem(item)
        del blocker
        if self.add_item is not None:
            self.filter_bar.setVisible(bool(entries))
            empty = not entries
            if self.list.property('emptyCollection') != empty:
                self.add_row_layout.setContentsMargins(0 if empty else self.add_row_inset, 2, 0, 2)
                self.list.setProperty('emptyCollection', empty)
                self.list.style().unpolish(self.list)
                self.list.style().polish(self.list)
        count=len(entries);deleted=sum('delete' in item['operations'] for item in entries)
        for name,label,n in (('all','All',count),('outline','Outlines',count-deleted),('delete','Delete',deleted)):
            self.filters[name].setText(f'{label} {n}')
            self.filters[name].setEnabled(bool(n))
        selected=next((name for name,button in self.filters.items() if button.isChecked()),'all')
        if not self.filters[selected].isEnabled():selected='all';self.filters[selected].setChecked(True)
        self._filter(selected,focus=False)

    def _filter(self,kind,*,focus=True):
        if focus:self.list.clearSelection()
        visible=[]
        for i,entry in enumerate(self.entries):
            show=kind=='all' or ('delete' in entry['operations'])==(kind=='delete')
            self.list.item(i).setHidden(not show)
            if show:visible.append(self.list.item(i))
        if focus and visible:self.list.setCurrentItem(visible[0])

    def _focus(self,item,_previous=None):
        if item is not None and item is not self.add_item and not item.isHidden():
            self.focused.emit(item.data(Qt.ItemDataRole.UserRole))

    def visible_keys(self):
        return tuple(item['identity'] for i,item in enumerate(self.entries) if not self.list.item(i).isHidden())

    def set_thumbnail(self,index,pixmap):
        self.list.item(index).setData(Qt.ItemDataRole.DecorationRole,
            pixmap.scaled(64,64,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation))

    def focus_key(self,key):
        for i,entry in enumerate(self.entries):
            if entry['identity']==key:
                if self.list.item(i).isHidden():self.filters['all'].click()
                self.list.setCurrentRow(i);return


class _EditMarker(QGraphicsObject):
    clicked=Signal(object)
    def __init__(self,entry):
        super().__init__();self.entry=entry;self.selected=False
        self.setFlag(self.GraphicsItemFlag.ItemIgnoresTransformations)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.setToolTip(operation_label(entry['operations']))
        self.setPos(*entry['center_um']);self.setZValue(20)
    def boundingRect(self):return QRectF(3,-26,24,24)
    def paint(self,painter,option,widget=None):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor('#ffffff'),1.4));painter.setBrush(QColor('#006b70' if self.selected else '#253c3e'))
        painter.drawEllipse(self.boundingRect().adjusted(1,1,-1,-1))
        font=painter.font();font.setBold(True);painter.setFont(font)
        painter.drawText(self.boundingRect(),Qt.AlignmentFlag.AlignCenter,str(self.entry['number']))
    def mousePressEvent(self,event):
        self.clicked.emit(self.entry['identity']);event.accept()


class CorrectionMarkers:
    """Number only visible pending cells, using their exact scoped identities."""
    def __init__(self,field,focus):self.field=field;self.focus=focus;self.items=[]
    def clear(self):
        for item in self.items:
            if isValid(item) and item.scene() is not None:item.scene().removeItem(item)
        self.items=[]
    def show(self,entries,bundle):
        self.clear()
        visible={self.field._cell_key(row) for row in bundle['metadata'].get('visible_cells',())}
        for entry in entries:
            if entry['identity'] not in visible:continue
            marker=_EditMarker(entry);marker.clicked.connect(self.focus)
            self.field.view.scene().addItem(marker);self.items.append(marker)
    def select(self,key):
        for item in self.items:
            if isValid(item):item.selected=item.entry['identity']==key;item.update()


class CellCorrectionQueue(QFrame):
    edit_requested=Signal()
    delete_requested=Signal()
    review_requested=Signal()
    focus_requested=Signal(object)
    changed=Signal()
    image_requested=Signal(str)

    def __init__(self,parent=None, *, add_button=None):
        super().__init__(parent);self.setProperty('stageInfoPanel',True)
        self._scope=self._active_scope=None;self._available=False;self._rows={};self._markers=None
        self._groups={};self._image_name='';self._field_context=None
        self.draft=None
        self.native_draft=None
        body=QVBoxLayout(self);body.setContentsMargins(12,10,12,10);body.setSpacing(8)
        self.header = QWidget(self)
        heading=QHBoxLayout(self.header)
        heading.setContentsMargins(0,0,0,0)
        self.summary=QLabel(self);self.summary.setProperty('stageFieldCaption',True);heading.addWidget(self.summary)
        self.image_groups=QComboBox(self);self.image_groups.setAccessibleName('Images with collected corrections')
        self.image_groups.activated.connect(lambda _index:self.image_requested.emit(self.image_groups.currentData()))
        self.image_groups.setMaximumWidth(310)
        heading.addStretch()
        heading.addWidget(self.image_groups)
        body.addWidget(self.header)
        self.pending=PendingCorrectionList(self, add_button=add_button);self.pending.list.setMaximumHeight(224)
        self.pending.focused.connect(self._focus);body.addWidget(self.pending)
        actions=QHBoxLayout()
        self.clear_button=ContentSizedButton('Clear queue',self);self.clear_button.clicked.connect(self.clear);actions.addWidget(self.clear_button)
        self.discard=ContentSizedButton('Discard draft',self);self.discard.clicked.connect(lambda:self.set_draft(None));actions.addWidget(self.discard)
        actions.addStretch()
        self.review=ContentSizedButton('Review changes…',self);self.review.clicked.connect(self.review_requested);self.review.hide()
        self.edit=ContentSizedButton('Resume draft…',self);self.edit.clicked.connect(self._request)
        actions.addWidget(self.edit);body.addLayout(actions)
        self.hint=QLabel(self);self.hint.setWordWrap(True);self.hint.setProperty('fieldHelp',True);body.addWidget(self.hint)
        self._refresh()

    def _save_group(self):
        if self._active_scope is None:return
        artifact,head=self._active_scope
        if self._rows:
            self._groups[artifact]=dict(rows=self._rows,draft=self.draft,head=head,name=self._image_name,field_context=self._field_context,available=self._available)
        else:self._groups.pop(artifact,None)

    def reconcile(self,artifacts,head_sha256):
        """Keep queued cells only where the current catalogue retains their population."""
        self._save_group()
        retained=set(artifacts)
        self._groups={artifact:group for artifact,group in self._groups.items() if artifact in retained}
        for group in self._groups.values():
            if group['head']!=head_sha256:group['draft']=None
            group['head']=head_sha256
        if self._active_scope is not None:
            artifact=self._active_scope[0]
            self._active_scope=(artifact,head_sha256)
            group=self._groups.get(artifact,{})
            self._rows=group.get('rows',{});self.draft=group.get('draft');self._field_context=group.get('field_context')
            self._scope=self._active_scope if self._rows else None
        self._refresh()

    def set_scope(self,artifact_ref_sha256,head_sha256,available,*,image_name=''):
        self._save_group()
        active=(artifact_ref_sha256,head_sha256)
        if any(group['head']!=head_sha256 for group in self._groups.values()):self._groups.clear()
        group=self._groups.get(artifact_ref_sha256,{})
        self._rows=group.get('rows',{});self.draft=group.get('draft');self._field_context=group.get('field_context')
        self._image_name=image_name or group.get('name') or 'Selected image'
        self._scope=active if self._rows else None
        self._active_scope,self._available=active,bool(available);self._refresh()

    def pending_count(self):
        return sum(len(group['rows']) for group in self._groups.values())

    def add(self,identity,operation,row,bundle=None):
        if self.draft is not None:
            raise ValueError('Discard the draft before changing its queued cells.')
        identity=tuple(identity)
        if (len(identity)!=5 or any(not isinstance(value,str) or not value for value in identity)
                or operation not in _LABELS or not self._available or self._active_scope is None):
            raise ValueError('Select an existing cell from the current segmentation.')
        if self._scope is not None and (self._scope!=self._active_scope or next(iter(self._rows))[:4]!=identity[:4]):
            raise ValueError('Select cells from the same tissue section for this editing session.')
        self._scope=self._active_scope
        current=self._rows.setdefault(identity,dict(row=dict(row),operations=set()))
        exclusive={'delete','split','merge','add_cell'}
        if operation in exclusive or current['operations'] & exclusive:current['operations'].clear()
        if operation=='add_nucleus':current['operations'].discard('nucleus_outline')
        if operation=='nucleus_outline':current['operations'].discard('add_nucleus')
        current['operations'].add(operation);self._refresh()

    def identities(self):return tuple(self._rows)
    def operations(self):return self._operations(self._rows)

    @staticmethod
    def _operations(rows):
        result=[dict(cell_id=identity[-1],operation=operation)
                for identity,item in sorted(rows.items()) for operation in sorted(item['operations']) if operation!='merge']
        sources=sorted(identity[-1] for identity,item in rows.items() if 'merge' in item['operations'])
        if len(sources)>1:result.append(dict(cell_id=sources[0],operation='merge',source_cell_ids=sources))
        return sorted(result,key=lambda item:(item['cell_id'],item['operation']))
    def queued_images(self):
        self._save_group()
        images=[dict(group,artifact_ref_sha256=artifact,identities=tuple(group['rows']),
                     operations=self._operations(group['rows'])) for artifact,group in self._groups.items()]
        names=[image['name'] for image in images]
        seen={}
        for image in images:
            name=image['name']
            if names.count(name)>1:
                seen[name]=seen.get(name,0)+1
                image['name']=f"{name} · Section {seen[name]}"
        return images

    def drafts_ready(self):
        return (not (self.native_draft and self.native_draft.get('pending'))
                and bool(self._groups) and all(group['draft'] is not None for group in self._groups.values()))

    def native_signature(self):
        """A saved native project belongs to exactly this head and queued set."""
        self._save_group()
        return tuple((artifact, group['head'], tuple((identity, tuple(sorted(item['operations'])))
            for identity, item in sorted(group['rows'].items()))) for artifact, group in self._groups.items())

    def native_recovery(self):
        if self.native_draft and self.native_draft['signature'] != self.native_signature():
            self.native_draft=None
        return self.native_draft

    def batch_ready(self):
        return bool(self._groups) and all(group.get('available',True) and
            sum('merge' in item['operations'] for item in group['rows'].values())!=1
            for group in self._groups.values())

    def clear_image(self,artifact):
        if self._active_scope and artifact==self._active_scope[0]:self.clear()
        else:self._groups.pop(artifact,None);self._refresh()

    def set_image_draft(self,artifact,candidate):
        if artifact not in self._groups:return
        self._groups[artifact]['draft']=candidate
        if self._active_scope and artifact==self._active_scope[0]:self.draft=candidate
        self._refresh()

    def clear(self):
        self._rows.clear();self._scope=None;self.draft=None;self._refresh()
    def set_draft(self,candidate):
        if candidate is None:self.native_draft=None
        self.draft=candidate;self._refresh()
    def _remove(self, identity):
        if self.draft is not None or identity not in self._rows:
            return
        del self._rows[identity]
        if not self._rows:self._scope=None
        self._refresh()
    def _focus(self,entry):
        if self._markers is not None:self._markers.select(entry['identity'])
        self.focus_requested.emit(entry)
    def link_field(self,field):self._markers=CorrectionMarkers(field,self.pending.focus_key)
    def update_field(self,bundle):
        metadata=bundle['metadata']
        self._field_context={key:metadata[key] for key in ('source_frame','field_um','display_options',
            'sample_id','project_entry_id','slide_id','section_id','cell_boundary_source','nucleus_boundary_source') if key in metadata}
        self._save_group()
        if self._markers is not None:
            self._markers.show(self.pending.entries if self._scope==self._active_scope else (),bundle)
            current=self.pending.list.currentItem()
            if current is not None and current is not self.pending.add_item and not current.isHidden():
                self._markers.select(current.data(Qt.ItemDataRole.UserRole)['identity'])
    def _request(self):
        if not self.batch_ready():return
        outlines=any(item['operation']!='delete' for group in self.queued_images() for item in group['operations'])
        (self.edit_requested if outlines else self.delete_requested).emit()
    def request_primary(self):
        """Open the complete collected set through the review footer."""
        if not self.batch_ready():return
        if self.drafts_ready():self.review_requested.emit()
        else:self._request()
    def _refresh(self):
        self._save_group()
        recovery=self.native_recovery()
        count=len(self._rows);total=self.pending_count();group_count=len(self._groups)
        self.setVisible(bool(total) or self._available)
        self.summary.setText('Collected corrections')
        if total:
            caption = f'{total} cell' + ('s' if total != 1 else '')
            if group_count > 1:
                caption += f' across {group_count} images'
            self.summary.setText(f'Collected corrections · {caption}')
        with QSignalBlocker(self.image_groups):
            self.image_groups.clear()
            for group in self.queued_images():
                n=len(group['rows'])
                self.image_groups.addItem(f"{group['name']} · {n} cell"+('s' if n!=1 else ''),group['artifact_ref_sha256'])
            self.image_groups.setCurrentIndex(self.image_groups.findData(self._active_scope[0]) if self._active_scope else -1)
        self.image_groups.setVisible(group_count>1 or bool(total and not count))
        if self.property('stageInfoPanel') != bool(total):
            self.setProperty('stageInfoPanel', bool(total))
            self.style().unpolish(self)
            self.style().polish(self)
        self.header.setVisible(bool(total))
        self.pending.setVisible(bool(count) or (self._available and self.pending.add_item is not None))
        self.layout().setContentsMargins(12 if total else 0, 10 if total else 0, 12 if total else 0, 10 if total else 0)
        self.pending.set_entries([dict(identity=identity,operations=sorted(item['operations']),
            center_um=[item['row']['x_um'],item['row']['y_um']]) for identity,item in self._rows.items()])
        items = self.pending.list
        row_height = sum(items.sizeHintForRow(i) for i in range(min(items.count(), 6)))
        items.setFixedHeight(min(260, row_height + 2 * items.frameWidth() + (12 if count else 2)))
        outlines=any(op!='delete' for item in self._rows.values() for op in item['operations'])
        self.edit.setText(('Resume draft…' if self.draft is not None else 'Edit outlines…') if outlines else 'Review deletions…')
        merging=sum('merge' in item['operations'] for item in self._rows.values())
        self.edit.setEnabled(bool(count) and merging!=1 and self._available and self._scope==self._active_scope)
        self.review.setEnabled(self.draft is not None and self.edit.isEnabled())
        self.discard.setVisible(self.draft is not None);self.clear_button.setVisible(self.draft is None)
        self.clear_button.setEnabled(self.draft is None)
        self.clear_button.setText('Clear image queue' if group_count>1 else 'Clear queue')
        self.clear_button.setVisible(bool(count) and self.draft is None)
        self.edit.hide()
        if self.draft is None:
            for index, entry in enumerate(self.pending.entries):
                item = self.pending.list.item(index)
                row = QWidget(self.pending.list)
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(0, 0, 6, 0)
                row_layout.addStretch()
                remove = QToolButton(row)
                remove.setText("×")
                remove.setAutoRaise(True)
                remove.setToolTip(f"Remove correction {entry['number']} from the queue")
                remove.setAccessibleName(remove.toolTip())
                remove.clicked.connect(lambda checked=False, key=entry['identity']: self._remove(key))
                row_layout.addWidget(remove)
                self.pending.list.setItemWidget(item, row)
        self.hint.setText('Return to the queued image to edit these cells.' if self._scope!=self._active_scope and count else
            'Saved outlines need checking. Choose Edit outlines to reopen the retained draft.' if recovery and recovery.get('project') and recovery.get('pending') else
            'Draft not applied. It remains available while this review is open.' if self.draft is not None else
            'Select another cell in the image and add it to this merge.' if merging==1 else
            'Choose an image with collected corrections.' if total and not count else '')
        self.hint.setVisible(bool(self.hint.text()))
        if self._markers is not None and not count:self._markers.clear()
        self.changed.emit()
