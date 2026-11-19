package org.mif.io;

import com.sun.jna.Native;
import com.sun.jna.Pointer;
import com.sun.jna.Structure;
import com.sun.jna.WString;
import com.sun.jna.win32.StdCallLibrary;
import java.io.EOFException;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.channels.SeekableByteChannel;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.SecureDirectoryStream;
import java.nio.file.StandardOpenOption;
import java.nio.file.attribute.BasicFileAttributes;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Set;
import org.apache.parquet.io.InputFile;
import org.apache.parquet.io.SeekableInputStream;

/** Keep one regular scientific file and its parents open across hashing and decoding. */
public final class PinnedInputFile implements InputFile, AutoCloseable {
    private record DirectoryPin(Path path, DirectoryStream<Path> stream, Object key) {}
    private final List<DirectoryPin> parents = new ArrayList<>();
    private final Path path;
    private final BasicFileAttributes original;
    private final Object mutationTime;
    private final SeekableByteChannel channel;
    private final WindowsPin windows;

    public PinnedInputFile(Path root, String member) throws IOException {
        if (!root.isAbsolute() || !root.equals(root.normalize()) || Path.of(member).isAbsolute()
                || !root.resolve(member).equals(root.resolve(member).normalize()))
            throw new IOException("Scientific file member is not canonical");
        path = root.resolve(member);
        WindowsPin win = null;
        SeekableByteChannel opened = null;
        try {
            if (System.getProperty("os.name").startsWith("Windows")) win = new WindowsPin(path);
            Path cursor = path.getRoot();
            while (true) {
                var attributes = Files.readAttributes(cursor, BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS);
                if (!attributes.isDirectory() || attributes.isSymbolicLink() || attributes.fileKey() == null || !cursor.toRealPath().equals(cursor))
                    throw new IOException("Scientific file parent is redirected");
                var stream = Files.newDirectoryStream(cursor);
                parents.add(new DirectoryPin(cursor, stream, attributes.fileKey()));
                if (cursor.equals(path.getParent())) break;
                cursor = cursor.resolve(path.getName(cursor.getNameCount()));
            }
            original = attributes();
            requireRegular(original, win);
            mutationTime = win == null ? Files.getAttribute(path, "unix:ctime", LinkOption.NOFOLLOW_LINKS) : null;
            var parent = parents.get(parents.size() - 1).stream();
            if (parent instanceof SecureDirectoryStream<Path> secure)
                opened = secure.newByteChannel(path.getFileName(), Set.of(StandardOpenOption.READ, LinkOption.NOFOLLOW_LINKS));
            else
                opened = Files.newByteChannel(path, Set.of(StandardOpenOption.READ, LinkOption.NOFOLLOW_LINKS));
            channel = opened; windows = win;
            validate();
        } catch (Throwable failure) {
            if (opened != null) opened.close();
            for (var parent : parents) parent.stream().close();
            if (win != null) win.close();
            throw failure;
        }
    }
    private BasicFileAttributes attributes() throws IOException {
        return Files.readAttributes(path, BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS);
    }
    private void requireRegular(BasicFileAttributes attrs, WindowsPin win) throws IOException {
        if (!attrs.isRegularFile() || attrs.isSymbolicLink() || attrs.fileKey() == null)
            throw new IOException("Scientific file is not an ordinary identified file");
        if (win == null && ((Number)Files.getAttribute(path, "unix:nlink", LinkOption.NOFOLLOW_LINKS)).longValue() != 1)
            throw new IOException("Scientific file must have exactly one hard link");
        if (win != null) win.validate();
    }
    public void validate() throws IOException {
        for (var parent : parents) {
            var attrs = Files.readAttributes(parent.path(), BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS);
            if (!attrs.isDirectory() || attrs.isSymbolicLink() || !parent.key().equals(attrs.fileKey()) || !parent.path().toRealPath().equals(parent.path()))
                throw new IOException("Scientific file retained parent was replaced");
        }
        var current = attributes(); requireRegular(current, windows);
        if (!original.fileKey().equals(current.fileKey()) || original.size() != current.size()
                || !original.lastModifiedTime().equals(current.lastModifiedTime())
                || !original.creationTime().equals(current.creationTime()) || channel.size() != original.size())
            throw new IOException("Scientific file retained file changed");
        if (windows == null && !mutationTime.equals(Files.getAttribute(path, "unix:ctime", LinkOption.NOFOLLOW_LINKS)))
            throw new IOException("Scientific file retained file metadata changed");
    }
    public String sha256() throws IOException {
        var digest = sha(); var buffer = ByteBuffer.allocate(1024 * 1024); channel.position(0);
        while (channel.read(buffer) >= 0) { buffer.flip(); digest.update(buffer); buffer.clear(); }
        channel.position(0); return HexFormat.of().formatHex(digest.digest());
    }
    @Override public long getLength() { return original.size(); }
    @Override public SeekableInputStream newStream() throws IOException {
        channel.position(0);
        return new SeekableInputStream() {
            private final ByteBuffer single = ByteBuffer.allocate(1);
            @Override public long getPos() throws IOException { return channel.position(); }
            @Override public void seek(long offset) throws IOException { channel.position(offset); }
            @Override public int read() throws IOException { single.clear(); int size = channel.read(single); return size < 0 ? -1 : Byte.toUnsignedInt(single.array()[0]); }
            @Override public int read(byte[] data, int offset, int length) throws IOException { return channel.read(ByteBuffer.wrap(data, offset, length)); }
            @Override public void readFully(byte[] data) throws IOException { readFully(data, 0, data.length); }
            @Override public void readFully(byte[] data, int offset, int length) throws IOException { readFully(ByteBuffer.wrap(data, offset, length)); }
            @Override public int read(ByteBuffer buffer) throws IOException { return channel.read(buffer); }
            @Override public void readFully(ByteBuffer buffer) throws IOException { while (buffer.hasRemaining()) if (channel.read(buffer) < 0) throw new EOFException("Truncated Scientific file"); }
            @Override public void close() { /* PinnedInputFile retains the read channel for the ending check. */ }
        };
    }
    @Override public void close() throws IOException {
        try { channel.close(); }
        finally {
            try { for (int i = parents.size() - 1; i >= 0; i--) parents.get(i).stream().close(); }
            finally { if (windows != null) windows.close(); }
        }
    }


