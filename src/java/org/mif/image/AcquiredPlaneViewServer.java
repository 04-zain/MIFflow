package org.mif.image;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonNull;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.google.gson.JsonPrimitive;
import com.google.gson.Strictness;
import com.google.gson.stream.JsonReader;
import com.google.gson.stream.JsonToken;
import java.awt.image.BandedSampleModel;
import java.awt.image.BufferedImage;
import java.awt.image.DataBuffer;
import java.awt.image.Raster;
import java.io.IOException;
import java.io.StringReader;
import java.math.BigDecimal;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.BasicFileAttributes;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Collection;
import java.util.HashMap;
import java.util.HashSet;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import ome.units.UNITS;
import ome.xml.model.enums.UnitsLength;
import org.jdom2.Element;
import org.jdom2.filter.ElementFilter;
import org.jdom2.input.SAXBuilder;
import org.jdom2.input.sax.XMLReaders;
import qupath.lib.color.ColorModelFactory;
import qupath.lib.images.servers.AbstractTileableImageServer;
import qupath.lib.images.servers.ImageChannel;
import qupath.lib.images.servers.ImageServer;
import qupath.lib.images.servers.ImageServerBuilder;
import qupath.lib.images.servers.ImageServerMetadata;
import qupath.lib.images.servers.ImageServers;
import qupath.lib.images.servers.PixelType;
import qupath.lib.images.servers.TileRequest;
import qupath.lib.regions.RegionRequest;

/** A complete registered C-by-T view, addressed by the original source decoder. */
public final class AcquiredPlaneViewServer extends AbstractTileableImageServer {
    private static final int MAX_PLANES = 512;
    private static final long MAX_DESCRIPTOR_BYTES = 32L * 1024 * 1024;
    private final URI uri;
    private final String[] args;
    private final Path descriptor;
    private final BasicFileAttributes descriptorIdentity;
    private final String descriptorDigest;
    private final Path parentPath;
    private final JsonObject parentIdentity;
    private final ImageServer<BufferedImage> parent;
    private final ImageServerMetadata original;
    private final int sourceChannels;
    private final int sourceTimes;
    private boolean closed;

