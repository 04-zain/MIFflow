"""Decision-bound factories for reviewed spectral-reference evidence.

This module joins immutable scientific results to exact workflow decisions.
It deliberately exposes no caller-authored evidence hashes or accepted result
pointers: every binding is derived from the proposal and decision records.

The `NegativeFallbackReviewProposal` record carries `positive`, `negative`, `background`,
`policy`, `measurement_ids` and its remaining declared fields.

The functions retain the selected spectral basis, component order and reviewed acquisition
facts through the operation. Numerical work is bounded by the configured pixel blocks, while
published references and results keep their input and settings identities."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .background import BackgroundModelResolution, SampledReferenceRoi
from .compatibility import (
    QuantitativeTransformabilityReport,
    StructuralCompatibilityReport,
    compare_quantitative_transformability,
    compare_structural_compatibility,
)
from .interactions import (
    EvidenceField,
    ReferenceActionCode,
    ReferenceCheckpointKind,
    ReferenceEvidence,
)
from .purification import (
    ConsensusInclusionDecision,
    MatchedControlReviewEvidence,
    NegativeFallbackReviewEvidence,
    PurifiedReferenceRoi,
    SignalDarkMeasurementReviewEvidence,
    _digest,
    _finite,
    _nonnegative,
    _text,
    _typed_tuple,
)
from .reference_workflow import (
    ReferenceDecisionRecord,
    ReferenceTransitionKind,
    ReferenceWorkflowCheckpoint,
    ReferenceWorkflowStatus,
    advance_reference_workflow,
)
from .typed_checksum import typed_checksum_sha256

# ==============================================================================
#                           Reference review bindings
# ==============================================================================


@dataclass(frozen=True, slots=True)
class MatchedControlReviewProposal:
    """One exact Task-5 positive/control pairing offered for explicit review."""

    positive: SampledReferenceRoi
    control: SampledReferenceRoi
    match_kind: str = field(init=False)
    structural_report: StructuralCompatibilityReport = field(init=False)
    quantitative_report: QuantitativeTransformabilityReport = field(init=False)
    proposal_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the MatchedControlReviewProposal fields when the record is constructed.

        The checks cover ``positive``, ``control``. Invalid field types, values or
        combinations raise the declared validation error before this instance can be used by
        its caller."""

        if (
            type(self.positive) is not SampledReferenceRoi
            or type(self.control) is not SampledReferenceRoi
        ):
            raise TypeError("matched-control proposal requires exact sampled ROIs")
        for item in (self.positive, self.control):
            if SampledReferenceRoi(item.selection, item.input_evidence, item.sampling) != item:
                raise ValueError("sampled ROI authority is stale")
        if self.positive.selection.roi.roi_role != "positive":
            raise ValueError("matched-control proposal requires a positive ROI")
        control_role = self.control.selection.roi.roi_role
        if control_role not in {"af", "negative"}:
            raise ValueError("matched-control proposal requires an AF or negative ROI")
        fluorophore = self.positive.input_evidence.fluorophore_identity
        if fluorophore is None or self.positive.selection.roi.role_link_id != fluorophore:
            raise ValueError("positive ROI does not bind its exact fluorophore")
        if control_role == "negative" and self.control.selection.roi.role_link_id != fluorophore:
            raise ValueError("negative ROI does not bind the positive fluorophore")
        structural = compare_structural_compatibility(
            reference_axis=self.positive.input_evidence.axis,
            candidate_axis=self.control.input_evidence.axis,
            reference_signature=self.positive.input_evidence.structural_signature,
            candidate_signature=self.control.input_evidence.structural_signature,
        )
        quantitative = compare_quantitative_transformability(
            reference_signature=self.positive.input_evidence.quantitative_signature,
            candidate_signature=self.control.input_evidence.quantitative_signature,
        )
        object.__setattr__(
            self,
            "match_kind",
            "negative_fallback" if control_role == "negative" else "dedicated_af",
        )
        object.__setattr__(self, "structural_report", structural)
        object.__setattr__(self, "quantitative_report", quantitative)
        object.__setattr__(self, "proposal_sha256", _digest(self, "proposal_sha256"))


def propose_matched_control_review(
    positive: SampledReferenceRoi,
    control: SampledReferenceRoi,
) -> MatchedControlReviewProposal:
    """Derive pairing and compatibility evidence from exact sampled inputs."""

    return MatchedControlReviewProposal(positive=positive, control=control)