    /** Copy our retained bytes into one absent final file and force them before use. */
    public String copyNewTo(Path destination, String expectedDigest, long expectedBytes) throws IOException {
        if (!destination.isAbsolute() || !destination.equals(destination.normalize()))
            throw new IOException("Model destination must be canonical and absolute");
        List<DirectoryPin> retained = new ArrayList<>();
        WindowsPin targetWindows = null;
        try {
            if (System.getProperty("os.name").startsWith("Windows"))
                targetWindows = new WindowsPin(destination.getParent(), true);
            Path cursor = destination.getRoot();
            while (true) {
                var attrs = Files.readAttributes(cursor, BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS);
                if (!attrs.isDirectory() || attrs.isSymbolicLink() || attrs.fileKey() == null || !cursor.toRealPath().equals(cursor))
                    throw new IOException("Model destination parent is redirected");
                retained.add(new DirectoryPin(cursor, Files.newDirectoryStream(cursor), attrs.fileKey()));
                if (cursor.equals(destination.getParent())) break;
                cursor = cursor.resolve(destination.getName(cursor.getNameCount()));
            }
            var parent = retained.get(retained.size() - 1).stream();
            Set<java.nio.file.OpenOption> options = Set.of(StandardOpenOption.WRITE, StandardOpenOption.CREATE_NEW, LinkOption.NOFOLLOW_LINKS);
            try (var output = parent instanceof SecureDirectoryStream<Path> secure
                    ? secure.newByteChannel(destination.getFileName(), options)
                    : Files.newByteChannel(destination, options)) {
                var digest = sha();
                var buffer = ByteBuffer.allocate(1024 * 1024);
                channel.position(0);
                long count = 0;
                while (channel.read(buffer) >= 0) {
                    buffer.flip(); count += buffer.remaining();
                    if (count > expectedBytes) throw new IOException("Model copy exceeds accepted size");
                    digest.update(buffer.asReadOnlyBuffer());
                    while (buffer.hasRemaining()) output.write(buffer);
                    buffer.clear();
                }
                String actual = HexFormat.of().formatHex(digest.digest());
                if (count != expectedBytes || !actual.equals(expectedDigest))
                    throw new IOException("Model copy differs from accepted bytes");
                if (!(output instanceof java.nio.channels.FileChannel fileChannel))
                    throw new IOException("Model destination does not expose a durable file channel");
                fileChannel.force(true);
                validate();
                for (var directory : retained) {
                    var attrs = Files.readAttributes(directory.path(), BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS);
                    if (!attrs.isDirectory() || attrs.isSymbolicLink() || !directory.key().equals(attrs.fileKey())
                            || !directory.path().toRealPath().equals(directory.path()))
                        throw new IOException("Model destination parent changed");
                }
                return actual;
            }
        } finally {
            for (int i = retained.size() - 1; i >= 0; i--) retained.get(i).stream().close();
            if (targetWindows != null) targetWindows.close();
        }
    }

