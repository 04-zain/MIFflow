"""Section-scoped segmentation options and their retained plan revision.

We keep the admitted source configuration unchanged. A reviewed settings change
belongs to one current segmentation task and gives its replacement a new task
identity, so upstream and unrelated accepted results remain reusable.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re

from mif_pipeline.pipeline_core.config import SegmentationConfig
from mif_pipeline.pipeline_core.durable_records import canonical_json_bytes
from .batch_artifacts import BatchArtifactError, _closed_mapping


@dataclass(frozen=True, slots=True)
class SegmentationSettingsRevision:
    predecessor_node_id: str
    revision: int
    settings: SegmentationConfig

    def __post_init__(self) -> None:
        if type(self.predecessor_node_id) is not str or re.fullmatch(
            r"node-[0-9a-f]{48}", self.predecessor_node_id
        ) is None:
            raise BatchArtifactError("segmentation revision requires its exact predecessor task")
        if type(self.revision) is not int or self.revision < 1:
            raise BatchArtifactError("segmentation revision number must be positive")
        if type(self.settings) is not SegmentationConfig:
            raise BatchArtifactError("segmentation revision settings must be typed")
        # Construction and wire parsing share the same explicit field checks.
        SegmentationConfig.from_mapping(self.settings.to_mapping(), field="segmentation")

    def to_mapping(self) -> dict[str, object]:
        return {"predecessor_node_id": self.predecessor_node_id,
                "revision": self.revision, "settings": self.settings.to_mapping()}

    @classmethod
    def from_mapping(cls, value: object) -> "SegmentationSettingsRevision":
        mapping = _closed_mapping(value, fields=frozenset({
            "predecessor_node_id", "revision", "settings"
        }), field="segmentation settings revision")
        return cls(mapping["predecessor_node_id"], mapping["revision"],
                   SegmentationConfig.from_mapping(mapping["settings"], field="segmentation"))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.to_mapping())).hexdigest()


def current_segmentation_settings(manifest, plan, sample_id):
    """Select current section models; fall back to admission before section expansion."""
    sample = next(item for item in manifest.enabled_samples if item.sample_id == sample_id)
    nodes = tuple(item for item in plan.nodes if item.kind == "segmentation"
                  and item.participant_ids == (sample_id,))
    selected = tuple(item.segmentation_settings_revision.settings
                     if item.segmentation_settings_revision is not None else sample.config.segmentation
                     for item in nodes) if nodes else (sample.config.segmentation,)
    by_model = {(None, None) if item is None else (item.model_path, item.model_version): item
                for item in selected}
    return tuple(by_model[key] for key in sorted(by_model, key=lambda key: (str(key[0]), key[1] or "")))
