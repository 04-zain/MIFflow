# Analysis and software settings

[Analysis setup](analysis.md) · [Analysis-folder contents](analysis-folder.md)

<a id="reference-configuration"></a>

<a id="reference-configuration--configuration"></a>
<a id="configuration"></a>

The files under `config/defaults/` set the initial values for each analysis method. Once you save a method’s settings in an analysis, those choices take precedence when you reopen it. The files under `config/software/` identify the software releases used for processing and the checks performed during Software setup.

<a id="reference-configuration--i-initial-settings-by-module"></a>
## Initial settings by module

Look in `src/python/mif_pipeline/config/defaults/` for the file corresponding to the processing module. Within each file, the named sections hold the starting values for individual methods.

### Tissue classifiers and KRONOS

`classifier.json` contains the initial RF, SVM and logistic-regression settings, feature normalization, training allocation and parameter-search values. `classifier.rtrees` sets the initial RF parameters, while `classifier.grid` lists the values initially offered for cross-validation. `classifier.kronos` contains the graph-fitting and working-memory settings used by the KRONOS route. The [tissue-classifier guide](../tissue-classification/index.md) explains feature selection, RF/SVM parameters and training procedures.

### Tissue detection

`tissue_detection.json` supplies the initial settings for finding tissue. The target pixel size (`tissue_detection.resolution_um_per_px`) and permitted deviation determine the initial choice of image level. The file also sets the minimum tissue area and the criteria for comparing revised tissue regions.

The `tissue_detection.editor` section supplies the initial threshold, features and fitting settings for the detector editor. For RF and SVM, the starting features are channel intensity and Gaussian smoothing at scales of 1 and 3 detector pixels. Once you choose an image level, **Image detail** expresses these scales in micrometres and passes them to QuPath with the selected channel. We save the channel and image level with the completed detector. [Tissue detection](../tissue/index.md)

### Paired-image alignment

`registration.json` contains the settings for global alignment, local refinement and checks on the completed transformation. The `registration.automatic` section selects the fitting channels, global view size, optimizer updates, thread count, starting orientations and whether automatic local refinement is enabled. `registration.bspline` selects the local view size, updates per level, final control-point spacing in µm, relative bending weight, maximum displacement and numerical inverse settings. The manual editor receives its initial permitted local displacement from `registration.manual`.

