package org.mif.cells;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.apache.parquet.example.data.Group;
import qupath.lib.objects.PathObject;
import qupath.lib.objects.classes.PathClass;

/** Resolve selected result metadata once; join only one admitted base group. */
final class CellComponents implements AutoCloseable {
    private static final com.google.gson.Gson JSON=new com.google.gson.Gson();
    static final String PROPERTY="mif.cell_components";
    private record Result(Path root,JsonObject descriptor,Set<String> fields,Map<String,String> sourceFields,Map<String,Map<Integer,JsonObject>> dictionaries) {}
    private record Contribution(Result result,int group,int offset,int rows,Path file) {}
    private record Value(Object scalar,Result source,String field) {}
    private record Row(String id,Map<String,Value> fields) {}
    private final List<Result> results=new ArrayList<>();
    private Map<String,Map<String,String>> selectedReferences=Map.of();
    private List<Map<String,Object>> selectedMetadata=List.of();
    private final Map<Long,List<Contribution>> byGroup=new HashMap<>();
    private final Map<String,PathClass> classes=new HashMap<>();
    private final long allowance;
    private record GroupKey(long group,Set<String> fields) {}
    static final class Joined {private final Map<Integer,Row> rows;private Joined(Map<Integer,Row> rows){this.rows=rows;}}
    CellComponents(String selected,String population,String layoutVersion,long allowance)throws IOException {
        LocalParquet.requireWorker();this.allowance=allowance;
        if(selected==null||selected.isBlank())return;
        try {
        long metadataCost=128+selected.length()*4L;SharedCellCache.metadata(this,metadataCost);
        selectedReferences=new LinkedHashMap<>();selectedMetadata=new ArrayList<>();
        var references=JsonParser.parseString(selected).getAsJsonObject();var paths=new LinkedHashMap<Path,Set<String>>();
        for(var field:references.entrySet())for(var scope:field.getValue().getAsJsonObject().entrySet())paths.computeIfAbsent(Path.of(scope.getValue().getAsString()).toAbsolutePath().normalize(),p->new HashSet<>()).add(field.getKey());
        for(var reference:paths.entrySet()) {
            Path path=reference.getKey();long candidateCost=Math.addExact(metadataCost,Math.multiplyExact(Files.size(path),8));SharedCellCache.metadata(this,candidateCost);byte[] descriptorBytes=Files.readAllBytes(path);var descriptor=JsonParser.parseString(new String(descriptorBytes,java.nio.charset.StandardCharsets.UTF_8)).getAsJsonObject();
            if(!population.equals(descriptor.get("population_id").getAsString())){SharedCellCache.metadata(this,metadataCost);continue;}
            metadataCost=candidateCost;
            if(!"mif-cell-component/v1".equals(descriptor.get("schema_version").getAsString())||!descriptor.get("complete").getAsBoolean()||!layoutVersion.equals(descriptor.get("layout_version").getAsString()))throw new IOException("Selected cell result belongs to an incomplete or different layout");
            var sourceFields=new LinkedHashMap<String,String>();
            for(String field:reference.getValue())sourceFields.put(field,sourceField(descriptor,field));
            var dictionaries=new HashMap<String,Map<Integer,JsonObject>>();
            for(var field:descriptor.getAsJsonObject("dictionaries").entrySet()) {
                var codes=new HashMap<Integer,JsonObject>();
                for(var raw:field.getValue().getAsJsonObject().getAsJsonArray("entries")){var entry=raw.getAsJsonObject();codes.put(entry.get("code").getAsInt(),entry);}
                dictionaries.put(field.getKey(),Map.copyOf(codes));
            }
            var result=new Result(path.getParent(),descriptor,Set.copyOf(reference.getValue()),java.util.Collections.unmodifiableMap(sourceFields),Map.copyOf(dictionaries));results.add(result);
            // i) Reuse this admitted metadata for native export rather than reopening it.
            for(String field:reference.getValue()) {
                var selectedScopes=new LinkedHashMap<String,String>();
                for(var scope:references.getAsJsonObject(field).entrySet())
                    if(Path.of(scope.getValue().getAsString()).toAbsolutePath().normalize().equals(path))selectedScopes.put(scope.getKey(),path.toString());
                selectedReferences.computeIfAbsent(field,key->new LinkedHashMap<>()).putAll(selectedScopes);
            }
            var summary=new LinkedHashMap<String,Object>();
            summary.put("path",path.toString());summary.put("sha256",sha(descriptorBytes));
            summary.put("population_id",population);summary.put("scope_key",descriptor.get("scope_key").getAsString());
            var attributes=descriptor.getAsJsonObject("attributes");var view=attributes==null?null:attributes.getAsJsonObject("phenotype_view");
            if(view!=null) {
                var identity=new LinkedHashMap<String,Object>();
                for(var item:view.entrySet()) {
                    var value=item.getValue();
                    if(!value.isJsonPrimitive())throw new IOException("Phenotype view identity must contain scalar metadata");
                    var scalar=value.getAsJsonPrimitive();identity.put(item.getKey(),scalar.isString()?scalar.getAsString():scalar.isBoolean()?scalar.getAsBoolean():scalar.getAsNumber());
                }
                summary.put("phenotype_view",java.util.Collections.unmodifiableMap(identity));
            }
            selectedMetadata.add(java.util.Collections.unmodifiableMap(summary));
            for(var raw:descriptor.getAsJsonArray("groups")) {
                var item=raw.getAsJsonObject();Path file=result.root.resolve(item.get("file").getAsString()).normalize();if(!file.startsWith(result.root))throw new IOException("Component part escapes its generation");
                var contribution=new Contribution(result,item.get("row_group").getAsInt(),item.get("base_row_offset").getAsInt(),item.get("row_count").getAsInt(),file);
                if(contribution.offset<0||contribution.rows<0||contribution.rows>8192)throw new IOException("Invalid component alignment range");
                byGroup.computeIfAbsent(item.get("group_id").getAsLong(),key->new ArrayList<>()).add(contribution);
            }
        }
        selectedReferences.replaceAll((field,scopes)->Map.copyOf(scopes));
        selectedReferences=Map.copyOf(selectedReferences);selectedMetadata=List.copyOf(selectedMetadata);
        }catch(IOException|RuntimeException|Error failure){SharedCellCache.releaseMetadata(this);throw failure;}
    }
    /** We expose only already admitted references and compact scientific identity. */
    synchronized Map<String,Map<String,String>> references() {
        return selectedReferences;
    }
    synchronized List<Map<String,Object>> metadata(){return selectedMetadata;}
    private static String sha(byte[] bytes) {
        try{return java.util.HexFormat.of().formatHex(java.security.MessageDigest.getInstance("SHA-256").digest(bytes));}
        catch(java.security.NoSuchAlgorithmException impossible){throw new AssertionError(impossible);}
    }
    synchronized Joined group(long id,int baseRows)throws IOException {
        return group(id,baseRows,null);
    }
    /**
     * We project only the requested selected result fields into one aligned group.
     * A null projection admits every scientific field; viewport requests select
     * the primary display class alone. The cache identity includes the projection
     * so a subsequent measurement lookup cannot reuse an incomplete display join.
     * Conflicting IDs, row ranges or overlapping selected scopes stop the read.
     */
    synchronized Joined group(long id,int baseRows,Set<String> projection)throws IOException {
        var groupKey=new GroupKey(id,projection==null?null:Set.copyOf(projection));
        var cached=(Joined)SharedCellCache.get(this,groupKey);if(cached!=null)return cached;
        var joined=new HashMap<Integer,Row>();long[] bytes={0};
        for(var contribution:byGroup.getOrDefault(id,List.of())) {
            var fields=new HashSet<>(contribution.result.fields);if(projection!=null)fields.retainAll(projection);
            if(fields.isEmpty())continue;
            if(contribution.offset+contribution.rows>baseRows)throw new IOException("Component alignment exceeds immutable base group");
            var projectedColumns=new HashSet<String>();for(String field:fields){String source=contribution.result.sourceFields.get(field);if(source!=null)projectedColumns.add(source);}
            var columns=new ArrayList<>(projectedColumns);columns.add("cell_id");columns.add("applied");int[] offset={0};
            LocalParquet.read(contribution.file,contribution.group,columns,row->{
                int baseOffset=contribution.offset+offset[0]++;if(!row.getBoolean("applied",0))return;
                String cellId=row.getString("cell_id",0);var values=joined.computeIfAbsent(baseOffset,position->{bytes[0]+=256+cellId.length()*2L;return new Row(cellId,new HashMap<>());});
                if(!values.id.equals(cellId))throw new IllegalArgumentException("Selected components disagree on aligned cell identity");
                var sourceValues=new HashMap<String,Object>();
                for(String field:fields) {
                    if(values.fields.containsKey(field))throw new IllegalArgumentException("Selected result scopes overlap for field "+field+" at "+cellId);
                    String source=contribution.result.sourceFields.get(field);
                    if(source!=null&&!sourceValues.containsKey(source))sourceValues.put(source,read(row,source,contribution.result.descriptor.getAsJsonObject("columns").get(source).getAsString()));
                    Object value=source==null?identityValue(contribution.result.descriptor,field):sourceValues.get(source);
                    bytes[0]+=64+field.length()*2L+(value instanceof String text?text.length()*2L:value instanceof List<?> items?32+items.size()*32L:16);
                    if(bytes[0]>allowance)throw new IllegalArgumentException("Selected scalar results exceed the admitted group memory allowance");
                    values.fields.put(field,new Value(value,contribution.result,field));
                }
            });
            if(offset[0]!=contribution.rows)throw new IOException("Component footer row count disagrees with its aligned descriptor");
        }
        var result=new Joined(joined);
        SharedCellCache.put(this,groupKey,result,bytes[0],()->{});
        return result;
    }
    @Override public void close(){SharedCellCache.release(this);results.clear();selectedReferences=Map.of();selectedMetadata=List.of();byGroup.clear();classes.clear();SharedCellCache.releaseMetadata(this);}
    /** We resolve a display alias to the one typed view column or shared identity it selects. */
    private static String sourceField(JsonObject descriptor,String target)throws IOException {
        if(descriptor.getAsJsonObject("columns").has(target))return target;
        var attributes=descriptor.getAsJsonObject("attributes");var identity=attributes==null?null:attributes.getAsJsonObject("phenotype_view");
        if(identity==null)throw new IOException("Selected component does not define field "+target);
        if(Set.of("selected_phenotype_view","phenotyping_generation_id","phenotyping_run_id").contains(target))return null;
        String source="phenotype_view/"+identity.get("mode").getAsString()+"/"+identity.get("phenotype_generation_id").getAsString()+"/"+target;
        if(!Set.of("primary_phenotype","phenotype_source","phenotype_components","marker_states","phenotype_confidence","cluster_id","cluster_label").contains(target)||!descriptor.getAsJsonObject("columns").has(source))throw new IOException("Selected phenotype component does not define alias "+target);
        return source;
    }
    private static String identityValue(JsonObject descriptor,String field) {
        var identity=descriptor.getAsJsonObject("attributes").getAsJsonObject("phenotype_view");
        String source=switch(field){case "selected_phenotype_view"->"mode";case "phenotyping_generation_id"->"phenotype_generation_id";case "phenotyping_run_id"->"run_id";default->throw new IllegalArgumentException("Unknown phenotype identity alias "+field);};
        return identity.get(source).getAsString();
    }
    private static Object read(Group row,String field,String type) {
        if(row.getFieldRepetitionCount(field)==0)return null;
        return switch(type){case "float64"->row.getDouble(field,0);case "int32"->row.getInteger(field,0);case "bool"->row.getBoolean(field,0);case "string"->row.getString(field,0);case "list_int32"->{var list=row.getGroup(field,0);var values=new ArrayList<Integer>();for(int i=0;i<list.getFieldRepetitionCount("list");i++)values.add(list.getGroup("list",i).getInteger("element",0));yield List.copyOf(values);}default->throw new IllegalArgumentException("Unsupported component scalar type");};
    }
    synchronized void invalidateDerived(PathObject cell) {
        for(var result:results)for(String field:result.fields){
            if("primary_phenotype".equals(field)&&!"true".equals(cell.getMetadata().get("mif.manual_class_override"))&&cell.getPathClass()!=null&&cell.getPathClass().toString().equals(cell.getMetadata().get(field)))cell.setPathClass(null);
            if(!field.startsWith("measurement:")&&!("primary_phenotype".equals(field)&&"true".equals(cell.getMetadata().get("mif.manual_class_override"))))cell.getMetadata().remove(field);
        }
        for(String field:List.of("secondary_roi_ids","selected_phenotype_view","phenotyping_generation_id","membership_generation_id","phenotype_views"))cell.getMetadata().remove(field);
    }
    static Integer color(com.google.gson.JsonElement value) {
        if(value==null||value.isJsonNull())return null;
        var primitive=value.getAsJsonPrimitive();
        return primitive.isNumber()?primitive.getAsInt():Integer.parseInt(primitive.getAsString().replace("#",""),16);
    }
    /** We decode field-local category codes in constant time, preserving null and empty lists. */
    private static com.google.gson.JsonElement value(Value value) {
        if(value.scalar==null)return com.google.gson.JsonNull.INSTANCE;
        String source=value.source.sourceFields.get(value.field);var dictionary=source==null?null:value.source.dictionaries.get(source);
        if(dictionary==null)return JSON.toJsonTree(value.scalar);
        if(value.scalar instanceof List<?> codes) {
            var names=new com.google.gson.JsonArray();
            for(Object raw:codes)names.add(category(dictionary,((Number)raw).intValue(),value.field).get("name").getAsString());
            return names;
        }
        return category(dictionary,((Number)value.scalar).intValue(),value.field).get("name");
    }
    private static JsonObject category(Map<Integer,JsonObject> dictionary,int code,String field) {
        var result=dictionary.get(code);
        if(result==null)throw new IllegalArgumentException("Undeclared result category "+code+" in "+field);
        return result;
    }
    /**
     * We apply one aligned row to a temporary native object. View history is
     * reconstructed here from typed fields and one shared descriptor identity;
     * the persisted tables never repeat a cell's full phenotype history.
     */
    synchronized void apply(Joined joined,int offset,String id,PathObject cell,long editRevision) {
        // A completed measurement replacement deselects automatic display
        // aliases. Sparse copies may still carry the old class, so we clear
        // their display before applying any retained historical view fields.
        // Explicit human class/source guards remain associated with the edit.
        if(selectedReferences.containsKey("mif.measurements_stale")&&!selectedReferences.containsKey("primary_phenotype")){
            boolean manual="true".equals(cell.getMetadata().get("mif.manual_class_override"));
            if(!manual){cell.setPathClass(null);cell.getMetadata().remove("mif.primary_class_source");}
            for(String field:NativeCellScope.PHENOTYPE_DISPLAY_FIELDS)if(!manual||!field.equals("primary_phenotype"))cell.getMetadata().remove(field);
        }
        Row row=joined.rows.get(offset);if(row==null)return;if(!row.id.equals(id))throw new IllegalArgumentException("Component cell identity disagrees with base row "+id);
        var views=new LinkedHashMap<Result,JsonObject>();
        for(var pair:row.fields.entrySet()) {
            var value=pair.getValue();String field=pair.getKey();
            if(editRevision>value.source.descriptor.get("geometry_revision").getAsLong())continue;
            if(field.startsWith("phenotype_view/")) {
                var identity=value.source.descriptor.getAsJsonObject("attributes").getAsJsonObject("phenotype_view");
                String prefix="phenotype_view/"+identity.get("mode").getAsString()+"/"+identity.get("phenotype_generation_id").getAsString()+"/";
                if(!field.startsWith(prefix))throw new IllegalArgumentException("Phenotype field differs from its component view identity");
                String name=field.substring(prefix.length());
                var decoded=value(value);
                if(name.equals("marker_states")&&!decoded.isJsonNull())decoded=JsonParser.parseString(decoded.getAsString());
                views.computeIfAbsent(value.source,key->new JsonObject()).add(name,decoded);
                continue;
            }
            String classSource=value.source.root.toString()+":"+value.source.descriptor.get("request_sha256").getAsString();
            if("primary_phenotype".equals(field)&&"true".equals(cell.getMetadata().get("mif.manual_class_override"))&&classSource.equals(cell.getMetadata().get("mif.manual_class_source")))continue;
            if("primary_phenotype".equals(field)){cell.getMetadata().remove("mif.manual_class_override");cell.getMetadata().remove("mif.manual_class_source");cell.getMetadata().put("mif.primary_class_source",classSource);}
            if(field.startsWith("measurement:")){String name=field.substring(12);if(value.scalar==null)cell.getMeasurementList().remove(name);else cell.getMeasurementList().put(name,((Number)value.scalar).doubleValue());}
            else {
                var decoded=value(value);
                if(decoded.isJsonNull()){cell.getMetadata().remove(field);if("primary_phenotype".equals(field))cell.setPathClass(null);}
                else {
                    String text=decoded.isJsonPrimitive()?decoded.getAsString():decoded.toString();cell.getMetadata().put(field,text);
                    if("primary_phenotype".equals(field)) {
                        int code=((Number)value.scalar).intValue();String classKey=value.source.root+":"+field+":"+code;
                        var entry=category(value.source.dictionaries.get(value.source.sourceFields.get(field)),code,field);
                        cell.setPathClass(classes.computeIfAbsent(classKey,key->PathClass.fromString(text,color(entry.get("color")))));
                    }
                }
            }
            if("mif.measurements_stale".equals(field)&&Boolean.FALSE.equals(value.scalar))cell.getMetadata().put("scientific_measurements_stale","false");
        }
        if(!views.isEmpty()) {
            String stored=cell.getMetadata().get("phenotype_views");
            var history=stored==null?new JsonObject():JsonParser.parseString(stored).getAsJsonObject();
            for(var pair:views.entrySet()) {
                var identity=pair.getKey().descriptor.getAsJsonObject("attributes").getAsJsonObject("phenotype_view");
                String mode=identity.get("mode").getAsString(),generation=identity.get("phenotype_generation_id").getAsString();
                var entry=pair.getValue();entry.addProperty("cell_id",id);entry.addProperty("analysis_domain_id",cell.getMetadata().get("analysis_domain_id"));
                entry.addProperty("schema_version","phenotype-cell-view/v1");entry.add("artifact_sha256",identity.get("artifact_sha256"));entry.add("lineage_sha256",identity.get("lineage_sha256"));
                String source=pair.getKey().root+":"+pair.getKey().descriptor.get("request_sha256").getAsString();
                if("true".equals(cell.getMetadata().get("mif.manual_class_override"))&&source.equals(cell.getMetadata().get("mif.manual_class_source")))
                    entry.addProperty("primary_phenotype",cell.getMetadata().get("primary_phenotype"));
                var generations=history.getAsJsonObject(mode);if(generations==null){generations=new JsonObject();history.add(mode,generations);}generations.add(generation,entry);
            }
            cell.getMetadata().put("phenotype_views",history.toString());
        }
    }
}
