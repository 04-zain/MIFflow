package org.mif.segmentation;

import ai.djl.Device;
import ai.djl.inference.Predictor;
import ai.djl.repository.zoo.Criteria;
import ai.djl.translate.Translator;
import java.awt.image.BufferedImage;
import java.io.IOException;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collection;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.ExecutorCompletionService;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.function.Consumer;
import org.bytedeco.opencv.opencv_core.Mat;
import org.locationtech.jts.geom.Envelope;
import qupath.lib.analysis.features.ObjectMeasurements;
import qupath.ext.instanseg.core.InstanSeg;
import qupath.ext.instanseg.core.InstanSegModel;
import qupath.ext.instanseg.core.InstanSegResults;
import qupath.lib.experimental.pixels.OpenCVProcessor;
import qupath.lib.experimental.pixels.OutputHandler;
import qupath.lib.experimental.pixels.Parameters;
import qupath.lib.experimental.pixels.Processor;
import qupath.lib.images.ImageData;
import qupath.lib.images.servers.ColorTransforms;
import qupath.lib.objects.PathObject;
import qupath.lib.objects.utils.ObjectProcessor;
import qupath.lib.objects.utils.Tiler;
import qupath.lib.regions.RegionRequest;
import qupath.opencv.ops.ImageOps;