Separate sections define the acceptable area, coverage and geometry when importing an alignment or transferring annotations. [Alignment controls and calculations](../alignment/index.md#reference-registration) explains the fitting sequence and the effect of those limits.

### Image preparation and spectral fitting

`unmixing.json` contains the spectral estimator, requested NNLS tolerance, iteration limit, processing-region dimensions and saved pixel type. New image writes use the fixed lossless Blosc/Zstd level 1 codec with byte shuffle. The file also sets the condition-number, cosine-similarity and matrix-rank checks for reference spectra. Its processing limits cover simultaneous calculations, prepared image regions and memory for the working arrays. Calculation precision sets the number format used to fit the spectra. Output precision sets the format used to store the resulting component intensities. [Processing and output settings](../unmixing/index.md#numerical-and-output-settings) explains these choices, and [image storage](../images/storage.md) describes the output layout.

### Cell segmentation

`segmentation.json` contains the initial image-tile size, overlap padding, simultaneous tile count, analysis area and automatic device selection. Install or select the model files in Software setup, then choose the segmentation method and area to process in the segmentation form. Computing selects the processor. The segmentation form’s advanced settings expose the tile size, padding and simultaneous tile count. [Cell segmentation](../cells/index.md) explains how these affect image sampling and execution.

### Cell classification and marker fitting

`phenotyping.json` contains supervised cell-classifier settings and the marker-threshold fitting settings. The threshold section sets histogram bins, the maximum cells sampled for mixture fitting, the random seed and the batch size used to count classifications. Gaussian and Gamma fits have iteration limits, convergence tolerances and numbers of starting fits. The Gaussian section additionally selects shared or separate component variances and a minimum variance relative to the data scale.

For a mixture fit, we propose the intensity at which the increasing marker score reaches `score_cutoff`. The researcher can adjust that intensity during review. The saved decision records the accepted threshold together with the measurement to which it applies. [How we obtain the gate from the fitted curves](../phenotyping/marker-rules.md#finding-the-threshold-from-the-fitted-curves)

### Unsupervised phenotyping

`clustering.json` contains the initial nearest-neighbour count, clustering resolution, random seed and marker-feature weight. The clustering result records the selected parameter values with the participating cells. [Unsupervised phenotyping](../phenotyping/unsupervised.md) explains scaling and how PhenoGraph and FlowSOM use the selected measurements to group cells. The saved UMAP request records its embedding and display settings; [optional UMAP display](../phenotyping/unsupervised.md#optional-umap-display) explains those parameters and the separate plot limit.

### Execution limits

`execution.json` sets the initial number of batch workers, in-flight tasks and tasks sharing a resource. The scheduler combines these limits with the CPU and memory available on the processing computer before starting work. [Memory and thread allocation](memory-and-threads.md#iv-queue-and-thread-allocation) distinguishes these batch limits from the workers and image regions inside one operation.

### Displayed channel names

`display.json` selects whether biomarker labels become displayed channel names.

<a id="example-section-structure"></a>
### Section structure

```json
{
  "schema": "mif-authoring-defaults/v1",
  "sections": {
    "classifier.svm": {
      "model_type": "C_SVC",
      "kernel": "LINEAR",
      "c": 1.0,
      "gamma": null,
      "max_iterations": 1000,
      "termination_epsilon": 1e-6
    }
  }
}
```

*The `classifier.svm` section of `classifier.json` supplies these initial SVM settings. The complete file also contains the other classifier sections.*

Each method's configuration class checks its field names and allowed values. To add a model, implement those checks, fitting, prediction and saved-model format alongside its default section.

<a id="reference-configuration--ii-reading-and-editing-initial-settings"></a>
## Reading and editing initial settings

`default_section("classifier.svm")` reads `classifier.json` and returns the named section. We read each module file on its first use in the process and return an independent copy of its values to the caller. This keeps edits in one form from changing another form's starting values.

We check the JSON for duplicate field names and nonfinite numerical values, including a decimal exponent that exceeds floating-point storage. A section placed in another module's file is also reported at that file's location. Parameter-specific checks then apply when the selected method is constructed.

We read these files from the imported `mif_pipeline` package. The [source-installation commands](../installation-and-build/software.md#install-the-python-application) install copies into the Python environment, so reinstall after editing repository defaults or software descriptions, then restart MIFflow. For a portable application, rebuild the distribution to include the updated files.

To revise a setting already accepted in an analysis, open the stage’s [revision form](../run/index.md#change-a-completed-decision). It shows which results used the previous setting so you can review the work to repeat before applying the change.

An imported model uses the settings saved during fitting. For a random forest these include the tree count, tree depth, feature preparation and class order. A tissue detector likewise retains the smoothing scales, centring and normalization used to calculate its features. Edits to a defaults file apply the next time you set up a model. Where available, the classifier records the defaults used for fitting under `training_defaults`.

Format requirements and model input checks are implemented beside the code that consumes them. For example, the tissue predictor checks the saved feature order before applying a detector. Changing a JSON starting value therefore requires its corresponding settings parser and calculation to support that value.

<a id="reference-configuration--iii-installed-software-descriptions"></a>
## Installed software descriptions

Each supported software component has a JSON description under `src/python/mif_pipeline/config/software/`. It identifies the release, download locations and files required for a valid installation. The component's setup code chooses the local destination, extracts the archive and checks that the installed libraries or programs load.

### InstanSeg extension and model

`instanseg.json` records the QuPath extension and pretrained model separately. The extension entry contains the 0.1.7 JAR download, byte count and checksum. The model entry contains the `fluorescence_nuclei_and_cells` model name, release 0.1.1, archive description and eight required filenames with their checksums. These include the network, model description, preprocessing/postprocessing files and supplied examples. The extension and model can therefore be checked and installed through their separate setup controls.

### Cellpose-SAM and StarDist

`cellpose.json` identifies the Cellpose-SAM checkpoint, its source revision and the associated model files. `stardist.json` identifies the StarDist model and QuPath extension. Both descriptions record download locations, file sizes and checksums, which Software setup uses to verify the selected files. The [segmentation guide](../cells/index.md) describes the outputs and settings of each method.

### Warpy

`warpy.json` records the Warpy 0.5.0 extension JAR, download size and checksum. Its installer first obtains QuPath's configured extension directory and checks existing Warpy copies. A missing extension is downloaded to a temporary directory and moved into that extension directory only after its bytes have been verified.

### Elastix and Transformix

`elastix.json` records the selected Elastix/Transformix 5.3.1 archive for each supported platform, the programs' location within that archive, and the required executable, library and licence files. A supplied existing installation is checked for the complete platform file set and execution of both programs. Automatic registration subsequently passes its image views and fitting parameters to those selected executables.

### QuPath

`qupath.json` records the supported QuPath version and the names, versions and checksums of its included DJL and PyTorch-engine Java libraries. Software setup uses these records when inspecting the QuPath installation we select. The program-specific release description contains the files needed by those checks; the actual executable path is saved in the desktop's software selections.

### PyTorch libraries for QuPath

`pytorch.json` records the Java engine version, the Java/native bridge download and platform-specific CPU library archives. Its file lists identify the required Linux shared libraries or Windows DLLs and their checksums. Software setup uses these records to install the native libraries that InstanSeg and the prepared KRONOS encoder run inside QuPath. The CUDA bridge has a separate checksum for each platform; it connects QuPath to the Torch libraries included in the NVIDIA worker bundle. [NVIDIA processing libraries](../installation-and-build/software.md#nvidia-processing-libraries) explains that installation.

### Java for image reading

`java.json` records the selected Temurin Java 21 distribution, platform archives, exact sizes and checksums, together with release/source/licence locations. After extraction, setup checks the program and JVM library and requires the executable to report Java 21. The combined image-reader check then opens the selected Java and Bio-Formats libraries together.

### Bio-Formats

`bioformats.json` records the ordered 64-JAR image-reader definition and its corresponding downloads. Each required JAR has a dependency coordinate, relative filename and checksum; its download entry additionally supplies the address and byte count. Setup compares the two lists in order before downloading. We retain that order when constructing the Java classpath so the same selected library set is used when opening vendor files.

### Nimbus weights

`nimbus.json` records the V1 checkpoint, its pinned source revision, download size, checksum and licence. Software setup verifies those weights independently of the [bundled Nimbus worker](../installation-and-build/building-and-releasing.md#bundled-nimbus-worker), which contains the inference libraries.

### BioLinkBERT marker descriptions

`biolinkbert.json` records the supported BioLinkBERT-large revision, model licence and six files used to encode new KRONOS marker descriptions. Each file has a download address, byte count and checksum. Software setup verifies these files during installation or selection; encoder preparation uses the selected local copy.

<a id="reference-configuration--reading-a-program-description"></a>
<a id="reading-a-program-description"></a>

## Read and update a program description

`read_software_description("instanseg")` reads the installed JSON by its supported program name. It requires the `mif-software-installation/v1` format and matching `program` field, rejects repeated fields and nonfinite numbers, and limits the file to 256 KiB. We cache each description on first use and return an independent copy to the caller. The program-specific functions then validate their version, download, path and required-file fields.

For Bio-Formats, the required-file section is encoded with sorted JSON keys, fixed separators and a trailing newline when calculating its checksum. That checksum describes the ordered JAR list and Java requirement. The separate download section must point to the same file definition before installation begins.

<a id="reference-configuration--keeping-installation-and-method-settings-together"></a>
<a id="keeping-installation-and-method-settings-together"></a>

### Change a supported software release

We select an installed program in Software setup and choose its analysis parameters at the corresponding processing step. For example, `software/elastix.json` identifies the programs and libraries to install, while `defaults/registration.json` sets the initial affine iterations and B-spline spacing shown in the alignment form.

When updating a supported software release, update its file description and the setup checks that confirm it can run. The method's numerical operations and parameter definitions remain in the processing code; the installation description records the files needed to run that code.

Existing analyses retain the model and processing inputs selected during setup.

<a id="reference-configuration--iv-saved-analysis-settings"></a>
## Saved analysis settings

[Analysis-folder contents](analysis-folder.md#editable-setup-and-recorded-run-inputs) identifies the setup, computing and run-definition files. These retain the samples, image paths and processing choices selected when preparing the run.

As processing progresses, the accepted review for each stage records the settings used to produce its result. For tissue detection, the record identifies the detector, the channel and image resolution it used, and the minimum tissue area retained. For a trained classifier they also identify the subjects, reference annotations, feature preparation and parameter values used for fitting. When you reopen the analysis, the progress record identifies which accepted results to display and which settings subsequent calculations should use.

---

**Continue:** [Analysis setup](analysis.md).
