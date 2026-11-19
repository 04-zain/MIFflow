/** Shared native image-feature editor for tissue detection and tissue classification.
 *
 * The window edits a private copy of the existing physical feature recipe. Apply
 * returns that recipe; closing or cancelling leaves the caller's settings intact.
 * The bounded image comparison uses the same PixelFeatureOps operation as fitting.
 */
import java.util.concurrent.Executors
import javafx.application.Platform
import javafx.animation.PauseTransition
import javafx.util.Duration
import javafx.stage.Stage
import javafx.scene.Scene
import javafx.scene.control.*
import javafx.scene.layout.*
import javafx.beans.value.ChangeListener
import qupath.lib.images.ImageData

class PixelFeatureWorkbench {
    static final Map FILTER_NAMES=[gaussian:"Gaussian smoothing",laplacian:"Laplacian",weighted_std_dev:"Local standard deviation",
        gradient_magnitude:"Gradient magnitude",hessian_determinant:"Hessian determinant",
        hessian_eigenvalue_max:"Hessian: maximum eigenvalue",hessian_eigenvalue_min:"Hessian: minimum eigenvalue",
        structure_tensor_eigenvalue_max:"Structure tensor: maximum eigenvalue",
        structure_tensor_eigenvalue_min:"Structure tensor: minimum eigenvalue",structure_tensor_coherence:"Structure tensor: coherence"]
    /** Local panel roles match the export picker and Qt application. */
    static void styleWorkbench(javafx.scene.layout.Pane root) {
        Map loadedFontFamilies=[:]
        ["Regular","SemiBold"].each {weight->
            def stream=org.mif.cells.NativeCellScope.class.getResourceAsStream("/org/mif/fonts/ubuntu-sans/UbuntuSans-${weight}.ttf")
            if(stream!=null)try {
                def font=javafx.scene.text.Font.loadFont(stream,14d)
                if(font!=null)loadedFontFamilies[weight]=font.family
            }finally {stream.close()}
        }
        List preferences=System.getProperty("os.name","").startsWith("Windows") ?
            ["Segoe UI",loadedFontFamilies.Regular ?: "Ubuntu Sans","DejaVu Sans"] :
            [loadedFontFamilies.Regular ?: "Ubuntu Sans","Ubuntu","DejaVu Sans"]
        String family=preferences.find {it==loadedFontFamilies.Regular || javafx.scene.text.Font.families.contains(it)} ?: javafx.scene.text.Font.default.family
        String headingFamily=(family==loadedFontFamilies.Regular && loadedFontFamilies.SemiBold) ?
            loadedFontFamilies.SemiBold : (["${family} SemiBold","${family} Semibold"].find {
                javafx.scene.text.Font.families.contains(it.toString())
            } ?: family)
        root.style="-fx-font-family:'${family.replace("'","")}';-fx-font-size:15px;"
        String css="""
            .root { -fx-background-color:white; -fx-accent:#006b70; -fx-focus-color:#006b70; -fx-faint-focus-color:transparent; }
            .label, .check-box, .radio-button { -fx-text-fill:#172d35; }
            .panel-title { -fx-font-family:'${headingFamily}'; -fx-font-size:20px; -fx-font-weight:600; }
            .section-title { -fx-font-family:'${headingFamily}'; -fx-font-size:16px; -fx-font-weight:600; -fx-text-fill:#172d35; }
            .field-caption { -fx-font-family:'${headingFamily}'; -fx-font-weight:600; -fx-text-fill:#294c56; }
            .help { -fx-text-fill:#486169; -fx-line-spacing:2; }
            .section { -fx-border-color:#dce4e4 transparent transparent transparent; -fx-border-width:1 0 0 0; -fx-padding:14 0 0 0; }
            .actions { -fx-background-color:#f3f5f5; -fx-border-color:#dce4e4 transparent transparent transparent; -fx-border-width:1 0 0 0; -fx-padding:14 20; }
            .button, .combo-box, .text-field {
                -fx-background-color:white; -fx-background-insets:0; -fx-background-radius:3;
                -fx-border-color:#bdcccc; -fx-border-radius:3; -fx-border-width:1; -fx-text-fill:#294148; -fx-padding:7 10;
            }
            .button:hover, .combo-box:hover { -fx-border-color:#8eaaa5; -fx-background-color:#eaf2ef; }
            .button:focused, .combo-box:focused, .text-field:focused { -fx-border-color:#006b70; }
            .button:pressed { -fx-background-color:#dcece6; }
            .primary { -fx-background-color:#006b70; -fx-border-color:#006b70; -fx-text-fill:white; }
            .primary:hover { -fx-background-color:#00575c; -fx-border-color:#00575c; }
            .primary:focused { -fx-border-color:#82bcb3; }
            .combo-box > .list-cell { -fx-background-color:transparent; -fx-text-fill:#172d35; -fx-padding:0 8 0 0; }
            .combo-box > .arrow-button { -fx-background-color:transparent; -fx-padding:0 2 0 8; }
            .combo-box .arrow { -fx-background-color:#526971; }
            .combo-box-popup .list-view { -fx-background-color:white; -fx-background-insets:0; -fx-border-color:#bdcccc; -fx-padding:3; }
            .combo-box-popup .list-cell { -fx-background-color:white; -fx-text-fill:#172d35; -fx-padding:8 10; }
            .combo-box-popup .list-cell:filled:hover, .combo-box-popup .list-cell:filled:selected { -fx-background-color:#e4f0ee; -fx-text-fill:#00575c; }
            .scroll-pane, .scroll-pane > .viewport { -fx-background-color:white; -fx-background-insets:0; -fx-padding:0; }
            .check-box > .box { -fx-background-color:white; -fx-background-insets:0; -fx-border-color:#9bb3b3; -fx-border-radius:2; -fx-background-radius:2; -fx-padding:3; }
            .check-box:selected > .box { -fx-background-color:#006b70; -fx-border-color:#006b70; }
            .check-box:selected > .box > .mark { -fx-background-color:white; }
            .radio-button > .radio { -fx-background-color:white; -fx-border-color:#9bb3b3; -fx-border-radius:12; -fx-background-insets:0; -fx-padding:3; }
            .radio-button:selected > .radio > .dot { -fx-background-color:#006b70; }
            .scale-row { -fx-padding:5 4; -fx-background-radius:0; }
            .scale-row .button { -fx-padding:4 7; }
            .recipe { -fx-border-color:transparent #dce4e4 transparent transparent; -fx-border-width:0 1 0 0; -fx-padding:0 16 0 0; }
            .inspection-settings { -fx-border-color:#dce4e4 transparent transparent transparent; -fx-border-width:1 0 0 0; -fx-padding:14 0 0 0; }
        """
        root.stylesheets.add("data:text/css;base64,"+java.util.Base64.encoder.encodeToString(css.getBytes(java.nio.charset.StandardCharsets.UTF_8)))
    }

