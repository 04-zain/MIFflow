"""

Lists the QuPath scripts included in the source, installed Python package and portable application.

Each workflow entry identifies an editable script under qupath/scripts.
The build copies that script into the matching relative directory under
mif_pipeline/qupath_runtime/assets. The application uses the same list when
preparing the scripts for an image-processing or editing operation.

We maintain the four internal software checks beside the workflow scripts
under qupath/scripts/general. Their shared relative paths tell the package
builder where to stage installed resources and tell the portable collector
which installed checks to include.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from enum import Enum
from pathlib import PurePosixPath

# ==============================================================================
#                       Workflow and software-check scripts
# ==============================================================================


# These installed software checks accompany both wheel and frozen-app builds.
# Their locations are shared by setup.py and the portable resource collector.
INTERNAL_PROBE_PATHS = (
    "general/03_probe_prerequisites.groovy",
    "general/04_probe_class_origins.groovy",
    "general/05_seed_managed_profile.groovy",
    "general/06_probe_warpy.groovy",
)


class QuPathScriptAsset(str, Enum):
    """

    Associates each supported native operation with its source Groovy file.

    The enum's declaration order defines the order returned by the complete
    script list. Its string values preserve the source path used by setup.py,
    while relative_script_path() derives the installed location beneath the
    assets directory. Individual operations select the entries they require
    before preparing their native launch.
    """

    ANNOTATE_SPECTRAL_REFERENCES = (
        "qupath/scripts/preprocessing/01_annotate_spectral_references.groovy"
    )
    REFERENCE_ANNOTATION_WORKBENCH = (
        "qupath/scripts/preprocessing/reference_annotation_workbench.groovy"
    )
    NATIVE_ANNOTATION_PREVIEW = (
        "qupath/scripts/compartment_analysis/native_annotation_preview.groovy"
    )
    ANNOTATION_PREVIEW_WORKBENCH = (
        "qupath/scripts/compartment_analysis/annotation_preview_workbench.groovy"
    )
    REFERENCE_FIRST_GUARD = "qupath/scripts/general/reference_first_guard.groovy"
    CLASSIFIER_REVIEW_POLICY = "qupath/scripts/compartment_analysis/classifier_review_policy.groovy"
    CLASSIFIER_REVIEW = "qupath/scripts/compartment_analysis/classifier_review.groovy"
    ANNOTATION_TOOLS = "qupath/scripts/general/annotation_tools.groovy"
    NATIVE_PREDICTION_DISPLAY = "qupath/scripts/general/native_prediction_display.groovy"
    PREDICTION_DISPLAY = "qupath/scripts/general/prediction_display.groovy"
    CLUSTER_ANNOTATIONS = "qupath/scripts/general/06_cluster_annotations.groovy"
    CLUSTER_ANNOTATION_ENTRY = "qupath/scripts/general/07_cluster_annotation_entry.groovy"
    RESTORE_INTERRUPTED_SEGMENTATION = "qupath/scripts/general/10_restore_interrupted_segmentation.groovy"
    CORRECT_KNOWN_CELL = "qupath/scripts/cell_analysis/09_correct_known_cell.groovy"
    SETUP_PROJECT = "qupath/scripts/general/00_setup_project.groovy"
    RESEARCHER_DESKTOP_BOOTSTRAP = "qupath/scripts/general/05_researcher_desktop.groovy"
    EXPORT_CLASSIFIED_REGION_GEOMETRY = "qupath/scripts/general/09_export_classified_region_geometry.groovy"
    EXPORT_REGION_SELECTION = "qupath/scripts/general/08_select_export_region.groovy"
    KRONOS_GUI_TOOLS = "qupath/scripts/compartment_analysis/kronos_gui_tools.groovy"
    GROUPED_CROSS_VALIDATION = "qupath/scripts/compartment_analysis/grouped_cross_validation.groovy"
    UPSERT_BATCH_PROJECT_ENTRY = "qupath/scripts/general/01_upsert_batch_project_entry.groovy"
    TRAIN_NATIVE_CLASSIFIER = "qupath/scripts/general/02_train_native_classifier.groovy"
    EVALUATE_HELD_OUT_CLASSIFIERS = "qupath/scripts/general/04_evaluate_held_out_classifiers.groovy"
    VERIFY_STAGE_SCRIPT_BUNDLE = "qupath/scripts/general/00_verify_stage_script_bundle.groovy"
    BATCH_STAGE_GUARD = "qupath/scripts/general/batch_stage_guard.groovy"
    AUTHENTICATE_SEMANTIC_LIVE_AUTHORITY = (
        "qupath/scripts/roi_validation/00_authenticate_semantic_live_authority.groovy"
    )
    SETUP_TISSUE_DETECTOR = "qupath/scripts/tissue_detection/00_setup_tissue_detector.groovy"
    TISSUE_AUTHORING_DRAFT = "qupath/scripts/tissue_detection/tissue_authoring_draft.groovy"
    TISSUE_AUTHORING = "qupath/scripts/tissue_detection/tissue_authoring.groovy"
    PIXEL_FEATURE_WORKBENCH = "qupath/scripts/general/pixel_feature_workbench.groovy"
    TISSUE_WORKBENCH = "qupath/scripts/tissue_detection/tissue_workbench.groovy"
    TISSUE_DETECTOR_SUPPORT = "qupath/scripts/tissue_detection/tissue_detector_support.groovy"
    DETECT_TISSUE_SECTIONS = (
        "qupath/scripts/tissue_detection/01_detect_and_review_tissue_sections.groovy"
    )
    SNAPSHOT_ANALYSIS_DOMAINS = "qupath/scripts/analysis_domain/01_snapshot_analysis_domains.groovy"
    SNAPSHOT_TRAINING_ANNOTATIONS = (
        "qupath/scripts/compartment_analysis/01_snapshot_training_annotations.groovy"
    )
    PATCH_GEOMETRY = "qupath/scripts/compartment_analysis/patch_geometry.groovy"
    KRONOS_WORKER_CLIENT = "qupath/scripts/compartment_analysis/kronos_worker_client.groovy"
    KRONOS_PATCH_BACKEND = "qupath/scripts/compartment_analysis/kronos_patch_backend.groovy"
    KRONOS_PATCH_OPERATION = "qupath/scripts/compartment_analysis/kronos_patch_operation.groovy"
    KRONOS_MODEL_CODEC = "qupath/scripts/compartment_analysis/kronos_model_codec.groovy"
    KRONOS_CLASSIFIER_RESOURCE = (
        "qupath/scripts/compartment_analysis/kronos_classifier_resource.groovy"
    )
    KRONOS_FINAL_TRAINING = "qupath/scripts/compartment_analysis/kronos_final_training.groovy"
    KRONOS_MARKER_STATISTICS = "qupath/scripts/compartment_analysis/kronos_marker_statistics.groovy"
    KRONOS_BUNDLE_SUPPORT = "qupath/scripts/compartment_analysis/kronos_bundle_support.groovy"
    KRONOS_LIVE_PREVIEW = "qupath/scripts/compartment_analysis/kronos_live_preview.groovy"
    APPLY_PRIMARY_COMPARTMENT_CLASSIFIER = (
        "qupath/scripts/compartment_analysis/02_apply_primary_compartment_classifier.groovy"
    )
    APPLY_SECONDARY_STRUCTURE_CLASSIFIERS = (
        "qupath/scripts/compartment_analysis/03_apply_secondary_structure_classifiers.groovy"
    )
    RETIRE_SAVED_RESULTS = "qupath/scripts/cell_analysis/08_retire_saved_results.groovy"
    RUN_INSTANSEG = "qupath/scripts/cell_analysis/04_run_instanseg.groovy"
    ALTERNATIVE_SEGMENTATION = "qupath/scripts/cell_analysis/alternative_segmentation.groovy"
    PREPARE_PHENOTYPING_MEASUREMENTS = (
        "qupath/scripts/cell_analysis/05_prepare_phenotyping_measurements.groovy"
    )
    NIMBUS_IMAGE_SOURCE = "qupath/scripts/cell_analysis/nimbus_image_source.groovy"
    SNAPSHOT_SUPERVISED_TRAINING_EVIDENCE = (
        "qupath/scripts/cell_analysis/05_snapshot_supervised_training_evidence.groovy"
    )
    APPLY_KNOWN_MARKER_CELL_CLASSIFIERS = (
        "qupath/scripts/cell_analysis/05_apply_known_marker_cell_classifiers.groovy"
    )
    KRONOS_CELL_FEATURES = "qupath/scripts/cell_analysis/kronos_cell_features.groovy"
    SUPERVISED_CLASSIFIER_SUPPORT = "qupath/scripts/cell_analysis/supervised_classifier_support.groovy"
    TRAIN_SUPERVISED_PHENOTYPES = (
        "qupath/scripts/cell_analysis/05_train_supervised_phenotypes.groovy"
    )
    EXPORT_CELL_AND_ROI_OBJECTS = (
        "qupath/scripts/cell_analysis/06_export_cell_and_roi_objects.groovy"
    )
    IMPORT_PYTHON_CELL_LABELS = "qupath/scripts/cell_analysis/07_import_python_cell_labels.groovy"
    EXPORT_SEMANTIC_CANDIDATE_EVIDENCE = (
        "qupath/scripts/roi_validation/08_export_semantic_candidate_evidence.groovy"
    )
    FINALIZE_SEMANTIC_ROI_RESULTS = (
        "qupath/scripts/roi_validation/09_finalize_semantic_roi_results.groovy"
    )
    ADD_MOVING_IMAGE = "qupath/scripts/registration/09_add_moving_image.groovy"
    LANDMARK_EXCHANGE = "qupath/scripts/registration/landmark_exchange.groovy"
    REGISTRATION_EDITOR = "qupath/scripts/registration/registration_editor_worker.groovy"
    AUTOMATIC_ELASTIX = "qupath/scripts/registration/automatic_elastix.groovy"
    AUTOMATIC_BSPLINE = "qupath/scripts/registration/automatic_bspline.groovy"
    AUTOMATIC_ALIGNMENT_ENTRY = "qupath/scripts/registration/automatic_alignment_entry.groovy"
    PAIRED_GEOMETRY = "qupath/scripts/registration/paired_geometry.groovy"
    PAIRED_GUI = "qupath/scripts/registration/paired_gui.groovy"
    IMPORT_WARPY_REGISTRATION = "qupath/scripts/registration/10_import_warpy_registration.groovy"
    TRANSFER_WARPY_OBJECTS = "qupath/scripts/registration/11_transfer_warpy_objects.groovy"
    COMMIT_WARPY_OBJECTS = "qupath/scripts/registration/12_commit_warpy_objects.groovy"
    CLEANUP_WARPY_OBJECTS = "qupath/scripts/registration/13_cleanup_warpy_objects.groovy"
    PROJECT_STAGE_BOOTSTRAP = "qupath/scripts/general/project_stage_bootstrap.groovy"
    SUPERVISED_PREDICTION_REUSE = "qupath/scripts/cell_analysis/supervised_prediction_reuse.groovy"
    SUPERVISED_REFERENCES = "qupath/scripts/cell_analysis/supervised_references.groovy"


def relative_script_path(asset: QuPathScriptAsset) -> PurePosixPath:
    """

    Derives a registered script's path within the installed assets directory.

    For example, qupath/scripts/registration/paired_gui.groovy becomes
    registration/paired_gui.groovy. We use the same category and filename when
    copying source scripts into a build or preparing a native operation.

    Args:
        asset: QuPathScriptAsset identifying the selected source script.

    Returns:
        Forward-slash relative path beneath mif_pipeline/qupath_runtime/assets.
    """

    return PurePosixPath(asset.value).relative_to(PurePosixPath("qupath") / "scripts")


def registered_script_assets() -> tuple[QuPathScriptAsset, ...]:
    """

    Returns every supported workflow script in the registry's declared order.

    The complete sequence lets builds and installed-resource checks compare
    their required script sets with the application. A native operation can
    select its smaller required subset from these same entries.

    Returns:
        Tuple containing all QuPathScriptAsset entries.
    """

    return tuple(QuPathScriptAsset)


__all__ = [
    "QuPathScriptAsset",
    "registered_script_assets",
    "relative_script_path",
]
