# File-format and implementation references

[Architecture and data flow](../../../README.md#architecture-and-data-flow) · [Source map](source-layout.md)

<a id="reference-architecture-sources"></a>
<a id="reference-architecture-sources--architecture-sources-and-limits-of-evidence"></a>
<a id="architecture-sources-and-limits-of-evidence"></a>

Each stage lists its file specifications and external implementations alongside the corresponding MIFflow guide. Links to a development branch follow changes upstream. To find the release selected for installation, see the [software descriptions](configuration.md#installed-software-descriptions).

<a id="reference-architecture-sources--i-how-we-use-the-sources"></a>
<a id="how-we-use-the-sources"></a>
## Image requests, chunks and compression

When the viewer needs pixels for a field, QuPath requests them at the selected resolution. The reader looks up the compressed chunks covering that field in the Zarr shard index, reads their bytes and decodes them into pixel values. The sources below document each step.

<a id="reference-architecture-sources--e01-qupath-07-image-region-scheduling"></a>
### E01. QuPath 0.7 image-region scheduling

[QuPath 0.7 image-region scheduling](https://raw.githubusercontent.com/qupath/qupath/v0.7.0/qupath-gui-fx/src/main/java/qupath/lib/gui/images/stores/DefaultImageRegionStore.java). When you pan across an image, QuPath displays any tiles it has cached and requests the missing tiles through its image store. For an export or whole-image rendering, it waits for the requested pixels before producing the result. For prepared images, the MIFflow extension supplies those pixels from OME-Zarr.

<a id="reference-architecture-sources--e02-zarr-v3-indexed-sharding-specification"></a>
### E02. Zarr v3 indexed sharding specification

[Zarr v3 indexed sharding](https://zarr-specs.readthedocs.io/en/latest/v3/codecs/sharding-indexed/index.html). The index records offsets and lengths for independently encoded chunks. The prepared-image reader uses these entries to locate the compressed chunks intersecting a requested region. [Image storage](../images/storage.md) explains the chosen chunk and shard layout.

<a id="reference-architecture-sources--e03-zarr-java-partial-shard-reader"></a>
### E03. zarr-java partial shard reader

[zarr-java partial shard reader](https://raw.githubusercontent.com/zarr-developers/zarr-java/main/src/main/java/dev/zarr/zarrjava/v3/codec/core/ShardingIndexedCodec.java). Its partial decoder locates inner chunks through the shard index. The packaged QuPath adapter supplies the selected reader/codec build. Use its build definition when checking the code shipped with an installation; the linked upstream `main` branch follows library development.

<a id="reference-architecture-sources--e11-blosc-context-api-and-thread-controls"></a>
### E11. Blosc context API and thread controls

[Blosc context API](https://github.com/Blosc/c-blosc/blob/main/blosc/blosc.h). The context calls accept compression/decompression thread counts independently of global settings. This is the interface used to coordinate codec work with regional decoding. [Memory and threads](memory-and-threads.md) explains the process and thread allowances.

## Cell columns, geometry and spatial indexes

We store cells in Parquet. Within each group of rows, the measurements, outlines and other fields occupy separate columns. A Python calculation can therefore load just the fields it needs into Arrow arrays in memory.

A viewer also needs to select cells by position. The spatial index identifies the row groups that might intersect the field of view. Within those groups, the reader checks the requested condition for each candidate cell, such as whether its outline intersects the field. GeoParquet describes how the outline and its coordinate metadata are stored.

<a id="reference-architecture-sources--e04-apache-arrow-columnar-and-dictionary-layouts"></a>
### E04. Apache Arrow columnar and dictionary layouts

In Arrow, a dictionary-encoded column stores each distinct label once and represents each row’s label by its index in that dictionary. A validity bitmap identifies missing values. The [Arrow format specification](https://arrow.apache.org/docs/format/Columnar.html) describes how these buffers are laid out in memory.

<a id="reference-architecture-sources--e05-parquet-encodings"></a>
### E05. Parquet encodings

[Parquet encodings](https://parquet.apache.org/docs/file-format/data-pages/encodings/). Dictionary pages encode repeated values within column chunks. The analysis's separate category dictionary records stable class meaning for each field and result version.

<a id="reference-architecture-sources--e06-parquet-file-organization"></a>
### E06. Parquet file organization

[Parquet file organization](https://parquet.apache.org/docs/file-format/). Final file metadata locates row groups and encoded columns. We close each file and check its metadata before making the completed dataset available; [saving and recovery](../run/saving-and-recovery.md#iii-completing-one-table-part) explains this sequence.

<a id="reference-architecture-sources--e07-geoparquet-11"></a>
### E07. GeoParquet 1.1

[GeoParquet 1.1](https://geoparquet.org/releases/v1.1.0/). The specification defines geometry encoding and metadata. We store cell geometry in primary-image level-zero pixel coordinates, retain X/Y calibration and declare a null geographic coordinate reference system. The cell dataset's separate row-group bounds identify candidates for a spatial query.

<a id="reference-architecture-sources--e08-jts-strtree-interface"></a>
### E08. JTS STRtree interface

[JTS STRtree](https://locationtech.github.io/jts/javadoc/org/locationtech/jts/index/strtree/STRtree.html). `STRtree` indexes bounding rectangles and returns candidates intersecting a query envelope. The native cell reader builds its index from the compact dataset layout to select candidate row groups; the subsequent cell query applies its geometry predicate. [Cell tables](../../reference/cell-storage.md) describes those two stages.

## File access and complete-result publication

For a regional read, the shard index gives the byte ranges needed to retrieve the compressed chunks. To publish a result, the writer finishes its data files and then replaces the record that points readers to them. The interfaces below support these reads and writes.

<a id="reference-architecture-sources--e09-java-21-filechannel"></a>
### E09. Java 21 FileChannel

[Java 21 FileChannel](https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/nio/channels/FileChannel.html). The interface supports reads from a specified byte position, mapped regions and synchronization to storage. The image adapter uses byte positions and lengths from the shard index when requesting compressed chunks; the filesystem and reader determine the actual I/O performed.

<a id="reference-architecture-sources--e10-java-21-bytebuffer"></a>
### E10. Java 21 ByteBuffer

[Java 21 ByteBuffer](https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/nio/ByteBuffer.html). Direct buffers have different allocation/deallocation costs and may enable native I/O optimizations. Explicit layout, byte order and lifetime remain necessary.

<a id="reference-architecture-sources--e16-java-21-files-move-semantics"></a>
### E16. Java 21 Files move semantics

[Java 21 Files](https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/nio/file/Files.html). File moves use the capabilities of the selected provider. We finish writing result files and request the filesystem's supported flush operations before replacing the selected-result record. Atomic replacement lets readers see either the complete old record or the complete new one on a supporting filesystem.

## Model input memory and segmentation

The encoder passes prepared image arrays into DJL tensors. Their device, layout and allocation lifetime determine when the input buffer can be shared and when a copy is required. InstanSeg adds the image tiling and reconciliation that produce completed cell and nucleus outlines.

<a id="reference-architecture-sources--e12-djl-native-memory-management"></a>
### E12. DJL native memory management

[DJL native memory management](https://docs.djl.ai/master/docs/development/memory_management.html). Model/predictor and per-call tensor scopes differ. Predictor contexts already manage temporary translator data; retained tensor outputs need a defined allocation and release point.

<a id="reference-architecture-sources--e13-djl-pytorch-manager-buffer-preparation"></a>
### E13. DJL PyTorch manager buffer preparation

[DJL PyTorch buffer preparation](https://raw.githubusercontent.com/deepjavalibrary/djl/master/engines/pytorch/pytorch-engine/src/main/java/ai/djl/pytorch/engine/PtNDManager.java). Compatible direct buffers can supply native CPU inputs. The selected dtype, byte order, strides and buffer lifetime determine the permitted sharing path.

<a id="reference-architecture-sources--e14-djl-pytorch-jni-buffer-handling"></a>
### E14. DJL PyTorch JNI buffer handling

[DJL PyTorch JNI handling](https://raw.githubusercontent.com/deepjavalibrary/djl/master/engines/pytorch/pytorch-engine/src/main/java/ai/djl/pytorch/jni/JniUtils.java). `createNdFromByteBuffer` chooses the dense CPU borrowing or copy path according to device and layout. Borrowed direct input remains live through the native tensor’s use. Read this alongside the manager’s buffer preparation when changing the encoder input path; the packaged DJL version is identified in `config/software/qupath.json` and `pytorch.json`.

<a id="reference-architecture-sources--e15-instanseg-qupath-extension-017"></a>
### E15. InstanSeg QuPath extension 0.1.7

[InstanSeg extension 0.1.7](https://raw.githubusercontent.com/qupath/qupath-extension-instanseg/v0.1.7/src/main/java/qupath/ext/instanseg/core/InstanSeg.java). This release defines the model tiling and object reconciliation called by the integration. [Cell processing](../cells/processing.md#finish-cells-after-their-neighbouring-tiles) explains how the adaptor retains overlapping detections until they can be finalized and written.

<a id="reference-architecture-sources--ii-prototype-source-observations"></a>
<a id="prototype-source-observations"></a>
<a id="cell-identity"></a>

<a id="cell-identity-in-the-application"></a>
## Cell identity and QuPath objects

To identify a cell throughout the analysis, the segmentation script uses `UUID.nameUUIDFromBytes` to combine its segmentation-population identity with a key derived from its final cell and nuclear outlines. Measurement and reference tables use this saved `cell_id` to find the same cell again.

When QuPath needs a displayed or editable object, the native reader gives the object the saved ID if it is a UUID. For other IDs it derives a UUID from the population identity and `cell_id`. In either case the object’s metadata retains the original population and cell ID so a selection in QuPath can retrieve the corresponding dataset row.

See `qupath/scripts/cell_analysis/04_run_instanseg.groovy` for creation and `src/java/org/mif/cells/CellDatasetProvider.java` for the QuPath object mapping. [Cell-table identities](../../reference/cell-storage.md) describes the saved dataset and result associations used by downstream readers.

<a id="reference-architecture-sources--iii-scope-of-the-comparison"></a>
<a id="scope-of-the-comparison"></a>
<a id="implementation-details"></a>

**Continue:** [Source components and integration points](source-layout.md).
