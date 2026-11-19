"""Threshold-dependent stain regions linked to saved outlines in the current field."""
from __future__ import annotations

import csv
from io import StringIO
import numpy as np
from PySide6.QtCore import Qt, Signal, QSignalBlocker, QTimer, QSize
from PySide6.QtGui import QImage, QPixmap, QTransform, QColor, QFontMetrics
from PySide6.QtWidgets import QFrame, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QComboBox, QLineEdit, QDoubleSpinBox, QListWidget, QListWidgetItem, QCheckBox, QStyledItemDelegate, QStyleOptionViewItem, QStyle
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from matplotlib.ticker import MaxNLocator
from shiboken6 import isValid

from mif_pipeline.cell_analysis.segmentation_relationships import native_pixels_available, stain_outline_relationships
from .researcher_widgets import ContentSizedButton


class _RelationshipDelegate(QStyledItemDelegate):
    """Keep counts and physical scale visible beside each inspectable relation."""
    def sizeHint(self,option,index):
        return QSize(220,option.fontMetrics.height()*2+16)

    def paint(self,painter,option,index):
        appearance=QStyleOptionViewItem(option);self.initStyleOption(appearance,index);appearance.text=''
        appearance.widget.style().drawControl(QStyle.ControlElement.CE_ItemViewItem,appearance,painter,appearance.widget)
        title,summary=index.data(Qt.ItemDataRole.UserRole)
        painter.save();left=option.rect.left()+10;width=option.rect.width()-20
        font=option.font;font.setBold(True);painter.setFont(font);painter.setPen(QColor('#173d47'))
        painter.drawText(left,option.rect.top()+7+option.fontMetrics.ascent(),
            QFontMetrics(font).elidedText(title,Qt.TextElideMode.ElideRight,width))
        font.setBold(False);font.setPointSizeF(max(8.,font.pointSizeF()-1.));painter.setFont(font);painter.setPen(QColor('#526971'))
        painter.drawText(left,option.rect.top()+9+option.fontMetrics.height()+QFontMetrics(font).ascent(),
            QFontMetrics(font).elidedText(summary,Qt.TextElideMode.ElideRight,width))
        painter.restore()


