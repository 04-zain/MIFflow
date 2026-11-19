"""Inspect saved region outlines against a bounded prepared-image overview.

The review service supplies the image asynchronously. We place it in the same
physical coordinates as the outlines; selection links regions to their saved
measurements. Editing the annotations remains in QuPath.
"""
from __future__ import annotations

import math
import json
from collections.abc import Mapping
from pathlib import Path

import numpy as np
from matplotlib.path import Path as PlotPath
from matplotlib.transforms import Affine2D
from PySide6.QtCore import Qt, QEvent, QItemSelectionModel, QSortFilterProxyModel, QRectF, QSize, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPainterPath, QPen, QPixmap, QTransform
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QGraphicsItem, QGraphicsPathItem, QGraphicsSimpleTextItem,
    QHeaderView, QHBoxLayout, QLabel, QListWidget, QMenu, QMessageBox, QPushButton, QSizePolicy,
    QScrollArea, QStyle, QStyleOptionGraphicsItem, QStyleOptionHeader, QTableView, QVBoxLayout, QWidget, QLayout,
)

from mif_pipeline.gui.interactions import FormTable, FormTableRow
from .figure_preview import _FigureView
from .models import FormTableModel
from .workbench import fit_scientific_columns, style_scientific_table
from .wrapping_layout import WrappingLayout


def _polygons(geometry):
    return (geometry['coordinates'],) if geometry['type'] == 'Polygon' else geometry['coordinates']


def _display_paths(records, x_scale, y_scale):
    """Bound display vertices using existing plotting simplification, ring by ring.

    Ring membership, including holes, stays intact. This approximation never
    replaces a record's exact geometry or supplies an area or overlap measure.
    """
    rings = [(row, ring) for row in records for polygon in _polygons(row['geometry']) for ring in polygon]
    count = sum(len(ring) for _, ring in rings)
    if count == 0:
        return {}
    if count > 2_000_000:
        raise ValueError('These outlines exceed the interactive map limit. The complete measurements remain in the table; inspect the outlines in QuPath.')
    bounds = [row.get('bounds_level0') for row in records]
    if all(bound is not None for bound in bounds):
        xmin = min(bound[0] * x_scale for bound in bounds)
        ymin = min(bound[1] * y_scale for bound in bounds)
        xmax = max(bound[2] * x_scale for bound in bounds)
        ymax = max(bound[3] * y_scale for bound in bounds)
    else:
        points = np.concatenate([np.asarray(ring, dtype=float)[:, :2] for _, ring in rings])
        low, high = points.min(axis=0), points.max(axis=0)
        xmin, ymin = low * (x_scale, y_scale)
        xmax, ymax = high * (x_scale, y_scale)
    extent = max(xmax - xmin, ymax - ymin, 1.)
    tolerance = extent / 6000. if count > 60_000 else 0.
    paths = {}
    total = 0
    for row, ring in rings:
        points = np.asarray(ring, dtype=float)[:, :2] * (x_scale, y_scale)
        if tolerance and len(points) > 8:
            plotted = PlotPath(points, closed=True)
            plotted.simplify_threshold = .25
            cleaned = plotted.cleaned(transform=Affine2D().scale(1. / tolerance), simplify=True)
            simplified = cleaned.vertices[(cleaned.codes != PlotPath.STOP) & (cleaned.codes != PlotPath.CLOSEPOLY)] * tolerance
            if len(simplified) >= 4:
                points = simplified
        total += len(points)
        if total > 250_000:
            raise ValueError('These outlines exceed the interactive map limit. The complete measurements remain in the table; inspect the outlines in QuPath.')
        path = paths.setdefault(row['region_id'], QPainterPath())
        path.setFillRule(Qt.FillRule.OddEvenFill)
        path.moveTo(float(points[0, 0]), float(points[0, 1]))
        for x, y in points[1:]:
            path.lineTo(float(x), float(y))
        path.closeSubpath()
    return paths


class _OutlineView(_FigureView):
    """Reuse the saved-figure view's pan/fit/wheel handling for region paths."""
    inspected = Signal(tuple)
    viewport_changed = Signal()
    height_changed = Signal(int)

    def __init__(self, parent=None):
        self._base_scale = 1.
        self.items_by_id = {}
        self.labels = {}
        self.image_item = None
        self.outlines_visible = True
        self._syncing = False
        self._focused_field = None
        super().__init__(QPixmap(), parent)
        self.scene().clear()
        self.setObjectName('region-review-map')
        self.setAccessibleName('Prepared image and region outlines')
        self.setAccessibleDescription('Select an outline to inspect its table row. Control selects multiple outlines. Drag empty space to pan; Control and scroll zooms.')
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        # The scale bar is fixed to the viewport. Scrolling cached pixels would
        # copy it into the image; redraw this bounded overview on each pan.
        self.setViewportUpdateMode(self.ViewportUpdateMode.FullViewportUpdate)
        self.setMinimumSize(260, 200)
        self.scene().selectionChanged.connect(self._selection_changed)
        self.zoom_changed.connect(self._update_labels)
        from .channel_legend import ChannelLegend
        self.channel_legend = ChannelLegend(self.viewport())

    def reset_view(self):
        self._focused_field = None
        super().reset_view()
        self._base_scale = self.transform().m11()
        self._update_labels()

    def zoom_by(self, factor):
        self._focused_field = None
        current = self.transform().m11()
        target = max(self._base_scale / 20, min(self._base_scale * 200, current * factor))
        self._fit = False
        self.scale(target / current, target / current)
        self.zoom_changed.emit(target)

    def _selection_changed(self):
        for region_id, item in self.items_by_id.items():
            item.setPen(QPen(QColor('#ffd56a') if item.isSelected() else item.data(1), 2.5 if item.isSelected() else 1.2))
            pen = item.pen(); pen.setCosmetic(True)
            if item.data(3):
                pen.setStyle(Qt.PenStyle.DashLine)
            item.setPen(pen)
            item.setBrush(QColor(255, 213, 106, 18) if item.isSelected() else item.data(2))
        self._update_labels()
        if not self._syncing:
            self.inspected.emit(tuple(item.data(0) for item in self.scene().selectedItems() if item.data(0)))

    def select_ids(self, ids):
        self._syncing = True
        try:
            wanted = set(ids)
            for region_id, item in self.items_by_id.items():
                item.setSelected(region_id in wanted)
        finally:
            self._syncing = False
        self._selection_changed()

    def focus_selected(self):
        selected = [item for item in self.items_by_id.values() if item.isSelected()]
        if not selected:
            return
        bounds = QRectF()
        for item in selected:
            bounds = bounds.united(item.sceneBoundingRect())
        margin = max(bounds.width(), bounds.height()) * .12
        self._fit = False
        field = bounds.adjusted(-margin, -margin, margin, margin)
        # Section selection changes the viewed field, never the page allocation.
        # Secondary review retains its existing crop-sized panel.
        if self.property('secondaryReview'):
            height = max(360, min(560, round(max(260, self.viewport().width()) * field.height() / max(field.width(), 1))))
            self.setFixedHeight(height)
        self._focused_field = field
        self.fitInView(field, Qt.AspectRatioMode.KeepAspectRatio)
        self.zoom_changed.emit(self.transform().m11())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._focused_field is not None:
            self.fitInView(self._focused_field, Qt.AspectRatioMode.KeepAspectRatio)
            self.zoom_changed.emit(self.transform().m11())
        self.height_changed.emit(event.size().height())

    def mousePressEvent(self, event):
        self._focused_field = None
        super().mousePressEvent(event)

    def _update_labels(self, *_args):
        for region_id, label in self.labels.items():
            item = self.items_by_id[region_id]
            bounds = self.mapFromScene(item.sceneBoundingRect()).boundingRect()
            label.setVisible(self.outlines_visible and (item.isSelected() or (bounds.width() > 45 and bounds.height() > 28)))
        self.viewport().update()

    def set_image(self, image, metadata):
        """Align the returned image with its physical extent and crop origin."""
        first_image = self.image_item is None
        if first_image:
            self.image_item = self.scene().addPixmap(QPixmap.fromImage(image))
            self.image_item.setZValue(-10)
            self.image_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        else:
            self.image_item.setPixmap(QPixmap.fromImage(image))
        width, height = float(metadata['width_um']), float(metadata['height_um'])
        self.image_item.setTransform(QTransform.fromScale(width / image.width(), height / image.height()))
        x, y = float(metadata.get('origin_x_um', 0)), float(metadata.get('origin_y_um', 0))
        self.image_item.setPos(x, y)
        self.image_item.setOpacity(1.)
        self.image_item.show()
        self.setBackgroundBrush(QColor('#172329'))
        self.setSceneRect(QRectF(x, y, width, height))
        if first_image:
            self.reset_view()

    def show_outlines(self, visible):
        self.outlines_visible = visible
        for item in self.items_by_id.values():
            # Hiding a selected QGraphicsItem clears its selection. Opacity
            # preserves the inspected row and viewport while comparing stains.
            item.setOpacity(1. if visible else 0.)
            item.setAcceptedMouseButtons(Qt.MouseButton.LeftButton if visible else Qt.MouseButton.NoButton)
        self._update_labels()

    def scrollContentsBy(self, dx, dy):
        super().scrollContentsBy(dx, dy)
        self.viewport_changed.emit()

    def paintEvent(self, event):
        super().paintEvent(event)
        scale = self.transform().m11()
        if scale <= 0 or (not self.items_by_id and self.image_item is None):
            return
        desired = 85 / scale
        decade = 10 ** math.floor(math.log10(desired))
        length = max(value * decade for value in (1., 2., 5., 10.) if value * decade <= desired)
        painter = QPainter(self.viewport())
        x, y = 14, self.viewport().height() - 20
        painter.fillRect(8, y - 24, max(100, length * scale + 15), 40, QColor(255, 255, 255, 225))
        painter.setPen(QPen(QColor('#253c3e'), 2))
        painter.drawLine(x, y, int(x + length * scale), y)
        painter.drawText(x, y - 7, f'{length / 1000:.3g} mm' if length >= 1000 else f'{length:.3g} µm')


