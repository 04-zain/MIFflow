package org.mif.cells;

import java.awt.Color;
import java.awt.Graphics2D;
import java.util.Set;
import java.util.UUID;
import qupath.lib.gui.viewer.OverlayOptions;
import qupath.lib.gui.viewer.PathObjectPainter;
import qupath.lib.objects.PathObject;

/** Use QuPath's cell/nucleus, fill and colour choices for prepared native cells. */
public final class CellSnapshotPainter {
    private CellSnapshotPainter() {}
    public static void paint(Graphics2D graphics,CellDatasetProvider.Snapshot snapshot,double downsample,
            OverlayOptions options,Set<UUID> hierarchyIds) {
        if(!options.getShowDetections()||snapshot.retired())return;
        if(snapshot.overview()) {
            int[] density=snapshot.density();if(density==null)return;double width=snapshot.bounds().getWidth()/64,height=snapshot.bounds().getHeight()/64;
            int maximum=java.util.Arrays.stream(density).max().orElse(1);
            for(int y=0;y<64;y++)for(int x=0;x<64;x++)if(density[y*64+x]>0) {
                graphics.setColor(new Color(255,200,0,Math.max(40,(int)(190.0*density[y*64+x]/maximum))));
                graphics.fill(new java.awt.geom.Rectangle2D.Double(snapshot.bounds().getMinX()+x*width,snapshot.bounds().getMinY()+y*height,width,height));
            }
        } else for(var object:snapshot.objects())paintObject(graphics,object,downsample,options,hierarchyIds);
    }
    /** Native hierarchy cells already have their own painter, including selected-cell styling. */
    public static void paintObject(Graphics2D graphics,PathObject object,double downsample,
            OverlayOptions options,Set<UUID> hierarchyIds) {
        if(options.getShowDetections()&&!hierarchyIds.contains(object.getID()))
            PathObjectPainter.paintObject(object,graphics,options,null,downsample);
    }
}