    public AcquiredPlaneViewServer(URI uri, String... args) throws IOException {
        this.uri = uri;
        this.args = args.clone();
        descriptor = Path.of(uri);
        descriptorIdentity = Files.readAttributes(descriptor, BasicFileAttributes.class);
        if (!descriptorIdentity.isRegularFile() || descriptorIdentity.size() > MAX_DESCRIPTOR_BYTES || Files.isSymbolicLink(descriptor))
            throw new IOException("Invalid acquired-plane description");
        ImageServer<BufferedImage> opened = null;
        try {
            byte[] payload = Files.readAllBytes(descriptor);
            descriptorDigest = digest(payload);
            JsonObject root = parseObject(new String(payload, StandardCharsets.UTF_8));
            fields(root, "schema", "parent_inspection", "decision", "rows", "view_ome_xml");
            if (!"mif-acquired-plane-view/v1".equals(string(root, "schema")))
                throw new IOException("Unsupported acquired-plane description");
            String retained = string(root, "parent_inspection");
            JsonObject inspection = parseObject(retained);
            fields(inspection, "schema_name", "schema_version", "source_identity", "canonical_ome_xml",
                    "series", "selection", "backend_versions", "content_sha256");
            if (!"mif-inspected-source-authority".equals(string(inspection, "schema_name")) || integer(inspection, "schema_version") != 2)
                throw new IOException("Acquired planes require an original inspected source");
            String contentDigest = string(inspection, "content_sha256");
            if (!retained.equals(canonical(inspection))) throw new IOException("Original inspection bytes are not canonical");
            JsonObject unsigned = inspection.deepCopy();
            unsigned.remove("content_sha256");
            if (!contentDigest.equals(digest(canonical(unsigned).getBytes(StandardCharsets.UTF_8))))
                throw new IOException("Original inspection content digest is stale");
            parentIdentity = inspection.getAsJsonObject("source_identity");
            fields(parentIdentity, "resolved_path", "file_sha256", "size_bytes", "modified_time_ns", "device_id", "inode_id");
            parentPath = Path.of(string(parentIdentity, "resolved_path"));
            if (!parentPath.isAbsolute() || !parentPath.normalize().equals(parentPath) ||
                    parentPath.toString().toLowerCase(java.util.Locale.ROOT).endsWith(".mif-plane-view.json"))
                throw new IOException("Acquired-plane views require an original parent source");
            for (String key : List.of("size_bytes", "modified_time_ns", "device_id", "inode_id")) {
                try { Long.parseLong(nonnegative(parentIdentity.get(key), key)); }
                catch (NumberFormatException error) { throw new IOException("Original source identity number is too large: " + key, error); }
            }
            String sourceDigest = string(parentIdentity, "file_sha256");
            if (!sourceDigest.matches("[0-9a-f]{64}")) throw new IOException("Invalid original source checksum");
            AlignedSourceCollectionServer.validateSourceIdentity(parentPath, parentIdentity);
            if (!sourceDigest.equals(fileDigest(Files.isDirectory(parentPath) ? parentPath.resolve("zarr.json") : parentPath)))
                throw new IOException("Original source checksum differs from inspection");
            JsonObject selection = inspection.getAsJsonObject("selection");
            fields(selection, "source_image_index", "source_series_index", "image_id", "pixels_id", "series_name");
            int series = integer(selection, "source_series_index"), imageIndex = integer(selection, "source_image_index");
            if (series != imageIndex) throw new IOException("Original image and series selection are not aligned");
            Element sourceOme = xml(string(inspection, "canonical_ome_xml"));
            var images = children(sourceOme, "Image");
            if (imageIndex >= images.size()) throw new IOException("Original image selection is outside its inspection");
            Element sourceImage = images.get(imageIndex), sourcePixels = onlyChild(sourceImage, "Pixels");
            if (!string(selection, "image_id").equals(sourceImage.getAttributeValue("ID", "")) ||
                    !string(selection, "pixels_id").equals(sourcePixels.getAttributeValue("ID", "")))
                throw new IOException("Original image selection differs from its inspection");
            sourceChannels = attributeInt(sourcePixels, "SizeC");
            sourceTimes = attributeInt(sourcePixels, "SizeT");
            if (sourceChannels < 1 || sourceTimes < 1 || (long)sourceChannels * sourceTimes > MAX_PLANES || attributeInt(sourcePixels, "SizeZ") != 1)
                throw new IOException("Acquired planes require a scalar complete C-by-T grid, Z=1 and at most 512 planes");
            rejectLinkedModulo(sourceOme, sourceImage, sourcePixels);
            var sourceChannelElements = children(sourcePixels, "Channel");
            if (sourceChannelElements.size() != sourceChannels) throw new IOException("Original scalar channel inventory differs");
            Set<String> channelIds = new HashSet<>();
            for (Element channel : sourceChannelElements) {
                if (channel.getAttributeValue("ID", "").isEmpty() || !channelIds.add(channel.getAttributeValue("ID", "")) ||
                        (channel.getAttribute("SamplesPerPixel") != null && attributeInt(channel, "SamplesPerPixel") != 1))
                    throw new IOException("Acquired planes require unique original scalar channels");
            }
            if (sourceTimes > 1) rejectChannelCycles(sourceOme, sourceChannelElements);
            JsonArray rows = root.getAsJsonArray("rows");
            validateRows(rows, root.getAsJsonObject("decision"), series, sourceChannelElements);
            Element viewPixels = validateProjection(xml(string(root, "view_ome_xml")), sourcePixels, rows);
            JsonArray inventory = inspection.getAsJsonArray("series");
            if (series >= inventory.size()) throw new IOException("Original series is outside its inspection");
            for (int i = 0; i < inventory.size(); i++) {
                JsonObject item = inventory.get(i).getAsJsonObject();
                fields(item, "index", "name", "resolutions", "storage_bits");
                if (integer(item, "index") != i) throw new IOException("Original series inventory is not ordered");
            }
            JsonObject selected = inventory.get(series).getAsJsonObject();
            if (!string(selection, "series_name").equals(string(selected, "name")))
                throw new IOException("Original selected series name differs from inspection");
            var support = ImageServers.getImageSupport(parentPath.toUri(), "--series", Integer.toString(series));
            if (support == null || support.getBuilders().size() != 1) throw new IOException("Acquired planes require one original image series");
            opened = support.getBuilders().get(0).build();
            validateGeometry(opened, selected.getAsJsonArray("resolutions"), sourcePixels);
            var metadata = opened.getMetadata();
            List<ImageChannel> channels = new ArrayList<>();
            var viewChannels = children(viewPixels, "Channel");
            for (int v = 0; v < rows.size(); v++) {
                var sourceChannel = metadata.getChannels().get(v % sourceChannels);
                String name = viewChannels.get(v).getAttributeValue("Name", "");
                channels.add(ImageChannel.getInstance(name.isBlank() ? sourceChannel.getName() : name, sourceChannel.getColor()));
            }
            original = new ImageServerMetadata.Builder(getClass(), uri.toString(), opened.getWidth(), opened.getHeight())
                    .name(metadata.getName()).rgb(false).pixelType(metadata.getPixelType()).sizeZ(1).sizeT(1).channels(channels)
                    .pixelSizeMicrons(metadata.getPixelCalibration().getPixelWidthMicrons(), metadata.getPixelCalibration().getPixelHeightMicrons())
                    .levels(metadata.getLevels()).preferredTileSize(256, 256).build();
            parent = opened;
            validateSources();
        } catch (Exception error) {
            if (opened != null) try { opened.close(); } catch (Exception closeError) { error.addSuppressed(closeError); }
            throw new IOException("Cannot open acquired planes: " + error.getMessage(), error);
        }
    }

