"""Bounded, read-only assignment feedback from accepted gates and measured cells."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
from math import isfinite
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

import numpy as np

from mif_pipeline.cell_analysis.marker_rules import (
    MarkerCellValues, MeasurementSemantic, ThresholdRule, apply_threshold_rules,
    apply_ordered_composites, _matrix_from_cells, _rule_match,
)
from mif_pipeline.cell_analysis.thresholds import resolve_accepted_threshold, threshold_intensity_domain
from mif_pipeline.cell_analysis.measurement_rows import (
    MarkerIntensityRowsDescriptor, MeasurementRowStreamExpectation, iter_selected_measurement_batches,
)
from mif_pipeline.cell_analysis.phenotyping_population import resolve_population_selection
from ._common import canonical_bytes, digest
from .marker_application_execution import _composites, _section_identity, decode_committed_marker_gate
from .marker_combinations import validate_combination_response
from .marker_settings import marker_feature_candidates, configured_marker_catalog, validate_marker_settings


def _retain(examples, identities):
    """Keep three deterministic full identities, independent of batch order."""
    examples[:] = sorted(set(examples).union(identities))[:3]


PATTERN_PAGE_SIZE = 128


def _pattern_row(pattern, count, phenotype, examples):
    return dict(pattern_id=pattern, states=[{'u':'unknown','0':'negative','1':'positive'}[s] for s in pattern],
                count=count, assignments=[dict(phenotype=phenotype,count=count)], examples=json.loads(examples))


def _pattern_projection(connection, where='1', parameters=(), *, offset=0, image_identity=None):
    source = 'patterns'
    if image_identity is not None:
        # Keep the stored assignment, but count only this exact image's sections.
        source = ('(SELECT p.pattern,c.count,p.phenotype,p.examples FROM patterns p JOIN '
            '(SELECT pattern,count(*) AS count FROM cells WHERE '
            "json_extract(scope,'$[0]')=? AND json_extract(scope,'$[1]')=? AND "
            "json_extract(scope,'$[2]')=? GROUP BY pattern) c ON c.pattern=p.pattern)")
        parameters = (*image_identity, *parameters)
    total_patterns, selected, unknown = connection.execute(
        f"SELECT count(*), coalesce(sum(count),0), coalesce(sum(CASE WHEN instr(pattern,'u')>0 THEN count ELSE 0 END),0) FROM {source} WHERE {where}",
        parameters).fetchone()
    rows = [_pattern_row(*row) for row in connection.execute(
        f'SELECT pattern,count,phenotype,examples FROM {source} WHERE {where} ORDER BY count DESC,pattern LIMIT ? OFFSET ?',
        (*parameters, PATTERN_PAGE_SIZE, offset))]
    if image_identity is not None:
        for row in rows:
            row['examples'] = [list((*json.loads(scope), cell_id)) for scope,cell_id in connection.execute(
                "SELECT scope,cell_id FROM cells WHERE pattern=? AND json_extract(scope,'$[0]')=? "
                "AND json_extract(scope,'$[1]')=? AND json_extract(scope,'$[2]')=? ORDER BY scope,cell_id LIMIT 3",
                (row['pattern_id'], *image_identity))]
    return dict(rows=rows, selected_cells=selected, known_cells=selected-unknown, unknown_cells=unknown,
                observed_pattern_count=total_patterns, displayed_pattern_count=len(rows), offset=offset,
                omitted_pattern_count=total_patterns-len(rows), omitted_cell_count=selected-sum(r['count'] for r in rows))


def preview_pattern_page(path, markers, definition, selection):
    """Read one observed-pattern page from the current definition's population cache."""
    if (not isinstance(selection, dict)
            or set(selection) not in ({'population','phenotype','offset'},
                                      {'population','phenotype','offset','image_identity'})):
        raise ValueError('Choose an observed-pattern population and page.')
    population, phenotype, offset = (selection[k] for k in ('population','phenotype','offset'))
    if (type(population) is not str or (phenotype is not None and type(phenotype) is not str)
            or type(offset) is not int or offset < 0 or offset % PATTERN_PAGE_SIZE):
        raise ValueError('Choose an available marker-pattern page.')
    rules, residual, _ = _composites(definition, tuple(markers))
    names = {rule.phenotype for rule in rules}
    where, parameters = '1', ()
    if population == 'all' and phenotype is None:
        pass
    elif population == 'unmatched' and phenotype is None:
        where, parameters = 'phenotype=?', (residual,)
    elif population == 'assigned' and phenotype in names | {residual}:
        where, parameters = 'phenotype=?', (phenotype,)
    elif population == 'matching' and phenotype in names:
        rule = next(rule for rule in rules if rule.phenotype == phenotype)
        required = [(markers.index(marker)+1, '1' if state == 'positive' else '0')
                    for marker,state in rule.marker_states.items() if state != 'ignore']
        where = ' AND '.join('substr(pattern,?,1)=?' for _ in required) or '1'
        parameters = tuple(value for pair in required for value in pair)
    else:
        raise ValueError('Choose a current phenotype or marker-pattern population.')
    with sqlite3.connect(path) as connection:
        image = selection.get('image_identity')
        if image is not None and tuple(image) not in {
                tuple(row[k] for k in ('sample_id','project_entry_id','slide_id'))
                for row in assignment_image_counts(path, ('all', None))}:
            raise ValueError('Choose an image in the included phenotype population.')
        projection = _pattern_projection(connection, where, parameters, offset=offset, image_identity=image)
    if offset and offset >= projection['observed_pattern_count']:
        raise ValueError('The marker-pattern page is outside this population.')
    return dict(pattern_page=dict(selection), markers=list(markers), projection=projection,
                definition_sha256=digest(definition))


