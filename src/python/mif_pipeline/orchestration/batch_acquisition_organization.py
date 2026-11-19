"""Owner-bound cycle organization at the end of source inspection."""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
from pathlib import Path

from mif_pipeline.io import InspectedSourceAuthority
from mif_pipeline.io.acquired_plane_view import build_plane_rows, project_plane_metadata
from mif_pipeline.io.canonical_ingestion import _validate_inspected_source_stat
from mif_pipeline.io.metadata.cycles import (resolve_channel_cycles, resolve_source_channel_cycles,
    validate_channel_cycle_values)
from mif_pipeline.pipeline_core import StageResult, StageStatus, InteractionReason, InteractionAction
from .batch_preprocessing_support import artifact_path
from .batch_preprocessing_wire import canonical_digest

INTERACTION_VERSION = 'mif-batch-acquisition-organization/v1'
RESPONSE_VERSION = 'mif-batch-acquisition-organization-response/v1'
REVISION_DISPATCH_VERSION = 'mif-inspection-review-dispatch/v1'


def pending_inspection_path(sample, node_id):
    return artifact_path(sample, f'inspection/{node_id}/pending-source-inspection.json')


def load_pending_inspection(sample, node_id):
    from .batch_stage1_execution import _validate_source_selection
    path = pending_inspection_path(sample, node_id)
    if path.is_symlink() or path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError('Invalid pending source inspection')
    payload = path.read_bytes()
    authority = InspectedSourceAuthority.from_json_bytes(payload)
    _validate_source_selection(authority, sample, allow_pending_organization=True)
    _validate_inspected_source_stat(authority.source_identity)
    return authority, hashlib.sha256(payload).hexdigest()


def requires_organization(authority):
    image = authority.canonical_ome.images[authority.selection.source_image_index]
    return image.pixels.size_t > 1 or resolve_channel_cycles(authority.canonical_ome,
        authority.selection.source_image_index) is not None


def previous_inspection_artifact(store, node, head):
    """Resolve only the completed predecessor named by this admitted revision."""
    revision = node.settled_review_revision
    if revision is None:
        return None
    terminal = store.load_attempt(revision.predecessor_terminal_record_sha256)
    attempts = head.attempt_ids_by_node.get(revision.predecessor_node_id, ())
    authorization = store.load_authorization(terminal.authorization_sha256)
    if (node.kind != 'inspection' or terminal.node_id != revision.predecessor_node_id
            or terminal.stage_result.stage != 'inspection' or terminal.stage_result.status is not StageStatus.COMPLETED
            or not attempts or terminal.attempt_id != attempts[-1]
            or head.terminal_attempt_sha256_by_attempt_id.get(attempts[-1]) != revision.predecessor_terminal_record_sha256
            or authorization.dispatch_binding.participant_ids != node.participant_ids
            or len(terminal.output_artifacts) != 1
            or terminal.output_artifacts[0].artifact_kind != 'source_inspection'):
        raise ValueError('Imaging-cycle revision lacks its exact completed inspection predecessor')
    return terminal.output_artifacts[0]


def inspection_response_dispatch(store, context, response):
    """Carry the exact retired inspection solely as review defaults."""
    execution = store.authenticate_current_execution_context(context)
    snapshot = store.reopen_execution_snapshot(expected_head_sha256=execution.state_head_sha256)
    node = next(item for item in snapshot.plan.nodes if item.node_id == context.node_id)
    previous = previous_inspection_artifact(store, node, snapshot.head)
    if previous is None:
        return response
    return dict(schema_version=REVISION_DISPATCH_VERSION,
        previous_inspection_artifact=previous.to_mapping(), accepted_response=response)


def organization_seed(authority, previous):
    if previous is None:
        return None
    from mif_pipeline.io.acquired_plane_view import is_acquired_plane_view, read_plane_descriptor
    if not is_acquired_plane_view(previous.source_identity.resolved_path):
        if previous.content_sha256 != authority.content_sha256:
            raise ValueError('Previous inspection differs from the original source selection')
        return None
    parent, record, _, _ = read_plane_descriptor(previous.source_identity.resolved_path, inspection=previous)
    if parent.content_sha256 != authority.content_sha256:
        raise ValueError('Previous cycle review belongs to another original inspection')
    return dict(previous_authority_sha256=previous.content_sha256,
        layout=record['decision']['layout'], registered=record['decision']['registered'],
        channel_cycle_values=[dict(channel_id=row['channel_id'], cycle=row['cycle']) for row in record['rows']])


