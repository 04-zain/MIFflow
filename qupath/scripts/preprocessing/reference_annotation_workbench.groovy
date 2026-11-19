// Guidance for an exact reference draft. QuPath still owns drawing and saving.
import javafx.application.Platform
import javafx.beans.value.ChangeListener
import javafx.geometry.Insets
import javafx.geometry.Pos
import javafx.scene.Scene
import javafx.scene.control.Button
import javafx.scene.control.ComboBox
import javafx.scene.control.Label
import javafx.scene.control.ToggleButton
import javafx.scene.control.ToggleGroup
import javafx.scene.layout.HBox
import javafx.scene.layout.Priority
import javafx.scene.layout.VBox
import javafx.stage.Stage
import javafx.stage.WindowEvent
import qupath.lib.gui.prefs.PathPrefs
import qupath.lib.objects.classes.PathClass
import qupath.lib.objects.hierarchy.events.PathObjectHierarchyListener

class ReferenceAnnotationWorkbench {
    static final Map CLASSES = [positive:'Positive reference', af:'Autofluorescence control',
                               negative:'Negative reference', background:'Image background']
    static final Map PURPOSES = [positive:'Draw tissue with clear fluorophore signal.',
        af:'Draw tissue in the unstained control image.',
        negative:'Draw tissue matching the stained reference region.',
        background:'Draw outside tissue in this image.']
    static final Map TITLES = [positive:'Stained tissue',af:'AF tissue',negative:'Negative tissue',background:'Image background']

