"""Accepted control-source evidence, independent of any stained reference.

The source plan and measured result are immutable. Stain plans reference the
accepted preparation; they neither own nor remeasure its pixels.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from .background import BackgroundEquivalencePolicy, SampledReferenceRoi, _pooled_side
from .purification import combine_af_regional_means
from .reference_rois import AcceptedReferenceRoiSelection, ReferenceRoiExport
from .reference_sampling import CompleteVectorQcPolicy
from .reference_service_roi_purification import (
    ReferenceRoiSelectionPlan, SampledReferenceRoiBundle, sample_reference_roi_selection_plan,
    _sha256, _text,
)
from .typed_checksum import typed_checksum_sha256


class _UnusableControlBackgroundError(ValueError):
    """Measured background cannot provide the required source correction."""


@dataclass(frozen=True, slots=True)
class SharedControlPreparationPlan:
    source_checkpoint_sha256: str
    input_id: str
    input_authority_sha256: str
    export: ReferenceRoiExport
    control_roi_ids: tuple[str, ...]
    background_roi_ids: tuple[str, ...]
    control_sets: tuple[tuple[str, ...], ...]
    control_sampling_policy: CompleteVectorQcPolicy
    background_sampling_policy: CompleteVectorQcPolicy
    background_policy: BackgroundEquivalencePolicy
    reviewer_id: str
    content_sha256: str = field(init=False)

    def __post_init__(self):
        _sha256(self.source_checkpoint_sha256, 'source preparation checkpoint')
        _sha256(self.input_authority_sha256, 'source input authority')
        _text(self.input_id, 'source input ID')
        _text(self.reviewer_id, 'source preparation reviewer')
        if type(self.export) is not ReferenceRoiExport:
            raise TypeError('Source preparation requires an accepted native region export')
        for role, identities in (('af', self.control_roi_ids), ('background', self.background_roi_ids)):
            if type(identities) is not tuple or not identities or len(set(identities)) != len(identities):
                raise ValueError('Choose distinct source AF and background regions')
            selected = tuple(AcceptedReferenceRoiSelection(self.export, identity) for identity in identities)
            if any(item.roi.roi_role != role or item.entry.image.reference_input_id != self.input_id
                   or item.roi.role_link_id != (item.entry.image.source_link_id if role == 'af' else self.input_id)
                   for item in selected):
                raise ValueError('Source preparation regions belong to a different source or role')
            if identities != tuple(roi.roi_id for roi in self.export.rois if roi.roi_id in identities):
                raise ValueError('Source regions must retain native export order')
        if (type(self.control_sets) is not tuple or not self.control_sets
                or any(type(group) is not tuple or not group or len(set(group)) != len(group)
                       or tuple(identity for identity in self.control_roi_ids if identity in group) != group
                       for group in self.control_sets)
                or len(set(self.control_sets)) != len(self.control_sets)
                or {identity for group in self.control_sets for identity in group} != set(self.control_roi_ids)):
            raise ValueError('AF sets must cover the selected source regions in native order')
        if (type(self.control_sampling_policy) is not CompleteVectorQcPolicy
                or type(self.background_sampling_policy) is not CompleteVectorQcPolicy
                or type(self.background_policy) is not BackgroundEquivalencePolicy):
            raise TypeError('Source preparation requires exact sampling and background policies')
        object.__setattr__(self, 'content_sha256', typed_checksum_sha256({
            'semantic': 'shared_control_preparation_plan_v1',
            **{name: getattr(self, name) for name in ('source_checkpoint_sha256', 'input_id',
                'input_authority_sha256', 'reviewer_id')},
            'export_sha256': self.export.content_sha256,
            'control_roi_ids': list(self.control_roi_ids), 'background_roi_ids': list(self.background_roi_ids),
            'control_sets': [list(group) for group in self.control_sets],
            'control_policy': _policy_values(self.control_sampling_policy),
            'background_sampling_policy': _policy_values(self.background_sampling_policy),
            'background_policy_sha256': self.background_policy.content_sha256,
        }))


def _policy_values(policy):
    return [policy.processing_chunk_shape.width, policy.processing_chunk_shape.height,
            policy.saturation_fraction, policy.near_zero_l2_threshold]


@dataclass(frozen=True, slots=True)
class SampledSharedControlPreparation:
    plan: SharedControlPreparationPlan
    controls: tuple[SampledReferenceRoi, ...]
    backgrounds: tuple[SampledReferenceRoi, ...]
    content_sha256: str = field(init=False)

    def __post_init__(self):
        if type(self.plan) is not SharedControlPreparationPlan:
            raise TypeError('Source sample result requires its exact preparation plan')
        for samples, identities in ((self.controls, self.plan.control_roi_ids),
                                    (self.backgrounds, self.plan.background_roi_ids)):
            if (type(samples) is not tuple or any(type(item) is not SampledReferenceRoi for item in samples)
                    or tuple(item.selection for item in samples) != tuple(
                        AcceptedReferenceRoiSelection(self.plan.export, identity) for identity in identities)
                    or any(item.input_evidence.input_id != self.plan.input_id for item in samples)):
                raise ValueError('Source samples differ from the exact prepared source selection')
        if len({item.input_evidence for item in self.controls + self.backgrounds}) != 1:
            raise ValueError('Source preparation mixes input evidence')
        object.__setattr__(self, 'content_sha256', typed_checksum_sha256({
            'semantic': 'sampled_shared_control_preparation_v1', 'plan': self.plan.content_sha256,
            'controls': [item.evidence_sha256 for item in self.controls],
            'backgrounds': [item.evidence_sha256 for item in self.backgrounds]}))

    @property
    def control_sets(self):
        return self.plan.control_sets

    def signed_regions(self):
        side = _pooled_side(self.controls[0], self.backgrounds, scale_floor=self.plan.background_policy.scale_floor)
        if side is None:
            raise _UnusableControlBackgroundError('Choose usable image background for this AF source')
        return {sample.selection.roi_id: tuple(float(value - offset) for value, offset in
            zip(sample.sampling.mean_vector, side.values, strict=True)) for sample in self.controls}

    def spectra(self):
        regions = self.signed_regions()
        return tuple(combine_af_regional_means(tuple(regions[identity] for identity in group))
                     for group in self.control_sets)


@dataclass(frozen=True, slots=True)
class ReferenceRoiSampleUse:
    """Only new stain-owned measurements are stored alongside a source reference."""
    plan: ReferenceRoiSelectionPlan
    source_preparation_sha256: str
    sampled_stain_rois: tuple[SampledReferenceRoi, ...]
    content_sha256: str = field(init=False)

    def __post_init__(self):
        _sha256(self.source_preparation_sha256, 'used source preparation')
        if (type(self.plan) is not ReferenceRoiSelectionPlan
                or self.plan.source_preparation_sha256 != self.source_preparation_sha256
                or type(self.sampled_stain_rois) is not tuple
                or any(type(sample) is not SampledReferenceRoi or sample.selection not in self.plan.selections
                       or sample.selection.entry.image.source_role != 'single_component_reference'
                       for sample in self.sampled_stain_rois)
                or len({sample.selection for sample in self.sampled_stain_rois}) != len(self.sampled_stain_rois)):
            raise ValueError('Stain sample use differs from its source preparation and selections')
        object.__setattr__(self, 'content_sha256', typed_checksum_sha256({
            'semantic': 'reference_roi_sample_use_v1', 'plan': self.plan.content_sha256,
            'source_preparation_sha256': self.source_preparation_sha256,
            'stain_samples': [sample.evidence_sha256 for sample in self.sampled_stain_rois]}))


@dataclass(frozen=True, slots=True)
class StainedReferenceRegionChoice:
    """Stain tissue and own background selected before any AF-set assignment."""
    positive_plan: ReferenceRoiSelectionPlan
    background_plan: ReferenceRoiSelectionPlan
    background_policy: BackgroundEquivalencePolicy
    content_sha256: str = field(init=False)

    def __post_init__(self):
        positive, background = self.positive_plan, self.background_plan
        if (type(positive) is not ReferenceRoiSelectionPlan or type(background) is not ReferenceRoiSelectionPlan
                or positive.checkpoint != 'positive_reference_rois' or background.checkpoint != 'background_reference_rois'
                or positive.source_preparation_sha256 is None
                or any(getattr(positive, name) != getattr(background, name) for name in
                    ('source_checkpoint_sha256', 'source_preparation_sha256', 'export', 'fluorophore', 'reviewer_id'))
                or positive.pairings or background.pairings
                or any(selection.roi.roi_role != role for plan, role in ((positive, 'positive'), (background, 'background'))
                       for selection in plan.selections)
                or len({selection.entry.image.reference_input_id for plan in (positive, background)
                        for selection in plan.selections}) != 1
                or type(self.background_policy) is not BackgroundEquivalencePolicy):
            raise ValueError('Stain selection must contain its own tissue and background under one AF authority')
        object.__setattr__(self, 'content_sha256', typed_checksum_sha256({
            'semantic': 'stained_reference_region_choice_v1', 'positive': positive.content_sha256,
            'background': background.content_sha256, 'policy': self.background_policy.content_sha256}))


def _stored(service, current, start, kind, subject, authority):
    return service._artifact_at_authority_or_none(run_id=current.run_id,
        start_authority_sha256=start, authority_checkpoint_sha256=authority,
        kind=kind, subject_id=subject)


def source_input_id(binding, current, *, fluorophore=None):
    if binding.shared_unstained_input_id is not None:
        return binding.shared_unstained_input_id
    fluorophore = current.current_fluorophore if fluorophore is None else fluorophore
    if fluorophore is None:
        return None
    return binding.negative_control_input_ids[current.fluorophores.index(fluorophore)]


def preparation_subject(current):
    return 'shared' if current.control_strategy == 'shared_unstained_control' else current.current_fluorophore


def source_revision_affects(decision, fluorophore):
    return (decision.response.option_value == 'revise_shared_control'
        and (fluorophore is None or decision.request.fluorophore is None or decision.request.fluorophore == fluorophore))


@dataclass(frozen=True, slots=True)
class ControlPreparationSetReview:
    """A small grouping decision pointing to the original, unchanged source samples."""
    source_checkpoint_sha256: str
    input_id: str
    sampled_authority_sha256: str
    sampled_subject_id: str
    sampled_preparation_sha256: str
    control_sets: tuple[tuple[str, ...], ...]
    reviewer_id: str
    content_sha256: str = field(init=False)

    def __post_init__(self):
        for name in ('source_checkpoint_sha256', 'sampled_authority_sha256', 'sampled_preparation_sha256'):
            _sha256(getattr(self, name), name)
        for name in ('input_id', 'sampled_subject_id', 'reviewer_id'):
            _text(getattr(self, name), name)
        _validate_control_sets(self.control_sets)
        object.__setattr__(self, 'content_sha256', typed_checksum_sha256({
            'semantic': 'control_preparation_set_review_v1',
            **{name: getattr(self, name) for name in ('source_checkpoint_sha256', 'input_id',
                'sampled_authority_sha256', 'sampled_subject_id', 'sampled_preparation_sha256', 'reviewer_id')},
            'control_sets': [list(group) for group in self.control_sets]}))


def _validate_control_sets(groups, identities=None):
    if (type(groups) is not tuple or not groups or any(type(group) is not tuple or not group
            or any(type(identity) is not str or not identity for identity in group) for group in groups)):
        raise ValueError('Choose at least one nonempty AF set')
    members = tuple(identity for group in groups for identity in group)
    if len(members) != len(set(members)):
        raise ValueError('Each source region can belong to only one AF set')
    if identities is not None and any(tuple(identity for identity in identities if identity in group) != group
                                     for group in groups):
        raise ValueError('AF sets must use sampled source regions in native order')


@dataclass(frozen=True, slots=True)
class ReviewedControlPreparation:
    sampled: SampledSharedControlPreparation
    review: ControlPreparationSetReview

    @property
    def plan(self):
        return self.sampled.plan

    @property
    def controls(self):
        return self.sampled.controls

    @property
    def backgrounds(self):
        return self.sampled.backgrounds

    @property
    def control_sets(self):
        return self.review.control_sets

    @property
    def content_sha256(self):
        return self.review.content_sha256

    def signed_regions(self):
        return self.sampled.signed_regions()

    def spectra(self):
        regions = self.signed_regions()
        return tuple(combine_af_regional_means(tuple(regions[identity] for identity in group))
                     for group in self.control_sets)


def _restored_source_proposal_authority(current, pointer, authority):
    """Keep the original samples behind a sibling's restored source proposal."""
    import json
    from .reference_workflow import ReferenceTransitionKind
    # Only an exact proposal carried across an unrelated component publication
    # can retain its earlier sampling authority. Ordinary revisions still use
    # their own authority and cannot resurrect a superseded source proposal.
    while True:
        matches = [(index, item) for index, item in enumerate(current.transition_log)
            if item.kind is ReferenceTransitionKind.PUBLICATION and item.source_checkpoint_sha256 == authority]
        if len(matches) != 1:
            return authority
        index, publication = matches[0]
        target = json.loads(publication.target_state_json)
        if (target['checkpoint'] != 'shared_control_preparation'
                or pointer.to_mapping() not in [item['pointer'] for item in target['proposed_artifacts']]):
            return authority
        for previous in reversed(current.transition_log[:index]):
            if previous.affected_fluorophore != publication.affected_fluorophore:
                continue
            if previous.kind is ReferenceTransitionKind.PUBLICATION:
                return authority
            if previous.kind is ReferenceTransitionKind.PREPARATION_REVISION:
                origin = json.loads(previous.source_state_json)
                if (origin['checkpoint'] != 'shared_control_preparation'
                        or origin['current_fluorophore'] != target['current_fluorophore']
                        or origin['current_fluorophore'] == publication.affected_fluorophore
                        or origin['proposed_artifacts'] != target['proposed_artifacts']
                        or origin['prior_checkpoint_sha256'] is None):
                    return authority
                authority = origin['prior_checkpoint_sha256']
                break
        else:
            return authority


