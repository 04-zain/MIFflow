package org.mif.inference;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.ToNumberPolicy;
import java.lang.reflect.Array;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.Collection;
import java.util.HashSet;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.bytedeco.opencv.opencv_ml.StatModel;
import org.bytedeco.javacpp.PointerScope;
import org.bytedeco.opencv.opencv_ml.RTrees;
import org.bytedeco.opencv.opencv_ml.SVM;
import qupath.lib.io.GsonTools;
import qupath.opencv.ml.FeaturePreprocessor;

/** Portable numerical content; workflow acceptance remains with the stage owner. */
public final class MifKronosModelCodec {
    private static final int MAX_BYTES=64*1024*1024;
    private static final Gson JSON=new GsonBuilder().disableHtmlEscaping().serializeNulls()
        .setObjectToNumberStrategy(ToNumberPolicy.LONG_OR_DOUBLE).create();
    private static final Set<String> FIELDS=Set.of("schema","convention","class_order","graph_parameters","training_node_ids",
        "label_assignments_sha256","loss_history","method","native_model","native_preprocessor");
    private static final Set<String> CONVENTION_FIELDS=Set.of("package_sha256","panel_sha256","marker_names","intensity_divisors",
        "pixel_size_um","nuclear_channel","patch_size","feature_width","graph_settings");
    private static final List<String> PARAMETER_NAMES=List.of("self_weight","neighbour_weight","bias","head_weight","head_bias");
    private MifKronosModelCodec() {}

