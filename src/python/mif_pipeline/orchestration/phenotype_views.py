"""Append-only persistence for accepted, method-specific phenotype views.

The `PreparedPhenotypeView` record carries `payload`, `scope_root`, `artifact_path`,
`artifact_sha256`, `lineage_sha256` and its remaining declared fields.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from mif_pipeline.pipeline_core.errors import ROIAnalysisError, validate_safe_path_segment
from mif_pipeline.qupath_runtime.exchange import _content_digest

ARTIFACT_SCHEMA = "phenotype-view-artifact/v1"
INVENTORY_SCHEMA = "phenotype-view-inventory/v1"
POINTER_SCHEMA = "phenotype-view-inventory-pointer/v1"
TRANSACTION_SCHEMA = "phenotype-view-transaction/v1"
PHENOTYPE_MODES = frozenset({"marker_rules", "supervised_classifier", "unsupervised_clustering"})
_ASSIGNMENT_FIELDS = frozenset(
    {
        "cell_id",
        "analysis_domain_id",
        "primary_phenotype",
        "phenotype_source",
        "phenotype_components",
        "marker_states",
        "phenotype_confidence",
        "cluster_id",
        "cluster_label",
    }
)
_NULLABLE_ASSIGNMENT_FIELDS = frozenset({"phenotype_confidence", "cluster_id", "cluster_label"})
_REQUIRED_ASSIGNMENT_FIELDS = _ASSIGNMENT_FIELDS - _NULLABLE_ASSIGNMENT_FIELDS
_TASK_FIELDS = frozenset(
    {
        "schema_version",
        "mode",
        "slide_id",
        "canonical_image_sha256",
        "tissue_generation_id",
        "phenotype_generation_id",
        "section_id",
        "source_run_id",
        "cell_ids",
        "cell_ids_sha256",
        "assignments",
        "evidence",
        "lineage",
    }
)


# ==============================================================================
#                                Phenotype views
# ==============================================================================


class PhenotypeViewStoreError(ROIAnalysisError):
    """Raised when accepted phenotype-view persistence is stale or corrupt."""


def _nonblank(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PhenotypeViewStoreError(f"{field} must be nonblank text")
    return value


def _sha256(value: object, field: str) -> str:
    text = _nonblank(value, field)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise PhenotypeViewStoreError(f"{field} must be a lowercase SHA-256")
    return text


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise PhenotypeViewStoreError(f"phenotype view artifact is unavailable: {path}") from exc
    return digest.hexdigest()


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PhenotypeViewStoreError(
            "phenotype view artifact must contain finite JSON values"
        ) from exc


def _read_mapping(
    path: Path, field: str, *, expected_sha256: str | None = None
) -> dict[str, Any]:
    """Parse our saved metadata and check its digest from the same bytes."""
    try:
        if path.stat().st_size > 16 * 1024**2:
            raise PhenotypeViewStoreError(f"{field} exceeds its metadata allowance")
        content = path.read_bytes()
        if expected_sha256 is not None and hashlib.sha256(content).hexdigest() != expected_sha256:
            raise PhenotypeViewStoreError(f"{field} differs from its recorded SHA-256")
        value = json.loads(content.decode("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PhenotypeViewStoreError(f"{field} is invalid JSON: {path}") from exc
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise PhenotypeViewStoreError(f"{field} must be a JSON object")
    return value


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _export(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _export(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_export(item) for item in value]
    return value


def stable_cell_ids_sha256(cell_ids: Sequence[str]) -> str:
    """Hash the exact sorted stable-ID JSON array used by QuPath exchanges."""
    return hashlib.sha256(
        json.dumps(list(cell_ids), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _canonical_assignment_mapping(
    value: object,
    *,
    additional_required_fields: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Normalize only schema-declared nullable assignment fields to null."""
    if not isinstance(value, Mapping):
        raise PhenotypeViewStoreError("phenotype assignment fields are invalid")
    fields = set(value)
    allowed = _ASSIGNMENT_FIELDS | additional_required_fields
    required = _REQUIRED_ASSIGNMENT_FIELDS | additional_required_fields
    if fields - allowed or not required.issubset(fields):
        raise PhenotypeViewStoreError("phenotype assignment fields are invalid")
    normalized = {key: _export(item) for key, item in value.items()}
    _nonblank(normalized["analysis_domain_id"], "assignment analysis-domain mapping")
    for field in _NULLABLE_ASSIGNMENT_FIELDS:
        normalized.setdefault(field, None)
    return normalized


