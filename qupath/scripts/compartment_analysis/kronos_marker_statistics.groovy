import java.security.MessageDigest
import org.locationtech.jts.io.WKBWriter
import org.locationtech.jts.io.ByteOrderValues
import org.locationtech.jts.operation.union.UnaryUnionOp
import org.mif.inference.KronosMarkerStatistics

/** Fit-only adapter over accepted material/domain tissue and the native moment reducer.
 * Its cache lives only for this authenticated operation; entries are keyed by the
 * exact image, union geometry/plane, channel mapping, divisor and method.
 */
final class MifKronosMarkerStatistics {
    static final Set BIOLOGY=["marker_name","marker_full_name","compartment","family","family_desc"].toSet()
    final Map<String,List> partials=[:]
    final Map<String,Map> receipts=[:]
    static void require(boolean condition,String message) {
        if(!condition)throw new IllegalArgumentException(message)
    }
    static String sha(byte[] value) {MessageDigest.getInstance("SHA-256").digest(value).encodeHex().toString()}
    /** Preserve exact doubles across Gson and Python canonical JSON number formatting. */
    static String float64Bits(Number value) {
        require(Double.isFinite(value.doubleValue()),"Statistics receipt contains a nonfinite value")
        Long.toHexString(Double.doubleToRawLongBits(value.doubleValue())).padLeft(16,"0")
    }
    static Object canonical(Object value) {
        if(value instanceof Map)return new TreeMap(value.collectEntries {key,item->[(key):canonical(item)]})
        if(value instanceof List)return value.collect {canonical(it)}
        value
    }
    static String jsonSha(Object value) {
        sha(new com.google.gson.GsonBuilder().disableHtmlEscaping().serializeNulls().create()
            .toJson(canonical(value)).getBytes(java.nio.charset.StandardCharsets.UTF_8))
    }
    static String geometrySha(def geometry) {
        require(geometry!=null && !geometry.empty && geometry.valid && geometry.dimension==2,
            "Automatic marker statistics require a valid accepted tissue area")
        def normalized=geometry.copy();normalized.normalize()
        sha(new WKBWriter(2,ByteOrderValues.LITTLE_ENDIAN,false).write(normalized))
    }
    static Map encoder(Map configuration) {
        configuration.feature_source!=null ? configuration.feature_source.encoder : configuration.encoder
    }
    static List pending(Map configuration) {
        (encoder(configuration)?.additional_markers ?: []).findAll {it instanceof Map && it.keySet()==BIOLOGY}
    }
    static Map binding(Map request,Map owner,String domainId) {
        List found=request.training_analysis_domain_bindings.findAll {
            it.project_entry_id==owner.project_entry_id && it.analysis_domain_id==domainId
        }
        require(found.size()==1,"Automatic marker statistics lack one authenticated analysis domain: "+domainId)
        found[0]
    }
    static void acceptedSection(def section,Map owner) {
        Map m=section?.metadata
        require(section!=null && section.ROI?.isArea() && m.pipeline_stage=="tissue_detection" &&
            m.generation_status=="accepted" && m.review_status=="accepted" && m.slide_id==owner.slide_id &&
            section.parent?.isRootObject(),"Automatic marker statistics require the current accepted TissueSection for "+owner.project_entry_id)
        geometrySha(section.ROI.geometry)
    }
    static void acceptedDomain(def domain,def section,Map own,Map selected) {
        Map m=domain.metadata
        require(domain.ROI?.isArea() && domain.parent==section && domain.locked &&
            m.pipeline_stage=="analysis_domain_selection" && m.analysis_domain_status=="accepted" &&
            m.analysis_domain_active=="true" && m.analysis_domain_id==selected.analysis_domain_id &&
            m.analysis_domain_revision==Integer.toString((selected.analysis_domain_revision as Number).intValue()) &&
            m.analysis_domain_revision_sha256==selected.analysis_domain_revision_sha256 &&
            m.analysis_domain_set_sha256==selected.domain_set_sha256 &&
            m.analysis_domain_project_entry_id==own.project_entry_id,
            "Automatic marker statistics analysis domain is absent, stale or reparented: "+selected.analysis_domain_id)
        geometrySha(domain.ROI.geometry)
    }
    static Map region(def image,Map owner,def section,def domain,def material,Map selected) {
        acceptedSection(section,owner)
        if(domain!=section)acceptedDomain(domain,section,owner,selected)
        else require(selected.analysis_domain_id==jsonSha([canonical_image_sha256:owner.image_identity_sha256,
            kind:"whole_tissue_section",schema_version:"analysis-domain/v1",section_id:section.metadata.section_id,
            source_path_object_id:section.ID.toString(),tissue_generation_id:section.metadata.generation_id]),
            "Automatic marker statistics whole-tissue domain identity differs")
        require(material.ROI?.isArea() && material.ROI.imagePlane==domain.ROI.imagePlane &&
            domain.ROI.imagePlane==section.ROI.imagePlane,"Automatic marker statistics region planes disagree")
        def geometry=material.ROI.geometry.intersection(domain.ROI.geometry).intersection(section.ROI.geometry)
        geometrySha(geometry)
        [image:image,owner:owner,plane:material.ROI.imagePlane,geometry:geometry,
         region:[analysis_domain_id:selected.analysis_domain_id,
            analysis_domain_revision_sha256:selected.analysis_domain_revision_sha256,
            domain_set_sha256:selected.domain_set_sha256,tissue_object_id:section.ID.toString(),
            material_object_id:material.ID.toString(),geometry_sha256:geometrySha(geometry)]]
    }
    static List pixelRegions(Map request,List images) {
        List result=[]
        request.training_entries.eachWithIndex {entry,int index ->
            Map owner=entry.entry_state
            def image=images[index]
            Map objects=image.hierarchy.annotationObjects.collectEntries {[(it.ID.toString()):it]}
            entry.evidence_rows.each {row ->
                def material=objects[row.material_parent_id]
                require(material!=null,"Automatic marker statistics accepted material parent is absent")
                Map selected=binding(request,owner,row.analysis_domain_id as String)
                require(selected.analysis_domain_revision_sha256==row.analysis_domain_revision_sha256 &&
                    selected.domain_set_sha256==row.domain_set_sha256,"Automatic marker statistics material binding changed")
                List sections=[],domains=[]
                Set seen=Collections.newSetFromMap(new IdentityHashMap())
                def cursor=material
                while(cursor!=null && !cursor.isRootObject()) {
                    require(seen.add(cursor),"Automatic marker statistics material ancestry contains a cycle")
                    if(cursor.metadata.pipeline_stage=="tissue_detection")sections.add(cursor)
                    if(cursor.metadata.pipeline_stage=="analysis_domain_selection")domains.add(cursor)
                    cursor=cursor.parent
                }
                require(sections.size()==1 && domains.size()<=1,"Automatic marker statistics cannot resolve accepted material ancestry")
                def section=sections[0],domain=domains.isEmpty()?section:domains[0]
                result.add(region(image,owner,section,domain,material,selected))
            }
        }
        result
    }
    static List cellRegions(Map request,List images) {
        List result=[]
        request.training_entries.eachWithIndex {entry,int index ->
            Map owner=entry.snapshot
            def image=images[index]
            List annotations=image.hierarchy.annotationObjects.toList()
            List sections=annotations.findAll {it.metadata.pipeline_stage=="tissue_detection" &&
                it.metadata.section_id==owner.section_id && it.metadata.slide_id==owner.slide_id &&
                it.metadata.generation_id==owner.segmentation_generation_id}
            require(sections.size()==1,"Automatic marker statistics require one accepted training TissueSection: "+owner.section_id)
            def section=sections[0]
            acceptedSection(section,owner)
            List selected=request.training_analysis_domain_bindings.findAll {
                it.project_entry_id==owner.project_entry_id && jsonSha(it) in owner.analysis_domain_binding_sha256s
            }
            require(!selected.isEmpty(),"Automatic marker statistics training section lacks authenticated analysis domains")
            for(Map choice:selected) {
                List matches=annotations.findAll {it.metadata.pipeline_stage=="analysis_domain_selection" &&
                    it.metadata.analysis_domain_id==choice.analysis_domain_id}
                def domain
                if(matches.isEmpty()) {
                    String wholeId=jsonSha([canonical_image_sha256:owner.image_identity_sha256,kind:"whole_tissue_section",
                        schema_version:"analysis-domain/v1",section_id:owner.section_id,
                        source_path_object_id:section.ID.toString(),tissue_generation_id:section.metadata.generation_id])
                    require(choice.analysis_domain_id==wholeId,
                        "Automatic marker statistics analysis domain cannot be resolved: "+choice.analysis_domain_id)
                    domain=section
                } else {
                    require(matches.size()==1,"Automatic marker statistics analysis domain is duplicated")
                    domain=matches[0]
                }
                result.add(region(image,owner,section,domain,domain,choice))
            }
        }
        result
    }
    Map resolve(Map configuration,Map request,List images) {
        List definitions=pending(configuration)
        if(definitions.isEmpty())return [configuration:configuration,receipt:null]
        List entries=request.training_entries
        boolean cells=configuration.feature_source!=null
        require(entries instanceof List && !entries.isEmpty() && entries.size()==images.size() &&
            entries.every {(cells?it.snapshot:it.entry_state).split=="training"},
            "Automatic marker statistics require exactly the current callback's training entries")
        Map enc=encoder(configuration)
        List names=enc.marker_names.findAll {name->definitions.any {it.marker_name==name}}
        require(names.size()==definitions.size(),"Automatic markers differ from the recorded marker order")
        List positions=names.collect {enc.marker_names.indexOf(it)}
        List regions=cells?cellRegions(request,images):pixelRegions(request,images)
        require(!regions.isEmpty(),"Automatic marker statistics training material is empty")
        Map grouped=regions.groupBy {[it.owner.project_entry_id,it.owner.image_identity_sha256,it.plane.z,it.plane.t]}
        List sources=[]
        List totals=names.collect {new KronosMarkerStatistics.Moments(0L,0d,0d)}
        grouped.keySet().toList().sort {a,b->
            (a[0]<=>b[0]) ?: (a[1]<=>b[1]) ?: (a[2]<=>b[2]) ?: (a[3]<=>b[3])
        }.each {key ->
            List pieces=grouped[key]
            Map first=pieces[0]
            def server=first.image.server
            List observed=server.metadata.channels.collect {it.name}
            List indices,nativeNames
            if(cells) {
                nativeNames=positions.collect {configuration.feature_source.channel_names[it]}
                require(nativeNames.every {observed.count(it)==1},"Automatic marker statistics source channel is absent or ambiguous")
                indices=nativeNames.collect {observed.indexOf(it)}
            } else {
                List channels=request.feature_spec.ordered_channels
                require(channels.collect {it.canonical_channel_id}==enc.channel_ids,
                    "Automatic marker statistics channel order differs from the source")
                indices=positions.collect {(channels[it].qupath_channel_index as Number).intValue()}
                nativeNames=positions.collect {channels[it].observed_qupath_channel_name}
                require(indices.indices.every {i->indices[i]>=0 && indices[i]<observed.size() && observed[indices[i]]==nativeNames[i]},
                    "Automatic marker statistics live channel name/index differs")
            }
            List divisors=positions.collect {enc.intensity_divisors[it]}
            def geometry=UnaryUnionOp.union(pieces.collect {it.geometry})
            Map identity=[method:KronosMarkerStatistics.METHOD,project_entry_id:key[0],image_identity_sha256:key[1],
                z:key[2],t:key[3],geometry_sha256:geometrySha(geometry),channel_ids:positions.collect {enc.channel_ids[it]},
                channel_names:nativeNames,channel_indices:indices,intensity_divisors:divisors]
            String cacheKey=jsonSha(identity)
            List moments=partials[cacheKey]
            if(moments==null) {
                try {moments=KronosMarkerStatistics.scan(server,geometry,first.plane,indices as int[],divisors as double[])}
                catch(Exception error) {throw new IOException("Automatic statistics for "+names.join(", ")+" failed in training image "+key[0]+": "+error.message+
                    ". Review the channel assignment and accepted training tissue.",error)}
                require(moments.every {it.count>0},"Automatic statistics for "+names.join(", ")+" in training image "+key[0]+
                    " found no native pixel centers inside accepted training tissue. Review its analysis regions and tissue boundaries.")
                partials[cacheKey]=moments
            }
            totals=totals.indices.collect {i->totals[i].merge(moments[i])}
            sources.add(identity+[intensity_divisors:divisors.collect {float64Bits(it as Number)},
                subject_ids:pieces.collect {it.owner.subject_id}.unique().sort(),
                regions:pieces.collect {it.region}.unique {jsonSha(it)}.sort {jsonSha(it)},
                moments:moments.collect {[count:it.count,mean:float64Bits(it.mean),m2:float64Bits(it.m2)]}])
        }
        List fitted=[]
        totals.eachWithIndex {moment,int i ->
            double sd
            try {
                require(moment.count>=2,"Fewer than two accepted tissue pixels are available")
                require(moment.m2>0,"The selected channel is constant across accepted training tissue")
                sd=moment.sampleStandardDeviation()
            }
            catch(Exception error) {throw new IllegalArgumentException("Automatic statistics for marker "+names[i]+
                " in training entries "+sources.collect {it.project_entry_id}.unique().join(", ")+": "+error.message+
                ". Review the channel assignment and accepted training tissue.",error)}
            fitted.add([marker_name:names[i],count:moment.count,mean:moment.mean,std:sd])
        }
        Map receipt=[schema:"kronos-marker-statistics-receipt/v1",method:KronosMarkerStatistics.METHOD,
            number_encoding:"ieee754-binary64-big-endian-hex",
            population:"native-pixel-centers-in-accepted-material-domain-tissue-union",variance_ddof:1,
            marker_names:names,sources:sources,fitted:fitted.collect {row ->
                row+[mean:float64Bits(row.mean as Number),std:float64Bits(row.std as Number)]}]
        String digest=jsonSha(receipt)
        receipts[digest]=receipt
        List rows=enc.additional_markers.collect {row ->
            if(row.keySet()!=BIOLOGY)return row
            Map values=fitted.find {it.marker_name==row.marker_name}
            row+[mean:values.mean,std:values.std,statistics_source:KronosMarkerStatistics.METHOD+":sha256:"+digest]
        }
        Map resolved=enc+[additional_markers:rows]
        Map config=cells ? configuration+[feature_source:configuration.feature_source+[encoder:resolved]] : configuration+[encoder:resolved]
        [configuration:config,receipt:receipt,receipt_sha256:digest]
    }
}
def adapter=new MifKronosMarkerStatistics()
return [resolve:{Map configuration,Map request,List images->adapter.resolve(configuration,request,images)},
    pending:{Map configuration->MifKronosMarkerStatistics.pending(configuration)},
    receipts:{->new TreeMap(adapter.receipts)}]
