"""One physical-coordinate registration workspace using the native image session.

The shared Qt scene handles navigation and display. Original image reads, point
mapping, global/local fitting and geometric review stay in the registered Java
worker. Only an overview and the newest bounded viewport are retained per view.
"""
from __future__ import annotations

import math
from copy import deepcopy

import numpy as np
from PySide6.QtCore import Qt, Signal, QTimer, QRect, QRectF, QPointF, QSize, QEvent
from PySide6.QtGui import QColor, QImage, QPixmap, QTransform, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout, QLabel,
    QPushButton, QToolButton, QButtonGroup, QSlider, QFrame, QComboBox, QCheckBox,
    QDoubleSpinBox, QSpinBox, QStackedWidget, QScrollArea, QSplitter, QTableWidget,
    QTableWidgetItem, QHeaderView, QDialog, QGraphicsPixmapItem, QToolTip, QSizePolicy, QMenu, QMessageBox, QLayout)

from .region_review import _OutlineView, _RegionOverview
from .image_appearance_editor import ImageAppearanceEditor
from .jobs import ManagedJobBridge, JobAccess
from .workbench import style_scientific_table
from .researcher_widgets import configure_disclosure
from .registration_review_geometry import contour_locations, grid_sample


class _CycleSourceLabel(QLabel):
    """Keep real source names available without sizing the cyclic workspace."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._source_name = ''
        self.setTextFormat(Qt.PlainText)
        self.setProperty('choiceHelp', True)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    def set_source_name(self, name):
        self._source_name = name
        self.setAccessibleName(name)
        self.setToolTip(name)
        self._elide()

    def _elide(self):
        self.setText(self.fontMetrics().elidedText(self._source_name, Qt.ElideMiddle, self.width()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()


class _CycleStatusLabel(QLabel):
    """Reserve space for an operation or error only while there is a message."""
    def setText(self, text):
        super().setText(text)
        self.setVisible(bool(text))


class _CycleOutlineLegend(QFrame):
    """Name the geometry colours independently of the intensity channels."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('cycle-outline-legend')
        self.setAccessibleName('Image outline key')
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setStyleSheet(
            '#cycle-outline-legend {background:rgba(23,35,41,225); border-radius:4px;}'
            '#cycle-outline-legend QLabel {color:#f0f5f4; background:transparent;}')
        contents = QVBoxLayout(self)
        contents.setContentsMargins(9, 6, 9, 7)
        contents.setSpacing(3)
        title = QLabel('Image outlines', self)
        title.setStyleSheet('font-weight:600;')
        contents.addWidget(title)
        self.rows = {}
        for name, label, color in (('reference', 'Reference image', '#23bcb8'),
                                   ('moving', 'Moving cycle', '#ef70b7'),
                                   ('crop', 'Retained area', '#ffe468')):
            row = QWidget(self)
            line = QHBoxLayout(row)
            line.setContentsMargins(0, 0, 0, 0)
            line.setSpacing(7)
            swatch = QLabel(row)
            swatch.setObjectName('cycle-outline-swatch-' + name)
            swatch.setFixedSize(14, 10)
            swatch.setStyleSheet(f'border:2px solid {color}; border-radius:0;')
            text = QLabel(label, row)
            line.addWidget(swatch)
            line.addWidget(text)
            contents.addWidget(row)
            self.rows[name] = (row, swatch, text)
        self.set_crop(None)
        self.hide()

    def set_crop(self, crop, *, valid=True):
        row, swatch, text = self.rows['crop']
        row.setVisible(crop is not None)
        swatch.setStyleSheet('border:2px solid ' + ('#ffe468' if valid else '#ff7979') + '; border-radius:0;')
        text.setText('Retained area' if valid else 'Invalid crop')
        self.adjustSize()


class _RegistrationPanelStack(QStackedWidget):
    """Size the settings scroll for the visible step, including wrapped text."""
    def sizeHint(self):
        page = self.currentWidget()
        return page.sizeHint() if page is not None else super().sizeHint()

    def minimumSizeHint(self):
        page = self.currentWidget()
        return page.minimumSizeHint() if page is not None else super().minimumSizeHint()

    def hasHeightForWidth(self):
        page = self.currentWidget()
        return page is not None and page.hasHeightForWidth()

    def heightForWidth(self, width):
        page = self.currentWidget()
        return page.heightForWidth(width) if page is not None else super().heightForWidth(width)


class _PairedSettingsScroll(QScrollArea):
    """Prefer readable fields while allowing narrow embedded review windows."""
    def sizeHint(self):
        hint = super().sizeHint()
        hint.setWidth(420)
        return hint


class _PairedStageActions(QStackedWidget):
    """Wrap a step's two actions when embedded beside return and save."""
    def _button_widths(self):
        return [[page.layout().itemAt(i).widget().minimumSizeHint().width()
                 for i in range(page.layout().count())]
                for page in (self.widget(i) for i in range(self.count()))]

    def sizeHint(self):
        hint = super().sizeHint()
        widths = self._button_widths()
        if widths:
            hint.setWidth(max(sum(row) + 7 for row in widths))
        return hint

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        widths = self._button_widths()
        if widths:
            hint.setWidth(max(max(row) for row in widths))
        return hint

    def resizeEvent(self, event):
        super().resizeEvent(event)
        for index, widths in enumerate(self._button_widths()):
            actions = self.widget(index).layout()
            actions.setDirection(QHBoxLayout.LeftToRight if self.width() >= sum(widths) + 7
                                 else QHBoxLayout.TopToBottom)
        if self.count():
            self.setMinimumHeight(max(self.widget(i).layout().minimumSize().height()
                                      for i in range(self.count())))
            self.updateGeometry()


class _PairedImageControls(QWidget):
    """Keep controls together without widening an embedded image workspace."""
    def __init__(self, display, scale, hint, actions, parent=None):
        super().__init__(parent)
        self._parts = (display, scale, hint, actions)
        self._narrow = None
        self._rows = []
        body = QVBoxLayout(self)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(7)
        body.setSizeConstraint(QLayout.SetNoConstraint)
        for _ in range(3):
            row = QWidget(self)
            line = QHBoxLayout(row)
            line.setContentsMargins(0, 0, 0, 0)
            line.setSpacing(6)
            line.setSizeConstraint(QLayout.SetNoConstraint)
            body.addWidget(row)
            self._rows.append(row)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self._adapt_timer = QTimer(self)
        self._adapt_timer.setSingleShot(True)
        self._adapt_timer.timeout.connect(self._adapt)
        self._adapt()

    def minimumSizeHint(self):
        return QSize(max(part.minimumSizeHint().width() for part in self._parts),
                     self.layout().minimumSize().height())

    def sizeHint(self):
        return self.minimumSizeHint()

    def _adapt(self):
        display, scale, hint, actions = self._parts
        widths = [part.minimumSizeHint().width() for part in self._parts]
        narrow = self.isVisible() and self.width() < max(widths[0]+widths[1], widths[2]+widths[3])+6
        if narrow != self._narrow:
            self._narrow = narrow
            for row in self._rows:
                while row.layout().count():
                    row.layout().takeAt(0)
            first, second, third = (row.layout() for row in self._rows)
            first.addWidget(display, 1)
            if narrow:
                second.addWidget(scale)
                second.addStretch()
                second.addWidget(hint)
                third.addStretch()
                third.addWidget(actions)
            else:
                first.addWidget(scale)
                second.addWidget(hint)
                second.addStretch()
                second.addWidget(actions)
            self._rows[2].setVisible(narrow)
        height = self.layout().minimumSize().height()
        if self.minimumHeight() != height:
            self.setMinimumHeight(height)
            self.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._adapt()

    def event(self, event):
        result = super().event(event)
        if event.type() in (QEvent.LayoutRequest, QEvent.Show) and hasattr(self, '_adapt_timer'):
            self._adapt_timer.start(0)
        return result


class _AlignmentGridItem(QGraphicsPixmapItem):
    """Inspect the existing sampled map without issuing another native read."""
    def __init__(self, pixmap, grid, pixel_size, paired_name):
        super().__init__(pixmap)
        self.grid, self.pixel_size, self.paired_name = grid, pixel_size, paired_name
        self.setShapeMode(QGraphicsPixmapItem.BoundingRectShape)
        self.setAcceptHoverEvents(True)
        self.setAcceptedMouseButtons(Qt.NoButton)

    def text_at(self, point):
        sample = grid_sample(self.grid, math.floor(point.x()), math.floor(point.y()), self.pixel_size)
        if sample is None:
            return ''
        x, y = sample['point_um']
        status = ('Outside the selected review region; not sampled.',
                  f'Outside the aligned {self.paired_name} image.',
                  f'Inside the aligned {self.paired_name} image.',
                  'Mapping or area change could not be calculated.')[sample['state']]
        lines = [f'Sampled mIF position: {x:.3f}, {y:.3f} µm', status]
        if sample['state'] == 2 and sample['area_ratio'] is not None:
            lines.append(f"Local area ratio: {sample['area_ratio']:.6g}")
        return '\n'.join(lines)

    def hoverMoveEvent(self, event):
        QToolTip.showText(event.screenPos(), self.text_at(event.pos()))
        super().hoverMoveEvent(event)

    def hoverLeaveEvent(self, event):
        QToolTip.hideText()
        super().hoverLeaveEvent(event)


class _RegistrationView(_OutlineView):
    """Shared pan/zoom view with source-pixel zoom and an optional pose gesture."""
    position_dragged = Signal(float, float)
    position_preview = Signal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pixel_um = 1.
        self.positioning = False
        self.crosshair = False
        self._pose_start = None
        self.setMinimumSize(260, 280)
        self.image_tools = QFrame(self.viewport())
        self.image_tools.setObjectName('registration-image-tools')
        self.image_tools.setStyleSheet(
            '#registration-image-tools {background:rgba(23,35,41,225); border-radius:4px;}'
            '#registration-image-tools QLabel {color:#f0f5f4; font-weight:600; background:transparent;}'
            '#registration-image-tools QPushButton {padding:3px 7px; min-height:20px;}')
        self.image_tools_layout = QHBoxLayout(self.image_tools)
        self.image_tools_layout.setContentsMargins(9, 5, 9, 5)
        self.image_tools_layout.setSpacing(8)
        self.image_label = QLabel(self.image_tools)
        self.image_tools_layout.addWidget(self.image_label)
        self.action_bar = QFrame(self.viewport())
        self.action_layout = QHBoxLayout(self.action_bar)
        self.action_layout.setContentsMargins(0, 0, 0, 0)
        self.action_layout.setSpacing(6)
        self.action_bar.hide()

    def zoom_by(self, factor):
        current = self.transform().m11()
        target = max(self._base_scale / 20, min(max(self._base_scale * 200, 8 / self.pixel_um), current * factor))
        self._fit = False
        self.scale(target / current, target / current)
        self.zoom_changed.emit(target)

    def mousePressEvent(self, event):
        if self.positioning and event.button() == Qt.LeftButton and event.modifiers() & Qt.ShiftModifier:
            self._pose_start = self.mapToScene(event.position().toPoint())
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._pose_start is not None:
            point = self.mapToScene(event.position().toPoint()) - self._pose_start
            self.position_preview.emit(point.x(), point.y())
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._pose_start is not None:
            end = self.mapToScene(event.position().toPoint())
            delta = end - self._pose_start
            self._pose_start = None
            self.position_dragged.emit(delta.x(), delta.y())
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.crosshair:
            painter = QPainter(self.viewport())
            centre = self.viewport().rect().center()
            painter.setPen(QPen(QColor('#f8db68'), 1.5))
            painter.drawLine(centre.x()-11, centre.y(), centre.x()+11, centre.y())
            painter.drawLine(centre.x(), centre.y()-11, centre.x(), centre.y()+11)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.position_controls()

    def position_controls(self):
        """Keep image labels and contextual actions inside the resized viewport."""
        if hasattr(self, 'image_tools'):
            self.image_tools.adjustSize()
            self.image_tools.move(10, 10)
            self.channel_legend.set_top(self.image_tools.geometry().bottom()+6)
            self.action_bar.adjustSize()
            self.action_bar.move(max(10, self.viewport().width()-self.action_bar.width()-10),
                                 max(10, self.viewport().height()-self.action_bar.height()-10))
        outline_legend = getattr(self, 'cycle_outline_legend', None)
        if outline_legend is not None:
            outline_legend.adjustSize()
            outline_legend.move(max(10, self.viewport().width()-outline_legend.width()-10),
                                max(10, self.viewport().height()-outline_legend.height()-10))
        overview = getattr(self, 'locator', None)
        if overview is not None:
            right=max(0,self.viewport().width()-overview.width()-10)
            top=10
            if self.channel_legend.isVisible() and right<self.channel_legend.geometry().right()+10:
                bottom=self.action_bar.geometry().top()-10 if self.action_bar.isVisible() else self.viewport().height()-10
                if outline_legend is not None and outline_legend.isVisible():
                    bottom=min(bottom, outline_legend.geometry().top()-10)
                top=max(self.channel_legend.geometry().bottom()+10,bottom-overview.height())
            overview.move(right,top)


class _RegistrationOverview(_RegionOverview):
    """Reuse the shared overview to move the field without changing its zoom."""
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            inverse, valid = self._transform.inverted()
            if valid:
                self.review.view.centerOn(inverse.map(event.position()))
                self.review._schedule('fixed')
                event.accept()
                return
        super().mousePressEvent(event)


def _channel_image(array, channels, descriptor):
    """Apply the shared appearance recipe to already bounded native channel data."""
    if descriptor['mode'] == 'rgb':
        rgb = np.clip(np.moveaxis(array[:3], 0, -1), 0, 255).astype(np.uint8)
    else:
        rgb = np.zeros((*array.shape[1:], 3), dtype=np.float32)
        for index, values in zip(channels, array):
            key = str(index)
            low, high = descriptor['limits'][key]
            scaled = np.nan_to_num((values-low)/(high-low), nan=0., posinf=1., neginf=0.)
            np.clip(scaled, 0., 1., out=scaled)
            color = QColor('#ffffff' if descriptor['mode'] == 'grayscale' else descriptor['colors'][key])
            rgb += scaled[..., None] * np.array([color.red(), color.green(), color.blue()], dtype=np.float32)
        rgb = np.clip(rgb, 0, 255).astype(np.uint8)
    rgb = np.ascontiguousarray(rgb)
    return QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0], QImage.Format_RGB888).copy()


