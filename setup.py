"""Stage registered QuPath scripts and the explicitly built native extension.

Project metadata, dependencies and ordinary package data are in pyproject.toml.
We add the same runtime assets to wheels and source distributions through the
build hooks below. The editable sources remain in their language directories.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil

from setuptools import setup
from setuptools.errors import FileError
from setuptools.command.build_py import build_py as _build_py
from setuptools.command.sdist import sdist as _sdist

REPOSITORY_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = REPOSITORY_ROOT / "src" / "python"
REGISTRY_PATH = SOURCE_ROOT / "mif_pipeline/qupath_runtime/_script_asset_registry.py"


def _load_script_registry():
    """Load our source-asset list without importing scientific dependencies."""
    spec = importlib.util.spec_from_file_location("_mif_qupath_registry", REGISTRY_PATH)
    if spec is None or spec.loader is None:
        raise FileError(f"cannot load QuPath script registry: {REGISTRY_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SCRIPT_REGISTRY = _load_script_registry()


def _stage_qupath_assets(staging_root: Path) -> tuple[Path, ...]:
    """Copy current runtime sources into an unpublished package staging tree.

    We copy each registered source to its installed resource location and return
    the staged paths for the wheel inventory. A missing script fails the build.
    Native releases supply their compiled JAR explicitly; we stage that archive
    and its streamed checksum together for profile setup.
    """
    assets = Path("mif_pipeline/qupath_runtime/assets")
    sources = [
        (REPOSITORY_ROOT / asset.value, assets / SCRIPT_REGISTRY.relative_script_path(asset))
        for asset in SCRIPT_REGISTRY.QuPathScriptAsset
    ] + [
        (REPOSITORY_ROOT / "qupath" / "scripts" / relative, assets / relative)
        for relative in SCRIPT_REGISTRY.INTERNAL_PROBE_PATHS
    ]
    missing = [str(source) for source, _ in sources if not source.is_file()]
    if missing:
        raise FileError("missing QuPath runtime source: " + ", ".join(missing))
    staged = []
    for source, relative in sources:
        target = staging_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        staged.append(target)
    native_build = os.environ.get("MIF_NATIVE_READER_BUILD_DIR")
    if native_build:
        jar = Path(native_build) / "qupath-extension-mif-0.1.0.jar"
        if not jar.is_file():
            raise FileError(
                f"Java extension JAR is missing: {jar}. "
                "Build it with scripts/build_native_extension.py, then set "
                "MIF_NATIVE_READER_BUILD_DIR to the actual output directory."
            )
        target = staging_root / assets / "native"
        target.mkdir(parents=True, exist_ok=True)
        jar_target = target / jar.name
        shutil.copyfile(jar, jar_target)
        with jar_target.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        description = {
            "name": jar.name, "version": "0.1.0",
            "url": "package://mif_pipeline.qupath_runtime.assets/native/" + jar.name,
            "size_bytes": jar_target.stat().st_size, "sha256": digest,
        }
        description_path = target / "reader.json"
        description_path.write_text(json.dumps(description, sort_keys=True), encoding="utf-8")
        staged.extend((jar_target, description_path))
    return tuple(staged)


class build_py(_build_py):
    def run(self) -> None:
        """Rebuild package staging so deleted modules cannot enter a later wheel."""
        staging = Path(self.build_lib).resolve()
        source = SOURCE_ROOT.resolve()
        if staging.is_relative_to(source) or source.is_relative_to(staging):
            raise FileError("Package staging must be separate from the Python source directory")
        for name in ("mif_pipeline", "mif_containment"):
            target = staging / name
            if target.exists():
                shutil.rmtree(target)
        super().run()
        self._staged_qupath_assets = _stage_qupath_assets(Path(self.build_lib))
        native = Path(self.build_lib) / "mif_pipeline/qupath_runtime/assets/native"
        if not all((native / name).is_file() for name in ("qupath-extension-mif-0.1.0.jar", "reader.json")):
            raise FileError("Build the native extension and set MIF_NATIVE_READER_BUILD_DIR before building the application")

    def get_outputs(self, include_bytecode: bool = True) -> list[str]:
        outputs = super().get_outputs(include_bytecode=include_bytecode)
        return list(dict.fromkeys(outputs + [
            str(path) for path in getattr(self, "_staged_qupath_assets", ())
        ]))


class sdist(_sdist):
    def make_release_tree(self, base_dir: str, files: list[str]) -> None:
        super().make_release_tree(base_dir, files)
        _stage_qupath_assets(Path(base_dir) / "src" / "python")


setup(cmdclass={"build_py": build_py, "sdist": sdist})