_ARTIFACT_FIELDS = (_TASK_FIELDS - {'cell_ids', 'assignments'}) | {
    'cell_count', 'cell_to_domain_membership_sha256', 'assignments_sha256'}


@dataclass(frozen=True, slots=True)
class PhenotypeViewSection:
    section_id: str
    source_run_id: str
    artifact_path: Path
    artifact_sha256: str
    lineage_sha256: str
    cell_count: int
    cell_ids_sha256: str
    cell_to_domain_membership_sha256: str
    assignments_sha256: str
    component_descriptor: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class PhenotypeViewGeneration:
    mode: str
    generation_id: str
    tissue_generation_id: str
    artifact_sha256: str
    sections: tuple[PhenotypeViewSection, ...]


@dataclass(frozen=True, slots=True)
class PhenotypeViewInventory:
    slide_id: str
    canonical_image_sha256: str
    tissue_generation_id: str
    views: Mapping[str, tuple[PhenotypeViewGeneration, ...]]
    inventory_sha256: str
    pointer_path: Path
    pointer_bytes: bytes


@dataclass(frozen=True, slots=True)
class PreparedPhenotypeView:
    payload: Mapping[str, Any]
    scope_root: Path
    artifact_path: Path
    artifact_sha256: str
    lineage_sha256: str
    transaction_id: str
    journal_path: Path
    predecessor_inventory_sha256: str | None
    attempt_id: int
    assignment_source: Mapping[str, Any] | None = None

    def to_request_mapping(self) -> dict[str, object]:
        """Expose our compact prepared identity, independently of task row storage."""
        payload = self.payload
        return {'schema_version': 'phenotype-view-reference/v1',
            **{key: payload[key] for key in ('mode', 'slide_id', 'canonical_image_sha256',
                'tissue_generation_id', 'phenotype_generation_id', 'section_id', 'source_run_id',
                'cell_count', 'cell_ids_sha256', 'cell_to_domain_membership_sha256', 'assignments_sha256')},
            'artifact_path': str(self.artifact_path), 'artifact_sha256': self.artifact_sha256,
            'lineage_sha256': self.lineage_sha256}


