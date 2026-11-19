"""Structured, resumable outcomes shared by ROI-analysis stages.

The `StageResult` record carries `stage`, `slide_id`, `status`, `generation_id`,
`interaction_reason` and its remaining declared fields.

These records and helpers define shared values used by several stages. Construction and
parsing validate the declared fields before the values are used to coordinate scientific
inputs, results or runtime resources."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any

from .errors import ROIAnalysisError

# ==============================================================================
#                                  Stage result
# ==============================================================================


class StageStatus(str, Enum):
    """Machine-readable terminal state for one stage invocation."""

    COMPLETED = "completed"
    NEEDS_INTERACTION = "needs_interaction"
    FAILED = "failed"


class InteractionReason(str, Enum):
    """Explicit reasons for pausing a deterministic headless workflow."""

    TISSUE_DETECTOR_SETUP_REQUIRED = "tissue_detector_setup_required"
    TISSUE_NATIVE_LEVEL_REQUIRED = "tissue_native_level_required"
    TISSUE_DETECTION_CHANNEL_REQUIRED = "tissue_detection_channel_required"
    TISSUE_SECTION_REVIEW_REQUIRED = "tissue_section_review_required"
    WARPY_REGISTRATION_REQUIRED = "warpy_registration_required"
    REGISTRATION_REVIEW_REQUIRED = "registration_review_required"
    TRANSFER_REVIEW_REQUIRED = "transfer_review_required"
    TRANSFER_COMMIT_RECOVERY_REQUIRED = "transfer_commit_recovery_required"
    TRAINING_PLAN_SELECTION_REQUIRED = "training_plan_selection_required"
    PRIMARY_TRAINING_ANNOTATIONS_REQUIRED = "primary_training_annotations_required"
    PRIMARY_CLASSIFIER_REVIEW_REQUIRED = "primary_classifier_review_required"
    SEGMENTATION_SETUP_REQUIRED = "segmentation_setup_required"
    MEASUREMENT_SETUP_REQUIRED = "measurement_setup_required"
    SECONDARY_TRAINING_ANNOTATIONS_REQUIRED = "secondary_training_annotations_required"
    ROI_REVIEW_REQUIRED = "roi_review_required"
    BIOMARKER_THRESHOLD_REQUIRED = "biomarker_threshold_required"
    PHENOTYPING_MODE_REQUIRED = "phenotyping_mode_required"
    PHENOTYPE_DEFINITION_COUNT_REQUIRED = "phenotype_definition_count_required"
    PHENOTYPE_CLASSES_REQUIRED = "phenotype_classes_required"
    PHENOTYPING_MEASUREMENTS_REQUIRED = "phenotyping_measurements_required"
    MARKER_THRESHOLD_REVIEW_REQUIRED = "marker_threshold_review_required"
    MARKER_COMPOSITE_RULES_REQUIRED = "marker_composite_rules_required"
    MARKER_RULES_REVIEW_REQUIRED = "marker_rules_review_required"
    SUPERVISED_PHENOTYPE_CLASSES_REQUIRED = "supervised_phenotype_classes_required"
    SUPERVISED_TRAINING_ANNOTATIONS_REQUIRED = "supervised_training_annotations_required"
    SUPERVISED_CLASSIFIER_REVIEW_REQUIRED = "supervised_classifier_review_required"
    CLUSTERING_CONFIGURATION_REQUIRED = "clustering_configuration_required"
    CLUSTER_LABEL_REVIEW_REQUIRED = "cluster_label_review_required"
    SECONDARY_ROI_OVERLAP_POLICY_REQUIRED = "secondary_roi_overlap_policy_required"
    ANALYSIS_DOMAIN_CHOICE_REQUIRED = "analysis_domain_choice_required"
    ANALYSIS_DOMAIN_ROIS_REQUIRED = "analysis_domain_rois_required"
    ANALYSIS_DOMAIN_REVIEW_REQUIRED = "analysis_domain_review_required"
    STRUCTURE_COUNT_POLICY_ACKNOWLEDGEMENT_REQUIRED = (
        "structure_count_policy_acknowledgement_required"
    )
    PRIMARY_STRUCTURE_COUNT_REVIEW_REQUIRED = "primary_structure_count_review_required"
    SECONDARY_STRUCTURE_SELECTION_REQUIRED = "secondary_structure_selection_required"
    SEMANTIC_REGION_DECISION_REQUIRED = "semantic_region_decision_required"
    OTHER_EXPLICIT_REASON = "other_explicit_reason"


INTERACTION_ACTION_SCHEMA_VERSION = "roi-interaction-actions/v2"


class InteractionAction(str, Enum):
    """Versioned UI/orchestrator actions emitted by interaction checkpoints."""

    ACCEPT = "accept"
    ABORT = "abort"
    ANNOTATE_PRIMARY = "annotate_primary"
    ANNOTATE_SECONDARY = "annotate_secondary"
    ACCEPT_REGISTRATION = "accept_registration"
    ACCEPT_REGISTRATION_AND_TRANSFER = "accept_registration_and_transfer"
    ACCEPT_TRANSFERS = "accept_transfers"
    REJECT_TRANSFERS = "reject_transfers"
    RETRY_TRANSFER_COMMIT = "retry_transfer_commit"
    QUARANTINE_TRANSFER_ATTEMPT = "quarantine_transfer_attempt"
    ABORT_TRANSFER_ATTEMPT = "abort_transfer_attempt"
    CREATE_THRESHOLDER = "create_thresholder"
    EDIT = "edit"
    MANUAL_ANNOTATION = "manual_annotation"
    NO_MOVING_REFERENCE = "no_moving_reference"
    MOVING_REFERENCE_AVAILABLE = "moving_reference_available"
    IMPORT_WARPY_TRANSFORM = "import_warpy_transform"
    REJECT = "reject"
    REPEAT = "repeat"
    REGISTER_WITH_WARPY = "register_with_warpy"
    RESTRICT_CLASSIFIER_TO_SLIDE = "restrict_classifier_to_slide"
    RETRAIN_CLASSIFIER = "retrain_classifier"
    REUSE_CLASSIFIER_FOR_VALIDATION_DOMAIN = "reuse_classifier_for_validation_domain"
    REVIEW_CLASSIFIER = "review_classifier"
    REVIEW_ROIS = "review_rois"
    SELECT_TRAINING_PLAN = "select_training_plan"
    CONFIGURE_SEGMENTATION = "configure_segmentation"
    CONFIGURE_MEASUREMENTS = "configure_measurements"
    SELECT_TISSUE_DETECTOR = "select_tissue_detector"
    IMPORT_TISSUE_DETECTOR = "import_tissue_detector"
    REFRESH_TISSUE_DETECTORS = "refresh_tissue_detectors"
    SELECT_TISSUE_NATIVE_LEVEL = "select_tissue_native_level"
    SELECT_CHANNEL = "select_channel"
    SET_BIOMARKER_THRESHOLD = "set_biomarker_threshold"
    SIDE_BY_SIDE = "side_by_side"
    SET_MOVING_ASSISTANCE_SCOPES = "set_moving_assistance_scopes"
    SKIP_MOVING_ASSISTANCE = "skip_moving_assistance"
    SKIP_MOVING_REFERENCE = "skip_moving_reference"
    TRAIN_CLASSIFIER = "train_classifier"
    SELECT_PHENOTYPING_MODE = "select_phenotyping_mode"
    DECLARE_PHENOTYPE_COUNT = "declare_phenotype_count"
    DECLARE_PHENOTYPE_CLASSES = "declare_phenotype_classes"
    SELECT_PHENOTYPING_MEASUREMENTS = "select_phenotyping_measurements"
    REVIEW_MARKER_THRESHOLDS = "review_marker_thresholds"
    DEFINE_MARKER_COMPOSITE_RULES = "define_marker_composite_rules"
    REVIEW_MARKER_RULES = "review_marker_rules"
    ANNOTATE_SUPERVISED_TRAINING = "annotate_supervised_training"
    REVIEW_SUPERVISED_CLASSIFIER = "review_supervised_classifier"
    CONFIGURE_CLUSTERING = "configure_clustering"
    ASSIGN_CLUSTER_LABELS = "assign_cluster_labels"
    ACCEPT_UNLABELED_CLUSTERS = "accept_unlabeled_clusters"
    REVISE_CLUSTERING = "revise_clustering"
    SELECT_SECONDARY_ROI_OVERLAP_POLICY = "select_secondary_roi_overlap_policy"
    USE_WHOLE_TISSUE = "use_whole_tissue"
    SELECT_ANALYSIS_ROIS = "select_analysis_rois"
    OPEN_QUPATH = "open_qupath"
    REVIEW_ANALYSIS_ROIS = "review_analysis_rois"
    ACCEPT_ANALYSIS_ROIS = "accept_analysis_rois"
    EDIT_ANALYSIS_ROIS = "edit_analysis_rois"
    ACKNOWLEDGE_STRUCTURE_COUNT_POLICY = "acknowledge_structure_count_policy"
    REVISE_STRUCTURE_COUNT_POLICY = "revise_structure_count_policy"
    ACCEPT_ALL_PRIMARY_STRUCTURES = "accept_all_primary_structures"
    EXCLUDE_ANALYSIS_DOMAIN = "exclude_analysis_domain"
    ACCEPT_SECONDARY_STRUCTURE_SELECTION = "accept_secondary_structure_selection"
    ACCEPT_SEMANTIC_REGION = "accept_semantic_region"
    REJECT_SEMANTIC_REGION = "reject_semantic_region"
    SELECT_SEMANTIC_REGION = "select_semantic_region"


_ACTIONS_BY_REASON: Mapping[InteractionReason, frozenset[InteractionAction]] = {
    InteractionReason.MEASUREMENT_SETUP_REQUIRED: frozenset(
        {InteractionAction.CONFIGURE_MEASUREMENTS}
    ),
    InteractionReason.SEGMENTATION_SETUP_REQUIRED: frozenset(
        {InteractionAction.CONFIGURE_SEGMENTATION}
    ),
    InteractionReason.TISSUE_DETECTOR_SETUP_REQUIRED: frozenset(
        {
            InteractionAction.SELECT_TISSUE_DETECTOR,
            InteractionAction.IMPORT_TISSUE_DETECTOR,
            InteractionAction.REFRESH_TISSUE_DETECTORS,
        }
    ),
    InteractionReason.TISSUE_NATIVE_LEVEL_REQUIRED: frozenset(
        {InteractionAction.SELECT_TISSUE_NATIVE_LEVEL}
    ),
    InteractionReason.TISSUE_DETECTION_CHANNEL_REQUIRED: frozenset(
        {InteractionAction.SELECT_CHANNEL}
    ),
    InteractionReason.TISSUE_SECTION_REVIEW_REQUIRED: frozenset(
        {
            InteractionAction.ACCEPT,
            InteractionAction.EDIT,
            InteractionAction.REPEAT,
            InteractionAction.REJECT,
        }
    ),
    InteractionReason.REGISTRATION_REVIEW_REQUIRED: frozenset(
        {
            InteractionAction.ACCEPT_REGISTRATION,
            InteractionAction.ACCEPT_REGISTRATION_AND_TRANSFER,
            InteractionAction.REJECT,
            InteractionAction.SIDE_BY_SIDE,
            InteractionAction.SKIP_MOVING_REFERENCE,
        }
    ),
    InteractionReason.TRANSFER_REVIEW_REQUIRED: frozenset(
        {
            InteractionAction.ACCEPT_TRANSFERS,
            InteractionAction.REJECT_TRANSFERS,
        }
    ),
    InteractionReason.WARPY_REGISTRATION_REQUIRED: frozenset(
        {
            InteractionAction.IMPORT_WARPY_TRANSFORM,
            InteractionAction.SIDE_BY_SIDE,
            InteractionAction.SKIP_MOVING_REFERENCE,
        }
    ),
    InteractionReason.TRANSFER_COMMIT_RECOVERY_REQUIRED: frozenset(
        {
            InteractionAction.RETRY_TRANSFER_COMMIT,
            InteractionAction.QUARANTINE_TRANSFER_ATTEMPT,
            InteractionAction.ABORT_TRANSFER_ATTEMPT,
        }
    ),
    InteractionReason.TRAINING_PLAN_SELECTION_REQUIRED: frozenset(
        {InteractionAction.SELECT_TRAINING_PLAN}
    ),
    InteractionReason.PRIMARY_TRAINING_ANNOTATIONS_REQUIRED: frozenset(
        {
            InteractionAction.ANNOTATE_PRIMARY,
            InteractionAction.MANUAL_ANNOTATION,
            InteractionAction.TRAIN_CLASSIFIER,
        }
    ),
    InteractionReason.PRIMARY_CLASSIFIER_REVIEW_REQUIRED: frozenset(
        {
            InteractionAction.ACCEPT,
            InteractionAction.REVIEW_CLASSIFIER,
            InteractionAction.RETRAIN_CLASSIFIER,
            InteractionAction.REJECT,
        }
    ),
    InteractionReason.SECONDARY_TRAINING_ANNOTATIONS_REQUIRED: frozenset(
        {
            InteractionAction.ANNOTATE_SECONDARY,
            InteractionAction.MANUAL_ANNOTATION,
            InteractionAction.TRAIN_CLASSIFIER,
        }
    ),
    InteractionReason.ROI_REVIEW_REQUIRED: frozenset(
        {
            InteractionAction.ACCEPT,
            InteractionAction.EDIT,
            InteractionAction.REJECT,
            InteractionAction.REVIEW_ROIS,
            InteractionAction.MANUAL_ANNOTATION,
        }
    ),
    InteractionReason.BIOMARKER_THRESHOLD_REQUIRED: frozenset(
        {InteractionAction.SET_BIOMARKER_THRESHOLD}
    ),
    InteractionReason.PHENOTYPING_MODE_REQUIRED: frozenset(
        {InteractionAction.SELECT_PHENOTYPING_MODE, InteractionAction.ABORT}
    ),
    InteractionReason.PHENOTYPE_DEFINITION_COUNT_REQUIRED: frozenset(
        {InteractionAction.DECLARE_PHENOTYPE_COUNT, InteractionAction.ABORT}
    ),
    InteractionReason.PHENOTYPE_CLASSES_REQUIRED: frozenset(
        {InteractionAction.DECLARE_PHENOTYPE_CLASSES, InteractionAction.ABORT}
    ),
    InteractionReason.PHENOTYPING_MEASUREMENTS_REQUIRED: frozenset(
        {InteractionAction.SELECT_PHENOTYPING_MEASUREMENTS, InteractionAction.ABORT}
    ),
    InteractionReason.MARKER_THRESHOLD_REVIEW_REQUIRED: frozenset(
        {InteractionAction.REVIEW_MARKER_THRESHOLDS, InteractionAction.ABORT}
    ),
    InteractionReason.MARKER_COMPOSITE_RULES_REQUIRED: frozenset(
        {InteractionAction.DEFINE_MARKER_COMPOSITE_RULES, InteractionAction.ABORT}
    ),
    InteractionReason.MARKER_RULES_REVIEW_REQUIRED: frozenset(
        {InteractionAction.REVIEW_MARKER_RULES, InteractionAction.ABORT}
    ),
    InteractionReason.SUPERVISED_PHENOTYPE_CLASSES_REQUIRED: frozenset(
        {InteractionAction.DECLARE_PHENOTYPE_CLASSES, InteractionAction.ABORT}
    ),
    InteractionReason.SUPERVISED_TRAINING_ANNOTATIONS_REQUIRED: frozenset(
        {InteractionAction.ANNOTATE_SUPERVISED_TRAINING, InteractionAction.ABORT}
    ),
    InteractionReason.SUPERVISED_CLASSIFIER_REVIEW_REQUIRED: frozenset(
        {InteractionAction.REVIEW_SUPERVISED_CLASSIFIER, InteractionAction.ABORT}
    ),
    InteractionReason.CLUSTERING_CONFIGURATION_REQUIRED: frozenset(
        {InteractionAction.CONFIGURE_CLUSTERING, InteractionAction.ABORT}
    ),
    InteractionReason.CLUSTER_LABEL_REVIEW_REQUIRED: frozenset(
        {
            InteractionAction.ASSIGN_CLUSTER_LABELS,
            InteractionAction.ACCEPT_UNLABELED_CLUSTERS,
            InteractionAction.REVISE_CLUSTERING,
            InteractionAction.ABORT,
        }
    ),
    InteractionReason.SECONDARY_ROI_OVERLAP_POLICY_REQUIRED: frozenset(
        {InteractionAction.SELECT_SECONDARY_ROI_OVERLAP_POLICY}
    ),
    InteractionReason.ANALYSIS_DOMAIN_CHOICE_REQUIRED: frozenset(
        {
            InteractionAction.USE_WHOLE_TISSUE,
            InteractionAction.SELECT_ANALYSIS_ROIS,
        }
    ),
    InteractionReason.ANALYSIS_DOMAIN_ROIS_REQUIRED: frozenset(
        {
            InteractionAction.OPEN_QUPATH,
            InteractionAction.REVIEW_ANALYSIS_ROIS,
            InteractionAction.USE_WHOLE_TISSUE,
        }
    ),
    InteractionReason.ANALYSIS_DOMAIN_REVIEW_REQUIRED: frozenset(
        {
            InteractionAction.ACCEPT_ANALYSIS_ROIS,
            InteractionAction.EDIT_ANALYSIS_ROIS,
            InteractionAction.USE_WHOLE_TISSUE,
        }
    ),
    InteractionReason.STRUCTURE_COUNT_POLICY_ACKNOWLEDGEMENT_REQUIRED: frozenset(
        {
            InteractionAction.ACKNOWLEDGE_STRUCTURE_COUNT_POLICY,
            InteractionAction.REVISE_STRUCTURE_COUNT_POLICY,
        }
    ),
    InteractionReason.PRIMARY_STRUCTURE_COUNT_REVIEW_REQUIRED: frozenset(
        {
            InteractionAction.ACCEPT_ALL_PRIMARY_STRUCTURES,
            InteractionAction.EXCLUDE_ANALYSIS_DOMAIN,
            InteractionAction.REVIEW_CLASSIFIER,
            InteractionAction.RETRAIN_CLASSIFIER,
        }
    ),
    InteractionReason.SECONDARY_STRUCTURE_SELECTION_REQUIRED: frozenset(
        {
            InteractionAction.ACCEPT_SECONDARY_STRUCTURE_SELECTION,
            InteractionAction.REVIEW_CLASSIFIER,
            InteractionAction.RETRAIN_CLASSIFIER,
        }
    ),
    InteractionReason.SEMANTIC_REGION_DECISION_REQUIRED: frozenset(
        {
            InteractionAction.ACCEPT_SEMANTIC_REGION,
            InteractionAction.REJECT_SEMANTIC_REGION,
            InteractionAction.SELECT_SEMANTIC_REGION,
        }
    ),
    InteractionReason.OTHER_EXPLICIT_REASON: frozenset(InteractionAction),
}


_ACTIONS_BY_STAGE: Mapping[str, frozenset[InteractionAction]] = {
    "tissue_resolution": _ACTIONS_BY_REASON[InteractionReason.TISSUE_NATIVE_LEVEL_REQUIRED],
    "tissue_detection": (
        _ACTIONS_BY_REASON[InteractionReason.TISSUE_DETECTION_CHANNEL_REQUIRED]
        | _ACTIONS_BY_REASON[InteractionReason.TISSUE_SECTION_REVIEW_REQUIRED]
        | frozenset({InteractionAction.CREATE_THRESHOLDER})
    ),
    "registration": (
        _ACTIONS_BY_REASON[InteractionReason.REGISTRATION_REVIEW_REQUIRED]
        | _ACTIONS_BY_REASON[InteractionReason.TRANSFER_REVIEW_REQUIRED]
        | _ACTIONS_BY_REASON[InteractionReason.WARPY_REGISTRATION_REQUIRED]
        | _ACTIONS_BY_REASON[InteractionReason.TRANSFER_COMMIT_RECOVERY_REQUIRED]
        | frozenset(
            {
                InteractionAction.NO_MOVING_REFERENCE,
                InteractionAction.MOVING_REFERENCE_AVAILABLE,
                InteractionAction.REGISTER_WITH_WARPY,
                InteractionAction.SKIP_MOVING_REFERENCE,
                InteractionAction.SET_MOVING_ASSISTANCE_SCOPES,
                InteractionAction.SKIP_MOVING_ASSISTANCE,
            }
        )
    ),
    "primary_classifier": (
        _ACTIONS_BY_REASON[InteractionReason.PRIMARY_TRAINING_ANNOTATIONS_REQUIRED]
        | _ACTIONS_BY_REASON[InteractionReason.PRIMARY_CLASSIFIER_REVIEW_REQUIRED]
        | _ACTIONS_BY_REASON[InteractionReason.STRUCTURE_COUNT_POLICY_ACKNOWLEDGEMENT_REQUIRED]
        | _ACTIONS_BY_REASON[InteractionReason.PRIMARY_STRUCTURE_COUNT_REVIEW_REQUIRED]
        | frozenset(
            {
                InteractionAction.RESTRICT_CLASSIFIER_TO_SLIDE,
                InteractionAction.REUSE_CLASSIFIER_FOR_VALIDATION_DOMAIN,
                InteractionAction.SET_MOVING_ASSISTANCE_SCOPES,
                InteractionAction.SKIP_MOVING_ASSISTANCE,
                InteractionAction.ACCEPT_REGISTRATION,
            }
        )
    ),
    "secondary_classifier": (
        _ACTIONS_BY_REASON[InteractionReason.SECONDARY_TRAINING_ANNOTATIONS_REQUIRED]
        | _ACTIONS_BY_REASON[InteractionReason.SECONDARY_STRUCTURE_SELECTION_REQUIRED]
        | frozenset(
            {
                InteractionAction.ACCEPT,
                InteractionAction.RESTRICT_CLASSIFIER_TO_SLIDE,
                InteractionAction.RETRAIN_CLASSIFIER,
                InteractionAction.REVIEW_CLASSIFIER,
                InteractionAction.REUSE_CLASSIFIER_FOR_VALIDATION_DOMAIN,
            }
        )
    ),
    "roi_review": _ACTIONS_BY_REASON[InteractionReason.ROI_REVIEW_REQUIRED],
    "semantic_validation": (
        _ACTIONS_BY_REASON[InteractionReason.ROI_REVIEW_REQUIRED]
        | _ACTIONS_BY_REASON[InteractionReason.SECONDARY_STRUCTURE_SELECTION_REQUIRED]
        | _ACTIONS_BY_REASON[InteractionReason.SEMANTIC_REGION_DECISION_REQUIRED]
    ),
    "phenotyping": frozenset().union(
        _ACTIONS_BY_REASON[InteractionReason.BIOMARKER_THRESHOLD_REQUIRED],
        *(
            _ACTIONS_BY_REASON[reason]
            for reason in InteractionReason
            if reason.value.startswith(
                ("phenotyping_", "phenotype_", "marker_", "supervised_", "clustering_", "cluster_")
            )
        ),
    ),
    "spatial_analysis": _ACTIONS_BY_REASON[InteractionReason.SECONDARY_ROI_OVERLAP_POLICY_REQUIRED],
    "analysis_domain": frozenset().union(
        _ACTIONS_BY_REASON[InteractionReason.ANALYSIS_DOMAIN_CHOICE_REQUIRED],
        _ACTIONS_BY_REASON[InteractionReason.ANALYSIS_DOMAIN_ROIS_REQUIRED],
        _ACTIONS_BY_REASON[InteractionReason.ANALYSIS_DOMAIN_REVIEW_REQUIRED],
    ),
    "structure_count_policy": _ACTIONS_BY_REASON[
        InteractionReason.STRUCTURE_COUNT_POLICY_ACKNOWLEDGEMENT_REQUIRED
    ],
    "primary_classifier_training_plan_review": _ACTIONS_BY_REASON[
        InteractionReason.TRAINING_PLAN_SELECTION_REQUIRED
    ],
    "secondary_classifier_training_plan_review": _ACTIONS_BY_REASON[
        InteractionReason.TRAINING_PLAN_SELECTION_REQUIRED
    ],
    "supervised_phenotype_training_plan_review": _ACTIONS_BY_REASON[
        InteractionReason.TRAINING_PLAN_SELECTION_REQUIRED
    ],
}


_STAGE_SCOPED_ACTIONS = frozenset(
    {
        InteractionAction.SELECT_TRAINING_PLAN,
        InteractionAction.USE_WHOLE_TISSUE,
        InteractionAction.SELECT_ANALYSIS_ROIS,
        InteractionAction.OPEN_QUPATH,
        InteractionAction.REVIEW_ANALYSIS_ROIS,
        InteractionAction.ACCEPT_ANALYSIS_ROIS,
        InteractionAction.EDIT_ANALYSIS_ROIS,
        InteractionAction.ACKNOWLEDGE_STRUCTURE_COUNT_POLICY,
        InteractionAction.REVISE_STRUCTURE_COUNT_POLICY,
        InteractionAction.ACCEPT_ALL_PRIMARY_STRUCTURES,
        InteractionAction.EXCLUDE_ANALYSIS_DOMAIN,
        InteractionAction.ACCEPT_SECONDARY_STRUCTURE_SELECTION,
        InteractionAction.ACCEPT_SEMANTIC_REGION,
        InteractionAction.REJECT_SEMANTIC_REGION,
    }
)


def validate_interaction_actions(
    reason: InteractionReason,
    actions: Sequence[str],
) -> tuple[str, ...]:
    """Return unique actions after vocabulary and reason compatibility checks."""
    if not isinstance(reason, InteractionReason):
        raise ROIAnalysisError("interaction reason is invalid")
    if isinstance(actions, (str, bytes)) or not isinstance(actions, Sequence):
        raise ROIAnalysisError("allowed actions must be a sequence")
    compatible = _ACTIONS_BY_REASON[reason]
    parsed: list[InteractionAction] = []
    for value in actions:
        try:
            action = InteractionAction(_nonblank(value, "allowed action"))
        except (TypeError, ValueError) as exc:
            raise ROIAnalysisError("allowed action is unknown") from exc
        if action not in compatible:
            raise ROIAnalysisError(
                f"allowed action {action.value!r} is incompatible with {reason.value}"
            )
        parsed.append(action)
    if not parsed:
        raise ROIAnalysisError("needs_interaction requires allowed actions")
    if len(set(parsed)) != len(parsed):
        raise ROIAnalysisError("allowed actions must be unique")
    return tuple(action.value for action in parsed)


def validate_review_action(stage: str, action: str) -> str:
    """Validate one persisted decision against the shared action vocabulary."""
    stage_name = _nonblank(stage, "stage")
    try:
        parsed = InteractionAction(_nonblank(action, "review action"))
    except (TypeError, ValueError) as exc:
        raise ROIAnalysisError("review action is unknown") from exc
    stage_actions = _ACTIONS_BY_STAGE.get(stage_name)
    if (stage_actions is None and parsed in _STAGE_SCOPED_ACTIONS) or (
        stage_actions is not None and parsed not in stage_actions
    ):
        raise ROIAnalysisError(
            f"review action {parsed.value!r} is incompatible with stage {stage_name!r}"
        )
    return parsed.value


def _nonblank(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ROIAnalysisError(f"{field} must be a non-empty string")
    return value


def _freeze_json(value: object, field: str) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ROIAnalysisError(f"{field} keys must be strings")
        return MappingProxyType(
            {key: _freeze_json(item, f"{field}.{key}") for key, item in value.items()}
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(_freeze_json(item, field) for item in value)
    raise ROIAnalysisError(f"{field} must contain only JSON-like values")


def _export_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _export_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_export_json(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    return value


@dataclass(frozen=True, slots=True)
class StageResult:
    """Immutable stage outcome with explicit interaction and failure branches."""

    stage: str
    slide_id: str
    status: StageStatus
    generation_id: str | None = None
    interaction_reason: InteractionReason | None = None
    affected_object_ids: tuple[str, ...] = ()
    message: str | None = None
    allowed_actions: tuple[str, ...] = ()
    resume_context: Mapping[str, Any] | None = None
    checkpoint_id: str | None = None
    outputs: Mapping[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    error_context: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        """Validate the StageResult fields when the record is constructed.

        The checks cover ``stage``, ``slide_id``, ``generation_id``, ``status``,
        ``allowed_actions``, ``interaction_reason``. Invalid field types, values or
        combinations raise the declared validation error before this instance can be used by
        its caller."""

        _nonblank(self.stage, "stage")
        _nonblank(self.slide_id, "slide_id")
        if not isinstance(self.status, StageStatus):
            raise ROIAnalysisError("status must be a StageStatus")
        if self.generation_id is not None:
            _nonblank(self.generation_id, "generation_id")
        object.__setattr__(
            self,
            "affected_object_ids",
            tuple(_nonblank(item, "affected_object_ids") for item in self.affected_object_ids),
        )
        object.__setattr__(
            self,
            "allowed_actions",
            (
                validate_interaction_actions(self.interaction_reason, self.allowed_actions)
                if self.status is StageStatus.NEEDS_INTERACTION
                and isinstance(self.interaction_reason, InteractionReason)
                else tuple(_nonblank(item, "allowed_actions") for item in self.allowed_actions)
            ),
        )
        for name in ("resume_context", "outputs", "error_context"):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, Mapping):
                    raise ROIAnalysisError(f"{name} must be a mapping")
                object.__setattr__(self, name, _freeze_json(value, name))
        if self.status is StageStatus.COMPLETED:
            if (
                self.interaction_reason is not None
                or self.message is not None
                or self.allowed_actions
                or self.resume_context is not None
                or self.checkpoint_id is not None
            ):
                raise ROIAnalysisError("completed results cannot carry interaction payloads")
            if (
                self.error_code is not None
                or self.error_message is not None
                or self.error_context is not None
            ):
                raise ROIAnalysisError("completed results cannot carry failed payloads")
        elif self.status is StageStatus.NEEDS_INTERACTION:
            if not isinstance(self.interaction_reason, InteractionReason):
                raise ROIAnalysisError("needs_interaction requires an interaction reason")
            _nonblank(self.message, "message")
            if not self.allowed_actions:
                raise ROIAnalysisError("needs_interaction requires allowed actions")
            _nonblank(self.checkpoint_id, "checkpoint_id")
            if self.resume_context is None or not self.resume_context:
                raise ROIAnalysisError("needs_interaction requires resume context")
            owner_version = self.resume_context.get("schema_version")
            if (
                type(owner_version) is not str
                or not owner_version
                or owner_version != owner_version.strip()
            ):
                raise ROIAnalysisError(
                    "needs_interaction resume_context.schema_version must be "
                    "non-empty canonical text supplied by its owning producer"
                )
            if self.outputs is not None:
                raise ROIAnalysisError("needs_interaction cannot carry completed outputs")
            if (
                self.error_code is not None
                or self.error_message is not None
                or self.error_context is not None
            ):
                raise ROIAnalysisError("needs_interaction cannot carry a failed payload")
        else:
            _nonblank(self.error_code, "error_code")
            _nonblank(self.error_message, "error_message")
            if (
                self.interaction_reason is not None
                or self.message is not None
                or self.allowed_actions
                or self.resume_context is not None
                or self.checkpoint_id is not None
            ):
                raise ROIAnalysisError("failed results cannot carry interaction payloads")
            if self.outputs is not None:
                raise ROIAnalysisError("failed results cannot carry completed outputs")

    @classmethod
    def completed(
        cls,
        *,
        stage: str,
        slide_id: str,
        generation_id: str | None = None,
        affected_object_ids: Sequence[str] = (),
        outputs: Mapping[str, Any] | None = None,
        interaction_reason: InteractionReason | None = None,
        allowed_actions: Sequence[str] = (),
    ) -> "StageResult":
        if interaction_reason is not None or allowed_actions:
            raise ROIAnalysisError("completed results cannot carry interaction reason or actions")
        return cls(
            stage=stage,
            slide_id=slide_id,
            status=StageStatus.COMPLETED,
            generation_id=generation_id,
            affected_object_ids=tuple(affected_object_ids),
            outputs=outputs,
        )

    @classmethod
    def needs_interaction(
        cls,
        *,
        stage: str,
        slide_id: str,
        reason: InteractionReason | None = None,
        message: str | None = None,
        allowed_actions: Sequence[str] | None = None,
        resume_context: Mapping[str, Any] | None = None,
        checkpoint_id: str | None = None,
        generation_id: str | None = None,
        affected_object_ids: Sequence[str] = (),
    ) -> "StageResult":
        if not isinstance(reason, InteractionReason):
            raise ROIAnalysisError("needs_interaction requires an interaction reason")
        _nonblank(message, "message")
        if not allowed_actions:
            raise ROIAnalysisError("needs_interaction requires allowed actions")
        _nonblank(checkpoint_id, "checkpoint_id")
        if not isinstance(resume_context, Mapping) or not resume_context:
            raise ROIAnalysisError("needs_interaction requires resume context")
        return cls(
            stage=stage,
            slide_id=slide_id,
            status=StageStatus.NEEDS_INTERACTION,
            generation_id=generation_id,
            interaction_reason=reason,
            affected_object_ids=tuple(affected_object_ids),
            message=message,
            allowed_actions=tuple(allowed_actions),
            resume_context=resume_context,
            checkpoint_id=checkpoint_id,
        )

    @classmethod
    def failed(
        cls,
        *,
        stage: str,
        slide_id: str,
        error_code: str | None,
        error_message: str | None,
        generation_id: str | None = None,
        affected_object_ids: Sequence[str] = (),
        error_context: Mapping[str, Any] | None = None,
    ) -> "StageResult":
        _nonblank(error_code, "error_code")
        _nonblank(error_message, "error_message")
        return cls(
            stage=stage,
            slide_id=slide_id,
            status=StageStatus.FAILED,
            generation_id=generation_id,
            affected_object_ids=tuple(affected_object_ids),
            error_code=error_code,
            error_message=error_message,
            error_context=error_context,
        )

    @classmethod
    def from_mapping(cls, value: object) -> "StageResult":
        """Decode the serialized fields into a validated StageResult record.

        The mapping is checked against the declared field inventory, including ``stage``,
        ``slide_id``, ``generation_id``, ``status``, ``interaction_reason``,
        ``affected_object_ids``. Nested values are decoded through the corresponding field
        validators before the record is constructed."""
        if not isinstance(value, Mapping):
            raise ROIAnalysisError("stage result must be a mapping")
        required = {
            "stage",
            "slide_id",
            "generation_id",
            "status",
            "interaction_reason",
            "affected_object_ids",
            "message",
            "allowed_actions",
            "resume_context",
            "checkpoint_id",
            "outputs",
            "error",
        }
        if set(value) != required:
            raise ROIAnalysisError("stage result fields do not match the schema")
        for field in ("affected_object_ids", "allowed_actions"):
            sequence = value[field]
            if isinstance(sequence, (str, bytes)) or not isinstance(sequence, Sequence):
                raise ROIAnalysisError(f"stage result {field} must be a sequence")
        try:
            status = StageStatus(value["status"])
            reason = _parse_interaction_reason(value["interaction_reason"])
        except (TypeError, ValueError) as exc:
            raise ROIAnalysisError("stage result status or interaction reason is invalid") from exc
        error = value["error"]
        if error is None:
            error_code = error_message = error_context = None
        elif isinstance(error, Mapping) and set(error) == {"code", "message", "context"}:
            error_code = error["code"]
            error_message = error["message"]
            error_context = error["context"]
        else:
            raise ROIAnalysisError("stage result error is invalid")
        return cls(
            stage=value["stage"],
            slide_id=value["slide_id"],
            generation_id=value["generation_id"],
            status=status,
            interaction_reason=reason,
            affected_object_ids=tuple(value["affected_object_ids"]),
            message=value["message"],
            allowed_actions=tuple(value["allowed_actions"]),
            resume_context=value["resume_context"],
            checkpoint_id=value["checkpoint_id"],
            outputs=value["outputs"],
            error_code=error_code,
            error_message=error_message,
            error_context=error_context,
        )

    def to_mapping(self) -> dict[str, Any]:
        """Return an independent JSON-safe wire representation."""
        return {
            "stage": self.stage,
            "slide_id": self.slide_id,
            "generation_id": self.generation_id,
            "status": self.status.value,
            "interaction_reason": (
                None if self.interaction_reason is None else self.interaction_reason.value
            ),
            "affected_object_ids": list(self.affected_object_ids),
            "message": self.message,
            "allowed_actions": list(self.allowed_actions),
            "resume_context": _export_json(self.resume_context),
            "checkpoint_id": self.checkpoint_id,
            "outputs": _export_json(self.outputs),
            "error": (
                None
                if self.error_code is None
                else {
                    "code": self.error_code,
                    "message": self.error_message,
                    "context": _export_json(self.error_context),
                }
            ),
        }

    @property
    def interaction_action_schema_version(self) -> str | None:
        """Return the closed action vocabulary governing this interaction."""
        if self.interaction_reason is None:
            return None
        return INTERACTION_ACTION_SCHEMA_VERSION


def _parse_interaction_reason(
    value: object,
) -> InteractionReason | None:
    if value is None:
        return None
    return InteractionReason(value)


@dataclass(frozen=True, slots=True)
class WorkflowCompletionOutcome:
    """Typed successful outcome for one sample workflow."""

    run_id: str
    stage_status: StageStatus
    reason: str
    eligible_domain_ids: tuple[str, ...] = ()
    excluded_domain_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _nonblank(self.run_id, "run_id")
        if self.stage_status is not StageStatus.COMPLETED:
            raise ROIAnalysisError("workflow completion outcome must be completed")
        if self.reason not in {"completed", "completed_no_eligible_domains"}:
            raise ROIAnalysisError("workflow completion reason is invalid")
        eligible = tuple(
            _nonblank(value, "eligible_domain_ids") for value in self.eligible_domain_ids
        )
        excluded = tuple(
            _nonblank(value, "excluded_domain_ids") for value in self.excluded_domain_ids
        )
        if len(set(eligible)) != len(eligible) or len(set(excluded)) != len(excluded):
            raise ROIAnalysisError("workflow domain IDs must be unique")
        if set(eligible) & set(excluded):
            raise ROIAnalysisError("eligible and excluded domain IDs must be disjoint")
        if self.reason == "completed_no_eligible_domains" and (eligible or not excluded):
            raise ROIAnalysisError(
                "completed_no_eligible_domains requires excluded and no eligible domains"
            )
        object.__setattr__(self, "eligible_domain_ids", eligible)
        object.__setattr__(self, "excluded_domain_ids", excluded)

    @classmethod
    def completed(
        cls,
        *,
        run_id: str,
        eligible_domain_ids: Sequence[str],
        excluded_domain_ids: Sequence[str] = (),
    ) -> "WorkflowCompletionOutcome":
        return cls(
            run_id=run_id,
            stage_status=StageStatus.COMPLETED,
            reason="completed",
            eligible_domain_ids=tuple(eligible_domain_ids),
            excluded_domain_ids=tuple(excluded_domain_ids),
        )

    @classmethod
    def completed_no_eligible_domains(
        cls,
        *,
        run_id: str,
        excluded_domain_ids: Sequence[str],
    ) -> "WorkflowCompletionOutcome":
        return cls(
            run_id=run_id,
            stage_status=StageStatus.COMPLETED,
            reason="completed_no_eligible_domains",
            excluded_domain_ids=tuple(excluded_domain_ids),
        )


def _requires_generation_manifest(result: StageResult) -> bool:
    """Return whether the exchange contract requires a generation manifest.

    This preserves the existing pre-generation interaction exceptions while
    keeping stage-policy decisions out of the generic QuPath transport layer.
    """
    if result.status is StageStatus.FAILED:
        return result.generation_id is not None
    if result.status is not StageStatus.NEEDS_INTERACTION or result.generation_id is not None:
        return True
    if result.interaction_reason in {
        InteractionReason.TISSUE_DETECTION_CHANNEL_REQUIRED,
        InteractionReason.SUPERVISED_TRAINING_ANNOTATIONS_REQUIRED,
    }:
        return False
    return not (
        result.interaction_reason is InteractionReason.OTHER_EXPLICIT_REASON
        and InteractionAction.CREATE_THRESHOLDER.value in result.allowed_actions
    )
