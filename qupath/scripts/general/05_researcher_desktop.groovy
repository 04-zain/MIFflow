/**
 * Opens QuPath with the controls needed by the current analysis step.
 *
 * The application supplies the project, registered script directory and current
 * module settings in one encoded request. We load the prediction, annotation and
 * paired-image helpers before opening their windows, so saved models and returned
 * annotations use the same functions as the current application.
 *
 * Image opening and model fitting can continue outside the JavaFX interface
 * thread. When the main window closes, we close the associated editors, retire
 * their predictions and wait for model cleanup before allowing the process to
 * finish. A detector draft additionally returns the selected model through its
 * saved editing-session result.
 */

import java.nio.file.Path
import java.nio.file.Files
import java.nio.charset.StandardCharsets
import java.security.MessageDigest
import java.util.concurrent.CountDownLatch
import javafx.application.Platform
import javafx.scene.control.Alert
import javafx.scene.control.Menu
import javafx.scene.control.MenuItem
import qupath.lib.gui.QuPathGUI
import qupath.lib.projects.ProjectIO
import java.awt.image.BufferedImage
import com.google.gson.Gson

/** Display the saved reference bounds without adding an annotation to the image. */
class RecordedReferenceExtent implements qupath.lib.gui.viewer.overlays.PathOverlay {
    final Object image
    final Map bounds
    final List cellOutlines
    RecordedReferenceExtent(image, Map bounds, List cellOutlines=[]) {this.image=image; this.bounds=new LinkedHashMap(bounds); this.cellOutlines=cellOutlines}
    void paintOverlay(java.awt.Graphics2D graphics, qupath.lib.regions.ImageRegion region,
                      double downsample, qupath.lib.images.ImageData current, boolean complete) {
        if(!current.is(image) || region.z!=(bounds.z as int) || region.t!=(bounds.t as int))return
        def copy=(java.awt.Graphics2D)graphics.create()
        try {
            if(cellOutlines) {
                cellOutlines.each {outline ->
                    copy.color=outline.kind=='nucleus' ? new java.awt.Color(255,224,82) : new java.awt.Color(34,211,238)
                    copy.stroke=new java.awt.BasicStroke((float)(1.5d*downsample))
                    outline.rings.each {ring ->
                        if(ring) {
                            def path=new java.awt.geom.Path2D.Double()
                            path.moveTo(ring[0][0] as double,ring[0][1] as double)
                            ring.drop(1).each {point -> path.lineTo(point[0] as double,point[1] as double)}
                            path.closePath()
                            copy.draw(path)
                        }
                    }
                }
                return
            }
            def rectangle=new java.awt.geom.Rectangle2D.Double(bounds.x as double,bounds.y as double,
                bounds.width as double,bounds.height as double)
            copy.color=java.awt.Color.BLACK
            copy.stroke=new java.awt.BasicStroke((float)(4d*downsample))
            copy.draw(rectangle)
            copy.color=java.awt.Color.WHITE
            copy.stroke=new java.awt.BasicStroke((float)(2d*downsample),java.awt.BasicStroke.CAP_BUTT,
                java.awt.BasicStroke.JOIN_MITER,10f,[8d*downsample,5d*downsample] as float[],0f)
            copy.draw(rectangle)
            copy.font=copy.font.deriveFont((float)(13d*downsample))
            float x=(float)((bounds.x as double)+4d*downsample)
            float y=(float)((bounds.y as double)-6d*downsample)
            copy.color=java.awt.Color.BLACK
            copy.drawString('Recorded reference extent',(float)(x+downsample),(float)(y+downsample))
            copy.color=java.awt.Color.WHITE
            copy.drawString('Recorded reference extent',x,y)
        } finally {copy.dispose()}
    }
}

