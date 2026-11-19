"""Final target-AF decisions over retained control measurements.

This module projects and validates a complete inventory. It never samples pixels,
changes extraction pairings, or treats a display curve as publication authority.
"""
from __future__ import annotations

from dataclasses import replace
import math

from .compatibility import compare_structural_compatibility, compare_quantitative_transformability
from .purification import combine_af_regional_means
from .reference_defaults import fixed_basis_review_defaults
from .reference_service_storage import ReferenceCheckpointConflictError
from .reference_service_target_af import (
    TargetAfRetainedRoiAuthority, TargetAfSharedRoiAuthority,
    TargetAfSourceReviewSelection, TargetAfRoiReviewSelection,
)
from .target_af import (
    FixedBasisColumnEvidence, _evaluate_fixed_basis, _target_background_values,
    TargetAfRoiProposal, ReviewedTargetAfRoiSelection, ProposalReviewDecision,
    TargetAfEndmemberCandidate,
)
from .typed_checksum import typed_checksum_sha256


GROUPS_OPTION = "review_reference_control_groups"


def _selection(inventory, selection):
    """Reject spectra, weights, foreign IDs, incomplete decisions and extra fields."""
    if type(selection) is not dict or set(selection) != {"sources", "regions"}:
        raise ValueError("AF group selection requires sources and regions only")
    source_rows, region_rows = selection["sources"], selection["regions"]
    if type(source_rows) not in {list, tuple} or type(region_rows) not in {list, tuple}:
        raise ValueError("AF group decisions must be ordered lists")
    sources = []
    for row in source_rows:
        if type(row) is not dict or set(row) != {"input_id", "preparation_disposition", "tissue_context_disposition"}:
            raise ValueError("AF source decisions contain unsupported fields")
        sources.append(TargetAfSourceReviewSelection(**row))
    regions = []
    for row in region_rows:
        if type(row) is not dict or set(row) != {"roi_id", "included", "component_identity"}:
            raise ValueError("AF region decisions contain unsupported fields")
        regions.append(TargetAfRoiReviewSelection(**row))
    if tuple(row.input_id for row in sources) != tuple(row["input_id"] for row in inventory["sources"]):
        raise ValueError("AF source decisions must cover the exact offered inventory in order")
    if tuple(row.roi_id for row in regions) != tuple(row["roi_id"] for row in inventory["regions"]):
        raise ValueError("AF region decisions must cover the exact offered inventory in order")
    return tuple(sources), tuple(regions)


def project_target_af_groups_selection(inventory, selection):
    """Recompute display groups from saved signed regional means; no acceptance."""
    from .reference_service import _fixed_basis_qc_policy_from_mapping, _TARGET_AF_QC_POLICY_VERSION

    sources, reviews = _selection(inventory, selection)
    source_by_id = {row.input_id: row for row in sources}
    source_facts = {row["input_id"]: row for row in inventory["sources"]}
    issues, regions, grouped = [], [], {}
    fluor_names = {name.casefold() for name in inventory.get("fluorophore_identities", ())}
    names = {}
    for original, review in zip(inventory["regions"], reviews, strict=True):
        row = {**original, "included": review.included, "component_identity": review.component_identity}
        regions.append(row)
        if not review.included:
            continue
        source = source_by_id[row["input_id"]]
        if not source.accepted:
            issues.append(f"Review whether {source_facts[source.input_id]['label']} is suitable for the mixed image.")
        if not source_facts[source.input_id]["acquisition_compatible"]:
            issues.append(f"{source_facts[source.input_id]['label']} has imaging settings incompatible with the mixed image.")
        if not row["usable"]:
            issues.append(f"Exclude {row['label']}: its corrected signal is unusable.")
        name = review.component_identity
        if not name:
            issues.append(f"Assign an AF group to {row['label']}.")
            continue
        key = name.casefold()
        if key in fluor_names:
            issues.append(f"AF group {name!r} conflicts with a fluorophore identity.")
        if key in names and names[key] != name:
            issues.append("AF group names must be distinct without case-only differences.")
        names[key] = name
        grouped.setdefault(name, []).append(row)
    if not grouped:
        issues.append("Include at least one usable region and assign an AF group.")
    components = []
    for name, members in grouped.items():
        if any(not item["usable"] for item in members):
            continue
        try:
            combined = combine_af_regional_means(tuple(tuple(row["signed_values"]) for row in members))
            if math.hypot(*combined.corrected_mean) <= inventory.get("minimum_l2_norm", 1e-8):
                raise ValueError("insufficient corrected signal")
        except (TypeError, ValueError) as exc:
            issues.append(f"AF group {name!r} cannot be used: {exc}.")
            continue
        total = math.fsum(combined.corrected_mean)
        counts = {}
        for member in members:
            counts[member["input_id"]] = counts.get(member["input_id"], 0) + 1
        components.append({"component": name, "roi_ids": [row["roi_id"] for row in members],
            "roi_weights": list(combined.roi_weights), "signed_values": list(combined.signed_mean),
            "corrected_values": list(combined.corrected_mean),
            "values": [value / total for value in combined.corrected_mean],
            "source_contributions": [{"input_id": source_id, "region_count": count,
                                      "weight": count / len(members)} for source_id, count in counts.items()]})
    quality = None
    if components and len(components) == len(grouped):
        policy = _fixed_basis_qc_policy_from_mapping(fixed_basis_review_defaults(),
            purpose="reference-unmixing-target-af-qc-policy/v1", policy_version=_TARGET_AF_QC_POLICY_VERSION)
        axis = tuple(inventory.get("measurement_ids", inventory["measurement_names"]))
        if all(len(row["values"]) == len(axis) for row in components):
            qc = _evaluate_fixed_basis(tuple(FixedBasisColumnEvidence(row["component"],
                "target_autofluorescence", axis, tuple(row["values"]), "unit_l1", 1.,
                typed_checksum_sha256(row)) for row in components), policy)
            quality = qc.disposition
            if qc.disposition == "failed":
                reasons = {"component_count_exceeds_measurements": "more groups than acquired bands",
                    "rank_deficient": "groups cannot be separated", "condition_number_failed": "unstable separation"}
                general = [reasons.get(code, code.replace("_", " ")) for code in qc.reason_codes
                           if not code.startswith("pairwise_similarity_")]
                if general:
                    issues.append("The AF groups fail spectral quality checks: " + ", ".join(general) + ".")
                for pair in qc.pairwise_spectra:
                    if pair.cosine_similarity >= qc.policy.cosine_fail_threshold:
                        issues.append(f"{pair.first_component_identity} and {pair.second_component_identity} "
                            "are too similar to use as separate references. Merge them or revise their regions.")
    return {**inventory, "sources": [{**row,
                "preparation_disposition": decision.preparation_disposition,
                "tissue_context_disposition": decision.tissue_context_disposition}
            for row, decision in zip(inventory["sources"], sources, strict=True)],
        "regions": regions, "components": components,
        "selection": {"sources": [dict(row) for row in selection["sources"]],
                      "regions": [dict(row) for row in selection["regions"]]},
        "issues": list(dict.fromkeys(issues)), "can_apply": not issues, "quality": quality}