def pattern_from_cache(path, pattern_id):
    """Resolve exact examples for any observed pattern, independent of its page."""
    with sqlite3.connect(path) as connection:
        row = connection.execute('SELECT pattern,count,phenotype,examples FROM patterns WHERE pattern=?',
                                 (pattern_id,)).fetchone()
    if row is None:
        raise ValueError('Choose a pattern observed in the current assignment population.')
    return _pattern_row(*row)


def assignment_image_counts(path, selection):
    """Exact included/matching counts in recorded analysis order, including zeroes."""
    kind, value = selection
    if kind not in {'all', 'pattern', 'phenotype'}:
        raise ValueError('Choose a phenotype or observed marker pattern.')
    expression = '1' if kind == 'all' else 'c.pattern=?' if kind == 'pattern' else 'p.phenotype=?'
    parameters = () if kind == 'all' else (value,)
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            f'SELECT s.scope,count(c.cell_id),coalesce(sum(CASE WHEN {expression} THEN 1 ELSE 0 END),0) '
            'FROM image_scopes s LEFT JOIN cells c ON c.scope=s.scope '
            'LEFT JOIN patterns p ON p.pattern=c.pattern GROUP BY s.scope ORDER BY s.position', parameters)
        images = {}
        for scope, included, matching in rows:
            identity = tuple(json.loads(scope)[:3])
            row = images.setdefault(identity, dict(zip(('sample_id','project_entry_id','slide_id'), identity),
                                                   included_cells=0, matching_cells=0))
            row['included_cells'] += included
            row['matching_cells'] += min(included, matching)
    return list(images.values())


