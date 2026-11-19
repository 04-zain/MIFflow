"""Canonical prototype About dialog.

The widget consumes the current presentation model and emits researcher actions through its
controller. Expensive native or scientific operations use managed jobs, while the Qt thread
retains the selected form and its response identity."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from pathlib import Path
import sys

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..prototype_notice import PrototypeNotice, trusted_local_prototype_notice

# ==============================================================================
#                                     About
# ==============================================================================


def _document_path(name: str) -> Path:
    if name not in {"LICENSE", "THIRD_PARTY_NOTICES.txt"}:
        raise ValueError("unsupported legal document")
    if getattr(sys, "frozen", False):
        return Path(sys.executable).absolute().parent / name
    repository = Path(__file__).resolve().parents[4]
    return (
        repository / "LICENSE" if name == "LICENSE" else repository / "packaging" / "notices" / name
    )


class AboutDialog(QDialog):
    """Show application identity and access to bundled legal documents."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        notice: PrototypeNotice | None = None,
    ) -> None:
        super().__init__(parent)
        self.notice = notice or trusted_local_prototype_notice()
        self._legal_viewer: QDialog | None = None
        self.setWindowTitle("About mIF Pipeline")
        layout = QVBoxLayout(self)
        title = QLabel("mIF Pipeline", self)
        layout.addWidget(title)
        classification = QLabel("Prototype", self)
        classification.setObjectName("prototype-classification")
        layout.addWidget(classification)
        legal = QGroupBox("Licenses and notices", self)
        legal_layout = QVBoxLayout(legal)
        copyright_label = QLabel("Copyright © 2026 Zain S. Ali (Queen's University)", legal)
        copyright_label.setObjectName("project-copyright")
        legal_layout.addWidget(copyright_label)
        license_summary = QLabel(
            "Original project code is GPL-3.0-or-later. You may copy, modify, "
            "and redistribute it under that license. It comes with no warranty.",
            legal,
        )
        license_summary.setObjectName("project-license-summary")
        license_summary.setWordWrap(True)
        legal_layout.addWidget(license_summary)
        license_button = QPushButton("View project license", legal)
        license_button.setObjectName("view-project-license")
        license_button.clicked.connect(lambda: self._show_document("LICENSE"))
        legal_layout.addWidget(license_button)
        third_party_summary = QLabel(
            "Qt/PySide6 and other third-party components retain separate licenses "
            "and copyright. See the bundled third-party notices.",
            legal,
        )
        third_party_summary.setObjectName("third-party-license-summary")
        third_party_summary.setWordWrap(True)
        legal_layout.addWidget(third_party_summary)
        notices_button = QPushButton("View third-party notices", legal)
        notices_button.setObjectName("view-third-party-notices")
        notices_button.clicked.connect(lambda: self._show_document("THIRD_PARTY_NOTICES.txt"))
        legal_layout.addWidget(notices_button)
        layout.addWidget(legal)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _show_document(self, name: str) -> None:
        path = _document_path(name)
        try:
            contents = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            contents = f"The bundled legal document could not be opened: {path}\n{exc}"
        if self._legal_viewer is None:
            viewer = QDialog(self)
            viewer.setObjectName("legal-document")
            viewer.resize(760, 600)
            viewer_layout = QVBoxLayout(viewer)
            editor = QPlainTextEdit(viewer)
            editor.setObjectName("legal-document-text")
            editor.setReadOnly(True)
            viewer_layout.addWidget(editor)
            close_button = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, viewer)
            close_button.rejected.connect(viewer.reject)
            viewer_layout.addWidget(close_button)
            self._legal_viewer = viewer
        self._legal_viewer.setWindowTitle(name)
        editor = self._legal_viewer.findChild(QPlainTextEdit, "legal-document-text")
        assert editor is not None
        editor.setPlainText(contents)
        self._legal_viewer.show()


__all__ = ["AboutDialog"]
