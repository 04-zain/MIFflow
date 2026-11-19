"""Retain a chosen cell-training plan while its reference annotations are collected.

The existing immutable plan store owns the settings. The review records its locator
and the measured image populations needed by annotation tickets; it does not publish
the plan as available to fitting until the researcher freezes the references.
"""

from collections.abc import Mapping

from ._shared import BatchTrainingError, _array, _closed_mapping, _digest, _sha256, _text, _export_json

REFERENCE_REVIEW_SCHEMA = "supervised-reference-review/v1"
_REVIEW_FIELDS = frozenset({
    "schema_version", "store_identity_sha256", "store_relative_locator",
    "body_sha256", "body_byte_count", "training_plan_set_sha256",
    "candidate_entry_ids", "references",
})


def reference_selection_sha256(plan):
    """Bind labelled cells to their scientific selection independently of the estimator.

    The existing source authorities retain the accepted measurements and
    phenotyping population. Full plan identity continues to own model settings,
    feature preparation and every fitted result.
    """
    request = plan.request
    if request.inherited_reference_selection_sha256 is not None:
        return request.inherited_reference_selection_sha256
    return _digest({
        "batch_id": request.batch_id,
        "classifier_id": request.classifier_id,
        "validation_domain_id": request.validation_domain_id,
        "entries": [entry.to_mapping() for entry in request.entries],
        "required_classes": list(request.required_classes),
        "analysis_domain_binding_set_sha256": plan.analysis_domain_binding_set_sha256,
        "authoring_source_artifact_ref_sha256s": list(plan.authoring_source_artifact_ref_sha256s),
        "point_selection_radius_level0": request.feature_requirements["point_selection_radius_level0"],
    })


def inherited_reference_selection_sha256(request, predecessor):
    """Keep one explicitly linked selection when its label semantics are unchanged.

    Current measurement authorities remain in the new plan. The native reference
    reader independently checks the saved points against current cell geometry.
    """
    old = predecessor.request
    fields = ("batch_id", "classifier_id", "classifier_kind", "validation_domain_id",
              "entries", "analysis_domain_bindings", "required_classes", "eligible_parent_classes")
    if any(getattr(request, key) != getattr(old, key) for key in fields):
        return None
    if (request.feature_requirements["point_selection_radius_level0"]
            != old.feature_requirements["point_selection_radius_level0"]):
        return None
    return reference_selection_sha256(predecessor)


def reference_review_context(plan_set, *, store, measurements):
    """Publish settings once and derive image/role choices from their accepted splits."""
    locator, sha256, byte_count = store.publish(plan_set)
    rows = []
    for plan in plan_set.plans:
        for entry in plan.request.entries:
            sections = [
                {
                    "section_id": item.section_id,
                    "segmentation_generation_id": item.segmentation_generation_id,
                    "segmentation_population_id": item.segmentation_population_id,
                    "measurement_generation_id": item.measurement_generation_id,
                    "analysis_domain_ids": sorted({
                        binding.analysis_domain_id for binding in item.analysis_domain_bindings
                    }),
                }
                for item in measurements if item.project_entry_id == entry.project_entry_id
            ]
            if not sections:
                raise BatchTrainingError("The selected reference image has no current measured cells.")
            rows.append({
                "training_plan_id": plan.training_plan_id,
                "reference_selection_sha256": reference_selection_sha256(plan),
                "project_entry_id": entry.project_entry_id,
                "sample_id": entry.sample_id,
                "slide_id": entry.slide_id,
                "subject_id": entry.subject_id,
                "split": entry.split,
                "reference_role": "training" if entry.split == "training" else "holdout",
                "class_ids": list(plan.request.required_classes),
                "point_selection_radius_level0": float(
                    plan.request.feature_requirements["point_selection_radius_level0"]
                ),
                "sections": sorted(sections, key=lambda item: item["section_id"]),
            })
    return {
        "schema_version": REFERENCE_REVIEW_SCHEMA,
        "store_identity_sha256": store.store_identity_sha256,
        "store_relative_locator": locator,
        "body_sha256": sha256,
        "body_byte_count": byte_count,
        "training_plan_set_sha256": plan_set.training_plan_set_sha256,
        "candidate_entry_ids": {
            plan.request.classifier_id: sorted({item.project_entry_id for item in plan.request.entries})
            for plan in plan_set.plans
        },
        "references": rows,
    }


def validate_reference_review_context(value):
    """Check the form grammar; the immutable store supplies the actual typed plan."""
    context = _closed_mapping(value, fields=_REVIEW_FIELDS, field="supervised reference review")
    if context["schema_version"] != REFERENCE_REVIEW_SCHEMA:
        raise BatchTrainingError("Reopen the current supervised reference review.")
    for key in ("store_identity_sha256", "body_sha256", "training_plan_set_sha256"):
        _sha256(context[key], key)
    _text(context["store_relative_locator"], "store_relative_locator")
    count = context["body_byte_count"]
    if type(count) is not int or count <= 0:
        raise BatchTrainingError("The saved supervised plan size is invalid.")
    if not isinstance(context["candidate_entry_ids"], Mapping):
        raise BatchTrainingError("The supervised reference images are unavailable.")
    rows = _array(context["references"], "supervised references")
    if not rows or any(not isinstance(row, Mapping) for row in rows):
        raise BatchTrainingError("The supervised reference roles are unavailable.")
    return context


