// Named controls for QuPath's existing tools; never create or relabel an object.
import javafx.application.Platform
import javafx.beans.value.ChangeListener
import javafx.collections.ListChangeListener
import javafx.scene.Scene
import javafx.scene.control.ComboBox
import javafx.scene.control.Label
import javafx.scene.control.MenuItem
import javafx.scene.control.Button
import javafx.scene.layout.HBox
import javafx.scene.layout.VBox
import javafx.scene.layout.Priority
import javafx.stage.Stage
import javafx.stage.WindowEvent
import qupath.lib.gui.prefs.PathPrefs
import qupath.lib.gui.viewer.tools.PathTools
import qupath.lib.objects.classes.PathClass
import java.util.function.Predicate

class MifAnnotationTools {
    private static boolean correctionFontsLoaded=false
    private static String correctionRegularFamily=null
    private static String correctionSemiboldFamily=null
    /** JavaFX cannot select weights in the host's variable Ubuntu fonts. */
    static String correctionFontFamily() {
        if(!correctionFontsLoaded) {
            ["Regular","SemiBold"].each {weight->
                def stream=org.mif.cells.NativeCellScope.class.getResourceAsStream("/org/mif/fonts/ubuntu-sans/UbuntuSans-${weight}.ttf")
                if(stream!=null)try {
                    def loaded=javafx.scene.text.Font.loadFont(stream,15d)
                    if(weight=="Regular")correctionRegularFamily=loaded?.family
                    else correctionSemiboldFamily=loaded?.family
                }finally {stream.close()}
            }
            correctionFontsLoaded=true
        }
        List preferences=System.getProperty("os.name","").startsWith("Windows") ? ["Segoe UI","Ubuntu Sans","DejaVu Sans"] : ["Ubuntu Sans","Ubuntu","DejaVu Sans"]
        return preferences.find {it==correctionRegularFamily || javafx.scene.text.Font.families.contains(it)} ?: javafx.scene.text.Font.default.family
    }

    /** One native project, ordered image navigation, and the existing per-image editor. */
    static Map openCorrectionBatch(gui,Map manifest,Map correctionHelper) {
        if(manifest.schema_version!="local-cell-corrections/v1" || !(manifest.images instanceof List) || manifest.images.empty)
            throw new IllegalArgumentException("Reopen the collected corrections from the application.")
        List images=manifest.images
        if(images.collect {it.entry_id}.toSet().size()!=images.size() ||
                images.any {image->!gui.project.imageList.any {it.ID.toString()==image.entry_id}})
            throw new IllegalArgumentException("The correction draft images changed. Reopen the collected corrections.")
        Stage window=new Stage();window.initOwner(gui.stage)
        ComboBox<String> imageList=new ComboBox<>();imageList.id="correction-images"
        imageList.accessibleText="Images with collected corrections";imageList.maxWidth=Double.MAX_VALUE
        imageList.minWidth=80d;imageList.prefWidth=220d
        imageList.items.addAll(images.collect {(it.display_name ?: it.ticket.image_name) as String})
        imageList.style="-fx-background-color:white;-fx-mark-color:#006b70;-fx-border-color:-mif-correction-border;-fx-padding:2 6;-fx-border-radius:4;-fx-background-radius:4;"
        Button previous=new Button("‹"),next=new Button("›")
        previous.id="correction-previous-image";next.id="correction-next-image"
        [previous,next].each {it.style="-fx-background-color:white;-fx-text-fill:#294c56;-fx-border-color:-mif-correction-border;-fx-padding:5 9;-fx-border-radius:4;-fx-background-radius:4;"}
        previous.tooltip=new javafx.scene.control.Tooltip("Previous image");next.tooltip=new javafx.scene.control.Tooltip("Next image")
        Label position=new Label();position.style="-fx-text-fill:#486169;-fx-font-size:15px;"
        position.minWidth=javafx.scene.layout.Region.USE_PREF_SIZE
        HBox navigator=new HBox(6d,previous,imageList,next,position);navigator.alignment=javafx.geometry.Pos.CENTER_LEFT
        navigator.id="correction-image-navigation"
        HBox.setHgrow(imageList,Priority.ALWAYS)
        Map state=[current:null,index:-1,switching:false,closed:false,sessions:[:]]
        Closure close=null
        Closure saveCurrent={->state.current?.save?.call()}
        Closure finish={->
            saveCurrent()
            images.each {image->
                def entry=gui.project.imageList.find {it.ID.toString()==image.entry_id}
                def data=gui.project.getEntry(gui.imageData)==entry?gui.imageData:entry.readImageData()
                try {validateCorrectionOutlines(data,image.ticket)}
                catch(IllegalArgumentException error){throw new IllegalArgumentException(image.ticket.image_name+": "+error.message)}
                finally {if(!data.is(gui.imageData))data.server.close()}
            }
            gui.project.syncChanges();gui.stage.hide()
        }
        Closure selectImage
        selectImage={int index->
            if(state.closed || state.switching || index<0 || index==state.index)return
            state.switching=true
            int previousIndex=state.index
            try {
                saveCurrent()
                state.current?.close?.call(false)
                Map image=images[index]
                Map session=state.sessions.computeIfAbsent(image.entry_id,{key->[:]} as java.util.function.Function)
                state.current=openCorrections(gui,image.ticket,correctionHelper,
                    [entry_id:image.entry_id,stage:window,navigator:navigator,session:session,onFinish:finish,
                     onHidden:{->close.call()}])
                if(state.current==null)throw new IllegalArgumentException("The selected image could not be opened.")
                state.index=index;imageList.selectionModel.select(index)
                position.text="${index+1} / ${images.size()}"
                previous.disable=index==0;next.disable=index==images.size()-1
            } catch(Throwable error) {
                imageList.selectionModel.select(previousIndex)
                throw error
            } finally {state.switching=false}
        }
        imageList.selectionModel.selectedIndexProperty().addListener({obs,old,value->
            if(!state.switching)try {selectImage(value as int)}catch(Exception error){qupath.fx.dialogs.Dialogs.showErrorMessage("Cell corrections",error.message)}
        } as ChangeListener)
        previous.setOnAction({event->imageList.selectionModel.select(state.index-1)} as javafx.event.EventHandler)
        next.setOnAction({event->imageList.selectionModel.select(state.index+1)} as javafx.event.EventHandler)
        ChangeListener imageChanged={obs,old,value->
            if(state.switching || state.closed || value==null)return
            def entry=gui.project.getEntry(value);int index=images.findIndexOf {it.entry_id==entry?.ID?.toString()}
            if(index>=0)selectImage(index)
        } as ChangeListener
        gui.imageDataProperty().addListener(imageChanged)
        close={->
            if(state.closed)return
            saveCurrent();state.closed=true
            gui.imageDataProperty().removeListener(imageChanged);state.current?.close?.call()
        }
        selectImage(0)
        return [stage:window,resourcesClosed:true,close:close,images:imageList,previousImage:previous,
            nextImage:next,current:{->state.current},selectImage:selectImage]
    }

    static void validateCorrectionOutlines(data,Map ticket) {
        if(ticket.cell_correction.operations.any {operation->
            if(operation.operation=="delete")return false
            String task=UUID.nameUUIDFromBytes((operation.cell_id+"|"+operation.operation).getBytes(java.nio.charset.StandardCharsets.UTF_8)).toString()
            return operation.operation in ["split","merge","add_cell"] ?
                !data.hierarchy.annotationObjects.any {it.metadata.get("mif.correction_task")==task && it.metadata.get("mif.correction_role")=="cell"} :
                !data.hierarchy.annotationObjects.any {it.ID.toString()==task}
        })throw new IllegalArgumentException("A queued outline is missing. Draw the requested outline or use Undo to restore it.")
    }

