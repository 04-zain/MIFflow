/**
 * Native reader and registration operations for the embedded Qt workspace.
 *
 * This process owns the original QuPath servers and the existing registration
 * helpers. Requests contain bounded original-pixel extents; returned channel
 * buffers keep their actual sample spacing. Qt owns presentation only. A close
 * marker interrupts a running fit and drains its native children before servers
 * close. Completed candidates use the existing save and import contracts.
 */
import java.nio.file.*
import java.nio.*
import java.security.MessageDigest
import java.awt.image.BufferedImage
import java.util.concurrent.*
import groovy.json.JsonSlurper
import groovy.json.JsonOutput
import qupath.lib.regions.RegionRequest
import qupath.lib.projects.ProjectIO
import org.locationtech.jts.operation.union.UnaryUnionOp
import qupath.lib.images.servers.ImageServers
import net.imglib2.realtransform.AffineTransform3D
import java.nio.file.attribute.BasicFileAttributes
import com.fasterxml.jackson.core.JsonFactory
import com.fasterxml.jackson.core.StreamReadFeature
import com.fasterxml.jackson.databind.ObjectMapper

def json=new JsonSlurper()
Path setupPath=Path.of(args[0])
if(Files.isSymbolicLink(setupPath) || !Files.isRegularFile(setupPath) || Files.size(setupPath)>4*1024*1024)
    throw new IllegalArgumentException('The registration session request is unavailable.')
Map setup=json.parse(setupPath.toFile())
boolean cycles=setup.schema=='cycle-registration-session/v1'
Path root=Path.of(setup.exchange_directory)
if(!root.isAbsolute() || root!=setupPath.parent || Files.isSymbolicLink(root))
    throw new IllegalArgumentException('The registration exchange is invalid.')
def digest={byte[] data -> MessageDigest.getInstance('SHA-256').digest(data).encodeHex().toString()}
def loader=qupath.lib.gui.QuPathGUI.getExtensionCatalogManager().extensionClassLoader
Path projectPath=cycles?null:Path.of(setup.project_file),bundle=cycles?null:Path.of(setup.bundle_root)
Map manifest=[:]
if(!cycles) {
    if(!projectPath.isAbsolute() || Files.isSymbolicLink(projectPath) || !Files.isRegularFile(projectPath) ||
       !(setup.bundle_sha256==~/[0-9a-f]{64}/) || bundle!=projectPath.parent.resolve('scripts/bundle-'+setup.bundle_sha256))
        throw new IllegalArgumentException('The registration project and session do not match.')
    byte[] inventory=Files.readAllBytes(bundle.resolve('qupath-script-bundle.v1.json'))
    if(digest(inventory)!=setup.bundle_sha256)throw new IllegalArgumentException('The registration script bundle changed.')
    manifest=json.parse(inventory)
}
def loadHelper={String name ->
    Map expected=cycles?setup.helpers[name]:manifest.assets.find {it.path==name}
    Path path=cycles?Path.of(expected.path):bundle.resolve(name)
    if(cycles && (path.parent!=root || Files.isSymbolicLink(path)))throw new IllegalArgumentException('The cycle helper is invalid.')
    byte[] code=Files.readAllBytes(path)
    if(expected==null || digest(code)!=expected.sha256)throw new IllegalArgumentException('A registration helper changed.')
    new GroovyShell(loader).evaluate(new String(code,java.nio.charset.StandardCharsets.UTF_8),name)
}
Map geometry=loadHelper('registration/paired_geometry.groovy')
Map algorithm=loadHelper('registration/automatic_elastix.groovy')
Map bspline=cycles ? [:] : loadHelper('registration/automatic_bspline.groovy')
Map view=setup.reference_display
boolean remote=setup.remote_ticket_sha256!=null
if(!(view.mode in ['alignment','candidate_overlay','overlay']) || (!cycles && view.fixed_entry_id==view.moving_entry_id))
    throw new IllegalArgumentException('Select distinct fixed and moving images.')
if(remote && (cycles || view.mode!='alignment' || view.method!='manual' || !(setup.remote_ticket_sha256==~/[0-9a-f]{64}/)))
    throw new IllegalArgumentException('Open remote manual alignment from its verified image-pair review.')
