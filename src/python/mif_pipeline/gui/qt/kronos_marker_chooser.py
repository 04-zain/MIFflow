"""Map selected image channels through one shared, inline KRONOS marker browser.

Each visible assignment retains the form's marker-name string. The browser uses
its existing model vocabulary and changes only the active assignment after an
explicit action; normalization and additional-marker metadata stay in the form.
"""
from __future__ import annotations

from collections.abc import Mapping

from PySide6.QtCore import Qt, Signal, QSignalBlocker
from PySide6.QtWidgets import (
    QBoxLayout, QButtonGroup, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
    QComboBox, QGridLayout,
)

from mif_pipeline.compartment_analysis.kronos_marker_preparation import marker_key


class KronosAdditionalMarkerDetails(QWidget):
    """Organize existing metadata editors without replacing their values."""

    def __init__(self, channel, parent=None):
        super().__init__(parent)
        self.channel = channel
        body = QVBoxLayout(self)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(8)
        heading = QLabel("Marker description", self)
        heading.setProperty("panelHeading", True)
        heading.setWordWrap(True)
        body.addWidget(heading)
        self.grid = QGridLayout()
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(20)
        self.grid.setVerticalSpacing(8)
        self.grid.setColumnStretch(0, 1)
        self.grid.setColumnStretch(1, 1)
        body.addLayout(self.grid)

    def prepare_control(self, name, caption, field):
        suffix = name.split("_additional_", 1)[1]
        labels = {"marker_full_name": "Full marker name", "compartment": "Subcellular location",
                  "family": "Marker family"}
        caption.setText(labels.get(suffix, caption.text().split(": ", 1)[-1].capitalize()))
        editor = caption.buddy()
        explanation = field.findChild(QLabel, "form-help-" + name)
        if explanation is not None:
            guidance = explanation.text()
            caption.setToolTip(guidance)
            if editor is not None:
                editor.setToolTip(guidance)
                editor.setAccessibleDescription(guidance)
            explanation.hide()
        if editor is not None:
            editor.setAccessibleName(self.channel + ": " + caption.text())
        if isinstance(editor, QLineEdit):
            placeholders = {"marker_full_name": "Full protein or marker name"}
            if suffix in placeholders:
                editor.setPlaceholderText(placeholders[suffix])
        elif isinstance(editor, QComboBox) and editor.count() and editor.itemData(0) is None:
            editor.setItemText(0, "Choose a location…" if suffix == "compartment" else "Choose a family…")