class AssignmentCounts:
    """Aggregate the existing evaluator's diagnostics, never retain its cell table."""
    def __init__(self, definition, markers, *, pattern_path=None):
        self.definition, self.markers = definition, tuple(markers)
        self.rules, self.residual, _ = _composites(definition, self.markers)
        self.selected = self.unmatched = 0
        self.unmatched_examples = []
        self.example_assignments = {}
        # An empty SQLite filename creates a private temporary database, deleted
        # on close. Store observed vectors and scoped membership; never enumerate panels.
        self._patterns = sqlite3.connect(str(pattern_path) if pattern_path is not None else '')
        self._patterns.execute('PRAGMA cache_size=-2048')
        self._patterns.execute('CREATE TABLE IF NOT EXISTS patterns (pattern TEXT PRIMARY KEY, count INTEGER, phenotype TEXT, examples TEXT, representative TEXT) WITHOUT ROWID')
        self._patterns.execute('CREATE TABLE IF NOT EXISTS cells (scope TEXT, cell_id TEXT, pattern TEXT, PRIMARY KEY(scope,cell_id)) WITHOUT ROWID')
        self._patterns.execute('CREATE TABLE IF NOT EXISTS image_scopes (scope TEXT PRIMARY KEY, position INTEGER) WITHOUT ROWID')
        self.rows = [dict(phenotype=r.phenotype, precedence=r.precedence,
                         matched=0, newly_assigned=0, overlap_with_prior=0, unknown_required=0,
                         examples=dict(assigned=[], overlap=[], unknown=[])) for r in self.rules]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            if exc[0] is None:
                self._patterns.commit()
        finally:
            self._patterns.close()

    def _add_patterns(self, matrix, identities, assigned_cells):
        grouped = {}
        indexed = []
        for states, identity, cell in zip(matrix, identities, assigned_cells, strict=True):
            pattern = ''.join('u' if state == -1 else str(state) for state in states)
            indexed.append((canonical_bytes(identity[:4]).decode(), identity[4], pattern))
            if pattern not in grouped:
                grouped[pattern] = [0, cell.primary_phenotype, [], (identity, cell.analysis_domain_id)]
            row = grouped[pattern]
            row[0] += 1
            _retain(row[2], (identity,))
        self._patterns.executemany('INSERT INTO cells VALUES (?,?,?)', indexed)
        for pattern, (count, phenotype, examples, representative) in grouped.items():
            prior = self._patterns.execute('SELECT count, examples FROM patterns WHERE pattern=?', (pattern,)).fetchone()
            if prior is not None:
                count += prior[0]
                _retain(examples, (tuple(i) for i in json.loads(prior[1])))
            self._patterns.execute('INSERT OR REPLACE INTO patterns VALUES (?,?,?,?,?)',
                                   (pattern, count, phenotype, json.dumps(examples), json.dumps(representative)))

    def _pattern_projection(self, where='1', parameters=()):
        return _pattern_projection(self._patterns, where, parameters)

    def _observed_patterns(self):
        by_phenotype = {}
        for rule in self.rules:
            required = [(self.markers.index(marker)+1, '1' if state=='positive' else '0')
                        for marker,state in rule.marker_states.items() if state!='ignore']
            where = ' AND '.join('substr(pattern,?,1)=?' for _ in required) or '1'
            by_phenotype[rule.phenotype] = self._pattern_projection(where, tuple(v for pair in required for v in pair))
        return dict(markers=list(self.markers), **self._pattern_projection(), by_phenotype=by_phenotype,
                    assigned_by_phenotype={name:self._pattern_projection('phenotype=?', (name,))
                                           for name in (*by_phenotype, self.residual)},
                    unmatched=self._pattern_projection('phenotype=?', (self.residual,)))

    def add(self, cells, scope):
        self._patterns.execute('INSERT OR IGNORE INTO image_scopes VALUES (?,(SELECT count(*) FROM image_scopes))',
                               (canonical_bytes(scope).decode(),))
        if not cells:
            return
        matrix = _matrix_from_cells(cells, self.markers)
        result = apply_ordered_composites(cells, self.rules, residual=self.residual,
                                          state_matrix=matrix, marker_order=self.markers)
        indices = dict(zip(self.markers, range(len(self.markers))))
        available = np.ones(len(cells), dtype=bool)
        identities = [(*scope, c.cell_id) for c in cells]
        self._add_patterns(matrix, identities, result.cells)
        for rule, diagnostic, row in zip(self.rules, result.diagnostics, self.rows, strict=True):
            match = _rule_match(matrix, indices, rule)
            required = [indices[m] for m, state in rule.marker_states.items() if state != 'ignore']
            unknown = np.any(matrix[:, required] == -1, axis=1)
            for name in ('matched', 'newly_assigned', 'overlap_with_prior'):
                row[name] += getattr(diagnostic, name)
            row['unknown_required'] += int(np.count_nonzero(unknown))
            for name, mask in (('assigned', match & available), ('overlap', match & ~available), ('unknown', unknown)):
                _retain(row['examples'][name], (identities[i] for i in np.flatnonzero(mask)))
            available &= ~match
        self.selected += len(cells)
        self.unmatched += int(np.count_nonzero(available))
        _retain(self.unmatched_examples, (identities[i] for i in np.flatnonzero(available)))
        retained = set(self.unmatched_examples)
        retained.update(identity for row in self.rows for examples in row['examples'].values()
                        for identity in examples)
        self.example_assignments = {identity: label for identity, label in self.example_assignments.items()
                                    if identity in retained}
        self.example_assignments.update((identity, cell.primary_phenotype)
            for identity, cell in zip(identities, result.cells, strict=True) if identity in retained)

    def reassign(self):
        """Evaluate every retained pattern, weighted by its actual cell count."""
        last = ''
        indices = dict(zip(self.markers, range(len(self.markers))))
        while batch := self._patterns.execute(
                'SELECT pattern,count,examples,representative FROM patterns WHERE pattern>? ORDER BY pattern LIMIT 512',
                (last,)).fetchall():
            last = batch[-1][0]
            # Use real representative cell identities within their original image
            # scopes; counts and examples retain all scopes sharing the pattern.
            by_scope = {}
            for pattern, count, examples, representative in batch:
                identity, domain = json.loads(representative)
                by_scope.setdefault(tuple(identity[:4]), []).append(
                    (pattern, count, [tuple(i) for i in json.loads(examples)], identity, domain))
            for scope, values in by_scope.items():
                matrix = np.array([[{'u':-1,'0':0,'1':1}[s] for s in row[0]] for row in values], dtype=np.int8)
                weights = np.array([row[1] for row in values], dtype=np.int64)
                cells = tuple(MarkerCellValues(row[3][-1],scope[2],scope[3],row[4],{}, {}) for row in values)
                assigned = apply_ordered_composites(cells,self.rules,residual=self.residual,
                    state_matrix=matrix,marker_order=self.markers).cells
                available = np.ones(len(values),dtype=bool)
                for rule, row in zip(self.rules,self.rows,strict=True):
                    match = _rule_match(matrix,indices,rule)
                    required = [indices[m] for m,state in rule.marker_states.items() if state!='ignore']
                    unknown = np.any(matrix[:,required]==-1,axis=1)
                    row['matched'] += int(weights[match].sum())
                    row['newly_assigned'] += int(weights[match & available].sum())
                    row['overlap_with_prior'] += int(weights[match & ~available].sum())
                    row['unknown_required'] += int(weights[unknown].sum())
                    for name,mask in (('assigned',match & available),('overlap',match & ~available),('unknown',unknown)):
                        _retain(row['examples'][name], (identity for i in np.flatnonzero(mask) for identity in values[i][2]))
                    available &= ~match
                self.selected += int(weights.sum())
                self.unmatched += int(weights[available].sum())
                _retain(self.unmatched_examples,(identity for i in np.flatnonzero(available) for identity in values[i][2]))
                retained = set(self.unmatched_examples)
                retained.update(identity for row in self.rows for examples in row['examples'].values() for identity in examples)
                self.example_assignments = {identity:label for identity,label in self.example_assignments.items() if identity in retained}
                for row,cell in zip(values,assigned,strict=True):
                    self._patterns.execute('UPDATE patterns SET phenotype=? WHERE pattern=?',(cell.primary_phenotype,row[0]))
                    self.example_assignments.update((identity,cell.primary_phenotype) for identity in row[2] if identity in retained)

    def result(self, *, total):
        patterns = self._observed_patterns()
        images = {}
        for scope, count in self._patterns.execute('SELECT s.scope,count(c.cell_id) FROM image_scopes s '
                'LEFT JOIN cells c ON c.scope=s.scope GROUP BY s.scope ORDER BY s.position'):
            identity = tuple(json.loads(scope)[:3])
            images[identity] = images.get(identity, 0) + count
        assignments = dict(self.example_assignments)
        for projection in (patterns, *patterns['by_phenotype'].values(), patterns['unmatched']):
            for row in projection['rows']:
                assignments.update((tuple(identity),row['assignments'][0]['phenotype']) for identity in row['examples'])
        return dict(definition_sha256=digest(self.definition), total=total, selected=self.selected,
                    excluded=total-self.selected, unmatched=self.unmatched,
                    residual_phenotype=self.residual,
                    rows=[{**r, 'examples': {k:[list(i) for i in v] for k,v in r['examples'].items()}} for r in self.rows],
                    unmatched_examples=[list(i) for i in self.unmatched_examples],
                    observed_patterns=patterns,
                    images=[dict(zip(('sample_id','project_entry_id','slide_id'), identity), included_cells=count)
                            for identity,count in images.items()],
                    example_assignments=[dict(cell_identity=list(identity), assigned_phenotype=label)
                        for identity, label in sorted(assignments.items())])


