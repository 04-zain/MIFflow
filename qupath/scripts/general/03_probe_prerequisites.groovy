// Diagnostic only: no project, image, inference, model download, or save.
import com.google.gson.GsonBuilder
import com.google.gson.stream.JsonReader
import com.google.gson.stream.JsonToken
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.LinkOption
import java.nio.file.StandardOpenOption
import java.nio.file.attribute.BasicFileAttributes
import java.security.MessageDigest

class ProbeIncompatible extends RuntimeException {
    ProbeIncompatible(String message) { super(message) }
}

def reject = { String message -> throw new ProbeIncompatible(message) }
if (args == null || args.length != 4) reject('Expected four probe arguments')
def requestPath = Path.of(args[0])
def evidencePath = Path.of(args[1])
def cacheRoot = Path.of(args[3])
if (![requestPath, evidencePath, cacheRoot].every {
    it.isAbsolute() && it == it.normalize()
}) reject('Probe paths must be absolute and normalized')
if (Files.size(requestPath) > 65536) reject('Request exceeds limit')
def request = [:]
Files.newBufferedReader(requestPath).withCloseable { input ->
    def reader = new JsonReader(input)
    reader.beginObject()
    while (reader.hasNext()) {
        def key = reader.nextName()
        if (request.containsKey(key)) reject('Duplicate request field')
        if (key == 'require_warpy' && reader.peek() == JsonToken.BOOLEAN)
            request[key] = reader.nextBoolean()
        else if (key in ['model_version', 'model_inventory_sha256'] && reader.peek() == JsonToken.NULL) {
            reader.nextNull()
            request[key] = null
        }
        else if (key in ['schema', 'challenge', 'model_version', 'model_inventory_sha256'] &&
                 reader.peek() == JsonToken.STRING)
            request[key] = reader.nextString()
        else reject('Unknown request field or invalid value type')
    }
    reader.endObject()
    if (reader.peek() != JsonToken.END_DOCUMENT) reject('Trailing request data')
}
if (request.keySet() != ['schema', 'challenge', 'model_version',
                         'model_inventory_sha256', 'require_warpy'].toSet() ||
    request.schema != 'qupath-prerequisite-probe/v3' ||
    !(request.challenge ==~ /[0-9a-f]{64}/) ||
    !((request.model_version == null && request.model_inventory_sha256 == null && args[2] == '') ||
      (request.model_version instanceof String && request.model_version &&
       request.model_version.size() <= 256 &&
       request.model_inventory_sha256 instanceof String &&
       (request.model_inventory_sha256 ==~ /[0-9a-f]{64}/) && args[2] != '')))
    reject('Invalid request contract')
def modelRoot = args[2] == '' ? null : Path.of(args[2])
if (modelRoot != null && (!modelRoot.isAbsolute() || modelRoot != modelRoot.normalize()))
    reject('Model path must be absolute and normalized')

def noLinks = { Path path ->
    for (def current = path; current != null; current = current.parent) {
        def attributes = Files.readAttributes(current, BasicFileAttributes, LinkOption.NOFOLLOW_LINKS)
        if (attributes.isSymbolicLink() || attributes.isOther()) reject('Unsafe filesystem member')
    }
}
noLinks(cacheRoot)
if (!Files.isDirectory(cacheRoot, LinkOption.NOFOLLOW_LINKS)) reject('Cache must be a directory')
// The Python launcher also sets these before JVM startup. Environment overrides
// system properties in DJL; reject conflicting inherited configuration.
def settings = [DJL_OFFLINE: 'true', DJL_DEFAULT_ENGINE: 'PyTorch',
                DJL_CACHE_DIR: cacheRoot.toString(), ENGINE_CACHE_DIR: cacheRoot.toString()]
settings.each { key, value ->
    if (System.getenv(key) != null && System.getenv(key) != value)
        reject('Conflicting DJL environment')
    System.setProperty(key, value)
}
System.setProperty('ai.djl.offline', 'true')
System.setProperty('OPT_OUT_TRACKING', 'true')

