package org.mif.cells;

import java.awt.Graphics2D;
import java.awt.Color;
import java.awt.Font;
import java.awt.RenderingHints;
import com.google.gson.JsonObject;
import javax.imageio.IIOImage;
import javax.imageio.ImageTypeSpecifier;
import javax.imageio.metadata.IIOMetadataNode;
import qupath.lib.io.GsonTools;
import java.awt.image.BufferedImage;
import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.util.Objects;
import java.util.ArrayList;
import java.util.Set;
import java.util.UUID;
import java.util.stream.Collectors;
import javafx.application.Platform;
import javax.imageio.ImageIO;
import org.locationtech.jts.geom.Envelope;
import qupath.fx.dialogs.Dialogs;
import qupath.fx.dialogs.FileChoosers;
import qupath.lib.display.settings.DisplaySettingUtils;
import qupath.lib.display.settings.ImageDisplaySettings;
import qupath.lib.gui.QuPathGUI;
import javafx.scene.control.*;
import javafx.scene.layout.VBox;
import javafx.geometry.Insets;
import qupath.lib.gui.images.servers.RenderedImageServer;
import qupath.lib.gui.images.stores.DefaultImageRegionStore;
import qupath.lib.gui.viewer.OverlayOptions;
import qupath.lib.gui.viewer.QuPathViewer;
import qupath.lib.gui.viewer.overlays.HierarchyOverlay;
import qupath.lib.images.ImageData;
import qupath.lib.objects.PathCellObject;
import qupath.lib.regions.ImageRegion;
import qupath.lib.regions.RegionRequest;

/** Export a bounded rendered image with complete saved cells on a worker. */
public final class CellFigureExport {
    private static final String TITLE="Export image";
    private static final String CURRENT="Current view",SELECTED="Selected annotation bounds",WHOLE="Whole image";
    private static final long MAX_PIXELS=16_000_000;
    private CellFigureExport() {}

    public record Request(ImageData<BufferedImage> image,ImageRegion region,double downsample,
            ImageDisplaySettings display,OverlayOptions options,DefaultImageRegionStore store,
            Object binding,Object components,Object edits,boolean showScaleBar,String metadata) {}

    record ScaleBar(double length,String unit,double outputPixels,int x,int y,int width,int height) {}

    /** Capture existing view choices only; exporting does not change the analysis. */
    public static Request capture(QuPathViewer viewer) {
        return capture(viewer,CURRENT,viewer==null?1:viewer.getDownsampleFactor());
    }

    public static Request capture(QuPathViewer viewer,String scope,double downsample) {
        return capture(viewer,scope,downsample,true);
    }

    public static Request capture(QuPathViewer viewer,String scope,double downsample,boolean showScaleBar) {
        if(!Platform.isFxApplicationThread())throw new IllegalStateException("Capture export settings on the interface thread");
        if(viewer==null||viewer.getImageData()==null)throw new IllegalArgumentException("Open an image before exporting its view");
        var image=viewer.getImageData();
        java.awt.geom.Rectangle2D bounds;
        if(CURRENT.equals(scope)) {
            if(Math.abs(viewer.getRotation())>1e-9)throw new IllegalArgumentException("Reset the viewer rotation before exporting the current image rectangle");
            bounds=viewer.getDisplayedRegionShape().getBounds2D();
        }else if(WHOLE.equals(scope))bounds=new java.awt.geom.Rectangle2D.Double(0,0,image.getServer().getWidth(),image.getServer().getHeight());
        else if(SELECTED.equals(scope)) {
            var selected=image.getHierarchy().getSelectionModel().getSelectedObject();
            if(selected==null||!selected.isAnnotation()||!selected.hasROI())throw new IllegalArgumentException("Select an annotation before exporting its bounds");
            var roi=selected.getROI();
            if(roi.getZ()!=viewer.getZPosition()||roi.getT()!=viewer.getTPosition())throw new IllegalArgumentException("The selected annotation must be on the displayed image plane");
            bounds=new java.awt.geom.Rectangle2D.Double(roi.getBoundsX(),roi.getBoundsY(),roi.getBoundsWidth(),roi.getBoundsHeight());
        }else throw new IllegalArgumentException("Unknown export scope: "+scope);
        int x=Math.max(0,(int)Math.floor(bounds.getMinX())),y=Math.max(0,(int)Math.floor(bounds.getMinY()));
        int right=Math.min(image.getServer().getWidth(),(int)Math.ceil(bounds.getMaxX()));
        int bottom=Math.min(image.getServer().getHeight(),(int)Math.ceil(bounds.getMaxY()));
        if(right<=x||bottom<=y)throw new IllegalArgumentException("The selected region does not intersect the image");
        return new Request(image,ImageRegion.createInstance(x,y,right-x,bottom-y,viewer.getZPosition(),viewer.getTPosition()),
                downsample,DisplaySettingUtils.displayToSettings(viewer.getImageDisplay(),"Current view"),
                new OverlayOptions(viewer.getOverlayOptions()),viewer.getImageRegionStore(),
                image.getProperty(CellDatasetProvider.DATASET_PROPERTY),image.getProperty(CellComponents.PROPERTY),
                image.getProperty(CellDatasetProvider.EDITS_PROPERTY),showScaleBar,captureMetadata(viewer));
    }

