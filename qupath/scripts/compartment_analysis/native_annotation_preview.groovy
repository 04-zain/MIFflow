// Provisional annotation feedback reuses the exact native fitting primitives.
// It never saves a classifier. A lease prevents replacing/freeing a model while a tile is using it.
import javafx.application.Platform
import javafx.stage.Stage
import javafx.stage.WindowEvent
import javafx.scene.Scene
import javafx.scene.control.*
import javafx.scene.layout.VBox
import javafx.scene.layout.HBox
import javafx.scene.layout.FlowPane
import javafx.scene.layout.BorderPane
import javafx.scene.layout.Priority
import javafx.geometry.Insets
import java.util.concurrent.Executors
import qupath.lib.classifiers.pixel.PixelClassifier
import qupath.lib.classifiers.pixel.PixelClassifierMetadata
import qupath.lib.images.ImageData
import qupath.lib.objects.PathObjects
import qupath.lib.objects.hierarchy.PathObjectHierarchy
import qupath.lib.objects.hierarchy.events.PathObjectHierarchyListener
import qupath.lib.objects.classes.PathClass
import qupath.lib.regions.RegionRequest
import java.awt.image.BufferedImage
import com.google.gson.GsonBuilder
import com.google.gson.ToNumberPolicy

final class MifProvisionalClassifier implements PixelClassifier,AutoCloseable {
    final PixelClassifier inner
    private final Closure release
    private boolean closed=false
    MifProvisionalClassifier(PixelClassifier inner,Closure release){this.inner=inner;
        this.release=release}
    PixelClassifierMetadata getMetadata(){inner.metadata}
    synchronized boolean supportsImage(ImageData image){!closed && inner.supportsImage(image)}
    synchronized BufferedImage applyClassification(ImageData image,RegionRequest request) {
        if(closed)throw new IOException("The annotation preview was replaced.")
        inner.applyClassification(image,request)
    }
    synchronized void close(){if(!closed){closed=true;
            release()}}
}

