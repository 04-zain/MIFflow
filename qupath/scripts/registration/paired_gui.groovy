/**
 * Read-only paired-image references while the primary mIF image remains available
 * for annotations. Fitting and candidate review belong to the embedded Qt editor.
 * The saved accepted transform supplies the overlay and registered side-by-side
 * reference; original pixels, primary annotations and reader cleanup are retained.
 */
import java.nio.file.Path
import java.util.concurrent.Executors
import javafx.application.Platform
import javafx.stage.Stage
import javafx.scene.Scene
import javafx.scene.layout.VBox
import javafx.scene.control.*
import javafx.geometry.Insets
import javafx.beans.value.ChangeListener
import qupath.lib.gui.viewer.QuPathViewer
import qupath.lib.gui.viewer.OverlayOptions
import qupath.lib.images.ImageData
import qupath.ext.imagecombinerwarpy.gui.ImageCombinerWarpyServerOverlay

class PairedDisplay {
    static void styleOwnedControls(javafx.scene.Parent root) {
        Set families=javafx.scene.text.Font.families as Set
        List preferred=System.getProperty("os.name").toLowerCase().contains("win") ?
            ["Segoe UI"] : ["Ubuntu Sans","Ubuntu"]
        String selected=preferred.find {families.contains(it)} ?: javafx.scene.text.Font.getDefault().family
        root.style="-fx-font-family:'"+selected.replace("'","")+"';-fx-font-size:14px;-fx-background-color:#f3f5f5;-fx-text-fill:#172d35;"
    }
    /**
     * Displays the transformed paired-image pixels over the current mIF image.
     *
     * We construct the paired view using the fixed image's width, height and
     * physical pixel calibration, then add it to QuPath as a custom image overlay.
     * Its initial opacity is one half. An image-change listener removes this
     * overlay when the fixed viewer switches to another image.
     *
     * Args:
     *     fixed: mIF viewer receiving the paired-image overlay.
     *     movingServer: Open reader for the original paired image.
     *     transform: Moving-to-fixed coordinate transform used by the current
     *         alignment or accepted reference.
     *     geometry: Shared transform functions, including warped() for creating
     *         the sampled image server.
     *     cleanup: Optional executor used to close the image reader without
     *         waiting on the JavaFX thread.
     *
     * Returns:
     *     Overlay and a close callback that removes the image-change listener,
     *     releases the paired-image reader and repaints the fixed viewer. A
     *     repeated close call leaves the already released resources alone.
     *
     * Raises:
     *     IllegalArgumentException: No mIF image is open in the fixed viewer.
     */
    static Map attach(QuPathViewer fixed,movingServer,transform,Map geometry,def cleanup=null) {
        def image=fixed.imageData
        if(image==null)throw new IllegalArgumentException("Open the primary mIF image first.")
        def calibration=image.server.pixelCalibration
        def warped=geometry.warped(movingServer,transform,image.server.width,image.server.height,
                                  calibration.pixelWidthMicrons,calibration.pixelHeightMicrons)
        def overlay=new ImageCombinerWarpyServerOverlay(fixed,warped)
        // QuPath's viewer listens for its own image path. The paired tiles have
        // another path, so keep visible requests alive and repaint when they arrive.
        def closed=new java.util.concurrent.atomic.AtomicBoolean()
        String warpedPath=warped.path
        def tileListener=[
            requiresTileRegion:{String path,region ->
                !closed.get() && path==warpedPath &&
                    fixed.requiresTileRegion(image.server.path,region)
            },
            tileAvailable:{String path,region,tile ->
                if(!closed.get() && path==warpedPath)fixed.repaint()
            }
        ] as qupath.lib.gui.images.stores.TileListener
        fixed.imageRegionStore.addTileListener(tileListener)
        overlay.setOpacity(0.5d);
        fixed.customOverlayLayers.add(overlay);
        fixed.repaint()
        ChangeListener listener
        Closure close={
            if(!closed.get()) {
                closed.set(true);
                fixed.imageRegionStore.removeTileListener(tileListener)
                fixed.customOverlayLayers.remove(overlay)
                fixed.imageDataProperty().removeListener(listener)
                if(cleanup!=null)cleanup.submit({warped.close()} as Runnable)
                else warped.close()
                fixed.repaint()
            }
        }
        listener={obs,old,current -> if(current==null || !current.is(image))close()} as ChangeListener
        fixed.imageDataProperty().addListener(listener)
        return [overlay:overlay,close:close]
    }
    /**
     * Centres the viewer on an annotation and chooses a downsample that contains its bounds.
     *
     * We divide the annotation's width and height by 80% of the available viewer
     * dimensions, using at least 200 display pixels on each axis. The larger ratio
     * determines the downsample, with a minimum of one. This leaves a margin around
     * the selected tissue region while keeping its full width and height visible.
     *
     * Args:
     *     viewer: QuPath viewer to reposition.
     *     object: Annotation with a region of interest. An absent region leaves
     *         the current viewer position unchanged.
     */
    static void focus(QuPathViewer viewer,object) {
        if(object?.ROI==null)return
        viewer.centerROI(object.ROI)
        double downsample=Math.max(object.ROI.boundsWidth/Math.max(200d,viewer.view.width*0.8d),
                                   object.ROI.boundsHeight/Math.max(200d,viewer.view.height*0.8d))
        viewer.setDownsampleFactor(Math.max(1d,downsample));
        viewer.repaint()
    }
    /** Select the paired entry in this sample; a single eligible pair needs no chooser. */
    static Map open(gui,Map geometry,List approvals=[],String displayMode="overlay") {
        def image=gui.viewer?.imageData,project=gui.project
        if(image==null || project==null)throw new IllegalArgumentException("Open the primary mIF image in its project first.")
        def entry=project.getEntry(image)
        if(entry==null)throw new IllegalArgumentException("The current image is not in this project.")
        String role=entry.getMetadataValue("mif_pipeline.image_role")
        if(role!=null && role!="primary_mif")throw new IllegalArgumentException("Activate the primary mIF image before opening paired-image guidance.")
        String sample=entry.getMetadataValue("mif_pipeline.sample_id")
        def candidates=project.imageList.findAll {it.ID!=entry.ID && (sample==null ||
            (it.getMetadataValue("mif_pipeline.sample_id")==sample && it.getMetadataValue("mif_pipeline.image_role")=="moving_reference"))}
        if(candidates.isEmpty())throw new IllegalArgumentException("Add the paired image through the registration step first.")
        if(candidates.size()==1)return openPair(gui,geometry,candidates.first(),approvals,displayMode)
        Map choices=[:]
        candidates.eachWithIndex {value,index -> choices[value.imageName+" ("+(index+1)+")"]=value}
        def dialog=new ChoiceDialog(choices.keySet().first(),choices.keySet())
        dialog.initOwner(gui.stage)
        dialog.title="Paired image"
        dialog.headerText="Select the paired image for "+entry.imageName
        def selected=dialog.showAndWait()
        if(!selected.present)return [:]
        return openPair(gui,geometry,choices[selected.get()],approvals,displayMode)
    }

