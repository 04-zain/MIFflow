package org.mif.cells;

import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.function.BiConsumer;

/** External ordering of compact candidate memberships without retaining PathObjects. */
public final class SortedCellIds implements AutoCloseable {
    private record Entry(int group, String id, String value) {}
    private static final int ROWS = 8192;
    private static final int FAN_IN = 64;
    private static final Comparator<String> IDS = (left, right) -> {
        int a = 0, b = 0;
        while (a < left.length() && b < right.length()) {
            int x = left.codePointAt(a), y = right.codePointAt(b);
            if (x != y) {
                return Integer.compare(x, y);
            }
            a += Character.charCount(x);
            b += Character.charCount(y);
        }
        return Integer.compare(left.length() - a, right.length() - b);
    };
    private static final Comparator<Entry> ENTRIES = Comparator.comparingInt(Entry::group)
            .thenComparing(Entry::id, IDS);

    /** A grouped stream retains candidate order without encoding it into a cell ID. */
    @FunctionalInterface
    public interface GroupedIdentityConsumer {
        void accept(int group, String id, String value) throws IOException;
    }

    private final Path root;
    private final List<Entry> pending = new ArrayList<>(ROWS);
    private List<Path> runs = new ArrayList<>();
    private int ordinal;

    public SortedCellIds(Path parent) throws IOException {
        Files.createDirectories(parent);
        root = Files.createTempDirectory(parent, ".semantic-ids-");
    }

    public void add(String id, boolean nucleus) throws IOException {
        addIdentity(id, Boolean.toString(nucleus));
    }

    /** We order compact identity values with the same bounded spill as memberships. */
    public void addIdentity(String id, String value) throws IOException {
        addGroupedIdentity(0, id, value);
    }

    /** All candidate groups share the same pending-row and merge bounds. */
    public void addGroupedIdentity(int group, String id, String value) throws IOException {
        if (group < 0) {
            throw new IllegalArgumentException("Candidate membership group must be nonnegative");
        }
        if (id == null || id.isBlank() || id.getBytes(StandardCharsets.UTF_8).length > 1024) {
            throw new IllegalArgumentException("Candidate membership requires a bounded stable cell ID");
        }
        if (value == null || value.isBlank() || value.getBytes(StandardCharsets.UTF_8).length > 1024) {
            throw new IllegalArgumentException("Cell identity requires a bounded value");
        }
        pending.add(new Entry(group, id, value));
        if (pending.size() == ROWS) {
            flush();
        }
    }

    /** Match the canonical ASCII JSON encoding of the base dataset ID summary. */
    public static String canonicalId(String id) {
        var result = new StringBuilder("\"");
        for (int i = 0; i < id.length(); i++) {
            char value = id.charAt(i);
            switch (value) {
                case '\"' -> result.append("\\\"");
                case '\\' -> result.append("\\\\");
                case '\b' -> result.append("\\b");
                case '\f' -> result.append("\\f");
                case '\n' -> result.append("\\n");
                case '\r' -> result.append("\\r");
                case '\t' -> result.append("\\t");
                default -> {
                    if (value < 32 || value > 126) {
                        result.append(String.format("\\u%04x", (int) value));
                    } else {
                        result.append(value);
                    }
                }
            }
        }
        return result.append('\"').toString();
    }

    private static void write(DataOutputStream output, Entry entry) throws IOException {
        output.writeInt(entry.group);
        byte[] text = entry.id.getBytes(StandardCharsets.UTF_8);
        output.writeInt(text.length);
        output.write(text);
        byte[] value = entry.value.getBytes(StandardCharsets.UTF_8);
        output.writeInt(value.length);
        output.write(value);
    }

    private static Entry read(DataInputStream input) throws IOException {
        int group;
        try {
            group = input.readInt();
        } catch (EOFException complete) {
            return null;
        }
        if (group < 0) {
            throw new IOException("Invalid internal membership group");
        }
        int size = input.readInt();
        if (size < 1 || size > 1024) {
            throw new IOException("Invalid internal membership spill");
        }
        byte[] text = input.readNBytes(size);
        if (text.length != size) {
            throw new EOFException("Truncated internal membership spill");
        }
        int valueSize = input.readInt();
        if (valueSize < 1 || valueSize > 1024) {
            throw new IOException("Invalid internal identity value");
        }
        byte[] value = input.readNBytes(valueSize);
        if (value.length != valueSize) {
            throw new EOFException("Truncated internal identity value");
        }
        return new Entry(group, new String(text, StandardCharsets.UTF_8),
                new String(value, StandardCharsets.UTF_8));
    }

