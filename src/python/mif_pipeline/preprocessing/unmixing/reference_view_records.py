"""Bounded, presentation-neutral review views for spectral-reference evidence.

The records here are disposable projections of exact stage-owned results. They are not
workflow state, scientific authority, or a home for sampled WSI arrays.

The `ReferenceReviewData` record carries `request_id`, `request_revision`, `checkpoint`,
`authoritative_state_sha256`, `workflow_checkpoint_sha256` and its remaining declared
fields.

The functions retain the selected spectral basis, component order and reviewed acquisition
facts through the operation. Numerical work is bounded by the configured pixel blocks, while
published references and results keep their input and settings identities."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
from typing import TypeAlias

from .interactions import ReferenceCheckpointKind, ReferenceInteractionRequest
from .typed_checksum import typed_checksum_sha256

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_TEXT = 4_096
_MAX_SERIES_POINTS = 16_384
_MAX_TOTAL_POINTS = 32_768
_MAX_TABLE_COLUMNS = 64
_MAX_TABLE_ROWS = 4_096
_MAX_REVIEW_BYTES = 1_048_576

ReviewScalar: TypeAlias = str | int | float | bool | None


# ==============================================================================
#                             Reference view records
# ==============================================================================


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be nonempty literal text")
    if len(value) > _MAX_TEXT:
        raise ValueError(f"{label} exceeds the review-data text bound")
    return value


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def _finite(value: object, label: str) -> float:
    if type(value) is not float or not math.isfinite(value):
        raise ValueError(f"{label} must be an exact finite float")
    return value


def _scalar(value: object, label: str) -> ReviewScalar:
    if value is None or type(value) in {bool, int}:
        return value  # type: ignore[return-value]
    if type(value) is float:
        return _finite(value, label)
    if type(value) is str:
        return _text(value, label)
    raise TypeError(f"{label} must be an exact JSON scalar")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _exact_mapping(value: object, expected: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{label} schema fields differ")
    return value


@dataclass(frozen=True, slots=True)
class ReferenceReviewPoint:
    x: float
    y: float

    def __post_init__(self) -> None:
        _finite(self.x, "review point x")
        _finite(self.y, "review point y")

    def to_mapping(self) -> dict[str, object]:
        return {"x": self.x, "y": self.y}

    @classmethod
    def from_mapping(cls, value: object) -> "ReferenceReviewPoint":
        root = _exact_mapping(value, {"x", "y"}, "review point")
        return cls(root["x"], root["y"])  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ReferenceReviewSeries:
    series_id: str
    label: str
    x_axis_label: str
    x_unit: str | None
    y_axis_label: str
    y_unit: str | None
    style_role: str
    points: tuple[ReferenceReviewPoint, ...]

    def __post_init__(self) -> None:
        _text(self.series_id, "review series ID")
        _text(self.label, "review series label")
        _text(self.x_axis_label, "review x-axis label")
        _optional_text(self.x_unit, "review x-axis unit")
        _text(self.y_axis_label, "review y-axis label")
        _optional_text(self.y_unit, "review y-axis unit")
        _text(self.style_role, "review series style role")
        if type(self.points) is not tuple or any(
            type(item) is not ReferenceReviewPoint for item in self.points
        ):
            raise TypeError("review series points must be an exact tuple")
        if not self.points:
            raise ValueError("review series must contain at least one point")
        if len(self.points) > _MAX_SERIES_POINTS:
            raise ValueError("review series exceeds its point-count ceiling")

    def to_mapping(self) -> dict[str, object]:
        return {
            "series_id": self.series_id,
            "label": self.label,
            "x_axis_label": self.x_axis_label,
            "x_unit": self.x_unit,
            "y_axis_label": self.y_axis_label,
            "y_unit": self.y_unit,
            "style_role": self.style_role,
            "points": [item.to_mapping() for item in self.points],
        }

    @classmethod
    def from_mapping(cls, value: object) -> "ReferenceReviewSeries":
        root = _exact_mapping(
            value,
            {
                "series_id",
                "label",
                "x_axis_label",
                "x_unit",
                "y_axis_label",
                "y_unit",
                "style_role",
                "points",
            },
            "review series",
        )
        if not isinstance(root["points"], list):
            raise TypeError("review series points must be an array")
        return cls(
            series_id=root["series_id"],  # type: ignore[arg-type]
            label=root["label"],  # type: ignore[arg-type]
            x_axis_label=root["x_axis_label"],  # type: ignore[arg-type]
            x_unit=root["x_unit"],  # type: ignore[arg-type]
            y_axis_label=root["y_axis_label"],  # type: ignore[arg-type]
            y_unit=root["y_unit"],  # type: ignore[arg-type]
            style_role=root["style_role"],  # type: ignore[arg-type]
            points=tuple(ReferenceReviewPoint.from_mapping(item) for item in root["points"]),
        )


@dataclass(frozen=True, slots=True)
class ReferenceReviewTableColumn:
    column_id: str
    label: str
    unit: str | None

    def __post_init__(self) -> None:
        _text(self.column_id, "review table column ID")
        _text(self.label, "review table column label")
        _optional_text(self.unit, "review table column unit")

    def to_mapping(self) -> dict[str, object]:
        return {"column_id": self.column_id, "label": self.label, "unit": self.unit}

    @classmethod
    def from_mapping(cls, value: object) -> "ReferenceReviewTableColumn":
        root = _exact_mapping(value, {"column_id", "label", "unit"}, "review table column")
        return cls(**root)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ReferenceReviewTableRow:
    row_id: str
    label: str
    cells: tuple[ReviewScalar, ...]

    def __post_init__(self) -> None:
        _text(self.row_id, "review table row ID")
        _text(self.label, "review table row label")
        if type(self.cells) is not tuple:
            raise TypeError("review table cells must be an exact tuple of JSON scalars")
        for item in self.cells:
            _scalar(item, "review table cell scalar")

    def to_mapping(self) -> dict[str, object]:
        return {"row_id": self.row_id, "label": self.label, "cells": list(self.cells)}

    @classmethod
    def from_mapping(cls, value: object) -> "ReferenceReviewTableRow":
        root = _exact_mapping(value, {"row_id", "label", "cells"}, "review table row")
        if not isinstance(root["cells"], list):
            raise TypeError("review table cells must be an array")
        return cls(
            row_id=root["row_id"],  # type: ignore[arg-type]
            label=root["label"],  # type: ignore[arg-type]
            cells=tuple(root["cells"]),
        )


@dataclass(frozen=True, slots=True)
class ReferenceReviewTable:
    table_id: str
    label: str
    columns: tuple[ReferenceReviewTableColumn, ...]
    rows: tuple[ReferenceReviewTableRow, ...]

    def __post_init__(self) -> None:
        _text(self.table_id, "review table ID")
        _text(self.label, "review table label")
        if (
            type(self.columns) is not tuple
            or not self.columns
            or any(type(item) is not ReferenceReviewTableColumn for item in self.columns)
        ):
            raise TypeError("review table columns must be an exact nonempty tuple")
        if len(self.columns) > _MAX_TABLE_COLUMNS:
            raise ValueError("review table exceeds its column-count ceiling")
        column_ids = tuple(item.column_id for item in self.columns)
        if len(column_ids) != len(set(column_ids)):
            raise ValueError("review table column IDs must be unique")
        if type(self.rows) is not tuple or any(
            type(item) is not ReferenceReviewTableRow for item in self.rows
        ):
            raise TypeError("review table rows must be an exact tuple")
        if len(self.rows) > _MAX_TABLE_ROWS:
            raise ValueError("review table exceeds its row-count ceiling")
        row_ids = tuple(item.row_id for item in self.rows)
        if len(row_ids) != len(set(row_ids)):
            raise ValueError("review table row IDs must be unique")
        if any(len(item.cells) != len(self.columns) for item in self.rows):
            raise ValueError("review table row width differs from its columns")

    def to_mapping(self) -> dict[str, object]:
        return {
            "table_id": self.table_id,
            "label": self.label,
            "columns": [item.to_mapping() for item in self.columns],
            "rows": [item.to_mapping() for item in self.rows],
        }

    @classmethod
    def from_mapping(cls, value: object) -> "ReferenceReviewTable":
        root = _exact_mapping(value, {"table_id", "label", "columns", "rows"}, "review table")
        if not isinstance(root["columns"], list) or not isinstance(root["rows"], list):
            raise TypeError("review table columns and rows must be arrays")
        return cls(
            table_id=root["table_id"],  # type: ignore[arg-type]
            label=root["label"],  # type: ignore[arg-type]
            columns=tuple(
                ReferenceReviewTableColumn.from_mapping(item) for item in root["columns"]
            ),
            rows=tuple(ReferenceReviewTableRow.from_mapping(item) for item in root["rows"]),
        )


@dataclass(frozen=True, slots=True)
class ReferenceReviewArtifactBinding:
    role: str
    artifact_id: str
    artifact_sha256: str

    def __post_init__(self) -> None:
        _text(self.role, "review source role")
        _text(self.artifact_id, "review source artifact ID")
        _sha256(self.artifact_sha256, "review source artifact SHA-256")

    def to_mapping(self) -> dict[str, object]:
        return {
            "role": self.role,
            "artifact_id": self.artifact_id,
            "artifact_sha256": self.artifact_sha256,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "ReferenceReviewArtifactBinding":
        root = _exact_mapping(
            value, {"role", "artifact_id", "artifact_sha256"}, "review source binding"
        )
        return cls(**root)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ReferenceReviewData:
    SCHEMA_NAME = "mif_pipeline.spectral_reference_review_data"
    SCHEMA_VERSION = 1

    request_id: str
    request_revision: int
    checkpoint: str
    authoritative_state_sha256: str
    workflow_checkpoint_sha256: str | None
    source_bindings: tuple[ReferenceReviewArtifactBinding, ...]
    series: tuple[ReferenceReviewSeries, ...]
    tables: tuple[ReferenceReviewTable, ...]
    content_sha256: str

    def __post_init__(self) -> None:
        _sha256(self.request_id, "review request ID")
        if type(self.request_revision) is not int or self.request_revision < 0:
            raise ValueError("review request revision must be nonnegative")
        try:
            ReferenceCheckpointKind(self.checkpoint)
        except (TypeError, ValueError) as exc:
            raise ValueError("review checkpoint is unsupported") from exc
        _sha256(self.authoritative_state_sha256, "review authoritative-state SHA-256")
        if self.workflow_checkpoint_sha256 is not None:
            _sha256(self.workflow_checkpoint_sha256, "review workflow-checkpoint SHA-256")
        for value, expected, label in (
            (self.source_bindings, ReferenceReviewArtifactBinding, "source bindings"),
            (self.series, ReferenceReviewSeries, "series"),
            (self.tables, ReferenceReviewTable, "tables"),
        ):
            if type(value) is not tuple or any(type(item) is not expected for item in value):
                raise TypeError(f"review {label} must be an exact tuple")
        if not self.source_bindings:
            raise ValueError("review data requires exact source bindings")
        binding_keys = tuple((item.role, item.artifact_id) for item in self.source_bindings)
        if len(binding_keys) != len(set(binding_keys)):
            raise ValueError("review source bindings must be unique")
        series_ids = tuple(item.series_id for item in self.series)
        table_ids = tuple(item.table_id for item in self.tables)
        if len(series_ids) != len(set(series_ids)) or len(table_ids) != len(set(table_ids)):
            raise ValueError("review series and table IDs must be unique")
        if sum(len(item.points) for item in self.series) > _MAX_TOTAL_POINTS:
            raise ValueError("review data exceeds its total point-count ceiling")
        _sha256(self.content_sha256, "review content SHA-256")
        if self.content_sha256 != typed_checksum_sha256(self._content_mapping()):
            raise ValueError("review data content checksum differs")
        if len(_canonical_json(self.to_mapping())) > _MAX_REVIEW_BYTES:
            raise ValueError("review data exceeds its serialized-size ceiling")

    def _content_mapping(self) -> dict[str, object]:
        return {
            "schema_name": self.SCHEMA_NAME,
            "schema_version": self.SCHEMA_VERSION,
            "request_id": self.request_id,
            "request_revision": self.request_revision,
            "checkpoint": self.checkpoint,
            "authoritative_state_sha256": self.authoritative_state_sha256,
            "workflow_checkpoint_sha256": self.workflow_checkpoint_sha256,
            "source_bindings": [item.to_mapping() for item in self.source_bindings],
            "series": [item.to_mapping() for item in self.series],
            "tables": [item.to_mapping() for item in self.tables],
        }

    def to_mapping(self) -> dict[str, object]:
        return {**self._content_mapping(), "content_sha256": self.content_sha256}

    @classmethod
    def create(
        cls,
        *,
        request: ReferenceInteractionRequest,
        workflow_checkpoint_sha256: str | None,
        source_bindings: tuple[ReferenceReviewArtifactBinding, ...],
        series: tuple[ReferenceReviewSeries, ...],
        tables: tuple[ReferenceReviewTable, ...],
    ) -> "ReferenceReviewData":
        if type(request) is not ReferenceInteractionRequest:
            raise TypeError("review request must be an exact ReferenceInteractionRequest")
        if type(source_bindings) is not tuple or any(
            type(item) is not ReferenceReviewArtifactBinding for item in source_bindings
        ):
            raise TypeError("review source bindings must be an exact tuple")
        for binding in source_bindings:
            evidence_matches = tuple(
                item
                for item in request.evidence
                if (
                    item.kind,
                    item.artifact_id,
                    item.artifact_sha256,
                )
                == (binding.role, binding.artifact_id, binding.artifact_sha256)
            )
            proposal_matches = tuple(
                item
                for item in request.proposed_artifacts
                if (
                    item.pointer.role,
                    item.pointer.artifact_id,
                    item.pointer.artifact_sha256,
                )
                == (binding.role, binding.artifact_id, binding.artifact_sha256)
            )
            if len(evidence_matches) + len(proposal_matches) != 1:
                raise ValueError(
                    "each review source requires one exact source binding in the request"
                )
        content = {
            "schema_name": cls.SCHEMA_NAME,
            "schema_version": cls.SCHEMA_VERSION,
            "request_id": request.request_id,
            "request_revision": request.revision,
            "checkpoint": request.checkpoint.value,
            "authoritative_state_sha256": request.authoritative_state_sha256,
            "workflow_checkpoint_sha256": workflow_checkpoint_sha256,
            "source_bindings": [item.to_mapping() for item in source_bindings],
            "series": [item.to_mapping() for item in series],
            "tables": [item.to_mapping() for item in tables],
        }
        return cls(
            request_id=request.request_id,
            request_revision=request.revision,
            checkpoint=request.checkpoint.value,
            authoritative_state_sha256=request.authoritative_state_sha256,
            workflow_checkpoint_sha256=workflow_checkpoint_sha256,
            source_bindings=source_bindings,
            series=series,
            tables=tables,
            content_sha256=typed_checksum_sha256(content),
        )

    @classmethod
    def from_mapping(cls, value: object) -> "ReferenceReviewData":
        root = _exact_mapping(
            value,
            {
                "schema_name",
                "schema_version",
                "request_id",
                "request_revision",
                "checkpoint",
                "authoritative_state_sha256",
                "workflow_checkpoint_sha256",
                "source_bindings",
                "series",
                "tables",
                "content_sha256",
            },
            "reference review data",
        )
        if root["schema_name"] != cls.SCHEMA_NAME or root["schema_version"] != cls.SCHEMA_VERSION:
            raise ValueError("unsupported reference review data schema")
        if not all(isinstance(root[key], list) for key in ("source_bindings", "series", "tables")):
            raise TypeError("reference review collections must be arrays")
        return cls(
            request_id=root["request_id"],  # type: ignore[arg-type]
            request_revision=root["request_revision"],  # type: ignore[arg-type]
            checkpoint=root["checkpoint"],  # type: ignore[arg-type]
            authoritative_state_sha256=root["authoritative_state_sha256"],  # type: ignore[arg-type]
            workflow_checkpoint_sha256=root["workflow_checkpoint_sha256"],  # type: ignore[arg-type]
            source_bindings=tuple(
                ReferenceReviewArtifactBinding.from_mapping(item)
                for item in root["source_bindings"]
            ),
            series=tuple(ReferenceReviewSeries.from_mapping(item) for item in root["series"]),
            tables=tuple(ReferenceReviewTable.from_mapping(item) for item in root["tables"]),
            content_sha256=root["content_sha256"],  # type: ignore[arg-type]
        )


def dumps_reference_review_data(value: ReferenceReviewData) -> str:
    if type(value) is not ReferenceReviewData:
        raise TypeError("value must be exact ReferenceReviewData")
    return _canonical_json(value.to_mapping()).decode("utf-8")


def loads_reference_review_data(payload: str | bytes) -> ReferenceReviewData:
    def exact_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_nonfinite(value: str) -> object:
        raise ValueError(f"nonfinite JSON number is forbidden: {value}")

    try:
        root = json.loads(
            payload,
            object_pairs_hook=exact_object,
            parse_constant=reject_nonfinite,
        )
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("reference review data is not valid JSON") from exc
    return ReferenceReviewData.from_mapping(root)


__all__ = [
    "ReferenceReviewArtifactBinding",
    "ReferenceReviewData",
    "ReferenceReviewPoint",
    "ReferenceReviewSeries",
    "ReferenceReviewTable",
    "ReferenceReviewTableColumn",
    "ReferenceReviewTableRow",
    "dumps_reference_review_data",
    "loads_reference_review_data",
]