    public static String digest(byte[] bytes) {
        try{return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes));}
        catch(NoSuchAlgorithmException error){throw new IllegalStateException(error);}
    }
    private static boolean sha(Object value){return value instanceof String text && text.matches("[0-9a-f]{64}");}
    private static boolean number(Object value,double expected){return value instanceof Number n && n.doubleValue()==expected;}
    private static boolean positive(Object value){return value instanceof Number n && n.doubleValue()>0;}
    private static void finite(Object value) {
        if(value instanceof Number n) {
            if(!Double.isFinite(n.doubleValue()))throw new IllegalArgumentException("The model contains a nonfinite value");
        }else if(value instanceof Map<?,?> map)map.values().forEach(MifKronosModelCodec::finite);
        else if(value instanceof Collection<?> collection)collection.forEach(MifKronosModelCodec::finite);
        else if(value!=null && value.getClass().isArray())for(int i=0;i<Array.getLength(value);i++)finite(Array.get(value,i));
    }
    private static void convention(Map<?,?> value) {
        if(value==null || !value.keySet().equals(CONVENTION_FIELDS)
                || !(number(value.get("patch_size"),32) || number(value.get("patch_size"),64)
                    || number(value.get("patch_size"),128) || number(value.get("patch_size"),256)) || !number(value.get("feature_width"),768)
                || !sha(value.get("package_sha256")) || !sha(value.get("panel_sha256")))
            throw new IllegalArgumentException("The exact encoder, panel and patch-grid convention is required");
        if(!(value.get("marker_names") instanceof List<?> markers) || markers.isEmpty() || markers.size()>32
                || markers.stream().anyMatch(v->!(v instanceof String s) || s.trim().isEmpty())
                || !(value.get("intensity_divisors") instanceof List<?> divisors) || divisors.size()!=markers.size()
                || divisors.stream().anyMatch(v->!positive(v))
                || !(value.get("pixel_size_um") instanceof List<?> pixels) || pixels.size()!=2
                || pixels.stream().anyMatch(v->!positive(v)))
            throw new IllegalArgumentException("The stored image-input convention is incomplete");
        finite(value);
    }
    /** Serialize a borrowed head; the training caller keeps its native ownership. */
    public static byte[] encode(Map<String,Object> fitted,StatModel model,FeaturePreprocessor preparation,String method,Map<String,Object> inputConvention) {
        convention(inputConvention);
        if(model==null || !List.of("rf","svm").contains(method) || !model.isTrained())
            throw new IllegalArgumentException("Only a completed native RF or SVM model can be encoded");
        Map<String,Object> body=new LinkedHashMap<>();
        body.put("schema","kronos-native-patch-model/v1");body.put("convention",inputConvention);
        body.put("class_order",fitted.get("class_order"));body.put("graph_parameters",fitted.get("parameters"));
        body.put("training_node_ids",fitted.get("training_ids"));body.put("label_assignments_sha256",fitted.get("label_sha256"));
        body.put("loss_history",fitted.get("loss_history"));body.put("method",method);
        body.put("native_model",GsonTools.getInstance().toJson(model,StatModel.class));
        body.put("native_preprocessor",GsonTools.getInstance().toJson(preparation,FeaturePreprocessor.class));
        finite(body);
        byte[] bytes=JSON.toJson(body).getBytes(StandardCharsets.UTF_8);
        if(bytes.length>MAX_BYTES)throw new IllegalArgumentException("The model exceeds the supported artifact size");
        return bytes;
    }

    /** Validate one external model and transfer its native allocation to the fit. */
    @SuppressWarnings("unchecked")
    public static Map<String,Object> decode(byte[] bytes,Map<String,Object> expectedConvention) {
        convention(expectedConvention);
        if(bytes==null || bytes.length==0 || bytes.length>MAX_BYTES)
            throw new IllegalArgumentException("The model exceeds the supported artifact size");
        Map<String,Object> body=JSON.fromJson(new String(bytes,StandardCharsets.UTF_8),Map.class);
        if(body==null || !body.keySet().equals(FIELDS) || !"kronos-native-patch-model/v1".equals(body.get("schema")))
            throw new IllegalArgumentException("The stored native patch model has an unsupported schema");
        finite(body);
        Map<String,Object> storedConvention=(Map<String,Object>)body.get("convention");convention(storedConvention);
        // JSON numbers preserve scientific values irrespective of boxed Integer/Long types.
        if(!JSON.toJsonTree(storedConvention).equals(JSON.toJsonTree(expectedConvention)))
            throw new IllegalArgumentException("The stored encoder/panel/grid convention differs from the requested convention");
        if(!(body.get("class_order") instanceof List<?> classes) || classes.size()<2 || classes.size()>128
                || new HashSet<>(classes).size()!=classes.size()
                || classes.stream().anyMatch(v->!(v instanceof String s) || s.trim().isEmpty()))
            throw new IllegalArgumentException("The stored class order is invalid");
        if(!(body.get("training_node_ids") instanceof List<?> ids) || ids.isEmpty() || ids.size()>4096
                || new HashSet<>(ids).size()!=ids.size() || !sha(body.get("label_assignments_sha256")))
            throw new IllegalArgumentException("The stored patch-label attribution is invalid");
        if(!(body.get("graph_parameters") instanceof Map<?,?> learned) || !learned.keySet().equals(Set.copyOf(PARAMETER_NAMES)))
            throw new IllegalArgumentException("The frozen graph/head parameters are incomplete");
        int hidden=((Number)((Map<?,?>)expectedConvention.get("graph_settings")).get("hidden_width")).intValue();
        if(hidden<1 || hidden>768)throw new IllegalArgumentException("The learned feature width is invalid");
        Map<String,Object> parameters=new LinkedHashMap<>();
        parameters.put("self_weight",matrix(learned.get("self_weight"),768,hidden));
        parameters.put("neighbour_weight",matrix(learned.get("neighbour_weight"),768,hidden));
        parameters.put("bias",vector(learned.get("bias"),hidden));
        parameters.put("head_weight",matrix(learned.get("head_weight"),hidden,classes.size()));
        parameters.put("head_bias",vector(learned.get("head_bias"),classes.size()));
        finite(parameters);
        StatModel model=null;
        // The QuPath tile caller has its own PointerScope. Keep all decoded
        // native model/PCA allocations until the deployment fit is closed.
        try(PointerScope allocations=new PointerScope()) {
          try {
            model=GsonTools.getInstance().fromJson((String)body.get("native_model"),StatModel.class);
            String method=(String)body.get("method");
            if(model==null || !model.isTrained() || model.getVarCount()!=hidden
                    || !("rf".equals(method) && model instanceof RTrees || "svm".equals(method) && model instanceof SVM))
                throw new IllegalArgumentException("The restored native classifier type/feature width is invalid");
            FeaturePreprocessor preparation=GsonTools.getInstance().fromJson((String)body.get("native_preprocessor"),FeaturePreprocessor.class);
            if(preparation==null)throw new IllegalArgumentException("The native feature preprocessor is absent");
            var fit=new MifKronosNativeFit(model,preparation,method,allocations);
            Map<String,Object> fitted=new LinkedHashMap<>();
            fitted.put("parameters",parameters);fitted.put("class_order",classes);fitted.put("training_ids",ids);
            fitted.put("label_sha256",body.get("label_assignments_sha256"));fitted.put("loss_history",body.get("loss_history"));
            Map<String,Object> result=new LinkedHashMap<>();
            result.put("fitted",fitted);result.put("native_fit",fit);result.put("convention",storedConvention);result.put("model_sha256",digest(bytes));
            allocations.extend();model=null;return result;
          }finally{if(model!=null)model.close();}
        }
    }
    private static float[] vector(Object value,int length) {
        if(!(value instanceof List<?> values) || values.size()!=length)
            throw new IllegalArgumentException("The stored graph parameter dimensions disagree");
        float[] result=new float[length];
        for(int i=0;i<length;i++)result[i]=((Number)values.get(i)).floatValue();
        return result;
    }
    private static float[][] matrix(Object value,int rows,int cols) {
        if(!(value instanceof List<?> values) || values.size()!=rows)
            throw new IllegalArgumentException("The stored graph parameter dimensions disagree");
        float[][] result=new float[rows][];
        for(int row=0;row<rows;row++)result[row]=vector(values.get(row),cols);
        return result;
    }
}
