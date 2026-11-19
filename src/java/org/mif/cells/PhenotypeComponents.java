package org.mif.cells;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import java.io.IOException;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeSet;
import qupath.lib.images.ImageData;

/** Typed phenotype values share the existing cell layout and geometry. */
public final class PhenotypeComponents {
    private static final Gson JSON = new com.google.gson.GsonBuilder().serializeNulls().create();
    private static final List<String> FIELDS = List.of("primary_phenotype", "phenotype_source",
            "phenotype_components", "marker_states", "phenotype_confidence", "cluster_id", "cluster_label");
    private PhenotypeComponents() {}

    /**
     * We read one owned assignment task while hashing its exact UTF-8 bytes.
     * Python and Java therefore share the same identity without reserializing
     * floating-point values or Unicode labels. The returned ID index owns each
     * parsed row once; completed results are subsequently read from Parquet.
     */
    public static Map<String,Map<String,Object>> readAssignments(Map<String,Object> source,
            String expectedSha) throws IOException {
        if (source == null || !source.keySet().equals(java.util.Set.of("path", "sha256"))
                || !expectedSha.equals(source.get("sha256")))
            throw new IllegalArgumentException("The assignment task differs from the prepared view");
        Path path = Path.of((String)source.get("path"));
        if (!path.isAbsolute() || java.nio.file.Files.size(path) > Runtime.getRuntime().maxMemory() / 16)
            throw new IllegalArgumentException("The assignment task exceeds its admitted native memory allowance");
        java.security.MessageDigest digest;
        try { digest = java.security.MessageDigest.getInstance("SHA-256"); }
        catch (java.security.NoSuchAlgorithmException impossible) { throw new AssertionError(impossible); }
        Map<String,Map<String,Object>> rows = new LinkedHashMap<>();
        try (var input = new java.security.DigestInputStream(java.nio.file.Files.newInputStream(path), digest);
                var reader = new com.google.gson.stream.JsonReader(new java.io.InputStreamReader(input,
                        java.nio.charset.StandardCharsets.UTF_8))) {
            reader.beginArray();
            String previous = null;
            while (reader.hasNext()) {
                @SuppressWarnings("unchecked") Map<String,Object> row = JSON.fromJson(reader, Map.class);
                Object rawId = row.get("cell_id");
                if (!(rawId instanceof String id) || id.isBlank() || previous != null && previous.compareTo(id) >= 0)
                    throw new IllegalArgumentException("Assignment cells must be unique and sorted by stable ID");
                rows.put(id, row); previous = id;
            }
            reader.endArray();
            if (reader.peek() != com.google.gson.stream.JsonToken.END_DOCUMENT)
                throw new IllegalArgumentException("The assignment task contains trailing content");
        }
        if (!java.util.HexFormat.of().formatHex(digest.digest()).equals(expectedSha))
            throw new IOException("The assignment task changed from its prepared identity");
        return rows;
    }

