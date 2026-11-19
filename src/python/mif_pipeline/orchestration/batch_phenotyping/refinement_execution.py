"""Compose a subset refit with unchanged parent memberships and source scope."""
from dataclasses import replace
import re

from mif_pipeline.cell_analysis.clustering import CellClusteringIdentity
from ._common import BatchPhenotypingError, canonical_bytes, digest
from .records import BatchClusterRefinement, generation_sha256, label_projections


def refit_options(snapshot,node):
    """Describe one current fit for the existing saved-options editor."""
    from collections import Counter
    from .codec import decode_committed_batch_clustering_result
    from .refinement_settings import ClusterRefitSettings
    authorities={item.artifact_ref.artifact_ref_sha256:item for item in snapshot.committed_artifact_authorities}
    owners=[item for item in authorities.values() if item.artifact_ref.producer_node_id==node.node_id
            and item.artifact_ref.artifact_kind=='clustered_phenotype_result']
    if len(owners)!=1:
        raise BatchPhenotypingError('Refitting requires a completed current clustering result.')
    owner=owners[0]
    sources=tuple(authorities[key] for key in owner.artifact_ref.owning_identity['source_authority_sha256s'])
    result=decode_committed_batch_clustering_result(owner,source_authorities=sources,maximum_byte_count=owner.artifact_ref.byte_count)
    model=result.refinement.evidence if result.refinement is not None else result.evidence
    method='flowsom' if model.graph_algorithm=='flowsom/v1' else 'phenograph'
    settings=ClusterRefitSettings(method=method,k=model.requested_k or 15,resolution=model.resolution,
        seed=model.seed,method_settings=dict(model.method_settings) if method=='flowsom' else {})
    counts=Counter(row.cluster_id for row in result.assignments)
    return settings,dict(cluster_counts=dict(sorted(counts.items())),cell_count=len(result.assignments),
        mode=result.mode.value,feature_measurements=list(result.evidence.feature_measurements),
        robust_centers=list(result.evidence.robust_centers),robust_scales=list(result.evidence.robust_scales),
        feature_weights=list(result.evidence.feature_weights),can_restore_parent=result.refinement is not None)


def _retained_result(terminal, source_refs, *, expected_ref=None):
    """Decode only the exact result named by an authenticated revision terminal."""
    import hashlib
    from pathlib import Path
    from mif_pipeline.pipeline_core.stage_result import StageStatus
    from .authority import clustering_result_owning_identity
    from .codec import _loads_batch_clustering_result
    from ._common import read_bounded_file
    refs=tuple(ref for ref in terminal.output_artifacts if ref.artifact_kind=='clustered_phenotype_result'
               and (expected_ref is None or ref.artifact_ref_sha256==expected_ref))
    if terminal.stage_result.status is not StageStatus.COMPLETED or len(refs)!=1:
        raise BatchPhenotypingError('The revision lacks its exact completed parent clustering result.')
    ref=refs[0]
    if ref.producer_node_id!=terminal.node_id or ref.producer_attempt_id!=terminal.attempt_id:
        raise BatchPhenotypingError('The retained parent result differs from its completed producer.')
    payload=read_bounded_file(Path(ref.locator),expected_byte_count=ref.byte_count,maximum_byte_count=ref.byte_count)
    if hashlib.sha256(payload).hexdigest()!=ref.sha256:
        raise BatchPhenotypingError('The retained parent clustering result changed.')
    result=_loads_batch_clustering_result(payload,expected_request_sha256=ref.owning_identity['request_sha256'],
        expected_source_authority_sha256s=source_refs)
    if canonical_bytes(clustering_result_owning_identity(result))!=canonical_bytes(ref.owning_identity):
        raise BatchPhenotypingError('The retained parent identity differs from its result.')
    return result,ref


