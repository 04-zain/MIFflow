package org.mif.image;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.awt.image.BandedSampleModel;
import java.awt.image.BufferedImage;
import java.awt.image.DataBuffer;
import java.awt.image.Raster;
import java.io.IOException;
import java.net.URI;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.BasicFileAttributes;
import java.util.ArrayList;
import java.util.Collection;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.TimeUnit;
import qupath.lib.color.ColorModelFactory;
import qupath.lib.images.servers.AbstractTileableImageServer;
import qupath.lib.images.servers.ImageChannel;
import qupath.lib.images.servers.ImageServer;
import qupath.lib.images.servers.ImageServerBuilder;
import qupath.lib.images.servers.ImageServerMetadata;
import qupath.lib.images.servers.ImageServers;
import qupath.lib.images.servers.TileRequest;
import qupath.lib.regions.RegionRequest;

/**
 * Presents already registered source files as one scalar image. Each requested
 * tile is read with QuPath's existing member decoder. Only one member decoder
 * stays open; headers and the small collection description are retained.
 */
public final class AlignedSourceCollectionServer extends AbstractTileableImageServer {
    private record Member(Path path, int series, int channels, JsonObject identity) {}
    private record Channel(int member, int channel) {}
    private final URI uri;
    private final String[] args;
    private final List<Member> members = new ArrayList<>();
    private final List<Channel> channels = new ArrayList<>();
    private final List<ImageServerBuilder.ServerBuilder<BufferedImage>> builders = new ArrayList<>();
    private final ImageServerMetadata original;
    private final BasicFileAttributes descriptorIdentity;
    private final CycleRegistrationGeometry registration;
    private ImageServer<BufferedImage> reader;
    private int readerIndex = -1;
    private boolean closed;

    public AlignedSourceCollectionServer(URI uri, String... args) throws IOException {
        this.uri = uri;
        this.args = args.clone();
        Path descriptor = Path.of(uri);
        descriptorIdentity = Files.readAttributes(descriptor, BasicFileAttributes.class);
        if (!descriptorIdentity.isRegularFile() || descriptorIdentity.size() > 1024 * 1024 || Files.isSymbolicLink(descriptor))
            throw new IOException("The aligned image description must be a regular file of at most 1 MiB");
        try {
            JsonObject root = JsonParser.parseString(Files.readString(descriptor)).getAsJsonObject();
            boolean registered = integer(root, "schema_version") == 2;
            if (registered) fields(root, "schema_name", "schema_version", "name", "alignment", "members", "channels", "registration");
            else fields(root, "schema_name", "schema_version", "name", "alignment", "members", "channels");
            if (!"mif-aligned-source-collection".equals(root.get("schema_name").getAsString()) ||
                    (registered ? !"registered_rigid".equals(root.get("alignment").getAsString()) :
                    integer(root, "schema_version") != 1 || !"already_registered".equals(root.get("alignment").getAsString())))
                throw new IOException("Unsupported aligned image collection");
            for (var item : root.getAsJsonArray("members")) {
                JsonObject m = item.getAsJsonObject();
                fields(m, "path", "image_index", "series_index", "file_sha256", "channel_count", "identity");
                Path path = Path.of(m.get("path").getAsString());
                if (!path.isAbsolute() || !path.normalize().equals(path) || path.toString().toLowerCase(java.util.Locale.ROOT).endsWith(".mif-source.json"))
                    throw new IOException("Collection members require absolute original-image paths");
                integer(m, "image_index");
                int count = integer(m, "channel_count");
                if (count < 1 || count > 4096) throw new IOException("Invalid collection member channel count");
                JsonObject identity = m.getAsJsonObject("identity");
                fields(identity, "resolved_path", "file_sha256", "size_bytes", "modified_time_ns", "device_id", "inode_id");
                String sha = m.get("file_sha256").getAsString();
                if (!sha.matches("[0-9a-f]{64}") || !sha.equals(identity.get("file_sha256").getAsString()) ||
                        !path.equals(Path.of(identity.get("resolved_path").getAsString())))
                    throw new IOException("Collection member identity differs from its inspected image");
                members.add(new Member(path, integer(m, "series_index"), count, identity));
            }
            if (members.size() < 2 || members.size() > 256) throw new IOException("Select 2 to 256 aligned images");
            Set<String> used = new HashSet<>();
            for (var item : root.getAsJsonArray("channels")) {
                JsonObject c = item.getAsJsonObject();
                fields(c, "member_index", "channel_index", "imaging_cycle");
                integer(c, "imaging_cycle");
                int m = integer(c, "member_index"), channel = integer(c, "channel_index");
                if (m >= members.size() || channel >= members.get(m).channels || !used.add(m + ":" + channel))
                    throw new IOException("Collection channel mapping is invalid or repeated");
                channels.add(new Channel(m, channel));
            }
            if (channels.isEmpty() || channels.size() > 4096 || channels.size() != members.stream().mapToInt(Member::channels).sum())
                throw new IOException("The collection must contain every selected member channel exactly once");
            List<ImageServerMetadata> headers = new ArrayList<>();
            for (Member member : members) {
                validateMember(member);
                var support = ImageServers.getImageSupport(member.path.toUri(), "--series", Integer.toString(member.series));
                if (support == null || support.getBuilders().size() != 1)
                    throw new IOException("Select exactly one source image for each collection member: " + member.path.getFileName());
                var builder = support.getBuilders().get(0);
                builders.add(builder);
                try (var source = builder.build()) {
                    var metadata = source.getMetadata();
                    if (source.isRGB() || source.nZSlices() != 1 || source.nTimepoints() != 1 || source.nChannels() != member.channels)
                        throw new IOException("Aligned inputs require scalar channels with one Z-plane and one time point");
                    if (!metadata.getPixelCalibration().hasPixelSizeMicrons())
                        throw new IOException("Aligned inputs require recorded physical pixel sizes");
                    if (!headers.isEmpty()) {
                        if (!registered) checkGeometry(headers.get(0), metadata);
                        else if (headers.get(0).getPixelType() != metadata.getPixelType())
                            throw new IOException("Registered cycle sources require matching original numerical types");
                    }
                    headers.add(metadata);
                }
                validateMember(member);
            }
            registration = registered ? new CycleRegistrationGeometry(root, headers) : null;
            var first = headers.get(registration == null ? 0 : registration.reference);
            int width = registration == null ? first.getWidth() : registration.width;
            int height = registration == null ? first.getHeight() : registration.height;
            var levels = new ImageServerMetadata.ImageResolutionLevel.Builder(width, height);
            for (var level : first.getLevels()) levels.addLevel(level.getDownsample(),
                    (int)Math.ceil(width/level.getDownsample()), (int)Math.ceil(height/level.getDownsample()));
            List<ImageChannel> ordered = channels.stream().map(c -> headers.get(c.member).getChannels().get(c.channel)).toList();
            original = new ImageServerMetadata.Builder(getClass(), uri.toString(), width, height)
                    .name(root.get("name").getAsString()).rgb(false).pixelType(registration == null ? first.getPixelType() : qupath.lib.images.servers.PixelType.FLOAT32)
                    .channels(ordered).sizeZ(1).sizeT(1).levels(registration == null ? first.getLevels() : levels.build())
                    .pixelSizeMicrons(first.getPixelCalibration().getPixelWidthMicrons(), first.getPixelCalibration().getPixelHeightMicrons())
                    .preferredTileSize(256, 256).build();
            validateDescriptor();
        } catch (Exception error) {
            throw new IOException("Cannot open the aligned image collection: " + error.getMessage(), error);
        }
    }