    /** Physical sigma values stay editable independently of the scale inspected. */
    static Map physicalScaleControls(List initial, Closure sampling) {
        def changes=new javafx.beans.property.SimpleLongProperty(0)
        def selected=new javafx.beans.property.SimpleObjectProperty<TextField>()
        ToggleGroup inspectGroup=new ToggleGroup()
        VBox rows=new VBox(6d)
        List entries=[]
        Label scaleHeader=new Label("σ (µm)")
        Label pixelHeader=new Label("σ (pixels)")
        Closure refresh={ ->
            def pixels=sampling()
            boolean square=(pixels[0] as double)==(pixels[1] as double)
            pixelHeader.text=square?"σ (pixels)":"σ X / Y (pixels)"
            entries.each {row ->
                try {
                    double value=Double.parseDouble(row.field.text.trim())
                    if(!Double.isFinite(value) || value<=0d)throw new NumberFormatException()
                    double x=value/(pixels[0] as double),y=value/(pixels[1] as double)
                    row.inspect.text=String.format(Locale.ROOT,"%.6g µm",value)
                    row.equivalent.text=square?String.format(Locale.ROOT,"%.4g",x):String.format(Locale.ROOT,"%.4g / %.4g",x,y)
                }catch(NumberFormatException ignored){row.inspect.text="Enter σ";row.equivalent.text=""}
            }
        }
        Closure changed={ -> refresh();changes.set(changes.get()+1) }
        Closure addRow={String value,List chosen ->
            TextField field=new TextField(value)
            field.minWidth=120d;field.prefWidth=230d;field.maxWidth=230d
            field.id="tissue-filter-scale-value"
            field.accessibleText="Gaussian sigma in micrometres"
            field.promptText="Enter σ"
            RadioButton inspect=new RadioButton();inspect.toggleGroup=inspectGroup
            inspect.id="tissue-filter-scale-inspect";inspect.minWidth=Region.USE_PREF_SIZE
            inspect.setOnAction({event -> selected.set(field)})
            Button remove=new Button("Remove");remove.id="tissue-filter-scale-remove";remove.minWidth=Region.USE_PREF_SIZE
            Label equivalent=new Label();equivalent.id="tissue-filter-scale-pixels";equivalent.minWidth=54d
            inspect.minWidth=108d;inspect.maxWidth=108d
            HBox box=new HBox(8d,inspect,equivalent,remove);box.styleClass.add("scale-row")
            box.alignment=javafx.geometry.Pos.CENTER_LEFT
            HBox.setHgrow(equivalent,Priority.ALWAYS);equivalent.maxWidth=Double.MAX_VALUE
            Map row=[field:field,filters:new ArrayList(chosen),equivalent:equivalent,box:box,inspect:inspect]
            entries.add(row);rows.children.add(box)
            field.textProperty().addListener({obs,old,updated -> changed()} as ChangeListener)
            remove.setOnAction({event ->
                entries.remove(row);rows.children.remove(box);inspect.toggleGroup=null
                if(selected.get()==field){selected.set(entries.isEmpty()?null:entries[0].field);if(!entries.isEmpty())entries[0].inspect.selected=true}
                changed()
            })
            if(selected.get()==null){inspect.selected=true;selected.set(field)}
            changed()
            field
        }
        Closure highlight={ -> entries.each {row ->
            boolean active=row.field==selected.get();row.inspect.selected=active
            row.box.style=active?"-fx-background-color:#e4f0ee;":""
        }}
        selected.addListener({obs,old,value -> highlight()} as ChangeListener)
        initial.each {addRow(Double.toString((it.sigma_um as Number).doubleValue()),it.filters)}
        highlight()
        HBox headers=new HBox(8d,scaleHeader,pixelHeader)
        scaleHeader.minWidth=108d;scaleHeader.maxWidth=108d
        Button add=new Button("Add scale");add.id="tissue-filter-scale-add"
        add.setOnAction({event ->
            Map current=entries.find {it.field==selected.get()}
            double next=1d
            try {if(current!=null)next=Double.parseDouble(current.field.text.trim())*2d}catch(NumberFormatException ignored){}
            if(!Double.isFinite(next)||next<=0d)next=1d
            while(Double.isFinite(next) && entries.any {it.field.text.trim().isDouble() && it.field.text.trim().toDouble()==next})next*=2d
            def field=addRow(Double.isFinite(next)?Double.toString(next):"",current?.filters ?: []);selected.set(field)
        })
        VBox node=new VBox(6d,headers,rows,add)
        node.id="tissue-filter-scales"
        return [node:node,changes:changes,selected:selected,entries:{->entries},fields:{->entries.collect {it.field}},refresh:refresh]
    }

