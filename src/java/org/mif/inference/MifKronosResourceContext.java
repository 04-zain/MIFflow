package org.mif.inference;

import java.awt.image.BufferedImage;
import java.io.IOException;
import java.util.List;
import org.bytedeco.javacpp.indexer.UByteIndexer;
import org.bytedeco.opencv.global.opencv_core;
import org.bytedeco.opencv.opencv_core.Mat;
import org.bytedeco.opencv.opencv_core.Scalar;
import qupath.lib.images.ImageData;
import qupath.lib.regions.RegionRequest;

/** Owns the live deployment sessions and merges their bounded class rasters. */
public final class MifKronosResourceContext implements AutoCloseable {
    public record ProviderScope(AutoCloseable backend,MifKronosPatchProvider provider) {}
    private final ImageData<BufferedImage> image;
    private final List<ProviderScope> providers;
    private final List<Integer> outputCodes;
    private final MifKronosNativeFit fit;
    private final AutoCloseable client;
    private boolean closed;

    /** Ownership transfers only after the existing owner completes image setup. */
    public MifKronosResourceContext(ImageData<BufferedImage> image,List<ProviderScope> providers,
            List<Integer> outputCodes,MifKronosNativeFit fit,AutoCloseable client) {
        this.image=image;this.providers=List.copyOf(providers);this.outputCodes=List.copyOf(outputCodes);
        this.fit=fit;this.client=client;
    }
    public static List<Integer> outputCodes(List<? extends Number> codes)throws IOException {
        if(codes==null || codes.isEmpty())throw new IOException("The saved classifier output labels are invalid");
        var values=new java.util.ArrayList<Integer>(codes.size());
        var unique=new java.util.HashSet<Integer>();
        for(Number code:codes) {
            if(code==null || code.doubleValue()!=code.intValue() || code.intValue()<0 || code.intValue()>254 || !unique.add(code.intValue()))
                throw new IOException("The saved classifier output labels are invalid");
            values.add(code.intValue());
        }
        return List.copyOf(values);
    }
    public synchronized Mat render(RegionRequest request)throws IOException {
        if(closed)throw new IOException("The deployment context is closed");
        Mat result=null;
        try {
            for(ProviderScope item:providers) {
                try(Mat part=item.provider().render(image,request)) {
                    if(result==null) {
                        result=new Mat(part.rows(),part.cols(),opencv_core.CV_8UC1);
                        try(Scalar blank=new Scalar(255)){result.put(blank);}
                    }
                    UByteIndexer input=part.createIndexer(),output=result.createIndexer();
                    try {
                        for(int y=0;y<part.rows();y++)for(int x=0;x<part.cols();x++) {
                            int code=input.get(y,x);
                            if(code!=255) {
                                if(code>=outputCodes.size())throw new IOException("Native model emitted an unconfigured class");
                                output.put(y,x,outputCodes.get(code));
                            }
                        }
                    }finally{input.release();output.release();}
                }
            }
            return result;
        }catch(Throwable error){if(result!=null)result.close();throw error;}
    }
    /** Close every owned session even if one close fails; preserve its cause. */
    @Override public synchronized void close()throws Exception {
        if(closed)return;closed=true;
        Throwable failure=null;
        for(ProviderScope item:providers) {
            failure=close(item.provider(),failure);failure=close(item.backend(),failure);
        }
        failure=close(fit,failure);failure=close(client,failure);
        if(failure instanceof Exception exception)throw exception;
        if(failure instanceof Error error)throw error;
    }
    private static Throwable close(AutoCloseable owner,Throwable previous) {
        try{owner.close();}catch(Throwable error){if(previous==null)return error;previous.addSuppressed(error);}
        return previous;
    }
}