    public static void prompt(QuPathGUI qupath) {
        try {
            var viewer=qupath.getViewer();
            if(viewer==null||viewer.getImageData()==null)throw new IllegalArgumentException("Open an image before exporting");
            var request=exportDialog(qupath,viewer).showAndWait().orElse(null);
            if(request==null)return;
            var file=FileChoosers.promptToSaveFile(TITLE,null,FileChoosers.createExtensionFilter("PNG image","*.png"));
            if(file==null)return;
            Path destination=file.toPath();
            if(!destination.getFileName().toString().toLowerCase(java.util.Locale.ROOT).endsWith(".png"))destination=destination.resolveSibling(destination.getFileName()+".png");
            if(Files.exists(destination)&&!Dialogs.showYesNoDialog(TITLE,"Replace "+destination.getFileName()+"?"))return;
            final Path output=destination;
            qupath.getThreadPoolManager().getSingleThreadExecutor(CellFigureExport.class).submit(()->{
                try {write(request,output);Platform.runLater(()->Dialogs.showInfoNotification(TITLE,"Saved "+output));}
                catch(Exception error){Platform.runLater(()->Dialogs.showErrorMessage(TITLE,error));}
            });
        }catch(Exception error){Dialogs.showErrorMessage(TITLE,error);}
    }

