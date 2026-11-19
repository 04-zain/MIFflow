"""Execution owner for one declared protein-marker-intensity clustering graph.

The operation retains the selected stable-cell population, measurement generation and
phenotype view. Its records and stage adapters connect accepted numerical results to the
current batch target and reviewed application scope."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
import sys
from types import MappingProxyType
from collections.abc import Mapping
from pathlib import Path
import sqlite3
import tempfile

import numpy as np
import scipy

from mif_pipeline.cell_analysis.clustering import (
    CellClusteringError,
    CellClusteringIdentity,
    FeatureRole,
    cluster_marker_matrix,
    _selected_feature_specs,
)
from mif_pipeline.cell_analysis.measurement_plan import FeatureKind
from mif_pipeline.cell_analysis.measurement_rows import (
    MarkerIntensityRowsDescriptor,
    MeasurementRowStreamError,
    MeasurementRowStreamExpectation,
    iter_selected_measurement_batches,
)
from mif_pipeline.datasets.identity import SortedIdDigest, SortedIdMappingDigest
from mif_pipeline.orchestration.batch_artifacts import (
    BatchArtifactError,
    BatchCommittedArtifactAuthority,
    BatchCurrentExecutionAuthority,
    BatchNodeExecutionContext,
    validate_committed_artifact_authority_set,
    validate_current_execution_authority,
)
from mif_pipeline.pipeline_core.config import ClusteringMode

from ._common import BatchPhenotypingError, digest, feature_schema_sha256, text
from .authority import decode_committed_preparation, validate_measurement_authority
from .records import (
    BatchClusterAssignment,
    BatchClusteringEntryComposition,
    BatchClusteringExecutionResult,
    BatchClusteringRequest,
    UnsupervisedPreparationInput,
    generation_sha256,
    label_projections,
)

# ==============================================================================
#                                   Execution
# ==============================================================================


def _runtime_versions(method='snn_leiden') -> Mapping[str, str]:
    values = {
        "numpy": np.__version__,
        "python": ".".join(str(item) for item in sys.version_info[:3]),
        "scipy": scipy.__version__,
    }
    for distribution in ("igraph", "leidenalg", *((method,) if method in {'flowsom', 'phenograph'} else ())):
        try:
            values[distribution] = version(distribution)
        except PackageNotFoundError as exc:
            recovery = (
                "Repair or replace the portable application with a complete distribution."
                if getattr(sys, "frozen", False)
                else "From the source folder, install the desktop with `python -m pip install '.[desktop]'` "
                "or a headless worker with `python -m pip install .`."
            )
            raise BatchPhenotypingError(
                f"Clustering requires installed {distribution} version metadata. {recovery}"
            ) from exc
    return MappingProxyType(dict(sorted(values.items())))


def _entry_composition(
    preparations: tuple[tuple[UnsupervisedPreparationInput, str], ...],
    selected_counts: Mapping[str, int],
) -> tuple[BatchClusteringEntryComposition, ...]:
    by_entry: dict[str, list[tuple[UnsupervisedPreparationInput, str]]] = {}
    for preparation, ref_sha in preparations:
        by_entry.setdefault(preparation.project_entry_id, []).append((preparation, ref_sha))
    result: list[BatchClusteringEntryComposition] = []
    for entry_id in sorted(by_entry):
        values = by_entry[entry_id]
        first = values[0][0]
        result.append(
            BatchClusteringEntryComposition(
                sample_id=first.sample_id,
                subject_id=first.subject_id,
                project_entry_id=entry_id,
                slide_id=first.slide_id,
                phenotyping_group_id=first.phenotyping_group_id,
                resolved_config_sha256=first.resolved_config_sha256,
                panel_sha256=first.panel_sha256,
                feature_schema_sha256=first.feature_schema_sha256,
                section_ids=tuple(sorted({x.section_id for x, _ in values})),
                validation_domain_ids=tuple(
                    sorted({d for x, _ in values for d in x.validation_domain_ids})
                ),
                analysis_domain_ids=tuple(
                    sorted({d for x, _ in values for d in x.analysis_domain_ids})
                ),
                segmentation_generation_ids=tuple(
                    sorted({x.segmentation_generation_id for x, _ in values})
                ),
                measurement_generation_ids=tuple(
                    sorted({x.measurement_generation_id for x, _ in values})
                ),
                measurement_plan_sha256s=tuple(
                    sorted({x.measurement_plan_sha256 for x, _ in values})
                ),
                preparation_ref_sha256s=tuple(sorted(ref_sha for _, ref_sha in values)),
                measurement_ref_sha256s=tuple(
                    sorted({x.measurement_result_ref_sha256 for x, _ in values})
                ),
                cell_count=sum(selected_counts[ref] for _, ref in values),
                cell_inventory_sha256=digest(
                    [
                        [item.section_id, item.cell_inventory_sha256]
                        for item, _ in sorted(values, key=lambda value: value[0].section_id)
                    ]
                ),
            )
        )
    return tuple(result)


def _selected_source_batches(measurement, names, *, population_selection=None):
    """Read our requested primitive columns from the owner's accepted selection.

    We match the current section, population and full declared feature schema
    using compact metadata. The measurement owner already checked publication;
    downstream fitting projects only its selected columns and applied rows.
    """
    reference = measurement.measurement_rows
    marker_features = tuple(item for item in measurement.measurement_plan.features if item.kind == FeatureKind.EXPRESSION.value)
    path = reference.path
    if path.parent.name != "measurement-rows" or path.parent.parent.name != "batch":
        raise BatchPhenotypingError("Clustering measurements are outside their canonical owner")
    descriptor = MarkerIntensityRowsDescriptor(path=path,sha256=reference.sha256,byte_count=reference.byte_count,row_count=reference.row_count,content_schema_version=reference.content_schema_version)
    expected = MeasurementRowStreamExpectation(
        project_entry_id=measurement.project_entry_id,slide_id=measurement.slide_id,
        section_id=measurement.section_id,segmentation_generation_id=measurement.measurement_generation_id,
        measurement_generation_id=measurement.measurement_generation_id,segmentation_population_id=measurement.segmentation_population_id,
        analysis_domain_bindings=tuple({"analysis_domain_id":item.analysis_domain_id,"analysis_domain_revision_sha256":item.analysis_domain_revision_sha256} for item in measurement.analysis_domain_bindings),
        feature_ids=tuple(item.feature_id for item in marker_features),measurement_names=tuple(item.measurement for item in marker_features),
    )
    yield from iter_selected_measurement_batches(descriptor=descriptor,authenticated_output_root=path.parent.parent.parent,expected=expected,measurement_names=names,population_selection=population_selection)


def _assemble_selected_matrix(decoded, measurements, selected, count, *, selected_cells=None):
    """Fill our one Float64 fitting buffer through projected completed datasets.

    i) A bounded disk index establishes stable composite-cell order from IDs
    and domain identities, checking exact membership commitments. ii) We read
    only selected numeric columns into that one allocation, retaining Int32
    source/domain codes for later assignments. Geometry, Python value tuples,
    duplicate dense matrices and persisted numeric sort tables are absent.
    """
    domain_labels = tuple(sorted({binding.analysis_domain_id for measurement in measurements.values() for binding in measurement.analysis_domain_bindings}))
    domain_lookup = {label:code for code,label in enumerate(domain_labels)}
    identities = []
    with tempfile.TemporaryDirectory(prefix='mif-clustering-identities-') as temporary:
        connection = sqlite3.connect(Path(temporary)/'identities.sqlite')
        try:
            connection.execute('PRAGMA cache_size=-8192')
            connection.execute('PRAGMA temp_store=FILE')
            connection.execute('CREATE TABLE cells(sample TEXT,entry TEXT,cell_id TEXT,domain_id TEXT,source INTEGER,selected INTEGER,position INTEGER,PRIMARY KEY(sample,entry,cell_id))')
            for source,(preparation,ref) in enumerate(decoded):
                measurement = measurements[ref]
                for batch in _selected_source_batches(measurement,(),population_selection=preparation.population_selection):
                    rows=zip(batch.column('cell_id').to_pylist(),batch.column('analysis_domain_id').to_pylist(),batch.column('phenotyping_selected').to_pylist())
                    def indexed_rows():
                        for id,domain,included in rows:
                            if selected_cells is not None:
                                retained=selected_cells.get((preparation.sample_id,preparation.project_entry_id,id))
                                if retained is not None and (not included or retained!=(preparation.slide_id,preparation.section_id,domain)):
                                    raise BatchPhenotypingError('Selected refit cells differ from their retained section or domain.')
                                included=included and retained is not None
                            yield preparation.sample_id,preparation.project_entry_id,id,domain,source,int(included)
                    connection.executemany('INSERT INTO cells VALUES(?,?,?,?,?,?,NULL)',indexed_rows())
                ids,mapping = SortedIdDigest(),SortedIdMappingDigest()
                domains={id:SortedIdDigest() for id in preparation.analysis_domain_ids}
                for id,domain in connection.execute('SELECT cell_id,domain_id FROM cells WHERE source=? ORDER BY cell_id',(source,)):
                    if domain not in domains:
                        raise BatchPhenotypingError('Clustering contains a foreign accepted domain')
                    ids.add(id);mapping.add(id,domain);domains[domain].add(id)
                if (ids.count!=preparation.cell_count or ids.hexdigest()!=preparation.cell_ids_sha256 or mapping.hexdigest()!=preparation.cell_to_domain_membership_sha256):
                    raise BatchPhenotypingError('Clustering current membership differs from its owner authority')
                for commitment in measurement.domain_cell_commitments:
                    digest=domains[commitment['analysis_domain_id']]
                    if digest.count!=commitment['cell_count'] or digest.hexdigest()!=commitment['cell_ids_sha256']:
                        raise BatchPhenotypingError('Clustering current domain membership differs')
            selected_count = connection.execute('SELECT COUNT(*) FROM cells WHERE selected=1').fetchone()[0]
            if selected_cells is not None and selected_count!=len(selected_cells):
                raise BatchPhenotypingError('The retained refit population is not fully present in the accepted measurements.')
            if selected_count == 0:
                raise BatchPhenotypingError('No cells are in the selected phenotyping regions. Return to cell selection and choose regions containing cells.')
            if selected_count > count:
                raise BatchPhenotypingError('Clustering selected population exceeds its admitted count')
            count = selected_count
            features = np.empty((count,len(selected)),dtype=np.float64)
            source_codes = np.empty(count,dtype=np.int32)
            domain_codes = np.empty(count,dtype=np.int32)
            cursor=connection.execute('SELECT sample,entry,cell_id,domain_id,source FROM cells WHERE selected=1 ORDER BY sample,entry,cell_id')
            position=0
            while rows:=cursor.fetchmany(512):
                updates=[]
                for sample,entry,id,domain,source in rows:
                    if position>=count:
                        raise BatchPhenotypingError('Clustering contains more cells than admitted')
                    identities.append(CellClusteringIdentity(sample,entry,id))
                    source_codes[position]=source;domain_codes[position]=domain_lookup[domain]
                    updates.append((position,sample,entry,id));position+=1
                connection.executemany('UPDATE cells SET position=? WHERE sample=? AND entry=? AND cell_id=?',updates)
            if position!=count:
                raise BatchPhenotypingError('Clustering current population count differs')
            filled=np.zeros(count,dtype=bool)
            names=tuple(spec.measurement for spec in selected)
            for source,(preparation,ref) in enumerate(decoded):
                for batch in _selected_source_batches(measurements[ref],names,population_selection=preparation.population_selection):
                    batch = batch.filter(batch.column("phenotyping_selected"))
                    if selected_cells is not None:
                        batch=batch.take(np.flatnonzero([(preparation.sample_id,preparation.project_entry_id,id) in selected_cells
                            for id in batch.column('cell_id').to_pylist()]))
                    if not batch.num_rows:
                        continue
                    ids=batch.column('cell_id').to_pylist()
                    placeholders=','.join('?' for _ in ids)
                    positions={id:(position,domain) for id,position,domain in connection.execute('SELECT cell_id,position,domain_id FROM cells WHERE sample=? AND entry=? AND cell_id IN ('+placeholders+')',(preparation.sample_id,preparation.project_entry_id,*ids))}
                    if len(positions)!=len(ids):
                        raise BatchPhenotypingError('Selected numeric rows differ from admitted identities')
                    offsets=np.array([positions[id][0] for id in ids],dtype=np.intp)
                    if np.any(filled[offsets]) or batch.column('analysis_domain_id').to_pylist()!=[positions[id][1] for id in ids]:
                        raise BatchPhenotypingError('Selected numeric rows repeat IDs or change domains')
                    for column,name in enumerate(names):
                        features[offsets,column]=batch.column('measurement:'+name).to_numpy(zero_copy_only=False)
                    filled[offsets]=True
            if not filled.all():
                raise BatchPhenotypingError('Selected numeric rows omit an admitted identity')
        except sqlite3.IntegrityError as error:
            raise BatchPhenotypingError('Clustering contains duplicate composite identities') from error
        finally:
            connection.close()
    return features,tuple(identities),source_codes,domain_codes,domain_labels


def execute_batch_clustering(
    *,
    request: BatchClusteringRequest,
    context: BatchNodeExecutionContext,
    execution_authority: BatchCurrentExecutionAuthority,
    preparation_authorities: tuple[BatchCommittedArtifactAuthority, ...],
    measurement_authorities: tuple[BatchCommittedArtifactAuthority, ...],
) -> BatchClusteringExecutionResult:
    """Authenticate, validate and cluster exactly one declared graph once."""

    if type(request) is not BatchClusteringRequest:
        raise BatchPhenotypingError("request must be an exact BatchClusteringRequest")
    try:
        current = validate_current_execution_authority(
            execution_authority, expected_context=context
        )
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError(
            "clustering requires current plan-issued execution authority"
        ) from exc
    expected_kind = {
        ClusteringMode.PER_SLIDE: "clustering_per_slide",
        ClusteringMode.POOLED: "clustering_pooled",
    }[request.mode]
    if (
        current.node_kind != expected_kind
        or context.adapter_spec.node_kind != expected_kind
        or current.active_plan_sha256 != request.active_plan_sha256
        or current.participant_ids != request.participant_ids
        or current.project_entry_ids != request.project_entry_ids
    ):
        raise BatchPhenotypingError("clustering request differs from plan authority")
    if type(preparation_authorities) is not tuple or type(measurement_authorities) is not tuple:
        raise BatchPhenotypingError("source authorities must be exact tuples")
    if not preparation_authorities or not measurement_authorities:
        raise BatchPhenotypingError("clustering requires preparation and measurement authorities")
    supplied_refs = {
        item.artifact_ref.artifact_ref_sha256
        for item in (*preparation_authorities, *measurement_authorities)
    }
    authorized_scientific_refs = {
        item.artifact_ref_sha256
        for item in context.inputs
        if item.artifact_kind in {"unsupervised_preparation_result", "measurements_result"}
    }
    if supplied_refs != authorized_scientific_refs:
        raise BatchPhenotypingError(
            "clustering sources do not exactly cover plan-authorized scientific inputs"
        )
    try:
        authenticated = validate_committed_artifact_authority_set(
            (*preparation_authorities, *measurement_authorities),
            expected_active_plan_sha256=request.active_plan_sha256,
        )
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("clustering source authority is stale or invalid") from exc
    if authenticated[0].state_head_sha256 != current.state_head_sha256:
        raise BatchPhenotypingError(
            "clustering sources are not authenticated at the current execution head"
        )
    preparation_set = tuple(
        x for x in authenticated if x.producer_node_kind == "unsupervised_preparation"
    )
    measurement_set = tuple(x for x in authenticated if x.producer_node_kind == "measurements")
    if len(preparation_set) != len(preparation_authorities) or len(measurement_set) != len(
        measurement_authorities
    ):
        raise BatchPhenotypingError("clustering authority producer kinds are invalid")

    declared_cell_count = 0
    declared_input_bytes = 0
    for authority in preparation_set:
        count = authority.artifact_ref.owning_identity.get("cell_count")
        byte_count = authority.artifact_ref.owning_identity.get("measurement_rows_byte_count")
        if type(count) is not int or count < 0:
            raise BatchPhenotypingError("preparation cell count identity is invalid")
        if type(byte_count) is not int or byte_count < 0:
            raise BatchPhenotypingError("measurement row byte count identity is invalid")
        declared_cell_count += count
        matrix_bytes = authority.artifact_ref.owning_identity.get("measurement_matrix_bytes")
        if type(matrix_bytes) is not int or matrix_bytes <= 0:
            raise BatchPhenotypingError("measurement numeric matrix allowance is invalid")
        declared_input_bytes += byte_count
    if declared_input_bytes > request.maximum_input_bytes:
        raise BatchPhenotypingError("declared graph exceeds maximum_input_bytes")

    decoded = tuple(
        sorted(
            (
                (
                    decode_committed_preparation(x, maximum_byte_count=16 * 1024**2),
                    x.artifact_ref.artifact_ref_sha256,
                )
                for x in preparation_set
            ),
            key=lambda item: (
                item[0].sample_id,
                item[0].project_entry_id,
                item[0].section_id,
                item[0].analysis_domain_ids,
            ),
        )
    )
    plan_scope = current.clustering_scope
    validation_domain_ids = tuple(
        sorted(
            {
                validation_domain_id
                for preparation, _ in decoded
                for validation_domain_id in preparation.validation_domain_ids
            }
        )
    )
    if (
        not isinstance(plan_scope, Mapping)
        or set(plan_scope) != {"mode", "phenotyping_group_id", "validation_domain_ids"}
        or plan_scope["mode"] != request.mode.value
        or plan_scope["phenotyping_group_id"] != request.phenotyping_group_id
        or type(plan_scope["validation_domain_ids"]) is not tuple
        or plan_scope["validation_domain_ids"] != validation_domain_ids
    ):
        raise BatchPhenotypingError("clustering graph scope differs from plan-issued authority")
    raw_preparation_bindings = tuple(
        binding
        for authority in preparation_set
        for binding in authority.artifact_ref.scope.analysis_domain_bindings
    )
    preparation_bindings = tuple(
        sorted(
            set(raw_preparation_bindings),
            key=lambda item: (
                item.sample_id,
                item.slide_id,
                item.analysis_domain_id,
                item.analysis_domain_revision,
                item.project_entry_id,
            ),
        )
    )
    if (
        len(preparation_bindings) != len(raw_preparation_bindings)
        or preparation_bindings != current.analysis_domain_bindings
    ):
        raise BatchPhenotypingError(
            "preparation domain bindings do not exactly cover plan authority"
        )
    preparation_configurations = tuple(
        sorted(
            {
                binding
                for authority in preparation_set
                for binding in authority.artifact_ref.configuration_bindings
            },
            key=lambda item: item.sample_id,
        )
    )
    if preparation_configurations != current.configuration_bindings:
        raise BatchPhenotypingError(
            "preparation configuration bindings do not exactly cover plan authority"
        )
    for preparation, ref_sha in decoded:
        ref = next(
            item.artifact_ref
            for item in preparation_set
            if item.artifact_ref.artifact_ref_sha256 == ref_sha
        )
        if (
            tuple(binding.analysis_domain_id for binding in ref.scope.analysis_domain_bindings)
            != preparation.analysis_domain_ids
        ):
            raise BatchPhenotypingError("preparation cells lack exact revision-bound domain scope")
    measurement_by_ref = {x.artifact_ref.artifact_ref_sha256: x for x in measurement_set}
    if len(measurement_by_ref) != len(measurement_set):
        raise BatchPhenotypingError("duplicate measurement authorities are not allowed")
    required_measurements = tuple(x.measurement_result_ref_sha256 for x, _ in decoded)
    if len(set(required_measurements)) != len(required_measurements) or set(
        measurement_by_ref
    ) != set(required_measurements):
        raise BatchPhenotypingError(
            "measurement authorities do not exactly cover preparation inputs"
        )
    measurement_projection_by_preparation_ref = {}
    for preparation, preparation_ref_sha256 in decoded:
        projection = validate_measurement_authority(
            measurement_by_ref[preparation.measurement_result_ref_sha256], preparation
        )
        measurement_projection_by_preparation_ref[preparation_ref_sha256] = projection
        preparation_ref = next(
            authority.artifact_ref
            for authority in preparation_set
            if authority.artifact_ref.artifact_ref_sha256 == preparation_ref_sha256
        )
        measurement_bindings = tuple(
            binding
            for binding in projection.analysis_domain_bindings
            if binding.analysis_domain_id in preparation.analysis_domain_ids
        )
        if measurement_bindings != preparation_ref.scope.analysis_domain_bindings:
            raise BatchPhenotypingError(
                "preparation domain revisions differ from measurement authority"
            )

    participants = tuple(sorted({x.sample_id for x, _ in decoded}))
    entries = tuple(sorted({x.project_entry_id for x, _ in decoded}))
    if participants != request.participant_ids or entries != request.project_entry_ids:
        raise BatchPhenotypingError("declared graph participants or entries differ")
    if any(
        x.clustering_mode is not request.mode
        or x.feature_specs != request.feature_specs
        or x.k != request.k
        or x.resolution != request.resolution
        or x.seed != request.seed
        or x.method != request.method
        or x.method_settings != request.method_settings
        for x, _ in decoded
    ):
        raise BatchPhenotypingError("clustering configuration is incompatible")
    if len({x.panel_sha256 for x, _ in decoded}) != 1:
        raise BatchPhenotypingError("declared graph contains incompatible panels")
    if len({x.measurement_feature_schema_sha256 for x, _ in decoded}) != 1:
        raise BatchPhenotypingError(
            "declared graph contains incompatible measurement feature schemas"
        )
    marker_row_schemas = {
        tuple(
            (item.feature_id, item.measurement)
            for item in measurement_projection_by_preparation_ref[ref_sha].measurement_plan.features
            if item.kind == FeatureKind.EXPRESSION.value
        )
        for _, ref_sha in decoded
    }
    if len(marker_row_schemas) != 1:
        raise BatchPhenotypingError("declared graph contains incompatible marker row schemas")
    by_entry_scope: dict[str, tuple[object, ...]] = {}
    for item, _ in decoded:
        scope = (
            item.sample_id,
            item.subject_id,
            item.slide_id,
            item.phenotyping_group_id,
            item.resolved_config_sha256,
            item.panel_sha256,
            item.feature_schema_sha256,
        )
        if by_entry_scope.setdefault(item.project_entry_id, scope) != scope:
            raise BatchPhenotypingError("project entry scope differs across sections")
    if request.mode is ClusteringMode.PER_SLIDE and len(entries) != 1:
        raise BatchPhenotypingError("per_slide graph contains more than one entry")
    if request.mode is ClusteringMode.POOLED and (
        validation_domain_ids != (current.validation_domain_id,)
    ):
        raise BatchPhenotypingError("pooled graph must remain within one Processing group")

    selected = _selected_feature_specs(request.feature_specs,excluded_roles=(FeatureRole.NUCLEAR,FeatureRole.AUTOFLUORESCENCE,FeatureRole.DAPI))
    try:
        maximum_selected_count = min(declared_cell_count, request.maximum_cell_count,
                                     (request.maximum_input_bytes - declared_input_bytes) // (len(selected) * 8))
        features,identities,source_codes,domain_codes,domain_labels = _assemble_selected_matrix(decoded,measurement_projection_by_preparation_ref,selected,maximum_selected_count)
        clustered = cluster_marker_matrix(features,identities=identities,domain_codes=domain_codes,domain_labels=domain_labels,feature_specs=selected,k=request.k,resolution=request.resolution,seed=request.seed,method=request.method,method_settings=request.method_settings)
    except (CellClusteringError,MeasurementRowStreamError,ImportError,TypeError,ValueError) as exc:
        raise BatchPhenotypingError("cell-analysis clustering failed") from exc
    assignments = tuple(
        BatchClusterAssignment(sample_id=item.identity.sample_id,project_entry_id=item.identity.project_entry_id,cell_id=item.identity.cell_id,
            cluster_id=text(item.cluster_id,"cluster_id"),slide_id=decoded[int(source_codes[i])][0].slide_id,
            section_id=decoded[int(source_codes[i])][0].section_id,analysis_domain_id=item.analysis_domain_id)
        for i,item in enumerate(clustered.assignments)
    )
    selected_counts = np.bincount(source_codes, minlength=len(decoded))
    composition = _entry_composition(decoded, {ref: int(selected_counts[index]) for index, (_, ref) in enumerate(decoded)})
    source_refs = tuple(sorted(x.artifact_ref.artifact_ref_sha256 for x in authenticated))
    runtime = _runtime_versions(request.method)
    generation = generation_sha256(
        request_sha256=request.request_sha256,
        source_authority_sha256s=source_refs,
        entry_composition=composition,
        assignments=assignments,
        evidence=clustered.evidence,
        runtime_versions=runtime,
    )
    return BatchClusteringExecutionResult(
        mode=request.mode,
        phenotyping_group_id=request.phenotyping_group_id,
        participant_ids=request.participant_ids,
        project_entry_ids=request.project_entry_ids,
        request_sha256=request.request_sha256,
        source_authority_sha256s=source_refs,
        source_state_head_sha256=authenticated[0].state_head_sha256,
        active_plan_sha256=request.active_plan_sha256,
        entry_composition=composition,
        assignments=assignments,
        entry_projections=label_projections(assignments, composition, generation),
        label_review_status="pending_optional_expert_review",
        input_population_sha256=digest(
            [[x.sample_id, x.project_entry_id, x.cell_inventory_sha256] for x in composition]
        ),
        output_assignment_sha256=digest([x.to_mapping() for x in assignments]),
        evidence=clustered.evidence,
        runtime_versions=runtime,
        clustering_generation_sha256=generation,
    )


__all__ = ["execute_batch_clustering"]
