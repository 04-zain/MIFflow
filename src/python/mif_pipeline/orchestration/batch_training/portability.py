"""Classifier deployment evidence and portability validation.

The `ClassifierDeploymentTargetEvidence` record carries `project_entry_id`, `sample_id`,
`slide_id`, `image_identity_sha256`, `observed_validation_domain_id` and its remaining
declared fields.

Training records keep subject roles, reference evidence, model settings and application
scope linked. Their validators and native adapters are called by the shared training service
before a fit, evaluation or accepted publication."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
import hashlib
import math
from pathlib import Path, PurePosixPath
from typing import Protocol

from mif_pipeline.pipeline_core import ROIAnalysisError
from mif_pipeline.qupath_runtime.project_content import same_project_snapshot
from mif_pipeline.qupath_runtime.catalogue import (
    ProjectDesignation,
    canonical_project_uri,
)

from ..batch_artifacts import (
    AnalysisDomainBinding,
    BatchArtifactError,
    BatchArtifactRef,
    BatchCommittedArtifactAuthority,
    validate_committed_artifact_authority_set,
)
from ..batch_analysis_domain_artifacts import (
    decode_committed_accepted_analysis_domain,
)
from ..batch_analysis_preflight import validate_analysis_frame_artifact_ref
from ..batch_project import (
    BatchProjectError,
    reopen_project_catalogue_entry_artifact,
)

from ._shared import (
    BatchTrainingError,
    ClassifierArtifactKind,
    _array,
    _binding_identity,
    _binding_sort_key,
    _classifier_training_producer_kind,
    _closed_mapping,
    _digest,
    _export_json,
    _json_object,
    _read_exact_artifact_bytes,
    _sha256,
    _sha256_tuple,
    _string_tuple,
    _text,
    _version_inventory,
)
from .planning import TrainingPlan
from .records import (
    ClassifierRecord,
    PixelClassifierOwnerPayload,
    SupervisedClassifierOwnerPayload,
    _validate_python_model_artifact_binding,
)

# ==============================================================================
#                                  Portability
# ==============================================================================


def _runtime_versions_for_compatibility(
    versions: tuple[tuple[str, str], ...],
    artifact_kind: ClassifierArtifactKind,
) -> tuple[tuple[str, str], ...]:
    """Compare runtime requirements without conflating two native script owners.

    A native record retains its training bundle as immutable provenance. The
    application bundle is independently authenticated by the current request
    and target observation. Both full inventories remain in those records;
    only this compatibility comparison excludes their script-bundle digests.
    """
    if artifact_kind is not ClassifierArtifactKind.QUPATH_NATIVE:
        return versions
    return tuple(item for item in versions if item[0] != "script_bundle_sha256")


def _supervised_features_match_observation(
    required: Mapping[str, object], observed: Mapping[str, object], *, feature_source=None
) -> bool:
    """Bind ordered accepted native features to the full observed inventory."""

    if set(required) not in ({"feature_schema"}, {"feature_schema", "feature_source"}) or set(observed) not in ({"feature_schema"}, {"feature_schema", "available_channels"}):
        return False
    if feature_source is not None:
        from .supervised_features import require_kronos_cell_schema, require_kronos_cell_channels
        try:
            if _export_json(required.get("feature_source")) != feature_source.to_mapping():
                return False
            require_kronos_cell_schema(feature_source, required["feature_schema"])
            require_kronos_cell_channels(feature_source, observed.get("available_channels"))
        except (BatchTrainingError, TypeError, ValueError):
            return False
        return True
    if "feature_source" in required:
        return False
    wanted = required["feature_schema"]
    available = observed["feature_schema"]
    if not isinstance(wanted, (tuple, list)) or not isinstance(available, (tuple, list)):
        return False
    if not wanted or not available:
        return False
    fields = {"measurement_name", "semantic_role"}
    seen: set[str] = set()
    observed_by_name: dict[str, str] = {}
    for row in available:
        if not isinstance(row, Mapping) or set(row) != fields:
            return False
        name, role = row["measurement_name"], row["semantic_role"]
        if (
            type(name) is not str
            or not name
            or type(role) is not str
            or not role
            or name in observed_by_name
        ):
            return False
        observed_by_name[name] = role
    for row in wanted:
        if not isinstance(row, Mapping) or set(row) != fields:
            return False
        name, role = row["measurement_name"], row["semantic_role"]
        if type(name) is not str or not name or type(role) is not str or not role or name in seen:
            return False
        seen.add(name)
        if observed_by_name.get(name) != role:
            return False
    return True


@dataclass(frozen=True, slots=True)
class ClassifierDeploymentObservation:
    """In-process provider/config observation used to attest one target."""

    provider_id: str
    can_report_required_metrics: bool
    observed_pixel_scale_um: float | None
    observed_channel_requirements: Mapping[str, object]
    observed_feature_requirements: Mapping[str, object]
    observed_runtime_versions: tuple[tuple[str, str], ...]
    observed_extension_versions: tuple[tuple[str, str], ...]
    observed_model_versions: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        """Validate the ClassifierDeploymentObservation fields when the record is constructed.

        The checks cover ``observed_pixel_scale_um``,
        ``provider_id``, ``can_report_required_metrics``. Invalid field types, values or
        combinations raise the declared validation error before this instance can be used by
        its caller."""

        object.__setattr__(self, "provider_id", _text(self.provider_id, "provider_id"))
        if type(self.can_report_required_metrics) is not bool:
            raise BatchTrainingError("can_report_required_metrics must be a boolean")
        if self.observed_pixel_scale_um is not None:
            value = self.observed_pixel_scale_um
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) <= 0
            ):
                raise BatchTrainingError("observed_pixel_scale_um must be positive and finite")
            object.__setattr__(self, "observed_pixel_scale_um", float(value))
        for field in (
            "observed_channel_requirements",
            "observed_feature_requirements",
        ):
            object.__setattr__(self, field, _json_object(getattr(self, field), field))
        for field in (
            "observed_runtime_versions",
            "observed_extension_versions",
            "observed_model_versions",
        ):
            object.__setattr__(
                self,
                field,
                _version_inventory(
                    getattr(self, field),
                    field,
                    nonempty=field == "observed_runtime_versions",
                    require_canonical=False,
                ),
            )


class ClassifierAuthorityResolver(Protocol):
    """Decision-time authority needed to validate a serialized request."""

    def authenticate_source_artifact(
        self, artifact_ref_sha256: str
    ) -> BatchCommittedArtifactAuthority: ...

    def resolve_project(self, project_identity_sha256: str) -> ProjectDesignation: ...

    def native_classifier_source(self, record: ClassifierRecord) -> Path: ...

    def resolve_training_plan(self, training_plan_id: str) -> TrainingPlan: ...

    def observe_deployment_target(
        self,
        *,
        project_entry_id: str,
        analysis_frame: BatchArtifactRef,
        project_catalogue_entry: BatchArtifactRef,
        accepted_analysis_domain_authorities: tuple[BatchCommittedArtifactAuthority, ...],
    ) -> ClassifierDeploymentObservation: ...


def _validate_current_project_designation(
    designation: ProjectDesignation,
    *,
    expected_project_identity_sha256: str | None = None,
) -> ProjectDesignation:
    if not isinstance(designation, ProjectDesignation):
        raise BatchTrainingError("project authority must be a ProjectDesignation")
    identity = designation.identity.project_identity_sha256
    if expected_project_identity_sha256 is not None and identity != _sha256(
        expected_project_identity_sha256,
        "expected_project_identity_sha256",
    ):
        raise BatchTrainingError("project authority substitution was detected")
    try:
        path = designation.project_path.resolve(strict=True)
        payload = path.read_bytes()
    except OSError as exc:
        raise BatchTrainingError("project authority bytes are not readable") from exc
    if (
        canonical_project_uri(path) != designation.project_uri
        or designation.authority_head.project_identity_sha256 != identity
        or designation.authority_head.active_project_uri != designation.project_uri
        or designation.authority_head.materialization_state != "materialized"
        or not same_project_snapshot(
            path, designation.authority_head.qpproj_snapshot_sha256,
            hashlib.sha256(payload).hexdigest(), observed_payload=payload,
        )
    ):
        raise BatchTrainingError("project authority is not current")
    return designation


@dataclass(frozen=True, slots=True)
class ClassifierDeploymentTargetEvidence:
    project_entry_id: str
    sample_id: str
    slide_id: str
    image_identity_sha256: str
    observed_validation_domain_id: str
    observed_pixel_scale_um: float | None
    analysis_domain_bindings: tuple[AnalysisDomainBinding, ...]
    analysis_domain_binding_set_sha256: str
    source_artifact_ref_sha256s: tuple[str, ...]
    observed_channel_requirements: Mapping[str, object]
    observed_channel_requirements_sha256: str
    observed_feature_requirements: Mapping[str, object]
    observed_feature_requirements_sha256: str
    observed_runtime_versions: tuple[tuple[str, str], ...]
    observed_extension_versions: tuple[tuple[str, str], ...]
    observed_model_versions: tuple[tuple[str, str], ...]
    target_evidence_sha256: str

    def __post_init__(self) -> None:
        """Validate the ClassifierDeploymentTargetEvidence fields when the record is
        constructed.

        The checks cover ``observed_pixel_scale_um``,
        ``analysis_domain_bindings``, ``analysis_domain_binding_set_sha256``,
        ``target_evidence_sha256``, ``image_identity_sha256``. Invalid field types, values
        or combinations raise the declared validation error before this instance can be used
        by its caller."""

        for field in ("project_entry_id", "sample_id", "slide_id", "observed_validation_domain_id"):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        object.__setattr__(
            self,
            "image_identity_sha256",
            _sha256(self.image_identity_sha256, "image_identity_sha256"),
        )
        if self.observed_pixel_scale_um is not None:
            value = self.observed_pixel_scale_um
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) <= 0
            ):
                raise BatchTrainingError("observed_pixel_scale_um must be positive and finite")
            object.__setattr__(self, "observed_pixel_scale_um", float(value))
        bindings = tuple(self.analysis_domain_bindings)
        if not bindings or not all(isinstance(item, AnalysisDomainBinding) for item in bindings):
            raise BatchTrainingError("target analysis_domain_bindings must contain typed bindings")
        if bindings != tuple(sorted(bindings, key=_binding_sort_key)) or len(
            {_binding_identity(item) for item in bindings}
        ) != len(bindings):
            raise BatchTrainingError("target analysis_domain_bindings must be canonical and unique")
        if any(
            (item.project_entry_id, item.sample_id, item.slide_id)
            != (self.project_entry_id, self.sample_id, self.slide_id)
            for item in bindings
        ):
            raise BatchTrainingError("target analysis-domain binding is foreign to its entry")
        object.__setattr__(self, "analysis_domain_bindings", bindings)
        binding_sha = _sha256(
            self.analysis_domain_binding_set_sha256,
            "analysis_domain_binding_set_sha256",
        )
        if binding_sha != _digest([item.to_mapping() for item in bindings]):
            raise BatchTrainingError("analysis_domain_binding_set_sha256 is not canonical")
        object.__setattr__(self, "analysis_domain_binding_set_sha256", binding_sha)
        object.__setattr__(
            self,
            "source_artifact_ref_sha256s",
            _sha256_tuple(
                self.source_artifact_ref_sha256s,
                "source_artifact_ref_sha256s",
                nonempty=True,
            ),
        )
        for field in ("observed_channel_requirements", "observed_feature_requirements"):
            frozen = _json_object(getattr(self, field), field)
            object.__setattr__(self, field, frozen)
            digest_field = f"{field}_sha256"
            digest = _sha256(getattr(self, digest_field), digest_field)
            if digest != _digest(frozen):
                raise BatchTrainingError(f"{digest_field} is not canonical")
            object.__setattr__(self, digest_field, digest)
        for field in (
            "observed_runtime_versions",
            "observed_extension_versions",
            "observed_model_versions",
        ):
            object.__setattr__(
                self,
                field,
                _version_inventory(
                    getattr(self, field),
                    field,
                    nonempty=field == "observed_runtime_versions",
                ),
            )
        target_sha = _sha256(self.target_evidence_sha256, "target_evidence_sha256")
        if target_sha != _digest(self._unsigned_mapping()):
            raise BatchTrainingError("target_evidence_sha256 is not canonical")
        object.__setattr__(self, "target_evidence_sha256", target_sha)

    @classmethod
    def create(
        cls,
        *,
        project_designation: ProjectDesignation,
        analysis_frame: BatchArtifactRef,
        project_catalogue_entry: BatchArtifactRef,
        accepted_analysis_domain_authorities: tuple[BatchCommittedArtifactAuthority, ...],
        selected_observation: ClassifierDeploymentObservation,
    ) -> "ClassifierDeploymentTargetEvidence":
        project = _validate_current_project_designation(project_designation)
        if not isinstance(selected_observation, ClassifierDeploymentObservation):
            raise BatchTrainingError(
                "target evidence requires a typed selected provider observation"
            )
        if not isinstance(analysis_frame, BatchArtifactRef) or not isinstance(
            project_catalogue_entry, BatchArtifactRef
        ):
            raise BatchTrainingError(
                "target evidence requires typed analysis-frame and project refs"
            )
        try:
            frame_evidence = validate_analysis_frame_artifact_ref(analysis_frame)
            catalogue, entry = reopen_project_catalogue_entry_artifact(project_catalogue_entry)
        except (BatchArtifactError, BatchProjectError, ROIAnalysisError) as exc:
            raise BatchTrainingError(
                "target analysis-frame or project owner validation failed"
            ) from exc
        if (
            catalogue.project_identity_sha256 != project.identity.project_identity_sha256
            or catalogue.project_uri != project.project_uri
        ):
            raise BatchTrainingError(
                "target project catalogue differs from retained project authority"
            )
        try:
            domain_authorities = validate_committed_artifact_authority_set(
                tuple(accepted_analysis_domain_authorities)
            )
            projections = tuple(
                decode_committed_accepted_analysis_domain(authority)
                for authority in domain_authorities
            )
        except (BatchArtifactError, ROIAnalysisError, TypeError, ValueError) as exc:
            raise BatchTrainingError(
                "target evidence requires committed accepted-domain authorities"
            ) from exc
        if not projections:
            raise BatchTrainingError(
                "target evidence requires committed accepted-domain authorities"
            )
        bindings = tuple(
            sorted(
                (
                    binding
                    for projection in projections
                    for binding in projection.artifact_ref.scope.analysis_domain_bindings
                ),
                key=_binding_sort_key,
            )
        )
        if not bindings or len({_binding_identity(item) for item in bindings}) != len(bindings):
            raise BatchTrainingError(
                "target accepted-domain authorities lack unique exact bindings"
            )
        common_identity = (
            entry.project_entry_id,
            entry.sample_id,
            entry.slide_id,
            entry.validation_domain_id,
            entry.image_sha256,
        )
        if any(
            (
                projection.project_entry_id,
                projection.artifact_ref.scope.sample_id,
                projection.artifact_ref.scope.slide_id,
                projection.artifact_ref.scope.validation_domain_id,
                projection.canonical_image_sha256,
            )
            != common_identity
            for projection in projections
        ):
            raise BatchTrainingError(
                "target accepted-domain authority has a foreign common identity"
            )
        frame_scope = analysis_frame.scope
        if (
            frame_scope.sample_id,
            frame_scope.slide_id,
            frame_scope.validation_domain_id,
            frame_evidence.spatial_frame.canonical_image_sha256,
        ) != (
            entry.sample_id,
            entry.slide_id,
            entry.validation_domain_id,
            entry.image_sha256,
        ):
            raise BatchTrainingError(
                "target analysis-frame identity differs from project and domain owners"
            )
        refs = (
            analysis_frame,
            project_catalogue_entry,
            *(authority.artifact_ref for authority in domain_authorities),
        )
        configuration_bindings = {
            (binding.sample_id, binding.resolved_config_sha256)
            for ref in refs
            for binding in ref.configuration_bindings
        }
        if (
            any(len(ref.configuration_bindings) != 1 for ref in refs)
            or len(configuration_bindings) != 1
            or next(iter(configuration_bindings))[0] != entry.sample_id
        ):
            raise BatchTrainingError("target owner sources lack one exact configuration binding")
        observation = selected_observation
        unsigned = {
            "project_entry_id": entry.project_entry_id,
            "sample_id": entry.sample_id,
            "slide_id": entry.slide_id,
            "image_identity_sha256": entry.image_sha256,
            "observed_validation_domain_id": entry.validation_domain_id,
            "observed_pixel_scale_um": observation.observed_pixel_scale_um,
            "analysis_domain_bindings": bindings,
            "analysis_domain_binding_set_sha256": _digest([item.to_mapping() for item in bindings]),
            "source_artifact_ref_sha256s": tuple(sorted(ref.artifact_ref_sha256 for ref in refs)),
            "observed_channel_requirements": observation.observed_channel_requirements,
            "observed_channel_requirements_sha256": _digest(
                observation.observed_channel_requirements
            ),
            "observed_feature_requirements": observation.observed_feature_requirements,
            "observed_feature_requirements_sha256": _digest(
                observation.observed_feature_requirements
            ),
            "observed_runtime_versions": observation.observed_runtime_versions,
            "observed_extension_versions": observation.observed_extension_versions,
            "observed_model_versions": observation.observed_model_versions,
        }
        prototype = object.__new__(cls)
        for name, item in unsigned.items():
            object.__setattr__(prototype, name, item)
        return cls(**unsigned, target_evidence_sha256=_digest(prototype._unsigned_mapping()))

    @classmethod
    def from_mapping(cls, value: object) -> "ClassifierDeploymentTargetEvidence":
        fields = frozenset(cls.__dataclass_fields__)
        mapping = _closed_mapping(
            value, fields=fields, field="classifier_deployment_target_evidence"
        )
        return cls(
            project_entry_id=mapping["project_entry_id"],  # type: ignore[arg-type]
            sample_id=mapping["sample_id"],  # type: ignore[arg-type]
            slide_id=mapping["slide_id"],  # type: ignore[arg-type]
            image_identity_sha256=mapping["image_identity_sha256"],  # type: ignore[arg-type]
            observed_validation_domain_id=mapping["observed_validation_domain_id"],  # type: ignore[arg-type]
            observed_pixel_scale_um=mapping["observed_pixel_scale_um"],  # type: ignore[arg-type]
            analysis_domain_bindings=tuple(
                AnalysisDomainBinding.from_mapping(item)
                for item in _array(mapping["analysis_domain_bindings"], "analysis_domain_bindings")
            ),
            analysis_domain_binding_set_sha256=mapping["analysis_domain_binding_set_sha256"],  # type: ignore[arg-type]
            source_artifact_ref_sha256s=tuple(_array(mapping["source_artifact_ref_sha256s"], "source_artifact_ref_sha256s")),  # type: ignore[arg-type]
            observed_channel_requirements=mapping["observed_channel_requirements"],  # type: ignore[arg-type]
            observed_channel_requirements_sha256=mapping["observed_channel_requirements_sha256"],  # type: ignore[arg-type]
            observed_feature_requirements=mapping["observed_feature_requirements"],  # type: ignore[arg-type]
            observed_feature_requirements_sha256=mapping["observed_feature_requirements_sha256"],  # type: ignore[arg-type]
            observed_runtime_versions=_version_inventory(
                mapping["observed_runtime_versions"], "observed_runtime_versions", nonempty=True
            ),
            observed_extension_versions=_version_inventory(
                mapping["observed_extension_versions"], "observed_extension_versions"
            ),
            observed_model_versions=_version_inventory(
                mapping["observed_model_versions"], "observed_model_versions"
            ),
            target_evidence_sha256=mapping["target_evidence_sha256"],  # type: ignore[arg-type]
        )

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            "project_entry_id": self.project_entry_id,
            "sample_id": self.sample_id,
            "slide_id": self.slide_id,
            "image_identity_sha256": self.image_identity_sha256,
            "observed_validation_domain_id": self.observed_validation_domain_id,
            "observed_pixel_scale_um": self.observed_pixel_scale_um,
            "analysis_domain_bindings": [
                item.to_mapping() for item in self.analysis_domain_bindings
            ],
            "analysis_domain_binding_set_sha256": self.analysis_domain_binding_set_sha256,
            "source_artifact_ref_sha256s": list(self.source_artifact_ref_sha256s),
            "observed_channel_requirements": _export_json(self.observed_channel_requirements),
            "observed_channel_requirements_sha256": self.observed_channel_requirements_sha256,
            "observed_feature_requirements": _export_json(self.observed_feature_requirements),
            "observed_feature_requirements_sha256": self.observed_feature_requirements_sha256,
            "observed_runtime_versions": [list(item) for item in self.observed_runtime_versions],
            "observed_extension_versions": [
                list(item) for item in self.observed_extension_versions
            ],
            "observed_model_versions": [list(item) for item in self.observed_model_versions],
        }

    def to_mapping(self) -> dict[str, object]:
        return {**self._unsigned_mapping(), "target_evidence_sha256": self.target_evidence_sha256}


@dataclass(frozen=True, slots=True)
class ClassifierApplicationRequest:
    classifier_id: str
    classifier_version: str
    classifier_record_sha256: str
    target_project_identity_sha256: str
    deployment_project_entry_ids: tuple[str, ...]
    deployment_analysis_domain_bindings: tuple[AnalysisDomainBinding, ...]
    deployment_binding_set_sha256: str
    target_evidence: tuple[ClassifierDeploymentTargetEvidence, ...]
    target_evidence_set_sha256: str
    application_request_sha256: str

    def __post_init__(self) -> None:
        """Validate the ClassifierApplicationRequest fields when the record is constructed.

        The checks cover ``deployment_project_entry_ids``,
        ``deployment_analysis_domain_bindings``, ``deployment_binding_set_sha256``,
        ``target_evidence``, ``target_evidence_set_sha256``, ``application_request_sha256``.
        Invalid field types, values or combinations raise the declared validation error
        before this instance can be used by its caller."""

        object.__setattr__(self, "classifier_id", _text(self.classifier_id, "classifier_id"))
        object.__setattr__(
            self, "classifier_version", _text(self.classifier_version, "classifier_version")
        )
        for field in (
            "classifier_record_sha256",
            "target_project_identity_sha256",
            "deployment_binding_set_sha256",
            "target_evidence_set_sha256",
        ):
            object.__setattr__(self, field, _sha256(getattr(self, field), field))
        entries = _string_tuple(
            self.deployment_project_entry_ids,
            "deployment_project_entry_ids",
            nonempty=True,
            canonical=True,
        )
        bindings = tuple(self.deployment_analysis_domain_bindings)
        if not bindings or not all(isinstance(item, AnalysisDomainBinding) for item in bindings):
            raise BatchTrainingError("deployment bindings must contain typed bindings")
        if bindings != tuple(sorted(bindings, key=_binding_sort_key)) or len(
            {_binding_identity(item) for item in bindings}
        ) != len(bindings):
            raise BatchTrainingError("deployment bindings must be canonical and unique")
        if {item.project_entry_id for item in bindings} != set(entries):
            raise BatchTrainingError("deployment bindings require exact entry coverage")
        if self.deployment_binding_set_sha256 != _digest([item.to_mapping() for item in bindings]):
            raise BatchTrainingError("deployment_binding_set_sha256 is not canonical")
        evidence = tuple(self.target_evidence)
        if not evidence or not all(
            isinstance(item, ClassifierDeploymentTargetEvidence) for item in evidence
        ):
            raise BatchTrainingError("target_evidence must contain typed observations")
        if evidence != tuple(sorted(evidence, key=lambda item: item.project_entry_id)) or len(
            {item.project_entry_id for item in evidence}
        ) != len(evidence):
            raise BatchTrainingError("target_evidence must be canonical with unique entries")
        if tuple(item.project_entry_id for item in evidence) != entries:
            raise BatchTrainingError("target_evidence requires exact deployment entry coverage")
        evidence_bindings = tuple(
            sorted(
                (binding for item in evidence for binding in item.analysis_domain_bindings),
                key=_binding_sort_key,
            )
        )
        if evidence_bindings != bindings:
            raise BatchTrainingError("target evidence differs from deployment binding coverage")
        if self.target_evidence_set_sha256 != _digest([item.to_mapping() for item in evidence]):
            raise BatchTrainingError("target_evidence_set_sha256 is not canonical")
        object.__setattr__(self, "deployment_project_entry_ids", entries)
        object.__setattr__(self, "deployment_analysis_domain_bindings", bindings)
        object.__setattr__(self, "target_evidence", evidence)
        request_sha = _sha256(self.application_request_sha256, "application_request_sha256")
        if request_sha != _digest(self._unsigned_mapping()):
            raise BatchTrainingError("application_request_sha256 is not canonical")
        object.__setattr__(self, "application_request_sha256", request_sha)

    @classmethod
    def create(
        cls,
        *,
        record: ClassifierRecord,
        target_project: ProjectDesignation,
        target_evidence: tuple[ClassifierDeploymentTargetEvidence, ...],
    ) -> "ClassifierApplicationRequest":
        if not isinstance(record, ClassifierRecord):
            raise BatchTrainingError("application requires a ClassifierRecord")
        project = _validate_current_project_designation(target_project)
        evidence = tuple(sorted(target_evidence, key=lambda item: item.project_entry_id))
        if not evidence:
            raise BatchTrainingError("application target evidence must not be empty")
        bindings = tuple(
            sorted(
                (binding for item in evidence for binding in item.analysis_domain_bindings),
                key=_binding_sort_key,
            )
        )
        unsigned = {
            "classifier_id": record.classifier_id,
            "classifier_version": record.classifier_version,
            "classifier_record_sha256": record.classifier_record_sha256,
            "target_project_identity_sha256": (project.identity.project_identity_sha256),
            "deployment_project_entry_ids": tuple(item.project_entry_id for item in evidence),
            "deployment_analysis_domain_bindings": bindings,
            "deployment_binding_set_sha256": _digest([item.to_mapping() for item in bindings]),
            "target_evidence": evidence,
            "target_evidence_set_sha256": _digest([item.to_mapping() for item in evidence]),
        }
        prototype = object.__new__(cls)
        for name, item in unsigned.items():
            object.__setattr__(prototype, name, item)
        return cls(**unsigned, application_request_sha256=_digest(prototype._unsigned_mapping()))

    @classmethod
    def from_mapping(cls, value: object) -> "ClassifierApplicationRequest":
        fields = frozenset(cls.__dataclass_fields__)
        mapping = _closed_mapping(value, fields=fields, field="classifier_application_request")
        return cls(
            classifier_id=mapping["classifier_id"],  # type: ignore[arg-type]
            classifier_version=mapping["classifier_version"],  # type: ignore[arg-type]
            classifier_record_sha256=mapping["classifier_record_sha256"],  # type: ignore[arg-type]
            target_project_identity_sha256=mapping["target_project_identity_sha256"],  # type: ignore[arg-type]
            deployment_project_entry_ids=tuple(_array(mapping["deployment_project_entry_ids"], "deployment_project_entry_ids")),  # type: ignore[arg-type]
            deployment_analysis_domain_bindings=tuple(
                AnalysisDomainBinding.from_mapping(item)
                for item in _array(
                    mapping["deployment_analysis_domain_bindings"],
                    "deployment_analysis_domain_bindings",
                )
            ),
            deployment_binding_set_sha256=mapping["deployment_binding_set_sha256"],  # type: ignore[arg-type]
            target_evidence=tuple(
                ClassifierDeploymentTargetEvidence.from_mapping(item)
                for item in _array(mapping["target_evidence"], "target_evidence")
            ),
            target_evidence_set_sha256=mapping["target_evidence_set_sha256"],  # type: ignore[arg-type]
            application_request_sha256=mapping["application_request_sha256"],  # type: ignore[arg-type]
        )

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            "classifier_id": self.classifier_id,
            "classifier_version": self.classifier_version,
            "classifier_record_sha256": self.classifier_record_sha256,
            "target_project_identity_sha256": self.target_project_identity_sha256,
            "deployment_project_entry_ids": list(self.deployment_project_entry_ids),
            "deployment_analysis_domain_bindings": [
                item.to_mapping() for item in self.deployment_analysis_domain_bindings
            ],
            "deployment_binding_set_sha256": self.deployment_binding_set_sha256,
            "target_evidence": [item.to_mapping() for item in self.target_evidence],
            "target_evidence_set_sha256": self.target_evidence_set_sha256,
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            **self._unsigned_mapping(),
            "application_request_sha256": self.application_request_sha256,
        }


@dataclass(frozen=True, slots=True)
class ClassifierPortabilityResult:
    compatible: bool
    incompatible_entry_ids: tuple[str, ...]
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.compatible) is not bool:
            raise BatchTrainingError("compatible must be a boolean")
        entries = _string_tuple(
            self.incompatible_entry_ids,
            "incompatible_entry_ids",
            nonempty=False,
            canonical=True,
        )
        reasons = _string_tuple(self.reasons, "reasons", nonempty=False, canonical=True)
        if self.compatible != (not entries and not reasons):
            raise BatchTrainingError("portability compatibility and diagnostics disagree")
        object.__setattr__(self, "incompatible_entry_ids", entries)
        object.__setattr__(self, "reasons", reasons)


def _resolve_current_deployment_target(
    retained: ClassifierDeploymentTargetEvidence,
    *,
    project: ProjectDesignation,
    authority_resolver: ClassifierAuthorityResolver,
) -> ClassifierDeploymentTargetEvidence:
    try:
        authorities = tuple(
            authority_resolver.authenticate_source_artifact(digest)
            for digest in retained.source_artifact_ref_sha256s
        )
    except (
        AttributeError,
        BatchArtifactError,
        KeyError,
        OSError,
        ROIAnalysisError,
        TypeError,
        ValueError,
    ) as exc:
        raise BatchTrainingError(
            "deployment authority resolver could not authenticate every source"
        ) from exc
    if not all(isinstance(item, BatchCommittedArtifactAuthority) for item in authorities):
        raise BatchTrainingError(
            "deployment authority resolver returned a foreign source capability"
        )
    for digest, authority in zip(retained.source_artifact_ref_sha256s, authorities, strict=True):
        if authority.artifact_ref.artifact_ref_sha256 != digest:
            raise BatchTrainingError(
                "deployment authority resolver source substitution was detected"
            )
    try:
        authenticated = validate_committed_artifact_authority_set(authorities)
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchTrainingError(
            "deployment authority resolver returned invalid committed authorities"
        ) from exc
    by_kind: dict[str, list[BatchCommittedArtifactAuthority]] = {}
    for authority in authenticated:
        by_kind.setdefault(authority.artifact_ref.artifact_kind, []).append(authority)
    if (
        set(by_kind)
        != {
            "analysis_frame",
            "project_catalogue_entry",
            "accepted_analysis_domain",
        }
        or len(by_kind["analysis_frame"]) != 1
        or len(by_kind["project_catalogue_entry"]) != 1
        or not by_kind["accepted_analysis_domain"]
    ):
        raise BatchTrainingError("deployment authority resolver returned foreign source kinds")
    frame = by_kind["analysis_frame"][0].artifact_ref
    project_ref = by_kind["project_catalogue_entry"][0].artifact_ref
    domain_authorities = tuple(by_kind["accepted_analysis_domain"])
    try:
        observation = authority_resolver.observe_deployment_target(
            project_entry_id=retained.project_entry_id,
            analysis_frame=frame,
            project_catalogue_entry=project_ref,
            accepted_analysis_domain_authorities=domain_authorities,
        )
    except (
        AttributeError,
        BatchArtifactError,
        KeyError,
        OSError,
        ROIAnalysisError,
        TypeError,
        ValueError,
    ) as exc:
        raise BatchTrainingError(
            "deployment authority resolver lacks a current target observation"
        ) from exc
    if not isinstance(observation, ClassifierDeploymentObservation):
        raise BatchTrainingError(
            "deployment authority resolver returned a foreign target observation"
        )
    return ClassifierDeploymentTargetEvidence.create(
        project_designation=project,
        analysis_frame=frame,
        project_catalogue_entry=project_ref,
        accepted_analysis_domain_authorities=domain_authorities,
        selected_observation=observation,
    )


def _resolve_current_project(
    authority_resolver: ClassifierAuthorityResolver,
    project_identity_sha256: str,
) -> ProjectDesignation:
    try:
        project = authority_resolver.resolve_project(project_identity_sha256)
    except (
        AttributeError,
        KeyError,
        OSError,
        ROIAnalysisError,
        TypeError,
        ValueError,
    ) as exc:
        raise BatchTrainingError(
            "deployment authority resolver could not resolve project authority"
        ) from exc
    return _validate_current_project_designation(
        project,
        expected_project_identity_sha256=project_identity_sha256,
    )


def _resolve_current_training_plan(
    authority_resolver: ClassifierAuthorityResolver,
    record: ClassifierRecord,
) -> TrainingPlan:
    try:
        plan = authority_resolver.resolve_training_plan(record.training_plan_id)
        if not isinstance(plan, TrainingPlan):
            raise BatchTrainingError(
                "deployment authority resolver returned a foreign training plan"
            )
        current = replace(plan)
    except (
        AttributeError,
        BatchTrainingError,
        KeyError,
        ROIAnalysisError,
        TypeError,
        ValueError,
    ) as exc:
        raise BatchTrainingError(
            "deployment authority resolver could not resolve training plan authority"
        ) from exc
    if (
        current.training_plan_id != record.training_plan_id
        or current.request.classifier_id != record.classifier_id
        or current.request.classifier_kind is not record.classifier_kind
        or current.classifier_engine != record.classifier_engine
        or current.request.validation_domain_id != record.validation_domain_id
        or _export_json(current.request.channel_requirements or {})
        != _export_json(record.channel_requirements)
    ):
        raise BatchTrainingError("current training plan authority differs from classifier record")
    return current


def validate_classifier_portability(
    record: ClassifierRecord,
    request: ClassifierApplicationRequest,
    *,
    authority_resolver: ClassifierAuthorityResolver,
) -> ClassifierPortabilityResult:
    """Validate classifier portability for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    if not isinstance(record, ClassifierRecord) or not isinstance(
        request, ClassifierApplicationRequest
    ):
        raise BatchTrainingError("portability requires typed record and application request")
    if authority_resolver is None:
        raise BatchTrainingError("portability requires an authority resolver")
    target_project = _resolve_current_project(
        authority_resolver, request.target_project_identity_sha256
    )
    global_reasons: set[str] = set()
    if (
        request.classifier_id != record.classifier_id
        or request.classifier_version != record.classifier_version
        or request.classifier_record_sha256 != record.classifier_record_sha256
    ):
        global_reasons.add("classifier record identity mismatch")
    if record.review_decision != "accepted":
        global_reasons.add("classifier review decision is not accepted")
    if record.artifact_kind is ClassifierArtifactKind.QUPATH_NATIVE:
        # The bound owner distinguishes unchanged training provenance from an
        # admitted export. Cross-project bytes require that import authority.
        from mif_pipeline.io.pinned_files import open_pinned_member
        try:
            resolved = authority_resolver.native_classifier_source(record)
            with open_pinned_member(resolved.parent, resolved.name) as opened:
                if opened.size != record.native_artifact_byte_count:
                    raise BatchTrainingError("Native classifier size differs")
                digest = hashlib.sha256()
                for chunk in opened.chunks():
                    digest.update(chunk)
                if digest.hexdigest() != record.native_artifact_sha256:
                    raise BatchTrainingError("Native classifier bytes differ")
        except (BatchTrainingError, OSError, ValueError, AttributeError):
            global_reasons.add("native classifier artifact bytes are not current")
    else:
        artifact = record.python_model_artifact
        assert artifact is not None
        try:
            authority = authority_resolver.authenticate_source_artifact(
                artifact.artifact_ref_sha256
            )
            if (
                not isinstance(authority, BatchCommittedArtifactAuthority)
                or authority.artifact_ref.artifact_ref_sha256 != artifact.artifact_ref_sha256
                or authority.artifact_ref != artifact
                or authority.producer_node_kind
                != _classifier_training_producer_kind(record.classifier_kind)
            ):
                raise BatchTrainingError("Python model authority substitution was detected")
            validate_committed_artifact_authority_set((authority,))
        except (BatchArtifactError, BatchTrainingError, KeyError, ROIAnalysisError):
            global_reasons.add("Python model artifact bytes are not current")
        else:
            try:
                current_plan = _resolve_current_training_plan(authority_resolver, record)
                current_artifact = _validate_python_model_artifact_binding(
                    authority.artifact_ref,
                    classifier_id=record.classifier_id,
                    classifier_version=record.classifier_version,
                    training_plan_id=record.training_plan_id,
                    accepted_training_evidence_sha256=(record.accepted_training_evidence_sha256),
                    validation_domain_id=record.validation_domain_id,
                    normalized_manifest_sha256=(current_plan.normalized_manifest_sha256),
                    model_configuration_sha256=_digest(current_plan.request.model_configuration),
                    expected_sample_ids=tuple(
                        sorted({item.sample_id for item in current_plan.request.entries})
                    ),
                )
            except (BatchArtifactError, BatchTrainingError):
                global_reasons.add("Python model owner identity is not current")
            else:
                try:
                    _read_exact_artifact_bytes(
                        current_artifact.locator,  # type: ignore[arg-type]
                        current_artifact.sha256,
                        current_artifact.byte_count,
                        field="Python model artifact",
                    )
                except BatchTrainingError:
                    global_reasons.add("Python model artifact bytes are not current")
    supervised_source = None
    if isinstance(record.owner_payload, SupervisedClassifierOwnerPayload) and record.artifact_kind is ClassifierArtifactKind.QUPATH_NATIVE:
        from .qupath_kronos_cell import KronosCellFeatureSource
        from .supervised_features import require_kronos_cell_schema
        if "feature_source" in record.feature_requirements:
            # Record publication already joins this source to the authenticated
            # training plan. Native deployment resolvers deliberately do not reopen plans.
            try:
                supervised_source = KronosCellFeatureSource.from_mapping(record.feature_requirements["feature_source"])
                require_kronos_cell_schema(supervised_source, record.feature_requirements.get("feature_schema"))
            except (BatchTrainingError, TypeError, ValueError):
                global_reasons.add("supervised feature source authority is invalid")
    pixel_owner = (
        record.owner_payload
        if isinstance(record.owner_payload, PixelClassifierOwnerPayload)
        else None
    )
    if (
        pixel_owner is not None
        and pixel_owner.classifier_definition.validation_status == "not_evaluated"
        and not pixel_owner.classifier_definition.unattended_review_override
    ):
        global_reasons.add("Accept the unchecked classifier before use.")
    incompatible: set[str] = set()
    reasons = set(global_reasons)
    for retained_target in request.target_evidence:
        target = _resolve_current_deployment_target(
            retained_target,
            project=target_project,
            authority_resolver=authority_resolver,
        )
        entry_reasons: list[str] = []
        if target != retained_target:
            entry_reasons.append("target evidence is stale")
        if target.observed_validation_domain_id != record.validation_domain_id:
            entry_reasons.append("validation domain mismatch")
        if dict(target.observed_channel_requirements) != dict(record.channel_requirements):
            entry_reasons.append("channel requirements mismatch")
        features_compatible = (
            _supervised_features_match_observation(
                record.feature_requirements,
                target.observed_feature_requirements,
                feature_source=supervised_source,
            )
            if isinstance(record.owner_payload, SupervisedClassifierOwnerPayload)
            and record.artifact_kind is ClassifierArtifactKind.QUPATH_NATIVE
            else dict(target.observed_feature_requirements) == {
                key: value for key, value in record.feature_requirements.items()
                # The serialized pixel model supplies its filters; the image supplies channels.
                if key != "image_features" or not isinstance(record.owner_payload, PixelClassifierOwnerPayload)
            }
        )
        if not features_compatible:
            entry_reasons.append("feature requirements mismatch")
        if _runtime_versions_for_compatibility(
            target.observed_runtime_versions, record.artifact_kind
        ) != _runtime_versions_for_compatibility(record.runtime_versions, record.artifact_kind):
            entry_reasons.append("runtime versions mismatch")
        if target.observed_extension_versions != record.extension_versions:
            entry_reasons.append("extension versions mismatch")
        if target.observed_model_versions != record.model_versions:
            entry_reasons.append("model versions mismatch")
        if pixel_owner is not None:
            definition = pixel_owner.classifier_definition
            scale = target.observed_pixel_scale_um
            if (
                scale is None
                or abs(scale - definition.pixel_scale_um) > definition.pixel_scale_tolerance_um
            ):
                entry_reasons.append("pixel scale mismatch")
        if entry_reasons or global_reasons:
            incompatible.add(target.project_entry_id)
        reasons.update(f"{target.project_entry_id}: {reason}" for reason in entry_reasons)
    return ClassifierPortabilityResult(
        compatible=not incompatible and not reasons,
        incompatible_entry_ids=tuple(sorted(incompatible)),
        reasons=tuple(sorted(reasons)),
    )
