"""Scientific request preparation and result validation for batch-owned stages.

Current-image invocations bind configured scientific inputs to accepted image,
domain and population references. Shared validators check returned segmentation,
measurement, classifier and semantic evidence before the batch owner publishes it."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
import hashlib
import json
import math
import os
import re
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from mif_pipeline.pipeline_core.durable_records import canonical_json_bytes
from mif_pipeline.compartment_analysis.count_qc import PrimaryCountDecision
from mif_pipeline.datasets.roi_inventory import RoiInventoryRef, RoiInventoryError, parse_roi_inventory_ref

from mif_pipeline.orchestration.checkpoints import CheckpointStore
from mif_pipeline.pipeline_core.config import (
    AnalysisConfig,
    ClassifierMode,
    PrimaryClassifierSettings,
    SecondaryStructureSpec,
    require_primary_settings_compatible,
    SegmentationConfig,
)
from mif_pipeline.analysis_domain import (
    AnalysisDomainManifest,
    AnalysisDomainRevision,
    DomainGeometryEvidence,
    DomainOverlapEvidence,
    validate_domain_manifest,
)
from mif_pipeline.compartment_analysis.classifiers import (
    ClassifierDefinition,
    ClassifierCompatibilityError,
    _validate_primary_output_inventory,
)
from mif_pipeline.pipeline_core.domain import (
    AnalysisDomainKind,
    AnalysisDomainRef,
    analysis_domain_id,
)
from mif_pipeline.pipeline_core.errors import ROIAnalysisError, validate_safe_path_segment
from mif_pipeline.pipeline_core.interactions import resolve_resume_envelope
from mif_pipeline.pipeline_core.stage_result import (
    InteractionReason,
    StageResult,
    StageStatus,
)
from mif_pipeline.qupath_runtime.runner import (
    _is_valid_physical_calibration_evidence,
)
from mif_pipeline.cell_analysis.measurement_plan import (
    FeatureCatalog,
    FeatureKind,
    MeasurementPlan,
)
from mif_pipeline.cell_analysis.measurement_setup import configured_measurement_feature_requests
from mif_pipeline.cell_analysis.measurement_rows import (
    MarkerIntensityRowsDescriptor,
    MeasurementRowStreamExpectation,
    ValidatedMeasurementRowStream,
    validate_measurement_row_stream,
)

PHENOTYPING_STAGES = (
    "measurements",
    "phenotyping",
    "clustering",
    "semantic_validation",
    "phenotype_handoff",
)
WORKFLOW_STAGES = (
    "preflight",
    "project",
    "tissue_resolution",
    "tissue_channel",
    "tissue_candidate",
    "tissue_acceptance",
    "registration",
    "analysis_domain_selection",
    "primary_classifier",
    "secondary_classifier",
    "segmentation",
    *PHENOTYPING_STAGES,
    "export",
)

_SECTION_STAGES = frozenset(
    {
        "primary_classifier",
        "secondary_classifier",
        "segmentation",
        "measurements",
        "phenotyping",
        "clustering",
        "semantic_validation",
        "cell_labels_import",
        "supervised_deployment",
    }
)

CURRENT_IMAGE_WORKFLOW_STAGES = (
    "tissue_candidate",
    "tissue_acceptance",
    "analysis_domain_selection",
    "primary_classifier",
    "secondary_classifier",
    "segmentation",
    "measurements",
    "cell_labels_import",
    "supervised_deployment",
)

_SCIENTIFIC_VALIDATION_JSON_MAX_DEPTH = 32
_SCIENTIFIC_VALIDATION_JSON_MAX_ITEMS = 100_000

_CURRENT_IMAGE_RUNTIME_AUTHORITY_KEYS = frozenset(
    {
        "project",
        "_project",
        "project_file",
        "project_path",
        "exchange_path",
        "exchange_paths",
        "runner",
        "store",
        "state_store",
        "checkpoint_store",
        "lease",
        "workspace_lease",
        "project_lease",
        "committer",
        "terminal_committer",
    }
)


# ==============================================================================
#                    Scientific requests and result validation
# ==============================================================================


def _nested_current_image_runtime_authority_keys(
    value: object,
) -> frozenset[str]:
    """Find runtime-authority field names anywhere in a JSON-like value."""

    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in _CURRENT_IMAGE_RUNTIME_AUTHORITY_KEYS:
                found.add(key)
            found.update(_nested_current_image_runtime_authority_keys(item))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            found.update(_nested_current_image_runtime_authority_keys(item))
    return frozenset(found)


class SectionBoundaryError(ROIAnalysisError):
    """Raised when a scoped service emits records for another tissue section."""


@dataclass(frozen=True, slots=True)
class StageInvocation:
    """One dependency-injected unit of work in a single biological domain."""

    stage: str
    config: AnalysisConfig
    run_id: str
    state: Mapping[str, Any]
    checkpoint_store: CheckpointStore
    section_id: str | None = None
    analysis_domain_id: str | None = None
    analysis_domain_revision: int | None = None
    analysis_domain_revision_sha256: str | None = None
    analysis_domain_path_object_id: str | None = None
    structure_id: str | None = None
    eligible_parent_id: str | None = None
    eligible_parent_ids: tuple[str, ...] = ()
    action: str | None = None
    action_context: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        """Validate the StageInvocation fields when the record is constructed.

        The checks cover ``stage``, ``analysis_domain_id``, ``analysis_domain_revision``,
        ``analysis_domain_revision_sha256``, ``analysis_domain_path_object_id``,
        ``eligible_parent_ids``. Invalid field types, values or combinations raise the
        declared validation error before this instance can be used by its caller."""

        if self.stage not in WORKFLOW_STAGES:
            raise ROIAnalysisError(f"unknown workflow stage: {self.stage!r}")
        if self.stage in _SECTION_STAGES and self.section_id is None:
            raise ROIAnalysisError(f"{self.stage} requires one section_id")
        if self.stage not in _SECTION_STAGES and self.section_id is not None:
            raise ROIAnalysisError(f"{self.stage} cannot be section scoped")
        domain_fields = (
            self.analysis_domain_id,
            self.analysis_domain_revision,
            self.analysis_domain_revision_sha256,
            self.analysis_domain_path_object_id,
        )
        if self.stage in {
            "primary_classifier",
            "secondary_classifier",
            "semantic_validation",
        }:
            if any(value is None for value in domain_fields):
                raise ROIAnalysisError(
                    f"{self.stage} requires exact analysis-domain revision identity"
                )
            if (
                not isinstance(self.analysis_domain_id, str)
                or len(self.analysis_domain_id) != 64
                or any(character not in "0123456789abcdef" for character in self.analysis_domain_id)
                or isinstance(self.analysis_domain_revision, bool)
                or not isinstance(self.analysis_domain_revision, int)
                or self.analysis_domain_revision < 1
                or not isinstance(self.analysis_domain_revision_sha256, str)
                or len(self.analysis_domain_revision_sha256) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in self.analysis_domain_revision_sha256
                )
                or not isinstance(self.analysis_domain_path_object_id, str)
                or not self.analysis_domain_path_object_id.strip()
            ):
                raise ROIAnalysisError(f"{self.stage} analysis-domain revision identity is invalid")
        elif any(value is not None for value in domain_fields):
            raise ROIAnalysisError("only domain-scoped invocations accept analysis-domain identity")
        parents = tuple(self.eligible_parent_ids)
        if any(not isinstance(item, str) or not item.strip() for item in parents):
            raise ROIAnalysisError("eligible parent IDs must be non-empty")
        if len(set(parents)) != len(parents):
            raise ROIAnalysisError("eligible parent IDs must be unique")
        object.__setattr__(self, "eligible_parent_ids", parents)
        if self.stage == "secondary_classifier":
            if self.structure_id is None or not parents or self.eligible_parent_id is not None:
                raise ROIAnalysisError(
                    "secondary_classifier requires one structure and its complete eligible-parent set"
                )
        elif self.structure_id is not None or self.eligible_parent_id is not None or parents:
            raise ROIAnalysisError(
                "only secondary_classifier accepts structure and eligible parent IDs"
            )

    def scope_mapping(self) -> dict[str, object]:
        return {
            "section_id": self.section_id,
            "analysis_domain_id": self.analysis_domain_id,
            "analysis_domain_revision": self.analysis_domain_revision,
            "analysis_domain_revision_sha256": self.analysis_domain_revision_sha256,
            "analysis_domain_path_object_id": self.analysis_domain_path_object_id,
            "structure_id": self.structure_id,
            "eligible_parent_id": self.eligible_parent_id,
            "eligible_parent_ids": list(self.eligible_parent_ids),
        }


@dataclass(frozen=True, slots=True)
class CurrentImageScientificInvocation:
    """Path-free scientific input shared by native and Batch current-image runs."""

    stage: str
    config: AnalysisConfig
    run_id: str
    project_entry_id: str
    scientific_state: Mapping[str, Any]
    section_id: str | None = None
    analysis_domain_id: str | None = None
    analysis_domain_revision: int | None = None
    analysis_domain_revision_sha256: str | None = None
    analysis_domain_path_object_id: str | None = None
    structure_id: str | None = None
    eligible_parent_id: str | None = None
    eligible_parent_ids: tuple[str, ...] = ()
    action: str | None = None
    action_context: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        """Validate the CurrentImageScientificInvocation fields when the record is constructed.

        The checks cover ``stage``, ``scientific_state``, ``action_context``,
        ``eligible_parent_ids``, ``config``, ``analysis_domain_id``. Invalid field types,
        values or combinations raise the declared validation error before this instance can
        be used by its caller."""

        if self.stage not in (*CURRENT_IMAGE_WORKFLOW_STAGES, "phenotyping"):
            raise ROIAnalysisError(f"unsupported current-image scientific stage: {self.stage!r}")
        if type(self.config) is not AnalysisConfig:
            raise ROIAnalysisError("current-image scientific invocation requires AnalysisConfig")
        for name in ("run_id", "project_entry_id"):
            value = getattr(self, name)
            if type(value) is not str or not value or value != value.strip():
                raise ROIAnalysisError(f"current-image scientific {name} must be canonical text")
        if not isinstance(self.scientific_state, Mapping) or any(
            type(key) is not str for key in self.scientific_state
        ):
            raise ROIAnalysisError("current-image scientific state must be a string-keyed mapping")
        state = _json_value(self.scientific_state)
        context = _json_value(self.action_context)
        if not isinstance(state, Mapping) or not isinstance(context, Mapping):
            raise ROIAnalysisError(
                "current-image scientific state and action context must be mappings"
            )
        leaked = sorted(
            _nested_current_image_runtime_authority_keys(state)
            | _nested_current_image_runtime_authority_keys(context)
        )
        if leaked:
            raise ROIAnalysisError(
                "current-image scientific state or action context contains runtime "
                "authority: " + ", ".join(leaked)
            )
        object.__setattr__(self, "scientific_state", MappingProxyType(dict(state)))
        object.__setattr__(self, "action_context", MappingProxyType(dict(context)))
        parents = tuple(self.eligible_parent_ids)
        if any(type(item) is not str or not item or item != item.strip() for item in parents):
            raise ROIAnalysisError("eligible parent IDs must be non-empty canonical text")
        if len(set(parents)) != len(parents):
            raise ROIAnalysisError("eligible parent IDs must be unique")
        object.__setattr__(self, "eligible_parent_ids", parents)
        if self.stage in {"primary_classifier", "secondary_classifier"}:
            domain_fields = (
                self.analysis_domain_id,
                self.analysis_domain_revision,
                self.analysis_domain_revision_sha256,
                self.analysis_domain_path_object_id,
            )
            if any(value is None for value in domain_fields):
                raise ROIAnalysisError(
                    f"{self.stage} requires exact analysis-domain revision identity"
                )
            if (
                type(self.analysis_domain_id) is not str
                or len(self.analysis_domain_id) != 64
                or any(character not in "0123456789abcdef" for character in self.analysis_domain_id)
                or isinstance(self.analysis_domain_revision, bool)
                or not isinstance(self.analysis_domain_revision, int)
                or self.analysis_domain_revision < 1
                or type(self.analysis_domain_revision_sha256) is not str
                or len(self.analysis_domain_revision_sha256) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in self.analysis_domain_revision_sha256
                )
                or type(self.analysis_domain_path_object_id) is not str
                or not self.analysis_domain_path_object_id
                or self.analysis_domain_path_object_id
                != self.analysis_domain_path_object_id.strip()
            ):
                raise ROIAnalysisError(f"{self.stage} analysis-domain revision identity is invalid")
        elif any(
            value is not None
            for value in (
                self.analysis_domain_id,
                self.analysis_domain_revision,
                self.analysis_domain_revision_sha256,
                self.analysis_domain_path_object_id,
            )
        ):
            raise ROIAnalysisError("only classifier current-image stages accept domain identity")
        if self.stage in {
            "primary_classifier",
            "secondary_classifier",
            "segmentation",
            "measurements",
            "phenotyping",
            "cell_labels_import",
            "supervised_deployment",
        }:
            if (
                type(self.section_id) is not str
                or not self.section_id
                or self.section_id != self.section_id.strip()
            ):
                raise ROIAnalysisError(f"{self.stage} requires one section_id")
        elif self.section_id is not None:
            raise ROIAnalysisError(f"{self.stage} cannot be section scoped")
        if self.stage == "secondary_classifier":
            if (
                type(self.structure_id) is not str
                or not self.structure_id
                or self.structure_id != self.structure_id.strip()
                or not parents
                or self.eligible_parent_id is not None
            ):
                raise ROIAnalysisError(
                    "secondary_classifier requires one structure and its complete eligible-parent set"
                )
        elif self.structure_id is not None or self.eligible_parent_id is not None or parents:
            raise ROIAnalysisError(
                "only secondary_classifier accepts structure and eligible parent IDs"
            )

    @property
    def state(self) -> Mapping[str, Any]:
        """Compatibility view containing only non-path project-entry identity."""

        return MappingProxyType(
            {
                **self.scientific_state,
                "project": MappingProxyType({"project_entry_id": self.project_entry_id}),
            }
        )

    def scope_mapping(self) -> dict[str, object]:
        return {
            "section_id": self.section_id,
            "analysis_domain_id": self.analysis_domain_id,
            "analysis_domain_revision": self.analysis_domain_revision,
            "analysis_domain_revision_sha256": self.analysis_domain_revision_sha256,
            "analysis_domain_path_object_id": self.analysis_domain_path_object_id,
            "structure_id": self.structure_id,
            "eligible_parent_id": self.eligible_parent_id,
            "eligible_parent_ids": list(self.eligible_parent_ids),
        }


def _freeze_segmentation_validation_json(
    value: object,
    *,
    _depth: int = 0,
    _remaining: list[int] | None = None,
    _active: set[int] | None = None,
) -> Any:
    """Retain only passive, recursively immutable JSON scientific facts."""

    if _depth > _SCIENTIFIC_VALIDATION_JSON_MAX_DEPTH:
        raise ROIAnalysisError("scientific validation context exceeds the JSON depth limit")
    remaining = [_SCIENTIFIC_VALIDATION_JSON_MAX_ITEMS] if _remaining is None else _remaining
    remaining[0] -= 1
    if remaining[0] < 0:
        raise ROIAnalysisError("scientific validation context exceeds the JSON item limit")
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ROIAnalysisError("scientific validation context contains a non-finite number")
        return value
    active = set() if _active is None else _active
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ROIAnalysisError("scientific validation context requires string keys")
        leaked = set(value).intersection(_CURRENT_IMAGE_RUNTIME_AUTHORITY_KEYS)
        if leaked:
            raise ROIAnalysisError(
                "scientific validation context contains runtime authority: "
                + ", ".join(sorted(leaked))
            )
        identity = id(value)
        if identity in active:
            raise ROIAnalysisError("scientific validation context contains a JSON cycle")
        active.add(identity)
        try:
            return MappingProxyType(
                {
                    key: _freeze_segmentation_validation_json(
                        value[key],
                        _depth=_depth + 1,
                        _remaining=remaining,
                        _active=active,
                    )
                    for key in sorted(value)
                }
            )
        finally:
            active.remove(identity)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        identity = id(value)
        if identity in active:
            raise ROIAnalysisError("scientific validation context contains a JSON cycle")
        active.add(identity)
        try:
            return tuple(
                _freeze_segmentation_validation_json(
                    item,
                    _depth=_depth + 1,
                    _remaining=remaining,
                    _active=active,
                )
                for item in value
            )
        finally:
            active.remove(identity)
    raise ROIAnalysisError("scientific validation context contains non-JSON or live runtime data")


@dataclass(frozen=True, slots=True)
class SegmentationResultValidationContext:
    """Path-free scientific authority needed to validate one cell population."""

    segmentation_config: Mapping[str, Any]
    canonical_image_id: str
    canonical_image_sha256: str
    project_entry_id: str
    slide_id: str
    section_id: str
    tissue_generation_id: str
    accepted_tissue_sections: tuple[Mapping[str, Any], ...]
    analysis_domain_selection: Mapping[str, Any]
    domain_set_sha256: str
    selected_annotations_locked: bool
    primary_domain_statuses: Mapping[str, str]
    segmentation_settings_revision_sha256: str | None = None

    def __post_init__(self) -> None:
        """Validate the SegmentationResultValidationContext fields when the record is
        constructed.

        The checks cover ``segmentation_config``, ``analysis_domain_selection``,
        ``accepted_tissue_sections``, ``domain_set_sha256``,
        ``selected_annotations_locked``, ``section_id``. Invalid field types, values or
        combinations raise the declared validation error before this instance can be used by
        its caller."""

        for name in (
            "canonical_image_id",
            "project_entry_id",
            "slide_id",
            "section_id",
            "tissue_generation_id",
        ):
            value = getattr(self, name)
            if type(value) is not str or not value or value != value.strip():
                raise ROIAnalysisError(f"segmentation validation {name} must be canonical text")
        if self.segmentation_settings_revision_sha256 is not None and not _is_sha256(self.segmentation_settings_revision_sha256):
            raise ROIAnalysisError("segmentation revision must have its canonical SHA-256")
        for name in ("canonical_image_sha256", "domain_set_sha256"):
            if not _is_sha256(getattr(self, name)):
                raise ROIAnalysisError(f"segmentation validation {name} must be a SHA-256")
        config = _freeze_segmentation_validation_json(self.segmentation_config)
        domains = _freeze_segmentation_validation_json(self.analysis_domain_selection)
        sections = _freeze_segmentation_validation_json(self.accepted_tissue_sections)
        expected_config_fields = {
            "model_path",
            "model_version",
            "device",
            "tile_size_px",
            "padding_px",
            "workers",
            "scope",
        }
        if isinstance(config, Mapping) and config.get("method") in {"cellpose", "stardist"}:
            from mif_pipeline.pipeline_core.config import SegmentationConfig
            SegmentationConfig.from_mapping(config, field="segmentation validation")
            expected_config_fields |= {"method", "parameters"}
        if not isinstance(config, Mapping) or set(config) != expected_config_fields:
            raise ROIAnalysisError("segmentation validation configuration is not the exact schema")
        if (
            any(
                type(config.get(name)) is not str
                or not config.get(name)
                or config.get(name) != str(config.get(name)).strip()
                for name in ("model_path", "model_version", "device")
            )
            or type(config.get("tile_size_px")) is not int
            or config["tile_size_px"] <= 0
            or type(config.get("padding_px")) is not int
            or config["padding_px"] < 0
            or type(config.get("workers")) is not int
            or config["workers"] <= 0
            or config.get("scope")
            not in {
                "accepted_tissue",
                "configured_union",
            }
        ):
            raise ROIAnalysisError("segmentation validation configuration is invalid")
        if not isinstance(domains, Mapping) or (
            domains.get("domain_set_sha256") != self.domain_set_sha256
        ):
            raise ROIAnalysisError("segmentation validation domain-set commitment differs")
        if (
            self.selected_annotations_locked is not True
            or domains.get("selected_annotations_locked") is not True
        ):
            raise ROIAnalysisError(
                "segmentation validation requires the authenticated selected-annotations lock"
            )
        if type(sections) is not tuple or not sections:
            raise ROIAnalysisError("segmentation validation requires accepted tissue sections")
        expected_section_fields = {
            "section_id",
            "tissue_roi_id",
            "tissue_path_object_id",
            "tissue_generation_id",
        }
        section_ids: list[str] = []
        for section in sections:
            if not isinstance(section, Mapping) or set(section) != expected_section_fields:
                raise ROIAnalysisError("segmentation validation tissue identity is not closed")
            if (
                any(
                    type(section.get(name)) is not str
                    or not section.get(name)
                    or section.get(name) != str(section.get(name)).strip()
                    for name in expected_section_fields
                )
                or section["tissue_generation_id"] != self.tissue_generation_id
            ):
                raise ROIAnalysisError("segmentation validation tissue identity is invalid")
            section_ids.append(section["section_id"])
        if len(set(section_ids)) != len(section_ids) or self.section_id not in section_ids:
            raise ROIAnalysisError("segmentation validation section is outside accepted tissue")
        statuses = self.primary_domain_statuses
        if not isinstance(statuses, Mapping) or not statuses or any(
            not _is_sha256(domain_id) or status not in {
                "active", "excluded_below_minimum", "excluded_by_primary_count_review"
            } for domain_id, status in statuses.items()
        ):
            raise ROIAnalysisError("segmentation primary domain dispositions are invalid")
        active_domains = _active_segmentation_domain_revisions(_mutable_json(domains), self.section_id)
        if set(statuses) != {item["analysis_domain_id"] for item in active_domains}:
            raise ROIAnalysisError("segmentation primary dispositions omit or add domains")
        object.__setattr__(self, "primary_domain_statuses", MappingProxyType(dict(statuses)))
        object.__setattr__(self, "segmentation_config", config)
        object.__setattr__(self, "analysis_domain_selection", domains)
        object.__setattr__(self, "accepted_tissue_sections", sections)

    def _scientific_state(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "preflight": MappingProxyType(
                    {
                        "canonical_image_id": self.canonical_image_id,
                        "canonical_image_sha256": self.canonical_image_sha256,
                    }
                ),
                "project": MappingProxyType({"project_entry_id": self.project_entry_id}),
                "tissue_acceptance": MappingProxyType(
                    {
                        "generation_id": self.tissue_generation_id,
                        "sections": tuple(
                            MappingProxyType(
                                {
                                    "section_id": item["section_id"],
                                    "roi_id": item["tissue_roi_id"],
                                    "path_object_id": item["tissue_path_object_id"],
                                }
                            )
                            for item in self.accepted_tissue_sections
                        ),
                    }
                ),
                # The existing authenticator deliberately compares the
                # producer's array wire shape, so reopen a private mutable copy
                # while keeping the public context recursively immutable.
                "analysis_domain_selection": _mutable_json(self.analysis_domain_selection),
                "domain_set_digest": self.domain_set_sha256,
            }
        )


@dataclass(frozen=True, slots=True)
class MeasurementResultValidationContext:
    """Path-free scientific authority for one section measurement result."""

    section_id: str
    segmentation_population_id: str
    segmentation_cell_count: int
    segmentation_cell_ids_sha256: str
    segmentation_domain_cell_mapping_sha256: str
    analysis_domain_bindings: tuple[Mapping[str, Any], ...]
    canonical_channels: tuple[Mapping[str, Any], ...]
    feature_requests: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        """Validate the MeasurementResultValidationContext fields when the record is
        constructed.

        The checks cover ``analysis_domain_bindings``, ``canonical_channels``,
        ``feature_requests``, ``section_id``, ``segmentation_population_id``,
        ``segmentation_cell_count``. Invalid field types, values or combinations raise the
        declared validation error before this instance can be used by its caller."""

        if (
            type(self.section_id) is not str
            or not self.section_id
            or self.section_id != self.section_id.strip()
        ):
            raise ROIAnalysisError("measurement validation section_id must be canonical text")
        if (
            type(self.segmentation_population_id) is not str
            or not self.segmentation_population_id.startswith("segmentation-population/v2/")
            or self.segmentation_population_id != self.segmentation_population_id.strip()
        ):
            raise ROIAnalysisError("measurement validation segmentation population is invalid")
        if type(self.segmentation_cell_count) is not int or self.segmentation_cell_count < 0:
            raise ROIAnalysisError("measurement validation segmentation cell count is invalid")
        for name in (
            "segmentation_cell_ids_sha256",
            "segmentation_domain_cell_mapping_sha256",
        ):
            if not _is_sha256(getattr(self, name)):
                raise ROIAnalysisError(f"measurement validation {name} must be a SHA-256")
        bindings = _freeze_segmentation_validation_json(self.analysis_domain_bindings)
        channels = _freeze_segmentation_validation_json(self.canonical_channels)
        requests = _freeze_segmentation_validation_json(self.feature_requests)
        if type(bindings) is not tuple or not bindings:
            raise ROIAnalysisError("measurement validation requires analysis-domain bindings")
        binding_fields = {
            "analysis_domain_id",
            "analysis_domain_revision_sha256",
        }
        binding_ids: list[str] = []
        for binding in bindings:
            if not isinstance(binding, Mapping) or set(binding) != binding_fields:
                raise ROIAnalysisError(
                    "measurement validation analysis-domain binding is not closed"
                )
            if not _is_sha256(binding.get("analysis_domain_id")) or not _is_sha256(
                binding.get("analysis_domain_revision_sha256")
            ):
                raise ROIAnalysisError("measurement validation analysis-domain binding is invalid")
            binding_ids.append(binding["analysis_domain_id"])
        if binding_ids != sorted(binding_ids) or len(set(binding_ids)) != len(binding_ids):
            raise ROIAnalysisError(
                "measurement validation analysis-domain bindings must be sorted and unique"
            )
        if type(channels) is not tuple or not channels:
            raise ROIAnalysisError("measurement validation requires canonical channels")
        channel_fields = {
            "channel_id",
            "name",
            "component",
            "fluorophore",
            "biomarker",
        }
        channel_ids: list[str] = []
        for channel in channels:
            if not isinstance(channel, Mapping) or set(channel) != channel_fields:
                raise ROIAnalysisError("measurement validation canonical channel is not closed")
            channel_id = channel.get("channel_id")
            if type(channel_id) is not str or not channel_id or channel_id != channel_id.strip():
                raise ROIAnalysisError("measurement validation canonical channel is invalid")
            if any(
                value is not None
                and (type(value) is not str or not value or value != value.strip())
                for value in (
                    channel.get("name"),
                    channel.get("component"),
                    channel.get("fluorophore"),
                    channel.get("biomarker"),
                )
            ):
                raise ROIAnalysisError("measurement validation canonical channel is invalid")
            channel_ids.append(channel_id)
        if len(set(channel_ids)) != len(channel_ids):
            raise ROIAnalysisError("measurement validation canonical channels must be unique")
        if type(requests) is not tuple or not requests:
            raise ROIAnalysisError("measurement validation requires configured feature requests")
        request_fields = {
            "feature_id",
            "measurement_name",
            "kind",
            "channel_id",
            "biomarker",
            "compartment",
            "units",
            "opt_in",
        }
        request_ids: list[str] = []
        request_names: list[str] = []
        for request in requests:
            if not isinstance(request, Mapping) or set(request) != request_fields:
                raise ROIAnalysisError("measurement validation feature request is not closed")
            for name in ("feature_id", "measurement_name", "kind"):
                value = request.get(name)
                if type(value) is not str or not value or value != value.strip():
                    raise ROIAnalysisError("measurement validation feature request is invalid")
            for name in ("channel_id", "biomarker", "compartment", "units"):
                value = request.get(name)
                if value is not None and (
                    type(value) is not str or not value or value != value.strip()
                ):
                    raise ROIAnalysisError("measurement validation feature request is invalid")
            if type(request.get("opt_in")) is not bool:
                raise ROIAnalysisError("measurement validation feature request opt_in is invalid")
            request_ids.append(request["feature_id"])
            request_names.append(request["measurement_name"])
        if len(set(request_ids)) != len(request_ids) or len(set(request_names)) != len(
            request_names
        ):
            raise ROIAnalysisError("measurement validation feature requests must be unique")
        object.__setattr__(self, "analysis_domain_bindings", bindings)
        object.__setattr__(self, "canonical_channels", channels)
        object.__setattr__(self, "feature_requests", requests)


@dataclass(frozen=True, slots=True, init=False)
class ValidatedMeasurementResult:
    """Validator-produced measurement plan and compact scientific commitments."""

    measurement_plan: MeasurementPlan
    selected_measurement_names: tuple[str, ...]
    cell_count: int
    cell_ids_sha256: str
    domain_cell_mapping_sha256: str
    domain_cell_commitments: tuple[Mapping[str, Any], ...]
    domain_cell_commitments_sha256: str
    measurement_inventory_sha256: str
    observed_measurements_sha256: str
    measurement_keys_sha256: str
    object_outputs_sha256: str
    measurement_rows: ValidatedMeasurementRowStream | None

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("ValidatedMeasurementResult is validator-only")


def _validated_measurement_result(**values: object) -> ValidatedMeasurementResult:
    result = object.__new__(ValidatedMeasurementResult)
    for name in ValidatedMeasurementResult.__dataclass_fields__:
        object.__setattr__(result, name, values[name])
    return result


def validate_measurement_result(
    output: Mapping[str, object],
    context: MeasurementResultValidationContext,
    measurement_rows: ValidatedMeasurementRowStream,
) -> ValidatedMeasurementResult:
    """Validate the exact current QuPath measurement object-output profile.

    The external inventory is checked against canonical channels and configured
    requests before selecting its immutable feature records into a measurement
    plan. Population, rates and selected columns must agree with the authenticated
    native row stream; the returned result retains those scientific commitments.
    """

    if not isinstance(output, Mapping) or type(context) is not MeasurementResultValidationContext:
        raise SectionBoundaryError("measurement result validation inputs are invalid")
    try:
        frozen = _freeze_segmentation_validation_json(output)
    except ROIAnalysisError as exc:
        raise SectionBoundaryError("measurement result is not bounded finite JSON") from exc
    expected_output_fields = {"cell_count", "section_id", "measurement_inventory",
        "observed_measurements", "measurement_keys", "measurement_rows", "coordinate_system"}
    if not isinstance(frozen, Mapping) or set(frozen) != expected_output_fields:
        raise SectionBoundaryError("measurement output keys are not closed")
    if (
        frozen.get("section_id") != context.section_id
        or frozen.get("coordinate_system") != "global_level0"
    ):
        raise SectionBoundaryError("measurement output section or coordinates differ")
    cell_count = frozen.get("cell_count")
    if (
        type(cell_count) is not int
        or cell_count <= 0
        or cell_count != context.segmentation_cell_count
    ):
        raise SectionBoundaryError(
            "measurement output requires the exact nonempty segmentation population"
        )
    binding_by_id = {item["analysis_domain_id"]: item for item in context.analysis_domain_bindings}
    if (
        type(measurement_rows) is not ValidatedMeasurementRowStream
        or frozen.get("measurement_rows") != measurement_rows.descriptor.to_mapping()
        or measurement_rows.cell_count != cell_count
        or measurement_rows.descriptor.row_count != cell_count
        or measurement_rows.header.section_id != context.section_id
        or measurement_rows.header.segmentation_population_id
        != context.segmentation_population_id
        or measurement_rows.header.analysis_domain_ids != tuple(binding_by_id)
    ):
        raise SectionBoundaryError(
            "measurement rows differ from the validated stream authority"
        )
    cell_ids_sha256 = measurement_rows.cell_ids_sha256
    domain_cell_mapping_sha256 = measurement_rows.cell_to_domain_membership_sha256
    domain_cell_commitments = tuple(
        MappingProxyType(item.to_mapping()) for item in measurement_rows.domain_cell_commitments
    )
    if tuple(binding_by_id) != tuple(
        item.analysis_domain_id for item in measurement_rows.domain_cell_commitments
    ):
        raise SectionBoundaryError(
            "measurement rows do not cover the complete domain authority"
        )
    if (
        cell_ids_sha256 != context.segmentation_cell_ids_sha256
        or domain_cell_mapping_sha256 != context.segmentation_domain_cell_mapping_sha256
    ):
        raise SectionBoundaryError("measurement stable-cell commitments differ from segmentation")
    if (
        measurement_rows.domain_cell_commitments_sha256
        != _mapping_sha256(_json_value(domain_cell_commitments))
    ):
        raise SectionBoundaryError(
            "measurement row domain commitments differ from their stream digest"
        )

    observed = frozen.get("observed_measurements")
    if type(observed) is not tuple:
        raise SectionBoundaryError("measurement observed-measurement inventory must be an array")
    observed_fields = {
        "measurement_name",
        "missing_rate",
        "non_finite_rate",
    }
    observed_by_name: dict[str, Mapping[str, object]] = {}
    for item in observed:
        if not isinstance(item, Mapping) or set(item) != observed_fields:
            raise SectionBoundaryError("measurement observed-measurement row is not closed")
        name = item.get("measurement_name")
        if type(name) is not str or not name or name != name.strip():
            raise SectionBoundaryError("measurement observed-measurement name is invalid")
        for rate_name in ("missing_rate", "non_finite_rate"):
            rate = item.get(rate_name)
            if (
                type(rate) not in {int, float}
                or not math.isfinite(float(rate))
                or not 0.0 <= float(rate) <= 1.0
            ):
                raise SectionBoundaryError("measurement observed-measurement rate is invalid")
        if name in observed_by_name:
            raise SectionBoundaryError("measurement observed-measurement names must be unique")
        observed_by_name[name] = item

    inventory = frozen.get("measurement_inventory")
    keys = frozen.get("measurement_keys")
    if type(inventory) is not tuple or type(keys) is not tuple:
        raise SectionBoundaryError("measurement inventory and key inventory must be arrays")
    inventory_fields = {
        "feature_id",
        "measurement_name",
        "kind",
        "channel_id",
        "biomarker",
        "compartment",
        "units",
        "opt_in",
        "producer",
        "producer_version",
        "missing_rate",
        "non_finite_rate",
    }
    if any(not isinstance(item, Mapping) or set(item) != inventory_fields for item in inventory):
        raise SectionBoundaryError("measurement feature inventory row is not closed")
    try:
        catalog = FeatureCatalog.from_inventory(inventory, context.canonical_channels)
    except ROIAnalysisError as exc:
        raise SectionBoundaryError("measurement feature inventory is invalid") from exc
    requests = context.feature_requests
    if len(inventory) != len(requests):
        raise SectionBoundaryError("measurement inventory differs from configured feature requests")
    request_fields = (
        "feature_id",
        "measurement_name",
        "kind",
        "channel_id",
        "biomarker",
        "compartment",
        "units",
        "opt_in",
    )
    for request, item in zip(requests, inventory, strict=True):
        if not isinstance(item, Mapping) or any(
            item.get(field) != request[field] for field in request_fields
        ):
            raise SectionBoundaryError(
                "measurement inventory differs from configured feature requests"
            )
        observed_item = observed_by_name.get(item["measurement_name"])
        if observed_item is None or any(
            item.get(field) != observed_item[field] for field in ("missing_rate", "non_finite_rate")
        ):
            raise SectionBoundaryError(
                "measurement inventory rates differ from observed measurements"
            )
        if float(item["missing_rate"]) != 0.0 or float(item["non_finite_rate"]) != 0.0:
            raise SectionBoundaryError("selected measurements must be complete and finite")
    selected_ids = tuple(item["feature_id"] for item in requests)
    try:
        plan = catalog.build_plan(selected_ids=selected_ids)
    except ROIAnalysisError as exc:
        raise SectionBoundaryError("measurement plan is invalid") from exc
    selected_names = plan.exact_measurement_names
    marker_features = tuple(
        item for item in plan.features if item.kind == FeatureKind.EXPRESSION.value
    )
    if measurement_rows.feature_ids != tuple(
        item.feature_id for item in marker_features
    ) or measurement_rows.measurement_names != tuple(
        item.measurement for item in marker_features
    ):
        raise SectionBoundaryError(
            "measurement row features differ from the validated marker plan"
        )
    if keys != selected_names:
        raise SectionBoundaryError("measurement key inventory differs from the validated plan")
    plain_inventory = _json_value(inventory)
    plain_observed = _json_value(tuple(observed_by_name[name] for name in sorted(observed_by_name)))
    plain_keys = _json_value(keys)
    plain_output = _json_value(frozen)
    plain_commitments = _json_value(domain_cell_commitments)
    return _validated_measurement_result(
        measurement_plan=plan,
        selected_measurement_names=selected_names,
        cell_count=cell_count,
        cell_ids_sha256=cell_ids_sha256,
        domain_cell_mapping_sha256=domain_cell_mapping_sha256,
        domain_cell_commitments=domain_cell_commitments,
        domain_cell_commitments_sha256=_mapping_sha256(plain_commitments),
        measurement_inventory_sha256=_mapping_sha256(plain_inventory),
        observed_measurements_sha256=_mapping_sha256(plain_observed),
        measurement_keys_sha256=_mapping_sha256(plain_keys),
        object_outputs_sha256=_mapping_sha256(plain_output),
        measurement_rows=measurement_rows,
    )


def _validate_tissue_resolution_evidence(
    result: StageResult,
    expected_selection: Mapping[str, object],
) -> dict[str, object] | None:
    """Validate tissue resolution evidence for the current operation.

    Structured inputs are decoded through their typed ``from_mapping`` constructors. The
    remaining comparisons check the relationships required by this operation before the
    caller can use those records."""

    from mif_pipeline.tissue_detection.resolution import (
        NativeLevelReadAttestation,
        TissueResolutionSelection,
    )

    if result.status is StageStatus.COMPLETED:
        evidence = result.outputs
    elif result.status is StageStatus.NEEDS_INTERACTION:
        evidence = result.resume_context
    else:
        return None
    if not isinstance(evidence, Mapping):
        raise ROIAnalysisError("tissue result lacks bound native-resolution evidence")
    selection = TissueResolutionSelection.from_mapping(evidence.get("tissue_resolution_selection"))
    if selection.to_mapping() != dict(expected_selection):
        raise ROIAnalysisError(
            "returned tissue resolution selection does not match the accepted record"
        )
    if "native_level_read_attestation" not in evidence:
        raise ROIAnalysisError("tissue result lacks explicit native read attestation state")
    raw_attestation = evidence["native_level_read_attestation"]
    if raw_attestation is None:
        raise ROIAnalysisError("tissue result lacks the successful native level read attestation")
    attestation = NativeLevelReadAttestation.from_mapping(raw_attestation)
    attestation.validate_selection_binding(selection)
    return attestation.to_mapping()


_TISSUE_OWNER_DERIVED_REQUEST_FIELDS = frozenset(
    {
        "config",
        "detector_settings_hash",
        "detector_validation",
        "generation_history",
        "accepted_generation_id",
        "max_display_number",
    }
)


def _tissue_setup_scientific_settings(invocation):
    """Parse owner-supplied science; authentication stays with the batch codec."""
    from mif_pipeline.pipeline_core.config import TissueDetectionConfig

    setup = invocation.state.get("tissue_setup")
    original = invocation.config.tissue_detection
    if "tissue_setup" not in invocation.state:
        if original is None:
            raise ROIAnalysisError("tissue detection requires accepted tissue setup settings")
        return original, {
            "tissue_setup_result_ref_sha256": None,
            "tissue_setup_result_sha256": None,
        }
    fields = {"tissue_detection", "tissue_setup_result_ref_sha256", "tissue_setup_result_sha256"}
    if not isinstance(setup, Mapping) or set(setup) != fields:
        raise ROIAnalysisError("tissue setup scientific projection requires exact fields")
    tissue = TissueDetectionConfig.from_mapping(
        setup["tissue_detection"], field="tissue_setup.tissue_detection"
    )
    hashes = {}
    for key in fields - {"tissue_detection"}:
        value = setup[key]
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)
        ):
            raise ROIAnalysisError(f"{key} must be a lowercase SHA-256")
        hashes[key] = value
    return tissue, hashes


def _measurement_request_settings(invocation):
    """Project explicit extraction inputs; current authority belongs to the codec."""
    from mif_pipeline.cell_analysis.measurement_setup import validate_measurement_feature_requests
    from mif_pipeline.pipeline_core.config import ChannelSpec

    original = invocation.config
    if "measurement_setup" not in invocation.state:
        if original.channels is None or original.segmentation is None:
            raise ROIAnalysisError("measurements require accepted stage inputs")
        channels = original.channels
        features = validate_measurement_feature_requests(
            configured_measurement_feature_requests(original), channels=channels
        )
        return channels, features, original.segmentation.model_version
    setup = invocation.state["measurement_setup"]
    if not isinstance(setup, Mapping) or set(setup) != {
        "canonical_channels",
        "feature_requests",
        "segmentation_model_version",
        "settings_revision",
    }:
        raise ROIAnalysisError("measurement setup requires exact extraction inputs")
    raw_channels = setup["canonical_channels"]
    fields = {"channel_id", "name", "component", "fluorophore", "biomarker"}
    if (
        not isinstance(raw_channels, (list, tuple))
        or not 1 <= len(raw_channels) <= 1024
        or any(not isinstance(row, Mapping) or set(row) != fields for row in raw_channels)
    ):
        raise ROIAnalysisError("measurement setup requires complete processed channels")
    channels = tuple(
        ChannelSpec.from_mapping(row, field="measurement channel") for row in raw_channels
    )
    if original.channels is not None and channels != original.channels:
        raise ROIAnalysisError("processed measurement channels differ from concrete admission")
    version = setup["segmentation_model_version"]
    if (
        type(version) is not str
        or not version
        or version != version.strip()
    ):
        raise ROIAnalysisError("measurement segmentation version differs from accepted inputs")
    features = validate_measurement_feature_requests(setup["feature_requests"], channels=channels)
    revision = setup["settings_revision"]
    if revision is not None:
        from .batch_measurement_settings import MeasurementSettingsRevision, validate_measurement_settings
        selected = MeasurementSettingsRevision.from_mapping(revision)
        features = validate_measurement_settings(selected.settings, config=original, channels=channels)
        if tuple(dict(item) for item in features) != tuple(dict(item) for item in setup["feature_requests"]):
            raise ROIAnalysisError("measurement request differs from its retained settings revision")
    else:
        configured = configured_measurement_feature_requests(original)
        if configured and features != validate_measurement_feature_requests(configured, channels=channels):
            raise ROIAnalysisError("selected measurements differ from concrete admission")
    return channels, features, version


def _tissue_request_channels(invocation) -> list[dict[str, str | None]]:
    """Project complete processed channels from the authorized invocation state."""
    from mif_pipeline.pipeline_core.config import ChannelSpec

    preflight = invocation.state.get("preflight")
    raw = preflight.get("channels") if isinstance(preflight, Mapping) else None
    if not isinstance(preflight, Mapping) or "channels" not in preflight:
        if invocation.config.channels is None:
            raise ROIAnalysisError("tissue detection requires processed channels")
        return [item.to_mapping() for item in invocation.config.channels]
    fields = {"channel_id", "name", "component", "fluorophore", "biomarker"}
    if (
        not isinstance(raw, (list, tuple))
        or not 1 <= len(raw) <= 1024
        or any(not isinstance(row, Mapping) or set(row) != fields for row in raw)
    ):
        raise ROIAnalysisError("tissue detection requires complete processed channels")
    channels = tuple(ChannelSpec.from_mapping(row, field="tissue processed channel") for row in raw)
    if len({item.channel_id for item in channels}) != len(channels):
        raise ROIAnalysisError("tissue processed channel IDs must be unique")
    if invocation.config.channels is not None and channels != invocation.config.channels:
        raise ROIAnalysisError("tissue processed channels differ from concrete admission")
    return [item.to_mapping() for item in channels]


def _segmentation_request_settings(
    invocation: CurrentImageScientificInvocation | StageInvocation,
) -> tuple[SegmentationConfig, dict[str, Any] | None]:
    """Keep accepted stage choices separate from the immutable admission config."""
    original = invocation.config.segmentation
    if "segmentation_setup" not in invocation.state:
        if original is None:
            raise ROIAnalysisError("segmentation requires accepted stage settings")
        return original, None
    setup = invocation.state["segmentation_setup"]
    if not isinstance(setup, Mapping) or set(setup) not in ({"settings", "model_evidence"},
            {"settings", "model_evidence", "settings_revision"}):
        raise ROIAnalysisError("segmentation setup requires exact settings and model evidence")
    selected = SegmentationConfig.from_mapping(
        setup["settings"], field="segmentation_setup.settings"
    )
    revision = None
    if "settings_revision" in setup:
        from .batch_segmentation_settings import SegmentationSettingsRevision
        revision = SegmentationSettingsRevision.from_mapping(setup["settings_revision"])
        if revision.settings != selected:
            raise ROIAnalysisError("segmentation settings differ from their plan revision")
    if revision is None and original is not None and selected != original:
        raise ROIAnalysisError("selected segmentation settings differ from concrete admission")
    evidence = setup["model_evidence"]
    if evidence is None:
        primaries = _active_segmentation_primary_outputs(invocation.state, invocation.section_id)
        if primaries and all(item["domain_status"] != "active" for item in primaries):
            return selected, None
    if (
        not isinstance(evidence, Mapping)
        or set(evidence) != {"model_path", "model_version", "evidence_sha256", "model_files"}
        or evidence["model_path"] != str(selected.model_path)
        or evidence["model_version"] != selected.model_version
        or not _is_sha256(evidence["evidence_sha256"])
        or not isinstance(evidence["model_files"], (list, tuple))
        or not evidence["model_files"]
    ):
        raise ROIAnalysisError("segmentation model evidence differs from selected settings")
    return selected, _json_value(evidence)


def _validated_tissue_generation_manifest(
    invocation: CurrentImageScientificInvocation,
    value: object,
    *,
    expected_selection: Mapping[str, object],
    expected_read_attestation: Mapping[str, object] | None,
    target_generation_id: str | None,
) -> tuple[dict[str, object], tuple[object, ...]]:
    """Validate one exact tissue manifest before it becomes continuation input."""

    from mif_pipeline.tissue_detection.generations import (
        TissueSectionGeneration,
        validate_generation_manifest,
    )

    required = {
        "schema_version",
        "invocation_id",
        "run_id",
        "stage",
        "slide_id",
        "generation_ids",
        "generations",
        "revision_events",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise ROIAnalysisError("tissue generation manifest fields are invalid")
    if (
        value.get("schema_version") != "roi-generation-manifest/v1"
        or value.get("run_id") != invocation.run_id
        or value.get("stage") != "tissue_detection"
        or value.get("slide_id") != invocation.config.slide_id
        or type(value.get("invocation_id")) is not str
        or not value["invocation_id"]
        or value["invocation_id"] != value["invocation_id"].strip()
    ):
        raise ROIAnalysisError("tissue generation manifest identity differs")
    raw_ids = value.get("generation_ids")
    raw_generations = value.get("generations")
    revision_events = value.get("revision_events")
    if (
        isinstance(raw_ids, (str, bytes))
        or not isinstance(raw_ids, Sequence)
        or isinstance(raw_generations, (str, bytes))
        or not isinstance(raw_generations, Sequence)
        or isinstance(revision_events, (str, bytes))
        or not isinstance(revision_events, Sequence)
    ):
        raise ROIAnalysisError("tissue generation manifest inventory is invalid")
    try:
        generations = validate_generation_manifest(raw_generations)
    except (KeyError, TypeError, ValueError, ROIAnalysisError) as exc:
        raise ROIAnalysisError("tissue generation manifest science is invalid") from exc
    generation_ids = tuple(item.generation_id for item in generations)
    if tuple(raw_ids) != generation_ids:
        raise ROIAnalysisError("tissue generation manifest ID inventory differs")
    canonical_image_id, canonical_image_sha256 = _canonical_identity(invocation.state)
    detector, _setup_hashes = _tissue_setup_scientific_settings(invocation)
    expected_detector_context = (
        invocation.config.slide_id,
        canonical_image_id,
        canonical_image_sha256,
        detector.detector_id,
        detector.detector_version,
        detector.settings_hash,
        detector.reference_channel_id,
        detector.reference_channel_selection_source,
        detector.stage_version,
    )
    expected_selection_value = dict(expected_selection)
    expected_read_attestation_value = None if expected_read_attestation is None else dict(expected_read_attestation)
    for generation in generations:
        assert isinstance(generation, TissueSectionGeneration)
        actual_detector_context = (
            generation.slide_id,
            generation.canonical_image_id,
            generation.canonical_image_sha256,
            generation.detector_id,
            generation.detector_version,
            generation.settings_hash,
            generation.reference_channel_id,
            generation.reference_channel_selection_source,
            generation.stage_version,
        )
        if actual_detector_context[:3] != expected_detector_context[:3]:
            raise ROIAnalysisError("tissue generation manifest image authority differs")
        if generation.generation_id != target_generation_id:
            # Retained generations keep the detector that originally produced them.
            # Their exact history comes from the authenticated predecessor owner.
            continue
        if actual_detector_context[3:] != expected_detector_context[3:]:
            raise ROIAnalysisError("tissue generation manifest detector authority differs")
        if generation.tissue_resolution_selection.to_mapping() != (expected_selection_value):
            raise ROIAnalysisError("tissue generation manifest resolution authority differs")
        if expected_read_attestation_value is not None and generation.native_level_read_attestation.to_mapping() != (
            expected_read_attestation_value
        ):
            raise ROIAnalysisError(
                "tissue generation manifest native read attestation authority differs"
            )
    plain = _json_value(value)
    assert isinstance(plain, Mapping)
    return dict(plain), generations


def _tissue_continuation_request_fields(
    invocation: CurrentImageScientificInvocation,
    *,
    expected_selection: Mapping[str, object],
    expected_read_attestation: Mapping[str, object] | None,
) -> dict[str, object]:
    """Derive tissue history/head/display facts only from validated manifests."""

    from mif_pipeline.tissue_detection.generations import GenerationStatus

    raw_substitutions = set(_TISSUE_OWNER_DERIVED_REQUEST_FIELDS.intersection(invocation.state))
    service_context = resolve_resume_envelope(invocation.action_context).service_context
    raw_substitutions.update(_TISSUE_OWNER_DERIVED_REQUEST_FIELDS.intersection(service_context))
    if raw_substitutions:
        raise ROIAnalysisError(
            "tissue request owner rejects raw authority substitutions: "
            + ", ".join(sorted(raw_substitutions))
        )

    state_manifest: object = None
    candidate_state = invocation.state.get("tissue_candidate")
    if isinstance(candidate_state, Mapping):
        state_manifest = candidate_state.get("generation_manifest")
    resume_manifest = service_context.get("generation_manifest")
    if (
        state_manifest is not None
        and resume_manifest is not None
        and state_manifest != resume_manifest
    ):
        raise ROIAnalysisError("tissue continuation manifest authority is ambiguous")
    raw_manifest = resume_manifest if resume_manifest is not None else state_manifest
    if raw_manifest is None:
        if invocation.stage == "tissue_acceptance" or invocation.action is not None:
            raise ROIAnalysisError(
                "tissue continuation requires authenticated generation manifest authority"
            )
        return {
            "generation_id": None,
            "generation_history": [],
            "accepted_generation_id": None,
            "max_display_number": 0,
        }

    review = invocation.state.get("tissue_review", {})
    new_native_read = isinstance(review, Mapping) and review.get("new_native_read") is True
    if expected_read_attestation is None and not (invocation.action == "repeat" and new_native_read):
        raise ROIAnalysisError("tissue continuation requires native read attestation authority")

    target_candidates = []
    for source in (service_context, candidate_state):
        if isinstance(source, Mapping):
            generation_id = source.get("generation_id")
            if type(generation_id) is str and generation_id.strip():
                target_candidates.append(generation_id)
    if len(set(target_candidates)) > 1:
        raise ROIAnalysisError("tissue continuation generation authority is ambiguous")
    target_generation_id = target_candidates[0] if target_candidates else None
    if invocation.stage == "tissue_acceptance" and target_generation_id is None:
        raw_generations = raw_manifest.get("generations", ()) if isinstance(raw_manifest, Mapping) else ()
        current = tuple(item.get("generation_id") for item in raw_generations
                        if isinstance(item, Mapping) and item.get("status") in {"candidate", "accepted"})
        if len(current) != 1:
            raise ROIAnalysisError("tissue acceptance requires one current generation authority")
        target_generation_id = current[0]

    _manifest, generations = _validated_tissue_generation_manifest(
        invocation,
        raw_manifest,
        expected_selection=expected_selection,
        expected_read_attestation=expected_read_attestation,
        target_generation_id=None if invocation.action == "repeat" else target_generation_id,
    )
    accepted = tuple(
        item.generation_id for item in generations if item.status is GenerationStatus.ACCEPTED
    )
    if len(accepted) > 1:
        raise ROIAnalysisError("tissue continuation has multiple accepted generations")
    max_display_number = max(
        (section.display_number for generation in generations for section in generation.sections),
        default=0,
    )
    generation_ids = {item.generation_id for item in generations}
    if target_generation_id is not None and target_generation_id not in generation_ids:
        raise ROIAnalysisError("tissue continuation generation is absent from history")
    return {
        "generation_id": (None if invocation.action == "repeat" else target_generation_id),
        "generation_history": [item.to_mapping() for item in generations],
        "accepted_generation_id": accepted[0] if accepted else None,
        "max_display_number": max_display_number,
    }


def _primary_settings_for_invocation(
    invocation: StageInvocation,
) -> tuple[PrimaryClassifierSettings, str | None]:
    """Project a closed accepted primary choice without changing admission."""

    admitted = invocation.config
    has_setup = "primary_setup" in invocation.state
    raw_setup = invocation.state.get("primary_setup")
    if not has_setup:
        if admitted.primary_classes is None or admitted.primary_classifier is None:
            raise ROIAnalysisError("primary classifier requires accepted stage settings")
        settings = PrimaryClassifierSettings(admitted.primary_classes, admitted.primary_classifier)
        plan_ref_sha256 = None
    else:
        if not isinstance(raw_setup, Mapping) or set(raw_setup) != {
            "settings",
            "settings_source_ref_sha256",
        }:
            raise ROIAnalysisError(
                "primary setup must contain exact settings and their accepted source ref"
            )
        plan_ref_sha256 = raw_setup["settings_source_ref_sha256"]
        if not _is_sha256(plan_ref_sha256):
            raise ROIAnalysisError("primary setup source ref must be a SHA-256 digest")
        try:
            settings = PrimaryClassifierSettings.from_mapping(raw_setup["settings"])
        except (ROIAnalysisError, TypeError, ValueError) as exc:
            raise ROIAnalysisError("primary setup lacks a complete settings pair") from exc
        if admitted.primary_classes is not None or admitted.primary_classifier is not None:
            if admitted.primary_classes is None or admitted.primary_classifier is None:
                raise ROIAnalysisError("primary setup differs from admitted settings")
            require_primary_settings_compatible(
                PrimaryClassifierSettings(admitted.primary_classes, admitted.primary_classifier),
                settings,
            )
    if admitted.channels is not None:
        settings.validate_channels(admitted.channels)
    return settings, plan_ref_sha256


def secondary_structures_for_invocation(invocation):
    """Decode separately accepted definitions without replacing admitted settings."""
    configured = invocation.config.secondary_structures
    selected = invocation.state.get("secondary_structure_settings")
    if selected is None:
        if configured is None:
            raise ROIAnalysisError("Secondary inference requires its accepted structure definitions")
        return configured
    if configured is not None:
        raise ROIAnalysisError("Configured secondary structures cannot be replaced by a separate selection")
    if (not isinstance(selected, Mapping)
            or set(selected) != {"structures", "selection_ref_sha256"}
            or not _is_sha256(selected["selection_ref_sha256"])
            or not isinstance(selected["structures"], (list, tuple))):
        raise ROIAnalysisError("Secondary definition selection is incomplete")
    structures = tuple(SecondaryStructureSpec.from_mapping(item, field=f"secondary_structures[{i}]")
                       for i, item in enumerate(selected["structures"]))
    if not structures or len({item.structure_id for item in structures}) != len(structures):
        raise ROIAnalysisError("Secondary inference requires distinct selected structures")
    return structures


def prepare_current_image_scientific_request(
    invocation: CurrentImageScientificInvocation,
) -> dict[str, object]:
    """Prepare only the path-free owner body for one current-image stage."""

    if type(invocation) is not CurrentImageScientificInvocation:
        raise ROIAnalysisError(
            "current-image request preparation requires its scientific invocation"
        )
    canonical_identity = _optional_canonical_identity(invocation.state)
    # The immutable current-image digest is injected by the owning native or
    # transport boundary.  Only the scientific OME image ID belongs in
    # the shared owner body.
    canonical_identity.pop("canonical_image_sha256", None)
    request: dict[str, object] = {
        "schema_version": "roi-workflow-invocation/v1",
        "stage": invocation.stage,
        "slide_id": invocation.config.slide_id,
        "run_id": invocation.run_id,
        "generation_ids": list(_generation_ids(invocation.state)),
        "generation_id": _generation_id(invocation.state),
        **canonical_identity,
        **invocation.scope_mapping(),
        "action": invocation.action,
        "action_context": _json_value(invocation.action_context),
        "classifier_reuse_decisions": _json_value(
            invocation.state.get("_classifier_reuse_decisions", {})
        ),
        "confirmed_marker_rules": _json_value(invocation.state.get("_confirmed_marker_rules", {})),
        "registration_context": _json_value(_registration_context(invocation)),
    }
    if invocation.stage == "analysis_domain_selection":
        service_context = resolve_resume_envelope(invocation.action_context).service_context
        candidate = _validated_analysis_domain_context_manifest(
            invocation,
            service_context.get("candidate_manifest"),
            role="candidate_manifest",
            allowed_statuses=frozenset({"candidate"}),
        )
        lineage_predecessor = _validated_analysis_domain_context_manifest(
            invocation,
            service_context.get("lineage_predecessor_candidate"),
            role="lineage_predecessor_candidate",
            allowed_statuses=frozenset({"candidate", "accepted"}),
        )
        retirement_predecessor = _validated_analysis_domain_context_manifest(
            invocation,
            service_context.get("accepted_retirement_predecessor"),
            role="accepted_retirement_predecessor",
            allowed_statuses=frozenset({"accepted"}),
        )
        successor_domain_set_sha256 = None
        if invocation.action == "use_whole_tissue" and retirement_predecessor is not None:
            successor = _whole_tissue_domain_output(invocation)
            if successor is None:
                raise ROIAnalysisError("whole-tissue retirement mutation lacks its successor")
            successor_domain_set_sha256 = successor["domain_set_sha256"]
        request.update(
            selection_policy=invocation.config.analysis_domain.selection_policy,
            saved_closed_acknowledged=(
                invocation.action_context.get("saved_closed_acknowledged") is True
            ),
            accepted_tissue_sections=list(_accepted_tissue_section_records(invocation.state)),
            analysis_domain_assistance=_analysis_domain_assistance(invocation),
            candidate_manifest=_json_value(candidate),
            lineage_predecessor_candidate=_json_value(lineage_predecessor),
            accepted_retirement_predecessor=_json_value(retirement_predecessor),
            candidate_path_object_ids=_candidate_path_object_ids(candidate),
            saved_selection_path=_analysis_domain_selection_path(invocation),
        )
        if successor_domain_set_sha256 is not None:
            request["whole_tissue_successor_domain_set_sha256"] = successor_domain_set_sha256
    if invocation.stage in {"tissue_candidate", "tissue_acceptance"}:
        from mif_pipeline.tissue_detection.resolution import (
            NativeLevelReadAttestation,
            TissueResolutionSelection,
        )

        tissue_settings, setup_hashes = _tissue_setup_scientific_settings(invocation)
        request.update(setup_hashes)

        tissue_channel = invocation.state.get("tissue_channel")
        raw_selection = (
            tissue_channel.get("tissue_resolution_selection")
            if isinstance(tissue_channel, Mapping)
            else None
        )
        if not isinstance(raw_selection, Mapping):
            raise ROIAnalysisError(
                "tissue detection requires an accepted tissue resolution selection"
            )
        resolution_state = invocation.state.get("tissue_resolution")
        native_pyramid_levels = (
            resolution_state.get("native_pyramid_inventory")
            if isinstance(resolution_state, Mapping)
            else None
        )
        if isinstance(native_pyramid_levels, (str, bytes)) or not isinstance(
            native_pyramid_levels, Sequence
        ):
            raise ROIAnalysisError("tissue detection requires the frozen native pyramid inventory")
        selection = TissueResolutionSelection.from_mapping(raw_selection)
        selection.validate_inventory_binding(native_pyramid_levels)
        selected_state = resolution_state.get("tissue_resolution_selection")
        if (
            not isinstance(selected_state, Mapping)
            or dict(selected_state) != selection.to_mapping()
        ):
            raise ROIAnalysisError(
                "tissue resolution selection changed after its authoritative stage"
            )
        request["tissue_resolution_selection"] = selection.to_mapping()
        request["native_pyramid_inventory"] = _json_value(native_pyramid_levels)
        raw_read_attestation: object = None
        if invocation.stage == "tissue_acceptance":
            candidate_state = invocation.state.get("tissue_candidate")
            if isinstance(candidate_state, Mapping):
                raw_read_attestation = candidate_state.get("native_level_read_attestation")
            if raw_read_attestation is None and invocation.action is not None:
                raw_read_attestation = resolve_resume_envelope(
                    invocation.action_context
                ).service_context.get("native_level_read_attestation")
        elif invocation.action is not None:
            raw_read_attestation = resolve_resume_envelope(
                invocation.action_context
            ).service_context.get("native_level_read_attestation")
        else:
            raw_read_attestation = tissue_channel.get("native_level_read_attestation")
        review = invocation.state.get("tissue_review", {})
        review_required = review.get("review_required", False) if isinstance(review, Mapping) else False
        new_native_read = review.get("new_native_read", False) if isinstance(review, Mapping) else False
        if type(review_required) is not bool or type(new_native_read) is not bool or (new_native_read and not review_required):
            raise ROIAnalysisError("tissue review policy differs from its current decision")
        request["review_required"] = review_required
        request["new_native_read"] = new_native_read
        if raw_read_attestation is None:
            if invocation.stage == "tissue_acceptance" or (invocation.action is not None and not (invocation.action == "repeat" and new_native_read)):
                raise ROIAnalysisError(
                    "tissue continuation requires the successful native read attestation"
                )
            request["native_level_read_attestation"] = None
        else:
            read_attestation = NativeLevelReadAttestation.from_mapping(raw_read_attestation)
            read_attestation.validate_selection_binding(selection)
            request["native_level_read_attestation"] = read_attestation.to_mapping()
        request["config"] = _json_value(
            {
                "tissue_detection": tissue_settings.to_mapping(),
                "channels": _tissue_request_channels(invocation),
            }
        )
        request["detector_settings_hash"] = tissue_settings.settings_hash
        # A detector profile becomes admissible only through its future typed
        # authenticated owner. Raw state or resume mappings cannot stand in for it.
        request["detector_validation"] = None
        request.update(
            _tissue_continuation_request_fields(
                invocation,
                expected_selection=selection.to_mapping(),
                expected_read_attestation=(
                    request["native_level_read_attestation"]
                    if isinstance(request["native_level_read_attestation"], Mapping)
                    else None
                ),
            )
        )
    if invocation.stage in {"primary_classifier", "secondary_classifier"}:
        if invocation.stage == "primary_classifier":
            primary_settings, plan_ref_sha256 = _primary_settings_for_invocation(invocation)
            settings_mapping = primary_settings.to_mapping()
            request["primary_settings"] = settings_mapping
            request["primary_settings_sha256"] = hashlib.sha256(
                canonical_json_bytes(settings_mapping)
            ).hexdigest()
            request["primary_settings_source_ref_sha256"] = plan_ref_sha256
        # The accepted classifier record supplies this decision. Count review and
        # secondary semantic review keep their separate actions and object states.
        classifier_review_status = invocation.state.get("classifier_review_status")
        if classifier_review_status not in {"accepted", "rejected", "needs_review"}:
            raise ROIAnalysisError("classifier application requires its recorded review decision")
        request["classifier_review_status"] = classifier_review_status
        if invocation.stage == "primary_classifier":
            request["primary_classifier_output_classes"] = list(
                _primary_output_inventory_for_invocation(invocation)
            )
        domain_output = invocation.state.get("analysis_domain_selection")
        domain_set_sha256 = (
            domain_output.get("domain_set_sha256") if isinstance(domain_output, Mapping) else None
        )
        if not isinstance(domain_set_sha256, str):
            raise ROIAnalysisError("classifier execution requires the accepted domain-set digest")
        request["domain_set_sha256"] = domain_set_sha256
        next(
            item
            for item in _accepted_analysis_domain_scopes(invocation.state)
            if item["analysis_domain_id"] == invocation.analysis_domain_id
        )
        request["analysis_domain_revision_evidence"] = _json_value(
            _accepted_analysis_domain_revision_evidence(
                invocation.state, invocation.analysis_domain_id
            )
        )
        if invocation.stage == "secondary_classifier":
            # The batch owner resolves these commitments from authenticated inputs.
            # No live object list or caller-selected file path is carried here.
            from .batch_classifier_inference_artifacts import parse_roi_parent_sources
            sources = parse_roi_parent_sources(invocation.state.get("secondary_parent_sources"))
            if tuple(item.eligible_parent_id for item in sources) != invocation.eligible_parent_ids:
                raise ROIAnalysisError("secondary parent sources changed configured order")
            predecessor = invocation.state.get("secondary_predecessor_result_ref_sha256")
            status = invocation.state.get("primary_domain_status")
            if not _is_sha256(predecessor) or status not in {
                "active", "excluded_below_minimum", "excluded_by_primary_count_review"
            }:
                raise ROIAnalysisError("secondary predecessor or primary disposition is invalid")
            request["parent_sources"] = [item.to_mapping() for item in sources]
            request["predecessor_result_ref_sha256"] = predecessor
            request["primary_domain_status"] = status
            if invocation.state.get("secondary_structure_settings") is not None:
                secondary_structures_for_invocation(invocation)
                request["secondary_structure_settings"] = _json_value(
                    invocation.state["secondary_structure_settings"])
        classifier_id = (
            _primary_settings_for_invocation(invocation)[0].primary_classifier.classifier_id
            if invocation.stage == "primary_classifier"
            else next(
                item.classifier.classifier_id
                for item in secondary_structures_for_invocation(invocation)
                if item.structure_id == invocation.structure_id
            )
        )
        evidence_records = invocation.state.get("_training_evidence_records", {})
        if isinstance(evidence_records, Mapping) and classifier_id in evidence_records:
            request["live_training_evidence_record"] = _json_value(evidence_records[classifier_id])
        classifier_application = invocation.state.get("classifier_application")
        if classifier_application is not None:
            required_application_fields = {
                "classifier_record_set_ref_sha256",
                "classifier_record_set_sha256",
                "output_id",
                "classifier_record_sha256",
                "classifier_id",
                "classifier_version",
                "classifier_kind",
                "application_request_sha256",
                "target_project_identity_sha256",
                "deployment_binding_set_sha256",
                "target_evidence_set_sha256",
                "project_relative_path",
                "source_model_path",
                "native_classifier_sha256",
                "native_classifier_byte_count",
                "pixel_classifier_definition",
                "pixel_classifier_definition_sha256",
            }
            if (
                not isinstance(classifier_application, Mapping)
                or set(classifier_application) != required_application_fields
            ):
                raise ROIAnalysisError("classifier application projection is not closed")
            for name in (
                "classifier_record_set_ref_sha256",
                "classifier_record_set_sha256",
                "classifier_record_sha256",
                "application_request_sha256",
                "target_project_identity_sha256",
                "deployment_binding_set_sha256",
                "target_evidence_set_sha256",
                "native_classifier_sha256",
                "pixel_classifier_definition_sha256",
            ):
                if not _is_sha256(classifier_application.get(name)):
                    raise ROIAnalysisError(f"classifier application {name} is invalid")
            for name in (
                "output_id",
                "classifier_id",
                "classifier_version",
                "classifier_kind",
            ):
                value = classifier_application.get(name)
                if type(value) is not str or not value or value != value.strip():
                    raise ROIAnalysisError(f"classifier application {name} is invalid")
            if classifier_application["classifier_id"] != classifier_id or classifier_application[
                "classifier_kind"
            ] != (
                "primary_compartment"
                if invocation.stage == "primary_classifier"
                else "secondary_structure"
            ):
                raise ROIAnalysisError("classifier application differs from the configured stage")
            locator = classifier_application.get("project_relative_path")
            if type(locator) is not str:
                raise ROIAnalysisError("classifier application native locator is invalid")
            relative = PurePosixPath(locator)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or len(relative.parts) != 3
                or relative.parts[:2] != ("classifiers", "pixel_classifiers")
            ):
                raise ROIAnalysisError(
                    "classifier application native locator is not project-relative"
                )
            source_model_path = classifier_application.get("source_model_path")
            if source_model_path is not None and (
                type(source_model_path) is not str or not Path(source_model_path).is_absolute()
                or Path(source_model_path).name != f"{classifier_application['native_classifier_sha256']}.json"
                or Path(source_model_path).parent.name != "models"
            ):
                raise ROIAnalysisError("classifier import source is not a bound export model member")
            byte_count = classifier_application.get("native_classifier_byte_count")
            if type(byte_count) is not int or byte_count <= 0:
                raise ROIAnalysisError("classifier application native byte count is invalid")
            try:
                definition = ClassifierDefinition.from_mapping(
                    classifier_application.get("pixel_classifier_definition")
                )
            except (ClassifierCompatibilityError, TypeError, ValueError) as exc:
                raise ROIAnalysisError(
                    "classifier application pixel definition is invalid"
                ) from exc
            if (
                definition.classifier_id != classifier_application["classifier_id"]
                or definition.version != classifier_application["classifier_version"]
            ):
                raise ROIAnalysisError("classifier application pixel definition identity differs")
            request["classifier_application"] = _json_value(classifier_application)
    population_id = _optional_segmentation_population_id(invocation.state, invocation.section_id)
    if population_id is not None:
        request["segmentation_population_id"] = population_id
    population_ids = _segmentation_population_ids(invocation.state)
    if population_ids:
        request["segmentation_population_ids"] = population_ids
    if invocation.stage == "segmentation":
        request["cell_dataset_directory"] = str(
            (Path(invocation.config.output_dir) / "cell-datasets" / "segmentation").resolve()
        )
        domain_output = invocation.state.get("analysis_domain_selection")
        if not isinstance(domain_output, Mapping):
            raise ROIAnalysisError("segmentation requires accepted analysis-domain authority")
        try:
            _authenticate_global_segmentation_domain_manifests(
                domain_output,
                invocation.state,
                slide_id=invocation.config.slide_id,
                project_entry_id=invocation.project_entry_id,
            )
            _active_segmentation_domain_revisions(domain_output, invocation.section_id)
        except ROIAnalysisError as exc:
            raise ROIAnalysisError(
                "segmentation requires complete authenticated Task 6 global and "
                "current-section authority"
            ) from exc
        request["domain_set_sha256"] = domain_output.get("domain_set_sha256")
        request["analysis_domain_selection_status"] = domain_output.get("selection_status")
        request["analysis_domain_manifests"] = _json_value(
            domain_output.get("analysis_domain_manifests")
        )
        selected_annotations_locked = domain_output.get("selected_annotations_locked")
        if selected_annotations_locked is not True:
            raise ROIAnalysisError("segmentation requires the accepted selected-annotations lock")
        request["selected_annotations_locked"] = selected_annotations_locked
        segmentation, model_evidence = _segmentation_request_settings(invocation)
        settings = segmentation.to_mapping()
        setup = invocation.state.get("segmentation_setup")
        if isinstance(setup, Mapping) and "settings_revision" in setup:
            from .batch_segmentation_settings import SegmentationSettingsRevision
            revision = SegmentationSettingsRevision.from_mapping(setup["settings_revision"])
            request["segmentation_settings_revision"] = revision.to_mapping()
        request["segmentation_settings"] = settings
        request["segmentation_settings_sha256"] = hashlib.sha256(
            canonical_json_bytes(settings)
        ).hexdigest()
        request["segmentation_model_evidence"] = model_evidence
        primary_skipped = invocation.state.get("primary_classification_skipped", False)
        if type(primary_skipped) is not bool:
            raise ROIAnalysisError("Segmentation requires its accepted primary classification choice")
        if primary_skipped and segmentation.scope != "accepted_tissue":
            raise ROIAnalysisError("Without primary classification, segment the accepted analysis regions")
        request["primary_classification_skipped"] = primary_skipped
        request["active_primary_outputs"] = [] if primary_skipped else _json_value(
            _active_segmentation_primary_outputs(invocation.state, invocation.section_id)
        )
    if invocation.stage == "measurements":
        channels, features, model_version = _measurement_request_settings(invocation)
        request["feature_requests"] = [dict(row) for row in features]
        request["canonical_channels"] = [channel.to_mapping() for channel in channels]
        request["segmentation_model_version"] = model_version
        population = invocation.state.get("segmentation", {}).get(invocation.section_id, {})
        base_digest = population.get("cell_evidence_sha256")
        if type(base_digest) is not str or re.fullmatch(r"[0-9a-f]{64}", base_digest) is None:
            raise ROIAnalysisError("measurements require the accepted base cell dataset checksum")
        request["segmentation_cell_dataset_sha256"] = base_digest
        request["phenotyping_population_sources"] = _json_value(
            invocation.state.get("phenotyping_population_sources", [])
        )
        if "measurement_reuse" in invocation.state:
            request["measurement_reuse"] = _json_value(invocation.state["measurement_reuse"])
        if "cell_correction" in invocation.state:
            request["cell_correction"] = _json_value(invocation.state["cell_correction"])
    if invocation.stage == "cell_labels_import":
        import_state = invocation.state.get("cluster_label_application")
        if not isinstance(import_state, Mapping):
            raise ROIAnalysisError("cell-label import requires prepared application authority")
        required = {
            "selected_display_view",
            "materialize_display_classes",
            "phenotype_view_operation",
            "phenotype_view_artifact",
            "phenotype_assignment_source",
        }
        if set(import_state) != required:
            raise ROIAnalysisError("cell-label import application authority is not closed")
        request.update(_json_value(import_state))
    if invocation.stage == "phenotyping":
        application_state = invocation.state.get("marker_rule_application")
        required = {
            "assignment_schema_version",
            "phenotype_assignment_source",
            "selected_display_view",
            "phenotype_generation_id",
            "materialize_display_classes",
            "phenotype_view_artifact",
        }
        if (
            not isinstance(application_state, Mapping)
            or set(application_state) != required
            or application_state.get("assignment_schema_version") != "marker-rule-assignments/v2"
            or application_state.get("selected_display_view") != "marker_rules"
        ):
            raise ROIAnalysisError("phenotyping requires closed marker-rule application authority")
        request.update(_json_value(application_state))
    if invocation.stage == "supervised_deployment":
        deployment = invocation.state.get("supervised_deployment")
        required = {
            "deployment_mode",
            "classifier",
            "class_ids",
            "feature_schema",
            "current_validation_domain_id",
            "expected_cell_ids",
            "phenotyping_cell_ids",
            "cell_id_to_analysis_domain_id",
            "segmentation_population_id",
            "prediction_identity",
            "materialize_display_classes",
            "expected_prediction_sha256",
            "stored_prediction",
            "phenotype_view_artifact",
            "phenotype_assignment_source",
        }
        if (
            not isinstance(deployment, Mapping)
            or set(deployment) != required
            or deployment.get("deployment_mode") != "batch_deployment"
            or type(deployment.get("materialize_display_classes")) is not bool
        ):
            raise ROIAnalysisError("supervised deployment requires closed application authority")
        request.update(_json_value(deployment))
    return request


def finalize_current_image_scientific_result(
    invocation: CurrentImageScientificInvocation,
    *,
    request: Mapping[str, object],
    wire_stage_result: StageResult | None,
    generation_manifest: Mapping[str, object] | None,
    primary_count_decision: PrimaryCountDecision | None = None,
) -> StageResult | Mapping[str, Any] | PrimaryClassifierDisposition:
    """Finalize current-image wire values without runner or path authority."""

    if type(invocation) is not CurrentImageScientificInvocation:
        raise ROIAnalysisError(
            "current-image result finalization requires its scientific invocation"
        )
    if not isinstance(request, Mapping):
        raise ROIAnalysisError("current-image result request must be a mapping")
    tissue_manifest_value: dict[str, object] | None = None
    tissue_manifest_generation_ids: set[str] = set()
    if invocation.stage in {"tissue_candidate", "tissue_acceptance"}:
        if wire_stage_result is None:
            raise ROIAnalysisError(
                "tissue detection requires a typed stage result with native evidence"
            )
        expected_selection = request.get("tissue_resolution_selection")
        if not isinstance(expected_selection, Mapping):
            raise ROIAnalysisError("tissue result lacks its expected resolution selection")
        validated_read_attestation = _validate_tissue_resolution_evidence(
            wire_stage_result,
            expected_selection,
        )
        if isinstance(generation_manifest, Mapping):
            if validated_read_attestation is None:
                raise ROIAnalysisError("tissue manifest requires native read attestation authority")
            tissue_manifest_value, generations = _validated_tissue_generation_manifest(
                invocation,
                generation_manifest,
                expected_selection=expected_selection,
                expected_read_attestation=validated_read_attestation,
                target_generation_id=wire_stage_result.generation_id,
            )
            tissue_manifest_generation_ids = {item.generation_id for item in generations}
            if wire_stage_result.generation_id not in tissue_manifest_generation_ids:
                raise ROIAnalysisError("tissue result generation is absent from its manifest")
    if wire_stage_result is not None and wire_stage_result.status is not StageStatus.COMPLETED:
        if (
            invocation.stage in {"tissue_candidate", "tissue_acceptance"}
            and tissue_manifest_value is not None
        ):
            resume_context = dict(wire_stage_result.resume_context or {})
            existing_manifest = resume_context.get("generation_manifest")
            if existing_manifest is not None and existing_manifest != tissue_manifest_value:
                raise ROIAnalysisError("tissue interaction contains conflicting manifest authority")
            resume_context["generation_manifest"] = tissue_manifest_value
            wire_stage_result = replace(
                wire_stage_result,
                resume_context=resume_context,
            )
        return _retarget_result(wire_stage_result, invocation.stage)
    if isinstance(generation_manifest, Mapping):
        manifest_value = (
            tissue_manifest_value
            if tissue_manifest_value is not None
            else _json_value(generation_manifest)
        )
        assert isinstance(manifest_value, Mapping)
        object_outputs = manifest_value.get("object_outputs")
        output = (
            dict(object_outputs) if isinstance(object_outputs, Mapping) else dict(manifest_value)
        )
        if wire_stage_result is not None and wire_stage_result.outputs is not None:
            output.update(_json_value(wire_stage_result.outputs))
        if invocation.stage == "segmentation":
            return output
        measurement_validated = False
        if invocation.stage == "measurements":
            output = dict(_attach_measurement_plan(invocation, output, request))
            measurement_validated = True
        if invocation.stage == "primary_classifier":
            if not isinstance(primary_count_decision, PrimaryCountDecision):
                raise ROIAnalysisError("primary finalization requires its validated table decision")
            return _finalize_primary_count_decision(invocation, primary_count_decision)
        if "generation" in output and isinstance(output["generation"], Mapping):
            generation = output["generation"]
        else:
            generation = manifest_value.get("generation")
            generations = manifest_value.get("generations")
            if generation is None and isinstance(generations, list):
                target_id = (
                    wire_stage_result.generation_id if wire_stage_result is not None else None
                )
                matches = [
                    item
                    for item in generations
                    if isinstance(item, Mapping)
                    and (target_id is None or item.get("generation_id") == target_id)
                ]
                if len(matches) == 1:
                    generation = matches[0]
            if generation is None:
                generation = manifest_value
        if isinstance(generation, Mapping):
            output.setdefault("generation", _json_value(generation))
            output.setdefault("generation_id", generation.get("generation_id"))
            output.setdefault(
                "generation_status",
                generation.get("status", output.get("generation_status")),
            )
            output.setdefault("sections", generation.get("sections"))
        if tissue_manifest_value is not None:
            output["generation_manifest"] = tissue_manifest_value
        generation_ids = manifest_value.get("generation_ids")
        if (
            output.get("generation_id") is None
            and isinstance(generation_ids, list)
            and len(generation_ids) == 1
        ):
            output["generation_id"] = generation_ids[0]
        if measurement_validated:
            return output
        return _attach_measurement_plan(invocation, output, request)
    output = _json_value(wire_stage_result.outputs if wire_stage_result is not None else {})
    assert isinstance(output, Mapping)
    if invocation.stage == "segmentation":
        return output
    return _attach_measurement_plan(invocation, output, request)


def _required_generation_id(state: Mapping[str, Any]) -> str:
    generation_id = _generation_id(state)
    if not isinstance(generation_id, str) or not generation_id.strip():
        raise ROIAnalysisError("accepted tissue generation is required")
    return generation_id


def _retarget_result(result: StageResult, stage: str) -> StageResult:
    if result.stage == stage:
        return result
    mapping = result.to_mapping()
    mapping["stage"] = stage
    return StageResult.from_mapping(mapping)


def _analysis_domain_interaction(
    invocation: StageInvocation,
) -> StageResult | None:
    """Return the next explicit domain-selection interaction, if any."""
    policy = invocation.config.analysis_domain.selection_policy
    if invocation.state.get("analysis_domain_review_required") is True:
        policy = "prompt"
    action = invocation.action
    service = resolve_resume_envelope(invocation.action_context).service_context
    assistance = _analysis_domain_assistance(invocation)
    current_candidate = _validated_analysis_domain_context_manifest(
        invocation,
        service.get("candidate_manifest"),
        role="candidate_manifest",
        allowed_statuses=frozenset({"candidate"}),
    )
    lineage_predecessor = _validated_analysis_domain_context_manifest(
        invocation,
        service.get("lineage_predecessor_candidate"),
        role="lineage_predecessor_candidate",
        allowed_statuses=frozenset({"candidate", "accepted"}),
    )
    retirement_predecessor = _validated_analysis_domain_context_manifest(
        invocation,
        service.get("accepted_retirement_predecessor"),
        role="accepted_retirement_predecessor",
        allowed_statuses=frozenset({"accepted"}),
    )
    if policy == "whole_tissue" or action == "use_whole_tissue":
        return None
    if action in {"review_analysis_rois", "accept_analysis_rois"}:
        if action == "accept_analysis_rois" and current_candidate is None:
            raise ROIAnalysisError(
                "analysis-domain acceptance requires the reviewed candidate manifest"
            )
        return None
    if policy == "prompt" and action is None:
        review_context = {
            "schema_version": "analysis-domain-choice-interaction/v1",
            "analysis_domain_assistance": assistance,
        }
        for role, predecessor in (
            ("lineage_predecessor_candidate", lineage_predecessor),
            ("accepted_retirement_predecessor", retirement_predecessor),
        ):
            if predecessor is not None:
                review_context[role] = _analysis_domain_manifest_reference(invocation, predecessor, role=role)
        return StageResult.needs_interaction(
            stage=invocation.stage,
            slide_id=invocation.config.slide_id,
            generation_id=_generation_id(invocation.state),
            reason=InteractionReason.ANALYSIS_DOMAIN_CHOICE_REQUIRED,
            affected_object_ids=_accepted_sections(invocation.state),
            message=("Choose whether to analyze the whole tissue or select regions in QuPath."),
            allowed_actions=("use_whole_tissue", "select_analysis_rois"),
            resume_context=review_context,
            checkpoint_id="pending-workflow-checkpoint",
        )
    if action not in {
        None,
        "select_analysis_rois",
        "open_qupath",
        "edit_analysis_rois",
    }:
        raise ROIAnalysisError("analysis-domain selection action is invalid")
    if policy not in {"prompt", "selected_rois"}:
        raise ROIAnalysisError("analysis-domain selection policy is invalid")
    if action == "edit_analysis_rois" and current_candidate is None and lineage_predecessor is None:
        raise ROIAnalysisError("analysis-domain editing requires a candidate lineage predecessor")
    predecessor = (
        current_candidate
        if action == "edit_analysis_rois" and current_candidate is not None
        else lineage_predecessor
    )
    context: dict[str, object] = {
        "schema_version": "analysis-domain-rois-interaction/v1",
        "analysis_domain_assistance": assistance,
        "canonical_project_entry_id": _project_entry_id(invocation.state),
        "saved_selection_path": _analysis_domain_selection_path(invocation),
    }
    if isinstance(predecessor, Mapping):
        context["lineage_predecessor_candidate"] = _analysis_domain_manifest_reference(
            invocation, predecessor, role="lineage_predecessor_candidate"
        )
    if isinstance(retirement_predecessor, Mapping):
        context["accepted_retirement_predecessor"] = _analysis_domain_manifest_reference(
            invocation,
            retirement_predecessor,
            role="accepted_retirement_predecessor",
        )
    published_ids = service.get("candidate_path_object_ids")
    if isinstance(published_ids, Sequence) and not isinstance(published_ids, (str, bytes)):
        context["candidate_path_object_ids"] = _json_value(published_ids)
    return StageResult.needs_interaction(
        stage=invocation.stage,
        slide_id=invocation.config.slide_id,
        generation_id=_generation_id(invocation.state),
        reason=InteractionReason.ANALYSIS_DOMAIN_ROIS_REQUIRED,
        affected_object_ids=_accepted_sections(invocation.state),
        message=(
            "Draw or select the regions to analyze in QuPath. Choose Save selected "
            "regions, close QuPath, then review the regions here."
        ),
        allowed_actions=(
            "open_qupath",
            "review_analysis_rois",
            "use_whole_tissue",
        ),
        resume_context=context,
        checkpoint_id="pending-workflow-checkpoint",
    )


def _project_entry_id(state: Mapping[str, Any]) -> str:
    project = state.get("project")
    entry_id = project.get("project_entry_id") if isinstance(project, Mapping) else None
    if not isinstance(entry_id, str) or not entry_id.strip():
        raise ROIAnalysisError("analysis domains require the exact canonical project entry")
    return entry_id


def _analysis_domain_selection_path(
    invocation: StageInvocation | CurrentImageScientificInvocation,
) -> str:
    """Locate this image's selection within the authenticated native project.

    We give native requests a project-relative path; their runtime supplies the
    current project. The review form uses that project's absolute location when
    returning a saved QuPath draft.
    """
    configured = invocation.config.analysis_domain.saved_selection_path
    if configured is not None:
        return str(configured)
    relative = (Path("data") / _project_entry_id(invocation.state) /
                "analysis-domain-pathobjects.v1.json")
    if isinstance(invocation, CurrentImageScientificInvocation):
        return str(relative)
    project = invocation.state.get("project")
    project_file = project.get("project_file") if isinstance(project, Mapping) else None
    if not isinstance(project_file, str) or not project_file.strip():
        raise ROIAnalysisError("analysis domains require the prepared QuPath project path")
    return str(Path(project_file).resolve().parent / relative)


def _accepted_tissue_section_records(
    state: Mapping[str, Any],
) -> tuple[dict[str, str], ...]:
    acceptance = state.get("tissue_acceptance")
    if not isinstance(acceptance, Mapping):
        raise ROIAnalysisError("analysis domains require accepted TissueSections")
    raw_sections = acceptance.get("sections")
    if isinstance(raw_sections, (str, bytes)) or not isinstance(raw_sections, Sequence):
        generation = acceptance.get("generation")
        raw_sections = generation.get("sections") if isinstance(generation, Mapping) else None
    if isinstance(raw_sections, (str, bytes)) or not isinstance(raw_sections, Sequence):
        raise ROIAnalysisError("accepted TissueSection records are missing")
    generation_id = _generation_id(state)
    if generation_id is None:
        raise ROIAnalysisError("accepted TissueSection generation is missing")
    records: list[dict[str, str]] = []
    for raw in raw_sections:
        if not isinstance(raw, Mapping):
            raise ROIAnalysisError("accepted TissueSection record is invalid")
        section_id = raw.get("section_id")
        tissue_roi_id = raw.get("roi_id")
        path_object_id = raw.get("path_object_id")
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (section_id, tissue_roi_id, path_object_id)
        ):
            raise ROIAnalysisError(
                "accepted TissueSection requires section, ROI, and PathObject UUID identity"
            )
        records.append(
            {
                "section_id": section_id,
                "tissue_roi_id": tissue_roi_id,
                "tissue_path_object_id": path_object_id,
                "tissue_generation_id": generation_id,
            }
        )
    if len({item["section_id"] for item in records}) != len(records) or not records:
        raise ROIAnalysisError("accepted TissueSection identities must be unique")
    return tuple(records)


def _analysis_domain_assistance(invocation: StageInvocation) -> dict[str, object]:
    """Describe visual context without transferring analysis-domain coordinates."""
    canonical_entry = _project_entry_id(invocation.state)
    base: dict[str, object] = {
        "schema_version": "analysis-domain-assistance/v1",
        "mode": "unavailable",
        "canonical_project_entry_id": canonical_entry,
        "annotation_project_entry_id": canonical_entry,
        "coordinate_system": "global_level0_pixels",
        "coordinate_transfer_authorized": False,
    }
    output = invocation.state.get("registration")
    if not isinstance(output, Mapping):
        return base
    mode = _effective_registration_mode(invocation)
    if mode == "side_by_side":
        moving_entry = _side_by_side_moving_project_entry_id(invocation, output)
        if moving_entry is not None:
            return {
                **base,
                "mode": "side_by_side",
                "moving_project_entry_id": moving_entry,
            }
        return base
    if mode != "warpy":
        return base
    try:
        available = _available_registration_evidence(output, _accepted_sections(invocation.state))
    except ROIAnalysisError:
        return base
    registration = output.get("registration")
    assert isinstance(registration, Mapping)
    fixed_entry = registration.get("fixed_project_entry_id", canonical_entry)
    if fixed_entry != canonical_entry:
        return base
    moving_entry = registration.get("moving_project_entry_id")
    return {
        **base,
        "mode": "warpy_overlay",
        "registration_id": registration.get("registration_id"),
        "moving_project_entry_id": moving_entry,
        "section_registration_qc": list(available),
    }


def _side_by_side_moving_project_entry_id(
    invocation: StageInvocation,
    output: Mapping[str, Any],
) -> str | None:
    """Resolve only a production-shaped nested moving-entry identity."""
    moving = output.get("moving_reference")
    expected = _effective_side_by_side_moving_scope(invocation)
    required = {
        "project_file",
        "project_entry_id",
        "image_id",
        "image_sha256",
        "image_path",
        "project_entry_path",
        "moving_modality",
        "source_metadata",
        "physical_calibration",
    }
    if (
        output.get("mode") != "side_by_side"
        or expected is None
        or not isinstance(moving, Mapping)
        or not required.issubset(moving)
    ):
        return None
    text_fields = (
        "project_file",
        "project_entry_id",
        "image_id",
        "image_path",
        "project_entry_path",
        "moving_modality",
    )
    if any(
        not isinstance(moving.get(field), str) or not moving[field].strip() for field in text_fields
    ):
        return None
    try:
        evidence_image_path = Path(moving["image_path"]).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None
    image_sha256 = moving.get("image_sha256")
    if (
        not isinstance(image_sha256, str)
        or len(image_sha256) != 64
        or any(character not in "0123456789abcdef" for character in image_sha256)
        or moving.get("image_id") != expected["image_id"]
        or evidence_image_path != expected["image_path"]
        or moving.get("moving_modality") != expected["moving_modality"]
        or not isinstance(moving.get("source_metadata"), Mapping)
        or dict(moving["source_metadata"]) != expected["source_metadata"]
        or not _is_valid_physical_calibration_evidence(moving.get("physical_calibration"))
    ):
        return None
    return moving["project_entry_id"]


def _effective_side_by_side_moving_scope(
    invocation: StageInvocation,
) -> dict[str, object] | None:
    """Return the exact moving source scope accepted for side-by-side use."""
    choice = invocation.state.get("_registration_choice")
    if isinstance(choice, Mapping) and choice.get("mode") == "side_by_side":
        if choice.get("availability") != "available":
            return None
        image = choice.get("moving_image")
        image_id = choice.get("moving_image_id")
        modality = choice.get("moving_modality")
        source_metadata = choice.get("source_metadata")
    else:
        registration = invocation.config.registration
        if registration.mode != "side_by_side":
            return None
        image = registration.moving_image
        image_id = registration.moving_image_id
        modality = registration.moving_modality
        source_metadata = registration.moving_image_metadata
    if (
        not isinstance(image, (str, Path))
        or not str(image).strip()
        or not isinstance(image_id, str)
        or not image_id.strip()
        or not isinstance(modality, str)
        or not modality.strip()
        or not isinstance(source_metadata, Mapping)
    ):
        return None
    return {
        "image_path": Path(image).expanduser().resolve(strict=False),
        "image_id": image_id,
        "moving_modality": modality,
        "source_metadata": _mutable_json(source_metadata),
    }


def _domain_set_sha256(manifests: Sequence[AnalysisDomainManifest]) -> str:
    return _mapping_sha256(
        {
            "schema_version": "analysis-domain-set/v1",
            "analysis_domain_manifest_sha256s": sorted(
                manifest.content_sha256 for manifest in manifests
            ),
        }
    )


def _domain_revision_mapping(revision: AnalysisDomainRevision) -> dict[str, object]:
    return {
        "analysis_domain_id": revision.ref.analysis_domain_id,
        "kind": revision.ref.kind.value,
        "slide_id": revision.ref.slide_id,
        "section_id": revision.ref.section_id,
        "tissue_generation_id": revision.ref.tissue_generation_id,
        "canonical_image_id": revision.canonical_image_id,
        "canonical_image_sha256": revision.canonical_image_sha256,
        "project_entry_id": revision.project_entry_id,
        "tissue_roi_id": revision.tissue_roi_id,
        "tissue_path_object_id": revision.tissue_path_object_id,
        "source_path_object_id": revision.source_path_object_id,
        "revision": revision.revision,
        "predecessor_sha256": revision.predecessor_sha256,
        "geometry_digest_policy": revision.geometry_digest_policy,
        "geometry_sha256": revision.geometry_sha256,
        "normalized_wkb_hex": (
            None if revision.normalized_wkb is None else revision.normalized_wkb.hex()
        ),
        "geometry_reference_path_object_id": (revision.geometry_reference_path_object_id),
        "content_sha256": revision.content_sha256,
    }


def _domain_manifest_mapping(manifest: AnalysisDomainManifest) -> dict[str, object]:
    return {
        "schema_version": manifest.schema_version,
        "canonical_image_id": manifest.canonical_image_id,
        "canonical_image_sha256": manifest.canonical_image_sha256,
        "project_entry_id": manifest.project_entry_id,
        "slide_id": manifest.slide_id,
        "section_id": manifest.section_id,
        "tissue_generation_id": manifest.tissue_generation_id,
        "tissue_roi_id": manifest.tissue_roi_id,
        "tissue_path_object_id": manifest.tissue_path_object_id,
        "revisions": [_domain_revision_mapping(revision) for revision in manifest.revisions],
        "content_sha256": manifest.content_sha256,
    }


def _validated_whole_tissue_assistance(
    value: object,
    *,
    project_entry_id: str,
) -> dict[str, object]:
    """Return one closed, plain, path-free assistance value."""

    def reject_rich_or_path_like(item: object) -> None:
        if isinstance(item, os.PathLike) or isinstance(item, Enum):
            raise ROIAnalysisError("analysis-domain assistance must contain plain path-free JSON")
        if isinstance(item, str):
            if item == "analysis-domain-assistance/v1":
                return
            folded = item.casefold()
            if (
                "/" in item
                or "\\" in item
                or folded.startswith("file:")
                or folded.endswith(".qpproj")
                or item in {".", ".."}
                or re.match(r"^[A-Za-z]:", item) is not None
            ):
                raise ROIAnalysisError(
                    "analysis-domain assistance must not contain path-like values"
                )
            return
        if item is None or type(item) in {bool, int, float}:
            if isinstance(item, float) and not math.isfinite(item):
                raise ROIAnalysisError("analysis-domain assistance numbers must be finite")
            return
        if isinstance(item, Mapping):
            if any(type(key) is not str for key in item):
                raise ROIAnalysisError("analysis-domain assistance requires string keys")
            for key, nested in item.items():
                reject_rich_or_path_like(key)
                reject_rich_or_path_like(nested)
            return
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            for nested in item:
                reject_rich_or_path_like(nested)
            return
        raise ROIAnalysisError("analysis-domain assistance must contain plain path-free JSON")

    reject_rich_or_path_like(value)
    if not isinstance(value, Mapping):
        raise ROIAnalysisError("analysis-domain assistance must be an object")
    mode = value.get("mode")
    common_fields = (
        "schema_version",
        "mode",
        "canonical_project_entry_id",
        "annotation_project_entry_id",
        "coordinate_system",
        "coordinate_transfer_authorized",
    )
    mode_fields = {
        "unavailable": common_fields,
        "side_by_side": (*common_fields, "moving_project_entry_id"),
        "warpy_overlay": (
            *common_fields,
            "registration_id",
            "moving_project_entry_id",
            "section_registration_qc",
        ),
    }
    expected = mode_fields.get(mode)
    if expected is None or set(value) != set(expected):
        raise ROIAnalysisError("analysis-domain assistance mode or fields are unsupported")
    if (
        value["schema_version"] != "analysis-domain-assistance/v1"
        or value["canonical_project_entry_id"] != project_entry_id
        or value["annotation_project_entry_id"] != project_entry_id
        or value["coordinate_system"] != "global_level0_pixels"
        or value["coordinate_transfer_authorized"] is not False
    ):
        raise ROIAnalysisError("analysis-domain assistance scope is invalid")
    for field in expected:
        if field in {"coordinate_transfer_authorized", "section_registration_qc"}:
            continue
        if type(value[field]) is not str or not value[field].strip():
            raise ROIAnalysisError(f"analysis-domain assistance {field} must be nonblank text")
    if mode == "warpy_overlay":
        raw_qc = value["section_registration_qc"]
        if (
            isinstance(raw_qc, (str, bytes, bytearray))
            or not isinstance(raw_qc, Sequence)
            or not raw_qc
        ):
            raise ROIAnalysisError("Warpy assistance requires section registration QC")
        registration_id = value["registration_id"]
        sections: list[dict[str, object]] = []
        seen: set[str] = set()
        for raw in raw_qc:
            if not isinstance(raw, Mapping) or set(raw) != {
                "section_id",
                "registration_id",
                "registration_qc",
            }:
                raise ROIAnalysisError("Warpy assistance section QC is not closed")
            section_id = raw["section_id"]
            qc = raw["registration_qc"]
            if (
                type(section_id) is not str
                or not section_id.strip()
                or section_id in seen
                or raw["registration_id"] != registration_id
                or not isinstance(qc, Mapping)
                or dict(qc) != {"status": "passed"}
            ):
                raise ROIAnalysisError("Warpy assistance section QC is invalid")
            seen.add(section_id)
            sections.append(
                {
                    "section_id": section_id,
                    "registration_id": registration_id,
                    "registration_qc": {"status": "passed"},
                }
            )
        if [item["section_id"] for item in sections] != sorted(seen):
            raise ROIAnalysisError("Warpy assistance section QC must be sorted")
        return {
            **{key: value[key] for key in expected if key != "section_registration_qc"},
            "section_registration_qc": sections,
        }
    return {key: value[key] for key in expected}


def build_whole_tissue_analysis_domain_output(
    *,
    canonical_image_id: str,
    canonical_image_sha256: str,
    project_entry_id: str,
    slide_id: str,
    tissue_generation_id: str,
    accepted_tissue_sections: Sequence[Mapping[str, object]],
    analysis_domain_assistance: Mapping[str, object],
) -> dict[str, object]:
    """Build the shared reference-only whole-tissue scientific projection."""

    for name, value in (
        ("canonical_image_id", canonical_image_id),
        ("project_entry_id", project_entry_id),
        ("slide_id", slide_id),
        ("tissue_generation_id", tissue_generation_id),
    ):
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ROIAnalysisError(f"{name} must be nonblank canonical text")
    if (
        not isinstance(canonical_image_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", canonical_image_sha256) is None
    ):
        raise ROIAnalysisError("canonical_image_sha256 must be a lowercase SHA-256")
    if (
        isinstance(accepted_tissue_sections, (str, bytes, bytearray))
        or not isinstance(accepted_tissue_sections, Sequence)
        or not accepted_tissue_sections
    ):
        raise ROIAnalysisError("whole-tissue projection requires accepted TissueSections")
    assistance = _validated_whole_tissue_assistance(
        analysis_domain_assistance,
        project_entry_id=project_entry_id,
    )

    manifests: list[AnalysisDomainManifest] = []
    evidence: list[dict[str, object]] = []
    section_ids: set[str] = set()
    path_object_ids: set[str] = set()
    expected_section_fields = {
        "section_id",
        "tissue_roi_id",
        "tissue_path_object_id",
        "tissue_generation_id",
    }
    for raw_section in accepted_tissue_sections:
        if not isinstance(raw_section, Mapping) or set(raw_section) != expected_section_fields:
            raise ROIAnalysisError("accepted TissueSection projection is not closed")
        section = {key: raw_section[key] for key in expected_section_fields}
        if any(
            not isinstance(value, str) or not value.strip() or value != value.strip()
            for value in section.values()
        ):
            raise ROIAnalysisError("accepted TissueSection identity is invalid")
        if section["tissue_generation_id"] != tissue_generation_id:
            raise ROIAnalysisError("accepted TissueSection generation differs")
        section_id = section["section_id"]
        source_id = section["tissue_path_object_id"]
        assert isinstance(section_id, str) and isinstance(source_id, str)
        if section_id in section_ids or source_id in path_object_ids:
            raise ROIAnalysisError("accepted TissueSection identities must be unique")
        section_ids.add(section_id)
        path_object_ids.add(source_id)
        ref = AnalysisDomainRef(
            analysis_domain_id=analysis_domain_id(
                kind=AnalysisDomainKind.WHOLE_TISSUE_SECTION,
                canonical_image_sha256=canonical_image_sha256,
                tissue_generation_id=section["tissue_generation_id"],
                section_id=section_id,
                source_path_object_id=source_id,
            ),
            kind=AnalysisDomainKind.WHOLE_TISSUE_SECTION,
            slide_id=slide_id,
            section_id=section_id,
            tissue_generation_id=section["tissue_generation_id"],
        )
        revision = AnalysisDomainRevision(
            ref=ref,
            canonical_image_id=canonical_image_id,
            canonical_image_sha256=canonical_image_sha256,
            project_entry_id=project_entry_id,
            tissue_roi_id=section["tissue_roi_id"],
            tissue_path_object_id=source_id,
            source_path_object_id=source_id,
            revision=1,
            predecessor_sha256=None,
            geometry_digest_policy="jts-normalized-wkb-sha256/v1",
            geometry_sha256=None,
            normalized_wkb=None,
            geometry_reference_path_object_id=source_id,
        )
        manifest = AnalysisDomainManifest(
            schema_version="analysis-domain/v1",
            canonical_image_id=canonical_image_id,
            canonical_image_sha256=canonical_image_sha256,
            project_entry_id=project_entry_id,
            slide_id=slide_id,
            section_id=section_id,
            tissue_generation_id=section["tissue_generation_id"],
            tissue_roi_id=section["tissue_roi_id"],
            tissue_path_object_id=source_id,
            revisions=(revision,),
        )
        overlap = DomainOverlapEvidence(
            canonical_image_id=canonical_image_id,
            canonical_image_sha256=canonical_image_sha256,
            project_entry_id=project_entry_id,
            section_id=section_id,
            tissue_generation_id=section["tissue_generation_id"],
            tissue_roi_id=section["tissue_roi_id"],
            tissue_path_object_id=source_id,
            geometry_evidence=(),
            positive_area_overlaps=(),
            generated_by_spatial_index=True,
            exact_jts_area_test=True,
        )
        validate_domain_manifest(manifest, overlap)
        manifests.append(manifest)
        evidence.append(_domain_overlap_mapping(overlap))
    return {
        "schema_version": "analysis-domain-selection/v1",
        "selection_status": "accepted",
        "selection_policy": "whole_tissue",
        "canonical_image_id": canonical_image_id,
        "canonical_image_sha256": canonical_image_sha256,
        "project_entry_id": project_entry_id,
        "tissue_generation_id": tissue_generation_id,
        "analysis_domain_manifests": [_domain_manifest_mapping(manifest) for manifest in manifests],
        "overlap_evidence": evidence,
        "domain_set_sha256": _domain_set_sha256(manifests),
        "saved_closed_acknowledged": True,
        "selected_annotations_locked": True,
        "analysis_domain_assistance": dict(assistance),
    }


def _whole_tissue_domain_output(
    invocation: StageInvocation,
) -> dict[str, object] | None:
    if invocation.stage != "analysis_domain_selection":
        return None
    if (
        invocation.config.analysis_domain.selection_policy != "whole_tissue"
        and invocation.action != "use_whole_tissue"
    ):
        return None
    canonical_image_id, canonical_sha = _canonical_identity(invocation.state)
    generation_id = _generation_id(invocation.state)
    if generation_id is None:
        raise ROIAnalysisError("accepted TissueSection generation is missing")
    return build_whole_tissue_analysis_domain_output(
        canonical_image_id=canonical_image_id,
        canonical_image_sha256=canonical_sha,
        project_entry_id=_project_entry_id(invocation.state),
        slide_id=invocation.config.slide_id,
        tissue_generation_id=generation_id,
        accepted_tissue_sections=_accepted_tissue_section_records(invocation.state),
        analysis_domain_assistance=_analysis_domain_assistance(invocation),
    )


def _domain_geometry_mapping(evidence: DomainGeometryEvidence) -> dict[str, object]:
    return {
        "path_object_id": evidence.path_object_id,
        "parent_path_object_id": evidence.parent_path_object_id,
        "tissue_roi_id": evidence.tissue_roi_id,
        "tissue_path_object_id": evidence.tissue_path_object_id,
        "geometry_digest_policy": evidence.geometry_digest_policy,
        "geometry_sha256": evidence.geometry_sha256,
        "normalized_wkb_hex": (
            None if evidence.normalized_wkb is None else evidence.normalized_wkb.hex()
        ),
        "is_valid": evidence.is_valid,
        "is_empty": evidence.is_empty,
        "is_finite": evidence.is_finite,
        "area": evidence.area,
        "covered_by_parent": evidence.covered_by_parent,
    }


def _domain_overlap_mapping(evidence: DomainOverlapEvidence) -> dict[str, object]:
    return {
        "canonical_image_id": evidence.canonical_image_id,
        "canonical_image_sha256": evidence.canonical_image_sha256,
        "project_entry_id": evidence.project_entry_id,
        "section_id": evidence.section_id,
        "tissue_generation_id": evidence.tissue_generation_id,
        "tissue_roi_id": evidence.tissue_roi_id,
        "tissue_path_object_id": evidence.tissue_path_object_id,
        "geometry_evidence": [
            _domain_geometry_mapping(item) for item in evidence.geometry_evidence
        ],
        "positive_area_overlaps": [list(pair) for pair in evidence.positive_area_overlaps],
        "generated_by_spatial_index": evidence.generated_by_spatial_index,
        "exact_jts_area_test": evidence.exact_jts_area_test,
    }


def _normalized_wkb(value: object, *, required: bool) -> bytes | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value or len(value) % 2:
        raise ROIAnalysisError("normalized WKB must be lowercase hexadecimal")
    try:
        decoded = bytes.fromhex(value)
    except ValueError as exc:
        raise ROIAnalysisError("normalized WKB must be lowercase hexadecimal") from exc
    if decoded.hex() != value:
        raise ROIAnalysisError("normalized WKB must be lowercase hexadecimal")
    return decoded


def _domain_revision_from_mapping(value: object) -> AnalysisDomainRevision:
    if not isinstance(value, Mapping):
        raise ROIAnalysisError("analysis-domain revision must be a mapping")
    try:
        kind = AnalysisDomainKind(value.get("kind"))
    except (TypeError, ValueError) as exc:
        raise ROIAnalysisError("analysis-domain revision kind is invalid") from exc
    normalized_wkb = _normalized_wkb(
        value.get("normalized_wkb_hex"),
        required=kind is AnalysisDomainKind.SELECTED_ROI,
    )
    revision = AnalysisDomainRevision(
        ref=AnalysisDomainRef(
            analysis_domain_id=value.get("analysis_domain_id"),
            kind=kind,
            slide_id=value.get("slide_id"),
            section_id=value.get("section_id"),
            tissue_generation_id=value.get("tissue_generation_id"),
        ),
        canonical_image_id=value.get("canonical_image_id"),
        canonical_image_sha256=value.get("canonical_image_sha256"),
        project_entry_id=value.get("project_entry_id"),
        tissue_roi_id=value.get("tissue_roi_id"),
        tissue_path_object_id=value.get("tissue_path_object_id"),
        source_path_object_id=value.get("source_path_object_id"),
        revision=value.get("revision"),
        predecessor_sha256=value.get("predecessor_sha256"),
        geometry_digest_policy=value.get("geometry_digest_policy"),
        geometry_sha256=value.get("geometry_sha256"),
        normalized_wkb=normalized_wkb,
        geometry_reference_path_object_id=value.get("geometry_reference_path_object_id"),
    )
    if value.get("content_sha256") != revision.content_sha256:
        raise ROIAnalysisError("analysis-domain revision content SHA-256 changed")
    return revision


def _domain_manifest_from_mapping(value: object) -> AnalysisDomainManifest:
    if not isinstance(value, Mapping):
        raise ROIAnalysisError("analysis-domain manifest must be a mapping")
    raw_revisions = value.get("revisions")
    if isinstance(raw_revisions, (str, bytes)) or not isinstance(raw_revisions, Sequence):
        raise ROIAnalysisError("analysis-domain manifest revisions are invalid")
    manifest = AnalysisDomainManifest(
        schema_version=value.get("schema_version"),
        canonical_image_id=value.get("canonical_image_id"),
        canonical_image_sha256=value.get("canonical_image_sha256"),
        project_entry_id=value.get("project_entry_id"),
        slide_id=value.get("slide_id"),
        section_id=value.get("section_id"),
        tissue_generation_id=value.get("tissue_generation_id"),
        tissue_roi_id=value.get("tissue_roi_id"),
        tissue_path_object_id=value.get("tissue_path_object_id"),
        revisions=tuple(_domain_revision_from_mapping(item) for item in raw_revisions),
    )
    if value.get("content_sha256") != manifest.content_sha256:
        raise ROIAnalysisError("analysis-domain manifest content SHA-256 changed")
    return manifest


def _domain_overlap_from_mapping(value: object) -> DomainOverlapEvidence:
    if not isinstance(value, Mapping):
        raise ROIAnalysisError("analysis-domain overlap evidence must be a mapping")
    raw_geometry = value.get("geometry_evidence")
    raw_overlaps = value.get("positive_area_overlaps")
    if (
        isinstance(raw_geometry, (str, bytes))
        or not isinstance(raw_geometry, Sequence)
        or isinstance(raw_overlaps, (str, bytes))
        or not isinstance(raw_overlaps, Sequence)
    ):
        raise ROIAnalysisError("analysis-domain overlap evidence is invalid")
    geometry: list[DomainGeometryEvidence] = []
    for raw in raw_geometry:
        if not isinstance(raw, Mapping):
            raise ROIAnalysisError("analysis-domain geometry evidence is invalid")
        geometry.append(
            DomainGeometryEvidence(
                path_object_id=raw.get("path_object_id"),
                parent_path_object_id=raw.get("parent_path_object_id"),
                tissue_roi_id=raw.get("tissue_roi_id"),
                tissue_path_object_id=raw.get("tissue_path_object_id"),
                geometry_digest_policy=raw.get("geometry_digest_policy"),
                geometry_sha256=raw.get("geometry_sha256"),
                normalized_wkb=_normalized_wkb(raw.get("normalized_wkb_hex"), required=True),
                is_valid=raw.get("is_valid"),
                is_empty=raw.get("is_empty"),
                is_finite=raw.get("is_finite"),
                area=raw.get("area"),
                covered_by_parent=raw.get("covered_by_parent"),
            )
        )
    overlaps: list[tuple[str, str]] = []
    for raw in raw_overlaps:
        if (
            isinstance(raw, (str, bytes))
            or not isinstance(raw, Sequence)
            or len(raw) != 2
            or any(not isinstance(item, str) or not item.strip() for item in raw)
        ):
            raise ROIAnalysisError("analysis-domain overlap pair is invalid")
        overlaps.append((raw[0], raw[1]))
    return DomainOverlapEvidence(
        canonical_image_id=value.get("canonical_image_id"),
        canonical_image_sha256=value.get("canonical_image_sha256"),
        project_entry_id=value.get("project_entry_id"),
        section_id=value.get("section_id"),
        tissue_generation_id=value.get("tissue_generation_id"),
        tissue_roi_id=value.get("tissue_roi_id"),
        tissue_path_object_id=value.get("tissue_path_object_id"),
        geometry_evidence=tuple(geometry),
        positive_area_overlaps=tuple(overlaps),
        generated_by_spatial_index=value.get("generated_by_spatial_index"),
        exact_jts_area_test=value.get("exact_jts_area_test"),
    )


def _validated_analysis_domain_output(
    invocation: StageInvocation,
    output: Mapping[str, Any],
) -> Mapping[str, Any]:
    if output.get("saved_closed_acknowledged") is not True:
        raise ROIAnalysisError("analysis-domain snapshot requires saved/closed acknowledgement")
    status = output.get("selection_status")
    if status not in {"candidate", "accepted"}:
        raise ROIAnalysisError("analysis-domain selection status is invalid")
    if invocation.action == "accept_analysis_rois" and status != "accepted":
        raise ROIAnalysisError("analysis-domain acceptance did not publish accepted state")
    schema = output.get("schema_version")
    if schema not in {
        "analysis-domain-selection/v1",
        "analysis-domain-candidate-manifest/v1",
    }:
        raise ROIAnalysisError("analysis-domain stage output schema is invalid")
    raw_manifests = output.get("analysis_domain_manifests")
    raw_evidence = output.get("overlap_evidence")
    if (
        isinstance(raw_manifests, (str, bytes))
        or not isinstance(raw_manifests, Sequence)
        or isinstance(raw_evidence, (str, bytes))
        or not isinstance(raw_evidence, Sequence)
    ):
        raise ROIAnalysisError("analysis-domain manifests and evidence are required")
    manifests = tuple(_domain_manifest_from_mapping(item) for item in raw_manifests)
    overlap = tuple(_domain_overlap_from_mapping(item) for item in raw_evidence)
    expected_sections = {
        item["section_id"]: item for item in _accepted_tissue_section_records(invocation.state)
    }
    if not manifests or not {item.section_id for item in manifests} <= set(expected_sections):
        raise ROIAnalysisError("Select at least one analysis region within the accepted tissue sections")
    canonical_image_id, canonical_sha = _canonical_identity(invocation.state)
    project_entry_id = _project_entry_id(invocation.state)
    evidence_by_domain: dict[tuple[str, tuple[str, ...]], DomainOverlapEvidence] = {}
    for item in overlap:
        evidence_key = (
            item.section_id,
            tuple(sorted(evidence.path_object_id for evidence in item.geometry_evidence)),
        )
        if evidence_key in evidence_by_domain:
            raise ROIAnalysisError("analysis-domain overlap evidence identity is duplicated")
        evidence_by_domain[evidence_key] = item
    manifest_keys: set[tuple[str, tuple[str, ...]]] = set()
    for manifest in manifests:
        expected = expected_sections[manifest.section_id]
        if (
            manifest.canonical_image_id != canonical_image_id
            or manifest.canonical_image_sha256 != canonical_sha
            or manifest.project_entry_id != project_entry_id
            or manifest.slide_id != invocation.config.slide_id
            or manifest.tissue_generation_id != expected["tissue_generation_id"]
            or manifest.tissue_roi_id != expected["tissue_roi_id"]
            or manifest.tissue_path_object_id != expected["tissue_path_object_id"]
        ):
            raise ROIAnalysisError(
                "analysis-domain manifest does not match current mIF tissue scope"
            )
        lineage_ids = {revision.ref.analysis_domain_id for revision in manifest.revisions}
        source_ids = {revision.source_path_object_id for revision in manifest.revisions}
        if len(lineage_ids) != 1 or len(source_ids) != 1:
            raise ROIAnalysisError(
                "each analysis-domain manifest must contain one independent lineage"
            )
        is_selected = manifest.revisions[-1].ref.kind is AnalysisDomainKind.SELECTED_ROI
        manifest_key = (
            manifest.section_id,
            tuple(sorted(source_ids)) if is_selected else (),
        )
        if manifest_key in manifest_keys:
            raise ROIAnalysisError("analysis-domain manifest identity is duplicated")
        manifest_keys.add(manifest_key)
        evidence = evidence_by_domain.get(manifest_key)
        if evidence is None:
            raise ROIAnalysisError("analysis-domain manifest lacks overlap evidence")
        validate_domain_manifest(manifest, evidence)
    if set(evidence_by_domain) != manifest_keys:
        raise ROIAnalysisError(
            "analysis-domain overlap evidence does not match the manifest inventory"
        )
    if output.get("domain_set_sha256") != _domain_set_sha256(manifests):
        raise ROIAnalysisError("analysis-domain set SHA-256 changed")
    if output.get("canonical_image_id") != canonical_image_id:
        raise ROIAnalysisError("analysis-domain output canonical image ID changed")
    if output.get("canonical_image_sha256") != canonical_sha:
        raise ROIAnalysisError("analysis-domain output canonical image SHA-256 changed")
    if output.get("project_entry_id") != project_entry_id:
        raise ROIAnalysisError("analysis-domain output project entry changed")
    if output.get("tissue_generation_id") != _generation_id(invocation.state):
        raise ROIAnalysisError("analysis-domain output tissue generation changed")
    normalized = _mutable_json(output)
    normalized["analysis_domain_assistance"] = _analysis_domain_assistance(invocation)
    return normalized


def _candidate_path_object_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ()
    direct = value.get("candidate_path_object_ids")
    if isinstance(direct, Sequence) and not isinstance(direct, (str, bytes)):
        identifiers = tuple(direct)
    else:
        manifests = value.get("analysis_domain_manifests")
        if isinstance(manifests, (str, bytes)) or not isinstance(manifests, Sequence):
            return ()
        identifiers = tuple(
            revisions[-1].get("source_path_object_id")
            for manifest in manifests
            if isinstance(manifest, Mapping)
            for revisions in (manifest.get("revisions"),)
            if isinstance(revisions, Sequence)
            and not isinstance(revisions, (str, bytes))
            and revisions
            and isinstance(revisions[-1], Mapping)
            and revisions[-1].get("kind") == AnalysisDomainKind.SELECTED_ROI.value
        )
    if any(not isinstance(item, str) or not item.strip() for item in identifiers):
        raise ROIAnalysisError("candidate PathObject UUID inventory is invalid")
    if len(set(identifiers)) != len(identifiers):
        raise ROIAnalysisError("candidate PathObject UUID inventory is duplicated")
    return identifiers


def _validated_analysis_domain_context_manifest(
    invocation: StageInvocation,
    value: object,
    *,
    role: str,
    allowed_statuses: frozenset[str],
) -> Mapping[str, Any] | None:
    """Validate one explicitly scoped candidate or retirement manifest role."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ROIAnalysisError(f"analysis-domain {role} must be a manifest object")
    if value.get("schema_version") == "analysis-domain-manifest-reference/v1":
        value = _load_analysis_domain_manifest_reference(invocation, value, role=role)
    if value.get("schema_version") != "analysis-domain-candidate-manifest/v1":
        raise ROIAnalysisError(f"analysis-domain {role} schema is invalid")
    status = value.get("selection_status")
    if status not in allowed_statuses:
        raise ROIAnalysisError(f"analysis-domain {role} status is invalid")
    if value.get("selected_annotations_locked") is not (status == "accepted"):
        raise ROIAnalysisError(f"analysis-domain {role} lock state is invalid")
    validation_invocation = replace(
        invocation,
        action="review_analysis_rois",
    )
    _validated_analysis_domain_output(validation_invocation, value)
    if not _candidate_path_object_ids(value):
        raise ROIAnalysisError(f"analysis-domain {role} selected-ROI inventory is empty")
    return _mutable_json(value)


