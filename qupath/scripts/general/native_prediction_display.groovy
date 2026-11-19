// QuPath 0.7 compatibility adapter for its existing classifier windows.
// Retain the exact native overlay, model, queue and controls. Only its lazy
// display server receives the shared palette before that viewer paints it.
import java.lang.reflect.Field
import javafx.application.Platform
import javafx.beans.value.ChangeListener
import javafx.collections.ObservableList
import javafx.collections.ListChangeListener
import javafx.stage.WindowEvent
import qupath.lib.common.GeneralTools
import qupath.lib.gui.viewer.overlays.PixelClassificationOverlay
import qupath.lib.classifiers.pixel.PixelClassificationImageServer

final class MifNativePredictionDisplay implements AutoCloseable {
    static final String KEY="mif.pipeline.native-prediction-display/v1"
    private final Object gui
    private final Map policy
    private final Field servers
    private final Map bindings=new IdentityHashMap()
    private final ObservableList viewers
    private final ListChangeListener viewerListListener
    private final javafx.event.EventHandler hiddenListener
    private boolean closed=false

    static install(gui,Map policy) {
        if(!Platform.isFxApplicationThread())throw new IllegalStateException("Install classifier display on the JavaFX thread.")
        def previous=gui.stage.properties[KEY]
        if(previous!=null)return previous
        def installed=new MifNativePredictionDisplay(gui,policy)
        gui.stage.properties[KEY]=installed
        return installed
    }

    private MifNativePredictionDisplay(gui,Map policy) {
        if(GeneralTools.getVersion()!="0.7.0")throw new IllegalArgumentException("The native classifier display adapter requires QuPath 0.7.0.")
        if(!(policy.server instanceof Closure))throw new IllegalArgumentException("The classifier display policy is unavailable.")
        this.gui=gui;
        this.policy=policy
        // QuPath exposes the native prediction server but has no public setter
        // for its palette. Keep this single version-pinned access isolated and
        // tested; do not replace the overlay or touch classifier metadata.
        servers=PixelClassificationOverlay.getDeclaredField("cachedServers")
        if(!Map.isAssignableFrom(servers.type) || !servers.trySetAccessible())
            throw new IllegalStateException("This QuPath build does not support the classifier display integration.")
        viewers=gui.viewerManager.allViewers
        viewerListListener={change ->
            while(change.next()) {
                if(change.wasRemoved())change.removed.each {detach(it)}
                if(change.wasAdded())change.addedSubList.each {attach(it)}
            }
        } as ListChangeListener
        hiddenListener={event ->close()} as javafx.event.EventHandler
        try {
            viewers.addListener(viewerListListener)
            new ArrayList(viewers).each {attach(it)}
            gui.stage.addEventHandler(WindowEvent.WINDOW_HIDDEN,hiddenListener)
        }catch(Throwable failure){close();
            throw failure}
    }

    private void attach(viewer) {
        if(closed || bindings.containsKey(viewer))return
        def layers=viewer.overlayLayers
        if(!(layers instanceof ObservableList))throw new IllegalStateException("The native viewer no longer exposes overlay changes.")
        ListChangeListener overlayListener={change ->refresh(viewer)} as ListChangeListener
        ChangeListener imageListener={value,oldImage,newImage ->refresh(viewer)} as ChangeListener
        bindings[viewer]=[layers:layers,overlayListener:overlayListener,imageListener:imageListener]
        layers.addListener(overlayListener);
        viewer.imageDataProperty().addListener(imageListener)
        refresh(viewer)
    }

    private void refresh(viewer) {
        if(closed)return
        if(!Platform.isFxApplicationThread())throw new IllegalStateException("Classifier display changed outside the JavaFX thread.")
        def image=viewer.imageData,overlay=viewer.customPixelLayerOverlay
        if(image==null || overlay==null || overlay.class!=PixelClassificationOverlay)return
        synchronized(overlay) {
            def source=overlay.getPixelClassificationServer(image)
            // Exclude feature images, custom operation servers, and the
            // already palette-configured pipeline server subclass.
            if(source==null || source.class!=PixelClassificationImageServer || !source.imageData.is(image))return
            def replacement=policy.server(image,source.classifier)
            if(replacement==null)return // Preserve probability/non-indexed display.
            if(replacement.metadata!=source.metadata || !replacement.classifier.is(source.classifier))
                throw new IllegalStateException("Display setup attempted to change classifier inputs or outputs.")
            Map map=servers.get(overlay)
            if(!map.get(image).is(source))throw new IllegalStateException("The prediction server changed while its display was configured.")
            map.put(image,replacement)
        }
    }

    private void detach(viewer) {
        def binding=bindings.remove(viewer)
        if(binding==null)return
        binding.layers.removeListener(binding.overlayListener)
        viewer.imageDataProperty().removeListener(binding.imageListener)
    }

    int getBoundViewerCount(){bindings.size()}

    void close() {
        if(closed)return
        closed=true
        viewers?.removeListener(viewerListListener)
        new ArrayList(bindings.keySet()).each {detach(it)}
        gui.stage.removeEventHandler(WindowEvent.WINDOW_HIDDEN,hiddenListener)
        if(gui.stage.properties[KEY].is(this))gui.stage.properties.remove(KEY)
        // Native classifier windows retain ownership of their overlay shutdown.
    }
}
return [install:MifNativePredictionDisplay.&install]
