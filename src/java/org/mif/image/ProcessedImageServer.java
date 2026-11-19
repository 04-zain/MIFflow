package org.mif.image;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import dev.zarr.zarrjava.v3.Array;
import java.awt.image.BandedSampleModel;
import java.awt.image.BufferedImage;
import java.awt.image.DataBuffer;
import java.awt.image.DataBufferDouble;
import java.awt.image.DataBufferFloat;
import java.awt.image.DataBufferByte;
import java.awt.image.DataBufferShort;
import java.awt.image.DataBufferUShort;
import java.awt.image.DataBufferInt;
import java.awt.image.Raster;
import java.io.IOException;
import java.net.URI;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.HexFormat;
import java.util.ArrayList;
import java.util.Collection;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;
import qupath.lib.color.ColorModelFactory;
import qupath.lib.images.servers.AbstractTileableImageServer;
import qupath.lib.images.servers.ImageChannel;
import qupath.lib.images.servers.ImageServerBuilder;
import qupath.lib.images.servers.ImageServerMetadata;
import qupath.lib.images.servers.PixelType;
import qupath.lib.images.servers.TileRequest;

/**
 * Reads completed c,y,x image levels directly through zarr-java 0.3.1.
 *
 * We resolve the NGFF axes, component order and physical scales when opening.
 * QuPath schedules scientific tile reads on its image workers; each read loads
 * the intersecting all-channel chunk using the standard partial shard decoder.
 * Resident and concurrent requests share the same immutable native image tile.
 * A read failure propagates to QuPath without substituting display pixels.
 */
public final class ProcessedImageServer extends AbstractTileableImageServer {
    private final URI uri;
    private final String[] args;
    private final RangeStore store;
    private final List<Array> arrays = new ArrayList<>();
    private final ImageServerMetadata original;
    private final long cacheBudget;
    private final String completedGenerationSha256;
    private volatile boolean closed;
    private final ConcurrentHashMap<String, CompletableFuture<BufferedImage>> pending = new ConcurrentHashMap<>();
    private final AtomicLong decodes = new AtomicLong();

    static boolean isSupported(Path root) throws IOException {
        JsonObject metadata = JsonParser.parseString(new String(readRootMetadata(root), StandardCharsets.UTF_8)).getAsJsonObject();
        if (!metadata.has("zarr_format") || metadata.get("zarr_format").getAsInt() != 3) return false;
        JsonObject attributes = metadata.getAsJsonObject("attributes");
        return attributes != null && attributes.has("ome")
                && attributes.getAsJsonObject("ome").has("version")
                && "0.5".equals(attributes.getAsJsonObject("ome").get("version").getAsString());
    }

    /** Read the root description, including retained source-image metadata. */
    private static byte[] readRootMetadata(Path root) throws IOException {
        return Files.readAllBytes(root.resolve("zarr.json"));
    }

    /** Reuse the admitted generation identity when selecting persistent numerical features. */
    public String getCompletedGenerationSha256() throws IOException {
        if (closed || completedGenerationSha256 == null)
            throw new IOException("Frozen features require an open, completed pipeline image generation");
        return completedGenerationSha256;
    }

