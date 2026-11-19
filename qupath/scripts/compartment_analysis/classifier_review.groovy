// Fixed-model review uses native tile prediction with an independent region filter
// and a Graphics2D clip. It never fits a model or changes its output labels.
import java.nio.file.Path
import java.nio.file.Files
import java.util.concurrent.Executors
import java.awt.Graphics2D
import javafx.application.Platform
import javafx.stage.Stage
import javafx.stage.WindowEvent
import javafx.scene.Scene
import javafx.scene.control.*
import javafx.scene.layout.VBox
import javafx.scene.layout.HBox
import javafx.scene.layout.BorderPane
import javafx.scene.layout.Priority
import javafx.geometry.Insets
import org.locationtech.jts.operation.union.UnaryUnionOp
import org.locationtech.jts.awt.ShapeWriter
import org.locationtech.jts.geom.Envelope
import qupath.lib.gui.viewer.OverlayOptions
import qupath.lib.gui.viewer.RegionFilter
import qupath.lib.gui.viewer.overlays.PathOverlay
import qupath.lib.gui.viewer.overlays.PixelClassificationOverlay
import qupath.lib.images.ImageData
import qupath.lib.regions.ImageRegion
import qupath.lib.regions.RegionRequest
import qupath.lib.objects.hierarchy.events.PathObjectHierarchyListener

final class MifScopedReviewOverlay implements PathOverlay {
    @Delegate(excludes=["paintOverlay"]) final PixelClassificationOverlay nativeOverlay
    final def shape
    final int z
    final int t
    MifScopedReviewOverlay(overlay,geometry,int z,int t) {
        nativeOverlay=overlay;
        shape=new ShapeWriter().toShape(geometry);
        this.z=z;
        this.t=t
    }
    void clip(Graphics2D graphics){graphics.clip(shape)}
    void paintOverlay(Graphics2D graphics,ImageRegion region,double downsample,ImageData image,boolean complete) {
        if(region.z!=z || region.t!=t)return
        Graphics2D copy=(Graphics2D)graphics.create()
        try {clip(copy);
            nativeOverlay.paintOverlay(copy,region,downsample,image,complete)}
        finally {copy.dispose()}
    }
}