def hashFile = { Path path ->
    noLinks(path)
    def before = Files.readAttributes(path, BasicFileAttributes, LinkOption.NOFOLLOW_LINKS)
    if (!before.isRegularFile()) reject('Artifact is not a regular file')
    def digest = MessageDigest.getInstance('SHA-256')
    Files.newInputStream(path, StandardOpenOption.READ, LinkOption.NOFOLLOW_LINKS).withCloseable { input ->
        byte[] buffer = new byte[1024 * 1024]
        int count
        while ((count = input.read(buffer)) != -1) digest.update(buffer, 0, count)
    }
    noLinks(path)
    def after = Files.readAttributes(path, BasicFileAttributes, LinkOption.NOFOLLOW_LINKS)
    if (before.fileKey() != after.fileKey() || before.size() != after.size() ||
        before.lastModifiedTime() != after.lastModifiedTime()) reject('Artifact changed during hashing')
    HexFormat.of().formatHex(digest.digest())
}
def inventory = {
    noLinks(modelRoot)
    if (!Files.isDirectory(modelRoot, LinkOption.NOFOLLOW_LINKS)) reject('Model root is not a directory')
    def listMembers = {
        def members = []
        Files.walk(modelRoot).withCloseable { paths ->
            paths.forEach { path ->
                noLinks(path)
                if (Files.isRegularFile(path, LinkOption.NOFOLLOW_LINKS))
                    members.add(modelRoot.relativize(path).toString().replace('\\', '/'))
                else if (!Files.isDirectory(path, LinkOption.NOFOLLOW_LINKS))
                    reject('Unsafe model member')
            }
        }
        members.sort()
    }
    def members = listMembers()
    if (!members.contains('rdf.yaml')) throw new java.nio.file.NoSuchFileException('rdf.yaml')
    def digest = MessageDigest.getInstance('SHA-256')
    members.each { name ->
        digest.update(name.getBytes('UTF-8'))
        digest.update((byte) 0)
        digest.update(hashFile(modelRoot.resolve(name)).getBytes('US-ASCII'))
        digest.update((byte) 10)
    }
    if (members != listMembers()) reject('Model inventory changed during hashing')
    HexFormat.of().formatHex(digest.digest())
}
def loadClass = { String name -> Class.forName(name, false, this.class.classLoader) }
def observe = { Map empty, Closure operation ->
    try {
        empty + operation() + [status: 'ready', diagnostic: 'Live observation completed']
    } catch (ClassNotFoundException | NoClassDefFoundError | java.nio.file.NoSuchFileException error) {
        empty + [status: 'missing', diagnostic: 'Required local component is absent']
    } catch (ProbeIncompatible | IllegalArgumentException | UnsupportedOperationException |
             LinkageError | IOException error) {
        empty + [status: 'incompatible', diagnostic: 'Local component is incompatible: ' + error.class.simpleName]
    } catch (RuntimeException error) {
        if (error.class.name != 'ai.djl.engine.EngineException' &&
            error.class.name != 'com.google.gson.JsonParseException' &&
            error.class.name != 'com.google.gson.JsonSyntaxException') throw error
        empty + [status: 'incompatible', diagnostic: 'Local component is incompatible: ' + error.class.simpleName]
    }
    // Unexpected programming/process errors intentionally escape; Python emits error.
}
def extension = { String className ->
    observe([loaded_version: null, code_source: null, code_source_sha256: null]) {
        def type = loadClass(className)
        def location = type.getProtectionDomain().getCodeSource()?.getLocation()
        if (location == null || location.protocol != 'file') reject('Extension has no local code source')
        def source = Path.of(location.toURI())
        def version = type.getPackage().getImplementationVersion()
        if (!version) reject('Extension has no implementation version')
        [loaded_version: version.toString(), code_source: source.toString(), code_source_sha256: hashFile(source)]
    }
}
def evidence = [schema: request.schema, challenge: request.challenge,
                qupath_version: qupath.lib.common.GeneralTools.getVersion(),
                preferences_namespace: qupath.lib.gui.prefs.PathPrefs.getUserPreferences().absolutePath(),
                user_directory: qupath.lib.gui.UserDirectoryManager.getInstance().getUserPath()?.toString()]