def _resolve_preparation(service, current, start, pointer, authority, input_id):
    from .reference_service_storage import ReferenceCheckpointConflictError
    from .reference_service_artifacts import ReferenceArtifactRepositoryError
    try:
        reviews = service._artifacts.load_all(run_id=current.run_id, start_authority_sha256=start,
            kind='control_preparation_set_review', subject_id=input_id)
    except ReferenceArtifactRepositoryError as exc:
        if str(exc) != 'reference scientific artifact is missing':
            raise
        reviews = ()
    matching = tuple(item for item in reviews if item.content_sha256 == pointer.artifact_id)
    if matching:
        if len(matching) != 1:
            raise ReferenceCheckpointConflictError('Source grouping authority is ambiguous')
        reviewed = matching[0]
        review = reviewed.record
        sampled = service._artifacts.load(run_id=current.run_id, start_authority_sha256=start,
            authority_checkpoint_sha256=review.sampled_authority_sha256,
            kind='shared_control_preparation', subject_id=review.sampled_subject_id)
        if (sampled.content_sha256 != review.sampled_preparation_sha256
                or review.input_id != input_id or sampled.record.plan.input_id != input_id):
            raise ReferenceCheckpointConflictError('Source grouping refers to different samples')
        _validate_control_sets(review.control_sets, sampled.record.plan.control_roi_ids)
        result = replace(reviewed, record=ReviewedControlPreparation(sampled.record, review))
    else:
        authority = _restored_source_proposal_authority(current, pointer, authority)
        result = _stored(service, current, start, 'shared_control_preparation', input_id, authority)
        if result is None and current.control_strategy == 'shared_unstained_control':
            result = _stored(service, current, start, 'shared_control_preparation', 'shared', authority)
        if result is None:
            raise ReferenceCheckpointConflictError('Source samples are missing')
    if pointer.artifact_id != result.content_sha256 or pointer.artifact_sha256 != result.record.content_sha256:
        raise ReferenceCheckpointConflictError('Source acceptance pointer differs')
    return result


