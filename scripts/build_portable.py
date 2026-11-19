#!/usr/bin/env python3
"""Build the Linux or Windows folder application from one captured source tree.

Platform launchers select native Python and local paths. This shared procedure
owns dependency installation, freezing, resource metadata and archive smoke checks.
Build work and logs are retained on failure; release outputs are never overwritten.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile

PLATFORMS = {
    "windows-x86_64": ("windows", "mif-desktop.exe", ".zip"),
    "ubuntu24.04-x86_64": ("linux", "mif-desktop", ".tar.gz"),
}


# ==============================================================================
#                         Source and archive boundaries
# ==============================================================================


def load_helper(name: str, path: Path):
    """Load a selected packaging helper without importing application source."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load build helper: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def archive_name(name: str) -> PurePosixPath:
    """Return a relative archive path, rejecting traversal and alternate separators."""
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or path.as_posix() != name
            or ".." in path.parts or "\\" in name or ":" in name
            or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise ValueError(f"archive entry is not a safe relative path: {name!r}")
    return path


def extract_archive(archive: Path, destination: Path, *, source: bool = False,
                    expected_root: str | None = None) -> Path:
    """Extract only safe paths into a newly claimed directory."""
    destination.mkdir()
    seen = set()

    def check(name: str):
        path = archive_name(name.rstrip("/"))
        key = path.as_posix().casefold() if os.name == "nt" else path.as_posix()
        if key in seen:
            raise ValueError(f"duplicate archive entry: {name}")
        seen.add(key)
        if expected_root is not None and path.parts[0] != expected_root:
            raise ValueError(f"archive entry is outside {expected_root}: {name}")

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as container:
            for member in container.infolist():
                check(member.filename)
                if stat.S_ISLNK(member.external_attr >> 16):
                    raise ValueError(f"ZIP contains a symbolic link: {member.filename}")
            container.extractall(destination)
    else:
        with tarfile.open(archive) as container:
            members = container.getmembers()
            for member in members:
                check(member.name)
                if source and not (member.isfile() or member.isdir()):
                    raise ValueError(f"source archive has a non-ordinary entry: {member.name}")
            # The data filter checks link targets and forbids devices and escaping paths.
            container.extractall(destination, members=members, filter="data")
    if not seen:
        raise ValueError("archive is empty")
    if expected_root is None:
        return destination
    children = tuple(destination.iterdir())
    if len(children) != 1 or children[0].name != expected_root or not children[0].is_dir():
        raise ValueError("archive did not extract exactly one application folder")
    return children[0]


