package org.mif.inference;

import java.awt.Color;
import java.awt.RenderingHints;
import java.awt.geom.AffineTransform;
import java.awt.image.BufferedImage;
import java.awt.image.DataBufferByte;
import java.io.IOException;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Map;
import java.util.function.BiFunction;
import org.locationtech.jts.geom.Geometry;
import org.locationtech.jts.awt.ShapeWriter;
import org.bytedeco.javacpp.indexer.FloatIndexer;
import org.bytedeco.javacpp.indexer.IntIndexer;
import org.bytedeco.opencv.global.opencv_core;
import org.bytedeco.opencv.opencv_core.Mat;
import qupath.lib.images.ImageData;
import qupath.lib.regions.RegionRequest;
import qupath.opencv.ml.FeaturePreprocessor;
import qupath.opencv.ml.OpenCVClassifiers;

/** Bounded patch prediction; the backend owns reusable frozen embedding arrays. */
public final class MifKronosPatchProvider implements AutoCloseable {
    private final ImageData<BufferedImage> image;
    private final Geometry scope;
    private final double downsample;
    private final int patchSize;
    private final BiFunction<Integer,Integer,float[]> embeddingReader;
    private final int classCount;
    private final GraphHeadKernel kernel;
    private boolean closed;
    private OpenCVClassifiers.OpenCVStatModel nativeClassifier;
    private FeaturePreprocessor preprocessor;

    public MifKronosPatchProvider(ImageData<BufferedImage> image,Geometry scope,double scale,
            BiFunction<Integer,Integer,float[]> reader,Map<String,Object> learned,int classes,int patchSize) {
        if(image==null || scope==null || scope.isEmpty() || !scope.isValid()
                || !Double.isFinite(scale) || scale<1 || classes<2 || classes>254
                || (patchSize!=32 && patchSize!=64 && patchSize!=128 && patchSize!=256))
            throw new IllegalArgumentException("Patch image, scope, sampling scale and class count must be explicit");
        this.patchSize=patchSize;
        this.image=image;this.scope=scope.copy();downsample=scale;embeddingReader=reader;classCount=classes;
        kernel=new GraphHeadKernel((float[][])learned.get("self_weight"),(float[][])learned.get("neighbour_weight"),
            (float[])learned.get("bias"),(float[][])learned.get("head_weight"),(float[])learned.get("head_bias"),classes);
    }
    public ImageData<BufferedImage> getImage(){return image;}
    public void setNativeClassifier(OpenCVClassifiers.OpenCVStatModel model){nativeClassifier=model;}
    public void setPreprocessor(FeaturePreprocessor preparation){preprocessor=preparation;}

    /** Read one exact model-sized patch in the declared planar channel order.
     * The returned float array owns its samples; normalization belongs to the
     * frozen encoder. A multi-channel patch borrows one reusable plane buffer.
     */
    public static float[] readPatch(ImageData<BufferedImage> image,RegionRequest request,
            List<Integer> channels,int patchSize)throws IOException {
        BufferedImage patch=image.getServer().readRegion(request);
        if(patch==null || patch.getWidth()!=patchSize || patch.getHeight()!=patchSize)
            throw new IOException("The native server did not return the exact "+patchSize+"-pixel model patch");
        int pixels=Math.multiplyExact(patchSize,patchSize);
        float[] values=new float[Math.multiplyExact(channels.size(),pixels)];
        float[] plane=channels.size()==1?values:new float[pixels];
        for(int channel=0;channel<channels.size();channel++) {
            patch.getRaster().getSamples(0,0,patchSize,patchSize,channels.get(channel),plane);
            if(channels.size()>1)System.arraycopy(plane,0,values,channel*pixels,pixels);
        }
        return values;
    }

    private float[] embedding(int row,int col)throws IOException {
        if(closed)throw new IOException("The patch-feature provider is closed");
        float[] values=embeddingReader.apply(row,col);
        if(values==null || values.length!=kernel.getInputWidth())
            throw new IOException("A complete embedding is required for each admitted context patch");
        return values;
    }
    /** Borrow the cached vectors directly; only the existing graph batch is dense. */
    private float[][] featuresBatch(int[][] positions)throws IOException {
        if(positions.length<1 || positions.length>GraphHeadKernel.MAX_BATCH_ROWS)
            throw new IllegalArgumentException("Graph work must contain one to 64 patches");
        float[][] own=new float[positions.length][];
        float[][][] neighbors=new float[positions.length][][];
        for(int i=0;i<positions.length;i++) {
            int row=positions[i][0],col=positions[i][1];own[i]=embedding(row,col);
            var adjacent=MifKronosPatchGeometry.neighbors(scope,image.getServer().getWidth(),image.getServer().getHeight(),row,col,downsample,patchSize);
            neighbors[i]=new float[adjacent.size()][];
            for(int j=0;j<adjacent.size();j++)neighbors[i][j]=embedding(adjacent.get(j).get(0),adjacent.get(j).get(1));
        }
        return kernel.features(own,neighbors);
    }
    public synchronized float[] featuresAt(int row,int col)throws IOException{return featuresBatch(new int[][]{{row,col}})[0];}