/** Share display choices with the application; image values and analysis settings are untouched. */
class ImageAppearanceSession implements AutoCloseable {
    Path path
    Map saved=[:]
    Map changed=[:]
    List<Closure> detach=[]
    Set watchedWindows=Collections.newSetFromMap(new IdentityHashMap())
    int displayActionDepth=0
    ImageAppearanceSession(gui, String file) {
        if(file==null)return
        path=Path.of(file)
        try { if(Files.isRegularFile(path))saved=new Gson().fromJson(Files.readString(path),Map.class) ?: [:] }
        catch(Exception ignored) { saved=[:] }
        // QuPath initializes display ranges asynchronously. Persist changes made
        // through its controls, rather than treating those defaults as user choices.
        javafx.stage.Window.windows.each {watchActions(it)}
        def windowsListener={change -> while(change.next())change.addedSubList.each {watchActions(it)}} as javafx.collections.ListChangeListener
        javafx.stage.Window.windows.addListener(windowsListener)
        detach.add({javafx.stage.Window.windows.removeListener(windowsListener)})
        gui.allViewers.each {attach(it)}
        def listener={change -> while(change.next())change.addedSubList.each {attach(it)}} as javafx.collections.ListChangeListener
        gui.allViewers.addListener(listener)
        detach.add({gui.allViewers.removeListener(listener)})
    }
    void watchActions(window) {
        if(!watchedWindows.add(window))return
        def previous=window.eventDispatcher
        def dispatcher={event,tail ->
            boolean action=event instanceof javafx.scene.input.InputEvent || event instanceof javafx.event.ActionEvent
            if(action)displayActionDepth++
            try {return previous.dispatchEvent(event,tail)}
            finally {if(action)displayActionDepth--}
        } as javafx.event.EventDispatcher
        window.eventDispatcher=dispatcher
        detach.add({
            if(window.eventDispatcher.is(dispatcher))window.eventDispatcher=previous
            watchedWindows.remove(window)
        })
    }
    static String imageKey(image) {
        if(image==null)return null
        def uris=image.server.getURIs()
        if(uris.size()!=1)return null
        def uri=uris.first()
        return (uri.scheme=='file' ? Path.of(uri).toAbsolutePath().normalize().toUri().toASCIIString() : uri.toASCIIString()).replaceAll('/+$','')
    }
    void attach(viewer) {
        if(path==null)return
        def state=[image:null,key:null,applying:false]
        Closure capture={ ->
            if(displayActionDepth==0 || state.applying || !state.key || viewer.imageData!=state.image || viewer.imageDisplay.imageData!=state.image)return
            def display=viewer.imageDisplay
            def channels=display.availableChannels().findAll {it instanceof qupath.lib.display.DirectServerChannelInfo}
            if(channels.isEmpty())return
            def selected=channels.findAll {display.selectedChannels().contains(it)}.collect {it.channel as int}
            Map value=new LinkedHashMap(changed[state.key] ?: saved[state.key] ?: [:])
            value.mode=display.useGrayscaleLuts() ? 'grayscale' : (selected.size()==1 ? 'colour' : 'composite')
            value.channels=selected
            value.colors=[:]; value.limits=[:]
            channels.each {channel ->
                value.colors["${channel.channel}"]=String.format('#%06x',channel.color & 0xffffff)
                value.limits["${channel.channel}"]=[channel.minDisplay as double,channel.maxDisplay as double]
            }
            changed[state.key]=value
        }
        Closure opened={ ->
            def image=viewer.imageData
            state.image=image; state.key=imageKey(image)
            if(image==null || state.key==null)return
            Map value=changed[state.key] ?: saved[state.key]
            state.applying=true
            try {
                if(value instanceof Map && value.mode!='rgb') {
                    def display=viewer.imageDisplay
                    def channels=display.availableChannels().findAll {it instanceof qupath.lib.display.DirectServerChannelInfo}
                    Set selected=(value.channels ?: []).collect {it as int} as Set
                    if(!channels.isEmpty() && channels.collect {it.channel as int}.containsAll(selected)) {
                        channels.each {channel ->
                            String key="${channel.channel}"
                            String color=value.colors?.get(key)
                            if(color==~/#[0-9a-fA-F]{6}/) {
                                int rgb=Integer.parseInt(color.substring(1),16)
                                channel.setLUTColor((rgb>>16)&255,(rgb>>8)&255,rgb&255)
                            }
                            def limits=value.limits?.get(key)
                            if(limits instanceof List && limits.size()==2 && limits.every {it instanceof Number && Double.isFinite(it.doubleValue())} && limits[1]>limits[0])
                                display.setMinMaxDisplay(channel,limits[0] as float,limits[1] as float)
                        }
                        display.setUseGrayscaleLuts(value.mode=='grayscale')
                        display.availableChannels().each {channel -> display.setChannelSelected(channel,
                            channel instanceof qupath.lib.display.DirectServerChannelInfo && selected.contains(channel.channel as int))}
                        viewer.repaintEntireImage()
                    }
                }
            } finally {state.applying=false}
        }
        def imageListener={obs,previous,current -> Platform.runLater({opened()} as Runnable)} as javafx.beans.value.ChangeListener
        def displayListener={obs,previous,current -> capture()} as javafx.beans.value.ChangeListener
        viewer.imageDataProperty().addListener(imageListener)
        viewer.imageDisplay.eventCountProperty().addListener(displayListener)
        detach.add({viewer.imageDataProperty().removeListener(imageListener);viewer.imageDisplay.eventCountProperty().removeListener(displayListener)})
        if(viewer.imageData!=null)Platform.runLater({opened()} as Runnable)
    }
    void close() {
        detach.each {it()};detach.clear()
        if(path==null || changed.isEmpty())return
        try {
            Map latest=[:]
            if(Files.isRegularFile(path))latest=new Gson().fromJson(Files.readString(path),Map.class) ?: [:]
            latest.putAll(changed)
            Files.createDirectories(path.parent)
            Path temporary=path.resolveSibling(path.fileName.toString()+'.tmp')
            Files.writeString(temporary,new Gson().toJson(latest))
            Files.move(temporary,path,java.nio.file.StandardCopyOption.REPLACE_EXISTING)
        } catch(Exception error) {System.err.println('Could not save image appearance: '+error.message)}
    }
}

// One image is owned by the loader until the JavaFX viewer explicitly takes it.
/**
 * Reads a tissue image outside the interface thread and holds it until the viewer takes it.
 *
 * The image can finish loading after we have closed QuPath or changed projects.
 * A synchronized pending value lets the interface take the completed image once,
 * while cancellation closes a completed image that is no longer needed.
 */