    /** Keep invalid settings editable until a valid request is explicitly exported. */
    private static Dialog<Request> exportDialog(QuPathGUI qupath,QuPathViewer viewer)throws IOException {
        var dialog=new Dialog<Request>();dialog.setTitle(TITLE);dialog.initOwner(qupath.getStage());
        var pane=dialog.getDialogPane();pane.setPrefWidth(480);
        var exportType=new ButtonType("Export",ButtonBar.ButtonData.OK_DONE);
        pane.getButtonTypes().addAll(ButtonType.CANCEL,exportType);
        var export=(Button)pane.lookupButton(exportType);export.setId("figure-export");
        export.getStyleClass().add("export-primary");
        pane.lookupButton(ButtonType.CANCEL).setId("figure-export-cancel");
        var scopes=new ArrayList<String>();scopes.add(CURRENT);
        var selected=viewer.getImageData().getHierarchy().getSelectionModel().getSelectedObject();
        if(selected!=null&&selected.isAnnotation()&&selected.hasROI())scopes.add(SELECTED);
        scopes.add(WHOLE);
        var scope=new ComboBox<String>();scope.getItems().setAll(scopes);scope.setValue(CURRENT);
        scope.setId("figure-export-scope");scope.setMaxWidth(Double.MAX_VALUE);
        var downsample=new TextField(Double.toString(viewer.getDownsampleFactor()));
        downsample.setId("figure-export-downsample");
        var scaleBar=new CheckBox("Show scale bar");scaleBar.setSelected(true);scaleBar.setId("figure-export-scale-bar");
        var title=exportLabel("Export image","export-title");
        var intro=exportLabel("Save the displayed channels and overlays as a PNG.","export-help");
        var regionCaption=exportLabel("Region","export-caption");regionCaption.setLabelFor(scope);
        var sampleCaption=exportLabel("Downsample","export-caption");sampleCaption.setLabelFor(downsample);
        var guidance=exportLabel("Source pixels per output pixel. Use 1 for full resolution.","export-help");
        var section=exportLabel("Output","export-section");
        var dimensions=exportLabel("","export-dimensions");dimensions.setId("figure-export-dimensions");
        dimensions.visibleProperty().bind(dimensions.textProperty().isNotEmpty());dimensions.managedProperty().bind(dimensions.visibleProperty());
        var limit=exportLabel("PNG output is limited to 16 million pixels.","export-help");
        var annotation=exportLabel("Annotation exports include the bounding rectangle.","export-help");
        annotation.visibleProperty().bind(scope.valueProperty().isEqualTo(SELECTED));
        annotation.managedProperty().bind(annotation.visibleProperty());
        var error=exportLabel("","export-error");error.setId("figure-export-error");
        error.visibleProperty().bind(error.textProperty().isNotEmpty());error.managedProperty().bind(error.visibleProperty());
        var header=new VBox(5,title,intro);header.setPadding(new Insets(20,22,18,22));
        var fields=new VBox(16,new VBox(7,regionCaption,scope),new VBox(7,sampleCaption,downsample,guidance),scaleBar);
        var output=new VBox(7,section,dimensions,limit,annotation);output.getStyleClass().add("export-output");
        var body=new VBox(18,fields,output,error);body.setPadding(new Insets(0,22,20,22));
        pane.setContent(new VBox(header,body));
        // The packaged 600 face has a separate JavaFX legacy family; requesting
        // weight 600 on Ubuntu Sans itself can silently choose the Regular face.
        javafx.scene.text.Font packagedSemibold=null;
        for(String weight:new String[]{"Regular","SemiBold"}) {
            try(var stream=CellFigureExport.class.getResourceAsStream("/org/mif/fonts/ubuntu-sans/UbuntuSans-"+weight+".ttf")) {
                if(stream!=null) {
                    var loaded=javafx.scene.text.Font.loadFont(stream,15);
                    if("SemiBold".equals(weight))packagedSemibold=loaded;
                }
            }
        }
        var families=javafx.scene.text.Font.getFamilies();
        var preferences=System.getProperty("os.name","").startsWith("Windows")?
                java.util.List.of("Segoe UI","Ubuntu Sans","DejaVu Sans"):java.util.List.of("Ubuntu Sans","Ubuntu","DejaVu Sans");
        String family=preferences.stream().filter(families::contains).findFirst().orElse(javafx.scene.text.Font.getDefault().getFamily());
        String headingFamily=java.util.List.of(family+" SemiBold",family+" Semibold").stream().filter(families::contains).findFirst().orElse(family);
        // getFamilies() may have been cached before QuPath loaded our resources.
        if("Ubuntu Sans".equals(family)&&packagedSemibold!=null)headingFamily=packagedSemibold.getFamily();
        pane.setStyle("-fx-font-family:'"+family.replace("'","")+"';-fx-font-size:15px;");
        for(var heading:java.util.List.of(title,section,regionCaption,sampleCaption))
            heading.setStyle("-fx-font-family:'"+headingFamily.replace("'","")+"';");
        String css="""
            .dialog-pane { -fx-background-color:white; -fx-padding:0; -fx-accent:#006b70; -fx-focus-color:#006b70; -fx-faint-focus-color:transparent; }
            .dialog-pane > .content { -fx-padding:0; }
            .label, .check-box { -fx-text-fill:#172d35; }
            .export-title, .export-section, .export-caption { -fx-font-weight:600; }
            .export-title { -fx-font-size:20px; }
            .export-section { -fx-font-size:16px; }
            .export-caption { -fx-font-size:15px; -fx-text-fill:#294c56; }
            .export-help { -fx-text-fill:#486169; -fx-line-spacing:2; }
            .export-output { -fx-border-color:#dce4e4 transparent transparent transparent; -fx-border-width:1 0 0 0; -fx-padding:16 0 0 0; }
            .export-error { -fx-background-color:#fff3ef; -fx-border-color:#b4543e; -fx-border-width:0 0 0 3; -fx-text-fill:#8d3629; -fx-padding:10 12; }
            .combo-box, .text-field { -fx-background-color:white; -fx-background-insets:0; -fx-background-radius:3; -fx-border-color:#bdcccc; -fx-border-radius:3; -fx-border-width:1; -fx-padding:8 10; -fx-text-fill:#172d35; }
            .combo-box:hover, .text-field:hover { -fx-border-color:#8eaaa5; }
            .combo-box:focused, .text-field:focused { -fx-border-color:#006b70; -fx-border-width:2; -fx-padding:7 9; }
            .combo-box > .list-cell { -fx-background-color:transparent; -fx-text-fill:#172d35; -fx-padding:0 8 0 0; }
            .combo-box > .arrow-button { -fx-background-color:transparent; -fx-padding:0 2 0 8; }
            .combo-box .arrow { -fx-background-color:#526971; }
            .combo-box-popup .list-view { -fx-background-color:white; -fx-border-color:#bdcccc; -fx-padding:3; }
            .combo-box-popup .list-cell { -fx-background-color:white; -fx-text-fill:#172d35; -fx-padding:8 10; }
            .combo-box-popup .list-cell:filled:hover, .combo-box-popup .list-cell:filled:selected { -fx-background-color:#e4f0ee; -fx-text-fill:#00575c; }
            .check-box .box { -fx-background-color:white; -fx-border-color:#bdcccc; -fx-border-radius:2; -fx-background-radius:2; }
            .check-box:selected .box { -fx-background-color:#006b70; -fx-border-color:#006b70; }
            .check-box:selected .mark { -fx-background-color:white; }
            .dialog-pane > .button-bar { -fx-background-color:#f3f5f5; -fx-border-color:#dce4e4 transparent transparent transparent; -fx-border-width:1 0 0 0; -fx-padding:14 22; }
            .button { -fx-background-color:white; -fx-background-insets:0; -fx-background-radius:3; -fx-border-color:#bdcccc; -fx-border-radius:3; -fx-border-width:1; -fx-text-fill:#294148; -fx-padding:8 14; }
            .button:hover { -fx-background-color:#eaf2ef; -fx-border-color:#8eaaa5; }
            .button:focused { -fx-border-color:#006b70; -fx-border-width:2; -fx-padding:7 13; }
            .export-primary { -fx-background-color:#006b70; -fx-border-color:#006b70; -fx-text-fill:white; }
            .export-primary:hover { -fx-background-color:#00575c; -fx-border-color:#00575c; }
            .export-primary:focused { -fx-border-color:#82bcb3; }
            """;
        pane.getStylesheets().add("data:text/css;base64,"+java.util.Base64.getEncoder().encodeToString(css.getBytes(java.nio.charset.StandardCharsets.UTF_8)));
        var valid=new java.util.concurrent.atomic.AtomicReference<Request>();
        Runnable update=()->{
            valid.set(null);export.setDisable(true);dimensions.setText("");
            try {
                double resolution;
                try {resolution=Double.parseDouble(downsample.getText().trim());}
                catch(NumberFormatException invalid){throw new IllegalArgumentException("Enter a downsample number greater than zero.");}
                var request=capture(viewer,scope.getValue(),resolution,scaleBar.isSelected());
                validateSize(request);
                dimensions.setText(Math.max(1,Math.round(request.region().getWidth()/request.downsample()))+" × "+Math.max(1,Math.round(request.region().getHeight()/request.downsample()))+" pixels");
                error.setText("");valid.set(request);export.setDisable(false);
            }catch(Exception invalid){error.setText(invalid.getMessage());}
        };
        downsample.textProperty().addListener((observable,previous,value)->update.run());
        scaleBar.selectedProperty().addListener((observable,previous,value)->update.run());
        scope.valueProperty().addListener((observable,previous,value)->{
            try {
                var region=capture(viewer,value,1).region();
                double resolution=WHOLE.equals(value)?Math.max(1,Math.sqrt((double)region.getWidth()*region.getHeight()/4_000_000)):viewer.getDownsampleFactor();
                downsample.setText(Double.toString(resolution));
                update.run();
            }catch(IllegalArgumentException invalid){valid.set(null);export.setDisable(true);dimensions.setText("");error.setText(invalid.getMessage());}
        });
        // Recheck the live viewer before accepting, including selection/plane changes.
        export.addEventFilter(javafx.event.ActionEvent.ACTION,event->{update.run();if(valid.get()==null)event.consume();});
        dialog.setResultConverter(button->button==exportType?valid.get():null);
        error.textProperty().addListener((observable,previous,value)->{
            if(dialog.isShowing())Platform.runLater(()->{if(dialog.isShowing()&&pane.getScene()!=null)pane.getScene().getWindow().sizeToScene();});
        });
        scope.valueProperty().addListener((observable,previous,value)->{
            if(dialog.isShowing())Platform.runLater(()->{if(dialog.isShowing()&&pane.getScene()!=null)pane.getScene().getWindow().sizeToScene();});
        });
        update.run();
        return dialog;
    }

