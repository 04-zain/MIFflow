"""Portable, strictly reviewed Stage-1 panel-mapping artifacts.

The `ReviewedPanelMapping` record carries `mapping_id`, `review_id`, `review_decision`,
`source_image_index`, `source_series_index` and its remaining declared fields."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, asdict
import hashlib
import json
from pathlib import Path
import re
from typing import Literal

from mif_pipeline.io import BiomarkerAssignmentRow

from .workflow import (
    Stage1Workflow,
    ComponentReviewRow,
    apply_stage1_channel_content,
    apply_stage1_component_values,
    apply_stage1_biomarker_values,
    apply_stage1_channel_name_replacement,
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_BYTES = 1_048_576


# ==============================================================================
#                                 Panel mapping
# ==============================================================================


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{field} must be non-empty canonical text")
    return value


def _index(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("reviewed panel mapping is not canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class ReviewedPanelChannel:
    image_index: int
    channel_index: int
    channel_id: str
    component_name: str
    fluorophore: str | None
    biomarker: str | None
    assignment: Literal["assigned", "none"]

    def __post_init__(self) -> None:
        object.__setattr__(self, "image_index", _index(self.image_index, "image_index"))
        object.__setattr__(self, "channel_index", _index(self.channel_index, "channel_index"))
        object.__setattr__(self, "channel_id", _text(self.channel_id, "channel_id"))
        object.__setattr__(self, "component_name", _text(self.component_name, "component_name"))
        if self.fluorophore is not None:
            object.__setattr__(self, "fluorophore", _text(self.fluorophore, "fluorophore"))
        if self.assignment not in {"assigned", "none"}:
            raise ValueError("panel mapping assignment must be assigned or none")
        if self.assignment == "assigned":
            object.__setattr__(self, "biomarker", _text(self.biomarker, "biomarker"))
        elif self.biomarker is not None:
            raise ValueError("none panel mapping assignment requires biomarker=None")

    def to_mapping(self) -> dict[str, object]:
        return {
            "image_index": self.image_index,
            "channel_index": self.channel_index,
            "channel_id": self.channel_id,
            "component_name": self.component_name,
            "fluorophore": self.fluorophore,
            "biomarker": self.biomarker,
            "assignment": self.assignment,
        }


def _channel_compatibility(channels: tuple[ReviewedPanelChannel, ...]) -> str:
    return _digest(_channel_selection_material(channels))


def _channel_selection_material(
    channels: Iterable[ReviewedPanelChannel | BiomarkerAssignmentRow],
) -> list[dict[str, object]]:
    return [
        {
            "image_index": item.image_index,
            "channel_index": item.channel_index,
            "channel_id": item.channel_id,
            "component_name": _text(item.component_name, "component_name"),
        }
        for item in channels
    ]


@dataclass(frozen=True, slots=True)
class ReviewedPanelMapping:
    """Accepted portable mapping; source paths and display names are non-identity."""

    SCHEMA_NAME = "mif_pipeline.reviewed_panel_mapping"
    SCHEMA_VERSION = 1

    mapping_id: str
    review_id: str
    review_decision: Literal["accepted"]
    source_image_index: int
    source_series_index: int
    source_image_id: str
    source_pixels_id: str
    channels: tuple[ReviewedPanelChannel, ...]
    replace_channel_names_with_biomarkers: bool
    producer_version: str
    runtime_versions: tuple[tuple[str, str], ...]
    schema_name: str = SCHEMA_NAME
    schema_version: int = SCHEMA_VERSION
    compatibility_signature_sha256: str = ""
    content_sha256: str = ""
    planned_components: tuple[ComponentReviewRow, ...] = ()
    source_band_ids: tuple[str, ...] = ()
    nuclear_stain: str | None = None
    nuclear_stain_reviewed: bool = False

    def __post_init__(self) -> None:
        """Validate the ReviewedPanelMapping fields when the record is constructed.

        The checks cover ``review_decision``, ``channels``, ``runtime_versions``,
        ``compatibility_signature_sha256``, ``content_sha256``, ``mapping_id``. Invalid
        field types, values or combinations raise the declared validation error before this
        instance can be used by its caller."""

        if (
            type(self.schema_name) is not str
            or type(self.schema_version) is not int
            or (self.schema_name, self.schema_version) != (self.SCHEMA_NAME, self.SCHEMA_VERSION)
        ):
            raise ValueError("reviewed panel mapping schema is unsupported")
        object.__setattr__(self, "mapping_id", _text(self.mapping_id, "mapping_id"))
        object.__setattr__(self, "review_id", _text(self.review_id, "review_id"))
        if self.review_decision != "accepted":
            raise ValueError("reviewed panel mapping decision must be accepted")
        object.__setattr__(
            self, "source_image_index", _index(self.source_image_index, "source_image_index")
        )
        object.__setattr__(
            self, "source_series_index", _index(self.source_series_index, "source_series_index")
        )
        object.__setattr__(self, "source_image_id", _text(self.source_image_id, "source_image_id"))
        object.__setattr__(
            self, "source_pixels_id", _text(self.source_pixels_id, "source_pixels_id")
        )
        channels = tuple(self.channels)
        if (not channels and not self.planned_components) or not all(type(item) is ReviewedPanelChannel for item in channels):
            raise ValueError("reviewed panel mapping requires typed channels")
        positions = tuple((item.image_index, item.channel_index) for item in channels)
        if positions != tuple((0, index) for index in range(len(channels))):
            raise ValueError("reviewed panel channels must be complete and ordered")
        # A portable mapping has no acquisition-cycle authority. Marker uniqueness
        # is checked against actual channel-bound cycles at creation and application.
        object.__setattr__(self, "channels", channels)
        if type(self.replace_channel_names_with_biomarkers) is not bool:
            raise ValueError("reviewed panel mapping name replacement must be boolean")
        object.__setattr__(
            self, "producer_version", _text(self.producer_version, "producer_version")
        )
        versions = tuple(self.runtime_versions)
        if (
            not versions
            or not all(type(item) is tuple and len(item) == 2 for item in versions)
            or versions != tuple(sorted(versions))
            or len({item[0] for item in versions}) != len(versions)
        ):
            raise ValueError("reviewed panel runtime versions must be unique and sorted")
        for name, version in versions:
            _text(name, "runtime name")
            _text(version, "runtime version")
        object.__setattr__(self, "runtime_versions", versions)
        if self.planned_components:
            from mif_pipeline.io.metadata.canonical import _validate_component_biomarkers
            if channels or not self.source_band_ids or any(not isinstance(i, str) or not i for i in self.source_band_ids):
                raise ValueError("Raw panel mappings retain source bands separately from components")
            if len(set(self.source_band_ids)) != len(self.source_band_ids):
                raise ValueError("Raw panel source band IDs must be unique")
            names = tuple(_text(row.component_name, "component_name") for row in self.planned_components)
            markers = tuple(row.biomarker for row in self.planned_components)
            if len(set(names)) != len(names) or any(row.assignment != ("none" if row.biomarker is None else "assigned") for row in self.planned_components):
                raise ValueError("Raw panel component decisions must be complete and unique")
            _validate_component_biomarkers(names, markers)
        elif self.source_band_ids:
            raise ValueError("Source band inventory requires a planned panel")
        if type(self.nuclear_stain_reviewed) is not bool:
            raise ValueError("Nuclear-stain review state must be boolean")
        if self.nuclear_stain is not None:
            if not self.nuclear_stain_reviewed:
                raise ValueError("A nuclear stain requires a reviewed choice")
            rows = self.planned_components or channels
            identities = [row.component_name if self.planned_components else row.channel_id for row in rows]
            if identities.count(self.nuclear_stain) != 1:
                raise ValueError("The nuclear stain must identify one reviewed component")
            selected = rows[identities.index(self.nuclear_stain)]
            from mif_pipeline.io.metadata.canonical import is_autofluorescence_component
            if selected.biomarker is not None or is_autofluorescence_component(selected.component_name):
                raise ValueError("The nuclear stain requires a DNA channel with no marker assignment")
        compatibility = (_digest({"source_band_ids": self.source_band_ids,
            "component_names": [row.component_name for row in self.planned_components]})
            if self.planned_components else _channel_compatibility(channels))
        if self.compatibility_signature_sha256 not in {"", compatibility}:
            raise ValueError("reviewed panel compatibility signature is stale")
        object.__setattr__(self, "compatibility_signature_sha256", compatibility)
        content = _digest(self._unsigned_mapping())
        if self.content_sha256 not in {"", content}:
            raise ValueError("reviewed panel mapping content digest is stale")
        object.__setattr__(self, "content_sha256", content)

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "mapping_id": self.mapping_id,
            "review_id": self.review_id,
            "review_decision": self.review_decision,
            "source_selection": {
                "source_image_index": self.source_image_index,
                "source_series_index": self.source_series_index,
                "source_image_id": self.source_image_id,
                "source_pixels_id": self.source_pixels_id,
            },
            **({"planned_components": [asdict(row) for row in self.planned_components],
                "source_band_ids": list(self.source_band_ids)} if self.planned_components else {}),
            **({"nuclear_stain": self.nuclear_stain} if self.nuclear_stain_reviewed else {}),
            "channels": [item.to_mapping() for item in self.channels],
            "replace_channel_names_with_biomarkers": (self.replace_channel_names_with_biomarkers),
            "producer_version": self.producer_version,
            "runtime_versions": {name: version for name, version in self.runtime_versions},
            "compatibility_signature_sha256": self.compatibility_signature_sha256,
        }

    def to_mapping(self) -> dict[str, object]:
        return {**self._unsigned_mapping(), "content_sha256": self.content_sha256}


def create_reviewed_panel_mapping(
    workflow: Stage1Workflow,
    *,
    review_id: str,
    producer_version: str,
    runtime_versions: Mapping[str, str],
) -> ReviewedPanelMapping:
    if not isinstance(workflow, Stage1Workflow) or workflow.status != "ready":
        raise ValueError("reviewed panel mapping requires a ready Stage-one workflow")
    if workflow.replace_channel_names_with_biomarkers is None:
        raise ValueError("reviewed panel mapping requires the slide-wide name decision")
    from mif_pipeline.io.metadata.spectral import resolve_spectral_state
    raw = resolve_spectral_state(workflow.metadata, 0).state == "raw"
    if not raw:
        from mif_pipeline.io.metadata.biomarkers import duplicate_channel_biomarker
        if duplicate_channel_biomarker(workflow.metadata, workflow.biomarker_rows) is not None:
            raise ValueError("reviewed panel mapping has duplicate non-null biomarkers")
    channels = tuple(
        ReviewedPanelChannel(
            image_index=row.image_index,
            channel_index=row.channel_index,
            channel_id=row.channel_id,
            component_name=_text(row.component_name, "component_name"),
            fluorophore=row.fluorophore,
            biomarker=row.biomarker,
            assignment=row.biomarker_assignment,  # type: ignore[arg-type]
        )
        for row in (() if raw else workflow.biomarker_rows)
    )
    versions = tuple(
        sorted(
            (_text(name, "runtime name"), _text(version, "runtime version"))
            for name, version in runtime_versions.items()
        )
    )
    from mif_pipeline.io.metadata.canonical import resolve_component_identity_inventory
    inventory = resolve_component_identity_inventory(workflow.metadata, 0)
    reviewed = inventory is not None and inventory.nuclear_stain_reviewed
    nuclear = None
    if reviewed and inventory.nuclear_component_index is not None:
        nuclear = (inventory.component_names if raw else inventory.channel_ids)[inventory.nuclear_component_index]
    mapping_material = {
        **({"nuclear_stain": nuclear} if reviewed else {}),
        "review_id": review_id,
        "compatibility": _channel_compatibility(channels),
        "channels": [item.to_mapping() for item in channels],
        "planned_components": [asdict(row) for row in workflow.component_rows] if raw else [],
        "source_band_ids": [row.channel_id for row in workflow.biomarker_rows] if raw else [],
        "replace_channel_names_with_biomarkers": (workflow.replace_channel_names_with_biomarkers),
    }
    return ReviewedPanelMapping(
        mapping_id="panel-" + _digest(mapping_material)[:48],
        review_id=review_id,
        review_decision="accepted",
        source_image_index=workflow.selection.source_image_index,
        source_series_index=workflow.selection.source_series_index,
        source_image_id=workflow.selection.image_id,
        source_pixels_id=workflow.selection.pixels_id,
        channels=channels,
        nuclear_stain=nuclear, nuclear_stain_reviewed=reviewed,
        planned_components=workflow.component_rows if raw else (),
        source_band_ids=tuple(row.channel_id for row in workflow.biomarker_rows) if raw else (),
        replace_channel_names_with_biomarkers=(workflow.replace_channel_names_with_biomarkers),
        producer_version=producer_version,
        runtime_versions=versions,
    )


def dump_reviewed_panel_mapping(mapping: ReviewedPanelMapping) -> bytes:
    if type(mapping) is not ReviewedPanelMapping:
        raise TypeError("panel mapping writer requires an exact reviewed mapping")
    return _canonical_bytes(mapping.to_mapping())


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate reviewed panel field {key!r}")
        result[key] = value
    return result


def loads_reviewed_panel_mapping(payload: bytes) -> ReviewedPanelMapping:
    if type(payload) is not bytes or not payload or len(payload) > _MAX_BYTES:
        raise ValueError("reviewed panel mapping bytes are absent or unbounded")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda item: (_ for _ in ()).throw(
                ValueError(f"nonfinite reviewed panel value {item!r}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("reviewed panel mapping is invalid JSON") from exc
    if not isinstance(value, dict) or _canonical_bytes(value) != payload:
        raise ValueError("reviewed panel mapping bytes are not canonical")
    expected = {
        "schema_name",
        "schema_version",
        "mapping_id",
        "review_id",
        "review_decision",
        "source_selection",
        "channels",
        "replace_channel_names_with_biomarkers",
        "producer_version",
        "runtime_versions",
        "compatibility_signature_sha256",
        "content_sha256",
    }
    if "nuclear_stain" in value:
        expected.add("nuclear_stain")
    if "planned_components" in value:
        expected |= {"planned_components", "source_band_ids"}
    if set(value) != expected:
        raise ValueError("reviewed panel mapping fields differ")
    selection = value["source_selection"]
    versions = value["runtime_versions"]
    raw_channels = value["channels"]
    if not isinstance(selection, dict) or set(selection) != {
        "source_image_index",
        "source_series_index",
        "source_image_id",
        "source_pixels_id",
    }:
        raise ValueError("reviewed panel source selection fields differ")
    if not isinstance(versions, dict) or not isinstance(raw_channels, list):
        raise ValueError("reviewed panel inventories are invalid")
    channel_fields = {
        "image_index",
        "channel_index",
        "channel_id",
        "component_name",
        "fluorophore",
        "biomarker",
        "assignment",
    }
    if any(not isinstance(item, dict) or set(item) != channel_fields for item in raw_channels):
        raise ValueError("reviewed panel channel fields differ")
    mapping = ReviewedPanelMapping(
        mapping_id=value["mapping_id"],
        review_id=value["review_id"],
        review_decision=value["review_decision"],
        source_image_index=selection["source_image_index"],
        source_series_index=selection["source_series_index"],
        source_image_id=selection["source_image_id"],
        source_pixels_id=selection["source_pixels_id"],
        channels=tuple(ReviewedPanelChannel(**item) for item in raw_channels),
        nuclear_stain=value.get("nuclear_stain"), nuclear_stain_reviewed="nuclear_stain" in value,
        planned_components=tuple(ComponentReviewRow(**row) for row in value.get("planned_components", ())),
        source_band_ids=tuple(value.get("source_band_ids", ())),
        replace_channel_names_with_biomarkers=value["replace_channel_names_with_biomarkers"],
        producer_version=value["producer_version"],
        runtime_versions=tuple(versions.items()),
        schema_name=value["schema_name"],
        schema_version=value["schema_version"],
        compatibility_signature_sha256=value["compatibility_signature_sha256"],
        content_sha256=value["content_sha256"],
    )
    return mapping


def load_reviewed_panel_mapping(path: str | Path) -> ReviewedPanelMapping:
    source = Path(path)
    try:
        size = source.stat().st_size
        if size <= 0 or size > _MAX_BYTES:
            raise ValueError("reviewed panel mapping file is absent or unbounded")
        return loads_reviewed_panel_mapping(source.read_bytes())
    except OSError as exc:
        raise ValueError(f"could not read reviewed panel mapping: {source}") from exc


def apply_reviewed_panel_mapping(
    workflow: Stage1Workflow, mapping: ReviewedPanelMapping
) -> Stage1Workflow:
    """Revalidate and apply one accepted mapping to an exact compatible target."""

    if not isinstance(workflow, Stage1Workflow) or type(mapping) is not ReviewedPanelMapping:
        raise TypeError("panel mapping application requires exact typed values")
    if mapping.nuclear_stain_reviewed:
        from mif_pipeline.io.metadata.canonical import resolve_component_identity_inventory
        from .workflow import apply_stage1_nuclear_stain
        if resolve_component_identity_inventory(workflow.metadata, 0) is not None:
            workflow = apply_stage1_nuclear_stain(workflow, None)
    if mapping.planned_components:
        if tuple(row.channel_id for row in workflow.biomarker_rows) != mapping.source_band_ids:
            raise ValueError("Raw reviewed panel does not match acquired source band order")
        from mif_pipeline.io.metadata.canonical import is_autofluorescence_component
        existing_names = tuple(row.component_name for row in workflow.component_rows if not is_autofluorescence_component(row.component_name))
        planned_names = tuple(row.component_name for row in mapping.planned_components if not is_autofluorescence_component(row.component_name))
        if existing_names and existing_names != planned_names:
            raise ValueError("Raw reviewed panel conflicts with declared output components")
        reviewed = apply_stage1_component_values(apply_stage1_channel_content(workflow, "raw"),
            [asdict(row) for row in mapping.planned_components])
        if mapping.nuclear_stain_reviewed:
            from .workflow import apply_stage1_nuclear_stain
            reviewed = apply_stage1_nuclear_stain(reviewed, mapping.nuclear_stain)
        return apply_stage1_channel_name_replacement(reviewed,
            replace_channel_names_with_biomarkers=mapping.replace_channel_names_with_biomarkers)
    target_compatibility = _digest(_channel_selection_material(workflow.biomarker_rows))
    if target_compatibility != mapping.compatibility_signature_sha256:
        raise ValueError("reviewed panel mapping is not compatible with target channels")
    values = {
        f"{item.image_index}:{item.channel_index}": {
            "assignment": item.assignment,
            "biomarker": item.biomarker,
            "component_name": item.component_name,
        }
        for item in mapping.channels
    }
    reviewed = apply_stage1_biomarker_values(workflow, values)
    if tuple(row.fluorophore for row in reviewed.biomarker_rows) != tuple(
        item.fluorophore for item in mapping.channels
    ):
        raise ValueError("reviewed panel fluorophores differ after target application")
    if mapping.nuclear_stain_reviewed:
        from .workflow import apply_stage1_nuclear_stain
        reviewed = apply_stage1_nuclear_stain(reviewed, mapping.nuclear_stain)
    return apply_stage1_channel_name_replacement(
        reviewed,
        replace_channel_names_with_biomarkers=(mapping.replace_channel_names_with_biomarkers),
    )


__all__ = [
    "ReviewedPanelChannel",
    "ReviewedPanelMapping",
    "apply_reviewed_panel_mapping",
    "create_reviewed_panel_mapping",
    "dump_reviewed_panel_mapping",
    "load_reviewed_panel_mapping",
    "loads_reviewed_panel_mapping",
]
