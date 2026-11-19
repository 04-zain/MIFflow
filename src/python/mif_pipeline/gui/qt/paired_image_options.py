"""Choose a paired image while keeping the primary image and its preparation."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from mif_pipeline.orchestration.batch_artifacts import _digest
from mif_pipeline.orchestration.batch_settled_review_revision import paired_reference_from_inventory


class PairedImageOptionsEditor(QWidget):
    inspection_requested = Signal(object)

    def __init__(self, settings, sample_id, parent=None, *, mounted_images=None):
        super().__init__(parent)
        self.settings = settings
        self.sample_id = sample_id
        self._mounted = mounted_images
        self._inspected = None
        layout = QVBoxLayout(self)
        note = QLabel(
            "Review the current alignment, or choose a paired image to add or replace. "
            "Preview the affected steps before applying the change.", self)
        note.setWordWrap(True)
        layout.addWidget(note)
        self.choose = QCheckBox("Choose a paired image", self)
        layout.addWidget(self.choose)
        self.editor = QWidget(self)
        form = QFormLayout(self.editor)
        row = QHBoxLayout()
        self.path = QLineEdit(self)
        self.path.setAccessibleName("Paired image file")
        row.addWidget(self.path, 1)
        browse = QPushButton("Choose image…", self)
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        form.addRow("Paired image", row)
        self.modality = QComboBox(self)
        for label, value in (("H&E", "he"), ("Other brightfield", "brightfield"),
                             ("Fluorescence", "fluorescence"), ("Other", "other")):
            self.modality.addItem(label, value)
        form.addRow("Image type", self.modality)
        inspect = QPushButton("Inspect image", self)
        inspect.clicked.connect(self._inspect)
        form.addRow(inspect)
        self.status = QLabel("Inspect the image before previewing the affected steps.", self)
        self.status.setWordWrap(True)
        form.addRow(self.status)
        layout.addWidget(self.editor)
        self.editor.setEnabled(False)
        self.choose.toggled.connect(self.editor.setEnabled)
        self.path.textChanged.connect(self._path_changed)
        layout.addStretch(1)

    def _browse(self):
        selected, _ = QFileDialog.getOpenFileName(self, "Choose paired image", self.path.text(), "Image files (*)")
        if selected:
            self.path.setText(selected)

    def _path_changed(self):
        self._inspected = None
        self.status.setText("Inspect the image before previewing the affected steps.")

    def _current_path(self):
        text = self.path.text().strip()
        if not text:
            raise ValueError("Choose the paired image file first.")
        return (self._mounted.cluster_path(text) if self._mounted is not None
                else Path(text).expanduser().resolve(strict=False))

    def _inspect(self):
        try:
            path = self._current_path()
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self.status.setText("Inspecting the paired image…")
        self.inspection_requested.emit(path)

    def set_inventory(self, inventory):
        self._inspected = None
        if self._mounted is not None:
            path = self._current_path()
            if not isinstance(inventory, dict) or inventory.get("source_mif_path") != path:
                raise ValueError("The selected image changed. Inspect it again.")
            pair = inventory.get("paired_image_inspection")
            if not isinstance(pair, dict):
                raise ValueError("The processing host did not return a supported paired image.")
            if (pair.get("source_path") != path
                    or pair.get("canonical_uri") != PurePosixPath(path).as_uri()
                    or pair.get("source_sha256") != inventory.get("source_file_sha256")
                    or pair.get("reference_id") != self.sample_id + "-paired"
                    or pair.get("descriptor_sha256") != _digest({
                        key: value for key, value in pair.items() if key != "descriptor_sha256"})):
                raise ValueError("The paired image details differ from its inspection. Inspect it again.")
        else:
            if inventory.source_identity.resolved_path != self._current_path():
                raise ValueError("The selected image changed. Inspect it again.")
            pair = paired_reference_from_inventory(inventory,
                reference_id=self.sample_id + "-paired", modality="other").to_mapping()
        self._inspected = pair
        self.status.setText("Image checked. Preview the affected steps to continue.")

    def selected_settings(self):
        if not self.choose.isChecked():
            return self.settings
        if self._inspected is None:
            raise ValueError("Inspect the paired image before previewing this revision.")
        pair = {**self._inspected, "modality": self.modality.currentData()}
        pair.pop("descriptor_sha256")
        pair["descriptor_sha256"] = _digest(pair)
        if self._mounted is not None:
            return {**self.settings, "paired_reference": pair}
        from mif_pipeline.orchestration.batch_project import PairedMovingReferenceAuthority
        return replace(self.settings, paired_reference=PairedMovingReferenceAuthority.from_mapping(pair))
