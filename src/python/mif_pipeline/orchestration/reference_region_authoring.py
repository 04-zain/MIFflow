"""Native region drafting for the current image-derived spectral reference.

QuPath owns drawing and class assignment. Existing project-owned ROI snapshots
and review publications retain the accepted numerical geometry.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from tempfile import NamedTemporaryFile

from mif_pipeline.io.canonical_ome_tiff import _physical_calibration
from mif_pipeline.preprocessing.unmixing.reference_rois import (
    ReferenceProjectImage, ReferenceRoleRequirement, ReferenceSectionScope,
    ReferenceRoiReviewDecision,
)
from mif_pipeline.qupath_runtime.reference_project import (
    ReferenceProjectLayout, prepare_reference_project, resume_reference_project,
    run_reference_project_prepare, snapshot_reference_rois,
    load_project_owned_reference_roi_export, publish_reference_roi_review,
)

ROLE_LABELS = {"positive": "Positive reference", "af": "Autofluorescence control",
               "negative": "Negative reference", "background": "Image background"}


def _context(operation, run_id, checkpoint_token):
    """Resolve the exact source preparation, or a retained combined project."""
    current, inputs = operation._service.reference_region_inputs(run_id, checkpoint_token)
    fluor = current.current_fluorophore
    identity = inputs[0].input_id if len(inputs) == 1 else fluor
    key = ('source-' if len(inputs) == 1 else '') + hashlib.sha256(
        (run_id + "\n" + identity).encode()).hexdigest()[:24]
    root = operation._root / "reference-regions" / key
    images = []
    for record in inputs:
        header = record.header
        x = _physical_calibration(header.physical_size_x, header.physical_size_x_unit, "X")[2]
        y = _physical_calibration(header.physical_size_y, header.physical_size_y_unit, "Y")[2]
        if x is None or y is None:
            raise ValueError("Complete reference image pixel size before drawing regions.")
        images.append(ReferenceProjectImage(
            record.input_id, header.ome_image_id, header.ome_pixels_id,
            header.source_file_sha256, Path(record.locator_binding.source_locator),
            header.size_x, header.size_y, x, y,
            "negative_control" if record.input_role == "single_stain_negative_control" else record.input_role,
            "shared" if record.input_role == "unstained_control" else fluor,
        ))
    requirements = []
    for image in images:
        requirements.append(ReferenceRoleRequirement(
            "background", image.reference_input_id, image.reference_input_id, 1))
        role = {"single_component_reference": "positive", "unstained_control": "af",
                "negative_control": "negative"}[image.source_role]
        requirements.append(ReferenceRoleRequirement(role, image.source_link_id,
                                                     image.reference_input_id, 1))
    scopes = tuple((image.reference_input_id, (ReferenceSectionScope(
        "whole", "declared_whole_image", None, None, None, "accepted"),)) for image in images)
    return current, inputs, tuple(images), tuple(requirements), scopes, root


def _layout(root):
    return ReferenceProjectLayout(root, root / "reference.qpproj", root / "scripts",
        root / "data", root / "exchange", root / "data/reference-project-request.v1.json",
        root / "scripts/preprocessing/01_annotate_spectral_references.groovy")


def _latest(layout):
    path = layout.root / "current-regions.json"
    if not path.exists():
        return None, None
    receipt = json.loads(path.read_text())
    artifact = Path(receipt["artifact_path"])
    value = load_project_owned_reference_roi_export(layout, artifact)
    if value.content_sha256 != receipt["content_sha256"]:
        raise ValueError("The saved reference-region review changed.")
    return value, artifact


def _save_latest(layout, export, artifact):
    """Retain the last project-owned region revision without changing its contents."""
    destination = layout.root / "current-regions.json"
    with NamedTemporaryFile(mode="w", dir=layout.root, delete=False, encoding="utf-8") as stream:
        json.dump({"artifact_path": str(artifact), "content_sha256": export.content_sha256}, stream)
        temporary = Path(stream.name)
    temporary.replace(destination)


def _summary(current, layout, export, locator=None):
    """Present saved region roles and explicit selections for the current phase."""
    from mif_pipeline.preprocessing.unmixing.reference_rois import _ring_area2
    rows = []
    role_counts = {}
    role = {"shared_control_preparation": "af", "positive_reference_rois": "positive", "af_reference_rois": "af",
            "negative_reference_rois": "negative", "background_reference_rois": "background"}[current.current_checkpoint.value]
    if export is not None:
        by_entry = {entry.project_entry_id: entry for entry in export.entries}
        for roi in export.rois:
            image = by_entry[roi.project_entry_id].image
            if (image.source_role == 'single_component_reference'
                    and image.source_link_id != current.current_fluorophore):
                continue
            if (image.source_role == 'negative_control'
                    and image.source_link_id != current.current_fluorophore):
                continue
            geometry = json.loads(roi.geometry_json)
            polygons = ([geometry["coordinates"]] if geometry["type"] == "Polygon"
                        else geometry["coordinates"])
            pixel_area = sum((abs(_ring_area2(polygon[0]))
                              - sum(abs(_ring_area2(ring)) for ring in polygon[1:])) / 2
                             for polygon in polygons)
            area = pixel_area * image.x_um_per_px * image.y_um_per_px
            key = (image.reference_input_id, roi.roi_role)
            role_counts[key] = role_counts.get(key, 0) + 1
            rows.append({"roi_id": roi.roi_id, "source": image.reference_input_id,
                         "image_name": image.source_path.name, "number": role_counts[key],
                         "role": roi.roi_role, "label": ROLE_LABELS[roi.roi_role],
                         "area_um2": area, "state": roi.review_state})
    accepted = bool(export and all(roi.review_state == "accepted" for roi in export.rois))
    return {"status": "reference_regions", "checkpoint_token": current.content_sha256,
            "checkpoint": current.current_checkpoint.value, "fluorophore": current.current_fluorophore,
            "project_path": str(layout.project_file), "rows": rows, "accepted": accepted,
            "snapshot_sha256": export.content_sha256 if export else None,
            "locator": locator if accepted else None,
            "selected_roi_ids": [row["roi_id"] for row in rows if row["role"] == role],
            "can_edit": current.current_checkpoint.value in {"shared_control_preparation", "positive_reference_rois"}}


def _component_region_resource(operation, inspection):
    """Locate the exact already-registered project; never register during a read."""
    from .reference_unmixing_resources import _strict_load
    export = inspection['values']['roi_export']
    for path in sorted(operation._resources._root.glob('*.json')):
        descriptor = _strict_load(path)
        if (descriptor.get('kind') != 'project_owned_reference_roi_export'
                or descriptor.get('semantic_id') != export.content_sha256):
            continue
        locator = descriptor['locator']
        if operation._resources.resolve_reference_roi_export(locator) != export:
            raise ValueError('The saved component regions differ from their registered source')
        layout = ReferenceProjectLayout(**{key: Path(value)
            for key, value in descriptor['layout'].items()})
        return export, locator, layout
    raise ValueError('The saved component region export is no longer registered')


def inspect_control_regions(service, run_id, checkpoint_token, *, input_id):
    """Read the exact source plan while leaving its accepted/proposed authority live."""
    from dataclasses import fields, replace
    from mif_pipeline.preprocessing.unmixing.reference_service_control_preparation import (
        accepted_preparation, source_input_id, _resolve_preparation)
    current, start = service._store._load_service_checkpoint(run_id)
    if current.content_sha256 != checkpoint_token:
        raise ValueError('The reference preparation changed. Reopen its overview.')
    binding = service._one_artifact(run_id, start, 'reference_control_files', 'reference-control-files').record
    if not binding.prepare_shared_control or source_input_id(binding, current) != input_id:
        raise ValueError('This source is not available for preparation changes in the current review.')
    pending = current.current_checkpoint.value == 'shared_control_preparation' and bool(current.request.proposed_artifacts)
    option = 'retry_shared_control_preparation' if pending else 'revise_shared_control'
    if option not in {item.value for item in current.request.options}:
        raise ValueError('The current review does not offer changes to this source.')
    prepared = (_resolve_preparation(service, current, start,
        current.request.proposed_artifacts[0].pointer, current.prior_checkpoint_sha256, input_id)
        if pending else accepted_preparation(service, current, start, input_id=input_id))
    if prepared is None:
        raise ValueError('This source has no saved preparation.')
    plan = prepared.record.plan
    source = service._store._load_service_generation(run_id, plan.source_checkpoint_sha256)
    if source.current_checkpoint.value != 'shared_control_preparation' or source_input_id(binding, source) != input_id:
        raise ValueError('The saved source preparation does not match its original page.')
    sampling = lambda policy: {'processing_chunk_shape': [policy.processing_chunk_shape.width,
        policy.processing_chunk_shape.height], 'saturation_fraction': policy.saturation_fraction,
        'near_zero_l2_threshold': policy.near_zero_l2_threshold}
    values = {'roi_export': plan.export, 'control_sets': [list(group) for group in prepared.record.control_sets],
        'background_roi_ids': list(plan.background_roi_ids),
        'control_sampling_policy': sampling(plan.control_sampling_policy),
        'background_sampling_policy': sampling(plan.background_sampling_policy),
        'background_policy': {field.name: getattr(plan.background_policy, field.name)
            for field in fields(plan.background_policy) if field.init and field.name != 'policy_version'}}
    view = replace(service._bounded_view(source, start), checkpoint_token=checkpoint_token, revision=current.revision)
    if service.resume(run_id).checkpoint.content_sha256 != checkpoint_token:
        raise ValueError('The source preparation changed while opening it.')
    return {'view': view, 'checkpoint': source, 'values': values, 'component': input_id,
        'phase': 'control_regions', 'checkpoint_token': checkpoint_token, 'revision_option': option}


def component_preparation_region_summary(operation, inspection, *, checkpoint_token):
    """Project retained geometry and policies without changing any native record."""
    export, locator, layout = _component_region_resource(operation, inspection)
    summary = _summary(inspection['checkpoint'], layout, export, locator)
    values = inspection['values']
    control = inspection.get('phase') == 'control_regions'
    selection = ('control_sets', 'background_roi_ids') if control else ('positive_roi_ids', 'background_roi_ids')
    policies = ('control_sampling_policy' if control else 'positive_sampling_policy',
        'background_sampling_policy', 'background_policy')
    summary.update(checkpoint_token=checkpoint_token, preparation_role='control' if control else 'stained',
        project_images=tuple({'input_id': entry.image.reference_input_id,
            'filename': entry.image.source_path.name, 'role': entry.image.source_role}
            for entry in export.entries),
        saved_selection={key: list(values[key]) for key in selection},
        saved_settings={key: values[key] for key in policies})
    return summary


def preparation_region_choices(service, current, start, export):
    """Recover source-route policies from exact service-authorized lineage.

    A revision invalidates numerical results, but not the user's prior settings.
    Walk only the relevant revision's authenticated predecessor; never choose an
    arbitrary artifact or a different source's most recent preparation.
    """
    from dataclasses import fields
    from mif_pipeline.preprocessing.unmixing.reference_service_control_preparation import (
        accepted_preparation, source_input_id, _resolve_preparation, _stored,
    )
    from mif_pipeline.preprocessing.unmixing.reference_service_storage import ReferenceCheckpointConflictError

    binding = service._one_artifact(current.run_id, start,
        'reference_control_files', 'reference-control-files').record
    if not binding.prepare_shared_control:
        return None
    control = current.current_checkpoint.value == 'shared_control_preparation'
    input_id = source_input_id(binding, current)
    fluor = current.current_fluorophore
    canonical = service._canonical_inventory(current.run_id, start)
    source_input = next(item for item in canonical if item.input_id == input_id)
    source = accepted_preparation(service, current, start, canonical_inputs=canonical, input_id=input_id)
    if not control and source is None:
        raise ReferenceCheckpointConflictError('Stained preparation settings require the accepted AF source')
    def background(policy):
        from mif_pipeline.preprocessing.unmixing.reference_service import _background_policy_from_mapping
        values = {field.name: getattr(policy, field.name)
            for field in fields(policy) if field.init and field.name != 'policy_version'}
        if _background_policy_from_mapping(values) != policy:
            raise ReferenceCheckpointConflictError('Saved background policy version cannot be reauthored')
        return values
    sampling = lambda policy: {
        'processing_chunk_shape': [policy.processing_chunk_shape.width, policy.processing_chunk_shape.height],
        'saturation_fraction': policy.saturation_fraction,
        'near_zero_l2_threshold': policy.near_zero_l2_threshold}
    settings = {} if control or source is None else {'background_policy': background(source.record.plan.background_policy)}
    cursor = current
    while True:
        phase = cursor.current_checkpoint.value
        authority = cursor.prior_checkpoint_sha256 if cursor.request.proposed_artifacts else cursor.content_sha256
        plan, groups, choice = None, None, None
        if control:
            retained = accepted_preparation(service, cursor, start, canonical_inputs=canonical, input_id=input_id)
            if phase == 'shared_control_preparation' and source_input_id(binding, cursor) == input_id:
                if cursor.request.proposed_artifacts:
                    retained = _resolve_preparation(service, cursor, start,
                        cursor.request.proposed_artifacts[0].pointer, authority, input_id)
                else:
                    stored = _stored(service, cursor, start, 'shared_control_preparation_plan', input_id, authority)
                    plan = None if stored is None else stored.record
            if retained is not None:
                plan, groups = retained.record.plan, retained.record.control_sets
            if plan is not None:
                if plan.input_id != input_id or plan.input_authority_sha256 != source_input.authority_sha256:
                    raise ReferenceCheckpointConflictError('Saved preparation settings refer to a different source')
                settings = {'control_sampling_policy': sampling(plan.control_sampling_policy),
                    'background_sampling_policy': sampling(plan.background_sampling_policy),
                    'background_policy': background(plan.background_policy)}
                selection = {} if export is None or plan.export.content_sha256 != export.content_sha256 else {
                    'control_sets': [list(group) for group in (groups or plan.control_sets)],
                    'background_roi_ids': list(plan.background_roi_ids)}
                return selection, settings
        elif cursor.current_fluorophore == fluor:
            if phase == 'positive_reference_rois':
                stored = _stored(service, cursor, start, 'stained_region_choice', fluor, authority)
            elif phase != 'shared_control_preparation' and any(item.response.option_value == 'accept_positive_reference_rois'
                     and item.request.fluorophore == fluor for item in cursor.active_decisions):
                _, positive_authority = service._active_roi_decision_authority(run_id=current.run_id,
                    current=cursor, start_authority_sha256=start, checkpoint='positive_reference_rois',
                    fluorophore=fluor, option_value='accept_positive_reference_rois', artifact_role='positive_reference_rois')
                stored = _stored(service, cursor, start, 'stained_region_choice', fluor, positive_authority)
            else:
                stored = None
            choice = None if stored is None else stored.record
            if choice is not None:
                positive = choice.positive_plan
                expected_input = binding.positive_reference_input_ids[current.fluorophores.index(fluor)]
                if positive.fluorophore != fluor or any(item.entry.image.reference_input_id != expected_input
                        for item in positive.selections + choice.background_plan.selections):
                    raise ReferenceCheckpointConflictError('Saved stained settings refer to a different image')
                settings.update(positive_sampling_policy=sampling(positive.sampling_policy),
                    background_sampling_policy=sampling(choice.background_plan.sampling_policy))
                if source is not None and positive.source_preparation_sha256 == source.content_sha256:
                    if choice.background_policy != source.record.plan.background_policy:
                        raise ReferenceCheckpointConflictError('Saved stained background policy differs from its source')
                    settings['background_policy'] = background(choice.background_policy)
                selection = {} if export is None or positive.export.content_sha256 != export.content_sha256 else {
                    'positive_roi_ids': [item.roi_id for item in positive.selections],
                    'background_roi_ids': [item.roi_id for item in choice.background_plan.selections]}
                return selection, settings
        revisions = [item for item in cursor.active_decisions
            if (item.response.option_value in {'revise_shared_control', 'retry_shared_control_preparation'}
                and (binding.shared_unstained_input_id is not None or item.request.fluorophore == fluor))
            or (not control and item.response.option_value == 'reselect_reference_rois'
                and item.request.fluorophore == fluor)]
        if not revisions:
            return {}, settings
        decision = revisions[-1]
        previous, result, loaded_start = service._store._load_service_decision_authority(current.run_id,
            source_checkpoint_sha256=decision.source_checkpoint_sha256,
            decision_record_sha256=decision.content_sha256)
        if loaded_start != start or previous.request != decision.request or result.decisions[-1] != decision:
            raise ReferenceCheckpointConflictError('Saved preparation settings lineage differs')
        cursor = previous


def _native_authoring_entries(entries, requirements):
    """Use the exact scientific role required by each saved project entry."""
    guides = []
    for entry in entries:
        image = entry.image
        roles = {item.roi_role for item in requirements if item.roi_role != 'background'
            and item.reference_input_id == image.reference_input_id
            and item.role_link_id == image.source_link_id}
        if len(roles) != 1:
            raise ValueError('The reference image must have one exact tissue annotation role.')
        guides.append({'entry_id': entry.project_entry_id, 'input_id': image.reference_input_id,
            'source_uri': image.source_path.as_uri(), 'role': next(iter(roles)),
            'image_name': image.source_path.name})
    return guides


def reference_region_action(operation, run_id, checkpoint_token, action, *, runner=None,
                            snapshot_sha256=None, reviewer_id=None, component=None, preparation_phase=None):
    """Edit, reopen or explicitly accept the current reference-region draft.

    Later control/background steps reuse these accepted regions. Editing starts
    at positive-region selection so dependent sampling cannot become stale.
    """
    display_run, display_token = run_id, checkpoint_token
    run_id, checkpoint_token = operation.reference_service_identity(run_id, checkpoint_token)
    inspection = None
    if component is None:
        current, inputs, images, requirements, scopes, root = _context(operation, run_id, checkpoint_token)
        project_id = "reference-regions-" + root.name
        layout = _layout(root)
    else:
        if preparation_phase == 'control_regions':
            inspection = inspect_control_regions(operation._service, run_id, checkpoint_token, input_id=component)
        elif preparation_phase in {None, 'regions'}:
            inspection = operation._service.inspect_component_preparation(run_id, checkpoint_token,
                component=component, phase='positive_reference_rois')
        else:
            raise ValueError('Native regions require the exact preparation page.')
        retained, _locator, layout = _component_region_resource(operation, inspection)
        current = inspection['checkpoint']
        _live, _start, _target, canonical = operation._service._basis_choice_inputs(run_id)
        by_id = {record.input_id: record for record in canonical}
        images = tuple(entry.image for entry in retained.entries)
        control = preparation_phase == 'control_regions'
        if any((image.reference_input_id != component or image.source_role not in {'unstained_control', 'negative_control'})
               if control else (image.source_role != 'single_component_reference' or image.source_link_id != component)
               for image in images):
            raise ValueError('Open the original combined region review for this reference source')
        inputs = tuple(by_id[image.reference_input_id] for image in images)
        requirements = retained.role_requirements
        scopes = tuple((entry.image.reference_input_id, entry.section_candidates) for entry in retained.entries)
        root, project_id = layout.root, retained.project_id
    if action == "edit":
        if current.current_checkpoint.value not in {"shared_control_preparation", "positive_reference_rois"}:
            raise ValueError("Return to positive-region selection to edit regions and repeat dependent sampling.")
        if runner is None:
            raise ValueError("A native QuPath session is required to edit regions.")
        layout = (resume_reference_project if layout.request_path.exists() else prepare_reference_project)(
            root, project_id=project_id, images=images, canonical_inputs=inputs)
        previous, _ = _latest(layout)
        # Native preparation is needed only once. Reopening preserves all annotations.
        if not (layout.exchange_dir / "prepare-live-evidence.v2.json").exists():
            run_reference_project_prepare(runner, layout, images=images, canonical_inputs=inputs,
                                          section_scopes=scopes)
        # Bind guidance to the same immutable entry/source identities used by
        # the snapshot. Opening one source never guesses from its display name.
        from mif_pipeline.preprocessing.unmixing.reference_rois import loads_reference_project_evidence
        evidence = layout.exchange_dir / "prepare-live-evidence.v2.json"
        raw = layout.request_path.read_bytes()
        entries = loads_reference_project_evidence(evidence.read_bytes(), request_source=raw,
            expected_request_sha256=hashlib.sha256(raw).hexdigest(), expected_project_id=project_id,
            expected_project_file=layout.project_file, expected_images=images, section_scopes=scopes)
        guide_entries = _native_authoring_entries(entries, requirements)
        result = runner.run_interactive_project(layout.project_file, log_directory=root / "logs",
            reference_authoring={'entries': guide_entries, 'initial_entry_id': guide_entries[0]['entry_id']})
        if (getattr(result, "returncode", None) != 0
                or getattr(getattr(result, "drain_evidence", None), "termination_reason", None) != "normal_exit"):
            raise ValueError("QuPath did not close normally; keep the current reference review open.")
        if operation._service.resume(run_id).checkpoint.content_sha256 != checkpoint_token:
            raise ValueError("The reference review changed while QuPath was open. Reopen the current review.")
        snapshot = snapshot_reference_rois(runner, layout, project_id=project_id, entries=entries,
            role_requirements=requirements, project_review_state="accepted",
            created_at=datetime.now(timezone.utc).isoformat(), predecessor=previous)
        _save_latest(layout, snapshot.export, snapshot.artifact_path)
    elif action not in {"load", "accept"}:
        raise ValueError("Unknown reference-region action.")
    export, artifact = _latest(layout)
    if action == "accept":
        if export is None or export.content_sha256 != snapshot_sha256:
            raise ValueError("Reload the displayed reference regions before accepting them.")
        if not reviewer_id:
            raise ValueError("Reference-region acceptance requires the current reviewer.")
        decisions = tuple(ReferenceRoiReviewDecision(roi.roi_id, roi.review_binding_sha256, "accepted")
                          for roi in export.rois if roi.review_state == "pending")
        if decisions:
            review = publish_reference_roi_review(layout, snapshot=export, decisions=decisions,
                reviewer_id=reviewer_id, reviewed_at=datetime.now(timezone.utc).isoformat())
            export, artifact = review.review.export, review.artifact_path
            _save_latest(layout, export, artifact)
    locator = None
    if export is not None and all(roi.review_state == "accepted" for roi in export.rois):
        locator = "native-reference-regions:" + export.content_sha256
        operation.register_reference_roi_export(locator, layout=layout, artifact_path=artifact)
    summary = _summary(current, layout, export, locator)
    from .reference_region_selection import saved_region_choices
    snapshot = operation._service.resume(run_id)
    if len(inputs) == 1:
        summary['preparation_role'] = ('control' if inputs[0].input_role in {
            'unstained_control', 'single_stain_negative_control'} else 'stained')
    summary['project_images'] = tuple({'input_id': image.reference_input_id,
        'filename': image.source_path.name, 'role': image.source_role} for image in images)
    if inspection is not None:
        values = inspection['values']
        control = preparation_phase == 'control_regions'
        selection = ('control_sets', 'background_roi_ids') if control else ('positive_roi_ids', 'background_roi_ids')
        prepared = ({key: list(values[key]) for key in selection}
            if export is not None and export.content_sha256 == values['roi_export'].content_sha256 else {},
            {key: values[key] for key in
                ('control_sampling_policy' if control else 'positive_sampling_policy',
                 'background_sampling_policy', 'background_policy')})
    else:
        prepared = preparation_region_choices(operation._service, current, snapshot.start_authority_sha256, export)
    if prepared is not None:
        summary['saved_selection'], summary['saved_settings'] = prepared
    else:
        summary['saved_selection'], summary['saved_settings'] = saved_region_choices(
            operation._service, current, snapshot.start_authority_sha256, export)
    if current.current_checkpoint.value in {"af_reference_rois", "negative_reference_rois", "background_reference_rois"}:
        snapshot = operation._service.resume(run_id)
        values = dict(operation._service._saved_positive_region_choices(current, snapshot.start_authority_sha256))
        values["roi_choices"] = tuple(dict(row) for row in values["roi_choices"])
        summary["saved_positive_regions"] = values
    if display_run != run_id or inspection is not None:
        # The authoring project belongs to the cycle task; the desktop retains
        # the parent review token so its next action cannot target another cycle.
        operation.reference_service_identity(display_run, display_token)
        summary["checkpoint_token"] = display_token
    return summary
