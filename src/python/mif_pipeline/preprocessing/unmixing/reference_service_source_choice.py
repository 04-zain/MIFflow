"""Recoverable service operations for basis-source and canonical-file choice.

The functions retain the selected spectral basis, component order and reviewed acquisition
facts through the operation. Numerical work is bounded by the configured pixel blocks, while
published references and results keep their input and settings identities."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import hashlib

from .canonical_reference_input import CanonicalReferenceInput
from .compatibility import (
    BasisSourceChoice,
    BasisSourceResolution,
    align_library_measurements,
    compare_quantitative_transformability,
    compare_structural_compatibility,
    resolve_basis_source,
    resolve_channel_fluor_identities,
)
from .interactions import ArtifactPointer, SelectedIdentifier, configured_response
from .reference_library import dumps_reference_artifact, loads_reference_artifact
from .reference_models import FluorophoreReferenceLibrary, ReviewRecord
from .reference_service_artifacts import (
    ReferenceArtifactRepositoryError,
    _ReferenceArtifactRepository,
)
from .reference_service_records import (
    ReferenceServiceAdvance,
    ReferenceServiceAdvanceStatus,
    ReferenceServiceSnapshot,
)
from .reference_service_result_operations import _ReferenceResultOperations
from .reference_service_source_records import (
    BasisSourceDecisionBinding,
    ReferenceControlFilesBinding,
)
from .reference_service_storage import (
    ReferenceCheckpointConflictError,
    ReferenceCheckpointStore,
)
from .reference_workflow import (
    ReferenceWorkflowCheckpoint,
    ReferenceWorkflowStatus,
    advance_reference_workflow,
    propose_reference_artifacts,
    publish_reference_artifacts,
)

# ==============================================================================
#                        Reference service source choice
# ==============================================================================


class _ReferenceSourceChoiceOperations:
    """Persist small replay bindings and derive all scientific authority internally."""

    def __init__(
        self,
        store: ReferenceCheckpointStore,
        artifacts: _ReferenceArtifactRepository,
    ) -> None:
        self._store = store
        self._artifacts = artifacts

    @staticmethod
    def _validate_target(
        current: ReferenceWorkflowCheckpoint,
        target: CanonicalReferenceInput,
    ) -> None:
        if (
            target.evidence.source_image_sha256 != current.target_image_sha256
            or target.evidence.axis.measurement_ids != current.measurement_ids
            or (target.fluorophore_names
                and target.fluorophore_names != current.fluorophores)
        ):
            raise ReferenceCheckpointConflictError(
                "canonical target authority differs from the live source request"
            )

    @staticmethod
    def _accepted_decision(
        current: ReferenceWorkflowCheckpoint,
        choice: BasisSourceChoice,
    ):
        matches = tuple(
            item
            for item in current.active_decisions
            if item.request.checkpoint.value == "basis_source"
            and item.response.option_value == choice.value
            and item.response.reviewer_id is not None
        )
        if len(matches) != 1:
            raise ReferenceCheckpointConflictError(
                "basis source retry lacks one exact accepted decision"
            )
        return matches[0]

    def _existing_binding(
        self,
        *,
        run_id: str,
        start_authority_sha256: str,
        source_checkpoint_sha256: str,
    ) -> BasisSourceDecisionBinding | None:
        try:
            values = self._artifacts.load_all(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                kind="basis_source_decision",
                subject_id="basis-source",
            )
        except ReferenceArtifactRepositoryError as exc:
            if "missing" in str(exc):
                return None
            raise
        matches = tuple(
            item.record
            for item in values
            if item.authority_checkpoint_sha256 == source_checkpoint_sha256
        )
        if len(matches) > 1:
            raise ReferenceCheckpointConflictError("basis source operation binding is ambiguous")
        return None if not matches else matches[0]  # type: ignore[return-value]

    def _persist_binding(
        self,
        *,
        run_id: str,
        start_authority_sha256: str,
        source: ReferenceWorkflowCheckpoint,
        choice: BasisSourceChoice,
        reviewer_id: str,
        target: CanonicalReferenceInput,
        imported_library_content_sha256: str | None = None,
        imported_library_bytes_sha256: str | None = None,
        validate_motif_before_persist: bool = False,
        prepublication_fence: Callable[[], object] | None = None,
    ) -> BasisSourceDecisionBinding:
        if source.request is None:
            raise ValueError("basis source binding requires the exact live request")
        existing = self._existing_binding(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
            source_checkpoint_sha256=source.content_sha256,
        )
        reviewed_at = (
            existing.reviewed_at
            if existing is not None
            else datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )
        binding = BasisSourceDecisionBinding(
            source_checkpoint_sha256=source.content_sha256,
            request_id=source.request.request_id,
            choice=choice,
            reviewer_id=reviewer_id,
            reviewed_at=reviewed_at,
            target_input_authority_sha256=target.authority_sha256,
            target_image_sha256=target.evidence.source_image_sha256,
            imported_library_content_sha256=imported_library_content_sha256,
            imported_library_bytes_sha256=imported_library_bytes_sha256,
        )
        if existing is not None and existing != binding:
            raise ReferenceCheckpointConflictError("basis source retry operation inputs differ")
        if validate_motif_before_persist:
            self._motif_resolution(current=source, target=target, binding=binding)
        try:
            if prepublication_fence is not None:
                prepublication_fence()
            stored = self._artifacts.put_unique(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                authority_checkpoint_sha256=source.content_sha256,
                kind="basis_source_decision",
                subject_id="basis-source",
                record=binding,
            )
        except ReferenceArtifactRepositoryError as exc:
            raise ReferenceCheckpointConflictError(
                "basis source retry operation inputs differ"
            ) from exc
        if type(stored.record) is not BasisSourceDecisionBinding:
            raise ReferenceCheckpointConflictError(
                "basis source operation binding has the wrong exact type"
            )
        return stored.record

    @staticmethod
    def _strict_imported_library(value: object) -> tuple[FluorophoreReferenceLibrary, bytes]:
        if type(value) is bytes:
            from .library_exchange import load_library_exchange

            loaded = load_library_exchange(value)
            raw = dumps_reference_artifact(loaded).encode("utf-8")
            if type(loaded) is not FluorophoreReferenceLibrary:
                raise ValueError("imported source must be a fluorophore reference library")
            canonical = dumps_reference_artifact(loaded).encode("utf-8")
            if canonical != raw:
                raise ValueError("imported library bytes must be exact canonical artifact bytes")
            return loaded, canonical
        if type(value) is FluorophoreReferenceLibrary:
            canonical = dumps_reference_artifact(value).encode("utf-8")
            loaded = loads_reference_artifact(canonical)
            if type(loaded) is not FluorophoreReferenceLibrary or loaded != value:
                raise ValueError("imported library object differs after strict round trip")
            return loaded, canonical
        raise TypeError("imported library must be exact canonical bytes or library object")

    @staticmethod
    def _validate_imported_library(
        current: ReferenceWorkflowCheckpoint,
        target: CanonicalReferenceInput,
        library: FluorophoreReferenceLibrary,
    ) -> None:
        from .curated_library import _spectral_label_order

        inventory = tuple(item.fluorophore_identity for item in library.spectra)
        _spectral_label_order(inventory, current.fluorophores, components=True)
        axis, signature, _ = align_library_measurements(library, target.evidence.axis)
        structural = compare_structural_compatibility(
            reference_axis=axis,
            candidate_axis=target.evidence.axis,
            reference_signature=library.structural_signature,
            candidate_signature=target.evidence.structural_signature,
        )
        quantitative = compare_quantitative_transformability(
            reference_axis=axis, candidate_axis=target.evidence.axis,
            reference_signature=signature,
            candidate_signature=target.evidence.quantitative_signature,
        )
        if not structural.compatible:
            if any(item.reason_code == "recorded_wavelength_mismatch" for item in structural.fields):
                raise ValueError("Check the spectral references: Recorded library wavelengths differ from this image. Check the selected library and measurement bands.")
            raise ValueError("imported library is structurally incompatible with target")
        if not quantitative.transformable:
            raise ValueError("imported library is quantitatively incompatible with target")

    def _accept_imported_library(
        self,
        *,
        run_id: str,
        expected_content_sha256: str,
        reviewer_id: str,
        imported_library: object,
        target: CanonicalReferenceInput,
        current: ReferenceWorkflowCheckpoint,
        start_authority_sha256: str,
        precommit_fence: Callable[[], object],
    ) -> ReferenceServiceAdvance:
        library, canonical = self._strict_imported_library(imported_library)
        self._validate_target(current, target)
        self._validate_imported_library(current, target, library)
        phase = "source"
        if current.content_sha256 != expected_content_sha256:
            if (
                current.status is ReferenceWorkflowStatus.AWAITING_RESPONSE
                and current.request is not None
                and current.request.checkpoint.value == "basis_source"
                and current.request.prior_checkpoint_sha256 == expected_content_sha256
                and current.request.proposed_artifacts
            ):
                phase = "proposal"
            elif (
                current.status is ReferenceWorkflowStatus.AWAITING_PUBLICATION
                and current.pending_publication is not None
                and current.pending_publication.publication_kind == "import_library"
                and current.pending_publication.proposal_artifacts
                and current.decisions[-1].request.prior_checkpoint_sha256 == expected_content_sha256
            ):
                phase = "pending"
            elif current.accepted_library is not None:
                decision = self._accepted_decision(
                    current, BasisSourceChoice.IMPORTED_REFERENCE_LIBRARY
                )
                if decision.request.prior_checkpoint_sha256 != expected_content_sha256:
                    raise ReferenceCheckpointConflictError(
                        "service basis-source checkpoint expectation is stale"
                    )
                phase = "published"
            else:
                raise ReferenceCheckpointConflictError(
                    "service basis-source checkpoint expectation is stale"
                )
        if phase == "source":
            if (
                current.status is not ReferenceWorkflowStatus.AWAITING_RESPONSE
                or current.request is None
                or current.request.checkpoint.value != "basis_source"
            ):
                raise ValueError("workflow is not awaiting basis source review")
            source = current
        else:
            binding = self._existing_binding(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                source_checkpoint_sha256=expected_content_sha256,
            )
            if binding is None:
                raise ReferenceCheckpointConflictError(
                    "imported source retry lacks its operation binding"
                )
            if (
                binding.choice is not BasisSourceChoice.IMPORTED_REFERENCE_LIBRARY
                or binding.reviewer_id != reviewer_id
                or binding.target_input_authority_sha256 != target.authority_sha256
                or binding.imported_library_content_sha256 != library.content_sha256
                or binding.imported_library_bytes_sha256 != hashlib.sha256(canonical).hexdigest()
            ):
                raise ReferenceCheckpointConflictError("basis source retry operation inputs differ")
            source = None
        if phase == "source":
            assert source is not None
            self._persist_binding(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                source=source,
                choice=BasisSourceChoice.IMPORTED_REFERENCE_LIBRARY,
                reviewer_id=reviewer_id,
                target=target,
                imported_library_content_sha256=library.content_sha256,
                imported_library_bytes_sha256=hashlib.sha256(canonical).hexdigest(),
                prepublication_fence=precommit_fence,
            )
        precommit_fence()
        candidate_artifact = self._artifacts.put(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
            authority_checkpoint_sha256=expected_content_sha256,
            kind="imported_library_candidate",
            subject_id="library",
            record=library,
        )
        precommit_fence()
        accepted_artifact = self._artifacts.put(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
            authority_checkpoint_sha256=expected_content_sha256,
            kind="accepted_library",
            subject_id="library",
            record=library,
        )
        proposal_pointer = ArtifactPointer(
            role="candidate_fluorophore_library",
            artifact_id=candidate_artifact.content_sha256,
            artifact_sha256=library.content_sha256,
            subject_id=None,
        )
        if phase == "source":
            proposed = propose_reference_artifacts(
                current,
                (
                    _ReferenceResultOperations._validated_artifact(
                        proposal_pointer,
                        "reviewed_fluorophore_library",
                        candidate_artifact.content_sha256,
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
            raise ReferenceCheckpointConflictError(
                "imported source retry artifact proposal differs"
            )
        if phase in {"source", "proposal"}:
            assert proposed is not None and proposed.request is not None
            response = configured_response(
                proposed.request,
                option_value=BasisSourceChoice.IMPORTED_REFERENCE_LIBRARY.value,
                reviewer_id=reviewer_id,
                artifacts=(proposal_pointer,),
            )
            pending_state = advance_reference_workflow(proposed, response)
        else:
            pending_state = current
        if phase == "published":
            assert current.accepted_library is not None
            reopened = self._artifacts.load_pointer(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                kind="accepted_library",
                subject_id="library",
                pointer=current.accepted_library,
            )
            if reopened.record != library:
                raise ReferenceCheckpointConflictError(
                    "published imported library differs from retry bytes"
                )
            return ReferenceServiceAdvance(
                ReferenceServiceAdvanceStatus.PERSISTED,
                ReferenceServiceSnapshot(current, start_authority_sha256),
            )
        pending = pending_state.pending_publication
        if pending is None:
            raise ValueError("imported library publication authority is missing")
        accepted_pointer = ArtifactPointer(
            role="accepted_fluorophore_library",
            artifact_id=accepted_artifact.content_sha256,
            artifact_sha256=library.content_sha256,
            subject_id=None,
        )
        publication = _ReferenceResultOperations._published_artifact(
            accepted_pointer,
            "reviewed_fluorophore_library",
            pending,
            accepted_artifact.content_sha256,
        )
        precommit_fence()
        result = publish_reference_artifacts(pending_state, (publication,))
        if phase == "source":
            assert proposed is not None
            precommit_fence()
            persisted_proposal, _ = self._store._compare_and_swap_service(
                current.content_sha256, proposed, start_authority_sha256
            )
        elif phase == "proposal":
            persisted_proposal = current
        else:
            persisted_proposal = None
        if phase in {"source", "proposal"}:
            assert persisted_proposal is not None
            precommit_fence()
            persisted_pending, _ = self._store._compare_and_swap_service(
                persisted_proposal.content_sha256,
                pending_state,
                start_authority_sha256,
            )
        else:
            persisted_pending = current
        precommit_fence()
        persisted, persisted_start = self._store._compare_and_swap_service(
            persisted_pending.content_sha256,
            result,
            start_authority_sha256,
        )
        return ReferenceServiceAdvance(
            ReferenceServiceAdvanceStatus.PERSISTED,
            ReferenceServiceSnapshot(persisted, persisted_start),
        )

    @staticmethod
    def _motif_resolution(
        *,
        current: ReferenceWorkflowCheckpoint,
        target: CanonicalReferenceInput,
        binding: BasisSourceDecisionBinding,
    ) -> BasisSourceResolution:
        if (
            binding.choice is not BasisSourceChoice.PHENOIMAGER_MOTIF_PROFILE
            or binding.target_input_authority_sha256 != target.authority_sha256
            or binding.target_image_sha256 != current.target_image_sha256
        ):
            raise ReferenceCheckpointConflictError(
                "MOTIF replay binding differs from canonical target authority"
            )
        resolution = resolve_basis_source(
            binding.choice,
            run_id=current.run_id,
            channel_fluors=target.component_names,
            target_axis=target.evidence.axis,
            target_structural_signature=None,
            target_quantitative_signature=target.evidence.quantitative_signature,
            review=ReviewRecord(
                decision="accepted",
                reviewer_id=binding.reviewer_id,
                reviewed_at=binding.reviewed_at,
            ),
            target_image_sha256=target.evidence.source_image_sha256,
        )
        if not resolution.basis_ready or not resolution.authoritative:
            raise ValueError("reviewed MOTIF source is incompatible with the canonical target")
        return resolution

    def accept_basis_source_choice(
        self,
        run_id: str,
        expected_content_sha256: str,
        choice: BasisSourceChoice | str,
        *,
        reviewer_id: str,
        imported_library,
        component_names: tuple[str, ...],
        target: CanonicalReferenceInput,
        precommit_fence: Callable[[], object],
    ) -> ReferenceServiceAdvance:
        try:
            selected = BasisSourceChoice(choice)
        except (TypeError, ValueError) as exc:
            raise ValueError("basis source choice is unsupported") from exc
        if (
            selected is not BasisSourceChoice.PHENOIMAGER_MOTIF_PROFILE
            and target.evidence.structural_signature is None
        ):
            raise ValueError(
                "empirical basis selection requires target imaging-settings compatibility profile"
            )
        if type(component_names) is not tuple:
            raise TypeError("reference component names must be an exact tuple")
        if component_names and selected is not BasisSourceChoice.CONSTRUCT_REFERENCE_LIBRARY:
            raise ValueError("Only reference-image construction declares components")
        current, start_authority_sha256 = self._store._load_service_checkpoint(run_id)
        if selected is BasisSourceChoice.IMPORTED_REFERENCE_LIBRARY:
            return self._accept_imported_library(
                run_id=run_id,
                expected_content_sha256=expected_content_sha256,
                reviewer_id=reviewer_id,
                imported_library=imported_library,
                target=target,
                current=current,
                start_authority_sha256=start_authority_sha256,
                precommit_fence=precommit_fence,
            )
        if imported_library is not None:
            raise ValueError("non-imported source choice cannot include a library")
        self._validate_target(current, target)
        if current.content_sha256 == expected_content_sha256:
            if (
                current.status is not ReferenceWorkflowStatus.AWAITING_RESPONSE
                or current.request is None
                or current.request.checkpoint.value != "basis_source"
            ):
                raise ValueError("workflow is not awaiting basis source review")
            source = current
        else:
            decision = self._accepted_decision(current, selected)
            if decision.response.selected_ids != component_names:
                raise ReferenceCheckpointConflictError("reference component declaration changed on retry")
            if decision.source_checkpoint_sha256 != expected_content_sha256:
                raise ReferenceCheckpointConflictError(
                    "service basis-source checkpoint expectation is stale"
                )
            source, result, loaded_start = self._store._load_service_decision_authority(
                run_id,
                source_checkpoint_sha256=decision.source_checkpoint_sha256,
                decision_record_sha256=decision.content_sha256,
            )
            if loaded_start != start_authority_sha256 or result.decisions[-1] != decision:
                raise ReferenceCheckpointConflictError("basis source decision authority differs")
            self._persist_binding(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                source=source,
                choice=selected,
                reviewer_id=reviewer_id,
                target=target,
                validate_motif_before_persist=(
                    selected is BasisSourceChoice.PHENOIMAGER_MOTIF_PROFILE
                ),
                prepublication_fence=precommit_fence,
            )
            return ReferenceServiceAdvance(
                ReferenceServiceAdvanceStatus.PERSISTED,
                ReferenceServiceSnapshot(current, start_authority_sha256),
            )
        assert source.request is not None
        response = configured_response(
            source.request,
            option_value=selected.value,
            reviewer_id=reviewer_id,
            selections=tuple(SelectedIdentifier("reference_component", name) for name in component_names),
        )
        candidate = advance_reference_workflow(source, response)
        self._persist_binding(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
            source=source,
            choice=selected,
            reviewer_id=reviewer_id,
            target=target,
            validate_motif_before_persist=(selected is BasisSourceChoice.PHENOIMAGER_MOTIF_PROFILE),
            prepublication_fence=precommit_fence,
        )
        precommit_fence()
        persisted, persisted_start = self._store._compare_and_swap_service(
            source.content_sha256,
            candidate,
            start_authority_sha256,
        )
        return ReferenceServiceAdvance(
            ReferenceServiceAdvanceStatus.PERSISTED,
            ReferenceServiceSnapshot(persisted, persisted_start),
        )

    @staticmethod
    def _validate_reference_input(
        record: CanonicalReferenceInput,
        *,
        target: CanonicalReferenceInput,
        role: str,
        fluorophore: str | None,
    ) -> None:
        if record.input_role != role or record.evidence.fluorophore_identity != fluorophore:
            raise ValueError("canonical reference/control role or fluorophore differs")
        if record.evidence.axis != target.evidence.axis:
            raise ValueError("canonical reference/control measurement axis differs")
        structural = compare_structural_compatibility(
            reference_axis=record.evidence.axis,
            candidate_axis=target.evidence.axis,
            reference_signature=record.evidence.structural_signature,
            candidate_signature=target.evidence.structural_signature,
        )
        quantitative = compare_quantitative_transformability(
            reference_signature=record.evidence.quantitative_signature,
            candidate_signature=target.evidence.quantitative_signature,
        )
        if not record.evidence.structurally_compatible or not structural.compatible:
            raise ValueError("canonical reference/control is structurally incompatible")
        if not record.evidence.quantitatively_transformable or not quantitative.transformable:
            raise ValueError("canonical reference/control is quantitatively incompatible")

    def accept_reference_control_files(
        self,
        run_id: str,
        expected_content_sha256: str,
        positive_reference_input_ids: tuple[str, ...],
        *,
        shared_unstained_input_id: str | None,
        negative_control_input_ids: tuple[str, ...],
        reviewer_id: str,
        target: CanonicalReferenceInput,
        prepare_shared_control: bool = False,
    ) -> ReferenceServiceAdvance:
        option_value = ("accept_reference_control_files_with_source_preparation"
                        if prepare_shared_control else "accept_reference_control_files")
        if target.evidence.structural_signature is None:
            raise ValueError(
                "reference/control selection requires target imaging-settings compatibility profile"
            )
        current, start_authority_sha256 = self._store._load_service_checkpoint(run_id)
        self._validate_target(current, target)
        if type(positive_reference_input_ids) is not tuple:
            raise TypeError("positive reference input IDs must be an exact tuple")
        if type(negative_control_input_ids) is not tuple:
            raise TypeError("negative control input IDs must be an exact tuple")
        phase = "source"
        if current.content_sha256 != expected_content_sha256:
            if (
                current.status is ReferenceWorkflowStatus.AWAITING_RESPONSE
                and current.request is not None
                and current.request.checkpoint.value == "reference_control_files"
                and current.request.prior_checkpoint_sha256 == expected_content_sha256
                and current.request.proposed_artifacts
            ):
                phase = "proposal"
            else:
                decisions = tuple(
                    item
                    for item in current.active_decisions
                    if item.request.checkpoint.value == "reference_control_files"
                    and item.request.prior_checkpoint_sha256 == expected_content_sha256
                    and item.response.option_value == option_value
                )
                if len(decisions) != 1:
                    raise ReferenceCheckpointConflictError(
                        "service file-selection checkpoint expectation is stale"
                    )
                phase = "accepted"
        if phase == "source" and (
            current.status is not ReferenceWorkflowStatus.AWAITING_RESPONSE
            or current.request is None
            or current.request.checkpoint.value != "reference_control_files"
            or current.basis_source != BasisSourceChoice.CONSTRUCT_REFERENCE_LIBRARY.value
        ):
            raise ValueError("workflow is not awaiting constructed reference/control files")
        strategy = current.control_strategy
        if strategy not in {
            "shared_unstained_control",
            "per_stain_negative_controls",
        }:
            raise ValueError("constructed workflow lacks a reviewed control strategy")
        binding = ReferenceControlFilesBinding(
            source_checkpoint_sha256=expected_content_sha256,
            request_id=(
                current.request.request_id
                if phase == "source" and current.request is not None
                else expected_content_sha256
            ),
            reviewer_id=reviewer_id,
            target_input_authority_sha256=target.authority_sha256,
            control_strategy=strategy,
            positive_reference_input_ids=positive_reference_input_ids,
            shared_unstained_input_id=shared_unstained_input_id,
            negative_control_input_ids=negative_control_input_ids,
            prepare_shared_control=prepare_shared_control,
        )
        try:
            stored_bindings = self._artifacts.load_all(
                run_id=run_id,
                start_authority_sha256=start_authority_sha256,
                kind="reference_control_files",
                subject_id="reference-control-files",
            )
        except ReferenceArtifactRepositoryError as exc:
            if "missing" not in str(exc):
                raise
            stored_bindings = ()
        matching_binding = tuple(
            item.record
            for item in stored_bindings
            if item.authority_checkpoint_sha256 == expected_content_sha256
        )
        if matching_binding:
            if len(matching_binding) != 1:
                raise ReferenceCheckpointConflictError(
                    "reference/control file binding is ambiguous"
                )
            existing = matching_binding[0]
            binding = ReferenceControlFilesBinding(
                source_checkpoint_sha256=expected_content_sha256,
                request_id=existing.request_id,
                reviewer_id=reviewer_id,
                target_input_authority_sha256=target.authority_sha256,
                control_strategy=strategy,
                positive_reference_input_ids=positive_reference_input_ids,
                shared_unstained_input_id=shared_unstained_input_id,
                negative_control_input_ids=negative_control_input_ids,
                prepare_shared_control=prepare_shared_control,
            )
            if existing != binding:
                raise ReferenceCheckpointConflictError("reference/control file retry inputs differ")
        elif phase != "source":
            raise ReferenceCheckpointConflictError(
                "reference/control file retry lacks its operation binding"
            )
        inventory = self._artifacts._load_all_canonical_inputs(
            run_id=run_id,
            start_authority_sha256=start_authority_sha256,
        )
        by_id = {item.record.input_id: item for item in inventory}
        if len(by_id) != len(inventory):
            raise ReferenceCheckpointConflictError("canonical input IDs are ambiguous")
        if len(positive_reference_input_ids) != len(current.fluorophores):
            raise ValueError("positive reference IDs must match the exact panel inventory")
        positives = []
        for input_id, fluorophore in zip(
            positive_reference_input_ids, current.fluorophores, strict=True
        ):
            if input_id not in by_id:
                raise ValueError("positive reference input ID is not registered")
            stored = by_id[input_id]
            self._validate_reference_input(
                stored.record,
                target=target,
                role="single_component_reference",
                fluorophore=fluorophore,
            )
            positives.append((stored, fluorophore))
        controls = []
        if strategy == "shared_unstained_control":
            assert shared_unstained_input_id is not None
            if shared_unstained_input_id not in by_id:
                raise ValueError("shared unstained input ID is not registered")
            stored = by_id[shared_unstained_input_id]
            self._validate_reference_input(
                stored.record,
                target=target,
                role="unstained_control",
                fluorophore=None,
            )
            controls.append((stored, "shared"))
        else:
            if len(negative_control_input_ids) != len(current.fluorophores):
                raise ValueError("negative control IDs must match the exact panel inventory")
            for input_id, fluorophore in zip(
                negative_control_input_ids, current.fluorophores, strict=True
            ):
                if input_id not in by_id:
                    raise ValueError("negative control input ID is not registered")
                stored = by_id[input_id]
                self._validate_reference_input(
                    stored.record,
                    target=target,
                    role="single_stain_negative_control",
                    fluorophore=fluorophore,
                )
                controls.append((stored, fluorophore))
        pointers = tuple(
            ArtifactPointer(
                "positive_reference_image",
                stored.content_sha256,
                stored.semantic_sha256,
                fluorophore,
            )
            for stored, fluorophore in positives
        ) + tuple(
            ArtifactPointer(
                (
                    "unstained_reference_image"
                    if strategy == "shared_unstained_control"
                    else "negative_reference_image"
                ),
                stored.content_sha256,
                stored.record.evidence.source_image_sha256,
                subject,
            )
            for stored, subject in controls
        )
        proposals = tuple(
            _ReferenceResultOperations._validated_artifact(
                pointer,
                "canonical_ome_tiff",
                pointer.artifact_id,
            )
            for pointer in pointers
        )
        if phase == "source":
            try:
                self._artifacts.put_unique(
                    run_id=run_id,
                    start_authority_sha256=start_authority_sha256,
                    authority_checkpoint_sha256=expected_content_sha256,
                    kind="reference_control_files",
                    subject_id="reference-control-files",
                    record=binding,
                )
            except ReferenceArtifactRepositoryError as exc:
                raise ReferenceCheckpointConflictError(
                    "reference/control file retry inputs differ"
                ) from exc
            proposed = propose_reference_artifacts(current, proposals)
        elif phase == "proposal":
            proposed = current
        else:
            decision = tuple(
                item
                for item in current.active_decisions
                if item.request.checkpoint.value == "reference_control_files"
                and item.request.prior_checkpoint_sha256 == expected_content_sha256
            )[0]
            if decision.response.artifacts != pointers:
                raise ReferenceCheckpointConflictError(
                    "accepted reference/control pointers differ from canonical inputs"
                )
            return ReferenceServiceAdvance(
                ReferenceServiceAdvanceStatus.PERSISTED,
                ReferenceServiceSnapshot(current, start_authority_sha256),
            )
        if tuple(item.pointer for item in proposed.request.proposed_artifacts) != pointers:  # type: ignore[union-attr]
            raise ReferenceCheckpointConflictError("reference/control retry proposals differ")
        assert proposed.request is not None
        response = configured_response(
            proposed.request,
            option_value=option_value,
            reviewer_id=reviewer_id,
            artifacts=pointers,
        )
        result = advance_reference_workflow(proposed, response)
        if phase == "source":
            persisted_proposal, _ = self._store._compare_and_swap_service(
                current.content_sha256, proposed, start_authority_sha256
            )
        else:
            persisted_proposal = current
        persisted, persisted_start = self._store._compare_and_swap_service(
            persisted_proposal.content_sha256, result, start_authority_sha256
        )
        return ReferenceServiceAdvance(
            ReferenceServiceAdvanceStatus.PERSISTED,
            ReferenceServiceSnapshot(persisted, persisted_start),
        )


__all__ = ["_ReferenceSourceChoiceOperations"]