    /**
     * Open an accepted overlay or a read-only side-by-side annotation reference.
     * An accepted transform must match both entries and their original sources.
     * A registered second viewer follows the primary centre and downsample every
     * 100 ms; an unregistered reference permits independent navigation.
     * Reader work remains serialized and an undelivered result is closed once.
     */
    static Map openPair(gui,Map geometry,def moving,List approvals=[],String displayMode="overlay") {
        if(!(displayMode in ["overlay","side_by_side"]))
            throw new IllegalArgumentException("Open alignment editing and candidate review in the main application.")
        def fixed=gui.viewer,image=fixed?.imageData,project=gui.project
        def fixedEntry=project?.getEntry(image)
        if(fixedEntry==null || moving==null || fixedEntry.ID==moving.ID || !project.imageList.contains(moving))
            throw new IllegalArgumentException("Select distinct mIF and paired entries from the same project.")
        Path transformFile=fixedEntry.entryPath.resolve("transform_"+fixedEntry.ID+"_"+moving.ID+".json")
        boolean registered=approvals.any {it.fixed_entry_id==fixedEntry.ID.toString() && it.moving_entry_id==moving.ID.toString()}
        if(displayMode=="overlay" && !registered)
            throw new IllegalArgumentException("Complete registration review in the main application before opening the annotation overlay.")
        Stage stage=new Stage()
        stage.initOwner(gui.stage)
        stage.title=displayMode=="overlay"?"Paired-image overlay":"Side-by-side reference"
        VBox body=new VBox(10d)
        body.padding=new Insets(16d)
        Label status=new Label("Opening the paired image…")
        status.wrapText=true
        body.children.add(status)
        Slider opacity=new Slider(0d,1d,0.5d)
        opacity.showTickLabels=true
        if(displayMode=="overlay")body.children.addAll(new Label("Paired-image opacity"),opacity)
        def worker=Executors.newSingleThreadExecutor()
        Map active=[closed:false,busy:false,overlay:null,paired:null,deliveries:[]]
        def closed=new java.util.concurrent.atomic.AtomicBoolean(false)
        Closure submit={Closure task,Closure use,Closure dispose ->
            if(closed.get() || active.busy)return
            active.busy=true
            Map delivery=[result:new java.util.concurrent.atomic.AtomicReference(),
                claimed:new java.util.concurrent.atomic.AtomicBoolean(false),dispose:dispose]
            active.deliveries.add(delivery)
            worker.submit({
                def value=null
                Throwable failure=null
                try {value=task()}catch(Throwable error){failure=error}
                delivery.result.set([value:value,error:failure])
                if(!closed.get())Platform.runLater({
                    if(closed.get() || !delivery.claimed.compareAndSet(false,true))return
                    active.busy=false
                    def outcome=delivery.result.get()
                    active.deliveries.remove(delivery)
                    if(fixed.imageData==null || !fixed.imageData.is(image)) {
                        if(outcome.value!=null)worker.submit({dispose(outcome.value)} as Runnable)
                    } else if(outcome.error!=null)status.text=outcome.error.message?:outcome.error.class.simpleName
                    else {
                        try {use(outcome.value)}
                        catch(Throwable problem){worker.submit({dispose(outcome.value)} as Runnable);status.text=problem.message?:problem.class.simpleName}
                    }
                } as Runnable)
            } as Runnable)
        }
        opacity.valueProperty().addListener({obs,old,value ->
            active.overlay?.overlay?.setOpacity(value.doubleValue());fixed.repaint()
        } as ChangeListener)
        def allTissue=image.hierarchy.annotationObjects.findAll {geometry.acceptedTissue(it)}
        def record=approvals.find {it.fixed_entry_id==fixedEntry.ID.toString() && it.moving_entry_id==moving.ID.toString()}
        if(record!=null)allTissue=image.hierarchy.annotationObjects.findAll {record.tissue_object_ids.contains(it.ID.toString())}
        if(allTissue) {
            ComboBox regions=new ComboBox()
            regions.items.addAll(allTissue.collect {it.name?:"Tissue region"})
            regions.id="registration-focus-tissue"
            regions.promptText="Focus on a tissue region"
            regions.selectionModel.selectedIndexProperty().addListener({obs,old,index ->
                if(index.intValue()>=0)focus(fixed,allTissue[index.intValue()])
            } as ChangeListener)
            body.children.add(regions)
        }
        submit({
            if(registered) {
                Map approved=geometry.approved(transformFile,fixedEntry.ID.toString(),moving.ID.toString(),approvals)
                Path fixedPath=Path.of(new URI(approved.fixed_image_uri)).normalize()
                Path movingPath=Path.of(new URI(approved.moving_image_uri)).normalize()
                if(!image.server.getURIs().any {it.scheme=="file" && Path.of(it).normalize()==fixedPath} ||
                   !moving.serverBuilder.getURIs().any {it.scheme=="file" && Path.of(it).normalize()==movingPath})
                    throw new IllegalArgumentException("The paired image source changed after registration review.")
                return [server:moving.serverBuilder.build(),transform:geometry.load(transformFile)]
            }
            return [server:moving.serverBuilder.build(),transform:null]
        },{prepared ->
            if(displayMode=="overlay") {
                active.overlay=attach(fixed,prepared.server,prepared.transform,geometry,worker)
                active.overlay.overlay.setOpacity(opacity.value)
                status.text="Accepted alignment over "+fixedEntry.imageName+". Draw and save annotations on the primary mIF image."
            } else {
                def server=prepared.server
                if(registered)server=geometry.warped(server,prepared.transform,image.server.width,image.server.height,
                    image.server.pixelCalibration.pixelWidthMicrons,image.server.pixelCalibration.pixelHeightMicrons)
                QuPathViewer paired=new QuPathViewer(gui.imageRegionStore,new OverlayOptions())
                paired.setImageData(new ImageData(server))
                active.paired=paired
                paired.view.prefWidth=700d;paired.view.prefHeight=500d
                body.children.add(paired.view)
                stage.sizeToScene()
                status.text=registered?"Registered reference beside the mIF image. Navigation follows the primary image.":"Paired reference beside the mIF image. Pan each image separately; this pair has not been aligned."
                if(registered) {
                    def timer=new javafx.animation.Timeline(new javafx.animation.KeyFrame(javafx.util.Duration.millis(100),{
                        paired.setDownsampleFactor(fixed.downsampleFactor)
                        paired.setCenterPixelLocation(fixed.centerPixelX,fixed.centerPixelY)
                    } as javafx.event.EventHandler))
                    timer.cycleCount=javafx.animation.Animation.INDEFINITE
                    timer.play();active.sync=timer
                }
            }
        },{prepared ->prepared.server?.close()})
        ChangeListener changed={obs,old,current ->if(current==null || !current.is(image))stage.close()} as ChangeListener
        fixed.imageDataProperty().addListener(changed)
        stage.setOnHidden({event ->
            if(active.closed)return
            active.closed=true;closed.set(true)
            fixed.imageDataProperty().removeListener(changed)
            active.remove("overlay")?.close()
            active.sync?.stop()
            if(active.paired) {
                def data=active.paired.imageData
                active.paired.closeViewer()
                if(data!=null)worker.submit({data.server.close()} as Runnable)
            }
            List pending=new ArrayList(active.deliveries)
            worker.submit({
                for(def delivery:pending) {
                    if(delivery.claimed.compareAndSet(false,true)) {
                        def outcome=delivery.result.get()
                        if(outcome?.value!=null)delivery.dispose(outcome.value)
                    }
                }
            } as Runnable)
            worker.shutdown()
        })
        stage.scene=new Scene(body)
        stage.show()
        styleOwnedControls(stage.scene.root)
        return [stage:stage,close:{stage.close()},state:active,worker:worker]
    }
}
return [attach:PairedDisplay.&attach,focus:PairedDisplay.&focus,open:PairedDisplay.&open,openPair:PairedDisplay.&openPair]
