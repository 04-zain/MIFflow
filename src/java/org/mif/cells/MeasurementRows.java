package org.mif.cells;

import java.awt.image.BufferedImage;
import java.io.IOException;
import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import qupath.lib.analysis.features.ObjectMeasurements;
import qupath.lib.images.servers.ImageServer;
import qupath.lib.objects.PathObject;
import qupath.lib.objects.PathCellObject;

/**
 * Calculate one admitted cell's numeric measurements and typed population row.
 * The caller owns current-cell/source admission and component publication. This
 * helper borrows the image server and geometries; it retains only request-sized
 * dictionaries and the successful row count, with no cell cache or native
 * allocation ownership.
 */
public final class MeasurementRows {
    private final ImageServer<BufferedImage> server;
    private final Collection<ObjectMeasurements.Measurements> statistics;
    private final Collection<ObjectMeasurements.Compartments> compartments;
    private final PopulationMembership membership;
    private final Map<String, Map<String, Integer>> categoryCodes;
    private final String runId;
    private final String generationId;
    private final List<String> measurementNames;
    private long rowCount;

    /** Freeze the decoded selection once; source and request validation stay with the stage. */
    public MeasurementRows(ImageServer<BufferedImage> server,
            Collection<ObjectMeasurements.Measurements> statistics,
            Collection<ObjectMeasurements.Compartments> compartments,
            List<String> measurementNames, PopulationMembership membership,
            Map<String, Map<String, Integer>> categoryCodes, String runId, String generationId) {
        this.server = server;
        this.statistics = List.copyOf(statistics);
        this.compartments = List.copyOf(compartments);
        this.membership = membership;
        this.categoryCodes = new LinkedHashMap<>();
        categoryCodes.forEach((kind, codes) -> this.categoryCodes.put(kind, Map.copyOf(codes)));
        this.runId = runId;
        this.generationId = generationId;
        this.measurementNames = List.copyOf(measurementNames);
    }

    /**
     * Use QuPath's existing full-resolution statistic/compartment union, then
     * read selected values in request order. Edited outlines receive fresh
     * morphology. A missing or nonfinite value rejects the row before it
     * can contribute to the successful publication count.
     */
    public Map<String, Object> calculate(PathObject cell) throws IOException {
        if (Long.parseLong(cell.getMetadata().getOrDefault("mif.geometry_revision", "0")) > 0L) {
            ObjectMeasurements.addShapeMeasurements(cell, server.getPixelCalibration(),
                    ObjectMeasurements.ShapeFeatures.values());
        }
        if (!statistics.isEmpty()) {
            ObjectMeasurements.addIntensityMeasurements(server, cell, 1.0d, statistics, compartments);
        }
        var measurements = cell.getMeasurements();
        Map<String, Object> result = new LinkedHashMap<>();
        for (String name : measurementNames) {
            Object value = isDerivedShapeMeasurement(name) ? derivedShapeMeasurement(cell, name) : measurements.get(name);
            if (!(value instanceof Number number) || !Double.isFinite(number.doubleValue())) {
                throw new IllegalStateException("The selected measurement '" + name
                        + "' is unavailable for cell " + cell.getMetadata().getOrDefault("cell_id", cell.getID().toString())
                        + " at level-0 pixel coordinates (" + cell.getROI().getCentroidX()
                        + ", " + cell.getROI().getCentroidY() + "). Inspect this cell and its requested measurement. "
                        + "For an unusable fragment, open Cell measurements > Edit options > Remove cells in QuPath; "
                        + "delete it, save and close QuPath, then recalculate measurements. "
                        + "Otherwise review the measurement selection or segmentation. "
                        + "An intensity region covering no pixel centres has no value.");
            }
            result.put("measurement:" + name, number.doubleValue());
        }

        var selected = membership.select(cell.getROI().getGeometry().getCentroid(),
                cell.getMetadata().get("analysis_domain_id"));
        result.put("population:detected_tissue", selected.detectedTissue());
        result.put("population:primary_structure", codes("primary_structure", selected.primaryStructure()));
        result.put("population:secondary_structure", codes("secondary_structure", selected.secondaryStructure()));
        result.put("measurement_run_id", runId);
        result.put("measurement_generation_id", generationId);
        result.put("mif.measurements_stale", false);
        rowCount++;
        return result;
    }

    /** Identify our three optional geometry features for calculation and saved provenance. */
    public static boolean isDerivedShapeMeasurement(String name) {
        return name.equals("Cell: Diameter ratio") || name.equals("Nucleus: Diameter ratio")
                || name.equals("Nucleus: Centroid offset µm");
    }

    /**
     * Calculate only a selected feature, borrowing finalized outlines and calibrated
     * shape measurements. Diameter ratio uses QuPath's maximum/minimum diameters;
     * it is not an ellipse-axis ratio. Nuclear offset is the Euclidean distance
     * between area centroids after applying X/Y pixel calibration independently.
     * Missing nuclei and degenerate diameters remain unavailable, never zero-filled.
     * No image pixels or neighbouring cells are read.
     */
    private double derivedShapeMeasurement(PathObject cell, String name) {
        if (name.endsWith(": Diameter ratio")) {
            String prefix = name.substring(0, name.indexOf(':'));
            var values = cell.getMeasurements();
            Number maximum = values.get(prefix + ": Max diameter µm");
            Number minimum = values.get(prefix + ": Min diameter µm");
            return maximum != null && minimum != null && minimum.doubleValue() > 0d
                    ? maximum.doubleValue() / minimum.doubleValue() : Double.NaN;
        }
        if (!(cell instanceof PathCellObject object) || object.getNucleusROI() == null)
            return Double.NaN;
        var calibration = server.getPixelCalibration();
        if (!calibration.hasPixelSizeMicrons())
            throw new IllegalStateException("Nuclear centre distance requires the image pixel size in micrometres");
        var nucleus = object.getNucleusROI();
        var outline = cell.getROI();
        return Math.hypot((nucleus.getCentroidX() - outline.getCentroidX()) * calibration.getPixelWidthMicrons(),
                (nucleus.getCentroidY() - outline.getCentroidY()) * calibration.getPixelHeightMicrons());
    }

    /** Convert the selected sorted labels using the stage's admitted fixed dictionary. */
    private List<Integer> codes(String kind, List<String> labels) {
        var dictionary = categoryCodes.get(kind);
        var result = new ArrayList<Integer>(labels.size());
        for (String label : labels) {
            result.add(dictionary.get(label));
        }
        return result;
    }

    /** Successful rows alone define the count used by the existing component publication check. */
    public long rowCount() {
        return rowCount;
    }

}
