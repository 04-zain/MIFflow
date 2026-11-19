package org.mif.segmentation;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.io.IOException;
import java.io.RandomAccessFile;
import java.nio.ByteBuffer;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.BitSet;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.Consumer;
import org.locationtech.jts.geom.Envelope;
import org.locationtech.jts.geom.Geometry;
import org.locationtech.jts.index.quadtree.Quadtree;
import org.locationtech.jts.index.strtree.STRtree;
import org.locationtech.jts.operation.relateng.RelateNG;
import qupath.lib.objects.PathObject;
import qupath.lib.objects.PathObjects;
import qupath.lib.objects.PathCellObject;
import qupath.lib.objects.classes.PathClass;
import qupath.lib.objects.utils.ObjectProcessor;
import qupath.lib.regions.ImagePlane;
import qupath.lib.roi.GeometryTools;

/**
 * Finalize complete groups of overlapping native cell predictions within an allowance.
 *
 * We retain original cell objects while they fit, reserving most of the allowance
 * for pair comparisons and native merging. Evicted candidates are serialized once
 * to a private spill; decoded candidates share the same bounded retention map.
 * Expected tile footprints establish when every possible contributor has arrived.
 * Complete groups retain the native merger, clipper and original contributor order.
 */
public final class ExactTileFinalizer implements AutoCloseable {
    public static final class RequiredMemoryException extends IOException {
        private static final long serialVersionUID = 1L;
        public final long requiredBytes;
        RequiredMemoryException(long bytes, String context) {
            super(context + " requires at least " + bytes + " bytes for exact native finalization");
            requiredBytes = bytes;
        }
    }

    private final List<Envelope> footprints;
    private final STRtree tiles = new STRtree();
    private final BitSet completed = new BitSet();
    private final Quadtree frontier = new Quadtree();
    private final Quadtree rawBounds = new Quadtree();
    private final Map<Long, Component> active = new HashMap<>();
    private final LinkedHashMap<Resident, PathObject> retained = new LinkedHashMap<>(16, .75f, true);
    private final Path spillPath;
    private RandomAccessFile spill;
    private final long memoryAllowance, metadataAllowance, retentionAllowance;
    private final ObjectProcessor nativeMerge;
    private final Consumer<PathObject> sink;
    private long unfinishedRecords, retainedBytes, sequence, finalCount;
    private long maximumResidentBytes, maximumFrontierComponents;
    private boolean failed, closed;

    private static final class Component {
        final long id;
        Envelope bounds;
        long bytes, count;
        Component parent;
        Resident first, last;
        Component(long id, Envelope bounds) {
            this.id = id;
            this.bounds = new Envelope(bounds);
        }
    }

    private static final class Resident {
        final Envelope bounds;
        final long order, bytes;
        final Component component;
        long offset = -1;
        Resident next;
        PathObject finalizing;
        Resident(Envelope bounds, long order, long bytes, Component component) {
            this.bounds = new Envelope(bounds);
            this.order = order;
            this.bytes = bytes;
            this.component = component;
        }
        Component root() {
            Component root = component;
            while (root.parent != null) root = root.parent;
            Component item = component;
            while (item.parent != null) {
                Component next = item.parent;
                item.parent = root;
                item = next;
            }
            return root;
        }
    }

    private record Ordered(long order, PathObject object) {}
    private record Encoded(byte[] bytes, int decodedBytes) {}