def _previous_plan(service, current, start):
    """Only the current pending proposal or its explicitly reselected predecessor."""
    pointers = []
    review_request = None
    decisions = current.active_decisions
    for index in range(len(decisions) - 1, -1, -1):
        if decisions[index].response.option_value == "revise_shared_control":
            decisions = decisions[index + 1:]
            break
    if current.request and current.request.checkpoint.value == "target_af_control_review":
        review_request = current.request
        pointers = [item.pointer for item in current.request.proposed_artifacts]
    if not pointers and current.request and current.request.checkpoint.value in {"target_af", "target_af_control_review"}:
        for decision in reversed(decisions):
            if decision.response.option_value == "reselect_target_af_source":
                if review_request is None:
                    review_request = decision.request
                pointers = [item.pointer for item in review_request.proposed_artifacts]
                break
    if not pointers and current.request and current.request.checkpoint.value == 'target_af':
        # A stain revision changes the assembled library, not the independently
        # reviewed AF grouping/normalization. Read its exact former model as
        # defaults and retained samples; it is never treated as a live new model.
        from .reference_workflow import ReferenceTransitionKind
        from .interactions import ArtifactPointer
        last_source_revision = max((item.response.request_revision for item in current.active_decisions
            if item.response.option_value == 'revise_shared_control'), default=-1)
        for transition in reversed(current.transition_log):
            if (transition.kind is not ReferenceTransitionKind.PREPARATION_REVISION
                    or transition.revision <= last_source_revision):
                continue
            origin = service._store._load_service_generation(current.run_id, transition.source_checkpoint_sha256)
            if origin.target_af_artifact is not None:
                previous = service._artifacts.load_pointer(run_id=current.run_id,
                    start_authority_sha256=start, kind='target_af_artifact', subject_id='target-af',
                    pointer=origin.target_af_artifact).record.proposal
                pointers = [ArtifactPointer('proposed_target_af_qc', previous.artifact_id,
                    previous.proposal_sha256, None)]
                break
            if origin.request and origin.request.checkpoint.value == 'target_af_control_review':
                review_request = origin.request
                pointers = [item.pointer for item in origin.request.proposed_artifacts]
                if pointers:
                    break
    pointers = [item for item in pointers if item.role == "proposed_target_af_qc"]
    interrupted_compatibility = None
    if not pointers:
        # The group proposal is durable before entering its review. A crash
        # before attaching its pointer leaves an empty review; recover only the
        # proposal bound to that exact source-selection decision, never an
        # arbitrary older reselected proposal.
        source_decision = next((item for item in reversed(decisions)
            if item.request.checkpoint.value == "target_af"), None)
        if (review_request is None or source_decision is None
                or source_decision.response.option_value != GROUPS_OPTION
                or review_request.target_control_compatibility is None
                or source_decision.request.target_control_compatibility != review_request.target_control_compatibility):
            return None
        interrupted_compatibility = review_request.target_control_compatibility
    elif len(pointers) != 1:
        raise ReferenceCheckpointConflictError("prior target-AF proposal authority is ambiguous")
    proposals = service._artifacts.load_all(run_id=current.run_id, start_authority_sha256=start,
        kind="target_af_proposal", subject_id="target-af")
    if interrupted_compatibility is not None:
        matches = [item for item in proposals if item.record.control_binding.selection_kind == GROUPS_OPTION
                   and item.record.control_binding.compatibility == interrupted_compatibility]
    else:
        matches = [item for item in proposals if item.record.proposal_sha256 == pointers[0].artifact_sha256
                   and item.record.artifact_id == pointers[0].artifact_id]
    if len(matches) != 1:
        raise ReferenceCheckpointConflictError("prior target-AF proposal authority is missing")
    stored = matches[0]
    plan = service._artifacts.load(run_id=current.run_id, start_authority_sha256=start,
        authority_checkpoint_sha256=stored.authority_checkpoint_sha256,
        kind="target_af_sampling_plan", subject_id="target-af").record
    return plan, stored.record