    private void validateRows(JsonArray rows, JsonObject decision, int series, List<Element> source) throws IOException {
        fields(decision, "origin", "layout", "registered");
        if (!"researcher".equals(string(decision, "origin")) ||
                !(sourceTimes > 1 ? "t_cycles" : "planar_channels").equals(string(decision, "layout")) ||
                !decision.get("registered").isJsonPrimitive() || !decision.getAsJsonPrimitive("registered").isBoolean() ||
                (sourceTimes > 1 && !decision.get("registered").getAsBoolean()))
            throw new IOException("Acquired T planes require explicit researcher acceptance of registered cycles");
        if (rows == null || rows.size() != sourceChannels * sourceTimes) throw new IOException("Acquired planes must retain the complete original C-by-T grid");
        Set<String> cycles = new HashSet<>();
        String currentCycle = null;
        for (int v = 0; v < rows.size(); v++) {
            JsonObject row = rows.get(v).getAsJsonObject();
            fields(row, "source_series_index", "source_c", "source_t", "source_z", "source_channel_id", "channel_id", "cycle");
            int c = v % sourceChannels, t = v / sourceChannels;
            String sourceId = source.get(c).getAttributeValue("ID", "");
            if (integer(row, "source_series_index") != series || integer(row, "source_c") != c ||
                    integer(row, "source_t") != t || integer(row, "source_z") != 0 ||
                    !sourceId.equals(string(row, "source_channel_id")) ||
                    !(sourceTimes > 1 ? "Channel:AcquiredPlane:" + v : sourceId).equals(string(row, "channel_id")))
                throw new IOException("Acquired-plane addresses must retain original T-major, then C order");
            String cycle = row.get("cycle").isJsonNull() ? null : nonnegative(row.get("cycle"), "cycle");
            if (sourceTimes > 1) {
                if (cycle == null) throw new IOException("Every original T needs an acquisition cycle");
                if (c == 0) {
                    if (!cycles.add(cycle)) throw new IOException("Original T planes cannot merge acquisition cycles");
                    currentCycle = cycle;
                } else if (!cycle.equals(currentCycle)) throw new IOException("Each original T must belong to one cycle");
            }
        }
    }