def accepted_preparation(service, current, start, *, canonical_inputs=None, fluorophore=None, input_id=None):
    """Resolve the live accepted source for one explicit input or stained component."""
    from .reference_service_storage import ReferenceCheckpointConflictError
    if not any(item.response.option_value == 'accept_reference_control_files_with_source_preparation'
               for item in current.active_decisions):
        return None
    binding = service._one_artifact(current.run_id, start, 'reference_control_files', 'reference-control-files').record
    if not binding.prepare_shared_control:
        return None
    if input_id is None:
        input_id = source_input_id(binding, current, fluorophore=fluorophore)
    if input_id is None:
        return None
    expected_subject = ('shared' if binding.shared_unstained_input_id is not None else
        current.fluorophores[binding.negative_control_input_ids.index(input_id)])
    decisions = current.active_decisions
    for index in range(len(decisions) - 1, -1, -1):
        if source_revision_affects(decisions[index], None if expected_subject == 'shared' else expected_subject):
            decisions = decisions[index + 1:]
            break
    choices = tuple(item for item in decisions if item.response.option_value == 'accept_shared_control_preparation'
        and item.response.artifacts[0].subject_id == expected_subject)
    if not choices:
        return None
    if len(choices) != 1:
        raise ReferenceCheckpointConflictError('Source acceptance is ambiguous')
    decision = choices[0]
    source, result, loaded_start = service._store._load_service_decision_authority(current.run_id,
        source_checkpoint_sha256=decision.source_checkpoint_sha256,
        decision_record_sha256=decision.content_sha256)
    if (loaded_start != start or source.request != decision.request or result.decisions[-1] != decision
            or len(decision.response.artifacts) != 1 or source.prior_checkpoint_sha256 is None):
        raise ReferenceCheckpointConflictError('Source acceptance lineage differs')
    stored = _resolve_preparation(service, current, start, decision.response.artifacts[0],
                                  source.prior_checkpoint_sha256, input_id)
    if canonical_inputs is None:
        canonical_inputs = service._canonical_inventory(current.run_id, start)
    record = next((item for item in canonical_inputs if item.input_id == input_id), None)
    if record is None or record.authority_sha256 != stored.record.plan.input_authority_sha256:
        raise ReferenceCheckpointConflictError('Source input authority changed')
    return stored


