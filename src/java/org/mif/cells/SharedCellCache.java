package org.mif.cells;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;

/** One process allowance for retained display geometry and projected scalar groups. */
final class SharedCellCache {
    private record Key(Object owner,Object query) {}
    private record Entry(Object value,long bytes,Runnable release) {}
    private static final long LIMIT=Long.parseLong(System.getenv().getOrDefault("MIF_OBJECT_CACHE_BYTES",Long.toString(Long.getLong("mif.cell.memoryBytes",64L*1024*1024))));
    private static final LinkedHashMap<Key,Entry> VALUES=new LinkedHashMap<>(16,.75f,true);
    private static long bytes,evictions;
    private static final long METADATA_LIMIT=Long.parseLong(System.getenv().getOrDefault("MIF_METADATA_CACHE_BYTES",Long.toString(Math.max(1,LIMIT/4))));
    private static final java.util.IdentityHashMap<Object,Long> METADATA=new java.util.IdentityHashMap<>();
    private static long metadataBytes;
    private static final java.util.concurrent.ThreadPoolExecutor READS=new java.util.concurrent.ThreadPoolExecutor(1,1,30,java.util.concurrent.TimeUnit.SECONDS,new java.util.concurrent.ArrayBlockingQueue<>(8),task->{var thread=new Thread(task,"mif-cell-display-read");thread.setDaemon(true);return thread;},new java.util.concurrent.ThreadPoolExecutor.AbortPolicy());
    static {READS.allowCoreThreadTimeOut(true);}
    private SharedCellCache() {}

    /** We return an owner-specific admitted value and refresh its process LRU position. */
    static synchronized Object get(Object owner,Object query){var entry=VALUES.get(new Key(owner,query));return entry==null?null:entry.value;}

    /**
     * We reserve retained bytes under one process cap and evict the oldest owners
     * across images. Release callbacks run outside the cache lock, allowing a
     * retired section snapshot to release its dependent composite view safely.
     * Values larger than the entire allowance cannot be retained.
     */
    static void put(Object owner,Object query,Object value,long weight,Runnable release) {
        if(weight<0||weight>LIMIT)throw new IllegalArgumentException("Prepared cell cache value exceeds the process object allowance");
        var retired=new ArrayList<Runnable>();
        synchronized(SharedCellCache.class) {
            // i) Remove a replaced reservation before admitting the new value.
            var key=new Key(owner,query);var previous=VALUES.remove(key);if(previous!=null){bytes-=previous.bytes;retired.add(previous.release);}
            // ii) Evict across all open images rather than granting each a new cap.
            while(bytes+weight>LIMIT&&!VALUES.isEmpty()){var iterator=VALUES.entrySet().iterator();var oldest=iterator.next().getValue();iterator.remove();bytes-=oldest.bytes;evictions++;retired.add(oldest.release);}
            VALUES.put(key,new Entry(value,weight,release));bytes+=weight;
        }
        retired.forEach(Runnable::run);
    }
    /** We release only the matching payload, so retirement cannot remove a newer value with the same query. */
    static void remove(Object owner,Object query,Object expected){Entry entry;synchronized(SharedCellCache.class){var key=new Key(owner,query);entry=VALUES.get(key);if(entry==null||entry.value!=expected)return;VALUES.remove(key);bytes-=entry.bytes;}entry.release.run();}
    /** We release all retained values for a closed or superseded owner, preserving other images. */
    static void release(Object owner) {
        List<Runnable> retired=new ArrayList<>();
        synchronized(SharedCellCache.class){var iterator=VALUES.entrySet().iterator();while(iterator.hasNext()){var entry=iterator.next();if(entry.getKey().owner==owner){bytes-=entry.getValue().bytes;retired.add(entry.getValue().release);iterator.remove();}}}
        retired.forEach(Runnable::run);
    }
    /** We serialize background display reads under one process worker and a bounded queue; saturation is explicit. */
    static void execute(Runnable task){READS.execute(task);}
    /**
     * We reserve retained descriptors, dictionaries and spatial indexes before
     * parsing or expanding them. Metadata remains owned by accepted open stores;
     * combined demand exceeding its separate process slice fails admission.
     */
    static synchronized void metadata(Object owner,long weight){long previous=METADATA.getOrDefault(owner,0L);if(weight<0||weight>METADATA_LIMIT||metadataBytes-previous+weight>METADATA_LIMIT)throw new IllegalArgumentException("Open cell metadata and indexes exceed the process metadata allowance");METADATA.put(owner,weight);metadataBytes=metadataBytes-previous+weight;}
    static synchronized void releaseMetadata(Object owner){Long weight=METADATA.remove(owner);if(weight!=null)metadataBytes-=weight;}
    static synchronized long metadataBytes(){return metadataBytes;}
    static long metadataLimit(){return METADATA_LIMIT;}
    static synchronized long bytes(){return bytes;}
    static synchronized long bytes(Object owner){return VALUES.entrySet().stream().filter(entry->entry.getKey().owner==owner).mapToLong(entry->entry.getValue().bytes).sum();}
    static long limit(){return LIMIT;}
    static synchronized long evictions(){return evictions;}
}
