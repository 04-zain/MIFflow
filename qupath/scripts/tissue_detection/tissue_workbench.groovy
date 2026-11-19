/**
 * Opens the tissue-detector editor and connects its settings, annotations, live overlay and saved result.
 *
 * Threshold detection selects its channel and stored resolution here. RF/SVM
 * receives those inputs and its physical feature recipe from the main setup.
 * We adjust a threshold or label Tissue/Background regions for RF/SVM. A completed change waits briefly before
 * requesting a new fit. Fitting runs on a separate worker, while the JavaFX thread
 * keeps drawing and receiving edits. Only the result matching the latest settings
 * is installed as the active preview.
 *
 * Saving writes the current classifier into the detector draft and records the
 * chosen channel and native level for the application to import. Updating a draft
 * name retains its preceding detector file in that draft's history. The main
 * analysis imports the selected result through its own save operation afterward.
 */

import java.nio.file.Path
import java.nio.file.Files
import java.util.concurrent.Executors
import java.util.concurrent.ThreadFactory
import javafx.application.Platform
import javafx.animation.PauseTransition
import javafx.util.Duration
import javafx.stage.Stage
import javafx.scene.Scene
import javafx.scene.control.*
import javafx.scene.layout.*
import javafx.beans.value.ChangeListener
import qupath.lib.gui.viewer.overlays.PixelClassificationOverlay
import qupath.lib.objects.hierarchy.events.PathObjectHierarchyListener
import qupath.lib.images.ImageData
import qupath.lib.io.PathIO
import qupath.opencv.ml.pixel.PixelClassifiers
import qupath.lib.gui.images.stores.ColorModelRenderer
import java.awt.image.IndexColorModel
import java.security.MessageDigest
import java.nio.file.StandardCopyOption
import org.mif.classification.PixelFeatureOps

/**
 * Builds the native QuPath controls for creating and reviewing a binary tissue detector.
 */
class TissueWorkbench {
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

