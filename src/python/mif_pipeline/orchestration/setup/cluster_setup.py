"""

Creates the initial analysis settings from the samples selected for cluster processing.

We send the complete sample definitions to the cluster, including subject,
slide, validation group and any paired image. The cluster checks these paths
and builds the same unresolved analysis configuration used for a new local
analysis. Channels, detectors and classifiers are selected at their workflow
steps after image inspection.

The resulting analysis.manifest.json is created inside the chosen cluster
analysis folder. Repeating an identical request reuses that file; a different
request cannot replace the existing analysis under the same name. Creating
these settings checks file availability without reading whole-slide pixels.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
from pathlib import Path
import hashlib, json, os, re, tempfile
from mif_pipeline.pipeline_core.config_draft import unresolved_analysis_settings
from ..batch_manifest import BatchManifest, validate_manifest, SCHEMA_NAME, SCHEMA_VERSION

MAX_BYTES = 1024 * 1024


# ==============================================================================
#                                 Cluster setup
# ==============================================================================


def parse_setup(text):
    """

    Reads the submitted cluster setup and rejects repeated JSON field names.

    The maximum encoded size is 1 MiB. We apply the duplicate-key check before
    constructing dictionaries, because ordinary JSON decoding would retain only
    the last value of a repeated key. NaN and infinity are also rejected before
    sample paths and numerical settings are validated.

    Args:
        text: UTF-8 JSON text received by the cluster setup command.

    Returns:
        Decoded setup value for prepare_cluster_analysis().

    Raises:
        ValueError: Text is oversized, invalid JSON, contains a repeated field
            or includes a nonfinite numerical constant.
    """

    if not isinstance(text, str) or len(text.encode()) > MAX_BYTES:
        raise ValueError("The analysis setup exceeds 1 MiB.")

    def unique(pairs):
        """

        Collects one JSON object's fields while checking that each name appears once.

        Args:
            pairs: Ordered key/value pairs provided by the JSON decoder.

        Returns:
            Dictionary containing those unique fields.

        Raises:
            ValueError: A field name is repeated within the object.
        """

        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Repeated setup field")
            result[key] = value
        return result

    return json.loads(
        text,
        object_pairs_hook=unique,
        parse_constant=lambda _v: (_ for _ in ()).throw(ValueError("Nonfinite setup value")),
    )


def _path(value, label):
    """

    Checks that a supplied cluster location is an absolute path.

    We reject parent-directory components so paths in one setup cannot be
    interpreted differently through an unresolved '..' component. Whether the
    required file or directory exists is checked by the calling operation.

    Args:
        value: Entered path text.
        label: Location name used in the error, such as image or results folder.

    Returns:
        Path for the selected absolute location.

    Raises:
        ValueError: The value is not text, is relative or contains '..'.
    """

    if not isinstance(value, str) or not Path(value).is_absolute() or ".." in Path(value).parts:
        raise ValueError("Enter an absolute cluster " + label + " path.")
    return Path(value)


def prepare_cluster_analysis(value):
    """

    Validates the selected samples and writes their initial cluster analysis configuration.

    We require the current complete sample format and check every included source
    image and paired-image path. Each sample keeps its original subject, slide
    and validation-group assignments. The shared configuration is initialized
    with later workflow selections left unresolved, so channel review and model
    choices remain attached to their proper analysis steps.

    Initial processing is limited to one simultaneous pipeline operation.
    Admission uses available host memory and the actual Slurm job allocation,
    with memory reserved for the operating system and process overhead.

    We encode the resolved settings once. If an identical manifest already exists,
    it is reused. Otherwise we validate the new manifest, write and flush a
    temporary file, then link it into its final filename without replacing an
    existing file. The temporary file is removed in either case.

    Args:
        value: Current cluster-analysis-setup/v2 dictionary containing the
            analysis name, cluster output directory, expected manifest path,
            CPU/memory selections and complete included sample definitions.

    Returns:
        Dictionary containing status, manifest path, analysis ID, sample count
        and the checksum of the written settings.

    Raises:
        ValueError: Required setup fields, resource limits, sample definitions
            or source/output paths are invalid.
        FileExistsError: The destination contains different analysis settings.
        OSError: Creating, writing, synchronizing or linking the settings fails.
    """

    required = {
        "schema_version",
        "batch_id",
        "output_root",
        "manifest",
        "cpus",
        "memory_gib",
        "samples",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value["schema_version"] != "cluster-analysis-setup/v2"
    ):
        raise ValueError("Complete the cluster analysis setup fields.")
    batch = value["batch_id"]
    if not isinstance(batch, str) or not re.fullmatch("[A-Za-z0-9][A-Za-z0-9_-]{0,99}", batch):
        raise ValueError(
            "Use letters, numbers, hyphens or underscores for the analysis folder name."
        )
    root = _path(value["output_root"], "results folder")
    destination = _path(value["manifest"], "setup file")
    workspace = root / batch
    if not root.is_dir() or root.is_symlink():
        raise ValueError("Select an existing cluster results folder.")
    if destination != workspace / "analysis.manifest.json":
        raise ValueError(
            "Save the setup as analysis.manifest.json inside the new analysis results folder."
        )
    if workspace.exists() and (workspace.is_symlink() or not workspace.is_dir()):
        raise ValueError("The analysis results folder is unavailable.")
    for field, high in (("cpus", 256), ("memory_gib", 4096)):
        if type(value[field]) is not int or not 1 <= value[field] <= high:
            raise ValueError("Invalid cluster " + field)
    rows = value["samples"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 10000:
        raise ValueError("Add at least one sample to the cluster analysis.")
    samples = []
    for index, row in enumerate(rows):
        from ..batch_manifest import BatchSample

        if isinstance(row, dict) and "source_collection" in row:
            from .aligned_collection import materialize_collection_request
            row = dict(row)
            collection = row.pop("source_collection")
            # Validate all ordinary sample fields before writing its descriptor.
            BatchSample.from_mapping(row, index=index)
            path = materialize_collection_request(collection, workspace)
            row["source_mif_path"] = str(path)
            for field in ("source_image_index", "source_series_index", "source_file_sha256"):
                row.pop(field, None)
        selected = BatchSample.from_mapping(row, index=index)
        image = _path(str(selected.source_mif_path), "image")
        if not selected.enabled:
            raise ValueError(
                "Only included Samples rows can be submitted for cluster preparation."
            )
        if not image.is_file():
            raise ValueError("The cluster image does not exist: " + str(image))
        if selected.reference is not None:
            reference = selected.reference.to_mapping()
            path = reference.get("source_path")
            if path is not None and not _path(path, "paired image").is_file():
                raise ValueError("The paired image does not exist: " + path)
        samples.append(selected.to_mapping())
    config = unresolved_analysis_settings(batch, root, samples[0]["slide_id"])
    resources = (
        "bioformats_conversion",
        "spectral_preprocessing",
        "scientific_compute",
        "qupath_image_read",
        "qupath_image_write",
        "project_catalogue_mutation",
        "shared_classifier_mutation",
    )
    raw = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "batch_id": batch,
        "validation_domain_id": "group-1",
        "output_root": str(root),
        "create_project_path": str(workspace / "qupath/project.qpproj"),
        "shared_analysis_config": config,
        "samples": samples,
        "execution_config": {
            "max_workers": 1,
            "max_in_flight": 1,
            "resource_capacities": {resource: 1 for resource in resources},
        },
    }
    raw["validation_domains"] = sorted(
        {
            "group-1",
            *(
                sample["validation_domain_id"]
                for sample in samples
                if sample.get("validation_domain_id")
            ),
        }
    )
    encoded = json.dumps(raw, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if destination.exists():
        if (
            destination.is_symlink()
            or destination.stat().st_size > MAX_BYTES
            or destination.read_bytes() != encoded
        ):
            raise FileExistsError(
                "A different cluster analysis setup already exists. Use a new analysis name."
            )
    else:
        validate_manifest(BatchManifest.from_json_bytes(encoded))
        workspace.mkdir(exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".analysis-", suffix=".json", dir=workspace)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temp, destination)
        finally:
            Path(temp).unlink(missing_ok=True)
    return {
        "status": "analysis_prepared",
        "manifest": str(destination),
        "batch_id": batch,
        "sample_count": len(samples),
        "manifest_sha256": hashlib.sha256(encoded).hexdigest(),
    }
