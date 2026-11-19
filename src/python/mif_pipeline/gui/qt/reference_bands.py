"""Choose signal and AF-estimation membership in recorded acquisition order."""
from itertools import groupby

import numpy as np
from PySide6.QtCore import Qt,QTimer,QSignalBlocker,QEvent
from PySide6.QtGui import QColor,QBrush
from PySide6.QtWidgets import (QComboBox,QHBoxLayout,QHeaderView,
    QSizePolicy,QTableWidget,QTableWidgetItem,QToolButton,QToolTip,QVBoxLayout,QWidget)
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from .reference_workspace import text_label,reference_table,decision_combo,INTENSITY_UNITS
from .researcher_widgets import configure_disclosure
from .spectral_review import SpectralLegend,show_spectral_hover
from mif_pipeline.preprocessing.unmixing.spectral_plots import spectral_axis,plot_spectral_profile,label_spectral_axis


class ReferenceBandsPanel(QWidget):
    def __init__(self,review,parent=None):
        super().__init__(parent)
        self.setObjectName('reference-bands-panel')
        self._ids=tuple(review['measurement_ids']); self._names=tuple(review['measurement_names'])
        self._regions=tuple(review['regions']); self._mode=review['correction_mode']
        self._wavelengths=review.get('measurement_wavelengths'); self._wavelength_unit=review.get('wavelength_unit')
        self._optical_settings=review.get('measurement_optical_settings')
        self.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Maximum)
        layout=QVBoxLayout(self); layout.setContentsMargins(0,0,0,0); layout.setSpacing(12)
        header=QHBoxLayout(); header.setSpacing(8)
        self.overview_actions=header
        component=text_label(review['component'],self,heading=True)
        self._component=component
        component.setSizePolicy(QSizePolicy.Policy.Maximum,QSizePolicy.Policy.Maximum)
        header.addWidget(component); header.addSpacing(16)
        self._plot=QWidget(self); plot=QVBoxLayout(self._plot)
        plot.setContentsMargins(0,0,0,0); plot.setSpacing(8)
        region_label=text_label('Region',self)
        region_label.setSizePolicy(QSizePolicy.Policy.Maximum,QSizePolicy.Policy.Maximum)
        header.addWidget(region_label)
        self.region_selector=QComboBox(self); self.region_selector.setObjectName('reference-band-region')
        self.region_selector.setAccessibleName('Reference region shown in plot')
        self.region_selector.setMaximumWidth(300)
        for r in self._regions:self.region_selector.addItem(r['label'],r['roi_id'])
        header.addWidget(self.region_selector); header.addStretch(1)
        layout.addLayout(header)
        layout.addWidget(self._plot)
        figure=Figure(figsize=(9,3.2),layout='constrained'); figure.set_facecolor(self.palette().window().color().name())
        self._axes=figure.add_subplot(111); self.canvas=FigureCanvasQTAgg(figure)
        self.canvas.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.canvas.setMinimumWidth(0); self.canvas.setFixedHeight(290); plot.addWidget(self.canvas)
        self.legend=SpectralLegend(self.canvas,self); plot.addWidget(self.legend)
        self._band_choices=QWidget(self); choices=QVBoxLayout(self._band_choices)
        choices.setContentsMargins(0,0,0,0); choices.setSpacing(8)
        table_heading=QHBoxLayout(); table_heading.addWidget(text_label('Band roles',self,heading=True))
        self._inspection_hint=text_label('',self,supporting=True)
        table_heading.addWidget(self._inspection_hint)
        self._selection_summary=text_label('',self,supporting=True)
        self._selection_summary.setAlignment(Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter)
        table_heading.addWidget(self._selection_summary); choices.addLayout(table_heading)
        self.table=QTableWidget(len(self._ids),4,self); self.table.setObjectName('reference-band-roles')
        self.table.setHorizontalHeaderLabels(('Acquired band','Stained region','AF control','Band role')); reference_table(self.table)
        self.table.setStyleSheet('QTableView[scientificTable="true"] { border: none; '
            'border-top: 1px solid #c7d4d3; border-bottom: 1px solid #c7d4d3; border-radius: 0; }')
        self.table.setTabKeyNavigation(False)
        self.table.setToolTip('Assign one signal band and one or more AF-estimation bands. All acquired bands remain in the extracted spectrum.')
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch)
        for col in (1,2):
            self.table.horizontalHeader().setSectionResizeMode(col,QHeaderView.ResizeMode.Stretch)
            self.table.horizontalHeaderItem(col).setTextAlignment(Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter)
        self.table.horizontalHeader().setSectionResizeMode(3,QHeaderView.ResizeMode.Stretch)
        self._role_editors=[]
        for row,(ident,name) in enumerate(zip(self._ids,self._names,strict=True)):
            item=QTableWidgetItem(name); item.setToolTip(name); item.setData(Qt.ItemDataRole.UserRole,ident)
            self.table.setItem(row,0,item)
            role=decision_combo(self.table,(('', 'other'),('Signal','signal'),('Estimate AF','dark')))
            role.setObjectName(f'reference-band-role-{row}')
            role.setAccessibleName(f'Role for band {row+1}: {name}')
            role.setToolTip('Choose Signal for the fluorophore band. Choose Estimate AF where fluorophore signal is negligible. Leave other bands blank.')
            role.setStyleSheet(role.styleSheet() +
                'QComboBox[bandRole="signal"] { background: #fff1df; }'
                'QComboBox[bandRole="dark"] { background: #e8f4f1; }')
            self._role_editors.append(role)
            self.table.setCellWidget(row,3,role)
            role.currentIndexChanged.connect(lambda _value,index=row:self._role_changed(index))
        self.table.setMouseTracking(True)
        self.table.cellEntered.connect(lambda row,_column:self._inspect_row(row))
        self.table.viewport().installEventFilter(self)
        choices.addWidget(self.table)
        self.canvas.setToolTip("Select a band to find it in the table.")
        self.table.setFixedHeight(min(193,46+36*len(self._ids)))
        layout.addWidget(self._band_choices)
        self._inspection_line=None
        self._footer=QWidget(self); footer=QVBoxLayout(self._footer)
        footer.setContentsMargins(0,0,0,0); footer.setSpacing(2)
        footer.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self._footer)
        self._disclosures=QVBoxLayout(); self._disclosures.setSpacing(8)
        footer.addLayout(self._disclosures)
        toggle=QToolButton(self); toggle.setText('Image sources and correction'); toggle.setCheckable(True)
        toggle.setObjectName('reference-band-sources')
        configure_disclosure(toggle); self._disclosures.addWidget(toggle,alignment=Qt.AlignmentFlag.AlignLeft)
        self.sources=text_label('',self,supporting=True); self.sources.hide()
        toggle.toggled.connect(self.sources.setVisible); self._disclosures.addWidget(self.sources)
        self._review_actions=QWidget(self); actions=QVBoxLayout(self._review_actions)
        actions.setContentsMargins(0,0,0,0); actions.setSpacing(12)
        actions.setAlignment(Qt.AlignmentFlag.AlignTop); self._review_actions.hide()
        footer.addWidget(self._review_actions)
        self.region_selector.currentIndexChanged.connect(self._draw)
        self.table.currentCellChanged.connect(lambda *_:self._inspect_row())
        self.canvas.mpl_connect('button_press_event',self._inspect_plot)
        self.canvas.mpl_connect('motion_notify_event',self._hover)
        self.canvas.mpl_connect('figure_leave_event',lambda _:QToolTip.hideText())
        self._draw()

    def add_review_actions(self,settings,navigation):
        """Keep existing form settings and navigation in the shared footer."""
        if settings is not None:
            settings._advanced_toggle.setText('Advanced extraction settings')
            settings._advanced_toggle.setToolTip('Review the signal checks used for fluorophore extraction.')
            settings.layout().setContentsMargins(0,0,0,0)
            settings.layout().setSpacing(4)
            settings.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Maximum)
            self._footer.layout().addWidget(settings)
            self._footer.layout().removeWidget(self._review_actions)
            self._footer.layout().addWidget(self._review_actions)
        self._review_actions.layout().setContentsMargins(0,12,0,0)
        self._review_actions.layout().addLayout(navigation)
        self._review_actions.show()

    def _role_changed(self,index):
        """A band has one role; moving signal releases the previous signal band."""
        if self._role_editors[index].currentData()=='signal':
            for row,editor in enumerate(self._role_editors):
                if row!=index and editor.currentData()=='signal':
                    blocker=QSignalBlocker(editor)
                    editor.setCurrentIndex(editor.findData('other'))
                    del blocker
        self.table.setCurrentCell(index,0)
        self._draw()

    def values(self):
        roles=tuple(editor.currentData() for editor in self._role_editors)
        return {'signal_measurement_id':next((ident for ident,role in zip(self._ids,roles,strict=True) if role=='signal'),None),
                'dark_measurement_ids':tuple(ident for ident,role in zip(self._ids,roles,strict=True) if role=='dark')}

    def set_values(self,values):
        current=self.values()
        signal=values.get('signal_measurement_id',current['signal_measurement_id'])
        dark=set(values.get('dark_measurement_ids',current['dark_measurement_ids']) or ())
        for ident,editor in zip(self._ids,self._role_editors,strict=True):
            blocker=QSignalBlocker(editor)
            role='signal' if ident==signal else 'dark' if ident in dark else 'other'
            editor.setCurrentIndex(editor.findData(role))
            del blocker
        self._draw()
        selected_rows=[i for i,editor in enumerate(self._role_editors) if editor.currentData()!='other']
        if selected_rows:
            item=self.table.item(min(selected_rows),0)
            QTimer.singleShot(0,lambda:self.table.scrollToItem(item,self.table.ScrollHint.PositionAtTop))

    def _draw(self,*_):
        if not hasattr(self,'canvas'):return
        region=self._regions[max(0,self.region_selector.currentIndex())]
        self.sources.setText(f"Stained image: {region['positive_filename']}\n"
            f"{region['control_label']}: {region['control_filename']}\n"
            "The stained spectrum shows intensities after background subtraction. The AF fit scales the control spectrum to each stained region.")
        visible={label:line.get_visible() for line,label,*_ in getattr(self,'_hover_profiles',())}
        self._axes.clear(); self._hover_profiles=[]
        self._inspection_line=None
        x,groups,unit=spectral_axis(self._names,wavelengths=self._wavelengths,wavelength_unit=self._wavelength_unit,optical_settings=self._optical_settings)
        for key,label,color in (('positive_values','Stained region','#157fa3'),('control_values','AF control spectrum','#a16a37')):
            values=region[key]
            line=plot_spectral_profile(self._axes,x,values,groups,ordinal=True,label=label,color=color,linewidth=1.7)
            line.set_visible(visible.get(label,True))
            self._hover_profiles.append((line,label,values,values))
        for row,(positive,control) in enumerate(zip(region['positive_values'],region['control_values'],strict=True)):
            for col,value in ((1,positive),(2,control)):
                item=QTableWidgetItem(f'{value:.6g}'); item.setToolTip(repr(value))
                item.setTextAlignment(Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(row,col,item)
        self.legend.set_lines(self._axes.lines)
        roles=tuple(editor.currentData() for editor in self._role_editors)
        for index,role in enumerate(roles):
            editor=self._role_editors[index]
            editor.setProperty('bandRole',role)
            editor.style().unpolish(editor); editor.style().polish(editor)
            color={'signal':'#fff0de','dark':'#e5f3f1'}.get(role)
            for column in range(3):
                self.table.item(index,column).setBackground(QBrush(QColor(color)) if color else QBrush())
        for role, consecutive in groupby(enumerate(roles), key=lambda item: item[1]):
            if role == "dark":
                indices = [index for index, _ in consecutive]
                self._axes.axvspan(indices[0] + .5, indices[-1] + 1.5, color="#39998c", alpha=.14, linewidth=0, zorder=0)
        selected=next((i for i,role in enumerate(roles) if role=='signal'),-1)
        count=roles.count('dark')
        self._selection_summary.setText(f"{'1 signal' if selected>=0 else 'Choose signal'} · {count} AF-estimation")
        if selected >= 0:
            self._axes.axvspan(selected + .5, selected + 1.5, color='#dfa54b', alpha=.22, linewidth=0, zorder=0)
        label_spectral_axis(self._axes,self._names,x,groups,unit,ordinal=True)
        self._axes.set_ylabel(INTENSITY_UNITS[self._mode].replace(' intensity', '\nintensity'))
        self._axes.set_ylim(bottom=0)
        self._axes.set_facecolor(self.palette().window().color().name()); self._axes.spines[['top','right']].set_visible(False)
        self._axes.set_axisbelow(True); self._axes.grid(axis='y',color='#d5dfe5',linewidth=.7,linestyle=(0,(3,4)))
        self._inspect_row()

    def _inspect_plot(self,event):
        """Locate an acquired band without assigning it a scientific role."""
        if event.button!=1 or event.inaxes is not self._axes or event.xdata is None:return
        row=int(np.floor(event.xdata+.5))-1
        if not 0<=row<len(self._ids):return
        self.table.setCurrentCell(row,0)
        self.table.scrollToItem(self.table.item(row,0),self.table.ScrollHint.PositionAtCenter)
        self.table.setFocus(Qt.FocusReason.MouseFocusReason)

    def eventFilter(self,watched,event):
        if watched is self.table.viewport() and event.type()==QEvent.Type.Leave:
            self._inspect_row()
        return super().eventFilter(watched,event)

    def _inspect_row(self,row=None):
        row=self.table.currentRow() if row is None else row
        if self._inspection_line is not None:
            self._inspection_line.remove(); self._inspection_line=None
        if row>=0:
            self._inspection_line=self._axes.axvline(row+1,color='#456c83',linewidth=1.1,alpha=.75)
            self._inspection_hint.setText(f'Acquired band {row+1} · {self._names[row]}')
        else:
            self._inspection_hint.clear()
        self.canvas.draw_idle()

    def _hover(self,event):
        show_spectral_hover(self.canvas,self._axes,event,self._names,np.arange(1,len(self._names)+1),self._hover_profiles)