class _RegionOutlineItem(QGraphicsPathItem):
    """Use the selected contour colour without Qt's enclosing selection box."""

    def paint(self, painter, option, widget=None):
        appearance = QStyleOptionGraphicsItem(option)
        appearance.state &= ~QStyle.StateFlag.State_Selected
        super().paint(painter, appearance, widget)


class _InspectedCellOutline(_RegionOutlineItem):
    """Read a cell without changing the selected region or its saved decision."""
    def __init__(self, path, callback):
        super().__init__(path)
        self._inspect = callback
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._inspect()
            event.accept()
        else:
            event.ignore()


class _RegionOverview(QWidget):
    """Locate the detailed view within the same image and exact displayed outlines.

    We reuse the overview pixels and physical contour paths already loaded by
    the review. Clicking a contour selects its table row; the white rectangle
    follows the main view as the researcher pans or zooms.
    """

    def __init__(self, review, parent=None):
        super().__init__(parent)
        self.review = review
        self.setObjectName('region-review-location')
        self.setAccessibleName('Selected section within the whole image')
        self.setMinimumSize(180, 200)
        self.setMaximumWidth(280)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._transform = QTransform()
        review.view.zoom_changed.connect(lambda _scale: self.update())
        review.view.viewport_changed.connect(self.update)

    def paintEvent(self, event):
        view = self.review.view
        bounds = view.sceneRect()
        if bounds.isEmpty():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.fillRect(self.rect(), QColor('#172329'))
        scale = min((self.width() - 16) / bounds.width(), (self.height() - 16) / bounds.height())
        self._transform = QTransform()
        self._transform.translate((self.width() - bounds.width() * scale) / 2,
                                  (self.height() - bounds.height() * scale) / 2)
        self._transform.scale(scale, scale)
        self._transform.translate(-bounds.left(), -bounds.top())
        painter.setTransform(self._transform)
        image = getattr(view, 'overview_image_item', None) or view.image_item
        if image is not None and image.isVisible():
            painter.setOpacity(image.opacity())
            painter.drawPixmap(image.sceneBoundingRect(), image.pixmap(), QRectF(image.pixmap().rect()))
            painter.setOpacity(1.)
        density = getattr(view, 'overview_density_item', None)
        if density is not None:
            painter.drawPixmap(density.sceneBoundingRect(), density.pixmap(), QRectF(density.pixmap().rect()))
        for item in view.items_by_id.values():
            pen = QPen(QColor('#ffd56a') if item.isSelected() else item.data(1), 2 if item.isSelected() else 1)
            pen.setCosmetic(True)
            if item.data(3):
                pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(QColor(255, 213, 106, 25) if item.isSelected() else Qt.BrushStyle.NoBrush)
            painter.drawPath(item.path())
        visible = view.mapToScene(view.viewport().rect()).boundingRect().intersected(bounds)
        pen = QPen(QColor('#ffffff'), 1, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(visible)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            inverse, valid = self._transform.inverted()
            if valid:
                point = inverse.map(event.position())
                matches = [key for key, item in self.review.view.items_by_id.items()
                           if key in self.review._by_id and item.path().contains(point)]
                if matches:
                    self.review.select_region_ids((min(matches, key=lambda key: self.review._by_id[key]['area_um2']),))
                    event.accept()
                    return
        super().mousePressEvent(event)


class _RegionHeader(QHeaderView):
    """Reserve the leading edge for sorting and align every heading with its values."""

    def __init__(self, parent):
        super().__init__(Qt.Orientation.Horizontal, parent)
        assets = Path(__file__).with_name('assets')
        self._marks = {
            Qt.SortOrder.AscendingOrder: QIcon(str(assets / 'sort-ascending.svg')),
            Qt.SortOrder.DescendingOrder: QIcon(str(assets / 'sort-descending.svg')),
        }

    def paintSection(self, painter, rect, index):
        option = QStyleOptionHeader()
        self.initStyleOption(option)
        self.initStyleOptionForIndex(option, index)
        option.rect = rect
        option.sortIndicator = QStyleOptionHeader.SortIndicator.None_
        painter.save()
        self.style().drawControl(QStyle.ControlElement.CE_Header, option, painter, self)
        if self.isSortIndicatorShown() and self.sortIndicatorSection() == index:
            self._marks[self.sortIndicatorOrder()].paint(
                painter, rect.left() + 6, rect.center().y() - 7, 12, 14)
        painter.restore()


class _RegionMeasurements(FormTableModel):

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.TextAlignmentRole and 0 <= section < len(self._columns):
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.ToolTipRole and 0 <= section < len(self._columns):
            description = {
                'Area (mm²)': 'Calibrated region area, excluding holes.',
                'Width (mm)': 'Width of the enclosing rectangle.',
                'Height (mm)': 'Height of the enclosing rectangle.',
                'Section area (%)': 'This region’s area as a percentage of its containing tissue section.',
            }.get(self._columns[section], '')
            return (description + '\n' if description else '') + 'Click the heading to sort; click again to reverse the order.'
        return super().headerData(section, orientation, role)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if index.isValid() and hasattr(self, 'excluded_ids'):
            excluded = self._row_ids[index.row()] in self.excluded_ids
            if self._columns[index.column()] == 'Use in analysis' and role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.UserRole):
                return 'Excluded' if excluded else 'Included'
            if excluded and role == Qt.ItemDataRole.ForegroundRole:
                return QColor('#687c80')
        if index.isValid() and role == Qt.ItemDataRole.TextAlignmentRole:
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        value = super().data(index, Qt.ItemDataRole.UserRole)
        if index.isValid() and isinstance(value, float):
            if role == Qt.ItemDataRole.DisplayRole:
                return f'{value:.4g}'
        return super().data(index, role)