def _analysis_domain_manifest_reference(
    invocation: StageInvocation,
    manifest: Mapping[str, object],
    *,
    role: str,
) -> dict[str, object]:
    """Publish geometry-bearing resume authority outside compact batch state."""

    safe_role = validate_safe_path_segment(role, "analysis-domain manifest role")
    content = _canonical_artifact_bytes(manifest)
    sha256 = hashlib.sha256(content).hexdigest()
    path = (
        invocation.config.output_dir
        / "analysis-domains"
        / "interaction-authority"
        / f"{safe_role}-{sha256}.json"
    ).resolve(strict=False)
    _publish_immutable_artifact(path, content, label="analysis-domain interaction authority")
    return {
        "schema_version": "analysis-domain-manifest-reference/v1",
        "role": role,
        "manifest_schema_version": manifest.get("schema_version"),
        "path": str(path),
        "sha256": sha256,
        "byte_count": len(content),
        "selection_status": manifest.get("selection_status"),
        "domain_set_sha256": manifest.get("domain_set_sha256"),
        "candidate_path_object_ids": list(_candidate_path_object_ids(manifest)),
    }


def _load_analysis_domain_manifest_reference(
    invocation: StageInvocation,
    reference: Mapping[str, object],
    *,
    role: str,
) -> Mapping[str, object]:
    """Read analysis domain manifest reference into its typed representation.

    The decoded fields are checked through the record constructors and the applicable
    context comparisons before returning the result. The validated representation is
    returned for the subsequent operation."""

    expected_fields = {
        "schema_version",
        "role",
        "manifest_schema_version",
        "path",
        "sha256",
        "byte_count",
        "selection_status",
        "domain_set_sha256",
        "candidate_path_object_ids",
    }
    path_value = reference.get("path")
    sha256 = reference.get("sha256")
    byte_count = reference.get("byte_count")
    if (
        set(reference) != expected_fields
        or reference.get("role") != role
        or not isinstance(path_value, str)
        or not path_value.strip()
        or not _is_sha256(sha256)
        or type(byte_count) is not int
        or byte_count <= 0
    ):
        raise ROIAnalysisError(f"analysis-domain {role} manifest reference is invalid")
    path = Path(path_value).expanduser().resolve(strict=False)
    root = invocation.config.output_dir.resolve(strict=False)
    try:
        owned = path.is_relative_to(root)
    except AttributeError:  # pragma: no cover - Python <3.9 compatibility
        owned = path == root or root in path.parents
    if not owned or not path.is_file():
        raise ROIAnalysisError(f"analysis-domain {role} manifest reference is not project-owned")
    try:
        content = path.read_bytes()
        decoded = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ROIAnalysisError(f"analysis-domain {role} manifest reference is unreadable") from exc
    if (
        len(content) != byte_count
        or hashlib.sha256(content).hexdigest() != sha256
        or not isinstance(decoded, Mapping)
        or _canonical_artifact_bytes(decoded) != content
        or decoded.get("schema_version") != reference.get("manifest_schema_version")
        or decoded.get("selection_status") != reference.get("selection_status")
        or decoded.get("domain_set_sha256") != reference.get("domain_set_sha256")
        or list(_candidate_path_object_ids(decoded))
        != list(reference.get("candidate_path_object_ids", ()))
    ):
        raise ROIAnalysisError(f"analysis-domain {role} manifest reference changed")
    return decoded


