"""Presentation vocabulary. Never apply these functions to stored identities.

The interface keeps editable researcher choices associated with the current image or
analysis request. Values are passed to the stage-specific validator before they become
accepted processing settings."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
import re


def cell_measurement_label(compartment: str, measurement: str) -> str:
    """Return the display name of an intensity quantity and its cell region.

    We translate the recorded QuPath quantity and compartment for controls and
    plot axes. Stored measurement names and source identifiers remain unchanged.
    Unrecognized quantities retain their supplied names.
    """
    quantity = {"Mean": "Mean intensity", "Median": "Median intensity", "Min": "Minimum intensity",
                "Max": "Maximum intensity", "Std.Dev.": "Intensity standard deviation", "Variance": "Intensity variance"}.get(measurement, measurement)
    region = {"cell": "whole cell", "nucleus": "nucleus", "cytoplasm": "cytoplasm", "membrane": "membrane"}.get(compartment.lower(), compartment)
    return quantity + " · " + region


FIELD_LABELS = {
    "spectral_state_override": "Does this image still need spectral unmixing?",
    "solver": "Unmixing method",
    "af_behavior": "Tissue autofluorescence",
    "chunk_width": "Processing block width (pixels)",
    "chunk_height": "Processing block height (pixels)",
    "calculation_precision": "Calculation precision",
    "output_dtype": "Output image precision",
    "nnls_tolerance": "NNLS stopping tolerance",
    "nnls_max_iterations": "Maximum NNLS iterations",
    "overwrite": "Replace the previous processed image at this destination",
    "moving_reference_path": "Paired image",
    "moving_reference_modality": "Paired-image modality",
    "moving_reference_id": "Paired-image name",
    "moving_reference_source_image_id": "Paired-image source",
    "moving_reference_metadata": "Paired-image information",
    "channel_name": "Image channel",
    "channel_id": "Channel identifier",
    "component_name": "Fluorophore / component",
    "fluorophore": "Fluorophore",
    "source_fluorophore": "Recorded fluorophore",
    "biomarker": "Marker name",
    "raw_mean": "Mean across cells",
    "raw_median": "Median across cells",
    "assignment": "Marker assignment",
    "validation_domain_id": "Processing group",
    "pending_interaction_reason": "Review needed",
    "resolved_config_hash": "Settings fingerprint",
    "resolved_config_sha256": "Settings fingerprint",
    "provenance_hash": "Processing-record fingerprint",
    "stage1_channel_rows": "Channels and markers",
    "batch_interaction_groups": "Required reviews",
    "model_method": "Classifier",
    "evaluation_intent": "Accuracy assessment",
    "training_patient_count": "Training subjects",
    "validation_patient_count": "Validation subjects",
    "test_patient_count": "Test subjects",
    "macro_precision": "Mean precision",
    "macro_recall": "Mean recall",
    "macro_f1": "Mean F1 score",
    "count": "Count",
    "value": "Value",
    "identifier": "Identifier",
    "roi_id": "Region",
    "analysis_domain_id": "Analysis region",
    "native_pyramid_inventory": "Available image resolutions",
    "x_downsample": "Horizontal scale factor",
    "y_downsample": "Vertical scale factor",
    "x_um_per_px": "Pixel width (µm)",
    "y_um_per_px": "Pixel height (µm)",
    "node_id": "Processing record",
    "dependency_ids": "Required preceding steps",
    "artifact_kind": "Output type",
    "failure_code": "Recorded issue",
    "selected_content_keys": "Selected records",
    "qupath_project_entry": "QuPath image",
}
_TOKENS = {
    "id": "ID",
    "ids": "IDs",
    "roi": "ROI",
    "rois": "ROIs",
    "qupath": "QuPath",
    "mif": "mIF",
    "svm": "SVM",
    "rf": "RF",
    "gnn": "GNN",
    "pca": "PCA",
    "ome": "OME",
    "tiff": "TIFF",
    "sha256": "SHA-256",
    "f1": "F1",
    "qc": "QC",
    "um": "µm",
}


# ==============================================================================
#                                  Terminology
# ==============================================================================


def field_label(key: str) -> str:
    """Caption a schema field; values and identifiers must not enter this path."""
    if key in FIELD_LABELS:
        return FIELD_LABELS[key]
    words = [_TOKENS.get(word.lower(), word) for word in key.split("_")]
    text = " ".join(words)
    return text[:1].upper() + text[1:]


def control_label(name: str, supplied: str) -> str:
    """Prefer explicit semantic captions; retain genuinely user-derived labels."""
    if name in FIELD_LABELS:
        return FIELD_LABELS[name]
    return field_label(supplied) if supplied == name else supplied


_METHODS = {
    "marker_rules": "Marker thresholds",
    "supervised_classifier": "Supervised classification",
    "unsupervised_clustering": "Unsupervised phenotyping",
    "RBF": "Radial basis function (RBF)",
    "LINEAR": "Linear",
    "rtrees": "Random forest (RF)",
    "svm": "Support vector machine (SVM)",
    "marker_based": "Marker thresholds",
    "supervised": "Supervised classification",
    "unsupervised": "Unsupervised phenotyping",
    "marker_threshold": "Marker thresholds",
    "per_slide": "Separate analysis for each image",
    "pooled": "Pooled",
    "training_only": "Training only",
    "train_validation_test": "Training, validation and test subjects",
    "triangle": "Triangle",
    "gmm": "Two-component Gaussian mixture",
    "gamma": "Two-component Gamma mixture",
    "manual": "Manual threshold",
    "rbf": "Radial basis function (RBF)",
    "linear": "Linear",
}


def choice_label(name: str, value: str) -> str:
    """Only translate fields with declared enum semantics, not marker/class names."""
    from .reference_copy import SPECTRAL_CHOICE_LABELS

    specific = {
        **SPECTRAL_CHOICE_LABELS,
        "spectral_state_override": {
            "raw": "Yes — separate the fluorophore signals (mixed image)",
            "unmixed": "No — fluorophore signals are already separated (previously unmixed)",
            "isolated": "No — fluorophore signals are already separated (isolated by measurement bands)",
        },
        "solver": {
            "nnls": "Non-negative least squares (NNLS)",
            "clipped_ls": "Clipped least squares",
        },
        "calculation_precision": {"float32": "32-bit floating point", "float64": "64-bit floating point"},
        "output_dtype": {
            "float32": "32-bit floating point (recommended)",
            "float64": "64-bit floating point",
        },
    }
    for suffix, captions in {
        "_normalization": {
            "none": "Keep feature values unchanged",
            "mean_variance": "Standardize each feature (z-scoring)",
            "min_max": "Scale each feature to 0–1 (min–max)",
        },
    }.items():
        if name.endswith(suffix):
            return captions.get(value, field_label(value))
    if name in specific:
        return specific[name].get(value, field_label(value))
    if re.fullmatch(r"stage1_\d+_assignment", name):
        return {"assigned": "Marker assigned", "none": "No marker"}.get(value, value)
    is_enum = (
        name.endswith(("_model_method", "_svm_kernel"))
        or re.fullmatch(r"m\d+(?:_s\d+)?_source", name) is not None
        or name
        in {
            "phenotyping_mode",
            "clustering_mode",
            "mode",
            "evaluation_intent",
            "detector_source",
            "native_level_selection_mode",
            "scope",
            "option_value",
        }
    )
    if not is_enum:
        return value
    return _METHODS.get(value, field_label(value) if "_" in value else value)


WAITING_FOR_INPUTS = (
    "This step becomes available after the preceding steps and required choices are complete."
)
REVIEW_REQUIRED = "Complete the required choices or annotations, then submit this step to continue."
PROCESSING = "Processing is in progress. The next required decision will appear here."


_STATUS_CAPTIONS = {
    "completed": "Completed",
    "failed": "Stopped",
    "needs_interaction": "Needs review",
    "paused_resumable": "Paused",
    "active_current_session": "Processing",
    "non_resumable": "Stopped — review details",
    "waiting": "Not yet selected",
    "planned": "Not yet selected",
    "ready": "Ready",
    "running": "Processing",
    "accepted": "Accepted",
    "rejected": "Not accepted",
    "training_only": "Training only",
    "not_started": "Not yet selected",
}


def table_value_label(column: str, value: object) -> str:
    """Format declared display columns while retaining stored names and identities."""
    if value is None:
        return ""
    if column in {"raw_mean", "raw_median"} and type(value) in {int, float}:
        return format(value, ".6g")
    if type(value) is str and column in {
        "status",
        "state",
        "execution_state",
        "resume_disposition",
    }:
        return _STATUS_CAPTIONS.get(value, value)
    if column == "required_by_node_kinds" and isinstance(value, (tuple, list)):
        from .presenters.workflow import BY_KIND

        return ", ".join(
            dict.fromkeys(
                BY_KIND[item].title if item in BY_KIND else field_label(item) for item in value
            )
        )
    if type(value) is str and column in {
        "current_or_final_stage",
        "stage",
        "node_kind",
        "required_stage",
    }:
        from .presenters.workflow import BY_KIND

        definition = BY_KIND.get(value)
        return definition.title if definition is not None else field_label(value)
    return str(value)