    /** A persistent navigator and named tools for the exact queued draft outlines. */
    static Map openCorrections(gui,Map ticket,Map correctionHelper=null,Map options=[:]) {
        if(!Platform.isFxApplicationThread())throw new IllegalStateException("Open corrections on the JavaFX thread.")
        Map scope=ticket.cell_correction
        if(!(scope instanceof Map) || !(scope.operations instanceof List) || scope.operations.isEmpty())
            throw new IllegalArgumentException("Reopen the selected cell corrections from the application.")
        def entry=gui.project.imageList.find {it.ID.toString()==(options.entry_id ?: "1")}
        if(entry==null)throw new IllegalArgumentException("The correction draft image is unavailable.")
        if((gui.imageData==null || gui.project.getEntry(gui.imageData)!=entry) && !gui.openImageEntry(entry))return null
        def data=gui.imageData,viewer=gui.viewer,hierarchy=data.hierarchy
        boolean identityTasks=scope.operations.any {it.operation in ["split","merge","add_cell","add_nucleus"]}
        if(identityTasks && correctionHelper==null)throw new IllegalArgumentException("Reopen the correction editor with its registered geometry tools.")
        Map session=options.containsKey("session") ? options.session : [:]
        List rows=session.rows ?: [];Map originals=session.originals ?: [:]
        if(!session.containsKey("rows"))scope.operations.each {operation ->
            String id=UUID.nameUUIDFromBytes((operation.cell_id+"|"+operation.operation).getBytes(java.nio.charset.StandardCharsets.UTF_8)).toString()
            boolean newIdentity=operation.operation in ["split","merge","add_cell"]
            def candidates=newIdentity ? hierarchy.annotationObjects.findAll {it.metadata.get("mif.correction_task")==id} : hierarchy.annotationObjects.findAll {it.ID.toString()==id}
            def outline=newIdentity ? candidates.find {it.metadata.get("mif.correction_role")=="cell"} : candidates.find()
            if((outline==null || outline.ROI==null || !outline.ROI.isArea()) && !(operation.operation in ["add_cell","add_nucleus","delete"]))
                throw new IllegalArgumentException("A queued outline is missing. Reopen the correction draft.")
            Map names=[split:"Split merged cell",merge:"Merge cell fragments",add_cell:"Add missed cell",add_nucleus:"Add missing nucleus",delete:"Remove cell"]
            def locationROI=outline?.ROI
            rows.add([id:outline?.ID?.toString() ?: id,task:id,cell_id:operation.cell_id,operation:operation.operation,request:operation,
                name:names[operation.operation] ?: outline.name,newIdentity:newIdentity,startingROI:outline?.ROI,
                x_um:locationROI==null?null:locationROI.centroidX*data.server.pixelCalibration.pixelWidthMicrons,
                y_um:locationROI==null?null:locationROI.centroidY*data.server.pixelCalibration.pixelHeightMicrons,
                starting:newIdentity?correctionHelper.exportObjects.call(candidates):null])
            if(outline!=null)originals[rows.last().id]=outline.ROI
        }
        session.rows=rows;session.originals=originals
        Stage window=options.stage ?: new Stage();if(options.stage==null)window.initOwner(gui.stage);window.title="Edit collected outlines"
        // Match the application queue's font, colours and two-line row hierarchy.
        String fontFamily=correctionFontFamily()
        // The static 600 face has its own legacy family; JavaFX calls its style Regular.
        String headingFamily=fontFamily==correctionRegularFamily && correctionSemiboldFamily!=null ? correctionSemiboldFamily :
            (["${fontFamily} SemiBold","${fontFamily} Semibold"].find {
                javafx.scene.text.Font.families.contains(it.toString())
            } ?: fontFamily)
        String emphasis="-fx-font-family:'${headingFamily}';-fx-font-weight:600;"
        Closure displayName={String name->name?.replaceFirst(/\s*·\s*\d+$/,"") ?: "Outline"}
        Map queueNames=[cell_outline:"Cell outline",nucleus_outline:"Nuclear outline",split:"Split cell",merge:"Merge cells",
            add_cell:"New cell",add_nucleus:"New nuclear outline",delete:"Delete cell"]
        Map taskTitles=[cell_outline:"Reshape cell outline",nucleus_outline:"Reshape nuclear outline",split:"Split cell",
            merge:"Refine merged outline",add_cell:"Draw missing cell",add_nucleus:"Draw missing nucleus",delete:"Remove cell"]
        Map rowStates=[:]
        String secondary="-fx-background-color:white;-fx-text-fill:#294c56;-fx-border-color:-mif-correction-border;-fx-padding:6 10;-fx-border-radius:4;-fx-background-radius:4;"
        String taskApply="-fx-background-color:#dcece6;-fx-text-fill:#005d62;-fx-border-color:-mif-correction-active-border;-fx-padding:6 10;-fx-border-radius:4;-fx-background-radius:4;"
        String primary="-fx-background-color:#006b70;-fx-text-fill:white;-fx-border-color:-mif-correction-primary-border;-fx-border-width:1;-fx-border-radius:4;-fx-padding:7 12;-fx-background-radius:4;"
        String muted="-fx-text-fill:#486169;-fx-font-size:15px;"
        Label heading=new Label("Edit collected outlines");heading.id="correction-page-heading";heading.style=emphasis+"-fx-font-size:20px;-fx-text-fill:#172d35;"
        heading.wrapText=true
        Label summary=new Label("Collected corrections");summary.style=emphasis+"-fx-font-size:16px;-fx-text-fill:#172d35;"
        summary.maxWidth=Double.MAX_VALUE
        def list=new javafx.scene.control.ListView<String>();list.id="correction-outlines";list.accessibleText="Collected cell corrections, including removals"
        rows.eachWithIndex {row,index->list.items.add(String.format(java.util.Locale.ROOT,"%d. %s",index+1,row.name))}
        list.setCellFactory({view->
            def cell=new javafx.scene.control.ListCell<String>() {
                @Override protected void updateItem(String value,boolean empty) {
                    super.updateItem(value,empty);setText(null)
                    if(empty)setGraphic(null)
                    else properties.get("renderCorrectionRow")?.call()
                }
            }
            Label badge=new Label();badge.minWidth=24d;badge.prefWidth=24d;badge.maxWidth=24d
            badge.minHeight=24d;badge.prefHeight=24d;badge.maxHeight=24d;badge.alignment=javafx.geometry.Pos.CENTER
            def badgeSlot=new javafx.scene.layout.StackPane(badge);badgeSlot.minWidth=32d;badgeSlot.prefWidth=32d;badgeSlot.maxWidth=32d
            Label taskName=new Label(),location=new Label();taskName.maxWidth=Double.MAX_VALUE;taskName.minWidth=0d
            location.maxWidth=Double.MAX_VALUE;location.minWidth=0d
            Label taskState=new Label();taskState.minWidth=javafx.scene.layout.Region.USE_PREF_SIZE
            HBox titleLine=new HBox(8d,taskName,taskState);titleLine.alignment=javafx.geometry.Pos.CENTER_LEFT
            HBox.setHgrow(taskName,Priority.ALWAYS)
            VBox labels=new VBox(2d,titleLine,location);labels.alignment=javafx.geometry.Pos.CENTER_LEFT;labels.minWidth=0d
            HBox rowGraphic=new HBox(10d,badgeSlot,labels);rowGraphic.alignment=javafx.geometry.Pos.CENTER_LEFT
            rowGraphic.maxWidth=Double.MAX_VALUE;HBox.setHgrow(labels,Priority.ALWAYS)
            Closure render={->
                if(cell.empty || cell.index<0 || cell.index>=rows.size()){cell.graphic=null;return}
                Map row=rows[cell.index];boolean selected=cell.selected
                String ink=selected?"white":"#172d35"
                badge.text=(cell.index+1).toString();taskName.text=queueNames[row.operation] ?: displayName(row.name)
                location.text=row.x_um!=null ? String.format(java.util.Locale.ROOT,"X %.1f · Y %.1f µm",row.x_um,row.y_um) :
                    row.operation in ["delete","add_nucleus"]?(row.locationUnavailable?"Location unavailable":"Locating cell…"):"Draw in the current field"
                String progress=rowStates[row.task] ?: "Ready"
                taskState.text=progress in ["Edited","Draw","Remove"]?progress:""
                cell.style="-fx-background-color:"+(selected?"#006b70":cell.hover?"#e4eeec":"transparent")+";-fx-padding:5 6;-fx-border-width:0;-fx-background-radius:4;"
                badge.style="-fx-text-fill:${ink};-fx-font-size:15px;-fx-border-color:${ink};-fx-border-width:1.2;-fx-border-radius:12;-fx-background-radius:12;"
                taskName.style=emphasis+"-fx-font-size:15px;-fx-text-fill:${ink};"
                location.style="-fx-font-size:15px;-fx-text-fill:"+(selected?"white":"#486169")+";"
                taskState.style="-fx-font-size:15px;-fx-text-fill:"+(selected?"#d8eeec":"#486169")+";"
                cell.accessibleText="${cell.index+1}. ${taskName.text}. ${location.text}"+(taskState.text?". ${taskState.text}":"")
                cell.graphic=rowGraphic
            }
            cell.properties.put("renderCorrectionRow",render)
            cell.selectedProperty().addListener({obs,old,value->render()} as ChangeListener)
            cell.hoverProperty().addListener({obs,old,value->render()} as ChangeListener)
            cell.indexProperty().addListener({obs,old,value->render()} as ChangeListener)
            cell
        } as javafx.util.Callback)
        list.style="-fx-background-color:#f2f6f5;-fx-control-inner-background:#f2f6f5;-fx-border-color:-mif-correction-queue-border;-fx-border-radius:4;-fx-background-radius:4;-fx-padding:3;-fx-background-insets:0;"
        list.fixedCellSize=48d;list.prefHeight=Math.min(104d,rows.size()*48d+8d)
        list.minHeight=Math.min(104d,list.prefHeight);list.maxHeight=rows.size()*48d+8d
        Button previous=new Button("‹"),next=new Button("›")
        previous.tooltip=new javafx.scene.control.Tooltip("Previous correction");next.tooltip=new javafx.scene.control.Tooltip("Next correction")
        previous.id="correction-previous-task";next.id="correction-next-task"
        previous.style=secondary+"-fx-padding:1 8;";next.style=previous.style
        Label position=new Label();position.style=muted;position.minWidth=42d;position.alignment=javafx.geometry.Pos.CENTER
        HBox navigation=new HBox(6d,summary,previous,position,next);navigation.alignment=javafx.geometry.Pos.CENTER_LEFT;HBox.setHgrow(summary,Priority.ALWAYS)
        Button move=new Button("Move vertices"),brush=new Button("Brush")
        Button drawCell=new Button("Draw cell"),drawNucleus=new Button("Draw nucleus"),useDrawn=new Button("Apply outline")
        Button cutline=new Button("Cutline"),split=new Button("Split at cutline")
        move.id="correction-move";brush.id="correction-brush"
        drawCell.id="correction-draw-cell";drawNucleus.id="correction-draw-nucleus";useDrawn.id="correction-use-drawn"
        cutline.id="correction-cutline";split.id="correction-split"
        [move,brush,drawCell,drawNucleus,cutline,split,useDrawn].each {button->
            button.maxWidth=Double.MAX_VALUE;button.minWidth=javafx.scene.layout.Region.USE_PREF_SIZE
            HBox.setHgrow(button,Priority.ALWAYS);button.style=secondary
        }
        HBox tools=new HBox(6d,move,brush,cutline,drawCell);tools.id="correction-tools"
        def size=new javafx.scene.control.Spinner<Integer>(1,200,PathPrefs.brushDiameterProperty().get())
        size.editable=true;size.prefWidth=82d;size.id="correction-brush-size";size.accessibleText="Brush diameter in screen pixels"
        size.style="-fx-background-color:white;-fx-border-color:-mif-correction-input-border;-fx-border-radius:4;-fx-background-radius:4;"
        size.editor.style="-fx-background-color:white;-fx-border-width:0;-fx-padding:4 6;-fx-text-fill:#294c56;"
        size.editor.alignment=javafx.geometry.Pos.CENTER_RIGHT
        Closure styleBrushSpinner={->
            ["increment","decrement"].each {direction->
                def button=size.lookup(".${direction}-arrow-button"),arrow=size.lookup(".${direction}-arrow")
                if(button!=null)button.style="-fx-background-color:transparent;-fx-border-width:0;-fx-padding:3 8;-fx-background-insets:0;"
                if(arrow!=null)arrow.style="-fx-background-color:#006b70;-fx-padding:0;-fx-pref-width:8;-fx-pref-height:5;-fx-min-width:8;-fx-min-height:5;-fx-max-width:8;-fx-max-height:5;-fx-shape:'"+
                    (direction=="increment"?"M 0 4 L 4 0 L 8 4 L 7 5 L 4 2 L 1 5 Z":"M 0 1 L 1 0 L 4 3 L 7 0 L 8 1 L 4 5 Z")+"';"
            }
        }
        size.skinProperty().addListener({obs,old,value->Platform.runLater({styleBrushSpinner()} as Runnable)} as ChangeListener)
        Label sizeLabel=new Label("Brush size");sizeLabel.labelFor=size;sizeLabel.style=emphasis+"-fx-text-fill:#294c56;"
        Label units=new Label("screen px");units.style=muted
        HBox brushSize=new HBox(8d,sizeLabel,size,units);brushSize.alignment=javafx.geometry.Pos.CENTER_LEFT
        Button undo=new Button("Undo"),redo=new Button("Redo"),reset=new Button("Reset outline")
        undo.id="correction-undo";redo.id="correction-redo";reset.id="correction-reset"
        [undo,redo,reset].each {it.style=secondary+"-fx-padding:4 9;-fx-font-size:15px;"}
        undo.tooltip=new javafx.scene.control.Tooltip("Undo the last edit (Ctrl+Z)")
        redo.tooltip=new javafx.scene.control.Tooltip("Redo the last undone edit")
        reset.tooltip=new javafx.scene.control.Tooltip("Restore this outline to its shape when this editing session opened.")
        move.tooltip=new javafx.scene.control.Tooltip("Move the selected outline or edit its vertices (M)")
        brush.tooltip=new javafx.scene.control.Tooltip("Paint the selected outline (B); hold Alt to erase.")
        cutline.tooltip=new javafx.scene.control.Tooltip("Draw a line across the selected cell, then split it.")
        useDrawn.tooltip=new javafx.scene.control.Tooltip("Attach the drawn outline to this correction. Review and acceptance follow when you return.")
        undo.disableProperty().bind(gui.undoRedoManager.canUndo().not());redo.disableProperty().bind(gui.undoRedoManager.canRedo().not())
        def historySpace=new javafx.scene.layout.Region();HBox.setHgrow(historySpace,Priority.ALWAYS)
        Button applyOutline=new Button("Apply");applyOutline.id="correction-apply"
        applyOutline.style=taskApply+"-fx-padding:4 9;-fx-font-size:15px;"
        applyOutline.minWidth=javafx.scene.layout.Region.USE_PREF_SIZE
        applyOutline.tooltip=new javafx.scene.control.Tooltip("Apply the completed cutline or drawn outline to this correction.")
        HBox history=new HBox(6d,undo,redo,historySpace,reset,applyOutline);history.alignment=javafx.geometry.Pos.CENTER_LEFT
        history.id="correction-outline-actions"
        def starting=new javafx.scene.control.CheckBox("Show starting outline")
        starting.id="correction-starting-outline";starting.selected=true;starting.style=muted;starting.maxWidth=Double.MAX_VALUE
        def startingSwatch=new javafx.scene.shape.Line(0d,0d,26d,0d)
        startingSwatch.stroke=javafx.scene.paint.Color.web("#b06fae");startingSwatch.strokeWidth=1.5d;startingSwatch.strokeDashArray.addAll(5d,4d)
        HBox startingRow=new HBox(8d,startingSwatch,starting);startingRow.alignment=javafx.geometry.Pos.CENTER_LEFT
        startingRow.id="correction-starting-outline-legend"
        Label help=new Label();help.id="correction-tool-help";help.wrapText=true;help.style=muted
        Label status=new Label();status.wrapText=true;status.style=muted
        Button finish=new Button("Return for review");finish.id="correction-return";finish.minWidth=javafx.scene.layout.Region.USE_PREF_SIZE
        finish.style=primary+"-fx-padding:7 13;"
        Label editHeading=new Label();editHeading.id="correction-task-heading";editHeading.wrapText=true
        editHeading.style=emphasis+"-fx-font-size:16px;-fx-text-fill:#172d35;"
        VBox taskHeading=new VBox(editHeading)
        taskHeading.style="-fx-padding:0 0 4;"
        Label removalNote=new Label("This cell will be removed when you accept the corrections.")
        removalNote.wrapText=true;removalNote.style=muted+"-fx-padding:2 0;"
        removalNote.minHeight=javafx.scene.layout.Region.USE_PREF_SIZE
        ComboBox<String> nucleusMode=new ComboBox<>();nucleusMode.items.addAll("Retain source nuclei","Redraw nuclei","No nuclei");nucleusMode.id="correction-nucleus-mode";nucleusMode.maxWidth=Double.MAX_VALUE
        nucleusMode.style="-fx-background-color:white;-fx-mark-color:#006b70;-fx-border-color:-mif-correction-border;-fx-padding:2 3;-fx-border-radius:4;-fx-background-radius:4;"
        Label nucleusLabel=new Label("Nuclei");nucleusLabel.labelFor=nucleusMode;nucleusLabel.style=emphasis+"-fx-text-fill:#294c56;"
        nucleusLabel.minWidth=javafx.scene.layout.Region.USE_PREF_SIZE
        HBox nuclei=new HBox(10d,nucleusLabel,nucleusMode);nuclei.alignment=javafx.geometry.Pos.CENTER_LEFT;HBox.setHgrow(nucleusMode,Priority.ALWAYS)
        Label association=new Label("Select a result cell to draw its nucleus.");association.wrapText=true;association.style=muted
        HBox drawingActions=new HBox(drawNucleus)
        VBox nucleiTools=new VBox(8d,nuclei,drawingActions,association)
        nucleiTools.style="-fx-border-color:#dce4e4 transparent transparent transparent;-fx-border-width:1 0 0 0;-fx-padding:10 0 0;"
        VBox toolContext=new VBox(8d,brushSize,help)
        VBox boundaryTools=new VBox(10d,tools,toolContext)
        VBox historyTools=new VBox(12d,startingRow,history)
        historyTools.style="-fx-border-color:#dce4e4 transparent transparent transparent;-fx-border-width:1 0 0 0;-fx-padding:12 0 0;"
        historyTools.minHeight=javafx.scene.layout.Region.USE_PREF_SIZE
        VBox editing=new VBox(10d,boundaryTools,nucleiTools)
        def editorBody=editing
        VBox selectedTask=new VBox(8d,editorBody)
        selectedTask.minHeight=javafx.scene.layout.Region.USE_PREF_SIZE
        selectedTask.style="-fx-padding:2 0 0;"
        def editorScroll=new javafx.scene.control.ScrollPane(selectedTask)
        editorScroll.fitToWidth=true;editorScroll.hbarPolicy=javafx.scene.control.ScrollPane.ScrollBarPolicy.NEVER
        editorScroll.minHeight=40d;editorScroll.style="-fx-background-color:transparent;-fx-background:transparent;-fx-padding:0;"
        editorScroll.visibleProperty().bind(editorBody.visibleProperty())
        editorScroll.managedProperty().bind(editorScroll.visibleProperty())
        HBox footer=new HBox(12d,status,finish);footer.id="correction-session-actions"
        footer.alignment=javafx.geometry.Pos.CENTER_LEFT;HBox.setHgrow(status,Priority.ALWAYS)
        status.maxWidth=Double.MAX_VALUE;status.minWidth=0d
        footer.minHeight=javafx.scene.layout.Region.USE_PREF_SIZE
        status.minHeight=javafx.scene.layout.Region.USE_PREF_SIZE
        footer.styleClass.add("actions")
        VBox taskArea=new VBox(10d,taskHeading,removalNote,editorScroll,historyTools)
        taskArea.id="correction-selected-task"
        taskArea.maxHeight=Double.MAX_VALUE
        taskArea.style="-fx-background-color:#f3f5f5;-fx-border-color:#dce4e4 transparent transparent transparent;-fx-border-width:1 0 0 0;-fx-padding:18 20;"
        VBox queueArea=new VBox(8d,navigation,list);queueArea.id="correction-queue"
        if(options.navigator!=null)queueArea.children.add(0,options.navigator)
        queueArea.style="-fx-padding:0;"
        queueArea.maxHeight=javafx.scene.layout.Region.USE_PREF_SIZE
        VBox.setVgrow(list,Priority.ALWAYS)
        VBox collection=new VBox(12d,heading,queueArea)
        collection.style="-fx-background-color:#f3f5f5;-fx-padding:20 20 16;"
        collection.minHeight=javafx.scene.layout.Region.USE_PREF_SIZE
        VBox content=new VBox(collection,taskArea,footer)
        styleDrawingPanel(content)
        content.styleClass.add("correction-workspace")
        String focusCss="""
            .correction-workspace {
                -fx-background-color:#f3f5f5;
                -mif-correction-border:#bdcccc; -mif-correction-active-border:#a6c5b9;
                -mif-correction-input-border:#becfce; -mif-correction-queue-border:#dce4e4;
                -mif-correction-primary-border:transparent;
            }
            .correction-workspace .button:focused, .correction-workspace .combo-box:focused,
            .correction-workspace .list-view:focused, .correction-workspace .spinner:contains-focus {
                -mif-correction-border:#006b70; -mif-correction-active-border:#006b70;
                -mif-correction-input-border:#006b70; -mif-correction-queue-border:#006b70;
                -mif-correction-primary-border:#82bcb3;
            }
        """
        content.stylesheets.add("data:text/css;base64,"+java.util.Base64.encoder.encodeToString(focusCss.getBytes(java.nio.charset.StandardCharsets.UTF_8)))
        VBox.setVgrow(editorScroll,Priority.ALWAYS)
        VBox.setVgrow(taskArea,Priority.ALWAYS)
        window.scene=new Scene(content,460,Math.max(590d,Math.min(650d,gui.stage.height-160d)));window.minWidth=430;window.minHeight=560
        window.scene.heightProperty().addListener({obs,old,value->
            list.prefHeight=Math.min(((value as double)<720d?2d:3d)*48d+8d,rows.size()*48d+8d)
        } as ChangeListener)
        Map state=[closed:false,selecting:false]
        // Saved cell locations are table reads and must stay off the JavaFX thread.
        List locationReads=[]
        rows.findAll {it.x_um==null && it.operation in ["add_nucleus","delete"]}.each {row->
            def read=java.util.concurrent.CompletableFuture.supplyAsync({->
                def source=org.mif.cells.NativeCellScope.open(data,scope.section_id as String).join().lookup(row.cell_id as String)
                if(source==null)return null
                [source.ROI.centroidX*data.server.pixelCalibration.pixelWidthMicrons,
                 source.ROI.centroidY*data.server.pixelCalibration.pixelHeightMicrons]
            } as java.util.function.Supplier)
            locationReads<<read
            read.whenComplete({location,error->Platform.runLater({
                if(state.closed)return
                if(error==null && location!=null){row.x_um=location[0];row.y_um=location[1]}
                else row.locationUnavailable=true
                list.refresh()
            } as Runnable)} as java.util.function.BiConsumer)
        }
        Closure refreshTool={->
            if(state.closed)return
            boolean isBrush=gui.toolManager.selectedTool==PathTools.BRUSH,isMove=gui.toolManager.selectedTool==PathTools.MOVE
            boolean isCutline=gui.toolManager.selectedTool==PathTools.POLYLINE
            boolean isDrawing=gui.toolManager.selectedTool==PathTools.POLYGON && state.pending!=null
            String active="-fx-background-color:#dcece6;-fx-text-fill:#005d62;-fx-border-color:-mif-correction-active-border;-fx-padding:6 10;"
            List visibleTools=tools.children.findAll {it.managed}
            visibleTools.eachWithIndex {button,index->
                boolean selected=button.is(move)?isMove:button.is(brush)?isBrush:button.is(cutline)?isCutline:
                    isDrawing && state.pending.role==(button.is(drawCell)?"cell":"nucleus")
                button.style=(selected?active:secondary)+"-fx-font-weight:normal;-fx-background-radius:3;-fx-border-radius:3;"
            }
            if(drawNucleus.parent.is(drawingActions))drawNucleus.style=isDrawing && state.pending.role=="nucleus"?taskApply:secondary
            int index=list.selectionModel.selectedIndex
            Map row=index>=0?rows[index]:null
            // QuPath returns to Move after completing a line or polygon. Keep
            // the pending task's apply action reachable until it is used.
            boolean cutting=row?.operation=="split" && state.cutTask==row.task
            boolean drawing=state.pending?.task!=null && state.pending.task==row?.task
            help.text=isBrush?"Paint to add; hold Alt to erase.":cutting?(isCutline?"Draw across the cell. Double-click to finish.":"Use the cutline to split this cell."):
                drawing?(isDrawing?"Draw the outline. Double-click to finish.":"Apply the drawn outline to this correction."):
                isMove && move.managed?"Drag an outline or its vertices to refine it.":""
            help.visible=!help.text.empty;help.managed=help.visible
            brushSize.visible=isBrush;brushSize.managed=isBrush
            split.visible=cutting;split.managed=cutting
            useDrawn.visible=drawing;useDrawn.managed=drawing
            applyOutline.visible=cutting || drawing;applyOutline.managed=applyOutline.visible
            def candidate=hierarchy.selectionModel.selectedObject
            boolean completed=!(isDrawing || isCutline) && candidate?.isAnnotation() && candidate.ROI!=null
            boolean drawnReady=completed && drawing && candidate.ROI.isArea() &&
                !candidate.metadata.containsKey("mif.correction_task") && !rows.any {it.id==candidate.ID.toString()}
            boolean cutReady=completed && cutting && candidate.ROI.isLine() && candidate.ROI.allPoints.size()>=2
            applyOutline.disable=!(drawnReady || cutReady)
            applyOutline.accessibleText=cutReady?"Apply cutline":"Apply drawn outline"
            toolContext.visible=isBrush || cutting || drawing || (isMove && move.managed);toolContext.managed=toolContext.visible
        }
        ChangeListener toolChanged={obs,old,value->refreshTool()} as ChangeListener
        gui.toolManager.selectedToolProperty().addListener(toolChanged)
        boolean oldCreate=PathPrefs.brushCreateNewObjectsProperty().get(),oldScale=PathPrefs.brushScaleByMagProperty().get(),oldSelection=PathPrefs.selectionModeProperty().get()
        int oldDiameter=PathPrefs.brushDiameterProperty().get()
        PathPrefs.brushCreateNewObjectsProperty().set(false);PathPrefs.brushScaleByMagProperty().set(true);PathPrefs.selectionModeProperty().set(false)
        Closure current={->
            int index=list.selectionModel.selectedIndex
            if(index<0)return null
            Map row=rows[index];def selected=hierarchy.selectionModel.selectedObject
            if(row.newIdentity && selected?.metadata?.get("mif.correction_task")==row.task)return selected
            def found=hierarchy.annotationObjects.find {it.ID.toString()==row.id}
            if(found==null && row.newIdentity) {
                found=hierarchy.annotationObjects.find {it.metadata.get("mif.correction_task")==row.task && it.metadata.get("mif.correction_role")=="cell"}
                if(found!=null)row.id=found.ID.toString()
            }
            return found
        }
        Closure taskObjects={Map row->hierarchy.annotationObjects.findAll {row.newIdentity ? it.metadata.get("mif.correction_task")==row.task : it.ID.toString()==row.id}}
        Closure taskCell={Map row->
            def selected=current()
            if(!row.newIdentity)return selected
            String result=selected?.metadata?.get("mif.correction_result_id")
            return taskObjects(row).find {it.metadata.get("mif.correction_role")=="cell" && (result==null || it.ID.toString()==result)}
        }
        Closure update={->
            if(state.closed)return
            int changed=rows.count {row->
                if(row.operation=="delete")return false
                def object=hierarchy.annotationObjects.find {it.ID.toString()==row.id}
                if(object==null)return originals[row.id]!=null || row.startingROI!=null
                originals[row.id]==null || !object.ROI.geometry.equalsNorm(originals[row.id].geometry) ||
                    (row.newIdentity && taskObjects(row).count {it.metadata.get("mif.correction_role")=="cell"}>1)
            }
            rows.each {row->
                def object=hierarchy.annotationObjects.find {it.ID.toString()==row.id}
                boolean altered=object!=null && (originals[row.id]==null || !object.ROI.geometry.equalsNorm(originals[row.id].geometry) ||
                    (row.newIdentity && taskObjects(row).count {it.metadata.get("mif.correction_role")=="cell"}>1))
                rowStates[row.task]=row.operation=="delete"?"Remove":object==null && row.operation in ["add_cell","add_nucleus"]?"Draw":altered?"Edited":"Ready"
            }
            list.refresh()
            status.text=changed==0?"No outline changes yet":"${changed} outline"+(changed==1?"":"s")+" changed"
            reset.disable=current()==null && !(list.selectionModel.selectedIndex>=0 && rows[list.selectionModel.selectedIndex].newIdentity)
            if(list.selectionModel.selectedIndex>=0) {
                Map row=rows[list.selectionModel.selectedIndex]
                editHeading.text=taskTitles[row.operation] ?: displayName(row.name)
                taskArea.accessibleText=taskTitles[row.operation] ?: displayName(row.name)
                removalNote.visible=row.operation=="delete";removalNote.managed=row.operation=="delete"
                editorBody.visible=row.operation!="delete";editorBody.managed=row.operation!="delete"
                historyTools.visible=row.operation!="delete";historyTools.managed=row.operation!="delete"
                if(row.operation=="delete") {
                    status.text="Queued for removal · return to review"
                    gui.toolManager.setSelectedTool(PathTools.MOVE)
                }
                reset.text=row.newIdentity?"Reset task":"Reset outline"
                drawCell.disable=row.operation!="add_cell"
                drawNucleus.disable=!(row.newIdentity || row.operation=="add_nucleus") || scope.cell_boundary_source=="nuclear_outline"
                cutline.disable=row.operation!="split";split.disable=row.operation!="split"
                nucleusMode.disable=!row.newIdentity || scope.cell_boundary_source=="nuclear_outline"
                def selectedOutline=current()
                boolean hasOutline=selectedOutline!=null && hierarchy.annotationObjects.contains(selectedOutline)
                boolean drawNewCell=row.operation=="add_cell" && !hasOutline
                boolean redrawNuclei=taskCell(row)?.metadata?.get("mif.correction_nucleus_mode")=="redraw"
                boolean drawNuclear=!drawNucleus.disabled && !drawNewCell && (!row.newIdentity || redrawNuclei)
                def drawingParent=row.operation=="add_nucleus"?tools:drawingActions
                if(!drawNucleus.parent.is(drawingParent)) {
                    drawNucleus.parent?.children?.remove(drawNucleus)
                    drawingParent.children.add(drawNucleus)
                }
                [[node:drawCell,show:drawNewCell],[node:drawNucleus,show:drawNuclear],
                    [node:move,show:hasOutline],[node:brush,show:hasOutline],
                    [node:cutline,show:!cutline.disabled],[node:nuclei,show:!nucleusMode.disabled && hasOutline],
                    [node:drawingActions,show:row.newIdentity && drawNuclear],
                    [node:association,show:row.operation=="split" && drawNuclear && taskObjects(row).count {it.metadata.get("mif.correction_role")=="cell"}>1],
                    [node:startingRow,show:row.startingROI!=null],[node:nucleiTools,show:row.newIdentity && hasOutline && !nucleusMode.disabled]].each {item->
                    item.node.visible=item.show;item.node.managed=item.show
                }
                state.updatingMode=true
                try {
                    String mode=taskCell(row)?.metadata?.get("mif.correction_nucleus_mode")
                    nucleusMode.selectionModel.select(mode=="redraw"?1:mode=="none"?2:0)
                } finally {state.updatingMode=false}
                refreshTool()
            }
            viewer.repaintEntireImage()
        }
        Closure revealSelected={->
            int index=list.selectionModel.selectedIndex
            if(index<0)return
            def flow=list.lookup(".virtual-flow")
            if(flow instanceof javafx.scene.control.skin.VirtualFlow)flow.scrollTo(index)
            else list.scrollTo(index)
        }
        Closure focus={->
            if(state.closed || state.selecting)return
            revealSelected()
            def object=current()
            if(object==null) {
                Map row=rows[list.selectionModel.selectedIndex]
                if(row.operation=="delete") {
                    viewer.setSelectedObject(null);position.text="${list.selectionModel.selectedIndex+1} of ${rows.size()}";update();return
                }
                if(row.operation in ["add_cell","add_nucleus"]) {
                    viewer.setSelectedObject(null)
                    List bounds=scope.review_bounds_level0
                    if(bounds?.size()==4) {
                        viewer.setDownsampleFactor(Math.max(((bounds[2] as double)-(bounds[0] as double))/Math.max(1d,viewer.view.width),((bounds[3] as double)-(bounds[1] as double))/Math.max(1d,viewer.view.height)))
                        viewer.setCenterPixelLocation(((bounds[0] as double)+(bounds[2] as double))/2,((bounds[1] as double)+(bounds[3] as double))/2)
                    }
                    position.text="${list.selectionModel.selectedIndex+1} of ${rows.size()}"
                    update();return
                }
                status.text="This outline is missing. Use Undo to restore it.";return
            }
            state.selecting=true
            try {
                viewer.setSelectedObject(object)
                double px=data.server.pixelCalibration.pixelWidthMicrons,py=data.server.pixelCalibration.pixelHeightMicrons
                double width=Math.max(object.ROI.boundsWidth*2.5d,60d/px),height=Math.max(object.ROI.boundsHeight*2.5d,50d/py)
                viewer.setDownsampleFactor(Math.max(width/Math.max(1d,viewer.view.width),height/Math.max(1d,viewer.view.height)))
                viewer.setCenterPixelLocation(object.ROI.centroidX,object.ROI.centroidY)
                position.text="${list.selectionModel.selectedIndex+1} of ${rows.size()}"
            } finally {state.selecting=false}
            update()
        }
        list.heightProperty().addListener({obs,old,value->
            Platform.runLater({if(!state.closed)revealSelected()} as Runnable)
        } as ChangeListener)
        list.selectionModel.selectedIndexProperty().addListener({obs,old,value->focus()} as ChangeListener)
        previous.setOnAction({event->list.selectionModel.select(Math.floorMod(list.selectionModel.selectedIndex-1,rows.size()))} as javafx.event.EventHandler)
        next.setOnAction({event->list.selectionModel.select((list.selectionModel.selectedIndex+1)%rows.size())} as javafx.event.EventHandler)
        move.setOnAction({event->gui.toolManager.setSelectedTool(PathTools.MOVE)} as javafx.event.EventHandler)
        brush.setOnAction({event->focus();gui.toolManager.setSelectedTool(PathTools.BRUSH)} as javafx.event.EventHandler)
        ChangeListener sizeChanged={obs,old,value->PathPrefs.brushDiameterProperty().set(value as int)} as ChangeListener
        size.valueProperty().addListener(sizeChanged)
        undo.setOnAction({event->gui.undoRedoManager.undoOnce();focus()} as javafx.event.EventHandler)
        redo.setOnAction({event->gui.undoRedoManager.redoOnce();focus()} as javafx.event.EventHandler)
        reset.setOnAction({event->
            Map row=rows[list.selectionModel.selectedIndex]
            if(row.newIdentity) {
                hierarchy.removeObjects(taskObjects(row),true)
                def restored=qupath.lib.io.PathIO.readObjectsFromGeoJSON(new ByteArrayInputStream(new com.google.gson.Gson().toJson(row.starting).getBytes(java.nio.charset.StandardCharsets.UTF_8)))
                hierarchy.addObjects(restored);def first=restored.find {it.metadata.get("mif.correction_role")=="cell"}
                row.id=first?.ID?.toString() ?: row.task
                if(first!=null)originals[row.id]=first.ROI
                focus()
            } else {def object=current();if(object!=null && originals[object.ID.toString()]!=null){object.setROI(originals[object.ID.toString()]);hierarchy.fireObjectsChangedEvent(window,[object]);focus()}}
        } as javafx.event.EventHandler)
        Closure beginDrawing={String role->
            Map row=rows[list.selectionModel.selectedIndex]
            def target=taskCell(row)
            if(role=="nucleus" && row.newIdentity && target==null){status.text="Draw the cell outline before assigning its nucleus.";return}
            state.pending=[task:row.task,role:role,target:target?.ID?.toString()]
            viewer.setSelectedObject(null);PathPrefs.brushCreateNewObjectsProperty().set(true)
            gui.toolManager.setSelectedTool(PathTools.POLYGON)
            refreshTool();status.text="Drawing ${role=="cell" && scope.cell_boundary_source=="nuclear_outline"?"nuclear":role} outline"
        }
        drawCell.setOnAction({event->beginDrawing("cell")} as javafx.event.EventHandler)
        drawNucleus.setOnAction({event->beginDrawing("nucleus")} as javafx.event.EventHandler)
        useDrawn.setOnAction({event->
            try {
                Map row=rows[list.selectionModel.selectedIndex]
                def drawn=hierarchy.selectionModel.selectedObject
                if(state.pending?.task!=row.task || drawn==null || !drawn.isAnnotation() || !drawn.ROI?.isArea() ||
                        drawn.metadata.containsKey("mif.correction_task") || rows.any {it.id==drawn.ID.toString()})
                    throw new IllegalArgumentException("Draw and select a new area for this task first.")
                def result
                if(row.operation=="add_nucleus") {
                    result=qupath.lib.objects.PathObjects.createAnnotationObject(drawn.ROI)
                    result.setID(UUID.fromString(row.task));result.name="Nuclear outline · "+(scope.operations.findIndexOf {it==row.request}+1)
                    def previousNucleus=hierarchy.annotationObjects.find {it.ID.toString()==row.task}
                    if(previousNucleus!=null)hierarchy.removeObjects([previousNucleus],true)
                } else {
                    boolean cell=state.pending.role=="cell"
                    UUID resultID=cell?UUID.randomUUID():UUID.fromString(state.pending.target as String)
                    if(cell && taskObjects(row).any {it.metadata.get("mif.correction_role")=="cell"})
                        throw new IllegalArgumentException("This task already has a cell. Select and reshape it, or Reset this outline.")
                    result=correctionHelper.newCorrectionOutline.call(drawn.ROI,row.request,resultID,cell?"cell":"nucleus",cell?"none":"redraw")
                    if(!cell) {
                        def existing=taskObjects(row).findAll {it.metadata.get("mif.correction_role")=="nucleus" && it.metadata.get("mif.correction_result_id")==resultID.toString()}
                        hierarchy.removeObjects(existing,true)
                        taskObjects(row).findAll {it.metadata.get("mif.correction_role")=="cell"}.each {it.metadata.put("mif.correction_nucleus_mode","redraw")}
                    }
                    if(cell)row.id=resultID.toString()
                }
                hierarchy.removeObjects([drawn],true);hierarchy.addObjects([result])
                if(row.operation=="add_cell" && scope.cell_boundary_source=="nuclear_outline") {
                    result.metadata.put("mif.correction_nucleus_mode","redraw")
                    hierarchy.addObjects([correctionHelper.newCorrectionOutline.call(result.ROI,row.request,result.ID,"nucleus","redraw")])
                }
                state.pending=null;PathPrefs.brushCreateNewObjectsProperty().set(false)
                row.x_um=result.ROI.centroidX*data.server.pixelCalibration.pixelWidthMicrons
                row.y_um=result.ROI.centroidY*data.server.pixelCalibration.pixelHeightMicrons
                list.items.set(list.selectionModel.selectedIndex,String.format(java.util.Locale.ROOT,"%d. %s",list.selectionModel.selectedIndex+1,row.name))
                viewer.setSelectedObject(result);gui.toolManager.setSelectedTool(PathTools.MOVE);update()
            }catch(Exception error){status.text=error.message ?: error.class.simpleName}
        } as javafx.event.EventHandler)
        cutline.setOnAction({event->
            Map row=rows[list.selectionModel.selectedIndex];def target=taskCell(row)
            if(target==null){status.text="Select the cell outline to split.";return}
            state.cutTarget=target.ID.toString();state.cutTask=row.task;viewer.setSelectedObject(null)
            gui.toolManager.setSelectedTool(PathTools.POLYLINE)
            refreshTool();status.text="Drawing cutline"
        } as javafx.event.EventHandler)
        split.setOnAction({event->
            try {
                Map row=rows[list.selectionModel.selectedIndex]
                def line=hierarchy.selectionModel.selectedObject
                def target=taskObjects(row).find {it.ID.toString()==state.cutTarget}
                if(state.cutTask!=row.task || target==null || line==null || !line.isAnnotation() || !(line.ROI.geometry instanceof org.locationtech.jts.geom.Lineal))
                    throw new IllegalArgumentException("Draw and select a cutline across this task's chosen cell.")
                List pieces=correctionHelper.splitCorrectionOutline.call(target.ROI,line.ROI)
                String mode=target.metadata.get("mif.correction_nucleus_mode")
                def nucleus=taskObjects(row).find {it.metadata.get("mif.correction_role")=="nucleus" && it.metadata.get("mif.correction_result_id")==target.ID.toString()}
                List assignments=pieces.collect {[]}
                if(nucleus!=null && scope.cell_boundary_source!="nuclear_outline") {
                    for(int i=0;i<nucleus.ROI.geometry.numGeometries;i++) {
                        def part=nucleus.ROI.geometry.getGeometryN(i)
                        List owners=(0..<pieces.size()).findAll {pieces[it].geometry.covers(part)}
                        if(owners.size()!=1)throw new IllegalArgumentException("The cutline crosses a nucleus. Explicitly choose Redraw nuclei or No nuclei before splitting.")
                        assignments[owners.first()] << part
                    }
                }
                List added=[]
                pieces.eachWithIndex {piece,index->
                    UUID id=UUID.randomUUID();added<<correctionHelper.newCorrectionOutline.call(piece,row.request,id,"cell",mode)
                    if(scope.cell_boundary_source=="nuclear_outline")added<<correctionHelper.newCorrectionOutline.call(piece,row.request,id,"nucleus",mode)
                    else if(!assignments[index].empty)added<<correctionHelper.newCorrectionOutline.call(correctionHelper.exactCorrectionROI.call(
                        org.locationtech.jts.operation.union.UnaryUnionOp.union(assignments[index]),piece.imagePlane),row.request,id,"nucleus",mode)
                }
                hierarchy.removeObjects([target,line]+(nucleus==null ? [] : [nucleus]),true);hierarchy.addObjects(added)
                row.id=added.first().ID.toString();viewer.setSelectedObject(added.first());gui.toolManager.setSelectedTool(PathTools.MOVE)
                state.cutTarget=null;state.cutTask=null;update();status.text="${pieces.size()} resulting outlines. Select each cell to inspect its nuclear association."
            }catch(Exception error){status.text=error.message ?: error.class.simpleName}
        } as javafx.event.EventHandler)
        applyOutline.setOnAction({event->
            Map row=rows[list.selectionModel.selectedIndex]
            boolean selectedCutline=hierarchy.selectionModel.selectedObject?.ROI?.isLine() ?: false
            if(state.pending?.task==row.task && !(state.cutTask==row.task && selectedCutline))useDrawn.fire()
            else split.fire()
        } as javafx.event.EventHandler)
        nucleusMode.valueProperty().addListener({obs,old,value->
            if(state.updatingMode || list.selectionModel.selectedIndex<0)return
            Map row=rows[list.selectionModel.selectedIndex];if(!row.newIdentity)return
            String mode=nucleusMode.selectionModel.selectedIndex==1?"redraw":nucleusMode.selectionModel.selectedIndex==2?"none":"retain"
            if(mode!="retain")hierarchy.removeObjects(taskObjects(row).findAll {it.metadata.get("mif.correction_role")=="nucleus"},true)
            taskObjects(row).findAll {it.metadata.get("mif.correction_role")=="cell"}.each {it.metadata.put("mif.correction_nucleus_mode",mode)}
            hierarchy.fireHierarchyChangedEvent(window)
            status.text=mode=="redraw"?"Source nuclei removed from this proposal. Draw and assign the observed nuclei to the resulting cells.":mode=="none"?"This proposal explicitly has no nuclei.":"Retain requires every source nucleus unchanged. Reset the task if those outlines were removed."
        } as ChangeListener)
        def selectionListener={selected,old,all->
            if(!state.selecting && selected!=null){int index=rows.findIndexOf {it.id==selected.ID.toString() || (it.newIdentity && it.task==selected.metadata.get("mif.correction_task"))};if(index>=0){state.selecting=true;list.selectionModel.select(index);list.scrollTo(index);state.selecting=false;position.text="${index+1} of ${rows.size()}";update()}}
            refreshTool()
        } as qupath.lib.objects.hierarchy.events.PathObjectSelectionListener
        hierarchy.selectionModel.addPathObjectSelectionListener(selectionListener)
        def hierarchyListener={event->if(!event.isChanging())Platform.runLater({update()} as Runnable)} as qupath.lib.objects.hierarchy.events.PathObjectHierarchyListener
        hierarchy.addListener(hierarchyListener)
        def overlay=[paintOverlay:{graphics,region,downsample,image,complete->
            if(state.closed || !starting.selected || !image.is(data))return
            int index=list.selectionModel.selectedIndex;if(index<0)return
            def roi=originals[rows[index].id] ?: rows[index].startingROI;if(roi==null || roi.z!=region.z || roi.t!=region.t)return
            def copy=(java.awt.Graphics2D)graphics.create()
            try {copy.color=new java.awt.Color(231,167,227,235);copy.stroke=new java.awt.BasicStroke((float)(1.5d*downsample),java.awt.BasicStroke.CAP_BUTT,java.awt.BasicStroke.JOIN_ROUND,10f,[5d*downsample,4d*downsample] as float[],0f);copy.draw(qupath.lib.roi.RoiTools.getShape(roi))}
            finally {copy.dispose()}
        }] as qupath.lib.gui.viewer.overlays.PathOverlay
        viewer.customOverlayLayers.add(overlay);starting.selectedProperty().addListener({obs,old,value->viewer.repaintEntireImage()} as ChangeListener)
        Closure save={->
            if(data.isChanged()){entry.saveImageData(data);gui.project.syncChanges();data.setChanged(false)}
        }
        finish.setOnAction({event->
            try {
                if(gui.imageData!=data)throw new IllegalArgumentException("Return to the correction draft image before saving.")
                if(options.onFinish!=null)options.onFinish.call()
                else {validateCorrectionOutlines(data,ticket);save();gui.stage.hide()}
            }catch(Exception error){status.text=error.message ?: error.class.simpleName}
        } as javafx.event.EventHandler)
        Closure close={boolean hide=true->
            if(state.closed)return
            save();session.selectedIndex=list.selectionModel.selectedIndex
            state.closed=true;hierarchy.removeListener(hierarchyListener);hierarchy.selectionModel.removePathObjectSelectionListener(selectionListener)
            locationReads.each {it.cancel(true)}
            viewer.customOverlayLayers.remove(overlay);undo.disableProperty().unbind();redo.disableProperty().unbind()
            size.valueProperty().removeListener(sizeChanged)
            gui.toolManager.selectedToolProperty().removeListener(toolChanged)
            PathPrefs.brushCreateNewObjectsProperty().set(oldCreate);PathPrefs.brushScaleByMagProperty().set(oldScale)
            PathPrefs.selectionModeProperty().set(oldSelection);PathPrefs.brushDiameterProperty().set(oldDiameter)
            if(options.navigator!=null)queueArea.children.remove(options.navigator)
            if(hide)window.hide();viewer.repaintEntireImage()
        }
        window.setOnHidden({event->if(options.onHidden!=null)options.onHidden.call();else close()} as javafx.event.EventHandler)
        window.show();window.x=gui.stage.x+gui.stage.width-window.width-20;window.y=gui.stage.y+90
        gui.toolManager.setSelectedTool(PathTools.MOVE);refreshTool();list.selectionModel.select((session.selectedIndex ?: 0) as int)
        Platform.runLater({styleBrushSpinner();focus()} as Runnable)
        return [stage:window,resourcesClosed:true,close:close,save:save,rows:rows,list:list,previous:previous,next:next,
            editing:editorBody,status:status,
            move:move,brush:brush,size:size,undo:undo,redo:redo,reset:reset,starting:starting,finish:finish,
            drawCell:drawCell,drawNucleus:drawNucleus,useDrawn:useDrawn,cutline:cutline,split:split,apply:applyOutline,nucleusMode:nucleusMode]
    }
    /** Keep new annotations in the current reference classes without relabeling objects. */
    static Map scopeReferenceClasses(List names) {
        if(!Platform.isFxApplicationThread())throw new IllegalStateException("Open reference classes on the JavaFX thread.")
        if(names.isEmpty() || names.any {!(it instanceof String) || !it.trim()} ||
                names.toSet().size()!=names.size())
            throw new IllegalArgumentException("The reference classes changed. Reopen References.")
        if(names.any {PathClass.fromString(it).toString()!=it})
            throw new IllegalArgumentException("A reference class would change when saved in QuPath. Reopen References and use exact class names.")
        def property=PathPrefs.autoSetAnnotationClassProperty()
        def previous=property.get()
        if(previous==null || !names.contains(previous.toString()))
            property.set(PathClass.fromString(names.first()))
        boolean closed=false
        return [close:{->
            if(!closed){closed=true;property.set(previous)}
        }]
    }

