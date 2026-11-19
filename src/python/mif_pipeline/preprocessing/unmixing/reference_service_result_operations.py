"""Recoverable extraction and consensus operations for the reference service.

The functions retain the selected spectral basis, component order and reviewed acquisition
facts through the operation. Numerical work is bounded by the configured pixel blocks, while
published references and results keep their input and settings identities."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from .interactions import (
    ArtifactPointer,
    PublishedArtifactEvidence,
    ReferenceActionCode,
    SelectedIdentifier,
    ValidatedArtifactEvidence,
    configured_response,
)
from .purification import (
    ConsensusInclusionDecision,
    FluorophoreConsensusCandidate,
    PurifiedReferenceRoi,
)
from .reference_qc import SpectrumMatrixQcPolicy
from .reference_review_bindings import (
    bind_consensus_inclusion_decisions,
    consensus_inclusion_request_evidence,
)
from .reference_service_artifacts import (
    ReferenceArtifactRepositoryError,
    _ReferenceArtifactRepository,
)
from .reference_service_composition import (
    ReferenceLibraryPublicationMetadata,
    compose_accepted_library,
    compose_accepted_spectrum,
)
from .reference_service_operation_bindings import (
    _consensus_operation_binding,
    _extraction_operation_binding,
    _reconstruct_qc_policy,
)
from .reference_service_records import (
    ReferenceServiceAdvance,
    ReferenceServiceAdvanceStatus,
    ReferenceServiceSnapshot,
)
from .reference_service_storage import (
    ReferenceCheckpointConflictError,
    ReferenceCheckpointStore,
)
from .reference_workflow import (
    ReferenceDecisionRecord,
    ReferenceTransitionKind,
    ReferenceWorkflowCheckpoint,
    ReferenceWorkflowStatus,
    advance_reference_workflow,
    propose_reference_artifacts,
    publish_reference_artifacts,
)
from .typed_checksum import typed_checksum_sha256

# ==============================================================================
#                      Reference service result operations
# ==============================================================================


def reviewed_consensus_inclusions(decisions, roi_review, *, checkpoint_sha256, reviewer_id):
    """Bind the explicit consensus-page choices to the accepted extraction lineage."""
    if type(roi_review) is not dict or set(roi_review) != {item.roi_id for item in decisions}:
        raise ValueError("Review every extracted region exactly once")
    if any(type(value) is not str or value not in {"include", "exclude", "retain"} for value in roi_review.values()):
        raise ValueError("Region choices must include, exclude, or explicitly retain a difference")
    if not any(value != "exclude" for value in roi_review.values()):
        raise ValueError("Include at least one region, or reselect the reference ROIs")
    binding = typed_checksum_sha256({
        "checkpoint": checkpoint_sha256, "reviewer": reviewer_id,
        "extraction_decisions": [item.content_sha256 for item in decisions],
        "region_choices": [[item.roi_id, roi_review[item.roi_id]] for item in decisions],
    })
    reasons = {"include": "Included after comparison with the combined reference",
               "exclude": "Excluded after comparison with the combined reference",
               "retain": "Retained after explicitly reviewing the spectral difference"}
    return tuple(ConsensusInclusionDecision(
        item.roi_id, item.purification_evidence_sha256,
        roi_review[item.roi_id] != "exclude", reasons[roi_review[item.roi_id]],
        binding, True,
    ) for item in decisions)


def validate_consensus_retention(candidate, roi_review):
    flagged = tuple(item.roi_id for item in candidate.roi_qc
                    if item.included and item.outlier_review_required)
    if flagged and (roi_review is None or any(roi_review.get(roi) != "retain" for roi in flagged)):
        raise ValueError("Review the differing regions: explicitly retain or exclude " + ", ".join(flagged))


class _ReferenceResultOperations:
    """Own checkpoint persistence without accepting caller-authored evidence."""

    def __init__(self, store: ReferenceCheckpointStore) -> None:
        if type(store) is not ReferenceCheckpointStore:
            raise TypeError("reference workflow service requires its checkpoint store")
        self._store = store
        self._artifacts = _ReferenceArtifactRepository(store.output_root, store._output_identity)

    @staticmethod
    def _validated_artifact(
        pointer: ArtifactPointer,
        artifact_type: str,
        content_sha256: str,
    ) -> ValidatedArtifactEvidence:
        return ValidatedArtifactEvidence(
            pointer=pointer,
            artifact_type=artifact_type,
            validator_id="mif_pipeline.reference_artifact_repository/v1",
            validation_evidence_sha256=typed_checksum_sha256(
                {
                    "semantic": "strict_reference_artifact_validation_v1",
                    "pointer": pointer.to_mapping(),
                    "repository_content_sha256": content_sha256,
                }
            ),
        )

    @staticmethod
    def _published_artifact(
        pointer: ArtifactPointer,
        artifact_type: str,
        pending,
        content_sha256: str,
    ) -> PublishedArtifactEvidence:
        return PublishedArtifactEvidence(
            pointer=pointer,
            artifact_type=artifact_type,
            publisher_id="mif_pipeline.reference_artifact_repository/v1",
            validator_id="mif_pipeline.reference_artifact_repository/v1",
            publication_evidence_sha256=typed_checksum_sha256(
                {
                    "semantic": "accepted_reference_artifact_publication_v1",
                    "pointer": pointer.to_mapping(),
                    "repository_content_sha256": content_sha256,
                    "review_request_id": pending.review_request_id,
                    "proposal_artifacts": [
                        item.to_mapping() for item in pending.proposal_artifacts
                    ],
                }
            ),
            review_request_id=pending.review_request_id,
            proposal_artifacts=pending.proposal_artifacts,
        )

    def accept_extraction(
        self,
        run_id: str,
        expected_content_sha256: str,
        purifications: tuple[PurifiedReferenceRoi, ...],
        dispositions: tuple[SelectedIdentifier, ...],
        *,
        reviewer_id: str,
    ) -> ReferenceServiceAdvance:
        """Persist exact purifications and bind a complete extraction review."""

        current, start_authority_sha256 = self._store._load_service_checkpoint(run_id)
        resumed_proposal = False
        if current.content_sha256 != expected_content_sha256:
            if (
                current.request is not None
                and current.request.checkpoint.value == "fluorophore_consensus"
            ):
                extraction_decision = self._current_extraction_decision(
                    current, current.current_fluorophore or ""
                )
                if expected_content_sha256 == self._decision_source_authority(extraction_decision):
                    stored = self._artifacts.load(
                        run_id=run_id,
                        start_authority_sha256=start_authority_sha256,
                        authority_checkpoint_sha256=current.content_sha256,
                        kind="consensus_inclusions",
                        subject_id=current.current_fluorophore or "",
                    )
                    retained_purifications = self._purifications_for_consensus(
                        run_id,
                        current,
                        start_authority_sha256,
                        current.current_fluorophore or "",
                    )
                    retained_decisions = stored.record
                    if (
                        retained_purifications == purifications
                        and tuple(
                            SelectedIdentifier(
                                (
                                    "consensus_included_roi"
                                    if item.included
                                    else "consensus_excluded_roi"
                                ),
                                item.roi_id,
                            )
                            for item in retained_decisions
                        )
                        == dispositions
                        and (extraction_decision.response.reviewer_id == reviewer_id)
                    ):
                        return ReferenceServiceAdvance(
                            ReferenceServiceAdvanceStatus.PERSISTED,
                            ReferenceServiceSnapshot(current, start_authority_sha256),
                        )
            if (
                current.status is ReferenceWorkflowStatus.AWAITING_RESPONSE
                and current.request is not None
                and current.request.checkpoint.value == "extraction_qc"
                and current.request.prior_checkpoint_sha256 == expected_content_sha256
                and current.request.proposed_artifacts
            ):
                resumed_proposal = True
            else:
                raise ReferenceCheckpointConflictError(
                    "service extraction checkpoint expectation is stale"
                )
        if (
            current.status is not ReferenceWorkflowStatus.AWAITING_RESPONSE
            or current.request is None
            or current.request.checkpoint.value != "extraction_qc"
            or current.current_fluorophore is None
        ):
            raise ValueError("current workflow is not awaiting extraction review")
        evidence = consensus_inclusion_request_evidence(purifications)
        if current.request.evidence != (evidence,):
            if resumed_proposal:
                raise ReferenceCheckpointConflictError(
                    "extraction retry purified-ROI inventory differs"
                )
            raise ValueError("extraction source lacks the exact purified-ROI inventory")
        if resumed_proposal:
            stored_purifications = self._artifacts.load(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                authority_checkpoint_sha256=expected_content_sha256,
                kind="purifications",
                subject_id=current.current_fluorophore,
            )
            if stored_purifications.record != purifications:
                raise ReferenceCheckpointConflictError("extraction retry scientific inputs differ")
        else:
            stored_purifications = self._artifacts.put(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                authority_checkpoint_sha256=current.content_sha256,
                kind="purifications",
                subject_id=current.current_fluorophore,
                record=purifications,
            )
        pointer = ArtifactPointer(
            role="extraction_qc",
            artifact_id=stored_purifications.content_sha256,
            artifact_sha256=evidence.artifact_sha256,
            subject_id=current.current_fluorophore,
        )
        proposed = (
            current
            if resumed_proposal
            else propose_reference_artifacts(
                current,
                (
                    self._validated_artifact(
                        pointer, "qc_evidence", stored_purifications.content_sha256
                    ),
                ),
            )
        )
        if tuple(
            item.pointer for item in proposed.request.proposed_artifacts  # type: ignore[union-attr]
        ) != (pointer,):
            raise ReferenceCheckpointConflictError("extraction retry artifact proposal differs")
        assert proposed.request is not None
        response = configured_response(
            proposed.request,
            option_value="accept_extraction_qc",
            reviewer_id=reviewer_id,
            artifacts=(pointer,),
            selections=dispositions,
        )
        result = advance_reference_workflow(proposed, response)
        decisions = bind_consensus_inclusion_decisions(purifications, proposed, result)
        try:
            self._artifacts.put_unique(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                authority_checkpoint_sha256=expected_content_sha256,
                kind="extraction_operation",
                subject_id=current.current_fluorophore,
                record=_extraction_operation_binding(
                    source_checkpoint_sha256=expected_content_sha256,
                    inventory_sha256=evidence.artifact_sha256,
                    dispositions=dispositions,
                    reviewer_id=reviewer_id,
                ),
            )
        except ReferenceArtifactRepositoryError as exc:
            raise ReferenceCheckpointConflictError(
                "extraction retry operation inputs differ"
            ) from exc
        self._artifacts.put(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
            authority_checkpoint_sha256=result.content_sha256,
            kind="consensus_inclusions",
            subject_id=current.current_fluorophore,
            record=decisions,
        )
        if resumed_proposal:
            persisted_proposal = proposed
        else:
            persisted_proposal, _ = self._store._compare_and_swap_service(
                current.content_sha256, proposed, start_authority_sha256
            )
        persisted, persisted_start = self._store._compare_and_swap_service(
            persisted_proposal.content_sha256, result, start_authority_sha256
        )
        return ReferenceServiceAdvance(
            ReferenceServiceAdvanceStatus.PERSISTED,
            ReferenceServiceSnapshot(persisted, persisted_start),
        )

    @staticmethod
    def _decision_source_authority(decision: ReferenceDecisionRecord) -> str:
        """Return the exact pre-proposal checkpoint bound by a review decision."""

        if type(decision) is not ReferenceDecisionRecord:
            raise TypeError("decision source authority requires an exact decision")
        source = decision.request.prior_checkpoint_sha256
        if source is None:
            raise ValueError("review decision lacks exact original source authority")
        return source

    @staticmethod
    def _current_extraction_decision(
        current: ReferenceWorkflowCheckpoint,
        fluorophore: str,
    ) -> ReferenceDecisionRecord:
        superseded = {
            request_id
            for transition in current.transition_log
            if transition.superseded_request_ids
            for request_id in transition.superseded_request_ids
        }
        reselections = tuple(
            index
            for index, item in enumerate(current.decisions)
            if item.request.fluorophore == fluorophore
            and item.response.action is ReferenceActionCode.RESELECT
        )
        lineage_start = 0 if not reselections else reselections[-1] + 1
        matches = tuple(
            item
            for item in current.decisions[lineage_start:]
            if item.request.checkpoint.value == "extraction_qc"
            and item.request.fluorophore == fluorophore
            and item.response.option_value == "accept_extraction_qc"
            and item.response.action is ReferenceActionCode.ACCEPT
            and item.request.request_id not in superseded
        )
        if len(matches) != 1:
            raise ValueError("consensus checkpoint lacks exact extraction lineage")
        return matches[0]

    def _purifications_for_consensus(
        self,
        run_id: str,
        current: ReferenceWorkflowCheckpoint,
        start_authority_sha256: str,
        fluorophore: str,
    ) -> tuple[PurifiedReferenceRoi, ...]:
        if not current.decisions:
            raise ValueError("consensus checkpoint lacks extraction decision")
        decision = self._current_extraction_decision(current, fluorophore)
        if len(decision.response.artifacts) != 1:
            raise ValueError("consensus checkpoint lacks exact extraction lineage")
        pointer = decision.response.artifacts[0]
        stored = self._artifacts.load(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
            authority_checkpoint_sha256=self._decision_source_authority(decision),
            kind="purifications",
            subject_id=decision.request.fluorophore or "",
        )
        if (
            pointer.artifact_id != stored.content_sha256
            or pointer.artifact_sha256 != stored.semantic_sha256
        ):
            raise ValueError("stored purifications differ from extraction authority")
        return stored.record  # type: ignore[return-value]

    def _reviewed_extraction_for_consensus(
        self,
        run_id: str,
        current: ReferenceWorkflowCheckpoint,
        start_authority_sha256: str,
        fluorophore: str,
    ) -> tuple[tuple[PurifiedReferenceRoi, ...], tuple[ConsensusInclusionDecision, ...]]:
        """Load and bind one saved extraction once for this operation."""
        purifications = self._purifications_for_consensus(
            run_id, current, start_authority_sha256, fluorophore
        )
        decision = self._current_extraction_decision(current, fluorophore)
        source, result, loaded_start = self._store._load_service_decision_authority(
            run_id,
            source_checkpoint_sha256=decision.source_checkpoint_sha256,
            decision_record_sha256=decision.content_sha256,
        )
        if loaded_start != start_authority_sha256:
            raise ValueError("extraction decision start authority differs")
        derived = bind_consensus_inclusion_decisions(purifications, source, result)
        stored = self._artifacts.load(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
            authority_checkpoint_sha256=result.content_sha256,
            kind="consensus_inclusions",
            subject_id=fluorophore,
        ).record
        if stored != derived:
            raise ValueError("stored consensus inclusions differ from accepted extraction response")
        return purifications, derived

    def _derived_inclusion_decisions(
        self,
        run_id: str,
        current: ReferenceWorkflowCheckpoint,
        start_authority_sha256: str,
        fluorophore: str,
    ) -> tuple[ConsensusInclusionDecision, ...]:
        return self._reviewed_extraction_for_consensus(
            run_id, current, start_authority_sha256, fluorophore
        )[1]

    @staticmethod
    def _reviewed_consensus_decision(
        current: ReferenceWorkflowCheckpoint,
        fluorophore: str,
    ) -> ReferenceDecisionRecord:
        superseded = {
            request_id
            for transition in current.transition_log
            if transition.superseded_request_ids
            for request_id in transition.superseded_request_ids
        }
        matches = tuple(
            item
            for item in current.decisions
            if item.request.checkpoint.value == "fluorophore_consensus"
            and item.request.fluorophore == fluorophore
            and item.response.action is ReferenceActionCode.ACCEPT
            and item.response.option_value == "accept_fluorophore_consensus"
            and item.request.request_id not in superseded
        )
        if len(matches) != 1 or len(matches[0].response.artifacts) != 1:
            raise ValueError("prior accepted spectrum lacks one exact reviewed consensus decision")
        decision = matches[0]
        pointer = decision.response.artifacts[0]
        if (
            pointer.role != "proposed_fluorophore_consensus_qc"
            or pointer.subject_id != fluorophore
            or tuple(item.pointer for item in decision.request.proposed_artifacts) != (pointer,)
        ):
            raise ValueError("prior accepted spectrum lacks exact reviewed proposal authority")
        return decision

    @classmethod
    def _reviewed_consensus_pointer(
        cls,
        current: ReferenceWorkflowCheckpoint,
        fluorophore: str,
    ) -> ArtifactPointer:
        return cls._reviewed_consensus_decision(current, fluorophore).response.artifacts[0]

    @classmethod
    def _reviewed_consensus_required_library(
        cls,
        current: ReferenceWorkflowCheckpoint,
        fluorophore: str,
    ) -> bool:
        decision = cls._reviewed_consensus_decision(current, fluorophore)
        publications = tuple(
            item
            for item in current.transition_log
            if item.kind is ReferenceTransitionKind.PUBLICATION
            and item.request_id == decision.request.request_id
            and item.affected_fluorophore == fluorophore
        )
        if len(publications) != 1:
            raise ValueError("prior accepted spectrum lacks exact publication lineage")
        publication = publications[0]
        if any(
            item.review_request_id != decision.request.request_id
            or item.proposal_artifacts != decision.response.artifacts
            for item in publication.publication_evidence
        ):
            raise ValueError("prior accepted spectrum publication differs from reviewed proposal")
        roles = tuple(item.role for item in publication.artifact_bindings)
        if roles == ("accepted_fluorophore_spectrum",):
            return False
        if roles == (
            "accepted_fluorophore_spectrum",
            "accepted_fluorophore_library",
        ):
            return True
        raise ValueError("prior accepted spectrum has invalid publication artifact roles")

    def extraction_inclusion_decisions(self, run_id: str) -> tuple[ConsensusInclusionDecision, ...]:
        """Reopen exact decision-bound inclusions for consensus composition."""

        current, start_authority_sha256 = self._store._load_service_checkpoint(run_id)
        if (
            current.request is None
            or current.request.checkpoint.value != "fluorophore_consensus"
            or current.current_fluorophore is None
        ):
            raise ValueError("workflow is not awaiting fluorophore consensus")
        return self._derived_inclusion_decisions(
            run_id,
            current,
            start_authority_sha256,
            current.current_fluorophore,
        )

    def accept_consensus(
        self,
        run_id: str,
        expected_content_sha256: str,
        candidate: FluorophoreConsensusCandidate,
        qc_policy: SpectrumMatrixQcPolicy,
        *,
        reviewer_id: str,
        reviewed_at: str,
        publication: ReferenceLibraryPublicationMetadata | None = None,
        roi_review: dict[str, str] | None = None,
    ) -> ReferenceServiceAdvance:
        """Persist, review, and publish one exact accepted consensus result."""

        current, start_authority_sha256 = self._store._load_service_checkpoint(run_id)
        qc_policy = _reconstruct_qc_policy(qc_policy)
        fluorophore = candidate.fluorophore_identity
        phase = "source"
        if current.content_sha256 != expected_content_sha256:
            if (
                current.status is ReferenceWorkflowStatus.AWAITING_RESPONSE
                and current.request is not None
                and current.request.checkpoint.value == "fluorophore_consensus"
                and current.request.prior_checkpoint_sha256 == expected_content_sha256
                and current.request.proposed_artifacts
            ):
                phase = "proposal"
            elif (
                current.status is ReferenceWorkflowStatus.AWAITING_PUBLICATION
                and current.pending_publication is not None
                and current.pending_publication.publication_kind == "fluorophore_consensus"
            ):
                decision = self._reviewed_consensus_decision(current, fluorophore)
                if (
                    expected_content_sha256 != self._decision_source_authority(decision)
                    or current.pending_publication.review_request_id != decision.request.request_id
                    or current.pending_publication.proposal_artifacts != decision.response.artifacts
                ):
                    raise ReferenceCheckpointConflictError(
                        "service consensus checkpoint expectation is stale"
                    )
                phase = "pending"
            elif any(
                item.fluorophore == fluorophore and item.accepted_spectrum is not None
                for item in current.fluorophore_artifacts
            ):
                decision = self._reviewed_consensus_decision(current, fluorophore)
                if expected_content_sha256 != self._decision_source_authority(decision):
                    raise ReferenceCheckpointConflictError(
                        "service consensus checkpoint expectation is stale"
                    )
                phase = "published"
            else:
                raise ReferenceCheckpointConflictError(
                    "service consensus checkpoint expectation is stale"
                )
        if phase != "published" and (
            current.current_fluorophore != fluorophore
            or (
                phase in {"source", "proposal"}
                and (
                    current.status is not ReferenceWorkflowStatus.AWAITING_RESPONSE
                    or current.request is None
                    or current.request.checkpoint.value != "fluorophore_consensus"
                )
            )
        ):
            raise ValueError("current workflow is not awaiting consensus review")
        purifications, decisions = self._reviewed_extraction_for_consensus(
            run_id, current, start_authority_sha256, fluorophore
        )
        if roi_review is not None:
            decisions = reviewed_consensus_inclusions(
                decisions, roi_review, checkpoint_sha256=expected_content_sha256,
                reviewer_id=reviewer_id,
            )
        validate_consensus_retention(candidate, roi_review)
        rebuilt = type(candidate)(
            component_identity=candidate.component_identity,
            fluorophore_identity=candidate.fluorophore_identity,
            policy=candidate.policy,
            measurement_ids=candidate.measurement_ids,
            values=candidate.values,
            roi_purifications=purifications,
            inclusion_decisions=decisions,
            roi_qc=candidate.roi_qc,
            disposition=candidate.disposition,
            reason_codes=candidate.reason_codes,
        )
        if rebuilt != candidate:
            raise ValueError("consensus candidate differs from exact extraction authority")
        if (
            type(reviewed_at) is not str
            or not reviewed_at
            or (publication is not None and publication.reviewed_at != reviewed_at)
        ):
            raise ValueError("reviewed_at must bind exact publication metadata")
        spectrum = compose_accepted_spectrum(
            candidate,
            reviewer_id=reviewer_id,
            reviewed_at=reviewed_at,
        )
        last = (
            self._reviewed_consensus_required_library(current, fluorophore)
            if phase == "published"
            else all(
                item.accepted_spectrum is not None or item.fluorophore == fluorophore
                for item in current.fluorophore_artifacts
            )
        )
        if last and publication is None:
            raise ValueError("final consensus requires explicit library publication metadata")
        if not last and publication is not None:
            raise ValueError("library metadata is accepted only for the final fluorophore")
        library = None
        if last:
            assert publication is not None
            spectra = []
            candidates = []
            for slot in current.fluorophore_artifacts:
                if slot.fluorophore == fluorophore:
                    spectra.append(spectrum)
                    candidates.append(candidate)
                    continue
                if slot.accepted_spectrum is None:
                    raise ValueError("final library lacks a prior accepted spectrum")
                prior_spectrum = self._artifacts.load_pointer(
                    run_id=run_id,
                    start_authority_sha256=start_authority_sha256,
                    kind="accepted_spectrum",
                    subject_id=slot.fluorophore,
                    pointer=slot.accepted_spectrum,
                ).record
                reviewed_pointer = self._reviewed_consensus_pointer(current, slot.fluorophore)
                prior_candidate = self._artifacts.load_pointer(
                    run_id=run_id,
                    start_authority_sha256=start_authority_sha256,
                    kind="consensus_candidate",
                    subject_id=slot.fluorophore,
                    pointer=reviewed_pointer,
                ).record
                projected = compose_accepted_spectrum(
                    prior_candidate,
                    reviewer_id=prior_spectrum.review.reviewer_id or "",
                    reviewed_at=prior_spectrum.review.reviewed_at,
                )
                if projected != prior_spectrum:
                    raise ValueError(
                        "prior accepted spectrum differs from reviewed consensus authority"
                    )
                spectra.append(prior_spectrum)
                candidates.append(prior_candidate)
            from .reference_catalogue import compose_shared_reference_catalogue
            try:
                scopes = self._artifacts.load_all(run_id=run_id, start_authority_sha256=start_authority_sha256,
                    kind="shared_construction_scope", subject_id="shared-construction")
            except ReferenceArtifactRepositoryError as error:
                if str(error) != "reference scientific artifact is missing":
                    raise
                scopes = ()
            if not scopes:
                library = compose_accepted_library(tuple(spectra), tuple(candidates), qc_policy,
                                                   publication, reviewer_id=reviewer_id)
            else:
                targets = tuple(item.record for item in self._artifacts._load_all_canonical_inputs(
                    run_id=run_id, start_authority_sha256=start_authority_sha256)
                    if item.record.input_role == "target_image")
                if len(scopes) != 1 or len(targets) != 1 or scopes[0].record.child_target_sha256 != targets[0].authority_sha256:
                    raise ValueError("Shared reference construction has changed target authority")
                library = compose_shared_reference_catalogue(tuple(spectra), tuple(candidates), qc_policy,
                    publication, scopes[0].record.cycle_panels, reviewer_id=reviewer_id)
        try:
            self._artifacts.put_unique(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                authority_checkpoint_sha256=expected_content_sha256,
                kind="consensus_operation",
                subject_id=fluorophore,
                record=_consensus_operation_binding(
                    source_checkpoint_sha256=expected_content_sha256,
                    candidate_sha256=candidate.candidate_sha256,
                    qc_policy=qc_policy,
                    reviewer_id=reviewer_id,
                    reviewed_at=reviewed_at,
                    publication=publication,
                ),
            )
        except ReferenceArtifactRepositoryError as exc:
            raise ReferenceCheckpointConflictError(
                "consensus retry operation inputs differ"
            ) from exc
        stored_candidate = self._artifacts.put(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
            authority_checkpoint_sha256=expected_content_sha256,
            kind="consensus_candidate",
            subject_id=fluorophore,
            record=candidate,
        )
        stored_spectrum = self._artifacts.put(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
            authority_checkpoint_sha256=expected_content_sha256,
            kind="accepted_spectrum",
            subject_id=fluorophore,
            record=spectrum,
        )
        stored_library = None
        if library is not None:
            stored_library = self._artifacts.put(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                authority_checkpoint_sha256=expected_content_sha256,
                kind="accepted_library",
                subject_id="library",
                record=library,
            )
        proposal_pointer = ArtifactPointer(
            "proposed_fluorophore_consensus_qc",
            stored_candidate.content_sha256,
            candidate.candidate_sha256,
            fluorophore,
        )
        if phase == "source":
            proposed = propose_reference_artifacts(
                current,
                (
                    self._validated_artifact(
                        proposal_pointer,
                        "qc_evidence",
                        stored_candidate.content_sha256,
                    ),
                ),
            )
        elif phase == "proposal":
            proposed = current
        else:
            proposed = None
        if proposed is not None and tuple(
            item.pointer for item in proposed.request.proposed_artifacts  # type: ignore[union-attr]
        ) != (proposal_pointer,):
            raise ReferenceCheckpointConflictError("consensus retry artifact proposal differs")
        if phase in {"source", "proposal"}:
            assert proposed is not None and proposed.request is not None
            response = configured_response(
                proposed.request,
                option_value="accept_fluorophore_consensus",
                reviewer_id=reviewer_id,
                artifacts=(proposal_pointer,),
            )
            pending_state = advance_reference_workflow(proposed, response)
        else:
            pending_state = current
        pending = pending_state.pending_publication
        if pending is None:
            if phase == "published":
                slots = tuple(
                    item
                    for item in current.fluorophore_artifacts
                    if item.fluorophore == fluorophore
                )
                if len(slots) != 1 or slots[0].accepted_spectrum is None:
                    raise ReferenceCheckpointConflictError(
                        "published retry lacks accepted spectrum authority"
                    )
                if (
                    self._artifacts.load_pointer(
                        run_id=run_id,
                        start_authority_sha256=start_authority_sha256,
                        kind="accepted_spectrum",
                        subject_id=fluorophore,
                        pointer=slots[0].accepted_spectrum,
                    ).record
                    != spectrum
                ):
                    raise ReferenceCheckpointConflictError("published retry spectrum differs")
                if last:
                    if (
                        library is None
                        or current.accepted_library is None
                        or self._artifacts.load_pointer(
                            run_id=run_id,
                            start_authority_sha256=start_authority_sha256,
                            kind="accepted_library",
                            subject_id="library",
                            pointer=current.accepted_library,
                        ).record
                        != library
                    ):
                        raise ReferenceCheckpointConflictError("published retry library differs")
                return ReferenceServiceAdvance(
                    ReferenceServiceAdvanceStatus.PERSISTED,
                    ReferenceServiceSnapshot(current, start_authority_sha256),
                )
            raise ValueError("consensus publication authority is missing")
        spectrum_pointer = ArtifactPointer(
            "accepted_fluorophore_spectrum",
            stored_spectrum.content_sha256,
            spectrum.spectrum_sha256,
            fluorophore,
        )
        publications = [
            self._published_artifact(
                spectrum_pointer,
                "extracted_fluorophore_spectrum",
                pending,
                stored_spectrum.content_sha256,
            )
        ]
        if stored_library is not None:
            publications.append(
                self._published_artifact(
                    ArtifactPointer(
                        "accepted_fluorophore_library",
                        stored_library.content_sha256,
                        library.content_sha256,  # type: ignore[union-attr]
                        None,
                    ),
                    "reviewed_fluorophore_library",
                    pending,
                    stored_library.content_sha256,
                )
            )
        result = publish_reference_artifacts(pending_state, tuple(publications))
        if phase == "source":
            assert proposed is not None
            persisted_proposal, _ = self._store._compare_and_swap_service(
                current.content_sha256, proposed, start_authority_sha256
            )
        elif phase == "proposal":
            persisted_proposal = current
        else:
            persisted_proposal = None
        if phase in {"source", "proposal"}:
            assert persisted_proposal is not None
            persisted_pending, _ = self._store._compare_and_swap_service(
                persisted_proposal.content_sha256,
                pending_state,
                start_authority_sha256,
            )
        else:
            persisted_pending = current
        persisted, persisted_start = self._store._compare_and_swap_service(
            persisted_pending.content_sha256, result, start_authority_sha256
        )
        return ReferenceServiceAdvance(
            ReferenceServiceAdvanceStatus.PERSISTED,
            ReferenceServiceSnapshot(persisted, persisted_start),
        )


__all__ = [
    "ReferenceLibraryPublicationMetadata",
    "ReferenceServiceAdvance",
    "ReferenceServiceAdvanceStatus",
    "ReferenceServiceSnapshot",
    "_ReferenceResultOperations",
]
