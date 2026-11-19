package org.mif.inference;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.channels.FileChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.security.MessageDigest;
import java.util.Arrays;
import java.util.HexFormat;
import java.util.Map;

/** A persistent spatial grid of frozen vectors, independent of labels and graph folds. */
public final class FrozenPatchTable implements AutoCloseable {
    private static final int WIDTH = 768;
    private static final int ROW_BYTES = WIDTH * Float.BYTES;
    private static final Object PROCESS_LOCK = new Object();
    private final Path descriptor;
    private final String descriptorSha;
    private final int rows, columns, offset;
    private final FileChannel matrix, completed, lock;

    /**
     * We identify the immutable image, physical grid, ordered panel and encoder
     * once. A sparse standard NumPy matrix stores 768 float32 values per grid
     * position; a parallel SHA table marks completed rows. Unvisited positions
     * consume no embedding allocation. Interrupted cache writes are recomputed,
     * while completed rows can be opened directly by the Python graph fitter.
     */
    public FrozenPatchTable(Path root, String identity, int rows, int columns) throws IOException {
        if (rows < 1 || columns < 1) throw new IllegalArgumentException("The patch grid is empty");
        this.rows = rows; this.columns = columns;
        long count = Math.multiplyExact((long)rows, columns);
        var metadata = new JsonObject();
        metadata.addProperty("schema_version", "mif-kronos-patch-table/v1");
        metadata.add("identity", JsonParser.parseString(identity));
        metadata.addProperty("grid_rows", rows); metadata.addProperty("grid_columns", columns);
        metadata.addProperty("feature_width", WIDTH); metadata.addProperty("dtype", "<f4");
        String shape = "{'descr': '<f4', 'fortran_order': False, 'shape': (" + count + ", 768), }";
        String header = shape + " ".repeat((64 - (10 + shape.length() + 1) % 64) % 64) + "\n";
        offset = 10 + header.length();
        metadata.addProperty("data_offset", offset);
        byte[] bytes = metadata.toString().getBytes(StandardCharsets.UTF_8);
        descriptorSha = hex(digest(bytes));
        Path directory = root.resolve(descriptorSha); Files.createDirectories(directory);
        descriptor = directory.resolve("dataset.json");
        lock = FileChannel.open(directory.resolve("write.lock"), StandardOpenOption.CREATE, StandardOpenOption.WRITE);
        FileChannel data = null, validity = null;
        try {
            synchronized (PROCESS_LOCK) {
                try (var held = lock.lock()) {
                    data = openNumeric(directory.resolve("features.npy"));
                    validity = openNumeric(directory.resolve("completed.sha256"));
                    if (!Files.exists(descriptor)) {
                        data.truncate(0); validity.truncate(0);
                        ByteBuffer prefix = ByteBuffer.allocate(offset).order(ByteOrder.LITTLE_ENDIAN);
                        prefix.put((byte)0x93).put("NUMPY".getBytes(StandardCharsets.US_ASCII));
                        prefix.put((byte)1).put((byte)0).putShort((short)header.length());
                        prefix.put(header.getBytes(StandardCharsets.US_ASCII)).flip(); write(data, prefix, 0);
                        write(data, ByteBuffer.wrap(new byte[1]), Math.addExact(offset, Math.multiplyExact(count, ROW_BYTES)) - 1);
                        write(validity, ByteBuffer.wrap(new byte[1]), Math.multiplyExact(count, 32) - 1);
                        data.force(true); validity.force(true);
                        Path temporary = Files.createTempFile(directory, ".dataset-", ".tmp");
                        try {
                            Files.write(temporary, bytes);
                            try { Files.move(temporary, descriptor, java.nio.file.StandardCopyOption.ATOMIC_MOVE); }
                            catch (java.nio.file.AtomicMoveNotSupportedException unsupported) { Files.move(temporary, descriptor); }
                        } finally { Files.deleteIfExists(temporary); }
                    } else if (!Arrays.equals(Files.readAllBytes(descriptor), bytes)
                            || data.size() != offset + count * ROW_BYTES || validity.size() != count * 32) {
                        throw new IOException("The frozen patch table differs from its declared grid");
                    }
                }
            }
            matrix = data; completed = validity;
        } catch (Throwable failure) {
            for (FileChannel channel : new FileChannel[]{data, validity, lock}) {
                if (channel != null) try { channel.close(); }
                catch (IOException cleanup) { failure.addSuppressed(cleanup); }
            }
            throw failure;
        }
    }