@contextmanager
def score_index(requests):
    """Index each distinct reviewed score file in one authenticated streaming pass."""
    if not requests:
        yield None
        return
    from mif_pipeline.cell_analysis.nimbus import _header_details, _identity, _score, _unique_object, _MAX_LINE_BYTES
    with TemporaryDirectory(prefix='mif-combination-scores-') as temporary:
        connection = sqlite3.connect(Path(temporary) / 'scores.sqlite')
        try:
            connection.execute('PRAGMA cache_size=-8192')
            connection.execute('PRAGMA temp_store=FILE')
            connection.execute('CREATE TABLE scores(file TEXT, identity TEXT, score REAL, PRIMARY KEY(file,identity)) WITHOUT ROWID')
            for path, (marker, provenance, measurements) in requests.items():
                checksum = hashlib.sha256()
                with Path(path).open('rb') as stream:
                    def record():
                        line = stream.readline(_MAX_LINE_BYTES+1)
                        if len(line)>_MAX_LINE_BYTES:
                            raise ValueError('Nimbus score record exceeds its size allowance.')
                        checksum.update(line)
                        if not line:
                            return None
                        value = json.loads(line, object_pairs_hook=_unique_object)
                        if not isinstance(value,dict):
                            raise ValueError('Nimbus score records must be objects.')
                        return value
                    scopes, _ = _header_details(record(),marker)
                    for m in measurements:
                        scope = (m.sample_id,m.project_entry_id,m.slide_id,m.section_id)
                        if scopes.get(scope)!=(m.segmentation_population_id,m.canonical_image_id):
                            raise ValueError('Nimbus scores differ from the accepted image or cell population.')
                    while (raw := record()) is not None:
                        if set(raw)!={'cell_identity','score'}:
                            raise ValueError('Nimbus score row requires cell_identity and score.')
                        identity = _identity(raw['cell_identity'],5)
                        if identity[:4] not in scopes:
                            raise ValueError('Nimbus score cell belongs to an undeclared image scope.')
                        score = _score(raw['score'])
                        try:
                            connection.execute('INSERT INTO scores VALUES(?,?,?)',
                                (path,canonical_bytes(identity).decode(),score if isfinite(score) else None))
                        except sqlite3.IntegrityError as exc:
                            raise ValueError('Nimbus scores contain a duplicate cell identity.') from exc
                if checksum.hexdigest()!=provenance['sha256']:
                    raise ValueError('Nimbus scores changed after review. Reopen the marker review.')
            connection.commit()
            yield connection
        finally:
            connection.close()


