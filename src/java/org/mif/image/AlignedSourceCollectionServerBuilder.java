package org.mif.image;

import java.awt.image.BufferedImage;
import java.io.IOException;
import java.net.URI;
import java.nio.file.Files;
import java.nio.file.Path;
import qupath.lib.images.servers.ImageServer;
import qupath.lib.images.servers.ImageServerBuilder;

/** Opens the application's inspected collection through existing image decoders. */
public final class AlignedSourceCollectionServerBuilder implements ImageServerBuilder<BufferedImage> {
    @Override public String getName() { return "MIFflow aligned image collection"; }
    @Override public String getDescription() { return "Registered channels from the selected original images"; }
    @Override public Class<BufferedImage> getImageType() { return BufferedImage.class; }
    @Override public ImageServer<BufferedImage> buildServer(URI uri, String... args) throws IOException {
        return new AlignedSourceCollectionServer(uri, args);
    }
    @Override public UriImageSupport<BufferedImage> checkImageSupport(URI uri, String... args) throws IOException {
        if (!"file".equalsIgnoreCase(uri.getScheme()) ||
                !uri.getPath().toLowerCase(java.util.Locale.ROOT).endsWith(".mif-source.json") ||
                !Files.isRegularFile(Path.of(uri))) return null;
        return UriImageSupport.createInstance(getClass(), 5f,
                DefaultImageServerBuilder.createInstance(getClass(), uri, args));
    }
}
