"""Current-state authority for native QuPath classifier application.

Training records keep subject roles, reference evidence, model settings and application
scope linked. Their validators and native adapters are called by the shared training service
before a fit, evaluation or accepted publication."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from mif_pipeline.pipeline_core import ROIAnalysisError
from mif_pipeline.pipeline_core.config import (
    PrimaryClassifierSettings,
    require_primary_settings_compatible,
)
from mif_pipeline.qupath_runtime.catalogue import (
    ProjectAuthorityError,
    ProjectDesignation,
    ProjectMutationLeaseProvider,
)
from mif_pipeline.qupath_runtime.scripts import registered_script_bundle_sha256

from ..batch_analysis_preflight import (
    BatchAnalysisPreflightError,
    resolve_analysis_channels,
    validate_analysis_frame_artifact_ref,
)
from ..batch_artifacts import (
    ArtifactOrigin,
    BatchArtifactError,
    BatchArtifactRef,
    BatchCommittedArtifactAuthority,
    BatchNodeExecutionContext,
    validate_committed_artifact_authority_set,
)
from ..batch_manifest import ResolvedBatchSample
from ..batch_measurement_artifacts import decode_committed_measurements_result
from ..batch_project import (
    BatchProjectError,
    reopen_project_catalogue_entry_artifact,
)
from ..batch_state import BatchStateError, BatchStateStore
from ._shared import BatchTrainingError
from .planning import TrainingPlan, ValidatedTrainingPlanSetProjection
from .records import ClassifierRecord, ClassifierRecordSet, _require_accepted_classifier_record_set
from ._shared import ClassifierKind
from .portability import (
    ClassifierAuthorityResolver,
    ClassifierDeploymentObservation,
)

# ==============================================================================
#                             Application authority
# ==============================================================================


def _pixel_feature_requirements(
    *,
    ordered_channel_ids: tuple[str, ...],
    channel_roles: Mapping[str, str],
) -> dict[str, object]:
    """Reproduce training's required-channel projection for one live target."""

    required = frozenset(channel_roles.values())
    selected = tuple(channel_id for channel_id in ordered_channel_ids if channel_id in required)
    if not required or set(selected) != required:
        raise BatchTrainingError("native classifier target lacks exact required live channels")
    return {
        "feature_policy": "pixel-channels/v1",
        "ordered_channel_ids": list(selected),
    }


def _secondary_application_channel_roles(structure, record_set) -> dict[str, str]:
    """Use the fitted model's selected inputs, saved during classifier setup."""
    output_id = structure.classifier.classifier_id
    digest = dict(record_set.output_inventory).get(output_id)
    matches = tuple(record for record in record_set.classifier_records
                    if record.classifier_record_sha256 == digest
                    and record.classifier_id == structure.classifier.classifier_id)
    if len(matches) != 1:
        raise BatchTrainingError("The selected structure has no matching fitted classifier.")
    definition = matches[0].owner_payload.classifier_definition
    roles = dict(definition.channel_roles)
    if not roles:
        raise BatchTrainingError("The fitted structure classifier has no selected image channels.")
    if f"Candidate_{structure.structure_id}" not in definition.output_classes:
        raise BatchTrainingError("The fitted classifier does not contain the selected structure class.")
    return roles


def _authority_by_digest(
    authorities: tuple[BatchCommittedArtifactAuthority, ...],
) -> dict[str, BatchCommittedArtifactAuthority]:
    result = {item.artifact_ref.artifact_ref_sha256: item for item in authorities}
    if len(result) != len(authorities):
        raise BatchTrainingError("current execution contains duplicate committed artifacts")
    return result