    /**
     * Index the padded tile footprints and reserve memory for retained and working objects.
     *
     * A quarter of the object allowance holds unfinished candidates. The remainder
     * accommodates comparisons, serialization and merging; a complete group also
     * evicts unrelated retained objects when its estimated workspace needs room.
     * Metadata admission includes record links and union-find component references.
     * These conservative estimates control admission; they are not JVM heap readings.
     * The spill file is created only when a candidate must leave memory.
     */
    public ExactTileFinalizer(List<Envelope> footprints, Path spillPath,
            long memoryAllowance, long metadataAllowance, ObjectProcessor nativeMerge,
            Consumer<PathObject> sink) throws IOException {
        this.footprints = footprints.stream().map(Envelope::new).toList();
        this.spillPath = spillPath;
        this.memoryAllowance = memoryAllowance;
        this.metadataAllowance = metadataAllowance;
        this.retentionAllowance = memoryAllowance / 4;
        this.nativeMerge = nativeMerge;
        this.sink = sink;
        long planBytes = footprints.size() * 192L;
        if (planBytes > metadataAllowance)
            throw new RequiredMemoryException(planBytes, "The expected tile dependency index");
        if (Files.exists(spillPath)) throw new IOException("Segmentation spill already exists");
        for (int i = 0; i < footprints.size(); i++) tiles.insert(this.footprints.get(i), i);
        tiles.build();
    }

    /** Accept one successful tile, then finalize groups with no outstanding contributors. */
    public synchronized void completeTile(int tile, List<? extends PathObject> objects) throws IOException {
        if (closed || failed) throw new IOException("Segmentation finalizer is unavailable");
        if (tile < 0 || tile >= footprints.size() || completed.get(tile))
            throw new IOException("Unexpected or repeated tile contribution");
        try {
            // i) Retain or spill candidates before marking this contribution complete.
            int ordinal = 0;
            for (var object : objects) {
                var geometry = object.getROI().getGeometry();
                Envelope bounds = geometry.getEnvelopeInternal();
                if (!footprints.get(tile).covers(bounds))
                    throw new IOException("Native tile object escapes its admitted contributor footprint");
                long bytes = estimateBytes(object);
                admitWorkspace(Math.multiplyExact(bytes, 3), "One native overlap candidate");
                Component component = connect(geometry, bounds);
                append(component, ((long) tile << 32) | (ordinal++ & 0xffffffffL), object, bounds, bytes);
                long metadata = footprints.size() * 192L + active.size() * 384L + unfinishedRecords * 512L;
                if (metadata > metadataAllowance)
                    throw new RequiredMemoryException(metadata, "The unfinished overlap frontier index");
            }
            // ii) Empty successful tiles also satisfy their spatial dependencies.
            completed.set(tile);
            @SuppressWarnings("unchecked") List<Component> touched = frontier.query(footprints.get(tile));
            for (var component : List.copyOf(touched))
                if (active.containsKey(component.id) && allContributorsCompleted(component.bounds))
                    finalizeComponent(component);
            maximumFrontierComponents = Math.max(maximumFrontierComponents, active.size());
        } catch (IOException | RuntimeException error) {
            failed = true;
            throw error;
        }
    }

    /** Estimate retained geometry, native-object fields and named scalar values without encoding. */
    private static long estimateBytes(PathObject object) {
        long points = object.getROI().getNumPoints();
        if (object instanceof PathCellObject cell && cell.getNucleusROI() != null)
            points = Math.addExact(points, cell.getNucleusROI().getNumPoints());
        long bytes = Math.addExact(2048, Math.multiplyExact(points, 128));
        for (String name : object.getMeasurementList().getNames())
            bytes = Math.addExact(bytes, 96L + 2L * name.length());
        for (var field : object.getMetadata().entrySet())
            bytes = Math.addExact(bytes, 128L + 2L * (field.getKey().length() + field.getValue().length()));
        if (object.getPathClass() != null) bytes = Math.addExact(bytes, 2L * object.getPathClass().toString().length());
        return bytes;
    }

