"""Strict source-neutral authority for the reviewed final run basis.

This module composes accepted upstream authorities. It deliberately performs no spectral
estimation and keeps the affine SSASU background outside the resolved basis columns.

The `FinalRunBasisProposal` record carries `source_kind`, `source_choice`,
`source_checkpoint_sha256`, `channel_fluors`, `run_id` and its remaining declared fields.

The functions retain the selected spectral basis, component order and reviewed acquisition
facts through the operation. Numerical work is bounded by the configured pixel blocks, while
published references and results keep their input and settings identities."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import math
import re

from .compatibility import (
    BasisSourceResolution,
    align_library_measurements,
    QuantitativeTransformabilityReport,
    StructuralCompatibilityReport,
    SyntheticMotifProfileCompatibilityEvidence,
    compare_quantitative_transformability,
    compare_structural_compatibility,
    resolve_channel_fluor_identities,
    motif_fluorophore_inventory,
)
from .reference_models import (
    BasisSourceLineage,
    FluorophoreReferenceLibrary,
    ReferenceInputEvidence,
    QuantitativeTransformSignature,
    ResolvedBasisColumn,
    ResolvedRunBasis,
    ReviewRecord,
    SpectralMeasurementAxis,
)
from .reference_sampling import (
    TissueMaskBinding,
    validate_tissue_mask_binding_authority,
)
from .target_af import (
    FixedBasisQcPolicy,
    FixedBasisQcResult,
    ProposalReviewDecision,
    RunBasisCompatibilityEvidence,
    TargetAutofluorescenceArtifact,
    assemble_fixed_run_basis_candidate,
    evaluate_run_basis_qc,
)
from .typed_checksum import typed_checksum_sha256

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_KINDS = {"registered_motif", "control_target_af", "curated_spectra", "fluorophore_only", "cycle_references"}
_LIBRARY_CHOICES = {"import_reference_library", "construct_reference_library"}
_SCHEMA_NAME = "mif_pipeline.final_run_basis"
_SCHEMA_VERSION = 3


# ==============================================================================
#                                  Final basis
# ==============================================================================


class FinalRunBasisError(ValueError):
    """Final-basis evidence is malformed, stale, or contradictory."""


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise FinalRunBasisError(f"{label} must be nonempty literal text")
    return value


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise FinalRunBasisError(f"{label} must be a lowercase SHA-256")
    return value


def _timestamp(value: object, label: str) -> str:
    text = _text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FinalRunBasisError(f"{label} must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise FinalRunBasisError(f"{label} must include a timezone")
    return text


def _channel_inventory(values: tuple[str, ...]) -> tuple[str, ...]:
    identities = resolve_channel_fluor_identities(values)
    if identities != values:
        raise FinalRunBasisError(
            "final Channel.Fluor inventory must be literal, unique, and ordered"
        )
    return identities


def _quantitative_signature_sha256(
    signature: QuantitativeTransformSignature,
) -> str:
    return typed_checksum_sha256(
        {
            "normalization_policy": signature.normalization_policy,
            "response_transform_id": signature.response_transform_id,
            "response_transform_semantic": signature.response_transform_semantic,
            "response_transform_authority": signature.response_transform_authority,
            "measurements": [
                {
                    "measurement_id": item.measurement_id,
                    "correction_mode": item.correction_mode,
                    "intensity_representation": item.intensity_representation,
                    "effective_digital_maximum": item.effective_digital_maximum,
                    "digital_range_meaningful_bits": (item.digital_range_meaningful_bits),
                    "digital_range_authority": None if item.digital_range_authority is None else item.digital_range_authority.value,
                    "digital_range_profile_id": item.digital_range_profile_id,
                    "offset": item.offset,
                    "offset_authority": item.offset_authority.value,
                    "offset_profile_identifier": item.offset_profile_identifier,
                    "exposure_seconds": item.exposure_seconds,
                    "gain": item.gain,
                    "binning_area": item.binning_area,
                    "response_correction_multiplier": item.response_correction_multiplier,
                }
                for item in signature.measurements
            ],
        }
    )


@dataclass(frozen=True, slots=True)
class FinalBasisSourceBinding:
    role: str
    artifact_id: str
    artifact_sha256: str
    run_id: str | None = None

    def __post_init__(self) -> None:
        if not (self.role.startswith("accepted_cycle_basis:") and self.role.partition(":")[2].isdigit()) and self.role not in {
            "curated_spectral_library",
            "instrument_profile",
            "basis_source_resolution",
            "fluorophore_library",
            "target_autofluorescence_artifact",
        }:
            raise FinalRunBasisError("final basis source-binding role is unsupported")
        _text(self.artifact_id, "source artifact_id")
        _sha256(self.artifact_sha256, "source artifact_sha256")
        if self.run_id is not None:
            _text(self.run_id, "source run_id")


@dataclass(frozen=True, slots=True)
class FinalBasisCompatibilityEvidence:
    source_kind: str
    structural_reports: tuple[StructuralCompatibilityReport, ...]
    quantitative_reports: tuple[QuantitativeTransformabilityReport, ...]
    synthetic_profile_compatibility: SyntheticMotifProfileCompatibilityEvidence | None
    source_binding_sha256s: tuple[str, ...]
    evidence_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the FinalBasisCompatibilityEvidence fields when the record is constructed.

        The checks cover ``source_binding_sha256s``, ``source_kind``,
        ``synthetic_profile_compatibility``, ``structural_reports``,
        ``quantitative_reports``. Invalid field types, values or combinations raise the
        declared validation error before this instance can be used by its caller."""

        if self.source_kind not in _SOURCE_KINDS:
            raise FinalRunBasisError("final compatibility source kind is unsupported")
        if type(self.structural_reports) is not tuple or any(
            type(item) is not StructuralCompatibilityReport for item in self.structural_reports
        ):
            raise TypeError("structural_reports must contain exact typed reports")
        if type(self.quantitative_reports) is not tuple or any(
            type(item) is not QuantitativeTransformabilityReport
            for item in self.quantitative_reports
        ):
            raise TypeError("quantitative_reports must contain exact typed reports")
        if self.source_kind == "registered_motif":
            evidence = self.synthetic_profile_compatibility
            if (
                self.structural_reports
                or self.quantitative_reports
                or type(evidence) is not SyntheticMotifProfileCompatibilityEvidence
                or not evidence.compatible
            ):
                raise FinalRunBasisError(
                    "registered MOTIF requires only accepted synthetic compatibility"
                )
        elif self.source_kind in {"curated_spectra", "cycle_references"}:
            if (
                self.structural_reports
                or self.quantitative_reports
                or self.synthetic_profile_compatibility is not None
            ):
                raise FinalRunBasisError(
                    "Curated spectra must retain declared compatibility, not fabricated empirical checks"
                )
        elif (
            len(self.structural_reports) != 1
            or len(self.quantitative_reports) != 1
            or self.synthetic_profile_compatibility is not None
            or not self.structural_reports[0].compatible
            or not self.quantitative_reports[0].transformable
        ):
            raise FinalRunBasisError(
                "empirical final basis requires one accepted structural and quantitative report"
            )
        if type(self.source_binding_sha256s) is not tuple or not self.source_binding_sha256s:
            raise FinalRunBasisError("final compatibility requires ordered source-binding SHA-256s")
        for value in self.source_binding_sha256s:
            _sha256(value, "compatibility source-binding SHA-256")
        object.__setattr__(
            self,
            "evidence_sha256",
            typed_checksum_sha256(
                {
                    "source_kind": self.source_kind,
                    "structural_report_sha256s": [
                        item.content_sha256 for item in self.structural_reports
                    ],
                    "quantitative_report_sha256s": [
                        item.content_sha256 for item in self.quantitative_reports
                    ],
                    "synthetic_profile_compatibility_sha256": (
                        None
                        if self.synthetic_profile_compatibility is None
                        else self.synthetic_profile_compatibility.content_sha256
                    ),
                    "source_binding_sha256s": list(self.source_binding_sha256s),
                }
            ),
        )