def organization_stage_result(authority, *, slide_id, node_id, pending_sha256, seed=None):
    ome = authority.canonical_ome
    pixels = ome.images[authority.selection.source_image_index].pixels
    temporal = pixels.size_t > 1
    recorded_cycles = None if temporal else resolve_channel_cycles(ome, authority.selection.source_image_index)
    source_cycles = None if temporal else resolve_source_channel_cycles(ome, authority.selection.source_image_index)
    # T supplies an address, not the staining-round declaration. Until explicit
    # review, no provisional cycle number is presented as an acquisition fact.
    numbers = (None,) * pixels.size_t if temporal else recorded_cycles or (None,) * pixels.size_c
    rows = build_plane_rows(authority, numbers)
    displayed = [dict(row, channel_name=pixels.channels[row['source_c']].name,
        channel_index=row['source_c'], fluorophore=pixels.channels[row['source_c']].fluor)
        for row in rows]
    context = dict(schema_version=INTERACTION_VERSION, response_schema_version=RESPONSE_VERSION,
        node_id=node_id, original_authority_sha256=authority.content_sha256,
        pending_inspection_sha256=pending_sha256, source_series_index=authority.selection.source_series_index,
        source_size_c=pixels.size_c, source_size_t=pixels.size_t,
        source_channel_cycles=None if source_cycles is None else list(source_cycles),
        plane_rows=displayed, layout=None if temporal else 'planar_channels',
        channel_cycle_values=[dict(channel_id=row['channel_id'], cycle=row['cycle']) for row in rows])
    if seed is not None:
        context.update(seed)
    return StageResult.needs_interaction(stage='inspection', slide_id=slide_id,
        reason=InteractionReason.OTHER_EXPLICIT_REASON,
        message='Review which acquired source planes belong to each imaging cycle.',
        allowed_actions=(InteractionAction.ACCEPT.value,),
        resume_context=dict(schema_version=INTERACTION_VERSION, service_context=context),
        checkpoint_id=canonical_digest(context))


def bind_organization_response(result, action, raw):
    if (not isinstance(result, StageResult) or result.stage != 'inspection'
            or result.status is not StageStatus.NEEDS_INTERACTION
            or result.interaction_reason is not InteractionReason.OTHER_EXPLICIT_REASON
            or action != 'accept' or action not in result.allowed_actions):
        raise ValueError('Cycle organization response is foreign or not offered')
    context = result.resume_context
    owner = context.get('service_context', {})
    if (context.get('schema_version') != INTERACTION_VERSION
            or owner.get('schema_version') != INTERACTION_VERSION
            or result.checkpoint_id != canonical_digest(owner)):
        raise ValueError('Cycle organization owner context is stale')
    if not isinstance(raw, Mapping) or set(raw) != {'layout', 'registered', 'channel_cycle_values'}:
        raise ValueError('Cycle organization response fields differ')
    temporal = owner['source_size_t'] > 1
    layout = 't_cycles' if temporal else 'planar_channels'
    if raw['layout'] != layout:
        raise ValueError('Confirm that source T positions are imaging cycles. Time-lapse and mixed time/cycle dimensions are unsupported.')
    if type(raw['registered']) is not bool or (temporal and not raw['registered']):
        raise ValueError('These cycles must already be registered to one spatial grid. Register the source first or use separate-cycle registration.')
    cycles = validate_channel_cycle_values(tuple(row['channel_id'] for row in owner['plane_rows']), raw['channel_cycle_values'])
    if temporal:
        count = owner['source_size_c']
        groups = [set(cycles[t * count:(t + 1) * count]) for t in range(owner['source_size_t'])]
        if any(None in group or len(group) != 1 for group in groups) or len(set(cycles)) != owner['source_size_t']:
            raise ValueError('Use one distinct cycle for every original T position, including all its bands')
    return dict(schema_version=RESPONSE_VERSION, checkpoint_id=result.checkpoint_id,
        original_authority_sha256=owner['original_authority_sha256'],
        pending_inspection_sha256=owner['pending_inspection_sha256'], layout=layout,
        registered=raw['registered'], channel_cycle_values=[dict(row) for row in raw['channel_cycle_values']])


def validate_organization_response(authority, result, response):
    fields = {'schema_version', 'checkpoint_id', 'original_authority_sha256',
        'pending_inspection_sha256', 'layout', 'registered', 'channel_cycle_values'}
    if not isinstance(response, Mapping) or set(response) != fields:
        raise ValueError('Cycle organization response fields differ')
    expected = bind_organization_response(result, 'accept', {key: response[key]
        for key in ('layout', 'registered', 'channel_cycle_values')})
    if dict(response) != expected:
        raise ValueError('Cycle organization response is stale or belongs to another inspection')
    pixels = authority.canonical_ome.images[authority.selection.source_image_index].pixels
    values = response['channel_cycle_values']
    cycles = tuple(row['cycle'] for row in values)
    rows = build_plane_rows(authority, cycles[::pixels.size_c] if pixels.size_t > 1 else cycles)
    decision = dict(origin='researcher', layout=response['layout'], registered=response['registered'])
    project_plane_metadata(authority, rows, decision)
    return rows, decision