    /** Connect only positive-area overlaps, reading retained objects before spilled records. */
    private Component connect(Geometry geometry, Envelope bounds) throws IOException {
        var combined = new Envelope(bounds);
        var members = new java.util.LinkedHashSet<Component>();
        @SuppressWarnings("unchecked") List<Resident> nearby = rawBounds.query(bounds);
        for (var resident : nearby) {
            var root = resident.root();
            if (members.contains(root) || !resident.bounds.intersects(bounds)) continue;
            var other = readObject(resident, true);
            // Polygon interiors must share area; touching edges or points do not connect groups.
            if (RelateNG.relate(geometry, other.getROI().getGeometry(), "2********")) {
                members.add(root);
                combined.expandToInclude(root.bounds);
            }
        }
        if (members.isEmpty()) {
            var result = new Component(++sequence, bounds);
            active.put(result.id, result);
            frontier.insert(result.bounds, result);
            return result;
        }

        // Extend an existing group instead of rebuilding its root for every candidate.
        // Linked records preserve contributor order; finalization sorts by tile and ordinal.
        var iterator = members.iterator();
        var result = iterator.next();
        boolean expanded = !result.bounds.equals(combined);
        if (expanded) frontier.remove(result.bounds, result);
        while (iterator.hasNext()) {
            var component = iterator.next();
            frontier.remove(component.bounds, component);
            active.remove(component.id);
            component.parent = result;
            result.last.next = component.first;
            result.last = component.last;
            result.bytes = Math.addExact(result.bytes, component.bytes);
            result.count += component.count;
        }
        if (expanded) {
            result.bounds = combined;
            frontier.insert(result.bounds, result);
        }
        return result;
    }

    /** Add one ordered record; component membership stays in the existing memory index. */
    private void append(Component component, long order, PathObject object, Envelope bounds, long bytes) throws IOException {
        var resident = new Resident(bounds, order, bytes, component);
        if (component.first == null) component.first = resident;
        else component.last.next = resident;
        component.last = resident;
        component.count++;
        component.bytes = Math.addExact(component.bytes, bytes);
        retain(resident, object);
        rawBounds.insert(resident.bounds, resident);
        unfinishedRecords++;
    }

    /** Keep one decoded object within the same retention allowance used by new candidates. */
    private void retain(Resident resident, PathObject object) throws IOException {
        if (resident.bytes > retentionAllowance) {
            spill(resident, object);
            return;
        }
        while (retainedBytes + resident.bytes > retentionAllowance) evictOldest();
        retained.put(resident, object);
        retainedBytes += resident.bytes;
        maximumResidentBytes = Math.max(maximumResidentBytes, retainedBytes);
    }

    /** Eviction serializes a candidate only once; later decoded copies can simply be dropped. */
    private void evictOldest() throws IOException {
        var iterator = retained.entrySet().iterator();
        if (!iterator.hasNext()) throw new IOException("No retained candidate can release the required workspace");
        var oldest = iterator.next();
        spill(oldest.getKey(), oldest.getValue());
        retainedBytes -= oldest.getKey().bytes;
        iterator.remove();
    }

    /** Reserve transient space while keeping unfinished candidates recoverable from their spill. */
    private void admitWorkspace(long required, String context) throws IOException {
        if (required > memoryAllowance) throw new RequiredMemoryException(required, context);
        while (retainedBytes > memoryAllowance - required) evictOldest();
        maximumResidentBytes = Math.max(maximumResidentBytes, retainedBytes + required);
    }

    /** Serialize exact native geometry only on first eviction, with one compact header write. */
    private void spill(Resident resident, PathObject object) throws IOException {
        if (resident.offset >= 0) return;
        Encoded payload = encode(object);
        if (spill == null) {
            Files.createDirectories(spillPath.toAbsolutePath().getParent());
            spill = new RandomAccessFile(spillPath.toFile(), "rw");
        }
        resident.offset = spill.length();
        spill.seek(resident.offset);
        spill.write(ByteBuffer.allocate(8).putInt(payload.bytes.length).putInt(payload.decodedBytes).array());
        spill.write(payload.bytes);
    }

