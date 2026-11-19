"""Compare connected staining regions with complete saved cell outlines."""
from __future__ import annotations

import numpy as np


def native_pixels_available(bundle):
    """Require original pixels rather than a pyramid or display sample."""
    metadata = bundle['metadata']
    return metadata.get('pyramid_level') == 0 and not metadata.get('display_sampling')


def stain_outline_relationships(bundle, *, channel, threshold, compartment, min_area_um2):
    """Describe connected native staining and complete saved outlines in one field.

    Finite pixels strictly above the supplied threshold connect through any of
    their eight neighbours. No smoothing, watershed or inferred nuclei are used.
    Region and contour edge truncation is excluded explicitly. Raster overlap
    uses pixel centres and even/odd rings, preserving holes and multipolygons.
    """
    from scipy import ndimage
    from skimage.measure import grid_points_in_poly

    if not native_pixels_available(bundle) or not bundle['metadata'].get('outline_detail_available',True):
        raise ValueError('Zoom in to compare original staining pixels with complete outlines.')
    channels=list(map(int,bundle['channel_indices']))
    if channel not in channels or compartment not in {'cell','nucleus'}:
        raise ValueError('Choose a loaded stain and an available outline compartment.')
    if not np.isfinite(threshold) or not np.isfinite(min_area_um2) or min_area_um2<0:
        raise ValueError('Enter a finite threshold and a nonnegative minimum stain area.')
    signal=np.asarray(bundle['pixels'])[channels.index(channel)]
    if signal.size>1024**2:
        raise ValueError('Zoom in to compare at most 1,048,576 original-image pixels.')
    height,width=signal.shape
    left,right,bottom,top=map(float,bundle['extent_um'])
    spacing=np.array([(right-left)/width,(bottom-top)/height])
    labels,count=ndimage.label(np.isfinite(signal)&(signal>threshold),structure=np.ones((3,3),dtype=bool))
    sizes=np.bincount(labels.ravel(),minlength=count+1)
    clipped=set(map(int,np.unique(np.concatenate([labels[0],labels[-1],labels[:,0],labels[:,-1]]))))-{0}
    retained=[i for i in np.flatnonzero(sizes*spacing.prod()>=min_area_um2) if i and i not in clipped]
    if len(retained)>4096:
        raise ValueError('Increase the minimum stain area or zoom in to compare fewer than 4,096 regions.')
    keep=np.zeros(count+1,dtype=bool);keep[retained]=True
    labels[~keep[labels]]=0
    centers=ndimage.center_of_mass(np.ones(signal.shape,dtype=np.uint8),labels,retained) if retained else []
    boxes=ndimage.find_objects(labels)
    regions={int(i):dict(region_id=int(i),area_um2=float(sizes[i]*spacing.prod()),
        center_um=[float(left+(center[1]+.5)*spacing[0]),float(top+(center[0]+.5)*spacing[1])],
        bounds_um=[left+boxes[i-1][1].start*spacing[0],top+boxes[i-1][0].start*spacing[1],
            left+boxes[i-1][1].stop*spacing[0],top+boxes[i-1][0].stop*spacing[1]],cell_indices=[])
        for i,center in zip(retained,centers)}
    outlines=[];clipped_outlines=0;work=0
    grouped={}
    for i,(index,kind) in enumerate(zip(bundle['outline_cell_indices'],bundle['outline_kinds'])):
        if str(kind)==compartment:
            grouped.setdefault(int(index),[]).append(np.asarray(bundle['outline_vertices_um'][bundle['outline_offsets'][i]:bundle['outline_offsets'][i+1]],dtype=float))
    if len(grouped)>4096:
        raise ValueError('Zoom in to compare fewer than 4,096 outlines.')
    for index,rings in sorted(grouped.items()):
        vertices=np.concatenate(rings);low,high=vertices.min(axis=0),vertices.max(axis=0)
        if np.any(low<=(left,top)) or np.any(high>=(right,bottom)):
            clipped_outlines+=1;continue
        origin=np.array([left,top])
        x0,y0=np.maximum(0,np.floor((low-origin)/spacing).astype(int))
        x1,y1=np.minimum((width,height),np.ceil((high-origin)/spacing).astype(int))
        if x1<=x0 or y1<=y0:continue
        work+=(x1-x0)*(y1-y0)
        if work>16*1024**2:
            raise ValueError('Zoom in to compare this dense set of outlines.')
        mask=np.zeros((y1-y0,x1-x0),dtype=bool)
        for ring in rings:
            local=(ring-origin)/spacing-(x0+.5,y0+.5)
            mask^=grid_points_in_poly(mask.shape,local[:,::-1])
        values,counts=np.unique(labels[y0:y1,x0:x1][mask],return_counts=True)
        overlap={int(i):int(n) for i,n in zip(values,counts) if i}
        row=bundle['metadata']['visible_cells'][index]
        outlines.append(dict(cell_index=index,cell_id=row['cell_id'],region_ids=list(overlap),
            overlap_pixels=list(overlap.values()),area_um2=float(mask.sum()*spacing.prod()),
            center_um=list(map(float,(low+high)/2))))
        for i in overlap:regions[i]['cell_indices'].append(index)
    labels.setflags(write=False)
    return dict(labels=labels,regions=list(regions.values()),outlines=outlines,
        clipped_region_count=len(clipped),clipped_outline_count=clipped_outlines,
        channel=int(channel),channel_name=str(bundle['channel_names'][channels.index(channel)]),
        threshold=float(threshold),min_area_um2=float(min_area_um2),compartment=compartment,
        pixel_size_um=spacing.tolist(),extent_um=list(map(float,bundle['extent_um'])),
        rule='finite native pixels > threshold; 8-connected; complete field regions and outlines')

