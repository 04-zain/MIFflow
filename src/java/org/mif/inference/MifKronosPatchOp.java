package org.mif.inference;

import java.awt.image.BufferedImage;
import java.io.IOException;
import java.lang.ref.WeakReference;
import java.net.URI;
import java.util.Collection;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import org.bytedeco.opencv.opencv_core.Mat;
import qupath.lib.images.ImageData;
import qupath.lib.images.servers.ImageChannel;
import qupath.lib.images.servers.PixelType;
import qupath.lib.regions.RegionRequest;
import qupath.opencv.ops.ImageDataOp;
import qupath.opencv.ops.ImageOp;
import qupath.opencv.ops.ImageOps;

/** A preview operation binds one immutable revision to one live model session. */
public final class MifKronosPatchOp implements ImageDataOp {
    private static final Map<String,WeakReference<MifKronosPatchProvider>> PROVIDERS=new ConcurrentHashMap<>();
    private String revision;
    static {ImageOps.registerDataOp(MifKronosPatchOp.class,"data.op.mif-kronos-patch-preview-v1");}
    private MifKronosPatchOp(String revision){this.revision=revision;}
    public static MifKronosPatchOp create(String revision,MifKronosPatchProvider provider) {
        var previous=PROVIDERS.putIfAbsent(revision,new WeakReference<>(provider));
        if(previous!=null && previous.get()!=provider)
            throw new IllegalArgumentException("An existing preview must not change its numerical model");
        return new MifKronosPatchOp(revision);
    }
    static void retire(MifKronosPatchProvider provider) {
        PROVIDERS.entrySet().removeIf(entry->entry.getValue().get()==null || entry.getValue().get()==provider);
    }
    private MifKronosPatchProvider provider() throws IOException {
        var reference=PROVIDERS.get(revision);
        var provider=reference==null?null:reference.get();
        if(provider==null)throw new IOException("This patch classifier requires its exact configured model session");
        return provider;
    }
    @Override public Mat apply(ImageData<BufferedImage> image,RegionRequest request)throws IOException{return provider().render(image,request);}
    @Override public boolean supportsImage(ImageData<BufferedImage> image){try{return provider().getImage()==image;}catch(IOException ignored){return false;}}
    @Override public List<ImageChannel> getChannels(ImageData<BufferedImage> image){return List.of(ImageChannel.getInstance("Class",null));}
    @Override public ImageDataOp appendOps(ImageOp... ops){if(ops.length>0)throw new UnsupportedOperationException("Patch classes are final labels, not image features");return this;}
    @Override public PixelType getOutputType(PixelType input){return PixelType.UINT8;}
    @Override public Collection<URI> getURIs(){return List.of();}
    @Override public boolean updateURIs(Map<URI,URI> replacements){return false;}
}