evidence.instanseg = extension('qupath.ext.instanseg.ui.InstanSegExtension')
evidence.djl = extension('qupath.ext.djl.DjlExtension')
evidence.model = modelRoot == null ? null : observe([version: null, inventory_sha256: null, valid: false]) {
    def checkedDigest = inventory()
    if (checkedDigest != request.model_inventory_sha256) reject('Model inventory mismatch')
    def Model = loadClass('qupath.bioimageio.spec.Model')
    def parsed = Model.parse(modelRoot)
    if (parsed.getVersion() != request.model_version) reject('Model version mismatch')
    def outputs = parsed.getOutputs()
    if (!outputs) reject('Missing model output')
    def axes = outputs.first().getAxes()
    def scaledAxis = loadClass('qupath.bioimageio.spec.tensor.axes.ScaledAxis')
    ['x', 'y'].each { id ->
        def matching = axes == null ? [] : axes.findAll { it.getID() == id }
        if (matching.size() != 1 || !scaledAxis.isInstance(matching.first()) ||
            matching.first().getType().name() != id.toUpperCase(Locale.ROOT) ||
            matching.first().getUnit()?.name() != 'MICROMETER') reject('Invalid model spatial axes')
        double scale = matching.first().getScale()
        if (!Double.isFinite(scale) || scale <= 0) reject('Invalid model spatial scale')
    }
    def InstanSegModel = loadClass('qupath.ext.instanseg.core.InstanSegModel')
    def nativeModel = InstanSegModel.fromPath(modelRoot)
    if (nativeModel == null || !nativeModel.isValid()) reject('Invalid InstanSeg model')
    if (inventory() != checkedDigest) reject('Model changed while parsing')
    [version: parsed.getVersion(), inventory_sha256: checkedDigest, valid: true]
}
evidence.cpu_engine = observe([engine_name: null, native_loaded: false, tensor_scalar: null,
                               code_source: null, code_source_sha256: null]) {
    def NDManager = loadClass('ai.djl.ndarray.NDManager')
    def Device = loadClass('ai.djl.Device')
    // Explicit CPU and engine overload avoids GPU/default-engine selection.
    NDManager.newBaseManager(Device.cpu(), 'PyTorch').withCloseable { manager ->
        def tensor = manager.create([[1.0f] as float[]] as float[][])
        def result = tensor.add(1.0f)
        float scalar = result.getFloat([0L, 0L] as long[])
        if (manager.getEngine().getEngineName() != 'PyTorch' || result.getDevice() != Device.cpu() ||
            !Float.isFinite(scalar) || scalar != 2.0f) reject('CPU tensor check failed')
        def engineType = manager.getEngine().getClass()
        if (engineType.name != 'ai.djl.pytorch.engine.PtEngine') reject('Wrong loaded engine class')
        def engineLocation = engineType.getProtectionDomain().getCodeSource()?.getLocation()
        if (engineLocation == null || engineLocation.protocol != 'file') reject('Engine has no local code source')
        def engineSource = Path.of(engineLocation.toURI())
        [engine_name: manager.getEngine().getEngineName(), native_loaded: true, tensor_scalar: (double) scalar,
         code_source: engineSource.toString(), code_source_sha256: hashFile(engineSource)]
    }
}
if (request.require_warpy) {
    evidence.warpy = extension('qupath.ext.warpy.WarpyExtension')
}
byte[] encoded = new GsonBuilder().serializeNulls().disableHtmlEscaping().create().toJson(evidence).getBytes('UTF-8')
if (encoded.length > 65536) reject('Evidence exceeds limit')
Files.write(evidencePath, encoded, StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE)