    static void fields(JsonObject value, String... names) throws IOException {
        if (value == null || !value.keySet().equals(Set.of(names))) throw new IOException("Invalid aligned image description fields");
    }

    static int integer(JsonObject value, String key) throws IOException {
        String text = value.get(key).toString();
        if (!text.matches("0|[1-9][0-9]*")) throw new IOException("Invalid collection index: " + key);
        try { return Integer.parseInt(text); } catch (NumberFormatException e) { throw new IOException("Collection index is too large: " + key, e); }
    }

    private static void checkGeometry(ImageServerMetadata first, ImageServerMetadata next) throws IOException {
        var a = first.getPixelCalibration(); var b = next.getPixelCalibration();
        if (first.getWidth() != next.getWidth() || first.getHeight() != next.getHeight() || first.getPixelType() != next.getPixelType() ||
                Math.abs(a.getPixelWidthMicrons() - b.getPixelWidthMicrons()) > 1e-9 ||
                Math.abs(a.getPixelHeightMicrons() - b.getPixelHeightMicrons()) > 1e-9)
            throw new IOException("Aligned inputs must use the same image dimensions, physical pixel sizes and numerical type");
    }

    private static void validateMember(Member member) throws IOException {
        validateSourceIdentity(member.path, member.identity);
    }

    static void validateSourceIdentity(Path source, JsonObject expected) throws IOException {
        Path path = source.toRealPath();
        if (!path.equals(source)) throw new IOException("Source image location changed; inspect it again: " + source.getFileName());
        Path record = Files.isDirectory(path) ? path.resolve("zarr.json") : path;
        var actual = Files.readAttributes(record, BasicFileAttributes.class);
        boolean changed = !actual.isRegularFile() || actual.size() != expected.get("size_bytes").getAsLong() ||
                actual.lastModifiedTime().to(TimeUnit.NANOSECONDS) != expected.get("modified_time_ns").getAsLong();
        if (Files.getFileStore(record).supportsFileAttributeView("unix")) {
            var unix = Files.readAttributes(record, "unix:dev,ino");
            changed |= ((Number)unix.get("dev")).longValue() != expected.get("device_id").getAsLong() ||
                    ((Number)unix.get("ino")).longValue() != expected.get("inode_id").getAsLong();
        }
        if (changed) throw new IOException("Source image changed after inspection; inspect it again: " + path.getFileName());
    }