def _require_columns_bound_to_source(
    columns: tuple[ResolvedBasisColumn, ...],
    *,
    component_kind: str,
    source_kind: str,
    binding: FinalBasisSourceBinding,
) -> None:
    selected = tuple(item for item in columns if item.component_kind == component_kind)
    if not selected:
        raise FinalRunBasisError(f"final source binding {binding.role!r} has no bound columns")
    expected = BasisSourceLineage(
        source_kind,
        binding.artifact_id,
        binding.artifact_sha256,
        binding.run_id,
    )
    if any(item.source != expected for item in selected):
        raise FinalRunBasisError(
            f"final source binding {binding.role!r} differs from column lineage"
        )


def _validate_source_bindings(proposal: "FinalRunBasisProposal") -> None:
    if proposal.source_kind == "cycle_references":
        from mif_pipeline.io.metadata.cycles import split_cycle_component
        bindings = {item.role: item for item in proposal.source_bindings}
        cycles = tuple(dict.fromkeys(split_cycle_component(column.component_identity)[1]
                                    for column in proposal.columns))
        if None in cycles or set(bindings) != {f"accepted_cycle_basis:{cycle}" for cycle in cycles}:
            raise FinalRunBasisError("Every cycle requires its accepted child basis")
        if proposal.compatibility.source_binding_sha256s != tuple(item.artifact_sha256 for item in proposal.source_bindings):
            raise FinalRunBasisError("Cycle compatibility differs from its accepted bases")
        for column in proposal.columns:
            binding = bindings[f"accepted_cycle_basis:{split_cycle_component(column.component_identity)[1]}"]
            if column.source != BasisSourceLineage("accepted_cycle_basis", binding.artifact_id,
                                                   binding.artifact_sha256, binding.run_id):
                raise FinalRunBasisError("A cycle column differs from its accepted basis lineage")
        return
    expected_roles = {
        "curated_spectra": ("curated_spectral_library",),
        "fluorophore_only": ("fluorophore_library",),
        "registered_motif": (
            "instrument_profile",
            "basis_source_resolution",
        ),
        "control_target_af": (
            "fluorophore_library",
            "target_autofluorescence_artifact",
        ),
    }[proposal.source_kind]
    roles = tuple(item.role for item in proposal.source_bindings)
    if roles != expected_roles:
        raise FinalRunBasisError("final source binding inventory is missing, extra, or reordered")
    if proposal.compatibility.source_binding_sha256s != tuple(
        item.artifact_sha256 for item in proposal.source_bindings
    ):
        raise FinalRunBasisError("final compatibility differs from ordered source bindings")
    bindings = dict(zip(roles, proposal.source_bindings, strict=True))
    if proposal.source_kind == "curated_spectra":
        binding = bindings["curated_spectral_library"]
        for kind in (("fluorophore", "profile_autofluorescence")
                     if proposal.af_behavior == "retain" else ("fluorophore",)):
            _require_columns_bound_to_source(
                proposal.columns,
                component_kind=kind,
                source_kind="curated_spectral_library",
                binding=binding,
            )
        return
    if proposal.source_kind == "registered_motif":
        profile_binding = bindings["instrument_profile"]
        resolution_binding = bindings["basis_source_resolution"]
        evidence = proposal.compatibility.synthetic_profile_compatibility
        assert evidence is not None
        if (
            profile_binding.artifact_id != evidence.profile.profile_id
            or profile_binding.artifact_sha256 != evidence.profile.resource_sha256
            or profile_binding.run_id is not None
            or resolution_binding.artifact_id != f"resolution:{proposal.run_id}"
            or resolution_binding.run_id != proposal.run_id
            or evidence.target_measurement_ids != proposal.axis.measurement_ids
            or evidence.target_parent_band_ids != proposal.axis.parent_band_ids
            or evidence.target_quantitative_signature != proposal.quantitative_signature
            or evidence.target_image_sha256 != proposal.target_image_sha256
        ):
            raise FinalRunBasisError("MOTIF source bindings differ from profile compatibility")
        _require_columns_bound_to_source(
            proposal.columns,
            component_kind="fluorophore",
            source_kind="instrument_profile",
            binding=profile_binding,
        )
        if proposal.af_behavior == "retain":
            _require_columns_bound_to_source(
                proposal.columns,
                component_kind="profile_autofluorescence",
                source_kind="instrument_profile",
                binding=profile_binding,
            )
        return
    library_binding = bindings["fluorophore_library"]
    _require_columns_bound_to_source(
        proposal.columns,
        component_kind="fluorophore",
        source_kind="fluorophore_reference_library",
        binding=library_binding,
    )
    if proposal.source_kind == "control_target_af":
        _require_columns_bound_to_source(
            proposal.columns,
            component_kind="target_autofluorescence",
            source_kind="target_autofluorescence_model",
            binding=bindings["target_autofluorescence_artifact"],
        )
        return


