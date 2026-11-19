package org.mif.segmentation;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import java.io.IOException;
import java.nio.channels.FileChannel;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import org.apache.parquet.example.data.simple.SimpleGroupFactory;
import org.apache.parquet.io.api.Binary;
import org.apache.parquet.schema.MessageType;
import org.apache.parquet.schema.MessageTypeParser;
import org.locationtech.jts.io.WKBWriter;
import org.mif.cells.LocalParquetWriter;
import qupath.lib.objects.PathCellObject;
import qupath.lib.objects.PathObject;

/** One bounded finalized native stream for the canonical spatial writer. */
public final class NativeCellStream implements AutoCloseable {
    private static final MessageType SCHEMA=MessageTypeParser.parseMessageType("message finalized_cells { required binary cell_id (UTF8); required binary cell_geometry; optional binary nucleus_geometry; required double x_level0; required double y_level0; optional double x_um; optional double y_um; required int32 c; required int32 z; required int32 t; optional binary primary_phenotype (UTF8); required group metadata (MAP) { repeated group key_value { required binary key (UTF8); required binary value (UTF8); } } required group measurements (MAP) { repeated group key_value { required binary key (UTF8); required double value; } } }");
    private final Path root;
    private final JsonObject descriptor=new JsonObject();
    private final JsonArray parts=new JsonArray();
    private final SimpleGroupFactory factory=new SimpleGroupFactory(SCHEMA);
    private LocalParquetWriter writer;
    private Path part;
    private int count;
    private long rows;
    private boolean completed;
    public NativeCellStream(Path root,JsonObject scope,String sourceSha)throws IOException {
        this.root=root.toAbsolutePath().normalize();if(Files.exists(root))throw new IOException("Native stream staging directory already exists");Files.createDirectories(root);
        descriptor.addProperty("schema_version","mif-native-cell-stream/v1");descriptor.addProperty("source_sha256",sourceSha);descriptor.add("scope",scope.deepCopy());descriptor.add("parts",parts);descriptor.addProperty("coordinate_system","global_level0_pixels");
    }
    public void write(PathObject cell)throws IOException {
        if(completed)throw new IOException("Finalized native stream is already complete");
        if(!(cell instanceof PathCellObject))throw new IOException("Configured native segmentation must finalize cell objects");String id=cell.getMetadata().get("cell_id");if(id==null||id.isBlank())throw new IOException("Finalized cell must have its accepted stable pipeline ID");
        if(writer!=null&&writer.getPosition()>=64L*1024*1024)finishPart();
        if(writer==null){part=root.resolve(String.format("part-%05d.parquet",count++));writer=new LocalParquetWriter(part,SCHEMA);}
        var roi=cell.getROI();var group=factory.newGroup().append("cell_id",id).append("cell_geometry",Binary.fromConstantByteArray(new WKBWriter().write(roi.getGeometry()))).append("x_level0",roi.getCentroidX()).append("y_level0",roi.getCentroidY()).append("c",roi.getC()).append("z",roi.getZ()).append("t",roi.getT());
        var scope=descriptor.getAsJsonObject("scope");if(scope.has("x_um_per_px")&&!scope.get("x_um_per_px").isJsonNull())group.append("x_um",roi.getCentroidX()*scope.get("x_um_per_px").getAsDouble());if(scope.has("y_um_per_px")&&!scope.get("y_um_per_px").isJsonNull())group.append("y_um",roi.getCentroidY()*scope.get("y_um_per_px").getAsDouble());
        var nucleus=((PathCellObject)cell).getNucleusROI();if(nucleus!=null)group.append("nucleus_geometry",Binary.fromConstantByteArray(new WKBWriter().write(nucleus.getGeometry())));
        if(cell.getPathClass()!=null)group.append("primary_phenotype",cell.getPathClass().toString());
        var metadata=group.addGroup("metadata");for(var field:cell.getMetadata().entrySet())metadata.addGroup("key_value").append("key",field.getKey()).append("value",field.getValue());
        var measurements=group.addGroup("measurements");for(String name:cell.getMeasurementList().getNames())measurements.addGroup("key_value").append("key",name).append("value",cell.getMeasurementList().get(name));
        writer.write(group);rows++;
    }
    private void finishPart()throws IOException {writer.close();var summary=new JsonObject();summary.addProperty("file",root.relativize(part).toString());summary.addProperty("row_count",writer.getRowCount());summary.addProperty("bytes",Files.size(part));parts.add(summary);writer=null;}
    public Path complete()throws IOException {
        if(writer!=null)finishPart();descriptor.addProperty("row_count",rows);descriptor.addProperty("complete",true);Path path=root.resolve("stream.json");Files.writeString(path,descriptor.toString(),StandardOpenOption.CREATE_NEW);try(var channel=FileChannel.open(path,StandardOpenOption.WRITE)){channel.force(true);}completed=true;return path;
    }
    @Override public void close()throws IOException {if(writer!=null){writer.close();writer=null;}if(!completed)Files.deleteIfExists(root.resolve("stream.json"));}
}