def accepted_preparations(service, current, start, *, canonical_inputs=None):
    if not any(item.response.option_value == 'accept_reference_control_files_with_source_preparation'
               for item in current.active_decisions):
        return ()
    binding = service._one_artifact(current.run_id, start, 'reference_control_files', 'reference-control-files').record
    if not binding.prepare_shared_control:
        return ()
    if canonical_inputs is None:
        canonical_inputs = service._canonical_inventory(current.run_id, start)
    identities = ((binding.shared_unstained_input_id,) if binding.shared_unstained_input_id is not None
                  else binding.negative_control_input_ids)
    return tuple(source for identity in identities if (source := accepted_preparation(service, current, start,
        canonical_inputs=canonical_inputs, input_id=identity)) is not None)


def prepare_shared_control(service, run_id, token, *, roi_export, control_roi_ids,
                           background_roi_ids, control_sets, control_sampling_policy,
                           background_sampling_policy, background_policy, reviewer_id):
    from .reference_service import _purification_sampling_policy_from_mapping, _background_policy_from_mapping
    from .reference_service_storage import ReferenceCheckpointConflictError
    from .reference_service_records import ReferenceServiceAdvance, ReferenceServiceAdvanceStatus, ReferenceServiceSnapshot
    from .reference_workflow import propose_reference_artifacts
    from .interactions import ArtifactPointer

    current, start = service._store._load_service_checkpoint(run_id)
    if current.request is None or current.request.checkpoint.value != 'shared_control_preparation':
        raise ValueError('Open the current AF preparation before sampling')
    authority = current.prior_checkpoint_sha256 if current.request.proposed_artifacts else current.content_sha256
    if token not in {current.content_sha256, authority}:
        raise ReferenceCheckpointConflictError('AF preparation submission is stale')
    binding = service._one_artifact(run_id, start, 'reference_control_files', 'reference-control-files').record
    source = next(item for item in service._canonical_inventory(run_id, start)
                  if item.input_id == source_input_id(binding, current))
    policy = lambda value: value if type(value) is CompleteVectorQcPolicy else _purification_sampling_policy_from_mapping(value)
    bg_policy = (background_policy if type(background_policy) is BackgroundEquivalencePolicy
                 else _background_policy_from_mapping(background_policy))
    plan = SharedControlPreparationPlan(authority, source.input_id, source.authority_sha256,
        roi_export, control_roi_ids, background_roi_ids, control_sets,
        policy(control_sampling_policy), policy(background_sampling_policy), bg_policy, reviewer_id)
    saved = _stored(service, current, start, 'shared_control_preparation_plan', source.input_id, authority)
    if saved is not None and saved.record != plan:
        raise ReferenceCheckpointConflictError('AF preparation retry choices differ')
    if saved is None:
        service._artifacts.put_unique(run_id=run_id, start_authority_sha256=start,
            authority_checkpoint_sha256=authority, kind='shared_control_preparation_plan', subject_id=source.input_id, record=plan)
    sampled = _stored(service, current, start, 'shared_control_preparation', source.input_id, authority)
    if sampled is None:
        from mif_pipeline.io import resolve_source_image_facts
        from .reference_sampling import MeasurementPlane, sample_reference_roi
        from .reference_service_roi_purification import _revalidate_live_target
        backend = service._sampling_backend()
        target = service._require_canonical_target(run_id, start)
        _revalidate_live_target(backend, target)
        groups = []
        with source.open_source(backend, compatibility_target=target) as image:
            facts = resolve_source_image_facts(image)
            planes = tuple(MeasurementPlane(item.measurement_id, item.channel_id, item.channel_index)
                           for item in source.measurement_planes)
            for identities, sampling_policy in ((control_roi_ids, plan.control_sampling_policy),
                                                 (background_roi_ids, plan.background_sampling_policy)):
                group = []
                for identity in identities:
                    selection = AcceptedReferenceRoiSelection(roi_export, identity)
                    sample = sample_reference_roi(image, facts, source.evidence, planes, selection, sampling_policy)
                    group.append(SampledReferenceRoi(selection, source.evidence, sample))
                groups.append(tuple(group))
        _revalidate_live_target(backend, target)
        result = SampledSharedControlPreparation(plan, *groups)
        sampled = service._artifacts.put_unique(run_id=run_id, start_authority_sha256=start,
            authority_checkpoint_sha256=authority, kind='shared_control_preparation', subject_id=source.input_id, record=result)
    if sampled.record.plan != plan:
        raise ReferenceCheckpointConflictError('AF measured evidence differs from its plan')
    pointer = ArtifactPointer('shared_control_preparation', sampled.content_sha256, sampled.record.content_sha256, preparation_subject(current))
    if current.request.proposed_artifacts:
        if tuple(row.pointer for row in current.request.proposed_artifacts) != (pointer,):
            raise ReferenceCheckpointConflictError('AF proposal differs from retained measurements')
        return ReferenceServiceAdvance(ReferenceServiceAdvanceStatus.PERSISTED, ReferenceServiceSnapshot(current, start))
    proposed = propose_reference_artifacts(current,
        (service._result_operations._validated_artifact(pointer, 'qupath_roi_geometry', sampled.content_sha256),))
    persisted, persisted_start = service._store._compare_and_swap_service(current.content_sha256, proposed, start)
    return ReferenceServiceAdvance(ReferenceServiceAdvanceStatus.PERSISTED, ReferenceServiceSnapshot(persisted, persisted_start))


def accept_shared_control(service, run_id, token, *, reviewer_id):
    from .reference_service_storage import ReferenceCheckpointConflictError
    from .reference_service_records import ReferenceServiceAdvance, ReferenceServiceAdvanceStatus, ReferenceServiceSnapshot
    from .reference_workflow import advance_reference_workflow
    from .interactions import configured_response
    current, start = service._store._load_service_checkpoint(run_id)
    if current.content_sha256 != token:
        source = accepted_preparation(service, current, start)
        if source is not None and any(decision.source_checkpoint_sha256 == token
                and decision.response.option_value == 'accept_shared_control_preparation'
                and decision.response.reviewer_id == reviewer_id
                and len(decision.response.artifacts) == 1
                and decision.response.artifacts[0].artifact_id == source.content_sha256
                for decision in current.active_decisions):
            return ReferenceServiceAdvance(ReferenceServiceAdvanceStatus.PERSISTED, ReferenceServiceSnapshot(current, start))
        raise ReferenceCheckpointConflictError('The AF preparation review is stale')
    if (current.request is None or current.request.checkpoint.value != 'shared_control_preparation'
            or len(current.request.proposed_artifacts) != 1):
        raise ValueError('Review the measured AF preparation before accepting it')
    binding = service._one_artifact(run_id, start, 'reference_control_files', 'reference-control-files').record
    pointer = current.request.proposed_artifacts[0].pointer
    stored = _resolve_preparation(service, current, start, pointer, current.prior_checkpoint_sha256,
                                  source_input_id(binding, current))
    stored.record.spectra()
    response = configured_response(current.request, option_value='accept_shared_control_preparation',
        reviewer_id=reviewer_id, artifacts=(pointer,))
    candidate = advance_reference_workflow(current, response)
    persisted, persisted_start = service._store._compare_and_swap_service(token, candidate, start)
    return ReferenceServiceAdvance(ReferenceServiceAdvanceStatus.PERSISTED, ReferenceServiceSnapshot(persisted, persisted_start))