def _optional_segmentation_population_id(
    state: Mapping[str, Any], section_id: str | None
) -> str | None:
    if section_id is None:
        return None
    segmentation = state.get("segmentation")
    scoped = segmentation.get(section_id) if isinstance(segmentation, Mapping) else None
    if not isinstance(scoped, Mapping):
        return None
    value = scoped.get("segmentation_population_id")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ROIAnalysisError("segmentation population identity is invalid")
    return value


def _segmentation_population_ids(
    state: Mapping[str, Any],
) -> dict[str, str]:
    segmentation = state.get("segmentation")
    if not isinstance(segmentation, Mapping):
        return {}
    result: dict[str, str] = {}
    for section_id, scoped in segmentation.items():
        if not isinstance(section_id, str) or not isinstance(scoped, Mapping):
            continue
        value = scoped.get("segmentation_population_id")
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise ROIAnalysisError("segmentation population identity is invalid")
        result[section_id] = value
    return dict(sorted(result.items()))


def _effective_registration_mode(invocation: StageInvocation) -> str:
    raw = invocation.state.get("_registration_choice")
    if isinstance(raw, Mapping) and raw.get("mode") in {"none", "warpy", "side_by_side", "skip"}:
        return raw["mode"]
    configured = invocation.config.registration.mode
    if configured != "prompt":
        return configured
    return "prompt"


