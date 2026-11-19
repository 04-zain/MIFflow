"""Read-only marker decisions for existing secondary-review populations."""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
import json
import math
from pathlib import Path

from mif_pipeline.cell_analysis.thresholds import (
    ThresholdFitPolicy, build_threshold_review, resolve_accepted_threshold,
    threshold_intensity_domain,
)
from .batch_phenotyping._common import canonical_bytes

_IMAGE = ('sample_id', 'project_entry_id', 'slide_id')
_PHENOTYPES = {'marker_phenotype_result', 'supervised_phenotype_result', 'cluster_label_import_result'}


@dataclass(frozen=True)
class _Section:
    authority: object
    measurement: object
    phenotype: object
    component: Mapping
    mode: str
    generation: str
    feature: object


@dataclass(frozen=True)
class _Sources:
    sections: tuple
    source_refs: tuple[str, ...]
    gates: tuple


def _identity(measurement):
    return tuple(getattr(measurement, key) for key in (*_IMAGE, 'section_id'))


def _resolve_sources(snapshot, context, measurement_id):
    """Resolve only current owners named by the pending definition context."""
    from .batch_artifacts import validate_committed_artifact_authority_set
    from .batch_measurement_artifacts import decode_committed_measurements_result
    from .batch_phenotyping.semantic_records import normalize_committed_phenotype_application
    from .batch_phenotyping.application_codec import decode_committed_cluster_label_import_result

    current = {item.artifact_ref.artifact_ref_sha256: item for item in snapshot.committed_artifact_authorities}
    references = context['source_ref_sha256s']
    if any(ref not in current for ref in references):
        raise ValueError('The secondary measurement sources changed. Reopen this review.')
    authorities = tuple(current[ref] for ref in references)
    validate_committed_artifact_authority_set(authorities)
    phenotypes = [item for item in authorities if item.artifact_ref.artifact_kind in _PHENOTYPES]
    sections, used = [], set()
    for authority in authorities:
        if authority.artifact_ref.artifact_kind != 'measurements_result':
            continue
        measured = decode_committed_measurements_result(authority)
        features = [item for item in measured.measurement_plan.features if item.measurement == measurement_id]
        if (measurement_id not in measured.selected_measurement_names or len(features) != 1
                or features[0].kind != 'expression' or not features[0].biomarker
                or measurement_id.rsplit(': ', 1)[-1] not in {'Mean', 'Median'}):
            raise ValueError('Choose a saved marker mean or median measured in every included image.')
        matches = [item for item in phenotypes if
            set(item.artifact_ref.scope.analysis_domain_bindings) == set(measured.analysis_domain_bindings)]
        if len(matches) != 1:
            raise ValueError('The saved phenotype and measurement sections no longer match.')
        phenotype = matches[0]
        if phenotype.artifact_ref.artifact_kind == 'cluster_label_import_result':
            identity = decode_committed_cluster_label_import_result(phenotype).identity
            component, mode = identity['component_descriptor'], 'unsupervised_clustering'
            generation = identity['phenotype_generation_id']
            cell_ids, membership = identity['cell_ids_sha256'], identity['cell_to_domain_membership_sha256']
        else:
            projected = normalize_committed_phenotype_application((phenotype,))
            component, mode = projected.component_descriptor, projected.phenotype_mode
            generation = projected.phenotype_generation_id
            cell_ids, membership = projected.cell_ids_sha256, projected.cell_to_domain_membership_sha256
        if (cell_ids != measured.cell_ids_sha256 or membership != measured.cell_to_domain_membership_sha256):
            raise ValueError('The saved phenotype labels belong to different measured cells.')
        sections.append(_Section(authority, measured, phenotype, component, mode, generation, features[0]))
        used.update((authority.artifact_ref.artifact_ref_sha256, phenotype.artifact_ref.artifact_ref_sha256))
    if not sections:
        raise ValueError('This secondary review has no current measured cells.')
    if len({_identity(item.measurement) for item in sections}) != len(sections):
        raise ValueError('The secondary review repeats a measured section.')
    if len({(s.measurement.slide_id, s.measurement.section_id) for s in sections}) != len(sections):
        raise ValueError('Marker decisions require distinct slide and section identities in this group.')
    semantics = {(s.feature.channel_id, s.feature.compartment, s.feature.biomarker) for s in sections}
    if len(semantics) != 1:
        raise ValueError('The selected marker measurement differs across images.')
    return _Sources(tuple(sorted(sections, key=lambda s: _identity(s.measurement))), tuple(sorted(used)),
                    tuple(item for item in authorities if item.artifact_ref.artifact_kind == 'marker_gate'))