def load_sample_use(service, current, start, authority, subject, *, plan=None, accepted_source=None):
    """Resolve a use record, reusing source authority already validated in this projection."""
    from .reference_service_storage import ReferenceCheckpointConflictError
    stored = _stored(service, current, start, 'roi_sample_use', subject, authority)
    if stored is None:
        return None
    use = stored.record
    if plan is not None and use.plan != plan:
        raise ReferenceCheckpointConflictError('Stain sample-use plan differs')
    source = accepted_source if accepted_source is not None else accepted_preparation(service, current, start, fluorophore=use.plan.fluorophore)
    if source is None or source.content_sha256 != use.source_preparation_sha256:
        raise ReferenceCheckpointConflictError('Stain samples refer to a superseded AF preparation')
    by_selection = {sample.selection: sample for sample in
                    source.record.controls + source.record.backgrounds + use.sampled_stain_rois}
    if any(selection not in by_selection for selection in use.plan.selections):
        raise ReferenceCheckpointConflictError('Stain sample use contains unprepared source regions')
    bundle = SampledReferenceRoiBundle(use.plan, tuple(by_selection[selection] for selection in use.plan.selections))
    return replace(stored, kind='sampled_roi_bundle', record=bundle)


def persist_sample_use(service, current, start, plan):
    from .reference_service_storage import ReferenceCheckpointConflictError
    source = accepted_preparation(service, current, start, fluorophore=plan.fluorophore)
    if source is None or source.content_sha256 != plan.source_preparation_sha256:
        raise ReferenceCheckpointConflictError('Stain choices refer to a superseded AF preparation')
    subject = f'{plan.fluorophore}:{plan.checkpoint}'
    retained = load_sample_use(service, current, start, current.content_sha256, subject, plan=plan)
    if retained is not None:
        return retained
    source_samples = {sample.selection: sample for sample in source.record.controls + source.record.backgrounds}
    stain_selections = []
    for selection in plan.selections:
        if selection.entry.image.reference_input_id == source.record.plan.input_id:
            if selection not in source_samples:
                raise ReferenceCheckpointConflictError('Only regions from the accepted source preparation can be assigned')
            expected = (source.record.plan.control_sampling_policy if selection.roi.roi_role == 'af'
                        else source.record.plan.background_sampling_policy)
            if plan.sampling_policy != expected:
                raise ReferenceCheckpointConflictError('AF sampling settings require a source preparation revision')
        else:
            stain_selections.append(selection)
    samples = ()
    if stain_selections:
        local = replace(plan, selections=tuple(stain_selections), source_preparation_sha256=None)
        samples = sample_reference_roi_selection_plan(service._sampling_backend(),
            service._canonical_inventory(current.run_id, start), local).samples
    use = ReferenceRoiSampleUse(plan, source.content_sha256, samples)
    service._artifacts.put_unique(run_id=current.run_id, start_authority_sha256=start,
        authority_checkpoint_sha256=current.content_sha256, kind='roi_sample_use', subject_id=subject, record=use)
    return load_sample_use(service, current, start, current.content_sha256, subject, plan=plan)


def _sample_authority(source):
    if type(source.record) is ReviewedControlPreparation:
        review = source.record.review
        return review.sampled_preparation_sha256, review.sampled_authority_sha256, review.sampled_subject_id
    return source.content_sha256, source.authority_checkpoint_sha256, source.subject_id


def _source_set_id(source, members):
    sample_sha, _, _ = _sample_authority(source)
    return typed_checksum_sha256({'sampled_preparation_sha256': sample_sha, 'roi_ids': list(members)})


def project_control_preparation_sets(summary, control_sets):
    """Pure preview of actual retained regional means; never opens source pixels."""
    import math
    groups = tuple(tuple(group) for group in control_sets)
    _validate_control_sets(groups, tuple(summary['control_roi_ids']))
    by_roi = {item['roi_id']: item for item in summary['regions']}
    previous = {tuple(item['roi_ids']): item for item in summary.get('sets', ())}
    sets = []
    for index, members in enumerate(groups):
        error, combined = None, None
        try:
            if any(by_roi[identity]['signed_values'] is None for identity in members):
                raise _UnusableControlBackgroundError(summary['diagnostic'])
            combined = combine_af_regional_means(tuple(tuple(by_roi[identity]['signed_values']) for identity in members))
        except ValueError as exc:
            error = str(exc)
        prior = previous.get(members, {})
        identity = prior.get('set_id') or typed_checksum_sha256({
            'sampled_preparation_sha256': summary['sampled_preparation_sha256'], 'roi_ids': list(members)})
        label = f'AF set {index + 1}'
        regions = []
        for roi_id in members:
            region = by_roi[roi_id]
            angle = None
            if combined is not None and region['values'] is not None:
                angle = math.degrees(math.acos(max(-1., min(1., math.fsum(
                    a * b for a, b in zip(region['values'], combined.normalized_shape, strict=True))))))
            regions.append({**region, 'included': True, 'angle_to_combined_degrees': angle,
                            'clipped_count': None if region['signed_values'] is None else
                                sum(value < 0. for value in region['signed_values'])})
        sets.append({'set_id': identity, 'label': label, 'positive_label': label,
            'control_filename': summary['control_filename'], 'roi_ids': list(members),
            'signed_values': None if combined is None else list(combined.signed_mean),
            'corrected_values': None if combined is None else list(combined.corrected_mean),
            'values': None if combined is None else list(combined.normalized_shape),
            'roi_weights': [1. / len(members)] * len(members),
            'regions': regions,
            'status': 'failed' if error else 'pass', 'diagnostic': error})
    included = {identity for members in groups for identity in members}
    return {**summary, 'sets': sets,
        'regions': [{**row, 'included': row['roi_id'] in included} for row in summary['regions']],
        'can_accept': bool(summary.get('proposal_available')) and all(item['status'] == 'pass' for item in sets)}