    static Map open(gui, Map request, Map tools) {
        if(!Platform.isFxApplicationThread())throw new IllegalStateException('Open reference guidance on the JavaFX thread.')
        def project=gui.project
        List records=request.entries
        if(request.keySet()!=['entries','initial_entry_id'] as Set || !(records instanceof List) || records.empty ||
                records.any {!(it instanceof Map) || it.keySet()!=['entry_id','input_id','source_uri','role','image_name'] as Set ||
                    !(it.role in ['positive','af','negative']) ||
                    !['entry_id','input_id','source_uri','image_name'].every {key->it[key] instanceof String && it[key].trim()}} ||
                records.collect {it.entry_id}.toSet().size()!=records.size() ||
                !records.any {it.entry_id==request.initial_entry_id} || project==null ||
                project.imageList.collect {it.ID.toString()}.toSet()!=records.collect {it.entry_id}.toSet())
            throw new IllegalArgumentException('The reference annotation sources changed. Reopen this preparation.')
        Map entries=[:]
        records.each {record->
            def entry=project.imageList.find {it.ID.toString()==record.entry_id}
            def uris=entry.getURIs().collect {it.normalize()}
            def expected=URI.create(record.source_uri).normalize()
            if(expected.scheme!='file' || uris.size()!=1 || uris[0]!=expected)
                throw new IllegalArgumentException('The reference image changed. Reopen this preparation.')
            entries[record.entry_id]=entry
        }
        Stage window=new Stage();window.initOwner(gui.stage);window.title='Annotate reference regions'
        Map state=[closed:false,syncing:false,current:null,image:null,scope:null]
        Label title=new Label('Reference annotations');title.styleClass.add('panel-title')
        Label imageName=new Label();imageName.wrapText=true;imageName.styleClass.add('help')
        ComboBox<String> images=new ComboBox<>();images.id='reference-annotation-image';images.maxWidth=Double.MAX_VALUE
        images.accessibleText='Reference image'
        def counts=records.countBy {it.image_name}
        records.eachWithIndex {record,index->images.items.add(record.image_name+(counts[record.image_name]>1?' · image '+(index+1):''))}
        images.visible=records.size()>1;images.managed=images.visible
        imageName.visible=!images.visible;imageName.managed=imageName.visible
        Label scope=new Label('Choose a region type, then draw in the image.');scope.wrapText=true;scope.styleClass.add('help')
        VBox header=new VBox(7d,title,imageName,images,scope);header.padding=new Insets(20,20,16,20)

        ToggleGroup roles=new ToggleGroup()
        ToggleButton tissue=new ToggleButton(),background=new ToggleButton()
        tissue.id='reference-tissue-role';background.id='reference-background-role'
        Label tissueTitle=new Label(),backgroundTitle=new Label(TITLES.background)
        Label tissuePurpose=new Label(),backgroundPurpose=new Label(PURPOSES.background)
        Label tissueCount=new Label(),backgroundCount=new Label()
        tissueCount.id='reference-tissue-count';backgroundCount.id='reference-background-count'
        [tissueTitle,backgroundTitle].each {it.styleClass.add('section-title')}
        [tissuePurpose,backgroundPurpose,tissueCount,backgroundCount].each {it.styleClass.add('help');it.wrapText=true}
        tissue.graphic=new VBox(5d,tissueTitle,tissuePurpose,tissueCount)
        background.graphic=new VBox(5d,backgroundTitle,backgroundPurpose,backgroundCount)
        [tissue,background].each {
            it.toggleGroup=roles;it.maxWidth=Double.MAX_VALUE;it.prefWidth=220;it.minWidth=0
            it.minHeight=112;it.prefHeight=112;it.maxHeight=112
            it.alignment=Pos.TOP_LEFT;it.accessibleRole=javafx.scene.AccessibleRole.RADIO_BUTTON
            HBox.setHgrow(it,Priority.ALWAYS)
        }
        background.userData=CLASSES.background;background.accessibleText='Draw image background'
        HBox roleRow=new HBox(10d,tissue,background)
        Map drawing=tools.create.call(gui,window)
        drawing.help.styleClass.add('help');drawing.choice.parent.alignment=Pos.CENTER_LEFT
        drawing.node.lookupAll('.label').findAll {it.labelFor!=null}.each {it.styleClass.add('field-caption')}
        Button assign=new Button('Assign to selected regions');assign.id='reference-assign-class'
        assign.managedProperty().bind(assign.visibleProperty())
        Label drawTitle=new Label('Draw regions');drawTitle.styleClass.add('section-title')
        VBox drawingSection=new VBox(10d,drawTitle,drawing.node,assign);drawingSection.styleClass.add('section')
        VBox body=new VBox(16d,roleRow,drawingSection);body.padding=new Insets(0,20,18,20)
        Label status=new Label();status.wrapText=true;status.styleClass.add('help');status.id='reference-annotation-status'
        status.visibleProperty().bind(status.textProperty().isNotEmpty())
        status.managedProperty().bind(status.visibleProperty())
        Button finish=new Button('Save and return');finish.id='reference-annotation-return';finish.styleClass.add('primary')
        HBox actions=new HBox(finish);actions.alignment=Pos.CENTER_RIGHT
        VBox footer=new VBox(10d,status,actions);footer.styleClass.add('actions')
        VBox content=new VBox(header,body,footer);tools.styleDrawingPanel.call(content)
        window.scene=new Scene(content,480,-1);window.resizable=false
        ChangeListener resize={obs,old,value->Platform.runLater({if(!state.closed)window.sizeToScene()} as Runnable)} as ChangeListener
        status.textProperty().addListener(resize);drawing.help.textProperty().addListener(resize)

        Closure refresh={->
            if(state.closed || state.current==null)return
            def image=gui.imageData
            boolean owned=gui.project.is(project) && image!=null && project.getEntry(image)==entries[state.current.entry_id]
            tissue.disable=!owned || gui.readOnly;background.disable=tissue.disabled
            assign.disable=tissue.disabled || roles.selectedToggle==null || image.hierarchy.selectionModel.selectedObjects.empty ||
                image.hierarchy.selectionModel.selectedObjects.any {!it.isAnnotation() || it.ROI==null || !it.ROI.isArea()}
            assign.visible=!assign.disabled
            if(assign.visible)assign.text='Set selected regions to '+TITLES[CLASSES.find {key,value->value==roles.selectedToggle.userData}?.key].toLowerCase()
            finish.disable=!owned || gui.readOnly
            if(!owned){status.text='Return to the reference image to continue.';return}
            def objects=image.hierarchy.annotationObjects
            int tissueNumber=objects.count {it.pathClass?.toString()==CLASSES[state.current.role]}
            int backgroundNumber=objects.count {it.pathClass?.toString()==CLASSES.background}
            tissueCount.text=tissueNumber+' region'+(tissueNumber==1?'':'s')
            backgroundCount.text=backgroundNumber+' region'+(backgroundNumber==1?'':'s')
            if(status.text=='Return to the reference image to continue.')status.text=''
        }
        Closure saveCurrent={->
            if(state.current==null)return
            if(gui.project!=project || gui.imageData==null || project.getEntry(gui.imageData)!=entries[state.current.entry_id])
                throw new IllegalStateException('Return to the reference image before saving.')
            entries[state.current.entry_id].saveImageData(gui.imageData)
            project.syncChanges()
        }
        PathObjectHierarchyListener hierarchyChanged={event->
            if(Platform.isFxApplicationThread())refresh();else Platform.runLater({refresh()} as Runnable)
        } as PathObjectHierarchyListener
        ChangeListener selectionChanged={obs,old,value->refresh()} as ChangeListener
        def selectionListener={selected,previous,all->refresh()} as qupath.lib.objects.hierarchy.events.PathObjectSelectionListener
        Closure detach={->
            state.image?.hierarchy?.removeListener(hierarchyChanged)
            state.image?.hierarchy?.selectionModel?.removePathObjectSelectionListener(selectionListener)
            state.scope?.close?.call();state.scope=null;state.image=null
        }
        Closure selectImage={int index->
            if(state.closed || state.syncing || index<0)return
            Map record=records[index]
            if(state.current?.entry_id==record.entry_id)return
            int previous=state.current==null?-1:records.indexOf(state.current)
            try {
                if(state.current!=null && gui.imageData!=null &&
                        project.getEntry(gui.imageData)==entries[state.current.entry_id])saveCurrent()
                def entry=entries[record.entry_id]
                if(gui.project!=project)throw new IllegalStateException('Reopen the reference preparation from the application.')
                if(gui.imageData==null || project.getEntry(gui.imageData)!=entry)
                    if(!gui.openImageEntry(entry))throw new IllegalStateException('The reference image was not opened.')
                detach();state.current=record;state.image=gui.imageData
                state.scope=tools.scopeReferenceClasses.call([CLASSES[record.role],CLASSES.background])
                state.syncing=true
                try {
                    images.selectionModel.select(index)
                    tissue.userData=CLASSES[record.role];tissue.accessibleText='Draw '+TITLES[record.role].toLowerCase()
                    tissueTitle.text=TITLES[record.role];tissuePurpose.text=PURPOSES[record.role]
                    roles.selectToggle(PathPrefs.autoSetAnnotationClassProperty().get()?.toString()==CLASSES.background?background:tissue)
                }finally {state.syncing=false}
                imageName.text=record.image_name
                state.image.hierarchy.addListener(hierarchyChanged)
                state.image.hierarchy.selectionModel.addPathObjectSelectionListener(selectionListener)
                status.text='';refresh();window.sizeToScene()
            }catch(Exception error){
                state.syncing=true
                try {images.selectionModel.select(previous)}finally{state.syncing=false}
                status.text=error.message ?: error.class.simpleName
                if(state.current==null)throw error
            }
        }
        images.selectionModel.selectedIndexProperty().addListener({obs,old,value->selectImage(value.intValue())} as ChangeListener)
        roles.selectedToggleProperty().addListener({obs,old,value->
            if(state.closed || state.syncing || tissue.disabled)return
            if(value==null && old!=null){roles.selectToggle(old);return}
            if(value==null)return
            // A role choice is the native class for the next annotation only.
            PathPrefs.autoSetAnnotationClassProperty().set(PathClass.fromString(value.userData as String))
            refresh();window.sizeToScene()
        } as ChangeListener)
        ChangeListener nativeClass={obs,old,value->
            if(state.closed || state.syncing)return
            state.syncing=true
            try {roles.selectToggle([tissue,background].find {it.userData==value?.toString()})}
            finally {state.syncing=false}
            refresh()
        } as ChangeListener
        PathPrefs.autoSetAnnotationClassProperty().addListener(nativeClass)
        gui.imageDataProperty().addListener(selectionChanged)
        assign.setOnAction({event->
            if(assign.disabled || roles.selectedToggle==null)return
            def objects=gui.imageData.hierarchy.selectionModel.selectedObjects.toList()
            objects.each {it.setPathClass(PathClass.fromString(roles.selectedToggle.userData as String))}
            gui.imageData.hierarchy.fireObjectClassificationsChangedEvent(window,objects)
            refresh()
        } as javafx.event.EventHandler)
        finish.setOnAction({event->
            try {
                saveCurrent()
                status.text='Regions saved. Returning to the application…'
                gui.sendQuitRequest()
            }catch(Exception error){status.text='Could not save: '+(error.message ?: error.class.simpleName)}
        } as javafx.event.EventHandler)
        Closure close={->
            if(state.closed)return
            state.closed=true;detach();drawing.close()
            PathPrefs.autoSetAnnotationClassProperty().removeListener(nativeClass)
            gui.imageDataProperty().removeListener(selectionChanged)
            window.hide()
        }
        window.setOnHidden({event->close()} as javafx.event.EventHandler)
        try {selectImage(records.findIndexOf {it.entry_id==request.initial_entry_id})}
        catch(Throwable error){close();throw error}
        window.show();window.x=gui.stage.x+gui.stage.width-window.width-20;window.y=gui.stage.y+75
        return [stage:window,close:close,resourcesClosed:true,images:images,tissue:tissue,background:background,assign:assign,
                finish:finish,status:status,drawing:drawing,save:saveCurrent]
    }
}
return [open:ReferenceAnnotationWorkbench.&open]
