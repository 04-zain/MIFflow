"""Edit ordered secondary definitions inside the existing review host."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QSize, QTimer, QSignalBlocker
from PySide6.QtGui import QPainter, QColor, QPen
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QFormLayout, QInputDialog, QFrame,
    QHBoxLayout, QLabel,
    QSpinBox, QDoubleSpinBox, QVBoxLayout, QWidget, QMenu, QToolButton, QSizePolicy,
    QScrollArea, QPushButton, QTabBar,
)

from ..interactions import _json_editable_value
from ..secondary_setup import (
    apply_secondary_classifier_route, available_classifier_id, definition_classifier_groups, definition_classifier_route,
    new_secondary_structure_draft, planned_secondary_definitions, secondary_structure_draft,
)
from .criterion_editor import CriterionProfileEditor
from .researcher_widgets import ContentSizedButton
from .selection_option import SelectionOption


from .wrapping_layout import WrappingLayout as _SubtypeFlow

class _SetupNode(QPushButton):
    """A compact page indicator with a full keyboard and pointer target."""
    def __init__(self, title, number, parent):
        super().__init__(parent)
        self.number = number
        self.setFixedSize(24, 32)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCheckable(True)
        self.setToolTip(title)
        self.setAccessibleName(title)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#a9c2bd"), 1))
        if self.number > 0:
            painter.drawLine(0, 16, 7, 16)
        if self.number < 2:
            painter.drawLine(17, 16, 24, 16)
        painter.setPen(QPen(QColor("#006b70"), 1.5))
        painter.setBrush(QColor("#087f80" if self.isChecked() else "#f4f8f6"))
        painter.drawEllipse(7, 11, 10, 10)
        if self.hasFocus() or self.underMouse():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("#006b70"), 1, Qt.PenStyle.DotLine))
            painter.drawRoundedRect(1, 2, 22, 28, 3, 3)


class SecondaryDefinitionEditor(QWidget):
    """Keep unsubmitted definition rows; the owner validates them on acceptance."""

    changed = Signal()
    page_changed = Signal()
    skip_requested = Signal()
    secondary_marker_requested = Signal(str, object, object)

    def __init__(self, context: Mapping[str, object], parent=None):
        super().__init__(parent)
        self._context = context
        self._rows = []
        self._classifier_route = "separate"
        self._saved_groups = {}
        self._selected = -1
        self._loading = False
        self.page = "overview"
        self._selection = None
        self._navigation_issues = {}
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self._fit_section)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.panel = QWidget(self)
        body = QVBoxLayout(self.panel)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(12)
        self.error = QLabel(self.panel)
        self.error.setProperty("validationError", True)
        self.error.setWordWrap(True)
        self.error.hide()
        body.addWidget(self.error)
        self.structure_heading = QLabel(self.panel)
        self.structure_heading.setProperty("panelHeading", True)
        self.structure_heading.setWordWrap(True)
        body.addWidget(self.structure_heading)
        self.setup_tabs = QTabBar(self.panel)
        self.setup_tabs.setObjectName("stage-page-tabs")
        self.setup_tabs.setAccessibleName("Structure setup pages")
        self.setup_tabs.setExpanding(False)
        self.setup_tabs.setDrawBase(False)
        self.setup_tabs.setUsesScrollButtons(True)
        self.setup_tabs.setElideMode(Qt.TextElideMode.ElideNone)
        self.setup_tabs.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self._setup_page_keys = ("detection", "counts", "review")
        for title in ("Detection area", "Count limits", "Review measurements"):
            self.setup_tabs.addTab(title)
        self.setup_tabs.currentChanged.connect(self._navigate_setup)
        body.addWidget(self.setup_tabs)
        self._setup_nodes = {}
        self._setup_graphs = {}
        self._edit_settings_buttons = {}
        self.pages = {}
        for key in ("overview", "preparation", "detection", "counts", "review"):
            page = QWidget(self.panel)
            page.setObjectName("secondary-page-" + key)
            page.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(0, 0, 0, 0)
            page_layout.setSpacing(12)
            self.pages[key] = page
            body.addWidget(page)
        overview = self.pages["overview"].layout()
        self.navigator = QScrollArea(self)
        self.navigator.setObjectName("secondary-definition-navigator")
        self.navigator.setAccessibleName("Structures and their subtypes")
        self.navigator.setFrameShape(QFrame.Shape.NoFrame)
        self.navigator.setWidgetResizable(True)
        self.navigator.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._navigation_body = QWidget(self.navigator)
        self._navigation_layout = QVBoxLayout(self._navigation_body)
        self._navigation_layout.setContentsMargins(0, 0, 0, 0)
        self._navigation_layout.setSpacing(14)
        self._navigation_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._navigation_buttons = {}
        self._navigation_statuses = {}
        self.navigator.setWidget(self._navigation_body)
        overview.addWidget(self.navigator)
        preparation = self.pages["preparation"].layout()
        self.classifier_routes = QWidget(self.pages["preparation"])
        self.classifier_routes.setObjectName("secondary-classifier-routes")
        self.classifier_routes.setStyleSheet("""
            QWidget#secondary-classifier-routes QFrame[selectionOption="true"] {
                background: #ffffff; border: 1px solid #d5ded9; border-radius: 5px;
            }
            QWidget#secondary-classifier-routes QFrame[optionFocused="true"] {
                border: 1px dotted #006b70;
            }
        """)
        route_layout = QVBoxLayout(self.classifier_routes)
        route_layout.setContentsMargins(0, 0, 0, 0)
        route_layout.setSpacing(10)
        self.route_group = QButtonGroup(self)
        self.route_buttons = {}
        for key, title, description in (
            ("separate", "Separate classifiers", "Train a separate classifier to distinguish each structure from other tissue. Each classifier can use its own channels and reference annotations. Regions detected by different classifiers can overlap."),
            ("joint", "Shared classifier", "Train one classifier to distinguish all selected structures from one another and from other tissue. They share the same channels and detection area. Each location receives one class."),
        ):
            option = SelectionOption(title, description, self.classifier_routes, reference_layout=True)
            option.setMinimumHeight(96)
            option.layout().setAlignment(Qt.AlignmentFlag.AlignTop)
            option.layout().setSpacing(8)
            option.button.setObjectName("secondary-route-" + key)
            self.route_group.addButton(option.button)
            self.route_buttons[key] = option.button
            option.button.clicked.connect(lambda _checked=False, route=key: self._choose_classifier_route(route))
            route_layout.addWidget(option)
        preparation.addWidget(self.classifier_routes)
        self.arrangement_error = QLabel(self.pages["preparation"])
        self.arrangement_error.setProperty("validationError", True)
        self.arrangement_error.setWordWrap(True)
        preparation.addWidget(self.arrangement_error)
        self.empty = QLabel("Add a structure, then choose its detection area and review measurements.", self)
        self.empty.setProperty("fieldHelp", True)
        self.empty.setWordWrap(True)
        overview.addWidget(self.empty)
        self.skip = ContentSizedButton("Continue without structures", self)
        self.skip.setObjectName("secondary-skip")
        self.skip.clicked.connect(self._skip_structures)
        overview.addWidget(self.skip, alignment=Qt.AlignmentFlag.AlignLeft)
        self.save_reason = QLabel(self)
        self.save_reason.setProperty("fieldHelp", True)
        self.save_reason.setWordWrap(True)
        overview.addWidget(self.save_reason)

        detection = self.pages["detection"].layout()
        self.area_mode = QComboBox(self)
        self.area_mode.setObjectName("secondary-detection-area")
        self.area_mode.setAccessibleName("Detection area")
        for label, value in (("Choose…", None), ("Whole analysis region", "whole"),
                             ("Selected primary tissue compartments", "compartments")):
            if value != "compartments" or context["eligible_primary_class_ids"]:
                self.area_mode.addItem(label, value)
        self.area_mode.hide()
        self.area_choices = {}
        self._area_group = QButtonGroup(self)
        area_options = QHBoxLayout()
        area_options.setSpacing(8)
        detection.addLayout(area_options)
        for key, title, detail in (
            ("whole", "Whole analysis region", "Detect structures throughout each accepted analysis region."),
            ("compartments", "Selected tissue classes", "Detect structures within the primary tissue classes selected below."),
        ):
            if key == "compartments" and not context["eligible_primary_class_ids"]:
                continue
            choice = SelectionOption(title, parent=self)
            choice.button.setAccessibleDescription(detail)
            choice.button.setObjectName("secondary-area-" + key)
            self._area_group.addButton(choice.button)
            choice.button.clicked.connect(lambda _checked=False, value=key:
                self.area_mode.setCurrentIndex(self.area_mode.findData(value)))
            self.area_choices[key] = choice
            area_options.addWidget(choice, 1)
        self.area_help = QLabel(self)
        self.area_help.setWordWrap(True)
        self.area_help.setProperty("fieldHelp", True)
        detection.addWidget(self.area_help)
        self.parents = QWidget(self)
        self.parents.setObjectName("secondary-parents")
        self.parents.setAccessibleName("Primary tissue compartments")
        self._parent_layout = QVBoxLayout(self.parents)
        self._parent_layout.setContentsMargins(12, 4, 12, 4)
        self._parent_layout.setSpacing(10)
        self._parent_choices = {}
        detection.addWidget(self.parents)
        self.minimum_candidate_area = QDoubleSpinBox(self)
        self.minimum_candidate_area.setObjectName("secondary-minimum-candidate-area")
        self.minimum_candidate_area.setAccessibleName("Minimum region area (µm²)")
        self.minimum_candidate_area.setRange(0, 1e12)
        self.minimum_candidate_area.setDecimals(2)
        self.minimum_candidate_area.setSuffix(" µm²")
        self.minimum_candidate_area.setGroupSeparatorShown(True)
        self.minimum_candidate_area.setMaximumWidth(190)
        minimum_area_row = QFormLayout()
        minimum_area_row.addRow("Minimum region area", self.minimum_candidate_area)
        detection.addLayout(minimum_area_row)
        review = self.pages["review"].layout()
        self.review_target_row = QWidget(self)
        target_layout = QHBoxLayout(self.review_target_row)
        target_layout.setContentsMargins(0, 0, 0, 0)
        target_layout.setSpacing(12)
        self.review_target = QComboBox(self.review_target_row)
        self.review_target.setObjectName("secondary-review-target")
        self.review_target.setAccessibleName("Subtype measurements to edit")
        self.review_target.setMinimumHeight(38)
        self.review_target.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.review_target.setMinimumContentsLength(18)
        self.review_target.setMinimumWidth(0)
        target_label = QLabel("Subtype", self.review_target_row)
        target_label.setBuddy(self.review_target)
        target_layout.addWidget(target_label)
        target_layout.addWidget(self.review_target, 1)
        review.addWidget(self.review_target_row)
        self.review_scope = QLabel(self)
        self.review_scope.setProperty("fieldHelp", True)
        self.review_scope.setWordWrap(True)
        review.addWidget(self.review_scope)
        self.criteria = CriterionProfileEditor(context, self)
        self.criteria.service_requested.connect(self.secondary_marker_requested)
        review.addWidget(self.criteria)
        counts = self.pages["counts"].layout()
        count_help = QLabel("Set how many accepted structures each analysis region should contain.", self)
        count_help.setWordWrap(True)
        count_help.setProperty("fieldHelp", True)
        counts.addWidget(count_help)
        self.options = QWidget(self)
        options = self.count_form = QFormLayout(self.options)
        options.setContentsMargins(0, 4, 0, 0)
        options.setVerticalSpacing(12)
        options.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.minimum = QSpinBox(self)
        self.minimum.setRange(0, 1_000_000)
        self.maximum = QSpinBox(self)
        self.maximum.setRange(0, 1_000_000)
        self.maximum.setSpecialValueText("No limit")
        self.policy = QComboBox(self)
        self.policy.addItem("Choose…", None)
        self.policy.addItem("Select regions manually", "manual_select")
        self.policy.addItem("Select regions randomly", "deterministic_hash_sample")
        self.seed = QSpinBox(self)
        self.seed.setRange(-2_147_483_648, 2_147_483_647)
        for text, field, description, name in (
            ("Minimum per analysis region", self.minimum,
             "If the count is below the minimum, the analysis region is excluded from later results.", "minimum"),
            ("Maximum per analysis region", self.maximum,
             "If the count exceeds the maximum, the selected method determines which regions are retained.", "maximum"),
        ):
            group = QWidget(self.options)
            column = QVBoxLayout(group)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(5)
            column.addWidget(field)
            help_label = QLabel(description, group)
            help_label.setObjectName("secondary-count-" + name + "-help")
            help_label.setProperty("fieldHelp", True)
            help_label.setWordWrap(True)
            column.addWidget(help_label)
            options.addRow(text, group)
        for text, field in (("When above maximum", self.policy), ("Random seed", self.seed)):
            options.addRow(text, field)
        counts.addWidget(self.options)
        layout.addWidget(self.panel)
        self.area_mode.currentIndexChanged.connect(self._area_changed)
        self.minimum_candidate_area.valueChanged.connect(self._edited)
        self.review_target.currentIndexChanged.connect(self._review_target_changed)
        self.criteria.changed.connect(self._edited)
        self.minimum.valueChanged.connect(self._edited)
        self.maximum.valueChanged.connect(self._edited)
        self.policy.currentIndexChanged.connect(self._edited)
        self.seed.valueChanged.connect(self._edited)
        self._refresh_table()
        self._show_page()

    def _skip_structures(self):
        if self._rows:
            return
        self._selection = "none"
        self.changed.emit()
        self.skip_requested.emit()

    def has_structures(self):
        return bool(self._rows)

    def page_title(self):
        """Name the selected setup task without mixing structure and subtype edits."""
        return {"overview": "Choose structures", "preparation": "Prepare classifiers", "detection": "Detection area",
                "counts": "Structure count limits",
                "review": "Subtype measurements" if self.criteria._index else "Review measurements"}[self.page]

    def needs_preparation(self):
        return len(self._rows) > 1

    def _choose_classifier_route(self, route):
        self._classifier_route = route
        self._refresh_preparation()
        self.changed.emit()

    def _refresh_preparation(self):
        groups = definition_classifier_groups(planned_secondary_definitions(
            self._rows, self._classifier_route, self._saved_groups))
        self.route_group.setExclusive(False)
        for route, button in self.route_buttons.items():
            button.setChecked(self._classifier_route == route)
        self.route_group.setExclusive(True)
        reasons = []
        primary_names = {item["class_id"]: item["display_name"]
                         for item in self._context["validation_config"]["primary_classes"] or ()}
        primary_names["AnalysisDomain"] = "Whole analysis region"
        for members in groups.values():
            areas = {tuple(sorted(row["eligible_parent_ids"])) for row in members}
            if len(areas) > 1:
                areas_by_name = [row["display_name"] + " (" + ", ".join(
                    primary_names.get(area, area) for area in row["eligible_parent_ids"]) + ")" for row in members]
                reasons.append("Choose the same detection area for " + " and ".join(areas_by_name)
                               + ", or use separate classifiers.")
        self.arrangement_error.setText("\n".join(reasons))
        self.arrangement_error.setVisible(bool(reasons))

    def save_block_reason(self):
        if self._selection == "none" and not self._rows:
            reason = ""
        elif not self._rows:
            reason = "Add a structure, or continue without structures."
        else:
            issue = next(((index, message) for index, message in self._navigation_issues.items() if message), None)
            reason = (issue[1] if issue[1] == "Choose detection area" else
                      f"{self._rows[issue[0]]['display_name']}: {issue[1]}") if issue else ""
        if not reason and self.page == "preparation":
            reason = self.arrangement_error.text()
        self.save_reason.setText(reason)
        self.save_reason.setVisible(bool(reason) and bool(self._rows) and reason != "Choose detection area")
        return reason

    def _show_page(self):
        for key, widget in self.pages.items():
            widget.setVisible(key == self.page)
        if self.page == "preparation":
            self._refresh_preparation()
        structure_page = self.page in self._setup_page_keys
        self.structure_heading.setVisible(structure_page)
        self.structure_heading.setText(self._rows[self._selected]["display_name"] if structure_page else "")
        self.setup_tabs.setVisible(structure_page and not self.criteria._index)
        if structure_page:
            with QSignalBlocker(self.setup_tabs):
                self.setup_tabs.setCurrentIndex(self._setup_page_keys.index(self.page))
        self._sync_steps()
        self._fit_section()
        self._fit_timer.start(0)
        self.page_changed.emit()

    def _navigate_setup(self, index):
        """Keep backward edits fluid; use the existing validators before advancing."""
        if self._loading or self.page not in self._setup_page_keys or index < 0:
            return
        target = self._setup_page_keys[index]
        current = self._setup_page_keys.index(self.page)
        if index == current:
            return
        validators = (self._validate_area, self._validate_counts)
        for step in range(current, index):
            if not self._checked(validators[step]):
                error = self.error.text()
                self._set_page(self._setup_page_keys[step])
                self.error.setText(error)
                self.error.show()
                return
        self._set_page(target)

    def _sync_steps(self):
        for index, row in enumerate(self._rows):
            for key, title, validate in (("detection", "Detection area", self._validate_area),
                    ("counts", "Count limits", self._validate_counts),
                    ("review", "Review measurements", self._validate_measurements)):
                try:
                    validate(row)
                    valid = True
                except (TypeError, ValueError):
                    valid = False
                button = self._setup_nodes[index][key]
                button.setChecked(valid)
                button.setAccessibleName(title + (", settings valid" if valid else ", needs attention"))
            self._setup_graphs[index].setVisible(bool(self._navigation_issues.get(index)))

    def _set_page(self, page):
        if page == "review" and self.page == "counts":
            self._review_return_page = "counts"
        self.page = page
        self.error.hide()
        self._show_page()
        self.changed.emit()

    def open_selected(self):
        if self._selected < 0:
            return
        if self._checked(self._validate_names):
            self._set_page("detection")

    def back(self):
        if self.page == "review":
            self._set_page(getattr(self, "_review_return_page", "counts"))
        else:
            self._set_page({"counts": "detection"}.get(self.page, "overview"))

    def next_page(self):
        if self.page == "overview" and self.needs_preparation() and not self.save_block_reason():
            self._set_page("preparation")
            self.route_buttons.get(self._classifier_route, self.route_buttons["separate"]).setFocus(Qt.FocusReason.TabFocusReason)
            return
        routes = {"detection": (self._validate_area, "counts"),
                  "counts": (self._validate_counts, "review")}
        if self.page in routes:
            validate, following = routes[self.page]
            if self._checked(validate):
                self._set_page(following)

    def done(self):
        if not self._checked(self._validate_review):
            return
        if not self.criteria._index:
            for page, validate in (("detection", self._validate_area),):
                if not self._checked(validate):
                    self.page = page
                    self._show_page()
                    self.changed.emit()
                    return
        self._set_page("overview")

    def _checked(self, validate):
        try:
            validate(self._rows[self._selected])
        except (TypeError, ValueError) as exc:
            self.error.setText(str(exc))
            self.error.show()
            return False
        self.error.hide()
        return True

    def _validate_names(self, row):
        if not row["display_name"].strip():
            raise ValueError("Enter a structure name.")
        names = [rule["class_id"].strip() for rule in row.get("_profile_content", row["semantic_profile"]).get("classification_rules", ())]
        if any(not name for name in names) or len(set(names)) != len(names):
            raise ValueError("Enter a distinct, nonempty name for each subtype.")

    def _validate_detection(self, row):
        self._validate_area(row)

    def _validate_area(self, row):
        parents = row["eligible_parent_ids"]
        if not parents or ("AnalysisDomain" in parents and parents != ["AnalysisDomain"]):
            raise ValueError("Choose detection area")
        if not set(parents) <= {"AnalysisDomain", *self._context["eligible_primary_class_ids"]}:
            raise ValueError("Choose primary compartments available in this processing group.")

    def _validate_review(self, row):
        self._validate_measurements(row)
        self._validate_counts(row)

    def _validate_measurements(self, row):
        from mif_pipeline.roi_validation.profiles import SemanticProfileV3
        profile = deepcopy(row.get("_profile_content", row["semantic_profile"]))
        profile.update(schema_version="semantic-profile/v4", profile_version="4")
        SemanticProfileV3.from_mapping(profile)

    def _validate_counts(self, row):
        from mif_pipeline.pipeline_core.config import StructureCountBounds
        StructureCountBounds.from_mapping(row["count_bounds"], field="Count limits", secondary=True)

    def _area_changed(self, *_):
        self.area_help.setText({
            "whole": "Detect structures throughout the accepted analysis regions.",
            "compartments": "Select the primary tissue classes in which to detect structures.",
        }.get(self.area_mode.currentData(), "Choose the tissue area in which to detect this structure."))
        self._area_group.setExclusive(False)
        for key, card in self.area_choices.items():
            card.button.setChecked(key == self.area_mode.currentData())
        self._area_group.setExclusive(True)
        if self._loading:
            return
        self.parents.setVisible(self.area_mode.currentData() == "compartments")
        self._edited()

    def _name_edited(self, text):
        if self._loading or self._selected < 0:
            return
        if self.criteria._index:
            self.criteria.set_rule_name(text.strip())
            self._store_profile(self._rows[self._selected])
        else:
            self._rows[self._selected]["display_name"] = text.strip()
        self._edited()

    def _review_target_changed(self, *_):
        if not self._loading and self.review_target.currentData() is not None:
            self._select_target(self._selected, self.review_target.currentData())

    def _add_structure(self):
        name, accepted = QInputDialog.getText(self, "Add structure", "Structure name")
        if accepted:
            try:
                self.add_structure(name)
            except ValueError as exc:
                self.error.setText(str(exc))
                self.error.show()

    def add_structure(self, name):
        self._add_row(new_secondary_structure_draft(name, self._context))

    def add_profile(self, path: Path):
        self._add_row(secondary_structure_draft(path, self._context))

    def _add_row(self, row):
        if any(item["structure_id"] == row["structure_id"] for item in self._rows):
            raise ValueError("This structure is already in the list. Select it to edit its definition.")
        classifier = row["classifier"]
        classifier["classifier_id"] = available_classifier_id(classifier["classifier_id"], definition_classifier_groups(self._rows))
        apply_secondary_classifier_route([row], joint=False)
        self._rows.append(row)
        self._selection = "define"
        self.error.hide()
        self._refresh_table(len(self._rows) - 1)
        self.changed.emit()

    def _add_subtype(self):
        name, accepted = QInputDialog.getText(self, "Add subtype", "Subtype name")
        if accepted:
            try:
                self.add_subtype(name)
            except ValueError as exc:
                self.error.setText(str(exc))
                self.error.show()

    def add_subtype(self, name):
        if not 0 <= self._selected < len(self._rows):
            raise ValueError("Add or select a structure first.")
        self.criteria.add_rule(name)
        self._refresh_table(self._selected, self.criteria._index)
        self.error.hide()
        self.changed.emit()

    def _refresh_table(self, selected=None, subtype=0):
        self._loading = True
        self._navigation_buttons = {}
        self._navigation_statuses = {}
        self._navigation_issues = {}
        self._setup_nodes = {}
        self._setup_graphs = {}
        self._edit_settings_buttons = {}
        while self._navigation_layout.count():
            item = self._navigation_layout.takeAt(0)
            item.widget().hide()
            item.widget().deleteLater()
        for index, row in enumerate(self._rows):
            self._profile(row)
            group = QFrame(self._navigation_body)
            group.setObjectName("secondary-navigation-group")
            group.setStyleSheet("QFrame#secondary-navigation-group { background:#f4f8f6; border:1px solid #ccdcd7; border-radius:8px; }")
            group_layout = QVBoxLayout(group)
            group_layout.setContentsMargins(12, 8, 12, 12)
            group_layout.setSpacing(9)
            title = self._navigation_button(index, 0, row["display_name"], group)
            title.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            title.setMinimumHeight(48)
            title.setStyleSheet("""
                QPushButton { color:#173d47; border:1px solid transparent;
                    background:transparent; border-radius:5px; min-height:46px; max-height:46px; }
                QPushButton:checked { background:#d4eae5; }
                QPushButton:hover { background:#e1eeea; }
                QPushButton:focus { border-color:#006b70; }
            """)
            heading = QHBoxLayout(title)
            heading.setContentsMargins(9, 6, 9, 6)
            identity = QVBoxLayout()
            identity.setSpacing(3)
            name = QLabel(title)
            name.setStyleSheet("font-size:17px; font-weight:600; color:#173d47; background:transparent;")
            name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            name.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            title.name_label = name
            identity.addWidget(name)
            status = QLabel(title)
            status.setStyleSheet("font-size:12px; color:#426068; background:#e6efeb; border-radius:4px; padding:2px 6px;")
            status.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self._navigation_statuses[index] = status
            heading.addLayout(identity, 1)
            heading.addWidget(status, 0, Qt.AlignmentFlag.AlignVCenter)
            title_row = QHBoxLayout()
            title_row.setSpacing(6)
            title_row.addWidget(title, 1)
            title_row.addWidget(self._item_menu(index, 0, group), alignment=Qt.AlignmentFlag.AlignTop)
            group_layout.addLayout(title_row)
            edit = ContentSizedButton("Edit settings", group)
            edit.setObjectName(f"secondary-edit-settings-{index}")
            edit.setProperty("actionRole", "navigation")
            edit.setCursor(Qt.CursorShape.PointingHandCursor)
            edit.setAccessibleName("Edit settings for " + row["display_name"])
            edit.clicked.connect(lambda _checked=False, selected=index: self._open_target(selected, 0))
            self._edit_settings_buttons[index] = edit
            title_row.insertWidget(1, edit)
            graph = QWidget(group)
            graph.setFixedSize(72, 32)
            graph.setAccessibleName("Setup pages for " + row["display_name"])
            nodes = QHBoxLayout(graph)
            nodes.setContentsMargins(0, 0, 0, 0)
            nodes.setSpacing(0)
            self._setup_nodes[index] = {}
            for number, (key, text) in enumerate((("detection", "Detection area"),
                    ("counts", "Count limits"),
                    ("review", "Review measurements"))):
                node = _SetupNode(text, number, graph)
                node.clicked.connect(lambda _checked=False, selected=index, page=key:
                    self._open_structure_page(selected, page))
                nodes.addWidget(node)
                self._setup_nodes[index][key] = node
            self._setup_graphs[index] = graph
            title_row.insertWidget(1, graph)
            divider = QFrame(group)
            divider.setFixedHeight(1)
            divider.setStyleSheet("background:#dce6e2; border:none;")
            group_layout.addWidget(divider)
            rules = row.get("_profile_content", row["semantic_profile"]).get("classification_rules", ())
            choices = QWidget(group)
            flow = _SubtypeFlow(choices)
            label = QLabel("Subtypes", choices)
            label.setProperty("fieldHelp", True)
            flow.addWidget(label)
            for number, rule in enumerate(rules, 1):
                button = self._navigation_button(index, number, rule["class_id"], choices)
                button.setStyleSheet("""
                    QPushButton { background:transparent; border:none; border-radius:3px;
                        padding:7px 14px; color:#173d47; }
                    QPushButton:checked { background:#d4eae5; border-color:#006b70; color:#005d62; }
                    QPushButton:hover { background:#e1eeea; }
                    QPushButton:focus { border:1px solid #006b70; }
                """)
                chip = QFrame(choices)
                chip.setObjectName("secondary-subtype-chip")
                chip.setStyleSheet("QFrame#secondary-subtype-chip { border:1px solid #bdd0cc; border-radius:5px; background:transparent; }")
                chip.navigation_button = button
                chip_layout = QHBoxLayout(chip)
                chip_layout.setContentsMargins(1, 1, 1, 1)
                chip_layout.setSpacing(0)
                chip_layout.addWidget(button, 1)
                chip_layout.addWidget(self._item_menu(index, number, chip))
                flow.addWidget(chip)
            nested = QHBoxLayout()
            nested.addSpacing(18)
            nested.addWidget(choices, 1)
            group_layout.addLayout(nested)
            add_subtype = ContentSizedButton("+ Add subtype", choices)
            add_subtype.setObjectName(f"secondary-add-subtype-{index}")
            add_subtype.setAccessibleName("Add subtype to " + row["display_name"])
            add_subtype.setToolTip("Add subtype to " + row["display_name"])
            add_subtype.clicked.connect(lambda _checked=False, index=index: self._add_subtype_to(index))
            flow.addWidget(add_subtype)
            self._navigation_layout.addWidget(group)
        add = ContentSizedButton("+ Add structure", self._navigation_body)
        add.setObjectName("secondary-add")
        add.setMinimumHeight(48)
        add.setStyleSheet("QPushButton { text-align:left; padding:12px 20px; border:1px dashed #9ebbb5; border-radius:8px; color:#006b70; background:transparent; } QPushButton:hover { background:#e1eeea; } QPushButton:focus { border:2px solid #006b70; }")
        add.clicked.connect(self._add_structure)
        self._navigation_layout.addWidget(add)
        self._selected = -1
        self._loading = False
        if self._rows:
            index = min(max(0, selected or 0), len(self._rows) - 1)
            rules = self._rows[index].get("_profile_content", self._rows[index]["semantic_profile"]).get("classification_rules", ())
            self._select_target(index, min(subtype, len(rules)))
        self.empty.setVisible(not self._rows)
        self.skip.setVisible(not self._rows and bool(self._context.get("remote_execution")))
        self.navigator.setVisible(True)
        for index in range(len(self._rows)):
            self._update_settings_status(index)
        self._fit_section()
        self._fit_timer.start(0)

    def _item_menu(self, index, subtype, parent):
        button = QToolButton(parent)
        button.setText("⋯")
        button.setObjectName(f"secondary-item-menu-{index}-{subtype}")
        button.setAccessibleName("Rename or remove " + ("subtype" if subtype else "structure"))
        button.setToolTip(button.accessibleName())
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setStyleSheet("QToolButton { border:none; background:transparent; padding:3px; font-size:18px; } QToolButton:hover { background:#e1eeea; } QToolButton::menu-indicator { image:none; }")
        menu = QMenu(button)
        menu.addAction("Rename…", lambda: self._rename_target(index, subtype))
        menu.addAction("Remove subtype" if subtype else "Remove structure", lambda: self._remove_target(index, subtype))
        button.setMenu(menu)
        return button

    def _rename_target(self, index, subtype):
        self._select_target(index, subtype)
        self._rename_selected()

    def _remove_target(self, index, subtype):
        self._select_target(index, subtype)
        self._remove()

    def _open_structure_page(self, index, page):
        self._review_return_page = "counts"
        self._select_target(index, 0)
        self._set_page(page)

    def _select_structure(self, index):
        self._select_target(index, 0)
        self._set_page("overview")

    def _open_target(self, index, subtype):
        self._review_return_page = "overview" if subtype else "counts"
        self._select_target(index, subtype)
        if self._checked(self._validate_names):
            self._set_page("review" if subtype else "detection")

    def _rename_selected(self):
        if self._selected < 0:
            return
        row = self._rows[self._selected]
        subtype = self.criteria._index
        name = row["_profile_content"]["classification_rules"][subtype - 1]["class_id"] if subtype else row["display_name"]
        text, accepted = QInputDialog.getText(self, "Rename subtype" if subtype else "Rename structure",
                                            "Subtype name" if subtype else "Structure name", text=name)
        if accepted:
            self._name_edited(text)

    def _add_subtype_to(self, index):
        self._select_target(index, 0)
        self._add_subtype()

    def _update_settings_status(self, index):
        row = self._rows[index]
        try:
            self._validate_names(row)
            self._validate_detection(row)
            self._validate_review(row)
        except (TypeError, ValueError) as error:
            self._navigation_issues[index] = str(error)
            next_action = str(error)
            if next_action != "Choose detection area":
                next_action = "Review definition"
            self._navigation_statuses[index].setText(next_action)
        else:
            self._navigation_issues[index] = ""
            self._navigation_statuses[index].setText("Ready to use")
        self._navigation_statuses[index].setVisible(not self._navigation_issues[index])
        self._navigation_buttons[index, 0].setAccessibleName("Configure " + row["display_name"] + ", " + self._navigation_statuses[index].text())

    def _navigation_button(self, index, subtype, name, parent):
        button = QPushButton(name, parent)
        button.full_name = name
        button.setCheckable(True)
        button.setObjectName(f"secondary-item-{index}-{subtype}")
        button.setToolTip(name)
        button.clicked.connect(lambda _checked=False, index=index, subtype=subtype:
            self._open_target(index, subtype) if subtype else self._select_structure(index))
        self._navigation_buttons[index, subtype] = button
        return button

    def _sync_navigation(self):
        for (index, subtype), button in self._navigation_buttons.items():
            row = self._rows[index]
            rules = row.get("_profile_content", row["semantic_profile"]).get("classification_rules", ())
            if subtype > len(rules):
                continue  # The next rebuild removes a just-deleted subtype button.
            name = rules[subtype - 1]["class_id"] if subtype else row["display_name"]
            button.full_name = name or ("Enter a subtype name" if subtype else "Enter a structure name")
            button.setToolTip(("Review settings for subtype " if subtype else "Detection and review settings for ") + button.full_name)
            menu = button.parentWidget().findChild(QToolButton, f"secondary-item-menu-{index}-{subtype}")
            if menu is not None:
                menu.setAccessibleName("Rename or remove " + button.full_name)
                menu.setToolTip(menu.accessibleName())
            button.setAccessibleName(("Review subtype " + button.full_name + " of " + row["display_name"])
                                     if subtype else "Structure " + button.full_name)
            button.setChecked((index, subtype) == (self._selected, self.criteria._index))
            if not subtype:
                button.setText("")
                button.name_label.setText(button.fontMetrics().elidedText(button.full_name, Qt.TextElideMode.ElideRight,
                                                                         max(1, self.width() - 300)))
                button.setAccessibleName("Configure " + button.full_name + ", " + self._navigation_statuses[index].text())
            else:
                button.parentWidget().parentWidget().layout().invalidate()

    def _store_profile(self, row):
        content = self.criteria.capture_draft()
        if content != row.get("_profile_original") and content.get("schema_version") == "semantic-profile/v3":
            content["schema_version"] = "semantic-profile/v4"
            content["profile_version"] = "4"
        row["_profile_content"] = content
        row["semantic_profile"] = (deepcopy(row["_profile_reference"])
                                   if content == row.get("_profile_original") else content)

    def _profile(self, row):
        profile = row.get("_profile_content", row["semantic_profile"])
        if "schema_version" not in profile:
            from mif_pipeline.orchestration.batch_semantic_profile_import import read_semantic_profile_source
            _data, parsed = read_semantic_profile_source(Path(profile["artifact_path"]))
            row["_profile_reference"] = deepcopy(row["semantic_profile"])
            row["_profile_original"] = parsed.to_mapping()
            profile = parsed.to_mapping()
        row["_profile_content"] = profile
        return profile

    def _select_target(self, current, subtype):
        self._selected = current
        if not 0 <= current < len(self._rows):
            return
        self._loading = True
        row = self._rows[current]
        profile = self._profile(row)
        self.criteria.set_channel_roles(row["classifier"]["channel_roles"])
        self.criteria.set_value(profile, subtype)
        self.review_target.clear()
        for number, rule in enumerate(profile.get("classification_rules", ()), 1):
            self.review_target.addItem(rule["class_id"], number)
        self.review_target.setCurrentIndex(self.review_target.findData(subtype))
        self.review_target_row.setVisible(bool(subtype))
        self.review_target.setVisible(bool(subtype))
        self.review_scope.setText("Additional measurements for assigning this subtype."
                                  if subtype else "Measurements for accepting or rejecting detected regions.")
        choices = {item["class_id"]: item["display_name"] for item in (self._context["validation_config"]["primary_classes"] or ())
                   if item["class_id"] in self._context["eligible_primary_class_ids"]}
        parents = row["eligible_parent_ids"]
        mode = "whole" if parents == ["AnalysisDomain"] else "compartments" if parents and "AnalysisDomain" not in parents else None
        self.area_mode.setCurrentIndex(self.area_mode.findData(mode))
        self.minimum_candidate_area.setValue(row.get("minimum_candidate_area_um2", 0))
        self.parents.setVisible(mode == "compartments")
        while self._parent_layout.count():
            item = self._parent_layout.takeAt(0)
            item.widget().hide()
            item.widget().deleteLater()
        self._parent_choices = {}
        for key in (*choices, *(key for key in row["eligible_parent_ids"] if key not in choices and key != "AnalysisDomain")):
            name = choices.get(key, key + " (unavailable — choose another location)")
            button = QCheckBox(name, self.parents)
            button.setObjectName("secondary-parent-" + key)
            button.setAccessibleName(name)
            button.setToolTip(name)
            button.setCheckable(True)
            button.setChecked(key in parents)
            button.setStyleSheet("QCheckBox { padding:7px 4px; color:#173d47; } QCheckBox:checked { color:#006b70; font-weight:600; }")
            button.toggled.connect(self._edited)
            self._parent_layout.addWidget(button)
            self._parent_choices[key] = button
        bounds = row["count_bounds"]
        self.minimum.setValue(bounds["minimum"])
        self.maximum.setValue(bounds["maximum"] or 0)
        self.policy.setCurrentIndex(max(0, self.policy.findData(bounds["excess_structure_policy"])))
        self.seed.setValue(bounds.get("sampling_seed") or 0)
        self._loading = False
        self._count_visibility()
        self._sync_navigation()
        self._update_settings_status(current)
        self._show_page()
        button = self._navigation_buttons.get((current, subtype))
        if button is not None and self.page == "overview":
            self.navigator.ensureWidgetVisible(button)

    def _fit_section(self, *_):
        self._sync_navigation()
        self._sync_steps()
        width = max(100, self.width() - 20)
        for choice in self.area_choices.values():
            content = choice.layout()
            choice.setMinimumHeight(max(content.sizeHint().height(), content.heightForWidth(max(100, width // 2 - 8))) + 4)
        height = self._navigation_layout.heightForWidth(width)
        self._navigation_body.setMinimumHeight(max(0, height))
        self.navigator.setFixedHeight(min(480, max(60, height)))
        self.criteria.editor._fit_measurements(max(1, self.width()))
        self.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_section()

    def _count_visibility(self, *_):
        self.count_form.setRowVisible(self.policy, self.maximum.value() > 0)
        self.count_form.setRowVisible(self.seed, self.maximum.value() > 0 and self.policy.currentData() == "deterministic_hash_sample")

    def _edited(self, *_):
        if self._loading:
            return
        field = self.sender()
        if 0 <= self._selected < len(self._rows):
            row = self._rows[self._selected]
            if field is self.criteria:
                self._store_profile(row)
            elif field is self.minimum_candidate_area:
                if self.minimum_candidate_area.value():
                    row["minimum_candidate_area_um2"] = self.minimum_candidate_area.value()
                else:
                    row.pop("minimum_candidate_area_um2", None)
            elif field is self.area_mode or field in self._parent_choices.values():
                parents = []
                if self.area_mode.currentData() == "whole":
                    parents = ["AnalysisDomain"]
                elif self.area_mode.currentData() == "compartments":
                    parents = [key for key, button in self._parent_choices.items() if button.isChecked()]
                row["eligible_parent_ids"] = parents
            elif field in (self.minimum, self.maximum, self.policy, self.seed):
                maximum = self.maximum.value() or None
                policy = self.policy.currentData() if maximum is not None else None
                row["count_bounds"] = {"minimum": self.minimum.value(), "maximum": maximum,
                                       "excess_structure_policy": policy,
                                       "sampling_seed": self.seed.value() if policy == "deterministic_hash_sample" else None}
        self._count_visibility()
        if 0 <= self._selected < len(self._rows):
            for number, rule in enumerate(self._rows[self._selected]["_profile_content"].get("classification_rules", ())):
                self.review_target.setItemText(number, rule["class_id"])
        if self._selected >= 0:
            self._update_settings_status(self._selected)
        self._fit_section()
        self.changed.emit()

    def _remove(self):
        if self.criteria._index:
            self.criteria.remove_rule()
            self._refresh_table(self._selected)
            self.changed.emit()
            return
        if 0 <= self._selected < len(self._rows):
            index = self._selected
            del self._rows[index]
            if len(self._rows) < 2:
                self._classifier_route = "separate"
            self._refresh_table(index)
            self.changed.emit()

    def _move(self, offset):
        index = self._selected
        target = index + offset
        if 0 <= index < len(self._rows) and 0 <= target < len(self._rows):
            self._rows[index], self._rows[target] = self._rows[target], self._rows[index]
            self._refresh_table(target, self.criteria._index)
            self.changed.emit()

    def capture_draft(self):
        return deepcopy({"selection": self._selection, "structures": self._rows,
                         "selected": self._selected, "subtype": self.criteria._index, "page": self.page,
                         "classifier_route": self._classifier_route, "saved_groups": self._saved_groups})

    def set_value(self, value):
        value = _json_editable_value(value)
        self._rows = deepcopy(value.get("structures", []))
        self._classifier_route = value.get("classifier_route", definition_classifier_route(self._rows))
        self._saved_groups = value.get("saved_groups", {
            key: [row["structure_id"] for row in members] for key, members in definition_classifier_groups(self._rows).items()
        })
        self._selected = -1
        self.page = "overview"
        self._selection = "define" if self._rows else value.get("selection")
        self.page = value.get("page") if value.get("page") in self.pages and self._rows else "overview"
        if self.page == "preparation" and not self.needs_preparation():
            self.page = "overview"
        self._refresh_table(value.get("selected", 0), value.get("subtype", 0))
        self._show_page()

    def value(self):
        if self.needs_preparation() and self.page != "preparation":
            raise ValueError("Continue to Prepare classifiers before saving these structures.")
        if self._selection == "define" and self._rows:
            self.criteria.value()
        if self.page == "preparation" and self.arrangement_error.text():
            raise ValueError(self.arrangement_error.text())
        rows = planned_secondary_definitions(self._rows, self._classifier_route, self._saved_groups)
        for row in rows:
            if self._selection == "define":
                self._validate_names(row)
                self._validate_detection(row)
                profile = self._profile(row)
                if (profile.get("schema_version") == "semantic-profile/v4"
                        and row.get("_profile_original") == profile and "_profile_reference" in row):
                    row["semantic_profile"] = row["_profile_reference"]
                else:
                    profile.update(schema_version="semantic-profile/v4", profile_version="4")
                    from mif_pipeline.roi_validation.profiles import SemanticProfileV3
                    SemanticProfileV3.from_mapping(profile)
                    row["semantic_profile"] = profile
            row.pop("_channel_role_pairs", None)
            for key in ("_profile_content", "_profile_reference", "_profile_original"):
                row.pop(key, None)
        return {"selection": self._selection, "structures": rows}