class RegionDecisionReview(QWidget):
    """One linked outline map/table with no scientific mutation on selection."""
    image_requested = Signal()
    appearance_requested = Signal(object)
    def __init__(self, projection: Mapping, parent=None, *, allow_exclusion=False):
        super().__init__(parent)
        self.setObjectName('region-decision-review')
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.projection = projection
        self._records = tuple(projection['regions'])
        choosing_regions = projection['kind'] == 'analysis_domains' and projection.get('selection_mode') == 'choose_regions'
        analysis = projection['kind'] == 'analysis_domains' and not choosing_regions
        count_selection = projection['kind'] == 'secondary_selection'
        secondary = projection['kind'] in {'secondary', 'secondary_selection'}
        self._bounded_workspace = not secondary
        self._workspace_update_pending = False
        self._scroll_viewport = None
        self._rows = tuple(row for row in self._records if row.get('layer') == ('secondary' if secondary else 'analysis' if analysis else 'tissue'))
        self._by_id = {row['region_id']: row for row in self._rows}
        self._excluded = set()
        self._allow_exclusion = bool(allow_exclusion and not analysis and not secondary)
        self._syncing = False
        self._image_attribute = 'cluster_id'
        self._image_path = None
        self._has_rgb = False
        self._initial_shown = False
        self._appearance_store = None
        self._store_checked_uri = None
        self._image_metadata = None
        self._applied_display = None
        self._pending_display = None
        self._save_pending_display = False
        self._review_channels = None
        self._pending_review_channels = None
        self._cell_items = []
        self._cell_revision = 0
        self._pending_cell_request = None
        self._cell_request_active = False
        self._last_cell_field = None
        self._cell_timer = QTimer(self)
        self._cell_timer.setSingleShot(True)
        self._cell_timer.setInterval(250)
        self._cell_timer.timeout.connect(self._request_cells)
        layout = QVBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(6 if not secondary else 8)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        noun = ('analysis region' if analysis else 'tissue section') + ('' if len(self._rows) == 1 else 's')
        self.summary = QLabel(f'{len(self._rows)} {noun} · {sum(row["area_um2"] for row in self._rows) / 1_000_000:.4g} mm² total area', self)
        if choosing_regions:
            self.summary.setText(f'{len(self._rows)} accepted {noun}')
        self.summary.setProperty('panelHeading', True); self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.summary.setVisible(not secondary)
        summary_actions = QWidget(self)
        summary_actions.setObjectName('region-review-inclusion-tools')
        summary_tools = WrappingLayout(summary_actions)
        layout.addWidget(summary_actions)
        summary_actions.setVisible(self._allow_exclusion)
        image_tools_panel = QWidget(self)
        image_tools_panel.setObjectName('region-review-image-tools')
        image_tools = QHBoxLayout() if secondary else WrappingLayout(image_tools_panel)
        self.appearance_button = QPushButton('Image appearance…', self)
        self.appearance_button.setEnabled(False)
        self.appearance_button.clicked.connect(lambda: self.appearance_requested.emit(self._image_metadata))
        self.appearance_summary = QLabel('', self)
        self.appearance_summary.setWordWrap(True)
        self.appearance_panel = QWidget(self)
        appearance_layout = QVBoxLayout(self.appearance_panel)
        appearance_layout.setContentsMargins(0, 0, 0, 0)
        appearance_layout.setSpacing(0)
        appearance_layout.addWidget(self.appearance_summary)
        self.appearance_legend = QWidget(self.appearance_panel)
        self.appearance_legend.setAccessibleName('Displayed channel colors')
        self._legend_layout = WrappingLayout(self.appearance_legend)
        appearance_layout.addWidget(self.appearance_legend)
        self.appearance_legend.hide()
        self.appearance_button.hide()
        if count_selection:
            image_tools.addStretch()
        self.outlines = QCheckBox('Region outlines' if secondary and not count_selection else 'Show outlines', self)
        self.outlines.setChecked(True)
        image_tools.addWidget(self.outlines)
        self.cells = QCheckBox('Cells', self)
        self.cells.setAccessibleName('Inspect saved segmented cells')
        self.cells.setToolTip('Show saved cell and nuclear outlines in this field and analysis domain. Cells may lie outside the outlined region. Click a cell for its saved phenotype.')
        self.cells.setEnabled(False)
        self.cells.setVisible(projection['kind'] == 'secondary')
        self.cells.toggled.connect(self._toggle_cells)
        if projection['kind'] == 'secondary':
            image_tools.addWidget(self.cells)
        self.cell_status = QLabel('', self)
        self.cell_status.setWordWrap(True)
        self.cell_status.setProperty('choiceHelp', True)
        self.cell_status.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.cell_status.setTextFormat(Qt.TextFormat.PlainText)
        self.cell_status.hide()
        self.image_status = QLabel('Loading image…', self)
        self.image_status.setWordWrap(True)
        self.image_status.setProperty('choiceHelp', True)
        split = QWidget(self)
        split.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        split_layout = QHBoxLayout(split)
        split_layout.setContentsMargins(0, 0, 0, 0)
        split_layout.setSpacing(8)
        split_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.view = _OutlineView(split)
        if not secondary:
            self.view.setFixedHeight(400)
        elif count_selection:
            self.view.setFixedHeight(320)
            self.view.setAccessibleName('Accepted structures on the prepared image')
        self.view.channel_legend.changed.connect(lambda display:self.set_display_settings(display,save=True))
        self.view.viewport_changed.connect(self._queue_cells)
        self.view.zoom_changed.connect(self._queue_cells)
        self.view.height_changed.connect(self._queue_cells)
        self.view.setProperty('secondaryReview', secondary and not count_selection)
        self.outlines.toggled.connect(self.view.show_outlines)
        self.table = QTableView(self); self.table.setObjectName('region-review-table')
        self.table.setHorizontalHeader(_RegionHeader(self.table))
        self.table.setAccessibleName('Analysis regions' if analysis else 'Tissue sections')
        section_numbers = {key: row.get('display_number') or row.get('display_label', '—')
                           for row in self._records if row.get('layer') == 'tissue'
                           for key in (row.get('region_id'), row.get('roi_id'), row.get('section_id')) if key is not None}
        has_fraction = any(row.get('section_fraction') is not None for row in self._rows)
        has_edits = not analysis and any(row.get('edited') for row in self._rows)
        columns = ('Region', 'Section', 'Area (mm²)', 'Width (mm)', 'Height (mm)') if analysis else ('Section', 'Area (mm²)', 'Width (mm)', 'Height (mm)')
        if has_edits:
            columns += ('Edited',)
        if has_fraction:
            columns += ('Section area (%)',)
        if self._allow_exclusion:
            columns += ('Use in analysis',)
        rows = []
        for row in self._rows:
            values = (
                row.get('display_number') if row.get('display_number') is not None else row.get('display_label', '—'),
                row.get('parent_display_number') or section_numbers.get(row.get('parent_region_id'), section_numbers.get(row.get('section_id'), '—')) if analysis else row.get('display_number'),
                row['area_um2'] / 1_000_000, row['width_um'] / 1000, row['height_um'] / 1000,
            )
            if not analysis:
                values = (values[0], *values[2:])
                if has_edits:
                    values += ('Yes' if row.get('edited') else '—',)
            if has_fraction:
                values += (row['section_fraction'] * 100 if row.get('section_fraction') is not None else '—',)
            if self._allow_exclusion:
                values += ('Included',)
            rows.append(FormTableRow(row['region_id'], values))
        rows = tuple(rows)
        self._source = _RegionMeasurements(FormTable('region_review', columns, rows), self.table)
        self._source.excluded_ids = self._excluded
        self._proxy = QSortFilterProxyModel(self.table); self._proxy.setSourceModel(self._source); self._proxy.setSortRole(Qt.ItemDataRole.UserRole)
        self.table.setModel(self._proxy); self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows); self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        style_scientific_table(self.table); fit_scientific_columns(self.table)
        self.table.setStyleSheet(
            'QTableView[scientificTable="true"] { border: none; border-top: 1px solid #c7d4d3; '
            'border-bottom: 1px solid #c7d4d3; border-radius: 0; padding: 0; }'
        )
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Fixed if self._bounded_workspace else QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setDefaultSectionSize(38)
        self.table.setSortingEnabled(True); self.table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        if self._bounded_workspace:
            self.table.viewport().installEventFilter(self)
            self.table.horizontalHeader().sortIndicatorChanged.connect(self._schedule_workspace_size)
        if analysis:
            self.table.setWordWrap(True)
            self.table.setColumnWidth(0, 180)
            self.table.resizeRowsToContents()
        self.table.setMinimumSize(240, 80)
        self.table.setFixedHeight(min(230, 44 + max(1, len(rows)) * 38))
        layout.addWidget(self.table)
        self.table.setVisible(not secondary)
        if self._allow_exclusion:
            self.exclude_button = QPushButton('Exclude selected sections', self)
            self.exclude_button.setObjectName('region-review-exclude')
            self.exclude_button.clicked.connect(lambda: self._change_inclusion(False))
            self.restore_button = QPushButton('Restore selected sections', self)
            self.restore_button.setObjectName('region-review-restore')
            self.restore_button.clicked.connect(lambda: self._change_inclusion(True))
            summary_tools.addWidget(self.exclude_button)
            summary_tools.addWidget(self.restore_button)
            self.inclusion_message = QLabel('', self)
            self.inclusion_message.setProperty('validationMessage', True)
            self.inclusion_message.setWordWrap(True)
            self.inclusion_message.hide()
            layout.addWidget(self.inclusion_message)
        self.selection_summary = QLabel('Select a row to inspect its contour and staining.', self)
        self.selection_summary.setObjectName('region-review-selection')
        self.selection_summary.setWordWrap(True)
        self.selection_summary.setProperty('panelHeading', True)
        if not secondary:
            # Selection describes the image field. Keep the sentence readable
            # across the page instead of sharing its width with five tools.
            layout.addWidget(self.selection_summary)
        self.selection_summary.setVisible(not secondary)
        if secondary:
            image_tools_panel.hide()
            layout.addWidget(self.appearance_panel)
        else:
            layout.addWidget(image_tools_panel)
            self.appearance_panel.hide()
        layout.addWidget(self.image_status)
        detail_panel = QWidget(split)
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(6)
        detail_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        detail_title = QLabel('Region image' if secondary else 'Selected region' if analysis else 'Selected section', detail_panel)
        detail_title.setProperty('panelHeading', True)
        detail_layout.addWidget(detail_title)
        detail_title.hide()
        detail_layout.addWidget(self.view)
        secondary_legend = 'Yellow: selected structures' if count_selection else 'Yellow: selected region'
        if any(record.get('layer') == 'context' for record in self._records):
            secondary_legend += ' · Gray: parent outline'
        detail_hint = QLabel(secondary_legend if secondary else 'Select a row to focus; drag the image to pan; Ctrl + scroll to zoom.', detail_panel)
        detail_hint.setWordWrap(True)
        detail_hint.setProperty('choiceHelp', True)
        if secondary and not count_selection:
            footer_labels = QVBoxLayout()
            footer_labels.setSpacing(2)
            footer_labels.addWidget(self.cell_status)
            footer_labels.addWidget(detail_hint)
            image_tools.insertLayout(0, footer_labels)
            detail_hint.setMinimumWidth(0)
            detail_hint.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            image_tools.setStretch(0, 1)
        elif secondary:
            detail_layout.addWidget(detail_hint)
        detail_layout.addStretch()
        location_panel = QWidget(split)
        location_layout = QVBoxLayout(location_panel)
        location_layout.setContentsMargins(0, 0, 0, 0)
        location_layout.setSpacing(6)
        location_title = QLabel('Crop overview' if secondary else 'Location in image', location_panel)
        location_title.setProperty('panelHeading', True)
        location_layout.addWidget(location_title)
        location_title.setVisible(secondary)
        self.overview = _RegionOverview(self, location_panel)
        if secondary:
            self.overview.setMinimumSize(120, 120)
            self.overview.setFixedSize(120, 120)
            self.overview.setAccessibleName('Detailed view within the image crop')
        else:
            self.overview.setFixedHeight(self.view.height())
            self.view.height_changed.connect(self.overview.setFixedHeight)
        location_layout.addWidget(self.overview)
        location_hint = QLabel('White frame: current view.', location_panel)
        location_hint.setProperty('choiceHelp', True)
        location_hint.setWordWrap(secondary)
        if secondary:
            location_layout.addWidget(location_hint)
        location_layout.addStretch()
        location_panel.setMaximumWidth(130 if secondary else 280)
        split_layout.addWidget(detail_panel, 3)
        if secondary:
            self.overview.setParent(self.view.viewport())
            self.overview.setFixedSize(96, 96)
            self.overview.setToolTip('Crop overview · white frame marks the current view')
            self.view.viewport().installEventFilter(self)
            self.overview.show()
            self.view.channel_legend.geometry_changed.connect(self._position_overview)
            location_panel.hide()
            self.view.setToolTip('Drag to pan; Ctrl + scroll to zoom. The inset shows the current view within the image crop.')
        else:
            split_layout.addWidget(location_panel, 1)
        layout.addWidget(split)
        if not secondary:
            hints = QHBoxLayout()
            hints.setSpacing(split_layout.spacing())
            hints.addWidget(detail_hint, 3)
            location_hint.setMaximumWidth(location_panel.maximumWidth())
            hints.addWidget(location_hint, 1)
            layout.addLayout(hints)
        if secondary:
            layout.addLayout(image_tools)
            if count_selection:
                layout.addWidget(self.cell_status)
        tools = QHBoxLayout()
        self.export_scope = QComboBox(self)
        self.export_scope.setAccessibleName('Sections shown in exported figures')
        self.export_scope.addItem('Included and excluded sections', True)
        self.export_scope.addItem('Included sections only', False)
        self.export_scope.hide()
        for title, name, callback in (('Fit image', 'fit-all', self.view.reset_view), ('Export image…', 'export-image', self._export_image), ('Outline figure…', 'export-map', self._export_map), ('Export data…', 'export-data', self._export_data)):
            button = QPushButton(title, self); button.setObjectName('region-review-' + name); button.clicked.connect(callback); tools.addWidget(button)
            if not secondary:
                tools.removeWidget(button)
                image_tools.addWidget(button)
                if name == 'fit-all':
                    self.outlines.setMinimumHeight(button.sizeHint().height())
            if secondary:
                if name == 'fit-all':
                    tools.removeWidget(button)
                    image_tools.insertWidget(image_tools.indexOf(self.outlines), button)
                else:
                    button.hide()
            if name == 'export-image':
                self._image_export_button = button
                button.setEnabled(False)
        if secondary:
            tools.addStretch(); layout.addLayout(tools)
        note = QLabel('', self)
        note.hide()
        note.setProperty('choiceHelp', True); note.setWordWrap(True); layout.addWidget(note)
        self.table.selectionModel().selectionChanged.connect(self._table_selected)
        self.table.doubleClicked.connect(lambda _index: self.view.focus_selected())
        self.view.inspected.connect(self.select_region_ids)
        try:
            paths = _display_paths(self._records, float(projection['x_um_per_px']), float(projection['y_um_per_px']))
            bounds = QRectF()
            for record in self._records:
                region_id = record['region_id']; path = paths.get(region_id)
                if path is None:
                    continue
                item = _RegionOutlineItem(path); item.setData(0, region_id)
                active = region_id in self._by_id
                colour = QColor('#32d4df') if active else QColor('#a5afad')
                fill = QColor(50, 212, 223, 1)
                item.setData(1, colour); item.setData(2, fill); item.setBrush(fill)
                pen = QPen(colour, 1.2); pen.setCosmetic(True); item.setPen(pen)
                item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, active)
                item.setZValue(1 if record.get('layer') in {'analysis', 'secondary'} else 0)
                item.setToolTip(f'{record.get("display_label", region_id)}\n{record["area_um2"] / 1_000_000:.4g} mm²')
                self.view.scene().addItem(item); self.view.items_by_id[region_id] = item; bounds = bounds.united(item.boundingRect())
                if active:
                    label = QGraphicsSimpleTextItem(str(record['display_number']) if record.get('display_number') is not None else '', item)
                    label.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
                    label.setAcceptedMouseButtons(Qt.MouseButton.NoButton); label.setPos(item.boundingRect().topLeft())
                    label.setBrush(QColor('#32d4df')); self.view.labels[region_id] = label
            margin = max(bounds.width(), bounds.height()) * .03
            self.view.setSceneRect(bounds.adjusted(-margin, -margin, margin, margin)); self.view.reset_view()
        except (KeyError, TypeError, ValueError) as error:
            self.view.hide(); self.findChild(QPushButton, 'region-review-fit-all').hide()
            note.show()
            note.setText(str(error) if isinstance(error, ValueError) else 'Outline geometry is unavailable in this view. Review the saved regions in QuPath.')
        self._add_conflicts(layout)
        if self._rows and not count_selection:
            selected = projection.get('selected_region_id') if secondary else None
            self.select_region_ids((selected or max(self._rows, key=lambda row: row['area_um2'])['region_id'],))

    def image_options(self):
        if self._cell_request_active and self._pending_cell_request is not None:
            return {'region_cells': dict(self._pending_cell_request)}
        return {'attribute': self._image_attribute}

    def _clear_cells(self):
        self.cell_status.setToolTip('')
        self.cell_status.setAccessibleDescription('')
        self.cell_status.setStyleSheet('')
        for item, _index in self._cell_items:
            self.view.scene().removeItem(item)
        self._cell_items.clear()

    def _toggle_cells(self, checked):
        self._cell_revision += 1
        self._cell_timer.stop()
        self._pending_cell_request = None
        self._cell_request_active = False
        self._last_cell_field = None
        self._clear_cells()
        self.cell_status.hide()
        if checked:
            self._queue_cells()

    def _queue_cells(self, *_args):
        if not self.cells.isChecked() or self._image_metadata is None or self._pending_display is not None:
            return
        metadata = self._image_metadata
        image = QRectF(float(metadata.get('origin_x_um', 0)), float(metadata.get('origin_y_um', 0)),
                       float(metadata['width_um']), float(metadata['height_um']))
        field = self.view.mapToScene(self.view.viewport().rect()).boundingRect().intersected(image)
        if field.isEmpty():
            return
        bounds = [float(value) for value in (field.left(), field.top(), field.right(), field.bottom())]
        if self._last_cell_field == bounds:
            return
        self._last_cell_field = bounds
        self._cell_revision += 1
        self._pending_cell_request = dict(field_um=bounds, request_key=self._cell_revision)
        self._cell_request_active = True
        self._clear_cells()
        self.cell_status.setText('Loading cells…')
        self.cell_status.show()
        self._cell_timer.start()

    def _request_cells(self):
        if self.cells.isChecked() and self._pending_cell_request is not None:
            self.image_requested.emit()

    def set_cell_artifact(self, artifact):
        request = (artifact.display_options or {}).get('region_cells')
        if not self.cells.isChecked() or not self._cell_request_active or request != self._pending_cell_request:
            return True  # Superseded fields are harmless, not a failed image load.
        try:
            if Path(artifact.path).stat().st_size > 16 * 1024**2:
                raise ValueError('The cell overlay exceeds the display allowance.')
            with np.load(artifact.path, allow_pickle=False) as archive:
                bundle = {name: archive[name] for name in archive.files}
            metadata = json.loads(str(bundle['metadata']))
            if metadata['request'] != request:
                return True
            self._install_cells(bundle, metadata)
        except (OSError, KeyError, TypeError, ValueError) as error:
            self._clear_cells()
            self.cell_status.setText('Could not display saved cells: ' + str(error))
            self.cell_status.show()
        return True

    def _install_cells(self, bundle, metadata):
        self._clear_cells()
        if not metadata['outline_detail_available']:
            self.cell_status.setText(metadata.get('message') or 'Zoom in to see cells.')
            self.cell_status.show()
            return
        rows = metadata['visible_cells']
        for ring, index in enumerate(bundle['outline_cell_indices']):
            start, stop = bundle['outline_offsets'][ring:ring+2]
            points = bundle['outline_vertices_um'][start:stop]
            if not len(points):
                continue
            path = QPainterPath(); path.moveTo(*map(float, points[0]))
            for point in points[1:]:
                path.lineTo(*map(float, point))
            path.closeSubpath()
            item = _InspectedCellOutline(path, lambda selected=int(index): self._inspect_saved_cell(rows, selected))
            color = QColor('#7ee3cf' if str(bundle['outline_kinds'][ring]) == 'cell' else '#b9dfed')
            color.setAlpha(180)
            pen = QPen(color, 1.); pen.setCosmetic(True); item.setPen(pen)
            item.setData(1, color)
            item.setZValue(4)
            item.setToolTip(str(rows[int(index)]['phenotype'] or 'No saved phenotype'))
            self.view.scene().addItem(item)
            self._cell_items.append((item, int(index)))
        self.cell_status.setText('Click a cell for its phenotype.' if rows else 'No saved cells in this field.')
        self.cell_status.show()

    def _inspect_saved_cell(self, rows, selected):
        row = rows[selected]
        for item, index in self._cell_items:
            pen = QPen(QColor('#ffffff') if index == selected else item.data(1), 2. if index == selected else 1.)
            pen.setCosmetic(True); item.setPen(pen)
        self.cell_status.setText('Phenotype: ' + str(row['phenotype']) if row['phenotype'] is not None else 'No saved phenotype for this cell.')
        self.cell_status.setToolTip('Cell ' + str(row['cell_id']) + ' · Section ' + str(row['section_id']))
        self.cell_status.setAccessibleDescription(self.cell_status.toolTip())
        self.cell_status.setStyleSheet('color:#005d61; font-weight:700;')

    def showEvent(self, event):
        super().showEvent(event)
        if self._bounded_workspace:
            parent = self.parentWidget()
            while parent is not None and not isinstance(parent, QScrollArea):
                parent = parent.parentWidget()
            if parent is not None and parent.viewport() is not self._scroll_viewport:
                if self._scroll_viewport is not None:
                    self._scroll_viewport.removeEventFilter(self)
                self._scroll_viewport = parent.viewport()
                self._scroll_viewport.installEventFilter(self)
            self._schedule_workspace_size()
        if not self._initial_shown:
            self._initial_shown = True
            QTimer.singleShot(0, self.view.focus_selected)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._bounded_workspace:
            self._schedule_workspace_size()

    def _schedule_workspace_size(self, *_args):
        if not self._workspace_update_pending:
            self._workspace_update_pending = True
            QTimer.singleShot(0, self._size_workspace)

    def _size_workspace(self):
        """Reserve a useful image field and scroll measurements inside the table."""
        self._workspace_update_pending = False
        if not self.isVisible() or not self._bounded_workspace:
            return
        self._fit_measurement_columns()
        available = self.height()
        review_height = self.layout().totalHeightForWidth(self.width())
        if review_height < 0:
            review_height = self.layout().sizeHint().height()
        if self._scroll_viewport is not None:
            scroll = self._scroll_viewport.parentWidget()
            content = scroll.widget()
            other_height = 0
            if content is not self and content.layout() is not None:
                content_height = content.layout().totalHeightForWidth(content.width())
                if content_height < 0:
                    content_height = content.layout().sizeHint().height()
                other_height = content_height - review_height
            available = self._scroll_viewport.height() - max(0, other_height)
        chrome = review_height - self.view.height() - self.table.height()
        row_height = self.table.verticalHeader().defaultSectionSize()
        header_height = self.table.horizontalHeader().height() + 2 * self.table.frameWidth()
        if self.table.horizontalScrollBar().isVisible():
            header_height += self.table.horizontalScrollBar().height()
        visible_rows = min(3, max(1, len(self._rows)))
        while visible_rows > 1 and available - chrome - header_height - visible_rows * row_height < 320:
            visible_rows -= 1
        table_height = header_height + visible_rows * row_height
        image_height = max(280, min(480, available - chrome - table_height))
        if self.table.height() != table_height:
            self.table.setFixedHeight(table_height)
        if self.view.height() != image_height:
            self.view.setFixedHeight(image_height)

    def _fit_measurement_columns(self):
        """Share spare width after each scientific heading and sort mark fit."""
        header = self.table.horizontalHeader()
        widths = []
        for column in range(self._proxy.columnCount()):
            option = QStyleOptionHeader()
            header.initStyleOptionForIndex(option, column)
            text_size = QSize(option.fontMetrics.horizontalAdvance(option.text), option.fontMetrics.height())
            width = header.style().sizeFromContents(
                QStyle.ContentsType.CT_HeaderSection, option, text_size, header).width()
            if header.isSortIndicatorShown() and header.sortIndicatorSection() == column:
                width += 23  # Leading margin, direction arrow and separation from the heading.
            widths.append(max(header.minimumSectionSize(), width))
        if not widths:
            return
        extra, remainder = divmod(max(0, self.table.viewport().width() - sum(widths)), len(widths))
        for column, minimum in enumerate(widths):
            width = minimum + extra + (column < remainder)
            if header.sectionSize(column) != width:
                header.resizeSection(column, width)

    def excluded_roi_ids(self):
        """Return the candidate ROI IDs omitted when this review is accepted."""
        return [row['roi_id'] for row in self._rows if row['region_id'] in self._excluded]

    def restore_excluded_roi_ids(self, ids):
        """Restore this review's draft without changing candidate geometry."""
        offered = {row['roi_id']: row['region_id'] for row in self._rows}
        if not self._allow_exclusion or not set(ids).issubset(offered):
            raise ValueError('Choose sections from the current tissue review.')
        self._excluded.clear()
        self._excluded.update(offered[key] for key in ids)
        self._refresh_inclusion()

    def _change_inclusion(self, include):
        selected = set(self.selected_region_ids())
        proposed = self._excluded - selected if include else self._excluded | selected
        if len(proposed) == len(self._rows):
            self.inclusion_message.setText('Keep at least one tissue section. To replace all detections, change the tissue settings and run detection again.')
            self.inclusion_message.show()
            return
        self._excluded.clear()
        self._excluded.update(proposed)
        self._refresh_inclusion()

    def _refresh_inclusion(self):
        self.inclusion_message.hide()
        remaining = [row for row in self._rows if row['region_id'] not in self._excluded]
        self.summary.setText(f'{len(remaining)} included; {len(self._excluded)} excluded; '
                             f'{sum(row["area_um2"] for row in remaining) / 1_000_000:.4g} mm² included area')
        for key, item in self.view.items_by_id.items():
            if key in self._by_id:
                item.setData(3, key in self._excluded)
                item.setData(1, QColor('#8d999c') if key in self._excluded else QColor('#32d4df'))
        if self._rows:
            self._source.dataChanged.emit(self._source.index(0, 0),
                self._source.index(len(self._rows) - 1, len(self._source._columns) - 1))
        self._table_selected()

    def _export_projection(self, *, for_figure=False):
        """Keep complete data exports; let figure exports omit excluded sections."""
        if not self._allow_exclusion:
            return self.projection
        rows = [{**row, 'included': row['region_id'] not in self._excluded} for row in self._records]
        if for_figure and not self.export_scope.currentData():
            rows = [row for row in rows if row['included']]
        return {**self.projection, 'regions': rows}

    def display_settings(self):
        """Return the fully resolved appearance of the displayed image, not staged edits."""
        return json.loads(json.dumps(self._applied_display)) if self._applied_display is not None else None

    def set_display_settings(self, descriptor, *, save=False, selection_changed=False):
        """Apply a display recipe through the existing bounded image request."""
        if self._image_metadata is None:
            return
        self._cell_request_active = False
        self._cell_timer.stop()
        self._last_cell_field = None
        self._save_pending_display = save
        self._pending_review_channels = list(descriptor['channels']) if selection_changed else None
        self._pending_display = json.loads(json.dumps(descriptor))
        self._image_export_button.setEnabled(False)
        self._image_attribute = 'region-display:' + json.dumps(descriptor, separators=(',', ':'), sort_keys=True)
        self.image_status.setText('Loading image…'); self.image_status.show()
        if self.view.image_item is not None:
            self.view.image_item.setOpacity(.25)
        self.overview.update()
        self.image_requested.emit()

    def set_appearance_store(self, path):
        """Connect the existing per-image display file shared with native review."""
        self._appearance_store = path
        self._store_checked_uri = None
        self.refresh_appearance()

    @Slot(str)
    def refresh_appearance(self, changed_uri=None):
        if self._appearance_store is None or self._image_metadata is None:
            return
        uri = self._image_metadata.get('image_uri')
        if not uri:
            return
        if changed_uri is not None and changed_uri.rstrip('/') != uri.rstrip('/'):
            return
        from mif_pipeline.gui.image_appearance import load_image_appearance
        saved = load_image_appearance(self._appearance_store, uri)
        self._store_checked_uri = uri
        if saved is not None and saved != self._applied_display:
            if changed_uri is not None:
                self.set_display_settings(saved,selection_changed=True)
            else:
                permitted=self._review_channels or self._applied_display['channels']
                visible=[c for c in permitted if c in saved.get('channels',())]
                if visible:
                    restored={**saved,'channels':visible,'mode':'composite' if len(visible)>1 else ('grayscale' if saved.get('mode')=='grayscale' else 'colour')}
                    if restored!=self._applied_display:self.set_display_settings(restored)

    def eventFilter(self, watched, event):
        if watched is self._scroll_viewport and event.type() == QEvent.Type.Resize:
            self._schedule_workspace_size()
        if watched is self.table.viewport() and event.type() == QEvent.Type.Resize:
            self._schedule_workspace_size()
        if watched is self.view.viewport() and event.type() == QEvent.Type.Resize:
            self._position_overview()
        return super().eventFilter(watched, event)

    def _position_overview(self):
        viewport=self.view.viewport();legend=self.view.channel_legend
        right=max(0,viewport.width()-self.overview.width()-10)
        narrow=legend.isVisible() and right<legend.geometry().right()+10
        self.overview.move(right,max(10,viewport.height()-self.overview.height()-10) if narrow else 10)
        self.overview.raise_()

    def set_image_artifacts(self, artifacts):
        """Place the returned image with its exact resolved display and calibration."""
        artifact = next((item for item in artifacts if item.artifact_id == 'region-image'
                         and item.status == 'available'), None)
        if artifact is None:
            return
        if artifact.path == self._image_path and self._pending_display is None:
            self.view.image_item.setOpacity(1.); self.view.image_item.show()
            self.image_status.hide(); self.overview.update()
            self._image_export_button.setEnabled(True)
            return
        image = QImage(str(artifact.path))
        try:
            if image.isNull():
                raise ValueError('The image overview could not be opened.')
            metadata = json.loads(image.text('mif-region-image'))
            returned=metadata.get('display')
            pending=self._pending_display
            if pending is not None and returned is not None:
                matches=(pending['channels']==returned['channels'] and pending['mode']==returned['mode'] and
                    all(returned.get(key,{}).get(str(c))==pending.get(key,{}).get(str(c))
                        for key in ('colors','limits') for c in pending['channels'] if str(c) in pending.get(key,{})))
                if not matches:return
            first_image = self.view.image_item is None
            self.view.set_image(image, metadata)
            if first_image:
                self.view.focus_selected()
            self._image_path = artifact.path
            self._image_metadata = metadata
            self.cells.setEnabled(True)
            display = metadata.get('display')
            if display is None:
                selected = metadata['selected_index']
                display = dict(mode=metadata['display_mode'], channels=[0,1,2] if selected == -1 else [selected],
                               colors={}, limits={str(selected):metadata['display_limits']} if 'display_limits' in metadata else {})
            prior = self._pending_display or self._applied_display or {}
            known = dict(display)
            known['colors'] = {str(c['index']): c['color'] for c in metadata['channels']}
            known['colors'].update(prior.get('colors', {})); known['colors'].update(display['colors'])
            known['limits'] = {**prior.get('limits', {}), **display['limits']}
            known['channel_ids'] = [channel.get('id') for channel in metadata['channels']]
            self._applied_display = known
            if self._pending_review_channels is not None:self._review_channels=self._pending_review_channels
            self._pending_review_channels=None
            save_requested = self._save_pending_display
            self._save_pending_display = False
            self._pending_display = None
            self.appearance_button.setEnabled(bool(metadata.get('image_uri')))
            self._show_channel_legend(metadata, known)
            self.image_status.hide(); self.overview.update()
            self._image_export_button.setEnabled(True)
            self._queue_cells()
            uri = metadata.get('image_uri')
            if self._appearance_store is not None and uri:
                if self._store_checked_uri != uri:
                    self.refresh_appearance()
                    if self._pending_display is not None:
                        return
                from mif_pipeline.gui.image_appearance import save_image_appearance
                try:
                    if save_requested:
                        save_image_appearance(self._appearance_store, uri, known)
                except OSError as error:
                    self.image_status.setText('Could not save image appearance: ' + str(error))
                    self.image_status.show()
        except (ValueError, KeyError, TypeError, ZeroDivisionError) as error:
            self.set_image_error(str(error) or 'The image overview could not be opened. Inspect the regions in QuPath.')

    def _show_channel_legend(self, metadata, display):
        self.appearance_panel.hide()
        if self._review_channels is None:self._review_channels=list(display['channels'])
        channels={c['index']:c for c in metadata['channels']}
        self.view.channel_legend.set_display([channels[i] for i in self._review_channels if i in channels],display,
            native_rgb=metadata.get('native_rgb',False))

    def _export_image(self):
        if self._image_metadata is None or self._image_path is None or self._pending_display is not None:
            return
        if self._excluded:
            menu = QMenu(self._image_export_button)
            choices = []
            for index in range(self.export_scope.count()):
                action = menu.addAction(self.export_scope.itemText(index))
                action.setCheckable(True)
                action.setChecked(index == self.export_scope.currentIndex())
                choices.append(action)
            choice = menu.exec(self._image_export_button.mapToGlobal(self._image_export_button.rect().bottomLeft()))
            menu.deleteLater()
            if choice is None:
                return
            self.export_scope.setCurrentIndex(choices.index(choice))
        from mif_pipeline.orchestration.region_review_figures import region_image_figure
        from .figure_export import export_figure
        visible = self.view.mapToScene(self.view.viewport().rect()).boundingRect()
        viewport = [visible.left(), visible.top(), visible.right(), visible.bottom()]
        selected = list(self.selected_region_ids())
        labels = [key for key, label in self.view.labels.items() if label.isVisible()]
        figure = region_image_figure(self._export_projection(for_figure=True), self._image_path, self._image_metadata,
                                     viewport_um=viewport, selected_ids=selected, show_outlines=self.outlines.isChecked(),
                                     label_ids=labels)
        description = json.dumps(dict(image=self._image_metadata, display=self.display_settings(),
                                      viewport_um=viewport, selected_region_ids=selected, label_region_ids=labels,
                                      show_outlines=self.outlines.isChecked()), separators=(',', ':'))
        try:
            export_figure(self, figure, 'region-image.svg', description=description)
        finally:
            figure.clear()

    def set_image_error(self, message):
        if self._cell_request_active:
            self._clear_cells()
            self.cell_status.setText(message)
            self.cell_status.show()
            return
        self._image_export_button.setEnabled(False)
        if self.view.image_item is not None:
            self.view.image_item.hide()
        self.image_status.setText(message)
        self.image_status.show()
        self.overview.update()

    def _export_map(self):
        import tempfile
        from pathlib import Path
        from PySide6.QtGui import QPixmap
        from mif_pipeline.cell_analysis.figure_style import OUTLINE_PALETTES
        from mif_pipeline.orchestration.region_review_figures import region_overview_figure
        from .figure_export import export_figure, save_figure
        from .figure_preview import FigurePreview

        projection = self._export_projection(for_figure=True)
        complete_projection = self._export_projection()
        has_scope = bool(self._excluded)
        temporary = tempfile.TemporaryDirectory(prefix='mif-outlines-')
        path = Path(temporary.name) / 'outlines.png'
        state = {'figure': region_overview_figure(projection)}
        save_figure(state['figure'], path)
        dialog = FigurePreview(path, self)
        controls = QHBoxLayout()
        if has_scope:
            scope = QComboBox(dialog)
            scope.setAccessibleName('Sections shown in exported figures')
            for index in range(self.export_scope.count()):
                scope.addItem(self.export_scope.itemText(index), self.export_scope.itemData(index))
            scope.setCurrentIndex(self.export_scope.currentIndex())
            controls.addWidget(scope)
        controls.addWidget(QLabel('Outline palette', dialog))
        palette = QComboBox(dialog)
        for key, values in OUTLINE_PALETTES.items():
            palette.addItem(values[0], key)
        controls.addWidget(palette)
        controls.addStretch()
        export = QPushButton('Export figure…', dialog)
        controls.addWidget(export)
        dialog.layout().insertLayout(0, controls)

        def changed():
            state['figure'].clear()
            shown = complete_projection
            if has_scope and not scope.currentData():
                shown = {**complete_projection, 'regions': [row for row in complete_projection['regions'] if row['included']]}
            state['figure'] = region_overview_figure(shown, palette=palette.currentData())
            save_figure(state['figure'], path)
            scene = dialog.view.scene()
            scene.clear()
            scene.addPixmap(QPixmap(str(path)))
            dialog.view.setSceneRect(scene.itemsBoundingRect())
            dialog.view.reset_view()

        def cleanup():
            state['figure'].clear()
            temporary.cleanup()

        palette.currentIndexChanged.connect(changed)
        if has_scope:
            scope.currentIndexChanged.connect(self.export_scope.setCurrentIndex)
            scope.currentIndexChanged.connect(changed)
        export.clicked.connect(lambda: export_figure(dialog, state['figure'],
            default_name='analysis-region-outlines' if projection['kind'] == 'analysis_domains' else 'tissue-outlines',
            description='Outline palette: ' + palette.currentData()))
        dialog.finished.connect(cleanup)
        dialog.show()

    def _export_data(self):
        from mif_pipeline.orchestration.region_review_figures import region_measurements_csv
        from .figure_export import _choose_path, _save_bytes
        path = _choose_path(self, 'region-measurements.csv', {'Region areas and dimensions (*.csv)': 'csv'})
        if path is None:
            return
        try:
            _save_bytes(region_measurements_csv(self._export_projection())[3], path)
        except OSError as error:
            QMessageBox.warning(self, 'Data not exported', f'Could not save {path.name}.\n\n{error}')

    def selected_region_ids(self):
        return tuple(self._rows[self._proxy.mapToSource(index).row()]['region_id'] for index in self.table.selectionModel().selectedRows())

    def select_region_ids(self, ids):
        if self._syncing:
            return
        self._syncing = True
        try:
            selection = self.table.selectionModel(); selection.clearSelection()
            wanted = set(ids)
            first = None
            for source_row, row in enumerate(self._rows):
                if row['region_id'] in wanted:
                    index = self._proxy.mapFromSource(self._source.index(source_row, 0))
                    if first is None:
                        first = index
                    selection.select(index, QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
                    self.table.scrollTo(index)
            if first is not None:
                selection.setCurrentIndex(first, QItemSelectionModel.SelectionFlag.NoUpdate)
        finally:
            self._syncing = False
        self._table_selected()

    def _table_selected(self, *_args):
        if self._syncing:
            return
        self._syncing = True
        try:
            ids = self.selected_region_ids(); self.view.select_ids(ids)
            if self._allow_exclusion:
                self.exclude_button.setEnabled(bool(set(ids) - self._excluded))
                self.restore_button.setEnabled(bool(set(ids) & self._excluded))
            rows = [self._by_id[key] for key in ids]
            if rows:
                total = sum(row['area_um2'] for row in rows) / 1_000_000
                if len(rows) == 1:
                    row = rows[0]
                    total_area = sum(item['area_um2'] for item in self._rows)
                    portion = (f" · {100 * row['area_um2'] / total_area:.1f}% of tissue"
                               if row.get('layer') == 'tissue' and total_area > 0 else '')
                    name = (f'Section {row["display_number"]}' if row.get('layer') == 'tissue' and row.get('display_number') is not None
                            else row.get('display_label', 'Selected region'))
                    state = '; excluded from analysis' if row['region_id'] in self._excluded else ''
                    self.selection_summary.setText(f'{name}: {total:.4g} mm²{portion}{state}')
                else:
                    self.selection_summary.setText(f'{len(rows)} selected: {total:.4g} mm² total area')
                self.view.focus_selected()
            else:
                self.selection_summary.setText('Select a row to inspect its contour and staining.')
                self.view.reset_view()
            self.overview.update()
        finally:
            self._syncing = False

    def _add_conflicts(self, layout):
        conflicts = self.projection.get('overlaps', ())
        if not conflicts:
            return
        title = QLabel('Overlapping regions must be corrected before acceptance.', self); title.setWordWrap(True); layout.addWidget(title)
        listing = QListWidget(self); listing.setObjectName('region-review-conflicts')
        for pair in conflicts:
            names = [self._by_id[key].get('display_label', key) for key in pair['region_ids'] if key in self._by_id]
            listing.addItem(' / '.join(names) + f': {pair["area_um2"]:.4g} µm² overlap')
        listing.currentRowChanged.connect(lambda index: self.select_region_ids(conflicts[index]['region_ids']) if index >= 0 else None)
        layout.addWidget(listing)


def region_action_label(reason, action, fallback):
    """Name actual owner transitions without implying that a prompt opens QuPath."""
    if reason == 'tissue_section_review_required':
        return {'accept': 'Accept sections', 'edit': 'Load saved edits', 'repeat': 'Run detection again'}.get(action, fallback)
    return {
        'use_whole_tissue': 'Use whole sections',
        'select_analysis_rois': 'Choose outlined subregions',
        'review_analysis_rois': 'Review saved regions',
        'accept_analysis_rois': 'Accept analysis regions',
        'edit_analysis_rois': 'Change analysis regions',
    }.get(action, fallback)


def native_region_action(reason):
    """Consolidate the existing native editor, with instructions at its action."""
    if reason == 'tissue_section_review_required':
        return True, 'Edit outlines in QuPath', 'Save and close QuPath, then choose Load saved edits to refresh this review before accepting the sections.'
    if reason == 'analysis_domain_rois_required':
        return True, 'Edit analysis regions in QuPath', 'In QuPath, choose Save selected regions. Close QuPath, then select Review saved regions below.'
    if reason in {'analysis_domain_choice_required', 'analysis_domain_review_required'}:
        return False, 'Edit analysis regions in QuPath', ''
    if reason == 'secondary_structure_selection_required':
        return False, 'Inspect structures', ''
    return None
