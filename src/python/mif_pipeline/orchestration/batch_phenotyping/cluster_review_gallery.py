"""Bounded exact cell examples for one authenticated cluster and image."""
from contextlib import ExitStack
import hashlib
import heapq


def render_cluster_cells(snapshot,evidence,options):
    """The caller authenticates the pending aggregate review before this read."""
    from .review_execution import _validate_sources
    from .marker_review_gallery import measurement_cells
    from ..saved_cell_gallery import gallery_resources,load_cell_crops,display_options
    from ..saved_cell_figures import _MeasuredCells
    from mif_pipeline.cell_analysis.gallery_plots import render_cell_gallery

    cluster_id=options.get('cluster_id')
    cluster=next((row for row in evidence.clusters if row.cluster_id==cluster_id),None)
    if cluster is None:raise ValueError('Select a cluster from the current review.')
    image_identity=options.get('image_identity')
    offered={(row.sample_id,row.project_entry_id,row.slide_id) for row in cluster.section_counts}
    if not isinstance(image_identity,(list,tuple)) or tuple(image_identity) not in offered:
        raise ValueError('Select an image containing the current cluster.')
    sample,entry,slide=image_identity
    count=options.get('count',4);seed=options.get('seed',0)
    if type(count) is not int or not 1<=count<=8 or type(seed) is not int or not 0<=seed<=2**31-1:
        raise ValueError('Choose one to eight cell examples and a nonnegative selection seed.')
    authorities={a.artifact_ref.artifact_ref_sha256:a for a in snapshot.committed_artifact_authorities}
    clustering=authorities[evidence.clustering_result_ref_sha256]
    sources=tuple(authorities[key] for key in evidence.source_authority_sha256s)
    result,resolved,features=_validate_sources(clustering_authority=clustering,source_authorities=sources,
        maximum_clustering_result_byte_count=max(a.artifact_ref.byte_count or 0 for a in (clustering,*sources)))
    matching=lambda row: row.cluster_id==cluster_id and (row.sample_id,row.project_entry_id,row.slide_id)==tuple(image_identity)
    total=sum(matching(row) for row in result.assignments)
    rank=lambda row:(hashlib.blake2b(f'{seed}:{row.cell_id}'.encode(),digest_size=16).digest(),row.cell_id)
    requested=options.get('cell_ids')
    if requested is not None:
        if (not isinstance(requested,(list,tuple)) or not 1<=len(requested)<=8
                or any(type(identity) is not str or not identity for identity in requested)
                or len(set(requested))!=len(requested)):
            raise ValueError('Choose up to eight distinct cells from the selected cluster and image.')
        found={row.cell_id:row for row in result.assignments if matching(row) and row.cell_id in requested}
        if set(found)!=set(requested):
            raise ValueError('A selected cell does not belong to this cluster and image.')
        chosen=[found[identity] for identity in requested]
    else:
        chosen=heapq.nsmallest(count,(row for row in result.assignments if matching(row)),key=rank)
    ids=tuple(row.cell_id for row in chosen)
    if not ids:raise ValueError('This cluster has no cells in the selected image.')
    selected={row.cell_id:row for row in chosen}
    rows={};frame=None;scope=None;first=None
    with ExitStack() as resources:
        for preparation,measurement in resolved:
            if (measurement.sample_id,measurement.project_entry_id,measurement.slide_id)!=tuple(image_identity):continue
            section_ids=[identity for identity,row in selected.items() if row.section_id==measurement.section_id]
            if not section_ids:continue
            authority=authorities[preparation.measurement_result_ref_sha256]
            if first is None:
                resources.enter_context(gallery_resources(snapshot,authority))
            with measurement_cells(measurement) as (view,component,current_scope):
                current_frame=(measurement.canonical_image_id,measurement.canonical_image_sha256,
                    current_scope['x_um_per_px'],current_scope['y_um_per_px'])
                if frame is not None and frame!=current_frame:raise ValueError('The selected cells do not share one accepted image frame.')
                frame,scope,first=current_frame,current_scope,measurement
                cells=_MeasuredCells(view,component,measurement,current_scope)
                for batch in cells.lookup_ids(section_ids,columns=('cell_id','x_level0','y_level0','cell_geometry','nucleus_geometry')):
                    for row in batch.to_pylist():
                        if row['cell_id'] in rows:raise ValueError('A cluster example has ambiguous measured geometry.')
                        rows[row['cell_id']]=row
        if set(rows)!=set(ids):raise ValueError('A cluster example is absent from its accepted measurements.')
        image=next((a.artifact_ref for a in snapshot.committed_artifact_authorities
            if a.artifact_ref.artifact_kind=='processed_image' and a.artifact_ref.scope.sample_id==sample
            and a.artifact_ref.sha256==frame[1]),None)
        if image is None:raise ValueError('The accepted cluster image is unavailable.')
        settings={'channel_display':'colour','merged_panel':True,**options,'cell_ids':list(ids),'selection_count':total}
        if 'channels' not in settings:
            channel_id=next((item.channel_id for item in first.measurement_plan.features if item.measurement==options.get('measurement')),None)
            target=next((i for i,c in enumerate(first.canonical_channels) if c['channel_id']==channel_id),None)
            nuclear=next((i for i,c in enumerate(first.canonical_channels) if c['name'].casefold() in {'dapi','dna','hoechst'}),None)
            settings['channels']=list(dict.fromkeys([target]+([nuclear] if nuclear is not None else []))) if target is not None else list(range(min(3,len(first.canonical_channels))))
        data=load_cell_crops(image.locator,rows,ids,(cluster_id,)*len(ids),settings,
            image_id=frame[0],image_sha256=frame[1],pixel_size_um=frame[2:])
        appearance=display_options(data,settings)
        appearance.update(available_channels=[dict(index=i,label=c['name']) for i,c in enumerate(first.canonical_channels)],
            sample_id=sample,image_label=sample,include_pixels=True,compact=True,export_basename='cluster-cell-examples',
            review_caption=(f'{cluster_id} · Selected cell in {sample}' if requested and len(ids)==1 else
                f'{cluster_id} · {len(ids)} selected cells in {sample}' if requested else
                f'{cluster_id} · {len(ids)} repeatable examples from {total:,} cells in {sample}'),
            cluster_request=dict(options))
        outputs=render_cell_gallery(data,appearance)
    return tuple(('cluster-cell-examples/'+identity.split('/',1)[-1],title,fmt,payload)
                 for identity,title,fmt,payload in outputs),appearance
