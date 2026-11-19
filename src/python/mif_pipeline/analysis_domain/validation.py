"""Validation for immutable analysis-domain manifests."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence

from mif_pipeline.pipeline_core import AnalysisDomainKind, analysis_domain_id

from .models import (
    ActiveAnalysisDomainAuthority,
    AnalysisDomainError,
    AnalysisDomainManifest,
    AnalysisDomainRevision,
    DomainGeometryEvidence,
    DomainOverlapEvidence,
    MAX_ANALYSIS_DOMAINS_PER_MANIFEST,
    MAX_ANALYSIS_DOMAIN_REVISIONS_PER_MANIFEST,
)

_GEOMETRY_DIGEST_POLICY = "jts-normalized-wkb-sha256/v1"
_MANIFEST_SCHEMA = "analysis-domain/v1"
_MAX_NORMALIZED_WKB_BYTES = 16 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")


# ==============================================================================
#                                   Validation
# ==============================================================================


def _nonblank(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AnalysisDomainError(f"{field} must be a non-empty string")
    return value


def _sha256(value: object, field: str) -> str:
    result = _nonblank(value, field)
    if _SHA256.fullmatch(result) is None:
        raise AnalysisDomainError(f"{field} must be a lowercase SHA-256")
    return result


def _validate_manifest_scope(manifest: AnalysisDomainManifest) -> None:
    if manifest.schema_version != _MANIFEST_SCHEMA:
        raise AnalysisDomainError("analysis-domain manifest schema version is unsupported")
    _nonblank(manifest.canonical_image_id, "canonical image ID")
    _sha256(manifest.canonical_image_sha256, "canonical image SHA-256")
    for field in (
        "project_entry_id",
        "slide_id",
        "section_id",
        "tissue_generation_id",
        "tissue_roi_id",
        "tissue_path_object_id",
    ):
        _nonblank(getattr(manifest, field), field)
    if not manifest.revisions:
        raise AnalysisDomainError("analysis-domain manifest must contain a revision")
    if len(manifest.revisions) > MAX_ANALYSIS_DOMAIN_REVISIONS_PER_MANIFEST:
        raise AnalysisDomainError("analysis-domain manifest exceeds its total revision limit")


def _validate_revision_scope(
    manifest: AnalysisDomainManifest, revision: AnalysisDomainRevision
) -> None:
    if not isinstance(revision, AnalysisDomainRevision):
        raise AnalysisDomainError("manifest revisions must be AnalysisDomainRevision records")
    if (
        revision.canonical_image_id != manifest.canonical_image_id
        or revision.canonical_image_sha256 != manifest.canonical_image_sha256
    ):
        raise AnalysisDomainError("domain revision has stale canonical image identity")
    if revision.project_entry_id != manifest.project_entry_id:
        raise AnalysisDomainError("domain revision has an exact project entry mismatch")
    if revision.ref.slide_id != manifest.slide_id:
        raise AnalysisDomainError("domain revision has a slide mismatch")
    if revision.ref.section_id != manifest.section_id:
        raise AnalysisDomainError("domain revision has a section mismatch")
    if revision.ref.tissue_generation_id != manifest.tissue_generation_id:
        raise AnalysisDomainError("domain revision has a stale tissue generation")
    if (
        revision.tissue_roi_id != manifest.tissue_roi_id
        or revision.tissue_path_object_id != manifest.tissue_path_object_id
    ):
        raise AnalysisDomainError(
            "domain revision does not match the accepted TissueSection identity"
        )
    for field in (
        "tissue_roi_id",
        "tissue_path_object_id",
        "source_path_object_id",
    ):
        _nonblank(getattr(revision, field), field)
    expected_id = analysis_domain_id(
        kind=revision.ref.kind,
        canonical_image_sha256=manifest.canonical_image_sha256,
        tissue_generation_id=manifest.tissue_generation_id,
        section_id=manifest.section_id,
        source_path_object_id=revision.source_path_object_id,
    )
    if revision.ref.analysis_domain_id != expected_id:
        raise AnalysisDomainError("analysis_domain_id does not match its bound identity")


def _validate_revision_geometry(revision: AnalysisDomainRevision) -> None:
    if revision.geometry_digest_policy != _GEOMETRY_DIGEST_POLICY:
        raise AnalysisDomainError("geometry digest policy is unsupported")
    if revision.ref.kind is AnalysisDomainKind.WHOLE_TISSUE_SECTION:
        if revision.source_path_object_id != revision.tissue_path_object_id:
            raise AnalysisDomainError("whole-tissue domain must reference its TissueSection")
        if revision.geometry_reference_path_object_id != revision.tissue_path_object_id:
            raise AnalysisDomainError("whole-tissue geometry reference must be the TissueSection")
        if revision.normalized_wkb is not None or revision.geometry_sha256 is not None:
            raise AnalysisDomainError("whole-tissue domain must not copy geometry")
        return
    if revision.ref.kind is not AnalysisDomainKind.SELECTED_ROI:
        raise AnalysisDomainError("domain kind is unsupported")
    if revision.geometry_reference_path_object_id is not None:
        raise AnalysisDomainError("selected ROI must carry geometry rather than a reference")
    if not isinstance(revision.normalized_wkb, bytes) or not revision.normalized_wkb:
        raise AnalysisDomainError("selected ROI normalized WKB must be nonempty bytes")
    if len(revision.normalized_wkb) > _MAX_NORMALIZED_WKB_BYTES:
        raise AnalysisDomainError("selected ROI normalized WKB exceeds the bounded limit")
    digest = _sha256(revision.geometry_sha256, "geometry SHA-256")
    if hashlib.sha256(revision.normalized_wkb).hexdigest() != digest:
        raise AnalysisDomainError("geometry SHA-256 does not match normalized WKB bytes")


def _active_revisions(
    manifest: AnalysisDomainManifest,
) -> tuple[AnalysisDomainRevision, ...]:
    grouped: dict[str, list[AnalysisDomainRevision]] = {}
    for revision in manifest.revisions:
        _validate_revision_scope(manifest, revision)
        _validate_revision_geometry(revision)
        grouped.setdefault(revision.ref.analysis_domain_id, []).append(revision)
    if len(grouped) > MAX_ANALYSIS_DOMAINS_PER_MANIFEST:
        raise AnalysisDomainError("analysis-domain manifest exceeds its bounded inventory")

    active: list[AnalysisDomainRevision] = []
    for revisions in grouped.values():
        numbers = [item.revision for item in revisions]
        if any(
            isinstance(number, bool) or not isinstance(number, int) or number < 1
            for number in numbers
        ):
            raise AnalysisDomainError("domain revision must be a positive integer")
        if len(numbers) != len(set(numbers)):
            raise AnalysisDomainError("analysis-domain revision fork is not permitted")
        ordered = sorted(revisions, key=lambda item: item.revision)
        first = ordered[0]
        if first.revision != 1 or first.predecessor_sha256 is not None:
            raise AnalysisDomainError("analysis-domain revision lineage must start at revision 1")
        for previous, current in zip(ordered, ordered[1:]):
            if current.revision != previous.revision + 1:
                raise AnalysisDomainError("analysis-domain revisions must be monotonic")
            if current.predecessor_sha256 != previous.content_sha256:
                raise AnalysisDomainError("analysis-domain revision predecessor is stale")
            if (
                current.ref != first.ref
                or current.source_path_object_id != first.source_path_object_id
            ):
                raise AnalysisDomainError("analysis-domain revision changed stable identity")
        active.append(ordered[-1])

    source_ids = [item.source_path_object_id for item in active]
    if len(source_ids) != len(set(source_ids)):
        raise AnalysisDomainError("manifest contains a duplicate PathObject UUID")
    kinds = {item.ref.kind for item in active}
    if kinds == {AnalysisDomainKind.WHOLE_TISSUE_SECTION} and len(active) != 1:
        raise AnalysisDomainError("whole-tissue selection must contain exactly one domain")
    if len(kinds) != 1:
        raise AnalysisDomainError("one section cannot mix whole-tissue and selected domains")
    return tuple(active)


def _validate_evidence_scope(
    manifest: AnalysisDomainManifest, evidence: DomainOverlapEvidence
) -> None:
    if not isinstance(evidence, DomainOverlapEvidence):
        raise AnalysisDomainError("overlap evidence must be a DomainOverlapEvidence record")
    if evidence.canonical_image_id != manifest.canonical_image_id:
        raise AnalysisDomainError("overlap evidence has a canonical image ID mismatch")
    if evidence.canonical_image_sha256 != manifest.canonical_image_sha256:
        raise AnalysisDomainError("overlap evidence has a stale canonical image")
    if evidence.project_entry_id != manifest.project_entry_id:
        raise AnalysisDomainError("overlap evidence has an exact project entry mismatch")
    if evidence.section_id != manifest.section_id:
        raise AnalysisDomainError("overlap evidence has a section mismatch")
    if evidence.tissue_generation_id != manifest.tissue_generation_id:
        raise AnalysisDomainError("overlap evidence has a stale tissue generation")
    if (
        evidence.tissue_roi_id != manifest.tissue_roi_id
        or evidence.tissue_path_object_id != manifest.tissue_path_object_id
    ):
        raise AnalysisDomainError(
            "overlap evidence does not match the accepted TissueSection identity"
        )
    if evidence.generated_by_spatial_index is not True or evidence.exact_jts_area_test is not True:
        raise AnalysisDomainError(
            "overlap evidence must be spatial-index-produced with an exact JTS area test"
        )


def _validate_geometry_evidence(
    manifest: AnalysisDomainManifest,
    revision: AnalysisDomainRevision,
    evidence: DomainGeometryEvidence,
) -> None:
    """Validate geometry evidence for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    if (
        evidence.tissue_roi_id != manifest.tissue_roi_id
        or evidence.tissue_path_object_id != manifest.tissue_path_object_id
        or evidence.parent_path_object_id != manifest.tissue_path_object_id
    ):
        raise AnalysisDomainError(
            "geometry evidence does not match the accepted TissueSection exact tissue parent"
        )
    if evidence.covered_by_parent is not True:
        raise AnalysisDomainError("selected ROI must be covered by its exact tissue parent")
    if evidence.geometry_digest_policy != _GEOMETRY_DIGEST_POLICY:
        raise AnalysisDomainError("geometry evidence uses an unsupported digest policy")
    if evidence.geometry_sha256 != revision.geometry_sha256:
        raise AnalysisDomainError("geometry SHA-256 differs from the domain revision")
    if evidence.normalized_wkb != revision.normalized_wkb:
        raise AnalysisDomainError("normalized WKB differs from the domain revision")
    if not isinstance(evidence.normalized_wkb, bytes) or not evidence.normalized_wkb:
        raise AnalysisDomainError("geometry evidence normalized WKB must be nonempty")
    if hashlib.sha256(evidence.normalized_wkb).hexdigest() != evidence.geometry_sha256:
        raise AnalysisDomainError("geometry SHA-256 does not match exact normalized WKB bytes")
    if evidence.is_valid is not True:
        raise AnalysisDomainError("selected ROI geometry must be valid")
    if evidence.is_empty is not False:
        raise AnalysisDomainError("selected ROI geometry must be nonempty")
    if evidence.is_finite is not True:
        raise AnalysisDomainError("selected ROI geometry must be finite")
    if (
        isinstance(evidence.area, bool)
        or not isinstance(evidence.area, (int, float))
        or not math.isfinite(float(evidence.area))
    ):
        raise AnalysisDomainError("selected ROI geometry area must be finite")
    if float(evidence.area) <= 0:
        raise AnalysisDomainError("selected ROI geometry must have positive area")