def control_source_row(binding, record, source, dependents, *, accepted=False,
                       pending=False, current=False, can_revise=False):
    """Describe one canonically bound control without loading its regional arrays."""
    from pathlib import Path
    return {'input_id': record.input_id, 'label': Path(record.locator_binding.source_locator).name,
        'input_role': record.input_role,
        'source_role': 'negative_control' if record.input_role == 'single_stain_negative_control' else 'unstained_control',
        'source_link_id': record.evidence.fluorophore_identity or 'shared',
        'control_roi_role': 'af' if binding.prepare_shared_control or binding.shared_unstained_input_id else 'negative',
        'preparation_mode': 'independent' if binding.prepare_shared_control else 'per_fluorophore',
        'preparation_sha256': None if source is None else source.content_sha256,
        'sampled_preparation_sha256': None if source is None else _sample_authority(source)[0],
        'accepted': accepted, 'proposal_available': pending, 'current': current,
        'can_open': current, 'can_revise': can_revise,
        'dependent_fluorophores': list(dependents), 'entry_page': 'control',
        'combined_region_draft': not binding.prepare_shared_control}


def project_control_preparation(source, row):
    """Project exactly one validated saved source and its retained AF grouping."""
    import math
    from .reference_service import _reference_wavelength_fields
    sampled = source.record
    diagnostic = None
    try:
        signed = sampled.signed_regions()
    except _UnusableControlBackgroundError as exc:
        signed, diagnostic = {}, str(exc)
    regions = []
    for index, sample in enumerate(sampled.controls):
        values = signed.get(sample.selection.roi_id)
        corrected = None if values is None else tuple(max(0., value) for value in values)
        norm = None if corrected is None else math.hypot(*corrected)
        regions.append({'roi_id': sample.selection.roi_id, 'label': f'Autofluorescence region {index + 1}',
            'control_filename': row['label'], 'signed_values': None if values is None else list(values),
            'corrected_values': None if corrected is None else list(corrected),
            'values': None if not norm else [value / norm for value in corrected],
            'accepted_pixel_count': sample.sampling.accepted_vector_count, 'corrected_l2': norm,
            'clipped_count': None if values is None else sum(value < 0. for value in values),
            'status': 'pass' if norm else 'failed', 'angle_to_combined_degrees': None})
    axis = sampled.controls[0].input_evidence.axis
    initial_sets = [{'roi_ids': list(group), 'set_id': _source_set_id(source, group), 'label': f'AF set {index + 1}'}
                    for index, group in enumerate(sampled.control_sets)]
    summary = {**row, 'control_roi_ids': list(sampled.plan.control_roi_ids),
        'control_filename': row['label'], 'diagnostic': diagnostic,
        'correction_mode': sampled.controls[0].input_evidence.quantitative_signature.measurements[0].correction_mode,
        'background_roi_ids': list(sampled.plan.background_roi_ids), 'sets': initial_sets,
        'measurement_names': list(axis.parent_band_ids), **dict(_reference_wavelength_fields(axis)), 'regions': regions}
    # Historical groups may overlap. Their existing scientific interpretation remains readable.
    if len({identity for group in sampled.control_sets for identity in group}) != sum(map(len, sampled.control_sets)):
        pieces = [project_control_preparation_sets(summary, (group,))['sets'][0] for group in sampled.control_sets]
        summary.update(sets=pieces, can_accept=row.get('proposal_available', False)
                       and all(item['status'] == 'pass' for item in pieces))
        return summary
    return project_control_preparation_sets(summary, sampled.control_sets)


def preparation_summary(service, current, start):
    """Project retained source evidence for either strategy without sampling pixels."""
    from pathlib import Path
    if not any(decision.response.option_value in {'accept_reference_control_files',
            'accept_reference_control_files_with_source_preparation'} for decision in current.active_decisions):
        return ()
    binding = service._one_artifact(current.run_id, start, 'reference_control_files', 'reference-control-files').record
    canonical_inputs = service._canonical_inventory(current.run_id, start)
    inputs = {item.input_id: item for item in canonical_inputs}
    label = lambda identity: Path(inputs[identity].locator_binding.source_locator).name
    phase = None if current.current_checkpoint is None else current.current_checkpoint.value
    active_id = source_input_id(binding, current)
    sources = {source.record.plan.input_id: source for source in accepted_preparations(service, current, start,
        canonical_inputs=canonical_inputs)}
    pending = phase == 'shared_control_preparation' and bool(current.request.proposed_artifacts)
    source = sources.get(active_id)
    if pending:
        source = _resolve_preparation(service, current, start, current.request.proposed_artifacts[0].pointer,
                                      current.prior_checkpoint_sha256, active_id)
    can_revise = bool(current.request and any(option.value == 'revise_shared_control' for option in current.request.options))
    def source_row(identity, dependents):
        stored = source if identity == active_id else sources.get(identity)
        return control_source_row(binding, inputs[identity], stored, dependents,
            accepted=identity in sources, pending=pending and identity == active_id,
            current=phase == 'shared_control_preparation' and identity == active_id,
            can_revise=can_revise and identity == active_id)
    shared = source_row(binding.shared_unstained_input_id, current.fluorophores) if binding.shared_unstained_input_id else None
    components = []
    for index, fluor in enumerate(current.fluorophores):
        control = None
        if binding.negative_control_input_ids:
            control = source_row(binding.negative_control_input_ids[index], (fluor,))
            if not binding.prepare_shared_control:
                after = current.active_decisions
                for position in range(len(after) - 1, -1, -1):
                    if after[position].response.option_value == 'reselect_reference_rois' and after[position].request.fluorophore == fluor:
                        after = after[position + 1:]
                        break
                control.update(accepted=any(item.request.fluorophore == fluor and
                    item.response.option_value == 'accept_negative_reference_rois' for item in after),
                    current=current.current_fluorophore == fluor and phase in
                        {'positive_reference_rois', 'negative_reference_rois', 'background_reference_rois'})
                control['can_open'] = control['current']
        identity = binding.positive_reference_input_ids[index]
        components.append({'name': fluor, 'input_id': identity, 'label': label(identity),
            'status': 'accepted' if fluor in current.accepted_fluorophores else
                'current' if fluor == current.current_fluorophore else 'not_started',
            'can_open': fluor == current.current_fluorophore and phase not in
                {'shared_control_preparation', 'negative_reference_rois', 'background_reference_rois'},
            'entry_page': 'stained', 'control': control})
    active = None if active_id is None else source_row(active_id,
        current.fluorophores if shared else (current.current_fluorophore,))
    progress = ('reference_preparation_progress', {'shared_control': shared, 'components': components,
        'control_source': active, 'preparation_mode': 'independent' if binding.prepare_shared_control else 'per_fluorophore',
        'current_checkpoint': phase, 'current_component': current.current_fluorophore})
    if source is None:
        return (progress,)
    summary = project_control_preparation(source, active)
    projected = (progress, ('control_preparation', summary), ('shared_control_preparation', summary))
    if active['accepted'] and phase == 'af_reference_rois':
        positive = service._live_roi_phase_artifact(current, start, 'sampled_roi_bundle',
            current.current_fluorophore, 'positive_reference_rois', accepted_source=source).record
        assignment = {'source_preparation_sha256': source.content_sha256, 'input_id': active_id,
            'sets': summary['sets'], 'positive_regions': [{'roi_id': item.selection.roi_id,
                'label': f'Stained region {index + 1}', 'accepted_pixel_count': item.sampling.accepted_vector_count}
                for index, item in enumerate(positive.samples)],
            'default_assignments': {item.selection.roi_id: summary['sets'][0]['set_id'] for item in positive.samples}
                if len(summary['sets']) == 1 else {}, 'measurement_names': summary['measurement_names']}
        projected += (('control_assignment', assignment), ('shared_control_assignment', assignment))
    return projected