def fixedEntry=null,movingEntry=null,image=null,fixed=null,moving=null,movingBuilder=null
// Raw cycles have no paired project or tissue authority. Recheck source stats
// before and after native operations, including cached viewport reads.
def validateCycles={ ->
    if(!cycles)return
    setup.entries.values().each {entry ->
        Path path=Path.of(entry.member.path),member=Files.isDirectory(path)?path.resolve('zarr.json'):path
        Map expected=entry.member.identity
        def actual=Files.readAttributes(member,BasicFileAttributes.class)
        if(path.toRealPath()!=path || !actual.isRegularFile() || actual.size()!=expected.size_bytes ||
           actual.lastModifiedTime().to(TimeUnit.NANOSECONDS)!=expected.modified_time_ns)
            throw new IOException('A cycle source changed after inspection: '+path.fileName)
        if(Files.getFileStore(member).supportsFileAttributeView('unix')) {
            Map unix=Files.readAttributes(member,'unix:dev,ino')
            if(unix.dev!=expected.device_id || unix.ino!=expected.inode_id)
                throw new IOException('A cycle source identity changed: '+path.fileName)
        }
    }
}
if(cycles) {
    validateCycles()
    def builders=setup.entries.collectEntries {role,entry ->
        def support=ImageServers.getImageSupport(Path.of(entry.member.path).toUri(),'--series',entry.member.series_index.toString())
        if(support==null || support.builders.size()!=1)throw new IllegalArgumentException('Select exactly one stitched image per cycle.')
        [(role):support.builders[0]]
    }
    fixed=builders.fixed.build();movingBuilder=builders.moving;moving=movingBuilder.build()
    [fixed:fixed,moving:moving].each {role,server ->
        Map entry=setup.entries[role],grid=entry.grid
        if(server.isRGB() || server.nZSlices()!=1 || server.nTimepoints()!=1 || server.nChannels()!=entry.member.channel_count ||
           server.width!=grid.width || server.height!=grid.height ||
           Math.abs(server.pixelCalibration.pixelWidthMicrons-grid.x_um)>1e-9 ||
           Math.abs(server.pixelCalibration.pixelHeightMicrons-grid.y_um)>1e-9)
            throw new IllegalArgumentException('A cycle reader differs from the inspected planar image.')
    }
} else {
    def project=ProjectIO.loadProject(projectPath.toFile(),BufferedImage.class)
    fixedEntry=project.imageList.find {it.ID.toString()==view.fixed_entry_id}
    movingEntry=project.imageList.find {it.ID.toString()==view.moving_entry_id}
    if(fixedEntry==null || movingEntry==null)throw new IllegalArgumentException('The selected image pair changed.')
    image=fixedEntry.readImageData();fixed=image.server;movingBuilder=movingEntry.serverBuilder;moving=movingBuilder.build()
}
Path transformFile=cycles?root.resolve('cycle-candidate.json'):fixedEntry.entryPath.resolve('transform_'+fixedEntry.ID+'_'+movingEntry.ID+'.json')
def write={Path path,Map value ->
    Path temporary=path.resolveSibling(path.fileName.toString()+'.tmp')
    Files.writeString(temporary,JsonOutput.toJson(value))
    Files.move(temporary,path,StandardCopyOption.REPLACE_EXISTING,StandardCopyOption.ATOMIC_MOVE)
}
def finite={value -> value instanceof Number && Double.isFinite(value.doubleValue())}
def calibration={server ->
    if(!finite(server.pixelCalibration.pixelWidthMicrons) || !finite(server.pixelCalibration.pixelHeightMicrons) ||
       server.pixelCalibration.pixelWidthMicrons<=0 || server.pixelCalibration.pixelHeightMicrons<=0)
        throw new IllegalArgumentException('Both images require positive physical pixel calibration.')
}
calibration(fixed);calibration(moving)
def metadata={server,entry ->
    [width:server.width,height:server.height,x_um:server.pixelCalibration.pixelWidthMicrons,
     y_um:server.pixelCalibration.pixelHeightMicrons,rgb:server.isRGB(),name:entry==null?server.metadata.name:entry.imageName,
     reference_modality:entry==null?null:entry.getMetadataValue('mif_pipeline.reference_modality'),
     image_uri:server.getURIs().first().toString(),channels:server.metadata.channels.withIndex().collect {channel,index ->
         [index:index,label:channel.name,color:String.format('#%06x',channel.color & 0xffffff)]}]
}
Map state=[transform:algorithm.similarity(fixed,moving,0d,0d,0d,1d),global:null,finalReady:false,
           stage:'global',revision:0,movingPoints:[],fixedPoints:[]]
