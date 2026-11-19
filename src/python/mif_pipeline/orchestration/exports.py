"""Atomic interoperability exports for auditable ROI-analysis records.

The `OutputArtifact` record carries `path`, `schema_version`, `media_type`, `record_count`,
`sha256`.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Callable, TypeVar
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

from mif_pipeline.datasets.geometry import polygon_wkb
from mif_pipeline.datasets.collection import (
    CELL_COLLECTION_SCHEMA_VERSION, CellDatasetCollection,
    open_cell_dataset, write_cell_dataset_collection,
)

from mif_pipeline.cell_analysis.spatial_handoff import (
    AnalysisDomainCellGroup,
    CanonicalSpatialFrameEvidence,
    DomainHandoffEvidence,
    DomainSetEvidence,
    FinalROIState,
    SpatialCellHandoff,
    SpatialHandoffError,
    TissueCellGroup,
)
from mif_pipeline.pipeline_core.config import TissueDetectionConfig
from mif_pipeline.pipeline_core.models import (
    CellRecord,
    ROIAnalysisError,
    ROIRecord,
    ReviewStatus,
    SemanticStatus,
    SourceKind,
)
from mif_pipeline.pipeline_core.domain import AnalysisDomainKind
from mif_pipeline.tissue_detection.generations import (
    DetectorValidationRecord,
    TissueDetectionQC,
    TissueSectionGeneration,
    validate_generation_manifest,
)

TISSUE_SCHEMA_VERSION = "roi-tissue-generations/v1"
ROI_TABLE_SCHEMA_VERSION = "roi-rois/v3"
SPATIAL_HANDOFF_SCHEMA_VERSION = "spatial-cell-handoff/v4"
ANALYSIS_PROVENANCE_SCHEMA_VERSION = "roi-analysis-provenance/v2"
LEVEL0_COORDINATE_SYSTEM = "global_level0_pixels"


# ==============================================================================
#                                    Exports
# ==============================================================================


class ExportError(ROIAnalysisError):
    """Raised when an export cannot be completed and validated atomically."""


def _sha256_text(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ExportError(f"{field} must be a lowercase SHA-256")
    return value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class OutputArtifact:
    """One immutable checksummed entry in the output inventory."""

    path: Path
    schema_version: str
    media_type: str
    record_count: int
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))
        for field in ("schema_version", "media_type"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ExportError(f"{field} must be a non-empty string")
        if (
            isinstance(self.record_count, bool)
            or not isinstance(self.record_count, int)
            or self.record_count < 0
        ):
            raise ExportError("record_count must be a non-negative integer")
        _sha256_text(self.sha256, "output sha256")

    def to_mapping(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "schema_version": self.schema_version,
            "media_type": self.media_type,
            "record_count": self.record_count,
            "sha256": self.sha256,
        }

    def validate_file(self) -> None:
        """Validate checksum plus the declared media/schema/count contract."""
        _validate_output_artifact_file(self)

    @classmethod
    def from_mapping(cls, value: object) -> "OutputArtifact":
        if not isinstance(value, Mapping):
            raise ExportError("output inventory entry must be a mapping")
        required = {"path", "schema_version", "media_type", "record_count", "sha256"}
        if set(value) != required:
            raise ExportError("output inventory entry fields do not match the schema")
        try:
            return cls(
                path=Path(value["path"]),
                schema_version=value["schema_version"],
                media_type=value["media_type"],
                record_count=value["record_count"],
                sha256=value["sha256"],
            )
        except (TypeError, ROIAnalysisError) as exc:
            raise ExportError("invalid output inventory entry") from exc


@dataclass(frozen=True, slots=True)
class SpatialHandoffArtifacts:
    """Atomically finalized handoff and canonical-cell dataset descriptions."""

    descriptor: OutputArtifact
    cells: OutputArtifact


def _temporary_path(destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")


def _atomic_replace(temporary: Path, destination: Path) -> None:
    os.replace(temporary, destination)


def _cleanup(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _write_json_temporary(path: Path, payload: object) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _validate_json_export(
    path: Path,
    *,
    schema_version: str,
    count_field: str,
    records_field: str,
    expected_count: int,
) -> None:
    try:
        with path.open(encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise ExportError(f"written JSON export is invalid: {path}") from exc
    if not isinstance(payload, Mapping) or payload.get("schema_version") != schema_version:
        raise ExportError("written JSON export schema is invalid")
    records = payload.get(records_field)
    if not isinstance(records, list):
        raise ExportError(f"written JSON export {records_field} must be a list")
    if payload.get(count_field) != expected_count or len(records) != expected_count:
        raise ExportError("written JSON export record count does not match")


def _validate_tissue_sections(path: Path, *, expected_count: int) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        generations = payload["generations"]
        sections = [section for generation in generations for section in generation["sections"]]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ExportError("written tissue export section schema is invalid") from exc
    if payload.get("section_count") != expected_count or len(sections) != expected_count:
        raise ExportError("written tissue export section count does not match")


def _artifact(
    destination: Path,
    *,
    schema_version: str,
    media_type: str,
    record_count: int,
) -> OutputArtifact:
    return OutputArtifact(
        path=destination,
        schema_version=schema_version,
        media_type=media_type,
        record_count=record_count,
        sha256=_file_sha256(destination),
    )


def _generation_export(generation: TissueSectionGeneration) -> dict[str, object]:
    sections: list[dict[str, object]] = []
    expected_detector_context = {
        "canonical_image_id": generation.canonical_image_id,
        "canonical_image_sha256": generation.canonical_image_sha256,
        "detector_id": generation.detector_id,
        "detector_version": generation.detector_version,
        "settings_hash": generation.settings_hash,
        "reference_channel_id": generation.reference_channel_id,
        "reference_channel_selection_source": (generation.reference_channel_selection_source),
        "stage_version": generation.stage_version,
        "tissue_resolution_selection": (generation.tissue_resolution_selection.to_mapping()),
        "native_level_read_attestation": (generation.native_level_read_attestation.to_mapping()),
    }
    for section in generation.sections:
        section_mapping = section.to_export_mapping()
        detector_context = section_mapping.pop("detector_context")
        if not isinstance(detector_context, Mapping):
            raise ExportError("tissue detector_context must be a mapping")
        if dict(detector_context) != expected_detector_context:
            raise ExportError("tissue PathObject detector context does not match its generation")
        sections.append(
            {
                "slide_id": section_mapping["slide_id"],
                "section_id": section_mapping["section_id"],
                "roi_id": section_mapping["roi_id"],
                "path_object_id": section_mapping["path_object_id"],
                "display_number": section_mapping["display_number"],
                "geometry": section_mapping["geometry"],
                "generation_id": generation.generation_id,
                "generation_status": generation.status.value,
                "revision": section_mapping["revision"],
                "review_status": section_mapping["review_status"],
                "detector_id": generation.detector_id,
                "detector_version": generation.detector_version,
                "detector_settings_hash": generation.settings_hash,
                "reference_channel_id": generation.reference_channel_id,
                "reference_channel_selection_source": (
                    generation.reference_channel_selection_source
                ),
                "tissue_resolution_selection": (
                    generation.tissue_resolution_selection.to_mapping()
                ),
                "native_level_read_attestation": (
                    generation.native_level_read_attestation.to_mapping()
                ),
                "provenance_record_id": section_mapping["provenance_pointer"],
                "derived_from": section_mapping["derived_from"],
                "supersedes": section_mapping["supersedes"],
            }
        )
    return {
        "slide_id": generation.slide_id,
        "generation_id": generation.generation_id,
        "generation_status": generation.status.value,
        "canonical_image_id": generation.canonical_image_id,
        "canonical_image_sha256": generation.canonical_image_sha256,
        "detector_id": generation.detector_id,
        "detector_version": generation.detector_version,
        "detector_settings_hash": generation.settings_hash,
        "reference_channel_id": generation.reference_channel_id,
        "reference_channel_selection_source": generation.reference_channel_selection_source,
        "stage_version": generation.stage_version,
        "tissue_resolution_selection": (generation.tissue_resolution_selection.to_mapping()),
        "native_level_read_attestation": (generation.native_level_read_attestation.to_mapping()),
        "derived_from_generation_id": generation.derived_from_generation_id,
        "sections": sections,
    }


def write_tissue_generation(
    path: str | Path,
    generations: TissueSectionGeneration | Iterable[TissueSectionGeneration],
    *,
    tissue_detection: TissueDetectionConfig | None = None,
    detector_validation: DetectorValidationRecord | None = None,
    tissue_qc: TissueDetectionQC | None = None,
    validation_generation_id: str | None = None,
    expected_canonical_image_id: str | None = None,
    expected_canonical_image_sha256: str | None = None,
) -> OutputArtifact:
    """Write all tissue-generation states without collapsing their history."""
    destination = Path(path)
    if isinstance(generations, TissueSectionGeneration):
        generation_values = (generations,)
    else:
        generation_values = tuple(generations)
    try:
        generation_values = validate_generation_manifest(
            generation_values,
            tissue_detection=tissue_detection,
            detector_validation=detector_validation,
            tissue_qc=tissue_qc,
            validation_generation_id=validation_generation_id,
            expected_canonical_image_id=expected_canonical_image_id,
            expected_canonical_image_sha256=expected_canonical_image_sha256,
        )
    except ROIAnalysisError as exc:
        raise ExportError(str(exc)) from exc
    exported = [_generation_export(item) for item in generation_values]
    section_count = sum(len(item["sections"]) for item in exported)
    payload = {
        "schema_version": TISSUE_SCHEMA_VERSION,
        "coordinate_system": LEVEL0_COORDINATE_SYSTEM,
        "generation_count": len(exported),
        "section_count": section_count,
        "generations": exported,
    }
    temporary = _temporary_path(destination)
    try:
        _write_json_temporary(temporary, payload)
        _validate_json_export(
            temporary,
            schema_version=TISSUE_SCHEMA_VERSION,
            count_field="generation_count",
            records_field="generations",
            expected_count=len(exported),
        )
        _validate_tissue_sections(temporary, expected_count=section_count)
        _atomic_replace(temporary, destination)
    finally:
        _cleanup(temporary)
    return _artifact(
        destination,
        schema_version=TISSUE_SCHEMA_VERSION,
        media_type="application/json",
        record_count=section_count,
    )


def _json_text(value: object) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


ROI_ARROW_SCHEMA = pa.schema(
    [
        pa.field("roi_id", pa.string(), nullable=False),
        pa.field("slide_id", pa.string(), nullable=False),
        pa.field("section_id", pa.string(), nullable=False),
        pa.field("class", pa.string(), nullable=False),
        pa.field("roi_level", pa.string(), nullable=False),
        pa.field("analysis_domain_id", pa.string(), nullable=False),
        pa.field("analysis_domain_kind", pa.string(), nullable=False),
        pa.field("parent_roi_id", pa.string()),
        pa.field("geometry", pa.binary(), nullable=False),
        pa.field("source", pa.string(), nullable=False),
        pa.field("classifier_id", pa.string()),
        pa.field("classifier_version", pa.string()),
        pa.field("classifier_confidence", pa.float64()),
        pa.field("semantic_profile", pa.string()),
        pa.field("semantic_status", pa.string(), nullable=False),
        pa.field("semantic_rule_results", pa.string()),
        pa.field("semantic_classification_status", pa.string()),
        pa.field("semantic_class_id", pa.string()),
        pa.field("review_rationale", pa.string()),
        pa.field("review_status", pa.string(), nullable=False),
        pa.field("original_predicted_geometry", pa.binary()),
        pa.field("registration_id", pa.string()),
        pa.field("registration_qc", pa.string()),
    ],
    metadata={
        b"schema_version": ROI_TABLE_SCHEMA_VERSION.encode("ascii"),
        b"coordinate_system": LEVEL0_COORDINATE_SYSTEM.encode("ascii"),
        b"nested_mapping_encoding": b"canonical-json",
        b"geo": json.dumps({
            "version": "1.1.0",
            "primary_column": "geometry",
            "columns": {
                name: {
                    "encoding": "WKB",
                    "geometry_types": ["Polygon", "MultiPolygon"],
                    "crs": None,
                    "edges": "planar",
                }
                for name in ("geometry", "original_predicted_geometry")
            },
        }, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    },
)


def _roi_arrow_mapping(record: ROIRecord) -> dict[str, object]:
    """Encode exact ROI outlines once; retain numerical fields as typed columns."""
    mapping = record.to_export_mapping()
    try:
        mapping["geometry"] = polygon_wkb(mapping["geometry"])[0]
        original = mapping["original_predicted_geometry"]
        mapping["original_predicted_geometry"] = (
            polygon_wkb(original)[0] if original is not None else None
        )
    except (TypeError, ValueError, KeyError, IndexError) as exc:
        raise ExportError("ROI export requires closed polygon outlines with finite x/y coordinates") from exc
    for field in ("semantic_rule_results", "registration_qc"):
        mapping[field] = _json_text(mapping[field])
    return mapping


RecordT = TypeVar("RecordT")


def _write_parquet_batches(
    temporary: Path,
    records: Iterable[RecordT],
    *,
    record_type: type[RecordT],
    schema: pa.Schema,
    mapper: Callable[[RecordT], dict[str, object]],
    batch_size: int,
) -> int:
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
        raise ExportError("batch_size must be a positive integer")
    count = 0
    batch: list[dict[str, object]] = []
    try:
        with pq.ParquetWriter(temporary, schema=schema) as writer:
            for record in records:
                if not isinstance(record, record_type):
                    raise ExportError(f"records must contain {record_type.__name__} instances")
                batch.append(mapper(record))
                if len(batch) == batch_size:
                    record_batch = pa.RecordBatch.from_pylist(batch, schema=schema)
                    writer.write_batch(record_batch)
                    count += len(batch)
                    batch.clear()
            if batch:
                record_batch = pa.RecordBatch.from_pylist(batch, schema=schema)
                writer.write_batch(record_batch)
                count += len(batch)
    except (pa.ArrowException, OSError) as exc:
        raise ExportError("could not write Parquet export") from exc
    return count


def _validate_parquet(path: Path, *, schema: pa.Schema, expected_count: int) -> None:
    try:
        parquet = pq.ParquetFile(path)
        actual_schema = parquet.schema_arrow
        count = parquet.metadata.num_rows
    except (pa.ArrowException, OSError) as exc:
        raise ExportError("written Parquet export is invalid") from exc
    # Parquet normalizes list child names (``item`` to ``element``), so compare
    # logical fields independently from the file-level contract metadata.
    if not actual_schema.equals(schema) or actual_schema.metadata != schema.metadata:
        raise ExportError("written Parquet export schema does not match")
    if count != expected_count:
        raise ExportError("written Parquet export record count does not match")


def write_roi_table(
    path: str | Path,
    records: Iterable[ROIRecord],
    *,
    batch_size: int = 1024,
) -> OutputArtifact:
    """Publish ROI records as a Parquet table in bounded batches.

    We check the completed schema and row count before atomically selecting
    the file, then return its recorded content identity to the export owner.
    """
    destination = Path(path)
    if destination.suffix.lower() != ".parquet":
        raise ExportError("ROI table destination must end in .parquet")
    temporary = _temporary_path(destination)
    try:
        count = _write_parquet_batches(
            temporary,
            records,
            record_type=ROIRecord,
            schema=ROI_ARROW_SCHEMA,
            mapper=_roi_arrow_mapping,
            batch_size=batch_size,
        )
        _validate_parquet(temporary, schema=ROI_ARROW_SCHEMA, expected_count=count)
        _atomic_replace(temporary, destination)
    finally:
        _cleanup(temporary)
    return _artifact(
        destination,
        schema_version=ROI_TABLE_SCHEMA_VERSION,
        media_type="application/vnd.apache.parquet",
        record_count=count,
    )


def _handoff_dataset_scope(handoff: SpatialCellHandoff, extra: Mapping[str, object] | None) -> dict[str, object]:
    """Retain native domain lineage independently of a selected result view."""
    extra = dict(extra or {})
    if set(extra) - {"sample_id", "image_entry_id", "domains"}:
        raise ExportError("native dataset scope contains unsupported fields")
    overrides = extra.pop("domains", {})
    if not isinstance(overrides, Mapping) or set(overrides) - set(handoff.domain_results):
        raise ExportError("native dataset domain scope is invalid")
    populations = sorted({result.segmentation_population_id for result in handoff.domain_results.values()})
    population = populations[0] if len(populations) == 1 else "segmentation-populations/" + hashlib.sha256(_json_text(populations).encode("utf-8")).hexdigest()
    domains = {}
    for domain_id, result in handoff.domain_results.items():
        member = result.member
        lineage = {
            "pipeline_stage": "segmentation",
            "generation_id": member.tissue_generation_id,
            "segmentation_population_id": result.segmentation_population_id,
            "analysis_domain_revision": str(member.analysis_domain_revision),
            "analysis_domain_revision_sha256": member.analysis_domain_revision_sha256,
            "domain_set_sha256": handoff.domain_set.domain_set_sha256,
            "analysis_domain_kind": member.analysis_domain_kind.value,
            "tissue_roi_id": member.tissue_roi_id,
        }
        additions = overrides.get(domain_id, {})
        if not isinstance(additions, Mapping) or set(additions) - {"measurement_generation_id"}:
            raise ExportError("native domain additions contain unsupported lineage")
        lineage.update(additions)
        domains[domain_id] = lineage
    return {
        "population_id": population,
        "spatial_frame": handoff.spatial_frame.to_mapping(),
        "domain_set_sha256": handoff.domain_set.domain_set_sha256,
        "selected_phenotype_view": handoff.selected_phenotype_view,
        "phenotype_generation_id": handoff.phenotype_generation_id,
        "active_analysis_domain_ids": sorted(id for id, result in handoff.domain_results.items() if result.domain_status == 'active'),
        "domains": domains,
        **extra,
    }


def write_spatial_handoff(
    path: str | Path,
    handoff: SpatialCellHandoff,
    *,
    dataset_scope: Mapping[str, object] | None = None,
) -> SpatialHandoffArtifacts:
    """Publish our selected existing datasets and atomically commit the handoff.

    i) We authenticate section bases and their narrow result/edit selections.
    ii) We save a reference collection and its compact population summaries.
    iii) We validate the completed descriptor before replacing the selected
    generation pointer. Existing geometry is reused throughout publication.
    """
    if not isinstance(handoff, SpatialCellHandoff):
        raise ExportError("spatial export requires a validated SpatialCellHandoff")
    descriptor_path = Path(path)
    if descriptor_path.suffix.lower() != ".json":
        raise ExportError("spatial handoff descriptor must end in .json")
    generation_id = uuid4().hex
    generations_dir = descriptor_path.parent / f".{descriptor_path.stem}.generations"
    generation_dir = generations_dir / generation_id
    cells_path = generation_dir / "cells" / "dataset.json"
    generation_descriptor_path = generation_dir / "descriptor.json"
    pointer_temporary = _temporary_path(descriptor_path)
    committed = False
    try:
        generation_dir.mkdir(parents=True, exist_ok=False)
        cells = write_cell_dataset_collection(
            cells_path.parent,
            handoff.cell_dataset,
            scope=_handoff_dataset_scope(handoff, dataset_scope),
        )
        if cells.row_count != handoff.cell_count:
            raise ExportError("spatial handoff cell count does not match")
        cells_sha256 = cells.sha256
        payload = _spatial_descriptor_payload(
            handoff,
            cells_path=cells_path,
            cells_sha256=cells_sha256,
        )
        descriptor_temporary = _temporary_path(generation_descriptor_path)
        _write_json_temporary(descriptor_temporary, payload)
        completed = open_cell_dataset(cells_path)
        try:
            if completed.selection_sha256 != handoff.cell_selection_sha256:
                raise ExportError('Published cell selection differs from its admitted scientific scope')
        finally:
            completed.close()
        _atomic_replace(descriptor_temporary, generation_descriptor_path)
        pointer = {
            "schema_version": SPATIAL_HANDOFF_SCHEMA_VERSION,
            "generation_id": generation_id,
            "generation_descriptor_path": str(
                generation_descriptor_path.relative_to(descriptor_path.parent)
            ),
            "generation_descriptor_sha256": _file_sha256(generation_descriptor_path),
        }
        _write_json_temporary(pointer_temporary, pointer)
        _atomic_replace(pointer_temporary, descriptor_path)
        committed = True
    except (OSError, ValueError, pa.ArrowException, SpatialHandoffError) as exc:
        raise ExportError("could not finalize spatial handoff atomically") from exc
    finally:
        _cleanup(pointer_temporary)
        if not committed and generation_dir.exists():
            shutil.rmtree(generation_dir)
    cells_artifact = OutputArtifact(
        path=cells_path,
        schema_version=CELL_COLLECTION_SCHEMA_VERSION,
        media_type="application/json",
        record_count=handoff.cell_count,
        sha256=_file_sha256(cells_path),
    )
    descriptor_artifact = _artifact(
        descriptor_path,
        schema_version=SPATIAL_HANDOFF_SCHEMA_VERSION,
        media_type="application/json",
        record_count=handoff.cell_count,
    )
    return SpatialHandoffArtifacts(descriptor=descriptor_artifact, cells=cells_artifact)


def _resolve_spatial_handoff(
    path: str | Path,
) -> tuple[SpatialCellHandoff, CellDatasetCollection, SpatialHandoffArtifacts]:
    """Load one current immutable generation and its exact output inventory."""
    descriptor_path = Path(path)
    try:
        pointer_bytes = descriptor_path.read_bytes()
        pointer = json.loads(pointer_bytes.decode("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExportError("spatial handoff descriptor is invalid") from exc
    if (
        not isinstance(pointer, Mapping)
        or set(pointer)
        != {
            "schema_version",
            "generation_id",
            "generation_descriptor_path",
            "generation_descriptor_sha256",
        }
        or pointer.get("schema_version") != SPATIAL_HANDOFF_SCHEMA_VERSION
    ):
        raise ExportError("spatial handoff pointer is invalid")
    relative = pointer.get("generation_descriptor_path")
    generation_id = pointer.get("generation_id")
    expected_parts = (f".{descriptor_path.stem}.generations", generation_id, "descriptor.json")
    if (
        not isinstance(generation_id, str)
        or not generation_id
        or not isinstance(relative, str)
        or Path(relative).is_absolute()
        or Path(relative).parts != expected_parts
    ):
        raise ExportError("spatial handoff generation path is invalid")
    generation_descriptor_path = descriptor_path.parent / relative
    try:
        generation_bytes = generation_descriptor_path.read_bytes()
        if hashlib.sha256(generation_bytes).hexdigest() != _sha256_text(
            pointer.get("generation_descriptor_sha256"), "generation descriptor SHA-256"
        ):
            raise ExportError("spatial handoff generation descriptor SHA-256 does not match")
    except OSError as exc:
        raise ExportError("spatial handoff committed generation is unavailable") from exc
    try:
        payload = json.loads(generation_bytes.decode("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExportError("spatial handoff generation descriptor is invalid") from exc
    if not isinstance(payload, Mapping) or payload.get("cells_path") != "cells/dataset.json":
        raise ExportError("spatial handoff cells_path must name its canonical dataset")
    cells_path = generation_descriptor_path.parent / "cells" / "dataset.json"
    expected_cells_sha256 = _sha256_text(payload.get('cells_sha256'),'spatial cells SHA-256')
    dataset = CellDatasetCollection(cells_path)
    if dataset.summary.sha256 != expected_cells_sha256:
        dataset.close()
        raise ExportError('Committed cell collection differs from its selected identity')
    try:
        handoff = _handoff_from_descriptor(payload,dataset)
    except (KeyError,TypeError,ValueError,ROIAnalysisError) as error:
        dataset.close()
        raise ExportError('Committed spatial handoff metadata is invalid') from error
    artifacts = SpatialHandoffArtifacts(
        descriptor=OutputArtifact(
            descriptor_path,
            schema_version=SPATIAL_HANDOFF_SCHEMA_VERSION,
            media_type="application/json",
            record_count=dataset.row_count,
            sha256=hashlib.sha256(pointer_bytes).hexdigest(),
        ),
        cells=OutputArtifact(
            cells_path,
            schema_version=CELL_COLLECTION_SCHEMA_VERSION,
            media_type="application/json",
            record_count=dataset.row_count,
            sha256=dataset.summary.sha256,
        ),
    )
    return handoff, dataset, artifacts


def load_spatial_handoff_with_artifacts(
    path: str | Path,
) -> tuple[SpatialCellHandoff, SpatialHandoffArtifacts]:
    """Open our accepted metadata and projected dataset without decoding cells.

    We validate the committed pointer and immutable descriptors once, retain
    exact domain/tissue counts, and return the same lazy selection to scientific
    consumers. Numeric columns and geometry are read only when requested.
    """
    handoff,_dataset,artifacts = _resolve_spatial_handoff(path)
    return handoff,artifacts


def load_spatial_handoff(path: str | Path) -> SpatialCellHandoff:
    """Load only the immutable generation named by the committed pointer."""

    handoff, _artifacts = load_spatial_handoff_with_artifacts(path)
    return handoff


_SPATIAL_DESCRIPTOR_FIELDS = {
    "schema_version",
    "spatial_frame",
    "domain_set",
    "selected_phenotype_view",
    "phenotype_generation_id",
    "phenotype_inventory_sha256",
    "cell_count",
    "cell_selection_sha256",
    "cells_path",
    "cells_schema_version",
    "cells_sha256",
    "tissue_groups",
    "domain_groups",
    "domain_results",
    "final_roi_states",
}
_SPATIAL_GROUP_FIELDS = {
    "tissue_roi_id",
    "slide_id",
    "section_id",
    "tissue_generation_id",
    "tissue_review_status",
    "cell_count",
}
_SPATIAL_DOMAIN_GROUP_FIELDS = {
    "analysis_domain_id",
    "domain_result_sha256",
    "cell_count",
}


def _spatial_descriptor_payload(
    handoff: SpatialCellHandoff, *, cells_path: Path, cells_sha256: str
) -> dict[str, object]:
    return {
        "schema_version": SPATIAL_HANDOFF_SCHEMA_VERSION,
        "spatial_frame": handoff.spatial_frame.to_mapping(),
        "domain_set": handoff.domain_set.to_mapping(),
        "selected_phenotype_view": handoff.selected_phenotype_view,
        "phenotype_generation_id": handoff.phenotype_generation_id,
        "phenotype_inventory_sha256": handoff.phenotype_inventory_sha256,
        "cell_count": handoff.cell_count,
        "cell_selection_sha256": handoff.cell_selection_sha256,
        "cells_path": "cells/dataset.json",
        "cells_schema_version": CELL_COLLECTION_SCHEMA_VERSION,
        "cells_sha256": cells_sha256,
        "tissue_groups": [
            {
                "tissue_roi_id": group.tissue_roi_id,
                "slide_id": group.slide_id,
                "section_id": group.section_id,
                "tissue_generation_id": group.tissue_generation_id,
                "tissue_review_status": group.tissue_review_status,
                "cell_count": group.cell_count,
            }
            for group in handoff.by_tissue.values()
        ],
        "domain_groups": [
            {
                "analysis_domain_id": group.analysis_domain_id,
                "domain_result_sha256": group.domain_result_sha256,
                "cell_count": group.cell_count,
            }
            for group in handoff.by_domain.values()
        ],
        "domain_results": [result.to_mapping() for result in handoff.domain_results.values()],
        "final_roi_states": [
            {
                "record": state.record.to_export_mapping(),
                "generation_id": state.generation_id,
                "pipeline_run_id": state.pipeline_run_id,
                "pipeline_stage": state.pipeline_stage,
                "classifier_status": state.classifier_status,
                "candidate_status": state.candidate_status,
                "path_object_id": state.path_object_id,
                "structure_id": state.structure_id,
                "geometry_sha256": state.geometry_sha256,
                "content_key": state.content_key,
            }
            for state in handoff.final_roi_states.values()
        ],
    }


def _final_roi_state_from_mapping(value: object) -> FinalROIState:
    if not isinstance(value, Mapping) or set(value) != {
        "record",
        "generation_id",
        "pipeline_run_id",
        "pipeline_stage",
        "classifier_status",
        "candidate_status",
        "path_object_id",
        "structure_id",
        "geometry_sha256",
        "content_key",
    }:
        raise ExportError("final ROI evidence schema is invalid")
    row = value["record"]
    if not isinstance(row, Mapping):
        raise ExportError("final ROI record is invalid")
    record = ROIRecord(
        roi_id=row["roi_id"],
        slide_id=row["slide_id"],
        section_id=row["section_id"],
        roi_class=row["class"],
        roi_level=row["roi_level"],
        analysis_domain_id=row["analysis_domain_id"],
        analysis_domain_kind=AnalysisDomainKind(row["analysis_domain_kind"]),
        parent_roi_id=row.get("parent_roi_id"),
        geometry=row["geometry"],
        source=SourceKind(row["source"]),
        classifier_id=row.get("classifier_id"),
        classifier_version=row.get("classifier_version"),
        classifier_confidence=row.get("classifier_confidence"),
        semantic_profile=row.get("semantic_profile"),
        semantic_status=SemanticStatus(row["semantic_status"]),
        semantic_rule_results=row.get("semantic_rule_results"),
        semantic_classification_status=row.get("semantic_classification_status"),
        semantic_class_id=row.get("semantic_class_id"),
        review_rationale=row.get("review_rationale"),
        review_status=ReviewStatus(row["review_status"]),
        original_predicted_geometry=row.get("original_predicted_geometry"),
        registration_id=row.get("registration_id"),
        registration_qc=row.get("registration_qc"),
    )
    return FinalROIState(
        record=record,
        generation_id=value["generation_id"],
        pipeline_run_id=value["pipeline_run_id"],
        pipeline_stage=value["pipeline_stage"],
        classifier_status=value["classifier_status"],
        candidate_status=value["candidate_status"],
        path_object_id=value["path_object_id"],
        structure_id=value["structure_id"],
        geometry_sha256=value["geometry_sha256"],
        content_key=value["content_key"],
    )


def _handoff_from_descriptor(payload,dataset):
    """Restore our compact scientific scopes over the selected collection.

    We decode current frame/domain/ROI evidence once and bind navigation counts
    to their selected domain readers. No population tuple, geometry table or
    second identity index is reconstructed by this persistence boundary.
    """
    if not isinstance(payload,Mapping) or set(payload)!=_SPATIAL_DESCRIPTOR_FIELDS or payload['schema_version']!=SPATIAL_HANDOFF_SCHEMA_VERSION or payload['cells_schema_version']!=CELL_COLLECTION_SCHEMA_VERSION or not isinstance(dataset,CellDatasetCollection):
        raise ExportError('Spatial handoff requires its exact current descriptor and collection')
    if dataset.row_count!=payload['cell_count'] or dataset.description['source_sha256']!=payload['cell_selection_sha256']:
        raise ExportError('Spatial handoff dataset identity/count differs from its owner')
    frame=CanonicalSpatialFrameEvidence.from_mapping(payload['spatial_frame'])
    domain_set=DomainSetEvidence.from_mapping(payload['domain_set'])
    scope=dataset.description['scope']
    if scope.get('spatial_frame')!=payload['spatial_frame'] or scope.get('domain_set_sha256')!=domain_set.domain_set_sha256:
        raise ExportError('Spatial handoff dataset scope differs from its accepted frame/domains')
    results_list=tuple(DomainHandoffEvidence.from_mapping(value) for value in payload['domain_results'])
    results={value.member.analysis_domain_id:value for value in results_list}
    states_list=tuple(_final_roi_state_from_mapping(value) for value in payload['final_roi_states'])
    states={value.record.roi_id:value for value in states_list}
    if len(results)!=len(results_list) or len(states)!=len(states_list):
        raise ExportError('Spatial handoff domain/ROI identities are duplicated')
    tissues={}
    for group in payload['tissue_groups']:
        if not isinstance(group,Mapping) or set(group)!=_SPATIAL_GROUP_FIELDS or group['tissue_roi_id'] in tissues:
            raise ExportError('Spatial handoff tissue group has invalid current fields')
        id=group['tissue_roi_id']
        domains=[domain for domain,result in results.items() if result.domain_status=='active' and result.member.tissue_roi_id==id]
        view=dataset.select_domains(domains)
        if view.row_count!=group['cell_count']:
            raise ExportError('Spatial handoff tissue count differs from its accepted domains')
        tissues[id]=TissueCellGroup(id,group['slide_id'],group['section_id'],group['tissue_generation_id'],group['tissue_review_status'],view)
    domains={}
    for group in payload['domain_groups']:
        if not isinstance(group,Mapping) or set(group)!=_SPATIAL_DOMAIN_GROUP_FIELDS or group['analysis_domain_id'] in domains:
            raise ExportError('Spatial handoff domain group has invalid current fields')
        id=group['analysis_domain_id'];view=dataset.select_domains([id])
        if view.row_count!=group['cell_count']:
            raise ExportError('Spatial handoff domain count differs from its accepted inventory')
        domains[id]=AnalysisDomainCellGroup(id,group['domain_result_sha256'],view)
    return SpatialCellHandoff(spatial_frame=frame,domain_set=domain_set,domain_results=results,by_domain=domains,by_tissue=tissues,cell_dataset=dataset,final_roi_states=states,selected_phenotype_view=payload['selected_phenotype_view'],phenotype_generation_id=payload['phenotype_generation_id'],phenotype_inventory_sha256=payload['phenotype_inventory_sha256'],cell_selection_sha256=payload['cell_selection_sha256'])


def _validate_output_artifact_file(artifact: OutputArtifact) -> None:
    """Validate output artifact file for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    path = artifact.path
    if not path.is_file():
        raise ExportError(f"output path does not exist: {path}")
    try:
        actual_sha256 = _file_sha256(path)
    except OSError as exc:
        raise ExportError(f"output path cannot be hashed: {path}") from exc
    if actual_sha256 != artifact.sha256:
        raise ExportError(f"output SHA-256 does not match: {path}")

    expected_media = {
        TISSUE_SCHEMA_VERSION: {"application/json"},
        ROI_TABLE_SCHEMA_VERSION: {"application/vnd.apache.parquet"},
        CELL_COLLECTION_SCHEMA_VERSION: {"application/json"},
        SPATIAL_HANDOFF_SCHEMA_VERSION: {"application/json"},
        ANALYSIS_PROVENANCE_SCHEMA_VERSION: {"application/json"},
    }
    allowed_media = expected_media.get(artifact.schema_version)
    if allowed_media is None:
        raise ExportError(f"unsupported output schema_version: {artifact.schema_version}")
    if artifact.media_type not in allowed_media:
        raise ExportError("output media_type is inconsistent with its schema_version")

    if artifact.schema_version == TISSUE_SCHEMA_VERSION:
        _validate_serialized_tissue_export(path, expected_section_count=artifact.record_count)
        return
    if artifact.schema_version == SPATIAL_HANDOFF_SCHEMA_VERSION:
        _handoff, dataset, _artifacts = _resolve_spatial_handoff(path)
        try:
            if dataset.row_count != artifact.record_count:
                raise ExportError("spatial handoff artifact record count does not match")
        finally:
            dataset.close()
        return
    if artifact.schema_version == ANALYSIS_PROVENANCE_SCHEMA_VERSION:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            revisions = payload["tissue_revision_records"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ExportError("analysis provenance output is invalid") from exc
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema_version") != ANALYSIS_PROVENANCE_SCHEMA_VERSION
            or not isinstance(revisions, list)
            or len(revisions) != artifact.record_count
        ):
            raise ExportError("analysis provenance inventory is invalid")
        return

    if artifact.schema_version == CELL_COLLECTION_SCHEMA_VERSION:
        dataset = open_cell_dataset(path)
        try:
            if dataset.row_count != artifact.record_count:
                raise ExportError("cell dataset artifact record count does not match")
        finally:
            dataset.close()
        return
    _validate_parquet(path, schema=ROI_ARROW_SCHEMA, expected_count=artifact.record_count)