final class MifFixedClassifierReview implements AutoCloseable {
    /** Local native presentation, matching the accepted prediction panels. */
    static void stylePanel(javafx.scene.Parent layout) {
        Map loadedFontFamilies=[:]
        ["Regular","SemiBold"].each {weight->
            def stream=org.mif.cells.NativeCellScope.class.getResourceAsStream("/org/mif/fonts/ubuntu-sans/UbuntuSans-${weight}.ttf")
            if(stream!=null)try {
                def font=javafx.scene.text.Font.loadFont(stream,15d)
                if(font!=null)loadedFontFamilies[weight]=font.family
            }finally {stream.close()}
        }
        List preferences=System.getProperty("os.name","").startsWith("Windows") ?
            ["Segoe UI",loadedFontFamilies.Regular ?: "Ubuntu Sans","DejaVu Sans"] :
            [loadedFontFamilies.Regular ?: "Ubuntu Sans","Ubuntu","DejaVu Sans"]
        String family=preferences.find {it==loadedFontFamilies.Regular || javafx.scene.text.Font.families.contains(it)} ?: javafx.scene.text.Font.default.family
        String heading=(family==loadedFontFamilies.Regular && loadedFontFamilies.SemiBold) ?
            loadedFontFamilies.SemiBold : (["${family} SemiBold","${family} Semibold"].find {
                javafx.scene.text.Font.families.contains(it.toString())
            } ?: family)
        layout.style="-fx-font-family:'${family.replace("'","")}';-fx-font-size:15px;"
        String css="""
            .root, .dialog-pane { -fx-background-color:white; -fx-accent:#006b70; -fx-focus-color:#006b70; -fx-faint-focus-color:transparent; }
            .label { -fx-text-fill:#172d35; }
            .mif-title, .mif-section-title, .mif-caption { -fx-font-family:'${heading.replace("'","")}'; -fx-font-weight:600; }
            .mif-title { -fx-font-size:20px; }
            .mif-section-title { -fx-font-size:16px; }
            .mif-caption { -fx-text-fill:#294c56; }
            .mif-help { -fx-text-fill:#486169; -fx-line-spacing:2; }
            .mif-footer, .dialog-pane > .button-bar { -fx-background-color:#f3f5f5; -fx-border-color:#dce4e4 transparent transparent transparent; -fx-border-width:1 0 0 0; }
            .dialog-pane > .header-panel { -fx-background-color:white; }
            .scroll-pane, .scroll-pane > .viewport { -fx-background-color:white; -fx-background-insets:0; -fx-padding:0; }
            .separator .line { -fx-border-color:#dce4e4; -fx-border-width:1 0 0 0; }
            .button, .combo-box { -fx-background-color:white; -fx-background-insets:0; -fx-background-radius:3; -fx-border-color:#bdcccc; -fx-border-radius:3; -fx-border-width:1; -fx-text-fill:#294148; -fx-padding:8 14; }
            .button:hover, .combo-box:hover { -fx-background-color:#eaf2ef; -fx-border-color:#8eaaa5; }
            .button:focused, .combo-box:focused { -fx-border-color:#006b70; -fx-border-width:2; -fx-padding:7 13; }
            .button:pressed { -fx-background-color:#dcece6; }
            .combo-box > .list-cell { -fx-background-color:transparent; -fx-text-fill:#172d35; -fx-padding:0 8 0 0; }
            .combo-box > .arrow-button { -fx-background-color:transparent; -fx-padding:0 2 0 8; }
            .combo-box .arrow { -fx-background-color:#526971; }
            .combo-box-popup .list-view { -fx-background-color:white; -fx-background-insets:0; -fx-border-color:#bdcccc; -fx-padding:3; }
            .combo-box-popup .list-cell { -fx-background-color:white; -fx-text-fill:#172d35; -fx-padding:8 10; }
            .combo-box-popup .list-cell:filled:hover, .combo-box-popup .list-cell:filled:selected { -fx-background-color:#e4f0ee; -fx-text-fill:#00575c; }
            .slider .track { -fx-background-color:#dce4e4; -fx-background-insets:0; }
            .slider .thumb { -fx-background-color:#006b70; -fx-background-insets:0; }
            .progress-indicator { -fx-progress-color:#006b70; }
        """
        layout.stylesheets.add("data:text/css;base64,"+java.util.Base64.encoder.encodeToString(css.getBytes(java.nio.charset.StandardCharsets.UTF_8)))
    }

    final Object gui
    final Map support
    final Object viewer
    final Object image
    final Object executor
    private final boolean ownsExecutor
    final Stage stage=new Stage()
    final Label status=new Label("Loading the fitted model and checking its reference set…")
    private def overlay=null
    private final Object previous
    private final boolean wasReadOnly
    private final boolean wasVisible
    private boolean closed=false
    private boolean boundKronos=false
    private final PathObjectHierarchyListener changed
    private final javafx.beans.value.ChangeListener imageChanged
    private final javafx.event.EventHandler hidden

    static createOverlay(options,image,classifier,List scopes,Map display) {
        if(scopes.isEmpty() || scopes.collect{[it.z,it.t]}.toSet().size()!=1)
            throw new IllegalArgumentException("Review requires current regions from one image plane.")
        def geometry=UnaryUnionOp.union(scopes.collect {it.geometry})
        int z=scopes.first().z as int,t=scopes.first().t as int
        def local=new OverlayOptions(options);
        local.showPixelClassification=true
        local.pixelClassificationRegionFilter={current,request->
            current.is(image) && request.z==z && request.t==t &&
            geometry.intersects(geometry.factory.toGeometry(new Envelope(request.x,request.x+request.width,request.y,request.y+request.height)))
        } as RegionFilter
        def nativeLayer=display.create(local,image,classifier,false)
        new MifScopedReviewOverlay(nativeLayer,geometry,z,t)
    }

