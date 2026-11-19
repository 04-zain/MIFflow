package org.mif.cells;

import com.github.luben.zstd.Zstd;
import java.io.EOFException;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.channels.FileChannel;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.List;
import java.util.function.Consumer;
import javafx.application.Platform;
import org.apache.parquet.ParquetReadOptions;
import org.apache.parquet.bytes.BytesInput;
import org.apache.parquet.compression.CompressionCodecFactory;
import org.apache.parquet.conf.PlainParquetConfiguration;
import org.apache.parquet.example.data.Group;
import org.apache.parquet.example.data.simple.convert.GroupRecordConverter;
import org.apache.parquet.hadoop.ParquetFileReader;
import org.apache.parquet.hadoop.metadata.CompressionCodecName;
import org.apache.parquet.io.ColumnIOFactory;
import org.apache.parquet.io.InputFile;
import org.apache.parquet.io.SeekableInputStream;
import org.apache.parquet.schema.MessageType;

/** Local range access with projected columns and an explicit physical row group. */
public final class LocalParquet {
    private LocalParquet() {}
    public static void requireWorker() {
        if (Platform.isFxApplicationThread()) throw new IllegalStateException("Cell data reads must run on a background worker");
    }
    public static long read(Path path, Integer selectedGroup, List<String> columns, Consumer<Group> rows) throws IOException {
        return read(new LocalFile(path), selectedGroup, columns, rows);
    }
    static long read(Path path, Consumer<Group> rows, MessageType schema, long count) throws IOException {
        return read(new LocalFile(path), null, null, rows, schema, count);
    }
    /** Project a caller-retained file without reopening its pathname. */
    public static long read(InputFile input, Integer selectedGroup, List<String> columns, Consumer<Group> rows) throws IOException {
        return read(input, selectedGroup, columns, rows, null, null);
    }
    /** Check the owner-selected schema and count in the same footer used for decoding. */
    public static long read(InputFile input, Integer selectedGroup, List<String> columns, Consumer<Group> rows,
            MessageType expectedSchema, Long expectedRowCount) throws IOException {
        requireWorker();
        var options = ParquetReadOptions.builder(new PlainParquetConfiguration())
                .withCodecFactory(new LocalCodecs()).withUseHadoopVectoredIo(false)
                .withMaxAllocationInBytes(8 * 1024 * 1024).build();
        long count = 0;
        try (var reader = ParquetFileReader.open(input, options)) {
            var full = reader.getFileMetaData().getSchema();
            if (expectedSchema != null && !full.equals(expectedSchema))
                throw new IOException("Typed table schema differs from its owner profile");
            if (expectedRowCount != null && reader.getRowGroups().stream().mapToLong(group -> group.getRowCount()).sum() != expectedRowCount)
                throw new IOException("Typed table footer count differs from its accepted descriptor");
            if (expectedSchema != null && reader.getRowGroups().stream().anyMatch(group -> group.getRowCount() > 8192 || group.getTotalByteSize() > 8L * 1024 * 1024))
                throw new IOException("Typed table row group exceeds its bounded writer profile");
            MessageType selected = columns == null ? full : new MessageType(full.getName(), columns.stream().map(full::getType).toList());
            reader.setRequestedSchema(selected);
            var io = new ColumnIOFactory().getColumnIO(selected);
            int first = selectedGroup == null ? 0 : selectedGroup;
            int last = selectedGroup == null ? reader.getRowGroups().size() : selectedGroup + 1;
            for (int group = first; group < last; group++) {
                // A completed empty table may retain a zero-row physical group.
                // Its footer still owns schema/count; there are no pages to decode.
                if (reader.getRowGroups().get(group).getRowCount() == 0) continue;
                try (var pages = reader.readRowGroup(group)) {
                    var records = io.getRecordReader(pages, new GroupRecordConverter(selected));
                    for (long row = 0; row < pages.getRowCount(); row++) { if(Thread.currentThread().isInterrupted())throw new java.util.concurrent.CancellationException("Cell data request interrupted");rows.accept(records.read()); count++; }
                }
            }
        }
        return count;
    }
    private record LocalFile(Path path) implements InputFile {
        @Override public long getLength() throws IOException { return java.nio.file.Files.size(path); }
        @Override public SeekableInputStream newStream() throws IOException {
            FileChannel channel = FileChannel.open(path, StandardOpenOption.READ);
            return new SeekableInputStream() {
                @Override public long getPos() throws IOException { return channel.position(); }
                @Override public void seek(long offset) throws IOException { channel.position(offset); }
                @Override public int read() throws IOException { ByteBuffer b = ByteBuffer.allocate(1); return channel.read(b) < 0 ? -1 : Byte.toUnsignedInt(b.array()[0]); }
                @Override public int read(byte[] data, int offset, int length) throws IOException { return channel.read(ByteBuffer.wrap(data, offset, length)); }
                @Override public void readFully(byte[] data) throws IOException { readFully(data, 0, data.length); }
                @Override public void readFully(byte[] data, int offset, int length) throws IOException { readFully(ByteBuffer.wrap(data, offset, length)); }
                @Override public int read(ByteBuffer target) throws IOException { return channel.read(target); }
                @Override public void readFully(ByteBuffer target) throws IOException {
                    while (target.hasRemaining()) if (channel.read(target) < 0) throw new EOFException("Truncated cell dataset range: " + path);
                }
                @Override public void close() throws IOException { channel.close(); }
            };
        }
    }
    private static final class LocalCodecs implements CompressionCodecFactory {
        @Override public BytesInputCompressor getCompressor(CompressionCodecName codec) { throw new UnsupportedOperationException("Cell provider is read-only"); }
        @Override public BytesInputDecompressor getDecompressor(CompressionCodecName codec) {
            if (codec != CompressionCodecName.ZSTD && codec != CompressionCodecName.UNCOMPRESSED) throw new IllegalArgumentException("Unsupported cell table compression: " + codec);
            return new BytesInputDecompressor() {
                @Override public BytesInput decompress(BytesInput source, int size) throws IOException {
                    if (codec == CompressionCodecName.UNCOMPRESSED) return source;
                    byte[] decoded = Zstd.decompress(source.toByteArray(), size);
                    if (decoded.length != size) throw new IOException("Cell table decompression size mismatch");
                    return BytesInput.from(decoded);
                }
                @Override public void decompress(ByteBuffer source, int sourceSize, ByteBuffer output, int outputSize) throws IOException {
                    byte[] encoded = new byte[sourceSize]; source.get(encoded);
                    output.put(decompress(BytesInput.from(encoded), outputSize).toByteArray());
                }
                @Override public void release() {}
            };
        }
        @Override public void release() {}
    }
}
