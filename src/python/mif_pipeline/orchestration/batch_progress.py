"""Persist a read-only current display projection independently of attempt history.

The execution owner publishes this after selecting a new head. Its file revision
binds the displayed state to that head; a missing or interrupted publication is
reported, never used to authorize a write. Polling reuses unchanged parsed data
and reads no historic plans, authorizations, terminals or scientific payloads.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from mif_pipeline.pipeline_core import InteractionReason, StageStatus
from .setup.batch_prerequisites import BatchPrerequisiteItem

HEAD_FIELDS = (
    'batch_id', 'sha256', 'active_plan_sha256', 'execution_state',
    'quiescent_pause_sha256', 'node_status_by_id', 'domain_task_status_by_id',
    'failure_codes_by_node', 'invalidated_node_ids', 'pending_interaction_ids',
    'pending_authorization_sha256_by_node', 'last_durable_node_ids_by_sample',
    'next_eligible_node_ids_by_sample', 'node_scope_by_id',
    'processed_image_paths_by_sample', 'sample_project_entry_id_by_sample',
    'operational_rebinding_sha256',
)
SAMPLE_FIELDS = ('sample_id', 'slide_id', 'subject_id', 'resolved_config_sha256')


def revision(path):
    """Observe one atomic file revision without reading its growing history."""
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class _DisplayRecord:
    values: Mapping

    def __getattr__(self, name):
        try:
            return self.values[name]
        except KeyError:
            raise AttributeError(name) from None


@dataclass(frozen=True, slots=True)
class CurrentProgressPlan:
    batch_id: str
    plan_sha256: str
    nodes: tuple
    deferred_nodes: tuple
    prerequisites: tuple


@dataclass(frozen=True, slots=True)
class CurrentProgressSnapshot:
    head: _DisplayRecord
    plan: CurrentProgressPlan
    manifest: _DisplayRecord
    current_results: Mapping
    completed_export_sample_ids: frozenset[str]


def build_projection(store, head):
    """Resolve current diagnostics once during the existing writer publication.

    Pending authorizations supersede their preceding attempt's diagnostic. We
    leave that terminal in history and skip its read for this display record.
    """
    import json

    plan = store._load_typed_plan(head.active_plan_sha256)
    manifest = json.loads(store.retained_manifest(head.normalized_manifest_sha256).normalized_manifest_bytes)
    # A checked candidate head has already validated these retained identities.
    samples = [item for item in manifest['samples'] if item.get('enabled', True)]
    diagnostics = {}
    active_node_ids = {node.node_id for node in plan.nodes}
    completed_exports = []
    processed_images = {}
    for node in plan.nodes:
        if (
            node.node_id in head.invalidated_node_ids
            or node.node_id in head.pending_authorization_sha256_by_node
        ):
            continue
        attempts = head.attempt_ids_by_node.get(node.node_id, ())
        if not attempts:
            continue
        digest = head.terminal_attempt_sha256_by_attempt_id.get(attempts[-1])
        if digest is None:
            continue
        status = head.node_status_by_id.get(node.node_id)
        prepares_image = 'processed_image' in node.declared_output_kinds
        if (status not in {StageStatus.FAILED, StageStatus.NEEDS_INTERACTION}
                and node.kind != 'final_non_spatial_export' and not prepares_image):
            continue
        attempt = store.load_attempt(digest)
        if attempt.node_id != node.node_id:
            raise ValueError('Current diagnostic belongs to another node')
        result = attempt.stage_result
        diagnostics[node.node_id] = {
            'interaction_reason': None if result.interaction_reason is None else result.interaction_reason.value,
            'error_code': result.error_code,
            'error_message': result.error_message,
        }
        if (node.kind == 'final_non_spatial_export' and len(node.participant_ids) == 1 and
            status is StageStatus.COMPLETED and any(
                artifact.artifact_kind == 'final_non_spatial_export_result' and
                artifact.artifact_ref_sha256 in head.available_artifact_ref_sha256_by_key
                for artifact in attempt.output_artifacts)):
            completed_exports.append(node.participant_ids[0])
        if prepares_image and status is StageStatus.COMPLETED:
            for artifact in attempt.output_artifacts:
                if (artifact.artifact_kind == 'processed_image' and artifact.locator is not None
                        and artifact.artifact_ref_sha256 in head.available_artifact_ref_sha256_by_key):
                    processed_images[artifact.scope.sample_id] = str(artifact.locator)
    current_head = {name: getattr(head, name) for name in HEAD_FIELDS}
    current_head['processed_image_paths_by_sample'] = processed_images
    for name in ('node_status_by_id', 'failure_codes_by_node', 'node_scope_by_id'):
        current_head[name] = {key: value for key, value in current_head[name].items() if key in active_node_ids}
    return {
        'schema_version': 'mif-batch-current-progress/v1',
        'head': current_head,
        'head_revision': revision(store.layout.state_head_path),
        'plan': {
            'batch_id': plan.batch_id, 'plan_sha256': plan.plan_sha256,
            'nodes': [{name: getattr(node, name) for name in ('node_id', 'kind', 'participant_ids', 'dependency_ids')} for node in plan.nodes],
            'deferred_nodes': [{name: getattr(node, name) for name in ('descriptor_sha256', 'node_kind', 'participant_ids', 'prerequisite_node_kinds')} for node in plan.deferred_nodes],
            'prerequisites': [item.to_mapping() for item in plan.prerequisites],
        },
        'samples': [{**{name: item.get(name) for name in SAMPLE_FIELDS}, 'resolved_config_sha256': head.resolved_config_sha256_by_sample[item['sample_id']]} for item in samples],
        'execution_config': manifest['execution_config'],
        'diagnostics': diagnostics,
        'completed_export_sample_ids': sorted(completed_exports),
    }


def decode_projection(mapping):
    """Decode the bounded display record at its filesystem boundary."""
    from .batch_state import BatchExecutionState
    from .batch_manifest import BatchExecutionConfig

    if not isinstance(mapping, Mapping) or set(mapping) != {
        'schema_version', 'head', 'head_revision', 'plan', 'samples', 'diagnostics',
        'completed_export_sample_ids',
        'execution_config',
    } or mapping['schema_version'] != 'mif-batch-current-progress/v1':
        raise ValueError('Current progress record has an invalid shape')
    fields = dict(mapping['head'])
    if set(fields) != set(HEAD_FIELDS):
        raise ValueError('Current progress head has an invalid shape')
    fields['execution_state'] = BatchExecutionState(fields['execution_state'])
    for name in ('node_status_by_id', 'domain_task_status_by_id'):
        fields[name] = {key: StageStatus(value) for key, value in fields[name].items()}
    material = mapping['plan']
    plan = CurrentProgressPlan(
        material['batch_id'], material['plan_sha256'],
        tuple(_DisplayRecord(_freeze(row)) for row in material['nodes']),
        tuple(_DisplayRecord(_freeze(row)) for row in material['deferred_nodes']),
        tuple(BatchPrerequisiteItem.from_mapping(row) for row in material['prerequisites']),
    )
    if plan.plan_sha256 != fields['active_plan_sha256'] or plan.batch_id != fields['batch_id']:
        raise ValueError('Current progress plan differs from its head')
    diagnostics = {}
    for key, row in mapping['diagnostics'].items():
        row = dict(row)
        row['interaction_reason'] = None if row['interaction_reason'] is None else InteractionReason(row['interaction_reason'])
        diagnostics[key] = _DisplayRecord(_freeze(row))
    return CurrentProgressSnapshot(
        _DisplayRecord(_freeze(fields)), plan,
        _DisplayRecord(MappingProxyType({
            'enabled_samples': tuple(_DisplayRecord(_freeze(row)) for row in mapping['samples']),
            'execution_config': BatchExecutionConfig.from_mapping(mapping['execution_config']),
        })),
        MappingProxyType(diagnostics), frozenset(mapping['completed_export_sample_ids']),
    )