def _registration_context(invocation: StageInvocation) -> Mapping[str, Any]:
    raw = invocation.state.get("registration", {})
    if not isinstance(raw, Mapping):
        raise ROIAnalysisError("registration-stage state must be a mapping")
    context = _mutable_json(raw)
    effective_mode = _effective_registration_mode(invocation)
    context["mode"] = effective_mode
    if effective_mode != "warpy":
        context["moving_assisted_scopes"] = []
        return context
    if invocation.config.registration.moving_assistance_policy == "configured":
        selected = [
            item.to_mapping() for item in invocation.config.registration.moving_assistance_scopes
        ]
    else:
        decision = invocation.state.get("_moving_assistance_scope_decision", {})
        selected = decision.get("scopes", ()) if isinstance(decision, Mapping) else ()
    if not selected:
        context["moving_assisted_scopes"] = []
        return context
    sections = _accepted_sections(invocation.state)
    available = _available_registration_evidence(raw, sections)
    passed = {
        (item["section_id"], item["registration_id"]): item["registration_qc"] for item in available
    }
    domain_sections = {
        item["analysis_domain_id"]: item["section_id"]
        for item in _accepted_analysis_domain_scopes(invocation.state)
    }
    scopes = []
    for raw_scope in selected:
        if not isinstance(raw_scope, Mapping):
            raise ROIAnalysisError("moving assistance scope state is invalid")
        section_id = domain_sections.get(raw_scope.get("analysis_domain_id"))
        key = (section_id, raw_scope.get("registration_id"))
        registration_qc = passed.get(key)
        if registration_qc != {"status": "passed"}:
            raise ROIAnalysisError(
                "moving assistance scope is not tied to exact passed registration QC"
            )
        scopes.append(
            {
                name: raw_scope.get(name)
                for name in (
                    "stage",
                    "analysis_domain_id",
                    "structure_id",
                    "eligible_parent_ids",
                    "registration_id",
                    "decision_id",
                    "decision_source",
                    "decided_by",
                )
            }
            | {"registration_qc": registration_qc}
        )
    context["moving_assisted_scopes"] = scopes
    return context


