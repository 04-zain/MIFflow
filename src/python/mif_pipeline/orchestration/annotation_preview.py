"""Record and validate permission for model-assisted reference annotation on development
subjects.

The selected preview configuration is bound to the current classifier, image, channels and
subject roles. Opening feedback records the subject's exposure so it cannot later become an
independent validation/test participant. Returned annotations retain that record, while
final fitting follows the declared plan."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
from collections.abc import Mapping
from pathlib import Path
import hashlib, json, os, re
from mif_pipeline.compartment_analysis.kronos_marker_preparation import has_pending_marker_statistics
from .batch_training.plan_selection import (
    validate_training_plan_selection_request,
    validate_training_plan_selection_response,
    validate_reference_class_names,
)
from .batch_training.cross_validation import QuPathGroupedCVConfiguration
from .batch_training.qupath_rtrees import QuPathPixelRTreesConfiguration
from .batch_training.qupath_svm import QuPathPixelSvmConfiguration
from .batch_training.qupath_kronos import QuPathKronosConfiguration

# ==============================================================================
#                               Annotation preview
# ==============================================================================


def plain(value):
    if isinstance(value, Mapping):
        return {key: plain(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(child) for child in value]
    return value


def _pixel_annotation_selection(value, context, entry_id):
    """Bind annotation classes to the selected classifier and included image."""
    if not isinstance(value, Mapping) or set(value) != {"candidate_id", "selection"}:
        raise ValueError("Reopen the classifier References page before drawing annotations.")
    context = validate_training_plan_selection_request(context)
    if context["supervised_options"] is not None:
        raise ValueError("Pixel reference classes require a primary or secondary classifier.")
    candidates = context["candidate_entry_ids"]
    candidate_id = value["candidate_id"]
    if not isinstance(candidate_id, str) or candidate_id not in candidates:
        raise ValueError("Select an offered classifier before opening References.")
    # An individual secondary reference visit can contain just its own draft.
    # Older tickets carry the whole plan, with the same full coverage checks.
    selection = value["selection"]
    selections = selection.get("selections") if isinstance(selection, Mapping) else None
    if (context["secondary_output_classes_by_candidate_id"] and isinstance(selections, (list, tuple))
            and len(selections) == 1):
        candidates = {candidate_id: candidates[candidate_id]}
    response = validate_training_plan_selection_response(
        selection, candidate_entry_ids=candidates, supervised=False
    )
    selected = next((row for row in response.selections
                     if row.candidate_id == value["candidate_id"]), None)
    if (selected is None or entry_id not in context["candidate_entry_ids"][selected.candidate_id]
            or entry_id not in {row.project_entry_id for row in selected.roles}):
        raise ValueError("Select an included reference image for the current classifier.")
    if context["secondary_output_classes_by_candidate_id"] and selected.primary_settings is not None:
        raise ValueError(
            "These annotation settings belong to a primary tissue classifier. "
            "Reopen References for the selected secondary structure classifier."
        )
    return selected, context


def _pixel_reference_names(selected, context):
    if context["secondary_output_classes_by_candidate_id"]:
        return {
            row["canonical_class_id"]: row["display_name"]
            for row in context["secondary_output_classes_by_candidate_id"][selected.candidate_id]["output_classes"]
        }
    if selected.primary_settings is not None:
        primary = plain(selected.primary_settings.to_mapping())
        names = {row["class_id"]: row["display_name"] for row in primary["primary_classes"]}
        if primary["primary_classifier"]["require_ignore_training"]:
            names["Ignore"] = "Ignore"
        return names
    raise ValueError("Reopen References to load the classifier's annotation classes.")


def annotation_reference_labels(value, context, entry_id):
    """Offer exact saved reference names with or without model feedback."""
    selected, context = _pixel_annotation_selection(value, context, entry_id)
    names = list(_pixel_reference_names(selected, context).values())
    validate_reference_class_names(names)
    return names


def validate_preview_selection(value, context, entry_id):
    """Validate preview selection for the current operation.

    The required mapping shape includes ``schema``, ``candidate_id``, ``selection``,
    ``acknowledged_bias``. Field checks are applied before the validated value is returned
    or passed to its next constructor."""

    fields = {"schema", "candidate_id", "selection", "acknowledged_bias"}
    if (
        not isinstance(value, Mapping)
        or set(value) != fields
        or value.get("schema") != "annotation-preview-selection/v1"
        or value.get("acknowledged_bias") is not True
    ):
        raise ValueError(
            "Enable the exploratory annotation preview and acknowledge its potential reference bias before opening it."
        )
    selected, context = _pixel_annotation_selection(
        {key: value[key] for key in ("candidate_id", "selection")}, context, entry_id
    )
    roles = {row.project_entry_id: row.split for row in selected.roles}
    if roles.get(entry_id) != "training":
        raise ValueError(
            "Annotation previews are available only on development images. Reserved validation/test and excluded images cannot show model feedback."
        )
    participants = context["candidate_participants"]
    subject = participants[entry_id]["subject_id"]
    if any(
        roles.get(key) in {"validation", "test"} and row["subject_id"] == subject
        for key, row in participants.items()
    ):
        raise ValueError(
            "A subject cannot supply both preview feedback and held-out validation/test images."
        )
    configuration = plain(selected.model_configuration)
    grid = configuration.get("configuration_schema") == "qupath-grouped-cross-validation/v1"
    if grid:
        configuration = (
            QuPathGroupedCVConfiguration.from_mapping(configuration).candidates[0].to_mapping()
        )
    schema = configuration.get("configuration_schema")
    parsers = {
        "qupath-pixel-rtrees/v1": QuPathPixelRTreesConfiguration,
        "qupath-pixel-svm/v1": QuPathPixelSvmConfiguration,
        "qupath-kronos-rtrees/v2": QuPathKronosConfiguration,
        "qupath-kronos-svm/v2": QuPathKronosConfiguration,
    }
    if schema not in parsers:
        raise ValueError("Choose RF, SVM, KRONOS–GNN–RF or KRONOS–GNN–SVM for preview.")
    model = parsers[schema].from_mapping(configuration)
    if isinstance(model, QuPathKronosConfiguration) and has_pending_marker_statistics(model.encoder):
        raise ValueError(
            "Prediction feedback needs the intensity mean and standard deviation for each new "
            "marker. KRONOS calculates these from training tissue when fitting the classifier "
            "from saved references. Turn off “Show predictions while annotating” to continue "
            "collecting references."
        )
    configuration = plain(model.to_mapping())
    primary = selected.primary_settings
    if primary is not None:
        primary = plain(primary.to_mapping() if hasattr(primary, "to_mapping") else primary)
        selected_channels = set(primary["primary_classifier"]["channel_roles"].values())
        kind = "primary"
        eligible = ["Tissue"]
    else:
        selected_channels = set((selected.secondary_channel_roles or {}).values())
        kind = "secondary"
        eligible = []
    channels = context.get("available_channels_by_candidate_id", {}).get(selected.candidate_id, [])
    if not channels and primary is not None:
        channels = context["application_participants"][0]["channels"]
    if not selected_channels or selected_channels - {row["channel_id"] for row in channels}:
        raise ValueError("The current preview is missing its configured image channels.")
    native = configuration.get("native_configuration", configuration)
    names = _pixel_reference_names(selected, context)
    labels = native["output_labels"]["ordered_labels"]
    ordered = [
        {
            "class_id": row["canonical_class_id"],
            "name": names[row["canonical_class_id"]],
            "code": row["qupath_label"],
        }
        for row in labels
        if row["canonical_class_id"] in names
    ]
    if len(ordered) < 2:
        raise ValueError("Select at least two named reference classes before opening preview.")
    return {
        "schema": "mif-annotation-preview/v1",
        "mode": "exploratory",
        "candidate_id": selected.candidate_id,
        "source_entry_id": entry_id,
        "subject_id": subject,
        "validation_domain_id": context["validation_domain_id"],
        "classifier_role": kind,
        "eligible_parent_ids": eligible,
        "configuration": configuration,
        "labels": ordered,
        "channels": [
            {"channel_id": row["channel_id"], "name": row["name"], "index": index}
            for index, row in enumerate(channels)
            if row["channel_id"] in selected_channels
        ],
        "seed": selected.random_seed,
        "uses_first_grid_candidate": grid,
    }


def _subject_folder(root, domain, subject):
    key = hashlib.sha256(json.dumps([domain, subject], separators=(",", ":")).encode()).hexdigest()
    return Path(root) / "cluster-reviews" / "annotation-feedback" / key


def record_preview_permission(root, choice, ticket_id):
    if not re.fullmatch("[0-9a-f]{32}", str(ticket_id)):
        raise ValueError("The annotation preview needs an exact current review identity.")
    folder = _subject_folder(root, choice["validation_domain_id"], choice["subject_id"])
    folder.mkdir(parents=True, exist_ok=True)
    if any(p.is_symlink() for p in (folder, folder.parent, folder.parent.parent)):
        raise ValueError("The annotation feedback folder was replaced.")
    record = {
        "schema": "annotation-feedback-permission/v1",
        "subject_id": choice["subject_id"],
        "validation_domain_id": choice["validation_domain_id"],
        "entry_id": choice["source_entry_id"],
        "candidate_id": choice["candidate_id"],
        "ticket_id": ticket_id,
        "preview_enabled": True,
    }
    with (folder / (ticket_id + ".json")).open("xb") as stream:
        stream.write(json.dumps(record, sort_keys=True, separators=(",", ":")).encode())
        stream.flush()
        os.fsync(stream.fileno())
    from .cluster.cluster_annotations import _sync

    _sync(folder)
    _sync(folder.parent)


def validate_feedback_roles(root, response, context):
    """Opting in prevents subsequent held-out reuse of that subject."""
    participants = context["candidate_participants"]
    domain = context["validation_domain_id"]
    for selection in plain(response)["selections"]:
        for row in selection["roles"]:
            if row["split"] not in {"validation", "test"}:
                continue
            subject = participants[row["project_entry_id"]]["subject_id"]
            folder = _subject_folder(root, domain, subject)
            if folder.is_symlink():
                raise ValueError("The annotation-feedback history cannot be verified.")
            if folder.exists() and next(folder.glob("*.json"), None) is not None:
                raise ValueError(
                    f"Subject {subject} was enabled for annotation-preview feedback. Keep it in development or leave it out; choose unexposed subjects for an independent validation/test set."
                )


def complete_preview_choice(choice, snapshot, context, *, eligible_parent_ids=None):
    result = plain(choice)
    native_names = snapshot.get("image_channel_names")
    for channel in result["channels"]:
        if not isinstance(native_names, list) or native_names.count(channel["name"]) != 1:
            raise ValueError(
                "The selected preview channels differ from the current QuPath image. Check the channel names in this classifier step."
            )
        channel["index"] = native_names.index(channel["name"])
    if result["classifier_role"] == "secondary":
        result["eligible_parent_ids"] = list(eligible_parent_ids or [])
        if not result["eligible_parent_ids"]:
            raise ValueError(
                "The secondary preview requires its configured eligible parent regions before it can be opened."
            )
    matching = next(
        (
            row
            for row in context.get("application_participants", [])
            if row["project_entry_id"] == result["source_entry_id"]
        ),
        None,
    )
    if matching is not None:
        result["allowed_domain_ids"] = [
            row["analysis_domain_id"] for row in matching["analysis_domain_bindings"]
        ]
    return result


def stage_preview_selection(service, batch_id, text):
    import uuid
    from .cluster.cluster_annotations import _new
    from .cluster.cluster_annotations import _read

    service._require_batch(batch_id)
    if not isinstance(text, str) or len(text.encode()) > 1024 * 1024:
        raise ValueError("The annotation-preview settings exceed the permitted size.")

    def unique(pairs):
        row = {}
        for key, value in pairs:
            if key in row:
                raise ValueError("Repeated preview setting.")
            row[key] = value
        return row

    value = json.loads(
        text,
        object_pairs_hook=unique,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError("A preview setting is not finite.")
        ),
    )
    if (
        not isinstance(value, dict)
        or value.get("schema") != "annotation-preview-selection/v1"
        or value.get("acknowledged_bias") is not True
    ):
        raise ValueError("Confirm the annotation-preview choice before opening it.")
    folder = service._workspace_layout.root / "cluster-reviews" / "preview-selections"
    folder.mkdir(parents=True, exist_ok=True)
    if folder.is_symlink():
        raise ValueError("The preview-settings folder was replaced.")
    identifier = uuid.uuid4().hex
    _new(folder / (identifier + ".json"), value)
    return {"submission_id": identifier}


def read_preview_selection(service, batch_id, identifier):
    from .cluster.cluster_annotations import _read

    service._require_batch(batch_id)
    if not re.fullmatch("[0-9a-f]{32}", str(identifier)):
        raise ValueError("Select the current annotation-preview settings.")
    folder = service._workspace_layout.root / "cluster-reviews" / "preview-selections"
    if folder.is_symlink():
        raise ValueError("The preview-settings folder was replaced.")
    return _read(folder / (identifier + ".json"))
