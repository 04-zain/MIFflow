/**
 * Return the fail-closed immutable-bundle verifier used by current-image stages.
 *
 * This file is itself a registered member of the digest-addressed bundle.  A
 * stage authenticates the manifest against the request-bound bundle SHA, then
 * authenticates these bytes from that manifest before evaluating this closure.
 * The contract binds the registered stage
 * asset; QuPath does not expose a trustworthy digest for an executing editor
 * buffer, so this deliberately makes no such claim.
 */

import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.Paths
import java.security.MessageDigest

return { Map request, Path boundProjectPath, String expectedAssetPath, def gson ->
    def fail = { String message ->
        throw new IllegalStateException("Immutable QuPath script bundle ${message}")
    }
    def sha256File = { Path path ->
        MessageDigest digest = MessageDigest.getInstance("SHA-256")
        def stream = Files.newInputStream(path)
        try {
            byte[] buffer = new byte[1024 * 1024]
            int count
            while ((count = stream.read(buffer)) >= 0) {
                if (count > 0) digest.update(buffer, 0, count)
            }
        } finally {
            stream.close()
        }
        digest.digest().encodeHex().toString()
    }
    def regularNonLink = { Path path ->
        !Files.isSymbolicLink(path) && Files.isRegularFile(path)
    }
    def directoryNonLink = { Path path ->
        !Files.isSymbolicLink(path) && Files.isDirectory(path)
    }
    def lowercaseSha256 = { value ->
        value instanceof String && (value as String) ==~ /[0-9a-f]{64}/
    }

    String bundleSha256 = request.script_bundle_sha256 as String
    String requestAssetPath = request.script_asset_path as String
    String requestAssetSha256 = request.script_asset_sha256 as String
    if (!lowercaseSha256(bundleSha256) ||
            requestAssetPath != expectedAssetPath ||
            requestAssetPath.contains("\\") || requestAssetPath.startsWith("/") ||
            requestAssetPath.split("/").any { it in ["", ".", ".."] } ||
            !lowercaseSha256(requestAssetSha256))
        fail("request identity is invalid")

    Path projectRoot = boundProjectPath.parent.toAbsolutePath().normalize()
    Path scriptsRoot = projectRoot.resolve("scripts").normalize()
    Path bundleRoot = scriptsRoot.resolve("bundle-${bundleSha256}").normalize()
    Path manifestPath = bundleRoot.resolve("qupath-script-bundle.v1.json").normalize()
    Path dataRoot = projectRoot.resolve("data").normalize()
    Path evidencePath = dataRoot
        .resolve("qupath-script-bundle.evidence.json").normalize()
    if (bundleRoot.parent != scriptsRoot ||
            dataRoot.parent != projectRoot || evidencePath.parent != dataRoot ||
            projectRoot.toRealPath() != projectRoot ||
            scriptsRoot.toRealPath() != scriptsRoot ||
            bundleRoot.toRealPath() != bundleRoot ||
            dataRoot.toRealPath() != dataRoot ||
            evidencePath.toRealPath() != evidencePath ||
            Files.isSymbolicLink(scriptsRoot) || Files.isSymbolicLink(bundleRoot) ||
            Files.isSymbolicLink(dataRoot) || Files.isSymbolicLink(evidencePath) ||
            !directoryNonLink(scriptsRoot) || !directoryNonLink(bundleRoot) ||
            !directoryNonLink(dataRoot) ||
            !regularNonLink(evidencePath) || !regularNonLink(manifestPath))
        fail("paths are missing or traverse symbolic links")

    if (sha256File(manifestPath) != bundleSha256)
        fail("manifest digest does not match the requested bundle")
    Map manifest = gson.fromJson(Files.readString(manifestPath), Map.class) as Map
    if (manifest == null || manifest.keySet() != ["schema_version", "assets"] as Set ||
            manifest.schema_version != "qupath-script-bundle/v1" ||
            !(manifest.assets instanceof List) || (manifest.assets as List).isEmpty())
        fail("manifest shape is invalid")

    Map evidence = gson.fromJson(Files.readString(evidencePath), Map.class) as Map
    Set evidenceFields = [
        "schema_version", "project_file", "bundle_sha256", "bundle_path",
        "manifest_path", "manifest_sha256", "assets"
    ] as Set
    if (evidence == null || evidence.keySet() != evidenceFields ||
            evidence.schema_version != "qupath-script-bundle-evidence/v1" ||
            evidence.project_file != boundProjectPath.toString() ||
            evidence.bundle_sha256 != bundleSha256 ||
            evidence.bundle_path != bundleRoot.toString() ||
            evidence.manifest_path != manifestPath.toString() ||
            evidence.manifest_sha256 != bundleSha256 ||
            !(evidence.assets instanceof List))
        fail("project evidence is missing, mixed, or stale")
    if (manifest.assets != evidence.assets)
        fail("manifest does not match the project evidence")

    Map<String, String> registeredAssets = [:]
    Set<String> expectedFiles = ["qupath-script-bundle.v1.json"] as Set
    Set<String> expectedDirectories = [] as Set
    (manifest.assets as List).each { raw ->
        if (!(raw instanceof Map) || raw.keySet() != ["path", "sha256"] as Set)
            fail("manifest asset shape is invalid")
        String relative = raw.path as String
        String digest = raw.sha256 as String
        if (!relative || relative.contains("\\") || relative.startsWith("/") ||
                relative.split("/").any { it in ["", ".", ".."] } ||
                !lowercaseSha256(digest) || registeredAssets.containsKey(relative))
            fail("manifest asset identity is invalid")
        Path assetPath = bundleRoot.resolve(relative).normalize()
        if (!assetPath.startsWith(bundleRoot) || !regularNonLink(assetPath) ||
                sha256File(assetPath) != digest)
            fail("asset digest mismatch: ${relative}")
        registeredAssets[relative] = digest
        expectedFiles.add(relative)
        List<String> parts = relative.split("/") as List<String>
        if (parts.size() > 1) {
            String parent = ""
            parts.take(parts.size() - 1).each { part ->
                parent = parent ? "${parent}/${part}" : part
                expectedDirectories.add(parent)
            }
        }
    }

    Set<String> actualFiles = [] as Set
    Set<String> actualDirectories = [] as Set
    def paths = Files.walk(bundleRoot)
    try {
        paths.forEach { Path path ->
            if (path == bundleRoot) return
            String relative = bundleRoot.relativize(path).toString().replace("\\", "/")
            if (Files.isSymbolicLink(path))
                fail("contains symbolic link: ${relative}")
            if (Files.isDirectory(path)) actualDirectories.add(relative)
            else if (Files.isRegularFile(path)) actualFiles.add(relative)
            else fail("contains unsupported entry: ${relative}")
        }
    } finally {
        paths.close()
    }
    if (actualFiles != expectedFiles || actualDirectories != expectedDirectories)
        fail("contains mixed, missing, or extra entries")
    if (registeredAssets[expectedAssetPath] != requestAssetSha256)
        fail("registered current asset does not match the request")
    true
}
