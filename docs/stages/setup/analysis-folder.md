# Analysis-folder contents

<a id="reference-analysis-folder"></a>

<a id="reference-analysis-folder--analysis-folder-contents"></a>

[Reopen and resume](../run/index.md#user-guide-reviewing-and-resuming) · [Saved analysis settings](configuration.md)

For local processing, the analysis folder contains both the setup and the results. For cluster streaming, look in the local folder for the connection settings and in the cluster analysis folder for the processing history and results.

To find a prepared image, QuPath project or cell dataset from the desktop, open [Results](../results/index.md#locate-the-image-and-qupath-project).

<a id="reference-analysis-folder--i-editable-setup-and-recorded-run-inputs"></a>

## Editable setup and recorded run inputs

The desktop saves your sample list and analysis choices in `analysis.setup.json`, so you can return to unfinished sections. It saves the selected processor and processing location in `analysis.computing.json`. For cluster streaming this file also holds the connection and folder mapping.

Preparing the run validates these choices, fills in defaults and writes `analysis.manifest.json`. This file identifies the images and settings at the start of the run. If you later change an accepted setting, we save a new plan identifying the work affected by that revision.

### Locations of saved files

The processing workspace contains the QuPath project, stage results and the records used to reopen the run. For local processing, look for this workspace under the analysis folder. With cluster streaming, the desktop keeps the connection settings locally and reads progress from the workspace on the cluster. Source images and shared models remain at the paths selected during setup.

```text
<processing workspace>/
    manifest/
        original.json
        normalized.json
    plans/
    state/
        head.json
        current-progress.json
        history-<checksum>.json
    qupath/
        project.qpproj
    attempts/
    interactions/
    catalogue/
    summaries/
    resume/
    samples/<sample>/...
```

| Processing record | What it records | When to consult it |
|---|---|---|
| `manifest/original.json`, `manifest/normalized.json` | Original and resolved processing inputs | Preparing and reopening the processing workspace |
| `plans/` | Successive plans and task dependencies | Determining the work required by each accepted revision |
| `state/head.json` | Current plan, project state, selected results and execution-history reference | Selecting the current analysis state |
| `state/current-progress.json` | Derived workflow, sample status, result references and error/review messages | Refreshing the desktop from the saved progress summary |
| Completed dataset description | Selected cells/regions, input versions, columns, data files, indexes and summary counts | Opening a cell or region result with its recorded definitions |
| `attempts/`, `interactions/` and `state/history-<checksum>.json` | Processing attempts, review requests/responses and recorded state changes | Tracing the operation and review that produced a selected result |

Stage results identify the prepared image, model and cell-table files used by later calculations, including files outside the per-sample folders.

<a id="reference-analysis-folder--ii-processing-progress-and-accepted-results"></a>
## Processing progress and accepted results

The plan lists each task and the results it needs before it can start. Each time a task runs, its attempt record identifies the inputs, settings, outcome and checked outputs. When you accept a review, we save your decision and update `state/head.json` to identify the results used by subsequent stages.

When you reopen an analysis, the desktop checks that the progress in `state/current-progress.json` agrees with the plan and results selected in `state/head.json`. Run then shows which samples are processing, which reviews need a decision and which steps have stopped. If a calculation ran more than once, its attempt records identify the inputs and outputs of each run. The [recovery guide](../run/saving-and-recovery.md#select-a-complete-dataset) explains how we select a completed result.

<a id="reference-analysis-folder--iii-qupath-project-and-annotation-drafts"></a>
## QuPath project and annotation drafts

The main QuPath project lists its images and classifiers. Each image's `data.qpdata` file stores its settings and annotations. An outline-editing draft contains the QuPath objects selected for correction.

We store accepted cell outlines in Spatial Parquet and keep subsequent corrections in separate edit tables. When a calculation requests cell data, the reader uses the cell IDs to apply the relevant edits and returns the requested columns one batch at a time. In Python, each batch is a PyArrow `RecordBatch` in memory.

Tissue-detector editing, classifier references and landmarks use separate draft projects containing the images and objects needed for that step. [Saving a project and returning annotations](../run/qupath-review.md#reference-qupath-integration--ii-saving-the-project-and-returning-annotations) explains the editing and submission sequence.

When a draft returns, we check its image coordinates, requested labels and permitted objects before accepting the changes into the analysis. The saved review identifies which annotations were used by the detector, classifier or alignment operation.

To decide whether a QuPath save affects later processing, we compare the image settings used by the analysis. The file `project.qpproj.mif-content.json` beside the project contains the checksums used for that comparison. If you saved after viewing the image and its identity, channels and physical calibration still match, the analysis can recognize those inputs on reopening. [How we compare saved project inputs](../run/qupath-review.md#reference-qupath-integration--ii-saving-the-project-and-returning-annotations)

Each step records the annotations, tissue outlines, model parameters or measured cells it used. Later calculations check those input versions before reusing the result. Changing a training label requires refitting its model; the original image entries can still be used.

<a id="reference-analysis-folder--iv-processed-images-and-exported-measurements"></a>
## Processed images and exported measurements

Each prepared image is a sharded `.ome.zarr` directory containing its full-resolution values, pyramid levels and OME metadata. Its processing record identifies the source image and metadata used to prepare it. For spectral unmixing, the record also includes the intensity correction, selected spectra and fitting settings. Later stages read the completed image directory selected by its accepted result. [Image storage](../images/storage.md) explains its chunk and shard layout.

Spatial Parquet tables store cell outlines and positions in a base dataset. Measurement and classification results can add selected columns through separate result tables. The final `spatial-cell-handoff.json` selects a completed generation in the adjacent hidden directory.

```text
<export directory>/
    spatial-cell-handoff.json
    .spatial-cell-handoff.generations/<generation-id>/
        descriptor.json
        cells/dataset.json
```

To open a spatial export, start with `spatial-cell-handoff.json`. It locates the export description in the adjacent hidden directory, so copy that directory with the handoff file. The description identifies the cell collection and gives the full paths to its tables, result columns and corrections. Those datasets must remain accessible at the recorded paths. See the [file format guide](../../reference/data-formats.md#file-identifiers-and-selection-checks) for the checksums and the [Python interface](python-api.md#publish-and-reopen-a-spatial-handoff) for reading selected columns.

<a id="reference-analysis-folder--v-reopening-and-cleanup"></a>
## Reopening and cleanup

Open the named analysis folder to inspect saved progress. To continue a paused calculation, the processing computer also needs access to its source images and any earlier results it uses. We check the saved request, its previous outcome and its output files to determine whether the current attempt can continue or a new one is needed. [Recovery after interruption](../run/saving-and-recovery.md#recover-after-interruption)

Before deleting an older dataset, check whether an accepted model or export still refers to it. Revisions can supersede an image or cell table for new calculations and still leave an earlier result dependent on that file. [Retention and cleanup](../run/saving-and-recovery.md#retain-results-and-clean-up-temporary-files)

---

**Continue:** [Analysis setup](analysis.md).