@contextmanager
def _section_cells(section):
    """Join the exact accepted phenotype component to the measured native view."""
    from mif_pipeline.cell_analysis.measurement_rows import MarkerIntensityRowsDescriptor, read_measurement_dataset_selection
    from mif_pipeline.datasets.cell_store import CellDataset
    from mif_pipeline.datasets.references import compose_cell_dataset_selection
    from .saved_cell_figures import _MeasuredCells

    measured = section.measurement
    ref = measured.measurement_rows
    selected = read_measurement_dataset_selection(
        MarkerIntensityRowsDescriptor(ref.path, ref.sha256, ref.byte_count, ref.row_count, ref.content_schema_version),
        authenticated_output_root=ref.path.parent.parent.parent)
    joined = compose_cell_dataset_selection(section_id=measured.section_id,
        population_id=measured.segmentation_population_id, cell_dataset=selected['cell_dataset'],
        measurement_selection=selected, phenotype_component=section.component,
        phenotype_view={'mode': section.mode, 'phenotype_generation_id': section.generation}, semantic_components={})
    fields = dict(joined['components'])
    for name in ('primary_phenotype', 'phenotyping_generation_id'):
        fields[name] = {measured.section_id: section.component['path']}
    base = CellDataset(joined['cell_dataset']['path'], max_batch_rows=2048)
    if (base.summary.sha256 != joined['cell_dataset']['sha256']
            or base.description['scope']['canonical_image_sha256'] != measured.canonical_image_sha256):
        raise ValueError('The marker review cell source differs from its accepted image.')
    if joined['cell_edits'] is not None:
        base = base.with_edits(joined['cell_edits'])
    try:
        view = base.with_components(fields, geometry_revision=joined['geometry_revision'])
        component = Path(selected['accepted_component']['path'])
        if next(item for item in view._components if item.path == component).sha256 != selected['accepted_component']['sha256']:
            raise ValueError('The marker review measured component changed.')
        yield _MeasuredCells(view, component, measured, base.description['scope'])
    finally:
        if joined['cell_edits'] is not None:
            base._edit_index.close()


def _iter_rows(sources, measurement_id):
    column = 'measurement:' + measurement_id
    for section in sources.sections:
        with _section_cells(section) as cells:
            dictionary = cells.dictionaries['primary_phenotype']
            count = 0
            for batch in cells.iter_batches(columns=('cell_id', column, 'primary_phenotype', 'phenotyping_generation_id')):
                for row in batch.to_pylist():
                    count += 1
                    if row['phenotyping_generation_id'] != section.generation or row['primary_phenotype'] is None:
                        raise ValueError('The selected phenotype assignments do not cover the measured cells.')
                    label = dictionary.decode([row['primary_phenotype']])[0]
                    yield (*_identity(section.measurement), row['cell_id']), row[column], label
            if count != section.measurement.cell_count:
                raise ValueError('The marker review cell count differs from its accepted source.')


