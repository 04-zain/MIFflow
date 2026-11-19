"""Select intensity measurements for one cellular region without repeated rows."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QGridLayout, QWidget

from mif_pipeline.gui.measurement_setup import intensity_measurement_choices


class IntensityMeasurements(QWidget):
    """Keep the six measurement choices visible and preserve required selections.

    Setup and revision pages use the same ordering and readable labels. Required
    measurements remain checked; the other measurements can be changed without
    changing the channel's cellular region.
    """

    changed = Signal()

    def __init__(self, selected=("Mean",), *, required=(), compact=False, parent=None):
        super().__init__(parent)
        self._required = frozenset(required)
        self._checks = {}
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(20 if not compact else 12)
        layout.setVerticalSpacing(8 if not compact else 4)
        for index, (value, label) in enumerate(intensity_measurement_choices()):
            check = QCheckBox(label, self)
            check.setObjectName("intensity-measurement-" + value)
            check.setEnabled(value not in self._required)
            if value in self._required:
                check.setToolTip("Required by the current marker rules or clustering.")
            check.toggled.connect(self.changed)
            layout.addWidget(check, index // 2, index % 2)
            self._checks[value] = check
        self.setFocusProxy(next(iter(self._checks.values())))
        self.set_value(selected)

    def value(self) -> list[str]:
        """Return checked statistics in the shared measurement order."""
        return [value for value, check in self._checks.items() if check.isChecked()]

    def set_value(self, selected) -> None:
        """Restore this page's selections while retaining required measurements."""
        selected = set(selected or ()) | self._required
        for value, check in self._checks.items():
            check.setChecked(value in selected)
