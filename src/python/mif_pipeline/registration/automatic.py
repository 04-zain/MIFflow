"""

Prepares automatic affine alignment and optional B-spline refinement for a selected image pair.

We read initial settings from the registration configuration, check the
selected image channels and numerical limits, and locate the installed
Elastix/Transformix programs. The native request retains each image's
full-resolution dimensions and X/Y pixel sizes together with the selected
tissue annotations and both fitting stages' parameters.

QuPath reads the reduced original-image views and runs the calculations.
The returned map is either the global affine or the complete affine-plus-
B-spline transformation. We check its image pair, method, settings and saved
contents before presenting it for review. Temporary intensity and mask images
are removed after each attempt; the parameters and process logs remain with
the proposed alignment.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
from collections.abc import Mapping
from dataclasses import dataclass, asdict, field
from pathlib import Path
import hashlib, json, math, os, shutil, sys, tempfile

from mif_pipeline._defaults import default_section

_ALIGNMENT_DEFAULTS = default_section("registration.automatic")
_BSPLINE_DEFAULTS = default_section("registration.bspline")

# ==============================================================================
#                                   Automatic
# ==============================================================================


@dataclass(frozen=True, slots=True)
class BSplineAlignmentSettings:
    """

    Defines the image resolution and control grid used for automatic local refinement.

    We fit a grid of physical displacement coefficients after global affine
    alignment. Grid spacing determines the size of local structures the deformation
    can follow: reducing it adds control points and permits shorter spatial changes.
    The image-view size independently determines how much pixel detail enters
    the intensity comparison. Both settings are recorded with the fitted result.

    Attributes:
        max_dimension: Maximum width or height of the refinement image view,
            from 128 to 4096 pixels.
        iterations: Maximum optimizer updates at each of three resolutions.
        grid_spacing_um: Final physical distance between control points in µm.
        bending_weight: Relative strength of the bending-energy gradient compared
            with the image-metric gradient, from zero to one.
        maximum_displacement_um: Largest permitted sampled local shift beyond
            the global affine, in micrometres.
        inverse_tolerance_pixels: Required moving-image pixel residual when
            numerically inverting the fitted B-spline, at most 0.1 pixels.
        inverse_max_iterations: Maximum Newton updates per inverse point query.
        maximum_control_points: Upper limit on the two-dimensional control grid.
            There are two fitted displacement coefficients per control point.
    """

    max_dimension: int = _BSPLINE_DEFAULTS["max_dimension"]
    iterations: int = _BSPLINE_DEFAULTS["iterations"]
    grid_spacing_um: float = _BSPLINE_DEFAULTS["grid_spacing_um"]
    bending_weight: float = _BSPLINE_DEFAULTS["bending_weight"]
    maximum_displacement_um: float = _BSPLINE_DEFAULTS["maximum_displacement_um"]
    inverse_tolerance_pixels: float = _BSPLINE_DEFAULTS["inverse_tolerance_pixels"]
    inverse_max_iterations: int = _BSPLINE_DEFAULTS["inverse_max_iterations"]
    maximum_control_points: int = _BSPLINE_DEFAULTS["maximum_control_points"]

    def __post_init__(self):
        """

        Checks the refinement settings before image reading or native optimization.

        Integer fields limit image size, optimizer updates and control-grid
        storage. Floating values must be finite, with positive spacing,
        displacement and inverse tolerance. A zero bending weight explicitly
        removes that penalty's contribution to the optimizer.

        Raises:
            ValueError: A field has an unsupported type or numerical value.
        """

        for name, lower, upper in (
            ("max_dimension", 128, 4096),
            ("iterations", 32, 2048),
            ("inverse_max_iterations", 1, 1000),
            ("maximum_control_points", 16, 262144),
        ):
            value = getattr(self, name)
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"{name} must be an integer from {lower} to {upper}.")
        for name in ("grid_spacing_um", "bending_weight", "maximum_displacement_um", "inverse_tolerance_pixels"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} must be a finite number.")
            if name == "bending_weight":
                if not 0 <= value <= 1:
                    raise ValueError("The bending weight must be between zero and one.")
            elif value <= 0:
                raise ValueError(f"{name} must be positive.")
        if self.inverse_tolerance_pixels > 0.1:
            raise ValueError("The inverse tolerance must be at most 0.1 pixels.")

    @classmethod
    def from_mapping(cls, values):
        """

        Resolves the supplied local-refinement choices using the module defaults.

        Args:
            values: Mapping of supported B-spline setting names to their values.

        Returns:
            Checked BSplineAlignmentSettings for the current fit.

        Raises:
            ValueError: A field name or parameter value is unsupported.
        """

        if not isinstance(values, Mapping) or set(values) - set(cls.__dataclass_fields__):
            raise ValueError("Check the B-spline refinement setting names.")
        return cls(**values)

    def to_mapping(self):
        """

        Collects every resolved local-refinement setting for native execution and saving.

        Returns:
            Dictionary containing the selected image resolution, control grid,
            optimization and inverse-calculation values.
        """

        return asdict(self)


@dataclass(frozen=True, slots=True)
class AutomaticAlignmentSettings:
    """

    Defines automatic affine alignment followed by optional B-spline refinement.

    We compare the selected image channels at reduced resolutions to estimate
    one global affine. When local_refinement is enabled, a second fit uses
    bspline settings to estimate spatially varying displacement from that affine.
    Each stage retains its image resolution and optimizer settings separately.

    Attributes:
        fixed_channel: Zero-based mIF channel index, or -1 for combined intensity.
        moving_channel: Zero-based paired-image channel index; -1 combines
            fluorescence intensity or calculates RGB stain contrast.
        max_dimension: Largest dimension of the reduced global-alignment view.
        iterations: Maximum global optimizer updates per image-pyramid resolution.
        threads: Native Elastix/Transformix thread count, from one to sixteen.
        orientation_search: Whether to compare four initial right-angle orientations.
        local_refinement: Whether to follow the completed affine with a B-spline fit.
        bspline: Checked settings for that optional local stage.
    """

    fixed_channel: int = _ALIGNMENT_DEFAULTS["fixed_channel"]
    moving_channel: int = _ALIGNMENT_DEFAULTS["moving_channel"]
    max_dimension: int = _ALIGNMENT_DEFAULTS["max_dimension"]
    iterations: int = _ALIGNMENT_DEFAULTS["iterations"]
    threads: int = _ALIGNMENT_DEFAULTS["threads"]
    orientation_search: bool = _ALIGNMENT_DEFAULTS["orientation_search"]
    local_refinement: bool = _ALIGNMENT_DEFAULTS["local_refinement"]
    bspline: BSplineAlignmentSettings = field(default_factory=BSplineAlignmentSettings)

    def __post_init__(self):
        """

        Checks selected channels, global fitting limits and the local-stage choice.

        Raises:
            ValueError: A channel or numerical work limit is outside its
                supported interval, or a stage choice is not a boolean.
            TypeError: bspline is not a checked BSplineAlignmentSettings instance.
        """

        for name, lower, upper in (
            ("fixed_channel", -1, 511), ("moving_channel", -1, 511),
            ("max_dimension", 128, 4096), ("iterations", 32, 2048), ("threads", 1, 16),
        ):
            value = getattr(self, name)
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"{name} must be an integer from {lower} to {upper}.")
        if type(self.orientation_search) is not bool or type(self.local_refinement) is not bool:
            raise ValueError("Select whether to compare orientations and apply local refinement.")
        if type(self.bspline) is not BSplineAlignmentSettings:
            raise TypeError("Local refinement requires checked B-spline settings.")

    @classmethod
    def from_mapping(cls, values):
        """

        Constructs the selected automatic route from its saved or entered parameter values.

        We decode the nested B-spline settings before constructing the complete
        route. Unspecified authoring values receive the module defaults; a saved
        run records the complete returned mapping.

        Args:
            values: Global alignment fields and optional nested bspline settings.

        Returns:
            AutomaticAlignmentSettings with both stages checked.

        Raises:
            ValueError: A setting name or numerical value is unsupported.
        """

        if not isinstance(values, Mapping) or set(values) - set(cls.__dataclass_fields__):
            raise ValueError("Check the automatic alignment setting names.")
        fields = dict(values)
        if "bspline" in fields:
            fields["bspline"] = BSplineAlignmentSettings.from_mapping(fields["bspline"])
        return cls(**fields)

    def to_mapping(self):
        """

        Records the complete affine and B-spline configuration passed to QuPath.

        Returns:
            Dictionary containing global settings, the local-refinement choice
            and its complete nested parameter values.
        """

        return asdict(self)


def find_elastix_directory(selected=None):
    """

    Finds a program folder containing both Elastix and Transformix.

    An explicit folder takes precedence, followed by MIF_ELASTIX_DIRECTORY and
    the saved desktop selection. With no saved selection, the function checks
    the managed installation, the Elastix program on PATH and supported local
    tool folders. Each candidate must be absolute and contain both executable
    programs; Windows uses their .exe filenames.

    Args:
        selected: Optional program-folder path from the current setup.
            A leading home-directory marker is expanded for this explicit path.

    Returns:
        Absolute Path to the folder containing the executable pair.

    Raises:
        ValueError: The selected folder is unusable, or no installation can be
            found. The error identifies the required Software setup action.
    """

    suffix = ".exe" if os.name == "nt" else ""
    candidates = []
    if selected:
        candidates.append(Path(selected).expanduser())
    elif os.environ.get("MIF_ELASTIX_DIRECTORY"):
        candidates.append(Path(os.environ["MIF_ELASTIX_DIRECTORY"]))
    else:
        from mif_pipeline.pipeline_core.software_preferences import (
            load_desktop_preferences,
            desktop_preferences_path,
        )
        from mif_pipeline.qupath_runtime.elastix_runtime import default_elastix_root

        preferences = load_desktop_preferences(desktop_preferences_path())
        saved = getattr(preferences, "elastix_directory", None)
        if saved:
            return find_elastix_directory(saved)
        managed = default_elastix_root()
        candidates.append(managed if os.name == "nt" else managed / "bin")
        binary = shutil.which("elastix" + suffix)
        if binary:
            candidates.append(Path(binary).parent)
        candidates.extend(
            (
                Path(sys.executable).absolute().parents[2] / ".tools/elastix-5.3.1/bin",
                Path(__file__).resolve().parents[3] / ".tools/elastix-5.3.1/bin",
            )
        )
    for path in candidates:
        if path.is_absolute() and all(
            (path / (name + suffix)).is_file() and os.access(path / (name + suffix), os.X_OK)
            for name in ("elastix", "transformix")
        ):
            return path
    raise ValueError(
        "Open Software setup and install Elastix and Transformix, or select their extracted program folder using Use existing."
    )


def estimate_candidate(
    *, runner, project_file, snapshot, tissue_object_ids, settings, tools_directory, helpers, driver
):
    """

    Runs automatic registration for the recorded image pair and returns its fitted transform.

    We prepare a new attempt folder and copy the selected image IDs, original
    source locations, dimensions and physical pixel sizes into the request. The
    native driver first fits the global affine and then, when selected, fits
    B-spline displacement using that affine as its initial transform. Tissue
    annotations supplied by the current review restrict the fixed-image metric.

    After the native calculation, we require the returned method to match the
    selected affine-only or affine-plus-B-spline route. The result must also
    contain the requested entries and parameter values, and its candidate file
    must agree with the returned checksum. These checks are completed before
    the caller can offer the transform for acceptance.

    Args:
        runner: QuPath runner used to execute the registered image-fitting script.
        project_file: Project containing the selected mIF and paired-image entries.
        snapshot: Recorded project checksum and fixed_entry/moving_entry values,
            including original source URIs, image dimensions and hexadecimal
            X/Y pixel sizes in µm/pixel.
        tissue_object_ids: Selected fixed-image tissue annotations. An empty
            list uses the complete prepared fixed-image view for fitting.
        settings: Checked AutomaticAlignmentSettings containing both stages'
            parameters and the local-refinement choice.
        tools_directory: Optional selected folder containing Elastix and Transformix.
        helpers: Current Groovy helper names and paths for affine fitting,
            B-spline fitting and shared coordinate calculations.
        driver: Registered script that opens the image pair, performs fitting
            and writes the candidate/result files.

    Returns:
        Candidate JSON path and decoded fitting result, including the method,
        complete parameter values and available coordinate/geometry checks.

    Raises:
        TypeError: settings is not an AutomaticAlignmentSettings instance.
        ValueError: The project changed, programs are unavailable, or the returned
            method, image pair, settings or candidate contents differ.
        OSError: Required files cannot be read, written or removed. The runner
            reports native process failures with the attempt's saved logs.
    """

    if type(settings) is not AutomaticAlignmentSettings:
        raise TypeError("Expected explicit alignment settings")
    project = Path(project_file)
    if hashlib.sha256(project.read_bytes()).hexdigest() != snapshot["qpproj_sha256"]:
        raise ValueError("The project changed before automatic alignment")
    folder = project.parent / "data" / "automatic-alignment"
    folder.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="candidate-", dir=folder))
    # Recover X/Y physical calibration separately for both images.
    # Elastix uses these spacings to interpret the reduced image views.
    entries = {}
    for role in ("fixed", "moving"):
        row = snapshot[role + "_entry"]
        entries[role] = {
            "project_entry_id": row["project_entry_id"],
            "source_uri": row["source_uri"],
            "width": row["level0_size_x_px"],
            "height": row["level0_size_y_px"],
            "x_um": float.fromhex(row["x_um_per_px_hex"]),
            "y_um": float.fromhex(row["y_um_per_px_hex"]),
        }
    request = {
        "schema": "automatic-alignment/v2",
        "project_file": str(project),
        "project_sha256": snapshot["qpproj_sha256"],
        "work_directory": str(directory),
        "entries": entries,
        "tissue_object_ids": list(tissue_object_ids),
        "settings": settings.to_mapping(),
        "tools_directory": str(find_elastix_directory(tools_directory)),
        "helpers": {
            name: {"path": str(path), "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()}
            for name, path in helpers.items()
        },
    }
    request_path = directory / "request.json"
    request_path.write_text(json.dumps(request, sort_keys=True, allow_nan=False))
    result_path = directory / "result.json"
    try:
        runner.run_evidence_script(
            project_file=project,
            script=driver,
            request_paths=(request_path,),
            log_directory=directory,
            require_existing_project=False,
            evidence_path=result_path,
        )
        result = json.loads(result_path.read_text())
        expected_method = "elastix-affine-bspline" if settings.local_refinement else "elastix-multiresolution-affine"
        if result.get("method") != expected_method:
            raise ValueError("The returned transform does not match the selected automatic registration route.")
        candidate = directory / "candidate.json"
        if (
            result.get("schema") != "automatic-alignment-result/v2"
            or result.get("candidate_path") != str(candidate)
            or result.get("fixed_entry_id") != entries["fixed"]["project_entry_id"]
            or result.get("moving_entry_id") != entries["moving"]["project_entry_id"]
            or result.get("settings") != settings.to_mapping()
            or candidate.is_symlink()
            or not candidate.is_file()
            or candidate.stat().st_size > 16 * 1024 * 1024
        ):
            raise ValueError("The automatic alignment result does not match this image pair")
        # Check the contents of the candidate identified by this result
        # before returning it to the alignment review.
        payload = candidate.read_bytes()
        if hashlib.sha256(payload).hexdigest() != result.get("candidate_sha256"):
            raise ValueError("The automatic alignment changed before publication")
        if hashlib.sha256(project.read_bytes()).hexdigest() != snapshot["qpproj_sha256"]:
            raise ValueError("The project changed during automatic alignment")
        return candidate, result
    finally:
        # Keep logs, exact settings and transformation parameters; remove temporary image arrays.
        for path in directory.rglob("*.mha"):
            if path.is_file() and not path.is_symlink():
                path.unlink()
