"""Carry existing stage choices through the command line and cluster controls.

These values describe an edit and its affected steps. The batch service checks
the current saved state again when applying it; transported previews never
authorize execution or supply saved-result retirement records.
"""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

from mif_pipeline.pipeline_core.config import SegmentationConfig
from .batch_manifest import BatchSample
from .batch_measurement_settings import MeasurementSettings
from .batch_settled_review_revision import SETTLED_REVIEW_KINDS, STAGE_OPTIONS_KINDS, SettledReviewRevision
from .batch_source_revision import SourceSelection
from .batch_stage_settings import BatchStageOptions, BatchStageRevisionPreview
from .batch_workflow import BatchWorkflowNode


def parse_revision(text: str) -> dict:
    """Read one bounded edit without accepting duplicate fields or invalid numbers."""
    if len(text.encode("utf-8")) > 1024 * 1024:
        raise ValueError("Stage options exceed 1 MiB.")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Repeated stage option: {key}")
            result[key] = value
        return result

    value = json.loads(text, object_pairs_hook=unique, parse_constant=_invalid_number)
    if not isinstance(value, dict):
        raise ValueError("Stage options must be a JSON object.")
    return value


def _invalid_number(value):
    raise ValueError(f"Invalid stage option number: {value}")


def settings_mapping(settings):
    """Use the existing stage-specific field names for a displayed selection."""
    if isinstance(settings, dict):
        return dict(settings)
    return settings.to_mapping()


def revision_settings(options: BatchStageOptions, value: dict):
    """Parse the edit with its existing validator on the processing machine.

    For a source change, this machine inspects the file and binds the selected
    eligible series. Filesystem facts supplied by a mounted client are never
    used to create the source identity.
    """
    if options.kind == "inspection":
        if "predecessor_node_id" in value:
            selected = SettledReviewRevision.from_mapping(value)
            if selected != options.inspection_review:
                raise ValueError("Cycle review must restart from its displayed current completion.")
            return selected
        from mif_pipeline.io import inspect_source_series_inventory
        if set(value) != {"source_mif_path", "source_image_index", "source_series_index", "source_file_sha256"}:
            raise ValueError("Source selection requires its path, image, series and inspected checksum.")
        if any(type(value[key]) is not int or value[key] < 0
               for key in ("source_image_index", "source_series_index")):
            raise ValueError("Source image and series must be nonnegative integer indices.")
        inventory = inspect_source_series_inventory(Path(value["source_mif_path"]))
        if inventory.source_identity.file_sha256 != value["source_file_sha256"]:
            raise ValueError("The source image changed. Inspect it again before applying this correction.")
        selected = [item for item in inventory.candidates
                    if (item.source_image_index, item.source_series_index) ==
                    (value["source_image_index"], value["source_series_index"])]
        if len(selected) != 1:
            raise ValueError("Choose an eligible image and series from the current inspection.")
        return SourceSelection.from_inventory(inventory, selected[0].candidate_id)
    if options.kind == "segmentation":
        return SegmentationConfig.from_mapping(value, field="segmentation")
    if options.kind == "measurements":
        return MeasurementSettings.from_mapping(value)
    if options.kind in SETTLED_REVIEW_KINDS:
        return SettledReviewRevision.from_mapping(value)
    raise ValueError("This step has no supported saved options.")


def encode_options(options: BatchStageOptions) -> dict:
    """Describe a selected task using its existing settings representation."""
    return {"batch_id": options.batch_id, "state_head_sha256": options.state_head_sha256,
            "node_id": options.node_id, "sample_id": options.sample_id,
            "section_id": options.section_id, "kind": options.kind,
            "settings": settings_mapping(options.settings),
            "required_feature_ids": list(options.required_feature_ids),
            "participant_ids": list(options.participant_ids),
            "saved_tissue": None if options.saved_tissue is None else options.saved_tissue.to_mapping(),
            "saved_unmixing": None if options.saved_unmixing is None else options.saved_unmixing.to_mapping(),
            "saved_secondary": None if options.saved_secondary is None else options.saved_secondary.to_mapping(),
            "saved_analysis_mode": options.saved_analysis_mode,
            "saved_clustering": options.saved_clustering,
            "inspection_review": None if options.inspection_review is None else options.inspection_review.to_mapping()}


