# Build and release MIFflow

There are two ways to build and distribute MIFflow.

- **Build a wheel** for users who have a Python 3.12 environment, including on a remote processing host. They install the wheel with pip. Installing it with the `desktop` extra also provides the graphical interface.
- **Build a portable application** for desktop users on Windows or Ubuntu who want to extract the download and run it immediately. The archive includes Python, Qt and the processing libraries for the chosen operating system and processor edition.

<a id="source-and-environment"></a>
<a id="packaging-files-and-prerequisites"></a>
## Prepare the QuPath extension

The wheel and the portable application both include our Groovy scripts and the Java extension they use in QuPath. The checkout supplies the scripts. Before packaging either distribution, [compile the extension against QuPath 0.7.0](native-extension.md#build-and-install). Keep the JAR and its build record together. Both builders take this directory as an input.

<a id="python-distributions"></a>
## Build a wheel and source distribution

Open a terminal at the root of the checkout and activate a Python 3.12 environment. Install the Python build tool, point `MIF_NATIVE_READER_BUILD_DIR` to your compiled extension and choose where to write the distributions. The examples use `mif-releases` as the output directory.

**Ubuntu**

```bash
python -m pip install build
export MIF_NATIVE_READER_BUILD_DIR="$HOME/mif_pipeline_build/native"
python -m build --outdir "$HOME/mif-releases"
```

**Windows PowerShell**

```powershell
python -m pip install build
$env:MIF_NATIVE_READER_BUILD_DIR = 'C:\mif_pipeline_build\native'
python -m build --outdir 'C:\mif-releases'
```

The build first packages the source files and the compiled extension into a source distribution. It then unpacks that archive and builds the installable wheel from its contents.

| File in the output directory | How to use it |
|---|---|
| `mif_pipeline-<version>-py3-none-any.whl` | Install or distribute this file. Pip installs the declared Python dependencies with it. [Install a wheel](software.md#install-a-shared-wheel) |
| `mif_pipeline-<version>.tar.gz` | Share this archive with users who want to inspect the packaged sources or rebuild the wheel. It includes the compiled extension as well as the Java source files, so rebuilding this distribution does not require compiling the extension again. |

To build only the wheel from your checkout, add `--wheel` to the build command. If you want to run a checkout locally, [install from source](software.md#install-the-python-application). In that case pip builds and installs the wheel in one operation. [Python build commands](https://build.pypa.io/en/stable/how-to/basic-usage.html)

<a id="portable-application-builds"></a>
## Build a portable application

Build the portable application on the operating system where it will run. The launcher uses a native x86-64 Python 3.12 installation to assemble the executable and libraries for that system. Run the launcher as an ordinary user from the repository checkout.

The launcher needs the compiled extension and a local copy of the InstanSeg model. Give it separate locations for assembling the application and writing the finished archive. Keep these directories outside the checkout.

| Directory | What to put there |
|---|---|
| Compiled extension | Use the output directory from the Java extension build. |
| InstanSeg model | Place the eight model files under `model/fluorescence_nuclei_and_cells/`. Pass the parent of `model/` to the launcher. |
| Temporary build space | Choose a working directory for installing dependencies and assembling the application. |
| Release output | Choose a separate directory for the verified archive and its checksum. |

<a id="choose-the-processor-edition"></a>
### Choose the processor edition

The build commands below produce the CPU edition by default. To produce the NVIDIA edition, add `-Accelerator cuda` to the PowerShell command or `--accelerator cuda` to a shell command. This selects the CUDA libraries for the Python calculations, the Nimbus scoring program and the models that run in QuPath. A computer running GPU processing also needs a compatible NVIDIA driver. [Check NVIDIA processing](software.md#nvidia-processing-libraries)

### Windows 11

Open native x86-64 PowerShell in the repository root. On Windows, the checkout, compiled extension, model, temporary build directory and output directory must be on a local fixed NTFS volume. Use a short path for `-StagingRoot` because the build creates several levels of subdirectories.

```powershell
.\scripts\build_windows.ps1 `
  -StagingRoot 'C:\mif-build' `
  -OutputRoot 'C:\mif-build\artifacts' `
  -NativeReaderBuildDir 'C:\mif_pipeline_build\native' `
  -InstanSegModelRoot 'C:\instanseg-build-input'
```

### Ubuntu 24.04

Run the Linux launcher from the checkout. Give it absolute paths to Python, the compiled extension, the model and the desired output directory. If you build the Ubuntu application under WSL2, keep the checkout, inputs, temporary workspace and output on the Linux filesystem outside `/mnt`.

```bash
scripts/build_linux.sh \
  --source-root "$PWD" \
  --output-dir "$HOME/mif-releases" \
  --python /usr/bin/python3.12 \
  --native-reader-build-dir "$HOME/mif_pipeline_build/native" \
  --instanseg-model-root "$HOME/instanseg-build-input"
```

### Windows build from WSL2

To build a Windows application from a checkout in WSL2, run the wrapper below. It copies the source to the Windows staging directory and runs the Windows builder using native Windows Python. Use a Linux path for `--source-root` and Windows paths for the other directories.

```bash
scripts/build_windows_from_wsl.sh \
  --source-root "$PWD" \
  --staging-root 'C:\mif-build' \
  --output-root 'C:\mif-build\artifacts' \
  --native-reader-build-dir 'C:\mif_pipeline_build\native' \
  --instanseg-model-root 'C:\instanseg-build-input'
```

<a id="outputs-and-verification"></a>
<a id="outputs"></a>
## Find the portable archive

The [portable builder](../../../scripts/build_portable.py) installs the wheel and its pinned dependencies in a fresh Python environment. After it checks the InstanSeg model, Torch edition and installed application files, PyInstaller assembles the executable and the libraries it will load. On Ubuntu the bundle also includes the system libraries needed by the interface.

Before the build finishes, the builder extracts the new archive, checks its contents against the build manifest and runs the diagnostic from the extracted executable. A successful build leaves two files in the output directory. One is the application archive (a ZIP on Windows or a `.tar.gz` on Ubuntu). The other is its `.sha256` checksum. The archive name identifies the application version, source checksum, operating system and processor edition.

<a id="bundled-nimbus-worker"></a>
<a id="bundled-numerical-workers"></a>
<a id="qupath-torch-libraries"></a>
<a id="how-processing-libraries-are-packaged"></a>
<details>
<summary>Processing libraries included with the portable desktop</summary>

| Program | Libraries included in the archive |
|---|---|
| KRONOS | The `mif-numerical-worker` executable prepares the encoder for QuPath and fits the spatial graph model. It shares the desktop’s Python libraries, including Torch 2.6.0. |
| Nimbus | Nimbus uses NumPy 1.26.4 and Torch 2.7.1. Its library versions differ from the desktop’s, so `build_nimbus_runtime.py` packages a separate scoring program under `runtimes/nimbus` in both editions. |
| QuPath GPU processing | For the NVIDIA edition, `build_native_gpu_runtime.py` prepares the Java adapter and records the locations of compatible Torch libraries under `runtimes/torch-native`. QuPath can reuse the libraries packaged for Nimbus. |

For a Python installation, the software guide explains how to [add Nimbus](software.md#nimbus-for-source-installations) or [prepare NVIDIA processing](software.md#add-nvidia-processing-libraries). Researchers select the model weights used for their analysis in [Software setup](software.md#open-software-setup).

</details>

<a id="acceptance-before-distribution"></a>
<a id="native-ome-zarr-reader-build"></a>
<a id="java-extension-resources"></a>
## Verify the distributions

### Check a wheel rebuilt from the source distribution

Extract the source distribution into a fresh directory and unset `MIF_NATIVE_READER_BUILD_DIR`. From the extracted directory, run `python -m build --wheel`. This checks that someone receiving only the source archive can build a wheel with the QuPath extension included.

Install the resulting wheel with the [desktop installation commands](software.md#install-a-shared-wheel) in a separate Python environment. Open a terminal outside the checkout and activate that environment. Run `mif-batch --help` and open `mif-desktop` to check the two installed entry points.

### Run the extracted portable application

Extract the complete archive outside the build tree on its target operating system. Run the extracted executable with `--portable-diagnostics-json` to check the bundled programs and libraries. Add `--result-json FILE` to write the report to a new file at an absolute path.

Open the extracted desktop, connect QuPath and read a source image. Run one of the intended analyses on the processor for that edition to check image reading and model execution together.

### Check the affected analysis stages

Run a representative whole slide through the affected stages. Pause the run, reopen it and continue to its saved results. For image-reader changes, compare the prepared image’s pixel values and physical calibration in QuPath with the expected values. For cluster changes, verify the shared paths and run a job in a Slurm allocation.

<a id="application-and-processing-program-identity"></a>
<a id="dependencies-and-licences"></a>
## Dependencies and licences

For a wheel, the [package definition](../../../pyproject.toml) specifies the Python dependencies and commands installed into the recipient’s environment. For portable archives, the [platform dependency files](../../../packaging/requirements/) specify exact versions and hashes for each edition. The build manifest identifies the source and packaged resources. A separate runtime record identifies the libraries used by Nimbus.

Distribute the [third-party notices](../../../packaging/notices/THIRD_PARTY_NOTICES.txt) with each build. These identify the software and model terms, including the licences for FlowSOM Python 0.2.2 (GPLv3) and PhenoGraph 1.5.7 (MIT). FlowSOM also includes the MIT attribution for its consensus-clustering code.

## Publish the downloads

Create a GitHub release for the tested source revision. Give each operating system and processor edition its own row in the download table. Link the application archive and its matching SHA-256 file in that row so users can identify their edition and check the download.

Each GitHub release asset must be smaller than 2 GiB. If an archive exceeds that limit, link its release row to the verified file on the download host. Download each published archive and verify it against the checksum produced by the builder. [GitHub release assets](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)