@dataclass(frozen=True, slots=True)
class _BoundNativeClassifierResolver(ClassifierAuthorityResolver):
    authority: "NativeClassifierApplicationAuthority"
    state_head_sha256: str
    active_plan_sha256: str
    project_entry_id: str
    analysis_frame: BatchArtifactRef
    project_catalogue_entry: BatchArtifactRef
    accepted_analysis_domain_authorities: tuple[BatchCommittedArtifactAuthority, ...]
    observation: ClassifierDeploymentObservation
    accepted_record_authority: BatchCommittedArtifactAuthority | None = None

    def _current_authorities(self) -> dict[str, BatchCommittedArtifactAuthority]:
        try:
            snapshot = self.authority.state_store.reopen_execution_snapshot(
                expected_head_sha256=self.state_head_sha256
            )
        except (BatchStateError, OSError, TypeError, ValueError) as exc:
            raise BatchTrainingError(
                "native classifier execution head is no longer current"
            ) from exc
        if (
            snapshot.head.sha256 != self.state_head_sha256
            or snapshot.head.active_plan_sha256 != self.active_plan_sha256
        ):
            raise BatchTrainingError("native classifier execution head changed during application")
        return _authority_by_digest(snapshot.committed_artifact_authorities)

    def authenticate_source_artifact(
        self, artifact_ref_sha256: str
    ) -> BatchCommittedArtifactAuthority:
        matches = self._current_authorities()
        try:
            return matches[artifact_ref_sha256]
        except (KeyError, TypeError) as exc:
            raise BatchTrainingError(
                "classifier source artifact is not committed at the current head"
            ) from exc

    def resolve_project(self, project_identity_sha256: str) -> ProjectDesignation:
        self._current_authorities()
        try:
            snapshot = self.authority.state_store.reopen_execution_snapshot(
                expected_head_sha256=self.state_head_sha256
            )
            head = snapshot.head
            designation = self.authority.project_provider.discover_managed(Path(head.project_path))
        except (BatchStateError, ProjectAuthorityError, OSError, TypeError, ValueError) as exc:
            raise BatchTrainingError(
                "native classifier project authority cannot be reopened"
            ) from exc
        if (
            designation.identity.project_identity_sha256 != project_identity_sha256
            or designation.identity.project_identity_sha256 != head.project_identity_sha256
            or designation.authority_head.authority_head_sha256
            != head.project_authority_head_sha256
            or designation.authority_head.qpproj_snapshot_sha256
            != head.project_qpproj_snapshot_sha256
            or designation.authority_head.materialization_state != "materialized"
        ):
            raise BatchTrainingError(
                "native classifier project authority differs from current state"
            )
        return designation

    def native_classifier_source(self, record: ClassifierRecord) -> Path:
        """Reopen model bytes from this target's accepted training or import owner."""
        self._current_authorities()
        accepted = self.accepted_record_authority
        if accepted is not None and accepted.artifact_ref.origin is ArtifactOrigin.MANIFEST_EXTERNAL:
            from .accepted_model_reuse import decode_imported_classifier_record_set

            material = decode_imported_classifier_record_set(
                accepted, state_store=self.authority.state_store,
            )
            return material.model_path(record)
        project = self.resolve_project(record.native_source_project_identity_sha256)
        return project.project_path.parent / record.native_artifact_project_relative_path

    def resolve_training_plan(self, training_plan_id: str) -> TrainingPlan:
        del training_plan_id
        raise BatchTrainingError(
            "native pixel classifier application does not resolve training plans"
        )

    def observe_deployment_target(
        self,
        *,
        project_entry_id: str,
        analysis_frame: BatchArtifactRef,
        project_catalogue_entry: BatchArtifactRef,
        accepted_analysis_domain_authorities: tuple[BatchCommittedArtifactAuthority, ...],
    ) -> ClassifierDeploymentObservation:
        self._current_authorities()
        if (
            project_entry_id != self.project_entry_id
            or analysis_frame != self.analysis_frame
            or project_catalogue_entry != self.project_catalogue_entry
            or tuple(accepted_analysis_domain_authorities)
            != self.accepted_analysis_domain_authorities
        ):
            raise BatchTrainingError(
                "native classifier deployment target differs from its bound owners"
            )
        return self.observation