/** Focused pinned native adapter: identical prediction/pruning/merging, bounded ownership. */
public final class NativeInstanSeg {
    private NativeInstanSeg() {}
    private static Object field(InstanSeg detector,String name)throws ReflectiveOperationException {var field=InstanSeg.class.getDeclaredField(name);field.setAccessible(true);return field.get(detector);}
    private static Method method(String name,Class<?>...parameters)throws ReflectiveOperationException {var method=InstanSeg.class.getDeclaredMethod(name,parameters);method.setAccessible(true);return method;}
    private static Object invoke(Method method,Object target,Object...arguments)throws Exception {try{return method.invoke(target,arguments);}catch(InvocationTargetException wrapped){if(wrapped.getCause() instanceof Exception error)throw error;if(wrapped.getCause() instanceof Error error)throw error;throw wrapped;}}
    private static long statistic(Object processor,String name)throws Exception {var method=processor.getClass().getDeclaredMethod(name);method.setAccessible(true);return ((Number)invoke(method,processor)).longValue();}
    @SuppressWarnings("unchecked")
    /**
     * We run the supplied pinned detector over this accepted parent ROI and image.
     * The native tile plan, channel transforms, translator, masks, postprocessor
     * and shape measurements retain their existing calculations. Requested marker
     * intensities are calculated by the later full-resolution measurement stage. Tile workers
     * share one predictor and follow the admitted CPU allowance; metadata and object byte
     * allowances bound the frontier and each finalized cell. We return
     * native tile/pixel/object counters and stream finalized cells to the consumer.
     * Unsupported extension settings, failed contributors, insufficient admission
     * and cancellation stop the owning publication and release every native owner.
     */
    public static InstanSegResults detect(InstanSeg detector,ImageData<BufferedImage> image,PathObject parent,Path spill,int requestedWorkers,long objectAllowance,long metadataAllowance,Consumer<PathObject> acceptedCells)throws Exception {
        if(javafx.application.Platform.isFxApplicationThread())throw new IllegalStateException("Native segmentation must run on its admitted background owner");
        if(!"0.1.7".equals(InstanSeg.class.getPackage().getImplementationVersion()))throw new IOException("Bounded native segmentation requires the pinned InstanSeg 0.1.7 contract");
        if(Boolean.parseBoolean(System.getProperty("instanseg.debug.tiles","false")))throw new IOException("Debug tile outputs cannot be published as canonical cells");
        if((Boolean)field(detector,"randomColors"))throw new IOException("Canonical native segmentation requires its existing deterministic color setting");
        if(requestedWorkers<1||requestedWorkers>2)throw new IllegalArgumentException("Segmentation supports 1 or 2 simultaneous tiles. Choose one of these values in segmentation settings.");
        int workerBudget=Integer.parseInt(System.getenv().getOrDefault("MIF_COMPUTE_CPU_THREADS",Integer.toString(requestedWorkers)));
        if(requestedWorkers>workerBudget)throw new IllegalArgumentException("The selected "+requestedWorkers+" simultaneous tiles require that many CPU threads; the operation has "+workerBudget+". Reduce simultaneous tiles in segmentation settings or increase the CPU allocation.");
        int workers=requestedWorkers;
        // Tile workers already provide parallelism; nested OpenCV pools remain serial.
        org.bytedeco.opencv.global.opencv_core.setNumThreads(1);
        // i) Reuse the pinned native geometry, sampling and channel-selection plan.
        long started=System.currentTimeMillis();var model=(InstanSegModel)field(detector,"model");int tileDims=(Integer)field(detector,"tileDims"),padding=(Integer)field(detector,"padding");
        double configured=(Double)field(detector,"downsample");double downsample=configured>0?configured:image.getServerMetadata().pixelSizeCalibrated()?model.getPreferredDownsample(image.getServer().getPixelCalibration()):1;
        int core=(int)Math.round(downsample*(tileDims-2d*padding));if(core<=0)throw new IllegalArgumentException("InstanSeg padding leaves no tile interior");
        long estimatedTiles=Math.multiplyExact((long)Math.ceil(parent.getROI().getBoundsWidth()/core)+2,(long)Math.ceil(parent.getROI().getBoundsHeight()/core)+2);
        if(estimatedTiles*1536>metadataAllowance)throw new ExactTileFinalizer.RequiredMemoryException(estimatedTiles*1536,"The native tile plan");
        var tiler=(Tiler)invoke(method("createTiler",Double.TYPE,Integer.TYPE,Integer.TYPE),null,downsample,tileDims,padding);var proxies=tiler.createAnnotations(parent.getROI());
        int pad=(int)Math.round(padding*downsample);var requests=proxies.stream().map(proxy->RegionRequest.createInstance(image.getServer().getPath(),downsample,proxy.getROI()).pad2D(qupath.lib.regions.Padding.symmetric(pad)).intersect2D(0,0,image.getServer().getWidth(),image.getServer().getHeight())).toList();
        var footprints=requests.stream().map(request->{var bounds=new Envelope(request.getX(),(double)request.getX()+request.getWidth(),request.getY(),(double)request.getY()+request.getHeight());bounds.expandBy(Math.max(1,downsample)+0.02);return bounds;}).toList();
        var channels=(List<ColorTransforms.ColorTransform>)invoke(method("getInputChannels",ImageData.class),detector,image);
        boolean[] selectedOutputs=null;int[] configuredOutputs=(int[])field(detector,"outputChannels");if(configuredOutputs!=null&&configuredOutputs.length>0){selectedOutputs=new boolean[model.getOutputChannels().orElseThrow()];for(int channel:configuredOutputs){if(channel<0||channel>=selectedOutputs.length)throw new IllegalArgumentException("Invalid model output channel");selectedOutputs[channel]=true;}}
        var translatorType=Class.forName("qupath.ext.instanseg.core.MatTranslator",true,InstanSeg.class.getClassLoader());var constructor=translatorType.getDeclaredConstructor(String.class,String.class,boolean[].class,Map.class);constructor.setAccessible(true);
        var translator=(Translator<Mat,Mat[]>)constructor.newInstance("CHW","CHW",selectedOutputs,(Map<String,Object>)field(detector,"optionalArgs"));
        var predictors=new ArrayBlockingQueue<Predictor<Mat,Mat[]>>(1);
        // ii) Admit a bounded prediction queue and measure each finalized shape once.
        // Completion may be visible before its worker releases the executor slot.
        // The submission loop still admits at most workers + 2 unfinished tiles;
        // allow that many queued wrappers during this brief handoff.
        var pool=new ThreadPoolExecutor(workers,workers,0,TimeUnit.MILLISECONDS,new ArrayBlockingQueue<>(workers + 2),task->{var thread=new Thread(task,"mif-instanseg-tile");thread.setDaemon(true);return thread;},new ThreadPoolExecutor.AbortPolicy());
        var calibration=image.getServer().getPixelCalibration();var shapeFeatures=ObjectMeasurements.ShapeFeatures.values();
        Consumer<PathObject> sink=cell->{long bytes=cell.getROI().getNumPoints()*128L+2048;if(bytes>objectAllowance/4)throw new IllegalArgumentException("One finalized native cell exceeds its output allowance");ObjectMeasurements.addShapeMeasurements(cell,calibration,shapeFeatures);acceptedCells.accept(cell);};
        ai.djl.repository.zoo.ZooModel<Mat,Mat[]> loaded=null;
        Predictor<Mat,Mat[]> predictor=null;
        try {
            loaded=Criteria.builder().setTypes(Mat.class,Mat[].class).optModelUrls(model.getPath().orElseThrow().resolve("instanseg.pt").toUri().toString()).optDevice((Device)field(detector,"device")).optTranslator(translator).build().loadModel();
            try(var finalizer=new ExactTileFinalizer(footprints,spill,objectAllowance*3/4,Math.max(1,metadataAllowance-proxies.size()*1024L), (ObjectProcessor)invoke(method("createPostProcessor",Boolean.TYPE),null,false),sink)) {
            predictor=loaded.newPredictor();predictors.add(predictor);
            var processor=(Processor<Mat,Mat,Mat[]>)invoke(method("createProcessor",java.util.concurrent.BlockingQueue.class,Collection.class,Integer.TYPE,Boolean.TYPE),null,predictors,channels,tileDims,Boolean.parseBoolean(System.getProperty("instanseg.padToInputSize","false")));
            var output=(OutputHandler<Mat,Mat,Mat[]>)invoke(method("createOutputHandler",Class.class,Boolean.TYPE,Integer.TYPE,List.class),null,field(detector,"preferredOutputType"),false,1,model.getOutputs().orElseThrow());
            // iii) Finalize only successful dependency-complete units; failed tiles remain incomplete.
            var completion=new ExecutorCompletionService<Void>(pool);int submitted=0,finished=0;
            while(finished<proxies.size()) {
                while(submitted<proxies.size()&&submitted-finished<workers+2) {
                    int tile=submitted++;completion.submit(()->{
                        Mat[] input={null},mask={null};Mat[] result=null;
                        var parameters=Parameters.<Mat,Mat>builder().imageData(image).region(requests.get(tile)).parent(parent).parentProxy(proxies.get(tile))
                            .imageFunction(params->{input[0]=ImageOps.buildImageDataOp(channels).apply(params.getImageData(),params.getRegionRequest());return input[0];})
                            .maskFunction((params,roi)->{mask[0]=OpenCVProcessor.createMatMaskSupplier().getMask(params,roi);return mask[0];}).build();
                        try {
                            List<PathObject> cells;
                            try {
                                result = processor.process(parameters);
                                if (result == null || !output.handleOutput(parameters, result))
                                    throw new IOException("Native prediction did not complete tile " + tile);
                                cells = new ArrayList<>(proxies.get(tile).getChildObjects());
                                proxies.get(tile).clearChildObjects();
                            } finally {
                                // The pinned converter has copied contours and scalar outputs
                                // into QuPath objects. Release pixel matrices before waiting for
                                // overlap finalization, shape measurements or the table writer.
                                if (result != null) for (var mat : result) if (mat != null) mat.close();
                                if (mask[0] != null) mask[0].close();
                                if (input[0] != null) input[0].close();
                            }
                            finalizer.completeTile(tile, cells);
                            return null;
                        } catch (Exception error) {
                            finalizer.failTile(tile);
                            throw error;
                        }
                    });
                }
                completion.take().get();finished++;
            }
            finalizer.finish();long failed=statistic(processor,"getTilesFailedCount");if(failed!=0)throw new IOException("Native segmentation reported failed tile contributors");
            return new InstanSegResults(statistic(processor,"getPixelsProcessedCount"),(int)statistic(processor,"getTilesProcessedCount"),(int)failed,Math.toIntExact(finalizer.getFinalCount()),System.currentTimeMillis()-started,false);
            }
        } finally {
            pool.shutdownNow();boolean interrupted=Thread.interrupted();while(!pool.isTerminated())try{pool.awaitTermination(100,TimeUnit.MILLISECONDS);}catch(InterruptedException error){interrupted=true;}
            if(predictor!=null)predictor.close();if(loaded!=null)loaded.close();if(interrupted)Thread.currentThread().interrupt();
        }
    }
}
