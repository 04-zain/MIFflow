"""State-authenticated marker-measurement population opening.

The `MarkerGateDomainAuthority` record carries `source_kind`, `validation_domain_id`,
`canonical_marker_ids`, `marker_settings` and its remaining
declared fields.

The operation retains the selected stable-cell population, measurement generation and
phenotype view. Its records and stage adapters connect accepted numerical results to the
current batch target and reviewed application scope."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from collections import Counter
from dataclasses import asdict, dataclass, replace
import json
from math import isfinite
from uuid import uuid4
from types import MappingProxyType
from typing import Final

import numpy as np

from mif_pipeline.cell_analysis import (
    AcceptedThreshold,
    Histogram,
    SectionThresholdOverride,
    ThresholdCandidate,
    ThresholdCounts,
    ThresholdFacet,
    ThresholdFitPolicy,
    ThresholdReview,
    ThresholdServiceError,
    accept_threshold,
    build_threshold_review,
    index_accepted_thresholds,
)
from mif_pipeline.cell_analysis.thresholds import GaussianFitPolicy, SlideThresholdOverride
from mif_pipeline.cell_analysis.nimbus import load_nimbus_scores
from mif_pipeline.cell_analysis.clustering import FeatureRole, FeatureSpec
from mif_pipeline.cell_analysis.marker_rules import MeasurementSemantic
from mif_pipeline.cell_analysis.measurement_plan import FeatureKind
from mif_pipeline.cell_analysis.measurement_rows import (
    MarkerIntensityRowsDescriptor,
    MeasurementRowStreamError,
    MeasurementRowStreamExpectation,
    validate_measurement_row_stream,
)
from mif_pipeline.pipeline_core import (
    InteractionAction,
    InteractionReason,
    PhenotypingMode,
    StageResult,
    StageStatus,
)

from ..batch_analysis_preflight import MIF_PIPELINE_BATCH_RUNTIME_VERSION
from ..batch_artifacts import (
    AnalysisDomainBinding,
    ArtifactConfigurationBinding,
    ArtifactIdentityContract,
    ArtifactOrigin,
    ArtifactProjectionTarget,
    ArtifactScope,
    BatchArtifactError,
    BatchArtifactRef,
    BatchCommittedArtifactAuthority,
    BatchCurrentExecutionAuthority,
    BatchNodeExecutionContext,
    BatchNodeExecutionResult,
    validate_committed_artifact_authority_set,
    validate_current_execution_authority,
)
from ..batch_manifest import (
    BatchManifestError,
    ResolvedBatchSample,
    ValidatedBatchManifest,
    revalidate_retained_manifest,
)
from ..batch_interactions import (
    BatchInteractionTarget,
    BatchTargetResponseValidation,
)
from ..batch_measurement_artifacts import (
    ValidatedMeasurementsResultProjection,
    decode_committed_measurements_result,
)
from ..batch_phenotyping_mode_artifacts import (
    ValidatedPhenotypingModeProjection,
    decode_committed_phenotyping_mode,
)
from ..batch_state import (
    BatchAttemptAuthorizationRequest,
    BatchStateError,
    BatchStateStore,
    batch_interaction_request_id,
)
from ._common import (
    BatchPhenotypingError,
    canonical_bytes,
    digest,
    feature_schema_sha256,
)
from .marker_codec import (
    MarkerResponseError,
    decode_marker_gate_response,
    encode_marker_gate_response,
)
from .marker_records import MarkerGateResponse
from mif_pipeline.cell_analysis.phenotyping_population import (
    validate_population_settings, resolve_population_selection, population_includes,
)
from mif_pipeline.orchestration.phenotyping_population_sources import current_population_sources, population_source_references
from .marker_previews import marker_distribution_previews
from .marker_settings import (
    MissingMarkerMeasurementsError,
    SETUP_CONTEXT_VERSION,
    marker_feature_candidates,
    configured_marker_catalog,
    marker_settings_locked,
    validate_marker_settings,
    marker_population_from_modes,
    validate_marker_setup_response,
)

_NODE_KIND: Final[str] = "marker_gate_review"
_ISSUER: Final[object] = object()
_EVIDENCE_ISSUER: Final[object] = object()
_INTERACTION_CONTEXT_VERSION: Final[str] = "marker-gates/v1"
_RESPONSE_VALIDATOR_ID: Final[str] = "marker-gate-response-validator"
_RESPONSE_VALIDATOR_VERSION: Final[str] = "2"
_SETUP_RESPONSE_VALIDATOR_ID: Final[str] = "marker-setup-response-validator"
_SETUP_RESPONSE_VALIDATOR_VERSION: Final[str] = "1"
_DOMAIN_AUTHORITY_ISSUER: Final[object] = object()
_DOMAIN_AUTHORITY_SCHEMA_NAME: Final[str] = "mif-batch-marker-gate-domain-authority"
_DOMAIN_AUTHORITY_PRODUCER_VERSION: Final[str] = (
    "mif-pipeline-batch-marker-gate-domain-authority/v6"
)


# ==============================================================================
#                                Marker execution
# ==============================================================================


@dataclass(frozen=True, slots=True)
class _MarkerObservation:
    identity: tuple[str, str, str, str, str]
    analysis_domain_id: str
    values_by_marker_id: tuple[tuple[str, float | None], ...]
    included: bool = True


@dataclass(frozen=True, slots=True)
class _MarkerThresholdRow:
    identity: tuple[str, str, str, str, str]
    analysis_domain_id: str
    value: float
    compartment: str
    cell_identity: str
    image_identity: str
    section_identity: str


@dataclass(frozen=True, slots=True)
class _MarkerAcceptanceEvidence:
    marker_id: str
    rows: tuple[_MarkerThresholdRow, ...]
    pooled_review: ThresholdReview
    section_reviews: tuple[tuple[str, tuple[_MarkerThresholdRow, ...], ThresholdReview], ...]
    slide_reviews: tuple[tuple[str, tuple[_MarkerThresholdRow, ...], ThresholdReview], ...] = ()
    fit_scope: str = "pooled"


@dataclass(frozen=True, slots=True, init=False)
class AuthenticatedMarkerMeasurementPopulation:
    """Issuer-only, nonserializable current marker-row population."""

    validation_domain_id: str
    canonical_marker_ids: tuple[str, ...]
    marker_semantics: tuple[MeasurementSemantic, ...]
    marker_settings: Mapping[str, object]
    marker_settings_sha256: str
    setup_lineage: Mapping[str, object] | None
    marker_workflow: Mapping[str, object] | None
    _population_source_refs: Mapping[str, tuple[str, ...]]
    measurement_projections: tuple[ValidatedMeasurementsResultProjection, ...]
    measurement_authority_bindings: tuple[Mapping[str, object], ...]
    population_cell_count: int
    population_cell_inventory_sha256: str
    source_state_head_sha256: str
    active_plan_sha256: str
    admission_plan_sha256: str
    node_id: str
    attempt_id: str
    attempt_authorization_sha256: str
    compatibility_signature_sha256: str
    _context: BatchNodeExecutionContext
    _current_execution: BatchCurrentExecutionAuthority
    _committed_authorities: tuple[BatchCommittedArtifactAuthority, ...]
    _observations: tuple[_MarkerObservation, ...]
    _seal_sha256: str
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("AuthenticatedMarkerMeasurementPopulation is issuer-only")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("AuthenticatedMarkerMeasurementPopulation cannot be subclassed")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("AuthenticatedMarkerMeasurementPopulation is nonserializable")

    def __copy__(self) -> object:
        raise TypeError("AuthenticatedMarkerMeasurementPopulation is noncopyable")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("AuthenticatedMarkerMeasurementPopulation is noncopyable")


@dataclass(frozen=True, slots=True, init=False)
class MarkerGateEvidence:
    """Issuer-only deterministic marker-threshold suggestion evidence."""

    validation_domain_id: str
    canonical_marker_ids: tuple[str, ...]
    marker_settings: Mapping[str, object]
    marker_settings_sha256: str
    setup_lineage: Mapping[str, object] | None
    request_context: Mapping[str, object]
    evidence_sha256: str
    source_state_head_sha256: str
    active_plan_sha256: str
    admission_plan_sha256: str
    node_id: str
    attempt_id: str
    attempt_authorization_sha256: str
    compatibility_signature_sha256: str
    threshold_fit_policy_sha256: str
    _population: AuthenticatedMarkerMeasurementPopulation
    _acceptance_evidence: tuple[_MarkerAcceptanceEvidence, ...]
    _population_seal_sha256: str
    _source_authority_seal_sha256s: tuple[str, ...]
    _seal_sha256: str
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("MarkerGateEvidence is issuer-only")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("MarkerGateEvidence cannot be subclassed")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("MarkerGateEvidence is nonserializable")

    def __copy__(self) -> object:
        raise TypeError("MarkerGateEvidence is noncopyable")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("MarkerGateEvidence is noncopyable")


@dataclass(frozen=True, slots=True, init=False)
class MarkerGateDomainAuthority:
    """Issuer-only accepted authority for one validation domain."""

    source_kind: str
    validation_domain_id: str
    canonical_marker_ids: tuple[str, ...]
    marker_settings: Mapping[str, object]
    marker_settings_sha256: str
    setup_lineage: Mapping[str, object] | None
    threshold_fit_policy_sha256: str
    measurement_authority_bindings: tuple[Mapping[str, object], ...]
    evidence_sha256: str
    accepted_thresholds: tuple[AcceptedThreshold, ...]
    node_id: str
    attempt_id: str
    attempt_authorization_sha256: str
    admission_plan_sha256: str
    compatibility_signature_sha256: str
    interaction_target_id: str
    interaction_group_id: str
    interaction_group_sha256: str
    interaction_response_sha256: str
    interaction_request_id: str
    interaction_context_sha256: str
    interaction_checkpoint_id: str
    interaction_predecessor_attempt_id: str
    interaction_predecessor_authorization_sha256: str
    interaction_predecessor_terminal_record_sha256: str
    interaction_action: str
    interaction_response_validator_id: str
    interaction_response_validator_version: str
    domain_gate_sha256: str
    _current_execution: BatchCurrentExecutionAuthority
    _analysis_domain_bindings: tuple[AnalysisDomainBinding, ...]
    _configuration_bindings: tuple[ArtifactConfigurationBinding, ...]
    _evidence: MarkerGateEvidence
    _request: StageResult
    _seal_sha256: str
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("MarkerGateDomainAuthority is issuer-only")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("MarkerGateDomainAuthority cannot be subclassed")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("MarkerGateDomainAuthority is nonserializable")

    def __copy__(self) -> object:
        raise TypeError("MarkerGateDomainAuthority is noncopyable")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("MarkerGateDomainAuthority is noncopyable")


def _validated_manifest(
    manifest: ValidatedBatchManifest,
    *,
    current: BatchCurrentExecutionAuthority,
    state_store: BatchStateStore,
) -> tuple[ValidatedBatchManifest, Mapping[str, ResolvedBatchSample]]:
    try:
        reopened = revalidate_retained_manifest(
            manifest.original_manifest_bytes, manifest.normalized_manifest_bytes
        )
        head = state_store.load_head()
        target = ArtifactProjectionTarget.from_manifest(
            reopened,
            current.participant_ids,
            ArtifactScope(
                validation_domain_id=current.validation_domain_id,
                sample_id=None,
                slide_id=None,
                project_entry_id=None,
                analysis_domain_bindings=current.analysis_domain_bindings,
            ),
        )
    except (
        BatchArtifactError,
        BatchManifestError,
        BatchStateError,
        TypeError,
        ValueError,
    ) as exc:
        raise BatchPhenotypingError("marker population manifest authority is invalid") from exc
    if (
        reopened != manifest
        or head.batch_id != reopened.manifest.batch_id
        or head.normalized_manifest_sha256 != reopened.normalized_manifest_sha256
        or head.sha256 != current.state_head_sha256
        or head.active_plan_sha256 != current.active_plan_sha256
        or target.configuration_bindings != current.configuration_bindings
        or target.participant_ids != current.participant_ids
    ):
        raise BatchPhenotypingError("marker population manifest differs from current execution")
    samples = {
        item.sample_id: item
        for item in reopened.enabled_samples
        if item.sample_id in current.participant_ids
    }
    if set(samples) != set(current.participant_ids) or any(
        item.validation_domain_id != current.validation_domain_id for item in samples.values()
    ):
        raise BatchPhenotypingError("marker population participants lack exact manifest samples")
    return reopened, MappingProxyType(samples)


def _validate_execution(
    context: BatchNodeExecutionContext,
    current: BatchCurrentExecutionAuthority,
) -> None:
    """Check marker population scope after common adapter and input admission."""
    if (
        current.node_kind != _NODE_KIND
        or current.node_scope != "validation_domain"
        or current.validation_domain_id is None
        or current.clustering_scope is not None
        or not current.participant_ids
        or not current.project_entry_ids
        or not current.analysis_domain_bindings
        or not current.configuration_bindings
        or context.current_image_identity_sha256 is not None
        or context.script_sha256 is not None
        or context.project_identity_sha256 is not None
        or context.project_authority_head_sha256 is not None
        or context.project_qpproj_snapshot_sha256 is not None
        or context.project_entry_projection_sha256 is not None
    ):
        raise BatchPhenotypingError("marker population execution scope is invalid")


def _authenticate_sources(
    *,
    context: BatchNodeExecutionContext,
    current: BatchCurrentExecutionAuthority,
    state_store: BatchStateStore,
) -> tuple[
    tuple[BatchCommittedArtifactAuthority, ...],
    tuple[ValidatedMeasurementsResultProjection, ...],
    tuple[ValidatedPhenotypingModeProjection, ...],
]:
    try:
        authorities = tuple(
            state_store.authenticate_available_artifact(ref) for ref in context.inputs
        )
        authorities = validate_committed_artifact_authority_set(
            authorities,
            expected_active_plan_sha256=current.active_plan_sha256,
        )
    except (BatchArtifactError, BatchStateError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("marker population input authority is not current") from exc
    ref_digests = tuple(sorted(item.artifact_ref.artifact_ref_sha256 for item in authorities))
    if (
        not authorities
        or any(item.state_head_sha256 != current.state_head_sha256 for item in authorities)
        or ref_digests != current.input_artifact_ref_sha256s
        or ref_digests != tuple(sorted(item.artifact_ref_sha256 for item in context.inputs))
    ):
        raise BatchPhenotypingError("marker population inputs differ from current execution")
    measurement_authorities = tuple(
        item for item in authorities if item.artifact_ref.artifact_kind == "measurements_result"
    )
    mode_authorities = tuple(
        item for item in authorities if item.artifact_ref.artifact_kind == "phenotyping_mode"
    )
    if (
        not measurement_authorities
        or not mode_authorities
        or len(measurement_authorities) + len(mode_authorities) != len(authorities)
    ):
        raise BatchPhenotypingError("marker population requires only measurement and mode inputs")
    try:
        measurements = tuple(
            decode_committed_measurements_result(item) for item in measurement_authorities
        )
        modes = tuple(decode_committed_phenotyping_mode(item) for item in mode_authorities)
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("marker population input decoding failed") from exc
    return authorities, measurements, modes


def _validate_measurements_and_modes(
    *,
    current: BatchCurrentExecutionAuthority,
    samples: Mapping[str, ResolvedBatchSample],
    measurements: tuple[ValidatedMeasurementsResultProjection, ...],
    modes: tuple[ValidatedPhenotypingModeProjection, ...],
    settings: Mapping[str, object],
    offered_marker_features: tuple[Mapping[str, object], ...],
) -> tuple[
    tuple[str, ...],
    tuple[MeasurementSemantic, ...],
    Mapping[str, ValidatedMeasurementsResultProjection],
]:
    """Validate measurements and modes for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    configured = tuple(item["marker_id"] for item in settings["marker_rules"])
    offers = {(item["marker_id"], item["feature_id"]): item for item in offered_marker_features}
    configured_semantics = tuple(
        MeasurementSemantic(
            channel_id=offers[(item["marker_id"], item["feature_id"])]["channel_id"],
            measurement=offers[(item["marker_id"], item["feature_id"])]["measurement"],
            compartment=offers[(item["marker_id"], item["feature_id"])]["compartment"],
            biomarker=item["marker_id"],
        )
        for item in settings["marker_rules"] if item["feature_id"] is not None
    )

    current_bindings = set(current.analysis_domain_bindings)
    entries = {
        (item.sample_id, item.slide_id, item.project_entry_id)
        for item in current.analysis_domain_bindings
    }
    if (
        not current_bindings
        or {key[0] for key in entries} != set(current.participant_ids)
        or {key[2] for key in entries} != set(current.project_entry_ids)
    ):
        raise BatchPhenotypingError("marker execution lacks participant analysis-domain bindings")

    by_ref: dict[str, ValidatedMeasurementsResultProjection] = {}
    physical_sections: set[tuple[str, str, str, str]] = set()
    covered_entries: set[tuple[str, str, str]] = set()
    covered_bindings: set[object] = set()
    expected_configurations = {item.sample_id: item for item in current.configuration_bindings}
    for measurement in measurements:
        ref = measurement.artifact_ref
        digest_key = ref.artifact_ref_sha256
        entry_key = (
            measurement.sample_id,
            measurement.slide_id,
            measurement.project_entry_id,
        )
        section_key = (*entry_key, measurement.section_id)
        sample = samples.get(measurement.sample_id)
        expected_configuration = expected_configurations.get(measurement.sample_id)
        measurement_bindings = set(measurement.analysis_domain_bindings)
        if (
            digest_key in by_ref
            or section_key in physical_sections
            or sample is None
            or entry_key not in entries
            or expected_configuration is None
            or not measurement_bindings
            or not measurement_bindings <= current_bindings
            or covered_bindings & measurement_bindings
            or any(
                item.sample_id != measurement.sample_id
                or item.slide_id != measurement.slide_id
                or item.project_entry_id != measurement.project_entry_id
                for item in measurement.analysis_domain_bindings
            )
            or measurement.state_head_sha256 != current.state_head_sha256
            or measurement.active_plan_sha256 != current.active_plan_sha256
            or measurement.slide_id != sample.slide_id
            or ref.scope.validation_domain_id != current.validation_domain_id
            or ref.scope.sample_id != measurement.sample_id
            or ref.scope.slide_id != measurement.slide_id
            or ref.scope.project_entry_id != measurement.project_entry_id
            or ref.scope.analysis_domain_bindings != measurement.analysis_domain_bindings
            or ref.configuration_bindings != (expected_configuration,)
            or ref.owning_identity.get("image_role") != "primary_mif"
            or measurement.execution_exchange_sha256
            != ref.owning_identity.get("execution_exchange_sha256")
        ):
            raise BatchPhenotypingError(
                "marker measurement owner has foreign section, domain, or configuration"
            )
        by_ref[digest_key] = measurement
        physical_sections.add(section_key)
        covered_entries.add(entry_key)
        covered_bindings.update(measurement_bindings)
    if covered_entries != entries or covered_bindings != current_bindings:
        raise BatchPhenotypingError("marker measurements do not exactly cover physical sections")

    expected_modes = set(current.analysis_domain_bindings)
    actual_modes: dict[AnalysisDomainBinding, ValidatedPhenotypingModeProjection] = {}
    for mode in modes:
        binding = mode.analysis_domain_binding
        measurement = by_ref.get(mode.measurements_result_ref_sha256)
        sample = samples.get(mode.sample_id)
        if (
            binding in actual_modes
            or measurement is None
            or sample is None
            or mode.selected_mode is not PhenotypingMode.MARKER_RULES
            or mode.configured_mode != sample.config.phenotyping.mode
            or mode.state_head_sha256 != current.state_head_sha256
            or mode.active_plan_sha256 != current.active_plan_sha256
            or mode.sample_id != measurement.sample_id
            or mode.slide_id != measurement.slide_id
            or mode.project_entry_id != measurement.project_entry_id
            or mode.validation_domain_id != current.validation_domain_id
            or mode.resolved_config_sha256 != sample.resolved_config_sha256
            or binding not in expected_modes
            or binding not in measurement.analysis_domain_bindings
        ):
            raise BatchPhenotypingError(
                "marker phenotyping mode authority differs from its measurement"
            )
        actual_modes[binding] = mode
    if set(actual_modes) != set(expected_modes):
        raise BatchPhenotypingError("marker modes do not exactly cover current analysis domains")
    return configured, configured_semantics, MappingProxyType(by_ref)


