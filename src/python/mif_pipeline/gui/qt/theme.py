"""

Applies the shared desktop colours, fonts and widget styles before opening analysis windows.

We use a light grey canvas, white input and section surfaces, dark text and
teal selected/primary actions. Named widget IDs and display properties select
the appropriate section, status, table and button styles. Focus and disabled
states remain defined separately so keyboard navigation and unavailable
actions can be distinguished.

Font selection follows the operating system, with a general sans-serif
fallback. Page and section heading sizes are derived from the selected base
font. The initial-window helper then constrains a requested size to the
usable display dimensions while preserving a margin for the desktop.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication, QWidget

_CANVAS = "#f3f5f5"
_INK = "#172d35"
_TEAL = "#006b70"
_WHITE = "#ffffff"


_STYLE = """
QMainWindow, QStackedWidget, QScrollArea { background: #f3f5f5; }
QScrollArea { border: none; }
QLabel#workspace-context { color: #172d35; font-weight: 600; }
QLabel#workspace-context-caption { color: #63787d; font-size: 9pt; }
QLabel#global-status { color: #31575a; }
QMenuBar, QToolBar, QStatusBar { background: #f7f9f8; color: #172d35; }
QMenuBar { border-bottom: 1px solid #d8e0de; }
QMenuBar::item { padding: 4px 9px; }
QMenuBar::item:selected, QMenu::item:selected { background: #d5ebe5; color: #172d35; }
QMenu { background: #ffffff; border: 1px solid #bdcccc; padding: 4px; }
QMenu::item { padding: 5px 24px 5px 12px; }
QMenu::item:disabled { color: #8a9a9d; }
QMenu::separator { height: 1px; background: #dce4e2; margin: 4px 7px; }
QToolBar { border-bottom: 1px solid #d8e0de; padding: 6px 8px; spacing: 6px; }
QToolBar::separator { background: #d8e0de; width: 1px; margin: 4px; }
QToolBar QToolButton {
    background: transparent; border: 1px solid transparent; border-radius: 4px;
    padding: 7px 9px; color: #294148;
}
QToolBar QToolButton:hover { background: #e8eeec; border-color: #d0dcd8; }
QToolBar QToolButton:checked { background: #e4ece9; border-color: #cbd9d4; }
QToolBar QToolButton:focus { border: 2px solid #006b70; }
QToolBar QToolButton:disabled { background: transparent; color: #8a9a9d; border-color: transparent; }
QDockWidget { color: #172d35; }
QDockWidget::title { background: #e4e9e7; padding: 5px 8px; }
QTabWidget::pane { border: 1px solid #c5d0cd; background: #ffffff; }
QTabBar::tab { background: #eef2f0; color: #486169; border: 1px solid #d5dfdb; padding: 7px 12px; }
QTabBar::tab:selected { background: #ffffff; border-bottom: 2px solid #006b70; }
QTabBar::tab:hover { background: #e5efeb; color: #172d35; }
QTabBar::tab:disabled { color: #8a9a9d; background: #f0f3f2; }
QTabBar::tab:focus { border: 2px solid #006b70; }
QStatusBar { border-top: 1px solid #ccd6d4; }
QStatusBar::item { border: none; }
QProgressBar#workspace-progress {
    background: #d5ebe5;
    border: 1px solid #82bcb3;
    border-radius: 4px;
}
QProgressBar#workspace-progress::chunk { background: #006b70; }
QWidget#analysis-sidebar { background: #edf1ef; }
QLabel[sidebarHeading="true"] { color: #52696e; font-size: 10pt; font-weight: 600; padding: 2px 10px; }
QLabel#sidebar-sample-count { color: #63787d; font-size: 10pt; padding: 2px 10px; }
QLabel#sidebar-empty-samples { color: #63787d; padding: 4px 10px; }
QFrame#sidebar-divider { color: #d5dfda; background: #d5dfda; border: none; }
QListWidget#project-samples { background: transparent; border: none; outline: none; }
QListWidget#project-samples::item { padding: 9px 9px; border: 1px solid transparent; border-bottom-color: #dfe6e2; border-radius: 4px; }
QListWidget#project-samples::item:hover { background: #e3ebe6; }
QListWidget#project-samples::item:selected { background: #d5ebe5; color: #173642; }
QListWidget#project-samples::item:focus { border: 2px solid #006b70; }
QListWidget#navigation-rail {
    background: transparent;
    color: #294148;
    border: none;
    padding: 2px;
    outline: none;
    font-weight: 500;
}
QListWidget#navigation-rail::item {
    min-height: 24px;
    padding: 7px 10px;
    border: 1px solid transparent;
    border-radius: 4px;
    margin-bottom: 2px;
}
QListWidget#navigation-rail::item:hover { background: #e3ebe6; }
QListWidget#navigation-rail::item:selected {
    background: #dcece6;
    color: #00575c;
    border-left: 3px solid #006b70;
    font-weight: 600;
}
QListWidget#navigation-rail::item:focus { border: 2px solid #006b70; }
QListWidget#navigation-rail::item:disabled { color: #8a9a9d; background: transparent; }
QLabel#first-run-title, QLabel#home-title, QLabel#software-setup-title,
QLabel#computing-title, QLabel#samples-title, QLabel#plan-title,
QLabel#run-title, QLabel#reviews-title, QLabel#results-title {
    color: #172d35;
    font-weight: 600;
    padding: 0 0 3px 0;
}
QLabel#home-title:focus, QLabel#software-setup-title:focus,
QLabel#computing-title:focus, QLabel#samples-title:focus, QLabel#plan-title:focus,
QLabel#run-title:focus, QLabel#reviews-title:focus, QLabel#results-title:focus {
    border-bottom: 2px solid #006b70;
}
QLabel#home-information, QLabel#software-setup-information,
QLabel#computing-information, QLabel#samples-information, QLabel#plan-information,
QLabel#run-information, QLabel#reviews-information, QLabel#results-information {
    color: #486169;
}
QWidget#workbench-action-bar {
    background: #ffffff;
    border-top: 1px solid #dce4e4;
    padding: 0;
}
QGroupBox {
    background: #ffffff;
    color: #172d35;
    border: none;
    border-top: 1px solid #dce4e4;
    border-radius: 0;
    margin-top: 24px;
    padding: 14px;
    font-weight: 500;
}
QGroupBox::title { subcontrol-origin: margin; left: 0; padding: 0 0 7px 0; }
QFrame#setup-step-qupath-controls, QFrame#setup-step-workspace-controls,
QFrame#welcome-panel,
QFrame#image-runtime-setup-controls, QFrame#cpu-setup-controls, QFrame#gpu-setup-controls, QFrame#qupath-profile-setup-controls, QFrame#registration-setup-controls,
QFrame#home-start-card {
    background: #ffffff;
    border: 1px solid #dce4e4;
    border-radius: 6px;
}
QFrame#welcome-panel { border-color: #cfdcd7; }
QFrame#setup-step-qupath, QFrame#setup-step-workspace { background: transparent; border: none; }
QLabel#first-run-title { font-size: 24px; font-weight: 700; }
QLabel#first-run-information { color: #486169; font-size: 14px; }
QPushButton#welcome-about { border: none; color: #006b70; background: transparent; }
QPushButton#welcome-about:hover { text-decoration: underline; }
QFrame#home-open-card { background: transparent; border: none; }
QFrame#home-setup-divider { background: #d5dfda; border: none; max-height: 1px; }
QLabel#prerequisite-summary, QLabel#home-analysis-folder, QLabel[supportingText="true"] { color: #52696e; }
QLabel[panelHeading="true"] { color: #172d35; font-weight: 600; }
QFrame[workbenchSection="true"] { background: #ffffff; border: 1px solid #dce4e4; border-radius: 8px; }
QFrame[workbenchSection="true"] QLabel, QWidget[workbenchField="true"] { background: transparent; }
QFrame[workbenchSection="true"] QLineEdit, QFrame[workbenchSection="true"] QComboBox, QFrame[workbenchSection="true"] QSpinBox { background: #ffffff; }
QLabel[dialogHeading="true"] { color: #172d35; font-size: 20px; font-weight: 600; }
QLabel[fieldCaption="true"] { color: #294148; font-weight: 500; }
QLabel[choiceHelp="true"] { color: #486169; }
QFrame[workbenchMessage="true"] { background: #e4f0ee; border: 1px solid #bfd7d0; border-radius: 6px; }
QLabel#setup-step-number {
    background: #e4f0ee;
    color: #006166;
    border-radius: 7px;
    padding: 5px 9px;
    font-weight: 600;
}
QLabel#setup-brand { color: #006b70; font-weight: 600; }
QLabel#setup-caption { color: #5e7178; }
QLabel[setupReadiness="true"] { color: #5e7178; }
QToolButton#setup-details-toggle {
    color: #31575a;
    background: transparent;
    border: 1px solid transparent;
    border-radius: 4px;
    padding: 6px 0;
    text-align: left;
}
QToolButton#setup-details-toggle:hover { color: #006b70; }
QToolButton#setup-details-toggle:focus { border: 2px solid #006b70; }
QLabel#plan-empty, QLabel#reviews-empty, QLabel#results-empty {
    background: #edf3f1;
    color: #244752;
    border-left: 3px solid #82bcb3;
    padding: 14px 18px;
}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit,
QTableView, QTableWidget, QTreeView {
    background: #ffffff;
    color: #172d35;
    border: 1px solid #bdcccc;
    border-radius: 5px;
    padding: 6px;
    selection-background-color: #c4e8e4;
    selection-color: #102936;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QPlainTextEdit:focus, QTextEdit:focus, QTableView:focus, QTreeView:focus {
    border: 2px solid #006b70;
}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover { border-color: #8eaaa5; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled,
QPlainTextEdit:disabled, QTextEdit:disabled {
    background: #eef2f0; color: #829397; border-color: #d9e2df;
}
QComboBox { padding-right: 36px; }
QComboBox::drop-down {
    subcontrol-origin: padding; subcontrol-position: top right; width: 30px;
    background: transparent; border: none;
}
QComboBox::down-arrow {
    image: url("@CHEVRON_DOWN@"); width: 14px; height: 14px;
}
QComboBox::down-arrow:disabled { image: url("@CHEVRON_DOWN_MUTED@"); }
QComboBox QAbstractItemView {
    background: #ffffff; color: #172d35; border: 1px solid #bdcccc;
    border-radius: 4px; padding: 3px; outline: none;
    selection-background-color: #dcece6; selection-color: #00575c;
}
QComboBox QAbstractItemView::item { min-height: 24px; padding: 4px 8px; }
QSpinBox, QDoubleSpinBox { padding-right: 32px; }
QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {
    subcontrol-origin: padding; subcontrol-position: center right;
    width: 24px; height: 26px; background: transparent;
    border: none; border-radius: 3px;
}
QSpinBox::up-button, QDoubleSpinBox::up-button { right: 3px; }
QSpinBox::down-button, QDoubleSpinBox::down-button { right: 29px; }
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover { background: #e0ede7; }
QSpinBox::up-button:pressed, QDoubleSpinBox::up-button:pressed,
QSpinBox::down-button:pressed, QDoubleSpinBox::down-button:pressed { background: #cfe4db; }
QSpinBox::up-button:disabled, QDoubleSpinBox::up-button:disabled,
QSpinBox::down-button:disabled, QDoubleSpinBox::down-button:disabled { background: transparent; }
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {
    image: url("@INCREMENT@"); width: 14px; height: 14px;
}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {
    image: url("@DECREMENT@"); width: 14px; height: 14px;
}
QSpinBox::up-arrow:disabled, QDoubleSpinBox::up-arrow:disabled,
QSpinBox::up-arrow:off, QDoubleSpinBox::up-arrow:off { image: url("@INCREMENT_MUTED@"); }
QSpinBox::down-arrow:disabled, QDoubleSpinBox::down-arrow:disabled,
QSpinBox::down-arrow:off, QDoubleSpinBox::down-arrow:off { image: url("@DECREMENT_MUTED@"); }
QHeaderView::section {
    background: #f0f4f3;
    color: #294148;
    border: none;
    border-right: 1px solid #d5ded9;
    padding: 8px 10px;
    font-weight: 500;
}
QTableView {
    gridline-color: #e5ebe7;
    alternate-background-color: #f5f9f6;
}
QTableView::item { padding: 4px 6px; }
QTableView::item:selected {
    background: #d5ebe5;
    color: #173642;
}
QToolBox::tab {
    background: #f2f5f1;
    color: #24383c;
    border: 1px solid #d5ded9;
    border-radius: 3px;
    padding: 0 8px;
}
QToolBox::tab:selected {
    background: #e7f2ed;
    border-left: 3px solid #006b70;
    font-weight: 600;
}
QToolBox::tab:hover { background: #e7f2ed; }
QToolButton#reviews-technical-toggle {
    color: #006b70;
    background: #f2f5f1;
    border: 1px solid #d5ded9;
    border-radius: 3px;
    padding: 7px 10px;
    text-align: left;
}
QFrame[modeCard="true"] { background: #ffffff; border: 1px solid #d5ded9; border-radius: 7px; }
QFrame[modeCard="true"][modeSelected="true"] { border: 2px solid #006b70; background: #e4f0ec; }
QFrame[modeCard="true"] QRadioButton { font-weight: 600; padding: 2px 0; }
QFrame[selectionOption="true"] { background: #ffffff; border: 1px solid #d5ded9; border-radius: 4px; }
QFrame[selectionOption="true"]:hover { background: #f7faf9; border-color: #43898b; }
QFrame[selectionOption="true"][optionSelected="true"] { background: #e4f0ec; border-color: #006b70; }
QFrame[selectionOption="true"][optionFocused="true"] { border: 1px dotted #006b70; }
QFrame[selectionOption="true"]:disabled { background: #f0f3f3; border-color: #dce3e3; }
QFrame[selectionOption="true"] QLabel { background: transparent; border: none; padding: 0; }
QFrame[selectionOption="true"] QLabel[selectionTitle="true"] { color: #172d35; font-weight: 600; }
QFrame[selectionOption="true"] QLabel[selectionHelp="true"] { color: #486169; font-weight: 400; }
QFrame[selectionOption="true"] QLabel:disabled { color: #7d8d92; }
QFrame[sourceChoice="true"] { background: #ffffff; border: 1px solid #d5ded9; border-radius: 5px; }
QFrame[sourceChoice="true"] QRadioButton { font-weight: 600; padding: 2px 0; }
QLabel[statusBanner="true"] { background: #e4f0ec; color: #00575c; border-left: 3px solid #006b70; padding: 10px 14px; font-weight: 600; }
QLabel#unmixing-recorded-image-status { padding: 0; }
QPushButton {
    background: #ffffff;
    color: #24444c;
    border: 1px solid #becfce;
    border-radius: 4px;
    padding: 6px 13px;
    min-height: 22px;
    font-weight: 500;
}
QPushButton:hover { background: #edf5f3; border-color: #43898b; }
QPushButton:pressed { background: #d8eae6; }
QPushButton:focus { border: 2px solid #006b70; }
QPushButton:disabled {
    background: #f0f3f3;
    color: #7d8d92;
    border-color: #dce3e3;
}
QToolButton {
    background: #ffffff; color: #294148; border: 1px solid #becfce;
    border-radius: 4px; padding: 4px 7px;
}
QToolButton:hover { background: #edf5f3; border-color: #43898b; }
QToolButton:pressed, QToolButton:checked { background: #dcece6; border-color: #a6c5b9; }
QToolButton:focus { border: 2px solid #006b70; }
QToolButton:disabled { background: #f0f3f3; color: #7d8d92; border-color: #dce3e3; }
QPushButton[actionRole="primary"] {
    background: #006b70;
    color: #ffffff;
    border-color: #006b70;
    font-weight: 500;
}
QPushButton[actionRole="primary"]:hover { background: #00575c; }
QPushButton[actionRole="primary"]:focus { border: 2px solid #d5ebe5; }
QPushButton[actionRole="primary"]:disabled {
    background: #b3cac7;
    color: #314c57;
    border-color: #b3cac7;
}
QPushButton#start-plan, QPushButton#resume-run,
QPushButton#submit-response, QPushButton#first-run-continue,
QPushButton#initialize-authority,
QPushButton#home-new-workspace {
    background: #006b70;
    color: #ffffff;
    border-color: #006b70;
    font-weight: 500;
}
QPushButton#start-plan:hover, QPushButton#resume-run:hover,
QPushButton#submit-response:hover, QPushButton#first-run-continue:hover,
QPushButton#initialize-authority:hover,
QPushButton#home-new-workspace:hover {
    background: #00575c;
}
QPushButton#start-plan:focus, QPushButton#resume-run:focus,
QPushButton#submit-response:focus, QPushButton#first-run-continue:focus,
QPushButton#initialize-authority:focus,
QPushButton#home-new-workspace:focus {
    border: 2px solid #d5ebe5;
}
QPushButton#start-plan:disabled, QPushButton#resume-run:disabled,
QPushButton#submit-response:disabled, QPushButton#first-run-continue:disabled,
QPushButton#initialize-authority:disabled,
QPushButton#home-new-workspace:disabled {
    background: #b3cac7;
    color: #314c57;
    border-color: #b3cac7;
}
QLabel#prototype-classification {
    color: #53676e;
    background: #e9eeed;
    border: 1px solid #d9e2e0;
    border-radius: 5px;
    padding: 4px 9px;
    font-weight: 400;
}
QLabel#plan-warnings { color: #8a5b08; }
"""


# ==============================================================================
#                                     Theme
# ==============================================================================


def apply_desktop_theme(application: QApplication) -> None:
    """

    Sets the application font, palette and named-widget stylesheet used by the desktop.

    We retain the application's current font size when it is at least 11 points.
    The preferred family is Segoe UI on Windows or Ubuntu Sans/Ubuntu elsewhere
    when available; otherwise we select the system's general font. The palette
    supplies the default canvas, input, text and selection colours.

    The stylesheet then applies the shared control and section treatments.
    Page headings use at least 18 points and otherwise seven points above the
    base font; panel headings use one point above the base. Calling this before
    window construction gives new widgets those same inherited settings.

    Args:
        application: Current QApplication receiving the theme.

    Raises:
        TypeError: The supplied value is not a QApplication.
    """

    if not isinstance(application, QApplication):
        raise TypeError("desktop theme requires a QApplication")
    font = QFont(application.font())
    families = set(QFontDatabase.families())
    preferred = ("Segoe UI",) if sys.platform == "win32" else ("Ubuntu Sans", "Ubuntu")
    for family in preferred:
        if family in families:
            font.setFamily(family)
            break
    else:
        font.setFamily(QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).family())
    font.setStyleHint(QFont.StyleHint.SansSerif)
    font.setPointSizeF(max(11.0, font.pointSizeF()))
    application.setFont(font)
    palette = QPalette(application.palette())
    palette.setColor(QPalette.ColorRole.Window, QColor(_CANVAS))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(_INK))
    palette.setColor(QPalette.ColorRole.Base, QColor(_WHITE))
    palette.setColor(QPalette.ColorRole.Text, QColor(_INK))
    palette.setColor(QPalette.ColorRole.Button, QColor(_WHITE))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(_INK))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(_TEAL))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(_WHITE))
    application.setPalette(palette)
    base_points = application.font().pointSizeF()
    title_points = max(18.0, base_points + 7.0) if base_points > 0 else 18.0
    stylesheet = _STYLE
    for name in ("chevron-down", "chevron-up", "chevron-down-muted", "chevron-up-muted",
                 "increment", "decrement", "increment-muted", "decrement-muted"):
        stylesheet = stylesheet.replace(
            "@" + name.upper().replace("-", "_") + "@",
            (Path(__file__).with_name("assets") / f"{name}.svg").as_posix(),
        )
    application.setStyleSheet(
        stylesheet
        + (
            "QLabel#first-run-title, QLabel#home-title, QLabel#software-setup-title, "
            "QLabel#computing-title, QLabel#samples-title, QLabel#plan-title, "
            "QLabel#run-title, QLabel#run-step-title, QLabel#reviews-title, QLabel#results-title "
            f"{{ font-size: {title_points:g}pt; }}"
            f'QLabel[panelHeading="true"] {{ font-size: {base_points + 1:g}pt; }}'
            f'QLabel[stagePageHeading="true"] {{ font-size: {base_points + 4:g}pt; }}'
            f'QLabel[formHeading="true"] {{ font-size: {base_points + 3:g}pt; }}'
        )
    )


def size_initial_window(widget: QWidget, width: int, height: int) -> None:
    """

    Fits a requested opening size within the active screen's available desktop area.

    We use the widget's screen or the primary screen when necessary. Width and
    height are limited independently to the available dimensions minus 80 pixels,
    with a minimum of one pixel. When no screen information is available, we
    apply the requested size directly.

    Args:
        widget: Window or widget whose initial dimensions are being set.
        width: Requested initial width in pixels.
        height: Requested initial height in pixels.
    """

    screen = widget.screen() or QApplication.primaryScreen()
    if screen is None:
        widget.resize(width, height)
        return
    available = screen.availableGeometry()
    widget.resize(
        min(width, max(1, available.width() - 80)),
        min(height, max(1, available.height() - 80)),
    )


__all__ = ["apply_desktop_theme", "size_initial_window"]


# Shared workflow drawing tokens use the same desktop colorway as the widgets.
WORKFLOW_PALETTE = {
    "canvas": "#f3f5f5",
    "surface": "#ffffff",
    "ink": "#172d35",
    "primary": "#006b70",
    "muted": "#486169",
    "border": "#bdcccc",
    "support": "#e4f0ee",
    "lane": "#edf3f1",
    "edge": "#8ca5a3",
    "warning": "#875516",
    "warning_surface": "#fff7ec",
}
_STYLE += """
QLabel#run-state { color: #173642; }
QLabel#run-step-title { font-size: 24px; font-weight: 600; color: #172d35; }
QLabel#run-step-status { color: #006b70; font-weight: 600; }
QLabel#workflow-caption { color: #486169; }
"""

_STYLE += '\nQPushButton[workflowCard="true"] { background: transparent; border: none; }\n'

# Stage surfaces share the same visual hierarchy as setup and review pages.
_STYLE += """
QFrame#stage-sample-context { background: #e4f0ee; border: none; border-radius: 8px; }
QFrame#stage-next-action, QFrame#stage-revision-actions { background: #ffffff; border: 1px solid #dce4e4; border-radius: 6px; }
QWidget#researcher-stage-page QFrame#stage-sample-context { background: transparent; border: none; }
QWidget#stage-current-actions { background: transparent; border: none; }
QLabel#run-step-status[stageState="planned"], QLabel#run-step-status[stageState="waiting"],
QLabel#run-step-status[stageState="partial"] { background: #e9eeed; color: #52696e; }
QLabel#run-step-status[stageState="needs_interaction"] { background: #fff1dc; color: #875516; }
QLabel#run-step-status[stageState="failed"] { background: #f8e8e6; color: #a13332; }
QFrame[stageInfoPanel="true"] { background: #ffffff; border: 1px solid #dce4e4; border-radius: 10px; }
QLabel[statusBadge="true"] { background: #e4f0ee; color: #006b70; border-radius: 7px; padding: 6px 10px; }
QLabel[stageDescription="true"] { color: #486169; padding-bottom: 2px; }
QLabel[choiceHelp="true"] { color: #486169; padding: 2px 0 4px 0; font-weight: 400; }
QLabel[helpBody="true"] { color: #486169; background: #edf3f1; padding: 14px 16px; border-radius: 8px; }
QToolButton[researchDisclosure="true"] { background: transparent; color: #006b70; border: none; padding: 7px 2px; }
QToolButton[researchDisclosure="true"]:focus { border: 2px solid #006b70; }
QTableView[scientificTable="true"] { background: #ffffff; border: 1px solid #dce4e4; border-radius: 9px; padding: 2px; alternate-background-color: #f3f5f5; }
QTableView[scientificTable="true"]::item { border-bottom: 1px solid #edf3f1; padding: 6px 10px; }
QTableWidget#channel-assignment-table::item { padding: 0px; }
QTableWidget#channel-assignment-table QLineEdit, QTableWidget#channel-assignment-table QComboBox { border: 1px solid #dce4e4; border-radius: 6px; padding: 7px 8px; }
QTableWidget#channel-assignment-table QLineEdit:focus, QTableWidget#channel-assignment-table QComboBox:focus { border: 2px solid #006b70; }
QLabel#stage-sample-summary { color: #31575a; }
QTabWidget#compute-tabs QTabBar::tab { padding: 10px 18px; border-bottom: 2px solid #dce4e4; }
QTabWidget#compute-tabs QTabBar::tab:selected { color: #006b70; background: #ffffff; border-bottom: 2px solid #006b70; }
QPushButton#stage-view-samples { background: transparent; border: none; color: #006b70; padding: 4px 7px; }
QPushButton#stage-view-samples:focus { border: 2px solid #006b70; }
QLabel[formHeading="true"], QLabel[stagePageHeading="true"] { color: #173d47; font-weight: 600; }
QFrame[stageFormPage="true"] { background: #ffffff; border: 1px solid #dbe4e5; border-radius: 10px; }
QLabel[stageFieldCaption="true"] { color: #294c56; font-weight: 600; background: transparent; }
QLabel[stagePageDescription="true"], QLabel[fieldHelp="true"] { color: #526971; background: transparent; }
QWidget[stageField="true"] { background: transparent; }
QLabel[validationMessage="true"] { color: #a13332; }
QTabBar#stage-page-tabs::tab {
    background: transparent; color: #526971; border: none;
    border-bottom: 2px solid #dce5e5; padding: 10px 15px; margin-right: 3px;
}
QTabBar#stage-page-tabs::tab:selected { background: #e6f0ef; color: #005d62; border-bottom: 2px solid #006b70; font-weight: 600; }
QTabBar#stage-page-tabs::tab:hover { background: #edf4f3; }
QTabBar#stage-page-tabs::tab:focus { border: 2px solid #006b70; }
QTabBar#stage-page-tabs::scroller { width: 48px; }
QTabBar#stage-page-tabs QToolButton { background: #edf4f3; border: 1px solid #c5d6d4; padding: 0; width: 24px; border-radius: 2px; }
QCheckBox { spacing: 8px; padding: 3px 0; }
QCheckBox::indicator { width: 18px; height: 18px; }
QFrame#run-status-card { background: #ffffff; border: 1px solid #dce4e4; border-radius: 10px; }
QLabel#run-progress-summary { color: #006b70; font-weight: 600; }
QLabel#run-status-caption { color: #526971; font-size: 10pt; font-weight: 600; }
QProgressBar#run-stage-progress { background: #e4eceb; border: none; border-radius: 3px; max-height: 6px; }
QProgressBar#run-stage-progress::chunk { background: #459998; border-radius: 3px; }
QFrame#run-status-card QFrame#stage-sample-context { background: transparent; border: none; }
QTableWidget#measurement-channel-table::item { padding: 0; }
QTableWidget#measurement-channel-table QComboBox { background: #ffffff; padding: 5px 9px; }
QPushButton[actionRole="navigation"] { color: #006b70; background: transparent; border: 1px solid transparent; padding: 6px 2px; }
QPushButton[actionRole="navigation"]:hover { background: #e4f0ee; }
QPushButton[actionRole="navigation"]:focus { border: 2px solid #006b70; }
"""