    /** A bounded view of the same native operation used by the fitted classifier. */
    static Map featureInspection(gui, data, Map scaleControls, Map filterControls,
                                 Map filterNames, channels, List channelIndices, double downsample,
                                 double[] featureSampling, worker, Map session, long delayMillis) {
        Map state=[active:false,generation:0L,pending:null,center:null,result:null,syncing:false,rangeField:null]
        def originalView=new javafx.scene.image.ImageView()
        def filteredView=new javafx.scene.image.ImageView()
        [originalView,filteredView].each {it.fitWidth=220d;it.fitHeight=Double.MAX_VALUE;it.preserveRatio=true;it.smooth=false}
        originalView.id="tissue-feature-original";filteredView.id="tissue-feature-filtered"
        Label originalCaption=new Label("Original channel"),filteredCaption=new Label("Filtered channel")
        [originalCaption,filteredCaption].each {it.minWidth=220d;it.prefWidth=220d;it.maxWidth=220d;it.minHeight=40d;it.maxHeight=Double.MAX_VALUE;it.wrapText=true;it.alignment=javafx.geometry.Pos.TOP_LEFT}
        originalCaption.id="tissue-feature-original-caption";filteredCaption.id="tissue-feature-filtered-caption"
        Label originalRange=new Label(),filteredRange=new Label()
        [originalRange,filteredRange].each {it.styleClass.add("help");it.wrapText=true}
        // One shared caption row keeps both images aligned when a feature name wraps.
        GridPane images=new GridPane();images.hgap=14d;images.vgap=5d
        images.add(originalCaption,0,0);images.add(filteredCaption,1,0)
        images.add(originalView,0,1);images.add(filteredView,1,1)
        images.add(originalRange,0,2);images.add(filteredRange,1,2)
        ComboBox<String> feature=new ComboBox<>();feature.id="tissue-inspect-filter";feature.maxWidth=Double.MAX_VALUE
        Button selectedRegion=new Button("Use selected region"),currentView=new Button("Use current view")
        selectedRegion.id="tissue-feature-selected-region";currentView.id="tissue-feature-current-view"
        selectedRegion.tooltip=new Tooltip(gui.readOnly ?
            "Center the fixed field inside one existing selected area annotation. Use current view to inspect another location." :
            "Center the fixed field inside one selected area annotation. Draw or move a rectangle in QuPath, then use this action to recenter.")
        currentView.tooltip=new Tooltip("Center the fixed field on the current QuPath view, even when an annotation is selected.")
        HBox location=new HBox(8d,selectedRegion,currentView)
        Label extent=new Label("Choose a region to inspect.");extent.wrapText=true;extent.id="tissue-feature-field"
        Label status=new Label();status.wrapText=true;status.id="tissue-feature-status"
        Label scaleLabel=new Label("Filter scale to edit")
        Slider adjustment=new Slider();adjustment.id="tissue-feature-adjustment";adjustment.showTickLabels=true
        adjustment.maxWidth=Double.MAX_VALUE
        adjustment.labelFormatter=[toString:{value -> String.format(Locale.ROOT,"%.3g",value)},fromString:{text -> Double.parseDouble(text)}] as javafx.util.StringConverter
        TextField exact=new TextField();exact.id="tissue-feature-exact-sigma";exact.prefWidth=210d;exact.maxWidth=210d
        Label exactUnit=new Label("µm")
        scaleLabel.text="Selected Gaussian σ (µm)"
        scaleLabel.labelFor=exact
        HBox exactRow=new HBox(8d,exact,exactUnit);exactRow.alignment=javafx.geometry.Pos.CENTER_LEFT
        exact.accessibleText="Exact inspected Gaussian sigma in micrometres"
        Label equivalent=new Label();equivalent.id="tissue-feature-adjustment-pixels"
        feature.tooltip=new Tooltip("Displays one selected feature. Every checked feature is included when fitting. Gaussian smoothing uses the original channel's display range; derivative and texture features have their own ranges, shown below the image.")
        def bar=new javafx.scene.shape.Line(0d,0d,88d,0d);bar.strokeWidth=3d
        Label barLabel=new Label();HBox scaleBar=new HBox(8d,bar,barLabel);scaleBar.alignment=javafx.geometry.Pos.CENTER_LEFT
        scaleLabel.styleClass.add("field-caption")
        equivalent.styleClass.add("help");extent.styleClass.add("help");status.styleClass.add("help")
        VBox adjustmentGroup=new VBox(6d,scaleLabel,exactRow,adjustment,equivalent);adjustmentGroup.styleClass.add("inspection-settings")
        VBox comparison=new VBox(8d,images,scaleBar,adjustmentGroup)
        comparison.minWidth=0d;comparison.maxWidth=440d;images.minWidth=0d
        Label featureLabel=new Label("Feature");featureLabel.labelFor=feature;featureLabel.styleClass.add("field-caption")
        HBox featureChoice=new HBox(10d,featureLabel,feature);featureChoice.alignment=javafx.geometry.Pos.CENTER_LEFT
        featureLabel.minWidth=72d;HBox.setHgrow(feature,Priority.ALWAYS)
        VBox panel=new VBox(10d,featureChoice,location,extent,comparison,status)
        panel.minWidth=0d
        panel.alignment=javafx.geometry.Pos.TOP_CENTER
        extent.maxWidth=Double.MAX_VALUE
        status.managedProperty().bind(status.textProperty().isNotEmpty());status.visibleProperty().bind(status.managedProperty())
        def tileWidth=javafx.beans.binding.Bindings.createDoubleBinding({ -> Math.max(1d,(comparison.width-14d)/2d) } as java.util.concurrent.Callable,comparison.widthProperty())
        [originalView,filteredView].each {it.fitWidthProperty().bind(tileWidth)}
        [originalCaption,filteredCaption].each {it.minWidthProperty().bind(tileWidth);it.prefWidthProperty().bind(tileWidth);it.maxWidthProperty().bind(tileWidth)}
        bar.endXProperty().bind(javafx.beans.binding.Bindings.createDoubleBinding({ -> originalView.boundsInLocal.width*0.4d } as java.util.concurrent.Callable,originalView.boundsInLocalProperty()))
        panel.id="tissue-feature-inspection"
        PauseTransition debounce=new PauseTransition(Duration.millis(delayMillis))
        Closure selectedArea={ ->
            def selection=data.hierarchy.selectionModel
            def obj=selection.singleSelection()?selection.selectedObject:null
            obj?.isAnnotation() && obj.ROI?.isArea()?obj:null
        }
        Closure fromAnnotation={obj ->
            def point=obj.ROI.geometry.interiorPoint.coordinate
            state.center=[x:point.x,y:point.y,z:obj.ROI.z,t:obj.ROI.t,source:"Annotation interior"]
        }
        Closure fromView={ -> state.center=[x:gui.viewer.centerPixelX,y:gui.viewer.centerPixelY,
            z:gui.viewer.getZPosition(),t:gui.viewer.getTPosition(),source:"Current view"] }
        Closure syncSlider={boolean reset ->
            def field=scaleControls.selected.get()
            if(state.boundField!=field) {
                if(state.boundField!=null)exact.textProperty().unbindBidirectional(state.boundField.textProperty())
                exact.text=field?.text ?: "";exact.disable=field==null
                if(field!=null)exact.textProperty().bindBidirectional(field.textProperty())
                state.boundField=field
            }
            try {
                double sigma=Double.parseDouble(field?.text?.trim() ?: "")
                if(!Double.isFinite(sigma)||sigma<=0d)throw new NumberFormatException()
                state.syncing=true
                if(!adjustment.valueChanging && (reset || state.rangeField!=field || sigma<adjustment.min || sigma>adjustment.max)) {
                    // A local exploration range, never a restriction on exact entry.
                    adjustment.min=Math.max(Double.MIN_VALUE,sigma/4d);adjustment.max=Double.isFinite(sigma*4d)?sigma*4d:Double.MAX_VALUE
                    adjustment.majorTickUnit=(adjustment.max-adjustment.min)/3d
                    adjustment.blockIncrement=sigma/20d;state.rangeField=field
                }
                adjustment.value=sigma;adjustment.disable=false
                double d=downsample
                double x=sigma/featureSampling[0],y=sigma/featureSampling[1]
                equivalent.text=x==y?String.format(Locale.ROOT,"σ = %.4g pixels",x):String.format(Locale.ROOT,"σ X / Y = %.4g / %.4g pixels",x,y)
            }catch(NumberFormatException ignored){adjustment.disable=true;equivalent.text="Enter a positive σ in the inspected row."}
            finally{state.syncing=false}
        }
        Closure request={ ->
            state.generation=(state.generation as long)+1
            state.pending?.cancel(false)
            state.result=null
            // Hide obsolete pixels immediately rather than showing them under new settings.
            originalView.image=null;filteredView.image=null;originalRange.text="";filteredRange.text=""
            if(!state.active || session.closed)return
            if(feature.value==null){status.text="Select a filter to compare it with the original image.";return}
            long generation=state.generation
            try {
                if(state.center==null){def obj=selectedArea();if(obj!=null)fromAnnotation(obj);else fromView()}
                int channel=channelIndices[channels.selectionModel.selectedIndex] as int
                String filter=filterNames.find {key,label -> label==feature.value}?.key
                if(filter==null)throw new IllegalArgumentException("Select a filter above to inspect its image values.")
                double sigma=Double.parseDouble(scaleControls.selected.get()?.text?.trim() ?: "")
                if(!Double.isFinite(sigma)||sigma<=0d)throw new IllegalArgumentException("Enter a positive σ in the inspected row.")
                double d=downsample
                double rx=featureSampling[0],ry=featureSampling[1]
                def prepared=org.mif.classification.PixelFeatureOps.create([channel] as int[],true,[new org.mif.classification.PixelFeatureOps.Scale(sigma,[filter])],rx,ry)
                if(state.center.sampleDownsample!=d) {
                    // Choose the field once for this location/resolution; scale edits
                    // keep it fixed even when the larger padding cannot be inspected.
                    double room=Math.floor(Math.sqrt(16_777_216d/data.server.nChannels()))-Math.max(prepared.padding().getXSum(),prepared.padding().getYSum())
                    if(room<Math.min(64d,Math.min(data.server.width,data.server.height)/d))
                        throw new IllegalArgumentException("This σ needs a larger inspection workspace. Its exact value is kept; use a coarser image resolution or the classifier preview to review it.")
                    state.center.sampleSide=Math.min(256d,room);state.center.sampleDownsample=d
                }
                int side=(int)Math.max(1d,Math.min((state.center.sampleSide as double)*d,Math.min(data.server.width,data.server.height)))
                int x=(int)Math.max(0d,Math.min((state.center.x as double)-side/2d,data.server.width-side))
                int y=(int)Math.max(0d,Math.min((state.center.y as double)-side/2d,data.server.height-side))
                def region=qupath.lib.regions.RegionRequest.createInstance(data.server.path,d,x,y,side,side,state.center.z as int,state.center.t as int)
                // Bound preview input to 64 MiB of float samples, including every
                // source channel and native filter padding. Exact recipe values stay unrestricted.
                long paddedWidth=(long)Math.ceil(side/d)+prepared.padding().getXSum()
                long paddedHeight=(long)Math.ceil(side/d)+prepared.padding().getYSum()
                if((double)paddedWidth*paddedHeight*data.server.nChannels()>16_777_216d)
                    throw new IllegalArgumentException("This σ needs a larger inspection workspace. Its exact value is kept; use a coarser image resolution or the classifier preview to review it.")
                String sampling=rx==ry?String.format(Locale.ROOT,"%.4g µm/pixel",rx):String.format(Locale.ROOT,"%.4g × %.4g µm/pixel",rx,ry)
                extent.text=String.format(Locale.ROOT,"%s; field %.4g × %.4g µm at %s (x %d, y %d).",state.center.source,side*data.server.pixelCalibration.pixelWidthMicrons,side*data.server.pixelCalibration.pixelHeightMicrons,sampling,x,y)
                filteredCaption.text=String.format(Locale.ROOT,"%s\nσ %.4g\u00a0µm",feature.value,sigma)
                originalCaption.text=data.server.metadata.channels[channel].name+": original"
                status.text="Computing this field…"
                state.pending=worker.submit({
                    try {
                        if(session.closed || generation!=(state.generation as long))return
                        def mat=prepared.operation().apply(data,region)
                        Map rendered
                        try {
                            if(mat.channels()!=2 || Math.abs(mat.cols()-side/d)>1d || Math.abs(mat.rows()-side/d)>1d)
                                throw new IllegalStateException("The native feature field has unexpected dimensions.")
                            def minimum=qupath.opencv.tools.OpenCVTools.channelMinimum(mat)
                            def maximum=qupath.opencv.tools.OpenCVTools.channelMaximum(mat)
                            def pixels=qupath.opencv.tools.OpenCVTools.matToBufferedImage(mat)
                            def imageServer=new qupath.lib.images.servers.WrappedBufferedImageServer("feature-inspection-"+UUID.randomUUID(),pixels,prepared.operation().getChannels(data))
                            try {
                                def imageData=new ImageData(imageServer,ImageData.ImageType.FLUORESCENCE)
                                List displays=[]
                                for(int band=0;band<2;band++) {
                                    int rangeBand=filter=="gaussian"?0:band
                                    float low=(float)minimum[rangeBand],high=(float)maximum[rangeBand]
                                    if(!Float.isFinite(low)||!Float.isFinite(high))throw new IllegalStateException("This field contains non-finite feature values. Choose another region.")
                                    if(high<=low)high=Math.nextUp(low)
                                    def info=new qupath.lib.display.DirectServerChannelInfo(imageData,band)
                                    info.setMinDisplay(low);info.setMaxDisplay(high)
                                    displays.add(qupath.lib.display.ImageDisplay.applyTransforms(pixels,null,[info],qupath.lib.display.ChannelDisplayMode.GRAYSCALE))
                                }
                                rendered=[images:displays,min:minimum.toList(),max:maximum.toList(),width:mat.cols(),height:mat.rows(),sigma:sigma,filter:filter,region:region,channel:channel]
                            }finally{imageServer.close()}
                        }finally{mat.close()}
                        Platform.runLater({
                            if(session.closed || generation!=(state.generation as long))return
                            originalView.image=javafx.embed.swing.SwingFXUtils.toFXImage(rendered.images[0],null)
                            filteredView.image=javafx.embed.swing.SwingFXUtils.toFXImage(rendered.images[1],null)
                            originalRange.text=String.format(Locale.ROOT,"Range %.4g to %.4g",rendered.min[0],rendered.max[0])
                            int rangeBand=filter=="gaussian"?0:1
                            filteredRange.text=String.format(Locale.ROOT,"Range %.4g to %.4g",rendered.min[rangeBand],rendered.max[rangeBand])
                            barLabel.text=String.format(Locale.ROOT,"%.4g µm",side*data.server.pixelCalibration.pixelWidthMicrons*0.4d)
                            status.text=""
                            state.result=rendered.findAll {key,value -> key!="images"}
                        } as Runnable)
                    }catch(Exception error){Platform.runLater({if(!session.closed && generation==(state.generation as long))status.text=error.message ?: error.class.simpleName} as Runnable)}
                } as Runnable)
            }catch(Exception error){status.text=error instanceof NumberFormatException?"Enter a positive σ in the inspected row.":(error.message ?: error.class.simpleName)}
        }
        Closure changed={ ->
            state.generation=(state.generation as long)+1;state.pending?.cancel(false);state.result=null
            originalView.image=null;filteredView.image=null
            syncSlider(false)
            debounce.stop()
            if(feature.value==null){status.text="Select a filter to compare it with the original image.";return}
            if(state.active && !session.closed){status.text="Waiting for the adjustment…";debounce.playFromStart()}
        }
        Closure refreshFilters={ ->
            String prior=feature.value
            feature.items.setAll(filterControls.findAll {key,control -> control.selected}.keySet().collect {filterNames[it]})
            if(prior in feature.items)feature.value=prior
            else if(!feature.items.isEmpty())feature.selectionModel.selectFirst()
            else feature.value=null
            boolean available=!feature.items.isEmpty()
            [featureChoice,location,extent,comparison].each {it.visible=available;it.managed=available}
        }
        exact.setOnAction({event -> syncSlider(true)})
        exact.focusedProperty().addListener({obs,old,value -> if(!value)syncSlider(true)} as ChangeListener)
        refreshFilters();syncSlider(true)
        debounce.setOnFinished({event -> request()})
        feature.valueProperty().addListener({obs,old,value -> changed()} as ChangeListener)
        scaleControls.changes.addListener({obs,old,value -> changed()} as ChangeListener)
        scaleControls.selected.addListener({obs,old,value -> syncSlider(true);changed();exact.requestFocus()} as ChangeListener)
        [channels.valueProperty()].each {it.addListener({obs,old,value -> changed()} as ChangeListener)}
        filterControls.each {key,control ->
            control.selectedProperty().addListener({obs,old,value -> refreshFilters();changed()} as ChangeListener)
            // Inspect a newly checked feature; restoring another scale's checks
            // only refreshes the available choices and preserves the current view.
            control.setOnAction({event -> if(control.selected)feature.value=filterNames[key]})
        }
        adjustment.valueProperty().addListener({obs,old,value ->
            if(!state.syncing && scaleControls.selected.get()!=null)scaleControls.selected.get().text=String.format(Locale.ROOT,"%.8g",value.doubleValue())
        } as ChangeListener)
        selectedRegion.setOnAction({event -> def obj=selectedArea();if(obj!=null){fromAnnotation(obj);debounce.stop();request()}})
        currentView.setOnAction({event -> fromView();debounce.stop();request()})
        def selectionListener={obj,previousObject,all -> Platform.runLater({if(!session.closed)selectedRegion.disable=selectedArea()==null} as Runnable)} as qupath.lib.objects.hierarchy.events.PathObjectSelectionListener
        data.hierarchy.selectionModel.addPathObjectSelectionListener(selectionListener)
        selectedRegion.disable=selectedArea()==null
        return [node:panel,slider:adjustment,state:state,setActive:{boolean active ->
            state.active=active
            if(active)request()
            else {debounce.stop();state.generation=(state.generation as long)+1;state.pending?.cancel(false)}
        },close:{ ->
            debounce.stop();state.generation=(state.generation as long)+1;state.pending?.cancel(false)
            data.hierarchy.selectionModel.removePathObjectSelectionListener(selectionListener)
            if(state.boundField!=null)exact.textProperty().unbindBidirectional(state.boundField.textProperty())
            originalView.image=null;filteredView.image=null;state.result=null
        }]
    }