class KronosMarkerAssignment(QPushButton):
    """Display and retain one channel's exact marker string as its form editor."""

    changed = Signal()

    def __init__(self, value: str | None, parent=None):
        super().__init__(parent)
        self._value = ""
        self.setCheckable(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(
            "QPushButton { text-align: left; padding: 9px 12px; }"
            "QPushButton:checked { background: #006b70; border: 2px solid #006b70; color: white; }"
        )
        self.set_value(value)

    def value(self) -> str:
        """Return the exact marker name supplied to the existing form decoder."""
        return self._value

    def set_value(self, value: str | None) -> None:
        """Restore or explicitly assign a string without remapping its identity."""
        marker = "" if value is None else str(value)
        changed = marker != self._value
        self._value = marker
        self.setText(marker or "Not assigned")
        if changed:
            self.changed.emit()


class KronosMarkerMapping(QWidget):
    """Keep channel assignments beside a single searchable model vocabulary."""

    assignment_selected = Signal(object)

    def __init__(self, vocabulary: Mapping, parent=None):
        super().__init__(parent)
        self.setObjectName("kronos-marker-mapping")
        self._markers = vocabulary.get("markers", {})
        self._assignments = []
        self._active = None
        self._active_value = None
        self._adding = False
        self._details_available = False
        self._name_drafts = {}
        self._stacked = False
        self.columns = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self.columns.setContentsMargins(0, 0, 0, 0)
        self.columns.setSpacing(20)

        left = QWidget(self)
        self.assignment_panel = left
        assignments = QVBoxLayout(left)
        assignments.setContentsMargins(0, 0, 0, 0)
        assignments.setSpacing(8)
        heading = QLabel("Channel assignments", left)
        heading.setProperty("panelHeading", True)
        assignments.addWidget(heading)
        self.assignment_scroll = QScrollArea(left)
        self.assignment_scroll.setObjectName("kronos-channel-assignments")
        self.assignment_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.assignment_scroll.setWidgetResizable(True)
        self.assignment_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.assignment_scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        self.assignment_content = QWidget(self.assignment_scroll)
        self.assignment_rows = QVBoxLayout(self.assignment_content)
        self.assignment_rows.setContentsMargins(0, 0, 6, 0)
        self.assignment_rows.setSpacing(8)
        self.assignment_rows.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.assignment_scroll.setWidget(self.assignment_content)
        assignments.addWidget(self.assignment_scroll)
        assignments.addStretch(1)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.group.buttonClicked.connect(lambda button: self.select_assignment(button, focus=True))
        self.columns.addWidget(left, 2)

        right = QWidget(self)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)
        self.heading = QLabel("Model markers", right)
        self.heading.setProperty("panelHeading", True)
        mode_row = QHBoxLayout()
        mode_row.setContentsMargins(0, 0, 0, 0)
        mode_row.setSpacing(10)
        mode_row.addWidget(self.heading)
        mode_row.addStretch()
        self.channel_picker = QComboBox(right)
        self.channel_picker.setObjectName("kronos-details-channel")
        self.channel_picker.setAccessibleName("Channel for the new marker")
        self.channel_picker.setToolTip("Choose the image channel whose marker is being edited.")
        self.channel_picker.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.channel_picker.setMinimumContentsLength(12)
        self.channel_picker.setMaximumWidth(220)
        self.channel_picker.currentIndexChanged.connect(self._pick_channel)
        self.channel_picker.hide()
        mode_row.addWidget(self.channel_picker)
        self.back = QPushButton("Back to model markers", right)
        self.back.setObjectName("kronos-marker-back")
        self.back.clicked.connect(self._show_choices)
        self.back.hide()
        mode_row.addWidget(self.back)
        right_layout.addLayout(mode_row)
        self.browser_content = QWidget(right)
        browser = QVBoxLayout(self.browser_content)
        browser.setContentsMargins(0, 0, 0, 0)
        browser.setSpacing(8)
        browser.setAlignment(Qt.AlignmentFlag.AlignTop)
        right_layout.addWidget(self.browser_content)
        self.detail_area = QWidget(right)
        self.detail_layout = QVBoxLayout(self.detail_area)
        self.detail_layout.setContentsMargins(0, 0, 0, 0)
        self.detail_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        right_layout.addWidget(self.detail_area)
        self.detail_area.hide()
        self.target = QLabel(right)
        self.target.setObjectName("kronos-marker-target")
        self.target.setProperty("muted", True)
        self.target.setWordWrap(True)
        browser.addWidget(self.target)
        self.setup_message = QLabel(
            "Select a KRONOS model folder in Software setup, then reopen this form to load its marker list.", right)
        self.setup_message.setObjectName("kronos-marker-setup-message")
        self.setup_message.setWordWrap(True)
        browser.addWidget(self.setup_message)

        self.choices = QWidget(right)
        choices = QVBoxLayout(self.choices)
        choices.setContentsMargins(0, 0, 0, 0)
        choices.setSpacing(8)
        self.search = QLineEdit(self.choices)
        self.search.setObjectName("kronos-marker-search")
        self.search.setAccessibleName("Search model markers")
        self.search.setPlaceholderText("Search model markers")
        self.search.setClearButtonEnabled(True)
        choices.addWidget(self.search)
        self.listing = QListWidget(self.choices)
        self.listing.setObjectName("kronos-marker-list")
        self.listing.setAccessibleName("Markers in the selected KRONOS model")
        self.listing.setStyleSheet("QListWidget::item { padding: 6px 8px; }")
        choices.addWidget(self.listing)
        self.match_count = QLabel(self.choices)
        self.match_count.setProperty("muted", True)
        choices.addWidget(self.match_count)
        browser.addWidget(self.choices)

        self.additional = QWidget(right)
        additional = QVBoxLayout(self.additional)
        additional.setContentsMargins(0, 0, 0, 0)
        additional.setSpacing(10)
        label = QLabel("Marker name", self.additional)
        self.new_name = QLineEdit(self.additional)
        self.new_name.setObjectName("kronos-marker-add-name")
        self.new_name.setAccessibleName("Name of marker not listed")
        label.setBuddy(self.new_name)
        additional.addWidget(label)
        additional.addWidget(self.new_name)
        self.new_message = QLabel(self.additional)
        self.new_message.setWordWrap(True)
        self.new_message.setProperty("validationMessage", True)
        additional.addWidget(self.new_message)
        browser.addWidget(self.additional)

        self.actions = QWidget(right)
        actions = QHBoxLayout(self.actions)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        self.choose = QPushButton("Use marker", self.actions)
        self.choose.setObjectName("kronos-marker-select")
        self.choose.setProperty("actionRole", "primary")
        self.choose.clicked.connect(self._apply_choice)
        actions.addWidget(self.choose)
        self.add = QPushButton("Add a marker not listed…", self.actions)
        self.add.setObjectName("kronos-marker-add")
        self.add.clicked.connect(self._show_additional)
        actions.addWidget(self.add)
        actions.addStretch(1)
        right_layout.addWidget(self.actions)
        self.columns.addWidget(right, 3)
        self.additional.hide()
        available = bool(self._markers)
        self.setup_message.setVisible(not available)
        self.choices.setVisible(available)
        self.actions.setVisible(available)
        self.search.textChanged.connect(self._filter)
        self.new_name.textChanged.connect(self._check_new_name)
        self.listing.currentRowChanged.connect(self._selection_changed)
        self.listing.itemDoubleClicked.connect(lambda _item: self._apply_choice())
        self.search.returnPressed.connect(self._apply_choice)
        self._filter()

    def _update_context(self):
        self.detail_area.setVisible(self._adding and self._details_available)
        self.target.setVisible(not self._adding)
        compact = self._stacked and self._adding
        self.assignment_panel.setVisible(not compact)
        self.channel_picker.setVisible(compact)
        self.heading.setText("Add a marker" if self._adding else "Model markers")
        if self._adding and self._active is not None and self._active.value() == self.new_name.text():
            self.heading.setText("Marker details")
        self._sync_channel_picker()

    def _sync_channel_picker(self):
        with QSignalBlocker(self.channel_picker):
            self.channel_picker.clear()
            selected = -1
            for index, (caption, field, editor) in enumerate(self._assignments):
                if field.isHidden():
                    continue
                label = caption.text()
                if editor.value() and editor.value() != label:
                    label += " → " + editor.value()
                self.channel_picker.addItem(label, index)
                if editor is self._active:
                    selected = self.channel_picker.count() - 1
            self.channel_picker.setCurrentIndex(selected)

    def _pick_channel(self, index):
        if index >= 0:
            self.select_assignment(self._assignments[self.channel_picker.itemData(index)][2])

    def set_details_available(self, available):
        self._details_available = available and bool(self._markers)
        self._update_context()

    def add_assignment(self, caption, field, editor: KronosMarkerAssignment) -> None:
        """Place the existing caption and form editor in the channel column."""
        self._assignments.append((caption, field, editor))
        self.assignment_rows.addWidget(caption)
        self.assignment_rows.addWidget(field)
        self.group.addButton(editor)

    def sync_assignments(self) -> None:
        """Follow the form's channel visibility and restored exact string values."""
        visible = [item for item in self._assignments if not item[1].isHidden()]
        self.setVisible(bool(visible))
        if not visible:
            self._active = None
            self._active_value = None
            return
        available = [item[2] for item in visible]
        if self._active not in available:
            self.select_assignment(available[0])
        elif self._active.value() != self._active_value:
            self.select_assignment(self._active)
        self.assignment_rows.activate()
        height = self.assignment_rows.sizeHint().height() + 4
        self.assignment_scroll.setFixedHeight(min(300, max(64, height)))
        self._sync_channel_picker()

    def select_assignment(self, editor: KronosMarkerAssignment, *, focus=False) -> None:
        """Target one visible channel without changing any marker assignment."""
        caption, field, _editor = next(item for item in self._assignments if item[2] is editor)
        if field.isHidden():
            return
        if self._active is not editor:
            self._show_choices()
        self._active = editor
        self._active_value = editor.value()
        editor.setChecked(True)
        self.target.setText("Choose the marker for " + caption.text())
        self._sync_channel_picker()
        self._show_choices()
        self.search.clear()
        self._filter()
        self.assignment_selected.emit(editor)
        if self._markers and editor.value() and marker_key(editor.value()) not in self._markers:
            self._show_additional()
        if focus and self._markers and not self._adding:
            self.search.setFocus()
            self.search.selectAll()

    def _filter(self, _text=None):
        key = marker_key(self.search.text().strip())
        self.listing.clear()
        names = sorted(row[0] for candidate, row in self._markers.items() if key in candidate)
        self.listing.addItems(names)
        preferred = key or (marker_key(self._active.value()) if self._active is not None else "")
        exact = next((index for index, name in enumerate(names) if marker_key(name) == preferred), None)
        if exact is not None or (key and names):
            self.listing.setCurrentRow(exact if exact is not None else 0)
            self.listing.scrollToItem(self.listing.currentItem())
        self.match_count.setText(f"{len(names)} of {len(self._markers)} markers" if names else "No listed marker matches this search.")
        row_height = max(30, self.listing.sizeHintForRow(0))
        self.listing.setFixedHeight(min(244, max(120, row_height * min(len(names), 7) + 4)))
        self._selection_changed()

    def _selection_changed(self, _row=None):
        if not self._adding:
            self.choose.setEnabled(self._active is not None and self.listing.currentItem() is not None)

    def _show_additional(self):
        if self._active is None or not self._markers:
            return
        self._adding = True
        self.back.show()
        self.choices.hide()
        self.additional.show()
        self.add.hide()
        existing = self._active.value()
        unknown = bool(existing) and marker_key(existing) not in self._markers
        self.new_name.setText(self._name_drafts.get(self._active, existing if unknown else self.search.text().strip()))
        self._active.setProperty("creatingAdditionalMarker", True)
        # Re-evaluate the existing conditional form controls without assigning a
        # speculative marker name or replacing any metadata editors.
        self._active.changed.emit()
        self._check_new_name()
        self._update_context()
        self.new_name.setFocus()
        self.new_name.selectAll()

    def _show_choices(self):
        if self._adding and self._active is not None:
            self._name_drafts[self._active] = self.new_name.text()
        self._adding = False
        self.additional.hide()
        self.back.hide()
        self.choices.setVisible(bool(self._markers))
        self.add.show()
        self.choose.setText("Use marker")
        self.choose.show()
        if self._active is not None and self._active.property("creatingAdditionalMarker"):
            self._active.setProperty("creatingAdditionalMarker", False)
            self._active.changed.emit()
        self._selection_changed()
        self._update_context()

    def _check_new_name(self, _text=None):
        value = self.new_name.text().strip()
        known = marker_key(value) in self._markers
        unchanged = self._active is not None and value == self._active.value()
        self.choose.setText("Update marker" if self._active is not None and marker_key(self._active.value()) not in self._markers and self._active.value() else "Add marker")
        self.choose.setVisible(not unchanged)
        self.choose.setEnabled(self._active is not None and bool(marker_key(value)) and not known and "\0" not in value)
        if self._adding and self._active is not None:
            self._name_drafts[self._active] = self.new_name.text()
        self.new_message.setText("This marker is already listed. Choose it from the model list." if known else "")
        self.new_message.setVisible(known)

    def _apply_choice(self):
        if not self.choose.isEnabled() or self._active is None:
            return
        additional = self._adding
        marker = self.new_name.text().strip() if additional else self.listing.currentItem().text()
        self._active.set_value(marker)
        self.select_assignment(self._active)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        stacked = self.width() < 720
        if stacked != self._stacked:
            self._stacked = stacked
            self.columns.setDirection(QBoxLayout.Direction.TopToBottom if stacked else QBoxLayout.Direction.LeftToRight)
            self._update_context()
