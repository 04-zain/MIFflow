"""Semantic evidence validation over bounded QuPath raw-object exports.

Batch producers and semantic execution reuse selected cell datasets, profile
selectors and file inventories here. Validators preserve population, geometry
and membership authority before those owners accept scientific results."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import hashlib
import json
from math import isfinite
from pathlib import Path
import sqlite3
import tempfile
from typing import Any

from mif_pipeline.cell_analysis.spatial_handoff import (
    semantic_parent_content_key,
    semantic_secondary_content_key,
)
from mif_pipeline.pipeline_core.models import ROIAnalysisError
from mif_pipeline.pipeline_core.config import AnalysisConfig
from mif_pipeline.roi_validation import SemanticProfileV3, SemanticEvidenceSnapshot

# ==============================================================================
#                       Dataset selections and semantic evidence
# ==============================================================================


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _raw_cell_dataset(raw):
    """Open our compact current native selection without decoding cell geometry.

    i) The raw artifact binds the base/result references and native identity summary.
    ii) Our canonical reader admits those immutable descriptors before any requested columns.
    """
    from mif_pipeline.datasets.collection import CellDatasetCollection
    if not isinstance(raw.get("cell_datasets"), list) or "cells" in raw:
        raise ROIAnalysisError("raw cell transport requires current dataset selections")
    return CellDatasetCollection.from_selection(raw["cell_datasets"], raw["cell_scope"], raw["cell_summary"])


def _select_current_raw_dataset(raw, expected_selectors):
    """Select our accepted scientific fields while retaining current sparse edits.

    i) We match the accepted base, geometry revision and scalar component references.
    ii) We retain the native snapshot's sparse checkpoint and discard unrelated
    display/history fields; subsequent reads resolve only the selected science.
    """
    selected = {item["section_id"]: item for item in raw["cell_datasets"]}
    references = []
    for expected in expected_selectors:
        current = selected.get(expected["section_id"])
        if (current is None or any(current[key] != expected[key] for key in
                ("population_id", "cell_dataset", "geometry_revision"))
                or any(current["components"].get(field) != scopes
                    for field, scopes in expected["components"].items())):
            raise ROIAnalysisError("raw scientific selection differs from its accepted owners")
        references.append({**expected, "cell_edits": current["cell_edits"]})
    if set(selected) != {item["section_id"] for item in references}:
        raise ROIAnalysisError("raw scientific section selection differs")
    from mif_pipeline.datasets.collection import CellDatasetCollection
    return CellDatasetCollection.from_selection(references, raw["cell_scope"], raw["cell_summary"])


def _iter_raw_cell_values(raw, *, columns, measurement_names=()):
    """Read our requested scalar scientific values in bounded Arrow groups.

    i) We decode only requested categorical vocabularies and selected intensities.
    ii) Population and generation metadata comes from the selected immutable scope;
    geometry remains unread until an actual spatial operation requests it.
    """
    dataset = _raw_cell_dataset(raw)
    scopes = {item["section_id"]: item["population_id"] for item in raw["cell_datasets"]}
    columns = list(dict.fromkeys(["cell_id", "section_id", "analysis_domain_id", *columns]))
    required = [name for name in columns if name in dataset.schema.names]
    for name in measurement_names:
        field = "measurement:" + name
        if field not in dataset.schema.names:
            dataset.close()
            raise ROIAnalysisError(f"Selected measurement is absent: {name}")
        required.append(field)
        if "present:" + field in dataset.schema.names:
            required.append("present:" + field)
    try:
        for batch in dataset.iter_batches(columns=required):
            rows = batch.to_pylist()
            for name, dictionary in dataset.dictionaries.items():
                if name not in batch.schema.names:
                    continue
                for row in rows:
                    row[name] = dictionary.decode(row[name]) if isinstance(row[name], list) else dictionary.decode([row[name]])[0]
            for row in rows:
                row.update(slide_id=raw["slide_id"], generation_id=raw["generation_id"],
                    pipeline_run_id=raw["pipeline_run_id"],
                    segmentation_population_id=scopes[row["section_id"]])
                states = row.get("marker_states")
                if isinstance(states, str):
                    row["marker_states"] = json.loads(states)
                row["marker_measurements"] = {name: row["measurement:" + name]
                    for name in measurement_names if row.get("present:measurement:" + name, True)}
                yield row
    finally:
        dataset.close()


class _IncrementalJSON:
    """Decode one JSON value at a time without retaining the containing file."""

    def __init__(self, path: Path, *, chunk_size: int = 64 * 1024) -> None:
        self._stream = path.open(encoding="utf-8")
        self._chunk_size = chunk_size
        self._buffer = ""
        self._position = 0
        self._eof = False
        self._decoder = json.JSONDecoder(object_pairs_hook=self._strict_object_pairs)

    @staticmethod
    def _strict_object_pairs(
        values: list[tuple[str, object]],
    ) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise ROIAnalysisError("streamed JSON contains a duplicate key")
            result[key] = value
        return result

    def close(self) -> None:
        self._stream.close()

    def _fill(self) -> bool:
        if self._position:
            self._buffer = self._buffer[self._position :]
            self._position = 0
        chunk = self._stream.read(self._chunk_size)
        if not chunk:
            self._eof = True
            return False
        self._buffer += chunk
        return True

    def _skip_space(self) -> None:
        while True:
            while self._position < len(self._buffer) and self._buffer[self._position].isspace():
                self._position += 1
            if self._position < len(self._buffer) or not self._fill():
                return

    def peek(self) -> str:
        self._skip_space()
        if self._position >= len(self._buffer):
            raise ROIAnalysisError("streamed JSON ended unexpectedly")
        return self._buffer[self._position]

    def expect(self, token: str) -> None:
        if self.peek() != token:
            raise ROIAnalysisError(f"streamed JSON expected {token!r}")
        self._position += 1

    def value(self) -> object:
        self._skip_space()
        while True:
            try:
                value, end = self._decoder.raw_decode(self._buffer, self._position)
            except json.JSONDecodeError as exc:
                if not self._eof and self._fill():
                    continue
                raise ROIAnalysisError(f"streamed JSON is invalid: {exc}") from exc
            self._position = end
            return value

    def array(self, consume: Callable[[object], None]) -> None:
        self.expect("[")
        if self.peek() == "]":
            self._position += 1
            return
        while True:
            consume(self.value())
            delimiter = self.peek()
            if delimiter == "]":
                self._position += 1
                return
            self.expect(",")

    def mapping(self, consume: Callable[[str, object], None]) -> None:
        self.expect("{")
        if self.peek() == "}":
            self._position += 1
            return
        while True:
            key = self.value()
            if not isinstance(key, str):
                raise ROIAnalysisError("streamed JSON mapping key is invalid")
            self.expect(":")
            consume(key, self.value())
            delimiter = self.peek()
            if delimiter == "}":
                self._position += 1
                return
            self.expect(",")

    def finish(self) -> None:
        self._skip_space()
        while self._position >= len(self._buffer) and not self._eof:
            if not self._fill():
                break
            self._skip_space()
        if self._position < len(self._buffer):
            raise ROIAnalysisError("streamed JSON has trailing content")

def _stream_json_object(
    path: Path,
    *,
    array_consumers: Mapping[str, Callable[[object], None]] | None = None,
    mapping_consumers: Mapping[str, Callable[[str, object], None]] | None = None,
    required_fields: Sequence[str] = (),
) -> dict[str, object]:
    array_consumers = {} if array_consumers is None else array_consumers
    mapping_consumers = {} if mapping_consumers is None else mapping_consumers
    reader = _IncrementalJSON(path)
    scalars: dict[str, object] = {}
    seen: set[str] = set()
    try:
        reader.expect("{")
        if reader.peek() == "}":
            reader.expect("}")
        else:
            while True:
                key = reader.value()
                if not isinstance(key, str):
                    raise ROIAnalysisError("streamed JSON object key is invalid")
                if key in seen:
                    raise ROIAnalysisError("streamed JSON object contains a duplicate key")
                seen.add(key)
                reader.expect(":")
                if key in array_consumers:
                    reader.array(array_consumers[key])
                elif key in mapping_consumers:
                    reader.mapping(mapping_consumers[key])
                else:
                    scalars[key] = reader.value()
                delimiter = reader.peek()
                if delimiter == "}":
                    reader.expect("}")
                    break
                reader.expect(",")
        reader.finish()
        missing = set(required_fields) - seen
        if missing:
            raise ROIAnalysisError(
                "streamed JSON is missing required containers: " + ", ".join(sorted(missing))
            )
    finally:
        reader.close()
    return scalars


def validate_semantic_raw_inventory_file(
    raw_objects_path: Path,
    *,
    raw_export_sha256: str,
    slide_id: str,
    section_id: str,
    analysis_domain_id: str,
    tissue_generation_id: str,
    pipeline_run_id: str,
) -> dict[str, tuple[dict[str, object], ...]]:
    """Stream and project one checksummed domain's semantic-candidate ROIs.

    The required mapping shape includes ``roi_id``, ``path_object_id``, ``parent_roi_id``,
    ``geometry_digest``. Field checks are applied before the validated value is returned or
    passed to its next constructor."""
    if not isinstance(raw_objects_path, Path) or not _is_sha256(raw_export_sha256):
        raise ROIAnalysisError("semantic raw export descriptor is invalid")
    try:
        if _file_sha256(raw_objects_path) != raw_export_sha256:
            raise ROIAnalysisError("semantic raw export digest does not match")
    except OSError as exc:
        raise ROIAnalysisError("semantic raw export is unreadable") from exc

    primary: list[dict[str, object]] = []
    candidates: list[dict[str, object]] = []

    def consume(value: object) -> None:
        if not isinstance(value, Mapping):
            raise ROIAnalysisError("semantic raw ROI inventory row is invalid")
        if (
            value.get("section_id") != section_id
            or value.get("analysis_domain_id") != analysis_domain_id
        ):
            return
        if (
            value.get("generation_id") != tissue_generation_id
            or value.get("pipeline_run_id") != pipeline_run_id
        ):
            raise ROIAnalysisError("semantic raw ROI inventory lineage is stale")
        level = value.get("roi_level")
        if level not in {"primary", "secondary"}:
            return
        required = (
            "roi_id",
            "path_object_id",
            "parent_roi_id",
            "geometry_digest",
        )
        if any(not isinstance(value.get(name), str) or not value[name] for name in required):
            raise ROIAnalysisError("semantic raw ROI inventory identity is incomplete")
        geometry_sha256 = value["geometry_digest"]
        if not _is_sha256(geometry_sha256):
            raise ROIAnalysisError("semantic raw ROI geometry digest is invalid")
        row: dict[str, object] = {
            "roi_id": value["roi_id"],
            "path_object_id": value["path_object_id"],
            "parent_roi_id": value["parent_roi_id"],
            "geometry_sha256": geometry_sha256,
        }
        if level == "primary":
            row["primary_class_id"] = value.get("primary_class_id")
            primary.append(row)
            return
        candidate_status = value.get("candidate_status")
        if candidate_status not in {"candidate", "other"}:
            raise ROIAnalysisError("semantic secondary candidate_status is invalid")
        structure_id = value.get("structure_id")
        if (
            value.get("pipeline_stage") != "secondary_classifier"
            or not isinstance(structure_id, str)
            or not structure_id
        ):
            raise ROIAnalysisError("semantic secondary inventory identity is incomplete")
        if candidate_status == "other":
            return
        parent_content_key = semantic_parent_content_key(
            analysis_domain_id=analysis_domain_id,
            parent_roi_id=str(value["parent_roi_id"]),
        )
        row.update(
            {
                "structure_id": structure_id,
                "parent_content_key": parent_content_key,
                "content_key": semantic_secondary_content_key(
                    analysis_domain_id=analysis_domain_id,
                    roi_id=str(value["roi_id"]),
                    path_object_id=str(value["path_object_id"]),
                    structure_id=structure_id,
                    parent_content_key=parent_content_key,
                    geometry_sha256=str(geometry_sha256),
                ),
            }
        )
        candidates.append(row)

    identity = _stream_json_object(
        raw_objects_path,
        array_consumers={
            "rois": consume,
            "tissue_sections": lambda _row: None,
        },
        required_fields=("rois", "tissue_sections"),
    )
    if (
        identity.get("schema_version") != "qupath-raw-objects/v1"
        or identity.get("slide_id") != slide_id
        or identity.get("generation_id") != tissue_generation_id
        or identity.get("coordinate_system") != "global_level0"
    ):
        raise ROIAnalysisError("semantic raw ROI inventory scope is invalid")
    for label, rows in (("primary", primary), ("secondary", candidates)):
        roi_ids = tuple(str(item["roi_id"]) for item in rows)
        if len(set(roi_ids)) != len(roi_ids):
            raise ROIAnalysisError(f"semantic {label} inventory duplicates an ROI ID")
    return {
        "primary": tuple(sorted(primary, key=lambda item: str(item["roi_id"]))),
        "secondary": tuple(sorted(candidates, key=lambda item: str(item["roi_id"]))),
    }


def _assemble_semantic_evidence_files(
    raw_objects_path: Path,
    membership_artifact_path: Path,
    *,
    raw_export_sha256: str,
    membership_artifact_sha256: str,
    physical_calibration: Mapping[str, object],
    phenotype_view: Mapping[str, object],
) -> tuple[SemanticEvidenceSnapshot, ...]:
    """Assemble scoped evidence with an incremental parser and disk-backed join."""
    if not _is_sha256(raw_export_sha256) or not _is_sha256(membership_artifact_sha256):
        raise ROIAnalysisError("semantic file transport digest is invalid")
    if not all(
        isinstance(physical_calibration.get(key), (int, float))
        and not isinstance(physical_calibration[key], bool)
        and float(physical_calibration[key]) > 0
        for key in ("x_um_per_px", "y_um_per_px")
    ):
        raise ROIAnalysisError("semantic evidence requires positive two-axis physical calibration")
    if any(
        not isinstance(phenotype_view.get(key), str) or not phenotype_view[key]
        for key in ("mode", "generation_id", "inventory_sha256")
    ):
        raise ROIAnalysisError("semantic evidence phenotype view is incomplete")
    if not _is_sha256(str(phenotype_view["inventory_sha256"])):
        raise ROIAnalysisError("semantic evidence phenotype inventory digest is invalid")

    secondary_rois: dict[str, Mapping[str, object]] = {}
    primaries: list[dict[str, object]] = []
    candidate_evidence: dict[str, Mapping[str, object]] = {}
    relation_candidate_ids: set[str] = set()
    evidence_selectors: list[tuple[str, dict[str, str]]] = []
    with tempfile.TemporaryDirectory(prefix="mif-semantic-transport-") as directory:
        database = sqlite3.connect(Path(directory) / "semantic.sqlite3")
        try:
            database.executescript("""
                CREATE TABLE cells (
                    cell_id TEXT PRIMARY KEY, slide_id TEXT, section_id TEXT,
                    analysis_domain_id TEXT, generation_id TEXT, pipeline_run_id TEXT,
                    population_id TEXT, phenotype TEXT, phenotype_generation_id TEXT,
                    cell_sort_key TEXT NOT NULL
                );
                CREATE TABLE markers (cell_id TEXT, marker_id TEXT, marker_state TEXT);
                CREATE TABLE measurements (cell_id TEXT, measurement_id TEXT, value REAL);
                CREATE TABLE relations (
                    candidate_id TEXT, cell_id TEXT, has_valid_nucleus INTEGER,
                    PRIMARY KEY (candidate_id, cell_id)
                );
            """)

            def consume_roi(value: object) -> None:
                if not isinstance(value, Mapping):
                    raise ROIAnalysisError("streamed raw ROI row is invalid")
                roi_id = value.get("roi_id")
                if value.get("roi_level") == "secondary":
                    if value.get("candidate_status") not in {"candidate", "other"}:
                        raise ROIAnalysisError("streamed secondary ROI candidate_status is invalid")
                    if not isinstance(roi_id, str) or not roi_id or roi_id in secondary_rois:
                        raise ROIAnalysisError("streamed secondary ROI ID is invalid")
                    secondary_rois[roi_id] = dict(value)
                elif value.get("roi_level") == "primary":
                    primaries.append(
                        {
                            "roi_id": roi_id,
                            "geometry_digest": value.get("geometry_digest"),
                            "primary_class_id": value.get("primary_class_id"),
                            "section_id": value.get("section_id"),
                            "analysis_domain_id": value.get("analysis_domain_id"),
                        }
                    )

            def consume_cell(value: object) -> None:
                if not isinstance(value, Mapping) or not isinstance(value.get("cell_id"), str):
                    raise ROIAnalysisError("streamed raw cell row is invalid")
                try:
                    database.execute(
                        "INSERT INTO cells VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            value["cell_id"],
                            value.get("slide_id"),
                            value.get("section_id"),
                            value.get("analysis_domain_id"),
                            value.get("generation_id"),
                            value.get("pipeline_run_id"),
                            value.get("segmentation_population_id"),
                            value.get("primary_phenotype"),
                            value.get("phenotyping_generation_id"),
                            _semantic_inventory_sort_key(value["cell_id"]),
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ROIAnalysisError("streamed raw cells duplicate a stable ID") from exc
                states = value.get("marker_states", {})
                if isinstance(states, Mapping):
                    database.executemany(
                        "INSERT INTO markers VALUES (?, ?, ?)",
                        (
                            (value["cell_id"], marker, state)
                            for marker, state in states.items()
                            if isinstance(marker, str) and isinstance(state, str)
                        ),
                    )
                measurements = value.get("marker_measurements", {})
                if isinstance(measurements, Mapping):
                    database.executemany(
                        "INSERT INTO measurements VALUES (?, ?, ?)",
                        (
                            (value["cell_id"], name, float(measurement))
                            for name, measurement in measurements.items()
                            if isinstance(name, str)
                            and isinstance(measurement, (int, float))
                            and not isinstance(measurement, bool)
                            and isfinite(float(measurement))
                        ),
                    )

            raw_identity = _stream_json_object(
                raw_objects_path,
                array_consumers={
                    "rois": consume_roi,
                    "tissue_sections": lambda _row: None,
                },
                required_fields=("rois", "tissue_sections"),
            )
            if (
                raw_identity.get("schema_version") != "qupath-raw-objects/v1"
                or raw_identity.get("coordinate_system") != "global_level0"
            ):
                raise ROIAnalysisError("streamed semantic raw export identity is invalid")

            def consume_relation(value: object) -> None:
                if not isinstance(value, Mapping):
                    raise ROIAnalysisError("streamed semantic relation row is invalid")
                candidate_id, cell_id = value.get("candidate_roi_id"), value.get("cell_id")
                if candidate_id not in secondary_rois or not isinstance(cell_id, str):
                    raise ROIAnalysisError(
                        "streamed semantic relation references a stale candidate or cell"
                    )
                try:
                    database.execute(
                        "INSERT INTO relations VALUES (?, ?, ?)",
                        (candidate_id, cell_id, int(bool(value.get("has_valid_nucleus")))),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ROIAnalysisError(
                        "semantic candidate relation duplicates a stable cell"
                    ) from exc
                relation_candidate_ids.add(str(candidate_id))

            def consume_candidate(candidate_id: str, value: object) -> None:
                if not isinstance(value, Mapping):
                    raise ROIAnalysisError("semantic candidate geometry facts are invalid")
                candidate_evidence[candidate_id] = dict(value)

            relation_identity = _stream_json_object(
                membership_artifact_path,
                array_consumers={
                    "relations": consume_relation,
                    "evidence_selectors": lambda value: _consume_evidence_selector(
                        value, evidence_selectors
                    ),
                },
                mapping_consumers={"candidate_evidence": consume_candidate},
                required_fields=(
                    "relations",
                    "evidence_selectors",
                    "candidate_evidence",
                ),
            )
            database.commit()
            required_identity = (
                "slide_id",
                "section_id",
                "analysis_domain_id",
                "generation_id",
                "pipeline_run_id",
                "segmentation_population_id",
            )
            if relation_identity.get("schema_version") != "semantic-candidate-membership/v1":
                raise ROIAnalysisError("semantic candidate membership schema is invalid")
            if relation_identity.get("raw_export_sha256") != raw_export_sha256:
                raise ROIAnalysisError(
                    "semantic candidate membership is bound to another raw export"
                )
            if not isinstance(relation_identity.get("phenotype_view"), Mapping) or dict(
                relation_identity["phenotype_view"]
            ) != dict(phenotype_view):
                raise ROIAnalysisError("semantic candidate membership phenotype view is stale")
            if any(
                not isinstance(relation_identity.get(key), str) or not relation_identity[key]
                for key in required_identity
            ):
                raise ROIAnalysisError("semantic candidate membership identity is incomplete")
            if relation_identity["slide_id"] != raw_identity.get("slide_id") or relation_identity[
                "generation_id"
            ] != raw_identity.get("generation_id"):
                raise ROIAnalysisError("semantic candidate membership crosses raw export identity")
            # i) Our admitted selectors choose only the intensities required
            # by this semantic operation. ii) We stream the selected scalar rows
            # once into the existing disk join, then check exact relation IDs.
            names = tuple(sorted({selector["measurement_id"]
                for kind, selector in evidence_selectors
                if kind in {"cell_measurement_aggregate", "population_marker_fraction"}}))
            for row in _iter_raw_cell_values(raw_identity,
                    columns=("primary_phenotype", "phenotyping_generation_id", "marker_states"),
                    measurement_names=names):
                consume_cell(row)
            database.commit()
            if database.execute("""SELECT 1 FROM relations r
                    LEFT JOIN cells c ON r.cell_id = c.cell_id
                    WHERE c.cell_id IS NULL LIMIT 1""").fetchone() is not None:
                raise ROIAnalysisError("streamed semantic relation references a stale candidate or cell")
            if (
                database.execute(
                    """SELECT 1 FROM cells WHERE
                       slide_id IS NOT ? OR section_id IS NOT ? OR analysis_domain_id IS NOT ?
                       OR generation_id IS NOT ? OR pipeline_run_id IS NOT ? OR population_id IS NOT ?
                   LIMIT 1""",
                    (
                        relation_identity["slide_id"],
                        relation_identity["section_id"],
                        relation_identity["analysis_domain_id"],
                        relation_identity["generation_id"],
                        relation_identity["pipeline_run_id"],
                        relation_identity["segmentation_population_id"],
                    ),
                ).fetchone()
                is not None
            ):
                raise ROIAnalysisError(
                    "semantic raw export contains a stale scoped cell population"
                )
            authority_ids = set(candidate_evidence)
            status_candidate_ids = {
                candidate_id
                for candidate_id, candidate in secondary_rois.items()
                if candidate.get("candidate_status") == "candidate"
            }
            if authority_ids != status_candidate_ids or not relation_candidate_ids <= authority_ids:
                raise ROIAnalysisError(
                    "semantic candidate membership lacks the complete candidate inventory"
                )
            candidates = {
                candidate_id: secondary_rois[candidate_id] for candidate_id in authority_ids
            }
            if relation_identity.get("candidate_count") != len(candidates):
                raise ROIAnalysisError(
                    "semantic candidate membership lacks the complete candidate inventory"
                )
            if relation_identity.get("candidate_inventory_sha256") != _candidate_inventory_digest(
                candidates
            ):
                raise ROIAnalysisError("semantic candidate membership candidate inventory is stale")

            primary_inventory = sorted(
                (
                    {key: row[key] for key in ("roi_id", "geometry_digest", "primary_class_id")}
                    for row in primaries
                    if row["section_id"] == relation_identity["section_id"]
                    and row["analysis_domain_id"] == relation_identity["analysis_domain_id"]
                ),
                key=lambda row: _semantic_inventory_sort_key(row["roi_id"]),
            )
            primary_digest = _mapping_digest({"primary_geometry_inventory/v1": primary_inventory})
            snapshots = []
            for candidate_id in sorted(candidates, key=_semantic_inventory_sort_key):
                snapshots.append(
                    _snapshot_from_sqlite(
                        database=database,
                        candidate_id=candidate_id,
                        candidate=candidates[candidate_id],
                        facts=candidate_evidence[candidate_id],
                        selectors=evidence_selectors,
                        relation_identity=relation_identity,
                        raw_export_sha256=raw_export_sha256,
                        membership_artifact_sha256=membership_artifact_sha256,
                        physical_calibration=physical_calibration,
                        phenotype_view=phenotype_view,
                        primary_geometry_inventory_sha256=primary_digest,
                    )
                )
            return tuple(snapshots)
        finally:
            database.close()


def _consume_evidence_selector(value: object, output: list[tuple[str, dict[str, str]]]) -> None:
    if (
        not isinstance(value, Mapping)
        or not isinstance(value.get("type"), str)
        or not isinstance(value.get("selector"), Mapping)
    ):
        raise ROIAnalysisError("semantic evidence selector is invalid")
    selector = dict(value["selector"])
    if any(not isinstance(key, str) or not isinstance(item, str) for key, item in selector.items()):
        raise ROIAnalysisError("semantic evidence selector is invalid")
    output.append((str(value["type"]), selector))


def _candidate_inventory_digest(
    candidates: Mapping[str, Mapping[str, object]],
) -> str:
    inventory = [
        {
            "candidate_roi_id": candidate_id,
            "candidate_path_object_id": candidates[candidate_id].get("path_object_id"),
            "geometry_digest": candidates[candidate_id].get("geometry_digest"),
        }
        for candidate_id in sorted(candidates, key=_semantic_inventory_sort_key)
    ]
    return hashlib.sha256(
        json.dumps(
            inventory,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _validate_semantic_candidate_membership_files(
    raw_objects_path: Path,
    membership_artifact_path: Path,
    *,
    raw_export_sha256: str,
    membership_artifact_sha256: str,
    expected_scope: Mapping[str, object],
    physical_calibration: Mapping[str, object],
    phenotype_view: Mapping[str, object],
) -> tuple[dict[str, object], tuple[SemanticEvidenceSnapshot, ...]]:
    """Reopen one producer artifact and return its exact candidate authority.

    The complete transport is validated through the same disk-backed join used
    by the semantic workflow.  The second bounded metadata pass retains only
    the closed scope and candidate identity bridge needed by provenance.
    """
    try:
        if (
            _file_sha256(raw_objects_path) != raw_export_sha256
            or _file_sha256(membership_artifact_path) != membership_artifact_sha256
        ):
            raise ROIAnalysisError("semantic candidate membership descriptor bytes are stale")
    except OSError as exc:
        raise ROIAnalysisError("semantic candidate membership authority is unreadable") from exc

    candidate_evidence: dict[str, dict[str, str]] = {}

    def consume_candidate(candidate_id: str, value: object) -> None:
        if (
            not isinstance(candidate_id, str)
            or not candidate_id.strip()
            or candidate_id in candidate_evidence
            or not isinstance(value, Mapping)
        ):
            raise ROIAnalysisError("semantic candidate membership candidate identity is invalid")
        path_object_id = value.get("candidate_path_object_id")
        geometry_sha256 = value.get("geometry_digest")
        if (
            not isinstance(path_object_id, str)
            or not path_object_id.strip()
            or not _is_sha256(geometry_sha256)
        ):
            raise ROIAnalysisError("semantic candidate membership candidate identity is invalid")
        candidate_evidence[candidate_id] = {
            "path_object_id": path_object_id,
            "geometry_sha256": str(geometry_sha256),
        }

    identity = _stream_json_object(
        membership_artifact_path,
        array_consumers={
            "relations": lambda _value: None,
            "evidence_selectors": lambda _value: None,
        },
        mapping_consumers={"candidate_evidence": consume_candidate},
        required_fields=(
            "relations",
            "evidence_selectors",
            "candidate_evidence",
        ),
    )
    expected_fields = {
        "schema_version",
        "slide_id",
        "section_id",
        "analysis_domain_id",
        "generation_id",
        "pipeline_run_id",
        "segmentation_population_id",
        "raw_export_path",
        "raw_export_sha256",
        "phenotype_view",
        "candidate_count",
        "candidate_inventory_sha256",
    }
    if (
        set(identity) != expected_fields
        or identity.get("schema_version") != "semantic-candidate-membership/v1"
    ):
        raise ROIAnalysisError("semantic candidate membership authority fields are invalid")
    required_scope = (
        "slide_id",
        "section_id",
        "analysis_domain_id",
        "generation_id",
        "pipeline_run_id",
        "segmentation_population_id",
        "raw_export_path",
    )
    if set(expected_scope) != set(required_scope) or any(
        not isinstance(expected_scope.get(field), str) or not str(expected_scope[field]).strip()
        for field in required_scope
    ):
        raise ROIAnalysisError("semantic candidate membership expected scope is invalid")
    if any(
        not isinstance(identity.get(field), str) or not str(identity[field]).strip()
        for field in required_scope
    ):
        raise ROIAnalysisError("semantic candidate membership authority scope is invalid")
    try:
        expected_raw_path = (
            Path(str(expected_scope["raw_export_path"])).expanduser().resolve(strict=False)
        )
        claimed_raw_path = Path(str(identity["raw_export_path"])).expanduser().resolve(strict=False)
        actual_raw_path = raw_objects_path.expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ROIAnalysisError("semantic candidate membership raw path is invalid") from exc
    if (
        any(
            identity.get(field) != expected_scope[field]
            for field in required_scope
            if field != "raw_export_path"
        )
        or claimed_raw_path != expected_raw_path
        or expected_raw_path != actual_raw_path
        or identity.get("raw_export_sha256") != raw_export_sha256
        or not isinstance(identity.get("phenotype_view"), Mapping)
        or dict(identity["phenotype_view"]) != dict(phenotype_view)
        or isinstance(identity.get("candidate_count"), bool)
        or identity.get("candidate_count") != len(candidate_evidence)
    ):
        raise ROIAnalysisError("semantic candidate membership authority binding is stale")
    digest_candidates = {
        candidate_id: {
            "path_object_id": evidence["path_object_id"],
            "geometry_digest": evidence["geometry_sha256"],
        }
        for candidate_id, evidence in candidate_evidence.items()
    }
    if identity.get("candidate_inventory_sha256") != _candidate_inventory_digest(digest_candidates):
        raise ROIAnalysisError("semantic candidate membership candidate inventory is stale")
    snapshots = _assemble_semantic_evidence_files(
        raw_objects_path,
        membership_artifact_path,
        raw_export_sha256=raw_export_sha256,
        membership_artifact_sha256=membership_artifact_sha256,
        physical_calibration=physical_calibration,
        phenotype_view=phenotype_view,
    )
    snapshot_evidence = {
        snapshot.candidate_roi_id: {
            "path_object_id": snapshot.values.get("candidate_path_object_id"),
            "geometry_sha256": snapshot.geometry_digest,
        }
        for snapshot in snapshots
    }
    if snapshot_evidence != candidate_evidence:
        raise ROIAnalysisError("semantic candidate membership candidate identity is stale")
    path_object_ids = {evidence["path_object_id"] for evidence in candidate_evidence.values()}
    if len(path_object_ids) != len(candidate_evidence):
        raise ROIAnalysisError("semantic candidate membership PathObject IDs are duplicated")
    return (
        {
            **identity,
            "artifact_sha256": membership_artifact_sha256,
            "candidates": candidate_evidence,
        },
        snapshots,
    )


def _validated_semantic_candidate_membership_authority(
    raw_objects_path: Path,
    membership_artifact_path: Path,
    *,
    raw_export_sha256: str,
    membership_artifact_sha256: str,
    expected_scope: Mapping[str, object],
    physical_calibration: Mapping[str, object],
    phenotype_view: Mapping[str, object],
) -> dict[str, object]:
    """Return the bounded authority projection used by provenance."""
    authority, _snapshots = _validate_semantic_candidate_membership_files(
        raw_objects_path,
        membership_artifact_path,
        raw_export_sha256=raw_export_sha256,
        membership_artifact_sha256=membership_artifact_sha256,
        expected_scope=expected_scope,
        physical_calibration=physical_calibration,
        phenotype_view=phenotype_view,
    )
    return authority


def validate_semantic_evidence_files(
    raw_objects_path: Path,
    membership_artifact_path: Path,
    *,
    raw_export_sha256: str,
    membership_artifact_sha256: str,
    slide_id: str,
    section_id: str,
    analysis_domain_id: str,
    tissue_generation_id: str,
    pipeline_run_id: str,
    segmentation_population_id: str,
    physical_calibration: Mapping[str, object],
    phenotype_view: Mapping[str, object],
) -> tuple[SemanticEvidenceSnapshot, ...]:
    """Validate exact file-backed candidate authority and return snapshots."""
    _authority, snapshots = _validate_semantic_candidate_membership_files(
        raw_objects_path,
        membership_artifact_path,
        raw_export_sha256=raw_export_sha256,
        membership_artifact_sha256=membership_artifact_sha256,
        expected_scope={
            "slide_id": slide_id,
            "section_id": section_id,
            "analysis_domain_id": analysis_domain_id,
            "generation_id": tissue_generation_id,
            "pipeline_run_id": pipeline_run_id,
            "segmentation_population_id": segmentation_population_id,
            "raw_export_path": str(raw_objects_path.resolve(strict=False)),
        },
        physical_calibration=physical_calibration,
        phenotype_view=phenotype_view,
    )
    return snapshots


def _snapshot_from_sqlite(
    *,
    database: sqlite3.Connection,
    candidate_id: str,
    candidate: Mapping[str, object],
    facts: Mapping[str, object],
    selectors: Sequence[tuple[str, dict[str, str]]],
    relation_identity: Mapping[str, object],
    raw_export_sha256: str,
    membership_artifact_sha256: str,
    physical_calibration: Mapping[str, object],
    phenotype_view: Mapping[str, object],
    primary_geometry_inventory_sha256: str,
) -> SemanticEvidenceSnapshot:
    required_lineage = {
        "geometry_digest",
        "member_cell_inventory_sha256",
        "physical_calibration",
        "primary_geometry_inventory_sha256",
    }
    if not required_lineage <= set(facts):
        raise ROIAnalysisError("semantic candidate lineage evidence is incomplete")
    if (
        candidate.get("slide_id") != relation_identity["slide_id"]
        or candidate.get("section_id") != relation_identity["section_id"]
        or candidate.get("analysis_domain_id") != relation_identity["analysis_domain_id"]
        or candidate.get("generation_id") != relation_identity["generation_id"]
        or candidate.get("pipeline_run_id") != relation_identity["pipeline_run_id"]
    ):
        raise ROIAnalysisError("semantic candidate crosses the scoped raw inventory")
    geometry_digest = candidate.get("geometry_digest")
    if not isinstance(geometry_digest, str) or not _is_sha256(geometry_digest):
        geometry = candidate.get("geometry")
        geometry_digest = _mapping_digest(dict(geometry) if isinstance(geometry, Mapping) else {})
    if facts.get("geometry_digest") != geometry_digest:
        raise ROIAnalysisError("semantic candidate geometry digest is stale")
    if (
        not isinstance(candidate.get("path_object_id"), str)
        or not str(candidate["path_object_id"]).strip()
        or facts.get("candidate_path_object_id") != candidate.get("path_object_id")
    ):
        raise ROIAnalysisError("semantic candidate source PathObject identity is stale")
    if not isinstance(facts.get("physical_calibration"), Mapping) or dict(
        facts["physical_calibration"]
    ) != dict(physical_calibration):
        raise ROIAnalysisError("semantic candidate physical calibration is stale")
    if facts.get("primary_geometry_inventory_sha256") != primary_geometry_inventory_sha256:
        raise ROIAnalysisError("semantic candidate primary geometry inventory is stale")

    invalid_member = database.execute(
        """SELECT 1 FROM relations r JOIN cells c ON c.cell_id = r.cell_id
           WHERE r.candidate_id = ? AND (
               c.slide_id IS NOT ? OR c.section_id IS NOT ? OR c.analysis_domain_id IS NOT ?
               OR c.generation_id IS NOT ? OR c.pipeline_run_id IS NOT ? OR c.population_id IS NOT ?
           ) LIMIT 1""",
        (
            candidate_id,
            relation_identity["slide_id"],
            relation_identity["section_id"],
            relation_identity["analysis_domain_id"],
            relation_identity["generation_id"],
            relation_identity["pipeline_run_id"],
            relation_identity["segmentation_population_id"],
        ),
    ).fetchone()
    if invalid_member is not None:
        raise ROIAnalysisError("semantic candidate member crosses scoped cell lineage")
    generations = {
        row[0]
        for row in database.execute(
            """SELECT DISTINCT c.phenotype_generation_id FROM relations r
               JOIN cells c ON c.cell_id = r.cell_id WHERE r.candidate_id = ?""",
            (candidate_id,),
        )
    }
    if generations and generations != {phenotype_view["generation_id"]}:
        raise ROIAnalysisError("semantic candidate member phenotype generation is stale")

    member_digest_builder = hashlib.sha256()
    member_digest_builder.update(b"[")
    member_count = 0
    nucleus_count = 0
    for cell_id, has_valid_nucleus in database.execute(
        """SELECT r.cell_id, r.has_valid_nucleus FROM relations r
           JOIN cells c ON c.cell_id = r.cell_id
           WHERE r.candidate_id = ? ORDER BY c.cell_sort_key, r.cell_id""",
        (candidate_id,),
    ):
        if member_count:
            member_digest_builder.update(b",")
        member_digest_builder.update(json.dumps(cell_id, separators=(",", ":")).encode())
        member_count += 1
        nucleus_count += int(bool(has_valid_nucleus))
    member_digest_builder.update(b"]")
    member_digest = member_digest_builder.hexdigest()
    if facts.get("member_cell_inventory_sha256") != member_digest:
        raise ROIAnalysisError("semantic candidate member-cell inventory digest is stale")
    if (
        facts.get("member_cell_count") is not None
        and facts.get("member_cell_count") != member_count
    ):
        raise ROIAnalysisError("semantic candidate member-cell count is stale")

    phenotype_counts = {
        str(phenotype): int(count)
        for phenotype, count in database.execute(
            """SELECT c.phenotype, COUNT(*) FROM relations r
               JOIN cells c ON c.cell_id = r.cell_id
               WHERE r.candidate_id = ? AND c.phenotype IS NOT NULL
               GROUP BY c.phenotype""",
            (candidate_id,),
        )
    }
    marker_counts = {
        (str(marker), str(state)): int(count)
        for marker, state, count in database.execute(
            """SELECT m.marker_id, m.marker_state, COUNT(*) FROM relations r
               JOIN markers m ON m.cell_id = r.cell_id
               WHERE r.candidate_id = ? GROUP BY m.marker_id, m.marker_state""",
            (candidate_id,),
        )
    }
    area_px2 = facts.get("candidate_area_px2")
    area_um2 = (
        float(area_px2)
        * float(physical_calibration["x_um_per_px"])
        * float(physical_calibration["y_um_per_px"])
        if isinstance(area_px2, (int, float))
        and not isinstance(area_px2, bool)
        and isfinite(float(area_px2))
        and float(area_px2) > 0
        else None
    )
    area_mm2 = area_um2 / 1_000_000.0 if area_um2 is not None else None
    unavailable: dict[str, dict[str, str]] = {}
    values: dict[str, object] = {
        "raw_export_sha256": raw_export_sha256,
        "provisional_membership_sha256": membership_artifact_sha256,
        "member_cell_inventory_sha256": member_digest,
        "segmentation_population_id": relation_identity["segmentation_population_id"],
        "phenotype_view": dict(phenotype_view),
        "physical_calibration": dict(physical_calibration),
        "segmented_cell_count": member_count,
        "segmented_nucleus_count": nucleus_count,
        "phenotype_counts": phenotype_counts,
        "marker_state_counts": {
            f"{marker}:{state}": count for (marker, state), count in marker_counts.items()
        },
        "transport_evidence": {
            "parser": "incremental-json/v1",
            "relation_rows_retained_peak": 1,
            "disk_backed_join": True,
        },
        **dict(facts),
    }
    _set_evidence_value(
        values, unavailable, "candidate_area", area_um2, "candidate_area_source_absent"
    )
    _set_evidence_value(
        values,
        unavailable,
        "geometry_component_count",
        facts.get("geometry_component_count"),
        "geometry_component_source_absent",
    )
    _set_evidence_value(
        values,
        unavailable,
        "cell_density",
        member_count / area_mm2 if area_mm2 else None,
        "calibrated_candidate_area_unavailable",
    )
    _set_evidence_value(
        values,
        unavailable,
        "nucleus_density",
        nucleus_count / area_mm2 if area_mm2 else None,
        "calibrated_candidate_area_unavailable",
    )
    _set_evidence_value(
        values,
        unavailable,
        "classifier_confidence",
        facts.get("classifier_confidence", candidate.get("classifier_confidence")),
        "classifier_confidence_source_absent",
    )
    _set_evidence_value(
        values,
        unavailable,
        "minimum_boundary_distance_um",
        facts.get("minimum_boundary_distance_um"),
        "primary_geometry_source_absent",
    )
    non_measurement = tuple(item for item in selectors
                            if item[0] not in {"cell_measurement_aggregate", "population_marker_fraction"})
    _add_selector_qualified_values(
        values=values,
        unavailable=unavailable,
        selectors=non_measurement,
        phenotype_counts=phenotype_counts,
        marker_counts=marker_counts,
        measurements={},
        member_count=member_count,
        area_mm2=area_mm2,
        phenotype_generation_id=str(phenotype_view["generation_id"]),
        facts=facts,
    )
    for kind, selector in selectors:
        if kind == "population_marker_fraction":
            _add_population_marker_fraction(
                database=database, candidate_id=candidate_id, selector=selector,
                phenotype_counts=phenotype_counts, relation_identity=relation_identity,
                values=values, unavailable=unavailable, member_count=member_count,
            )
            continue
        if kind != "cell_measurement_aggregate":
            continue
        key = _semantic_evidence_key(kind, selector)
        measurement_id = selector.get("measurement_id", "")
        observed_count, mean = database.execute(
            """SELECT COUNT(m.value), AVG(m.value) FROM relations r
               JOIN measurements m ON m.cell_id = r.cell_id
               WHERE r.candidate_id = ? AND m.measurement_id = ?""",
            (candidate_id, measurement_id),
        ).fetchone()
        _record_measurement_counts(values, key, member_count, observed_count)
        if observed_count == 0 or (selector.get("missing_policy") != "exclude" and observed_count != member_count):
            unavailable[key] = {"reason": "measurement_source_absent"}
        elif selector.get("statistic") == "mean":
            values[key] = float(mean)
        elif selector.get("statistic") == "median":
            middle = observed_count // 2
            if observed_count % 2:
                values[key] = float(
                    database.execute(
                        """SELECT m.value FROM relations r JOIN measurements m ON m.cell_id = r.cell_id
                       WHERE r.candidate_id = ? AND m.measurement_id = ? ORDER BY m.value LIMIT 1 OFFSET ?""",
                        (candidate_id, measurement_id, middle),
                    ).fetchone()[0]
                )
            else:
                pair = database.execute(
                    """SELECT m.value FROM relations r JOIN measurements m ON m.cell_id = r.cell_id
                       WHERE r.candidate_id = ? AND m.measurement_id = ? ORDER BY m.value LIMIT 2 OFFSET ?""",
                    (candidate_id, measurement_id, middle - 1),
                ).fetchall()
                values[key] = (float(pair[0][0]) + float(pair[1][0])) / 2.0
        else:
            unavailable[key] = {"reason": "measurement_statistic_unavailable"}
    if unavailable:
        values["unavailable_evidence"] = unavailable
    return SemanticEvidenceSnapshot(
        slide_id=str(relation_identity["slide_id"]),
        section_id=str(relation_identity["section_id"]),
        analysis_domain_id=str(relation_identity["analysis_domain_id"]),
        candidate_roi_id=candidate_id,
        structure_id=_structure_id_from_candidate(candidate),
        geometry_digest=geometry_digest,
        membership_digest=member_digest,
        values=values,
    )


def _record_measurement_counts(values, key, member_count, observed_count):
    values.setdefault("calculation_inputs", {})[key] = {
        "total_cell_count": member_count,
        "contributing_cell_count": observed_count,
        "excluded_cell_count": member_count - observed_count,
    }


def _add_population_marker_fraction(
    *, database, candidate_id, selector, phenotype_counts, relation_identity, values, unavailable, member_count,
):
    """Count an existing phenotype union using bounded batches of saved intensity.

    The shared threshold evaluator owns finite/nonnegative domains and constant
    decisions. Missing measurements and out-of-domain values remain unknown;
    only positive and negative cells enter this measurement's denominator.
    """
    import numpy as np
    from mif_pipeline.cell_analysis.marker_rules import threshold_states
    from mif_pipeline.roi_validation.population_marker import (
        marker_rule_for_section, parse_population_marker_gate, parse_population_phenotype_ids,
    )

    key = _semantic_evidence_key("population_marker_fraction", selector)
    phenotypes = parse_population_phenotype_ids(selector["phenotype_ids"])
    gate = parse_population_marker_gate(selector["positivity_gate"], selector["measurement_id"])
    try:
        rule = marker_rule_for_section(gate, relation_identity["slide_id"], relation_identity["section_id"])
    except ValueError:
        unavailable[key] = {"reason": "positivity_gate_scope_unavailable"}
        return
    parent_count = sum(phenotype_counts.get(label, 0) for label in phenotypes)
    all_cells = selector["denominator"] == "all_valid_marker_cells"
    placeholders = ",".join("?" for _ in phenotypes)
    population_clause = "" if all_cells else f"AND c.phenotype IN ({placeholders})"
    cursor = database.execute(
        f"""SELECT m.value, c.phenotype FROM relations r JOIN cells c ON c.cell_id = r.cell_id
            JOIN measurements m ON m.cell_id = r.cell_id
            WHERE r.candidate_id = ? AND m.measurement_id = ?
              {population_clause}""",
        (candidate_id, selector["measurement_id"], *(() if all_cells else phenotypes)),
    )
    positive = negative = valid_count = 0
    parent_labels = set(phenotypes)
    while rows := cursor.fetchmany(8192):
        states = threshold_states(np.fromiter((row[0] for row in rows), dtype=np.float64), rule)
        valid_count += int(np.count_nonzero(states >= 0))
        parent_states = states[np.fromiter((row[1] in parent_labels for row in rows), dtype=bool)] if all_cells else states
        positive += int(np.count_nonzero(parent_states == 1))
        negative += int(np.count_nonzero(parent_states == 0))
    parent_valid_count = positive + negative
    selected_count = positive if selector["marker_state_id"] == "positive" else negative
    values.setdefault("calculation_inputs", {})[key] = {
        "parent_cell_count": parent_count,
        "positive_cell_count": positive,
        "negative_cell_count": negative,
        "unknown_cell_count": parent_count - parent_valid_count,
        "parent_valid_marker_cell_count": parent_valid_count,
        "valid_marker_cell_count": valid_count,
        "denominator_cell_count": valid_count,
        "denominator_excluded_cell_count": (member_count if all_cells else parent_count) - valid_count,
        "selected_cell_count": selected_count,
    }
    if valid_count:
        values[key] = selected_count / valid_count
    else:
        unavailable[key] = {"reason": "valid_marker_cell_denominator_is_zero"}


def _semantic_evidence_key(kind: str, selector: Mapping[str, str]) -> str:
    return kind + ":" + json.dumps(dict(selector), sort_keys=True, separators=(",", ":"))


def _semantic_inventory_sort_key(value: object) -> str:
    return json.dumps(str(value), ensure_ascii=True, separators=(",", ":"))


def _set_evidence_value(
    values: dict[str, object],
    unavailable: dict[str, dict[str, str]],
    key: str,
    value: object,
    reason: str,
) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(float(value)):
        values.pop(key, None)
        unavailable[key] = {"reason": reason}
        return
    values[key] = value


def _add_selector_qualified_values(
    *,
    values: dict[str, object],
    unavailable: dict[str, dict[str, str]],
    selectors: Sequence[tuple[str, dict[str, str]]],
    phenotype_counts: Mapping[str, int],
    marker_counts: Mapping[tuple[str, str], int],
    measurements: Mapping[str, Sequence[float]],
    member_count: int,
    area_mm2: float | None,
    phenotype_generation_id: str,
    facts: Mapping[str, object],
) -> None:
    # A restricted phenotyping result retains every segmented cell. Its explicit
    # excluded state cannot serve as negative-marker or absent-phenotype evidence
    # for a larger semantic region; geometric counts keep their full denominator.
    incomplete_phenotyping = phenotype_counts.get("Not phenotyped", 0) > 0
    for kind, selector in selectors:
        key = _semantic_evidence_key(kind, selector)
        if incomplete_phenotyping and (kind.startswith("phenotype_") or kind.startswith("marker_state_")):
            unavailable[key] = {"reason": "phenotyping_population_does_not_cover_candidate"}
            continue
        generation_field = (
            "phenotype_generation_id"
            if kind.startswith("phenotype_")
            else "marker_generation_id" if kind.startswith("marker_state_") else None
        )
        if (
            generation_field is not None
            and generation_field in selector
            and selector.get(generation_field) != phenotype_generation_id
        ):
            unavailable[key] = {"reason": "selected_generation_mismatch"}
            continue
        if kind.startswith("phenotype_"):
            selected_count = phenotype_counts.get(selector.get("phenotype_id", ""), 0)
            if kind == "phenotype_count":
                values[key] = selected_count
            elif kind == "phenotype_fraction":
                if member_count:
                    values[key] = selected_count / member_count
                else:
                    unavailable[key] = {"reason": "segmented_cell_denominator_is_zero"}
            elif kind == "phenotype_density":
                if area_mm2:
                    values[key] = selected_count / area_mm2
                else:
                    unavailable[key] = {"reason": "calibrated_candidate_area_unavailable"}
            continue
        if kind.startswith("marker_state_"):
            selected_count = marker_counts.get(
                (selector.get("marker_id", ""), selector.get("marker_state_id", "")), 0
            )
            if kind == "marker_state_count":
                values[key] = selected_count
            elif member_count:
                values[key] = selected_count / member_count
            else:
                unavailable[key] = {"reason": "segmented_cell_denominator_is_zero"}
            continue
        if kind == "cell_measurement_aggregate":
            observed = tuple(value for value in measurements.get(selector.get("measurement_id", ""), ())
                             if isinstance(value, (int, float)) and not isinstance(value, bool)
                             and isfinite(float(value)))
            _record_measurement_counts(values, key, member_count, len(observed))
            if not observed or (selector.get("missing_policy") != "exclude" and len(observed) != member_count):
                unavailable[key] = {"reason": "measurement_source_absent"}
            elif selector.get("statistic") == "mean":
                values[key] = sum(observed) / len(observed)
            elif selector.get("statistic") == "median":
                ordered = sorted(observed)
                middle = len(ordered) // 2
                values[key] = (
                    ordered[middle]
                    if len(ordered) % 2
                    else (ordered[middle - 1] + ordered[middle]) / 2.0
                )
            else:
                unavailable[key] = {"reason": "measurement_statistic_unavailable"}
            continue
        if kind == "distance_to_primary":
            by_class = facts.get("distance_to_primary_um")
            value = (
                by_class.get(selector.get("primary_class_id"))
                if isinstance(by_class, Mapping)
                else None
            )
            _set_evidence_value(values, unavailable, key, value, "primary_geometry_source_absent")
            continue
        if kind == "overlap_fraction_with_primary":
            by_class = facts.get("overlap_fraction_with_primary")
            by_denominator = (
                by_class.get(selector.get("primary_class_id"))
                if isinstance(by_class, Mapping)
                else None
            )
            value = (
                by_denominator.get(selector.get("denominator"))
                if isinstance(by_denominator, Mapping)
                else None
            )
            _set_evidence_value(values, unavailable, key, value, "primary_overlap_source_absent")


def _structure_id_from_candidate(candidate: Mapping[str, object]) -> str:
    value = candidate.get("structure_id")
    if isinstance(value, str) and value:
        return value
    roi_class = candidate.get("class")
    if isinstance(roi_class, str) and roi_class.startswith("Candidate_"):
        return roi_class.removeprefix("Candidate_")
    raise ROIAnalysisError("semantic candidate lacks an exact structure ID")


def validate_semantic_profile_selectors(
    profile: SemanticProfileV3, config: AnalysisConfig, *,
    secondary_structures=None, defer_cell_selectors: bool = False,
    cell_selector_inventory: Mapping[str, object] | None = None,
) -> None:
    """Check region selectors now and cell selectors when their results exist."""
    primary_ids = {item.class_id for item in config.primary_classes or ()}
    definitions = config.secondary_structures if secondary_structures is None else secondary_structures
    structures = {item.structure_id: item for item in definitions}
    matching_structures = tuple(
        item
        for item in structures.values()
        if (
            item.semantic_profile.profile_id,
            item.semantic_profile.profile_version,
        )
        == (profile.profile_id, profile.profile_version)
    )
    if len(matching_structures) != 1 or matching_structures[0].structure_id != profile.structure_id:
        raise ROIAnalysisError(
            "semantic profile structure_id does not match its configured secondary structure"
        )

    phenotype_ids = {item.class_id for item in config.phenotyping.phenotype_classes}
    marker_ids = {
        marker_id
        for marker_id, rule in config.phenotyping.known_marker_rules.items()
        if isinstance(rule, Mapping) and rule.get("mode") != "unused"
    }
    measurement_ids = {
        rule["measurement"]
        for rule in config.phenotyping.known_marker_rules.values()
        if isinstance(rule, Mapping)
        and rule.get("mode") == "threshold"
        and isinstance(rule.get("measurement"), str)
    }
    measurement_ids.update(
        spec["source"]
        for spec in config.clustering.feature_specs
        if isinstance(spec, Mapping)
        and spec.get("kind") == "expression"
        and isinstance(spec.get("source"), str)
    )
    channel_ids = {channel.channel_id for channel in config.channels}
    channel_bindings: dict[str, set[str]] = {}
    for classifier in (config.primary_classifier, *(item.classifier for item in definitions)):
        if classifier is None:
            continue
        for role, channel_id in classifier.channel_roles.items():
            channel_bindings.setdefault(role, set()).add(channel_id)
    measurement_channels = None
    if cell_selector_inventory is not None:
        if set(cell_selector_inventory) != {
            "phenotype_ids", "marker_ids", "measurement_ids", "channel_roles", "measurement_channels"
        }:
            raise ROIAnalysisError("Semantic cell selector inventory is incomplete")
        phenotype_ids = set(cell_selector_inventory["phenotype_ids"])
        marker_ids = set(cell_selector_inventory["marker_ids"])
        measurement_ids = set(cell_selector_inventory["measurement_ids"])
        channel_ids = set(cell_selector_inventory["channel_roles"])
        measurement_channels = cell_selector_inventory["measurement_channels"]

    def visit(node: object) -> None:
        if not isinstance(node, Mapping):
            return
        if "all_of" in node or "any_of" in node:
            for child in next(iter(node.values())):
                visit(child)
            return
        if "at_least" in node:
            for child in node["at_least"]["criteria"]:
                visit(child)
            return
        selector = node.get("selector", {})
        if not isinstance(selector, Mapping):
            return
        if "primary_class_id" in selector and selector["primary_class_id"] not in primary_ids:
            raise ROIAnalysisError("semantic profile references an unknown primary_class_id")
        if "structure_id" in selector and selector["structure_id"] not in structures:
            raise ROIAnalysisError("semantic profile references an unknown structure_id")
        if not defer_cell_selectors and "phenotype_id" in selector and selector["phenotype_id"] not in phenotype_ids:
            raise ROIAnalysisError("semantic profile references an unknown phenotype_id")
        if not defer_cell_selectors and "phenotype_ids" in selector:
            from mif_pipeline.roi_validation.population_marker import parse_population_phenotype_ids
            if not set(parse_population_phenotype_ids(selector["phenotype_ids"])) <= phenotype_ids:
                raise ROIAnalysisError("semantic profile references an unknown population phenotype_id")
        if not defer_cell_selectors and "marker_id" in selector and selector["marker_id"] not in marker_ids:
            raise ROIAnalysisError("semantic profile references an unknown marker_id")
        if not defer_cell_selectors and "measurement_id" in selector and selector["measurement_id"] not in measurement_ids:
            raise ROIAnalysisError("semantic profile references an unknown measurement_id")
        if not defer_cell_selectors and "channel_role" in selector:
            selected_channel = selector["channel_role"]
            if selected_channel not in channel_ids:
                bindings = channel_bindings.get(selected_channel, set())
                if len(bindings) > 1:
                    raise ROIAnalysisError(
                        f"Measurement {selector['measurement_id']!r} has an ambiguous channel role "
                        f"{selected_channel!r}; choose its image channel."
                    )
                if not bindings or not bindings <= channel_ids:
                    raise ROIAnalysisError(
                        f"Measurement {selector['measurement_id']!r} references an unknown channel {selected_channel!r}."
                    )
                selected_channel = next(iter(bindings))
            if (
                measurement_channels is not None
                and measurement_channels[selector["measurement_id"]] != selected_channel
            ):
                raise ROIAnalysisError(
                    f"The selected channel {selected_channel!r} does not match saved measurement {selector['measurement_id']!r}."
                )
        for generation_field in (
            "phenotype_generation_id",
            "marker_generation_id",
        ):
            if generation_field in selector:
                raise ROIAnalysisError(
                    f"semantic profile {generation_field} has no configuration source of truth"
                )

    visit(profile.criteria)
    for rule in profile.classification_rules:
        visit(rule.criteria)


def _mapping_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


__all__ = ["validate_semantic_profile_selectors"]
