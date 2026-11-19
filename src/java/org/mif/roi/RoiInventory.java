package org.mif.roi;

import java.io.IOException;
import java.math.BigDecimal;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.util.HashSet;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.function.Consumer;
import java.util.function.Function;
import org.apache.parquet.example.data.simple.SimpleGroupFactory;
import org.apache.parquet.io.api.Binary;
import org.apache.parquet.schema.MessageType;
import org.apache.parquet.schema.MessageTypeParser;
import org.mif.cells.LocalParquet;
import org.mif.cells.LocalParquetWriter;
import org.mif.io.PinnedInputFile;

/** One completed classifier ROI identity table, with retained local read authority. */
public final class RoiInventory {
    private RoiInventory() {}
    public static final MessageType SCHEMA = MessageTypeParser.parseMessageType(
        "message mif_roi_inventory { required binary roi_id (STRING); required binary path_object_id (STRING); "
        + "required fixed_len_byte_array(32) object_revision_sha256; required binary class_id (STRING); }");
    private static final List<String> COLUMNS = List.of("roi_id", "path_object_id", "object_revision_sha256", "class_id");
    private static final Set<String> REFERENCE_FIELDS = Set.of("path", "sha256", "byte_count", "row_count");

    /** The revision is hex only at our live-metadata boundary; Parquet stores binary32. */
    public record Row(String roiId, String pathObjectId, String objectRevisionSha256, String classId) {
        public Row {
            text(roiId, "roi_id"); text(pathObjectId, "path_object_id"); text(classId, "class_id");
            digest(objectRevisionSha256);
        }
    }

    public static String member(String invocationId) {
        text(invocationId, "invocation_id");
        if (invocationId.equals(".") || invocationId.equals("..") || invocationId.indexOf('/') >= 0
                || invocationId.indexOf('\\') >= 0 || invocationId.indexOf(':') >= 0)
            throw new IllegalArgumentException("ROI inventory invocation must be one safe path segment");
        return "roi-inventories/" + invocationId + ".parquet";
    }

    /** Create missing sample directories below a real existing ancestor, then
     * reserve an absent inventory member before expensive inference. An imported
     * analysis can publish its first new result in an unmaterialized sample. */
    public static Path prepareOutput(Path admittedRoot, String invocationId) throws IOException {
        if (admittedRoot == null || !admittedRoot.isAbsolute()
                || !admittedRoot.equals(admittedRoot.normalize()))
            throw new IOException("ROI inventory root must be an admitted absolute directory");
        var missing = new java.util.ArrayDeque<Path>();
        Path ancestor = admittedRoot;
        while (!Files.exists(ancestor, LinkOption.NOFOLLOW_LINKS)) {
            missing.addFirst(ancestor);
            ancestor = ancestor.getParent();
            if (ancestor == null)
                throw new IOException("ROI inventory output has no available filesystem root");
        }
        root(ancestor);
        for (Path directory : missing) {
            root(directory.getParent());
            try { Files.createDirectory(directory); }
            catch (java.nio.file.FileAlreadyExistsException existing) {
                if (!Files.isDirectory(directory, LinkOption.NOFOLLOW_LINKS)) throw existing;
            }
            root(directory);
        }
        Path root = root(admittedRoot);
        Path directory = root.resolve("roi-inventories");
        try { Files.createDirectory(directory); }
        catch (java.nio.file.FileAlreadyExistsException existing) {
            if (!Files.isDirectory(directory, LinkOption.NOFOLLOW_LINKS)) throw existing;
        }
        if (!directory.toRealPath().equals(directory) || !Files.isDirectory(directory, LinkOption.NOFOLLOW_LINKS))
            throw new IOException("ROI inventory parent is redirected or unavailable");
        Path destination = root.resolve(member(invocationId));
        if (Files.exists(destination, LinkOption.NOFOLLOW_LINKS))
            throw new java.nio.file.FileAlreadyExistsException(destination.toString());
        return destination;
    }

