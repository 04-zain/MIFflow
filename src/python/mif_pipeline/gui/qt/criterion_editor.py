"""Visual editing of the existing semantic-profile criterion tree."""
from __future__ import annotations

from copy import deepcopy
import math
from uuid import uuid4

from PySide6.QtCore import Qt, Signal, QModelIndex, QSize, QSignalBlocker
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QFrame, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
    QHeaderView, QPushButton, QToolButton, QMenu, QLayout, QBoxLayout, QSizePolicy,
)
from ..feature_channels import channel_choice_labels

from mif_pipeline.gui.structure_criteria import QUANTITIES, SELECTORS, SELECTOR_LABELS, COMPARISONS, measurement_column_label


_MEASUREMENT_TYPES = (
    ("region", "Region size and shape", ("candidate_area", "geometry_component_count")),
    ("counts", "Cell counts and density", ("segmented_cell_count", "segmented_nucleus_count", "cell_density", "nucleus_density", "phenotype_count", "phenotype_density")),
    ("phenotype", "Phenotype fractions", ("phenotype_fraction",)),
    ("marker", "Marker status", ("marker_state_count", "marker_state_fraction", "population_marker_fraction")),
    ("intensity", "Channel intensities", ()),
    ("shape", "Cell and nuclear shape", ()),
    ("other", "Other saved measurements", ()),
    ("location", "Tissue distance and overlap", ("distance_to_primary", "overlap_fraction_with_primary")),
    ("confidence", "Classifier confidence", ("classifier_confidence",)),
)


class _ExactChoiceField(QWidget):
    """Search offered choices without editing or replacing the current identity."""
    def __init__(self, combo, placeholder, parent=None):
        super().__init__(parent)
        self.combo = combo
        self._original_flags = None
        combo.model().modelReset.connect(self._choices_reset)
        body = QVBoxLayout(self)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(5)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText(placeholder)
        self.search.setAccessibleName(placeholder.rstrip('…'))
        self.search.setClearButtonEnabled(True)
        body.addWidget(self.search)
        body.addWidget(combo)
        self.no_matches = QLabel("No matching choices.", self)
        self.no_matches.setProperty("fieldHelp", True)
        body.addWidget(self.no_matches)
        self.search.textChanged.connect(self._filter)
        self.reset_search()

    def reset_search(self):
        self.search.clear()
        self.search.setVisible(self.combo.count() > 13 and not self.combo.isEditable())
        self._filter()

    def _choices_reset(self):
        # The same combo is repopulated for different criteria and categories.
        # Availability from its old model must never leak into the new choices.
        self._original_flags = None

    def _filter(self, *_):
        terms = self.search.text().casefold().split()
        model = self.combo.model()
        if terms and self._original_flags is None:
            self._original_flags = [model.item(index).flags() for index in range(self.combo.count())]
        for index in range(self.combo.count()):
            value = self.combo.itemData(index)
            text = (self.combo.itemText(index) + " " + str(value or "")).casefold()
            hidden = bool(value) and not all(term in text for term in terms)
            self.combo.view().setRowHidden(index, hidden)
            if self._original_flags is not None:
                flags = self._original_flags[index]
                # Hiding a popup row alone does not affect a closed combo's
                # keyboard traversal. Disable only nonmatching choices while
                # retaining the source availability flags for clearing search.
                model.item(index).setFlags(flags & ~Qt.ItemFlag.ItemIsEnabled if hidden else flags)
        if not terms:
            self._original_flags = None
        self.no_matches.setVisible(bool(terms) and not self.visible_values())

    def visible_values(self):
        return [self.combo.itemData(index) for index in range(self.combo.count())
                if self.combo.itemData(index) and not self.combo.view().isRowHidden(index)]


def _group(node):
    return next((key for key in ("all_of", "any_of", "at_least") if key in node), None)