def warped=null
def replaceTransform={transform ->
    warped?.close()
    def reader=movingBuilder.build()
    try {warped=geometry.warped(reader,transform,fixed.width,fixed.height,
        fixed.pixelCalibration.pixelWidthMicrons,fixed.pixelCalibration.pixelHeightMicrons)}
    catch(Throwable error){reader.close();throw error}
    state.transform=transform;state.revision++
}
def tissueObjects=cycles ? [] : image.hierarchy.annotationObjects.findAll {geometry.acceptedTissue(it)}
def tissue=tissueObjects ? UnaryUnionOp.union(tissueObjects.collect {it.ROI.geometry}) : null
def rings={shape ->
    List output=[]
    for(int i=0;i<shape.numGeometries;i++) {
        def part=shape.getGeometryN(i)
        if(part instanceof org.locationtech.jts.geom.Polygon) {
            output.add(part.exteriorRing.coordinates.collect {[it.x,it.y]})
            for(int h=0;h<part.numInteriorRing;h++)output.add(part.getInteriorRingN(h).coordinates.collect {[it.x,it.y]})
        }
    }
    output
}
def outline={ ->
    List points=[];double[] mapped=new double[3]
    for(int edge=0;edge<4;edge++)for(int p=0;p<=128;p++) {
        double t=p/128d
        double x=edge==0?t*moving.width:edge==1?moving.width:edge==2?(1d-t)*moving.width:0d
        double y=edge==0?0d:edge==1?t*moving.height:edge==2?moving.height:(1d-t)*moving.height
        state.transform.apply([x,y,0d] as double[],mapped)
        points.add([mapped[0],mapped[1]])
    }
    points
}
def physicalMatrix={transform ->
    double fx=fixed.pixelCalibration.pixelWidthMicrons,fy=fixed.pixelCalibration.pixelHeightMicrons
    double mx=moving.pixelCalibration.pixelWidthMicrons,my=moving.pixelCalibration.pixelHeightMicrons
    [transform.get(0,0)*fx/mx,transform.get(0,1)*fx/my,transform.get(0,3)*fx,
     transform.get(1,0)*fy/mx,transform.get(1,1)*fy/my,transform.get(1,3)*fy]
}
def fromPhysical={List m ->
    double fx=fixed.pixelCalibration.pixelWidthMicrons,fy=fixed.pixelCalibration.pixelHeightMicrons
    double mx=moving.pixelCalibration.pixelWidthMicrons,my=moving.pixelCalibration.pixelHeightMicrons
    def result=new AffineTransform3D()
    result.set(m[0]*mx/fx,m[1]*my/fx,0d,m[2]/fx,m[3]*mx/fy,m[4]*my/fy,0d,m[5]/fy,0d,0d,1d,0d)
    result
}
def rigid={transform ->
    List m=physicalMatrix(transform)
    double angle=Math.atan2(m[3]-m[1],m[0]+m[4]),c=Math.cos(angle),s=Math.sin(angle)
    if(Math.max(Math.max(Math.abs(m[0]-c),Math.abs(m[1]+s)),Math.max(Math.abs(m[3]-s),Math.abs(m[4]-c)))>1e-4)
        throw new IllegalArgumentException('Cycle alignment must remain rigid in physical coordinates.')
    fromPhysical([c,-s,m[2],s,c,m[5]])
}
def currentPose={ ->
    List m=physicalMatrix(state.transform)
    double mx=(moving.width-1)*moving.pixelCalibration.pixelWidthMicrons/2d,my=(moving.height-1)*moving.pixelCalibration.pixelHeightMicrons/2d
    [m[2]+m[0]*mx+m[1]*my-(fixed.width-1)*fixed.pixelCalibration.pixelWidthMicrons/2d,
     m[5]+m[3]*mx+m[4]*my-(fixed.height-1)*fixed.pixelCalibration.pixelHeightMicrons/2d,
     Math.toDegrees(Math.atan2(m[3],m[0])),1d]
}
def status={ ->
    Map result=[stage:state.stage,revision:state.revision,global_ready:state.global!=null,
                final_ready:state.finalReady,outline:outline()]
    if(cycles)result.putAll([matrix:physicalMatrix(state.transform),pose:currentPose()])
    result
}
def sampling={ ->
    def sampled=geometry.reviewSampling(image,moving,state.transform)
    return sampled.scopes.collect {scope ->
        def result=scope.result
        def grid=[x:result.x(),y:result.y(),spacing:result.spacing(),columns:result.columns(),rows:result.rows()]
        grid.states=result.states().collect {it as int}
        grid.area_ratios=result.areaRatios().collect {Double.isFinite(it) ? it : null}
        return [name:scope.name,evidence:result.evidence(),rings:(scope.geometry==null ? [] : rings(scope.geometry)),grid:grid]
    }
}
def cleanViews={Path folder ->
    Files.walk(folder).withCloseable {files -> files.filter {Files.isRegularFile(it) && it.toString().endsWith('.mha')}.forEach {Files.deleteIfExists(it)}}
}
def integer={Map values,String name,int lo,int hi ->
    def value=values[name]
    if(!finite(value) || value.doubleValue()!=value.intValue() || value.intValue()<lo || value.intValue()>hi)
        throw new IllegalArgumentException('The '+name.replace('_',' ')+' setting is outside its supported range.')
    value.intValue()
}
def positive={Map values,String name ->
    if(!finite(values[name]) || values[name]<=0)throw new IllegalArgumentException('Enter a positive '+name.replace('_',' ')+'.')
    values[name].doubleValue()
}
def poseValues={spec ->
    List pose=spec.pose ?: [0d,0d,0d,1d]
    if(pose.size()!=4 || !pose.every {finite(it)} || pose[3]<=0)throw new IllegalArgumentException('Enter a finite starting position and positive scale.')
    pose.collect {it.doubleValue()}
}
// Remote manual alignment retains the existing cluster exchange contracts. The
// Qt controls use these same readers and transformations; affine fitting and
// final publication remain owned by the cluster.
def strictMapper=new ObjectMapper(JsonFactory.builder().enable(StreamReadFeature.STRICT_DUPLICATE_DETECTION).build())
def readRecord={Path path ->
    if(Files.isSymbolicLink(path) || !Files.isRegularFile(path) || Files.size(path)>1024*1024)
        throw new IllegalArgumentException('The saved registration draft is unavailable or too large.')
    strictMapper.readValue(Files.readString(path),Map.class)
}
Path draftFile=remote?projectPath.parent.resolve('landmarks.json'):null
Map restoredAlignment=[global:null,final:null]
Map remoteReceipt=null
long draftRevision=0
def draftRecord={Map alignment,boolean ready ->
    [schema:'manual-alignment-draft/v1',ticket_sha256:setup.remote_ticket_sha256,
     ready_for_return:ready,alignment:alignment]
}
def writeDraft={Map alignment,boolean ready ->
    Path temporary=Files.createTempFile(draftFile.parent,'.alignment-','.json')
    try {
        Files.write(temporary,strictMapper.writeValueAsBytes(draftRecord(alignment,ready)))
        java.nio.channels.FileChannel.open(temporary,StandardOpenOption.WRITE).withCloseable {it.force(true)}
        try {Files.move(temporary,draftFile,StandardCopyOption.ATOMIC_MOVE,StandardCopyOption.REPLACE_EXISTING)}
        catch(AtomicMoveNotSupportedException ignored){Files.move(temporary,draftFile,StandardCopyOption.REPLACE_EXISTING)}
    } finally {Files.deleteIfExists(temporary)}
}
def globalInputs={Map spec ->
    List pose=poseValues(spec)
    Map options=new LinkedHashMap(setup.editor_defaults.registration);options.putAll(spec.settings ?: [:])
    options=options.subMap(['fixed_channel','moving_channel','max_dimension','iterations','threads','orientation_search'])
    Map alignment=[schema:'manual-alignment/v1',stage:'global',
        starting_pose:[x_um:pose[0],y_um:pose[1],rotation_degrees:pose[2],scale:pose[3]],
        use_starting_pose:spec.use_pose,global_settings:options,
        tools_directory:restoredAlignment.global?.tools_directory]
    geometry.validateManualAlignment(alignment,fixed,moving)
}
def finalInputs={Map spec,boolean unfinished ->
    if(state.global==null || remoteReceipt==null)throw new IllegalArgumentException('Review the returned global affine before local refinement.')
    if(!(spec.method in ['global','landmarks']))throw new IllegalArgumentException('Choose global alignment or manual landmark refinement.')
    boolean refine=spec.method=='landmarks'
    Map alignment=[schema:'manual-alignment/v1',stage:'final',global_fit_id:remoteReceipt.global_fit_id,
        global_transform_sha256:remoteReceipt.global_transform_sha256,local_refinement:refine,
        local_moving_points:refine?(spec.moving_points ?: []):[],local_fixed_points:refine?(spec.fixed_points ?: []):[],
        maximum_displacement_um:spec.maximum_displacement_um!=null?spec.maximum_displacement_um:setup.editor_defaults.manual_registration.maximum_displacement_um]
    geometry.validateManualAlignment(alignment,fixed,moving,unfinished)
}
def invalidateSavedDraft={ ->
    if(Files.exists(draftFile)) {
        Map previous=readRecord(draftFile)
        if(previous.schema=='manual-alignment-draft/v1' && previous.ticket_sha256==setup.remote_ticket_sha256)
            writeDraft(previous.alignment,false)
    }
}
def consumeDraft={ ->
    if(!remote)return
    if(Files.exists(root.resolve('draft-invalid')))invalidateSavedDraft()
    Path marker=root.resolve('draft-inputs.json')
    if(!Files.exists(marker))return
    Map inputs=readRecord(marker)
    if(!(inputs.revision instanceof Number) || inputs.revision<=draftRevision)return
    // Invalidate readiness before validating edited settings, so invalid current
    // inputs cannot return an earlier explicitly saved request on close.
    invalidateSavedDraft()
    state.finalReady=false
    if(inputs.global_changed){state.global=null;remoteReceipt=null;state.stage='global'}else state.stage='local'
    Map alignment=state.global==null?globalInputs(inputs):finalInputs(inputs,true)
    writeDraft(alignment,false)
    if(alignment.stage=='global')restoredAlignment.global=alignment
    else restoredAlignment.final=alignment
    draftRevision=inputs.revision as long
}
def restoreRemote={ ->
    Map session=readRecord(projectPath.parent.resolve('landmark-session.json'))
    if(session.ticket?.ticket_sha256!=setup.remote_ticket_sha256 ||
       session.entries!=[fixed:view.fixed_entry_id,moving:view.moving_entry_id])
        throw new IllegalArgumentException('The remote alignment belongs to a different image-pair ticket.')
    Map saved=null
    if(Files.exists(draftFile)) {
        saved=readRecord(draftFile)
        if(saved.keySet()!=['schema','ticket_sha256','ready_for_return','alignment'] as Set ||
           saved.schema!='manual-alignment-draft/v1' || saved.ticket_sha256!=setup.remote_ticket_sha256 ||
           !(saved.ready_for_return instanceof Boolean))throw new IllegalArgumentException('The saved manual alignment belongs to another review.')
        saved.alignment=geometry.validateManualAlignment(saved.alignment,fixed,moving,!saved.ready_for_return)
    }
    Path receiptFile=projectPath.parent.resolve('global-fit.json')
    if(Files.exists(receiptFile)) {
        Map receipt=readRecord(receiptFile)
        if(receipt.keySet()!=['schema','ticket_sha256','global_fit_id','global_transform_sha256','global_transform_file','alignment','global_metrics'] as Set ||
           receipt.schema!='manual-global-view/v1' || receipt.ticket_sha256!=setup.remote_ticket_sha256 ||
           !(receipt.global_fit_id==~/[0-9a-f]{64}/) || !(receipt.global_transform_sha256==~/[0-9a-f]{64}/) ||
           receipt.global_transform_file!='global-affine-'+receipt.global_fit_id+'.json' || !(receipt.global_metrics instanceof Map))
            throw new IllegalArgumentException('The returned global affine belongs to another registration review.')
        receipt.alignment=geometry.validateManualAlignment(receipt.alignment,fixed,moving)
        if(receipt.alignment.stage!='global')throw new IllegalArgumentException('The affine result is missing its starting pose and image-fitting settings.')
        boolean matches=saved==null || (saved.alignment.stage=='global'?saved.alignment==receipt.alignment:
            saved.alignment.global_fit_id==receipt.global_fit_id && saved.alignment.global_transform_sha256==receipt.global_transform_sha256)
        if(matches) {
            Path transform=projectPath.parent.resolve(receipt.global_transform_file)
            if(Files.isSymbolicLink(transform) || !Files.isRegularFile(transform) || Files.size(transform)>16*1024*1024 ||
               digest(Files.readAllBytes(transform))!=receipt.global_transform_sha256)
                throw new IllegalArgumentException('The returned global affine changed.')
            state.global=geometry.load(transform)
            if(!(state.global instanceof AffineTransform3D))throw new IllegalArgumentException('The returned global result is not an affine.')
            state.transform=state.global;state.stage='global_review';remoteReceipt=receipt
            state.globalEstimate=receipt.global_metrics;state.globalSettings=receipt.alignment.global_settings
            if(saved?.alignment?.stage=='global' && saved.ready_for_return)writeDraft(saved.alignment,false)
        }
    }
    if(saved?.alignment?.stage=='final' && state.global==null)
        throw new IllegalArgumentException('Restore the global affine associated with this local draft before continuing.')
    restoredAlignment.global=saved?.alignment?.stage=='global'?saved.alignment:remoteReceipt?.alignment
    restoredAlignment.final=saved?.alignment?.stage=='final'?saved.alignment:null
    if(state.global==null && restoredAlignment.global) {
        Map pose=restoredAlignment.global.starting_pose
        state.transform=algorithm.similarity(fixed,moving,pose.x_um as double,pose.y_um as double,pose.rotation_degrees as double,pose.scale as double)
    }
    if(restoredAlignment.final) {
        state.movingPoints=restoredAlignment.final.local_moving_points;state.fixedPoints=restoredAlignment.final.local_fixed_points
        if(saved.ready_for_return) {
            Map alignment=restoredAlignment.final
            Map fitted=[transform:state.global,method:'global-affine']
            if(alignment.local_refinement)fitted=geometry.refineLandmarks(state.global,state.movingPoints,state.fixedPoints,
                fixed.pixelCalibration.pixelWidthMicrons,fixed.pixelCalibration.pixelHeightMicrons)
            fitted.qc=geometry.checkDeformation(fitted.transform,state.global,fixed,tissue,alignment.maximum_displacement_um as double)
            state.transform=fitted.transform;state.localEstimate=fitted;state.remoteFinal=alignment
            state.finalReady=true;state.stage='review'
        }
    }
}
def transformPoints={List points,boolean inverse ->
    if(points.size()>10000)throw new IllegalArgumentException('Too many point queries.')
    double[] mapped=new double[3]
    points.collect {p ->
        if(!(p instanceof List) || p.size()!=2 || !p.every {finite(it)})throw new IllegalArgumentException('Invalid point coordinates.')
        (inverse?state.transform.inverse():state.transform).apply([p[0],p[1],0d] as double[],mapped)
        [mapped[0],mapped[1]]
    }
}
def execute={Map spec ->
    String operation=spec.operation
    validateCycles()
    consumeDraft()
    if(cycles && operation=='local')throw new IllegalArgumentException('Cycle registration has no local deformation stage.')
    if(!(operation in ['render','points','status','pose','affine','local','invalidate','save']))throw new IllegalArgumentException('Unknown registration operation.')
    if(view.mode!='alignment' && operation in ['pose','affine','local','invalidate','save'])throw new IllegalArgumentException('A saved alignment review cannot change its transformation.')
    if(operation=='status')return status()
    if(operation=='points')return [points:transformPoints(spec.points as List,spec.direction!='moving_to_fixed'),revision:state.revision]
    if(operation=='invalidate') {
        state.finalReady=false
        if(spec.global){state.global=null;state.stage='global'}else state.stage='local'
        return status()
    }
    if(operation=='render') {
        if(spec.space!=null && !(spec.space in ['fixed','moving']))throw new IllegalArgumentException('Select the fixed or original paired-image coordinate space.')
        boolean original=spec.space=='moving'
        def coordinateServer=original?moving:fixed
        int x=integer(spec,'x',0,coordinateServer.width-1),y=integer(spec,'y',0,coordinateServer.height-1)
        int w=integer(spec,'width',1,coordinateServer.width-x),h=integer(spec,'height',1,coordinateServer.height-y)
        double ds=positive(spec,'downsample')
        if(Math.max(w,h)/ds>2048d)throw new IllegalArgumentException('The requested viewport is too large.')
        List sources=original ? ['moving'] : (spec.sources ?: ['fixed','moving'])
        if(sources.isEmpty() || sources.size()>2 || sources.toSet().size()!=sources.size() || !sources.every {it in ['fixed','moving']})
            throw new IllegalArgumentException('Select fixed or paired image pixels.')
        List outputs=[]
        long start=System.nanoTime()
        for(String source:sources) {
            def server=source=='fixed'?fixed:(original?moving:warped)
            List channels=spec[source+'_channels'] ?: (server.isRGB() ? [0,1,2] : [0])
            if(channels.isEmpty() || channels.size()>64 || channels.toSet().size()!=channels.size() ||
               !channels.every {it instanceof Number && it.intValue()==it.doubleValue() && it>=0 && it<server.nChannels()} ||
               Math.ceil(w/ds)*Math.ceil(h/ds)*channels.size()>16777216d)
                throw new IllegalArgumentException('The selected channel viewport is too large or invalid.')
            if(Math.ceil(w/ds)*Math.ceil(h/ds)*server.nChannels()>67108864d)
                throw new IllegalArgumentException('Reduce the native channel viewport before reading it.')
            def pixels=server.readRegion(RegionRequest.createInstance(server.path,ds,x,y,w,h)).raster
            if(pixels.width>2048 || pixels.height>2048 || pixels.width*(long)pixels.height*channels.size()>16777216L)
                throw new IllegalArgumentException('The native reader returned an oversized viewport.')
            float[] values=new float[pixels.width*pixels.height]
            def bytes=ByteBuffer.allocate(values.length*4*channels.size()).order(ByteOrder.LITTLE_ENDIAN)
            def floats=bytes.asFloatBuffer()
            boolean[] outside=source=='moving' && !original && spec.mask_outside_moving==true ?
                geometry.uncoveredPixels(state.transform,x,y,ds,pixels.width,pixels.height,moving.width,moving.height) : null
            for(int channel:channels) {
                pixels.getSamples(0,0,pixels.width,pixels.height,channel,values)
                if(outside!=null)for(int i=0;i<values.length;i++)if(outside[i])values[i]=Float.NaN
                floats.put(values)
            }
            String name='pixels-'+spec.id+'-'+source+'.f32'
            Files.write(root.resolve(name),bytes.array())
            outputs.add([source:source,file:name,shape:[channels.size(),pixels.height,pixels.width],rgb:server.isRGB(),channels:channels])
        }
        return [x:x,y:y,width:w,height:h,downsample:ds,space:original?'moving':'fixed',images:outputs,
                revision:state.revision,read_ms:(System.nanoTime()-start)/1e6]
    }
    if(operation=='pose') {
        if(remote)invalidateSavedDraft()
        state.global=null;state.finalReady=false;state.stage='global'
        List pose=poseValues(spec)
        if(cycles && pose[3]!=1d)throw new IllegalArgumentException('Cycle registration does not change physical scale.')
        replaceTransform(algorithm.similarity(fixed,moving,*pose))
        if(cycles){state.global=state.transform;state.finalReady=true;state.globalEstimate=[method:'manual-rigid'];state.globalSettings=[:] }
        return status()
    }
    if(operation=='affine') {
        state.global=null;state.finalReady=false;state.stage='global'
        if(remote) {
            invalidateSavedDraft()
            Map alignment=globalInputs(spec)
            writeDraft(alignment,true);restoredAlignment.global=alignment;remoteReceipt=null
            return status()+[remote_saved_stage:'global']
        }
        Map options=new LinkedHashMap(setup.editor_defaults.registration);options.putAll(spec.settings ?: [:])
        if(cycles){
            options.transform_model='rigid'
            if(options.fixed_channel!=setup.editor_defaults.registration.fixed_channel ||
               options.moving_channel!=setup.editor_defaults.registration.moving_channel ||
               options.fixed_channel<0 || options.moving_channel<0)
                throw new IllegalArgumentException('Use the selected shared-stain channel in each cycle.')
        }
        integer(options,'max_dimension',128,4096);integer(options,'iterations',32,2048);integer(options,'threads',1,16)
        integer(options,'fixed_channel',-1,fixed.nChannels()-1);integer(options,'moving_channel',-1,moving.nChannels()-1)
        if(!(options.orientation_search instanceof Boolean))throw new IllegalArgumentException('Choose whether to search orientations.')
        if(!view.tools_directory)throw new IllegalArgumentException('Install Elastix and Transformix in Software setup.')
        def initial=spec.use_pose?algorithm.similarity(fixed,moving,*poseValues(spec)):null
        Path folder=Files.createTempDirectory(cycles?root:fixedEntry.entryPath,'registration-fit-')
        Map fitted
        try {fitted=algorithm.align(fixed,moving,options,Path.of(view.tools_directory),folder,tissue,initial)}
        finally {cleanViews(folder)}
        if(cycles)fitted.transform=rigid(fitted.transform)
        replaceTransform(fitted.transform);state.global=fitted.transform;state.globalEstimate=fitted;state.globalSettings=options
        if(cycles)state.finalReady=true
        state.fixedPoints=[];state.movingPoints=[];state.stage='global_review'
        return status()+[estimate:fitted.findAll {key,value -> key!='transform'}]
    }
    if(operation=='local') {
        if(state.global==null)throw new IllegalArgumentException('Calculate global affine alignment first.')
        state.finalReady=false
        String method=spec.method
        if(!(method in ['global','landmarks','bspline']))throw new IllegalArgumentException('Select a local refinement method.')
        Map options=new LinkedHashMap(setup.editor_defaults.bspline);options.putAll(spec.settings ?: [:])
        Map remoteFinal=null
        if(remote) {
            invalidateSavedDraft()
            remoteFinal=finalInputs(spec+[maximum_displacement_um:options.maximum_displacement_um],false)
        }
        Map fitted=[transform:state.global,method:'global-affine'];Map stored=[:]
        if(method=='landmarks') {
            List a=spec.moving_points ?: [],b=spec.fixed_points ?: []
            if(a.size()>1000 || a.size()!=b.size() || ![a,b].every {list -> list.every {p -> p instanceof List && p.size()==2 && p.every {finite(it)}}})
                throw new IllegalArgumentException('Provide matching finite landmark pairs.')
            double maximum=positive(options,'maximum_displacement_um')
            fitted=geometry.refineLandmarks(state.global,a,b,fixed.pixelCalibration.pixelWidthMicrons,fixed.pixelCalibration.pixelHeightMicrons)
            fitted.qc=geometry.checkDeformation(fitted.transform,state.global,fixed,tissue,maximum)
            state.movingPoints=a;state.fixedPoints=b;stored=[maximum_displacement_um:maximum]
        } else if(method=='bspline') {
            integer(options,'max_dimension',128,4096);integer(options,'iterations',32,2048)
            positive(options,'grid_spacing_um');positive(options,'maximum_displacement_um')
            if(!finite(options.bending_weight) || options.bending_weight<0 || options.bending_weight>1)throw new IllegalArgumentException('Enter a bending weight from 0 to 1.')
            ['fixed_channel','moving_channel','threads'].each {options[it]=state.globalSettings[it]}
            Path folder=Files.createTempDirectory(cycles?root:fixedEntry.entryPath,'registration-fit-')
            try {fitted=bspline.align(fixed,moving,state.global,options,Path.of(view.tools_directory),folder,tissue,algorithm,geometry)}
            finally {cleanViews(folder)}
            stored=options
        }
        replaceTransform(fitted.transform)
        def scopes=sampling()
        state.localEstimate=fitted;state.localSettings=stored;state.finalReady=true;state.stage='review'
        if(remote)state.remoteFinal=remoteFinal
        return status()+[scopes:scopes,estimate:fitted.findAll {key,value -> key!='transform'}]
    }
    if(operation=='save') {
        if(!state.finalReady || state.global==null)throw new IllegalArgumentException('Calculate and inspect the final alignment before saving.')
        if(remote) {
            if(state.remoteFinal==null)throw new IllegalArgumentException('Calculate the current local choice before saving.')
            writeDraft(state.remoteFinal,true)
            return status()+[remote_saved_stage:'final']
        }
        if(cycles) {
            Map result=status()+[evidence:[method:state.globalEstimate?.method ?: 'manual-rigid',
                estimate:state.globalEstimate?.findAll {key,value -> key!='transform'} ?: [:],settings:state.globalSettings ?: [:]]]
            write(transformFile,result)
            return result
        }
        Map details=[schema:'staged-registration-candidate/v2',route:view.method=='automatic'?'Automatic registration':'Manual registration',
            fixedEntry:fixedEntry.ID.toString(),movingEntry:movingEntry.ID.toString(),
            global:state.globalEstimate.findAll {key,value -> key!='transform'},globalSettings:state.globalSettings,
            local:state.localEstimate.findAll {key,value -> key!='transform'},localSettings:state.localSettings,
            movingPoints:state.movingPoints,fixedPoints:state.fixedPoints]
        geometry.save(transformFile,state.transform,details)
        geometry.load(transformFile)
        state.stage='saved'
        return status()+[path:transformFile.toString(),sha256:digest(Files.readAllBytes(transformFile))]
    }
}
def executor=Executors.newSingleThreadExecutor()
try {
    if(remote)restoreRemote()
    if(view.mode=='candidate_overlay') {
        if(Files.isSymbolicLink(transformFile) || !Files.isRegularFile(transformFile) || Files.size(transformFile)>16*1024*1024 ||
           digest(Files.readAllBytes(transformFile))!=view.candidate_transform_sha256)
            throw new IllegalArgumentException('The candidate differs from the pending registration review.')
        state.transform=geometry.load(transformFile);state.finalReady=true;state.stage='review'
    } else if(view.mode=='overlay') {
        Map record=geometry.approved(transformFile,fixedEntry.ID.toString(),movingEntry.ID.toString(),setup.paired_views ?: [])
        Path fixedPath=Path.of(new URI(record.fixed_image_uri)).normalize(),movingPath=Path.of(new URI(record.moving_image_uri)).normalize()
        if(!fixed.getURIs().any {it.scheme=='file' && Path.of(it).normalize()==fixedPath} ||
           !moving.getURIs().any {it.scheme=='file' && Path.of(it).normalize()==movingPath})
            throw new IllegalArgumentException('The paired image source changed after registration review.')
        state.transform=geometry.load(transformFile);state.finalReady=true;state.stage='review'
    }
    if(cycles){
        state.transform=rigid(fromPhysical(setup.initial_transform));state.global=state.transform;state.finalReady=true
        state.globalEstimate=new LinkedHashMap(setup.initial_evidence?.estimate ?: [:])
        state.globalEstimate.method=setup.initial_evidence?.method ?: 'manual-rigid'
        state.globalSettings=setup.initial_evidence?.settings ?: [:]
    }
    replaceTransform(state.transform)
    write(root.resolve('ready.json'),[fixed:metadata(fixed,fixedEntry),moving:metadata(moving,movingEntry),
        mode:view.mode,method:view.method ?: 'manual',defaults:setup.editor_defaults,policy:view.review_policy ?: [:],
        remote_execution:remote,restored_alignment:remote?restoredAlignment:null,
        pose:cycles?currentPose():null,automatic_available:cycles?view.tools_directory!=null:true,
        tissue:tissueObjects.collect {object -> [name:object.name ?: 'Tissue',rings:rings(object.ROI.geometry)]},
        scopes:view.mode=='alignment' && !state.finalReady ? [] : sampling(),state:status()])
    int next=0
    while(!Files.exists(root.resolve('close'))) {
        Path request=root.resolve('request-'+next+'.json')
        if(!Files.exists(request)){Thread.sleep(20);continue}
        Map spec
        try {
            if(Files.isSymbolicLink(request) || Files.size(request)>4*1024*1024)throw new IllegalArgumentException('Invalid registration request.')
            spec=json.parse(request.toFile())
            if(spec.id!=next)throw new IllegalArgumentException('The request belongs to another registration operation.')
            Future future=executor.submit({def result=execute(spec);validateCycles();result} as Callable)
            while(!future.isDone()) {
                if(Files.exists(root.resolve('close')) && spec.operation!='save'){future.cancel(true);break}
                Thread.sleep(20)
            }
            if(!future.isCancelled())write(root.resolve('response-'+next+'.json'),[id:next,result:future.get()])
        } catch(Throwable error) {
            def cause=error instanceof ExecutionException ? error.cause : error
            write(root.resolve('response-'+next+'.json'),[id:next,error:cause.toString()]);cause.printStackTrace()
        }
        next++
    }
} catch(Throwable error) {
    if(!Files.exists(root.resolve('ready.json')))write(root.resolve('ready.json'),[error:error.toString()])
    throw error
} finally {
    executor.shutdownNow()
    if(!executor.awaitTermination(90,TimeUnit.SECONDS))throw new IllegalStateException('Registration computation did not stop; readers remain retained until process exit.')
    try {consumeDraft()}
    finally {warped?.close();moving.close();fixed.close()}
}
System.exit(0)
