package org.mif.image;

import dev.zarr.zarrjava.store.FilesystemStore;
import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.ByteBuffer;
import java.nio.channels.SeekableByteChannel;
import java.nio.file.Files;
import java.nio.file.NoSuchFileException;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.atomic.AtomicLong;

/** Read-only filesystem ranges with complete short reads and bounded shard indexes. */
final class RangeStore extends FilesystemStore {
    private final Path root;
    private final long indexBudget;
    private final Map<String, Integer> requiredLevels = new java.util.concurrent.ConcurrentHashMap<>();
    private long indexBytes;
    private final Map<String, byte[]> indexes = new LinkedHashMap<>(16, 0.75f, true);
    final AtomicLong reads = new AtomicLong();
    final AtomicLong bytes = new AtomicLong();

    RangeStore(Path root, long indexBudget) {
        super(root);
        this.root = root.toAbsolutePath().normalize();
        this.indexBudget = indexBudget;
    }

    private Path path(String[] keys) {
        Path target = root;
        for (String key : keys) target = target.resolve(key);
        target = target.normalize();
        if (!target.startsWith(root)) throw new IllegalArgumentException("Image key leaves its directory");
        return target;
    }

    @Override public ByteBuffer get(String[] keys) { return range(keys, 0, null); }
    @Override public ByteBuffer get(String[] keys, long start) {
        if (start >= 0) return range(keys, start, null);
        String key = String.join("/", keys) + ":" + start;
        synchronized (indexes) {
            byte[] cached = indexes.get(key);
            if (cached != null) return ByteBuffer.wrap(cached).asReadOnlyBuffer();
            ByteBuffer read = range(keys, start, null);
            if (read == null) return null;
            byte[] data = new byte[read.remaining()];
            read.get(data);
            if (data.length <= indexBudget) {
                while (indexBytes + data.length > indexBudget && !indexes.isEmpty()) {
                    var iterator = indexes.entrySet().iterator();
                    indexBytes -= iterator.next().getValue().length;
                    iterator.remove();
                }
                indexes.put(key, data);
                indexBytes += data.length;
            }
            return ByteBuffer.wrap(data).asReadOnlyBuffer();
        }
    }
    @Override public ByteBuffer get(String[] keys, long start, long end) { return range(keys, start, end); }

    private ByteBuffer range(String[] keys, long start, Long end) {
        try (SeekableByteChannel channel = Files.newByteChannel(path(keys))) {
            long size = channel.size();
            long first = start < 0 ? size + start : start;
            long last = end == null ? size : end;
            if (first < 0 || last < first || last > size || last - first > Integer.MAX_VALUE)
                throw new IOException("Invalid or truncated image range: " + first + ".." + last + " of " + size);
            ByteBuffer result = ByteBuffer.allocate((int)(last - first));
            channel.position(first);
            while (result.hasRemaining()) {
                if (channel.read(result) < 0) throw new IOException("Image range ended before its declared length");
            }
            reads.incrementAndGet();
            bytes.addAndGet(result.capacity());
            result.flip();
            return result;
        } catch (NoSuchFileException missing) {
            String[] parts = root.relativize(path(keys)).toString().replace('\\', '/').split("/");
            if (parts.length == 5 && parts[1].equals("c") && requiredLevels.containsKey(parts[0])) {
                int index = Integer.parseInt(parts[3]) * requiredLevels.get(parts[0]) + Integer.parseInt(parts[4]);
                try (SeekableByteChannel bitmap = Files.newByteChannel(root.resolve(parts[0]).resolve(".stored.bin"))) {
                    bitmap.position(index / 8);
                    ByteBuffer bit = ByteBuffer.allocate(1);
                    if (bitmap.read(bit) != 1 || (bit.get(0) & (1 << (index % 8))) != 0)
                        throw new IOException("Processed image is missing a completed non-fill shard");
                } catch (IOException failure) {
                    throw new UncheckedIOException("Unable to recover processed image shard " + String.join("/", keys), failure);
                }
            }
            return null; // A missing Zarr chunk has the declared fill value.
        } catch (IOException error) {
            throw new UncheckedIOException("Unable to read processed image " + String.join("/", keys), error);
        }
    }

    @Override public void set(String[] keys, ByteBuffer data) { throw new UnsupportedOperationException("Processed image is read-only"); }
    @Override public void delete(String[] keys) { throw new UnsupportedOperationException("Processed image is read-only"); }
    void clear() { synchronized (indexes) { indexes.clear(); indexBytes = 0; } }
    void declareCompletedLevel(String name, int columns) { requiredLevels.put(name, columns); }
}
