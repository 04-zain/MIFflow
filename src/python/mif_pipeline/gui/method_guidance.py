"""Concise method descriptions and the conditions that guide their selection.

Setup forms and review panels share these explanations. Each choice states its
calculation and the observed data pattern or analysis goal that supports its use.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True, slots=True)
class GuidancePoints:
    """Describe a calculation and the interpretation relevant to its selection."""

    method: str
    when_to_use: str

    def items(self) -> tuple[tuple[str, str], ...]:
        """Retain the two descriptive parts for callers that inspect the copy."""
        return (("", self.method), ("", self.when_to_use))

    def text(self) -> str:
        """Return the same explanation for tooltips and assistive technology."""
        return " ".join(part for part in (self.method, self.when_to_use) if part)


THRESHOLD_POINTS = {
    "gmm": GuidancePoints(
        "Fits two normal distributions with a shared variance to cell intensities.",
        "The gate is the intensity where the posterior probability of belonging to the higher-mean population reaches the cutoff (0.5 by default)."),
    "gmm_separate": GuidancePoints(
        "Fits two normal distributions when the weakly and strongly stained populations have different spreads.",
        "The gate uses the posterior probability for the higher-mean population. If it decreases as staining intensifies, we use its upward changes to calculate the gate score."),
    "gamma": GuidancePoints(
        "Fits two Gamma distributions to positive intensities when staining has a long bright tail.",
        "We use the higher-mean population’s posterior probability to set the gate, adjusting the score if the probability decreases as staining intensifies. Zero intensity is scored negative."),
    "triangle": GuidancePoints(
        "Places the gate at the histogram bin furthest from the line joining the dominant peak to its longer tail.",
        "The histogram needs a distinct peak and tail for this calculation."),
    "nimbus": GuidancePoints(
        "Uses the Nimbus model to score marker expression from the marker image and segmented cells.",
        "The score cutoff determines whether a cell is marker-positive. An optional review finds the intensity threshold that best reproduces those positive and negative calls."),
    "manual": GuidancePoints(
        "Applies the intensity gate entered here or placed on the histogram.",
        "Choose the gate using control samples, image review or an established protocol."),
}
VARIANCE_POINTS = {
    "shared": GuidancePoints(
        "Constrains both Gaussian components to the same fitted variance.",
        "Their means and mixture weights remain separate."),
    "unshared": GuidancePoints(
        "Fits a separate variance for each Gaussian component.",
        "The extra flexibility allows different component widths and can change the resulting gate."),
}
SCOPE_POINTS = {
    "pooled": GuidancePoints(
        "Fit one marker intensity model across slides.",
        "The marker's measured intensities should be comparable across slides."),
    "per_slide": GuidancePoints(
        "Fit a separate marker intensity model for each slide.",
        "Each slide receives its own threshold to accommodate differences in staining or background."),
}
CLASSIFIER_POINTS = {
    "rtrees": GuidancePoints(
        "Combines predictions from decision trees trained on different samples of the labelled data.",
        ""),
    "svm": GuidancePoints(
        "Fits boundaries between labelled classes, balancing class separation against errors in the training examples.",
        ""),
    "logistic_regression": GuidancePoints(
        "Fits a weighted combination of the selected measurements to estimate the probability of each class.",
        ""),
}
FEATURE_POINTS = {
    "measurements": GuidancePoints(
        "Use saved marker intensities and cell or nuclear shape measurements.",
        ""),
    "kronos": GuidancePoints(
        "The fixed KRONOS encoder converts each sampled cell crop into a feature vector (embedding).",
        "RF, SVM or logistic regression learns phenotype labels from these embeddings."),
}
PHENOTYPE_POINTS = {
    "marker_rules": GuidancePoints(
        "Assigns positive, negative or unknown marker states from reviewed gates, then applies the phenotype rules in order.",
        "The first matching rule assigns the cell phenotype. Cells that match none receive the label chosen for unmatched cells."),
    "supervised_classifier": GuidancePoints(
        "Learns to assign the reference phenotype labels using cell measurements or image features extracted by KRONOS.",
        ""),
    "unsupervised_clustering": GuidancePoints(
        "Groups cells by their selected measurements using PhenoGraph’s neighbour graph or FlowSOM’s self-organizing map.",
        "Review each cluster’s marker profile and tissue location, then assign its biological label."),
}

PHENOTYPE_HELP = {key: value.text() for key, value in PHENOTYPE_POINTS.items()}
RF_HELP = CLASSIFIER_POINTS["rtrees"].text()
SVM_HELP = CLASSIFIER_POINTS["svm"].text()
def method_guidance_points(name: str, value: object) -> GuidancePoints | None:
    """Return the explanation for the selected form choice."""
    if not isinstance(value, str):
        return None
    if name.endswith("_ignore_policy"):
        return {
            "none": None,
            "annotated": GuidancePoints("Annotate areas to exclude and label them Ignore in QuPath.", ""),
        }.get(value)
    if name == "solver":
        return {
            "nnls": GuidancePoints(
                "Fits fluorophore intensities from the reference spectra, requiring every fitted intensity to be zero or greater.",
                ""),
            "clipped_ls": GuidancePoints(
                "Fits fluorophore intensities by least squares and sets negative estimates to zero.",
                ""),
        }.get(value)
    if name.endswith("_model_method") or name in {"model_method", "classifier", "classifier_method"}:
        if value.startswith("kronos_"):
            base = CLASSIFIER_POINTS.get(value.removeprefix("kronos_"))
            classifier = {"kronos_rtrees": "a random forest", "kronos_svm": "an SVM",
                          "kronos_logistic_regression": "logistic regression"}.get(value)
            if base and name.startswith("supervised_"):
                return GuidancePoints(
                    "KRONOS extracts image features around each nucleus, and " + classifier
                    + " uses labelled cells to learn their phenotypes.", "")
            if base:
                return GuidancePoints(
                    "KRONOS extracts features from image patches, a graph neural network combines information from neighbouring patches, and "
                    + classifier + " learns the tissue classes.", "")
        return CLASSIFIER_POINTS.get(value)
    if name in {"feature_backend", "supervised_feature_backend", "feature_source"} or name.endswith("_feature_backend"):
        return FEATURE_POINTS.get(value)
    if name in {"phenotyping_mode", "mode", "phenotyping_method"}:
        return PHENOTYPE_POINTS.get(value)
    if name == "marker_fit_scope":
        return SCOPE_POINTS.get(value)
    if name.endswith("_gaussian_variance"):
        return VARIANCE_POINTS.get(value)
    if name.startswith("marker_") and name.endswith("_method") or re.fullmatch(r"m\d+(?:_s\d+)?_source", name):
        return THRESHOLD_POINTS.get(value)
    return None


def method_guidance(name: str, value: object) -> str:
    """Return plain-text guidance for a selected method, if one is available."""
    points = method_guidance_points(name, value)
    return points.text() if points else ""


_STAGE_METHODS = {
    "inspection": (
        "We read the image dimensions, pixel sizes and channel metadata. For cyclic images, "
        "review which planes belong to each cycle before assigning markers."
    ),
    "channels": (
        "Channel assignments identify each fluorophore, its marker and the nuclear stain. "
        "For mixed images, specify the components to recover. For separated images, match "
        "the components to their existing channels."
    ),
    "unmixing": (
        "Spectral unmixing estimates how much each reference spectrum contributes to the signal "
        "measured in each pixel. A library can include fluorophores and tissue autofluorescence. "
        "When the bands already isolate the signals, their assigned channels can be used directly."
    ),
    "tissue": (
        "Tissue detection separates the specimen from the slide background. Apply a threshold "
        "to a selected channel or train a random forest or support vector machine on regions "
        "labelled Tissue and Background. Review the resulting tissue sections before continuing."
    ),
    "registration": (
        "Alignment maps the paired image to the mIF pixel grid for display and annotation transfer. "
        "Global alignment fits one transformation across the image. Local refinement uses landmark "
        "pairs or a B-spline transformation to correct residual differences across the tissue."
    ),
    "regions": (
        "Analysis regions define where classification and cell segmentation run. "
        "Use each accepted tissue section in full or draw smaller regions within it."
    ),
    "classification": (
        "Tissue classifiers learn to distinguish compartments from the staining in labelled reference regions. "
        "Random forest and support vector machine classifiers use pixel intensities and selected "
        "image filters. The KRONOS route uses image patches and their neighbours to derive the "
        "features supplied to either classifier."
    ),
    "segmentation": (
        "Segmentation identifies individual cells or nuclei in the selected tissue. InstanSeg "
        "predicts both outlines. Cellpose-SAM predicts cell boundaries and StarDist predicts "
        "nuclear outlines."
    ),
    "measurements": (
        "Intensity measurements summarize the selected channel within each cell, nucleus or "
        "cytoplasm. Shape measurements describe the geometry of the corresponding outline."
    ),
    "phenotyping": (
        "Phenotyping assigns biological labels to individual cells. Marker rules use reviewed "
        "marker decisions. Supervised classifiers learn from labelled cells. PhenoGraph or "
        "FlowSOM groups cells by their measurements for review and naming."
    ),
    "structures": (
        "A tissue classifier detects candidate regions for each structure. Train one classifier "
        "across the structures or a separate classifier for each. Review the detected outlines "
        "with their regional measurements and assign subtypes when applicable."
    ),
}


def stage_guidance(key: str) -> str:
    """Return the method summary shown on the Run card."""
    return _STAGE_METHODS.get(key, "")
