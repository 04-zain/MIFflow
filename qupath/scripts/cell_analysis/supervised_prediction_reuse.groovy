/**
 * Opens reviewed native predictions or performs their initial calculation.
 *
 * We read the accepted prediction file once at this process boundary. Its
 * complete-file digest protects the stored rows; the selected input identities
 * and exact cell IDs protect their association with the current image, model
 * and measurements. Accepting those rows never invokes the inference closure.
 */
import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.nio.file.Path
import java.security.MessageDigest
import com.google.gson.GsonBuilder
import com.google.gson.ToNumberPolicy

return [readOrPredict: { Map reference, Map identity, List<String> cellIds,
                         List<String> classIds, Closure inference,
                         String rowField = 'rows' ->
    if (!(rowField in ['rows', 'predictions']))
        throw new IllegalArgumentException('Prediction owner must select its current row field')
    if (reference == null) return [predictions: inference.call()]
    if (reference.keySet() != ['path', 'sha256', 'byte_count'] as Set ||
        !(reference.path instanceof String) ||
        !(reference.sha256 ==~ /[0-9a-f]{64}/) ||
        !(reference.byte_count instanceof Number) ||
        (reference.byte_count as Number).longValue() <= 0L)
        throw new IllegalArgumentException('Stored prediction file reference is invalid')
    Path source = Path.of(reference.path as String).toAbsolutePath().normalize()
    if (!Files.isRegularFile(source) ||
        Files.size(source) != (reference.byte_count as Number).longValue())
        throw new IllegalStateException('Stored prediction bytes changed or are unavailable')
    byte[] bytes = Files.readAllBytes(source)
    String digest = MessageDigest.getInstance('SHA-256').digest(bytes).encodeHex().toString()
    if (bytes.length != (reference.byte_count as Number).longValue() || digest != reference.sha256)
        throw new IllegalStateException('Stored prediction bytes changed')
    def gson = new GsonBuilder().setObjectToNumberStrategy(ToNumberPolicy.LONG_OR_DOUBLE).create()
    Map stored = gson.fromJson(new String(bytes, StandardCharsets.UTF_8), Map) as Map
    if (stored == null)
        throw new IllegalStateException('Stored prediction current inputs differ')
    def changedInput = identity.find { key, value -> stored[key] != value }
    if (changedInput != null)
        throw new IllegalStateException('Stored prediction current inputs differ: ' + changedInput.key)
    if (!(stored[rowField] instanceof List) || stored.cell_count != cellIds.size() ||
        (stored[rowField] as List).collect { it.cell_id } != cellIds)
        throw new IllegalStateException('Stored prediction cell population differs')
    List<Map> rows = stored[rowField] as List<Map>
    if (rows.any { row ->
        !(row instanceof Map) || row.keySet() != ['cell_id', 'class_id', 'confidence'] as Set ||
        !(row.class_id in classIds) || !(row.confidence instanceof Number) ||
        !Double.isFinite((row.confidence as Number).doubleValue()) ||
        (row.confidence as Number).doubleValue() < 0d ||
        (row.confidence as Number).doubleValue() > 1d
    } || !(stored.rows_sha256 ==~ /[0-9a-f]{64}/))
        throw new IllegalStateException('Stored prediction classes or confidence values are invalid')
    [predictions: rows, rows_sha256: stored.rows_sha256,
     prediction_sha256: digest, prediction_byte_count: bytes.length]
}]