def validate_domain_manifest(
    manifest: AnalysisDomainManifest,
    overlap_evidence: DomainOverlapEvidence,
) -> AnalysisDomainManifest:
    """Validate a domain manifest against exact QuPath/JTS overlap evidence."""
    if not isinstance(manifest, AnalysisDomainManifest):
        raise AnalysisDomainError("manifest must be an AnalysisDomainManifest")
    _validate_manifest_scope(manifest)
    active = _active_revisions(manifest)
    _validate_evidence_scope(manifest, overlap_evidence)

    selected = tuple(item for item in active if item.ref.kind is AnalysisDomainKind.SELECTED_ROI)
    evidence_ids = [item.path_object_id for item in overlap_evidence.geometry_evidence]
    if len(evidence_ids) != len(set(evidence_ids)):
        raise AnalysisDomainError("overlap evidence contains a duplicate PathObject UUID")
    expected_ids = {item.source_path_object_id for item in selected}
    if set(evidence_ids) != expected_ids or len(evidence_ids) != len(expected_ids):
        raise AnalysisDomainError("geometry evidence must cover the exact active domain inventory")
    revisions_by_source = {item.source_path_object_id: item for item in selected}
    for evidence in overlap_evidence.geometry_evidence:
        _validate_geometry_evidence(
            manifest, revisions_by_source[evidence.path_object_id], evidence
        )
    if overlap_evidence.positive_area_overlaps:
        raise AnalysisDomainError("selected analysis domains have a positive-area overlap")
    return manifest