class TissueImageLoading {
    private Object pending
    private boolean cancelled=false
    /**
     * Reads the selected entry and builds its image server before the interface receives it.
     *
     * We check cancellation before reading and again before storing the completed
     * image. If cancellation occurs during the read, the newly opened image is closed
     * instead of being handed to a window that has already gone away.
     *
     * Args:
     *     entry: Project entry selected for tissue-detector editing.
     *
     * Returns:
     *     True when a completed image is retained for take(); False when cancelled.
     */
    boolean load(entry) {
        synchronized(this){if(cancelled)return false}
        def image=entry.readImageData()
        boolean retained=false
        try {
            image.getServer() // Build a lazy server here, not on JavaFX.
            synchronized(this){
                if(!cancelled){pending=image;
                    retained=true}
            }
            return retained
        }finally{if(!retained)image.close()}
    }
    /**
     * Transfers the completed image to the caller and clears the pending reference.
     *
     * Returns:
     *     Loaded ImageData, or null when loading did not complete or was cancelled.
     *     After receiving an image, the viewer or caller is responsible for closing it.
     */
    synchronized Object take() {
        def image=pending;
        pending=null;
        return image
    }
    /**
     * Stops further delivery and closes any completed image still awaiting the viewer.
     *
     * The cancelled flag and pending-image reference are changed together. Closing
     * is performed after leaving the synchronized section, keeping a potentially
     * slow image-reader close outside that short state update.
     */
    void cancel() {
        def image
        synchronized(this) {
            cancelled=true;
            image=pending;
            pending=null
        }
        image?.close()
    }
}


// Decode the launch selected by Python. The editor defaults are explicit in
// this request, so native windows use the same module files as the application.
if(args.size()!=1)throw new IllegalArgumentException("Expected one encoded desktop request")
Map request=new Gson().fromJson(new String(Base64.urlDecoder.decode(args[0]),StandardCharsets.UTF_8),Map.class)
if((request.keySet()-["paired_views","reference_display","tissue_authoring","image_appearance_path","classifier_review","feature_inspection","reference_authoring","inspection"])!=["project_file","bundle_root","bundle_sha256","editor_defaults"] as Set ||
   (request.containsKey("paired_views") && (!(request.paired_views instanceof List) || request.paired_views.size()>1024)) ||
   !(request.bundle_sha256==~/[0-9a-f]{64}/))throw new IllegalArgumentException("The desktop request is invalid")
if(!(request.editor_defaults instanceof Map) || request.editor_defaults.keySet()!=["tissue","registration","bspline","manual_registration"] as Set ||
   !request.editor_defaults.values().every {it instanceof Map})
    throw new IllegalArgumentException("Reopen QuPath through the current application to load its module settings.")
Path project=Path.of(request.project_file),bundle=Path.of(request.bundle_root)
if(!project.isAbsolute() || !Files.isRegularFile(project) || Files.isSymbolicLink(project) ||
   bundle!=project.parent.resolve("scripts/bundle-"+request.bundle_sha256))
    throw new IllegalArgumentException("The project and script bundle do not match")
Path manifest=bundle.resolve("qupath-script-bundle.v1.json")
byte[] inventory=Files.readAllBytes(manifest)
String digest=MessageDigest.getInstance("SHA-256").digest(inventory).encodeHex().toString()
if(digest!=request.bundle_sha256)throw new IllegalArgumentException("The script bundle changed")
Map parsed=new Gson().fromJson(new String(inventory,StandardCharsets.UTF_8),Map.class)
String helper="compartment_analysis/kronos_bundle_support.groovy"
Map expected=parsed.assets.find {it.path==helper}
byte[] code=Files.readAllBytes(bundle.resolve(helper))
if(expected==null || MessageDigest.getInstance("SHA-256").digest(code).encodeHex().toString()!=expected.sha256)
    throw new IllegalArgumentException("The KRONOS registration helper changed")

// Load the prediction/model helpers before reading saved classifiers. Their
// registered types are required when QuPath reconstructs those classifier files.
Map support=new GroovyShell(this.class.classLoader).evaluate(new String(code,StandardCharsets.UTF_8),helper).load(bundle)
String toolsName="compartment_analysis/kronos_gui_tools.groovy"
Map toolsEvidence=parsed.assets.find {it.path==toolsName}
byte[] toolsCode=Files.readAllBytes(bundle.resolve(toolsName))
if(toolsEvidence==null || MessageDigest.getInstance("SHA-256").digest(toolsCode).encodeHex().toString()!=toolsEvidence.sha256)
    throw new IllegalArgumentException("The researcher GUI tools changed")