    /** Reuse a resident object or decode the exact geometry, optionally retaining it for later pairs. */
    private PathObject readObject(Resident resident, boolean cache) throws IOException {
        var object = retained.get(resident);
        if (object != null) return object;
        if (spill == null || resident.offset < 0) throw new IOException("Missing native overlap candidate");
        if (cache) admitWorkspace(Math.multiplyExact(resident.bytes, 3), "One native overlap candidate");
        spill.seek(resident.offset);
        int length = spill.readInt(), decodedBytes = spill.readInt();
        if (length < 0 || decodedBytes < 0) throw new IOException("Invalid private overlap spill record");
        byte[] payload = new byte[length];
        spill.readFully(payload);
        object = decode(com.github.luben.zstd.Zstd.decompress(payload, decodedBytes));
        if (cache) retain(resident, object);
        return object;
    }

    /** Every scheduled footprint intersecting the group's bounds must have succeeded. */
    private boolean allContributorsCompleted(Envelope bounds) {
        @SuppressWarnings("unchecked") List<Integer> possible = tiles.query(bounds);
        for (int tile : possible)
            if (footprints.get(tile).intersects(bounds) && !completed.get(tile)) return false;
        return true;
    }

    /**
     * Restore native order and finalize one complete group with admitted merge workspace.
     *
     * Three times the retained-object estimate covers the group's input and native
     * merge temporaries. Already resident group objects move into that workspace;
     * unrelated cached objects are evicted if necessary. Group inputs are decoded
     * at most once here. Native processing and sink failures leave publication incomplete.
     */
    private void finalizeComponent(Component component) throws IOException {
        long required = Math.multiplyExact(component.bytes, 3);
        if (required > memoryAllowance)
            throw new RequiredMemoryException(required, "One complete overlap component with " + component.count + " native objects");
        var records = new ArrayList<Ordered>();
        for (var resident = component.first; resident != null; resident = resident.next) {
            var object = retained.remove(resident);
            if (object != null) {
                retainedBytes -= resident.bytes;
                resident.finalizing = object;
            }
        }
        admitWorkspace(required, "One complete native overlap component");
        for (var resident = component.first; resident != null; resident = resident.next) {
            records.add(new Ordered(resident.order, resident.finalizing != null
                ? resident.finalizing : readObject(resident, false)));
            resident.finalizing = null;
        }
        records.sort(java.util.Comparator.comparingLong(Ordered::order));
        var input = records.stream().map(Ordered::object).toList();
        var output = nativeMerge.process(input);
        for (var object : output) {
            sink.accept(object);
            finalCount++;
        }
        frontier.remove(component.bounds, component);
        active.remove(component.id);
        for (var resident = component.first; resident != null; resident = resident.next) {
            rawBounds.remove(resident.bounds, resident);
            unfinishedRecords--;
        }
        component.first = null;
        component.last = null;
    }

    /** Failed or cancelled tiles never become completed empty contributors. */
    public synchronized void failTile(int tile) { failed = true; }

