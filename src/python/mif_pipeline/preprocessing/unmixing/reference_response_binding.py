"""Bind adapter choices to exact live spectral-reference requests.

The binder is deliberately presentation-neutral. Callers provide only the option and
explicit typed selections; immutable artifact pointers and request evidence acknowledgements
are derived from the immutable request admitted by the workflow owner.

The functions retain the selected spectral basis, component order and reviewed acquisition
facts through the operation. Numerical work is bounded by the configured pixel blocks, while
published references and results keep their input and settings identities."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from .interactions import (
    ArtifactPointer,
    ReferenceActionCode,
    ReferenceCheckpointKind,
    ReferenceEvidence,
    ReferenceInteractionRequest,
    ReferenceOption,
    ReferenceResponse,
    SelectedIdentifier,
    configured_response,
)

# ==============================================================================
#                           Reference response binding
# ==============================================================================


class ReferenceServiceDataRequired(RuntimeError):
    """The live service request lacks exact data needed to bind a response."""


def _selected_option(
    request: ReferenceInteractionRequest,
    option_value: str,
) -> ReferenceOption:
    matches = tuple(item for item in request.options if item.value == option_value)
    if len(matches) != 1:
        raise ValueError("configured response option is not available")
    return matches[0]


def _required_artifacts(
    request: ReferenceInteractionRequest,
    option: ReferenceOption,
) -> tuple[ArtifactPointer, ...]:
    requirements = option.artifact_requirements
    if not requirements:
        if option.minimum_artifacts:
            raise ReferenceServiceDataRequired(
                "needs-service-data: exact artifact requirements are unavailable"
            )
        return ()
    proposals = request.proposed_artifacts
    if len(proposals) != len(requirements):
        raise ReferenceServiceDataRequired(
            "needs-service-data: option lacks its exact proposed artifact inventory"
        )
    used: set[int] = set()
    pointers: list[ArtifactPointer] = []
    for requirement in requirements:
        matches = tuple(
            (index, item)
            for index, item in enumerate(proposals)
            if item.pointer.role == requirement.role
            and item.pointer.subject_id == requirement.subject_id
        )
        if len(matches) != 1:
            raise ReferenceServiceDataRequired(
                "needs-service-data: option lacks its exact proposed artifact inventory"
            )
        index, proposal = matches[0]
        if index in used:
            raise ReferenceServiceDataRequired(
                "needs-service-data: option lacks its exact proposed artifact inventory"
            )
        used.add(index)
        pointers.append(proposal.pointer)
    if len(used) != len(proposals):
        raise ReferenceServiceDataRequired(
            "needs-service-data: option lacks its exact proposed artifact inventory"
        )
    return tuple(pointers)


def _evidence_fluorophore(evidence: ReferenceEvidence) -> str | None:
    matches = tuple(item.value for item in evidence.summary if item.name == "fluorophore")
    if len(matches) != 1 or not isinstance(matches[0], str):
        return None
    return matches[0]


def _negative_fallback_evidence(request: ReferenceInteractionRequest) -> None:
    fluorophore = request.fluorophore
    candidates = tuple(
        item for item in request.evidence if item.kind == "negative_fallback_review_proposal"
    )
    valid = (
        fluorophore is not None
        and len(candidates) == 1
        and _evidence_fluorophore(candidates[0]) == fluorophore
        and candidates[0].artifact_id.startswith(f"negative-fallback:{fluorophore}:")
        and candidates[0].artifact_id != f"negative-fallback:{fluorophore}:"
        and candidates[0].suggested_ids == request.measurement_ids
    )
    if not valid:
        raise ReferenceServiceDataRequired(
            "needs-service-data: exact negative-fallback proposal evidence "
            "for the request fluorophore and measurement axis is unavailable"
        )


def _consensus_inventory(
    request: ReferenceInteractionRequest,
) -> tuple[str, ...]:
    candidates = tuple(
        item for item in request.evidence if item.kind == "consensus_inclusion_review"
    )
    fluorophore = request.fluorophore
    if (
        fluorophore is None
        or len(candidates) != 1
        or not candidates[0].suggested_ids
        or _evidence_fluorophore(candidates[0]) != fluorophore
        or candidates[0].artifact_id != f"consensus-inclusion:{fluorophore}"
    ):
        raise ReferenceServiceDataRequired(
            "needs-service-data: exact consensus candidate inventory is unavailable"
        )
    return candidates[0].suggested_ids


def _validate_signal_dark_selections(
    request: ReferenceInteractionRequest,
    selections: tuple[SelectedIdentifier, ...],
) -> None:
    _negative_fallback_evidence(request)
    if not selections:
        return
    if (
        len(selections) < 2
        or selections[0].role != "signal_measurement"
        or any(item.role != "dark_measurement" for item in selections[1:])
    ):
        raise ValueError("response requires canonical signal/dark selection roles and order")
    if any(item.identifier not in request.measurement_ids for item in selections):
        raise ValueError("selection is outside the authoritative measurement inventory")
    signal_id = selections[0].identifier
    dark_ids = tuple(item.identifier for item in selections[1:])
    dark_set = set(dark_ids)
    if signal_id in dark_ids or dark_ids != tuple(
        item for item in request.measurement_ids if item in dark_set
    ):
        raise ValueError("response requires canonical signal/dark selection roles and order")


def _bound_selections(
    request: ReferenceInteractionRequest,
    option: ReferenceOption,
    selections: tuple[SelectedIdentifier, ...],
    artifacts: tuple[ArtifactPointer, ...],
) -> tuple[SelectedIdentifier, ...]:
    if type(selections) is not tuple or any(
        type(item) is not SelectedIdentifier for item in selections
    ):
        raise TypeError("selections must be an exact tuple of SelectedIdentifier records")

    if option.action.code is ReferenceActionCode.RESELECT:
        if selections:
            raise ValueError("reselection is derived by the live request")
        if request.fluorophore is None:
            return ()
        return (SelectedIdentifier("affected_fluorophore", request.fluorophore),)

    if (
        request.checkpoint is ReferenceCheckpointKind.SIGNAL_DARK_MEASUREMENTS
        and option.action.code is ReferenceActionCode.ACCEPT
    ):
        _validate_signal_dark_selections(request, selections)
        return selections

    if (
        request.checkpoint is ReferenceCheckpointKind.EXTRACTION_QC
        and option.action.code is ReferenceActionCode.ACCEPT
    ):
        inventory = _consensus_inventory(request)
        if tuple(item.identifier for item in selections) != inventory or any(
            item.role not in {"consensus_included_roi", "consensus_excluded_roi"}
            for item in selections
        ):
            raise ValueError(
                "response must disposition the complete authoritative consensus inventory"
            )
        consensus_evidence = next(
            item for item in request.evidence if item.kind == "consensus_inclusion_review"
        )
        if (
            len(artifacts) != 1
            or artifacts[0].role != "extraction_qc"
            or artifacts[0].subject_id != request.fluorophore
            or artifacts[0].artifact_sha256 != consensus_evidence.artifact_sha256
        ):
            raise ReferenceServiceDataRequired(
                "needs-service-data: extraction-QC artifact does not bind the "
                "consensus inventory hash"
            )
        return selections

    if selections:
        raise ValueError("response contains unexpected explicit selections")
    return ()


def bind_reference_response(
    request: ReferenceInteractionRequest,
    *,
    option_value: str,
    reviewer_id: str,
    selections: tuple[SelectedIdentifier, ...] = (),
) -> ReferenceResponse:
    """Create one response using only authority present in the live request."""

    if type(request) is not ReferenceInteractionRequest:
        raise TypeError("request must be an exact ReferenceInteractionRequest")
    option = _selected_option(request, option_value)
    artifacts = _required_artifacts(request, option)
    bound_selections = _bound_selections(request, option, selections, artifacts)
    return configured_response(
        request,
        option_value=option_value,
        reviewer_id=reviewer_id,
        selections=bound_selections,
        artifacts=artifacts,
    )


__all__ = ["ReferenceServiceDataRequired", "bind_reference_response"]
