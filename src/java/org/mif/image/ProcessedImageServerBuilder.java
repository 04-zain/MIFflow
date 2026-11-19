package org.mif.image;

import java.awt.image.BufferedImage;
import java.io.IOException;
import java.net.URI;
import java.nio.file.Files;
import java.nio.file.Path;
import qupath.lib.images.servers.ImageServer;
import qupath.lib.images.servers.ImageServerBuilder;

/** Selects the direct mIF reader once when QuPath opens a v3 image directory. */
public final class ProcessedImageServerBuilder implements ImageServerBuilder<BufferedImage> {
    @Override public String getName() { return "mIF processed image (NGFF 0.5 / Zarr 3)"; }
    @Override public String getDescription() { return "Same-JVM indexed-shard image access with calibrated multichannel values"; }
    @Override public Class<BufferedImage> getImageType() { return BufferedImage.class; }
    @Override public ImageServer<BufferedImage> buildServer(URI uri, String... args) throws IOException {
        return new ProcessedImageServer(uri, args);
    }
    @Override public UriImageSupport<BufferedImage> checkImageSupport(URI uri, String... args) throws IOException {
        if (!"file".equalsIgnoreCase(uri.getScheme()) || !Files.isRegularFile(Path.of(uri).resolve("zarr.json")))
            return null;
        if (!ProcessedImageServer.isSupported(Path.of(uri))) return null;
        return UriImageSupport.createInstance(getClass(), 5f,
                DefaultImageServerBuilder.createInstance(getClass(), uri, args));
    }
}