def retained_target_af_regions(service, current, start):
    """Resolve live accepted controls once per source/ROI, retaining every use."""
    stored_inputs = service._artifacts._load_all_canonical_inputs(run_id=current.run_id,
                                                               start_authority_sha256=start)
    by_id = {item.record.input_id: item for item in stored_inputs}
    if len(by_id) != len(stored_inputs):
        raise ReferenceCheckpointConflictError("canonical AF sources are ambiguous")
    previous = _previous_plan(service, current, start)
    regions = {}
    from .reference_service_control_preparation import accepted_preparations, source_revision_affects
    from .background import resolve_background_model
    preparations = accepted_preparations(service, current, start,
        canonical_inputs=tuple(item.record for item in stored_inputs))
    prepared_by_input = {item.record.plan.input_id: item for item in preparations}
    prepared_by_sha256 = {item.content_sha256: item for item in preparations}
    for prepared in preparations:
        source = prepared.record
        for sample in source.controls:
            background = resolve_background_model(sample, sample, source.backgrounds,
                source.backgrounds, (), None, source.plan.background_policy)
            regions[(source.plan.input_id, sample.selection.roi_id)] = TargetAfRetainedRoiAuthority(
                sample, source.plan.control_sampling_policy, background, (), prepared.content_sha256)
    for fluor in current.fluorophores:
        decisions = current.active_decisions
        for index in range(len(decisions) - 1, -1, -1):
            if source_revision_affects(decisions[index],
                    None if current.control_strategy == "shared_unstained_control" else fluor):
                decisions = decisions[index + 1:]
                break
        phases = tuple(phase for phase in ("af_reference_rois", "negative_reference_rois") if any(
            decision.request.checkpoint.value == phase and decision.request.fluorophore == fluor
            and decision.response.option_value == "accept_" + phase for decision in decisions))
        if not phases:
            continue
        if len(phases) != 1:
            raise ReferenceCheckpointConflictError("live control region phase is ambiguous")
        phase = phases[0]
        def phase_records(name):
            decision, authority = service._active_roi_decision_authority(run_id=current.run_id,
                current=current, start_authority_sha256=start, checkpoint=name, fluorophore=fluor,
                option_value="accept_" + name, artifact_role=name)
            plan = service._artifacts.load(run_id=current.run_id, start_authority_sha256=start,
                authority_checkpoint_sha256=authority, kind="roi_selection_plan", subject_id=f"{fluor}:{name}")
            bundle = service._load_roi_artifact(current, start, authority,
                "sampled_roi_bundle", f"{fluor}:{name}",
                accepted_source=prepared_by_sha256.get(plan.record.source_preparation_sha256))
            pointer = decision.response.artifacts[0]
            if (bundle.record.plan != plan.record or pointer.artifact_id != plan.content_sha256
                    or pointer.artifact_sha256 != plan.record.export.content_sha256):
                raise ReferenceCheckpointConflictError("live control sampling pointer lineage differs")
            return decision, plan, bundle, authority
        decision, plan, bundle, _ = phase_records(phase)
        bg_decision, bg_plan, bg_bundle, bg_authority = phase_records("background_reference_rois")
        resolutions = service._artifacts.load(run_id=current.run_id, start_authority_sha256=start,
            authority_checkpoint_sha256=bg_authority, kind="background_resolutions", subject_id=fluor).record
        operation = service._artifacts.load(run_id=current.run_id, start_authority_sha256=start,
            authority_checkpoint_sha256=bg_authority, kind="background_operation", subject_id=fluor).record
        if (len(resolutions) != len(plan.record.pairings)
                or operation.control_bundle_sha256 != bundle.record.content_sha256
                or operation.background_bundle_sha256 != bg_bundle.record.content_sha256
                or operation.resolution_sha256s != tuple(item.resolution_sha256 for item in resolutions)):
            raise ReferenceCheckpointConflictError("live control/background operation differs")
        for sample in bundle.record.samples:
            source_id = sample.input_evidence.input_id
            prepared = prepared_by_input.get(source_id)
            source = by_id.get(source_id)
            if source is None or source.record.evidence != sample.input_evidence:
                raise ReferenceCheckpointConflictError("retained AF sample source authority changed")
            selected_resolutions = tuple(resolution for pairing, resolution in
                zip(plan.record.pairings, resolutions, strict=True)
                if pairing.control_roi_id == sample.selection.roi_id)
            if not selected_resolutions:
                raise ReferenceCheckpointConflictError("retained AF region has no live matched background")
            first = selected_resolutions[0]
            for resolution in selected_resolutions:
                if (resolution.status != "resolved" or resolution.policy != operation.policy
                        or resolution.control_side is None
                        or resolution.control_side.source_input_id != source_id
                        or resolution.control_side.source_image_sha256 != sample.input_evidence.source_image_sha256
                        or not set(resolution.control_side.source_rois).issubset(bg_bundle.record.samples +
                            (() if prepared is None else prepared.record.backgrounds))
                        or resolution.control_side != first.control_side):
                    raise ReferenceCheckpointConflictError("retained AF region has conflicting background authority")
            lineage = TargetAfSharedRoiAuthority(fluor, (sample.selection.roi_id,),
                decision.content_sha256, plan.content_sha256, bundle.content_sha256,
                bg_decision.content_sha256, bg_plan.content_sha256, bg_bundle.content_sha256,
                (first.resolution_sha256,))
            # Exact ROI identity is independent of the stain that reused it.
            key = (source_id, sample.selection.roi_id)
            retained = TargetAfRetainedRoiAuthority(sample, plan.record.sampling_policy, first, (lineage,))
            prior = regions.get(key)
            if prior is not None:
                if (prior.source_roi != sample or prior.sampling_policy != retained.sampling_policy
                        or prior.background.control_side != first.control_side
                        or prior.background.policy != first.policy):
                    raise ReferenceCheckpointConflictError("one AF region has conflicting live sampling/background authorities")
                retained = replace(prior, extraction_authorities=(*prior.extraction_authorities, lineage))
            regions[key] = retained
    # A separately sampled target blank has no extraction phase; reuse its exact
    # pending plan and sampled background on edit, with no fresh source reads.
    if previous is not None:
        old_plan, proposal = previous
        if old_plan.selection_kind == "select_target_unstained_control":
            bundle = service._artifacts.load(run_id=current.run_id, start_authority_sha256=start,
                authority_checkpoint_sha256=old_plan.source_checkpoint_sha256,
                kind="target_af_sample_bundle", subject_id="target-af").record
            background = service._artifacts.load(run_id=current.run_id, start_authority_sha256=start,
                authority_checkpoint_sha256=old_plan.source_checkpoint_sha256,
                kind="target_af_background", subject_id="target-af").record
            extra = tuple(TargetAfRetainedRoiAuthority(sample, old_plan.sampling_policy, background, ())
                          for sample in bundle.samples[:len(old_plan.af_selections)])
        else:
            extra = tuple(item for item in old_plan.retained_authorities
                          if not item.extraction_authorities and item.source_preparation_sha256 is None)
        for item in extra:
            source = by_id.get(item.source_roi.input_evidence.input_id)
            if source is None or source.record.evidence != item.source_roi.input_evidence:
                raise ReferenceCheckpointConflictError("saved target-AF source authority changed")
            regions[(source.record.input_id, item.source_roi.selection.roi_id)] = item
    if len({item.source_roi.selection.roi_id for item in regions.values()}) != len(regions):
        raise ReferenceCheckpointConflictError("different AF sources reused one ROI identifier")
    return tuple(regions.values()), by_id, previous