Map toolsApi=new GroovyShell(this.class.classLoader).evaluate(new String(toolsCode,StandardCharsets.UTF_8),toolsName)
CountDownLatch closed=new CountDownLatch(1)
def startupFailure=new java.util.concurrent.atomic.AtomicReference<Throwable>()
Platform.startup({
    QuPathGUI gui=null
    def nativeDisplay=null
    def imageAppearance=null
    def referenceFirst=null
    def referenceClasses=null
    def recordedExtent=null
    def recordedContext=null
    def recordedContextVisible=null
    Map correctionTicket=null
    Map correctionBatch=null
    try {
        gui=QuPathGUI.createHiddenInstance()
        Platform.setImplicitExit(false)
        gui.setProject(ProjectIO.loadProject(project.toFile(),BufferedImage.class))
        if(request.inspection!=null)gui.setReadOnly(true)
        Path annotationTicket=project.parent.parent.resolve("cluster-ticket.json")
        if(Files.exists(annotationTicket)) {
            if(!Files.isRegularFile(annotationTicket,java.nio.file.LinkOption.NOFOLLOW_LINKS) ||
                    Files.size(annotationTicket)>32*1024*1024)
                throw new IllegalArgumentException("The annotation ticket changed. Reopen References.")
            Map ticket=new com.google.gson.Gson().fromJson(Files.readString(annotationTicket),Map.class)
            if(ticket.cell_correction!=null)correctionTicket=ticket
            if(ticket.annotation_reference_labels!=null)
                referenceClasses=support.annotationTools.scopeReferenceClasses.call(ticket.annotation_reference_labels as List)
        }
        Path correctionTickets=project.parent.parent.resolve("correction-tickets.json")
        if(Files.exists(correctionTickets)) {
            if(!Files.isRegularFile(correctionTickets,java.nio.file.LinkOption.NOFOLLOW_LINKS) || Files.size(correctionTickets)>32*1024*1024)
                throw new IllegalArgumentException("Reopen the collected corrections from the application.")
            correctionBatch=new com.google.gson.Gson().fromJson(Files.readString(correctionTickets),Map.class)
        }
        gui.stage.setWidth(1250);
        gui.stage.setHeight(900)
        nativeDisplay=support.nativeDisplay.install(gui,support.displayPolicy)
        imageAppearance=new ImageAppearanceSession(gui,request.image_appearance_path as String)
        referenceFirst=support.referenceFirst.install.call(gui)
        def tools=toolsApi.install(gui,support)
        List pairedWindows=[]
        Map pairedApis=[:]
        ClassLoader pairedLoader = null

        // Read one helper from the script directory selected for this project opening.
        // Its recorded checksum lets us detect a changed helper before executing it.
        Closure loadPaired={String name ->
            Map item=parsed.assets.find {it.path==name}
            if(item==null)throw new IllegalArgumentException("Update the project tools before opening paired-image guidance.")
            byte[] payload=Files.readAllBytes(bundle.resolve(name))
            if(MessageDigest.getInstance("SHA-256").digest(payload).encodeHex().toString()!=item.sha256)
                throw new IllegalArgumentException("The paired-image support changed. Reopen the project from the pipeline.")
            return new GroovyShell(pairedLoader ?: this.class.classLoader).evaluate(new String(payload,StandardCharsets.UTF_8),name)
        }

        // Load the installed Warpy classes only when a paired-image operation is opened.
        // These tools only display annotation references; Qt owns registration.
        Closure ensurePairedTools={
            if(pairedApis.geometry)return
            def warpyLoader = new GroovyClassLoader(qupath.lib.gui.QuPathGUI.getExtensionCatalogManager().extensionClassLoader)
            try { warpyLoader.loadClass("qupath.ext.warpy.Warpy") }
            catch (ClassNotFoundException missing) {
                String launcher = System.getProperty("jpackage.app-path")
                // Packaged QuPath stores its application JARs beside the Windows launcher
                // in app/, and above the Linux bin/ launcher in lib/app/.
                boolean windows = System.getProperty("os.name").toLowerCase().contains("win")
                Path extension = launcher == null ? null : (windows ?
                    Path.of(launcher).parent.resolve("app/qupath-extension-warpy-0.5.0.jar") :
                    Path.of(launcher).parent.parent.resolve("lib/app/qupath-extension-warpy-0.5.0.jar"))
                if(extension == null || !Files.isRegularFile(extension) || Files.isSymbolicLink(extension) || Files.size(extension)!=113594L ||
                   MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(extension)).encodeHex().toString()!="e4bc1c636adbcfd6e6dafde32d615afa032d3c0a21458660b2de380a884f5cb8")
                    throw new IllegalArgumentException("Install the verified Warpy extension in Software setup before alignment.")
                warpyLoader.addClasspath(extension.toString())
            }
            pairedLoader=warpyLoader
            pairedApis.geometry=loadPaired("registration/paired_geometry.groovy")
            pairedApis.gui=loadPaired("registration/paired_gui.groovy")
        }
        Closure openPaired={ ->
            try {
                ensurePairedTools()
                def opened=pairedApis.gui.open(gui,pairedApis.geometry,request.paired_views?:[],"overlay")
                if(opened.stage)pairedWindows.add(opened)
            }catch(Throwable error){qupath.fx.dialogs.Dialogs.showErrorMessage("Paired image",error.message?:error.class.simpleName)}
        }
        def pipelineMenu=gui.menuBar.menus.find {it.text=="mIF"}
        def overlayItem=new javafx.scene.control.MenuItem("Paired-image overlay…")
        overlayItem.setOnAction({event -> openPaired()} as javafx.event.EventHandler)
        def sideItem=new javafx.scene.control.MenuItem("Side-by-side reference…")
        sideItem.setOnAction({event ->
            try {
                ensurePairedTools()
                def opened=pairedApis.gui.open(gui,pairedApis.geometry,request.paired_views ?: [],"side_by_side")
                if(opened.stage)pairedWindows.add(opened)
            } catch(Throwable error){qupath.fx.dialogs.Dialogs.showErrorMessage("Paired image",error.message ?: error.class.simpleName)}
        } as javafx.event.EventHandler)
        pipelineMenu.items.addAll(new javafx.scene.control.SeparatorMenuItem(),overlayItem,sideItem)
        List annotationWindows=[]
        Closure openReferenceAnnotations=null
        if(request.reference_authoring!=null) {
            Map guide=loadPaired('preprocessing/reference_annotation_workbench.groovy')
            openReferenceAnnotations={->
                def existing=annotationWindows.find {it.stage?.showing}
                if(existing!=null){existing.stage.toFront();return}
                def opened=guide.open.call(gui,request.reference_authoring as Map,support.annotationTools)
                annotationWindows.add(opened)
            }
            def referenceItem=new MenuItem('Annotate reference regions…')
            referenceItem.setOnAction({event->
                try {openReferenceAnnotations()}
                catch(Throwable error){qupath.fx.dialogs.Dialogs.showErrorMessage('Reference annotations',error.message ?: error.class.simpleName)}
            } as javafx.event.EventHandler)
            pipelineMenu.items.add(referenceItem)
        }
        Closure openCorrections=null
        if(correctionTicket!=null || correctionBatch!=null) {
            openCorrections={->
                try {
                    def existing=annotationWindows.find {it.stage?.showing}
                    if(existing!=null){existing.stage.toFront();return}
                    def correctionGeometry=loadPaired("general/06_cluster_annotations.groovy")
                    def opened=correctionBatch!=null ? support.annotationTools.openCorrectionBatch.call(gui,correctionBatch,correctionGeometry) :
                        support.annotationTools.openCorrections.call(gui,correctionTicket,correctionGeometry)
                    if(opened!=null)annotationWindows.add(opened)
                }catch(Throwable error){qupath.fx.dialogs.Dialogs.showErrorMessage("Cell corrections",error.message ?: error.class.simpleName)}
            }
            def correctionItem=new MenuItem("Correct selected outlines…")
            correctionItem.setOnAction({event->openCorrections()} as javafx.event.EventHandler)
            pipelineMenu.items.add(correctionItem)
        }
        Closure openRegionSelection=null
        Path regionFile=project.parent.resolve("analysis-region-selection.json")
        if(Files.exists(regionFile)) {
            if(!Files.isRegularFile(regionFile,java.nio.file.LinkOption.NOFOLLOW_LINKS) || Files.size(regionFile)>4096)
                throw new IllegalArgumentException("Reopen the region selection step from the application.")
            Map selection=new Gson().fromJson(Files.readString(regionFile),Map.class)
            if(selection.keySet()!=["ticket_id","entry_id"] as Set ||
                    !(selection.ticket_id==~/[0-9a-f]{32}/) || !(selection.entry_id instanceof String))
                throw new IllegalArgumentException("The region selection draft is invalid. Reopen this step.")
            openRegionSelection={->
                def existing=annotationWindows.find {it.stage?.showing}
                if(existing!=null){existing.stage.toFront();return}
                def opened=support.annotationTools.openRegionSelection.call(gui,selection.entry_id)
                if(opened!=null)annotationWindows.add(opened)
            }
            def regionItem=new MenuItem("Select analysis regions…")
            regionItem.setOnAction({event->openRegionSelection()} as javafx.event.EventHandler)
            pipelineMenu.items.add(regionItem)
        }

        // Expose model feedback only when the annotation draft was opened with that
        // option selected. Its saved settings identify the chosen development image and model.
        Path previewFile=project.parent.resolve("annotation-preview.json")
        Closure openAnnotationPreview=null
        if(Files.exists(previewFile)) {
            def previewItem=new MenuItem("Open annotation preview…")
            openAnnotationPreview={->
                try {
                    if(!Files.isRegularFile(previewFile,java.nio.file.LinkOption.NOFOLLOW_LINKS) || Files.size(previewFile)>1024*1024)
                        throw new IllegalArgumentException("Reopen the annotation draft from the classifier step.")
                    byte[] previewBytes=Files.newInputStream(previewFile,java.nio.file.LinkOption.NOFOLLOW_LINKS).withCloseable {it.readNBytes(1024*1024+1)}
                    if(previewBytes.length>1024*1024)throw new IllegalArgumentException("The preview settings changed. Reopen the annotation draft.")
                    Map choice=new com.google.gson.Gson().fromJson(new String(previewBytes,java.nio.charset.StandardCharsets.UTF_8),Map.class)
                    def existing=annotationWindows.find {!it.resourcesClosed}
                    if(existing!=null){existing.stage?.toFront();return}
                    def entry=gui.project.imageList.find {it.ID.toString()=="1"}
                    if(entry==null)throw new IllegalArgumentException("The development annotation image is unavailable. Reopen the draft.")
                    if(gui.imageData==null || gui.project.getEntry(gui.imageData)!=entry)
                        if(!gui.openImageEntry(entry))return
                    def opened=support.annotationPreview.open.call(gui,choice,support,bundle)
                    if(opened!=null)annotationWindows.add(opened)
                }catch(Throwable error){qupath.fx.dialogs.Dialogs.showErrorMessage("Annotation preview",error.message?:error.class.simpleName)}
            }
            previewItem.setOnAction({event->openAnnotationPreview()} as javafx.event.EventHandler)
            pipelineMenu.items.add(previewItem)
        }
        List tissueWindows=[]
        Map tissueLoading=[busy:false]
        def tissueImage=new TissueImageLoading()
        def tissueLoader=java.util.concurrent.Executors.newSingleThreadExecutor({Runnable task ->
            Thread thread=new Thread(task,"tissue-image-open");
            thread.daemon=true;
            thread
        } as java.util.concurrent.ThreadFactory)

        // Combine this detector-editing request with the tissue module's initial settings.
        // The native editor reads the same configuration values instead of repeating defaults.
        Closure showTissue={Map settings ->
            def existing=tissueWindows.find {it.stage?.showing}
            if(existing!=null){existing.stage.toFront();return}
            Map algorithm=loadPaired("tissue_detection/tissue_authoring.groovy")
            def validation=loadPaired("tissue_detection/tissue_detector_support.groovy")
            Map workbench=loadPaired("tissue_detection/tissue_workbench.groovy")
            Map selected=new LinkedHashMap(settings)
            selected.defaults=new LinkedHashMap(request.editor_defaults.tissue)
            Map featureWorkbench=loadPaired("general/pixel_feature_workbench.groovy")
            def opened=workbench["open"].call(gui,algorithm,validation,selected,support.annotationTools,featureWorkbench)
            tissueWindows.add(opened)
        }

        // Open the selected image in its editing draft. When no image is displayed yet,
        // read its image data on the loader thread and deliver the completed reader to JavaFX.
        Closure openTissue={Map settings ->
            if(!Files.isRegularFile(project.parent.resolve("tissue-session.json")))
                throw new IllegalArgumentException("Create or edit the tissue detector from the pipeline’s Tissue detection step. This opens a separate draft and preserves the analysis project.")
            if(tissueLoading.busy)return
            def selected=settings.entry_id ? gui.project.imageList.find {it.ID.toString()==settings.entry_id} : null
            if(settings.entry_id && selected==null)throw new IllegalArgumentException("The tissue image is no longer in this project.")
            if(selected!=null && gui.viewer.imageData==null) {
                // This entry is explicitly the mIF tissue target. Read its saved
                // metadata/hierarchy off the UI thread; opening never saves it.
                tissueLoading.busy=true
                def expectedProject=gui.project
                tissueLoader.submit({
                    Throwable failed=null
                    try {if(!tissueImage.load(selected))return}catch(Throwable error){failed=error}
                    def failure=failed
                    Platform.runLater({
                        tissueLoading.busy=false
                        def image=tissueImage.take()
                        if(!gui.stage.showing || gui.project!=expectedProject || gui.viewer.imageData!=null) {
                            image?.close();
                            return
                        }
                        if(image==null && failure==null)return
                        if(failure!=null) {
                            qupath.fx.dialogs.Dialogs.showErrorMessage("Tissue image",failure.message?:failure.class.simpleName);
                            return
                        }
                        try {
                            if(image.imageType==null || image.imageType==qupath.lib.images.ImageData.ImageType.UNSET) {
                                image.setImageType(qupath.lib.images.ImageData.ImageType.FLUORESCENCE)
                                image.setChanged(false)
                            }
                            gui.viewer.imageData=image
                            showTissue(settings)
                        }catch(Throwable error){qupath.fx.dialogs.Dialogs.showErrorMessage("Tissue detection",error.message?:error.class.simpleName)}
                    } as Runnable)
                } as Runnable)
                return
            }
            if(selected!=null && gui.project.getEntry(gui.viewer.imageData)!=selected) {
                if(!gui.openImageEntry(selected))return
            }
            showTissue(settings)
        }
        def tissueItem=new MenuItem("Create tissue detector…")
        tissueItem.setOnAction({event ->
            try {openTissue([method:"threshold"])}
            catch(Throwable error){qupath.fx.dialogs.Dialogs.showErrorMessage("Tissue detection",error.message ?: error.class.simpleName)}
        } as javafx.event.EventHandler)
        pipelineMenu.items.addAll(new javafx.scene.control.SeparatorMenuItem(),tissueItem)

        // Close child editors before stopping JavaFX. Some prediction windows finish
        // model cleanup asynchronously and need that event loop until their close completes.
        gui.stage.setOnHidden({
            tissueImage.cancel()
            tissueLoader.shutdownNow()
            annotationWindows.each {it.close()}
            tissueWindows.each {it.close()}
            pairedWindows.each {it.close()}
            if(recordedExtent!=null)gui.viewer.customOverlayLayers.remove(recordedExtent)
            if(recordedContext!=null) {
                recordedContext.visibleProperty().unbind()
                recordedContext.prefWidthProperty().unbind()
                recordedContextVisible.dispose()
                recordedContext.parent?.children?.remove(recordedContext)
            }
            tools.close()
            imageAppearance.close()
            nativeDisplay.close()
            referenceFirst.close()
            referenceClasses?.close()
            def remaining=new ArrayList(annotationWindows)
            Runnable finish={Platform.exit();
                closed.countDown()} as Runnable
            if(remaining.every {it.resourcesClosed})Platform.runLater(finish)
            else Thread.start("mif-preview-close") {
                while(!remaining.every {it.resourcesClosed})Thread.sleep(100)
                Platform.runLater(finish)
            }
        } as javafx.event.EventHandler)
        gui.stage.show()
        if(openReferenceAnnotations!=null)Platform.runLater({->
            try {openReferenceAnnotations()}
            catch(Throwable error){startupFailure.set(error);error.printStackTrace();gui.stage.hide()}
        } as Runnable)
        if(openCorrections!=null)Platform.runLater({->openCorrections()} as Runnable)
        if(openRegionSelection!=null)Platform.runLater({->openRegionSelection()} as Runnable)
        if(openAnnotationPreview!=null)Platform.runLater({->openAnnotationPreview()} as Runnable)
        if(request.inspection?.slide_id)Platform.runLater({->
            try {
                def matches=gui.project.imageList.findAll {entry->
                    entry.getMetadataMap()["mif_pipeline.slide_id"]==request.inspection.slide_id &&
                        entry.getMetadataMap()["mif_pipeline.image_role"]=="primary_mif"
                }
                if(matches.size()!=1 || !gui.openImageEntry(matches.first()))
                    throw new IllegalArgumentException("The classified image could not be identified in the saved project.")
                def outlines=gui.imageData.hierarchy.annotationObjects.findAll {obj->
                    obj.metadata["pipeline_stage"]=="primary_classifier" && obj.metadata["pipeline_active"]!="false" &&
                        obj.metadata["analysis_domain_id"]==request.inspection.domain_id &&
                        obj.parent?.ID?.toString()==obj.metadata["analysis_domain_path_object_id"]
                }
                def domains=outlines.collect {it.parent}.unique()
                if(request.inspection.class_id) {
                    def selected=outlines.findAll {it.metadata["primary_class_id"]==request.inspection.class_id}
                    if(!selected.isEmpty())gui.imageData.hierarchy.selectionModel.setSelectedObjects(selected,selected.first())
                }
                if(domains.size()==1) {
                    def roi=domains.first().ROI
                    def viewer=gui.viewer
                    viewer.setZPosition(roi.z)
                    viewer.setTPosition(roi.t)
                    viewer.setDownsampleFactor(Math.max(viewer.minDownsample,1.1d*Math.max(
                        roi.boundsWidth/Math.max(1d,viewer.view.width),roi.boundsHeight/Math.max(1d,viewer.view.height))))
                    viewer.setCenterPixelLocation(roi.boundsX+roi.boundsWidth/2d,roi.boundsY+roi.boundsHeight/2d)
                }
                gui.overlayOptions.showAnnotations=true
            } catch(Throwable error){startupFailure.set(error);error.printStackTrace();gui.stage.hide()}
        } as Runnable)
        if(request.feature_inspection!=null) {
            Map settings=request.feature_inspection
            Platform.runLater({
                try {
                    def entry=gui.project.imageList.find {it.ID.toString()==settings.entry_id}
                    if(entry==null)throw new IllegalArgumentException("The selected preview image is unavailable in this project.")
                    if(gui.openImageEntry(entry)) {
                        gui.setReadOnly(true)
                        Map featureWorkbench=loadPaired("general/pixel_feature_workbench.groovy")
                        Closure applied={Map recipe ->
                            Files.writeString(Path.of(settings.result_path as String),new Gson().toJson(recipe),StandardCharsets.UTF_8)
                            gui.stage.hide()
                        }
                        Closure cancelled={->gui.stage.hide()}
                        def opened=featureWorkbench["open"].call(gui,settings,applied,cancelled)
                        tissueWindows.add(opened)
                    } else gui.stage.hide()
                } catch(Throwable error) {
                    startupFailure.set(error)
                    error.printStackTrace()
                    gui.stage.hide()
                }
            } as Runnable)
        }
        if(request.classifier_review!=null) {
            Map review=request.classifier_review
            Platform.runLater({
                try {
                    def entry=gui.project.imageList.find {it.ID.toString()==review.project_entry_id}
                    if(entry==null)throw new IllegalArgumentException("The selected reference image is unavailable in this project.")
                    if(gui.openImageEntry(entry)) {
                        if(review.roi_bounds instanceof Map) {
                            // Inspect the original evaluation location. When supplied,
                            // its saved classifier provides the matching image overlay.
                            Map bounds=review.roi_bounds
                            def viewer=gui.viewer
                            gui.setReadOnly(true)
                            viewer.setZPosition(bounds.z as int)
                            viewer.setTPosition(bounds.t as int)
                            viewer.setDownsampleFactor(Math.max(viewer.minDownsample,1.24d*Math.max(
                                (bounds.width as double)/Math.max(1d,viewer.view.width),
                                (bounds.height as double)/Math.max(1d,viewer.view.height))))
                            viewer.setCenterPixelLocation((bounds.x as double)+(bounds.width as double)/2d,
                                (bounds.y as double)+(bounds.height as double)/2d)
                            gui.imageData.hierarchy.selectionModel.clearSelection()
                            recordedExtent=new RecordedReferenceExtent(gui.imageData,bounds,(review.cell_outlines?:[]) as List)
                            viewer.customOverlayLayers.add(recordedExtent)
                            if(review.classifier_name) {
                                tools.reviewResource(review.classifier_name as String,review)
                            }
                            def reference=gui.imageData.hierarchy.annotationObjects.find {it.ID.toString()==review.annotation_uuid}
                            gui.imageData.hierarchy.selectionModel.setSelectedObject(reference)
                            gui.overlayOptions.showAnnotations=true
                            viewer.repaint()
                            def reviewedImage=gui.imageData
                            String resultText=review.split=='cross_validation' ? review.predicted_class : "Predicted: ${review.predicted_class}"
                            recordedContext=new javafx.scene.control.Label("Reference: ${review.reference_class}\n${resultText}")
                            recordedContext.id='mif-recorded-reference-context'
                            recordedContext.mouseTransparent=true
                            recordedContext.wrapText=true
                            recordedContext.minWidth=0d
                            recordedContext.maxWidth=javafx.scene.layout.Region.USE_PREF_SIZE
                            recordedContext.maxHeight=javafx.scene.layout.Region.USE_PREF_SIZE
                            recordedContext.prefWidthProperty().bind(javafx.beans.binding.Bindings.min(560d,viewer.view.widthProperty().subtract(16d)))
                            recordedContext.style='-fx-background-color: #171717; -fx-text-fill: white; -fx-padding: 8; -fx-background-radius: 4;'
                            javafx.scene.layout.StackPane.setAlignment(recordedContext,javafx.geometry.Pos.TOP_LEFT)
                            javafx.scene.layout.StackPane.setMargin(recordedContext,new javafx.geometry.Insets(8d))
                            recordedContextVisible=javafx.beans.binding.Bindings.createBooleanBinding({ ->
                                viewer.imageData?.is(reviewedImage) && viewer.getZPosition()==(bounds.z as int) && viewer.getTPosition()==(bounds.t as int)
                            } as java.util.concurrent.Callable,viewer.imageDataProperty(),viewer.zPositionProperty(),viewer.tPositionProperty())
                            recordedContext.visibleProperty().bind(recordedContextVisible)
                            viewer.view.children.add(recordedContext)
                        } else tools.reviewResource(review.classifier_name as String)
                    }
                } catch(Throwable error) {qupath.fx.dialogs.Dialogs.showErrorMessage("Classifier review",error.message ?: error.class.simpleName)}
            } as Runnable)
        }
        if(request.tissue_authoring!=null) {
            Map tissue=request.tissue_authoring
            if((tissue.keySet()-["setup"])!=["entry_id","method"] as Set || !(tissue.method in ["threshold","rf","svm"]))
                throw new IllegalArgumentException("The tissue workbench request is invalid")
            openTissue(tissue)
        }

        // Native paired windows are read-only references during annotation.
        if(request.reference_display!=null) {
            Map view=request.reference_display
            Set fields=["mode","fixed_entry_id","moving_entry_id"] as Set
            if(view.keySet()!=fields || !(view.mode in ["overlay","side_by_side"]))
                throw new IllegalArgumentException("Open alignment editing and candidate review in the main application.")
            def fixedEntry=gui.project.imageList.find {it.ID.toString()==view.fixed_entry_id}
            def movingEntry=gui.project.imageList.find {it.ID.toString()==view.moving_entry_id}
            if(fixedEntry==null || movingEntry==null || fixedEntry.ID==movingEntry.ID)
                throw new IllegalArgumentException("The requested mIF and paired entries are unavailable.")
            Platform.runLater({
                try {
                    gui.openImageEntry(fixedEntry)
                    ensurePairedTools()
                    def opened=pairedApis.gui.openPair(gui,pairedApis.geometry,movingEntry,request.paired_views ?: [],view.mode)
                    if(opened.stage)pairedWindows.add(opened)
                } catch(Throwable error) {qupath.fx.dialogs.Dialogs.showErrorMessage("Paired image",error.message ?: error.class.simpleName)}
            } as Runnable)
        }
    } catch(Throwable error) {
        startupFailure.set(error);
        error.printStackTrace()
        imageAppearance?.close()
        nativeDisplay?.close()
        referenceClasses?.close()
        if(gui!=null)gui.stage.hide()
        Platform.runLater({Platform.exit();
            closed.countDown()} as Runnable)
    }
} as Runnable)
closed.await()
if(startupFailure.get()!=null)throw startupFailure.get()
