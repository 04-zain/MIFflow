package org.mif.cells;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import java.io.IOException;
import java.nio.channels.FileChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.LinkedHashMap;
import java.util.Map;
import org.apache.parquet.example.data.simple.SimpleGroupFactory;
import org.apache.parquet.schema.MessageType;
import org.apache.parquet.schema.PrimitiveType.PrimitiveTypeName;
import org.apache.parquet.schema.LogicalTypeAnnotation;
import org.apache.parquet.schema.Types;

/** Aligned scalar results; unchanged geometry belongs exclusively to the base. */
final class CellComponentWriter implements AutoCloseable {
    private final Path root;
    private final Map<String,String> columns;
    private final JsonObject descriptor=new JsonObject();
    private final JsonArray groups=new JsonArray();
    private final SimpleGroupFactory factory;
    private final MessageType schema;
    private LocalParquetWriter writer;
    private Path part;
    private int parts;
    private long total;
    private boolean completed;
    CellComponentWriter(Path root,String population,String layoutVersion,String requestSha,String scopeKey,Map<String,String> columns,JsonObject dictionaries)throws IOException {
        LocalParquet.requireWorker();this.root=root.toAbsolutePath().normalize();this.columns=new LinkedHashMap<>(columns);
        if(!requestSha.matches("[0-9a-f]{64}"))throw new IllegalArgumentException("Component request must name the accepted scientific input identity");
        if(columns.isEmpty()||columns.containsKey("cell_id")||columns.containsKey("applied"))throw new IllegalArgumentException("Component fields must be nonempty scalar result fields");
        validateDictionaries(columns,dictionaries);
        if(Files.exists(root))throw new IOException("Component staging directory already exists");Files.createDirectories(root);
        var builder=Types.buildMessage().required(PrimitiveTypeName.BINARY).as(LogicalTypeAnnotation.stringType()).named("cell_id").required(PrimitiveTypeName.BOOLEAN).named("applied");
        var fields=new JsonObject();
        for(var entry:columns.entrySet()) {
            if(entry.getKey().isBlank()||entry.getKey().contains("\u0000"))throw new IllegalArgumentException("Invalid component field name");
            fields.addProperty(entry.getKey(),entry.getValue());
            switch(entry.getValue()) {
                case "float64" -> builder.optional(PrimitiveTypeName.DOUBLE).named(entry.getKey());
                case "int32" -> builder.optional(PrimitiveTypeName.INT32).named(entry.getKey());
                case "bool" -> builder.optional(PrimitiveTypeName.BOOLEAN).named(entry.getKey());
                case "string" -> builder.optional(PrimitiveTypeName.BINARY).as(LogicalTypeAnnotation.stringType()).named(entry.getKey());
                case "list_int32" -> builder.addField(Types.optionalList().requiredElement(PrimitiveTypeName.INT32).named(entry.getKey()));
                default -> throw new IllegalArgumentException("Unsupported component scalar type "+entry.getValue());
            }
        }
        schema=builder.named("cell_results");factory=new SimpleGroupFactory(schema);
        descriptor.addProperty("schema_version","mif-cell-component/v1");descriptor.addProperty("population_id",population);descriptor.addProperty("layout_version",layoutVersion);
        descriptor.addProperty("request_sha256",requestSha);descriptor.addProperty("scope_key",scopeKey);descriptor.add("columns",fields);descriptor.add("dictionaries",dictionaries.deepCopy());descriptor.add("groups",groups);
    }
    static void validateDictionaries(Map<String,String> columns,JsonObject dictionaries) {
        for(var field:dictionaries.entrySet()) {
            if(!SetHolder.CATEGORY_TYPES.contains(columns.get(field.getKey())))throw new IllegalArgumentException("Result dictionary must correspond to a declared Int32 field");
            var dictionary=field.getValue().getAsJsonObject();
            if(!dictionary.has("dictionary_id")||dictionary.get("dictionary_id").getAsString().isBlank()||!dictionary.has("field")||!field.getKey().equals(dictionary.get("field").getAsString())||dictionary.get("version").getAsInt()!=1)throw new IllegalArgumentException("Result dictionary must use the canonical frozen category mapping");
            int expected=0;var identities=new java.util.HashSet<String>();
            for(var raw:dictionary.getAsJsonArray("entries")){var entry=raw.getAsJsonObject();if(entry.get("code").getAsInt()!=expected++||!identities.add(entry.get("label_id").getAsString())||!entry.has("name")||!entry.has("color"))throw new IllegalArgumentException("Result category codes must be unique contiguous Int32 with stable label identity");}
        }
    }
    private static final class SetHolder {static final java.util.Set<String> CATEGORY_TYPES=java.util.Set.of("int32","list_int32");}
    private void part()throws IOException {
        if(writer!=null&&writer.getPosition()>=64L*1024*1024){writer.close();writer=null;}
        if(writer==null){part=root.resolve(String.format("part-%05d.parquet",parts++));writer=new LocalParquetWriter(part,schema);}
    }
    /** Stream one base group preserving its exact row order, including unapplied rows. */
    void group(long baseGroup,int baseRows,CellRows source)throws IOException {
        part();int[] offset={0},begin={0},physical={writer.getGroupCount()};
        source.forEach((id,values)->{
            var row=factory.newGroup().append("cell_id",id).append("applied",values!=null);
            if(values!=null)for(var field:columns.entrySet()) {
                Object value=values.get(field.getKey());if(value==null)continue;
                switch(field.getValue()) {
                    case "float64" -> row.append(field.getKey(),((Number)value).doubleValue());
                    case "int32" -> row.append(field.getKey(),code(field.getKey(),value));
                    case "bool" -> row.append(field.getKey(),(Boolean)value);
                    case "string" -> row.append(field.getKey(),(String)value);
                    case "list_int32" -> {var list=row.addGroup(field.getKey());for(Object valueCode:(Iterable<?>)value)list.addGroup("list").append("element",code(field.getKey(),valueCode));}
                    default -> throw new IllegalStateException();
                }
            }
            if(writer.shouldFlushBefore(row)){writer.flushGroup();mapping(baseGroup,physical[0],begin[0],offset[0]-begin[0]);begin[0]=offset[0];physical[0]=writer.getGroupCount();}
            writer.write(row);offset[0]++;total++;
            if(writer.getGroupCount()!=physical[0]){mapping(baseGroup,physical[0],begin[0],offset[0]-begin[0]);begin[0]=offset[0];physical[0]=writer.getGroupCount();}
        });
        if(offset[0]!=baseRows)throw new IOException("Component row count disagrees with immutable base group");
        if(offset[0]>begin[0]){writer.flushGroup();mapping(baseGroup,physical[0],begin[0],offset[0]-begin[0]);}
    }
    private int code(String field,Object value) {
        long code=((Number)value).longValue();if(code<Integer.MIN_VALUE||code>Integer.MAX_VALUE||((Number)value).doubleValue()!=code)throw new IllegalArgumentException("Result category must be an exact Int32");
        if(descriptor.getAsJsonObject("dictionaries").has(field)&&(code<0||code>=descriptor.getAsJsonObject("dictionaries").getAsJsonObject(field).getAsJsonArray("entries").size()))throw new IllegalArgumentException("Undeclared result category code in "+field);
        return (int)code;
    }
    private void mapping(long baseGroup,int physical,int offset,int rows) {
        var item=new JsonObject();item.addProperty("group_id",baseGroup);item.addProperty("file",root.relativize(part).toString());item.addProperty("row_group",physical);item.addProperty("base_row_offset",offset);item.addProperty("row_count",rows);groups.add(item);
    }
    /** We publish one completed result descriptor with compact shared attributes after all aligned rows finish. */
    Path complete(long geometryRevision,JsonObject attributes)throws IOException {
        if(writer!=null){writer.close();writer=null;}if(attributes.toString().getBytes(java.nio.charset.StandardCharsets.UTF_8).length>1024*1024)throw new IOException("Cell result attributes exceed the compact metadata allowance");descriptor.add("attributes",attributes.deepCopy());descriptor.addProperty("geometry_revision",geometryRevision);descriptor.addProperty("row_count",total);descriptor.addProperty("complete",true);
        Path path=root.resolve("component.json");Files.writeString(path,descriptor.toString(),StandardCharsets.UTF_8,StandardOpenOption.CREATE_NEW);try(var channel=FileChannel.open(path,StandardOpenOption.WRITE)){channel.force(true);}completed=true;return path;
    }
    @Override public void close()throws IOException {if(writer!=null){writer.close();writer=null;}if(!completed)Files.deleteIfExists(root.resolve("component.json"));}
    @FunctionalInterface interface RowSink {void accept(String id,Map<String,Object> values)throws IOException;}
    @FunctionalInterface interface CellRows {void forEach(RowSink sink)throws IOException;}
}
