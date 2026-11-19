package org.mif.roi;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import org.locationtech.jts.geom.Geometry;
import org.locationtech.jts.geom.Point;
import org.locationtech.jts.geom.util.AffineTransformation;
import org.locationtech.jts.index.strtree.STRtree;
import org.locationtech.jts.operation.union.UnaryUnionOp;
import org.mif.cells.SortedCellIds;

/** Membership and calibrated geometry facts for already admitted semantic candidates. */
public final class SemanticCandidateEvidence implements AutoCloseable {
    /** Candidate ordinals come from the stage's existing canonical ROI-ID order. */
    private record Candidate(int ordinal, Geometry geometry) {}

    /** The member digest retains the canonical sorted stable-ID array, including empty arrays. */
    public record Membership(String cellInventorySha256, int cellCount) {}

    /** Source metadata and geometry identity digests remain the caller's responsibility. */
    public record GeometryFacts(double areaPx2, int componentCount,
                                Map<String, Double> distanceToPrimaryUm,
                                Map<String, Map<String, Double>> overlapFractionWithPrimary,
                                Double minimumBoundaryDistanceUm) {}

    @FunctionalInterface
    public interface RelationConsumer {
        void accept(int candidateOrdinal, String cellId, boolean hasNucleus) throws IOException;
    }

    private final List<Geometry> candidates;
    private final STRtree candidateIndex = new STRtree();
    private final Map<String, Geometry> primaryGeometryByClass = new LinkedHashMap<>();
    private final AffineTransformation physicalTransform;
    private final SortedCellIds members;

    /**
     * Borrow admitted geometries and preserve the caller's primary union order.
     * One sorter bounds all candidate memberships together; no cells are retained.
     */
    public SemanticCandidateEvidence(List<Geometry> candidateGeometries,
                                     Map<String, List<Geometry>> primariesByClass,
                                     double xUmPerPx, double yUmPerPx,
                                     Path scratchParent) throws IOException {
        candidates = List.copyOf(candidateGeometries);
        physicalTransform = AffineTransformation.scaleInstance(xUmPerPx, yUmPerPx);
        for (int ordinal = 0; ordinal < candidates.size(); ordinal++) {
            Geometry geometry = candidates.get(ordinal);
            if (!geometry.isEmpty()) {
                candidateIndex.insert(geometry.getEnvelopeInternal(), new Candidate(ordinal, geometry));
            }
        }
        candidateIndex.build();
        for (var entry : primariesByClass.entrySet()) {
            var calibrated = new ArrayList<Geometry>(entry.getValue().size());
            for (Geometry geometry : entry.getValue()) {
                calibrated.add(physicalTransform.transform(geometry));
            }
            primaryGeometryByClass.put(entry.getKey(), UnaryUnionOp.union(calibrated));
        }
        members = new SortedCellIds(scratchParent);
    }

    /** Compute a cell centroid once in the caller; covers still includes exact boundaries. */
    public void include(Point centroid, String cellId, boolean hasNucleus) throws IOException {
        for (Object raw : candidateIndex.query(centroid.getEnvelopeInternal())) {
            var candidate = (Candidate) raw;
            if (candidate.geometry().covers(centroid)) {
                members.addGroupedIdentity(candidate.ordinal(), cellId, Boolean.toString(hasNucleus));
            }
        }
    }

    /**
     * Emit the original candidate-then-cell relation order and form each digest
     * while streaming it. Per-candidate state contains only a digest and count.
     */
    public List<Membership> forEachRelation(RelationConsumer consumer) throws IOException {
        var digests = new ArrayList<MessageDigest>(candidates.size());
        int[] counts = new int[candidates.size()];
        for (int ordinal = 0; ordinal < candidates.size(); ordinal++) {
            MessageDigest digest = sha256();
            digest.update((byte) '[');
            digests.add(digest);
        }
        members.forEachGroupedIdentity((ordinal, cellId, value) -> {
            consumer.accept(ordinal, cellId, Boolean.parseBoolean(value));
            MessageDigest digest = digests.get(ordinal);
            if (counts[ordinal] != 0) {
                digest.update((byte) ',');
            }
            digest.update(SortedCellIds.canonicalId(cellId).getBytes(StandardCharsets.UTF_8));
            counts[ordinal]++;
        });
        var result = new ArrayList<Membership>(candidates.size());
        for (int ordinal = 0; ordinal < candidates.size(); ordinal++) {
            MessageDigest digest = digests.get(ordinal);
            digest.update((byte) ']');
            result.add(new Membership(HexFormat.of().formatHex(digest.digest()), counts[ordinal]));
        }
        return List.copyOf(result);
    }

    /** Preserve the original calibrated JTS operations and per-class fraction order. */
    public GeometryFacts geometryFacts(int candidateOrdinal) {
        Geometry geometry = candidates.get(candidateOrdinal);
        Geometry calibratedCandidate = physicalTransform.transform(geometry);
        var distances = new TreeMap<String, Double>();
        var overlaps = new TreeMap<String, Map<String, Double>>();
        var boundaryDistances = new ArrayList<Double>();
        for (var entry : primaryGeometryByClass.entrySet()) {
            Geometry primary = entry.getValue();
            distances.put(entry.getKey(), calibratedCandidate.distance(primary));
            boundaryDistances.add(calibratedCandidate.getBoundary().distance(primary.getBoundary()));
            double intersectionArea = calibratedCandidate.intersection(primary).getArea();
            var fractions = new LinkedHashMap<String, Double>();
            fractions.put("candidate_area", calibratedCandidate.getArea() > 0.0
                    ? intersectionArea / calibratedCandidate.getArea() : 0.0);
            fractions.put("primary_area", primary.getArea() > 0.0
                    ? intersectionArea / primary.getArea() : 0.0);
            overlaps.put(entry.getKey(), fractions);
        }
        Double minimum = boundaryDistances.isEmpty() ? null : Collections.min(boundaryDistances);
        return new GeometryFacts(geometry.getArea(), geometry.getNumGeometries(),
                distances, overlaps, minimum);
    }

    private static MessageDigest sha256() {
        try {
            return MessageDigest.getInstance("SHA-256");
        } catch (NoSuchAlgorithmException impossible) {
            throw new IllegalStateException("SHA-256 is unavailable", impossible);
        }
    }

    /** Release only the helper's private spill files, including failed exports. */
    @Override
    public void close() throws IOException {
        members.close();
    }
}