    private static Label exportLabel(String text,String style) {
        var label=new Label(text);label.setWrapText(true);label.setMaxWidth(Double.MAX_VALUE);label.getStyleClass().add(style);return label;
    }

    /** Render to a temporary file; incomplete reads never replace the chosen PNG. */
    public static void write(Request request,Path destination)throws Exception {
        LocalParquet.requireWorker();
        double downsample=request.downsample();var region=request.region();
        validateSize(request);
        checkSelection(request);
        var hierarchy=new HierarchyOverlay(request.store(),request.options(),request.image());
        Path temporary=Files.createTempFile(destination.toAbsolutePath().getParent(),".mif-view-",".png");
        try(var rendered=new RenderedImageServer.Builder(request.image()).settings(request.display()).store(request.store())
                .downsamples(downsample).overlayOpacity(request.options().getOpacity()).layers(hierarchy).build()) {
            var query=RegionRequest.createInstance(rendered.getPath(),downsample,region);
            BufferedImage result=rendered.readRegion(query);
            // Stream cells once over the requested rectangle, independently of raster tile sizes.
            if(request.options().getOpacity()>0) {
                var graphics=result.createGraphics();
                try {
                    graphics.setClip(0,0,result.getWidth(),result.getHeight());
                    graphics.scale(1/downsample,1/downsample);graphics.translate(-region.getX(),-region.getY());
                    graphics.setComposite(java.awt.AlphaComposite.getInstance(java.awt.AlphaComposite.SRC_OVER,request.options().getOpacity()));
                    paintCells(graphics,request.image(),region,downsample,request.options());
                }finally{graphics.dispose();}
            }
            checkSelection(request);
            var metadata=GsonTools.getInstance().fromJson(request.metadata(),JsonObject.class);
            metadata.add("display",GsonTools.getInstance().toJsonTree(request.display()));
            metadata.add("region",GsonTools.getInstance().toJsonTree(java.util.Map.of(
                    "x",region.getX(),"y",region.getY(),"width",region.getWidth(),"height",region.getHeight(),"z",region.getZ(),"t",region.getT())));
            metadata.addProperty("downsample",downsample);
            metadata.addProperty("outputWidth",result.getWidth());metadata.addProperty("outputHeight",result.getHeight());
            var calibration=metadata.getAsJsonObject("calibration");
            calibration.addProperty("outputPixelWidth",calibration.get("pixelWidth").getAsDouble()*downsample);
            calibration.addProperty("outputPixelHeight",calibration.get("pixelHeight").getAsDouble()*downsample);
            metadata.addProperty("showScaleBar",request.showScaleBar());
            if(request.showScaleBar())metadata.add("scaleBar",GsonTools.getInstance().toJsonTree(paintScaleBar(result,
                    calibration.get("outputPixelWidth").getAsDouble(),calibration.get("xUnit").getAsString())));
            writePng(result,metadata,temporary);
            Files.move(temporary,destination.toAbsolutePath(),StandardCopyOption.REPLACE_EXISTING);
        }finally{Files.deleteIfExists(temporary);}
    }

