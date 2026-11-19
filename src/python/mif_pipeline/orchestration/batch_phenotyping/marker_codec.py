"""Strict canonical JSON codec for marker-gate responses.

The operation retains the selected stable-cell population, measurement generation and
phenotype view. Its records and stage adapters connect accepted numerical results to the
current batch target and reviewed application scope."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from .marker_records import (
    MarkerGateResponse,
    MarkerResponseError,
    MarkerSelection,
    SectionOverride,
    SlideOverride,
    _digest,
    _text,
)

# ==============================================================================
#                                  Marker codec
# ==============================================================================


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise MarkerResponseError("response must be canonical JSON") from exc


def _closed(value: object, fields: frozenset[str], label: str) -> Mapping[str, object]:
    if (
        not isinstance(value, Mapping)
        or set(value) != fields
        or any(type(k) is not str for k in value)
    ):
        raise MarkerResponseError(f"{label} has invalid fields")
    return value


def _parse(value: object) -> object:
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    else:
        raise MarkerResponseError("response must be UTF-8 JSON bytes")
    try:
        parsed = json.loads(
            raw.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise MarkerResponseError("response is not valid JSON") from exc
    if _canonical(parsed) != raw:
        raise MarkerResponseError("response JSON is not canonical")
    return parsed


_ADJUSTMENT_FIELDS = frozenset({"adjusted_threshold", "adjusted_score_cutoff", "adjustment_source"})


def _adjustment_fields(value):
    return {name: getattr(value, name) for name in _ADJUSTMENT_FIELDS if getattr(value, name) is not None}


def encode_marker_gate_response(response: MarkerGateResponse) -> bytes:
    if not isinstance(response, MarkerGateResponse):
        raise MarkerResponseError("response must be a MarkerGateResponse")
    return _canonical(
        {
            "evidence_sha256": response.evidence_sha256,
            "selections": [
                {
                    "marker_id": item.marker_id,
                    "review_record_id": item.review_record_id,
                    "reviewer_id": item.reviewer_id,
                    "section_overrides": [
                        {
                            "manual_threshold": override.manual_threshold,
                            "reason": override.reason,
                            "review_record_id": override.review_record_id,
                            "reviewer_id": override.reviewer_id,
                            "section_id": override.section_id,
                            "selection_source": override.selection_source,
                        }
                        for override in item.section_overrides
                    ],
                    **_adjustment_fields(item),
                    **({"nimbus_distill": True} if item.nimbus_distill else {}),
                    **({"slide_overrides": [
                        {"image_identity": override.image_identity,
                         "selection_source": override.selection_source,
                         "manual_threshold": override.manual_threshold,
                         "reason": override.reason,
                         "reviewer_id": override.reviewer_id,
                         "review_record_id": override.review_record_id,
                         **_adjustment_fields(override)}
                        for override in item.slide_overrides
                    ]} if item.slide_overrides else {}),
                    "selection_source": item.selection_source,
                    "manual_threshold": item.manual_threshold,
                }
                for item in response.selections
            ],
            "validation_domain_id": response.validation_domain_id,
            **({"refit_current_marker": True} if response.refit_current_marker else {}),
            **({'marker_settings': response.marker_settings} if response.marker_settings is not None else {}),
        }
    )


def decode_marker_gate_response(
    payload: bytes,
    *,
    expected_validation_domain_id: str,
    expected_evidence_sha256: str,
    expected_marker_ids: Sequence[str],
    allowed_section_ids: Sequence[str],
    allowed_slide_ids: Sequence[str] = (),
) -> MarkerGateResponse:
    _text(expected_validation_domain_id, "expected_validation_domain_id")
    _digest(expected_evidence_sha256, "expected_evidence_sha256")

    def owner_scope(value: object, field: str) -> tuple[str, ...]:
        if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
            raise MarkerResponseError(f"{field} must be a canonical sequence")
        result = tuple(_text(item, field) for item in value)
        if not result or result != tuple(sorted(result)) or len(set(result)) != len(result):
            raise MarkerResponseError(f"{field} must be nonempty, sorted, and unique")
        return result

    expected_markers = owner_scope(expected_marker_ids, "expected_marker_ids")
    allowed_sections = owner_scope(allowed_section_ids, "allowed_section_ids") if allowed_section_ids or not allowed_slide_ids else ()
    allowed_slides = owner_scope(allowed_slide_ids, "allowed_slide_ids") if allowed_slide_ids else ()
    parsed_root = _parse(payload)
    root = _closed(
        parsed_root,
        frozenset({"validation_domain_id", "evidence_sha256", "selections"} | ({key for key in ('refit_current_marker','marker_settings') if key in parsed_root} if isinstance(parsed_root, Mapping) else set())),
        "response",
    )
    if (
        root["validation_domain_id"] != expected_validation_domain_id
        or root["evidence_sha256"] != expected_evidence_sha256
    ):
        raise MarkerResponseError("response domain or evidence digest differs")
    if not isinstance(root["selections"], list):
        raise MarkerResponseError("selections must be an array")
    selections: list[MarkerSelection] = []
    for raw in root["selections"]:
        item = _closed(
            raw,
            frozenset(
                {
                    "marker_id",
                    "selection_source",
                    "manual_threshold",
                    "reviewer_id",
                    "review_record_id",
                    "section_overrides",
                } | ({"slide_overrides"} if isinstance(raw, Mapping) and "slide_overrides" in raw else set())
                  | ({"nimbus_distill"} if isinstance(raw, Mapping) and "nimbus_distill" in raw else set())
                  | (_ADJUSTMENT_FIELDS & set(raw) if isinstance(raw, Mapping) else set())
            ),
            "selection",
        )
        overrides_raw = item["section_overrides"]
        if not isinstance(overrides_raw, list):
            raise MarkerResponseError("section_overrides must be an array")
        overrides: list[SectionOverride] = []
        for override_raw in overrides_raw:
            override = _closed(
                override_raw,
                frozenset(
                    {
                        "section_id",
                        "selection_source",
                        "manual_threshold",
                        "reason",
                        "reviewer_id",
                        "review_record_id",
                    }
                ),
                "section override",
            )
            overrides.append(SectionOverride(**override))
        slide_values = item.get("slide_overrides", [])
        if not isinstance(slide_values, list):
            raise MarkerResponseError("slide_overrides must be an array")
        slide_overrides = tuple(SlideOverride(**_closed(raw_slide, frozenset({
            "image_identity", "selection_source", "manual_threshold", "reason", "reviewer_id", "review_record_id"
        } | (_ADJUSTMENT_FIELDS & set(raw_slide) if isinstance(raw_slide, Mapping) else set())), "slide override")) for raw_slide in slide_values)
        selections.append(MarkerSelection(**dict(item, section_overrides=tuple(overrides), slide_overrides=slide_overrides)))
    result = MarkerGateResponse(
        root["validation_domain_id"], root["evidence_sha256"], tuple(selections), root.get("refit_current_marker", False), root.get('marker_settings')
    )
    expected = expected_markers
    if not result.refit_current_marker and tuple(item.marker_id for item in result.selections) != expected:
        raise MarkerResponseError("selection marker set differs")
    allowed = set(allowed_sections)
    if any(
        override.section_id not in allowed
        for item in result.selections
        for override in item.section_overrides
    ):
        raise MarkerResponseError("section override is out of scope")
    if any(override.image_identity not in set(allowed_slides) for item in result.selections for override in item.slide_overrides):
        raise MarkerResponseError("slide override is out of scope")
    return result


__all__ = ["MarkerResponseError", "decode_marker_gate_response", "encode_marker_gate_response"]
