"""Review accepted tissue choices locally before requesting a real revision."""
from __future__ import annotations

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QComboBox, QPushButton, QHBoxLayout, QStackedWidget, QScrollArea, QLineEdit, QCheckBox

from mif_pipeline.gui.tissue_setup import build_saved_tissue_setup_form, decode_tissue_setup_values
from .forms import FormRenderer
from .revision_summary import choice_changes
from .revision_review import RevisionReview


class _SavedTissueForm(FormRenderer):
    def __init__(self, parent):
        super().__init__(parent=parent)
        self.observed_source = None

    def _raw_values(self, **kwargs):
        values = super()._raw_values(**kwargs)
        if self.observed_source is not None:
            values["__observed_source"] = self.observed_source.to_mapping()
        return values


class SavedTissueEditor(QWidget):
    """The saved form is presentation, never a substitute for a pending request."""

    changed_save = Signal(str, object, object)
    preview_requested = Signal(str, object, object)
    finished = Signal()
    source_selected = Signal(str)
    authoring_requested = Signal(str)
    feature_inspection_requested = Signal(int)

    def __init__(self, options, parent=None):
        super().__init__(parent)
        self.options = options
        self.saved = options.saved_tissue
        self.setObjectName("saved-tissue-settings")
        self._busy = False
        self._preview_change = None
        self.within_node = False
        outer = QVBoxLayout(self)
        self.steps = QLabel("Step 1 of 2 · Choose changes", self)
        self.steps.setWordWrap(True)
        outer.addWidget(self.steps)
        self.pages = QStackedWidget(self)
        outer.addWidget(self.pages, 1)
        self.choices = QWidget(self.pages)
        layout = QVBoxLayout(self.choices)
        layout.setContentsMargins(0, 0, 0, 0)
        self.pages.addWidget(self.choices)
        title = QLabel("Tissue detection", self)
        title.setProperty("pageTitle", True)
        layout.addWidget(title)
        scope = QLabel(f"Sample {options.sample_id}", self)
        scope.setWordWrap(True)
        layout.addWidget(scope)
        self.notice = QLabel("", self)
        self.notice.setWordWrap(True)
        self.notice.setObjectName("saved-tissue-impact")
        layout.addWidget(self.notice)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        self.form = _SavedTissueForm(scroll)
        self.form.set_form(build_saved_tissue_setup_form(self.saved))
        self._baseline = decode_tissue_setup_values(self.form._form, "select_tissue_detector", self.form._raw_values())
        self.form.submission_ready.connect(self._setup_save)
        self.form.cancelled.connect(self.finished)
        self.form.tissue_source_selected.connect(self.source_selected)
        self.form.tissue_source_invalidated.connect(self._source_invalidated)
        self.form.tissue_authoring_requested.connect(self.authoring_requested)
        self.form.feature_inspection_requested.connect(self.feature_inspection_requested)
        scroll.setWidget(self.form)
        self._setup_scroll = scroll
        layout.addWidget(scroll, 1)
        self.resolution = QWidget(self)
        rlayout = QVBoxLayout(self.resolution)
        heading = QLabel("Saved tissue-detection resolution", self.resolution)
        heading.setProperty("panelHeading", True)
        rlayout.addWidget(heading)
        self.level = QComboBox(self.resolution)
        self.level.setObjectName("saved-tissue-native-level")
        allowed = self.saved.compatible_native_resolution_indices
        for row in self.saved.native_pyramid_inventory:
            index = row["native_resolution_index"]
            if index in allowed and (index != 0 or self.saved.parameters.allow_level0_selection):
                self.level.addItem(f"Level {index} · {row['x_um_per_px']:.3g} × {row['y_um_per_px']:.3g} µm/px", index)
        self._saved_level = None if self.saved.resolution_selection is None else self.saved.resolution_selection["native_resolution_index"]
        self.level.setCurrentIndex(self.level.findData(self._saved_level))
        self.level.setEnabled(self.level.count() > 1)
        rlayout.addWidget(self.level)
        self.level_note = QLabel("", self.resolution)
        self.level_note.setWordWrap(True)
        rlayout.addWidget(self.level_note)
        self.level.currentIndexChanged.connect(self._resolution_note)
        self._resolution_note()
        rlayout.addStretch()
        actions = QHBoxLayout()
        back = QPushButton("Back: tissue settings", self.resolution)
        back.setObjectName("saved-tissue-resolution-back")
        back.clicked.connect(self.show_setup)
        actions.addWidget(back)
        save = QPushButton("Save and next", self.resolution)
        save.setObjectName("saved-tissue-resolution-save")
        save.setProperty("actionRole", "primary")
        save.clicked.connect(self._resolution_save)
        actions.addWidget(save)
        rlayout.addLayout(actions)
        layout.addWidget(self.resolution, 1)
        self.resolution.hide()
        self.preview_button = QPushButton("Review affected results", self)
        self.preview_button.setObjectName("saved-tissue-preview")
        self.preview_button.clicked.connect(self._preview_changes)
        layout.addWidget(self.preview_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.review = RevisionReview(self.pages)
        self.pages.addWidget(self.review)
        self.summary = self.review.summary
        self.review.back.clicked.connect(self._back_to_choices)
        self.review.cancel.clicked.connect(self.finished)
        self.review.apply.clicked.connect(self._apply)
        for field in self.form._editors.values():
            if isinstance(field, QLineEdit):
                field.textChanged.connect(self._clear_impact)
            elif isinstance(field, QComboBox):
                field.currentIndexChanged.connect(self._clear_impact)
            elif isinstance(field, QCheckBox):
                field.toggled.connect(self._clear_impact)
        self.level.currentIndexChanged.connect(self._clear_impact)

    def set_within_node(self, unfinished):
        """Use ordinary continuation while the current tissue step is unfinished."""
        self.within_node = bool(unfinished)
        self.steps.setVisible(not self.within_node)
        self.preview_button.setVisible(not self.within_node)
        self._back_to_choices()

    def _back_to_choices(self):
        """Return to the retained settings without accepting the proposed change."""
        if self._busy:
            return
        self._clear_impact()
        self.pages.setCurrentWidget(self.choices)
        self.steps.setText("Step 1 of 2 · Choose changes")

    def _clear_impact(self, *_args):
        self._preview_change = None
        self.review.apply.setEnabled(False)
        self.summary.clear_preview()
        self.summary.hide()
        self.notice.clear()

    def _selected_change(self):
        """Parse the visible saved-settings page through its existing value decoder."""
        if not self.resolution.isHidden():
            level = self.level.currentData()
            if type(level) is not int or level not in self.saved.compatible_native_resolution_indices:
                raise ValueError("Choose one of the detector's available image resolutions.")
            if level == self._saved_level:
                return None
            return "tissue_resolution", "select_tissue_native_level", {"native_resolution_index": str(level)}
        raw = self.form._raw_values()
        action = "import_tissue_detector" if raw.get("detector_source") == "import" else "select_tissue_detector"
        values = decode_tissue_setup_values(self.form._form, action, raw)
        return None if values == self._baseline else ("tissue_setup", action, dict(raw))

    def _preview_changes(self):
        """Request consequences for changed choices without publishing the revision."""
        if self._busy:
            return
        self._clear_impact()
        try:
            selected = self._selected_change()
        except (TypeError, ValueError) as error:
            self.show_error(str(error))
            return
        if selected is None:
            self.notice.setText("These choices match the saved tissue settings.")
            return
        self.preview_requested.emit(*selected)

    def _apply(self):
        """Submit the reviewed draft through the existing changed-save operation."""
        if self._busy or self._preview_change is None:
            return
        try:
            selected = self._selected_change()
        except (TypeError, ValueError) as error:
            self.show_error(str(error))
            return
        if selected != self._preview_change:
            self._back_to_choices()
            self.show_error("The choices changed. Review the affected results again.")
            return
        self.set_busy(True)
        self.changed_save.emit(*selected)

    def _save_changes(self):
        if not self.within_node:
            self._preview_changes()
            return
        if self._busy:
            return
        self._clear_impact()
        try:
            selected = self._selected_change()
        except (TypeError, ValueError) as error:
            self.show_error(str(error))
            return
        if selected is not None:
            # The navigation owner still checks the exact revision preview.
            self.changed_save.emit(*selected)

    def _source_invalidated(self):
        self.form.observed_source = None

    def _setup_save(self, action, values):
        if self._busy:
            return
        if values == self._baseline:
            if (self.saved.resolution_selection is not None
                    and not (self.level.count() == 1 and self.level.currentData() == self._saved_level)):
                self.show_resolution()
            else:
                self.finished.emit()
            return
        self._save_changes()

    def _resolution_note(self):
        self.level_note.setText(
            "This is the saved detector's only compatible image resolution. " * (self.level.count() == 1)
            + ("Full resolution retains the finest detail and increases memory use and processing time."
               if self.level.currentData() == 0 else ""))

    def _resolution_save(self):
        if self._busy:
            return
        level = self.level.currentData()
        if type(level) is not int or level not in self.saved.compatible_native_resolution_indices:
            self.show_error("Choose one of the detector's available image resolutions.")
            return
        if level == self._saved_level:
            self.finished.emit()
        else:
            self._save_changes()

    def show_setup(self):
        self.resolution.hide()
        self._setup_scroll.show()
        self.form._set_tissue_step(2)
        self._clear_impact()

    def show_resolution(self):
        self._setup_scroll.hide()
        self.resolution.show()
        self._clear_impact()

    def set_busy(self, busy):
        self._busy = busy
        self.preview_button.setEnabled(not busy)
        self._setup_scroll.setEnabled(not busy)
        self.resolution.setEnabled(not busy)
        self.review.back.setEnabled(not busy)
        self.review.cancel.setEnabled(not busy)
        self.review.apply.setEnabled(not busy and self._preview_change is not None)

    def show_error(self, message):
        self.set_busy(False)
        self._preview_change = None
        self.review.apply.setEnabled(False)
        self.notice.setText(str(message))
        self.review.set_notice(str(message))

    def show_impact(self, preview, workflow_nodes):
        # The exact preview found effects that require the completed-work review.
        self.set_within_node(False)
        if not self.resolution.isHidden():
            current = self.level.itemText(self.level.findData(self._saved_level))
            proposed = self.level.currentText()
        else:
            raw = self.form._raw_values()
            action = "import_tissue_detector" if raw.get("detector_source") == "import" else "select_tissue_detector"
            values = decode_tissue_setup_values(self.form._form, action, raw)
            current, proposed = choice_changes(self._baseline, values, {
                "minimum_object_area_um2": "Smallest tissue region (µm²)",
                "classifier_name": "Tissue detector", "reference_channel_id": "Detection channel",
                "native_resolution_index": "Image level", "resolution_um_per_px": "Image detail (µm/px)"})
        self.review.show_preview(preview, workflow_nodes, current or "Saved tissue detector settings.", proposed or "Use the selected tissue detector settings.")
        self._preview_change = None if preview.blocked_reason else self._selected_change()
        self.review.apply.setEnabled(self._preview_change is not None)
        self.review.set_notice(preview.blocked_reason or "")
        self.pages.setCurrentWidget(self.review)
        self.steps.setText("Step 2 of 2 · Review affected results")