    private Element validateProjection(Element viewOme, Element source, JsonArray rows) throws IOException {
        var images = children(viewOme, "Image");
        if (images.size() != 1) throw new IOException("Acquired-plane metadata must describe one image");
        Element view = onlyChild(images.get(0), "Pixels");
        if (!images.get(0).getAttributeValue("ID", "").equals(source.getParentElement().getAttributeValue("ID", "")) ||
                !attributes(view, Set.of("SizeC", "SizeT", "SizeZ", "DimensionOrder")).equals(
                    attributes(source, Set.of("SizeC", "SizeT", "SizeZ", "DimensionOrder"))) ||
                attributeInt(view, "SizeC") != rows.size() || attributeInt(view, "SizeT") != 1 || attributeInt(view, "SizeZ") != 1 ||
                attributeInt(view, "SizeX") != attributeInt(source, "SizeX") || attributeInt(view, "SizeY") != attributeInt(source, "SizeY") ||
                !view.getAttributeValue("Type", "").equals(source.getAttributeValue("Type", "")))
            throw new IOException("Acquired-plane metadata geometry differs from its original source");
        var sourceChannelsXml = children(source, "Channel");
        var viewChannels = children(view, "Channel");
        if (viewChannels.size() != rows.size()) throw new IOException("Acquired-plane channel metadata differs");
        for (int v = 0; v < rows.size(); v++) {
            if (!viewChannels.get(v).getAttributeValue("ID", "").equals(string(rows.get(v).getAsJsonObject(), "channel_id")) ||
                    !attributes(viewChannels.get(v), Set.of("ID")).equals(attributes(sourceChannelsXml.get(v % sourceChannels), Set.of("ID"))))
                throw new IOException("Acquired-plane channel facts differ from the original channel");
        }
        Map<Integer, Element> planes = new HashMap<>();
        for (Element plane : children(source, "Plane")) {
            int c = attributeInt(plane, "TheC"), t = attributeInt(plane, "TheT");
            if (c >= sourceChannels || t >= sourceTimes || attributeInt(plane, "TheZ") != 0 || planes.put(t * sourceChannels + c, plane) != null)
                throw new IOException("Original source has duplicate or out-of-grid Plane metadata");
        }
        Set<Integer> seen = new HashSet<>();
        for (Element plane : children(view, "Plane")) {
            int v = attributeInt(plane, "TheC");
            Element originalPlane = planes.get(v);
            if (originalPlane == null || !seen.add(v) || attributeInt(plane, "TheT") != 0 || attributeInt(plane, "TheZ") != 0 ||
                    !attributes(plane, Set.of("TheC", "TheT", "TheZ")).equals(attributes(originalPlane, Set.of("TheC", "TheT", "TheZ"))))
                throw new IOException("Acquired-plane metadata differs from its exact original C/T/Z plane");
        }
        if (seen.size() != planes.size()) throw new IOException("Acquired-plane metadata omitted original Plane facts");
        return view;
    }

