/** Authenticate the complete accepted Task 6 set and exact live semantic objects. */

import java.nio.charset.StandardCharsets
import java.security.MessageDigest
import java.nio.file.Files
import java.nio.file.Path
import java.util.function.Consumer
import java.util.function.BiConsumer
import org.mif.roi.RoiInventory
import org.mif.cells.NativeCellScope
import org.mif.cells.CellDatasetProvider
import org.mif.cells.SortedCellIds
import java.util.Collections
import java.util.IdentityHashMap
import org.locationtech.jts.geom.Geometry
import org.locationtech.jts.geom.MultiPolygon
import org.locationtech.jts.geom.Polygon
import org.locationtech.jts.io.ByteOrderValues
import org.locationtech.jts.io.WKBWriter
import qupath.lib.io.GsonTools

import static qupath.lib.scripting.QP.*

return { Map request, String slideId, String runId, String generationId,
        String requestedEntryId, def gson ->
    String sectionId = request.section_id as String
    String analysisDomainId = request.analysis_domain_id as String
    String analysisDomainRevisionSha256 =
        request.analysis_domain_revision_sha256 as String
    String analysisDomainPathObjectId =
        request.analysis_domain_path_object_id as String
    String domainSetSha256 = request.domain_set_sha256 as String
    Number requestedRevision = request.analysis_domain_revision as Number
    Map domainRevision = request.analysis_domain_revision_evidence as Map
    if (!sectionId || !(analysisDomainId ==~ /[0-9a-f]{64}/) ||
            !(analysisDomainRevisionSha256 ==~ /[0-9a-f]{64}/) ||
            !analysisDomainPathObjectId || !(domainSetSha256 ==~ /[0-9a-f]{64}/) ||
            requestedRevision == null || requestedRevision.intValue() < 1 ||
            requestedRevision.doubleValue() != requestedRevision.intValue() ||
            request.analysis_domain_selection_status != "accepted" ||
            !(request.analysis_domain_manifests instanceof List) ||
            !(request.accepted_tissue_section_authorities instanceof List))
        throw new IllegalStateException(
            "Complete accepted semantic live authority is required"
        )

    def canonicalGson = GsonTools.getInstance().newBuilder().serializeNulls().create()
    Closure canonicalValue
    canonicalValue = { Object value ->
        if (value instanceof Map) {
            Map sorted = new TreeMap<String, Object>()
            (value as Map).each { key, nested ->
                sorted.put(key as String, canonicalValue(nested))
            }
            return sorted
        }
        if (value instanceof List)
            return (value as List).collect { canonicalValue(it) }
        value
    }
    def canonicalSha256 = { Object value ->
        MessageDigest.getInstance("SHA-256").digest(
            canonicalGson.toJson(canonicalValue(value))
                .getBytes(StandardCharsets.UTF_8)
        ).encodeHex().toString()
    }
    def md = { object -> object.getMetadata() }
    def liveGeometrySha256 = { Geometry geometry ->
        if (geometry == null || geometry.isEmpty() || !geometry.isValid()) return null
        Geometry normalized = geometry.copy();
        normalized.normalize()
        MessageDigest.getInstance("SHA-256").digest(
            new WKBWriter(2, ByteOrderValues.LITTLE_ENDIAN, false).write(normalized)
        ).encodeHex().toString()
    }
    def ring = { sequence ->
        sequence.toCoordinateArray().collect { coordinate ->
            [coordinate.x, coordinate.y]
        }
    }
    def polygonCoordinates = { Polygon polygon ->
        List rings = [ring(polygon.exteriorRing.coordinateSequence)]
        (0..<polygon.numInteriorRing).each { index ->
            rings << ring(polygon.getInteriorRingN(index).coordinateSequence)
        }
        rings
    }
    def geometryJson = { Geometry geometry ->
        if (geometry instanceof Polygon)
            return [type: "Polygon", coordinates: polygonCoordinates(geometry)]
        if (geometry instanceof MultiPolygon)
            return [type: "MultiPolygon", coordinates:
                (0..<geometry.numGeometries).collect { index ->
                    polygonCoordinates(geometry.getGeometryN(index) as Polygon)
                }]
        throw new IllegalStateException(
            "Unsupported accepted TissueSection geometry ${geometry?.geometryType}"
        )
    }

    // One bounded hierarchy inventory is indexed by UUID before any semantic
    // metadata predicate is applied, so a tampered expected object cannot vanish.
    List allAnnotations = getAnnotationObjects()
    Map<String, Object> annotationByPathObjectId = [:]
    allAnnotations.each { object ->
        String identity = object.getID().toString()
        if (annotationByPathObjectId.put(identity, object) != null)
            throw new IllegalStateException("Annotation PathObject identity is duplicated")
    }
    List acceptedSections = allAnnotations.findAll { object ->
        Map metadata = md(object)
        metadata.get("pipeline_stage") == "tissue_detection" &&
            metadata.get("generation_status") == "accepted" &&
            metadata.get("review_status") == "accepted" &&
            metadata.get("slide_id") == slideId &&
            metadata.get("generation_id") == generationId
    }
    Map<String, Object> acceptedSectionsById = [:]
    acceptedSections.each { object ->
        String acceptedSectionId = md(object).get("section_id") as String
        if (!acceptedSectionId ||
                acceptedSectionsById.put(acceptedSectionId, object) != null)
            throw new IllegalStateException(
                "Accepted TissueSection inventory is ambiguous"
            )
    }
    if (acceptedSectionsById.isEmpty())
        throw new IllegalStateException("Accepted TissueSection inventory is empty")

    Set tissueAuthorityFields = [
        "schema_version", "slide_id", "section_id", "tissue_roi_id",
        "tissue_path_object_id", "tissue_generation_id", "revision",
        "geometry_digest_policy", "geometry_sha256", "geometry",
    ] as Set
    List<Map> tissueAuthorities =
        request.accepted_tissue_section_authorities as List
    if (tissueAuthorities.any { !(it instanceof Map) } ||
            tissueAuthorities.collect { it.section_id as String } !=
                tissueAuthorities.collect { it.section_id as String }.sort())
        throw new IllegalStateException(
            "Accepted TissueSection live authorities must be sorted mappings"
        )
    Map<String, Map> tissueAuthorityById = [:]
    tissueAuthorities.each { Map authority ->
        String authoritySectionId = authority.section_id as String
        def liveSection = acceptedSectionsById[authoritySectionId]
        Map metadata = liveSection == null ? [:] : md(liveSection)
        Number authorityRevision = authority.revision as Number
        Geometry liveGeometry = liveSection?.getROI()?.getGeometry()
        Map serializedLiveGeometry = liveGeometry == null ? null :
            geometryJson(liveGeometry) as Map
        if (authority.keySet() != tissueAuthorityFields ||
                authority.schema_version !=
                    "accepted-tissue-section-live-authority/v1" ||
                liveSection == null || liveSection.getParent() !=
                    getCurrentImageData().getHierarchy().getRootObject() ||
                authority.slide_id != slideId ||
                authority.tissue_generation_id != generationId ||
                authority.tissue_roi_id != metadata.get("roi_id") ||
                authority.tissue_path_object_id != liveSection.getID().toString() ||
                authorityRevision == null || authorityRevision.intValue() < 0 ||
                authorityRevision.doubleValue() != authorityRevision.intValue() ||
                metadata.get("revision") !=
                    Integer.toString(authorityRevision.intValue()) ||
                authority.geometry_digest_policy !=
                    "canonical-geojson-sha256/v1" ||
                !(authority.geometry_sha256 as String ==~ /[0-9a-f]{64}/) ||
                !(authority.geometry instanceof Map) ||
                canonicalSha256(authority.geometry) != authority.geometry_sha256 ||
                canonicalValue(authority.geometry) !=
                    canonicalValue(serializedLiveGeometry) ||
                canonicalSha256(serializedLiveGeometry) != authority.geometry_sha256 ||
                tissueAuthorityById.put(authoritySectionId, authority) != null)
            throw new IllegalStateException(
                "Accepted TissueSection PathObject, revision, or live geometry is stale"
            )
    }
    if (tissueAuthorityById.keySet() != acceptedSectionsById.keySet())
        throw new IllegalStateException(
            "Accepted TissueSection live authority does not cover the global set"
        )
    def section = acceptedSectionsById[sectionId]
    if (section == null)
        throw new IllegalStateException(
            "Exact requested accepted TissueSection is absent"
        )

    Set task6RevisionFields = [
        "analysis_domain_id", "kind", "slide_id", "section_id",
        "tissue_generation_id", "canonical_image_id", "canonical_image_sha256",
        "project_entry_id", "tissue_roi_id", "tissue_path_object_id",
        "source_path_object_id", "revision", "predecessor_sha256",
        "geometry_digest_policy", "geometry_sha256", "normalized_wkb_hex",
        "geometry_reference_path_object_id", "content_sha256",
    ] as Set
    Set task6ManifestFields = [
        "schema_version", "canonical_image_id", "canonical_image_sha256",
        "project_entry_id", "slide_id", "section_id", "tissue_generation_id",
        "tissue_roi_id", "tissue_path_object_id", "revisions", "content_sha256",
    ] as Set
    List<Map> currentRevisions = []
    Set<String> manifestContentSha256s = [] as Set<String>
    Set<String> manifestSourceKeys = [] as Set<String>
    (request.analysis_domain_manifests as List).each { rawManifest ->
        if (!(rawManifest instanceof Map))
            throw new IllegalStateException("Task 6 manifest must be a mapping")
        Map manifest = rawManifest as Map
        String manifestSectionId = manifest.section_id as String
        def manifestSection = acceptedSectionsById[manifestSectionId]
        Map tissueAuthority = tissueAuthorityById[manifestSectionId]
        if (manifest.keySet() != task6ManifestFields ||
                manifest.schema_version != "analysis-domain/v1" ||
                !(manifest.content_sha256 as String ==~ /[0-9a-f]{64}/) ||
                !(manifest.revisions instanceof List) ||
                (manifest.revisions as List).isEmpty() ||
                manifestSection == null || tissueAuthority == null ||
                manifest.slide_id != slideId ||
                manifest.canonical_image_id != request.canonical_image_id ||
                manifest.canonical_image_sha256 != request.canonical_image_sha256 ||
                manifest.project_entry_id != requestedEntryId ||
                manifest.tissue_generation_id != generationId ||
                manifest.tissue_roi_id != tissueAuthority.tissue_roi_id ||
                manifest.tissue_path_object_id !=
                    tissueAuthority.tissue_path_object_id)
            throw new IllegalStateException(
                "Task 6 manifest is outside accepted TissueSection authority"
            )
        List<Map> history = (manifest.revisions as List).collect { rawRevision ->
            if (!(rawRevision instanceof Map))
                throw new IllegalStateException("Task 6 revision must be a mapping")
            rawRevision as Map
        }
        String firstDomainId = null
        String firstSourcePathObjectId = null
        String firstKind = null
        String predecessorSha256 = null
        history.eachWithIndex { revision, index ->
            Number revisionNumber = revision.revision as Number
            String domainId = revision.analysis_domain_id as String
            String kind = revision.kind as String
            String sourcePathObjectId = revision.source_path_object_id as String
            String revisionSha256 = revision.content_sha256 as String
            if (revision.keySet() != task6RevisionFields ||
                    !(domainId ==~ /[0-9a-f]{64}/) ||
                    !(revisionSha256 ==~ /[0-9a-f]{64}/) ||
                    !sourcePathObjectId ||
                    !(kind in ["selected_roi", "whole_tissue_section"]) ||
                    revisionNumber == null ||
                    revisionNumber.intValue() != index + 1 ||
                    revisionNumber.doubleValue() != revisionNumber.intValue() ||
                    revision.predecessor_sha256 != predecessorSha256 ||
                    revision.section_id != manifestSectionId ||
                    revision.slide_id != slideId ||
                    revision.tissue_generation_id != generationId ||
                    revision.canonical_image_id != request.canonical_image_id ||
                    revision.canonical_image_sha256 !=
                        request.canonical_image_sha256 ||
                    revision.project_entry_id != requestedEntryId ||
                    revision.tissue_roi_id != manifest.tissue_roi_id ||
                    revision.tissue_path_object_id !=
                        manifest.tissue_path_object_id ||
                    (firstDomainId != null && (
                        domainId != firstDomainId ||
                        sourcePathObjectId != firstSourcePathObjectId ||
                        kind != firstKind)))
                throw new IllegalStateException(
                    "Task 6 revision history is stale or unordered"
                )
            String expectedDomainId = canonicalSha256([
                canonical_image_sha256: request.canonical_image_sha256,
                kind: kind, schema_version: "analysis-domain/v1",
                section_id: manifestSectionId,
                source_path_object_id: sourcePathObjectId,
                tissue_generation_id: generationId,
            ])
            String geometrySha256 = revision.geometry_sha256 as String
            String normalizedWkbHex = revision.normalized_wkb_hex as String
            String geometryReferencePathObjectId =
                revision.geometry_reference_path_object_id as String
            if (domainId != expectedDomainId ||
                    revision.geometry_digest_policy !=
                        "jts-normalized-wkb-sha256/v1" ||
                    (kind == "selected_roi" && (
                        !(geometrySha256 ==~ /[0-9a-f]{64}/) ||
                        !(normalizedWkbHex ==~ /(?:[0-9a-f]{2})+/) ||
                        geometryReferencePathObjectId != null ||
                        MessageDigest.getInstance("SHA-256")
                            .digest(normalizedWkbHex.decodeHex())
                            .encodeHex().toString() != geometrySha256
                    )) ||
                    (kind == "whole_tissue_section" && (
                        geometrySha256 != null || normalizedWkbHex != null ||
                        geometryReferencePathObjectId != sourcePathObjectId ||
                        sourcePathObjectId != manifest.tissue_path_object_id ||
                        revisionNumber.intValue() != 1 ||
                        predecessorSha256 != null
                    )))
                throw new IllegalStateException(
                    "Task 6 revision geometry or whole-tissue reference is invalid"
                )
            Map revisionPayload = [
                analysis_domain_id: domainId,
                canonical_image_id: request.canonical_image_id,
                canonical_image_sha256: request.canonical_image_sha256,
                geometry_digest_policy: revision.geometry_digest_policy,
                geometry_reference_path_object_id: geometryReferencePathObjectId,
                geometry_sha256: geometrySha256, kind: kind,
                predecessor_sha256: predecessorSha256,
                project_entry_id: requestedEntryId,
                revision: revisionNumber.intValue(),
                section_id: manifestSectionId, slide_id: slideId,
                source_path_object_id: sourcePathObjectId,
                tissue_generation_id: generationId,
                tissue_path_object_id: manifest.tissue_path_object_id,
                tissue_roi_id: manifest.tissue_roi_id,
            ]
            if (canonicalSha256(revisionPayload) != revisionSha256)
                throw new IllegalStateException("Task 6 revision digest is stale")
            firstDomainId = firstDomainId ?: domainId
            firstSourcePathObjectId =
                firstSourcePathObjectId ?: sourcePathObjectId
            firstKind = firstKind ?: kind
            predecessorSha256 = revisionSha256
        }
        Map manifestPayload = [
            canonical_image_id: manifest.canonical_image_id,
            canonical_image_sha256: manifest.canonical_image_sha256,
            project_entry_id: manifest.project_entry_id,
            revision_sha256s: history.collect { it.content_sha256 as String },
            schema_version: manifest.schema_version,
            section_id: manifest.section_id, slide_id: manifest.slide_id,
            tissue_generation_id: manifest.tissue_generation_id,
            tissue_path_object_id: manifest.tissue_path_object_id,
            tissue_roi_id: manifest.tissue_roi_id,
        ]
        String sourceKey = "${manifestSectionId}|${firstSourcePathObjectId}"
        if (canonicalSha256(manifestPayload) != manifest.content_sha256 ||
                !manifestContentSha256s.add(manifest.content_sha256 as String) ||
                !manifestSourceKeys.add(sourceKey))
            throw new IllegalStateException(
                "Task 6 manifest content or inventory is stale"
            )
        currentRevisions << history.last()
    }
    if (currentRevisions.isEmpty() ||
            canonicalSha256([
                schema_version: "analysis-domain-set/v1",
                analysis_domain_manifest_sha256s:
                    manifestContentSha256s.toList().sort(),
            ]) != domainSetSha256)
        throw new IllegalStateException(
            "Task 6 global manifest set is incomplete or stale"
        )

    // Selected-domain roots are the live current-selection index. Expected
    // roots are resolved by UUID first; tagged roots outside the request are
    // permitted only when Task 6 has explicitly retired them. This makes an
    // old whole-tissue request fail after a whole-to-selected transition.
    Map<String, Map> expectedSelectedByPathObjectId = [:]
    currentRevisions.findAll { it.kind == "selected_roi" }.each { revision ->
        String sourcePathObjectId = revision.source_path_object_id as String
        if (expectedSelectedByPathObjectId.put(
                sourcePathObjectId, revision as Map
            ) != null)
            throw new IllegalStateException(
                "Task 6 current selected-domain PathObject inventory is duplicated"
            )
    }
    expectedSelectedByPathObjectId.each { sourcePathObjectId, revision ->
        def liveRoot = annotationByPathObjectId[sourcePathObjectId]
        def expectedParent = acceptedSectionsById[revision.section_id as String]
        Map metadata = liveRoot == null ? [:] : md(liveRoot)
        Geometry liveRootGeometry = liveRoot?.getROI()?.getGeometry()
        if (liveRoot == null || expectedParent == null ||
                liveRoot.getParent() != expectedParent || !liveRoot.isLocked() ||
                metadata.get("pipeline_stage") != "analysis_domain_selection" ||
                metadata.get("analysis_domain_status") != "accepted" ||
                metadata.get("analysis_domain_active") != "true" ||
                metadata.get("analysis_domain_id") != revision.analysis_domain_id ||
                metadata.get("analysis_domain_revision") !=
                    Integer.toString((revision.revision as Number).intValue()) ||
                metadata.get("analysis_domain_revision_sha256") !=
                    revision.content_sha256 ||
                metadata.get("analysis_domain_set_sha256") != domainSetSha256 ||
                metadata.get("analysis_domain_project_entry_id") !=
                    requestedEntryId ||
                metadata.get("analysis_domain_geometry_sha256") !=
                    revision.geometry_sha256 ||
                liveGeometrySha256(liveRootGeometry) != revision.geometry_sha256)
            throw new IllegalStateException(
                "Current selected-domain root is stale, replaced, or outside the global set"
            )
    }
    Set acceptedSectionObjects = acceptedSectionsById.values() as Set
    List taggedDomainRoots = allAnnotations.findAll { object ->
        Map metadata = md(object)
        acceptedSectionObjects.contains(object.getParent()) && (
            metadata.get("pipeline_stage") == "analysis_domain_selection" ||
            metadata.get("analysis_domain_status") != null ||
            metadata.get("analysis_domain_active") != null
        )
    }
    taggedDomainRoots.each { object ->
        String pathObjectId = object.getID().toString()
        if (expectedSelectedByPathObjectId.containsKey(pathObjectId)) return
        Map metadata = md(object)
        if (metadata.get("pipeline_stage") != "analysis_domain_selection" ||
                metadata.get("analysis_domain_status") != "retired" ||
                metadata.get("analysis_domain_active") != "false" ||
                !object.isLocked())
            throw new IllegalStateException(
                "Live current analysis-domain selection differs from Task 6 authority"
            )
    }
    List<Map> currentTargetRevisions = currentRevisions.findAll { revision ->
        revision.analysis_domain_id == analysisDomainId
    }
    if (currentTargetRevisions.size() != 1 || domainRevision == null ||
            domainRevision.keySet() != task6RevisionFields ||
            canonicalValue(domainRevision) !=
                canonicalValue(currentTargetRevisions.first()) ||
            domainRevision.content_sha256 != analysisDomainRevisionSha256 ||
            domainRevision.source_path_object_id != analysisDomainPathObjectId ||
            domainRevision.revision != request.analysis_domain_revision ||
            domainRevision.section_id != sectionId)
        throw new IllegalStateException(
            "Requested analysis-domain revision is not the exact current Task 6 revision"
        )

    def domain = annotationByPathObjectId[analysisDomainPathObjectId]
    Map domainMetadata = domain == null ? [:] : md(domain)
    boolean selectedDomain = domainRevision.kind == "selected_roi" &&
        domain != section && domain?.getParent() == section && domain?.isLocked() &&
        domainMetadata.get("pipeline_stage") == "analysis_domain_selection" &&
        domainMetadata.get("analysis_domain_status") == "accepted" &&
        domainMetadata.get("analysis_domain_active") == "true" &&
        domainMetadata.get("analysis_domain_id") == analysisDomainId &&
        domainMetadata.get("analysis_domain_revision") ==
            Integer.toString(requestedRevision.intValue()) &&
        domainMetadata.get("analysis_domain_revision_sha256") ==
            analysisDomainRevisionSha256 &&
        domainMetadata.get("analysis_domain_set_sha256") == domainSetSha256 &&
        domainMetadata.get("analysis_domain_project_entry_id") == requestedEntryId &&
        domainMetadata.get("analysis_domain_geometry_sha256") ==
            domainRevision.geometry_sha256
    boolean wholeTissueDomain = domainRevision.kind == "whole_tissue_section" &&
        domain == section && domainRevision.geometry_sha256 == null &&
        domainRevision.normalized_wkb_hex == null &&
        domainRevision.geometry_reference_path_object_id ==
            section.getID().toString()
    Geometry domainGeometry = domain?.getROI()?.getGeometry()
    if (!(selectedDomain || wholeTissueDomain) || domainGeometry == null ||
            domainGeometry.isEmpty() || !domainGeometry.isValid() ||
            !domainGeometry.coveredBy(section.getROI().getGeometry()) ||
            (domainRevision.geometry_sha256 != null &&
                liveGeometrySha256(domainGeometry) != domainRevision.geometry_sha256))
        throw new IllegalStateException(
            "Analysis-domain root is stale, inactive, unlocked, or outside exact tissue"
        )

    // i) Open each accepted source table once. One sorted ROI-to-object index
    // proves cross-table uniqueness and supplies live parents and final order;
    // table rows are released immediately after matching their IDs and revision.
    if ((request.containsKey("primary_classification_skipped") && request.primary_classification_skipped != true) ||
            !(request.semantic_roi_sources instanceof List) ||
            !(request.resolved_config instanceof Map) || !(request.resolved_config.output_dir instanceof String))
        throw new IllegalStateException("Accepted semantic ROI tables and output location are required")
    Set sourceFields = ["source_kind", "source_result_ref_sha256", "producer_attempt_id", "roi_inventory", "structure_id"] as Set
    Set sourceResults = new HashSet()
    Map<String, Object> roiById = new TreeMap<>()
    int primarySources = 0
    request.semantic_roi_sources.each { rawSource ->
        if (!(rawSource instanceof Map) || rawSource.keySet() != sourceFields ||
                !(rawSource.source_kind in ["primary", "secondary"]) ||
                !(rawSource.source_result_ref_sha256 instanceof String) ||
                !(rawSource.source_result_ref_sha256 ==~ /[0-9a-f]{64}/) ||
                !sourceResults.add(rawSource.source_result_ref_sha256) ||
                !(rawSource.producer_attempt_id instanceof String) || !(rawSource.roi_inventory instanceof Map))
            throw new IllegalStateException("Semantic ROI source selection is invalid")
        boolean primarySource = rawSource.source_kind == "primary"
        if (primarySource) {
            primarySources++
            if (rawSource.structure_id != null) throw new IllegalStateException("Primary ROI source has a secondary structure")
        } else if (!(rawSource.structure_id instanceof String) || !rawSource.structure_id) {
            throw new IllegalStateException("Secondary ROI source lacks its structure")
        }
        RoiInventory.read(MifProjectStageBootstrapV1.producerOutputRoot(request, request.resolved_config as Map, rawSource.producer_attempt_id as String), rawSource.roi_inventory as Map,
                RoiInventory.member(rawSource.producer_attempt_id as String), { RoiInventory.Row row ->
            def object = annotationByPathObjectId[row.pathObjectId()]
            Map metadata = object == null ? [:] : md(object)
            if (metadata.get("pipeline_stage") != (primarySource ? "primary_classifier" : "secondary_classifier") ||
                    metadata.get(primarySource ? "primary_class_id" : "structure_id") != row.classId() ||
                    (!primarySource && row.classId() != rawSource.structure_id) ||
                    metadata.get("roi_id") != row.roiId() ||
                    metadata.get("object_revision_sha256") != row.objectRevisionSha256())
                throw new IllegalStateException("Semantic ROI table differs from its live classifier result")
            // A live object has exactly one stable ROI ID. Matching it here
            // makes a repeated object under another table ID invalid as well.
            if (roiById.putIfAbsent(row.roiId(), object) != null)
                throw new IllegalStateException("Semantic ROI is repeated across accepted classifier tables")
        } as Consumer<RoiInventory.Row>)
    }
    int expectedPrimarySources = request.primary_classification_skipped ? 0 : 1
    if (primarySources != expectedPrimarySources)
        throw new IllegalStateException("Semantic ROI selection differs from the accepted primary classification choice")
    roiById.each { roiId, object ->
        Map metadata = md(object)
        String stage = metadata.get("pipeline_stage") as String
        String type = stage == "primary_classifier" ? "primary" :
            (stage == "secondary_classifier" ? "secondary" : null)
        String typeId = type == "primary" ?
            metadata.get("primary_class_id") as String :
            metadata.get("structure_id") as String
        Geometry geometry = object.getROI()?.getGeometry()
        String geometrySha256 = liveGeometrySha256(geometry) as String
        def parent = object.getParent()
        String parentPathObjectId = parent?.getID()?.toString()
        String parentRoiId = metadata.get("parent_roi_id") as String
        def stableParent = parentRoiId == analysisDomainId ?
            domain : roiById[parentRoiId]
        if (type == null || !typeId || geometrySha256 == null ||
                metadata.get("slide_id") != slideId ||
                metadata.get("section_id") != sectionId ||
                metadata.get("generation_id") != generationId ||
                metadata.get("pipeline_run_id") != runId ||
                metadata.get("analysis_domain_id") != analysisDomainId ||
                metadata.get("analysis_domain_kind") != domainRevision.kind ||
                metadata.get("analysis_domain_revision") !=
                    Integer.toString(requestedRevision.intValue()) ||
                metadata.get("analysis_domain_revision_sha256") !=
                    analysisDomainRevisionSha256 ||
                metadata.get("analysis_domain_path_object_id") !=
                    analysisDomainPathObjectId ||
                metadata.get("domain_set_sha256") != domainSetSha256 ||
                metadata.get("pipeline_active") != "true" ||
                metadata.get("geometry_sha256") != geometrySha256 ||
                stableParent == null || parent != stableParent ||
                metadata.get("parent_path_object_id") != parentPathObjectId ||
                (type == "primary" && parent != domain) ||
                !geometry.coveredBy(parent.getROI().getGeometry()))
            throw new IllegalStateException(
                "Semantic ROI metadata, seal, parent, or live geometry is stale"
            )
        String expectedObjectRevisionSha256 = canonicalSha256([
            analysis_domain_id: analysisDomainId,
            analysis_domain_revision_sha256: analysisDomainRevisionSha256,
            analysis_domain_set_sha256: domainSetSha256,
            classification: object.getClassification(),
            geometry_sha256: geometrySha256,
            parent_path_object_id: parentPathObjectId,
            path_object_id: object.getID().toString(),
            pipeline_active: metadata.get("pipeline_active"),
            type: type, type_id: typeId,
        ])
        if (metadata.get("object_revision_sha256") !=
                expectedObjectRevisionSha256)
            throw new IllegalStateException(
                "Semantic ROI object_revision_sha256 is stale or replaced"
            )
    }
    roiById.values().each { object ->
        Set seen = Collections.newSetFromMap(new IdentityHashMap())
        def cursor = object
        while (cursor != domain) {
            if (cursor == null || !seen.add(cursor))
                throw new IllegalStateException(
                    "Semantic ROI hierarchy does not terminate at the exact domain root"
                )
            cursor = cursor.getParent()
        }
    }

    Map reference = request.semantic_cell_dataset as Map
    Set referenceFields = ["path", "sha256", "schema_version", "row_count", "source_sha256"] as Set
    if (reference == null || reference.keySet() != referenceFields ||
            reference.schema_version != "mif-cell-dataset/v1" ||
            !(reference.sha256 as String ==~ /[0-9a-f]{64}/) ||
            !(reference.source_sha256 as String ==~ /[0-9a-f]{64}/) ||
            !(request.semantic_cell_count instanceof Number) ||
            (request.semantic_cell_count as Number).longValue() < 0 ||
            !(request.semantic_cell_ids_sha256 as String ==~ /[0-9a-f]{64}/))
        throw new IllegalStateException("Semantic cell dataset selection is invalid")
    Path datasetPath = Path.of(reference.path as String).toAbsolutePath().normalize()
    Map bindings = gson.fromJson(getCurrentImageData().getProperty(
        CellDatasetProvider.DATASET_PROPERTY) as String, Map) as Map
    if (datasetPath.fileName.toString() != "dataset.json" ||
            bindings == null || bindings[sectionId] == null ||
            Path.of(bindings[sectionId] as String).toAbsolutePath().normalize() != datasetPath ||
            Files.size(datasetPath) > 64L * 1024 * 1024)
        throw new IllegalStateException("Semantic cells are not the selected native section dataset")
    byte[] descriptionBytes = Files.readAllBytes(datasetPath)
    if (MessageDigest.getInstance("SHA-256").digest(descriptionBytes).encodeHex().toString() != reference.sha256)
        throw new IllegalStateException("Semantic cell dataset identity changed")
    Map description = gson.fromJson(new String(descriptionBytes, StandardCharsets.UTF_8), Map) as Map
    Map domainInventory = (description.domain_inventories as Map)[analysisDomainId] as Map
    Map selectedCellDomain = (description.scope.domains as Map)[analysisDomainId] as Map
    if (description.schema_version != reference.schema_version ||
            description.row_count != reference.row_count ||
            description.source_sha256 != reference.source_sha256 ||
            description.scope.population_id != request.segmentation_population_id ||
            selectedCellDomain == null ||
            selectedCellDomain.analysis_domain_revision_sha256 != analysisDomainRevisionSha256 ||
            selectedCellDomain.domain_set_sha256 != domainSetSha256 ||
            domainInventory == null || domainInventory.cell_count != request.semantic_cell_count ||
            domainInventory.cell_ids_sha256 != request.semantic_cell_ids_sha256)
        throw new IllegalStateException("Semantic cell population, domain revision or ID inventory changed")
    def provider = NativeCellScope.open(getCurrentImageData(), sectionId).join()
    Closure forEachCell = { Closure consume ->
        long count = 0
        provider.forEach([
            "mif.measurements_stale", "selected_phenotype_view", "phenotyping_generation_id",
        ] as Set<String>, { cell ->
            Map metadata = md(cell)
            if (metadata.get("analysis_domain_id") != analysisDomainId) return
            if (metadata.get("segmentation_population_id") != request.segmentation_population_id ||
                    metadata.get("cell_id") != metadata.get("mif.cell_id") ||
                    metadata.get("pipeline_run_id") != runId ||
                    metadata.get("section_id") != sectionId ||
                    metadata.get("slide_id") != slideId ||
                    metadata.get("analysis_domain_revision_sha256") != analysisDomainRevisionSha256 ||
                    metadata.get("domain_set_sha256") != domainSetSha256 ||
                    metadata.get("scientific_measurements_stale") == "true")
                throw new IllegalStateException("Semantic cell scope or selected measurements are stale")
            count++
            consume(cell)
        } as Consumer)
        if (count != (request.semantic_cell_count as Number).longValue())
            throw new IllegalStateException("Semantic cell edits changed the selected ID population")
    }
    MessageDigest selectedIds = MessageDigest.getInstance("SHA-256")
    selectedIds.update("[".getBytes(StandardCharsets.UTF_8))
    boolean firstId = true
    try (SortedCellIds ids = new SortedCellIds(Path.of(System.getProperty("java.io.tmpdir")))) {
        forEachCell { cell -> ids.add(md(cell).get("cell_id") as String, false) }
        ids.forEach({ String cellId, Boolean ignored ->
            if (!firstId) selectedIds.update(",".getBytes(StandardCharsets.UTF_8))
            firstId = false
            selectedIds.update(SortedCellIds.canonicalId(cellId).getBytes(StandardCharsets.UTF_8))
        } as BiConsumer)
    }
    selectedIds.update("]".getBytes(StandardCharsets.UTF_8))
    if (selectedIds.digest().encodeHex().toString() != request.semantic_cell_ids_sha256)
        throw new IllegalStateException("Semantic cell edits changed the selected stable ID inventory")
    [section: section, domain: domain, roi_by_id: Collections.unmodifiableMap(roiById),
     cell_provider: provider, for_each_cell: forEachCell,
     cell_count: (request.semantic_cell_count as Number).longValue(),
     cell_ids_sha256: request.semantic_cell_ids_sha256,
     canonical_sha256: canonicalSha256,
     live_geometry_sha256: liveGeometrySha256]
}
