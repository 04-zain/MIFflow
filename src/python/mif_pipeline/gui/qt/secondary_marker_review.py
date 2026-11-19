"""Source-bound marker thresholds for secondary-structure measurements."""
from copy import deepcopy
from collections.abc import Mapping
import json
from math import isfinite

from PySide6.QtCore import Qt, Signal, QSignalBlocker, QTimer
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QComboBox, QListWidget, QListWidgetItem, QPushButton,
    QDialog, QDialogButtonBox, QScrollArea, QCheckBox)

from ..structure_criteria import measurement_column_label
from .marker_gates import MarkerHistogram, _intensity_gate_caption
from .marker_gate_field import MarkerGateField


def _copy_evidence(value):
    """Copy the owner’s immutable records without changing numerical values."""
    if isinstance(value, Mapping):
        return {key: _copy_evidence(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_copy_evidence(item) for item in value]
    return value


def _json(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def marker_measurement_choices(context):
    inventory = context.get("cell_selector_inventory") or {}
    explicit = context.get("marker_review_scope", {}).get("marker_measurement_ids")
    if explicit is not None:
        return [name for name in inventory.get("measurement_ids", ()) if name in explicit]
    return [name for name in inventory.get("measurement_ids", ())
            if inventory.get("measurement_channels", {}).get(name) and name.rsplit(": ", 1)[-1] in {"Mean", "Median"}]


def _decode(value, default):
    try:
        return json.loads(value) if value else deepcopy(default)
    except (ValueError, TypeError):
        return deepcopy(default)


class _PopulationMarkerField(MarkerGateField):
    """Inspect measured intensities without relabeling saved phenotype calls."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.population.setItemText(0, "At or above threshold")
        self.population.setItemText(1, "Below threshold")

    def _inspect_cell(self, row):
        value = row.get("value")
        self.cell_value.setText(f"Intensity {value:.6g}" if value is not None and isfinite(value) else "Intensity unavailable.")
        self.cell_value.setToolTip(f'Cell {row["cell_id"]}\nExact saved value: {value!r}')
        self.cell_value.show()


class PopulationMarkerSelector(QWidget):
    changed = Signal()
    service_requested = Signal(str, object, object)

    def __init__(self, context, parent=None):
        super().__init__(parent)
        from .criterion_editor import _ExactChoiceField
        self.revision = 0
        self._loading = False
        self._gate_text = ""
        self._extra = {}
        self._lookup = None
        self._binding = None
        self._dialog = None
        inventory = context.get("cell_selector_inventory") or {}
        self._available_populations = [label for label in inventory.get("phenotype_ids", ()) if label != "Not phenotyped"]
        self._available_measurements = marker_measurement_choices(context)
        body = QFormLayout(self)
        body.setContentsMargins(0, 0, 0, 0)
        body.setVerticalSpacing(10)
        body.setHorizontalSpacing(16)
        body.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        body.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        body.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        population = QWidget(self)
        layout = QVBoxLayout(population)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        self.search = QLineEdit(population)
        self.search.setPlaceholderText("Find a phenotype…")
        self.search.setClearButtonEnabled(True)
        layout.addWidget(self.search)
        self.populations = QListWidget(population)
        self.populations.setAccessibleName("Phenotype populations for marker fraction")
        self.populations.setMaximumHeight(112)
        layout.addWidget(self.populations)
        self.population_summary = QLabel(population)
        self.population_summary.setWordWrap(True)
        self.population_summary.setProperty("fieldHelp", True)
        layout.addWidget(self.population_summary)
        body.addRow("Population", population)
        self.measurement = QComboBox(self)
        self.measurement.setMinimumContentsLength(18)
        self.measurement.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.measurement_field = _ExactChoiceField(self.measurement, "Find an intensity measurement…", self)
        body.addRow("Intensity measurement", self.measurement_field)
        self.state = QComboBox(self)
        self.state.addItem("Positive", "positive")
        self.state.addItem("Negative", "negative")
        body.addRow("Fraction", self.state)
        self.denominator = QComboBox(self)
        self.denominator.addItem("Selected population", "valid_marker_cells")
        self.denominator.addItem("All cells in the region", "all_valid_marker_cells")
        body.addRow("Divide by", self.denominator)
        self.denominator.currentIndexChanged.connect(self._state_changed)
        threshold = QWidget(self)
        row = QVBoxLayout(threshold)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.gate_summary = QLabel(threshold)
        self.gate_summary.setWordWrap(True)
        row.addWidget(self.gate_summary)
        self.review_button = QPushButton("Review threshold…", threshold)
        row.addWidget(self.review_button, 0, Qt.AlignmentFlag.AlignLeft)
        body.addRow("Positivity threshold", threshold)
        self.search.textChanged.connect(self._filter)
        self.populations.itemChanged.connect(self._selection_changed)
        self.measurement.currentIndexChanged.connect(self._selection_changed)
        self.state.currentIndexChanged.connect(self._state_changed)
        self.review_button.clicked.connect(self.open_review)
        self.set_selector({})

    def _filter(self):
        terms = self.search.text().casefold().split()
        for index in range(self.populations.count()):
            item = self.populations.item(index)
            item.setHidden(not all(term in item.text().casefold() for term in terms))

    def selected_populations(self):
        return [self.populations.item(index).data(Qt.ItemDataRole.UserRole)
                for index in range(self.populations.count())
                if self.populations.item(index).checkState() == Qt.CheckState.Checked]

    def identity(self):
        return {"measurement_id": self.measurement.currentData() or "",
                "phenotype_ids": self.selected_populations()}

    def selector(self):
        return {**self._extra, "phenotype_ids": _json(self.selected_populations()),
                "measurement_id": self.measurement.currentData() or "",
                "marker_state_id": self.state.currentData(), "denominator": self.denominator.currentData(),
                "missing_policy": "exclude", "positivity_gate": self._gate_text}

    def set_selector(self, selector, binding=None):
        old_binding = self._binding
        self._binding = binding
        old = self.identity() if self.populations.count() else None
        self._loading = True
        selected = _decode(selector.get("phenotype_ids"), [])
        self._extra = {key: value for key, value in selector.items() if key == "channel_role"}
        self._gate_text = selector.get("positivity_gate", "")
        self.populations.clear()
        for value in dict.fromkeys([*self._available_populations, *selected]):
            item = QListWidgetItem(value, self.populations)
            item.setData(Qt.ItemDataRole.UserRole, value)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if value in selected else Qt.CheckState.Unchecked)
        self.populations.setFixedHeight(min(112, max(48, self.populations.count() * (self.populations.fontMetrics().height() + 5) + 6)))
        self.search.setVisible(len(self._available_populations) > 8)
        self._filter()
        self.measurement.clear()
        self.measurement.addItem("Choose an intensity measurement…", "")
        current = selector.get("measurement_id", "")
        for value in dict.fromkeys([*self._available_measurements, *([current] if current else [])]):
            self.measurement.addItem(measurement_column_label(value), value)
        self.measurement.setCurrentIndex(max(0, self.measurement.findData(current)))
        self.measurement_field.reset_search()
        self.state.setCurrentIndex(max(0, self.state.findData(selector.get("marker_state_id", "positive"))))
        self.denominator.setCurrentIndex(max(0, self.denominator.findData(selector.get("denominator", "valid_marker_cells"))))
        self._loading = False
        if old != self.identity() or old_binding != self._binding:
            self.revision += 1
            self._lookup = None
        self._refresh()
        if self.identity()["measurement_id"] and selected and not self._gate_text and self._lookup is None:
            QTimer.singleShot(0, self, self._lookup_accepted)

    def _state_changed(self):
        if not self._loading:
            self.changed.emit()

    def _selection_changed(self, *_):
        if self._loading:
            return
        self.revision += 1
        self._gate_text = ""
        self._extra = {}
        self._lookup = None
        self._refresh()
        self.changed.emit()
        self._lookup_accepted()

    def _refresh(self):
        selected = self.selected_populations()
        self.population_summary.setText(" + ".join(selected) if selected else "Choose one or more saved phenotypes.")
        self.population_summary.setVisible(not selected or self.populations.count() > 4)
        self.review_button.setEnabled(bool(selected and self.measurement.currentData()))
        gate = _decode(self._gate_text, {})
        decisions = gate.get("decisions", ())
        if decisions:
            values = {(row.get("outcome"), row.get("threshold")) for row in decisions}
            if len(values) == 1:
                outcome, threshold = next(iter(values))
                caption = ("All cells positive" if outcome == "all_positive" else
                           "All cells negative" if outcome == "all_negative" else
                           _intensity_gate_caption(threshold) if threshold is not None else "Reviewed threshold")
            else:
                caption = "Reviewed thresholds by tissue section"
            self.gate_summary.setText(caption)
        else:
            self.gate_summary.setText("A reviewed threshold is required.")

    def _lookup_accepted(self):
        identity = self.identity()
        if not identity["measurement_id"] or not identity["phenotype_ids"] or self._gate_text:
            return
        options = {**identity, "method": "manual", "manual_threshold": None, "prefill_only": True}
        if options == self._lookup:
            return
        self._lookup = deepcopy(options)
        self.gate_summary.setText("Checking existing thresholds…")
        self.service_requested.emit("review", options, self)

    def set_result(self, kind, options, result):
        if kind != "review" or options != self._lookup or any(options.get(k) != v for k, v in self.identity().items()):
            return
        gate = result.get("accepted_prefill")
        if gate and gate.get("measurement_id") == self.measurement.currentData() and not self._gate_text:
            self._gate_text = _json(gate)
            self.changed.emit()
        self._refresh()

    def set_error(self, kind, options, message):
        if kind == "review" and options == self._lookup:
            self.gate_summary.setText("Review a threshold for this measurement.")
            self.gate_summary.setToolTip(str(message))

    def open_review(self):
        if not self.review_button.isEnabled():
            return
        identity = deepcopy(self.identity())
        binding = self._binding
        dialog = SecondaryMarkerThresholdReview(identity, _decode(self._gate_text, None), self)
        self._dialog = dialog
        dialog.service_requested.connect(self.service_requested)
        def accepted():
            if self.identity() == identity and self._binding == binding and dialog.gate is not None:
                self._gate_text = _json(dialog.gate)
                self.revision += 1
                self._refresh()
                self.changed.emit()
        dialog.accepted.connect(accepted)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.open()
        dialog.load()


class SecondaryMarkerThresholdReview(QDialog):
    service_requested = Signal(str, object, object)

    def __init__(self, identity, existing_gate=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Review positivity threshold")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(850, 700)
        self.revision = 0
        self.identity = deepcopy(identity)
        self.gate = None
        self._existing_gate = deepcopy(existing_gate)
        self._pending = {}
        self._result = None
        self._sections = []
        self._threshold = None
        self._field_open = False
        self._view_only = True
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self.load)
        layout = QVBoxLayout(self)
        scroll = QScrollArea(self)
        self._scroll = scroll
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget(scroll)
        body = QVBoxLayout(content)
        body.setSpacing(12)
        heading = QLabel(measurement_column_label(identity["measurement_id"]), content)
        heading.setProperty("panelHeading", True)
        heading.setWordWrap(True)
        body.addWidget(heading)
        population = QLabel(" + ".join(identity["phenotype_ids"]), content)
        population.setWordWrap(True)
        body.addWidget(population)
        scope_row = QHBoxLayout()
        scope_row.addWidget(QLabel("Threshold scope", content))
        self.scope = QComboBox(content)
        self.scope.addItem("Processing group", None)
        scope_row.addWidget(self.scope, 1)
        body.addLayout(scope_row)
        controls = QHBoxLayout()
        self.method = QComboBox(content)
        self.method.setPlaceholderText("Different methods by image")
        for label, key in (("Manual", "manual"), ("Triangle", "triangle"), ("Gaussian mixture", "gmm"), ("Gamma mixture", "gamma")):
            self.method.addItem(label, key)
        controls.addWidget(QLabel("Method", content))
        controls.addWidget(self.method)
        self.manual = QLineEdit(content)
        self.manual.setPlaceholderText("Enter a threshold")
        self.manual.setAccessibleName("Manual positivity threshold")
        controls.addWidget(self.manual, 1)
        body.addLayout(controls)
        self.status = QLabel(content)
        self.status.setWordWrap(True)
        body.addWidget(self.status)
        self.chart = MarkerHistogram(content)
        self.chart.range_selection_enabled = True
        body.addWidget(self.chart)
        self.chart.set_evidence({"counts": [], "bin_edges": []}, ())
        self.chart.axis_label = measurement_column_label(identity["measurement_id"]) + " · measurement units"
        action_row = QHBoxLayout()
        self.fit = QCheckBox("Show fitted components", content)
        self.fit.setVisible(False)
        self.fit.toggled.connect(self.chart.set_fit_visible)
        action_row.addWidget(self.fit)
        action_row.addStretch()
        self.inspect = QPushButton("Inspect in tissue", content)
        self.inspect.setEnabled(False)
        self.inspect.clicked.connect(self._open_field)
        action_row.addWidget(self.inspect)
        body.addLayout(action_row)
        self.field = _PopulationMarkerField(content)
        self.field.cell_view.view.setMaximumHeight(480)
        self.field.hide()
        self.field.requested.connect(self._request_field)
        self.field.range_changed.connect(self.chart.set_selected_range)
        body.addWidget(self.field)
        body.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel, self)
        self.use = self.buttons.addButton("Use threshold", QDialogButtonBox.ButtonRole.AcceptRole)
        self.use.setEnabled(False)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.scope.currentIndexChanged.connect(self._scope_changed)
        self.method.currentIndexChanged.connect(self._changed)
        self.manual.textChanged.connect(self._changed)
        self.chart.threshold_changed.connect(self._dragged)
        self.chart.range_selected.connect(self._inspect_range)
        self._show_retained_choice()

    def _changed(self, *_):
        self._view_only = False
        self._pending.clear()
        self.revision += 1
        self.gate = None
        self.use.setEnabled(False)
        self.inspect.setEnabled(False)
        self.field.setEnabled(False)
        self.manual.setVisible(self.method.currentData() == "manual")
        self._timer.start()

    def _scope_changed(self):
        self.revision += 1
        self._pending.clear()
        self._view_only = True
        self._result = None
        self.gate = None
        self.use.setEnabled(False)
        self.inspect.setEnabled(False)
        self.field.setEnabled(False)
        self._show_retained_choice()
        self.load()

    def _scoped_decisions(self):
        rows = (self._existing_gate or {}).get("decisions", [])
        image = self.scope.currentData()
        if image is None:
            return rows
        allowed = {(row["slide_id"], row["section_id"]) for row in self._sections if row["image_identity"] == image}
        return [row for row in rows if (row["slide_id"], row["section_id"]) in allowed]

    def _show_retained_choice(self):
        decisions = self._scoped_decisions()
        methods = {row["method"] for row in decisions}
        thresholds = {row.get("threshold") for row in decisions}
        with QSignalBlocker(self.method), QSignalBlocker(self.manual):
            self.method.setCurrentIndex(self.method.findData(next(iter(methods))) if len(methods) == 1 else -1 if methods else 0)
            self.manual.setText(format(next(iter(thresholds)), ".17g") if len(thresholds) == 1 and None not in thresholds else "")
        self.manual.setPlaceholderText("Different thresholds by image" if len(thresholds) > 1 else "Enter a threshold")
        self.manual.setVisible(self.method.currentData() == "manual")

    def _dragged(self, threshold):
        with QSignalBlocker(self.method):
            self.method.setCurrentIndex(self.method.findData("manual"))
        self.manual.show()
        self.manual.setText(format(threshold, ".17g"))

    def load(self):
        self._timer.stop()
        threshold = None
        if self.method.currentData() == "manual" and self.manual.text().strip():
            try:
                threshold = float(self.manual.text())
                if not isfinite(threshold):
                    raise ValueError
            except ValueError:
                self.status.setText("Enter a finite intensity threshold.")
                return
        options = {**self.identity, "method": "manual" if self._view_only else self.method.currentData(),
                   "manual_threshold": None if self._view_only else threshold, "image_identity": self.scope.currentData()}
        if self._existing_gate:
            options["existing_gate"] = deepcopy(self._existing_gate)
        self._pending["review"] = deepcopy(options)
        if not self._view_only and self.method.currentData() == "manual" and self._result is not None:
            self._manual_result(options, threshold)
            return
        self.status.setText("Loading threshold evidence…")
        self.service_requested.emit("review", options, self)

    def _manual_result(self, options, threshold):
        result = deepcopy(self._result)
        result["method"] = "manual"
        result["gate"] = None
        result["gate_complete"] = False
        counts = self.chart.threshold_counts(threshold) if threshold is not None else None
        result["review"]["manual"] = {"method": "manual", "status": "available" if counts else "unavailable",
            "threshold": threshold, "outcome": "threshold",
            "counts": {"positive": counts["above"], "negative": counts["below"]} if counts else None}
        if counts and counts["total"]:
            decisions = {(row["slide_id"], row["section_id"]): row for row in (self._existing_gate or {}).get("decisions", [])}
            image = self.scope.currentData()
            for row in result["section_choices"]:
                if image is None or row["image_identity"] == image:
                    decisions[(row["slide_id"], row["section_id"])] = {"slide_id": row["slide_id"], "section_id": row["section_id"],
                        "method": "manual", "threshold": threshold, "outcome": "threshold", "intensity_domain": "finite"}
            result["gate"] = {"schema_version": "population-marker-gate/v1", "measurement_id": self.identity["measurement_id"],
                "decisions": list(decisions.values()), "source_ref_sha256s": sorted(set(result["source_ref_sha256s"]) | set((self._existing_gate or {}).get("source_ref_sha256s", [])))}
            result["gate_complete"] = set(decisions) == {(row["slide_id"], row["section_id"]) for row in result["section_choices"]}
        self.set_result("review", options, result)

    def set_result(self, kind, options, result):
        if options != self._pending.get(kind):
            return
        if kind == "field":
            current = self.field.options()
            if current is None or any(options.get(key) != value for key, value in current.items()):
                return
            self.field.set_bundle(result)
            return
        self._result = _copy_evidence(result)
        self._sections = deepcopy(result.get("section_choices", []))
        selected_scope = self.scope.currentData()
        with QSignalBlocker(self.scope):
            self.scope.clear()
            self.scope.addItem("Processing group", None)
            for row in result.get("images", []):
                self.scope.addItem(row.get("label") or " / ".join((row["sample_id"], row["slide_id"])), row.get("image_identity") or [row[key] for key in ("sample_id", "project_entry_id", "slide_id")])
            self.scope.setCurrentIndex(max(0, self.scope.findData(selected_scope)))
        review = result["review"]
        method = self.method.currentData()
        candidates = review.get("candidates", ())
        candidate = (review.get("manual") if method == "manual" else
                     candidates.get(method) if isinstance(candidates, dict) else
                     next((row for row in candidates if row.get("method") == method), None))
        candidate = candidate or {}
        self.gate = deepcopy(result.get("retained_gate") if self._view_only else result.get("gate"))
        if self._view_only and self.gate is None:
            self.gate = deepcopy(result.get("accepted_prefill"))
        if self.gate:
            self._existing_gate = deepcopy(self.gate)
            if self._view_only:
                self._show_retained_choice()
                method = self.method.currentData()
        self._threshold = None if self._view_only else candidate.get("threshold")
        if self._threshold is None and self.gate:
            thresholds = {row.get("threshold") for row in self._scoped_decisions()}
            if len(thresholds) == 1:
                self._threshold = next(iter(thresholds))
        lines = [("Threshold", self._threshold, Qt.PenStyle.SolidLine)] if self._threshold is not None else []
        self.chart.set_candidate(None if self._view_only else candidate)
        self.chart.set_evidence(review.get("histogram", {"counts": [], "bin_edges": []}), lines)
        self.chart.set_empirical_cdf(review.get("empirical_cdf", {"values": [], "counts": []}))
        self.chart.set_manual_threshold(self._threshold)
        self.fit.setVisible(not self._view_only and bool(candidate.get("component_densities")) and method in {"gmm", "gamma"})
        self.chart.set_fit_visible(self.fit.isChecked() and method in {"gmm", "gamma"})
        count = review.get("eligible_count", 0)
        excluded = review.get("unknown_count", 0)
        counts = review.get("retained_counts") if self._view_only else candidate.get("counts")
        valid = counts["positive"] + counts["negative"] if counts else None
        if valid is not None:
            excluded = counts.get("excluded_count", count + excluded - valid)
        caption = (f'{counts["positive"]:,} of {valid:,} cells positive.' if counts else
                   f"{count:,} cells with an available measurement.")
        if excluded:
            caption += f" {excluded:,} {"cell" if excluded == 1 else "cells"} excluded."
        if not self.gate and candidate.get("reason") and not self._view_only:
            from ..presenters.marker_gates import marker_candidate_guidance
            caption += " " + marker_candidate_guidance(candidate, can_revise=False, can_use_manual=True)
        if self._view_only and self.gate:
            caption += " Reviewed thresholds retained."
        self.status.setText(caption)
        self.status.setToolTip("Excluded cells have no finite intensity measurement, or fall outside the selected method’s intensity domain." if excluded else "")
        complete = (result.get("retained_gate_complete", False) if self._view_only else result.get("gate_complete", False))
        if self._view_only and result.get("accepted_prefill") == self.gate and self.gate:
            complete = {(row["slide_id"], row["section_id"]) for row in self.gate["decisions"]} == {(row["slide_id"], row["section_id"]) for row in result.get("section_choices", [])}
        self.use.setEnabled(bool(self.gate) and complete)
        if self.gate and not complete:
            self.status.setText(caption + " Review a threshold for the remaining images.")
        self.inspect.setEnabled(self._threshold is not None)
        if self._field_open:
            self._configure_field()

    def set_error(self, kind, options, message):
        if options != self._pending.get(kind):
            return
        if kind == "field":
            self.field.set_error(str(message))
        else:
            self.gate = None
            self.use.setEnabled(False)
            self.status.setText(str(message))

    def _open_field(self):
        self._field_open = True
        self.field.show()
        self._configure_field()
        QTimer.singleShot(0, self, lambda: self._scroll.verticalScrollBar().setValue(self.field.y()))

    def _configure_field(self):
        if self._threshold is None or self._result is None:
            return
        values = self.chart._cdf_values or self.chart.histogram["bin_edges"]
        if not values:
            return
        self.field.setEnabled(True)
        self.field.configure(self.identity["measurement_id"], "intensity", self._threshold,
                             (values[0], values[-1]), [row for row in self._result.get("images", [])
                                 if self.scope.currentData() is None or row.get("image_identity") == self.scope.currentData()])

    def _request_field(self):
        field_options = self.field.options()
        if field_options is None or self.gate is None:
            return
        self.revision += 1
        options = {**field_options, **self.identity, "gate": deepcopy(self.gate)}
        self._pending["field"] = deepcopy(options)
        self.service_requested.emit("field", options, self)

    def _inspect_range(self, lower, upper):
        self._open_field()
        self.field.select_range(lower, upper)

    def accept(self):
        if self.use.isEnabled() and self.gate is not None:
            super().accept()

    def done(self, result):
        self._pending.clear()
        self.revision += 1
        self._timer.stop()
        self.field._timer.stop()
        super().done(result)