    private static MessageDigest sha() {
        try { return MessageDigest.getInstance("SHA-256"); }
        catch (NoSuchAlgorithmException unavailable) { throw new IllegalStateException(unavailable); }
    }

/** Windows denies file writes/deletes and directory renames while Java reads the pinned bytes. */
private static final class WindowsPin implements AutoCloseable {
    public interface Kernel extends StdCallLibrary {
        Pointer CreateFileW(WString path, int access, int share, Pointer security, int creation, int flags, Pointer template);
        boolean GetFileInformationByHandle(Pointer handle, Info information);
        boolean CloseHandle(Pointer handle);
    }
    @Structure.FieldOrder({"attributes", "creationLow", "creationHigh", "accessLow", "accessHigh", "writeLow", "writeHigh", "volume", "sizeHigh", "sizeLow", "links", "indexHigh", "indexLow"})
    @SuppressWarnings("unused") // Complete native layout includes read-only access timestamps.
    public static class Info extends Structure {
        public int attributes, creationLow, creationHigh, accessLow, accessHigh, writeLow, writeHigh, volume, sizeHigh, sizeLow, links, indexHigh, indexLow;
    }
    private final Kernel kernel = Native.load("kernel32", Kernel.class);
    private final List<Pointer> directories = new ArrayList<>();
    private Pointer file;
    private Info original;
    WindowsPin(Path path) throws IOException { this(path, false); }
    WindowsPin(Path path, boolean directoryOnly) throws IOException {
        try {
            Path cursor = path.getRoot();
            while (true) {
                Pointer handle = open(cursor, 0x80, 3, 0x02200000);
                directories.add(handle);
                Info info = info(handle);
                if ((info.attributes & 0x400) != 0 || (info.attributes & 0x10) == 0)
                    throw new IOException("Scientific file Windows parent is redirected");
                if (cursor.equals(directoryOnly ? path : path.getParent())) break;
                cursor = cursor.resolve(path.getName(cursor.getNameCount()));
            }
            if (directoryOnly) return;
            file = open(path, 0x80000000, 1, 0x00200000);
            original = info(file); validate();
        } catch (Throwable failure) { close(); throw failure; }
    }
    private Pointer open(Path path, int access, int share, int flags) throws IOException {
        Pointer handle = kernel.CreateFileW(new WString(path.toString()), access, share, null, 3, flags, null);
        if (handle == null || Pointer.nativeValue(handle) == -1L)
            throw new IOException("Cannot retain Scientific file Windows handle: " + Native.getLastError());
        return handle;
    }
    private Info info(Pointer handle) throws IOException {
        Info info = new Info();
        if (!kernel.GetFileInformationByHandle(handle, info))
            throw new IOException("Cannot inspect Scientific file Windows handle: " + Native.getLastError());
        return info;
    }
    void validate() throws IOException {
        if (file == null) return;
        Info now = info(file);
        if ((now.attributes & (0x400 | 0x10)) != 0 || now.links != 1 || now.volume != original.volume
                || now.indexHigh != original.indexHigh || now.indexLow != original.indexLow
                || now.sizeHigh != original.sizeHigh || now.sizeLow != original.sizeLow
                || now.writeHigh != original.writeHigh || now.writeLow != original.writeLow)
            throw new IOException("Scientific file Windows file changed or has multiple links");
    }
    @Override public void close() throws IOException {
        IOException failure = null;
        if (file != null && !kernel.CloseHandle(file)) failure = new IOException("Cannot close Scientific file Windows file handle");
        file = null;
        for (int i = directories.size() - 1; i >= 0; i--)
            if (!kernel.CloseHandle(directories.get(i))) failure = new IOException("Cannot close Scientific file Windows parent handle");
        directories.clear();
        if (failure != null) throw failure;
    }
}
}
