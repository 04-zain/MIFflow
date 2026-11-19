# Public image sources

These are the source images used in the walkthroughs. Each entry identifies the original dataset, its reuse terms and the field or channels shown in the guides.

## LuCa fluorescence field

The PerkinElmer file `LuCa-7color_[13860,52919]_1x1component_data.tif` contains eight float32 component channels over 1868 × 1400 pixels at 0.49799447890790055 µm/pixel. The [LuCa example](luca.md) provides its direct download, CC-BY 4.0 attribution and channel panel, and identifies the [cell populations](luca.md#recorded-example-populations) used by the measurement and marker-decision walkthroughs.

## Whole-slide fluorescence inputs

The [OME LuCa Scan1](https://downloads.openmicroscopy.org/images/OME-TIFF/2016-06/sub-resolutions/Fluorescence/LuCa-7color_Scan1.ome.tiff) file contains a 24,960 × 34,560-pixel, five-channel uint8 image at approximately 0.498 µm/pixel. Its channels are DAPI, FITC, CY3, Texas Red and CY5. This is a different scan and channel representation from the eight-component LuCa field described above.

[HTAN MEL02-1-1](https://hta-melatlas-1.s3.amazonaws.com/data/MEL02-1-1.ome.tif) contains an 11,218 × 13,599-pixel, 48-channel uint16 image at approximately 0.650 µm/pixel. The [publisher's data description](https://labsyspharm.github.io/HTA-MELATLAS-1/data.html) identifies it as whole-slide cyclic immunofluorescence (CyCIF), assembled from registered imaging rounds with flat-field correction. The [Minerva channel definitions](https://labsyspharm.github.io/HTA-MELATLAS-1/exhibits/MEL02-1-1-overview.json) provide marker names for the displayed channels.

The [source overview](../assets/figures/fluorescence-wsi-source-overviews.png) shows channel zero from each original image, with brightness limits selected separately to reveal its tissue.

<a id="melanoma-classifier-batch"></a>

### Melanoma classifier batch

The [classifier review example](../stages/tissue-classification/index.md#inspecting-and-accepting-the-model) uses three 1024 × 1024-pixel fields from each of [MEL02-1-1](https://hta-melatlas-1.s3.amazonaws.com/data/MEL02-1-1.ome.tif), [MEL05-1-1](https://hta-melatlas-1.s3.amazonaws.com/data/MEL05-1-1.ome.tif) and [MEL09-1-1](https://hta-melatlas-1.s3.amazonaws.com/data/MEL09-1-1.ome.tif) in the same HTAN CyCIF collection. The fields retain the source uint16 intensities and approximately 0.650 µm/pixel calibration. Each prepared field contains DNA, SOX10 and KERATIN; the classifier uses DNA and KERATIN.

Thirty-six manually reviewed rectangular annotations define the walkthrough classes **Keratin-rich epithelium** and **Other nucleated tissue**. The recorded scores describe agreement with these annotations. MEL02 and MEL05 supply the two grouped cross-validation folds and the final fit; MEL09 supplies the independent test.

The [Results composition and compartment figures](../stages/results/figures.md#phenotype-composition-and-tissue-compartments) use 1,652 cells from the first MEL05 field. A supervised cell classifier fitted on SOX10 and KERATIN means assigns **KERATIN-positive epithelial cells**, **SOX10-positive cells** and **Other nucleated cells**; a 0.70 tree-vote cutoff retains ten cells as **Unclassified**.

## MAPS classical Hodgkin lymphoma fluorescence

The unsupervised phenotype review uses a **1024 × 1024-pixel** field from the **cHL CODEX** dataset released by [Shaban et al.](https://www.nature.com/articles/s41467-023-44188-w), available under **CC BY 4.0** from [Zenodo 10067010](https://zenodo.org/records/10067010). The field begins at source position **(2560, 1536)**, contains 23 fluorescence channels, and retains the supplied values and **0.37 µm/pixel** calibration.

The [unsupervised phenotyping walkthrough](../stages/phenotyping/unsupervised.md#maps-refined-example) and Results examples use **3,459 InstanSeg cells** and 16 whole-cell marker means for an initial twelve-group PhenoGraph fit. Broad B-cell, CD4 T-cell and CD8 T-cell assignments follow review of marker distributions and native tissue fields; unresolved groups retain **Undefined**, and all cells remain included. The separately labelled optional FlowSOM/reclustering illustration uses a different fit.

## Cyclic tonsil channel assignments

The [cyclic-image guide](../stages/cyclic-if-methods/index.md) uses `Arlene_Tonsil_9x9.qptiff` from [Sangüesa, de Andrea and Ariz, Zenodo 22107401](https://zenodo.org/records/22107401), released under **CC BY 4.0**. The image contains 43 channels over 12,672 × 9,508 pixels. Its original channel metadata supplies marker names and cycle assignments for 42 channels; DAPI-01 lacks that per-channel value, but the embedded scan profile identifies it as cycle 1. The walkthrough reviews these source assignments before preparing the channels.

<a id="separate-cycif-acquisition-files"></a>
## Separate CyCIF cycle files

The separate-file registration example in the [cyclic-image guide](../stages/cyclic-if-methods/index.md#align-separate-cycle-files) uses **MCMICRO Exemplar 001**, a lung adenocarcinoma specimen imaged by CyCIF. [Schapiro et al., *MCMICRO: a scalable, modular image-processing pipeline for multiplexed tissue imaging*](https://doi.org/10.1038/s41592-021-01308-y), Nature Methods **19**, 311–315 (2022), describe the resource. The [official datasets page](https://mcmicro.org/datasets/) supplies the exemplar for demonstration purposes, and the [MCMICRO tutorial](https://mcmicro.org/tutorial/tutorial.html) describes its raw cycle images and shared Hoechst stain.

The screenshots show the corresponding field **761** (first image series) from `exemplar-001-cycle-06.ome.tiff` and `exemplar-001-cycle-07.ome.tiff`. Each original field contains four uint16 channels over **1,280 × 1,080 pixels** at **0.65 µm/pixel**. Registration uses the original Hoechst channels. Cycle 6 supplies the reference grid, and the retained common rectangle begins at reference pixel **(45, 1)** with dimensions **1,234 × 1,078 pixels**.

The guide reproduces limited application views of these demonstration images. The article is published under **CC BY 4.0**, and the MCMICRO website identifies its own content as **CC BY-NC 4.0**. The source pages do not specify a separate licence for the linked raw-image downloads; those files are not redistributed here.

## Same-section tonsil fluorescence and H&E

The positive tonsil pair from [Marlin et al., Zenodo 8023452](https://zenodo.org/records/8023452) is released under CC BY 4.0. The [study methods and antibody table](https://onlinelibrary.wiley.com/doi/10.1002/cyto.a.24789) describe fluorescence imaging followed by H&E staining and imaging of the same tissue section. The original CZI scans retain their distinct image extents, physical sampling and image coordinates.

| Original file | Image | Sampling |
| --- | --- | --- |
| `2022_08_9_Tonsil_IF_PositiveIF.czi` | 24,517 × 33,405 pixels; four uint16 fluorescence channels | 0.344107 µm/pixel |
| `2022_08_10_Tonsil_IF_HE_PostiveIF.czi` | 42,792 × 60,118 pixels; RGB H&E | 0.172054 µm/pixel |

The stored fluorescence channels are **Cy5, AF555, AF488 and DAPI**. The methods identify the corresponding antibody targets as **CD4, vimentin and CD45**; CD45 uses an AF488 secondary antibody. Marker assignments retain these original channel names separately.

The alignment example uses independently framed native rectangles around the same follicular chain: fluorescence begins at **(15,000, 13,000)** and contains **5,000 × 6,000** pixels; H&E begins at **(25,000, 21,000)** and contains **11,000 × 14,000** pixels. Lossless OME-TIFF exports retain the original level-zero values and calibration. The fields were selected before fitting any spatial transform.

## Tonsil lymphoid follicles

`KB_tonsil_TLS_Scan1.qptiff` is a tonsil control from the [MacFawn et al. public image collection](https://data.mendeley.com/datasets/9zxht4rchk/1), released under CC BY 4.0 with the study *The activity of tertiary lymphoid structures in high grade serous ovarian cancer is governed by site, stroma, and cellular interactions*. The image contains 21,120 × 23,040 pixels at approximately 0.497 µm/pixel. Its staining panel includes DAPI, CD4, PNAd, CD21, Ki67, CD20 and AID; the image also includes an autofluorescence band.

The follicle example uses a 2,600 × 2,600-pixel field beginning at x = 12,600 and y = 14,900 in the source image. We first unmixed the source with the MOTiF library. The derived crop retains those prepared float32 values and the source pixel dimensions exactly. The selected field shows a CD20-rich mantle around a smaller CD21/AID/Ki67-rich centre, with CD4-rich tissue outside. The example concerns lymphoid follicles in tonsil tissue.

<a id="cmu-1-source-and-regional-processing"></a>

## CMU-1 whole-slide preparation

The public [CMU-1.svs](https://openslide.cs.cmu.edu/download/openslide-testdata/Aperio/CMU-1.svs) image comes from the OpenSlide Aperio collection. [QuPath's image acknowledgements](https://qupath.readthedocs.io/en/stable/docs/intro/acknowledgements.html) record its CC0 1.0 reuse terms.

| Property | Recorded value |
|---|---|
| Original image | 46,000 × 32,914 pixels, RGB uint8 |
| Native pyramid | 46,000 × 32,914; 11,500 × 8,228; 2,875 × 2,057 pixels |
| Physical sampling | 0.499 µm/pixel along X and Y |
| Prepared image | Complete RGB uint8 OME-Zarr image, retaining all three native levels |
| Stored RGB organization | Red, Green and Blue sample planes in all-sample 256 × 256 chunks and 1024 × 1024 shards |

The [RGB preparation method](../stages/images/processing.md#prepare-a-packed-rgb-image) explains how we preserve the colour image and its resolution levels. The same source supplies the [zoomed annotation view](../stages/run/qupath-review.md#reference-qupath-integration--ii-saving-the-project-and-returning-annotations).

The retained regional measurement example uses a separate **1109 × 1057** level-zero window beginning at **(8195, 19204)**. Adding that origin to a regional coordinate locates the corresponding point in the whole slide.

[Images and channels](../stages/images/index.md) · [Image reading and preparation](../stages/images/processing.md) · [Cell measurements](../stages/cell-measurements/index.md#luca-measurement-example)

---

**Continue:** [Import and inspect the images](../stages/setup/analysis.md#import-the-images-together).
