# Saved image, model and cell data

<a id="reference-data-formats"></a>
<a id="reference-data-formats--saved-image-model-and-cell-data"></a>

[Results and export](../stages/results/index.md) · [Additional figures](../stages/results/figures.md) · [Cell tables and spatial queries](cell-storage.md)

Use this reference to choose a file for downstream analysis and interpret its values, units and selected population. Complete cell results use linked Parquet tables; figure exports contain the values or image arrays used in a selected view.

For per-cell analysis, open the **Cell-data reference** listed in [Results & export](../stages/results/index.md#locate-the-final-outputs). It identifies the outlines, measurements and phenotype assignments belonging to the accepted result. [Cell datasets and spatial queries](cell-storage.md) describes how readers combine those files.

**On this page:** [Choose a file](#choose-the-files-for-your-analysis) · [Read complete cells](#input-files-for-spatial-analysis) · [Coordinates and populations](#coordinates-units-and-selected-populations) · [Region results](#classified-region-tables) · [Figure data](#figure-data).

## Choose the files for your analysis

| Data needed | Saved representation | Where to read more |
|---|---|---|
| Prepared image | OME-Zarr directory containing the full-resolution channels, lower-resolution levels and image metadata | [Image storage](../stages/images/storage.md) |
| Cell outlines and measurements | Parquet datasets selected by the cell-data reference | [Read exported cells](#input-files-for-spatial-analysis) |
| Tissue and structure regions | `rois.parquet`, with outlines, classes and review decisions | [Region tables](#classified-region-tables) |
| Fitted model or image alignment | Model files and descriptions of their inputs, parameters and class or coordinate mappings | [Models and transformations](#fitted-classifiers-and-registration-transforms) |
| Values shown in a figure | CSV tables; NumPy archives for tissue fields, cell-gallery pixels/outlines and reconstruction image/residual arrays | [Export figure data](../stages/results/index.md#open-and-export-figures) |

<a id="reference-data-formats--v-input-files-for-spatial-analysis"></a>
## Input files for spatial analysis

The **Cell-data reference** in Results identifies `spatial-cell-handoff.json`. Open this file to read the selected cells, measurements, phenotype assignments and accepted manual edits together. It locates the selected export's `descriptor.json`, which identifies the collection in `cells/dataset.json`. The collection specifies which tissue-section datasets, measurement tables, phenotype tables and outline edits belong to that result:

```text
spatial-cell-handoff.json
    → selected export/descriptor.json
        → cells/dataset.json
            → section cell datasets
            → selected measurements and phenotype tables
            → accepted cell edits
```

The JSON descriptions identify the files to combine; the per-cell values remain in Parquet. Reads exclude deleted cells and cells from regions excluded during review. Region records retain excluded regions and regions with zero cells, allowing the exported population to be compared with the accepted region decisions. A tissue section excluded entirely by primary classification records zero exported cells and that cell analysis was skipped.

### Read the selected columns in Python

Use `orchestration.exports.load_spatial_handoff()` to open the reference, then inspect `cell_dataset.schema` for the available column names. Calling `cell_dataset.iter_batches(columns=...)` decodes the requested columns from Parquet row groups into bounded Arrow batches in RAM. The reader also loads the cell IDs needed to join measurements and classifications, and the region IDs needed to select included cells. After joining and filtering, it returns the requested columns as an Arrow `RecordBatch`. [Python table reads](cell-storage.md#python-table-reads) explains these selections and the additional fields needed for a spatial query.

To calculate a marker's intensity distribution, request its mean-intensity column and add each batch's values to the histogram before advancing. Request `cell_id` in the same call when the calculation must retain which cell supplied each value.

Categorical columns contain integer codes. Resolve them using `cell_dataset.dictionaries`, or use `handoff.iter_cell_records()` for complete records with decoded labels. A category code, including zero, has the meaning assigned by its dictionary. [Category dictionaries](cell-storage.md#vi-category-dictionaries) explains this representation; the [Python reader example](../stages/setup/python-api.md#publish-and-reopen-a-spatial-handoff) shows the calls used to read it.

### Keep the linked datasets available

Keep `spatial-cell-handoff.json`, the selected export directory and the underlying section datasets together with their JSON descriptions and dictionaries. The handoff locates its export directory through relative paths, but the collection identifies section datasets by absolute path. Those dataset locations must therefore remain accessible when opening a copied handoff. For reopening the desktop analysis, retain the [analysis and processing folders](../stages/results/index.md#revisions-and-retention), which also contain setup and saved decisions.

<a id="file-identifiers-and-selection-checks"></a>
### Format identifiers

The reference file uses format `spatial-cell-handoff/v4`, and the cell collection uses `mif-cell-dataset-collection/v1`. These identifiers tell the reader which file structure to expect. The reference also records the SHA-256 checksum of the selected export's descriptor; that descriptor records the collection checksum and selected cell count. On opening the reference, we verify these checksums and the recorded cell selection so that the returned tables belong to the same saved result.

The accompanying `analysis-provenance.json` uses `roi-analysis-provenance/v2` to record region-processing history and the accepted output files. We finish writing the selected data before updating the reference that opens them; [Completing and selecting a dataset](../stages/run/saving-and-recovery.md#select-a-complete-dataset) explains this sequence. The [format registry](../../src/python/mif_pipeline/pipeline_core/data/record-formats.json) lists the identifiers recognized by the application.

## Coordinates, units and selected populations

Cell and region outlines are stored as Well-Known Binary (WKB) geometry in the full-resolution mIF image grid, with X increasing to the right and Y downward from its upper-left corner. The physical pixel width and height calibrate the two axes separately: `(x, y)` converts to `(x × pixel_width_um, y × pixel_height_um)`, and pixel area converts to µm² by multiplying by both pixel sizes. The cell fields `x_level0` and `y_level0` use image pixels; `x_um` and `y_um` use the corresponding physical coordinates. GeoParquet records `crs: null` because these are image coordinates with their own calibration.

Image and figure archives record their sampling separately. Tissue-field outlines use absolute µm; gallery outlines and crop extents use µm relative to each cell centre. The [field](#tissue-field-pixels-and-outlines) and [gallery](#gallery-pixels-and-outlines) array tables specify axis order and coordinate origin. Measurement definitions retain their units and cellular region, so a nuclear intensity, whole-cell intensity and geometric area remain distinguishable when exported together.

The complete cell dataset contains the selected, included cells. A fitted clustering table contains the cells used in that fit; a UMAP or gallery export contains its selected cells; a histogram or composition table contains aggregates. Region tables also retain excluded regions and regions with no exported cells. Use the selection and count fields attached to each output when comparing it with another export. [Region counts and denominators](../stages/results/figures.md#reconcile-regions-and-denominators) explains the corresponding percentages and density calculations.

<a id="reference-data-formats--iv-cell-measurements-and-phenotype-results"></a>
## Cell measurements and phenotype results

A completed segmentation creates the base cell dataset: cell IDs, centroids, region associations, and cell and nucleus outlines. Separate Parquet tables hold the requested measurements as Float64 values. Each measurement's description identifies its channel, marker where applicable, cellular region, measure and units.

Marker rules, supervised classification and reviewed clusters can each produce phenotype tables for those same cells. The selected result determines which phenotype columns accompany the shared geometry and measurements. A supervised result retains the predicted class and model score, together with the accepted assignment and whether the confidence cutoff rejected that prediction.

For unsupervised results, **Cluster memberships and lineage** exports each fitted cell's original, immediate-parent and current cluster IDs with its image and cell identity. The group-review export records naming and inclusion decisions separately: `review_assignments.csv` lists named, unnamed and excluded groups, their cell counts and percentages of all clustered cells; `reporting_groups.csv` records the combined phenotype groups. The final cell dataset retains the accepted per-cell assignments.

When combining samples or segmentations, identify each cell by `(sample_id, image_entry_id, population_id, cell_id)`. Here, `population_id` identifies the segmentation population. Sample, image and population context can be stored in dataset and section metadata rather than repeated in every returned row. Within one population, the tables use cell IDs or their saved row order to associate measurements and labels with the correct outlines. [Cell-table structure](cell-storage.md#i-dataset-structure-and-keys) describes those associations and the integer codes used for repeated category names.

## Classified region tables

After primary or secondary tissue classification, a table under `roi-inventories` connects each classified region to its QuPath annotation. It contains the region ID, annotation ID, saved annotation revision and class ID, sorted by region ID. The annotation supplies the editable outline and parent association. A table with no rows records a classification that produced no regions.

The final `rois.parquet` contains one row per exported region, including its class, parent, analysis-region association and review decisions. Current outlines and retained original predictions use **Well-Known Binary (WKB)**, which encodes the coordinates of polygon boundaries, holes and multipart regions. The table also retains the classifier that produced the region, confidence when available, criterion results and alignment-quality details.

GeoParquet metadata identifies both geometry columns and declares `crs: null` for their full-resolution image coordinates. The [coordinate conventions](#coordinates-units-and-selected-populations) above give the distance and area conversions. [Region counts and denominators](../stages/results/figures.md#reconcile-regions-and-denominators) explains how those outlines relate to cell counts.

<a id="reference-data-formats--vi-secondary-structure-results"></a>
## Secondary-structure results

Use **Final structure outcomes** to identify the structures included in the exported population. Inclusion requires `final_disposition == active_included` and `domain_status == active` for the containing analysis region. The table retains every saved region's outcome, including structures removed by review or count selection.

**Final secondary cell memberships** contains one row for each exported cell–included-structure pair, with its parent analysis-region and tissue-section identities. **Final structure outcomes** records the number of these rows for each region as `final_member_cells`. An included structure can have zero members; the disposition and domain status identify whether a zero-member region belongs to the final selection. A cell in two overlapping structures contributes one membership row to each. To count unique cells across those structures, combine their membership rows and count each complete `(sample_id, image_entry_id, population_id, cell_id)` identity once. The [saved-outcome table](../stages/results/figures.md#secondary-results) explains how these counts appear in the viewer.

The count-decision table records how many eligible structures were observed, the chosen minimum and maximum, and how many were finally included. An absent maximum means no upper limit; a minimum of zero allows the analysis region to contain no eligible structures. When a selection method was used to choose structures under a maximum, the table records it; a blank method means none was recorded. [Structure review](../stages/secondary-structures/index.md#user-guide-structure-review) explains how to interpret those decisions.

The region table records acceptance or rejection in `review_status`, the chosen subtype in `semantic_class_id`, and an optional observation in `review_rationale`. When the subtype is blank, use `semantic_classification_status` to distinguish **Cannot assess**, a rejected region and a structure with no subtypes; it also records when a subtype was assigned. Classifier background remains background even when its raw review status is `needs_review`; use the final disposition to interpret its outcome.

Criterion measurements describe the cells associated with the candidate when it was reviewed. Its candidate-cell count can therefore remain recorded after rejection, while its final membership count is zero. **Recorded criterion measurements** retains the value, unit, comparison outcome and unavailable reason for each recorded measurement.

To read those selected values, follow `semantic.result_artifacts` in `analysis-provenance.json` to the recorded `semantic-result-set/v1` JSON files. Match each entry in `semantic_results` to the region table using its `roi_id`, then read its `measurement_results`: the value, unit, selected measurement, summary statistic and any threshold comparison. Measurements chosen only for observation are also retained. Read `outcome` and `unavailable_reason` alongside the value to distinguish an unavailable measurement from a measured zero.

<a id="reference-data-formats--ii-processed-images-and-channel-descriptions"></a>
## Processed images and channel descriptions

The prepared image is an OME-NGFF 0.5/Zarr v3 directory containing one selected two-dimensional image and its resolution pyramid. Each level retains the channel order and identities. The metadata records the pixel type, physical pixel width and height, fluorophores and reviewed marker assignments.

For an already aligned image, channels that were previously unmixed or acquired in isolated measurement bands retain their source pixel type, intensities and order during preparation. The saved metadata retains the source's spectral history and the reviewed reason for copying. For isolated measurement bands, it also records the one-to-one component and marker assignments. When MIFflow registers separate cycle files, we first interpolate their pixels onto the reference grid and crop them to the accepted common area, as described under [Register separate cycle files](../stages/cyclic-if-methods/index.md#register-separate-cycle-files).

Spectral unmixing saves the selected output type and records the source image, intensity corrections, reference spectra, solver settings and component-intensity calculation. [Image storage](../stages/images/storage.md#i-array-shape-metadata-and-encoding) explains how the prepared arrays are divided into chunks and shards; [unmixing](../stages/unmixing/index.md) explains the intensity calculations.

<a id="reference-data-formats--iii-fitted-classifiers-and-registration-transforms"></a>
## Fitted classifiers and registration transforms

A saved classifier includes its input features, their preparation, the fitted classifier and the mapping from class indices to biological names. Conventional tissue models retain the chosen channels, image filters, sampling and fitted normalization or PCA. KRONOS tissue models also identify the encoder and retain the fitted graph parameters. Supervised cell models record either the selected cell measurements or the image inputs used to calculate cell embeddings; KRONOS cell embeddings enter the classifier without that tissue graph.

The associated fit record identifies the reference subjects and annotations, fitting settings and available evaluation results. A training-only fit records that evaluation was not performed. See [tissue model execution](../stages/tissue-classification/model-execution.md) and [supervised cell classification](../stages/phenotyping/supervised.md) for how those files are used to make predictions.

To display an aligned field or transfer an existing annotation, we need both images’ pixel sizes and the transformation between them. The registration record identifies the fixed mIF image and its paired image and stores the parameters for the accepted fit. For an affine these are the matrix and translation. A landmark refinement also retains the TPS parameters, and an image-based local refinement retains the B-spline control grid and displacement coefficients. The [alignment guide](../stages/alignment/index.md) explains which direction to evaluate each map for display and for annotation transfer.

## Figure data

Figure CSVs retain numerical values, identities and selection metadata; NumPy `.npz` archives retain sampled arrays and their coordinates. The inventory below identifies the data behind each view. [Figure export controls](../stages/results/index.md#open-and-export-figures) describes saving them, and [result figures](../stages/results/figures.md) explains their scientific interpretation and denominators.

| Selected figure | Data export |
|---|---|
| Accepted reference spectra | **Accepted reference spectra · Data** CSV of component and acquired-band identities, available wavelengths, saved basis values and the displayed fractions of total signal. |
| Acquired bands and components | **Panel data** CSV of selected bands and components, intensity summaries, display limits and calibrated field coordinates. **Field data** NPZ contains the selected band arrays after intensity correction and saved component arrays, their identities, cycle assignments and display metadata. |
| Reconstruction images or mean spectra | **Field mean spectra** CSV: observed and reconstructed means for every acquired band, field coordinates and physical sampling. **Reconstruction field data** NPZ: observed/reconstructed images for the selected band, residual arrays, observed signal norm, all-band mean spectra and field/display metadata. |
| Tissue or analysis-region outlines | **Region areas and dimensions** CSV of sample and region identifiers, containing tissue section, calibrated area, width and height, edited status and inclusion where recorded. Width and height describe the region’s enclosing rectangle; area excludes interior holes. |
| Aligned images | **Field data** NPZ contains the current bounded sampled arrays, coordinates and display settings. **Check summary** CSV contains the selected review region's recorded diagnostics; **Sampled checks** CSV contains every original grid point, its mIF coordinates, mapping state and local area ratio. |
| Classifier review and data | [Named CSV tables](#classifier-review-tables) of subject allocation, model settings, fitting contributions, cross-validation settings and folds, evaluation scores, matrices and reference locations. |
| Tissue-classifier confusion | **Classifier confusion data** CSV of reference/predicted class pairs, counts, reference-class totals and fractions, evaluation split and selected subject or pooled population. |
| Tissue-classifier precision–recall | **Curves** CSV of thresholds, precision, recall, true/false positive counts, class prevalence and average precision. **Exact score counts** CSV preserves counts at each tied score by image, subject, fold and model. **Evaluation meaning** CSV defines the sampling, threshold rule and average-precision calculation. |
| Classified regions | **Class areas and sizes** CSV: counts, class areas, accepted analysis-region area, fractions, median and first/third area quartiles. **Individual region sizes (largest first)** CSV: each object’s area, class, native object identity and image bounds. Areas are in µm². |
| Final secondary structures | **Final structure outcomes** CSV includes subtype, observation, final disposition, member-cell count and calibrated area. **Recorded criterion measurements** retains values, units, criteria, inputs and unavailable reasons. **Final structure count decisions** retains observed/minimum/maximum/included counts and selection method. **Final secondary cell memberships** contains one row per included structure and cell, with complete sample/image/population/section/domain identities. **Secondary-region areas and dimensions** accompanies the outline figure. |
| Accepted marker intensity or Nimbus scores | **Histogram data** CSV of interval edges, cell counts, accepted cutoff and outcome. |
| Accepted marker fitted distribution | **Histogram data** and **Component density data** CSVs retain the counts and plotted weighted curves. **Fitted component parameters** CSV gives each component's mean, variance, weight and weight population at full saved precision. Gamma curve densities include the positive-value fraction; its component weights describe the positive-value fit. |
| UMAP | **UMAP coordinates and colour data** CSV of the displayed cells' sample/project/cell identities, both coordinates, selected colour attribute and value, and colour-display metadata. |
| Cell-shape or intensity distribution | **Measurement distribution data** CSV of histogram-bin edges and counts, selected interval and count, measurement identity and units, and unavailable-value counts. **Selected cell measurements** CSV lists each example cell’s identity and exact value. The tissue view also exports its visible field, outlines and cell values as described in the [measurement export guide](../stages/results/figures.md#measurement-distributions). |
| FlowSOM learned profiles | **Learned profile data** CSV of each profile's scaled marker values, group and plotted coordinates. **Profile connection data** CSV gives each tree connection and its distance in the scaled, weighted measurement space. |
| Cluster memberships and lineage | CSV of each fitted cell's sample, image, slide, section, analysis domain and cell ID, with its original, immediate-parent and current cluster IDs. This table records fitted memberships; accepted phenotype names and exclusions are retained with the reviewed assignments. |
| Phenotype composition | **Phenotype composition data** CSV of category counts, assignment availability, the total exported-cell count and each category's fraction. Includes all category pages. |
| Phenotypes by tissue compartment | **Phenotypes by tissue compartment data** CSV of sample/compartment identities, phenotype counts, assignment availability, each compartment's total and within-compartment fractions. Includes all category and compartment pages. |
| Marker profiles | **Marker profile data (all categories)** CSV of group identity, total cells, measurement names/units, usable-cell counts and group means. Includes every category for the selected measurement group. |
| Co-expression | **Marker combination data (all counted combinations)** CSV of every observed marker combination, complete-call counts and fractions, and unknown/outside-selection counts. |
| Phenotype rule assignments | **Phenotype rule summary** CSV of ordered rules, required marker states, matches, assigned cells, overlap with earlier rules and unknown required-marker counts. Includes unmatched and excluded totals. |
| Cell gallery | **Cell gallery selection and display data** CSV of full cell IDs, channels, positions, sampling and display settings; **Cell gallery pixels and outlines** NPZ contains the sampled pixels and drawn outlines. |

When a measurement distribution is restricted to an accepted phenotype, its histogram and example CSVs include the selected `phenotype`. Counts and missing/invalid totals refer to that subset. The visible-field CSV retains the same selection, and the field NPZ records it with its display options. With **All phenotypes**, the CSV describes the full selected population without a phenotype filter.

For a region selection, the histogram CSV includes its label and exact selection; field CSVs include the selection and available image/section/domain identities. The field NPZ retains the same selection and its saved bounds. Secondary filters use final accepted memberships. Selecting a secondary class counts each cell once across all included structures of that class.

<a id="classifier-review-tables"></a>
### Classifier review tables

Classifier tables describe the recorded fit, its subject allocation and any cross-validation or evaluation. Tables appear only when that fit retained the corresponding results; a training-only fit has no validation or test matrix. [Classifier review](../stages/results/index.md#reopen-a-stage-review) links to the views and their controls.

Observation units differ between evaluations. Tissue validation/test matrices count reference regions using one majority prediction per region (`annotation_majority`); tissue cross-validation matrices count pixels or patches; supervised cell matrices count reference cells (`cells`). Reference-region bounds use full-resolution image coordinates with the saved Z and T indices.

| CSV choice | Recorded values and comparison supported |
|---|---|
| **Subject allocation** | Classifier, subject, sample, QuPath image entry, training/validation/test split and saved classifier name. Use it to check which images belong to each subject and split. |
| **Saved model recipe** | One setting per row, with its exact value encoded as JSON: selected model parameters, channels, features and random seed. |
| **Fitting contributions by class** and **Fitting contributions by image** | Class or image identity, fitting-observation count and unit. Native image classifiers report pixels, KRONOS tissue classifiers report patches, and supervised cell classifiers report cells. |
| **Cross-validation candidates** | Parameter-combination number, selected flag, selection score, fold number and fold score. |
| **Cross-validation candidate settings** | Every parameter combination's settings and JSON values, retaining its number and selected flag. Join this to the candidate scores by classifier and candidate number. |
| **Cross-validation subject folds** | Classifier, subject and fold number. All images belonging to one subject share its fold. |
| **Selected candidate out-of-fold counts** | Reference/predicted class pair, count, reference-class total and fraction for each subject and the pooled result; accuracy, macro precision, macro recall and macro F1; selected candidate and subject fold. The interpretation field identifies these as tuning results. |
| **Out-of-fold reference locations** | Subject, sample, image entry, annotation ID and bounds, reference class, candidate and fold, reference agreement, sample count/unit and original-fold class votes encoded as JSON. |
| **Recorded evaluation scores** | Each saved metric by evaluation split and scope: pooled, subject or image, with the corresponding identity. |
| **Classifier evaluation** or **Cell classifier evaluation** | Validation/test reference/predicted pairs, counts, reference totals and fractions, subject or pooled scope, observation unit and the four aggregate classification scores. |
| **Evaluated reference locations** | Validation/test subject, sample, image entry, annotation ID and bounds, reference and predicted classes. When retained, the table also includes agreement, declared and ignored sample counts, sample unit and class votes as JSON. |
| **Evaluated reference cells** | Validation/test subject, sample, image entry and cell ID; reference and predicted classes; saved geometry/source identities and segmentation/measurement generations. X/Y coordinates are present when retained with the reference. |

<a id="tissue-field-pixels-and-outlines"></a>
### Tissue field pixels and outlines

The tissue-view **Field data…** or **Tissue field data…** export saves the displayed field as NPZ, or the retained visible cells as CSV. The CSV records whether all visible outlines were available; a wide view can use spatial counts without retaining individual outlines.

| NPZ array | Coordinates and interpretation |
|---|---|
| `pixels`, `channel_indices`, `channel_names` | Image values in channel, Y, X order, with the selected channel identities. |
| `extent_um` | The image field's left, right, bottom and top positions in absolute mIF image coordinates, in µm. |
| `outline_vertices_um`, `outline_offsets` | Drawn ring vertices in absolute µm and the offsets delimiting each ring. `outline_cell_indices` and `outline_kinds` associate each ring with a retained cell and its cell/nucleus kind. |
| `grid_selected`, `grid_measured`, `grid_extent_um` | Measurement-review counts of cells in the selected range and cells with usable values, with the spatial grid's physical extent. |
| `metadata_json` | Image and field identity, sampling, retained cell records, outline availability and display settings. |

In [nuclear staining coverage review](../stages/cells/index.md#inspect-nuclear-staining-coverage), a selected field also retains `coverage_overlay` and `coverage_extent_um`. Categories are 0 for no highlight, 1 for above-threshold staining covered by nuclear outlines, and 2 for above-threshold staining outside those outlines. This array retains the displayed sampling. The separate **Nuclear coverage table…** CSV records native-pixel counts and calibrated areas for every sampled field, together with the channel, threshold and field bounds.

When an aligned H&E view is exported from unsupervised review, the archive also retains its sampled RGB pixels, physical origin and pixel size, accepted alignment identity and blend settings. These field archives reproduce the sampled view; use the complete cell dataset and prepared image for the full stored geometry and channels.

The separate alignment inspector exports `fixed_pixels` and `moving_pixels` in channel, Y, X order, with channel indices and `x_um`/`y_um` sample centres in the fixed mIF frame. Its `rendered_rgba` array preserves the displayed composite; `metadata_json` records the source frames, view bounds, physical sampling and appearance. Paired pixels have already been resampled by the accepted transform. The archive contains the sampled viewport, not the complete original paired image or a per-pixel validity mask.

<a id="gallery-pixels-and-outlines"></a>
### Gallery pixels and outlines

For **Cell gallery**, the CSV contains one row per selected cell and channel: full cell ID, phenotype, channel identity, display limits and colour, crop dimensions, centre coordinates, image identity, pyramid level and physical pixel sizes. It also records the sampling seed or explicit-ID selection, channel display mode and whether the merged panel was shown.

The **Cell gallery pixels and outlines** NumPy archive retains the arrays used to draw the figure:

| Array | Coordinates and interpretation |
|---|---|
| `pixels` | Axes are cell, channel, Y, X. Values come from the recorded pyramid level; unavailable pixels are NaN. |
| `cell_ids`, `channel_indices`, `channel_names` | Identities in the same order as the pixel axes. |
| `centers_um` | Cell centres in the prepared mIF image's physical coordinates. |
| `extents_um` | Each crop's left, right, bottom and top positions, in µm relative to its cell centre. |
| `outline_vertices_um`, `outline_offsets` | Concatenated drawn ring vertices and the start/end offsets delimiting each ring, in µm relative to the cell centre. |
| `outline_cell_indices`, `outline_kinds` | The selected-cell row and cell/nucleus kind for each ring. |
| `metadata_json` | Display options, source-image frame, sampling level and pixel sizes, selection method and array conventions. |

These arrays allow the exported gallery to be redrawn at its recorded sampling. Use the [cell dataset](#input-files-for-spatial-analysis) for the complete accepted geometry, and the [prepared image](#processed-images-and-channel-descriptions) for full-resolution channels.

<a id="reference-data-formats--i-analysis-setup-and-progress"></a>
## Analysis setup and progress

The editable setup files retain the sample list and chosen settings. `analysis.manifest.json` records the inputs used to start processing, while `state/head.json` identifies the current completed steps, pending reviews and accepted outputs. [Analysis-folder contents](../stages/setup/analysis-folder.md#reference-analysis-folder--i-editable-setup-and-recorded-run-inputs) describes their locations; [saving and recovery](../stages/run/saving-and-recovery.md) explains how they support reopening and revising the analysis.

<a id="reference-data-formats--vii-file-completion-and-current-formats"></a>
<a id="file-completion-and-current-formats"></a>

---

**Continue:** [Results and export](../stages/results/index.md).
