# The MIFflow extension for QuPath

The MIFflow extension lets QuPath read prepared OME-Zarr images and display cell outlines from the analysis. Our Groovy scripts also call Java methods in the extension to segment cells, measure their staining and apply classifiers.

If you installed MIFflow from a wheel or downloaded the portable desktop, the compiled extension is included. Select your QuPath installation in [Software setup](software.md#qupath-and-application-extensions) to use it. If you are installing or packaging MIFflow from a repository checkout, build the extension first.

<a id="build-and-install"></a>
## Build the extension from source

#### 1. Install QuPath 0.7.0 and a Java 21 JDK

To compile the MIFflow extension, the Java compiler needs the QuPath libraries that our extension calls. The build script reads these libraries from your local QuPath installation.

Install QuPath 0.7.0 and note its root installation directory (the directory containing `lib/app` on Ubuntu or `app` on Windows). Install a Java 21 JDK to compile the extension.

#### 2. Choose a dependency cache and an output directory

The extension also needs third-party libraries for reading OME-Zarr images and Parquet cell tables. Choose two directories outside the repository checkout.

- **Dependency cache.** The build script downloads the libraries here on the first build and reuses them on subsequent builds. The build script packages these libraries with our compiled extension classes in the extension JAR. 
- **Output directory.** The script writes the compiled extension JAR, its checksum and the build record here.

#### 3. Run the build script

From the repository root, run the command for your operating system. Set `--qupath` to the installation directory from step 1, `--download-dependencies` to the cache and `--output` to the directory for the compiled extension.

**Ubuntu**

```bash
python3.12 scripts/build_native_extension.py --qupath /path/to/QuPath \
  --download-dependencies "$HOME/mif_pipeline_build/dependencies" \
  --output "$HOME/mif_pipeline_build/native"
```

**Windows PowerShell**

```powershell
py -3.12 scripts/build_native_extension.py --qupath 'C:\path\to\QuPath' `
  --download-dependencies 'C:\mif_pipeline_build\dependencies' `
  --output 'C:\mif_pipeline_build\native'
```

The script prints the path to `qupath-extension-mif-0.1.0.jar` when compilation succeeds. Beside the JAR, the script writes its SHA-256 checksum and a build record (`reader.json`) that identifies the file to include in the Python package.

Use this output directory when [installing from the checkout](software.md#install-the-python-application) or [building a wheel](building-and-releasing.md#python-distributions). Set `MIF_NATIVE_READER_BUILD_DIR` to the directory so the Python build can find and package the JAR. For a portable application, pass the directory to the [build launcher](building-and-releasing.md#portable-application-builds).

<details>
<summary>Build offline or use a different Java compiler</summary>

For an offline build, replace `--download-dependencies` with the paths to three local JARs. Pass zarr-java 0.3.1 with `--reader` for OME-Zarr image access, Apache Parquet 1.16.0 with `--parquet` for cell tables, and the Hadoop client API 3.4.1 with `--hadoop-api` for Parquet’s Java dependencies. The script checks each file against its expected SHA-256 checksum. If a file fails the check, replace it with the specified release before rebuilding.

By default, the script uses `javac` to compile the sources under `src/java`. If your QuPath installation uses Java 25 class files that your Java 21 compiler cannot read, add `--use-ecj` to compile with Eclipse compiler 3.43.0. Use `--ecj` to supply its JAR offline. The build includes the dependency notices and the service registrations under `src/java-resources` that QuPath uses to find the image reader and cell overlay.

</details>

## How QuPath loads the extension

During **Software setup**, we prepare a QuPath user directory for MIFflow. Its `extensions` folder receives our bundled JAR and the InstanSeg and Warpy extensions. For InstanSeg and Warpy, setup reuses compatible local files or downloads the files that are missing.

QuPath looks up its user directory in a named set of preferences called a *profile*. During setup we save the new user directory in that profile. When we launch QuPath, we pass `-Dqupath.prefs.name=<profile name>` on the command line. QuPath reads the directory from those preferences and loads the JARs in its `extensions` folder. If you ran setup with QuPath already open, reopen it from MIFflow to load them.

<a id="image-reading"></a>
## Read a field from a prepared image

When you pan or zoom in QuPath, the viewer requests a rectangle of pixels at the displayed resolution. At the corresponding OME-Zarr level, a 256 × 256-pixel chunk stores all channels for that area. Sixteen chunks fit in a 1024 × 1024-pixel shard. The shard index gives the location and length of each compressed chunk, so the reader can retrieve the chunks that intersect the requested rectangle. Previously decoded chunks can be reused for overlapping views. The QuPath project entry saves the image location and reader settings for the next session. [Image storage and regional reads](../images/storage.md)

For physical measurements, the reader uses the X and Y pixel sizes recorded at the requested pyramid level. Estimating them from the ratio of image dimensions would introduce errors when the reduced dimensions were rounded. The reader supports levels with zero translation along the image axes. For unsigned 32-bit images, it supplies double-precision values to QuPath so that every stored integer intensity can be represented exactly.

<a id="cell-tables-and-segmentation"></a>
<a id="reconcile-overlapping-segmentation-tiles"></a>
## Save and display segmented cells

InstanSeg processes overlapping tiles. When predictions from adjacent tiles overlap, we group them and wait until all tiles touching that group have finished. The InstanSeg postprocessor then reconciles their outlines, and QuPath measures the resulting shapes. A failed tile leaves the affected group pending. [How overlapping predictions are reconciled](../cells/processing.md)

The QuPath image identifies its tissue sections’ cell tables through the `mif.cell_datasets` property. Each record contains the cell’s outline in well-known binary (WKB) format and its centroid as two double-precision coordinates. At low magnification the viewer can count these centroids in a 64 × 64 grid to show where cells are concentrated. As you zoom in, it retrieves the outlines in the displayed field so you can select a cell and open its record by ID.

When reconstructing an outline, we preserve the coordinate precision of its vertices and holes. For outlines saved with packed-float coordinates, we use the original QuPath geometry factory. For double-precision WKB, we construct a `GeometryROI` directly. [Cell geometry and coordinate precision](../../reference/cell-storage.md)

<a id="retain-edits-and-derived-results"></a>
<a id="measurement-and-geometry-calculations"></a>
## Use the saved cells and regions

### Measure a corrected cell

When you accept an outline correction, we save the edited geometry in a separate Parquet table. Subsequent calculations read that geometry when measuring the corrected cell. For each selected channel and cell region, we reuse measurements that still apply and calculate the remaining values in QuPath. [Cell tables and corrections](../../reference/cell-storage.md)

### Assign cells to regions and transfer regions between images

To assign a cell or candidate structure to a region, the extension reads their coordinates in the same image. To transfer an outline between paired images, it first maps the outline into the destination image using the saved alignment. For automatic registration, the Groovy scripts call Elastix and Transformix. Java evaluates the transformation in physical coordinates and measures the transferred region’s area, fragmentation and overlap with tissue. [Image alignment](../alignment/index.md)

<a id="model-application-and-reference-cells"></a>
<a id="classification-results-and-display"></a>
<a id="match-imported-points-to-cells"></a>

### Match reference cells and display phenotypes

When an imported reference point has no cell ID, its image coordinates identify the cell to use for training. We find eligible centroids within the specified radius in full-resolution pixels and select the nearest one. The cell ID resolves ties. The saved reference records the matched cell, its image and the segmentation result. [Matching reference points](../phenotyping/supervised.md#match-point-references-to-cells)

During supervised prediction, we read cells in batches and save the predicted phenotype and confidence with each cell ID. QuPath can switch between accepted phenotype results using their saved columns. The prefix `phenotype_view/<mode>/<generation>/` identifies the result. Depending on the method, the columns record the phenotype and its source, component labels, marker states, confidence, cluster ID and cluster label.

<a id="image-cache-and-request-handling"></a>
<a id="shared-memory-limits"></a>
## How the extension limits memory use

| Data in memory | What happens at the limit |
|---|---|
| Decoded image tiles | `MIF_IMAGE_CACHE_BYTES` sets a limit for the process. Use the image-server argument `--cache-bytes` to allow less for an individual image. Each reader also caches shard indexes up to one sixteenth of its requested allowance, capped at 1 MiB. |
| Predictions from overlapping tiles | We can move predictions awaiting reconciliation to a temporary Zstd-compressed file and restore their geometry when adjacent tiles finish. If a connected group is too large to reconcile, the calculation reports how much memory it requires. |
| Visible cells and measurement columns | The shared cache uses up to `MIF_OBJECT_CACHE_BYTES`. It releases the least recently used entries first. A subsequent request for those cells or measurements reads them from disk. |
| Dataset descriptions and spatial indexes | `MIF_METADATA_CACHE_BYTES` sets the allowance. Opening a dataset reports an error if its description and index would exceed that allowance. |

<a id="read-accounting-and-geometry-precision"></a>

For diagnostics, the reader counts file-range reads, decoded bytes and the outlines reconstructed for display.