    private int[] labelsFor(float[][] features)throws IOException {
        if(nativeClassifier==null)return kernel.labels(features);
        try(Mat samples=new Mat(features.length,kernel.getHiddenWidth(),opencv_core.CV_32FC1);Mat result=new Mat()) {
            FloatIndexer index=samples.createIndexer();
            try {for(int row=0;row<features.length;row++)for(int col=0;col<kernel.getHiddenWidth();col++)index.put(row,col,features[row][col]);}
            finally{index.release();}
            if(preprocessor!=null)preprocessor.apply(samples,false);
            nativeClassifier.predict(samples,result,null);
            int[] labels=new int[features.length];
            IntIndexer predicted=result.createIndexer();
            try {for(int row=0;row<labels.length;row++) {
                labels[row]=predicted.get(row);
                if(labels[row]<0 || labels[row]>=classCount)
                    throw new IOException("Native classifier returned an undeclared patch class");
            }}finally{predicted.release();}
            return labels;
        }
    }

    public synchronized Mat render(ImageData<BufferedImage> current,RegionRequest request)throws IOException {
        if(closed || current!=image)throw new IOException("The patch provider is not bound to this image");
        double scale=request.getDownsample();
        if(Math.abs(scale-downsample)>1e-9 || request.getZ()!=0 || request.getT()!=0)
            throw new IOException("Patch prediction requires the recorded image resolution and plane");
        int left=(int)Math.round(request.getX()/scale),top=(int)Math.round(request.getY()/scale);
        int right=(int)Math.round((request.getX()+request.getWidth())/scale),bottom=(int)Math.round((request.getY()+request.getHeight())/scale);
        if(Math.round(left*scale)!=request.getX() || Math.round(top*scale)!=request.getY()
                || Math.round(right*scale)!=request.getX()+request.getWidth() || Math.round(bottom*scale)!=request.getY()+request.getHeight())
            throw new IOException("The native tile envelope does not identify an exact output grid");
        int width=right-left,height=bottom-top;
        if(width<=0 || height<=0 || (long)width*height>1048576)
            throw new IOException("Prediction tile exceeds the bounded raster limit");
        var mask=new BufferedImage(width,height,BufferedImage.TYPE_BYTE_GRAY);
        var painter=mask.createGraphics();
        try {
            painter.setColor(Color.WHITE);painter.setRenderingHint(RenderingHints.KEY_ANTIALIASING,RenderingHints.VALUE_ANTIALIAS_OFF);
            painter.transform(new AffineTransform(1/scale,0,0,1/scale,-left,-top));painter.fill(new ShapeWriter().toShape(scope));
        }finally{painter.dispose();}
        byte[] pixels=((DataBufferByte)mask.getRaster().getDataBuffer()).getData();
        int[][] positions=GraphHeadKernel.requiredPatches(pixels,width,height,left,top,patchSize);
        int[] labels=new int[positions.length];Arrays.fill(labels,255);
        List<Integer> eligible=new ArrayList<>();
        for(int i=0;i<positions.length;i++)if(MifKronosPatchGeometry.admitted(scope,image.getServer().getWidth(),image.getServer().getHeight(),positions[i][0],positions[i][1],scale,patchSize))eligible.add(i);
        for(int start=0;start<eligible.size();start+=GraphHeadKernel.MAX_BATCH_ROWS) {
            int count=Math.min(eligible.size()-start,GraphHeadKernel.MAX_BATCH_ROWS);
            int[][] targets=new int[count][];
            for(int i=0;i<count;i++)targets[i]=positions[eligible.get(start+i)];
            int[] predicted=labelsFor(featuresBatch(targets));
            for(int i=0;i<count;i++)labels[eligible.get(start+i)]=predicted[i];
        }
        byte[] rendered=GraphHeadKernel.rasterize(pixels,width,height,left,top,positions,labels,patchSize);
        Mat output=new Mat(height,width,opencv_core.CV_8UC1);
        try{output.data().put(rendered);return output;}
        catch(Throwable error){output.close();throw error;}
    }
    @Override public synchronized void close(){closed=true;MifKronosPatchOp.retire(this);}
}
