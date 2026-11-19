"""Authoritative response schemas for ROI interaction checkpoints.

These records and helpers define shared values used by several stages. Construction and
parsing validate the declared fields before the values are used to coordinate scientific
inputs, results or runtime resources."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from math import isfinite
import os
from pathlib import Path
from typing import Any

from .config import (
    ChannelSpec,
    ClusteringConfig,
    ClusteringMode,
    PhenotypeClassSpec,
    PhenotypingConfig,
    PhenotypingMode,
    TissueDetectionParameters,
    validate_tissue_classifier_name,
)
from .errors import ROIAnalysisError
from .stage_result import (
    InteractionAction,
    InteractionReason,
    StageResult,
    StageStatus,
)

MAX_PHENOTYPE_DEFINITION_COUNT = 64
MAX_PHENOTYPE_CLASS_ID_LENGTH = 64
MAX_PHENOTYPE_DISPLAY_NAME_LENGTH = 128


# ==============================================================================
#                                  Interactions
# ==============================================================================


def validate_measurement_setup_context(result: StageResult) -> tuple[ChannelSpec, ...]:
    """Validate the closed control inventory, without granting image authority."""
    context = result.resume_context
    _require_fields(context, {"schema_version", "checkpoint_id", "section_id", "channels"}
        | ({"nuclear_channel_id"} if "nuclear_channel_id" in context else set())
        | ({"cell_boundary_source"} if "cell_boundary_source" in context else set()))
    from mif_pipeline.cell_analysis.measurement_setup import segmentation_compartments
    segmentation_compartments(context.get("cell_boundary_source"))
    if (
        context["schema_version"] != "measurement-setup-interaction/v1"
        or context["checkpoint_id"] != result.checkpoint_id
    ):
        raise ROIAnalysisError("measurement setup context identity is invalid")
    section_id = context["section_id"]
    if type(section_id) is not str or not section_id or section_id != section_id.strip():
        raise ROIAnalysisError("measurement setup requires an exact section identity")
    raw_channels = context["channels"]
    if not isinstance(raw_channels, (list, tuple)) or not 1 <= len(raw_channels) <= 1024:
        raise ROIAnalysisError("measurement setup requires processed channels")
    channels = tuple(
        ChannelSpec.from_mapping(item, field="measurement setup channel") for item in raw_channels
    )
    if len({item.channel_id for item in channels}) != len(channels):
        raise ROIAnalysisError("measurement setup channel IDs are duplicated")
    nuclear = context.get("nuclear_channel_id")
    if nuclear is not None and not any(channel.channel_id == nuclear and channel.biomarker is None for channel in channels):
        raise ROIAnalysisError("Review the nuclear stain in Channels and markers")
    return channels


@dataclass(frozen=True, slots=True)
class ResumeEnvelope:
    """Canonical service context and validated response from any resume layer."""

    service_context: Mapping[str, Any]
    response: Mapping[str, Any]


def resolve_resume_envelope(value: object) -> ResumeEnvelope:
    """Unwrap workflow persistence layers without flattening service evidence."""

    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ROIAnalysisError("resume envelope must be a string-keyed mapping")
    response = value.get("response", {})
    if not isinstance(response, Mapping) or not all(isinstance(key, str) for key in response):
        raise ROIAnalysisError("resume envelope response must be a string-keyed mapping")
    current: Mapping[str, Any] = value
    merged: dict[str, Any] = {}
    seen: set[int] = set()
    while isinstance(current.get("service_context"), Mapping):
        if id(current) in seen:
            raise ROIAnalysisError("resume envelope service_context is cyclic")
        seen.add(id(current))
        merged.update(
            {
                key: item
                for key, item in current.items()
                if key not in {"service_context", "response"}
            }
        )
        nested = current["service_context"]
        assert isinstance(nested, Mapping)
        if not all(isinstance(key, str) for key in nested):
            raise ROIAnalysisError("resume service context must be string-keyed")
        current = nested
    merged.update(
        {key: item for key, item in current.items() if key not in {"service_context", "response"}}
    )
    return ResumeEnvelope(service_context=merged, response=response)


def validate_interaction_response(
    result: StageResult,
    action: str,
    response: Mapping[str, object] = None,
) -> dict[str, object]:
    """Validate one adapter response and return a resumable context.

    The required mapping shape includes ``feature_id``, ``measurement_name``, ``kind``,
    ``channel_id``, ``biomarker``, ``compartment``. Field checks are applied before the
    validated value is returned or passed to its next constructor.

    CLI and desktop presentation adapters must call this function so response meaning cannot
    drift between frontends."""
    if not isinstance(result, StageResult) or result.status is not StageStatus.NEEDS_INTERACTION:
        raise ValueError("interaction response requires a needs_interaction StageResult")
    reason = result.interaction_reason
    assert reason is not None
    if response is None:
        if reason not in _PHENOTYPING_REASONS or not isinstance(action, Mapping):
            raise ValueError("interaction response mapping is required")
        response = action
        action = _inferred_phenotyping_action(reason)
    if not isinstance(action, str) or action not in result.allowed_actions:
        raise ValueError("selected action was not offered")
    if not isinstance(response, Mapping) or not all(isinstance(key, str) for key in response):
        raise ValueError("interaction response must be a string-keyed mapping")

    context = result.resume_context or {}
    if action == InteractionAction.ABORT.value:
        _require_fields(response, set())
        if reason not in _PHENOTYPING_REASONS:
            raise ValueError("abort is not available for this interaction")
        validated = {"phenotyping_aborted": True}
    elif reason is InteractionReason.SEMANTIC_REGION_DECISION_REQUIRED:
        service = resolve_resume_envelope(context).service_context
        if action == InteractionAction.SELECT_SEMANTIC_REGION.value:
            _require_fields(response, {"candidate_roi_id"})
            candidate = response["candidate_roi_id"]
            if (service.get("schema_version") != "semantic-region-interaction/v2"
                    or not isinstance(candidate, str)
                    or candidate not in {item["roi_id"] for item in service.get("review_candidates", ())}):
                raise ValueError("Choose an available region to review.")
            validated = {"candidate_roi_id": candidate}
        elif service.get("schema_version") == "semantic-region-interaction/v2":
            if "reviewer_id" not in response or set(response) - {"reviewer_id", "rationale", "subtype_status", "semantic_class_id"}:
                raise ValueError("region review response fields are invalid")
            reviewer = response["reviewer_id"]
            rationale = response.get("rationale")
            if not isinstance(reviewer, str) or not reviewer.strip():
                raise ValueError("region review requires a reviewer identifier")
            if rationale is not None and not isinstance(rationale, str):
                raise ValueError("region observation must be text")
            classes = service.get("declared_subtypes", ())
            status, class_id = response.get("subtype_status"), response.get("semantic_class_id")
            if action == "reject_semantic_region":
                status, class_id = "not_evaluated", None
            elif not classes:
                if class_id is not None or status not in {None, "not_configured"}:
                    raise ValueError("this structure has no subtypes")
                status = "not_configured"
            elif status == "assigned":
                if class_id not in classes:
                    raise ValueError("choose one declared subtype")
            elif status != "cannot_assess" or class_id is not None:
                raise ValueError("choose a subtype or Cannot assess")
            validated = {"reviewer_id": reviewer.strip(), "rationale": rationale.strip() or None if rationale is not None else None,
                         "subtype_status": status, "semantic_class_id": class_id}
        else:
            _require_fields(response, {"reviewer_id", "rationale"})
            reviewer_id = response["reviewer_id"]
            rationale = response["rationale"]
            if not isinstance(reviewer_id, str) or not reviewer_id.strip():
                raise ValueError("semantic region decision requires a reviewer identifier")
            if not isinstance(rationale, str) or not rationale.strip():
                raise ValueError("semantic region decision requires a rationale")
            validated = {
                "reviewer_id": reviewer_id.strip(),
                "rationale": rationale.strip(),
            }
    elif reason is InteractionReason.SECONDARY_STRUCTURE_SELECTION_REQUIRED:
        validated = _manual_secondary_selection_response(context, response)
    elif reason in _ACTION_ONLY_REASONS:
        # These decisions are encoded by their closed offered action. Material
        # scientific identity remains in the immutable resume context.
        _require_fields(response, set())
        validated = {}
    elif reason is InteractionReason.PHENOTYPING_MODE_REQUIRED:
        _require_fields(response, {"mode"} | ({"phenotyping_population"} if "population_options" in context else set()))
        if "population_options" in context:
            from mif_pipeline.cell_analysis.phenotyping_population import validate_offered_population
            population = validate_offered_population(response["phenotyping_population"], context["population_options"])
        available = _string_sequence(context.get("available_modes"), "available phenotyping modes")
        try:
            mode = PhenotypingMode(response["mode"])
        except (TypeError, ValueError) as exc:
            raise ROIAnalysisError("phenotyping mode is invalid") from exc
        if mode.value not in available:
            raise ROIAnalysisError("phenotyping mode was not offered")
        validated = {
            "phenotyping_mode": mode.value,
            **({"phenotyping_population": population} if "population_options" in context else {}),
            "next_reason": (
                InteractionReason.PHENOTYPING_MEASUREMENTS_REQUIRED.value
                if mode is PhenotypingMode.UNSUPERVISED_CLUSTERING
                else InteractionReason.PHENOTYPE_DEFINITION_COUNT_REQUIRED.value
            ),
        }
    elif reason is InteractionReason.PHENOTYPE_DEFINITION_COUNT_REQUIRED:
        _require_fields(response, {"phenotype_count"})
        count = _phenotype_count(response["phenotype_count"], field="phenotype_count")
        validated = {
            "phenotype_count": count,
            "next_reason": InteractionReason.PHENOTYPE_CLASSES_REQUIRED.value,
        }
    elif reason in {
        InteractionReason.PHENOTYPE_CLASSES_REQUIRED,
        InteractionReason.SUPERVISED_PHENOTYPE_CLASSES_REQUIRED,
    }:
        _require_fields(response, {"phenotype_classes"})
        count = context.get("phenotype_count")
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ROIAnalysisError("phenotype class interaction lacks a positive integer count")
        classes = _phenotype_classes(response["phenotype_classes"], count)
        mode = _phenotyping_mode_from_context(context)
        validated = {
            "phenotype_classes": [item.to_mapping() for item in classes],
            "next_reason": (
                InteractionReason.MARKER_COMPOSITE_RULES_REQUIRED.value
                if mode is PhenotypingMode.MARKER_RULES
                else InteractionReason.PHENOTYPING_MEASUREMENTS_REQUIRED.value
            ),
        }
    elif reason is InteractionReason.PHENOTYPING_MEASUREMENTS_REQUIRED:
        _require_fields(response, {"feature_ids"})
        available = _string_sequence(
            context.get("available_feature_ids", ()), "available feature IDs"
        )
        selected = _string_sequence(response["feature_ids"], "selected feature IDs")
        if available and not set(selected) <= set(available):
            raise ROIAnalysisError("selected phenotyping feature was not offered")
        if not selected:
            raise ROIAnalysisError("at least one phenotyping feature must be selected")
        mode = _phenotyping_mode_from_context(context)
        validated = {
            "selected_feature_ids": list(selected),
            **(
                {
                    "next_reason": InteractionReason.CLUSTERING_CONFIGURATION_REQUIRED.value,
                }
                if mode is PhenotypingMode.UNSUPERVISED_CLUSTERING
                else {"phenotyping_setup_complete": True}
            ),
        }
    elif reason is InteractionReason.MARKER_COMPOSITE_RULES_REQUIRED:
        _require_fields(response, {"composite_rules", "residual_phenotype_id"})
        classes = context.get("phenotype_classes")
        if isinstance(classes, (str, bytes)) or not isinstance(classes, Sequence):
            raise ROIAnalysisError("marker composite interaction lacks phenotype classes")
        class_ids = tuple(item.get("class_id") for item in classes if isinstance(item, Mapping))
        rules, residual = _marker_composite_response(
            response["composite_rules"],
            response["residual_phenotype_id"],
            class_ids=class_ids,
            biomarker_ids=_string_sequence(
                context.get("known_biomarker_ids", ()), "known biomarker IDs"
            ),
        )
        validated = {
            "composite_rules": rules,
            "residual_phenotype_id": residual,
            "next_reason": InteractionReason.MARKER_THRESHOLD_REVIEW_REQUIRED.value,
        }
    elif reason is InteractionReason.MARKER_THRESHOLD_REVIEW_REQUIRED:
        _require_fields(response, {"accepted_thresholds"})
        biomarkers = _string_sequence(context.get("known_biomarker_ids", ()), "known biomarker IDs")
        thresholds = response["accepted_thresholds"]
        if not isinstance(thresholds, Mapping) or set(thresholds) != set(biomarkers):
            raise ROIAnalysisError("accepted thresholds must cover every known biomarker")
        validated = {
            "accepted_thresholds": {
                marker: _accepted_artifact(artifact, field=f"accepted threshold {marker}")
                for marker, artifact in thresholds.items()
            },
            "next_reason": InteractionReason.MARKER_RULES_REVIEW_REQUIRED.value,
        }
    elif reason is InteractionReason.MARKER_RULES_REVIEW_REQUIRED:
        _require_fields(response, {"accepted_generation"})
        generation = _accepted_artifact(
            response["accepted_generation"],
            field="accepted marker-rule generation",
            expected_view=PhenotypingMode.MARKER_RULES.value,
        )
        validated = {
            "accepted_generation": generation,
            "phenotyping_setup_complete": True,
        }
    elif reason is InteractionReason.CLUSTERING_CONFIGURATION_REQUIRED:
        batch_stage = result.stage == "unsupervised_preparation"
        batch_schema = context.get("schema_version") == "batch-clustering-setup/v2"
        if batch_stage != batch_schema:
            raise ROIAnalysisError("clustering setup stage and schema do not match")
        if batch_stage:
            _require_fields(response, {"feature_ids", "clustering_config"})
            if not {
                "clustering_mode",
            } <= set(context):
                raise ROIAnalysisError("batch clustering scope context is incomplete")
            available = _string_sequence(
                context.get("available_feature_ids"), "available clustering feature IDs"
            )
            if not available:
                raise ROIAnalysisError("batch clustering requires available feature IDs")
            selected = _string_sequence(response["feature_ids"], "selected clustering feature IDs")
            if not selected:
                raise ROIAnalysisError("at least one clustering feature must be selected")
            if not set(selected) <= set(available):
                raise ROIAnalysisError("selected clustering feature was not offered")
            raw_mode = context.get("clustering_mode")
            if raw_mode is None:
                expected_mode = None
            else:
                if type(raw_mode) is not str:
                    raise ROIAnalysisError("batch clustering mode is invalid")
                try:
                    expected_mode = ClusteringMode(raw_mode)
                except ValueError as exc:
                    raise ROIAnalysisError("batch clustering mode is invalid") from exc
        else:
            _require_fields(response, {"clustering_config"})
            selected = _string_sequence(
                context.get("selected_feature_ids", ()), "selected clustering feature IDs"
            )
        clustering = ClusteringConfig.from_mapping(
            response["clustering_config"], field="clustering response"
        )
        if not clustering.enabled:
            raise ROIAnalysisError("accepted clustering configuration must be enabled")
        if batch_stage and expected_mode is not None and clustering.mode is not expected_mode:
            raise ROIAnalysisError("batch clustering mode does not match requested scope")
        configured = tuple(
            raw.get("feature_id") for raw in clustering.feature_specs if isinstance(raw, Mapping)
        )
        if configured != selected:
            raise ROIAnalysisError(
                "clustering feature_specs must exactly match selected_feature_ids "
                "in the selected order"
            )
        validated = {
            **({"selected_feature_ids": list(selected)} if batch_stage else {}),
            "clustering_config": clustering.to_mapping(),
            "phenotyping_setup_complete": True,
        }
    elif reason is InteractionReason.SUPERVISED_TRAINING_ANNOTATIONS_REQUIRED:
        _require_fields(response, {"classifier"})
        validated = {
            "classifier": _supervised_classifier_response(response["classifier"]),
            "supervised_annotations_reviewed": True,
        }
    elif reason is InteractionReason.SUPERVISED_CLASSIFIER_REVIEW_REQUIRED:
        _require_fields(response, {"accepted_generation"})
        generation = _accepted_artifact(
            response["accepted_generation"],
            field="accepted supervised generation",
            expected_view=PhenotypingMode.SUPERVISED_CLASSIFIER.value,
        )
        validated = {
            "accepted_generation": generation,
            "phenotyping_setup_complete": True,
        }
    elif reason is InteractionReason.CLUSTER_LABEL_REVIEW_REQUIRED:
        if action == InteractionAction.ASSIGN_CLUSTER_LABELS.value:
            _require_fields(response, {"cluster_labels"})
            offered = _string_sequence(
                context.get("cluster_ids", result.affected_object_ids),
                "offered cluster IDs",
            )
            raw_labels = response["cluster_labels"]
            if not isinstance(raw_labels, Mapping) or not raw_labels:
                raise ROIAnalysisError("at least one cluster label must be assigned")
            if not set(raw_labels) <= set(offered):
                raise ROIAnalysisError("cluster label references a cluster that was not offered")
            labels: dict[str, str] = {}
            for cluster_id, label in raw_labels.items():
                if not isinstance(label, str) or not label.strip():
                    raise ROIAnalysisError("cluster labels must be non-empty text")
                labels[cluster_id] = label.strip()
            validated = {"cluster_labels": labels}
        elif action in {
            InteractionAction.ACCEPT_UNLABELED_CLUSTERS.value,
            InteractionAction.REVISE_CLUSTERING.value,
        }:
            _require_fields(response, set())
            validated = {}
        else:
            raise ValueError("cluster review action was not offered")
    elif reason is InteractionReason.MEASUREMENT_SETUP_REQUIRED:
        # Core checks the response envelope; the measurement owner validates
        # scientific feature semantics against the authenticated processed frame.
        validate_measurement_setup_context(result)
        _require_fields(response, {"feature_requests"})
        rows = response["feature_requests"]
        if not isinstance(rows, (list, tuple)) or not 1 <= len(rows) <= 1024:
            raise ROIAnalysisError("choose at least one measurement feature")
        fields = {
            "feature_id",
            "measurement_name",
            "kind",
            "channel_id",
            "biomarker",
            "compartment",
            "units",
            "opt_in",
        }
        for row in rows:
            _require_fields(row, fields)
            for key in fields - {"opt_in"}:
                value = row[key]
                if value is None and key in {"channel_id", "biomarker", "compartment", "units"}:
                    continue
                if type(value) is not str or not value or value != value.strip():
                    raise ROIAnalysisError(f"measurement {key} must be explicit canonical text")
            if type(row["opt_in"]) is not bool:
                raise ROIAnalysisError("measurement opt_in must be boolean")
        validated = {"feature_requests": [dict(row) for row in rows]}
    elif reason is InteractionReason.SEGMENTATION_SETUP_REQUIRED:
        from .config import SegmentationConfig

        _require_fields(context, {"schema_version", "checkpoint_id", "section_id"}
            | ({"channels"} if "channels" in context else set())
            | ({"primary_classification_skipped"} if context.get("primary_classification_skipped") is True else set())
            | ({"nuclear_channel_id"} if "nuclear_channel_id" in context else set()))
        if context["schema_version"] != "segmentation-setup-interaction/v1":
            raise ROIAnalysisError("segmentation setup interaction schema is invalid")
        if context["checkpoint_id"] != result.checkpoint_id:
            raise ROIAnalysisError("segmentation setup checkpoint identity is invalid")
        section_id = context["section_id"]
        if type(section_id) is not str or not section_id or section_id != section_id.strip():
            raise ROIAnalysisError("segmentation setup requires an exact section identity")
        _require_fields(response, {"segmentation"})
        selected = SegmentationConfig.from_mapping(response["segmentation"], field="segmentation")
        if context.get("primary_classification_skipped") is True and selected.scope != "accepted_tissue":
            raise ROIAnalysisError("Without primary classification, segment the accepted analysis regions")
        validated = {"segmentation": selected.to_mapping()}
    elif reason is InteractionReason.TISSUE_DETECTOR_SETUP_REQUIRED:
        validated = _tissue_detector_setup_response(result, context, action, response)
    elif reason is InteractionReason.TISSUE_SECTION_REVIEW_REQUIRED:
        fields = {"excluded_roi_ids"} if action == "accept" and "excluded_roi_ids" in response else set()
        _require_fields(response, fields)
        validated = {}
        if fields:
            excluded = _string_sequence(response["excluded_roi_ids"], "excluded tissue sections")
            offered = set(result.affected_object_ids)
            if not set(excluded) <= offered:
                raise ValueError("Only sections in this tissue review can be excluded")
            if not offered - set(excluded):
                raise ValueError("Keep at least one tissue section before accepting")
            validated["excluded_roi_ids"] = list(excluded)
    elif reason is InteractionReason.TISSUE_NATIVE_LEVEL_REQUIRED:
        _require_fields(response, {"native_resolution_index", "level0_opt_in"})
        index = response["native_resolution_index"]
        opt_in = response["level0_opt_in"]
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ValueError("native_resolution_index must be a non-negative integer")
        if not isinstance(opt_in, bool):
            raise ValueError("level0_opt_in must be a boolean")
        inventory = context.get("native_pyramid_inventory")
        if isinstance(inventory, (str, bytes)) or not isinstance(inventory, Sequence):
            raise ValueError("tissue native-level interaction lacks an inventory")
        offered = {
            item.get("native_resolution_index") for item in inventory if isinstance(item, Mapping)
        }
        if index not in offered:
            raise ValueError("native_resolution_index was not offered")
        compatible = context.get("compatible_native_resolution_indices")
        if compatible is not None and index not in compatible:
            raise ValueError(
                "The chosen resolution does not match the saved detector. Choose a compatible "
                "resolution or return to detector setup to choose a different detector."
            )
        if index == 0:
            if context.get("allow_level0_selection") is not True:
                raise ValueError("level-0 tissue selection is not authorized")
            if not opt_in:
                raise ValueError("interactive level-0 tissue selection requires opt-in")
        elif opt_in:
            raise ValueError("level0_opt_in must be false for a lower native level")
        validated = {
            "native_resolution_index": index,
            "level0_opt_in": opt_in,
        }
    elif reason is InteractionReason.TISSUE_DETECTION_CHANNEL_REQUIRED:
        _require_fields(response, {"reference_channel_id"})
        available = _string_sequence(context.get("available_channel_ids"), "available channels")
        selected = response["reference_channel_id"]
        if not isinstance(selected, str) or selected not in available:
            raise ValueError("reference channel must be an available channel")
        validated: dict[str, object] = {"reference_channel_id": selected}
    elif reason in {
        InteractionReason.PRIMARY_TRAINING_ANNOTATIONS_REQUIRED,
        InteractionReason.SECONDARY_TRAINING_ANNOTATIONS_REQUIRED,
    }:
        _require_fields(response, set())
        requirements = context.get("training_annotation_requirements")
        if not isinstance(requirements, Mapping):
            raise ValueError("training interaction lacks annotation requirements")
        classes = requirements.get("classes")
        if isinstance(classes, (str, bytes)) or not isinstance(classes, Sequence):
            raise ValueError("training annotation classes must be a sequence")
        expected: set[str] = set()
        for item in classes:
            if not isinstance(item, Mapping):
                raise ValueError("training annotation class must be a mapping")
            class_id = item.get("class_id")
            required = item.get("required")
            if not isinstance(class_id, str) or not class_id.strip():
                raise ValueError("training class ID must be non-empty text")
            if isinstance(required, bool) or not isinstance(required, int) or required < 0:
                raise ValueError("training required count must be a non-negative integer")
            if class_id in expected:
                raise ValueError("training class IDs must be unique")
            expected.add(class_id)
        validated = {}
    elif reason is InteractionReason.BIOMARKER_THRESHOLD_REQUIRED:
        _require_fields(response, {"known_marker_rules"})
        expected = set(
            _string_sequence(
                context.get("biomarker_ids", result.affected_object_ids),
                "biomarker IDs",
            )
        )
        channel_bindings = context.get("biomarker_channels")
        if not isinstance(channel_bindings, Mapping) or set(channel_bindings) != expected:
            raise ValueError("threshold interaction lacks exact biomarker channel bindings")
        rules = response["known_marker_rules"]
        if not isinstance(rules, Mapping) or set(rules) != expected:
            raise ValueError("threshold rules must cover exactly every displayed biomarker")
        parsed = PhenotypingConfig.from_mapping(
            {
                "mode_policy": {
                    "schema_version": "phenotyping-mode-policy/v1",
                    "mode": "marker_rules",
                },
                "phenotype_classes": [],
                "known_marker_rules": dict(rules),
                "composite_rules": [],
            },
            field="threshold_response",
        )
        validated_rules: dict[str, object] = {}
        for biomarker, rule in parsed.known_marker_rules.items():
            assert isinstance(rule, Mapping)
            if rule.get("mode") != "threshold":
                raise ValueError(f"threshold rule for {biomarker} must use mode='threshold'")
            if rule.get("channel_id") != channel_bindings.get(biomarker):
                raise ValueError(
                    f"threshold rule for {biomarker} does not match its canonical channel"
                )
            if rule.get("confirmed") is not True:
                raise ValueError(f"threshold rule for {biomarker} must be explicitly confirmed")
            validated_rules[biomarker] = _json_value(rule, f"threshold rule for {biomarker}")
        validated = {"known_marker_rules": validated_rules}
    elif reason is InteractionReason.OTHER_EXPLICIT_REASON and isinstance(
        context.get("moving_reference_availability_decision"), Mapping
    ):
        expected = (
            {"moving_image", "moving_image_id", "moving_modality", "source_metadata"}
            if action == "moving_reference_available"
            else set()
        )
        _require_fields(response, expected)
        validated = {}
        if action == "moving_reference_available":
            raw_path = response["moving_image"]
            if not isinstance(raw_path, str) or not raw_path.strip():
                raise ValueError("moving_image must be a non-empty local file path")
            moving_image = Path(raw_path).expanduser().resolve()
            if not moving_image.is_file():
                raise ValueError(f"moving_image does not exist or is not a file: {moving_image}")
            validated["moving_image"] = str(moving_image)
            for field in ("moving_image_id", "moving_modality"):
                raw = response[field]
                if not isinstance(raw, str) or not raw.strip():
                    raise ValueError(f"{field} must be a non-empty string")
                validated[field] = raw.strip()
            validated["source_metadata"] = _json_value(
                response["source_metadata"], "source_metadata"
            )
    elif reason is InteractionReason.OTHER_EXPLICIT_REASON and isinstance(
        context.get("moving_reference_usage_decision"), Mapping
    ):
        _require_fields(response, set())
        validated = {}
    elif reason is InteractionReason.OTHER_EXPLICIT_REASON and isinstance(
        context.get("classifier_reuse_decision"), Mapping
    ):
        _require_fields(response, {"decided_by"})
        decided_by = response["decided_by"]
        if not isinstance(decided_by, str) or not decided_by.strip():
            raise ValueError("classifier reuse decision requires an operator identifier")
        validated = {"decided_by": decided_by.strip()}
    elif reason is InteractionReason.OTHER_EXPLICIT_REASON and isinstance(
        context.get("moving_assistance_scope_decision"), Mapping
    ):
        expected_fields = (
            {"decided_by", "moving_assistance_scopes"}
            if action == "set_moving_assistance_scopes"
            else {"decided_by"}
        )
        _require_fields(response, expected_fields)
        decided_by = response["decided_by"]
        if not isinstance(decided_by, str) or not decided_by.strip():
            raise ValueError("moving assistance decision requires an operator identifier")
        validated = {"decided_by": decided_by.strip()}
        if action == "set_moving_assistance_scopes":
            raw_scopes = response["moving_assistance_scopes"]
            if isinstance(raw_scopes, (str, bytes)) or not isinstance(raw_scopes, Sequence):
                raise ValueError("moving assistance scopes must be an array")
            decision = context["moving_assistance_scope_decision"]
            assert isinstance(decision, Mapping)
            eligible = decision.get("eligible_classifier_scopes")
            registrations = decision.get("available_registrations")
            if not isinstance(eligible, Sequence) or isinstance(eligible, (str, bytes)):
                raise ValueError("moving assistance interaction lacks eligible scopes")
            if not isinstance(registrations, Sequence) or isinstance(registrations, (str, bytes)):
                raise ValueError("moving assistance interaction lacks passed registrations")
            eligible_keys = {
                _scope_selector(item) for item in eligible if isinstance(item, Mapping)
            }
            available = {
                (item.get("section_id"), item.get("registration_id"))
                for item in registrations
                if isinstance(item, Mapping) and item.get("registration_qc") == {"status": "passed"}
            }
            parsed_scopes = []
            seen = set()
            for raw in raw_scopes:
                if not isinstance(raw, Mapping):
                    raise ValueError("moving assistance scope row must be an object")
                _require_fields(
                    raw,
                    {
                        "stage",
                        "analysis_domain_id",
                        "structure_id",
                        "eligible_parent_ids",
                        "registration_id",
                    },
                )
                selector = _scope_selector(raw)
                if selector not in eligible_keys:
                    raise ValueError("moving assistance scope is not an offered classifier scope")
                offered = next(
                    (
                        item
                        for item in eligible
                        if isinstance(item, Mapping) and _scope_selector(item) == selector
                    ),
                    None,
                )
                if (
                    offered is None
                    or (offered.get("section_id"), raw.get("registration_id")) not in available
                ):
                    raise ValueError("moving assistance scope lacks exact passed registration QC")
                full = (*selector, raw.get("registration_id"))
                if full in seen:
                    raise ValueError("moving assistance scopes must be unique")
                seen.add(full)
                parsed_scopes.append(dict(raw))
            if not parsed_scopes:
                raise ValueError("set moving assistance requires at least one scope")
            validated["moving_assistance_scopes"] = parsed_scopes
    elif reason is InteractionReason.TRANSFER_REVIEW_REQUIRED:
        _require_fields(response, {"reviewed_by"})
        reviewed_by = response["reviewed_by"]
        if not isinstance(reviewed_by, str) or not reviewed_by.strip():
            raise ValueError("transfer review requires an operator identifier")
        validated = {"reviewed_by": reviewed_by.strip()}
    elif reason is InteractionReason.REGISTRATION_REVIEW_REQUIRED:
        expected = (
            {"reviewed_by", "source_object_ids"}
            if action == "accept_registration_and_transfer"
            else {"reviewed_by"} if action in {"accept_registration", "reject"} else set()
        )
        _require_fields(response, expected)
        validated = {}
        if expected:
            reviewed_by = response["reviewed_by"]
            if not isinstance(reviewed_by, str) or not reviewed_by.strip():
                raise ValueError("registration review requires an operator identifier")
            validated["reviewed_by"] = reviewed_by.strip()
        if action == "accept_registration_and_transfer":
            source_ids = _string_sequence(response["source_object_ids"], "source_object_ids")
            if not source_ids or len(set(source_ids)) != len(source_ids):
                raise ValueError("source_object_ids must be nonempty and unique")
            validated["source_object_ids"] = list(source_ids)
    else:
        _require_fields(response, set())
        validated = {}

    resumed = _json_value(context, "resume context")
    assert isinstance(resumed, dict)
    resumed["action"] = action
    resumed["response"] = validated
    resumed.update(validated)
    if validated.get("phenotyping_setup_complete") is True:
        resumed.pop("next_reason", None)
    return resumed


_PHENOTYPING_REASONS = frozenset(
    {
        InteractionReason.PHENOTYPING_MODE_REQUIRED,
        InteractionReason.PHENOTYPE_DEFINITION_COUNT_REQUIRED,
        InteractionReason.PHENOTYPE_CLASSES_REQUIRED,
        InteractionReason.PHENOTYPING_MEASUREMENTS_REQUIRED,
        InteractionReason.MARKER_THRESHOLD_REVIEW_REQUIRED,
        InteractionReason.MARKER_COMPOSITE_RULES_REQUIRED,
        InteractionReason.MARKER_RULES_REVIEW_REQUIRED,
        InteractionReason.SUPERVISED_PHENOTYPE_CLASSES_REQUIRED,
        InteractionReason.SUPERVISED_TRAINING_ANNOTATIONS_REQUIRED,
        InteractionReason.SUPERVISED_CLASSIFIER_REVIEW_REQUIRED,
        InteractionReason.CLUSTERING_CONFIGURATION_REQUIRED,
        InteractionReason.CLUSTER_LABEL_REVIEW_REQUIRED,
    }
)


_ACTION_ONLY_REASONS = frozenset(
    {
        InteractionReason.ANALYSIS_DOMAIN_CHOICE_REQUIRED,
        InteractionReason.ANALYSIS_DOMAIN_ROIS_REQUIRED,
        InteractionReason.ANALYSIS_DOMAIN_REVIEW_REQUIRED,
        InteractionReason.ROI_REVIEW_REQUIRED,
        InteractionReason.STRUCTURE_COUNT_POLICY_ACKNOWLEDGEMENT_REQUIRED,
        InteractionReason.PRIMARY_STRUCTURE_COUNT_REVIEW_REQUIRED,
    }
)


def next_phenotyping_interaction(
    config: object,
    *,
    state: Mapping[str, object],
) -> StageResult:
    """Return the next bounded, presentation-neutral phenotyping checkpoint."""
    slide_id = getattr(config, "slide_id", None)
    if not isinstance(slide_id, str) or not slide_id.strip():
        raise ROIAnalysisError("phenotyping interaction requires a configured slide_id")
    context = _bounded_phenotyping_context(config, state)
    channels = getattr(config, "channels", None)
    if isinstance(channels, (str, bytes)) or not isinstance(channels, Sequence):
        raise ROIAnalysisError("phenotyping interaction requires configured channels")
    if context.get("phenotyping_aborted") is True:
        raise ROIAnalysisError("aborted phenotyping has no next interaction")
    if context.get("phenotyping_setup_complete") is True:
        raise ROIAnalysisError("completed phenotyping setup has no next interaction")
    known_biomarkers = tuple(
        channel.biomarker
        for channel in channels
        if isinstance(getattr(channel, "biomarker", None), str) and channel.biomarker.strip()
    )
    context["known_biomarker_ids"] = list(known_biomarkers)
    raw_reason = context.get("next_reason")
    if raw_reason is None:
        reason = InteractionReason.PHENOTYPING_MODE_REQUIRED
    else:
        try:
            reason = InteractionReason(raw_reason)
        except (TypeError, ValueError) as exc:
            raise ROIAnalysisError(
                "phenotyping interaction state has an invalid next_reason"
            ) from exc
    context["schema_version"] = "phenotyping-interaction/v1"
    if reason is InteractionReason.PHENOTYPING_MODE_REQUIRED:
        available_modes = (
            (
                PhenotypingMode.MARKER_RULES,
                PhenotypingMode.SUPERVISED_CLASSIFIER,
                PhenotypingMode.UNSUPERVISED_CLUSTERING,
            )
            if known_biomarkers
            else (
                PhenotypingMode.UNSUPERVISED_CLUSTERING,
                PhenotypingMode.SUPERVISED_CLASSIFIER,
            )
        )
        context.update(
            {
                "available_modes": [item.value for item in available_modes],
                "recommended_mode": available_modes[0].value,
                "known_biomarker_ids": list(known_biomarkers),
                "unavailable_modes": (
                    {}
                    if known_biomarkers
                    else {
                        PhenotypingMode.MARKER_RULES.value: (
                            "Marker rules require at least one canonical biomarker."
                        )
                    }
                ),
            }
        )
        return _phenotyping_request(
            config,
            reason,
            "Choose how to assign phenotypes to these cells.",
            (InteractionAction.SELECT_PHENOTYPING_MODE.value, InteractionAction.ABORT.value),
            context,
        )
    mode = _phenotyping_mode_from_context(context)
    if reason is InteractionReason.PHENOTYPE_DEFINITION_COUNT_REQUIRED:
        if mode is PhenotypingMode.UNSUPERVISED_CLUSTERING:
            raise ROIAnalysisError("unsupervised clustering does not define a phenotype count")
        return _phenotyping_request(
            config,
            reason,
            "Declare the positive number of phenotype definitions.",
            (InteractionAction.DECLARE_PHENOTYPE_COUNT.value, InteractionAction.ABORT.value),
            context,
        )
    if reason in {
        InteractionReason.PHENOTYPE_CLASSES_REQUIRED,
        InteractionReason.SUPERVISED_PHENOTYPE_CLASSES_REQUIRED,
    }:
        return _phenotyping_request(
            config,
            reason,
            "Declare the exact phenotype classes in user-defined order.",
            (InteractionAction.DECLARE_PHENOTYPE_CLASSES.value, InteractionAction.ABORT.value),
            context,
        )
    if reason is InteractionReason.PHENOTYPING_MEASUREMENTS_REQUIRED:
        return _phenotyping_request(
            config,
            reason,
            "Select the exact measured features for the chosen phenotyping method.",
            (
                InteractionAction.SELECT_PHENOTYPING_MEASUREMENTS.value,
                InteractionAction.ABORT.value,
            ),
            context,
        )
    if reason is InteractionReason.MARKER_COMPOSITE_RULES_REQUIRED:
        return _phenotyping_request(
            config,
            reason,
            "Define the declared marker-rule composites in precedence order.",
            (InteractionAction.DEFINE_MARKER_COMPOSITE_RULES.value, InteractionAction.ABORT.value),
            context,
        )
    if reason is InteractionReason.MARKER_THRESHOLD_REVIEW_REQUIRED:
        return _phenotyping_request(
            config,
            reason,
            "Review and accept one threshold artifact for every known biomarker.",
            (InteractionAction.REVIEW_MARKER_THRESHOLDS.value, InteractionAction.ABORT.value),
            context,
        )
    if reason is InteractionReason.MARKER_RULES_REVIEW_REQUIRED:
        return _phenotyping_request(
            config,
            reason,
            "Review the complete ordered marker-rule phenotype generation.",
            (InteractionAction.REVIEW_MARKER_RULES.value, InteractionAction.ABORT.value),
            context,
        )
    if reason is InteractionReason.CLUSTERING_CONFIGURATION_REQUIRED:
        return _phenotyping_request(
            config,
            reason,
            "Confirm the exact neutral clustering feature and graph configuration.",
            (InteractionAction.CONFIGURE_CLUSTERING.value, InteractionAction.ABORT.value),
            context,
        )
    raise ROIAnalysisError(f"phenotyping interaction reason {reason.value!r} is not available yet")


def _validate_phenotyping_resume_state(
    config: object, state: Mapping[str, object]
) -> dict[str, object]:
    """Revalidate every persisted artifact before resumed phenotyping executes."""
    return _bounded_phenotyping_context(config, state)


def _phenotyping_request(
    config: object,
    reason: InteractionReason,
    message: str,
    actions: tuple[str, ...],
    context: Mapping[str, object],
) -> StageResult:
    return StageResult.needs_interaction(
        stage="phenotyping",
        slide_id=getattr(config, "slide_id"),
        reason=reason,
        message=message,
        allowed_actions=actions,
        resume_context=context,
        checkpoint_id="pending-phenotyping-checkpoint",
    )


def _bounded_phenotyping_context(config: object, state: Mapping[str, object]) -> dict[str, object]:
    """Validate project identity and retain only resumable phenotyping state."""
    if not isinstance(state, Mapping) or not all(isinstance(key, str) for key in state):
        raise ROIAnalysisError("phenotyping interaction state must be a string-keyed mapping")
    required = {
        "qupath_project_path",
        "canonical_image_id",
        "canonical_image_sha256",
        "config_hash",
        "generation_id",
    }
    missing = sorted(required - set(state))
    if missing:
        raise ROIAnalysisError(
            "phenotyping interaction lacks required project identity fields: " + ", ".join(missing)
        )
    project_value = state["qupath_project_path"]
    if not isinstance(project_value, str) or not project_value.strip():
        raise ROIAnalysisError("phenotyping interaction requires a project path")
    project_path = Path(project_value).expanduser().resolve()
    output_dir = getattr(config, "output_dir", None)
    if not isinstance(output_dir, Path) or project_path.parent != output_dir.resolve():
        raise ROIAnalysisError("phenotyping project path must be project-owned")
    if project_path.suffix != ".qpproj" or not project_path.is_file():
        raise ROIAnalysisError("phenotyping project path must be a prepared .qpproj file")
    context: dict[str, object] = {"qupath_project_path": str(project_path)}
    for field in ("canonical_image_id", "generation_id"):
        value = state[field]
        if not isinstance(value, str) or not value.strip():
            raise ROIAnalysisError(f"phenotyping interaction {field} must be non-empty text")
        context[field] = value.strip()
    for field in ("canonical_image_sha256", "config_hash"):
        value = state[field]
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ROIAnalysisError(f"phenotyping interaction {field} must be a lowercase SHA-256")
        context[field] = value
    if "phenotyping_mode" in state:
        try:
            context["phenotyping_mode"] = PhenotypingMode(state["phenotyping_mode"]).value
        except (TypeError, ValueError) as exc:
            raise ROIAnalysisError("phenotyping interaction has an invalid mode") from exc
    if "next_reason" in state:
        try:
            next_reason = InteractionReason(state["next_reason"])
        except (TypeError, ValueError) as exc:
            raise ROIAnalysisError(
                "phenotyping interaction state has an invalid next_reason"
            ) from exc
        if next_reason not in _PHENOTYPING_REASONS:
            raise ROIAnalysisError(
                "phenotyping interaction next_reason is not a phenotyping reason"
            )
        context["next_reason"] = next_reason.value
    if "phenotype_count" in state:
        if "phenotyping_mode" not in context:
            raise ROIAnalysisError("phenotype_count requires a selected phenotyping mode")
        context["phenotype_count"] = _phenotype_count(
            state["phenotype_count"], field="phenotype_count"
        )
    if "phenotype_classes" in state:
        if "phenotype_count" not in context:
            raise ROIAnalysisError("phenotype_classes requires phenotype_count")
        declared_phase = context.get("next_reason") in {
            InteractionReason.MARKER_COMPOSITE_RULES_REQUIRED.value,
            InteractionReason.MARKER_THRESHOLD_REVIEW_REQUIRED.value,
            InteractionReason.MARKER_RULES_REVIEW_REQUIRED.value,
            InteractionReason.PHENOTYPING_MEASUREMENTS_REQUIRED.value,
        }
        completed_phase = (
            state.get("phenotyping_setup_complete") is True and "next_reason" not in context
        )
        if not declared_phase and not completed_phase:
            raise ROIAnalysisError("phenotype_classes are not permitted before declaration")
        classes = _carried_phenotype_classes(state["phenotype_classes"], context["phenotype_count"])
        context["phenotype_classes"] = [item.to_mapping() for item in classes]
    for field in ("available_feature_ids", "selected_feature_ids"):
        if field in state:
            context[field] = list(_string_sequence(state[field], field.replace("_", " ")))
    if "composite_rules" in state:
        rules = _json_value(state["composite_rules"], "composite rules")
        if not isinstance(rules, list):
            raise ROIAnalysisError("composite_rules must be a sequence")
        context["composite_rules"] = rules
    if "residual_phenotype_id" in state:
        residual = state["residual_phenotype_id"]
        if not isinstance(residual, str) or not residual.strip():
            raise ROIAnalysisError("residual_phenotype_id must be non-empty text")
        context["residual_phenotype_id"] = residual
    if "accepted_thresholds" in state:
        thresholds = state["accepted_thresholds"]
        if not isinstance(thresholds, Mapping):
            raise ROIAnalysisError("accepted_thresholds must be a mapping")
        context["accepted_thresholds"] = {
            marker: _accepted_artifact(artifact, field=f"accepted threshold {marker}")
            for marker, artifact in thresholds.items()
        }
    if "clustering_config" in state:
        context["clustering_config"] = ClusteringConfig.from_mapping(
            state["clustering_config"], field="carried clustering configuration"
        ).to_mapping()
    if "classifier" in state:
        context["classifier"] = _supervised_classifier_response(state["classifier"])
    if "accepted_generation" in state:
        context["accepted_generation"] = _accepted_artifact(
            state["accepted_generation"], field="accepted phenotype generation"
        )
    if "phenotyping_setup_complete" in state:
        if not isinstance(state["phenotyping_setup_complete"], bool):
            raise ROIAnalysisError("phenotyping_setup_complete must be a boolean")
        context["phenotyping_setup_complete"] = state["phenotyping_setup_complete"]
    if "phenotyping_aborted" in state:
        if not isinstance(state["phenotyping_aborted"], bool):
            raise ROIAnalysisError("phenotyping_aborted must be a boolean")
        context["phenotyping_aborted"] = state["phenotyping_aborted"]
    _validate_completed_phenotyping_context(config, context)
    return context


def _validate_completed_phenotyping_context(config: object, context: Mapping[str, object]) -> None:
    """Reject forged completed setup while retaining its declared class schema.

    The required mapping shape includes ``selected_feature_ids``, ``clustering_config``.
    Field checks are applied before the validated value is returned or passed to its next
    constructor."""
    if context.get("phenotyping_setup_complete") is not True:
        return
    if "next_reason" in context:
        raise ROIAnalysisError("completed phenotyping state cannot retain next_reason")
    mode = _phenotyping_mode_from_context(context)
    if mode is PhenotypingMode.MARKER_RULES:
        required = {
            "phenotype_count",
            "phenotype_classes",
            "composite_rules",
            "residual_phenotype_id",
            "accepted_thresholds",
            "accepted_generation",
        }
        if not required <= set(context):
            raise ROIAnalysisError("completed marker phenotyping state is incomplete")
        if any(
            field in context
            for field in ("selected_feature_ids", "clustering_config", "classifier")
        ):
            raise ROIAnalysisError(
                "completed marker phenotyping state contains mode-incompatible fields"
            )
        classes = context["phenotype_classes"]
        assert isinstance(classes, list)
        class_ids = tuple(item["class_id"] for item in classes)
        known_biomarkers = _configured_biomarker_ids(config)
        _validate_carried_marker_composites(
            context["composite_rules"],
            context["residual_phenotype_id"],
            class_ids=class_ids,
            biomarker_ids=known_biomarkers,
        )
        thresholds = context["accepted_thresholds"]
        if not isinstance(thresholds, Mapping) or set(thresholds) != set(known_biomarkers):
            raise ROIAnalysisError(
                "completed marker phenotyping thresholds must cover every known biomarker"
            )
        generation = context["accepted_generation"]
        if (
            not isinstance(generation, Mapping)
            or generation.get("selected_display_view") != PhenotypingMode.MARKER_RULES.value
            or not isinstance(generation.get("generation_id"), str)
            or not generation["generation_id"].strip()
        ):
            raise ROIAnalysisError("completed marker phenotyping generation is invalid")
        return
    if mode is PhenotypingMode.SUPERVISED_CLASSIFIER:
        required = {"phenotype_count", "phenotype_classes", "selected_feature_ids"}
        if not required <= set(context):
            raise ROIAnalysisError("completed supervised phenotyping state is incomplete")
        if any(
            field in context
            for field in ("composite_rules", "residual_phenotype_id", "accepted_thresholds")
        ):
            raise ROIAnalysisError(
                "completed supervised phenotyping state contains mode-incompatible fields"
            )
        generation = context.get("accepted_generation")
        if generation is not None and (
            not isinstance(generation, Mapping)
            or generation.get("selected_display_view")
            != PhenotypingMode.SUPERVISED_CLASSIFIER.value
        ):
            raise ROIAnalysisError("completed supervised phenotyping generation is invalid")
        _validate_completed_feature_selection(context, mode="supervised")
        return
    if mode is PhenotypingMode.UNSUPERVISED_CLUSTERING:
        required = {"selected_feature_ids", "clustering_config"}
        if not required <= set(context):
            raise ROIAnalysisError("completed unsupervised phenotyping state is incomplete")
        if "phenotype_count" in context or "phenotype_classes" in context:
            raise ROIAnalysisError(
                "completed unsupervised phenotyping state cannot declare phenotype classes"
            )
        if any(
            field in context
            for field in (
                "composite_rules",
                "residual_phenotype_id",
                "accepted_thresholds",
                "classifier",
                "accepted_generation",
            )
        ):
            raise ROIAnalysisError(
                "completed unsupervised phenotyping state contains mode-incompatible fields"
            )
        _validate_completed_feature_selection(context, mode="unsupervised")
        clustering = context["clustering_config"]
        assert isinstance(clustering, Mapping)
        configured = tuple(
            raw.get("feature_id") for raw in clustering["feature_specs"] if isinstance(raw, Mapping)
        )
        selected = tuple(context["selected_feature_ids"])
        if configured != selected:
            raise ROIAnalysisError(
                "completed unsupervised clustering feature_specs must exactly "
                "match selected_feature_ids in the selected order"
            )


def _validate_completed_feature_selection(context: Mapping[str, object], *, mode: str) -> None:
    selected = context.get("selected_feature_ids")
    if not isinstance(selected, list) or not selected:
        raise ROIAnalysisError(f"completed {mode} phenotyping feature schema is incomplete")
    available = context.get("available_feature_ids")
    if isinstance(available, list) and not set(selected) <= set(available):
        raise ROIAnalysisError(
            f"completed {mode} phenotyping feature schema contains an unavailable feature"
        )


def _configured_biomarker_ids(config: object) -> tuple[str, ...]:
    channels = getattr(config, "channels", ())
    return tuple(
        channel.biomarker
        for channel in channels
        if isinstance(getattr(channel, "biomarker", None), str) and channel.biomarker.strip()
    )


def _validate_carried_marker_composites(
    value: object,
    residual_value: object,
    *,
    class_ids: Sequence[str],
    biomarker_ids: Sequence[str],
) -> None:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ROIAnalysisError("completed marker composite state is invalid")
    if not isinstance(residual_value, str) or residual_value not in class_ids:
        raise ROIAnalysisError("completed marker residual phenotype is invalid")
    seen: set[str] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping) or set(raw) != {
            "phenotype_id",
            "marker_states",
            "precedence",
        }:
            raise ROIAnalysisError("completed marker composite state is invalid")
        phenotype_id = raw["phenotype_id"]
        marker_states = raw["marker_states"]
        if (
            not isinstance(phenotype_id, str)
            or phenotype_id not in class_ids
            or phenotype_id == residual_value
            or phenotype_id in seen
            or raw["precedence"] != index
            or not isinstance(marker_states, Mapping)
            or set(marker_states) != set(biomarker_ids)
            or any(
                marker_state not in {"positive", "negative", "ignore"}
                for marker_state in marker_states.values()
            )
        ):
            raise ROIAnalysisError("completed marker composite state is invalid")
        seen.add(phenotype_id)


def _inferred_phenotyping_action(reason: InteractionReason) -> str:
    if reason is InteractionReason.PHENOTYPING_MODE_REQUIRED:
        return InteractionAction.SELECT_PHENOTYPING_MODE.value
    if reason is InteractionReason.PHENOTYPE_DEFINITION_COUNT_REQUIRED:
        return InteractionAction.DECLARE_PHENOTYPE_COUNT.value
    if reason in {
        InteractionReason.PHENOTYPE_CLASSES_REQUIRED,
        InteractionReason.SUPERVISED_PHENOTYPE_CLASSES_REQUIRED,
    }:
        return InteractionAction.DECLARE_PHENOTYPE_CLASSES.value
    raise ValueError("an explicit action is required for this interaction")


def _phenotyping_mode_from_context(context: Mapping[str, object]) -> PhenotypingMode:
    try:
        return PhenotypingMode(context.get("phenotyping_mode"))
    except (TypeError, ValueError) as exc:
        raise ROIAnalysisError("phenotyping interaction lacks a selected mode") from exc


def _phenotype_classes(value: object, count: int) -> tuple[PhenotypeClassSpec, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ROIAnalysisError("phenotype_classes must be a sequence")
    if len(value) != count:
        raise ROIAnalysisError("phenotype_classes must contain exactly the declared count")
    classes: list[PhenotypeClassSpec] = []
    class_ids: set[str] = set()
    display_names: set[str] = set()
    for precedence, raw in enumerate(value):
        if not isinstance(raw, Mapping) or set(raw) != {"class_id", "display_name"}:
            raise ROIAnalysisError("phenotype class must contain exactly class_id and display_name")
        item = _phenotype_class_spec(raw["class_id"], raw["display_name"], precedence)
        if item.class_id in class_ids or item.display_name.casefold() in display_names:
            raise ROIAnalysisError("phenotype class IDs and display names must be unique")
        class_ids.add(item.class_id)
        display_names.add(item.display_name.casefold())
        classes.append(item)
    return tuple(classes)


def _carried_phenotype_classes(value: object, count: object) -> tuple[PhenotypeClassSpec, ...]:
    parsed_count = _phenotype_count(count, field="phenotype_count")
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ROIAnalysisError("phenotype_classes must be a sequence")
    if len(value) != parsed_count:
        raise ROIAnalysisError("phenotype_classes must contain exactly the declared count")
    classes: list[PhenotypeClassSpec] = []
    class_ids: set[str] = set()
    display_names: set[str] = set()
    for precedence, raw in enumerate(value):
        if not isinstance(raw, Mapping) or set(raw) != {
            "class_id",
            "display_name",
            "precedence",
        }:
            raise ROIAnalysisError("carried phenotype class has an invalid shape")
        if raw["precedence"] != precedence:
            raise ROIAnalysisError("carried phenotype class precedence is invalid")
        item = _phenotype_class_spec(raw["class_id"], raw["display_name"], precedence)
        if item.class_id in class_ids or item.display_name.casefold() in display_names:
            raise ROIAnalysisError("phenotype class IDs and display names must be unique")
        class_ids.add(item.class_id)
        display_names.add(item.display_name.casefold())
        classes.append(item)
    return tuple(classes)


def _phenotype_count(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ROIAnalysisError(f"{field} must be a positive integer")
    if value > MAX_PHENOTYPE_DEFINITION_COUNT:
        raise ROIAnalysisError(f"{field} must be at most {MAX_PHENOTYPE_DEFINITION_COUNT}")
    return value


def _phenotype_class_spec(
    class_id: object, display_name: object, precedence: int
) -> PhenotypeClassSpec:
    if not isinstance(class_id, str) or not class_id.strip():
        raise ROIAnalysisError("phenotype class_id must be non-empty text")
    if not isinstance(display_name, str) or not display_name.strip():
        raise ROIAnalysisError("phenotype display_name must be non-empty text")
    normalized_id = class_id.strip()
    normalized_name = display_name.strip()
    if len(normalized_id) > MAX_PHENOTYPE_CLASS_ID_LENGTH:
        raise ROIAnalysisError("phenotype class_id exceeds the maximum length")
    if len(normalized_name) > MAX_PHENOTYPE_DISPLAY_NAME_LENGTH:
        raise ROIAnalysisError("phenotype display_name exceeds the maximum length")
    return PhenotypeClassSpec(normalized_id, normalized_name, precedence)


def _marker_composite_response(
    value: object,
    residual_value: object,
    *,
    class_ids: Sequence[object],
    biomarker_ids: Sequence[str],
) -> tuple[list[dict[str, object]], str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ROIAnalysisError("composite_rules must be a sequence")
    if not isinstance(residual_value, str) or not residual_value.strip():
        raise ROIAnalysisError("residual_phenotype_id must be non-empty text")
    residual = residual_value.strip()
    expected_classes = tuple(item for item in class_ids if isinstance(item, str) and item.strip())
    if len(expected_classes) != len(class_ids) or residual not in expected_classes:
        raise ROIAnalysisError("residual phenotype must be one declared phenotype class")
    rules: list[dict[str, object]] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping) or set(raw) != {"phenotype_id", "marker_states"}:
            raise ROIAnalysisError("composite rule requires exactly phenotype_id and marker_states")
        phenotype_id = raw["phenotype_id"]
        if (
            not isinstance(phenotype_id, str)
            or phenotype_id not in expected_classes
            or phenotype_id == residual
            or phenotype_id in seen
        ):
            raise ROIAnalysisError("composite rule phenotype is invalid or duplicated")
        marker_states = raw["marker_states"]
        if not isinstance(marker_states, Mapping) or set(marker_states) != set(biomarker_ids):
            raise ROIAnalysisError("composite marker states must cover every known biomarker")
        normalized_states: dict[str, str] = {}
        for marker, marker_state in marker_states.items():
            if marker_state not in {"positive", "negative", "ignore"}:
                raise ROIAnalysisError("composite marker state is invalid")
            normalized_states[marker] = marker_state
        seen.add(phenotype_id)
        rules.append(
            {
                "phenotype_id": phenotype_id,
                "marker_states": normalized_states,
                "precedence": index,
            }
        )
    if set(expected_classes) != seen | {residual}:
        raise ROIAnalysisError(
            "composite rules and residual must cover every declared phenotype class"
        )
    return rules, residual


def _accepted_artifact(
    value: object,
    *,
    field: str,
    expected_view: str | None = None,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ROIAnalysisError(f"{field} must be a mapping")
    required = {"artifact_path", "artifact_sha256"}
    allowed = required | {"generation_id", "selected_display_view"}
    if set(value) - allowed or not required <= set(value):
        raise ROIAnalysisError(f"{field} fields are invalid")
    path = value["artifact_path"]
    if not isinstance(path, str) or not path.strip():
        raise ROIAnalysisError(f"{field} artifact_path must be non-empty text")
    digest = value["artifact_sha256"]
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ROIAnalysisError(f"{field} artifact_sha256 must be a lowercase SHA-256")
    try:
        artifact_path = Path(path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ROIAnalysisError(f"{field} artifact_path must be an existing local file") from exc
    if not artifact_path.is_file():
        raise ROIAnalysisError(f"{field} artifact_path must be an existing local file")
    actual = hashlib.sha256()
    try:
        with artifact_path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                actual.update(chunk)
    except OSError as exc:
        raise ROIAnalysisError(f"{field} artifact_path must be readable") from exc
    if actual.hexdigest() != digest:
        raise ROIAnalysisError(f"{field} artifact_sha256 does not match the complete file")
    generation_id = value.get("generation_id")
    if generation_id is not None and (
        not isinstance(generation_id, str) or not generation_id.strip()
    ):
        raise ROIAnalysisError(f"{field} generation_id must be non-empty text")
    selected_view = value.get("selected_display_view")
    if expected_view is not None and selected_view != expected_view:
        raise ROIAnalysisError(f"{field} selected display view is invalid")
    normalized = {
        "artifact_path": str(artifact_path),
        "artifact_sha256": digest,
    }
    if generation_id is not None:
        normalized["generation_id"] = generation_id.strip()
    if selected_view is not None:
        normalized["selected_display_view"] = selected_view
    return normalized


def _manual_secondary_selection_response(
    context: Mapping[str, object], response: Mapping[str, object]
) -> dict[str, object]:
    """Validate one exact manual cap against its immutable inventory artifact."""
    _require_fields(
        response,
        {
            "selected_content_keys",
            "selection_criterion",
            "reviewer_id",
            "rationale",
        },
    )
    service_context = resolve_resume_envelope(context).service_context
    inventory = _manual_secondary_inventory(service_context)
    selected = _string_sequence(response["selected_content_keys"], "selected_content_keys")
    maximum = inventory["maximum"]
    assert isinstance(maximum, int) and not isinstance(maximum, bool)
    if len(selected) != maximum:
        raise ValueError("manual selection requires exactly maximum content keys")
    inventory_keys = tuple(str(item["content_key"]) for item in inventory["items"])
    selected_set = set(selected)
    if not selected_set <= set(inventory_keys):
        raise ValueError("manual selection contains a content key outside the bound inventory")
    canonical_selected = tuple(key for key in inventory_keys if key in selected_set)
    if selected != canonical_selected:
        raise ValueError("manual selection keys must use canonical inventory order")
    text_fields: dict[str, str] = {}
    for field in ("selection_criterion", "reviewer_id", "rationale"):
        value = response[field]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"manual selection {field} must be non-empty text")
        text_fields[field] = value.strip()
    return {
        "selected_content_keys": list(selected),
        **text_fields,
    }


def _manual_secondary_inventory(
    context: Mapping[str, object],
) -> dict[str, object]:
    binding_fields = (
        "slide_id",
        "section_id",
        "analysis_domain_id",
        "analysis_domain_revision",
        "analysis_domain_revision_sha256",
        "tissue_generation_id",
        "pipeline_run_id",
        "config_sha256",
        "semantic_result_set_sha256",
        "structure_id",
        "maximum",
        "observed_count",
        "inventory_sha256",
        "selection_algorithm",
    )
    descriptor = context.get("inventory_descriptor")
    if not isinstance(descriptor, Mapping) or set(descriptor) != {
        "artifact_id",
        "schema_version",
        "path",
        "sha256",
        "record_count",
    }:
        raise ValueError("manual selection inventory descriptor fields are invalid")
    for field in ("artifact_id", "schema_version", "path"):
        value = descriptor.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"manual selection inventory descriptor {field} is invalid")
    digest = _interaction_sha256(
        descriptor.get("sha256"), "manual selection inventory descriptor SHA-256"
    )
    record_count = _strict_nonnegative_int(
        descriptor.get("record_count"), "manual selection inventory record_count"
    )
    try:
        path = Path(str(descriptor["path"])).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ValueError("manual selection inventory artifact is missing") from exc
    if not path.is_file():
        raise ValueError("manual selection inventory artifact is missing")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ValueError("manual selection inventory artifact is unreadable") from exc
    if hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("manual selection inventory artifact SHA-256 does not match")
    try:
        payload = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("manual selection inventory artifact is not strict JSON") from exc
    expected_fields = {"schema_version", *binding_fields, "items"}
    if not isinstance(payload, dict) or set(payload) != expected_fields:
        raise ValueError("manual selection inventory artifact fields are invalid")
    if any(context.get(field) != payload.get(field) for field in binding_fields):
        raise ValueError("manual selection inventory binding is stale")
    if (
        context.get("schema_version") != "secondary-structure-selection/v1"
        or descriptor.get("schema_version") != "structure-count-inventory/v1"
        or payload.get("schema_version") != descriptor.get("schema_version")
        or payload.get("selection_algorithm") != "manual-select/v1"
    ):
        raise ValueError("manual selection inventory schema or algorithm is invalid")
    for field in (
        "slide_id",
        "section_id",
        "tissue_generation_id",
        "pipeline_run_id",
        "structure_id",
    ):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"manual selection inventory {field} is invalid")
    for field in (
        "analysis_domain_id",
        "analysis_domain_revision_sha256",
        "config_sha256",
        "semantic_result_set_sha256",
        "inventory_sha256",
    ):
        _interaction_sha256(payload.get(field), f"manual selection inventory {field}")
    revision = _strict_nonnegative_int(
        payload.get("analysis_domain_revision"),
        "manual selection inventory analysis_domain_revision",
    )
    maximum = _strict_nonnegative_int(payload.get("maximum"), "manual selection inventory maximum")
    observed = _strict_nonnegative_int(
        payload.get("observed_count"), "manual selection inventory observed_count"
    )
    if revision < 1 or maximum < 1 or observed <= maximum:
        raise ValueError("manual selection inventory count or revision binding is invalid")
    items = payload.get("items")
    if isinstance(items, (str, bytes)) or not isinstance(items, Sequence):
        raise ValueError("manual selection inventory items must be an array")
    parsed_items: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, Mapping) or set(item) != {
            "content_key",
            "roi_id",
            "parent_content_key",
            "geometry_sha256",
        }:
            raise ValueError("manual selection inventory row fields are invalid")
        content_key = _interaction_sha256(item.get("content_key"), "manual selection content key")
        parent_key = _interaction_sha256(
            item.get("parent_content_key"), "manual selection parent content key"
        )
        geometry = _interaction_sha256(
            item.get("geometry_sha256"), "manual selection geometry SHA-256"
        )
        roi_id = item.get("roi_id")
        if not isinstance(roi_id, str) or not roi_id.strip():
            raise ValueError("manual selection inventory roi_id is invalid")
        parsed_items.append(
            {
                "content_key": content_key,
                "roi_id": roi_id,
                "parent_content_key": parent_key,
                "geometry_sha256": geometry,
            }
        )
    keys = tuple(item["content_key"] for item in parsed_items)
    roi_ids = tuple(item["roi_id"] for item in parsed_items)
    inventory_digest = hashlib.sha256(
        json.dumps(
            {
                "items": [
                    {
                        "content_key": item["content_key"],
                        "geometry_sha256": item["geometry_sha256"],
                        "parent_content_key": item["parent_content_key"],
                        "structure_id": payload["structure_id"],
                    }
                    for item in parsed_items
                ],
                "schema_version": "object-inventory/v1",
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()
    if (
        len(parsed_items) != observed
        or record_count != observed
        or len(set(keys)) != len(keys)
        or len(set(roi_ids)) != len(roi_ids)
        or keys != tuple(sorted(keys))
        or inventory_digest != payload.get("inventory_sha256")
    ):
        raise ValueError("manual selection inventory cardinality or canonical order is invalid")
    return {**payload, "items": parsed_items}


def _strict_json_object(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    parsed: dict[str, object] = {}
    for key, value in pairs:
        if key in parsed:
            raise ValueError(f"duplicate JSON key: {key}")
        parsed[key] = value
    return parsed


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _interaction_sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256")
    return value


def _strict_nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _supervised_classifier_response(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ROIAnalysisError("supervised classifier must be a mapping")
    expected = {
        "artifact_path",
        "classifier_id",
        "classifier_sha256",
        "qupath_version",
        "class_ids",
        "feature_schema",
        "training_cell_ids",
        "holdout_cell_ids",
        "validation_domain_id",
    }
    if set(value) != expected:
        raise ROIAnalysisError("supervised classifier fields are invalid")
    artifact = _accepted_artifact(
        {
            "artifact_path": value["artifact_path"],
            "artifact_sha256": value["classifier_sha256"],
        },
        field="supervised classifier",
    )
    strings: dict[str, str] = {}
    for field in ("classifier_id", "qupath_version", "validation_domain_id"):
        raw = value[field]
        if not isinstance(raw, str) or not raw.strip():
            raise ROIAnalysisError(f"supervised classifier {field} must be non-empty text")
        strings[field] = raw.strip()
    class_ids = list(_string_sequence(value["class_ids"], "supervised classifier class IDs"))
    training = list(_string_sequence(value["training_cell_ids"], "training cell IDs"))
    holdout = list(_string_sequence(value["holdout_cell_ids"], "holdout cell IDs"))
    if not class_ids or not training or not holdout:
        raise ROIAnalysisError(
            "supervised classifier requires classes and disjoint training/holdout cells"
        )
    if set(training).intersection(holdout):
        raise ROIAnalysisError("supervised training and holdout cell IDs must be disjoint")
    raw_schema = value["feature_schema"]
    if isinstance(raw_schema, (str, bytes)) or not isinstance(raw_schema, Sequence):
        raise ROIAnalysisError("supervised classifier feature_schema must be a sequence")
    schema: list[dict[str, str]] = []
    names: set[str] = set()
    for raw in raw_schema:
        if not isinstance(raw, Mapping) or set(raw) != {"measurement_name", "semantic_role"}:
            raise ROIAnalysisError("supervised classifier feature_schema is invalid")
        measurement = raw["measurement_name"]
        role = raw["semantic_role"]
        if (
            not isinstance(measurement, str)
            or not measurement.strip()
            or not isinstance(role, str)
            or not role.strip()
            or measurement in names
        ):
            raise ROIAnalysisError("supervised classifier feature_schema is invalid")
        names.add(measurement)
        schema.append({"measurement_name": measurement, "semantic_role": role})
    if not schema:
        raise ROIAnalysisError("supervised classifier feature_schema cannot be empty")
    return {
        "artifact_path": artifact["artifact_path"],
        "classifier_sha256": artifact["artifact_sha256"],
        **strings,
        "class_ids": class_ids,
        "feature_schema": schema,
        "training_cell_ids": training,
        "holdout_cell_ids": holdout,
    }


def _require_fields(value: Mapping[str, object], expected: set[str]) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        details = []
        if missing:
            details.append(f"missing fields: {missing}")
        if extra:
            details.append(f"unexpected response fields: {extra}")
        raise ValueError("; ".join(details) or "interaction response fields are invalid")




def _tissue_setup_sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ROIAnalysisError(f"{field} must be a lowercase SHA-256")
    return value


def _tissue_setup_byte_count(value: object, field: str) -> int:
    from mif_pipeline.tissue_detection.setup import MAX_TISSUE_CLASSIFIER_BYTES

    if type(value) is not int or value <= 0 or value > MAX_TISSUE_CLASSIFIER_BYTES:
        raise ROIAnalysisError(f"{field} must be a positive integer at most 64 MiB")
    return value


def _tissue_setup_local_path(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\0" in value
        or not Path(value).is_absolute()
        or os.path.normpath(value) != value
    ):
        raise ROIAnalysisError(f"{field} must be an absolute normalized local path")
    return value


def _tissue_detector_setup_response(
    result: StageResult,
    context: Mapping[str, object],
    action: str,
    response: Mapping[str, object],
) -> dict[str, object]:
    _require_fields(
        context,
        {
            "schema_version",
            "checkpoint_id",
            "available_classifiers",
            "channels",
            "native_pyramid_inventory",
            "project_path",
        },
    )
    if context["schema_version"] != "tissue-detector-setup-interaction/v1":
        raise ROIAnalysisError("tissue detector setup interaction schema is invalid")
    if context["checkpoint_id"] != result.checkpoint_id:
        raise ROIAnalysisError("tissue detector setup checkpoint identity is invalid")
    _tissue_setup_local_path(context["project_path"], "project_path")
    inventory = context["native_pyramid_inventory"]
    if (
        isinstance(inventory, (str, bytes))
        or not isinstance(inventory, Sequence)
        or not inventory
        or any(not isinstance(row, Mapping) for row in inventory)
    ):
        raise ROIAnalysisError("tissue detector setup requires a native pyramid inventory")
    raw_channels = context["channels"]
    if (
        isinstance(raw_channels, (str, bytes))
        or not isinstance(raw_channels, Sequence)
        or not raw_channels
    ):
        raise ROIAnalysisError("tissue detector setup requires channels")
    channels: list[ChannelSpec] = []
    for index, raw in enumerate(raw_channels):
        if not isinstance(raw, Mapping):
            raise ROIAnalysisError("tissue detector setup channel must be a mapping")
        _require_fields(raw, {"channel_id", "name", "component", "fluorophore", "biomarker"})
        channels.append(ChannelSpec.from_mapping(raw, field=f"channels[{index}]"))
    channel_ids = {channel.channel_id for channel in channels}
    if len(channel_ids) != len(channels):
        raise ROIAnalysisError("tissue detector setup channel IDs must be unique")
    raw_resources = context["available_classifiers"]
    if (
        isinstance(raw_resources, (str, bytes))
        or not isinstance(raw_resources, Sequence)
        or len(raw_resources) > 256
    ):
        raise ROIAnalysisError("tissue detector setup resource inventory is invalid")
    offered: dict[str, str] = {}
    for raw in raw_resources:
        if not isinstance(raw, Mapping):
            raise ROIAnalysisError("tissue detector setup resource must be a mapping")
        _require_fields(raw, {"classifier_name", "sha256", "byte_count"})
        name = validate_tissue_classifier_name(raw["classifier_name"])
        digest = _tissue_setup_sha256(raw["sha256"], "available classifier sha256")
        _tissue_setup_byte_count(raw["byte_count"], "available classifier byte_count")
        if name in offered:
            raise ROIAnalysisError("tissue detector setup resource names must be unique")
        offered[name] = digest

    if action == InteractionAction.REFRESH_TISSUE_DETECTORS.value:
        _require_fields(response, set())
        return {}
    selecting = action == InteractionAction.SELECT_TISSUE_DETECTOR.value
    _require_fields(response, {"parameters"} if selecting else {"parameters", "source"})
    parameters = TissueDetectionParameters.from_mapping(
        response["parameters"],
        field="tissue_detection",
    )
    if (
        parameters.reference_channel_id is None
        or parameters.reference_channel_selection_source is None
        or parameters.reference_channel_id not in channel_ids
    ):
        raise ROIAnalysisError(
            "tissue detector reference channel must be explicitly selected from offered channels"
        )
    name = validate_tissue_classifier_name(parameters.classifier_name)
    if selecting:
        if name not in offered:
            raise ROIAnalysisError("selected tissue detector was not offered")
        return {
            "parameters": parameters.to_mapping(),
            "source": None,
            "expected_classifier_sha256": offered[name],
        }
    if name in offered:
        raise ROIAnalysisError("imported tissue detector name conflicts with an existing resource")
    source = response["source"]
    if not isinstance(source, Mapping):
        raise ROIAnalysisError("tissue detector import source must be a mapping")
    _require_fields(source, {"path", "sha256", "byte_count"})
    return {
        "parameters": parameters.to_mapping(),
        "source": {
            "path": _tissue_setup_local_path(source["path"], "source.path"),
            "sha256": _tissue_setup_sha256(source["sha256"], "source.sha256"),
            "byte_count": _tissue_setup_byte_count(source["byte_count"], "source.byte_count"),
        },
        "expected_classifier_sha256": None,
    }


def _string_sequence(value: object, field: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{field} must be a sequence")
    parsed: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field} must contain non-empty strings")
        parsed.append(item)
    if len(set(parsed)) != len(parsed):
        raise ValueError(f"{field} must be unique")
    return tuple(parsed)


def _scope_selector(value: Mapping[str, object]) -> tuple[object, object, object, object]:
    raw_parents = value.get("eligible_parent_ids")
    if isinstance(raw_parents, (str, bytes)) or not isinstance(raw_parents, Sequence):
        raise ValueError("moving assistance eligible_parent_ids must be a sequence")
    parents = tuple(raw_parents)
    if any(not isinstance(item, str) or not item.strip() for item in parents) or len(
        set(parents)
    ) != len(parents):
        raise ValueError("moving assistance eligible_parent_ids must be unique non-empty strings")
    return (
        value.get("stage"),
        value.get("analysis_domain_id"),
        value.get("structure_id"),
        parents,
    )


def _json_value(value: object, field: str) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError(f"{field} numbers must be finite")
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError(f"{field} keys must be strings")
        return {key: _json_value(item, field) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_json_value(item, field) for item in value]
    raise ValueError(f"{field} must contain JSON-like values")
