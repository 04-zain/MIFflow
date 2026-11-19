// Opens only explicitly opted-in, current development annotation drafts.
import java.nio.file.Files
import java.nio.file.Path
import java.nio.charset.StandardCharsets
import java.security.MessageDigest
import javafx.application.Platform
import javafx.scene.control.ChoiceDialog
import qupath.lib.roi.GeometryTools

class MifAnnotationPreviewWorkbench {
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
    static void styleChoice(javafx.scene.control.ChoiceDialog dialog,String title,String help) {
        def heading=new javafx.scene.control.Label(title)
        heading.styleClass.add("mif-title");heading.wrapText=true
        def instruction=new javafx.scene.control.Label(help)
        instruction.styleClass.add("mif-help");instruction.wrapText=true
        def header=new javafx.scene.layout.VBox(8d,heading,instruction)
        header.padding=new javafx.geometry.Insets(16d,16d,8d,16d)
        dialog.dialogPane.header=header
        dialog.dialogPane.prefWidth=520d
        dialog.resizable=true
        stylePanel(dialog.dialogPane)
        dialog.dialogPane.lookupAll('.label').findAll {it.text==dialog.contentText}.each {
            it.styleClass.add("mif-caption");it.minWidth=javafx.scene.layout.Region.USE_PREF_SIZE
        }
        dialog.dialogPane.lookupAll('.combo-box').each {
            it.minWidth=0d;it.prefWidth=360d;it.maxWidth=Double.MAX_VALUE
        }
    }