def _validate_serialized_tissue_export(
    path: Path, *, expected_section_count: int
) -> tuple[TissueSectionGeneration, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExportError("output tissue JSON is invalid") from exc
    root_fields = {
        "schema_version",
        "coordinate_system",
        "generation_count",
        "section_count",
        "generations",
    }
    if (
        not isinstance(payload, Mapping)
        or set(payload) != root_fields
        or payload["schema_version"] != TISSUE_SCHEMA_VERSION
        or payload["coordinate_system"] != LEVEL0_COORDINATE_SYSTEM
        or not isinstance(payload["generations"], list)
    ):
        raise ExportError("output tissue JSON schema is invalid")
    generation_fields = {
        "slide_id",
        "generation_id",
        "generation_status",
        "canonical_image_id",
        "canonical_image_sha256",
        "detector_id",
        "detector_version",
        "detector_settings_hash",
        "reference_channel_id",
        "reference_channel_selection_source",
        "stage_version",
        "tissue_resolution_selection",
        "native_level_read_attestation",
        "derived_from_generation_id",
        "sections",
    }
    section_fields = {
        "slide_id",
        "section_id",
        "roi_id",
        "path_object_id",
        "display_number",
        "geometry",
        "generation_id",
        "generation_status",
        "revision",
        "review_status",
        "detector_id",
        "detector_version",
        "detector_settings_hash",
        "reference_channel_id",
        "reference_channel_selection_source",
        "tissue_resolution_selection",
        "native_level_read_attestation",
        "provenance_record_id",
        "derived_from",
        "supersedes",
    }
    reconstructed: list[dict[str, object]] = []
    section_count = 0
    for generation in payload["generations"]:
        if (
            not isinstance(generation, Mapping)
            or set(generation) != generation_fields
            or not isinstance(generation["sections"], list)
        ):
            raise ExportError("output tissue generation schema is invalid")
        detector_context = {
            "canonical_image_id": generation["canonical_image_id"],
            "canonical_image_sha256": generation["canonical_image_sha256"],
            "detector_id": generation["detector_id"],
            "detector_version": generation["detector_version"],
            "settings_hash": generation["detector_settings_hash"],
            "reference_channel_id": generation["reference_channel_id"],
            "reference_channel_selection_source": generation["reference_channel_selection_source"],
            "stage_version": generation["stage_version"],
            "tissue_resolution_selection": generation["tissue_resolution_selection"],
            "native_level_read_attestation": generation["native_level_read_attestation"],
        }
        sections: list[dict[str, object]] = []
        for section in generation["sections"]:
            if not isinstance(section, Mapping) or set(section) != section_fields:
                raise ExportError("output tissue section schema is invalid")
            if (
                section["generation_status"] != generation["generation_status"]
                or section["detector_id"] != generation["detector_id"]
                or section["detector_version"] != generation["detector_version"]
                or section["detector_settings_hash"] != generation["detector_settings_hash"]
                or section["reference_channel_id"] != generation["reference_channel_id"]
                or section["reference_channel_selection_source"]
                != generation["reference_channel_selection_source"]
                or section["tissue_resolution_selection"]
                != generation["tissue_resolution_selection"]
                or section["native_level_read_attestation"]
                != generation["native_level_read_attestation"]
            ):
                raise ExportError("output tissue section context does not match its generation")
            sections.append(
                {
                    "slide_id": section["slide_id"],
                    "section_id": section["section_id"],
                    "roi_id": section["roi_id"],
                    "path_object_id": section["path_object_id"],
                    "display_number": section["display_number"],
                    "generation_id": section["generation_id"],
                    "revision": section["revision"],
                    "review_status": section["review_status"],
                    "detector_context": detector_context,
                    "provenance_pointer": section["provenance_record_id"],
                    "geometry": section["geometry"],
                    "derived_from": section["derived_from"],
                    "supersedes": section["supersedes"],
                }
            )
        section_count += len(sections)
        reconstructed.append(
            {
                "slide_id": generation["slide_id"],
                "generation_id": generation["generation_id"],
                "status": generation["generation_status"],
                "canonical_image_id": generation["canonical_image_id"],
                "canonical_image_sha256": generation["canonical_image_sha256"],
                "detector_id": generation["detector_id"],
                "detector_version": generation["detector_version"],
                "settings_hash": generation["detector_settings_hash"],
                "reference_channel_id": generation["reference_channel_id"],
                "reference_channel_selection_source": generation[
                    "reference_channel_selection_source"
                ],
                "stage_version": generation["stage_version"],
                "tissue_resolution_selection": generation["tissue_resolution_selection"],
                "native_level_read_attestation": generation["native_level_read_attestation"],
                "sections": sections,
                "derived_from_generation_id": generation["derived_from_generation_id"],
            }
        )
    if (
        payload["generation_count"] != len(reconstructed)
        or payload["section_count"] != section_count
        or section_count != expected_section_count
    ):
        raise ExportError("output tissue record count does not match")
    try:
        return validate_generation_manifest(reconstructed)
    except ROIAnalysisError as exc:
        raise ExportError(str(exc)) from exc


def load_tissue_generation_artifact(
    artifact: OutputArtifact,
) -> tuple[TissueSectionGeneration, ...]:
    """Reload a trusted tissue inventory entry and reconstruct exact generations."""

    if type(artifact) is not OutputArtifact:
        raise TypeError("tissue generation loader requires exact OutputArtifact")
    if (
        artifact.schema_version != TISSUE_SCHEMA_VERSION
        or artifact.media_type != "application/json"
    ):
        raise ExportError("output artifact is not a tissue-generation export")
    artifact.validate_file()
    return _validate_serialized_tissue_export(
        artifact.path, expected_section_count=artifact.record_count
    )
