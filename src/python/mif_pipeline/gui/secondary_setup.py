"""Present the group's first secondary-structure choice using its accepted inputs."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
from pathlib import Path
import re

from mif_pipeline.gui.interactions import ControlKind, FormControl, FormModel
from mif_pipeline.orchestration.batch_secondary_definitions import (
    RESPONSE_SCHEMA, secondary_definition_review, validate_definition_context,
    validate_secondary_definition_choice,
)
from mif_pipeline.pipeline_core import StageResult
from mif_pipeline.pipeline_core.config import ClassifierSpec, ClassifierMode, StructureCountBounds


def secondary_structure_draft(path: Path, context: Mapping[str, object]) -> dict[str, object]:
    """Read one supplied criterion profile; leave scientific parents/channels explicit."""
    from mif_pipeline.orchestration.batch_semantic_profile_import import read_semantic_profile_source

    context = validate_definition_context(context)
    path = path.expanduser().resolve(strict=True)
    data, profile = read_semantic_profile_source(path)
    classifier = ClassifierSpec(
        "secondary-" + profile.structure_id,
        {},
        ClassifierMode.TRAIN_NEW, validation_domain_id=context["validation_domain_id"],
    ).to_mapping()
    value = {
        "structure_id": profile.structure_id,
        "display_name": profile.structure_id.replace("_", " ").replace("-", " ").capitalize(),
        "eligible_parent_ids": [],
        "classifier": classifier,
        "count_bounds": StructureCountBounds().to_mapping(secondary=True),
        "semantic_profile": {"profile_id": profile.profile_id,
                             "profile_version": profile.profile_version,
                             "content_sha256": hashlib.sha256(data).hexdigest(),
                             "artifact_path": str(path)},
    }
    value["_profile_content"] = profile.to_mapping()
    value["_profile_reference"] = dict(value["semantic_profile"])
    value["_profile_original"] = profile.to_mapping()
    return value


def new_secondary_structure_draft(name: str, context: Mapping[str, object]) -> dict[str, object]:
    """Start an unsubmitted structure without inventing biological conditions."""
    context = validate_definition_context(context)
    name = name.strip()
    structure_id = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if not name or not structure_id:
        raise ValueError("Enter a structure name containing letters or numbers.")
    classifier = ClassifierSpec(
        "secondary-" + structure_id,
        {},
        ClassifierMode.TRAIN_NEW, validation_domain_id=context["validation_domain_id"],
    ).to_mapping()
    return {
        "structure_id": structure_id, "display_name": name,
        "eligible_parent_ids": [], "classifier": classifier,
        "count_bounds": StructureCountBounds().to_mapping(secondary=True),
        "semantic_profile": {
            "schema_version": "semantic-profile/v4",
            "profile_id": "secondary-" + structure_id, "profile_version": "4",
            "structure_id": structure_id, "criteria": None,
        },
    }


def definition_classifier_groups(rows):
    groups = {}
    for row in rows:
        groups.setdefault(row["classifier"]["classifier_id"], []).append(row)
    return groups


def definition_classifier_route(rows):
    groups = definition_classifier_groups(rows)
    if len(groups) == len(rows) and not any(row.get("mutually_exclusive") for row in rows):
        return "separate"
    if len(groups) == 1 and len(rows) > 1 and all(row.get("mutually_exclusive") for row in rows):
        return "joint"
    return "saved"


def planned_secondary_definitions(draft_rows, route, saved_groups):
    """Derive the proposed grouping without changing authored classifier or criterion settings."""
    rows = deepcopy(draft_rows)
    groups = definition_classifier_groups(rows)
    for classifier_id, members in groups.items():
        previous = saved_groups.get(classifier_id)
        if previous and set(previous) != {row["structure_id"] for row in members}:
            # A surviving member of a shared classifier now needs its own model labels.
            if len(members) == 1:
                members[0]["mutually_exclusive"] = True
            apply_secondary_classifier_route(members, joint=len(members) > 1,
                                             reserved_ids=groups.keys() - {classifier_id})
    if route != "saved" and route != definition_classifier_route(rows):
        apply_secondary_classifier_route(rows, joint=route == "joint")
    return rows


def available_classifier_id(base, used):
    candidate = base
    suffix = 1
    while candidate in used:
        suffix += 1
        candidate = f"{base}-{suffix}"
    return candidate


def apply_secondary_classifier_route(rows, *, joint, reserved_ids=()):
    """Express the chosen route through the existing classifier identities."""
    from mif_pipeline.pipeline_core.config import secondary_classifier_output_classes

    if not rows:
        return
    def canonicalize_review_channels(value, roles):
        if isinstance(value, dict):
            if value.get("type") in {"cell_measurement_aggregate", "population_marker_fraction"}:
                selector = value.get("selector", {})
                role = selector.get("channel_role")
                if role in roles:
                    selector["channel_role"] = roles[role]
            for child in value.values():
                canonicalize_review_channels(child, roles)
        elif isinstance(value, list):
            for child in value:
                canonicalize_review_channels(child, roles)

    def preserve_review_channels(row, remaining_roles):
        # Only replace bindings that this route edit would discard or change.
        replaced = {role: channel for role, channel in row["classifier"]["channel_roles"].items()
                    if remaining_roles.get(role) != channel}
        canonicalize_review_channels(row.get("_profile_content", row["semantic_profile"]), replaced)

    joint = joint and len(rows) > 1
    if joint:
        shared = deepcopy(rows[0]["classifier"])
        if len(definition_classifier_groups(rows)) > 1:
            shared.update(classifier_id=available_classifier_id("secondary-joint", reserved_ids), channel_roles={})
        shared["output_classes"] = list(secondary_classifier_output_classes(
            tuple(row["structure_id"] for row in rows), joint=True))
        for row in rows:
            preserve_review_channels(row, shared["channel_roles"])
            row["classifier"] = deepcopy(shared)
            row["mutually_exclusive"] = True
    else:
        groups = definition_classifier_groups(rows)
        used = set(reserved_ids) | {key for key, members in groups.items()
                                    if len(members) == 1 and not members[0].get("mutually_exclusive")}
        for row in rows:
            if row.get("mutually_exclusive") or len(groups[row["classifier"]["classifier_id"]]) > 1:
                preserve_review_channels(row, {})
                classifier_id = available_classifier_id("secondary-" + row["structure_id"], used)
                used.add(classifier_id)
                row["classifier"].update(classifier_id=classifier_id, channel_roles={})
            elif "mutually_exclusive" in row:
                continue  # An existing independent classifier retains its declared output inventory.
            row["classifier"]["output_classes"] = list(secondary_classifier_output_classes(
                (row["structure_id"],), joint=False))
            row["mutually_exclusive"] = False


def build_secondary_setup_form(result: StageResult) -> FormModel:
    if type(result) is not StageResult or result != secondary_definition_review(result.resume_context):
        raise ValueError("Refresh the secondary structure definition review.")
    initial = result.resume_context.get("initial_secondary_structures")
    return FormModel(
        form_id="secondary-structure-definition", title="Choose structures",
        message="Choose the structures to detect. Add subtypes if you want to distinguish forms of a structure during image review.",
        allowed_actions=result.allowed_actions,
        controls=(FormControl("definitions", "Structures", ControlKind.JSON_OBJECT,
                              value={"selection": None if initial is None else "define" if initial else "none",
                                     "structures": [] if initial is None else initial}),),
        context={"adapter": "secondary_definition", "definition_context": result.resume_context,
                 "stage_result": result.to_mapping()},
    )


def submit_secondary_setup_form(form: FormModel, action: str, values: Mapping[str, object]) -> dict[str, object]:
    if form.context.get("adapter") != "secondary_definition" or action not in form.allowed_actions:
        raise ValueError("Refresh the secondary structure definition review.")
    draft = values.get("definitions")
    if not isinstance(draft, Mapping) or draft.get("selection") not in {"none", "define"}:
        raise ValueError("Add a structure, or continue without structures.")
    rows = draft.get("structures", ()) if draft["selection"] == "define" else ()
    if draft["selection"] == "define" and not rows:
        raise ValueError("Add a structure, or continue without structures.")
    choice = {"schema_version": RESPONSE_SCHEMA, "secondary_structures": rows}
    validate_secondary_definition_choice(choice, form.context["definition_context"])
    return choice