def _available_registration_evidence(
    output: Mapping[str, Any],
    section_ids: Sequence[str],
) -> tuple[dict[str, object], ...]:
    record = output.get("registration")
    if not isinstance(record, Mapping):
        raise ROIAnalysisError("moving assistance requires an accepted registration record")
    registration_id = record.get("registration_id")
    qc = record.get("registration_qc")
    if (
        record.get("provider") != "warpy"
        or record.get("acceptance_state") != "accepted"
        or not isinstance(registration_id, str)
        or not isinstance(qc, Mapping)
        or qc.get("computed_status") != "passed"
        or qc.get("review_state") != "accepted"
    ):
        raise ROIAnalysisError("moving assistance requires accepted passing Warpy QC")
    raw_sections = qc.get("section_records")
    if isinstance(raw_sections, (str, bytes)) or not isinstance(raw_sections, Sequence):
        raise ROIAnalysisError("registration QC section records must be an array")
    passed_sections = {
        item.get("section_id")
        for item in raw_sections
        if isinstance(item, Mapping) and item.get("computed_status") == "passed"
    }
    available: list[dict[str, object]] = []
    for section_id in section_ids:
        if section_id not in passed_sections:
            raise ROIAnalysisError(
                f"moving assistance lacks passed registration QC for section {section_id!r}"
            )
        available.append(
            {
                "section_id": section_id,
                "registration_id": registration_id,
                "registration_qc": {"status": "passed"},
            }
        )
    return tuple(available)


