"""Export an inspected accepted H&E field with its native physical mapping."""
from io import BytesIO
import json
import math
import numpy as np


def aligned_cell_npz(bundle,index,alignment,options,mode,opacity):
    metadata=json.loads(str(bundle['metadata_json']))
    metadata['display_options']=dict(options)
    metadata['aligned_reference']={**alignment['metadata'],'cell_id':str(bundle['cell_ids'][index]),
        'cell_index':index,'origin_um':list(alignment['origin_um']),
        'pixel_size_um':list(alignment['pixel_size_um']),'display_mode':mode,'opacity':opacity,
        'pixel_axes':'channel,y,x','pixel_values':'native warped RGB, 0–255; NaN is unavailable'}
    output=BytesIO()
    np.savez_compressed(output,**{k:v for k,v in bundle.items() if k!='metadata_json'},
        aligned_reference_pixels=alignment['pixels'],metadata_json=np.asarray(json.dumps(metadata,sort_keys=True)))
    return output.getvalue()


def aligned_cell_figure(bundle,index,alignment,options,mode,opacity):
    from matplotlib.figure import Figure
    from matplotlib.colors import to_rgb
    left,right,bottom,top=map(float,bundle['extents_um'][index]);width=right-left;height=bottom-top
    figure=Figure(figsize=(8,max(2,8*height/width)),layout='constrained');axis=figure.subplots()
    if mode=='fluorescence_he':
        channels=list(map(int,bundle['channel_indices']));pixels=bundle['pixels'][index]
        rgb=np.zeros((*pixels.shape[1:],3),dtype=float)
        displayed=options.get('displayed_channel','composite')
        for channel in options['channels'] if displayed=='composite' else [int(displayed)]:
            low,high=options['limits'][str(channel)]
            signal=np.clip((pixels[channels.index(channel)]-low)/(high-low),0,1)
            colour=to_rgb(options.get('channel_colors',{}).get(str(channel),'#ffffff')) if displayed=='composite' or options.get('channel_display')=='colour' else (1,1,1)
            rgb+=np.nan_to_num(signal)[...,None]*colour
        axis.imshow(np.clip(rgb,0,1),extent=(left,right,bottom,top),interpolation='nearest')
    source=alignment['pixels'];valid=np.all(np.isfinite(source),axis=0)
    rgba=np.zeros((*source.shape[1:],4),dtype=float)
    rgba[...,:3]=np.clip(np.nan_to_num(source).transpose(1,2,0)/255,0,1)
    rgba[...,3]=valid*(opacity if mode=='fluorescence_he' else 1.)
    center=bundle['centers_um'][index];x,y=np.asarray(alignment['origin_um'])-center;px,py=alignment['pixel_size_um']
    axis.imshow(rgba,extent=(x,x+source.shape[2]*px,y+source.shape[1]*py,y),interpolation='nearest')
    for ring,cell in enumerate(bundle['outline_cell_indices']):
        if int(cell)!=index:continue
        kind=str(bundle['outline_kinds'][ring])
        if not options.get('show_nuclear_outlines' if kind=='nucleus' else 'show_cell_outlines',True):continue
        start,stop=bundle['outline_offsets'][ring:ring+2];points=bundle['outline_vertices_um'][start:stop]
        axis.plot(points[:,0],points[:,1],color='#ffe052' if kind=='nucleus' else '#22d3ee',linewidth=.8)
    axis.set(xlim=(left,right),ylim=(bottom,top));axis.set_axis_off()
    desired=width/6;decade=10**math.floor(math.log10(desired));length=max(n*decade for n in (1,2,5,10) if n*decade<=desired)
    x,y=left+.025*width,bottom-.06*height
    axis.plot((x,x+length),(y,y),color='black',linewidth=4);axis.plot((x,x+length),(y,y),color='white',linewidth=2)
    axis.text(x,y-.025*height,f'{length:g} µm',color='white',fontsize=9,bbox=dict(facecolor='black',alpha=.6,edgecolor='none',pad=2))
    return figure
