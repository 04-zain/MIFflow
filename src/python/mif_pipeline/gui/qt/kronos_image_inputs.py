"""Patch geometry, encoder channels and the cell-field inspector."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout,
    QWidget, QWidgetItem,
)


class KronosImageInputs(QWidget):
    """Arrange the form's original editors around the field they describe."""

    def __init__(self, coverage, horizontal, vertical, *, native_sampling=None,
                 native_group=False, original_resolution=None, parent=None):
        super().__init__(parent)
        self.setObjectName('kronos-image-inputs')
        self._x, self._y = horizontal, vertical
        self.native_sampling = tuple(native_sampling) if native_sampling else None
        self.native_reset = None
        self.patch_choices = None
        self.grid = QVBoxLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(16)
        self.dimensions = QFrame(self)
        self.dimensions.setObjectName('kronos-image-dimensions')
        self.dimensions.setStyleSheet(
            'QFrame#kronos-image-dimensions { background: #f3f5f5; border-radius: 6px; }'
            'QLabel { background: transparent; }'
            'QPushButton[patchChoice="true"] { background: white; border: 1px solid #bdcccc; '
            'border-radius: 2px; padding: 6px 8px; }'
            'QPushButton[patchChoice="true"]:checked { background: #006b70; color: white; border-color: #006b70; }'
            'QPushButton[patchChoice="true"]:focus { border: 2px solid #173d47; }'
            'QPushButton#kronos-use-native-resolution { background: transparent; border: none; '
            'color: #006b70; padding: 1px 0; text-align: left; }'
            'QPushButton#kronos-use-native-resolution:focus { border: 1px dotted #006b70; }'
        )
        self.grid.addWidget(self.dimensions)
        self.band = QGridLayout(self.dimensions)
        self.band.setContentsMargins(16, 14, 16, 14)
        self.band.setHorizontalSpacing(22)
        self.band.setVerticalSpacing(12)
        self.patch = QWidget(self.dimensions)
        self.patch_fields = QVBoxLayout(self.patch)
        self.patch_fields.setContentsMargins(0, 0, 0, 0)
        self.patch_fields.setSpacing(5)
        self.sampling = QWidget(self.dimensions)
        sampling = QVBoxLayout(self.sampling)
        sampling.setContentsMargins(0, 0, 0, 0)
        sampling.setSpacing(5)
        self.sampling_caption = self._label('Sampling resolution (µm/pixel)', 'stageFieldCaption')
        self.sampling_caption.setToolTip(
            'Physical width and height represented by each patch pixel. '
            'Larger values cover more tissue at the same patch size, with less spatial detail.'
        )
        self.sampling_caption.setBuddy(horizontal)
        sampling.addWidget(self.sampling_caption)
        self._axis_fields = {}
        self.axes = QGridLayout()
        self.axes.setContentsMargins(0, 0, 0, 0)
        self.axes.setSpacing(7)
        sampling.addLayout(self.axes)
        if native_sampling is not None and len(native_sampling) == 2:
            x, y = native_sampling
            size = f'{x:.5g}' if x == y else f'{x:.5g} × {y:.5g}'
            source = 'Default for these images' if native_group else 'Original image resolution'
            self.native = self._label(f'{source}  ·  {size} µm/pixel', 'fieldHelp')
            self.native.setToolTip(f'{x} × {y} µm/pixel')
            sampling.addWidget(self.native)
            action = 'Use default resolution' if native_group else 'Use image resolution'
            self.native_reset = QPushButton(action, self.sampling)
            self.native_reset.setObjectName('kronos-use-native-resolution')
            self.native_reset.clicked.connect(self._restore_native)
            sampling.addWidget(self.native_reset, alignment=Qt.AlignmentFlag.AlignLeft)
            horizontal.textChanged.connect(self._update_native_action)
            if vertical is not horizontal:
                vertical.textChanged.connect(self._update_native_action)
            self._update_native_action()
        elif original_resolution is not None:
            text, tooltip = original_resolution
            self.native = self._label(text, 'fieldHelp')
            self.native.setToolTip(tooltip)
            sampling.addWidget(self.native)
        self.output = QWidget(self.dimensions)
        self.output_layout = QGridLayout(self.output)
        self.output_layout.setContentsMargins(0, 0, 0, 0)
        self.output_layout.setHorizontalSpacing(12)
        self.output_layout.setVerticalSpacing(5)
        self.field_caption = self._label('Physical field', 'stageFieldCaption')
        self.field_help = self._label('Width × height around the nucleus' if horizontal is not vertical
                                      else 'Width × height in tissue', 'fieldHelp')
        self.coverage = coverage
        coverage.setProperty('fieldHelp', False)
        coverage.setProperty('panelHeading', True)
        coverage.setContentsMargins(0, 0, 0, 0)
        coverage.setMinimumHeight(horizontal.sizeHint().height())
        coverage.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        self.channels = QWidget(self)
        channels = QVBoxLayout(self.channels)
        channels.setContentsMargins(0, 0, 0, 0)
        channels.setSpacing(0)
        self.grid.addWidget(self.channels)
        self.preview = None
        self._has_channels = False
        self._arrangement = None
        self._arrange(self.width())

    def _update_native_action(self):
        if self.native_reset is None:
            return
        try:
            current = (float(self._x.text()), float(self._y.text()))
        except ValueError:
            current = None
        changed = current != self.native_sampling
        self.native_reset.setEnabled(changed)
        self.native_reset.setVisible(changed)

    def _restore_native(self):
        x, y = self.native_sampling
        self._x.setText(str(x))
        self._y.setText(str(y))

    def _patch_selector(self, editor, field):
        if self.patch_choices is None:
            choices = QWidget(field)
            row = QHBoxLayout(choices)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(2)
            self.patch_choices = QButtonGroup(choices)
            for index in range(editor.count()):
                side = editor.itemData(index)
                if side is None:
                    continue
                button = QPushButton(str(side), choices)
                button.setObjectName('kronos-patch-' + str(side))
                button.setProperty('patchChoice', True)
                button.setCheckable(True)
                button.setAccessibleName(f'{side} by {side} pixel patch')
                button.setToolTip(editor.itemText(index) + ' pixels')
                self.patch_choices.addButton(button, index)
                row.addWidget(button)
            self.patch_choices.idClicked.connect(editor.setCurrentIndex)
            editor.currentIndexChanged.connect(self._select_patch)
            editor.hide()
            field.layout().insertWidget(0, choices)
            editor.setFocusProxy(self.patch_choices.buttons()[0])
        self._select_patch(editor.currentIndex())

    def _select_patch(self, index):
        button = self.patch_choices.button(index)
        if button is not None:
            button.setChecked(True)
        elif self.patch_choices.checkedButton() is not None:
            self.patch_choices.setExclusive(False)
            self.patch_choices.checkedButton().setChecked(False)
            self.patch_choices.setExclusive(True)

    def _label(self, text, style):
        label = QLabel(text, self.dimensions)
        label.setWordWrap(True)
        label.setProperty(style, True)
        return label

    def _arrange(self, width):
        # Give the two axis editors sufficient space for calibrated pixel sizes.
        arrangement = 'wide' if width >= 840 else 'medium' if width >= 515 else 'narrow'
        if arrangement == self._arrangement:
            return
        self._arrangement = arrangement
        for layout in (self.band, self.output_layout):
            while layout.count():
                layout.takeAt(0)
        top = Qt.AlignmentFlag.AlignTop
        self.band.addWidget(self.patch, 0, 0, top)
        self.band.setColumnStretch(0, 1 if arrangement == 'narrow' else 3)
        self.band.setColumnStretch(1, 0 if arrangement == 'narrow' else 5)
        self.band.setColumnStretch(2, 3 if arrangement == 'wide' else 0)
        self.output_layout.setColumnStretch(2, 0)
        self.output_layout.addWidget(self.field_caption, 0, 0)
        self.output_layout.addWidget(self.coverage, 1, 0)
        self.output_layout.addWidget(self.field_help, 2, 0)
        if arrangement == 'wide':
            self.band.addWidget(self.sampling, 0, 1, top)
            self.band.addWidget(self.output, 0, 2, top)
        elif arrangement == 'medium':
            self.band.addWidget(self.sampling, 0, 1, top)
            self.band.addWidget(self.output, 1, 0, 1, 2, top)
            self.output_layout.addWidget(self.coverage, 0, 1)
            self.output_layout.addWidget(self.field_help, 0, 2)
            self.output_layout.setColumnStretch(2, 1)
        else:
            self.band.addWidget(self.sampling, 1, 0, top)
            self.band.addWidget(self.output, 2, 0, top)
            self.output_layout.addWidget(self.coverage, 0, 1)
            self.output_layout.addWidget(self.field_help, 1, 0, 1, 2)
        self._arrange_axes()
        self.updateGeometry()

    def _arrange_axes(self):
        while self.axes.count():
            self.axes.takeAt(0)
        paired = self._arrangement == 'wide'
        for index, (caption, field) in enumerate(self._axis_fields.values()):
            row, column = (0, 2 * index) if paired else (index, 0)
            if caption is not None:
                self.axes.addWidget(caption, row, column, Qt.AlignmentFlag.AlignVCenter)
                column += 1
            self.axes.addWidget(field, row, column, Qt.AlignmentFlag.AlignTop)
        self.axes.setColumnStretch(1, 1 if self._x is not self._y else 0)
        self.axes.setColumnStretch(3, 1 if paired and self._x is not self._y else 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._arrange(event.size().width())

    def clear_fields(self):
        for layout in (self.channels.layout(), self.patch_fields, self.axes):
            while layout.count():
                layout.takeAt(0)
        self._has_channels = False
        self._axis_fields.clear()

    def add_field(self, name, caption, field):
        editor = caption.buddy()
        visible = not field.isHidden()
        field.layout().setContentsMargins(0, 0, 0, 0)
        if name.endswith('_channel_ids'):
            self._has_channels = visible
            editor.set_heading(caption)
            if field.parentWidget() is not self.channels:
                field.setParent(self.channels)
                field.setVisible(visible)
            # Fresh items measure the channel inventory after a search or resize.
            self.channels.layout().addItem(QWidgetItem(field))
            return
        help_label = field.findChild(QLabel, 'form-help-' + name)
        if help_label is not None:
            help_label.hide()
        if name.endswith('_patch_size'):
            caption.setText('Patch size (pixels)')
            self._patch_selector(editor, field)
            editor.setMinimumContentsLength(0)
            editor.setAccessibleDescription('Number of pixels along each side of the encoder patch.')
            self.patch_fields.addWidget(caption)
            self.patch_fields.addWidget(field)
        else:
            if name.endswith(('_pixel_x_um', '_pixel_y_um')):
                axis = 'X' if name.endswith('_pixel_x_um') else 'Y'
                caption.setText(axis)
                caption.setWordWrap(False)
                caption.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
                self._axis_fields[name] = (caption, field)
            else:
                # The tissue form moves this caption back to Image preparation.
                # Use the shared sampling caption here and keep its original text.
                caption.hide()
                editor.show()
                self._axis_fields[name] = (None, field)
            if not editor.hasFocus() and not editor.hasSelectedText():
                editor.setCursorPosition(0)
            self._arrange_axes()
        field.setVisible(visible)

    def set_preview(self, preview):
        self.preview = preview
        if self.grid.indexOf(preview) < 0:
            self.grid.addWidget(preview)
        self.finish()

    def finish(self):
        self.channels.setVisible(self._has_channels)
        self._update_native_action()
        self.updateGeometry()