final class MifNativeAnnotationPreview implements AutoCloseable {
    /** Local presentation only; keep typography aligned with the native export picker. */
    private static void stylePanel(javafx.scene.Parent layout) {
        Map loadedFontFamilies=[:]
        ["Regular","SemiBold"].each {weight->
            def stream=org.mif.cells.NativeCellScope.class.getResourceAsStream("/org/mif/fonts/ubuntu-sans/UbuntuSans-${weight}.ttf")
            if(stream!=null)try {
                def font=javafx.scene.text.Font.loadFont(stream,15d)
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
        layout.style="-fx-font-family:'${fontFamily.replace("'","")}';-fx-font-size:15px;"
        String css="""
            .root { -fx-background-color:white; -fx-accent:#006b70; -fx-focus-color:#006b70; -fx-faint-focus-color:transparent; }
            .label { -fx-text-fill:#172d35; }
            .mif-title, .mif-section-title, .mif-caption, .titled-pane > .title {
                -fx-font-family:'${headingFamily.replace("'","")}'; -fx-font-weight:600;
            }
            .mif-title { -fx-font-size:20px; }
            .mif-section-title { -fx-font-size:16px; }
            .mif-caption { -fx-text-fill:#294c56; }
            .mif-help { -fx-text-fill:#486169; -fx-line-spacing:2; }
            .mif-section { -fx-border-color:#dce4e4 transparent transparent transparent; -fx-border-width:1 0 0 0; -fx-padding:16 0 0 0; }
            .mif-footer { -fx-background-color:#f3f5f5; -fx-border-color:#dce4e4 transparent transparent transparent; -fx-border-width:1 0 0 0; }
            .mif-status { -fx-text-fill:#486169; -fx-line-spacing:2; }
            .mif-class-labels { -fx-background-color:#f3f5f5; -fx-background-radius:3; -fx-padding:12 14; }
            .mif-class-chip { -fx-background-color:white; -fx-border-color:#dce4e4; -fx-border-radius:3; -fx-background-radius:3; -fx-padding:6 10; }
            .scroll-pane, .scroll-pane > .viewport { -fx-background-color:white; -fx-background-insets:0; -fx-padding:0; }
            .text-field, .combo-box, .text-area {
                -fx-background-color:white; -fx-background-insets:0; -fx-background-radius:3;
                -fx-border-color:#bdcccc; -fx-border-radius:3; -fx-border-width:1;
                -fx-text-fill:#172d35; -fx-padding:7 10;
            }
            .text-field:hover, .combo-box:hover { -fx-border-color:#8eaaa5; }
            .text-field:focused, .combo-box:focused { -fx-border-color:#006b70; -fx-border-width:2; -fx-padding:6 9; }
            .combo-box > .list-cell { -fx-background-color:transparent; -fx-text-fill:#172d35; -fx-padding:0 8 0 0; }
            .combo-box:disabled { -fx-opacity:1; -fx-background-color:#f3f5f5; -fx-border-color:#dce4e4; }
            .combo-box:disabled > .list-cell { -fx-opacity:1; -fx-text-fill:#486169; }
            .combo-box:disabled > .arrow-button { -fx-opacity:1; }
            .combo-box > .arrow-button { -fx-background-color:transparent; -fx-padding:0 2 0 8; }
            .combo-box .arrow { -fx-background-color:#526971; }
            .combo-box-popup .list-view { -fx-background-color:white; -fx-background-insets:0; -fx-border-color:#bdcccc; -fx-padding:3; }
            .combo-box-popup .list-cell { -fx-background-color:white; -fx-text-fill:#172d35; -fx-padding:8 10; }
            .combo-box-popup .list-cell:filled:hover, .combo-box-popup .list-cell:filled:selected { -fx-background-color:#e4f0ee; -fx-text-fill:#00575c; }
            .text-area .content { -fx-background-color:white; }
            .button {
                -fx-background-color:white; -fx-background-insets:0; -fx-background-radius:3;
                -fx-border-color:#bdcccc; -fx-border-radius:3; -fx-border-width:1;
                -fx-text-fill:#294148; -fx-padding:8 14;
            }
            .button:hover { -fx-background-color:#eaf2ef; -fx-border-color:#8eaaa5; }
            .button:focused { -fx-border-color:#006b70; -fx-border-width:2; -fx-padding:7 13; }
            .button:pressed { -fx-background-color:#dcece6; }
            .mif-primary { -fx-background-color:#006b70; -fx-border-color:#006b70; -fx-text-fill:white; }
            .mif-primary:hover { -fx-background-color:#00575c; -fx-border-color:#00575c; }
            .mif-primary:focused { -fx-border-color:#82bcb3; }
            .mif-primary:pressed { -fx-background-color:#004b4f; }
            .check-box { -fx-text-fill:#172d35; }
            .check-box > .box { -fx-background-color:white; -fx-background-insets:0; -fx-background-radius:3; -fx-border-color:#bdcccc; -fx-border-radius:3; -fx-padding:3; }
            .check-box:selected > .box { -fx-background-color:#006b70; -fx-border-color:#006b70; }
            .check-box:selected .mark { -fx-background-color:white; }
            .slider .track { -fx-background-color:#dce4e4; -fx-background-insets:0; }
            .slider .thumb { -fx-background-color:#006b70; -fx-background-insets:0; }
            .titled-pane { -fx-text-fill:#294c56; }
            .titled-pane > .title { -fx-background-color:white; -fx-background-insets:0; -fx-background-radius:0; -fx-border-color:#dce4e4 transparent transparent transparent; -fx-border-width:1 0 0 0; -fx-padding:12 0 8 0; }
            .titled-pane > .title > .arrow-button .arrow { -fx-background-color:#526971; }
            .titled-pane > .content { -fx-background-color:white; -fx-border-width:0; -fx-padding:4 0 8 0; }
        """
        layout.stylesheets.add("data:text/css;base64,"+java.util.Base64.encoder.encodeToString(css.getBytes(java.nio.charset.StandardCharsets.UTF_8)))
    }

    final Object gui
    final Object viewer
    final Object image
    final Object parent
    final Map request
    final Map support
    final Stage stage=new Stage()
    final Label status=new Label("Preview not fitted. Add the reference regions, then update the preview.")
    final TextArea diagnostic=new TextArea()
    final Button updateButton=new Button("Update preview")
    final Slider opacity=new Slider(0d,1d,0.45d)
    final Object compute=Executors.newSingleThreadExecutor()
    final Object cleanup=Executors.newSingleThreadExecutor()
    private final PathObjectHierarchyListener changed
    private final javafx.beans.value.ChangeListener imageChanged
    private final javafx.event.EventHandler hidden
    private final Object priorOverlay
    private final boolean priorVisible
    private final Object geometry
    private final Map tools
    private Object overlay=null
    private MifProvisionalClassifier model=null
    private Map pending=null
    private Object fitMethod=null
    private long revision=0
    private boolean busy=false
    private volatile boolean closed=false
    volatile boolean resourcesClosed=false
    int fitCount=0

    MifNativeAnnotationPreview(gui,Map request,parent,Map support) {
        if(!Platform.isFxApplicationThread())throw new IllegalStateException("Open annotation preview on the QuPath application thread.")
        this.gui=gui;
        viewer=gui.viewer;
        image=viewer.imageData;
        this.request=request;
        this.parent=parent;
        this.support=support
        if(image==null || gui.readOnly || request.mode!="exploratory" || request.configuration.configuration_schema.startsWith("qupath-kronos"))
            throw new IllegalArgumentException("Open this explicit RF/SVM preview while editing its development image.")
        geometry=parent.ROI.geometry.copy()
        priorOverlay=viewer.customPixelLayerOverlay;
        priorVisible=gui.overlayOptions.showPixelClassification
        image.setProperty("mif.annotation_feedback","model_assisted")
        status.id="prediction-status";status.wrapText=true;status.styleClass.add("mif-status");
        diagnostic.editable=false;
        diagnostic.wrapText=true;
        diagnostic.prefRowCount=4
        String methodName=request.configuration.configuration_schema=="qupath-pixel-rtrees/v1"?"Random forest":"Support vector machine"
        Label title=new Label(methodName+" annotation preview");
        title.wrapText=true
        title.styleClass.add("mif-title")
        VBox header=new VBox(title);
        header.padding=new Insets(20,22,18,22)
        Label referencesTitle=new Label("1  Draw and label references");
        referencesTitle.styleClass.add("mif-section-title")
        Label drawingInstruction=new Label("Draw reference regions inside the selected area.");
        drawingInstruction.wrapText=true;drawingInstruction.styleClass.add("mif-help")
        VBox references=new VBox(10,referencesTitle,drawingInstruction)
        tools=support.annotationTools?.create?.call(gui,stage);
        if(tools!=null) {
            tools.node.lookupAll('.label').each {it.styleClass.add(it.id=='annotation-drawing-help'?'mif-help':'mif-caption')}
            references.children.add(tools.node)
        }
        Label labelsTitle=new Label("Class labels");labelsTitle.styleClass.add("mif-caption")
        labelsTitle.minWidth=javafx.scene.layout.Region.USE_PREF_SIZE
        FlowPane classLegend=new FlowPane(8d,8d);classLegend.minWidth=0d;classLegend.id="prediction-class-legend"
        request.labels.each {row->
            def pathClass=PathClass.fromString(row.name)
            Label chip=new Label(row.name);chip.styleClass.add("mif-class-chip")
            chip.wrapText=true;chip.maxWidthProperty().bind(classLegend.widthProperty())
            // FlowPane otherwise measures a wrapped Label at its unbounded height.
            chip.minHeightProperty().bind(javafx.beans.binding.Bindings.createDoubleBinding(
                {->chip.prefHeight(classLegend.width>0d?classLegend.width:classLegend.prefWrapLength)} as java.util.concurrent.Callable<Double>,
                classLegend.widthProperty(),chip.fontProperty(),chip.paddingProperty()))
            if(pathClass.color!=null) {
                int color=pathClass.color
                chip.graphic=new javafx.scene.shape.Rectangle(10d,10d,javafx.scene.paint.Color.rgb((color>>16)&255,(color>>8)&255,color&255))
                chip.graphicTextGap=7d
            }
            classLegend.children.add(chip)
        }
        Label labelingInstruction=new Label("In QuPath’s Annotations tab, choose a class and use Set selected to label the selected region.");
        labelingInstruction.wrapText=true;labelingInstruction.styleClass.add("mif-help")
        HBox legendRow=new HBox(12d,labelsTitle,classLegend);legendRow.alignment=javafx.geometry.Pos.TOP_LEFT
        HBox.setMargin(labelsTitle,new Insets(7d,0d,0d,0d))
        HBox.setHgrow(classLegend,Priority.ALWAYS)
        VBox labels=new VBox(8d,legendRow,labelingInstruction)
        labels.id="prediction-class-labels";labels.styleClass.add("mif-class-labels")
        references.children.add(labels)
        Label previewTitle=new Label("2  Fit and inspect predictions");
        previewTitle.styleClass.add("mif-section-title");previewTitle.wrapText=true
        previewTitle.maxWidth=Double.MAX_VALUE;previewTitle.minWidth=0
        updateButton.id="prediction-update";updateButton.styleClass.add("mif-primary")
        updateButton.minWidth=javafx.scene.layout.Region.USE_PREF_SIZE
        HBox previewHeading=new HBox(12d,previewTitle,updateButton)
        previewHeading.id="prediction-heading";previewHeading.alignment=javafx.geometry.Pos.CENTER_LEFT
        HBox.setHgrow(previewTitle,Priority.ALWAYS)
        opacity.id="prediction-opacity"
        Label opacityValue=new Label("45%");opacityValue.id="prediction-opacity-value";
        opacityValue.minWidth=javafx.scene.layout.Region.USE_PREF_SIZE
        Label opacityName=new Label("Overlay opacity");
        opacityName.labelFor=opacity;opacityName.styleClass.add("mif-caption")
        opacityName.minWidth=javafx.scene.layout.Region.USE_PREF_SIZE
        opacity.minWidth=60d
        HBox display=new HBox(10d,opacityName,opacity,opacityValue);
        display.alignment=javafx.geometry.Pos.CENTER_LEFT
        HBox.setHgrow(opacity,Priority.ALWAYS)
        VBox preview=new VBox(12d,previewHeading,status,display)
        preview.styleClass.add("mif-section")
        status.maxWidth=Double.MAX_VALUE
        Label recipe=new Label(request.uses_first_grid_candidate ? "Update preview fits the first parameter combination to this image’s current reference annotations." : "Update preview trains the classifier using the entered settings and this image’s current reference annotations.");
        recipe.wrapText=true
        Label caution=new Label("Viewing predictions can influence which regions you annotate and how you label them. The saved annotations record that this preview was enabled.");
        caution.wrapText=true
        Label persistence=new Label("Save the reference annotations in QuPath, then return to the pipeline to train and save the classifier using the completed annotations. The model fitted here is used for this preview only.");
        persistence.wrapText=true
        TitledPane methodDetails=new TitledPane("Preview method and reference feedback",new VBox(9,recipe,caution,persistence));
        methodDetails.id="prediction-method-details"
        [recipe,caution,persistence].each {it.styleClass.add("mif-help")}
        methodDetails.content.padding=new Insets(4,0,6,0)
        methodDetails.minWidth=0
        methodDetails.expanded=false
        Button closeButton=new Button("Close preview");closeButton.id="prediction-close";
        closeButton.setOnAction({event->close()} as javafx.event.EventHandler)
        TitledPane details=new TitledPane("Technical details",diagnostic);
        details.expanded=false;details.id="prediction-technical-details"
        VBox body=new VBox(18,references,preview,methodDetails,details)
        body.minWidth=0
        body.padding=new Insets(0,22,20,22);
        ScrollPane scroll=new ScrollPane(body);
        scroll.fitToWidth=true;
        scroll.hbarPolicy=ScrollPane.ScrollBarPolicy.NEVER
        Label saveHint=new Label("Save annotations in QuPath.");
        saveHint.wrapText=true;saveHint.styleClass.add("mif-help");
        saveHint.maxWidth=Double.MAX_VALUE
        HBox footer=new HBox(12,saveHint,closeButton);
        footer.alignment=javafx.geometry.Pos.CENTER_LEFT;
        footer.padding=new Insets(14,22,14,22);footer.styleClass.add("mif-footer");
        HBox.setHgrow(saveHint,Priority.ALWAYS)
        BorderPane layout=new BorderPane(scroll,header,null,footer,null);
        stylePanel(layout)
        stage.initOwner(gui.stage);
        stage.title="Annotation preview";
        stage.scene=new Scene(layout,560,-1);
        stage.minWidth=500;
        stage.minHeight=440
        stage.setOnCloseRequest({event->event.consume();
            close()} as javafx.event.EventHandler)
        updateButton.setOnAction({event->requestUpdate()} as javafx.event.EventHandler)
        opacity.valueProperty().addListener({o,a,b->
            opacityValue.text=Math.round(b.doubleValue()*100d)+"%"
            if(overlay!=null){overlay.opacity=b.doubleValue();
                viewer.forceOverlayUpdate()}} as javafx.beans.value.ChangeListener)
        changed={event->
            if(event.isChanging() || event.isObjectMeasurementEvent())return
            Runnable invalidate={if(!closed){revision++;
                    pending=null;
                    retire();
                    status.text="References changed. Update the preview to fit the current annotations.";
                    }} as Runnable
            if(Platform.isFxApplicationThread())invalidate.run();
            else Platform.runLater(invalidate)
        } as PathObjectHierarchyListener
        imageChanged={o,a,b->if(b!=image)close()} as javafx.beans.value.ChangeListener
        hidden={event->close()} as javafx.event.EventHandler
        image.hierarchy.addListener(changed);
        viewer.imageDataProperty().addListener(imageChanged);
        gui.stage.addEventHandler(WindowEvent.WINDOW_HIDDEN,hidden)
        // Size the initial view to its wrapped content; keep the footer reachable
        // on smaller screens through the existing scroll area.
        layout.applyCss()
        def screens=javafx.stage.Screen.getScreensForRectangle(gui.stage.x,gui.stage.y,gui.stage.width,gui.stage.height)
        def bounds=(screens.isEmpty()?javafx.stage.Screen.primary:screens.first()).visualBounds
        double availableHeight=Math.max(1d,bounds.height-40d)
        stage.minHeight=Math.min(stage.minHeight,availableHeight)
        scroll.prefViewportHeight=Math.min(body.prefHeight(558d),
            Math.max(1d,availableHeight-header.prefHeight(560d)-footer.prefHeight(560d)-40d))
        stage.sizeToScene()
        stage.show()
        stage.height=Math.min(stage.height,availableHeight)
        stage.y=Math.max(bounds.minY,Math.min(stage.y,bounds.maxY-stage.height))
    }

    private List annotations() {
        if(!parent.ROI.geometry.equalsExact(geometry))throw new IllegalArgumentException("The preview region changed. Close this draft and reopen the current annotation step.")
        List names=request.labels.collect {it.name}
        image.hierarchy.annotationObjects.findAll {object->
            object.ROI!=null && object.ROI.isArea() && !object.isLocked() && object.metadata.get("pipeline_stage")==null &&
            object.ROI.z==parent.ROI.z && object.ROI.t==parent.ROI.t && names.contains(object.pathClass?.toString()) && object.ROI.geometry.coveredBy(geometry)
        }.collect {object->[id:object.ID.toString(),name:object.pathClass.toString(),roi:object.ROI.duplicate()]}
    }
    void requestUpdate() {
        if(closed)return
        try {pending=[revision:++revision,annotations:annotations()];
            status.text=busy?"The latest references are queued.":"Fitting the annotation preview…";
            startPending()}
        catch(Throwable error){status.text=error.message;
            diagnostic.text=error.toString()}
    }
    private MifProvisionalClassifier fit(List annotations) {
        if(annotations.collect {it.name}.toSet()!=request.labels.collect {it.name}.toSet())throw new IllegalArgumentException("Draw at least one region for every selected reference class inside this preview region.")
        if(fitMethod==null) {
            def shell=new GroovyShell(this.class.classLoader)
            shell.parse(support.nativeTrainingSource,"02_train_native_classifier.groovy")
            fitMethod=shell.classLoader.loadClass("MifNativeTrainingAssetV1").getDeclaredMethod("fitSelectionCandidate",Map,List,List,Map)
            fitMethod.accessible=true
        }
        def hierarchy=new PathObjectHierarchy()
        annotations.each {row->hierarchy.addObject(PathObjects.createAnnotationObject(row.roi,PathClass.fromString(row.name)))}
        def view=new ImageData(image.server,hierarchy,image.imageType)
        Map wire=[model_configuration:request.configuration,random_seed:request.seed,required_classes:request.labels.collect {it.class_id},
            feature_spec:[ordered_channels:request.channels.collect {row->[canonical_channel_id:row.channel_id,qupath_channel_index:row.index,observed_qupath_channel_name:row.name]}],
            training_entries:[[entry_state:[project_entry_id:request.source_entry_id],evidence_rows:annotations.collect {row->
                [class_id:request.labels.find{it.name==row.name}.class_id,path_class_id:row.name]}]]]
        def gson=new GsonBuilder().serializeNulls().setObjectToNumberStrategy(ToNumberPolicy.LONG_OR_DOUBLE).create()
        Map exact=gson.fromJson(gson.toJson(wire),Map.class)
        Map fitted
        try {fitted=fitMethod.invoke(null,exact,[image],[view],null)}
        catch(java.lang.reflect.InvocationTargetException error){throw error.cause}
        new MifProvisionalClassifier(fitted.classifier,fitted.close)
    }
    private void startPending() {
        if(closed || busy || pending==null)return
        Map work=pending;
        pending=null;
        busy=true
        compute.submit({
            MifProvisionalClassifier fitted=null;
            Throwable failure=null
            try{fitted=fit(work.annotations)}catch(Throwable error){failure=error}
            def value=fitted;
            def error=failure
            Platform.runLater({
                busy=false
                if(closed || work.revision!=revision){if(value!=null)cleanup.submit({value.close()} as Runnable)}
                else if(error!=null){status.text=error.message?:"Preview fitting failed.";
                    diagnostic.text=error.toString()}
                else {
                    try {
                        def scopes=[[id:parent.ID.toString(),geometry:geometry,revision:"provisional",split:"training",z:parent.ROI.z,t:parent.ROI.t]]
                        def candidate=support.classifierReview.createOverlay.call(gui.overlayOptions,image,value,scopes,support.displayPolicy)
                        candidate.opacity=opacity.value;
                        candidate.livePrediction=true
                        retire();
                        model=value;
                        overlay=candidate;
                        viewer.customPixelLayerOverlay=candidate;
                        gui.overlayOptions.showPixelClassification=true
                        fitCount++;
                        status.text="Predictions updated. Inspect the labelled classes across the selected region.";
                        diagnostic.clear();
                        viewer.forceOverlayUpdate()
                    }catch(Throwable problem){cleanup.submit({value.close()} as Runnable);
                        status.text="The preview could not be displayed.";
                        diagnostic.text=problem.toString()}
                }
                startPending()
            } as Runnable)
        } as Runnable)
    }
    private void retire() {
        def previous=overlay;
        overlay=null;
        def fitted=model;
        model=null
        previous?.stop();
        if(previous!=null && viewer.customPixelLayerOverlay?.is(previous))viewer.customPixelLayerOverlay=priorOverlay
        if(fitted!=null)cleanup.submit({fitted.close()} as Runnable)
    }
    void close() {
        if(closed)return
        closed=true;
        revision++;
        pending=null;
        image.hierarchy.removeListener(changed);
        viewer.imageDataProperty().removeListener(imageChanged);
        gui.stage.removeEventHandler(WindowEvent.WINDOW_HIDDEN,hidden)
        tools?.close?.call();
        retire();
        gui.overlayOptions.showPixelClassification=priorVisible;
        stage.hide()
        compute.submit({Platform.runLater({cleanup.submit({resourcesClosed=true} as Runnable);
                cleanup.shutdown()} as Runnable)} as Runnable)
        compute.shutdown()
    }
}
return [open:{gui,Map request,parent,Map support->new MifNativeAnnotationPreview(gui,request,parent,support)}]
