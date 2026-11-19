"""Strict operation-owned resource registry for reference unmixing.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
from typing import Mapping

from mif_pipeline.io import CanonicalOmeTiffHeader
from mif_pipeline.preprocessing.unmixing.reference_rois import (
    AcceptedReferenceRoiSelection,
    ReferenceRoiExport,
    select_accepted_reference_roi,
)
from mif_pipeline.preprocessing.unmixing.reference_sampling import (
    TargetSamplingStratum,
    TissueMaskBinding,
)
from mif_pipeline.qupath_runtime.reference_project import (
    ReferenceProjectLayout,
    load_project_owned_reference_roi_export,
)
from mif_pipeline.tissue_detection.generations import GenerationStatus

from .exports import OutputArtifact, load_tissue_generation_artifact

# ==============================================================================
#                          Reference unmixing resources
# ==============================================================================


class ReferenceUnmixingResourceError(ValueError):
    """A locator is missing, remapped, or no longer authoritative."""


def _canonical_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value.strip() or value != value.strip():
        raise ReferenceUnmixingResourceError(f"{label} must be nonempty literal text")
    return value


def _absolute(value: object, label: str) -> Path:
    if type(value) is not str:
        path = Path(value) if isinstance(value, Path) else None
    else:
        path = Path(value)
    if path is None or not path.is_absolute():
        raise ReferenceUnmixingResourceError(f"{label} must be absolute")
    return path


def _strict_load(path: Path) -> dict[str, object]:
    try:
        encoded = path.read_bytes()
        value = json.loads(encoded)
    except (OSError, json.JSONDecodeError) as exc:
        raise ReferenceUnmixingResourceError(
            "registered resource descriptor is unreadable"
        ) from exc
    if type(value) is not dict or encoded != _canonical_bytes(value):
        raise ReferenceUnmixingResourceError("registered resource descriptor is not canonical")
    return value


def _geometry_points(value: object):
    if type(value) is list:
        if len(value) == 2 and all(
            type(item) in {int, float} and type(item) is not bool for item in value
        ):
            yield float(value[0]), float(value[1])
            return
        for item in value:
            yield from _geometry_points(item)


def tissue_mask_binding_from_generation_artifact(
    artifact: OutputArtifact,
    header: CanonicalOmeTiffHeader,
) -> TissueMaskBinding:
    """Derive an exact target binding from a trusted accepted tissue export."""

    if type(header) is not CanonicalOmeTiffHeader:
        raise TypeError("tissue binding requires exact canonical header")
    generations = load_tissue_generation_artifact(artifact)
    accepted = tuple(item for item in generations if item.status is GenerationStatus.ACCEPTED)
    if len(accepted) != 1:
        raise ReferenceUnmixingResourceError(
            "tissue export must contain one exact accepted generation"
        )
    generation = accepted[0]
    if (
        generation.canonical_image_id != header.image_id
        or generation.canonical_image_sha256 != header.sha256
    ):
        raise ReferenceUnmixingResourceError(
            "accepted tissue generation does not bind the canonical target"
        )
    if not generation.sections:
        raise ReferenceUnmixingResourceError(
            "accepted tissue generation must contain nonempty sections"
        )
    strata: list[TargetSamplingStratum] = []
    for section in sorted(generation.sections, key=lambda item: item.section_id):
        exported_geometry = section.to_export_mapping()["geometry"]
        geometry_json = json.dumps(
            exported_geometry,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        geometry = json.loads(geometry_json)
        points = tuple(_geometry_points(geometry.get("coordinates")))
        if not points or any(
            x < 0.0 or y < 0.0 or x > header.size_x or y > header.size_y for x, y in points
        ):
            raise ReferenceUnmixingResourceError(
                "accepted tissue section geometry exceeds target bounds"
            )
        strata.append(TargetSamplingStratum.create(section.section_id, geometry_json))
    return TissueMaskBinding.create(
        header=header,
        source_mask_sha256=artifact.sha256,
        strata=tuple(strata),
    )


class ReferenceUnmixingResourceRegistry:
    """Persist immutable locator bindings and revalidate producers on every use."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def open_existing(cls, root: str | Path) -> "ReferenceUnmixingResourceRegistry":
        """Open a pre-existing registry without creating filesystem state."""

        requested = Path(root)
        if not requested.is_absolute() or requested.is_symlink() or not requested.is_dir():
            raise ReferenceUnmixingResourceError(
                "existing resource registry layout is missing or invalid"
            )
        instance = cls.__new__(cls)
        instance._root = requested.resolve()
        return instance

    def _path(self, locator: str) -> Path:
        locator = _text(locator, "resource locator")
        return self._root / (hashlib.sha256(locator.encode("utf-8")).hexdigest() + ".json")

    def _register(self, locator: str, descriptor: dict[str, object]) -> None:
        path = self._path(locator)
        value = {
            "schema_version": "reference-unmixing-resource/v1",
            "locator": locator,
            **descriptor,
        }
        encoded = _canonical_bytes(value)
        if path.exists():
            if path.read_bytes() != encoded:
                raise ReferenceUnmixingResourceError("resource locator cannot be remapped")
            return
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != encoded:
                    raise ReferenceUnmixingResourceError("resource locator cannot be remapped")
        finally:
            temporary.unlink(missing_ok=True)

    def register_reference_roi_export(
        self,
        locator: str,
        layout: ReferenceProjectLayout,
        artifact_path: str | Path,
    ) -> None:
        export = load_project_owned_reference_roi_export(layout, artifact_path)
        self._register(
            locator,
            {
                "kind": "project_owned_reference_roi_export",
                "layout": {
                    field: str(getattr(layout, field))
                    for field in (
                        "root",
                        "project_file",
                        "scripts_dir",
                        "data_dir",
                        "exchange_dir",
                        "request_path",
                        "script_path",
                    )
                },
                "artifact_path": str(Path(artifact_path)),
                "semantic_id": export.content_sha256,
            },
        )

    def register_tissue_generation(self, locator: str, artifact: OutputArtifact) -> None:
        if type(artifact) is not OutputArtifact:
            raise TypeError("tissue resource registration requires exact OutputArtifact")
        path = artifact.path.resolve()
        pinned = replace(artifact, path=path)
        load_tissue_generation_artifact(pinned)
        self._register(
            locator,
            {"kind": "tissue_generation_output", "artifact": pinned.to_mapping()},
        )

    def _resolve(self, locator: str, kind: str) -> dict[str, object]:
        value = _strict_load(self._path(locator))
        if value.get("schema_version") != "reference-unmixing-resource/v1":
            raise ReferenceUnmixingResourceError("resource descriptor schema is unsupported")
        if value.get("locator") != locator or value.get("kind") != kind:
            raise ReferenceUnmixingResourceError("resource locator has the wrong registered kind")
        return value

    def resolve_reference_roi_export(self, locator: str) -> ReferenceRoiExport:
        value = self._resolve(locator, "project_owned_reference_roi_export")
        layout_value = value.get("layout")
        if type(layout_value) is not dict or set(layout_value) != {
            "root",
            "project_file",
            "scripts_dir",
            "data_dir",
            "exchange_dir",
            "request_path",
            "script_path",
        }:
            raise ReferenceUnmixingResourceError("ROI resource layout is invalid")
        layout = ReferenceProjectLayout(
            **{
                field: _absolute(layout_value[field], f"ROI resource {field}")
                for field in layout_value
            }
        )
        export = load_project_owned_reference_roi_export(
            layout, _absolute(value.get("artifact_path"), "ROI resource artifact")
        )
        if export.content_sha256 != value.get("semantic_id"):
            raise ReferenceUnmixingResourceError("registered ROI export identity changed")
        return export

    def resolve_reference_roi_selections(
        self, locator: str, roi_ids: tuple[str, ...]
    ) -> tuple[AcceptedReferenceRoiSelection, ...]:
        if type(roi_ids) is not tuple or any(type(item) is not str for item in roi_ids):
            raise TypeError("ROI IDs must be an exact tuple of text")
        if len(roi_ids) != len(set(roi_ids)):
            raise ReferenceUnmixingResourceError("ROI IDs must be unique")
        export = self.resolve_reference_roi_export(locator)
        return tuple(select_accepted_reference_roi(export, item) for item in roi_ids)

    def resolve_tissue_mask(
        self, locator: str, header: CanonicalOmeTiffHeader
    ) -> TissueMaskBinding:
        value = self._resolve(locator, "tissue_generation_output")
        artifact = OutputArtifact.from_mapping(value.get("artifact"))
        return tissue_mask_binding_from_generation_artifact(artifact, header)


__all__ = [
    "ReferenceUnmixingResourceError",
    "ReferenceUnmixingResourceRegistry",
    "tissue_mask_binding_from_generation_artifact",
]
