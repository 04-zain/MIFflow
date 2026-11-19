package org.mif.inference;

import java.awt.image.BufferedImage;
import java.io.IOException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.WeakHashMap;
import org.locationtech.jts.geom.Geometry;
import qupath.lib.images.ImageData;

/** Existing image-scoped deployment reuse; stage owners authenticate scope input. */
public final class MifKronosResourceRuntime {
    @FunctionalInterface public interface ContextFactory {
        MifKronosResourceContext open(ImageData<BufferedImage> image,MifKronosResourceOp operation,List<Map<String,Object>> scopes)throws IOException;
    }
    private static ContextFactory factory;
    private static final Map<ImageData<BufferedImage>,List<Map<String,Object>>> SCOPES=Collections.synchronizedMap(new WeakHashMap<>());
    private static final Map<ImageData<BufferedImage>,Map<String,MifKronosResourceContext>> CONTEXTS=Collections.synchronizedMap(new WeakHashMap<>());
    private MifKronosResourceRuntime() {}
    public static synchronized void configure(ContextFactory owner){factory=owner;}
    public static boolean supportsImage(ImageData<BufferedImage> image){return SCOPES.containsKey(image);}

    /** Copy and order the accepted component geometries before any inference. */
    public static void bind(ImageData<BufferedImage> image,List<Map<String,Object>> scopes)throws Exception {
        if(image==null || scopes==null || scopes.isEmpty() || scopes.size()>4096)
            throw new IllegalArgumentException("An accepted bounded image scope is required");
        List<Map<String,Object>> checked=new ArrayList<>();
        var names=new HashSet<String>();
        for(Map<String,Object> scope:scopes) {
            if(!scope.keySet().equals(Set.of("id","geometry","revision","split"))
                    || !(scope.get("id") instanceof String id) || id.isEmpty()
                    || !(scope.get("revision") instanceof String revision) || revision.isEmpty()
                    || !(scope.get("geometry") instanceof Geometry geometry) || !geometry.isValid() || geometry.isEmpty()
                    || !Set.of("training","validation","test","inference").contains(scope.get("split")) || !names.add(id))
                throw new IllegalArgumentException("Image prediction scopes are invalid or ambiguous");
            List<Geometry> components=new ArrayList<>();
            for(int i=0;i<geometry.getNumGeometries();i++)components.add(geometry.getGeometryN(i));
            components.sort(Comparator.comparing(Geometry::toText));
            for(int i=0;i<components.size();i++) {
                Map<String,Object> component=new LinkedHashMap<>();
                component.put("id",id+":component:"+i);component.put("geometry",components.get(i).copy());
                component.put("revision",revision);component.put("split",scope.get("split"));checked.add(component);
            }
        }
        closeImage(image);
        checked.sort((a,b)->{
            int area=Double.compare(((Geometry)b.get("geometry")).getArea(),((Geometry)a.get("geometry")).getArea());
            return area!=0?area:((String)b.get("id")).compareTo((String)a.get("id"));
        });
        SCOPES.put(image,checked);
    }
    public static void closeImage(ImageData<BufferedImage> image)throws Exception {
        var contexts=CONTEXTS.remove(image);SCOPES.remove(image);Throwable failure=null;
        if(contexts!=null)for(var context:contexts.values())try{context.close();}
        catch(Throwable error){if(failure==null)failure=error;else failure.addSuppressed(error);}
        if(failure instanceof Exception exception)throw exception;
        if(failure instanceof Error error)throw error;
    }
    static synchronized MifKronosResourceContext context(ImageData<BufferedImage> image,MifKronosResourceOp operation)throws IOException {
        if(!SCOPES.containsKey(image) || factory==null)
            throw new IOException("This classifier has no current accepted image scope; open it through the pipeline");
        var contexts=CONTEXTS.computeIfAbsent(image,key->new LinkedHashMap<>());
        String key=operation.getModelSha256()+"|"+operation.getResourceRevision();
        if(!contexts.containsKey(key))contexts.put(key,factory.open(image,operation,SCOPES.get(image)));
        return contexts.get(key);
    }
}