def _classifier_state_key(
    invocation: StageInvocation,
    classifier_id: str,
) -> str:
    if invocation.stage == "primary_classifier":
        return f"primary:{classifier_id}"
    return f"secondary:{invocation.structure_id}:{classifier_id}"


def _primary_output_inventory_for_invocation(
    invocation: StageInvocation,
) -> tuple[str, ...]:
    """Return the one immutable authority for this primary invocation's outputs."""
    settings, _plan_ref = _primary_settings_for_invocation(invocation)
    classifier = settings.primary_classifier
    try:
        configured_outputs = _validate_primary_output_inventory(
            tuple(item.class_id for item in settings.primary_classes),
            classifier.output_classes,
            require_ignore_training=classifier.require_ignore_training,
        )
    except ClassifierCompatibilityError as exc:
        raise ROIAnalysisError(
            "accepted primary settings output inventory is incompatible"
        ) from exc
    decisions = invocation.state.get("_classifier_reuse_decisions", {})
    if not isinstance(decisions, Mapping):
        raise ROIAnalysisError("accepted classifier decisions are malformed")
    decision = decisions.get(_classifier_state_key(invocation, classifier.classifier_id))
    if decision is None:
        return configured_outputs
    if not isinstance(decision, Mapping):
        raise ROIAnalysisError("accepted primary classifier decision is malformed")
    try:
        definition = ClassifierDefinition.from_mapping(decision.get("classifier_definition"))
    except (ClassifierCompatibilityError, TypeError, ValueError) as exc:
        raise ROIAnalysisError(
            "accepted primary classifier decision lacks a complete definition"
        ) from exc
    try:
        validated_outputs = _validate_primary_output_inventory(
            tuple(item.class_id for item in settings.primary_classes),
            definition.output_classes,
            require_ignore_training=classifier.require_ignore_training,
        )
    except ClassifierCompatibilityError as exc:
        raise ROIAnalysisError("accepted primary definition outputs are incompatible") from exc
    if validated_outputs != configured_outputs:
        raise ROIAnalysisError(
            "accepted primary classifier outputs differ from configured authority"
        )
    return configured_outputs


def next_semantic_region_interaction(
    *,
    profiles: Sequence[object],
    evidence: Sequence[object],
    decisions: Sequence[object],
    tissue_generation_id: str,
    checkpoint_id: str,
    selected_candidate_roi_id: str | None = None,
) -> StageResult | None:
    """Return the next region review, or a retained v3 criterion review, in canonical order."""
    from mif_pipeline.roi_validation import (
        RuleOutcome,
        SemanticEvidenceSnapshot,
        SemanticProfileV3,
        SemanticRegionDecision,
    )
    from mif_pipeline.roi_validation.criteria import evaluate_criterion, parse_criterion

    if not isinstance(tissue_generation_id, str) or not tissue_generation_id.strip():
        raise ROIAnalysisError("semantic interaction requires a tissue generation")
    if not isinstance(checkpoint_id, str) or not checkpoint_id.strip():
        raise ROIAnalysisError("semantic interaction requires a checkpoint ID")
    profile_values = tuple(profiles)
    evidence_values = tuple(evidence)
    decision_values = tuple(decisions)
    if not all(isinstance(item, SemanticProfileV3) for item in profile_values):
        raise ROIAnalysisError("semantic interaction profiles are invalid")
    if not all(isinstance(item, SemanticEvidenceSnapshot) for item in evidence_values):
        raise ROIAnalysisError("semantic interaction evidence is invalid")
    if not all(isinstance(item, SemanticRegionDecision) for item in decision_values):
        raise ROIAnalysisError("semantic interaction decisions are invalid")
    profiles_by_structure = {item.structure_id: item for item in profile_values}
    if len(profiles_by_structure) != len(profile_values):
        raise ROIAnalysisError("semantic interaction profiles duplicate a structure")

    if any(profile.schema_version == "semantic-profile/v4" for profile in profile_values):
        if any(profile.schema_version != "semantic-profile/v4" for profile in profile_values):
            raise ROIAnalysisError("Revise the previous structure definitions before unified region review.")
        pending = []
        for region_index, snapshot in enumerate(sorted(evidence_values, key=lambda item: (item.analysis_domain_id, item.candidate_roi_id)), start=1):
            profile = profiles_by_structure.get(snapshot.structure_id)
            if profile is None:
                raise ROIAnalysisError("semantic evidence has no exact configured profile")
            if any(decision.matches_region(profile, snapshot) for decision in decision_values):
                continue
            pending.append((region_index, snapshot, profile))
        if selected_candidate_roi_id is not None:
            selected = [item for item in pending if item[1].candidate_roi_id == selected_candidate_roi_id]
            if len(selected) != 1:
                raise ROIAnalysisError("The selected region is no longer awaiting review.")
            current = selected[0]
        else:
            current = pending[0] if pending else None
        if current is not None:
            region_index, snapshot, profile = current
            def candidate_area(item):
                value = item[1].values.get("candidate_area")
                return float(value) if type(value) in {int, float} and value >= 0 else -1.0
            review_candidates = [
                {"roi_id": item.candidate_roi_id, "region_index": index,
                 "area_um2": item.values.get("candidate_area")}
                for index, item, _profile in sorted(pending, key=lambda item: (-candidate_area(item), item[0]))
            ]
            return StageResult.needs_interaction(
                stage="semantic_validation", slide_id=snapshot.slide_id,
                generation_id=tissue_generation_id,
                reason=InteractionReason.SEMANTIC_REGION_DECISION_REQUIRED,
                affected_object_ids=(snapshot.candidate_roi_id,),
                message="Review the image and selected measurements, then record your decision for this region.",
                allowed_actions=("accept_semantic_region", "reject_semantic_region", "select_semantic_region"),
                resume_context={
                    "schema_version": "semantic-region-interaction/v2",
                    "slide_id": snapshot.slide_id, "section_id": snapshot.section_id,
                    "analysis_domain_id": snapshot.analysis_domain_id,
                    "roi_id": snapshot.candidate_roi_id, "structure_id": snapshot.structure_id,
                    "profile_id": profile.profile_id, "profile_version": profile.profile_version,
                    "profile_content_sha256": profile.content_sha256,
                    "evidence_snapshot_sha256": snapshot.digest,
                    "geometry_sha256": snapshot.geometry_digest, "checkpoint_id": checkpoint_id,
                    "region_label": f"Region {region_index} of {len(evidence_values)}",
                    "review_candidates": review_candidates,
                    "declared_subtypes": [rule.class_id for rule in profile.classification_rules],
                    "semantic_evidence": _semantic_reviewer_evidence(profile, snapshot, decision_values),
                    "semantic_region_decisions": [decision.to_mapping() for decision in decision_values],
                }, checkpoint_id=checkpoint_id,
            )
        return None

    unresolved: list[tuple[str, str, str, object, object, object, str, str | None]] = []

    def manual_leaves(criterion: object) -> tuple[object, ...]:
        children = getattr(criterion, "children", ())
        if children:
            return tuple(leaf for child in children for leaf in manual_leaves(child))
        return (criterion,) if getattr(criterion, "kind", None) == "manual_region_decision" else ()

    for snapshot in evidence_values:
        profile = profiles_by_structure.get(snapshot.structure_id)
        if profile is None:
            raise ROIAnalysisError("semantic evidence has no exact configured profile")
        criterion = parse_criterion(profile.criteria)
        for leaf in manual_leaves(criterion):
            evaluated = evaluate_criterion(
                leaf,
                snapshot,
                decision_values,
                profile.profile_id,
                profile.profile_version,
                profile.content_sha256,
            )
            if evaluated.outcome is RuleOutcome.NEEDS_REVIEW:
                unresolved.append(
                    (
                        snapshot.analysis_domain_id,
                        snapshot.candidate_roi_id,
                        str(getattr(leaf, "criterion_id")),
                        snapshot,
                        profile,
                        leaf,
                        "acceptance",
                        None,
                    )
                )
        acceptance = evaluate_criterion(
            criterion,
            snapshot,
            decision_values,
            profile.profile_id,
            profile.profile_version,
            profile.content_sha256,
        )
        if acceptance.outcome is RuleOutcome.PASS:
            for rule in profile.classification_rules:
                class_criterion = parse_criterion(rule.criteria)
                for leaf in manual_leaves(class_criterion):
                    evaluated = evaluate_criterion(
                        leaf,
                        snapshot,
                        decision_values,
                        profile.profile_id,
                        profile.profile_version,
                        profile.content_sha256,
                    )
                    if evaluated.outcome is RuleOutcome.NEEDS_REVIEW:
                        unresolved.append(
                            (
                                snapshot.analysis_domain_id,
                                snapshot.candidate_roi_id,
                                str(getattr(leaf, "criterion_id")),
                                snapshot,
                                profile,
                                leaf,
                                "classification",
                                rule.class_id,
                            )
                        )
    if not unresolved:
        return None
    unresolved.sort(key=lambda item: item[:3])
    (
        domain_id,
        roi_id,
        criterion_id,
        snapshot,
        profile,
        criterion,
        criterion_scope,
        semantic_class_id,
    ) = unresolved[0]
    return StageResult.needs_interaction(
        stage="semantic_validation",
        slide_id=snapshot.slide_id,
        generation_id=tissue_generation_id,
        reason=InteractionReason.SEMANTIC_REGION_DECISION_REQUIRED,
        affected_object_ids=(roi_id,),
        message="Review one exact semantic criterion for this candidate ROI.",
        allowed_actions=(
            "accept_semantic_region",
            "reject_semantic_region",
        ),
        resume_context={
            "schema_version": "semantic-region-interaction/v1",
            "slide_id": snapshot.slide_id,
            "section_id": snapshot.section_id,
            "analysis_domain_id": domain_id,
            "roi_id": roi_id,
            "structure_id": snapshot.structure_id,
            "criterion_id": criterion_id,
            "criterion_scope": criterion_scope,
            "semantic_class_id": semantic_class_id,
            "prompt": getattr(criterion, "prompt", None),
            "profile_id": profile.profile_id,
            "profile_version": profile.profile_version,
            "profile_content_sha256": profile.content_sha256,
            "evidence_snapshot_sha256": snapshot.digest,
            "geometry_sha256": snapshot.geometry_digest,
            "checkpoint_id": checkpoint_id,
            "semantic_evidence": _semantic_reviewer_evidence(
                profile,
                snapshot,
                decision_values,
            ),
            "semantic_region_decisions": [decision.to_mapping() for decision in decision_values],
        },
        checkpoint_id=checkpoint_id,
    )


def _semantic_reviewer_evidence(
    profile: object,
    snapshot: object,
    decisions: Sequence[object],
) -> list[dict[str, object]]:
    """Return bounded, criterion-scoped evidence for one human review.

    This deliberately projects evaluated scalar criterion evidence rather than
    the snapshot's raw member inventories or measurement vectors.
    """
    from mif_pipeline.roi_validation import (
        RuleOutcome,
        SemanticEvidenceSnapshot,
        SemanticProfileV3,
    )
    from mif_pipeline.roi_validation.criteria import Criterion, evaluate_criterion, parse_criterion

    if not isinstance(profile, SemanticProfileV3) or not isinstance(
        snapshot, SemanticEvidenceSnapshot
    ):
        raise ROIAnalysisError("semantic reviewer evidence requires typed profile and snapshot")
    current = profile.schema_version == "semantic-profile/v4"
    def leaves(criterion: Criterion) -> tuple[Criterion, ...]:
        if not criterion.children:
            return (criterion,)
        return tuple(child for item in criterion.children for child in leaves(item))
    unavailable = snapshot.values.get("unavailable_evidence", {})
    if not isinstance(unavailable, Mapping):
        unavailable = {}
    root = None if profile.criteria is None else parse_criterion(profile.criteria, allow_observations=current)
    scoped_roots = [] if root is None else [("acceptance", None, root)]
    acceptance = None if root is None else evaluate_criterion(
        root, snapshot, decisions, profile.profile_id, profile.profile_version, profile.content_sha256,
    )
    if current or acceptance.outcome is RuleOutcome.PASS:
        scoped_roots.extend(("classification", rule.class_id,
            parse_criterion(rule.criteria, allow_observations=current))
            for rule in profile.classification_rules if rule.criteria is not None)
    rows: list[dict[str, object]] = []
    for criterion_scope, semantic_class_id, scoped_root in scoped_roots:
        for criterion in leaves(scoped_root):
            result = evaluate_criterion(
                criterion,
                snapshot,
                decisions,
                profile.profile_id,
                profile.profile_version,
                profile.content_sha256,
            )
            selector = dict(criterion.selector or {})
            key = (
                criterion.kind
                if not selector
                else criterion.kind
                + ":"
                + json.dumps(selector, sort_keys=True, separators=(",", ":"))
            )
            unavailable_value = unavailable.get(key)
            reason = (
                unavailable_value.get("reason")
                if isinstance(unavailable_value, Mapping)
                and isinstance(unavailable_value.get("reason"), str)
                else (result.unavailable_evidence[0] if result.unavailable_evidence else None)
            )
            rows.append(
                {
                    "criterion_id": criterion.criterion_id,
                    "kind": criterion.kind,
                    "required": criterion.required,
                    "prompt": criterion.prompt,
                    "selector": selector,
                    "value": result.observed_value,
                    "unit": result.unit,
                    "comparator": result.comparator,
                    "threshold": result.threshold,
                    "outcome": result.outcome.value,
                    "unavailable_reason": reason,
                    "criterion_scope": criterion_scope,
                    "semantic_class_id": semantic_class_id,
                    **({"subtype_id": semantic_class_id} if current else {}),
                    "calculation_inputs": _semantic_review_calculation_inputs(criterion, snapshot.values),
                }
            )
    return rows


def _semantic_review_calculation_inputs(criterion, values: Mapping[str, object]) -> dict[str, object]:
    """Expose recorded counts and area beside the evaluated fraction or density."""
    kind = criterion.kind
    selector = criterion.selector or {}
    recorded_inputs = values.get("calculation_inputs")
    if isinstance(recorded_inputs, Mapping):
        key = kind + ":" + json.dumps(dict(selector), sort_keys=True, separators=(",", ":"))
        counts = recorded_inputs.get(key)
        if isinstance(counts, Mapping):
            return dict(counts)
    inputs = {}
    if kind in {"phenotype_fraction", "phenotype_density"}:
        counts = values.get("phenotype_counts")
        if isinstance(counts, Mapping):
            inputs["selected_cell_count"] = counts.get(selector.get("phenotype_id"), 0)
    elif kind == "marker_state_fraction":
        counts = values.get("marker_state_counts")
        if isinstance(counts, Mapping):
            inputs["selected_cell_count"] = counts.get(
                f"{selector.get('marker_id')}:{selector.get('marker_state_id')}", 0,
            )
    elif kind in {"cell_density", "nucleus_density"}:
        key = "segmented_cell_count" if kind == "cell_density" else "segmented_nucleus_count"
        if key in values:
            inputs["selected_cell_count"] = values[key]
    if kind in {"phenotype_fraction", "marker_state_fraction"} and "segmented_cell_count" in values:
        inputs["member_cell_count"] = values["segmented_cell_count"]
    if kind in {"cell_density", "nucleus_density", "phenotype_density"} and "candidate_area" in values:
        inputs["area_um2"] = values["candidate_area"]
    return inputs


