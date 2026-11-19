"""Build the native extension using an installed QuPath and pinned dependencies.

Build outputs and downloaded libraries belong to the caller's external build
directory. The extension JAR includes the pinned Java reader and its native
codecs; QuPath discovers the image builder through its service registry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import zipfile
from urllib.request import urlopen


READER_VERSION = "0.3.1"
READER_SHA256 = "fe0b217e62477439ce018d978d4d2d1c54026b0d89e20fdb0c16c2ff6fe679a7"


PARQUET_SHA256 = "957e18ceaf78fa71f05082f9908e8d770c097657823902a2bafdd2e24c3cf28d"
HADOOP_API_SHA256 = "a964d4daa054c9615bbafb4553efbb140fa7fb9ac6f358a24393f183a5703438"

MAVEN_DEPENDENCIES = {
    "reader": ("dev/zarr/zarr-java/0.3.1/zarr-java-0.3.1-shaded.jar", READER_SHA256),
    "parquet": ("org/apache/parquet/parquet-hadoop-bundle/1.16.0/parquet-hadoop-bundle-1.16.0.jar", PARQUET_SHA256),
    "hadoop_api": ("org/apache/hadoop/hadoop-client-api/3.4.1/hadoop-client-api-3.4.1.jar", HADOOP_API_SHA256),
    "ecj": ("org/eclipse/jdt/ecj/3.43.0/ecj-3.43.0.jar", "c786468c65e906498e7e36ece4e0d04c6d3dd34c9a61b34a3a5b512801911a82"),
}


def download_dependency(url: str, destination: Path, expected: str) -> Path:
    """Reuse a pinned local JAR or download and verify it before publishing."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        with destination.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != expected:
            raise ValueError(f"Dependency checksum differs: {destination}. Remove or replace this file before retrying.")
        return destination
    pending = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=destination.name + ".", suffix=".tmp", delete=False) as output:
            pending = Path(output.name)
            digest = hashlib.sha256()
            with urlopen(url, timeout=60) as response:
                for block in iter(lambda: response.read(1024 * 1024), b""):
                    output.write(block)
                    digest.update(block)
        if digest.hexdigest() != expected:
            raise ValueError(f"Downloaded dependency checksum differs: {destination.name}")
        os.replace(pending, destination)
        return destination
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def qupath_libraries(distribution: Path) -> list[Path]:
    for folder in (distribution / "lib" / "app", distribution / "app"):
        jars = sorted(folder.glob("*.jar"))
        if jars:
            return jars
    raise ValueError(f"No QuPath libraries found in {distribution}. Select the installation folder containing lib/app or app.")