    private void validateGeometry(ImageServer<BufferedImage> source, JsonArray resolutions, Element pixels) throws IOException {
        if (source.isRGB() || source.nChannels() != sourceChannels || source.nTimepoints() != sourceTimes || source.nZSlices() != 1 ||
                resolutions == null || resolutions.isEmpty() || resolutions.size() != source.getMetadata().getLevels().size() ||
                source.getWidth() != attributeInt(pixels, "SizeX") || source.getHeight() != attributeInt(pixels, "SizeY") ||
                source.getPixelType() != omeType(pixels.getAttributeValue("Type", "")))
            throw new IOException("Original selected series geometry differs from inspection");
        validatePhysicalSize(pixels, "X", source.getPixelCalibration().getPixelWidthMicrons());
        validatePhysicalSize(pixels, "Y", source.getPixelCalibration().getPixelHeightMicrons());
        for (int i = 0; i < resolutions.size(); i++) {
            JsonObject level = resolutions.get(i).getAsJsonObject();
            fields(level, "index", "size_x", "size_y", "size_z", "size_c", "size_t", "rgb_samples", "dtype", "dimension_order", "is_thumbnail");
            var actual = source.getMetadata().getLevels().get(i);
            if (integer(level, "index") != i || integer(level, "size_x") != actual.getWidth() || integer(level, "size_y") != actual.getHeight() ||
                    integer(level, "size_z") != 1 || integer(level, "size_c") != sourceChannels || integer(level, "size_t") != sourceTimes ||
                    integer(level, "rgb_samples") != 1 || numpyType(string(level, "dtype")) != source.getPixelType() ||
                    !level.get("is_thumbnail").isJsonPrimitive() || !level.getAsJsonPrimitive("is_thumbnail").isBoolean() || level.get("is_thumbnail").getAsBoolean() ||
                    !Set.of("XYZCT", "XYZTC", "XYCZT", "XYCTZ", "XYTCZ", "XYTZC").contains(string(level, "dimension_order")))
                throw new IOException("Original selected series resolution differs from inspection: " + i);
        }
    }

    private static void validatePhysicalSize(Element pixels, String axis, double actual) throws IOException {
        String size = pixels.getAttributeValue("PhysicalSize" + axis);
        if (size == null) return;
        try {
            double expected = UnitsLength.create(Double.parseDouble(size),
                    UnitsLength.fromString(pixels.getAttributeValue("PhysicalSize" + axis + "Unit", "µm"))).value(UNITS.MICROMETER).doubleValue();
            if (!Double.isFinite(expected) || expected <= 0 || !Double.isFinite(actual) ||
                    Math.abs(actual - expected) > Math.max(1e-9, Math.abs(expected) * 1e-9))
                throw new IOException("Original source physical calibration differs from inspection: " + axis);
        } catch (IllegalArgumentException | ome.xml.model.enums.EnumerationException error) {
            throw new IOException("Unsupported original source physical calibration: " + axis, error);
        }
    }

    private static PixelType numpyType(String dtype) throws IOException {
        if (!dtype.matches("[<>=|][uif][1248]")) throw new IOException("Unsupported inspected numerical type");
        return switch (dtype.substring(1)) {
            case "u1" -> PixelType.UINT8; case "i1" -> PixelType.INT8;
            case "u2" -> PixelType.UINT16; case "i2" -> PixelType.INT16;
            case "u4" -> PixelType.UINT32; case "i4" -> PixelType.INT32;
            case "f4" -> PixelType.FLOAT32; case "f8" -> PixelType.FLOAT64;
            default -> throw new IOException("Unsupported inspected numerical type");
        };
    }

    private static PixelType omeType(String type) throws IOException {
        return switch (type) {
            case "uint8" -> PixelType.UINT8; case "int8" -> PixelType.INT8;
            case "uint16" -> PixelType.UINT16; case "int16" -> PixelType.INT16;
            case "uint32" -> PixelType.UINT32; case "int32" -> PixelType.INT32;
            case "float" -> PixelType.FLOAT32; case "double" -> PixelType.FLOAT64;
            default -> throw new IOException("Unsupported OME numerical type");
        };
    }

    /** Also used by reference children before their own cached reads. */
    synchronized void validateSources() throws IOException {
        if (closed) throw new IOException("Acquired-plane view is closed");
        AlignedSourceCollectionServer.validateSourceIdentity(parentPath, parentIdentity);
        var actual = Files.readAttributes(descriptor, BasicFileAttributes.class);
        if (!actual.isRegularFile() || Files.isSymbolicLink(descriptor) || actual.size() != descriptorIdentity.size() ||
                !actual.lastModifiedTime().equals(descriptorIdentity.lastModifiedTime()) || !Objects.equals(actual.fileKey(), descriptorIdentity.fileKey()) ||
                !descriptorDigest.equals(fileDigest(descriptor)))
            throw new IOException("Acquired-plane organization changed; reopen the inspected source");
    }

