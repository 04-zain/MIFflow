# Python interfaces

[Building and integration](../../../README.md#building-and-integration) · [Source organization](source-layout.md)

<a id="reference-python-api"></a>

<a id="reference-python-api--python-interfaces"></a>

Use the image and calculation interfaces below when working with pixels or cell measurements in Python. To run a stage in an existing analysis or submit a review, use the batch service. It checks the saved analysis state and records the result.

<a id="reference-python-api--i-metadata-review-and-source-reading"></a>
## Metadata review and source reading

### Inspect and select a series

Use `io.inspect_source_series_inventory()` to list the image series in a source file. Select the series containing the tissue to analyse. The entry lists its dimensions and stored resolutions. Keep the original series index and OME identifiers with your selection so subsequent reads use the same image.

Call `io.canonical_ingestion.selected_canonical_ome()` to construct the OME description of the selected series. Channel review adds the component and marker assignments and any imaging settings supplied by the researcher. The prepared OME-Zarr image is written later, when regional processing reads the original pixels and applies the reviewed corrections.

### Open the selected pixels

Open the reviewed series with `io.reviewed_source.open_reviewed_source()` inside a `with` block. Within that reader the selected series is numbered zero, regardless of its index in the original file. Request the region in the reviewed image’s coordinates to obtain one array indexed by band, row and column. Leaving the block closes the reader. [Reading and processing image regions](../images/processing.md#reference-image-processing)

<a id="reference-python-api--ii-spectral-calculations"></a>
## Spectral calculations

<a id="prepare-acquisition-corrections-and-the-spectral-solver"></a>
### Prepare intensity corrections and the spectral solver

`preprocessing.unmixing.normalization.CanonicalOffsetExposureNormalization.build_factors()` uses the reviewed exposure, detector and signal-range metadata to prepare per-band divisors, any applied offsets and response multipliers. `normalize_region()` applies each band's factors across its rows and columns, returning an independent float32 array of the same shape. The accepted full, exposure-only or as-stored correction determines which factors are applied; [intensity correction](../unmixing/index.md#acquisition-correction) explains those choices. The source pixels and reusable factors remain unchanged.

Arrange the reference spectra with one row per measured band and one column per component. For each image block, supply the measured intensities in the same band order, with one column per pixel. The solver estimates a coefficient for each component at each pixel.

- `preprocessing.unmixing.solvers.ClippedLeastSquaresSolver` calculates the pseudoinverse of the reference matrix once, applies it to each image block and sets negative coefficients to zero.
- `BatchedNnlsSolver` requires nonnegative coefficients during fitting and reports whether the fit met the selected convergence criterion.

Both return the component-by-pixel coefficients and diagnostics for the calculation.

`tensor_solver.create_spectral_solver()` selects the implementation supporting the requested estimator, calculation precision and processor.

### Process a prepared source or run the complete image operation

`preprocessing.unmixing.engine.process_regions_to_zarr()` accepts an open regional source, validated image settings, a spectral basis, normalization factors, a prepared solver and the destination generation. It corrects and fits image regions, multiplies the fitted coefficients by the accepted component reporting factors, and writes the resulting intensities and pyramid levels. The returned `ProcessingSummary` records the staged array location, completed region and level counts, reporting weights and solver diagnostics. The enclosing caller retains responsibility for closing the source reader.

`preprocessing.unmixing.workflow.run_unmixing()` opens the selected image using its saved inspection and reviewed metadata, applies the accepted intensity correction and spectral reference, and processes image regions into the completed component image. The workflow retains the convergence report with the image operation. [NNLS convergence](../unmixing/index.md#nnls-convergence) explains the criterion and review action for regions that do not converge.

The equations, solver paths and parameter meanings are in [Spectral unmixing](../unmixing/index.md#reference-unmixing).

<a id="reference-python-api--iii-marker-thresholds-and-rules"></a>
## Marker thresholds and rules

Pass aligned cell measurements, cell IDs, section and compartment labels to `cell_analysis.thresholds.build_threshold_review()`. Supply a `ThresholdFitPolicy` to set the fitting parameters. A cell with a nonfinite measurement has an unknown result for that marker and is excluded from the fit and histogram.

By default the review compares manual, triangle, two-component Gaussian and two-component gamma thresholds. It returns the candidates, histograms, counts of cells with finite measurements, IDs of the cells sampled for fitting and IDs of example cells. Use `methods` to select particular methods. An empty sequence opens manual and distribution review without fitting a mixture.

`accept_threshold()` records the selected threshold and any replacement thresholds for individual tissue sections. Pass this choice to `cell_analysis.marker_rules.apply_threshold_rules()` with the same measurement definition used for fitting. The function compares each cell’s measurement with the threshold in the selected direction and returns its marker state.

Pass the marker states and [ordered phenotype rules](../phenotyping/marker-rules.md#write-phenotype-combinations) to `apply_ordered_composites()`. Each cell receives the phenotype of the first rule it satisfies, or the residual phenotype if it matches none. Keep the measurements and IDs in the same cell order so each assignment can be linked back to its cell.

<a id="reference-python-api--iv-kronos-and-learned-graph-features"></a>
## KRONOS and learned graph features

`compartment_analysis.kronos_panel.KronosPanel` records the marker order and intensity preparation used to turn an image patch into encoder input. `kronos_encoder.inspect_kronos_package()` checks the selected pretrained package, and `kronos_native.export_native_encoder()` prepares its native deployment with the accepted panel. Java's [FrozenEncoder](../../../src/java/org/mif/inference/FrozenEncoder.java) applies the prepared model to patches of the saved size (32, 64, 128 or 256 pixels per side), returning a 768-value feature vector for each patch.

`compartment_analysis.patch_graph.PatchGraph` connects patches immediately left, right, above and below when their image, accepted region, tissue component and training/evaluation identifiers agree. `graph_training.train_mean_self()` uses the patch vectors, permitted neighbours and reference labels to fit a layer that combines each patch's own vector with the mean of its neighbours. A temporary linear classification layer supplies the class scores used in the cross-entropy training loss. `graph_features.FrozenMeanSelf` retains the fitted self and neighbour matrices and bias for later feature calculation.

In QuPath, `MifKronosPatchProvider` gathers those encoder vectors and [GraphHeadKernel.features()](../../../src/java/org/mif/inference/GraphHeadKernel.java) applies the saved graph parameters to produce spatial-feature rows. The native RF/SVM fitter applies the selected normalization to these rows and fits the tissue classifier; prediction uses the same saved graph, feature order and fitted normalization.

The final native fitter and patch-level prediction adapter are registered Groovy scripts under `qupath/scripts/compartment_analysis/`. Their feature normalization, class mappings and saved model fields are part of the complete classifier. See [KRONOS classification](../tissue-classification/kronos.md#reference-kronos-classification) and [Pixel classification](../tissue-classification/index.md#reference-pixel-classification) for the full model construction.

<a id="reference-python-api--v-supervised-cells-and-classifier-evaluation"></a>
## Supervised cells and classifier evaluation

When you add or move a reference point in QuPath, the [reference importer](../phenotyping/supervised.md#match-point-references-to-cells) matches it to an eligible cell and saves that cell’s ID with the point. In Python, `cell_analysis.supervised.map_point_evidence()` uses the saved ID to retrieve the cell. The result includes both positions and the distance between them, so you can inspect where the reference was placed relative to the cell.

Before evaluation, `validate_training_holdout()` checks that training and held-out references share neither cell IDs nor reference-region IDs. During prediction, the native stage checks the model against the selected cell population, measurements, reference classes and training or held-out roles. It returns predictions for review. To prepare the review metadata, Python requests the cell IDs and analysis-region IDs.

`cell_analysis.supervised_qc.evaluate_predictions()` matches reference and predicted labels for the complete declared held-out cell set. It returns the confusion matrix, full reference-class support, precision, recall, F1, accuracy and rejected-cell IDs. Rejected cells remain in recall and accuracy denominators.

The supervised stage reads cells in explicit batches and saves prediction columns in the same row order. Training folds, held-out evaluation and model application all pass their selected batches directly to the classifier. The generic QuPath overloads that expect all cells in the project hierarchy report an explicit-batch requirement, because automatic cells are read from their Parquet dataset.

`compartment_analysis.classifier_qc.reduce_classifier_confusion()` combines complete reference/prediction observations into pooled, image and subject matrices. `classifier_metrics_from_confusion()` calculates their scores using the same declared class order. `evaluation_plots.render_classifier_confusion_figure()` encodes the saved region counts and within-reference-class percentages as PNG, PDF or SVG bytes for display/export.

[Training and evaluation](../tissue-classification/index.md#reference-training-and-evaluation) gives the matching equations, reference checks, confidence rule and the distinct subject-weighted parameter-selection calculation.

<a id="reference-python-api--vi-analysis-and-batch-operations"></a>
## Analysis and batch operations

`tissue_detection` implements detector setup, native-resolution selection and accepted tissue regions. `analysis_domain` handles selected analysis regions. `registration` handles paired-image transformations and review. `cell_analysis` provides measured-cell phenotyping and clustering. `roi_validation` evaluates candidate structures against the selected geometry, cell-count, phenotype and manual-review rules. `datasets` reads and writes the spatial cell tables and selected result components; `cell_analysis.spatial_handoff` records those selections with the accepted analysis regions and structure memberships.

### Open a selected cell dataset

Use `datasets.collection.CellDatasetCollection.from_selection()` to open the segmentation datasets with their selected measurements, classifications and outline edits. We check the metadata and summary counts, then load the row-group layout and category dictionaries within the reader’s memory allowance.

If the selection includes outline edits, we read them in batches and check that their cell IDs occur in the segmentation dataset. We build a temporary index on disk so later reads can retrieve the edits for the cells in each batch.

Use `iter_batches(columns=...)` to process selected measurements a batch at a time. For each batch, the reader loads the required columns from Parquet into memory. It uses the cell IDs to join accepted results and apply outline edits, then yields a PyArrow `RecordBatch` containing the columns you requested.

To include only cells from particular analysis regions, call `select_domains(domain_ids)` first. The selection keeps the same source datasets. During reading, we also load `analysis_domain_id` to identify the cells that belong to those regions.

If you already have the cell IDs, use `lookup_ids(ids, columns=...)`. The reader uses its ID indexes to locate the relevant row groups and returns the requested columns in batches. The list of IDs in one request must fit the reader’s batch allowance.

If you have an image field instead, use `query(bounds, columns=..., predicate="intersects")` with `(min_x, min_y, max_x, max_y)` in full-resolution pixels. The reader first finds row groups that overlap the rectangle. It then reads the fields required to test the individual cells in those groups. With the default predicate, a cell qualifies if any part of its outline intersects the field. Use `"centroid"` to include only cells whose centre lies in the field, or `"bounds"` to test each cell’s bounding rectangle. In all three cases, the returned Arrow batches contain the columns you requested for the matching cells.

### Publish and reopen a spatial handoff

`cell_analysis.build_spatial_handoff()` combines this selection with tissue sections, analysis regions, phenotype results and final structure-review decisions. It uses saved region and tissue counts without constructing a Python object for every cell.

`orchestration.exports.write_spatial_handoff()` saves a new export version containing the selected dataset references and identity summaries. It then updates the file that selects this export version. `load_spatial_handoff()` opens the selected metadata and collection, including any saved-edit index described above. Base measurements and outlines are read when a calculation requests them. `handoff.iter_cell_records()` reads complete cell records, including geometry, and checks their fields and associations as they are requested. Close `handoff.cell_dataset` when its readers are no longer needed.

For a saved export, copy the handoff path from Results and use it when calling `load_spatial_handoff`. Request the fields needed by the calculation. Measurement names retain their exact native feature names, as in the example below.

```python
from mif_pipeline.orchestration.exports import load_spatial_handoff

handoff = load_spatial_handoff("/path/from/Results/spatial-cell-handoff.json")
try:
    for batch in handoff.cell_dataset.iter_batches(
        columns=("cell_id", "x_um", "y_um", "measurement:Cell: PDL1: Mean")
    ):
        calculate_from_selected_columns(batch)
finally:
    handoff.cell_dataset.close()
```

*Replace the handoff path and measurement name with those from your result. Inside the loop, replace `calculate_from_selected_columns(batch)` with the calculation to perform on that batch of cells.*

The example reads cell IDs, calibrated centres and mean PDL1 intensities into memory for one batch at a time. If you request a categorical field such as a phenotype, the batch stores its values as Int32 codes. Look up a non-null code in `handoff.cell_dataset.dictionaries[field].labels[code]` to obtain its label. If you need complete cell records, `handoff.iter_cell_records()` returns them with the labels decoded.

When transferring a handoff to another computer, include the base dataset, selected result tables and edits. The selection files identify each dataset by its absolute path, so it must be accessible at that path on the destination computer.

### Open an analysis service and submit a review

`orchestration.batch_service.open_batch_service_provider()` is a context manager for one analysis workspace. Resolve that workspace with `BatchWorkspaceLayout.resolve(output_root, batch_id)`, then use `provider.for_workspace(output_root, batch_id)` to reopen it or `provider.for_manifest(manifest_path)` to open its manifest. The lookup must match the provider's workspace. Leaving the `with` block releases its readers, native processes and processing sessions.

Read `service.status(batch_id).state_head_sha256` to identify the current version of the analysis. Then call `service.list_interactions(batch_id, BatchInteractionQuery(offset=0, limit=50))` for the waiting reviews and their targets. If the response supplies a `next_offset`, use it to read the next page. After the last page, check the analysis version again. If it changed during the listing, repeat the listing so the choices you present belong to the same version. The [CLI review listing and response envelope](command-line.md#reviews-and-retry) expose the same identifiers and selection modes.

The helper below submits a reviewed response document with the six fields defined in the CLI guide. The caller supplies the existing output root, analysis ID, selected QuPath executable and completed response object. Optional runtime arguments, such as the selected QuPath launch binding or native-library root, are passed through to the provider.

```python
from pathlib import Path

from mif_pipeline.orchestration.batch_interactions import (
    BatchInteractionResponseRequest,
    BatchTargetSelection,
)
from mif_pipeline.orchestration.batch_service import open_batch_service_provider
from mif_pipeline.orchestration.batch_state import BatchWorkspaceLayout


def submit_review(output_root, batch_id, qupath_executable, document, **runtime_options):
    output_root = Path(output_root)
    request = BatchInteractionResponseRequest(
        group_id=document["group_id"],
        group_sha256=document["group_sha256"],
        selection=BatchTargetSelection(**document["selection"]),
        action=document["action"],
        response=document["response"],
        expected_state_head_sha256=document["expected_state_head_sha256"],
    )
    layout = BatchWorkspaceLayout.resolve(output_root, batch_id)
    with open_batch_service_provider(
        workspace_layout=layout,
        qupath_executable=Path(qupath_executable),
        **runtime_options,
    ) as provider:
        service = provider.for_workspace(output_root, batch_id)
        return service.apply_interaction_response(batch_id, request)
```

Inspect the returned `applications`, each of which reports acceptance or rejection for its target. The service checks the current state, group identity, offered action and stage-specific answers before recording an accepted decision. A stale-state exception requires listing the review again.

After the response is accepted, call `service.resume(batch_id)` within an open provider when ready to continue processing. [The service implementation](../../../src/python/mif_pipeline/orchestration/batch_service.py) defines the available runtime arguments and methods. [Batch execution](../run/execution.md#reference-batch-execution) describes shared classifier fits, sample progression and saved pause/resume behavior.

<a id="reference-python-api--vii-defaults-program-compatibility-and-native-scripts"></a>
## Defaults, program compatibility and native scripts

`_defaults.default_section()` reads a named group of starting values from its module-specific JSON file. For example, `classifier.svm` selects the SVM section of `config/defaults/classifier.json`. New settings are checked by their corresponding settings classes and later saved explicitly with the analysis.

`_software.read_software_description()` reads the selected installed program definition under `config/software` and returns an independent copy of its version, download and required-file fields. Program-specific setup operations validate these fields and use them when inspecting or installing the component. [Analysis and software settings](configuration.md#reference-configuration) identifies the installed-program descriptions and their setup operations.

`_release.py` contains the numerical release and message-format identifiers. Program initialization compares these values and the required installed file locations before calculations begin.

`qupath_runtime` resolves the selected QuPath installation, stages registered scripts, supplies exact project/image requests and reads their completed results. Use those interfaces or the batch service when adding an operation that changes the project, so the saved project changes and accepted analysis result remain associated with the same request.

---

**Continue:** [Source organization](source-layout.md).