    static byte[] readBounded(Path path,int limit) {
        if(!Files.isRegularFile(path,java.nio.file.LinkOption.NOFOLLOW_LINKS) || Files.size(path)>limit)
            throw new IllegalArgumentException("The annotation preview files are unavailable. Reopen the draft from the classifier step.")
        byte[] bytes=Files.newInputStream(path,java.nio.file.LinkOption.NOFOLLOW_LINKS).withCloseable {it.readNBytes(limit+1)}
        if(bytes.length>limit)throw new IllegalArgumentException("The annotation preview file changed while it was being read.")
        bytes
    }
    static List regions(image,Map request,Map reviewPolicy) {
        List objects=image.hierarchy.annotationObjects.toList()
        List domains=objects.findAll {object->
            def m=object.metadata
            object.isLocked() && object.ROI?.isArea() && m.pipeline_stage=="analysis_domain_selection" &&
            m.analysis_domain_status=="accepted" && m.analysis_domain_active=="true" &&
            (!request.allowed_domain_ids || request.allowed_domain_ids.contains(m.analysis_domain_id))
        }
        if(domains.isEmpty())domains=objects.findAll {object->
            if(!object.ROI?.isArea())return false
            String id=reviewPolicy.wholeTissueDomainId.call(object)
            id!=null && (!request.allowed_domain_ids || request.allowed_domain_ids.contains(id))
        }
        if(domains.isEmpty())throw new IllegalArgumentException("Accept the tissue and analysis regions before enabling classifier annotation preview.")
        if(request.classifier_role=="primary")return domains
        List parents=objects.findAll {object->
            def m=object.metadata
            String kind=m.pipeline_stage=="primary_classifier"?m.primary_class_id:m.pipeline_stage=="secondary_classifier"?m.structure_id:null
            object.isLocked() && object.ROI?.isArea() && m.pipeline_active=="true" && request.eligible_parent_ids.contains(kind) &&
            domains.any {domain->object.ROI.z==domain.ROI.z && object.ROI.t==domain.ROI.t && object.ROI.geometry.coveredBy(domain.ROI.geometry)}
        }
        if(request.eligible_parent_ids.contains("AnalysisDomain"))parents.addAll(domains)
        if(parents.isEmpty())throw new IllegalArgumentException("There are no accepted eligible parent regions for this secondary preview.")
        parents.unique {it.ID.toString()}
    }
    static open(gui,Map request,Map support,Path bundle) {
        if(request.schema!="mif-annotation-preview/v1" || request.mode!="exploratory" || !(request.ticket_id==~/[0-9a-f]{32}/))
            throw new IllegalArgumentException("Reopen the explicitly enabled annotation preview from its classifier step.")
        Path project=gui.project?.path
        Path ticketPath=project?.parent?.parent?.resolve("cluster-ticket.json")
        if(project==null || ticketPath==null || !Files.isRegularFile(ticketPath,java.nio.file.LinkOption.NOFOLLOW_LINKS) || Files.size(ticketPath)>32L*1024*1024)
            throw new IllegalArgumentException("Open the annotation draft prepared by the current classifier step.")
        def gson=new com.google.gson.Gson()
        Map ticket=gson.fromJson(new String(readBounded(ticketPath,32*1024*1024),StandardCharsets.UTF_8),Map.class)
        if(ticket.annotation_preview==null || ticket.ticket_id!=request.ticket_id || ticket.entry_id!=request.source_entry_id ||
            gson.toJsonTree(request)!=gson.toJsonTree(ticket.annotation_preview+[ticket_id:ticket.ticket_id]))
            throw new IllegalArgumentException("The preview settings differ from this annotation draft. Reopen it from the classifier step.")
        def image=gui.viewer.imageData
        if(image==null || gui.readOnly)throw new IllegalArgumentException("Open the current development annotation draft first.")
        if(gui.project.getEntry(image)?.ID?.toString()!="1")throw new IllegalArgumentException("Open the development image provided in this annotation draft.")
        def uris=image.server.URIs
        if(!(ticket.local_image_path instanceof String) || uris.size()!=1 || uris.first().scheme!="file" ||
            Path.of(uris.first()).toAbsolutePath().normalize()!=Path.of(ticket.local_image_path).toAbsolutePath().normalize())
            throw new IllegalArgumentException("The annotation draft now refers to another image. Reopen the selected development image from the classifier step.")
        request.channels.each {row->
            if(row.index<0 || row.index>=image.server.nChannels() || image.server.metadata.channels[row.index as int].name!=row.name)
                throw new IllegalArgumentException("The preview image channel names or order changed.")
        }
        List parents=regions(image,request,support.reviewPolicy)
        def parent=parents.first()
        if(parents.size()>1) {
            Map choices=[:];
            parents.eachWithIndex {object,int index->choices[(object.name?:"Preview region "+(index+1))+" · "+(index+1)]=object}
            ChoiceDialog<String> dialog=new ChoiceDialog<>(choices.keySet().first(),choices.keySet())
            dialog.initOwner(gui.stage);
            dialog.title="Annotation preview";
            dialog.headerText="Choose the tissue or parent region for this preview.";
            dialog.contentText="Region";
            dialog.resizable=true;
            styleChoice(dialog,"Choose a preview region","Choose the tissue or parent region for this preview.")
            def selected=dialog.showAndWait();
            if(selected.isEmpty())return null
            parent=choices[selected.get()]
        }
        image.setProperty("mif.annotation_feedback","model_assisted")
        Map configuration=request.configuration
        if(!configuration.configuration_schema.startsWith("qupath-kronos")) {
            Path source=bundle.resolve("general/02_train_native_classifier.groovy")
            def inventory=new com.google.gson.Gson().fromJson(new String(readBounded(bundle.resolve("qupath-script-bundle.v1.json"),1024*1024),StandardCharsets.UTF_8),Map.class)
            def expected=inventory.assets.find {it.path=="general/02_train_native_classifier.groovy"}
            byte[] bytes=readBounded(source,1024*1024)
            if(bytes.length>1024*1024 || MessageDigest.getInstance("SHA-256").digest(bytes).encodeHex().toString()!=expected?.sha256)
                throw new IllegalArgumentException("The native preview fitting tools changed. Reopen the application.")
            return support.nativeAnnotation.open.call(gui,request,parent,support+[nativeTrainingSource:new String(bytes,StandardCharsets.UTF_8)])
        }
        Map encoder=configuration.encoder
        // The preview runs on the annotation computer. No inaccessible cluster
        // interpreter or model directory is substituted with another installation.
        if(!Files.isExecutable(Path.of(encoder.worker_command[0])) || !Files.isDirectory(Path.of(encoder.package_directory)))
            throw new IllegalArgumentException("KRONOS annotation preview needs the selected Python program and KRONOS model folder on this annotation computer. Leave preview off to annotate without loading a model.")
        Map nativeConfig=configuration.native_configuration
        Map parameters=nativeConfig.rtrees ?: nativeConfig.svm
        Map settings=[normalization:nativeConfig.normalization,seed:request.seed,
            max_depth:parameters.max_depth,min_sample_count:parameters.min_sample_count,max_trees:parameters.max_trees,
            active_var_count:parameters.active_var_count,kernel:parameters.kernel,c:parameters.c,gamma:parameters.gamma,
            max_iterations:parameters.max_iterations,epsilon:parameters.termination_epsilon]
        double sx=image.server.pixelCalibration.pixelWidthMicrons,sy=image.server.pixelCalibration.pixelHeightMicrons
        double ds=(encoder.pixel_size_um[0] as double)/sx
        if(!Double.isFinite(ds) || ds<1d || Math.abs((encoder.pixel_size_um[1] as double)/sy-ds)>1e-9)
            throw new IllegalArgumentException("The image cannot supply the selected KRONOS physical sampling size.")
        settings.configuration=nativeConfig
        Map operations=new LinkedHashMap(support.operations)
        operations.fitNative=support.training.fitNative
        return new MifKronosAnnotationLoader(gui,image,parent,request,support,operations,settings,ds)

    }
}
final class MifKronosAnnotationLoader implements AutoCloseable {
    final javafx.stage.Stage stage=new javafx.stage.Stage()
    final def executor=java.util.concurrent.Executors.newSingleThreadExecutor()
    volatile boolean resourcesClosed=false
    volatile boolean closed=false
    volatile Object client=null
    volatile Object backend=null
    Object session=null
    private final Object gui
    private final Object image
    private final javafx.beans.value.ChangeListener imageChanged
    MifKronosAnnotationLoader(gui,image,parent,Map request,Map support,Map operations,Map settings,double ds) {
        this.gui=gui;
        this.image=image
        stage.initOwner(gui.stage);
        stage.title="Preparing KRONOS preview"
        def title=new javafx.scene.control.Label("Preparing KRONOS preview")
        title.styleClass.add("mif-title");title.wrapText=true
        def header=new javafx.scene.layout.VBox(title)
        header.padding=new javafx.geometry.Insets(18d,20d,14d,20d)
        def label=new javafx.scene.control.Label("Preparing the model and image for preview…");
        label.wrapText=true;label.styleClass.add("mif-help")
        def progress=new javafx.scene.control.ProgressIndicator()
        progress.setPrefSize(32d,32d);progress.setMaxSize(32d,32d)
        def cancel=new javafx.scene.control.Button("Cancel preview");
        cancel.setOnAction({event->close()} as javafx.event.EventHandler)
        def body=new javafx.scene.layout.VBox(16d,label,progress)
        body.padding=new javafx.geometry.Insets(12d,20d,20d,20d)
        def footer=new javafx.scene.layout.HBox(cancel)
        footer.alignment=javafx.geometry.Pos.CENTER_RIGHT
        footer.padding=new javafx.geometry.Insets(12d,20d,16d,20d)
        footer.styleClass.add("mif-footer")
        def scroll=new javafx.scene.control.ScrollPane(body)
        scroll.fitToWidth=true;scroll.hbarPolicy=javafx.scene.control.ScrollPane.ScrollBarPolicy.NEVER
        def layout=new javafx.scene.layout.BorderPane(scroll,header,null,footer,null)
        MifAnnotationPreviewWorkbench.stylePanel(layout)
        stage.scene=new javafx.scene.Scene(layout,470,260)
        stage.minWidth=390d;stage.minHeight=230d
        stage.setOnCloseRequest({event->event.consume();
            close()} as javafx.event.EventHandler)
        imageChanged={o,a,b->if(b!=image)close()} as javafx.beans.value.ChangeListener
        gui.viewer.imageDataProperty().addListener(imageChanged)
        def shape=parent.ROI.geometry.copy()
        List scopes=[[id:parent.ID.toString(),geometry:shape,revision:parent.metadata.analysis_domain_revision_sha256?:parent.metadata.object_revision_sha256,
            split:"training",z:parent.ROI.z,t:parent.ROI.t]]
        Map identity=[patient_id:request.subject_id,image_id:image.server.path,entry_id:request.source_entry_id,split:"training",
            fold_id:"annotation-preview",domain_id:parent.ID.toString(),domain_revision:scopes.first().revision,
            tissue_component_id:parent.ID.toString(),eligible_parent_id:parent.ID.toString()]
        operations.display={options,data,classifier,boolean ignored->support.classifierReview.createOverlay.call(options,data,classifier,scopes,support.displayPolicy)}
        operations.initialMethod=request.configuration.configuration_schema=="qupath-kronos-svm/v2"?"svm":"rf"
        operations.lockMethod=true;
        operations.referenceGeometry=shape
        Map worker=support.training.workerSettings.call(request.configuration.encoder)
        stage.show()
        executor.submit({
            Throwable error=null
            try {
                client=support.workers.createTemporary(worker,"mif-annotation-kronos-")
                if(!closed)backend=support.backend.backend(image,shape,ds,request.channels.collect{it.index as int},identity,support.geometry,operations,client)
            }catch(Throwable failure){error=failure}
            if(closed || error!=null) {
                try {backend?.close();
                    client?.close()}finally{resourcesClosed=true}
                if(error!=null){def failure=error;
                    Platform.runLater({stage.hide();
                        if(!closed)qupath.fx.dialogs.Dialogs.showErrorMessage("KRONOS preview",failure.message?:failure.class.simpleName);
                        close()} as Runnable)}
                return
            }
            Platform.runLater({
                stage.hide()
                if(closed || gui.viewer.imageData!=image){close();
                    return}
                try {
                    session=support.preview.open.call(gui,backend,request.labels.collect{it.name},request.configuration.graph,settings,operations)
                    session.stage.addEventHandler(javafx.stage.WindowEvent.WINDOW_HIDDEN,{event->close()} as javafx.event.EventHandler)
                }
                catch(Throwable failure){qupath.fx.dialogs.Dialogs.showErrorMessage("KRONOS preview",failure.message?:failure.class.simpleName);
                    close()}
            } as Runnable)
        } as Runnable)
    }
    javafx.stage.Stage getStage(){session!=null?session.stage:stage}
    boolean isResourcesClosed(){session!=null?session.resourcesClosed:resourcesClosed}
    void close() {
        if(closed)return
        closed=true;
        stage.hide();
        gui.viewer.imageDataProperty().removeListener(imageChanged)
        if(session!=null){session.close();
            executor.shutdown();
            return}
        executor.submit({try{backend?.close();
                client?.close()}finally{resourcesClosed=true}} as Runnable)
        executor.shutdown()
    }
}
return [open:MifAnnotationPreviewWorkbench.&open,regions:MifAnnotationPreviewWorkbench.&regions]
