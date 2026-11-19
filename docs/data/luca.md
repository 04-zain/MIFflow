# LuCa example image

<a id="tutorials-public-data"></a>
<a id="tutorials-public-data--luca-input-preparation-and-capture-record"></a>
<a id="tutorials-public-data--source-image"></a>
## Source image

The stage guides use the PerkinElmer field `LuCa-7color_[13860,52919]_1x1component_data.tif` from the [OME Vectra sample collection](https://downloads.openmicroscopy.org/images/Vectra-QPTIFF/perkinelmer/PKI_fields/LuCa-7color_%5B13860%2C52919%5D_1x1component_data.tif). The dataset is attributed to PerkinElmer under **CC BY 4.0** in [QuPath's image acknowledgements](https://qupath.readthedocs.io/en/stable/docs/intro/acknowledgements.html).

| Property | Value |
|---|---|
| Full-resolution image | 1868 × 1400 pixels, eight channels |
| Stored component type | float32 |
| X and Y pixel size | Approximately 0.498 µm |
| Component state | Already unmixed; vendor pages record `IsUnmixedComponent=True` |
| Processing input | Original vendor TIFF, analytical series 0 |

The two-image examples also use [`LuCa-7color_[17572,60173]_3x3component_data.tif`](https://downloads.openmicroscopy.org/images/Vectra-QPTIFF/perkinelmer/PKI_fields/LuCa-7color_%5B17572%2C60173%5D_3x3component_data.tif), a 5604 × 4200-pixel field with the same eight-channel panel and approximately 0.498 µm pixel size. These fields use separate image identities within the same subject in the walkthrough.

<a id="tutorials-public-data--prepare-the-vendor-image"></a>
## Prepare the vendor image

Download the original TIFF and add it on **Samples**. Its sole eligible analytical image is selected automatically; the other series is an RGB thumbnail. If a file contains several eligible images, choose the images to add from the selection dialog. Complete [Channels and markers](../stages/images/index.md#user-guide-images-and-channels), retaining the already-unmixed image state and assigning the markers listed below.

Preparation writes the supplied component intensities to OME-Zarr, preserving channel order and physical calibration. The vendor's zero exposure values are treated as unavailable exposure times. Choose whether to display marker names with the channel-name option. [Image processing](../stages/images/processing.md#reference-image-processing) explains how we read and write the image in regions.

<a id="tutorials-public-data--quantitative-panel"></a>
## Quantitative panel

| Position | Original channel | Component | Marker or role |
|---|---|---|---|
| 0 | PDL1 (Opal 520) | Opal 520 | PDL1 |
| 1 | CD8 (Opal 540) | Opal 540 | CD8 |
| 2 | FoxP3 (Opal 570) | Opal 570 | FoxP3 |
| 3 | CD68 (Opal 620) | Opal 620 | CD68 |
| 4 | PD1 (Opal 650) | Opal 650 | PD1 |
| 5 | CK (Opal 690) | Opal 690 | CK |
| 6 | DAPI | DAPI | Nuclear stain |
| 7 | Autofluorescence | Autofluorescence | Estimated background component |

<a id="tutorials-public-data--screenshot-acquisition"></a>
<a id="screenshot-acquisition"></a>
<a id="recorded-example-populations"></a>
<a id="inputs-and-completed-operations"></a>
## Examples in the guides

The LuCa walkthrough illustrates tissue detection, cell segmentation, measurement selection and marker decisions. The guides identify the field, selected population and settings beside each result. Some method comparisons use a separately segmented population from the same source field:

| Example | Population and calculation | Guide |
|---|---|---|
| Full-field measurements and Nimbus | 7,123 cells; whole-cell Mean for the eight channels. Nimbus V1 evaluates PDL1 at 10× objective magnification. | [Measurements](../stages/cell-measurements/index.md#luca-measurement-example), [Nimbus](../stages/phenotyping/nimbus.md) |
| PDL1 mixture comparison | 1,173 cells; the Gaussian and Gamma methods use the same whole-cell PDL1 Mean values. | [Marker distributions](../stages/phenotyping/marker-rules.md#measured-luca-pdl1-example) |
| Two-field marker review | A 512 × 512-pixel analysis region in each source image, containing 834 and 730 cells. | [Marker decisions](../stages/phenotyping/marker-rules.md#review-the-marker-decision) |

<a id="tutorials-public-data--reproducing-the-documented-scope"></a>
<a id="reproducing-the-documented-scope"></a>
## Follow the example

Start with [Images and channels](../stages/images/index.md), then continue through tissue detection, segmentation and measurements. Use the stage guides to choose settings and inspect the results. For the two-image example, the [analysis setup guide](../stages/setup/analysis.md#user-guide-batch-analysis) explains image, subject and processing-group assignments.

---

**Continue:** [Images and channels](../stages/images/index.md).
