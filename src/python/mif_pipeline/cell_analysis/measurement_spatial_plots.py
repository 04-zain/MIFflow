"""Export the currently inspected measurement field without further source reads."""
import csv
import json
import math
from io import BytesIO, StringIO

import numpy as np


def density_rgba(grid):
    """Smooth the overview colour on the fixed count grid, leaving counts intact.

    One grid-cell Gaussian sigma suppresses tile edges without changing with
    viewport zoom. Exact counts and dense-field navigation use the original grid.
    """
    from scipy.ndimage import gaussian_filter

    density = gaussian_filter(np.asarray(grid, dtype=float), sigma=1,
                              mode='constant', truncate=2)
    rgba = np.zeros((*density.shape, 4), dtype=np.uint8)
    rgba[..., :3] = (255, 168, 45)
    peak = density.max(initial=0)
    if peak > 0:
        rgba[..., 3] = np.rint(220 * np.sqrt(density / peak)).astype(np.uint8)
    return rgba


def aggregate_count_grid(grid, factor=1):
    """Sum equal spatial tiles for display while retaining every counted cell.

    The factor divides both grid dimensions, so the resulting tiles cover the
    same physical extent. No interpolation or smoothing changes their counts.
    The original grid remains available in the exported data.
    """
    values = np.asarray(grid)
    factor = int(factor)
    if factor < 1 or values.ndim != 2 or any(size % factor for size in values.shape):
        raise ValueError('The display aggregation must divide both count-grid dimensions.')
    if factor == 1:
        return values
    height, width = values.shape
    return values.reshape(height // factor, factor, width // factor, factor).sum(axis=(1, 3))


def field_npz(bundle, options):
    """Retain the exact bounded pixels, spatial counts, geometry and display choices."""
    stream=BytesIO()
    metadata={**bundle['metadata'],'display_options':dict(options)}
    arrays={key:value for key,value in bundle.items() if key!='metadata'}
    np.savez_compressed(stream,**arrays,metadata_json=np.asarray(json.dumps(metadata,sort_keys=True)))
    return stream.getvalue()


def field_cells_csv(bundle):
    """Export the visible retained cells; completeness is explicit in each row."""
    stream=StringIO(newline='');writer=csv.writer(stream)
    metadata=bundle['metadata']
    if metadata.get('review_kind') == 'segmentation':
        keys=('sample_id','project_entry_id','slide_id','section_id','cell_id','x_um','y_um')
        writer.writerow((*keys,'all_visible_outlines_available'))
        for row in metadata.get('visible_cells',()):writer.writerow((*[row[key] for key in keys],metadata['outline_detail_available']))
        return stream.getvalue().encode('utf-8')
    selection_columns = ('phenotype',) if metadata.get('phenotype') is not None else ()
    selection_values = (metadata['phenotype'],) if selection_columns else ()
    region = metadata.get('display_options', {}).get('region')
    identity_columns = ()
    if region is not None:
        selection_columns += ('region_selection',)
        selection_values += (json.dumps(region, sort_keys=True),)
        identity_columns = tuple(key for key in ('sample_id','project_entry_id','slide_id','section_id','analysis_domain_id')
            if all(key in row for row in metadata.get('visible_cells', ())))
    writer.writerow(('cell_id','saved_value','selected','x_um','y_um','field_selected','field_measured','all_visible_outlines_available',*identity_columns,*selection_columns))
    for row in metadata.get('visible_cells',()):
        writer.writerow((row['cell_id'],row['value'],row['selected'],row['x_um'],row['y_um'],
            metadata['field_selected'],metadata['field_measured'],metadata['outline_detail_available'],
            *(row[key] for key in identity_columns),*selection_values))
    return stream.getvalue().encode('utf-8')


def field_figure(bundle, options):
    """Draw the source pixels and retained contours in their physical field."""
    from matplotlib.figure import Figure
    from matplotlib.colors import to_rgb
    extent=tuple(map(float,bundle['extent_um']))
    width,height=extent[1]-extent[0],extent[2]-extent[3]
    figure=Figure(figsize=(8,max(3,min(7,8*height/width))),layout='constrained')
    axis=figure.add_subplot()
    available=list(map(int,bundle['channel_indices']))
    mode=options.get('displayed_channel','composite')
    aligned=mode in ('he','fluorescence_he')
    channel=options.get('fluorescence_channel','composite') if aligned else mode
    selected=options['channels'] if channel=='composite' else [int(channel)]
    rgb=np.zeros((*bundle['pixels'].shape[1:],3));valid=np.ones(rgb.shape[:2],dtype=bool)
    for channel in selected:
        pixels=bundle['pixels'][available.index(channel)]
        low,high=options['limits'][str(channel)]
        values=np.clip((pixels-low)/(high-low),0,1);valid&=np.isfinite(values)
        color=to_rgb(options.get('channel_colors',{}).get(str(channel),'#ffffff')) if options.get('channel_display')=='colour' else (1,1,1)
        rgb+=np.nan_to_num(values)[...,None]*color
    rgb=np.clip(rgb,0,1);rgb[~valid]=(218/255,221/255,225/255)
    if mode != 'he':
        axis.imshow(rgb,extent=extent,interpolation='nearest')
    if aligned:
        source=np.asarray(bundle['aligned_reference_pixels'])
        rgba=np.zeros((*source.shape[1:],4),dtype=float)
        rgba[...,:3]=np.clip(np.nan_to_num(source).transpose(1,2,0)/255,0,1)
        rgba[...,3]=np.all(np.isfinite(source),axis=0)*(options['alignment_opacity'] if mode=='fluorescence_he' else 1.)
        x,y=map(float,bundle['aligned_reference_origin_um'])
        px,py=map(float,bundle['aligned_reference_pixel_size_um'])
        axis.imshow(rgba,extent=(x,x+source.shape[2]*px,y+source.shape[1]*py,y),interpolation='nearest')
    metadata=bundle['metadata']
    if not metadata['outline_detail_available'] and metadata.get('review_kind') != 'segmentation':
        axis.imshow(density_rgba(bundle['grid_selected']),
                    extent=bundle['grid_extent_um'],interpolation='bilinear')
    rows=metadata.get('visible_cells',())
    for ring,index in enumerate(bundle['outline_cell_indices']):
        kind=str(bundle['outline_kinds'][ring])
        if not options.get('show_nuclear_outlines' if kind=='nucleus' else 'show_cell_outlines',True):continue
        start,stop=bundle['outline_offsets'][ring:ring+2];vertices=bundle['outline_vertices_um'][start:stop];row=rows[int(index)]
        selected_identity = options.get('selected_cell_identity')
        active = (tuple(row.get(key) for key in ('sample_id','project_entry_id','slide_id','section_id','cell_id')) == tuple(selected_identity)
                  if selected_identity is not None else row['cell_id'] == options.get('selected_cell_id'))
        plain=metadata.get('review_kind')=='segmentation';selected=row.get('selected',plain)
        color='#ff6b6b' if active else ('#ffd48a' if kind=='nucleus' else '#60d9c1' if plain else '#ffb547') if selected else '#b9c2c5'
        axis.plot(vertices[:,0],vertices[:,1],color=color,linewidth=1.2 if active else .7,
                  alpha=1. if active or selected and kind=='cell' else .75 if selected else .33)
    axis.set(xlim=(extent[0],extent[1]),ylim=(extent[2],extent[3]))
    axis.set_axis_off()
    desired=width/6;decade=10**math.floor(math.log10(desired))
    length=max(v*decade for v in (1,2,5,10) if v*decade<=desired)
    left,bottom=extent[0]+.025*width,extent[2]-.05*height
    axis.plot([left,left+length],[bottom,bottom],color='black',linewidth=4)
    axis.plot([left,left+length],[bottom,bottom],color='white',linewidth=2)
    axis.text(left,bottom-.025*height,f'{length:g} µm',color='white',fontsize=9,
        bbox={'facecolor':'black','alpha':.65,'edgecolor':'none','pad':2})
    return figure
