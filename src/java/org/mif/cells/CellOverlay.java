package org.mif.cells;

import java.awt.Color;
import java.awt.Graphics2D;
import java.awt.image.BufferedImage;
import java.util.concurrent.CompletableFuture;
import javafx.application.Platform;
import javafx.scene.input.MouseEvent;
import org.locationtech.jts.geom.Envelope;
import qupath.lib.gui.viewer.QuPathViewer;
import qupath.lib.gui.viewer.overlays.PathOverlay;
import qupath.lib.gui.viewer.tools.PathTools;
import qupath.lib.images.ImageData;
import qupath.lib.objects.PathObject;
import qupath.lib.objects.hierarchy.events.PathObjectHierarchyEvent;
import qupath.lib.objects.hierarchy.events.PathObjectHierarchyListener;
import qupath.lib.regions.ImageRegion;

/** Paint completed snapshots; all storage and native geometry work stays off FX. */
public final class CellOverlay implements PathOverlay, AutoCloseable, PathObjectHierarchyListener {
    private final QuPathViewer viewer;
    private ImageData<BufferedImage> image;
    private CompletableFuture<NativeCellCollection> provider;
    private String binding;
    private Object componentSelection;
    private CellDatasetProvider.Snapshot snapshot;
    private Envelope desired;
    private long sequence;
    private String failure;
    private boolean installingSelection;
    private PathObject cleanSelection;
    private final javafx.event.EventHandler<MouseEvent> clickHandler=this::selectCell;
    private final javafx.beans.value.ChangeListener<ImageData<BufferedImage>> imageListener=(observable,oldImage,newImage)->attach(newImage);

    public CellOverlay(QuPathViewer viewer) {
        this.viewer=viewer;
        viewer.imageDataProperty().addListener(imageListener);
        viewer.getView().addEventHandler(MouseEvent.MOUSE_CLICKED,clickHandler);
        attach(viewer.getImageData());
    }
    private void attach(ImageData<BufferedImage> current) {
        if(image!=null)image.getHierarchy().removeListener(this);
        image=current;provider=null;snapshot=null;desired=null;failure=null;sequence++;cleanSelection=null;
        binding=current==null?null:(String)current.getProperty(CellDatasetProvider.DATASET_PROPERTY);
        componentSelection=current==null?null:current.getProperty(CellComponents.PROPERTY);
        if(current!=null) {
            current.getHierarchy().addListener(this);
        }
    }
    @Override public void paintOverlay(Graphics2D graphics,ImageRegion region,double downsample,ImageData<BufferedImage> current,boolean paintCompletely) {
        var options=viewer.getOverlayOptions();
        if(current==null||!options.getShowDetections())return;
        if(paintCompletely&&!Platform.isFxApplicationThread()) {
            CellFigureExport.paintCells(graphics,current,region,downsample,options);
            return;
        }
        if(current!=image||!java.util.Objects.equals(binding,current.getProperty(CellDatasetProvider.DATASET_PROPERTY)))attach(current);
        Object results=current.getProperty(CellComponents.PROPERTY);
        if(!java.util.Objects.equals(results,componentSelection)){componentSelection=results;desired=null;snapshot=null;sequence++;}
        if(provider==null && current.getProperty(CellDatasetProvider.DATASET_PROPERTY)!=null)provider=NativeCellScope.open(current);
        if(provider==null)return;
        Envelope bounds=new Envelope(region.getMinX(),region.getMaxX(),region.getMinY(),region.getMaxY());
        // QuPath supplies the server bounds; its image-coordinate clip identifies the viewport.
        var clip=graphics.getClipBounds();
        if(clip!=null)bounds=bounds.intersection(new Envelope(clip.getMinX(),clip.getMaxX(),clip.getMinY(),clip.getMaxY()));
        if(bounds.isNull())return;
        if(snapshot!=null&&snapshot.retired()){snapshot=null;desired=null;}
        if(!bounds.equals(desired))submit(bounds);
        var available=snapshot;
        if(available!=null)CellSnapshotPainter.paint(graphics,available,downsample,options,CellFigureExport.hierarchyCellIds(current,region));
        if(available==null || !bounds.equals(available.bounds()) || available.overview() || failure!=null) {
            graphics.setColor(Color.WHITE);
            String status=failure!=null?"Cell data unavailable: "+failure:available!=null&&available.overview()?"Cell density overview":"Loading cell outlines";
            graphics.drawString(status,(float)(bounds.getMinX()+12*downsample),(float)(bounds.getMinY()+22*downsample));
        }
    }
    private void submit(Envelope bounds) {
        desired=new Envelope(bounds);long requested=++sequence;ImageData<BufferedImage> requestedImage=image;failure=null;
        provider.thenCompose(owner->owner.request(bounds)).whenComplete((result,error)->Platform.runLater(()->{
            if(image!=requestedImage||sequence!=requested){viewer.repaint();return;}
            if(error==null)snapshot=result;
            else {Throwable cause=error instanceof java.util.concurrent.CompletionException?error.getCause():error;failure=cause.getMessage();}
            viewer.repaint();
        }));
    }
    private void selectCell(MouseEvent event) {
        // Drawing and brush clicks belong to the active native tool. A delayed
        // detection lookup must not replace an annotation being authored.
        if(viewer.getActiveTool()!=PathTools.MOVE||!viewer.getOverlayOptions().getShowDetections()||provider==null||event.getButton()!=javafx.scene.input.MouseButton.PRIMARY||event.getClickCount()!=1)return;
        var point=viewer.componentPointToImagePoint(event.getX(),event.getY(),null,false);
        Envelope area=new Envelope(point.getX(),point.getX(),point.getY(),point.getY());
        long requested=sequence;ImageData<BufferedImage> requestedImage=image;
        provider.thenCompose(owner->owner.request(area)).whenComplete((result,error)->{
            if(error!=null||result.objects().isEmpty())return;
            PathObject chosen=result.objects().get(0);
            // Reopen the exact ID with measurements outside FX before handing native tools the cell.
            provider.thenCompose(owner->owner.lookupAsync(chosen.getMetadata().get("mif.cell_id"))).whenComplete((cell,readError)->Platform.runLater(()->{
                if(readError!=null||cell==null||image!=requestedImage||sequence!=requested||viewer.getActiveTool()!=PathTools.MOVE)return;
                installingSelection=true;
                try {
                    if(cleanSelection!=null)image.getHierarchy().removeObject(cleanSelection,true);
                    cleanSelection=cell;image.getHierarchy().addObject(cell);
                    image.getHierarchy().getSelectionModel().setSelectedObject(cell);
                }finally{installingSelection=false;}
                viewer.repaint();
            }));
        });
    }
    @Override public void hierarchyChanged(PathObjectHierarchyEvent event) {
        if(installingSelection||event.isChanging()||provider==null)return;
        if(provider.isCompletedExceptionally())return;var ready=provider.getNow(null);if(ready==null)return;
        for(var object:event.getChangedObjects()) {
            if(!ready.ownsPopulation(object.getMetadata().get("mif.population_id")))continue;
            boolean removed=event.getEventType().name().contains("REMOVED");
            ready.recordEdit(image,object,removed);
            if(object==cleanSelection)cleanSelection=null;
            sequence++;desired=null; // Changed geometry selects a new query revision.
        }
        viewer.repaint();
    }
    @Override public void close() {
        if(image!=null)image.getHierarchy().removeListener(this);
        viewer.getView().removeEventHandler(MouseEvent.MOUSE_CLICKED,clickHandler);
        viewer.imageDataProperty().removeListener(imageListener);
        snapshot=null;provider=null;image=null;
    }
}