    /**
     * We write the accepted assignments in immutable base order, checking each
     * cell's current domain as it passes through the bounded native reader.
     * Category names become Int32 codes; the view identity is recorded once in
     * the descriptor. Historical view fields have their own namespace. Optional
     * display fields select the same values without changing other saved views.
     *
     * The returned descriptor is complete but not selected. Its caller owns the
     * saved-image transaction. Unknown, missing or differently scoped cells stop
     * that transaction; this method never inserts automatic cells in a hierarchy.
     */
    public static Path publish(ImageData<?> image, String sectionId, Path output,
            Map<String,Object> reference, String runId,
            Map<String,Map<String,Object>> assignments) throws IOException {
        var provider = NativeCellScope.open(image, sectionId).join();
        String mode = (String)reference.get("mode");
        String generation = (String)reference.get("phenotype_generation_id");
        String prefix = "phenotype_view/" + mode + "/" + generation + "/";
        if (mode == null || mode.isBlank() || generation == null || generation.isBlank())
            throw new IllegalArgumentException("A phenotype component requires its selected view and generation");
        if (((Number)reference.get("cell_count")).longValue() != assignments.size())
            throw new IllegalArgumentException("Phenotype view count differs from its accepted assignments");
        // An unchanged accepted component retains both its files and manual-edit association.
        Object selected = image.getProperty("mif.cell_components");
        if (selected instanceof String text) {
            var references = com.google.gson.JsonParser.parseString(text).getAsJsonObject();
            var scopes = references.getAsJsonObject(prefix + "primary_phenotype");
            if (scopes != null && scopes.has(sectionId)) {
                Path previousPath = Path.of(scopes.get(sectionId).getAsString());
                var previous = com.google.gson.JsonParser.parseString(java.nio.file.Files.readString(previousPath)).getAsJsonObject();
                if (previous.get("request_sha256").getAsString().equals(reference.get("artifact_sha256"))
                        && previous.get("geometry_revision").getAsLong() == provider.getGeometryRevision())
                    return previousPath;
            }
        }
        // i) Freeze compact dictionaries from the already accepted assignments.
        var columns = new LinkedHashMap<String,String>();
        var dictionaries = new JsonObject();
        var codes = new LinkedHashMap<String,Map<String,Integer>>();
        for (String field : FIELDS) {
            String type = field.equals("phenotype_confidence") ? "float64"
                    : field.equals("marker_states") ? "string"
                    : field.equals("phenotype_components") ? "list_int32" : "int32";
            columns.put(prefix + field, type);
            if (type.equals("int32") || type.equals("list_int32")) {
                var names = new TreeSet<String>();
                for (var row : assignments.values()) {
                    Object value = row.get(field);
                    if (value instanceof List<?> values) for (Object name : values) names.add((String)name);
                    else if (value != null) names.add((String)value);
                }
                var mapping = new LinkedHashMap<String,Integer>();
                for (String name : names) mapping.put(name, mapping.size());
                codes.put(field, mapping);
                dictionaries.add(prefix + field, dictionary(prefix + field, mapping));
            }
        }
        var attributes = new JsonObject();
        var view = new JsonObject();
        for (String field : List.of("mode", "phenotype_generation_id", "artifact_sha256", "lineage_sha256"))
            view.addProperty(field, (String)reference.get(field));
        view.addProperty("run_id", runId);
        view.addProperty("cell_count", assignments.size());
        view.addProperty("cell_ids_sha256", (String)reference.get("cell_ids_sha256"));
        view.addProperty("cell_to_domain_membership_sha256", (String)reference.get("cell_to_domain_membership_sha256"));
        attributes.add("phenotype_view", view);
        // ii) Join this generation's complete value set for its immutable-identity
        // check. Other historical generations and display aliases are not inputs.
        // Sparse manual edits and base lineage retain their existing association.
        long[] seen = {0};
        Path descriptor = provider.writeComponent(output, (String)reference.get("artifact_sha256"),
                sectionId, null, columns.keySet(), columns, dictionaries, cell -> {
            String id = cell.getMetadata().get("cell_id");
            var assignment = assignments.get(id);
            if (assignment == null || !cell.getMetadata().get("analysis_domain_id").equals(assignment.get("analysis_domain_id")))
                throw new IllegalArgumentException("Phenotype assignments differ from current cell/domain identity: " + id);
            String saved = cell.getMetadata().get("phenotype_views");
            if (saved != null) {
                var history = com.google.gson.JsonParser.parseString(saved).getAsJsonObject();
                var generations = history.getAsJsonObject(mode);
                if (generations != null && generations.has(generation)) {
                    var previous = generations.getAsJsonObject(generation);
                    if (!reference.get("artifact_sha256").equals(previous.get("artifact_sha256").getAsString())
                            || !reference.get("lineage_sha256").equals(previous.get("lineage_sha256").getAsString()))
                        throw new IllegalArgumentException("An accepted phenotype generation cannot change its saved identity");
                    for (String field : FIELDS)
                        if (!JSON.toJsonTree(assignment.get(field)).equals(previous.get(field)))
                            throw new IllegalArgumentException("An accepted phenotype generation cannot change its cell values");
                }
            }
            var row = new LinkedHashMap<String,Object>();
            for (String field : FIELDS) {
                Object value = assignment.get(field);
                if (field.equals("marker_states")) value = JSON.toJson(value);
                else if (codes.containsKey(field) && value != null) {
                    var mapping = codes.get(field);
                    if (value instanceof List<?> names) {
                        var encoded = new ArrayList<Integer>();
                        for (Object name : names) encoded.add(mapping.get((String)name));
                        value = encoded;
                    } else value = mapping.get((String)value);
                }
                row.put(prefix + field, value);
            }
            seen[0]++;
            return row;
        }, attributes);
        if (seen[0] != assignments.size())
            throw new IOException("Phenotype assignments include cells outside the current population");
        return descriptor;
    }

    /** We record each distinct label once, with contiguous codes local to its field. */
    private static JsonObject dictionary(String field, Map<String,Integer> codes) {
        var result = new JsonObject();
        result.addProperty("field", field);
        result.addProperty("dictionary_id", "phenotype:" + field);
        result.addProperty("version", 1);
        var entries = new JsonArray();
        for (var pair : codes.entrySet()) {
            var entry = new JsonObject();
            entry.addProperty("code", pair.getValue());
            entry.addProperty("label_id", pair.getKey());
            entry.addProperty("name", pair.getKey());
            entry.add("color", com.google.gson.JsonNull.INSTANCE);
            entries.add(entry);
        }
        result.add("entries", entries);
        return result;
    }
}
