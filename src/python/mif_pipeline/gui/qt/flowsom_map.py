"""Inspect the retained FlowSOM codebook and its actual minimum spanning tree."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt, Signal, QSignalBlocker
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QBoxLayout, QLabel, QPushButton, QToolButton, QComboBox, QSizePolicy

from .classifier_review_data import review_table, set_table
from .researcher_widgets import configure_disclosure


class FlowSomMap(QWidget):
    """A codebook view; cell inspection uses only a recorded canonical group map."""

    cluster_selected = Signal(str)
    inspect_cluster = Signal(str)
    availability_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._model = None
        self._selected_cluster = None
        self._selected_node = None
        self._press = None
        self._dragged = False
        self._profile_details=False
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.toggle = QToolButton(self)
        self.toggle.setText('FlowSOM map')
        self.toggle.setCheckable(True)
        configure_disclosure(self.toggle)
        layout.addWidget(self.toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        self.body = QWidget(self)
        self.body.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Maximum)
        body = QVBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setAlignment(Qt.AlignmentFlag.AlignTop)
        body.setSpacing(10)
        description = self.description = QLabel('Each node is a learned marker profile. Edges form the minimum spanning tree of distances between those profiles.', self.body)
        description.setWordWrap(True)
        description.setProperty('fieldHelp', True)
        body.addWidget(description);description.hide()
        toolbar=QHBoxLayout();toolbar.setSpacing(10)
        self.node_choice=QComboBox(self.body)
        self.node_choice.setAccessibleName('Learned map profile')
        self.node_choice.setPlaceholderText('Select a map node…')
        self.node_choice.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.node_choice.setMinimumContentsLength(12)
        self.node_choice.currentIndexChanged.connect(self.select_node)
        toolbar.addWidget(self.node_choice,1)
        self.fit=QPushButton('Fit view',self.body);self.fit.clicked.connect(self.fit_view);toolbar.addWidget(self.fit)
        self.export=QPushButton('Export…',self.body);self.export.clicked.connect(self._export);toolbar.addWidget(self.export)
        self.toolbar=QWidget(self.body);self.toolbar.setLayout(toolbar)
        toolbar.setContentsMargins(0,0,0,0);body.addWidget(self.toolbar);self.toolbar.hide()
        self.node_choice.hide();self.fit.hide();self.export.hide()
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        self.figure=Figure(figsize=(7,5),layout='constrained')
        self.canvas=FigureCanvasQTAgg(self.figure)
        self.canvas.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Fixed)
        self.canvas.setMinimumWidth(0);self.canvas.setFixedHeight(490)
        self.canvas.setAccessibleName('FlowSOM map and selected learned marker profile')
        self.canvas.setAccessibleDescription('Select a map node to compare its learned marker values and locate its cell group in tissue. Scroll over the map to zoom; drag to pan.')
        body.addWidget(self.canvas)
        self.selected=QLabel(self.body);self.selected.setWordWrap(True);self.selected.setProperty('fieldHelp',True)
        body.addWidget(self.selected);self.selected.hide()
        self.caption=QLabel('Tree positions are arranged for readability; drawn line lengths are not marker-space distances.',self.body)
        self.canvas.setToolTip('The tree layout is arranged for readability. Select a profile to inspect connected-profile distances in weighted, scaled marker measurements.')
        self.caption.hide()
        layout.addWidget(self.body);self.body.hide();self.toggle.toggled.connect(self.body.setVisible)
        self._wide_plot=None
        for name,callback in (('motion_notify_event',self._motion),('button_press_event',self._pressed),
                              ('button_release_event',self._released),('scroll_event',self._zoom)):
            self.canvas.mpl_connect(name,callback)
        self.hide()

    def resizeEvent(self,event):
        super().resizeEvent(event)
        self._layout_plot()
        self._fit_body_height()

    def _fit_body_height(self):
        # The wrapped labels must not consume the fixed canvas's allocated height.
        self.body.layout().invalidate()
        height=self.body.layout().heightForWidth(max(1,self.width()))
        if height>0:self.body.setFixedHeight(height)
        self.body.layout().activate()
        self.updateGeometry()

    def _layout_plot(self):
        if self._model is None or not hasattr(self,'profile_axis'):return
        mode=(self.width()>=680,self._profile_details)
        if self._wide_plot==mode:return
        from matplotlib.gridspec import GridSpec
        if self._profile_details:
            grid=GridSpec(2,1,figure=self.figure,height_ratios=(1.5,1))
            self.axis.set_subplotspec(grid[0]);self.profile_axis.set_subplotspec(grid[1])
        else:
            self.axis.set_subplotspec(GridSpec(1,1,figure=self.figure)[0])
        self.profile_axis.set_visible(self._profile_details)
        self.canvas.setFixedHeight(580 if self._profile_details else 500)
        self._wide_plot=mode;self.canvas.draw_idle()

    def set_profile_details(self,shown):
        self._profile_details=bool(shown);self.node_choice.setVisible(shown);self.toolbar.setVisible(shown)
        self._layout_plot();self._fit_body_height()

    def _draw_profile(self,index):
        axis=self.profile_axis;axis.clear()
        if index is None:
            axis.set_axis_off()
            axis.text(.5,.5,'Select a map node to compare its learned marker values',
                transform=axis.transAxes,ha='center',va='center',wrap=True,color='#486169',fontsize=9)
        else:
            values=self._codes[index]
            names=[name.removeprefix('Cell: ').replace(': Mean',' · Mean').replace(': Median',' · Median') for name in self._measurements]
            positions=np.arange(len(values))
            bars=axis.barh(positions,values,color='#368d8d',height=.58,zorder=2)
            axis.set_yticks(positions,names);axis.invert_yaxis()
            low=min(0.,float(np.min(values)));high=max(0.,float(np.max(values)))
            span=max(high-low,1.)
            axis.set_xlim(low-.08*span,high+.24*span)
            axis.axvline(0,color='#b8c9c8',lw=.8,zorder=1)
            for position,value in zip(positions,values):
                axis.text(.99,position,f'{value:.3g}',transform=axis.get_yaxis_transform(),
                    ha='right',va='center',fontsize=9,color='#173d47')
            axis.set_xlabel('Weighted scaled marker value',fontsize=9)
            axis.spines[['top','right','left']].set_visible(False)
            axis.tick_params(axis='y',length=0)
        from .umap_points import match_interface_style
        match_interface_style(self.figure,self)
        self.canvas.draw_idle()

    def set_model(self, model):
        self._model = model
        self._selected_node = None
        self.selected.setText('Select a node; its linked group is highlighted in the tissue view.')
        if model is None:
            self.hide()
            self.availability_changed.emit(False)
            return
        self.description.setText('Each node is a learned marker profile. Lines connect all profiles with the smallest total distance in the weighted, scaled marker measurements.'
                                 + (' '+model['scope_note'] if model.get('scope_note') else ''))
        codes = np.asarray(model['model_codebook'], dtype=float)
        self._groups = tuple(model['model_clusters'])
        self._labels = dict(model.get('model_group_labels', ()))
        self._measurements = tuple(model['feature_measurements'])
        self._counts = dict(model.get('cluster_counts', {}))
        if (codes.ndim != 2 or not 1 < len(codes) <= 900
                or codes.shape[1] != len(self._measurements)
                or len(self._groups) != len(codes) or not np.isfinite(codes).all()):
            raise ValueError('The retained FlowSOM codebook dimensions are invalid.')
        self._codes = codes
        with QSignalBlocker(self.node_choice):
            self.node_choice.clear()
            for index, group in enumerate(self._groups):
                label = self._labels.get(group)
                self.node_choice.addItem(f'Map profile {index + 1}')
            self.node_choice.setCurrentIndex(-1)
        from mif_pipeline.cell_analysis.flowsom_plots import tree_geometry, draw_tree
        self._coordinates, self._edges, self._edge_distances = tree_geometry(codes)
        self.figure.clear()
        self.axis = self.figure.add_subplot(211)
        self.profile_axis = self.figure.add_subplot(212)
        self._wide_plot=None
        self._layout_plot()
        self._draw_profile(None)
        self._nodes = draw_tree(self.axis, model, self._coordinates, self._edges)
        self._highlight = self.axis.scatter([], [], s=105, facecolors='none', edgecolors='#d38b24', linewidths=1.5, zorder=4)
        self._node_focus = self.axis.scatter([], [], s=150, facecolors='none', edgecolors='#203f43', linewidths=1.5, zorder=4)
        self._selection_note=self.figure.text(.025,.98,'',transform=self.figure.transFigure,
            ha='left',va='top',fontsize=9,color='#36565d',zorder=6,
            bbox=dict(facecolor=self.palette().window().color().name(),edgecolor='none',alpha=.94,pad=4))
        self._hover = self.axis.annotate('', xy=(0, 0), xytext=(12, 12), textcoords='offset points',
            fontsize=9, bbox=dict(boxstyle='round,pad=.4', facecolor='white', edgecolor='#9aabad', alpha=.97), zorder=5)
        self._hover.set_visible(False)
        self.axis.margins(.12)
        self.axis.autoscale_view()
        self._limits = self.axis.get_xlim(), self.axis.get_ylim()
        from .umap_points import match_interface_style
        match_interface_style(self.figure, self)
        # Loading a model restores its focus without changing review selection.
        with QSignalBlocker(self):self.select_cluster(self._selected_cluster)
        self.show()
        self.availability_changed.emit(True)
        self._fit_body_height()
        self.canvas.draw_idle()

    def select_cluster(self, cluster):
        self._selected_cluster = cluster
        if self._model is None:
            return
        selected = [index for index, group in enumerate(self._groups)
                    if cluster is not None and self._labels.get(group) == cluster]
        self._highlight.set_offsets(self._coordinates[selected] if selected else np.empty((0, 2)))
        if selected and self._selected_node not in selected:
            self.select_node(selected[0])
        elif not selected:
            self._selected_node=None
            with QSignalBlocker(self.node_choice):self.node_choice.setCurrentIndex(-1)
            self._node_focus.set_offsets(np.empty((0,2)))
            self._draw_profile(None)
            self._selection_note.set_text('')
            if self._model.get('scope_note'):
                self.selected.setText('The selected group is outside this refit model.')
        self.canvas.draw_idle()

    def highlight_clusters(self, clusters):
        """Highlight exact selected groups without changing the active map profile."""
        if self._model is None:return
        selected=[index for index,group in enumerate(self._groups) if self._labels.get(group) in clusters]
        self._highlight.set_offsets(self._coordinates[selected] if selected else np.empty((0,2)))
        self.canvas.draw_idle()

    def _hit(self, event):
        if self._model is None or event.inaxes is not self.axis:
            return None
        contains, details = self._nodes.contains(event)
        if not contains:
            return None
        indices = details['ind']
        points = self.axis.transData.transform(self._coordinates[indices])
        return int(indices[np.argmin(np.sum((points - [event.x, event.y]) ** 2, axis=1))])

    def _node_title(self, index):
        group = self._groups[index]
        label = self._labels.get(group)
        title = f'Profile {index + 1}'
        if label is not None:
            title += f' · {label}'
            if label in self._counts:
                title += f" · {self._counts[label]:,} cells in this group"
        else:
            title += f' · model group {group}'
        return title

    def _motion(self, event):
        if self._model is None:
            return
        if self._press is not None and event.inaxes is self.axis:
            x, y, xlim, ylim = self._press
            self._dragged |= abs(event.x - x) + abs(event.y - y) > 4
            dx = (event.x - x) * (xlim[1] - xlim[0]) / self.axis.bbox.width
            dy = (event.y - y) * (ylim[1] - ylim[0]) / self.axis.bbox.height
            self.axis.set_xlim(xlim[0] - dx, xlim[1] - dx)
            self.axis.set_ylim(ylim[0] - dy, ylim[1] - dy)
            self.canvas.draw_idle()
            return
        index = self._hit(event)
        self._hover.set_visible(index is not None)
        if index is not None:
            self._hover.xy = self._coordinates[index]
            right = event.x > self.axis.bbox.x0 + self.axis.bbox.width / 2
            upper = event.y > self.axis.bbox.y0 + self.axis.bbox.height / 2
            self._hover.set_position((-12 if right else 12, -12 if upper else 12))
            self._hover.set_ha('right' if right else 'left')
            self._hover.set_va('top' if upper else 'bottom')
            values = '\n'.join(f'{name}: {value:.3g}' for name, value in zip(self._measurements[:6], self._codes[index][:6]))
            self._hover.set_text(self._node_title(index) + '\nLearned scaled values\n' + values)
        self.canvas.draw_idle()

    def _pressed(self, event):
        if self._model is not None and event.inaxes is self.axis and event.button == 1:
            self._press = event.x, event.y, self.axis.get_xlim(), self.axis.get_ylim()
            self._dragged = False

    def _released(self, event):
        if self._press is None:
            return
        self._press = None
        index = None if self._dragged else self._hit(event)
        if index is not None:
            self.select_node(index)

    def select_node(self, index):
        if self._model is None or not 0 <= index < len(self._codes):
            return
        self._selected_node = index
        with QSignalBlocker(self.node_choice):
            self.node_choice.setCurrentIndex(index)
        self._node_focus.set_offsets(self._coordinates[[index]])
        self.canvas.draw_idle()
        neighbours = [(b if a == index else a, distance) for (a, b), distance in zip(self._edges, self._edge_distances) if index in (a, b)]
        links = '\n'.join(f'Profile {node + 1} · distance {distance:.3g}' for node,distance in neighbours)
        self.selected.setText(f'Profile {index+1} · connected to profile {neighbours[0][0]+1} · distance {neighbours[0][1]:.3g}'
            if len(neighbours)==1 else f'Profile {index+1} · connected to {len(neighbours)} profiles')
        self.selected.setToolTip(links)
        if len(neighbours)==1:
            detail=f'To profile {neighbours[0][0]+1} · distance {neighbours[0][1]:.3g}'
        else:
            distances=[distance for _,distance in neighbours]
            detail=(f'{len(neighbours)} connections · distances {min(distances):.3g}–{max(distances):.3g}'
                    if distances else 'No connected profiles')
        self._selection_note.set_text(f'Profile {index+1}\n'+detail)
        self.canvas.setToolTip('Distances use weighted, scaled marker measurements.\n'+links)
        self._draw_profile(index)
        label = self._labels.get(self._groups[index])
        if label is not None:
            self.select_cluster(label)
            self.cluster_selected.emit(label)
        else:
            reason = ('This model group has no cells in the fitted population.' if self._labels
                      else 'This saved profile has no recorded link to a numbered cell cluster.')
            self.selected.setText(self.selected.text() + '\n' + reason)
        self._fit_body_height()

    def _inspect(self):
        if self._selected_node is not None:
            label = self._labels.get(self._groups[self._selected_node])
            if label is not None:
                self.inspect_cluster.emit(label)

    def _zoom(self, event):
        if self._model is None or event.inaxes is not self.axis:
            return
        factor = .8 if event.button == 'up' else 1.25
        for get, set_, center in ((self.axis.get_xlim, self.axis.set_xlim, event.xdata),
                                  (self.axis.get_ylim, self.axis.set_ylim, event.ydata)):
            lower, upper = get()
            set_(center + (lower - center) * factor, center + (upper - center) * factor)
        self.canvas.draw_idle()

    def fit_view(self):
        if self._model is not None:
            self.axis.set_xlim(self._limits[0]); self.axis.set_ylim(self._limits[1])
            self.canvas.draw_idle()

    def _export(self):
        from .figure_export import export_figure
        from matplotlib.text import Text
        from mif_pipeline.cell_analysis.figure_style import apply_figure_font
        from .umap_points import match_interface_style
        texts = [(text, text.get_fontproperties().copy()) for text in self.figure.findobj(Text)]
        try:
            self.figure.set_facecolor('white')
            self.axis.set_facecolor('white')
            apply_figure_font(self.figure)
            export_figure(self, self.figure, 'flowsom-map.svg', description=self.caption.text())
        finally:
            for text, properties in texts:
                text.set_fontproperties(properties)
            match_interface_style(self.figure, self)
            self.canvas.draw_idle()