def _measurement_batches(measurement, names, selection):
    features = tuple(f for f in measurement.measurement_plan.features if f.kind=='expression')
    ref = measurement.measurement_rows
    return iter_selected_measurement_batches(
        descriptor=MarkerIntensityRowsDescriptor(ref.path,ref.sha256,ref.byte_count,ref.row_count,ref.content_schema_version),
        authenticated_output_root=ref.path.parent.parent.parent,
        expected=MeasurementRowStreamExpectation(
            project_entry_id=measurement.project_entry_id,slide_id=measurement.slide_id,
            section_id=measurement.section_id,segmentation_generation_id=measurement.measurement_generation_id,
            measurement_generation_id=measurement.measurement_generation_id,
            segmentation_population_id=measurement.segmentation_population_id,
            analysis_domain_bindings=tuple({'analysis_domain_id':b.analysis_domain_id,
                'analysis_domain_revision_sha256':b.analysis_domain_revision_sha256} for b in measurement.analysis_domain_bindings),
            feature_ids=tuple(f.feature_id for f in features),measurement_names=tuple(f.measurement for f in features)),
        measurement_names=names,population_selection=selection)


def preview_assignments(gate, measurements, definition, *, pattern_path=None):
    """Reuse accepted numerical decisions; evaluate draft ordering without fitting."""
    markers, offered = marker_feature_candidates(measurements)
    markers, offered = configured_marker_catalog(gate.marker_settings,
        available_marker_ids=markers, offered_marker_features=offered)
    if markers != gate.canonical_marker_ids:
        raise ValueError('The accepted gates differ from the measured marker inventory.')
    settings = validate_marker_settings(gate.marker_settings,canonical_marker_ids=markers,offered_marker_features=offered)
    raw_rules = {r['marker_id']:r for r in settings['marker_rules']}
    features = {(f['marker_id'],f['feature_id']):f for f in offered}
    prepared, requests = [], {}
    for m in measurements:
        scope = (m.sample_id,m.project_entry_id,m.slide_id,m.section_id)
        semantics, thresholds, direct = [], [], []
        for accepted in gate.accepted_thresholds:
            raw = raw_rules[accepted.marker_id]
            feature = features[(accepted.marker_id,raw['feature_id'])]
            selected, _ = resolve_accepted_threshold(accepted,
                image_id=canonical_bytes(scope[:3]).decode(),section_id=_section_identity(*scope))
            semantics.append(MeasurementSemantic(feature['channel_id'],feature['measurement'],feature['compartment'],accepted.marker_id))
            if selected.outcome=='direct_scores':
                provenance = accepted.score_provenance
                path = str(Path(provenance['path']))
                if path not in requests:
                    requests[path] = (accepted.marker_id,provenance,[])
                elif requests[path][0]!=accepted.marker_id or requests[path][1]['sha256']!=provenance['sha256']:
                    raise ValueError('Accepted Nimbus score identities conflict.')
                requests[path][2].append(m)
                cutoff = provenance['cutoff'] if selected.score_cutoff is None else selected.score_cutoff
                direct.append((accepted.marker_id,path,float(cutoff)))
            else:
                thresholds.append(ThresholdRule(accepted.marker_id,feature['measurement'],feature['compartment'],
                    raw['polarity'],selected.threshold,True,selected.outcome,threshold_intensity_domain(selected)))
        prepared.append((m,scope,tuple(semantics),tuple(thresholds),direct))
    total = 0
    with AssignmentCounts(definition,markers,pattern_path=pattern_path) as counts, score_index(requests) as index:
        for m,scope,semantics,thresholds,direct in prepared:
            counts.add((), scope)
            selection = resolve_population_selection(settings['phenotyping_population'],
                sample_id=m.sample_id,project_entry_id=m.project_entry_id,slide_id=m.slide_id)
            names = tuple(dict.fromkeys(r.measurement for r in thresholds))
            count = 0
            for batch in _measurement_batches(m,names,selection):
                rows = batch.to_pylist()
                count += len(rows)
                cells = tuple(MarkerCellValues(r['cell_id'],m.slide_id,m.section_id,r['analysis_domain_id'],
                    {name:r['measurement:'+name] for name in names},{marker:None for marker in markers})
                    for r in rows if r['phenotyping_selected'])
                thresholded = apply_threshold_rules(cells,thresholds,measurement_semantics=semantics)
                for marker,path,cutoff in direct:
                    updated = []
                    for cell in thresholded:
                        row = index.execute('SELECT score FROM scores WHERE file=? AND identity=?',
                            (path,canonical_bytes((*scope,cell.cell_id)).decode())).fetchone()
                        if row is None:
                            raise ValueError('Nimbus scores are missing a selected measured cell. Reopen the marker review.')
                        updated.append(replace(cell,marker_states={**cell.marker_states,
                            marker:None if row[0] is None else row[0]>=cutoff}))
                    thresholded = tuple(updated)
                counts.add(thresholded,scope)
            if count!=m.cell_count:
                raise ValueError('The measured cell count differs from the accepted marker population.')
            total += count
        return counts.result(total=total)