def review_control_sets(service, run_id, token, *, control_sets, reviewer_id):
    """Regroup retained source samples, invalidating dependent extraction only."""
    from .reference_service_storage import ReferenceCheckpointConflictError
    from .reference_service_records import ReferenceServiceAdvance, ReferenceServiceAdvanceStatus, ReferenceServiceSnapshot
    from .reference_workflow import propose_reference_artifacts
    from .interactions import ArtifactPointer
    current, start = service._store._load_service_checkpoint(run_id)
    _validate_control_sets(control_sets)
    binding = service._one_artifact(run_id, start, 'reference_control_files', 'reference-control-files').record
    input_id = source_input_id(binding, current)
    if input_id is None:
        raise ValueError('Open the control source whose sets should be edited')
    saved = _stored(service, current, start, 'control_preparation_set_review', input_id, token)
    if current.content_sha256 != token:
        if saved is None or saved.record.control_sets != control_sets or saved.record.reviewer_id != reviewer_id:
            raise ReferenceCheckpointConflictError('AF-set review submission is stale')
        pointers = (() if current.request is None else tuple(item.pointer for item in current.request.proposed_artifacts))
        accepted = accepted_preparation(service, current, start)
        if (any(item.artifact_id == saved.content_sha256 for item in pointers)
                or accepted is not None and accepted.content_sha256 == saved.content_sha256):
            return ReferenceServiceAdvance(ReferenceServiceAdvanceStatus.PERSISTED, ReferenceServiceSnapshot(current, start))
        if (current.current_checkpoint.value != 'shared_control_preparation' or not current.decisions
                or current.decisions[-1].source_checkpoint_sha256 != token
                or current.decisions[-1].response.option_value != 'revise_shared_control'):
            raise ReferenceCheckpointConflictError('AF-set review was superseded')
    elif current.current_checkpoint.value == 'shared_control_preparation':
        if not current.request.proposed_artifacts:
            raise ValueError('Sample this source before reviewing its AF sets')
        source = _resolve_preparation(service, current, start, current.request.proposed_artifacts[0].pointer,
            current.prior_checkpoint_sha256, input_id)
    else:
        source = accepted_preparation(service, current, start)
        if source is None:
            raise ValueError('AF-set review requires a prepared source')
    if saved is None:
        _validate_control_sets(control_sets, source.record.plan.control_roi_ids)
        sample_sha, sample_authority, sample_subject = _sample_authority(source)
        review = ControlPreparationSetReview(token, input_id, sample_authority, sample_subject,
            sample_sha, control_sets, reviewer_id)
        saved = service._artifacts.put_unique(run_id=run_id, start_authority_sha256=start,
            authority_checkpoint_sha256=token, kind='control_preparation_set_review', subject_id=input_id, record=review)
    elif saved.record.control_sets != control_sets or saved.record.reviewer_id != reviewer_id:
        raise ReferenceCheckpointConflictError('AF-set review retry choices differ')
    if current.current_checkpoint.value != 'shared_control_preparation':
        service.respond_with_offered_ids(run_id, current.content_sha256, option='revise_shared_control', reviewer_id=reviewer_id)
        current, start = service._store._load_service_checkpoint(run_id)
    pointer = ArtifactPointer('shared_control_preparation', saved.content_sha256,
        saved.record.content_sha256, preparation_subject(current))
    proposed = propose_reference_artifacts(current,
        (service._result_operations._validated_artifact(pointer, 'qupath_roi_geometry', saved.content_sha256),))
    persisted, persisted_start = service._store._compare_and_swap_service(current.content_sha256, proposed, start)
    return ReferenceServiceAdvance(ReferenceServiceAdvanceStatus.PERSISTED, ReferenceServiceSnapshot(persisted, persisted_start))


def control_set_id(preparation_sha256, index):
    return typed_checksum_sha256({'source_preparation_sha256': preparation_sha256, 'set_index': index})


