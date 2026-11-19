package org.mif.cells;

import java.io.IOException;
import java.nio.file.Path;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.List;
import java.util.WeakHashMap;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.function.Consumer;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import qupath.lib.images.ImageData;
import qupath.lib.objects.PathObject;

/** Independent accepted section bases; one aggregate bounded image selection. */
public final class NativeCellScope {
    private record Selected(String path,CompletableFuture<CellDatasetProvider> provider) {}
    private static final Map<ImageData<?>,Map<String,Selected>> sections=new WeakHashMap<>();
    private static final Map<ImageData<?>,CompletableFuture<NativeCellCollection>> selected=new WeakHashMap<>();
    private static final ExecutorService startup=Executors.newSingleThreadExecutor(task->{var thread=new Thread(task,"mif-cell-layout");thread.setDaemon(true);return thread;});
    // These aliases select the current automatic display. Namespaced phenotype
    // fields retain historical views independently of this selection.
    static final List<String> PHENOTYPE_DISPLAY_FIELDS=List.of("primary_phenotype","phenotype_source","phenotype_components","marker_states","phenotype_confidence","cluster_id","cluster_label","selected_phenotype_view","phenotyping_generation_id","phenotyping_run_id");
    private NativeCellScope() {}
    /** We read the saved section-to-base selection; an image without a selection has no implicit hierarchy population. */
    private static JsonObject bindings(ImageData<?> image) {
        Object value=image.getProperty(CellDatasetProvider.DATASET_PROPERTY);return value instanceof String text?JsonParser.parseString(text).getAsJsonObject():new JsonObject();
    }
    /**
     * We open the completed base selected for this section on the shared startup
     * worker. Its accepted receipt, scalar references and sparse checkpoint are
     * resolved before callers receive the provider. The image allowance is shared
     * among its sections; an absent section, mismatched receipt or invalid selected
     * result completes the future with an error instead of returning visible cells.
     */
    public static CompletableFuture<CellDatasetProvider> open(ImageData<?> image,String sectionId) {
        synchronized(sections) {
            var bindings=bindings(image);if(!bindings.has(sectionId))return CompletableFuture.failedFuture(new IllegalArgumentException("Image entry has no accepted cell base for section "+sectionId));String path=bindings.get(sectionId).getAsString();
            var owners=sections.computeIfAbsent(image,key->new LinkedHashMap<>());var previous=owners.get(sectionId);if(previous!=null&&previous.path.equals(path))return previous.provider;
            if(previous!=null)previous.provider.thenAccept(CellDatasetProvider::close);
            int count=bindings.size();long total=Long.parseLong(System.getenv().getOrDefault("MIF_OBJECT_CACHE_BYTES",Long.toString(Long.getLong("mif.cell.memoryBytes",64L*1024*1024))));long budget=total/Math.max(1,count);
            var provider=CompletableFuture.supplyAsync(()->{try{var owner=new CellDatasetProvider(Path.of(path),budget);Object selectedReceipt=image.getProperty("mif.segmentation_populations");if(selectedReceipt instanceof String receipts)owner.setAcceptedReceipt(JsonParser.parseString(receipts).getAsJsonObject().getAsJsonObject(sectionId));owner.selectComponents((String)image.getProperty(CellComponents.PROPERTY));owner.restoreEdits(image);return owner;}catch(IOException error){throw new java.util.concurrent.CompletionException(error);}},startup);
            owners.put(sectionId,new Selected(path,provider));return provider;
        }
    }
    /** We combine all accepted section providers without retaining their complete cells; an unbound image fails explicitly. */
    public static CompletableFuture<NativeCellCollection> open(ImageData<?> image) {
        synchronized(selected) {return selected.computeIfAbsent(image,key->{var bindings=bindings(image);if(bindings.size()==0)return CompletableFuture.failedFuture(new IllegalArgumentException("Image entry has no accepted cell bases"));var owners=new ArrayList<CompletableFuture<CellDatasetProvider>>();for(String section:bindings.keySet())owners.add(open(image,section));return CompletableFuture.allOf(owners.toArray(CompletableFuture[]::new)).thenApply(ignored->new NativeCellCollection(owners.stream().map(CompletableFuture::join).toList()));});}
    }
    /** We stream every accepted section off FX; viewport density and current hierarchy contents do not define scientific scope. */
    public static void forEach(ImageData<?> image,Consumer<PathObject> consumer)throws IOException {
        LocalParquet.requireWorker();open(image).join().forEach(consumer);
    }
    /** Bind one completed section generation; retain every other accepted base. */
    public static void bind(ImageData<?> image,String datasetPath,String sectionId) {
        if(sectionId==null||sectionId.isBlank())throw new IllegalArgumentException("Cell base binding requires its accepted section");
        var bindings=bindings(image);bindings.addProperty(sectionId,Path.of(datasetPath).toAbsolutePath().normalize().toString());close(image);image.setProperty(CellDatasetProvider.DATASET_PROPERTY,bindings.toString());
        // Result and edit references belong to population IDs. Unchanged sections
        // remain selected; replaced populations are ignored by their new base.
    }
    /** Select this field/scope result only; other sections and fields remain selected. */
    public static void selectComponent(ImageData<?> image,String descriptorPath)throws IOException {
        selectComponent(image,descriptorPath,false);
    }
    /** We replace numeric measurements and retire this section's automatic display, retaining historical views and sparse human edits. */
    public static void selectMeasurementComponent(ImageData<?> image,String descriptorPath)throws IOException {
        selectComponent(image,descriptorPath,true);
    }
    private static void selectComponent(ImageData<?> image,String descriptorPath,boolean retireDisplay)throws IOException {
        LocalParquet.requireWorker();Path path=Path.of(descriptorPath).toAbsolutePath().normalize();var result=JsonParser.parseString(Files.readString(path)).getAsJsonObject();
        String population=result.get("population_id").getAsString();CellDatasetProvider target=null;for(String section:bindings(image).keySet()){var provider=open(image,section).join();if(provider.ownsPopulation(population)){target=provider;break;}}
        if(!result.get("complete").getAsBoolean()||target==null)throw new IOException("Cannot select an incomplete result or an unselected population");
        Object stored=image.getProperty(CellComponents.PROPERTY);var references=stored instanceof String text?JsonParser.parseString(text).getAsJsonObject():new JsonObject();
        for(var field:result.getAsJsonObject("columns").entrySet()){var scopes=references.getAsJsonObject(field.getKey());if(scopes==null){scopes=new JsonObject();references.add(field.getKey(),scopes);}scopes.addProperty(result.get("scope_key").getAsString(),path.toString());}
        if(retireDisplay)for(String field:PHENOTYPE_DISPLAY_FIELDS){var scopes=references.getAsJsonObject(field);if(scopes!=null){scopes.remove(result.get("scope_key").getAsString());if(scopes.size()==0)references.remove(field);}}
        target.selectComponents(references.toString());image.setProperty(CellComponents.PROPERTY,references.toString());
    }
    /**
     * We select a completed phenotype view's namespace and optionally point its
     * display fields at the same typed columns. Shared mode/generation/run values
     * come from descriptor attributes. Other views, sections and display choices
     * remain selected; changing the display never rewrites the accepted table.
     */
    public static void selectPhenotypeComponent(ImageData<?> image,String descriptorPath,boolean materialize)throws IOException {
        LocalParquet.requireWorker();Path path=Path.of(descriptorPath).toAbsolutePath().normalize();var result=JsonParser.parseString(Files.readString(path)).getAsJsonObject();
        var attributes=result.getAsJsonObject("attributes");var identity=attributes==null?null:attributes.getAsJsonObject("phenotype_view");if(identity==null)throw new IOException("Selected result does not describe a phenotype view");
        selectComponent(image,descriptorPath);if(!materialize)return;
        var references=JsonParser.parseString((String)image.getProperty(CellComponents.PROPERTY)).getAsJsonObject();String section=result.get("scope_key").getAsString();
        for(String field:PHENOTYPE_DISPLAY_FIELDS){var scopes=references.getAsJsonObject(field);if(scopes==null){scopes=new JsonObject();references.add(field,scopes);}scopes.addProperty(section,path.toString());}
        var provider=open(image,section).join();provider.selectComponents(references.toString());image.setProperty(CellComponents.PROPERTY,references.toString());
    }
    /** Retire admitted current cell selections while retaining their files, results and sparse edits. */
    public static void retireSavedCellResults(ImageData<?> image,List<Map<String,Object>> artifacts)throws IOException {
        LocalParquet.requireWorker();
        var selectedBases=bindings(image);
        Object savedReceipts=image.getProperty("mif.segmentation_populations");
        var receipts=savedReceipts instanceof String text?JsonParser.parseString(text).getAsJsonObject():new JsonObject();
        var baseSections=new java.util.HashSet<String>();
        var displays=new ArrayList<Map<String,Object>>();
        for(var artifact:artifacts) {
            String kind=(String)artifact.get("artifact_kind");
            if(!"segmentation_result".equals(kind)) {
                if(!"primary_inference_result".equals(kind)&&!"secondary_inference_result".equals(kind))displays.add(artifact);
                continue;
            }
            @SuppressWarnings("unchecked") var identity=(Map<String,Object>)artifact.get("owning_identity");
            String section=(String)identity.get("section_id");
            if(!baseSections.add(section)||!selectedBases.has(section)||!receipts.has(section))
                throw new IOException("Retired cell base is missing or ambiguous for section "+section);
            var provider=open(image,section).join();
            var receipt=receipts.getAsJsonObject(section);
            if(!section.equals(provider.getSectionId())||!provider.getPopulation().equals(identity.get("segmentation_population_id"))
                    ||!provider.getDescriptionSha256().equals(identity.get("cell_evidence_sha256")))
                throw new IOException("Selected cell base differs from the admitted result for section "+section);
            for(String field:List.of("segmentation_population_id","section_population_sha256","domain_cell_mapping_sha256","cell_evidence_sha256"))
                if(!receipt.has(field)||!receipt.get(field).getAsString().equals(identity.get(field)))
                    throw new IOException("Selected cell receipt differs from the admitted result for section "+section);
        }
        if(!displays.isEmpty())retirePhenotypeDisplays(image,displays);
        for(String section:baseSections){selectedBases.remove(section);receipts.remove(section);}
        if(!baseSections.isEmpty()) {
            close(image);
            image.setProperty(CellDatasetProvider.DATASET_PROPERTY,selectedBases.toString());
            image.setProperty("mif.segmentation_populations",receipts.toString());
        }
    }
    /** Retire only the admitted automatic selections; retain immutable views and sparse edits. */
    public static void retirePhenotypeDisplays(ImageData<?> image,List<Map<String,Object>> artifacts)throws IOException {
        LocalParquet.requireWorker();
        if(artifacts==null||artifacts.isEmpty())throw new IllegalArgumentException("Display retirement requires its applied results");
        Object stored=image.getProperty(CellComponents.PROPERTY);
        var references=stored instanceof String text?JsonParser.parseString(text).getAsJsonObject():new JsonObject();
        var pathsBySection=new LinkedHashMap<String,java.util.Set<String>>();
        // Validate the complete entry before changing any saved selection.
        for(var artifact:artifacts){
            @SuppressWarnings("unchecked") var identity=(Map<String,Object>)artifact.get("owning_identity");
            @SuppressWarnings("unchecked") var component=(Map<String,Object>)identity.get("component_descriptor");
            String section=(String)identity.get("section_id");
            Path path=Path.of((String)component.get("path")).toAbsolutePath().normalize();
            var provider=open(image,section).join();
            // Reuse the provider's admitted descriptor metadata; do not reopen or rehash tables.
            var matches=provider.getSelectedComponentMetadata().stream().filter(item->path.toString().equals(item.get("path"))).toList();
            if(matches.size()!=1)throw new IOException("Retired phenotype descriptor is not selected: "+section);
            var descriptor=matches.get(0);
            @SuppressWarnings("unchecked") var view=(Map<String,Object>)descriptor.get("phenotype_view");
            Object expectedPopulation="supervised_phenotype_result".equals(artifact.get("artifact_kind"))
                    ?descriptor.get("population_id"):identity.get("segmentation_population_id");
            if(!component.get("sha256").equals(descriptor.get("sha256"))||!section.equals(descriptor.get("scope_key"))
                    ||!provider.getPopulation().equals(expectedPopulation)
                    ||view==null||!identity.get("phenotype_generation_id").equals(view.get("phenotype_generation_id"))
                    ||!identity.get("phenotype_view_artifact_sha256").equals(view.get("artifact_sha256")))
                throw new IOException("Retired phenotype view differs from its accepted section: "+section);
            String prefix="phenotype_view/"+view.get("mode")+"/"+view.get("phenotype_generation_id")+"/primary_phenotype";
            var history=references.getAsJsonObject(prefix);
            if(history==null||!history.has(section)||!path.toString().equals(history.get(section).getAsString()))
                throw new IOException("Retired phenotype history is no longer selected: "+section);
            pathsBySection.computeIfAbsent(section,key->new java.util.HashSet<>()).add(path.toString());
        }
        for(var entry:pathsBySection.entrySet())for(String field:PHENOTYPE_DISPLAY_FIELDS){
            var scopes=references.getAsJsonObject(field);
            if(scopes!=null&&scopes.has(entry.getKey())&&!entry.getValue().contains(scopes.get(entry.getKey()).getAsString()))
                throw new IOException("The selected phenotype display changed for section "+entry.getKey()+"; refresh the analysis before revising it");
        }
        for(String section:pathsBySection.keySet())for(String field:PHENOTYPE_DISPLAY_FIELDS){
            var scopes=references.getAsJsonObject(field);
            if(scopes!=null){scopes.remove(section);if(scopes.size()==0)references.remove(field);}
        }
        // Closing releases cached automatic classes; reopening applies retained manual guards.
        close(image);image.setProperty(CellComponents.PROPERTY,references.toString());
    }
    /** We release this image's selected providers, queued reads and prepared residency; its saved base/result references remain durable. */
    public static void close(ImageData<?> image) {
        synchronized(selected){var collection=selected.remove(image);if(collection!=null)collection.thenAccept(NativeCellCollection::close);}
        synchronized(sections){var providers=sections.remove(image);if(providers!=null)for(var provider:providers.values())provider.provider.thenAccept(CellDatasetProvider::close);}
    }
}