class PhenotypeViewStore:
    """Our current metadata inventory selects immutable typed phenotype components."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _scope_root(self, slide_id, canonical_image_sha256, tissue_generation_id):
        return self.root / validate_safe_path_segment(slide_id, 'slide_id') / _sha256(canonical_image_sha256, 'canonical_image_sha256') / hashlib.sha256(tissue_generation_id.encode()).hexdigest()

    def artifact_path(self, artifact, *, artifact_sha256):
        """Keep each prepared result's provenance when an unchanged task is retried.

        A retry has a new producing attempt but can retain the same scientific
        generation and section. The metadata digest distinguishes those attempts;
        the accepted inventory still selects one completed result for the section.
        """
        scope = self._scope_root(artifact['slide_id'], artifact['canonical_image_sha256'], artifact['tissue_generation_id'])
        mode = validate_safe_path_segment(artifact['mode'], 'phenotype mode')
        generation = validate_safe_path_segment(artifact['phenotype_generation_id'], 'phenotype generation')
        section = validate_safe_path_segment(artifact['section_id'], 'section')
        return scope / mode / generation / section / (_sha256(artifact_sha256, 'phenotype metadata SHA-256') + '.json')

    @staticmethod
    def _metadata(value):
        """Admit our current bounded metadata without rebuilding scientific assignments."""
        if not isinstance(value, Mapping) or set(value) != _ARTIFACT_FIELDS:
            raise PhenotypeViewStoreError('Phenotype metadata requires its current fields')
        value = _export(value)
        if value['schema_version'] != ARTIFACT_SCHEMA or value['mode'] not in PHENOTYPE_MODES:
            raise PhenotypeViewStoreError('Phenotype metadata has an invalid current mode/schema')
        for key in ('slide_id', 'tissue_generation_id', 'phenotype_generation_id', 'section_id', 'source_run_id'):
            _nonblank(value[key], key)
        for key in ('canonical_image_sha256', 'cell_ids_sha256', 'cell_to_domain_membership_sha256', 'assignments_sha256'):
            _sha256(value[key], key)
        if type(value['cell_count']) is not int or value['cell_count'] < 0:
            raise PhenotypeViewStoreError('Phenotype metadata requires its accepted cell count')
        if not isinstance(value['lineage'], dict) or not isinstance(value['evidence'], dict) or len(_json_bytes(value)) > 16 * 1024**2:
            raise PhenotypeViewStoreError('Phenotype metadata exceeds its finite metadata allowance')
        return value

    def prepare(self, artifact):
        """Validate our task rows once and persist only their compact accepted identity.

        i) We create a temporary exact-byte assignment source for native application.
        ii) We retain its digest in metadata; completed views select typed components.
        """
        task = self._validate_task_artifact(artifact)
        assignments = task.pop('assignments')
        ids = task.pop('cell_ids')
        assignment_bytes = _json_bytes(assignments)
        task.update(cell_count=len(ids), assignments_sha256=hashlib.sha256(assignment_bytes).hexdigest(),
            cell_to_domain_membership_sha256=_content_digest({row['cell_id']: row['analysis_domain_id'] for row in assignments}))
        prepared = self.reopen_prepared(task)
        source = prepared.scope_root / '.pending' / (prepared.artifact_sha256 + '.json')
        if source.exists() and source.read_bytes() != assignment_bytes:
            raise PhenotypeViewStoreError('A prepared assignment source changed its exact bytes')
        if not source.exists():
            _atomic_write(source, assignment_bytes)
        from dataclasses import replace
        return replace(prepared, assignment_source=_freeze({'path': str(source.resolve()), 'sha256': task['assignments_sha256']}))

    def reopen_prepared(self, metadata):
        """Reopen our immutable metadata and current transaction without task rows."""
        value = self._metadata(metadata)
        payload = _json_bytes(value)
        sha = hashlib.sha256(payload).hexdigest()
        path = self.artifact_path(value, artifact_sha256=sha)
        if path.exists() and path.read_bytes() != payload:
            raise PhenotypeViewStoreError('An accepted phenotype generation is immutable')
        if not path.exists():
            _atomic_write(path, payload)
        scope = self._scope_root(value['slide_id'], value['canonical_image_sha256'], value['tissue_generation_id'])
        # A repeated task reuses its original current transaction and component.
        for journal_path in sorted((scope / '.transactions').glob('*.json')):
            journal = self._read_journal_file(journal_path)
            if journal['artifact_sha256'] == sha:
                return self._prepared(value, path, scope, journal_path, journal)
        current = self._load_optional(**{key: value[key] for key in ('slide_id', 'canonical_image_sha256', 'tissue_generation_id')})
        predecessor = current.inventory_sha256 if current else None
        attempt = 0
        transaction = _content_digest({'artifact_sha256': sha, 'predecessor_inventory_sha256': predecessor, 'attempt_id': attempt})
        journal_path = scope / '.transactions' / (transaction + '.json')
        journal = {'schema_version': TRANSACTION_SCHEMA, 'transaction_id': transaction,
            'artifact_path': str(path.relative_to(scope)), 'artifact_sha256': sha,
            'predecessor_inventory_sha256': predecessor, 'attempt_id': attempt,
            'state': 'prepared', 'component_descriptor': None, 'inventory_sha256': None}
        self._write_journal(journal_path, journal)
        return self._prepared(value, path, scope, journal_path, journal)

    def reopen_recorded(self, reference):
        """Read the exact prepared file and transaction named by a completed result.

        We retain its original location and producing attempt. This operation
        writes neither metadata nor assignments while the result is checked.
        """
        if not isinstance(reference, Mapping):
            raise PhenotypeViewStoreError('The recorded phenotype reference is invalid')
        path = Path(reference['artifact_path'])
        if not path.is_absolute():
            raise PhenotypeViewStoreError('The recorded phenotype metadata changed')
        metadata = self._metadata(_read_mapping(path, 'recorded phenotype metadata', expected_sha256=_sha256(reference['artifact_sha256'], 'recorded phenotype SHA-256')))
        scope = self._scope_root(metadata['slide_id'], metadata['canonical_image_sha256'], metadata['tissue_generation_id'])
        relative = path.resolve().relative_to(scope.resolve())
        matches = []
        for journal_path in (scope / '.transactions').glob('*.json'):
            journal = self._read_journal_file(journal_path)
            if journal['artifact_sha256'] == reference['artifact_sha256']:
                if journal['artifact_path'] != str(relative):
                    raise PhenotypeViewStoreError('The recorded phenotype transaction location differs')
                matches.append(self._prepared(metadata, path, scope, journal_path, journal))
        if len(matches) != 1 or matches[0].to_request_mapping() != dict(reference):
            raise PhenotypeViewStoreError('The recorded phenotype transaction differs')
        return matches[0]

    @staticmethod
    def _prepared(metadata, path, scope, journal_path, journal):
        return PreparedPhenotypeView(_freeze(metadata), scope, path, journal['artifact_sha256'],
            _content_digest(metadata['lineage']), journal['transaction_id'], journal_path,
            journal['predecessor_inventory_sha256'], journal['attempt_id'])

    @staticmethod
    def _write_journal(path, journal):
        value = {key: _export(item) for key, item in journal.items() if key != 'journal_sha256'}
        value['journal_sha256'] = _content_digest(value)
        _atomic_write(path, _json_bytes(value))

    @staticmethod
    def _read_journal_file(path):
        journal = _read_mapping(path, 'phenotype transaction')
        fields = {'schema_version', 'transaction_id', 'artifact_path', 'artifact_sha256',
            'predecessor_inventory_sha256', 'attempt_id', 'state', 'component_descriptor',
            'inventory_sha256', 'journal_sha256'}
        if (set(journal) != fields or journal['schema_version'] != TRANSACTION_SCHEMA
                or journal['state'] not in {'prepared', 'component_applied', 'head_committed'}
                or journal['journal_sha256'] != _content_digest({key: value for key, value in journal.items() if key != 'journal_sha256'})
                or path.name != journal['transaction_id'] + '.json'):
            raise PhenotypeViewStoreError('The current phenotype transaction is corrupt')
        for key in ('artifact_sha256', 'transaction_id'):
            _sha256(journal[key], key)
        if (type(journal['attempt_id']) is not int or journal['attempt_id'] < 0
                or Path(journal['artifact_path']).is_absolute() or '..' in Path(journal['artifact_path']).parts
                or journal['transaction_id'] != _content_digest({key: journal[key] for key in ('artifact_sha256', 'predecessor_inventory_sha256', 'attempt_id')})):
            raise PhenotypeViewStoreError('The current phenotype transaction identity differs')
        if journal['predecessor_inventory_sha256'] is not None:
            _sha256(journal['predecessor_inventory_sha256'], 'phenotype predecessor')
        if journal['state'] in {'component_applied', 'head_committed'}:
            from mif_pipeline.datasets.references import freeze_component_reference
            freeze_component_reference(journal['component_descriptor'])
        if journal['state'] == 'head_committed':
            _sha256(journal['inventory_sha256'], 'inventory_sha256')
        return journal

    def _read_prepared_journal(self, prepared):
        journal = self._read_journal_file(prepared.journal_path)
        if (journal['artifact_sha256'] != prepared.artifact_sha256
                or journal['transaction_id'] != prepared.transaction_id
                or journal['predecessor_inventory_sha256'] != prepared.predecessor_inventory_sha256
                or _file_sha256(prepared.artifact_path) != prepared.artifact_sha256):
            raise PhenotypeViewStoreError('The current phenotype transaction differs from its prepared owner')
        return journal

    def mark_component_applied(self, prepared, component_reference):
        """Accept our completed native component before the inventory head advances."""
        from mif_pipeline.datasets.references import freeze_component_reference, read_component_reference
        reference = freeze_component_reference(component_reference)
        description = read_component_reference(reference)
        metadata = prepared.payload
        view = description.get('attributes', {}).get('phenotype_view', {})
        expected = {'mode': metadata['mode'], 'phenotype_generation_id': metadata['phenotype_generation_id'],
            'artifact_sha256': prepared.artifact_sha256, 'lineage_sha256': prepared.lineage_sha256,
            'cell_count': metadata['cell_count'], 'cell_ids_sha256': metadata['cell_ids_sha256'],
            'cell_to_domain_membership_sha256': metadata['cell_to_domain_membership_sha256']}
        prefix = 'phenotype_view/' + metadata['mode'] + '/' + metadata['phenotype_generation_id'] + '/'
        names = ('primary_phenotype', 'phenotype_source', 'phenotype_components', 'marker_states', 'phenotype_confidence', 'cluster_id', 'cluster_label')
        if (description.get('scope_key') != metadata['section_id']
                or description.get('request_sha256') != prepared.artifact_sha256
                or type(view.get('cell_count')) is not int
                or any(view.get(key) != value for key, value in expected.items())
                or any(prefix + name not in description['columns'] for name in names)):
            raise PhenotypeViewStoreError('The native component differs from our prepared phenotype view')
        journal = self._read_prepared_journal(prepared)
        if journal['component_descriptor'] is not None and journal['component_descriptor'] != dict(reference):
            raise PhenotypeViewStoreError('The component conflicts with our current transaction')
        if journal['state'] == 'prepared':
            journal.update(state='component_applied', component_descriptor=dict(reference))
            self._write_journal(prepared.journal_path, journal)
        source = prepared.scope_root / '.pending' / (prepared.artifact_sha256 + '.json')
        source.unlink(missing_ok=True)
        return _freeze({'artifact_sha256': prepared.artifact_sha256, 'component_descriptor': dict(reference)})

    @staticmethod
    def _contains(inventory, prepared):
        return any(section.artifact_sha256 == prepared.artifact_sha256
            for generation in inventory.views.get(prepared.payload['mode'], ())
            if generation.generation_id == prepared.payload['phenotype_generation_id']
            for section in generation.sections if section.section_id == prepared.payload['section_id'])

    def commit(self, prepared):
        """Publish our completed component selection with its current predecessor guard."""
        journal = self._read_prepared_journal(prepared)
        if journal['state'] not in {'component_applied', 'head_committed'}:
            raise PhenotypeViewStoreError('A phenotype task must complete before publishing its head')
        metadata = prepared.payload
        current = self._load_optional(**{key: metadata[key] for key in ('slide_id', 'canonical_image_sha256', 'tissue_generation_id')})
        if current and self._contains(current, prepared):
            journal.update(state='head_committed', inventory_sha256=current.inventory_sha256)
            self._write_journal(prepared.journal_path, journal)
            return current
        if (current.inventory_sha256 if current else None) != prepared.predecessor_inventory_sha256 or journal['state'] == 'head_committed':
            raise PhenotypeViewStoreError('The phenotype head differs from its current predecessor')
        views = self._serialize_views(current.views, prepared.scope_root) if current else {}
        generations = views.setdefault(metadata['mode'], [])
        generation = next((item for item in generations if item['generation_id'] == metadata['phenotype_generation_id']), None)
        if generation is None:
            generation = {'mode': metadata['mode'], 'generation_id': metadata['phenotype_generation_id'],
                'tissue_generation_id': metadata['tissue_generation_id'], 'artifact_sha256': '', 'sections': []}
            generations.append(generation)
        section = {key: metadata[key] for key in ('section_id', 'source_run_id', 'cell_count',
            'cell_ids_sha256', 'cell_to_domain_membership_sha256', 'assignments_sha256')}
        section.update(artifact_path=str(prepared.artifact_path.relative_to(prepared.scope_root)),
            artifact_sha256=prepared.artifact_sha256, lineage_sha256=prepared.lineage_sha256,
            component_descriptor=journal['component_descriptor'])
        existing = next((item for item in generation['sections'] if item['section_id'] == section['section_id']), None)
        if existing and existing != section:
            raise PhenotypeViewStoreError('An accepted phenotype section cannot change its component')
        if existing is None:
            generation['sections'].append(section)
            generation['sections'].sort(key=lambda item: item['section_id'])
        generation['artifact_sha256'] = _content_digest({key: value for key, value in generation.items() if key != 'artifact_sha256'})
        current = self._commit_inventory(prepared.scope_root, metadata, views, prepared.predecessor_inventory_sha256)
        journal.update(state='head_committed', inventory_sha256=current.inventory_sha256)
        self._write_journal(prepared.journal_path, journal)
        return current

    def _commit_inventory(self, scope, metadata, views, predecessor):
        body = {'schema_version': INVENTORY_SCHEMA,
            **{key: metadata[key] for key in ('slide_id', 'canonical_image_sha256', 'tissue_generation_id')},
            'predecessor_inventory_sha256': predecessor, 'views': views}
        revision = _content_digest(body)
        payload = {**body, 'revision_id': revision}
        path = scope / '.inventory.generations' / revision / 'descriptor.json'
        raw = _json_bytes(payload)
        if len(raw) > 16 * 1024**2:
            raise PhenotypeViewStoreError('The phenotype inventory exceeds its metadata allowance')
        if path.exists() and path.read_bytes() != raw:
            raise PhenotypeViewStoreError('A phenotype inventory generation cannot change')
        if not path.exists():
            _atomic_write(path, raw)
        sha = hashlib.sha256(raw).hexdigest()
        _atomic_write(scope / 'inventory.json', _json_bytes({'schema_version': POINTER_SCHEMA,
            'revision_id': revision, 'descriptor_path': str(path.relative_to(scope)),
            'descriptor_sha256': sha, 'predecessor_descriptor_sha256': predecessor}))
        return self._load_optional(**{key: metadata[key] for key in ('slide_id', 'canonical_image_sha256', 'tissue_generation_id')})

    def _load_optional(self, *, slide_id, canonical_image_sha256, tissue_generation_id):
        scope = self._scope_root(slide_id, canonical_image_sha256, tissue_generation_id)
        pointer_path = scope / 'inventory.json'
        if not pointer_path.exists():
            return None
        raw_pointer = pointer_path.read_bytes()
        pointer = _read_mapping(pointer_path, 'phenotype pointer')
        if set(pointer) != {'schema_version', 'revision_id', 'descriptor_path', 'descriptor_sha256', 'predecessor_descriptor_sha256'} or pointer['schema_version'] != POINTER_SCHEMA:
            raise PhenotypeViewStoreError('The current phenotype pointer is invalid')
        revision = _sha256(pointer['revision_id'], 'phenotype revision')
        if Path(pointer['descriptor_path']).parts != ('.inventory.generations', revision, 'descriptor.json'):
            raise PhenotypeViewStoreError('The phenotype inventory locator is invalid')
        path = scope / pointer['descriptor_path']
        body = _read_mapping(path, 'phenotype inventory', expected_sha256=_sha256(pointer['descriptor_sha256'], 'phenotype inventory SHA-256'))
        fields = {'schema_version', 'revision_id', 'slide_id', 'canonical_image_sha256', 'tissue_generation_id', 'predecessor_inventory_sha256', 'views'}
        if (set(body) != fields or body['schema_version'] != INVENTORY_SCHEMA or body['revision_id'] != revision
                or _content_digest({key: value for key, value in body.items() if key != 'revision_id'}) != revision
                or body['slide_id'] != slide_id or body['canonical_image_sha256'] != canonical_image_sha256
                or body['tissue_generation_id'] != tissue_generation_id
                or body['predecessor_inventory_sha256'] != pointer['predecessor_descriptor_sha256']):
            raise PhenotypeViewStoreError('The current phenotype inventory scope or revision differs')
        views = {}
        for mode, generations in body['views'].items():
            if mode not in PHENOTYPE_MODES:
                raise PhenotypeViewStoreError('The current phenotype mode is invalid')
            parsed = []
            for generation in generations:
                if (set(generation) != {'mode', 'generation_id', 'tissue_generation_id', 'artifact_sha256', 'sections'}
                        or generation['mode'] != mode or generation['tissue_generation_id'] != tissue_generation_id
                        or generation['artifact_sha256'] != _content_digest({key: value for key, value in generation.items() if key != 'artifact_sha256'})):
                    raise PhenotypeViewStoreError('A phenotype generation changed its selected identity')
                sections = tuple(self._parse_section(section, scope, mode, generation['generation_id'], tissue_generation_id) for section in generation['sections'])
                if not sections or len({section.section_id for section in sections}) != len(sections):
                    raise PhenotypeViewStoreError('A phenotype generation requires exact distinct sections')
                parsed.append(PhenotypeViewGeneration(mode, generation['generation_id'], tissue_generation_id, generation['artifact_sha256'], sections))
            if len({generation.generation_id for generation in parsed}) != len(parsed):
                raise PhenotypeViewStoreError('A phenotype inventory repeats a generation')
            views[mode] = tuple(parsed)
        return PhenotypeViewInventory(slide_id, canonical_image_sha256, tissue_generation_id,
            MappingProxyType(views), pointer['descriptor_sha256'], pointer_path, raw_pointer)

    def _parse_section(self, section, scope, mode, generation, tissue):
        fields = {'section_id', 'source_run_id', 'artifact_path', 'artifact_sha256', 'lineage_sha256',
            'cell_count', 'cell_ids_sha256', 'cell_to_domain_membership_sha256', 'assignments_sha256', 'component_descriptor'}
        if set(section) != fields or Path(section['artifact_path']).is_absolute():
            raise PhenotypeViewStoreError('A phenotype section requires its current metadata and component')
        path = (scope / section['artifact_path']).resolve()
        if scope.resolve() not in path.parents:
            raise PhenotypeViewStoreError('A phenotype section metadata locator or SHA differs')
        metadata = self._metadata(_read_mapping(path, 'phenotype metadata', expected_sha256=_sha256(section['artifact_sha256'], 'phenotype section SHA-256')))
        if metadata['mode'] != mode or metadata['phenotype_generation_id'] != generation or metadata['tissue_generation_id'] != tissue or any(metadata[key] != section[key] for key in ('section_id', 'source_run_id', 'cell_count', 'cell_ids_sha256', 'cell_to_domain_membership_sha256', 'assignments_sha256')) or _content_digest(metadata['lineage']) != section['lineage_sha256']:
            raise PhenotypeViewStoreError('A phenotype section differs from its accepted lineage')
        from mif_pipeline.datasets.references import freeze_component_reference
        reference = freeze_component_reference(section['component_descriptor'])
        return PhenotypeViewSection(section['section_id'], section['source_run_id'], path,
            section['artifact_sha256'], section['lineage_sha256'], section['cell_count'],
            section['cell_ids_sha256'], section['cell_to_domain_membership_sha256'],
            section['assignments_sha256'], reference)

    @staticmethod
    def _serialize_views(views, scope):
        return {mode: [{'mode': generation.mode, 'generation_id': generation.generation_id,
            'tissue_generation_id': generation.tissue_generation_id, 'artifact_sha256': generation.artifact_sha256,
            'sections': [{**{key: getattr(section, key) for key in ('section_id', 'source_run_id',
                'artifact_sha256', 'lineage_sha256', 'cell_count', 'cell_ids_sha256', 'cell_to_domain_membership_sha256', 'assignments_sha256')},
                'artifact_path': str(section.artifact_path.relative_to(scope)),
                'component_descriptor': dict(section.component_descriptor)} for section in generation.sections]} for generation in generations] for mode, generations in views.items()}

    def inventory_mapping(self, inventory):
        scope = self._scope_root(inventory.slide_id, inventory.canonical_image_sha256, inventory.tissue_generation_id)
        views = self._serialize_views(inventory.views, scope)
        for generations in views.values():
            for generation in generations:
                for section in generation['sections']:
                    section['artifact_path'] = str(scope / section['artifact_path'])
        return {'schema_version': INVENTORY_SCHEMA, 'slide_id': inventory.slide_id,
            'canonical_image_sha256': inventory.canonical_image_sha256, 'tissue_generation_id': inventory.tissue_generation_id,
            'inventory_sha256': inventory.inventory_sha256, 'views': views}

    def load(self, *, slide_id, canonical_image_sha256, tissue_generation_id, expected_inventory_sha256=None):
        inventory = self._load_optional(slide_id=slide_id, canonical_image_sha256=canonical_image_sha256, tissue_generation_id=tissue_generation_id)
        if inventory is None or expected_inventory_sha256 is not None and inventory.inventory_sha256 != expected_inventory_sha256:
            raise PhenotypeViewStoreError('The current phenotype inventory is absent or differs')
        scope = self._scope_root(slide_id, canonical_image_sha256, tissue_generation_id)
        for path in (scope / '.transactions').glob('*.json'):
            journal = self._read_journal_file(path)
            if journal['state'] == 'head_committed':
                metadata = self._metadata(_read_mapping(scope / journal['artifact_path'], 'phenotype metadata'))
                prepared = self._prepared(metadata, scope / journal['artifact_path'], scope, path, journal)
                if not self._contains(inventory, prepared):
                    raise PhenotypeViewStoreError('The phenotype pointer lost a committed generation')
        return inventory

    def select(self, inventory, *, mode, generation_id=None, expected_section_inventories):
        """Select our accepted view using the finalized population's compact identity.

        i) We select one current generation in the requested section scope.
        ii) We compare the owner's count and ID/domain commitments without reopening cells.
        """
        generations = inventory.views.get(mode, ())
        selected = generations[-1] if generation_id is None and generations else next((item for item in generations if item.generation_id == generation_id), None)
        if selected is None or {section.section_id for section in selected.sections} != set(expected_section_inventories):
            raise PhenotypeViewStoreError('The selected phenotype generation has a different section scope')
        for section in selected.sections:
            expected = expected_section_inventories[section.section_id]
            if any(expected.get(key) != getattr(section, key) for key in (
                    'cell_count', 'cell_ids_sha256', 'cell_to_domain_membership_sha256')):
                raise PhenotypeViewStoreError('The selected phenotype generation has a different cell/domain identity')
        return selected

    @staticmethod
    def _validate_task_artifact(value: Mapping[str, Any]) -> dict[str, Any]:
        """Validate artifact for the current operation.

        The implementation compares the supplied record identities and content digests with
        the operation context. A mismatch is reported at this boundary before the record is
        accepted by its caller."""

        if not isinstance(value, Mapping) or set(value) != _TASK_FIELDS:
            raise PhenotypeViewStoreError("phenotype view artifact fields are invalid")
        normalized = json.loads(json.dumps(value, sort_keys=True, allow_nan=False))
        if normalized.get("schema_version") != ARTIFACT_SCHEMA:
            raise PhenotypeViewStoreError("phenotype view artifact schema is invalid")
        mode = _nonblank(normalized.get("mode"), "phenotype view mode")
        if mode not in PHENOTYPE_MODES:
            raise PhenotypeViewStoreError("phenotype view mode is invalid")
        for field in (
            "slide_id",
            "tissue_generation_id",
            "phenotype_generation_id",
            "section_id",
            "source_run_id",
        ):
            _nonblank(normalized.get(field), field)
        _sha256(normalized.get("canonical_image_sha256"), "canonical_image_sha256")
        cell_ids = normalized.get("cell_ids")
        if (
            not isinstance(cell_ids, list)
            or any(not isinstance(item, str) or not item.strip() for item in cell_ids)
            or cell_ids != sorted(cell_ids)
            or len(cell_ids) != len(set(cell_ids))
        ):
            raise PhenotypeViewStoreError("phenotype artifact cell_ids are invalid")
        if normalized.get("cell_ids_sha256") != stable_cell_ids_sha256(cell_ids):
            raise PhenotypeViewStoreError("phenotype artifact cell_ids SHA-256 does not match")
        assignments = normalized.get("assignments")
        if not isinstance(assignments, list) or len(assignments) != len(cell_ids):
            raise PhenotypeViewStoreError("phenotype assignments are incomplete")
        assignment_ids: list[str] = []
        for index, assignment_value in enumerate(assignments):
            assignment = _canonical_assignment_mapping(assignment_value)
            assignments[index] = assignment
            cell_id = _nonblank(assignment.get("cell_id"), "assignment cell_id")
            assignment_ids.append(cell_id)
            _nonblank(assignment.get("primary_phenotype"), "primary_phenotype")
            _nonblank(assignment.get("phenotype_source"), "phenotype_source")
            components = assignment.get("phenotype_components")
            if (
                not isinstance(components, list)
                or any(not isinstance(item, str) or not item.strip() for item in components)
                or len(components) != len(set(components))
            ):
                raise PhenotypeViewStoreError("phenotype components are invalid")
            states = assignment.get("marker_states")
            if not isinstance(states, dict) or any(
                not isinstance(marker, str)
                or not marker.strip()
                or state not in {"positive", "negative", "unknown"}
                for marker, state in states.items()
            ):
                raise PhenotypeViewStoreError("marker states are invalid")
            confidence = assignment.get("phenotype_confidence")
            if confidence is not None and (
                isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not math.isfinite(float(confidence))
                or not 0 <= float(confidence) <= 1
            ):
                raise PhenotypeViewStoreError("phenotype confidence is invalid")
            cluster_id = assignment.get("cluster_id")
            cluster_label = assignment.get("cluster_label")
            if cluster_id is not None:
                _nonblank(cluster_id, "cluster_id")
            if cluster_label is not None:
                _nonblank(cluster_label, "cluster_label")
            if mode == "marker_rules" and (
                assignment["phenotype_source"] != "marker_rules"
                or not states
                or confidence is not None
                or cluster_id is not None
                or cluster_label is not None
            ):
                raise PhenotypeViewStoreError("marker-rule assignment evidence is invalid")
            population_excluded = assignment["phenotype_source"] == "population_excluded"
            if population_excluded and (
                mode not in {"supervised_classifier", "unsupervised_clustering"}
                or assignment["primary_phenotype"] != "Not phenotyped"
                or assignment["phenotype_components"] or states
                or confidence is not None or cluster_id is not None or cluster_label is not None
            ):
                raise PhenotypeViewStoreError("Excluded cell assignment evidence is invalid")
            if mode == "supervised_classifier" and not population_excluded and (
                assignment["phenotype_source"] != "supervised_qupath"
                or states
                or confidence is None
                or cluster_id is not None
                or cluster_label is not None
            ):
                raise PhenotypeViewStoreError("supervised assignment evidence is invalid")
            if mode == "unsupervised_clustering" and not population_excluded and (
                assignment["phenotype_source"] not in {"cluster_id", "expert_cluster_label"}
                or states
                or cluster_id is None
            ):
                raise PhenotypeViewStoreError("clustering assignment evidence is invalid")
        if assignment_ids != cell_ids:
            raise PhenotypeViewStoreError(
                "phenotype assignments must follow the exact sorted cell inventory"
            )
        if not isinstance(normalized.get("evidence"), dict) or not isinstance(
            normalized.get("lineage"), dict
        ):
            raise PhenotypeViewStoreError("phenotype evidence and lineage must be mappings")
        return normalized

__all__ = ["ARTIFACT_SCHEMA", "PHENOTYPE_MODES", "PhenotypeViewGeneration", "PhenotypeViewInventory", "PhenotypeViewSection", "PhenotypeViewStore", "PhenotypeViewStoreError", "PreparedPhenotypeView", "stable_cell_ids_sha256"]