    private Path nextRun() {
        return root.resolve("run-" + ordinal++ + ".bin");
    }

    private void flush() throws IOException {
        if (pending.isEmpty()) {
            return;
        }
        pending.sort(ENTRIES);
        Path path = nextRun();
        try (var output = new DataOutputStream(new BufferedOutputStream(Files.newOutputStream(path)))) {
            for (Entry entry : pending) {
                write(output, entry);
            }
        }
        runs.add(path);
        pending.clear();
    }

    private static final class Cursor implements AutoCloseable {
        private final DataInputStream input;
        private Entry value;
        Cursor(Path path) throws IOException {
            input = new DataInputStream(new BufferedInputStream(Files.newInputStream(path)));
            try {
                value = read(input);
            } catch (IOException failure) {
                try {
                    input.close();
                } catch (IOException closeFailure) {
                    failure.addSuppressed(closeFailure);
                }
                throw failure;
            }
        }
        public void close() throws IOException {
            input.close();
        }
    }

    private static void merge(List<Path> paths, GroupedIdentityConsumer consumer) throws IOException {
        var opened = new ArrayList<Cursor>();
        var queue = new PriorityQueue<Cursor>(Comparator.comparing(cursor -> cursor.value, ENTRIES));
        try {
            for (Path path : paths) {
                var cursor = new Cursor(path);
                opened.add(cursor);
                if (cursor.value != null) {
                    queue.add(cursor);
                }
            }
            Entry previous = null;
            while (!queue.isEmpty()) {
                var cursor = queue.remove();
                Entry value = cursor.value;
                if (previous != null && value.group == previous.group && value.id.equals(previous.id)) {
                    throw new IOException("Candidate membership cell ID is duplicated");
                }
                consumer.accept(value.group, value.id, value.value);
                previous = value;
                cursor.value = read(cursor.input);
                if (cursor.value != null) {
                    queue.add(cursor);
                }
            }
        } finally {
            IOException closeFailure = null;
            for (Cursor cursor : opened) {
                try {
                    cursor.close();
                } catch (IOException failure) {
                    if (closeFailure == null) {
                        closeFailure = failure;
                    } else {
                        closeFailure.addSuppressed(failure);
                    }
                }
            }
            if (closeFailure != null) {
                throw closeFailure;
            }
        }
    }

    public void forEach(BiConsumer<String, Boolean> consumer) throws IOException {
        forEachIdentity((id, value) -> consumer.accept(id, Boolean.parseBoolean(value)));
    }

    /** We stream sorted unique identity pairs without retaining the population. */
    public void forEachIdentity(BiConsumer<String, String> consumer) throws IOException {
        forEachGroupedIdentity((group, id, value) -> consumer.accept(id, value));
    }

    /** Stream candidate ordinal then Unicode cell ID, permitting membership in multiple groups. */
    public void forEachGroupedIdentity(GroupedIdentityConsumer consumer) throws IOException {
        flush();
        while (runs.size() > FAN_IN) {
            var next = new ArrayList<Path>();
            for (int start = 0; start < runs.size(); start += FAN_IN) {
                var group = runs.subList(start, Math.min(start + FAN_IN, runs.size()));
                Path merged = nextRun();
                try (var output = new DataOutputStream(new BufferedOutputStream(Files.newOutputStream(merged)))) {
                    merge(group, (ordinal, id, value) -> write(output, new Entry(ordinal, id, value)));
                }
                next.add(merged);
                for (Path path : group) {
                    Files.delete(path);
                }
            }
            runs = next;
        }
        merge(runs, consumer);
    }

    public void close() throws IOException {
        try (var files = Files.list(root)) {
            for (Path path : files.toList()) {
                Files.deleteIfExists(path);
            }
        } finally {
            Files.deleteIfExists(root);
        }
    }
}
