package org.mif.cells;

import java.io.IOException;
import java.util.concurrent.CompletableFuture;
import java.util.function.Consumer;
import org.locationtech.jts.geom.Envelope;
import qupath.lib.images.ImageData;
import qupath.lib.objects.PathObject;

/** The image view and explicit whole-population commands share this bounded source. */
public interface NativeCellSource {
    CompletableFuture<CellDatasetProvider.Snapshot> request(Envelope bounds);
    CompletableFuture<PathObject> lookupAsync(String id);
    void forEach(Consumer<PathObject> consumer)throws IOException;
    void recordEdit(ImageData<?> image,PathObject cell,boolean deleted);
    boolean ownsPopulation(String population);
}
