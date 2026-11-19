/**
 * We bind coordinate-only annotations once to the selected image/population.
 * Unchanged saved coordinates keep their stable native cell IDs. Added or moved
 * ticket points select their newly indicated cells; coordinate order is not identity.
 * A new reference queries bounded base groups by native centroid and applies
 * the original inclusive Euclidean radius and stable-ID tie rule. Two passes
 * retain only the minimum distance and selected ID, including centroid-in-hole
 * cells. The caller saves the association in the project's existing transaction.
 */
import org.locationtech.jts.geom.Envelope
import org.mif.cells.CellDatasetProvider
import org.mif.cells.NativeCellScope
import java.util.function.Consumer

Closure readReferences = { data, Map reference ->
    if (!(reference.reference_selection_sha256 ==~ /[0-9a-f]{64}/))
        throw new IllegalArgumentException("The reference cell selection must be explicit")
    Map bySection = reference.sections.collectEntries { [(it.section_id): []] }
    Map sections = reference.sections.collectEntries { [(it.section_id): it] }
    Map providers = reference.sections.collectEntries {
        [(it.section_id): NativeCellScope.open(data, it.section_id as String).join()]
    }
    Set<String> cells = [] as Set
    Set<String> ids = [] as Set
    data.hierarchy.annotationObjects.findAll { point ->
        point.ROI?.isPoint() && point.metadata.pipeline_stage == "supervised_training" &&
            point.metadata.get("mif.reference_selection_sha256") == reference.reference_selection_sha256 &&
            point.metadata.get("mif.split") == reference.split
    }.each { point ->
        Map values = point.metadata
        String classId = point.pathClass?.toString()
        String regionId = values.spatial_region_id
        if (values.supervised_image_entry_id != reference.project_entry_id ||
                values.supervised_evidence_set != reference.reference_role ||
                values.slide_id != reference.slide_id || !regionId?.trim() ||
                !(classId in reference.class_ids) || values.supervised_point_bindings == null)
            throw new IllegalStateException("Return the labelled reference draft before freezing this image")
        List bindings = new com.google.gson.Gson().fromJson(values.supervised_point_bindings, List.class)
        List coordinates = point.ROI.allPoints
        if (bindings.size() != coordinates.size())
            throw new IllegalStateException("Reference points changed after their cell identities were saved")
        bindings.eachWithIndex { binding, index ->
            Map section = sections[binding.section_id]
            if (section == null) return
            def coordinate = coordinates[index]
            def provider = providers[binding.section_id]
            def cell = provider.lookup(binding.cell_id as String)
            String pointId = binding.point_id as String
            if (binding.segmentation_generation_id != section.segmentation_generation_id ||
                    !provider.ownsPopulation(binding.segmentation_population_id as String) ||
                    (section.segmentation_population_id != null &&
                        binding.segmentation_population_id != section.segmentation_population_id) ||
                    !pointId?.trim() || !ids.add(pointId) || !cells.add(binding.cell_id as String) ||
                    binding.x_level0 != coordinate.x || binding.y_level0 != coordinate.y || cell == null ||
                    cell.metadata.get("generation_id") != section.segmentation_generation_id ||
                    cell.metadata.get("measurement_generation_id") != section.measurement_generation_id ||
                    !(cell.metadata.get("analysis_domain_id") in section.analysis_domain_ids))
                throw new IllegalStateException("A reference point no longer matches its selected cell population or role")
            bySection[binding.section_id] << [point_id: pointId, cell_id: binding.cell_id,
                slide_id: reference.slide_id, section_id: binding.section_id,
                x_level0: coordinate.x, y_level0: coordinate.y,
                class_id: classId, evidence_set: reference.reference_role, spatial_region_id: regionId]
        }
    }
    bySection.each { sectionId, points ->
        // Reference coverage belongs to the complete plan, not every section.
        points.sort { it.point_id }
    }
    return bySection
}

Closure resolveGrouped = { Map owners, List coordinates ->
    List selected = Collections.nCopies(coordinates.size(), null).toList()
    Map buckets = (0..<coordinates.size()).groupBy { index ->
        def coordinate = coordinates[index]
        [Math.floor((coordinate.x as double)/4096d), Math.floor((coordinate.y as double)/4096d)]
    }
    buckets.values().each { indices ->
        indices.collate(256).each { batchIndices ->
            List batch = batchIndices.collect { coordinates[it] }
            double[] nearest = new double[batch.size()]
            Arrays.fill(nearest, Double.POSITIVE_INFINITY)
            owners.values().each { owner ->
                double[] local = owner.resolver.nearestBatch.call(batch)
                for (int i = 0; i < batch.size(); i++) nearest[i] = Math.min(nearest[i], local[i])
            }
            // Tolerance ties are not transitive. Fix the global minimum across
            // every section before choosing the lexicographically smallest ID.
            double[] limits = new double[batch.size()]
            for (int i = 0; i < batch.size(); i++) limits[i] = nearest[i] + Math.ulp(1d)*16d
            owners.values().each { owner ->
                String[] local = owner.resolver.selectBatch.call(batch, limits)
                for (int i = 0; i < batch.size(); i++) {
                    int index = batchIndices[i]
                    if (local[i] != null && (selected[index] == null || local[i] < selected[index].cell_id))
                        selected[index] = [owner: owner, cell_id: local[i]]
                }
            }
        }
    }
    return selected
}

