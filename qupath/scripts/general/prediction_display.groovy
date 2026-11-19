// Display-only indexed colours. Class values, learned models and accepted regions are unchanged.
import java.awt.image.IndexColorModel
import java.awt.image.DataBuffer
import qupath.lib.classifiers.pixel.PixelClassificationImageServer
import qupath.lib.gui.images.stores.ColorModelRenderer
import qupath.lib.gui.viewer.OverlayOptions
import qupath.lib.gui.viewer.RegionFilter
import qupath.lib.gui.viewer.overlays.PixelClassificationOverlay
import qupath.lib.objects.classes.PathClass
import qupath.lib.objects.classes.PathClassTools
import qupath.lib.images.servers.ImageServerMetadata.ChannelType

class MifPredictionDisplay {
    static palette(metadata) {
        Map labels=metadata.classificationLabels
        if(metadata.outputType!=ChannelType.CLASSIFICATION || labels.isEmpty() || labels.keySet().any {it<0 || it>255} ||
           (metadata.outputPixelType!=null && metadata.outputPixelType!=qupath.lib.images.servers.PixelType.UINT8))return null
        int[] colours=new int[256]
        labels.each {index,label ->
            if(label!=null && label!=PathClass.NULL_CLASS && !PathClassTools.isIgnoredClass(label)) {
                int color=label.color==null ? 0xdddddd : label.color
                int r=(color>>16)&255,g=(color>>8)&255,b=color&255
                int maximum=Math.max(r,Math.max(g,b))
                if(maximum==0){r=220;
                    g=220;
                    b=220}
                else if(maximum<220){double scale=220d/maximum;
                    r=(int)Math.round(r*scale);
                    g=(int)Math.round(g*scale);
                    b=(int)Math.round(b*scale)}
                colours[index as int]=(0xff000000L | (r<<16) | (g<<8) | b) as int
            }
        }
        return new IndexColorModel(8,256,colours,0,true,-1,DataBuffer.TYPE_BYTE)
    }

    static server(data,classifier) {
        def colours=palette(classifier.metadata)
        return colours==null ? null : new MifDisplayPredictionServer(data,classifier,colours)
    }

    static create(options,data,classifier,boolean everywhere=false) {
        def local=new OverlayOptions(options)
        local.showPixelClassification=true
        if(everywhere)local.pixelClassificationRegionFilter=RegionFilter.StandardRegionFilters.EVERYWHERE
        def server=server(data,classifier)
        if(server==null)return PixelClassificationOverlay.create(local,classifier,1)
        def palette=palette(classifier.metadata)
        def overlay=PixelClassificationOverlay.create(local,[(data):server],new ColorModelRenderer(palette))
        overlay.setMaxThreads(1)
        return overlay
    }
}
// Marker subclass uses the native implementation without overriding prediction.
class MifDisplayPredictionServer extends PixelClassificationImageServer {
    MifDisplayPredictionServer(data,classifier,colours) {
        super(data,classifier,"mif-preview-"+UUID.randomUUID(),colours)
    }
}
return [create:MifPredictionDisplay.&create,palette:MifPredictionDisplay.&palette,server:MifPredictionDisplay.&server]