def validate_semantic_result_graph(
    rows: Sequence[object],
    *,
    analysis_domain_id: str,
    analysis_domain_parent_id: str,
    accepted_primary_roi_ids: Sequence[str] = (),
    profiles: Sequence[object] = (),
) -> tuple[Mapping[str, object], ...]:
    """Validate bounded result identity/ancestry before QuPath receives a request.

    The required mapping shape includes ``schema_version``, ``slide_id``, ``section_id``,
    ``analysis_domain_id``, ``tissue_generation_id``, ``pipeline_run_id``. Field checks are
    applied before the validated value is returned or passed to its next constructor."""
    from mif_pipeline.roi_validation import SemanticProfileV3, SemanticRegionDecision
    from mif_pipeline.roi_validation.criteria import parse_criterion

    if not isinstance(analysis_domain_id, str) or len(analysis_domain_id) != 64:
        raise ROIAnalysisError("semantic result graph analysis domain ID is invalid")
    if not isinstance(analysis_domain_parent_id, str) or not analysis_domain_parent_id:
        raise ROIAnalysisError("semantic result graph domain parent is invalid")
    primary_ids = set(accepted_primary_roi_ids)
    profile_values = tuple(profiles)
    if not all(isinstance(item, SemanticProfileV3) for item in profile_values):
        raise ROIAnalysisError("semantic result profiles are invalid")
    profiles_by_structure = {item.structure_id: item for item in profile_values}
    if len(profiles_by_structure) != len(profile_values):
        raise ROIAnalysisError("semantic result profiles duplicate a structure")
    values: list[Mapping[str, object]] = []
    required = {
        "schema_version",
        "slide_id",
        "section_id",
        "analysis_domain_id",
        "tissue_generation_id",
        "pipeline_run_id",
        "roi_id",
        "path_object_id",
        "structure_id",
        "parent_roi_id",
        "semantic_profile",
        "semantic_status",
        "semantic_rule_results",
        "review_status",
        "profile_id",
        "profile_version",
        "profile_content_sha256",
        "evidence_snapshot_sha256",
        "geometry_sha256",
        "semantic_classification_status",
        "semantic_class_id",
        "semantic_classification_rule_results",
    }

    def criterion_outcome(raw_result: object) -> str:
        criterion_fields = {
            "criterion_id",
            "outcome",
            "required",
            "observed_value",
            "unit",
            "comparator",
            "threshold",
            "evidence_digest",
            "unavailable_evidence",
        }
        if not isinstance(raw_result, Mapping) or set(raw_result) != criterion_fields:
            raise ROIAnalysisError("semantic criterion result fields are invalid")
        outcome = raw_result.get("outcome")
        unavailable = raw_result.get("unavailable_evidence")
        if (
            not isinstance(raw_result.get("criterion_id"), str)
            or not raw_result["criterion_id"]
            or outcome not in {"pass", "fail", "unavailable", "needs_review", "available"}
            or not isinstance(raw_result.get("required"), bool)
            or (
                raw_result.get("evidence_digest") is not None
                and not _is_sha256(raw_result.get("evidence_digest"))
            )
            or isinstance(unavailable, (str, bytes))
            or not isinstance(unavailable, Sequence)
            or any(not isinstance(item, str) or not item for item in unavailable)
        ):
            raise ROIAnalysisError("semantic criterion result is invalid")
        return str(outcome)

    def current_result(raw):
        if set(raw) != required | {"region_decision", "measurement_results"}:
            raise ROIAnalysisError("semantic-result/v3 fields are incomplete")
        try:
            decision = SemanticRegionDecision.from_mapping(raw["region_decision"])
        except (TypeError, ValueError) as exc:
            raise ROIAnalysisError("region review record is invalid") from exc
        exact = {"slide_id": decision.slide_id, "section_id": decision.section_id,
                 "analysis_domain_id": decision.analysis_domain_id, "roi_id": decision.candidate_roi_id,
                 "profile_id": decision.profile_id, "profile_version": decision.profile_version,
                 "profile_content_sha256": decision.profile_content_sha256,
                 "evidence_snapshot_sha256": decision.evidence_digest, "geometry_sha256": decision.geometry_digest,
                 "semantic_classification_status": decision.subtype_status, "semantic_class_id": decision.semantic_class_id,
                 "semantic_status": "accepted" if decision.action == "accept" else "rejected",
                 "review_status": "accepted" if decision.action == "accept" else "rejected"}
        if decision.schema_version != "semantic-region-decision/v2" or any(raw.get(k) != v for k,v in exact.items()):
            raise ROIAnalysisError("region result differs from its exact reviewer decision")
        if decision.analysis_domain_id != analysis_domain_id:
            raise ROIAnalysisError("region decision crosses its analysis domain")
        for field in ("path_object_id", "structure_id", "parent_roi_id", "tissue_generation_id", "pipeline_run_id"):
            if not isinstance(raw.get(field), str) or not raw[field].strip():
                raise ROIAnalysisError("region result identity is incomplete")
        if raw.get("semantic_profile") != f"{decision.profile_id}@{decision.profile_version}":
            raise ROIAnalysisError("region result profile identity differs")
        checks = raw["semantic_classification_rule_results"]
        if not isinstance(checks, (tuple, list)) or any(not isinstance(item, Mapping) or set(item) != {"class_id", "result"}
                or not isinstance(item["class_id"], str) or not item["class_id"].strip() for item in checks):
            raise ROIAnalysisError("region subtype measurement results are invalid")
        classes = [item["class_id"] for item in checks]
        if len(classes) != len(set(classes)):
            raise ROIAnalysisError("region subtype measurements duplicate a subtype")
        if decision.action == "accept":
            if not classes and decision.subtype_status != "not_configured":
                raise ROIAnalysisError("region subtype status differs from its declared subtypes")
            if classes and (decision.subtype_status not in {"assigned", "cannot_assess"}
                    or decision.subtype_status == "assigned" and decision.semantic_class_id not in classes):
                raise ROIAnalysisError("region assignment is not a declared subtype")
        for result in [raw["semantic_rule_results"], *(item["result"] for item in checks)]:
            if result is not None:
                outcome = criterion_outcome(result)
                if outcome == "needs_review" or result["evidence_digest"] != decision.evidence_digest:
                    raise ROIAnalysisError("region measurement evidence is unresolved or stale")
        measurements = raw["measurement_results"]
        measurement_fields = {"criterion_id", "kind", "required", "prompt", "selector", "value",
            "unit", "comparator", "threshold", "outcome", "unavailable_reason", "criterion_scope",
            "semantic_class_id", "subtype_id", "calculation_inputs"}
        if not isinstance(measurements, (tuple, list)) or any(
            not isinstance(item, Mapping) or set(item) != measurement_fields
            or not isinstance(item["criterion_id"], str) or not item["criterion_id"]
            or item["kind"] == "manual_region_decision" or item["prompt"] is not None
            or item["outcome"] not in {"available", "pass", "fail", "unavailable"}
            or item["subtype_id"] != item["semantic_class_id"]
            or item["subtype_id"] is not None and item["subtype_id"] not in classes
            for item in measurements
        ):
            raise ROIAnalysisError("selected measurement results are invalid")
        measurement_ids = [(item["subtype_id"], item["criterion_id"]) for item in measurements]
        if len(measurement_ids) != len(set(measurement_ids)):
            raise ROIAnalysisError("selected measurement results duplicate a condition")
        profile = profiles_by_structure.get(raw["structure_id"])
        if profiles_by_structure and profile is None:
            raise ROIAnalysisError("region result lacks its configured profile")
        if profile is not None:
            if (profile.schema_version != "semantic-profile/v4" or profile.content_sha256 != decision.profile_content_sha256
                    or profile.profile_id != decision.profile_id or profile.profile_version != decision.profile_version
                    or classes != [rule.class_id for rule in profile.classification_rules]):
                raise ROIAnalysisError("region result does not match its exact profile")
            def leaf_ids(node):
                if node.children:
                    return [identity for child in node.children for identity in leaf_ids(child)]
                return [node.criterion_id]
            selected_ids = [(subtype, identity)
                for subtype, condition in [(None, profile.criteria), *((rule.class_id, rule.criteria) for rule in profile.classification_rules)]
                if condition is not None
                for identity in leaf_ids(parse_criterion(condition, allow_observations=True))]
            if measurement_ids != selected_ids:
                raise ROIAnalysisError("selected measurement results differ from the configured selection")
            for condition, result in [(profile.criteria, raw["semantic_rule_results"]),
                    *((rule.criteria, item["result"]) for rule,item in zip(profile.classification_rules, checks))]:
                if (condition is None) != (result is None):
                    raise ROIAnalysisError("region measurements do not match the configured selection")
                if condition is not None:
                    root = parse_criterion(condition, allow_observations=True)
                    if result["criterion_id"] != (root.criterion_id or root.kind):
                        raise ROIAnalysisError("region measurement result root is invalid")

    for raw in rows:
        if isinstance(raw, Mapping) and raw.get("schema_version") == "semantic-result/v3":
            current_result(raw)
            values.append(raw)
            continue
        if not isinstance(raw, Mapping) or set(raw) != required:
            raise ROIAnalysisError("semantic-result/v2 row fields are incomplete")
        if raw.get("schema_version") != "semantic-result/v2":
            raise ROIAnalysisError("semantic result schema is invalid")
        if raw.get("analysis_domain_id") != analysis_domain_id:
            raise ROIAnalysisError("semantic result crosses its analysis domain")
        for field in ("profile_content_sha256", "evidence_snapshot_sha256", "geometry_sha256"):
            value = raw.get(field)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
            ):
                raise ROIAnalysisError(f"semantic result {field} is invalid")
        if raw.get("semantic_status") not in {"accepted", "rejected", "indeterminate"}:
            raise ROIAnalysisError("semantic result status is invalid")
        if raw.get("review_status") not in {"accepted", "rejected", "needs_review"}:
            raise ROIAnalysisError("semantic result review status is invalid")
        acceptance_outcome = criterion_outcome(raw.get("semantic_rule_results"))
        expected_acceptance = {
            "accepted": "pass",
            "rejected": "fail",
            "indeterminate": "unavailable",
        }[str(raw["semantic_status"])]
        if acceptance_outcome != expected_acceptance:
            raise ROIAnalysisError("semantic acceptance result is inconsistent")
        if raw["semantic_rule_results"].get("evidence_digest") != raw.get(
            "evidence_snapshot_sha256"
        ):
            raise ROIAnalysisError("semantic acceptance evidence binding is stale")
        classification_status = raw.get("semantic_classification_status")
        semantic_class_id = raw.get("semantic_class_id")
        rule_results = raw.get("semantic_classification_rule_results")
        if classification_status not in {
            "not_configured",
            "not_evaluated",
            "classified",
            "unclassified",
            "indeterminate",
        }:
            raise ROIAnalysisError("semantic classification status is invalid")
        if semantic_class_id is not None and (
            not isinstance(semantic_class_id, str) or not semantic_class_id.strip()
        ):
            raise ROIAnalysisError("semantic classification class ID is invalid")
        if isinstance(rule_results, (str, bytes)) or not isinstance(rule_results, Sequence):
            raise ROIAnalysisError("semantic classification rule results are invalid")
        if classification_status in {"not_configured", "not_evaluated"}:
            if semantic_class_id is not None or rule_results:
                raise ROIAnalysisError("semantic classification fields are inconsistent")
        elif classification_status == "classified":
            if semantic_class_id is None or not rule_results:
                raise ROIAnalysisError("classified semantic result is incomplete")
        elif semantic_class_id is not None or not rule_results:
            raise ROIAnalysisError("semantic classification fields are inconsistent")
        class_ids: list[str] = []
        class_outcomes: list[str] = []
        for item in rule_results:
            if (
                not isinstance(item, Mapping)
                or set(item) != {"class_id", "result"}
                or not isinstance(item.get("class_id"), str)
                or not str(item["class_id"]).strip()
                or not isinstance(item.get("result"), Mapping)
            ):
                raise ROIAnalysisError("semantic classification rule result is invalid")
            class_ids.append(str(item["class_id"]))
            class_outcomes.append(criterion_outcome(item["result"]))
            if item["result"].get("evidence_digest") != raw.get("evidence_snapshot_sha256"):
                raise ROIAnalysisError("semantic classification evidence binding is stale")
        if len(class_ids) != len(set(class_ids)):
            raise ROIAnalysisError("semantic classification rule IDs must be unique")
        pass_count = class_outcomes.count("pass")
        if pass_count >= 2 or "needs_review" in class_outcomes:
            raise ROIAnalysisError("semantic classification is unresolved or ambiguous")
        if classification_status == "not_configured":
            valid_classification = semantic_class_id is None and not rule_results
        elif classification_status == "not_evaluated":
            valid_classification = (
                raw.get("semantic_status") != "accepted"
                and semantic_class_id is None
                and not rule_results
            )
        elif classification_status == "classified":
            valid_classification = (
                raw.get("semantic_status") == "accepted"
                and pass_count == 1
                and "unavailable" not in class_outcomes
                and semantic_class_id == class_ids[class_outcomes.index("pass")]
            )
        elif classification_status == "unclassified":
            valid_classification = (
                raw.get("semantic_status") == "accepted"
                and semantic_class_id is None
                and bool(rule_results)
                and set(class_outcomes) == {"fail"}
            )
        else:
            valid_classification = (
                raw.get("semantic_status") == "accepted"
                and semantic_class_id is None
                and "unavailable" in class_outcomes
            )
        if not valid_classification:
            raise ROIAnalysisError("semantic classification fields are inconsistent")
        if profile_values:
            profile = profiles_by_structure.get(raw.get("structure_id"))
            if profile is None:
                raise ROIAnalysisError("semantic result has no exact configured profile")
            if (
                raw.get("profile_id") != profile.profile_id
                or raw.get("profile_version") != profile.profile_version
                or raw.get("semantic_profile") != profile.identifier
                or raw.get("profile_content_sha256") != profile.content_sha256
            ):
                raise ROIAnalysisError("semantic result profile binding is stale")
            acceptance_root = parse_criterion(profile.criteria)
            if raw["semantic_rule_results"].get("criterion_id") != (
                acceptance_root.criterion_id or acceptance_root.kind
            ):
                raise ROIAnalysisError("semantic acceptance rule result root is invalid")
            expected_class_ids = [rule.class_id for rule in profile.classification_rules]
            if not expected_class_ids:
                expected_status = "not_configured"
                expected_class_id = None
                expected_outcomes: list[str] = []
            elif raw.get("semantic_status") != "accepted":
                expected_status = "not_evaluated"
                expected_class_id = None
                expected_outcomes = []
            else:
                if class_ids != expected_class_ids:
                    raise ROIAnalysisError(
                        "semantic classification rule inventory or order is invalid"
                    )
                for item, rule in zip(rule_results, profile.classification_rules):
                    root = parse_criterion(rule.criteria)
                    expected_root_id = root.criterion_id or root.kind
                    if item["result"].get("criterion_id") != expected_root_id:
                        raise ROIAnalysisError(
                            "semantic classification rule result root is invalid"
                        )
                if "needs_review" in class_outcomes:
                    raise ROIAnalysisError(
                        "semantic classification needs review before publication"
                    )
                passed = [
                    class_id
                    for class_id, outcome in zip(class_ids, class_outcomes)
                    if outcome == "pass"
                ]
                if len(passed) >= 2:
                    raise ROIAnalysisError("ambiguous semantic classification cannot be published")
                if "unavailable" in class_outcomes:
                    expected_status = "indeterminate"
                    expected_class_id = None
                elif len(passed) == 1:
                    expected_status = "classified"
                    expected_class_id = passed[0]
                else:
                    expected_status = "unclassified"
                    expected_class_id = None
                expected_outcomes = class_outcomes
            if (
                classification_status != expected_status
                or semantic_class_id != expected_class_id
                or class_ids != expected_class_ids[: len(expected_outcomes)]
                or class_outcomes != expected_outcomes
            ):
                raise ROIAnalysisError("semantic classification does not match its exact profile")
        values.append(raw)
    by_id = {str(item["roi_id"]): item for item in values}
    if len(by_id) != len(values) or any(not key for key in by_id):
        raise ROIAnalysisError("semantic result ROI IDs must be nonblank and unique")
    scope_fields = (
        "slide_id",
        "section_id",
        "analysis_domain_id",
        "tissue_generation_id",
        "pipeline_run_id",
    )
    scopes = {tuple(item[field] for field in scope_fields) for item in values}
    if len(scopes) > 1:
        raise ROIAnalysisError("semantic results cross finalization scope")

    def active(item: Mapping[str, object]) -> bool:
        return item["semantic_status"] == "accepted" and item["review_status"] == "accepted"

    for roi_id, row in by_id.items():
        seen: set[str] = set()
        cursor = roi_id
        while cursor not in {analysis_domain_parent_id, *primary_ids}:
            if cursor in seen:
                raise ROIAnalysisError("semantic result graph contains a cycle")
            seen.add(cursor)
            node = by_id.get(cursor)
            if node is None:
                raise ROIAnalysisError("semantic result graph has an unknown ancestor")
            parent_id = node.get("parent_roi_id")
            if not isinstance(parent_id, str) or not parent_id:
                raise ROIAnalysisError("semantic result graph has a blank ancestor")
            if active(row) and cursor != roi_id and not active(node):
                raise ROIAnalysisError("active semantic result requires every accepted ancestor")
            cursor = parent_id
        if active(row):
            for ancestor_id in seen - {roi_id}:
                if not active(by_id[ancestor_id]):
                    raise ROIAnalysisError(
                        "active semantic result requires every accepted ancestor"
                    )
    return tuple(values)


def evaluate_finalized_secondary_count_policy(
    *,
    domain: object,
    final_inventory: Sequence[object],
    policies: Mapping[str, object],
    selection_contexts: Mapping[str, object] | None = None,
    manual_selections: Mapping[str, object] | None = None,
) -> object:
    """Pass only final semantically accepted inventories to count QC."""
    from mif_pipeline.compartment_analysis import (
        ObjectInventoryItem,
        StructureCountObservation,
        evaluate_final_secondary_counts,
    )

    grouped: dict[str, list[ObjectInventoryItem]] = {str(key): [] for key in policies}
    for raw in final_inventory:
        if not isinstance(raw, Mapping):
            raise ROIAnalysisError("final semantic inventory row is invalid")
        if raw.get("semantic_status") != "accepted" or raw.get("review_status") != "accepted":
            continue
        structure_id = raw.get("structure_id")
        if structure_id not in grouped:
            raise ROIAnalysisError("final semantic inventory references an unknown structure")
        grouped[str(structure_id)].append(
            ObjectInventoryItem(
                content_key=raw.get("content_key"),
                structure_id=str(structure_id),
                parent_content_key=raw.get("parent_content_key"),
                geometry_sha256=raw.get("geometry_sha256"),
            )
        )
    observations = tuple(
        StructureCountObservation(
            output_id=structure_id,
            count=len(items),
            inventory=tuple(items),
        )
        for structure_id, items in grouped.items()
    )
    return evaluate_final_secondary_counts(
        domain=domain,
        observations=observations,
        policies=policies,
        selection_contexts=selection_contexts,
        manual_selections=manual_selections,
    )


def _primary_count_decision(
    invocation: StageInvocation | CurrentImageScientificInvocation,
    roi_inventory: RoiInventoryRef,
    counts_by_class: Mapping[str, int],
) -> PrimaryCountDecision:
    """Bind table-derived biological counts to the accepted domain and settings.

    The classifier acceptance owner opens and validates the inventory once.
    Ignore rows remain in that inventory for audit but do not enter biological
    counts. Absent configured classes contribute zero so minima remain effective.
    """
    from mif_pipeline.compartment_analysis import evaluate_primary_counts
    from mif_pipeline.pipeline_core.config import StructureCountBounds

    revision = _accepted_analysis_domain_revision_evidence(invocation.state, invocation.analysis_domain_id)
    try:
        kind = AnalysisDomainKind(str(revision.get("kind")))
    except ValueError as exc:
        raise ROIAnalysisError("primary count domain kind is invalid") from exc
    domain = AnalysisDomainRef(
        analysis_domain_id=str(invocation.analysis_domain_id), kind=kind,
        slide_id=invocation.config.slide_id, section_id=str(invocation.section_id),
        tissue_generation_id=_required_generation_id(invocation.state),
    )
    settings, _plan_ref = _primary_settings_for_invocation(invocation)
    classes = tuple(item.class_id for item in settings.primary_classes)
    if set(counts_by_class) - {*classes, "Ignore"}:
        raise ROIAnalysisError("primary table contains a class outside accepted settings")
    return evaluate_primary_counts(
        domain=domain, roi_inventory_sha256=roi_inventory.sha256,
        counts_by_class={class_id: counts_by_class.get(class_id, 0) for class_id in classes},
        overall_policy=settings.primary_classifier.count_bounds,
        class_policies={class_id: settings.primary_classifier.class_count_bounds.get(
            class_id, StructureCountBounds()) for class_id in classes},
    )


@dataclass(frozen=True, slots=True)
class PrimaryClassifierDisposition:
    """Resolved whole-domain action over the exact observed primary decision."""

    decision: PrimaryCountDecision
    domain_status: str
    review_action: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.decision, PrimaryCountDecision):
            raise ROIAnalysisError("primary disposition requires a typed count decision")
        expected = "excluded_below_minimum" if self.decision.domain_exclusion is not None else "active"
        if self.review_action == "exclude_analysis_domain":
            expected = "excluded_by_primary_count_review"
        elif self.review_action not in {None, "accept_all_primary_structures"}:
            raise ROIAnalysisError("primary disposition review action is invalid")
        if self.domain_status != expected:
            raise ROIAnalysisError("primary disposition contradicts its decision or action")


def _finalize_primary_count_decision(
    invocation: CurrentImageScientificInvocation,
    decision: PrimaryCountDecision,
) -> PrimaryClassifierDisposition | StageResult:
    """Resolve the four review actions without changing the observed decision digest."""
    decision_mapping = decision.to_mapping()
    actions = {"accept_all_primary_structures", "exclude_analysis_domain", "review_classifier", "retrain_classifier"}
    if invocation.action in actions:
        prior = resolve_resume_envelope(invocation.action_context).service_context
        if prior.get("primary_count_decision_sha256") != decision_mapping["decision_sha256"]:
            raise ROIAnalysisError("primary count review decision changed before resume")
    if invocation.action == "exclude_analysis_domain":
        return PrimaryClassifierDisposition(decision, "excluded_by_primary_count_review", invocation.action)
    if any(item.status.value == "primary_review_required" for item in decision.outputs) and invocation.action != "accept_all_primary_structures":
        settings, _plan_ref = _primary_settings_for_invocation(invocation)
        return StageResult.needs_interaction(
            stage="primary_classifier", slide_id=invocation.config.slide_id,
            generation_id=_generation_id(invocation.state),
            reason=InteractionReason.PRIMARY_STRUCTURE_COUNT_REVIEW_REQUIRED,
            message="Primary classifier output exceeded a configured finite maximum. Review the complete population.",
            allowed_actions=("accept_all_primary_structures", "exclude_analysis_domain"),
            resume_context={
                "schema_version": "primary-count-review/v1",
                "analysis_domain_id": invocation.analysis_domain_id, "section_id": invocation.section_id,
                "primary_class_labels": {item.class_id: item.display_name for item in settings.primary_classes},
                **_primary_count_review_context(decision),
            },
            checkpoint_id="primary-count-review-" + str(decision_mapping["decision_sha256"])[:24],
        )
    return PrimaryClassifierDisposition(
        decision, "excluded_below_minimum" if decision.domain_exclusion is not None else "active",
        invocation.action if invocation.action == "accept_all_primary_structures" else None,
    )