return [read: readReferences, resolve: resolveGrouped, create: { String imageEntryId, String populationId, CellDatasetProvider provider,
        double radius, Closure eligible = { true } ->
    if (!imageEntryId?.trim() || !populationId?.trim() || !provider.ownsPopulation(populationId) ||
        !Double.isFinite(radius) || radius <= 0d)
        throw new IllegalArgumentException('Reference image, current population and positive radius are required')
    Closure nearestDistance = { double x, double y ->
        Envelope bounds = new Envelope(x-radius, x+radius, y-radius, y+radius)
        double nearest = Double.POSITIVE_INFINITY
        provider.forEachCentroidInBounds(bounds, { cell ->
            if (!eligible(cell)) return
            double distance = Math.hypot(cell.ROI.centroidX-x, cell.ROI.centroidY-y)
            if (distance <= radius && distance < nearest) nearest = distance
        } as Consumer)
        return nearest
    }
    Closure selectWithin = { double x, double y, double tieLimit ->
        Envelope bounds = new Envelope(x-radius, x+radius, y-radius, y+radius)
        String cellId = null
        provider.forEachCentroidInBounds(bounds, { cell ->
            if (!eligible(cell)) return
            double distance = Math.hypot(cell.ROI.centroidX-x, cell.ROI.centroidY-y)
            if (distance <= radius && distance <= tieLimit) {
                String candidate = cell.metadata.get('cell_id')
                if (cellId == null || candidate < cellId) cellId = candidate
            }
        } as Consumer)
        return cellId
    }
    Closure visitBatch = { List coordinates, Closure visit ->
        Envelope bounds = new Envelope()
        coordinates.each { coordinate ->
            double x = coordinate.x, y = coordinate.y
            bounds.expandToInclude(x-radius, y-radius)
            bounds.expandToInclude(x+radius, y+radius)
        }
        provider.forEachCentroidInBounds(bounds, { cell ->
            if (!eligible(cell)) return
            double cx = cell.ROI.centroidX, cy = cell.ROI.centroidY
            for (int i = 0; i < coordinates.size(); i++) {
                double x = coordinates[i].x, y = coordinates[i].y
                // The larger batch envelope must not widen an individual
                // point's original inclusive square or Euclidean radius.
                if (cx < x-radius || cx > x+radius || cy < y-radius || cy > y+radius) continue
                double distance = Math.hypot(cx-x, cy-y)
                if (distance <= radius) visit.call(i, distance, cell)
            }
        } as Consumer)
    }
    Closure nearestBatch = { List coordinates ->
        double[] nearest = new double[coordinates.size()]
        Arrays.fill(nearest, Double.POSITIVE_INFINITY)
        visitBatch.call(coordinates, { int index, double distance, cell ->
            if (distance < nearest[index]) nearest[index] = distance
        })
        return nearest
    }
    Closure selectBatch = { List coordinates, double[] limits ->
        String[] selected = new String[coordinates.size()]
        visitBatch.call(coordinates, { int index, double distance, cell ->
            if (Double.isFinite(limits[index]) && distance <= limits[index]) {
                String candidate = cell.metadata.get('cell_id')
                if (selected[index] == null || candidate < selected[index]) selected[index] = candidate
            }
        })
        return selected
    }
    // The original non-batch model review still owns single-point association.
    // Annotation tickets share each pair of traversals across nearby points.
    Map stats = [spatial_index_builds: 0, legacy_matches: 0, new_bindings: 0]
    Closure bind = { point ->
        Map values = point.metadata
        String cellId = values.get('supervised_cell_id') as String
        if (cellId != null) {
            if (values.get('supervised_image_entry_id') != imageEntryId ||
                    values.get('supervised_segmentation_population_id') != populationId ||
                    provider.lookup(cellId) == null)
                throw new IllegalStateException('Saved supervised reference belongs to a different image or population')
            return cellId
        }
        double x = point.ROI.centroidX, y = point.ROI.centroidY
        double nearest = nearestDistance.call(x, y)
        if (!Double.isFinite(nearest))
            throw new IllegalStateException('Supervised reference has no cell within its inclusive radius')
        cellId = selectWithin.call(x, y, nearest + Math.ulp(1d)*16d)
        values.put('supervised_cell_id', cellId)
        values.put('supervised_image_entry_id', imageEntryId)
        values.put('supervised_segmentation_population_id', populationId)
        stats.legacy_matches++
        stats.new_bindings++
        return cellId
    }
    [nearestBatch: nearestBatch, selectBatch: selectBatch, bind: bind, stats: stats]
}]