def _marker_features(
    measurement: ValidatedMeasurementsResultProjection,
    semantics: tuple[MeasurementSemantic, ...],
    population_settings: Mapping[str, object] | None = None,
    expected_population_source_ref_sha256s=None,
) -> tuple[FeatureSpec, ...]:
    selected: list[FeatureSpec] = []
    for semantic in semantics:
        candidates = tuple(
            item
            for item in measurement.measurement_plan.features
            if item.biomarker == semantic.biomarker
            and item.channel_id == semantic.channel_id
            and item.measurement == semantic.measurement
            and item.compartment == semantic.compartment
        )
        if len(candidates) != 1:
            raise BatchPhenotypingError("marker measurement semantic is missing or ambiguous")
        feature = candidates[0]
        if (
            feature.kind != FeatureKind.EXPRESSION.value
            or feature.role is not FeatureRole.EXPRESSION
            or feature.feature_id is None
            or feature.biomarker != semantic.biomarker
            or feature.channel_id != semantic.channel_id
            or feature.measurement != semantic.measurement
            or feature.compartment != semantic.compartment
        ):
            raise BatchPhenotypingError(
                "marker feature is not an explicit protein-marker intensity semantic"
            )
        selected.append(feature)
    marker_features = tuple(
        item
        for item in measurement.measurement_plan.features
        if item.kind == FeatureKind.EXPRESSION.value
    )
    if len(set(selected)) != len(selected):
        raise BatchPhenotypingError("marker row schema contains duplicate selected features")
    return marker_features


def _reopen_measurement_rows(
    *,
    measurement: ValidatedMeasurementsResultProjection,
    sample: ResolvedBatchSample,
    marker_ids: tuple[str, ...],
    semantics: tuple[MeasurementSemantic, ...],
    population_settings: Mapping[str, object] | None = None,
    expected_population_source_ref_sha256s=None,
) -> tuple[
    tuple[_MarkerObservation, ...],
    tuple[FeatureSpec, ...],
    str,
]:
    marker_features = _marker_features(measurement, semantics)
    reference = measurement.measurement_rows
    provisional: list[tuple[str, str, tuple[float, ...]]] = []
    try:
        descriptor = MarkerIntensityRowsDescriptor(
            path=reference.path,
            content_schema_version=reference.content_schema_version,
            sha256=reference.sha256,
            byte_count=reference.byte_count,
            row_count=reference.row_count,
        )
        expectation = MeasurementRowStreamExpectation(
            project_entry_id=measurement.project_entry_id,
            slide_id=measurement.slide_id,
            section_id=measurement.section_id,
            segmentation_generation_id=measurement.measurement_generation_id,
            measurement_generation_id=measurement.measurement_generation_id,
            segmentation_population_id=measurement.segmentation_population_id,
            analysis_domain_bindings=tuple(
                {
                    "analysis_domain_id": item.analysis_domain_id,
                    "analysis_domain_revision_sha256": (item.analysis_domain_revision_sha256),
                }
                for item in measurement.analysis_domain_bindings
            ),
            feature_ids=tuple(item.feature_id for item in marker_features),
            measurement_names=tuple(item.measurement for item in marker_features),
        )
        selection = resolve_population_selection(population_settings, sample_id=measurement.sample_id,
            project_entry_id=measurement.project_entry_id, slide_id=measurement.slide_id)
        rows = validate_measurement_row_stream(
            descriptor=descriptor,
            authenticated_output_root=reference.path.parent.parent.parent,
            expected=expectation,
            population_selection=selection,
            expected_population_source_ref_sha256s=expected_population_source_ref_sha256s if selection["kind"] != "all_segmented" else None,
            provisional_population_callback=lambda cell_id, domain_id, values, membership: (
                provisional.append((cell_id, domain_id, values, population_includes(selection, membership)))
            ),
        )
        checks = (
            (rows.cell_count, measurement.cell_count),
            (rows.cell_count, reference.row_count),
            (rows.cell_ids_sha256, measurement.cell_ids_sha256),
            (rows.cell_ids_sha256, reference.cell_ids_sha256),
            (
                rows.cell_to_domain_membership_sha256,
                measurement.cell_to_domain_membership_sha256,
            ),
            (
                rows.cell_to_domain_membership_sha256,
                reference.cell_to_domain_membership_sha256,
            ),
            (
                rows.domain_cell_commitments_sha256,
                measurement.domain_cell_commitments_sha256,
            ),
            (
                rows.domain_cell_commitments_sha256,
                reference.domain_cell_commitments_sha256,
            ),
            (rows.row_inventory_sha256, reference.row_inventory_sha256),
            (
                feature_schema_sha256(marker_features),
                reference.feature_schema_sha256,
            ),
            (len(provisional), measurement.cell_count),
        )
        if any(actual != expected for actual, expected in checks):
            raise BatchPhenotypingError("marker row population differs from measurement authority")
        if tuple(item.to_mapping() for item in rows.domain_cell_commitments) != tuple(
            dict(item) for item in measurement.domain_cell_commitments
        ):
            raise BatchPhenotypingError(
                "marker row domain commitments differ from measurement authority"
            )
        feature_markers = tuple(item.biomarker for item in marker_features)
        selected_by_marker = {item.biomarker: item for item in semantics}
        selected_positions = tuple(
            next(
                index
                for index, feature in enumerate(marker_features)
                if feature.biomarker == marker
                and feature.channel_id == selected_by_marker[marker].channel_id
                and feature.measurement == selected_by_marker[marker].measurement
                and feature.compartment == selected_by_marker[marker].compartment
            )
            for marker in marker_ids
        )
        if (
            not set(marker_ids) <= set(feature_markers)
            or rows.feature_ids != tuple(item.feature_id for item in marker_features)
            or rows.measurement_names != tuple(item.measurement for item in marker_features)
        ):
            raise BatchPhenotypingError("marker row columns differ from explicit marker semantics")
        observations = tuple(
            _MarkerObservation(
                identity=(
                    measurement.sample_id,
                    measurement.project_entry_id,
                    measurement.slide_id,
                    measurement.section_id,
                    cell_id,
                ),
                analysis_domain_id=domain_id,
                values_by_marker_id=tuple(
                    (marker, values[index])
                    for marker, index in zip(marker_ids, selected_positions, strict=True)
                ),
                included=included,
            )
            for cell_id, domain_id, values, included in provisional
        )
    except BaseException:
        provisional.clear()
        raise
    provisional.clear()
    return observations, marker_features, rows.row_inventory_sha256


def _binding(
    measurement: ValidatedMeasurementsResultProjection,
    sample: ResolvedBatchSample,
) -> Mapping[str, object]:
    return MappingProxyType(
        {
            "project_entry_id": measurement.project_entry_id,
            "sample_id": measurement.sample_id,
            "slide_id": measurement.slide_id,
            "section_id": measurement.section_id,
            "measurements_result_ref_sha256": (measurement.artifact_ref.artifact_ref_sha256),
            "resolved_config_sha256": sample.resolved_config_sha256,
            "analysis_domain_bindings": tuple(
                MappingProxyType(item.to_mapping()) for item in measurement.analysis_domain_bindings
            ),
            "authenticated_cell_count": measurement.cell_count,
            "authenticated_cell_inventory_sha256": measurement.cell_ids_sha256,
        }
    )


def _marker_sequence_progress(workflow: Mapping[str, object]) -> dict[str, object]:
    """Carry confirmed progress and its shared-settings lock into the next prompt."""
    return {key: workflow[key] for key in
            ("version", "current_marker_id", "completed", "settings_locked", "retained_completed", "return_marker_id") if key in workflow}


def _marker_setup_request(
    *,
    current: BatchCurrentExecutionAuthority,
    measurements: tuple[ValidatedMeasurementsResultProjection, ...],
    samples: Mapping[str, ResolvedBatchSample],
    marker_ids: tuple[str, ...],
    offered: tuple[Mapping[str, object], ...],
    initial_settings: Mapping[str, object] | None = None,
    workflow: Mapping[str, object] | None = None,
    retained_bindings: tuple[Mapping[str, object], ...] | None = None,
    population_settings: Mapping[str, object] | None = None,
    source_refs_by_measurement=None,
    software_requirement: Mapping[str, str] | None = None,
) -> StageResult:
    bindings = retained_bindings or tuple(
        _canonical_projection(_binding(item, samples[item.sample_id]), "marker_setup_binding")
        for item in sorted(
            measurements,
            key=lambda value: (
                value.sample_id,
                value.project_entry_id,
                value.slide_id,
                value.section_id,
            ),
        )
    )
    context = {
        "schema_version": SETUP_CONTEXT_VERSION,
        "validation_domain_id": current.validation_domain_id,
        "canonical_marker_ids": marker_ids,
        "measurement_authority_bindings": bindings,
        "offered_marker_features": offered,
    }
    if initial_settings is not None:
        context["initial_marker_settings"] = initial_settings
    if software_requirement is not None:
        from mif_pipeline.orchestration.setup.nimbus_requirements import NimbusSoftwareRequirement

        context["software_requirement"] = NimbusSoftwareRequirement.from_mapping(
            software_requirement
        ).to_mapping()
    if workflow is not None:
        context["marker_workflow"] = workflow
        selection = population_settings if population_settings is not None else (initial_settings.get("phenotyping_population") if initial_settings else None)
        if selection is None and samples:
            selections = [validate_population_settings(getattr(item.config.phenotyping, "population", None)) for item in samples.values()]
            if any(item != selections[0] for item in selections):
                raise BatchPhenotypingError("marker participants have different shared population settings")
            selection = selections[0]
        context["phenotyping_population"] = validate_population_settings(selection)
        population_options = {}
        context["marker_distributions"] = marker_distribution_previews(measurements, workflow["current_marker_id"],
            population_settings=context["phenotyping_population"], source_refs_by_measurement=source_refs_by_measurement,
            population_options_out=population_options)
        context["population_options"] = population_options
    evidence_sha256 = digest(context)
    return StageResult.needs_interaction(
        stage=_NODE_KIND,
        slide_id=measurements[0].slide_id,
        reason=InteractionReason.MARKER_COMPOSITE_RULES_REQUIRED,
        affected_object_ids=tuple(
            sorted({item.analysis_domain_id for item in current.analysis_domain_bindings})
        ),
        message=(software_requirement["message"] if software_requirement is not None else
                 "Choose pooled or per-slide fitting, marker methods and optional mixture constraints before reviewing gates."),
        allowed_actions=(
            InteractionAction.DEFINE_MARKER_COMPOSITE_RULES.value,
            InteractionAction.ABORT.value,
        ),
        resume_context={**context, "setup_evidence_sha256": evidence_sha256},
        checkpoint_id="marker-setup-" + evidence_sha256[:48],
    )


