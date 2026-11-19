package org.mif.cells;

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
import qupath.lib.objects.PathCellObject;
import qupath.lib.objects.PathObject;

/** Persist only human edits, independently of the clean immutable cell base. */
final class CellEditWriter implements AutoCloseable {
    static final MessageType SCHEMA=MessageTypeParser.parseMessageType("message cell_edits { required binary cell_id (UTF8); required boolean deleted; required int64 geometry_revision; required binary cell_geometry; optional binary nucleus_geometry; required double x_level0; required double y_level0; optional double x_um; optional double y_um; required int32 c; required int32 z; required int32 t; optional binary primary_phenotype (UTF8); required group metadata (MAP) { repeated group key_value { required binary key (UTF8); required binary value (UTF8); } } }");
    private final Path root;
    private final JsonObject descriptor=new JsonObject();
    private final JsonArray parts=new JsonArray();
    private final SimpleGroupFactory factory=new SimpleGroupFactory(SCHEMA);
    private final JsonObject scope;
    private LocalParquetWriter writer;
    private Path part;
    private int count;
    private long rows;
    private boolean complete;
    CellEditWriter(Path root,JsonObject base,String baseSha,long revision)throws IOException {
        LocalParquet.requireWorker();this.root=root.toAbsolutePath().normalize();scope=base.getAsJsonObject("scope");
        if(Files.exists(root))throw new IOException("Sparse edit publication directory already exists");Files.createDirectories(root);
        descriptor.addProperty("schema_version","mif-cell-edits/v1");descriptor.addProperty("population_id",scope.get("population_id").getAsString());descriptor.addProperty("layout_version",base.get("layout_version").getAsString());descriptor.addProperty("base_sha256",baseSha);descriptor.addProperty("geometry_revision",revision);descriptor.add("parts",parts);
    }
    void record(String id,boolean deleted,long revision,PathObject cell)throws IOException {
        if(complete)throw new IOException("Sparse edit generation is complete");
        if(writer!=null&&writer.getPosition()>=64L*1024*1024)finishPart();
        if(writer==null){part=root.resolve(String.format("part-%05d.parquet",count++));writer=new LocalParquetWriter(part,SCHEMA);}
        var roi=cell.getROI();var row=factory.newGroup().append("cell_id",id).append("deleted",deleted).append("geometry_revision",revision).append("cell_geometry",Binary.fromConstantByteArray(new WKBWriter().write(roi.getGeometry()))).append("x_level0",roi.getCentroidX()).append("y_level0",roi.getCentroidY()).append("c",roi.getC()).append("z",roi.getZ()).append("t",roi.getT());
        var nucleus=((PathCellObject)cell).getNucleusROI();if(nucleus!=null)row.append("nucleus_geometry",Binary.fromConstantByteArray(new WKBWriter().write(nucleus.getGeometry())));
        if(scope.has("x_um_per_px")&&!scope.get("x_um_per_px").isJsonNull())row.append("x_um",roi.getCentroidX()*scope.get("x_um_per_px").getAsDouble());
        if(scope.has("y_um_per_px")&&!scope.get("y_um_per_px").isJsonNull())row.append("y_um",roi.getCentroidY()*scope.get("y_um_per_px").getAsDouble());
        if(cell.getPathClass()!=null)row.append("primary_phenotype",cell.getPathClass().toString());
        var metadata=row.addGroup("metadata");for(var field:cell.getMetadata().entrySet())metadata.addGroup("key_value").append("key",field.getKey()).append("value",field.getValue());
        writer.write(row);rows++;
    }
    private void finishPart()throws IOException {writer.close();var summary=new JsonObject();summary.addProperty("file",root.relativize(part).toString());summary.addProperty("row_count",writer.getRowCount());summary.addProperty("bytes",Files.size(part));parts.add(summary);writer=null;}
    Path complete()throws IOException {if(writer!=null)finishPart();descriptor.addProperty("row_count",rows);descriptor.addProperty("complete",true);Path path=root.resolve("edit.json");Files.writeString(path,descriptor.toString(),StandardOpenOption.CREATE_NEW);try(var channel=FileChannel.open(path,StandardOpenOption.WRITE)){channel.force(true);}complete=true;return path;}
    @Override public void close()throws IOException {if(writer!=null){writer.close();writer=null;}if(!complete)Files.deleteIfExists(root.resolve("edit.json"));}
}