    /** Snapshot publication metadata with the same viewer choices as the rendered request. */
    private static String captureMetadata(QuPathViewer viewer) {
        var image=viewer.getImageData();var server=image.getServer();var options=viewer.getOverlayOptions();
        var calibration=server.getPixelCalibration();var metadata=new JsonObject();
        metadata.addProperty("imageName",server.getMetadata().getName());
        metadata.add("sourceUris",GsonTools.getInstance().toJsonTree(server.getURIs().stream().map(Object::toString).toList()));
        metadata.add("sourceChannels",GsonTools.getInstance().toJsonTree(server.getMetadata().getChannels()));
        var pixels=new JsonObject();
        pixels.addProperty("pixelWidth",calibration.getPixelWidth());pixels.addProperty("pixelHeight",calibration.getPixelHeight());
        pixels.addProperty("xUnit",calibration.getPixelWidthUnit());pixels.addProperty("yUnit",calibration.getPixelHeightUnit());
        metadata.add("calibration",pixels);
        var overlays=new JsonObject();
        overlays.addProperty("opacity",options.getOpacity());
        overlays.addProperty("showDetections",options.getShowDetections());overlays.addProperty("showAnnotations",options.getShowAnnotations());
        overlays.addProperty("showNames",options.getShowNames());overlays.addProperty("fillDetections",options.getFillDetections());
        overlays.addProperty("fillAnnotations",options.getFillAnnotations());overlays.addProperty("detectionDisplayMode",options.getDetectionDisplayMode().name());
        overlays.addProperty("classVisibilityMode",options.getSelectedClassVisibilityMode().name());
        overlays.addProperty("exactSelectedClasses",options.getUseExactSelectedClasses());
        overlays.add("selectedClasses",GsonTools.getInstance().toJsonTree(options.selectedClassesProperty().stream().map(Object::toString).toList()));
        metadata.add("overlays",overlays);
        metadata.add("cellDataset",GsonTools.getInstance().toJsonTree(image.getProperty(CellDatasetProvider.DATASET_PROPERTY)));
        metadata.add("cellComponents",GsonTools.getInstance().toJsonTree(image.getProperty(CellComponents.PROPERTY)));
        metadata.add("cellEdits",GsonTools.getInstance().toJsonTree(image.getProperty(CellDatasetProvider.EDITS_PROPERTY)));
        return GsonTools.getInstance().toJson(metadata);
    }