def _canonical_artifact_bytes(payload: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            payload,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _publish_immutable_artifact(path: Path, content: bytes, *, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content:
            raise ROIAnalysisError(f"immutable {label} was replaced")
        return
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != content:
                raise ROIAnalysisError(f"{label} publication conflicted")
    finally:
        temporary.unlink(missing_ok=True)


def _primary_count_review_context(decision: PrimaryCountDecision) -> dict[str, object]:
    """Present bounded count summaries bound to the one authenticated ROI table."""
    return {
        "primary_count_decision_sha256": decision.to_mapping()["decision_sha256"],
        "roi_inventory_sha256": decision.roi_inventory_sha256,
        "primary_count_outputs": [item.to_mapping() for item in decision.outputs],
    }


def _attach_measurement_plan(
    invocation: StageInvocation | CurrentImageScientificInvocation,
    output: Mapping[str, Any],
    request: Mapping[str, object],
) -> Mapping[str, Any]:
    """Delegate one native measurement result to the public scientific owner."""
    if invocation.stage != "measurements":
        return output
    context = _native_measurement_validation_context(invocation, request)
    scientific_output = dict(output)
    validated_rows = None
    raw_descriptor = scientific_output.get("measurement_rows")
    if raw_descriptor is not None:
        try:
            descriptor = MarkerIntensityRowsDescriptor.from_mapping(raw_descriptor)
            marker_requests = tuple(
                item
                for item in context.feature_requests
                if item["kind"] == FeatureKind.EXPRESSION.value
            )
            expected = MeasurementRowStreamExpectation(
                project_entry_id=invocation.project_entry_id,
                slide_id=invocation.config.slide_id,
                section_id=context.section_id,
                segmentation_generation_id=str(request["generation_id"]),
                measurement_generation_id=str(request["generation_id"]),
                segmentation_population_id=context.segmentation_population_id,
                cell_dataset_sha256=request.get("segmentation_cell_dataset_sha256"),
                analysis_domain_bindings=context.analysis_domain_bindings,
                feature_ids=tuple(item["feature_id"] for item in marker_requests),
                measurement_names=tuple(item["measurement_name"] for item in marker_requests),
            )
            validated_rows = validate_measurement_row_stream(
                descriptor=descriptor,
                authenticated_output_root=Path(request.get("operational_output_dir", invocation.config.output_dir)),
                expected=expected,
                expected_base_population=(context.segmentation_cell_count,
                    context.segmentation_cell_ids_sha256,
                    context.segmentation_domain_cell_mapping_sha256),
            )
            context = replace(context,
                segmentation_cell_count=validated_rows.cell_count,
                segmentation_cell_ids_sha256=validated_rows.cell_ids_sha256,
                segmentation_domain_cell_mapping_sha256=validated_rows.cell_to_domain_membership_sha256)
        except (KeyError, TypeError, ROIAnalysisError) as exc:
            raise SectionBoundaryError("measurement row stream authentication failed") from exc
    validated = validate_measurement_result(scientific_output, context, validated_rows)
    enriched = dict(scientific_output)
    enriched["measurement_plan"] = validated.measurement_plan.to_mapping()
    enriched["selected_measurement_names"] = list(validated.selected_measurement_names)
    return enriched


def _native_measurement_validation_context(
    invocation: StageInvocation | CurrentImageScientificInvocation,
    request: Mapping[str, object],
) -> MeasurementResultValidationContext:
    section_id = invocation.section_id
    segmentation = invocation.state.get("segmentation")
    scoped = (
        segmentation.get(section_id)
        if isinstance(segmentation, Mapping) and section_id is not None
        else None
    )
    population = scoped.get("segmentation_population") if isinstance(scoped, Mapping) else None
    active_domains = (
        population.get("active_domain_revisions") if isinstance(population, Mapping) else None
    )
    requests = request.get("feature_requests")
    if (
        section_id is None
        or not isinstance(scoped, Mapping)
        or not isinstance(active_domains, Sequence)
        or isinstance(active_domains, (str, bytes, bytearray))
        or not isinstance(requests, Sequence)
        or isinstance(requests, (str, bytes, bytearray))
        or request.get("segmentation_population_id") != scoped.get("segmentation_population_id")
    ):
        raise SectionBoundaryError(
            "measurement validation lacks exact segmentation scientific inputs"
        )
    try:
        bindings = tuple(
            sorted(
                (
                    {
                        "analysis_domain_id": item["analysis_domain_id"],
                        "analysis_domain_revision_sha256": item["content_sha256"],
                    }
                    for item in active_domains
                    if isinstance(item, Mapping)
                ),
                key=lambda item: item["analysis_domain_id"],
            )
        )
        if len(bindings) != len(active_domains):
            raise SectionBoundaryError("measurement validation domain bindings are incomplete")
        return MeasurementResultValidationContext(
            section_id=section_id,
            segmentation_population_id=scoped.get("segmentation_population_id"),
            segmentation_cell_count=scoped.get("cell_count"),
            segmentation_cell_ids_sha256=scoped.get("cell_ids_sha256"),
            segmentation_domain_cell_mapping_sha256=scoped.get("domain_cell_mapping_sha256"),
            analysis_domain_bindings=bindings,
            canonical_channels=tuple(
                item.to_mapping() for item in _measurement_request_settings(invocation)[0]
            ),
            feature_requests=tuple(requests),
        )
    except (KeyError, TypeError, ROIAnalysisError) as exc:
        raise SectionBoundaryError(
            "measurement validation lacks exact segmentation scientific inputs"
        ) from exc


def _accepted_analysis_domain_scopes(
    state: Mapping[str, Any],
) -> tuple[dict[str, object], ...]:
    output = state.get("analysis_domain_selection")
    if not isinstance(output, Mapping):
        raise ROIAnalysisError(
            "accepted analysis-domain checkpoint is required before classification"
        )
    if (
        output.get("selection_status") != "accepted"
        or output.get("selected_annotations_locked") is not True
    ):
        raise ROIAnalysisError("classifier invocations require accepted locked analysis domains")
    manifests = output.get("analysis_domain_manifests")
    if isinstance(manifests, (str, bytes)) or not isinstance(manifests, Sequence):
        raise ROIAnalysisError("analysis-domain manifest inventory is invalid")
    scopes: list[dict[str, object]] = []
    for manifest in manifests:
        if not isinstance(manifest, Mapping):
            raise ROIAnalysisError("analysis-domain manifest must be a mapping")
        revisions = manifest.get("revisions")
        if (
            isinstance(revisions, (str, bytes))
            or not isinstance(revisions, Sequence)
            or not revisions
            or not isinstance(revisions[-1], Mapping)
        ):
            raise ROIAnalysisError("analysis-domain revision inventory is invalid")
        latest = revisions[-1]
        domain_id = latest.get("analysis_domain_id")
        section_id = latest.get("section_id")
        revision = latest.get("revision")
        revision_sha256 = latest.get("content_sha256")
        path_object_id = latest.get("source_path_object_id")
        if (
            not isinstance(domain_id, str)
            or len(domain_id) != 64
            or any(character not in "0123456789abcdef" for character in domain_id)
            or not isinstance(section_id, str)
            or not section_id.strip()
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
            or not isinstance(revision_sha256, str)
            or len(revision_sha256) != 64
            or any(character not in "0123456789abcdef" for character in revision_sha256)
            or not isinstance(path_object_id, str)
            or not path_object_id.strip()
        ):
            raise ROIAnalysisError("analysis-domain classifier scope lacks exact revision identity")
        scopes.append(
            {
                "section_id": section_id,
                "analysis_domain_id": domain_id,
                "analysis_domain_revision": revision,
                "analysis_domain_revision_sha256": revision_sha256,
                "analysis_domain_path_object_id": path_object_id,
            }
        )
    if not scopes:
        raise ROIAnalysisError("analysis-domain classifier scope is empty")
    scopes.sort(key=lambda item: str(item["analysis_domain_id"]))
    domain_ids = tuple(str(item["analysis_domain_id"]) for item in scopes)
    if len(set(domain_ids)) != len(domain_ids):
        raise ROIAnalysisError("analysis-domain classifier scope is duplicated")
    recorded = state.get("analysis_domain_ids")
    if recorded is not None and tuple(sorted(recorded)) != domain_ids:
        raise ROIAnalysisError("analysis-domain classifier scope changed after acceptance")
    if output.get("domain_set_sha256") != state.get(
        "domain_set_digest", output.get("domain_set_sha256")
    ):
        raise ROIAnalysisError("analysis-domain set digest changed after acceptance")
    return tuple(scopes)


def _active_segmentation_domain_revisions(
    domain_output: Mapping[str, Any], section_id: str
) -> tuple[dict[str, object], ...]:
    """Return the complete accepted domain-revision authority for one section."""
    if not isinstance(section_id, str) or not section_id.strip():
        raise ROIAnalysisError("segmentation domain scope requires a section ID")
    scopes = _accepted_analysis_domain_scopes(
        {
            "analysis_domain_selection": domain_output,
            "domain_set_digest": domain_output.get("domain_set_sha256"),
        }
    )
    manifests = domain_output.get("analysis_domain_manifests")
    assert isinstance(manifests, Sequence) and not isinstance(manifests, (str, bytes))
    by_domain_id: dict[str, dict[str, object]] = {}
    for manifest in manifests:
        assert isinstance(manifest, Mapping)
        revisions = manifest.get("revisions")
        assert isinstance(revisions, Sequence) and not isinstance(revisions, (str, bytes))
        assert revisions and isinstance(revisions[-1], Mapping)
        latest = revisions[-1]
        if latest.get("section_id") != section_id:
            continue
        domain_id = latest.get("analysis_domain_id")
        kind = latest.get("kind")
        if kind not in {"selected_roi", "whole_tissue_section"}:
            raise ROIAnalysisError("segmentation domain kind is invalid")
        if not isinstance(domain_id, str) or domain_id in by_domain_id:
            raise ROIAnalysisError("segmentation domain revision identity is duplicated")
        by_domain_id[domain_id] = _mutable_json(latest)
    expected_ids = {
        scope["analysis_domain_id"] for scope in scopes if scope["section_id"] == section_id
    }
    if set(by_domain_id) != expected_ids or not by_domain_id:
        raise ROIAnalysisError("segmentation active-domain revisions do not match accepted scope")
    for scope in scopes:
        domain_id = scope["analysis_domain_id"]
        if scope["section_id"] != section_id:
            continue
        revision = by_domain_id[domain_id]
        if any(
            revision.get(field) != scope[expected]
            for field, expected in (
                ("analysis_domain_id", "analysis_domain_id"),
                ("revision", "analysis_domain_revision"),
                ("content_sha256", "analysis_domain_revision_sha256"),
                ("source_path_object_id", "analysis_domain_path_object_id"),
            )
        ):
            raise ROIAnalysisError("segmentation domain revision differs from accepted scope")
    return tuple(by_domain_id[key] for key in sorted(by_domain_id))


def _authenticate_global_segmentation_domain_manifests(
    domain_output: Mapping[str, Any],
    state: Mapping[str, Any],
    *,
    slide_id: str,
    project_entry_id: str,
) -> tuple[AnalysisDomainManifest, ...]:
    """Authenticate the complete set before a section is projected.

    The section projection is deliberately derived only after every immutable history in the
    accepted global manifest set has been parsed with 's null-preserving canonical records.
    A local latest-revision list cannot prove the global-set commitment and is therefore
    never accepted here."""
    if domain_output.get("selection_status") != "accepted":
        raise ROIAnalysisError("segmentation requires accepted analysis-domain authority")
    raw_manifests = domain_output.get("analysis_domain_manifests")
    if (
        isinstance(raw_manifests, (str, bytes))
        or not isinstance(raw_manifests, Sequence)
        or not raw_manifests
    ):
        raise ROIAnalysisError("segmentation requires complete Task 6 manifests")
    accepted = {item["section_id"]: item for item in _accepted_tissue_section_records(state)}
    canonical_image_id, canonical_image_sha256 = _canonical_identity(state)
    if not isinstance(project_entry_id, str) or not project_entry_id.strip():
        raise ROIAnalysisError("segmentation requires the exact canonical project entry")
    manifests: list[AnalysisDomainManifest] = []
    seen_sources: set[tuple[str, str]] = set()
    seen_content: set[str] = set()
    for raw in raw_manifests:
        manifest = _domain_manifest_from_mapping(raw)
        # This closes the wire shape as well as reserializing the immutable
        # content with the producer policy.
        if not isinstance(raw, Mapping) or dict(raw) != _domain_manifest_mapping(manifest):
            raise ROIAnalysisError("analysis-domain manifest wire shape changed")
        section = accepted.get(manifest.section_id)
        if section is None or any(
            actual != expected
            for actual, expected in (
                (manifest.canonical_image_id, canonical_image_id),
                (manifest.canonical_image_sha256, canonical_image_sha256),
                (manifest.project_entry_id, project_entry_id),
                (manifest.slide_id, slide_id),
                (manifest.tissue_generation_id, section["tissue_generation_id"]),
                (manifest.tissue_roi_id, section["tissue_roi_id"]),
                (manifest.tissue_path_object_id, section["tissue_path_object_id"]),
            )
        ):
            raise ROIAnalysisError("analysis-domain manifest is outside accepted tissue")
        history: list[AnalysisDomainRevision] = list(manifest.revisions)
        first = history[0]
        source_key = (manifest.section_id, first.source_path_object_id)
        if source_key in seen_sources or manifest.content_sha256 in seen_content:
            raise ROIAnalysisError("analysis-domain manifest inventory is duplicated")
        for ordinal, revision in enumerate(history, start=1):
            predecessor = None if ordinal == 1 else history[ordinal - 2].content_sha256
            if (
                revision.analysis_domain_id != first.analysis_domain_id
                or revision.ref.kind != first.ref.kind
                or revision.source_path_object_id != first.source_path_object_id
                or revision.revision != ordinal
                or revision.predecessor_sha256 != predecessor
            ):
                raise ROIAnalysisError("analysis-domain manifest revision history is not ordered")
        seen_sources.add(source_key)
        seen_content.add(manifest.content_sha256)
        manifests.append(manifest)
    if domain_output.get("domain_set_sha256") != _domain_set_sha256(manifests):
        raise ROIAnalysisError("analysis-domain global set digest changed")
    return tuple(manifests)


def _active_segmentation_primary_outputs(
    state: Mapping[str, Any], section_id: str
) -> tuple[dict[str, object], ...]:
    """Bind configured-union scope to every exact active domain primary output."""
    domain_output = state.get("analysis_domain_selection")
    if not isinstance(domain_output, Mapping):
        raise ROIAnalysisError("configured union requires accepted analysis domains")
    domains = _active_segmentation_domain_revisions(domain_output, section_id)
    primary = state.get("primary_classifier")
    section_scope = primary.get(section_id) if isinstance(primary, Mapping) else None
    domain_outputs = (
        section_scope.get("analysis_domains") if isinstance(section_scope, Mapping) else None
    )
    if not isinstance(domain_outputs, Mapping):
        raise ROIAnalysisError(
            "configured union requires current section primary-classifier outputs"
        )
    domain_set_sha256 = domain_output.get("domain_set_sha256")
    outputs: list[dict[str, object]] = []
    for domain in domains:
        domain_id = domain["analysis_domain_id"]
        stored = domain_outputs.get(domain_id)
        output = stored.get("primary") if isinstance(stored, Mapping) else None
        expected = {
            "analysis_domain_id": domain_id,
            "analysis_domain_revision": domain["revision"],
            "analysis_domain_revision_sha256": domain["content_sha256"],
            "analysis_domain_path_object_id": domain["source_path_object_id"],
            "domain_set_sha256": domain_set_sha256,
            "section_id": section_id,
        }
        if not isinstance(output, Mapping) or any(
            output.get(field) != value for field, value in expected.items()
        ):
            raise ROIAnalysisError(
                "configured union primary output differs from active domain authority"
            )
        try:
            inventory = parse_roi_inventory_ref(output.get("roi_inventory"))
        except RoiInventoryError as exc:
            raise ROIAnalysisError("segmentation primary ROI table reference is invalid") from exc
        disposition = output.get("domain_status")
        producer = output.get("producer_attempt_id")
        if disposition not in {"active", "excluded_below_minimum", "excluded_by_primary_count_review"} or not isinstance(producer, str) or not producer:
            raise ROIAnalysisError("segmentation primary disposition or table owner is invalid")
        outputs.append({**expected, "roi_inventory": inventory.to_mapping(),
                        "domain_status": disposition, "producer_attempt_id": producer})
    return tuple(outputs)


def _accepted_analysis_domain_revision_evidence(
    state: Mapping[str, Any], analysis_domain_id: str | None
) -> Mapping[str, Any]:
    output = state.get("analysis_domain_selection")
    manifests = output.get("analysis_domain_manifests") if isinstance(output, Mapping) else None
    if isinstance(manifests, (str, bytes)) or not isinstance(manifests, Sequence):
        raise ROIAnalysisError("analysis-domain revision evidence is missing")
    matches: list[Mapping[str, Any]] = []
    for manifest in manifests:
        revisions = manifest.get("revisions") if isinstance(manifest, Mapping) else None
        if (
            isinstance(revisions, Sequence)
            and not isinstance(revisions, (str, bytes))
            and revisions
            and isinstance(revisions[-1], Mapping)
            and revisions[-1].get("analysis_domain_id") == analysis_domain_id
        ):
            matches.append(revisions[-1])
    if len(matches) != 1:
        raise ROIAnalysisError("exact accepted analysis-domain revision evidence is ambiguous")
    return matches[0]


def _accepted_sections(state: Mapping[str, Any]) -> tuple[str, ...]:
    acceptance = state.get("tissue_acceptance")
    if not isinstance(acceptance, Mapping):
        raise ROIAnalysisError("accepted tissue checkpoint is required before section analysis")
    sections = acceptance.get("sections")
    if not isinstance(sections, Sequence) or isinstance(sections, (str, bytes)):
        generation = acceptance.get("generation")
        sections = generation.get("sections") if isinstance(generation, Mapping) else None
    if not isinstance(sections, Sequence) or isinstance(sections, (str, bytes)):
        raise ROIAnalysisError("accepted tissue output requires sections")
    identifiers: list[str] = []
    for item in sections:
        if not isinstance(item, Mapping):
            raise ROIAnalysisError("accepted tissue section must be a mapping")
        section_id = item.get("section_id")
        if not isinstance(section_id, str) or not section_id.strip():
            raise ROIAnalysisError("accepted tissue section requires section_id")
        identifiers.append(section_id)
    if not identifiers or len(set(identifiers)) != len(identifiers):
        raise ROIAnalysisError("accepted tissue section IDs must be non-empty and unique")
    return tuple(identifiers)


def _canonical_identity(state: Mapping[str, Any]) -> tuple[str, str]:
    preflight = state.get("preflight")
    if not isinstance(preflight, Mapping):
        raise ROIAnalysisError("preflight output is required for checkpoint identity")
    canonical_id = preflight.get("canonical_image_id")
    canonical_sha = preflight.get("canonical_image_sha256")
    if not isinstance(canonical_id, str) or not canonical_id.strip():
        raise ROIAnalysisError("preflight output lacks canonical_image_id")
    if (
        not isinstance(canonical_sha, str)
        or len(canonical_sha) != 64
        or any(character not in "0123456789abcdef" for character in canonical_sha)
    ):
        raise ROIAnalysisError("preflight output lacks canonical_image_sha256")
    return canonical_id, canonical_sha


def _optional_canonical_identity(
    state: Mapping[str, Any],
) -> dict[str, str]:
    if not isinstance(state.get("preflight"), Mapping):
        return {}
    canonical_id, canonical_sha = _canonical_identity(state)
    return {
        "canonical_image_id": canonical_id,
        "canonical_image_sha256": canonical_sha,
    }


def _generation_ids(state: Mapping[str, Any]) -> tuple[str, ...]:
    for stage in ("tissue_acceptance", "tissue_candidate"):
        output = state.get(stage)
        if isinstance(output, Mapping):
            generation_id = output.get("generation_id")
            if isinstance(generation_id, str) and generation_id.strip():
                return (generation_id,)
            generation = output.get("generation")
            if isinstance(generation, Mapping):
                generation_id = generation.get("generation_id")
                if isinstance(generation_id, str) and generation_id.strip():
                    return (generation_id,)
    return ()


def _generation_id(state: Mapping[str, Any]) -> str | None:
    values = _generation_ids(state)
    return values[0] if values else None


def validate_segmentation_population_result(
    output: Mapping[str, Any],
    context: SegmentationResultValidationContext,
) -> None:
    """Validate the closed segmentation-population/v2 scientific result."""

    if not isinstance(output, Mapping) or type(context) is not SegmentationResultValidationContext:
        raise SectionBoundaryError("segmentation result validation inputs are invalid")
    _validate_strict_segmentation_population_output(output, context)


def _validate_strict_segmentation_population_output(
    output: Mapping[str, Any], context: SegmentationResultValidationContext
) -> None:
    """Bind compact population lineage to the completed canonical dataset."""
    state = context._scientific_state()
    top_fields = {
        "cell_dataset",
        "cell_ids_sha256",
        "cell_count",
        "section_id",
        "segmentation_passes",
        "segmentation_reused_without_recomputation",
        "segmentation_skipped_excluded_domains",
        "segmentation_population_id",
        "segmentation_config_sha256",
        "segmentation_domain_sha256",
        "domain_set_sha256",
        "union_geometry_sha256",
        "domain_cell_inventories",
        "domain_cell_counts",
        "domain_cell_mapping_sha256",
        "cell_evidence_sha256",
        "section_population_sha256",
        "segmentation_population",
        "segmentation_status",
        "tiles_processed",
        "pixels_processed",
        "coordinate_system",
    }
    population_fields = {
        "cell_dataset",
        "schema_version",
        "segmentation_population_id",
        "canonical_image_id",
        "canonical_image_sha256",
        "slide_id",
        "tissue_generation_id",
        "section_id",
        "tissue_roi_id",
        "segmentation_config_sha256",
        "segmentation_domain_sha256",
        "domain_set_sha256",
        "union_geometry_sha256",
        "active_domain_revisions",
        "domain_cell_inventories",
        "domain_cell_mapping_sha256",
        "cell_evidence_sha256",
        "section_population_sha256",
    }
    if set(output) != top_fields:
        raise SectionBoundaryError("segmentation output v2 keys are not closed")
    population = output.get("segmentation_population")
    if not isinstance(population, dict) or set(population) != population_fields:
        raise SectionBoundaryError("segmentation population v2 keys are not closed")
    domain_output = state["analysis_domain_selection"]
    try:
        _authenticate_global_segmentation_domain_manifests(
            domain_output,
            state,
            slide_id=context.slide_id,
            project_entry_id=context.project_entry_id,
        )
        domains = _active_segmentation_domain_revisions(domain_output, context.section_id)
    except ROIAnalysisError as exc:
        raise SectionBoundaryError(
            "segmentation output has unauthenticated Task 6 domain authority"
        ) from exc
    expected_domains = {item["analysis_domain_id"]: item for item in domains}
    domain_identity = [
        {
            "analysis_domain_id": item["analysis_domain_id"],
            "kind": item["kind"],
            "revision": item["revision"],
            "content_sha256": item["content_sha256"],
            "source_path_object_id": item["source_path_object_id"],
            "geometry_sha256": item["geometry_sha256"],
        }
        for item in domains
    ]
    section_records = {item["section_id"]: item for item in _accepted_tissue_section_records(state)}
    section_record = section_records.get(context.section_id)
    if section_record is None:
        raise SectionBoundaryError("segmentation section is not accepted tissue")
    canonical_image_id, canonical_image_sha256 = _canonical_identity(state)
    segmentation_config = context.segmentation_config
    config_mapping = dict(segmentation_config)
    if config_mapping.get("method") in {"cellpose", "stardist"}:
        from mif_pipeline.pipeline_core.config import SegmentationConfig
        config_mapping = SegmentationConfig.from_mapping(segmentation_config, field="segmentation validation").to_mapping()
    segmentation_config_sha256 = hashlib.sha256(
        canonical_json_bytes(config_mapping)
    ).hexdigest()
    scope = segmentation_config.get("scope")
    if scope not in {"accepted_tissue", "configured_union"}:
        raise SectionBoundaryError("segmentation scope is invalid")
    union_geometry_sha256 = output.get("union_geometry_sha256")
    if not _is_sha256(union_geometry_sha256):
        raise SectionBoundaryError("segmentation union geometry digest is invalid")
    segmentation_domain_sha256 = _mapping_sha256(
        {
            "scope": scope,
            "domain_set_sha256": domain_output.get("domain_set_sha256"),
            "active_domain_revisions": domain_identity,
            "union_geometry_sha256": union_geometry_sha256,
            **({"segmentation_settings_revision_sha256": context.segmentation_settings_revision_sha256}
               if context.segmentation_settings_revision_sha256 is not None else {}),
        }
    )
    population_id = "segmentation-population/v2/" + _mapping_sha256(
        {
            "schema_version": "segmentation-population/v2",
            "canonical_image_id": canonical_image_id,
            "canonical_image_sha256": canonical_image_sha256,
            "slide_id": context.slide_id,
            "tissue_generation_id": section_record["tissue_generation_id"],
            "section_id": context.section_id,
            "tissue_roi_id": section_record["tissue_roi_id"],
            "segmentation_config_sha256": segmentation_config_sha256,
            "segmentation_domain_sha256": segmentation_domain_sha256,
        }
    )
    expected_identity = {
        "schema_version": "segmentation-population/v2",
        "segmentation_population_id": population_id,
        "canonical_image_id": canonical_image_id,
        "canonical_image_sha256": canonical_image_sha256,
        "slide_id": context.slide_id,
        "tissue_generation_id": section_record["tissue_generation_id"],
        "section_id": context.section_id,
        "tissue_roi_id": section_record["tissue_roi_id"],
        "segmentation_config_sha256": segmentation_config_sha256,
        "segmentation_domain_sha256": segmentation_domain_sha256,
        "domain_set_sha256": domain_output.get("domain_set_sha256"),
        "union_geometry_sha256": union_geometry_sha256,
        "active_domain_revisions": domain_identity,
    }
    if any(population.get(key) != value for key, value in expected_identity.items()):
        raise SectionBoundaryError("segmentation population lineage is stale")
    if any(
        output.get(key) != population.get(key)
        for key in (
            "segmentation_population_id",
            "segmentation_config_sha256",
            "segmentation_domain_sha256",
            "domain_set_sha256",
            "union_geometry_sha256",
        )
    ):
        raise SectionBoundaryError("segmentation top-level lineage differs from population")
    from mif_pipeline.datasets.references import read_cell_dataset_reference

    reference = output.get("cell_dataset")
    try:
        description = read_cell_dataset_reference(reference)
    except (OSError, TypeError, ValueError, KeyError) as exc:
        raise SectionBoundaryError("segmentation cell dataset is unavailable or changed") from exc
    dataset_scope = description.get("scope", {})
    if any(dataset_scope.get(key) != value for key, value in {
        "section_id": context.section_id,
        "segmentation_population_id": population_id,
        "canonical_image_id": canonical_image_id,
        "canonical_image_sha256": canonical_image_sha256,
    }.items()):
        raise SectionBoundaryError("segmentation cell dataset belongs to another population")
    cell_count = description["row_count"]
    if (type(cell_count) is not int or cell_count < 0
            or type(output.get("cell_count")) is not int
            or output["cell_count"] != cell_count
            or output.get("cell_ids_sha256") != description.get("cell_ids_sha256")
            or not _is_sha256(description.get("cell_ids_sha256"))):
        raise SectionBoundaryError("segmentation cell dataset count or ID digest differs")
    inventories = output.get("domain_cell_inventories")
    summaries = description.get("domain_inventories")
    if not isinstance(summaries, dict) or set(summaries) != set(expected_domains):
        raise SectionBoundaryError("segmentation dataset domain coverage differs")
    expected_inventories = []
    expected_counts = {}
    for domain_id in sorted(expected_domains):
        domain = expected_domains[domain_id]
        summary = summaries[domain_id]
        if (not isinstance(summary, dict) or type(summary.get("cell_count")) is not int
                or summary["cell_count"] < 0 or not _is_sha256(summary.get("cell_ids_sha256"))):
            raise SectionBoundaryError("segmentation dataset domain summary is malformed")
        expected_inventories.append({
            **{key: domain[key] for key in ("analysis_domain_id", "kind", "revision", "content_sha256")},
            "cell_count": summary["cell_count"], "cell_ids_sha256": summary["cell_ids_sha256"],
        })
        expected_counts[domain_id] = summary["cell_count"]
        if context.primary_domain_statuses[domain_id] != "active" and summary["cell_count"] != 0:
            raise SectionBoundaryError("excluded primary domain contains active segmented cells")
    if (inventories != expected_inventories or output.get("domain_cell_counts") != expected_counts
            or sum(expected_counts.values()) != cell_count):
        raise SectionBoundaryError("segmentation compact domain inventories differ")
    mapping_sha256 = description.get("domain_cell_mapping_sha256")
    evidence_sha256 = reference["sha256"]
    if not _is_sha256(mapping_sha256) or any(
        output.get(key) != value or population.get(key) != value
        for key, value in {
            "cell_dataset": reference,
            "domain_cell_inventories": inventories,
            "domain_cell_mapping_sha256": mapping_sha256,
            "cell_evidence_sha256": evidence_sha256,
        }.items()
    ):
        raise SectionBoundaryError("segmentation dataset identity or domain mapping differs")
    section_population_sha256 = _mapping_sha256(
        {
            "segmentation_population_id": population_id,
            "cell_dataset": dict(reference),
            "domain_cell_inventories": list(inventories),
            "domain_cell_mapping_sha256": mapping_sha256,
            "cell_evidence_sha256": evidence_sha256,
        }
    )
    if (
        output.get("section_population_sha256") != section_population_sha256
        or population.get("section_population_sha256") != section_population_sha256
        or output.get("section_id") != context.section_id
        or output.get("coordinate_system") != "global_level0"
        or output.get("segmentation_status")
        != ("completed_zero_cells" if not cell_count else "completed_with_cells")
        or type(output.get("segmentation_skipped_excluded_domains")) is not bool
        or output.get("segmentation_skipped_excluded_domains") != all(
            status != "active" for status in context.primary_domain_statuses.values()
        )
        or (output.get("segmentation_skipped_excluded_domains") and cell_count != 0)
        or (output.get("segmentation_skipped_excluded_domains") and output.get("segmentation_reused_without_recomputation"))
        or not isinstance(output.get("segmentation_reused_without_recomputation"), bool)
        or not isinstance(output.get("segmentation_passes"), int)
        or isinstance(output.get("segmentation_passes"), bool)
        or output.get("segmentation_passes") not in {0, 1}
        or not isinstance(output.get("tiles_processed"), int)
        or isinstance(output.get("tiles_processed"), bool)
        or not isinstance(output.get("pixels_processed"), int)
        or isinstance(output.get("pixels_processed"), bool)
        or output.get("tiles_processed") < 0
        or output.get("pixels_processed") < 0
        or (
            output.get("segmentation_passes") == 0
            and (output.get("tiles_processed") != 0 or output.get("pixels_processed") != 0)
        )
        or (
            output.get("segmentation_passes") == 1
            and (output.get("tiles_processed") <= 0 or output.get("pixels_processed") <= 0)
        )
        or (output.get("segmentation_passes") == 0)
        != (output.get("segmentation_reused_without_recomputation") or output.get("segmentation_skipped_excluded_domains"))
    ):
        raise SectionBoundaryError("segmentation population completion evidence is invalid")


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def validate_classifier_output_scope(
    output: Mapping[str, Any],
    invocation: StageInvocation | CurrentImageScientificInvocation,
) -> None:
    """Validate one classifier result against its exact biological scope.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""
    if invocation.stage not in {"primary_classifier", "secondary_classifier"}:
        return
    domain_output = invocation.state.get("analysis_domain_selection")
    domain_set_sha256 = (
        domain_output.get("domain_set_sha256") if isinstance(domain_output, Mapping) else None
    )
    expected = {
        "section_id": invocation.section_id,
        "analysis_domain_id": invocation.analysis_domain_id,
        "analysis_domain_revision": invocation.analysis_domain_revision,
        "analysis_domain_revision_sha256": (invocation.analysis_domain_revision_sha256),
        "analysis_domain_path_object_id": (invocation.analysis_domain_path_object_id),
        "domain_set_sha256": domain_set_sha256,
    }
    if any(output.get(name) != value for name, value in expected.items()):
        raise SectionBoundaryError("classifier output changed its exact analysis-domain identity")
    try:
        inventory = parse_roi_inventory_ref(output.get("roi_inventory"))
    except RoiInventoryError as exc:
        raise SectionBoundaryError("classifier ROI table reference is invalid") from exc
    if type(output.get("object_count")) is not int or output["object_count"] != inventory.row_count:
        raise SectionBoundaryError("classifier object count differs from its ROI table")
    if invocation.stage == "secondary_classifier":
        from .batch_classifier_inference_artifacts import parse_roi_parent_sources
        sources = parse_roi_parent_sources(output.get("parent_sources"))
        if (
            output.get("structure_id") != invocation.structure_id
            or tuple(output.get("eligible_parent_ids", ())) != invocation.eligible_parent_ids
            or tuple(item.eligible_parent_id for item in sources) != invocation.eligible_parent_ids
            or not _is_sha256(output.get("predecessor_result_ref_sha256"))
        ):
            raise SectionBoundaryError("secondary output changed its ordered parent scope")


def _mapping_sha256(
    value: Mapping[str, Any] | Sequence[Any],
) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _json_value(value: object) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        raise ROIAnalysisError("workflow JSON numbers must be finite")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ROIAnalysisError("workflow mappings require string keys")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    if hasattr(value, "to_mapping") and callable(value.to_mapping):
        return _json_value(value.to_mapping())
    raise ROIAnalysisError(f"workflow state is not JSON serializable: {type(value).__name__}")


def _mutable_json(value: object) -> Any:
    return _json_value(value)
