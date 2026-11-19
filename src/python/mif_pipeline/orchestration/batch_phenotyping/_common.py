"""Private validation and canonical encoding helpers.

The operation retains the selected stable-cell population, measurement generation and
phenotype view. Its records and stage adapters connect accepted numerical results to the
current batch target and reviewed application scope."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
from math import isfinite
import re
import os
from pathlib import Path
import stat

from mif_pipeline.cell_analysis.clustering import (
    CellClusteringError,
    FeatureRole,
    FeatureSpec,
)
from mif_pipeline.pipeline_core.models import CellRecord, ROIAnalysisError

INPUT_SCHEMA = "mif-batch-unsupervised-input/v2"
RESULT_SCHEMA = "mif-batch-clustering-result/v1"
PREPARATION_PRODUCER_VERSION = "mif-pipeline-batch-unsupervised-preparation/v2"
DEFAULT_MAXIMUM_INPUT_BYTES = 512 * 1024 * 1024
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


# ==============================================================================
#                                     Common
# ==============================================================================


class BatchPhenotypingError(ROIAnalysisError):
    """Raised when phenotype graph authority or result identity is invalid."""


def text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BatchPhenotypingError(f"{field} must be nonblank canonical text")
    if len(value.encode("utf-8")) > 4096:
        raise BatchPhenotypingError(f"{field} exceeds its UTF-8 byte limit")
    return value


def optional_text(value: object, field: str) -> str | None:
    return None if value is None else text(value, field)


def sha256(value: object, field: str) -> str:
    value = text(value, field)
    if SHA256_PATTERN.fullmatch(value) is None:
        raise BatchPhenotypingError(f"{field} must be a lowercase SHA-256")
    return value


def integer(value: object, field: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        qualifier = "positive" if positive else "nonnegative"
        raise BatchPhenotypingError(f"{field} must be a {qualifier} integer")
    return value


def number(value: object, field: str, *, positive: bool = False) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(float(value))
        or (positive and float(value) <= 0.0)
    ):
        qualifier = "positive finite" if positive else "finite"
        raise BatchPhenotypingError(f"{field} must be {qualifier}")
    return float(value)


def exact_tuple(value: object, field: str) -> tuple[object, ...]:
    if type(value) is not tuple:
        raise BatchPhenotypingError(f"{field} must be an exact tuple")
    return value


def canonical_ids(values: object, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    resolved = tuple(text(item, field) for item in exact_tuple(values, field))
    if (not resolved and not allow_empty) or resolved != tuple(sorted(set(resolved))):
        raise BatchPhenotypingError(f"{field} must be a canonical sorted unique tuple")
    return resolved


def mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(type(key) is str for key in value):
        raise BatchPhenotypingError(f"{field} must be a string-keyed object")
    return value


def closed_mapping(value: object, fields: frozenset[str], field: str) -> Mapping[str, object]:
    value = mapping(value, field)
    if set(value) != fields:
        raise BatchPhenotypingError(f"{field} does not have its exact closed fields")
    return value


def sequence(value: object, field: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise BatchPhenotypingError(f"{field} must be an array")
    return value


def plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [plain_json(item) for item in value]
    return value


def canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            plain_json(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BatchPhenotypingError("phenotyping material is not canonical JSON") from exc


def digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def read_bounded_file(
    path: Path,
    *,
    expected_byte_count: int,
    maximum_byte_count: int,
) -> bytes:
    """Read one regular file without following links or allocating past its bound."""

    expected = integer(expected_byte_count, "expected_byte_count")
    maximum = integer(maximum_byte_count, "maximum_byte_count", positive=True)
    if expected > maximum or path.is_symlink():
        raise BatchPhenotypingError("artifact exceeds its read bound or is a symlink")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            details = os.fstat(stream.fileno())
            if not stat.S_ISREG(details.st_mode) or details.st_size != expected:
                raise BatchPhenotypingError("artifact size or file type differs")
            payload = stream.read(expected + 1)
    except BatchPhenotypingError:
        raise
    except OSError as exc:
        raise BatchPhenotypingError("artifact bytes are unreadable") from exc
    if len(payload) != expected:
        raise BatchPhenotypingError("artifact byte count differs")
    return payload


FEATURE_FIELDS = frozenset(
    {
        "measurement",
        "role",
        "channel_id",
        "feature_id",
        "transform",
        "weight",
        "kind",
        "biomarker",
        "compartment",
        "units",
        "producer",
        "producer_version",
        "missing_rate",
        "non_finite_rate",
        "opt_in",
    }
)


def feature_mapping(value: FeatureSpec) -> dict[str, object]:
    return {
        "measurement": value.measurement,
        "role": value.role.value,
        "channel_id": value.channel_id,
        "feature_id": value.feature_id,
        "transform": value.transform,
        "weight": value.weight,
        "kind": value.kind,
        "biomarker": value.biomarker,
        "compartment": value.compartment,
        "units": value.units,
        "producer": value.producer,
        "producer_version": value.producer_version,
        "missing_rate": value.missing_rate,
        "non_finite_rate": value.non_finite_rate,
        "opt_in": value.opt_in,
    }


def feature_from_mapping(value: object) -> FeatureSpec:
    value = closed_mapping(value, FEATURE_FIELDS, "feature specification")
    try:
        return FeatureSpec(
            measurement=value["measurement"],
            role=FeatureRole(value["role"]),
            channel_id=value["channel_id"],
            feature_id=value["feature_id"],
            transform=value["transform"],
            weight=value["weight"],
            kind=value["kind"],
            biomarker=value["biomarker"],
            compartment=value["compartment"],
            units=value["units"],
            producer=value["producer"],
            producer_version=value["producer_version"],
            missing_rate=value["missing_rate"],
            non_finite_rate=value["non_finite_rate"],
            opt_in=value["opt_in"],
        )  # type: ignore[arg-type]
    except (CellClusteringError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("feature specification is invalid") from exc


def validated_features(values: object) -> tuple[FeatureSpec, ...]:
    values = exact_tuple(values, "feature_specs")
    if not values or not all(type(item) is FeatureSpec for item in values):
        raise BatchPhenotypingError("feature_specs must contain exact FeatureSpec values")
    features: tuple[FeatureSpec, ...] = values  # type: ignore[assignment]
    measurements = tuple(item.measurement for item in features)
    feature_ids = tuple(item.feature_id for item in features)
    if (
        any(item.role is not FeatureRole.EXPRESSION for item in features)
        or any(item.kind != "expression" or item.transform != "robust_scale" for item in features)
        or len(set(measurements)) != len(measurements)
        or any(item is None for item in feature_ids)
        or len(set(feature_ids)) != len(feature_ids)
    ):
        raise BatchPhenotypingError(
            "feature_specs must be unique ordered protein-marker intensity features "
            "using the compatible expression wire token and robust_scale transform"
        )
    return features


def feature_schema_sha256(values: tuple[FeatureSpec, ...]) -> str:
    return digest([feature_mapping(item) for item in values])


def cell_inventory_sha256(
    sample_id: str, project_entry_id: str, cells: tuple[CellRecord, ...]
) -> str:
    return digest([[sample_id, project_entry_id, cell.to_export_mapping()] for cell in cells])


def compact_cell_inventory_sha256(
    *,
    sample_id: str,
    project_entry_id: str,
    section_id: str,
    measurement_rows_reference_sha256: str,
    row_inventory_sha256: str,
    cell_count: int,
    cell_ids_sha256: str,
    cell_to_domain_membership_sha256: str,
    domain_cell_commitments_sha256: str,
    feature_schema_sha256_value: str,
) -> str:
    """Digest the compact, row-owner-backed preparation population identity."""

    return digest(
        {
            "sample_id": sample_id,
            "project_entry_id": project_entry_id,
            "section_id": section_id,
            "measurement_rows_reference_sha256": (measurement_rows_reference_sha256),
            "row_inventory_sha256": row_inventory_sha256,
            "cell_count": cell_count,
            "cell_ids_sha256": cell_ids_sha256,
            "cell_to_domain_membership_sha256": (cell_to_domain_membership_sha256),
            "domain_cell_commitments_sha256": domain_cell_commitments_sha256,
            "feature_schema_sha256": feature_schema_sha256_value,
        }
    )
