package org.mif.cells;

import com.github.luben.zstd.Zstd;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.channels.FileChannel;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.Map;
import org.apache.parquet.bytes.BytesInput;
import org.apache.parquet.bytes.HeapByteBufferAllocator;
import org.apache.parquet.column.ParquetProperties;
import org.apache.parquet.column.impl.ColumnWriteStoreV1;
import org.apache.parquet.compression.CompressionCodecFactory;
import org.apache.parquet.example.data.Group;
import org.apache.parquet.example.data.GroupWriter;
import org.apache.parquet.hadoop.ColumnChunkPageWriteStore;
import org.apache.parquet.hadoop.ParquetFileWriter;
import org.apache.parquet.hadoop.metadata.CompressionCodecName;
import org.apache.parquet.io.ColumnIOFactory;
import org.apache.parquet.io.OutputFile;
import org.apache.parquet.io.PositionOutputStream;
import org.apache.parquet.schema.MessageType;

/** Write bounded local typed groups and synchronize one completed part. */
public final class LocalParquetWriter implements AutoCloseable {
    private final Path path;
    private final MessageType schema;
    private final ParquetFileWriter file;
    private final ParquetProperties properties;
    private ColumnChunkPageWriteStore pages;
    private ColumnWriteStoreV1 columns;
    private GroupWriter records;
    private org.apache.parquet.io.api.RecordConsumer consumer;
    private long rows, total, decodedBytes;
    private int groups;
    private boolean closed;
    private final CompressionCodecFactory.BytesInputCompressor compressor=new CompressionCodecFactory.BytesInputCompressor(){
        @Override public BytesInput compress(BytesInput input)throws IOException{return BytesInput.from(Zstd.compress(input.toByteArray(),1));}
        @Override public CompressionCodecName getCodecName(){return CompressionCodecName.ZSTD;}
        @Override public void release(){}
    };
    public LocalParquetWriter(Path path,MessageType schema)throws IOException {
        LocalParquet.requireWorker();this.path=path;this.schema=schema;
        properties=ParquetProperties.builder().withPageSize(64*1024).withDictionaryPageSize(64*1024).withDictionaryEncoding(true).withPageWriteChecksumEnabled(true).build();
        file=new ParquetFileWriter(new LocalOutput(path),schema,ParquetFileWriter.Mode.CREATE,64L*1024*1024,0,64,64,true);
        file.start();openGroup();
    }
    private void openGroup(){pages=new ColumnChunkPageWriteStore(compressor,schema,new HeapByteBufferAllocator(),64,true);columns=new ColumnWriteStoreV1(schema,pages,properties);consumer=new ColumnIOFactory().getColumnIO(schema).getRecordWriter(columns);records=new GroupWriter(consumer,schema);}
    public void write(Group row)throws IOException {
        if(closed)throw new IOException("Cell part writer is closed");if(shouldFlushBefore(row))flushGroup();decodedBytes+=size(row);records.write(row);rows++;total++;
        if(rows>=8192 || columns.getBufferedSize()>=8L*1024*1024)flushGroup();
    }
    /** Admission uses the decoded typed values, including list and binary payloads. */
    public boolean shouldFlushBefore(Group row) {
        long bytes=size(row);if(bytes>8L*1024*1024)throw new IllegalArgumentException("One cell result exceeds the admitted 8 MiB decoded group size");
        return rows>0&&decodedBytes+bytes>8L*1024*1024;
    }
    private static long size(Group row) {
        long bytes=0;
        for(int field=0;field<row.getType().getFieldCount();field++) {
            var type=row.getType().getType(field);int count=row.getFieldRepetitionCount(field);bytes+=4;
            for(int item=0;item<count;item++) {
                if(!type.isPrimitive()){bytes+=size(row.getGroup(field,item));continue;}
                bytes+=switch(type.asPrimitiveType().getPrimitiveTypeName()){case BINARY,FIXED_LEN_BYTE_ARRAY->row.getBinary(field,item).length()+4L;case BOOLEAN->1;case INT32,FLOAT->4;case INT64,DOUBLE->8;case INT96->12;};
            }
        }
        return bytes;
    }
    /** Close the current logical contribution without retaining prior row objects. */
    public void flushGroup()throws IOException {
        if(rows==0)return;
        consumer.flush();columns.flush();file.startBlock(rows);pages.flushToFileWriter(file);file.endBlock();columns.close();pages.close();rows=0;decodedBytes=0;groups++;openGroup();
    }
    public int getGroupCount(){return groups;}
    public long getRowCount(){return total;}
    public long getPosition()throws IOException{return file.getPos();}
    @Override public void close()throws IOException {
        if(closed)return;closed=true;
        try {
            flushGroup();columns.close();pages.close();file.end(Map.of("mif.scalar_profile","typed-zstd1"));
            var footer=file.getFooter();
            if(!footer.getFileMetaData().getSchema().equals(schema)||footer.getBlocks().stream().mapToLong(block->block.getRowCount()).sum()!=total)throw new IOException("Completed cell part schema or row count disagrees with its writer");
        } finally {file.close();}
        try(var channel=FileChannel.open(path,StandardOpenOption.WRITE)){channel.force(true);}
    }
    private record LocalOutput(Path path)implements OutputFile {
        @Override public PositionOutputStream create(long blockSize)throws IOException{return stream(false);}
        @Override public PositionOutputStream createOrOverwrite(long blockSize)throws IOException{return stream(true);}
        private PositionOutputStream stream(boolean overwrite)throws IOException {
            FileChannel channel=overwrite?FileChannel.open(path,StandardOpenOption.CREATE,StandardOpenOption.TRUNCATE_EXISTING,StandardOpenOption.WRITE):FileChannel.open(path,StandardOpenOption.CREATE_NEW,StandardOpenOption.WRITE);
            return new PositionOutputStream(){
                @Override public long getPos()throws IOException{return channel.position();}
                @Override public void write(int value)throws IOException{write(new byte[]{(byte)value});}
                @Override public void write(byte[] bytes,int offset,int length)throws IOException{ByteBuffer buffer=ByteBuffer.wrap(bytes,offset,length);while(buffer.hasRemaining())channel.write(buffer);}
                @Override public void flush()throws IOException{channel.force(true);}
                @Override public void close()throws IOException{channel.force(true);channel.close();}
            };
        }
        @Override public boolean supportsBlockSize(){return false;}
        @Override public long defaultBlockSize(){return 0;}
        @Override public String getPath(){return path.toString();}
    }
}
