// Bind the current image and cached-vector reader to the compiled prediction operation.
import java.util.function.BiFunction
import qupath.lib.images.ImageData
import org.locationtech.jts.geom.Geometry
import org.mif.inference.MifKronosPatchProvider
import org.mif.inference.MifKronosPatchOp
return [provider:{ImageData image,Geometry scope,double ds,Closure reader,Map model,int classes,int patchSize ->
            new MifKronosPatchProvider(image,scope,ds,reader as BiFunction<Integer,Integer,float[]>,model,classes,patchSize)},
        operation:{String revision,MifKronosPatchProvider provider -> MifKronosPatchOp.create(revision,provider)}]