    /** Emit the already sorted object collection once, then commit completed bytes. */
    public static <T> Map<String, Object> write(Path admittedRoot, String invocationId, List<T> sortedObjects,
            Function<T, Row> projection) throws IOException {
        Path destination = prepareOutput(admittedRoot, invocationId);
        var factory = new SimpleGroupFactory(SCHEMA);
        String previous = null;
        Set<String> pathIds = new HashSet<>();
        long count;
        try (var writer = new LocalParquetWriter(destination, SCHEMA)) {
            for (T object : sortedObjects) {
                Row row = projection.apply(object);
                if (previous != null && row.roiId().compareTo(previous) <= 0)
                    throw new IllegalArgumentException("ROI inventory IDs must be strictly ascending");
                if (!pathIds.add(row.pathObjectId()))
                    throw new IllegalArgumentException("ROI inventory repeats a native path object ID");
                writer.write(factory.newGroup().append("roi_id", row.roiId()).append("path_object_id", row.pathObjectId())
                    .append("object_revision_sha256", Binary.fromConstantByteArray(HexFormat.of().parseHex(row.objectRevisionSha256())))
                    .append("class_id", row.classId()));
                previous = row.roiId();
            }
            count = writer.getRowCount();
        }
        // LocalParquetWriter completed its footer/schema/count check and fsync.
        // We hash its completed bytes without another footer or row-table read.
        try (var pin = new PinnedInputFile(root(admittedRoot), member(invocationId))) {
            Map<String, Object> descriptor = new LinkedHashMap<>();
            descriptor.put("path", member(invocationId)); descriptor.put("sha256", pin.sha256());
            descriptor.put("byte_count", pin.getLength()); descriptor.put("row_count", count);
            pin.validate();
            return descriptor;
        }
    }

    /** Hash and decode the same retained file, emitting only one typed row at a time. */
    public static long read(Path admittedRoot, Map<String, ?> descriptor, String expectedMember,
            Consumer<Row> rows) throws IOException {
        LocalParquet.requireWorker();
        if (descriptor == null || !descriptor.keySet().equals(REFERENCE_FIELDS)
                || !(descriptor.get("path") instanceof String path) || !path.equals(expectedMember))
            throw new IllegalArgumentException("ROI table descriptor differs from its admitted producer member");
        checkMember(expectedMember);
        String expectedDigest = digest(descriptor.get("sha256"));
        long bytes = integer(descriptor.get("byte_count"), false), count = integer(descriptor.get("row_count"), true);
        try (var pin = new PinnedInputFile(root(admittedRoot), expectedMember)) {
            if (pin.getLength() != bytes || !pin.sha256().equals(expectedDigest))
                throw new IOException("ROI inventory bytes differ from their accepted descriptor");
            String[] previous = {null};
            Set<String> pathIds = new HashSet<>();
            long observed = LocalParquet.read(pin, null, COLUMNS, group -> {
                for (int field = 0; field < 4; field++)
                    if (group.getFieldRepetitionCount(field) != 1)
                        throw new IllegalArgumentException("ROI inventory requires exactly one value per field");
                byte[] revision = group.getBinary("object_revision_sha256", 0).getBytes();
                if (revision.length != 32) throw new IllegalArgumentException("ROI inventory revision is not binary32");
                Row row = new Row(group.getString("roi_id", 0), group.getString("path_object_id", 0),
                    HexFormat.of().formatHex(revision), group.getString("class_id", 0));
                if (previous[0] != null && row.roiId().compareTo(previous[0]) <= 0)
                    throw new IllegalArgumentException("ROI inventory IDs are not strictly ascending");
                if (!pathIds.add(row.pathObjectId()))
                    throw new IllegalArgumentException("ROI inventory repeats a native path object ID");
                previous[0] = row.roiId(); rows.accept(row);
            }, SCHEMA, count);
            if (observed != count) throw new IOException("ROI inventory decoded count differs from its descriptor");
            pin.validate();
            return observed;
        }
    }

    private static String text(Object value, String field) {
        if (!(value instanceof String text) || text.isEmpty() || !text.equals(text.strip()) || text.indexOf('\0') >= 0)
            throw new IllegalArgumentException(field + " must be canonical nonempty text");
        return text;
    }
    private static String digest(Object value) {
        if (!(value instanceof String text) || !text.matches("[0-9a-f]{64}"))
            throw new IllegalArgumentException("ROI inventory requires a lowercase SHA-256");
        return text;
    }
    private static long integer(Object value, boolean zeroAllowed) {
        if (!(value instanceof Number number)) throw new IllegalArgumentException("ROI inventory count must be an integer");
        final long result;
        try { result = new BigDecimal(number.toString()).longValueExact(); }
        catch (NumberFormatException | ArithmeticException invalid) { throw new IllegalArgumentException("ROI inventory count must be an integer", invalid); }
        if (result < (zeroAllowed ? 0 : 1)) throw new IllegalArgumentException("ROI inventory count is outside its allowed range");
        return result;
    }
    private static void checkMember(String path) {
        if (path == null || !path.startsWith("roi-inventories/") || !path.endsWith(".parquet")
                || !member(path.substring("roi-inventories/".length(), path.length() - ".parquet".length())).equals(path))
            throw new IllegalArgumentException("ROI inventory member is not canonical");
    }
    private static Path root(Path path) throws IOException {
        if (path == null || !path.isAbsolute() || !path.equals(path.normalize()) || !path.toRealPath().equals(path)
                || !Files.isDirectory(path, LinkOption.NOFOLLOW_LINKS))
            throw new IOException("ROI inventory root must be an admitted real absolute directory");
        return path;
    }
}