    public ProcessedImageServer(URI uri, String... args) throws IOException {
        this.uri = uri;
        this.args = args.clone();
        Path root = Path.of(uri).toAbsolutePath().normalize();
        long requestedBudget = Long.parseLong(System.getenv().getOrDefault("MIF_IMAGE_CACHE_BYTES",Long.toString(64L*1024*1024)));
        for (int i = 0; i < args.length; i++) {
            if ("--cache-bytes".equals(args[i]) && i + 1 < args.length)
                requestedBudget = Long.parseLong(args[++i]);
        }
        if (requestedBudget < 0) throw new IOException("Image cache allowance must be nonnegative");
        cacheBudget = requestedBudget;
        DecodedImageCache.open(uri.toString());
        store = new RangeStore(root, Math.min(1024L * 1024, requestedBudget / 16));
        try {
            // i) Resolve the external image metadata once, before processing pixels.
            byte[] rootBytes = readRootMetadata(root);
            JsonObject rootMetadata = JsonParser.parseString(new String(rootBytes, StandardCharsets.UTF_8)).getAsJsonObject();
            JsonObject attributes = rootMetadata.getAsJsonObject("attributes");
            JsonObject ome = attributes.getAsJsonObject("ome");
            if (rootMetadata.get("zarr_format").getAsInt() != 3 || !"0.5".equals(ome.get("version").getAsString()))
                throw new IOException("Processed image requires OME-NGFF 0.5 on Zarr 3");
            if (attributes.has("mif_pipeline") && !attributes.getAsJsonObject("mif_pipeline").get("complete").getAsBoolean())
                throw new IOException("Processed image preparation contains unfinished shards");
            completedGenerationSha256 = attributes.has("mif_pipeline")
                    ? HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(rootBytes)) : null;
            JsonArray multiscales = ome.getAsJsonArray("multiscales");
            if (multiscales.size() != 1) throw new IOException("Select one two-dimensional image before opening");
            JsonObject multiscale = multiscales.get(0).getAsJsonObject();
            JsonArray axes = multiscale.getAsJsonArray("axes");
            if (axes.size() != 3 || !"c".equals(axes.get(0).getAsJsonObject().get("name").getAsString())
                    || !"y".equals(axes.get(1).getAsJsonObject().get("name").getAsString())
                    || !"x".equals(axes.get(2).getAsJsonObject().get("name").getAsString()))
                throw new IOException("Processed image must contain the selected c,y,x plane");
            for (int axis = 1; axis <= 2; axis++)
                if (!"micrometer".equals(axes.get(axis).getAsJsonObject().get("unit").getAsString()))
                    throw new IOException("Processed image spatial calibration must use micrometres");

            JsonArray datasets = multiscale.getAsJsonArray("datasets");
            double x0 = 0, y0 = 0;
            int channels = 0, width = 0, height = 0;
            PixelType pixelType = null;
            ImageServerMetadata.ImageResolutionLevel.Builder levels = null;
            // ii) Open level arrays and preserve physical downsample independently of odd dimensions.
            for (int level = 0; level < datasets.size(); level++) {
                JsonObject dataset = datasets.get(level).getAsJsonObject();
                String location = dataset.get("path").getAsString();
                Array array = Array.open(store.resolve(location));
                JsonObject raw = JsonParser.parseString(Files.readString(root.resolve(location).resolve("zarr.json"))).getAsJsonObject();
                long[] shape = array.metadata().shape;
                if (shape.length != 3) throw new IOException("Processed image array must have three dimensions");
                String dtype = raw.get("data_type").getAsString();
                PixelType current = switch (dtype) {
                    case "float32" -> PixelType.FLOAT32;
                    case "float64" -> PixelType.FLOAT64;
                    case "uint8" -> PixelType.UINT8;
                    case "int8" -> PixelType.INT8;
                    case "uint16" -> PixelType.UINT16;
                    case "int16" -> PixelType.INT16;
                    case "uint32" -> PixelType.FLOAT64; // AWT signed int rasters cannot preserve UInt32 samples.
                    case "int32" -> PixelType.INT32;
                    default -> throw new IOException("Unsupported processed image scalar type: " + dtype);
                };
                requireProfile(raw, Math.toIntExact(shape[0]), "uint32".equals(dtype) ? 4 : current.getBytesPerPixel());
                double[] scale = scale(dataset);
                if (level == 0) {
                    channels = Math.toIntExact(shape[0]); width = Math.toIntExact(shape[2]); height = Math.toIntExact(shape[1]);
                    x0 = scale[2]; y0 = scale[1]; pixelType = current;
                    levels = new ImageServerMetadata.ImageResolutionLevel.Builder(width, height);
                } else if (shape[0] != channels || current != pixelType) {
                    throw new IOException("Processed image levels disagree on channels or scalar type");
                }
                double downsample = scale[2] / x0;
                if (!(downsample > 0) || Math.abs(scale[1] / y0 - downsample) > downsample * 1e-8)
                    throw new IOException("Image level has incompatible X/Y downsample factors");
                levels.addLevel(downsample, Math.toIntExact(shape[2]), Math.toIntExact(shape[1]));
                arrays.add(array);
                if (attributes.has("mif_pipeline")) store.declareCompletedLevel(location, (int)((shape[2] + 1023) / 1024));
            }
            if (arrays.isEmpty()) throw new IOException("Processed image contains no image levels");
            var sourceMetadata = attributes.has("mif_pipeline")
                    ? new loci.formats.services.OMEXMLServiceImpl().createOMEXMLMetadata(
                        attributes.getAsJsonObject("mif_pipeline").get("ome_xml").getAsString())
                    : null;
            boolean rgb = sourceMetadata != null && isRGBSource(sourceMetadata);
            if (rgb && (channels != 3 || pixelType != PixelType.UINT8))
                throw new IOException("Processed RGB image requires three uint8 sample planes");
            List<ImageChannel> channelList = new ArrayList<>();
            JsonArray descriptions = ome.has("omero") ? ome.getAsJsonObject("omero").getAsJsonArray("channels") : null;
            if (descriptions == null || descriptions.size() != channels)
                throw new IOException("Processed image channel description does not match its component count");
            for (int c = 0; c < channels; c++) {
                JsonObject description = descriptions.get(c).getAsJsonObject();
                int color = description.has("color") ? Integer.parseInt(description.get("color").getAsString(), 16)
                        : ImageChannel.getDefaultChannelColor(c);
                channelList.add(ImageChannel.getInstance(description.get("label").getAsString(), color));
            }
            var metadataBuilder = new ImageServerMetadata.Builder(getClass(), uri.toString(), width, height)
                    .name(root.getFileName().toString()).rgb(rgb).pixelType(pixelType)
                    .channels(channelList).pixelSizeMicrons(x0, y0).levels(levels.build())
                    .preferredTileSize(256, 256);
            double magnification = sourceMagnification(sourceMetadata);
            if (Double.isFinite(magnification) && magnification > 0)
                metadataBuilder.magnification(magnification);
            original = metadataBuilder.build();
        } catch (IOException failure) { store.clear();DecodedImageCache.close(uri.toString());throw failure; }
        catch (Exception failure) { store.clear();DecodedImageCache.close(uri.toString());throw new IOException("Unable to open processed image " + root, failure); }
    }

    /** Identify RGB samples from the retained OME channel topology once at open. */
    private static boolean isRGBSource(ome.xml.meta.MetadataRetrieve metadata) {
        return metadata.getImageCount() == 1 && metadata.getChannelCount(0) == 1
                && metadata.getPixelsSizeC(0).getValue() == 3
                && metadata.getChannelSamplesPerPixel(0, 0) != null
                && metadata.getChannelSamplesPerPixel(0, 0).getValue() == 3
                && metadata.getPixelsType(0) == ome.xml.model.enums.PixelType.UINT8;
    }

    /**
     * We read nominal magnification from the objective linked to the selected
     * OME image. Other instruments and objectives do not describe this image;
     * missing references leave magnification unknown without inferring it from
     * pixel size. The retained OME metadata is shared with the RGB check.
     */
    private static double sourceMagnification(ome.xml.meta.OMEXMLMetadata metadata) {
        if (metadata == null || metadata.getImageCount() != 1) return Double.NaN;
        var image = ((ome.xml.model.OME) metadata.getRoot()).getImage(0);
        var instrument = image.getLinkedInstrument();
        var settings = image.getObjectiveSettings();
        if (instrument == null || settings == null || settings.getID() == null) return Double.NaN;
        for (int index = 0; index < instrument.sizeOfObjectiveList(); index++) {
            var objective = instrument.getObjective(index);
            if (!settings.getID().equals(objective.getID())) continue;
            Double value = objective.getNominalMagnification();
            return value != null && Double.isFinite(value) && value > 0 ? value : Double.NaN;
        }
        return Double.NaN;
    }

    private static double[] scale(JsonObject dataset) throws IOException {
        double[] result = {1, 1, 1};
        boolean found = false;
        for (var element : dataset.getAsJsonArray("coordinateTransformations")) {
            JsonObject transform = element.getAsJsonObject();
            String type = transform.get("type").getAsString();
            if ("scale".equals(type)) {
                JsonArray values = transform.getAsJsonArray("scale");
                if (values.size() != 3) throw new IOException("Image scale must describe c,y,x");
                for (int i = 0; i < 3; i++) result[i] *= values.get(i).getAsDouble();
                found = true;
            } else if ("translation".equals(type)) {
                for (var value : transform.getAsJsonArray("translation"))
                    if (value.getAsDouble() != 0) throw new IOException("QuPath image coordinates require an explicit transform for a translated image");
            } else throw new IOException("Unsupported image coordinate transform: " + type);
        }
        if (!found || !Double.isFinite(result[1]) || !Double.isFinite(result[2]) || result[1] <= 0 || result[2] <= 0)
            throw new IOException("Processed image requires positive finite X/Y calibration");
        return result;
    }

    private static void requireProfile(JsonObject raw, int channels, int scalarBytes) throws IOException {
        JsonArray shard = raw.getAsJsonObject("chunk_grid").getAsJsonObject("configuration").getAsJsonArray("chunk_shape");
        JsonArray codecs = raw.getAsJsonArray("codecs");
        if (channels < 1 || shard.size() != 3 || shard.get(0).getAsInt() != channels
                || shard.get(1).getAsInt() != 1024 || shard.get(2).getAsInt() != 1024
                || codecs.size() != 1 || !"sharding_indexed".equals(codecs.get(0).getAsJsonObject().get("name").getAsString()))
            throw new IOException("Processed image requires all-channel 1024-square indexed shards");
        JsonObject config = codecs.get(0).getAsJsonObject().getAsJsonObject("configuration");
        JsonArray chunks = config.getAsJsonArray("chunk_shape");
        JsonArray inner = config.getAsJsonArray("codecs");
        JsonArray index = config.getAsJsonArray("index_codecs");
        if (chunks.size() != 3 || chunks.get(0).getAsInt() != channels || chunks.get(1).getAsInt() != 256 || chunks.get(2).getAsInt() != 256
                || !"end".equals(config.get("index_location").getAsString()) || inner.size() != 2 || index.size() != 2
                || !"crc32c".equals(index.get(1).getAsJsonObject().get("name").getAsString()))
            throw new IOException("Processed image requires 256-square chunks and an end index with CRC32C");
        JsonObject blosc = inner.get(1).getAsJsonObject().getAsJsonObject("configuration");
        if (!"bytes".equals(inner.get(0).getAsJsonObject().get("name").getAsString())
                || (scalarBytes > 1 && !"little".equals(inner.get(0).getAsJsonObject().getAsJsonObject("configuration").get("endian").getAsString()))
                || !"blosc".equals(inner.get(1).getAsJsonObject().get("name").getAsString())
                || !"zstd".equals(blosc.get("cname").getAsString()) || blosc.get("clevel").getAsInt() != 1
                || !"shuffle".equals(blosc.get("shuffle").getAsString()) || blosc.get("typesize").getAsInt() != scalarBytes
                || raw.get("fill_value").getAsDouble() != 0)
            throw new IOException("Processed image requires little-endian lossless Blosc/Zstd level1 byte shuffle and zero fill");
    }

    @Override protected BufferedImage readTile(TileRequest request) throws IOException {
        if (closed) throw new IOException("Processed image reader is closed");
        String key = request.getLevel() + ":" + request.getTileY() + ":" + request.getTileX();
        BufferedImage resident = DecodedImageCache.get(uri.toString(),key);
        if (resident != null) return resident;
        var future = new CompletableFuture<BufferedImage>();
        var existing = pending.putIfAbsent(key, future);
        if (existing != null) {
            try { return existing.get(); }
            catch (InterruptedException interrupted) { Thread.currentThread().interrupt(); throw new IOException("Image request interrupted", interrupted); }
            catch (Exception failure) { throw new IOException("Shared image request failed", failure.getCause()); }
        }
        try {
            // iii) Decode all channels once and construct native planes with bulk copies.
            int w = request.getTileWidth(), h = request.getTileHeight(), c = nChannels();
            var values = arrays.get(request.getLevel()).read(new long[]{0, request.getTileY(), request.getTileX()},
                    new long[]{c, h, w}, false);
            decodes.incrementAndGet();
            int pixels = Math.multiplyExact(w, h);
            BufferedImage tile;
            long bytes;
            if (isRGB()) {
                // The source RGB samples are stored as planar bytes; pack them exactly once.
                byte[] flat = (byte[])values.copyTo1DJavaArray();
                tile = new BufferedImage(w, h, BufferedImage.TYPE_INT_RGB);
                int[] packed = ((DataBufferInt)tile.getRaster().getDataBuffer()).getData();
                for (int i = 0; i < pixels; i++)
                    packed[i] = (flat[i] & 255) << 16 | (flat[pixels + i] & 255) << 8
                            | (flat[2 * pixels + i] & 255);
                bytes = (long)pixels * Integer.BYTES;
            } else {
                DataBuffer buffer;
                int type;
                if (getPixelType() == PixelType.FLOAT32) {
                    float[] flat = (float[])values.copyTo1DJavaArray();
                    float[][] planes = new float[c][pixels];
                    for (int channel = 0; channel < c; channel++) System.arraycopy(flat, channel * pixels, planes[channel], 0, pixels);
                    buffer = new DataBufferFloat(planes, pixels); type = DataBuffer.TYPE_FLOAT;
                } else if (getPixelType() == PixelType.FLOAT64) {
                    Object scalarValues = values.copyTo1DJavaArray();
                    double[] flat;
                    if (scalarValues instanceof int[] unsigned) {
                        flat = new double[unsigned.length];
                        for (int i = 0; i < unsigned.length; i++) flat[i] = Integer.toUnsignedLong(unsigned[i]);
                    } else flat = (double[])scalarValues;
                    double[][] planes = new double[c][pixels];
                    for (int channel = 0; channel < c; channel++) System.arraycopy(flat, channel * pixels, planes[channel], 0, pixels);
                    buffer = new DataBufferDouble(planes, pixels); type = DataBuffer.TYPE_DOUBLE;
                } else if (getPixelType() == PixelType.UINT8) {
                    byte[] flat = (byte[])values.copyTo1DJavaArray();
                    byte[][] planes = new byte[c][pixels];
                    for (int channel = 0; channel < c; channel++) System.arraycopy(flat, channel * pixels, planes[channel], 0, pixels);
                    buffer = new DataBufferByte(planes, pixels); type = DataBuffer.TYPE_BYTE;
                } else if (getPixelType() == PixelType.UINT16 || getPixelType() == PixelType.INT16) {
                    short[] flat = (short[])values.copyTo1DJavaArray();
                    short[][] planes = new short[c][pixels];
                    for (int channel = 0; channel < c; channel++) System.arraycopy(flat, channel * pixels, planes[channel], 0, pixels);
                    boolean unsigned = getPixelType() == PixelType.UINT16;
                    buffer = unsigned ? new DataBufferUShort(planes, pixels) : new DataBufferShort(planes, pixels);
                    type = unsigned ? DataBuffer.TYPE_USHORT : DataBuffer.TYPE_SHORT;
                } else if (getPixelType() == PixelType.INT8) {
                    byte[] flat = (byte[])values.copyTo1DJavaArray();
                    short[][] planes = new short[c][pixels];
                    for (int channel = 0; channel < c; channel++)
                        for (int index = 0; index < pixels; index++) planes[channel][index] = flat[channel * pixels + index];
                    buffer = new DataBufferShort(planes, pixels); type = DataBuffer.TYPE_SHORT;
                } else {
                    int[] flat = (int[])values.copyTo1DJavaArray();
                    int[][] planes = new int[c][pixels];
                    for (int channel = 0; channel < c; channel++) System.arraycopy(flat, channel * pixels, planes[channel], 0, pixels);
                    buffer = new DataBufferInt(planes, pixels);
                    type = DataBuffer.TYPE_INT;
                }
                var raster = Raster.createWritableRaster(new BandedSampleModel(type, w, h, c), buffer, null);
                tile = new BufferedImage(ColorModelFactory.createColorModel(getPixelType(), getMetadata().getChannels()), raster, false, null);
                bytes = (long)c * pixels * DataBuffer.getDataTypeSize(type) / 8;
            }
            if(!closed)DecodedImageCache.put(uri.toString(),key,tile,bytes,cacheBudget);
            future.complete(tile);
            return tile;
        } catch (Exception failure) {
            future.completeExceptionally(failure);
            throw new IOException("Processed image tile read failed: " + key, failure);
        } finally { pending.remove(key, future); }
    }

    public long getDecodeCount() { return decodes.get(); }
    public long getRangeReadCount() { return store.reads.get(); }
    public long getRangeReadBytes() { return store.bytes.get(); }
    public long getDecodedCacheBytes() { return DecodedImageCache.ownerBytes(uri.toString()); }
    public long getProcessDecodedCacheBytes() { return DecodedImageCache.processBytes(); }
    @Override public ImageServerMetadata getOriginalMetadata() { return original; }
    @Override public String getServerType() { return "mIF NGFF 0.5 / zarr-java 0.3.1"; }
    @Override public Collection<URI> getURIs() { return List.of(uri); }
    @Override protected String createID() { return getClass().getName() + ":" + uri; }
    @Override protected ImageServerBuilder.ServerBuilder<BufferedImage> createServerBuilder() {
        return ImageServerBuilder.DefaultImageServerBuilder.createInstance(ProcessedImageServerBuilder.class, original, uri, args);
    }
    @Override public void close() throws Exception {
        if(closed)return;closed = true;
        DecodedImageCache.close(uri.toString());
        store.clear(); super.close();
    }
}
