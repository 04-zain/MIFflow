package org.mif.inference;

import java.awt.image.BufferedImage;
import java.io.IOException;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collection;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import com.google.gson.Gson;
import org.bytedeco.opencv.opencv_core.Mat;
import qupath.lib.classifiers.pixel.PixelClassifier;
import qupath.lib.images.ImageData;
import qupath.lib.images.servers.ImageChannel;
import qupath.lib.images.servers.PixelCalibration;
import qupath.lib.images.servers.PixelType;
import qupath.lib.io.GsonTools;
import qupath.lib.objects.classes.PathClass;
import qupath.lib.regions.RegionRequest;
import qupath.opencv.ml.pixel.PixelClassifiers;
import qupath.opencv.ops.ImageDataOp;
import qupath.opencv.ops.ImageOp;
import qupath.opencv.ops.ImageOps;

/** Serialized native patch classifier; the live owner supplies accepted image scopes. */
public final class MifKronosResourceOp implements ImageDataOp {
    private String modelJson;
    private String modelSha256;
    private String resourceRevision;
    private Map<String,Object> convention;
    private Map<String,Object> workerSettings;
    private Map<String,Object> channelFeatures;
    private Map<String,Object> reviewPolicy;
    private List<Integer> outputCodes;
    static{ImageOps.registerDataOp(MifKronosResourceOp.class,"data.op.mif-kronos-native-v1");}
    public String getModelJson(){return modelJson;}
    public String getModelSha256(){return modelSha256;}
    public String getResourceRevision(){return resourceRevision;}
    public Map<String,Object> getConvention(){return convention;}
    public Map<String,Object> getWorkerSettings(){return KronosEncoderInputs.saved(workerSettings);}
    public Map<String,Object> getChannelFeatures(){return channelFeatures;}
    public Map<String,Object> getReviewPolicy(){return reviewPolicy;}
    public List<Integer> getOutputCodes(){return outputCodes;}
    @Override public Mat apply(ImageData<BufferedImage> image,RegionRequest request)throws IOException{return MifKronosResourceRuntime.context(image,this).render(request);}
    @Override public boolean supportsImage(ImageData<BufferedImage> image){return MifKronosResourceRuntime.supportsImage(image);}
    @Override public List<ImageChannel> getChannels(ImageData<BufferedImage> image){return List.of(ImageChannel.getInstance("Class",null));}
    @Override public ImageDataOp appendOps(ImageOp... operations){if(operations.length>0)throw new UnsupportedOperationException("KRONOS output is a final patch label");return this;}
    @Override public PixelType getOutputType(PixelType input){return PixelType.UINT8;}
    @Override public Collection<URI> getURIs(){return List.of();}
    @Override public boolean updateURIs(Map<URI,URI> replacements){return false;}

    public static PixelClassifier create(byte[] bytes,Map<String,Object> convention,Map<String,Object> worker,
            Map<String,Object> channels,List<? extends Number> codes,Map<Integer,PathClass> labels,Map<String,Object> policy)throws IOException {
        if(codes==null || codes.size()>128)throw new IllegalArgumentException("Native patch classes require unique byte labels excluding 255");
        var operation=new MifKronosResourceOp();
        operation.modelJson=new String(bytes,StandardCharsets.UTF_8);operation.modelSha256=MifKronosModelCodec.digest(bytes);
        operation.resourceRevision=UUID.randomUUID().toString();operation.convention=convention;operation.workerSettings=worker;
        operation.channelFeatures=channels;operation.outputCodes=MifKronosResourceContext.outputCodes(codes);operation.reviewPolicy=policy;
        var pixels=(List<?>)convention.get("pixel_size_um");
        var calibration=new PixelCalibration.Builder().pixelSizeMicrons(((Number)pixels.get(0)).doubleValue(),((Number)pixels.get(1)).doubleValue()).build();
        return PixelClassifiers.createClassifier(operation,calibration,labels);
    }
    @SuppressWarnings("unchecked")
    public static Map<String,Object> describe(PixelClassifier classifier) {
        Map<String,Object> tree=new Gson().fromJson(GsonTools.getInstance().toJson(classifier,PixelClassifier.class),Map.class);
        List<Map<String,Object>> matches=new ArrayList<>();visit(tree,matches);
        if(matches.size()!=1)throw new IllegalArgumentException("Select a saved KRONOS classifier");
        Map<String,Object> result=matches.get(0);
        result.put("workerSettings",KronosEncoderInputs.saved(
                (Map<String,Object>)result.get("workerSettings")));
        return result;
    }
    @SuppressWarnings("unchecked")
    private static void visit(Object value,List<Map<String,Object>> matches) {
        if(value instanceof Map<?,?> map) {
            if(map.containsKey("modelJson") && map.containsKey("modelSha256") && map.containsKey("workerSettings"))matches.add((Map<String,Object>)map);
            else map.values().forEach(child->visit(child,matches));
        }else if(value instanceof List<?> list)list.forEach(child->visit(child,matches));
    }
}
