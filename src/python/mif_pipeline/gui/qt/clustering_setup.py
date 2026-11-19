"""Organize clustering choices while retaining the ordinary form editors."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QEvent, QTimer, QSignalBlocker
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QScrollArea, QSizePolicy, QTabBar, QToolButton, QVBoxLayout, QWidget,
)

from .clustering_features import ClusteringFeatures, feature_caption
from .researcher_widgets import configure_disclosure
from .phenotyping_scope import FitScopeChoices
from .selection_option import SelectionOption


_METHODS = (
    ("snn_leiden", "Shared-neighbour Leiden", "Connect cells with shared nearest neighbours, then find groups with Leiden."),
    ("flowsom", "FlowSOM", "Learns a grid of marker profiles and assigns each cell to the closest profile. Similar profiles are then combined into the requested number of groups."),
    ("phenograph", "PhenoGraph", "Connects cells with similar measurements. Cells that share more neighbours have stronger connections, which Leiden uses to find groups."),
)


class _ChoiceCards(QWidget):
    """Visible, wrapping choices linked to an existing form combo box."""

    def __init__(self, editor, choices, name, parent=None):
        super().__init__(parent)
        self.editor = editor
        self.cards = {}
        self._arrangement = None
        self.group = QButtonGroup(self)
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        for key, title, detail in choices:
            if editor.findData(key) < 0:
                continue
            card = SelectionOption(title, detail, self, reference_layout=True)
            button = card.button
            button.setObjectName(f"clustering-{name}-{key}")
            button.setAccessibleDescription(detail)
            self.group.addButton(button)
            button.clicked.connect(lambda _checked=False, value=key: editor.setCurrentIndex(editor.findData(value)))
            card.description.setObjectName(f"clustering-{name}-{key}-description")
            self.cards[key] = (card, button)
        editor.currentIndexChanged.connect(self.sync)
        editor.hide()
        self.sync()

    def sync(self, *_args):
        value = self.editor.currentData()
        self.group.setExclusive(False)
        for key, (card, button) in self.cards.items():
            if key == "snn_leiden":
                card.setVisible(value == "snn_leiden")
            button.setChecked(key == value)
        self.group.setExclusive(True)
        self._arrange()
        visible = [button for card, button in self.cards.values() if not card.isHidden()]
        if visible:
            self.setFocusProxy(next((button for button in visible if button.isChecked()), visible[0]))
            self.editor.setFocusProxy(self.focusProxy())

    def _arrange(self):
        shown = [card for key, (card, _button) in self.cards.items()
                 if key != "snn_leiden" or self.editor.currentData() == key]
        arrangement = tuple(shown)
        if arrangement == self._arrangement:
            return
        self._arrangement = arrangement
        for card, _button in self.cards.values():
            self.layout().removeWidget(card)
        for index, card in enumerate(shown):
            self.layout().addWidget(card, index, 0)
        self.layout().setColumnStretch(0, 1)


class ClusteringSetup(QWidget):
    step_requested = Signal(int)

    def __init__(self, form, editors, rows, heading, parent=None):
        super().__init__(parent)
        self.setObjectName("clustering-setup-workbench")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.form, self.editors, self.rows = form, editors, rows
        self.resume = form.context["resume_context"]
        self._choices = []
        self._parameter_boxes = {}
        self._step = 0
        self._fit_pending = False
        self._parameter_layout = None
        self.first_step = 0 if "clustering_mode" in editors else 1
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.tabs = QTabBar(self)
        self.tabs.setObjectName("stage-page-tabs")
        self.tabs.setAccessibleName("Unsupervised phenotyping setup pages")
        self.tabs.setExpanding(False)
        self.tabs.setDrawBase(False)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.setElideMode(Qt.TextElideMode.ElideNone)
        self.tabs.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        for step, title in enumerate(("Scope", "Method", "Measurements", "Settings")):
            self.tabs.addTab(f"{step + 1:02d}  {title}")
        self.tabs.currentChanged.connect(self.step_requested.emit)
        layout.addWidget(self.tabs)
        self.card = card = QFrame(self)
        card.setObjectName("clustering-settings-card")
        card.setProperty("stageFormPage", True)
        # Scope and method are choice pages, like the reference-control list.
        # Keep their heading and rows on the page canvas; the parameter pages
        # retain their existing settings surface.
        card.setStyleSheet('''
            QFrame#clustering-settings-card[clusteringChoicePage="true"] {
                background: transparent; border: none; border-radius: 0;
            }
            QFrame#clustering-settings-card QFrame[selectionOption="true"] {
                background: #ffffff; border: 1px solid #d5ded9; border-radius: 3px;
            }
            QFrame#clustering-settings-card QFrame[selectionOption="true"][optionFocused="true"] {
                border: 1px dotted #006b70;
            }
        ''')
        body = QVBoxLayout(card)
        body.setContentsMargins(24, 22, 24, 24)
        body.setSpacing(12)
        body.setAlignment(Qt.AlignmentFlag.AlignTop)
        heading.setProperty("stagePageHeading", True)
        heading.setWordWrap(True)
        body.addWidget(heading)
        self.summary = QLabel(card)
        self.summary.setObjectName("clustering-setup-context")
        self.summary.setWordWrap(True)
        self.summary.setProperty("fieldHelp", True)
        body.addWidget(self.summary)
        self.pages = []
        for _step in range(4):
            page = QWidget(card)
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(0, 0, 0, 0)
            page_layout.setSpacing(12)
            page_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
            body.addWidget(page)
            self.pages.append(page)
            page.installEventFilter(self)
        # The enclosing card already aligns the page to the top. Let this
        # layout allocate the full wrapped heights of both method cards.
        self.pages[1].layout().setAlignment(Qt.AlignmentFlag(0))
        layout.addWidget(card)

        for name, (label, field) in rows.items():
            field.layout().setContentsMargins(0, 0, 0, 0)
            field.layout().setSpacing(5)
            if name in {"clustering_mode", "method"}:
                label.hide()
                label.setParent(field)
                choices = (FitScopeChoices(editors[name], field, stacked=True, descriptions={
                    "pooled": "Fit one model to the cells in each processing group so that slides with comparable staining share the same groups.",
                    "per_slide": "Identify groups independently on each slide. Group numbers are specific to the slide.",
                }) if name == "clustering_mode"
                           else _ChoiceCards(editors[name], _METHODS, "method", field))
                field.layout().insertWidget(0, choices)
                self._choices.append(choices)
                self.pages[0 if name == "clustering_mode" else 1].layout().addWidget(field)
            elif name == "feature_ids":
                label.hide()
                label.setParent(field)
                self.pages[2].layout().addWidget(field)
        comparison = QWidget(self.pages[1])
        comparison.setObjectName("clustering-method-guidance")
        advice = QVBoxLayout(comparison)
        advice.setContentsMargins(0, 0, 0, 0)
        advice.setSpacing(8)
        comparison_heading = QLabel('Choosing between methods', comparison)
        comparison_heading.setWordWrap(True)
        comparison_heading.setProperty("panelHeading", True)
        advice.addWidget(comparison_heading)
        comparison_text = QLabel(
            "<b>FlowSOM:</b> Requires a chosen number of final groups. Useful when you have an expected number of phenotypes, "
            "or need efficient fitting of a large slide batch.<br><br>"
            "<b>PhenoGraph:</b> Use when you want to explore the groups "
            "supported by the selected measurements.", comparison)
        comparison_text.setObjectName("clustering-method-comparison")
        comparison_text.setWordWrap(True)
        comparison_text.setProperty("fieldHelp", True)
        advice.addWidget(comparison_text)
        self.pages[1].layout().addWidget(comparison)
        self.main_grid = main_grid = QGridLayout()
        main_grid.setHorizontalSpacing(20)
        main_grid.setVerticalSpacing(12)
        main_grid.setColumnStretch(0, 1)
        main_grid.setColumnStretch(1, 1)
        self.pages[3].layout().addLayout(main_grid)
        for name, column in (("k", 0), ("resolution", 1), ("flowsom_clusters", 0)):
            self._add_parameter(main_grid, name, 0, column, span=2 if name == "flowsom_clusters" else 1)
        self.advanced = QToolButton(self.pages[3])
        self.advanced.setObjectName("clustering-setup-advanced")
        self.advanced.setText("Advanced settings")
        self.advanced.setCheckable(True)
        configure_disclosure(self.advanced)
        self.pages[3].layout().addWidget(self.advanced, alignment=Qt.AlignmentFlag.AlignLeft)
        self.advanced_body = QWidget(self.pages[3])
        self.advanced_body.installEventFilter(self)
        advanced_layout = QVBoxLayout(self.advanced_body)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        advanced_layout.setSpacing(16)
        advanced_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.advanced_grid = advanced_grid = QGridLayout()
        advanced_grid.setHorizontalSpacing(20)
        advanced_grid.setVerticalSpacing(12)
        advanced_grid.setColumnStretch(0, 1)
        advanced_grid.setColumnStretch(1, 1)
        for name, row, column in (("flowsom_grid", 0, 0), ("flowsom_iterations", 0, 1), ("seed", 1, 0)):
            self._add_parameter(advanced_grid, name, row, column)
        advanced_layout.addLayout(advanced_grid)
        weights = QLabel("Feature scaling and weights", self.advanced_body)
        weights.setProperty("panelHeading", True)
        advanced_layout.addWidget(weights)
        explanation = QLabel("Larger weights give a measurement more influence on grouping cells.", self.advanced_body)
        explanation.setWordWrap(True)
        explanation.setProperty("fieldHelp", True)
        advanced_layout.addWidget(explanation)
        self.weight_scroll = QScrollArea(self.advanced_body)
        self.weight_scroll.setStyleSheet("QScrollArea { background: transparent; }")
        self.weight_scroll.setWidgetResizable(True)
        self.weight_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.weight_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        weight_body = QWidget(self.weight_scroll)
        weight_grid = QGridLayout(weight_body)
        weight_grid.setContentsMargins(0, 0, 0, 0)
        weight_grid.setHorizontalSpacing(18)
        weight_grid.setVerticalSpacing(6)
        weight_grid.setColumnStretch(0, 1)
        features = self.resume["available_features"]
        for index, feature in enumerate(features):
            name = f"feature_weight_{index}"
            if name not in rows:
                continue
            label, field = rows[name]
            label.setText(feature_caption(feature))
            label.setToolTip(feature["measurement_name"] + "\n" + feature["feature_id"])
            editors[name].setFixedWidth(136)
            editors[name].parentWidget().layout().setAlignment(Qt.AlignmentFlag.AlignLeft)
            weight_grid.addWidget(label, index, 0)
            weight_grid.addWidget(field, index, 1)
        self.weight_scroll.setWidget(weight_body)
        # This is part of the white settings page, not another canvas surface.
        self.weight_scroll.viewport().setAutoFillBackground(False)
        weight_body.setAutoFillBackground(False)
        advanced_layout.addWidget(self.weight_scroll)
        self.pages[3].layout().addWidget(self.advanced_body)
        self.advanced.toggled.connect(lambda _checked: self.refresh(self._step))
        selector = editors.get("feature_ids")
        if isinstance(selector, ClusteringFeatures):
            selector.changed.connect(lambda: self.refresh(self._step))

    def _add_parameter(self, layout, name, row, column, span=1):
        if name not in self.rows:
            return
        label, field = self.rows[name]
        box = QFrame(self)
        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(0, 6, 0, 12)
        box_layout.setSpacing(6)
        box_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        label.setProperty("stageFieldCaption", True)
        # Reuse the form's input, help and validation widgets. Settings are
        # aligned rows; the bordered choice cards belong to the method page.
        heading = QHBoxLayout()
        heading.setContentsMargins(0, 0, 0, 0)
        heading.setSpacing(18)
        heading.addWidget(label, 1)
        input_row = field.layout().takeAt(0).widget()
        self.editors[name].setFixedWidth(136)
        input_row.layout().setAlignment(Qt.AlignmentFlag.AlignRight)
        heading.addWidget(input_row, 0, Qt.AlignmentFlag.AlignTop)
        box_layout.addLayout(heading)
        box_layout.addWidget(field)
        layout.addWidget(box, row, column, 1, span)
        self._parameter_boxes[name] = box
        box.installEventFilter(self)
        field.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() in {QEvent.Type.LayoutRequest, QEvent.Type.Resize}:
            self._queue_fit()
        return super().eventFilter(watched, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._queue_fit()

    def _queue_fit(self):
        if not self._fit_pending:
            self._fit_pending = True
            QTimer.singleShot(0, self, self._fit_content)

    def _fit_content(self):
        """Give each wrapped field its required height before sizing its card."""
        self._fit_pending = False
        columns = 1
        signature = (columns, tuple(name for name, box in self._parameter_boxes.items() if not box.isHidden()))
        if signature != self._parameter_layout:
            self._parameter_layout = signature
            for grid, names in ((self.main_grid, ("k", "resolution", "flowsom_clusters")),
                                (self.advanced_grid, ("flowsom_grid", "flowsom_iterations", "seed"))):
                available = [name for name in names if name in self._parameter_boxes]
                visible = [name for name in available if not self._parameter_boxes[name].isHidden()]
                for name in available:
                    grid.removeWidget(self._parameter_boxes[name])
                for index, name in enumerate(visible):
                    grid.addWidget(self._parameter_boxes[name], index // columns, index % columns)
                grid.setColumnStretch(0, 1)
                grid.setColumnStretch(1, 1 if columns == 2 and len(visible) > 1 else 0)
        widgets = []
        for choice in self._choices:
            if choice.isVisible() and isinstance(choice, _ChoiceCards):
                widgets.extend(card for card, _button in choice.cards.values() if card.isVisible())
                widgets.extend((choice, choice.parentWidget()))
        for name, box in self._parameter_boxes.items():
            if box.isVisible():
                widgets.extend((self.rows[name][1], box))
        if self.advanced_body.isVisible():
            widgets.append(self.advanced_body)
        widgets.extend((self.pages[self._step], self.card, self))
        for widget in widgets:
            height = widget.layout().totalHeightForWidth(max(1, widget.width()))
            if height >= 0 and widget.minimumHeight() != height:
                widget.setMinimumHeight(height)

    def refresh(self, step):
        self._step = step
        choice_page = step in (0, 1)
        if self.card.property("clusteringChoicePage") != choice_page:
            self.card.setProperty("clusteringChoicePage", choice_page)
            self.card.layout().setContentsMargins(
                *(0, 0, 0, 0) if choice_page else (24, 22, 24, 24))
            self.card.layout().setSpacing(8 if choice_page else 12)
            self.layout().setSpacing(10 if choice_page else 14)
            self.card.style().unpolish(self.card)
            self.card.style().polish(self.card)
            self.card.update()
        self._queue_fit()
        for index, page in enumerate(self.pages):
            page.setVisible(index == step)
        with QSignalBlocker(self.tabs):
            self.tabs.setCurrentIndex(step)
            for index in range(self.tabs.count()):
                self.tabs.setTabEnabled(index, index >= self.first_step)
        for choice in self._choices:
            choice.sync()
        scope = self.editors.get("clustering_mode")
        mode = scope.currentData() if isinstance(scope, QComboBox) else self.resume.get("clustering_mode")
        method = self.editors.get("method")
        flowsom = isinstance(method, QComboBox) and method.currentData() == "flowsom"
        for name, box in self._parameter_boxes.items():
            box.setVisible((name not in {"k", "resolution"} if flowsom else name not in {"flowsom_clusters", "flowsom_grid", "flowsom_iterations"}))
        self.advanced_body.setVisible(self.advanced.isChecked())
        selector = self.editors.get("feature_ids")
        selected = selector.value() if isinstance(selector, ClusteringFeatures) else ()
        for index, feature in enumerate(self.resume["available_features"]):
            name = f"feature_weight_{index}"
            if name in self.rows:
                for widget in self.rows[name]:
                    widget.setVisible(feature["feature_id"] in selected)
        self.weight_scroll.widget().layout().invalidate()
        self.weight_scroll.setFixedHeight(min(280, max(44, self.weight_scroll.widget().sizeHint().height() + 8)))
        if step == 0:
            self.summary.setText("")
        elif step == 1:
            self.summary.setText("")
        elif step == 2:
            self.summary.setText("Select the marker measurements used to group cells.")
        else:
            scope_label = "Pooled within each Processing group" if mode == "pooled" else "Per slide"
            method_label = next((title for key, title, _detail in _METHODS
                                 if isinstance(method, QComboBox) and key == method.currentData()), "")
            self.summary.setText(f"{method_label} · {len(selected):,} measurements per cell · {scope_label}")
        self.summary.setVisible(bool(self.summary.text()))



__all__ = ["ClusteringSetup"]
