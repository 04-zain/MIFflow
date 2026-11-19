package org.mif.image;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.awt.image.BufferedImage;
import java.io.IOException;
import java.net.URI;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.BasicFileAttributes;
import java.util.Arrays;
import java.util.Collection;
import java.util.List;
import java.util.Set;
import qupath.lib.color.ColorModelFactory;
import qupath.lib.images.servers.AbstractTileableImageServer;
import qupath.lib.images.servers.ImageServer;
import qupath.lib.images.servers.ImageServerBuilder;
import qupath.lib.images.servers.ImageServerMetadata;
import qupath.lib.images.servers.ImageServers;
import qupath.lib.images.servers.TileRequest;
import qupath.lib.regions.RegionRequest;

/** Exact channel selection over the existing original-image decoder. */
public final class ReferenceChannelViewServer extends AbstractTileableImageServer {
    private final URI uri;
    private final String[] args;
    private final Path parentPath;
    private final JsonObject parentIdentity;
    private final BasicFileAttributes descriptorIdentity;
    private final ImageServer<BufferedImage> parent;
    private final ImageServerMetadata original;
    private final int[] channels;
    private boolean closed;

    public ReferenceChannelViewServer(URI uri, String... args) throws IOException {
        this.uri = uri; this.args = args.clone();
        Path descriptor = Path.of(uri);
        descriptorIdentity = Files.readAttributes(descriptor, BasicFileAttributes.class);
        if (!descriptorIdentity.isRegularFile() || descriptorIdentity.size() > 32L * 1024 * 1024 || Files.isSymbolicLink(descriptor))
            throw new IOException("Invalid reference channel description");
        ImageServer<BufferedImage> opened = null;
        try {
            var root = JsonParser.parseString(Files.readString(descriptor)).getAsJsonObject();
            if (!root.keySet().equals(Set.of("schema", "parent_inspection", "reviewed_ome_xml", "source_channel_indices")) ||
                    !"mif-reference-channel-view/v1".equals(root.get("schema").getAsString()))
                throw new IOException("Unsupported reference channel description");
            var inspection = JsonParser.parseString(root.get("parent_inspection").getAsString()).getAsJsonObject();
            if (!"mif-inspected-source-authority".equals(inspection.get("schema_name").getAsString()))
                throw new IOException("Reference channels require an inspected parent image");
            parentIdentity = inspection.getAsJsonObject("source_identity");
            parentPath = Path.of(parentIdentity.get("resolved_path").getAsString());
            if (!parentPath.isAbsolute() || parentPath.toString().toLowerCase(java.util.Locale.ROOT).endsWith(".mif-channel-view.json"))
                throw new IOException("Reference channel views cannot contain other views");
            AlignedSourceCollectionServer.validateSourceIdentity(parentPath, parentIdentity);
            int series = inspection.getAsJsonObject("selection").get("source_series_index").getAsInt();
            if (series < 0) throw new IOException("Invalid reference parent image selection");
            var support = ImageServers.getImageSupport(parentPath.toUri(), "--series", Integer.toString(series));
            if (support == null || support.getBuilders().size() != 1) throw new IOException("Reference view requires one original image");
            opened = support.getBuilders().get(0).build();
            if (opened.isRGB() || opened.nZSlices() != 1 || opened.nTimepoints() != 1)
                throw new IOException("Reference views require scalar channels with one Z-plane and one time point");
            var selected = root.getAsJsonArray("source_channel_indices");
            if (selected.isEmpty() || selected.size() > opened.nChannels()) throw new IOException("Select valid reference channels");
            channels = new int[selected.size()];
            for (int i = 0; i < channels.length; i++) {
                String text = selected.get(i).toString();
                if (!text.matches("0|[1-9][0-9]*")) throw new IOException("Invalid reference channel index");
                channels[i] = Integer.parseInt(text);
                if (channels[i] >= opened.nChannels()) throw new IOException("Reference channel is outside its source image");
            }
            if (Arrays.stream(channels).distinct().count() != channels.length) throw new IOException("Reference channels cannot repeat");
            var metadata = opened.getMetadata();
            original = new ImageServerMetadata.Builder(getClass(), uri.toString(), opened.getWidth(), opened.getHeight())
                    .name(metadata.getName()).rgb(false).pixelType(metadata.getPixelType()).sizeZ(1).sizeT(1)
                    .channels(Arrays.stream(channels).mapToObj(c -> metadata.getChannels().get(c)).toList())
                    .pixelSizeMicrons(metadata.getPixelCalibration().getPixelWidthMicrons(), metadata.getPixelCalibration().getPixelHeightMicrons())
                    .levels(metadata.getLevels()).preferredTileSize(256, 256).build();
            parent = opened;
            validateSources();
        } catch (Exception error) {
            if (opened != null) try { opened.close(); } catch (Exception closeError) { error.addSuppressed(closeError); }
            throw new IOException("Cannot open reference channels: " + error.getMessage(), error);
        }
    }

    private synchronized void validateSources() throws IOException {
        if (closed) throw new IOException("Reference channel view is closed");
        AlignedSourceCollectionServer.validateSourceIdentity(parentPath, parentIdentity);
        if (parent instanceof AcquiredPlaneViewServer acquired) acquired.validateSources();
        var actual = Files.readAttributes(Path.of(uri), BasicFileAttributes.class);
        if (actual.size() != descriptorIdentity.size() || !actual.lastModifiedTime().equals(descriptorIdentity.lastModifiedTime()) ||
                !java.util.Objects.equals(actual.fileKey(), descriptorIdentity.fileKey()))
            throw new IOException("Reference channel selection changed; reopen the inspected source");
    }

    @Override public BufferedImage readRegion(RegionRequest request) throws IOException {
        // Cached regions bypass readTile; do not retain the reader lock while
        // QuPath waits for another thread's pending tile.
        validateSources();
        var image = super.readRegion(request);
        validateSources();
        return image;
    }

    @Override protected synchronized BufferedImage readTile(TileRequest request) throws IOException {
        validateSources();
        var tile = parent.readRegion(request.getRegionRequest().updatePath(parent.getPath()));
        if (tile == null) throw new IOException("The original image did not return the requested reference region");
        var raster = tile.getRaster().createWritableChild(0, 0, tile.getWidth(), tile.getHeight(), 0, 0, channels);
        validateSources();
        return new BufferedImage(ColorModelFactory.createColorModel(getPixelType(), original.getChannels()), raster, false, null);
    }

    @Override public ImageServerMetadata getOriginalMetadata() { return original; }
    @Override public String getServerType() { return "MIFflow reference channel selection"; }
    @Override public Collection<URI> getURIs() { return List.of(uri); }
    @Override protected String createID() { return getClass().getName() + ":" + uri; }
    @Override protected ImageServerBuilder.ServerBuilder<BufferedImage> createServerBuilder() {
        return ImageServerBuilder.DefaultImageServerBuilder.createInstance(ReferenceChannelViewServerBuilder.class, original, uri, args);
    }
    @Override public synchronized void close() throws Exception {
        if (closed) return;
        closed = true;
        try { parent.close(); } finally { super.close(); }
    }
}
