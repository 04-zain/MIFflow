"""Link cluster interpretation, raw marker profiles and exact image examples."""
from collections import Counter
import json

from PySide6.QtCore import QAbstractTableModel,QModelIndex,QItemSelectionModel,QSignalBlocker,QEvent,Qt,Signal
from PySide6.QtGui import QColor,QPen
from PySide6.QtWidgets import (QWidget,QFrame,QVBoxLayout,QHBoxLayout,QLabel,QComboBox,
    QTableView,QHeaderView,QStyledItemDelegate,QStyle,QBoxLayout,QSizePolicy,QLineEdit,QMenu,
    QWidgetAction,QCheckBox,QApplication,QGridLayout,QToolButton)

from .workbench import style_scientific_table
from .classifier_review_data import review_table,set_table,csv_bytes
from .marker_cell_examples import MarkerCellExamples
from .cluster_field_review import ClusterFieldReview


class _Profiles(QAbstractTableModel):
    def __init__(self,ids,rows,parent):
        super().__init__(parent)
        self.ids=tuple(ids);self.names=tuple(dict.fromkeys(r['measurement'] for r in rows))
        self.rows={(r['cluster_id'],r['measurement']):r for r in rows}
        self.statistic='raw_median';self.excluded=set();self._ranges()

    def _ranges(self):
        self.ranges={}
        for name in self.names:
            values=[float(self.rows[key][self.statistic]) for key in self.rows if key[1]==name]
            self.ranges[name]=(min(values),max(values))

    def rowCount(self,parent=QModelIndex()):return 0 if parent.isValid() else len(self.ids)
    def columnCount(self,parent=QModelIndex()):return 0 if parent.isValid() else len(self.names)

    def headerData(self,section,orientation,role=Qt.ItemDataRole.DisplayRole):
        if orientation==Qt.Orientation.Vertical and role==Qt.ItemDataRole.DisplayRole:
            return self.ids[section]
        if orientation==Qt.Orientation.Vertical and role==Qt.ItemDataRole.ToolTipRole:
            return self.ids[section]+(' · Excluded from phenotyping' if self.ids[section] in self.excluded else '')
        if orientation==Qt.Orientation.Horizontal:
            name=self.names[section]
            if role==Qt.ItemDataRole.DisplayRole:return name.replace(': ','\n')
            if role==Qt.ItemDataRole.ToolTipRole:return name
        return None

    def data(self,index,role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():return None
        name=self.names[index.column()];row=self.rows.get((self.ids[index.row()],name))
        if row is None:return '—' if role==Qt.ItemDataRole.DisplayRole else None
        value=float(row[self.statistic]);low,high=self.ranges[name]
        fraction=(value-low)/(high-low) if high>low else .5
        if role==Qt.ItemDataRole.DisplayRole:return f'{value:.3g}'
        if role==Qt.ItemDataRole.TextAlignmentRole:return Qt.AlignmentFlag.AlignCenter
        if role==Qt.ItemDataRole.BackgroundRole:
            if self.ids[index.row()] in self.excluded:return QColor('#e9eeee')
            return QColor.fromRgbF(.94-.87*fraction,.97-.55*fraction,.96-.50*fraction)
        if role==Qt.ItemDataRole.ForegroundRole:return QColor('#778b8f' if self.ids[index.row()] in self.excluded else 'white' if fraction>.65 else '#173d47')
        if role==Qt.ItemDataRole.ToolTipRole:
            units=row['units'] or 'saved units'
            status=' · Excluded from phenotyping' if self.ids[index.row()] in self.excluded else ''
            return f"{self.ids[index.row()]}{status} · {name}\nMedian: {row['raw_median']!r} {units}\nMean: {row['raw_mean']!r} {units}\nColour range for this measurement: {low!r} to {high!r}"
        return None

    def set_statistic(self,name):
        self.beginResetModel();self.statistic=name;self._ranges();self.endResetModel()


class _ProfileDelegate(QStyledItemDelegate):
    def paint(self,painter,option,index):
        selected=bool(option.state & QStyle.StateFlag.State_Selected)
        painter.save()
        background=index.data(Qt.ItemDataRole.BackgroundRole)
        painter.fillRect(option.rect,background if background is not None else QColor('#edf1f1'))
        painter.setPen(index.data(Qt.ItemDataRole.ForegroundRole) or QColor('#173d47'))
        painter.setFont(option.font)
        painter.drawText(option.rect,Qt.AlignmentFlag.AlignCenter,str(index.data() or ''))
        if selected:
            painter.setPen(QPen(QColor('#d38b24'),2))
            painter.drawLine(option.rect.topLeft(),option.rect.topRight())
            painter.drawLine(option.rect.bottomLeft(),option.rect.bottomRight())
        painter.restore()


class _ProfileTable(QTableView):
    def selectionCommand(self,index,event=None):
        if (event is not None and event.type() in (QEvent.Type.MouseButtonPress,QEvent.Type.MouseButtonRelease)
                and len(self.selectionModel().selectedRows())>1
                and not event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            return QItemSelectionModel.SelectionFlag.NoUpdate
        return super().selectionCommand(index,event)


class ClusterReview(QWidget):
    image_requested=Signal()
    cluster_selected=Signal(str)
    page_changed=Signal()
    refit_requested=Signal(tuple)
    consumed_tables=frozenset({'marker_profiles','entry_counts','section_counts'})

    def __init__(self,form,decisions,model,parent=None):
        super().__init__(parent)
        self.decisions=decisions;self.model=model;self._pending=None;self._measurement=None;self._point_cell_id=None
        self.model_view=None
        self._syncing=False
        self._tables={t.table_id:t for t in form.tables if t.table_id in self.consumed_tables}
        self._profiles=[dict(zip(self._tables['marker_profiles'].columns,row.values)) for row in self._tables['marker_profiles'].rows]
        self._entries=[]
        for row in self._tables['entry_counts'].rows:
            values=dict(zip(self._tables['entry_counts'].columns,row.values));values['project_entry_id']=json.loads(row.row_id)[2]
            self._entries.append(values)
        self._sections=[dict(zip(self._tables['section_counts'].columns,row.values),project_entry_id=json.loads(row.row_id)[2])
                        for row in self._tables['section_counts'].rows]
        self._image_totals=Counter()
        for row in self._sections:self._image_totals[row['sample_id'],row['project_entry_id'],row['slide_id']]+=row['cell_count']
        self.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Maximum)
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0);layout.setSpacing(14)
        from .training_fit_summary import _ReviewPages
        from .researcher_widgets import ContentSizedButton
        self.pages=_ReviewPages(self)
        self.pages.setObjectName('cluster-review-pages');self.pages.setDocumentMode(True)
        bar=self.pages.tabBar();bar.setObjectName('stage-page-tabs');bar.setExpanding(False)
        bar.setDrawBase(False);bar.setUsesScrollButtons(True);bar.setElideMode(Qt.TextElideMode.ElideNone)
        self.compare_page=QFrame(self.pages);self.assignment_page=QFrame(self.pages)
        self.compare_page.setObjectName('cluster-compare-page')
        self.compare_page.setStyleSheet('QFrame#cluster-compare-page { background: #f3f5f5; }')
        self.compare_layout=QVBoxLayout(self.compare_page);self.assignment_layout=QVBoxLayout(self.assignment_page)
        for page,body in ((self.compare_page,self.compare_layout),(self.assignment_page,self.assignment_layout)):
            page.setProperty('stageFormPage',True)
            body.setContentsMargins(18,18,18,18);body.setSpacing(12);body.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.pages.addTab(self.compare_page,'01  Interpret groups')
        self.pages.addTab(self.assignment_page,'02  Review phenotypes')
        layout.addWidget(self.pages)
        # The form's existing explanation belongs with the editable decisions.
        self.profile_layout=self.assignment_layout
        self.profile_page=self.compare_page;self.cell_page=self.compare_page
        self.reporting_page=self.assignment_page
        self.population=QLabel(f'{len(model._ids):,} clusters · {sum(model._counts.values()):,} cells · '
                              f'{len(self._image_totals):,} image'+('s' if len(self._image_totals)!=1 else ''),self)
        self.population.setProperty('fieldHelp',True);self.population.setWordWrap(True)
        self._comparison_mode=None
        self._available_views=set()
        self.cluster_choice=ContentSizedButton('Choose groups',self);self.cluster_choice.setAccessibleName('Choose groups')
        self.cluster_choice.setStyleSheet('min-width:164px;')
        self.cluster_choice.clicked.connect(self._select_groups)
        self.cluster_choice.setSizePolicy(QSizePolicy.Policy.Preferred,QSizePolicy.Policy.Fixed)
        self.editing=QFrame(self);self.editing.setProperty('stageInfoPanel',True)
        self.editing_layout=QVBoxLayout(self.editing);self.editing_layout.setContentsMargins(12,12,12,10)
        self.editing_layout.setSpacing(8)
        self.selection_row=QBoxLayout(QBoxLayout.Direction.LeftToRight)
        selection_row=self.selection_row
        self.selection_summary=QLabel(self);self.selection_summary.setWordWrap(True)
        self.selection_summary.setProperty('fieldHelp',True);selection_row.addWidget(self.selection_summary,1)
        self.assignment_tools=QGridLayout();self.assignment_tools.setSpacing(8)
        self.name=QLineEdit(self);self.name.setPlaceholderText('Phenotype name')
        self.name.setAccessibleName('Phenotype name for selected groups')
        self.name.textChanged.connect(self._name_preview)
        self.merge=ContentSizedButton('Apply name',self);self.merge.clicked.connect(self._name_selected)
        self.merge.setProperty('actionRole','primary')
        self.group_actions=ContentSizedButton('Group actions',self)
        self.group_menu=QMenu(self.group_actions)
        self.exclude_action=self.group_menu.addAction('Exclude from phenotyping',lambda:self._include_selected(False))
        self.include_action=self.group_menu.addAction('Include in phenotyping',lambda:self._include_selected(True))
        self.clear_name_action=self.group_menu.addAction('Clear phenotype name',self._clear_selected_names)
        self.group_menu.addSeparator()
        self.refit=self.group_menu.addAction('Recluster selected groups…',self._request_refit)
        self.refit.setToolTip('Recluster the checked cells to look for finer groups, then review the result.')
        self.group_actions.setMenu(self.group_menu)
        self.editing_layout.addLayout(self.assignment_tools)
        self.editing_layout.addLayout(selection_row)
        self.group_focus=QLabel(self);self.group_focus.setWordWrap(True)
        self.group_focus.setProperty('fieldHelp',True);self.group_focus.linkActivated.connect(self.select_cluster)
        self.group_focus.hide();self.editing_layout.addWidget(self.group_focus)
        self.name_preview=QLabel(self);self.name_preview.setWordWrap(True);self.name_preview.setProperty('fieldHelp',True)
        self.name_preview.hide();self.editing_layout.addWidget(self.name_preview)
        self.compare_layout.addWidget(self.editing)
        self.workspace=QBoxLayout(QBoxLayout.Direction.LeftToRight);self.workspace.setSpacing(22)
        self.compare_layout.addLayout(self.workspace)
        self.comparison_panel=QWidget(self);self.comparison_layout=QVBoxLayout(self.comparison_panel)
        self.comparison_panel.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Maximum)
        self.comparison_layout.setContentsMargins(0,0,0,0);self.comparison_layout.setSpacing(10)
        self.comparison_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.tissue_panel=QWidget(self);self.cell_layout=QVBoxLayout(self.tissue_panel)
        self.cell_layout.setContentsMargins(0,0,0,0);self.cell_layout.setSpacing(10)
        self.cell_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.measured_view=QWidget(self);measured=QVBoxLayout(self.measured_view)
        self.measured_view.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Maximum)
        measured.setContentsMargins(0,0,0,0);measured.setSpacing(10)
        measured.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.statistic=QComboBox(self);self.statistic.addItem('Median across cells','raw_median');self.statistic.addItem('Mean across cells','raw_mean')
        profile_toolbar=QHBoxLayout();profile_toolbar.setSpacing(8)
        heading=QLabel('Measured profiles',self);heading.setStyleSheet('font-weight:600;')
        profile_toolbar.addWidget(heading,1);profile_toolbar.addWidget(self.statistic)
        self.profile_toolbar=QWidget(self);self.profile_toolbar.setLayout(profile_toolbar)
        profile_toolbar.setContentsMargins(0,0,0,0);self.profile_toolbar.setFixedHeight(36)
        measured.addWidget(self.profile_toolbar)
        legend=QLabel('Group means or medians · each measurement column has its own colour scale',self)
        legend.setProperty('fieldHelp',True);legend.setWordWrap(True)
        self.profiles=_ProfileTable(self);self.profile_model=_Profiles(model._ids,self._profiles,self.profiles)
        self.profiles.setModel(self.profile_model);style_scientific_table(self.profiles)
        self.profiles.verticalHeader().show()
        self.profiles.verticalHeader().setMinimumWidth(112)
        self.profiles.setItemDelegate(_ProfileDelegate(self.profiles))
        self.profiles.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.profiles.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
        self.profiles.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.profiles.setMouseTracking(True);self.profiles.setShowGrid(False)
        self.profiles.horizontalHeader().setDefaultSectionSize(76)
        self.profiles.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.profiles.ensurePolished()
        metrics=self.profiles.fontMetrics()
        numeric_width=max((metrics.horizontalAdvance(f'{float(row[key]):.3g}')
            for row in self._profiles for key in ('raw_mean','raw_median')),default=0)
        header_width=max((metrics.horizontalAdvance(line)
            for name in self.profile_model.names for line in name.replace(': ','\n').splitlines()),default=0)
        styled_header_width=max((self.profiles.horizontalHeader().sectionSizeHint(column)
            for column in range(self.profile_model.columnCount())),default=0)
        self.profiles.horizontalHeader().setMinimumSectionSize(max(64,numeric_width+16,header_width+16,styled_header_width))
        self.profiles.horizontalHeader().setFixedHeight(70)
        self.profiles.verticalHeader().setDefaultSectionSize(32)
        self.profiles.setFixedHeight(500)
        measured.addWidget(self.profiles)
        measured.addWidget(legend)
        hint=QLabel('Select a group to locate its cells. Select a marker value to use that channel in the tissue view.',self)
        self.profiles.setAccessibleDescription(hint.text());hint.hide()
        self.workspace.addWidget(self.measured_view,1,Qt.AlignmentFlag.AlignTop)
        self.workspace.addWidget(self.comparison_panel,1,Qt.AlignmentFlag.AlignTop)
        self.visual_buttons=QHBoxLayout();self.visual_buttons.setSpacing(6)
        self.graph_choice=QComboBox(self);self.graph_choice.setAccessibleName('Comparison graph')
        self.graph_choice.currentIndexChanged.connect(lambda:self.set_comparison_view(self.graph_choice.currentData()))
        self.visual_buttons.addWidget(self.graph_choice)
        self.visual_buttons.addStretch()
        self.graph_actions=QToolButton(self);self.graph_actions.setText('⋯')
        self.graph_actions.setAccessibleName('Graph actions');self.graph_actions.setToolTip('Graph actions')
        self.graph_actions.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu=QMenu(self.graph_actions)
        self.fit_graph_action=menu.addAction('Fit graph',self._fit_graph)
        self.export_graph_action=menu.addAction('Export figure…',self._export_graph)
        self.umap_settings_action=menu.addAction('UMAP settings…',self._show_umap_settings)
        self.profile_details_action=menu.addAction('Learned profile values')
        self.profile_details_action.setCheckable(True)
        self.profile_details_action.toggled.connect(lambda shown:self.model_view.set_profile_details(shown) if self.model_view is not None else None)
        menu.aboutToShow.connect(self._prepare_graph_actions)
        self.graph_actions.setMenu(menu);self.visual_buttons.addWidget(self.graph_actions)
        self.graph_toolbar=QWidget(self);self.graph_toolbar.setLayout(self.visual_buttons)
        self.visual_buttons.setContentsMargins(0,0,0,0);self.graph_toolbar.setFixedHeight(36)
        self.comparison_layout.addWidget(self.graph_toolbar)
        self.graph_description=QLabel(self);self.graph_description.setWordWrap(True)
        self.graph_description.setProperty('fieldHelp',True)
        self.comparison_layout.addWidget(self.graph_description)
        self._comparison_views={}
        self.tissue_heading=QLabel('Cells in tissue',self);self.tissue_heading.setStyleSheet('font-weight:600;')
        self.cell_layout.addWidget(self.tissue_heading)
        self.compare_layout.addWidget(self.tissue_panel)
        self.selected_summary=QLabel(self);self.selected_summary.setWordWrap(True);self.selected_summary.hide()
        self.cell_layout.addWidget(self.selected_summary)
        self.images=review_table('cluster-image-abundance',self);self.cell_layout.addWidget(self.images)
        self.field=ClusterFieldReview(self);self.field.show_image_summary=len(self._image_totals)>1
        self.cell_layout.addWidget(self.field)
        self.field.gallery_requested.connect(self._request_examples)
        self.field.cell_view.view.inspected.connect(self._tissue_cell_selected)
        self.examples=MarkerCellExamples([],self,title='Cluster cells',embedded=True)
        self.examples.image.hide();self.examples.refresh.hide();self.examples.requested.connect(self._request_examples)
        self.examples.hide();self.cell_layout.addWidget(self.examples)

        self.population.hide()
        self.reporting_overview=QWidget(self)
        overview=QVBoxLayout(self.reporting_overview);overview.setContentsMargins(0,0,0,0);overview.setSpacing(6)
        heading=QLabel('Final phenotypes',self);heading.setProperty('panelHeading',True)
        overview.addWidget(heading)
        self.summary=QLabel(self);self.summary.setWordWrap(True);self.summary.setProperty('fieldHelp',True)
        overview.addWidget(self.summary)
        self.assignment_layout.addWidget(self.reporting_overview)
        self.inspect=ContentSizedButton('Review selected group',self)
        self.inspect.clicked.connect(self._inspect_reporting_group)
        style_scientific_table(decisions);decisions.verticalHeader().hide()
        decisions.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        decisions.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
        decisions.setMinimumWidth(0);decisions.setMaximumWidth(16777215)
        for column in range(3):decisions.horizontalHeader().setSectionResizeMode(column,QHeaderView.ResizeMode.ResizeToContents)
        decisions.horizontalHeader().setSectionResizeMode(3,QHeaderView.ResizeMode.Stretch)
        decisions.verticalHeader().setDefaultSectionSize(34)
        decisions.horizontalHeader().setFixedHeight(40)
        decisions.setFixedHeight(40+min(10,len(model._ids))*34+22)
        decisions.hide()
        from .cluster_lineage import ClusterRefitHistory,ClusterContributions
        self.refit_history=None
        if form.context.get('refit_summary'):
            self.refit.setToolTip('Fit the checked groups again, or restore the groups saved before the latest refit.')
            self.refit_history=ClusterRefitHistory(form.context['refit_summary'],model._counts,self)
            self.assignment_layout.insertWidget(0,self.refit_history)
            self.refit_separator=QFrame(self.assignment_page)
            self.refit_separator.setFixedHeight(1)
            self.refit_separator.setStyleSheet('background:#d4e0e1;')
            self.assignment_layout.insertWidget(1,self.refit_separator)
        self.reporting=review_table('cluster-reporting-groups',self);self.assignment_layout.addWidget(self.reporting)
        self.contributions=ClusterContributions(model._counts,form.context.get('refit_summary'),self)
        self.contributions.inspect_requested.connect(self.inspect_cluster)
        self.assignment_layout.addWidget(self.contributions)
        self.reporting_summary=QLabel(self);self.reporting_summary.setWordWrap(True);self.reporting_summary.setProperty('fieldHelp',True)
        self.reporting_summary.hide()
        self.export=ContentSizedButton('Export review data…',self);self.export.clicked.connect(self._export_data)
        self.reporting_actions=QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.reporting_actions.addWidget(self.inspect);self.reporting_actions.addStretch();self.reporting_actions.addWidget(self.export)
        self.assignment_layout.addLayout(self.reporting_actions)
        self.navigation=QWidget(self);self.navigation.setObjectName('stage-form-navigation')
        navigation=QHBoxLayout(self.navigation);navigation.setContentsMargins(0,4,0,4)
        self.back=ContentSizedButton('Back',self);self.next=ContentSizedButton('Next: Review phenotypes',self)
        self.next.setProperty('actionRole','primary');self.position=QLabel(self)
        navigation.addWidget(self.back);navigation.addWidget(self.position,1);navigation.addWidget(self.next)
        layout.addWidget(self.navigation)
        self.back.clicked.connect(lambda:self.pages.setCurrentIndex(0));self.next.clicked.connect(lambda:self.pages.setCurrentIndex(1))
        self.pages.currentChanged.connect(self._page_changed)
        self.statistic.currentIndexChanged.connect(self._statistic_changed)
        decisions.selectionModel().selectionChanged.connect(lambda *_:self._synchronize(decisions,self.profiles))
        self.profiles.selectionModel().selectionChanged.connect(lambda *_:self._synchronize(self.profiles,decisions))
        decisions.selectionModel().currentRowChanged.connect(lambda *_:self._show_cluster())
        self.profiles.clicked.connect(self._profile_clicked)
        model.dataChanged.connect(self._decisions_changed);model.modelReset.connect(self._decisions_changed)
        self._decisions_changed()
        if model.rowCount():decisions.selectRow(0)
        self._adapt_matrix();self._comparison_changed();self._page_changed()

    def _add_comparison_view(self,key,title,widget):
        self.comparison_layout.insertWidget(self.comparison_layout.count()-1,widget)
        self._comparison_views[key]=widget;self._available_views.add(key)

    def attach_umap(self, widget):
        self.umap=widget
        self._add_comparison_view('umap','UMAP',widget)
        widget.toggle.setChecked(True);widget.toggle.hide()
        if hasattr(widget,'set_integrated'):widget.set_integrated()
        self._comparison_changed()

    def attach_model_view(self, widget):
        self.model_view=widget
        self._add_comparison_view('model','FlowSOM map',widget)
        self._available_views.discard('model')
        widget.toggle.setChecked(True);widget.toggle.hide()
        widget.availability_changed.connect(self._model_available)
        self.cluster_selected.connect(self._select_model_cluster)
        widget.cluster_selected.connect(self.select_cluster)
        widget.inspect_cluster.connect(self.inspect_cluster)
        widget.select_cluster(self._cluster)
        self._comparison_changed()

    def _select_model_cluster(self, identity):
        # A programmatic highlight must not collapse the researcher's multi-selection.
        with QSignalBlocker(self.model_view):self.model_view.select_cluster(identity)

    def _model_available(self, available):
        if available:self._available_views.add('model')
        else:self._available_views.discard('model')
        self._comparison_changed()

    def set_comparison_view(self,key):
        if key in self._available_views:
            self._comparison_mode=key
            self._comparison_changed()

    def _comparison_changed(self,*_):
        available=[key for key in ('model','umap') if key in self._available_views]
        if self._comparison_mode not in available:
            self._comparison_mode=available[0] if available else None
        self.comparison_panel.setVisible(bool(available))
        with QSignalBlocker(self.graph_choice):
            self.graph_choice.clear()
            for key in available:self.graph_choice.addItem('FlowSOM map' if key=='model' else 'UMAP',key)
            self.graph_choice.setCurrentIndex(self.graph_choice.findData(self._comparison_mode))
        for key,widget in self._comparison_views.items():
            active=key==self._comparison_mode
            widget.setVisible(active)
        self.graph_description.setText(
            'Each node stores one learned value per selected measurement. Lines connect the nodes as a minimum spanning tree, minimizing the sum of distances between connected nodes in the scaled and weighted measurements.'
            if self._comparison_mode=='model' else
            'Each point is one cell. Nearby points tend to have similar selected measurements. Select a point to inspect that cell in tissue.')
        self.umap_settings_action.setVisible(self._comparison_mode=='umap')
        self.profile_details_action.setVisible(self._comparison_mode=='model')
        self._highlight_selection()
        self.pages.updateGeometry()

    def _fit_graph(self):
        if self._comparison_mode=='model':self.model_view.fit_view()
        elif hasattr(self,'umap'):self.umap._fit_view()

    def _prepare_graph_actions(self):
        ready=self._comparison_mode=='model' or (hasattr(self,'umap') and self.umap.open_view.isEnabled())
        self.fit_graph_action.setEnabled(ready);self.export_graph_action.setEnabled(ready)

    def _export_graph(self):
        if self._comparison_mode=='model':self.model_view.export.click()
        elif hasattr(self,'umap'):self.umap.export_view.click()

    def _show_umap_settings(self):
        if hasattr(self,'umap'):self.umap.parameters.setVisible(not self.umap.parameters.isVisible())

    def _highlight_selection(self):
        identities=tuple(self.model._ids[index.row()] for index in self.decisions.selectionModel().selectedRows())
        if self.model_view is not None:self.model_view.highlight_clusters(identities)
        umap=getattr(self,'umap',None)
        if umap is not None and umap.point_view is not None:umap.point_view.highlight_clusters(identities)

    def _page_changed(self,*_):
        first=self.pages.currentIndex()==0
        self.back.setVisible(not first);self.next.setVisible(first)
        self.position.setText('Page 1 of 2' if first else 'Page 2 of 2')
        if first and hasattr(self,'_cluster'):
            self._request_cells()
            if self.field._bundle is None:self.field.refresh()
        elif not first:
            self.field._timer.stop();self.field.cell_view._field_timer.stop()
            selected=next((index for index,row in enumerate(self._reporting_rows)
                           if getattr(self,'_cluster',None) in row['cluster_ids']),None)
            if selected is not None:
                with QSignalBlocker(self.reporting.selectionModel()):self.reporting.selectRow(selected)
                self._reporting_details(self._reporting_rows[selected])
                self.reporting.scrollTo(self.reporting.model().index(selected,0))
            self.inspect.setEnabled(self.reporting.currentIndex().isValid())
        self.page_changed.emit()

    def _request_refit(self):
        identities=tuple(self.model._ids[index.row()] for index in self.decisions.selectionModel().selectedRows())
        if identities:self.refit_requested.emit(tuple(sorted(identities)))

    def select_cluster(self, identity):
        if identity not in self.model._ids:return
        row=self.model._ids.index(identity)
        selection=self.decisions.selectionModel()
        if QApplication.keyboardModifiers() & Qt.KeyboardModifier.ControlModifier:
            selection.select(self.model.index(row,0),QItemSelectionModel.SelectionFlag.Select|QItemSelectionModel.SelectionFlag.Rows)
            selection.setCurrentIndex(self.model.index(row,0),QItemSelectionModel.SelectionFlag.NoUpdate)
        elif len(selection.selectedRows())>1:
            selection.setCurrentIndex(self.model.index(row,0),QItemSelectionModel.SelectionFlag.NoUpdate)
        elif [index.row() for index in selection.selectedRows()]!=[row]:
            self.decisions.selectRow(row)

    def inspect_cluster(self, identity):
        self.select_cluster(identity);self.pages.setCurrentIndex(0)
        self._request_cells()

    def resizeEvent(self,event):
        super().resizeEvent(event);self._adapt_matrix()

    def _adapt_matrix(self):
        self.workspace.setDirection(QBoxLayout.Direction.LeftToRight if self.width()>=1100 else QBoxLayout.Direction.TopToBottom)
        self.selection_row.setDirection(QBoxLayout.Direction.LeftToRight if self.width()>=650 else QBoxLayout.Direction.TopToBottom)
        self.reporting_actions.setDirection(QBoxLayout.Direction.LeftToRight if self.width()>=650 else QBoxLayout.Direction.TopToBottom)
        for widget in (self.cluster_choice,self.name,self.merge,self.group_actions):self.assignment_tools.removeWidget(widget)
        for column in range(4):self.assignment_tools.setColumnStretch(column,0)
        self.assignment_tools.setColumnStretch(1,1)
        if self.width()>=900:
            for column,widget in enumerate((self.cluster_choice,self.name,self.merge,self.group_actions)):
                self.assignment_tools.addWidget(widget,0,column)
        else:
            self.assignment_tools.addWidget(self.cluster_choice,0,0,1,2)
            self.assignment_tools.addWidget(self.group_actions,0,2)
            self.assignment_tools.addWidget(self.name,1,0,1,2)
            self.assignment_tools.addWidget(self.merge,1,2)
        self.pages.updateGeometry()

    def _synchronize(self,source,target):
        if self._syncing:return
        self._syncing=True
        try:
            target.selectionModel().clearSelection()
            for index in source.selectionModel().selectedRows():
                target.selectionModel().select(target.model().index(index.row(),0),QItemSelectionModel.SelectionFlag.Select|QItemSelectionModel.SelectionFlag.Rows)
            selected=source.currentIndex()
            if selected.isValid():
                target.selectionModel().setCurrentIndex(target.model().index(selected.row(),0),QItemSelectionModel.SelectionFlag.NoUpdate)
                if target is self.profiles:
                    horizontal=target.horizontalScrollBar().value()
                    target.scrollTo(target.model().index(selected.row(),0),QTableView.ScrollHint.PositionAtCenter)
                    target.horizontalScrollBar().setValue(horizontal)
        finally:self._syncing=False
        self._selection_changed()
        self._show_cluster()

    def _profile_clicked(self,index):
        if index.isValid():
            self.profiles.selectionModel().setCurrentIndex(index,QItemSelectionModel.SelectionFlag.NoUpdate)
            self.decisions.selectionModel().setCurrentIndex(self.model.index(index.row(),0),QItemSelectionModel.SelectionFlag.NoUpdate)
            measurement=self.profile_model.names[index.column()]
            if measurement!=self._measurement:
                self.field._display.pop('channels',None)
                self.field.cell_view._review_channels=None
            self._measurement=measurement;self._point_cell_id=None
            self._request_cells()

    def _statistic_changed(self):
        self.profile_model.set_statistic(self.statistic.currentData())
        self._synchronize(self.decisions,self.profiles)

    def _decisions_changed(self,*_):
        decisions=self.model.raw_value()
        self.profile_model.excluded={key for key in self.model._ids if not decisions[key]['include']}
        if self.profile_model.rowCount():
            self.profile_model.dataChanged.emit(self.profile_model.index(0,0),self.profile_model.index(self.profile_model.rowCount()-1,self.profile_model.columnCount()-1))
        groups=self.model.reporting_groups()
        included=sum(row['cell_count'] for row in groups);excluded=sum(self.model._counts.values())-included
        rows=[dict(row,status='Named' if all(decisions[key]['name'] for key in row['cluster_ids']) else 'Unnamed') for row in groups]
        rows.sort(key=lambda row:(row['status']!='Named',row['phenotype_name'].casefold()))
        excluded_ids=tuple(key for key in self.model._ids if not decisions[key]['include'])
        if excluded_ids:
            rows.append(dict(phenotype_name='Not phenotyped',cell_count=excluded,cluster_ids=excluded_ids,status='Excluded'))
        self._reporting_rows=tuple(rows)
        total=sum(self.model._counts.values())
        counts={status:sum(row['cell_count'] for row in rows if row['status']==status) for status in ('Named','Unnamed','Excluded')}
        self.summary.setText(f"{counts['Named']:,} named cells · {counts['Unnamed']:,} unnamed · {counts['Excluded']:,} excluded")
        set_table(self.reporting,('Phenotype','Cells','% of all cells','Status'),
            ((row['phenotype_name'],f"{row['cell_count']:,}",f"{100*row['cell_count']/total:.2f}" if total else '—',row['status'])
             for row in rows))
        self.reporting.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch)
        self.reporting.horizontalHeader().setSectionResizeMode(1,QHeaderView.ResizeMode.ResizeToContents)
        self.reporting.horizontalHeader().setSectionResizeMode(2,QHeaderView.ResizeMode.ResizeToContents)
        self.reporting.horizontalHeader().setSectionResizeMode(3,QHeaderView.ResizeMode.ResizeToContents)
        row_height=self.reporting.verticalHeader().defaultSectionSize()
        self.reporting.setFixedHeight(40+min(12,len(rows))*row_height+22)
        if getattr(self,'_cluster',None) is not None:
            selected=next((index for index,row in enumerate(rows) if self._cluster in row['cluster_ids']),None)
            if selected is not None:
                self.reporting.selectRow(selected);self._reporting_details(rows[selected])
        self.reporting.selectionModel().currentRowChanged.connect(self._reporting_selected)
        self._selection_changed()
        if self.decisions.currentIndex().isValid():self._show_cluster()

    def _selection_changed(self):
        rows=self.decisions.selectionModel().selectedRows()
        decisions=self.model.raw_value()
        identities=tuple(self.model._ids[row.row()] for row in rows)
        excluded=sum(not decisions[key]['include'] for key in identities)
        self.refit.setEnabled(bool(identities));self.inspect.setEnabled(bool(identities))
        self.group_actions.setEnabled(bool(identities))
        self.include_action.setVisible(bool(excluded));self.exclude_action.setVisible(excluded<len(identities))
        self.clear_name_action.setEnabled(bool(identities) and not excluded and any(decisions[key]['name'] for key in identities))
        self.cluster_choice.setText((identities[0] if len(identities)==1 else f'{len(identities)} groups selected' if identities else 'Choose groups')+' ▾')
        self.cluster_choice.setToolTip('\n'.join(f"{key} · {decisions[key]['name'] or 'Unnamed'} · {self.model._counts[key]:,} cells" for key in identities))
        if not identities:
            text='Select a group in the view, or choose groups to name together.'
        else:
            count=sum(self.model._counts[key] for key in identities)
            text=f'{count:,} cells · {100*count/sum(self.model._counts.values()):.2f}% of clustered cells'
            if excluded:
                text+=f' · {excluded:,} excluded from phenotyping'
        self.selection_summary.setText(text)
        self.tissue_heading.setText('Cells in tissue'+(' · '+self._cluster
            if len(identities)>1 and hasattr(self,'_cluster') else ''))
        self.selection_summary.setToolTip(', '.join(identities))
        names={decisions[key]['name'] for key in identities}
        with QSignalBlocker(self.name):self.name.setText(next(iter(names)) if len(names)==1 else '')
        self.name.setEnabled(bool(identities) and not excluded)
        self._update_group_focus()
        self._name_preview()
        self._highlight_selection()

    def _include_selected(self,included):
        identities=tuple(index.row() for index in self.decisions.selectionModel().selectedRows())
        checked=Qt.CheckState.Checked if included else Qt.CheckState.Unchecked
        for row in identities:self.model.setData(self.model.index(row,2),checked,Qt.ItemDataRole.CheckStateRole)

    def _clear_selected_names(self):
        for index in self.decisions.selectionModel().selectedRows():
            self.model.setData(self.model.index(index.row(),3),'',Qt.ItemDataRole.EditRole)

    def _update_group_focus(self):
        identities=tuple(self.model._ids[index.row()] for index in self.decisions.selectionModel().selectedRows())
        self.group_focus.setVisible(len(identities)>1)
        from html import escape
        parts=[]
        for key in identities:
            text=escape(key)
            if key==getattr(self,'_cluster',None):text='<b>'+text+'</b>'
            parts.append(f'<a href="{escape(key)}" style="color:#276a70;text-decoration:none">{text}</a>')
        focus=getattr(self,'_cluster',None)
        if focus not in identities and focus is not None:
            parts.append('Viewing <b>'+escape(focus)+'</b>')
        self.group_focus.setText(' · '.join(parts))
        self.group_focus.setToolTip('The bold group is shown in tissue. The checked groups receive the name or group action. Use the selector or Ctrl-click to change that selection.')

    def _reporting_selected(self,index,*_):
        groups=self._reporting_rows
        if not index.isValid() or index.row()>=len(groups):return
        self._reporting_details(groups[index.row()])
        ids=groups[index.row()]['cluster_ids'];selection=self.decisions.selectionModel()
        self._syncing=True
        try:
            selection.clearSelection()
            for identity in ids:
                row=self.model._ids.index(identity)
                selection.select(self.model.index(row,0),QItemSelectionModel.SelectionFlag.Select|QItemSelectionModel.SelectionFlag.Rows)
            selection.setCurrentIndex(self.model.index(self.model._ids.index(ids[0]),0),QItemSelectionModel.SelectionFlag.NoUpdate)
        finally:self._syncing=False
        self._synchronize(self.decisions,self.profiles)

    def _reporting_details(self,row):
        self.contributions.set_group(row)

    def _inspect_reporting_group(self):
        index=self.reporting.currentIndex()
        if index.isValid():
            self._reporting_selected(index)
            self.pages.setCurrentIndex(0)

    def _select_groups(self):
        """Choose exact existing groups while keeping the current visual in place."""
        menu=QMenu(self)
        selected={index.row() for index in self.decisions.selectionModel().selectedRows()}
        decisions=self.model.raw_value()
        for row,key in enumerate(self.model._ids):
            name=decisions[key]['name']
            text=f'{key}'+(f' · {name}' if name else '')+f' · {self.model._counts[key]:,} cells'+(' · Excluded' if not decisions[key]['include'] else '')
            check=QCheckBox(text,menu);check.setChecked(row in selected)
            check.setContentsMargins(10,5,16,5)
            check.toggled.connect(lambda checked,index=row:self._select_group_row(index,checked))
            action=QWidgetAction(menu);action.setDefaultWidget(check);menu.addAction(action)
        menu.exec(self.cluster_choice.mapToGlobal(self.cluster_choice.rect().bottomLeft()))
        menu.deleteLater()

    def _select_group_row(self,row,checked):
        selection=self.decisions.selectionModel()
        flag=QItemSelectionModel.SelectionFlag.Select if checked else QItemSelectionModel.SelectionFlag.Deselect
        selection.select(self.model.index(row,0),flag|QItemSelectionModel.SelectionFlag.Rows)
        if checked and not selection.isRowSelected(selection.currentIndex().row(),QModelIndex()):
            selection.setCurrentIndex(self.model.index(row,0),QItemSelectionModel.SelectionFlag.NoUpdate)
        elif selection.currentIndex().row()==row and selection.selectedRows():
            selection.setCurrentIndex(selection.selectedRows()[0],QItemSelectionModel.SelectionFlag.NoUpdate)

    def _name_preview(self):
        identities=tuple(self.model._ids[row.row()] for row in self.decisions.selectionModel().selectedRows())
        decisions=self.model.raw_value();name=self.name.text().strip()
        enabled=bool(name and identities) and all(decisions[key]['include'] for key in identities)
        self.merge.setEnabled(enabled and any(decisions[key]['name']!=name for key in identities))
        members=tuple(key for key in self.model._ids if key in identities or
            (decisions[key]['include'] and (decisions[key]['name'] or key)==name)) if enabled else ()
        self.name_preview.setVisible(len(members)>1)
        self.merge.setText('Merge in reports' if len(members)>1 else 'Apply name')
        if len(members)>1:
            count=sum(self.model._counts[key] for key in members)
            self.name_preview.setText(f'Applying this name combines {len(members):,} groups ({count:,} cells) in reports.')
            self.name_preview.setToolTip(', '.join(members))

    def _selected_image(self):
        index=self.images.currentIndex()
        if index.isValid() and index.row()<len(getattr(self,'_visible_images',())):
            row=self._visible_images[index.row()]
            return row['sample_id'],row['project_entry_id'],row['slide_id']
        return None

    def _show_cluster(self):
        index=self.decisions.currentIndex()
        if not index.isValid():return
        identity=self.model._ids[index.row()]
        if getattr(self,'_cluster',None)==identity:return
        selected_image=self._selected_image()
        self._cluster=identity;self._pending=None;self.examples.hide();self.examples.cell_view.clear()
        self.tissue_heading.setText('Cells in tissue'+(' · '+identity
            if len(self.decisions.selectionModel().selectedRows())>1 else ''))
        self._update_group_focus()
        self._point_cell_id=None
        count=self.model._counts[identity];total=sum(self.model._counts.values())
        self.selected_summary.setText(f'{identity} · {count:,} cells · {100*count/total:.2f}% of clustered cells')
        counts=Counter()
        for row in self._sections:
            if row['cluster_id']==identity:counts[row['sample_id'],row['project_entry_id'],row['slide_id']]+=row['cell_count']
        self._visible_images=tuple(dict(sample_id=key[0],project_entry_id=key[1],slide_id=key[2],cell_count=counts[key])
                                   for key in sorted(self._image_totals))
        single_image=len(self._image_totals)==1
        self.selected_summary.setVisible(not single_image)
        if not single_image:
            self.selected_summary.setText('Across images: '+self.selected_summary.text())
        set_table(self.images,('Image','Cluster cells','Of image'),
            ((' · '.join(dict.fromkeys((r['sample_id'],r['slide_id']))),f"{r['cell_count']:,}",
              f"{100*r['cell_count']/self._image_totals[r['sample_id'],r['project_entry_id'],r['slide_id']]:.3g}%") for r in self._visible_images))
        self.images.setToolTip('Of image uses all clustered cells in that image as its denominator.')
        self.images.setMaximumHeight(218)
        self.images.setFixedHeight(self.images.horizontalHeader().height()
            +sum(self.images.rowHeight(i) for i in range(min(5,len(self._visible_images))))
            +2*self.images.frameWidth()+6)
        self.images.selectionModel().currentRowChanged.connect(self._image_selected)
        if self._visible_images:
            selected=next((i for i,row in enumerate(self._visible_images)
                if (row['sample_id'],row['project_entry_id'],row['slide_id'])==selected_image),0)
            self.images.selectRow(selected)
        self.images.setVisible(not single_image)
        self.cluster_selected.emit(identity)
        self._highlight_selection()

    def _image_selected(self,*_):
        self._pending=None;self._point_cell_id=None;self.examples.hide();self.examples.cell_view.clear()
        self._request_cells()

    def _name_selected(self):
        rows=self.decisions.selectionModel().selectedRows()
        identities=tuple(self.model._ids[row.row()] for row in rows)
        if not identities or any(not self.model.raw_value()[key]['include'] for key in identities):return
        name=self.name.text().strip()
        if name:self.model.name_clusters(identities,name)

    def _request_cells(self):
        if self.pages.currentIndex()!=0:return
        index=self.images.currentIndex()
        if not index.isValid():return
        row=self._visible_images[index.row()]
        self.field.gallery_action.setEnabled(bool(row['cell_count']))
        self.field.configure(self._cluster,(row['sample_id'],row['project_entry_id'],row['slide_id']),
                             self._measurement,focus_cell=self._point_cell_id)

    def _tissue_cell_selected(self,identities):
        # Keep the selected full identity and camera in the shared field viewer.
        # Programmatic focus restoration must not collapse a multi-group selection.
        rows=self.field.cell_view._by_id
        row=next((rows[key] for key in identities if key in rows),{})
        identity=row.get('cluster_id')
        if identity in self.model._ids:
            self.select_cluster(identity)

    def _request_examples(self):
        index=self.images.currentIndex()
        if not index.isValid():return
        row=self._visible_images[index.row()]
        if not row['cell_count']:return
        appearance=self.examples.options() if self.examples._display_options is not None else {}
        self._pending={**appearance,'cluster_id':self._cluster,
            'image_identity':[row['sample_id'],row['project_entry_id'],row['slide_id']],
            'measurement':self._measurement,'field_aspect':max(.25,min(4.,self.width()/320)), 'pyramid_level':0}
        if self._point_cell_id is not None:
            self._pending['cell_ids']=[self._point_cell_id]
        self.examples.show();self.examples.set_busy();self.image_requested.emit()

    def select_embedding_cell(self,row):
        """Link one saved UMAP point to its exact cluster, image and measured cell."""
        cluster=row.get('cluster_id')
        if cluster not in self.model._ids:
            return False
        matches=[i for i,value in enumerate(self._visible_images)
            if (value['sample_id'],value['project_entry_id'])==(row['sample_id'],row['project_entry_id'])
            and ('slide_id' not in row or value['slide_id']==row['slide_id'])]
        if len(matches)!=1:
            return False
        self.select_cluster(cluster)
        self.images.selectRow(matches[0])
        self.pages.setCurrentIndex(0)
        self._point_cell_id=row['cell_id']
        self._request_cells()
        return True

    def image_options(self):
        return {'cluster_gallery_settings':dict(self._pending)} if self._pending else None

    def cancel_image_request(self):
        """Discard a pending image reply when the researcher requests another review."""
        if self._pending is not None:
            self._pending=None;self.examples.hide()

    def set_image_plots(self,plots):
        if not plots or (plots[0].display_options or {}).get('cluster_request')!=self._pending:return False
        self.examples.set_plots(plots)
        count=len(self.examples.cell_view.bundle['cell_ids'])
        self.examples.status.setText(f"{count:,} cell{'s' if count!=1 else ''} shown")
        self._pending=None;return True

    def set_image_error(self,message):
        self.examples.set_error(message)

    def _export_data(self):
        from io import BytesIO
        from zipfile import ZipFile,ZIP_DEFLATED
        from .figure_export import _choose_path,_save_bytes
        path=_choose_path(self,'cluster-review.zip',{'Review tables (*.zip)':'zip'})
        if path is None:return
        output=BytesIO()
        with ZipFile(output,'w',ZIP_DEFLATED) as archive:
            for name,table in self._tables.items():archive.writestr(name+'.csv',csv_bytes(table.columns,(r.values for r in table.rows)))
            archive.writestr('interpretation.csv',csv_bytes(('cluster_id','cell_count','include','phenotype_name'),
                ((identity,self.model._counts[identity],row['include'],row['name']) for identity,row in self.model.raw_value().items())))
            archive.writestr('reporting_groups.csv',csv_bytes(('phenotype_name','cell_count','cluster_ids'),
                ((row['phenotype_name'],row['cell_count'],json.dumps(row['cluster_ids']))
                 for row in self.model.reporting_groups())))
            total=sum(self.model._counts.values())
            archive.writestr('review_assignments.csv',csv_bytes(
                ('phenotype_name','status','cell_count','percent_of_all_clustered_cells','total_clustered_cell_count','cluster_ids'),
                ((row['phenotype_name'],row['status'],row['cell_count'],100*row['cell_count']/total if total else '',
                  total,json.dumps(row['cluster_ids'])) for row in self._reporting_rows)))
        _save_bytes(output.getvalue(),path)
