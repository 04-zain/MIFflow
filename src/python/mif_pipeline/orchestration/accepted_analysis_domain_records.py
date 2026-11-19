"""Path-free records and decoders for accepted analysis-domain artifacts.

The `ValidatedAcceptedAnalysisDomainProjection` record carries `artifact_ref`,
`state_head_sha256`, `active_plan_sha256`, `terminal_record_sha256`, `selection_mode` and
its remaining declared fields.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Final

from mif_pipeline.analysis_domain import (
    ActiveAnalysisDomainAuthority,
    AnalysisDomainError,
)
from mif_pipeline.cell_analysis import DomainSetEvidence, DomainSetMemberEvidence
from mif_pipeline.pipeline_core import AnalysisDomainKind

from .batch_analysis_preflight import MIF_PIPELINE_BATCH_RUNTIME_VERSION
from .batch_artifacts import (
    AnalysisDomainBinding,
    ArtifactIdentityContract,
    ArtifactOrigin,
    BatchArtifactError,
    BatchArtifactRef,
    BatchCommittedArtifactAuthority,
    validate_artifact_ref,
    validate_committed_artifact_authority_set,
)

MAX_ACCEPTED_ANALYSIS_DOMAIN_MEMBERS: Final[int] = 145
_CONTRACT_ID: Final[str] = "mif-batch-accepted-analysis-domain-identity/v1"
_ARTIFACT_KIND: Final[str] = "accepted_analysis_domain"
_SCHEMA_NAME: Final[str] = "mif-batch-accepted-analysis-domain"
_SCHEMA_VERSION: Final[int] = 1
_PRODUCER_VERSION: Final[str] = "mif-pipeline-batch-accepted-analysis-domain/v1"
SELECTED_MODE: Final[str] = "selected_roi"
DIRECT_WHOLE_MODE: Final[str] = "whole_tissue_direct"
RETIRED_WHOLE_MODE: Final[str] = "whole_tissue_after_retirement"
SELECTION_MODES: Final[frozenset[str]] = frozenset(
    {SELECTED_MODE, DIRECT_WHOLE_MODE, RETIRED_WHOLE_MODE}
)
_NODE_KIND: Final[str] = "analysis_domain_selection"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

_COMMON_IDENTITY_FIELDS = {
    "selection_mode",
    "sample_id",
    "slide_id",
    "project_entry_id",
    "canonical_image_id",
    "canonical_image_sha256",
    "tissue_generation_id",
    "source_input_artifact_ref_sha256s",
    "accepted_output_sha256",
    "assistance_sha256",
    "resolved_config_sha256",
    "saved_state_disposition",
    "saved_state_after_sha256",
    "domain_set.schema_version",
    "domain_set.domain_set_sha256",
    "domain_set.members.analysis_domain_id",
    "domain_set.members.analysis_domain_kind",
    "domain_set.members.slide_id",
    "domain_set.members.section_id",
    "domain_set.members.tissue_generation_id",
    "domain_set.members.tissue_roi_id",
    "domain_set.members.analysis_domain_revision",
    "domain_set.members.analysis_domain_revision_sha256",
    "domain_set.members.analysis_domain_manifest_sha256",
    "domain_authorities.analysis_domain_id",
    "domain_authorities.analysis_domain_kind",
    "domain_authorities.canonical_image_id",
    "domain_authorities.canonical_image_sha256",
    "domain_authorities.project_entry_id",
    "domain_authorities.slide_id",
    "domain_authorities.section_id",
    "domain_authorities.tissue_generation_id",
    "domain_authorities.tissue_roi_id",
    "domain_authorities.tissue_path_object_id",
    "domain_authorities.source_path_object_id",
    "domain_authorities.analysis_domain_revision",
    "domain_authorities.analysis_domain_revision_sha256",
    "domain_authorities.analysis_domain_manifest_sha256",
    "domain_authorities.geometry_digest_policy",
    "domain_authorities.geometry_sha256",
    "domain_authorities.geometry_reference_path_object_id",
    "mode_evidence.schema_version",
}
_MODE_IDENTITY_FIELDS = {
    "mode_evidence.generation_ids",
    "mode_evidence.generation_manifest_payload_sha256",
    "mode_evidence.generation_manifest_byte_count",
    "mode_evidence.object_outputs_sha256",
    "mode_evidence.execution_exchange_sha256",
    "mode_evidence.validated_exchange_sha256",
    "mode_evidence.attempt_authorization_sha256",
    "mode_evidence.input_artifact_set_sha256",
    "mode_evidence.node_material_sha256",
    "mode_evidence.dispatch_binding_sha256",
    "mode_evidence.execution_context_sha256",
    "mode_evidence.current_state_head_sha256",
    "mode_evidence.active_plan_sha256",
    "mode_evidence.predecessor_accepted_analysis_domain_ref_sha256",
    "mode_evidence.predecessor_domain_set_sha256",
    "mode_evidence.successor_domain_set_sha256",
    "mode_evidence.retirement_attestation_sha256",
    "mode_evidence.retired_source_path_object_ids",
    "mode_evidence.absent_source_path_object_ids",
}
ACCEPTED_ANALYSIS_DOMAIN_IDENTITY_FIELDS: tuple[str, ...] = tuple(sorted(_COMMON_IDENTITY_FIELDS))
ACCEPTED_ANALYSIS_DOMAIN_OPTIONAL_IDENTITY_FIELDS: tuple[str, ...] = tuple(
    sorted(_MODE_IDENTITY_FIELDS)
)


# ==============================================================================
#                        Accepted analysis domain records
# ==============================================================================


@dataclass(frozen=True, slots=True, init=False)
class ValidatedAcceptedAnalysisDomainArtifactRefProjection:
    """Path-free owner projection without batch-state authority."""

    artifact_ref: BatchArtifactRef
    selection_mode: str
    sample_id: str
    slide_id: str
    project_entry_id: str
    canonical_image_id: str
    canonical_image_sha256: str
    tissue_generation_id: str
    source_input_artifact_ref_sha256s: tuple[str, ...]
    accepted_output_sha256: str
    resolved_config_sha256: str
    saved_state_disposition: str
    domain_set: DomainSetEvidence
    domain_authorities: tuple[ActiveAnalysisDomainAuthority, ...]
    assistance_sha256: str
    saved_state_after_sha256: str
    execution_exchange_sha256: str | None
    mode_evidence: Mapping[str, object]

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("ValidatedAcceptedAnalysisDomainArtifactRefProjection is decoder-only")


@dataclass(frozen=True, slots=True, init=False)
class ValidatedAcceptedAnalysisDomainProjection:
    artifact_ref: BatchArtifactRef
    state_head_sha256: str
    active_plan_sha256: str
    terminal_record_sha256: str
    selection_mode: str
    sample_id: str
    slide_id: str
    project_entry_id: str
    canonical_image_id: str
    canonical_image_sha256: str
    tissue_generation_id: str
    source_input_artifact_ref_sha256s: tuple[str, ...]
    accepted_output_sha256: str
    resolved_config_sha256: str
    saved_state_disposition: str
    domain_set: DomainSetEvidence
    domain_authorities: tuple[ActiveAnalysisDomainAuthority, ...]
    assistance_sha256: str
    saved_state_after_sha256: str
    execution_exchange_sha256: str | None
    mode_evidence: Mapping[str, object]

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("ValidatedAcceptedAnalysisDomainProjection is decoder-only")


def _projection_instance(cls: type, values: Mapping[str, object]):
    result = object.__new__(cls)
    for name in cls.__dataclass_fields__:
        object.__setattr__(result, name, values[name])
    return result


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _digest(value: object) -> str:
    try:
        payload = json.dumps(
            _plain(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise BatchArtifactError("accepted analysis-domain evidence is not finite JSON") from exc
    return hashlib.sha256(payload).hexdigest()


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BatchArtifactError(f"{field} must be nonblank canonical text")
    return value


def _sha256(value: object, field: str) -> str:
    digest = _text(value, field)
    if _SHA256.fullmatch(digest) is None:
        raise BatchArtifactError(f"{field} must be a lowercase SHA-256")
    return digest


def _positive_integer(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise BatchArtifactError(f"{field} must be a positive integer")
    return value


def _sequence(value: object, field: str) -> tuple[object, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise BatchArtifactError(f"{field} must be an array")
    return tuple(value)


def _closed_mapping(value: object, fields: frozenset[str], field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise BatchArtifactError(f"{field} must be a string-keyed object")
    unknown = sorted(set(value) - fields)
    missing = sorted(fields - set(value))
    if unknown:
        raise BatchArtifactError(f"{field} has unknown field(s): {', '.join(unknown)}")
    if missing:
        raise BatchArtifactError(f"{field} lacks required field(s): {', '.join(missing)}")
    return value


def domain_set_from_authorities(
    authorities: tuple[ActiveAnalysisDomainAuthority, ...],
) -> DomainSetEvidence:
    return DomainSetEvidence.create(
        tuple(
            DomainSetMemberEvidence(
                analysis_domain_id=item.analysis_domain_id,
                analysis_domain_kind=item.analysis_domain_kind,
                slide_id=item.slide_id,
                section_id=item.section_id,
                tissue_generation_id=item.tissue_generation_id,
                tissue_roi_id=item.tissue_roi_id,
                analysis_domain_revision=item.analysis_domain_revision,
                analysis_domain_revision_sha256=item.analysis_domain_revision_sha256,
                analysis_domain_manifest_sha256=item.analysis_domain_manifest_sha256,
            )
            for item in authorities
        )
    )


def analysis_domain_bindings(
    *,
    sample_id: str,
    authorities: tuple[ActiveAnalysisDomainAuthority, ...],
    domain_set_sha256: str,
) -> tuple[AnalysisDomainBinding, ...]:
    return tuple(
        sorted(
            (
                AnalysisDomainBinding(
                    project_entry_id=item.project_entry_id,
                    sample_id=sample_id,
                    slide_id=item.slide_id,
                    analysis_domain_id=item.analysis_domain_id,
                    analysis_domain_revision=item.analysis_domain_revision,
                    analysis_domain_revision_sha256=item.analysis_domain_revision_sha256,
                    domain_set_sha256=domain_set_sha256,
                )
                for item in authorities
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


def accepted_analysis_domain_identity_contract() -> ArtifactIdentityContract:
    return ArtifactIdentityContract(
        contract_id=_CONTRACT_ID,
        artifact_kind=_ARTIFACT_KIND,
        schema_name=_SCHEMA_NAME,
        schema_version=_SCHEMA_VERSION,
        required_fields=ACCEPTED_ANALYSIS_DOMAIN_IDENTITY_FIELDS,
        optional_fields=ACCEPTED_ANALYSIS_DOMAIN_OPTIONAL_IDENTITY_FIELDS,
        required_runtime_names=("mif_pipeline",),
    )


def _saved_state(value: Mapping[str, object]) -> tuple[str, str]:
    disposition = _text(value["saved_state_disposition"], "saved_state_disposition")
    if disposition not in {"authenticated_unchanged", "authenticated_changed"}:
        raise BatchArtifactError("accepted analysis-domain saved-state disposition is invalid")
    return disposition, _sha256(value["saved_state_after_sha256"], "saved_state_after_sha256")


def validate_accepted_analysis_domain_artifact_ref(
    artifact_ref: BatchArtifactRef,
) -> ValidatedAcceptedAnalysisDomainArtifactRefProjection:
    """Validate owner data without asserting batch-state availability.

    Structured inputs are decoded through their typed ``from_mapping`` constructors. The
    remaining comparisons check the relationships required by this operation before the
    caller can use those records."""

    ref = validate_artifact_ref(artifact_ref)
    contract = accepted_analysis_domain_identity_contract()
    if (
        ref.artifact_kind != _ARTIFACT_KIND
        or (ref.schema_name, ref.schema_version) != (_SCHEMA_NAME, _SCHEMA_VERSION)
        or ref.identity_contract != contract
        or ref.owning_identity_contract_sha256 != contract.contract_sha256
        or ref.producer_version != _PRODUCER_VERSION
        or ref.origin is not ArtifactOrigin.INTERNAL_NODE
        or ref.locator is not None
        or ref.sha256 is not None
        or ref.byte_count is not None
        or ref.runtime_versions.get("mif_pipeline") != MIF_PIPELINE_BATCH_RUNTIME_VERSION
    ):
        raise BatchArtifactError(
            "accepted analysis-domain ref lacks the locatorless owner contract"
        )
    identity = _closed_mapping(
        ref.owning_identity,
        frozenset(
            {
                "selection_mode",
                "sample_id",
                "slide_id",
                "project_entry_id",
                "canonical_image_id",
                "canonical_image_sha256",
                "tissue_generation_id",
                "source_input_artifact_ref_sha256s",
                "accepted_output_sha256",
                "assistance_sha256",
                "resolved_config_sha256",
                "saved_state_disposition",
                "saved_state_after_sha256",
                "domain_set",
                "domain_authorities",
                "mode_evidence",
            }
        ),
        "accepted analysis-domain identity",
    )
    selection_mode = _text(identity["selection_mode"], "selection_mode")
    if selection_mode not in SELECTION_MODES:
        raise BatchArtifactError("accepted analysis-domain selection_mode is invalid")
    if ref.artifact_id != "accepted-domains-" + _digest(identity)[:48]:
        raise BatchArtifactError("accepted analysis-domain artifact ID differs")
    sample_id = _text(identity["sample_id"], "sample_id")
    slide_id = _text(identity["slide_id"], "slide_id")
    project_entry_id = _text(identity["project_entry_id"], "project_entry_id")
    canonical_image_id = _text(identity["canonical_image_id"], "canonical_image_id")
    canonical_image_sha256 = _sha256(identity["canonical_image_sha256"], "canonical_image_sha256")
    tissue_generation_id = _text(identity["tissue_generation_id"], "tissue_generation_id")
    source_refs = tuple(
        _sha256(item, "source_input_artifact_ref_sha256s item")
        for item in _sequence(
            identity["source_input_artifact_ref_sha256s"],
            "source_input_artifact_ref_sha256s",
        )
    )
    if not source_refs or source_refs != tuple(sorted(set(source_refs))):
        raise BatchArtifactError(
            "accepted analysis-domain source-input references are not canonical"
        )
    saved_state_disposition, saved_state_after_sha256 = _saved_state(identity)
    resolved_config_sha256 = _sha256(identity["resolved_config_sha256"], "resolved_config_sha256")
    accepted_output_sha256 = _sha256(identity["accepted_output_sha256"], "accepted_output_sha256")
    try:
        domain_set = DomainSetEvidence.from_mapping(_plain(identity["domain_set"]))
        domain_authorities = tuple(
            ActiveAnalysisDomainAuthority.from_mapping(_plain(item))
            for item in _sequence(identity["domain_authorities"], "domain_authorities")
        )
    except (AnalysisDomainError, TypeError, ValueError) as exc:
        raise BatchArtifactError("accepted analysis-domain typed projection is invalid") from exc
    expected_kind = (
        AnalysisDomainKind.SELECTED_ROI
        if selection_mode == SELECTED_MODE
        else AnalysisDomainKind.WHOLE_TISSUE_SECTION
    )
    if (
        not domain_authorities
        or len(domain_authorities) > MAX_ACCEPTED_ANALYSIS_DOMAIN_MEMBERS
        or any(item.analysis_domain_kind is not expected_kind for item in domain_authorities)
        or domain_set_from_authorities(domain_authorities) != domain_set
        or any(
            item.project_entry_id != project_entry_id
            or item.canonical_image_id != canonical_image_id
            or item.canonical_image_sha256 != canonical_image_sha256
            or item.tissue_generation_id != tissue_generation_id
            or item.slide_id != slide_id
            for item in domain_authorities
        )
    ):
        raise BatchArtifactError("accepted analysis-domain authorities disagree")
    mode_fields = {
        SELECTED_MODE: frozenset(
            {
                "schema_version",
                "generation_ids",
                "generation_manifest_payload_sha256",
                "generation_manifest_byte_count",
                "object_outputs_sha256",
                "execution_exchange_sha256",
                "validated_exchange_sha256",
            }
        ),
        DIRECT_WHOLE_MODE: frozenset(
            {
                "schema_version",
                "attempt_authorization_sha256",
                "input_artifact_set_sha256",
                "node_material_sha256",
                "dispatch_binding_sha256",
                "execution_context_sha256",
                "current_state_head_sha256",
                "active_plan_sha256",
            }
        ),
        RETIRED_WHOLE_MODE: frozenset(
            {
                "schema_version",
                "generation_ids",
                "generation_manifest_payload_sha256",
                "generation_manifest_byte_count",
                "object_outputs_sha256",
                "execution_exchange_sha256",
                "validated_exchange_sha256",
                "predecessor_accepted_analysis_domain_ref_sha256",
                "predecessor_domain_set_sha256",
                "successor_domain_set_sha256",
                "retirement_attestation_sha256",
                "retired_source_path_object_ids",
                "absent_source_path_object_ids",
            }
        ),
    }
    mode_evidence = _closed_mapping(
        identity["mode_evidence"],
        mode_fields[selection_mode],
        "accepted analysis-domain mode evidence",
    )
    execution_exchange_sha256: str | None
    if selection_mode == SELECTED_MODE:
        if mode_evidence["schema_version"] != "mif-batch-selected-analysis-domain-evidence/v1":
            raise BatchArtifactError("selected mode evidence schema is invalid")
        generations = tuple(
            _text(item, "generation_ids item")
            for item in _sequence(mode_evidence["generation_ids"], "generation_ids")
        )
        if generations != (tissue_generation_id,):
            raise BatchArtifactError("selected generation identity differs")
        _positive_integer(
            mode_evidence["generation_manifest_byte_count"],
            "generation_manifest_byte_count",
        )
        for field in (
            "generation_manifest_payload_sha256",
            "object_outputs_sha256",
            "execution_exchange_sha256",
            "validated_exchange_sha256",
        ):
            _sha256(mode_evidence[field], field)
        execution_exchange_sha256 = mode_evidence["execution_exchange_sha256"]
        if set(ref.runtime_versions) != {"mif_pipeline", "qupath"}:
            raise BatchArtifactError("selected mode requires exact QuPath runtime evidence")
        _text(ref.runtime_versions["qupath"], "runtime_versions.qupath")
    elif selection_mode == DIRECT_WHOLE_MODE:
        if (
            mode_evidence["schema_version"] != "mif-batch-whole-tissue-direct-evidence/v1"
            or saved_state_disposition != "authenticated_unchanged"
            or set(ref.runtime_versions) != {"mif_pipeline"}
            or mode_evidence["attempt_authorization_sha256"]
            != ref.producer_attempt_authorization_sha256
        ):
            raise BatchArtifactError("direct whole-tissue mode evidence is invalid")
        for field in (
            "attempt_authorization_sha256",
            "input_artifact_set_sha256",
            "node_material_sha256",
            "dispatch_binding_sha256",
            "execution_context_sha256",
            "current_state_head_sha256",
            "active_plan_sha256",
        ):
            _sha256(mode_evidence[field], field)
        execution_exchange_sha256 = None
    else:
        if (
            mode_evidence["schema_version"] != "mif-batch-whole-tissue-retirement-evidence/v1"
            or mode_evidence["successor_domain_set_sha256"] != domain_set.domain_set_sha256
            or set(ref.runtime_versions) != {"mif_pipeline", "qupath"}
            or saved_state_disposition not in {"authenticated_changed", "authenticated_unchanged"}
        ):
            raise BatchArtifactError("retirement whole-tissue mode evidence is invalid")
        generations = tuple(
            _text(item, "generation_ids item")
            for item in _sequence(mode_evidence["generation_ids"], "generation_ids")
        )
        if len(generations) != 1:
            raise BatchArtifactError("retirement generation identity is invalid")
        _positive_integer(
            mode_evidence["generation_manifest_byte_count"],
            "generation_manifest_byte_count",
        )
        for field in (
            "generation_manifest_payload_sha256",
            "object_outputs_sha256",
            "execution_exchange_sha256",
            "validated_exchange_sha256",
            "predecessor_accepted_analysis_domain_ref_sha256",
            "predecessor_domain_set_sha256",
            "successor_domain_set_sha256",
            "retirement_attestation_sha256",
        ):
            _sha256(mode_evidence[field], field)
        retired = tuple(
            _text(item, "retired_source_path_object_ids item")
            for item in _sequence(
                mode_evidence["retired_source_path_object_ids"],
                "retired_source_path_object_ids",
            )
        )
        absent = tuple(
            _text(item, "absent_source_path_object_ids item")
            for item in _sequence(
                mode_evidence["absent_source_path_object_ids"],
                "absent_source_path_object_ids",
            )
        )
        if (
            retired != tuple(sorted(set(retired)))
            or absent != tuple(sorted(set(absent)))
            or set(retired).intersection(absent)
        ):
            raise BatchArtifactError("retirement source UUID evidence is invalid")
        execution_exchange_sha256 = mode_evidence["execution_exchange_sha256"]
        _text(ref.runtime_versions["qupath"], "runtime_versions.qupath")
    expected_bindings = analysis_domain_bindings(
        sample_id=sample_id,
        authorities=domain_authorities,
        domain_set_sha256=domain_set.domain_set_sha256,
    )
    if (
        ref.scope.validation_domain_id is None
        or ref.scope.sample_id != sample_id
        or ref.scope.slide_id != slide_id
        or ref.scope.project_entry_id != project_entry_id
        or ref.scope.analysis_domain_bindings != expected_bindings
        or len(ref.configuration_bindings) != 1
        or ref.configuration_bindings[0].sample_id != sample_id
        or ref.configuration_bindings[0].resolved_config_sha256 != resolved_config_sha256
    ):
        raise BatchArtifactError("accepted analysis-domain ref scope differs")
    return _projection_instance(
        ValidatedAcceptedAnalysisDomainArtifactRefProjection,
        {
            "artifact_ref": ref,
            "selection_mode": selection_mode,
            "sample_id": sample_id,
            "slide_id": slide_id,
            "project_entry_id": project_entry_id,
            "canonical_image_id": canonical_image_id,
            "canonical_image_sha256": canonical_image_sha256,
            "tissue_generation_id": tissue_generation_id,
            "source_input_artifact_ref_sha256s": source_refs,
            "accepted_output_sha256": accepted_output_sha256,
            "resolved_config_sha256": resolved_config_sha256,
            "saved_state_disposition": saved_state_disposition,
            "domain_set": domain_set,
            "domain_authorities": domain_authorities,
            "assistance_sha256": _sha256(identity["assistance_sha256"], "assistance_sha256"),
            "saved_state_after_sha256": saved_state_after_sha256,
            "execution_exchange_sha256": execution_exchange_sha256,
            "mode_evidence": mode_evidence,
        },
    )


def decode_committed_accepted_analysis_domain(
    authority: BatchCommittedArtifactAuthority,
) -> ValidatedAcceptedAnalysisDomainProjection:
    if type(authority) is not BatchCommittedArtifactAuthority:
        raise BatchArtifactError(
            "accepted analysis-domain decoding requires committed state authority"
        )
    validate_committed_artifact_authority_set(
        (authority,), expected_active_plan_sha256=authority.active_plan_sha256
    )
    if authority.producer_node_kind != _NODE_KIND:
        raise BatchArtifactError("accepted analysis-domain producer kind is invalid")
    ref_projection = validate_accepted_analysis_domain_artifact_ref(authority.artifact_ref)
    if authority.execution_exchange_sha256 != ref_projection.execution_exchange_sha256:
        raise BatchArtifactError("committed attempt exchange differs from accepted-domain identity")
    # The selected domains retain their producing plan. Current availability
    # is supplied by the committed reference after downstream plan expansion.
    values = {
        name: getattr(ref_projection, name)
        for name in ValidatedAcceptedAnalysisDomainArtifactRefProjection.__dataclass_fields__
    }
    values.update(
        state_head_sha256=authority.state_head_sha256,
        active_plan_sha256=authority.active_plan_sha256,
        terminal_record_sha256=authority.terminal_record_sha256,
    )
    return _projection_instance(ValidatedAcceptedAnalysisDomainProjection, values)


__all__ = [
    "ACCEPTED_ANALYSIS_DOMAIN_IDENTITY_FIELDS",
    "ACCEPTED_ANALYSIS_DOMAIN_OPTIONAL_IDENTITY_FIELDS",
    "DIRECT_WHOLE_MODE",
    "MAX_ACCEPTED_ANALYSIS_DOMAIN_MEMBERS",
    "RETIRED_WHOLE_MODE",
    "SELECTED_MODE",
    "SELECTION_MODES",
    "ValidatedAcceptedAnalysisDomainArtifactRefProjection",
    "ValidatedAcceptedAnalysisDomainProjection",
    "accepted_analysis_domain_identity_contract",
    "analysis_domain_bindings",
    "decode_committed_accepted_analysis_domain",
    "domain_set_from_authorities",
    "validate_accepted_analysis_domain_artifact_ref",
]