def matched_control_review_request_evidence(
    proposal: MatchedControlReviewProposal,
) -> ReferenceEvidence:
    """Expose the exact pairing proposal for acknowledgement by one request."""

    if type(proposal) is not MatchedControlReviewProposal:
        raise TypeError("proposal must be an exact MatchedControlReviewProposal")
    if MatchedControlReviewProposal(proposal.positive, proposal.control) != proposal:
        raise ValueError("matched-control proposal authority is stale")
    positive_id = proposal.positive.selection.roi.roi_id
    control_id = proposal.control.selection.roi.roi_id
    return ReferenceEvidence(
        evidence_id=typed_checksum_sha256(
            {
                "kind": "matched_control_review_proposal",
                "proposal_sha256": proposal.proposal_sha256,
            }
        ),
        kind="matched_control_review_proposal",
        artifact_id=f"matched-control:{positive_id}:{control_id}",
        artifact_sha256=proposal.proposal_sha256,
        summary=(),
    )


def _require_accepted_review_decision(
    source: ReferenceWorkflowCheckpoint,
    result: ReferenceWorkflowCheckpoint,
    *,
    checkpoint: ReferenceCheckpointKind,
    option_value: str,
    fluorophore: str,
) -> ReferenceDecisionRecord:
    if (
        type(source) is not ReferenceWorkflowCheckpoint
        or type(result) is not ReferenceWorkflowCheckpoint
    ):
        raise TypeError("review authority requires exact workflow checkpoints")
    if (
        source.status is not ReferenceWorkflowStatus.AWAITING_RESPONSE
        or source.request is None
        or len(result.decisions) != len(source.decisions) + 1
        or result.decisions[:-1] != source.decisions
        or len(result.transition_log) != len(source.transition_log) + 1
        or result.transition_log[:-1] != source.transition_log
    ):
        raise ValueError("review decision is not the exact live workflow tip")
    decision = result.decisions[-1]
    transition = result.transition_log[-1]
    if (
        decision.source_checkpoint_sha256 != source.content_sha256
        or decision.request != source.request
        or decision.request.authoritative_state_sha256 != transition.source_state_sha256
        or transition.kind is not ReferenceTransitionKind.DECISION
        or transition.source_checkpoint_sha256 != source.content_sha256
        or transition.request_id != decision.request.request_id
        or transition.decision_record_sha256 != decision.content_sha256
        or transition.revision != result.revision
        or result.revision != source.revision + 1
        or result.prior_checkpoint_sha256 != source.content_sha256
    ):
        raise ValueError("review decision is not bound to the live workflow transition")
    if (
        decision.request.checkpoint is not checkpoint
        or decision.request.fluorophore != fluorophore
        or decision.response.action is not ReferenceActionCode.ACCEPT
        or decision.response.option_value != option_value
        or decision.response.request_id != decision.request.request_id
        or decision.response.request_revision != decision.request.revision
    ):
        raise ValueError("review decision does not bind the required checkpoint")
    next_evidence = () if result.request is None else result.request.evidence
    next_proposed = () if result.request is None else result.request.proposed_artifacts
    replayed = advance_reference_workflow(
        source,
        decision.response,
        next_evidence=next_evidence,
        next_proposed_artifacts=next_proposed,
    )
    # Saved request wording/options are presentation snapshots. Their current
    # labels may change without changing a reviewed scientific transition.
    # Keep exact source decision and transition checks, and compare all target
    # scientific state rather than regenerating the historical presentation.
    if (replayed._authoritative_state_mapping() != result._authoritative_state_mapping()
            or replayed.decisions != result.decisions or replayed.transition_log != result.transition_log):
        raise ValueError("review workflow transition does not replay exactly")
    return decision


