"""Connect the independent native Torch libraries to QuPath's Java bridge.

A neutral library record identifies the pinned CUDA payload. Portable builds
may share compatible files with another runtime, while source installations can
provide the libraries alone. No scoring executable, model or Python import is
needed. The application's main Python runtime keeps its own pinned Torch version.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path, PurePosixPath
import sys
import tempfile
from typing import Mapping
from zipfile import ZipFile

from mif_pipeline._software import read_software_description
from mif_pipeline.pipeline_core.atomic_publication import publish_no_replace
from .prerequisite_setup import default_cpu_setup_target
from .setup_transfer import download_pinned_file


@dataclass(frozen=True, slots=True)
class NativeGpuStatus:
    """Describe installed libraries without probing the processor or loading Torch."""

    state: str
    python_detail: str
    native_detail: str


def _python_library_detail(torch_version: str) -> str:
    """Report installed metadata without treating an unspecified build as CPU."""
    if torch_version == "not installed":
        return "Python: PyTorch is missing. Complete or repair the application installation."
    if torch_version.split("+")[0] != "2.6.0":
        return (f"Python: PyTorch {torch_version} is installed; version 2.6.0 is required. "
                "Repair the application or reinstall the documented source dependencies.")
    if "+cu" in torch_version:
        return f"Python: NVIDIA and CPU libraries installed (PyTorch {torch_version}). Check the device in Computing."
    if "+cpu" in torch_version:
        return (f"Python: CPU libraries installed (PyTorch {torch_version}). "
                "For NVIDIA processing, use the NVIDIA edition or install PyTorch 2.6.0+cu126 as described in the source setup guide.")
    return (f"Python: PyTorch {torch_version} is installed. This version label does not identify NVIDIA support. "
            "Use Computing to check the available devices.")


def native_torch_runtime_root() -> Path:
    """Locate the library record beside the desktop or in its Python prefix."""
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(sys.prefix)
    return base / "runtimes" / "torch-native"


def _platform() -> str:
    if sys.platform not in {"linux", "win32"}:
        raise ValueError("The NVIDIA application is available for Linux and Windows.")
    return "win-x86_64" if sys.platform == "win32" else "linux-x86_64"


def native_gpu_cache(platform: str, environ: Mapping[str, str] | None = None, *,
                     root: Path | None = None) -> Path:
    """Reuse a packaged bridge, or select the user's writable DJL setup cache."""
    bundled = (root or native_torch_runtime_root()) / "djl-cache"
    if _bridge_path(bundled, platform).is_file():
        return bundled
    return default_cpu_setup_target(platform, environ=environ).parents[2] / "gpu" / "djl-cache"


def _bridge_path(cache: Path, platform: str) -> Path:
    release = read_software_description("pytorch")
    name = release["cuda_jni"][platform]["path"]
    return (cache / "pytorch" / f'{release["version"]}-cu128-{platform}'
            / f'{release["java_engine_version"]}-{name}')


def cuda_library_directories(root: Path, platform: str) -> tuple[Path, ...] | None:
    """Read the independent library record without loading Torch or a scorer.

    A relative provider may reuse compatible files in a sibling runtime. All
    paths must stay within the installation's runtimes directory, so the same
    record remains valid when a portable application is moved or extracted.
    """
    record = root / "runtime.json"
    if not record.is_file():
        return None
    description = json.loads(record.read_text(encoding="utf-8"))
    if not isinstance(description, dict):
        raise ValueError("The native Torch library record is invalid. Repair or reinstall the NVIDIA libraries.")
    if platform not in {"linux-x86_64", "win-x86_64"}:
        raise ValueError("The NVIDIA libraries require Linux or Windows x86-64.")
    expected_platform = "win32" if platform == "win-x86_64" else "linux"
    if (description.get("schema") != "mif-native-torch-runtime/v1"
            or description.get("platform") != expected_platform
            or description.get("torch_version") != "2.7.1+cu128"):
        raise ValueError("The included NVIDIA libraries do not match QuPath's required Torch 2.7.1 CUDA 12.8 release.")
    relative = description.get("library_root")
    if (not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative
            or any(ord(character) < 32 for character in relative)
            or PurePosixPath(relative).is_absolute() or PurePosixPath(relative).as_posix() != relative):
        raise ValueError("The native Torch library location must be a relative path within this installation.")
    internal = (root / relative).resolve()
    if not internal.is_relative_to(root.parent.resolve()):
        raise ValueError("The native Torch library location is outside this installation's runtimes directory.")
    torch = internal / "torch" / "lib"
    required = ("torch.dll", "torch_cpu.dll", "torch_cuda.dll", "c10.dll") if platform == "win-x86_64" else (
        "libtorch.so", "libtorch_cpu.so", "libtorch_cuda.so", "libc10.so")
    if not all((torch / name).is_file() for name in required):
        raise ValueError("The included NVIDIA library folder is incomplete. Repair or reinstall the NVIDIA application.")
    # Frozen Windows providers may collect dependency DLLs at their root.
    # Linux wheels retain separate nvidia/<component>/lib folders.
    dependencies = sorted(path for path in (internal / "nvidia").glob("*/lib") if path.is_dir())
    directories = (torch, *dependencies, internal)
    if any(not path.resolve().is_relative_to(root.parent.resolve()) for path in directories):
        raise ValueError("A native Torch library directory is outside this installation.")
    return directories


