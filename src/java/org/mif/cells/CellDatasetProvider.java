package org.mif.cells;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Base64;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentHashMap;
import java.util.function.Consumer;
import java.util.function.Function;
import org.apache.parquet.example.data.Group;
import org.locationtech.jts.geom.Envelope;
import org.locationtech.jts.index.strtree.STRtree;
import org.locationtech.jts.io.WKBReader;
import org.locationtech.jts.io.WKBWriter;
import qupath.lib.images.ImageData;
import qupath.lib.objects.PathObject;
import qupath.lib.objects.PathObjects;
import qupath.lib.objects.classes.PathClass;
import qupath.lib.regions.ImagePlane;
import qupath.lib.roi.GeometryTools;

/** Resolve one immutable population once and read only admitted native groups. */
public final class CellDatasetProvider implements AutoCloseable,NativeCellSource {
    public static final String DATASET_PROPERTY = "mif.cell_datasets";
    public static final String EDITS_PROPERTY = "mif.cell_edits";
    private final Path root;
    private JsonObject description;
    private final String population;
    private final String descriptionSha;
    private JsonObject acceptedReceipt;
    private final List<Layout> layout = new ArrayList<>();
    private final Map<Long,Layout> byId = new HashMap<>();
    private STRtree index = new STRtree();
    private final Map<String,Map<Integer,PathClass>> classes = new HashMap<>();
    private final Map<String,Map<Integer,String>> labels = new HashMap<>();
    private final Map<String,Map<Integer,Integer>> colors = new HashMap<>();
    private final long memoryBudget;
    private final Map<String,CompletableFuture<Snapshot>> pending = new ConcurrentHashMap<>();
    private final Map<String,Edit> edits = new ConcurrentHashMap<>();
    private long groupReads, geometryDecodes;
    private volatile boolean closed;
    private volatile long revision;
    private volatile CellComponents components;
    private volatile long geometryRevision;
    private record Layout(long id, Path file, int group, int rows, long bytes, Envelope bounds) {}
    private record Edit(PathObject object, boolean deleted, long revision) {}
    private static final String CREATED_GROUP = "mif.created_group_id";
    private static long createdGroup(PathObject cell) {
        String value=cell.getMetadata().get(CREATED_GROUP);
        return value==null?-1:Long.parseLong(value);
    }
    private List<Map.Entry<String,Edit>> creations() {
        return edits.entrySet().stream().filter(item->createdGroup(item.getValue().object)>=0)
                .sorted(java.util.Comparator.comparingLong(item->createdGroup(item.getValue().object))).toList();
    }
    /** A prepared display payload relinquishes geometry when its process reservation is evicted. */
    public static final class Snapshot {
        private volatile List<PathObject> objects;
        private volatile int[] density;
        private final Envelope bounds;
        private final boolean overview;
        private final long revision,memoryBytes;
        private volatile boolean retired;
        private Runnable release=()->{};
        private final List<java.lang.ref.WeakReference<Snapshot>> dependents=new ArrayList<>();
        public Snapshot(List<PathObject> objects,int[] density,Envelope bounds,boolean overview,long revision,long memoryBytes){this.objects=objects;this.density=density;this.bounds=bounds;this.overview=overview;this.revision=revision;this.memoryBytes=memoryBytes;}
        public List<PathObject> objects(){return objects;}
        public int[] density(){return density;}
        public Envelope bounds(){return bounds;}
        public boolean overview(){return overview;}
        public long revision(){return revision;}
        public long memoryBytes(){return retired?0:memoryBytes;}
        public boolean retired(){return retired;}
        synchronized void onRetire(Runnable action){release=action;if(retired)action.run();}
        /** We make composite views relinquish their references when any underlying section is evicted. */
        synchronized void dependent(Snapshot view){if(retired)view.retire();else {dependents.removeIf(reference->reference.get()==null||reference.get().retired());dependents.add(new java.lang.ref.WeakReference<>(view));}}
        void retire(){List<java.lang.ref.WeakReference<Snapshot>> views;Runnable action;synchronized(this){if(retired)return;retired=true;objects=List.of();density=null;views=List.copyOf(dependents);dependents.clear();action=release;release=()->{};}views.forEach(reference->{var view=reference.get();if(view!=null)view.retire();});action.run();}
    }
    private static final Set<String> DISPLAY_FIELDS = Set.of("primary_phenotype");
    private static final List<String> VIEW_COLUMNS = List.of("cell_id", "x_level0", "y_level0", "cell_geometry", "nucleus_geometry", "min_x", "min_y", "max_x", "max_y", "primary_phenotype", "section_id", "analysis_domain_id", "tissue_roi_id");