def decode_options(value: dict, *, remote=False) -> BatchStageOptions:
    """Restore displayed settings without performing an edit or opening an image."""
    fields = dict(value)
    kind, settings = fields["kind"], fields["settings"]
    if kind == "inspection":
        # Remote source paths are display strings. Resolving them on a Windows
        # client would incorrectly prepend its local drive. The processing
        # host alone creates and validates the actual SourceSelection.
        settings = SimpleNamespace(**settings) if remote else BatchSample.from_mapping(settings, index=0)
    elif kind == "segmentation":
        path = settings["model_path"]
        settings = SegmentationConfig.from_mapping(settings, field="segmentation")
        if remote:
            from dataclasses import replace
            if not PurePosixPath(path).is_absolute():
                raise ValueError("The cluster model folder must be an absolute path.")
            settings = replace(settings, model_path=PurePosixPath(path))
    elif kind == "measurements":
        settings = MeasurementSettings.from_mapping(settings)
    elif kind in SETTLED_REVIEW_KINDS:
        # Keep compute-host file paths as display values on mounted clients.
        settings = settings if remote and kind == "registration" else SettledReviewRevision.from_mapping(settings)
    else:
        raise ValueError("The returned step has no supported saved options.")
    fields.update(settings=settings, required_feature_ids=tuple(fields["required_feature_ids"]),
                  participant_ids=tuple(fields["participant_ids"]))
    review = fields.get("inspection_review")
    fields["inspection_review"] = None if review is None else SettledReviewRevision.from_mapping(review)
    from .batch_tissue_saved_settings import SavedTissueSettings
    saved = fields.get("saved_tissue")
    fields["saved_tissue"] = None if saved is None else SavedTissueSettings.from_mapping(saved)
    from mif_pipeline.pipeline_core import StageResult
    saved = fields.get("saved_unmixing")
    fields["saved_unmixing"] = None if saved is None else StageResult.from_mapping(saved)
    saved = fields.get("saved_secondary")
    fields["saved_secondary"] = None if saved is None else StageResult.from_mapping(saved)
    return BatchStageOptions(**fields)


def encode_preview(preview: BatchStageRevisionPreview) -> dict:
    """Return the affected steps; apply reconstructs retirement records itself."""
    return {"options": encode_options(preview.options), "settings": settings_mapping(preview.settings),
            "affected_node_ids": list(preview.affected_node_ids),
            "preserved_completed_node_ids": list(preview.preserved_completed_node_ids),
            "deferred_node_kinds": list(preview.deferred_node_kinds),
            "blocked_reason": preview.blocked_reason}


def decode_preview(value: dict, *, settings, remote=False) -> BatchStageRevisionPreview:
    """Associate a returned preview with the exact edit still shown in the form."""
    if value["settings"] != settings_mapping(settings):
        raise ValueError("The returned preview describes different options. Preview the current edit again.")
    return BatchStageRevisionPreview(decode_options(value["options"], remote=remote), settings,
        tuple(value["affected_node_ids"]), tuple(value["preserved_completed_node_ids"]),
        tuple(value["deferred_node_kinds"]), value["blocked_reason"])


def revision_choices(status) -> dict:
    """List current settled tasks; selecting one opens its service-validated options."""
    from .batch_source_revision import SOURCE_CORRECTION_KINDS
    from .batch_settled_review_revision import review_target
    nodes = {node.node_id: node for node in status.workflow_nodes if not node.deferred}
    plan = SimpleNamespace(nodes=tuple(SimpleNamespace(**asdict(node), participant_ids=node.sample_ids)
                                      for node in nodes.values()))
    choices = {}
    for node in plan.nodes:
        if (node.kind not in STAGE_OPTIONS_KINDS
                or (node.status not in {"completed", "failed"} and not (node.kind == "reference_unmixing" and node.status == "needs_interaction"))
                or (node.kind in SOURCE_CORRECTION_KINDS and node.kind != "reference_unmixing" and not node.source_correction_available)):
            continue
        target = review_target(plan, node)
        choices[target.node_id] = asdict(nodes[target.node_id])
    return {"batch_id": status.batch_id, "state_head_sha256": status.state_head_sha256,
            "choices": list(choices.values()),
            "workflow_nodes": [asdict(node) for node in status.workflow_nodes]}


def decode_workflow(rows) -> tuple[BatchWorkflowNode, ...]:
    """Restore the existing workflow labels used by the affected-step dialog."""
    return tuple(BatchWorkflowNode(**{**row, **{key: tuple(row[key])
        for key in ("sample_ids", "dependency_ids", "prerequisite_kinds")}}) for row in rows)
