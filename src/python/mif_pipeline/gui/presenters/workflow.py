"""Scientific stage vocabulary and graph projection; no execution decisions.

The `WorkflowStageDefinition` record carries `key`, `title`, `description`, `user_input`,
`outputs` and its remaining declared fields.

The presenter converts the supplied typed results into display rows and action values. The
current stage or batch service remains responsible for validating the response and
publishing the accepted scientific operation."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
from dataclasses import dataclass
import re
from mif_pipeline.orchestration.batch_workflow import BatchWorkflowNode

# ==============================================================================
#                                    Workflow
# ==============================================================================


@dataclass(frozen=True, slots=True)
class WorkflowStageDefinition:
    key: str
    title: str
    description: str
    user_input: str
    outputs: str
    lane: int
    kinds: tuple[str, ...]


STAGES = (
    WorkflowStageDefinition(
        "inspection",
        "Image inspection",
        "Identify the image series, channels and pixel sizes to use for each sample.",
        "Select the series containing the tissue if the file contains several possible images.",
        "Verified image inventory and image metadata.",
        0,
        ("inspection",),
    ),
    WorkflowStageDefinition(
        "channels",
        "Channels & markers",
        "Identify the fluorophores, their markers and the nuclear stain.",
        "Enter the components to recover from mixed bands or match the existing channels to their markers.",
        "A reviewed channel and marker assignment.",
        0,
        ("channel_review",),
    ),
    WorkflowStageDefinition(
        "unmixing",
        "Spectral unmixing",
        "Prepare the fluorophore channels used for analysis and open them in QuPath.",
        "Separate mixed spectral bands using reference spectra, or prepare existing component and marker channels with their intensities preserved.",
        "Analysis-ready OME-Zarr images and a linked QuPath project.",
        0,
        ("reference_unmixing", "analysis_preflight", "project_upsert"),
    ),
    WorkflowStageDefinition(
        "tissue",
        "Tissue detection",
        "Separate tissue from the slide background and select the sections to analyse.",
        "Choose a detection method and review the outlines of the tissue sections it identifies.",
        "Accepted tissue sections for downstream analysis.",
        1,
        (
            "tissue_setup",
            "tissue_resolution",
            "tissue_channel",
            "tissue_candidate",
            "tissue_acceptance",
        ),
    ),
    WorkflowStageDefinition(
        "registration",
        "Image alignment",
        "Match the paired image to the mIF image, first globally and then with optional local refinement.",
        "Open the alignment editor for manual positioning or automatic global fitting. Review the overlay, then refine local tissue differences with landmarks or automatic B-spline fitting.",
        "An accepted alignment for comparing the paired image with mIF.",
        1,
        ("paired_image_preparation", "registration"),
    ),
    WorkflowStageDefinition(
        "regions",
        "Analysis regions",
        "Choose whole tissue sections or draw smaller regions for analysis.",
        "Choose whole tissue or selected regions; create or select annotations in QuPath when requested.",
        "An explicit, accepted analysis area.",
        1,
        ("analysis_domain_selection",),
    ),
    WorkflowStageDefinition(
        "classification",
        "Tissue classification",
        "Identify tissue compartments within the analysis regions from staining in the selected image channels.",
        "Choose a classifier, annotate representative tissue in QuPath and review the resulting class map. Keep training and evaluation subjects separate when assessing accuracy.",
        "Primary tissue classes within the accepted analysis area.",
        1,
        (
            "primary_classifier_training_plan_review",
            "primary_training_evidence_snapshot",
            "primary_classifier_training_review",
            "primary_classifier_inference",
        ),
    ),
    WorkflowStageDefinition(
        "segmentation",
        "Cell segmentation",
        "Locate cells or nuclei in the selected tissue and review their outlines.",
        "Choose the area to segment. InstanSeg uses the model from Software setup and the processor selected in Computing.",
        "Cell objects and their spatial boundaries.",
        2,
        ("segmentation", "segmentation_correction"),
    ),
    WorkflowStageDefinition(
        "measurements",
        "Cell measurements",
        "Measure staining in selected parts of each cell and calculate features of cell or nuclear shape.",
        "Choose the required measurements when prompted. Measurement computation is automatic.",
        "Cell-level measurements for phenotyping and export.",
        2,
        ("measurements",),
    ),
    WorkflowStageDefinition(
        "saved_result_retirement",
        "Updating revised results",
        "Retire affected saved cell selections, generated regions and automatic displays before revised work begins.",
        "Continue saved work completes the update. If saving stops, Details identifies the recorded file or saved-state problem.",
        "Historical results and manual edits remain saved after current selections are retired.",
        2,
        ("saved_result_retirement",),
    ),
    WorkflowStageDefinition(
        "phenotyping",
        "Cell phenotyping",
        "Assign cell phenotypes using marker rules, a supervised classifier or clustering.",
        "Choose marker rules, supervised classification or clustering. Complete only the thresholds, annotations or label reviews required by that route.",
        "Reviewed cell phenotypes with their selected method and settings.",
        2,
        (
            "phenotyping_mode_resolution",
            "marker_gate_review",
            "marker_combination_selection",
            "marker_rule_application",
            "supervised_phenotype_training_plan_review",
            "supervised_training_evidence_snapshot",
            "supervised_phenotype_training_review",
            "supervised_phenotype_inference",
            "unsupervised_preparation",
            "clustering_per_slide",
            "clustering_pooled",
            "cluster_review_evidence",
            "cluster_label_review",
            "clustering_label_import",
        ),
    ),
    WorkflowStageDefinition(
        "structures",
        "Secondary structures",
        "Detect structures from tissue staining and review their outlines alongside cell counts, measurements and phenotypes.",
        "Define the structures and their detection areas, fit their classifiers, then review the outlines and selected measurements to accept regions and assign subtypes.",
        "Included structures, subtype assignments and their member cells.",
        2,
        (
            "secondary_structure_definition",
            "secondary_classifier_training_plan_review",
            "secondary_training_evidence_snapshot",
            "secondary_classifier_training_review",
            "secondary_classifier_inference",
            "no_secondary_completion",
            "semantic_raw_inventory_snapshot",
            "phenotype_hierarchy_attestation",
            "semantic_evidence",
            "semantic_evaluation",
            "final_count_cap",
            "semantic_finalization",
        ),
    ),
    WorkflowStageDefinition(
        "results",
        "Results & export",
        "Publish the accepted results for downstream analysis.",
        "No additional method choice is needed for automatic export.",
        "Exported cell and region results with the settings used to calculate them.",
        2,
        ("phenotype_handoff", "final_non_spatial_export"),
    ),
)
BY_KIND = {kind: stage for stage in STAGES for kind in stage.kinds}


def optional_setup_skip_label(node: BatchWorkflowNode) -> str | None:
    """Offer a shortcut only for an actual pending optional setup request."""
    if node.status != "needs_interaction" or node.deferred:
        return None
    choices = {
        ("primary_classifier_training_plan_review", "training_plan_selection_required"): "Skip classification",
        ("secondary_structure_definition", "other_explicit_reason"): "Skip structures",
    }
    return choices.get((node.kind, node.interaction_reason))


STATUS_LABELS = {
    "failed": "Stopped — see details",
    "needs_interaction": "Needs review",
    "running": "Processing",
    "ready": "Ready",
    "completed": "Completed",
    "planned": "Not yet selected",
    "waiting": "Not yet selected",
    "partial": "Partly completed",
}


@dataclass(frozen=True, slots=True)
class WorkflowStageView:
    definition: WorkflowStageDefinition
    nodes: tuple[BatchWorkflowNode, ...]
    status: str

    @property
    def key(self) -> str:
        return self.definition.key

    @property
    def review_node_ids(self) -> tuple[str, ...]:
        return tuple(
            node.node_id
            for node in self.nodes
            if node.status == "needs_interaction" and not node.deferred and node.kind in BY_KIND
        )

    @property
    def sample_ids(self) -> tuple[str, ...]:
        return tuple(sorted({sample for node in self.nodes for sample in node.sample_ids}))


    @property
    def status_summary(self) -> str:
        """Describe the displayed status and how many samples currently have it.

        We aggregate each sample's stage nodes with the same priority used for
        the overall stage. Batch-wide nodes apply to every participating sample.
        Mixed stages therefore report the affected count instead of attaching
        the total sample count to one sample's review or failure status.
        """
        label = STATUS_LABELS.get(self.status, "Not yet selected")
        total = len(self.sample_ids)
        if total <= 1:
            return label
        affected = sum(
            _stage_status(tuple(node for node in self.nodes
                                if not node.sample_ids or sample in node.sample_ids)) == self.status
            for sample in self.sample_ids
        )
        if 0 < affected < total:
            return f"{label}: {affected} of {total} samples"
        return f"{label} · {total} samples"


@dataclass(frozen=True, slots=True)
class WorkflowGraphView:
    stages: tuple[WorkflowStageView, ...]
    edges: tuple[tuple[str, str], ...]
    planned_edges: tuple[tuple[str, str], ...] = ()


def _stage_status(nodes: tuple[BatchWorkflowNode, ...]) -> str:
    states = {node.status for node in nodes}
    for priority in ("failed", "needs_interaction", "running", "ready"):
        if priority in states:
            return priority
    if all(node.deferred for node in nodes):
        return "planned"
    if states == {"completed"}:
        return "completed"
    if any(node.eligible for node in nodes):
        return "ready"
    return "partial" if "completed" in states else "waiting"


def build_workflow_graph(nodes: tuple[BatchWorkflowNode, ...]) -> WorkflowGraphView:
    if type(nodes) is not tuple or any(type(node) is not BatchWorkflowNode for node in nodes):
        raise TypeError("workflow graph requires immutable workflow records")
    groups: dict[str, list[BatchWorkflowNode]] = {}
    definitions = {stage.key: stage for stage in STAGES}
    # Revision housekeeping stays in execution, status and retry records, but
    # is not a scientific stage for the researcher to configure or review.
    internal = {node.node_id: node for node in nodes if node.kind == "saved_result_retirement"}
    for node in nodes:
        if node.node_id in internal:
            continue
        definition = BY_KIND.get(node.kind)
        if definition is None:
            # Unknown future stages stay visible and read-only rather than disappearing.
            definition = WorkflowStageDefinition(
                node.kind,
                node.kind.replace("_", " ").capitalize(),
                "A processing stage supplied by the current workflow.",
                "This desktop version does not provide a stage-specific form for this step. Retain the saved analysis and inspect Details.",
                "Outputs defined by the current analysis plan.",
                2,
                (node.kind,),
            )
            definitions[node.kind] = definition
        groups.setdefault(definition.key, []).append(node)
    stages = tuple(
        WorkflowStageView(definition, tuple(groups[key]), _stage_status(tuple(groups[key])))
        for key, definition in definitions.items()
        if key in groups
    )
    by_node = {node.node_id: stage.key for stage in stages for node in stage.nodes}
    edges = set()
    for stage in stages:
        for node in stage.nodes:
            if node.deferred:
                continue
            parents, seen, pending = set(), set(), list(node.dependency_ids)
            while pending:
                parent = pending.pop()
                if parent in seen:
                    continue
                seen.add(parent)
                if parent in internal:
                    pending.extend(internal[parent].dependency_ids)
                elif parent in by_node:
                    parents.add(by_node[parent])
            edges.update((parent, stage.key) for parent in parents if parent != stage.key)
    # Remove visually redundant links only when an alternative directed path exists.
    # Never invent adjacency from the catalogue's display ordering.
    reduced = set(edges)
    for edge in sorted(edges):
        remaining = reduced - {edge}
        seen, pending = set(), [edge[0]]
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            pending.extend(b for a, b in remaining if a == current)
        if edge[1] in seen:
            reduced.discard(edge)
    # Deferred records describe planning gates, not a fully instantiated DAG.
    # Show their declared scientific sequence as explicitly planned links, never
    # pass these display-only links to the scheduler or use them as eligibility.
    known = {definition.key for definition in STAGES}
    ordered = [stage for stage in stages if stage.key in known]
    planned = {
        (a.key, b.key)
        for a, b in zip(ordered, ordered[1:])
        if any(node.deferred for node in b.nodes) and (a.key, b.key) not in reduced
    }
    return WorkflowGraphView(stages, tuple(sorted(reduced)), tuple(sorted(planned)))


def failure_guidance(code: str | None, message: str | None, *, stage_kind: str | None = None, source_correction_available: bool = False) -> str:
    detail = f"{code or ''} {message or ''}".lower()
    if code == "segmentation_administratively_abandoned":
        return (
            "The prior local process is no longer present. Accepted tissue and primary regions "
            "have been verified and restored, and the interrupted state was backed up. "
            "The old segmentation remains unaccepted and its exit is unknown. "
            "Open Segmentation options and apply your saved settings to start a new attempt."
        )
    if code == "preprocessing_attributeerror" and "correction_mode" in (message or ""):
        return (
            "An internal application error prevented the spectral-reference choices from opening. "
            "The image and channel choices have been saved. Update the application, then "
            "reopen the analysis and its stopped step. Copy Details to report the application error."
        )
    if (code or "").lower().endswith(("attributeerror", "keyerror", "indexerror")):
        return (
            "The application encountered an unexpected internal error. "
            "The diagnostic does not identify a correction to your settings. "
            "Use Copy details to report the error."
        )
    if "unresolved project mutation" in detail:
        return (
            "An earlier QuPath operation has not finished or its result has not yet been recorded. "
            "If QuPath is still processing this analysis, wait for it to finish. "
            "Then refresh Run to check the recorded operation. "
            "Keep the analysis folder unchanged until its status is confirmed."
        )
    if any(
        term in detail
        for term in (
            "project control authority differs",
            "project changed outside",
        )
    ):
        return (
            "The saved analysis no longer matches its QuPath project. Retain the project and detector drafts, "
            "then open a complete analysis backup or create a new analysis from the original images."
        )
    if code == "preprocessing_memoryerror":
        return (
            "The application calculation ran out of working memory. Close unused applications or image windows. "
            "Details contains the original memory error; this step's controls show its available recovery actions."
        )
    # Launch diagnostics echo -XX:+ExitOnOutOfMemoryError even when the
    # process failed for a different reason. Match the exception itself.
    if re.search(r"\boutofmemoryerror\b", detail) or any(
        term in detail for term in ("java heap space", "gc overhead limit",
                                    "cuda out of memory", "cuda error: out of memory")
    ):
        if stage_kind == "segmentation":
            return ("Segmentation ran out of working memory. Close unused applications or image windows. "
                    "Change segmentation settings… lets you reduce Simultaneous tiles or Tile size (pixels). "
                    "Preview the affected steps before applying the change.")
        return ("QuPath ran out of working memory. Close unused applications or image windows. "
                "Details contains the original memory error; this step's controls show its available recovery actions.")
    reported = ""
    if code in {"qupath_project_stage_incomplete", "native_classifier_fit_failed"}:
        reported = next((line.partition("QuPath reported: ")[2] for line in (message or "").splitlines()
                         if line.startswith("QuPath reported: ")), "")
        if reported:
            reported = re.sub(r"^(?:Caused by:\s*)?(?:[\w$]+\.)*[\w$]*(?:Exception|Error):\s*", "", reported)
            reported = re.sub(r" in [\w./-]+\.groovy at line number \d+.*$", "", reported)
        if reported and code == "qupath_project_stage_incomplete":
            return ("QuPath could not complete this step.\n\n" + reported
                    + "\n\nOpen Details to copy the full error report.")
    if code == "preprocessing_dependencyversionerror" or (code or "").lower().startswith(("bioformats", "bffile", "payload_absent", "dependencyversion", "java_")):
        return (
            "An image-reading software check failed. Open Details to identify the failed check, "
            "then correct Java or Bio-Formats in Software setup."
        )
    if code == "native_classifier_fit_failed" or stage_kind in {
        "primary_classifier_training_plan_review", "secondary_classifier_training_plan_review",
        "supervised_phenotype_training_plan_review", "primary_classifier_training_review",
        "secondary_classifier_training_review", "supervised_phenotype_training_review",
    }:
        cause = "Classifier fitting stopped.\n\n" + reported + "\n\n" if reported else ""
        return (cause + "Open Details for the recorded cause. Change classifier setup… reopens the saved classifier "
                "and subject evaluation choices. Preview the affected steps before applying a correction.")
    if stage_kind in {"segmentation", "measurements"}:
        action = "Change segmentation settings…" if stage_kind == "segmentation" else "Change cell measurements…"
        return (f"Open Details for the recorded cause. Use {action} to review this section's saved choices, "
                "then preview the affected steps before applying a correction.")
    if "pinned zarr-python 3" in detail:
        return ("The image writer requires the pinned Zarr-Python 3 installation. Correct the reported software installation, "
                "then reopen this stopped step. Completed inspection and channel review are retained.")
    if source_correction_available:
        return ("Open Details for the recorded cause. To correct the original image or analytical series, use "
                "Change source image… and Inspect image, then preview the affected steps before applying the correction.")
    if "spectrallibraryerror" in detail:
        return "The spectral library could not be matched to this image. Completed steps are retained. Open Details for the failed library check."
    if stage_kind == "saved_result_retirement":
        return ("Open Details to identify the affected image entry and saved-file problem. "
                "Restore write access or free disk space only when the recorded error identifies that problem. "
                "Keep the analysis folder unchanged while resolving the reported error.")
    if stage_kind in {"tissue_setup", "tissue_resolution", "tissue_candidate"}:
        return ("Open Details for the recorded cause. Select Review tissue settings… to review or change the detector and image resolution. "
                "Saving changed choices repeats the affected steps.")
    if stage_kind == "analysis_domain_selection":
        return ("Open Details for the recorded cause. Use Change analysis regions… to review whole tissue "
                "or selected regions. Preview the affected steps before applying a correction.")
    if stage_kind == "registration":
        return ("Open Details for the recorded cause. Use Change paired image or alignment… to reopen the existing paired-image "
                "choices, then preview the affected steps before applying a correction. "
                "The configured images are retained; previous transforms and manual annotations stay saved as history.")
    return ("We could not determine why this step stopped. "
            "Open Details to inspect the original error, or use Copy details to report the problem.")