def _manifest_evidence_key(
    manifest: AnalysisDomainManifest,
    active: tuple[AnalysisDomainRevision, ...],
) -> tuple[object, ...]:
    selected_source_ids = tuple(
        sorted(
            item.source_path_object_id
            for item in active
            if item.ref.kind is AnalysisDomainKind.SELECTED_ROI
        )
    )
    return (
        manifest.canonical_image_id,
        manifest.canonical_image_sha256,
        manifest.project_entry_id,
        manifest.section_id,
        manifest.tissue_generation_id,
        manifest.tissue_roi_id,
        manifest.tissue_path_object_id,
        selected_source_ids,
    )


def _overlap_evidence_key(evidence: DomainOverlapEvidence) -> tuple[object, ...]:
    return (
        evidence.canonical_image_id,
        evidence.canonical_image_sha256,
        evidence.project_entry_id,
        evidence.section_id,
        evidence.tissue_generation_id,
        evidence.tissue_roi_id,
        evidence.tissue_path_object_id,
        tuple(sorted(item.path_object_id for item in evidence.geometry_evidence)),
    )


def resolve_active_analysis_domain_authorities(
    manifests: Sequence[AnalysisDomainManifest],
    overlap_evidence: Sequence[DomainOverlapEvidence],
) -> tuple[ActiveAnalysisDomainAuthority, ...]:
    """Validate owner records and project their canonical active authorities."""
    if isinstance(manifests, (str, bytes)) or not isinstance(manifests, Sequence):
        raise AnalysisDomainError("analysis-domain manifests must be a sequence")
    if isinstance(overlap_evidence, (str, bytes)) or not isinstance(overlap_evidence, Sequence):
        raise AnalysisDomainError("overlap evidence must be a sequence")
    manifest_values = tuple(manifests)
    evidence_values = tuple(overlap_evidence)
    if not manifest_values:
        raise AnalysisDomainError("active analysis-domain projection must not be empty")
    if len(manifest_values) != len(evidence_values):
        raise AnalysisDomainError(
            "manifest/evidence bijection must cover the exact active domain inventory"
        )

    manifests_by_key: dict[tuple[object, ...], AnalysisDomainManifest] = {}
    active_by_key: dict[tuple[object, ...], tuple[AnalysisDomainRevision, ...]] = {}
    manifest_digests: set[str] = set()
    common_scope: tuple[str, str, str, str] | None = None
    for manifest in manifest_values:
        if not isinstance(manifest, AnalysisDomainManifest):
            raise AnalysisDomainError(
                "manifest inventory must contain AnalysisDomainManifest records"
            )
        _validate_manifest_scope(manifest)
        active = _active_revisions(manifest)
        scope = (
            manifest.canonical_image_id,
            manifest.canonical_image_sha256,
            manifest.project_entry_id,
            manifest.slide_id,
        )
        if common_scope is None:
            common_scope = scope
        elif scope != common_scope:
            raise AnalysisDomainError(
                "active domain manifests must share a common image, project, and slide"
            )
        if manifest.content_sha256 in manifest_digests:
            raise AnalysisDomainError(
                "active domain projection contains a duplicate manifest identity"
            )
        manifest_digests.add(manifest.content_sha256)
        key = _manifest_evidence_key(manifest, active)
        if key in manifests_by_key:
            raise AnalysisDomainError("manifest/evidence bijection has duplicate manifest coverage")
        manifests_by_key[key] = manifest
        active_by_key[key] = active

    evidence_by_key: dict[tuple[object, ...], DomainOverlapEvidence] = {}
    for evidence in evidence_values:
        if not isinstance(evidence, DomainOverlapEvidence):
            raise AnalysisDomainError(
                "overlap inventory must contain DomainOverlapEvidence records"
            )
        key = _overlap_evidence_key(evidence)
        if key in evidence_by_key:
            raise AnalysisDomainError("manifest/evidence bijection has duplicate overlap coverage")
        evidence_by_key[key] = evidence
    if set(manifests_by_key) != set(evidence_by_key):
        raise AnalysisDomainError(
            "manifest/evidence bijection must cover the exact active domain inventory"
        )

    authorities: list[ActiveAnalysisDomainAuthority] = []
    domain_ids: set[str] = set()
    source_ids: set[str] = set()
    for key, manifest in manifests_by_key.items():
        validate_domain_manifest(manifest, evidence_by_key[key])
        for revision in active_by_key[key]:
            if revision.analysis_domain_id in domain_ids:
                raise AnalysisDomainError(
                    "active domain projection contains a duplicate domain identity"
                )
            if revision.source_path_object_id in source_ids:
                raise AnalysisDomainError(
                    "active domain projection contains a duplicate source identity"
                )
            domain_ids.add(revision.analysis_domain_id)
            source_ids.add(revision.source_path_object_id)
            authorities.append(
                ActiveAnalysisDomainAuthority(
                    analysis_domain_id=revision.analysis_domain_id,
                    analysis_domain_kind=revision.ref.kind,
                    canonical_image_id=revision.canonical_image_id,
                    canonical_image_sha256=revision.canonical_image_sha256,
                    project_entry_id=revision.project_entry_id,
                    slide_id=revision.ref.slide_id,
                    section_id=revision.ref.section_id,
                    tissue_generation_id=revision.ref.tissue_generation_id,
                    tissue_roi_id=revision.tissue_roi_id,
                    tissue_path_object_id=revision.tissue_path_object_id,
                    source_path_object_id=revision.source_path_object_id,
                    analysis_domain_revision=revision.revision,
                    analysis_domain_revision_sha256=revision.content_sha256,
                    analysis_domain_manifest_sha256=manifest.content_sha256,
                    geometry_digest_policy=revision.geometry_digest_policy,
                    geometry_sha256=revision.geometry_sha256,
                    geometry_reference_path_object_id=(revision.geometry_reference_path_object_id),
                )
            )
    return tuple(sorted(authorities, key=lambda item: item.analysis_domain_id))


__all__ = [
    "resolve_active_analysis_domain_authorities",
    "validate_domain_manifest",
]