def verify_bundle(bundle: Path):
    """Verify the extracted payload once against its included checksum inventory."""
    expected = {}
    for record in (bundle / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, separator, name = record.partition("  ")
        archive_name(name)
        if not separator or not re.fullmatch("[0-9a-f]{64}", digest) or name in expected:
            raise ValueError(f"invalid payload checksum record: {record}")
        expected[name] = digest
    actual = set()
    for path in bundle.rglob("*"):
        if path.is_symlink() or not path.is_file() or path == bundle / "SHA256SUMS":
            continue
        name = path.relative_to(bundle).as_posix()
        actual.add(name)
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if expected.get(name) != digest:
            raise ValueError(f"extracted payload checksum differs: {name}")
    if not actual or actual != set(expected):
        raise ValueError("payload inventory has missing or unexpected files")
    manifest = json.loads((bundle / "BUILD-MANIFEST.json").read_text(encoding="utf-8"))
    links = [
        {"path": path.relative_to(bundle).as_posix(), "target": os.readlink(path)}
        for path in sorted(bundle.rglob("*")) if path.is_symlink()
    ]
    if links != manifest["symlinks"]:
        raise ValueError("extracted links differ from the release inventory")


# ==============================================================================
#                      Installed application and release data
# ==============================================================================


def segmentation_runtime_probe():
    """Exercise the pinned segmentation imports and a CPU native operator at build time."""
    from importlib import import_module, metadata
    import torch

    for name, expected in (
        ("torch", "2.6.0"), ("torchvision", "0.21.0"),
        ("cellpose", "4.0.9"), ("segment-anything", "1.0"),
    ):
        if metadata.version(name).partition("+")[0] != expected:
            raise RuntimeError(f"installed {name} differs from the pinned segmentation runtime")
    import_module("cellpose.models")
    import_module("segment_anything")
    from torchvision.ops import nms

    boxes = torch.tensor([[0., 0., 4., 4.], [0., 0., 4., 4.]])
    scores = torch.tensor([0.9, 0.8])
    if nms(boxes, scores, 0.5).tolist() != [0]:
        raise RuntimeError("Torchvision's native CPU operator returned an unexpected result")


def installed_probe(source: Path, model: Path, accelerator: str = "cpu"):
    """Check installed imports and selected resources before freezing."""
    import importlib
    import torch
    if bool(torch.version.cuda) != (accelerator == "cuda"):
        raise RuntimeError("installed Torch libraries differ from the selected CPU/NVIDIA edition")
    segmentation_runtime_probe()
    from mif_pipeline.preprocessing.unmixing import load_motif_spectral_library
    from mif_pipeline.qupath_runtime.scripts import registered_script_bundle_sha256
    from mif_pipeline.qupath_runtime.payload_lock import MODEL_NAME, verify_bundled_instanseg_model

    for name in (
        "mif_pipeline", "mif_pipeline.gui.qt.frozen_bootstrap",
        "mif_pipeline.gui.qt.portable_diagnostics", "mif_pipeline.datasets.native_cells",
        "mif_pipeline.orchestration.batch_preprocessing_execution",
        "mif_pipeline.pipeline_core.win32_authority_provisioning" if os.name == "nt"
        else "mif_pipeline.pipeline_core.posix_authority_provisioning",
    ):
        origin = Path(importlib.import_module(name).__file__).resolve()
        if origin.is_relative_to(source.resolve()):
            raise RuntimeError(f"installed import resolved into build source: {origin}")
    if load_motif_spectral_library().sha256 != "b59e06af09eb934d2cecf29c21ebb418fedd0e4ce301a36caf7f11dea5d94d3f":
        raise RuntimeError("installed spectral-library resource digest differs")
    registered_script_bundle_sha256()
    if verify_bundled_instanseg_model(model).status != "verified":
        raise RuntimeError("selected InstanSeg model differs from installed lock")
    for parent, names in ((model, {"model"}), (model / "model", {MODEL_NAME})):
        children = tuple(parent.iterdir())
        if {child.name for child in children} != names or any(
            not stat.S_ISDIR(child.lstat().st_mode) for child in children
        ):
            raise RuntimeError("InstanSeg model directory contains unexpected members")
    print("Installed application imports and selected resources verified")


def native_library_probe(bundle: Path, accelerator: str):
    """Check the relocatable native library record and adapter without loading CUDA."""
    if accelerator != "cuda":
        return
    from mif_pipeline.orchestration.setup.native_gpu_setup import NativeGpuSetupService

    root = bundle / "runtimes/torch-native"
    status = NativeGpuSetupService(root=root, cache=root / "djl-cache").inspect()
    if status.state != "ready":
        raise ValueError(status.native_detail)
    print("Native Torch library locations and adapter verified; GPU execution requires a device check")


def write_metadata(bundle: Path, source: Path, source_sha: str, platform_tag: str, accelerator: str = "cpu"):
    """Verify collected wheel resources and write their release identity and notices."""
    from importlib import metadata
    from PySide6 import __version__ as pyside_version
    from PySide6.QtCore import qVersion

    release = load_helper("_mif_release_metadata", source / "packaging/release_metadata.py")
    collector = load_helper("_mif_resource_inventory", source / "packaging/pyinstaller/resource_inventory.py")
    inventory = collector.verify_portable_resources(bundle / "_internal")
    native_library_probe(bundle, accelerator)
    lock_platform = PLATFORMS[platform_tag][0]
    manifest = release.build_manifest(
        bundle, project_version=metadata.version("mif-pipeline"),
        source_archive_sha256=source_sha, platform_tag=platform_tag,
        python_version=platform.python_version(), pyinstaller_version=metadata.version("pyinstaller"),
        pyside_version=pyside_version, qt_version=qVersion(),
        dependency_lock=source / f"packaging/requirements/prototype-{lock_platform}{'-cuda' if accelerator == 'cuda' else ''}.lock",
        pyinstaller_spec=source / "packaging/pyinstaller/mif_desktop.spec",
        resource_inventory=inventory, resource_subdirectory="_internal",
        external_prerequisites=(
            "separately installed native stable QuPath 0.7.x executable",
            "operator-provided images, projects, and reference data",
            "separately installed supported local Java 21 and locked Bio-Formats runtime",
        ), prototype_limitations=release.PROTOTYPE_LIMITATIONS,
    )
    release.write_release_metadata(bundle, dict(manifest, accelerator=accelerator))


@contextmanager
def phase(name: str):
    """Report a named build operation and its elapsed time after success."""
    start = time.monotonic()
    print(f"[package] {name}...", flush=True)
    yield
    print(f"[package] {name} completed ({time.monotonic() - start:.1f} s)", flush=True)


def smoke_executable(executable: Path, env: dict[str, str]):
    """Run extracted diagnostics, stopping its process tree if the check times out."""
    process = subprocess.Popen(
        [str(executable), "--portable-diagnostics-json"], cwd=executable.parent,
        env=env, start_new_session=os.name != "nt",
    )
    try:
        status = process.wait(timeout=120)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            stopped = subprocess.run(
                [str(Path(os.environ["SystemRoot"]) / "System32/taskkill.exe"),
                 "/PID", str(process.pid), "/T", "/F"],
                timeout=5, check=False,
            )
            if stopped.returncode:
                raise RuntimeError("portable diagnostic timed out; process tree cleanup failed")
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        raise RuntimeError("portable diagnostic timed out after 120 seconds; process tree stopped")
    if status:
        raise RuntimeError(f"portable diagnostic failed with exit code {status}")


def write_archive(bundle: Path, archive: Path):
    """Compress the complete folder into a new ZIP or tar.gz, retaining Linux links."""
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as container:
            for path in sorted(bundle.rglob("*")):
                if path.is_symlink():
                    raise ValueError(f"Windows bundle contains a symbolic link: {path}")
                if path.is_file():
                    container.write(path, f"{bundle.name}/{path.relative_to(bundle).as_posix()}")
    else:
        with archive.open("xb") as output, tarfile.open(fileobj=output, mode="w:gz", compresslevel=6) as container:
            container.add(bundle, arcname=bundle.name)


def publish_archive(staged: Path, output: Path) -> Path:
    """Copy once to a fresh output while calculating its adjacent SHA-256."""
    sidecar = output.with_name(output.name + ".sha256")
    if output.exists() or output.is_symlink() or sidecar.exists() or sidecar.is_symlink():
        raise FileExistsError(f"release output already exists: {output}")
    digest = hashlib.sha256()
    with staged.open("rb") as source, output.open("xb") as destination:
        while block := source.read(1024 * 1024):
            destination.write(block)
            digest.update(block)
        destination.flush()
        os.fsync(destination.fileno())
    with sidecar.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(f"{digest.hexdigest()}  {output.name}\n")
        stream.flush()
        os.fsync(stream.fileno())
    return sidecar


# ==============================================================================
#                            Shared portable build
# ==============================================================================


def build(args):
    """Build selected native inputs and publish only after extracted checks pass.

    A checkout is captured once; the WSL handoff supplies its captured archive.
    Every command uses the fresh environment and leaves diagnostic work on failure.
    """
    if (platform.python_implementation() != "CPython" or sys.version_info[:2] != (3, 12)
            or platform.machine().lower() not in {"x86_64", "amd64"}):
        raise RuntimeError("portable builds require native CPython 3.12 x86-64")
    platform_name, executable_name, suffix = PLATFORMS[args.platform]
    if (os.name == "nt") != (platform_name == "windows"):
        raise RuntimeError("build the application on its target operating system")
    source_input = args.source_root.resolve(strict=True)
    output = args.output_dir.resolve(strict=True)
    model = args.instanseg_model_root.resolve(strict=True)
    native = args.native_reader_build_dir.resolve(strict=True)
    for path in (output, model, native, args.build_parent.resolve(strict=True)):
        if path.is_relative_to(source_input):
            raise ValueError(f"build inputs and outputs must be outside the source checkout: {path}")
    jar = native / "qupath-extension-mif-0.1.0.jar"
    if not stat.S_ISREG(jar.lstat().st_mode):
        raise ValueError("native reader JAR must be a regular file")
    if args.source_archive:
        archive = args.source_archive.resolve(strict=True)
        root = archive.parent
        if not re.fullmatch("[0-9a-f]{64}", args.source_archive_sha256 or ""):
            raise ValueError("source archive handoff requires its SHA-256")
        expected_root = args.build_parent.resolve(strict=True) / args.source_archive_sha256
        if (archive.name != "source.tar" or root != expected_root or source_input != root / "source"
                or {path.name for path in root.iterdir()} != {"source.tar", "source"}):
            raise ValueError("source archive handoff must name its staged source directory and SHA-256")
        with archive.open("rb") as stream:
            source_sha = hashlib.file_digest(stream, "sha256").hexdigest()
        if source_sha != args.source_archive_sha256:
            raise ValueError("staged source archive differs from the WSL handoff")
        source = source_input
    else:
        root = Path(tempfile.mkdtemp(prefix="mif-portable-", dir=args.build_parent))
        archive = root / "source.tar"
        release = load_helper("_mif_snapshot", source_input / "packaging/release_metadata.py")
        source_sha = release.snapshot_source_tree(source_input, archive)
        source = extract_archive(archive, root / "source", source=True)
    print(f"[package] Build workspace: {root}", flush=True)
    print(f"[package] Build log: {root / 'build.log'}", flush=True)
    success = False
    try:
        with (root / "build.log").open("x", encoding="utf-8") as log:
            env = dict(os.environ)
            for name in ("PYTHONPATH", "MIF_INSTANSEG_MODEL_BUILD_ROOT", "MIF_NATIVE_READER_BUILD_DIR", "PYTHONPYCACHEPREFIX"):
                env.pop(name, None)
            env.update(PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_INPUT="1", PIP_NO_INDEX="1",
                       PIP_CACHE_DIR=str(root / "pip-cache"), PYTHONDONTWRITEBYTECODE="1",
                       PYINSTALLER_CONFIG_DIR=str(root / "pyinstaller-cache"))
            temp = root / "temp"
            temp.mkdir()
            env.update(TEMP=str(temp), TMP=str(temp), TMPDIR=str(temp))

            def run(arguments, *, cwd=root, changes=None, capture=False):
                """Execute an argument list, streaming output to the console and build log."""
                command = [str(item) for item in arguments]
                child_env = env.copy()
                child_env.update(changes or {})
                log.write(f"\nCommand: {command!r}\nDirectory: {cwd}\n")
                log.flush()
                process = subprocess.Popen(command, cwd=cwd, env=child_env, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, text=True, errors="replace")
                result = []
                assert process.stdout is not None
                for line in process.stdout:
                    print(line, end="", flush=True)
                    log.write(line)
                    if capture:
                        result.append(line)
                log.flush()
                if process.wait():
                    raise subprocess.CalledProcessError(process.returncode, command)
                return "".join(result).strip()

            venv = root / "venv"
            with phase("Creating native build environment and installing pinned dependencies"):
                run([sys.executable, "-I", "-m", "venv", venv])
                python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
                lock = source / f"packaging/requirements/prototype-{platform_name}{'-cuda' if args.accelerator == 'cuda' else ''}.lock"
                # The lock permits source builds for ANTLR and FlowIO. Install their
                # pinned build tools before installing the full dependency set.
                build_pins = re.findall(
                    r"(?m)^(?:setuptools|wheel|packaging)==[^\n]+(?:\n[ \t]+[^\n]*)*",
                    lock.read_text(encoding="utf-8"),
                )
                if len(build_pins) != 3:
                    raise RuntimeError("the dependency lock must include the three build-tool pins")
                build_lock = root / "build-prerequisites.lock"
                build_lock.write_text("\n".join(build_pins) + "\n", encoding="utf-8")
                run([python, "-I", "-m", "pip", "install", "--require-hashes", "--only-binary=:all:",
                     "--no-deps", "-r", build_lock], cwd=source, changes={"PIP_NO_INDEX": "0"})
                run([python, "-I", "-m", "pip", "install", "--require-hashes",
                     "--no-build-isolation", "-r", lock],
                    cwd=source, changes={"PIP_NO_INDEX": "0"})
            helper = source / "scripts/build_portable.py"
            wheel_root = root / "wheel"
            wheel_root.mkdir()
            with phase("Building and checking the installed application wheel"):
                run([python, "-I", "-m", "build", "--wheel", "--no-isolation", "--outdir", wheel_root, "."],
                    cwd=source, changes={"MIF_NATIVE_READER_BUILD_DIR": str(native)})
                wheels = tuple(wheel_root.glob("mif_pipeline-*.whl"))
                if len(wheels) != 1:
                    raise RuntimeError("wheel build must produce exactly one mif_pipeline wheel")
                run([python, "-I", "-m", "pip", "install", "--no-index", "--no-deps", wheels[0]])
                run([python, "-I", "-m", "pip", "check"])
                run([python, "-I", "-B", helper, "installed-probe", source, model, "--accelerator", args.accelerator])
            dist = root / "dist"
            with phase("Freezing the desktop and isolated Nimbus worker"):
                run([python, "-I", "-m", "PyInstaller", "--noconfirm", "--distpath", dist,
                     "--workpath", root / "pyinstaller-work", source / "packaging/pyinstaller/mif_desktop.spec"],
                    cwd=source, changes={"MIF_INSTANSEG_MODEL_BUILD_ROOT": str(model)})
                bundle = dist / "mif-desktop"
                if not (bundle / executable_name).is_file() or not (bundle / "_internal").is_dir():
                    raise RuntimeError("PyInstaller folder is missing the executable or libraries")
                if platform_name == "linux":
                    run([python, "-I", source / "scripts/stage_linux_gui_libraries.py", "--lock",
                         source / "packaging/requirements/linux-gui-libraries.lock.json", "--bundle", bundle,
                         "--build-root", root / "linux-gui-libraries"])
                run([python, "-I", source / "scripts/build_nimbus_runtime.py", "--source-root", source,
                     "--build-root", root / "nimbus-worker", "--output-root", bundle / "runtimes/nimbus", "--accelerator", args.accelerator])
            if args.accelerator == "cuda":
                with phase("Connecting the independent QuPath Torch libraries"):
                    # Register the compatible collected files once. QuPath's
                    # readiness check does not require the scoring executable.
                    run([python, "-I", source / "scripts/build_native_gpu_runtime.py", "--source-root", source,
                         "--build-root", root / "native-torch", "--output-root", bundle / "runtimes/torch-native",
                         "--reuse-library-root", bundle / "runtimes/nimbus/_internal"])
            version = run([python, "-I", "-c", 'from importlib.metadata import version; print(version("mif-pipeline"))'], capture=True)
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]*", version):
                raise ValueError("installed project version is not a safe release name")
            release_name = f"mif-pipeline-prototype-{version}-{source_sha}-{args.platform}-{args.accelerator}"
            bundle = bundle.rename(dist / (f"mif-pipeline-{version}" if os.name == "nt" else release_name))
            staged = root / (release_name + suffix)
            with phase("Verifying resources and writing release notices and metadata"):
                run([python, "-I", "-B", helper, "release-metadata", bundle, source, source_sha, args.platform, "--accelerator", args.accelerator])
            with phase("Archiving and checking the extracted application"):
                write_archive(bundle, staged)
                extracted = extract_archive(staged, root / "smoke-extraction", expected_root=bundle.name)
                verify_bundle(extracted)
                manifest = json.loads((extracted / "BUILD-MANIFEST.json").read_text(encoding="utf-8"))
                if (manifest["source_archive_sha256"], manifest["platform_tag"], manifest["project_version"]) != (source_sha, args.platform, version):
                    raise ValueError("extracted manifest differs from the release identity")
                run([python, "-I", "-B", helper, "native-library-probe", extracted, "--accelerator", args.accelerator])
                run([python, "-I", "-B", helper, "smoke-executable", extracted / executable_name])
            with phase("Publishing archive and adjacent checksum"):
                destination = output / staged.name
                sidecar = publish_archive(staged, destination)
            print(destination, flush=True)
            print(sidecar, flush=True)
            success = True
    finally:
        if success:
            # Keep the small build log beside the source snapshot for diagnosis.
            for name in ("source", "venv", "wheel", "dist", "pyinstaller-work", "pyinstaller-cache",
                         "pip-cache", "temp", "linux-gui-libraries", "nimbus-worker", "native-torch", "smoke-extraction"):
                path = root / name
                if path.exists():
                    shutil.rmtree(path)
            staged.unlink(missing_ok=True)
        else:
            print(f"[package] Failed build workspace and log preserved at {root}", file=sys.stderr, flush=True)


