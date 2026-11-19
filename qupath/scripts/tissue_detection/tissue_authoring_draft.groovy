// Create a new editable project from one source entry. Never save the source project.
import java.nio.file.Path
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.StandardOpenOption
import java.security.MessageDigest
import java.awt.image.BufferedImage
import qupath.lib.projects.Projects
import qupath.lib.projects.ProjectIO
import qupath.lib.images.ImageData
import qupath.lib.io.GsonTools
import qupath.lib.objects.PathObject
import com.google.gson.Gson
if(args.length!=2)throw new IllegalArgumentException("Expected a tissue draft request and result")
Path input=Path.of(args[0]),output=Path.of(args[1])
if(Files.size(input)>65536)throw new IllegalArgumentException("The detector draft request is too large")
Map request=new Gson().fromJson(Files.readString(input),Map.class)
if(request.keySet()!=["schema","source_project","source_sha256","source_entry_id","draft_folder"] as Set || request.schema!="tissue-authoring-draft/v1")
    throw new IllegalArgumentException("The detector draft request is invalid")
Path source=Path.of(request.source_project),folder=Path.of(request.draft_folder)
def hash={Path p ->MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(p)).encodeHex().toString()}
if(!source.isAbsolute()||Files.isSymbolicLink(source)||Files.size(source)>4*1024*1024||hash(source)!=request.source_sha256||Files.exists(folder))
    throw new IllegalArgumentException("The project changed or the detector draft folder already exists")
def original=ProjectIO.loadProject(source.toFile(),BufferedImage.class)
def entries=original.imageList.findAll {it.ID.toString()==request.source_entry_id}
if(entries.size()!=1)throw new IllegalArgumentException("The selected image is not in the source project")
def entry=entries[0]
Path dataPath=entry.entryPath.resolve("data.qpdata")
if(Files.isSymbolicLink(dataPath)||!Files.isRegularFile(dataPath)||Files.size(dataPath)>256L*1024*1024)
    throw new IllegalArgumentException("The saved image annotations cannot be read for a detector draft")
String hierarchyHash=hash(dataPath)
def image=entry.readImageData()
try {
    def annotations=image.hierarchy.annotationObjects
    if(annotations.size()>4096||annotations.sum{it.ROI?.numPoints ?: 0}>250000)
        throw new IllegalArgumentException("Use fewer annotation regions to prepare the detector draft")
    String shapes=GsonTools.getInstance().toJson(annotations)
    if(shapes.getBytes("UTF-8").length>8*1024*1024)throw new IllegalArgumentException("The annotation draft exceeds 8 MiB")
    def copied=GsonTools.getInstance().fromJson(shapes,PathObject[].class)
    Files.createDirectories(folder)
    def draft=Projects.createProject(folder.toFile(),BufferedImage.class)
    def target=draft.addImage(entry.serverBuilder)
    target.imageName=entry.imageName
    def data=new ImageData(image.server,image.imageType)
    data.hierarchy.addObjects(copied.toList())
    target.saveImageData(data);
    draft.syncChanges()
    if(hash(source)!=request.source_sha256||hash(dataPath)!=hierarchyHash)
        throw new IllegalArgumentException("The source project or annotations changed while creating the draft")
    Map result=request.findAll {key,value ->key!="draft_folder"}+[project_file:draft.path.toString(),entry_id:target.ID.toString()]
    Files.writeString(output,new Gson().toJson(result),StandardOpenOption.CREATE_NEW)
} finally {image.close()}