    MifFixedClassifierReview(gui,Map support,String name) {
        this.gui=gui;
        this.support=support;
        viewer=gui.viewer;
        image=viewer.imageData
        ownsExecutor=support.reviewExecutor==null;
        executor=support.reviewExecutor ?: Executors.newSingleThreadExecutor()
        def selectedProject=gui.project
        if(image==null || gui.project==null)throw new IllegalArgumentException("Open the selected image before reviewing its fitted classifier.")
        def entry=gui.project.getEntry(image)
        if(entry==null || !gui.project.pixelClassifiers.names.contains(name))throw new IllegalArgumentException("Select a saved classifier from this analysis.")
        if(!(name==~/[A-Za-z0-9_.-]{1,200}/))throw new IllegalArgumentException("The saved classifier name is not supported.")
        previous=viewer.customPixelLayerOverlay;
        wasReadOnly=gui.readOnly;
        wasVisible=gui.overlayOptions.showPixelClassification
        stage.initOwner(gui.stage);
        stage.title="Fitted classifier review"
        Label title=new Label("Fitted classifier predictions");
        title.styleClass.add("mif-title")
        title.wrapText=true;
        title.minHeight=javafx.scene.layout.Region.USE_PREF_SIZE;
        title.tooltip=new Tooltip("Model file: "+name+".json");
        VBox header=new VBox(title);
        header.padding=new Insets(18,20,14,20)
        Label instruction=new Label(support.evaluationReference ?
            "Compare the reference outline with the predicted classes. Adjust overlay opacity to inspect the staining at the same position." :
            "Pan and zoom across the image regions used to develop the classifier. Inspect the predictions and their agreement with the reference annotations.");
        instruction.wrapText=true;
        instruction.styleClass.add("mif-help")
        status.wrapText=true;
        status.maxWidth=Double.MAX_VALUE;
        status.styleClass.add("mif-help");
        status.id="fixed-review-status"
        Slider opacity=new Slider(0,1,0.45);
        opacity.id="fixed-review-opacity"
        Label opacityName=new Label("Overlay opacity");
        opacityName.labelFor=opacity;
        opacityName.styleClass.add("mif-caption");
        opacityName.maxWidth=Double.MAX_VALUE
        Label opacityValue=new Label("45%");
        opacityValue.id="fixed-review-opacity-value"
        HBox opacityHeading=new HBox(8,opacityName,opacityValue);
        HBox.setHgrow(opacityName,Priority.ALWAYS)
        opacity.valueProperty().addListener({o,a,b->
            opacityValue.text=Math.round(b.doubleValue()*100d)+"%"
            if(overlay!=null){overlay.opacity=b.doubleValue();
                viewer.forceOverlayUpdate()}} as javafx.beans.value.ChangeListener)
        Label nextStep=new Label("Close this view and return to the classifier review to record your decision. The saved model remains fixed during inspection.");
        nextStep.wrapText=true;
        nextStep.styleClass.add("mif-help")
        Button closeButton=new Button("Close review");
        closeButton.id="fixed-review-close";
        closeButton.setOnAction({event->close()} as javafx.event.EventHandler)
        Label section=new Label("Inspect predictions");
        section.styleClass.add("mif-section-title")
        VBox content=new VBox(15,instruction,section,opacityHeading,opacity,status,new Separator(),nextStep)
        content.padding=new Insets(12,20,18,20);
        ScrollPane scroll=new ScrollPane(content);
        scroll.fitToWidth=true;
        scroll.hbarPolicy=ScrollPane.ScrollBarPolicy.NEVER
        HBox footer=new HBox(closeButton);
        footer.styleClass.add("mif-footer");
        footer.alignment=javafx.geometry.Pos.CENTER_RIGHT;
        footer.padding=new Insets(10,20,16,20)
        BorderPane layout=new BorderPane(scroll,header,null,footer,null);
        stylePanel(layout)
        stage.scene=new Scene(layout,540,510);
        stage.minWidth=430;
        stage.minHeight=340
        stage.setOnCloseRequest({event->event.consume();
            close()} as javafx.event.EventHandler)
        changed={event->if(!event.isChanging() && !event.isObjectMeasurementEvent())Platform.runLater({
            if(!closed){close();
                    qupath.fx.dialogs.Dialogs.showInfoNotification("Reference set changed","Return to the classifier step to record the revised references before fitting again.")}
        } as Runnable)} as PathObjectHierarchyListener
        imageChanged={o,a,b->if(b!=image)close()} as javafx.beans.value.ChangeListener
        hidden={event->close()} as javafx.event.EventHandler
        image.hierarchy.addListener(changed);
        viewer.imageDataProperty().addListener(imageChanged)
        gui.stage.addEventHandler(WindowEvent.WINDOW_HIDDEN,hidden)
        gui.setReadOnly(true);
        stage.show()
        Path model=selectedProject.path.parent.resolve("classifiers/pixel_classifiers/"+name+".json")
        executor.submit({
            def classifier=null;
            Map policy=null;
            List scopes=null;
            Throwable failure=null;
            boolean kronos=false
            try {
                boolean foldReview=support.evaluationReference?.split=="cross_validation"
                Map snapshot=foldReview ? support.reviewPolicy.foldSnapshot(model,support.evaluationReference) :
                    support.reviewPolicy.snapshot.call(model)
                policy=snapshot.scope
                scopes=support.evaluationReference && !foldReview ?
                    support.reviewPolicy.evaluationScope(policy,image,entry.ID.toString(),support.evaluationReference) :
                    support.reviewPolicy.validate(policy,image,entry.ID.toString())
                if(foldReview)scopes=scopes.collect {it+[split:"validation"]}
                classifier=support.reviewPolicy.loadClassifier(snapshot)
                kronos=snapshot.kronos
            }catch(Throwable error){failure=error}
            def loaded=classifier;
            def locations=scopes;
            def error=failure;
            boolean scoped=kronos
            Platform.runLater({
                if(closed)return
                try {
                    if(error!=null)throw error
                    // No editable references have been exposed during loading.
                    if(scoped)support.resources.bind(image,locations.collect {row->[id:row.id,geometry:row.geometry,revision:row.revision,split:row.split]})
                    boundKronos=scoped
                    overlay=createOverlay(gui.overlayOptions,image,loaded,locations,support.displayPolicy)
                    overlay.opacity=opacity.value;
                    overlay.livePrediction=true
                    viewer.customPixelLayerOverlay=overlay;
                    gui.overlayOptions.showPixelClassification=true
                    status.text="Fitted model ready. Predictions appear as the requested image regions load."
                    viewer.forceOverlayUpdate()
                }catch(Throwable problem){close();
                    qupath.fx.dialogs.Dialogs.showErrorMessage("Classifier review",problem.message?:problem.class.simpleName)}
            } as Runnable)
        } as Runnable)
    }
    void close() {
        if(closed)return
        closed=true
        image.hierarchy.removeListener(changed);
        viewer.imageDataProperty().removeListener(imageChanged)
        gui.stage.removeEventHandler(WindowEvent.WINDOW_HIDDEN,hidden)
        def retired=overlay;
        overlay=null;
        retired?.stop()
        if(retired!=null && viewer.customPixelLayerOverlay?.is(retired))viewer.customPixelLayerOverlay=previous
        gui.overlayOptions.showPixelClassification=wasVisible
        gui.setReadOnly(wasReadOnly);
        stage.hide()
        if(boundKronos)executor.submit({support.resources.closeImage(image)} as Runnable)
        if(ownsExecutor)executor.shutdown()
    }
}
return [createOverlay:MifFixedClassifierReview.&createOverlay,
        open:{gui,Map support,String name->new MifFixedClassifierReview(gui,support,name)}]
