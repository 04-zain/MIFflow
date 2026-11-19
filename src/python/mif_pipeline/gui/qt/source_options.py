"""Select a corrected original image through the existing IO series inventory."""
from pathlib import Path
from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QLabel,QLineEdit,QPushButton,QComboBox,QFileDialog,QFrame,QDialog
from mif_pipeline.io import SourceSeriesInventory
from mif_pipeline.io.canonical_ingestion import CanonicalIngestionError
from mif_pipeline.orchestration.batch_source_revision import SourceSelection

class SourceOptionsEditor(QWidget):
    inspection_requested = Signal(object)

    def __init__(self, sample, parent=None, *, mounted_images=None,
                 inspection_review=None, inspection_action='change_source'):
        super().__init__(parent)
        self.sample, self.inventory = sample, None
        self._mounted = mounted_images
        self._inspection_review = inspection_review
        outer = QVBoxLayout(self)
        self.action = QComboBox(self)
        self.action.setObjectName('inspection-options-action')
        self.action.setAccessibleName('Image inspection action')
        self.action.addItem('Change source image', 'change_source')
        if inspection_review is not None:
            self.action.addItem('Review cycle assignments', 'review_acquisition_cycles')
        self.action.setVisible(inspection_review is not None)
        outer.addWidget(self.action)
        self._source_editor = QWidget(self)
        layout = QVBoxLayout(self._source_editor)
        layout.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._source_editor)
        self._cycle_note = QLabel(
            f'Review which cycle each image channel belongs to in {Path(sample.source_mif_path).name}.', self)
        self._cycle_note.setWordWrap(True)
        self._cycle_note.setTextFormat(Qt.TextFormat.PlainText)
        self._cycle_note.setToolTip(str(sample.source_mif_path))
        outer.addWidget(self._cycle_note)
        outer.addStretch(1)
        card = QFrame(self)
        card.setObjectName('source-image-selection')
        card.setStyleSheet('QFrame#source-image-selection { background: white; border: 1px solid #ccd8da; border-radius: 6px; }')
        card_layout = QVBoxLayout(card)
        card_layout.addWidget(QLabel('Source image', card))
        self.filename = QLabel(Path(sample.source_mif_path).name, card)
        self.filename.setWordWrap(True)
        self.filename.setTextFormat(Qt.TextFormat.PlainText)
        card_layout.addWidget(self.filename)
        displayed_path = str(sample.source_mif_path) if mounted_images is None else mounted_images.local_path(str(sample.source_mif_path))
        self.path = QLineEdit(displayed_path, self)
        self.path.hide()
        self.filename.setToolTip(displayed_path)
        actions = QHBoxLayout()
        browse = QPushButton('Choose image…', card)
        browse.clicked.connect(self._browse)
        actions.addWidget(browse)
        inspect = QPushButton('Inspect image', card)
        inspect.clicked.connect(self._inspect)
        actions.addWidget(inspect)
        actions.addStretch(1)
        card_layout.addLayout(actions)
        layout.addWidget(card)
        self.series_label = QLabel('Image / series', self)
        layout.addWidget(self.series_label)
        self.series = QComboBox(self)
        self.series.setAccessibleName('Image / series')
        self.series.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.series.setMinimumContentsLength(24)
        self.series.setEnabled(False)
        self.series_label.hide()
        self.series.hide()
        layout.addWidget(self.series)
        self.status = QLabel('Inspect the selected image before continuing.', self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addStretch(1)
        self.path.textChanged.connect(self._path_changed)
        self.action.currentIndexChanged.connect(self._action_changed)
        selected_action = self.action.findData(inspection_action)
        if selected_action < 0:
            raise ValueError('The current inspection cannot offer this review action.')
        self.action.setCurrentIndex(selected_action)
        self._action_changed()

    def _action_changed(self):
        reviewing = self.action.currentData() == 'review_acquisition_cycles'
        self._source_editor.setVisible(not reviewing)
        self._cycle_note.setVisible(reviewing)

    def _browse(self):
        path,_ = QFileDialog.getOpenFileName(self,'Choose original fluorescence image',str(Path(self.path.text()).parent),'Image files (*)')
        if path:self.path.setText(path)

    def _path_changed(self):
        self.inventory = None;self.series.clear();self.series.setEnabled(False)
        self.filename.setText(Path(self.path.text()).name)
        self.filename.setToolTip(self.path.text())
        self.series_label.hide();self.series.hide()
        self.status.setText('Inspect the selected image before continuing.')
        self.status.show()

    def _inspect(self):
        try:
            path = (self._mounted.cluster_path(self.path.text().strip()) if self._mounted is not None
                    else Path(self.path.text().strip()).expanduser().resolve(strict=False))
        except ValueError as error:
            self.status.setText(str(error));return
        self.status.show()
        self.status.setText('Inspecting the selected image…')
        self.inspection_requested.emit(path)

    def set_inventory(self, inventory):
        """Bind the inspected source, offering a series choice only for multiple eligible images.

        A sole eligible series keeps its existing inventory candidate binding
        in the hidden selector for submission.
        """
        if self._mounted is not None:
            from types import SimpleNamespace
            if not isinstance(inventory, dict) or inventory.get("source_mif_path") != self._mounted.cluster_path(self.path.text().strip()):
                raise ValueError('The selected image changed. Inspect the current image again.')
            self._remote_inventory = inventory
            inventory = SimpleNamespace(source_identity=SimpleNamespace(resolved_path=inventory["source_mif_path"]),
                candidates=tuple(SimpleNamespace(**item) for item in inventory["candidates"]))
        elif type(inventory) is not SourceSeriesInventory:
            raise ValueError('Image inspection returned an invalid series inventory.')
        if self._mounted is None and inventory.source_identity.resolved_path != Path(self.path.text().strip()).expanduser().resolve(strict=False):
            raise ValueError('The selected image changed. Inspect the current image again.')
        self.inventory = inventory;self.series.clear();first = None;retained = None;eligible_count = 0
        for index,candidate in enumerate(inventory.candidates):
            self.series.addItem(f'{candidate.image_display_name} · {candidate.series_display_name}',candidate.candidate_id)
            if candidate.eligibility not in {'eligible', 'needs_organization'}:
                item=self.series.model().item(index);item.setEnabled(False)
                item.setToolTip('This image is not an eligible analytical series: '+', '.join(candidate.reasons))
            else:
                eligible_count += 1
                if first is None:first=index
                if (inventory.source_identity.resolved_path == self.sample.source_mif_path
                        and candidate.source_image_index == getattr(self.sample, "source_image_index", None)
                        and candidate.source_series_index == getattr(self.sample, "source_series_index", None)):
                    retained=index
        self.series.setEnabled(first is not None)
        self.series_label.setVisible(eligible_count > 1)
        self.series.setVisible(eligible_count > 1)
        if first is not None:
            self.series.setCurrentIndex(first if retained is None else retained)
            self.status.clear()
            self.status.hide()
        else:
            self.status.setText('This file has no eligible analytical series. Choose another original fluorescence image and inspect it.')
            self.status.show()

    def selected_settings(self):
        """Bind the current choice to the IO-owned opened source identity."""
        if self.action.currentData() == 'review_acquisition_cycles':
            return self._inspection_review
        if self.inventory is None or self.series.currentData() is None:
            raise ValueError('Inspect the selected image and choose an eligible analytical series first.')
        if self._mounted is not None:
            selected = next(item for item in self.inventory.candidates if item.candidate_id == self.series.currentData())
            return {"source_mif_path": self._remote_inventory["source_mif_path"],
                    "source_file_sha256": self._remote_inventory["source_file_sha256"],
                    "source_image_index": selected.source_image_index,
                    "source_series_index": selected.source_series_index}
        try:
            return SourceSelection.from_inventory(self.inventory,self.series.currentData())
        except CanonicalIngestionError as exc:
            raise ValueError(str(exc)) from exc


class SourceImagesDialog(QDialog):
    """Choose eligible full-resolution images from one retained IO inventory."""

    def __init__(self, inventory, parent=None, *, single_selection=False):
        from PySide6.QtWidgets import QDialogButtonBox, QTableWidget, QTableWidgetItem, QHeaderView
        from .workbench import style_scientific_table
        super().__init__(parent)
        self.single_selection = single_selection
        self.setWindowTitle("Choose paired image" if single_selection else "Choose images")
        self.setModal(True)
        self.resize(680, 380)
        self.candidates = tuple(item for item in inventory.candidates if item.eligibility in ({"eligible"} if single_selection else {"eligible", "needs_organization"}))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(14)
        filename = QLabel(inventory.source_identity.resolved_path.name, self)
        filename.setTextFormat(Qt.TextFormat.PlainText)
        filename.setWordWrap(True)
        filename.setProperty("panelHeading", True)
        layout.addWidget(filename)
        help_text = QLabel("Select the image to pair with this sample." if single_selection else "Select one or more images. Each becomes a separate sample.", self)
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        self.table = QTableWidget(len(self.candidates), 4, self)
        self.table.setObjectName("source-image-choices")
        self.table.setHorizontalHeaderLabels(("Use", "Image", "Dimensions", "Channels"))
        style_scientific_table(self.table)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection if single_selection else QTableWidget.SelectionMode.NoSelection)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setColumnHidden(0, single_selection)
        self.table.verticalHeader().hide()
        for row, candidate in enumerate(self.candidates):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(Qt.CheckState.Unchecked)
            self.table.setItem(row, 0, check)
            name = candidate.image_display_name
            if sum(item.image_display_name == name for item in self.candidates) > 1:
                name += f" · image {candidate.source_image_index + 1}"
            dimensions = f"{candidate.size_x:,} × {candidate.size_y:,}" if candidate.size_x and candidate.size_y else "—"
            for column, value in enumerate((name, dimensions, str(candidate.size_c or "—")), 1):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(row, column, item)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in (0, 2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok, self)
        self.use = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.use.setText("Use selected image" if single_selection else "Use selected images")
        self.use.setProperty("actionRole", "primary")
        self.use.setEnabled(False)
        self.table.itemChanged.connect(lambda _item: self.use.setEnabled(bool(self.selected_candidate_ids())))
        self.table.itemSelectionChanged.connect(lambda: self.use.setEnabled(bool(self.selected_candidate_ids())))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_candidate_ids(self):
        if self.single_selection:
            rows = self.table.selectionModel().selectedRows()
            return tuple(self.candidates[index.row()].candidate_id for index in rows)
        return tuple(candidate.candidate_id for row, candidate in enumerate(self.candidates)
                     if self.table.item(row, 0).checkState() == Qt.CheckState.Checked)
