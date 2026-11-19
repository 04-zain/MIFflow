"""State-authenticated owner for compact unsupervised preparation inputs.

The operation retains the selected stable-cell population, measurement generation and
phenotype view. Its records and stage adapters connect accepted numerical results to the
current batch target and reviewed application scope."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import stat
from collections.abc import Mapping
from typing import Final

from mif_pipeline.cell_analysis.measurement_plan import CellFeatureError, FeatureKind
from mif_pipeline.cell_analysis.measurement_rows import (
    MarkerIntensityRowsDescriptor,
    MeasurementRowStreamError,
    read_measurement_dataset_selection,
)
from mif_pipeline.pipeline_core import (
    ClusteringConfig,
    ClusteringMode,
    InteractionAction,
    InteractionReason,
    PhenotypingMode,
    ROIAnalysisError,
    StageResult,
    validate_interaction_response,
)

from ..batch_artifacts import (
    ArtifactConfigurationBinding,
    ArtifactMultiplicity,
    ArtifactOrigin,
    ArtifactProjectionTarget,
    ArtifactScope,
    BatchArtifactError,
    BatchArtifactRef,
    BatchCommittedArtifactAuthority,
    BatchCurrentExecutionAuthority,
    BatchNodeExecutionContext,
    BatchNodeExecutionResult,
    DispatchMode,
    validate_committed_artifact_authority_set,
    validate_current_execution_authority,
)
from ..batch_manifest import (
    BatchManifestError,
    ValidatedBatchManifest,
    revalidate_retained_manifest,
)
from ..batch_measurement_artifacts import decode_committed_measurements_result
from ..batch_phenotyping_mode_artifacts import decode_committed_phenotyping_mode
from ..batch_state import (
    BatchStateError,
    BatchStateStore,
    batch_interaction_request_id,
)
from ._common import (
    PREPARATION_PRODUCER_VERSION,
    BatchPhenotypingError,
    canonical_bytes,
    digest,
    feature_schema_sha256,
    plain_json,
    read_bounded_file,
)
from .authority import (
    unsupervised_preparation_identity_contract,
    unsupervised_preparation_owning_identity,
)
from .codec import persist_unsupervised_preparation
from .records import UnsupervisedPreparationInput

_NODE_KIND: Final[str] = "unsupervised_preparation"
_INTERACTION_SCHEMA_VERSION: Final[str] = "batch-clustering-setup/v2"
_RESPONSE_VALIDATOR_ID: Final[str] = "roi-interaction-validator"
_RESPONSE_VALIDATOR_VERSION: Final[str] = "roi-interaction-actions/v2"


# ==============================================================================
#                                  Preparation
# ==============================================================================


def _validated_manifest(
    manifest: ValidatedBatchManifest,
    *,
    execution: BatchCurrentExecutionAuthority,
    context: BatchNodeExecutionContext,
):
    if type(manifest) is not ValidatedBatchManifest:
        raise BatchPhenotypingError("preparation requires retained manifest authority")
    try:
        reopened = revalidate_retained_manifest(
            manifest.original_manifest_bytes, manifest.normalized_manifest_bytes
        )
    except (BatchManifestError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("preparation manifest authority is invalid") from exc
    if (
        reopened.manifest != manifest.manifest
        or reopened.original_manifest_bytes != manifest.original_manifest_bytes
        or reopened.original_manifest_sha256 != manifest.original_manifest_sha256
        or reopened.normalized_manifest_bytes != manifest.normalized_manifest_bytes
        or reopened.normalized_manifest_sha256 != manifest.normalized_manifest_sha256
        or reopened.samples != manifest.samples
    ):
        raise BatchPhenotypingError("retained preparation manifest changed")
    matching = tuple(
        item for item in reopened.enabled_samples if item.sample_id == execution.participant_ids[0]
    )
    if len(matching) != 1:
        raise BatchPhenotypingError("preparation participant is not one enabled sample")
    sample = matching[0]
    try:
        target = ArtifactProjectionTarget.from_manifest(
            reopened,
            execution.participant_ids,
            ArtifactScope(
                validation_domain_id=execution.validation_domain_id,
                sample_id=sample.sample_id,
                slide_id=sample.slide_id,
                project_entry_id=execution.project_entry_ids[0],
                analysis_domain_bindings=execution.analysis_domain_bindings,
            ),
        )
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("preparation manifest target is invalid") from exc
    if (
        target.target_sha256 != context.projection_target_sha256
        or target.configuration_bindings != execution.configuration_bindings
    ):
        raise BatchPhenotypingError("preparation manifest differs from current execution")
    return sample


def _validate_execution(
    context: BatchNodeExecutionContext,
    authority: BatchCurrentExecutionAuthority,
) -> BatchCurrentExecutionAuthority:
    if type(context) is not BatchNodeExecutionContext:
        raise BatchPhenotypingError("preparation requires an exact execution context")
    try:
        authority = validate_current_execution_authority(authority, expected_context=context)
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("preparation execution authority is invalid") from exc
    requirements = {item.artifact_kind: item for item in context.adapter_spec.required_inputs}
    if (
        authority.node_kind != _NODE_KIND
        or authority.node_scope != "sample"
        or authority.clustering_scope is not None
        or len(authority.participant_ids) != 1
        or len(authority.project_entry_ids) != 1
        or authority.validation_domain_id is None
        or not authority.analysis_domain_bindings
        or len(authority.configuration_bindings) != 1
        or context.adapter_spec.node_kind != _NODE_KIND
        or context.adapter_spec.dispatch_mode is not DispatchMode.SINGLE
        or context.adapter_spec.resource_kind != "scientific_compute"
        or tuple(context.adapter_spec.declared_outputs) != ("unsupervised_preparation_result",)
        or context.adapter_spec.output_identity_contracts
        != (unsupervised_preparation_identity_contract(),)
        or context.adapter_spec.optional_inputs
        or set(requirements) != {"measurements_result", "phenotyping_mode"}
        or requirements["measurements_result"].multiplicity is not ArtifactMultiplicity.SINGLE
        or requirements["phenotyping_mode"].multiplicity is not ArtifactMultiplicity.EXACT_SET
        or context.current_image_identity_sha256 is not None
        or context.script_sha256 is not None
        or context.project_identity_sha256 is not None
        or context.project_authority_head_sha256 is not None
        or context.project_qpproj_snapshot_sha256 is not None
        or context.project_entry_projection_sha256 is not None
    ):
        raise BatchPhenotypingError("preparation execution scope is invalid")
    return authority


def _validate_sources(
    *,
    context: BatchNodeExecutionContext,
    execution: BatchCurrentExecutionAuthority,
    measurement_authority: BatchCommittedArtifactAuthority,
    mode_authorities: tuple[BatchCommittedArtifactAuthority, ...],
):
    if (
        type(measurement_authority) is not BatchCommittedArtifactAuthority
        or type(mode_authorities) is not tuple
        or not all(type(item) is BatchCommittedArtifactAuthority for item in mode_authorities)
    ):
        raise BatchPhenotypingError("preparation sources require exact committed authorities")
    expected_mode_count = len(execution.analysis_domain_bindings)
    if len(mode_authorities) != expected_mode_count:
        raise BatchPhenotypingError("preparation requires one mode authority per analysis domain")
    try:
        sources = validate_committed_artifact_authority_set(
            (measurement_authority, *mode_authorities),
            expected_active_plan_sha256=execution.active_plan_sha256,
        )
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("preparation source authority set is invalid") from exc
    if (
        any(item.state_head_sha256 != execution.state_head_sha256 for item in sources)
        or tuple(sorted(item.artifact_ref.artifact_ref_sha256 for item in sources))
        != execution.input_artifact_ref_sha256s
        or tuple(sorted(item.artifact_ref.artifact_ref_sha256 for item in sources))
        != tuple(sorted(item.artifact_ref_sha256 for item in context.inputs))
    ):
        raise BatchPhenotypingError("preparation sources differ from current inputs")
    try:
        measurement = decode_committed_measurements_result(measurement_authority)
        modes = tuple(decode_committed_phenotyping_mode(item) for item in mode_authorities)
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("preparation source decoding failed") from exc
    return measurement, modes


def _validate_modes(
    *,
    modes: tuple[object, ...],
    execution: BatchCurrentExecutionAuthority,
    measurement,
    sample,
) -> None:
    expected = {item.analysis_domain_id: item for item in execution.analysis_domain_bindings}
    actual: dict[str, object] = {}
    for mode in modes:
        binding = mode.analysis_domain_binding
        domain_id = binding.analysis_domain_id
        if domain_id in actual:
            raise BatchPhenotypingError("preparation mode domains are duplicated")
        if (
            mode.selected_mode is not PhenotypingMode.UNSUPERVISED_CLUSTERING
            or mode.configured_mode != sample.config.phenotyping.mode
            or mode.measurements_result_ref_sha256 != measurement.artifact_ref.artifact_ref_sha256
            or mode.state_head_sha256 != execution.state_head_sha256
            or mode.active_plan_sha256 != execution.active_plan_sha256
            or mode.sample_id != sample.sample_id
            or mode.slide_id != sample.slide_id
            or mode.project_entry_id != execution.project_entry_ids[0]
            or mode.validation_domain_id != sample.validation_domain_id
            or mode.resolved_config_sha256 != sample.resolved_config_sha256
            or binding != expected.get(domain_id)
        ):
            raise BatchPhenotypingError("preparation mode authority differs")
        actual[domain_id] = mode
    if len({digest(plain_json(mode.population_selection)) for mode in modes}) != 1:
        raise BatchPhenotypingError("Choose the same phenotyping cells for all analysis regions in this image")
    if set(actual) != set(expected):
        raise BatchPhenotypingError("preparation mode authorities do not cover domains")


def _validate_measurement(*, measurement, execution, sample):
    ref = measurement.artifact_ref
    expected_configuration = (
        ArtifactConfigurationBinding(sample.sample_id, sample.resolved_config_sha256),
    )
    if (
        measurement.state_head_sha256 != execution.state_head_sha256
        or measurement.active_plan_sha256 != execution.active_plan_sha256
        or measurement.sample_id != sample.sample_id
        or measurement.slide_id != sample.slide_id
        or measurement.project_entry_id != execution.project_entry_ids[0]
        or ref.scope.validation_domain_id != sample.validation_domain_id
        or ref.scope.sample_id != sample.sample_id
        or ref.scope.slide_id != sample.slide_id
        or ref.scope.project_entry_id != execution.project_entry_ids[0]
        or ref.scope.analysis_domain_bindings != execution.analysis_domain_bindings
        or measurement.analysis_domain_bindings != execution.analysis_domain_bindings
        or ref.configuration_bindings != expected_configuration
        or execution.configuration_bindings != expected_configuration
        or ref.owning_identity.get("image_role") != "primary_mif"
        or measurement.execution_exchange_sha256
        != ref.owning_identity.get("execution_exchange_sha256")
    ):
        raise BatchPhenotypingError("preparation measurement authority differs")
    return ref


def _selected_measurement_authority(*, measurement, sample):
    """Reuse our checked measurement publication for compact preparation.

    We authenticate its small current selection and match the recorded feature
    order and section lineage. Preparation records the already validated counts
    and commitments; it does not replay all numerical columns before fitting.
    """
    marker_features=tuple(item for item in measurement.measurement_plan.features if item.kind==FeatureKind.EXPRESSION.value)
    reference=measurement.measurement_rows
    descriptor=MarkerIntensityRowsDescriptor(path=reference.path,sha256=reference.sha256,byte_count=reference.byte_count,row_count=reference.row_count,content_schema_version=reference.content_schema_version)
    selected=read_measurement_dataset_selection(descriptor,authenticated_output_root=reference.path.parent.parent.parent)
    if (tuple(selected['feature_ids'])!=tuple(item.feature_id for item in marker_features) or
            tuple(selected['measurement_names'])!=tuple(item.measurement for item in marker_features) or
            selected['section_id']!=measurement.section_id or selected['project_entry_id']!=measurement.project_entry_id or
            selected['segmentation_population_id']!=measurement.segmentation_population_id or
            selected['measurement_generation_id']!=measurement.measurement_generation_id or
            reference.row_count!=measurement.cell_count):
        raise BatchPhenotypingError('Clustering preparation differs from accepted measurement authority')
    return reference,marker_features


def _ensure_output_directory(sample_output_root: Path) -> Path:
    """Create the fixed stage directory under the manifest-owned sample root.

    This prototype trusts local same-user directory replacement after each
    lstat; files themselves are published with an atomic no-replace link.
    """

    root = sample_output_root
    if not root.is_absolute() or root == Path(root.anchor) or root.is_symlink():
        raise BatchPhenotypingError("sample output root is invalid")
    try:
        root.mkdir(parents=True, exist_ok=True)
        root_details = os.lstat(root)
        if not stat.S_ISDIR(root_details.st_mode) or stat.S_ISLNK(root_details.st_mode):
            raise BatchPhenotypingError("sample output root is not a real directory")
        current = root
        for name in ("phenotyping", "unsupervised-inputs"):
            current = current / name
            try:
                os.mkdir(current)
            except FileExistsError:
                pass
            details = os.lstat(current)
            if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
                raise BatchPhenotypingError("preparation output ancestry is invalid")
    except BatchPhenotypingError:
        raise
    except OSError as exc:
        raise BatchPhenotypingError("preparation output directory is unavailable") from exc
    return current


def _clustering_setup_request(*, context, execution, sample, measurement,
                              revising=False, initial_config=None) -> StageResult:
    available_feature_ids = tuple(
        item.feature_id
        for item in measurement.measurement_plan.features
        if item.kind == FeatureKind.EXPRESSION.value
        and item.measurement.rsplit(": ", 1)[-1] in {"Mean", "Median"}
    )
    if not available_feature_ids or len(set(available_feature_ids)) != len(available_feature_ids):
        raise BatchPhenotypingError("preparation requires unique observed expression features")
    available_features = [
        item
        for item in measurement.measurement_plan.to_mapping()["features"]
        if item["feature_id"] in available_feature_ids
    ]
    resume_context = {
        "schema_version": _INTERACTION_SCHEMA_VERSION,
        "node_id": context.node_id,
        "sample_id": sample.sample_id,
        "slide_id": sample.slide_id,
        "section_id": measurement.section_id,
        "validation_domain_id": sample.validation_domain_id,
        "analysis_domain_bindings": [
            item.to_mapping() for item in execution.analysis_domain_bindings
        ],
        "resolved_config_sha256": sample.resolved_config_sha256,
        "input_artifact_ref_sha256s": list(execution.input_artifact_ref_sha256s),
        "available_feature_ids": list(available_feature_ids),
        "available_features": available_features,
        "clustering_mode": (
            sample.config.clustering.mode.value
            if not revising and sample.config.clustering.mode is not None
            else None
        ),
        "configured_k": sample.config.clustering.k,
        "configured_method": sample.config.clustering.method,
        "configured_method_settings": dict(sample.config.clustering.method_settings),
        "configured_resolution": sample.config.clustering.resolution,
        "configured_seed": sample.config.clustering.seed,
    }
    if revising and initial_config is not None:
        resume_context.update(
            initial_clustering_config=initial_config.to_mapping(),
            configured_k=initial_config.k,
            configured_method=initial_config.method,
            configured_method_settings=dict(initial_config.method_settings),
            configured_resolution=initial_config.resolution,
            configured_seed=initial_config.seed,
        )
    return StageResult.needs_interaction(
        stage=_NODE_KIND,
        slide_id=sample.slide_id,
        reason=InteractionReason.CLUSTERING_CONFIGURATION_REQUIRED,
        message="Choose the cell measurements and clustering settings.",
        allowed_actions=(InteractionAction.CONFIGURE_CLUSTERING.value,),
        resume_context=resume_context,
        checkpoint_id="batch-clustering-setup-" + digest(resume_context)[:48],
    )


def _clustering_retry_config(
    *, context, request: StageResult, state_store: BatchStateStore
) -> ClusteringConfig | None:
    authorization = context.authorization
    try:
        response = state_store.load_validated_interaction_response(authorization)
    except (BatchStateError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("clustering setup response is not authenticated") from exc
    predecessor = authorization.interaction_predecessor_terminal_record_sha256
    if response is None:
        if authorization.interaction_response_sha256 is not None or predecessor is not None:
            raise BatchPhenotypingError("clustering setup retry lineage is incomplete")
        return None
    context_sha256 = digest(request.to_mapping()["resume_context"])
    try:
        request_id = batch_interaction_request_id(
            node_id=context.node_id,
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
            or response["node_id"] != context.node_id
            or response["interaction_request_id"] != request_id
            or response["interaction_context_sha256"] != context_sha256
            or response["action"] != InteractionAction.CONFIGURE_CLUSTERING.value
            or response["response_validator_id"] != _RESPONSE_VALIDATOR_ID
            or response["response_validator_version"] != _RESPONSE_VALIDATOR_VERSION
            or persisted_request != request
            or persisted_request.checkpoint_id != request.checkpoint_id
            or request.checkpoint_id is None
        ):
            raise BatchPhenotypingError("clustering setup retry lineage differs")
        validated = response["response"]
        if not isinstance(validated, Mapping):
            raise BatchPhenotypingError("clustering setup response is malformed")
        selected_feature_ids = validated["selected_feature_ids"]
        clustering_mapping = validated["clustering_config"]
        expected = validate_interaction_response(
            request,
            InteractionAction.CONFIGURE_CLUSTERING.value,
            {
                "feature_ids": selected_feature_ids,
                "clustering_config": clustering_mapping,
            },
        )
        if (
            plain_json(validated) != expected
            or validated.get("phenotyping_setup_complete") is not True
        ):
            raise BatchPhenotypingError("clustering setup validation differs")
        return ClusteringConfig.from_mapping(
            validated["clustering_config"], field="accepted clustering config"
        )
    except (BatchStateError, ROIAnalysisError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, BatchPhenotypingError):
            raise
        raise BatchPhenotypingError("clustering setup retry response is invalid") from exc


def _resolve_clustering_config(*, context, execution, sample, measurement, state_store):
    configured = sample.config.clustering
    revising = False
    initial_config = configured
    if type(state_store) is BatchStateStore:
        current = state_store._load_plan_snapshot(execution.active_plan_sha256)
        if current.node_by_id[context.node_id].get("settled_review_revision") is not None:
            previous = state_store.settled_review_predecessor(
                context, execution=execution, node_kind=_NODE_KIND)
            revising = True
            if previous is not None:
                response = state_store.load_validated_interaction_response(previous[1])
                if response is not None:
                    initial_config = ClusteringConfig.from_mapping(
                        response["response"]["clustering_config"], field="previous clustering settings")
    if not revising and configured.enabled and configured.feature_specs:
        authorization = context.authorization
        if (
            authorization.interaction_response_sha256 is not None
            or authorization.interaction_predecessor_terminal_record_sha256 is not None
        ):
            raise BatchPhenotypingError("concrete clustering config cannot accept a setup retry")
        return configured
    if not revising and not configured.enabled and configured.feature_specs:
        raise BatchPhenotypingError("disabled concrete clustering config is an opt-out")
    if not revising and not configured.enabled and configured.k is not None:
        raise BatchPhenotypingError("disabled concrete clustering config is an opt-out")
    if type(state_store) is not BatchStateStore:
        raise BatchPhenotypingError("deferred clustering setup requires exact state owner")
    try:
        current = state_store.authenticate_current_execution_context(context)
    except (BatchStateError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("clustering setup context is not current") from exc
    if current != execution:
        raise BatchPhenotypingError("clustering setup current execution differs")
    request = _clustering_setup_request(
        context=context, execution=execution, sample=sample, measurement=measurement,
        revising=revising, initial_config=initial_config,
    )
    selected = _clustering_retry_config(context=context, request=request, state_store=state_store)
    if selected is None:
        return request
    selected_config = selected
    if not selected_config.enabled:
        raise BatchPhenotypingError("accepted clustering settings must enable clustering")
    if not revising and (
        (configured.mode is not None and selected_config.mode is not configured.mode)
        or selected_config.exclude_channel_roles != configured.exclude_channel_roles
        or (
            configured.enabled
            and configured.feature_specs
            and selected_config.feature_specs != configured.feature_specs
        )
        or (
            configured.enabled
            and (selected_config.k, selected_config.resolution, selected_config.seed)
            != (configured.k, configured.resolution, configured.seed)
        )

    ):
        raise BatchPhenotypingError("accepted clustering settings differ from admission")
    return selected


def build_unsupervised_preparation_execution_result(
    *,
    context: BatchNodeExecutionContext,
    execution_authority: BatchCurrentExecutionAuthority,
    manifest: ValidatedBatchManifest,
    measurement_authority: BatchCommittedArtifactAuthority,
    mode_authorities: tuple[BatchCommittedArtifactAuthority, ...],
    state_store: BatchStateStore | None = None,
) -> BatchNodeExecutionResult | StageResult:
    """Build one compact descriptor solely from current committed owners."""

    execution = _validate_execution(context, execution_authority)
    sample = _validated_manifest(manifest, execution=execution, context=context)
    measurement, modes = _validate_sources(
        context=context,
        execution=execution,
        measurement_authority=measurement_authority,
        mode_authorities=mode_authorities,
    )
    measurement_ref = _validate_measurement(
        measurement=measurement, execution=execution, sample=sample
    )
    _validate_modes(modes=modes, execution=execution, measurement=measurement, sample=sample)
    clustering_selection = _resolve_clustering_config(
        context=context,
        execution=execution,
        sample=sample,
        measurement=measurement,
        state_store=state_store,
    )
    if isinstance(clustering_selection, StageResult):
        return clustering_selection
    clustering = clustering_selection
    try:
        feature_specs = measurement.measurement_plan.bind_clustering_features(
            clustering.feature_specs
        )
    except (CellFeatureError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError(
            "configured clustering features do not bind measurement authority"
        ) from exc
    if any(item.measurement.rsplit(": ", 1)[-1] not in {"Mean", "Median"}
           for item in feature_specs):
        raise BatchPhenotypingError("Choose Mean or Median marker intensities for clustering.")
    rows, marker_features = _selected_measurement_authority(measurement=measurement, sample=sample)
    row_columns = {
        (item.feature_id, item.measurement) for item in marker_features
    }
    if (
        any((item.feature_id, item.measurement) not in row_columns for item in feature_specs)
    ):
        raise BatchPhenotypingError("clustering features differ from row authority")

    panel_sha256 = digest(
        {
            "canonical_channels": [dict(item) for item in measurement.canonical_channels],
        }
    )
    reference = measurement.measurement_rows
    preparation = UnsupervisedPreparationInput(
        sample_id=sample.sample_id,
        subject_id=sample.subject_id,
        project_entry_id=measurement.project_entry_id,
        slide_id=sample.slide_id,
        section_id=measurement.section_id,
        image_role="primary_mif",
        phenotyping_group_id=None,
        resolved_config_sha256=sample.resolved_config_sha256,
        panel_sha256=panel_sha256,
        validation_domain_ids=(sample.validation_domain_id,),
        analysis_domain_ids=tuple(
            item.analysis_domain_id for item in measurement.analysis_domain_bindings
        ),
        segmentation_generation_id=measurement.segmentation_generation_id,
        measurement_generation_id=measurement.measurement_generation_id,
        measurement_plan_sha256=measurement.measurement_plan_sha256,
        measurement_result_ref_sha256=measurement_ref.artifact_ref_sha256,
        measurement_rows_reference_sha256=reference.reference_sha256,
        measurement_rows_sha256=reference.sha256,
        measurement_rows_byte_count=reference.byte_count,
        measurement_matrix_bytes=measurement.cell_count * len(feature_specs) * 8,
        row_inventory_sha256=rows.row_inventory_sha256,
        cell_count=measurement.cell_count,
        cell_ids_sha256=rows.cell_ids_sha256,
        cell_to_domain_membership_sha256=rows.cell_to_domain_membership_sha256,
        domain_cell_commitments_sha256=rows.domain_cell_commitments_sha256,
        measurement_saved_state_after_sha256=measurement.saved_state_after_sha256,
        measurement_execution_exchange_sha256=measurement.execution_exchange_sha256,
        measurement_feature_schema_sha256=measurement.feature_schema_sha256,
        population_selection=modes[0].population_selection,
        clustering_mode=clustering.mode,
        method=clustering.method,
        method_settings=clustering.method_settings,
        k=clustering.k,
        resolution=clustering.resolution,
        seed=clustering.seed,
        feature_specs=feature_specs,
    )
    from ..batch_manifest import operational_sample_output_root

    output_root = (sample.sample_output_root if state_store is None else
                   operational_sample_output_root(sample, state_store.layout.root))
    directory = _ensure_output_directory(output_root)
    persisted = persist_unsupervised_preparation(preparation, directory)
    reopened_payload = read_bounded_file(
        persisted.locator,
        expected_byte_count=persisted.byte_count,
        maximum_byte_count=persisted.byte_count,
    )
    try:
        reopened = UnsupervisedPreparationInput.from_mapping(
            json.loads(reopened_payload.decode("utf-8"))
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("persisted preparation cannot be reopened") from exc
    if reopened != preparation or canonical_bytes(reopened.to_mapping()) != reopened_payload:
        raise BatchPhenotypingError("persisted preparation differs from owner value")

    contract = unsupervised_preparation_identity_contract()
    output = BatchArtifactRef(
        artifact_id="unsupervised-preparation-" + persisted.sha256[:48],
        artifact_kind="unsupervised_preparation_result",
        schema_name="mif-batch-unsupervised-input",
        schema_version=2,
        origin=ArtifactOrigin.INTERNAL_NODE,
        producer_node_id=context.node_id,
        producer_attempt_id=context.authorization.attempt_id,
        producer_attempt_authorization_sha256=(context.authorization.authorization_sha256),
        external_declaration_sha256=None,
        external_validation_receipt_sha256=None,
        locator=persisted.locator,
        sha256=persisted.sha256,
        byte_count=persisted.byte_count,
        scope=ArtifactScope(
            validation_domain_id=sample.validation_domain_id,
            sample_id=sample.sample_id,
            slide_id=sample.slide_id,
            project_entry_id=measurement.project_entry_id,
            analysis_domain_bindings=measurement.analysis_domain_bindings,
        ),
        owning_identity=unsupervised_preparation_owning_identity(preparation),
        identity_contract=contract,
        owning_identity_contract_sha256=contract.contract_sha256,
        producer_version=PREPARATION_PRODUCER_VERSION,
        runtime_versions={"python": platform.python_version()},
        compatibility_signature_sha256=context.compatibility_signature_sha256,
        configuration_bindings=(
            ArtifactConfigurationBinding(sample.sample_id, sample.resolved_config_sha256),
        ),
    )
    return BatchNodeExecutionResult(
        authorization_sha256=context.authorization.authorization_sha256,
        outputs=(output,),
    )


__all__ = ["build_unsupervised_preparation_execution_result"]
