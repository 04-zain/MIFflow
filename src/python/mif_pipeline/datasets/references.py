"""Read the compact identity of a completed cell dataset without decoding cells."""
from collections.abc import Mapping
import hashlib
import json
from pathlib import Path

from .cell_store import CELL_DATASET_SCHEMA_VERSION


def read_cell_dataset_reference(reference: Mapping) -> dict:
    """Read the selected base descriptor and confirm its saved identity.

    We compare the descriptor bytes with the supplied digest, then confirm the
    schema, row count and source identity. The returned mapping contains layout
    and population summaries; this boundary does not decode Parquet rows. A
    changed descriptor or an oversized metadata file cannot authorize a stage.
    """
    if not isinstance(reference, Mapping) or set(reference) != {
        "path", "sha256", "schema_version", "row_count", "source_sha256"
    }:
        raise ValueError("A cell dataset reference requires its completed identity")
    path = Path(reference["path"])
    if not path.is_absolute() or path.name != "dataset.json":
        raise ValueError("A cell dataset reference requires an absolute descriptor path")
    if path.stat().st_size > 64 * 1024**2:
        raise ValueError("Cell dataset metadata exceeds its allowance")
    payload = path.read_bytes()
    description = json.loads(payload)
    if (hashlib.sha256(payload).hexdigest() != reference["sha256"]
            or reference["schema_version"] != CELL_DATASET_SCHEMA_VERSION
            or any(description.get(key) != reference[key]
                   for key in ("schema_version", "row_count", "source_sha256"))):
        raise ValueError("The completed cell dataset differs from its selected identity")
    return description


def read_membership_component_reference(reference: Mapping, *, domain_id: str) -> dict:
    """Read a completed secondary-membership descriptor for one domain.

    We confirm the selected bytes, domain and Int32 membership-list definition
    before returning the component metadata. The consuming cell reader resolves
    its dictionaries and aligned rows. Incomplete or differently scoped results
    cannot supply memberships for this domain.
    """
    if not isinstance(reference, Mapping) or set(reference) != {"path", "sha256"}:
        raise ValueError("A membership component requires its descriptor and digest")
    path = Path(reference["path"])
    if not path.is_absolute() or path.stat().st_size > 64 * 1024**2:
        raise ValueError("The membership component descriptor is outside its allowance")
    payload = path.read_bytes()
    description = json.loads(payload)
    if (hashlib.sha256(payload).hexdigest() != reference["sha256"]
            or description.get("schema_version") != "mif-cell-component/v1"
            or description.get("complete") is not True
            or description.get("scope_key") != domain_id
            or description.get("columns", {}).get("secondary_roi_ids") != "list_int32"):
        raise ValueError("The completed membership component differs from its selected identity")
    return description


def freeze_component_reference(reference: Mapping) -> Mapping:
    """Freeze our current compact result reference at its decoding boundary."""
    from types import MappingProxyType
    if not isinstance(reference, Mapping) or set(reference) != {'path', 'sha256'}:
        raise ValueError('A component reference requires its path and SHA-256')
    path, digest = reference['path'], reference['sha256']
    if (not isinstance(path, str) or not Path(path).is_absolute()
            or not isinstance(digest, str) or len(digest) != 64
            or any(character not in '0123456789abcdef' for character in digest)):
        raise ValueError('A component reference requires an absolute path and SHA-256')
    return MappingProxyType({'path': path, 'sha256': digest})


def read_component_reference(reference: Mapping) -> dict:
    """Read our completed result metadata without visiting scientific rows.

    We check the selected descriptor bytes and completion state once where a
    consumer chooses that result. Scope, population and shape revision remain
    explicit inputs to the selection join below.
    """
    reference = freeze_component_reference(reference)
    path = Path(reference['path'])
    before = path.stat()
    if before.st_size > 64 * 1024**2:
        raise ValueError('Cell component metadata exceeds its allowance')
    payload = path.read_bytes()
    after = path.stat()
    description = json.loads(payload)
    if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or hashlib.sha256(payload).hexdigest() != reference['sha256']
            or description.get('schema_version') != 'mif-cell-component/v1'
            or description.get('complete') is not True
            or not isinstance(description.get('columns'), dict)):
        raise ValueError('The completed component differs from its selected identity')
    return description


def compose_cell_dataset_selection(*, section_id: str, population_id: str,
        cell_dataset: Mapping, measurement_selection: Mapping,
        phenotype_component: Mapping, phenotype_view: Mapping,
        semantic_components: Mapping) -> dict:
    """Join our selected current results onto one immutable section population.

    i) We preserve the accepted base and measurement selection, including its
    current sparse shape checkpoint. ii) We add only the chosen phenotype and
    finalized domain components after comparing their population, layout and
    shape revision. This join reads compact descriptors and never rewrites cells.
    """
    base_ref = freeze_component_reference({key: cell_dataset[key] for key in ('path', 'sha256')})
    if dict(base_ref) != dict(measurement_selection['cell_dataset']):
        raise ValueError('The measurement selection belongs to a different accepted base')
    base_path = Path(base_ref['path'])
    if base_path.stat().st_size > 64 * 1024**2:
        raise ValueError('Cell dataset metadata exceeds its allowance')
    payload = base_path.read_bytes()
    base = json.loads(payload)
    if (hashlib.sha256(payload).hexdigest() != base_ref['sha256']
            or base.get('schema_version') != CELL_DATASET_SCHEMA_VERSION
            or base['scope']['population_id'] != population_id
            or base['scope'].get('section_id') != section_id):
        raise ValueError('The accepted section population differs from its selected base')
    revision = measurement_selection['geometry_revision']
    if type(revision) is not int or revision < 0:
        raise ValueError('A current selection requires its geometry revision')
    selected_view = dict(phenotype_view)
    if set(selected_view) != {'mode', 'phenotype_generation_id'}:
        raise ValueError('Select the exact current phenotype mode and generation')
    fields = {field: dict(scopes) for field, scopes in measurement_selection['components'].items()}

    def merge(reference, scope, *, phenotype=False):
        description = read_component_reference(reference)
        if (description['population_id'] != population_id
                or description['layout_version'] != base['layout_version']
                or description['geometry_revision'] != revision
                or description['scope_key'] != scope):
            raise ValueError('The selected component differs from the current section or geometry')
        columns = description['columns']
        if phenotype:
            view = description.get('attributes', {}).get('phenotype_view', {})
            prefix = 'phenotype_view/' + selected_view['mode'] + '/' + selected_view['phenotype_generation_id'] + '/'
            names = {'primary_phenotype', 'phenotype_source', 'phenotype_components', 'marker_states', 'phenotype_confidence', 'cluster_id', 'cluster_label'}
            if (any(view.get(key) != value for key, value in selected_view.items())
                    or not {prefix + name for name in names}.issubset(columns)):
                raise ValueError('The selected component omits the chosen phenotype view')
            columns = {name: value for name, value in columns.items() if name.startswith(prefix)}
        else:
            names = {'secondary_roi_ids', 'membership_run_id', 'membership_generation_id'}
            if not names.issubset(columns):
                raise ValueError('The finalized domain component omits its membership fields')
            columns = {name: columns[name] for name in names}
        for field in columns:
            fields.setdefault(field, {})[scope] = reference['path']

    merge(phenotype_component, section_id, phenotype=True)
    for domain_id, reference in semantic_components.items():
        merge(reference, domain_id)
    return {'section_id': section_id, 'population_id': population_id,
            'cell_dataset': dict(base_ref), 'components': fields,
            'geometry_revision': revision, 'cell_edits': measurement_selection['cell_edits'],
            'phenotype_view': selected_view}
