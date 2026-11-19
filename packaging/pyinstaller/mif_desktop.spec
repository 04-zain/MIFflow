# -*- mode: python ; coding: utf-8 -*-

from importlib.util import module_from_spec, spec_from_file_location
import os
from pathlib import Path
import sys

from PyInstaller.utils.hooks import copy_metadata
from PyInstaller.utils.hooks.qt import pyside6_library_info


_RESOURCE_INVENTORY_MODULE = "_mif_portable_resource_inventory"
_resource_inventory_spec = spec_from_file_location(
    _RESOURCE_INVENTORY_MODULE,
    Path(SPECPATH) / "resource_inventory.py",
)
if _resource_inventory_spec is None or _resource_inventory_spec.loader is None:
    raise RuntimeError("cannot load portable resource inventory")
_resource_inventory = module_from_spec(_resource_inventory_spec)
sys.modules[_RESOURCE_INVENTORY_MODULE] = _resource_inventory
_resource_inventory_spec.loader.exec_module(_resource_inventory)
collect_frozen_datas = _resource_inventory.collect_frozen_datas
_instanseg_model_text = os.environ.get("MIF_INSTANSEG_MODEL_BUILD_ROOT")
if not _instanseg_model_text:
    raise RuntimeError("explicit staged InstanSeg model root is required")
_instanseg_model_root = Path(_instanseg_model_text)

REQUIRED_HIDDEN_IMPORTS = _resource_inventory.REQUIRED_HIDDEN_IMPORTS

QT_HIDDEN_IMPORTS = (
    "PySide6",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
)
# Scientific routes import these backends after their inputs are prepared.
CLUSTERING_HIDDEN_IMPORTS = ("igraph", "leidenalg", "umap", "flowsom.models", "phenograph.cluster")
# ome-types registers this xsdata class type through a metadata entry point.
XSDATA_RUNTIME_PLUGIN_HIDDEN_IMPORTS = (
    "xsdata_pydantic_basemodel.hooks.class_type",
)
# The Bio-Formats worker loads this bridge only after JVM attestation.
BIOFORMATS_BRIDGE_HIDDEN_IMPORTS = ("bffile",)
RUNTIME_METADATA_DISTRIBUTIONS = (
    "bffile",
    "igraph",
    "leidenalg",
    "umap-learn",
    "flowsom", "phenograph",
    "flexcache",
    "flexparser",
    "jgo",
    "ome-types",
    "pint",
    "scyjava",
    "threadpoolctl",
    "torch", "torchvision", "cellpose", "segment_anything", "transformers", "omegaconf", "tokenizers", "safetensors", "huggingface-hub",
    "fastremap", "fill-voids", "opencv-python-headless", "roifile",
)
# We retain scientific distribution versions used in clustering and embedding provenance.
_runtime_metadata_datas = [
    item
    for name in RUNTIME_METADATA_DISTRIBUTIONS
    for item in copy_metadata(name, recursive=False)
]

qt_binaries = [
    *pyside6_library_info.collect_plugins("platforms"),
    *pyside6_library_info.collect_plugins("imageformats"),
    *pyside6_library_info.collect_plugins("styles"),
]


def _without_unused_qt_binaries(binaries, datas, platform_name):
    # QtGui's hook collects unused Virtual Keyboard and PDF plugins and their libraries.
    expected = {
        "nt": {"qtvirtualkeyboardplugin.dll", "qt6virtualkeyboard.dll"},
        "posix": {
            "libqtvirtualkeyboardplugin.so",
            "libqt6virtualkeyboard.so.6",
            "libqt6virtualkeyboardqml.so.6",
        },
    }.get(platform_name)
    if expected is None:
        raise RuntimeError("unsupported platform for Qt Virtual Keyboard exclusion")
    expected_pdf = (
        {
            "PySide6/plugins/imageformats/qpdf.dll",
            "PySide6/Qt6Pdf.dll",
        }
        if platform_name == "nt" else {
            "PySide6/Qt/plugins/imageformats/libqpdf.so",
            "PySide6/Qt/lib/libQt6Pdf.so.6",
        }
    )
    expected_symlinks = (
        {
            (name, f"PySide6/Qt/lib/{name}", "SYMLINK")
            for name in ("libQt6VirtualKeyboard.so.6", "libQt6VirtualKeyboardQml.so.6")
        }
        if platform_name == "posix" else set()
    )
    expected_pdf_symlinks = (
        {("libQt6Pdf.so.6", "PySide6/Qt/lib/libQt6Pdf.so.6", "SYMLINK")}
        if platform_name == "posix" else set()
    )
    present = {Path(binary[0]).name.casefold() for binary in binaries}
    if not expected <= present or not expected_symlinks <= set(datas):
        raise RuntimeError("expected Qt Virtual Keyboard binaries are missing from analysis")
    pdf_paths = {path.casefold() for path in expected_pdf}
    present_paths = {str(binary[0]).replace("\\", "/").casefold() for binary in binaries}
    if not pdf_paths <= present_paths or not expected_pdf_symlinks <= set(datas):
        raise RuntimeError("expected Qt PDF binaries are missing from analysis")
    retained_binaries = [
        binary for binary in binaries
        if Path(binary[0]).name.casefold() not in expected
        and str(binary[0]).replace("\\", "/").casefold() not in pdf_paths
    ]
    retained_datas = [data for data in datas if data not in expected_symlinks | expected_pdf_symlinks]
    if any(
        "virtualkeyboard" in Path(entry[0]).name.casefold()
        for entry in (*retained_binaries, *retained_datas)
    ):
        raise RuntimeError("unrecognized Qt Virtual Keyboard binary remains in analysis")
    if any(
        "qpdf" in Path(entry[0]).name.casefold()
        or "qt6pdf" in Path(entry[0]).name.casefold()
        for entry in (*retained_binaries, *retained_datas)
    ):
        raise RuntimeError("unrecognized Qt PDF binary remains in analysis")
    return retained_binaries, retained_datas


analysis = Analysis(
    ["../entrypoints/mif_desktop.py"],
    pathex=[],
    binaries=qt_binaries,
    datas=[
        *collect_frozen_datas(_instanseg_model_root),
        *_runtime_metadata_datas,
    ],
    hiddenimports=[
        *REQUIRED_HIDDEN_IMPORTS,
        *QT_HIDDEN_IMPORTS,
        *CLUSTERING_HIDDEN_IMPORTS,
        *XSDATA_RUNTIME_PLUGIN_HIDDEN_IMPORTS,
        *BIOFORMATS_BRIDGE_HIDDEN_IMPORTS,
        # The selected external model imports these auto classes lazily.
        "transformers.models.auto.tokenization_auto",
        "transformers.models.auto.modeling_auto",
        "transformers.models.bert.configuration_bert",
        "transformers.models.bert.modeling_bert",
        "transformers.models.bert.tokenization_bert",
        "transformers.models.bert.tokenization_bert_fast",
        "omegaconf",
        "cellpose.models", "cellpose.vit_sam", "cellpose.dynamics", "segment_anything",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PyQt6"],
    noarchive=False,
    optimize=0,
)
analysis.binaries, analysis.datas = _without_unused_qt_binaries(
    analysis.binaries, analysis.datas, os.name,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="mif-desktop",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
# This private console launcher shares the same code archive and libraries.
# Its binary stdin/stdout carry the KRONOS protocol on Windows as well as Linux.
worker = EXE(
    pyz, analysis.scripts, [], exclude_binaries=True,
    name="mif-numerical-worker", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=True,
)
collect = COLLECT(
    exe, worker,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="mif-desktop",
)
