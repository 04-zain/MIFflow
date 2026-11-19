// Decode portable numerical content once; workflow acceptance belongs to the stage.
import org.mif.inference.MifKronosModelCodec
import org.bytedeco.opencv.opencv_ml.StatModel
import qupath.opencv.ml.FeaturePreprocessor
return [encode:{Map fitted,StatModel model,FeaturePreprocessor preparation,String method,Map convention ->
            MifKronosModelCodec.encode(fitted,model,preparation,method,convention)},
        decode:{byte[] bytes,Map convention -> MifKronosModelCodec.decode(bytes,convention)}]
