"""One keyboard-reachable six-route desktop shell.

The widget consumes the current presentation model and emits researcher actions through its
controller. Expensive native or scientific operations use managed jobs, while the Qt thread
retains the selected form and its response identity."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping

from PySide6.QtCore import Qt, QThread, QSize, QSignalBlocker, Signal
from PySide6.QtGui import QAction, QCloseEvent, QColor, QIcon, QKeySequence, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QDockWidget,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ..first_run import FirstRunPrerequisiteState
from ..prototype_notice import PrototypeNotice, trusted_local_prototype_notice
from .about import AboutDialog
from .home import HomePage
from .theme import size_initial_window
from .workbench import PageHeading

_ROUTES = (
    ("home", "Home"),
    ("computing", "Computing"),
    ("samples", "Samples"),
    ("plan", "Plan"),
    ("run", "Run"),
    ("reviews", "Current review"),
    ("results", "Results"),
)
_NAVIGATION_ROUTES = tuple(item for item in _ROUTES if item[0] != "reviews")
_INJECTED_ROUTES = frozenset(
    route_id for route_id, _label in _ROUTES if route_id not in {"home", "computing"}
)


def _shell_icon(name: str) -> QIcon:
    """Draw the small, consistent line icons used only by application navigation."""
    paths = {
        "home": (((2, 7), (8, 2), (14, 7)), ((4, 6), (4, 14), (12, 14), (12, 6)), ((7, 14), (7, 10), (9, 10), (9, 14))),
        "computing": (((2, 2), (14, 2), (14, 10), (2, 10), (2, 2)), ((8, 10), (8, 14)), ((5, 14), (11, 14))),
        "samples": (((3, 11), (2, 11), (2, 2), (11, 2), (11, 3)), ((5, 5), (14, 5), (14, 14), (5, 14), (5, 5))),
        "plan": (((6, 2), (10, 2), (10, 6), (6, 6), (6, 2)), ((8, 6), (8, 8), (3, 8), (3, 10)), ((8, 8), (13, 8), (13, 10)), ((1, 10), (5, 10), (5, 14), (1, 14), (1, 10)), ((11, 10), (15, 10), (15, 14), (11, 14), (11, 10))),
        "run": (((4, 2), (13, 8), (4, 14), (4, 2)),),
        "results": (((2, 2), (2, 14), (14, 14)), ((6, 11), (6, 8)), ((10, 11), (10, 4)), ((14, 11), (14, 6))),
        "sidebar": (((2, 3), (14, 3), (14, 13), (2, 13), (2, 3)), ((6, 3), (6, 13))),
        "new": (((8, 3), (8, 13)), ((3, 8), (13, 8))),
        "open": (((2, 13), (2, 3), (6, 3), (8, 5), (14, 5)), ((2, 13), (5, 7), (15, 7), (12, 13), (2, 13))),
    }
    icon = QIcon()
    for mode, colour in ((QIcon.Mode.Normal, "#486169"), (QIcon.Mode.Selected, "#006b70"), (QIcon.Mode.Disabled, "#9ba9ad")):
        for scale in (1, 2):
            pixmap = QPixmap(16 * scale, 16 * scale)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.scale(scale, scale)
            pen = QPen(QColor(colour), 1.35)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            for points in paths[name]:
                path = QPainterPath()
                path.moveTo(*points[0])
                for point in points[1:]:
                    path.lineTo(*point)
                painter.drawPath(path)
            painter.end()
            icon.addPixmap(pixmap, mode)
    return icon


# ==============================================================================
#                                     Shell
# ==============================================================================


def _validated_route_pages(
    route_pages: Mapping[str, QWidget] | None,
    home_page: QWidget | None,
) -> dict[str, QWidget] | None:
    if route_pages is None:
        return None
    if not isinstance(route_pages, Mapping):
        raise TypeError("route pages must be a mapping")
    if set(route_pages) not in (_INJECTED_ROUTES, _INJECTED_ROUTES | {"computing"}):
        raise ValueError("route pages must name exactly the five non-home routes")
    pages = dict(route_pages)
    if not all(isinstance(page, QWidget) for page in pages.values()):
        raise TypeError("each route page must be a QWidget")
    if len({id(page) for page in pages.values()}) != len(pages):
        raise ValueError("each route must use a distinct page widget")
    if home_page is not None and any(page is home_page for page in pages.values()):
        raise ValueError("home and route pages must be distinct widgets")
    return pages


class WorkspaceShell(QMainWindow):
    """Own navigation and close requests without scientific or close inference."""

    close_requested = Signal()
    route_changed = Signal(str)
    sample_selected = Signal(str)

    def __init__(
        self,
        *,
        prerequisite_state: FirstRunPrerequisiteState | None = None,
        admitted: bool = True,
        home_page: HomePage | None = None,
        route_pages: Mapping[str, QWidget] | None = None,
        notice: PrototypeNotice | None = None,
        parent: QWidget | None = None,
    ) -> None:
        route_pages = _validated_route_pages(route_pages, home_page)
        super().__init__(parent)
        self.notice = notice or trusted_local_prototype_notice()
        self._close_allowed = False
        self._admitted = admitted
        self._welcome_active = False
        self._welcome_page: QWidget | None = None
        self._about_dialog: AboutDialog | None = None
        self._route_pages_installed = route_pages is not None
        self.setWindowTitle("mIF Pipeline")

        self._dock = QDockWidget("Sidebar", self)
        self._dock.setObjectName("analysis-dock")
        self._dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self._dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable)
        dock_title = QWidget(self._dock)
        dock_title.setFixedHeight(0)
        self._dock.setTitleBarWidget(dock_title)
        sidebar = QWidget(self._dock)
        sidebar.setObjectName("analysis-sidebar")
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(8, 16, 8, 10)
        sidebar_layout.setSpacing(8)
        workflow_heading = QLabel("Workflow", sidebar)
        workflow_heading.setProperty("sidebarHeading", True)
        sidebar_layout.addWidget(workflow_heading)
        self._project_samples = QListWidget(sidebar)
        self._project_samples.setObjectName("project-samples")
        self._project_samples.setAccessibleName("Project samples")
        self._project_samples.setWordWrap(True)
        # Qt changes the current item when this list gains focus. Navigate only
        # on an explicit click or keyboard activation, never on focus transfer.
        self._project_samples.itemClicked.connect(
            lambda item: self._select_project_sample(item, None)
        )
        self._project_samples.itemActivated.connect(
            lambda item: self._select_project_sample(item, None)
        )
        self._global_status = QLabel("", self)
        self._global_status.setObjectName("global-status")
        self._global_status.setAccessibleName("Workspace status")
        self.statusBar().setObjectName("workspace-status-bar")
        self.statusBar().addWidget(self._global_status, 1)
        self._activity = QLabel("", self)
        self._activity.setObjectName("workspace-activity")
        self._activity.setAccessibleName("Current activity")
        self._progress = QProgressBar(self)
        self._progress.setObjectName("workspace-progress")
        self._progress.setAccessibleName("Work in progress")
        self._progress.setRange(0, 0)
        self._progress.setTextVisible(False)
        self._progress.setMinimumWidth(160)
        self._progress.setMinimumHeight(self._progress.fontMetrics().lineSpacing() + 12)
        self._progress.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.statusBar().addPermanentWidget(self._activity)
        self.statusBar().addPermanentWidget(self._progress, 1)
        self._activity.hide()
        self._progress.hide()
        self._navigation = QListWidget(sidebar)
        self._navigation.setObjectName("navigation-rail")
        self._navigation.setAccessibleName("Workspace navigation")
        self._navigation.setAccessibleDescription(
            "Use arrow keys to choose a page, or Alt plus 1 through 6 to open one directly."
        )
        route_text_width = max(
            self._navigation.fontMetrics().horizontalAdvance(label) for _route_id, label in _NAVIGATION_ROUTES
        )
        self._navigation.setMinimumWidth(route_text_width + 32)
        self._navigation.setIconSize(QSize(16, 16))
        self._navigation.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._navigation.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sidebar_layout.addWidget(self._navigation)
        divider = QFrame(sidebar)
        divider.setObjectName("sidebar-divider")
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setFrameShadow(QFrame.Shadow.Plain)
        divider.setFixedHeight(1)
        sidebar_layout.addWidget(divider)
        samples_heading = QHBoxLayout()
        samples_label = QLabel("Samples", sidebar)
        samples_label.setProperty("sidebarHeading", True)
        samples_heading.addWidget(samples_label)
        samples_heading.addStretch(1)
        self._sample_count = QLabel("", sidebar)
        self._sample_count.setObjectName("sidebar-sample-count")
        samples_heading.addWidget(self._sample_count)
        sidebar_layout.addLayout(samples_heading)
        sidebar_layout.addWidget(self._project_samples, 1)
        self._empty_samples = QLabel("Open an analysis to view its samples.", sidebar)
        self._empty_samples.setObjectName("sidebar-empty-samples")
        self._empty_samples.setWordWrap(True)
        self._empty_samples.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        sidebar_layout.addWidget(self._empty_samples, 1)
        self._project_samples.hide()
        self._dock.setWidget(sidebar)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self._dock)
        self._stack = QStackedWidget(self)
        self._stack.setObjectName("route-stack")
        self._route_widgets: dict[str, QWidget] = {}

        home = home_page or HomePage(
            draft=None,
            prerequisite_state=prerequisite_state,
            notice=self.notice,
        )
        for route_id, label in _NAVIGATION_ROUTES:
            item = QListWidgetItem(_shell_icon(route_id), label)
            item.setToolTip(f"{label} · Alt+{self._navigation.count() + 1}")
            self._navigation.addItem(item)
        self._navigation.ensurePolished()
        self._navigation.setFixedHeight(
            sum(self._navigation.sizeHintForRow(row) for row in range(self._navigation.count())) + 8
        )
        for route_id, label in _ROUTES:
            if route_id == "home":
                page = home
            elif route_pages is not None and route_id in route_pages:
                page = route_pages[route_id]
            else:
                page = self._empty_route_page(route_id, label)
            self._route_widgets[route_id] = page
            self._stack.addWidget(page)
        self._navigation.currentRowChanged.connect(self._select_route)
        self._navigation.itemClicked.connect(lambda item: self._select_route(self._navigation.row(item)))
        self._navigation.itemActivated.connect(lambda item: self._select_route(self._navigation.row(item)))
        self._navigation.setCurrentRow(0)
        self._entry_stack = QStackedWidget(self)
        self._entry_stack.addWidget(self._stack)
        self.setCentralWidget(self._entry_stack)
        self.resizeDocks([self._dock], [max(210, route_text_width + 40)], Qt.Orientation.Horizontal)
        size_initial_window(self, 1120, 780)

        file_menu = self.menuBar().addMenu("&File")
        new = QAction(_shell_icon("new"), "New analysis…", self)
        new.setObjectName("new-analysis")
        new.setShortcut(QKeySequence.StandardKey.New)
        new.triggered.connect(self._new_analysis)
        file_menu.addAction(new)
        open_workspace = QAction(
            _shell_icon("open"),
            "Open analysis…",
            self,
        )
        open_workspace.setObjectName("open-workspace")
        open_workspace.setShortcut(QKeySequence.StandardKey.Open)
        open_workspace.triggered.connect(self._open_workspace)
        file_menu.addAction(open_workspace)
        file_menu.addSeparator()
        close = file_menu.addAction("Close")
        close.triggered.connect(self.close)
        view_menu = self.menuBar().addMenu("&View")
        view_menu.addAction(self._dock.toggleViewAction())
        view_menu.addSeparator()
        for number, (route_id, label) in enumerate(_NAVIGATION_ROUTES, 1):
            action = QAction(label, self)
            action.setShortcut(QKeySequence(f"Alt+{number}"))
            action.triggered.connect(
                lambda _checked=False, selected=route_id: self.show_route(selected)
            )
            view_menu.addAction(action)
            self.addAction(action)
        about = QAction("About", self)
        about.setShortcut(QKeySequence("F1"))
        about.triggered.connect(self._show_about)
        self.addAction(about)
        self._about_action = about
        self.menuBar().addMenu("&Help").addAction(about)
        toolbar = QToolBar("Workspace", self)
        toolbar.setObjectName("workspace-toolbar")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(18, 18))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        sidebar_action = self._dock.toggleViewAction()
        sidebar_action.setIcon(_shell_icon("sidebar"))
        sidebar_action.setToolTip("Show or hide the sidebar")
        toolbar.addAction(sidebar_action)
        toolbar.widgetForAction(sidebar_action).setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        toolbar.addSeparator()
        context = QWidget(toolbar)
        context.setObjectName("workspace-header-context")
        context.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        context_layout = QVBoxLayout(context)
        context_layout.setContentsMargins(10, 0, 12, 0)
        context_layout.setSpacing(1)
        context_caption = QLabel("Current analysis", context)
        context_caption.setObjectName("workspace-context-caption")
        context_layout.addWidget(context_caption)
        self._workspace_context = QLabel("No analysis open", context)
        self._workspace_context.setObjectName("workspace-context")
        self._workspace_context.setAccessibleName("Current analysis")
        self._workspace_context.setTextFormat(Qt.TextFormat.PlainText)
        self._workspace_context.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        context_layout.addWidget(self._workspace_context)
        toolbar.addWidget(context)
        toolbar.addAction(new)
        toolbar.addAction(open_workspace)
        self.addToolBar(toolbar)
        self.set_admitted(admitted)

    def _empty_route_page(self, route_id: str, title: str) -> QWidget:
        """Explain an unavailable analysis page without presenting empty controls."""
        descriptions = {
            "computing": "Choose where an analysis runs and check the available resources.",
            "samples": "Add images, then assign subjects and processing groups.",
            "plan": "Check the analysis plan before starting image processing.",
            "run": "Follow processing progress and open each step for review.",
            "reviews": "Inspect and accept the current analysis step.",
            "results": "Explore saved outputs and export completed analysis results.",
        }
        page = QWidget(self._stack)
        page.setObjectName(f"route-{route_id}")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(18)
        layout.addWidget(PageHeading(route_id, title, descriptions[route_id], page))
        message = QLabel("Create or open an analysis to get started.", page)
        message.setObjectName("route-empty-message")
        message.setWordWrap(True)
        layout.addWidget(message)
        actions = QHBoxLayout()
        new = QPushButton("New analysis", page)
        new.setProperty("actionRole", "primary")
        new.clicked.connect(self._new_analysis)
        actions.addWidget(new)
        open_saved = QPushButton("Open analysis…", page)
        open_saved.clicked.connect(self._open_workspace)
        actions.addWidget(open_saved)
        actions.addStretch(1)
        layout.addLayout(actions)
        layout.addStretch(1)
        return page

    def install_welcome(self, page: QWidget) -> None:
        """Install the computer-preparation view once, inside this window."""
        if self._welcome_page is not None:
            raise RuntimeError("welcome is already installed")
        self._welcome_page = page
        self._entry_stack.addWidget(page)

    def show_welcome(self, visible: bool) -> None:
        if visible and self._welcome_page is None:
            raise RuntimeError("welcome is not installed")
        changed = visible != self._welcome_active
        self._welcome_active = visible
        self._entry_stack.setCurrentWidget(self._welcome_page if visible else self._stack)
        self._dock.setVisible(not visible)
        self.menuBar().setVisible(not visible)
        self.statusBar().setVisible(not visible)
        self.findChild(QToolBar, "workspace-toolbar").setVisible(not visible)
        if changed:
            size_initial_window(self, 610 if visible else 1120, 470 if visible else 780)

    def set_admitted(self, admitted: bool) -> None:
        """Render composition-owned admission without inspecting prerequisites."""
        self._admitted = admitted
        self._route_widgets["home"].set_admitted(admitted)
        for name in ("new-analysis", "open-workspace"):
            self.findChild(QAction, name).setEnabled(admitted)
        self._project_samples.setEnabled(admitted)
        for index in range(1, self._navigation.count()):
            item = self._navigation.item(index)
            flags = item.flags()
            item.setFlags(
                flags | Qt.ItemFlag.ItemIsEnabled
                if admitted
                else flags & ~Qt.ItemFlag.ItemIsEnabled
            )

    def _new_analysis(self) -> None:
        if not self._admitted:
            return
        self.show_route("home")
        self._route_widgets["home"].focus_new_analysis()

    def _open_workspace(self) -> None:
        if not self._admitted:
            return
        self._route_widgets["home"].choose_workspace()

    def set_project_samples(self, samples: tuple[tuple[str, str], ...]) -> None:
        """Render supplied sample identities without selecting or opening one."""

        previous = self._project_samples.currentItem()
        selected_id = previous.data(Qt.ItemDataRole.UserRole) if previous is not None else None
        with QSignalBlocker(self._project_samples):
            self._project_samples.clear()
            for sample_id, filename in samples:
                item = QListWidgetItem(f"{sample_id}\n{filename}")
                item.setData(Qt.ItemDataRole.UserRole, sample_id)
                item.setToolTip(f"{sample_id}\n{filename}")
                self._project_samples.addItem(item)
                if sample_id == selected_id:
                    self._project_samples.setCurrentItem(item)
        self._project_samples.setVisible(bool(samples))
        self._empty_samples.setVisible(not samples)
        self._sample_count.setText(str(len(samples)) if samples else "")

    def _select_project_sample(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        if current is not None:
            self.sample_selected.emit(current.data(Qt.ItemDataRole.UserRole))

    def install_route_pages(self, route_pages: Mapping[str, QWidget]) -> None:
        """Replace the five placeholders once after a real draft supplies pages."""

        if QThread.currentThread() != self.thread():
            raise RuntimeError("route pages must be installed on the Qt thread")
        if self._route_pages_installed:
            raise RuntimeError("route pages are already installed")
        pages = _validated_route_pages(route_pages, self._route_widgets["home"])
        if pages is None:
            raise TypeError("route pages must be a mapping")
        if any(page in self._route_widgets.values() for page in pages.values()):
            raise ValueError("route pages cannot reuse existing shell widgets")
        current_route = self.current_route_id
        for index, (route_id, _label) in enumerate(_ROUTES[1:], 1):
            if route_id not in pages:
                continue
            previous = self._route_widgets[route_id]
            self._stack.removeWidget(previous)
            previous.setParent(None)
            previous.deleteLater()
            page = pages[route_id]
            self._stack.insertWidget(index, page)
            self._route_widgets[route_id] = page
        self._stack.setCurrentIndex(self.route_ids.index(current_route))
        self._route_pages_installed = True

    def clear_workspace_pages(self) -> None:
        """Remove the closed analysis pages while retaining Home and the window."""
        self.show_route("home")
        for index, (route_id, title) in enumerate(_ROUTES[1:], 1):
            previous = self._route_widgets[route_id]
            self._stack.removeWidget(previous)
            previous.deleteLater()
            page = self._empty_route_page(route_id, title)
            self._stack.insertWidget(index, page)
            self._route_widgets[route_id] = page
        self._route_pages_installed = False
        self.set_project_samples(())
        self.set_workspace_context(None, None)

    def set_workspace_context(self, workspace_name: str | None, status_text: str | None) -> None:
        """Render caller-supplied workspace text without deriving state."""

        self._workspace_context.setText(workspace_name or "No analysis open")
        self._workspace_context.setToolTip(workspace_name or "No analysis open")
        self._empty_samples.setText(
            "Add images on the Samples page." if workspace_name else "Open an analysis to view its samples."
        )
        self._global_status.setText(status_text or "")

    def set_activity(self, message: str | None) -> None:
        """Show indeterminate worker activity independently of workspace state."""

        self._activity.setText(message or "")
        visible = bool(message)
        self._activity.setVisible(visible)
        self._progress.setVisible(visible)

    @property
    def is_admitted(self) -> bool:
        """Whether the composition currently permits analysis opening."""
        return self._admitted

    @property
    def route_ids(self) -> tuple[str, ...]:
        return tuple(route_id for route_id, _label in _ROUTES)

    @property
    def current_route_id(self) -> str:
        return self.route_ids[self._stack.currentIndex()]

    def show_route(self, route_id: str) -> None:
        """Navigate to one installed route using its stable public ID."""
        if route_id not in self.route_ids:
            raise ValueError("unknown workspace route")
        if self._welcome_active:
            return
        if not self._admitted and route_id != "home":
            return
        visible_route = "run" if route_id == "reviews" else route_id
        navigation_ids = tuple(key for key, _label in _NAVIGATION_ROUTES)
        with QSignalBlocker(self._navigation):
            self._navigation.setCurrentRow(navigation_ids.index(visible_route))
        changed = self.current_route_id != route_id
        self._stack.setCurrentWidget(self._route_widgets[route_id])
        if changed:
            self.route_changed.emit(route_id)
        heading = self._route_widgets[route_id].findChild(QLabel, f"{route_id}-title")
        if heading is not None:
            heading.setFocus()

    def _select_route(self, row: int) -> None:
        if not 0 <= row < len(_NAVIGATION_ROUTES):
            return
        if not self._admitted and row != 0:
            self._navigation.setCurrentRow(0)
            return
        self.show_route(_NAVIGATION_ROUTES[row][0])

    def _show_about(self) -> None:
        dialog = AboutDialog(self, notice=self.notice)
        self._about_dialog = dialog
        dialog.show()

    def allow_close(self) -> None:
        """Accept only a subsequent close explicitly approved by a controller."""

        self._close_allowed = True

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._close_allowed:
            event.accept()
            return
        event.ignore()
        self.close_requested.emit()


__all__ = ["WorkspaceShell"]