    private static final long MAX_CLASSIFIER_BYTES = 64L * 1024 * 1024
    /**
     * Returns the method-specific instructions shown below the detector settings.
     *
     * Args:
     *     method: Selected threshold, RF or SVM route.
     *
     * Returns:
     *     Instructions identifying the required channel or Tissue/Background annotations.
     *
     * Raises:
     *     IllegalArgumentException: The method name is unsupported.
     */
    static String guidance(String method) {
        if(method=="threshold")return "Separate tissue using the selected channel and smoothing scale. The overlay updates when the threshold or smoothing changes."
        if(method=="rf")return "Random forest combines decision trees trained on the annotated Tissue and Background examples."
        if(method=="svm")return "SVM fits a boundary between the annotated Tissue and Background examples using their selected image measurements."
        throw new IllegalArgumentException("Select a tissue detection method.")
    }
    /**
     * Opens a tissue-detector editing session for the selected project image.
     *
     * We populate the controls from the module defaults carried by request, then
     * calculate the initial native resolution through algorithm.startingLevel().
     * Setting changes and completed reference edits increment a revision number.
     * Each background fit captures that number and a separate annotation snapshot;
     * its result is used only while both the image and revision still match.
     *
     * The preview uses QuPath's native pixel-classification image server. Class zero
     * is transparent and class one is bright cyan. Its own region filter permits
     * prediction throughout the image, so an annotation-only filter left in another
     * QuPath window cannot restrict this tissue preview.
     *
     * The save action serializes the active classifier, checks its channel and native
     * resolution, and writes the result into the editing draft. Its return record
     * contains the session ID, model checksum, channel and level selected for import.
     * Closing removes the annotation listeners and preview and stops queued fitting.
     *
     * Args:
     *     gui: Current QuPath application and selected image viewer.
     *     algorithm: Tissue fitting functions: snapshot, fit and startingLevel, with
     *         the expected Tissue and Background class objects.
     *     validation: Native detector checker used before returning a saved model.
     *     request: Requested method and module defaults for this editing session.
     *     annotationTools: Optional controls selecting QuPath's brush, polygon,
     *         rectangle and ellipse tools for the current annotations.
     *
     * Returns:
     *     Map containing the editing stage, session state, close action and explicit
     *     fit action. The application keeps this session until its window closes.
     *
     * Raises:
     *     IllegalArgumentException: No matching project image is open or the module
     *         settings are absent. Field and save errors are shown in the editor.
     */
    static Map open(
        gui,
        Map algorithm,
        Closure validation,
        Map request,
        Map annotationTools,
        Map featureWorkbench
    ) {
        if(!(request.defaults instanceof Map))
            throw new IllegalArgumentException("Open the tissue detector through the pipeline to load its settings.")
        Map defaults=new LinkedHashMap(request.defaults)
        def viewer=gui.viewer,data=viewer.imageData
        if(data==null)throw new IllegalArgumentException("Open the mIF image before creating a tissue detector.")
        def project=gui.project,entry=project?.getEntry(data)
        if(entry==null)throw new IllegalArgumentException("Open the selected project image for tissue detection.")
        boolean prepared=request.method in ["rf","svm"]
        Map setup=request.setup
        double[] down=data.server.preferredDownsamples
        if(prepared) {
            if(!(setup instanceof Map) || setup.keySet()!=(["channel_index","resolution_index","image_features"] as Set))
                throw new IllegalArgumentException("Choose the channel, resolution and image features in Tissue detection before opening QuPath.")
            [channel_index:data.server.nChannels(),resolution_index:down.length].each {key,limit->
                def value=setup[key]
                if(!(value instanceof Number) || !Double.isFinite(value.doubleValue()) ||
                    value.doubleValue()!=Math.rint(value.doubleValue()) || value.doubleValue()<0d || value.doubleValue()>=limit)
                    throw new IllegalArgumentException("The prepared tissue inputs do not match this image. Reopen Tissue detection setup.")
            }
            Map recipe=setup.image_features
            if(!(recipe instanceof Map) || recipe.keySet()!=(["include_intensity","scales"] as Set) ||
                !(recipe.include_intensity instanceof Boolean) || !(recipe.scales instanceof List))
                throw new IllegalArgumentException("Select image features in Tissue detection setup.")
            double factor=down[(setup.resolution_index as Number).intValue()]
            PixelFeatureOps.create([(setup.channel_index as Number).intValue()] as int[],recipe.include_intensity,
                PixelFeatureOps.scales(recipe.scales),factor*data.server.pixelCalibration.pixelWidthMicrons,
                factor*data.server.pixelCalibration.pixelHeightMicrons)
        } else if(request.method!="threshold" || setup!=null) {
            throw new IllegalArgumentException("Open the selected tissue detector from Tissue detection setup.")
        }
        Map featureRecipe=prepared ? [include_intensity:setup.image_features.include_intensity,
            scales:setup.image_features.scales.collect {row->[sigma_um:row.sigma_um,filters:new ArrayList(row.filters)]}] : null
        Stage stage=new Stage();
        stage.initOwner(gui.stage);
        stage.title="Tissue detection"
        Label heading=new Label("Create a tissue detector");
        heading.styleClass.add("mif-title")
        Label introduction=new Label(prepared ?
            "Train from labelled regions, inspect the overlay, then save the detector." :
            "Adjust the threshold, inspect the tissue overlay, then save the detector.");
        introduction.wrapText=true;introduction.styleClass.add("mif-help")
        VBox header=new VBox(5d,heading)
        if(!prepared)header.children.add(introduction)
        header.padding=new javafx.geometry.Insets(20,22,18,22)
        VBox body=new VBox(18d);
        body.padding=new javafx.geometry.Insets(0,22,20,22)
        Label help=new Label();help.wrapText=true;help.styleClass.add("mif-help")
        Label status=new Label(prepared?"Preview not fitted. Label both classes, then update the preview.":"Preview not updated. Adjust the threshold, then update the preview.");
        status.id="tissue-status";status.wrapText=true;status.styleClass.add("mif-status")
        status.minHeight=Region.USE_PREF_SIZE
        ComboBox<String> method=new ComboBox<>();
        List methodKeys=prepared ? ["rf","svm"] : ["threshold"]
        Map methodTitles=[threshold:"Pixel threshold",rf:"Random forest",svm:"Support vector machine"]
        method.items.addAll(methodKeys.collect {methodTitles[it]})
        method.selectionModel.select(methodKeys.indexOf(request.method));
        Closure methodKey={->methodKeys[method.selectionModel.selectedIndex]}
        method.id="tissue-method";method.maxWidth=Double.MAX_VALUE
        ComboBox<String> channels=new ComboBox<>();
        data.server.metadata.channels.eachWithIndex {channel,index -> channels.items.add((index+1)+". "+channel.name)}
        channels.selectionModel.select(((prepared?setup.channel_index:defaults.channel_index) as Number).intValue());
        channels.id="tissue-channel"
        ComboBox<String> levels=new ComboBox<>();
        down.each {value -> levels.items.add(String.format("%.3g × %.3g µm per pixel",value*data.server.pixelCalibration.pixelWidthMicrons,value*data.server.pixelCalibration.pixelHeightMicrons))}
        levels.selectionModel.select(prepared ? (setup.resolution_index as Number).intValue() : algorithm.startingLevel(down.toList(),data.server.pixelCalibration.pixelWidthMicrons,data.server.pixelCalibration.pixelHeightMicrons,(defaults.target_pixel_size_um as Number).doubleValue()));
        levels.id="tissue-resolution"
        TextField threshold=new TextField(defaults.threshold.toString());
        threshold.id="tissue-threshold"
        TextField smoothing=new TextField(defaults.smoothing_sigma_px.toString());
        smoothing.id="tissue-smoothing"
        Label smoothingHelp=new Label();
        smoothingHelp.wrapText=true;smoothingHelp.styleClass.add("mif-help")
        smoothingHelp.id="tissue-smoothing-help"
        TextField name=new TextField(defaults.detector_name.toString());
        name.id="tissue-detector-name"
        Closure caption={String text,javafx.scene.Node input->
            Label label=new Label(text);label.wrapText=true;label.labelFor=input
            label.styleClass.add("mif-caption");label
        }
        Closure form={->
            GridPane grid=new GridPane();grid.hgap=14d;grid.vgap=10d
            ColumnConstraints labels=new ColumnConstraints(130d,180d,220d)
            ColumnConstraints inputs=new ColumnConstraints(140d,380d,Double.MAX_VALUE);inputs.hgrow=Priority.ALWAYS
            grid.columnConstraints.addAll(labels,inputs);grid
        }
        GridPane sourceFields=form()
        sourceFields.add(caption("Detection channel",channels),0,0);sourceFields.add(channels,1,0)
        sourceFields.add(caption("Image resolution",levels),0,1);sourceFields.add(levels,1,1)
        [channels,levels,threshold,smoothing].each {it.maxWidth=Double.MAX_VALUE}
        Label sourceTitle=new Label("Source image");sourceTitle.styleClass.add("mif-section-title")
        Label preparedSource=new Label("Source: ${channels.value} · ${levels.value}");
        preparedSource.wrapText=true;preparedSource.styleClass.add("mif-help")
        preparedSource.minWidth=0d;preparedSource.maxWidth=Double.MAX_VALUE
        preparedSource.minHeight=Region.USE_PREF_SIZE
        preparedSource.id="tissue-prepared-source"
        VBox sourceControls=new VBox(8d,sourceTitle,sourceFields)
        if(prepared)header.children.add(preparedSource)
        // Use QuPath's ordinary class list for both existing and new reference regions.
        [algorithm.tissue,algorithm.background].each {pathClass->
            if(!gui.availablePathClasses.contains(pathClass))gui.availablePathClasses.add(pathClass)
        }
        Map drawingControls=annotationTools?.create?.call(gui,stage)
        Label referenceTitle=new Label("1  Draw and label references");referenceTitle.styleClass.add("mif-section-title")
        VBox annotationControls=new VBox(10d,referenceTitle)
        if(drawingControls!=null) {
            drawingControls.node.lookupAll('.label').each {it.styleClass.add(it.id=='annotation-drawing-help'?'mif-help':'mif-caption')}
            annotationControls.children.add(drawingControls.node)
        }
        Label labelsTitle=new Label("Class labels");labelsTitle.styleClass.add("mif-caption")
        labelsTitle.minWidth=javafx.scene.layout.Region.USE_PREF_SIZE
        FlowPane classLegend=new FlowPane(8d,8d);classLegend.minWidth=0d;classLegend.id="tissue-class-legend"
        [algorithm.tissue,algorithm.background].each {pathClass->
            Label chip=new Label(pathClass.toString());chip.styleClass.add("mif-class-chip")
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
        HBox legendRow=new HBox(12d,labelsTitle,classLegend);legendRow.alignment=javafx.geometry.Pos.TOP_LEFT
        HBox.setMargin(labelsTitle,new javafx.geometry.Insets(7d,0d,0d,0d))
        HBox.setHgrow(classLegend,Priority.ALWAYS)
        VBox labels=new VBox(8d,legendRow,help)
        labels.id="tissue-class-labels";labels.styleClass.add("mif-class-labels")
        annotationControls.children.add(labels)
        Map parameterNames=[max_trees:"Number of trees",max_depth:"Maximum tree depth",
            min_sample_count:"Minimum samples per node",active_var_count:"Features per split (0 = automatic)",
            c:"SVM cost C",gamma:"RBF gamma",max_iterations:"Maximum iterations",termination_epsilon:"Convergence tolerance"]
        Map parameterMethods=[rf:["max_trees","max_depth","min_sample_count","active_var_count"],
            svm:["c","gamma","max_iterations","termination_epsilon"]]
        Set integerParameters=["max_trees","max_depth","min_sample_count","active_var_count","max_iterations"] as Set
        Map parameterFields=[:],parameterLabels=[:]
        GridPane fittingFields=new GridPane();fittingFields.hgap=14d;fittingFields.vgap=10d
        ColumnConstraints parameterCaption=new ColumnConstraints();parameterCaption.hgrow=Priority.ALWAYS
        ColumnConstraints parameterInput=new ColumnConstraints(190d,220d,240d)
        fittingFields.columnConstraints.addAll(parameterCaption,parameterInput)
        parameterNames.eachWithIndex {key,title,index->
            String initialValue=defaults[key].toString()
            if(integerParameters.contains(key) && defaults[key] instanceof Number) {
                // JSON requests may decode whole-number defaults as doubles.
                initialValue=new BigDecimal(initialValue).stripTrailingZeros().toPlainString()
            }
            TextField field=new TextField(initialValue);field.id="tissue-fit-"+key
            Label label=caption(title,field)
            parameterFields[key]=field;parameterLabels[key]=label
            fittingFields.add(label,0,index);fittingFields.add(field,1,index)
        }
        GridPane methodFields=form()
        methodFields.add(caption("Classifier",method),0,0);methodFields.add(method,1,0)
        Label fittingHelp=new Label();fittingHelp.wrapText=true;fittingHelp.styleClass.add("mif-help")
        TitledPane fittingDetails=new TitledPane("Classifier settings",new VBox(10d,fittingHelp,fittingFields))
        fittingDetails.id="tissue-classifier-settings";fittingDetails.expanded=false
        VBox fittingControls=new VBox(10d,methodFields,fittingDetails)
        GridPane thresholdFields=form()
        Label thresholdLabel=caption("Intensity threshold",threshold);thresholdLabel.id="tissue-threshold-label"
        Label smoothingLabel=caption("Gaussian σ (pixels)",smoothing);smoothingLabel.id="tissue-smoothing-label"
        thresholdFields.add(thresholdLabel,0,0);thresholdFields.add(threshold,1,0)
        thresholdFields.add(smoothingLabel,0,1);thresholdFields.add(smoothing,1,1)
        Label thresholdTitle=new Label("Threshold and smoothing");thresholdTitle.styleClass.add("mif-section-title")
        VBox thresholdControls=new VBox(10d,thresholdTitle,thresholdFields,smoothingHelp)
        thresholdControls.styleClass.add("mif-section")
        CheckBox live=new CheckBox("Update preview after changes");
        live.selected=defaults.auto_update as boolean;
        live.id="tissue-live"
        Button update=new Button("Update preview"),save=new Button("Save detector and close")
        update.id="tissue-update-preview";update.styleClass.add("mif-primary")
        update.minWidth=javafx.scene.layout.Region.USE_PREF_SIZE
        save.id="tissue-save";save.styleClass.add("mif-primary")
        save.disable=true
        Slider opacity=new Slider(0d,1d,(defaults.preview_opacity as Number).doubleValue());
        opacity.id="tissue-preview-opacity"
        opacity.blockIncrement=0.05d
        Label opacityValue=new Label(String.format(Locale.ROOT,"%.0f%%",opacity.value*100d))
        opacityValue.id="tissue-preview-opacity-value"
        opacityValue.minWidth=40d
        Label opacityHelp=caption("Overlay opacity",opacity)
        HBox display=new HBox(10d,opacityHelp,opacity,opacityValue);
        display.alignment=javafx.geometry.Pos.CENTER_LEFT
        HBox.setHgrow(opacity,Priority.ALWAYS)
        Label previewTitle=new Label(prepared?"2  Fit and inspect predictions":"Preview");previewTitle.styleClass.add("mif-section-title")
        previewTitle.wrapText=true;previewTitle.minWidth=0d;previewTitle.maxWidth=Double.MAX_VALUE
        HBox previewHeading=new HBox(12d,previewTitle,update)
        previewHeading.id="tissue-preview-heading";previewHeading.alignment=javafx.geometry.Pos.CENTER_LEFT
        HBox.setHgrow(previewTitle,Priority.ALWAYS)
        VBox previewControls=new VBox(12d,previewHeading,status,fittingControls,display,live)
        previewControls.styleClass.add("mif-section")
        if(!prepared)body.children.add(sourceControls)
        body.children.addAll(annotationControls,thresholdControls,previewControls)
        Closure resizeToContent={->}

        // Keep one active preview model. The revision number identifies which controls
        // and annotations it was fitted from, including edits made while a fit is running.
        Map state=[closed:false,busy:false,revision:0,classifier:null,overlay:null,fitCount:0]
        // QuPath draws indexed prediction tiles directly, bypassing its RGB
        // renderer. Bind the palette to the display server, not only the overlay.
        def previewPalette=new IndexColorModel(8,2,[0,0] as byte[],[0,220] as byte[],[0,255] as byte[],[0,255] as byte[])
        opacity.valueProperty().addListener({obs,old,value ->
            opacityValue.text=String.format(Locale.ROOT,"%.0f%%",value.doubleValue()*100d)
            if(state.overlay!=null){state.overlay.setOpacity(value.doubleValue());
                viewer.repaint()}
        } as ChangeListener)
        def worker=Executors.newSingleThreadExecutor({Runnable task -> Thread thread=new Thread(task,"tissue-detector-preview");
            thread.daemon=true;
            thread} as ThreadFactory)
        PauseTransition debounce=new PauseTransition(Duration.millis((defaults.update_delay_ms as Number).doubleValue()))

        // After an edit, disable saving the preceding fit. Restarting the short delay
        // combines closely spaced typing or drawing changes into one update request.
        Closure invalidate={ -> state.revision=(state.revision as long)+1;
            save.disable=true
            if(live.selected && !state.closed){debounce.playFromStart()}
        }

        // Combine the loaded module parameters with the visible channel, resolution
        // and method controls. Threshold-only fields are read only for that method.
        Closure options={ ->
            String selectedMethod=methodKey()
            Map entered=[:]
            (parameterMethods[selectedMethod] ?: []).each {key->
                try {entered[key]=integerParameters.contains(key)?Integer.parseInt(parameterFields[key].text.trim()):Double.parseDouble(parameterFields[key].text.trim())}
                catch(NumberFormatException error){throw new IllegalArgumentException("Enter "+(integerParameters.contains(key)?"a whole number":"a number")+" for "+parameterNames[key]+".")}
            }
            new LinkedHashMap(defaults) + entered + [
                image_features:featureRecipe,
                method:methodKey(),
                channel:channels.selectionModel.selectedIndex,
                downsample:down[levels.selectionModel.selectedIndex],
                threshold:methodKey()=="threshold" ? Double.parseDouble(threshold.text.trim()) : 0d,
                smoothing_sigma_px:methodKey()=="threshold" ? Double.parseDouble(smoothing.text.trim()) : 0d
            ]
        }

        // Copy the current annotations on the interface thread before fitting. The
        // worker then reads features and fits independently of further viewer edits.
        Closure fit={ ->
            if(state.closed || state.busy || viewer.imageData!=data)return
            try {
                Map settings=options();
                def copied=algorithm.snapshot(data,settings);
                long revision=state.revision
                state.busy=true;
                state.fitCount++;
                save.disable=true;
                update.disable=true
                state.overlay?.setLivePrediction(false)
                status.text=settings.method=="threshold"?"Updating the threshold preview…":"Training the classifier from labelled regions…"
                worker.submit({
                    Map result=null;
                    Throwable failure=null
                    try {result=algorithm.fit(copied,settings)}catch(Throwable error){failure=error}
                    Platform.runLater({
                        state.busy=false;
                        update.disable=false
                        if(state.closed || viewer.imageData!=data)return

                        // An edit made during fitting belongs to a newer revision. Keep saving disabled
                        // and request a new fit rather than installing the earlier model as current.
                        if(state.revision!=revision){if(live.selected)debounce.playFromStart();
                            return}
                        if(failure!=null){status.text=failure.message ?: failure.class.simpleName;
                            return}
                        if(state.overlay!=null){viewer.customOverlayLayers.remove(state.overlay);
                            state.overlay.stop();
                            state.overlay=null}
                        state.classifier=result.classifier;
                        state.settings=settings
                        def previewServer=new qupath.lib.classifiers.pixel.PixelClassificationImageServer(
                            data,result.classifier,"mif-tissue-preview-"+UUID.randomUUID(),previewPalette)
                        def previewOptions=new qupath.lib.gui.viewer.OverlayOptions(viewer.overlayOptions)
                        previewOptions.pixelClassificationRegionFilter=qupath.lib.gui.viewer.RegionFilter.StandardRegionFilters.EVERYWHERE
                        previewOptions.showPixelClassification=true
                        state.overlay=PixelClassificationOverlay.create(previewOptions,[(data):previewServer],new ColorModelRenderer(previewPalette))
                        state.overlay.setOpacity(opacity.value);
                        state.overlay.setLivePrediction(true)
                        viewer.overlayOptions.showPixelClassification=true
                        viewer.customOverlayLayers.add(state.overlay);
                        viewer.repaint()
                        save.disable=false
                        status.text=settings.method=="threshold"?"Threshold preview updated.":
                            "Prediction updated from "+result.samples+" labelled pixels."
                    } as Runnable)
                } as Runnable)
            }catch(Exception error){status.text=error.message;
                save.disable=true}
        }
        debounce.setOnFinished({event -> fit()})
        update.setOnAction({event -> debounce.stop();
            fit()})
        ChangeListener settingsChanged={obs,old,value -> invalidate()} as ChangeListener
        [method.valueProperty(),channels.valueProperty(),levels.valueProperty(),threshold.textProperty(),smoothing.textProperty()].each {it.addListener(settingsChanged)}
        parameterFields.values().each {it.textProperty().addListener(settingsChanged)}
        live.selectedProperty().addListener({obs,old,value -> if(value)invalidate();
            else debounce.stop()} as ChangeListener)
        Closure modeChanged={ ->
            boolean isThreshold=methodKey()=="threshold"
            help.text="In QuPath’s Annotations tab, choose a class and use Set selected to label the selected region."
            update.text="Update preview"
            method.tooltip=new Tooltip(guidance(methodKey()))
            fittingHelp.text=guidance(methodKey())
            annotationControls.visible=!isThreshold;annotationControls.managed=!isThreshold
            fittingControls.visible=!isThreshold;fittingControls.managed=!isThreshold
            thresholdControls.visible=isThreshold;thresholdControls.managed=isThreshold
            List active=parameterMethods[methodKey()] ?: []
            parameterFields.each {key,field->
                boolean show=active.contains(key);field.visible=show;field.managed=show
                parameterLabels[key].visible=show;parameterLabels[key].managed=show
                if(show){GridPane.setRowIndex(field,active.indexOf(key));GridPane.setRowIndex(parameterLabels[key],active.indexOf(key))}
            }
            if(stage.showing)resizeToContent()
        }
        Closure smoothingScale={ ->
            String scaleText=""
            try {
                double sigma=Double.parseDouble(smoothing.text.trim()),d=down[levels.selectionModel.selectedIndex]
                if(Double.isFinite(sigma) && sigma>=0d && sigma<=20d)
                    scaleText=String.format("\nGaussian σ: %.3g µm (X), %.3g µm (Y).",sigma*d*data.server.pixelCalibration.pixelWidthMicrons,sigma*d*data.server.pixelCalibration.pixelHeightMicrons)
            }catch(Exception ignored){}
            smoothingHelp.text="Smooth the selected channel before applying its threshold."+scaleText
        }
        smoothing.textProperty().addListener({obs,old,value -> smoothingScale()} as ChangeListener)
        levels.valueProperty().addListener({obs,old,value -> smoothingScale()} as ChangeListener);
        smoothingScale()
        method.valueProperty().addListener({obs,old,value -> modeChanged()} as ChangeListener)
        modeChanged()

        // Request training after completed annotation edits. Measurement notifications
        // and ordinary zoom/opacity updates do not change the labelled training data.
        PathObjectHierarchyListener hierarchyChanged={event ->
            if(event.isChanging() || event.isObjectMeasurementEvent())return
            boolean annotationsChanged=event.changedObjects.any {it.isAnnotation()} || (event.isStructureChangeEvent() && event.changedObjects.isEmpty())
            if(annotationsChanged)Platform.runLater({if(!state.closed && methodKey()!="threshold")invalidate()} as Runnable)
        } as PathObjectHierarchyListener
        data.hierarchy.addListener(hierarchyChanged)
        save.setOnAction({event ->
            if(state.busy || state.classifier==null || save.disable)return
            try {
                String selected=name.text.trim().replaceFirst(/\.json$/,"")
                if(!(selected==~/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}/))throw new IllegalArgumentException("Use letters, numbers, hyphens or underscores for the detector name.")

                // When updating a saved draft name, retain its preceding file under that file's
                // checksum before replacing it. The draft list still contains one current detector.
                if(project.pixelClassifiers.contains(selected)) {
                    if(!qupath.fx.dialogs.Dialogs.showYesNoDialog("Update detector?",
                        "Replace the draft saved as "+selected+" with this detector? Its previous version will be retained in detector history."))return
                    Path current=project.path.parent.resolve("classifiers/pixel_classifiers/"+selected+".json")
                    if(Files.isSymbolicLink(current)||!Files.isRegularFile(current)||Files.size(current)>MAX_CLASSIFIER_BYTES)
                        throw new IllegalArgumentException("The previous detector cannot be read safely. Save this detector under a different name.")
                    byte[] prior=Files.readAllBytes(current)
                    String oldHash=MessageDigest.getInstance("SHA-256").digest(prior).encodeHex().toString()
                    Path history=project.path.parent.resolve("detector-history/"+selected)
                    Files.createDirectories(history)
                    Path previous=history.resolve(oldHash+".json")
                    if(!Files.exists(previous))Files.write(previous,prior,java.nio.file.StandardOpenOption.CREATE_NEW)
                    else if(Files.isSymbolicLink(previous)||!Arrays.equals(prior,Files.readAllBytes(previous)))
                        throw new IllegalArgumentException("The previous detector history has changed. Save this detector under a different name.")
                }
                Path temporary=Files.createTempFile("tissue-detector-",".json")
                try {
                    PixelClassifiers.writeClassifier(state.classifier,temporary)
                    if(Files.size(temporary)>MAX_CLASSIFIER_BYTES)throw new IllegalArgumentException("The detector is too large. Use fewer training regions or a smaller forest.")
                    def document=new com.google.gson.GsonBuilder().disableHtmlEscaping().serializeNulls().setObjectToNumberStrategy(com.google.gson.ToNumberPolicy.BIG_DECIMAL).create().fromJson(Files.readString(temporary),Map.class)
                    def res=state.classifier.metadata.inputResolution
                    validation(state.classifier,document,data,state.settings.channel,
                        [[native_resolution_index:levels.selectionModel.selectedIndex,
                          x_um_per_px:res.pixelWidthMicrons,y_um_per_px:res.pixelHeightMicrons]])
                    project.pixelClassifiers.put(selected,state.classifier)
                    entry.saveImageData(data)

                    // Associate the returned model with this exact editing session. The application
                    // reads this small result after QuPath closes to select the new detector automatically.
                    Path sessionFile=project.path.parent.resolve("tissue-session.json")
                    if(Files.isRegularFile(sessionFile) && !Files.isSymbolicLink(sessionFile) && Files.size(sessionFile)<=65536) {
                        Map session=new com.google.gson.GsonBuilder().disableHtmlEscaping().serializeNulls().setObjectToNumberStrategy(com.google.gson.ToNumberPolicy.BIG_DECIMAL).create().fromJson(Files.readString(sessionFile),Map.class)
                        if(session.schema!="tissue-authoring-session/v1"||!(session.session_id==~/[0-9a-f]{32}/))
                            throw new IllegalArgumentException("Reopen the detector draft from the pipeline before returning this save.")
                        Path saved=project.path.parent.resolve("classifiers/pixel_classifiers/"+selected+".json")
                        Map result=[schema:"tissue-authoring-result/v1",session_id:session.session_id,classifier_name:selected,
                            classifier_sha256:MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(saved)).encodeHex().toString(),
                            channel_index:state.settings.channel,resolution_index:levels.selectionModel.selectedIndex]
                        Path staged=Files.createTempFile(project.path.parent,".tissue-result-",".json")
                        Files.writeString(staged,new com.google.gson.GsonBuilder().disableHtmlEscaping().serializeNulls().setObjectToNumberStrategy(com.google.gson.ToNumberPolicy.BIG_DECIMAL).create().toJson(result))
                        Files.move(staged,project.path.parent.resolve("tissue-result.json"),StandardCopyOption.REPLACE_EXISTING,StandardCopyOption.ATOMIC_MOVE)
                    }
                    state.savedName=selected;
                    stage.close()
                }finally{Files.deleteIfExists(temporary)}
            }catch(Exception error){status.text=error.message ?: error.class.simpleName}
        })