    /**
     * We open a completed cell descriptor on a background thread and index its
     * full-outline group bounds once. The byte allowance bounds group admission,
     * category metadata, selected scalar joins and prepared view residency.
     * Unsupported formats, escaping paths and oversized groups or indexes fail
     * at opening before scientific or display consumers receive any cells.
     */
    public CellDatasetProvider(Path path, long memoryBudget) throws IOException {
        LocalParquet.requireWorker();
        this.root = (Files.isDirectory(path) ? path : path.getParent()).toAbsolutePath().normalize();
        this.memoryBudget = memoryBudget;
        if (memoryBudget < 8*1024*1024) throw new IllegalArgumentException("Cell provider requires one admitted row group");
        try {
        // i) Establish the completed base identity before constructing native objects.
        long metadataCost=Math.multiplyExact(Files.size(root.resolve("dataset.json")),8);
        SharedCellCache.metadata(this,metadataCost);
        byte[] metadata=Files.readAllBytes(root.resolve("dataset.json"));description = JsonParser.parseString(new String(metadata,java.nio.charset.StandardCharsets.UTF_8)).getAsJsonObject();
        try{descriptionSha=java.util.HexFormat.of().formatHex(java.security.MessageDigest.getInstance("SHA-256").digest(metadata));}catch(java.security.NoSuchAlgorithmException impossible){throw new AssertionError(impossible);}
        if (!"mif-cell-dataset/v1".equals(description.get("schema_version").getAsString())) throw new IOException("Unsupported cell population format");
        population = description.getAsJsonObject("scope").get("population_id").getAsString();
        readDictionaries();
        // ii) Build the full-outline group index; category classes remain lazy.
        LocalParquet.read(resolve(description.get("layout").getAsString()), null, null, row -> {
            var entry = new Layout(row.getLong("group_id",0), resolve(row.getString("file",0)), row.getInteger("row_group",0), row.getInteger("row_count",0), row.getLong("decoded_bytes",0), envelope(row));
            if (entry.bytes > memoryBudget || entry.rows > 8192) throw new IllegalArgumentException("Cell group exceeds this session's row or byte admission");
            SharedCellCache.metadata(this,metadataCost+(layout.size()+1L)*384);
            layout.add(entry); byId.put(entry.id,entry); index.insert(entry.bounds,entry);
            if ((long)layout.size()*192 > Math.min(memoryBudget/4,64L*1024*1024)) throw new IllegalArgumentException("Cell layout exceeds the index memory allowance");
        });
        index.build();
        }catch(IOException|RuntimeException|Error failure){SharedCellCache.releaseMetadata(this);throw failure;}
    }
    private Path resolve(String relative) {
        Path result=root.resolve(relative).normalize();
        if (!result.startsWith(root)) throw new IllegalArgumentException("Cell dataset path escapes its selected generation");
        return result;
    }
    private static Envelope envelope(Group row) { return new Envelope(row.getDouble("min_x",0),row.getDouble("max_x",0),row.getDouble("min_y",0),row.getDouble("max_y",0)); }
    private void readDictionaries() {
        for (var field : description.getAsJsonObject("dictionaries").entrySet()) {
            var values = new HashMap<Integer,PathClass>();var names=new HashMap<Integer,String>();var palette=new HashMap<Integer,Integer>();
            for (var raw : field.getValue().getAsJsonObject().getAsJsonArray("entries")) {
                var entry=raw.getAsJsonObject();int code=entry.get("code").getAsInt();String name=entry.get("name").getAsString();
                Integer color=CellComponents.color(entry.get("color"));
                palette.put(code,color);names.put(code,name);
            }
            classes.put(field.getKey(),values);labels.put(field.getKey(),names);colors.put(field.getKey(),palette);
        }
    }
    private synchronized PathClass category(String field,int code) {
        String name=labels.get(field).get(code);if(name==null)throw new IllegalArgumentException("Undeclared category code "+code+" in "+field);
        return classes.get(field).computeIfAbsent(code,key->PathClass.fromString(name,colors.get(field).get(code)));
    }
    /**
     * We resolve field-to-scope component references against this population and
     * layout, then invalidate prepared views. Other section populations are
     * excluded; incomplete results and conflicting layouts are rejected.
     */
    public void selectComponents(String references)throws IOException {
        LocalParquet.requireWorker();var previous=components;components=new CellComponents(references,population,description.get("layout_version").getAsString(),memoryBudget/4);if(previous!=null)previous.close();
        revision++;
        SharedCellCache.release(this);
    }
    /** We bind the compact accepted receipt only when its population, base digest and count identify this store. */
    public void setAcceptedReceipt(JsonObject receipt)throws IOException {
        if(receipt==null){acceptedReceipt=null;return;}
        var evidence=receipt.getAsJsonObject("cell_dataset");
        if(evidence==null||!population.equals(receipt.get("segmentation_population_id").getAsString())||
                !descriptionSha.equals(evidence.get("sha256").getAsString())||
                evidence.get("row_count").getAsLong()!=description.get("row_count").getAsLong()||
                !descriptionSha.equals(receipt.get("cell_evidence_sha256").getAsString()))
            throw new IOException("Selected segmentation receipt does not identify this immutable cell base");
        acceptedReceipt=receipt.deepCopy();
    }
    private Integer code(Group row,String field) { return row.getFieldRepetitionCount(field)==0?null:row.getInteger(field,0); }
    /** We restore exact WKB vertices and the declared native factory used for shape arithmetic. */
    private org.locationtech.jts.geom.Geometry geometry(byte[] bytes)throws org.locationtech.jts.io.ParseException {
        var decoded=new WKBReader().read(bytes);
        var profile=description.getAsJsonObject("scope").get("native_geometry_factory");
        // Decode exact WKB first: WKBReader's precision model must not snap coordinates.
        return profile!=null&&"qupath-default-packed-float".equals(profile.getAsString())?GeometryTools.getDefaultFactory().createGeometry(decoded):decoded;
    }
    /**
     * We preserve arbitrary double WKB through the native geometry ROI constructor.
     * The public converter simplifies simple polygons and rounds their vertices;
     * its declared packed-float profile remains necessary for native segmentation
     * shape arithmetic. QuPath 0.7 exposes no public exact geometry ROI factory, so
     * we reach only its two-argument constructor and copy the declared geometry.
     * An unavailable native constructor fails before returning changed outlines.
     */
    private qupath.lib.roi.interfaces.ROI roi(org.locationtech.jts.geom.Geometry geometry) {
        var profile=description.getAsJsonObject("scope").get("native_geometry_factory");
        if(profile!=null&&"qupath-default-packed-float".equals(profile.getAsString()))return GeometryTools.geometryToROI(geometry,ImagePlane.getDefaultPlane());
        return exactRoi(geometry);
    }
    private static qupath.lib.roi.interfaces.ROI exactRoi(org.locationtech.jts.geom.Geometry geometry) {
        try {return ExactGeometry.CTOR.newInstance(geometry,ImagePlane.getDefaultPlane());}
        catch(ReflectiveOperationException failure){throw new IllegalStateException("QuPath cannot construct the declared exact double geometry",failure);}
    }
    private static final class ExactGeometry {
        private static final java.lang.reflect.Constructor<qupath.lib.roi.GeometryROI> CTOR=constructor();
        private static java.lang.reflect.Constructor<qupath.lib.roi.GeometryROI> constructor(){
            try{var ctor=qupath.lib.roi.GeometryROI.class.getDeclaredConstructor(org.locationtech.jts.geom.Geometry.class,ImagePlane.class);ctor.setAccessible(true);return ctor;}
            catch(ReflectiveOperationException failure){throw new ExceptionInInitializerError(failure);}
        }
    }
    /** We hydrate one typed base row with native geometry, stable identity, measurements and accepted lineage. */
    private PathObject object(Group row) {
        try {
            geometryDecodes++;
            var cell=roi(geometry(row.getBinary("cell_geometry",0).getBytes()));
            var nucleus=row.getFieldRepetitionCount("nucleus_geometry")==0?null:roi(geometry(row.getBinary("nucleus_geometry",0).getBytes()));
            Integer code=code(row,"primary_phenotype");
            PathClass cls=code==null?null:category("primary_phenotype",code);
            if (code!=null && cls==null) throw new IllegalArgumentException("Undeclared phenotype category code "+code);
            var result=PathObjects.createCellObject(cell,nucleus,cls);
            String id=row.getString("cell_id",0);
            try {result.setID(UUID.fromString(id));}catch(IllegalArgumentException nonUuid){result.setID(UUID.nameUUIDFromBytes((population+":"+id).getBytes(java.nio.charset.StandardCharsets.UTF_8)));}
            result.getMetadata().put("cell_id",id);result.getMetadata().put("mif.cell_id",id);result.getMetadata().put("mif.population_id",population);result.getMetadata().put("segmentation_population_id",population);
            result.getMetadata().put("mif.geometry_identity",geometryIdentity(result));result.getMetadata().put("mif.geometry_revision","0");
            result.getMetadata().put("cell_evidence_sha256",descriptionSha);
            if(acceptedReceipt!=null)for(String name:List.of("domain_cell_mapping_sha256","section_population_sha256","cell_evidence_sha256"))if(acceptedReceipt.has(name))result.getMetadata().put("section_population_sha256".equals(name)?"segmentation_section_population_sha256":name,acceptedReceipt.get(name).getAsString());
            for(var field:description.getAsJsonObject("scope").entrySet())if(field.getValue().isJsonPrimitive())result.getMetadata().put(field.getKey(),field.getValue().getAsString());
            var domains=description.getAsJsonObject("scope").getAsJsonObject("domains");
            if(domains!=null && row.getType().containsField("analysis_domain_id") && row.getFieldRepetitionCount("analysis_domain_id")>0) {
                var domain=domains.getAsJsonObject(row.getString("analysis_domain_id",0));
                if(domain==null)throw new IllegalArgumentException("Cell domain is absent from its selected lineage");
                for(var field:domain.entrySet())if(field.getValue().isJsonPrimitive())result.getMetadata().put(field.getKey(),field.getValue().getAsString());
            }
            for (String field : row.getType().getFields().stream().map(t->t.getName()).toList()) {
                if (row.getFieldRepetitionCount(field)==0) continue;
                if (field.startsWith("measurement:")) result.getMeasurementList().put(field.substring(12),row.getDouble(field,0));
                else if (Set.of("section_id","analysis_domain_id","tissue_roi_id","slide_id","tissue_generation_id","phenotyping_generation_id","pipeline_run_id","membership_generation_id").contains(field)) result.getMetadata().put(field,row.getString(field,0));
                else if (labels.containsKey(field)) {
                    if(!row.getType().getType(field).isPrimitive()) {
                        var list=row.getGroup(field,0);var names=new com.google.gson.JsonArray();
                        for(int i=0;i<list.getFieldRepetitionCount("list");i++){int category=list.getGroup("list",i).getInteger("element",0);String name=labels.get(field).get(category);if(name==null)throw new IllegalArgumentException("Undeclared base list category "+category+" in "+field);names.add(name);}
                        result.getMetadata().put(field,names.toString());
                    } else {Integer category=code(row,field);if(category!=null)result.getMetadata().put(field,labels.get(field).get(category));}
                }
            }
            return result;
        } catch (org.locationtech.jts.io.ParseException failure) { throw new IllegalArgumentException("Invalid cell geometry "+row.getString("cell_id",0),failure); }
    }
    /** We share a background rectangle read and return a detachable future; peer cancellation preserves the shared result. */
    public CompletableFuture<Snapshot> request(Envelope bounds) {
        if (closed) return CompletableFuture.failedFuture(new IllegalStateException("Cell provider is closed"));
        long capturedRevision=revision;String key=capturedRevision+":"+bounds;
        var resident=(Snapshot)SharedCellCache.get(this,key);if(resident!=null&&!resident.retired())return CompletableFuture.completedFuture(resident);
        var shared=pending.computeIfAbsent(key,k->{
            var future=new CompletableFuture<Snapshot>();
            try { SharedCellCache.execute(()->{try{if(closed)throw new IllegalStateException("Cell provider is closed");future.complete(query(bounds));}catch(Throwable error){future.completeExceptionally(error);}}); }
            catch (java.util.concurrent.RejectedExecutionException saturated) {future.completeExceptionally(saturated);}
            return future;
        });
        shared.whenComplete((result,error)->pending.remove(key,shared));
        return shared.thenApply(result->result); // Detachable consumer stage.
    }
    /**
     * We project candidate groups for the requested level-zero rectangle, apply
     * selected results and sparse edits, and return native outlines or a labelled
     * density overview within view admission. Scientific ROI iteration uses the
     * separate complete-cell API; storage failures return errors instead of an
     * apparently empty completed view.
     */
    public Snapshot query(Envelope bounds) throws IOException {
        LocalParquet.requireWorker();if(closed)throw new IllegalStateException("Cell provider is closed");
        var selectedComponents=components;
        long capturedRevision=revision;String key=capturedRevision+":"+bounds;
        var resident=(Snapshot)SharedCellCache.get(this,key);if(resident!=null&&!resident.retired())return resident;
        @SuppressWarnings("unchecked") List<Layout> candidates=index.query(bounds);
        boolean overview=candidates.stream().mapToLong(entry->entry.rows).sum()>20000 || candidates.stream().mapToLong(entry->entry.bytes).sum()*8>memoryBudget/4;
        var objects=new ArrayList<PathObject>();int[] density=overview?new int[64*64]:null;
        var viewGeometry=overview?null:GeometryTools.getDefaultFactory().toGeometry(bounds);
        long[] used={0};
        for (var entry:candidates) {
            groupReads++;
            List<String> columns=overview?List.of("cell_id","x_level0","y_level0","min_x","min_y","max_x","max_y"):VIEW_COLUMNS;
            var joined=!overview && selectedComponents!=null?selectedComponents.group(entry.id,entry.rows,DISPLAY_FIELDS):null;int[] offset={0};
            LocalParquet.read(entry.file,entry.group,columns,row->{
                int baseOffset=offset[0]++;
                String id=row.getString("cell_id",0);Edit edit=edits.get(id);
                if (edit!=null) return;
                if (!envelope(row).intersects(bounds)) return;
                if (overview) {double x=row.getDouble("x_level0",0),y=row.getDouble("y_level0",0);if(bounds.covers(x,y)) density[bin(bounds,x,y)]++;return;}
                used[0]+=row.getBinary("cell_geometry",0).length()*8L+512;
                if(row.getFieldRepetitionCount("nucleus_geometry")>0)used[0]+=row.getBinary("nucleus_geometry",0).length()*8L;
                if (used[0]>memoryBudget/4) throw new IllegalArgumentException("Exact cell display exceeds its admitted geometry budget; zoom in");
                PathObject cell=object(row);
                if(selectedComponents!=null)selectedComponents.apply(joined,baseOffset,id,cell,0);
                if(cell.getROI().getGeometry().intersects(viewGeometry))objects.add(cell);
            });
        }
        // Edited bounds have their own effective spatial association, independent of base home block.
        for (var pair:edits.entrySet()) {var edit=pair.getValue();if(edit.deleted || !edit.object.getROI().getGeometry().getEnvelopeInternal().intersects(bounds))continue;
            if (overview) {var roi=edit.object.getROI();if(bounds.covers(roi.getCentroidX(),roi.getCentroidY()))density[bin(bounds,roi.getCentroidX(),roi.getCentroidY())]++;}
            else {
                // Bounds locate candidates; the edited outline must meet the same exact view test as a base cell.
                if(!edit.object.getROI().getGeometry().intersects(viewGeometry))continue;
                PathObject displayed=lookup(pair.getKey(),true);if(displayed==null)continue;
                used[0]+=displayed.getROI().getGeometry().getNumPoints()*64L+512;
                var nucleus=((qupath.lib.objects.PathCellObject)displayed).getNucleusROI();if(nucleus!=null)used[0]+=nucleus.getGeometry().getNumPoints()*64L;
                if(used[0]>memoryBudget/4)throw new IllegalArgumentException("Edited cell display exceeds its admitted geometry budget; zoom in");
                objects.add(displayed);
            }
        }
        var result=new Snapshot(List.copyOf(objects),density,new Envelope(bounds),overview,capturedRevision,overview?64*64*4:used[0]);
        if(!closed&&capturedRevision==revision){
            SharedCellCache.put(this,key,result,result.memoryBytes(),result::retire);
            result.onRetire(()->SharedCellCache.remove(this,key,result));
        } else result.retire();
        return result;
    }
    private static int bin(Envelope bounds,double x,double y) {int bx=(int)((x-bounds.getMinX())/Math.max(1,bounds.getWidth())*64);int by=(int)((y-bounds.getMinY())/Math.max(1,bounds.getHeight())*64);return Math.max(0,Math.min(63,by))*64+Math.max(0,Math.min(63,bx));}
    /**
     * We stream stable cell/domain identities from projected base columns and
     * the current sparse checkpoint. Deleted edits are excluded, effective edit
     * metadata supplies its domain, and concurrent revisions invalidate the
     * traversal. This reads no geometry, measurements or selected result groups.
     */
    public void forEachIdentity(java.util.function.BiConsumer<String,String> consumer)throws IOException {
        LocalParquet.requireWorker();if(closed)throw new IllegalStateException("Cell provider is closed");long originalRevision=revision;
        for(var entry:layout){groupReads++;LocalParquet.read(entry.file,entry.group,List.of("cell_id","analysis_domain_id"),row->{
            String id=row.getString("cell_id",0);Edit edit=edits.get(id);if(edit!=null&&edit.deleted)return;
            String domain=edit==null?row.getString("analysis_domain_id",0):edit.object.getMetadata().get("analysis_domain_id");
            if(domain==null||domain.isBlank())throw new IllegalArgumentException("Effective cell identity lacks its accepted domain");
            consumer.accept(id,domain);
        });}
        for(var item:creations())if(!item.getValue().deleted)
            consumer.accept(item.getKey(),item.getValue().object.getMetadata().get("analysis_domain_id"));
        if(originalRevision!=revision)throw new IOException("Cell edits changed during identity iteration");
    }
    /** Whole-population work streams one physical group and one native cell at a time. */
    public void forEach(Consumer<PathObject> consumer) throws IOException {
        forEachSelected(components,null,consumer);
    }
    /** Stream the selected scalar fields with the same base geometry, IDs and edits. */
    public void forEach(Set<String> inputFields,Consumer<PathObject> consumer) throws IOException {
        forEachSelected(components,Set.copyOf(inputFields),consumer);
    }
    /** We hydrate one physical group at a time and clone edited cells; a concurrent edit invalidates this scientific traversal. */
    private void forEachSelected(CellComponents selectedComponents,Set<String> projection,Consumer<PathObject> consumer)throws IOException {
        LocalParquet.requireWorker();if(closed)throw new IllegalStateException("Cell provider is closed");long originalRevision=revision;Set<String> seen=new HashSet<>();
        for (var entry:layout) {
            var joined=selectedComponents!=null?selectedComponents.group(entry.id,entry.rows,projection):null;int[] offset={0};
            groupReads++;LocalParquet.read(entry.file,entry.group,null,row->{
                int baseOffset=offset[0]++;
                String id=row.getString("cell_id",0);Edit edit=edits.get(id);
                if(edit==null){var cell=object(row);if(selectedComponents!=null)selectedComponents.apply(joined,baseOffset,id,cell,0);consumer.accept(cell);}else {seen.add(id);if(!edit.deleted){var cell=copyEdit(edit.object);if(selectedComponents!=null)selectedComponents.apply(joined,baseOffset,id,cell,editGeometryRevision(cell));consumer.accept(cell);}}
            });
        }
        for(var pair:creations())if(!pair.getValue().deleted) {
            var cell=copyEdit(pair.getValue().object);
            if(selectedComponents!=null)selectedComponents.apply(selectedComponents.group(createdGroup(cell),1,projection),0,pair.getKey(),cell,editGeometryRevision(cell));
            consumer.accept(cell);
        }
        for(var pair:edits.entrySet())if(!seen.contains(pair.getKey()) && createdGroup(pair.getValue().object)<0)
            throw new IOException("Saved edit refers to an ID outside the immutable cell base");
        if(originalRevision!=revision)throw new IOException("Cell edits changed during a complete-population operation; retry against the selected revision");
    }
    /**
     * We identify rectangles that may contain accepted cell pixels without
     * reading a cell table or hydrating geometry. Complete row-group outlines
     * conservatively cover immutable cells; moved, nondeleted edits are checked
     * separately because they may lie outside their original group. A positive
     * result can include empty space within a group. A negative result lets a
     * caller omit work that cannot contribute to any current cell.
     */
    public boolean mayHaveCellsInBounds(Envelope bounds) throws IOException {
        LocalParquet.requireWorker();
        if (closed) throw new IllegalStateException("Cell provider is closed");
        long originalRevision = revision;
        boolean possible = !index.query(bounds).isEmpty();
        if (!possible) {
            for (var edit : edits.values()) {
                if (!edit.deleted && edit.object.getROI().getGeometry()
                        .getEnvelopeInternal().intersects(bounds)) {
                    possible = true;
                    break;
                }
            }
        }
        if (originalRevision != revision)
            throw new IOException("Cell edits changed during cell-bound selection");
        return possible;
    }