def target_af_groups_inventory(service, current, start):
    """Read-only final decision inventory and its exact retained authorities."""
    from .reference_service import (_reference_region_label, _reference_wavelength_fields,
                                   _TARGET_AF_ESTIMATION_POLICY_VERSION)
    from .target_af import TargetAfEstimationPolicy

    retained, by_id, previous = retained_target_af_regions(service, current, start)
    if not retained:
        return None, retained, by_id
    target = service._require_canonical_target(current.run_id, start)
    old_reviews = {} if previous is None else {row.roi_id: row for row in previous[0].roi_reviews}
    old_sources = {} if previous is None else {row.input_id: row for row in previous[0].source_reviews}
    if previous is not None and not old_sources:
        for binding in previous[1].control_bindings:
            old_sources[binding.control_input.input_id] = TargetAfSourceReviewSelection(
                binding.control_input.input_id, "accepted", "accepted")
    compatibility = current.target_control_compatibility
    if compatibility is not None and compatibility.accepted and (
            compatibility.target_image_sha256 == target.evidence.source_image_sha256):
        for source_id, stored in by_id.items():
            if (stored.content_sha256 == compatibility.control_artifact.artifact_id
                    and stored.record.evidence.source_image_sha256 == compatibility.control_artifact.artifact_sha256):
                old_sources.setdefault(source_id, TargetAfSourceReviewSelection(source_id, "accepted", "accepted"))
    policy = (previous[0].estimation_policy if previous is not None else TargetAfEstimationPolicy(
        _TARGET_AF_ESTIMATION_POLICY_VERSION, 1e-8,
        typed_checksum_sha256({"semantic": "retained_target_af_display_policy_v1", "minimum_l2_norm": 1e-8})))
    sources, regions = [], []
    for item in retained:
        sample = item.source_roi
        source_id = sample.input_evidence.input_id
        source = by_id[source_id].record
        if not any(row["input_id"] == source_id for row in sources):
            structural = compare_structural_compatibility(reference_axis=source.evidence.axis,
                candidate_axis=target.evidence.axis, reference_signature=source.evidence.structural_signature,
                candidate_signature=target.evidence.structural_signature)
            quantitative = compare_quantitative_transformability(reference_signature=source.evidence.quantitative_signature,
                candidate_signature=target.evidence.quantitative_signature)
            reasons = []
            if source.evidence.axis != target.evidence.axis:
                reasons.append("Acquired measurement axes differ.")
            if not structural.compatible:
                reasons.append("Recorded acquisition settings differ.")
            if not quantitative.transformable:
                reasons.append("Quantitative correction is incompatible.")
            disposition = old_sources.get(source_id, TargetAfSourceReviewSelection(source_id, "unreviewed", "unreviewed"))
            sources.append({"input_id": source_id, "label": sample.selection.entry.image.source_path.name,
                "filename": sample.selection.entry.image.source_path.name, "origin_role": source.input_role,
                "fluorophores": list(dict.fromkeys(authority.fluorophore for region in retained
                    if region.source_roi.input_evidence.input_id == source_id for authority in region.extraction_authorities)),
                "acquisition_compatible": not reasons, "incompatibility_reasons": reasons,
                "preparation_disposition": disposition.preparation_disposition,
                "tissue_context_disposition": disposition.tissue_context_disposition})
        roi = TargetAfRoiProposal(sample, "AF comparison")
        candidate = TargetAfEndmemberCandidate(ReviewedTargetAfRoiSelection(roi,
            ProposalReviewDecision(roi.proposal_sha256, "accepted", "display-only", "2000-01-01T00:00:00Z", 1)),
            item.background, policy)
        signed = candidate.signed_background_difference
        review = old_reviews.get(sample.selection.roi_id)
        regions.append({"roi_id": sample.selection.roi_id, "input_id": source_id,
            "label": _reference_region_label(sample, "Control" if sample.selection.roi.roi_role == "negative" else "Autofluorescence"),
            "filename": sample.selection.entry.image.source_path.name, "origin_role": sample.selection.roi.roi_role,
            "signed_values": list(signed), "corrected_values": list(candidate.corrected_values),
            "values": list(candidate.values or ()), "accepted_pixel_count": sample.sampling.accepted_vector_count,
            "corrected_l2": math.hypot(*candidate.corrected_values), "clipped_count": sum(value < 0 for value in signed),
            "clipped_l2": math.hypot(*(min(value, 0.) for value in signed)), "usable": candidate.disposition == "pass",
            "included": review.included if review is not None else False,
            "component_identity": review.component_identity if review is not None else ""})
    # A lone retained region needs only its target suitability decision; it has
    # no unresolved grouping choice. Multiple observations always start unassigned.
    if previous is None and len(regions) == 1 and regions[0]["usable"]:
        regions[0].update(included=True, component_identity="AF")
    extraction_sets = []
    from .reference_service_control_preparation import accepted_preparations, _source_set_id
    preparations = accepted_preparations(service, current, start,
        canonical_inputs=tuple(item.record for item in by_id.values()))
    offered_roi_ids = {item["roi_id"] for item in regions}
    prepared_inputs, offered_groups = set(), set()
    for prepared in preparations:
        input_id = prepared.record.plan.input_id
        prepared_inputs.add(input_id)
        filename = next(row["filename"] for row in sources if row["input_id"] == input_id)
        for index, ids in enumerate(prepared.record.control_sets, 1):
            if set(ids).issubset(offered_roi_ids):
                extraction_sets.append({"label": f"AF set {index} — {filename}",
                    "set_id": _source_set_id(prepared, ids), "input_id": input_id,
                    "control_filename": filename, "roi_ids": list(ids)})
                offered_groups.add(frozenset(ids))
    # Older extraction plans have no independent preparation. Retain their
    # groups once per actual control membership, even when several stains reuse them.
    fluorophores = tuple(dict.fromkeys(authority.fluorophore for item in retained
        for authority in item.extraction_authorities))
    region_sources = {row["roi_id"]: row["input_id"] for row in regions}
    source_counts = {}
    for fluor in fluorophores:
        from .reference_workflow import _uses_source_preparation
        phase = ("negative_reference_rois" if current.control_strategy == "per_stain_negative_controls"
                 and not _uses_source_preparation(current.active_decisions) else "af_reference_rois")
        plan = service._live_roi_phase_artifact(current, start, "roi_selection_plan", fluor, phase).record
        by_positive = {}
        for pairing in plan.pairings:
            by_positive.setdefault(pairing.positive_roi_id, []).append(pairing.control_roi_id)
        groups = tuple(dict.fromkeys(tuple(ids) for ids in by_positive.values()))
        for ids in groups:
            if not set(ids).issubset(offered_roi_ids) or frozenset(ids) in offered_groups:
                continue
            input_ids = tuple(dict.fromkeys(region_sources[roi_id] for roi_id in ids))
            if all(input_id in prepared_inputs for input_id in input_ids):
                continue
            source_counts[input_ids] = source_counts.get(input_ids, 0) + 1
            filenames = [row["filename"] for row in sources if row["input_id"] in input_ids]
            extraction_sets.append({"label": f"AF set {source_counts[input_ids]} — {', '.join(filenames)}",
                                    "fluorophore": fluor, "roi_ids": list(ids)})
            offered_groups.add(frozenset(ids))
    inventory = {"measurement_names": list(target.evidence.axis.parent_band_ids),
        "measurement_ids": list(current.measurement_ids),
        **{key: list(value) if type(value) is tuple else value
           for key, value in _reference_wavelength_fields(target.evidence.axis)},
        "fluorophore_identities": list(current.fluorophores), "minimum_l2_norm": policy.minimum_l2_norm,
        "sources": sources, "regions": regions, "extraction_sets": extraction_sets}
    selection = {"sources": [{key: row[key] for key in ("input_id", "preparation_disposition", "tissue_context_disposition")}
                              for row in sources],
                 "regions": [{key: row[key] for key in ("roi_id", "included", "component_identity")} for row in regions]}
    return project_target_af_groups_selection(inventory, selection), retained, by_id