@dataclass(frozen=True, slots=True)
class NativeClassifierApplicationAuthority:
    """Bind native classifier deployment facts to one authenticated batch head."""

    state_store: BatchStateStore
    project_provider: ProjectMutationLeaseProvider

    def __post_init__(self) -> None:
        if type(self.state_store) is not BatchStateStore:
            raise TypeError("state_store must be the exact BatchStateStore owner")
        if type(self.project_provider) is not ProjectMutationLeaseProvider:
            raise TypeError("project_provider must be the exact ProjectMutationLeaseProvider owner")

    def _authenticate_target(
        self,
        *,
        context: BatchNodeExecutionContext,
        resolved_sample: ResolvedBatchSample,
        analysis_frame: BatchArtifactRef,
        project_catalogue_entry: BatchArtifactRef,
        accepted_analysis_domain_authorities: tuple[BatchCommittedArtifactAuthority, ...],
    ) -> tuple[
        object,
        object,
        object,
        object,
        tuple[BatchCommittedArtifactAuthority, ...],
        ResolvedBatchSample,
    ]:
        if type(context) is not BatchNodeExecutionContext:
            raise BatchTrainingError("native classifier target requires an exact execution context")
        if not isinstance(resolved_sample, ResolvedBatchSample):
            raise BatchTrainingError("native classifier target requires a resolved batch sample")
        try:
            current = self.state_store.authenticate_current_execution_context(context)
            snapshot = self.state_store.reopen_execution_snapshot(
                expected_head_sha256=current.state_head_sha256
            )
            frame = validate_analysis_frame_artifact_ref(analysis_frame)
            _catalogue, entry = reopen_project_catalogue_entry_artifact(project_catalogue_entry)
            domain_authorities = validate_committed_artifact_authority_set(
                tuple(accepted_analysis_domain_authorities)
            )
        except (
            BatchArtifactError,
            BatchProjectError,
            BatchStateError,
            ROIAnalysisError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            raise BatchTrainingError(
                "native classifier target owners cannot be authenticated"
            ) from exc

        if (
            snapshot.head.sha256 != current.state_head_sha256
            or snapshot.head.active_plan_sha256 != current.active_plan_sha256
        ):
            raise BatchTrainingError(
                "native classifier target is not bound to the current plan head"
            )
        if len(current.participant_ids) != 1:
            raise BatchTrainingError("native classifier target lacks one current manifest sample")
        manifest_samples = tuple(
            item
            for item in snapshot.manifest.samples
            if item.enabled and item.sample_id == current.participant_ids[0]
        )
        if len(manifest_samples) != 1:
            raise BatchTrainingError("native classifier target lacks one current manifest sample")
        manifest_sample = manifest_samples[0]
        canonical_config_sha256 = hashlib.sha256(
            json.dumps(
                manifest_sample.config.to_mapping(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        if (
            manifest_sample != resolved_sample
            or manifest_sample.resolved_config_sha256 != canonical_config_sha256
        ):
            raise BatchTrainingError(
                "native classifier target differs from current manifest sample"
            )
        expected_configuration = (
            resolved_sample.sample_id,
            resolved_sample.resolved_config_sha256,
        )
        configuration_bindings = tuple(
            (item.sample_id, item.resolved_config_sha256) for item in current.configuration_bindings
        )
        if (
            current.participant_ids != (resolved_sample.sample_id,)
            or current.project_entry_ids != (entry.project_entry_id,)
            or current.validation_domain_id != resolved_sample.validation_domain_id
            or configuration_bindings != (expected_configuration,)
        ):
            raise BatchTrainingError("native classifier target differs from its execution scope")

        required_refs = (
            analysis_frame,
            project_catalogue_entry,
            *(item.artifact_ref for item in domain_authorities),
        )
        required_digests = {item.artifact_ref_sha256 for item in required_refs}
        current_digests = set(current.input_artifact_ref_sha256s)
        authorities = _authority_by_digest(snapshot.committed_artifact_authorities)
        if (
            len(required_digests) != len(required_refs)
            or not required_digests.issubset(current_digests)
            or any(
                (
                    authorities.get(item.artifact_ref_sha256).artifact_ref != item
                    if item.artifact_ref_sha256 in authorities
                    else True
                )
                for item in required_refs
            )
        ):
            raise BatchTrainingError(
                "native classifier target inputs are not current committed owners"
            )

        if (
            entry.sample_id != resolved_sample.sample_id
            or entry.slide_id != resolved_sample.slide_id
            or entry.validation_domain_id != resolved_sample.validation_domain_id
            or analysis_frame.scope.sample_id != resolved_sample.sample_id
            or analysis_frame.scope.slide_id != resolved_sample.slide_id
        ):
            raise BatchTrainingError(
                "native classifier image identity differs from resolved sample"
            )
        return (
            current,
            snapshot,
            frame,
            entry,
            domain_authorities,
            manifest_sample,
        )

    def bind_pixel_target(
        self,
        *,
        context: BatchNodeExecutionContext,
        resolved_sample: ResolvedBatchSample,
        classifier_role: str,
        structure_id: str | None,
        analysis_frame: BatchArtifactRef,
        project_catalogue_entry: BatchArtifactRef,
        accepted_analysis_domain_authorities: tuple[BatchCommittedArtifactAuthority, ...],
        accepted_record_set: ClassifierRecordSet | None = None,
        accepted_plan_set: ValidatedTrainingPlanSetProjection | None = None,
        accepted_record_authority: BatchCommittedArtifactAuthority | None = None,
    ) -> ClassifierAuthorityResolver:
        if classifier_role not in {"primary", "secondary"}:
            raise BatchTrainingError("native classifier role is unsupported")
        current, snapshot, frame, entry, domain_authorities, sample = self._authenticate_target(
            context=context,
            resolved_sample=resolved_sample,
            analysis_frame=analysis_frame,
            project_catalogue_entry=project_catalogue_entry,
            accepted_analysis_domain_authorities=(accepted_analysis_domain_authorities),
        )
        if classifier_role == "secondary":
            from ..batch_secondary_definitions import resolve_secondary_structures, secondary_definition_selections
            selection = secondary_definition_selections(
                snapshot, state_store=self.state_store, participant_ids=(sample.sample_id,), node_id=context.node_id,
            ).get(sample.sample_id)
            secondary_structures = resolve_secondary_structures(sample, selection)
        imported = (accepted_record_authority is not None
                    and accepted_record_authority.artifact_ref.origin is ArtifactOrigin.MANIFEST_EXTERNAL)
        if imported:
            from .accepted_model_reuse import (
                decode_imported_classifier_record_set, validate_imported_primary_settings,
            )
            ref = accepted_record_authority.artifact_ref
            if (ref.artifact_ref_sha256 not in current.input_artifact_ref_sha256s
                    or accepted_record_authority.state_head_sha256 != current.state_head_sha256
                    or accepted_record_authority.active_plan_sha256 != current.active_plan_sha256):
                raise BatchTrainingError("Saved classifier is not this current target's input")
            material = decode_imported_classifier_record_set(
                accepted_record_authority, state_store=self.state_store,
            )
            if material.record_set != accepted_record_set or accepted_plan_set is not None:
                raise BatchTrainingError("Saved classifier source differs from admitted records")
            if classifier_role == "primary":
                if structure_id is not None:
                    raise BatchTrainingError("Saved primary target carries a structure")
                classifier = validate_imported_primary_settings(sample.config, material).primary_classifier
            else:
                matches = tuple(item for item in secondary_structures
                                if item.structure_id == structure_id)
                if len(matches) != 1:
                    raise BatchTrainingError("Saved secondary target is not configured exactly once")
                classifier = matches[0].classifier
        else:
            if classifier_role == "primary":
                if structure_id is not None:
                    raise BatchTrainingError(
                        "primary classifier target cannot name a secondary structure"
                    )
                if (
                    type(accepted_record_set) is not ClassifierRecordSet
                    or type(accepted_plan_set) is not ValidatedTrainingPlanSetProjection
                    or type(accepted_record_authority) is not BatchCommittedArtifactAuthority
                    or accepted_record_set.classifier_kind is not ClassifierKind.PRIMARY_COMPARTMENT
                    or accepted_plan_set.plan_set.classifier_kind
                    is not ClassifierKind.PRIMARY_COMPARTMENT
                    or accepted_record_set.source_training_plan_set_sha256
                    != accepted_plan_set.plan_set.training_plan_set_sha256
                    or len(accepted_plan_set.plan_set.plans) != 1
                    or len(accepted_record_set.classifier_records) != 1
                ):
                    raise BatchTrainingError("primary target requires its accepted record and plan")
                _require_accepted_classifier_record_set(accepted_record_set)
                plan = accepted_plan_set.plan_set.plans[0]
                record = accepted_record_set.classifier_records[0]
                settings = plan.request.primary_settings
                record_ref = accepted_record_authority.artifact_ref
                plan_ref = accepted_plan_set.artifact_ref
                if (
                    record_ref.artifact_kind != "classifier_record_set"
                    or plan_ref.artifact_kind != "training_plan_set"
                    or record_ref.artifact_ref_sha256 not in current.input_artifact_ref_sha256s
                    or record_ref.owning_identity.get("classifier_record_set_sha256")
                    != accepted_record_set.classifier_record_set_sha256
                    or record_ref.owning_identity.get("source_training_plan_set_sha256")
                    != accepted_plan_set.plan_set.training_plan_set_sha256
                ):
                    raise BatchTrainingError(
                        "primary accepted record is not the current classifier input"
                    )
                try:
                    current_record_authority = self.state_store.authenticate_available_artifact(
                        record_ref
                    )
                    current_plan_authority = self.state_store.authenticate_available_artifact(plan_ref)
                except (BatchStateError, BatchArtifactError, OSError, TypeError, ValueError) as exc:
                    raise BatchTrainingError("primary accepted record or plan is not current") from exc
                if (
                    current_record_authority != accepted_record_authority
                    or current_plan_authority.artifact_ref != plan_ref
                    or current_plan_authority.state_head_sha256 != current.state_head_sha256
                    or current_plan_authority.active_plan_sha256 != current.active_plan_sha256
                    or accepted_record_authority.state_head_sha256 != current.state_head_sha256
                    or accepted_record_authority.active_plan_sha256 != current.active_plan_sha256
                    or accepted_plan_set.state_head_sha256 != current.state_head_sha256
                    or accepted_plan_set.active_plan_sha256 != current.active_plan_sha256
                    or plan_ref.owning_identity.get("training_plan_set_sha256")
                    != accepted_plan_set.plan_set.training_plan_set_sha256
                    or plan_ref.configuration_bindings != accepted_plan_set.configuration_bindings
                    or plan_ref.scope.validation_domain_id != sample.validation_domain_id
                    or accepted_plan_set.plan_set.normalized_manifest_sha256
                    != accepted_plan_set.normalized_manifest_sha256
                    or plan.request.normalized_manifest_sha256
                    != accepted_plan_set.normalized_manifest_sha256
                    or plan.request.batch_id != snapshot.manifest.manifest.batch_id
                    or accepted_plan_set.batch_id != snapshot.manifest.manifest.batch_id
                    or accepted_plan_set.validation_domain_id != sample.validation_domain_id
                    or accepted_plan_set.classifier_kind is not ClassifierKind.PRIMARY_COMPARTMENT
                    or sample.sample_id not in accepted_plan_set.participant_ids
                    or entry.project_entry_id not in accepted_plan_set.project_entry_ids
                    or tuple(
                        item
                        for item in accepted_plan_set.configuration_bindings
                        if item.sample_id == sample.sample_id
                    )
                    != current.configuration_bindings
                ):
                    raise BatchTrainingError("primary accepted plan is not current for target scope")
                accepted_application_bindings = tuple(
                    item
                    for item in plan.request.application_analysis_domain_bindings
                    if item.project_entry_id == entry.project_entry_id
                )
                current_application_bindings = tuple(
                    item
                    for item in current.analysis_domain_bindings
                    if item.project_entry_id == entry.project_entry_id
                )
                current_domain_owner_bindings = tuple(
                    item
                    for authority in domain_authorities
                    for item in authority.artifact_ref.scope.analysis_domain_bindings
                    if item.project_entry_id == entry.project_entry_id
                )
                if (
                    not accepted_application_bindings
                    or len(set(accepted_application_bindings)) != len(accepted_application_bindings)
                    or not current_application_bindings
                    or len(set(current_application_bindings)) != len(current_application_bindings)
                    or not set(current_application_bindings).issubset(
                        set(accepted_application_bindings)
                    )
                    or set(current_application_bindings) != set(current_domain_owner_bindings)
                    or len(current_domain_owner_bindings) != len(current_application_bindings)
                    or tuple(accepted_plan_set.analysis_domain_bindings)
                    != tuple(plan.request.application_analysis_domain_bindings)
                    or plan_ref.scope.analysis_domain_bindings
                    != tuple(plan.request.application_analysis_domain_bindings)
                ):
                    raise BatchTrainingError(
                        "primary target application domain bindings differ from current accepted scope"
                    )
                try:
                    resolved_channels = resolve_analysis_channels(
                        sample.config,
                        analysis_frame,
                        sample_id=sample.sample_id,
                        validation_domain_id=sample.validation_domain_id,
                    )
                except (BatchAnalysisPreflightError, ROIAnalysisError, TypeError, ValueError) as exc:
                    raise BatchTrainingError(
                        "primary target analysis-frame channels are invalid"
                    ) from exc
                if (
                    type(settings) is not PrimaryClassifierSettings
                    or plan.request.classifier_id != record.classifier_id
                    or plan.training_plan_id != record.training_plan_id
                    or record.validation_domain_id != sample.validation_domain_id
                    or plan.request.validation_domain_id != sample.validation_domain_id
                ):
                    raise BatchTrainingError("primary target accepted classifier identity differs")
                application = tuple(
                    item
                    for item in plan.request.application_entries
                    if item.project_entry_id == entry.project_entry_id
                )
                if len(application) != 1 or (
                    application[0].sample_id,
                    application[0].slide_id,
                    application[0].image_identity_sha256,
                    application[0].channels,
                ) != (
                    sample.sample_id,
                    sample.slide_id,
                    entry.image_sha256,
                    resolved_channels,
                ):
                    raise BatchTrainingError("primary target is outside accepted application inventory")
                admitted = (
                    None
                    if sample.config.primary_classifier is None
                    else PrimaryClassifierSettings(
                        sample.config.primary_classes, sample.config.primary_classifier
                    )
                )
                if admitted is not None:
                    # The authenticated accepted plan owns its explicit Ignore choice;
                    # every other admitted setting must remain exact.
                    try:
                        require_primary_settings_compatible(admitted, settings)
                    except ROIAnalysisError as exc:
                        raise BatchTrainingError(
                            "primary target accepted settings differ from admission"
                        ) from exc
                try:
                    settings.validate_channels(application[0].channels)
                except ValueError as exc:
                    raise BatchTrainingError("primary target accepted channels are invalid") from exc
                classifier = settings.primary_classifier
            else:
                if (
                    type(accepted_record_set) is not ClassifierRecordSet
                    or accepted_plan_set is not None
                    or type(accepted_record_authority) is not BatchCommittedArtifactAuthority
                    or accepted_record_authority.artifact_ref.artifact_ref_sha256
                    not in current.input_artifact_ref_sha256s
                ):
                    raise BatchTrainingError("The structure classifier requires its accepted fitted model.")
                matches = tuple(
                    item
                    for item in secondary_structures
                    if item.structure_id == structure_id
                )
                if len(matches) != 1:
                    raise BatchTrainingError(
                        "secondary classifier target is not configured exactly once"
                    )
                classifier = matches[0].classifier
        scale_x = frame.spatial_frame.x_um_per_px
        scale_y = frame.spatial_frame.y_um_per_px
        if scale_x != scale_y:
            raise BatchTrainingError(
                "native classifier target requires isotropic pixel calibration"
            )
        channel_roles = (
            _secondary_application_channel_roles(matches[0], accepted_record_set)
            if classifier_role == "secondary" else dict(classifier.channel_roles)
        )
        feature_requirements = _pixel_feature_requirements(
            ordered_channel_ids=frame.ordered_channel_ids,
            channel_roles=channel_roles,
        )
        observation = ClassifierDeploymentObservation(
            provider_id="qupath-native-classifier-application/v1",
            can_report_required_metrics=True,
            observed_pixel_scale_um=scale_x,
            observed_channel_requirements={"channel_roles": channel_roles},
            observed_feature_requirements=feature_requirements,
            observed_runtime_versions=(
                ("qupath", frame.qupath_executable_version),
                (
                    "script_bundle_sha256",
                    registered_script_bundle_sha256(),
                ),
            ),
            observed_extension_versions=(),
            observed_model_versions=(),
        )
        return _BoundNativeClassifierResolver(
            authority=self,
            state_head_sha256=current.state_head_sha256,
            active_plan_sha256=current.active_plan_sha256,
            project_entry_id=entry.project_entry_id,
            analysis_frame=analysis_frame,
            project_catalogue_entry=project_catalogue_entry,
            accepted_analysis_domain_authorities=domain_authorities,
            observation=observation,
            accepted_record_authority=accepted_record_authority,
        )

    def bind_supervised_target(
        self,
        *,
        context: BatchNodeExecutionContext,
        resolved_sample: ResolvedBatchSample,
        analysis_frame: BatchArtifactRef,
        project_catalogue_entry: BatchArtifactRef,
        accepted_analysis_domain_authorities: tuple[BatchCommittedArtifactAuthority, ...],
        measurements_result_authority: BatchCommittedArtifactAuthority,
    ) -> ClassifierAuthorityResolver:
        """Bind supervised feature evidence to the current measurement owner."""

        current, snapshot, frame, entry, domain_authorities, sample = self._authenticate_target(
            context=context,
            resolved_sample=resolved_sample,
            analysis_frame=analysis_frame,
            project_catalogue_entry=project_catalogue_entry,
            accepted_analysis_domain_authorities=(accepted_analysis_domain_authorities),
        )
        if type(measurements_result_authority) is not BatchCommittedArtifactAuthority:
            raise BatchTrainingError("supervised classifier target requires committed measurements")
        measurement_ref = measurements_result_authority.artifact_ref
        authorities = _authority_by_digest(snapshot.committed_artifact_authorities)
        if (
            measurement_ref.artifact_ref_sha256 not in current.input_artifact_ref_sha256s
            or authorities.get(measurement_ref.artifact_ref_sha256) != measurements_result_authority
        ):
            raise BatchTrainingError("supervised measurement owner is not current execution input")
        try:
            measurement = decode_committed_measurements_result(measurements_result_authority)
        except (BatchArtifactError, ROIAnalysisError, OSError, TypeError, ValueError) as exc:
            raise BatchTrainingError("supervised measurement owner cannot be decoded") from exc
        expected_bindings = tuple(
            sorted(
                (
                    binding
                    for authority in domain_authorities
                    for binding in authority.artifact_ref.scope.analysis_domain_bindings
                ),
                key=lambda item: (
                    item.sample_id,
                    item.slide_id,
                    item.analysis_domain_id,
                    item.analysis_domain_revision,
                    item.project_entry_id,
                ),
            )
        )
        if (
            measurement.artifact_ref != measurement_ref
            or measurement.project_entry_id != entry.project_entry_id
            or measurement.sample_id != resolved_sample.sample_id
            or measurement.slide_id != resolved_sample.slide_id
            or measurement.analysis_domain_bindings != expected_bindings
        ):
            raise BatchTrainingError(
                "supervised measurement identity differs from deployment target"
            )
        feature_schema = [
            {
                "measurement_name": item.measurement,
                "semantic_role": item.role.value,
            }
            for item in measurement.measurement_plan.features
        ]
        try:
            image_channels = resolve_analysis_channels(
                sample.config, analysis_frame,
                sample_id=sample.sample_id,
                validation_domain_id=sample.validation_domain_id,
            )
        except (BatchAnalysisPreflightError, ROIAnalysisError, TypeError, ValueError) as exc:
            raise BatchTrainingError("supervised target analysis-frame channels are invalid") from exc
        observation = ClassifierDeploymentObservation(
            provider_id="qupath-native-supervised-application/v1",
            can_report_required_metrics=True,
            observed_pixel_scale_um=frame.spatial_frame.x_um_per_px,
            observed_channel_requirements={},
            observed_feature_requirements={
                "feature_schema": feature_schema,
                "available_channels": [item.to_mapping() for item in image_channels],
            },
            observed_runtime_versions=(
                ("qupath", frame.qupath_executable_version),
                (
                    "script_bundle_sha256",
                    registered_script_bundle_sha256(),
                ),
            ),
            observed_extension_versions=(),
            observed_model_versions=(),
        )
        return _BoundNativeClassifierResolver(
            authority=self,
            state_head_sha256=current.state_head_sha256,
            active_plan_sha256=current.active_plan_sha256,
            project_entry_id=entry.project_entry_id,
            analysis_frame=analysis_frame,
            project_catalogue_entry=project_catalogue_entry,
            accepted_analysis_domain_authorities=domain_authorities,
            observation=observation,
        )


__all__ = ["NativeClassifierApplicationAuthority"]
