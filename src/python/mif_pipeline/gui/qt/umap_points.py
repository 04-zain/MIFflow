"""Inspect saved UMAP coordinates while retaining their original cell identities."""
from pathlib import Path

import numpy as np
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel


def match_interface_style(figure, widget):
    """Use the same page palette and typography as native measurement plots."""
    from PySide6.QtGui import QFontInfo
    from matplotlib.text import Text
    from matplotlib.colors import to_rgba
    font = QFontInfo(widget.font())
    background = widget.palette().window().color().name()
    foreground = widget.palette().windowText().color().name()
    figure.set_facecolor(background)
    for axis in figure.axes:
        axis.set_facecolor(background)
        axis.tick_params(colors=foreground)
        for spine in axis.spines.values():
            spine.set_color('#bdcccc')
    for text in figure.findobj(Text):
        properties = text.get_fontproperties().copy()
        properties.set_file(None)
        properties.set_family(font.family())
        properties.set_size(font.pointSizeF())
        text.set_fontproperties(properties)
        if to_rgba(text.get_color()) == (0., 0., 0., 1.):
            text.set_color(foreground)


class UmapPointView(QWidget):
    """Native hover, zoom, pan and exact point selection on an existing coordinate fit.

    The retained table supplies cell identities and colour values. A two-dimensional
    search tree resolves nearby screen points; this display neither fits clusters
    nor interprets a drawn boundary as a partition of the original marker space.
    """
    cell_selected = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        self.figure = Figure(figsize=(8, 4), layout='constrained')
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.canvas.setMinimumHeight(330)
        self.canvas.setMaximumHeight(480)
        self.canvas.setAccessibleName('Interactive UMAP coordinates')
        self.canvas.setAccessibleDescription('Hover for exact coordinates and marker values. Click a point to inspect its cell; scroll to zoom and drag to pan.')
        layout = QVBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.canvas)
        self.caption = QLabel(self); self.caption.setWordWrap(True); layout.addWidget(self.caption)
        self._table = None; self._tree = None; self._press = None; self._dragged = False
        self._cluster = None; self._point = None; self._tree_transform = None
        for name, callback in (('motion_notify_event', self._motion), ('button_press_event', self._pressed),
                               ('button_release_event', self._released), ('scroll_event', self._zoom),
                               ('draw_event', self._index_points)):
            self.canvas.mpl_connect(name, callback)
        self.hide()

    def set_csv(self, path):
        """Read one bounded saved coordinate table; no source measurements are refitted."""
        import pyarrow as pa
        import pyarrow.csv as csv
        from matplotlib.colors import ListedColormap
        path = Path(path)
        if path.stat().st_size > 256 * 1024**2:
            raise ValueError('This coordinate table exceeds the review file allowance.')
        strings = ('sample_id', 'project_entry_id', 'slide_id', 'section_id', 'cell_id', 'cluster_id', 'colour_value',
                   'colour_kind', 'category_colour', 'measurement', 'units')
        table = csv.read_csv(path, convert_options=csv.ConvertOptions(column_types={name: pa.string() for name in strings})).combine_chunks()
        required = {'sample_id', 'project_entry_id', 'cell_id', 'umap_1', 'umap_2', 'colour_value', 'colour_kind'}
        if not required <= set(table.column_names) or not 0 < table.num_rows <= 1_000_000:
            raise ValueError('The UMAP table needs bounded coordinates and their exact cell identities.')
        coordinates = np.column_stack([table[name].to_numpy() for name in ('umap_1', 'umap_2')]).astype(float)
        if not np.isfinite(coordinates).all():
            raise ValueError('The saved UMAP coordinates contain an unavailable value.')
        self._table = table; self._coordinates = coordinates
        self._cluster = None; self._point = None; self._tree = None; self._tree_transform = None
        self.figure.clear(); self.axis = self.figure.add_subplot()
        first = lambda name: table[name][0].as_py() if name in table.column_names else None
        self._continuous = first('colour_kind') == 'continuous'
        self._cluster_colour = first('colour_attribute') == 'cluster_id'
        if self._continuous:
            values = table['colour_value'].cast(pa.float64()).to_numpy()
            artist = self.axis.scatter(*coordinates.T, c=values, s=9, alpha=.65, linewidths=0,
                cmap='viridis', vmin=first('colour_min'), vmax=first('colour_max'), rasterized=True)
            label = first('measurement') or 'Saved measurement'
            if first('units'): label += ' (' + first('units') + ')'
            self.figure.colorbar(artist, ax=self.axis, label=label)
        else:
            from mif_pipeline.cell_analysis.embedding_plots import _readable_category_color
            categories = table['category_colour'].dictionary_encode().combine_chunks()
            colours = [_readable_category_color(color) for color in categories.dictionary.to_pylist()]
            self.axis.scatter(*coordinates.T, c=categories.indices.to_numpy(), cmap=ListedColormap(colours),
                vmin=-.5, vmax=len(colours)-.5, s=9, alpha=.65, linewidths=0, rasterized=True)
        self.axis.set(xlabel='UMAP 1', ylabel='UMAP 2')
        self.axis.set_aspect('equal',adjustable='box')
        self.axis.spines[['top', 'right']].set_visible(False)
        self._full_limits = self.axis.get_xlim(), self.axis.get_ylim()
        self._highlight = self.axis.scatter([], [], s=23, facecolors='none', edgecolors='#d38b24', linewidths=.7)
        self._selected = self.axis.scatter([], [], s=70, facecolors='none', edgecolors='#d54e37', linewidths=1.5, zorder=5)
        self._hover = self.axis.annotate('', xy=(0, 0), xytext=(12, 12), textcoords='offset points',
            fontsize=9, bbox=dict(boxstyle='round,pad=.4', facecolor='white', edgecolor='#9aabad', alpha=.96))
        self._hover.set_visible(False)
        match_interface_style(self.figure, self)
        self.caption.setText(f'{table.num_rows:,} displayed cells · Click a point to inspect its cell; scroll to zoom and drag to pan.')
        self.show(); self.canvas.draw_idle()

    def _row(self, index):
        row = {name: self._table[name][index].as_py() for name in ('sample_id', 'project_entry_id', 'slide_id', 'section_id', 'cell_id')
               if name in self._table.column_names}
        row['cluster_id'] = (self._table['cluster_id'][index].as_py() if 'cluster_id' in self._table.column_names
                             else self._table['colour_value'][index].as_py() if self._cluster_colour else None)
        return row

    def _index_points(self, _event=None):
        if self._table is not None:
            transform = (*self.axis.get_xlim(), *self.axis.get_ylim(), *self.axis.bbox.bounds)
            if transform == self._tree_transform:
                return
            from scipy.spatial import cKDTree
            self._tree = cKDTree(self.axis.transData.transform(self._coordinates))
            self._tree_transform = transform

    def _nearest(self, event):
        if self._tree is None or event.inaxes is not self.axis:
            return None
        distance, index = self._tree.query((event.x, event.y), distance_upper_bound=9)
        return int(index) if np.isfinite(distance) else None

    def _motion(self, event):
        if self._table is None:
            return
        if self._press is not None and event.xdata is not None and event.ydata is not None:
            x, y, xlim, ylim = self._press
            dx = (event.x-x) / self.axis.bbox.width * (xlim[1]-xlim[0])
            dy = (event.y-y) / self.axis.bbox.height * (ylim[1]-ylim[0])
            if abs(event.x-x)+abs(event.y-y) > 3:
                self._dragged = True; self._hover.set_visible(False)
                self.axis.set(xlim=np.asarray(xlim)-dx, ylim=np.asarray(ylim)-dy); self.canvas.draw_idle()
            return
        index = self._nearest(event)
        if index is None:
            if self._hover.get_visible(): self._hover.set_visible(False); self.canvas.draw_idle()
            return
        row = self._row(index); x, y = map(float, self._coordinates[index])
        text = f"UMAP 1: {x!r}\nUMAP 2: {y!r}\n{row['sample_id']} · {row['cluster_id'] or 'Cell'}"
        if self._continuous:
            text += '\n' + str(self._table['measurement'][index].as_py()) + ': ' + str(self._table['colour_value'][index].as_py())
        self._hover.xy = (x, y); self._hover.set_text(text)
        right = event.x > self.axis.bbox.x0 + self.axis.bbox.width*.6
        upper = event.y > self.axis.bbox.y0 + self.axis.bbox.height*.6
        self._hover.set_position((-12 if right else 12, -12 if upper else 12))
        self._hover.set_ha('right' if right else 'left'); self._hover.set_va('top' if upper else 'bottom')
        self._hover.set_visible(True); self.canvas.draw_idle()

    def _pressed(self, event):
        if self._table is not None and event.inaxes is self.axis and event.button == 1:
            self._press = event.x, event.y, self.axis.get_xlim(), self.axis.get_ylim(); self._dragged = False

    def _released(self, event):
        if self._press is None:
            return
        self._press = None
        if not self._dragged:
            index = self._nearest(event)
            if index is not None:
                self._point = index; self._selected.set_offsets(self._coordinates[index:index+1])
                self.cell_selected.emit(self._row(index)); self.canvas.draw_idle()

    def _zoom(self, event):
        if self._table is None or event.inaxes is not self.axis:
            return
        scale = .8 if event.button == 'up' else 1.25
        self.axis.set(xlim=event.xdata+(np.asarray(self.axis.get_xlim())-event.xdata)*scale,
                      ylim=event.ydata+(np.asarray(self.axis.get_ylim())-event.ydata)*scale)
        self.canvas.draw_idle()

    def fit_view(self):
        if self._table is not None:
            self.axis.set(xlim=self._full_limits[0], ylim=self._full_limits[1]); self.canvas.draw_idle()

    def select_cluster(self, cluster_id):
        """Highlight the selected saved cluster without changing any memberships."""
        if self._table is None or cluster_id == self._cluster:
            return
        import pyarrow.compute as pc
        self._cluster = cluster_id
        name = 'cluster_id' if 'cluster_id' in self._table.column_names else 'colour_value' if self._cluster_colour else None
        selected = pc.equal(self._table[name], cluster_id).to_numpy() if name else np.zeros(len(self._coordinates), dtype=bool)
        self._highlight.set_offsets(self._coordinates[selected]); self.canvas.draw_idle()

    def highlight_clusters(self, cluster_ids):
        """Highlight exact review groups while retaining the focused cell and camera."""
        if self._table is None:return
        import pyarrow as pa
        import pyarrow.compute as pc
        name='cluster_id' if 'cluster_id' in self._table.column_names else 'colour_value' if self._cluster_colour else None
        selected=(pc.is_in(self._table[name],value_set=pa.array(tuple(cluster_ids),type=pa.string())).to_numpy()
                  if name else np.zeros(len(self._coordinates),dtype=bool))
        self._highlight.set_offsets(self._coordinates[selected]);self.canvas.draw_idle()
