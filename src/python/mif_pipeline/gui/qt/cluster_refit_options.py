"""Choose a selected-cell refit while retaining the parent measurement geometry."""
from dataclasses import replace
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget,QFrame,QVBoxLayout,QHBoxLayout,QGridLayout,QLabel,QComboBox,QTableWidget,QTableWidgetItem,
    QHeaderView,QSpinBox,QDoubleSpinBox,QLineEdit,QToolButton,QProgressBar,QSizePolicy)

from mif_pipeline.orchestration.batch_phenotyping.refinement_settings import ClusterRefitSettings
from .workbench import style_scientific_table
from .researcher_widgets import configure_disclosure


class ClusterRefitOptionsEditor(QWidget):
    def __init__(self,options,parent=None):
        super().__init__(parent)
        self.options=options; self.saved=options.saved_clustering
        initial=options.settings.cluster_refit
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0);layout.setSpacing(12)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.card=QFrame(self);self.card.setProperty('stageFormPage',True)
        card=QVBoxLayout(self.card);card.setContentsMargins(20,18,20,18);card.setSpacing(20)
        card.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.card)
        action_row=QHBoxLayout();action_row.setSpacing(12)
        operation_label=QLabel('Action',self.card);operation_label.setProperty('stageFieldCaption',True)
        action_row.addWidget(operation_label)
        self.operation=QComboBox(self)
        self.operation.addItem('Recluster selected groups','refit')
        if self.saved['can_restore_parent']:
            self.operation.addItem('Restore previous groups','restore')
        operation_label.setBuddy(self.operation)
        operation_label.setVisible(self.operation.count()>1)
        action_row.addWidget(self.operation,1);card.addLayout(action_row)
        self.operation.setVisible(self.operation.count()>1)
        self.note=QLabel('The refit retains the original measurements, scaling and weights.',self)
        self.note.setWordWrap(True);self.note.setProperty('fieldHelp',True)
        self.editor=QWidget(self);self.body=QGridLayout(self.editor)
        self.body.setContentsMargins(0,0,0,0);self.body.setHorizontalSpacing(32);self.body.setVerticalSpacing(24)
        self.body.setAlignment(Qt.AlignmentFlag.AlignTop)
        card.addWidget(self.editor)
        self.selection_panel=QFrame(self);self.selection_panel.setObjectName('refit-selection-scope')
        selection=QVBoxLayout(self.selection_panel);selection.setContentsMargins(0,0,0,0);selection.setSpacing(8)
        selection.setAlignment(Qt.AlignmentFlag.AlignTop)
        selection_heading=QHBoxLayout();selection_heading.setContentsMargins(0,0,0,0)
        heading=QLabel('Selected groups',self);heading.setProperty('panelHeading',True)
        selection_heading.addWidget(heading,1)
        self.change_groups=QToolButton(self);self.change_groups.setText('Change groups');self.change_groups.setCheckable(True)
        configure_disclosure(self.change_groups);selection_heading.addWidget(self.change_groups)
        selection.addLayout(selection_heading)
        self.summary=QLabel(self);self.summary.setWordWrap(True)
        self.summary.setProperty('stageFieldCaption',True);selection.addWidget(self.summary)
        self.scope_bar=QProgressBar(self);self.scope_bar.setRange(0,1000);self.scope_bar.setTextVisible(False)
        self.scope_bar.setFixedHeight(7);self.scope_bar.setAccessibleName('Proportion of cells selected for reclustering')
        self.scope_bar.setStyleSheet('QProgressBar {border:0;border-radius:3px;background:#dce6e6;} QProgressBar::chunk {border-radius:3px;background:#4a8b94;}')
        selection.addWidget(self.scope_bar)
        self.scope_note=QLabel(self);self.scope_note.setWordWrap(True)
        self.scope_note.setProperty('fieldHelp',True);selection.addWidget(self.scope_note)
        self.selected_table=QTableWidget(0,2,self)
        self.selected_table.setHorizontalHeaderLabels(('Selected group','Cells'))
        self.selected_table.verticalHeader().hide();style_scientific_table(self.selected_table)
        self.selected_table.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch)
        self.selected_table.horizontalHeader().setSectionResizeMode(1,QHeaderView.ResizeMode.ResizeToContents)
        self.selected_table.horizontalHeader().setFixedHeight(32)
        self.selected_table.verticalHeader().setDefaultSectionSize(32)
        self.selected_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.selected_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        selection.addWidget(self.selected_table)
        self.clusters=QTableWidget(len(self.saved['cluster_counts']),3,self)
        self.clusters.setObjectName('refit-cluster-selection')
        self.clusters.setHorizontalHeaderLabels(('Select','Group','Cells'))
        self.clusters.verticalHeader().hide();style_scientific_table(self.clusters)
        self.clusters.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.ResizeToContents)
        self.clusters.horizontalHeader().setSectionResizeMode(1,QHeaderView.ResizeMode.Stretch)
        self.clusters.horizontalHeader().setSectionResizeMode(2,QHeaderView.ResizeMode.ResizeToContents)
        self.clusters.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for row,(identity,count) in enumerate(self.saved['cluster_counts'].items()):
            check=QTableWidgetItem();check.setFlags(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(Qt.CheckState.Checked if identity in initial.cluster_ids else Qt.CheckState.Unchecked)
            self.clusters.setItem(row,0,check);self.clusters.setItem(row,1,QTableWidgetItem(identity))
            self.clusters.setItem(row,2,QTableWidgetItem(f'{count:,}'))
        self.clusters.setFixedHeight(self.clusters.horizontalHeader().height()+min(7,self.clusters.rowCount())*38+8)
        selection.addWidget(self.clusters)
        self.change_groups.toggled.connect(self._selection_visibility)
        self.change_groups.setChecked(not bool(initial.cluster_ids));self.clusters.setVisible(not bool(initial.cluster_ids))
        self.parameters_card=QWidget(self)
        parameters=QVBoxLayout(self.parameters_card);parameters.setContentsMargins(0,0,0,0);parameters.setSpacing(12)
        parameters.setAlignment(Qt.AlignmentFlag.AlignTop)
        heading=QLabel('Method and settings',self);heading.setProperty('panelHeading',True)
        heading.setMinimumHeight(self.change_groups.sizeHint().height());parameters.addWidget(heading)
        parameters.addWidget(self.note)
        method_heading=QLabel('Clustering method',self);method_heading.setProperty('stageFieldCaption',True);parameters.addWidget(method_heading)
        self.method=QComboBox(self);self.method.addItem('PhenoGraph','phenograph');self.method.addItem('FlowSOM','flowsom')
        self.method.setCurrentIndex(self.method.findData(initial.method));parameters.addWidget(self.method)
        self.fit_guidance=QLabel(self);self.fit_guidance.setWordWrap(True)
        self.fit_guidance.setProperty('fieldHelp',True);parameters.addWidget(self.fit_guidance)
        self.core_grid=QGridLayout();self.core_grid.setHorizontalSpacing(20);self.core_grid.setVerticalSpacing(12)
        parameters.addLayout(self.core_grid)
        for section in (self.selection_panel,self.parameters_card):
            section.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Maximum)
        self._boxes={};self._help={}

        def setting(name,title,explanation,widget):
            box=QWidget(self);rows=QVBoxLayout(box);rows.setContentsMargins(0,0,0,0);rows.setSpacing(4)
            label=QLabel(title,box);label.setProperty('stageFieldCaption',True);label.setBuddy(widget);rows.addWidget(label)
            widget.setFixedWidth(160);rows.addWidget(widget,alignment=Qt.AlignmentFlag.AlignLeft)
            help=QLabel(explanation,box);help.setProperty('fieldHelp',True);help.setWordWrap(True);rows.addWidget(help)
            rows.addStretch(1)
            widget.setObjectName('refit-'+name)
            self._boxes[name]=box;self._help[name]=help;return widget

        def integer(low,high,value):
            widget=QSpinBox(self);widget.setRange(low,high);widget.setValue(value);return widget

        self.k=setting('k','Nearest neighbours (k)','Neighbours are drawn from the selected cells.',integer(1,64,initial.k))
        resolution=QDoubleSpinBox(self);resolution.setDecimals(4);resolution.setRange(.0001,1e6);resolution.setValue(initial.resolution)
        self.resolution=setting('resolution','Leiden resolution','Higher values tend to produce finer groups; lower values favour coarser groups.',resolution)
        flow=dict(initial.method_settings) if initial.method=='flowsom' else dict(grid_size=10,n_clusters=20,iterations=10)
        self.groups=setting('groups','Final clusters','Map nodes are combined into this many final groups (metaclusters). More groups allow a finer division; fewer favour a coarser division.',integer(2,900,flow['n_clusters']))
        self.advanced=QToolButton(self);self.advanced.setText('Advanced settings');self.advanced.setCheckable(True)
        configure_disclosure(self.advanced);parameters.addWidget(self.advanced,alignment=Qt.AlignmentFlag.AlignLeft)
        self.advanced_body=QWidget(self);advanced=QVBoxLayout(self.advanced_body);advanced.setContentsMargins(0,0,0,0);advanced.setSpacing(14)
        self.advanced_grid=QGridLayout();self.advanced_grid.setHorizontalSpacing(20);self.advanced_grid.setVerticalSpacing(12)
        advanced.addLayout(self.advanced_grid);parameters.addWidget(self.advanced_body)
        self.grid=setting('grid','Map width and height','A 10 × 10 map has 100 nodes. Each node learns one value for every selected measurement.',integer(2,30,flow['grid_size']))
        self.iterations=setting('iterations','Training iterations','Each iteration draws as many training examples as there are selected cells.',integer(1,100,flow['iterations']))
        seed=QLineEdit(str(initial.seed),self)
        self.seed=setting('seed','Random seed','Keep the seed and settings to reproduce this selected-cell fit.',seed)
        self.transform_toggle=QToolButton(self);self.transform_toggle.setText('Original measurements and scaling')
        self.transform_toggle.setCheckable(True);configure_disclosure(self.transform_toggle)
        advanced.addWidget(self.transform_toggle,alignment=Qt.AlignmentFlag.AlignLeft)
        names=self.saved['feature_measurements']
        self.transform=QTableWidget(len(names),4,self)
        self.transform.setHorizontalHeaderLabels(('Measurement','Center','Divisor','Weight'))
        self.transform.verticalHeader().hide();style_scientific_table(self.transform)
        self.transform.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.transform.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for i,name in enumerate(names):
            for j,value in enumerate((name,self.saved['robust_centers'][i],self.saved['robust_scales'][i],self.saved['feature_weights'][i])):
                self.transform.setItem(i,j,QTableWidgetItem(str(value)))
        self.transform.setFixedHeight(self.transform.horizontalHeader().height()+min(6,len(names))*38+8)
        advanced.addWidget(self.transform);self.transform.hide()
        self.restore_note=QLabel('Restore the exact groups saved before the latest refit, then review their phenotype names.',self)
        self.restore_note.setWordWrap(True);self.restore_note.setProperty('fieldHelp',True);self.restore_note.hide();card.addWidget(self.restore_note)
        self.clusters.cellChanged.connect(self._update_selection)
        self.grid.valueChanged.connect(self._update_map_help)
        self.groups.valueChanged.connect(self._update_guidance)
        self.method.currentIndexChanged.connect(self._show_settings)
        self.advanced.toggled.connect(self._show_settings)
        self.operation.currentIndexChanged.connect(self._show_settings)
        self.transform_toggle.toggled.connect(self.transform.setVisible)
        self._update_selection();self._update_map_help();self._show_settings()

    def _selected_clusters(self):
        return tuple(sorted(self.clusters.item(row,1).text() for row in range(self.clusters.rowCount())
            if self.clusters.item(row,0).checkState()==Qt.CheckState.Checked))

    def _update_selection(self):
        selected=self._selected_clusters()
        count=sum(self.saved['cluster_counts'][key] for key in selected)
        total=sum(self.saved['cluster_counts'].values())
        self.summary.setText(f'{count:,} cells selected · {len(selected):,} group'+('' if len(selected)==1 else 's'))
        self.scope_bar.setValue(round(1000*count/total) if total else 0)
        self.scope_bar.setAccessibleDescription(f'{count:,} of {total:,} cells selected.')
        self.scope_note.setText(f'{100*count/total:.1f}% of clustered cells' if selected and total else 'Choose the groups to recluster.')
        self.selected_table.setRowCount(len(selected))
        for row,key in enumerate(selected):
            self.selected_table.setItem(row,0,QTableWidgetItem(key))
            item=QTableWidgetItem(f'{self.saved["cluster_counts"][key]:,}')
            item.setTextAlignment(Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter)
            self.selected_table.setItem(row,1,item)
        self.selected_table.setFixedHeight(32+min(4,len(selected))*32+12)
        self._selection_visibility();self._update_guidance()

    def _selection_visibility(self):
        expanded=self.change_groups.isChecked()
        self.clusters.setVisible(expanded)
        self.selected_table.setVisible(not expanded and bool(self._selected_clusters()))

    def _update_guidance(self):
        count=len(self._selected_clusters())
        self.fit_guidance.setText('Selected groups are fitted together, so new groups can cross their previous boundaries. '
            'To combine only phenotype names and counts, use Merge in reports.')
        self.fit_guidance.setVisible(count>1)

    def _show_settings(self):
        restore=self.operation.currentData()=='restore';self.editor.setVisible(not restore);self.restore_note.setVisible(restore)
        self.note.setVisible(not restore)
        flow=self.method.currentData()=='flowsom';advanced=self.advanced.isChecked()
        self.advanced_body.setVisible(advanced)
        for name,box in self._boxes.items():
            box.setVisible((not flow if name in {'k','resolution'} else flow if name=='groups'
                            else advanced and (flow or name=='seed')))
        self._arrange_parameters()
        self._update_guidance()

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,'advanced_grid'):self._arrange_parameters()

    def _arrange_parameters(self):
        columns=self.width()>=940
        wide=self.width()>=650;flow=self.method.currentData()=='flowsom'
        arrangement=(columns,wide,flow)
        if getattr(self,'_parameter_arrangement',None)==arrangement:return
        self._parameter_arrangement=arrangement
        for section in (self.selection_panel,self.parameters_card):self.body.removeWidget(section)
        self.body.addWidget(self.selection_panel,0,0,Qt.AlignmentFlag.AlignTop)
        self.body.addWidget(self.parameters_card,0 if columns else 1,1 if columns else 0,Qt.AlignmentFlag.AlignTop)
        self.body.setColumnStretch(0,2 if columns else 1);self.body.setColumnStretch(1,3 if columns else 0)
        for grid in (self.core_grid,self.advanced_grid):
            for box in self._boxes.values():grid.removeWidget(box)
            grid.setColumnStretch(0,1)
            grid.setColumnStretch(1,1 if grid is self.core_grid and wide else 0)
        if flow:self.core_grid.addWidget(self._boxes['groups'],0,0,1,2 if wide else 1)
        else:
            for i,name in enumerate(('k','resolution')):self.core_grid.addWidget(self._boxes[name],0 if wide else i,i if wide else 0)
        if flow:
            for i,name in enumerate(('grid','iterations')):self.advanced_grid.addWidget(self._boxes[name],i,0)
        self.advanced_grid.addWidget(self._boxes['seed'],2 if flow else 0,0)

    def _update_map_help(self):
        side=self.grid.value();nodes=side*side
        self._help['grid'].setText(f'A {side} × {side} map has {nodes:,} nodes and requires at least {nodes:,} selected cells. Each node learns one value for every selected measurement.')

    def selected_settings(self):
        restore=self.operation.currentData()=='restore'
        if restore:
            return replace(self.options.settings,cluster_refit=replace(
                self.options.settings.cluster_refit,cluster_ids=(),restore_parent=True))
        ids=() if restore else self._selected_clusters()
        if not restore and not ids:
            raise ValueError('Select groups to recluster.')
        try:seed=int(self.seed.text())
        except ValueError:raise ValueError('Enter a whole-number random seed.') from None
        method=self.method.currentData()
        count=sum(self.saved['cluster_counts'][key] for key in ids)
        if method=='flowsom' and count<self.grid.value()**2:
            raise ValueError(f'This map needs {self.grid.value()**2:,} cells; {count:,} are selected. Choose a smaller map or include more groups.')
        settings=ClusterRefitSettings(ids,method,self.k.value(),self.resolution.value(),seed,
            dict(grid_size=self.grid.value(),n_clusters=self.groups.value(),iterations=self.iterations.value()) if method=='flowsom' else {},restore)
        return replace(self.options.settings,cluster_refit=settings)