def _options(context, options):
    if not isinstance(options, Mapping):
        raise ValueError('Choose a saved marker measurement.')
    name = options.get('measurement_id')
    inventory = context['cell_selector_inventory']
    if name not in inventory['measurement_ids']:
        raise ValueError('Choose a saved measurement available in this group.')
    labels = options.get('phenotype_ids', [])
    if (not isinstance(labels, (list, tuple))
            or any(not isinstance(label, str) or label not in inventory['phenotype_ids'] for label in labels)
            or len(set(labels)) != len(labels)):
        raise ValueError('Choose populations from the accepted phenotype labels.')
    method = options.get('method') or 'manual'
    if method not in {'manual', 'triangle', 'gmm', 'gamma'}:
        raise ValueError('Choose Triangle, Gaussian, Gamma, or Manual.')
    cutoff = options.get('manual_threshold')
    if cutoff is not None and (isinstance(cutoff, bool) or not isinstance(cutoff, (int, float)) or not math.isfinite(cutoff)):
        raise ValueError('Enter a finite intensity threshold.')
    image = options.get('image_identity')
    if image is not None and (not isinstance(image, (list, tuple)) or len(image) != 3
            or any(not isinstance(item, str) or not item for item in image)):
        raise ValueError('Choose an image from this review.')
    return name, tuple(labels), method, cutoff, None if image is None else tuple(image)


def _gate(name, decisions, refs):
    return {'schema_version': 'population-marker-gate/v1', 'measurement_id': name,
            'decisions': decisions, 'source_ref_sha256s': sorted(set(refs))}


def _accepted_prefill(sources, name, sections):
    """Reuse only unambiguous exact, unrestricted, higher-intensity decisions."""
    from .batch_phenotyping.marker_application_execution import decode_committed_marker_gate
    from mif_pipeline.cell_analysis.phenotyping_population import resolve_population_selection

    choices = []
    for authority in sources.gates:
        gate = decode_committed_marker_gate(authority)
        decisions = []
        for section in sections:
            measured, feature = section.measurement, section.feature
            refs = {item['measurements_result_ref_sha256'] for item in gate.measurement_authority_bindings}
            rules = [row for row in gate.marker_settings['marker_rules'] if
                     row['marker_id'] == feature.biomarker and row['feature_id'] == feature.feature_id
                     and row['polarity'] in {'positive', 'above'}]
            if measured.artifact_ref.artifact_ref_sha256 not in refs or len(rules) != 1:
                break
            population = resolve_population_selection(gate.marker_settings.get('phenotyping_population'),
                sample_id=measured.sample_id, project_entry_id=measured.project_entry_id, slide_id=measured.slide_id)
            if population['kind'] != 'all_segmented':
                break  # A spatially restricted fit is not automatically a gate for a different population.
            accepted = next((item for item in gate.accepted_thresholds if item.marker_id == feature.biomarker), None)
            if accepted is None:
                break
            try:
                selected, _ = resolve_accepted_threshold(accepted,
                    image_id=canonical_bytes(_identity(measured)[:3]).decode(),
                    section_id=canonical_bytes(_identity(measured)).decode())
            except ValueError:
                break
            if selected.outcome not in {'threshold', 'all_positive', 'all_negative'} or selected.selection_source not in {'manual','triangle','gmm','gamma'}:
                break
            decisions.append(dict(slide_id=measured.slide_id, section_id=measured.section_id,
                threshold=selected.threshold, outcome=selected.outcome,
                intensity_domain=threshold_intensity_domain(selected), method=selected.selection_source))
        if len(decisions) == len(sections):
            choices.append(_gate(name, decisions, (*sources.source_refs, authority.artifact_ref.artifact_ref_sha256)))
    return choices[0] if len(choices) == 1 else None


@contextmanager
def _review_resources(snapshot, sources):
    from .saved_cell_gallery import gallery_resources
    # Full-scope identities, finite arrays, exact CDF support and NumPy fitting
    # temporaries are admitted together before any cell-sized allocation.
    memory_bytes = 128 * 1024**2 + sum(s.measurement.cell_count for s in sources.sections) * 2048
    with gallery_resources(snapshot, sources.sections[0].authority, memory_bytes=memory_bytes):
        yield


