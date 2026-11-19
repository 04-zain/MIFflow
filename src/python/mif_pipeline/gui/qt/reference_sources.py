"""Grouped source rows reuse the form's reviewed-file editors and submission."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QGridLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from .researcher_widgets import ContentSizedButton


class ReferenceSourceOverview(QWidget):
    source_requested = Signal(str, str)

    def __init__(self, renderer, groups):
        super().__init__(renderer)
        self.setObjectName("reference-source-overview")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 8)
        layout.setSpacing(20)
        self.selectors = {}
        self.statuses = {}
        self.add_buttons = {}
        self._renderer = renderer
        label_width = min(190, max(self.fontMetrics().horizontalAdvance(row["label"])
                                  for group in groups for row in group["rows"]))
        # ReviewsTrainingPage moves navigation into its fixed footer after
        # set_form; retain the actual button across that reparenting.
        self._accept = renderer.findChild(QPushButton, f"form-{renderer._form.form_id}-action-accept")
        for group in groups:
            section = QWidget(self)
            grid = QGridLayout(section)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setHorizontalSpacing(16)
            grid.setVerticalSpacing(10)
            grid.setColumnStretch(1, 1)
            grid.setColumnMinimumWidth(0, label_width)
            grid.setColumnMinimumWidth(2, self.fontMetrics().horizontalAdvance("Missing"))
            title = QLabel(group["title"], section)
            title.setProperty("panelHeading", True)
            grid.addWidget(title, 0, 0, 1, 4)
            for index, row in enumerate(group["rows"], 1):
                name = row["control"]
                editor = renderer._editors[name]
                _hide_control(renderer, name)
                # Move the original selector, preserving exact IDs, validation,
                # draft restoration and the filename associations from the owner.
                label = QLabel(row["label"], section)
                label.setTextFormat(Qt.TextFormat.PlainText)
                label.setWordWrap(True)
                label.setBuddy(editor)
                editor.setParent(section)
                editor.setMinimumContentsLength(16)
                editor.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                editor.setItemText(0, "Select reviewed file" if editor.count() > 1 else "No reviewed file")
                editor.show()
                status = QLabel(section)
                status.setObjectName("reference-source-status-" + name)
                button = ContentSizedButton("Add file…", section)
                button.setObjectName("reference-source-add-" + name)
                button.setAccessibleName(f"Add file: {group['title']} — {row['label']}")
                button.setEnabled(row["option"] is not None)
                button.clicked.connect(lambda _checked=False, option=row["option"], title=button.accessibleName(): self._add_file(option, title))
                for col, widget in enumerate((label, editor, status, button)):
                    grid.addWidget(widget, index, col)
                self.selectors[name] = editor
                self.statuses[name] = status
                self.add_buttons[name] = button
                editor.currentIndexChanged.connect(self.refresh)
            layout.addWidget(section)
        self.progress = QLabel(self)
        self.progress.setObjectName("reference-source-progress")
        self.progress.setWordWrap(True)
        layout.addWidget(self.progress)
        self.refresh()

    def _add_file(self, option, title):
        from ..interactions import ControlKind, FormControl
        control = FormControl("locator", title, ControlKind.LOCAL_FILE)
        locator = self._renderer._path_chooser(control)
        if locator:
            self.source_requested.emit(option, str(locator))

    def refresh(self, *_args):
        ready = 0
        for name, selector in self.selectors.items():
            selected = selector.currentData() is not None
            ready += selected
            self.statuses[name].setText("Ready" if selected else "Missing")
            self.statuses[name].setAccessibleName(f"{selector.accessibleName()}: {'Ready' if selected else 'Missing'}")
            selector.setToolTip(selector.currentText() if selected else "Add a file and review its imaging settings.")
        self.progress.setText(f"{ready} of {len(self.selectors)} sources ready")
        accept = self._accept
        if accept is not None:
            accept.setText("Continue to reference regions")
            accept.setEnabled(ready == len(self.selectors))
            accept.setToolTip("" if accept.isEnabled() else "Add and review a file for every required source.")


def _hide_control(renderer, name):
    label = renderer.findChild(QLabel, "form-label-" + name)
    if label is not None:
        label.hide()
    renderer._editors[name].parentWidget().hide()


def install_reference_source_overview(renderer, form):
    if not form.context.get("reference_source_step"):
        return
    _hide_control(renderer, "option_value")
    if "input_id" in renderer._editors:
        # Registration IDs are generated by the page; the researcher selects
        # a file and reviews its scientific metadata.
        _hide_control(renderer, "input_id")
    back = renderer.findChild(QPushButton, "reference-choices-back")
    if back is not None:
        back.hide()
    groups = form.context.get("reference_source_overview")
    if groups:
        panel = ReferenceSourceOverview(renderer, groups)
        renderer._layout.insertWidget(2, panel)
