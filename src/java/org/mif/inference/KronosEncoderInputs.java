package org.mif.inference;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Restore the declared KRONOS input types at QuPath's saved-map boundary.
 * Gson omits null map entries and reads untyped integers as doubles. Required
 * missing fields and present invalid values remain for the normal validator.
 */
public final class KronosEncoderInputs {
    private KronosEncoderInputs() {}

    public static Map<String, Object> saved(Map<String, Object> source) {
        Map<String, Object> result = new LinkedHashMap<>(source);
        result.putIfAbsent("marker_text_model", null);
        result.putIfAbsent("nuclear_channel", null);
        integral(result, "patch_size", 32, 256, true);
        Object markers = result.get("marker_names");
        if (markers instanceof List<?> names)
            integral(result, "nuclear_channel", 0, names.size() - 1, false);
        integral(result, "threads", 1, 8, false);
        integral(result, "activation_budget_bytes", 1, (1L << 53) - 1, false);
        return result;
    }

    /** Recover only exact valid integer values; never truncate a saved fraction. */
    private static void integral(Map<String, Object> values, String field,
            long minimum, long maximum, boolean patchSize) {
        Object value = values.get(field);
        if (!(value instanceof Double) && !(value instanceof Float))
            return;
        double number = ((Number) value).doubleValue();
        if (!Double.isFinite(number) || number != Math.rint(number)
                || number < minimum || number > maximum)
            return;
        if (patchSize && number != 32 && number != 64 && number != 128 && number != 256)
            return;
        values.put(field, (long) number);
    }
}
