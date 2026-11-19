"""Compact view of the current recipe edited in the shared native feature window."""
import math
from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import QMenu, QFrame, QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget
from .researcher_widgets import ContentSizedButton
from mif_pipeline.gui.pixel_image_features import FILTER_LABELS, image_feature_summary


class PixelFeatureSummary(QWidget):
    inspect_requested = Signal()

    def __init__(self, parent=None, *, training=False):
        super().__init__(parent)
        self.setObjectName("pixel-feature-summary")
        self.training = training
        self._rendered_rows = None
        self._channel_count = None
        self._feature_count = 0
        self._scale_count = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        title = QLabel("Image features", self)
        title.setProperty("panelHeading", True)
        heading = QHBoxLayout()
        heading.addWidget(title)
        heading.addStretch()
        layout.addLayout(heading)
        self.inspect = ContentSizedButton("Choose features and preview…", self)
        self.inspect.setObjectName("inspect-pixel-features")
        self.preview_entry = None
        self.inspect.clicked.connect(self.inspect_requested)
        self.summary = QLabel(self)
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.summary.setProperty("choiceHelp", True)
        if training:
            heading.addWidget(self.summary)
        else:
            layout.addWidget(self.summary)
        self.features = QFrame(self)
        self.features.setObjectName("selected-image-features")
        self.rows = QGridLayout(self.features)
        self.rows.setContentsMargins(12, 10, 12, 10)
        self.rows.setHorizontalSpacing(24)
        self.rows.setVerticalSpacing(10)
        self.rows.setColumnStretch(1, 1)
        layout.addWidget(self.features, 0, Qt.AlignmentFlag.AlignTop if training else Qt.AlignmentFlag(0))
        if training:
            self.rows.setContentsMargins(0, 4, 0, 10)
            self.rows.setColumnStretch(0, 3)
            self.rows.setColumnStretch(1, 2)
        layout.addWidget(self.inspect, 0, Qt.AlignmentFlag.AlignLeft)

    def set_preview_images(self, entries, participants):
        if len(entries) < 2:
            return
        self.inspect.clicked.disconnect(self.inspect_requested)
        menu = QMenu(self.inspect)
        menu.setTitle("Choose an image to preview")
        for entry in entries:
            row = participants[entry]
            sample = row.get("sample_id") or row.get("slide_id") or entry
            subject = row.get("subject_id")
            action = menu.addAction(f"{subject} · {sample}" if subject and subject != sample else sample)
            action.setData(entry)
        menu.triggered.connect(self._inspect_entry)
        self.inspect.setMenu(menu)

    def _inspect_entry(self, action):
        self.preview_entry = action.data()
        self.inspect_requested.emit()

    def set_channel_count(self, count):
        self._channel_count = count
        self._update_summary()

    def _update_summary(self):
        count = self._feature_count
        if self._channel_count is None:
            scales = self._scale_count
            text = (f"{count} feature{'s' if count != 1 else ''} per channel"
                    f" · {scales} filter scale{'s' if scales != 1 else ''}")
        else:
            channels = self._channel_count
            total = count * channels
            text = (f"{total} feature{'s' if total != 1 else ''}"
                    f" · {channels} channel{'s' if channels != 1 else ''}"
                    f" × {count} feature{'s' if count != 1 else ''} per channel")
        if self.training and self._channel_count is not None:
            text = f"{total} {'feature' if total == 1 else 'features'} per pixel"
        self.summary.setText(text)
        self.summary.setVisible(not self.training or self._channel_count != 1 or count > 1)

    def set_recipe(self, recipe, sampling=None):
        self._feature_count = int(recipe["include_intensity"]) + sum(len(row["filters"]) for row in recipe["scales"])
        self._scale_count = sum(bool(row["filters"]) for row in recipe["scales"])
        self._update_summary()
        self.summary.setToolTip(image_feature_summary(recipe))
        if self.training:
            self._training_recipe(recipe, sampling)
            return
        while self.rows.count():
            old = self.rows.takeAt(0).widget()
            old.hide()
            old.deleteLater()
        rows = [("Unfiltered", "Channel intensity")] if recipe["include_intensity"] else []
        rows.extend((f"σ {row['sigma_um']:.6g} µm", ", ".join(FILTER_LABELS[key] for key in row["filters"]))
                    for row in recipe["scales"] if row["filters"])
        for index, (scale, names) in enumerate(rows):
            label = QLabel(scale, self.features)
            label.setProperty("panelHeading", True)
            label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            description = QLabel(names, self.features)
            description.setTextFormat(Qt.TextFormat.PlainText)
            description.setWordWrap(True)
            self.rows.addWidget(label, index, 0)
            self.rows.addWidget(description, index, 1)
        self.features.setVisible(bool(rows))

    def _training_recipe(self, recipe, sampling):
        try:
            resolution = float(sampling)
        except (ValueError, TypeError):
            resolution = 0
        rows = [("Channel intensity", "Unfiltered")] if recipe["include_intensity"] else []
        for group in recipe["scales"]:
            if not group["filters"]:
                continue
            sigma = group["sigma_um"]
            scale = f"{sigma:.6g} µm"
            if resolution > 0 and math.isfinite(resolution):
                scale += f"  ({sigma / resolution:.4g} px)"
            rows.append((", ".join(FILTER_LABELS[key] for key in group["filters"]), scale))
        if rows == self._rendered_rows:
            return
        self._rendered_rows = rows
        while self.rows.count():
            previous = self.rows.takeAt(0).widget()
            previous.hide()
            previous.deleteLater()
        for row, values in enumerate([("Input", "Filter scale (σ)"), *rows]):
            for column, text in enumerate(values):
                label = QLabel(text, self.features)
                label.setTextFormat(Qt.TextFormat.PlainText)
                label.setWordWrap(True)
                label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
                if row == 0:
                    label.setProperty("stageFieldCaption", True)
                self.rows.addWidget(label, row + 1 if row else 0, column)
        rule = QFrame(self.features)
        rule.setFixedHeight(1)
        rule.setStyleSheet("background:#c8d7d6")
        self.rows.addWidget(rule, 1, 0, 1, 2)
        self.features.setVisible(bool(rows))
