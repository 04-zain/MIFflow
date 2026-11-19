"""Closed raw-input evidence for training-fitted KRONOS cell normalization."""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import math
import re
import struct

from ._shared import BatchTrainingError, _export_json, _freeze_json

BIOLOGY_FIELDS = frozenset({"marker_name", "marker_full_name", "compartment", "family", "family_desc"})
EVIDENCE_FIELD = "deferred_feature_evidence"
_FIELDS = frozenset({"schema", "source_intent_sha256", "automatic_marker_names", "cell_input_sha256"})
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_AUTOMATIC_SOURCE = re.compile(r"kronos-native-tissue-sample-moments/v1:sha256:[0-9a-f]{64}\Z")


def canonical_input_digest(value: object) -> str:
    """Mirror MifKronosCellFeatures.digestValue, including UTF-8 key order."""
    digest = hashlib.sha256()

    def add(item):
        if item is None:
            digest.update(b"n")
        elif type(item) is bool:
            digest.update(b"t" if item else b"f")
        elif type(item) in (int, float):
            try:
                number = float(item)
            except (OverflowError, ValueError) as exc:
                raise BatchTrainingError("Input identity contains an invalid number") from exc
            if not math.isfinite(number):
                raise BatchTrainingError("Input identity contains a nonfinite number")
            digest.update(b"d" + struct.pack(">d", number))
        elif type(item) is str:
            encoded = item.encode("utf-8")
            digest.update(b"s" + struct.pack(">I", len(encoded)) + encoded)
        elif isinstance(item, (list, tuple)):
            digest.update(b"l" + struct.pack(">I", len(item)))
            for child in item:
                add(child)
        elif isinstance(item, Mapping) and all(type(key) is str for key in item):
            digest.update(b"m" + struct.pack(">I", len(item)))
            for key in sorted(item, key=lambda key: key.encode("utf-8")):
                add(key)
                add(item[key])
        else:
            raise BatchTrainingError("Input identity contains unsupported values")
    add(value)
    return digest.hexdigest()


def _source_mapping(source):
    from .qupath_kronos_cell import KronosCellFeatureSource
    if source is None:
        return None
    if type(source) is not KronosCellFeatureSource:
        source = KronosCellFeatureSource.from_mapping(source)
    return source.to_mapping()


def pending_marker_names(source) -> tuple[str, ...]:
    source = _source_mapping(source)
    if source is None:
        return ()
    rows = source["encoder"]["additional_markers"]
    names = {row["marker_name"] for row in rows if set(row) == BIOLOGY_FIELDS}
    return tuple(name for name in source["encoder"]["marker_names"] if name in names)


def source_intent_sha256(source, automatic_names) -> str:
    source = _source_mapping(source)
    if source is None or not isinstance(automatic_names, (list, tuple)) or not automatic_names:
        raise BatchTrainingError("Deferred cell inputs require an explicit KRONOS source and automatic markers")
    names = tuple(automatic_names)
    if any(type(name) is not str for name in names) or len(set(names)) != len(names):
        raise BatchTrainingError("Deferred automatic marker names must be distinct strings")
    encoder = source["encoder"]
    rows = encoder["additional_markers"]
    if (names != tuple(name for name in encoder["marker_names"] if name in names)
            or sum(row["marker_name"] in names for row in rows) != len(names)
            or not set(pending_marker_names(source)).issubset(names)):
        raise BatchTrainingError("Deferred marker identities differ from the source")
    projected = []
    for row in rows:
        if row["marker_name"] not in names:
            projected.append(row)
            continue
        if not (set(row) == BIOLOGY_FIELDS or (
            set(row) == BIOLOGY_FIELDS | {"mean", "std", "statistics_source"}
            and type(row["statistics_source"]) is str
            and _AUTOMATIC_SOURCE.fullmatch(row["statistics_source"])
        )):
            raise BatchTrainingError("Deferred inputs cannot replace explicit marker statistics")
        projected.append({key: value for key, value in row.items() if key in BIOLOGY_FIELDS})
    return canonical_input_digest({**source, "encoder": {**encoder, "additional_markers": projected}})


def validate_deferred_evidence(value, cell_ids, *, source=None, require_pending=False):
    """Check the exact inventory; optionally bind it to an authenticated source."""
    ids = tuple(cell_ids)
    if len(set(ids)) != len(ids) or any(type(item) is not str or not item for item in ids):
        raise BatchTrainingError("Deferred cell inventory requires distinct stable IDs")
    if not isinstance(value, Mapping) or set(value) != _FIELDS:
        raise BatchTrainingError("Deferred cell evidence fields are incomplete or unknown")
    names = value["automatic_marker_names"]
    hashes = value["cell_input_sha256"]
    if (value["schema"] != "kronos-cell-input-evidence/v1"
            or type(value["source_intent_sha256"]) is not str
            or not _SHA.fullmatch(value["source_intent_sha256"])
            or not isinstance(names, (list, tuple)) or not names
            or any(type(name) is not str or not name for name in names)
            or len(set(names)) != len(names)
            or not isinstance(hashes, Mapping) or set(hashes) != set(ids)
            or any(type(digest) is not str or not _SHA.fullmatch(digest) for digest in hashes.values())):
        raise BatchTrainingError("Deferred cell evidence differs from its exact cell inventory")
    if source is not None:
        if require_pending and tuple(names) != pending_marker_names(source):
            raise BatchTrainingError("Initial deferred evidence requires the source's exact pending markers")
        if value["source_intent_sha256"] != source_intent_sha256(source, names):
            raise BatchTrainingError("Deferred cell source intent differs")
    elif require_pending:
        raise BatchTrainingError("Deferred cell inputs require an authenticated pending source")
    return _freeze_json(value, EVIDENCE_FIELD)


def validate_snapshot_inputs(cells, schema, value):
    """Reject empty or placeholder vectors outside the explicit deferred branch."""
    names = {item.measurement_name for item in schema}
    if value is None:
        if any(set(cell.marker_measurements) != names for cell in cells):
            raise BatchTrainingError("Snapshot measurements differ from the ordered feature schema")
        return None
    from .qupath_kronos_cell import kronos_cell_measurements
    if [item.to_mapping() for item in schema] != kronos_cell_measurements():
        raise BatchTrainingError("Deferred inputs require the exact KRONOS cell feature schema")
    if any(cell.marker_measurements for cell in cells):
        raise BatchTrainingError("Deferred cells must not contain precomputed or placeholder measurements")
    return validate_deferred_evidence(value, (cell.cell_id for cell in cells))


def project_deferred_evidence(value, cell_ids):
    if value is None:
        return {}
    ids = tuple(cell_ids)
    result = _export_json(value)
    if not set(ids).issubset(result["cell_input_sha256"]):
        raise BatchTrainingError("Deferred projection contains unknown cells")
    result["cell_input_sha256"] = {key: result["cell_input_sha256"][key] for key in ids}
    validate_deferred_evidence(result, ids)
    return {EVIDENCE_FIELD: result}