def propose_target_af_groups(service, run_id, expected, *, selection, reviewer_id):
    """Apply reviewed IDs, then enter the existing combined spectral review."""
    from .interactions import ArtifactPointer, TargetControlCompatibilityEvidence, configured_response
    from .reference_models import VersionRecord
    from .reference_service import (ReferenceServiceAdvance, ReferenceServiceAdvanceStatus,
        ReferenceServiceSnapshot, _derived_service_timestamp, _TARGET_CONTROL_REVIEW_POLICY_ID,
        _TARGET_CONTROL_REVIEW_POLICY_VERSION, _TARGET_AF_ESTIMATION_POLICY_VERSION,
        _TARGET_AF_QC_POLICY_VERSION, _fixed_basis_qc_policy_from_mapping)
    from .reference_service_target_af import TargetAfSamplingPlan, TargetAfSampleBundle
    from .reference_workflow import (bind_target_control_compatibility, advance_reference_workflow,
                                     propose_reference_artifacts)
    from .target_af import (TargetAfEstimationPolicy, validate_target_control_binding,
        estimate_target_af_candidates, evaluate_target_af_qc, propose_target_autofluorescence_model)
    from .reference_view_builders import build_target_af_review_data

    if type(reviewer_id) is not str or not reviewer_id or reviewer_id != reviewer_id.strip():
        raise ValueError("target-AF reviewer ID must be nonempty literal text")
    current, start = service._store._load_service_checkpoint(run_id)
    if (current.request is None or current.request.checkpoint.value not in {"target_af", "target_af_control_review"}
            or current.accepted_library is None):
        raise ValueError("AF grouping requires a live final library review")
    retrying = current.content_sha256 != expected
    if retrying and not any(item.source_checkpoint_sha256 == expected for item in current.transition_log):
        raise ReferenceCheckpointConflictError("AF grouping checkpoint is stale")
    inventory, retained, by_id = target_af_groups_inventory(service, current, start)
    if inventory is None:
        raise ValueError("No accepted control measurements are available for final AF comparison")
    projected = project_target_af_groups_selection(inventory, selection)
    if not projected["can_apply"]:
        raise ValueError(" ".join(projected["issues"]))
    source_reviews, roi_reviews = _selection(inventory, selection)
    target = service._require_canonical_target(run_id, start)
    library = service._accepted_final_library(current, start)
    source_ids = tuple(dict.fromkeys(item.source_roi.input_evidence.input_id for item, review in
        zip(retained, roi_reviews, strict=True) if review.included))
    # Verify durable source observations without reopening readers or resampling.
    target.validate_source_location()
    for source_id in source_ids:
        by_id[source_id].record.validate_source_location()
    for item, review in zip(retained, roi_reviews, strict=True):
        if review.included:
            for side in (item.background.stained_side, item.background.control_side):
                if side is not None:
                    for sampled in side.source_rois:
                        stored = by_id.get(sampled.input_evidence.input_id)
                        if stored is None or stored.record.evidence != sampled.input_evidence:
                            raise ReferenceCheckpointConflictError("retained AF background source changed")
                        stored.record.validate_source_location()
    reviews_by_id = {item.input_id: item for item in source_reviews}
    bindings = []
    for source_id in source_ids:
        stored = by_id[source_id]
        source = stored.record
        role = {"unstained_control": "unstained_reference_image",
                "target_unstained_control": "target_unstained_control_image",
                "single_stain_negative_control": "negative_reference_image"}[source.input_role]
        pointer = (current.available_reference_control if role == "unstained_reference_image" else
                   ArtifactPointer(role, stored.content_sha256, source.evidence.source_image_sha256,
                       source.evidence.fluorophore_identity if role == "negative_reference_image" else None))
        if pointer is None or pointer.artifact_sha256 != source.evidence.source_image_sha256:
            raise ReferenceCheckpointConflictError("selected control pointer differs from accepted source")
        structural = compare_structural_compatibility(reference_axis=source.evidence.axis,
            candidate_axis=target.evidence.axis, reference_signature=source.evidence.structural_signature,
            candidate_signature=target.evidence.structural_signature)
        quantitative = compare_quantitative_transformability(reference_signature=source.evidence.quantitative_signature,
            candidate_signature=target.evidence.quantitative_signature)
        source_review = reviews_by_id[source_id]
        compatibility = TargetControlCompatibilityEvidence(pointer, target.evidence.source_image_sha256,
            source_review.preparation_disposition, source_review.tissue_context_disposition,
            reviewer_id, typed_checksum_sha256({"semantic": "retained_control_target_use_v1",
                "checkpoint": expected, "target_authority": target.authority_sha256,
                "source_authority": source.authority_sha256, "reviewer_id": reviewer_id,
                "preparation_disposition": source_review.preparation_disposition,
                "tissue_context_disposition": source_review.tissue_context_disposition}),
            "service-recomputed-structural-v1", structural.content_sha256, structural.compatible,
            "service-recomputed-quantitative-v1", quantitative.content_sha256, quantitative.transformable,
            reviewer_id, _TARGET_CONTROL_REVIEW_POLICY_ID, _TARGET_CONTROL_REVIEW_POLICY_VERSION)
        bindings.append(validate_target_control_binding(selection_kind=GROUPS_OPTION,
            target_input=target.evidence, control_input=source.evidence, compatibility=compatibility,
            structural_report=structural, quantitative_report=quantitative))
    first = next(item for item, review in zip(retained, roi_reviews, strict=True) if review.included)
    estimation_policy = TargetAfEstimationPolicy(_TARGET_AF_ESTIMATION_POLICY_VERSION,
        inventory["minimum_l2_norm"], typed_checksum_sha256({"semantic": "retained_target_af_display_policy_v1",
                                                         "minimum_l2_norm": inventory["minimum_l2_norm"]}))
    qc_policy = _fixed_basis_qc_policy_from_mapping(fixed_basis_review_defaults(),
        purpose="reference-unmixing-target-af-qc-policy/v1", policy_version=_TARGET_AF_QC_POLICY_VERSION)
    created_at = _derived_service_timestamp(expected, "control-target-af-proposal")
    artifact_id = "control-target-af-" + typed_checksum_sha256({"checkpoint": expected,
        "selection": selection, "reviewer_id": reviewer_id})[:24]
    versions = (VersionRecord("mif_pipeline", "reference-unmixing-operation/v1"),)
    policies = (VersionRecord("background", first.background.policy.policy_version),
                VersionRecord("target_af", estimation_policy.policy_version),
                VersionRecord("fixed_basis_qc", qc_policy.policy_version))
    plan = TargetAfSamplingPlan(expected, GROUPS_OPTION, source_ids[0], library.content_sha256,
        retained[0].source_roi.selection.export, tuple(item.source_roi.selection for item in retained), (),
        roi_reviews, first.sampling_policy, first.background.policy, estimation_policy, qc_policy,
        artifact_id, created_at, reviewer_id, versions, policies, (), source_ids[1:], retained, source_reviews)
    bundle = TargetAfSampleBundle(plan, tuple(item.source_roi for item in retained))
    selections = []
    for item, review in zip(retained, roi_reviews, strict=True):
        roi = TargetAfRoiProposal(item.source_roi, review.component_identity or "Unassigned AF region")
        selections.append(ReviewedTargetAfRoiSelection(roi, ProposalReviewDecision(roi.proposal_sha256,
            "accepted" if review.included else "rejected", reviewer_id, created_at, 1)))
    estimation = estimate_target_af_candidates(selections=tuple(selections), background=first.background,
        backgrounds=tuple(item.background for item in retained), policy=estimation_policy)
    qc = evaluate_target_af_qc(estimation.components, qc_policy)
    proposal = propose_target_autofluorescence_model(artifact_id=artifact_id, created_at=created_at,
        run_id=run_id, control_binding=bindings[0], additional_control_bindings=tuple(bindings[1:]),
        estimation=estimation, qc=qc, software_versions=versions, policy_versions=policies)
    # The complete plan includes exclusions and all live extraction lineages.
    def persist(kind, record, subject="target-af"):
        return service._artifacts.put_unique(run_id=run_id, start_authority_sha256=start,
            authority_checkpoint_sha256=expected, kind=kind, subject_id=subject, record=record)
    existing = service._artifact_at_authority_or_none(run_id=run_id, start_authority_sha256=start,
        authority_checkpoint_sha256=expected, kind="target_af_sampling_plan", subject_id="target-af")
    if existing is not None and existing.record != plan:
        raise ReferenceCheckpointConflictError("AF grouping differs from persisted selection authority")
    if retrying and existing is None:
        raise ReferenceCheckpointConflictError("AF grouping retry has no retained selection authority")
    persist("target_control_binding", bindings[0])
    for binding in bindings[1:]:
        persist("target_control_binding", binding, "target-af:" + binding.control_input.input_id)
    persist("target_af_sampling_plan", plan)
    persist("target_af_sample_bundle", bundle)
    persist("target_af_background", first.background)
    stored_proposal = persist("target_af_proposal", proposal)
    pointer = ArtifactPointer("proposed_target_af_qc", proposal.artifact_id, proposal.proposal_sha256)
    validated = service._result_operations._validated_artifact(pointer, "qc_evidence", stored_proposal.content_sha256)
    active = current
    compatibility = bindings[0].compatibility
    if active.request.checkpoint.value == "target_af":
        if active.target_control_compatibility != compatibility:
            bound = bind_target_control_compatibility(active, compatibility)
            active, _ = service._store._compare_and_swap_service(active.content_sha256, bound, start)
        response = configured_response(active.request, option_value=GROUPS_OPTION, reviewer_id=reviewer_id)
        advanced = advance_reference_workflow(active, response)
        active, _ = service._store._compare_and_swap_service(active.content_sha256, advanced, start)
    if (active.request is None or active.request.checkpoint.value != "target_af_control_review"
            or active.target_control_compatibility != compatibility):
        raise ReferenceCheckpointConflictError("AF grouping retry is outside its exact review phase")
    if active.request.proposed_artifacts:
        if active.request.proposed_artifacts != (validated,):
            raise ReferenceCheckpointConflictError("AF grouping cannot overwrite a pending proposal; reopen Edit AF groups")
    else:
        proposed = propose_reference_artifacts(active, (validated,))
        active, _ = service._store._compare_and_swap_service(active.content_sha256, proposed, start)
    review = build_target_af_review_data(active.request, proposal, workflow_checkpoint_sha256=active.content_sha256)
    return ReferenceServiceAdvance(ReferenceServiceAdvanceStatus.PERSISTED,
                                   ReferenceServiceSnapshot(active, start, review))