class CriterionEditor(QWidget):
    """Preserve criterion IDs and nesting while editing their ordinary fields."""
    changed = Signal()
    service_requested = Signal(str, object, object)

    def __init__(self, context, parent=None):
        super().__init__(parent)
        self._context = context
        self._loading = False
        self._selected = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self._row_labels = {}
        self._row_frames = []
        self.workspace = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.workspace.setSpacing(18)
        layout.addLayout(self.workspace)
        self.roster = QWidget(self)
        roster_layout = QVBoxLayout(self.roster)
        roster_layout.setContentsMargins(0, 0, 0, 0)
        roster_layout.setSpacing(8)
        roster_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        roster_heading = QLabel("Selected measurements", self.roster)
        roster_heading.setProperty("panelHeading", True)
        layout.insertWidget(0, roster_heading)
        self.rows = QVBoxLayout()
        self.rows.setSpacing(0)
        roster_layout.addLayout(self.rows)
        self.workspace.addWidget(self.roster, 5, Qt.AlignmentFlag.AlignTop)
        self.add_button = QPushButton("+ Add measurement", self)
        self.add_button.setObjectName("secondary-add-measurement")
        self.add_menu = QMenu(self.add_button)
        from mif_pipeline.cell_analysis.measurement_plan import shape_measurement_choices
        self._shape_labels = {row["measurement_name"]: label for _key, label, row in shape_measurement_choices()}
        self._inventory = context.get("cell_selector_inventory")
        self._families = list(_MEASUREMENT_TYPES)
        if self._inventory is None:
            self._families = [row for row in self._families if row[0] not in {"intensity", "shape", "other"}]
            self._families.insert(4, ("saved", "Saved cell measurements", ("cell_measurement_aggregate",)))
        for key, title, _quantities in self._families:
            action = self.add_menu.addAction(title)
            action.setEnabled(self._family_available(key))
            action.triggered.connect(lambda _checked=False, selected=key: self._add_family(selected))
        self.add_button.setMenu(self.add_menu)
        roster_layout.addWidget(self.add_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.list_panel = QWidget(self)
        self.list_panel.hide()
        self.tree = QTreeWidget(self.list_panel)
        self.tree.setObjectName("secondary-criteria-tree")
        self.tree.setAccessibleName("Selected review measurements")
        self.tree.setHeaderHidden(True)
        self.tree.setColumnCount(2)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.tree.header().setStretchLastSection(False)
        self.tree.setColumnWidth(1, 32)
        self.tree.setIndentation(16)
        self.tree.setUniformRowHeights(False)
        self.tree.setWordWrap(True)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tree.setFrameShape(QFrame.Shape.NoFrame)
        self.tree.setStyleSheet("""
            QTreeWidget { background: #edf3f1; border: none; padding: 4px; }
            QTreeWidget::item { padding: 7px 5px; border-radius: 3px; }
            QTreeWidget::item:selected { background: #d4eae5; color: #005d62; }
            QTreeWidget::item:hover:!selected { background: #e0eeea; }
        """)
        self.detail_panel = QFrame(self)
        self.detail_panel.setObjectName("secondary-measurement-details")
        self.detail_panel.setStyleSheet("QFrame#secondary-measurement-details { background:#ffffff; border:1px solid #d3dfda; border-radius:6px; }")
        self.detail_panel.setMinimumWidth(0)
        self.detail_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        detail_layout = QVBoxLayout(self.detail_panel)
        detail_layout.setContentsMargins(16, 14, 16, 16)
        detail_layout.setSpacing(14)
        self.detail_heading = QLabel("Measurement settings", self.detail_panel)
        self.detail_heading.setProperty("panelHeading", True)
        self.detail_heading.setWordWrap(True)
        detail_layout.addWidget(self.detail_heading)
        self.fields = QWidget(self.detail_panel)
        self.fields.setMinimumWidth(0)
        detail_layout.addWidget(self.fields)
        self.workspace.addWidget(self.detail_panel, 6, Qt.AlignmentFlag.AlignTop)
        self.form = QFormLayout(self.fields)
        self.form.setContentsMargins(0, 0, 0, 0)
        self.form.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.form.setVerticalSpacing(10)
        self.form.setHorizontalSpacing(16)
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.quantity = QComboBox(self.fields)
        self.quantity.addItem("Choose a quantity…", None)
        for key, (label, _unit) in QUANTITIES.items():
            self.quantity.addItem(label, key)
            inventory = context.get("cell_selector_inventory")
            needed = ("phenotype_ids" if key.startswith("phenotype_") else
                      "marker_ids" if key.startswith("marker_state_") else
                      "measurement_ids" if key == "cell_measurement_aggregate" else None)
            if inventory is not None and needed and not inventory[needed]:
                self.quantity.model().item(self.quantity.count() - 1).setEnabled(False)
        self.quantity.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.quantity.setMinimumContentsLength(18)
        # Keep the existing quantity editor as the type-change adapter; the
        # visible catalog offers exactly one category-dependent Measure choice.
        self.quantity.hide()
        self.category = QComboBox(self.fields)
        self.category.setObjectName("secondary-measurement-type")
        self.category.setAccessibleName("Measurement type")
        self.category.addItem("Choose a measurement type…", None)
        for key, title, _quantities in self._families:
            self.category.addItem(title, key)
            self.category.model().item(self.category.count() - 1).setEnabled(self._family_available(key))
        self.category.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.category.setMinimumContentsLength(18)
        self.form.addRow("Measurement type", self.category)
        self.measure_choice = QComboBox(self.fields)
        self.measure_choice.setObjectName("secondary-measure-choice")
        self.measure_choice.setAccessibleName("Measure")
        self.measure_choice.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.measure_choice.setMinimumContentsLength(18)
        self.measurement_search = _ExactChoiceField(self.measure_choice, "Find a measurement…", self.fields)
        self.measurement_label = QLabel("Measure", self.fields)
        self.form.addRow(self.measurement_label, self.measurement_search)
        self._measure_options = {}
        self.selector_widgets = {}
        self._selector_fields = {}
        for key, label in SELECTOR_LABELS.items():
            combo = QComboBox(self.fields)
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(16)
            field = _ExactChoiceField(combo, "Find a " + ("measurement" if key == "measurement_id" else label.lower()) + "…", self.fields)
            self.form.addRow("Measured column" if key == "measurement_id" else label, field)
            self.selector_widgets[key] = combo
            self._selector_fields[key] = field
            combo.currentTextChanged.connect(self._edited)
        from .secondary_marker_review import PopulationMarkerSelector
        self.population_marker = PopulationMarkerSelector(context, self.fields)
        self.population_marker.changed.connect(self._edited)
        self.population_marker.service_requested.connect(self.service_requested)
        self.form.addRow(self.population_marker)
        self.statistics = QWidget(self.fields)
        statistic_layout = QVBoxLayout(self.statistics)
        statistic_layout.setContentsMargins(0, 0, 0, 0)
        statistic_layout.setSpacing(5)
        statistic_strip = QFrame(self.statistics)
        statistic_strip.setObjectName("secondary-summary-choice")
        statistic_strip.setStyleSheet("QFrame#secondary-summary-choice { border:1px solid #cbdcd7; border-radius:4px; background:#f2f6f4; }")
        statistic_choices = QHBoxLayout(statistic_strip)
        statistic_choices.setContentsMargins(2, 2, 2, 2)
        statistic_choices.setSpacing(2)
        self.statistic_buttons = {}
        for label, value in (("Mean", "mean"), ("Median", "median")):
            button = QPushButton(label, statistic_strip)
            button.setObjectName("secondary-statistic-" + value)
            button.setAccessibleName(label + " across cells in this region")
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setMinimumWidth(68)
            button.setStyleSheet("QPushButton { padding:5px 10px; border:1px solid transparent; border-radius:2px; background:transparent; } QPushButton:checked { background:#d4eae5; border-color:#92b9b2; color:#005d62; font-weight:600; } QPushButton:focus { border:1px dotted #006b70; }")
            button.clicked.connect(lambda _checked=False, selected=value:
                self.selector_widgets["statistic"].setCurrentIndex(
                    self.selector_widgets["statistic"].findData(selected)))
            statistic_choices.addWidget(button)
            self.statistic_buttons[value] = button
        statistic_layout.addWidget(statistic_strip, 0, Qt.AlignmentFlag.AlignLeft)
        self.form.addRow("Summary across cells", self.statistics)
        self.selector_widgets["statistic"].currentIndexChanged.connect(self._statistic_changed)
        self.compare = QCheckBox("Compare with a threshold", self.fields)
        self.compare.setObjectName("secondary-measurement-compare")
        self.form.addRow(self.compare)
        self.compare.toggled.connect(self._comparison_changed)
        self.threshold_row = QWidget(self.fields)
        threshold_layout = QHBoxLayout(self.threshold_row)
        threshold_layout.setContentsMargins(0, 0, 0, 0)
        threshold_layout.setSpacing(8)
        self.comparison = QComboBox(self.threshold_row)
        for label, value in COMPARISONS:
            self.comparison.addItem(label, value)
        self.comparison.setAccessibleName("Threshold comparison")
        threshold_layout.addWidget(self.comparison, 3)
        self.threshold = QLineEdit(self.fields)
        self.threshold.setPlaceholderText("Enter a value")
        self.threshold.setAccessibleName("Threshold value")
        self.threshold.setMinimumWidth(0)
        threshold_layout.addWidget(self.threshold, 2)
        self.unit = QLabel(self.threshold_row)
        threshold_layout.addWidget(self.unit)
        self.form.addRow(self.threshold_row)
        self.help = QLabel(self.fields)
        self.help.setWordWrap(True)
        self.help.setProperty("fieldHelp", True)
        self.form.addRow(self.help)
        self.empty = QLabel("No measurements selected.", self)
        self.empty.setProperty("fieldHelp", True)
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.empty.setContentsMargins(0, 8, 0, 0)
        roster_layout.insertWidget(1, self.empty)
        self.category.currentIndexChanged.connect(self._category_changed)
        self.measure_choice.currentIndexChanged.connect(self._measure_chosen)
        self.tree.currentItemChanged.connect(self._select)
        self.quantity.currentIndexChanged.connect(self._quantity_changed)
        self.comparison.currentIndexChanged.connect(self._edited)
        # textChanged also handles programmatic restoration in tests and host editors.
        self.threshold.textChanged.connect(self._edited)
        self.set_value({"all_of": []})

    def _quantity_available(self, kind):
        if kind == "population_marker_fraction":
            from .secondary_marker_review import marker_measurement_choices
            return self._inventory is not None and any(label != "Not phenotyped" for label in self._inventory.get("phenotype_ids", ())) and bool(marker_measurement_choices(self._context))
        needed = ("phenotype_ids" if kind.startswith("phenotype_") else
                  "marker_ids" if kind.startswith("marker_state_") else
                  "measurement_ids" if kind == "cell_measurement_aggregate" else None)
        return self._inventory is None or not needed or bool(self._inventory[needed])

    def _measurement_family(self, name):
        if self._inventory is None:
            return "saved"
        if self._inventory.get("measurement_channels", {}).get(name):
            return "intensity"
        return "shape" if name in self._shape_labels else "other"

    def _family_for(self, data):
        kind = data.get("type")
        if kind == "cell_measurement_aggregate":
            return self._measurement_family(data.get("selector", {}).get("measurement_id", ""))
        return next((key for key, _label, kinds in self._families if kind in kinds), None)

    def _family_available(self, family):
        kinds = next(kinds for key, _label, kinds in self._families if key == family)
        if kinds:
            return any(self._quantity_available(kind) for kind in kinds)
        return any(self._measurement_family(name) == family for name in self._inventory["measurement_ids"])

    def _populate_measures(self, family, data=None):
        data = data or {}
        with QSignalBlocker(self.measure_choice):
            self.measure_choice.clear()
            self.measure_choice.addItem("Choose a measure…", None)
            self._measure_options = {}
            kinds = next((kinds for key, _label, kinds in self._families if key == family), ())
            for kind in kinds:
                token = "quantity:" + kind
                self._measure_options[token] = (kind, None)
                self.measure_choice.addItem(QUANTITIES[kind][0], token)
                self.measure_choice.model().item(self.measure_choice.count() - 1).setEnabled(self._quantity_available(kind))
            if family in {"intensity", "shape", "other"}:
                names = [name for name in self._inventory["measurement_ids"] if self._measurement_family(name) == family]
                selected = data.get("selector", {}).get("measurement_id")
                if selected and self._measurement_family(selected) == family and selected not in names:
                    names.append(selected)  # Preserve an old saved choice; validation remains the owner's job.
                for name in names:
                    token = "measurement:" + name
                    self._measure_options[token] = ("cell_measurement_aggregate", name)
                    self.measure_choice.addItem(self._shape_labels.get(name, measurement_column_label(name)), token)
            token = ("measurement:" + str(data.get("selector", {}).get("measurement_id", ""))
                     if data.get("type") == "cell_measurement_aggregate" and family != "saved"
                     else "quantity:" + str(data.get("type")))
            self.measure_choice.setCurrentIndex(max(0, self.measure_choice.findData(token)))
            self.measurement_search.reset_search()

    def _category_changed(self, *_):
        if self._loading or self._selected is None:
            return
        data = self._selected.data(0, Qt.ItemDataRole.UserRole)
        family = self.category.currentData()
        if family == self._family_for(data):
            self._select(self._selected)
            return
        self._populate_measures(family)
        self.measurement_label.setText("Measure")
        for field in self._selector_fields.values():
            self.form.setRowVisible(field, False)
        for field in (self.statistics, self.population_marker, self.compare, self.threshold_row):
            self.form.setRowVisible(field, False)
        self.help.setText("Choose a measure in this type. The current measurement is kept until you choose.")
        self.form.setRowVisible(self.help, True)

    def _measure_chosen(self, *_):
        if self._loading or self._selected is None:
            return
        choice = self._measure_options.get(self.measure_choice.currentData())
        if choice is None:
            return
        kind, measurement_id = choice
        self.quantity.setCurrentIndex(self.quantity.findData(kind))
        if measurement_id is not None:
            combo = self.selector_widgets["measurement_id"]
            combo.setCurrentIndex(combo.findData(measurement_id))
        self._select(self._selected)

    def _add_family(self, family):
        self.add_condition()
        self.category.setCurrentIndex(self.category.findData(family))
        self.measure_choice.setFocus()

    def _fit_measurements(self, width=None):
        direction = (QBoxLayout.Direction.LeftToRight if self.width() >= 900
                     else QBoxLayout.Direction.TopToBottom)
        if self.workspace.direction() != direction:
            self.workspace.setDirection(direction)
        for frame in self._row_frames:
            available = max(80, frame.width() - 100)
            label = frame.header_label
            frame.header_button.setMinimumHeight(max(38, label.heightForWidth(available) + 14))
        self.updateGeometry()

    def _rebuild_rows(self):
        # The existing tree remains the exact criterion model, including saved groups.
        while self.rows.count():
            item = self.rows.takeAt(0)
            item.widget().deleteLater()
        self._row_labels.clear()
        self._row_frames.clear()
        root = self.tree.topLevelItem(0)
        flat = self.tree.rootIndex().isValid()

        def add(item, depth=0):
            data = item.data(0, Qt.ItemDataRole.UserRole)
            group = "group" in data
            active = item is self._selected and not group
            frame = QFrame(self.roster)
            frame.setObjectName("measurement-inline-row")
            frame.setStyleSheet("QFrame#measurement-inline-row { border:0; border-bottom:1px solid #d3dfda; background:" +
                ("#dceee8" if active else "#ffffff") + "; }")
            outer = QHBoxLayout(frame)
            outer.setContentsMargins(10 + depth * 14, 4, 6, 4)
            outer.setSpacing(8)
            button = QPushButton(frame)
            button.setFlat(True)
            button.setCheckable(not group)
            button.setChecked(active)
            button.setStyleSheet("QPushButton { border:0; background:transparent; text-align:left; padding:0; } QPushButton:focus { border:1px solid #006b70; }")
            button_layout = QHBoxLayout(button)
            button_layout.setContentsMargins(0, 3, 0, 3)
            button_layout.setSpacing(12)
            title, summary = self._roster_summary(item)
            label = QLabel(title, button)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            label.setStyleSheet("font-weight:" + ("600" if active else "400") + "; color:#174f53; background:transparent;")
            button_layout.addWidget(label, 1)
            detail = QLabel(summary, button)
            detail.setTextFormat(Qt.TextFormat.PlainText)
            detail.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            detail.setStyleSheet("color:#526d70; background:transparent;")
            detail.setToolTip("Summary across cells" if data.get("type") == "cell_measurement_aggregate" else "")
            detail.setVisible(bool(summary))
            button_layout.addWidget(detail)
            button.setAccessibleName("Edit " + item.text(0).replace("\n", ", "))
            button.clicked.connect(lambda _checked=False, selected=item: self._open_row(selected))
            if group:
                button.setEnabled(False)
            outer.addWidget(button, 1)
            remove = QToolButton(frame)
            remove.setText("×")
            remove.setAccessibleName("Remove " + item.text(0).split("\n", 1)[0])
            remove.setToolTip(remove.accessibleName())
            remove.setFixedSize(28, 28)
            remove.setStyleSheet("QToolButton { border:0; color:#576e73; background:transparent; font-size:20px; } QToolButton:hover { color:#873d37; background:#eadbd9; }")
            remove.clicked.connect(lambda _checked=False, selected=item: self._remove_item(selected))
            outer.addWidget(remove)
            frame.header_button = button
            frame.header_label = label
            frame.header_detail = detail
            self.rows.addWidget(frame)
            self._row_labels[id(item)] = (label, detail)
            self._row_frames.append(frame)
            for index in range(item.childCount()):
                add(item.child(index), depth + 1)

        if flat:
            for index in range(root.childCount()):
                add(root.child(index))
        elif root is not None:
            add(root)
        self.empty.setVisible(not self._row_frames)
        selected_data = self._selected.data(0, Qt.ItemDataRole.UserRole) if self._selected else {}
        self.detail_panel.setVisible(bool(self._selected) and "group" not in selected_data)
        if self._selected is not None and "group" not in selected_data:
            self.detail_heading.setText(self._roster_summary(self._selected)[0])
        self._fit_measurements()

    def _roster_summary(self, item):
        data = item.data(0, Qt.ItemDataRole.UserRole)
        lines = item.text(0).splitlines()
        statistic = {"mean": "Mean", "median": "Median"}.get(data.get("selector", {}).get("statistic"), "")
        # Aggregation belongs on the same row, separate from the measured column.
        lines = [line for line in lines if line != statistic + " across cells"]
        return " · ".join(lines), statistic

    def _open_row(self, item):
        self.tree.setCurrentItem(item)
        self._select(item)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_measurements()

    def _item(self, node, parent):
        item = QTreeWidgetItem(parent)
        item.setSizeHint(0, QSize(0, 36))
        kind = _group(node)
        if kind:
            data = {"group": kind, "k": node[kind]["k"] if kind == "at_least" else 1}
            children = node[kind]["criteria"] if kind == "at_least" else node[kind]
            for child in children:
                self._item(child, item)
        else:
            data = deepcopy(node)
        item.setData(0, Qt.ItemDataRole.UserRole, data)
        self._label(item)
        return item

    def _label(self, item):
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if "group" in data:
            label = {"all_of": "All conditions", "any_of": "Any condition",
                     "at_least": f"At least {data['k']} conditions"}[data["group"]]
        else:
            label = QUANTITIES.get(data.get("type"), ("Choose a measurement", ""))[0]
            selector = data.get("selector", {})
            selected = []
            for key in ("phenotype_id", "marker_id", "marker_state_id", "primary_class_id", "measurement_id", "channel_role", "statistic"):
                if selector.get(key):
                    choices, _editable = self._selector_choices(data.get("type"), key)
                    choice = next((name for name, value in choices if value == selector[key]), str(selector[key]))
                    selected.append(choice + " across cells" if key == "statistic" else choice)
            if data.get("type") == "population_marker_fraction":
                import json
                try:
                    populations = json.loads(selector.get("phenotype_ids", "[]"))
                except (TypeError, ValueError):
                    populations = []
                label = measurement_column_label(selector.get("measurement_id", "")) or "Marker fraction within a phenotype population"
                if populations:
                    label += "\n" + str(selector.get("marker_state_id", "positive")).capitalize() + " fraction in " + " + ".join(populations)
            elif data.get("type") == "cell_measurement_aggregate" and selector.get("measurement_id"):
                statistic = {"mean": "Mean", "median": "Median"}.get(selector.get("statistic"))
                label = measurement_column_label(selector["measurement_id"])
                if statistic:
                    label += "\n" + statistic + " across cells"
            elif selected:
                label += "\n" + "; ".join(selected)
            if data.get("type") == "manual_region_decision":
                label = data.get("prompt") or label
            elif data.get("threshold") is not None:
                comparison = {"ge": "≥", "gt": ">", "le": "≤", "lt": "<", "eq": "="}
                value = data["threshold"]
                value = f"{value:g}" if isinstance(value, (int, float)) else value
                unit = data.get("unit") or QUANTITIES.get(data.get("type"), ("", ""))[1]
                suffix = "" if unit in {"measurement units", "score"} else unit
                label += f"\n{comparison.get(data.get('comparator'), '')} {value} {suffix}".rstrip()
            if data.get("required") is False:
                label += " (optional)"
        item.setText(0, label)
        item.setToolTip(0, label)
        if id(item) in self._row_labels:
            text, summary = self._roster_summary(item)
            title, detail = self._row_labels[id(item)]
            title.setText(text)
            detail.setText(summary)
            detail.setVisible(bool(summary))
            if item is self._selected:
                self.detail_heading.setText(text)

    def set_value(self, value):
        self._loading = True
        self._selected = None
        self.tree.setRootIndex(QModelIndex())
        self._row_labels.clear()
        self.tree.clear()
        item = self._item(value if value is not None else {"all_of": []}, self.tree)
        self.tree.expandAll()
        flat = (_group(value or {"all_of": []}) == "all_of" and
                all(_group(child) is None for child in (value or {"all_of": []})["all_of"]))
        self.tree.setRootIndex(self.tree.indexFromItem(item) if flat else QModelIndex())
        self.tree.setRootIsDecorated(not flat)
        self._loading = False
        self.tree.setCurrentItem(item)
        self._select(item.child(0) if item.childCount() else item)
        if item.childCount():
            self.tree.setCurrentItem(item.child(0))

    def _read(self, item, *, draft):
        data = deepcopy(item.data(0, Qt.ItemDataRole.UserRole))
        if "group" in data:
            children = [self._read(item.child(i), draft=draft) for i in range(item.childCount())]
            if not children:
                if item.parent() is None and data["group"] == "all_of":
                    return None
                if not draft:
                    raise ValueError("Add a condition to each saved group, or remove the empty group.")
            return {data["group"]: {"k": data["k"], "criteria": children}
                    if data["group"] == "at_least" else children}
        if not draft:
            if data.get("type") == "manual_region_decision":
                raise ValueError("Remove the old review question before saving this definition. Every region now has one researcher review.")
            if data.get("type") not in QUANTITIES:
                raise ValueError("Choose a measurement for each item.")
            elif "comparator" in data or "threshold" in data:
                raw = data.get("threshold")
                try:
                    if data["type"].endswith("_count") and isinstance(raw, float) and not raw.is_integer():
                        raise ValueError
                    data["threshold"] = int(raw) if data["type"].endswith("_count") else float(raw)
                    if not math.isfinite(data["threshold"]):
                        raise ValueError
                except (ValueError, TypeError, OverflowError):
                    raise ValueError("Enter a number for each selected threshold. Use whole numbers for counts.") from None
            if data["type"] == "population_marker_fraction":
                import json
                selector = data.get("selector", {})
                try:
                    populations = json.loads(selector.get("phenotype_ids", "[]"))
                except (TypeError, ValueError):
                    populations = []
                if not populations:
                    raise ValueError("Choose at least one phenotype population.")
                if not selector.get("positivity_gate"):
                    raise ValueError("Review a positivity threshold for each population marker fraction.")
            for key in SELECTORS.get(data["type"], ()):
                if not (data.get("selector") or {}).get(key):
                    label = "measurement name" if key == "measurement_id" else SELECTOR_LABELS[key].lower()
                    raise ValueError(f"Choose or enter a {label} for each selected measurement.")
        return data

    def capture_draft(self):
        return self._read(self.tree.topLevelItem(0), draft=True)

    def value(self):
        return self._read(self.tree.topLevelItem(0), draft=False)

    def _selector_choices(self, kind, key):
        config = self._context.get("validation_config", {})
        fixed = {
            "marker_state_id": [("Positive", "positive"), ("Negative", "negative")],
            "statistic": [("Mean", "mean"), ("Median", "median")],
            "missing_policy": [("Exclude cells without a measurement", "exclude"), ("Report unavailable", "unavailable")],
            "denominator": ([("Cells in the region", "segmented_cells")]
                if kind != "overlap_fraction_with_primary" else
                [("Candidate region area", "candidate_area"), ("Primary compartment area", "primary_area")]),
            "primary_class_id": [(row["display_name"], row["class_id"]) for row in (config.get("primary_classes") or ())],
        }
        if key in fixed:
            return fixed[key], False
        inventory = self._context.get("cell_selector_inventory")
        inventory_key = {"phenotype_id": "phenotype_ids", "marker_id": "marker_ids",
                         "measurement_id": "measurement_ids", "channel_role": "channel_roles"}.get(key)
        if inventory is not None and inventory_key:
            labels = channel_choice_labels(config.get("channels", ()))
            return [(measurement_column_label(value) if key == "measurement_id" else
                     labels.get(value, value) if key == "channel_role" else value, value)
                    for value in inventory[inventory_key]], False
        if key == "channel_role":
            labels = channel_choice_labels(config.get("channels", ()))
            return [(labels.get(channel, channel), role)
                    for role, channel in self._context.get("channel_roles", {}).items()], False
        phenotype = config.get("phenotyping", {})
        choices = {
            "phenotype_id": [(r.get("display_name", r["class_id"]), r["class_id"])
                             for r in phenotype.get("phenotype_classes", ())],
            "marker_id": [(key, key) for key in phenotype.get("known_marker_rules", {})],
        }
        return choices.get(key, []), True

    def _select(self, item, _previous=None):
        if self._loading or item is None:
            return
        self._loading = True
        self._selected = item
        data = item.data(0, Qt.ItemDataRole.UserRole)
        group = data.get("group")
        kind = data.get("type")
        manual = kind == "manual_region_decision"
        self.fields.setVisible(not group)
        self.empty.setVisible(bool(group))
        self.form.setRowVisible(self.category, not group and not manual)
        self.form.setRowVisible(self.measurement_search, not group and not manual)
        self.form.setRowVisible(self.compare, not group and kind is not None and not manual)
        compared = "comparator" in data or "threshold" in data
        self.compare.setChecked(compared)
        self.form.setRowVisible(self.threshold_row, not group and kind is not None and not manual and compared)
        self.quantity.setCurrentIndex(max(0, self.quantity.findData(kind)))
        self.comparison.setCurrentIndex(max(0, self.comparison.findData(data.get("comparator", "ge"))))
        self.threshold.setText("" if data.get("threshold") is None else str(data["threshold"]))
        unit = data.get("unit") or QUANTITIES.get(kind, ("", ""))[1]
        if unit in {"fraction", "score", "measurement units"}:
            unit = ""
        self.unit.setText(unit)
        self.measurement_label.setText("Measure" + (f" ({unit})" if unit and not compared else ""))
        for key, widget in self.selector_widgets.items():
            choices, editable = self._selector_choices(kind, key)
            widget.clear()
            widget.setEditable(editable)
            widget.addItem("Choose…", "")
            for label, value in choices:
                widget.addItem(label, value)
            selected = data.get("selector", {}).get(key, "")
            constant = (key in {"missing_policy", "denominator"} and len(choices) == 1
                        and not editable and selected == choices[0][1])
            self.form.setRowVisible(self._selector_fields[key], key in SELECTORS.get(kind, ()) and key not in {"statistic", "missing_policy"} and not constant
                                    and not (key == "measurement_id" and self._inventory is not None))
            index = widget.findData(selected)
            if index < 0:
                widget.addItem(selected, selected)
                index = widget.count() - 1
            widget.setCurrentIndex(index)
            self._selector_fields[key].reset_search()
        self.form.setRowVisible(self.population_marker, kind == "population_marker_fraction")
        if kind == "population_marker_fraction":
            for field in self._selector_fields.values():
                self.form.setRowVisible(field, False)
            self.population_marker.set_selector(data.get("selector", {}), binding=(id(item), data.get("criterion_id")))
        elif self.population_marker._binding is not None:
            self.population_marker.set_selector({}, binding=None)
        shared_labels = [self.form.labelForField(self.category), self.measurement_label]
        population_form = self.population_marker.layout()
        shared_labels.extend(population_form.itemAt(row, QFormLayout.ItemRole.LabelRole).widget()
            for row in range(population_form.rowCount()) if population_form.itemAt(row, QFormLayout.ItemRole.LabelRole))
        label_width = max(label.fontMetrics().horizontalAdvance(label.text()) for label in shared_labels) if kind == "population_marker_fraction" else 0
        for label in shared_labels:
            label.setMinimumWidth(label_width)
        family = self._family_for(data)
        self.category.setCurrentIndex(max(0, self.category.findData(family)))
        self._populate_measures(family, data)
        self.form.setRowVisible(self.statistics, kind == "cell_measurement_aggregate")
        self._statistic_changed()
        self._refresh_help(data, item)
        self._loading = False
        self._rebuild_rows()

    def _refresh_help(self, data, item=None):
        item = item or self._selected
        kind = data.get("type")
        group = data.get("group")
        manual = kind == "manual_region_decision"
        compared = "comparator" in data or "threshold" in data
        if manual:
            hint = "This saved question belongs to the previous review format. Remove it before saving; region review now records the researcher’s decision directly."
        elif compared and kind in {"phenotype_fraction", "marker_state_fraction", "population_marker_fraction", "overlap_fraction_with_primary"}:
            hint = "Enter fractions from 0 to 1; 0.25 means 25%."
        elif kind in {"phenotype_count", "phenotype_density"} and not data.get("selector", {}).get("phenotype_id"):
            hint = "Choose a phenotype from the completed cell assignments."
        elif kind == "marker_state_count" and not data.get("selector", {}).get("marker_id"):
            hint = "Choose a marker from the completed marker assignments."
        elif kind == "classifier_confidence":
            hint = "Review can show classifier confidence when the detector has saved a score for this region."
        elif group:
            hint = ("No measurements selected."
                    if item.childCount() == 0 else "Select a condition to edit its measurement. This saved group retains its existing calculation.")
        else:
            hint = ""
        self.help.setText(hint)
        if group:
            self.empty.setText(hint)
        self.form.setRowVisible(self.help, bool(hint))

    def _statistic_changed(self, *_):
        value = self.selector_widgets["statistic"].currentData()
        for key, button in self.statistic_buttons.items():
            button.setAutoExclusive(False)
            button.setChecked(key == value)
            button.setAutoExclusive(True)

    def _edited(self, *_):
        if self._loading or self._selected is None:
            return
        data = deepcopy(self._selected.data(0, Qt.ItemDataRole.UserRole))
        if data.get("type"):
            if data["type"] != "manual_region_decision":
                if self.compare.isChecked():
                    data["comparator"] = self.comparison.currentData()
                    text = self.threshold.text().strip()
                    # Preserve incomplete typing until submission.
                    try:
                        data["threshold"] = int(text) if data["type"].endswith("_count") else float(text)
                    except ValueError:
                        data["threshold"] = text
                else:
                    data.pop("comparator", None)
                    data.pop("threshold", None)
                keys = SELECTORS.get(data["type"], ())
                if keys:
                    previous = data.get("selector", {})
                    data["selector"] = ({"channel_role": previous["channel_role"]}
                                        if data["type"] == "cell_measurement_aggregate" and "channel_role" in previous else {})
                    for key in keys:
                        if data["type"] == "population_marker_fraction":
                            continue
                        widget = self.selector_widgets[key]
                        index = widget.currentIndex()
                        data["selector"][key] = (widget.itemData(index)
                            if index >= 0 and widget.currentText() == widget.itemText(index)
                            else widget.currentText().strip())
                    if data["type"] == "population_marker_fraction":
                        data["selector"] = self.population_marker.selector()
                    if data["selector"].get("measurement_id") != previous.get("measurement_id"):
                        data["selector"].pop("channel_role", None)
        self._selected.setData(0, Qt.ItemDataRole.UserRole, data)
        self._refresh_help(data)
        self._label(self._selected)
        self._fit_measurements()
        self.changed.emit()

    def _quantity_changed(self, *_):
        if self._loading or self._selected is None:
            return
        old = self._selected.data(0, Qt.ItemDataRole.UserRole)
        kind = self.quantity.currentData()
        data = {"criterion_id": old["criterion_id"], "type": kind}
        if "required" in old:
            data["required"] = old["required"]
        if kind:
            data.update(selector={})
            unit = QUANTITIES[kind][1]
            if unit not in {"measurement units", "score"}:
                data["unit"] = unit
            for key in SELECTORS.get(kind, ()):
                choices, editable = self._selector_choices(kind, key)
                data["selector"][key] = choices[0][1] if len(choices) == 1 and not editable else ""
            if kind == "cell_measurement_aggregate":
                data["selector"]["missing_policy"] = "exclude"
            elif kind == "population_marker_fraction":
                data["selector"].update(phenotype_ids="[]", marker_state_id="positive", denominator="valid_marker_cells", missing_policy="exclude")
        self._selected.setData(0, Qt.ItemDataRole.UserRole, data)
        self._label(self._selected)
        self._select(self._selected)
        self.changed.emit()

    def _comparison_changed(self, *_):
        if self._loading or self._selected is None:
            return
        self._edited()
        self._select(self._selected)

    def _append(self, node):
        selected = self.tree.currentItem() or self.tree.topLevelItem(0)
        if "group" not in selected.data(0, Qt.ItemDataRole.UserRole):
            if selected.parent() is not None:
                selected = selected.parent()
            else:
                root = self.tree.takeTopLevelItem(0)
                selected = self._item({"all_of": []}, self.tree)
                selected.addChild(root)
        item = self._item(node, selected)
        self.tree.expandAll()
        self.tree.setCurrentItem(item)
        self.changed.emit()

    def add_condition(self, kind=None):
        self._append({"criterion_id": "criterion-" + uuid4().hex[:12], "type": None})
        if kind in QUANTITIES:
            self.quantity.setCurrentIndex(self.quantity.findData(kind))

    def remove_selected(self):
        self._remove_item(self.tree.currentItem())

    def _remove_item(self, item):
        if item is None:
            return
        if item.parent() is None:
            self.set_value({"all_of": []})
        else:
            parent = item.parent()
            parent.removeChild(item)
            selected = parent.child(0) if parent.childCount() else parent
            self.tree.setCurrentItem(selected)
            self._select(selected)
        self.changed.emit()


class CriterionProfileEditor(QWidget):
    """Edit the selected structure or subtype's optional measurement conditions."""
    changed = Signal()
    service_requested = Signal(str, object, object)

    def __init__(self, context, parent=None):
        super().__init__(parent)
        self._profile = None
        self._index = 0
        self._loading = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.editor = CriterionEditor(context, self)
        layout.addWidget(self.editor)
        self.editor.changed.connect(self._changed)
        self.editor.service_requested.connect(self.service_requested)

    def set_value(self, profile, index=0):
        self._profile = deepcopy(profile)
        self._index = min(index, len(self._profile.get("classification_rules", ())))
        self._load()

    def _save(self, draft=True):
        if self._profile is None or self._loading:
            return
        criteria = self.editor.capture_draft() if draft else self.editor.value()
        if self._index == 0:
            self._profile["criteria"] = criteria
        else:
            self._profile["classification_rules"][self._index - 1]["criteria"] = criteria

    def _load(self):
        self._loading = True
        rule = None if self._index == 0 else self._profile["classification_rules"][self._index - 1]
        criteria = self._profile.get("criteria") if rule is None else rule.get("criteria")
        self.editor.set_value(criteria)
        self._loading = False

    def set_rule_name(self, name):
        if self._index:
            self._profile["classification_rules"][self._index - 1]["class_id"] = name

    def select_rule(self, index):
        self._save()
        self._index = index
        self._load()

    def _changed(self, *_):
        if self._loading:
            return
        self._save()
        self.changed.emit()

    def add_rule(self, name):
        self._save()
        name = name.strip()
        if not name or any(row["class_id"] == name for row in self._profile.get("classification_rules", ())):
            raise ValueError("Enter a distinct, nonempty subtype name.")
        rules = self._profile.setdefault("classification_rules", [])
        rules.append({"class_id": name, "criteria": None})
        self._index = len(rules)
        self._load()
        self.changed.emit()

    def remove_rule(self):
        if self._index:
            del self._profile["classification_rules"][self._index - 1]
            if not self._profile["classification_rules"]:
                del self._profile["classification_rules"]
            self._index = 0
            self._load()
            self.changed.emit()

    def capture_draft(self):
        self._save()
        return deepcopy(self._profile)

    def value(self):
        self._save(draft=False)
        return deepcopy(self._profile)

    def set_channel_roles(self, roles):
        if self.editor._context.get("channel_roles") != dict(roles):
            self.editor._context = {**self.editor._context, "channel_roles": dict(roles)}
            self.editor._select(self.editor.tree.currentItem())
