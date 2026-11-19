"""Tables and exact CSV exports for the fitted-classifier review workspace."""

import csv
import re
from io import BytesIO, StringIO
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

from PySide6.QtWidgets import QFileDialog, QMenu, QMessageBox, QToolButton, QTableView, QHeaderView
from PySide6.QtCore import Qt, Signal

from mif_pipeline.gui.interactions import FormTable, FormTableRow
from .models import FormTableModel
from .workbench import style_scientific_table, _fit_bounded_table_height
from .figure_export import _save_bytes


def csv_bytes(columns, rows):
    stream = StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(columns)
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def read_artifact_rows(artifacts, name):
    item = next((item for item in artifacts if item.artifact_id == name + "/csv"
                 and item.status == "available"), None)
    if item is None:
        return ()
    with Path(item.path).open(newline="", encoding="utf-8") as stream:
        return tuple(csv.DictReader(stream))


def recorded_region_request(row):
    """Navigate to the original evaluation bounds without rerunning a model."""
    return {key: row[key] for key in ('project_entry_id', 'annotation_uuid', 'split', 'subject_id')} | {
        'reference_class': row['reference_name'], 'predicted_class': row['predicted_name'],
        'roi_bounds': {key: int(row[key]) if key in ('z','t') else float(row[key])
                       for key in ('x','y','width','height','z','t')}}


def set_table(view, columns, rows, *, identities=None):
    rows = tuple(rows)
    identities = tuple(str(i) for i in range(len(rows))) if identities is None else tuple(identities)
    old = view.model()
    view.setModel(FormTableModel(FormTable(view.objectName() or "review-table", tuple(columns),
        tuple(FormTableRow(key, tuple(row)) for key, row in zip(identities, rows, strict=True))), view))
    if old is not None:
        old.deleteLater()
    _fit_bounded_table_height(view)


def review_table(name, parent):
    view = QTableView(parent)
    view.setObjectName(name)
    view.setAccessibleName(name.replace("-", " "))
    view.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
    view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QTableView.SelectionMode.SingleSelection)
    style_scientific_table(view)
    view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    return view


class ReviewDataExport(QToolButton):
    """Export existing review tables, singly or together, without display rounding."""

    exported = Signal(str)

    def __init__(self, form, parent=None):
        super().__init__(parent)
        self.setText("Export review data…")
        self.setObjectName("classifier-review-export-data")
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._tables = {}
        for table in form.tables:
            if table.table_id == "training_fit_classifiers" or table.table_id.startswith((
                    "training_classes_", "training_images_", "cv_candidates_", "selected_parameters_")):
                self._tables[table.table_id] = (table.title or table.table_id,
                    csv_bytes(table.columns, (row.values for row in table.rows)))
        for index, row in enumerate(form.context['stage_result']['resume_context']['classifiers']):
            cv = row.get('model_selection')
            if cv:
                self._tables[f'cv_candidates_{index}'] = ('Cross-validation candidates', csv_bytes(
                    ('classifier', 'candidate', 'selected', 'selection_score', 'fold', 'fold_score'),
                    ((row['classifier_id'], item['candidate_index'] + 1,
                      item['candidate_index'] == cv['selected_candidate_index'], item['selection_score'], fold + 1, score)
                     for item in cv['candidate_scores'] for fold, score in enumerate(item['fold_scores']))))
        self._base_tables = dict(self._tables)
        self._refresh_menu()

    def set_artifacts(self, artifacts):
        tables = dict(self._base_tables)
        for item in artifacts:
            if item.status == "available" and item.artifact_id in {
                    "classifier-allocation/csv", "classifier-recipe/csv", "classifier-confusion-data/csv",
                    "classifier-fitting-classes/csv", "classifier-fitting-images/csv",
                    "classifier-cross-validation/csv", "classifier-evaluation-locations/csv",
                    "classifier-evaluation-cells/csv",
                    "classifier-evaluation-scores/csv", "classifier-cv-parameters/csv",
                    "classifier-cv-folds/csv", "classifier-cv-confusion-data/csv", "classifier-cv-reference-locations/csv"}:
                tables[item.artifact_id.split("/")[0]] = (item.title, Path(item.path).read_bytes())
        # Prefer the exact shared CSVs when the service supplies them; keep the
        # presenter tables only while those artifacts are still loading.
        replacements = {'classifier-fitting-classes': 'training_classes_',
                        'classifier-fitting-images': 'training_images_',
                        'classifier-cross-validation': 'cv_candidates_'}
        for name, prefix in replacements.items():
            if name in tables:
                tables = {key: value for key, value in tables.items() if not key.startswith(prefix)}
        self._tables = tables
        self._refresh_menu()

    def _refresh_menu(self):
        old = self.menu()
        menu = QMenu(self)
        menu.addAction("All recorded tables (.zip)…", lambda: self._export(None))
        menu.addSeparator()
        for key, (title, _) in self._tables.items():
            menu.addAction(title + " (.csv)…", lambda _checked=False, key=key: self._export(key))
        self.setMenu(menu)
        if old is not None:
            old.deleteLater()

    def data(self, key=None):
        if key is not None:
            return self._tables[key][1]
        stream = BytesIO()
        with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
            for name, (_, data) in self._tables.items():
                archive.writestr(name + ".csv", data)
            archive.writestr("README.txt", "Recorded classifier review tables.\n"
                "Counts use each table's recorded observation unit: cells, pixels, patches or reference regions.\n"
                "Each table retains its recorded population and evaluation scope.\n")
        return stream.getvalue()

    def _export(self, key):
        suffix = "zip" if key is None else "csv"
        name = "classifier-review" if key is None else re.sub(r"[^\w.-]+", "-", key)
        filename, _ = QFileDialog.getSaveFileName(self, "Export classifier review data", name + "." + suffix,
            "ZIP archive (*.zip)" if key is None else "CSV table (*.csv)")
        if not filename:
            return
        path = Path(filename)
        try:
            _save_bytes(self.data(key), path)
            self.exported.emit(path.name)
        except OSError as error:
            QMessageBox.warning(self, "Data not exported", f"Could not save {path.name}.\n\n{error}")