    /** Horizontal calibration alone determines a horizontal bar, including anisotropic images. */
    static ScaleBar paintScaleBar(BufferedImage image,double unitsPerPixel,String unit)throws IOException {
        if(!Double.isFinite(unitsPerPixel)||unitsPerPixel<=0)throw new IOException("A scale bar requires a positive pixel size");
        double maximum=image.getWidth()*0.25*unitsPerPixel;
        double decade=Math.pow(10,Math.floor(Math.log10(maximum)));
        double length=(maximum/decade>=5?5:maximum/decade>=2?2:1)*decade;
        double pixels=length/unitsPerPixel;
        if("µm".equals(unit)&&length>=1000){length/=1000;unit="mm";}
        String label=String.format(java.util.Locale.ROOT,"%s %s",java.math.BigDecimal.valueOf(length).stripTrailingZeros().toPlainString(),unit);
        var graphics=image.createGraphics();
        try {
            graphics.setRenderingHint(RenderingHints.KEY_TEXT_ANTIALIASING,RenderingHints.VALUE_TEXT_ANTIALIAS_ON);
            int fontSize=Math.max(16,(int)Math.round(image.getWidth()/60.0));
            graphics.setFont(new Font(Font.SANS_SERIF,Font.PLAIN,fontSize));
            var metrics=graphics.getFontMetrics();int padding=Math.max(8,fontSize/2),margin=Math.max(12,3*fontSize/4);
            int stroke=Math.max(3,(int)Math.round(fontSize*0.1875)),gap=Math.max(7,(int)Math.round(fontSize*0.4375));
            int width=(int)Math.ceil(Math.max(pixels,metrics.stringWidth(label)))+2*padding;
            int height=metrics.getHeight()+2*padding+gap;
            if(width+2*margin>image.getWidth()||height+2*margin>image.getHeight())
                throw new IOException("The scale bar does not fit; export a larger image or turn off Show scale bar");
            int x=image.getWidth()-margin-width,y=image.getHeight()-margin-height;
            graphics.setColor(new Color(0,0,0,175));graphics.fillRoundRect(x,y,width,height,6,6);
            graphics.setColor(Color.WHITE);
            graphics.drawString(label,x+(width-metrics.stringWidth(label))/2,y+padding+metrics.getAscent());
            graphics.fill(new java.awt.geom.Rectangle2D.Double(x+(width-pixels)/2,y+height-padding-stroke,pixels,stroke));
            return new ScaleBar(length,unit,pixels,x,y,width,height);
        }finally{graphics.dispose();}
    }