# ==============================================================================
#                               Command interface
# ==============================================================================


def main():
    """Dispatch the build or its environment-specific resource and smoke checks."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("extract-source")
    capture.add_argument("archive", type=Path)
    capture.add_argument("destination", type=Path)
    probe = commands.add_parser("installed-probe")
    probe.add_argument("source", type=Path)
    probe.add_argument("model", type=Path)
    probe.add_argument("--accelerator", choices=("cpu", "cuda"), default="cpu")
    smoke = commands.add_parser("smoke-executable")
    smoke.add_argument("executable", type=Path)
    native_libraries = commands.add_parser("native-library-probe")
    native_libraries.add_argument("bundle", type=Path)
    native_libraries.add_argument("--accelerator", choices=("cpu", "cuda"), default="cpu")
    metadata = commands.add_parser("release-metadata")
    metadata.add_argument("bundle", type=Path)
    metadata.add_argument("source", type=Path)
    metadata.add_argument("source_sha")
    metadata.add_argument("platform_tag", choices=PLATFORMS)
    metadata.add_argument("--accelerator", choices=("cpu", "cuda"), default="cpu")
    portable = commands.add_parser("build")
    portable.add_argument("--source-root", required=True, type=Path)
    portable.add_argument("--output-dir", required=True, type=Path)
    portable.add_argument("--build-parent", required=True, type=Path)
    portable.add_argument("--platform", required=True, choices=PLATFORMS)
    portable.add_argument("--accelerator", choices=("cpu", "cuda"), default="cpu")
    portable.add_argument("--instanseg-model-root", required=True, type=Path)
    portable.add_argument("--native-reader-build-dir", required=True, type=Path)
    portable.add_argument("--source-archive", type=Path)
    portable.add_argument("--source-archive-sha256")
    args = parser.parse_args()
    if args.command == "extract-source":
        extract_archive(args.archive, args.destination, source=True)
    elif args.command == "installed-probe":
        installed_probe(args.source, args.model, args.accelerator)
    elif args.command == "smoke-executable":
        smoke_executable(args.executable, dict(os.environ))
    elif args.command == "native-library-probe":
        native_library_probe(args.bundle, args.accelerator)
    elif args.command == "release-metadata":
        write_metadata(args.bundle, args.source, args.source_sha, args.platform_tag, args.accelerator)
    else:
        build(args)


if __name__ == "__main__":
    main()