def _accepted_setup_response(
    *,
    authorization: object,
    request: StageResult,
    node_id: str,
    state_store: BatchStateStore,
) -> tuple[dict[str, object], Mapping[str, object]]:
    try:
        revision = request.interaction_reason is InteractionReason.MARKER_THRESHOLD_REVIEW_REQUIRED
        response = state_store.load_validated_interaction_response(authorization)
        if response is None or authorization.retry_ordinal < 1:
            raise BatchPhenotypingError("marker setup retry is incomplete")
        context_sha256 = digest(request.to_mapping()["resume_context"])
        request_id = batch_interaction_request_id(
            node_id=node_id,
            attempt_id=response["prior_attempt_id"],
            attempt_authorization_sha256=response["prior_attempt_authorization_sha256"],
            interaction_context_sha256=context_sha256,
        )
        if (
            response["node_id"] != node_id
            or response["interaction_request_id"] != request_id
            or response["interaction_context_sha256"] != context_sha256
            or response["action"] != (InteractionAction.REVIEW_MARKER_THRESHOLDS.value if revision else InteractionAction.DEFINE_MARKER_COMPOSITE_RULES.value)
            or response["response_validator_id"] != (_RESPONSE_VALIDATOR_ID if revision else _SETUP_RESPONSE_VALIDATOR_ID)
            or response["response_validator_version"] != (_RESPONSE_VALIDATOR_VERSION if revision else _SETUP_RESPONSE_VALIDATOR_VERSION)
            or response["prior_terminal_record_sha256"]
            != authorization.interaction_predecessor_terminal_record_sha256
            or state_store.load_interaction_request(request_id) != request
        ):
            raise BatchPhenotypingError("marker setup response lineage differs")
        if revision:
            from .marker_preparation import validate_preparation_change
            normalized = {'marker_settings': validate_preparation_change(request, response['response']['marker_settings'])}
        else:
            normalized = validate_marker_setup_response(request, response["response"])
        lineage = {
            "interaction_response_sha256": response["response_sha256"],
            "interaction_request_id": request_id,
            "interaction_context_sha256": context_sha256,
            "interaction_predecessor_attempt_id": response["prior_attempt_id"],
            "interaction_predecessor_authorization_sha256": response[
                "prior_attempt_authorization_sha256"
            ],
            "interaction_predecessor_terminal_record_sha256": response[
                "prior_terminal_record_sha256"
            ],
            "interaction_target_id": response["target_id"],
            "interaction_group_id": response["group_id"],
            "interaction_group_sha256": response["group_sha256"],
            "interaction_action": response["action"],
            "interaction_response_validator_id": response["response_validator_id"],
            "interaction_response_validator_version": response["response_validator_version"],
        }
        if "revisit_marker_id" in normalized:
            return normalized, lineage
        return normalized.get("marker_settings", {"_preview_population": normalized.get("phenotyping_population")}), lineage
    except (BatchStateError, KeyError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("marker setup response is not authenticated") from exc


def _resolve_marker_settings(
    *,
    context: BatchNodeExecutionContext,
    current: BatchCurrentExecutionAuthority,
    state_store: BatchStateStore,
    samples: Mapping[str, ResolvedBatchSample],
    measurements: tuple[ValidatedMeasurementsResultProjection, ...],
    population_settings: Mapping[str, object],
) -> tuple[dict[str, object], Mapping[str, object] | None] | StageResult:
    """Resolve the current setup response or return its next unfitted prompt.

    Revisiting a confirmed marker retains the preceding evidence prefix and
    shared choices, clears later methods, and requests setup without fitting.
    """
    try:
        marker_ids, offered = marker_feature_candidates(measurements)
    except MissingMarkerMeasurementsError as exc:
        return StageResult.failed(
            stage=_NODE_KIND,
            slide_id=measurements[0].slide_id,
            error_code="marker_measurements_missing",
            error_message=str(exc),
        )
    snapshot = state_store.reopen_execution_snapshot(expected_head_sha256=current.state_head_sha256)
    population_source_refs = {item.artifact_ref.artifact_ref_sha256: tuple(sorted(population_source_references(
        current_population_sources(snapshot=snapshot, measurement=item, config=samples[item.sample_id].config))))
        for item in measurements}
    authorization = context.authorization
    predecessor_sha256 = authorization.interaction_predecessor_terminal_record_sha256
    if predecessor_sha256 is not None:
        predecessor = state_store.load_attempt(predecessor_sha256)
        previous_request = predecessor.stage_result
        workflow = previous_request.resume_context.get("marker_workflow")
        if workflow is not None:
            if predecessor.node_id != context.node_id:
                raise BatchPhenotypingError("marker sequence belongs to another node")
            if previous_request.interaction_reason is InteractionReason.MARKER_COMPOSITE_RULES_REQUIRED:
                settings, lineage = _accepted_setup_response(authorization=authorization,
                    request=previous_request, node_id=context.node_id, state_store=state_store)
                if "revisit_marker_id" in settings:
                    marker = settings["revisit_marker_id"]
                    if 'marker_settings' in settings:
                        return settings['marker_settings'], lineage
                    initial = previous_request.resume_context["initial_marker_settings"]
                    marker_ids, offered = configured_marker_catalog(initial,
                        available_marker_ids=marker_ids, offered_marker_features=offered)
                    index = marker_ids.index(marker)
                    revised = {**initial, "marker_rules": tuple(
                        rule if offset < index else {
                            "marker_id": rule["marker_id"], "feature_id": rule["feature_id"],
                            "polarity": rule["polarity"], "method": None,
                            "gaussian_variance": "shared", "component_location_constraints": {},
                        } for offset, rule in enumerate(initial["marker_rules"]))}
                    return _marker_setup_request(current=current, measurements=measurements, samples=samples,
                        marker_ids=marker_ids, offered=offered, initial_settings=revised,
                        source_refs_by_measurement=population_source_refs,
                        workflow={**_marker_sequence_progress(workflow), "current_marker_id": marker,
                            "completed": workflow["completed"][:index], "settings_locked": True})
                if "_preview_population" in settings:
                    initial = previous_request.resume_context.get('initial_marker_settings')
                    if initial is not None and marker_settings_locked(previous_request.resume_context):
                        marker_ids, offered = configured_marker_catalog(initial,
                            available_marker_ids=marker_ids, offered_marker_features=offered)
                    return _marker_setup_request(current=current, measurements=measurements, samples=samples,
                        marker_ids=marker_ids, offered=offered,
                        initial_settings=previous_request.resume_context.get("initial_marker_settings"),
                        population_settings=settings["_preview_population"], source_refs_by_measurement=population_source_refs,
                        workflow=_marker_sequence_progress(workflow))
                available_ids, available_features = marker_ids, offered
                marker_ids, offered = configured_marker_catalog(settings,
                    available_marker_ids=marker_ids, offered_marker_features=offered)
                progress = _marker_sequence_progress(workflow)
                if not marker_settings_locked(previous_request.resume_context):
                    progress['current_marker_id'] = marker_ids[0]
                if next(item for item in settings["marker_rules"] if item["marker_id"] == progress["current_marker_id"])["method"] is None:
                    if not marker_settings_locked(previous_request.resume_context):
                        marker_ids, offered = available_ids, available_features
                    return _marker_setup_request(current=current, measurements=measurements, samples=samples,
                        marker_ids=marker_ids, offered=offered, initial_settings=settings,
                        source_refs_by_measurement=population_source_refs, workflow=progress)
                return settings, lineage
            if previous_request.interaction_reason is InteractionReason.MARKER_THRESHOLD_REVIEW_REQUIRED:
                response = state_store.load_validated_interaction_response(authorization)
                if response is not None and 'marker_settings' in response['response']:
                    return _accepted_setup_response(authorization=authorization,
                        request=previous_request, node_id=context.node_id, state_store=state_store)
                return dict(workflow["settings"]), workflow["setup_lineage"]
            raise BatchPhenotypingError("marker sequence has an unsupported predecessor phase")
    if authorization.retry_ordinal != 0 or predecessor_sha256 is not None:
        raise BatchPhenotypingError("Marker review requires its current sequential setup record")
    if authorization.interaction_response_sha256 is not None:
        raise BatchPhenotypingError("Initial marker setup has an unexpected response")
    node = next(item for item in snapshot.plan.nodes if item.node_id == context.node_id)
    initial_settings = None
    if node.settled_review_revision is not None:
        initial_settings = marker_review_initial_settings(node.settled_review_revision, state_store)
    first_marker = marker_ids[0]
    if initial_settings is not None:
        selected, _ = configured_marker_catalog(initial_settings,
            available_marker_ids=marker_ids, offered_marker_features=offered)
        first_marker = selected[0]
    return _marker_setup_request(
        current=current, measurements=measurements, samples=samples,
        marker_ids=marker_ids, offered=offered, initial_settings=initial_settings,
        population_settings=population_settings,
        source_refs_by_measurement=population_source_refs,
        workflow={"version": "marker-sequence/v1", "current_marker_id": first_marker, "completed": ()},
    )


def _measurement_projection_material(
    projection: ValidatedMeasurementsResultProjection,
) -> Mapping[str, object]:
    values: dict[str, object] = {}
    for name in ValidatedMeasurementsResultProjection.__dataclass_fields__:
        value = getattr(projection, name)
        if name == "artifact_ref":
            values[name] = projection.artifact_ref.artifact_ref_sha256
        elif name == "analysis_domain_bindings":
            values[name] = tuple(item.to_mapping() for item in value)
        elif name == "measurement_plan":
            values[name] = value.to_mapping()
        elif name == "measurement_rows":
            values[name] = {
                field: (str(value.path) if field == "path" else getattr(value, field))
                for field in value.__dataclass_fields__
            }
        else:
            values[name] = value
    return values


def _population_material(values: Mapping[str, object]) -> Mapping[str, object]:
    current = values["_current_execution"]
    authorities = values["_committed_authorities"]
    observations = values["_observations"]
    assert isinstance(current, BatchCurrentExecutionAuthority)
    assert isinstance(authorities, tuple)
    assert isinstance(observations, tuple)
    return {
        "population_source_refs": values["_population_source_refs"],
        "public": {
            key: (
                [
                    {
                        "channel_id": item.channel_id,
                        "measurement": item.measurement,
                        "compartment": item.compartment,
                        "biomarker": item.biomarker,
                    }
                    for item in value
                ]
                if key == "marker_semantics"
                else (
                    tuple(_measurement_projection_material(item) for item in value)
                    if key == "measurement_projections"
                    else value
                )
            )
            for key, value in values.items()
            if key
            in {
                "validation_domain_id",
                "canonical_marker_ids",
                "marker_semantics",
                "marker_settings",
                "marker_settings_sha256",
                "setup_lineage",
                "marker_workflow",
                "measurement_projections",
                "measurement_authority_bindings",
                "population_cell_count",
                "population_cell_inventory_sha256",
                "source_state_head_sha256",
                "active_plan_sha256",
                "admission_plan_sha256",
                "node_id",
                "attempt_id",
                "attempt_authorization_sha256",
                "compatibility_signature_sha256",
            }
        },
        "current_execution": {
            "execution_context_sha256": current.execution_context_sha256,
            "seal_sha256": current._seal_sha256,
        },
        "committed_authorities": [
            {
                "artifact_ref_sha256": item.artifact_ref.artifact_ref_sha256,
                "terminal_record_sha256": item.terminal_record_sha256,
                "seal_sha256": item._seal_sha256,
            }
            for item in authorities
        ],
        "observations": [
            {
                "identity": item.identity,
                "analysis_domain_id": item.analysis_domain_id,
                "values_by_marker_id": item.values_by_marker_id,
                "included": item.included,
            }
            for item in observations
        ],
    }


def _issue_population(values: Mapping[str, object]) -> AuthenticatedMarkerMeasurementPopulation:
    issued_values = dict(values)
    issued_values["_seal_sha256"] = digest(_population_material(issued_values))
    issued_values["_issuer"] = _ISSUER
    result = object.__new__(AuthenticatedMarkerMeasurementPopulation)
    for name in AuthenticatedMarkerMeasurementPopulation.__dataclass_fields__:
        object.__setattr__(result, name, issued_values[name])
    return result


def _population_values(
    population: AuthenticatedMarkerMeasurementPopulation,
) -> Mapping[str, object]:
    return {
        name: getattr(population, name)
        for name in AuthenticatedMarkerMeasurementPopulation.__dataclass_fields__
        if name not in {"_seal_sha256", "_issuer"}
    }


def _reauthenticate_population(
    population: AuthenticatedMarkerMeasurementPopulation,
    *,
    state_store: BatchStateStore,
) -> None:
    if type(population) is not AuthenticatedMarkerMeasurementPopulation:
        raise TypeError("marker evidence requires an exact marker population")
    if not isinstance(state_store, BatchStateStore):
        raise TypeError("marker evidence requires the exact state owner")
    if type(state_store) is not BatchStateStore:
        raise BatchPhenotypingError("marker evidence requires the exact state owner")
    if population._issuer is not _ISSUER or population._seal_sha256 != digest(
        _population_material(_population_values(population))
    ):
        raise BatchPhenotypingError("marker population capability is invalid")
    try:
        current = state_store.authenticate_current_execution_context(population._context)
        validate_current_execution_authority(current, expected_context=population._context)
        authorities = validate_committed_artifact_authority_set(
            population._committed_authorities,
            expected_active_plan_sha256=population.active_plan_sha256,
        )
    except (BatchArtifactError, BatchStateError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("marker population is not current and authenticated") from exc
    if (
        current != population._current_execution
        or tuple(authorities) != population._committed_authorities
        or any(
            item.state_head_sha256 != population.source_state_head_sha256 for item in authorities
        )
        or tuple(sorted(item.artifact_ref.artifact_ref_sha256 for item in authorities))
        != current.input_artifact_ref_sha256s
    ):
        raise BatchPhenotypingError("marker population differs from current execution authority")


def _canonical_projection(value: object, field: str) -> object:
    if isinstance(value, Mapping):
        if not all(type(key) is str for key in value):
            raise BatchPhenotypingError(f"{field} has a non-text key")
        return MappingProxyType(
            {key: _canonical_projection(item, f"{field}.{key}") for key, item in value.items()}
        )
    if isinstance(value, (tuple, list)):
        return tuple(
            _canonical_projection(item, f"{field}[{index}]") for index, item in enumerate(value)
        )
    if value is None or type(value) in {str, bool}:
        return value
    if type(value) is int:
        return value
    if isinstance(value, float):
        if not isfinite(float(value)):
            raise BatchPhenotypingError(f"{field} must be finite")
        return float(value)
    raise BatchPhenotypingError(f"{field} is not canonical JSON evidence")


def _identity_token(identity: tuple[str, ...]) -> str:
    return canonical_bytes(identity).decode("utf-8")


def _histogram_projection(histogram: Histogram) -> Mapping[str, object]:
    return MappingProxyType(
        {
            "counts": histogram.counts,
            "bin_edges": histogram.bin_edges,
        }
    )


def _counts_projection(
    counts: ThresholdCounts | None,
) -> Mapping[str, object] | None:
    if counts is None:
        return None
    return MappingProxyType(
        {
            "negative": counts.negative,
            "positive": counts.positive,
            "total": counts.total,
            "positive_fraction": counts.positive_fraction,
        }
    )


def _candidate_projection(candidate: object) -> Mapping[str, object]:
    return MappingProxyType(
        {
            "method": candidate.method,
            "status": candidate.status,
            "threshold": candidate.threshold,
            "reason": candidate.reason,
            "iterations": candidate.iterations,
            "converged": candidate.converged,
            "parameters": _canonical_projection(
                candidate.parameters, f"{candidate.method}.parameters"
            ),
            "density_x": candidate.density_x,
            "component_densities": candidate.component_densities,
            "posterior_x": candidate.posterior_x,
            "positive_posterior": candidate.positive_posterior,
            "counts": _counts_projection(candidate.counts),
            "interpretation": candidate.interpretation,
            "outcome": candidate.outcome,
        }
    )


def _facet_projection(facet: ThresholdFacet) -> Mapping[str, object]:
    return MappingProxyType(
        {
            "section": facet.section,
            "compartment": facet.compartment,
            "histogram": _histogram_projection(facet.histogram),
        }
    )


def _row_identity_projection(
    row: _MarkerThresholdRow,
) -> Mapping[str, object]:
    sample_id, project_entry_id, slide_id, section_id, cell_id = row.identity
    return MappingProxyType(
        {
            "cell_identity": row.cell_identity,
            "image_identity": row.image_identity,
            "section_identity": row.section_identity,
            "sample_id": sample_id,
            "project_entry_id": project_entry_id,
            "slide_id": slide_id,
            "section_id": section_id,
            "cell_id": cell_id,
            "analysis_domain_id": row.analysis_domain_id,
        }
    )


def _review_projection(
    review: ThresholdReview,
    rows: tuple[_MarkerThresholdRow, ...],
    *, include_empirical: bool = True, excluded_count: int = 0,
) -> Mapping[str, object]:
    rows_by_identity = {item.cell_identity: item for item in rows}
    if len(rows_by_identity) != len(rows):
        raise BatchPhenotypingError("threshold review rows have duplicate composite identities")
    try:
        fit_rows = tuple(rows_by_identity[item] for item in review.fit_cell_ids)
        example_rows = tuple(rows_by_identity[item] for item in review.example_cell_ids)
    except KeyError as exc:
        raise BatchPhenotypingError(
            "threshold owner returned a foreign composite identity"
        ) from exc
    if len(fit_rows) != len(set(review.fit_cell_ids)) or len(example_rows) != len(
        set(review.example_cell_ids)
    ):
        raise BatchPhenotypingError("threshold owner returned duplicate composite identities")
    frequencies = Counter(item.value for item in rows if isfinite(item.value)) if include_empirical else {}
    support = sorted(frequencies)
    projected = {
        **({"empirical_cdf": {"values": support, "counts": [frequencies[value] for value in support]}} if include_empirical else {}),
        "histogram": _histogram_projection(review.histogram),
        "manual": _candidate_projection(review.manual),
        "candidates": tuple(_candidate_projection(item) for item in review.candidates),
        "facets": tuple(_facet_projection(item) for item in review.facets),
        "eligible_count": review.eligible_count,
        "unknown_count": review.unknown_count,
        "excluded_count": excluded_count,
        "fit_cell_ids": tuple(item.cell_identity for item in fit_rows),
        "fit_cell_count": len(fit_rows),
        "fit_cell_ids_sha256": digest(tuple(item.identity for item in fit_rows)),
        "examples": tuple(_row_identity_projection(item) for item in example_rows),
    }
    return _canonical_projection(projected, "threshold_review")


def _policy_projection(policy: ThresholdFitPolicy) -> Mapping[str, object]:
    return _canonical_projection(asdict(policy), "threshold_fit_policy")


def _marker_rows(
    population: AuthenticatedMarkerMeasurementPopulation,
    semantic: MeasurementSemantic,
) -> tuple[_MarkerThresholdRow, ...]:
    rows: list[_MarkerThresholdRow] = []
    for observation in population._observations:
        if not observation.included:
            continue
        values = dict(observation.values_by_marker_id)
        if set(values) != {item.biomarker for item in population.marker_semantics}:
            raise BatchPhenotypingError("marker evidence population has incomplete marker values")
        value = values.get(semantic.biomarker)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise BatchPhenotypingError("marker intensity must be numeric or missing")
        resolved = float("nan") if value is None else float(value)
        image_identity = observation.identity[:3]
        section_identity = observation.identity[:4]
        rows.append(
            _MarkerThresholdRow(
                identity=observation.identity,
                analysis_domain_id=observation.analysis_domain_id,
                value=resolved,
                compartment=semantic.compartment,
                cell_identity=_identity_token(observation.identity),
                image_identity=_identity_token(image_identity),
                section_identity=_identity_token(section_identity),
            )
        )
    rows.sort(key=lambda item: item.identity)
    return tuple(rows)


def _threshold_review(
    rows: tuple[_MarkerThresholdRow, ...],
    policy: ThresholdFitPolicy,
    *,
    manual_threshold: float | None = None,
    methods: tuple[str, ...] | None = None,
    constraints: Mapping[str, object] | None = None,
    nimbus_scores: object = None,
    nimbus_score_cutoff: float = 0.5,
) -> ThresholdReview:
    try:
        return build_threshold_review(
            tuple(item.value for item in rows),
            tuple(item.cell_identity for item in rows),
            tuple(item.section_identity for item in rows),
            tuple(item.compartment for item in rows),
            policy,
            manual_threshold=manual_threshold,
            methods=methods,
            constraints=constraints,
            nimbus_scores=nimbus_scores,
            nimbus_score_cutoff=nimbus_score_cutoff,
        )
    except (ThresholdServiceError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError(
            "marker threshold owner rejected the authenticated view"
        ) from exc


def _image_groups(
    rows: tuple[_MarkerThresholdRow, ...],
) -> tuple[tuple[tuple[str, str, str], tuple[_MarkerThresholdRow, ...]], ...]:
    grouped: dict[tuple[str, str, str], list[_MarkerThresholdRow]] = {}
    for row in rows:
        grouped.setdefault(row.identity[:3], []).append(row)
    return tuple(
        (
            key,
            tuple(sorted(grouped[key], key=lambda item: item.identity)),
        )
        for key in sorted(grouped)
    )


def _section_groups(
    rows: tuple[_MarkerThresholdRow, ...],
) -> tuple[tuple[tuple[str, str, str, str], tuple[_MarkerThresholdRow, ...]], ...]:
    grouped: dict[tuple[str, str, str, str], list[_MarkerThresholdRow]] = {}
    for row in rows:
        grouped.setdefault(row.identity[:4], []).append(row)
    return tuple(
        (
            key,
            tuple(sorted(grouped[key], key=lambda item: item.identity)),
        )
        for key in sorted(grouped)
    )


def _compartment_groups(
    rows: tuple[_MarkerThresholdRow, ...],
) -> tuple[tuple[str, tuple[_MarkerThresholdRow, ...]], ...]:
    grouped: dict[str, list[_MarkerThresholdRow]] = {}
    for row in rows:
        grouped.setdefault(row.compartment, []).append(row)
    return tuple(
        (
            key,
            tuple(sorted(grouped[key], key=lambda item: item.identity)),
        )
        for key in sorted(grouped)
    )


def _image_fields(identity: tuple[str, str, str]) -> dict[str, object]:
    return {
        "image_identity": _identity_token(identity),
        "sample_id": identity[0],
        "project_entry_id": identity[1],
        "slide_id": identity[2],
    }


def _section_fields(
    identity: tuple[str, str, str, str],
) -> dict[str, object]:
    return {
        **_image_fields(identity[:3]),
        "section_identity": _identity_token(identity),
        "section_id": identity[3],
    }


def _distribution_projection(
    review: ThresholdReview,
) -> Mapping[str, object]:
    return MappingProxyType(
        {
            "eligible_count": review.eligible_count,
            "unknown_count": review.unknown_count,
            "histogram": _histogram_projection(review.histogram),
        }
    )


def _nimbus_data(population: AuthenticatedMarkerMeasurementPopulation, marker: str,
                 *, expected_sha256: str | None = None, score_path=None,
                 nimbus_score_provider=None) -> tuple[dict[str, float], Mapping[str, object]]:
    """Read pretrained scores bound to the same native cell and image population."""
    rule = next(item for item in population.marker_settings["marker_rules"] if item["marker_id"] == marker)
    path = score_path or rule.get("nimbus_score_path")
    identities = tuple(item.identity for item in population._observations)
    if path:
        by_scope = {(item.sample_id, item.project_entry_id, item.slide_id, item.section_id): item for item in population.measurement_projections}
        result = load_nimbus_scores(path, expected_identities=identities,
            expected_population_by_scope={scope: item.segmentation_population_id for scope, item in by_scope.items()},
            expected_image_by_scope={scope: item.canonical_image_id for scope, item in by_scope.items()},
            marker=marker, expected_sha256=expected_sha256, allow_superset=True)
    elif nimbus_score_provider is not None:
        result = nimbus_score_provider(population, marker, expected_sha256)
    else:
        raise BatchPhenotypingError("Nimbus requires the configured native image-score provider or an explicit identity-bound score import")
    provenance = dict(result.provenance)
    provenance["cutoff"] = rule.get("nimbus_cutoff", 0.5)
    return {_identity_token(identity): float(score) for identity, score in zip(identities, result.scores, strict=True)}, provenance


def _direct_nimbus_review(review: ThresholdReview, rows: tuple[_MarkerThresholdRow, ...],
                          scores: Mapping[str, float], provenance: Mapping[str, object]) -> ThresholdReview:
    """Attach direct pretrained expression-confidence scores; distillation is an explicit later action."""
    finite = tuple(scores[row.cell_identity] for row in rows if isfinite(scores[row.cell_identity]))
    cutoff = provenance["cutoff"]
    positive = sum(score >= cutoff for score in finite)
    histogram_counts, histogram_edges = np.histogram(finite, bins=50, range=(0.0, 1.0))
    frequencies = Counter(finite)
    support = sorted(frequencies)
    candidate = ThresholdCandidate(method="nimbus", status="available" if finite else "unavailable", threshold=None,
        outcome="direct_scores" if finite else "unknown", reason="Direct pretrained Nimbus scores" if finite else "No finite Nimbus scores in this scope",
        parameters={"score_provenance": dict(provenance), "score_cutoff": cutoff, "known_count": len(finite), "unknown_count": len(rows) - len(finite),
            "score_histogram": {"counts": histogram_counts.tolist(), "bin_edges": histogram_edges.tolist()},
            "score_empirical_cdf": {"values": support, "counts": [frequencies[value] for value in support]}},
        counts=ThresholdCounts(len(finite) - positive, positive) if finite else None,
        interpretation="pretrained_nimbus_score")
    return replace(review, candidates=(candidate,))


def _marker_review_projection(
    population: AuthenticatedMarkerMeasurementPopulation,
    semantic: MeasurementSemantic,
    policy: ThresholdFitPolicy,
    *, nimbus_score_provider=None,
) -> tuple[Mapping[str, object], _MarkerAcceptanceEvidence]:
    """Fit only the configured scope; distributions and count previews never refit."""
    rows = _marker_rows(population, semantic)
    if not rows:
        raise BatchPhenotypingError("No cells in the selected region; choose another population and refresh its distribution before fitting")
    rule = next(item for item in population.marker_settings["marker_rules"] if item["marker_id"] == semantic.biomarker)
    fit_scope = population.marker_settings["fit_scope"]
    methods = () if rule["method"] in {"manual", "nimbus"} else (rule["method"],)
    constraints = rule.get("component_location_constraints", {})
    marker_policy = replace(policy, gaussian=GaussianFitPolicy(variance_mode=rule.get("gaussian_variance", "shared")))
    pooled_review = _threshold_review(rows, marker_policy, methods=methods if fit_scope == "pooled" else (), constraints=constraints)
    image_groups = _image_groups(rows)
    excluded_by_image = Counter(item.identity[:3] for item in population._observations if not item.included)
    excluded_by_section = Counter(item.identity[:4] for item in population._observations if not item.included)
    all_image_groups = tuple((identity, dict(image_groups).get(identity, ())) for identity in sorted({item.identity[:3] for item in population._observations}))
    section_groups = _section_groups(rows)
    slide_reviews = tuple((identity, group, _threshold_review(group, marker_policy, methods=methods, constraints=constraints))
                          for identity, group in image_groups) if fit_scope == "per_slide" else ()
    nimbus_scores = None
    if rule.get("method") == "nimbus":
        nimbus_scores, provenance = _nimbus_data(population, semantic.biomarker, nimbus_score_provider=nimbus_score_provider)
        if fit_scope == "pooled":
            pooled_review = _direct_nimbus_review(pooled_review, rows, nimbus_scores, provenance)
        else:
            slide_reviews = tuple((identity, group, _direct_nimbus_review(review, group, nimbus_scores, provenance)) for identity, group, review in slide_reviews)
    section_reviews = tuple((identity, group, _threshold_review(group, marker_policy, methods=()))
                            for identity, group in section_groups)

    def distribution(group: tuple[_MarkerThresholdRow, ...]) -> Mapping[str, object]:
        return _distribution_projection(_threshold_review(group, marker_policy, methods=()))

    per_image_distributions = tuple({**_image_fields(identity), **distribution(group), "excluded_count": excluded_by_image[identity]} for identity, group in all_image_groups)
    per_section_distributions = tuple({**_section_fields(identity), **_distribution_projection(review), "excluded_count": excluded_by_section[identity]} for identity, _group, review in section_reviews)
    projected_sections = tuple({**_section_fields(identity), "review": _review_projection(review, group, include_empirical=False)} for identity, group, review in section_reviews)
    projected_slides = tuple({**_image_fields(identity), "review": _review_projection(review, group, excluded_count=excluded_by_image[identity])} for identity, group, review in slide_reviews)
    candidate_counts = []
    examples = []
    selected_reviews = slide_reviews if fit_scope == "per_slide" else ((None, rows, pooled_review),)
    for scope_identity, _scope_rows, review in selected_reviews:
        for candidate in review.candidates:
            if candidate.status != "available":
                continue
            for image_identity, image_rows in image_groups:
                if scope_identity is not None and image_identity != scope_identity:
                    continue
                count_rows = tuple(row for row in image_rows if isfinite(row.value))
                if candidate.outcome == "direct_scores":
                    finite_scores = tuple(nimbus_scores[row.cell_identity] for row in image_rows if isfinite(nimbus_scores[row.cell_identity]))
                    positive = sum(score >= candidate.parameters["score_cutoff"] for score in finite_scores)
                    count_total = len(finite_scores)
                elif candidate.outcome == "all_positive":
                    positive = len(count_rows)
                    count_total = len(count_rows)
                elif candidate.outcome == "all_negative":
                    positive = 0
                    count_total = len(count_rows)
                elif candidate.threshold is not None:
                    positive = sum(row.value >= candidate.threshold for row in count_rows)
                    count_total = len(count_rows)
                else:
                    continue
                candidate_counts.append({**_image_fields(image_identity), "method": candidate.method,
                    "threshold": candidate.threshold, "outcome": candidate.outcome,
                    "negative": count_total - positive, "positive": positive,
                    "positive_fraction": positive / count_total if count_total else 0.0})
                if candidate.threshold is None or not count_rows:
                    continue
                image_rows = count_rows
                # Three deterministic representatives require one bounded selection pass.
                near = min(image_rows, key=lambda row: (abs(row.value - candidate.threshold), row.identity))
                below = min((row for row in image_rows if row.value < candidate.threshold), key=lambda row: (row.value, row.identity), default=None)
                above = max((row for row in image_rows if row.value >= candidate.threshold), key=lambda row: (row.value, row.identity), default=None)
                seen = set()
                for role, row in (("near", near), ("below", below), ("above", above)):
                    if row is None or row.cell_identity in seen:
                        continue
                    seen.add(row.cell_identity)
                    examples.append({**_row_identity_projection(row), "method": candidate.method,
                        "threshold": candidate.threshold, "value": row.value,
                        "compartment": row.compartment, "role": role})
    projection = {
        "marker_id": semantic.biomarker,
        "fit_scope": fit_scope,
        "configured_method": rule.get("method"),
        "gaussian_variance": rule.get("gaussian_variance", "shared"),
        "component_location_constraints": constraints,
        "missing_count": sum(not isfinite(row.value) for row in rows),
        "excluded_count": sum(not observation.included for observation in population._observations),
        "measurement_semantic": {"biomarker": semantic.biomarker, "channel_id": semantic.channel_id,
            "measurement": semantic.measurement, "compartment": semantic.compartment,
            "semantic_kind": "mif_protein_marker_intensity"},
        "pooled_review": _review_projection(pooled_review, rows, include_empirical=fit_scope == "pooled", excluded_count=sum(excluded_by_image.values())),
        "per_image_distributions": per_image_distributions,
        "per_section_distributions": per_section_distributions,
        "per_compartment_distributions": tuple({"compartment": compartment, **distribution(group)} for compartment, group in _compartment_groups(rows)),
        "per_section_reviews": projected_sections,
        "per_slide_reviews": projected_slides,
        "per_image_candidate_counts": candidate_counts,
        "examples": examples,
    }
    canonical = _canonical_projection(projection, f"marker_reviews.{semantic.biomarker}")
    acceptance = _MarkerAcceptanceEvidence(semantic.biomarker, rows, pooled_review,
        tuple((_identity_token(identity), group, review) for identity, group, review in section_reviews),
        tuple((_identity_token(identity), group, review) for identity, group, review in slide_reviews), fit_scope)
    return canonical, acceptance


def _projection_matches(current: object, retained: object) -> bool:
    """Compare the complete current numerical record, including every saved field."""
    if isinstance(retained, Mapping):
        return isinstance(current, Mapping) and set(current) == set(retained) and all(_projection_matches(current[key], value) for key, value in retained.items())
    if isinstance(retained, (tuple, list)):
        return isinstance(current, (tuple, list)) and len(current) == len(retained) and all(_projection_matches(a, b) for a, b in zip(current, retained, strict=True))
    return current == retained


def _restore_review(value: Mapping[str, object], rows: tuple[_MarkerThresholdRow, ...],
                    policy: ThresholdFitPolicy | None = None) -> ThresholdReview:
    """Reopen authenticated persisted numerical evidence without evaluating any model."""
    def histogram(raw: Mapping[str, object]) -> Histogram:
        return Histogram(tuple(raw["counts"]), tuple(raw["bin_edges"]))
    def candidate(raw: Mapping[str, object]) -> ThresholdCandidate:
        counts = raw["counts"]
        try:
            fields = {name: raw[name] for name in ThresholdCandidate.__dataclass_fields__}
        except KeyError as exc:
            raise BatchPhenotypingError("Retained marker candidate lacks current decision fields") from exc
        fields["parameters"] = dict(raw["parameters"])
        fields["counts"] = None if counts is None else ThresholdCounts(counts["negative"], counts["positive"])
        for name in ("density_x", "posterior_x", "positive_posterior"):
            fields[name] = tuple(raw[name])
        fields["component_densities"] = tuple(tuple(item) for item in raw["component_densities"])
        return ThresholdCandidate(**fields)
    if not {"fit_cell_ids", "unknown_count", "excluded_count"} <= set(value):
        raise BatchPhenotypingError("Retained marker review lacks current fitting-population evidence")
    fit_cell_ids = value["fit_cell_ids"]
    review = ThresholdReview(histogram(value["histogram"]), candidate(value["manual"]),
        tuple(candidate(item) for item in value["candidates"]),
        tuple(ThresholdFacet(item["section"], item["compartment"], histogram(item["histogram"])) for item in value["facets"]),
        tuple(fit_cell_ids), tuple(item["cell_identity"] for item in value["examples"]), value["eligible_count"], unknown_count=value["unknown_count"])
    if not _projection_matches(_review_projection(review, rows, include_empirical="empirical_cdf" in value, excluded_count=value["excluded_count"]), value):
        raise BatchPhenotypingError("retained marker review differs from its current cell population")
    return review


def _restore_marker_review(population: AuthenticatedMarkerMeasurementPopulation, semantic: MeasurementSemantic,
                           projection: Mapping[str, object], policy: ThresholdFitPolicy | None = None) -> tuple[Mapping[str, object], _MarkerAcceptanceEvidence]:
    rows = _marker_rows(population, semantic)
    images = {_identity_token(identity): group for identity, group in _image_groups(rows)}
    sections = {_identity_token(identity): group for identity, group in _section_groups(rows)}
    pooled = _restore_review(projection["pooled_review"], rows, policy)
    restored_sections = tuple((item["section_identity"], sections[item["section_identity"]],
        _restore_review(item["review"], sections[item["section_identity"]], policy)) for item in projection["per_section_reviews"])
    restored_slides = tuple((item["image_identity"], images[item["image_identity"]],
        _restore_review(item["review"], images[item["image_identity"]], policy)) for item in projection["per_slide_reviews"])
    canonical = _canonical_projection(projection, "retained_marker_review")
    return canonical, _MarkerAcceptanceEvidence(semantic.biomarker, rows, pooled, restored_sections, restored_slides, projection["fit_scope"])


def _persisted_reviews(population: AuthenticatedMarkerMeasurementPopulation, state_store: BatchStateStore) -> Mapping[str, object] | None:
    """Return the authenticated predecessor review only for the same settings and measurements."""
    predecessor = population._context.authorization.interaction_predecessor_terminal_record_sha256
    if predecessor is None:
        return None
    attempt = state_store.load_attempt(predecessor)
    request = attempt.stage_result
    if request.interaction_reason is not InteractionReason.MARKER_THRESHOLD_REVIEW_REQUIRED:
        return None
    context = request.resume_context
    if attempt.node_id != population.node_id or context.get("marker_settings_sha256") != population.marker_settings_sha256:
        response = state_store.load_validated_interaction_response(population._context.authorization)
        if (attempt.node_id == population.node_id and response is not None
                and response['response'].get('refit_current_marker') is True
                and 'marker_settings' in response['response']
                and population.setup_lineage is not None
                and population.setup_lineage['interaction_response_sha256'] == response['response_sha256']
                and digest(response['response']['marker_settings']) == population.marker_settings_sha256):
            return None
        raise BatchPhenotypingError("retained review settings differ from the accepted setup")
    if _canonical_projection(context.get("measurement_authority_bindings"), "bindings") != _canonical_projection(_canonical_measurement_bindings(population), "bindings"):
        raise BatchPhenotypingError("retained marker review measurements changed")
    without_digest = {key: value for key, value in context.items() if key not in {"schema_version", "evidence_sha256"}}
    if context.get("evidence_sha256") != digest(without_digest):
        raise BatchPhenotypingError("retained marker review evidence digest differs")
    return context


def _evidence_material(values: Mapping[str, object]) -> Mapping[str, object]:
    population = values["_population"]
    acceptance = values["_acceptance_evidence"]
    assert isinstance(population, AuthenticatedMarkerMeasurementPopulation)
    assert isinstance(acceptance, tuple)
    return {
        "public": {
            key: value
            for key, value in values.items()
            if key
            not in {
                "_population",
                "_acceptance_evidence",
                "_seal_sha256",
                "_issuer",
            }
        },
        "population_binding": id(population),
        "acceptance_bindings": tuple(
            {
                "evidence_binding": id(item),
                "marker_id": item.marker_id,
                "rows_binding": id(item.rows),
                "pooled_review_binding": id(item.pooled_review),
                "fit_scope": item.fit_scope,
                "slide_review_bindings": tuple((image_id, id(rows), id(review)) for image_id, rows, review in item.slide_reviews),
                "section_review_bindings": tuple(
                    (section_id, id(rows), id(review))
                    for section_id, rows, review in item.section_reviews
                ),
            }
            for item in acceptance
        ),
    }


def _issue_evidence(values: Mapping[str, object]) -> MarkerGateEvidence:
    issued_values = dict(values)
    issued_values["_seal_sha256"] = digest(_evidence_material(issued_values))
    issued_values["_issuer"] = _EVIDENCE_ISSUER
    result = object.__new__(MarkerGateEvidence)
    for name in MarkerGateEvidence.__dataclass_fields__:
        object.__setattr__(result, name, issued_values[name])
    return result


def _evidence_values(evidence: MarkerGateEvidence) -> Mapping[str, object]:
    return {
        name: getattr(evidence, name)
        for name in MarkerGateEvidence.__dataclass_fields__
        if name not in {"_seal_sha256", "_issuer"}
    }


def _project_retained_section_reviews(
    evidence: _MarkerAcceptanceEvidence,
) -> tuple[Mapping[str, object], ...]:
    projected: list[Mapping[str, object]] = []
    for section_identity, rows, review in evidence.section_reviews:
        if not rows or any(row.section_identity != section_identity for row in rows):
            raise BatchPhenotypingError("marker acceptance evidence has foreign section rows")
        identity = rows[0].identity[:4]
        if _identity_token(identity) != section_identity:
            raise BatchPhenotypingError("marker acceptance evidence section identity is invalid")
        projected.append(
            MappingProxyType(
                {
                    **_section_fields(identity),
                    "review": _review_projection(review, rows, include_empirical=False),
                }
            )
        )
    return tuple(projected)


def _reauthenticate_evidence(
    evidence: MarkerGateEvidence,
    state_store: BatchStateStore,
) -> None:
    if type(evidence) is not MarkerGateEvidence:
        raise TypeError("marker gate resolution requires exact evidence")
    if not isinstance(state_store, BatchStateStore):
        raise TypeError("marker gate resolution requires the exact state owner")
    if type(state_store) is not BatchStateStore:
        raise BatchPhenotypingError("marker gate resolution requires the exact state owner")
    if evidence._issuer is not _EVIDENCE_ISSUER or evidence._seal_sha256 != digest(
        _evidence_material(_evidence_values(evidence))
    ):
        raise BatchPhenotypingError("marker gate evidence capability is invalid")
    population = evidence._population
    _reauthenticate_population(population, state_store=state_store)
    if (
        evidence._population_seal_sha256 != population._seal_sha256
        or evidence._source_authority_seal_sha256s
        != tuple(item._seal_sha256 for item in population._committed_authorities)
        or evidence.validation_domain_id != population.validation_domain_id
        or evidence.canonical_marker_ids != population.canonical_marker_ids
        or evidence.marker_settings != population.marker_settings
        or evidence.marker_settings_sha256 != population.marker_settings_sha256
        or evidence.setup_lineage != population.setup_lineage
        or evidence.source_state_head_sha256 != population.source_state_head_sha256
        or evidence.active_plan_sha256 != population.active_plan_sha256
        or evidence.admission_plan_sha256 != population.admission_plan_sha256
        or evidence.node_id != population.node_id
        or evidence.attempt_id != population.attempt_id
        or evidence.attempt_authorization_sha256 != population.attempt_authorization_sha256
        or evidence.compatibility_signature_sha256 != population.compatibility_signature_sha256
        or evidence.threshold_fit_policy_sha256
        != digest(evidence.request_context.get("threshold_fit_policy"))
    ):
        raise BatchPhenotypingError("marker gate evidence differs from its current population")
    context_fields = (
        "validation_domain_id",
        "canonical_marker_ids",
        "marker_settings_sha256",
        "threshold_fit_policy",
        "measurement_authority_bindings",
        "marker_reviews",
        *(("marker_workflow",) if "marker_workflow" in evidence.request_context else ()),
        "evidence_sha256",
    )
    context_without_digest = {
        key: evidence.request_context[key] for key in context_fields if key != "evidence_sha256"
    }
    if (
        tuple(evidence.request_context) != context_fields
        or evidence.request_context["validation_domain_id"] != evidence.validation_domain_id
        or tuple(evidence.request_context["canonical_marker_ids"]) != evidence.canonical_marker_ids
        or evidence.request_context["marker_settings_sha256"] != evidence.marker_settings_sha256
        or tuple(evidence.request_context["measurement_authority_bindings"])
        != _canonical_measurement_bindings(population)
        or evidence.request_context["evidence_sha256"] != evidence.evidence_sha256
        or digest(context_without_digest) != evidence.evidence_sha256
    ):
        raise BatchPhenotypingError("marker gate request evidence is invalid")
    retained = evidence._acceptance_evidence
    marker_reviews = tuple(evidence.request_context["marker_reviews"])
    expected_markers = (evidence._population.marker_workflow["current_marker_id"],) if evidence._population.marker_workflow else evidence.canonical_marker_ids
    if tuple(item.marker_id for item in retained) != expected_markers or len(
        marker_reviews
    ) != len(retained):
        raise BatchPhenotypingError("marker gate retained reviews differ from marker cover")
    for acceptance, projection in zip(retained, marker_reviews, strict=True):
        if (
            not isinstance(projection, Mapping)
            or projection.get("marker_id") != acceptance.marker_id
            or not _projection_matches(_review_projection(acceptance.pooled_review, acceptance.rows, include_empirical="empirical_cdf" in projection.get("pooled_review", {}), excluded_count=projection.get("pooled_review", {}).get("excluded_count", 0)), projection.get("pooled_review"))
            or not _projection_matches(_project_retained_section_reviews(acceptance), tuple(projection.get("per_section_reviews", ())))
        ):
            raise BatchPhenotypingError(
                "marker gate retained owner reviews differ from request evidence"
            )


def marker_gate_domain_identity_contract() -> ArtifactIdentityContract:
    """Return the current marker-gate domain-authority identity contract."""

    return ArtifactIdentityContract(
        contract_id="mif-batch-marker-gate-domain-authority-identity/v6",
        artifact_kind="marker_gate",
        schema_name=_DOMAIN_AUTHORITY_SCHEMA_NAME,
        schema_version=6,
        required_fields=(
            "accepted_thresholds.marker_id",
            "accepted_thresholds.review_record_id",
            "accepted_thresholds.reviewer_id",
            "accepted_thresholds.selection_source",
            "accepted_thresholds.threshold",
            "accepted_thresholds.validation_domain",
            "admission_plan_sha256",
            "attempt_authorization_sha256",
            "attempt_id",
            "canonical_marker_ids",
            "compatibility_signature_sha256",
            "domain_gate_sha256",
            "evidence_sha256",
            "interaction_action",
            "interaction_checkpoint_id",
            "interaction_context_sha256",
            "interaction_group_id",
            "interaction_group_sha256",
            "interaction_predecessor_attempt_id",
            "interaction_predecessor_authorization_sha256",
            "interaction_predecessor_terminal_record_sha256",
            "interaction_request_id",
            "interaction_response_sha256",
            "interaction_response_validator_id",
            "interaction_response_validator_version",
            "interaction_target_id",
            "measurement_authority_bindings.analysis_domain_bindings.analysis_domain_id",
            "measurement_authority_bindings.analysis_domain_bindings.analysis_domain_revision",
            "measurement_authority_bindings.analysis_domain_bindings.analysis_domain_revision_sha256",
            "measurement_authority_bindings.analysis_domain_bindings.domain_set_sha256",
            "measurement_authority_bindings.analysis_domain_bindings.project_entry_id",
            "measurement_authority_bindings.analysis_domain_bindings.sample_id",
            "measurement_authority_bindings.analysis_domain_bindings.slide_id",
            "measurement_authority_bindings.authenticated_cell_count",
            "measurement_authority_bindings.authenticated_cell_inventory_sha256",
            "measurement_authority_bindings.measurements_result_ref_sha256",
            "measurement_authority_bindings.project_entry_id",
            "measurement_authority_bindings.resolved_config_sha256",
            "measurement_authority_bindings.sample_id",
            "measurement_authority_bindings.section_id",
            "measurement_authority_bindings.slide_id",
            "node_id",
            "marker_settings",
            "marker_settings_sha256",
            "source_kind",
            "threshold_fit_policy_sha256",
            "validation_domain_id",
        ),
        optional_fields=(
            "setup_lineage",
            "accepted_thresholds.section_overrides",
            "accepted_thresholds.section_overrides.reason",
            "accepted_thresholds.section_overrides.review_record_id",
            "accepted_thresholds.section_overrides.reviewer_id",
            "accepted_thresholds.section_overrides.section_id",
            "accepted_thresholds.section_overrides.selection_source",
            "accepted_thresholds.section_overrides.threshold",
        ) + ("accepted_thresholds.outcome", "accepted_thresholds.fit_scope", "accepted_thresholds.score_provenance",
               "accepted_thresholds.section_overrides.outcome", "accepted_thresholds.slide_thresholds",
               "accepted_thresholds.slide_thresholds.image_id", "accepted_thresholds.slide_thresholds.threshold",
               "accepted_thresholds.slide_thresholds.selection_source", "accepted_thresholds.slide_thresholds.reason",
               "accepted_thresholds.slide_thresholds.reviewer_id", "accepted_thresholds.slide_thresholds.review_record_id",
               "accepted_thresholds.slide_thresholds.outcome") + tuple(
            prefix + field for prefix in ("accepted_thresholds.", "accepted_thresholds.slide_thresholds.")
            for field in ("proposed_threshold", "proposed_outcome", "proposed_score_cutoff", "score_cutoff", "adjustment_source")
        ),
        required_runtime_names=("mif_pipeline",),
    )


def _marker_gate_request(evidence: MarkerGateEvidence) -> StageResult:
    population = evidence._population
    context = population._context
    expected_contract = marker_gate_domain_identity_contract()
    if (
        context.node_id != evidence.node_id
        or context.adapter_spec.node_kind != _NODE_KIND
        or population._current_execution.node_kind != _NODE_KIND
        or population._current_execution.node_scope != "validation_domain"
        or population._current_execution.validation_domain_id != evidence.validation_domain_id
        or context.adapter_spec.declared_outputs != ("marker_gate",)
        or context.adapter_spec.output_identity_contracts != (expected_contract,)
    ):
        raise BatchPhenotypingError("marker gate request execution contract is invalid")
    measurement_bindings = tuple(evidence.request_context["measurement_authority_bindings"])
    if not measurement_bindings:
        raise BatchPhenotypingError("marker gate request lacks a presentation anchor")
    first = measurement_bindings[0]
    if not isinstance(first, Mapping) or type(first.get("slide_id")) is not str:
        raise BatchPhenotypingError("marker gate request presentation anchor is invalid")
    analysis_domain_ids = tuple(
        sorted(
            {
                str(binding["analysis_domain_id"])
                for measurement in measurement_bindings
                for binding in tuple(measurement["analysis_domain_bindings"])
            }
        )
    )
    if not analysis_domain_ids:
        raise BatchPhenotypingError("marker gate request lacks analysis-domain authority")
    return StageResult.needs_interaction(
        stage=_NODE_KIND,
        slide_id=first["slide_id"],
        reason=InteractionReason.MARKER_THRESHOLD_REVIEW_REQUIRED,
        affected_object_ids=analysis_domain_ids,
        message=("Review and explicitly select one threshold for every canonical marker."),
        allowed_actions=(
            InteractionAction.REVIEW_MARKER_THRESHOLDS.value,
            InteractionAction.ABORT.value,
        ),
        resume_context={
            "schema_version": _INTERACTION_CONTEXT_VERSION,
            **evidence.request_context,
        },
        checkpoint_id="marker-gate-" + evidence.evidence_sha256[:48],
    )


def _response_scope_from_request(
    result: StageResult,
) -> tuple[str, str, tuple[str, ...], tuple[str, ...]]:
    context = result.resume_context
    if (
        result.stage != _NODE_KIND
        or result.status is not StageStatus.NEEDS_INTERACTION
        or result.interaction_reason is not InteractionReason.MARKER_THRESHOLD_REVIEW_REQUIRED
        or result.allowed_actions
        != (
            InteractionAction.REVIEW_MARKER_THRESHOLDS.value,
            InteractionAction.ABORT.value,
        )
        or not isinstance(context, Mapping)
        or context.get("schema_version") != _INTERACTION_CONTEXT_VERSION
    ):
        raise MarkerResponseError("target is not a marker-gate review request")
    try:
        validation_domain_id = context["validation_domain_id"]
        evidence_sha256 = context["evidence_sha256"]
        marker_reviews = tuple(context["marker_reviews"])
        marker_ids = tuple(item["marker_id"] for item in marker_reviews) if context.get("marker_workflow") else tuple(context["canonical_marker_ids"])
        section_ids = tuple(
            sorted(
                {
                    section["section_identity"]
                    for marker in marker_reviews
                    for section in tuple(marker["per_section_reviews"])
                }
            )
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MarkerResponseError("marker-gate request scope is malformed") from exc
    return (
        validation_domain_id,
        evidence_sha256,
        marker_ids,
        section_ids,
    )


def validate_marker_gate_target_response(
    target: BatchInteractionTarget,
    action: str,
    response: Mapping[str, object],
    *,
    authorization_request: BatchAttemptAuthorizationRequest,
) -> BatchTargetResponseValidation:
    """Validate one exact C1.1 response for the generic retry owner."""

    if not isinstance(target, BatchInteractionTarget):
        raise TypeError("marker response validation requires an interaction target")
    if not isinstance(authorization_request, BatchAttemptAuthorizationRequest):
        raise TypeError("marker response validation requires a base authorization request")
    if action == InteractionAction.ABORT.value:
        if (action not in target.stage_result.allowed_actions
                or not isinstance(response, Mapping) or response):
            return BatchTargetResponseValidation(target.target_id, target.node_id, "rejected",
                None, None, None, None, "marker_abort_response_invalid")
        return BatchTargetResponseValidation(target.target_id, target.node_id, "accepted",
            {}, authorization_request, _RESPONSE_VALIDATOR_ID,
            _RESPONSE_VALIDATOR_VERSION, None)
    if target.stage_result.interaction_reason is InteractionReason.MARKER_COMPOSITE_RULES_REQUIRED:
        try:
            if action != InteractionAction.DEFINE_MARKER_COMPOSITE_RULES.value:
                raise BatchPhenotypingError("marker setup response action is invalid")
            if "revisit_marker_id" not in response and response.get("preview_population") is not True and (not isinstance(response.get("marker_settings"), Mapping) or response["marker_settings"].get("schema_version") != "marker-phenotype-settings/v4"):
                raise BatchPhenotypingError("new marker setup requires explicit scope and fit configuration")
            normalized = validate_marker_setup_response(target.stage_result, response)
        except (BatchPhenotypingError, TypeError, ValueError):
            return BatchTargetResponseValidation(
                target_id=target.target_id,
                node_id=target.node_id,
                status="rejected",
                response=None,
                authorization_request=None,
                response_validator_id=None,
                response_validator_version=None,
                rejection_code="marker_setup_response_invalid",
            )
        return BatchTargetResponseValidation(
            target_id=target.target_id,
            node_id=target.node_id,
            status="accepted",
            response=normalized,
            authorization_request=authorization_request,
            response_validator_id=_SETUP_RESPONSE_VALIDATOR_ID,
            response_validator_version=_SETUP_RESPONSE_VALIDATOR_VERSION,
            rejection_code=None,
        )
    try:
        if action != InteractionAction.REVIEW_MARKER_THRESHOLDS.value:
            raise MarkerResponseError("marker response action is invalid")
        (
            validation_domain_id,
            evidence_sha256,
            marker_ids,
            section_ids,
        ) = _response_scope_from_request(target.stage_result)
        decoded = decode_marker_gate_response(
            canonical_bytes(response),
            expected_validation_domain_id=validation_domain_id,
            expected_evidence_sha256=evidence_sha256,
            expected_marker_ids=marker_ids,
            allowed_section_ids=section_ids,
            allowed_slide_ids=tuple(sorted({slide["image_identity"] for marker in target.stage_result.resume_context["marker_reviews"] for slide in marker.get("per_slide_reviews", ())})),
        )
        if decoded.refit_current_marker:
            if not target.stage_result.resume_context.get("marker_workflow"):
                raise MarkerResponseError("refitting requires the current marker workflow")
            if decoded.marker_settings is not None:
                from .marker_preparation import validate_preparation_change
                validate_preparation_change(target.stage_result, decoded.marker_settings)
        else:
            _validate_projected_marker_selections(target.stage_result.resume_context, decoded)
        canonical_response = json.loads(encode_marker_gate_response(decoded).decode("utf-8"))
        assert isinstance(canonical_response, Mapping)
    except (MarkerResponseError, TypeError, ValueError, KeyError):
        return BatchTargetResponseValidation(
            target_id=target.target_id,
            node_id=target.node_id,
            status="rejected",
            response=None,
            authorization_request=None,
            response_validator_id=None,
            response_validator_version=None,
            rejection_code="marker_gate_response_invalid",
        )
    return BatchTargetResponseValidation(
        target_id=target.target_id,
        node_id=target.node_id,
        status="accepted",
        response=canonical_response,
        authorization_request=authorization_request,
        response_validator_id=_RESPONSE_VALIDATOR_ID,
        response_validator_version=_RESPONSE_VALIDATOR_VERSION,
        rejection_code=None,
    )


def _accepted_threshold_mapping(
    acceptance: AcceptedThreshold,
) -> Mapping[str, object]:
    return MappingProxyType(
        {
            "marker_id": acceptance.marker_id,
            "validation_domain": acceptance.validation_domain,
            "threshold": acceptance.threshold,
            "outcome": acceptance.outcome,
            "fit_scope": acceptance.fit_scope,
            **{field: getattr(acceptance, field) for field in (
                "proposed_threshold", "proposed_outcome", "proposed_score_cutoff", "score_cutoff", "adjustment_source")},
            "score_provenance": canonical_bytes(dict(acceptance.score_provenance)).decode("utf-8"),
            "slide_thresholds": tuple(MappingProxyType(asdict(item)) for item in acceptance.slide_thresholds),
            "selection_source": acceptance.selection_source,
            "reviewer_id": acceptance.reviewer_id,
            "review_record_id": acceptance.review_record_id,
            "section_overrides": tuple(
                MappingProxyType(
                    {
                        "section_id": item.section_id,
                        "threshold": item.threshold,
                        "outcome": item.outcome,
                        "selection_source": item.selection_source,
                        "reason": item.reason,
                        "reviewer_id": item.reviewer_id,
                        "review_record_id": item.review_record_id,
                    }
                )
                for item in acceptance.section_overrides
            ),
        }
    )


def _domain_authority_identity(
    values: Mapping[str, object],
    *,
    include_digest: bool,
) -> Mapping[str, object]:
    identity = {
        "source_kind": values["source_kind"],
        "validation_domain_id": values["validation_domain_id"],
        "canonical_marker_ids": values["canonical_marker_ids"],
        "marker_settings": canonical_bytes(values["marker_settings"]).decode("utf-8"),
        "marker_settings_sha256": values["marker_settings_sha256"],
        "setup_lineage": (
            None
            if values["setup_lineage"] is None
            else canonical_bytes(values["setup_lineage"]).decode("utf-8")
        ),
        "threshold_fit_policy_sha256": values["threshold_fit_policy_sha256"],
        "measurement_authority_bindings": values["measurement_authority_bindings"],
        "evidence_sha256": values["evidence_sha256"],
        "accepted_thresholds": tuple(
            _accepted_threshold_mapping(item) for item in values["accepted_thresholds"]
        ),
        "node_id": values["node_id"],
        "attempt_id": values["attempt_id"],
        "attempt_authorization_sha256": values["attempt_authorization_sha256"],
        "admission_plan_sha256": values["admission_plan_sha256"],
        "compatibility_signature_sha256": values["compatibility_signature_sha256"],
        "interaction_target_id": values["interaction_target_id"],
        "interaction_group_id": values["interaction_group_id"],
        "interaction_group_sha256": values["interaction_group_sha256"],
        "interaction_response_sha256": values["interaction_response_sha256"],
        "interaction_request_id": values["interaction_request_id"],
        "interaction_context_sha256": values["interaction_context_sha256"],
        "interaction_checkpoint_id": values["interaction_checkpoint_id"],
        "interaction_predecessor_attempt_id": values["interaction_predecessor_attempt_id"],
        "interaction_predecessor_authorization_sha256": values[
            "interaction_predecessor_authorization_sha256"
        ],
        "interaction_predecessor_terminal_record_sha256": values[
            "interaction_predecessor_terminal_record_sha256"
        ],
        "interaction_action": values["interaction_action"],
        "interaction_response_validator_id": values["interaction_response_validator_id"],
        "interaction_response_validator_version": values["interaction_response_validator_version"],
    }
    if include_digest:
        identity["domain_gate_sha256"] = values["domain_gate_sha256"]
    return MappingProxyType(identity)


def _domain_authority_seal_material(
    values: Mapping[str, object],
) -> Mapping[str, object]:
    current = values["_current_execution"]
    evidence = values["_evidence"]
    request = values["_request"]
    assert isinstance(current, BatchCurrentExecutionAuthority)
    assert isinstance(evidence, MarkerGateEvidence)
    assert isinstance(request, StageResult)
    return {
        "identity": _domain_authority_identity(values, include_digest=True),
        "current_execution_binding": id(current),
        "current_execution_seal_sha256": current._seal_sha256,
        "analysis_domain_bindings": tuple(
            item.to_mapping() for item in values["_analysis_domain_bindings"]
        ),
        "configuration_bindings": tuple(
            item.to_mapping() for item in values["_configuration_bindings"]
        ),
        "evidence_binding": id(evidence),
        "evidence_seal_sha256": evidence._seal_sha256,
        "accepted_threshold_bindings": tuple(id(item) for item in values["accepted_thresholds"]),
        "request": request.to_mapping(),
    }


def _issue_domain_authority(
    values: Mapping[str, object],
) -> MarkerGateDomainAuthority:
    issued = dict(values)
    issued["domain_gate_sha256"] = digest(_domain_authority_identity(issued, include_digest=False))
    issued["_seal_sha256"] = digest(_domain_authority_seal_material(issued))
    issued["_issuer"] = _DOMAIN_AUTHORITY_ISSUER
    authority = object.__new__(MarkerGateDomainAuthority)
    for name in MarkerGateDomainAuthority.__dataclass_fields__:
        object.__setattr__(authority, name, issued[name])
    return authority


def _allowed_section_ids(evidence: MarkerGateEvidence) -> tuple[str, ...]:
    section_ids = tuple(
        sorted(
            {
                section_id
                for item in evidence._acceptance_evidence
                for section_id, _rows, _review in item.section_reviews
            }
        )
    )
    if not section_ids:
        raise BatchPhenotypingError("marker gate evidence lacks section scope")
    return section_ids


def _load_retry_response(
    evidence: MarkerGateEvidence,
    request: StageResult,
    state_store: BatchStateStore,
) -> tuple[MarkerGateResponse, Mapping[str, object]] | None:
    authorization = evidence._population._context.authorization
    try:
        response = state_store.load_validated_interaction_response(authorization)
    except (BatchStateError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("marker gate retry response is not authenticated") from exc
    predecessor = authorization.interaction_predecessor_terminal_record_sha256
    if response is None:
        if (
            authorization.interaction_response_sha256 is not None
            or predecessor is not None
            or authorization.retry_ordinal != 0
        ):
            raise BatchPhenotypingError("marker gate authorization has incomplete retry lineage")
        return None
    context_sha256 = digest(request.to_mapping()["resume_context"])
    try:
        request_id = batch_interaction_request_id(
            node_id=evidence.node_id,
            attempt_id=response["prior_attempt_id"],
            attempt_authorization_sha256=(response["prior_attempt_authorization_sha256"]),
            interaction_context_sha256=context_sha256,
        )
        persisted_request = state_store.load_interaction_request(request_id)
        if (
            authorization.retry_ordinal <= 0
            or authorization.interaction_response_sha256 != response["response_sha256"]
            or predecessor is None
            or predecessor != response["prior_terminal_record_sha256"]
            or response["node_id"] != evidence.node_id
            or response["interaction_request_id"] != request_id
            or response["interaction_context_sha256"] != context_sha256
            or response["action"] != InteractionAction.REVIEW_MARKER_THRESHOLDS.value
            or response["response_validator_id"] != _RESPONSE_VALIDATOR_ID
            or response["response_validator_version"] != _RESPONSE_VALIDATOR_VERSION
            or persisted_request != request
            or persisted_request.checkpoint_id != request.checkpoint_id
            or request.checkpoint_id is None
        ):
            raise BatchPhenotypingError("marker gate retry lineage differs from its request")
        decoded = decode_marker_gate_response(
            canonical_bytes(response["response"]),
            expected_validation_domain_id=evidence.validation_domain_id,
            expected_evidence_sha256=evidence.evidence_sha256,
            expected_marker_ids=tuple(item.marker_id for item in evidence._acceptance_evidence),
            allowed_section_ids=_allowed_section_ids(evidence),
            allowed_slide_ids=tuple(sorted({image_id for marker in evidence._acceptance_evidence for image_id, _rows, _review in marker.slide_reviews})),
        )
    except (
        BatchStateError,
        MarkerResponseError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        if isinstance(exc, BatchPhenotypingError):
            raise
        raise BatchPhenotypingError(
            "marker gate retry response differs from authenticated scope"
        ) from exc
    return decoded, response


def _review_for_selection(
    review: ThresholdReview,
    rows: tuple[_MarkerThresholdRow, ...],
    *,
    selection_source: str,
    manual_threshold: float | int | None,
) -> ThresholdReview:
    if selection_source != "manual":
        return review
    if manual_threshold is None:
        raise BatchPhenotypingError("manual marker selection lacks a threshold")
    return _threshold_review(
        rows,
        ThresholdFitPolicy(),
        manual_threshold=float(manual_threshold),
        methods=(),
    )


def _selected_decision(candidate, selection) -> dict[str, object]:
    """Retain the model proposal while applying an explicitly reviewed adjustment."""
    if candidate.status != "available":
        raise ThresholdServiceError("selection is not an available retained candidate")
    decision = {"threshold": candidate.threshold, "outcome": candidate.outcome}
    if candidate.outcome == "direct_scores":
        decision["score_cutoff"] = float(candidate.parameters["score_cutoff"])
    if selection.adjustment_source is None:
        return decision
    decision.update(proposed_threshold=candidate.threshold, proposed_outcome=candidate.outcome,
                    adjustment_source=selection.adjustment_source)
    if selection.adjusted_score_cutoff is not None:
        if candidate.outcome != "direct_scores":
            raise ThresholdServiceError("score adjustment requires a direct score candidate")
        decision.update(proposed_score_cutoff=float(candidate.parameters["score_cutoff"]),
                        score_cutoff=float(selection.adjusted_score_cutoff))
    else:
        if candidate.outcome == "direct_scores":
            raise ThresholdServiceError("direct scores require a score cutoff adjustment")
        decision.update(threshold=float(selection.adjusted_threshold), outcome="threshold")
    return decision


def _validate_projected_marker_selections(context, response):
    """Check fitted-method availability and complete image coverage before saving a response."""
    indexed = {item["marker_id"]: item for item in context["marker_reviews"]}
    for selection in response.selections:
        marker = indexed[selection.marker_id]
        slides = {item["image_identity"]: item["review"] for item in marker["per_slide_reviews"]}
        sections = {item["section_identity"]: item["review"] for item in marker["per_section_reviews"]}
        if marker["fit_scope"] == "per_slide":
            if {item.image_identity for item in selection.slide_overrides} != set(slides):
                raise MarkerResponseError("Select a marker decision for every image")
            pairs = [(slides[item.image_identity], item) for item in selection.slide_overrides]
        else:
            if selection.slide_overrides:
                raise MarkerResponseError("pooled review cannot include separate image decisions")
            pairs = [(marker["pooled_review"], selection)]
        pairs.extend((sections[item.section_id], item) for item in selection.section_overrides)
        for review, choice in pairs:
            if choice.selection_source == "manual":
                continue
            raw = next((item for item in review["candidates"] if item["method"] == choice.selection_source), None)
            if raw is None or raw["status"] != "available":
                raise MarkerResponseError("the selected method has no available saved fit")


def _accept_marker_thresholds(
    evidence: MarkerGateEvidence,
    response: MarkerGateResponse,
) -> tuple[AcceptedThreshold, ...]:
    if getattr(evidence, "evidence_sha256", response.evidence_sha256) != response.evidence_sha256:
        raise BatchPhenotypingError("marker interpretation refers to different review evidence")
    retained = {item.marker_id: item for item in evidence._acceptance_evidence}
    accepted: list[AcceptedThreshold] = []
    try:
        for selection in response.selections:
            marker = retained[selection.marker_id]
            section_reviews = {
                section_id: (rows, review) for section_id, rows, review in marker.section_reviews
            }
            overrides: list[SectionThresholdOverride] = []
            for override in selection.section_overrides:
                rows, owner_review = section_reviews[override.section_id]
                selected_review = _review_for_selection(
                    owner_review,
                    rows,
                    selection_source=override.selection_source,
                    manual_threshold=override.manual_threshold,
                )
                candidate = selected_review.candidate(override.selection_source)
                if candidate.status != "available":
                    raise ThresholdServiceError(
                        "section selection is not an available owner candidate"
                    )
                overrides.append(
                    SectionThresholdOverride(
                        section_id=override.section_id,
                        threshold=candidate.threshold,
                        outcome=candidate.outcome,
                        selection_source=override.selection_source,
                        reason=override.reason,
                        reviewer_id=override.reviewer_id,
                        review_record_id=override.review_record_id,
                    )
                )
            if marker.fit_scope == "per_slide":
                if not marker.slide_reviews:
                    raise BatchPhenotypingError("No cells in the selected region; choose another population before accepting a gate")
                chosen = {item.image_identity: item for item in selection.slide_overrides}
                expected_slides = {image_id for image_id, _rows, _review in marker.slide_reviews}
                if set(chosen) != expected_slides:
                    raise BatchPhenotypingError("per-slide acceptance requires explicit complete slide coverage")
                slides = []
                for image_id, rows, review in marker.slide_reviews:
                    override = chosen[image_id]
                    selected = _review_for_selection(review, rows, selection_source=override.selection_source,
                        manual_threshold=override.manual_threshold)
                    candidate = selected.candidate(override.selection_source)
                    if candidate.status != "available":
                        raise ThresholdServiceError("slide selection is not an available retained candidate")
                    slides.append(SlideThresholdOverride(image_id=image_id, **_selected_decision(candidate, override),
                        selection_source=override.selection_source, reason=override.reason,
                        reviewer_id=override.reviewer_id, review_record_id=override.review_record_id))
                accepted.append(AcceptedThreshold(marker_id=selection.marker_id,
                    validation_domain=evidence.validation_domain_id, threshold=None, outcome="per_slide",
                    fit_scope="per_slide", slide_thresholds=tuple(slides), selection_source=selection.selection_source,
                    reviewer_id=selection.reviewer_id, review_record_id=selection.review_record_id,
                    section_overrides=tuple(overrides),
                    score_provenance=marker.slide_reviews[0][2].candidate("nimbus").parameters.get("score_provenance", {}) if any(item.method == "nimbus" for item in marker.slide_reviews[0][2].candidates) else {}))
                continue
            if selection.slide_overrides:
                raise BatchPhenotypingError("pooled acceptance cannot include per-slide selections")
            selected_pooled_review = _review_for_selection(
                marker.pooled_review,
                marker.rows,
                selection_source=selection.selection_source,
                manual_threshold=selection.manual_threshold,
            )
            accepted.append(
                replace(accept_threshold(
                    selected_pooled_review,
                    marker_id=selection.marker_id,
                    validation_domain=evidence.validation_domain_id,
                    method=selection.selection_source,
                    reviewer_id=selection.reviewer_id,
                    review_record_id=selection.review_record_id,
                    section_overrides=tuple(overrides),
                ),
                    **_selected_decision(selected_pooled_review.candidate(selection.selection_source), selection))
            )
        indexed = index_accepted_thresholds(accepted)
    except (
        KeyError,
        MarkerResponseError,
        ThresholdServiceError,
        TypeError,
        ValueError,
    ) as exc:
        accepted.clear()
        if isinstance(exc, BatchPhenotypingError):
            raise
        raise BatchPhenotypingError(
            "marker gate selections cannot be resolved by the threshold owner"
        ) from exc
    expected = {
        (item.marker_id, evidence.validation_domain_id) for item in evidence._acceptance_evidence
    }
    if set(indexed) != expected:
        accepted.clear()
        raise BatchPhenotypingError("marker gate acceptances do not exactly cover the domain")
    return tuple(accepted)


def _accepted_domain_authority(
    evidence: MarkerGateEvidence,
    request: StageResult,
    response: MarkerGateResponse,
    envelope: Mapping[str, object],
    *, accepted_thresholds: tuple[AcceptedThreshold, ...] | None = None,
) -> MarkerGateDomainAuthority:
    population = evidence._population
    authorization = population._context.authorization
    accepted = accepted_thresholds or _accept_marker_thresholds(evidence, response)
    if population.marker_workflow:
        from .marker_application_execution import _threshold
        accepted = tuple(_threshold(item["accepted_threshold"]) for item in population.marker_workflow["completed"]) + accepted
    if request.checkpoint_id is None:
        raise BatchPhenotypingError("marker gate request lacks a checkpoint")
    return _issue_domain_authority(
        {
            "source_kind": "accepted_interaction",
            "validation_domain_id": evidence.validation_domain_id,
            "canonical_marker_ids": evidence.canonical_marker_ids,
            "marker_settings": evidence.marker_settings,
            "marker_settings_sha256": evidence.marker_settings_sha256,
            "setup_lineage": evidence.setup_lineage,
            "threshold_fit_policy_sha256": (evidence.threshold_fit_policy_sha256),
            "measurement_authority_bindings": tuple(
                evidence.request_context["measurement_authority_bindings"]
            ),
            "evidence_sha256": evidence.evidence_sha256,
            "accepted_thresholds": accepted,
            "node_id": evidence.node_id,
            "attempt_id": authorization.attempt_id,
            "attempt_authorization_sha256": (authorization.authorization_sha256),
            "admission_plan_sha256": authorization.admission_plan_sha256,
            "compatibility_signature_sha256": (population.compatibility_signature_sha256),
            "interaction_target_id": envelope["target_id"],
            "interaction_group_id": envelope["group_id"],
            "interaction_group_sha256": envelope["group_sha256"],
            "interaction_response_sha256": envelope["response_sha256"],
            "interaction_request_id": envelope["interaction_request_id"],
            "interaction_context_sha256": envelope["interaction_context_sha256"],
            "interaction_checkpoint_id": request.checkpoint_id,
            "interaction_predecessor_attempt_id": envelope["prior_attempt_id"],
            "interaction_predecessor_authorization_sha256": envelope[
                "prior_attempt_authorization_sha256"
            ],
            "interaction_predecessor_terminal_record_sha256": envelope[
                "prior_terminal_record_sha256"
            ],
            "interaction_action": envelope["action"],
            "interaction_response_validator_id": envelope["response_validator_id"],
            "interaction_response_validator_version": envelope["response_validator_version"],
            "_current_execution": population._current_execution,
            "_analysis_domain_bindings": (population._current_execution.analysis_domain_bindings),
            "_configuration_bindings": (population._current_execution.configuration_bindings),
            "_evidence": evidence,
            "_request": request,
        }
    )


def _nimbus_distillation_request(evidence: MarkerGateEvidence, response: MarkerGateResponse) -> StageResult | None:
    requested = {item.marker_id for item in response.selections if item.nimbus_distill}
    if not requested:
        return None
    retained = []
    projections = []
    for marker, projection in zip(evidence._acceptance_evidence, evidence.request_context["marker_reviews"], strict=True):
        if marker.marker_id not in requested:
            retained.append(marker)
            projections.append(projection)
            continue
        reviews = marker.slide_reviews if marker.fit_scope == "per_slide" else ((None, marker.rows, marker.pooled_review),)
        direct = reviews[0][2].candidate("nimbus")
        scores, _provenance = _nimbus_data(evidence._population, marker.marker_id,
            expected_sha256=direct.parameters["score_provenance"]["sha256"],
            score_path=direct.parameters["score_provenance"]["path"])
        selection = next(item for item in response.selections if item.marker_id == marker.marker_id)
        slide_selections = {item.image_identity: item for item in selection.slide_overrides}
        updated = []
        for identity, rows, review in reviews:
            chosen = slide_selections[identity] if identity is not None else selection
            teacher_cutoff = chosen.adjusted_score_cutoff
            if teacher_cutoff is None:
                teacher_cutoff = review.candidate("nimbus").parameters["score_cutoff"]
            if any(item.method == "nimbus_distilled" for item in review.candidates):
                raise BatchPhenotypingError("Nimbus distillation was already reviewed; select its retained result")
            distillation = _threshold_review(rows, ThresholdFitPolicy(), methods=("nimbus",),
                nimbus_scores=tuple(scores[row.cell_identity] for row in rows),
                nimbus_score_cutoff=teacher_cutoff)
            candidate = replace(distillation.candidate("nimbus"), method="nimbus_distilled",
                parameters={**distillation.candidate("nimbus").parameters,
                    "score_provenance": direct.parameters["score_provenance"]})
            updated.append((identity, rows, replace(review, candidates=(*review.candidates, candidate))))
        mutable = dict(projection)
        if marker.fit_scope == "per_slide":
            marker = replace(marker, slide_reviews=tuple(updated))
            mutable["per_slide_reviews"] = tuple({**_image_fields(rows[0].identity[:3]), "review": _review_projection(review, rows, excluded_count=next(item["review"].get("excluded_count", 0) for item in projection["per_slide_reviews"] if item["image_identity"] == _identity))} for _identity, rows, review in updated)
        else:
            marker = replace(marker, pooled_review=updated[0][2])
            mutable["pooled_review"] = _review_projection(marker.pooled_review, marker.rows, excluded_count=projection["pooled_review"].get("excluded_count", 0))
        mutable["nimbus_distillation_requested"] = True
        retained.append(marker)
        projections.append(_canonical_projection(mutable, "nimbus_distillation_review"))
    context = {key: value for key, value in evidence.request_context.items() if key != "evidence_sha256"}
    context["marker_reviews"] = tuple(projections)
    evidence_sha256 = digest(context)
    context["evidence_sha256"] = evidence_sha256
    values = dict(_evidence_values(evidence))
    values.update(request_context=MappingProxyType(context), evidence_sha256=evidence_sha256,
                  _acceptance_evidence=tuple(retained))
    return _marker_gate_request(_issue_evidence(values))


def build_marker_software_setup_request(
    population: AuthenticatedMarkerMeasurementPopulation,
    requirement,
    *,
    state_store: BatchStateStore,
) -> StageResult:
    """Retain the current marker and accepted prefix while its software is repaired.

    This is an ordinary marker setup interaction, so correcting a path or
    installing the checkpoint returns through the existing authenticated
    response and execution lifecycle. No failed-node retry is required.
    """
    from mif_pipeline.orchestration.setup.nimbus_requirements import NimbusSoftwareRequirement

    if type(requirement) is not NimbusSoftwareRequirement:
        raise TypeError("Marker software recovery requires a typed Nimbus requirement")
    _reauthenticate_population(population, state_store=state_store)
    workflow = population.marker_workflow
    if workflow is None:
        raise BatchPhenotypingError("Nimbus software recovery requires the current marker workflow")
    marker_ids, offered = marker_feature_candidates(population.measurement_projections)
    if marker_settings_locked({'marker_workflow': workflow}):
        marker_ids, offered = configured_marker_catalog(population.marker_settings,
            available_marker_ids=marker_ids, offered_marker_features=offered)
    return _marker_setup_request(
        current=population._current_execution,
        measurements=population.measurement_projections, samples={},
        marker_ids=marker_ids, offered=offered,
        initial_settings=population.marker_settings,
        workflow=_marker_sequence_progress(workflow),
        retained_bindings=population.measurement_authority_bindings,
        source_refs_by_measurement=population._population_source_refs,
        software_requirement=requirement.to_mapping(),
    )


def _next_marker_setup_request(evidence: MarkerGateEvidence,
                               accepted: tuple[AcceptedThreshold, ...]) -> StageResult | None:
    """Persist a confirmed marker and request the next marker's unfitted distribution."""
    population = evidence._population
    workflow = population.marker_workflow
    if workflow is None:
        return None
    current = workflow["current_marker_id"]
    index = population.canonical_marker_ids.index(current)
    if index == len(population.canonical_marker_ids) - 1 and 'return_marker_id' not in workflow:
        return None
    if len(accepted) != 1 or accepted[0].marker_id != current:
        raise BatchPhenotypingError("sequential review did not confirm exactly its current marker")
    rule = next(item for item in population.marker_settings["marker_rules"] if item["marker_id"] == current)
    completed = (*workflow["completed"], {"accepted_threshold": _accepted_threshold_mapping(accepted[0]),
        "review_evidence_sha256": evidence.evidence_sha256, "marker_rule": rule})
    if 'return_marker_id' in workflow:
        completed = (*completed, *workflow['retained_completed'])
        next_workflow = dict(version=workflow['version'], current_marker_id=workflow['return_marker_id'],
            completed=completed, settings_locked=True)
    else:
        next_workflow = {**_marker_sequence_progress(workflow),
            "current_marker_id": population.canonical_marker_ids[index + 1], "completed": completed}
    marker_ids, offered = marker_feature_candidates(population.measurement_projections)
    marker_ids, offered = configured_marker_catalog(population.marker_settings,
        available_marker_ids=marker_ids, offered_marker_features=offered)
    return _marker_setup_request(current=population._current_execution,
        measurements=population.measurement_projections, samples={}, marker_ids=marker_ids,
        offered=offered, initial_settings=population.marker_settings, workflow=next_workflow,
        retained_bindings=population.measurement_authority_bindings,
        source_refs_by_measurement=population._population_source_refs)


def _refit_marker_setup_request(evidence: MarkerGateEvidence) -> StageResult:
    """Reopen the current marker choices while retaining the accepted prefix.

    No decision from the rejected review is carried into the new setup. The usual
    setup response and fitting lifecycle creates fresh evidence and unchecked
    interpretation controls for the revised method, bounds or population.
    """
    population = evidence._population
    workflow = population.marker_workflow
    if workflow is None:
        raise BatchPhenotypingError("refitting requires the current marker workflow")
    marker_ids, offered = marker_feature_candidates(population.measurement_projections)
    if marker_settings_locked({'marker_workflow': workflow}):
        marker_ids, offered = configured_marker_catalog(population.marker_settings,
            available_marker_ids=marker_ids, offered_marker_features=offered)
    return _marker_setup_request(current=population._current_execution,
        measurements=population.measurement_projections, samples={}, marker_ids=marker_ids,
        offered=offered, initial_settings=population.marker_settings,
        workflow=_marker_sequence_progress(workflow),
        retained_bindings=population.measurement_authority_bindings,
        source_refs_by_measurement=population._population_source_refs)


def resolve_marker_gate_domain_authority(
    evidence: MarkerGateEvidence,
    *,
    state_store: BatchStateStore,
) -> MarkerGateDomainAuthority | StageResult:
    """Request review or resolve one authenticated accepted retry."""

    _reauthenticate_evidence(evidence, state_store)
    request = _marker_gate_request(evidence)
    authorization = evidence._population._context.authorization
    if evidence._population.marker_workflow and authorization.interaction_predecessor_terminal_record_sha256:
        predecessor = state_store.load_attempt(authorization.interaction_predecessor_terminal_record_sha256)
        if predecessor.stage_result.interaction_reason is InteractionReason.MARKER_COMPOSITE_RULES_REQUIRED:
            return request
        if (evidence.setup_lineage is not None
                and evidence.setup_lineage.get('interaction_action') == InteractionAction.REVIEW_MARKER_THRESHOLDS.value
                and evidence.setup_lineage.get('interaction_response_sha256') == authorization.interaction_response_sha256):
            return request
    if evidence.setup_lineage is not None and authorization.retry_ordinal == 1:
        return request
    resolved = _load_retry_response(evidence, request, state_store)
    if resolved is None:
        return request
    response, envelope = resolved
    if response.refit_current_marker:
        return _refit_marker_setup_request(evidence)
    followup = _nimbus_distillation_request(evidence, response)
    if followup is not None:
        return followup
    accepted = _accept_marker_thresholds(evidence, response)
    next_marker = _next_marker_setup_request(evidence, accepted)
    if next_marker is not None:
        return next_marker
    return _accepted_domain_authority(evidence, request, response, envelope, accepted_thresholds=accepted)


def _domain_authority_values(
    authority: MarkerGateDomainAuthority,
) -> Mapping[str, object]:
    return {
        name: getattr(authority, name)
        for name in MarkerGateDomainAuthority.__dataclass_fields__
        if name not in {"_seal_sha256", "_issuer"}
    }


def _reauthenticate_domain_authority(
    authority: MarkerGateDomainAuthority,
) -> Mapping[str, object]:
    if type(authority) is not MarkerGateDomainAuthority:
        raise TypeError("marker gate output requires exact domain authority")
    values = _domain_authority_values(authority)
    current = authority._current_execution
    evidence = authority._evidence
    try:
        indexed = index_accepted_thresholds(authority.accepted_thresholds)
    except (ThresholdServiceError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("marker gate authority threshold cover is invalid") from exc
    expected_cover = {
        (marker_id, authority.validation_domain_id) for marker_id in authority.canonical_marker_ids
    }
    if (
        authority._issuer is not _DOMAIN_AUTHORITY_ISSUER
        or authority._seal_sha256 != digest(_domain_authority_seal_material(values))
        or authority.domain_gate_sha256
        != digest(_domain_authority_identity(values, include_digest=False))
        or authority.source_kind != "accepted_interaction"
        or tuple(sorted(authority.canonical_marker_ids)) != authority.canonical_marker_ids
        or tuple(item.marker_id for item in authority.accepted_thresholds)
        != authority.canonical_marker_ids
        or not all(
            type(item) is AcceptedThreshold
            and item.validation_domain == authority.validation_domain_id
            and all(
                type(override) is SectionThresholdOverride for override in item.section_overrides
            )
            for item in authority.accepted_thresholds
        )
        or set(indexed) != expected_cover
        or current.node_kind != _NODE_KIND
        or current.node_scope != "validation_domain"
        or current.validation_domain_id != authority.validation_domain_id
        or current.node_id != authority.node_id
        or current.authorization_sha256 != authority.attempt_authorization_sha256
        or evidence.attempt_id != authority.attempt_id
        or evidence.attempt_authorization_sha256 != authority.attempt_authorization_sha256
        or evidence.admission_plan_sha256 != authority.admission_plan_sha256
        or evidence.compatibility_signature_sha256 != authority.compatibility_signature_sha256
        or evidence.validation_domain_id != authority.validation_domain_id
        or evidence.canonical_marker_ids != authority.canonical_marker_ids
        or evidence.marker_settings != authority.marker_settings
        or evidence.marker_settings_sha256 != authority.marker_settings_sha256
        or evidence.setup_lineage != authority.setup_lineage
        or authority.marker_settings_sha256 != digest(authority.marker_settings)
        or evidence.threshold_fit_policy_sha256 != authority.threshold_fit_policy_sha256
        or evidence.evidence_sha256 != authority.evidence_sha256
        or tuple(evidence.request_context["measurement_authority_bindings"])
        != authority.measurement_authority_bindings
        or authority._analysis_domain_bindings != current.analysis_domain_bindings
        or authority._configuration_bindings != current.configuration_bindings
        or not authority._analysis_domain_bindings
        or not authority._configuration_bindings
    ):
        raise BatchPhenotypingError("marker gate domain authority is invalid or incomplete")
    return values


def build_marker_gate_execution_result(
    authority: MarkerGateDomainAuthority,
) -> BatchNodeExecutionResult:
    """Build the locatorless committed edge for one accepted domain gate."""

    values = _reauthenticate_domain_authority(authority)
    identity = _domain_authority_identity(values, include_digest=True)
    contract = marker_gate_domain_identity_contract()
    artifact = BatchArtifactRef(
        artifact_id="marker-gate-" + authority.domain_gate_sha256[:48],
        artifact_kind="marker_gate",
        schema_name=_DOMAIN_AUTHORITY_SCHEMA_NAME,
        schema_version=6,
        origin=ArtifactOrigin.INTERNAL_NODE,
        producer_node_id=authority.node_id,
        producer_attempt_id=authority.attempt_id,
        producer_attempt_authorization_sha256=(authority.attempt_authorization_sha256),
        external_declaration_sha256=None,
        external_validation_receipt_sha256=None,
        locator=None,
        sha256=None,
        byte_count=None,
        scope=ArtifactScope(
            validation_domain_id=authority.validation_domain_id,
            sample_id=None,
            slide_id=None,
            project_entry_id=None,
            analysis_domain_bindings=authority._analysis_domain_bindings,
        ),
        owning_identity=identity,
        identity_contract=contract,
        owning_identity_contract_sha256=contract.contract_sha256,
        producer_version=_DOMAIN_AUTHORITY_PRODUCER_VERSION,
        runtime_versions={"mif_pipeline": MIF_PIPELINE_BATCH_RUNTIME_VERSION},
        compatibility_signature_sha256=(authority.compatibility_signature_sha256),
        configuration_bindings=authority._configuration_bindings,
    )
    return BatchNodeExecutionResult(
        authority.attempt_authorization_sha256,
        (artifact,),
    )


def _canonical_measurement_bindings(
    population: AuthenticatedMarkerMeasurementPopulation,
) -> tuple[Mapping[str, object], ...]:
    keyed: list[tuple[tuple[str, str, str, str, str], Mapping[str, object]]] = []
    for binding in population.measurement_authority_bindings:
        try:
            key = (
                str(binding["sample_id"]),
                str(binding["project_entry_id"]),
                str(binding["slide_id"]),
                str(binding["section_id"]),
                str(binding["measurements_result_ref_sha256"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BatchPhenotypingError(
                "marker evidence measurement binding is incomplete"
            ) from exc
        projected = _canonical_projection(binding, "measurement_authority_binding")
        assert isinstance(projected, Mapping)
        keyed.append((key, projected))
    keyed.sort(key=lambda item: item[0])
    if not keyed or len({key for key, _binding in keyed}) != len(keyed):
        raise BatchPhenotypingError("marker evidence measurement bindings are empty or duplicated")
    return tuple(binding for _key, binding in keyed)


def build_marker_gate_evidence(
    population: AuthenticatedMarkerMeasurementPopulation,
    *,
    state_store: BatchStateStore,
    nimbus_score_provider=None,
) -> MarkerGateEvidence:
    """Build deterministic threshold suggestions from one current population."""

    _reauthenticate_population(population, state_store=state_store)
    policy = ThresholdFitPolicy()
    policy_projection = _policy_projection(policy)
    policy_sha256 = digest(policy_projection)
    semantics_by_marker = {item.biomarker: item for item in population.marker_semantics}
    expected_semantics = tuple(item["marker_id"] for item in population.marker_settings["marker_rules"] if item["feature_id"] is not None)
    if tuple(sorted(semantics_by_marker)) != expected_semantics or len(
        semantics_by_marker
    ) != len(population.marker_semantics):
        raise BatchPhenotypingError(
            "marker evidence semantics differ from the authenticated markers"
        )
    persisted = _persisted_reviews(population, state_store)
    if persisted is not None:
        policy_projection = persisted["threshold_fit_policy"]
        policy_sha256 = digest(policy_projection)
        policy = ThresholdFitPolicy(**dict(policy_projection))
        prior_reviews = {item["marker_id"]: item for item in persisted["marker_reviews"]}
    current_markers = (population.marker_workflow["current_marker_id"],) if population.marker_workflow else population.canonical_marker_ids
    composed_reviews = tuple(
        _marker_review_projection(population, semantics_by_marker[marker_id], policy, nimbus_score_provider=nimbus_score_provider)
        if persisted is None else _restore_marker_review(population, semantics_by_marker[marker_id], prior_reviews[marker_id], policy)
        for marker_id in current_markers
    )
    marker_reviews = tuple(item[0] for item in composed_reviews)
    acceptance_evidence = tuple(item[1] for item in composed_reviews)
    measurement_bindings = _canonical_measurement_bindings(population)
    context_without_digest = _canonical_projection(
        {
            "validation_domain_id": population.validation_domain_id,
            "canonical_marker_ids": population.canonical_marker_ids,
            "marker_settings_sha256": population.marker_settings_sha256,
            "threshold_fit_policy": policy_projection,
            "measurement_authority_bindings": measurement_bindings,
            "marker_reviews": marker_reviews,
            **({"marker_workflow": population.marker_workflow} if population.marker_workflow else {}),
            **({'offered_marker_features': (persisted['offered_marker_features'] if persisted is not None
                else marker_feature_candidates(population.measurement_projections)[1])}
               if population.marker_workflow and (persisted is None or 'offered_marker_features' in persisted) else {}),
        },
        "marker_gate_request_context",
    )
    assert isinstance(context_without_digest, Mapping)
    evidence_sha256 = digest(context_without_digest)
    request_context = MappingProxyType(
        {**context_without_digest, "evidence_sha256": evidence_sha256}
    )
    values: Mapping[str, object] = {
        "validation_domain_id": population.validation_domain_id,
        "canonical_marker_ids": population.canonical_marker_ids,
        "marker_settings": population.marker_settings,
        "marker_settings_sha256": population.marker_settings_sha256,
        "setup_lineage": population.setup_lineage,
        "request_context": request_context,
        "evidence_sha256": evidence_sha256,
        "source_state_head_sha256": population.source_state_head_sha256,
        "active_plan_sha256": population.active_plan_sha256,
        "admission_plan_sha256": population.admission_plan_sha256,
        "node_id": population.node_id,
        "attempt_id": population.attempt_id,
        "attempt_authorization_sha256": (population.attempt_authorization_sha256),
        "compatibility_signature_sha256": (population.compatibility_signature_sha256),
        "threshold_fit_policy_sha256": policy_sha256,
        "_population": population,
        "_acceptance_evidence": acceptance_evidence,
        "_population_seal_sha256": population._seal_sha256,
        "_source_authority_seal_sha256s": tuple(
            item._seal_sha256 for item in population._committed_authorities
        ),
    }
    return _issue_evidence(values)


def open_authenticated_marker_measurement_population(
    context: BatchNodeExecutionContext,
    *,
    state_store: BatchStateStore,
    manifest: ValidatedBatchManifest,
) -> AuthenticatedMarkerMeasurementPopulation | StageResult:
    """Open one exact current marker-gate input population."""

    if type(context) is not BatchNodeExecutionContext:
        raise TypeError("marker population requires an exact execution context")
    if not isinstance(state_store, BatchStateStore):
        raise TypeError("marker population requires the exact state owner")
    if type(state_store) is not BatchStateStore:
        raise BatchPhenotypingError("marker population requires the exact state owner")
    if type(manifest) is not ValidatedBatchManifest:
        raise TypeError("marker population requires a retained manifest")
    try:
        current = state_store.authenticate_current_execution_context(context)
        validate_current_execution_authority(current, expected_context=context)
    except (BatchArtifactError, BatchStateError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError(
            "marker population context is not current and authenticated"
        ) from exc
    _validate_execution(context, current)
    if context.authorization.interaction_response_sha256 is not None:
        response = state_store.load_validated_interaction_response(context.authorization)
        if response is not None and response["action"] == InteractionAction.ABORT.value:
            request = state_store.load_interaction_request(response["interaction_request_id"])
            if (request.stage != _NODE_KIND
                    or request.interaction_reason not in {
                        InteractionReason.MARKER_COMPOSITE_RULES_REQUIRED,
                        InteractionReason.MARKER_THRESHOLD_REVIEW_REQUIRED}
                    or response["response_validator_id"] != _RESPONSE_VALIDATOR_ID
                    or response["response_validator_version"] != _RESPONSE_VALIDATOR_VERSION
                    or InteractionAction.ABORT.value not in request.allowed_actions
                    or response["response"]):
                raise BatchPhenotypingError("marker cancellation differs from its offered review")
            return StageResult.failed(stage=_NODE_KIND, slide_id=request.slide_id,
                error_code="marker_review_stopped",
                error_message="Marker review was stopped. Use Change options to reopen this step.")
    _reopened, samples = _validated_manifest(
        manifest,
        current=current,
        state_store=state_store,
    )
    authorities, measurements, modes = _authenticate_sources(
        context=context, current=current, state_store=state_store
    )
    source_snapshot = state_store.reopen_execution_snapshot(expected_head_sha256=current.state_head_sha256)
    population_source_refs = {item.artifact_ref.artifact_ref_sha256: tuple(sorted(population_source_references(
        current_population_sources(snapshot=source_snapshot, measurement=item, config=samples[item.sample_id].config))))
        for item in measurements}
    inherited_population = marker_population_from_modes(modes)
    resolved_settings = _resolve_marker_settings(
        context=context,
        current=current,
        state_store=state_store,
        samples=samples,
        measurements=measurements,
        population_settings=inherited_population,
    )
    if type(resolved_settings) is StageResult:
        return resolved_settings
    settings, setup_lineage = resolved_settings
    marker_ids_from_measurements, offered = marker_feature_candidates(measurements)
    marker_ids_from_measurements, offered = configured_marker_catalog(settings,
        available_marker_ids=marker_ids_from_measurements, offered_marker_features=offered)
    settings = validate_marker_settings(
        settings,
        canonical_marker_ids=marker_ids_from_measurements,
        offered_marker_features=offered,
        allow_pending=True,
    )
    if settings["phenotyping_population"] != inherited_population:
        raise BatchPhenotypingError("Marker fitting must use the cell selection accepted before the phenotyping method.")
    marker_ids, semantics, measurements_by_ref = _validate_measurements_and_modes(
        current=current,
        samples=samples,
        measurements=measurements,
        modes=modes,
        settings=settings,
        offered_marker_features=offered,
    )

    opened: list[
        tuple[
            tuple[str, str, str, str, str],
            ValidatedMeasurementsResultProjection,
            Mapping[str, object],
            tuple[_MarkerObservation, ...],
            tuple[FeatureSpec, ...],
        ]
    ] = []
    try:
        for measurement in measurements_by_ref.values():
            sample = samples[measurement.sample_id]
            observations, features, _row_inventory = _reopen_measurement_rows(
                measurement=measurement,
                sample=sample,
                marker_ids=tuple(item.biomarker for item in semantics),
                semantics=semantics,
                population_settings=settings.get("phenotyping_population"),
                expected_population_source_ref_sha256s=frozenset(population_source_refs[measurement.artifact_ref.artifact_ref_sha256]),
            )
            binding = _binding(measurement, sample)
            sort_key = (
                measurement.sample_id,
                measurement.project_entry_id,
                measurement.slide_id,
                measurement.section_id,
                measurement.artifact_ref.artifact_ref_sha256,
            )
            opened.append((sort_key, measurement, binding, observations, features))
    except (
        BatchPhenotypingError,
        MeasurementRowStreamError,
        TypeError,
        ValueError,
        OSError,
    ) as exc:
        opened.clear()
        raise BatchPhenotypingError("marker measurement rows cannot be authenticated") from exc
    opened.sort(key=lambda item: item[0])
    plan_sha256s = {item[1].measurement_plan_sha256 for item in opened}
    feature_sha256s = {item[1].feature_schema_sha256 for item in opened}
    marker_feature_sha256s = {feature_schema_sha256(item[4]) for item in opened}
    if len(plan_sha256s) != 1 or len(feature_sha256s) != 1 or len(marker_feature_sha256s) != 1:
        opened.clear()
        raise BatchPhenotypingError("marker measurement schemas differ across participants")
    observations = tuple(
        sorted(
            (observation for item in opened for observation in item[3]),
            key=lambda item: item.identity,
        )
    )
    identities = tuple(item.identity for item in observations)
    if (
        not observations
        or len(set(identities)) != len(identities)
        or len(observations) != sum(item[1].cell_count for item in opened)
        or any(
            tuple(marker for marker, _value in item.values_by_marker_id) != tuple(semantic.biomarker for semantic in semantics)
            for item in observations
        )
    ):
        opened.clear()
        raise BatchPhenotypingError("marker population composite cell inventory is invalid")
    ordered_measurements = tuple(item[1] for item in opened)
    bindings = tuple(item[2] for item in opened)
    ordered_authorities = tuple(
        sorted(
            authorities,
            key=lambda item: item.artifact_ref.artifact_ref_sha256,
        )
    )
    authorization = context.authorization
    workflow = None
    if authorization.interaction_predecessor_terminal_record_sha256 is not None:
        prior = state_store.load_attempt(authorization.interaction_predecessor_terminal_record_sha256)
        progress = prior.stage_result.resume_context.get("marker_workflow")
        if progress is not None:
            selected_progress = _marker_sequence_progress(progress)
            response = state_store.load_validated_interaction_response(authorization)
            if response is not None and 'revisit_marker_id' in response['response'] and 'marker_settings' in response['response']:
                from .marker_preparation import revision_progress
                selected_progress = revision_progress(progress, response['response']['revisit_marker_id'])
            if not progress.get('completed') and not progress.get('settings_locked', False):
                selected_progress['current_marker_id'] = marker_ids[0]
            workflow = _canonical_projection({**selected_progress,
                "settings": settings, "setup_lineage": setup_lineage}, "marker_workflow")
    values: Mapping[str, object] = {
        "validation_domain_id": current.validation_domain_id,
        "canonical_marker_ids": marker_ids,
        "marker_semantics": semantics,
        "marker_settings": settings,
        "marker_settings_sha256": digest(settings),
        "setup_lineage": setup_lineage,
        "marker_workflow": workflow,
        "_population_source_refs": population_source_refs,
        "measurement_projections": ordered_measurements,
        "measurement_authority_bindings": bindings,
        "population_cell_count": len(observations),
        "population_cell_inventory_sha256": digest(identities),
        "source_state_head_sha256": current.state_head_sha256,
        "active_plan_sha256": current.active_plan_sha256,
        "admission_plan_sha256": authorization.admission_plan_sha256,
        "node_id": current.node_id,
        "attempt_id": authorization.attempt_id,
        "attempt_authorization_sha256": authorization.authorization_sha256,
        "compatibility_signature_sha256": context.compatibility_signature_sha256,
        "_context": context,
        "_current_execution": current,
        "_committed_authorities": ordered_authorities,
        "_observations": observations,
    }
    return _issue_population(values)


__all__ = [
    "AuthenticatedMarkerMeasurementPopulation",
    "MarkerGateDomainAuthority",
    "MarkerGateEvidence",
    "build_marker_gate_evidence",
    "build_marker_gate_execution_result",
    "build_marker_software_setup_request",
    "marker_gate_domain_identity_contract",
    "open_authenticated_marker_measurement_population",
    "resolve_marker_gate_domain_authority",
    "validate_marker_gate_target_response",
]

def marker_review_initial_settings(revision, state_store):
    """Seed choices from the retained completion, requiring new marker confirmations.

    The saved terminal is a setup reference, not a selected gate for computation.
    Its settings are checked against the current measurement features by the
    ordinary setup validator. Failed predecessors start with fresh choices.
    """
    attempt = state_store.load_attempt(revision.predecessor_terminal_record_sha256)
    if attempt.node_id != revision.predecessor_node_id or attempt.stage_result.status not in {
        StageStatus.COMPLETED, StageStatus.FAILED
    }:
        raise BatchArtifactError("marker review revision differs from its retained settled task")
    if attempt.stage_result.status is StageStatus.FAILED:
        return None
    refs = tuple(ref for ref in attempt.output_artifacts if ref.artifact_kind == "marker_gate")
    if (len(refs) != 1 or refs[0].producer_node_id != revision.predecessor_node_id
            or refs[0].identity_contract != marker_gate_domain_identity_contract()):
        raise BatchArtifactError("marker review revision lacks its retained accepted gate")
    settings = json.loads(refs[0].owning_identity["marker_settings"])
    settings["marker_rules"] = tuple({
        "marker_id": rule["marker_id"], "feature_id": rule["feature_id"],
        "polarity": rule["polarity"], "method": None,
        "gaussian_variance": "shared", "component_location_constraints": {},
    } for rule in settings["marker_rules"])
    return settings
