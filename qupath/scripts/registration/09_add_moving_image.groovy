/** Add one exact moving-modality entry to the prepared QuPath project. */

import java.awt.image.BufferedImage
import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.Paths
import java.nio.file.StandardCopyOption
import java.util.UUID
import qupath.lib.images.servers.ImageServers
import qupath.lib.images.servers.ImageServerBuilder
import qupath.lib.io.GsonTools
import org.mif.image.ManagedImageTypes

import static qupath.lib.scripting.QP.*

if (args.size() != 2)
    throw new IllegalArgumentException("Expected moving-image request and evidence paths")
Path requestPath = Paths.get(args[0] as String).toAbsolutePath().normalize()
Path evidencePath = Paths.get(args[1] as String).toAbsolutePath().normalize()
def gson = GsonTools.getInstance()
def prettyGson = gson.newBuilder().serializeNulls().setPrettyPrinting().create()
Map request = gson.fromJson(Files.readString(requestPath), Map.class) as Map
Set expected = [
    "schema_version", "project_file", "image_path", "image_id",
    "image_sha256", "entry_name", "hierarchy_role", "moving_modality",
    "source_metadata"
] as Set
if (request.keySet() != expected ||
        request.schema_version != "qupath-moving-image-request/v1" ||
        request.hierarchy_role != "reference_only" ||
        !(request.moving_modality instanceof String) ||
        !(request.source_metadata instanceof Map))
    throw new IllegalArgumentException("Invalid moving-image request")

def project = getProject()
if (project == null)
    throw new IllegalStateException("No prepared QuPath project is open")
Path actualProject = project.getPath().toAbsolutePath().normalize()
Path requestedProject = Paths.get(request.project_file as String).toAbsolutePath().normalize()
if (actualProject != requestedProject)
    throw new IllegalArgumentException("Moving-image request targets another QuPath project")
Path imagePath = Paths.get(request.image_path as String).toAbsolutePath().normalize()
if (!Files.isRegularFile(imagePath))
    throw new IllegalArgumentException("Moving image is not a local file")
def imageUri = imagePath.toUri().normalize()
Map selected = request.source_metadata.source_series_selection as Map
if (selected != null && (!(selected.source_series_index instanceof Number) ||
        selected.source_series_index.intValue() < 0 ||
        selected.source_series_index.doubleValue() != selected.source_series_index.intValue() ||
        selected.image_id != request.image_id || selected.source_file_sha256 != request.image_sha256))
    throw new IllegalArgumentException("Invalid inspected moving-image selection")
// Select by the native reader's explicit series argument, never by builder order.
def support = selected == null ? ImageServers.getImageSupport(imageUri) :
    ImageServers.getImageSupport(imageUri, '--series', selected.source_series_index.intValue().toString())
if (support == null || support.getBuilders().size() != 1)
    throw new IllegalStateException("The selected moving image must resolve to exactly one QuPath server builder")
def selectedBuilder = support.getBuilders().get(0)
def selectedServer = selectedBuilder.build()
try {
    if (selected != null && (selectedServer.getWidth() != selected.size_x ||
            selectedServer.getHeight() != selected.size_y || selectedServer.nChannels() != selected.size_c ||
            selectedServer.nZSlices() != 1 || selectedServer.nTimepoints() != 1))
        throw new IllegalStateException("The selected QuPath image differs from the inspected image dimensions")
} finally {
    selectedServer.close()
}
// Saved builders may carry researcher-edited names and display metadata. Match
// only the exact source-opening recipe, retaining those existing image settings.
def sourceRecipe = { builder ->
    Map record = gson.fromJson(gson.toJson(builder, ImageServerBuilder.ServerBuilder.class), Map.class)
    if (record.builderType != 'uri' || !(record.uri instanceof String) ||
            !(["builderType", "providerClassName", "uri", "args", "metadata"] as Set).containsAll(record.keySet()))
        throw new IllegalStateException("Cannot verify the selected moving-image builder")
    return [builderType:record.builderType, providerClassName:record.providerClassName,
            uri:Paths.get(java.net.URI.create(record.uri)).toAbsolutePath().normalize().toString(), args:record.args]
}
def selectedRecipe = sourceRecipe(selectedBuilder)
def matches = project.getImageList().findAll { candidate ->
    candidate.getURIs().any { uri -> uri.normalize() == imageUri } &&
        sourceRecipe(candidate.getServerBuilder()) == selectedRecipe
}
if (matches.size() > 1)
    throw new IllegalStateException("Moving image resolves to multiple matching project entries")
def entry = matches ? matches[0] : null
if (entry == null) {
    entry = project.addImage(selectedBuilder)
    if (entry == null)
        throw new IllegalStateException("QuPath could not add moving image")
}
entry.setImageName(request.entry_name as String)
def imageData = entry.readImageData()
if (selected != null && (imageData.getServer().getWidth() != selected.size_x ||
        imageData.getServer().getHeight() != selected.size_y ||
        imageData.getServer().nChannels() != selected.size_c ||
        imageData.getServer().nZSlices() != 1 || imageData.getServer().nTimepoints() != 1))
    throw new IllegalStateException("The saved moving-image entry differs from the inspected image dimensions")
// We persist the reviewed paired-image type only when none was selected.
// Existing stain/type choices remain with the image during this normal save.
ManagedImageTypes.initializeIfUnset(imageData, "moving_reference", request.moving_modality as String)
def calibration = imageData.getServer().getPixelCalibration()
double pixelWidth = calibration.getPixelWidthMicrons()
double pixelHeight = calibration.getPixelHeightMicrons()
if (!Double.isFinite(pixelWidth) || !Double.isFinite(pixelHeight) ||
        pixelWidth <= 0d || pixelHeight <= 0d)
    throw new IllegalStateException("Moving project entry requires physical X/Y calibration")
entry.saveImageData(imageData)
project.syncChanges()

Map evidence = [
    schema_version: "qupath-moving-image/v1",
    project_file: requestedProject.toString(),
    project_entry_id: entry.getID(),
    project_entry_path: entry.getEntryPath().toAbsolutePath().normalize().toString(),
    image_id: request.image_id,
    image_sha256: request.image_sha256,
    image_path: imagePath.toString(),
    entry_name: request.entry_name,
    hierarchy_role: "reference_only",
    moving_modality: request.moving_modality,
    source_metadata: request.source_metadata,
    physical_calibration: [
        x_um_per_px: pixelWidth,
        y_um_per_px: pixelHeight,
    ],
]
Files.createDirectories(evidencePath.parent)
Path temporary = evidencePath.resolveSibling(
    ".${evidencePath.fileName}.${UUID.randomUUID()}.tmp"
)
Files.writeString(
    temporary,
    prettyGson.toJson(evidence) + "\n",
    StandardCharsets.UTF_8,
)
try {
    Files.move(temporary, evidencePath, StandardCopyOption.ATOMIC_MOVE,
        StandardCopyOption.REPLACE_EXISTING)
} catch (java.nio.file.AtomicMoveNotSupportedException ignored) {
    Files.move(temporary, evidencePath, StandardCopyOption.REPLACE_EXISTING)
}
println "Prepared moving project entry ${entry.getID()}"
