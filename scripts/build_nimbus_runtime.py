"""Build the portable Nimbus worker in its own pinned Python environment.

Release builders call this before assembling the desktop archive. The worker
includes Python and its numerical libraries, so deployment needs no interpreter
selection, environment activation or network installation. Model weights remain
the separately selected optional download in Software setup.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import venv


def build_runtime(source_root: Path, build_root: Path, output_root: Path, *, accelerator: str = "cpu") -> None:
    """Freeze current worker sources and verified dependencies into a new folder.

    We build with CPython 3.12 on the release's native platform. The two hash
    locks separate the executed inference dependencies from Nimbus's optional
    notebook/training stack. A completed worker must report the expected import
    versions before its directory is copied into the application bundle.
    """
    if sys.version_info[:2] != (3, 12) or sys.platform not in {"linux", "win32"}:
        raise RuntimeError("Build Nimbus with native CPython 3.12 on Linux or Windows")
    if output_root.exists():
        raise FileExistsError(f"Choose an empty Nimbus output location: {output_root}")
    if accelerator not in {"cpu", "cuda"}:
        raise ValueError("Choose the CPU or NVIDIA CUDA edition")
    requirements = source_root / "packaging/requirements"
    platform_name = "windows" if os.name == "nt" else "linux"
    lock = requirements / f"nimbus-{platform_name}{'-cuda' if accelerator == 'cuda' else ''}.lock"
    code_lock = requirements / "nimbus-model-code.lock"
    environment_root = build_root / "environment"
    build_root.mkdir(parents=True, exist_ok=True)
    venv.EnvBuilder(with_pip=True, clear=True).create(environment_root)
    python = environment_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    # The parent disables indexes for its wheel build. This fresh worker
    # environment installs its own hash-locked dependencies before freezing.
    environment.pop("PIP_NO_INDEX", None)
    # Override the desktop builder's cache; --clean must stay inside this worker build.
    environment["PYINSTALLER_CONFIG_DIR"] = str((build_root / "pyinstaller-cache").resolve())

    def run(*arguments: str) -> None:
        subprocess.run([str(python), *arguments], cwd=source_root, env=environment, check=True)

    run("-m", "pip", "install", "--only-binary=:all:", "--require-hashes",
        "--extra-index-url", "https://download.pytorch.org/whl/" + ("cu128" if accelerator == "cuda" else "cpu"),
        "-r", str(lock))
    # Only the released UNet module is used; its dependency closure is above.
    run("-m", "pip", "install", "--only-binary=:all:", "--require-hashes", "--no-deps", "-r", str(code_lock))
    run("-m", "PyInstaller", "--clean", "--noconfirm", "--distpath", str(build_root / "dist"),
        "--workpath", str(build_root / "work"), str(source_root / "packaging/pyinstaller/mif_nimbus.spec"))
    bundle = build_root / "dist/mif-nimbus"
    executable = bundle / ("mif-nimbus.exe" if os.name == "nt" else "mif-nimbus")
    probe = subprocess.run([str(executable), "--check-runtime"], capture_output=True, check=True,
                           env=environment, timeout=60)
    versions = json.loads(probe.stdout)
    expected_torch = "2.7.1+cu128" if accelerator == "cuda" else "2.7.1+cpu"
    if versions.get("torch") != expected_torch:
        raise RuntimeError("Nimbus Torch differs from the selected CPU/NVIDIA edition")
    sources = [source_root / "src/python/mif_pipeline/cell_analysis" / name
               for name in ("nimbus.py", "nimbus_streaming.py", "nimbus_tiling.py")]
    sources.extend(source_root / name for name in (
        "packaging/pyinstaller/mif_nimbus.spec",
        "scripts/build_nimbus_runtime.py",
        "src/python/mif_pipeline/_devices.py",
    ))
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    description = {
        "schema": "mif-nimbus-runtime/v1", "platform": sys.platform, "accelerator": accelerator,
        "versions": versions, "sources": {str(path.relative_to(source_root)): digest(path) for path in sources},
        "dependency_lock_sha256": digest(lock), "model_code_lock_sha256": digest(code_lock),
    }
    (bundle / "runtime.json").write_text(json.dumps(description, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    # Keep notices from all installed build/runtime distributions with the worker.
    run("-c", """from importlib.metadata import distributions
from pathlib import Path
import shutil,sys
target=Path(sys.argv[1])
for distribution in distributions():
    for file in distribution.files or ():
        if not any('license' in part.lower() or 'copying' in part.lower() or 'notice' in part.lower() for part in file.parts):
            continue
        source=Path(distribution.locate_file(file))
        if source.is_file():
            destination=target/distribution.metadata['Name']/Path(*file.parts)
            destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source,destination)
""", str(bundle / "licenses"))
    # CPython is embedded in the worker and is not a wheel distribution.
    python_license = next((path for path in (
        Path(sysconfig.get_path("stdlib")) / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE.txt",
    ) if path.is_file()), None)
    if python_license is None:
        raise FileNotFoundError("The build interpreter's CPython LICENSE.txt is missing")
    python_notices = bundle / "licenses" / "Python"
    python_notices.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(python_license, python_notices / "LICENSE.txt")
    output_root.parent.mkdir(parents=True, exist_ok=True)
    # PyInstaller uses relative library symlinks on POSIX. Keep their identity
    # and shared storage when moving the complete worker into the application.
    shutil.copytree(bundle, output_root, symlinks=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--build-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--accelerator", choices=("cpu", "cuda"), default="cpu")
    args = parser.parse_args()
    build_runtime(args.source_root.resolve(), args.build_root.resolve(), args.output_root.resolve(), accelerator=args.accelerator)
