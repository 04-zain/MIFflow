# The worker is built in its own pinned numerical environment.
from pathlib import Path
from PyInstaller.utils.hooks import copy_metadata

source = Path(SPECPATH).parents[1]
modules = source / "src/python/mif_pipeline/cell_analysis"
analysis = Analysis(
    [str(modules / "nimbus_streaming.py")],
    pathex=[str(modules), str(source / "src/python")],
    datas=[item for distribution in ("Nimbus-Inference", "numpy", "torch", "opencv-python-headless")
           for item in copy_metadata(distribution)],
    hiddenimports=["nimbus_inference.unet"],
    excludes=["tkinter", "PySide6", "PyQt5", "PyQt6", "matplotlib", "IPython", "ipywidgets"],
    hookspath=[], hooksconfig={}, runtime_hooks=[], noarchive=False, optimize=0,
)
# The Torch hook collects upstream test programs and development executables.
# Scoring needs the shared-memory helper, but neither these tests nor protoc.
def runtime_file(entry):
    """Retain inference files and the Torch process helper in the frozen worker."""
    parts = entry[0].replace("\\", "/").split("/")
    if parts[:2] == ["torch", "test"]:
        return False
    if parts[:2] == ["torch", "bin"]:
        return parts[-1] in {"torch_shm_manager", "torch_shm_manager.exe"}
    return True

analysis.binaries = [entry for entry in analysis.binaries if runtime_file(entry)]
analysis.datas = [entry for entry in analysis.datas if runtime_file(entry)]
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, [], exclude_binaries=True, name="mif-nimbus",
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=True)
collect = COLLECT(exe, analysis.binaries, analysis.datas, strip=False, upx=False, name="mif-nimbus")
