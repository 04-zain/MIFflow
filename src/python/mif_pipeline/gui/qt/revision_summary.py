"""Present an existing revision preview without changing its execution decisions."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePath
from PySide6.QtCore import Qt, QSize
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QLayout, QSizePolicy, QVBoxLayout, QWidget,
)
from ..presenters.workflow import STAGES


CHANGE_SUBJECTS = {
    "inspection": "source image or series", "reference_unmixing": "unmixing settings",
    "channel_review": "channels and markers",
    "segmentation": "segmentation settings", "measurements": "cell measurements",
    "marker_gate_review": "marker gates", "marker_combination_selection": "phenotype combinations", "phenotyping_mode_resolution": "phenotyping method",
    "unsupervised_preparation": "unsupervised phenotyping settings", "cluster_label_review": "cluster labels",
    'clustering_per_slide':'selected cluster memberships','clustering_pooled':'selected cluster memberships',
    "tissue_setup": "tissue detector", "tissue_resolution": "tissue image resolution",
    "tissue_candidate": "tissue sections", "analysis_domain_selection": "analysis regions",
    "registration": "paired image or alignment",
    "primary_classifier_training_plan_review": "primary classifier setup",
    "secondary_structure_definition": "secondary structures",
    "secondary_classifier_training_plan_review": "secondary classifier setup",
    "supervised_phenotype_training_plan_review": "cell phenotype classifier setup",
}


def choice_changes(before, after, labels=None):
    """Describe changed scientific fields in corresponding current/proposed columns.

    Callers supply already parsed values and optional form labels. Exact values
    remain in the editors; this presentation never produces a submission.
    """
    labels = labels or {}
    def leaves(values, prefix=()):
        result = {}
        for key, value in values.items():
            path = (*prefix, key)
            if isinstance(value, Mapping):
                result.update(leaves(value, path))
            else:
                result[path] = value
        return result
    before, after = leaves(before), leaves(after)
    def display(value):
        if isinstance(value, bool):
            return "Yes" if value else "No"
        if isinstance(value, (tuple, list)):
            return ", ".join(display(item) for item in value)
        if isinstance(value, Mapping):
            return "; ".join(f"{str(key).replace('_', ' ')}: {display(item)}" for key, item in value.items())
        if isinstance(value, float):
            return f"{value:g}"
        return {"accepted_tissue": "Accepted tissue", "configured_union": "Selected analysis regions",
                "raw": "Raw spectral image", "unmixed": "Separated components"}.get(str(value), str(value))
    keys = tuple(dict.fromkeys((*before, *after)))
    changed = [key for key in keys if not any(part.startswith("__") for part in key)
               and before.get(key) != after.get(key)]
    def name(path):
        # Full paths keep repeated names in distinct nested sections separate.
        full = ".".join(path)
        if full in labels:
            return labels[full]
        if sum(key[-1] == path[-1] for key in keys) == 1:
            return labels.get(path[-1], path[-1].replace("_", " ").capitalize())
        return " / ".join(part.replace("_", " ").capitalize() for part in path)
    def field_value(key, values):
        value = values.get(key, "Not selected")
        if key[-1] == "chunk_size" and isinstance(value, (tuple, list)):
            return " × ".join(display(item) for item in value)
        return display(value)
    return tuple("\n".join(f"{name(key)}: {field_value(key, values)}"
                            for key in changed) for values in (before, after))



def source_choices(settings, selected, *, show_series=True):
    """Identify the selected file using a parent suffix only for colliding filenames.

    A repeated selection describes the recalculation requested by the source
    revision. A sole eligible image needs no series detail in this comparison.
    """
    selected = selected if isinstance(selected, Mapping) else selected.to_mapping()
    before, after = PurePath(settings.source_mif_path), PurePath(selected["source_mif_path"])
    names = before.name, after.name
    if before != after and names[0] == names[1]:
        for depth in range(2, max(len(before.parts), len(after.parts)) + 1):
            names = str(PurePath(*before.parts[-depth:])), str(PurePath(*after.parts[-depth:]))
            if names[0] != names[1]:
                break
    indices = ((getattr(settings, "source_image_index", None), getattr(settings, "source_series_index", None)),
               (selected["source_image_index"], selected["source_series_index"]))
    if before == after and (not show_series or indices[0] == indices[1]):
        return names[0], "Reprocess the same image"
    if not show_series:
        return names
    return tuple(name + "\n" + ("Default image / series" if None in pair
                 else f"Image {pair[0]} · Series {pair[1]}") for name, pair in zip(names, indices))


def stage_choices(options, selected=None):
    """Summarize the saved stage decision and a parsed replacement when available."""
    subject = CHANGE_SUBJECTS[options.kind]
    saved = f"Saved {subject}."
    proposed = "Review the affected stages before applying this change."
    if selected is None:
        return saved, proposed
    if options.kind in {'clustering_per_slide','clustering_pooled'}:
        refit=selected.cluster_refit
        if refit.restore_parent:
            return 'Current reclustering result.', 'Restore the exact groups saved before this refit.'
        count=sum(options.saved_clustering['cluster_counts'][key] for key in refit.cluster_ids)
        method='FlowSOM' if refit.method=='flowsom' else 'PhenoGraph'
        return (f"{options.saved_clustering['cell_count']:,} cells assigned to the current groups.",
                f'Recluster {count:,} cells from {len(refit.cluster_ids):,} selected groups using {method}. '
                'Keep their original measurements and scaling, then review the resulting groups and phenotype names.')
    if options.kind == "analysis_domain_selection":
        current = {
            "selected_roi": "Drawn regions within accepted sections",
            "whole_tissue_direct": "Whole accepted sections",
            "whole_tissue_after_retirement": "Whole accepted sections",
            None: "No accepted analysis area",
        }[options.saved_analysis_mode]
        return current, "Reopen the analysis-area choice: use whole accepted sections or draw regions within them."
    if options.kind == "segmentation":
        before = options.settings.to_mapping()
        after = selected if isinstance(selected, dict) else selected.to_mapping()
        current, changed = choice_changes(before, after, {"tile_size_px": "Tile size (pixels)",
            "padding_px": "Tile overlap (pixels)", "workers": "Simultaneous tiles", "scope": "Area to segment"})
        return current or saved, changed or "Use the saved segmentation settings."
    if options.kind == "measurements":
        before, after = options.settings.feature_requests, selected.feature_requests
        def describe(rows, other):
            changed = [row for row in rows if row not in other]
            roles = {"expression": "Protein marker intensity", "dapi": "DNA", "autofluorescence": "Autofluorescence",
                     "morphology": "Cell shape", "nuclear_morphology": "Nuclear shape"}
            return "\n".join(str(row["measurement_name"]) + " · " + roles.get(row["kind"], row["kind"].replace("_", " "))
                             for row in changed)
        if before == after:
            return f"{len(before)} saved measurements.", "Use the saved measurements."
        return (describe(before, after) or "Existing measurements retained.",
                describe(after, before) or "Remove the listed measurements.")
    if options.kind == "inspection":
        return source_choices(options.settings, selected)
    if options.kind == "registration":
        def describe(settings):
            paired = settings.get("paired_reference") if isinstance(settings, Mapping) else settings.paired_reference
            if paired is None:
                return "No paired image selected."
            return PurePath(paired["source_path"] if isinstance(paired, Mapping) else paired.source_path).name
        return describe(options.settings), "Reopen alignment with the saved paired image." if selected == options.settings else describe(selected)
    return saved, f"Reopen {subject}; use the saved choices as a starting point."


_RESULT_LABELS = {
    "inspection": "Image selection", "channels": "Channel and marker assignments",
    "unmixing": "Prepared fluorescence images", "tissue": "Tissue outlines",
    "registration": "Paired-image alignment", "regions": "Included analysis regions",
    "classification": "Tissue-class predictions", "structures": "Secondary structure outlines and reviewed regions",
    "segmentation": "Cell boundaries", "measurements": "Per-cell measurements",
    "saved_result_retirement": "Current saved-result selections", "phenotyping": "Cell phenotype labels",
    "results": "Cell and region exports",
}


def _stage_results(members):
    """Name each affected output once, in the existing scientific stage order."""
    results = []
    for stage in STAGES:
        kinds = {node.kind for node in members if node.kind in stage.kinds}
        if not kinds:
            continue
        label = _RESULT_LABELS[stage.key]
        if stage.key == "classification" and kinds.intersection({
                "primary_classifier_training_plan_review", "primary_classifier_training_review"}):
            label = "Primary tissue classifier"
        elif stage.key == "structures" and kinds == {"secondary_structure_definition"}:
            label = "Secondary structure definitions"
        results.append((stage.key, label))
    return tuple(results)


def _impact_groups(preview, nodes):
    """Keep shared tasks separate from independently repeated image work.

    Sample identities come only from affected nodes. Independent single-image
    scopes combine only when they have the same affected stages and outputs.
    """
    affected_ids = set(preview.affected_node_ids)
    scopes = {}
    for node in nodes:
        if node.node_id in affected_ids:
            scopes.setdefault(tuple(node.sample_ids), []).append(node)
    groups = []
    selected = (preview.options.sample_id,)
    if selected in scopes:
        groups.append(("Selected image", selected, scopes.pop(selected)))
    independent = {}
    shared = []
    for samples, members in scopes.items():
        if len(samples) == 1:
            signature = _stage_results(members)
            grouped_samples, grouped_members = independent.setdefault(signature, ([], []))
            grouped_samples.extend(samples)
            grouped_members.extend(members)
        else:
            shared.append(("Shared work" if samples else "Analysis-wide work", samples, members))
    for samples, members in independent.values():
        groups.append(("Other images" if groups else "Affected images", tuple(sorted(samples)), members))
    return (*groups, *shared)


class _SampleChips(QWidget):
    """Wrap exact sample names into as many columns as the current width permits."""

    def __init__(self, samples, parent):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._grid = QGridLayout(self)
        self._grid.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(6)
        self._labels = []
        self._columns = 0
        for sample in samples:
            label = QLabel(sample, self)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label.setProperty("sampleIdentity", sample)
            label.setStyleSheet("background: #edf4f4; color: #31575a; border: 1px solid #d7e5e6; border-radius: 9px; padding: 3px 8px;")
            self._labels.append(label)
        self._arrange(self.width())

    def _arrange(self, width):
        if not self._labels:
            self.setFixedHeight(0)
            return
        chip_width = max(label.sizeHint().width() for label in self._labels)
        columns = max(1, min(len(self._labels), (width + 6) // (chip_width + 6)))
        if columns != self._columns:
            while self._grid.count():
                self._grid.takeAt(0)
            for index, label in enumerate(self._labels):
                self._grid.addWidget(label, index // columns, index % columns, Qt.AlignmentFlag.AlignLeft)
            self._columns = columns
        rows = (len(self._labels) + columns - 1) // columns
        available = max(1, (width - (columns - 1) * 6) // columns)
        height = max(max(label.sizeHint().height(), label.heightForWidth(available)) for label in self._labels)
        self.setFixedHeight(rows * height + (rows - 1) * 6)
        # Resizing can change our height during the grid's resize event.
        # Keep its geometry at the final height so the first row is not clipped.
        self._grid.setGeometry(self.rect())

    def minimumSizeHint(self):
        return QSize(0, self.height())

    def resizeEvent(self, event):
        self._arrange(event.size().width())
        super().resizeEvent(event)


class _ResultList(QWidget):
    """List affected outputs in two readable columns when the dialog has room."""

    def __init__(self, results, parent):
        super().__init__(parent)
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(20)
        self._grid.setVerticalSpacing(5)
        self._labels = []
        self._columns = None
        for _key, name in results:
            label = QLabel("• " + name, self)
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setStyleSheet("color: #294148;")
            self._labels.append(label)
        self._arrange()

    def _arrange(self):
        columns = 2 if self.width() >= 580 and len(self._labels) > 1 else 1
        if columns == self._columns:
            return
        self._columns = columns
        while self._grid.count():
            self._grid.takeAt(0)
        # Read down the first column, then down the second, in scientific stage order.
        rows = max(1, (len(self._labels) + columns - 1) // columns)
        for index, label in enumerate(self._labels):
            self._grid.addWidget(label, index % rows, index // rows, Qt.AlignmentFlag.AlignTop)
        self._grid.setColumnStretch(0, 1)
        self._grid.setColumnStretch(1, 1 if columns == 2 else 0)

    def resizeEvent(self, event):
        self._arrange()
        super().resizeEvent(event)


class RevisionSummary(QFrame):
    """Separate the proposed scientific change from its exact affected image scopes."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("revision-summary")
        self.setStyleSheet(
            "QFrame#revision-summary { background: transparent; border: none; }"
            "QFrame#revision-choices { background: white; border: 1px solid #d3dfe0; border-radius: 8px; }"
            "QFrame#revision-impact-group { background: white; border: 1px solid #dbe4e5; border-radius: 8px; }"
            "QLabel { background: transparent; }")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        self.choices = QFrame(self)
        self.choices.setObjectName("revision-choices")
        self._choices_layout = QGridLayout(self.choices)
        self._choices_layout.setContentsMargins(16, 14, 16, 14)
        self._choices_layout.setHorizontalSpacing(24)
        self._choices_layout.setVerticalSpacing(14)
        self._current_host, self.current = self._choice("Current", self.choices)
        self._proposed_host, self.proposed = self._choice("Proposed change", self.choices)
        self._compare = True
        self._choices_columns = None
        self._arrange_choices()
        layout.addWidget(self.choices)
        self.impact_header = QWidget(self)
        header = QHBoxLayout(self.impact_header)
        header.setContentsMargins(0, 0, 0, 0)
        self.heading = self._label("Affected samples and results", heading=True)
        header.addWidget(self.heading, 1)
        self.count = self._label("")
        self.count.setStyleSheet("color: #486169;")
        header.addWidget(self.count)
        layout.addWidget(self.impact_header)
        self.groups = QWidget(self)
        self.group_layout = QVBoxLayout(self.groups)
        self.group_layout.setContentsMargins(0, 0, 0, 0)
        self.group_layout.setSpacing(10)
        layout.addWidget(self.groups)
        self.clear_preview()

    def _label(self, text, *, heading=False, parent=None):
        label = QLabel(text, parent or self)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        if heading:
            font = label.font(); font.setBold(True); label.setFont(font)
        return label

    def _choice(self, title, parent):
        host = QWidget(parent)
        column = QVBoxLayout(host)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(7)
        column.setAlignment(Qt.AlignmentFlag.AlignTop)
        column.addWidget(self._label(title, heading=True))
        value = self._label("")
        value.setAlignment(Qt.AlignmentFlag.AlignTop)
        column.addWidget(value, 1)
        return host, value

    def set_choices(self, current, proposed, *, compare=True):
        """Compare edited values, or describe the choice that will be reopened."""
        self._compare = compare
        self._current_host.setVisible(compare)
        self._proposed_host.layout().itemAt(0).widget().setText("Proposed change" if compare else "This change")
        self.current.setText(current)
        self.proposed.setText(proposed)
        self._arrange_choices()

    def _arrange_choices(self):
        columns = 2 if self._compare and self.width() >= 560 else 1
        key = (columns, self._compare)
        if key == self._choices_columns:
            return
        self._choices_columns = key
        while self._choices_layout.count():
            self._choices_layout.takeAt(0)
        if self._compare:
            self._choices_layout.addWidget(self._current_host, 0, 0)
        self._choices_layout.addWidget(self._proposed_host, 0 if columns == 2 or not self._compare else 1,
                                      1 if columns == 2 else 0)
        self._choices_layout.setColumnStretch(0, 1)
        self._choices_layout.setColumnStretch(1, 1 if columns == 2 else 0)

    def resizeEvent(self, event):
        self._arrange_choices()
        super().resizeEvent(event)

    def clear_preview(self):
        """Remove obsolete consequences while retaining the current/proposed context."""
        self.impact_header.hide()
        self.groups.hide()

    def set_preview(self, preview, nodes):
        """Show exact affected image scopes with each output named once per group."""
        while self.group_layout.count():
            item = self.group_layout.takeAt(0)
            item.widget().deleteLater()
        groups = _impact_groups(preview, tuple(nodes))
        all_samples = {sample for _title, samples, _members in groups for sample in samples}
        self.count.setText(f"{len(all_samples)} {'image' if len(all_samples) == 1 else 'images'}")
        shown_samples = set()
        for title, samples, members in groups:
            group = QFrame(self.groups)
            group.setObjectName("revision-impact-group")
            group.setProperty("affectedSamples", list(samples))
            group.setProperty("affectedStages", [key for key, _label in _stage_results(members)])
            layout = QVBoxLayout(group)
            layout.setContentsMargins(16, 12, 16, 12)
            layout.setSpacing(10)
            layout.addWidget(self._label(title, heading=True, parent=group))
            if samples and set(samples) == shown_samples:
                layout.addWidget(self._label(f"Across all {len(samples)} images listed above", parent=group))
            elif samples:
                layout.addWidget(_SampleChips(samples, group))
            shown_samples.update(samples)
            layout.addWidget(_ResultList(_stage_results(members), group))
            self.group_layout.addWidget(group)
        self.impact_header.show()
        self.groups.show()