def build(qupath: Path, reader: Path, destination: Path, compiler: Path | None,
        parquet: Path, hadoop_api: Path) -> Path:
    """Build the image, cell and model services used by installed QuPath.

    We verify the supplied dependencies, compile our Java sources against the
    selected QuPath distribution, and publish one completed extension JAR.
    Dependencies retain their licence resources. Our service registrations take
    precedence so QuPath discovers the image reader and cell overlay together.

    Args:
        qupath: QuPath distribution root containing ``lib/app`` (Linux) or ``app`` (Windows).
        reader: Pinned zarr-java 0.3.1 shaded dependency, supplied locally or downloaded.
        destination: External build directory for the compiled JAR and records.
        compiler: Optional ECJ JAR for a host JDK older than QuPath's classfiles.
        parquet: Pinned shaded Parquet dependency for local cell tables.
        hadoop_api: Pinned API classes required by the Parquet dependency.

    Returns:
        Compiled extension JAR for application packaging. Set
        MIF_NATIVE_READER_BUILD_DIR to its output directory before installing
        from source; Software setup installs the packaged JAR into the
        application-managed QuPath extension folder.
    """
    # i) Establish the dependency identity before compiling any source.
    if hashlib.sha256(reader.read_bytes()).hexdigest() != READER_SHA256:
        raise ValueError("The Java Zarr reader differs from the pinned build dependency")
    for dependency, expected in ((parquet, PARQUET_SHA256), (hadoop_api, HADOOP_API_SHA256)):
        if hashlib.sha256(dependency.read_bytes()).hexdigest() != expected:
            raise ValueError("The local cell reader differs from the pinned build dependency")
    dependencies = [reader, parquet, hadoop_api]
    root = Path(__file__).resolve().parents[1]
    destination.mkdir(parents=True, exist_ok=True)
    jars = qupath_libraries(qupath)
    classpath = os.pathsep.join(str(path) for path in [*jars, *dependencies])
    # ii) Compile into a private external workspace, keeping sources unmodified.
    with tempfile.TemporaryDirectory(prefix="mif-native-build-", dir=destination) as workspace:
        classes = Path(workspace) / "classes"
        sources = sorted((root / "src" / "java").rglob("*.java"))
        if compiler is None:
            command = ["javac", "--release", "21", "-proc:none"]
        else:
            command = ["java", "-jar", str(compiler), "-21", "-proc:none"]
        subprocess.run([*command, "-classpath", classpath, "-d", str(classes), *map(str, sources)], check=True)
        output = destination / "qupath-extension-mif-0.1.0.jar"
        resources = root / "src" / "java-resources"
        own_names = {path.relative_to(resources).as_posix() for path in resources.rglob("*") if path.is_file()}
        # iii) Publish the completed archive and record its installation identity.
        pending_output = output.with_suffix(".jar.tmp")
        with zipfile.ZipFile(pending_output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            written = set()
            for dependency_key, dependency_path in zip(("reader", "parquet", "hadoop_api"), dependencies):
                artifact = Path(MAVEN_DEPENDENCIES[dependency_key][0]).stem
                with zipfile.ZipFile(dependency_path) as dependency:
                    for item in dependency.infolist():
                        name = item.filename
                        # Shaded dependencies share legal-resource paths. Retain
                        # every original payload before the first-wins merge,
                        # including notices nested inside dependency packages.
                        if name.upper().endswith(("LICENSE", "LICENSE.TXT", "LICENSE.MD",
                                                  "LICENCE", "LICENCE.TXT", "LICENCE.MD",
                                                  "NOTICE", "NOTICE.TXT", "NOTICE.MD")):
                            notice_name = f"META-INF/licenses/{artifact}/{name}"
                            archive.writestr(notice_name, dependency.read(item))
                            written.add(notice_name)
                        if name.endswith("/") or name in own_names or name in written or name.upper().endswith((".SF", ".RSA", ".DSA")):
                            continue
                        if name == "META-INF/MANIFEST.MF":
                            continue
                        archive.writestr(name, dependency.read(item)); written.add(name)
            for directory in (classes, resources):
                for path in sorted(directory.rglob("*")):
                    if path.is_file():
                        archive.write(path, path.relative_to(directory).as_posix())
            archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\nImplementation-Title: mIF processed image reader\nImplementation-Version: 0.1.0\n\n")
        os.replace(pending_output, output)
        with output.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        (destination / "qupath-extension-mif-0.1.0.sha256").write_text(digest + "\n")
        (destination / "reader.json").write_text(json.dumps({
            "name": output.name, "version": "0.1.0",
            "url": "package://mif_pipeline.qupath_runtime.assets/native/" + output.name,
            "size_bytes": output.stat().st_size,
            "sha256": digest,
        }, sort_keys=True) + "\n")
        return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qupath", required=True, type=Path)
    parser.add_argument("--reader", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ecj", type=Path)
    parser.add_argument("--parquet", type=Path)
    parser.add_argument("--hadoop-api", type=Path)
    parser.add_argument("--download-dependencies", type=Path, metavar="DIRECTORY",
                        help="Download missing pinned Maven JARs into this directory; explicit local paths take precedence")
    parser.add_argument("--use-ecj", action="store_true",
                        help="Use the pinned Eclipse compiler; downloads it with --download-dependencies unless --ecj is supplied")
    options = parser.parse_args()
    required = ("reader", "parquet", "hadoop_api") + (("ecj",) if options.use_ecj else ())
    missing = [name for name in required if getattr(options, name) is None]
    if missing and options.download_dependencies is None:
        parser.error("Supply --download-dependencies DIRECTORY or local " + ", ".join("--" + name.replace("_", "-") for name in missing))
    for name in missing:
        relative, expected = MAVEN_DEPENDENCIES[name]
        destination = options.download_dependencies / relative.rsplit("/", 1)[-1]
        print(f"Preparing {destination.name}", flush=True)
        setattr(options, name, download_dependency("https://repo.maven.apache.org/maven2/" + relative, destination, expected))
    print(build(options.qupath, options.reader, options.output, options.ecj, options.parquet, options.hadoop_api))
