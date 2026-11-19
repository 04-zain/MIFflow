package org.mif.classification;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/** Subject folds and macro-F1; callers own independently fitted native models. */
public final class GroupedCrossValidation {
    public interface Operations {
        Map<String, Object> fit(Map<String, Object> candidate,
                List<Map<String, Object>> training, int seed) throws Exception;
        Map<String, Object> predict(Map<String, Object> model,
                List<Map<String, Object>> held) throws Exception;
        void close(Map<String, Object> model) throws Exception;
    }

    private GroupedCrossValidation() {}

    private static void require(boolean valid, String message) {
        if (!valid) {
            throw new IllegalArgumentException(message);
        }
    }

    private static String digest(String value) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
                    .digest(value.getBytes(StandardCharsets.UTF_8)));
        } catch (NoSuchAlgorithmException error) {
            throw new IllegalStateException(error);
        }
    }

    /** Order subjects by the seeded digest and assign folds round-robin.
     * All entries from one subject stay together, regardless of entry order.
     */
    public static Map<String, Integer> folds(List<Map<String, Object>> entries,
            int requested, int seed) {
        List<String> subjects = entries.stream().map(row -> (String) row.get("subject_id"))
                .distinct().sorted().toList();
        require(subjects.size() >= 2 && subjects.size() <= 512 &&
                subjects.stream().allMatch(s -> s != null && !s.isEmpty() && s.trim().equals(s)),
                "Subject cross-validation requires 2–512 distinct development subjects");
        int count = requested == 0 ? Math.min(5, subjects.size()) : requested;
        require(seed >= 0 && count >= 2 && count <= 10 && count <= subjects.size(),
                "The requested subject fold count is not feasible");
        subjects = new ArrayList<>(subjects);
        subjects.sort((a, b) -> {
            int c = digest(seed + "|" + a).compareTo(digest(seed + "|" + b));
            return c == 0 ? a.compareTo(b) : c;
        });
        Map<String, Integer> assignment = new TreeMap<>();
        for (int i = 0; i < subjects.size(); i++) {
            assignment.put(subjects.get(i), i % count);
        }
        return assignment;
    }

    /** Canonicalize section counts before pooling a subject's observations. */
    @SuppressWarnings("unchecked")
    public static List<List<Long>> matrix(Object value, int width) {
        require(value instanceof List<?> rows && rows.size() == width &&
                rows.stream().allMatch(row -> row instanceof List<?> list && list.size() == width),
                "Confusion matrix class inventory differs");
        List<List<Long>> result = new ArrayList<>();
        for (var row : (List<List<?>>) value) {
            List<Long> counts = new ArrayList<>();
            for (Object item : row) {
                require(item instanceof Number n && n.doubleValue() == n.longValue() &&
                        n.longValue() >= 0 && n.longValue() <= 9007199254740991L,
                        "Confusion counts are invalid");
                long n = ((Number) item).longValue();
                counts.add(n);
            }
            result.add(counts);
        }
        return result;
    }

    /** Normalize each subject's pooled observations before macro-F1.
     * Each subject has equal weight even when its entries contain more pixels.
     */
    public static double score(Map<String, Object> confusions, int width) {
        double[][] weighted = new double[width][width];
        for (String subject : confusions.keySet().stream().sorted().toList()) {
            var counts = matrix(confusions.get(subject), width);
            long total = 0;
            for (var row : counts) {
                for (long n : row) {
                    total += n;
                }
            }
            require(total > 0, "A held-out subject has no evaluable reference observations");
            for (int i = 0; i < width; i++) {
                for (int j = 0; j < width; j++) {
                    weighted[i][j] += (double) counts.get(i).get(j) / (double) total;
                }
            }
        }
        double score = 0;
        for (int i = 0; i < width; i++) {
            double denominator = 0;
            for (int j = 0; j < width; j++) {
                denominator += weighted[i][j] + weighted[j][i];
            }
            if (denominator > 0) {
                score += 2 * weighted[i][i] / denominator;
            }
        }
        return score / width;
    }

    /** Fit each candidate independently on each fold's training subjects.
     * Fold models close after their held-out predictions. Candidates must score
     * the same reference inventory; a tied score keeps the first candidate.
     * The selected candidate is refitted on every development subject, and the
     * caller owns that final model. This method does not publish a classifier.
     */
    @SuppressWarnings("unchecked")
    public static Map<String, Object> run(List<Map<String, Object>> entries,
            List<String> classes, List<Map<String, Object>> candidates,
            int requested, int seed, Operations owner) throws Exception {
        int width = classes.size();
        require(width >= 2 && width <= 128 && new HashSet<>(classes).size() == width,
                "Declared classes must be distinct");
        require(!candidates.isEmpty() && candidates.size() <= 12,
                "Choose 1–12 prespecified candidate settings");
        require(!entries.isEmpty() && entries.stream().map(row -> row.get("entry_id"))
                .distinct().count() == entries.size(), "Development entries must be distinct");
        var assignment = folds(entries, requested, seed);
        var foldIds = assignment.values().stream().distinct().sorted().toList();

        // Admit only folds whose training cohort covers all declared classes.
        for (int fold : foldIds) {
            var observed = new HashSet<>();
            for (var row : entries) {
                if (assignment.get(row.get("subject_id")) != fold) {
                    observed.addAll((List<?>) row.get("classes"));
                }
            }
            require(observed.equals(new HashSet<>(classes)),
                    "A fold training cohort does not cover every declared class; revise references or use fixed settings");
        }
        List<Map<String, Object>> results = new ArrayList<>();
        Map<String, Object> expectedSupport = null;

        // Pool multiple held-out entries from the same subject before scoring.
        for (int candidate = 0; candidate < candidates.size(); candidate++) {
            Map<String, Object> subjects = new TreeMap<>();
            for (int fold : foldIds) {
                var training = entries.stream()
                        .filter(row -> assignment.get(row.get("subject_id")) != fold).toList();
                var held = entries.stream()
                        .filter(row -> assignment.get(row.get("subject_id")) == fold).toList();
                var model = owner.fit(candidates.get(candidate), training, seed);
                try {
                    var predicted = owner.predict(model, held);
                    require(predicted.keySet().equals(new HashSet<>(held.stream()
                            .map(row -> row.get("entry_id")).toList())),
                            "Fold predictions must cover every held-out entry exactly");
                    for (var entry : held) {
                        var counts = matrix(predicted.get(entry.get("entry_id")), width);
                        String subject = (String) entry.get("subject_id");
                        var pooled = (List<List<Long>>) subjects.computeIfAbsent(subject, key -> {
                            var zero = new ArrayList<List<Long>>();
                            for (int i = 0; i < width; i++) {
                                zero.add(new ArrayList<>(java.util.Collections.nCopies(width, 0L)));
                            }
                            return zero;
                        });
                        for (int i = 0; i < width; i++) {
                            for (int j = 0; j < width; j++) {
                                pooled.get(i).set(j, pooled.get(i).get(j) + counts.get(i).get(j));
                            }
                        }
                    }
                } finally {
                    owner.close(model);
                }
            }

            // Reject any candidate that changes held-out class support.
            Map<String, Object> support = new LinkedHashMap<>();
            subjects.forEach((subject, raw) -> support.put(subject,
                    ((List<List<Long>>) raw).stream().map(row -> {
                        long total = 0;
                        for (long n : row) {
                            total += n;
                        }
                        return total;
                    }).toList()));
            require(expectedSupport == null || support.equals(expectedSupport),
                    "Candidate scoring changed the held-out reference inventory");
            expectedSupport = support;
            List<Double> scores = new ArrayList<>();
            for (int fold : foldIds) {
                Map<String, Object> selected = new TreeMap<>();
                subjects.forEach((subject, m) -> {
                    if (assignment.get(subject) == fold) {
                        selected.put(subject, m);
                    }
                });
                scores.add(score(selected, width));
            }
            Map<String, Object> result = new LinkedHashMap<>();
            result.put("candidate_index", candidate);
            result.put("subject_confusions", subjects);
            result.put("fold_scores", scores);
            result.put("selection_score", score(subjects, width));
            results.add(result);
        }

        // Retain declared candidate order for ties, then refit the whole cohort.
        int best = 0;
        for (int i = 1; i < results.size(); i++) {
            if ((double) results.get(i).get("selection_score") >
                    (double) results.get(best).get("selection_score")) {
                best = i;
            }
        }
        Map<String, Object> report = new LinkedHashMap<>();
        report.put("schema", "mif-grouped-cv-selection/v1");
        report.put("scoring", "subject_weighted_macro_f1/v1");
        report.put("folds_by_subject", assignment);
        report.put("classes", classes);
        report.put("candidates", results);
        report.put("selected_candidate_index", best);
        report.put("selection_score", results.get(best).get("selection_score"));
        report.put("refit_subject_ids", new ArrayList<>(assignment.keySet()));
        report.put("refit_seed", seed);
        report.put("learned_chain_refit_per_fold", true);
        report.put("score_interpretation", "tuning_not_independent_test");
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("final_model", owner.fit(candidates.get(best), entries, seed));
        result.put("report", report);
        return result;
    }
}
