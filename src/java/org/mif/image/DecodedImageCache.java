package org.mif.image;

import java.awt.image.BufferedImage;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.Map;

/** One byte-bounded decoded cache shared by the native process's image servers. */
final class DecodedImageCache {
    private record Entry(String owner,BufferedImage image,long bytes) {}
    private static final long budget=Long.parseLong(System.getenv().getOrDefault("MIF_IMAGE_CACHE_BYTES",Long.toString(64L*1024*1024)));
    private static final Map<String,Integer> owners=new HashMap<>();
    private static final LinkedHashMap<String,Entry> entries=new LinkedHashMap<>(16,.75f,true);
    private static long bytes;
    private DecodedImageCache() {}
    static synchronized void open(String owner){owners.merge(owner,1,Integer::sum);}
    static synchronized BufferedImage get(String owner,String key){var entry=entries.get(owner+":"+key);return entry==null?null:entry.image;}
    static synchronized void put(String owner,String key,BufferedImage image,long size,long ownerBudget) {
        if(size>Math.min(budget,ownerBudget)||!owners.containsKey(owner))return;
        while((bytes+size>budget||ownerBytes(owner)+size>ownerBudget)&&!entries.isEmpty()) {
            var iterator=entries.entrySet().iterator();var removed=iterator.next().getValue();bytes-=removed.bytes;iterator.remove();
        }
        String id=owner+":"+key;var old=entries.remove(id);if(old!=null)bytes-=old.bytes;
        entries.put(id,new Entry(owner,image,size));bytes+=size;
    }
    static synchronized long ownerBytes(String owner){return entries.values().stream().filter(entry->entry.owner.equals(owner)).mapToLong(entry->entry.bytes).sum();}
    static synchronized long processBytes(){return bytes;}
    static synchronized void close(String owner) {
        if(owners.getOrDefault(owner,0)>1){owners.merge(owner,-1,Integer::sum);return;}
        owners.remove(owner);var iterator=entries.entrySet().iterator();
        while(iterator.hasNext()){var entry=iterator.next().getValue();if(entry.owner.equals(owner)){bytes-=entry.bytes;iterator.remove();}}
    }
}
