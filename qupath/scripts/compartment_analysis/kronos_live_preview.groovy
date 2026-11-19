// Provisional QuPath feedback fits graph features and RF/SVM from one annotation snapshot.
import javafx.application.Platform
import javafx.stage.Stage
import javafx.scene.Scene
import javafx.scene.control.*
import javafx.scene.layout.VBox
import javafx.scene.layout.HBox
import javafx.scene.layout.FlowPane
import javafx.scene.layout.BorderPane
import javafx.scene.layout.Priority
import javafx.geometry.Insets
import java.util.concurrent.Executors
import java.util.concurrent.ExecutorService
import qupath.lib.gui.QuPathGUI
import qupath.lib.gui.viewer.overlays.PixelClassificationOverlay
import qupath.lib.objects.hierarchy.events.PathObjectHierarchyListener
import qupath.lib.objects.classes.PathClass
import qupath.opencv.ml.pixel.PixelClassifiers

final class MifKronosLivePreview implements AutoCloseable {
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

    final QuPathGUI gui
    final Object viewer
    private final javafx.beans.value.ChangeListener imageListener
    final Object backend
    final List<String> classes
    final Map graphSettings
    final Map nativeSettings
    final Map operations
    final Stage stage=new Stage()
    final Label status=new Label("Preview not fitted.")
    final Button updateButton=new Button("Update preview")
    final ComboBox<String> method=new ComboBox<>()
    final Label phaseLabel=new Label()
    final Label phaseDescription=new Label()
    final TextArea diagnostic=new TextArea()
    final Slider opacity=new Slider(0.0,1.0,0.45)
    private final ExecutorService compute=Executors.newSingleThreadExecutor()
    private final ExecutorService control=Executors.newSingleThreadExecutor()
    private final PathObjectHierarchyListener annotationListener
    private final boolean previousPredictionVisibility
    private final Object previousOverlay
    private Map drawingControls=null
    private Object overlay=null
    private Object provider=null
    private Object nativeFit=null
    private Map pending=null
    private volatile long requestedRevision=0
    private volatile boolean closed=false
    volatile boolean busy=false
    volatile boolean resourcesClosed=false
    Map fitted=null
    int completedSpatialFits=0
    int completedClassifierFits=0