class RegistrationEditor(QWidget):
    """Fit and inspect one pair, then return its saved candidate to normal review.

    ``close_requested`` asks the controller to close the native session. After
    its process ends, ``worker_finished`` drains this page's response jobs and
    emits ``finished``; the controller can then remove the page and its exchange.
    ``candidate_saved`` means the existing save operation has written, reopened
    and hashed the candidate. It does not mean the pipeline accepted it.
    """
    close_requested = Signal()
    candidate_saved = Signal(object)
    finished = Signal()

    def __init__(self, session, parent=None, *, global_only=False, read_only=False):
        super().__init__(parent)
        self.session = session
        self.global_only = global_only
        self.read_only = bool(read_only and global_only)
        self.review_crop = None
        self.review_crop_valid = True
        self.setObjectName('registration-editor')
        self.setStyleSheet(
            '#registration-editor QPushButton:checked {background:#dceeea; border:1px solid #007579; color:#005b60;}'
            '#registration-editor QFrame[workbenchSection="true"], '
            '#registration-editor QTableView[scientificTable="true"] {border-radius:0;}'
            '#registration-editor QLabel[registrationField="true"] {color:#294148; font-weight:500;}'
            '#registration-editor QSlider::groove:horizontal {height:4px; background:#d4dfdd; border-radius:2px;}'
            '#registration-editor QSlider::sub-page:horizontal {background:#007b80; border-radius:2px;}'
            '#registration-editor QSlider::handle:horizontal {background:#007b80; border:2px solid #ffffff; '
            'width:14px; height:14px; margin:-7px 0; border-radius:8px;}'
            '#registration-editor QSlider::handle:horizontal:hover {background:#005f64;}'
            '#registration-editor QSlider::handle:horizontal:disabled {background:#a7b9b6;}')
        self.jobs = ManagedJobBridge(self)
        self.jobs.job_succeeded.connect(self._ready)
        self.jobs.job_failed.connect(self._failed)
        self.jobs.active_jobs_changed.connect(self._drained)
        self._job = None
        self._action = None
        self._queued_operation = None
        self._closing = False
        self._worker_done = False
        self._finished_emitted = False
        self._changing = False
        self._generation = 0
        self._loaded_generation = -1
        self._pending_views = set()
        self._overview_needed = {'fixed', 'moving'}
        self._overview = {'fixed': [], 'moving': []}
        self._detail = {'fixed': [], 'moving': []}
        self._buffers = {}
        self._paths = []
        self._grid_item = None
        self._landmark_items = []
        self._meta = None
        self._state = {}
        self._fitted_local_settings = {}
        self._scopes = []
        self._boundary_points = []
        self._boundary_index = -1
        self._appearances = {}
        self._pending_appearance_saves = {}
        self._review_channels = {}
        self._pending_review_channels = {}
        self._moving_points, self._fixed_points = [], []
        self._remote_local_method = 'landmarks'
        self.source = 'overlay'
        self.step = 0
        self.frames = []
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(70)
        self.timer.timeout.connect(self._fetch)
        self._build()
        self._job = self.jobs.submit('registration_open', session.wait_ready, access=JobAccess.READ)
        self._action = ('open', None)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self.global_only:
            # Preserve the compact viewer allocation, then give the next 40 px
            # to the paired controls before growing the image workspace again.
            self.side_scroll.setMaximumWidth(min(420, max(380, event.size().width() - 720)))

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*(0, 0, 0, 0) if self.global_only else (16, 14, 16, 12))
        layout.setSpacing(8 if self.global_only else 10)
        title = QLabel('Align cycle to reference' if self.global_only else 'Paired-image registration', self)
        self.heading = title
        title.setObjectName('cycle-registration-heading' if self.global_only else 'reviews-title')
        if self.global_only:
            title.setProperty('dialogHeading', True)
        if self.global_only:
            layout.addWidget(title)
        else:
            title_row = QHBoxLayout()
            title_row.addWidget(title)
            title_row.addStretch()
            layout.addLayout(title_row)
        self.subtitle = QLabel('Opening the original images…', self)
        self.subtitle.setWordWrap(True)
        if self.global_only:
            self.subtitle.hide()
            self.source_context = QFrame(self)
            source_layout = QHBoxLayout(self.source_context)
            source_layout.setContentsMargins(0, 0, 0, 2)
            source_layout.setSpacing(24)
            self.cycle_source_labels = {}
            for source, role in (('fixed', 'Reference'), ('moving', 'Moving cycle')):
                context = QHBoxLayout()
                context.setSpacing(8)
                caption = QLabel(role, self.source_context)
                caption.setProperty('fieldCaption', True)
                context.addWidget(caption)
                name = _CycleSourceLabel(self.source_context)
                name.set_source_name('Opening original image…')
                context.addWidget(name, 1)
                source_layout.addLayout(context, 1)
                self.cycle_source_labels[source] = name
            layout.addWidget(self.source_context)
        else:
            self.subtitle.setProperty('choiceHelp', True)
            title_row.addWidget(self.subtitle)
            context = QFrame(self)
            context.setProperty('workbenchSection', True)
            sources_layout = QHBoxLayout(context)
            sources_layout.setContentsMargins(12, 7, 12, 7)
            sources_layout.setSpacing(24)
            self.paired_source_labels = {}
            for source, role in (('fixed', 'mIF'), ('moving', 'Paired image')):
                source_row = QHBoxLayout()
                caption = QLabel(role, context)
                caption.setProperty('fieldCaption', True)
                source_row.addWidget(caption)
                name = _CycleSourceLabel(context)
                name.set_source_name('Opening original image…')
                source_row.addWidget(name, 1)
                sources_layout.addLayout(source_row, 1)
                self.paired_source_labels[source] = name
            layout.addWidget(context)
        steps = QHBoxLayout()
        self.steps = []
        group = QButtonGroup(self)
        group.setExclusive(True)
        for index, name in enumerate(('Global alignment', 'Local refinement', 'Review')):
            button = QPushButton(name, self)
            button.setCheckable(True)
            button.setObjectName('registration-step-'+str(index))
            button.clicked.connect(lambda checked=False, i=index: self._select_step(i))
            group.addButton(button)
            steps.addWidget(button)
            self.steps.append(button)
            if self.global_only:
                button.hide()
        layout.addLayout(steps)
        main = QHBoxLayout()
        main.setSpacing(12)
        layout.addLayout(main, 1)
        left = QVBoxLayout()
        left.setSpacing(7)
        main.addLayout(left, 1)
        self.views = QSplitter(Qt.Horizontal, self)
        self.view = _RegistrationView(self)
        self.moving_view = _RegistrationView(self)
        if self.global_only:
            self.view.setMinimumHeight(180 if self.read_only else 200)
            self.view.cycle_outline_legend = _CycleOutlineLegend(self.view.viewport())
        else:
            self.view.setMinimumHeight(240)
            self.moving_view.setMinimumHeight(240)
        self.view.channel_legend.changed.connect(lambda display:self._change_appearance('moving' if self._display_source()=='moving' else 'fixed',display))
        self.moving_view.channel_legend.changed.connect(lambda display:self._change_appearance('moving',display))
        self.view.setAccessibleName('Fixed image and aligned paired image')
        self.moving_view.setAccessibleName('Original paired image for landmarks')
        self.views.addWidget(self.view)
        self.views.addWidget(self.moving_view)
        self.moving_view.hide()
        left.addWidget(self.views, 1)
        self.locator = _RegistrationOverview(self, self.view.viewport())
        self.locator.setMinimumSize(0, 0)
        self.locator.setFixedSize(150, 165)
        self.locator.setToolTip('Click to centre this part of the mIF image at the current zoom')
        self.locator.hide()
        self.view.locator = self.locator
        for name, viewer in (('fixed', self.view), ('moving', self.moving_view)):
            viewer.viewport_changed.connect(lambda n=name: self._schedule(n))
            viewer.zoom_changed.connect(lambda _, n=name: self._schedule(n))
        self.view.position_dragged.connect(self._drag_pose)
        self.view.position_preview.connect(self._preview_drag)
        self.preview_position = QPushButton('Preview starting position', self.view.action_bar)
        self.preview_position.setObjectName('registration-preview-position')
        self.preview_position.clicked.connect(self._preview_pose)
        self.view.action_layout.addWidget(self.preview_position)
        self.align_centers = QPushButton('Align view centers', self.moving_view.action_bar)
        self.align_centers.setObjectName('registration-align-centers')
        self.align_centers.setToolTip('Use the global alignment to centre the same position in both images at the same physical zoom.')
        self.align_centers.clicked.connect(self._match_view)
        self.moving_view.action_layout.addWidget(self.align_centers)
        self.landmark_overlay = QPushButton('H&E overlay', self.view.image_tools)
        self.landmark_overlay.setCheckable(True)
        self.landmark_overlay.setToolTip('Show the aligned paired image over mIF while retaining the original paired image beside it.')
        self.landmark_overlay.toggled.connect(self._apply_source)
        self.landmark_overlay.hide()
        self.view.image_tools_layout.addWidget(self.landmark_overlay)
        self.display_bar = QWidget(self)
        display = QHBoxLayout(self.display_bar)
        display.setContentsMargins(0, 0, 0, 0)
        self.source_buttons = {}
        sources = QButtonGroup(self)
        sources.setExclusive(True)
        for label, value in (('Overlay', 'overlay'), ('mIF', 'fixed'), ('Paired image', 'moving')):
            button = QPushButton(label, self)
            button.setCheckable(True)
            button.setChecked(value == self.source)
            button.clicked.connect(lambda checked=False, v=value: self.set_source(v))
            button.setObjectName('registration-source-'+value)
            sources.addButton(button)
            display.addWidget(button)
            self.source_buttons[value] = button
        self.opacity = QSlider(Qt.Horizontal, self)
        self.opacity.setRange(0, 100)
        self.opacity.setValue(50)
        self.opacity.setAccessibleName('Paired image opacity')
        self.opacity.setMinimumWidth(65)
        self.opacity.setMaximumWidth(180)
        self.opacity.setMinimumHeight(24)
        self.opacity.valueChanged.connect(self._apply_source)
        display.addWidget(QLabel('Opacity', self))
        display.addWidget(self.opacity, 1)
        self.opacity_label = QLabel('50%', self)
        self.opacity_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.opacity_label.setFixedWidth(self.opacity_label.fontMetrics().horizontalAdvance('100%') + 4)
        display.addWidget(self.opacity_label)
        display.addStretch()
        left.addWidget(self.display_bar)
        nav = QHBoxLayout()
        self.navigation_hint = QLabel('Drag to pan · Ctrl + scroll to zoom', self)
        self.navigation_hint.setStyleSheet('color:#53686c; font-size:11px;')
        self.navigation_hint.setWordWrap(True)
        left.addWidget(self.navigation_hint)
        nav.addStretch()
        self.fit_view = QPushButton('Fit view', self)
        self.fit_view.clicked.connect(self.view.reset_view)
        nav.addWidget(self.fit_view)
        self.native_button = QPushButton('1:1', self)
        self.native_button.setToolTip('One mIF source pixel per screen pixel')
        self.native_button.clicked.connect(self.native_scale)
        nav.addWidget(self.native_button)
        self.appearance_button = QPushButton('Appearance', self)
        self.appearance_button.clicked.connect(self._appearance_dialog)
        nav.addWidget(self.appearance_button)
        self.export_button = QPushButton('Export…', self)
        self.export_button.setEnabled(False)
        export_menu = QMenu(self.export_button)
        for label, kind, help_text in (
            ('Aligned field figure…', 'figure', 'Save the displayed image and overlays with a calibrated scale and legend.'),
            ('Field data…', 'field', 'Save bounded sampled channel arrays, physical coordinates and display choices as NPZ. Paired pixels are already resampled.'),
            ('Check summary…', 'summary', 'Save the existing unrounded check values for the selected review region as CSV.'),
            ('Sampled checks…', 'samples', 'Save all existing grid sample positions, states and area ratios for the selected review region as CSV.')):
            action = export_menu.addAction(label, lambda checked=False, value=kind: self._export_alignment(value))
            action.setToolTip(help_text)
        export_menu.setToolTipsVisible(True)
        self.export_button.setMenu(export_menu)
        nav.addWidget(self.export_button)
        left.addLayout(nav)
        self.side_scroll = QScrollArea(self) if self.global_only else _PairedSettingsScroll(self)
        self.side_scroll.setWidgetResizable(True)
        self.side_scroll.setFrameShape(QFrame.NoFrame)
        self.side_scroll.setMinimumWidth(310)
        self.side_scroll.setMaximumWidth(420)
        if self.global_only:
            self.side_scroll.setMinimumWidth(290)
            self.side_scroll.setMaximumWidth(320)
        self.panels = QStackedWidget(self) if self.global_only else _RegistrationPanelStack(self)
        self.side_scroll.setWidget(self.panels)
        main.addWidget(self.side_scroll)
        self._global_panel()
        self._local_panel()
        self._review_panel()
        if self.global_only:
            # The cyclic editor never visits the paired local/review pages.
            # Keeping them in the stack makes their height size the sidebar.
            for index in (2, 1):
                page = self.panels.widget(index)
                self.panels.removeWidget(page)
                page.setParent(self)
                page.hide()
        # Outline visibility belongs with image display and navigation controls.
        left.insertWidget(left.indexOf(self.display_bar) + 1, self.tissue_visible,
                          alignment=Qt.AlignRight)
        if not self.global_only:
            self._arrange_paired_image_controls(left, nav)
        else:
            self._arrange_cycle_image_controls(left, nav)
            self.view.setAccessibleName('Reference image and aligned moving cycle')
            self.opacity.setAccessibleName('Moving cycle opacity')
            self.locator.setToolTip('Click to centre this part of the reference image at the current zoom')
            self.native_button.setToolTip('One reference-image source pixel per screen pixel')
        status_label = _CycleStatusLabel if self.global_only else QLabel
        self.status = status_label('Opening the original image readers…', self)
        self.status.setWordWrap(True)
        self.status.setObjectName('registration-status')
        layout.addWidget(self.status)
        footer = QHBoxLayout()
        self.close_button = QPushButton('Back to cycles' if self.global_only else 'Return to alignment decision', self)
        self.close_button.clicked.connect(self.request_close)
        footer.addWidget(self.close_button)
        footer.addStretch()
        self.save_button = QPushButton('Use this cycle alignment' if self.global_only else 'Save candidate for review', self)
        self.save_button.setProperty('actionRole', 'primary')
        self.save_button.setObjectName('registration-save')
        self.save_button.clicked.connect(lambda: self._operate('save'))
        self.save_button.setEnabled(False)
        footer.addWidget(self.save_button)
        layout.addLayout(footer)
        if not self.global_only:
            self._arrange_paired_stage_actions(footer)
        else:
            self._arrange_cycle_result(layout, footer)
            if self.read_only:
                self.heading.hide()
                self.source_context.hide()
                self.cycle_result.hide()
                self.side_scroll.hide()
                self.status.setParent(self)
                layout.addWidget(self.status)
        self.panels.setEnabled(False)
        self.steps[0].setChecked(True)

    def _arrange_paired_image_controls(self, layout, navigation):
        """Use two rows when space permits and a third in narrow workspaces."""
        layout.removeWidget(self.display_bar)
        layout.removeWidget(self.navigation_hint)
        layout.removeWidget(self.tissue_visible)
        layout.removeItem(navigation)
        self.tissue_visible.setText('Tissue outlines')
        self.tissue_visible.setToolTip('Show accepted tissue outlines')
        self.navigation_hint.setWordWrap(False)
        groups = []
        for widgets in ((self.fit_view, self.native_button),
                        (self.tissue_visible, self.appearance_button, self.export_button)):
            group = QWidget(self)
            row = QHBoxLayout(group)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            for widget in widgets:
                row.addWidget(widget)
            groups.append(group)
        self.paired_image_controls = _PairedImageControls(
            self.display_bar, groups[0], self.navigation_hint, groups[1], self)
        layout.addWidget(self.paired_image_controls)

    def _arrange_cycle_image_controls(self, layout, navigation):
        """Keep image display together; the view itself carries navigation help."""
        layout.removeWidget(self.display_bar)
        layout.removeWidget(self.navigation_hint)
        layout.removeWidget(self.tissue_visible)
        layout.removeItem(navigation)
        self.navigation_hint.hide()
        self.tissue_visible.hide()
        self.fit_view.setText('Fit')
        self.fit_view.setToolTip('Fit the complete reference image without changing its aspect ratio')
        actions = QWidget(self)
        row = QHBoxLayout(actions)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        for widget in (self.fit_view, self.native_button, self.appearance_button):
            row.addWidget(widget)
        empty = QWidget(self)
        empty.hide()
        self.paired_image_controls = _PairedImageControls(
            self.display_bar, actions, self.navigation_hint, empty, self)
        layout.addWidget(self.paired_image_controls)
        self.view.setToolTip('Drag to pan · Ctrl + scroll to zoom · Shift + drag to position the moving cycle')

    @staticmethod
    def _paired_section(parent, title):
        frame = QFrame(parent)
        contents = QVBoxLayout(frame)
        contents.setContentsMargins(0, 4, 0, 6)
        contents.setSpacing(9)
        heading = QLabel(title, frame)
        heading.setProperty('panelHeading', True)
        heading.setWordWrap(True)
        contents.addWidget(heading)
        return frame, contents

    def _arrange_paired_stage_actions(self, footer):
        """Keep the current step's actions beside return and candidate save."""
        self.paired_stage_actions = _PairedStageActions(self)
        self.paired_stage_actions.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        keep = self.findChild(QPushButton, 'registration-keep-global')
        for buttons in ((self.run_global, self.next_local), (keep, self.run_local)):
            page = QWidget(self.paired_stage_actions)
            actions = QHBoxLayout(page)
            actions.setContentsMargins(0, 0, 0, 0)
            actions.setSpacing(7)
            for button in buttons:
                button.parentWidget().layout().removeWidget(button)
                actions.addWidget(button)
            self.paired_stage_actions.addWidget(page)
        self.paired_stage_actions.setEnabled(False)
        footer.insertWidget(footer.indexOf(self.save_button), self.paired_stage_actions)

    def _panel(self, title, description):
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(10)
        heading = QLabel(title, panel)
        heading.setStyleSheet('font-weight:600; color:#006b70;')
        heading.setWordWrap(True)
        layout.addWidget(heading)
        label = QLabel(description, panel)
        label.setWordWrap(True)
        layout.addWidget(label)
        self.panels.addWidget(panel)
        return panel, layout

    @staticmethod
    def _spin(low, high, value, *, decimals=None):
        spin = QSpinBox() if decimals is None else QDoubleSpinBox()
        spin.setRange(low, high)
        if decimals is not None:
            spin.setDecimals(decimals)
        spin.setValue(value)
        spin.setKeyboardTracking(False)
        return spin

    def _global_panel(self):
        panel, layout = self._panel('Rigid alignment' if self.global_only else 'Global affine',
            'Shift + drag to move the cycle, or enter shifts and a rotation.' if self.global_only else
            'Choose image signals that show the same structures. Fitting adjusts translation, rotation, scale and shear across the image.')
        self.global_guidance = layout.itemAt(1).widget()
        form = QFormLayout()
        self.fixed_channel, self.moving_channel = QComboBox(panel), QComboBox(panel)
        form.addRow('Reference signal' if self.global_only else 'mIF signal', self.fixed_channel)
        form.addRow('Cycle signal' if self.global_only else 'Paired signal', self.moving_channel)
        if self.global_only:
            form.setRowVisible(self.fixed_channel, False)
            form.setRowVisible(self.moving_channel, False)
        layout.addLayout(form)
        self.pose_panel = QWidget(panel)
        pose_layout = QVBoxLayout(self.pose_panel)
        pose_layout.setContentsMargins(0, 0, 0, 0)
        pose_form = QFormLayout() if self.global_only else QGridLayout()
        if not self.global_only:
            pose_form.setHorizontalSpacing(12)
            pose_form.setVerticalSpacing(5)
        self.pose = [self._spin(-1e6, 1e6, 0, decimals=3), self._spin(-1e6, 1e6, 0, decimals=3),
                     self._spin(-360, 360, 0, decimals=3), self._spin(.01, 100, 1, decimals=5)]
        pose_labels = (('Horizontal (µm)', 'Vertical (µm)', 'Rotation (°)', 'Uniform scale') if self.global_only else
                       ('Horizontal shift (µm)', 'Vertical shift (µm)', 'Rotation (degrees)', 'Uniform scale'))
        for index, (label, spin) in enumerate(zip(pose_labels, self.pose)):
            if self.global_only:
                pose_form.addRow(label, spin)
                spin.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
                spin.setMinimumWidth(100)
                pose_form.labelForField(spin).setProperty('registrationField', True)
            else:
                caption = QLabel(label, self.pose_panel)
                caption.setProperty('registrationField', True)
                caption.setBuddy(spin)
                pose_form.addWidget(caption, 2 * (index // 2), index % 2)
                pose_form.addWidget(spin, 2 * (index // 2) + 1, index % 2)
                pose_form.setColumnStretch(index % 2, 1)
                spin.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
                spin.setMinimumWidth(100)
            spin.valueChanged.connect(self._global_changed)
        if self.global_only:
            self.pose[3].setValue(1.)
            pose_form.setRowVisible(self.pose[3], False)
        pose_layout.addLayout(pose_form)
        self.use_pose = QCheckBox('Use this position to start fitting', panel)
        self.use_pose.setChecked(True)
        self.use_pose.toggled.connect(self._global_changed)
        pose_layout.addWidget(self.use_pose)
        layout.addWidget(self.pose_panel)
        advanced = QToolButton(panel)
        advanced.setText('Fit settings')
        advanced.setCheckable(True)
        configure_disclosure(advanced)
        layout.addWidget(advanced)
        self.global_settings = QWidget(panel)
        settings = QFormLayout(self.global_settings)
        settings.setContentsMargins(0, 0, 0, 0)
        self.global_view = self._spin(128, 4096, 1024)
        self.global_iterations = self._spin(32, 2048, 512)
        self.threads = self._spin(1, 16, 2)
        self.global_view.setToolTip('Maximum width or height of each image used for fitting. Larger views retain smaller structures and require more image memory and computation.')
        self.global_iterations.setToolTip('Maximum updates to the affine parameters at each of three smoothing levels. Increasing the limit allows more fitting updates and can increase fitting time.')
        self.threads.setToolTip('Maximum CPU threads available to Elastix fitting and Transformix coordinate checks.')
        self.orientation = QCheckBox('Search image orientations', panel)
        self.orientation.setToolTip('Compare four starting orientations when a manual starting position is not used.')
        for label, spin in (('View size (pixels)', self.global_view), ('Iterations', self.global_iterations), ('Threads', self.threads)):
            settings.addRow(label, spin)
            spin.valueChanged.connect(self._global_changed)
        settings.addRow(self.orientation)
        self.orientation.toggled.connect(self._global_changed)
        self.global_settings.hide()
        advanced.toggled.connect(self.global_settings.setVisible)
        layout.addWidget(self.global_settings)
        self.run_global = QPushButton('Calculate global alignment', panel)
        self.run_global.setProperty('actionRole', 'primary')
        self.run_global.setObjectName('registration-run-global')
        self.run_global.clicked.connect(self._fit_global)
        layout.addWidget(self.run_global)
        self.next_local = QPushButton('Continue to local refinement', panel)
        self.next_local.clicked.connect(lambda: self._select_step(1))
        layout.addWidget(self.next_local)
        if self.global_only:
            self.next_local.hide()
            self.run_global.setText('Fit automatically')
            self.preview_position.setText('Apply manual position')
            self.global_iterations.setToolTip('Maximum updates to rigid rotation and translation at each smoothing level.')
            self._arrange_cycle_controls(panel, layout, advanced)
        else:
            self._arrange_paired_global_controls(panel, layout, form, advanced)
        layout.addStretch()
        self.fixed_channel.currentIndexChanged.connect(self._global_changed)
        self.moving_channel.currentIndexChanged.connect(self._global_changed)

    def _arrange_paired_global_controls(self, panel, layout, signals, disclosure):
        layout.itemAt(0).widget().hide()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(20)
        layout.removeItem(signals)
        for widget in (self.global_guidance, self.pose_panel, disclosure,
                       self.global_settings, self.run_global, self.next_local):
            layout.removeWidget(widget)
        signals.setRowWrapPolicy(QFormLayout.WrapAllRows)
        signals.setVerticalSpacing(5)
        for row in range(signals.rowCount()):
            signals.itemAt(row, QFormLayout.LabelRole).widget().setProperty('registrationField', True)
        for selector in (self.fixed_channel, self.moving_channel):
            selector.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            selector.setMinimumContentsLength(10)
            selector.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            selector.currentTextChanged.connect(selector.setToolTip)
        self.paired_pose_section, pose_layout = self._paired_section(panel, 'Starting position')
        pose_layout.setSpacing(6)
        pose_hint = QLabel('Shift + drag to move the paired image.', self.paired_pose_section)
        pose_hint.setWordWrap(True)
        pose_hint.setProperty('choiceHelp', True)
        self.pose_panel.layout().removeWidget(self.use_pose)
        pose_layout.addWidget(self.pose_panel)
        pose_layout.addWidget(self.use_pose)
        pose_layout.addWidget(pose_hint)
        layout.addWidget(self.paired_pose_section)

        automatic, automatic_layout = self._paired_section(panel, 'Fit inputs')
        self.global_guidance.setProperty('choiceHelp', True)
        automatic_layout.addWidget(self.global_guidance)
        automatic_layout.addLayout(signals)
        automatic_layout.addWidget(disclosure)
        automatic_layout.addWidget(self.global_settings)
        automatic_layout.addWidget(self.run_global)
        self.paired_fit_disclosure = disclosure
        layout.addWidget(automatic)
        layout.addWidget(self.next_local)

    def _arrange_cycle_controls(self, panel, layout, disclosure):
        """Select a tool for the same alignment without moving the viewer."""
        layout.itemAt(0).widget().hide()
        layout.setContentsMargins(0, 0, 0, 0)
        for widget in (self.global_guidance, self.pose_panel, disclosure,
                       self.global_settings, self.run_global):
            layout.removeWidget(widget)
        self.pose_panel.layout().removeWidget(self.use_pose)
        self.pose_panel.hide()
        self.cycle_tools = QFrame(panel)
        tools = QVBoxLayout(self.cycle_tools)
        tools.setContentsMargins(8, 0, 0, 0)
        tools.setSpacing(9)
        caption = QLabel('Alignment tool', self.cycle_tools)
        caption.setProperty('fieldCaption', True)
        tools.addWidget(caption)
        selector = QHBoxLayout()
        selector.setSpacing(0)
        tools.addLayout(selector)
        group = QButtonGroup(self.cycle_tools)
        group.setExclusive(True)
        self.cycle_tool_buttons = {}
        self.cycle_tool_pages = []
        pages = []
        for name, title in (('position', 'Manual'), ('automatic', 'Automatic')):
            button = QPushButton(title, self.cycle_tools)
            button.setText(title)
            button.setObjectName('registration-cycle-tool-' + name)
            button.setCheckable(True)
            button.setAccessibleName(title + ' alignment controls')
            button.setStyleSheet('border-top-right-radius:0; border-bottom-right-radius:0;' if name == 'position' else
                                'border-top-left-radius:0; border-bottom-left-radius:0;')
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.setChecked(name == 'position')
            button.clicked.connect(lambda checked=False, tool=name: self._select_cycle_tool(tool))
            group.addButton(button)
            selector.addWidget(button, 1)
            self.cycle_tool_buttons[name] = button
            page = QWidget(self.cycle_tools)
            contents = QVBoxLayout(page)
            contents.setContentsMargins(0, 2, 0, 0)
            contents.setSpacing(9)
            tools.addWidget(page)
            self.cycle_tool_pages.append(page)
            pages.append(contents)
        manual_layout, automatic_layout = pages
        self.global_guidance.setProperty('choiceHelp', True)
        manual_layout.addWidget(self.global_guidance)
        positions = QGridLayout()
        positions.setContentsMargins(0, 0, 0, 0)
        positions.setHorizontalSpacing(10)
        positions.setVerticalSpacing(5)
        for index, title in enumerate(('Horizontal (µm)', 'Vertical (µm)', 'Rotation (°)')):
            row, column = divmod(index, 2)
            label = QLabel(title, self.cycle_tools)
            label.setProperty('registrationField', True)
            label.setBuddy(self.pose[index])
            positions.addWidget(label, row*2, column)
            positions.addWidget(self.pose[index], row*2+1, column)
        positions.setColumnStretch(0, 1)
        positions.setColumnStretch(1, 1)
        self.view.action_layout.removeWidget(self.preview_position)
        self.preview_position.setText('Apply position')
        positions.addWidget(self.preview_position, 3, 1)
        manual_layout.addLayout(positions)
        self.cycle_automatic_guidance = QLabel(
            'Fit rotation and translation from the shared stain.', self.cycle_tools)
        self.cycle_automatic_guidance.setWordWrap(True)
        self.cycle_automatic_guidance.setProperty('choiceHelp', True)
        automatic_layout.addWidget(self.cycle_automatic_guidance)
        self.use_pose.setText('Start from current position')
        automatic_layout.addWidget(self.use_pose)
        self.use_pose.setToolTip('Clear to start with the image centres aligned.')
        automatic_layout.addWidget(disclosure)
        automatic_layout.addWidget(self.global_settings)
        self.run_global.setText('Fit automatically')
        automatic_layout.addWidget(self.run_global)
        self.fit_settings_disclosure = disclosure
        layout.insertWidget(0, self.cycle_tools)
        self._select_cycle_tool('position')

    def _select_cycle_tool(self, tool):
        index = 1 if tool == 'automatic' else 0
        self._cycle_tool = 'automatic' if index else 'position'
        self.cycle_tool_buttons[self._cycle_tool].setChecked(True)
        for page_index, page in enumerate(self.cycle_tool_pages):
            page.setVisible(page_index == index)
        if self._meta is not None:
            self._refresh_controls()

    def _arrange_cycle_result(self, layout, footer):
        layout.removeWidget(self.status)
        layout.removeItem(footer)
        while footer.count():
            footer.takeAt(0)
        result = QFrame(self)
        self.cycle_result = result
        row = QHBoxLayout(result)
        row.setContentsMargins(0, 4, 0, 0)
        row.setSpacing(18)
        row.addWidget(self.close_button)
        details = QVBoxLayout()
        details.setSpacing(3)
        self.cycle_result_state = QLabel('Opening alignment…', result)
        self.cycle_result_state.setProperty('panelHeading', True)
        self.cycle_result_state.setWordWrap(True)
        details.addWidget(self.cycle_result_state)
        self.status.setProperty('choiceHelp', True)
        details.addWidget(self.status)
        row.addLayout(details, 1)
        self.save_button.setText('Use cycle alignment')
        row.addWidget(self.save_button)
        layout.addWidget(result)

    def _refresh_cycle_controls(self, busy):
        saved = self._meta is not None and self._meta['mode'] != 'alignment'
        self.cycle_tools.setVisible(not saved and not self.read_only)
        self.side_scroll.setVisible(not saved and not self.read_only)
        self.view.action_bar.hide()
        if self._meta is None:
            state = 'Opening alignment…'
        elif saved:
            state = 'Saved · ' + self._cycle_alignment_method()
        elif self._closing:
            state = 'Closing alignment…'
        elif busy:
            state = {'affine': 'Fitting alignment…', 'pose': 'Applying position…',
                     'save': 'Saving alignment…'}.get(self._action[0], 'Updating alignment…')
        elif self._state.get('final_ready'):
            state = 'Ready to use · ' + self._cycle_alignment_method()
        else:
            state = 'Changes not applied'
        self.cycle_result_state.setText(state)

    def _cycle_alignment_method(self):
        owner = getattr(self.session, 'owner', None)
        index = getattr(self.session, 'member_index', None)
        evidence = owner.evidence[index] if owner is not None and index is not None else {}
        method = evidence.get('method', self._state.get('estimate', {}).get('method', ''))
        return ('Automatic rigid fit' if 'elastix' in method else
                'Manual position' if method == 'manual-rigid' else 'Rigid alignment')

    def _local_panel(self):
        panel, layout = self._panel('Local refinement', 'Inspect the global alignment. Keep it, or correct remaining local differences.')
        self.local_guidance = layout.itemAt(1).widget()
        self.landmark_panel = QWidget(panel)
        landmarks = QVBoxLayout(self.landmark_panel)
        landmarks.setContentsMargins(0, 0, 0, 0)
        guidance = QLabel('Centre the same structure in both images. Add at least 6 pairs distributed across the tissue.', panel)
        guidance.setWordWrap(True)
        landmarks.addWidget(guidance)
        self.landmarks = QTableWidget(0, 3, panel)
        self.landmarks.setHorizontalHeaderLabels(['Pair', 'mIF (px)', 'Paired (px)'])
        style_scientific_table(self.landmarks)
        self.landmarks.setEditTriggers(QTableWidget.NoEditTriggers)
        self.landmarks.setSelectionBehavior(QTableWidget.SelectRows)
        self.landmarks.setSelectionMode(QTableWidget.SingleSelection)
        header = self.landmarks.horizontalHeader()
        header.setMinimumSectionSize(42)
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        self.landmarks.verticalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.landmarks.setMinimumHeight(150)
        self.landmarks.itemSelectionChanged.connect(self._focus_landmark)
        landmarks.addWidget(self.landmarks)
        buttons = QHBoxLayout()
        self.landmark_buttons = {}
        for label, operation in (('Add pair', 'add'), ('Replace', 'replace'), ('Remove', 'remove')):
            button = QPushButton(label, panel)
            button.clicked.connect(lambda checked=False, op=operation: self._edit_landmark(op))
            buttons.addWidget(button)
            self.landmark_buttons[operation] = button
            button.setEnabled(operation == 'add')
        landmarks.addLayout(buttons)
        layout.addWidget(self.landmark_panel)
        self.bspline_panel = QWidget(panel)
        form = QFormLayout(self.bspline_panel)
        form.setContentsMargins(0, 0, 0, 0)
        self.local_view = self._spin(128, 4096, 2048)
        self.local_iterations = self._spin(32, 2048, 256)
        self.grid_spacing = self._spin(.1, 1e6, 400, decimals=2)
        self.bending = self._spin(0, 1, .05, decimals=4)
        for label, spin in (('View size (pixels)', self.local_view), ('Iterations', self.local_iterations),
                            ('Grid spacing (µm)', self.grid_spacing), ('Bending penalty weight', self.bending)):
            form.addRow(label, spin)
            spin.valueChanged.connect(self._local_changed)
        self.local_view.setToolTip('Maximum width or height of each image used for local fitting. Larger views preserve smaller structures and require more image memory and computation.')
        self.local_iterations.setToolTip('Maximum updates to the displacement coefficients at each of three fitting levels. Increasing the limit allows more fitting updates and can increase fitting time.')
        self.grid_spacing.setToolTip('Distance between displacement control points at the finest level. Smaller spacing adds coefficients and allows changes over shorter distances.')
        self.bending.setToolTip('Contribution of the bending penalty to each fitting update, relative to the image-comparison gradient. Larger values favour smoother displacement changes; zero removes this penalty.')
        layout.addWidget(self.bspline_panel)
        maximum = QFormLayout()
        self.maximum_shift = self._spin(.01, 1e6, 200, decimals=2)
        self.maximum_shift.valueChanged.connect(self._local_changed)
        self.maximum_shift.setToolTip('Largest additional displacement permitted at sampled positions after local fitting, in micrometres. A fit exceeding this limit is rejected for review of its settings or starting alignment.')
        maximum.addRow('Maximum local shift (µm)', self.maximum_shift)
        # These forms are visually one settings column. Keep their field edges
        # aligned even though maximum displacement also applies to landmarks.
        label_width = max(form.labelForField(spin).sizeHint().width() for spin in
                          (self.local_view, self.local_iterations, self.grid_spacing, self.bending))
        label_width = max(label_width, maximum.labelForField(self.maximum_shift).sizeHint().width())
        for settings_form, fields in ((form, (self.local_view, self.local_iterations, self.grid_spacing, self.bending)),
                                      (maximum, (self.maximum_shift,))):
            settings_form.setHorizontalSpacing(8)
            for field in fields:
                settings_form.labelForField(field).setMinimumWidth(label_width)
                field.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
                field.setMinimumWidth(110)
        layout.addLayout(maximum)
        keep = QPushButton('Continue with global alignment', panel)
        keep.setObjectName('registration-keep-global')
        keep.clicked.connect(lambda: self._fit_local('global'))
        layout.addWidget(keep)
        self.run_local = QPushButton('Calculate refinement', panel)
        self.run_local.setObjectName('registration-run-local')
        self.run_local.setProperty('actionRole', 'primary')
        self.run_local.clicked.connect(lambda: self._fit_local('landmarks' if self._manual else 'bspline'))
        layout.addWidget(self.run_local)
        if not self.global_only:
            self._arrange_paired_local_controls(panel, layout, maximum, keep)
        layout.addStretch()

    def _arrange_paired_local_controls(self, panel, layout, maximum, keep):
        layout.itemAt(0).widget().hide()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.removeItem(maximum)
        for widget in (self.local_guidance, self.landmark_panel, self.bspline_panel, keep, self.run_local):
            layout.removeWidget(widget)
        self.paired_local_section, refinement = self._paired_section(panel, 'Local refinement')
        self.local_guidance.setProperty('choiceHelp', True)
        refinement.addWidget(self.local_guidance)
        self.landmark_count = QLabel('0 pairs · Add at least 6 pairs.', self.landmark_panel)
        self.landmark_count.setProperty('fieldCaption', True)
        self.landmark_count.setWordWrap(True)
        self.landmark_panel.layout().insertWidget(1, self.landmark_count)
        self.landmark_panel.layout().setStretch(self.landmark_panel.layout().indexOf(self.landmarks), 1)
        self.landmarks.setTextElideMode(Qt.ElideNone)
        refinement.addWidget(self.landmark_panel, 1)
        refinement.addWidget(self.bspline_panel)
        maximum.setRowWrapPolicy(QFormLayout.WrapLongRows)
        refinement.addSpacing(5)
        refinement.addLayout(maximum)
        refinement.addWidget(self.run_local)
        layout.addWidget(self.paired_local_section)
        keep.setToolTip('Review the global affine alignment without adding local displacement.')
        layout.addWidget(keep)

    def _review_panel(self):
        panel, layout = self._panel('Inspect the alignment', 'Compare corresponding structures across the tissue before returning to the alignment decision.')
        self.scope = QComboBox(panel)
        self.scope.setAccessibleName('Region used for geometric review')
        self.scope.currentIndexChanged.connect(self._select_scope)
        layout.addWidget(QLabel('Review region', panel))
        layout.addWidget(self.scope)
        self.boundary_controls = QWidget(panel)
        boundary = QHBoxLayout(self.boundary_controls)
        boundary.setContentsMargins(0, 0, 0, 0)
        boundary.setSpacing(4)
        self.boundary_previous = QToolButton(panel)
        self.boundary_previous.setArrowType(Qt.LeftArrow)
        self.boundary_previous.setAccessibleName('Previous tissue boundary location')
        self.boundary_previous.setToolTip('Previous tissue boundary location')
        self.boundary_previous.clicked.connect(lambda: self._inspect_boundary(-1))
        boundary.addWidget(self.boundary_previous)
        self.boundary_location = QPushButton('Inspect tissue boundary', panel)
        self.boundary_location.setToolTip('Visit 12 locations spaced along the accepted contours. Compare tissue in both images; pan to inspect other boundaries.')
        self.boundary_location.clicked.connect(lambda: self._inspect_boundary(0))
        boundary.addWidget(self.boundary_location, 1)
        self.boundary_next = QToolButton(panel)
        self.boundary_next.setArrowType(Qt.RightArrow)
        self.boundary_next.setAccessibleName('Next tissue boundary location')
        self.boundary_next.setToolTip('Next tissue boundary location')
        self.boundary_next.clicked.connect(lambda: self._inspect_boundary(1))
        boundary.addWidget(self.boundary_next)
        for button in (self.boundary_previous, self.boundary_next):
            button.setMinimumHeight(self.boundary_location.sizeHint().height())
        self.boundary_controls.hide()
        layout.addWidget(self.boundary_controls)
        self.check_summary = QLabel('', panel)
        self.check_summary.setObjectName('registration-geometry-summary')
        self.check_summary.setWordWrap(True)
        layout.addWidget(self.check_summary)
        self.checks = QTableWidget(3, 3, panel)
        self.checks.setObjectName('registration-checks')
        self.checks.setHorizontalHeaderLabels(['Check', 'Measured', 'Limit'])
        style_scientific_table(self.checks)
        self.checks.setEditTriggers(QTableWidget.NoEditTriggers)
        self.checks.setSelectionBehavior(QTableWidget.SelectRows)
        self.checks.setSelectionMode(QTableWidget.SingleSelection)
        self.checks.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.checks.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)
        self.checks.horizontalHeader().setSectionResizeMode(2, QHeaderView.Fixed)
        self.checks.setColumnWidth(1, 96)
        self.checks.setColumnWidth(2, 75)
        self.checks.setColumnHidden(2, True)
        self.checks.setWordWrap(True)
        self.checks.setTextElideMode(Qt.ElideNone)
        # The outer scrollbar can change this width during row sizing.
        self._check_size_timer = QTimer(self.checks)
        self._check_size_timer.setSingleShot(True)
        self._check_size_timer.timeout.connect(self._fit_checks)
        self.checks.horizontalHeader().sectionResized.connect(lambda *_: self._check_size_timer.start(0))
        self.checks.itemSelectionChanged.connect(self._draw_checks)
        layout.addWidget(self.checks)
        self.check_detail = QFrame(panel)
        self.check_detail.setObjectName('registration-check-explanation')
        self.check_detail.setProperty('modeCard', True)
        detail = QVBoxLayout(self.check_detail)
        detail.setContentsMargins(14, 14, 14, 14)
        detail.setSpacing(12)
        self.check_note = QLabel('', self.check_detail)
        self.check_note.setWordWrap(True)
        detail.addWidget(self.check_note)
        self.key = QLabel('', self.check_detail)
        self.key.setWordWrap(True)
        self.key.setProperty('fieldHelp', True)
        detail.addWidget(self.key)
        self.sampling_note = QLabel('', self.check_detail)
        self.sampling_note.setWordWrap(True)
        self.sampling_note.setProperty('fieldHelp', True)
        detail.addWidget(self.sampling_note)
        layout.addWidget(self.check_detail)
        self.tissue_visible = QCheckBox('Show accepted tissue outlines', panel)
        self.tissue_visible.setChecked(True)
        self.tissue_visible.toggled.connect(self._draw_checks)
        layout.insertWidget(4, self.tissue_visible)
        if not self.global_only:
            self._arrange_paired_review_controls(panel, layout)
        layout.addStretch()

    def _arrange_paired_review_controls(self, panel, layout):
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.itemAt(0).widget().hide()
        guidance = layout.itemAt(1).widget()
        guidance.setText('Compare tissue structures across the image.')
        guidance.setProperty('choiceHelp', True)
        region_label = layout.itemAt(layout.indexOf(self.scope)-1).widget()
        region_row = QWidget(panel)
        region_layout = QHBoxLayout(region_row)
        region_layout.setContentsMargins(0, 0, 0, 0)
        region_layout.addWidget(region_label)
        region_layout.addWidget(self.scope, 1)
        layout.insertWidget(2, region_row)
        self.scope.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.scope.setMinimumContentsLength(12)
        self.scope.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.scope.currentTextChanged.connect(self.scope.setToolTip)
        self.check_summary.setProperty('statusBanner', True)
        self.check_detail.setProperty('modeCard', False)
        self.check_detail.setProperty('workbenchSection', True)
        self.check_detail.layout().setContentsMargins(12, 10, 12, 12)
        self.check_detail.layout().setSpacing(8)
        self.check_name = QLabel(self.check_detail)
        self.check_name.setProperty('panelHeading', True)
        self.check_name.setWordWrap(True)
        self.check_detail.layout().insertWidget(0, self.check_name)
        self.check_groups = QWidget(panel)
        groups = QHBoxLayout(self.check_groups)
        groups.setContentsMargins(0, 0, 0, 0)
        groups.setSpacing(0)
        self.check_groups.setStyleSheet(
            'QToolButton {background:transparent; color:#31575a; border:1px solid #bacccd; '
            'border-radius:0; padding:5px 10px;}'
            'QToolButton#registration-check-overall {border-top-left-radius:3px; border-bottom-left-radius:3px;}'
            'QToolButton#registration-check-local {border-top-right-radius:3px; border-bottom-right-radius:3px;}'
            'QToolButton:checked {background:#dceeed; color:#006b70; border-color:#007e83;}'
            'QToolButton:hover {background:#edf5f3;}'
            'QToolButton:focus {border:2px solid #006b70; padding:4px 9px;}')
        self.check_group_buttons = {}
        selection = QButtonGroup(self.check_groups)
        selection.setExclusive(True)
        for name, title in (('overall', 'Overall alignment'), ('local', 'Local refinement')):
            button = QToolButton(self.check_groups)
            button.setText(title)
            button.setObjectName('registration-check-' + name)
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.setCheckable(True)
            button.setChecked(name == 'overall')
            button.clicked.connect(lambda checked=False, value=name: self._show_check_group(value))
            selection.addButton(button)
            groups.addWidget(button)
            self.check_group_buttons[name] = button
        self._check_group = 'overall'
        self._check_group_rows = {'overall': 1, 'local': 5}
        layout.insertWidget(layout.indexOf(self.checks), self.check_groups)
        self.check_groups.hide()

    def _show_check_group(self, group, *, select=True):
        """Page the existing rows while keeping their map and export identities."""
        current = self.checks.currentRow()
        if current in (1, 2, 3) or current >= 5:
            self._check_group_rows['local' if current >= 5 else 'overall'] = current
        local = self.checks.rowCount() > 4
        group = 'local' if group == 'local' and local else 'overall'
        self._check_group = group
        self.check_groups.setVisible(local)
        for name, button in self.check_group_buttons.items():
            button.setChecked(name == group)
        for row in range(self.checks.rowCount()):
            visible = (row >= 5 if group == 'local' else 1 <= row <= 3)
            if row == 0 and not local:
                visible = True
            self.checks.setRowHidden(row, not visible)
        self.checks.setColumnHidden(2, not any(
            not self.checks.isRowHidden(row) and self.checks.item(row, 2) is not None
            and self.checks.item(row, 2).text() for row in range(self.checks.rowCount())))
        if select and (self.checks.currentRow() < 0 or self.checks.isRowHidden(self.checks.currentRow())):
            preferred = self._check_group_rows[group]
            self.checks.selectRow(preferred if preferred < self.checks.rowCount() else 5 if group == 'local' else 1)
        self._fit_checks()

    @property
    def _manual(self):
        return self._meta is not None and self._meta['method'] == 'manual'

    @property
    def _remote_execution(self):
        return not self.global_only and bool(self._meta and self._meta.get('remote_execution'))

    def _restore_remote_alignment(self):
        """Restore validated cluster inputs while opening, without dirtying them."""
        restored = self._meta.get('restored_alignment') or {}
        global_input = restored.get('global')
        if global_input:
            pose = global_input['starting_pose']
            for spin, key in zip(self.pose, ('x_um', 'y_um', 'rotation_degrees', 'scale')):
                spin.setValue(pose[key])
            settings = global_input['global_settings']
            for name, selector in (('fixed_channel', self.fixed_channel), ('moving_channel', self.moving_channel)):
                selector.setCurrentIndex(selector.findData(settings[name]))
            for spin, key in ((self.global_view, 'max_dimension'), (self.global_iterations, 'iterations'),
                              (self.threads, 'threads')):
                spin.setValue(settings[key])
            self.orientation.setChecked(settings['orientation_search'])
            self.use_pose.setChecked(global_input['use_starting_pose'])
        final_input = restored.get('final')
        if final_input:
            self._remote_local_method = 'landmarks' if final_input['local_refinement'] else 'global'
            self._moving_points = deepcopy(final_input['local_moving_points'])
            self._fixed_points = deepcopy(final_input['local_fixed_points'])
            self.maximum_shift.setValue(final_input['maximum_displacement_um'])
            self._update_landmarks()
        self.subtitle.setText('Cluster manual alignment')
        self.run_global.setText('Save for affine fitting')
        self.save_button.setText('Save alignment request')
        self.global_guidance.setText('Save these image signals and starting settings for affine fitting on the cluster.')

    def _retain_remote_draft(self, *, global_changed):
        """Keep unfinished remote inputs through closing without a native wait."""
        if not self._remote_execution:
            return
        try:
            self.session.retain_remote_draft(global_changed=global_changed,
                pose=[spin.value() for spin in self.pose], use_pose=self.use_pose.isChecked(),
                settings=dict(fixed_channel=self.fixed_channel.currentData(), moving_channel=self.moving_channel.currentData(),
                    max_dimension=self.global_view.value(), iterations=self.global_iterations.value(),
                    threads=self.threads.value(), orientation_search=self.orientation.isChecked()),
                method=self._remote_local_method, moving_points=deepcopy(self._moving_points),
                fixed_points=deepcopy(self._fixed_points), maximum_displacement_um=self.maximum_shift.value())
        except RuntimeError as error:
            self.status.setText(str(error))
        self._refresh_controls()

    @property
    def _landmark_mode(self):
        return not self.global_only and self.step == 1 and self._manual

    @property
    def _paired_label(self):
        if getattr(self, 'global_only', False):
            return 'Moving cycle'
        return 'H&E' if self._meta and self._meta['moving'].get('reference_modality') == 'he' else 'Paired image'

    def _open(self, metadata):
        self._changing = True
        self._meta = metadata
        self._state = metadata['state']
        try:
            stage = ('Accepted' if metadata['mode'] == 'overlay' else
                     'Candidate' if metadata['mode'] == 'candidate_overlay' else
                     'Manual' if self._manual else 'Automatic')
            self.subtitle.setText(stage+' · mIF and '+self._paired_label)
            for name, selector in (('fixed', self.fixed_channel), ('moving', self.moving_channel)):
                item = metadata[name]
                if not self.global_only:
                    selector.addItem('RGB stain contrast' if item['rgb'] else 'Combined fluorescence', -1)
                for channel in item['channels']:
                    selector.addItem(channel['label'], channel['index'])
                selector.setCurrentIndex(selector.findData(metadata['defaults']['registration'][name+'_channel']))
                descriptor = dict(mode='rgb' if item['rgb'] else 'colour',
                    channels=[0, 1, 2] if item['rgb'] else [metadata['defaults']['registration'][name+'_channel'] if self.global_only else 0],
                    colors={str(c['index']): c['color'] for c in item['channels']}, limits={})
                if self.global_only:
                    descriptor['colors'][str(descriptor['channels'][0])] = '#33c8c8' if name == 'fixed' else '#ef70b7'
                saved = item.get('appearance')
                if (isinstance(saved, dict) and saved.get('mode') in {'rgb', 'grayscale', 'colour', 'composite'}
                        and isinstance(saved.get('channels'), list) and saved['channels']
                        and all(type(c) is int and 0 <= c < len(item['channels']) for c in saved['channels'])
                        and (saved['mode'] != 'rgb' or item['rgb'])):
                    descriptor.update({key:deepcopy(saved[key]) for key in ('mode', 'channels', 'colors', 'limits') if key in saved})
                self._appearances[name] = descriptor
            self.view.pixel_um = min(metadata['fixed']['x_um'], metadata['fixed']['y_um'])
            self.moving_view.pixel_um = min(metadata['moving']['x_um'], metadata['moving']['y_um'])
            defaults = metadata['defaults']
            for spin, key in ((self.global_view, 'max_dimension'), (self.global_iterations, 'iterations'), (self.threads, 'threads')):
                spin.setValue(defaults['registration'][key])
            self.orientation.setChecked(defaults['registration']['orientation_search'])
            for spin, key in ((self.local_view, 'max_dimension'), (self.local_iterations, 'iterations'),
                              (self.grid_spacing, 'grid_spacing_um'), (self.bending, 'bending_weight')):
                spin.setValue(defaults['bspline'][key])
            self.maximum_shift.setValue(defaults['manual_registration' if self._manual else 'bspline']['maximum_displacement_um'])
            self.pose_panel.setVisible(self._manual and not self.global_only)
            self.global_guidance.setText(
                'Set the starting position, then fit translation, rotation, scale and shear from the image signals.'
                if self._manual else
                'Fit translation, rotation, scale and shear from the image signals. Fit settings controls the search across starting orientations.')
            self.local_guidance.setText(
                'Keep the global affine, or add matching landmark pairs for a local thin-plate spline (TPS).'
                if self._manual else
                'Keep the global affine, or fit a local B-spline from the image signals to correct remaining displacement.')
            self.landmark_overlay.setText(self._paired_label.replace('&', '&&') + ' overlay')
            self.source_buttons['moving'].setText(self._paired_label.replace('&', '&&'))
            self.view.image_label.setToolTip(metadata['fixed']['name'])
            self.moving_view.image_label.setToolTip(metadata['moving']['name'])
            self.tissue_visible.setVisible(bool(metadata.get('tissue')))
            self.landmark_panel.setVisible(self._manual)
            self.bspline_panel.setVisible(not self._manual)
            saved = metadata['mode'] != 'alignment'
            self.save_button.setVisible(not saved)
            for button in self.steps[:2]:
                button.setVisible(not saved and not self.global_only)
            if not self.global_only:
                self.subtitle.setText(stage)
                for source, label in self.paired_source_labels.items():
                    label.set_source_name(metadata[source]['name'])
                self.paired_pose_section.setVisible(self._manual)
                self.use_pose.setVisible(self._manual)
                self.global_guidance.setText(
                    'Fit translation, rotation, scale and shear from the selected signals.')
                self.global_guidance.setVisible(not self._manual)
                self.local_guidance.setText(
                    'Use matching landmarks to correct remaining local displacement.' if self._manual else
                    'Fit local displacement from the same image signals.')
                self.local_guidance.setVisible(not self._manual)
                self.paired_local_section.layout().itemAt(0).widget().setText(
                    'Landmark refinement (TPS)' if self._manual else 'Local B-spline refinement')
                local_layout = self.paired_local_section.parentWidget().layout()
                local_layout.setStretch(local_layout.indexOf(self.paired_local_section), 1 if self._manual else 0)
                local_layout.setStretch(local_layout.count()-1, 0 if self._manual else 1)
                self.steps[2].setVisible(not saved)
            if self.global_only:
                for source, label in self.cycle_source_labels.items():
                    label.set_source_name(getattr(self, 'cycle_names', {}).get(source, metadata[source]['name']))
                    label.setToolTip(metadata[source]['name'])
                self.global_guidance.setText('Shift + drag to move the cycle, or enter shifts and a rotation.')
                self.source_buttons['fixed'].setText('Reference')
                self.fixed_channel.setEnabled(False)
                self.moving_channel.setEnabled(False)
                self.run_global.setEnabled(metadata.get('automatic_available', True))
                self.run_global.setToolTip('Fit rigid rotation and translation using Elastix.' if metadata.get('automatic_available', True) else 'Install Elastix and Transformix in Software setup to use automatic fitting.')
                if not metadata.get('automatic_available', True):
                    self.cycle_automatic_guidance.setText(
                        'Install Elastix and Transformix in Software setup to enable automatic fitting.')
                for spin, value in zip(self.pose, metadata.get('pose', [0., 0., 0., 1.])):
                    spin.setValue(value)
            if self._remote_execution:
                self._restore_remote_alignment()
            self._set_scopes(metadata.get('scopes', []))
            initial_step = (2 if self._state.get('final_ready') else 1 if self._state.get('global_ready') else 0)
            self._select_step(2 if saved else initial_step if self._remote_execution else 0)
            if self.global_only and self._cycle_alignment_method() == 'Automatic rigid fit':
                self._select_cycle_tool('automatic')
            self._refresh_controls()
            self.status.setText('Loading image overview…')
        finally:
            self._changing = False
        self._pending_views.add('fixed')
        self._fetch()

    def _refresh_controls(self):
        busy = self._action is not None and self._action[0] not in {'render', 'open'}
        available = self._meta is not None and not busy and not self._closing
        self.panels.setEnabled(available)
        if not self.global_only:
            self.paired_stage_actions.setEnabled(available)
            self.paired_stage_actions.setVisible(self._meta is not None and self.step < 2
                                                and self._meta['mode'] == 'alignment')
            if self.step < 2:
                self.paired_stage_actions.setCurrentIndex(self.step)
            self.save_button.setVisible(self._meta is not None and self._meta['mode'] == 'alignment'
                                        and (self.step == 2 or bool(self._state.get('final_ready'))))
            save_role = 'primary' if self.step == 2 else 'secondary'
            if self.save_button.property('actionRole') != save_role:
                self.save_button.setProperty('actionRole', save_role)
                self.save_button.style().unpolish(self.save_button)
                self.save_button.style().polish(self.save_button)
        self.steps[1].setEnabled(bool(self._state.get('global_ready')) and not busy)
        self.steps[2].setEnabled(bool(self._state.get('final_ready')) and not busy)
        self.next_local.setEnabled(bool(self._state.get('global_ready')))
        self.save_button.setEnabled(bool(self._state.get('final_ready')) and not busy and not self._closing)
        if self._remote_execution:
            draft_available = available and not getattr(self.session, 'draft_error', None)
            self.run_global.setEnabled(draft_available)
            self.save_button.setEnabled(draft_available and bool(self._state.get('final_ready')))
        self.appearance_button.setEnabled(available)
        self.orientation.setEnabled(not (self._manual and self.use_pose.isChecked()))
        positioning = self._manual and self.step == 0 and self._meta['mode'] == 'alignment'
        if self.global_only:
            positioning = positioning and self._cycle_tool == 'position' and not self.read_only
        self.view.positioning = positioning and available
        self.preview_position.setVisible(positioning)
        self.preview_position.setEnabled(available)
        self.view.action_bar.setVisible(positioning)
        self.align_centers.setVisible(self._landmark_mode)
        self.align_centers.setEnabled(available)
        self.moving_view.action_bar.setVisible(self._landmark_mode)
        self.landmark_overlay.setVisible(self._landmark_mode)
        self.navigation_hint.setText('Drag to pan · Ctrl + scroll to zoom'
                                    + (' · Shift + drag to position the moving cycle' if positioning and self.global_only else
                                       ' · Shift + drag to position the paired image' if positioning else '')
                                    + (' · Teal: reference stain · Pink: moving stain' if self.global_only else ''))
        self.navigation_hint.setText('Drag to pan · Ctrl + scroll to zoom')
        if self.global_only:
            self._refresh_cycle_controls(busy)
        self.view.position_controls()
        self.moving_view.position_controls()
        self._refresh_export()

    def _refresh_export(self):
        accepted = self._meta is not None and self._meta['mode'] == 'overlay'
        self.export_button.setVisible(accepted)
        self.export_button.setEnabled(accepted and not self._closing and self._job is None
            and not self._pending_views and not self.timer.isActive()
            and self._loaded_generation == self._generation and ('fixed', False) in self._buffers)

    def _export_alignment(self, kind):
        self._refresh_export()
        if not self.export_button.isEnabled():
            return
        from . import registration_exports as exports
        from .figure_export import _choose_path, _save_bytes, export_figure
        try:
            if kind in {'figure', 'field'}:
                bundle = exports.capture_field(self)
                if kind == 'figure':
                    export_figure(self, exports.field_figure(bundle), 'aligned-field.svg')
                    return
                payload = exports.field_npz(bundle)
                path = _choose_path(self, 'aligned-field.npz', {'NumPy field data (*.npz)': 'npz'})
            else:
                scope = self._scopes[self.scope.currentIndex()]
                payload = (exports.check_summary_csv(scope) if kind == 'summary' else
                           exports.sampled_checks_csv(scope, (self._meta['fixed']['x_um'], self._meta['fixed']['y_um'])))
                path = _choose_path(self, 'alignment-'+kind+'.csv', {'CSV table (*.csv)': 'csv'})
            if path is not None:
                _save_bytes(payload, path)
        except (OSError, ValueError, RuntimeError) as error:
            QMessageBox.warning(self, 'Alignment not exported', str(error))

    def _select_step(self, index):
        if self.global_only and index != 0:
            return
        if self._meta is None:
            return
        if index == 1 and not self._state.get('global_ready'):
            return
        if index == 2 and not self._state.get('final_ready'):
            return
        self.step = index
        self.steps[index].setChecked(True)
        # Hidden fitting pages must not impose their width on the Review panel.
        for page_index in range(self.panels.count()):
            policy = QSizePolicy.Preferred if page_index == index else QSizePolicy.Ignored
            self.panels.widget(page_index).setSizePolicy(policy, policy)
        self.panels.setCurrentIndex(index)
        self.panels.layout().invalidate()
        self.panels.layout().activate()
        self.panels.updateGeometry()
        pair = self._landmark_mode
        self.moving_view.setVisible(pair)
        if pair:
            self.views.setSizes([self.views.width() // 2] * 2)
        self.view.crosshair = pair
        self.moving_view.crosshair = pair
        if pair:
            self._pending_views.add('moving')
            self.timer.start()
        self._apply_source()
        self._refresh_controls()
        self._draw_checks()
        self._draw_landmarks()

    def _global_changed(self, *_):
        if self._changing or self._meta is None:
            return
        self._state.update(global_ready=False, final_ready=False)
        self._generation += 1
        self._refresh_controls()
        self.status.setText('Apply the position or fit automatically to update the alignment.' if self.global_only else
                            'Settings changed. Recalculate the global alignment.')
        if self._remote_execution:
            self.status.setText('Settings changed. Save a new affine fitting request.')
            self._retain_remote_draft(global_changed=True)

    def _local_changed(self, *_):
        if self._changing or self._meta is None:
            return
        self._state['final_ready'] = False
        self._refresh_controls()
        self.status.setText('Refinement changed. Recalculate before saving.')
        if self._remote_execution:
            self._remote_local_method = 'landmarks'
            self._retain_remote_draft(global_changed=False)

    def _preview_pose(self):
        self._operate('pose', pose=[spin.value() for spin in self.pose])

    def _drag_pose(self, dx, dy):
        if self._closing or (self._job is not None and self._action[0] != 'render'):
            return
        self.pose[0].setValue(self.pose[0].value()+dx)
        self.pose[1].setValue(self.pose[1].value()+dy)
        self._preview_pose()

    def _preview_drag(self, dx, dy):
        """Move the displayed paired layer during the gesture; Java commits its pose."""
        for source, item in self._overview['fixed'] + self._detail['fixed']:
            if source == 'moving':
                origin = item.data(99)
                if origin is None:
                    origin = item.pos()
                    item.setData(99, origin)
                item.setPos(origin + QPointF(dx, dy))

    def _fit_global(self):
        settings = dict(fixed_channel=self.fixed_channel.currentData(), moving_channel=self.moving_channel.currentData(),
                        max_dimension=self.global_view.value(), iterations=self.global_iterations.value(),
                        threads=self.threads.value(), orientation_search=self.orientation.isChecked())
        if self.global_only:
            settings['transform_model'] = 'rigid'
        self._operate('affine', settings=settings, use_pose=self._manual and self.use_pose.isChecked(),
                      pose=[spin.value() for spin in self.pose])

    def _fit_local(self, method):
        if self._remote_execution:
            self._remote_local_method = method
        settings = dict(max_dimension=self.local_view.value(), iterations=self.local_iterations.value(),
                        grid_spacing_um=self.grid_spacing.value(), bending_weight=self.bending.value(),
                        maximum_displacement_um=self.maximum_shift.value())
        self._operate('local', method=method, settings=settings,
                      moving_points=deepcopy(self._moving_points), fixed_points=deepcopy(self._fixed_points))

    def _operate(self, operation, **values):
        if self.read_only and operation in {'pose', 'affine', 'local', 'save'}:
            return
        if self._closing:
            return
        if self._job is not None and self._action[0] == 'render':
            self._queued_operation = (operation, values)
            self.status.setText('Finishing the current image read…')
            return
        if self._job is not None:
            self.status.setText('Wait for the current image read or calculation to finish.')
            return
        self.timer.stop()
        self._generation += 1
        if operation == 'affine':
            self._state.update(global_ready=False, final_ready=False)
        elif operation in {'local', 'pose'}:
            self._state['final_ready'] = False
        self._action = (operation, deepcopy(values.get('settings', {})) if operation == 'local' else None)
        self._job = self.jobs.submit('registration_'+operation,
                                    lambda: self.session.request(operation, **values), access=JobAccess.MUTATION)
        self.status.setText({'affine':'Fitting rigid cycle alignment…' if self.global_only else 'Fitting global affine alignment…', 'local':'Calculating the final transformation and checks…',
                             'pose':'Applying the cycle position…' if self.global_only else 'Updating the starting position…',
                             'save':'Saving the cycle alignment…' if self.global_only else 'Saving the candidate for pipeline review…'}.get(operation, 'Reading coordinates…'))
        if self._remote_execution and operation in {'affine', 'save'}:
            self.status.setText('Saving affine fitting request…' if operation == 'affine' else 'Saving alignment request…')
        self._refresh_controls()

    def set_source(self, source):
        if source not in self.source_buttons:
            raise ValueError('Select an available image source.')
        self.source = source
        self.source_buttons[source].setChecked(True)
        self._apply_source()
        self.native_button.setToolTip('One paired-image source pixel per screen pixel' if source == 'moving'
                                      else 'One mIF source pixel per screen pixel')
        if self.global_only:
            self.native_button.setToolTip('One moving-cycle source pixel per screen pixel' if source == 'moving'
                                          else 'One reference-image source pixel per screen pixel')
        if self._meta is not None:
            self._schedule('fixed')

    def _apply_source(self, *_):
        source = self._display_source()
        fixed = 0. if source == 'moving' else 1.
        moving = 0. if source == 'fixed' else 1. if source == 'moving' else self.opacity.value()/100
        for items in (self._overview['fixed'], self._detail['fixed']):
            for name, item in items:
                item.setOpacity(fixed if name == 'fixed' else moving)
        for button in self.source_buttons.values():
            button.setVisible(not self._landmark_mode)
        # Keep the viewport dimensions stable when the optional overlay toggles.
        self.display_bar.setVisible(True)
        self.opacity.setEnabled(source == 'overlay')
        self.opacity_label.setText(f'{self.opacity.value()}%')
        fixed_name = 'Reference' if self.global_only else 'mIF'
        self.view.image_label.setText({'fixed':fixed_name, 'moving':self._paired_label,
                                      'overlay':fixed_name+' + '+self._paired_label}[source])
        self.moving_view.image_label.setText(self._paired_label)
        self.view.position_controls()
        self.moving_view.position_controls()
        if self._meta is not None:
            self._refresh_channel_legends()
            self._update_locator()

    def _display_source(self):
        """Landmark panes retain both originals; the left overlay is optional."""
        if self._landmark_mode:
            return 'overlay' if self.landmark_overlay.isChecked() else 'fixed'
        return self.source

    def native_scale(self):
        if self._meta is None:
            return
        centre = self.view.mapToScene(self.view.viewport().rect().center())
        self.view._fit = False
        self.view.resetTransform()
        native = self._meta['moving' if self._display_source() == 'moving' else 'fixed']
        self.view.scale(1/native['x_um'], 1/native['y_um'])
        self.view.centerOn(centre)
        self._schedule('fixed')

    def _schedule(self, name):
        if self._changing or self._closing or self._meta is None:
            return
        if name == 'moving' and not self.moving_view.isVisible():
            return
        self._generation += 1
        self._pending_views.add(name)
        self._update_locator()
        self.timer.start()
        self._refresh_export()

    def _update_locator(self):
        bounds = self.view.sceneRect()
        visible = self.view.mapToScene(self.view.viewport().rect()).boundingRect()
        detailed = (not bounds.isEmpty() and
                    (visible.width() < bounds.width()*.85 or visible.height() < bounds.height()*.85))
        self.locator.setFixedSize(112, 123) if self._landmark_mode else self.locator.setFixedSize(150, 165)
        space = (self.view.viewport().width()>=self.locator.width()+20 and
                 self.view.viewport().height()>=self.view.channel_legend.geometry().bottom()+self.locator.height()+30)
        self.locator.setVisible(detailed and space and bool(self._overview['fixed']))
        self.view.position_controls()
        self.locator.update()

    def _fetch(self):
        self._refresh_export()
        if self._job is not None or self._closing or self._meta is None or not self._pending_views:
            return
        name = 'fixed' if 'fixed' in self._pending_views else 'moving'
        self._pending_views.discard(name)
        if name == 'moving' and not self.moving_view.isVisible():
            self._refresh_export()
            return
        overview = name in self._overview_needed
        metadata = self._meta[name]
        viewer = self.view if name == 'fixed' else self.moving_view
        if overview:
            x, y, w, h = 0, 0, metadata['width'], metadata['height']
            downsample = max(1., max(w, h)/1024.)
        else:
            visible = viewer.mapToScene(viewer.viewport().rect()).boundingRect()
            visible.adjust(-visible.width()*.12, -visible.height()*.12, visible.width()*.12, visible.height()*.12)
            x, y = max(0, math.floor(visible.left()/metadata['x_um'])), max(0, math.floor(visible.top()/metadata['y_um']))
            right = min(metadata['width'], math.ceil(visible.right()/metadata['x_um']))
            bottom = min(metadata['height'], math.ceil(visible.bottom()/metadata['y_um']))
            w, h = right-x, bottom-y
            if w <= 0 or h <= 0:
                self.timer.start()
                return
            finest = (min(1., self._meta['moving']['x_um']/metadata['x_um'],
                          self._meta['moving']['y_um']/metadata['y_um']) if name == 'fixed' else 1.)
            downsample = max(finest, 1/(viewer.transform().m11()*metadata['x_um']), max(w, h)/1600.)
            if abs(downsample-1) < 1e-10:
                downsample = 1.
        generation = self._generation
        sources = ('moving',) if name == 'moving' else ('fixed', 'moving')
        # Bound both the returned selected channels and the native server's
        # source raster. Large multiplex composites can require a coarser read.
        selected = max(len(self._appearances[source]['channels']) for source in sources)
        native_channels = max(len(self._meta[source]['channels']) for source in sources)
        downsample = max(downsample, math.sqrt(w*h*selected/16_777_216),
                         math.sqrt(w*h*native_channels/67_108_864))
        while (math.ceil(w/downsample)*math.ceil(h/downsample)*selected > 16_777_216
               or math.ceil(w/downsample)*math.ceil(h/downsample)*native_channels > 67_108_864):
            downsample *= 1.01
        spec = dict(x=x, y=y, width=w, height=h, downsample=downsample, space=name,
                    fixed_channels=self._appearances['fixed']['channels'], moving_channels=self._appearances['moving']['channels'])
        self._action = ('render', (name, overview, generation))
        self._job = self.jobs.submit('registration_viewport', lambda: self.session.request('render', **spec), access=JobAccess.READ)

    def _frame(self, data, name, overview, generation):
        self.frames.append({key:value for key, value in data.items() if key not in {'arrays', 'images'}})
        if len(self.frames) > 100:
            del self.frames[:-100]
        if not overview and (generation != self._generation or data['revision'] != self._state['revision']):
            self._pending_views.add(name)
            return
        if any(not set(self._appearances[image['source']]['channels']).issubset(image['channels']) for image in data['images']):
            self._pending_views.add(name)
            return
        self._changing = True
        try:
            viewer = self.view if name == 'fixed' else self.moving_view
            metadata = self._meta[name]
            items = self._overview[name] if overview else self._detail[name]
            for _, item in items:
                viewer.scene().removeItem(item)
            items.clear()
            for image, values in zip(data['images'], data['arrays']):
                source = image['source']
                descriptor = self._appearances[source]
                for channel, pixels in zip(image['channels'], values):
                    key = str(channel)
                    if key not in descriptor['limits']:
                        finite = pixels[np.isfinite(pixels)]
                        low, high = np.percentile(finite, (1., 99.5)) if finite.size else (0., 1.)
                        descriptor['limits'][key] = [float(low), float(high if high > low else low+1)]
                selected=descriptor['channels']
                rendered = _channel_image(values[[image['channels'].index(c) for c in selected]], selected, descriptor)
                item = viewer.scene().addPixmap(QPixmap.fromImage(rendered))
                item.setZValue((-10 if overview else -5) + (1 if source == 'moving' else 0))
                item.setTransform(QTransform.fromScale(data['downsample']*metadata['x_um'], data['downsample']*metadata['y_um']))
                item.setPos(data['x']*metadata['x_um'], data['y']*metadata['y_um'])
                item.setAcceptedMouseButtons(Qt.NoButton)
                items.append((source, item))
            viewer.image_item = self._overview[name][0][1]
            if overview and name == 'fixed':
                # The locator retains the fixed overview even while its main
                # scene layer is hidden to inspect the paired stain alone.
                original = self._overview[name][0][1]
                locator_image = QGraphicsPixmapItem(original.pixmap())
                locator_image.setTransform(original.transform())
                locator_image.setPos(original.pos())
                viewer.overview_image_item = locator_image
            viewer.setBackgroundBrush(QColor('#172329'))
            viewer.setSceneRect(QRectF(0, 0, metadata['width']*metadata['x_um'], metadata['height']*metadata['y_um']))
            first = overview and name in self._overview_needed and (name, False) not in self._buffers
            self._overview_needed.discard(name)
            self._buffers[(name, overview)] = data
            if first:
                viewer.reset_view()
            self._loaded_generation = generation
            self._apply_source()
            self._update_locator()
            self.status.setText('Global alignment ready. Keep it or choose local refinement.'
                                if self._state.get('global_ready') and not self._state.get('final_ready') else '')
        finally:
            self._changing = False
        self._save_rendered_appearances()
        if overview:
            self._pending_views.add(name)

    def _ready(self, job, result):
        if job != self._job:
            return
        operation, context = self._action
        self._job = None
        self._action = None
        if self._closing:
            self._drained()
            return
        if operation == 'open':
            self._open(result)
            return
        if operation == 'render':
            self._frame(result, *context)
        elif operation == 'points':
            point = result['points'][0]
            metadata = self._meta['moving']
            self.moving_view.centerOn(point[0]*metadata['x_um'], point[1]*metadata['y_um'])
            self._schedule('moving')
        else:
            self._state.update(result)
            if self.global_only and 'pose' in result:
                self._changing = True
                for spin, value in zip(self.pose, result['pose']):
                    spin.setValue(value)
                self._changing = False
            if operation == 'save' or (self._remote_execution and operation == 'affine'
                                       and result.get('remote_saved_stage') == 'global'):
                self.candidate_saved.emit(result)
                self.request_close()
                return
            for name in ('fixed',):
                # Drop transformed pixels immediately; a changed transform must
                # never leave its former detail visible over a fresh overview.
                for _, item in self._detail[name]+self._overview[name]:
                    self.view.scene().removeItem(item)
                self._detail[name].clear()
                self._overview[name].clear()
                self._overview_needed.add(name)
                self._pending_views.add(name)
            if operation == 'affine':
                self._fitted_local_settings = {}
                self._moving_points.clear()
                self._fixed_points.clear()
                self._update_landmarks()
                self.status.setText('Automatic fit complete.' if self.global_only else
                    'Global fit ready. Inspect the overlay, then continue to local refinement.')
            elif operation == 'pose' and self.global_only:
                self.status.setText('Position applied.')
            elif operation == 'local':
                self._fitted_local_settings = deepcopy(context or {})
                self._set_scopes(result['scopes'])
                self._select_step(2)
            self._draw_checks()
        self._refresh_controls()
        if self._queued_operation is not None:
            operation, values = self._queued_operation
            self._queued_operation = None
            self._operate(operation, **values)
            return
        if self._pending_views:
            self.timer.start(1)

    def _failed(self, job, error):
        if job != self._job:
            if not self._closing:
                self.status.setText(error.message)
            return
        operation = self._action[0]
        self._job = None
        self._action = None
        if operation == 'pose':
            for source, item in self._overview['fixed'] + self._detail['fixed']:
                if source == 'moving' and item.data(99) is not None:
                    item.setPos(item.data(99))
                    item.setData(99, None)
        if not self._closing:
            self.status.setText(error.message)
            self.status.setToolTip(error.diagnostic)
            self._refresh_controls()
        self._drained()

    def _set_scopes(self, scopes):
        self._scopes = scopes
        self.scope.blockSignals(True)
        self.scope.clear()
        self.scope.addItems([scope['name'] for scope in scopes])
        self.scope.blockSignals(False)
        if scopes:
            self._select_scope(0, focus=False)

    def _select_scope(self, index, focus=True):
        if not 0 <= index < len(self._scopes):
            return
        scope = self._scopes[index]
        fixed = self._meta.get('fixed') if self._meta else None
        self._boundary_points = contour_locations(scope['rings'], (fixed['x_um'], fixed['y_um'])) if fixed else []
        self._boundary_index = -1
        self.boundary_location.setText('Inspect tissue boundary')
        self.boundary_location.setToolTip('Visit 12 locations spaced along the accepted contours. Compare tissue in both images; pan to inspect other boundaries.')
        self.boundary_controls.setVisible(bool(self._boundary_points))
        evidence = scope['evidence']
        deformation = evidence.get('deformation', {})
        def fraction(name):
            value = evidence.get(name)
            return 'Unavailable' if value is None else f'{value*100:.1f}%'
        minimum, maximum = deformation.get('jacobian_min'), deformation.get('jacobian_max')
        tissue_scope = bool(scope['rings'])
        values = [('Tissue in paired image' if tissue_scope else 'Image-frame coverage', fraction('coverage_fraction')),
                  ('Outside paired image', fraction('out_of_bounds_fraction')),
                  ('Local area ratio', 'Unavailable' if minimum is None or maximum is None else f'{minimum:.4g} – {maximum:.4g}')]
        policy = self._meta.get('policy', {}) if self._meta else {}
        limits = [f"≥ {policy['min_coverage_fraction']*100:.1f}%" if policy.get('min_coverage_fraction') is not None else '',
                  f"≤ {policy['max_out_of_bounds_fraction']*100:.1f}%" if policy.get('max_out_of_bounds_fraction') is not None else '',
                  f"{policy['min_jacobian']:.3g}–{policy['max_jacobian']:.3g}" if policy.get('min_jacobian') is not None and policy.get('max_jacobian') is not None else '']
        if not tissue_scope and policy:
            limits[:2] = ['Not applied', 'Not applied']
        local = self._local_check_rows()
        current = self.checks.currentRow()
        self.checks.blockSignals(True)
        self.checks.clearSpans()
        self.checks.setRowCount(4 + (1 + len(local) if local else 0))
        self.checks.setColumnHidden(2, not any(limits) and not any(item[2] for item in local))
        for row, title in (((0, 'Overall alignment'),) + (((4, 'Local refinement'),) if local else ())):
            heading = QTableWidgetItem(title)
            heading.setFlags(Qt.ItemIsEnabled)
            font = heading.font(); font.setBold(True); heading.setFont(font)
            heading.setBackground(self.checks.palette().alternateBase())
            self.checks.setItem(row, 0, heading)
            self.checks.setSpan(row, 0, 1, 3)
        for index, cells in enumerate(values):
            for column, value in enumerate((*cells, limits[index])):
                self.checks.setItem(index + 1, column, QTableWidgetItem(value))
        for row, (name, measured, limit, explanation) in enumerate(local, 5):
            for column, value in enumerate((name, measured, limit)):
                item = QTableWidgetItem(value)
                item.setToolTip(explanation['note'])
                self.checks.setItem(row, column, item)
            self.checks.item(row, 0).setData(Qt.UserRole, explanation)
        if hasattr(self, 'check_groups'):
            self._show_check_group('local' if current >= 5 and local else 'overall', select=False)
        self.checks.blockSignals(False)
        if current >= self.checks.rowCount() or current in (0, 4):
            self.checks.selectRow(1)
        if self.checks.currentRow() < 0:
            self.checks.selectRow(1)
        self._fit_checks()
        self._update_check_summary(evidence)
        self._draw_checks()
        if focus and self._meta is not None:
            grid = scope['grid']
            meta = self._meta['fixed']
            field = QRectF(grid['x']*meta['x_um'], grid['y']*meta['y_um'],
                           grid['columns']*grid['spacing']*meta['x_um'], grid['rows']*grid['spacing']*meta['y_um'])
            self.view._fit = False
            self.view.fitInView(field, Qt.KeepAspectRatio)
            self._schedule('fixed')

    def _inspect_boundary(self, offset=0):
        if not self._boundary_points:
            return
        self._boundary_index = ((0 if offset >= 0 else len(self._boundary_points)-1)
            if self._boundary_index < 0 else (self._boundary_index + offset) % len(self._boundary_points))
        location = self._boundary_points[self._boundary_index]
        x, y = location['point_um']
        visible = self.view.mapToScene(self.view.viewport().rect()).boundingRect()
        self.view._fit = False
        if max(visible.width(), visible.height()) > 420:
            aspect = self.view.viewport().width() / max(1, self.view.viewport().height())
            width, height = 400*min(1., aspect), 400*min(1., 1/aspect)
            self.view.fitInView(QRectF(x-width/2, y-height/2, width, height), Qt.KeepAspectRatio)
        self.view.centerOn(x, y)
        self.tissue_visible.setChecked(True)
        self.checks.selectRow(1)
        self.boundary_location.setText(f'Boundary {self._boundary_index+1} / {len(self._boundary_points)}')
        self.boundary_location.setToolTip(f"Contour {location['ring']+1} · mIF {x:.3f}, {y:.3f} µm. Click to centre this location again.")
        self._schedule('fixed')

    def _update_check_summary(self, evidence):
        """Expose existing sampled checks without implying anatomical validation."""
        issues = []
        for scope in self._scopes:
            item = scope['evidence']
            folds = item.get('deformation', {}).get('negative_jacobian_count')
            problems = []
            if item.get('bounds_finite') is False:
                problems.append('some positions or area changes could not be calculated')
            if folds is not None and folds > 0:
                problems.append(f'{folds:,} sampled positions with a fold or collapse')
            if problems:
                issues.append(scope['name'] + ': ' + '; '.join(problems))
        if issues:
            self.check_summary.setText('Geometry needs attention\n' + '\n'.join(issues))
            self.check_summary.setStyleSheet('color:#a02d35; font-weight:600;')
        else:
            folds = evidence.get('deformation', {}).get('negative_jacobian_count')
            if evidence.get('bounds_finite') is True and folds == 0:
                text = 'All sampled positions could be mapped. No folds or collapsed areas were found at those positions.'
                if not getattr(self, 'global_only', False):
                    text = 'All sampled positions could be mapped. No folds or collapse were found.'
            else:
                text = 'Position and area checks are unavailable for this region.'
            self.check_summary.setText(text)
            self.check_summary.setStyleSheet('color:#173d47;')

    def _local_check_rows(self):
        """Present only diagnostics returned by the existing candidate fitter."""
        estimate = getattr(self, '_state', {}).get('estimate', {})
        qc = estimate.get('qc', {})
        count = qc.get('sampleCount')
        sampling = (f'The check evaluated {count:,} locations' if count is not None else 'The check uses sampled locations')
        sampling += ' on a 17 × 17 grid across the mIF image'
        if (getattr(self, '_meta', None) or {}).get('tissue'):
            sampling += ', restricted to accepted tissue'
        sampling += '.'
        rows = []
        def add(name, value, limit, note, source=sampling):
            rows.append((name, value, limit, dict(note=note, sampling=source)))
        if qc.get('maximumDisplacementUm') is not None:
            limit = getattr(self, '_fitted_local_settings', {}).get('maximum_displacement_um')
            add('Maximum local shift', f"{qc['maximumDisplacementUm']:.4g} µm",
                f'≤ {limit:g} µm' if limit is not None else '',
                'Largest distance between a sampled point’s position after global alignment and its position after local refinement, in micrometres.')
        if qc.get('minimumJacobian') is not None and qc.get('maximumJacobian') is not None:
            add('Area change from local refinement', f"{qc['minimumJacobian']:.4g} – {qc['maximumJacobian']:.4g}", '> 0.05\n≤ 20',
                'Area after local refinement divided by the area after global alignment, measured at the same sampled locations. '
                'Ratios below 1 show additional compression; ratios above 1 show additional expansion.')
        if qc.get('maximumInverseErrorPixels') is not None:
            add('Inverse round-trip error', f"{qc['maximumInverseErrorPixels']:.4g} px", '≤ 0.5 px',
                'Largest distance between a sampled point in the paired image and the position returned after mapping it to mIF and back. '
                'Reported in paired-image pixels.')
        if estimate.get('method') == 'elastix-affine-bspline' and estimate.get('independentPointErrorPixels') is not None:
            add('Transformix agreement', f"{estimate['independentPointErrorPixels']:.4g} px", '≤ 0.01 px',
                'Largest difference between positions calculated by this application and by Elastix’s Transformix program from the same saved B-spline transformation. '
                'Reported in paired-image pixels.',
                'Comparison with Transformix')
        if estimate.get('method') == 'global-affine-local-tps' and estimate.get('landmarkCount'):
            add('Fitted landmark pairs', f"{estimate['landmarkCount']:,}", '',
                'Landmark pairs used to fit the local thin-plate spline.', 'Landmarks used for fitting')
            if estimate.get('rmsMicrometres') is not None:
                add('Fitted landmark RMS', f"{estimate['rmsMicrometres']:.4g} µm", '',
                    'Root-mean-square distance between each transformed paired-image landmark and its matching mIF landmark, in micrometres.',
                    'Landmarks used for fitting')
        return rows

    def _fit_checks(self, *_):
        if getattr(self, '_sizing_checks', False):
            return
        self._sizing_checks = True
        try:
            if not self.global_only:
                metrics = self.checks.fontMetrics()
                for column in (1, 2):
                    if self.checks.isColumnHidden(column):
                        continue
                    values = [self.checks.horizontalHeaderItem(column).text()]
                    values.extend(self.checks.item(row, column).text()
                                  for row in range(self.checks.rowCount())
                                  if not self.checks.isRowHidden(row) and self.checks.item(row, column) is not None)
                    width = max(metrics.horizontalAdvance(line)
                                for value in values for line in value.splitlines()) + 36
                    self.checks.setColumnWidth(column, width)
            self.checks.resizeRowsToContents()
            for row in range(self.checks.rowCount()):
                if self.checks.isRowHidden(row):
                    continue
                heading = self.checks.columnSpan(row, 0) > 1
                height = 34 if heading else 44
                if not heading:
                    for column in range(3):
                        item = self.checks.item(row, column)
                        if item is not None and not self.checks.isColumnHidden(column):
                            text = self.checks.fontMetrics().boundingRect(
                                QRect(0, 0, max(1, self.checks.columnWidth(column)-28), 2000),
                                Qt.TextWordWrap, item.text())
                            height = max(height, text.height()+18)
                self.checks.setRowHeight(row, height)
            self.checks.setFixedHeight(self.checks.horizontalHeader().height()
                + sum(self.checks.rowHeight(row) for row in range(self.checks.rowCount())
                      if not self.checks.isRowHidden(row)) + 2*self.checks.frameWidth())
        finally:
            self._sizing_checks = False

    def _path(self, rings, color, width=1.5):
        path = QPainterPath()
        meta = self._meta['fixed']
        for ring in rings:
            for i, point in enumerate(ring):
                (path.moveTo if i == 0 else path.lineTo)(point[0]*meta['x_um'], point[1]*meta['y_um'])
            path.closeSubpath()
        pen = QPen(QColor(color), width)
        pen.setCosmetic(True)
        item = self.view.scene().addPath(path, pen)
        item.setZValue(10)
        item.setAcceptedMouseButtons(Qt.NoButton)
        self._paths.append(item)

    def set_review_crop(self, crop, *, valid=True):
        """Redraw a retained rectangle in reference pixels without reading images."""
        self.review_crop = tuple(crop)
        self.review_crop_valid = valid
        self._draw_checks()

    def _draw_checks(self, *_):
        for item in self._paths:
            self.view.scene().removeItem(item)
        self._paths.clear()
        if self._grid_item is not None:
            self.view.scene().removeItem(self._grid_item)
            self._grid_item = None
        if self._meta is None:
            return
        if self.global_only:
            self.view.cycle_outline_legend.set_crop(self.review_crop, valid=self.review_crop_valid)
            self.view.cycle_outline_legend.show()
            self.view.position_controls()
        if self.step == 2 or getattr(self, 'global_only', False):
            meta = self._meta['fixed']
            index = self.scope.currentIndex()
            tissue_scope = 0 <= index < len(self._scopes) and bool(self._scopes[index]['rings'])
            if self.global_only or not tissue_scope:
                self._path([[[0, 0], [meta['width'], 0], [meta['width'], meta['height']], [0, meta['height']]]], '#23bcb8')
            self._path([self._state.get('outline', [])], '#ef70b7' if self.global_only else '#f3b746')
        if getattr(self, 'global_only', False) and self.review_crop is not None:
            x, y, width, height = self.review_crop
            if self.read_only and self.review_crop_valid:
                meta = self._meta['fixed']
                outside = QPainterPath()
                outside.addRect(QRectF(0, 0, meta['width']*meta['x_um'], meta['height']*meta['y_um']))
                kept = QPainterPath()
                kept.addRect(QRectF(x*meta['x_um'], y*meta['y_um'],
                                   max(0, width-1)*meta['x_um'], max(0, height-1)*meta['y_um']))
                shade = self.view.scene().addPath(outside.subtracted(kept),
                    QPen(Qt.NoPen), QColor(12, 20, 26, 125))
                shade.setZValue(8)
                shade.setAcceptedMouseButtons(Qt.NoButton)
                self._paths.append(shade)
            self._path([[[x, y], [x+width-1, y], [x+width-1, y+height-1], [x, y+height-1]]],
                       '#ffe468' if self.review_crop_valid else '#ff7979', 2.)
        if self.tissue_visible.isChecked():
            for scope in self._meta.get('tissue', []):
                selected = self.step == 2 and self.scope.currentText() == scope['name']
                self._path(scope['rings'], '#23bcb8' if selected else '#d8e6dc', 2. if selected else 1.2)
        index = self.scope.currentIndex()
        row = self.checks.currentRow()
        if hasattr(self, 'check_groups'):
            group = 'local' if row >= 5 else 'overall'
            if group != self._check_group:
                self._show_check_group(group, select=False)
            item = self.checks.item(row, 0)
            self.check_name.setText(item.text() if item is not None else '')
        if self.step != 2 or not 0 <= index < len(self._scopes):
            return
        if row >= 4 or row < 1:
            item = self.checks.item(row, 0)
            explanation = item.data(Qt.UserRole) if item is not None else None
            self.key.clear()
            self.key.hide()
            self.check_note.setText(explanation['note'] if explanation else '')
            self.sampling_note.setText(explanation['sampling'] if explanation else '')
            return
        row -= 1
        self.key.show()
        paired_name = 'H&E' if self._paired_label == 'H&E' else 'paired'
        tissue_scope = bool(self._scopes[index]['rings'])
        outline_key = ('Teal: mIF tissue boundary. ' if self.tissue_visible.isChecked() else '') if tissue_scope else 'Teal: mIF image bounds. '
        keys = (outline_key + 'Gold: aligned '+paired_name+' image bounds.',
                'Orange: outside the paired image. Magenta: an invalid mapping.',
                'Blue: compression. Pale: unchanged area. Orange: expansion. Magenta: a fold or invalid mapping.')
        self.key.setText(keys[max(0, row)])
        grid = self._scopes[index]['grid']
        review_area = 'this tissue section' if tissue_scope else 'the mIF image'
        notes = (f'Percentage of sampled locations in {review_area} that fall inside the aligned {paired_name} image.',
                 f'Percentage of sampled locations in {review_area} that fall outside the aligned {paired_name} image or cannot be mapped.',
                 'Ratio of aligned area to original area in the paired image. '
                 'Values below 1 show compression; values above 1 show expansion. '
                 'Zero indicates collapse, and a negative value indicates a fold.')
        note = notes[max(0, row)]
        self.check_note.setText(note)
        meta = self._meta['fixed']
        states = np.asarray(grid['states'])
        self.sampling_note.setText(f"Grid spacing: {grid['spacing']*meta['x_um']:.3g} × "
                                   f"{grid['spacing']*meta['y_um']:.3g} µm · "
                                   f"{np.count_nonzero(states):,} positions in the review region" +
                                   ('\n\nHover over the map to inspect a sampled value.' if row > 0 else ''))
        if not self.global_only:
            self.sampling_note.setText(
                f"The grid samples {np.count_nonzero(states):,} locations in {review_area}, "
                f"spaced {grid['spacing']*meta['x_um']:.3g} µm horizontally and "
                f"{grid['spacing']*meta['y_um']:.3g} µm vertically." +
                ('\n\nHover over the map to inspect a sampled value.' if row > 0 else ''))
        if row <= 0:
            return
        rows, columns = grid['rows'], grid['columns']
        pixels = np.zeros((rows*columns, 4), dtype=np.uint8)
        if row == 1:
            pixels[states == 1] = (232, 137, 43, 125)
            pixels[states == 3] = (224, 55, 181, 145)
        else:
            ratios = np.asarray([np.nan if value is None else value for value in grid['area_ratios']])
            selected = (states == 2) & np.isfinite(ratios) & (ratios > 0)
            amount = np.minimum(1, np.abs(np.log2(np.maximum(ratios[selected], 1e-20)))/2)
            ends = np.where((ratios[selected] < 1)[:, None], (33, 139, 165), (207, 119, 38))
            rgb = np.array((235, 240, 239)) + (ends-np.array((235, 240, 239)))*amount[:, None]
            pixels[selected, :3] = rgb.astype(np.uint8)
            pixels[selected, 3] = (20+130*amount).astype(np.uint8)
            pixels[(states == 3) | ((states == 2) & (ratios <= 0))] = (224, 55, 181, 145)
        pixels = pixels.reshape(rows, columns, 4)
        qimage = QImage(pixels.data, columns, rows, columns*4, QImage.Format_RGBA8888).copy()
        self._grid_item = _AlignmentGridItem(QPixmap.fromImage(qimage), grid,
                                            (meta['x_um'], meta['y_um']), paired_name)
        self.view.scene().addItem(self._grid_item)
        meta = self._meta['fixed']
        self._grid_item.setTransform(QTransform.fromScale(grid['spacing']*meta['x_um'], grid['spacing']*meta['y_um']))
        self._grid_item.setPos(grid['x']*meta['x_um'], grid['y']*meta['y_um'])
        self._grid_item.setZValue(8)
        self._grid_item.setAcceptedMouseButtons(Qt.NoButton)

    def _centre_pixels(self, viewer, metadata):
        point = viewer.mapToScene(viewer.viewport().rect().center())
        return [point.x()/metadata['x_um'], point.y()/metadata['y_um']]

    def _match_view(self):
        point = self._centre_pixels(self.view, self._meta['fixed'])
        # Physical zoom is shared; the native inverse chooses the moving centre.
        self.moving_view._fit = False
        self.moving_view.setTransform(self.view.transform())
        self._operate('points', points=[point])

    def _edit_landmark(self, operation):
        if self._job is not None:
            return
        row = self.landmarks.currentRow()
        if operation in {'replace', 'remove'} and row < 0:
            self.status.setText('Select a landmark pair first.')
            return
        if operation == 'add' and len(self._fixed_points) >= 256:
            self.status.setText('At most 256 landmark pairs can be fitted.')
            return
        if operation == 'remove':
            del self._fixed_points[row], self._moving_points[row]
        else:
            fixed = self._centre_pixels(self.view, self._meta['fixed'])
            moving = self._centre_pixels(self.moving_view, self._meta['moving'])
            for point, name in ((fixed, 'fixed'), (moving, 'moving')):
                meta = self._meta[name]
                if not 0 <= point[0] < meta['width'] or not 0 <= point[1] < meta['height']:
                    self.status.setText('Place both view centres within their image bounds.')
                    return
            if operation == 'replace':
                self._fixed_points[row], self._moving_points[row] = fixed, moving
            else:
                self._fixed_points.append(fixed)
                self._moving_points.append(moving)
        self._local_changed()
        self._update_landmarks()

    def _update_landmarks(self):
        self.landmarks.blockSignals(True)
        self.landmarks.setRowCount(len(self._fixed_points))
        for row, (fixed, moving) in enumerate(zip(self._fixed_points, self._moving_points)):
            for col, text in enumerate((str(row+1), f'x {fixed[0]:.1f}\ny {fixed[1]:.1f}', f'x {moving[0]:.1f}\ny {moving[1]:.1f}')):
                self.landmarks.setItem(row, col, QTableWidgetItem(text))
        self.landmarks.blockSignals(False)
        if hasattr(self, 'landmark_count'):
            count = len(self._fixed_points)
            self.landmark_count.setText(
                f'{count} pairs · Add {6-count} more to reach 6.' if count < 6 else
                f'{count} pairs · Select a row to revisit its locations.')
        self._refresh_landmark_actions()
        self._draw_landmarks()

    def _refresh_landmark_actions(self):
        selected = 0 <= self.landmarks.currentRow() < len(self._fixed_points)
        for operation in ('replace', 'remove'):
            self.landmark_buttons[operation].setEnabled(selected)

    def _focus_landmark(self):
        self._refresh_landmark_actions()
        row = self.landmarks.currentRow()
        if row < 0:
            return
        for name, viewer, points in (('fixed', self.view, self._fixed_points), ('moving', self.moving_view, self._moving_points)):
            meta = self._meta[name]
            viewer.centerOn(points[row][0]*meta['x_um'], points[row][1]*meta['y_um'])
            self._schedule(name)
        self._draw_landmarks()

    def _draw_landmarks(self):
        for viewer, item in self._landmark_items:
            viewer.scene().removeItem(item)
        self._landmark_items.clear()
        if self.step != 1 or not self._manual:
            return
        for name, viewer, points in (('fixed', self.view, self._fixed_points), ('moving', self.moving_view, self._moving_points)):
            meta = self._meta[name]
            for i, point in enumerate(points):
                label = viewer.scene().addSimpleText(str(i+1))
                label.setFlag(label.GraphicsItemFlag.ItemIgnoresTransformations)
                label.setBrush(QColor('#ffe468'))
                if not self.global_only:
                    selected = i == self.landmarks.currentRow()
                    label.setBrush(QColor('#ffe468' if selected else '#e1eeee'))
                    font = label.font()
                    font.setBold(selected)
                    label.setFont(font)
                    if selected:
                        marker = viewer.scene().addEllipse(-7, -7, 14, 14,
                            QPen(QColor('#ffe468'), 2))
                        marker.setFlag(marker.GraphicsItemFlag.ItemIgnoresTransformations)
                        marker.setPos(point[0]*meta['x_um'], point[1]*meta['y_um'])
                        marker.setZValue(19)
                        marker.setAcceptedMouseButtons(Qt.NoButton)
                        self._landmark_items.append((viewer, marker))
                        label.setText('  '+str(i+1))
                label.setPos(point[0]*meta['x_um'], point[1]*meta['y_um'])
                label.setZValue(20)
                self._landmark_items.append((viewer, label))

    def _appearance_dialog(self):
        if self._meta is None or self._closing:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('Image appearance')
        dialog.resize(700, 400)
        layout = QVBoxLayout(dialog)
        choice = QComboBox(dialog)
        choice.addItem('Reference' if self.global_only else 'mIF', 'fixed')
        choice.addItem('Paired image', 'moving')
        layout.addWidget(choice)
        editor = ImageAppearanceEditor(dialog)
        layout.addWidget(editor)
        def select():
            name = choice.currentData()
            metadata = dict(self._meta[name], native_rgb=self._meta[name]['rgb'])
            editor.set_image(metadata, deepcopy(self._appearances[name]))
        def apply(descriptor):
            self._change_appearance(choice.currentData(),descriptor,selection_changed=True)
            dialog.accept()
        choice.currentIndexChanged.connect(select)
        editor.apply_requested.connect(apply)
        editor.requested.connect(apply)
        select()
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.open()

    def _refresh_channel_legends(self):
        for viewer,name in ((self.view,'moving' if self._display_source()=='moving' else 'fixed'),(self.moving_view,'moving')):
            selected=self._review_channels.setdefault(name,list(self._appearances[name]['channels']))
            channels={c['index']:c for c in self._meta[name]['channels']}
            owner = getattr(self.session, 'owner', None)
            member = getattr(self.session, 'member_index', None)
            if self.global_only and owner is not None and member is not None and owner.shared_stain:
                index = owner.registration_channels[owner.reference_member if name == 'fixed' else member]
                if index in channels:
                    channels[index] = dict(channels[index], label=f'{owner.shared_stain} · Channel {index+1}')
            viewer.channel_legend.set_display([channels[i] for i in selected if i in channels],self._appearances[name],native_rgb=self._meta[name]['rgb'])
            viewer.position_controls()

    def _save_rendered_appearances(self):
        for name,requested in tuple(self._pending_appearance_saves.items()):
            display=self._appearances[name]
            if display['channels']!=requested['channels']:continue
            buffers=[record for (pane,_),buffer in self._buffers.items() if pane=='fixed' or self.moving_view.isVisible() for record in buffer['images'] if record['source']==name]
            if not buffers or not all(set(display['channels']).issubset(record['channels']) for record in buffers):continue
            if not all(str(c) in display['limits'] for c in display['channels']):continue
            descriptor=deepcopy(display);uri=self._meta[name]['image_uri']
            try:
                self.session.save_appearance(uri,descriptor)
            except OSError as error:
                self.status.setText('Could not save image appearance: '+str(error))
                return
            del self._pending_appearance_saves[name]
            if name in self._pending_review_channels:
                self._review_channels[name]=self._pending_review_channels.pop(name)
                self._refresh_channel_legends()

    def _change_appearance(self,name,descriptor,*,selection_changed=False):
        if self._meta is None or self._closing:return
        self._appearances[name]=deepcopy(descriptor)
        if selection_changed:self._pending_review_channels[name]=list(descriptor['channels'])
        else:self._pending_review_channels.pop(name,None)
        self._pending_appearance_saves[name]=deepcopy(descriptor)
        if self._repaint_appearance(name):
            self._refresh_channel_legends();self._save_rendered_appearances();return
        self._generation+=1
        self._overview_needed.add('fixed')
        if name=='moving':self._overview_needed.add('moving')
        self._pending_views.add('fixed')
        if self.moving_view.isVisible():self._pending_views.add('moving')
        self.timer.start()

    def _repaint_appearance(self, source):
        """Reuse bounded channel arrays when only colours, limits or selection change."""
        descriptor = self._appearances[source]
        overview = self._buffers.get(('fixed', True))
        if overview is None:
            return False
        item = next((item for item in overview['images'] if item['source'] == source), None)
        if item is None or not set(descriptor['channels']).issubset(item['channels']):
            return False
        values = overview['arrays'][overview['images'].index(item)]
        for channel in descriptor['channels']:
            key = str(channel)
            if key not in descriptor['limits']:
                pixels = values[item['channels'].index(channel)]
                finite = pixels[np.isfinite(pixels)]
                low, high = np.percentile(finite, (1., 99.5)) if finite.size else (0., 1.)
                descriptor['limits'][key] = [float(low), float(high if high > low else low+1)]
        for (name, is_overview), buffer in self._buffers.items():
            if name=='moving' and not self.moving_view.isVisible():continue
            images = self._overview[name] if is_overview else self._detail[name]
            for record, array in zip(buffer['images'], buffer['arrays']):
                if record['source'] != source:
                    continue
                if not set(descriptor['channels']).issubset(record['channels']):
                    return False
                selected = array[[record['channels'].index(channel) for channel in descriptor['channels']]]
                rendered = _channel_image(selected, descriptor['channels'], descriptor)
                for image_source, image_item in images:
                    if image_source == source:
                        image_item.setPixmap(QPixmap.fromImage(rendered))
                        if source == 'fixed' and name == 'fixed' and is_overview:
                            self.view.overview_image_item.setPixmap(image_item.pixmap())
        self._update_locator()
        return True

    def request_close(self):
        """Signal native close immediately; the controller retains this page until drained."""
        if self._closing:
            return
        self._closing = True
        self.timer.stop()
        self._pending_views.clear()
        self._queued_operation = None
        self.panels.setEnabled(False)
        if not self.global_only:
            self.paired_stage_actions.setEnabled(False)
        self.save_button.setEnabled(False)
        self.close_button.setEnabled(False)
        self.status.setText('Closing the image readers…')
        self.close_requested.emit()

    def worker_finished(self, error=None):
        """Finish asynchronously after the native job and every local request have ended."""
        self._worker_done = True
        self._closing = True
        self.timer.stop()
        if error:
            self.status.setText(str(error))
        self._drained()

    def _drained(self, *_):
        if self._worker_done and not self.jobs.has_active_jobs and not self._finished_emitted:
            self._finished_emitted = True
            # Active-job changes precede its success/failure delivery. Defer
            # deletion until those handlers have returned to the Qt event loop.
            QTimer.singleShot(0, self.finished.emit)
