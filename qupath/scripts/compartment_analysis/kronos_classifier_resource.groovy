// The authenticated stage owner assembles worker/backends; Java owns deployment reuse and raster calculations.
import java.nio.charset.StandardCharsets
import qupath.lib.images.ImageData
import org.mif.inference.MifKronosModelCodec
import org.mif.inference.MifKronosResourceOp
import org.mif.inference.MifKronosResourceRuntime
import org.mif.inference.MifKronosResourceContext

return [configure:{Object geometry,Map operations,Map workers,Map backend,Map codec ->
    // Initialize subtype registration before reopening a saved classifier.
    Class.forName(MifKronosResourceOp.name,true,MifKronosResourceOp.classLoader)
    MifKronosResourceRuntime.configure({ImageData image,MifKronosResourceOp operation,List<Map> scopes ->
        List<Integer> outputCodes=MifKronosResourceContext.outputCodes(operation.outputCodes)
        byte[] modelBytes=operation.modelJson.getBytes(StandardCharsets.UTF_8)
        if(MifKronosModelCodec.digest(modelBytes)!=operation.modelSha256)
            throw new IOException("The serialized KRONOS model content changed")
        def channels=image.server.metadata.channels
        List rows=operation.channelFeatures.ordered_channels
        for(Map row:rows) {
            if(!(row.qupath_channel_index instanceof Number) || row.qupath_channel_index<0 || row.qupath_channel_index>=channels.size() ||
                channels[row.qupath_channel_index as int].name!=row.observed_qupath_channel_name)
                throw new IOException("The current image channel order differs from the classifier input")
        }
        double px=image.server.pixelCalibration.pixelWidthMicrons,py=image.server.pixelCalibration.pixelHeightMicrons
        double requestedX=operation.convention.pixel_size_um[0] as double,requestedY=operation.convention.pixel_size_um[1] as double
        if(!Double.isFinite(px) || !Double.isFinite(py) || px<=0d || py<=0d)
            throw new IOException("The processed image requires explicit physical pixel calibration")
        double downsample=requestedX/px
        if(downsample<1d || !Double.isFinite(downsample) || Math.abs(requestedY/py-downsample)>1e-9)
            throw new IOException("The image cannot supply the recorded physical sampling grid")
        Object opened=workers.createTemporary(operation.workerSettings,"mif-kronos-deployment-")
        Object nativeFit=null
        List<MifKronosResourceContext.ProviderScope> providers=[]
        try {
            if(opened.ready.panel_sha256!=operation.convention.panel_sha256)
                throw new IOException("The current encoder input convention differs from the saved classifier")
            Map restored=codec.decode(modelBytes,operation.convention)
            nativeFit=restored.native_fit
            if(restored.fitted.class_order.size()!=outputCodes.size())throw new IOException("Classifier output labels differ from the graph")
            for(Map scope:scopes) {
                Map identity=[patient_id:"current-image",image_id:image.server.path,entry_id:"current-entry",split:scope.split,
                    fold_id:"deployment",domain_id:scope.id,domain_revision:scope.revision,tissue_component_id:scope.id,eligible_parent_id:scope.id]
                def current=backend.backend(image,scope.geometry,downsample,
                    rows.collect{(it.qupath_channel_index as Number).intValue()},identity,geometry,operations,opened)
                try {providers.add(new MifKronosResourceContext.ProviderScope(current,current.provider(restored.fitted,nativeFit)))}
                catch(Throwable error){current.close();throw error}
            }
            return new MifKronosResourceContext(image,providers,outputCodes,nativeFit,opened)
        } catch(Throwable error) {
            providers.each{try{it.provider().close()}catch(Throwable closeError){error.addSuppressed(closeError)}
                try{it.backend().close()}catch(Throwable closeError){error.addSuppressed(closeError)}}
            try{nativeFit?.close()}catch(Throwable closeError){error.addSuppressed(closeError)}
            try{opened.close()}catch(Throwable closeError){error.addSuppressed(closeError)}
            throw error
        }
    } as MifKronosResourceRuntime.ContextFactory)
},
    bind:{ImageData image,List scopes -> MifKronosResourceRuntime.bind(image,scopes)},
    closeImage:{ImageData image -> MifKronosResourceRuntime.closeImage(image)},
    describe:{Object classifier -> MifKronosResourceOp.describe(classifier)},
    create:{byte[] bytes,Map convention,Map worker,Map channels,List codes,Map labels,Map reviewPolicy=null ->
        MifKronosResourceOp.create(bytes,convention,worker,channels,codes,labels,reviewPolicy)}]