def validate_retained_target_af_proposal(service, current, start, proposal, *, source_locations=False):
    """Rebind grouped publication to every unchanged live extraction authority."""
    if proposal.control_binding.selection_kind != GROUPS_OPTION:
        return
    previous = _previous_plan(service, current, start)
    if previous is None or previous[1] != proposal:
        raise ReferenceCheckpointConflictError("grouped AF proposal is not the current displayed authority")
    plan = previous[0]
    retained, by_id, _ = retained_target_af_regions(service, current, start)
    if retained != plan.retained_authorities:
        raise ReferenceCheckpointConflictError("grouped AF sampling or background authority changed")
    if service._accepted_final_library(current, start).content_sha256 != plan.accepted_library_sha256:
        raise ReferenceCheckpointConflictError("grouped AF accepted library changed")
    for binding in proposal.control_bindings:
        stored = by_id.get(binding.control_input.input_id)
        if stored is None or stored.record.evidence != binding.control_input:
            raise ReferenceCheckpointConflictError("grouped AF control input authority changed")
        if source_locations:
            stored.record.validate_source_location()
    if source_locations:
        service._require_canonical_target(current.run_id, start).validate_source_location()
        included = {item.roi_id for item in plan.roi_reviews if item.included}
        for item in retained:
            if item.source_roi.selection.roi_id not in included:
                continue
            for side in (item.background.stained_side, item.background.control_side):
                if side is not None:
                    for sample in side.source_rois:
                        source = by_id.get(sample.input_evidence.input_id)
                        if source is None or source.record.evidence != sample.input_evidence:
                            raise ReferenceCheckpointConflictError("grouped AF background source authority changed")
                        source.record.validate_source_location()
