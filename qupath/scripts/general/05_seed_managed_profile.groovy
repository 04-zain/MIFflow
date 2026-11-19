// Seed only an isolated generation's user path. No project, image or scientific work.
import com.google.gson.GsonBuilder
import com.google.gson.stream.JsonReader
import com.google.gson.stream.JsonToken
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.Path
import java.nio.file.StandardOpenOption
import qupath.lib.common.GeneralTools
import qupath.lib.gui.prefs.PathPrefs

if (args == null || args.length != 2)
    throw new IllegalArgumentException('Expected two seed arguments')
def requestPath = Path.of(args[0])
def evidencePath = Path.of(args[1])
if (![requestPath, evidencePath].every { it.isAbsolute() && it == it.normalize() })
    throw new IllegalArgumentException('Seed paths must be absolute and normalized')
if (!Files.isRegularFile(requestPath, LinkOption.NOFOLLOW_LINKS) || Files.size(requestPath) > 65536)
    throw new IllegalArgumentException('Invalid or oversized seed request')
def fields = ['schema', 'challenge', 'qupath_version', 'preferences_namespace', 'profile_root'].toSet()
def request = [:]
Files.newBufferedReader(requestPath).withCloseable { input ->
    def reader = new JsonReader(input)
    reader.beginObject()
    while (reader.hasNext()) {
        def key = reader.nextName()
        if (request.containsKey(key) || !fields.contains(key) || reader.peek() != JsonToken.STRING)
            throw new IllegalArgumentException('Invalid seed request field')
        request[key] = reader.nextString()
    }
    reader.endObject()
    if (reader.peek() != JsonToken.END_DOCUMENT)
        throw new IllegalArgumentException('Trailing seed request data')
}
if (request.keySet() != fields || request.schema != 'qupath-profile-seed/v1' ||
    !(request.challenge ==~ /[0-9a-f]{64}/) || request.qupath_version != '0.7.0' ||
    !(request.preferences_namespace ==~ /\/mif-warpy-[0-9a-f]{32}/))
    throw new IllegalArgumentException('Invalid seed request contract')
def profileRoot = Path.of(request.profile_root)
if (!profileRoot.isAbsolute() || profileRoot != profileRoot.normalize() ||
    profileRoot.toString() != request.profile_root ||
    profileRoot.fileName.toString() != request.preferences_namespace.substring('/mif-warpy-'.length()) ||
    !Files.isDirectory(profileRoot, LinkOption.NOFOLLOW_LINKS))
    throw new IllegalArgumentException('Managed profile directory is unavailable')
if (GeneralTools.getVersion() != request.qupath_version ||
    PathPrefs.getUserPreferences().absolutePath() != request.preferences_namespace)
    throw new IllegalArgumentException('Native QuPath version or namespace differs')
def property = PathPrefs.userPathProperty()
String previous = property.get()
if (previous != null && !previous.isBlank() && previous != request.profile_root)
    throw new IllegalArgumentException('Managed namespace already names another user path')
property.set(request.profile_root)
if (!PathPrefs.savePreferences() || property.get() != request.profile_root ||
    PathPrefs.getUserPreferences().absolutePath() != request.preferences_namespace)
    throw new IllegalStateException('Managed preferences could not be saved exactly')
def evidence = request + [previous_user_path: previous, saved: true]
byte[] encoded = new GsonBuilder().serializeNulls().disableHtmlEscaping().create().toJson(evidence).getBytes('UTF-8')
if (encoded.length > 65536) throw new IllegalArgumentException('Seed response exceeds limit')
Files.write(evidencePath, encoded, StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE)