def prepare_stained_reference(service, run_id, token, *, roi_export, positive_roi_ids,
                               background_roi_ids, positive_sampling_policy,
                               background_sampling_policy, background_policy, reviewer_id):
    from .reference_service import _purification_sampling_policy_from_mapping, _background_policy_from_mapping
    from .reference_service_storage import ReferenceCheckpointConflictError
    current, start = service._store._load_service_checkpoint(run_id)
    source = accepted_preparation(service, current, start)
    if source is None or current.current_fluorophore is None:
        raise ValueError('Accept the AF preparation before preparing a stain')
    binding = service._one_artifact(run_id, start, 'reference_control_files', 'reference-control-files').record
    input_id = binding.positive_reference_input_ids[current.fluorophores.index(current.current_fluorophore)]
    policy = lambda value: value if type(value) is CompleteVectorQcPolicy else _purification_sampling_policy_from_mapping(value)
    bg_policy = background_policy if type(background_policy) is BackgroundEquivalencePolicy else _background_policy_from_mapping(background_policy)
    plans = []
    for phase, identities, sampling in (('positive_reference_rois', positive_roi_ids, positive_sampling_policy),
                                        ('background_reference_rois', background_roi_ids, background_sampling_policy)):
        selections = tuple(AcceptedReferenceRoiSelection(roi_export, identity) for identity in identities)
        if any(item.entry.image.reference_input_id != input_id for item in selections):
            raise ValueError('Stain tissue and background must belong to the selected stained image')
        plans.append(ReferenceRoiSelectionPlan(token, phase, current.current_fluorophore, roi_export,
            selections, (), policy(sampling), reviewer_id, source.content_sha256))
    choice = StainedReferenceRegionChoice(*plans, bg_policy)
    if bg_policy != source.record.plan.background_policy:
        raise ValueError('AF background settings require a source preparation revision')
    if current.content_sha256 == token and current.current_checkpoint.value != 'positive_reference_rois':
        raise ValueError('Open stained regions before preparing this reference')
    previous = _stored(service, current, start, 'stained_region_choice', current.current_fluorophore, token)
    if previous is not None and previous.record != choice:
        raise ReferenceCheckpointConflictError('Stained-region retry choices differ')
    if previous is None:
        if current.content_sha256 != token:
            raise ReferenceCheckpointConflictError('Stained-region submission is stale')
        service._artifacts.put_unique(run_id=run_id, start_authority_sha256=start,
            authority_checkpoint_sha256=token, kind='stained_region_choice', subject_id=current.current_fluorophore, record=choice)
    return service.advance_reference_purification(run_id, token, reviewer_id=reviewer_id,
        roi_export=roi_export, selections=choice.positive_plan.selections,
        sampling_policy=choice.positive_plan.sampling_policy)


def _assignment_origin(current, token):
    import json
    from .reference_service_storage import ReferenceCheckpointConflictError
    if current.content_sha256 == token:
        if current.current_checkpoint.value != 'af_reference_rois':
            raise ValueError('Open the AF-set assignment before continuing')
        return
    transitions = list(current.transition_log)
    matching = [index for index, transition in enumerate(transitions) if transition.source_checkpoint_sha256 == token]
    if len(matching) != 1:
        raise ReferenceCheckpointConflictError('AF-set assignment is stale')
    decisions = {item.content_sha256: item for item in current.decisions}
    for transition in transitions[matching[0]:]:
        source = json.loads(transition.source_state_json)
        if (source['checkpoint'] not in {'af_reference_rois', 'background_reference_rois'}
                or source['current_fluorophore'] != current.current_fluorophore
                or transition.kind.value not in {'proposal', 'decision'}
                or transition.kind.value == 'decision' and decisions[transition.decision_record_sha256].response.option_value
                    not in {'accept_af_reference_rois', 'accept_background_reference_rois'}):
            raise ReferenceCheckpointConflictError('AF-set assignment was superseded by another decision')


def assign_shared_control_sets(service, run_id, token, *, assignments, reviewer_id):
    from .reference_service_storage import ReferenceCheckpointConflictError
    from .reference_service_records import ReferenceServiceAdvance, ReferenceServiceAdvanceStatus, ReferenceServiceSnapshot
    from .reference_service_roi_purification import ReferenceRoiPairingSelection
    current, start = service._store._load_service_checkpoint(run_id)
    _assignment_origin(current, token)
    source = accepted_preparation(service, current, start)
    if source is None:
        raise ValueError('AF-set assignments require an accepted source preparation')
    _, positive_authority = service._active_roi_decision_authority(run_id=run_id, current=current,
        start_authority_sha256=start, checkpoint='positive_reference_rois', fluorophore=current.current_fluorophore,
        option_value='accept_positive_reference_rois', artifact_role='positive_reference_rois')
    choice = service._artifacts.load(run_id=run_id, start_authority_sha256=start,
        authority_checkpoint_sha256=positive_authority, kind='stained_region_choice', subject_id=current.current_fluorophore).record
    if choice.positive_plan.source_preparation_sha256 != source.content_sha256:
        raise ReferenceCheckpointConflictError('Stained regions refer to an earlier AF preparation')
    positive_ids = tuple(item.roi_id for item in choice.positive_plan.selections)
    sets = {_source_set_id(source, group): group for group in source.record.control_sets}
    sets.update({control_set_id(source.content_sha256, index): group
                 for index, group in enumerate(source.record.control_sets)})
    if type(assignments) is not dict or set(assignments) != set(positive_ids) or any(value not in sets for value in assignments.values()):
        raise ValueError('Assign one accepted AF set to every stained region')
    pairings = tuple(ReferenceRoiPairingSelection(identity, control) for identity in positive_ids
                     for control in sets[assignments[identity]])
    used = {item.control_roi_id for item in pairings}
    selections = tuple(sample.selection for sample in source.record.controls if sample.selection.roi_id in used)
    service.advance_reference_purification(run_id, token, reviewer_id=reviewer_id,
        roi_export=source.record.plan.export, selections=selections, pairings=pairings,
        sampling_policy=source.record.plan.control_sampling_policy)
    current, start = service._store._load_service_checkpoint(run_id)
    if current.current_checkpoint.value == 'background_reference_rois':
        background_token = current.prior_checkpoint_sha256 if current.request.proposed_artifacts else current.content_sha256
        return service.advance_reference_purification(run_id, background_token, reviewer_id=reviewer_id,
            roi_export=choice.background_plan.export, selections=choice.background_plan.selections,
            sampling_policy=choice.background_plan.sampling_policy, background_policy=choice.background_policy)
    return ReferenceServiceAdvance(ReferenceServiceAdvanceStatus.PERSISTED, ReferenceServiceSnapshot(current, start))