    @Override public BufferedImage readRegion(RegionRequest request) throws IOException {
        validateSources();
        if (request.getZ() != 0 || request.getT() != 0) throw new IOException("Acquired-plane view has only virtual T=0 and Z=0");
        // Do not hold the reader lock while QuPath waits for a pending cached tile.
        BufferedImage result = super.readRegion(request);
        validateSources();
        return result;
    }

    @Override protected synchronized BufferedImage readTile(TileRequest request) throws IOException {
        validateSources();
        if (request.getZ() != 0 || request.getT() != 0) throw new IOException("Acquired-plane view has only virtual T=0 and Z=0");
        int width = request.getTileWidth(), height = request.getTileHeight();
        int type = switch (getPixelType()) {
            case UINT8 -> DataBuffer.TYPE_BYTE; case INT8, INT16 -> DataBuffer.TYPE_SHORT;
            case UINT16 -> DataBuffer.TYPE_USHORT; case INT32 -> DataBuffer.TYPE_INT;
            case UINT32, FLOAT64 -> DataBuffer.TYPE_DOUBLE; case FLOAT32 -> DataBuffer.TYPE_FLOAT;
        };
        var sample = new BandedSampleModel(type, width, height, sourceChannels * sourceTimes);
        var raster = Raster.createWritableRaster(sample, sample.createDataBuffer(), null);
        for (int t = 0; t < sourceTimes; t++) {
            validateSources();
            // The original decoder owns OME DimensionOrder and explicit TIFF mappings.
            var region = request.getRegionRequest().updatePath(parent.getPath()).updateZ(0).updateT(t);
            var tile = parent.readRegion(region);
            if (tile == null || tile.getWidth() != width || tile.getHeight() != height || tile.getRaster().getNumBands() != sourceChannels)
                throw new IOException("Original source decoder returned an incompatible acquired-plane tile");
            int[] bands = new int[sourceChannels];
            for (int c = 0; c < sourceChannels; c++) bands[c] = t * sourceChannels + c;
            raster.createWritableChild(0, 0, width, height, 0, 0, bands).setRect(tile.getRaster());
            validateSources();
        }
        return new BufferedImage(ColorModelFactory.createColorModel(getPixelType(), original.getChannels()), raster, false, null);
    }

    @Override public ImageServerMetadata getOriginalMetadata() { return original; }
    @Override public String getServerType() { return "MIFflow acquired source planes"; }
    @Override public Collection<URI> getURIs() { return List.of(uri); }
    @Override protected String createID() { return getClass().getName() + ":" + uri; }
    @Override protected ImageServerBuilder.ServerBuilder<BufferedImage> createServerBuilder() {
        return ImageServerBuilder.DefaultImageServerBuilder.createInstance(AcquiredPlaneViewServerBuilder.class, original, uri, args);
    }
    @Override public synchronized void close() throws Exception {
        if (closed) return;
        closed = true;
        try { parent.close(); } finally { super.close(); }
    }