    private void validateDescriptor() throws IOException {
        var actual = Files.readAttributes(Path.of(uri), BasicFileAttributes.class);
        if (actual.size() != descriptorIdentity.size() || !actual.lastModifiedTime().equals(descriptorIdentity.lastModifiedTime()) ||
                !java.util.Objects.equals(actual.fileKey(), descriptorIdentity.fileKey()))
            throw new IOException("The aligned image description changed; reopen the inspected source");
    }

    private void closeReader() throws IOException {
        if (reader == null) return;
        try { reader.close(); } catch (Exception e) { throw new IOException("Could not close the source image", e); }
        finally { reader = null; readerIndex = -1; }
    }

    private synchronized void validateReadableSources() throws IOException {
        if (closed) throw new IOException("The aligned image collection is closed");
        validateDescriptor();
        for (Member member : members) validateMember(member);
    }

    @Override public BufferedImage readRegion(RegionRequest request) throws IOException {
        // QuPath can return cached pixels without calling readTile. Validate at
        // this boundary too, without holding the reader lock while awaiting tiles.
        validateReadableSources();
        var image = super.readRegion(request);
        validateReadableSources();
        return image;
    }

    @Override protected synchronized BufferedImage readTile(TileRequest request) throws IOException {
        if (closed) throw new IOException("The aligned image collection is closed");
        validateDescriptor();
        if (request.getZ() != 0 || request.getT() != 0) throw new IOException("Aligned image collection has one Z-plane and time point");
        int width = request.getTileWidth(), height = request.getTileHeight();
        int type = switch (getPixelType()) {
            case UINT8 -> DataBuffer.TYPE_BYTE;
            case INT8, INT16 -> DataBuffer.TYPE_SHORT;
            case UINT16 -> DataBuffer.TYPE_USHORT;
            case INT32 -> DataBuffer.TYPE_INT;
            case UINT32, FLOAT64 -> DataBuffer.TYPE_DOUBLE;
            case FLOAT32 -> DataBuffer.TYPE_FLOAT;
        };
        var sample = new BandedSampleModel(type, width, height, channels.size());
        var raster = Raster.createWritableRaster(sample, sample.createDataBuffer(), null);
        for (int m = 0; m < members.size(); m++) {
            validateMember(members.get(m));
            if (readerIndex != m) {
                closeReader();
                try { reader = builders.get(m).build(); } catch (Exception e) { throw new IOException("Cannot reopen aligned source image", e); }
                readerIndex = m;
            }
            if (registration != null) {
                float[][] values = registration.read(reader, m, request.getRegionRequest(), width, height);
                for (int c = 0; c < channels.size(); c++) {
                    var mapping = channels.get(c);
                    if (mapping.member == m) raster.setSamples(0, 0, width, height, c, values[mapping.channel]);
                }
                validateMember(members.get(m));
                continue;
            }
            var tile = reader.readRegion(request.getRegionRequest().updatePath(reader.getPath()));
            if (tile == null || tile.getWidth() != width || tile.getHeight() != height || tile.getRaster().getNumBands() != members.get(m).channels)
                throw new IOException("Source decoder returned an incompatible aligned image tile");
            for (int c = 0; c < channels.size(); c++) {
                var mapping = channels.get(c);
                if (mapping.member == m) {
                    double[] values = tile.getRaster().getSamples(0, 0, width, height, mapping.channel, (double[])null);
                    raster.setSamples(0, 0, width, height, c, values);
                }
            }
            validateMember(members.get(m));
        }
        validateDescriptor();
        return new BufferedImage(ColorModelFactory.createColorModel(getPixelType(), original.getChannels()), raster, false, null);
    }

    @Override public ImageServerMetadata getOriginalMetadata() { return original; }
    @Override public String getServerType() { return "MIFflow aligned source images"; }
    @Override public Collection<URI> getURIs() { return List.of(uri); }
    @Override protected String createID() { return getClass().getName() + ":" + uri; }
    @Override protected ImageServerBuilder.ServerBuilder<BufferedImage> createServerBuilder() {
        return ImageServerBuilder.DefaultImageServerBuilder.createInstance(AlignedSourceCollectionServerBuilder.class, original, uri, args);
    }
    @Override public synchronized void close() throws Exception {
        if (closed) return;
        closed = true;
        try { closeReader(); } finally { super.close(); }
    }
}
