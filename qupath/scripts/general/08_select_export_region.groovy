/** Return a display-only export rectangle; no project or analysis regions are saved. */
import java.nio.file.Path
import java.nio.file.Files
import java.nio.file.StandardOpenOption
import java.awt.image.BufferedImage
import java.util.concurrent.CountDownLatch
import javafx.application.Platform
import qupath.lib.gui.QuPathGUI
import qupath.lib.gui.viewer.tools.PathTools
import qupath.lib.images.ImageData
import qupath.lib.images.servers.ImageServerProvider
import qupath.lib.objects.PathObjects
import qupath.lib.roi.ROIs
import qupath.lib.regions.ImagePlane
import com.google.gson.Gson
import org.mif.cells.CellFigureExport

/** Apply only a usable display preference; never persist picker display changes. */
class ExportRegionAppearance {
    static void apply(viewer, value) {
        if(!(value instanceof Map) || !(value.mode in ['colour','composite','grayscale']) ||
            !(value.channels instanceof List) ||
            (value.colors!=null && !(value.colors instanceof Map)) ||
            (value.limits!=null && !(value.limits instanceof Map)))return
        def display=viewer.imageDisplay
        def channels=display.availableChannels().findAll {it instanceof qupath.lib.display.DirectServerChannelInfo}
        Set available=channels.collect {it.channel as int} as Set
        if(channels.isEmpty() || !value.channels.every {
            it instanceof Number && Double.isFinite(it.doubleValue()) &&
                it.doubleValue()==it.intValue() && available.contains(it.intValue())
        })return
        // Validate all relevant values first so a malformed profile cannot leave
        // a partially restored image or interrupt the coordinate picker.
        if(!channels.every {channel ->
            String key="${channel.channel}"
            def color=value.colors?.get(key)
            def limits=value.limits?.get(key)
            (color==null || (color instanceof String && color==~/#[0-9a-fA-F]{6}/)) &&
                (limits==null || (limits instanceof List && limits.size()==2 &&
                    limits.every {it instanceof Number && Float.isFinite(it.floatValue())} &&
                    limits[1].floatValue()>limits[0].floatValue()))
        })return
        Set selected=value.channels.collect {it.intValue()} as Set
        channels.each {channel ->
            String key="${channel.channel}"
            def color=value.colors?.get(key)
            if(color!=null) {
                int rgb=Integer.parseInt(color.substring(1),16)
                channel.setLUTColor((rgb>>16)&255,(rgb>>8)&255,rgb&255)
            }
            def limits=value.limits?.get(key)
            if(limits!=null)display.setMinMaxDisplay(channel,limits[0] as float,limits[1] as float)
        }
        display.setUseGrayscaleLuts(value.mode=='grayscale')
        display.availableChannels().each {channel -> display.setChannelSelected(channel,
            channel instanceof qupath.lib.display.DirectServerChannelInfo && selected.contains(channel.channel as int))}
        viewer.repaintEntireImage()
    }
}

if(args.length!=2)throw new IllegalArgumentException("Expected an export-region request and result path")
Path requestPath = Path.of(args[0])
Path resultPath = Path.of(args[1])
if(Files.size(requestPath)>256*1024 || Files.exists(resultPath))
    throw new IllegalArgumentException("Reopen the region picker from Results.")
Map request=new Gson().fromJson(Files.readString(requestPath),Map.class)
Path imagePath=Path.of(request.image_path)
def server=ImageServerProvider.buildServer(imagePath.toString(),BufferedImage.class)
if(server.width!=request.size_x || server.height!=request.size_y ||
    Math.abs(server.pixelCalibration.pixelWidthMicrons-(request.pixel_width_um as double))>1e-12 ||
    Math.abs(server.pixelCalibration.pixelHeightMicrons-(request.pixel_height_um as double))>1e-12) {
    server.close()
    throw new IllegalArgumentException("The displayed image differs from the source-pixel export grid.")
}
def image=new ImageData(server,ImageData.ImageType.FLUORESCENCE)
def initial=request.initial
def rectangle=PathObjects.createAnnotationObject(ROIs.createRectangleROI(
    initial.x as double,initial.y as double,initial.width as double,initial.height as double,ImagePlane.getDefaultPlane()))
image.hierarchy.addObject(rectangle)
image.setChanged(false)
CountDownLatch closed=new CountDownLatch(1)
def failure=new java.util.concurrent.atomic.AtomicReference<Throwable>()
Platform.startup({
    QuPathGUI gui=null
    try {
        gui=QuPathGUI.createHiddenInstance()
        Platform.setImplicitExit(false)
        gui.stage.width = 1250
        gui.stage.height = 900
        gui.viewer.imageData=image
        def viewer = gui.viewer
        // Loading image data clears selection; select only after the viewer is attached.
        viewer.setSelectedObject(rectangle)
        gui.toolManager.setSelectedTool(PathTools.MOVE)
        viewer.imageDisplay.setUseInvertedBackground(false)
        def window=new javafx.stage.Stage()
        window.initOwner(gui.stage)
        window.title="Choose export region"
        def heading=new javafx.scene.control.Label("Export field")
        heading.styleClass.add("picker-title")
        def guidance=new javafx.scene.control.Label("Choose the part of the image to export.")
        guidance.styleClass.add("picker-help")
        guidance.wrapText=true
        def scope=new javafx.scene.control.ComboBox<String>()
        scope.id="export-region-scope"
        scope.items.addAll("Current view","Selected annotation bounds")
        scope.value="Selected annotation bounds"
        scope.maxWidth=Double.MAX_VALUE
        scope.accessibleText="Export field source"
        def sourceLabel=new javafx.scene.control.Label("Field source")
        sourceLabel.labelFor=scope
        sourceLabel.styleClass.add("picker-field-caption")
        def editHeading=new javafx.scene.control.Label("Adjust the rectangle in QuPath")
        editHeading.styleClass.add("picker-section-heading")
        // Keep the tool and its shortcut with the instruction it enables.
        Closure instruction={String title,String shortcut,String detail ->
            def label=new javafx.scene.control.Label(title)
            label.styleClass.add("picker-instruction-title")
            def key=new javafx.scene.control.Label(shortcut)
            key.styleClass.add("picker-shortcut")
            def row=new javafx.scene.layout.HBox(8,label,key)
            row.alignment=javafx.geometry.Pos.CENTER_LEFT
            def help=new javafx.scene.control.Label(detail)
            help.wrapText=true
            help.styleClass.add("picker-help")
            new javafx.scene.layout.VBox(4,row,help)
        }
        def moveHelp=instruction("Move or resize","Move · M",
            "Double-click the rectangle to select it. Drag inside to move it, or drag a corner to resize it.")
        def drawHelp=instruction("Draw a replacement","Rectangle · R",
            "Delete the selected rectangle, then drag to draw a new one. Keep one rectangle selected to return its bounds.")
        def editing=new javafx.scene.layout.VBox(12,editHeading,moveHelp,drawHelp)
        editing.id="export-region-editing-help"
        editing.styleClass.add("picker-guidance")
        editing.visibleProperty().bind(scope.valueProperty().isEqualTo("Selected annotation bounds"))
        editing.managedProperty().bind(editing.visibleProperty())
        def viewHeading=new javafx.scene.control.Label("Frame the image in QuPath")
        viewHeading.styleClass.add("picker-section-heading")
        def viewHelp=new javafx.scene.control.Label("Pan to the area you want and zoom in to fit the size limit. The export uses the visible image field.")
        viewHelp.wrapText=true
        viewHelp.styleClass.add("picker-help")
        def framing=new javafx.scene.layout.VBox(7,viewHeading,viewHelp)
        framing.id="export-region-view-help"
        framing.styleClass.add("picker-guidance")
        framing.visibleProperty().bind(scope.valueProperty().isEqualTo("Current view"))
        framing.managedProperty().bind(framing.visibleProperty())
        def limitLabel=new javafx.scene.control.Label("Maximum field")
        limitLabel.styleClass.add("picker-help")
        limitLabel.maxWidth=Double.MAX_VALUE
        javafx.scene.layout.HBox.setHgrow(limitLabel,javafx.scene.layout.Priority.ALWAYS)
        def limitValue=new javafx.scene.control.Label("${request.max_width as int} × ${request.max_height as int} source pixels")
        limitValue.styleClass.add("picker-limit")
        def limit=new javafx.scene.layout.HBox(12,limitLabel,limitValue)
        limit.alignment=javafx.geometry.Pos.CENTER_LEFT
        def message=new javafx.scene.control.Label("")
        message.id = "export-region-message"
        message.wrapText = true
        message.visibleProperty().bind(message.textProperty().isNotEmpty())
        message.managedProperty().bind(message.visibleProperty())
        message.maxWidth=Double.MAX_VALUE
        message.styleClass.add("picker-error")
        def use=new javafx.scene.control.Button("Use field and return")
        use.id="export-region-use"
        use.styleClass.add("picker-primary")
        use.defaultButton=true
        def cancel=new javafx.scene.control.Button("Cancel")
        cancel.id="export-region-cancel"
        cancel.cancelButton=true
        // Drawing is local to this image-only window. Return or cancel discards it.
        Closure finish = { ->
            image.setChanged(false)
            window.hide()
            gui.stage.hide()
        }
        use.setOnAction({event->
            try {
                if(viewer.imageData!=image)throw new IllegalArgumentException("Use the prepared image opened by Results.")
                if(scope.value=="Selected annotation bounds" && !image.hierarchy.selectionModel.singleSelection())
                    throw new IllegalArgumentException("Select exactly one annotation. Use Move (M), then double-click the rectangle you want.")
                def bounds=CellFigureExport.capture(viewer,scope.value,1d).region()
                if(bounds.z!=0 || bounds.t!=0 || bounds.width>(request.max_width as int) || bounds.height>(request.max_height as int))
                    throw new IllegalArgumentException("Choose a region no larger than ${request.max_width as int} × ${request.max_height as int} source pixels. Zoom in or draw a smaller rectangle.")
                Map result=[x:bounds.x,y:bounds.y,width:bounds.width,height:bounds.height]
                Files.writeString(resultPath,new Gson().toJson(result),StandardOpenOption.CREATE_NEW)
                finish()
            } catch (Throwable error) {
                message.text = error.message ?: error.class.simpleName
            }
        } as javafx.event.EventHandler)
        cancel.setOnAction({event->finish()} as javafx.event.EventHandler)
        window.setOnCloseRequest({ event ->
            event.consume()
            finish()
        } as javafx.event.EventHandler)
        // Closing this image-only picker cancels the export choice, including unsaved drawing.
        gui.stage.setOnCloseRequest({ event ->
            event.consume()
            finish()
        } as javafx.event.EventHandler)
        gui.stage.setOnHidden({
            window.hide()
            Platform.runLater({
                Platform.exit()
                closed.countDown()
            } as Runnable)
        } as javafx.event.EventHandler)
        def header=new javafx.scene.layout.VBox(5,heading,guidance)
        header.padding=new javafx.geometry.Insets(20,22,18,22)
        def selection=new javafx.scene.layout.VBox(7,sourceLabel,scope)
        def body=new javafx.scene.layout.VBox(18,selection,editing,framing,limit,message)
        body.padding=new javafx.geometry.Insets(0,22,20,22)
        def actions=new javafx.scene.layout.HBox(10,cancel,use)
        actions.alignment=javafx.geometry.Pos.CENTER_RIGHT
        actions.padding=new javafx.geometry.Insets(14,22,14,22)
        actions.styleClass.add("picker-actions")
        def content=new javafx.scene.layout.VBox(header,body,actions)
        // JavaFX does not reliably select weights in Linux's variable Ubuntu font.
        // The static 400/600 faces match the main application's body and headings.
        Map loadedFontFamilies=[:]
        ["Regular","SemiBold"].each {weight->
            def stream=org.mif.cells.NativeCellScope.class.getResourceAsStream("/org/mif/fonts/ubuntu-sans/UbuntuSans-${weight}.ttf")
            if(stream!=null)try {
                def font=javafx.scene.text.Font.loadFont(stream,14d)
                if(font!=null)loadedFontFamilies[weight]=font.family
            }finally {stream.close()}
        }
        List fontPreferences=System.getProperty("os.name","").startsWith("Windows") ?
            ["Segoe UI",loadedFontFamilies.Regular ?: "Ubuntu Sans","DejaVu Sans"] :
            [loadedFontFamilies.Regular ?: "Ubuntu Sans","Ubuntu","DejaVu Sans"]
        String fontFamily=fontPreferences.find {it==loadedFontFamilies.Regular || javafx.scene.text.Font.families.contains(it)} ?: javafx.scene.text.Font.default.family
        // Use the loaded face directly: JavaFX may cache families before loading it.
        String headingFamily=(fontFamily==loadedFontFamilies.Regular && loadedFontFamilies.SemiBold) ?
            loadedFontFamilies.SemiBold : (["${fontFamily} SemiBold","${fontFamily} Semibold"].find {
                javafx.scene.text.Font.families.contains(it.toString())
            } ?: fontFamily)
        [heading,sourceLabel,editHeading,viewHeading].each {
            it.style="-fx-font-family:'${headingFamily.replace("'","")}';"
        }
        // Qt resolves the desktop's 11pt body to 15 logical pixels at 96 dpi.
        content.style="-fx-font-family:'${fontFamily.replace("'","")}';-fx-font-size:15px;"
        String css="""
            .root { -fx-background-color:white; -fx-accent:#006b70; -fx-focus-color:#006b70; -fx-faint-focus-color:transparent; }
            .label { -fx-text-fill:#172d35; }
            .picker-title { -fx-font-size:20px; -fx-font-weight:600; }
            .picker-section-heading { -fx-font-size:12pt; -fx-text-fill:#172d35; -fx-font-weight:600; }
            .picker-field-caption { -fx-text-fill:#294c56; -fx-font-weight:600; }
            .picker-instruction-title { -fx-text-fill:#294148; }
            .picker-help { -fx-text-fill:#486169; -fx-line-spacing:2; }
            .picker-shortcut { -fx-text-fill:#63787d; -fx-font-size:0.92em; }
            .picker-guidance { -fx-border-color:#dce4e4 transparent transparent transparent; -fx-border-width:1 0 0 0; -fx-padding:16 0 0 0; }
            .picker-limit { -fx-font-size:0.93em; -fx-text-fill:#294148; }
            .combo-box {
                -fx-background-color:white; -fx-background-insets:0; -fx-background-radius:3;
                -fx-border-color:#bdcccc; -fx-border-radius:3; -fx-border-width:1; -fx-padding:8 10;
            }
            .combo-box:hover { -fx-border-color:#8eaaa5; }
            .combo-box:focused { -fx-border-color:#006b70; -fx-border-width:2; -fx-padding:7 9; }
            .combo-box > .list-cell { -fx-background-color:transparent; -fx-text-fill:#172d35; -fx-padding:0 8 0 0; }
            .combo-box > .arrow-button { -fx-background-color:transparent; -fx-padding:0 2 0 8; }
            .combo-box .arrow { -fx-background-color:#526971; }
            .combo-box-popup .list-view { -fx-background-color:white; -fx-background-insets:0; -fx-border-color:#bdcccc; -fx-padding:3; }
            .combo-box-popup .list-cell { -fx-background-color:white; -fx-text-fill:#172d35; -fx-padding:8 10; }
            .combo-box-popup .list-cell:filled:hover, .combo-box-popup .list-cell:filled:selected { -fx-background-color:#e4f0ee; -fx-text-fill:#00575c; }
            .picker-actions { -fx-background-color:#f3f5f5; -fx-border-color:#dce4e4 transparent transparent transparent; -fx-border-width:1 0 0 0; }
            .button {
                -fx-background-color:white; -fx-background-insets:0; -fx-background-radius:3;
                -fx-border-color:#bdcccc; -fx-border-radius:3; -fx-border-width:1;
                -fx-text-fill:#294148; -fx-padding:8 14;
            }
            .button:hover { -fx-background-color:#eaf2ef; -fx-border-color:#8eaaa5; }
            .button:focused { -fx-border-color:#006b70; -fx-border-width:2; -fx-padding:7 13; }
            .button:pressed { -fx-background-color:#dcece6; }
            .picker-primary { -fx-background-color:#006b70; -fx-border-color:#006b70; -fx-text-fill:white; }
            .picker-primary:hover { -fx-background-color:#00575c; -fx-border-color:#00575c; }
            .picker-primary:focused { -fx-border-color:#82bcb3; }
            .picker-primary:pressed { -fx-background-color:#004b4f; }
            .picker-error { -fx-background-color:#fff3ef; -fx-border-color:#b4543e; -fx-border-width:0 0 0 3; -fx-text-fill:#8d3629; -fx-padding:10 12; -fx-line-spacing:2; }
        """
        content.stylesheets.add("data:text/css;base64,"+java.util.Base64.encoder.encodeToString(css.getBytes(java.nio.charset.StandardCharsets.UTF_8)))
        window.scene=new javafx.scene.Scene(content,472,-1)
        window.resizable=false
        // Context and validation take their natural height, without clipping or an
        // empty panel when the user switches to the shorter current-view guidance.
        Closure fitWindow={->Platform.runLater({window.sizeToScene()} as Runnable)}
        scope.valueProperty().addListener({observable,oldValue,newValue->
            message.text=""
            fitWindow()
        } as javafx.beans.value.ChangeListener)
        message.textProperty().addListener({observable,oldValue,newValue->fitWindow()} as javafx.beans.value.ChangeListener)
        gui.stage.show()
        // Centre after layout gives the viewer its actual image-display dimensions.
        Platform.runLater({
            // QuPath initializes channel display defaults asynchronously on opening.
            ExportRegionAppearance.apply(viewer,request.appearance)
            viewer.setDownsampleFactor(Math.max(0.25d, (initial.width as double) / 500d))
            viewer.setCenterPixelLocation((initial.x as double) + (initial.width as double) / 2,
                                          (initial.y as double) + (initial.height as double) / 2)
        } as Runnable)
        window.show()
    }catch(Throwable error){
        failure.set(error)
        if(gui!=null)gui.stage.hide()
        Platform.exit()
        closed.countDown()
    }
} as Runnable)
closed.await()
image.close()
if(failure.get()!=null)throw failure.get()
