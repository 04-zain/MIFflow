"""Materialization of a dedicated multi-image spectral-reference project.

The `ReferenceRoiSnapshotRequest` record carries `project_id`, `project_file`,
`project_review_state`, `entries`, `role_requirements` and its remaining declared fields.

The runtime checks the declared project, image or process identity before exposing the
selected operation. Result records retain the native outcome and relevant file/saved-state
facts for the batch service to validate and publish."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mif_pipeline.preprocessing.unmixing.canonical_reference_input import CanonicalReferenceInput

from mif_pipeline.preprocessing.unmixing.reference_rois import (
    reference_project_imported_metadata,
    validate_reference_project_imported_metadata,
    ReferenceProjectEntry,
    ReferenceProjectImage,
    ReferenceRoiExport,
    ReferenceRoiRecord,
    ReferenceRoiReviewDecision,
    ReferenceRoiReviewResult,
    ReferenceRoleRequirement,
    ReferenceSectionScope,
    accept_reference_roi_snapshot,
    dumps_reference_roi_export,
    loads_reference_roi_export,
    loads_reference_project_evidence,
)
from mif_pipeline.preprocessing.unmixing.typed_checksum import typed_checksum_sha256

from .runner import QuPathEvidenceRun, QuPathRunner
from .scripts import QuPathScriptAsset, copy_script_assets

# ==============================================================================
#                               Reference project
# ==============================================================================


class ReferenceProjectPreparationError(ValueError):
    """A reference project request cannot be published without clobbering state."""


@dataclass(frozen=True, slots=True)
class ReferenceProjectLayout:
    root: Path
    project_file: Path
    scripts_dir: Path
    data_dir: Path
    exchange_dir: Path
    request_path: Path
    script_path: Path


@dataclass(frozen=True, slots=True)
class ReferenceProjectPrepareResult:
    entries: tuple[ReferenceProjectEntry, ...]
    run: QuPathEvidenceRun


@dataclass(frozen=True, slots=True)
class ReferenceRoiSnapshotRequest:
    project_id: str
    project_file: Path
    project_review_state: str
    entries: tuple[ReferenceProjectEntry, ...]
    role_requirements: tuple[ReferenceRoleRequirement, ...]
    created_at: str
    predecessor_content_sha256: str | None
    imported_metadata: tuple[dict[str, object], ...]

    def __post_init__(self) -> None:
        """Validate the ReferenceRoiSnapshotRequest fields when the record is constructed.

        The checks cover ``project_review_state``, ``entries``, ``role_requirements``,
        ``predecessor_content_sha256``, ``project_id``, ``project_file``. Invalid field
        types, values or combinations raise the declared validation error before this
        instance can be used by its caller."""

        if not isinstance(self.project_id, str) or not self.project_id.strip():
            raise ValueError("project_id must be non-empty")
        if not self.project_file.is_absolute():
            raise ValueError("project_file must be absolute")
        if self.project_review_state not in {"pending", "accepted", "rejected"}:
            raise ValueError("project_review_state is invalid")
        if (
            type(self.entries) is not tuple
            or not self.entries
            or any(not isinstance(item, ReferenceProjectEntry) for item in self.entries)
        ):
            raise TypeError("entries must be a nonempty typed tuple")
        validate_reference_project_imported_metadata(
            tuple(item.image for item in self.entries), list(self.imported_metadata)
        )
        entry_ids = tuple(item.project_entry_id for item in self.entries)
        input_ids_tuple = tuple(item.image.reference_input_id for item in self.entries)
        identities = tuple(item.image.canonical_identity for item in self.entries)
        if (
            len(entry_ids) != len(set(entry_ids))
            or len(input_ids_tuple) != len(set(input_ids_tuple))
            or len(identities) != len(set(identities))
        ):
            raise ValueError("snapshot entries must have unique typed identities")
        if (
            type(self.role_requirements) is not tuple
            or not self.role_requirements
            or any(
                not isinstance(item, ReferenceRoleRequirement) for item in self.role_requirements
            )
        ):
            raise TypeError("role_requirements must be a nonempty typed tuple")
        input_ids = {item.image.reference_input_id for item in self.entries}
        if any(item.reference_input_id not in input_ids for item in self.role_requirements):
            raise ValueError("role requirement references an unknown project input")
        requirement_keys = tuple(
            (item.roi_role, item.role_link_id, item.reference_input_id)
            for item in self.role_requirements
        )
        if len(requirement_keys) != len(set(requirement_keys)):
            raise ValueError("duplicate snapshot role requirement")
        if not isinstance(self.created_at, str) or not self.created_at.strip():
            raise ValueError("created_at must be non-empty")
        if self.predecessor_content_sha256 is not None and (
            not isinstance(self.predecessor_content_sha256, str)
            or len(self.predecessor_content_sha256) != 64
            or any(
                character not in "0123456789abcdef" for character in self.predecessor_content_sha256
            )
        ):
            raise ValueError("predecessor_content_sha256 must be a lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class ReferenceRoiSnapshotResult:
    export: ReferenceRoiExport
    run: QuPathEvidenceRun
    request_sha256: str
    artifact_path: Path
    ownership_path: Path


@dataclass(frozen=True, slots=True)
class ReferenceRoiReviewPublication:
    """One lineage-claimed, project-owned reviewed ROI revision."""

    review: ReferenceRoiReviewResult
    artifact_path: Path
    lineage_claim_path: Path


def _image_mapping(image: ReferenceProjectImage) -> dict[str, object]:
    return {
        "reference_input_id": image.reference_input_id,
        "canonical_image_id": image.canonical_image_id,
        "canonical_pixels_id": image.canonical_pixels_id,
        "source_file_sha256": image.source_file_sha256,
        "source_path": str(image.source_path),
        "width_px": image.width_px,
        "height_px": image.height_px,
        "x_um_per_px": image.x_um_per_px,
        "y_um_per_px": image.y_um_per_px,
        "source_role": image.source_role,
        "source_link_id": image.source_link_id,
    }


def prepare_reference_project(
    root: str | Path,
    *,
    project_id: str,
    images: tuple[ReferenceProjectImage, ...],
    canonical_inputs: tuple[CanonicalReferenceInput, ...],
) -> ReferenceProjectLayout:
    """Materialize a dedicated immutable multi-image reference-project request."""
    if (
        not isinstance(project_id, str)
        or not project_id.strip()
        or project_id != project_id.strip()
    ):
        raise ValueError("project_id must be non-empty text without surrounding whitespace")
    if type(images) is not tuple or not images:
        raise ValueError("images must be a nonempty tuple")
    if any(not isinstance(item, ReferenceProjectImage) for item in images):
        raise TypeError("images must contain ReferenceProjectImage records")
    input_ids = tuple(item.reference_input_id for item in images)
    identities = tuple(item.canonical_identity for item in images)
    if len(input_ids) != len(set(input_ids)):
        raise ValueError("duplicate reference_input_id")
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate canonical image identity")
    imported_metadata = _imported_metadata(images, canonical_inputs)
    project_root = Path(root).resolve()
    request_path = project_root / "data" / "reference-project-request.v1.json"
    if request_path.exists():
        raise ReferenceProjectPreparationError(
            f"reference project request already exists: {request_path}"
        )
    scripts_dir = project_root / "scripts"
    data_dir = project_root / "data"
    exchange_dir = project_root / "exchange"
    for directory in (scripts_dir, data_dir, exchange_dir):
        directory.mkdir(parents=True, exist_ok=True)
    copy_script_assets(scripts_dir)
    relative = Path(QuPathScriptAsset.ANNOTATE_SPECTRAL_REFERENCES.value).relative_to(
        Path("qupath") / "scripts"
    )
    script = scripts_dir / relative
    request = {
        "schema_version": "spectral-reference-project-request/v1",
        "project_id": project_id,
        "project_file": str(project_root / "reference.qpproj"),
        "images": [_image_mapping(item) for item in images],
        "imported_metadata": imported_metadata,
    }
    _write_json_once(request_path, request)
    return ReferenceProjectLayout(
        root=project_root,
        project_file=project_root / "reference.qpproj",
        scripts_dir=scripts_dir,
        data_dir=data_dir,
        exchange_dir=exchange_dir,
        request_path=request_path,
        script_path=script,
    )


def resume_reference_project(
    root: str | Path,
    *,
    project_id: str,
    images: tuple[ReferenceProjectImage, ...],
    canonical_inputs: tuple[CanonicalReferenceInput, ...],
) -> ReferenceProjectLayout:
    """Resume a partial prepare only when its immutable request is byte-semantically exact."""
    imported_metadata = _imported_metadata(images, canonical_inputs)
    project_root = Path(root).resolve()
    request_path = project_root / "data" / "reference-project-request.v1.json"
    expected = {
        "schema_version": "spectral-reference-project-request/v1",
        "project_id": project_id,
        "project_file": str(project_root / "reference.qpproj"),
        "images": [_image_mapping(item) for item in images],
        "imported_metadata": imported_metadata,
    }
    _require_request_mapping(
        request_path,
        expected,
        mismatch_message="partial reference project request does not match exact resume inputs",
    )
    relative = Path(QuPathScriptAsset.ANNOTATE_SPECTRAL_REFERENCES.value).relative_to(
        Path("qupath") / "scripts"
    )
    script = project_root / "scripts" / relative
    if not script.is_file():
        raise ReferenceProjectPreparationError("partial reference project script is missing")
    return ReferenceProjectLayout(
        root=project_root,
        project_file=project_root / "reference.qpproj",
        scripts_dir=project_root / "scripts",
        data_dir=project_root / "data",
        exchange_dir=project_root / "exchange",
        request_path=request_path,
        script_path=script,
    )


def run_reference_project_prepare(
    runner: QuPathRunner,
    layout: ReferenceProjectLayout,
    *,
    images: tuple[ReferenceProjectImage, ...],
    canonical_inputs: tuple[CanonicalReferenceInput, ...],
    section_scopes: tuple[tuple[str, tuple[ReferenceSectionScope, ...]], ...],
) -> ReferenceProjectPrepareResult:
    """Prepare native views using retained metadata and checked source observations."""
    if not isinstance(runner, QuPathRunner):
        raise TypeError("runner must be a QuPathRunner")
    project_id = _project_id_from_request(layout.request_path)
    imported_metadata = _imported_metadata(images, canonical_inputs)
    _require_request_mapping(
        layout.request_path,
        {
            "schema_version": "spectral-reference-project-request/v1",
            "project_id": project_id,
            "project_file": str(layout.project_file),
            "images": [_image_mapping(item) for item in images],
            "imported_metadata": imported_metadata,
        },
        mismatch_message="prepare images differ from the immutable project request",
    )
    before = _source_observations(images, imported_metadata)
    evidence_path = layout.exchange_dir / "prepare-live-evidence.v2.json"
    run = runner.run_evidence_script(
        project_file=layout.project_file,
        script=layout.script_path,
        request_paths=(layout.request_path,),
        log_directory=layout.data_dir,
        require_existing_project=False,
        evidence_path=evidence_path,
    )
    _validate_run_binding(run, layout.script_path, layout.request_path, evidence_path)
    if _source_observations(images, imported_metadata) != before:
        raise ReferenceProjectPreparationError("reference source changed during QuPath invocation")
    entries = loads_reference_project_evidence(
        evidence_path.read_bytes(),
        request_source=layout.request_path.read_bytes(),
        expected_request_sha256=run.request_sha256s[0][1],
        expected_project_id=project_id,
        expected_project_file=layout.project_file,
        expected_images=images,
        section_scopes=section_scopes,
    )
    return ReferenceProjectPrepareResult(entries, run)


def snapshot_reference_rois(
    runner: QuPathRunner,
    layout: ReferenceProjectLayout,
    *,
    project_id: str,
    entries: tuple[ReferenceProjectEntry, ...],
    role_requirements: tuple[ReferenceRoleRequirement, ...],
    project_review_state: str,
    created_at: str,
    predecessor: ReferenceRoiExport | None = None,
) -> ReferenceRoiSnapshotResult:
    """Snapshot live QuPath objects and construct Python-owned typed evidence."""
    images = tuple(item.image for item in entries)
    imported_metadata = _retained_metadata(layout, images)
    _require_request_mapping(
        layout.request_path,
        {
            "schema_version": "spectral-reference-project-request/v1",
            "project_id": project_id,
            "project_file": str(layout.project_file),
            "images": [_image_mapping(item.image) for item in entries],
            "imported_metadata": imported_metadata,
        },
        mismatch_message="snapshot entries differ from the immutable project request",
    )
    if predecessor is not None:
        if not isinstance(predecessor, ReferenceRoiExport):
            raise TypeError("predecessor must be a ReferenceRoiExport")
        if (
            predecessor.project_id != project_id
            or predecessor.project_file != layout.project_file
            or predecessor.entries != entries
        ):
            raise ReferenceProjectPreparationError("snapshot predecessor lineage changed")
    request = ReferenceRoiSnapshotRequest(
        project_id=project_id,
        project_file=layout.project_file,
        project_review_state=project_review_state,
        entries=entries,
        role_requirements=role_requirements,
        created_at=created_at,
        predecessor_content_sha256=(predecessor.content_sha256 if predecessor else None),
        imported_metadata=tuple(imported_metadata),
    )
    if not layout.project_file.is_file():
        raise ReferenceProjectPreparationError("reference project is missing before snapshot")
    mapping = _snapshot_request_mapping(request)
    request_bytes = _json_artifact_bytes(mapping)
    request_sha256 = hashlib.sha256(request_bytes).hexdigest()
    directory = layout.exchange_dir / f"snapshot-{request_sha256}"
    request_path = directory / "request.v1.json"
    evidence_path = directory / "live-evidence.v1.json"
    _write_json_once(request_path, mapping)
    before = _source_observations(images, imported_metadata)
    run = runner.run_evidence_script(
        project_file=layout.project_file,
        script=layout.script_path,
        request_paths=(request_path,),
        log_directory=directory,
        require_existing_project=False,
        evidence_path=evidence_path,
    )
    _validate_run_binding(run, layout.script_path, request_path, evidence_path)
    if _source_observations(images, imported_metadata) != before:
        raise ReferenceProjectPreparationError("reference source changed during QuPath invocation")
    export = loads_reference_roi_live_evidence(
        evidence_path.read_bytes(),
        request,
        expected_evidence_sha256=run.evidence_sha256,
        expected_request_sha256=request_sha256,
    )
    _claim_lineage(layout, request.predecessor_content_sha256, export.content_sha256)
    artifact_path = layout.exchange_dir / "snapshots" / f"{export.content_sha256}.json"
    _publish_export_artifact(artifact_path, export)
    ownership_path = layout.exchange_dir / "snapshots" / f"{export.content_sha256}.ownership.json"
    ownership = {
        "schema_version": "spectral-reference-snapshot-ownership/v1",
        "project_id": export.project_id,
        "project_file": str(export.project_file),
        "export_content_sha256": export.content_sha256,
        "artifact_path": str(artifact_path),
        "request_path": str(request_path),
        "request_sha256": request_sha256,
        "evidence_path": str(run.evidence_path),
        "evidence_sha256": run.evidence_sha256,
        "script_path": str(layout.script_path),
        "script_sha256": run.script_sha256,
        "project_entry_ids": [item.project_entry_id for item in export.entries],
        "runner_executable_version": run.record.executable_version,
        "runner_command": list(run.record.command),
    }
    ownership["content_sha256"] = typed_checksum_sha256(ownership)
    _publish_json_artifact(ownership_path, ownership)
    return ReferenceRoiSnapshotResult(export, run, request_sha256, artifact_path, ownership_path)


def publish_reference_roi_review(
    layout: ReferenceProjectLayout,
    *,
    snapshot: ReferenceRoiExport,
    decisions: tuple[ReferenceRoiReviewDecision, ...],
    reviewer_id: str,
    reviewed_at: str,
) -> ReferenceRoiReviewPublication:
    """Claim and publish exactly one reviewed child for a project-owned predecessor."""
    if not isinstance(layout, ReferenceProjectLayout):
        raise TypeError("layout must be a ReferenceProjectLayout")
    if not isinstance(snapshot, ReferenceRoiExport):
        raise TypeError("snapshot must be a ReferenceRoiExport")
    if type(decisions) is not tuple or not decisions:
        raise ValueError("authoritative review publication requires at least one decision")
    if snapshot.project_file != layout.project_file:
        raise ReferenceProjectPreparationError(
            "review snapshot belongs to another reference project"
        )
    _require_request_mapping(
        layout.request_path,
        {
            "schema_version": "spectral-reference-project-request/v1",
            "project_id": snapshot.project_id,
            "project_file": str(layout.project_file),
            "images": [_image_mapping(item.image) for item in snapshot.entries],
            "imported_metadata": _retained_metadata(layout, tuple(item.image for item in snapshot.entries)),
        },
        mismatch_message="review snapshot entries differ from the immutable project request",
    )
    _require_project_owned_export(layout, snapshot)
    review = accept_reference_roi_snapshot(
        snapshot,
        decisions=decisions,
        reviewer_id=reviewer_id,
        reviewed_at=reviewed_at,
    )
    if tuple(item.review_state for item in review.export.rois) == tuple(
        item.review_state for item in snapshot.rois
    ):
        raise ReferenceProjectPreparationError(
            "authoritative review must apply at least one current non-stale decision"
        )
    child = review.export
    _claim_lineage(layout, snapshot.content_sha256, child.content_sha256)
    claim_path = _lineage_claim_path(layout, snapshot.content_sha256)
    artifact_path = layout.exchange_dir / "reviews" / f"{child.content_sha256}.json"
    _publish_export_artifact(artifact_path, child)
    return ReferenceRoiReviewPublication(review, artifact_path, claim_path)


def loads_reference_roi_live_evidence(
    source: str | bytes,
    request: ReferenceRoiSnapshotRequest,
    *,
    expected_evidence_sha256: str,
    expected_request_sha256: str,
) -> ReferenceRoiExport:
    """Strictly validate live-only QuPath evidence and bind trusted image facts."""
    encoded = source.encode("utf-8") if isinstance(source, str) else source
    if hashlib.sha256(encoded).hexdigest() != expected_evidence_sha256:
        raise ReferenceProjectPreparationError("live ROI evidence runner digest changed")
    if (
        hashlib.sha256(_json_artifact_bytes(_snapshot_request_mapping(request))).hexdigest()
        != expected_request_sha256
    ):
        raise ReferenceProjectPreparationError("live ROI request digest changed")
    try:
        root = json.loads(
            source,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except json.JSONDecodeError as exc:
        raise ReferenceProjectPreparationError("live ROI evidence is invalid JSON") from exc
    _exact_fields(
        root,
        {
            "schema_version",
            "project_id",
            "project_file",
            "request_sha256",
            "entries",
            "rois",
            "content_sha256",
        },
        "live ROI evidence",
    )
    content_sha256 = root["content_sha256"]
    if (
        not isinstance(content_sha256, str)
        or typed_checksum_sha256(
            {key: item for key, item in root.items() if key != "content_sha256"}
        )
        != content_sha256
    ):
        raise ReferenceProjectPreparationError("live ROI evidence content SHA-256 changed")
    if (
        root["schema_version"] != "spectral-reference-roi-live-evidence/v1"
        or root["project_id"] != request.project_id
        or Path(root["project_file"]) != request.project_file
        or root["request_sha256"] != expected_request_sha256
    ):
        raise ReferenceProjectPreparationError("live ROI evidence project identity changed")
    raw_entries = root["entries"]
    raw_rois = root["rois"]
    if not isinstance(raw_entries, list) or not isinstance(raw_rois, list):
        raise ReferenceProjectPreparationError("live ROI entries and rois must be arrays")
    expected_by_input = {item.image.reference_input_id: item for item in request.entries}
    live_by_entry_id: dict[str, ReferenceProjectEntry] = {}
    for raw in raw_entries:
        _exact_fields(
            raw,
            {
                "reference_input_id",
                "project_entry_id",
                "project_entry_path",
                "source_uri",
                "canonical_image_id",
                "canonical_pixels_id",
                "width_px",
                "height_px",
                "x_um_per_px",
                "y_um_per_px",
                "section_candidates",
            },
            "live reference entry",
        )
        expected = expected_by_input.get(raw["reference_input_id"])
        if expected is None:
            raise ReferenceProjectPreparationError("live reference input is unknown")
        _validate_live_image_fields(raw, expected)
        if raw["section_candidates"] != [
            _scope_mapping(scope) for scope in expected.section_candidates
        ]:
            raise ReferenceProjectPreparationError("live reference section evidence changed")
        if raw["project_entry_id"] in live_by_entry_id:
            raise ReferenceProjectPreparationError("duplicate live project-entry identity")
        live_by_entry_id[raw["project_entry_id"]] = expected
    if len(live_by_entry_id) != len(request.entries):
        raise ReferenceProjectPreparationError("live project-entry inventory changed")
    rois: list[ReferenceRoiRecord] = []
    for raw in raw_rois:
        _exact_fields(
            raw,
            {
                "roi_id",
                "path_object_id",
                "project_entry_id",
                "roi_role",
                "role_link_id",
                "section_id",
                "coordinate_system",
                "geometry",
                "review_state",
                "reviewer_id",
                "reviewed_at",
            },
            "live ROI",
        )
        entry = live_by_entry_id.get(raw["project_entry_id"])
        if entry is None:
            raise ReferenceProjectPreparationError("live ROI references unknown entry")
        geometry_json = _canonical_bytes(raw["geometry"]).decode("utf-8")
        rois.append(
            ReferenceRoiRecord(
                canonical_image_id=entry.image.canonical_image_id,
                source_file_sha256=entry.image.source_file_sha256,
                geometry_json=geometry_json,
                geometry_sha256=hashlib.sha256(geometry_json.encode("utf-8")).hexdigest(),
                **{key: item for key, item in raw.items() if key != "geometry"},
            )
        )
    return ReferenceRoiExport.create(
        project_id=request.project_id,
        project_file=request.project_file,
        project_review_state=request.project_review_state,
        entries=request.entries,
        rois=tuple(rois),
        role_requirements=request.role_requirements,
        created_at=request.created_at,
        predecessor_content_sha256=request.predecessor_content_sha256,
    )


def _scope_mapping(scope: ReferenceSectionScope) -> dict[str, object]:
    return {
        "section_id": scope.section_id,
        "scope_kind": scope.scope_kind,
        "tissue_path_object_id": scope.tissue_path_object_id,
        "section_geometry_json": scope.section_geometry_json,
        "section_geometry_sha256": scope.section_geometry_sha256,
        "review_state": scope.review_state,
    }


def _snapshot_request_mapping(request: ReferenceRoiSnapshotRequest) -> dict[str, object]:
    return {
        "schema_version": "spectral-reference-roi-snapshot-request/v1",
        "project_id": request.project_id,
        "project_file": str(request.project_file),
        "project_review_state": request.project_review_state,
        "imported_metadata": list(request.imported_metadata),
        "entries": [
            {
                "reference_input_id": entry.image.reference_input_id,
                "project_entry_id": entry.project_entry_id,
                "project_entry_path": str(entry.project_entry_path),
                "source_uri": entry.image.source_path.as_uri(),
                "canonical_image_id": entry.image.canonical_image_id,
                "canonical_pixels_id": entry.image.canonical_pixels_id,
                "width_px": entry.image.width_px,
                "height_px": entry.image.height_px,
                "x_um_per_px": entry.image.x_um_per_px,
                "y_um_per_px": entry.image.y_um_per_px,
                "source_role": entry.image.source_role,
                "source_link_id": entry.image.source_link_id,
                "section_candidates": [_scope_mapping(scope) for scope in entry.section_candidates],
            }
            for entry in request.entries
        ],
        "role_requirements": [
            {
                "roi_role": item.roi_role,
                "role_link_id": item.role_link_id,
                "reference_input_id": item.reference_input_id,
                "minimum_count": item.minimum_count,
            }
            for item in request.role_requirements
        ],
        "created_at": request.created_at,
        "predecessor_content_sha256": request.predecessor_content_sha256,
    }


def _validate_live_image_fields(raw: dict[str, object], expected: ReferenceProjectEntry) -> None:
    for name in (
        "reference_input_id",
        "project_entry_id",
        "project_entry_path",
        "source_uri",
        "canonical_image_id",
        "canonical_pixels_id",
    ):
        if not isinstance(raw[name], str) or not raw[name].strip():
            raise ReferenceProjectPreparationError(f"live {name} must be non-empty text")
    for name in ("width_px", "height_px"):
        if isinstance(raw[name], bool) or not isinstance(raw[name], int):
            raise ReferenceProjectPreparationError(f"live {name} must be an integer")
    for name in ("x_um_per_px", "y_um_per_px"):
        if (
            isinstance(raw[name], bool)
            or not isinstance(raw[name], (int, float))
            or not math.isfinite(float(raw[name]))
        ):
            raise ReferenceProjectPreparationError(f"live {name} must be finite")
    image = expected.image
    if (
        raw["project_entry_id"] != expected.project_entry_id
        or Path(raw["project_entry_path"]) != expected.project_entry_path
        or raw["source_uri"] != image.source_path.as_uri()
        or raw["canonical_image_id"] != image.canonical_image_id
        or raw["canonical_pixels_id"] != image.canonical_pixels_id
        or raw["width_px"] != image.width_px
        or raw["height_px"] != image.height_px
        or abs(float(raw["x_um_per_px"]) - image.x_um_per_px) > 1e-9
        or abs(float(raw["y_um_per_px"]) - image.y_um_per_px) > 1e-9
    ):
        raise ReferenceProjectPreparationError("live canonical image evidence changed")


def _imported_metadata(
    images: tuple[ReferenceProjectImage, ...],
    canonical_inputs: tuple[CanonicalReferenceInput, ...],
) -> list[dict[str, object]]:
    """Create one checked display projection for each retained imported reference."""
    if type(canonical_inputs) is not tuple or len(canonical_inputs) != len(images):
        raise ValueError("canonical_inputs must match every reference image")
    return [reference_project_imported_metadata(image, value)
            for image, value in zip(images, canonical_inputs, strict=True)]


def _retained_metadata(
    layout: ReferenceProjectLayout, images: tuple[ReferenceProjectImage, ...],
) -> list[dict[str, object]]:
    """Read the immutable prepare request, retaining imported authority for later snapshots."""
    value = _load_json_strict(layout.request_path)
    metadata = value.get("imported_metadata")
    validate_reference_project_imported_metadata(images, metadata)
    return metadata


def _source_observations(images, metadata) -> tuple:
    """Check imported source observations without hashing or decoding image pixels again."""
    observations = []
    for image, value in zip(images, metadata, strict=True):
        stat = image.source_path.stat()
        expected = value["source_observations"]
        actual = (stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino)
        if actual != tuple(expected[name] for name in
                           ("size_bytes", "modified_time_ns", "device_id", "inode_id")):
            raise ReferenceProjectPreparationError("imported reference source file observations changed")
        observations.append(actual)
    return tuple(observations)


def _validate_run_binding(
    run: QuPathEvidenceRun,
    script_path: Path,
    request_path: Path,
    evidence_path: Path,
) -> None:
    expected_script = script_path.resolve()
    expected_request = request_path.resolve()
    expected_evidence = evidence_path.resolve()
    if (
        run.evidence_path != expected_evidence
        or run.evidence_sha256 != _file_sha256(expected_evidence)
        or run.script_sha256 != _file_sha256(expected_script)
        or run.request_sha256s != ((expected_request, _file_sha256(expected_request)),)
    ):
        raise ReferenceProjectPreparationError(
            "QuPath evidence run is not bound to the exact script/request/output"
        )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _project_id_from_request(path: Path) -> str:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReferenceProjectPreparationError("reference project request is unreadable") from exc
    if not isinstance(value, dict) or not isinstance(value.get("project_id"), str):
        raise ReferenceProjectPreparationError("reference project request identity is invalid")
    return value["project_id"]


def _require_request_mapping(
    path: Path,
    expected: dict[str, object],
    *,
    mismatch_message: str,
) -> None:
    try:
        actual = json.loads(
            path.read_bytes(),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ReferenceProjectPreparationError("reference project request is unreadable") from exc
    if actual != expected:
        raise ReferenceProjectPreparationError(mismatch_message)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ReferenceProjectPreparationError(f"duplicate JSON field {key!r}")
        result[key] = value
    return result


def _reject_nonfinite(value: str):
    raise ReferenceProjectPreparationError(f"non-finite JSON number {value!r}")


def _exact_fields(value: object, expected: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ReferenceProjectPreparationError(f"{label} schema fields differ")
    return value


def _write_json_once(path: Path, value: object) -> None:
    if path.exists():
        raise ReferenceProjectPreparationError(f"artifact already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    ) as stream:
        temporary = Path(stream.name)
        stream.write(_json_artifact_bytes(value))
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.link(temporary, path)
    except FileExistsError as exc:
        raise ReferenceProjectPreparationError(f"artifact already exists: {path}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _json_artifact_bytes(value: object) -> bytes:
    return (json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _claim_lineage(
    layout: ReferenceProjectLayout,
    predecessor_content_sha256: str | None,
    child_content_sha256: str,
) -> None:
    claim_path = _lineage_claim_path(layout, predecessor_content_sha256)
    claim = {
        "predecessor_content_sha256": predecessor_content_sha256,
        "child_content_sha256": child_content_sha256,
    }
    try:
        _write_json_once(claim_path, claim)
    except ReferenceProjectPreparationError:
        try:
            existing = json.loads(claim_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReferenceProjectPreparationError("reference lineage claim is unreadable") from exc
        if existing != claim:
            raise ReferenceProjectPreparationError("reference lineage fork is forbidden")


def _lineage_claim_path(
    layout: ReferenceProjectLayout,
    predecessor_content_sha256: str | None,
) -> Path:
    lineage_key = predecessor_content_sha256 or "genesis"
    return layout.exchange_dir / "lineage" / f"{lineage_key}.json"


def _publish_export_artifact(path: Path, export: ReferenceRoiExport) -> None:
    encoded = dumps_reference_roi_export(export).encode("utf-8")
    if path.exists():
        _require_matching_review_artifact(path, export)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    ) as stream:
        temporary = Path(stream.name)
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.link(temporary, path)
    except FileExistsError:
        _require_matching_review_artifact(path, export)
    finally:
        temporary.unlink(missing_ok=True)


def _publish_json_artifact(path: Path, value: dict[str, object]) -> None:
    if path.exists():
        if _load_json_strict(path) != value:
            raise ReferenceProjectPreparationError("project-owned JSON artifact differs")
        return
    try:
        _write_json_once(path, value)
    except ReferenceProjectPreparationError:
        if _load_json_strict(path) != value:
            raise ReferenceProjectPreparationError("project-owned JSON artifact differs")


def _load_json_strict(path: Path) -> dict[str, object]:
    try:
        value = json.loads(
            path.read_bytes(),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ReferenceProjectPreparationError("project-owned JSON artifact is unreadable") from exc
    if not isinstance(value, dict):
        raise ReferenceProjectPreparationError("project-owned JSON artifact must be an object")
    return value


def _require_project_owned_export(
    layout: ReferenceProjectLayout,
    export: ReferenceRoiExport,
    *,
    seen: frozenset[str] = frozenset(),
) -> None:
    if export.content_sha256 in seen:
        raise ReferenceProjectPreparationError("reference lineage contains a cycle")
    snapshot_path = layout.exchange_dir / "snapshots" / f"{export.content_sha256}.json"
    review_path = layout.exchange_dir / "reviews" / f"{export.content_sha256}.json"
    existing = tuple(path for path in (snapshot_path, review_path) if path.is_file())
    if len(existing) != 1:
        raise ReferenceProjectPreparationError(
            "review input is not one exact project-owned artifact"
        )
    _require_matching_review_artifact(existing[0], export)
    _require_lineage_claim(
        layout,
        export.predecessor_content_sha256,
        export.content_sha256,
    )
    if existing[0] == snapshot_path:
        _require_snapshot_ownership(layout, export, snapshot_path)
    elif export.predecessor_content_sha256 is None:
        raise ReferenceProjectPreparationError(
            "project-owned review artifact must name its predecessor"
        )
    if export.predecessor_content_sha256 is not None:
        parent = _load_project_owned_export(layout, export.predecessor_content_sha256)
        _require_project_owned_export(
            layout,
            parent,
            seen=seen | {export.content_sha256},
        )


def _load_project_owned_export(
    layout: ReferenceProjectLayout,
    content_sha256: str,
) -> ReferenceRoiExport:
    paths = (
        layout.exchange_dir / "snapshots" / f"{content_sha256}.json",
        layout.exchange_dir / "reviews" / f"{content_sha256}.json",
    )
    existing = tuple(path for path in paths if path.is_file())
    if len(existing) != 1:
        raise ReferenceProjectPreparationError(
            "reference lineage predecessor is missing or ambiguous"
        )
    try:
        export = loads_reference_roi_export(existing[0].read_bytes())
    except (OSError, ValueError) as exc:
        raise ReferenceProjectPreparationError("reference lineage predecessor is invalid") from exc
    if export.content_sha256 != content_sha256:
        raise ReferenceProjectPreparationError("reference lineage predecessor digest changed")
    return export


def load_project_owned_reference_roi_export(
    layout: ReferenceProjectLayout,
    artifact_path: str | Path,
) -> ReferenceRoiExport:
    """Reload one exact project-owned ROI export and its complete lineage."""

    if type(layout) is not ReferenceProjectLayout:
        raise TypeError("project-owned ROI loading requires exact project layout")
    supplied = Path(artifact_path)
    if not supplied.is_absolute():
        raise ReferenceProjectPreparationError("project-owned ROI artifact path must be absolute")
    digest = supplied.stem
    if (
        supplied.suffix != ".json"
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ReferenceProjectPreparationError(
            "project-owned ROI artifact path does not name an export digest"
        )
    expected = layout.exchange_dir / supplied.parent.name / supplied.name
    if supplied != expected or supplied.parent.name not in {"snapshots", "reviews"}:
        raise ReferenceProjectPreparationError(
            "ROI export is outside the exact project-owned exchange"
        )
    export = _load_project_owned_export(layout, digest)
    _require_project_owned_export(layout, export)
    return export


def _require_lineage_claim(
    layout: ReferenceProjectLayout,
    predecessor_content_sha256: str | None,
    child_content_sha256: str,
) -> None:
    claim = _load_json_strict(_lineage_claim_path(layout, predecessor_content_sha256))
    expected = {
        "predecessor_content_sha256": predecessor_content_sha256,
        "child_content_sha256": child_content_sha256,
    }
    if claim != expected:
        raise ReferenceProjectPreparationError("reference lineage claim does not match exact child")


def _require_snapshot_ownership(
    layout: ReferenceProjectLayout,
    export: ReferenceRoiExport,
    artifact_path: Path,
) -> None:
    ownership_path = layout.exchange_dir / "snapshots" / f"{export.content_sha256}.ownership.json"
    ownership = _load_json_strict(ownership_path)
    _exact_fields(
        ownership,
        {
            "schema_version",
            "project_id",
            "project_file",
            "export_content_sha256",
            "artifact_path",
            "request_path",
            "request_sha256",
            "evidence_path",
            "evidence_sha256",
            "script_path",
            "script_sha256",
            "project_entry_ids",
            "runner_executable_version",
            "runner_command",
            "content_sha256",
        },
        "snapshot ownership",
    )
    declared = ownership["content_sha256"]
    payload = {key: value for key, value in ownership.items() if key != "content_sha256"}
    if not isinstance(declared, str) or typed_checksum_sha256(payload) != declared:
        raise ReferenceProjectPreparationError("snapshot ownership checksum changed")
    request_path = Path(ownership["request_path"])
    evidence_path = Path(ownership["evidence_path"])
    script_path = Path(ownership["script_path"])
    if (
        ownership["schema_version"] != "spectral-reference-snapshot-ownership/v1"
        or ownership["project_id"] != export.project_id
        or Path(ownership["project_file"]) != layout.project_file
        or ownership["export_content_sha256"] != export.content_sha256
        or Path(ownership["artifact_path"]) != artifact_path
        or script_path != layout.script_path
        or ownership["project_entry_ids"] != [item.project_entry_id for item in export.entries]
        or not isinstance(ownership["runner_executable_version"], str)
        or not ownership["runner_executable_version"]
    ):
        raise ReferenceProjectPreparationError("snapshot ownership identity changed")
    for path, digest, label in (
        (request_path, ownership["request_sha256"], "request"),
        (evidence_path, ownership["evidence_sha256"], "evidence"),
        (script_path, ownership["script_sha256"], "script"),
    ):
        if not isinstance(digest, str) or not path.is_file() or _file_sha256(path) != digest:
            raise ReferenceProjectPreparationError(f"snapshot ownership {label} digest changed")
    expected_request_dir = layout.exchange_dir / f"snapshot-{ownership['request_sha256']}"
    if (
        request_path != expected_request_dir / "request.v1.json"
        or evidence_path != expected_request_dir / "live-evidence.v1.json"
    ):
        raise ReferenceProjectPreparationError("snapshot ownership exchange paths changed")
    request = ReferenceRoiSnapshotRequest(
        project_id=export.project_id,
        project_file=export.project_file,
        project_review_state=export.project_review_state,
        entries=export.entries,
        role_requirements=export.role_requirements,
        created_at=export.created_at,
        predecessor_content_sha256=export.predecessor_content_sha256,
        imported_metadata=tuple(_retained_metadata(layout, tuple(item.image for item in export.entries))),
    )
    _require_request_mapping(
        request_path,
        _snapshot_request_mapping(request),
        mismatch_message="project-owned snapshot request changed",
    )
    reconstructed = loads_reference_roi_live_evidence(
        evidence_path.read_bytes(),
        request,
        expected_evidence_sha256=ownership["evidence_sha256"],
        expected_request_sha256=ownership["request_sha256"],
    )
    if reconstructed != export:
        raise ReferenceProjectPreparationError("runner evidence does not reconstruct snapshot")
    command = ownership["runner_command"]
    expected_tail = [
        "script",
        "--args",
        str(request_path),
        "--args",
        str(evidence_path),
        str(layout.script_path),
    ]
    # The runner owns launcher/profile options. Reopening binds the actual
    # script and its exact request/output arguments, independent of that prefix.
    if (not isinstance(command, list) or len(command) <= len(expected_tail)
            or command[-len(expected_tail):] != expected_tail):
        raise ReferenceProjectPreparationError("snapshot runner command binding changed")



def _require_matching_review_artifact(path: Path, export: ReferenceRoiExport) -> None:
    try:
        encoded = path.read_bytes()
        existing = loads_reference_roi_export(encoded)
    except (OSError, ValueError) as exc:
        raise ReferenceProjectPreparationError(
            "claimed reference review artifact is unreadable or invalid"
        ) from exc
    if existing != export or encoded != dumps_reference_roi_export(export).encode("utf-8"):
        raise ReferenceProjectPreparationError("claimed reference review artifact content differs")


__all__ = [
    "ReferenceProjectLayout",
    "ReferenceProjectPreparationError",
    "ReferenceProjectPrepareResult",
    "ReferenceRoiSnapshotRequest",
    "ReferenceRoiSnapshotResult",
    "ReferenceRoiReviewPublication",
    "loads_reference_roi_live_evidence",
    "load_project_owned_reference_roi_export",
    "prepare_reference_project",
    "publish_reference_roi_review",
    "run_reference_project_prepare",
    "resume_reference_project",
    "snapshot_reference_rois",
]
