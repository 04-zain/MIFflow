// Candidate discovery only: no project, image, model, engine execution, or network access.
import com.google.gson.GsonBuilder
import com.google.gson.stream.JsonReader
import com.google.gson.stream.JsonToken
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.LinkOption
import java.nio.file.StandardOpenOption
import java.nio.file.attribute.BasicFileAttributes
import java.security.MessageDigest

if (args == null || args.length != 2) throw new IllegalArgumentException('Expected two probe arguments')
def requestPath = Path.of(args[0])
def evidencePath = Path.of(args[1])
if (![requestPath, evidencePath].every { it.isAbsolute() && it == it.normalize() })
    throw new IllegalArgumentException('Probe paths must be absolute and normalized')
if (Files.size(requestPath) > 65536) throw new IllegalArgumentException('Request exceeds limit')
def request = [:]
Files.newBufferedReader(requestPath).withCloseable { input ->
    def reader = new JsonReader(input)
    reader.beginObject()
    while (reader.hasNext()) {
        def key = reader.nextName()
        if (request.containsKey(key) || !(key in ['schema', 'challenge']) || reader.peek() != JsonToken.STRING)
            throw new IllegalArgumentException('Invalid request field')
        request[key] = reader.nextString()
    }
    reader.endObject()
    if (reader.peek() != JsonToken.END_DOCUMENT) throw new IllegalArgumentException('Trailing request data')
}
if (request.keySet() != ['schema', 'challenge'].toSet() ||
    request.schema != 'qupath-class-origin-candidate/v3' || !(request.challenge ==~ /[0-9a-f]{64}/))
    throw new IllegalArgumentException('Invalid request contract')

def hashSource = { Path path ->
    def before = Files.readAttributes(path, BasicFileAttributes, LinkOption.NOFOLLOW_LINKS)
    if (!before.isRegularFile()) throw new IllegalArgumentException('Code source is not a regular file')
    def digest = MessageDigest.getInstance('SHA-256')
    Files.newInputStream(path, StandardOpenOption.READ, LinkOption.NOFOLLOW_LINKS).withCloseable { input ->
        byte[] buffer = new byte[1024 * 1024]
        int count
        while ((count = input.read(buffer)) != -1) digest.update(buffer, 0, count)
    }
    def after = Files.readAttributes(path, BasicFileAttributes, LinkOption.NOFOLLOW_LINKS)
    if (before.fileKey() != after.fileKey() || before.size() != after.size() ||
        before.lastModifiedTime() != after.lastModifiedTime())
        throw new IllegalArgumentException('Code source changed during hashing')
    HexFormat.of().formatHex(digest.digest())
}
def observe = { String name, boolean requireVersion ->
    def empty = [status: 'missing', class_name: name, loaded_version: null,
                 code_source: null, code_source_sha256: null]
    try {
        def type = Class.forName(name, false, this.class.classLoader)
        def location = type.getProtectionDomain().getCodeSource()?.getLocation()
        if (location == null || location.protocol != 'file')
            throw new IllegalArgumentException('No local code source')
        def version = requireVersion ? type.getPackage().getImplementationVersion() : null
        if (requireVersion && !version) throw new IllegalArgumentException('No implementation version')
        def source = Path.of(location.toURI())
        [status: 'observed', class_name: name, loaded_version: version?.toString(),
         code_source: source.toString(), code_source_sha256: hashSource(source)]
    } catch (ClassNotFoundException | NoClassDefFoundError error) {
        empty
    } catch (IllegalArgumentException | LinkageError | IOException error) {
        empty + [status: 'incompatible']
    }
}
// QuPath 0.7 ScriptCommand initialized this static manager before running us.
// Read local inventories only; Catalog.getExtensions() would download catalogs.
def observeExtensions = {
    try {
        def directory = qupath.lib.gui.UserDirectoryManager.getInstance().getExtensionsPath()
        if (directory == null) return [status: 'unconfigured', directory: null, jars: []]
        def manager = qupath.lib.gui.QuPathGUI.getExtensionCatalogManager()
        def snapshot = {
            def manual = new ArrayList(manager.getManuallyInstalledJars())
            def catalog = new ArrayList(manager.getCatalogManagedInstalledJars())
            if (manual.size() + catalog.size() > 1024)
                throw new IllegalArgumentException('Extension inventory exceeds limit')
            (manual + catalog).collect { it.toString() }.unique().sort()
        }
        def jars = snapshot()
        if (directory != qupath.lib.gui.UserDirectoryManager.getInstance().getExtensionsPath() || jars != snapshot())
            throw new IllegalArgumentException('Extension inventory changed')
        [status: 'observed', directory: directory.toString(), jars: jars]
    } catch (Exception error) {
        [status: 'error', directory: null, jars: []]
    }
}
def evidence = [schema: request.schema, challenge: request.challenge,
                qupath_version: qupath.lib.common.GeneralTools.getVersion(),
                preferences_namespace: qupath.lib.gui.prefs.PathPrefs.getUserPreferences().absolutePath(),
                extensions: observeExtensions(),
                instanseg: observe('qupath.ext.instanseg.ui.InstanSegExtension', true),
                djl: observe('qupath.ext.djl.DjlExtension', true),
                pytorch_engine: observe('ai.djl.pytorch.engine.PtEngine', false),
                warpy: observe('qupath.ext.warpy.WarpyExtension', true)]
byte[] encoded = new GsonBuilder().serializeNulls().disableHtmlEscaping().create().toJson(evidence).getBytes('UTF-8')
if (encoded.length > 65536) throw new IllegalArgumentException('Evidence exceeds limit')
Files.write(evidencePath, encoded, StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE)
