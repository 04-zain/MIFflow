package org.mif.image;

import java.awt.image.BufferedImage;
import java.io.IOException;
import java.net.URI;
import java.nio.file.Files;
import java.nio.file.Path;
import qupath.lib.images.servers.ImageServer;
import qupath.lib.images.servers.ImageServerBuilder;

/** Discovers immutable reviewed views of exact original acquisition planes. */
public final class AcquiredPlaneViewServerBuilder implements ImageServerBuilder<BufferedImage> {
    @Override public String getName() { return "MIFflow acquired source planes"; }
    @Override public String getDescription() { return "Reviewed registered source C/T/Z planes exposed as scalar channels"; }
    @Override public Class<BufferedImage> getImageType() { return BufferedImage.class; }
    @Override public ImageServer<BufferedImage> buildServer(URI uri, String... args) throws IOException {
        return new AcquiredPlaneViewServer(uri, args);
    }
    @Override public UriImageSupport<BufferedImage> checkImageSupport(URI uri, String... args) throws IOException {
        if (!"file".equalsIgnoreCase(uri.getScheme()) ||
                !uri.getPath().toLowerCase(java.util.Locale.ROOT).endsWith(".mif-plane-view.json") ||
                !Files.isRegularFile(Path.of(uri))) return null;
        return UriImageSupport.createInstance(getClass(), 5f,
                DefaultImageServerBuilder.createInstance(getClass(), uri, args));
    }
}