@dataclass(frozen=True, slots=True)
class FinalRunBasisProposal:
    source_kind: str
    source_choice: str
    af_behavior: str
    source_checkpoint_sha256: str
    channel_fluors: tuple[str, ...]
    run_id: str
    target_image_sha256: str
    axis: SpectralMeasurementAxis
    columns: tuple[ResolvedBasisColumn, ...]
    qc_policy: FixedBasisQcPolicy
    qc: FixedBasisQcResult
    compatibility: FinalBasisCompatibilityEvidence
    source_bindings: tuple[FinalBasisSourceBinding, ...]
    quantitative_signature: QuantitativeTransformSignature
    ssasu_context: None  # Null-only legacy wire field; learned-AF execution was retired.
    proposal_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the FinalRunBasisProposal fields when the record is constructed.

        The checks cover ``source_kind``, ``source_checkpoint_sha256``, ``channel_fluors``,
        ``run_id``, ``target_image_sha256``, ``columns``. Invalid field types, values or
        combinations raise the declared validation error before this instance can be used by
        its caller."""

        if self.source_kind not in _SOURCE_KINDS:
            raise FinalRunBasisError("final proposal source kind is unsupported")
        if self.source_kind == "registered_motif":
            if self.source_choice != "phenoimager_motif_profile":
                raise FinalRunBasisError("registered MOTIF source choice differs")
        elif self.source_kind == "cycle_references":
            if self.source_choice != "shared_cycle_reference_library":
                raise FinalRunBasisError("Shared cycle source choice differs")
        elif self.source_kind == "curated_spectra":
            if self.source_choice != "curated_reference_library":
                raise FinalRunBasisError("Curated source choice differs")
        elif self.source_choice not in _LIBRARY_CHOICES:
            raise FinalRunBasisError("library final source choice is unsupported")
        if self.af_behavior not in {"retain", "already_removed"}:
            raise FinalRunBasisError("final target AF choice is unsupported")
        if (self.source_kind == "control_target_af" and self.af_behavior != "retain") or (
            self.source_kind == "fluorophore_only" and self.af_behavior != "already_removed"
        ):
            raise FinalRunBasisError("final source kind differs from target AF choice")
        _sha256(self.source_checkpoint_sha256, "source checkpoint SHA-256")
        channel_fluors = _channel_inventory(self.channel_fluors)
        _text(self.run_id, "final proposal run_id")
        _sha256(self.target_image_sha256, "final proposal target image SHA-256")
        if type(self.axis) is not SpectralMeasurementAxis:
            raise TypeError("final proposal axis must be exact SpectralMeasurementAxis")
        if (
            type(self.columns) is not tuple
            or not self.columns
            or any(type(item) is not ResolvedBasisColumn for item in self.columns)
        ):
            raise TypeError("final proposal columns must be exact ResolvedBasisColumn records")
        identities = tuple(item.component_identity for item in self.columns)
        if len(identities) != len(set(identities)):
            raise FinalRunBasisError("final proposal has duplicate component identities")
        fluor_columns = tuple(
            item.fluorophore_identity
            for item in self.columns
            if item.component_kind == "fluorophore"
        )
        expected_fluors = (
            motif_fluorophore_inventory(channel_fluors)
            if self.source_kind in {"registered_motif", "curated_spectra", "cycle_references"}
            else channel_fluors
        )
        if self.source_kind in {"curated_spectra", "cycle_references"}:
            from mif_pipeline.io.metadata.cycles import split_cycle_component
            expected_fluors = tuple(split_cycle_component(name)[0] for name in expected_fluors)
        if self.source_kind == "cycle_references" and (
                tuple(item.component_identity for item in self.columns if item.component_kind == "fluorophore") != channel_fluors
                or any(split_cycle_component(item.component_identity)[0] != item.fluorophore_identity
                       for item in self.columns if item.component_kind == "fluorophore")):
            raise FinalRunBasisError("Cycle basis columns differ from the exact reviewed fluorophore mapping")
        if set(fluor_columns) != set(expected_fluors):
            raise FinalRunBasisError(
                "final fluorophore columns differ from literal Channel.Fluor inventory"
            )
        first_af = next(
            (
                index
                for index, item in enumerate(self.columns)
                if item.component_kind != "fluorophore"
            ),
            len(self.columns),
        )
        if (first_af < len(self.columns)) != (self.af_behavior == "retain") or any(
            item.component_kind == "fluorophore" for item in self.columns[first_af:]
        ):
            raise FinalRunBasisError(
                "final basis AF columns must match the target choice and follow fluorophores"
            )
        if type(self.qc_policy) is not FixedBasisQcPolicy:
            raise TypeError("final proposal QC policy must be exact FixedBasisQcPolicy")
        if type(self.qc) is not FixedBasisQcResult:
            raise TypeError("final proposal QC must be exact FixedBasisQcResult")
        recomputed_qc = evaluate_run_basis_qc(self.columns, self.axis, self.qc_policy)
        if recomputed_qc != self.qc or self.qc.disposition == "failed":
            raise FinalRunBasisError("final basis QC is stale or failed")
        if (
            type(self.compatibility) is not FinalBasisCompatibilityEvidence
            or self.compatibility.source_kind != self.source_kind
        ):
            raise TypeError("final proposal compatibility evidence differs")
        if (
            type(self.source_bindings) is not tuple
            or not self.source_bindings
            or any(type(item) is not FinalBasisSourceBinding for item in self.source_bindings)
        ):
            raise TypeError("final proposal source bindings are invalid")
        roles = tuple(item.role for item in self.source_bindings)
        if len(roles) != len(set(roles)):
            raise FinalRunBasisError("final proposal source-binding roles must be unique")
        if type(self.quantitative_signature) is not QuantitativeTransformSignature:
            raise TypeError("final proposal quantitative signature has wrong type")
        if self.quantitative_signature.measurement_ids != self.axis.measurement_ids:
            raise FinalRunBasisError("final quantitative axis differs from basis axis")
        if self.ssasu_context is not None:
            raise FinalRunBasisError(
                "Learned-autofluorescence models are not supported in this release; choose a spectral reference library or control image."
            )
        _validate_source_bindings(self)
        object.__setattr__(
            self,
            "proposal_sha256",
            typed_checksum_sha256(
                {
                    "source_kind": self.source_kind,
                    "source_choice": self.source_choice,
                    **({"af_behavior": self.af_behavior} if self.af_behavior != "retain" else {}),
                    "source_checkpoint_sha256": self.source_checkpoint_sha256,
                    "channel_fluors": list(self.channel_fluors),
                    "run_id": self.run_id,
                    "target_image_sha256": self.target_image_sha256,
                    "axis_sha256": typed_checksum_sha256(list(self.axis.measurement_ids)),
                    "column_sha256s": [item.spectrum_sha256 for item in self.columns],
                    "qc_policy_sha256": self.qc_policy.content_sha256,
                    "qc_sha256": self.qc.evidence_sha256,
                    "compatibility_sha256": self.compatibility.evidence_sha256,
                    "source_bindings": [
                        {
                            "role": item.role,
                            "artifact_id": item.artifact_id,
                            "artifact_sha256": item.artifact_sha256,
                            "run_id": item.run_id,
                        }
                        for item in self.source_bindings
                    ],
                    "quantitative_signature_sha256": _quantitative_signature_sha256(
                        self.quantitative_signature
                    ),
                    "ssasu_context_sha256": (None),
                }
            ),
        )

    @property
    def candidate_sha256(self) -> str:
        """Compatibility alias used only by the immutable final review builder."""
        return self.proposal_sha256

    @property
    def source_artifact_sha256s(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.source.content_sha256 for item in self.columns))

    @property
    def component_order(self) -> tuple[str, ...]:
        return tuple(item.component_identity for item in self.columns)


@dataclass(frozen=True, slots=True)
class FinalRunBasisArtifact:
    proposal: FinalRunBasisProposal
    decision: ProposalReviewDecision
    artifact_id: str
    created_at: str
    biomarkers: tuple[str | None, ...]
    basis: ResolvedRunBasis = field(init=False)
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the FinalRunBasisArtifact fields when the record is constructed.

        The checks cover ``artifact_id``, ``created_at``, ``proposal``, ``decision``.
        Invalid field types, values or combinations raise the declared validation error
        before this instance can be used by its caller."""

        if type(self.proposal) is not FinalRunBasisProposal:
            raise TypeError("final artifact proposal must be exact FinalRunBasisProposal")
        if type(self.decision) is not ProposalReviewDecision:
            raise TypeError("final artifact decision must be exact ProposalReviewDecision")
        if (
            self.decision.proposal_sha256 != self.proposal.proposal_sha256
            or self.decision.decision != "accepted"
        ):
            raise FinalRunBasisError(
                "final artifact requires its exact accepted final-basis decision"
            )
        if self.proposal.qc.disposition == "failed":
            raise FinalRunBasisError("failed final basis cannot be accepted")
        _text(self.artifact_id, "final artifact_id")
        _timestamp(self.created_at, "final artifact created_at")
        if type(self.biomarkers) is not tuple or len(self.biomarkers) != len(self.proposal.columns):
            raise FinalRunBasisError("Final biomarker assignments must cover the output components")
        for column, biomarker in zip(self.proposal.columns, self.biomarkers):
            if biomarker is not None:
                _text(biomarker, "component biomarker")
                if column.component_kind != "fluorophore":
                    raise FinalRunBasisError("Autofluorescence cannot have a biological marker")
        basis = ResolvedRunBasis(
            self.proposal.run_id,
            self.proposal.axis,
            self.proposal.columns,
            ReviewRecord(
                "accepted",
                self.decision.reviewer_id,
                self.decision.reviewed_at,
            ),
        )
        object.__setattr__(self, "basis", basis)
        object.__setattr__(
            self,
            "content_sha256",
            typed_checksum_sha256(
                {
                    "artifact_id": self.artifact_id,
                    "created_at": self.created_at,
                    "proposal_sha256": self.proposal.proposal_sha256,
                    "decision_sha256": self.decision.decision_sha256,
                    "biomarkers": list(self.biomarkers),
                    "basis_column_sha256s": [item.spectrum_sha256 for item in basis.columns],
                    "basis_review": [
                        basis.review.decision,
                        basis.review.reviewer_id,
                        basis.review.reviewed_at,
                    ],
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class FinalRunBasisRuntimeHandoff:
    run_id: str
    source_checkpoint_sha256: str
    artifact_id: str
    artifact_sha256: str
    proposal_sha256: str
    handoff_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _text(self.run_id, "final handoff run_id")
        _sha256(self.source_checkpoint_sha256, "final handoff source checkpoint")
        _text(self.artifact_id, "final handoff artifact_id")
        _sha256(self.artifact_sha256, "final handoff artifact_sha256")
        _sha256(self.proposal_sha256, "final handoff proposal_sha256")
        object.__setattr__(
            self,
            "handoff_sha256",
            typed_checksum_sha256(
                {
                    "semantic": "final_run_basis_runtime_handoff_v1",
                    "run_id": self.run_id,
                    "source_checkpoint_sha256": self.source_checkpoint_sha256,
                    "artifact_id": self.artifact_id,
                    "artifact_sha256": self.artifact_sha256,
                    "proposal_sha256": self.proposal_sha256,
                }
            ),
        )

    def validate_artifact(self, artifact: FinalRunBasisArtifact) -> None:
        if type(artifact) is not FinalRunBasisArtifact:
            raise TypeError("final handoff artifact has wrong exact type")
        validate_final_run_basis_authority(artifact)
        if (
            self.run_id != artifact.proposal.run_id
            or self.source_checkpoint_sha256 != artifact.proposal.source_checkpoint_sha256
            or self.artifact_id != artifact.artifact_id
            or self.artifact_sha256 != artifact.content_sha256
            or self.proposal_sha256 != artifact.proposal.proposal_sha256
        ):
            raise FinalRunBasisError("final runtime handoff differs from accepted artifact")


def _compatibility_from_task7(
    evidence: RunBasisCompatibilityEvidence,
    source_bindings: tuple[FinalBasisSourceBinding, ...],
) -> FinalBasisCompatibilityEvidence:
    return FinalBasisCompatibilityEvidence(
        "control_target_af",
        (evidence.structural_report,),
        (evidence.quantitative_report,),
        None,
        tuple(item.artifact_sha256 for item in source_bindings),
    )


def propose_fluorophore_final_run_basis(
    *, source_checkpoint_sha256: str, source_choice: str,
    channel_fluors: tuple[str, ...], fluorophore_library: FluorophoreReferenceLibrary,
    target: ReferenceInputEvidence, run_id: str, qc_policy: FixedBasisQcPolicy,
) -> FinalRunBasisProposal:
    """Compose an explicitly AF-free target basis from accepted reference shapes."""
    library = fluorophore_library
    axis, signature, row_order = align_library_measurements(library, target.axis)
    structural = compare_structural_compatibility(
        reference_axis=axis, candidate_axis=target.axis,
        reference_signature=library.structural_signature,
        candidate_signature=target.structural_signature,
    )
    quantitative = compare_quantitative_transformability(
        reference_axis=axis, candidate_axis=target.axis,
        reference_signature=signature,
        candidate_signature=target.quantitative_signature,
    )
    binding = FinalBasisSourceBinding("fluorophore_library", library.artifact_id, library.content_sha256)
    lineage = BasisSourceLineage("fluorophore_reference_library", library.artifact_id, library.content_sha256, None)
    by_fluor = {item.fluorophore_identity: item for item in library.spectra}
    columns = tuple(
        ResolvedBasisColumn(item.component_identity, item.fluorophore_identity, item.component_kind,
                            tuple(item.values[i] for i in row_order), item.normalization_convention, item.abundance_rescaling_factor, lineage)
        for item in (by_fluor[identity] for identity in _channel_inventory(channel_fluors))
    )
    return FinalRunBasisProposal(
        source_kind="fluorophore_only", source_choice=source_choice, af_behavior="already_removed",
        source_checkpoint_sha256=source_checkpoint_sha256, channel_fluors=channel_fluors,
        run_id=run_id, target_image_sha256=target.source_image_sha256, axis=target.axis,
        columns=columns, qc_policy=qc_policy, qc=evaluate_run_basis_qc(columns, target.axis, qc_policy),
        compatibility=FinalBasisCompatibilityEvidence("fluorophore_only", (structural,), (quantitative,), None, (library.content_sha256,)),
        source_bindings=(binding,), quantitative_signature=target.quantitative_signature, ssasu_context=None,
    )


def propose_control_final_run_basis(
    *,
    source_checkpoint_sha256: str,
    source_choice: str,
    channel_fluors: tuple[str, ...],
    fluorophore_library: FluorophoreReferenceLibrary,
    target_af_artifact: TargetAutofluorescenceArtifact,
    qc_policy: FixedBasisQcPolicy,
) -> FinalRunBasisProposal:
    """Recompute a control-derived final candidate from accepted bytes."""
    candidate = assemble_fixed_run_basis_candidate(
        source_choice=source_choice,
        channel_fluors=_channel_inventory(channel_fluors),
        fluorophore_library=fluorophore_library,
        target_af_artifact=target_af_artifact,
        qc_policy=qc_policy,
    )
    model = target_af_artifact.model
    columns = candidate.columns
    source_bindings = (
        FinalBasisSourceBinding(
            "fluorophore_library",
            fluorophore_library.artifact_id,
            fluorophore_library.content_sha256,
        ),
        FinalBasisSourceBinding(
            "target_autofluorescence_artifact",
            model.artifact_id,
            target_af_artifact.content_sha256,
            model.run_id,
        ),
    )
    return FinalRunBasisProposal(
        source_kind="control_target_af",
        source_choice=source_choice,
        af_behavior="retain",
        source_checkpoint_sha256=source_checkpoint_sha256,
        channel_fluors=channel_fluors,
        run_id=candidate.run_id,
        target_image_sha256=candidate.target_image_sha256,
        axis=candidate.axis,
        columns=columns,
        qc_policy=qc_policy,
        qc=candidate.qc,
        compatibility=_compatibility_from_task7(candidate.compatibility, source_bindings),
        source_bindings=source_bindings,
        quantitative_signature=model.quantitative_signature,
        ssasu_context=None,
    )


def propose_motif_final_run_basis(
    *,
    source_checkpoint_sha256: str,
    resolution: BasisSourceResolution,
    channel_fluors: tuple[str, ...],
    qc_policy: FixedBasisQcPolicy,
    af_behavior: str = "retain",
) -> FinalRunBasisProposal:
    """Recompute final QC over one resolver-proven packaged MOTIF profile."""
    if type(resolution) is not BasisSourceResolution:
        raise TypeError("resolution must be exact BasisSourceResolution")
    if resolution.authority_path != "trusted_packaged_motif_profile":
        raise FinalRunBasisError("MOTIF final basis requires trusted packaged authority")
    basis = resolution.resolved_basis
    if basis is None or resolution.resolution_evidence_sha256 is None:
        raise FinalRunBasisError("MOTIF resolution is not ready")
    inventory = _channel_inventory(channel_fluors)
    selected = tuple(
        item.fluorophore_identity for item in basis.columns if item.component_kind == "fluorophore"
    )
    if set(selected) != set(motif_fluorophore_inventory(inventory)):
        raise FinalRunBasisError("MOTIF basis differs from live Channel.Fluor inventory")
    source_ids = tuple(dict.fromkeys(item.source.artifact_id for item in basis.columns))
    source_hashes = tuple(dict.fromkeys(item.source.content_sha256 for item in basis.columns))
    if len(source_ids) != 1 or len(source_hashes) != 1:
        raise FinalRunBasisError("MOTIF final basis must bind one packaged profile")
    columns = tuple(c for c in basis.columns if af_behavior == "retain" or c.component_kind == "fluorophore")
    qc = evaluate_run_basis_qc(columns, basis.axis, qc_policy)
    source_bindings = (
        FinalBasisSourceBinding("instrument_profile", source_ids[0], source_hashes[0]),
        FinalBasisSourceBinding(
            "basis_source_resolution",
            f"resolution:{basis.run_id}",
            resolution.resolution_evidence_sha256,
            basis.run_id,
        ),
    )
    compatibility = FinalBasisCompatibilityEvidence(
        "registered_motif",
        (),
        (),
        resolution.synthetic_profile_compatibility,
        tuple(item.artifact_sha256 for item in source_bindings),
    )
    return FinalRunBasisProposal(
        source_kind="registered_motif",
        source_choice=resolution.choice.value,
        af_behavior=af_behavior,
        source_checkpoint_sha256=source_checkpoint_sha256,
        channel_fluors=inventory,
        run_id=basis.run_id,
        target_image_sha256=_sha256(resolution.target_image_sha256, "MOTIF target image SHA-256"),
        axis=basis.axis,
        columns=columns,
        qc_policy=qc_policy,
        qc=qc,
        compatibility=compatibility,
        source_bindings=source_bindings,
        quantitative_signature=resolution.target_quantitative_signature,
        ssasu_context=None,
    )


def accept_final_run_basis(
    *,
    proposal: FinalRunBasisProposal,
    decision: ProposalReviewDecision,
    artifact_id: str,
    created_at: str,
    biomarkers: tuple[str | None, ...],
) -> FinalRunBasisArtifact:
    artifact = FinalRunBasisArtifact(proposal, decision, artifact_id, created_at, biomarkers)
    validate_final_run_basis_authority(artifact)
    return artifact


def validate_final_run_basis_proposal(proposal: FinalRunBasisProposal) -> None:
    """Recursively reconstruct a proposal and reject stale nested authority."""
    if type(proposal) is not FinalRunBasisProposal:
        raise TypeError("proposal must be exact FinalRunBasisProposal")
    from . import reference_evidence_wire

    try:
        rebuilt = reference_evidence_wire._decode(
            reference_evidence_wire._encode(proposal),
            reference_evidence_wire._registry(),
        )
    except Exception as exc:
        raise FinalRunBasisError("final run-basis proposal is stale or invalid") from exc
    if type(rebuilt) is not FinalRunBasisProposal or rebuilt != proposal:
        raise FinalRunBasisError("final run-basis proposal is stale or invalid")


def validate_final_run_basis_authority(artifact: FinalRunBasisArtifact) -> None:
    """Recursively reconstruct a final artifact and reject stale derived fields."""
    if type(artifact) is not FinalRunBasisArtifact:
        raise TypeError("artifact must be exact FinalRunBasisArtifact")
    validate_final_run_basis_proposal(artifact.proposal)
    from . import reference_evidence_wire

    try:
        rebuilt = reference_evidence_wire._decode(
            reference_evidence_wire._encode(artifact),
            reference_evidence_wire._registry(),
        )
    except Exception as exc:
        raise FinalRunBasisError("final run-basis authority is stale or invalid") from exc
    if type(rebuilt) is not FinalRunBasisArtifact or rebuilt != artifact:
        raise FinalRunBasisError("final run-basis authority is stale or invalid")


def dumps_final_run_basis_artifact(artifact: FinalRunBasisArtifact) -> str:
    if type(artifact) is not FinalRunBasisArtifact:
        raise TypeError("artifact must be exact FinalRunBasisArtifact")
    validate_final_run_basis_authority(artifact)
    from . import reference_evidence_wire

    payload = {
        "schema_name": _SCHEMA_NAME,
        "schema_version": _SCHEMA_VERSION,
        "record": reference_evidence_wire._encode(artifact),
    }
    return (
        json.dumps(
            {**payload, "content_sha256": typed_checksum_sha256(payload)},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    )


def loads_final_run_basis_artifact(source: str | bytes) -> FinalRunBasisArtifact:
    if not isinstance(source, (str, bytes)):
        raise TypeError("source must be str or bytes")
    from . import reference_evidence_wire

    try:
        root = json.loads(
            source,
            object_pairs_hook=reference_evidence_wire._reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                FinalRunBasisError(f"nonfinite JSON constant {value!r}")
            ),
        )
        envelope = reference_evidence_wire._mapping(
            root,
            frozenset({"schema_name", "schema_version", "record", "content_sha256"}),
            "final run-basis envelope",
        )
        if (
            envelope["schema_name"] == _SCHEMA_NAME
            and type(envelope["schema_version"]) is int
            and envelope["schema_version"] in (1, 2)
        ):
            raise FinalRunBasisError(
                "final run-basis versions 1 and 2 require source-backed migration"
            )
        if (
            envelope["schema_name"] != _SCHEMA_NAME
            or type(envelope["schema_version"]) is not int
            or envelope["schema_version"] != _SCHEMA_VERSION
        ):
            raise FinalRunBasisError("unsupported final run-basis schema")
        payload = {
            "schema_name": envelope["schema_name"],
            "schema_version": envelope["schema_version"],
            "record": envelope["record"],
        }
        if typed_checksum_sha256(payload) != envelope["content_sha256"]:
            raise FinalRunBasisError("final run-basis envelope checksum differs")
        decoded = reference_evidence_wire._decode(
            envelope["record"], reference_evidence_wire._registry()
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        if isinstance(exc, FinalRunBasisError):
            raise
        raise FinalRunBasisError("invalid final run-basis artifact") from exc
    if type(decoded) is not FinalRunBasisArtifact:
        raise FinalRunBasisError("final run-basis payload has wrong type")
    validate_final_run_basis_authority(decoded)
    if dumps_final_run_basis_artifact(decoded) != (
        source.decode("utf-8") if isinstance(source, bytes) else source
    ):
        raise FinalRunBasisError("final run-basis bytes are noncanonical")
    return decoded


__all__ = [
    "FinalBasisCompatibilityEvidence",
    "FinalBasisSourceBinding",
    "FinalRunBasisArtifact",
    "FinalRunBasisError",
    "FinalRunBasisProposal",
    "FinalRunBasisRuntimeHandoff",
    "accept_final_run_basis",
    "dumps_final_run_basis_artifact",
    "loads_final_run_basis_artifact",
    "propose_control_final_run_basis",
    "propose_motif_final_run_basis",
    "validate_final_run_basis_authority",
    "validate_final_run_basis_proposal",
]