    MifKronosLivePreview(QuPathGUI gui,Object backend,List<String> classes,Map graphSettings,
                         Map nativeSettings,Map operations) {
        if(!Platform.isFxApplicationThread()) throw new IllegalStateException("Create preview controls on the QuPath application thread")
        if(gui.getImageData()!=backend.image) throw new IllegalArgumentException("Preview image must match the current training image")
        this.gui=gui;
        this.viewer=gui.viewer;
        this.backend=backend;
        this.classes=List.copyOf(classes)
        this.graphSettings=Map.copyOf(graphSettings);
        this.nativeSettings=Collections.unmodifiableMap(new LinkedHashMap(nativeSettings));
        this.operations=operations
        previousOverlay=viewer.customPixelLayerOverlay
        previousPredictionVisibility=gui.overlayOptions.getShowPixelClassification()
        method.items.addAll("Random forest (RF)","Support vector machine (SVM)");
        method.selectionModel.select(operations.initialMethod=="svm"?1:0)
        method.disable=operations.lockMethod==true
        method.id="prediction-classifier"
        Label title=new Label("KRONOS annotation preview");
        title.wrapText=true;title.styleClass.add("mif-title")
        VBox header=new VBox(title);
        header.padding=new Insets(20,22,18,22)
        Label referencesTitle=new Label("1  Draw and label references");
        referencesTitle.styleClass.add("mif-section-title")
        Label drawingInstruction=new Label("Fully cover each training patch with one class. Reference regions from different classes must not overlap.");
        drawingInstruction.wrapText=true;drawingInstruction.styleClass.add("mif-help")
        VBox references=new VBox(10,referencesTitle,drawingInstruction)
        drawingControls=operations.annotationTools?.create?.call(gui,stage)
        if(drawingControls!=null) {
            drawingControls.node.lookupAll('.label').each {it.styleClass.add(it.id=='annotation-drawing-help'?'mif-help':'mif-caption')}
            references.children.add(drawingControls.node)
        }
        Label labelsTitle=new Label("Class labels");labelsTitle.styleClass.add("mif-caption")
        labelsTitle.minWidth=javafx.scene.layout.Region.USE_PREF_SIZE
        FlowPane classLegend=new FlowPane(8d,8d);classLegend.minWidth=0d;classLegend.id="prediction-class-legend"
        classes.each {name->
            def pathClass=PathClass.fromString(name)
            Label chip=new Label(name);chip.styleClass.add("mif-class-chip")
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
        status.setWrapText(true);
        status.maxWidth=Double.MAX_VALUE;
        status.id="prediction-status";status.styleClass.add("mif-status")
        phaseLabel.styleClass.add("mif-section-title");phaseLabel.id="prediction-phase"
        phaseLabel.wrapText=true;phaseLabel.maxWidth=Double.MAX_VALUE;phaseLabel.minWidth=0
        updateButton.minWidth=javafx.scene.layout.Region.USE_PREF_SIZE
        HBox previewHeading=new HBox(12d,phaseLabel,updateButton)
        previewHeading.id="prediction-heading";previewHeading.alignment=javafx.geometry.Pos.CENTER_LEFT
        HBox.setHgrow(phaseLabel,Priority.ALWAYS)
        phaseDescription.wrapText=true;phaseDescription.styleClass.add("mif-help")
        Label methodName=new Label("Classifier");
        methodName.minWidth=javafx.scene.layout.Region.USE_PREF_SIZE
        methodName.labelFor=method;methodName.styleClass.add("mif-caption");
        method.minWidth=0
        method.maxWidth=Double.MAX_VALUE
        HBox methodRow=new HBox(10,methodName,method);
        methodRow.alignment=javafx.geometry.Pos.CENTER_LEFT;
        HBox.setHgrow(method,Priority.ALWAYS)
        VBox fitting=new VBox(10,previewHeading,methodRow)
        opacity.id="prediction-opacity"
        Label opacityValue=new Label("45%");opacityValue.id="prediction-opacity-value";
        opacityValue.minWidth=javafx.scene.layout.Region.USE_PREF_SIZE
        Label opacityName=new Label("Overlay opacity");
        opacityName.labelFor=opacity;opacityName.styleClass.add("mif-caption");
        opacityName.minWidth=javafx.scene.layout.Region.USE_PREF_SIZE
        opacity.minWidth=60d
        HBox display=new HBox(10d,opacityName,opacity,opacityValue);
        display.alignment=javafx.geometry.Pos.CENTER_LEFT
        HBox.setHgrow(opacity,Priority.ALWAYS)
        VBox inspection=new VBox(12d,status,display)
        VBox preview=new VBox(12d,fitting,inspection);preview.styleClass.add("mif-section")
        Label previewMethod=new Label("KRONOS encoder weights stay fixed. The overlay shows the selected classifier’s predictions.");
        previewMethod.wrapText=true
        Label caution=new Label("Viewing predictions can influence which regions you annotate and how you label them. The saved annotations record that this preview was enabled.");
        caution.wrapText=true
        Label persistence=new Label("Save the reference annotations in QuPath, then return to the pipeline to train and save the classifier using the completed annotations. The model fitted here is used for this preview only.");
        persistence.wrapText=true
        TitledPane methodDetails=new TitledPane("Preview method and reference feedback",new VBox(9,phaseDescription,previewMethod,caution,persistence));
        methodDetails.id="prediction-method-details"
        [previewMethod,caution,persistence].each {it.styleClass.add("mif-help")}
        methodDetails.content.padding=new Insets(4,0,6,0)
        methodDetails.expanded=false
        diagnostic.setEditable(false);
        diagnostic.setWrapText(true);
        diagnostic.setPrefRowCount(4)
        TitledPane details=new TitledPane("Technical details",diagnostic);
        details.setExpanded(false);details.id="prediction-technical-details"
        Button closeButton=new Button("Close preview");closeButton.id="prediction-close";
        closeButton.setOnAction({close()} as javafx.event.EventHandler)
        VBox content=new VBox(18,references,preview,methodDetails,details)
        content.padding=new Insets(0,22,20,22)
        updateButton.id="prediction-update";updateButton.styleClass.add("mif-primary")
        ScrollPane scroll=new ScrollPane(content);
        scroll.setFitToWidth(true);
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
        stage.setTitle("KRONOS annotation preview");
        stage.setScene(new Scene(layout,560,-1));
        stage.minWidth=500;
        stage.minHeight=460
        stage.setOnCloseRequest({ event -> event.consume();
            close() } as javafx.event.EventHandler)
        updateButton.setOnAction({requestUpdate()} as javafx.event.EventHandler)
        opacity.valueProperty().addListener({ observable,oldValue,newValue ->
            opacityValue.text=Math.round(newValue.doubleValue()*100d)+"%"
            if(overlay!=null) { overlay.setOpacity(newValue.doubleValue());
                viewer.forceOverlayUpdate() }
        } as javafx.beans.value.ChangeListener)
        method.valueProperty().addListener({ observable,oldValue,newValue ->
            requestedRevision++;
            pending=null;
            retireOverlay();
            status.text="Classifier changed. Update the preview to fit the selected method.";
            refreshControls()
        } as javafx.beans.value.ChangeListener)
        annotationListener={ event ->
            if(event.isChanging() || event.isObjectMeasurementEvent()) return
            Runnable invalidate={
                if(!closed) {
                    requestedRevision++;
                    pending=null;
                    retireOverlay()
                    status.text="References changed. Update the preview to refit the graph and classifier together."
                    refreshControls()
                }
            } as Runnable
            if(Platform.isFxApplicationThread())invalidate.run();
            else Platform.runLater(invalidate)
        } as PathObjectHierarchyListener
        backend.image.hierarchy.addListener(annotationListener)
        imageListener={ observable,previous,current ->
            if(current!=backend.image && !closed) close()
        } as javafx.beans.value.ChangeListener
        viewer.imageDataProperty().addListener(imageListener)
        refreshControls();
        // Fit the initial wrapped content and scroll within the available screen.
        layout.applyCss()
        def screens=javafx.stage.Screen.getScreensForRectangle(gui.stage.x,gui.stage.y,gui.stage.width,gui.stage.height)
        def bounds=(screens.isEmpty()?javafx.stage.Screen.primary:screens.first()).visualBounds
        double availableHeight=Math.max(1d,bounds.height-40d)
        stage.minHeight=Math.min(stage.minHeight,availableHeight)
        scroll.prefViewportHeight=Math.min(content.prefHeight(558d),
            Math.max(1d,availableHeight-header.prefHeight(560d)-footer.prefHeight(560d)-40d))
        stage.sizeToScene()
        stage.show()
        stage.height=Math.min(stage.height,availableHeight)
        stage.y=Math.max(bounds.minY,Math.min(stage.y,bounds.maxY-stage.height))
    }

    private List<Map> annotations() {
        return backend.image.hierarchy.getAnnotationObjects().findAll { object ->
            object.getROI()!=null && object.getROI().isArea() && object.getROI().getZ()==0 && object.getROI().getT()==0 &&
            object.getPathClass()!=null && !object.isLocked() && object.metadata.get("pipeline_stage")==null && classes.contains(object.getPathClass().toString()) &&
            (operations.referenceGeometry==null || object.ROI.geometry.coveredBy(operations.referenceGeometry))
        }.collect { object -> [id:object.getID().toString(),label:object.getPathClass().toString(),geometry:object.getROI().getGeometry().copy()] }
    }

    private void refreshControls() {
        updateButton.setDisable(closed)
        updateButton.setText("Update preview")
        phaseLabel.setText("2  Fit and inspect predictions")
        phaseDescription.setText("Update fits the graph model, then the selected classifier, from the same reference annotations. Image embeddings are reused.")
    }

    void requestUpdate() {
        if(closed) return
        pending=[revision:++requestedRevision,annotations:annotations(),
                 method:method.selectionModel.selectedIndex==0 ? "rf" : "svm"]
        status.setText(busy ? "The latest annotation changes are queued." : "Preparing the preview…")
        refreshControls();
        startPending()
    }

    private void startPending() {
        if(closed || busy || pending==null) return
        Map request=pending;
        pending=null;
        busy=true;
        refreshControls()
        compute.submit({
            Map result=null;
            Throwable failure=null
            Object fit=null
            try {
                Map model=backend.trainSpatial(request.annotations,classes,graphSettings,{ event ->
                    Platform.runLater({ if(!closed && request.revision==requestedRevision)
                        status.setText("Fitting graph model · "+Math.round(100.0*(event.epoch as int)/(graphSettings.epochs as int))+"%") } as Runnable)
                })
                // A revised request cannot continue into a classifier fit against old labels.
                // Both dependent stages share the immutable geometry snapshot captured above.
                if(!closed && request.revision==requestedRevision) {
                    Platform.runLater({if(!closed && request.revision==requestedRevision)
                        status.text="Fitting "+(request.method=="rf"?"random forest":"support vector machine")+"…"} as Runnable)
                    fit=backend.fitClassifier(model,request.annotations,classes,request.method,nativeSettings)
                    result=[fitted:model,fit:fit,provider:backend.provider(model,fit)]
                }
            } catch(Throwable error) {
                try {fit?.close()}catch(Throwable cleanupError){error.addSuppressed(cleanupError)}
                failure=error
            }
            Map completed=result;
            Throwable error=failure
            Platform.runLater({
                busy=false
                if(closed || request.revision!=requestedRevision) {
                    dispose(completed)
                } else if(error!=null) {
                    diagnostic.setText(error.toString())
                    status.setText("The preview was not updated. Open Technical details for the recorded cause.")
                } else {
                    try {
                        publishPreview(completed)
                        fitted=completed.fitted;
                        completedSpatialFits++;
                        completedClassifierFits++
                        status.setText("Predictions updated. After editing references, select Update preview to refit the graph and classifier together.")
                        diagnostic.clear()
                    } catch(Throwable renderError) {
                        dispose(completed);
                        diagnostic.setText(renderError.toString())
                        status.setText("The preview was not updated. Open Technical details to review the rendering error.")
                    }
                }
                refreshControls();
                startPending()
            } as Runnable)
        } as Runnable)
    }

    private void publishPreview(Map completed) {
        def operation=operations.operation("preview-"+UUID.randomUUID().toString(),completed.provider)
        Map labels=[:]
        for(int i=0;i<classes.size();i++) labels[i]=PathClass.fromString(classes[i])
        labels[255]=PathClass.NULL_CLASS
        def calibration=backend.image.server.pixelCalibration.createScaledInstance(backend.downsample,backend.downsample)
        def classifier=PixelClassifiers.createClassifier(operation,calibration,labels)
        def candidate=operations.display(gui.overlayOptions,backend.image,classifier,false)
        try {
            candidate.setOpacity(opacity.value);
            candidate.setLivePrediction(true)
            retireOverlay()
            viewer.setCustomPixelLayerOverlay(candidate)
            overlay=candidate;
            provider=completed.provider;
            nativeFit=completed.fit
            gui.overlayOptions.setShowPixelClassification(true)
            viewer.forceOverlayUpdate()
        } catch(Throwable error) { candidate.stop();
            throw error }
    }

    private void dispose(Map result) {
        if(result==null) return
        control.submit({
            try { result.provider?.close() } finally { result.fit?.close() }
        } as Runnable)
    }

    private void retireOverlay() {
        if(overlay==null) return
        overlay.stop()
        if(viewer.customPixelLayerOverlay==overlay)viewer.setCustomPixelLayerOverlay(previousOverlay)
        dispose([provider:provider,fit:nativeFit])
        overlay=null;
        provider=null;
        nativeFit=null
    }

    @Override void close() {
        if(closed) return
        closed=true;
        pending=null;
        requestedRevision++;
        drawingControls?.close?.call()
        backend.image.hierarchy.removeListener(annotationListener)
        viewer.imageDataProperty().removeListener(imageListener)
        retireOverlay();
        gui.overlayOptions.setShowPixelClassification(previousPredictionVisibility);
        stage.hide()
        control.submit({ backend.client.cancelActive() } as Runnable)
        compute.submit({
            try { backend.close() } finally { backend.client.close() }
            // Fit completions were queued on JavaFX before this barrier. Let them
            // retire their results before the resource executor is shut down.
            Platform.runLater({
                control.submit({ resourcesClosed=true } as Runnable)
                control.shutdown()
            } as Runnable)
        } as Runnable)
        compute.shutdown()
    }
}
return [open:{QuPathGUI gui,Object backend,List<String> classes,Map graphSettings,Map nativeSettings,Map operations ->
    new MifKronosLivePreview(gui,backend,classes,graphSettings,nativeSettings,operations)}]