def execute_refit_revision(*,store,context,current,preparations,measurements,decoded_preparations):
    """Reuse current sources and the existing authenticated predecessor route."""
    from .authority import validate_measurement_authority
    from .execution import _assemble_selected_matrix,_runtime_versions
    from .refinement_settings import ClusterRefitSettings
    from mif_pipeline.cell_analysis.clustering_refinement import refit_marker_matrix
    node=store._load_plan_snapshot(current.active_plan_sha256).node_by_id[context.node_id]
    binding=node.get('settled_review_revision',{})
    if 'cluster_refit' not in binding:
        return None
    settings=ClusterRefitSettings.from_mapping(binding['cluster_refit'])
    previous=store.settled_review_predecessor(context,execution=current)
    if previous is None:
        raise BatchPhenotypingError('Refitting requires its exact retained parent calculation.')
    terminal,_authorization=previous
    sources=tuple(sorted(item.artifact_ref.artifact_ref_sha256 for item in (*preparations,*measurements)))
    parent,ref=_retained_result(terminal,sources)
    if (parent.participant_ids!=current.participant_ids or parent.project_entry_ids!=current.project_entry_ids
            or current.clustering_scope.get('mode')!=parent.mode.value):
        raise BatchPhenotypingError('The refit scope differs from its retained parent population.')
    if settings.restore_parent:
        if parent.refinement is None:
            raise BatchPhenotypingError('This result has no selected-cluster refit to discard.')
        lineage=parent.refinement
        original_terminal=store.load_attempt(lineage.parent_terminal_record_sha256)
        original,_=_retained_result(original_terminal,sources,expected_ref=lineage.parent_result_ref_sha256)
        if original.result_sha256!=lineage.parent_result_sha256:
            raise BatchPhenotypingError('The parent restoration differs from the recorded refit lineage.')
        return restored_parent_result(original,discarded_result_sha256=parent.result_sha256,
            state_head_sha256=current.state_head_sha256,active_plan_sha256=current.active_plan_sha256)
    selected=tuple(row for row in parent.assignments if row.cluster_id in settings.cluster_ids)
    if (not settings.cluster_ids or {row.cluster_id for row in selected}!=set(settings.cluster_ids)
            or len(selected)<2):
        raise BatchPhenotypingError('Select current clusters containing at least two cells.')
    cells={(row.sample_id,row.project_entry_id,row.cell_id):(row.slide_id,row.section_id,row.analysis_domain_id) for row in selected}
    decoded=tuple((p,a.artifact_ref.artifact_ref_sha256) for p,a in zip(decoded_preparations,preparations,strict=True))
    measurement_by_ref={item.artifact_ref.artifact_ref_sha256:item for item in measurements}
    resolved={key:validate_measurement_authority(measurement_by_ref[p.measurement_result_ref_sha256],p) for p,key in decoded}
    from mif_pipeline.cell_analysis.clustering import _selected_feature_specs, FeatureRole
    features=_selected_feature_specs(decoded_preparations[0].feature_specs,
        excluded_roles=(FeatureRole.NUCLEAR,FeatureRole.AUTOFLUORESCENCE,FeatureRole.DAPI))
    matrix,identities,_sources,_domains,_labels=_assemble_selected_matrix(decoded,resolved,features,len(selected),selected_cells=cells)
    labels,evidence=refit_marker_matrix(matrix,identities=identities,feature_specs=features,parent_evidence=parent.evidence,
        method=settings.method,method_settings=settings.method_settings,k=settings.k,resolution=settings.resolution,seed=settings.seed)
    return refined_result(parent,labels,evidence,settings=settings,parent_terminal_record_sha256=terminal.terminal_record_sha256,
        parent_result_ref_sha256=ref.artifact_ref_sha256,state_head_sha256=current.state_head_sha256,
        active_plan_sha256=current.active_plan_sha256,runtime_versions=_runtime_versions(settings.method))


def _with_assignments(parent, assignments, *, request_sha256, state_head_sha256,
                      active_plan_sha256, refinement):
    generation=generation_sha256(request_sha256=request_sha256,
        source_authority_sha256s=parent.source_authority_sha256s,entry_composition=parent.entry_composition,
        assignments=assignments,evidence=parent.evidence,runtime_versions=parent.runtime_versions,
        refinement=refinement)
    return replace(parent,request_sha256=request_sha256,source_state_head_sha256=state_head_sha256,
        active_plan_sha256=active_plan_sha256,assignments=assignments,
        entry_projections=label_projections(assignments,parent.entry_composition,generation),
        output_assignment_sha256=digest([row.to_mapping() for row in assignments]),
        clustering_generation_sha256=generation,refinement=refinement)


def refined_result(parent, labels, evidence, *, settings, parent_terminal_record_sha256,
                   parent_result_ref_sha256, state_head_sha256, active_plan_sha256, runtime_versions):
    """Replace exactly the selected memberships, retaining full per-cell lineage."""
    selected=frozenset(settings.cluster_ids)
    if settings.restore_parent or not selected or not selected<={row.cluster_id for row in parent.assignments}:
        raise BatchPhenotypingError('Select current parent clusters to refit.')
    identity=lambda row:CellClusteringIdentity(row.sample_id,row.project_entry_id,row.cell_id)
    subset=tuple(row for row in parent.assignments if row.cluster_id in selected)
    if set(labels)!={identity(row) for row in subset} or len(subset)!=evidence.node_count:
        raise BatchPhenotypingError('Refit membership must exactly cover the selected parent cells.')
    existing={row.cluster_id for row in parent.assignments}
    numbers=[int(match[1]) for label in existing if (match:=re.fullmatch(r'Cluster_(\d+)',label))]
    first=max(numbers,default=0)+1
    local=sorted(set(labels.values()))
    child_labels={label:f'Cluster_{first+i:03d}' for i,label in enumerate(local)}
    if existing.intersection(child_labels.values()):
        raise BatchPhenotypingError('Refit labels overlap existing cluster IDs.')
    assignments=tuple(replace(row,cluster_id=child_labels[labels[identity(row)]],
        parent_cluster_id=row.cluster_id,original_cluster_id=row.original_cluster_id or row.cluster_id)
        if row.cluster_id in selected else row for row in parent.assignments)
    refinement=BatchClusterRefinement(parent_terminal_record_sha256,parent_result_ref_sha256,
        parent.result_sha256,tuple(sorted(selected)),evidence,child_labels,runtime_versions)
    request=digest(dict(parent_result_sha256=parent.result_sha256,refit=settings.to_mapping()))
    return _with_assignments(parent,assignments,request_sha256=request,state_head_sha256=state_head_sha256,
        active_plan_sha256=active_plan_sha256,refinement=refinement)


def restored_parent_result(parent, *, discarded_result_sha256, state_head_sha256, active_plan_sha256):
    """Publish exact parent memberships through a successor without refitting."""
    request=digest(dict(restore_parent_result_sha256=parent.result_sha256,
                        discarded_result_sha256=discarded_result_sha256))
    return _with_assignments(parent,parent.assignments,request_sha256=request,state_head_sha256=state_head_sha256,
        active_plan_sha256=active_plan_sha256,refinement=parent.refinement)
