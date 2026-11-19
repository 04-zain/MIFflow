"""Preview the complete reporting group before naming selected clusters."""
from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QLineEdit, QDialogButtonBox

from .classifier_review_data import review_table, set_table


class ClusterNameDialog(QDialog):
    def __init__(self, model, identities, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Name or merge clusters')
        self.resize(min(600,max(400,parent.width()-40)) if parent else 600, 450)
        self._model = model
        self._ids = tuple(identities)
        self._decisions = model.raw_value()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20); layout.setSpacing(12)
        title = QLabel('Review the phenotype name', self); title.setProperty('panelHeading', True)
        layout.addWidget(title)
        explanation = QLabel('Clusters with the same phenotype name are combined in reports. Original cluster IDs stay available.', self)
        explanation.setWordWrap(True); layout.addWidget(explanation)
        table = review_table('selected-clusters-to-name', self)
        set_table(table, ('Cluster', 'Cells', 'Current name'),
                  ((key, f'{model._counts[key]:,}', self._decisions[key]['name'] or '—') for key in self._ids))
        table.setFixedHeight(table.horizontalHeader().height()
                             +sum(table.rowHeight(i) for i in range(min(5,len(self._ids))))
                             +2*table.frameWidth()+6)
        layout.addWidget(table)
        label = QLabel('Phenotype name', self)
        self.name = QLineEdit(self); self.name.setObjectName('cluster-phenotype-name')
        label.setBuddy(self.name)
        layout.addWidget(label); layout.addWidget(self.name)
        self.preview = QLabel(self); self.preview.setWordWrap(True); layout.addWidget(self.preview)
        self.members = QLabel(self); self.members.setWordWrap(True); self.members.setProperty('fieldHelp', True)
        layout.addWidget(self.members)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel, parent=self)
        self.apply = buttons.addButton('Apply name', QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.name.textChanged.connect(self._update_preview)
        current_names = {self._decisions[key]['name'] for key in self._ids}
        if len(current_names) == 1:
            self.name.setText(next(iter(current_names)))
        self._update_preview()
        self.name.setFocus(); self.name.selectAll()

    def _update_preview(self):
        name = self.name.text().strip()
        self.apply.setEnabled(bool(name))
        if not name:
            self.preview.setText('Enter a name to preview the reporting group.')
            self.members.clear()
            return
        members = tuple(key for key in self._model._ids
                        if key in self._ids or (self._decisions[key]['include']
                        and (self._decisions[key]['name'] or key) == name))
        count = sum(self._model._counts[key] for key in members)
        self.preview.setText(f'{name} · {count:,} cells · {len(members):,} '
                             + ('cluster' if len(members) == 1 else 'clusters'))
        self.members.setText('Members: ' + ', '.join(members[:8])
                             + (f' · {len(members)-8:,} more' if len(members)>8 else ''))
        self.members.setToolTip(', '.join(members))