    /** Finish only after every expected contribution succeeds; sync a spill only if one was needed. */
    public synchronized void finish() throws IOException {
        if (failed || completed.cardinality() != footprints.size())
            throw new IOException("Segmentation has failed, cancelled or incomplete tile contributors; finalized output cannot be published");
        for (var component : List.copyOf(active.values())) {
            if (!allContributorsCompleted(component.bounds)) throw new IOException("Incomplete frontier dependencies");
            finalizeComponent(component);
        }
        if (spill != null) spill.getFD().sync();
    }
    /**
     * We compress owned native geometry, factory and coordinate-sequence values
     * alongside exact measurements and metadata. This preserves native shape
     * arithmetic; the published cell stream separately writes standard WKB.
     */
    private static Encoded encode(PathObject object)throws IOException {
        var bytes=new ByteArrayOutputStream();try(var out=new DataOutputStream(bytes)) {
            out.writeBoolean(object instanceof PathCellObject);var roi=object.getROI();out.writeInt(roi.getC());out.writeInt(roi.getZ());out.writeInt(roi.getT());writeGeometry(out,roi.getGeometry());
            var nucleus=object instanceof PathCellObject cell?cell.getNucleusROI():null;writeGeometry(out,nucleus==null?null:nucleus.getGeometry());
            out.writeBoolean(object.getPathClass()!=null);if(object.getPathClass()!=null){out.writeUTF(object.getPathClass().toString());Integer color=object.getPathClass().getColor();out.writeBoolean(color!=null);if(color!=null)out.writeInt(color);}
            var measurements=object.getMeasurementList();out.writeInt(measurements.size());for(String name:measurements.getNames()){out.writeUTF(name);out.writeDouble(measurements.get(name));}
            out.writeInt(object.getMetadata().size());for(var field:object.getMetadata().entrySet()){out.writeUTF(field.getKey());out.writeUTF(field.getValue());}
        }byte[] raw=bytes.toByteArray();return new Encoded(com.github.luben.zstd.Zstd.compress(raw,1),raw.length);
    }
    // Internal records originate only in this finalizer. JTS serialization retains
    // the native packed coordinate sequences and geometry factory; WKB alone
    // changes scaled shape statistics when read using a different factory.
    private static void writeGeometry(DataOutputStream out,org.locationtech.jts.geom.Geometry geometry)throws IOException {if(geometry==null){out.writeInt(-1);return;}var bytes=new ByteArrayOutputStream();try(var object=new java.io.ObjectOutputStream(bytes)){object.writeObject(geometry);}byte[] raw=bytes.toByteArray();out.writeInt(raw.length);out.write(raw);}
    private static org.locationtech.jts.geom.Geometry geometry(DataInputStream in)throws IOException {int size=in.readInt();if(size<0)return null;if(size>in.available())throw new IOException("Truncated geometry spill");byte[] bytes=new byte[size];in.readFully(bytes);try(var object=new java.io.ObjectInputStream(new ByteArrayInputStream(bytes))){return (org.locationtech.jts.geom.Geometry)object.readObject();}catch(ClassNotFoundException error){throw new IOException("Native geometry class is unavailable",error);}}
    /** We restore one private native record, rejecting trailing or truncated values. */
    private static PathObject decode(byte[] bytes)throws IOException {
        try(var in=new DataInputStream(new ByteArrayInputStream(bytes))) {
            boolean cell=in.readBoolean();var plane=ImagePlane.getPlaneWithChannel(in.readInt(),in.readInt(),in.readInt());var roi=GeometryTools.geometryToROI(geometry(in),plane);var nucleusGeometry=geometry(in);var nucleus=nucleusGeometry==null?null:GeometryTools.geometryToROI(nucleusGeometry,plane);
            PathClass cls=null;if(in.readBoolean()){String name=in.readUTF();Integer color=in.readBoolean()?in.readInt():null;cls=PathClass.fromString(name,color);}PathObject object=cell?PathObjects.createCellObject(roi,nucleus,cls):PathObjects.createDetectionObject(roi,cls);
            for(int i=in.readInt();i>0;i--)object.getMeasurementList().put(in.readUTF(),in.readDouble());for(int i=in.readInt();i>0;i--)object.getMetadata().put(in.readUTF(),in.readUTF());if(in.available()!=0)throw new IOException("Unexpected spill record fields");return object;
        }
    }
    public long getFinalCount() { return finalCount; }
    public long getMaximumResidentBytes() { return maximumResidentBytes; }
    public long getMaximumFrontierComponents() { return maximumFrontierComponents; }
    public synchronized int getCompletedTileCount() { return completed.cardinality(); }

    @Override public synchronized void close() throws IOException {
        if (!closed) {
            closed = true;
            if (spill != null) spill.close();
            retained.clear();
            for (var component : active.values())
                for (var resident = component.first; resident != null; resident = resident.next)
                    resident.finalizing = null;
            active.clear();
            retainedBytes = 0;
        }
    }
}
