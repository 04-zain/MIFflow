package org.mif.inference;

import java.io.IOException;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.function.Function;
import org.bytedeco.javacpp.indexer.FloatIndexer;
import org.bytedeco.javacpp.indexer.IntIndexer;
import org.bytedeco.opencv.global.opencv_core;
import org.bytedeco.opencv.global.opencv_ml;
import org.bytedeco.opencv.opencv_core.Mat;
import org.bytedeco.opencv.opencv_ml.TrainData;
import org.bytedeco.opencv.opencv_ml.StatModel;
import org.bytedeco.javacpp.PointerScope;
import org.mif.classification.FeaturePreparation;
import qupath.lib.classifiers.Normalization;
import qupath.opencv.ml.FeaturePreprocessor;
import qupath.opencv.ml.OpenCVClassifiers;

/** Owns a fitted native head; providers borrow its wrapper and preparation. */
public final class MifKronosNativeFit implements AutoCloseable {
    private final StatModel model;
    private final OpenCVClassifiers.OpenCVStatModel wrapper;
    private final FeaturePreprocessor preprocessor;
    private final String method;
    private final PointerScope allocations;
    private boolean closed;
    public MifKronosNativeFit(StatModel model,FeaturePreprocessor preprocessor,String method) {
        this(model,preprocessor,method,null);
    }
    MifKronosNativeFit(StatModel model,FeaturePreprocessor preprocessor,String method,PointerScope allocations) {
        this.model=model;this.wrapper=OpenCVClassifiers.wrapStatModel(model);
        this.preprocessor=preprocessor;this.method=method;this.allocations=allocations;
        // A fitted head may be handed out from an enclosing native tile scope.
        if(allocations==null)model.retainReference();
    }
    public StatModel getModel(){return model;}
    public OpenCVClassifiers.OpenCVStatModel getWrapper(){return wrapper;}
    public FeaturePreprocessor getPreprocessor(){return preprocessor;}
    public String getMethod(){return method;}

    /** One admitted graph-feature row in the caller's established order. */
    public record TrainingRow(MifKronosPatchProvider provider,int row,int column,int target) {}

    /** Owns temporary training matrices; fitted preparation may outlive these rows. */
    public static final class PreparedRows implements AutoCloseable {
        private final Mat samples;
        private final Mat responses;
        private final TrainData training;
        private final FeaturePreprocessor preprocessor;
        private boolean closed;
        private PreparedRows(Mat samples,Mat responses,TrainData training,FeaturePreprocessor preprocessor) {
            this.samples=samples;this.responses=responses;this.training=training;this.preprocessor=preprocessor;
        }
        public TrainData getTrainData(){return training;}
        public FeaturePreprocessor getPreprocessor(){return preprocessor;}
        @Override public synchronized void close(){
            if(closed)return;closed=true;
            try{training.close();}finally{try{responses.close();}finally{samples.close();}}
        }
    }

    /** Assemble labelled rows across existing providers and fit their normalization.
     * Providers remain caller-owned; all temporary matrices close on failure.
     */
    public static PreparedRows prepareRows(List<TrainingRow> rows,int width,Normalization normalization)throws IOException {
        Mat samples=null,responses=null;
        TrainData training=null;
        try {
            samples=new Mat(rows.size(),width,opencv_core.CV_32FC1);
            responses=new Mat(rows.size(),1,opencv_core.CV_32SC1);
            try(FloatIndexer sx=samples.createIndexer();IntIndexer ty=responses.createIndexer()) {
                for(int i=0;i<rows.size();i++) {
                    TrainingRow row=rows.get(i);
                    float[] values=row.provider().featuresAt(row.row(),row.column());
                    for(int column=0;column<width;column++)sx.put(i,column,values[column]);
                    ty.put(i,row.target());
                }
            }
            FeaturePreprocessor preparation=FeaturePreparation.pixel(samples,
                    new FeaturePreparation.Settings(normalization,0d,false),0d);
            training=TrainData.create(samples,opencv_ml.ROW_SAMPLE,responses);
            return new PreparedRows(samples,responses,training,preparation);
        }catch(IOException | RuntimeException | Error error) {
            if(training!=null)training.close();
            if(responses!=null)responses.close();
            if(samples!=null)samples.close();
            throw error;
        }
    }

    /** Fit the preview head with the same numerical row preparation as final fitting. */
    public static MifKronosNativeFit fit(MifKronosPatchProvider provider,List<String> ids,
            Map<String,String> labels,List<String> classes,int width,Normalization normalization,
            String method,Function<TrainData,StatModel> trainer)throws IOException {
        List<TrainingRow> rows=new ArrayList<>(ids.size());
        for(String id:ids) {
            String[] position=id.split(":");
            rows.add(new TrainingRow(provider,Integer.parseInt(position[0]),Integer.parseInt(position[1]),
                    classes.indexOf(labels.get(id))));
        }
        StatModel model=null;
        try(PreparedRows prepared=prepareRows(rows,width,normalization)) {
            model=trainer.apply(prepared.getTrainData());
            return new MifKronosNativeFit(model,prepared.getPreprocessor(),method);
        }catch(IOException | RuntimeException | Error error) {
            if(model!=null)model.close();
            throw error;
        }
    }
    // Decoded PCA/model allocations belong to this fit, not the first tile.
    @Override public synchronized void close(){
        if(closed)return;closed=true;
        if(allocations==null)model.close();else allocations.deallocate();
    }
}