    /** Open the current image with a copied feature recipe and return changes only on Apply.
     *
     * Classifiers supply their saved scalar pixel size. Detector callers supply
     * their actual X/Y sampling and downsample. Source calibration continues to
     * determine the displayed physical field and scale bar in both cases.
     */
    static Map open(gui, Map settings, Closure onApply, Closure onCancel) {
        def data=gui.viewer.imageData
        if(data==null)throw new IllegalArgumentException("Open an image before inspecting its features.")
        List indices=new ArrayList(settings.channel_indices)
        if(indices.isEmpty() || indices.any {it<0 || it>=data.server.nChannels()})
            throw new IllegalArgumentException("Choose available image channels before inspecting features.")
        def requested=settings.pixel_size_um
        double[] sampling=requested instanceof List ? [requested[0] as double,requested[1] as double] as double[] : [requested as double,requested as double] as double[]
        double downsample=settings.downsample!=null ? settings.downsample as double : (requested as double)/data.server.pixelCalibration.averagedPixelSizeMicrons
        if(!Double.isFinite(downsample) || downsample<=0d || sampling.any {!Double.isFinite(it)||it<=0d})
            throw new IllegalArgumentException("Enter a positive image sampling size before inspecting features.")
        Map initial=settings.pixel_features
        Stage stage=new Stage();stage.initOwner(settings.owner ?: gui.stage);stage.title="Inspect and edit image features"
        Map state=[closed:false,applied:false]
        def worker=Executors.newSingleThreadExecutor({Runnable task ->
            Thread thread=new Thread(task,"image-feature-inspection");thread.daemon=true;thread
        } as java.util.concurrent.ThreadFactory)
        CheckBox intensity=new CheckBox("Include channel intensity");intensity.id="tissue-include-intensity"
        intensity.selected=initial.include_intensity
        Map filters=[:]
        VBox filterRows=new VBox(8d)
        FILTER_NAMES.each {key,label ->
            CheckBox control=new CheckBox(label);control.id="tissue-filter-"+key;control.wrapText=true
            control.maxWidth=Double.MAX_VALUE
            filters[key]=control;filterRows.children.add(control)
        }
        Map scales=physicalScaleControls(initial.scales,{->sampling})
        Map filterState=[loading:false]
        Closure loadFilters={ ->
            Map row=scales.entries().find {it.field==scales.selected.get()}
            filterState.loading=true
            filters.each {key,control -> control.selected=row!=null && key in row.filters;control.disable=row==null}
            filterState.loading=false
        }
        scales.selected.addListener({obs,old,value -> loadFilters()} as ChangeListener)
        filters.values().each {control -> control.selectedProperty().addListener({o,a,b ->
            if(!filterState.loading) {
                Map row=scales.entries().find {it.field==scales.selected.get()}
                if(row!=null) {
                    List checked=filters.findAll {key,item->item.selected}.keySet().toList()
                    row.filters=row.filters.findAll {it in checked}+checked.findAll {!(it in row.filters)}
                    row.inspect.tooltip=new Tooltip(row.filters.collect{FILTER_NAMES[it]}.join(", "))
                }
            }
        } as ChangeListener)}
        loadFilters()
        Label scaleHelp=new Label("New scales copy the selected scale’s filters.");scaleHelp.wrapText=true;scaleHelp.styleClass.add("help")
        Label scaleTitle=new Label("Scales");scaleTitle.styleClass.add("section-title")
        VBox scaleRows=new VBox(8d,scaleTitle,scaleHelp,scales.node)
        ComboBox<String> channels=new ComboBox<>();channels.id="feature-preview-channel"
        channels.items.setAll(indices.collect{data.server.metadata.channels[it as int].name});channels.selectionModel.selectFirst();channels.maxWidth=Double.MAX_VALUE
        Label channelLabel=new Label("Channel");channelLabel.labelFor=channels;channelLabel.styleClass.add("field-caption")
        Label filterTitle=new Label("Filters at the selected scale");filterTitle.styleClass.add("section-title")
        VBox filterGroup=new VBox(10d,filterTitle,filterRows);filterGroup.styleClass.add("section")
        VBox intensityGroup=new VBox(8d,intensity);intensityGroup.styleClass.add("section")
        VBox recipe=new VBox(16d,scaleRows,filterGroup,intensityGroup);recipe.styleClass.add("recipe")
        recipe.minWidth=292d;recipe.prefWidth=322d;recipe.maxWidth=322d
        Map inspection=featureInspection(gui,data,scales,filters,FILTER_NAMES,channels,indices,downsample,sampling,worker,state,250L)
        Label previewTitle=new Label("Inspect the image");previewTitle.styleClass.add("section-title")
        HBox channelChoice=new HBox(10d,channelLabel,channels);channelChoice.alignment=javafx.geometry.Pos.CENTER_LEFT
        channelLabel.minWidth=72d;HBox.setHgrow(channels,Priority.ALWAYS)
        VBox preview=new VBox(10d,previewTitle,channelChoice,inspection.node);preview.minWidth=0d
        HBox body=new HBox(20d,recipe,preview);HBox.setHgrow(preview,Priority.ALWAYS)
        body.padding=new javafx.geometry.Insets(0,20,20,20)
        ScrollPane scroll=new ScrollPane(body);scroll.fitToWidth=true;scroll.hbarPolicy=ScrollPane.ScrollBarPolicy.NEVER
        Label heading=new Label("Image features");heading.styleClass.add("panel-title")
        Label guidance=new Label("Choose filters for each scale, then inspect their effect on the image.");guidance.wrapText=true;guidance.styleClass.add("help")
        VBox header=new VBox(5d,heading,guidance);header.padding=new javafx.geometry.Insets(20,20,16,20)
        Label status=new Label();status.wrapText=true;status.id="feature-editor-status"
        status.managedProperty().bind(status.textProperty().isNotEmpty());status.visibleProperty().bind(status.managedProperty())
        Button cancel=new Button("Cancel"),apply=new Button("Apply image features")
        cancel.id="feature-editor-cancel";apply.id="feature-editor-apply"
        apply.defaultButton=true;cancel.cancelButton=true;apply.styleClass.add("primary")
        Region space=new Region();HBox.setHgrow(space,Priority.ALWAYS)
        HBox actions=new HBox(10d,space,cancel,apply)
        VBox footer=new VBox(8d,status,actions);footer.styleClass.add("actions")
        BorderPane layout=new BorderPane(scroll);layout.top=header;layout.bottom=footer;styleWorkbench(layout)
        stage.scene=new Scene(layout,960d,800d);stage.minWidth=780d;stage.minHeight=540d
        Closure read={->
            List groups=scales.entries().findAll {!it.filters.isEmpty()}.collect {row ->
                double sigma
                try {sigma=Double.parseDouble(row.field.text.trim())}
                catch(NumberFormatException error){throw new IllegalArgumentException("Enter a positive Gaussian σ for every scale with selected filters.")}
                [sigma_um:sigma,filters:new ArrayList(row.filters)]
            }
            // The fitter validates the same groups and calculates the same feature count.
            org.mif.classification.PixelFeatureOps.create(indices.collect{it as int} as int[],intensity.selected,
                org.mif.classification.PixelFeatureOps.scales(groups),sampling[0],sampling[1])
            [include_intensity:intensity.selected,scales:groups]
        }
        ChangeListener imageChanged
        Closure close={->
            if(state.closed)return
            state.closed=true;inspection.close();worker.shutdownNow()
            gui.viewer.imageDataProperty().removeListener(imageChanged)
            stage.hide()
            if(!state.applied)onCancel?.call()
        }
        imageChanged={o,a,b->if(b!=data)close()} as ChangeListener
        gui.viewer.imageDataProperty().addListener(imageChanged)
        cancel.setOnAction({event->close()})
        stage.setOnCloseRequest({event->event.consume();close()})
        stage.setOnHidden({event->close()})
        apply.setOnAction({event->
            try {Map value=read();state.applied=true
                try {onApply.call(value)}catch(Exception error){state.applied=false;throw error}
                close()
            }
            catch(Exception error){status.text=error.message ?: error.class.simpleName}
        })
        stage.setOnShown({event->inspection.setActive(true)})
        stage.show()
        [stage:stage,state:state,inspection:inspection,close:close,read:read]
    }
}
return [open:PixelFeatureWorkbench.&open]
