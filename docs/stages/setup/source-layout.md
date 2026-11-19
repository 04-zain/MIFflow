# Source layout

The desktop and the command line use the same Python service to run an analysis. To trace a setting from a desktop form to a saved result, follow the [form and its submitted request](#desktop-forms-and-processing-requests) into the [stage that performs the calculation](#find-the-code-for-an-analysis-stage). The [processing guide](../run/program-execution.md) explains where Python, Groovy and Java take part.

<a id="source-components-and-integration-points"></a>
<a id="source-locations-and-responsibilities"></a>
<a id="i-setup-and-execution"></a>
<a id="setup-requests-and-saved-execution-state"></a>

## Desktop forms and processing requests

The launch commands are registered in [pyproject.toml](../../../pyproject.toml). `mif-desktop` starts the Qt application. `mif-batch` starts the [batch command-line interface](../../../src/python/mif_pipeline/orchestration/batch_cli.py). Both use the [batch service](../../../src/python/mif_pipeline/orchestration/batch_service.py) to open an analysis, retrieve a waiting review or continue processing after a decision.

**To find a desktop control**, start with the page in [gui/qt](../../../src/python/mif_pipeline/gui/qt). For a waiting review, follow the field name into the code that creates the form and interprets its submitted values. For example, the [marker review widget](../../../src/python/mif_pipeline/gui/qt/marker_gates.py) draws the histogram and lets the researcher move the gate. The [marker review presenter](../../../src/python/mif_pipeline/gui/presenters/marker_gates.py) defines the choices and translates the selected cutoff into the response used by the batch service.

**To find the inputs to a stage**, start with the task in [orchestration](../../../src/python/mif_pipeline/orchestration). During setup, [analysis_setup.py](../../../src/python/mif_pipeline/gui/analysis_setup.py) and [manifest_draft.py](../../../src/python/mif_pipeline/gui/manifest_draft.py) assemble the samples, image locations and method choices that define the run. The [scheduler](../../../src/python/mif_pipeline/orchestration/batch_scheduler.py) determines when a task can start. Its stage executor selects the accepted images, regions, measurements or models and passes them to the calculation. For a task that uses QuPath, the [native runner](../../../src/python/mif_pipeline/qupath_runtime/runner.py) launches the registered [Groovy script](../../../qupath/scripts) with that request.

**To trace a streaming review**, follow the request through the [remote client](../../../src/python/mif_pipeline/orchestration/remote_compute.py) and the [cluster review page format](../../../src/python/mif_pipeline/orchestration/cluster/cluster_review_pages.py). The desktop opens the form in [cluster_reviews.py](../../../src/python/mif_pipeline/gui/qt/cluster_reviews.py). For an action inside that form, check where the cluster dialog sends the request and how it returns the result to the page. Annotation drafts, landmarks and figures have separate transfer routines under [orchestration/cluster](../../../src/python/mif_pipeline/orchestration/cluster).

<a id="i-image-reading-and-spectral-processing"></a>
<a id="ii-image-preparation-and-native-reading"></a>
<a id="image-preparation-and-native-reading"></a>

## Find the code for an analysis stage

### Reading and preparing images

[Source inspection](../../../src/python/mif_pipeline/io/source_facts.py) identifies the series, channels and physical calibration in an image file. Once the researcher selects a series, [reviewed_source.py](../../../src/python/mif_pipeline/io/reviewed_source.py) opens its pixels through the appropriate [image reader](../../../src/python/mif_pipeline/io/backends). To trace an incorrect channel or image level, follow that selection into the request for a specific image region.

To follow a pixel through spectral unmixing, start with the [regional processing engine](../../../src/python/mif_pipeline/preprocessing/unmixing/engine.py). For each region, [normalization.py](../../../src/python/mif_pipeline/preprocessing/unmixing/normalization.py) applies the reviewed intensity corrections before the [solver](../../../src/python/mif_pipeline/preprocessing/unmixing/solvers.py) estimates the component intensities. The grouped, float32 and tensor solvers are in the same directory. To follow those intensities into an image on screen, inspect the [image writers](../../../src/python/mif_pipeline/io/writers) that store the OME-Zarr arrays, then the [Java image reader](../../../src/java/org/mif/image) that opens them in QuPath. [Image layout and compression](../images/storage.md)

<a id="ii-tissue-analysis-regions-and-paired-images"></a>
<a id="iii-tissue-classifiers-and-cell-phenotypes"></a>
<a id="iii-tissue-registration-and-model-execution"></a>
<a id="tissue-registration-and-model-execution"></a>

### Tissue, alignment and classification

| Operation | Where to follow the calculation |
|---|---|
| Detect and review tissue | The [tissue scripts](../../../qupath/scripts/tissue_detection) create or apply the detector and return proposed tissue sections. [Java geometry routines](../../../src/java/org/mif/geometry) match sections and calculate their quality measures. The [tissue detection module](../../../src/python/mif_pipeline/tissue_detection) records the detector and reviewed sections. |
| Select analysis regions | The [QuPath scripts](../../../qupath/scripts/analysis_domain) read the boundaries selected on the image. The [analysis domain module](../../../src/python/mif_pipeline/analysis_domain) identifies the areas that subsequent stages may use. |
| Align a paired image | The [registration module](../../../src/python/mif_pipeline/registration) handles the fitting and transfer requests. Follow the [registration scripts](../../../qupath/scripts/registration) for Elastix and Transformix calls, [RegistrationImageViews](../../../src/java/org/mif/image/RegistrationImageViews.java) for the fields used in fitting, and the [Java registration code](../../../src/java/org/mif/registration) for landmark transforms and geometry checks. |
| Train and apply a classifier | [Batch training](../../../src/python/mif_pipeline/orchestration/batch_training) organizes the references and subject allocation. The [classifier scripts](../../../qupath/scripts/compartment_analysis) pass training data to the [Java classification code](../../../src/java/org/mif/classification), which prepares the features, fits the OpenCV model and calculates predictions and evaluation scores. |
| Use KRONOS features | The [encoder preparation](../../../src/python/mif_pipeline/compartment_analysis/kronos_encoder.py) selects the marker panel. The [Java patch provider](../../../src/java/org/mif/inference/MifKronosPatchProvider.java) extracts features using the fixed encoder. For the tissue graph, follow the saved patch features into [graph training](../../../src/python/mif_pipeline/compartment_analysis/graph_training.py) and the fitted graph into [GraphHeadKernel](../../../src/java/org/mif/inference/GraphHeadKernel.java). |

<a id="v-biological-structure-checks-and-exports"></a>
<a id="iv-cells-and-quantitative-results"></a>
<a id="cells-and-quantitative-results"></a>

### Cells, phenotypes and secondary structures

**Segmentation.** For InstanSeg, begin with the [stage script](../../../qupath/scripts/cell_analysis/04_run_instanseg.groovy) and follow the tile detections into the [Java segmentation code](../../../src/java/org/mif/segmentation), which reconciles outlines where tiles overlap. Cellpose-SAM and StarDist enter through the [alternative segmentation script](../../../qupath/scripts/cell_analysis/alternative_segmentation.groovy). Cellpose runs in a [Python worker](../../../src/python/mif_pipeline/cell_analysis/cellpose_worker.py) and StarDist runs through its QuPath extension.

**Cell measurements.** The [measurement plan](../../../src/python/mif_pipeline/cell_analysis/measurement_plan.py) identifies the columns to calculate. The [measurement script](../../../qupath/scripts/cell_analysis/05_prepare_phenotyping_measurements.groovy) supplies the image, cell outlines and selected measurements to Java. [MeasurementRows](../../../src/java/org/mif/cells/MeasurementRows.java) calculates the per-cell values, including marker intensities and measurements derived from cell or nuclear geometry. [PopulationMembership](../../../src/java/org/mif/cells/PopulationMembership.java) identifies the regions to which each cell belongs.

**Cell phenotyping.** Start in [batch_phenotyping](../../../src/python/mif_pipeline/orchestration/batch_phenotyping) to see which cells and measurement columns enter a fit. Then follow the selected method.

- For marker rules, [thresholds.py](../../../src/python/mif_pipeline/cell_analysis/thresholds.py) fits the gates and [marker_rules.py](../../../src/python/mif_pipeline/cell_analysis/marker_rules.py) applies the ordered rules. For Nimbus, the [image script](../../../qupath/scripts/cell_analysis/nimbus_image_source.groovy) supplies marker pixels and cell masks for scoring.
- For supervised phenotyping, the [reference script](../../../qupath/scripts/cell_analysis/supervised_references.groovy) matches each labelled point to a cell. [supervised.py](../../../src/python/mif_pipeline/cell_analysis/supervised.py) uses those cell IDs to prepare the training data. For image-based features, the [KRONOS cell script](../../../qupath/scripts/cell_analysis/kronos_cell_features.groovy) requests physically calibrated crops and masks from [CellImageSamples](../../../src/java/org/mif/image/CellImageSamples.java).
- For unsupervised phenotyping, [clustering.py](../../../src/python/mif_pipeline/cell_analysis/clustering.py) prepares the selected features. [clustering_methods.py](../../../src/python/mif_pipeline/cell_analysis/clustering_methods.py) implements the PhenoGraph and FlowSOM fitting routes. Follow the fitted groups into [cluster_review.py](../../../src/python/mif_pipeline/cell_analysis/cluster_review.py) to trace a reviewed assignment.

**Secondary structures.** The [Java region code](../../../src/java/org/mif/roi) calculates geometry and cell membership for the detected regions. [roi_validation](../../../src/python/mif_pipeline/roi_validation) evaluates the selected criteria. The [region review scripts](../../../qupath/scripts/roi_validation) collect the evidence and save the accepted regions and their membership.

<a id="iv-qupath-and-python-execution"></a>
<a id="v-shared-dataset-services"></a>
<a id="shared-dataset-services"></a>

## Follow a saved result

For cell tables, [cell_store.py](../../../src/python/mif_pipeline/datasets/cell_store.py) writes batches of cells to Parquet and builds the index for looking up a cell by ID. To trace which table a later stage read, follow the [saved dataset selection](../run/saving-and-recovery.md#iv-selecting-a-complete-dataset). It identifies the version the coordinator accepted after the writer completed the dataset.

To identify **which result a stage used**, start with [batch_state.py](../../../src/python/mif_pipeline/orchestration/batch_state.py). The saved `state/head.json` identifies the current plan and selected results. [batch_history.py](../../../src/python/mif_pipeline/orchestration/batch_history.py) records changes to that state. [batch_progress.py](../../../src/python/mif_pipeline/orchestration/batch_progress.py) produces the `state/current-progress.json` summary shown on Run. [Files in an analysis folder](analysis-folder.md#processing-progress-and-accepted-results)

To inspect **the values in a cell result**, follow the selected dataset into [datasets/collection.py](../../../src/python/mif_pipeline/datasets/collection.py). It opens the cell outlines together with accepted edits and the requested measurement or phenotype columns. Cell IDs link those records within a segmentation result. A whole-population calculation can read the requested columns in batches. The [Java cell reader](../../../src/java/org/mif/cells) uses the same IDs and field definitions when QuPath requests the outlines and labels in the displayed region. For an export, [exports.py](../../../src/python/mif_pipeline/orchestration/exports.py) and [spatial_handoff.py](../../../src/python/mif_pipeline/cell_analysis/spatial_handoff.py) identify the datasets that the recipient can open. [Reading selected cells and columns](../../reference/cell-storage.md)

<a id="vi-packaging-and-documentation"></a>
<a id="script-preparation-and-packaging"></a>
<a id="vii-configuration-and-saved-results"></a>
<a id="vii-initial-settings-and-saved-results"></a>
<a id="defaults-and-worker-compatibility"></a>

## Change a default, script or installed component

For the initial value on a new form, look in [config/defaults](../../../src/python/mif_pipeline/config/defaults), loaded by [_defaults.py](../../../src/python/mif_pipeline/_defaults.py). To change which external release Software setup locates or installs, use [config/software](../../../src/python/mif_pipeline/config/software), [_software.py](../../../src/python/mif_pipeline/_software.py) and the [setup routines](../../../src/python/mif_pipeline/orchestration/setup). An existing analysis keeps the settings it has already saved. [Analysis and software settings](configuration.md)

For a QuPath script, edit the source under [qupath/scripts](../../../qupath/scripts). The [script registry](../../../src/python/mif_pipeline/qupath_runtime/_script_asset_registry.py) identifies the packaged entry points. Before running a project stage, we add the [shared startup checks](../../../qupath/scripts/general/project_stage_bootstrap.groovy) and save the prepared script and its checksum with the run. If you launch MIFflow from an installed package, [reinstall after changing a script](../installation-and-build/software.md#update-an-existing-source-installation) so the runner loads that change.

Changes to the Java classes or their [service registrations](../../../src/java-resources) require [rebuilding the extension](../installation-and-build/native-extension.md). For changes to the distribution itself, follow [setup.py](../../../setup.py) for the Python package and [packaging](../../../packaging) for the portable editions. The [build guide](../installation-and-build/building-and-releasing.md) describes how to check the installed programs and resources.

For a worker startup or resource issue, follow the task allowance into the [process controls](../../../src/python/mif_containment) and [QuPath runtime](../../../src/python/mif_pipeline/qupath_runtime). The launch settings limit memory, threads and caches. The caller also checks the worker's release and protocol version before sending it a calculation. [Memory during processing](memory-and-threads.md) · [How programs exchange work](../run/program-execution.md)
