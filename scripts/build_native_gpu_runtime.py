#!/usr/bin/env python3
"""Stage QuPath's pinned NVIDIA libraries independently of scoring software.

Source installations copy native libraries from hash-locked wheels. Portable
builds can register a compatible library directory already inside the same
application, without copying those libraries again. Neither path imports Torch
or loads a model. Device availability is checked separately in Computing.
"""
from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import distributions
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile


def _torch_version(provider: Path) -> str:
    """Read the wheel metadata without importing its Python package."""
    versions = [item.version for item in distributions(path=[str(provider)])
                if item.metadata.get("Name", "").lower() == "torch"]
    if versions != ["2.7.1+cu128"]:
        raise ValueError("The native library provider must contain exactly Torch 2.7.1+cu128.")
    return versions[0]


def _copy_licenses(provider: Path, destination: Path) -> None:
    """Retain the original notices for every redistributed library wheel."""
    for distribution in distributions(path=[str(provider)]):
        name = distribution.metadata.get("Name", "")
        if name.lower() != "torch" and not name.lower().startswith("nvidia-"):
            continue
        copied = 0
        for file in distribution.files or ():
            if not any(word in part.lower() for part in file.parts for word in ("license", "licence", "copying", "notice")):
                continue
            source = Path(distribution.locate_file(file))
            if not source.is_file():
                continue
            target = destination / name / Path(*file.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            copied += 1
        if not copied:
            raise ValueError(f"The {name} library wheel is missing its upstream licence text.")


def build_runtime(source_root: Path, build_root: Path, output_root: Path, *,
                  reuse_library_root: Path | None = None) -> None:
    """Publish a neutral library record, its adapter and any owned library files."""
    if sys.version_info[:2] != (3, 12) or sys.platform not in {"linux", "win32"}:
        raise RuntimeError("Build the native libraries with CPython 3.12 on Linux or Windows.")
    if platform.machine().lower() not in {"x86_64", "amd64"}:
        raise RuntimeError("The native NVIDIA libraries require an x86-64 computer.")
    source_root, build_root, output_root = map(Path, (source_root, build_root, output_root))
    if output_root.exists() or output_root.is_symlink():
        raise FileExistsError(output_root)
    lock_platform = "windows" if os.name == "nt" else "linux"
    native_platform = "win-x86_64" if os.name == "nt" else "linux-x86_64"
    lock = source_root / f"packaging/requirements/native-torch-{lock_platform}-cuda.lock"
    provider = reuse_library_root
    if provider is None:
        provider = build_root / "wheel-libraries"
        if provider.exists():
            raise FileExistsError(provider)
        build_root.mkdir(parents=True, exist_ok=True)
        environment = {name: value for name, value in os.environ.items()
                       if name not in {"PYTHONPATH", "PYTHONHOME", "PIP_NO_INDEX"}}
        subprocess.run([
            sys.executable, "-I", "-m", "pip", "install", "--require-hashes", "--only-binary=:all:",
            "--no-deps", "--no-compile", "--extra-index-url", "https://download.pytorch.org/whl/cu128",
            "--target", str(provider), "-r", str(lock),
        ], cwd=source_root, env=environment, check=True)
    provider = Path(provider).resolve(strict=True)
    torch_version = _torch_version(provider)

    # Use the selected source's small setup helper, independently of the
    # version of the desktop installed in the build interpreter.
    sys.path.insert(0, str(source_root / "src/python"))
    from mif_pipeline.orchestration.setup.native_gpu_setup import NativeGpuSetupService, cuda_library_directories
    from mif_pipeline.pipeline_core.atomic_publication import publish_no_replace

    output_root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".torch-native-", dir=output_root.parent) as temporary:
        staged = Path(temporary)
        if reuse_library_root is None:
            libraries = staged / "libraries"
            shutil.copytree(provider / "torch/lib", libraries / "torch/lib", symlinks=True)
            for folder in sorted((provider / "nvidia").glob("*/lib")):
                if folder.is_dir():
                    shutil.copytree(folder, libraries / folder.relative_to(provider), symlinks=True)
            _copy_licenses(provider, staged / "licenses")
            relative = "libraries"
        else:
            relative = Path(os.path.relpath(provider, staged)).as_posix()
        record = {
            "schema": "mif-native-torch-runtime/v1", "platform": sys.platform,
            "torch_version": torch_version, "library_root": relative,
            "dependency_lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
        }
        (staged / "runtime.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        cuda_library_directories(staged, native_platform)
        jni_notices = staged / "licenses/DJL"
        jni_notices.mkdir(parents=True, exist_ok=True)
        for name in ("DJL-Apache-2.0-LICENSE.txt", "DJL-NOTICE.txt"):
            shutil.copyfile(source_root / "packaging/notices" / name, jni_notices / name)
        status = NativeGpuSetupService(root=staged, cache=staged / "djl-cache", platform=native_platform).install()
        if status.state != "ready":
            raise RuntimeError(status.native_detail)
        publish_no_replace(staged, output_root, native_platform)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--build-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--reuse-library-root", type=Path,
                        help="Reuse a compatible provider within the same runtimes directory.")
    args = parser.parse_args()
    build_runtime(args.source_root.resolve(), args.build_root.resolve(), args.output_root.resolve(),
                  reuse_library_root=args.reuse_library_root.resolve() if args.reuse_library_root else None)
