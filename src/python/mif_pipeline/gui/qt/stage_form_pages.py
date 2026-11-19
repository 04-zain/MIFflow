"""Sequential settings pages that keep scientific values in their existing editors.

The form renderer owns validation, defaults and submitted values. This component
only groups its controls, selects the visible page and provides a movable footer.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from PySide6.QtCore import Qt, Signal, QSignalBlocker, QEvent, QPoint, QTimer
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QLayout, QComboBox,
    QVBoxLayout, QWidget, QWidgetItem, QTabBar, QSizePolicy, QAbstractScrollArea,
)

from .researcher_widgets import ContentSizedButton, configure_disclosure
from .kronos_marker_chooser import KronosMarkerAssignment, KronosMarkerMapping, KronosAdditionalMarkerDetails
from .kronos_image_inputs import KronosImageInputs
from .primary_count_bounds import PrimaryClassCountBounds


@dataclass(frozen=True)
class SettingsPage:
    key: str
    title: str
    description: str
    order: int
    phase: int = 1


def training_page(name: str, *, supervised: bool, candidates: tuple[str, ...], primary: bool = False, secondary: bool = False) -> SettingsPage:
    """Assign an existing training field to its scientific decision page."""
    if name in {"classifier_source", "saved_classifier_export"}:
        return SettingsPage("classifier-source", "Classifier source", "", -10, 0)
    match = re.match(r"(?:primary|secondary|training|supervised)_(\d+)_", name)
    index = int(match.group(1)) if match else 0
    suffix = f" · {candidates[index]}" if not secondary and len(candidates) > 1 and index < len(candidates) else ""

    def page(key, title, description, order, phase=1):
        return SettingsPage(f"{index}-{key}", title + suffix, description, index * 100 + order, phase)

    preparation = ("Scaling determines how much each feature contributes to distances between training examples. "
                   "PCA can reduce the number of classifier inputs when several features vary together.")

    if (name in {"reference_annotations_complete", "allow_annotation_preview", "annotation_preview_candidate"}
            or (name.startswith("primary_") and name.endswith(("_training_regions", "_review_policy")))):
        # The selected-reference workspace supplies image-specific area and save guidance.
        description = ""
        return SettingsPage("references", "Reference annotations", description, 10000)
    if name.startswith("primary_") and name.endswith(("_count_minimum", "_count_maximum", "_class_bounds")):
        return page("region-counts", "Region count limits",
                    "Set limits for how many classified regions each analysis region should contain, "
                    "either in total or for specific tissue classes.", 70)
    if re.fullmatch(r"primary_\d+_class_(?:count|\d+_(?:name|id))", name) or name.endswith("_class_ids") or (supervised and name.endswith("_point_radius")) or name.endswith("_ignore_policy"):
        return page("labels", "Class labels", "Specify which tissue compartments the classifier should identify." if not supervised else "Name the phenotypes you will assign to reference cells in QuPath.", 0, 0)
    if re.fullmatch(r"(?:primary|secondary)_\d+_channel_ids", name):
        return page("channels", "Image channels",
                    "Choose the channels used to distinguish the selected structures from other tissue."
                    if secondary else "Choose the stains that distinguish your tissue classes. RF and SVM use their intensities and filter responses. KRONOS encodes the selected marker images.", 10, 0)
    if supervised and (name.endswith(("_model_method", "_procedure", "_intent", "_cv_graph_widths"))
                       or any(part in name for part in ("_grid_", "_rtrees_", "_svm_", "_logistic_regression_"))):
        return page("method", "Classifier", "", 5, 0)
    if name.endswith(("_feature_ids", "_point_radius")):
        return page("features", "Cell measurements", "Select saved marker intensities and cell or nuclear shape measurements for training.", 10, 0)
    if (primary or secondary) and name.startswith("training_"):
        if name.endswith(("_model_method", "_procedure", "_intent")):
            return page("model", "Classifier", "", 20)
        if name.endswith(("_normalization", "_pca_variance", "_pca_normalize")):
            return page("feature-preparation", "Feature preparation",
                        preparation, 24)
        if "_image_" in name or name.endswith(("_resolution", "_maximum_samples", "_reweight", "_boundary_strategy", "_boundary_thickness", "_boundary_label")):
            return page("image-features", "Image preparation",
                        "Choose the pixel size at which to classify the tissue. At this resolution, each pixel contributes the intensities and filter responses selected below.", 21)
        if ("_patient_" in name or name.endswith(("_cv_folds", "_assignment_mode",
                                                  "_test_subjects", "_validation_subjects", "_seed"))):
            return page("subjects", "Subject allocation", "", 40)
        if ("_grid_" in name or "_rtrees_" in name or "_svm_" in name
                or name.endswith("_cv_graph_widths")
                or ("_kronos_" in name and name.endswith(("_width", "_epochs", "_batch", "_learning_rate", "_weight_decay")))):
            return page("model", "Classifier", "", 20)
    if "_image_" in name and name.startswith("training_"):
        return page("image-features", "Image features", "Choose channel intensities and filtered images for the classifier. Filters are calculated from the same image channels at each physical scale.", 21)
    if "_kronos_" in name:
        if "_panel_" in name:
            return page("kronos-panel", "KRONOS channel inputs", "", 23, 0 if supervised else 1)
        if name.endswith(("_width", "_epochs", "_batch", "_learning_rate", "_weight_decay")):
            return page("fit", "Fitting and validation", "Fit one parameter combination or compare combinations using subject-grouped validation. Set model parameters and search values in Advanced settings.", 40)
        description = ("Encode a cell image centred on its nucleus, excluding staining outside the cell. "
                       if supervised else "Encode tissue patches for a graph model that learns from each patch and its neighbours. ")
        description += ("Patch size sets the number of pixels along each side of the image, "
                        "and sampling resolution sets the physical distance represented by each pixel.")
        return page("kronos-model", "KRONOS image inputs", description, 22, 0 if supervised else 1)
    if name.endswith(("_assignment_mode", "_test_subjects", "_validation_subjects", "_seed", "_cv_folds")) or "_patient_" in name:
        return page("subjects", "Subject allocation", "", 50)
    if "_grid_" in name or "_rtrees_" in name or "_svm_" in name or "_logistic_regression_" in name or name.endswith(("_procedure", "_intent", "_cv_folds", "_cv_graph_widths")):
        description = ("Fit the displayed model settings, or compare parameter combinations using subject-grouped validation."
                       if supervised else "Fit one parameter combination or compare combinations using subject-grouped validation. Set model parameters and search values in Advanced settings.")
        return page("fit", "Fitting and validation", description, 40)
    if supervised:
        return page("model", "Feature preparation",
                    preparation, 30, 1)
    if name.endswith("_model_method"):
        return page("model", "Classifier", "Select the model fitted to your reference labels. Evaluation is configured on the following pages.", 20, 0 if supervised else 1)
    return page("model", "Classifier", "Select the model and input sampling used to fit the reference labels. Image features are selected on the following page.", 20, 0 if supervised else 1)


def advanced_training_control(name: str, *, primary: bool = False, supervised: bool = False, secondary: bool = False) -> bool:
    """Identify optional numerical and processing controls without changing their values.

    We use the same field families as the owning form's visibility rules. Required
    class labels, channels, methods and subject allocation stay in the main card;
    fitting grids and resource controls appear beneath its disclosure.
    """
    if name.endswith(("_normalization", "_pca_variance", "_pca_normalize")):
        return False
    if (primary or secondary) and name.endswith("_rtrees_active_var_count"):
        return True
    if (primary or secondary) and ("_grid_" in name or name.endswith((
            "_rtrees_max_depth", "_rtrees_min_sample_count", "_rtrees_active_var_count", "_rtrees_max_trees",
            "_svm_kernel", "_svm_c", "_svm_gamma", "_cv_folds", "_cv_graph_widths"))):
        return False
    if supervised and ("_grid_" in name or name.endswith((
            "_rtrees_max_depth", "_rtrees_min_sample_count", "_rtrees_active_var_count", "_rtrees_max_trees",
            "_svm_kernel", "_svm_c", "_svm_gamma", "_logistic_regression_regularization",
            "_cv_folds", "_normalization", "_pca_variance"))):
        return False
    if name.startswith("primary_") and name.endswith(("_count_minimum", "_count_maximum", "_class_bounds")):
        return False
    if "_kronos_" in name:
        return name.endswith(("_width", "_epochs", "_batch", "_learning_rate", "_weight_decay", "_threads", "_memory_mib", "_divisor"))
    if any(part in name for part in ("_grid_", "_rtrees_", "_svm_", "_logistic_regression_", "_count_", "_output_label_")):
        return True
    if name.endswith(("_point_radius", "_seed", "_cv_folds", "_cv_graph_widths", "_maximum_samples", "_reweight", "_normalization", "_pca_variance", "_pca_normalize", "_boundary_strategy", "_boundary_thickness", "_boundary_label", "_classifier_id", "_training_regions", "_class_bounds", "_review_policy")):
        return True
    return bool(re.fullmatch(r"primary_\d+_class_\d+_id", name))


class StageFormPages(QWidget):
    """Route a form's existing controls without replacing their values or signals."""

    page_changed = Signal()
    candidate_changed = Signal(int)
    definition_back_requested = Signal()

    def __init__(self, before_change, parent=None, *, kronos_vocabulary=None):
        super().__init__(parent)
        self.setObjectName("stage-form-pages")
        self._before_change = before_change
        self._definitions = {}
        self._pages = {}
        self._grids = {}
        self._positions = {}
        self._advanced_pages = {}
        self._advanced_grids = {}
        self._advanced_positions = {}
        self._fields = {}
        self._field_layouts = {}
        self._kronos_vocabulary = kronos_vocabulary or {}
        self._kronos_mappings = {}
        self._kronos_headers = {}
        self._kronos_scaling_help = {}
        self._kronos_details = {}
        self._kronos_detail_grids = {}
        self._kronos_footprints = {}
        self._kronos_previews = {}
        self._kronos_input_fields = {}
        self._multiclass_ignore_policies = {}
        self.feature_preparation = None
        self.projection_pages = {}
        self.tissue_parameters = {}
        self._single_column = False
        self._viewport = None
        self._card_fit_pending = False
        self._available = []
        self.current_key = None
        self.candidate_index = None
        self.classifier_overview = None
        self.candidate_issues = []
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(16)
        self.body.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.tabs = QTabBar(self)
        self.tabs.setObjectName("stage-page-tabs")
        self.tabs.setAccessibleName("Settings pages")
        self.tabs.setExpanding(False)
        self.tabs.setDrawBase(False)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.setElideMode(Qt.TextElideMode.ElideNone)
        self.tabs.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.tabs.currentChanged.connect(self._tab_selected)
        self.body.addWidget(self.tabs)
        self.advanced = QWidget(self)
        self.advanced.setObjectName("stage-advanced-settings")
        self.options = QVBoxLayout(self.advanced)
        self.options.setContentsMargins(0, 0, 0, 0)
        self.options.setSpacing(12)
        self.body.addWidget(self.advanced)
        self.navigation = QWidget(parent)
        self.navigation.setObjectName("stage-form-navigation")
        row = QHBoxLayout(self.navigation)
        row.setContentsMargins(0, 4, 0, 4)
        row.setSpacing(10)
        self.back = ContentSizedButton("Back", self.navigation)
        self.back.setObjectName("stage-form-back")
        self.back.setProperty("actionRole", "secondary")
        self.next = ContentSizedButton("Next", self.navigation)
        self.next.setObjectName("stage-form-next")
        self.next.setProperty("actionRole", "primary")
        self.position = QLabel(self.navigation)
        self.position.setWordWrap(True)
        row.addWidget(self.back)
        row.addWidget(self.position, 1)
        row.addWidget(self.next)
        self.back.clicked.connect(self._back)
        self.next.clicked.connect(lambda: self._move(1))

    def set_candidates(self, groups, *, back_caption="Back"):
        """Use the presenter's candidate order and the structures covered by each."""
        self.candidate_index = 0
        self._definition_back_caption = back_caption
        self.candidate_labels = [", ".join(names) for names in groups]
        self.candidate_header = QWidget(self)
        row = QHBoxLayout(self.candidate_header)
        row.setContentsMargins(0, 0, 0, 0)
        self._candidate_titles = [
            names[0] if len(names) == 1 else f"Shared classifier · {len(names)} structures"
            for names in groups
        ]
        self.candidate_heading = QLabel(self._candidate_titles[0], self.candidate_header)
        self.candidate_heading.setToolTip(self.candidate_labels[0])
        self.candidate_heading.setAccessibleName(self.candidate_labels[0])
        self.candidate_heading.setTextFormat(Qt.TextFormat.PlainText)
        self.candidate_heading.setWordWrap(True)
        self.candidate_heading.setProperty("panelHeading", True)
        row.addWidget(self.candidate_heading, 1)
        self.candidate_status = QLabel(self.candidate_header)
        self.candidate_status.setProperty("choiceHelp", True)
        row.addWidget(self.candidate_status)
        self.body.insertWidget(0, self.candidate_header)

        if len(groups) < 2:
            return
        self.return_to_classifiers = ContentSizedButton("Back to classifiers", self.candidate_header)
        self.return_to_classifiers.setObjectName("secondary-back-to-classifiers")
        self.return_to_classifiers.clicked.connect(self.show_classifiers)
        row.insertWidget(0, self.return_to_classifiers)
        from .secondary_classifier_preparations import SecondaryClassifierPreparations
        self.classifier_overview = SecondaryClassifierPreparations(groups, self)
        self.classifier_overview.open_requested.connect(self._open_candidate_section)
        self.candidate_issues = self.classifier_overview.issues
        self.body.insertWidget(0, self.classifier_overview)
        self.current_key = "classifiers"

    def _open_candidate_section(self, index, section):
        destination = {"inputs": f"{index}-channels", "training": f"{index}-subjects", "references": "references"}
        self.select_candidate(index, destination[section])

    def show_classifiers(self):
        if self.classifier_overview is None or self.current_key == "classifiers":
            return
        self.current_key = "classifiers"
        self._show_current()
        self.page_changed.emit()

    def select_candidate(self, index, destination=None):
        """Activate this classifier before displaying its controls or annotation list."""
        if self.candidate_index is None or not 0 <= index < len(self.candidate_labels):
            return
        changed = self.candidate_index != index
        previous = self.current_key
        if changed:
            self.candidate_index = index
            self.candidate_heading.setText(self._candidate_titles[index])
            self.candidate_heading.setToolTip(self.candidate_labels[index])
            self.candidate_heading.setAccessibleName(self.candidate_labels[index])
            self.sync()
        if destination is None:
            destination = (f"{index}-" + previous.split("-", 1)[1]
                           if previous and re.match(r"\d+-", previous) else previous)
        if destination in self._available:
            self.select(destination, validate=False)
        if changed:
            self.candidate_changed.emit(index)

    def add_control(self, definition, name, caption, field, *, compact=False, advanced=False, table=None, classifier_choices=None):
        """Place an existing editor in its main card or lower advanced area.

        Captions, validation messages and signals remain attached to the original
        editor. Both areas share one page identity so validation can reveal the
        requested control and navigation retains every edited value.
        """
        if re.fullmatch(r"primary_\d+_class_(?:count|\d+_(?:name|id))", name) or name.endswith(("_class_bounds", "_class_ids")):
            compact = False
        key = definition.key
        if key not in self._pages:
            self._definitions[key] = definition
            page = QFrame(self)
            page.setProperty("stageFormPage", True)
            page.setObjectName("stage-page-" + key)
            page.installEventFilter(self)
            page.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
            layout = QVBoxLayout(page)
            layout.setContentsMargins(24, 22, 24, 24)
            layout.setSpacing(12)
            if key.endswith('-kronos-model'):
                layout.setContentsMargins(24, 18, 24, 18)
                layout.setSpacing(10)
            if key == "references" or (key.endswith("-subjects") and getattr(table, "_integrated_procedure", False)):
                page.setStyleSheet(f'QFrame#stage-page-{key} {{ background: transparent; border: none; border-radius: 0; }}')
                layout.setContentsMargins(8, 16, 8, 16)
            title = QLabel(definition.title, page)
            title.setProperty("stagePageHeading", True)
            title.setWordWrap(True)
            title.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            layout.addWidget(title)
            description = QLabel(definition.description, page)
            description.setObjectName("description-" + key)
            description.setProperty("stagePageDescription", True)
            description.setWordWrap(True)
            description.setVisible(bool(definition.description))
            layout.addWidget(description)
            grid = QGridLayout()
            grid.setContentsMargins(0, 6, 0, 0)
            grid.setHorizontalSpacing(22)
            grid.setVerticalSpacing(8)
            grid.setColumnStretch(0, 1)
            grid.setColumnStretch(1, 1)
            layout.addLayout(grid)
            self._pages[key], self._grids[key], self._positions[key] = page, grid, (0, 0)
            if key.endswith("-kronos-panel"):
                mapping = KronosMarkerMapping(self._kronos_vocabulary, page)
                self._kronos_mappings[key] = mapping
                mapping.assignment_selected.connect(lambda editor, key=key: self._select_kronos_details(key, editor))
                layout.insertWidget(2, mapping)
                for section, text in (("mapping", "Marker description"), ("scaling", "Intensity scaling")):
                    heading = QLabel(text, page)
                    heading.setObjectName(f"kronos-section-{key}-{section}")
                    heading.setProperty("panelHeading", True)
                    heading.setContentsMargins(0, 0, 0, 2)
                    heading.hide()
                    self._kronos_headers[key, section] = heading
                explanation = QLabel(
                    "Divide intensities by this value, subtract the marker mean, then divide by its standard deviation.", page)
                explanation.setObjectName("kronos-scaling-help-" + key)
                explanation.setWordWrap(True)
                explanation.setProperty("fieldHelp", True)
                explanation.hide()
                self._kronos_scaling_help[key] = explanation
            if table is not None:
                layout.addWidget(table)
            self.body.insertWidget(self.body.indexOf(self.advanced), page)
            extra = QWidget(self.advanced)
            extra.setObjectName("stage-advanced-" + key)
            extra.installEventFilter(self)
            extra_grid = QGridLayout(extra)
            extra_grid.setContentsMargins(20, 4, 20, 12)
            extra_grid.setHorizontalSpacing(22)
            extra_grid.setVerticalSpacing(14)
            extra_grid.setColumnStretch(0, 1)
            extra_grid.setColumnStretch(1, 1)
            self._advanced_pages[key] = extra
            self._advanced_grids[key] = extra_grid
            self._advanced_positions[key] = (0, 0)
            self.options.addWidget(extra)
            extra.hide()
            page.hide()
        caption.setProperty("stageFieldCaption", True)
        field.setProperty("stageField", True)
        self._fields[name] = (key, caption, field)
        if key.endswith('-model') and name in {'supervised_0_normalization', 'supervised_0_pca_variance', 'supervised_0_pca_normalize'}:
            from .supervised_feature_preparation import SupervisedFeaturePreparation
            if self.feature_preparation is None:
                self.feature_preparation = SupervisedFeaturePreparation(self._pages[key])
                self._pages[key].layout().insertWidget(2, self.feature_preparation)
            self.feature_preparation.add_control(name, caption, field)
            return
        if key.endswith("-feature-preparation") and name.endswith(("_normalization", "_pca_variance", "_pca_normalize")):
            from .supervised_feature_preparation import SupervisedFeaturePreparation
            if key not in self.projection_pages:
                self.projection_pages[key] = SupervisedFeaturePreparation(self._pages[key])
                self._pages[key].layout().addWidget(self.projection_pages[key])
            self.projection_pages[key].add_control(name, caption, field)
            return
        if classifier_choices is not None:
            classifier_choices.set_procedure(caption, field)
            return
        if isinstance(caption.buddy(), PrimaryClassCountBounds):
            prefix = name.removesuffix("_class_bounds")
            total_names = [prefix + "_count_" + kind for kind in ("minimum", "maximum")]
            if all(total in self._fields for total in total_names):
                total_fields = []
                for total in total_names:
                    _key, total_caption, total_field = self._fields[total]
                    self._field_layouts.pop(total)
                    total_fields.append((total, total_caption, total_field))
                caption.buddy().set_total_fields(total_fields)
                caption.clear()
                caption.setMaximumHeight(0)
                field.layout().addWidget(caption)
                help_text = field.findChild(QLabel, "form-help-" + name)
                if help_text is not None:
                    help_text.hide()
                compact = False
        if key.endswith("-channels") and name.endswith("_channel_ids"):
            caption.buddy().set_heading(caption)
            self._pages[key].layout().insertWidget(2, field)
            return
        if isinstance(caption.buddy(), KronosMarkerAssignment):
            self._kronos_mappings[key].add_assignment(caption, field, caption.buddy())
            return
        if key.endswith("-kronos-panel") and "_additional_" in name:
            prefix = name.split("_additional_", 1)[0]
            detail_key = key, prefix
            if detail_key not in self._kronos_details:
                panel = KronosAdditionalMarkerDetails(caption.text().split(":", 1)[0], self._pages[key])
                panel.setObjectName("kronos-details-" + prefix)
                self._kronos_mappings[key].detail_layout.addWidget(panel)
                self._kronos_details[detail_key] = panel
                self._kronos_detail_grids[detail_key] = panel.grid
            self._kronos_details[detail_key].prepare_control(name, caption, field)
            compact = name.endswith(("_compartment", "_family"))
        if table is not None:
            table.add_control(name, field, caption.buddy())
            caption.setText("")
            caption.setMaximumHeight(0)
            field.layout().addWidget(caption)
            return
        tissue_fit = key.endswith("-model") and any(
            field_name.endswith("_model_method") and field_key == key
            for field_name, (field_key, _, _) in self._fields.items()
        )
        parameter = any(part in name for part in ("_grid_", "_rtrees_", "_svm_", "_logistic_regression_"))
        if not advanced and (tissue_fit or key.endswith(("-fit", "-method"))) and (parameter or name.endswith("_cv_graph_widths")):
            from .tissue_classifier_parameters import TissueClassifierParameters
            if key not in self.tissue_parameters:
                self.tissue_parameters[key] = TissueClassifierParameters(self._pages[key])
                self._pages[key].layout().addWidget(self.tissue_parameters[key])
            self.tissue_parameters[key].add_control(name, caption, field)
            return
        if key.endswith("-image-features") and name.endswith("_resolution"):
            compact = False
        self._field_layouts[name] = (compact, advanced)
        positions = self._advanced_positions if advanced else self._positions
        row, column = positions[key]
        if not compact and column:
            row, column = row + 2, 0
        span = 1 if compact else 2
        grid = self._advanced_grids[key] if advanced else self._grids[key]
        grid.addWidget(caption, row, column, 1, span, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(field, row + 1, column, 1, span, Qt.AlignmentFlag.AlignTop)
        positions[key] = (row, 1) if compact and column == 0 else (row + 2, 0)

    def _relayout_fields(self):
        """Reflow existing editors and group KRONOS identities before their scaling.

        Hidden channel choices retain their drafts. Section headings and keyboard
        order follow the fields that the renderer has made applicable.
        """
        for grid in (*self._grids.values(), *self._advanced_grids.values(), *self._kronos_detail_grids.values()):
            while grid.count():
                grid.takeAt(0)
        for body in self._kronos_input_fields.values():
            body.clear_fields()
        positions = {}
        ordered = list(self._field_layouts.items())
        original_order = {name: index for index, (name, _) in enumerate(ordered)}

        def section(name):
            if name.endswith("_divisor"):
                return "scaling"
            return "mapping"

        def order(item):
            name, (_, advanced) = item
            key = self._fields[name][0]
            if key.endswith("-kronos-panel"):
                channel = int(re.search(r"_panel_(\d+)_", name).group(1))
                return self._definitions[key].order, advanced, section(name) != "mapping", channel, original_order[name]
            if key.endswith("-kronos-model"):
                rank = 0 if name.endswith("_channel_ids") else 2 if name.endswith(("_threads", "_memory_mib")) else 1
                return self._definitions[key].order, advanced, False, rank, original_order[name]
            return self._definitions[key].order, advanced, False, 0, original_order[name]

        ordered.sort(key=order)
        shown_headers = set()
        focus_order = {}
        for name, (compact, advanced) in ordered:
            key, caption, field = self._fields[name]
            grid = (self._advanced_grids if advanced else self._grids)[key]
            location = (key, advanced)
            if key in self._kronos_input_fields and not advanced:
                self._kronos_input_fields[key].add_field(name, caption, field)
                continue
            additional = key.endswith("-kronos-panel") and "_additional_" in name
            if additional:
                detail_key = key, name.split("_additional_", 1)[0]
                grid = self._kronos_detail_grids[detail_key]
                location = detail_key
            row, column = positions.get(location, (0, 0))
            if name.endswith("_kronos_pixel_x_um") and column:
                row, column = row + 2, 0
            if key.endswith("-kronos-panel") and not additional and not field.isHidden():
                heading_key = key, section(name)
                if heading_key not in shown_headers:
                    if column:
                        row, column = row + 2, 0
                    grid.addWidget(self._kronos_headers[heading_key], row, 0, 1, 2)
                    row += 1
                    if section(name) == "scaling":
                        grid.addWidget(self._kronos_scaling_help[key], row, 0, 1, 2)
                        row += 1
                    shown_headers.add(heading_key)
                targets = focus_order.setdefault(key, [])
                if caption.buddy() is not None:
                    targets.append(caption.buddy())
            paired = compact and not self._single_column and not (key.endswith("-kronos-model") and not advanced)
            if not paired and column:
                row, column = row + 2, 0
            span = 1 if paired else 2
            if caption.maximumHeight() != 0:
                grid.addWidget(caption, row, column, 1, span, Qt.AlignmentFlag.AlignTop)
            if (name.endswith("_image_features")
                    or (name.startswith("supervised_") and name.endswith("_feature_ids"))):
                # Feature summaries and selectors reflow after edits or resize.
                # Qt's cached item can retain their earlier height at this width.
                item = QWidgetItem(field)
                item.setAlignment(Qt.AlignmentFlag.AlignTop)
                grid.addItem(item, row + 1, column, 1, span)
            else:
                grid.addWidget(field, row + 1, column, 1, span, Qt.AlignmentFlag.AlignTop)
            if not field.isHidden():
                positions[location] = (row, 1) if paired and column == 0 else (row + 2, 0)
        for key, body in self._kronos_input_fields.items():
            body.finish()
            item = QWidgetItem(body)
            item.setAlignment(Qt.AlignmentFlag.AlignTop)
            self._grids[key].addItem(item, 0, 0, 1, 2)
        for key, heading in self._kronos_headers.items():
            heading.setVisible(key in shown_headers)
        for key, explanation in self._kronos_scaling_help.items():
            explanation.setVisible((key, "scaling") in shown_headers)
        for targets in focus_order.values():
            for previous, following in zip(targets, targets[1:]):
                QWidget.setTabOrder(previous, following)
        self.updateGeometry()

    def _adapt_columns(self):
        width = self.width()
        parent = self.parentWidget()
        while parent is not None:
            if isinstance(parent, QAbstractScrollArea):
                viewport = parent.viewport()
                if self._viewport is not viewport:
                    if self._viewport is not None:
                        self._viewport.removeEventFilter(self)
                    self._viewport = viewport
                    viewport.installEventFilter(self)
                inset = max(0, self.mapTo(viewport, QPoint()).x())
                width = min(width, viewport.width() - 2 * inset)
                break
            parent = parent.parentWidget()
        minimum_field = max((
            self._fields[name][2].minimumSizeHint().width()
            for name, (compact, _advanced) in self._field_layouts.items()
            if compact and self._fields[name][0] == self.current_key
        ), default=0)
        single_column = width < max(620, minimum_field * 2 + 72)
        if single_column != self._single_column:
            self._single_column = single_column
            self._relayout_fields()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._adapt_columns()
        self._queue_card_fit()

    def showEvent(self, event):
        super().showEvent(event)
        self._adapt_columns()

    def eventFilter(self, watched, event):
        if watched is self._viewport and event.type() == QEvent.Type.Resize:
            self._adapt_columns()
        elif (watched is self._pages.get(self.current_key)
              or watched is self._advanced_pages.get(self.current_key)) and event.type() in {QEvent.Type.LayoutRequest, QEvent.Type.Resize}:
            self._queue_card_fit()
        return super().eventFilter(watched, event)

    def _queue_card_fit(self) -> None:
        """Measure wrapped content after Qt has finished the current layout pass."""
        if not self._card_fit_pending:
            self._card_fit_pending = True
            QTimer.singleShot(0, self, self._fit_current_card)

    def _fit_current_card(self) -> None:
        """Keep wrapped descriptions and summaries inside the visible card.

        Qt's width-independent minimum can shorten a card below its wrapped
        content. We use the layout's height at the current width so a compact
        window scrolls its content instead of overlapping neighbouring rows.
        """
        self._card_fit_pending = False
        page = self._pages.get(self.current_key)
        if page is not None:
            for name, (key, _caption, field) in self._fields.items():
                if key == self.current_key and "_additional_" in name and field.isVisibleTo(page):
                    required = max(0, field.layout().totalHeightForWidth(max(1, field.width())))
                    if field.minimumHeight() != required:
                        field.setMinimumHeight(required)
            height = page.layout().totalHeightForWidth(max(1, page.width()))
            if height >= 0 and page.minimumHeight() != height:
                page.setMinimumHeight(height)
            # Advanced numerical fields wrap at their own column width too.
            extra = self._advanced_pages[self.current_key]
            required = max(0, extra.layout().totalHeightForWidth(max(1, extra.width())))
            if extra.minimumHeight() != required:
                extra.setMinimumHeight(required)
            if self.candidate_index is not None:
                # Include the candidate header and disclosure in the width-dependent
                # minimum; otherwise the parent can allocate less than the card needs.
                required = self.body.totalHeightForWidth(max(1, self.width()))
                if required >= 0 and self.minimumHeight() != required:
                    self.setMinimumHeight(required)

    def _tab_selected(self, index):
        if 0 <= index < len(self._available):
            self.select(self._available[index])
            # A rejected page change leaves both the editor and tab on the
            # page whose existing validation needs attention.
            with QSignalBlocker(self.tabs):
                self.tabs.setCurrentIndex(self._available.index(self.current_key))

    def add_advanced_toggle(self, toggle):
        """Keep the shared disclosure directly below the selected page's card."""
        configure_disclosure(toggle)
        self.options.insertWidget(0, toggle, alignment=Qt.AlignmentFlag.AlignLeft)

    def add_summary(self, key, widget, *, before_controls=False, sampling=None, native_sampling=None, native_group=False, original_resolution=None):
        """Place a live fit summary beside the controls that determine it."""
        if key in self._pages:
            if key.endswith("-kronos-model"):
                self._kronos_footprints[key] = widget
                patch = next((field for name, (page_key, _caption, field) in self._fields.items()
                              if page_key == key and name.endswith("_kronos_patch_size")), None)
                if patch is not None and sampling is not None:
                    editor, horizontal, vertical = sampling
                    inputs = KronosImageInputs(widget, horizontal, vertical,
                        native_sampling=native_sampling, native_group=native_group,
                        original_resolution=original_resolution, parent=self._pages[key])
                    self._kronos_input_fields[key] = inputs
                    self._relayout_fields()
                    return inputs
                if patch is not None:
                    editor = next((caption.buddy() for name, (page_key, caption, _field) in self._fields.items()
                                   if page_key == key and name.endswith("_kronos_patch_size")), None)
                    if isinstance(editor, QComboBox):
                        patch.layout().removeWidget(editor)
                        row = QWidget(patch)
                        layout = QHBoxLayout(row)
                        layout.setContentsMargins(0, 0, 0, 0)
                        layout.setSpacing(16)
                        editor.setMinimumContentsLength(0)
                        editor.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
                        editor.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
                        layout.addWidget(editor, 0, Qt.AlignmentFlag.AlignTop)
                        layout.addWidget(widget, 1, Qt.AlignmentFlag.AlignVCenter)
                        patch.layout().insertWidget(0, row)
                    else:
                        patch.layout().addWidget(widget)
                return
            layout = self._pages[key].layout()
            if before_controls:
                layout.insertWidget(2, widget)
            else:
                layout.addWidget(widget)

    def add_kronos_preview(self, key, widget):
        self._kronos_previews[key] = widget
        self._kronos_input_fields[key].set_preview(widget)
        self._relayout_fields()
        self._adapt_columns()

    def move_control(self, name, key):
        """Move the same editor between method pages without copying its draft."""
        previous, caption, field = self._fields[name]
        if previous != key and key in self._pages:
            self._fields[name] = key, caption, field
            if re.fullmatch(r"training_\d+_resolution", name) and caption.buddy() is not None:
                editor = caption.buddy()
                preset = field.findChild(QComboBox, 'training-resolution-' + name)
                in_kronos = key.endswith('-kronos-model')
                if preset is not None:
                    preset.setVisible(not in_kronos)
                    editor.setVisible(in_kronos or preset.currentData() is None)
                caption.setVisible(not in_kronos)
                help_label = field.findChild(QLabel, 'form-help-' + name)
                if help_label is not None:
                    help_label.setVisible(not in_kronos)

    def _select_kronos_details(self, key, editor):
        active = next((name.removesuffix("_marker") for name, (page_key, caption, _field) in self._fields.items()
                       if page_key == key and caption.buddy() is editor), None)
        active_details = False
        for (page_key, prefix), panel in self._kronos_details.items():
            if page_key == key:
                applicable = any(not field.isHidden() for name, (_key, _caption, field) in self._fields.items()
                                 if name.startswith(prefix + "_additional_"))
                panel.setVisible(prefix == active and applicable)
                active_details |= prefix == active and applicable
        self._kronos_mappings[key].set_details_available(active_details)
        self.updateGeometry()

    def capture_marker_state(self):
        """Retain the focused channel and details view across a form revisit."""
        result = {}
        for key, mapping in self._kronos_mappings.items():
            active = next((name for name, (page, caption, _field) in self._fields.items()
                           if page == key and caption.buddy() is mapping._active), None)
            result[key] = {"assignment": active, "details": mapping._adding,
                           "search": mapping.search.text(), "new_name": mapping.new_name.text(),
                           "name_drafts": {name: mapping._name_drafts[caption.buddy()]
                                           for name, (page, caption, _field) in self._fields.items()
                                           if page == key and caption.buddy() in mapping._name_drafts}}
        return result

    def restore_marker_state(self, values):
        for key, state in values.items():
            mapping = self._kronos_mappings.get(key)
            assignment = self._fields.get(state.get("assignment"))
            if mapping is None or assignment is None or assignment[2].isHidden():
                continue
            for name, value in state.get("name_drafts", {}).items():
                field = self._fields.get(name)
                if field is not None and field[0] == key:
                    mapping._name_drafts[field[1].buddy()] = value
            mapping.select_assignment(assignment[1].buddy())
            mapping.search.setText(state.get("search", ""))
            if state.get("details"):
                mapping._show_additional()
                mapping.new_name.setText(state.get("new_name", mapping.new_name.text()))
            else:
                mapping.new_name.setText(state.get("new_name", mapping.new_name.text()))
                mapping._show_choices()

    def primary_row_selected(self, name):
        """Keep rows beyond the chosen count as drafts, outside submitted inputs."""
        match = re.fullmatch(r"(primary_\d+)_(class)_(\d+)_(?:name|id)", name)
        if match is None:
            return True
        prefix, kind, row = match.groups()
        count_field = self._fields.get(f"{prefix}_{kind}_count")
        if count_field is None:
            return True
        value = count_field[1].buddy().currentData()
        return value is not None and int(row) < int(value)

    def sync(self):
        """Recompute available pages from the renderer's conditional field states."""
        previous = self.current_key
        for name, (_key, caption, field) in self._fields.items():
            if not self.primary_row_selected(name):
                caption.hide()
                field.hide()
        for name, (_key, caption, field) in self._fields.items():
            if not name.endswith("_ignore_policy"):
                continue
            count = self._fields.get(name.removesuffix("_ignore_policy") + "_class_count")
            if count is None:
                continue
            policy = caption.buddy()
            single_class = count[1].buddy().currentData() == "1"
            description = self._pages[_key].findChild(QLabel, "description-" + _key)
            text = self._definitions[_key].description
            if single_class:
                text += " With one tissue class, we use Ignore as the second training class."
            description.setText(text)
            if single_class:
                caption.hide()
                field.hide()
                if name not in self._multiclass_ignore_policies:
                    self._multiclass_ignore_policies[name] = policy.currentData()
                policy.setCurrentIndex(policy.findData("annotated"))
            elif name in self._multiclass_ignore_policies:
                previous_policy = self._multiclass_ignore_policies.pop(name)
                policy.setCurrentIndex(policy.findData(previous_policy))
        available = {
            key for key, _caption, field in self._fields.values() if not field.isHidden()
        }
        if self.candidate_index is not None:
            available = {key for key in available if not re.match(r"\d+-", key)
                         or key.startswith(f"{self.candidate_index}-")}
        self._available = sorted(available, key=lambda key: self._definitions[key].order)
        if self.current_key != "classifiers" and self.current_key not in self._available:
            self.current_key = self._available[0] if self._available else None
        for mapping in self._kronos_mappings.values():
            mapping.sync_assignments()
        for key, mapping in self._kronos_mappings.items():
            self._select_kronos_details(key, mapping._active)
        self._relayout_fields()
        self._adapt_columns()
        self._show_current()
        if previous != self.current_key:
            self.page_changed.emit()

    def _show_current(self):
        overview = self.current_key == "classifiers"
        if self.candidate_index is not None:
            self.candidate_status.setVisible(self.current_key not in {"references", f"{self.candidate_index}-channels"})
        if self.classifier_overview is not None:
            self.classifier_overview.setVisible(overview)
            self.candidate_header.setVisible(not overview)
            self.advanced.setVisible(not overview)
            if overview:
                self.setMinimumHeight(0)
        for key, page in self._pages.items():
            page.setVisible(key == self.current_key)
            self._advanced_pages[key].setVisible(key == self.current_key)
        count = len(self._available)
        index = self._available.index(self.current_key) if self.current_key in self._available else -1
        short_titles = {"Class labels": "Classes", "Image channels": "Channels", "Classifier inputs": "Inputs", "Cell measurements": "Inputs",
                        "Fitting and validation": "Training", "Subject allocation": "Subjects",
                        "Image preparation": "Preparation", "Reference annotations": "References",
                        "KRONOS image inputs": "Images", "KRONOS channel inputs": "Markers", "Channels to measure": "Channels", "Region count limits": "Region counts"}
        labels = [short_titles.get(self._definitions[key].title, self._definitions[key].title.removesuffix(" measurements"))
                  for key in self._available]
        with QSignalBlocker(self.tabs):
            if [self.tabs.tabData(i) for i in range(self.tabs.count())] != self._available:
                while self.tabs.count():
                    self.tabs.removeTab(0)
                for number, (key, title) in enumerate(zip(self._available, labels), 1):
                    tab = self.tabs.addTab(f"{number:02d}  {title}")
                    self.tabs.setTabData(tab, key)
                    self.tabs.setTabToolTip(tab, self._definitions[key].title)
            self.tabs.setCurrentIndex(index)
        self.tabs.setVisible(not overview and count > 1)
        self.position.setText(f"Page {index + 1} of {count}" if count else "")
        self.back.setVisible(index > 0 or (index == 0 and self.candidate_index is not None))
        if self.candidate_index is not None:
            self.back.setText(self._definition_back_caption if index == 0 and self.classifier_overview is None else "Back")
        self.next.setVisible(0 <= index < count - 1)
        if 0 <= index < count - 1:
            self.next.setText("Next: " + self._definitions[self._available[index + 1]].title.split(" · ")[0])
        self.navigation.setVisible(not overview and count > 1)
        self._queue_card_fit()
        self.updateGeometry()

    @property
    def current_phase(self):
        return self._definitions[self.current_key].phase if self.current_key in self._definitions else 0

    @property
    def at_last_page(self):
        return bool(self._available) and self.current_key == self._available[-1]

    def _back(self):
        if self.candidate_index is not None and self.current_key in self._available[:1]:
            if self.classifier_overview is None:
                self.definition_back_requested.emit()
            else:
                self.show_classifiers()
        else:
            self._move(-1)

    def _move(self, offset):
        if self.current_key not in self._available:
            return
        index = self._available.index(self.current_key) + offset
        if 0 <= index < len(self._available):
            self.select(self._available[index])

    def select(self, key, *, validate=True):
        """Change pages after the owning form's existing boundary validation."""
        if key == "classifiers":
            self.show_classifiers()
            return
        if key not in self._available or key == self.current_key:
            return
        previous = self.current_key
        forward = previous in self._available and self._available.index(key) > self._available.index(previous)
        if validate and forward and not self._before_change(self._definitions[previous], self._definitions[key]):
            return
        self.current_key = key
        self._adapt_columns()
        self._show_current()
        self.page_changed.emit()
        self._pages[key].findChild(QLabel).setFocus(Qt.FocusReason.OtherFocusReason)

    def reveal(self, name):
        """Open the page containing a control whose validation needs attention."""
        if name in self._fields:
            key = self._fields[name][0]
            if key.endswith("-kronos-panel") and "_additional_" in name:
                marker = self._fields.get(name.split("_additional_", 1)[0] + "_marker")
                if marker is not None:
                    self._kronos_mappings[key].select_assignment(marker[1].buddy())
                    self._kronos_mappings[key]._show_additional()
            if self.candidate_index is not None and re.match(r"\d+-", key):
                self.select_candidate(int(key.split("-", 1)[0]))
            self.select(key, validate=False)
