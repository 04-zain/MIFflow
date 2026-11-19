package org.mif.cells;

import java.io.IOException;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.function.Consumer;
import org.locationtech.jts.geom.Envelope;
import qupath.lib.images.ImageData;
import qupath.lib.objects.PathObject;

/** Retain independent accepted section bases without copying their clean geometry. */
public final class NativeCellCollection implements NativeCellSource,AutoCloseable {
    private final List<CellDatasetProvider> sections;
    private volatile boolean closed;
    NativeCellCollection(List<CellDatasetProvider> sections){this.sections=List.copyOf(sections);}
    /** We compose prepared section views and retire their references with the shared process reservation. */
    @Override public CompletableFuture<CellDatasetProvider.Snapshot> request(Envelope bounds) {
        if(closed)return CompletableFuture.failedFuture(new IllegalStateException("Cell collection is closed"));
        if(sections.size()==1)return sections.getFirst().request(bounds);
        var requests=sections.stream().map(section->section.request(bounds)).toList();
        return CompletableFuture.allOf(requests.toArray(CompletableFuture[]::new)).thenApply(ignored->{
            var snapshots=requests.stream().map(CompletableFuture::join).toList();boolean overview=snapshots.stream().anyMatch(CellDatasetProvider.Snapshot::overview);long revision=0;
            var objects=new ArrayList<PathObject>();int[] density=overview?new int[4096]:null;
            for(var snapshot:snapshots) {
                revision=31*revision+snapshot.revision();
                if(!overview)objects.addAll(snapshot.objects());
                else if(snapshot.density()!=null)for(int i=0;i<density.length;i++)density[i]+=snapshot.density()[i];
                else for(var object:snapshot.objects()){double x=object.getROI().getCentroidX(),y=object.getROI().getCentroidY();if(bounds.covers(x,y)){int bx=(int)((x-bounds.getMinX())/Math.max(1,bounds.getWidth())*64),by=(int)((y-bounds.getMinY())/Math.max(1,bounds.getHeight())*64);density[Math.max(0,Math.min(63,by))*64+Math.max(0,Math.min(63,bx))]++;}}
            }
            long retained=256L+snapshots.size()*64L+objects.size()*8L+(density==null?0:density.length*4L);
            var result=new CellDatasetProvider.Snapshot(List.copyOf(objects),density,new Envelope(bounds),overview,revision,retained);
            Object key=new Object();SharedCellCache.put(this,key,result,retained,result::retire);
            result.onRetire(()->SharedCellCache.remove(this,key,result));
            snapshots.forEach(snapshot->snapshot.dependent(result));
            return result;
        });
    }
    @Override public CompletableFuture<PathObject> lookupAsync(String id) {
        var future=new CompletableFuture<PathObject>();try{SharedCellCache.execute(()->{try{if(closed)throw new IllegalStateException("Cell collection is closed");future.complete(lookup(id));}catch(Throwable error){future.completeExceptionally(error);}});}catch(java.util.concurrent.RejectedExecutionException error){future.completeExceptionally(error);}return future;
    }
    public PathObject lookup(String id)throws IOException {LocalParquet.requireWorker();for(var section:sections){var cell=section.lookup(id);if(cell!=null)return cell;}return null;}
    public long getRowCount(){return sections.stream().mapToLong(CellDatasetProvider::getRowCount).sum();}
    @Override public void forEach(Consumer<PathObject> consumer)throws IOException {LocalParquet.requireWorker();for(var section:sections)section.forEach(consumer);}
    public void forEachInBounds(Envelope bounds,Consumer<PathObject> consumer)throws IOException {LocalParquet.requireWorker();for(var section:sections)section.forEachInBounds(bounds,consumer);}
    @Override public void recordEdit(ImageData<?> image,PathObject cell,boolean deleted) {String population=cell.getMetadata().get("mif.population_id");for(var section:sections)if(section.ownsPopulation(population)){section.recordEdit(image,cell,deleted);return;}throw new IllegalArgumentException("Edited cell belongs to another accepted image population");}
    @Override public boolean ownsPopulation(String population){return sections.stream().anyMatch(section->section.ownsPopulation(population));}
    @Override public void close(){closed=true;SharedCellCache.release(this);}
}
