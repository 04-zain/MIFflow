"""

Organizes image inspection, channel review and the saved choices needed to resume them.

We first select the analytical image from the source file and read its OME
description and pyramid dimensions. Channel review then records each
fluorescence component and biomarker, followed by the choice to use biomarker
labels as the processed image's channel names. These operations update
metadata before whole-slide pixel processing begins.

The same workflow record is used by the desktop form and command-line
operations. It retains the selected image, a copy of the reviewed metadata,
the channel rows and the next required decision. We derive those views from
the metadata together so saving cannot combine channel rows from one review
with image settings from another.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import Literal

from ome_types import OME, from_xml

from mif_pipeline.io import (
    BiomarkerAssignmentRow,
    BioFormatsBackend,
    BioFormatsSource,
    CanonicalImageSelection,
    CanonicalIngestionError,
    InspectedSourceAuthority,
    SeriesInfo,
    ComponentIdentityMode,
    apply_biomarker_assignment_rows,
    open_image,
    resolve_biomarker_assignment_rows,
    resolve_component_identity_inventory,
    resolve_canonical_image_selection,
    selected_canonical_ome,
)

from .scan import (
    ScanStructureReport,
    build_scan_structure_report,
    write_stage1_outputs,
)

# ==============================================================================
#                                    Workflow
# ==============================================================================


@dataclass(frozen=True, slots=True)
class ComponentReviewRow:
    """An output identity and its marker choice, independent of source band indices."""
    component_name: str
    biomarker: str | None
    assignment: Literal["assigned", "none", "missing"]


@dataclass(frozen=True, slots=True)
class Stage1Workflow:
    """

    Retains the selected image and the current channel-review decisions.

    The source's original image and series identifiers remain in selection,
    while metadata and series present that chosen image at position zero for
    review. Metadata is copied during construction, allowing unfinished edits
    to be discarded without changing the previously completed review.

    Attributes:
        source_path: Absolute path of the inspected source image.
        metadata: OME model for the selected analytical image and its reviewed
            channel/acquisition annotations.
        series: Single selected reader series with its native pyramid levels,
            presented as series zero within this workflow.
        selection: Original image/series indices and identifiers used to reopen
            the analytical image from the vendor file.
        report: Image and channel summary calculated from metadata and series.
        biomarker_rows: Existing per-channel metadata, used for separated input review.
        component_rows: Ordered output fluorescence identities and marker decisions;
            independent of the acquired band count for raw input.
        status: The next decision: complete channel review, select channel-name
            replacement, or proceed with a ready review.
        replace_channel_names_with_biomarkers: Final channel-naming choice,
            or None while that choice remains unfinished.
    """

    source_path: Path
    metadata: OME
    series: tuple[SeriesInfo, ...]
    selection: CanonicalImageSelection
    report: ScanStructureReport
    biomarker_rows: tuple[BiomarkerAssignmentRow, ...]
    component_rows: tuple[ComponentReviewRow, ...]
    status: Literal[
        "needs_biomarkers",
        "needs_channel_name_replacement",
        "ready",
    ]
    replace_channel_names_with_biomarkers: bool | None


@dataclass(frozen=True, slots=True)
class Stage1Checkpoint:
    """

    Records the completed analytical-image selection and channel-naming choice.

    The saved selection identifies the original image inside the source file,
    including Image and Pixels IDs. A series can legitimately have no name;
    we retain its empty name together with its nonempty image identifiers.
    The saved OME XML carries the associated component and biomarker decisions.

    Attributes:
        schema_version: Current saved format, 2.
        source_path: Absolute filename used for the original inspection.
        replace_channel_names_with_biomarkers: Whether assigned biomarker
            labels replace the channel display names in output metadata.
        source_image_index: Original OME image position.
        source_series_index: Original Bio-Formats series position used for pixels.
        source_image_id: Selected OME Image ID.
        source_pixels_id: Selected OME Pixels ID.
        source_series_name: Original series name, including an empty string for
            an unnamed series.
    """

    schema_version: int
    source_path: Path
    replace_channel_names_with_biomarkers: bool
    source_image_index: int | None = None
    source_series_index: int | None = None
    source_image_id: str | None = None
    source_pixels_id: str | None = None
    source_series_name: str | None = None

    def to_dict(self) -> dict[str, object]:
        """

        Collects the completed image selection into the current saved JSON fields.

        Returns:
            Dictionary containing the source path, all original image identifiers
            and the channel-naming choice.

        Raises:
            ValueError: The record requests a format other than the current version.
        """

        if self.schema_version != 2:
            raise ValueError("stage1-checkpoint current writer is '2'")
        result: dict[str, object] = {
            "schema_version": self.schema_version,
            "source_path": str(self.source_path),
            "replace_channel_names_with_biomarkers": (self.replace_channel_names_with_biomarkers),
        }
        result.update(
            {
                "source_image_index": self.source_image_index,
                "source_series_index": self.source_series_index,
                "source_image_id": self.source_image_id,
                "source_pixels_id": self.source_pixels_id,
                "source_series_name": self.source_series_name,
            }
        )
        return result


def inspect_wsi(
    source_path: str | Path,
    *,
    backend: BioFormatsBackend | None = None,
    image_index: int | None = None,
    series_index: int | None = None,
) -> Stage1Workflow:
    """

    Opens the source, selects its analytical image and prepares the channel-review workflow.

    We inspect image-series descriptions with the planar-channel requirement
    used for mIF. An unambiguous analytical image can be selected automatically;
    explicit image and series positions resolve a file containing several eligible
    images. The selected OME and pyramid descriptions are copied before the
    source reader closes, so metadata review does not retain an idle image reader.

    Args:
        source_path: Vendor or standardized source filename.
        backend: Optional configured BioFormatsBackend used to open the image.
        image_index: Optional original OME image position selected for analysis.
        series_index: Optional matching native reader-series position.

    Returns:
        Stage1Workflow with the selected metadata, channel rows and next
        required review decision.

    Raises:
        ValueError: Image selection is ambiguous or incompatible with mIF review.
    """

    resolved_source = Path(source_path).expanduser().resolve()
    with open_image(resolved_source, backend=backend) as source:
        try:
            selection = resolve_canonical_image_selection(
                source.metadata,
                source.series,
                image_index=image_index,
                series_index=series_index,
                require_planar_channels=True,
            )
        except CanonicalIngestionError as exc:
            raise ValueError(f"stage-one image selection failed: {exc}") from exc
        metadata = selected_canonical_ome(source.metadata, selection)
        selected_series = replace(
            source.series[selection.source_series_index],
            index=0,
        )
        return _build_workflow(
            resolved_source,
            metadata,
            (selected_series,),
            selection,
        )


def inspect_opened_wsi(
    source_path: str | Path,
    source: BioFormatsSource,
    *,
    image_index: int | None = None,
    series_index: int | None = None,
    allow_pending_organization: bool = False,
) -> tuple[Stage1Workflow, InspectedSourceAuthority]:
    """

    Prepares metadata review from a source reader that is already open.

    After selecting the analytical image, we build the same channel-review
    workflow used by inspect_wsi(). We also retain the reader's source checksum,
    complete OME description, original series list and software versions. Later
    steps can recover this inspection without repeating the source metadata read.

    Args:
        source_path: Filename associated with the open reader.
        source: Open BioFormatsSource with its recorded file identity.
        image_index: Optional original OME image position.
        series_index: Optional matching reader-series position.

    Returns:
        Pair containing the review workflow and its recorded source inspection.
        The caller remains responsible for closing the supplied reader.

    Raises:
        TypeError: source is not a BioFormatsSource.
        ValueError: The source path differs or the selected image is incompatible.
    """

    resolved_source = Path(source_path).expanduser().resolve()
    if not isinstance(source, BioFormatsSource):
        raise TypeError("opened Stage-one inspection requires BioFormatsSource")
    if source.identity.resolved_path != resolved_source:
        raise ValueError("opened source identity differs from requested source path")
    try:
        selection = resolve_canonical_image_selection(
            source.metadata,
            source.series,
            image_index=image_index,
            series_index=series_index,
            require_planar_channels=True,
            allow_pending_organization=allow_pending_organization,
        )
    except CanonicalIngestionError as exc:
        raise ValueError(f"stage-one image selection failed: {exc}") from exc
    metadata = selected_canonical_ome(source.metadata, selection)
    selected_series = replace(
        source.series[selection.source_series_index],
        index=0,
    )
    workflow = _build_workflow(
        resolved_source,
        metadata,
        (selected_series,),
        selection,
    )
    authority = InspectedSourceAuthority.create(
        source_identity=source.identity,
        canonical_ome=source.metadata,
        series=source.series,
        selection=selection,
        backend_versions=source.versions,
    )
    return workflow, authority


def stage1_workflow_from_inspection(
    authority: InspectedSourceAuthority,
) -> Stage1Workflow:
    """

    Restores the channel-review workflow from a recorded source inspection.

    We parse its saved OME XML, select the recorded analytical image and present
    its native-series description at position zero. The workflow summary and
    channel rows are then recalculated from that selected metadata.

    Args:
        authority: InspectedSourceAuthority containing the source selection,
            OME XML, native series and reader versions saved during inspection.

    Returns:
        Stage1Workflow reconstructed without another source image opening.

    Raises:
        TypeError: The supplied inspection has an incompatible type.
        ValueError: Its image selection or metadata cannot form a valid review.
    """

    if not isinstance(authority, InspectedSourceAuthority):
        raise TypeError("Stage-one reconstruction requires inspected authority")
    metadata = selected_canonical_ome(authority.canonical_ome, authority.selection)
    selected_series = replace(
        authority.series[authority.selection.source_series_index],
        index=0,
    )
    return _build_workflow(
        authority.source_identity.resolved_path,
        metadata,
        (selected_series,),
        authority.selection,
    )


def apply_stage1_channel_content(workflow: Stage1Workflow, content: str) -> Stage1Workflow:
    """Record raw bands or reviewed separated components at their source boundary."""
    from mif_pipeline.io.metadata.spectral import confirmed_raw_band_metadata, confirmed_unmixed_copy_metadata

    if content == "raw":
        metadata = confirmed_raw_band_metadata(workflow.metadata)
        replacement = workflow.replace_channel_names_with_biomarkers
    elif content == "unmixed" and workflow.status == "ready":
        metadata = confirmed_unmixed_copy_metadata(workflow.metadata, user_declared=True)
        replacement = workflow.replace_channel_names_with_biomarkers
    else:
        raise ValueError("Separated components require a completed component and marker review")

    return _build_workflow(
        workflow.source_path, metadata, workflow.series, workflow.selection,
        replace_channel_names_with_biomarkers=replacement,
    )


def apply_stage1_channel_cycles(workflow: Stage1Workflow, values) -> Stage1Workflow:
    """Review acquired-channel cycles independently of output component identities."""
    from mif_pipeline.io.metadata.cycles import apply_channel_cycle_review
    if not isinstance(values, (list, tuple)):
        raise ValueError("Acquired-channel cycle review must be a list")
    if not values:
        return workflow
    metadata = apply_channel_cycle_review(workflow.metadata, values)
    return _build_workflow(workflow.source_path, metadata, workflow.series, workflow.selection,
        replace_channel_names_with_biomarkers=workflow.replace_channel_names_with_biomarkers)


def apply_stage1_component_values(workflow: Stage1Workflow, values) -> Stage1Workflow:
    """Save a completed raw output panel while retaining every acquired band."""
    from mif_pipeline.io.metadata.canonical import apply_component_identity_inventory, is_autofluorescence_component
    from mif_pipeline.io.metadata.calibration import recorded_autofluorescence_subtracted
    from mif_pipeline.io.metadata.spectral import resolve_spectral_state
    from mif_pipeline.io.metadata.cycles import resolve_channel_cycles, split_cycle_component, cycle_component_name
    if resolve_spectral_state(workflow.metadata, 0).state != "raw":
        raise ValueError("Independent component review requires raw measurement bands")
    if not isinstance(values, (list, tuple)) or not values or len(values) > 512:
        raise ValueError("Specify the output fluorescence components")
    names, markers = [], []
    for row in values:
        if not isinstance(row, Mapping) or set(row) != {"component_name", "assignment", "biomarker"}:
            raise ValueError("Each component needs a name and an explicit marker decision")
        marker, assignment = row["biomarker"], row["assignment"]
        if assignment not in {"assigned", "none"}:
            raise ValueError("Choose an assigned marker or no marker for every component")
        if assignment == "assigned" and (not isinstance(marker, str) or not marker.strip()):
            raise ValueError("Assigned markers need a name")
        if assignment == "none" and marker is not None:
            raise ValueError("No-marker components require biomarker=None")
        names.append(row["component_name"])
        markers.append(marker.strip() if isinstance(marker, str) else None)
    if any(not isinstance(name, str) or not name.strip() for name in names):
        raise ValueError("Every fluorescence component needs a name")
    channel_cycles = resolve_channel_cycles(workflow.metadata, require_complete=True)
    cycles = tuple(dict.fromkeys(channel_cycles)) if channel_cycles is not None else ()
    declared_cycles = tuple(split_cycle_component(name)[1] for name in names)
    cyclic = len(cycles) > 1 or any(cycle is not None for cycle in declared_cycles)
    if cyclic and (not cycles or any(cycle not in cycles for cycle in declared_cycles)):
        raise ValueError("Assign every output component to a recorded imaging cycle, for example Cy5 [cycle 1]")
    af_indices = [i for i, name in enumerate(names) if is_autofluorescence_component(name)]
    af_cycles = [declared_cycles[i] for i in af_indices]
    if len(af_cycles) != len(set(af_cycles)):
        raise ValueError("The output panel must contain at most one autofluorescence component per cycle")
    if not any(not is_autofluorescence_component(name) for name in names):
        raise ValueError("Specify at least one fluorescence component in addition to autofluorescence")
    retain_af = recorded_autofluorescence_subtracted(workflow.metadata) is not True
    if not retain_af and af_indices:
        raise ValueError("The source metadata records AF already removed; omit the AF output component")
    if any(markers[i] is not None for i in af_indices):
        raise ValueError("Autofluorescence has no biological marker")
    if retain_af:
        retained = {declared_cycles[i]: names[i] for i in af_indices}
        markers = [marker for i, marker in enumerate(markers) if i not in af_indices]
        names = [name for i, name in enumerate(names) if i not in af_indices]
        for cycle in cycles if cyclic else (None,):
            names.append(retained.get(cycle, "AF" if cycle is None else cycle_component_name("AF", cycle)))
            markers.append(None)
    previous = resolve_component_identity_inventory(workflow.metadata, 0)
    reviewed = previous is not None and previous.nuclear_stain_reviewed
    nuclear = None
    if reviewed and previous.nuclear_component_index is not None:
        identity = previous.component_names[previous.nuclear_component_index]
        if names.count(identity) == 1:
            nuclear = names.index(identity)
        else:
            reviewed = False
    metadata = workflow.metadata.model_copy(deep=True)
    # Replacing a reviewed panel is an explicit edit; preserve the B-axis records.
    annotation_id = "Annotation:ComponentIdentity:0"
    metadata.images[0].annotation_refs = [r for r in metadata.images[0].annotation_refs if r.id != annotation_id]
    metadata.structured_annotations.map_annotations = [a for a in metadata.structured_annotations.map_annotations if a.id != annotation_id]
    from mif_pipeline.io.backends.errors import MetadataCanonicalizationError
    try:
        metadata = apply_component_identity_inventory(
            metadata, 0, names, mode=ComponentIdentityMode.PLANNED_INVENTORY,
            component_biomarkers=tuple(markers),
            nuclear_component_index=nuclear, nuclear_stain_reviewed=reviewed,
        )
    except MetadataCanonicalizationError as exc:
        raise ValueError(str(exc)) from exc
    return _build_workflow(workflow.source_path, metadata, workflow.series, workflow.selection)


def apply_stage1_nuclear_stain(workflow: Stage1Workflow, identity: str | None) -> Stage1Workflow:
    """Return the review with its DNA-stain role saved in the existing OME inventory.

    Raw panels identify the output component by name; separated images use the
    current channel ID. None explicitly records that no nuclear stain is used.
    """
    from mif_pipeline.io.metadata.canonical import apply_nuclear_component_choice
    from mif_pipeline.io.metadata.spectral import resolve_spectral_state
    inventory = resolve_component_identity_inventory(workflow.metadata, 0)
    if inventory is None:
        raise ValueError("Complete the channel and marker table before choosing a nuclear stain")
    identities = (inventory.component_names if resolve_spectral_state(workflow.metadata, 0).state == "raw"
                  else inventory.channel_ids)
    if identity is not None and (type(identity) is not str or identities.count(identity) != 1):
        raise ValueError("Choose an available nuclear stain in Channels and markers")
    metadata = apply_nuclear_component_choice(workflow.metadata, 0,
        None if identity is None else identities.index(identity))
    return _build_workflow(workflow.source_path, metadata, workflow.series, workflow.selection,
        replace_channel_names_with_biomarkers=workflow.replace_channel_names_with_biomarkers)


def apply_stage1_biomarkers(
    workflow: Stage1Workflow,
    rows: Iterable[BiomarkerAssignmentRow],
) -> Stage1Workflow:
    """

    Validates the completed channel rows and creates the next review state.

    Each source channel must have an explicit component and biomarker decision.
    We compare its ID, original display name and fluorophore with the source
    rows before applying those choices to an OME copy. The resulting workflow
    then asks whether assigned biomarkers should become output display names.

    Args:
        workflow: Current selected-image review.
        rows: Complete BiomarkerAssignmentRow values for its displayed channels.

    Returns:
        Updated Stage1Workflow retaining the original source selection.

    Raises:
        MetadataMappingError: A channel is missing, unreviewed or inconsistent,
            or a component/biomarker decision conflicts with the source.
    """

    from mif_pipeline.io.metadata.canonical import CHANNEL_METADATA_NAMESPACE

    metadata = workflow.metadata.model_copy(deep=True)
    # A completed researcher review replaces earlier marker choices, just as
    # a raw component-panel review does. Acquisition identities stay unchanged.
    replaced = {annotation.id for annotation in metadata.structured_annotations.map_annotations
                if annotation.namespace == CHANNEL_METADATA_NAMESPACE}
    for image in metadata.images:
        for channel in image.pixels.channels:
            channel.annotation_refs = [ref for ref in channel.annotation_refs if ref.id not in replaced]
    metadata.structured_annotations.map_annotations = [
        annotation for annotation in metadata.structured_annotations.map_annotations
        if annotation.id not in replaced
    ]
    metadata = apply_biomarker_assignment_rows(metadata, rows)
    return _build_workflow(
        workflow.source_path,
        metadata,
        workflow.series,
        workflow.selection,
        replace_channel_names_with_biomarkers=None,
    )


def apply_stage1_channel_name_replacement(
    workflow: Stage1Workflow,
    *,
    replace_channel_names_with_biomarkers: bool,
) -> Stage1Workflow:
    """

    Applies the reviewed choice to keep source display names or replace them with biomarker labels.

    When replacement is selected, each channel with an assigned biomarker receives
    that biomarker as Channel.Name. Channels carrying an explicit no-marker choice
    retain their existing display name. The fluorescence-component declarations
    and biomarker annotations remain separately available for unmixing and analysis.

    Args:
        workflow: Channel review waiting for its image-wide naming choice.
        replace_channel_names_with_biomarkers: True to use assigned biomarkers
            as display names, or False to retain the existing names.

    Returns:
        Ready Stage1Workflow with the naming choice recorded.

    Raises:
        ValueError: Channel review is incomplete or the choice is not boolean.
    """

    if workflow.status != "needs_channel_name_replacement":
        raise ValueError(
            "channel-name replacement is available only after complete biomarker review"
        )
    if not isinstance(replace_channel_names_with_biomarkers, bool):
        raise ValueError("channel-name replacement choice must be boolean")
    metadata = workflow.metadata.model_copy(deep=True)
    from mif_pipeline.io.metadata.spectral import resolve_spectral_state
    if replace_channel_names_with_biomarkers and resolve_spectral_state(metadata, 0).state != "raw":
        for row in resolve_biomarker_assignment_rows(metadata):
            if row.biomarker_assignment == "assigned":
                assert row.biomarker is not None
                metadata.images[row.image_index].pixels.channels[
                    row.channel_index
                ].name = row.biomarker
    return _build_workflow(
        workflow.source_path,
        metadata,
        workflow.series,
        workflow.selection,
        replace_channel_names_with_biomarkers=replace_channel_names_with_biomarkers,
    )


def apply_stage1_biomarker_values(
    workflow: Stage1Workflow,
    values: Mapping[str, Mapping[str, object]],
) -> Stage1Workflow:
    """

    Converts entered channel decisions into the reviewed rows used by metadata validation.

    The form identifies each row by its image/channel position, such as "0:2".
    We require exactly the displayed row set, decode its assigned/no-marker choice
    and retain the source-channel fields already present in the workflow. A supplied
    component_name updates the editable component field; its agreement with source
    metadata is checked when the rows are applied.

    Args:
        workflow: Selected-image review containing the displayed source rows.
        values: Mapping from image:channel keys to assignment, biomarker and
            optional component_name values.

    Returns:
        Updated workflow after applying the same component and marker checks
        used by CSV review.

    Raises:
        ValueError: Rows, field names or marker-state values are incomplete or invalid.
        MetadataMappingError: The completed values disagree with source-channel
            identities or contain inconsistent assignments.
    """

    if not isinstance(values, Mapping):
        raise ValueError("stage-one biomarker values must be a mapping")
    expected = {f"{row.image_index}:{row.channel_index}": row for row in workflow.biomarker_rows}
    if set(values) != set(expected):
        raise ValueError("stage-one biomarker values must cover every displayed channel")
    resolved: list[BiomarkerAssignmentRow] = []
    for row_id, row in expected.items():
        value = values[row_id]
        if not isinstance(value, Mapping) or not {
            "assignment",
            "biomarker",
        }.issubset(value):
            raise ValueError("each biomarker response requires assignment and biomarker")
        unexpected = set(value) - {"assignment", "biomarker", "component_name"}
        if unexpected:
            raise ValueError(
                "each biomarker response has unsupported fields: " + ", ".join(sorted(unexpected))
            )
        assignment = value["assignment"]
        if assignment not in {"assigned", "none"}:
            raise ValueError("biomarker assignment must be assigned or none")
        biomarker = value["biomarker"]
        if assignment == "assigned":
            if not isinstance(biomarker, str) or not biomarker.strip():
                raise ValueError("assigned biomarker must be non-empty text")
            biomarker = biomarker.strip()
        elif biomarker is not None:
            raise ValueError("none biomarker assignment requires biomarker=None")
        resolved.append(
            replace(
                row,
                biomarker=biomarker,
                biomarker_assignment=assignment,
                component_name=value.get("component_name", row.component_name),
            )
        )
    return apply_stage1_biomarkers(workflow, resolved)


def write_stage1_workflow(
    workflow: Stage1Workflow,
    output_dir: str | Path,
    *,
    overwrite: bool = False,
) -> dict[str, str]:
    """

    Saves the selected image metadata, review tables and completed naming decision.

    Before writing, we recalculate the image summary, channel rows and readiness
    from the workflow's OME model. They must match the values being saved. This
    catches a form or caller holding rows from an earlier edit after metadata
    has changed.

    The output includes OME XML, the inspection summary, channel/biomarker/series
    CSV files and the original analytical-image selection. A ready workflow also
    receives stage1-checkpoint.json containing its final naming choice. Pending
    channel review can be saved for inspection, but has no completed checkpoint.

    Args:
        workflow: Current selected-image review and its derived summary.
        output_dir: Folder receiving the inspection files.
        overwrite: Whether the writer may replace the existing inspection files.

    Returns:
        Dictionary mapping each written output name to its filename.

    Raises:
        ValueError: The workflow's stored rows, summary or status differ from
            its current metadata.
        OSError: Writing or replacing an output fails.
    """

    expected = _build_workflow(
        workflow.source_path,
        workflow.metadata,
        workflow.series,
        workflow.selection,
        replace_channel_names_with_biomarkers=(workflow.replace_channel_names_with_biomarkers),
    )
    if (
        workflow.source_path != expected.source_path
        or workflow.selection != expected.selection
        or workflow.report != expected.report
        or workflow.biomarker_rows != expected.biomarker_rows
        or workflow.component_rows != expected.component_rows
        or workflow.status != expected.status
        or (
            workflow.replace_channel_names_with_biomarkers
            != expected.replace_channel_names_with_biomarkers
        )
    ):
        raise ValueError("Stage-one workflow is internally inconsistent with its metadata")
    checkpoint = (
        Stage1Checkpoint(
            schema_version=2,
            source_path=workflow.source_path,
            replace_channel_names_with_biomarkers=(workflow.replace_channel_names_with_biomarkers),
            source_image_index=workflow.selection.source_image_index,
            source_series_index=workflow.selection.source_series_index,
            source_image_id=workflow.selection.image_id,
            source_pixels_id=workflow.selection.pixels_id,
            source_series_name=workflow.selection.series_name,
        )
        if workflow.status == "ready"
        else None
    )
    return write_stage1_outputs(
        workflow.metadata,
        workflow.series,
        workflow.report,
        output_dir,
        overwrite=overwrite,
        stage1_selection={
            "schema_version": 1,
            "source_image_index": workflow.selection.source_image_index,
            "source_series_index": workflow.selection.source_series_index,
            "source_image_id": workflow.selection.image_id,
            "source_pixels_id": workflow.selection.pixels_id,
            "source_series_name": workflow.selection.series_name,
        },
        stage1_checkpoint=(checkpoint.to_dict() if checkpoint is not None else None),
    )


def read_stage1_checkpoint(output_dir: str | Path) -> Stage1Checkpoint:
    """

    Reads the completed image selection and channel-naming choice needed to resume review.

    We require the current record's source filename, original image and series
    positions, Image and Pixels IDs, literal series name and naming choice.
    An unnamed series remains an empty string; its numerical selection and
    nonempty image identifiers still establish which analytical image was used.

    Args:
        output_dir: Folder containing stage1-checkpoint.json.

    Returns:
        Stage1Checkpoint with the saved source selection and naming decision.

    Raises:
        ValueError: The file is missing, the format is unsupported, fields are
            incomplete or their path/index/identifier types are invalid.
    """

    path = Path(output_dir).expanduser().resolve() / "stage1-checkpoint.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"The saved image inspection is missing: {path}") from None
    except json.JSONDecodeError as error:
        raise ValueError(f"The saved image inspection is not valid JSON: {path}") from error
    fields = {"schema_version", "source_path", "replace_channel_names_with_biomarkers",
        "source_image_index", "source_series_index", "source_image_id", "source_pixels_id", "source_series_name"}
    if not isinstance(payload, dict) or set(payload) != fields or type(payload["schema_version"]) is not int or payload["schema_version"] != 2:
        raise ValueError("Repeat image inspection to record the selected analytical image using the current format.")
    for name in ("source_image_index", "source_series_index"):
        if type(payload[name]) is not int or payload[name] < 0:
            raise ValueError("The saved image and series indices must be nonnegative integers.")
    for name in ("source_image_id", "source_pixels_id"):
        if not isinstance(payload[name], str) or not payload[name].strip():
            raise ValueError("The saved image inspection is missing its image identifiers.")
    if not isinstance(payload["source_series_name"], str):
        raise ValueError("The saved source-series name must be text; an unnamed series is recorded as an empty string.")
    source_path = payload["source_path"]
    if not isinstance(source_path, str) or not Path(source_path).is_absolute():
        raise ValueError("The saved source-image path must be absolute.")
    choice = payload["replace_channel_names_with_biomarkers"]
    if type(choice) is not bool:
        raise ValueError("Confirm whether biomarker names replace channel display names.")
    return Stage1Checkpoint(2, Path(source_path), choice, payload["source_image_index"],
        payload["source_series_index"], payload["source_image_id"], payload["source_pixels_id"], payload["source_series_name"])


def resume_stage1_workflow(
    source_path: str | Path,
    output_dir: str | Path,
    *,
    backend: BioFormatsBackend | None = None,
) -> Stage1Workflow:
    """

    Reopens the selected source image and restores its completed metadata review.

    We read the saved checkpoint and reviewed OME XML, then inspect the recorded
    original image-series pair. Its identifiers and dimensions must still agree.
    The source display names and fluorophores are used when reapplying the saved
    component/biomarker decisions, after which the saved naming choice is applied.

    Args:
        source_path: Original image filename selected for this analysis.
        output_dir: Folder containing the completed inspection and OME XML.
        backend: Optional reader used to reopen the source.

    Returns:
        Ready Stage1Workflow reconstructed through the same checks used for a
        newly completed channel review.

    Raises:
        ValueError: The saved source, selection, image dimensions or reviewed
            metadata differ, or the restored review remains incomplete.
    """

    resolved_source = Path(source_path).expanduser().resolve()
    checkpoint = read_stage1_checkpoint(output_dir)
    if checkpoint.source_path != resolved_source:
        raise ValueError("stage-one checkpoint source identity does not match the requested input")
    metadata_path = Path(output_dir).expanduser().resolve() / "metadata.ome.xml"
    try:
        reviewed_metadata = from_xml(metadata_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"stage-one reviewed metadata does not exist: {metadata_path}") from None
    except Exception as exc:
        raise ValueError(f"stage-one reviewed metadata is invalid: {metadata_path}") from exc
    source_workflow = inspect_wsi(
        resolved_source,
        backend=backend,
        image_index=checkpoint.source_image_index,
        series_index=checkpoint.source_series_index,
    )
    if (
        checkpoint.source_image_id != source_workflow.selection.image_id
        or checkpoint.source_pixels_id != source_workflow.selection.pixels_id
        or checkpoint.source_series_name != source_workflow.selection.series_name
    ):
        raise ValueError("stage-one checkpoint selected image identity does not match the source")
    _validate_resume_snapshot(
        source_workflow.metadata,
        reviewed_metadata,
        replace_channel_names_with_biomarkers=(checkpoint.replace_channel_names_with_biomarkers),
    )
    from mif_pipeline.io.metadata.cycles import resolve_channel_cycle_review
    reviewed_cycles = resolve_channel_cycle_review(reviewed_metadata)
    if reviewed_cycles is not None:
        source_workflow = apply_stage1_channel_cycles(source_workflow, [
            dict(channel_id=channel.id, cycle=cycle) for channel, cycle in
            zip(reviewed_metadata.images[0].pixels.channels, reviewed_cycles, strict=True)])
    from mif_pipeline.io.metadata.spectral import resolve_spectral_state
    if resolve_spectral_state(reviewed_metadata, 0).state == "raw":
        source_workflow = apply_stage1_channel_content(source_workflow, "raw")
    if resolve_spectral_state(reviewed_metadata, 0).state == "raw":
        inventory = resolve_component_identity_inventory(reviewed_metadata, 0)
        if inventory is None or inventory.component_biomarkers is None:
            raise ValueError("Saved raw review lacks component marker decisions")
        reviewed = apply_stage1_component_values(source_workflow, [
            {"component_name": name, "assignment": "none" if marker is None else "assigned", "biomarker": marker}
            for name, marker in zip(inventory.component_names, inventory.component_biomarkers, strict=True)
        ])
        if inventory.nuclear_stain_reviewed:
            reviewed = apply_stage1_nuclear_stain(reviewed, None if inventory.nuclear_component_index is None
                else inventory.component_names[inventory.nuclear_component_index])
        return apply_stage1_channel_name_replacement(reviewed,
            replace_channel_names_with_biomarkers=checkpoint.replace_channel_names_with_biomarkers)
    reviewed_rows = resolve_biomarker_assignment_rows(reviewed_metadata)
    source_named_rows = tuple(
        replace(
            row,
            channel_name=source_workflow.metadata.images[row.image_index]
            .pixels.channels[row.channel_index]
            .name,
            fluorophore=source_workflow.metadata.images[row.image_index]
            .pixels.channels[row.channel_index]
            .fluor,
        )
        for row in reviewed_rows
    )
    reviewed = apply_stage1_biomarkers(source_workflow, source_named_rows)
    inventory = resolve_component_identity_inventory(reviewed_metadata, 0)
    if inventory is not None and inventory.nuclear_stain_reviewed:
        reviewed = apply_stage1_nuclear_stain(reviewed, None if inventory.nuclear_component_index is None
            else inventory.channel_ids[inventory.nuclear_component_index])
    ready = apply_stage1_channel_name_replacement(
        reviewed,
        replace_channel_names_with_biomarkers=(checkpoint.replace_channel_names_with_biomarkers),
    )
    if ready.status != "ready":
        raise ValueError("stage-one checkpoint did not restore a ready workflow")
    from mif_pipeline.io.metadata.spectral import resolve_prepared_channel_order
    if (resolve_spectral_state(reviewed_metadata, 0).state == "unmixed"
            or resolve_prepared_channel_order(reviewed_metadata, 0) is not None):
        ready = apply_stage1_channel_content(ready, "unmixed")
    return ready


def _build_workflow(
    source_path: str | Path,
    metadata: OME,
    series: tuple[SeriesInfo, ...],
    selection: CanonicalImageSelection,
    *,
    replace_channel_names_with_biomarkers: bool | None = None,
) -> Stage1Workflow:
    """

    Builds the image summary, channel rows and next decision from one OME copy.

    We require the selected analytical image at position zero and verify its
    Image/Pixels identifiers against selection. Biomarker review is complete
    when every channel is assigned or intentionally has no marker, with no
    duplicated named marker. Component review is complete when the explicit
    per-channel inventory covers the same ordered Channel IDs and reviewed labels.
    Dye metadata is optional and is checked separately by the inventory resolver.

    Incomplete component or marker decisions lead to needs_biomarkers. Once those
    are complete, an unanswered naming choice leads to needs_channel_name_replacement;
    a recorded boolean choice makes the workflow ready.

    Args:
        source_path: Original image filename.
        metadata: Selected OME model to copy for this workflow.
        series: Single selected native series, presented at index zero.
        selection: Original vendor-image/series identifiers.
        replace_channel_names_with_biomarkers: Recorded naming choice, or None
            when it has not yet been supplied.

    Returns:
        Stage1Workflow with mutually consistent metadata, summary, rows and status.

    Raises:
        ValueError: The selected image count or identifiers disagree.
    """

    resolved_source = Path(source_path).expanduser().resolve()
    metadata_snapshot = metadata.model_copy(deep=True)
    series_snapshot = tuple(series)
    _validate_stage1_topology(metadata_snapshot, series_snapshot)
    if (
        not isinstance(selection, CanonicalImageSelection)
        or metadata_snapshot.images[0].id != selection.image_id
        or metadata_snapshot.images[0].pixels.id != selection.pixels_id
    ):
        raise ValueError("stage-one selected image identity differs from metadata")
    report = build_scan_structure_report(
        resolved_source,
        metadata_snapshot,
        series_snapshot,
    )
    rows = resolve_biomarker_assignment_rows(metadata_snapshot)
    component_inventory = resolve_component_identity_inventory(metadata_snapshot, 0)
    biomarkers_complete = (
        bool(rows)
        and all(row.biomarker_assignment != "missing" for row in rows)
        and _duplicate_non_null_biomarker(metadata_snapshot, rows) is None
    )
    components_complete = (
        bool(rows)
        and component_inventory is not None
        and component_inventory.mode is ComponentIdentityMode.CHANNEL_FLUOROPHORES
        and tuple(row.channel_id for row in rows) == component_inventory.channel_ids
        and tuple(row.component_name for row in rows) == component_inventory.component_names
    )
    from mif_pipeline.io.metadata.spectral import resolve_spectral_state, _resolve_source_spectral_state
    from mif_pipeline.io.metadata.canonical import resolve_unmixing_input_metadata, is_autofluorescence_component
    from mif_pipeline.io.metadata.calibration import recorded_autofluorescence_subtracted
    state, _ = _resolve_source_spectral_state(metadata_snapshot, 0,
        spectral=resolve_spectral_state(metadata_snapshot, 0),
        acquisition=resolve_unmixing_input_metadata(metadata_snapshot, 0))
    raw = state == "raw"
    from mif_pipeline.io.metadata.cycles import resolve_channel_cycles, cycle_component_name, split_cycle_component
    channel_cycles = resolve_channel_cycles(metadata_snapshot)
    cycles = tuple(dict.fromkeys(c for c in channel_cycles if c is not None)) if channel_cycles else ()
    cyclic = len(cycles) > 1
    if component_inventory is not None:
        names = component_inventory.component_names
        markers = component_inventory.component_biomarkers
        if markers is None and component_inventory.mode is ComponentIdentityMode.CHANNEL_FLUOROPHORES:
            component_rows = tuple(ComponentReviewRow(row.component_name, row.biomarker, row.biomarker_assignment) for row in rows)
        else:
            component_rows = tuple(ComponentReviewRow(name, markers[i] if markers is not None else None,
                "missing" if markers is None else "none" if markers[i] is None else "assigned") for i, name in enumerate(names))
    elif state == "unmixed":
        component_rows = tuple(ComponentReviewRow(row.fluorophore, row.biomarker, row.biomarker_assignment)
                               for row in rows if row.fluorophore)
    else:
        # Several acquired bands may record the same fluorophore. The planned
        # output panel has one row per fluorophore, while source rows retain every band.
        declared: dict[tuple[int | None, str], ComponentReviewRow] = {}
        for row in rows:
            if not row.fluorophore:
                continue
            cycle = channel_cycles[row.channel_index] if cyclic else None
            name = cycle_component_name(row.fluorophore, cycle) if cycle is not None else row.fluorophore
            component = ComponentReviewRow(name, row.biomarker, row.biomarker_assignment)
            key = (cycle, "AF" if is_autofluorescence_component(row.fluorophore) else row.fluorophore)
            previous = declared.setdefault(key, component)
            if (previous.biomarker, previous.assignment) != (component.biomarker, component.assignment):
                # Conflicting or incomplete marker declarations need one explicit choice.
                declared[key] = ComponentReviewRow(previous.component_name, None, "missing")
        component_rows = tuple(declared.values())
    if state != "unmixed":
        af_rows = tuple(row for row in component_rows if is_autofluorescence_component(row.component_name))
        af_by_cycle = {split_cycle_component(row.component_name)[1]: row for row in af_rows}
        if len(af_by_cycle) != len(af_rows):
            raise ValueError("The declared output panel has multiple autofluorescence identities in one cycle")
        component_rows = tuple(row for row in component_rows if not is_autofluorescence_component(row.component_name))
        if recorded_autofluorescence_subtracted(metadata_snapshot) is not True:
            for cycle in cycles if cyclic else (None,):
                name = af_by_cycle[cycle].component_name if cycle in af_by_cycle else (
                    "AF" if cycle is None else cycle_component_name("AF", cycle))
                component_rows += (ComponentReviewRow(name, None, "none"),)
    if raw:
        biomarkers_complete = bool(component_rows) and all(row.assignment != "missing" for row in component_rows)
        components_complete = component_inventory is not None and component_inventory.mode is ComponentIdentityMode.PLANNED_INVENTORY
    if not biomarkers_complete or not components_complete:
        status: Literal[
            "needs_biomarkers",
            "needs_channel_name_replacement",
            "ready",
        ] = "needs_biomarkers"
        replacement = None
    elif replace_channel_names_with_biomarkers is None:
        status = "needs_channel_name_replacement"
        replacement = None
    else:
        status = "ready"
        replacement = replace_channel_names_with_biomarkers
    return Stage1Workflow(
        source_path=resolved_source,
        metadata=metadata_snapshot,
        series=series_snapshot,
        selection=selection,
        report=report,
        biomarker_rows=rows,
        component_rows=component_rows,
        status=status,
        replace_channel_names_with_biomarkers=replacement,
    )


def _validate_stage1_topology(ome: OME, series: tuple[SeriesInfo, ...]) -> None:
    """

    Requires the single analytical image selected for channel review.

    Args:
        ome: Selected OME model.
        series: Corresponding reader-series sequence.

    Raises:
        ValueError: Either description contains other than one image/series.
    """

    if len(ome.images) != 1 or len(series) != 1:
        raise ValueError(
            "stage-one biomarker workflow requires exactly one selected OME image/series"
        )


def _validate_resume_snapshot(
    source_ome: OME,
    reviewed_ome: OME,
    *,
    replace_channel_names_with_biomarkers: bool,
) -> None:
    """

    Checks that saved channel decisions still describe the inspected source image.

    Image and Pixels IDs, X/Y/Z/C/T dimensions, dimension order, dtype and channel
    IDs must agree. A completed component record must account for reviewed Fluor
    values. We then check marker uniqueness and derive the expected display name
    for each channel from the saved naming choice.

    Args:
        source_ome: Newly inspected source metadata.
        reviewed_ome: OME model saved after component and biomarker review.
        replace_channel_names_with_biomarkers: Saved choice determining each
            expected Channel.Name.

    Raises:
        ValueError: The source geometry, ordered channel IDs, component review,
            marker uniqueness or expected display names differ.
    """

    if len(source_ome.images) != len(reviewed_ome.images):
        raise ValueError("stage-one reviewed metadata image topology differs from source")
    pixel_fields = (
        "size_x",
        "size_y",
        "size_z",
        "size_c",
        "size_t",
        "dimension_order",
        "type",
    )
    for image_index, (source_image, reviewed_image) in enumerate(
        zip(source_ome.images, reviewed_ome.images)
    ):
        inventory = resolve_component_identity_inventory(
            reviewed_ome,
            image_index,
        )
        from mif_pipeline.io.metadata.spectral import resolve_spectral_state
        if resolve_spectral_state(reviewed_ome, image_index).state != "raw" and (
            inventory is None or inventory.mode is not ComponentIdentityMode.CHANNEL_FLUOROPHORES
        ):
            raise ValueError(
                "stage-one reviewed metadata requires a typed per-channel " "component assertion"
            )
        if source_image.id != reviewed_image.id:
            raise ValueError(
                f"stage-one reviewed metadata image identity differs at image {image_index}"
            )
        source_pixels = source_image.pixels
        reviewed_pixels = reviewed_image.pixels
        if source_pixels.id != reviewed_pixels.id or any(
            getattr(source_pixels, field) != getattr(reviewed_pixels, field)
            for field in pixel_fields
        ):
            raise ValueError(
                f"stage-one reviewed metadata pixel topology differs at image {image_index}"
            )
        if len(source_pixels.channels) != len(reviewed_pixels.channels):
            raise ValueError(
                f"stage-one reviewed metadata channel topology differs at image {image_index}"
            )
        for channel_index, (source_channel, reviewed_channel) in enumerate(
            zip(source_pixels.channels, reviewed_pixels.channels)
        ):
            if source_channel.id != reviewed_channel.id:
                raise ValueError(
                    "stage-one reviewed metadata channel identity differs at "
                    f"image {image_index}, channel {channel_index}"
                )
            if source_channel.fluor != reviewed_channel.fluor:
                inventory = resolve_component_identity_inventory(
                    reviewed_ome,
                    image_index,
                )
                if (
                    inventory is None
                    or inventory.mode is not ComponentIdentityMode.CHANNEL_FLUOROPHORES
                    or not inventory.channel_fluor_promoted
                    or reviewed_channel.fluor != inventory.component_names[channel_index]
                ):
                    raise ValueError(
                        "stage-one reviewed Channel.Fluor change lacks an explicit "
                        "per-channel component assertion"
                    )
    reviewed_rows = resolve_biomarker_assignment_rows(reviewed_ome)
    duplicate_biomarker = _duplicate_non_null_biomarker(reviewed_ome, reviewed_rows)
    if duplicate_biomarker is not None:
        raise ValueError(
            f"stage-one reviewed metadata has duplicate biomarker " f"{duplicate_biomarker!r}"
        )
    raw = resolve_spectral_state(reviewed_ome, 0).state == "raw"
    for row in reviewed_rows:
        source_name = source_ome.images[row.image_index].pixels.channels[row.channel_index].name
        expected_name = source_name
        if not raw and replace_channel_names_with_biomarkers and row.biomarker_assignment == "assigned":
            assert row.biomarker is not None
            expected_name = row.biomarker
        if row.channel_name != expected_name:
            raise ValueError(
                "stage-one reviewed metadata Channel.Name does not match the "
                "checkpoint replacement choice"
            )


def _duplicate_non_null_biomarker(
    ome: OME,
    rows: Iterable[BiomarkerAssignmentRow],
) -> str | None:
    """Find marker conflicts using the actual channel-bound cycle metadata."""
    from mif_pipeline.io.metadata.biomarkers import duplicate_channel_biomarker
    duplicate = duplicate_channel_biomarker(ome, rows)
    return None if duplicate is None else duplicate[0]
