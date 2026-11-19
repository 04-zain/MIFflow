// Measure only the classified objects in the selected saved inventory. Never save image data.
import java.nio.file.Files
import java.nio.file.Path
import java.nio.charset.StandardCharsets
import java.security.MessageDigest
import java.util.function.Consumer
import org.locationtech.jts.io.WKBWriter
import org.locationtech.jts.io.ByteOrderValues
import org.locationtech.jts.io.WKBReader
import org.locationtech.jts.operation.union.UnaryUnionOp
import org.mif.roi.RoiInventory
import qupath.lib.io.PathIO

class SavedClassifiedRegions {
    static String digest(byte[] bytes) {
        MessageDigest.getInstance('SHA-256').digest(bytes).encodeHex().toString()
    }
    static Map measure(object, RoiInventory.Row row, Map request) {
        Map m=object?.metadata
        if (object==null || m.roi_id!=row.roiId() || object.ID.toString()!=row.pathObjectId() ||
                m.object_revision_sha256!=row.objectRevisionSha256() ||
                m.primary_class_id!=row.classId() || m.pipeline_stage!='primary_classifier' ||
                m.pipeline_active!='true' || m.analysis_domain_id!=request.analysis_domain_id ||
                object.parent?.ID?.toString()!=request.analysis_domain_path_object_id)
            throw new IllegalStateException('A classified region differs from its saved result. Reopen the current result.')
        def roi=object.ROI
        if (roi==null || !roi.isArea() || roi.imagePlane!=object.parent.ROI.imagePlane)
            throw new IllegalStateException('A classified region has no area on its analysis plane.')
        def geometry=roi.geometry
        def normalized=geometry.copy(); normalized.normalize()
        String geometrySha=digest(new WKBWriter(2,ByteOrderValues.LITTLE_ENDIAN,false).write(normalized))
        // Match the producer's revision using the observed boundary, class and parent.
        Map revision=new TreeMap([
            analysis_domain_id:request.analysis_domain_id,
            analysis_domain_revision_sha256:request.analysis_domain_revision_sha256,
            analysis_domain_set_sha256:request.domain_set_sha256,
            classification:object.classification,
            geometry_sha256:geometrySha,
            parent_path_object_id:object.parent.ID.toString(),
            path_object_id:object.ID.toString(),
            pipeline_active:m.pipeline_active,
            type:'primary', type_id:row.classId()])
        String observed=digest(qupath.lib.io.GsonTools.getInstance().newBuilder().serializeNulls().create()
            .toJson(revision).getBytes(StandardCharsets.UTF_8))
        if (observed!=row.objectRevisionSha256())
            throw new IllegalStateException('A classified region boundary or class changed after this saved result.')
        double area=geometry.area*(request.x_um_per_px as double)*(request.y_um_per_px as double)
        if (!(area>0d) || !Double.isFinite(area))
            throw new IllegalStateException('The classified region has no finite calibrated area.')
        def bounds=geometry.envelopeInternal
        return [roi_id:row.roiId(),path_object_id:row.pathObjectId(),class_id:row.classId(),area_um2:area,
                bounds_level0:[bounds.minX,bounds.minY,bounds.width,bounds.height]]
    }
    static Map read(Map request) {
        if (![request.x_um_per_px,request.y_um_per_px].every {
                it instanceof Number && Double.isFinite(it.doubleValue()) && it.doubleValue()>0d })
            throw new IllegalArgumentException('The accepted image has no calibrated pixel size.')
        def hierarchy=PathIO.readHierarchy(Path.of(request.hierarchy_file as String))
        if(hierarchy==null)throw new IllegalStateException('The saved image hierarchy is unavailable.')
        Map byId=hierarchy.annotationObjects.collectEntries { [(it.ID.toString()):it] }
        List regions=[]
        RoiInventory.read(Path.of(request.inventory_root as String),request.roi_inventory as Map,
            RoiInventory.member(request.producer_attempt_id as String), { RoiInventory.Row row ->
                regions.add(measure(byId[row.pathObjectId()],row,request))
            } as Consumer<RoiInventory.Row>)
        return [regions:regions]
    }

    static Map segmentationUnion(Map request) {
        // The caller supplies exact accepted domain bytes and inventory owners.
        // Reuse the existing boundary/revision checks before forming the same
        // native union used by segmentation. No image server or project save.
        def hierarchy=PathIO.readHierarchy(Path.of(request.hierarchy_file as String))
        if(hierarchy==null)throw new IllegalStateException('The saved image hierarchy is unavailable.')
        Map byId=hierarchy.annotationObjects.collectEntries { [(it.ID.toString()):it] }
        def decoder=new WKBReader(qupath.lib.roi.GeometryTools.getDefaultFactory())
        List domains=[];List primary=[]
        request.domains.each { domain ->
            def geometry=decoder.read(WKBReader.hexToBytes(domain.geometry_wkb_hex as String))
            if(!geometry.isValid() || geometry.isEmpty())throw new IllegalStateException('The accepted segmentation domain is invalid.')
            domains.add(geometry)
            if(request.scope=='configured_union') {
                Map owner=domain.primary as Map
                if(owner==null)throw new IllegalStateException('The configured segmentation domain lacks its accepted primary regions.')
                RoiInventory.read(Path.of(owner.inventory_root as String),owner.roi_inventory as Map,
                    RoiInventory.member(owner.producer_attempt_id as String), { RoiInventory.Row row ->
                        def object=byId[row.pathObjectId()]
                        measure(object,row,owner)
                        if(row.classId()!='Ignore')primary.add(object.ROI.geometry)
                    } as Consumer<RoiInventory.Row>)
            }
        }
        if(domains.isEmpty())throw new IllegalStateException('There is no included segmentation area to inspect.')
        def geometry=UnaryUnionOp.union(domains)
        if(request.scope=='configured_union') {
            if(primary.isEmpty())throw new IllegalStateException('The configured segmentation union has no primary regions.')
            geometry=geometry.intersection(UnaryUnionOp.union(primary))
        } else if(request.scope!='accepted_tissue')throw new IllegalArgumentException('Unsupported segmentation scope.')
        if(!geometry.isValid() || geometry.isEmpty())throw new IllegalStateException('The saved segmentation union is invalid.')
        geometry.normalize()
        byte[] bytes=new WKBWriter(2,ByteOrderValues.LITTLE_ENDIAN,false).write(geometry)
        if(digest(bytes)!=request.union_geometry_sha256)
            throw new IllegalStateException('The reconstructed area differs from the accepted segmentation union.')
        return [geometry_wkb_hex:org.locationtech.jts.io.WKBWriter.toHex(bytes),union_geometry_sha256:request.union_geometry_sha256]
    }
}

if(args.size()!=2)throw new IllegalArgumentException('Expected request and result paths')
Path result=Path.of(args[1])
if(Files.exists(result))throw new IllegalArgumentException('The result destination already exists')
def gson=new com.google.gson.GsonBuilder().disableHtmlEscaping().create()
Map request=gson.fromJson(Files.readString(Path.of(args[0])),Map.class)
Files.writeString(result,gson.toJson(request.segmentation_union == true ?
    SavedClassifiedRegions.segmentationUnion(request) : SavedClassifiedRegions.read(request)),StandardCharsets.UTF_8)