def bind_matched_control_review(
    proposal: MatchedControlReviewProposal,
    source: ReferenceWorkflowCheckpoint,
    result: ReferenceWorkflowCheckpoint,
) -> MatchedControlReviewEvidence:
    """Bind one acknowledged exact pairing proposal to its review decision."""

    if type(proposal) is not MatchedControlReviewProposal:
        raise TypeError("proposal must be an exact MatchedControlReviewProposal")
    proposal = MatchedControlReviewProposal(proposal.positive, proposal.control)
    fluorophore = proposal.positive.input_evidence.fluorophore_identity
    assert fluorophore is not None
    negative = proposal.match_kind == "negative_fallback"
    decision = _require_accepted_review_decision(
        source,
        result,
        checkpoint=(
            ReferenceCheckpointKind.NEGATIVE_REFERENCE_ROIS
            if negative
            else ReferenceCheckpointKind.AF_REFERENCE_ROIS
        ),
        option_value=("accept_negative_reference_rois" if negative else "accept_af_reference_rois"),
        fluorophore=fluorophore,
    )
    if (
        source.measurement_ids != proposal.positive.sampling.measurement_ids
        or source.measurement_ids != proposal.control.sampling.measurement_ids
    ):
        raise ValueError("matched-control workflow measurement axis differs")
    expected_evidence = matched_control_review_request_evidence(proposal)
    if tuple(
        item
        for item in decision.request.evidence
        if item.kind == expected_evidence.kind and item.artifact_sha256 == proposal.proposal_sha256
    ) != (expected_evidence,):
        raise ValueError("review decision lacks the exact matched-control proposal evidence")
    if len(decision.response.artifacts) != 1:
        raise ValueError("matched-control review requires one exact ROI artifact")
    artifact = decision.response.artifacts[0]
    if (
        artifact.role != ("negative_reference_rois" if negative else "af_reference_rois")
        or artifact.subject_id != (fluorophore if negative or source.control_strategy == "per_stain_negative_controls" else "shared")
        or artifact.artifact_sha256 != proposal.control.selection.export.content_sha256
        or decision.response.selections
    ):
        raise ValueError("matched-control review artifact or inventory differs")
    binding = typed_checksum_sha256(
        {
            "semantic": "matched_control_review_binding_v1",
            "proposal_sha256": proposal.proposal_sha256,
            "decision_sha256": decision.content_sha256,
        }
    )
    return MatchedControlReviewEvidence(
        positive_roi_id=proposal.positive.selection.roi.roi_id,
        control_roi_id=proposal.control.selection.roi.roi_id,
        match_kind=proposal.match_kind,
        positive_sampling_evidence_sha256=(proposal.positive.sampling.sampling_evidence_sha256),
        control_sampling_evidence_sha256=(proposal.control.sampling.sampling_evidence_sha256),
        structural_report=proposal.structural_report,
        quantitative_report=proposal.quantitative_report,
        review_binding_sha256=binding,
        accepted=True,
    )


def bind_signal_dark_measurement_review(
    positive: SampledReferenceRoi,
    control: SampledReferenceRoi,
    source: ReferenceWorkflowCheckpoint,
    result: ReferenceWorkflowCheckpoint,
) -> SignalDarkMeasurementReviewEvidence:
    """Bind one exact accepted signal/dark decision to its sampled ROI pair."""

    if type(positive) is not SampledReferenceRoi or type(control) is not SampledReferenceRoi:
        raise TypeError("signal/dark review requires exact sampled ROIs")
    for item in (positive, control):
        if SampledReferenceRoi(item.selection, item.input_evidence, item.sampling) != item:
            raise ValueError("sampled ROI authority is stale")
    if positive.selection.roi.roi_role != "positive" or control.selection.roi.roi_role not in {
        "af",
        "negative",
    }:
        raise ValueError("signal/dark review requires a positive and AF/negative ROI")
    fluorophore = positive.input_evidence.fluorophore_identity
    if fluorophore is None:
        raise ValueError("signal/dark positive ROI lacks fluorophore identity")
    if (
        control.selection.roi.roi_role == "negative"
        and control.input_evidence.fluorophore_identity != fluorophore
    ):
        raise ValueError("signal/dark negative ROI fluorophore identity differs")
    axis = positive.sampling.measurement_ids
    if control.sampling.measurement_ids != axis:
        raise ValueError("signal/dark sampled ROI measurement axes differ")
    decision = _require_accepted_review_decision(
        source,
        result,
        checkpoint=ReferenceCheckpointKind.SIGNAL_DARK_MEASUREMENTS,
        option_value="accept_signal_dark_measurements",
        fluorophore=fluorophore,
    )
    if source.measurement_ids != axis or decision.request.measurement_ids != axis:
        raise ValueError("signal/dark workflow measurement axis differs")
    superseded = {
        request_id
        for transition in source.transition_log
        if transition.superseded_request_ids
        for request_id in transition.superseded_request_ids
    }
    reselections = tuple(
        index
        for index, prior in enumerate(source.decisions)
        if prior.request.fluorophore == fluorophore
        and prior.response.action is ReferenceActionCode.RESELECT
        and prior.response.option_value != "revise_signal_dark_measurements"
    )
    lineage_start = 0 if not reselections else reselections[-1] + 1
    current_decisions = tuple(
        prior
        for prior in source.decisions[lineage_start:]
        if prior.request.fluorophore == fluorophore
        and prior.response.action is ReferenceActionCode.ACCEPT
        and prior.request.request_id not in superseded
    )

    def current_artifact(role: str, subject: str):
        candidates = tuple(
            artifact
            for prior in current_decisions
            for artifact in prior.response.artifacts
            if artifact.role == role and artifact.subject_id == subject
        )
        return None if not candidates else candidates[-1]

    positive_artifact = current_artifact("positive_reference_rois", fluorophore)
    control_role = (
        "af_reference_rois" if control.selection.roi.roi_role == "af" else "negative_reference_rois"
    )
    control_subject = "shared" if control_role == "af_reference_rois" and source.control_strategy == "shared_unstained_control" else fluorophore
    control_artifact = current_artifact(control_role, control_subject)
    if (
        positive_artifact is None
        or control_artifact is None
        or positive_artifact.artifact_sha256 != positive.selection.export.content_sha256
        or control_artifact.artifact_sha256 != control.selection.export.content_sha256
    ):
        raise ValueError("signal/dark decision lacks the current reviewed positive/control ROI")
    if decision.response.artifacts:
        raise ValueError("signal/dark decision cannot carry artifact pointers")
    selections = decision.response.selections
    signal = tuple(item for item in selections if item.role == "signal_measurement")
    dark = tuple(item for item in selections if item.role == "dark_measurement")
    if (
        len(signal) != 1
        or not dark
        or len(dark) != len({item.identifier for item in dark})
        or any(item.identifier not in axis for item in (*signal, *dark))
        or signal[0].identifier in {item.identifier for item in dark}
    ):
        raise ValueError("signal/dark decision selection inventory is invalid")
    axis_index = {item: index for index, item in enumerate(axis)}
    canonical_dark = tuple(sorted(dark, key=lambda item: axis_index[item.identifier]))
    if selections != (*signal, *canonical_dark):
        raise ValueError("signal/dark selections are not in canonical axis order")
    signal_id = signal[0].identifier
    dark_ids = tuple(item.identifier for item in dark)
    excluded_ids = tuple(item for item in axis if item != signal_id and item not in set(dark_ids))
    return SignalDarkMeasurementReviewEvidence(
        signal_measurement_id=signal_id,
        dark_measurement_ids=dark_ids,
        excluded_measurement_ids=excluded_ids,
        fluorophore_identity=fluorophore,
        positive_roi_id=positive.selection.roi.roi_id,
        control_roi_id=control.selection.roi.roi_id,
        positive_input_id=positive.input_evidence.input_id,
        control_input_id=control.input_evidence.input_id,
        positive_sampling_evidence_sha256=positive.sampling.sampling_evidence_sha256,
        control_sampling_evidence_sha256=control.sampling.sampling_evidence_sha256,
        positive_source_image_sha256=positive.input_evidence.source_image_sha256,
        control_source_image_sha256=control.input_evidence.source_image_sha256,
        reviewer_id=decision.response.reviewer_id,
        source_checkpoint_sha256=source.content_sha256,
        result_checkpoint_sha256=result.content_sha256,
        decision_record_sha256=decision.content_sha256,
        accepted=True,
    )