        // Remove this window's listeners and overlay before stopping queued work.
        // Future viewer edits must not restart a detector session that has been closed.
        Map referenceClasses=null
        Closure close={ ->
            if(state.closed)return
            state.closed=true;
            debounce.stop();
            data.hierarchy.removeListener(hierarchyChanged)
            drawingControls?.close?.call()
            referenceClasses?.close?.call()
            if(state.overlay!=null){viewer.customOverlayLayers.remove(state.overlay);
                state.overlay.stop();
                state.overlay=null}
            worker.shutdownNow()
        }
        stage.setOnHidden({event -> close()})
        ChangeListener imageChanged={obs,old,value -> if(value!=data)stage.close()} as ChangeListener
        viewer.imageDataProperty().addListener(imageChanged)
        stage.addEventHandler(javafx.stage.WindowEvent.WINDOW_HIDDEN,{event -> viewer.imageDataProperty().removeListener(imageChanged)} as javafx.event.EventHandler)
        ScrollPane scroll=new ScrollPane(body);
        scroll.fitToWidth=true;scroll.hbarPolicy=ScrollPane.ScrollBarPolicy.NEVER
        // The name and final save action stay reachable while
        // the source, fit and preview controls scroll on smaller displays.
        Label nameLabel=caption("Detector name",name);nameLabel.id="tissue-name-label"
        HBox nameRow=new HBox(14d,nameLabel,name);nameRow.alignment=javafx.geometry.Pos.CENTER_LEFT
        HBox.setHgrow(name,Priority.ALWAYS);name.maxWidth=Double.MAX_VALUE
        Button closeButton=new Button("Close");closeButton.id="tissue-close"
        closeButton.setOnAction({event->stage.close()} as javafx.event.EventHandler)
        HBox actions=new HBox(10d,closeButton,save);actions.alignment=javafx.geometry.Pos.CENTER_RIGHT
        VBox footer=new VBox(10d,nameRow,actions);
        footer.padding=new javafx.geometry.Insets(14,22,14,22);footer.styleClass.add("mif-footer")
        BorderPane layout=new BorderPane(scroll,header,null,footer,null)
        stage.minWidth=540d
        stage.minHeight=440d
        stage.scene=new Scene(layout,660d,-1d)
        stylePanel(layout)
        resizeToContent={->
            layout.applyCss()
            def screens=javafx.stage.Screen.getScreensForRectangle(gui.stage.x,gui.stage.y,gui.stage.width,gui.stage.height)
            def bounds=(screens.isEmpty()?javafx.stage.Screen.primary:screens.first()).visualBounds
            double availableHeight=Math.max(1d,bounds.height-40d)
            stage.minHeight=Math.min(stage.minHeight,availableHeight)
            double width=stage.scene.width>0d?stage.scene.width:660d
            scroll.prefViewportHeight=Math.min(body.prefHeight(width-2d),
                Math.max(1d,availableHeight-header.prefHeight(width)-footer.prefHeight(width)-40d))
            stage.sizeToScene()
            stage.height=Math.min(stage.height,availableHeight)
            if(stage.showing)stage.y=Math.max(bounds.minY,Math.min(stage.y,bounds.maxY-stage.height))
        }
        resizeToContent()
        referenceClasses=annotationTools?.scopeReferenceClasses?.call(
            [algorithm.tissue.toString(),algorithm.background.toString()])
        stage.show()
        invalidate()
        return [stage:stage,state:state,featureRecipe:{->featureRecipe},close:{stage.close();
            close()},fit:fit]
    }
}
return [open:TissueWorkbench.&open,guidance:TissueWorkbench.&guidance]