def prepare_secondary_marker_review(snapshot, definition_context, options, dependencies=None):
    """Return distributions and gate proposals; acceptance remains in the profile editor."""
    name, labels, method, manual, image = _options(definition_context, options)
    sources = _resolve_sources(snapshot, definition_context, name)
    existing = options.get('existing_gate')
    if existing is not None:
        from mif_pipeline.roi_validation.population_marker import parse_population_marker_gate
        existing = parse_population_marker_gate(existing, name)
        if (not set(sources.source_refs) <= set(existing['source_ref_sha256s'])
                or not set(existing['source_ref_sha256s']) <= set(definition_context['source_ref_sha256s'])):
            raise ValueError('The saved marker decision belongs to different measurement or phenotype sources.')
        available = {(_identity(s.measurement)[2], _identity(s.measurement)[3]) for s in sources.sections}
        decisions = {(d['slide_id'], d['section_id']): d for d in existing['decisions']}
        if not set(decisions) <= available:
            raise ValueError('The saved marker decision contains a different image or section.')
    if options.get('prefill_only') is True:
        sections = tuple(s for s in sources.sections if image is None or _identity(s.measurement)[:3] == image)
        if not sections:
            raise ValueError('Choose an image in this secondary measurement review.')
        section_choices, images = _image_choices(sources)
        result = dict(measurement_id=name, phenotype_ids=list(labels), image_identity=None if image is None else list(image),
            method=method, review=None, gate=None, section_choices=section_choices, images=images,
            accepted_prefill=_accepted_prefill(sources, name, sections), source_ref_sha256s=list(sources.source_refs))
    else:
        resource_sources = _Sources(tuple(s for s in sources.sections if
            image is None or _identity(s.measurement)[:3] == image), sources.source_refs, sources.gates)
        if not resource_sources.sections:
            raise ValueError('Choose an image in this secondary measurement review.')
        with _review_resources(snapshot, resource_sources):
            result = _prepare_review(sources, name, labels, method, manual, image, existing)
    if existing is not None:
        generated = result['gate']
        if generated is not None:
            decisions.update(((d['slide_id'], d['section_id']), d) for d in generated['decisions'])
        if generated is not None:
            result['gate'] = _gate(name, [decisions[key] for key in sorted(decisions)], existing['source_ref_sha256s'])
    expected = {(s.measurement.slide_id, s.measurement.section_id) for s in sources.sections}
    result['gate_complete'] = (result['gate'] is not None and
        {(d['slide_id'], d['section_id']) for d in result['gate']['decisions']} == expected)
    result['retained_gate'] = existing
    result['retained_gate_complete'] = (existing is not None and
        {(d['slide_id'], d['section_id']) for d in existing['decisions']} == expected)
    return result


def _image_choices(sources):
    sections = [dict(zip((*_IMAGE, 'section_id'), _identity(s.measurement)),
        image_identity=list(_identity(s.measurement)[:3]),
        label=f'{s.measurement.sample_id} / {s.measurement.slide_id} / {s.measurement.section_id}') for s in sources.sections]
    images = {tuple(row['image_identity']): dict(image_identity=row['image_identity'],
        **{key: row[key] for key in _IMAGE}, label=f"{row['sample_id']} / {row['slide_id']}") for row in sections}
    return sections, list(images.values())


