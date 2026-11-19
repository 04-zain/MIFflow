// Workflow guard for pipeline-owned QuPath sessions. It is not a sandbox for hostile scripts.
import javafx.application.Platform
import javafx.collections.ListChangeListener
import javafx.beans.value.ChangeListener
import javafx.scene.control.Menu
import javafx.stage.WindowEvent
import qupath.lib.gui.actions.ActionTools
import qupath.lib.gui.localization.QuPathResources
import qupath.lib.gui.viewer.overlays.PixelClassificationOverlay
import qupath.fx.dialogs.Dialogs

final class MifReferenceFirstGuard implements AutoCloseable {
    static final String KEY="mif.pipeline.reference-first/v1"
    static final String MESSAGE="Save and close the reference annotations, then select Freeze references and fit in the classifier step. To enable predictions during annotation, select Show predictions while annotating before opening the draft."
    private final Object gui
    private final Closure explain
    private final List restored=[]
    private boolean closed=false
    private final List bindings=[]
    private final Set approved=Collections.newSetFromMap(new IdentityHashMap())
    private final ListChangeListener viewerListener
    private final javafx.event.EventHandler hidden
    MifReferenceFirstGuard(gui,Closure explain) {
        this.gui=gui;
        this.explain=explain
        Set names=["trainPixelClassifier","loadPixelClassifier","createThresholder"].collect {
            QuPathResources.getString("Action.Processing.Classify."+it)
        } as Set
        Closure visit
        visit={items->items.each {item->
            if(item instanceof Menu)visit(item.items)
            else if(names.contains(item.text)) {
                def action=ActionTools.getActionProperty(item)
                if(action==null)throw new IllegalStateException("The installed QuPath classifier command cannot be controlled by this workflow.")
                def old=action.eventHandler;
                    boolean visible=item.visible
                restored.add([action:action,handler:old,item:item,visible:visible])
            }
        }}
        visit(gui.menuBar.menus)
        if(restored.size()!=3)throw new IllegalStateException("The expected native classifier commands are unavailable. Use the supported QuPath 0.7.0 installation.")
        restored.each {row->row.action.setEventHandler({event->this.explain(MESSAGE)} as java.util.function.Consumer);
            row.item.visible=false}
        viewerListener={change->while(change.next()){
            if(change.wasRemoved())change.removed.each {detach(it)}
            if(change.wasAdded())change.addedSubList.each {attach(it)}
        }} as ListChangeListener
        new ArrayList(gui.viewerManager.allViewers).each {attach(it)}
        gui.viewerManager.allViewers.addListener(viewerListener)
        hidden={event->close()} as javafx.event.EventHandler
        gui.stage.addEventHandler(WindowEvent.WINDOW_HIDDEN,hidden)
    }
    private void attach(viewer) {
        ListChangeListener listener={change->
            def overlay=viewer.customPixelLayerOverlay
            if(!closed && overlay instanceof PixelClassificationOverlay && !approved.contains(overlay)) {
                overlay.livePrediction=false;
                overlay.stop()
                Platform.runLater({if(!closed && viewer.customPixelLayerOverlay?.is(overlay))viewer.customPixelLayerOverlay=null} as Runnable)
            }
        } as ListChangeListener
        viewer.overlayLayers.addListener(listener);
        bindings.add([viewer:viewer,listener:listener])
    }
    private void detach(viewer) {
        def binding=bindings.find {it.viewer.is(viewer)}
        if(binding!=null){viewer.overlayLayers.removeListener(binding.listener);
            bindings.remove(binding)}
    }
    void allow(overlay){approved.add(overlay)}
    void revoke(overlay){approved.remove(overlay)}
    void close() {
        if(closed)return
        closed=true
        bindings.toList().each {detach(it.viewer)}
        gui.viewerManager.allViewers.removeListener(viewerListener)
        restored.reverseEach {row->row.action.setEventHandler(row.handler);
            row.item.visible=row.visible}
        gui.stage.removeEventHandler(WindowEvent.WINDOW_HIDDEN,hidden)
        approved.clear()
        if(gui.stage.properties[KEY]?.is(this))gui.stage.properties.remove(KEY)
    }
    static install(gui,Closure explain=null) {
        if(!Platform.isFxApplicationThread())throw new IllegalStateException("Install reference-first controls on the QuPath application thread.")
        def current=gui.stage.properties[KEY]
        if(current!=null)return current
        def guard=new MifReferenceFirstGuard(gui,explain ?: {text->Dialogs.showInfoNotification("Classifier workflow",text)})
        gui.stage.properties[KEY]=guard
        guard
    }
}
return [install:MifReferenceFirstGuard.&install]
