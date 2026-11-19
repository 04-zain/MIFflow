"""Typed composition of reviewed consensus results into schema-v1 artifacts.

The `ReferenceLibraryPublicationMetadata` record carries `artifact_id`, `created_at`,
`reviewed_at`, `software_versions`, `policy_versions` and its remaining declared fields.

The functions retain the selected spectral basis, component order and reviewed acquisition
facts through the operation. Numerical work is bounded by the configured pixel blocks, while
published references and results keep their input and settings identities."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
import math

from .background import SampledReferenceRoi
from .purification import FluorophoreConsensusCandidate, PurifiedReferenceRoi
from .reference_models import (
    AcceptedSpectrum,
    ArtifactLineage,
    FluorophoreReferenceLibrary,
    LibraryQcEvidence,
    QcMetric,
    ReferenceInputEvidence,
    ReviewRecord,
    RoiEvidence,
    RoiBackgroundEvidence,
    VersionRecord,
)
from .reference_qc import (
    SpectrumMatrixColumn,
    SpectrumMatrixQcPolicy,
    evaluate_library_qc,
)

# ==============================================================================
#                         Reference service composition
# ==============================================================================


@dataclass(frozen=True, slots=True)
class ReferenceLibraryPublicationMetadata:
    """Non-scientific names, timestamps, and version labels for publication."""

    artifact_id: str
    created_at: str
    reviewed_at: str
    software_versions: tuple[tuple[str, str], ...]
    policy_versions: tuple[tuple[str, str], ...]
    creation_reason: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.artifact_id, "artifact_id"),
            (self.created_at, "created_at"),
            (self.reviewed_at, "reviewed_at"),
            (self.creation_reason, "creation_reason"),
        ):
            if type(value) is not str or not value or value != value.strip():
                raise ValueError(f"{label} must be nonempty literal text")
        for values, label in (
            (self.software_versions, "software_versions"),
            (self.policy_versions, "policy_versions"),
        ):
            if type(values) is not tuple or not values:
                raise TypeError(f"{label} must be a nonempty tuple")
            if any(
                type(item) is not tuple
                or len(item) != 2
                or any(type(part) is not str or not part for part in item)
                for item in values
            ):
                raise TypeError(f"{label} must contain exact name/version pairs")
            if len({item[0] for item in values}) != len(values):
                raise ValueError(f"{label} contains duplicate names")


def _roi_evidence(
    sampled: SampledReferenceRoi,
    *,
    purification: PurifiedReferenceRoi | None,
    consensus_inclusion: str,
    inclusion_reason: str,
    matched_roi_ids: tuple[str, ...] = (),
) -> RoiEvidence:
    sampling = sampled.sampling
    attestation = sampling.attestation
    rejected = sum(item.count for item in attestation.rejected_counts)
    return RoiEvidence(
        roi_id=sampled.selection.roi.roi_id,
        roi_role=sampled.selection.roi.roi_role,
        source_input_id=sampled.input_evidence.input_id,
        source_image_sha256=sampled.input_evidence.source_image_sha256,
        geometry_artifact_sha256=sampled.selection.roi.geometry_sha256,
        complete_vector_estimator_version="complete_vector_arithmetic_mean_v1",
        sampling_policy=attestation.rasterization_policy,
        sampling_seed=None,
        requested_pixel_count=None,
        total_pixel_count=attestation.candidate_pixel_count,
        accepted_pixel_count=sampling.accepted_vector_count,
        rejected_pixel_count=rejected,
        rejection_counts=attestation.rejected_counts,
        roi_coverage_fraction=float(
            sampling.accepted_vector_count / attestation.candidate_pixel_count
        ),
        sampled_chunk_ids=sampling.processing_chunk_ids,
        signal_measurement_id=(
            None if purification is None else purification.signal_dark_review.signal_measurement_id
        ),
        dark_measurement_ids=(
            () if purification is None else purification.signal_dark_review.dark_measurement_ids
        ),
        matched_af_roi_ids=matched_roi_ids,
        af_subtraction_factor=(
            None if purification is None else purification.af_subtraction_factor
        ),
        purified_spectrum=(None if purification is None else purification.purified_spectrum),
        consensus_inclusion=consensus_inclusion,
        inclusion_reason=inclusion_reason,
        qc_metrics=(),
        accepted=True,
        reason_codes=(),
    )


def compose_accepted_spectrum(
    candidate: FluorophoreConsensusCandidate,
    *,
    reviewer_id: str,
    reviewed_at: str,
) -> AcceptedSpectrum:
    """Project one exact reviewed consensus into the existing artifact schema."""

    if type(candidate) is not FluorophoreConsensusCandidate:
        raise TypeError("candidate must be exact FluorophoreConsensusCandidate")
    rebuilt = FluorophoreConsensusCandidate(
        component_identity=candidate.component_identity,
        fluorophore_identity=candidate.fluorophore_identity,
        policy=candidate.policy,
        measurement_ids=candidate.measurement_ids,
        values=candidate.values,
        roi_purifications=candidate.roi_purifications,
        inclusion_decisions=candidate.inclusion_decisions,
        roi_qc=candidate.roi_qc,
        disposition=candidate.disposition,
        reason_codes=candidate.reason_codes,
    )
    if rebuilt != candidate:
        raise ValueError("accepted spectrum requires exact consensus authority")
    if type(reviewer_id) is not str or not reviewer_id:
        raise ValueError("reviewer_id must be nonempty literal text")
    models = tuple(item.background_resolution.model for item in candidate.roi_purifications)
    if any(item is None for item in models):
        raise ValueError("accepted spectrum requires exact resolved background models")
    evidence: list[RoiEvidence] = []
    seen_nonpositive: set[tuple[str, str]] = set()
    regional_backgrounds = []
    for purification, decision in zip(
        candidate.roi_purifications,
        candidate.inclusion_decisions,
        strict=True,
    ):
        evidence.append(
            _roi_evidence(
                purification.positive,
                purification=purification,
                consensus_inclusion="included" if decision.included else "excluded",
                inclusion_reason=decision.reason,
                matched_roi_ids=((purification.control.selection.roi.roi_id,) if purification.control_set is None
                                 else purification.control_set.roi_ids),
            )
        )
        controls = ((purification.control, purification.background_resolution),) if purification.control_set is None else tuple(
            (member.control, member.background_resolution) for member in purification.control_set.members)
        for control, resolution in controls:
            regional_backgrounds.append(RoiBackgroundEvidence(purification.positive.selection.roi.roi_id,
                control.selection.roi.roi_id, resolution.model))
            key = (control.input_evidence.input_id, control.selection.roi.roi_id)
            if key not in seen_nonpositive:
                evidence.append(_roi_evidence(control, purification=None,
                    consensus_inclusion="not_applicable", inclusion_reason="matched_reviewed_control"))
                seen_nonpositive.add(key)
            for side in (resolution.stained_side, resolution.control_side):
                assert side is not None
                for background_roi in side.source_rois:
                    key = (background_roi.input_evidence.input_id, background_roi.selection.roi.roi_id)
                    if key in seen_nonpositive:
                        continue
                    evidence.append(_roi_evidence(background_roi, purification=None,
                        consensus_inclusion="not_applicable", inclusion_reason="accepted_background_evidence"))
                    seen_nonpositive.add(key)
    model = models[0]
    assert model is not None
    total = math.fsum(candidate.values)
    return AcceptedSpectrum(
        component_identity=candidate.component_identity,
        fluorophore_identity=candidate.fluorophore_identity,
        component_kind="fluorophore",
        normalization_convention="unit_l1",
        abundance_rescaling_factor=1.0,
        values=tuple(value / total for value in candidate.values),
        roi_evidence=tuple(evidence),
        background_model=model,
        regional_backgrounds=(tuple(regional_backgrounds) if any(item.control_set is not None
            for item in candidate.roi_purifications) or len(set(models)) > 1 else ()),
        review=ReviewRecord("accepted", reviewer_id, reviewed_at),
    )


def compose_accepted_library(
    spectra: tuple[AcceptedSpectrum, ...],
    candidates: tuple[FluorophoreConsensusCandidate, ...],
    qc_policy: SpectrumMatrixQcPolicy,
    metadata: ReferenceLibraryPublicationMetadata,
    *,
    reviewer_id: str,
) -> FluorophoreReferenceLibrary:
    """Build the existing portable library from exact accepted consensuses."""

    if type(spectra) is not tuple or type(candidates) is not tuple or not spectra:
        raise TypeError("accepted spectra and candidates must be nonempty tuples")
    if len(spectra) != len(candidates):
        raise ValueError("accepted spectra and consensus inventories differ")
    if type(qc_policy) is not SpectrumMatrixQcPolicy:
        raise TypeError("qc_policy must be exact SpectrumMatrixQcPolicy")
    if type(metadata) is not ReferenceLibraryPublicationMetadata:
        raise TypeError("publication metadata has the wrong exact type")
    for spectrum, candidate in zip(spectra, candidates, strict=True):
        expected = compose_accepted_spectrum(
            candidate,
            reviewer_id=spectrum.review.reviewer_id or "",
            reviewed_at=spectrum.review.reviewed_at,
        )
        if spectrum != expected:
            raise ValueError("accepted spectrum differs from exact consensus authority")
    columns = tuple(SpectrumMatrixColumn.from_consensus(item) for item in candidates)
    qc = evaluate_library_qc(columns, qc_policy)
    if qc.disposition != "pass" or qc.condition_number is None:
        raise ValueError("accepted library requires passing exact matrix QC")
    source_inputs: list[ReferenceInputEvidence] = []
    for candidate in candidates:
        for purification in candidate.roi_purifications:
            controls = ((purification.control, purification.background_resolution),) if purification.control_set is None else tuple(
                (member.control, member.background_resolution) for member in purification.control_set.members)
            for value in (purification.positive.input_evidence,
                *(source.input_evidence for source, _ in controls),
                *(roi.input_evidence for _, resolution in controls
                  for side in (resolution.stained_side, resolution.control_side)
                  if side is not None for roi in side.source_rois)):
                if value.input_id not in {item.input_id for item in source_inputs}:
                    source_inputs.append(value)
    first = source_inputs[0]
    metrics = tuple(
        QcMetric(
            name=item.name,
            value=item.value,
            passed=item.disposition == "pass",
        )
        for item in qc.metrics
    )
    library_qc = LibraryQcEvidence(
        policy_version=qc_policy.policy_version,
        rank=qc.rank,
        condition_number=qc.condition_number,
        pairwise_spectra=qc.pairwise_spectra,
        metrics=metrics,
        passed=True,
    )
    versions = lambda values: tuple(VersionRecord(*item) for item in values)
    return FluorophoreReferenceLibrary(
        artifact_id=metadata.artifact_id,
        created_at=metadata.created_at,
        axis=first.axis,
        structural_signature=first.structural_signature,
        quantitative_signature=first.quantitative_signature,
        source_inputs=tuple(source_inputs),
        normalization_convention="unit_l1",
        spectra=spectra,
        qc=library_qc,
        software_versions=versions(metadata.software_versions),
        policy_versions=versions(metadata.policy_versions),
        review=ReviewRecord("accepted", reviewer_id, metadata.reviewed_at),
        lineage=ArtifactLineage(
            predecessor_artifact_id=None,
            predecessor_content_sha256=None,
            source_artifact_sha256s=tuple(item.candidate_sha256 for item in candidates),
            creation_reason=metadata.creation_reason,
        ),
    )


__all__ = [
    "ReferenceLibraryPublicationMetadata",
    "compose_accepted_library",
    "compose_accepted_spectrum",
]