    /** Local panel roles match the export picker and Qt application. */
    static void styleDrawingPanel(javafx.scene.layout.Pane root) {
        Map bundledFamilies=[:]
        ["Regular","SemiBold"].each {weight->
            def stream=org.mif.cells.NativeCellScope.class.getResourceAsStream("/org/mif/fonts/ubuntu-sans/UbuntuSans-${weight}.ttf")
            if(stream!=null)try {
                def loaded=javafx.scene.text.Font.loadFont(stream,15d)
                if(loaded!=null)bundledFamilies[weight]=loaded.family
            }finally {stream.close()}
        }
        List preferences=System.getProperty("os.name","").startsWith("Windows") ?
            ["Segoe UI","Ubuntu Sans","DejaVu Sans"] : ["Ubuntu Sans","Ubuntu","DejaVu Sans"]
        String family=preferences.find {bundledFamilies.values().contains(it) || javafx.scene.text.Font.families.contains(it)} ?: javafx.scene.text.Font.default.family
        String headingFamily=family==bundledFamilies.Regular && bundledFamilies.SemiBold!=null ? bundledFamilies.SemiBold :
            (["${family} SemiBold","${family} Semibold"].find {
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
            .button, .toggle-button, .combo-box, .text-field {
                -fx-background-color:white; -fx-background-insets:0; -fx-background-radius:3;
                -fx-border-color:#bdcccc; -fx-border-radius:3; -fx-border-width:1; -fx-text-fill:#294148; -fx-padding:7 10;
            }
            .button:hover, .toggle-button:hover, .combo-box:hover { -fx-border-color:#8eaaa5; -fx-background-color:#eaf2ef; }
            .button:focused, .toggle-button:focused, .combo-box:focused, .text-field:focused { -fx-border-color:#006b70; }
            .toggle-button:selected { -fx-border-color:#006b70; -fx-background-color:#e4f0ee; }
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
        """
        root.stylesheets.add("data:text/css;base64,"+java.util.Base64.encoder.encodeToString(css.getBytes(java.nio.charset.StandardCharsets.UTF_8)))
    }

    /**
     * Let researchers draw and explicitly select areas in the current draft image.
     *
     * We read QuPath's selection when Save is pressed, check that every selected
     * object is an eligible area in this entry, and save its IDs inside the draft.
     * The annotation exchange returns those IDs after the window closes; the
     * analysis-region step then checks their tissue membership and overlap.
     */
    static Map openRegionSelection(gui,String entryId) {
        if(!Platform.isFxApplicationThread())throw new IllegalStateException("Open region selection on the JavaFX thread.")
        def entry=gui.project.imageList.find {it.ID.toString()==entryId}
        if(entry==null)throw new IllegalArgumentException("The region review image is unavailable. Reopen this step.")
        if((gui.imageData==null || gui.project.getEntry(gui.imageData)!=entry) && !gui.openImageEntry(entry))return null
        Stage window=new Stage();window.initOwner(gui.stage);window.title="Select analysis regions"
        Map controls=create(gui,window)
        Label heading=new Label("Analysis regions");heading.styleClass.add("panel-title")
        Label instruction=new Label("Draw the areas you want to analyze in QuPath.")
        instruction.wrapText=true;instruction.styleClass.add("help")
        Label drawTitle=new Label("Draw regions");drawTitle.styleClass.add("section-title")
        Label selectTitle=new Label("Select the regions to include");selectTitle.styleClass.add("section-title")
        Label selectionHelp=new Label("Select your drawn regions in the image or annotation list, then save the selection.")
        selectionHelp.wrapText=true;selectionHelp.styleClass.add("help")
        Label status=new Label("Save before closing QuPath, then review the regions in the application.")
        status.wrapText=true;status.id="analysis-region-status";status.styleClass.add("help")
        controls.help.styleClass.add("help");controls.choice.parent.alignment=javafx.geometry.Pos.CENTER_LEFT
        controls.node.lookupAll(".label").findAll {it.labelFor!=null}.each {it.styleClass.add("field-caption")}
        Button save=new Button("Save selected regions");save.id="save-analysis-regions";save.styleClass.add("primary")
        save.setOnAction({event->
            try {
                def data=gui.imageData
                if(data==null || gui.project.getEntry(data)!=entry)
                    throw new IllegalArgumentException("Return to the image opened for this region review.")
                def selected=new ArrayList(data.hierarchy.selectionModel.selectedObjects)
                Set annotations=new HashSet(data.hierarchy.annotationObjects)
                if(selected.isEmpty() || selected.any {object->
                    Map metadata=object.getMetadata()
                    boolean protectedObject=object.isLocked() || metadata.keySet().any {key->
                        key.toString().startsWith("pipeline_") || key.toString().startsWith("analysis_domain_") ||
                        key.toString().startsWith("tissue_") || key.toString()=="generation_id"}
                    !annotations.contains(object) || !object.isAnnotation() || object.ROI==null || !object.ROI.isArea() ||
                        (protectedObject && metadata.get("pipeline_stage")!="analysis_domain_selection")
                })throw new IllegalArgumentException("Select drawn area annotations or existing analysis regions from this image.")
                data.setProperty("mif.analysis_region_selection",new com.google.gson.Gson().toJson(selected.collect {it.ID.toString()}.sort()))
                entry.saveImageData(data)
                status.text="Saved ${selected.size()} selected region${selected.size()==1?'':'s'}. Close QuPath, then review the regions in the application."
            }catch(Exception error){qupath.fx.dialogs.Dialogs.showErrorMessage("Analysis regions",error.message ?: error.class.simpleName)}
        } as javafx.event.EventHandler)
        VBox header=new VBox(5d,heading,instruction);header.padding=new javafx.geometry.Insets(20,20,16,20)
        VBox drawing=new VBox(10d,drawTitle,controls.node)
        VBox selection=new VBox(7d,selectTitle,selectionHelp);selection.styleClass.add("section")
        VBox body=new VBox(16d,drawing,selection);body.padding=new javafx.geometry.Insets(0,20,18,20)
        HBox actions=new HBox(save);actions.alignment=javafx.geometry.Pos.CENTER_RIGHT
        VBox footer=new VBox(12d,status,actions);footer.styleClass.add("actions")
        VBox content=new VBox(header,body,footer);styleDrawingPanel(content)
        window.scene=new Scene(content,472,-1);window.resizable=false
        status.textProperty().addListener({o,a,b->Platform.runLater({window.sizeToScene()} as Runnable)} as ChangeListener)
        controls.help.textProperty().addListener({o,a,b->Platform.runLater({window.sizeToScene()} as Runnable)} as ChangeListener)
        def viewer=gui.viewer
        def image=viewer.imageData
        def options=viewer.overlayOptions
        Predicate previous=options.getShowObjectPredicate()
        Predicate visible={object->
            if(previous!=null && !previous.test(object))return false
            if(!viewer.imageData.is(image))return true
            Map metadata=object.getMetadata()
            return !(metadata.get("pipeline_stage")=="analysis_domain_selection" &&
                metadata.get("analysis_domain_status")=="retired" &&
                metadata.get("analysis_domain_active")=="false" &&
                metadata.get("analysis_domain_audit_only")=="true")
        } as Predicate
        options.setShowObjectPredicate(visible)
        boolean closed=false
        def hidden=null
        Closure close={->
            if(closed)return
            closed=true
            if(options.getShowObjectPredicate().is(visible))options.setShowObjectPredicate(previous)
            controls.close()
            window.removeEventHandler(WindowEvent.WINDOW_HIDDEN,hidden)
            viewer.repaint()
        }
        hidden={event->close()} as javafx.event.EventHandler
        window.addEventHandler(WindowEvent.WINDOW_HIDDEN,hidden)
        window.show();viewer.repaint()
        return [stage:window,resourcesClosed:true,close:{->close();window.hide()}]
    }
    /** Build the existing tool selector, keeping its label readable as the choice shrinks. */
    static Map create(gui,window) {
        if(!Platform.isFxApplicationThread())throw new IllegalStateException("Open annotation controls on the JavaFX thread.")
        def manager=gui.toolManager
        Map tools=["Move":PathTools.MOVE,"Brush":PathTools.BRUSH,"Polygon":PathTools.POLYGON,
                   "Rectangle":PathTools.RECTANGLE,"Ellipse":PathTools.ELLIPSE]
        Map tips=["Move":"Select and move annotations in the image.",
                  "Wand":"Click and drag to follow the visible image. Adjust channel contrast or QuPath wand preferences to change its sensitivity.",
                  "Brush":"Paint an annotation region in the image.",
                  "Polygon":"Click around the region. Double-click to finish the polygon.",
                  "Rectangle":"Drag to draw a rectangle. Hold Shift to draw a square.",
                  "Ellipse":"Drag to draw an ellipse. Hold Shift to draw a circle."]
        ComboBox<String> choice=new ComboBox<>();
        choice.items.addAll(tools.keySet())
        choice.id="annotation-drawing-tool";
        choice.accessibleText="Annotation drawing tool"
        choice.minWidth=0
        choice.maxWidth=Double.MAX_VALUE
        Label title=new Label("Drawing tool");
        title.minWidth=javafx.scene.layout.Region.USE_PREF_SIZE
        title.labelFor=choice
        Label help=new Label();
        help.wrapText=true;
        help.id="annotation-drawing-help"
        HBox row=new HBox(10d,title,choice);
        HBox.setHgrow(choice,Priority.ALWAYS)
        VBox node=new VBox(6d,row,help);
        node.id="annotation-drawing-controls"
        Map state=[syncing:false,closed:false]
        Closure refresh={->
            if(state.closed)return
            state.syncing=true
            try {
                choice.value=tools.find {name,tool->tool.is(manager.selectedTool)}?.key
                help.text=PathPrefs.selectionModeProperty().get() ?
                    "Selection mode is on in QuPath. Turn it off in the toolbar to draw new annotations." :
                    (tips[choice.value] ?: "The selected QuPath tool is available in its toolbar.")
            }finally{state.syncing=false}
        }
        ChangeListener selected={obs,old,value->
            if(!state.syncing && !state.closed && value!=null) {
                if(!choice.disabled)manager.setSelectedTool(tools[value])
                refresh()
            }
        } as ChangeListener
        ChangeListener nativeChanged={obs,old,value->refresh()} as ChangeListener
        // QuPath's processing extension installs Wand asynchronously after OpenCV loads.
        // Read its registered instance, rather than constructing a second native tool.
        ListChangeListener toolsChanged=null
        Closure registered={->
            def wand=manager.tools.find {it.name=="Wand"}
            if(wand!=null){tools["Wand"]=wand;manager.tools.removeListener(toolsChanged)}
            state.syncing=true
            try {choice.items.setAll(tools.keySet())}finally{state.syncing=false}
            refresh()
        }
        toolsChanged={change->registered()} as ListChangeListener
        manager.tools.addListener(toolsChanged)
        choice.valueProperty().addListener(selected)
        manager.selectedToolProperty().addListener(nativeChanged)
        PathPrefs.selectionModeProperty().addListener(nativeChanged)
        choice.disableProperty().bind(manager.lockSelectedToolProperty()
            .or(gui.imageDataProperty().isNull()).or(gui.readOnlyProperty()))
        def hidden=null
        Closure close={->
            if(state.closed)return
            state.closed=true
            choice.valueProperty().removeListener(selected)
            manager.selectedToolProperty().removeListener(nativeChanged)
            manager.tools.removeListener(toolsChanged)
            PathPrefs.selectionModeProperty().removeListener(nativeChanged)
            choice.disableProperty().unbind();
            choice.disable=true
            window.removeEventHandler(WindowEvent.WINDOW_HIDDEN,hidden)
        }
        hidden={event->close()} as javafx.event.EventHandler
        window.addEventHandler(WindowEvent.WINDOW_HIDDEN,hidden)
        registered()
        return [node:node,choice:choice,help:help,close:close,isClosed:{->state.closed}]
    }

    static Map install(gui,menu) {
        MenuItem item=new MenuItem("Drawing tools…");
        item.id="mif-annotation-tools"
        Map state=[window:null,controls:null,closed:false]
        // Choose the annotation starting tool once, including when Wand finishes
        // loading after the image. Subsequent toolbar/keyboard choices remain free.
        def manager=gui.toolManager
        Map initial=[done:false]
        ListChangeListener registered=null
        ChangeListener imageOpened=null,toolSelected=null
        Closure stopDefault={->
            initial.done=true
            manager.tools.removeListener(registered)
            gui.imageDataProperty().removeListener(imageOpened)
            manager.selectedToolProperty().removeListener(toolSelected)
        }
        Closure defaultTool={->
            if(initial.done || state.closed || gui.imageData==null || gui.readOnly)return
            def wand=manager.tools.find {it.name=="Wand"}
            if(wand!=null && !manager.lockSelectedToolProperty().get()) {
                stopDefault()
                manager.setSelectedTool(wand)
            }
        }
        registered={change->defaultTool()} as ListChangeListener
        imageOpened={obs,old,value->defaultTool()} as ChangeListener
        toolSelected={obs,old,value->stopDefault()} as ChangeListener
        manager.tools.addListener(registered)
        gui.imageDataProperty().addListener(imageOpened)
        manager.selectedToolProperty().addListener(toolSelected)
        defaultTool()
        item.setOnAction({event->
            if(state.closed)return
            if(state.window?.showing){state.window.toFront();
                return}
            Stage window=new Stage();
            window.initOwner(gui.stage);
            window.title="Annotation tools"
            Map controls=create(gui,window);
            VBox content=new VBox(controls.node);content.padding=new javafx.geometry.Insets(18)
            controls.help.styleClass.add("help");controls.choice.parent.alignment=javafx.geometry.Pos.CENTER_LEFT
            controls.node.lookupAll(".label").findAll {it.labelFor!=null}.each {it.styleClass.add("field-caption")}
            styleDrawingPanel(content)
            window.scene=new Scene(content,430,-1);window.resizable=false
            controls.help.textProperty().addListener({o,a,b->Platform.runLater({window.sizeToScene()} as Runnable)} as ChangeListener)
            state.window=window;
            state.controls=controls;
            window.show()
        } as javafx.event.EventHandler)
        item.disableProperty().bind(gui.imageDataProperty().isNull().or(gui.readOnlyProperty()))
        menu.items.add(item)
        return [item:item,close:{->
            if(!state.closed){state.closed=true;
                state.controls?.close?.call();
                state.window?.hide()
                manager.tools.removeListener(registered)
                gui.imageDataProperty().removeListener(imageOpened)
                manager.selectedToolProperty().removeListener(toolSelected)
                item.disableProperty().unbind();
                menu.items.remove(item);
                item.setOnAction(null)}
        }]
    }
}
return [create:MifAnnotationTools.&create,install:MifAnnotationTools.&install,
        styleDrawingPanel:MifAnnotationTools.&styleDrawingPanel,
        scopeReferenceClasses:MifAnnotationTools.&scopeReferenceClasses,
        openCorrections:MifAnnotationTools.&openCorrections,
        openCorrectionBatch:MifAnnotationTools.&openCorrectionBatch,
        openRegionSelection:MifAnnotationTools.&openRegionSelection]
