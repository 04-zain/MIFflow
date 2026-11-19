package org.mif.cells;

import java.util.IdentityHashMap;
import java.util.Map;
import javafx.collections.ListChangeListener;
import qupath.lib.gui.QuPathGUI;
import qupath.lib.gui.extensions.QuPathExtension;
import qupath.lib.gui.viewer.QuPathViewer;

/** Install the disk-backed overlay in QuPath's existing image viewers. */
public final class CellDatasetExtension implements QuPathExtension {
    @Override public String getName(){return "mIF native data access";}
    @Override public String getDescription(){return "Display and edit the selected spatial cell population without loading the entire dataset.";}
    @Override public void installExtension(QuPathGUI qupath) {
        qupath.installCommand("mIF>Export image…",()->CellFigureExport.prompt(qupath));
        Map<QuPathViewer,CellOverlay> overlays=new IdentityHashMap<>();
        for(var viewer:qupath.getAllViewers()){var overlay=new CellOverlay(viewer);overlays.put(viewer,overlay);viewer.getCustomOverlayLayers().add(overlay);}
        qupath.getAllViewers().addListener((ListChangeListener<QuPathViewer>)change->{
            while(change.next()) {
                for(var removed:change.getRemoved()){var overlay=overlays.remove(removed);if(overlay!=null)overlay.close();}
                for(var viewer:change.getAddedSubList()){var overlay=new CellOverlay(viewer);overlays.put(viewer,overlay);viewer.getCustomOverlayLayers().add(overlay);}
            }
        });
    }
}