def _prepare_review(sources, name, labels, method, manual, image, existing=None):
    from .batch_phenotyping.marker_execution import _candidate_projection, _histogram_projection
    sections = tuple(s for s in sources.sections if image is None or _identity(s.measurement)[:3] == image)
    if not sections:
        raise ValueError('Choose an image in this secondary measurement review.')
    identities, values, scopes = [], [], []
    excluded_population = 0
    scoped = _Sources(sections, sources.source_refs, sources.gates)
    for identity, value, label in _iter_rows(scoped, name):
        if image is not None and identity[:3] != image:
            continue
        if labels and label not in labels:
            excluded_population += 1
            continue
        identities.append(canonical_bytes(identity).decode())
        scopes.append(canonical_bytes(identity[:4]).decode())
        values.append(float(value) if value is not None else math.nan)
    review = build_threshold_review(values, identities, scopes,
        [sections[0].feature.compartment] * len(values), ThresholdFitPolicy(),
        methods=() if method == 'manual' else (method,), manual_threshold=manual if method == 'manual' else None)
    candidate = review.candidate(method)
    frequencies = Counter(value for value in values if math.isfinite(value))
    def projected_candidate(item):
        projected = dict(_candidate_projection(item))
        if item.status == 'available':
            import numpy as np
            from mif_pipeline.cell_analysis.marker_rules import ThresholdRule, threshold_states
            domain = 'nonnegative' if item.method == 'gamma' else 'finite'
            states = threshold_states(np.asarray(values), ThresholdRule(
                biomarker=name, measurement=name, compartment=sections[0].feature.compartment,
                polarity='above', cutoff=item.threshold, confirmed=True, outcome=item.outcome,
                intensity_domain=domain))
            positive, negative = int(np.count_nonzero(states == 1)), int(np.count_nonzero(states == 0))
            total = positive + negative
            projected['counts'] = dict(positive=positive, negative=negative, total=total,
                positive_fraction=positive / total if total else 0., unknown=len(values)-total)
        return projected
    manual_projection = projected_candidate(review.manual)
    candidate_projections = [projected_candidate(item) for item in review.candidates]
    selected_projection = manual_projection if method == 'manual' else next(p for p in candidate_projections if p['method'] == method)
    selected_counts = selected_projection.get('counts')
    known_count = selected_counts['total'] if selected_counts is not None else review.eligible_count
    unknown_count = selected_counts['unknown'] if selected_counts is not None else review.unknown_count
    projection = dict(histogram=dict(_histogram_projection(review.histogram)),
        empirical_cdf={'values': sorted(frequencies), 'counts': [frequencies[v] for v in sorted(frequencies)]},
        manual=manual_projection, candidates=candidate_projections,
        eligible_count=known_count, unknown_count=unknown_count, finite_count=review.eligible_count,
        excluded_count=unknown_count, excluded_population_count=excluded_population)
    decisions = [dict(slide_id=s.measurement.slide_id, section_id=s.measurement.section_id,
        threshold=candidate.threshold, outcome=candidate.outcome,
        intensity_domain='nonnegative' if method == 'gamma' else 'finite', method=method) for s in sections]
    gate = (_gate(name, decisions, sources.source_refs)
            if candidate.status == 'available' and known_count else None)
    section_choices, images = _image_choices(sources)
    prefill = _accepted_prefill(sources, name, sections)
    retained = existing or prefill
    if retained is not None:
        projection['retained_decisions'] = [d for d in retained['decisions'] if
            (d['slide_id'], d['section_id']) in {(s.measurement.slide_id, s.measurement.section_id) for s in sections}]
        projection['retained_counts'] = _decision_counts(retained, values, scopes)
    return dict(measurement_id=name, phenotype_ids=list(labels), image_identity=None if image is None else list(image),
        method=method, review=projection, section_choices=section_choices, images=images, gate=gate,
        accepted_prefill=prefill, source_ref_sha256s=list(sources.source_refs))


def _decision_counts(gate, values, scopes):
    """Count retained per-section decisions with the same unknown domain as evaluation."""
    import numpy as np
    from mif_pipeline.cell_analysis.marker_rules import threshold_states
    from mif_pipeline.roi_validation.population_marker import marker_rule_for_section
    covered = {(d['slide_id'], d['section_id']) for d in gate['decisions']}
    grouped = {}
    for value, scope in zip(values, scopes, strict=True):
        grouped.setdefault(tuple(json.loads(scope)[2:4]), []).append(value)
    positive = negative = unknown = 0
    for scope, numbers in grouped.items():
        if scope not in covered:
            unknown += len(numbers)
            continue
        states = threshold_states(np.asarray(numbers), marker_rule_for_section(gate, *scope))
        positive += int(np.count_nonzero(states == 1))
        negative += int(np.count_nonzero(states == 0))
        unknown += int(np.count_nonzero(states == -1))
    return dict(positive=positive, negative=negative, total=positive + negative,
                unknown=unknown, excluded_count=unknown, population_count=len(values))