    private long index(int row, int column) {
        if (row < 0 || column < 0 || row >= rows || column >= columns)
            throw new IllegalArgumentException("Patch coordinates exceed the recorded grid");
        return (long)row * columns + column;
    }

    /** Read one completed vector; a missing or interrupted cache row needs encoding. */
    public float[] read(int row, int column) throws IOException {
        long position = index(row, column);
        synchronized (PROCESS_LOCK) {
            try (var held = lock.lock()) {
                byte[] expected = new byte[32]; read(completed, ByteBuffer.wrap(expected), position * 32);
                if (Arrays.equals(expected, new byte[32])) return null;
                byte[] bytes = new byte[ROW_BYTES]; read(matrix, ByteBuffer.wrap(bytes), offset + position * ROW_BYTES);
                if (!Arrays.equals(expected, digest(bytes))) return null;
                float[] values = new float[WIDTH];
                ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN).asFloatBuffer().get(values);
                return values;
            }
        }
    }

    /** Publish a row after its numerical bytes; reference labels never enter this table. */
    public float[] write(int row, int column, float[] values) throws IOException {
        if (values.length != WIDTH) throw new IllegalArgumentException("The frozen vector width changed");
        for (float value : values) if (!Float.isFinite(value)) throw new IllegalArgumentException("Frozen vectors must be finite");
        long position = index(row, column);
        ByteBuffer bytes = ByteBuffer.allocate(ROW_BYTES).order(ByteOrder.LITTLE_ENDIAN);
        bytes.asFloatBuffer().put(values);
        synchronized (PROCESS_LOCK) {
            try (var held = lock.lock()) {
                byte[] previous = new byte[32];
                read(completed, ByteBuffer.wrap(previous), position * 32);
                if (!Arrays.equals(previous, new byte[32])) {
                    byte[] stored = new byte[ROW_BYTES];
                    read(matrix, ByteBuffer.wrap(stored), offset + position * ROW_BYTES);
                    if (Arrays.equals(previous, digest(stored))) {
                        float[] retained = new float[WIDTH];
                        ByteBuffer.wrap(stored).order(ByteOrder.LITTLE_ENDIAN).asFloatBuffer().get(retained);
                        return retained;
                    }
                }
                write(matrix, bytes, offset + position * ROW_BYTES);
                write(completed, ByteBuffer.wrap(digest(bytes.array())), position * 32);
            }
        }
        return values;
    }

    /** A compact row locator lets graph fitting map the canonical numeric source. */
    public Map<String,Object> reference(int row, int column) {
        return Map.of("path", descriptor.toString(), "sha256", descriptorSha, "row_index", index(row, column));
    }

    private static FileChannel openNumeric(Path path) throws IOException {
        return Files.exists(path)
                ? FileChannel.open(path, StandardOpenOption.READ, StandardOpenOption.WRITE)
                : FileChannel.open(path, StandardOpenOption.CREATE_NEW, StandardOpenOption.SPARSE,
                        StandardOpenOption.READ, StandardOpenOption.WRITE);
    }
    private static void read(FileChannel channel, ByteBuffer bytes, long offset) throws IOException {
        while (bytes.hasRemaining()) { int size = channel.read(bytes, offset); if (size < 0) throw new IOException("Incomplete frozen patch row"); offset += size; }
    }
    private static void write(FileChannel channel, ByteBuffer bytes, long offset) throws IOException {
        while (bytes.hasRemaining()) offset += channel.write(bytes, offset);
    }
    private static byte[] digest(byte[] bytes) {
        try { return MessageDigest.getInstance("SHA-256").digest(bytes); }
        catch (java.security.NoSuchAlgorithmException impossible) { throw new AssertionError(impossible); }
    }
    private static String hex(byte[] bytes) { return HexFormat.of().formatHex(bytes); }
    @Override public void close() throws IOException {
        try { matrix.close(); } finally { try { completed.close(); } finally { lock.close(); } }
    }
}