    /** Keep specimen calibration in text metadata; PNG print resolution remains unset. */
    private static void writePng(BufferedImage image,JsonObject exportMetadata,Path destination)throws IOException {
        var writers=ImageIO.getImageWritersByFormatName("png");
        if(!writers.hasNext())throw new IOException("No PNG writer is available");
        var writer=writers.next();
        try(var output=ImageIO.createImageOutputStream(destination.toFile())) {
            writer.setOutput(output);var parameters=writer.getDefaultWriteParam();
            var metadata=writer.getDefaultImageMetadata(ImageTypeSpecifier.createFromRenderedImage(image),parameters);
            var root=new IIOMetadataNode("javax_imageio_png_1.0");var text=new IIOMetadataNode("iTXt");
            var entry=new IIOMetadataNode("iTXtEntry");entry.setAttribute("keyword","mif-export");
            entry.setAttribute("compressionFlag","FALSE");entry.setAttribute("compressionMethod","0");
            entry.setAttribute("languageTag","");entry.setAttribute("translatedKeyword","");
            entry.setAttribute("text",GsonTools.getInstance().toJson(exportMetadata));text.appendChild(entry);root.appendChild(text);
            metadata.mergeTree("javax_imageio_png_1.0",root);
            writer.write(null,new IIOImage(image,null,metadata),parameters);
        }finally{writer.dispose();}
    }

    private static void validateSize(Request request)throws IOException {
        double downsample=request.downsample();var region=request.region();
        if(!Double.isFinite(downsample)||downsample<=0)throw new IOException("Downsample must be a finite number greater than zero");
        double pixels=(double)Math.max(1,Math.round(region.getWidth()/downsample))*Math.max(1,Math.round(region.getHeight()/downsample));
        if(pixels>MAX_PIXELS)throw new IOException("PNG output exceeds 16 million pixels; increase downsample to export a smaller image");
    }

    private static void checkSelection(Request request)throws IOException {
        if(!Objects.equals(request.binding(),request.image().getProperty(CellDatasetProvider.DATASET_PROPERTY))
                ||!Objects.equals(request.components(),request.image().getProperty(CellComponents.PROPERTY))
                ||!Objects.equals(request.edits(),request.image().getProperty(CellDatasetProvider.EDITS_PROPERTY)))
            throw new IOException("Cell results or edits changed during export; export the image again");
    }

    static Set<UUID> hierarchyCellIds(ImageData<?> image,ImageRegion region) {
        return image.getHierarchy().getAllDetectionsForRegion(region).stream().filter(PathCellObject.class::isInstance)
                .map(object->object.getID()).collect(Collectors.toSet());
    }

    /** Complete geometry iteration is independent of any asynchronous viewport snapshot. */
    public static void paintCells(Graphics2D graphics,ImageData<BufferedImage> image,ImageRegion region,
            double downsample,OverlayOptions options) {
        if(!options.getShowDetections()||image.getProperty(CellDatasetProvider.DATASET_PROPERTY)==null)return;
        LocalParquet.requireWorker();
        var ids=hierarchyCellIds(image,region);
        var bounds=new Envelope(region.getMinX(),region.getMaxX(),region.getMinY(),region.getMaxY());
        var clip=graphics.getClipBounds();
        if(clip!=null)bounds=bounds.intersection(new Envelope(clip.getMinX(),clip.getMaxX(),clip.getMinY(),clip.getMaxY()));
        if(bounds.isNull())return;
        try {
            NativeCellScope.open(image).join().forEachInBounds(bounds,object->{
                if(object.getROI().getZ()==region.getZ()&&object.getROI().getT()==region.getT())
                    CellSnapshotPainter.paintObject(graphics,object,downsample,options,ids);
            });
        }catch(IOException error){throw new UncheckedIOException("Cannot export complete cell outlines",error);}
    }
}