    private static void fields(JsonObject value, String... fields) throws IOException {
        if (value == null || !value.keySet().equals(Set.of(fields))) throw new IOException("Invalid acquired-plane description fields");
    }
    private static String string(JsonObject value, String field) throws IOException {
        JsonElement item = value.get(field);
        if (item == null || !item.isJsonPrimitive() || !item.getAsJsonPrimitive().isString()) throw new IOException("Expected text for " + field);
        return item.getAsString();
    }
    private static String nonnegative(JsonElement value, String field) throws IOException {
        if (value == null || !value.isJsonPrimitive() || !value.getAsJsonPrimitive().isNumber() || !value.toString().matches("0|[1-9][0-9]*"))
            throw new IOException("Expected a nonnegative integer for " + field);
        return value.toString();
    }
    private static int integer(JsonObject value, String field) throws IOException {
        try { return Integer.parseInt(nonnegative(value.get(field), field)); }
        catch (NumberFormatException error) { throw new IOException("Integer is too large: " + field, error); }
    }
    private static int attributeInt(Element value, String field) throws IOException {
        String text = value.getAttributeValue(field, "");
        if (!text.matches("0|[1-9][0-9]*")) throw new IOException("Invalid OME coordinate or size: " + field);
        try { return Integer.parseInt(text); } catch (NumberFormatException error) { throw new IOException("OME coordinate is too large", error); }
    }
    private static String digest(byte[] bytes) throws Exception { return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes)); }
    private static String fileDigest(Path path) throws IOException {
        try (var stream = Files.newInputStream(path)) {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            byte[] buffer = new byte[1024 * 1024];
            for (int count; (count = stream.read(buffer)) >= 0;) digest.update(buffer, 0, count);
            return HexFormat.of().formatHex(digest.digest());
        } catch (java.security.NoSuchAlgorithmException error) { throw new IOException(error); }
    }
    private static JsonObject parseObject(String text) throws IOException {
        try (var reader = new JsonReader(new StringReader(text))) {
            reader.setStrictness(Strictness.STRICT);
            JsonElement result = parse(reader, 0);
            if (!result.isJsonObject() || reader.peek() != JsonToken.END_DOCUMENT) throw new IOException("Expected one JSON object");
            return result.getAsJsonObject();
        }
    }
    private static JsonElement parse(JsonReader reader, int depth) throws IOException {
        if (depth > 64) throw new IOException("Acquired-plane JSON nesting is too deep");
        switch (reader.peek()) {
            case BEGIN_OBJECT:
                JsonObject object = new JsonObject(); reader.beginObject();
                while (reader.hasNext()) {
                    String key = reader.nextName();
                    if (object.has(key)) throw new IOException("Repeated acquired-plane JSON field: " + key);
                    object.add(key, parse(reader, depth + 1));
                }
                reader.endObject(); return object;
            case BEGIN_ARRAY:
                JsonArray array = new JsonArray(); reader.beginArray();
                while (reader.hasNext()) array.add(parse(reader, depth + 1));
                reader.endArray(); return array;
            case STRING: return new JsonPrimitive(reader.nextString());
            case NUMBER: return JsonParser.parseString(reader.nextString());
            case BOOLEAN: return new JsonPrimitive(reader.nextBoolean());
            case NULL: reader.nextNull(); return JsonNull.INSTANCE;
            default: throw new IOException("Invalid acquired-plane JSON value");
        }
    }
    private static String canonical(JsonElement value) {
        if (value.isJsonObject()) return "{" + String.join(",", value.getAsJsonObject().keySet().stream().sorted()
                .map(key -> quote(key) + ":" + canonical(value.getAsJsonObject().get(key))).toList()) + "}";
        if (value.isJsonArray()) {
            List<String> values = new ArrayList<>();
            for (JsonElement item : value.getAsJsonArray()) values.add(canonical(item));
            return "[" + String.join(",", values) + "]";
        }
        return value.isJsonPrimitive() && value.getAsJsonPrimitive().isString() ? quote(value.getAsString()) : value.toString();
    }
    private static String quote(String value) {
        StringBuilder result = new StringBuilder("\"");
        for (int i = 0; i < value.length(); i++) {
            char c = value.charAt(i);
            switch (c) {
                case '"' -> result.append("\\\""); case '\\' -> result.append("\\\\");
                case '\b' -> result.append("\\b"); case '\f' -> result.append("\\f");
                case '\n' -> result.append("\\n"); case '\r' -> result.append("\\r"); case '\t' -> result.append("\\t");
                default -> { if (c < 32) result.append(String.format("\\u%04x", (int)c)); else result.append(c); }
            }
        }
        return result.append('"').toString();
    }
    private static Element xml(String text) throws Exception {
        var builder = new SAXBuilder(XMLReaders.NONVALIDATING);
        builder.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true);
        builder.setFeature("http://xml.org/sax/features/external-general-entities", false);
        builder.setFeature("http://xml.org/sax/features/external-parameter-entities", false);
        builder.setExpandEntities(false);
        Element root = builder.build(new StringReader(text)).getRootElement();
        if (!"OME".equals(root.getName())) throw new IOException("Expected original OME metadata");
        return root;
    }
    private static List<Element> children(Element element, String name) {
        return element.getChildren().stream().filter(child -> name.equals(child.getName())).toList();
    }
    private static Element onlyChild(Element element, String name) throws IOException {
        var result = children(element, name);
        if (result.size() != 1) throw new IOException("Expected one OME " + name);
        return result.get(0);
    }
    private static Map<String, String> attributes(Element element, Set<String> omitted) {
        Map<String, String> result = new HashMap<>();
        for (var item : element.getAttributes())
            if (!omitted.contains(item.getQualifiedName())) result.put(item.getQualifiedName(), item.getValue());
        return result;
    }
    private static void rejectLinkedModulo(Element ome, Element image, Element pixels) throws IOException {
        Set<String> refs = new HashSet<>();
        for (Element parent : List.of(image, pixels)) for (Element ref : children(parent, "AnnotationRef")) refs.add(ref.getAttributeValue("ID", ""));
        for (Element annotation : ome.getDescendants(new ElementFilter("XMLAnnotation"))) {
            if (!refs.contains(annotation.getAttributeValue("ID", ""))) continue;
            for (String axis : List.of("C", "T", "Z")) {
                for (Element modulo : annotation.getDescendants(new ElementFilter("ModuloAlong" + axis))) {
                    int count = children(modulo, "Label").size();
                    if (count == 0) try {
                        BigDecimal start = new BigDecimal(modulo.getAttributeValue("Start", "")), end = new BigDecimal(modulo.getAttributeValue("End", "")),
                                step = new BigDecimal(modulo.getAttributeValue("Step", ""));
                        if (step.signum() <= 0) throw new ArithmeticException();
                        count = end.subtract(start).divide(step).add(BigDecimal.ONE).intValueExact();
                    } catch (NumberFormatException | ArithmeticException error) { throw new IOException("Linked OME Modulo has invalid cardinality", error); }
                    if (count <= 0 || attributeInt(pixels, "Size" + axis) % count != 0)
                        throw new IOException("Linked OME Modulo cardinality does not divide its source axis");
                    throw new IOException("Linked OME Modulo acquisition meaning is unsupported; export explicit registered cycle/channel planes");
                }
            }
        }
    }

    private static void rejectChannelCycles(Element ome, List<Element> channels) throws IOException {
        Set<String> refs = new HashSet<>();
        for (Element channel : channels) for (Element ref : children(channel, "AnnotationRef")) refs.add(ref.getAttributeValue("ID", ""));
        for (Element annotation : ome.getDescendants(new ElementFilter("MapAnnotation"))) {
            if (!refs.contains(annotation.getAttributeValue("ID", ""))) continue;
            String namespace = annotation.getAttributeValue("Namespace", "");
            if ("urn:mif-pipeline:channel-cycle-review:v1".equals(namespace))
                throw new IOException("Channel-bound cycles conflict with the simple T-as-cycles layout");
            if ("urn:mif-pipeline:source-channel:v1".equals(namespace))
                for (Element entry : annotation.getDescendants(new ElementFilter("M")))
                    if ("imaging_cycle".equals(entry.getAttributeValue("K", "")))
                        throw new IOException("Channel-bound cycles conflict with the simple T-as-cycles layout");
        }
    }
}
