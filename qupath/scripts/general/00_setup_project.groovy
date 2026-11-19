/** Create/open the real QuPath project and add the canonical mIF image. */

import java.awt.image.BufferedImage
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.Paths
import java.nio.file.StandardCopyOption
import java.nio.charset.StandardCharsets
import java.util.UUID
import qupath.lib.images.ImageData
import qupath.lib.images.servers.ImageServers
import qupath.lib.io.GsonTools
import qupath.lib.projects.ProjectIO
import qupath.lib.projects.Projects

if (args.size() != 4)
    throw new IllegalArgumentException(
        "Expected analysis-config.json, project.qpproj, canonical evidence path, and image SHA-256"
    )
Path configPath = Paths.get(args[0] as String).toAbsolutePath().normalize()
Path projectPath = Paths.get(args[1] as String).toAbsolutePath().normalize()
Path evidencePath = Paths.get(args[2] as String).toAbsolutePath().normalize()
String canonicalImageSha256 = args[3] as String
if (!(canonicalImageSha256 ==~ /[0-9a-f]{64}/))
    throw new IllegalArgumentException("Canonical image SHA-256 is invalid")
Map config = GsonTools.getInstance().fromJson(
    Files.readString(configPath), Map.class
) as Map
if (config.schema_version != "6")
    throw new IllegalArgumentException("ROI analysis requires schema_version=6")

Files.createDirectories(projectPath.parent)
def project = Files.isRegularFile(projectPath)
    ? ProjectIO.loadProject(projectPath.toFile(), BufferedImage.class)
    : Projects.createProject(projectPath.toFile(), BufferedImage.class)
def canonicalUri = Paths.get(config.input_image as String)
    .toAbsolutePath().normalize().toUri().normalize()
def canonicalMatches = project.imageList.findAll { item ->
    item.getURIs().any { uri -> uri.normalize() == canonicalUri }
}
if (canonicalMatches.size() > 1)
    throw new IllegalStateException("Canonical image resolves to multiple project entries")
def entry = canonicalMatches ? canonicalMatches[0] : null
if (entry == null) {
    def support = ImageServers.getImageSupport(canonicalUri)
    if (support == null || support.getBuilders().isEmpty())
        throw new IllegalStateException("No QuPath ImageServerBuilder supports ${canonicalUri}")
    if (support.getBuilders().size() != 1)
        throw new IllegalStateException(
            "Canonical image must resolve to exactly one QuPath server builder; got ${support.getBuilders().size()}"
        )
    entry = project.addImage(support.getBuilders().get(0))
    if (entry == null)
        throw new IllegalStateException("QuPath could not add canonical image ${canonicalUri}")
}
entry.setImageName(config.slide_id as String)
def imageData = entry.readImageData()
imageData.setImageType(ImageData.ImageType.FLUORESCENCE)
Map<String, String> requiredEntryMetadata = [
    "mif_pipeline.image_role": "primary_mif",
    "mif_pipeline.source_image_sha256": canonicalImageSha256,
]
requiredEntryMetadata.each { String key, String expected ->
    Object observed = entry.getMetadataMap().get(key)
    if (observed != null && observed != expected)
        throw new IllegalStateException("Canonical entry metadata conflicts for ${key}")
    entry.putMetadataValue(key, expected)
}
entry.saveImageData(imageData)
project.syncChanges()
def calibration = imageData.getServer().getPixelCalibration()
double x = calibration.getPixelWidthMicrons()
double y = calibration.getPixelHeightMicrons()
if (!Double.isFinite(x) || !Double.isFinite(y) || x <= 0d || y <= 0d)
    throw new IllegalStateException("Canonical project entry requires physical calibration")
double[] preferredDownsamples = imageData.getServer().getPreferredDownsamples()
if (preferredDownsamples == null || preferredDownsamples.length == 0)
    throw new IllegalStateException("Canonical project entry exposes no native resolutions")
List<Map> nativePyramidLevels = []
double previousDownsample = 0d
preferredDownsamples.eachWithIndex { rawDownsample, index ->
    double downsample = rawDownsample as double
    if (!Double.isFinite(downsample) || downsample <= 0d || downsample <= previousDownsample)
        throw new IllegalStateException("Canonical project native downsamples are invalid")
    if (index == 0 && Math.abs(downsample - 1d) > 1e-9)
        throw new IllegalStateException("Canonical project native level 0 must have downsample 1")
    nativePyramidLevels << [
        native_resolution_index: index,
        x_downsample: downsample,
        y_downsample: downsample,
        x_um_per_px: x * downsample,
        y_um_per_px: y * downsample,
    ]
    previousDownsample = downsample
}
Map evidence = [
    schema_version: "qupath-canonical-image/v3",
    project_file: project.getPath().toAbsolutePath().normalize().toString(),
    project_entry_id: entry.getID().toString(),
    project_entry_path: entry.getEntryPath().toAbsolutePath().normalize().toString(),
    canonical_image_sha256: canonicalImageSha256,
    image_path: Paths.get(config.input_image as String).toAbsolutePath().normalize().toString(),
    entry_name: entry.getImageName(),
    physical_calibration: [x_um_per_px: x, y_um_per_px: y],
    native_pyramid_levels: nativePyramidLevels,
]
def prettyGson = GsonTools.getInstance().newBuilder().setPrettyPrinting().create()
Files.createDirectories(evidencePath.parent)
Path temporary = evidencePath.resolveSibling(".${evidencePath.fileName}.${UUID.randomUUID()}.tmp")
Files.writeString(temporary, prettyGson.toJson(evidence) + "\n", StandardCharsets.UTF_8)
try {
    Files.move(temporary, evidencePath, StandardCopyOption.ATOMIC_MOVE,
        StandardCopyOption.REPLACE_EXISTING)
} catch (java.nio.file.AtomicMoveNotSupportedException ignored) {
    Files.move(temporary, evidencePath, StandardCopyOption.REPLACE_EXISTING)
}
println "Prepared canonical fluorescence image ${config.slide_id} in ${project.getPath()}"
