// Prepare native reference edits and return their changes for the owning save operation.
import java.nio.charset.StandardCharsets
import java.security.MessageDigest
import qupath.lib.io.PathIO
import qupath.lib.objects.PathObjectTools
import org.mif.roi.BiologicalParentSelector

class ClusterAnnotationEdits {
    static final int MAX_OBJECTS=100000
    static final int MAX_BYTES=32*1024*1024
    static Object canonical(Object value) {
        if(value instanceof Map) {
            def sorted=new TreeMap<String,Object>()
            value.each {k,v -> sorted.put(k.toString(),canonical(v))}
            return sorted
        }
        if(value instanceof List)return value.collect {canonical(it)}
        return value
    }
    static String digest(Object value) {
        byte[] bytes=new com.google.gson.GsonBuilder().disableHtmlEscaping().serializeNulls().setObjectToNumberStrategy(com.google.gson.ToNumberPolicy.BIG_DECIMAL).create().toJson(canonical(value)).getBytes(StandardCharsets.UTF_8)
        if(bytes.length>MAX_BYTES)throw new IllegalArgumentException("Annotation review exceeds 32 MiB; use a smaller tissue scope.")
        return MessageDigest.getInstance("SHA-256").digest(bytes).encodeHex().toString()
    }
    static boolean protectedObject(object) {
        Map metadata=object.getMetadata()
        return object.isLocked() || metadata.keySet().any {key ->
            key.toString().startsWith("pipeline_") || key.toString().startsWith("analysis_domain_") ||
            key.toString().startsWith("tissue_") || key.toString()=="generation_id"}
    }
    /** A pending tissue review can change its own candidate outlines only. */
    static boolean candidateOutline(object, Map scope) {
        Map values=object.metadata
        return scope!=null && values.pipeline_stage=="tissue_detection" &&
            values.generation_status=="candidate" && values.generation_id==scope.generation_id &&
            values.roi_id in scope.roi_ids
    }
    /** Only references from the ticket's current image/population/role are editable. */
    static boolean ownedReference(object, Map reference) {
        if (reference == null || !object.ROI?.isPoint() ||
                !(reference.reference_selection_sha256 ==~ /[0-9a-f]{64}/)) return false
        Map values = object.metadata
        if (values.pipeline_stage != "supervised_training" ||
                values.supervised_image_entry_id != reference.project_entry_id ||
                values.get("mif.reference_selection_sha256") != reference.reference_selection_sha256 ||
                values.get("mif.split") != reference.split) return false
        String bindings = values.supervised_point_bindings
        if (bindings == null) return false
        List records = new com.google.gson.Gson().fromJson(bindings, List.class)
        return !records.isEmpty() && records.every { row ->
            reference.sections.any { section ->
                row.section_id == section.section_id &&
                row.segmentation_generation_id == section.segmentation_generation_id &&
                row.segmentation_population_id == section.segmentation_population_id
            }
        }
    }
    static boolean referenceField(String key) {
        return key.startsWith("supervised_") || key == "mif.training_plan_id" ||
            key == "mif.reference_selection_sha256" ||
            key == "mif.split" || key == "pipeline_stage" || key == "spatial_region_id"
    }
    static Map exportObjects(objects) {
        if(objects.size()>MAX_OBJECTS)throw new IllegalArgumentException("Too many annotation objects for one review.")
        def out=new ByteArrayOutputStream()
        PathIO.exportObjectsAsGeoJSON(out,objects,PathIO.GeoJsonExportOptions.FEATURE_COLLECTION)
        if(out.size()>MAX_BYTES)throw new IllegalArgumentException("Annotation review exceeds 32 MiB.")
        return new com.google.gson.GsonBuilder().disableHtmlEscaping().serializeNulls().setObjectToNumberStrategy(com.google.gson.ToNumberPolicy.BIG_DECIMAL).create().fromJson(out.toString("UTF-8"),Map.class)
    }
    static Map snapshot(data, Map reference = null) {
        def annotations=data.hierarchy.annotationObjects.sort {it.ID.toString()}
        Map result=[schema_version:"cluster-user-annotations/v1",
            objects:exportObjects(annotations),
            protected_ids:annotations.findAll {protectedObject(it) && !ownedReference(it, reference)}.collect {it.ID.toString()},
            editable_ids:annotations.findAll {!protectedObject(it) || ownedReference(it, reference)}.collect {it.ID.toString()},
            width:data.server.width,height:data.server.height]
        result.content_sha256=digest(result)
        return result
    }
    /** Replay explicit deletions through the existing owner; never import draft geometry or labels. */
    static Map removeCells(data, Map scope, Map baseline, Map incoming) {
        List fields=["mif.cell_datasets", "mif.segmentation_populations", "mif.cell_components", "mif.cell_edits"]
        if(scope==null || baseline==null || incoming==null ||
                !incoming.keySet().every {it in fields})
            throw new IllegalArgumentException("Cell removals require the current measurement recovery draft.")
        Map current=fields.findAll {data.getProperty(it)!=null}.collectEntries {[(it):data.getProperty(it)]}
        if(current!=baseline || fields.findAll {it!="mif.cell_edits"}.any {incoming[it]!=baseline[it]})
            throw new IllegalArgumentException("The saved cell population or measurements changed. Open a new removal draft.")
        def parse={value -> value==null ? [:] : new com.google.gson.Gson().fromJson(value as String,Map.class)}
        Map before=parse(baseline["mif.cell_edits"]), after=parse(incoming["mif.cell_edits"])
        String population=scope.segmentation_population_id
        if(after.keySet()-[population] != before.keySet()-[population] ||
                before.keySet().findAll {it!=population}.any {after[it]!=before[it]})
            throw new IllegalArgumentException("The draft changed cells outside the selected tissue section.")
        def provider=org.mif.cells.NativeCellScope.open(data,scope.section_id as String).join()
        if(provider.population!=population || provider.descriptionSha256!=scope.cell_evidence_sha256)
            throw new IllegalArgumentException("The accepted cell population changed. Open a new removal draft.")
        Map oldRows=before[population]?.edits ?: [:], newRows=after[population]?.edits ?: [:]
        if(!newRows.keySet().containsAll(oldRows.keySet()))
            throw new IllegalArgumentException("The draft removed an earlier saved cell correction.")
        List removed=[]
        newRows.keySet().sort().each {id ->
            Map row=newRows[id]
            if(row==oldRows[id])return
            if(row.deleted!=true || oldRows[id]?.deleted==true)
                throw new IllegalArgumentException("This draft accepts cell removals only; keep other cell edits unchanged.")
            def cell=provider.lookup(id as String)
            if(cell==null)throw new IllegalArgumentException("A removed cell is absent from the accepted population.")
            provider.recordEdit(data,cell,true)
            Map generated=parse(data.getProperty("mif.cell_edits"))[population]
            // Native counters describe the editor session. The accepting owner writes
            // its own counters and checks every scientific field against its cell.
            Map expected=new LinkedHashMap(generated.edits[id]), received=new LinkedHashMap(row)
            ["revision","geometry_revision"].each {expected.remove(it);received.remove(it)}
            if(expected!=received || after[population].population_id!=population ||
                    after[population].layout_version!=generated.layout_version ||
                    after[population].keySet()!=generated.keySet())
                throw new IllegalArgumentException("A removed cell also changed its outline, class or population.")
            removed << id
        }
        if(removed.isEmpty() && after!=before)
            throw new IllegalArgumentException("The draft changed cell records without an explicit removal.")
        return [removed_cell_ids:removed,removed_cells:removed.size(),remaining_cells:provider.currentRowCount]
    }
    /** Resolve the exact accepted cell before exposing its one editable outline. */
    static Map correctionCell(data, Map scope) {
        if(scope == null || !(scope.operation in ["cell_outline", "nucleus_outline", "add_nucleus", "delete", "split", "merge"]))
            throw new IllegalArgumentException("Choose an existing cell outline, nuclear outline or explicit deletion.")
        def provider=org.mif.cells.NativeCellScope.open(data,scope.section_id as String).join()
        if(provider.population!=scope.segmentation_population_id || provider.descriptionSha256!=scope.cell_evidence_sha256 ||
                provider.geometryRevision!=(scope.geometry_revision as Number)?.longValue())
            throw new IllegalArgumentException("The selected cells changed. Open a new correction draft.")
        def cell=provider.lookup(scope.cell_id as String)
        if(cell==null)throw new IllegalArgumentException("The selected cell is absent from the accepted population.")
        boolean nuclearOnly=scope.cell_boundary_source=="nuclear_outline"
        if(scope.operation=="nucleus_outline" && cell.nucleusROI==null)
            throw new IllegalArgumentException("This cell has no nuclear outline to reshape.")
        if(scope.operation=="add_nucleus" && cell.nucleusROI!=null)
            throw new IllegalArgumentException("This cell already has a nuclear outline. Choose Reshape nuclear outline.")
        if(scope.operation=="add_nucleus" && nuclearOnly)
            throw new IllegalArgumentException("This result contains nuclear outlines rather than separate cell compartments.")
        if(scope.operation=="cell_outline" && nuclearOnly)
            throw new IllegalArgumentException("This result contains nuclear outlines. Choose Reshape nuclear outline.")
        return [provider:provider,cell:cell,nuclearOnly:nuclearOnly]
    }
    static List correctionOperations(Map scope) {
        def operations=scope.containsKey("operations") ? scope.operations :
            [[cell_id:scope.cell_id,operation:scope.operation]]
        if(!(operations instanceof List) || operations.isEmpty() || operations.size()>MAX_OBJECTS)
            throw new IllegalArgumentException("Select a bounded list of existing cell corrections.")
        operations.each {item ->
            if(!(item instanceof Map) || item.keySet()!=((item.operation=="merge" ? ["cell_id","operation","source_cell_ids"] : ["cell_id","operation"]) as Set) ||
                    !(item.cell_id instanceof String) || !item.cell_id || item.cell_id.trim()!=item.cell_id ||
                    !(item.operation in ["cell_outline","nucleus_outline","add_nucleus","delete","split","merge","add_cell"]))
                throw new IllegalArgumentException("Each correction must identify a known cell and existing compartment.")
            if(item.operation=="merge" && (!(item.source_cell_ids instanceof List) || item.source_cell_ids.size()<2 ||
                    item.source_cell_ids.any {!(it instanceof String) || !it || it.trim()!=it} ||
                    item.source_cell_ids.toSet().size()!=item.source_cell_ids.size() || item.source_cell_ids.sort(false)!=item.source_cell_ids ||
                    item.cell_id!=item.source_cell_ids.first()))
                throw new IllegalArgumentException("Merge requires distinct sorted source cells in one task.")
            if(item.operation=="add_cell")UUID.fromString(item.cell_id as String)
        }
        if(operations.collect {it.cell_id+"|"+it.operation}.toSet().size()!=operations.size() ||
                operations.groupBy {it.cell_id}.values().any {rows -> rows.size()>1 && rows.any {!(it.operation in ["cell_outline","nucleus_outline","add_nucleus"])}} ||
                operations.groupBy {it.cell_id}.values().any {rows -> rows.any {it.operation=="nucleus_outline"} && rows.any {it.operation=="add_nucleus"}})
            throw new IllegalArgumentException("Choose deletion or distinct outline edits for each cell.")
        Map consumed=[:]
        operations.each {op -> (op.operation=="merge" ? op.source_cell_ids : [op.cell_id]).each {id ->
            if(consumed.containsKey(id) && (op.operation in ["merge","split","add_cell"] || consumed[id] in ["merge","split","add_cell"]))
                throw new IllegalArgumentException("Each source cell belongs to one identity correction task.")
            consumed[id]=op.operation
        }}
        return operations.sort(false) {a,b -> a.cell_id<=>b.cell_id ?: a.operation<=>b.operation}
    }
    static UUID correctionTaskID(Map operation) {
        return UUID.nameUUIDFromBytes((operation.cell_id+"|"+operation.operation).getBytes(StandardCharsets.UTF_8))
    }
    static UUID correctionOutlineID(cell,Map operation,Map scope) {
        // These are temporary annotation IDs; the underlying cell ID is retained.
        return scope.containsKey("operations") ? correctionTaskID(operation) : cell.ID
    }
    static def newCorrectionOutline(roi,Map operation,UUID resultID,String role,String nucleusMode="retain") {
        if(!(role in ["cell","nucleus"]) || !(nucleusMode in ["retain","redraw","none"]))
            throw new IllegalArgumentException("Choose the new compartment and explicit nuclear association.")
        def outline=qupath.lib.objects.PathObjects.createAnnotationObject(roi)
        outline.setID(role=="cell" ? resultID : UUID.nameUUIDFromBytes((resultID.toString()+"|nucleus").getBytes(StandardCharsets.UTF_8)))
        outline.name=role=="cell" ? "New cell" : "Nuclear outline"
        outline.metadata.put("mif.correction_task",correctionTaskID(operation).toString())
        outline.metadata.put("mif.correction_result_id",resultID.toString())
        outline.metadata.put("mif.correction_role",role)
        if(role=="cell")outline.metadata.put("mif.correction_nucleus_mode",nucleusMode)
        return outline
    }
    static def exactCorrectionROI(geometry,plane) {
        def constructor=qupath.lib.roi.GeometryROI.class.getDeclaredConstructor(org.locationtech.jts.geom.Geometry.class,qupath.lib.regions.ImagePlane.class)
        constructor.setAccessible(true)
        return constructor.newInstance(geometry,plane)
    }
    /** Exact partition proposal from a drawn cutline; no buffers or invented connectors. */
    static List splitCorrectionOutline(cellROI,cutlineROI) {
        if(cellROI==null || cutlineROI==null || cellROI.imagePlane!=cutlineROI.imagePlane ||
                !(cutlineROI.geometry instanceof org.locationtech.jts.geom.Lineal))
            throw new IllegalArgumentException("Draw a cutline across the selected cell in its image plane.")
        def factory=new org.locationtech.jts.geom.GeometryFactory()
        def source=factory.createGeometry(cellROI.geometry),cut=factory.createGeometry(cutlineROI.geometry)
        def polygonizer=new org.locationtech.jts.operation.polygonize.Polygonizer()
        polygonizer.add(source.boundary.union(cut))
        List pieces=polygonizer.polygons.findAll {source.covers(it.interiorPoint)}.toList()
        if(pieces.size()<2 || pieces.any {it.empty || !it.valid || it.area<=0d} ||
                !org.locationtech.jts.operation.union.UnaryUnionOp.union(pieces).equalsTopo(source))
            throw new IllegalArgumentException("The cutline must divide the cell into at least two valid complete outlines.")
        return pieces.sort(false) {a,b -> a.centroid.x<=>b.centroid.x ?: a.centroid.y<=>b.centroid.y ?: a.area<=>b.area}.collect {
            exactCorrectionROI(it,cellROI.imagePlane)
        }
    }
    static Map knownCellDraft(data, Map scope) {
        List outlines=[]
        correctionOperations(scope).eachWithIndex {operation,index ->
            if(operation.operation in ["add_cell","add_nucleus"]) {
                if(operation.operation=="add_nucleus")correctionCell(data,scope+operation)
                return
            }
            if(operation.operation in ["split","merge"]) {
                List sources=(operation.operation=="merge" ? operation.source_cell_ids : [operation.cell_id]).collect {id ->
                    correctionCell(data,scope+operation+[cell_id:id]).cell
                }
                if(sources.collect {it.metadata.get("analysis_domain_id")}.toSet().size()!=1)
                    throw new IllegalArgumentException("Merge cells from the same accepted analysis area.")
                UUID id=UUID.nameUUIDFromBytes((correctionTaskID(operation).toString()+"|proposal").getBytes(StandardCharsets.UTF_8))
                def geometry=org.locationtech.jts.operation.union.UnaryUnionOp.union(sources.collect {it.ROI.geometry})
                def roi=sources.size()==1 ? sources.first().ROI : exactCorrectionROI(geometry,sources.first().ROI.imagePlane)
                outlines << newCorrectionOutline(roi,operation,id,"cell","retain")
                List nuclei=sources.findAll {it.nucleusROI!=null}.collect {it.nucleusROI.geometry}
                if(!nuclei.empty)outlines << newCorrectionOutline(exactCorrectionROI(
                    org.locationtech.jts.operation.union.UnaryUnionOp.union(nuclei),roi.imagePlane),operation,id,"nucleus")
                return
            }
            Map selected=correctionCell(data,scope+operation)
            if(operation.operation!="delete") {
                def outline=qupath.lib.objects.PathObjects.createAnnotationObject(
                    operation.operation=="nucleus_outline" ? selected.cell.nucleusROI : selected.cell.ROI)
                outline.setID(correctionOutlineID(selected.cell,operation,scope))
                String name=operation.operation=="nucleus_outline" ? "Nuclear outline" : "Cell outline"
                outline.setName(scope.containsKey("operations") ? name+" · "+(index+1) : name)
                outlines << outline
            }
        }
        return exportObjects(outlines)
    }
    /** Validate all final geometries before recording any sparse edit. */
    static Map correctKnownCells(data, Map scope, Map incoming, boolean apply=false) {
        List operations=correctionOperations(scope)
        if(incoming?.type!="FeatureCollection" || !(incoming.features instanceof List) || incoming.features.size()>MAX_OBJECTS)
            throw new IllegalArgumentException("Return the selected existing outlines as one FeatureCollection.")
        digest(incoming)
        def decode={Map objects -> PathIO.readObjectsFromGeoJSON(new ByteArrayInputStream(
            new com.google.gson.Gson().toJson(objects).getBytes(StandardCharsets.UTF_8)))}
        def expected=decode(knownCellDraft(data,scope)).findAll {!it.metadata.containsKey("mif.correction_task")}.collectEntries {[(it.ID):it]}
        def submitted=decode(incoming)
        List identityOutlines=submitted.findAll {it.metadata.containsKey("mif.correction_task")}
        List ordinary=submitted.findAll {!it.metadata.containsKey("mif.correction_task")}
        Set missingNucleusIDs=operations.findAll {it.operation=="add_nucleus"}.collect {correctionTaskID(it)} as Set
        if(submitted.collect {it.ID}.toSet().size()!=submitted.size() || ordinary.size()!=expected.size()+missingNucleusIDs.size() ||
                ordinary.collect {it.ID}.toSet()!=expected.keySet()+missingNucleusIDs ||
                ordinary.findAll {!missingNucleusIDs.contains(it.ID)}.any {!it.isAnnotation() || !sameAnnotation(expected[it.ID],it,true)})
            throw new IllegalArgumentException("Keep every selected outline's identity, name and class unchanged.")
        ordinary.findAll {missingNucleusIDs.contains(it.ID)}.each {outline ->
            int position=operations.findIndexOf {correctionTaskID(it)==outline.ID}
            String name=scope.containsKey("operations") ? "Nuclear outline · "+(position+1) : "Nuclear outline"
            if(!outline.isAnnotation() || outline.pathClass!=null || outline.name!=name || !outline.metadata.isEmpty())
                throw new IllegalArgumentException("Return the explicitly drawn missing nucleus for its selected cell.")
        }
        Map returned=submitted.collectEntries {[(it.ID):it.ROI]}
        def writer=new org.locationtech.jts.io.WKBWriter()
        def hex={roi -> roi==null ? null : writer.write(roi.geometry).encodeHex().toString()}
        def read={String encoded -> new org.locationtech.jts.io.WKBReader().read(encoded.decodeHex())}
        def area=null;Map domains=[:]
        if(operations.any {it.operation!="delete"}) {
            area=read(scope.analysis_area_wkb_hex as String)
            def normalized=area.copy();normalized.normalize()
            String areaSha=MessageDigest.getInstance("SHA-256").digest(new org.locationtech.jts.io.WKBWriter(
                2,org.locationtech.jts.io.ByteOrderValues.LITTLE_ENDIAN,false).write(normalized)).encodeHex().toString()
            if(areaSha!=scope.union_geometry_sha256 || !(scope.domains instanceof Map) || scope.domains.isEmpty())
                throw new IllegalArgumentException("The correction areas differ from the accepted segmentation.")
            domains=scope.domains.collectEntries {key,value -> [(key):read(value as String)]}
        }
        def provider=org.mif.cells.NativeCellScope.open(data,scope.section_id as String).join()
        if(provider.population!=scope.segmentation_population_id || provider.descriptionSha256!=scope.cell_evidence_sha256 ||
                provider.geometryRevision!=(scope.geometry_revision as Number).longValue())
            throw new IllegalArgumentException("The selected cells changed. Open a new correction draft.")
        Map proposed=[:];List changes=[],unchanged=[]
        operations.findAll {!(it.operation in ["split","merge","add_cell"])}.groupBy {it.cell_id}.each {id,rows ->
            Map selected=correctionCell(data,scope+rows.first())
            provider=selected.provider
            def cell=selected.cell,cellROI=cell.ROI,nucleusROI=cell.nucleusROI
            Map before=[cell_geometry_wkb_hex:hex(cellROI),nucleus_geometry_wkb_hex:hex(nucleusROI)]
            boolean deleted=rows.first().operation=="delete"
            List changedOperations=[]
            rows.findAll {it.operation!="delete"}.each {operation ->
                // Validate each requested compartment, including a second one on the same cell.
                correctionCell(data,scope+operation)
                UUID annotationID=correctionOutlineID(cell,operation,scope)
                def roi=returned[annotationID],geometry=roi.geometry
                if(roi.imagePlane!=cell.ROI.imagePlane || !(geometry instanceof org.locationtech.jts.geom.Polygonal) ||
                        geometry.empty || !geometry.valid || !Double.isFinite(geometry.area) || geometry.area<=0d ||
                        !geometry.coordinates.every {Double.isFinite(it.x)&&Double.isFinite(it.y)})
                    throw new IllegalArgumentException("Draw a valid outline in the original image plane for "+id+".")
                if(operation.operation=="add_nucleus" || !geometry.equalsNorm(expected[annotationID].ROI.geometry))changedOperations << operation.operation
                if(operation.operation=="cell_outline" || selected.nuclearOnly)cellROI=roi
                if(operation.operation in ["nucleus_outline","add_nucleus"])nucleusROI=roi
            }
            boolean changed=deleted || !changedOperations.isEmpty()
            if(changed && !deleted) {
                if(nucleusROI!=null && !cellROI.geometry.covers(nucleusROI.geometry))
                    throw new IllegalArgumentException("Keep the nuclear outline inside its cell outline: "+id+".")
                if(!area.covers(cellROI.geometry))
                    throw new IllegalArgumentException("Keep the cell inside the accepted segmentation area: "+id+".")
                List owners=domains.findAll {key,geometry -> geometry.covers(cellROI.geometry.centroid)}.keySet().toList()
                if(owners!=[cell.metadata.get("analysis_domain_id")])
                    throw new IllegalArgumentException("Keep the cell in its original, unambiguous analysis area: "+id+".")
            }
            def replacement=qupath.lib.objects.PathObjects.createCellObject(cellROI,nucleusROI,cell.pathClass)
            replacement.setID(cell.ID);replacement.metadata.putAll(cell.metadata)
            proposed[id]=[cell:replacement,deleted:deleted,changed:changed]
            if(changed) {
                def calibration=data.server.pixelCalibration
                changes << [cell_id:id,operations:(deleted ? ["delete"] : changedOperations),before:before,
                    after:(deleted ? null : [cell_geometry_wkb_hex:hex(cellROI),nucleus_geometry_wkb_hex:hex(nucleusROI)]),
                    center_um:[cell.ROI.centroidX*calibration.pixelWidthMicrons,cell.ROI.centroidY*calibration.pixelHeightMicrons]]
            } else unchanged << id
        }
        Set expectedTasks=operations.findAll {it.operation in ["split","merge","add_cell"]}.collect {correctionTaskID(it).toString()} as Set
        if(identityOutlines.any {!expectedTasks.contains(it.metadata.get("mif.correction_task"))})
            throw new IllegalArgumentException("A new outline belongs to an unselected correction task.")
        operations.findAll {it.operation in ["split","merge","add_cell"]}.each {operation ->
            List sourceIDs=operation.operation=="merge" ? operation.source_cell_ids : operation.operation=="split" ? [operation.cell_id] : []
            List sources=sourceIDs.collect {id -> correctionCell(data,scope+operation+[cell_id:id]).cell}
            if(sources.collect {it.metadata.get("analysis_domain_id")}.toSet().size()>1)
                throw new IllegalArgumentException("Merge cells from the same accepted analysis area.")
            List returnedTask=identityOutlines.findAll {it.metadata.get("mif.correction_task")==correctionTaskID(operation).toString()}
            Map groups=returnedTask.groupBy {it.metadata.get("mif.correction_result_id")}
            if((operation.operation=="split" && groups.size()<2) || (operation.operation!="split" && groups.size()!=1))
                throw new IllegalArgumentException(operation.operation=="split" ? "Split the selected cell into at least two reviewed outlines." : "Draw exactly one resulting cell for this task.")
            List created=[];Set modes=[] as Set
            groups.sort().each {id,parts ->
                UUID resultID=UUID.fromString(id as String)
                if(proposed.containsKey(id) || provider.hasCellIdentity(id as String))
                    throw new IllegalArgumentException("Each resulting cell requires a fresh identity.")
                List cells=parts.findAll {it.metadata.get("mif.correction_role")=="cell"}
                List nuclei=parts.findAll {it.metadata.get("mif.correction_role")=="nucleus"}
                if(cells.size()!=1 || nuclei.size()>1 || cells.size()+nuclei.size()!=parts.size() || cells.first().ID!=resultID)
                    throw new IllegalArgumentException("Associate at most one explicit nuclear outline with each resulting cell.")
                String mode=cells.first().metadata.get("mif.correction_nucleus_mode")
                if(!(mode in ["retain","redraw","none"]) || (operation.operation=="add_cell" && mode=="retain"))
                    throw new IllegalArgumentException("Choose explicitly whether to retain, redraw or omit nuclear outlines.")
                modes.add(mode)
                parts.each {part ->
                    String role=part.metadata.get("mif.correction_role")
                    Set keys=["mif.correction_task","mif.correction_role","mif.correction_result_id"] as Set
                    if(role=="cell")keys.add("mif.correction_nucleus_mode")
                    def roi=part.ROI,geometry=roi?.geometry
                    if(!part.isAnnotation() || part.pathClass!=null || part.metadata.keySet()!=keys ||
                            part.name!=(role=="cell" ? "New cell" : "Nuclear outline") || roi==null || roi.c!=-1 || roi.z!=0 || roi.t!=0 ||
                            !(geometry instanceof org.locationtech.jts.geom.Polygonal) || geometry.empty || !geometry.valid ||
                            !Double.isFinite(geometry.area) || geometry.area<=0d || !geometry.coordinates.every {Double.isFinite(it.x)&&Double.isFinite(it.y)})
                        throw new IllegalArgumentException("Draw valid cell and nuclear outlines with their assigned identities.")
                }
                def cellROI=cells.first().ROI,nucleusROI=nuclei.empty?null:nuclei.first().ROI
                if(scope.cell_boundary_source=="nuclear_outline" && (nucleusROI==null || !cellROI.geometry.equalsNorm(nucleusROI.geometry)))
                    throw new IllegalArgumentException("This result contains nuclear outlines: each resulting cell transport must exactly match its nuclear outline.")
                if(nucleusROI!=null && !cellROI.geometry.covers(nucleusROI.geometry))
                    throw new IllegalArgumentException("Keep each nuclear outline inside its explicitly associated cell.")
                if(!area.covers(cellROI.geometry))throw new IllegalArgumentException("Keep each new cell inside the accepted segmentation area.")
                List owners=domains.findAll {key,geometry -> geometry.covers(cellROI.geometry.centroid)}.keySet().toList()
                if(owners.size()!=1 || (!sources.empty && owners!=[sources.first().metadata.get("analysis_domain_id")]))
                    throw new IllegalArgumentException("Keep each new cell in one unambiguous accepted analysis area.")
                def cell=qupath.lib.objects.PathObjects.createCellObject(cellROI,nucleusROI,null)
                cell.setID(resultID);cell.metadata.put("analysis_domain_id",owners.first())
                proposed[id]=[cell:cell,deleted:false,changed:true,created:true,operation:operation.operation,sourceIDs:sourceIDs]
                created << cell
                def calibration=data.server.pixelCalibration
                changes << [cell_id:id,operations:["add_cell"],before:null,
                    after:[cell_geometry_wkb_hex:hex(cellROI),nucleus_geometry_wkb_hex:hex(nucleusROI)],
                    center_um:[cellROI.centroidX*calibration.pixelWidthMicrons,cellROI.centroidY*calibration.pixelHeightMicrons],
                    correction_operation:operation.operation,source_cell_ids:sourceIDs,nucleus_mode:mode]
            }
            if(modes.size()!=1)throw new IllegalArgumentException("Use one explicit nuclear decision for this correction task.")
            List outputNuclei=created.findAll {it.nucleusROI!=null}.collect {it.nucleusROI.geometry}
            List inputNuclei=sources.findAll {it.nucleusROI!=null}.collect {it.nucleusROI.geometry}
            String mode=modes.first()
            if((mode=="none" && !outputNuclei.empty) || (mode=="redraw" && outputNuclei.empty))
                throw new IllegalArgumentException("The returned nuclei differ from the chosen nuclear decision.")
            if(mode=="retain" && (inputNuclei.empty!=outputNuclei.empty || (!inputNuclei.empty &&
                    !org.locationtech.jts.operation.union.UnaryUnionOp.union(inputNuclei).equalsTopo(org.locationtech.jts.operation.union.UnaryUnionOp.union(outputNuclei)))))
                throw new IllegalArgumentException("Retain all original nuclear geometry exactly, or explicitly choose redraw or no nuclei.")
            sources.each {cell ->
                String id=cell.metadata.get("mif.cell_id")
                proposed[id]=[cell:cell,deleted:true,changed:true]
                def calibration=data.server.pixelCalibration
                changes << [cell_id:id,operations:[operation.operation],before:[cell_geometry_wkb_hex:hex(cell.ROI),nucleus_geometry_wkb_hex:hex(cell.nucleusROI)],after:null,
                    center_um:[cell.ROI.centroidX*calibration.pixelWidthMicrons,cell.ROI.centroidY*calibration.pixelHeightMicrons]]
            }
        }
        def targets=new org.locationtech.jts.index.strtree.STRtree()
        proposed.each {id,value -> if(!value.deleted)targets.insert(value.cell.ROI.geometry.envelopeInternal,id)}
        targets.build()
        proposed.each {id,value ->
            if(value.changed && !value.deleted) {
                def geometry=value.cell.ROI.geometry
                boolean overlap=false
                provider.forEachInBounds(geometry.envelopeInternal,{neighbour ->
                    if(!proposed.containsKey(neighbour.metadata.get("mif.cell_id")) &&
                            geometry.intersection(neighbour.ROI.geometry).area>0d)overlap=true
                } as java.util.function.Consumer)
                if(!overlap)overlap=targets.query(geometry.envelopeInternal).any {other ->
                    other!=id && geometry.intersection(proposed[other].cell.ROI.geometry).area>0d}
                if(overlap)throw new IllegalArgumentException("The proposed cell overlaps a final neighbour: "+id+".")
            }
        }
        // Mutation starts only after every target and final neighbor relation passes.
        if(apply)proposed.each {id,value -> if(value.changed) {
            if(value.created)provider.recordCreatedCell(data,value.cell,value.operation as String,value.sourceIDs as List)
            else provider.recordEdit(data,value.cell,value.deleted)
        }}
        return [changes:changes,unchanged_cell_ids:unchanged,
            geometry_revision:provider.geometryRevision,cell_count:provider.currentRowCount]
    }
    /** Existing single-cell callers use the same batch geometry implementation. */
    static Map correctKnownCell(data, Map scope, Map incoming, boolean apply=false) {
        Map result=correctKnownCells(data,scope,incoming,apply)
        if(result.changes.isEmpty())throw new IllegalArgumentException("The outline is unchanged.")
        Map change=result.changes.first()
        return [cell_id:change.cell_id,operation:scope.operation,before:change.before,after:change.after,
            geometry_revision:result.geometry_revision,cell_count:result.cell_count]
    }
    /** Drafts contain ordinary classed points; backend scope fields remain in the ticket. */
    static Map draftObjects(Map snapshot, Map reference, Map tissueCandidate = null) {
        byte[] bytes = new com.google.gson.Gson().toJson(snapshot.objects).getBytes(StandardCharsets.UTF_8)
        def objects = PathIO.readObjectsFromGeoJSON(new ByteArrayInputStream(bytes))
        objects.findAll { ownedReference(it, reference) }.each { object ->
            object.metadata.keySet().findAll { referenceField(it.toString()) }.each { object.metadata.remove(it) }
        }
        objects.findAll { candidateOutline(it, tissueCandidate) }.each { it.setLocked(false) }
        return exportObjects(objects)
    }
    /**
     * Compare the recorded annotation values and its exact normalized geometry.
     *
     * We normalize ring starting vertices, orientation and component order so a
     * native GeoJSON round trip retains the same boundary and holes. Image-plane
     * identity and exported properties are compared separately. The original
     * protected object remains in the hierarchy when incoming references are added.
     */
    static boolean sameAnnotation(original, received, boolean allowOutlineEdit = false) {
        if(original == null || received == null) return false
        def a=original.ROI
        def b=received.ROI
        if(a == null || b == null || a.imagePlane != b.imagePlane ||
                (!allowOutlineEdit && !a.geometry.equalsNorm(b.geometry))) return false
        Map left=new LinkedHashMap(exportObjects([original]).features[0])
        Map right=new LinkedHashMap(exportObjects([received]).features[0])
        left.remove("geometry")
        right.remove("geometry")
        if(allowOutlineEdit) {
            (left.get("properties") as Map).remove("isLocked")
            (right.get("properties") as Map).remove("isLocked")
        }
        return digest(left)==digest(right)
    }
    static Map apply(data,Map baseline,Map incoming,Map reference = null,List primaryParentIds = null,List referenceLabels = null,Map tissueCandidate = null) {
        Map now=snapshot(data, reference)
        if(now.content_sha256!=baseline.content_sha256 || digest(now)!=digest(baseline))
            throw new IllegalArgumentException("The saved annotations changed while this review was open. Refresh before importing.")
        if(incoming.type!="FeatureCollection" || !(incoming.features instanceof List) || incoming.features.size()>MAX_OBJECTS)
            throw new IllegalArgumentException("Export all annotation objects as one GeoJSON FeatureCollection.")
        byte[] bytes=new com.google.gson.GsonBuilder().disableHtmlEscaping().serializeNulls().setObjectToNumberStrategy(com.google.gson.ToNumberPolicy.BIG_DECIMAL).create().toJson(incoming).getBytes(StandardCharsets.UTF_8)
        if(bytes.length>MAX_BYTES)throw new IllegalArgumentException("Annotation review exceeds 32 MiB.")
        def submitted=PathIO.readObjectsFromGeoJSON(new ByteArrayInputStream(bytes))
        if(submitted.size()!=incoming.features.size() || submitted.any {!it.isAnnotation()})
            throw new IllegalArgumentException("This review accepts annotation regions only.")
        Map originalById=data.hierarchy.annotationObjects.collectEntries {[(it.ID.toString()):it]}
        Map receivedById=[:]
        Set allIds=data.hierarchy.getFlattenedObjectList(null).collect {it.ID.toString()} as Set
        submitted.each {object ->
            String id=object.ID.toString()
            if(receivedById.containsKey(id))throw new IllegalArgumentException("Repeated annotation identity.")
            receivedById[id]=object
            def roi=object.ROI;
            def geometry=roi?.geometry
            if(roi==null || roi.z!=0 || roi.t!=0 || geometry.isEmpty() || !geometry.isValid() ||
                !geometry.coordinates.every {Double.isFinite(it.x) && Double.isFinite(it.y) &&
                    it.x>=0d && it.y>=0d && it.x<=data.server.width && it.y<=data.server.height})
                throw new IllegalArgumentException("An annotation has invalid coordinates or falls outside this image.")
            if(!originalById.containsKey(id) && allIds.contains(id))
                throw new IllegalArgumentException("An annotation identity belongs to another object.")
            if(!(id in baseline.protected_ids) && protectedObject(object))
                throw new IllegalArgumentException("Protected pipeline labels cannot be supplied as user annotations.")
            if(reference != null && !(id in baseline.protected_ids) &&
                    object.metadata.keySet().any { referenceField(it.toString()) })
                throw new IllegalArgumentException("Draw points and use Set class; reference identities are attached when the draft is returned.")
        }
        Map correctedOutlines=[:]
        baseline.protected_ids.each {id ->
            def original=originalById[id],received=receivedById[id]
            boolean editableOutline=candidateOutline(original,tissueCandidate)
            if(!receivedById.containsKey(id) ||
                (editableOutline && (!received.ROI.isArea() || received.ROI.geometry.area<=0d)) ||
                !sameAnnotation(original,received,editableOutline))
                throw new IllegalArgumentException("A protected tissue or analysis region was changed or removed.")
            if(editableOutline && !original.ROI.geometry.equalsNorm(received.ROI.geometry))
                correctedOutlines[id]=received.ROI
        }
        List selectedIds=[]
        if(incoming.containsKey("mif_analysis_region_selection")) {
            def selected=incoming.mif_analysis_region_selection
            if(!(selected instanceof List) || selected.isEmpty() || selected.size()>MAX_OBJECTS ||
                    selected.any {!(it instanceof String)} || selected.toSet().size()!=selected.size())
                throw new IllegalArgumentException("Select one or more distinct analysis regions in this image.")
            selected.each {id ->
                def object=receivedById[id]
                if(object==null || !object.ROI.isArea() ||
                        (protectedObject(object) && object.getMetadata().get("pipeline_stage")!="analysis_domain_selection"))
                    throw new IllegalArgumentException("Select drawn area annotations or existing analysis regions from this image.")
            }
            selectedIds=new ArrayList(selected)
        }
        def editable=submitted.findAll {!(it.ID.toString() in baseline.protected_ids)}
        Set addedIds=editable.findAll {!(it.ID.toString() in baseline.editable_ids)}.collect {it.ID.toString()} as Set
        Set changedIds=editable.findAll {originalById.containsKey(it.ID.toString()) &&
            !sameAnnotation(originalById[it.ID.toString()],it)}.collect {it.ID.toString()} as Set
        Set removedIds=baseline.editable_ids.findAll {!receivedById.containsKey(it)} as Set
        Set modifiedIds=addedIds+changedIds
        def modified=editable.findAll {it.ID.toString() in modifiedIds}
        def existing=(changedIds+removedIds).collect {originalById[it]}
        int added=addedIds.size()
        int removed=removedIds.size()
        int changed=changedIds.size()
        Map untouchedParents=baseline.editable_ids.findAll {!(it in changedIds) && !(it in removedIds)}.collectEntries {id ->
            [(id):originalById[id].parent?.ID?.toString()]}
        Map protectedParents=baseline.protected_ids.collectEntries {id ->
            [(id):originalById[id].parent?.ID?.toString()]}
        def selector=null
        if(primaryParentIds!=null) {
            if(!referenceLabels)
                throw new IllegalArgumentException("The reference classes are unavailable. Reopen References.")
            selector=new BiologicalParentSelector(primaryParentIds.collect {id ->
                def parent=originalById[id]
                if(!(id in baseline.protected_ids) || parent?.ROI?.isArea()!=true)
                    throw new IllegalArgumentException("An accepted reference parent is unavailable.")
                new BiologicalParentSelector.Parent(parent,parent.ROI.geometry,id)
            })
        }
        // Bulk removal promotes retained children to the root. Reinsert those
        // detections after the edited regions, using native centroid ownership.
        def descendants=new LinkedHashSet()
        def pending=new ArrayDeque(existing.collectMany {new ArrayList(it.childObjects)})
        while(!pending.isEmpty()) {
            def child=pending.removeFirst()
            if(descendants.add(child))pending.addAll(child.childObjects)
        }
        data.hierarchy.removeObjects(existing,true)
        def remaining=modified
        if(selector!=null) {
            remaining=modified.findAll {object ->
                if(!referenceLabels.contains(object.pathClass?.toString()))return true
                def parent=object.ROI.isArea() ? selector.smallestContaining(object.ROI.geometry) : null
                if(parent==null)return true
                parent.addChildObject(object)
                return false
            }
        }
        data.hierarchy.insertPathObjects(remaining)
        data.hierarchy.insertPathObjects(descendants.findAll {it.isDetection()})
        untouchedParents.each {id,parentId ->
            def object=originalById[id]
            def parent=parentId==data.hierarchy.rootObject.ID.toString() ? data.hierarchy.rootObject : originalById[parentId]
            if(object.parent?.ID?.toString()!=parentId && parent!=null &&
                    (parent==data.hierarchy.rootObject || PathObjectTools.hierarchyContainsObject(data.hierarchy,parent)))
                parent.addChildObject(object)
        }
        baseline.protected_ids.each {id ->
            def object=originalById[id]
            String parentId=protectedParents[id]
            def parent=parentId==data.hierarchy.rootObject.ID.toString() ? data.hierarchy.rootObject : originalById[parentId]
            // Keep existing scientific parents when new reference areas contain protected annotations.
            if(object.parent?.ID?.toString() in modifiedIds && parent!=null &&
                    (parent==data.hierarchy.rootObject || PathObjectTools.hierarchyContainsObject(data.hierarchy,parent)))
                parent.addChildObject(object)
            if(originalById[id].parent?.ID?.toString()!=protectedParents[id])
                throw new IllegalArgumentException("The new regions would change a protected hierarchy relationship.")
        }
        // Preserve the original identity, metadata, lock and hierarchy. The tissue
        // owner records revisions and checks these outlines when saved edits load.
        correctedOutlines.each {id,roi -> originalById[id].setROI(roi)}
        if(incoming.mif_annotation_feedback==true)data.setProperty("mif.annotation_feedback","model_assisted")
        Map changes=[added:added,changed:changed+correctedOutlines.size(),removed:removed,protected_count:baseline.protected_ids.size()]
        if(!selectedIds.isEmpty())changes.selected_region_ids=selectedIds
        return changes
    }
}
return [snapshot:ClusterAnnotationEdits.&snapshot,apply:ClusterAnnotationEdits.&apply,
        removeCells:ClusterAnnotationEdits.&removeCells,
        knownCellDraft:ClusterAnnotationEdits.&knownCellDraft,correctKnownCell:ClusterAnnotationEdits.&correctKnownCell,
        correctKnownCells:ClusterAnnotationEdits.&correctKnownCells,correctionOperations:ClusterAnnotationEdits.&correctionOperations,
        correctionTaskID:ClusterAnnotationEdits.&correctionTaskID,newCorrectionOutline:ClusterAnnotationEdits.&newCorrectionOutline,
        splitCorrectionOutline:ClusterAnnotationEdits.&splitCorrectionOutline,exactCorrectionROI:ClusterAnnotationEdits.&exactCorrectionROI,
        draftObjects:ClusterAnnotationEdits.&draftObjects,ownedReference:ClusterAnnotationEdits.&ownedReference,
        exportObjects:ClusterAnnotationEdits.&exportObjects,
        protectedObject:ClusterAnnotationEdits.&protectedObject]
