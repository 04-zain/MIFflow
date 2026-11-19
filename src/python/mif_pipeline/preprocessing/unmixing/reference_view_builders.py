"""Stage-specific projections into bounded spectral-reference review records.

The functions retain the selected spectral basis, component order and reviewed acquisition
facts through the operation. Numerical work is bounded by the configured pixel blocks, while
published references and results keep their input and settings identities."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import replace
import json
import math

from .background import BackgroundModelResolution, SampledReferenceRoi
from .interactions import (
    ReferenceCheckpointKind,
    ReferenceInteractionRequest,
    ReferenceReasonCode,
)
from .purification import FluorophoreConsensusCandidate, PurifiedReferenceRoi
from .reference_view_records import (
    ReferenceReviewArtifactBinding,
    ReferenceReviewData,
    ReferenceReviewPoint,
    ReferenceReviewSeries,
    ReferenceReviewTable,
    ReferenceReviewTableColumn,
    ReferenceReviewTableRow,
)
from .final_basis import FinalRunBasisProposal
from .target_af import (
    FixedBasisQcResult,
    RunBasisCandidate,
    TargetAutofluorescenceProposal,
)

# ==============================================================================
#                            Reference view builders
# ==============================================================================


def _validate_request(
    request: ReferenceInteractionRequest,
    *,
    checkpoints: frozenset[ReferenceCheckpointKind],
    measurement_ids: tuple[str, ...],
    fluorophore: str | None,
    target_image_sha256: str | None = None,
    run_id: str | None = None,
    reasons: frozenset[ReferenceReasonCode],
) -> None:
    if type(request) is not ReferenceInteractionRequest:
        raise TypeError("review request must be an exact ReferenceInteractionRequest")
    if request.checkpoint not in checkpoints:
        raise ValueError("review request checkpoint does not match the stage result")
    if request.reason.code not in reasons:
        raise ValueError("review request reason does not match the stage result")
    if request.measurement_ids != measurement_ids:
        raise ValueError("review request measurement axis does not match the stage result")
    if fluorophore is not None and request.fluorophore != fluorophore:
        raise ValueError("review request fluorophore does not match the stage result")
    if fluorophore is None and request.fluorophore is not None:
        raise ValueError("unscoped review result cannot bind a fluorophore-scoped request")
    if target_image_sha256 is not None and request.target_image_sha256 != target_image_sha256:
        raise ValueError("review request target identity does not match the stage result")
    if run_id is not None and request.run_id != run_id:
        raise ValueError("review request run identity does not match the stage result")


def _revalidate(value: object, hash_name: str) -> object:
    declared = getattr(value, hash_name)
    try:
        rebuilt = replace(value)  # type: ignore[arg-type]
    except Exception as exc:
        raise ValueError("source result authority revalidation failed") from exc
    if getattr(rebuilt, hash_name) != declared:
        raise ValueError("source result authority checksum is stale")
    return rebuilt


def _spectral_series(
    series_id: str,
    label: str,
    values: tuple[float, ...],
    *,
    style_role: str,
    y_axis_label: str = "Normalized signal",
    y_unit: str | None = "relative",
) -> ReferenceReviewSeries:
    return ReferenceReviewSeries(
        series_id=series_id,
        label=label,
        x_axis_label="Measurement index",
        x_unit="index",
        y_axis_label=y_axis_label,
        y_unit=y_unit,
        style_role=style_role,
        points=tuple(
            ReferenceReviewPoint(float(index), float(value)) for index, value in enumerate(values)
        ),
    )


def _axis_table(measurement_ids: tuple[str, ...]) -> ReferenceReviewTable:
    return ReferenceReviewTable(
        "measurement-axis",
        "Ordered spectral measurement axis",
        (
            ReferenceReviewTableColumn("index", "Index", "index"),
            ReferenceReviewTableColumn("measurement_id", "Measurement ID", None),
        ),
        tuple(
            ReferenceReviewTableRow(f"measurement:{index}", item, (index, item))
            for index, item in enumerate(measurement_ids)
        ),
    )


def _make_data(
    request: ReferenceInteractionRequest,
    workflow_checkpoint_sha256: str | None,
    bindings: tuple[ReferenceReviewArtifactBinding, ...],
    series: tuple[ReferenceReviewSeries, ...],
    tables: tuple[ReferenceReviewTable, ...],
) -> ReferenceReviewData:
    return ReferenceReviewData.create(
        request=request,
        workflow_checkpoint_sha256=workflow_checkpoint_sha256,
        source_bindings=bindings,
        series=series,
        tables=tables,
    )


def build_roi_sampling_review_data(
    request: ReferenceInteractionRequest,
    sampled: SampledReferenceRoi,
    *,
    workflow_checkpoint_sha256: str | None = None,
) -> ReferenceReviewData:
    if type(sampled) is not SampledReferenceRoi:
        raise TypeError("sampled must be exact SampledReferenceRoi")
    sampled = _revalidate(sampled, "evidence_sha256")  # type: ignore[assignment]
    result = sampled.sampling
    checkpoint_by_role = {
        "positive": ReferenceCheckpointKind.POSITIVE_REFERENCE_ROIS,
        "af": ReferenceCheckpointKind.AF_REFERENCE_ROIS,
        "negative": ReferenceCheckpointKind.NEGATIVE_REFERENCE_ROIS,
        "background": ReferenceCheckpointKind.BACKGROUND_REFERENCE_ROIS,
    }
    reason_by_role = {
        "positive": frozenset({ReferenceReasonCode.POSITIVE_REFERENCE_ROIS_REQUIRED}),
        "af": frozenset(
            {
                ReferenceReasonCode.AF_REFERENCE_ROIS_REQUIRED,
                ReferenceReasonCode.UNSTAINED_REFERENCE_REQUIRED_OR_FALLBACK_CHOICE,
            }
        ),
        "negative": frozenset({ReferenceReasonCode.NEGATIVE_REFERENCE_ROIS_REQUIRED}),
        "background": frozenset(
            {
                ReferenceReasonCode.REFERENCE_BACKGROUND_MODEL_REQUIRED,
                ReferenceReasonCode.REFERENCE_BACKGROUND_ROIS_REQUIRED,
                ReferenceReasonCode.REFERENCE_BACKGROUND_QC_FAILED,
            }
        ),
    }
    role = sampled.selection.roi.roi_role
    fluorophore = sampled.input_evidence.fluorophore_identity
    _validate_request(
        request,
        checkpoints=frozenset({checkpoint_by_role[role]}),
        measurement_ids=result.measurement_ids,
        fluorophore=fluorophore,
        reasons=reason_by_role[role],
    )
    count_rows = (
        ReferenceReviewTableRow(
            "accepted", "Accepted vectors", ("accepted", result.accepted_vector_count)
        ),
        *(
            ReferenceReviewTableRow(
                f"rejected:{item.reason}",
                f"Rejected: {item.reason}",
                (item.reason, item.count),
            )
            for item in result.attestation.rejected_counts
        ),
    )
    return _make_data(
        request,
        workflow_checkpoint_sha256,
        (
            ReferenceReviewArtifactBinding(
                "roi_sampling_review_data", result.roi_id, result.sampling_evidence_sha256
            ),
        ),
        (
            _spectral_series(
                "roi-mean-spectrum",
                f"ROI {result.roi_id} mean",
                result.mean_vector,
                style_role="observed",
            ),
        ),
        (
            ReferenceReviewTable(
                "vector-counts",
                "Complete-vector disposition counts",
                (
                    ReferenceReviewTableColumn("disposition", "Disposition", None),
                    ReferenceReviewTableColumn("count", "Count", "vectors"),
                ),
                count_rows,
            ),
            _axis_table(result.measurement_ids),
        ),
    )


def build_background_review_data(
    request: ReferenceInteractionRequest,
    resolution: BackgroundModelResolution,
    *,
    stained_context: SampledReferenceRoi,
    workflow_checkpoint_sha256: str | None = None,
) -> ReferenceReviewData:
    if (
        type(resolution) is not BackgroundModelResolution
        or type(stained_context) is not SampledReferenceRoi
    ):
        raise TypeError("background review requires exact resolution and stained context")
    resolution = _revalidate(resolution, "resolution_sha256")  # type: ignore[assignment]
    stained_context = _revalidate(stained_context, "evidence_sha256")  # type: ignore[assignment]
    side = resolution.stained_side
    if side is not None and side.source_input_id != stained_context.input_evidence.input_id:
        raise ValueError("background resolution does not bind the stained review context")
    measurement_ids = (
        stained_context.sampling.measurement_ids if side is None else side.measurement_ids
    )
    _validate_request(
        request,
        checkpoints=frozenset({ReferenceCheckpointKind.BACKGROUND_REFERENCE_ROIS}),
        measurement_ids=measurement_ids,
        fluorophore=stained_context.input_evidence.fluorophore_identity,
        reasons=frozenset(
            {
                ReferenceReasonCode.REFERENCE_BACKGROUND_MODEL_REQUIRED,
                ReferenceReasonCode.REFERENCE_BACKGROUND_ROIS_REQUIRED,
                ReferenceReasonCode.REFERENCE_BACKGROUND_QC_FAILED,
            }
        ),
    )
    series = (
        *(
            ()
            if side is None
            else (
                _spectral_series(
                    "stained-background",
                    "Stained/reference background",
                    side.values,
                    style_role="background",
                ),
            )
        ),
        *(
            ()
            if resolution.control_side is None
            else (
                _spectral_series(
                    "control-background",
                    "Control background",
                    resolution.control_side.values,
                    style_role="background",
                ),
            )
        ),
    )
    rows = [
        ReferenceReviewTableRow("status", "Status", ("status", resolution.status)),
        ReferenceReviewTableRow("policy", "Policy", ("policy", resolution.policy.policy_version)),
        ReferenceReviewTableRow(
            "reason-codes",
            "Reason codes",
            (
                "reason_codes",
                ",".join(resolution.reason_codes) if resolution.reason_codes else None,
            ),
        ),
    ]
    if resolution.equivalence is not None:
        rows.extend(
            (
                ReferenceReviewTableRow(
                    "equivalence-passed",
                    "Equivalence passed",
                    ("equivalence_passed", resolution.equivalence.passed),
                ),
                ReferenceReviewTableRow(
                    "l2-difference",
                    "L2 difference",
                    ("l2_difference", resolution.equivalence.l2_difference),
                ),
                ReferenceReviewTableRow(
                    "l2-bound", "L2 bound", ("l2_bound", resolution.equivalence.l2_bound)
                ),
            )
        )
    return _make_data(
        request,
        workflow_checkpoint_sha256,
        (
            ReferenceReviewArtifactBinding(
                "background_resolution_review_data",
                f"background-resolution:{stained_context.input_evidence.input_id}",
                resolution.resolution_sha256,
            ),
            ReferenceReviewArtifactBinding(
                "stained_sampling_context",
                stained_context.selection.roi.roi_id,
                stained_context.evidence_sha256,
            ),
        ),
        series,
        (
            ReferenceReviewTable(
                "background-summary",
                "Background resolution summary",
                (
                    ReferenceReviewTableColumn("metric", "Metric", None),
                    ReferenceReviewTableColumn("value", "Value", None),
                ),
                tuple(rows),
            ),
            _axis_table(measurement_ids),
        ),
    )


def purification_clipping_metrics(result: PurifiedReferenceRoi) -> tuple[float | None, float | None]:
    """Return clipped L2 and clipped/retained L2 before spectrum normalization."""
    clipped_l2 = math.hypot(*(item.raw_value for item in result.negative_residual_clamps))
    retained_l2 = None if result.raw_residual is None else math.hypot(
        *(max(value, 0.0) for value in result.raw_residual)
    )
    clipped_ratio = None if not retained_l2 else clipped_l2 / retained_l2
    # Extreme finite bands can overflow their aggregate magnitude; the signed
    # per-band values remain available even when that summary is unavailable.
    clipped_l2 = clipped_l2 if math.isfinite(clipped_l2) else None
    if clipped_ratio is not None and not math.isfinite(clipped_ratio):
        clipped_ratio = None
    return clipped_l2, clipped_ratio


def build_consensus_review_data(
    request: ReferenceInteractionRequest,
    candidate: FluorophoreConsensusCandidate,
    *,
    workflow_checkpoint_sha256: str | None = None,
) -> ReferenceReviewData:
    if type(candidate) is not FluorophoreConsensusCandidate:
        raise TypeError("candidate must be exact FluorophoreConsensusCandidate")
    candidate = _revalidate(candidate, "candidate_sha256")  # type: ignore[assignment]
    _validate_request(
        request,
        checkpoints=frozenset({ReferenceCheckpointKind.FLUOROPHORE_CONSENSUS}),
        measurement_ids=candidate.measurement_ids,
        fluorophore=candidate.fluorophore_identity,
        reasons=frozenset({ReferenceReasonCode.REFERENCE_SPECTRUM_REVIEW_REQUIRED}),
    )
    member_series = tuple(
        _spectral_series(
            f"member:{item.positive.selection.roi.roi_id}",
            f"Member ROI {item.positive.selection.roi.roi_id}",
            item.purified_spectrum,  # type: ignore[arg-type]
            style_role="member",
        )
        for item in candidate.roi_purifications
    )
    rows = tuple(
        ReferenceReviewTableRow(
            item.roi_id,
            item.roi_id,
            (
                decision.included,
                item.spectral_angle_degrees,
                item.euclidean_distance,
                item.outlier_review_required,
            ),
        )
        for item, decision in zip(candidate.roi_qc, candidate.inclusion_decisions, strict=True)
    )
    return _make_data(
        request,
        workflow_checkpoint_sha256,
        (
            ReferenceReviewArtifactBinding(
                "fluorophore_consensus_review_data",
                candidate.component_identity,
                candidate.candidate_sha256,
            ),
        ),
        member_series
        + (
            _spectral_series(
                "consensus", "Consensus spectrum", candidate.values, style_role="consensus"
            ),
        ),
        (
            ReferenceReviewTable(
                "consensus-members",
                "Consensus member review",
                (
                    ReferenceReviewTableColumn("included", "Included", None),
                    ReferenceReviewTableColumn("spectral_angle", "Spectral angle", "degrees"),
                    ReferenceReviewTableColumn(
                        "euclidean_distance", "Euclidean distance", "relative"
                    ),
                    ReferenceReviewTableColumn("review_required", "Outlier review required", None),
                ),
                rows,
            ),
            ReferenceReviewTable(
                "consensus-summary",
                "Consensus disposition",
                (
                    ReferenceReviewTableColumn("disposition", "Disposition", None),
                    ReferenceReviewTableColumn("reason_codes", "Reason codes", None),
                ),
                (
                    ReferenceReviewTableRow(
                        "consensus",
                        "Consensus",
                        (candidate.disposition, ",".join(candidate.reason_codes) or None),
                    ),
                ),
            ),
            _axis_table(candidate.measurement_ids),
        ),
    )


def _fixed_qc_tables(qc: FixedBasisQcResult) -> tuple[ReferenceReviewTable, ...]:
    metric_rows = (
        ReferenceReviewTableRow("rank", "Rank", ("rank", qc.rank, None)),
        ReferenceReviewTableRow(
            "rank-tolerance", "Rank tolerance", ("rank_tolerance", qc.rank_tolerance, None)
        ),
        ReferenceReviewTableRow(
            "condition-number", "Condition number", ("condition_number", qc.condition_number, None)
        ),
        ReferenceReviewTableRow(
            "disposition", "Disposition", ("disposition", qc.disposition, None)
        ),
        ReferenceReviewTableRow(
            "reason-codes",
            "Reason codes",
            ("reason_codes", ",".join(qc.reason_codes) or None, None),
        ),
    )
    pair_rows = tuple(
        ReferenceReviewTableRow(
            f"{item.first_component_identity}:{item.second_component_identity}",
            f"{item.first_component_identity} / {item.second_component_identity}",
            (
                item.first_component_identity,
                item.second_component_identity,
                item.cosine_similarity,
                item.spectral_angle_degrees,
            ),
        )
        for item in qc.pairwise_spectra
    )
    tables = [
        ReferenceReviewTable(
            "fixed-basis-qc",
            "Fixed-basis QC",
            (
                ReferenceReviewTableColumn("metric", "Metric", None),
                ReferenceReviewTableColumn("value", "Value", None),
                ReferenceReviewTableColumn("threshold", "Threshold", None),
            ),
            metric_rows,
        ),
        ReferenceReviewTable(
            "fixed-basis-thresholds",
            "Fixed-basis QC policy thresholds",
            (
                ReferenceReviewTableColumn("metric", "Metric", None),
                ReferenceReviewTableColumn("value", "Value", None),
            ),
            (
                ReferenceReviewTableRow(
                    "svd-absolute",
                    "SVD absolute tolerance",
                    ("svd_absolute_tolerance", qc.policy.svd_absolute_tolerance),
                ),
                ReferenceReviewTableRow(
                    "svd-relative",
                    "SVD relative tolerance",
                    ("svd_relative_tolerance", qc.policy.svd_relative_tolerance),
                ),
                ReferenceReviewTableRow(
                    "cosine-review",
                    "Cosine review threshold",
                    ("cosine_review_threshold", qc.policy.cosine_review_threshold),
                ),
                ReferenceReviewTableRow(
                    "cosine-fail",
                    "Cosine fail threshold",
                    ("cosine_fail_threshold", qc.policy.cosine_fail_threshold),
                ),
                ReferenceReviewTableRow(
                    "condition-review",
                    "Condition-number review threshold",
                    ("condition_review_threshold", qc.policy.condition_review_threshold),
                ),
                ReferenceReviewTableRow(
                    "condition-fail",
                    "Condition-number fail threshold",
                    ("condition_fail_threshold", qc.policy.condition_fail_threshold),
                ),
            ),
        ),
    ]
    if pair_rows:
        tables.append(
            ReferenceReviewTable(
                "pairwise-spectra",
                "Pairwise spectrum metrics",
                (
                    ReferenceReviewTableColumn("first", "First component", None),
                    ReferenceReviewTableColumn("second", "Second component", None),
                    ReferenceReviewTableColumn("cosine", "Cosine similarity", None),
                    ReferenceReviewTableColumn("angle", "Spectral angle", "degrees"),
                ),
                pair_rows,
            )
        )
    return tuple(tables)


def build_fixed_basis_qc_review_data(
    request: ReferenceInteractionRequest,
    qc: FixedBasisQcResult,
    *,
    workflow_checkpoint_sha256: str | None = None,
) -> ReferenceReviewData:
    if type(qc) is not FixedBasisQcResult:
        raise TypeError("qc must be exact FixedBasisQcResult")
    qc = _revalidate(qc, "evidence_sha256")  # type: ignore[assignment]
    _validate_request(
        request,
        checkpoints=frozenset(
            {
                ReferenceCheckpointKind.TARGET_AF,
                ReferenceCheckpointKind.TARGET_AF_CONTROL_REVIEW,
                ReferenceCheckpointKind.FINAL_RUN_BASIS,
            }
        ),
        measurement_ids=qc.measurement_ids,
        fluorophore=None,
        reasons=frozenset(
            {
                ReferenceReasonCode.TARGET_AF_REVIEW_REQUIRED,
                ReferenceReasonCode.TARGET_AF_MODEL_QC_FAILED,
                ReferenceReasonCode.FINAL_RUN_BASIS_REVIEW_REQUIRED,
            }
        ),
    )
    series = tuple(
        _spectral_series(
            column.component_identity,
            column.component_identity,
            column.values,
            style_role="basis-column",
        )
        for column in qc.columns
    ) + (
        ReferenceReviewSeries(
            "singular-values",
            "Singular values",
            "Singular-value index",
            "index",
            "Singular value",
            None,
            "diagnostic",
            tuple(
                ReferenceReviewPoint(float(index), value)
                for index, value in enumerate(qc.singular_values)
            ),
        ),
    )
    return _make_data(
        request,
        workflow_checkpoint_sha256,
        (
            ReferenceReviewArtifactBinding(
                "fixed_basis_qc_review_data", "fixed-basis-qc", qc.evidence_sha256
            ),
        ),
        series,
        _fixed_qc_tables(qc) + (_axis_table(qc.measurement_ids),),
    )


def build_target_af_review_data(
    request: ReferenceInteractionRequest,
    proposal: TargetAutofluorescenceProposal,
    *,
    workflow_checkpoint_sha256: str | None = None,
) -> ReferenceReviewData:
    if type(proposal) is not TargetAutofluorescenceProposal:
        raise TypeError("proposal must be exact TargetAutofluorescenceProposal")
    proposal = _revalidate(proposal, "proposal_sha256")  # type: ignore[assignment]
    _validate_request(
        request,
        checkpoints=frozenset(
            {ReferenceCheckpointKind.TARGET_AF, ReferenceCheckpointKind.TARGET_AF_CONTROL_REVIEW}
        ),
        measurement_ids=proposal.qc.measurement_ids,
        fluorophore=None,
        target_image_sha256=proposal.control_binding.target_input.source_image_sha256,
        run_id=proposal.run_id,
        reasons=frozenset(
            {
                ReferenceReasonCode.TARGET_AF_REVIEW_REQUIRED,
                ReferenceReasonCode.TARGET_AF_MODEL_QC_FAILED,
            }
        ),
    )
    candidate_series = tuple(
        _spectral_series(item.component_identity, item.component_identity, item.values, style_role="target-af")  # type: ignore[arg-type]
        for item in proposal.estimation.components if item.values is not None
    ) + tuple(
        _spectral_series(
            f"corrected:{item.source_roi.selection.roi.roi_id}",
            f"{item.component_identity} after background clipping",
            item.corrected_values,
            style_role="corrected",
        )
        for item in proposal.estimation.candidates
    )
    candidate_series += tuple(
        _spectral_series(
            f"signed-background-difference:{item.source_roi.selection.roi.roi_id}",
            f"{item.component_identity} mean minus background (signed)",
            item.signed_background_difference,
            style_role="diagnostic",
        )
        for item in proposal.estimation.candidates
    )
    selection_rows = tuple(
        ReferenceReviewTableRow(
            item.roi_id,
            item.roi_id,
            (item.included, item.component_identity, item.decision.decision),
        )
        for item in proposal.estimation.selections
    )
    candidate_rows = tuple(
        ReferenceReviewTableRow(
            item.source_roi.selection.roi.roi_id,
            item.component_identity,
            (
                item.component_identity,
                item.source_roi.selection.roi.roi_id,
                item.disposition,
                ",".join(item.reason_codes) if item.reason_codes else None,
                item.evidence_sha256,
            ),
        )
        for item in proposal.estimation.candidates
    )
    policy = proposal.estimation.policy
    binding_role = (
        "proposed_target_af_qc"
        if any(
            item.pointer.role == "proposed_target_af_qc"
            and item.pointer.artifact_id == proposal.artifact_id
            and item.pointer.artifact_sha256 == proposal.proposal_sha256
            for item in request.proposed_artifacts
        )
        else "target_af_proposal"
    )
    return _make_data(
        request,
        workflow_checkpoint_sha256,
        (
            ReferenceReviewArtifactBinding(
                binding_role, proposal.artifact_id, proposal.proposal_sha256
            ),
        ),
        candidate_series,
        (
            ReferenceReviewTable(
                "target-af-rois",
                "Target-AF ROI decisions",
                (
                    ReferenceReviewTableColumn("included", "Included", None),
                    ReferenceReviewTableColumn("component", "Component", None),
                    ReferenceReviewTableColumn("decision", "Decision", None),
                ),
                selection_rows,
            ),
            ReferenceReviewTable(
                "target-af-candidate-diagnostics",
                "Target-AF candidate diagnostics",
                (
                    ReferenceReviewTableColumn("component", "Component", None),
                    ReferenceReviewTableColumn("roi_id", "ROI ID", None),
                    ReferenceReviewTableColumn("disposition", "Disposition", None),
                    ReferenceReviewTableColumn("reason_codes", "Reason codes", None),
                    ReferenceReviewTableColumn("evidence_sha256", "Evidence SHA-256", None),
                ),
                candidate_rows,
            ),
            ReferenceReviewTable(
                "target-af-estimation-policy",
                "Target-AF signal requirement",
                (
                    ReferenceReviewTableColumn("metric", "Metric", None),
                    ReferenceReviewTableColumn("value", "Value", None),
                ),
                (
                    ReferenceReviewTableRow(
                        "policy-version",
                        "Policy version",
                        ("policy_version", policy.policy_version),
                    ),
                    ReferenceReviewTableRow(
                        "minimum-l2-norm",
                        "Minimum L2 norm",
                        ("minimum_l2_norm", policy.minimum_l2_norm),
                    ),
                    ReferenceReviewTableRow(
                        "authority-sha256",
                        "Authority SHA-256",
                        ("authority_sha256", policy.authority_sha256),
                    ),
                    ReferenceReviewTableRow(
                        "policy-sha256", "Policy SHA-256", ("content_sha256", policy.content_sha256)
                    ),
                ),
            ),
            *_fixed_qc_tables(proposal.qc),
            _axis_table(proposal.qc.measurement_ids),
        ),
    )


def build_legacy_run_basis_review_data(
    request: ReferenceInteractionRequest,
    candidate: RunBasisCandidate,
    *,
    workflow_checkpoint_sha256: str | None = None,
) -> ReferenceReviewData:
    """Preserve the candidate projection without granting final authority."""
    if type(candidate) is not RunBasisCandidate:
        raise TypeError("candidate must be exact RunBasisCandidate")
    candidate = _revalidate(candidate, "candidate_sha256")  # type: ignore[assignment]
    _validate_request(
        request,
        checkpoints=frozenset({ReferenceCheckpointKind.FINAL_RUN_BASIS}),
        measurement_ids=candidate.axis.measurement_ids,
        fluorophore=None,
        target_image_sha256=candidate.target_image_sha256,
        run_id=candidate.run_id,
        reasons=frozenset({ReferenceReasonCode.FINAL_RUN_BASIS_REVIEW_REQUIRED}),
    )
    series = tuple(
        _spectral_series(
            item.component_identity, item.component_identity, item.values, style_role="basis-column"
        )
        for item in candidate.columns
    )
    lineage_rows = tuple(
        ReferenceReviewTableRow(
            item.component_identity,
            item.component_identity,
            (
                item.component_identity,
                item.component_kind,
                item.fluorophore_identity,
                item.source.source_kind,
                item.source.artifact_id,
                item.source.content_sha256,
            ),
        )
        for item in candidate.columns
    )
    return _make_data(
        request,
        workflow_checkpoint_sha256,
        tuple(
            ReferenceReviewArtifactBinding("run_basis_source", f"source:{index}", sha256)
            for index, sha256 in enumerate(candidate.source_artifact_sha256s)
        )
        + (
            ReferenceReviewArtifactBinding(
                "run_basis_candidate", candidate.run_id, candidate.candidate_sha256
            ),
        ),
        series,
        (
            ReferenceReviewTable(
                "run-basis-columns",
                "Ordered run-basis columns",
                (
                    ReferenceReviewTableColumn("component", "Component", None),
                    ReferenceReviewTableColumn("kind", "Kind", None),
                    ReferenceReviewTableColumn("fluorophore", "Fluorophore", None),
                    ReferenceReviewTableColumn("source_kind", "Source kind", None),
                    ReferenceReviewTableColumn("source_artifact", "Source artifact", None),
                    ReferenceReviewTableColumn("source_sha256", "Source SHA-256", None),
                ),
                lineage_rows,
            ),
            ReferenceReviewTable(
                "run-basis-qc",
                "Run-basis QC disposition",
                (
                    ReferenceReviewTableColumn("metric", "Metric", None),
                    ReferenceReviewTableColumn("value", "Value", None),
                ),
                (
                    ReferenceReviewTableRow(
                        "disposition", "Disposition", ("disposition", candidate.qc.disposition)
                    ),
                    ReferenceReviewTableRow("rank", "Rank", ("rank", candidate.qc.rank)),
                    ReferenceReviewTableRow(
                        "condition-number",
                        "Condition number",
                        ("condition_number", candidate.qc.condition_number),
                    ),
                ),
            ),
            _axis_table(candidate.axis.measurement_ids),
        )
        + (
            ReferenceReviewTable(
                "final-run-basis-authority",
                "Legacy run-basis candidate authority",
                (
                    ReferenceReviewTableColumn("field", "Field", None),
                    ReferenceReviewTableColumn("value", "Value", None),
                ),
                (
                    ReferenceReviewTableRow(
                        "source-kind",
                        "Source kind",
                        (
                            "source_kind",
                            "control_target_af",
                        ),
                    ),
                ),
            ),
        ),
    )


def _compatibility_value(value: object) -> str | int | float | bool | None:
    if type(value) is tuple:
        return json.dumps(list(value), separators=(",", ":"), ensure_ascii=False)
    if value is None or type(value) in {str, int, float, bool}:
        return value  # type: ignore[return-value]
    raise TypeError("compatibility review value is not a bounded scalar")


def _compatibility_table(
    table_id: str,
    label: str,
    reports: tuple[object, ...],
) -> ReferenceReviewTable:
    rows = tuple(
        ReferenceReviewTableRow(
            f"report:{report_index}:{field.field_name}",
            field.field_name,
            (
                report_index,
                field.field_name,
                _compatibility_value(field.reference_value),
                _compatibility_value(field.candidate_value),
                field.accepted,
                field.reason_code,
            ),
        )
        for report_index, report in enumerate(reports)
        for field in report.fields
    )
    return ReferenceReviewTable(
        table_id,
        label,
        (
            ReferenceReviewTableColumn("report", "Report", "index"),
            ReferenceReviewTableColumn("field", "Field", None),
            ReferenceReviewTableColumn("reference", "Reference value", None),
            ReferenceReviewTableColumn("candidate", "Candidate value", None),
            ReferenceReviewTableColumn("accepted", "Accepted", None),
            ReferenceReviewTableColumn("reason", "Reason code", None),
        ),
        rows,
    )


def _final_qc_tables(proposal: FinalRunBasisProposal) -> tuple[ReferenceReviewTable, ...]:
    qc = proposal.qc
    authority = ReferenceReviewTable(
        "final-qc-authority",
        "Final run-basis QC authority",
        (
            ReferenceReviewTableColumn("field", "Field", None),
            ReferenceReviewTableColumn("value", "Value", None),
        ),
        (
            ReferenceReviewTableRow(
                "policy-version", "Policy version", ("policy_version", qc.policy.policy_version)
            ),
            ReferenceReviewTableRow(
                "policy-authority",
                "Policy authority",
                ("authority_sha256", qc.policy.authority_sha256),
            ),
            ReferenceReviewTableRow(
                "policy-sha256", "Policy SHA-256", ("policy_sha256", qc.policy.content_sha256)
            ),
            ReferenceReviewTableRow(
                "qc-sha256", "QC evidence SHA-256", ("qc_sha256", qc.evidence_sha256)
            ),
            ReferenceReviewTableRow(
                "measurement-ids",
                "Measurement IDs",
                ("measurement_ids", json.dumps(list(qc.measurement_ids), separators=(",", ":"))),
            ),
        ),
    )
    columns = ReferenceReviewTable(
        "final-qc-columns",
        "Final run-basis QC column evidence",
        (
            ReferenceReviewTableColumn("component", "Component", None),
            ReferenceReviewTableColumn("kind", "Kind", None),
            ReferenceReviewTableColumn("normalization", "Normalization", None),
            ReferenceReviewTableColumn("rescaling", "Abundance rescaling", None),
            ReferenceReviewTableColumn("source", "Source evidence SHA-256", None),
            ReferenceReviewTableColumn("evidence", "Column evidence SHA-256", None),
        ),
        tuple(
            ReferenceReviewTableRow(
                item.component_identity,
                item.component_identity,
                (
                    item.component_identity,
                    item.component_kind,
                    item.normalization_convention,
                    item.abundance_rescaling_factor,
                    item.source_evidence_sha256,
                    item.evidence_sha256,
                ),
            )
            for item in qc.columns
        ),
    )
    singular = ReferenceReviewTable(
        "final-qc-singular-values",
        "Final run-basis singular values",
        (
            ReferenceReviewTableColumn("index", "Index", "index"),
            ReferenceReviewTableColumn("value", "Singular value", None),
        ),
        tuple(
            ReferenceReviewTableRow(
                f"singular-value:{index}",
                f"Singular value {index}",
                (index, value),
            )
            for index, value in enumerate(qc.singular_values)
        ),
    )
    return _fixed_qc_tables(qc) + (authority, columns, singular)


def _final_quantitative_signature_tables(
    proposal: FinalRunBasisProposal,
) -> tuple[ReferenceReviewTable, ReferenceReviewTable]:
    signature = proposal.quantitative_signature
    return (
        ReferenceReviewTable(
            "final-quantitative-signature",
            "Final quantitative transform signature",
            (
                ReferenceReviewTableColumn("field", "Field", None),
                ReferenceReviewTableColumn("value", "Value", None),
            ),
            (
                ReferenceReviewTableRow(
                    "normalization-policy",
                    "Normalization policy",
                    ("normalization_policy", signature.normalization_policy),
                ),
                ReferenceReviewTableRow(
                    "response-transform-id",
                    "Response transform ID",
                    ("response_transform_id", signature.response_transform_id),
                ),
                ReferenceReviewTableRow(
                    "response-transform-semantic",
                    "Response transform semantic",
                    (
                        "response_transform_semantic",
                        signature.response_transform_semantic,
                    ),
                ),
                ReferenceReviewTableRow(
                    "response-transform-authority",
                    "Response transform authority",
                    (
                        "response_transform_authority",
                        signature.response_transform_authority,
                    ),
                ),
            ),
        ),
        ReferenceReviewTable(
            "final-quantitative-measurements",
            "Final per-measurement quantitative transform",
            (
                ReferenceReviewTableColumn("measurement", "Measurement ID", None),
                ReferenceReviewTableColumn("digital_maximum", "Effective digital maximum", None),
                ReferenceReviewTableColumn("digital_authority", "Digital-range authority", None),
                ReferenceReviewTableColumn("digital_profile", "Digital-range profile", None),
                ReferenceReviewTableColumn("offset", "Offset", None),
                ReferenceReviewTableColumn("offset_authority", "Offset authority", None),
                ReferenceReviewTableColumn("offset_profile", "Offset profile", None),
                ReferenceReviewTableColumn("exposure", "Exposure", "seconds"),
                ReferenceReviewTableColumn("gain", "Gain", None),
                ReferenceReviewTableColumn("binning", "Binning area", None),
                ReferenceReviewTableColumn("response_multiplier", "Response multiplier", None),
            ),
            tuple(
                ReferenceReviewTableRow(
                    item.measurement_id,
                    item.measurement_id,
                    (
                        item.measurement_id,
                        item.effective_digital_maximum,
                        item.digital_range_authority.value if item.digital_range_authority is not None else "Not applied",
                        item.digital_range_profile_id,
                        item.offset,
                        item.offset_authority.value,
                        item.offset_profile_identifier,
                        item.exposure_seconds,
                        item.gain,
                        item.binning_area,
                        item.response_correction_multiplier,
                    ),
                )
                for item in signature.measurements
            ),
        ),
    )


def build_run_basis_review_data(
    request: ReferenceInteractionRequest,
    proposal: FinalRunBasisProposal,
    *,
    workflow_checkpoint_sha256: str | None = None,
) -> ReferenceReviewData:
    """Project only the exact, source-neutral final proposal for scientific review."""
    if type(proposal) is not FinalRunBasisProposal:
        raise TypeError("proposal must be exact FinalRunBasisProposal")
    proposal = _revalidate(proposal, "proposal_sha256")  # type: ignore[assignment]
    _validate_request(
        request,
        checkpoints=frozenset({ReferenceCheckpointKind.FINAL_RUN_BASIS}),
        measurement_ids=proposal.axis.measurement_ids,
        fluorophore=None,
        target_image_sha256=proposal.target_image_sha256,
        run_id=proposal.run_id,
        reasons=frozenset({ReferenceReasonCode.FINAL_RUN_BASIS_REVIEW_REQUIRED}),
    )
    series = tuple(
        _spectral_series(
            item.component_identity,
            item.component_identity,
            item.values,
            style_role="basis-column",
        )
        for item in proposal.columns
    )
    column_rows = tuple(
        ReferenceReviewTableRow(
            item.component_identity,
            item.component_identity,
            (
                item.component_identity,
                item.component_kind,
                item.fluorophore_identity,
                item.source.source_kind,
                item.source.artifact_id,
                item.source.content_sha256,
                item.source.run_id,
            ),
        )
        for item in proposal.columns
    )
    source_binding_rows = tuple(
        ReferenceReviewTableRow(
            item.role,
            item.role,
            (item.role, item.artifact_id, item.artifact_sha256, item.run_id),
        )
        for item in proposal.source_bindings
    )
    authority_rows = (
        ReferenceReviewTableRow(
            "source-kind", "Source kind", ("source_kind", proposal.source_kind)
        ),
        ReferenceReviewTableRow(
            "source-choice", "Source choice", ("source_choice", proposal.source_choice)
        ),
        ReferenceReviewTableRow(
            "source-checkpoint",
            "Source checkpoint SHA-256",
            ("source_checkpoint_sha256", proposal.source_checkpoint_sha256),
        ),
        ReferenceReviewTableRow(
            "proposal", "Proposal SHA-256", ("proposal_sha256", proposal.proposal_sha256)
        ),
        ReferenceReviewTableRow(
            "compatibility",
            "Compatibility SHA-256",
            ("compatibility_sha256", proposal.compatibility.evidence_sha256),
        ),
        ReferenceReviewTableRow(
            "target", "Target image SHA-256", ("target_image_sha256", proposal.target_image_sha256)
        ),
        ReferenceReviewTableRow(
            "channel-fluors",
            "Channel.Fluor inventory",
            ("channel_fluors", json.dumps(list(proposal.channel_fluors), separators=(",", ":"))),
        ),
    )
    compatibility_tables = (
        _compatibility_table(
            "final-structural-compatibility",
            "Final structural compatibility",
            proposal.compatibility.structural_reports,
        ),
        _compatibility_table(
            "final-quantitative-compatibility",
            "Final quantitative transformability",
            proposal.compatibility.quantitative_reports,
        ),
    )
    motif = proposal.compatibility.synthetic_profile_compatibility
    motif_tables = (
        ()
        if motif is None
        else (
            ReferenceReviewTable(
                "final-motif-compatibility",
                "Registered MOTIF synthetic compatibility",
                (
                    ReferenceReviewTableColumn("field", "Field", None),
                    ReferenceReviewTableColumn("value", "Value", None),
                ),
                (
                    ReferenceReviewTableRow(
                        "profile-id", "Profile ID", ("profile_id", motif.profile.profile_id)
                    ),
                    ReferenceReviewTableRow(
                        "profile-resource",
                        "Profile resource SHA-256",
                        ("profile_resource_sha256", motif.profile.resource_sha256),
                    ),
                    ReferenceReviewTableRow(
                        "profile-evidence",
                        "Profile evidence SHA-256",
                        ("profile_evidence_sha256", motif.profile.evidence_sha256),
                    ),
                    ReferenceReviewTableRow(
                        "target-measurements",
                        "Target measurements",
                        (
                            "target_measurement_ids",
                            json.dumps(list(motif.target_measurement_ids), separators=(",", ":")),
                        ),
                    ),
                    ReferenceReviewTableRow(
                        "target-parent-bands",
                        "Target parent bands",
                        (
                            "target_parent_band_ids",
                            json.dumps(list(motif.target_parent_band_ids), separators=(",", ":")),
                        ),
                    ),
                    ReferenceReviewTableRow(
                        "target-image",
                        "Target image SHA-256",
                        ("target_image_sha256", motif.target_image_sha256),
                    ),
                    ReferenceReviewTableRow(
                        "compatible", "Compatible", ("compatible", motif.compatible)
                    ),
                    ReferenceReviewTableRow(
                        "reason-codes",
                        "Reason codes",
                        (
                            "reason_codes",
                            json.dumps(list(motif.reason_codes), separators=(",", ":")),
                        ),
                    ),
                    ReferenceReviewTableRow(
                        "evidence", "Evidence SHA-256", ("evidence_sha256", motif.content_sha256)
                    ),
                ),
            ),
        )
    )
    return _make_data(
        request,
        workflow_checkpoint_sha256,
        (
            ReferenceReviewArtifactBinding(
                "proposed_run_basis_qc",
                proposal.run_id,
                proposal.proposal_sha256,
            ),
        ),
        series,
        (
            ReferenceReviewTable(
                "final-run-basis-authority",
                "Final run-basis proposal authority",
                (
                    ReferenceReviewTableColumn("field", "Field", None),
                    ReferenceReviewTableColumn("value", "Value", None),
                ),
                authority_rows,
            ),
            ReferenceReviewTable(
                "run-basis-columns",
                "Ordered final run-basis columns",
                (
                    ReferenceReviewTableColumn("component", "Component", None),
                    ReferenceReviewTableColumn("kind", "Kind", None),
                    ReferenceReviewTableColumn("fluorophore", "Fluorophore", None),
                    ReferenceReviewTableColumn("source_kind", "Source kind", None),
                    ReferenceReviewTableColumn("source_artifact", "Source artifact", None),
                    ReferenceReviewTableColumn("source_sha256", "Source SHA-256", None),
                    ReferenceReviewTableColumn("source_run", "Source run", None),
                ),
                column_rows,
            ),
            ReferenceReviewTable(
                "final-source-bindings",
                "Exact ordered final source bindings",
                (
                    ReferenceReviewTableColumn("role", "Role", None),
                    ReferenceReviewTableColumn("artifact", "Artifact ID", None),
                    ReferenceReviewTableColumn("sha256", "SHA-256", None),
                    ReferenceReviewTableColumn("run", "Run ID", None),
                ),
                source_binding_rows,
            ),
        )
        + compatibility_tables
        + motif_tables
        + _final_quantitative_signature_tables(proposal)
        + _final_qc_tables(proposal)
        + (_axis_table(proposal.axis.measurement_ids),),
    )


__all__ = [
    "build_background_review_data",
    "build_consensus_review_data",
    "build_fixed_basis_qc_review_data",
    "build_legacy_run_basis_review_data",
    "build_roi_sampling_review_data",
    "build_run_basis_review_data",
    "build_target_af_review_data",
]
