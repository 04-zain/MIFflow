package org.mif.inference;

import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.atomic.AtomicLong;

/** One process allowance for immutable frozen patch vectors across image backends. */
public final class FrozenFeatureCache {
    private record Key(long owner, int row, int col) {}
    private static final AtomicLong OWNERS = new AtomicLong();
    private static final LinkedHashMap<Key, float[]> VALUES = new LinkedHashMap<>(64, 0.75f, true);
    private static final long LIMIT = allowance();
    private static long residentBytes;

    private FrozenFeatureCache() {}

    private static long allowance() {
        String configured = System.getenv("MIF_FEATURE_CACHE_BYTES");
        long value = configured == null ? Math.min(64L << 20, Runtime.getRuntime().maxMemory() / 256)
            : Long.parseLong(configured);
        if (value <= 0) throw new IllegalArgumentException("Frozen feature cache allowance must be positive");
        return value;
    }

    private static long charge(float[] values) { return 128L + 4L * values.length; }
    public static long newOwner() { return OWNERS.incrementAndGet(); }
    public static long limitBytes() { return LIMIT; }
    public static synchronized long residentBytes() { return residentBytes; }

    /** Returned vectors remain owned by the cache; callers read them without mutation. */
    public static synchronized float[] get(long owner, int row, int col) {
        return VALUES.get(new Key(owner, row, col));
    }

    public static synchronized void put(long owner, int row, int col, float[] values) {
        if (values == null || values.length < 1 || values.length > 4096)
            throw new IllegalArgumentException("Frozen feature dimensions are invalid");
        for (float value : values)
            if (!Float.isFinite(value)) throw new IllegalArgumentException("Frozen features must be finite");
        Key key = new Key(owner, row, col);
        float[] previous = VALUES.remove(key);
        if (previous != null) residentBytes -= charge(previous);
        long bytes = charge(values);
        if (bytes > LIMIT) return;
        Iterator<Map.Entry<Key, float[]>> entries = VALUES.entrySet().iterator();
        while (residentBytes + bytes > LIMIT && entries.hasNext()) {
            residentBytes -= charge(entries.next().getValue());
            entries.remove();
        }
        VALUES.put(key, values);
        residentBytes += bytes;
    }

    public static synchronized void release(long owner) {
        Iterator<Map.Entry<Key, float[]>> entries = VALUES.entrySet().iterator();
        while (entries.hasNext()) {
            Map.Entry<Key, float[]> entry = entries.next();
            if (entry.getKey().owner == owner) {
                residentBytes -= charge(entry.getValue());
                entries.remove();
            }
        }
    }
}