def reopen_reference_plan(value, *, store):
    """Read the same plan body through its existing store without authoring new settings."""
    context = validate_reference_review_context(value)
    if context["store_identity_sha256"] != store.store_identity_sha256:
        raise BatchTrainingError("The reference settings belong to another saved analysis.")
    plan_set = store.reopen(
        context["store_relative_locator"], sha256=context["body_sha256"],
        byte_count=context["body_byte_count"],
    )
    if plan_set.training_plan_set_sha256 != context["training_plan_set_sha256"]:
        raise BatchTrainingError("The supervised reference settings have changed.")
    return plan_set


def reference_choice(context, entry_id, role):
    """Resolve an offered image/split choice; its plan determines the reference role."""
    checked = validate_reference_review_context(context)
    choice = _closed_mapping(role, fields=frozenset({"training_plan_id", "split"}),
                             field="supervised annotation choice")
    rows = [row for row in checked["references"]
            if row["project_entry_id"] == entry_id
            and row["split"] == choice["split"]
            and row["training_plan_id"] == choice["training_plan_id"]]
    if len(rows) != 1:
        raise BatchTrainingError("Choose a reference role offered for this image.")
    return _export_json(rows[0])


def validate_reference_completion(value, context):
    """Select the displayed plan; readiness comes from its saved native references."""
    context = validate_reference_review_context(context)
    checked = _closed_mapping(value, fields=frozenset({"training_plan_set_sha256"}),
                             field="supervised reference completion")
    if checked["training_plan_set_sha256"] != context["training_plan_set_sha256"]:
        raise BatchTrainingError("Reopen the current reference plan before fitting.")
    return dict(checked)


def validate_saved_reference_coverage(context, *, store, head, runner):
    """Inspect all included images sequentially in one read-only QuPath process.

    Annotation import owns point binding and publication. At Freeze we read the
    saved bindings against their currently selected cells. No features, pixels
    or encoder calls are needed, and the existing script checks literal project
    and hierarchy bytes around each bounded image lifetime.
    """
    import uuid
    from ..cluster.cluster_annotations import _current_project_sha, run_annotation_script

    checked = validate_reference_review_context(context)
    by_entry = {}
    for row in checked["references"]:
        by_entry.setdefault(row["project_entry_id"], []).append(_export_json(row))
    folder = store.layout.root / "cluster-reviews" / "reference-checks" / uuid.uuid4().hex
    folder.mkdir(parents=True)
    result = run_annotation_script(runner, folder, {
        "mode": "reference-coverage", "project_file": head.project_path,
        "project_sha256": _current_project_sha(head.project_path, head.project_qpproj_snapshot_sha256),
        "references_by_entry": by_entry,
    })
    returned = result.get("references", ())
    keys = [(row["training_plan_id"], row["project_entry_id"], row["split"])
            for row in checked["references"]]
    if sorted((row["training_plan_id"], row["project_entry_id"], row["split"])
              for row in returned) != sorted(keys):
        raise BatchTrainingError("The saved reference images differ from this training plan.")
    cells_by_role = {}
    cells_by_split = {}
    regions_by_role = {}
    training_classes = {}
    for row in returned:
        plan_id, entry_id, split = row["training_plan_id"], row["project_entry_id"], row["split"]
        role = "training" if split == "training" else "holdout"
        for point in row["points"]:
            cells = cells_by_role.setdefault((plan_id, role), set())
            cell = (entry_id, point["cell_id"])
            if cell in cells:
                raise BatchTrainingError("Each cell can have only one reference label in a role.")
            cells.add(cell)
            cells_by_split.setdefault((plan_id, split), set()).add(cell)
            regions_by_role.setdefault((plan_id, role), set()).add((entry_id, point["spatial_region_id"]))
            if role == "training":
                training_classes.setdefault(plan_id, set()).add(point["class_id"])
    plans = {row["training_plan_id"]: set(row["class_ids"])
             for row in checked["references"]}
    for plan_id, classes in plans.items():
        if not classes <= training_classes.get(plan_id, set()):
            raise BatchTrainingError("Add saved training points for every declared cell type before fitting.")
        if (cells_by_role.get((plan_id, "training"), set()) & cells_by_role.get((plan_id, "holdout"), set())
                or regions_by_role.get((plan_id, "training"), set()) & regions_by_role.get((plan_id, "holdout"), set())):
            raise BatchTrainingError("Training and held-out reference cells and point groups must remain separate.")
    required_splits = {(row["training_plan_id"], row["split"])
                       for row in checked["references"]}
    for plan_id, split in required_splits:
        if not cells_by_split.get((plan_id, split)):
            label = {"training": "training", "validation": "validation",
                     "test": "independent test"}[split]
            raise BatchTrainingError(f"Add saved {label} reference points before fitting this plan.")