class _PopulationCells:
    """Filter saved phenotype codes while retaining bounded tissue queries."""
    def __init__(self, cells, labels, generation):
        self.cells, self.labels, self.generation = cells, set(labels), generation
        self.schema, self.scope, self.dictionaries = cells.schema, cells.scope, cells.dictionaries

    def _batches(self, method, columns, *args, **kwargs):
        import pyarrow as pa
        fields = tuple(dict.fromkeys((*columns, 'primary_phenotype', 'phenotyping_generation_id')))
        dictionary = self.dictionaries['primary_phenotype']
        for batch in getattr(self.cells, method)(*args, columns=fields, **kwargs):
            if any(value != self.generation for value in batch.column('phenotyping_generation_id').to_pylist()):
                raise ValueError('The tissue view phenotype generation changed.')
            codes = batch.column('primary_phenotype').to_pylist()
            if any(value is None for value in codes):
                raise ValueError('The tissue view lacks accepted phenotype assignments.')
            decoded = dictionary.decode(codes)
            if self.labels:
                batch = batch.filter(pa.array([label in self.labels for label in decoded]))
            if batch.num_rows:
                yield batch.select(columns)

    def iter_batches(self, *, columns):
        yield from self._batches('iter_batches', columns)

    def query(self, bounds, *, columns, predicate='intersects'):
        yield from self._batches('query', columns, bounds, predicate=predicate)


def prepare_secondary_marker_field(provider, snapshot, definition_context, options, dependencies=None):
    """Reuse the bounded intensity viewer with the selected saved population."""
    from .measurement_spatial_review import MeasurementFieldReader, _ImageMeasurementCells
    from .saved_cell_gallery import gallery_resources

    name, labels, _method, _manual, image = _options(definition_context, options)
    sources = _resolve_sources(snapshot, definition_context, name)
    sections = tuple(section for section in sources.sections if _identity(section.measurement)[:3] == image)
    if not sections:
        raise ValueError('Choose an image before inspecting marker intensity in tissue.')
    if len({(s.measurement.canonical_image_id, s.measurement.canonical_image_sha256) for s in sections}) != 1:
        raise ValueError('The selected sections do not share one measured image.')

    class SecondaryMarkerFieldReader(MeasurementFieldReader):
        @contextmanager
        def _selected_cells(self, authority):
            with ExitStack() as stack:
                yield _ImageMeasurementCells([(_identity(section.measurement),
                    _PopulationCells(stack.enter_context(_section_cells(section)), labels, section.generation))
                    for section in sections])

    authority = sections[0].authority
    key = ('secondary-marker', snapshot.head.sha256, sources.source_refs, image, name, labels)
    with gallery_resources(snapshot, authority):
        previous = provider._measurement_field
        if previous is None or previous[0] != key:
            provider._measurement_field = None
            if previous is not None:
                previous[1].close()
            reader = SecondaryMarkerFieldReader(authority, snapshot)
            provider._measurement_field = (key, reader)
        else:
            reader = previous[1]
        display = {**options, 'measurement': name}
        if not display.get('channels'):
            display['channels'] = [next(index for index, channel in enumerate(sections[0].measurement.canonical_channels)
                if channel['channel_id'] == sections[0].feature.channel_id)]
        result = reader.read(authority, snapshot, display)
        result['metadata'].update(available_channels=[dict(index=i, label=c['label']) for i,c in enumerate(reader.image.channels)],
            image_uri=reader.image.path.as_uri(), population_label=', '.join(labels) if labels else 'all measured cells',
            phenotype_ids=list(labels), measurement_id=name)
        return result