def native_gpu_environment(inherited: Mapping[str, str]) -> dict[str, str]:
    """Select the installed CUDA build before QuPath starts, including CPU use.

    A CUDA libtorch build also runs CPU calculations. Selecting it as one unit
    avoids mixing same-named CPU and CUDA libraries in a single JVM. Explicit
    prerequisite-probe selections remain isolated. Launching checks locations;
    setup verifies the bridge checksum once instead of rehashing it per stage.
    """
    environment = dict(inherited)
    if sys.platform not in {"linux", "win32"}:
        return environment
    environment["DJL_OFFLINE"] = "true"
    if environment.get("PYTORCH_LIBRARY_PATH") or environment.get("PYTORCH_FLAVOR"):
        return environment
    platform = _platform()
    cache = native_gpu_cache(platform, environment)
    if not _bridge_path(cache, platform).is_file():
        return environment
    directories = cuda_library_directories(native_torch_runtime_root(), platform)
    if directories is None:
        return environment
    release = read_software_description("pytorch")
    environment.update(PYTORCH_LIBRARY_PATH=str(directories[0]), PYTORCH_FLAVOR="cu128",
                       PYTORCH_VERSION=release["version"], DJL_CACHE_DIR=str(cache))
    name = "PATH" if platform == "win-x86_64" else "LD_LIBRARY_PATH"
    environment[name] = os.pathsep.join((*map(str, directories), *filter(None, (environment.get(name),))))
    return environment


class NativeGpuSetupService:
    """Inspect installed support and add the pinned bridge after a setup action."""

    def __init__(self, *, root: Path | None = None, cache: Path | None = None,
                 platform: str | None = None):
        self.platform = platform or {"linux": "linux-x86_64", "win32": "win-x86_64"}.get(sys.platform, sys.platform)
        self.root = root or native_torch_runtime_root()
        self.cache = cache
        if self.cache is None and self.platform in {"linux-x86_64", "win-x86_64"}:
            self.cache = native_gpu_cache(self.platform, root=self.root)

    def inspect(self) -> NativeGpuStatus:
        """Report Python support separately from QuPath library installation."""
        try:
            torch_version = version("torch")
        except PackageNotFoundError:
            torch_version = "not installed"
        python_detail = _python_library_detail(torch_version)
        if self.platform not in {"linux-x86_64", "win-x86_64"}:
            return NativeGpuStatus("unavailable", python_detail,
                "InstanSeg and KRONOS: NVIDIA library setup is available on Linux and Windows.")
        try:
            directories = cuda_library_directories(self.root, self.platform)
            if directories is None:
                return NativeGpuStatus("unavailable", python_detail,
                    "InstanSeg and KRONOS in QuPath: NVIDIA libraries are missing. Use the NVIDIA edition or build the native Torch libraries described in the source setup guide.")
            bridge = _bridge_path(self.cache, self.platform)
            if not bridge.is_file():
                return NativeGpuStatus("missing", python_detail,
                    "InstanSeg and KRONOS in QuPath: NVIDIA libraries are included. Select Install to add their 3.4 MB Java adapter.")
            expected = read_software_description("pytorch")["cuda_jni"][self.platform]["sha256"]
            if hashlib.sha256(bridge.read_bytes()).hexdigest() != expected:
                raise ValueError(f"The installed Java adapter does not match the required release: {bridge}")
        except (OSError, ValueError, TypeError, KeyError) as error:
            return NativeGpuStatus("incompatible", python_detail, str(error))
        return NativeGpuStatus("ready", python_detail,
            "InstanSeg and KRONOS in QuPath: NVIDIA libraries and the Java adapter are installed. They require a compatible NVIDIA driver, even for CPU processing. Choose and check the device in Computing.")

    def install(self) -> NativeGpuStatus:
        """Verify the publisher archive and publish its one matching JNI library.

        The caller supplies the explicit installation action. We reuse an intact
        installation, never overwrite an existing incompatible bridge, and move
        the verified file to its final name only after extraction succeeds.
        """
        status = self.inspect()
        if status.state == "ready":
            return status
        if status.state != "missing":
            raise ValueError(status.native_detail)
        release = read_software_description("pytorch")
        archive = release["jni_archive"]
        member = release["cuda_jni"][self.platform]
        target = _bridge_path(self.cache, self.platform)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".gpu-bridge-", dir=target.parent) as temporary:
            folder = Path(temporary)
            download = folder / "bridge.jar"
            download_pinned_file(archive["url"], download, archive["sha256"], archive["size_bytes"])
            with ZipFile(download) as zipped:
                data = zipped.read(f'jnilib/{self.platform}/cu128/{member["path"]}')
            if hashlib.sha256(data).hexdigest() != member["sha256"]:
                raise ValueError("The downloaded Java adapter does not match the required release.")
            prepared = folder / member["path"]
            prepared.write_bytes(data)
            publish_no_replace(prepared, target, self.platform)
        return self.inspect()
