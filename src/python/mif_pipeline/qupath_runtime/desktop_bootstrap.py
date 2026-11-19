"""

Prepares the QuPath opening used by the current analysis step.

We open a specific project with the current annotation, prediction and
paired-image controls. The request names that project and its installed
Groovy scripts, then includes the module settings used to initialize the
native editors. A detector draft can also select its image and method;
a paired-image request identifies the two entries and the required view.

Registered scripts are kept in versioned directories beside each project.
When an existing pipeline project needs the current editing controls, we
can add that current directory while retaining the scripts associated with
its preceding accepted processing steps. Opening controls and rewriting
accepted analysis results remain separate operations.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
import base64
from dataclasses import dataclass
import json
from pathlib import Path
from .project import ProjectLayout
from .scripts import (
    authenticate_registered_script_bundle,
    registered_script_bundle_sha256,
    bundle_script_path,
    QuPathScriptAsset,
)

# ==============================================================================
#                               Desktop bootstrap
# ==============================================================================


@dataclass(frozen=True, slots=True)
class DesktopBootstrap:
    """

    Describes the project and native startup script used to open one QuPath session.

    Attributes:
        project_path: Absolute QuPath project filename to open.
        script_path: Current registered desktop-startup Groovy script.
        bundle_root: Directory containing the helper scripts selected for this opening.
        bundle_sha256: Checksum identifying their recorded script list and contents.
    """

    project_path: Path
    script_path: Path
    bundle_root: Path
    bundle_sha256: str

    def encoded_request(
        self, paired_views: tuple[dict, ...] = (), *, reference_display=None, tissue_authoring=None, image_appearance_path=None, classifier_review=None, feature_inspection=None, reference_authoring=None, inspection=None
    ) -> str:
        """

        Encodes the selected QuPath view and editor settings into one launch argument.

        We read tissue-editor, automatic-registration and local-registration
        defaults from their module JSON files. The tissue editor's initial physical
        pixel size is taken from the same tissue setting used by the application.
        These resolved values accompany the project, so Groovy uses the settings
        selected by this process rather than another set of hard-coded defaults.

        Optional view choices add only the fields required by that operation. A
        detector opening includes its entry and method. Paired-image views include
        fixed/moving entry IDs and, for candidate or landmark review, the matching
        transform or session checksum. The complete JSON is encoded with URL-safe
        Base64 so it can be supplied as one process argument.

        Args:
            paired_views: Recorded accepted image-pair displays available in this project.
            reference_display: Optional paired-image request selecting overlay,
                side-by-side, guided alignment, candidate preview or landmark editing.
            tissue_authoring: Optional detector request containing entry_id and
                method: threshold, rf or svm.
            image_appearance_path: Analysis-local display preferences shared with
                QuPath. Channel colours and contrast affect viewing and image export.
            feature_inspection: Image, filters and physical sampling for the shared
                feature editor; Apply writes the chosen recipe to its result path.
            classifier_review: Selected fitting image and saved classifier to open
                in the native prediction reviewer.

        Returns:
            Encoded text consumed by the registered QuPath desktop-startup script.

        Raises:
            ValueError: A requested view, image identifier, detector method, program
                path or required transform/session checksum is invalid.
        """

        from mif_pipeline._defaults import default_section

        # Resolve native-editor starting values from the same module
        # files used by the Python forms, then carry them into QuPath.
        tissue_defaults = default_section("tissue_detection.editor")
        tissue_defaults["target_pixel_size_um"] = default_section("tissue_detection")["resolution_um_per_px"]
        data = {
            "project_file": str(self.project_path),
            "bundle_root": str(self.bundle_root),
            "bundle_sha256": self.bundle_sha256,
            "editor_defaults": {
                "tissue": tissue_defaults,
                "registration": default_section("registration.automatic"),
                "bspline": default_section("registration.bspline"),
                "manual_registration": default_section("registration.manual"),
            },
        }
        if inspection is not None:
            data["inspection"] = dict(inspection)
        if feature_inspection is not None:
            data["feature_inspection"] = dict(feature_inspection)
        if classifier_review is not None:
            data["classifier_review"] = dict(classifier_review)
        if reference_authoring is not None:
            import re
            from urllib.parse import urlparse

            entries = reference_authoring.get('entries') if isinstance(reference_authoring, dict) else None
            fields = {'entry_id', 'input_id', 'source_uri', 'role', 'image_name'}
            if (not isinstance(reference_authoring, dict)
                    or set(reference_authoring) != {'entries', 'initial_entry_id'}
                    or not isinstance(entries, list) or not 1 <= len(entries) <= 1024
                    or any(not isinstance(entry, dict) or set(entry) != fields
                           or any(not isinstance(entry[key], str) or not entry[key].strip()
                                  for key in fields)
                           or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', entry['entry_id'])
                           or entry['role'] not in {'positive', 'af', 'negative'}
                           or urlparse(entry['source_uri']).scheme != 'file'
                           or not urlparse(entry['source_uri']).path.startswith('/')
                           for entry in entries)
                    or any(len({entry[key] for entry in entries}) != len(entries)
                           for key in ('entry_id', 'input_id', 'source_uri'))
                    or reference_authoring['initial_entry_id'] not in {entry['entry_id'] for entry in entries}):
                raise ValueError('The reference annotation source is invalid')
            data['reference_authoring'] = {key: value for key, value in reference_authoring.items()}
        if image_appearance_path is not None:
            data["image_appearance_path"] = str(Path(image_appearance_path).absolute())
        if tissue_authoring is not None:
            import re

            if (
                not isinstance(tissue_authoring, dict)
                or set(tissue_authoring) != ({"entry_id", "method"} if tissue_authoring.get("method") == "threshold"
                                             else {"entry_id", "method", "setup"})
                or not isinstance(tissue_authoring["entry_id"], str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", tissue_authoring["entry_id"])
                or tissue_authoring["method"] not in {"threshold", "rf", "svm"}
            ):
                raise ValueError("The tissue-detector workbench choice is invalid")
            data["tissue_authoring"] = dict(tissue_authoring)
        if paired_views:
            if len(paired_views) > 1024:
                raise ValueError("Too many paired-image display records")
            data["paired_views"] = list(paired_views)
        if reference_display is not None:
            import re

            if not isinstance(reference_display, dict):
                raise ValueError("The paired-image display choice is invalid")
            base = {"mode", "fixed_entry_id", "moving_entry_id"}
            mode = reference_display.get("mode")
            expected = base | (
                {"candidate_transform_sha256"}
                if mode == "candidate_overlay"
                else set()
            )
            if mode == "candidate_overlay" and "review_policy" in reference_display:
                expected.add("review_policy")
            if mode == "alignment":
                expected |= {key for key in ("tools_directory", "method") if key in reference_display}
                if reference_display.get("method", "manual") not in {"manual", "automatic"}:
                    raise ValueError("The registration method is invalid")
            if (
                set(reference_display) != expected
                or mode
                not in {"overlay", "side_by_side", "alignment", "candidate_overlay"}
                or any(
                    not isinstance(reference_display[k], str) or not reference_display[k]
                    for k in ("fixed_entry_id", "moving_entry_id")
                )
            ):
                raise ValueError("The paired-image display choice is invalid")
            if mode == "candidate_overlay" and not re.fullmatch(
                "[0-9a-f]{64}", str(reference_display["candidate_transform_sha256"])
            ):
                raise ValueError(
                    "Candidate preview requires the exact pending transformation checksum"
                )
            if (
                "tools_directory" in reference_display
                and not Path(reference_display["tools_directory"]).is_absolute()
            ):
                raise ValueError("Alignment program directory must be absolute")
            data["reference_display"] = reference_display
        return base64.urlsafe_b64encode(
            json.dumps(data, separators=(",", ":"), allow_nan=False).encode()
        ).decode()


def resolve_desktop_bootstrap(project_path: Path) -> DesktopBootstrap | None:
    """

    Finds and checks the current desktop-startup scripts beside a QuPath project.

    The current application determines its registered script checksum and looks
    for that directory under the project's scripts folder. If present, the
    recorded files must match before we return their startup entry. A project
    without that directory needs preparation before it can use these controls.

    Args:
        project_path: Existing absolute QuPath project filename.

    Returns:
        DesktopBootstrap for the checked current script directory, or None when
        that directory has not been installed beside this project.

    Raises:
        ValueError: The project path is invalid or a present current script
            directory differs from its registered contents.
    """

    project = Path(project_path)
    if not project.is_absolute() or not project.is_file():
        raise ValueError("An existing absolute project file is required")
    root = project.parent
    digest = registered_script_bundle_sha256()
    bundle_root = root / "scripts" / ("bundle-" + digest)
    if not bundle_root.exists():
        return None
    layout = ProjectLayout(
        root,
        project,
        root / "scripts",
        root / "classifiers/pixel_classifiers",
        root / "classifiers/object_classifiers",
        root / "data",
        root / "exchange",
        root / "checkpoints",
        root / "provenance",
        root / "runs",
        root / "data/analysis-config.json",
        "{}",
    )
    try:
        bundle = authenticate_registered_script_bundle(layout, expected_bundle_sha256=digest)
    except Exception as error:
        raise ValueError("The current desktop script bundle is invalid or has changed") from error
    script = bundle_script_path(bundle, QuPathScriptAsset.RESEARCHER_DESKTOP_BOOTSTRAP)
    return DesktopBootstrap(project, script, bundle.root, digest)


def prepare_desktop_bootstrap(project_path: Path) -> DesktopBootstrap | None:
    """

    Adds the current editing scripts when reopening a recognized pipeline project.

    First use an already installed current directory when available. Otherwise,
    inspect the preceding registered directories: each saved file list and its
    named helper files must still match. At least one directory must identify
    the pipeline's project-creation or desktop-startup helper.

    We then add the current registered scripts and resolve their startup entry.
    The project's image data, annotations, accepted models and earlier script
    directories stay in place. A project with no recognizable pipeline scripts
    returns None and follows the caller's ordinary-project opening policy.

    Args:
        project_path: Existing project for which the current annotation and
            display controls are being prepared.

    Returns:
        DesktopBootstrap after the current helpers are available, or None for
        a project without an identifiable pipeline script directory.

    Raises:
        ValueError: Recorded script paths, contents, format or project-data
            directory cannot be verified before preparation.
    """

    from pathlib import PurePosixPath
    import hashlib
    from .scripts import materialize_script_bundle, registered_script_assets

    current = resolve_desktop_bootstrap(project_path)
    if current is not None:
        return current
    project = Path(project_path)
    base = project.parent
    scripts = base / "scripts"
    if not scripts.exists():
        return None
    if scripts.is_symlink():
        raise ValueError("The project script folder is linked")
    candidates = tuple(scripts.glob("bundle-*/qupath-script-bundle.v1.json"))
    if not candidates:
        return None
    if len(candidates) > 256:
        raise ValueError("The project has too many script revisions")
    recognized = False
    for inventory in candidates:
        folder = inventory.parent
        if (
            folder.is_symlink()
            or inventory.is_symlink()
            or not inventory.is_file()
            or inventory.stat().st_size > 1024 * 1024
        ):
            raise ValueError("A previous script inventory is invalid")
        data = inventory.read_bytes()
        if hashlib.sha256(data).hexdigest() != folder.name.removeprefix("bundle-"):
            raise ValueError("A previous script inventory changed")
        value = json.loads(data)
        if (
            set(value) != {"schema_version", "assets"}
            or value["schema_version"] != "qupath-script-bundle/v1"
        ):
            raise ValueError("A previous script inventory has an unsupported format")
        assets = value["assets"]
        if not isinstance(assets, list) or not 0 < len(assets) <= 128:
            raise ValueError("A previous script inventory is incomplete")
        names = set()
        for row in assets:
            if not isinstance(row, dict) or set(row) != {"path", "sha256"}:
                raise ValueError("Invalid previous script entry")
            name = row["path"]
            relative = PurePosixPath(name)
            if (
                not isinstance(name, str)
                or relative.is_absolute()
                or ".." in relative.parts
                or name in names
            ):
                raise ValueError("Invalid previous script path")
            names.add(name)
            path = folder / relative
            if (
                path.is_symlink()
                or not path.is_file()
                or not path.resolve().is_relative_to(folder.resolve())
                or path.stat().st_size > 16 * 1024 * 1024
                or hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]
            ):
                raise ValueError("A previous script no longer matches its inventory")
        recognized |= bool(
            names & {"general/05_researcher_desktop.groovy", "general/00_setup_project.groovy"}
        )
    if not recognized:
        return None
    if not (base / "data").is_dir() or (base / "data").is_symlink():
        raise ValueError("The pipeline project data folder is unavailable")
    layout = ProjectLayout(
        base,
        project,
        scripts,
        base / "classifiers/pixel_classifiers",
        base / "classifiers/object_classifiers",
        base / "data",
        base / "exchange",
        base / "checkpoints",
        base / "provenance",
        base / "runs",
        base / "data/analysis-config.json",
        "{}",
    )
    # Add a new script directory for current editing. Previous numerical
    # results keep their existing recorded scripts and model files.
    materialize_script_bundle(layout, registered_script_assets())
    return resolve_desktop_bootstrap(project)