class StainRelationshipReview(QFrame):
    """Describe the entire loaded field without launching reads or guessing labels."""
    result_changed=Signal(bool)

    def __init__(self,field,parent=None):
        super().__init__(parent)
        self.field=field;self._bundle=None;self._result=None;self._overlay=None
        self._mode='merged';self._records=[];self._outline_indices=[];self._outline_position=0;self._region_position=0;self._bar_start=0
        self.setProperty('stageInfoPanel',True);self.setMinimumWidth(0)
        body=QVBoxLayout(self);body.setContentsMargins(12,12,12,12);body.setSpacing(12)
        self.title=QLabel(self);self.title.setProperty('panelHeading',True);self.title.setWordWrap(True);body.addWidget(self.title)
        form=QFormLayout();form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setVerticalSpacing(10)
        self.channel=QComboBox(self);self.channel.setMinimumContentsLength(9)
        self.channel.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.channel.setAccessibleName('Stain for outline overlap');form.addRow('Stain',self.channel)
        self.compartment=QComboBox(self);self.compartment.addItem('Nuclear outlines','nucleus');self.compartment.addItem('Cell outlines','cell')
        self.compartment.setAccessibleName('Outline compartment');form.addRow('Outlines',self.compartment)
        self.threshold=QLineEdit(self);self.threshold.setPlaceholderText('Stored channel units');self.threshold.setAccessibleName('Staining threshold')
        self.threshold.setToolTip('Pixels above this intensity form a stained region when they touch along an edge or corner.')
        form.addRow('Threshold',self.threshold)
        self.minimum_area=QDoubleSpinBox(self);self.minimum_area.setRange(0,10000);self.minimum_area.setDecimals(2);self.minimum_area.setValue(2.)
        self.minimum_area.setSuffix(' µm²');self.minimum_area.setAccessibleName('Minimum overlap area')
        self.minimum_area.setToolTip('List overlaps of at least this area between a stained region and an outline.')
        form.addRow('Minimum overlap',self.minimum_area);body.addLayout(form)
        self.message=QLabel(self);self.message.setWordWrap(True);self.message.setProperty('fieldHelp',True);body.addWidget(self.message)
        self.list_heading=QLabel(self);self.list_heading.setProperty('stageFieldCaption',True);body.addWidget(self.list_heading)
        self.locations=QListWidget(self);self.locations.setMinimumWidth(0);self.locations.setMaximumHeight(170)
        self.locations.setItemDelegate(_RelationshipDelegate(self.locations))
        self.locations.setStyleSheet('QListWidget { background: #f2f6f5; border: none; border-radius: 7px; padding: 3px; outline: none; } '
            'QListWidget::item { border-radius: 5px; margin: 1px 0; } '
            'QListWidget::item:selected { background: #cce6e2; border-left: 3px solid #006b70; } '
            'QListWidget::item:hover:!selected { background: #e4eeec; }')
        self.locations.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.locations.setAccessibleName('Outlines or stained regions selected for overlap review')
        self.locations.currentRowChanged.connect(self._selected);body.addWidget(self.locations)
        self.highlight=QCheckBox('Highlight selected staining',self);self.highlight.setChecked(True);self.highlight.toggled.connect(self._highlight);body.addWidget(self.highlight)
        self.selected_heading=QLabel(self);self.selected_heading.setProperty('stageFieldCaption',True);self.selected_heading.setWordWrap(True);body.addWidget(self.selected_heading)
        self.canvas=FigureCanvasQTAgg(Figure(figsize=(3.2,2),layout='constrained'));self.canvas.setMinimumSize(0,140);self.canvas.setMaximumHeight(360)
        self.canvas.setAccessibleName('Stain area inside and outside each selected outline');self.axis=self.canvas.figure.subplots()
        self.canvas.mpl_connect('button_press_event',self._picked);body.addWidget(self.canvas,1);self.canvas.hide()
        self.legend=QLabel('Filled: inside outline · Pale: outside this outline',self)
        self.legend.setProperty('fieldHelp',True);self.legend.setWordWrap(True);body.addWidget(self.legend)
        paging=QHBoxLayout();self.previous_outline=ContentSizedButton('‹ Outline',self);self.next_outline=ContentSizedButton('Outline ›',self)
        self.previous_outline.clicked.connect(lambda:self._visit_outline(-1));self.next_outline.clicked.connect(lambda:self._visit_outline(1))
        self.position=QLabel(self);self.position.setAlignment(Qt.AlignmentFlag.AlignCenter)
        paging.addWidget(self.previous_outline);paging.addWidget(self.position,1);paging.addWidget(self.next_outline);body.addLayout(paging)
        self.selected_detail=QLabel(self);self.selected_detail.setProperty('fieldHelp',True);self.selected_detail.setWordWrap(True);body.addWidget(self.selected_detail)
        self.detail=QLabel(self);self.detail.hide()
        body.addStretch()
        self._timer=QTimer(self);self._timer.setSingleShot(True);self._timer.setInterval(350);self._timer.timeout.connect(self._calculate)
        self.channel.currentIndexChanged.connect(self._edited);self.compartment.currentIndexChanged.connect(self._edited)
        self.threshold.textChanged.connect(self._edited);self.minimum_area.valueChanged.connect(self._edited)
        self._present();self._clear()

    def set_active(self,mode):
        active=mode in {'merged','fragmented'};self.setVisible(active)
        changed=active and mode!=self._mode
        if active:self._mode=mode;self._present()
        if self._overlay is not None and isValid(self._overlay):self._overlay.setVisible(active and self.highlight.isChecked())
        if active:
            if changed and self._result is not None:self._populate()
            elif self._result is None:self._edited()
            else:self._show_field_totals()
        else:
            self._timer.stop()
            self.field.set_inspection_note()

    def _present(self):
        self.title.setText('Stain regions within one outline' if self._mode=='merged' else 'Outlines sharing one stained region')
        self.list_heading.setText('Candidate outlines' if self._mode=='merged' else 'Candidate stained regions')
        noun='Region' if self._mode=='merged' else 'Outline'
        self.previous_outline.setText(f'‹ {noun}');self.next_outline.setText(f'{noun} ›')
        self.canvas.setToolTip('Select a bar to highlight its stained region and locate the overlapping outline.')

    def set_bundle(self,bundle):
        self._bundle=bundle
        previous=self.channel.currentData()
        with QSignalBlocker(self.channel),QSignalBlocker(self.compartment):
            self.channel.clear()
            for channel,name in zip(bundle['channel_indices'],bundle['channel_names']):self.channel.addItem(str(name),int(channel))
            preferred=previous if previous is not None else bundle['metadata'].get('nuclear_channel')
            self.channel.setCurrentIndex(max(0,self.channel.findData(preferred)))
            present=set(map(str,bundle['outline_kinds']))
            for index in range(self.compartment.count()):self.compartment.model().item(index).setEnabled(self.compartment.itemData(index) in present)
            if self.compartment.currentData() not in present:
                self.compartment.setCurrentIndex(next((i for i in range(self.compartment.count()) if self.compartment.itemData(i) in present),0))
        self._edited()

    def _clear_overlay(self):
        if self._overlay is not None and isValid(self._overlay) and self._overlay.scene() is not None:self._overlay.scene().removeItem(self._overlay)
        self._overlay=None

    def invalidate_field(self):
        self._timer.stop();self._bundle=None;self._clear()
        self.message.setText('Loading current field…')

    def _clear(self):
        self._result=None;self._records=[];self._outline_indices=[];self._clear_overlay()
        self.field.set_inspection_note()
        self.message.show()
        with QSignalBlocker(self.locations):self.locations.clear()
        self.canvas.hide();self.legend.hide();self.selected_heading.clear();self.selected_detail.clear();self.detail.clear();self.position.clear()
        self.previous_outline.setEnabled(False);self.next_outline.setEnabled(False);self.result_changed.emit(False)

    def _edited(self,*_args):
        self._timer.stop();self._clear()
        if self._bundle is None:return
        if not native_pixels_available(self._bundle):
            self.message.setText('Zoom in to compare original staining pixels and complete outlines.');return
        try:threshold=float(self.threshold.text())
        except ValueError:threshold=np.nan
        if not np.isfinite(threshold):
            self.message.setText('Enter a stain threshold to compare the current field.');return
        self.message.setText('Updating current field…')
        if not self.isHidden():self._timer.start()

    def _calculate(self):
        if self._bundle is None or self.isHidden():return
        try:
            self._result=stain_outline_relationships(self._bundle,channel=self.channel.currentData(),threshold=float(self.threshold.text()),
                compartment=self.compartment.currentData(),min_area_um2=self.minimum_area.value())
        except ValueError as error:self._clear();self.message.setText(str(error));return
        self._populate();self.result_changed.emit(True)

    def _populate(self):
        result=self._result
        merged=self._mode=='merged'
        pixel_area=np.prod(result['pixel_size_um'])
        regions={row['region_id']:row for row in result['regions']}
        outlines={row['cell_index']:row for row in result['outlines']}
        self._records=[]
        if merged:
            for row in result['outlines']:
                shared=[(i,n) for i,n in zip(row['region_ids'],row['overlap_pixels'])
                    if n*pixel_area>=max(result['min_area_um2'],regions[i]['area_um2']*.5)]
                if len(shared)>1:self._records.append({**row,'region_ids':[i for i,n in shared],'overlap_pixels':[n for i,n in shared]})
        else:
            for row in result['regions']:
                shared=[i for i in row['cell_indices'] if
                    outlines[i]['overlap_pixels'][outlines[i]['region_ids'].index(row['region_id'])]*pixel_area>=result['min_area_um2']]
                if len(shared)>1:self._records.append({**row,'cell_indices':shared})
        self._records.sort(key=lambda row:(-len(row['region_ids' if merged else 'cell_indices']),-row['area_um2']))
        with QSignalBlocker(self.locations):
            self.locations.clear()
            for number,row in enumerate(self._records,1):
                count=len(row['region_ids' if merged else 'cell_indices'])
                if merged:
                    title=f'Outline {number} · {count} stain regions'
                    summary=f"{row['area_um2']:.1f} µm² outline · {sum(row['overlap_pixels'])*pixel_area:.1f} µm² shared"
                else:
                    shares=[100*outlines[i]['overlap_pixels'][outlines[i]['region_ids'].index(row['region_id'])]*pixel_area/row['area_um2'] for i in row['cell_indices']]
                    title=f'Region {number} · {count} outlines'
                    summary=f"{row['area_um2']:.1f} µm² stained region · {min(shares):.0f}–{max(shares):.0f}%"
                item=QListWidgetItem(title+'\n'+summary,self.locations);item.setData(Qt.ItemDataRole.UserRole,(title,summary))
                x,y=row['center_um'];item.setToolTip(f'{title}\n{summary}\nX {x:.2f} · Y {y:.2f} µm')
        row_height=self.fontMetrics().height()*2+18
        self.locations.setFixedHeight(min(170,max(58,len(self._records)*row_height+6)))
        noun='outline' if merged else 'stained region'
        self.message.setText('' if self._records else f'No candidate {noun}s in this field.')
        self.message.setVisible(not self._records)
        self._show_field_totals()
        self.locations.setToolTip(
            ("Each listed stained region has at least half its area inside the outline. " if merged else '')+
            f"Every listed overlap shares at least {result['min_area_um2']:g} µm².")
        if self._records:self.locations.setCurrentRow(0)
        else:self.canvas.hide();self.legend.hide();self.selected_heading.clear();self.selected_detail.clear();self._clear_overlay();self.position.clear();self.previous_outline.setEnabled(False);self.next_outline.setEnabled(False)

    def _show_field_totals(self):
        result = self._result
        if result is not None and not self.isHidden():
            kind = 'nuclear' if result['compartment'] == 'nucleus' else 'cell'
            self.field.set_inspection_note(
                f"{len(result['outlines']):,} fully visible {kind} outlines\n"
                f"{len(result['regions']):,} fully visible stained regions",
                'Counts use the selected outline type and minimum stain area. '
                'Stained regions and outlines touching the field edge are excluded from overlap comparisons.')

    def _selected(self,index):
        if not 0<=index<len(self._records):return
        row=self._records[index]
        self._outline_indices=[row['cell_index']] if self._mode=='merged' else row['cell_indices']
        self._outline_position=0;self._region_position=0;self._focus_outline();self._plot();self._highlight()

    def _focus_outline(self):
        if not self._outline_indices:return
        row=self._bundle['metadata']['visible_cells'][self._outline_indices[self._outline_position]]
        self.field._inspect_cell([self.field._cell_key(row)])
        merged=self._mode=='merged'
        count=len(self._records[self.locations.currentRow()]['region_ids']) if merged else len(self._outline_indices)
        position=self._region_position if merged else self._outline_position
        self.position.setText(f'{position+1} of {count}')
        self.previous_outline.setEnabled(count>1);self.next_outline.setEnabled(count>1)

    def _visit_outline(self,step):
        if self._outline_indices:
            if self._mode=='merged':self._region_position=(self._region_position+step)%len(self._records[self.locations.currentRow()]['region_ids'])
            else:self._outline_position=(self._outline_position+step)%len(self._outline_indices)
            self._focus_outline();self._plot();self._highlight()

    def _highlight(self,*_args):
        self._clear_overlay()
        index=self.locations.currentRow()
        if self._result is None or not 0<=index<len(self._records) or not self.highlight.isChecked():return
        rgba=self.highlight_rgba()
        mask=rgba[:,:,3]
        height,width=mask.shape
        image=QImage(rgba.data,width,height,rgba.strides[0],QImage.Format.Format_RGBA8888).copy()
        self._overlay=self.field.view.scene().addPixmap(QPixmap.fromImage(image))
        left,right,bottom,top=self._result['extent_um'];self._overlay.setPos(left,top)
        self._overlay.setTransform(QTransform.fromScale((right-left)/width,(bottom-top)/height))
        self._overlay.setTransformationMode(Qt.TransformationMode.FastTransformation)
        self._overlay.setZValue(4);self._overlay.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self._overlay.setVisible(not self.isHidden())

    def selected_region_ids(self):
        index=self.locations.currentRow()
        if self._result is None or not 0<=index<len(self._records):return []
        row=self._records[index]
        return [row['region_ids'][self._region_position]] if self._mode=='merged' else [row['region_id']]

    def highlight_rgba(self):
        labels=self._result['labels'];mask=np.isin(labels,self.selected_region_ids())
        rgba=np.zeros((*labels.shape,4),dtype=np.uint8);rgba[mask]=(189,104,71,130)
        return rgba

    def _plot(self):
        row=self._records[self.locations.currentRow()];pixel_area=np.prod(self._result['pixel_size_um'])
        merged=self._mode=='merged'
        if merged:
            regions={r['region_id']:r for r in self._result['regions']}
            total=np.array([regions[i]['area_um2'] for i in row['region_ids']]);areas=np.array(row['overlap_pixels'])*pixel_area
            names=[f'Region {i+1} · {area:.1f} µm²' for i,area in enumerate(total)]
            selected=self._region_position
            self.selected_heading.setText(f"Outline {self.locations.currentRow()+1} · {row['area_um2']:.1f} µm²")
        else:
            outlines={r['cell_index']:r for r in self._result['outlines']}
            areas=np.array([outlines[i]['overlap_pixels'][outlines[i]['region_ids'].index(row['region_id'])]*pixel_area for i in row['cell_indices']])
            names=[f'Outline {i+1} · {area:.1f} µm²' for i,area in enumerate(areas)]
            total=np.full(len(names),row['area_um2']);selected=self._outline_position
            self.selected_heading.setText(f"Region {self.locations.currentRow()+1} · {row['area_um2']:.1f} µm²")
        percentages=100*areas/total
        self._bar_start=(selected//4)*4;stop=min(self._bar_start+4,len(names));count=stop-self._bar_start
        self.axis.clear()
        for y,i in enumerate(range(self._bar_start,stop)):
            color='#bd6847' if i==selected else '#167c84'
            self.axis.barh(y,areas[i],color=color,height=.36)
            self.axis.barh(y,total[i]-areas[i],left=areas[i],color='#e6edeb',height=.36)
            self.axis.text(0,y-.3,names[i],fontsize=9,va='bottom',color='#173d47')
            self.axis.text(max(total),y-.3,f'{percentages[i]:.0f}% inside',fontsize=9,ha='right',va='bottom',color=color,fontweight='bold')
        self.axis.set(xlim=(0,max(total)),ylim=(count-.55,-.65),yticks=[])
        self.axis.xaxis.set_major_locator(MaxNLocator(nbins=3,min_n_ticks=3))
        self.axis.set_xlabel('Stain area (µm²)',fontsize=8)
        self.axis.tick_params(axis='x',labelsize=8,color='#b4c3c0');self.axis.spines[['top','right','left']].set_visible(False)
        self.axis.spines['bottom'].set_color('#b4c3c0')
        self.axis.grid(axis='x',color='#e5ebeb',linewidth=.5);self.axis.set_axisbelow(True)
        self.canvas.setFixedHeight(90+count*38)
        noun=f'Region {selected+1}' if merged else f'Outline {selected+1}'
        self.selected_detail.setText(f'Showing {self._bar_start+1}–{stop} of {len(names)} bars; use the arrows to continue.')
        self.selected_detail.setVisible(len(names)>4)
        self.canvas.setToolTip(f'{noun}: {areas[selected]:.1f} of {total[selected]:.1f} µm² stain inside this outline. '
            'Select either segment of a bar to highlight its exact stained region and saved outline.')
        self.legend.show()
        from mif_pipeline.cell_analysis.figure_style import apply_figure_font
        apply_figure_font(self.canvas.figure);self.canvas.show();self.canvas.draw_idle()

    def _picked(self,event):
        if event.button==1 and event.inaxes is self.axis and event.ydata is not None and self._records:
            index=round(event.ydata)+self._bar_start
            count=len(self._records[self.locations.currentRow()]['region_ids']) if self._mode=='merged' else len(self._outline_indices)
            if 0<=index<count:
                if self._mode=='merged':self._region_position=index
                else:self._outline_position=index
                self._focus_outline();self._plot();self._highlight()

    def csv_bytes(self):
        output=StringIO();writer=csv.writer(output);result=self._result
        writer.writerow(['image_sha256','channel','threshold','minimum_shared_stain_um2','compartment',
            'field_left_um','field_right_um','field_bottom_um','field_top_um','pixel_width_um','pixel_height_um',
            'sample_id','project_entry_id','slide_id','section_id','cell_id','stain_region','overlap_pixels','overlap_um2',
            'stain_region_area_um2','outline_area_um2','fraction_of_stain_region_inside_outline'])
        frame=self._bundle['metadata']['source_frame'];pixel_area=np.prod(result['pixel_size_um'])
        region_areas={row['region_id']:row['area_um2'] for row in result['regions']}
        for row in result['outlines']:
            identity=self._bundle['metadata']['visible_cells'][row['cell_index']]
            for region,count in zip(row['region_ids'],row['overlap_pixels']):
                writer.writerow([frame.get('image_sha256',''),result['channel'],result['threshold'],result['min_area_um2'],result['compartment'],
                    *result['extent_um'],*result['pixel_size_um'],*(identity.get(key,'') for key in ('sample_id','project_entry_id','slide_id','section_id')),
                    row['cell_id'],region,count,count*pixel_area,region_areas[region],row['area_um2'],count*pixel_area/region_areas[region]])
        return output.getvalue().encode('utf-8')

    def export_data(self):
        if self._result is not None:
            from .figure_export import _choose_path,_save_bytes
            path=_choose_path(self,'segmentation-stain-outline-overlap.csv',{'CSV table (*.csv)':'csv'})
            if path is not None:_save_bytes(self.csv_bytes(),path)

    def export_plot(self):
        if self._result is not None and self._records:
            from .figure_export import export_figure
            export_figure(self,self.canvas.figure,'segmentation-stain-outline-overlap.svg')