    /** Exact scientific ROI iteration; density display limits never replace cells. */
    public void forEachInBounds(Envelope bounds,Consumer<PathObject> consumer)throws IOException {
        LocalParquet.requireWorker();if(closed)throw new IllegalStateException("Cell provider is closed");long originalRevision=revision;var selectedComponents=components;
        var rectangle=new org.locationtech.jts.geom.GeometryFactory().toGeometry(bounds);
        @SuppressWarnings("unchecked") List<Layout> candidates=index.query(bounds);
        for(var entry:candidates){var joined=selectedComponents!=null?selectedComponents.group(entry.id,entry.rows):null;int[] offset={0};
            groupReads++;LocalParquet.read(entry.file,entry.group,null,row->{int baseOffset=offset[0]++;String id=row.getString("cell_id",0);if(edits.containsKey(id))return;
                var cell=object(row);if(!cell.getROI().getGeometry().intersects(rectangle))return;
                if(selectedComponents!=null)selectedComponents.apply(joined,baseOffset,id,cell,0);consumer.accept(cell);
            });
        }
        // Moved cells can intersect this ROI from a different immutable home group.
        for(var pair:edits.entrySet())if(!pair.getValue().deleted&&pair.getValue().object.getROI().getGeometry().intersects(rectangle)){var cell=lookup(pair.getKey());if(cell!=null)consumer.accept(cell);}
        if(originalRevision!=revision)throw new IOException("Cell edits changed during scientific ROI iteration");
    }
    /**
     * We stream cells whose native ROI centroid lies in the
     * inclusive envelope. The base outline index selects candidate groups, then
     * centroid coordinates decide inclusion even for an interior polygon hole.
     * Moved sparse edits are searched separately; deleted cells are excluded.
     * A changed edit revision invalidates the traversal rather than mixing views.
     */
    public void forEachCentroidInBounds(Envelope bounds,Consumer<PathObject> consumer)throws IOException {
        LocalParquet.requireWorker();if(closed)throw new IllegalStateException("Cell provider is closed");long originalRevision=revision;var selectedComponents=components;
        @SuppressWarnings("unchecked") List<Layout> candidates=index.query(bounds);
        // i) Read only spatially relevant groups, retaining one hydrated cell.
        for(var entry:candidates){var joined=selectedComponents!=null?selectedComponents.group(entry.id,entry.rows):null;int[] offset={0};
            groupReads++;LocalParquet.read(entry.file,entry.group,null,row->{int baseOffset=offset[0]++;String id=row.getString("cell_id",0);if(edits.containsKey(id))return;
                var cell=object(row);if(!bounds.covers(cell.getROI().getCentroidX(),cell.getROI().getCentroidY()))return;
                if(selectedComponents!=null)selectedComponents.apply(joined,baseOffset,id,cell,0);consumer.accept(cell);
            });
        }
        // ii) A moved edit can have a different immutable home group.
        for(var pair:edits.entrySet())if(!pair.getValue().deleted&&bounds.covers(pair.getValue().object.getROI().getCentroidX(),pair.getValue().object.getROI().getCentroidY())){var cell=lookup(pair.getKey());if(cell!=null)consumer.accept(cell);}
        if(originalRevision!=revision)throw new IOException("Cell edits changed during scientific centroid iteration");
    }
    /**
     * We calculate scoped typed values from candidate base groups and clone sparse
     * edits before callbacks can mutate them. Only the callback's explicit
     * selected input fields are joined; base lineage and sparse edits remain
     * intact. Null callback results leave a row
     * unapplied; available null fields intentionally remove that value. The
     * completed descriptor preserves base IDs/order and geometry revision.
     * Changed edits, invalid types and conflicting dictionaries stop publication.
     */
    public Path writeComponent(Path privateOutput, String requestSha, String scopeKey, Envelope bounds,
            Set<String> inputFields, Map<String,String> columns, JsonObject dictionaries,
            Function<PathObject,Map<String,Object>> calculation) throws IOException {
        return writeComponent(privateOutput,requestSha,scopeKey,bounds,inputFields,columns,dictionaries,
                calculation,new JsonObject());
    }
    /** We store compact result attributes once in the descriptor while retaining aligned typed rows. */
    public Path writeComponent(Path privateOutput, String requestSha, String scopeKey, Envelope bounds,
            Set<String> inputFields, Map<String,String> columns, JsonObject dictionaries,
            Function<PathObject,Map<String,Object>> calculation, JsonObject attributes) throws IOException {
        LocalParquet.requireWorker();if(closed)throw new IllegalStateException("Cell provider is closed");long originalRevision=revision;
        long originalGeometryRevision=geometryRevision;var selectedComponents=components;
        Set<String> projection=Set.copyOf(inputFields);
        @SuppressWarnings("unchecked") List<Layout> candidates=bounds==null?layout:index.query(bounds);
        var ordered=new ArrayList<>(candidates);ordered.sort(java.util.Comparator.comparingLong(Layout::id));
        try(var writer=new CellComponentWriter(privateOutput,population,description.get("layout_version").getAsString(),requestSha,scopeKey,columns,dictionaries)) {
            for(var entry:ordered) {
                var joined=selectedComponents!=null?selectedComponents.group(entry.id,entry.rows,projection):null;int[] offset={0};
                writer.group(entry.id,entry.rows,sink->LocalParquet.read(entry.file,entry.group,null,row->{
                    String id=row.getString("cell_id",0);int baseOffset=offset[0]++;Edit edit=edits.get(id);
                    PathObject cell=edit==null?object(row):copyEdit(edit.object);if(selectedComponents!=null)selectedComponents.apply(joined,baseOffset,id,cell,edit==null?0:editGeometryRevision(cell));
                    Map<String,Object> values=edit!=null&&edit.deleted?null:calculation.apply(cell);
                    try{sink.accept(id,values);}catch(IOException error){throw new java.io.UncheckedIOException(error);}
                }));
            }
            for(var pair:creations()) {
                var edit=pair.getValue();var cell=copyEdit(edit.object);long group=createdGroup(cell);
                if(bounds!=null&&!cell.getROI().getGeometry().getEnvelopeInternal().intersects(bounds))continue;
                if(selectedComponents!=null)selectedComponents.apply(selectedComponents.group(group,1,projection),0,pair.getKey(),cell,editGeometryRevision(cell));
                writer.group(group,1,sink->sink.accept(pair.getKey(),edit.deleted?null:calculation.apply(cell)));
            }
            if(originalRevision!=revision)throw new IOException("Cell edits changed while scalar results were calculated; discard this incomplete result");
            return writer.complete(originalGeometryRevision,attributes);
        }catch(java.io.UncheckedIOException error){throw error.getCause();}
    }
    /** Publish a narrow scientific edit reference; clean geometry stays in its base. */
    public Path publishEdits(Path privateOutput)throws IOException {
        LocalParquet.requireWorker();if(closed)throw new IllegalStateException("Cell provider is closed");long originalRevision=revision;
        try(var writer=new CellEditWriter(privateOutput,description,descriptionSha,geometryRevision)) {
            for(var pair:edits.entrySet()){var edit=pair.getValue();PathObject cell=edit.deleted?copyEdit(edit.object):lookup(pair.getKey());
                if(cell==null)throw new IOException("Sparse edit refers to an unknown immutable base cell");
                writer.record(pair.getKey(),edit.deleted,editGeometryRevision(cell),cell);
            }
            if(originalRevision!=revision)throw new IOException("Cell edits changed during sparse edit publication");return writer.complete();
        }
    }
    public CompletableFuture<PathObject> lookupAsync(String id) {
        var result=new CompletableFuture<PathObject>();
        try{SharedCellCache.execute(()->{try{if(closed)throw new IllegalStateException("Cell provider is closed");result.complete(lookup(id));}catch(Throwable error){result.completeExceptionally(error);}});}
        catch(java.util.concurrent.RejectedExecutionException error){result.completeExceptionally(error);}
        return result;
    }
    /** Known-ID retrieval follows the sorted compact ID-index group ranges. */
    public PathObject lookup(String id) throws IOException {return lookup(id,false);}
    /** Include retired identities when validating a reviewed creation. */
    public boolean hasCellIdentity(String id)throws IOException {
        LocalParquet.requireWorker();requireOpenMetadata();return edits.containsKey(id)||lookup(id,false,false)!=null;
    }
    /** We reopen full scientific values or the display class under the same stable ID and sparse edit revision. */
    private PathObject lookup(String id,boolean displayOnly) throws IOException {
        return lookup(id,displayOnly,true);
    }
    private PathObject lookup(String id,boolean displayOnly,boolean current) throws IOException {
        LocalParquet.requireWorker();Edit edited=current?edits.get(id):null;if(edited!=null&&edited.deleted)return null;var selectedComponents=current?components:null;
        if(edited!=null&&createdGroup(edited.object)>=0) {
            var cell=displayOnly?copyDisplayEdit(edited.object):copyEdit(edited.object);
            if(selectedComponents!=null)selectedComponents.apply(selectedComponents.group(createdGroup(edited.object),1,displayOnly?DISPLAY_FIELDS:null),0,id,cell,editGeometryRevision(cell));
            return cell;
        }
        long[] location={-1};int[] offset={-1};
        for(var raw:description.getAsJsonArray("id_groups")) {
            var entry=raw.getAsJsonObject();if(id.compareTo(entry.get("minimum").getAsString())<0||id.compareTo(entry.get("maximum").getAsString())>0)continue;
            LocalParquet.read(resolve(description.get("id_index").getAsString()),entry.get("row_group").getAsInt(),List.of("cell_id","group_id","row_offset"),row->{if(id.equals(row.getString("cell_id",0))){location[0]=row.getLong("group_id",0);offset[0]=row.getInteger("row_offset",0);}});
        }
        if(location[0]<0)return null;var entry=byId.get(location[0]);PathObject[] result={null};int[] rowNumber={0};groupReads++;
        var joined=selectedComponents!=null?selectedComponents.group(entry.id,entry.rows,displayOnly?DISPLAY_FIELDS:null):null;
        LocalParquet.read(entry.file,entry.group,displayOnly?VIEW_COLUMNS:null,row->{if(rowNumber[0]++==offset[0]){result[0]=edited==null?object(row):displayOnly?copyDisplayEdit(edited.object):copyEdit(edited.object);if(selectedComponents!=null)selectedComponents.apply(joined,offset[0],id,result[0],edited==null?0:editGeometryRevision(result[0]));}});return result[0];
    }
    /** Select one completed sparse checkpoint over this immutable base.
     * Rows are admitted before replacing the current selection. Reopening never
     * records another edit, changes the geometry revision or saves the image.
     */
    public void selectEdits(ImageData<?> image,Path path,String sha256)throws IOException {
        selectEdits(image,path,sha256,false);
    }
    /** Measurement recovery may retain only explicit deletions above the selected checkpoint. */
    public synchronized void selectEdits(ImageData<?> image,Path path,String sha256,boolean retainDeletions)throws IOException {
        LocalParquet.requireWorker();requireOpenMetadata();
        if(Files.size(path)>16L*1024*1024)throw new IOException("Sparse edit metadata exceeds its allowance");
        byte[] bytes=Files.readAllBytes(path);String actual;
        try{actual=java.util.HexFormat.of().formatHex(java.security.MessageDigest.getInstance("SHA-256").digest(bytes));}
        catch(java.security.NoSuchAlgorithmException impossible){throw new AssertionError(impossible);}
        if(!actual.equals(sha256))throw new IOException("Selected sparse edit descriptor changed");
        var selected=JsonParser.parseString(new String(bytes,java.nio.charset.StandardCharsets.UTF_8)).getAsJsonObject();
        if(!"mif-cell-edits/v1".equals(selected.get("schema_version").getAsString())
                ||!selected.get("complete").getAsBoolean()
                ||!population.equals(selected.get("population_id").getAsString())
                ||!description.get("layout_version").getAsString().equals(selected.get("layout_version").getAsString())
                ||!descriptionSha.equals(selected.get("base_sha256").getAsString()))
            throw new IOException("Selected edits belong to another immutable cell base");
        long selectedRevision=selected.get("geometry_revision").getAsLong();
        if(selectedRevision<0)throw new IOException("Selected geometry revision is invalid");
        final long admittedRevision=selectedRevision;
        var replacements=new HashMap<String,Edit>();var createdGroups=new HashSet<Long>();long[] sequence={0};
        try {
            for(var item:selected.getAsJsonArray("parts")) {
                var part=item.getAsJsonObject();Path relative=Path.of(part.get("file").getAsString());
                if(relative.isAbsolute()||relative.getNameCount()!=1||!relative.toString().endsWith(".parquet"))
                    throw new IOException("Sparse edits require local completed Parquet parts");
                Path file=path.toAbsolutePath().getParent().resolve(relative);
                if(Files.size(file)!=part.get("bytes").getAsLong())throw new IOException("Selected sparse edit part size changed");
                LocalParquet.read(file,row->{
                    try {
                        String id=row.getString("cell_id",0);long version=row.getLong("geometry_revision",0);
                        if(replacements.containsKey(id)||version<0||version>admittedRevision
                                ||row.getInteger("c",0)!=-1||row.getInteger("z",0)!=0||row.getInteger("t",0)!=0)
                            throw new IOException("Sparse edit identity, revision or image plane is invalid");
                        var metadata=row.getGroup("metadata",0);var manual=new HashMap<String,String>();
                        for(int i=0;i<metadata.getFieldRepetitionCount("key_value");i++) {
                            var pair=metadata.getGroup("key_value",i);manual.put(pair.getString("key",0),pair.getString("value",0));
                        }
                        boolean created=manual.containsKey(CREATED_GROUP);
                        PathObject base=lookup(id,false,false);
                        if((base==null)!=created)throw new IOException("Sparse edit has no accepted base or explicit created identity");
                        var cellGeometry=created?new WKBReader().read(row.getBinary("cell_geometry",0).getBytes()):geometry(row.getBinary("cell_geometry",0).getBytes());
                        var nucleusGeometry=row.getFieldRepetitionCount("nucleus_geometry")==0?null:created?new WKBReader().read(row.getBinary("nucleus_geometry",0).getBytes()):geometry(row.getBinary("nucleus_geometry",0).getBytes());
                        if(!validEditGeometry(cellGeometry)||(nucleusGeometry!=null&&!validEditGeometry(nucleusGeometry)))
                            throw new IOException("Selected edited outline is invalid");
                        var cell=PathObjects.createCellObject(created?exactRoi(cellGeometry):roi(cellGeometry),nucleusGeometry==null?null:created?exactRoi(nucleusGeometry):roi(nucleusGeometry),
                                row.getFieldRepetitionCount("primary_phenotype")==0?null:PathClass.fromString(row.getString("primary_phenotype",0)));
                        if(created) {
                            cell.setID(UUID.fromString(id));cell.getMetadata().putAll(manual);
                            validateCreatedCell(cell,id);
                            if(version==0||!createdGroups.add(createdGroup(cell)))throw new IOException("Created cell group or revision is invalid");
                        } else {
                            cell.setID(base.getID());cell.getMetadata().putAll(base.getMetadata());
                            for(String key:List.of("mif.cell_id","mif.population_id","section_id","analysis_domain_id"))
                                if(!java.util.Objects.equals(base.getMetadata().get(key),manual.get(key)))
                                    throw new IOException("Sparse edit changes the cell's immutable identity or analysis area");
                        }
                        cell.getMetadata().putAll(manual);
                        if("true".equals(manual.get("mif.manual_class_override"))) {
                            cell.getMetadata().put("mif.manual_class_override","true");
                            cell.getMetadata().put("mif.manual_class_source",manual.getOrDefault("mif.manual_class_source",""));
                            if(cell.getPathClass()==null)cell.getMetadata().remove("primary_phenotype");
                            else cell.getMetadata().put("primary_phenotype",cell.getPathClass().toString());
                        }
                        cell.getMetadata().put("mif.geometry_revision",Long.toString(version));
                        cell.getMetadata().put("mif.geometry_identity",geometryIdentity(cell));
                        cell.getMetadata().put("geometry_revision",Long.toString(version));
                        if(version==0)cell.getMeasurementList().putAll(base.getMeasurementList());
                        else {
                            if(components!=null)components.invalidateDerived(cell);
                            cell.getMetadata().put("scientific_measurements_stale","true");
                            cell.getMetadata().put("mif.measurements_stale","true");
                        }
                        replacements.put(id,new Edit(cell,row.getBoolean("deleted",0),++sequence[0]));
                    }catch(IOException failure){throw new java.io.UncheckedIOException(failure);}
                    catch(org.locationtech.jts.io.ParseException failure){throw new java.io.UncheckedIOException(new IOException("Selected edited geometry is invalid",failure));}
                },CellEditWriter.SCHEMA,part.get("row_count").getAsLong());
            }
        }catch(java.io.UncheckedIOException failure){throw failure.getCause();}
        if(sequence[0]!=selected.get("row_count").getAsLong())throw new IOException("Sparse edit rows differ from their descriptor");
        validateCreatedSources(replacements);
        if(retainDeletions&&!edits.isEmpty()) {
            int extraDeletions=0;
            for(var pair:edits.entrySet()) {
                var live=pair.getValue();var expected=replacements.get(pair.getKey());
                PathObject original=expected==null?lookup(pair.getKey(),false,false):expected.object;
                if(original==null||!geometryIdentity(original).equals(geometryIdentity(live.object))
                        ||(!live.deleted&&(expected==null||expected.deleted)))
                    throw new IOException("Current cell edits differ from the accepted checkpoint and explicit deletion recovery");
                if(live.deleted&&(expected==null||!expected.deleted)) {
                    replacements.put(pair.getKey(),new Edit(copyEdit(live.object),true,++sequence[0]));extraDeletions++;
                }
            }
            if(geometryRevision!=selectedRevision+extraDeletions)
                throw new IOException("Current deletion revision differs from the accepted checkpoint");
            selectedRevision=geometryRevision;
        }
        Object saved=image.getProperty(EDITS_PROPERTY);
        var checkpoints=saved instanceof String text?JsonParser.parseString(text).getAsJsonObject():new JsonObject();
        edits.clear();edits.putAll(replacements);geometryRevision=selectedRevision;revision=Math.max(revision+1,sequence[0]);
        checkpoints.add(population,JsonParser.parseString(checkpoint()));image.setProperty(EDITS_PROPERTY,checkpoints.toString());
        SharedCellCache.release(this);
    }
    private static boolean validEditGeometry(org.locationtech.jts.geom.Geometry shape) {
        return shape instanceof org.locationtech.jts.geom.Polygonal&&!shape.isEmpty()&&shape.isValid()
                &&Double.isFinite(shape.getArea())&&shape.getArea()>0;
    }
    private long firstCreatedGroup() {return layout.stream().mapToLong(Layout::id).max().orElse(-1)+1;}
    private void validateCreatedCell(PathObject cell,String id)throws IOException {
        var metadata=cell.getMetadata();long group=createdGroup(cell);
        var scope=description.getAsJsonObject("scope");String domain=metadata.get("analysis_domain_id");
        if(group<firstCreatedGroup()||!Long.toString(group).equals(metadata.get(CREATED_GROUP))||
                !id.equals(cell.getID().toString())||!id.equals(metadata.get("mif.cell_id"))||!id.equals(metadata.get("cell_id"))||
                !population.equals(metadata.get("mif.population_id"))||!population.equals(metadata.get("segmentation_population_id"))||
                !getSectionId().equals(metadata.get("section_id"))||!descriptionSha.equals(metadata.get("cell_evidence_sha256"))||
                domain==null||!scope.getAsJsonObject("domains").has(domain))
            throw new IOException("Created cell changes its accepted population, domain or appended identity");
        String operation=metadata.get("mif.correction_operation");
        if(!Set.of("split","merge","add_cell").contains(operation))throw new IOException("Created cell has no explicit correction provenance");
        var sources=JsonParser.parseString(metadata.getOrDefault("mif.correction_source_ids","null"));
        if(!sources.isJsonArray())throw new IOException("Created cell has no explicit source identities");
        var ids=new java.util.TreeSet<String>();
        for(var value:sources.getAsJsonArray())if(!value.isJsonPrimitive()||!value.getAsJsonPrimitive().isString()||value.getAsString().isBlank()||!ids.add(value.getAsString())||id.equals(value.getAsString()))throw new IOException("Created source identities are invalid");
        if(("add_cell".equals(operation)&&!ids.isEmpty())||("split".equals(operation)&&ids.size()!=1)||("merge".equals(operation)&&ids.size()<2))throw new IOException("Created cell source count differs from its explicit operation");
        if(!new com.google.gson.Gson().toJson(ids).equals(metadata.get("mif.correction_source_ids")))throw new IOException("Created cell source identities are not canonical");
        var roi=cell.getROI();var nucleus=((qupath.lib.objects.PathCellObject)cell).getNucleusROI();
        if(roi.getC()!=-1||roi.getZ()!=0||roi.getT()!=0||!validEditGeometry(roi.getGeometry())||
                (nucleus!=null&&(!nucleus.getImagePlane().equals(roi.getImagePlane())||!validEditGeometry(nucleus.getGeometry())||!roi.getGeometry().covers(nucleus.getGeometry()))))
            throw new IOException("Created cell or explicitly associated nucleus is invalid");
    }
    private void validateCreatedSources(Map<String,Edit> selected)throws IOException {
        for(var item:selected.entrySet())if(createdGroup(item.getValue().object)>=0) {
            var cell=item.getValue().object;
            for(var raw:JsonParser.parseString(cell.getMetadata().get("mif.correction_source_ids")).getAsJsonArray()) {
                String id=raw.getAsString();var previous=selected.get(id);var source=previous==null?lookup(id,false,false):previous.object;
                if(source==null||!java.util.Objects.equals(source.getMetadata().get("analysis_domain_id"),cell.getMetadata().get("analysis_domain_id"))||
                        (createdGroup(source)>=0&&createdGroup(source)>=createdGroup(cell)))
                    throw new IOException("Created cell provenance names an absent, later or differently scoped source");
            }
        }
    }
    /** Add an explicitly reviewed identity to the existing sparse checkpoint.
     * The new cell receives a permanent appended group for ordinary scalar
     * component writes. Source geometry and identities remain unchanged.
     */
    public synchronized void recordCreatedCell(ImageData<?> image,PathObject cell,String operation,List<String> sourceIds)throws IOException {
        LocalParquet.requireWorker();requireOpenMetadata();String id=cell.getID().toString();
        if(edits.containsKey(id)||lookup(id,false,false)!=null)throw new IOException("A created cell identity already exists");
        var scope=description.getAsJsonObject("scope");String domainId=cell.getMetadata().get("analysis_domain_id");
        var domain=scope.getAsJsonObject("domains").getAsJsonObject(domainId);
        if(domain==null)throw new IOException("A created cell requires one accepted analysis domain");
        var fresh=PathObjects.createCellObject(cell.getROI(),((qupath.lib.objects.PathCellObject)cell).getNucleusROI(),null);fresh.setID(cell.getID());
        for(var field:scope.entrySet())if(field.getValue().isJsonPrimitive())fresh.getMetadata().put(field.getKey(),field.getValue().getAsString());
        for(var field:domain.entrySet())if(field.getValue().isJsonPrimitive())fresh.getMetadata().put(field.getKey(),field.getValue().getAsString());
        if(acceptedReceipt!=null)for(String name:List.of("domain_cell_mapping_sha256","section_population_sha256","cell_evidence_sha256"))if(acceptedReceipt.has(name))fresh.getMetadata().put("section_population_sha256".equals(name)?"segmentation_section_population_sha256":name,acceptedReceipt.get(name).getAsString());
        fresh.getMetadata().put("cell_id",id);fresh.getMetadata().put("mif.cell_id",id);fresh.getMetadata().put("mif.population_id",population);
        fresh.getMetadata().put("segmentation_population_id",population);fresh.getMetadata().put("cell_evidence_sha256",descriptionSha);fresh.getMetadata().put("analysis_domain_id",domainId);
        long group=Math.max(firstCreatedGroup(),creations().stream().mapToLong(item->createdGroup(item.getValue().object)+1).max().orElse(0));
        fresh.getMetadata().put(CREATED_GROUP,Long.toString(group));fresh.getMetadata().put("mif.correction_operation",operation);
        if(sourceIds==null||new HashSet<>(sourceIds).size()!=sourceIds.size())throw new IOException("Created source identities must be explicit and distinct");
        fresh.getMetadata().put("mif.correction_source_ids",new com.google.gson.Gson().toJson(new java.util.TreeSet<>(sourceIds)));
        validateCreatedCell(fresh,id);var proposed=new HashMap<>(edits);proposed.put(id,new Edit(fresh,false,revision+1));validateCreatedSources(proposed);
        recordEdit(image,fresh,false);
    }
    /**
     * We checkpoint the supplied stable cell edit or deletion in its image data.
     * Shape changes advance geometry revision and invalidate derived results;
     * manual labels retain their selected result identity so reloading the same
     * automatic component preserves the human decision. Unknown populations fail.
     */
    public synchronized void recordEdit(ImageData<?> image,PathObject cell,boolean deleted) {
        String id=cell.getMetadata().get("mif.cell_id");if(id==null||!population.equals(cell.getMetadata().get("mif.population_id")))throw new IllegalArgumentException("Edited cell belongs to another population");
        String actualClass=cell.getPathClass()==null?null:cell.getPathClass().toString();
        if(!java.util.Objects.equals(actualClass,cell.getMetadata().get("primary_phenotype"))){cell.getMetadata().put("mif.manual_class_override","true");cell.getMetadata().put("mif.manual_class_source",cell.getMetadata().getOrDefault("mif.primary_class_source",""));if(actualClass==null)cell.getMetadata().remove("primary_phenotype");else cell.getMetadata().put("primary_phenotype",actualClass);}
        String identity=geometryIdentity(cell);boolean shapeChanged=deleted||!identity.equals(cell.getMetadata().get("mif.geometry_identity"));
        if(shapeChanged){cell.getMetadata().put("mif.geometry_revision",Long.toString(++geometryRevision));cell.getMeasurementList().clear();if(components!=null)components.invalidateDerived(cell);cell.getMetadata().put("scientific_measurements_stale","true");cell.getMetadata().put("mif.measurements_stale","true");}
        cell.getMetadata().put("mif.geometry_identity",identity);cell.getMetadata().put("geometry_revision",Long.toString(editGeometryRevision(cell)));
        edits.put(id,new Edit(cell,deleted,++revision));Object saved=image.getProperty(EDITS_PROPERTY);var checkpoints=saved instanceof String text?JsonParser.parseString(text).getAsJsonObject():new JsonObject();checkpoints.add(population,JsonParser.parseString(checkpoint()));image.setProperty(EDITS_PROPERTY,checkpoints.toString());
        SharedCellCache.release(this);
    }
    private static long editGeometryRevision(PathObject cell){return Long.parseLong(cell.getMetadata().getOrDefault("mif.geometry_revision","0"));}
    /** We retain only identity and manual-class guards in a prepared sparse display object. */
    private static PathObject copyDisplayEdit(PathObject original) {
        var copy=PathObjects.createCellObject(original.getROI(),((qupath.lib.objects.PathCellObject)original).getNucleusROI(),original.getPathClass());copy.setID(original.getID());
        for(String field:List.of("cell_id","mif.cell_id","mif.population_id","mif.geometry_revision","mif.manual_class_override","mif.manual_class_source","mif.primary_class_source","primary_phenotype")){String value=original.getMetadata().get(field);if(value!=null)copy.getMetadata().put(field,value);}
        return copy;
    }
    private static PathObject copyEdit(PathObject original) {var copy=PathObjects.createCellObject(original.getROI(),((qupath.lib.objects.PathCellObject)original).getNucleusROI(),original.getPathClass());copy.setID(original.getID());copy.getMetadata().putAll(original.getMetadata());copy.getMeasurementList().putAll(original.getMeasurementList());return copy;}
    private static String geometryIdentity(PathObject cell) {
        try{var digest=java.security.MessageDigest.getInstance("SHA-256");var writer=new WKBWriter();digest.update(writer.write(cell.getROI().getGeometry()));var nucleus=((qupath.lib.objects.PathCellObject)cell).getNucleusROI();if(nucleus!=null)digest.update(writer.write(nucleus.getGeometry()));return java.util.HexFormat.of().formatHex(digest.digest());}catch(java.security.NoSuchAlgorithmException impossible){throw new AssertionError(impossible);}
    }
    /** We serialize sparse geometry, deletion and manual-label decisions under this immutable population/layout. */
    private String checkpoint() {
        var out=new JsonObject();out.addProperty("population_id",population);out.addProperty("layout_version",description.get("layout_version").getAsString());out.addProperty("geometry_revision",geometryRevision);var rows=new JsonObject();
        for(var pair:edits.entrySet()) {var edit=pair.getValue();var row=new JsonObject();row.addProperty("deleted",edit.deleted);row.addProperty("revision",edit.revision);row.addProperty("geometry_revision",editGeometryRevision(edit.object));row.addProperty("geometry",Base64.getEncoder().encodeToString(new WKBWriter().write(edit.object.getROI().getGeometry())));
            var nucleus=((qupath.lib.objects.PathCellObject)edit.object).getNucleusROI();if(nucleus!=null)row.addProperty("nucleus",Base64.getEncoder().encodeToString(new WKBWriter().write(nucleus.getGeometry())));if(edit.object.getPathClass()!=null)row.addProperty("class_name",edit.object.getPathClass().toString());if("true".equals(edit.object.getMetadata().get("mif.manual_class_override"))){row.addProperty("manual_class_override",true);row.addProperty("manual_class_source",edit.object.getMetadata().getOrDefault("mif.manual_class_source",""));}if(createdGroup(edit.object)>=0)row.add("metadata",new com.google.gson.Gson().toJsonTree(edit.object.getMetadata()));rows.add(pair.getKey(),row);}
        out.add("edits",rows);return out.toString();
    }
    /** We restore this population's saved sparse checkpoint, rejecting unknown IDs or a different immutable layout. */
    public void restoreEdits(ImageData<?> image) throws IOException {
        LocalParquet.requireWorker();Object stored=image.getProperty(EDITS_PROPERTY);if(!(stored instanceof String text))return;
        var checkpoints=JsonParser.parseString(text).getAsJsonObject();var saved=checkpoints.getAsJsonObject(population);if(saved==null)return;if(!description.get("layout_version").getAsString().equals(saved.get("layout_version").getAsString()))throw new IOException("Saved edits belong to a different cell geometry layout");
        geometryRevision=saved.get("geometry_revision").getAsLong();var createdGroups=new HashSet<Long>();
        try {for(var pair:saved.getAsJsonObject("edits").entrySet()) {
            var row=pair.getValue().getAsJsonObject();var base=lookup(pair.getKey(),false,false);
            boolean created=row.has("metadata")&&row.getAsJsonObject("metadata").has(CREATED_GROUP);
            if((base==null)!=created)throw new IOException("Saved edit has no accepted base or explicit created identity");
            byte[] encoded=Base64.getDecoder().decode(row.get("geometry").getAsString());
            var geometry=created?new WKBReader().read(encoded):geometry(encoded);
            var nucleus=row.has("nucleus")?(created?new WKBReader().read(Base64.getDecoder().decode(row.get("nucleus").getAsString())):geometry(Base64.getDecoder().decode(row.get("nucleus").getAsString()))):null;
            var edited=PathObjects.createCellObject(created?exactRoi(geometry):roi(geometry),nucleus==null?null:created?exactRoi(nucleus):roi(nucleus),row.has("class_name")?PathClass.fromString(row.get("class_name").getAsString()):null);
            if(created) {
                edited.setID(UUID.fromString(pair.getKey()));
                for(var field:row.getAsJsonObject("metadata").entrySet())edited.getMetadata().put(field.getKey(),field.getValue().getAsString());
                validateCreatedCell(edited,pair.getKey());
                if(!createdGroups.add(createdGroup(edited))||row.get("geometry_revision").getAsLong()<=0)throw new IOException("Saved created group or revision is invalid");
            } else {edited.setID(base.getID());edited.getMetadata().putAll(base.getMetadata());}
            if(row.has("manual_class_override")&&row.get("manual_class_override").getAsBoolean()){edited.getMetadata().put("mif.manual_class_override","true");edited.getMetadata().put("mif.manual_class_source",row.get("manual_class_source").getAsString());if(edited.getPathClass()==null)edited.getMetadata().remove("primary_phenotype");else edited.getMetadata().put("primary_phenotype",edited.getPathClass().toString());}long geometryVersion=row.get("geometry_revision").getAsLong();edited.getMetadata().put("mif.geometry_revision",Long.toString(geometryVersion));edited.getMetadata().put("mif.geometry_identity",geometryIdentity(edited));if(geometryVersion==0)edited.getMeasurementList().putAll(base.getMeasurementList());else {if(components!=null)components.invalidateDerived(edited);edited.getMetadata().put("scientific_measurements_stale","true");edited.getMetadata().put("mif.measurements_stale","true");}long version=row.get("revision").getAsLong();revision=Math.max(revision,version);edits.put(pair.getKey(),new Edit(edited,row.get("deleted").getAsBoolean(),version));
        }validateCreatedSources(edits);}catch(org.locationtech.jts.io.ParseException error){throw new IOException("Saved edited geometry is invalid",error);}
    }
    public int getEditCount(){return edits.size();}
    public long getGeometryRevision(){return geometryRevision;}
    public long getRowCount(){return description.get("row_count").getAsLong();}
    /** Explicit saved deletions alone reduce the current immutable population. */
    public synchronized long getCurrentRowCount(){return getRowCount()+edits.values().stream().filter(edit->createdGroup(edit.object)>=0).count()-edits.values().stream().filter(edit->edit.deleted).count();}
    /** We expose I/O and hydration counters for investigating spatial reuse and display work. */
    public long getGroupReads(){return groupReads;}
    public long getGeometryDecodes(){return geometryDecodes;}
    public long getResidentSnapshotBytes(){return SharedCellCache.bytes(this);}
    /** We expose the shared retained cache reservations for resource diagnostics across open images. */
    public static long getProcessObjectCacheBytes(){return SharedCellCache.bytes();}
    public static long getProcessObjectCacheLimit(){return SharedCellCache.limit();}
    public static long getProcessObjectCacheEvictions(){return SharedCellCache.evictions();}
    public static long getProcessCellMetadataBytes(){return SharedCellCache.metadataBytes();}
    public static long getProcessCellMetadataLimit(){return SharedCellCache.metadataLimit();}
    /**
     * We reuse our admitted base identity and domain scope for compact exports.
     * i) These accessors never reopen a descriptor or decode a cell geometry.
     * ii) Selected result references include only this population and layout;
     * their metadata contains exact descriptor SHA and shared phenotype identity.
     * A closed owner cannot expose a completed scientific selection.
     */
    public String getDescriptionSha256(){requireOpenMetadata();return descriptionSha;}
    public String getSectionId(){requireOpenMetadata();return description.getAsJsonObject("scope").get("section_id").getAsString();}
    public Set<String> getDomainIds(){requireOpenMetadata();return Set.copyOf(description.getAsJsonObject("scope").getAsJsonObject("domains").keySet());}
    public Map<String,Map<String,String>> getSelectedComponentReferences(){requireOpenMetadata();var selected=components;return selected==null?Map.of():selected.references();}
    public List<Map<String,Object>> getSelectedComponentMetadata(){requireOpenMetadata();var selected=components;return selected==null?List.of():selected.metadata();}
    private void requireOpenMetadata(){if(closed)throw new IllegalStateException("Cell provider is closed");}
    public String getPopulation(){return population;}
    @Override public boolean ownsPopulation(String population){return this.population.equals(population);}
    @Override public void close(){closed=true;pending.values().forEach(future->future.completeExceptionally(new IllegalStateException("Cell provider is closed")));pending.clear();edits.clear();SharedCellCache.release(this);if(components!=null){components.close();components=null;}layout.clear();byId.clear();classes.clear();labels.clear();colors.clear();index=null;description=null;acceptedReceipt=null;SharedCellCache.releaseMetadata(this);revision++;}
}