@dataclass(frozen=True, slots=True)
class NegativeFallbackReviewPolicy:
    """Numerical denominator floor for the observed negative/positive signal ratio."""

    policy_version: str
    negativity_scale_floor: float
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _text(self.policy_version, "negative-fallback review policy version")
        if _nonnegative(self.negativity_scale_floor, "negativity scale floor") <= 0.0:
            raise ValueError("negativity scale floor must be positive")
        object.__setattr__(self, "content_sha256", _digest(self, "content_sha256"))


@dataclass(frozen=True, slots=True)
class NegativeFallbackReviewProposal:
    """Exact background-corrected negativity metrics for every measurement."""

    positive: SampledReferenceRoi
    negative: SampledReferenceRoi
    background: BackgroundModelResolution
    policy: NegativeFallbackReviewPolicy
    measurement_ids: tuple[str, ...] = field(init=False)
    per_measurement_negativity_ratios: tuple[float, ...] = field(init=False)
    proposal_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the NegativeFallbackReviewProposal fields when the record is constructed.

        The checks cover ``positive``, ``negative``, ``policy``, ``background``. Invalid
        field types, values or combinations raise the declared validation error before this
        instance can be used by its caller."""

        if (
            type(self.positive) is not SampledReferenceRoi
            or type(self.negative) is not SampledReferenceRoi
        ):
            raise TypeError("negative-fallback proposal requires exact sampled ROIs")
        for item in (self.positive, self.negative):
            if SampledReferenceRoi(item.selection, item.input_evidence, item.sampling) != item:
                raise ValueError("sampled ROI authority is stale")
        if self.positive.selection.roi.roi_role != "positive" or (
            self.negative.selection.roi.roi_role != "negative"
        ):
            raise ValueError("negative-fallback proposal requires positive/negative ROIs")
        if (
            self.negative.input_evidence.input_role != "single_stain_negative_control"
            or self.positive.input_evidence.fluorophore_identity != self.negative.input_evidence.fluorophore_identity
            or self.positive.selection.roi.role_link_id != self.negative.selection.roi.role_link_id
        ):
            raise ValueError("Negative control requires the matching fluorophore evidence")
        if type(self.background) is not BackgroundModelResolution:
            raise TypeError("background must be an exact BackgroundModelResolution")
        rebuilt_background = BackgroundModelResolution(
            status=self.background.status,
            stained_side=self.background.stained_side,
            control_side=self.background.control_side,
            model=self.background.model,
            equivalence=self.background.equivalence,
            compatibility=self.background.compatibility,
            policy=self.background.policy,
            authority_evidence_sha256s=self.background.authority_evidence_sha256s,
            reason_codes=self.background.reason_codes,
        )
        if (
            rebuilt_background != self.background
            or self.background.status != "resolved"
            or self.background.model is None
        ):
            raise ValueError("negative-fallback proposal requires a resolved exact background")
        if type(self.policy) is not NegativeFallbackReviewPolicy:
            raise TypeError("policy must be an exact NegativeFallbackReviewPolicy")
        if (
            NegativeFallbackReviewPolicy(
                self.policy.policy_version,
                self.policy.negativity_scale_floor,
            )
            != self.policy
        ):
            raise ValueError("negative-fallback review policy authority is stale")
        axis = self.positive.sampling.measurement_ids
        if axis != self.negative.sampling.measurement_ids:
            raise ValueError("negative-fallback measurement axes differ")
        model = self.background.model
        if (
            model.stained_background.source_input_id != self.positive.input_evidence.input_id
            or model.control_background.source_input_id != self.negative.input_evidence.input_id
            or model.stained_background.source_image_sha256
            != self.positive.input_evidence.source_image_sha256
            or model.control_background.source_image_sha256
            != self.negative.input_evidence.source_image_sha256
        ):
            raise ValueError("negative-fallback background does not bind both samples")
        corrected_positive = tuple(
            max(_finite(float(value - offset), "reference mean minus background"), 0.0)
            for value, offset in zip(
                self.positive.sampling.mean_vector,
                model.stained_background.values,
                strict=True,
            )
        )
        corrected_negative = tuple(
            max(_finite(float(value - offset), "reference mean minus background"), 0.0)
            for value, offset in zip(
                self.negative.sampling.mean_vector,
                model.control_background.values,
                strict=True,
            )
        )
        ratios = tuple(
            float(negative / max(positive, self.policy.negativity_scale_floor))
            for positive, negative in zip(corrected_positive, corrected_negative, strict=True)
        )
        object.__setattr__(self, "measurement_ids", axis)
        object.__setattr__(self, "per_measurement_negativity_ratios", ratios)
        object.__setattr__(self, "proposal_sha256", _digest(self, "proposal_sha256"))


def propose_negative_fallback_review(
    positive: SampledReferenceRoi,
    negative: SampledReferenceRoi,
    background: BackgroundModelResolution,
    policy: NegativeFallbackReviewPolicy,
) -> NegativeFallbackReviewProposal:
    """Derive all per-measurement fallback-negativity ratios from exact evidence."""

    return NegativeFallbackReviewProposal(positive, negative, background, policy)


def negative_fallback_review_request_evidence(
    proposal: NegativeFallbackReviewProposal,
) -> ReferenceEvidence:
    """Expose exact derived fallback metrics before signal selection."""

    if type(proposal) is not NegativeFallbackReviewProposal:
        raise TypeError("proposal must be an exact NegativeFallbackReviewProposal")
    if (
        NegativeFallbackReviewProposal(
            proposal.positive, proposal.negative, proposal.background, proposal.policy
        )
        != proposal
    ):
        raise ValueError("negative-fallback proposal authority is stale")
    fluorophore = proposal.positive.input_evidence.fluorophore_identity
    assert fluorophore is not None
    return ReferenceEvidence(
        evidence_id=typed_checksum_sha256(
            {
                "kind": "negative_fallback_review_proposal",
                "proposal_sha256": proposal.proposal_sha256,
            }
        ),
        kind="negative_fallback_review_proposal",
        artifact_id=(
            f"negative-fallback:{fluorophore}:" f"{proposal.negative.selection.roi.roi_id}"
        ),
        artifact_sha256=proposal.proposal_sha256,
        summary=(EvidenceField("fluorophore", fluorophore),),
        suggested_ids=proposal.measurement_ids,
    )


def bind_negative_fallback_review(
    proposal: NegativeFallbackReviewProposal,
    anatomy_source: ReferenceWorkflowCheckpoint,
    anatomy_result: ReferenceWorkflowCheckpoint,
    measurement_source: ReferenceWorkflowCheckpoint,
    measurement_result: ReferenceWorkflowCheckpoint,
) -> NegativeFallbackReviewEvidence:
    """Bind separate anatomy and signal-negativity decisions."""

    if type(proposal) is not NegativeFallbackReviewProposal:
        raise TypeError("proposal must be an exact NegativeFallbackReviewProposal")
    proposal = NegativeFallbackReviewProposal(
        proposal.positive, proposal.negative, proposal.background, proposal.policy
    )
    fluorophore = proposal.positive.input_evidence.fluorophore_identity
    assert fluorophore is not None
    anatomy_decision = _require_accepted_review_decision(
        anatomy_source,
        anatomy_result,
        checkpoint=ReferenceCheckpointKind.NEGATIVE_REFERENCE_ROIS,
        option_value="accept_negative_reference_rois",
        fluorophore=fluorophore,
    )
    if len(anatomy_decision.response.artifacts) != 1:
        raise ValueError("anatomy review requires one exact negative-ROI artifact")
    anatomy_artifact = anatomy_decision.response.artifacts[0]
    if (
        anatomy_artifact.role != "negative_reference_rois"
        or anatomy_artifact.subject_id != fluorophore
        or anatomy_artifact.artifact_sha256 != proposal.negative.selection.export.content_sha256
        or anatomy_decision.response.selections
    ):
        raise ValueError("anatomy review ROI artifact or inventory differs")
    measurement_decision = _require_accepted_review_decision(
        measurement_source,
        measurement_result,
        checkpoint=ReferenceCheckpointKind.SIGNAL_DARK_MEASUREMENTS,
        option_value="accept_signal_dark_measurements",
        fluorophore=fluorophore,
    )
    if (
        anatomy_source.run_id != measurement_source.run_id
        or anatomy_source.target_image_sha256 != measurement_source.target_image_sha256
        or anatomy_source.fluorophores != measurement_source.fluorophores
        or anatomy_result.decisions != measurement_source.decisions[: len(anatomy_result.decisions)]
        or anatomy_result.transition_log
        != measurement_source.transition_log[: len(anatomy_result.transition_log)]
    ):
        raise ValueError("negative reviews do not share the same workflow lineage")
    expected_evidence = negative_fallback_review_request_evidence(proposal)
    if tuple(
        item
        for item in measurement_decision.request.evidence
        if item.kind == expected_evidence.kind and item.artifact_sha256 == proposal.proposal_sha256
    ) != (expected_evidence,):
        raise ValueError("signal review lacks exact negative-fallback proposal evidence")
    if (
        anatomy_source.measurement_ids != proposal.measurement_ids
        or measurement_source.measurement_ids != proposal.measurement_ids
    ):
        raise ValueError("signal review measurement axis differs from the proposal")
    selections = measurement_decision.response.selections
    signal = tuple(item for item in selections if item.role == "signal_measurement")
    dark = tuple(item for item in selections if item.role == "dark_measurement")
    axis_index = {item: index for index, item in enumerate(proposal.measurement_ids)}
    canonical = (*signal, *sorted(dark, key=lambda item: axis_index[item.identifier]))
    if len(signal) != 1 or selections != canonical:
        raise ValueError("signal/dark measurement inventory is not in canonical order")
    signal_id = signal[0].identifier
    ratio = proposal.per_measurement_negativity_ratios[axis_index[signal_id]]
    return NegativeFallbackReviewEvidence(
        negative_roi_id=proposal.negative.selection.roi.roi_id,
        positive_roi_id=proposal.positive.selection.roi.roi_id,
        signal_measurement_ids=(signal_id,),
        fluorophore_identity=fluorophore,
        positive_input_id=proposal.positive.input_evidence.input_id,
        control_input_id=proposal.negative.input_evidence.input_id,
        positive_sampling_evidence_sha256=(proposal.positive.sampling.sampling_evidence_sha256),
        control_sampling_evidence_sha256=(proposal.negative.sampling.sampling_evidence_sha256),
        positive_source_image_sha256=proposal.positive.input_evidence.source_image_sha256,
        control_source_image_sha256=proposal.negative.input_evidence.source_image_sha256,
        fluorophore_negativity_ratio=ratio,
        negativity_scale_floor=proposal.policy.negativity_scale_floor,
        anatomically_matched=True,
        negativity_authority_sha256=typed_checksum_sha256(
            {
                "semantic": "negative_fallback_negativity_review_v1",
                "proposal_sha256": proposal.proposal_sha256,
                "decision_sha256": measurement_decision.content_sha256,
                "signal_measurement_id": signal_id,
            }
        ),
        anatomical_match_authority_sha256=typed_checksum_sha256(
            {
                "semantic": "negative_fallback_anatomy_review_v1",
                "proposal_sha256": proposal.proposal_sha256,
                "decision_sha256": anatomy_decision.content_sha256,
            }
        ),
        accepted=True,
    )


def _consensus_inclusion_payload(
    purifications: tuple[PurifiedReferenceRoi, ...],
) -> tuple[str, tuple[str, ...], str]:
    items = _typed_tuple(purifications, PurifiedReferenceRoi, "consensus review purifications")
    for item in items:
        rebuilt = PurifiedReferenceRoi(
            positive=item.positive,
            control=item.control,
            control_set=item.control_set,
            control_signal_dark_reviews=item.control_signal_dark_reviews,
            background_resolution=item.background_resolution,
            match_review=item.match_review,
            negative_fallback_review=item.negative_fallback_review,
            signal_dark_review=item.signal_dark_review,
            dark_measurements=item.dark_measurements,
            policy=item.policy,
            measurement_ids=item.measurement_ids,
            measured_positive=item.measured_positive,
            measured_control=item.measured_control,
            background_corrected_positive=item.background_corrected_positive,
            background_corrected_control=item.background_corrected_control,
            af_subtraction_factor=item.af_subtraction_factor,
            raw_residual=item.raw_residual,
            negative_residual_clamps=item.negative_residual_clamps,
            dark_band_sensitivity=item.dark_band_sensitivity,
            purified_spectrum=item.purified_spectrum,
            disposition=item.disposition,
            reason_codes=item.reason_codes,
        )
        if rebuilt != item:
            raise ValueError("consensus purification authority is stale")
    fluorophores = tuple(item.positive.input_evidence.fluorophore_identity for item in items)
    if any(item is None for item in fluorophores) or len(set(fluorophores)) != 1:
        raise ValueError("consensus review purifications must bind one fluorophore")
    measurement_axes = tuple(item.measurement_ids for item in items)
    if len(set(measurement_axes)) != 1:
        raise ValueError("consensus review purification measurement axes differ")
    if any(item.disposition != "pass" or item.purified_spectrum is None for item in items):
        raise ValueError("consensus review requires passing purified ROI evidence")
    roi_ids = tuple(item.positive.selection.roi.roi_id for item in items)
    if len(roi_ids) != len(set(roi_ids)):
        raise ValueError("consensus review purification ROI identities must be unique")
    content_sha256 = typed_checksum_sha256(
        {
            "semantic": "consensus_inclusion_review_inventory_v1",
            "fluorophore": fluorophores[0],
            "purification_evidence_sha256s": [item.evidence_sha256 for item in items],
            "roi_ids": list(roi_ids),
        }
    )
    fluorophore = fluorophores[0]
    assert fluorophore is not None
    return fluorophore, roi_ids, content_sha256


def consensus_inclusion_request_evidence(
    purifications: tuple[PurifiedReferenceRoi, ...],
) -> ReferenceEvidence:
    """Expose the exact ordered purified-ROI inventory before inclusion review."""

    fluorophore, roi_ids, content_sha256 = _consensus_inclusion_payload(purifications)
    return ReferenceEvidence(
        evidence_id=typed_checksum_sha256(
            {
                "kind": "consensus_inclusion_review",
                "content_sha256": content_sha256,
            }
        ),
        kind="consensus_inclusion_review",
        artifact_id=f"consensus-inclusion:{fluorophore}",
        artifact_sha256=content_sha256,
        summary=(EvidenceField("fluorophore", fluorophore),),
        suggested_ids=roi_ids,
        ambiguous=False,
    )


def extraction_request_evidence(
    purifications: tuple[PurifiedReferenceRoi, ...],
) -> tuple[ReferenceEvidence, ...]:
    """Expose failed extraction for revision without admitting it to consensus."""
    items = _typed_tuple(purifications, PurifiedReferenceRoi, "extraction purifications")
    if not items:
        raise ValueError("extraction review requires its measured ROI inventory")
    failed = tuple(item for item in items if item.disposition != "pass")
    if not failed:
        return (consensus_inclusion_request_evidence(items),)
    for item in items:
        if replace(item) != item:
            raise ValueError("extraction purification authority is stale")
    def reason_text(item: PurifiedReferenceRoi, reason: str) -> str:
        messages = {
            "background_corrected_af_near_zero":
                "The AF region has too little signal after background subtraction to estimate its spectrum. Review the AF and background regions.",
            "dark_band_denominator_below_tolerance":
                "The selected dark bands contain too little AF signal to estimate its contribution. Review the AF region and dark bands.",
            "purified_spectrum_near_zero":
                "Too little fluorophore signal remains after AF subtraction. Review the stained regions and control pairing.",
        }
        if reason in messages:
            return messages[reason]
        if "nonfinite" in reason:
            return "The extraction arithmetic produced a nonfinite value. Inspect the recorded image values and calculation settings before retrying."
        return "The extracted spectrum failed its recorded quality check. Inspect the recorded extraction details before continuing."
    return tuple(
        ReferenceEvidence(
            evidence_id=typed_checksum_sha256({"kind": "reference_extraction_failure",
                                               "purification_sha256": item.evidence_sha256}),
            kind="reference_extraction_failure",
            artifact_id="purification:" + item.positive.selection.roi.roi_id,
            artifact_sha256=item.evidence_sha256,
            summary=(
                EvidenceField("roi_id", item.positive.selection.roi.roi_id),
                EvidenceField("reason", " ".join(dict.fromkeys(reason_text(item, reason) for reason in item.reason_codes))),
                EvidenceField("reason_codes", "; ".join(item.reason_codes)),
            ),
        ) for item in failed
    )


def bind_consensus_inclusion_decisions(
    purifications: tuple[PurifiedReferenceRoi, ...],
    source: ReferenceWorkflowCheckpoint,
    result: ReferenceWorkflowCheckpoint,
) -> tuple[ConsensusInclusionDecision, ...]:
    """Bind a complete ordered per-ROI inclusion inventory to extraction review."""

    fluorophore, roi_ids, inventory_sha256 = _consensus_inclusion_payload(purifications)
    decision = _require_accepted_review_decision(
        source,
        result,
        checkpoint=ReferenceCheckpointKind.EXTRACTION_QC,
        option_value="accept_extraction_qc",
        fluorophore=fluorophore,
    )
    if source.measurement_ids != purifications[0].measurement_ids:
        raise ValueError("consensus workflow measurement axis differs")
    expected_evidence = consensus_inclusion_request_evidence(purifications)
    if tuple(
        item
        for item in decision.request.evidence
        if item.kind == expected_evidence.kind and item.artifact_sha256 == inventory_sha256
    ) != (expected_evidence,):
        raise ValueError("extraction review lacks exact consensus inclusion evidence")
    if len(decision.response.artifacts) != 1:
        raise ValueError("consensus inclusion review requires one extraction-QC artifact")
    artifact = decision.response.artifacts[0]
    if (
        artifact.role != "extraction_qc"
        or artifact.subject_id != fluorophore
        or artifact.artifact_sha256 != inventory_sha256
    ):
        raise ValueError("consensus inclusion extraction artifact differs")
    selections = decision.response.selections
    actual_ids = tuple(item.identifier for item in selections)
    allowed_roles = {"consensus_included_roi", "consensus_excluded_roi"}
    if (
        len(selections) != len(roi_ids)
        or actual_ids != roi_ids
        or len(actual_ids) != len(set(actual_ids))
        or any(item.role not in allowed_roles for item in selections)
    ):
        raise ValueError("consensus inclusion ROI inventory is missing, extra, or reordered")
    results: list[ConsensusInclusionDecision] = []
    for index, (purification, selection) in enumerate(zip(purifications, selections, strict=True)):
        included = selection.role == "consensus_included_roi"
        results.append(
            ConsensusInclusionDecision(
                roi_id=selection.identifier,
                purification_evidence_sha256=purification.evidence_sha256,
                included=included,
                reason=(
                    "explicit_reviewer_inclusion" if included else "explicit_reviewer_exclusion"
                ),
                review_binding_sha256=typed_checksum_sha256(
                    {
                        "semantic": "consensus_inclusion_review_binding_v1",
                        "decision_sha256": decision.content_sha256,
                        "inventory_sha256": inventory_sha256,
                        "index": index,
                        "roi_id": selection.identifier,
                        "purification_evidence_sha256": purification.evidence_sha256,
                        "included": included,
                    }
                ),
                accepted=True,
            )
        )
    return tuple(results)


__all__ = (
    "MatchedControlReviewProposal",
    "NegativeFallbackReviewPolicy",
    "NegativeFallbackReviewProposal",
    "bind_consensus_inclusion_decisions",
    "bind_matched_control_review",
    "bind_negative_fallback_review",
    "bind_signal_dark_measurement_review",
    "consensus_inclusion_request_evidence",
    "extraction_request_evidence",
    "matched_control_review_request_evidence",
    "negative_fallback_review_request_evidence",
    "propose_matched_control_review",
    "propose_negative_fallback_review",
)
