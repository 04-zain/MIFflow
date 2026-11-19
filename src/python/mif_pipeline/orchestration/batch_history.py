"""Separate bounded current state from immutable incremental execution history.

History events store only additions to head and attempt lineage. The current file
selects one completed tail. Execution opens reconstruct that selected view once;
progress readers use their independent current projection and never traverse it.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

LIST_FIELDS = ('ancestor_head_sha256s', 'plan_history_sha256s')
MAP_FIELDS = (
    'terminal_attempt_sha256_by_attempt_id', 'revision_reason_by_plan_sha256',
    'node_scope_by_id', 'node_status_by_id', 'failure_codes_by_node',
    'generation_references_by_node', 'checkpoint_descriptor_sha256_by_node',
    'last_durable_node_ids_by_sample', 'last_durable_node_ids_by_domain',
)
ATTEMPT_FIELD = 'attempt_ids_by_node'
HISTORY_FIELDS = (*LIST_FIELDS, *MAP_FIELDS, ATTEMPT_FIELD)
CURRENT_SCHEMA = 'mif-batch-current'


def canonical_bytes(value):
    return json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode('utf-8')


def empty_history():
    return {**{name: [] for name in LIST_FIELDS}, **{name: {} for name in MAP_FIELDS}, ATTEMPT_FIELD: {}}


def history_event(before, after, predecessor):
    """Encode only newly admitted lineage and changed reason/terminal entries."""
    append = {}
    for name in LIST_FIELDS:
        previous, current = list(before[name]), list(after[name])
        if current[:len(previous)] != previous:
            raise ValueError('Execution lineage must be append-only')
        append[name] = current[len(previous):]
    attempts = {}
    for node, current in after[ATTEMPT_FIELD].items():
        previous = list(before[ATTEMPT_FIELD].get(node, ()))
        current = list(current)
        if current[:len(previous)] != previous:
            raise ValueError('Attempt lineage must be append-only')
        if current[len(previous):]:
            attempts[node] = current[len(previous):]
    if set(before[ATTEMPT_FIELD]) - set(after[ATTEMPT_FIELD]):
        raise ValueError('Attempt lineage cannot be removed')
    updates = {}
    for name in MAP_FIELDS:
        updates[name] = {key: value for key, value in after[name].items() if before[name].get(key) != value}
        updates[name].update({key: None for key in set(before[name]) - set(after[name])})
    return {'schema_version': 'mif-batch-history/v1', 'predecessor': predecessor,
            'append': append, 'attempt_appends': attempts, 'updates': updates}


def apply_event(before, event):
    if not isinstance(event, Mapping) or set(event) != {'schema_version', 'predecessor', 'append', 'attempt_appends', 'updates'} or event['schema_version'] != 'mif-batch-history/v1':
        raise ValueError('Execution history event has an invalid shape')
    if set(event['append']) != set(LIST_FIELDS) or set(event['updates']) != set(MAP_FIELDS):
        raise ValueError('Execution history event has invalid fields')
    result = {name: list(before[name]) + list(event['append'][name]) for name in LIST_FIELDS}
    for name in MAP_FIELDS:
        result[name] = dict(before[name])
        for key, value in event['updates'][name].items():
            if value is None:
                result[name].pop(key, None)
            else:
                result[name][key] = value
    result[ATTEMPT_FIELD] = {node: list(values) for node, values in before[ATTEMPT_FIELD].items()}
    for node, values in event['attempt_appends'].items():
        result[ATTEMPT_FIELD].setdefault(node, []).extend(values)
    return result


def current_mapping(full_head, tail):
    return {**{key: value for key, value in full_head.items() if key not in HISTORY_FIELDS},
            'schema_name': CURRENT_SCHEMA, 'history_tail_sha256': tail}


def initial_current_bytes(full_head):
    """Prepare the exact compact file selected by a fresh graph import."""
    history = {name: full_head[name] for name in HISTORY_FIELDS}
    event = history_event(empty_history(), history, None)
    tail = hashlib.sha256(canonical_bytes(event)).hexdigest()
    return canonical_bytes(current_mapping(full_head, tail))


def read_history(tail, read_bytes, cache=None):
    """Reopen selected immutable events, reusing one owned decoded prefix."""
    events = []
    cursor = tail
    seen = set()
    while cursor is not None and (cache is None or cursor != cache[0]):
        if not isinstance(cursor, str) or len(cursor) != 64 or any(c not in '0123456789abcdef' for c in cursor) or cursor in seen:
            raise ValueError('Execution history tail is invalid or cyclic')
        seen.add(cursor)
        payload = read_bytes(cursor)
        if hashlib.sha256(payload).hexdigest() != cursor:
            raise ValueError('Execution history event bytes changed')
        event = json.loads(payload)
        if canonical_bytes(event) != payload:
            raise ValueError('Execution history bytes are not canonical')
        events.append(event)
        cursor = event['predecessor']
    history = empty_history() if cursor is None else cache[1]
    for event in reversed(events):
        history = apply_event(history, event)
    return history


def full_mapping(current, history):
    if current.get('schema_name') != CURRENT_SCHEMA or 'history_tail_sha256' not in current:
        raise ValueError('Execution requires the current compact state format')
    return {**{key: value for key, value in current.items() if key != 'history_tail_sha256'},
            **history, 'schema_name': 'mif-batch-state-head'}


def decode_file_mapping(mapping, state_root):
    """Decode the selected state for the project durability boundary."""
    def read(digest):
        path = state_root / f'history-{digest}.json'
        if path.is_symlink() or not path.is_file():
            raise ValueError('Execution history event is missing or linked')
        return path.read_bytes()
    history = read_history(mapping.get('history_tail_sha256'), read)
    return full_mapping(mapping, history)