def resolve_preview_inputs(snapshot, context):
    """Resolve the accepted inputs named by a validated combination context."""
    from ..batch_measurement_artifacts import decode_committed_measurements_result
    current = {a.artifact_ref.artifact_ref_sha256:a for a in snapshot.committed_artifact_authorities}
    try:
        gate = decode_committed_marker_gate(current[context['marker_gate_ref_sha256']])
        authorities = tuple(current[b['measurements_result_ref_sha256']] for b in gate.measurement_authority_bindings)
        measurements = tuple(decode_committed_measurements_result(a) for a in authorities)
    except KeyError as exc:
        raise ValueError('The accepted marker inputs are no longer available. Reopen this step.') from exc
    if tuple(context['canonical_marker_ids'])!=gate.canonical_marker_ids:
        raise ValueError('The combination request differs from its accepted marker inventory.')
    sample_order = {sample.sample_id:index for index,sample in enumerate(snapshot.manifest.enabled_samples)}
    node_order = {node.node_id:index for index,node in enumerate(snapshot.plan.nodes)}
    ordered = sorted(zip(measurements, authorities, strict=True), key=lambda pair: (
        sample_order[pair[0].sample_id], node_order.get(pair[1].artifact_ref.producer_node_id, len(node_order))))
    measurements, authorities = tuple(zip(*ordered))
    return gate, measurements, authorities


def preview_from_snapshot(snapshot, result, phenotype_definition, *, pattern_path=None):
    """Count a draft against the exact current pending request's accepted inputs."""
    definition = validate_combination_response(result,{'phenotype_definition':phenotype_definition})['phenotype_definition']
    gate, measurements, _ = resolve_preview_inputs(snapshot, result.resume_context)
    return preview_assignments(gate,measurements,definition,pattern_path=pattern_path)


def preview_from_population(path, markers, total, definition):
    """Reassign a current accepted population without reading source cell files."""
    with AssignmentCounts(definition,markers,pattern_path=path) as counts:
        counts.reassign()
        return counts.result(total=total)
