"""Expose the batch service as a command-line interface with structured JSON results.

The parser selects setup, validation, execution, review or cluster-exchange operations.
Runtime paths are resolved before opening the workspace service. Each command prints the
corresponding result, or a bounded error with a nonzero exit status, and closes the runtime
resources it opened."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import fields, is_dataclass
from enum import Enum
import json
import os
from pathlib import Path
import sys
from typing import TextIO

from .batch_dag import BatchPlan
from .batch_interactions import (
    BatchAggregateInteractionGroup,
    BatchInteractionGroup,
    BatchInteractionApplicationResult,
    BatchInteractionQuery,
    BatchInteractionResponseRequest,
    BatchTargetSelection,
)
from .batch_manifest import BatchManifest, ValidatedBatchManifest
from .setup.batch_prerequisites import BatchPrerequisiteOwnerInputs
from .batch_service import open_batch_service_provider
from .batch_service_models import BatchRunReport
from .batch_state import BatchWorkspaceLayout
from mif_pipeline.pipeline_core import InteractionAction
from mif_pipeline.io import selected_offline_bioformats_inventory
from mif_pipeline.io import configure_offline_runtime_selection
from mif_pipeline.io.bioformats_prerequisite import InventoryMissing
from mif_pipeline.qupath_runtime.managed_profile import load_managed_qupath_profile
from mif_pipeline.qupath_runtime.class_origin_candidate import probe_qupath_class_origins

from .prototype_owner_inputs import prototype_owner_inputs_supplier
from .setup.prerequisite_setup import setup_cpu_payload
from .setup.image_runtime_setup import ImageRuntimeSetupService
from .setup.instanseg_setup import InstanSegSetupService
from .setup.instanseg_setup import default_instanseg_model_target
from .setup.nimbus_setup import NimbusSetupService
from .setup.nimbus_setup import default_nimbus_model_target
from .setup.qupath_profile_setup import ManagedQuPathSetupService

OwnerInputs = Callable[[ValidatedBatchManifest, BatchPlan], BatchPrerequisiteOwnerInputs]


# ==============================================================================
#                                   Batch cli
# ==============================================================================


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description="Run or inspect a local mIF batch")
    parser.add_argument("--qupath", type=Path, help="Exact local QuPath executable")
    parser.add_argument(
        "--qupath-profile",
        type=Path,
        help="Verified extension setup manifest.json for this QuPath installation",
    )
    parser.add_argument("--java-home", type=Path, help="Exact Java 21 home")
    parser.add_argument("--bioformats-root", type=Path, help="Exact source Bio-Formats root")
    parser.add_argument("--instanseg-jar", type=Path)
    parser.add_argument("--djl-jar", type=Path)
    parser.add_argument("--pytorch-engine-jar", type=Path)
    parser.add_argument("--cpu-native-root", type=Path)
    parser.add_argument("--qupath-cache-root", type=Path)
    parser.add_argument(
        "--selective-compute-profile",
        type=Path,
        help="Explicit consented numerical offload profile; annotations and publication stay local",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "setup-workspace", help="Initialize this processing account's application workspace"
    )
    setup = commands.add_parser("setup-cpu")
    setup.add_argument("target", type=Path)
    setup.add_argument("--consent-download", action="store_true")
    profile = commands.add_parser("setup-qupath-profile")
    profile.add_argument(
        "profiles_root", type=Path, help="Existing folder for the separate extension setup"
    )
    profile.add_argument("--consent-download", action="store_true")
    image = commands.add_parser("setup-image")
    image.add_argument("action", choices=("inspect", "select", "install"))
    image.add_argument("component", choices=("java", "bioformats"))
    image.add_argument("location", nargs="?", type=Path)
    image.add_argument("--managed-root", type=Path)
    image.add_argument("--consent-download", action="store_true")
    instanseg = commands.add_parser("setup-instanseg")
    instanseg.add_argument("action", choices=("inspect", "install"))
    instanseg.add_argument("component", choices=("extension", "model"))
    instanseg.add_argument("--extension-directory", type=Path)
    instanseg.add_argument("--consent-download", action="store_true")
    elastix = commands.add_parser(
        "setup-elastix", help="Install or check Elastix and Transformix 5.3.1"
    )
    elastix.add_argument("action", choices=("inspect", "install", "select"))
    elastix.add_argument("location", nargs="?", type=Path)
    elastix.add_argument("--target", type=Path)
    elastix.add_argument("--consent-download", action="store_true")
    nimbus = commands.add_parser("setup-nimbus", help="Install, select or check the pinned Nimbus V1 model")
    nimbus.add_argument("action", choices=("inspect", "install", "select"))
    nimbus.add_argument("location", nargs="?", type=Path, help="Existing checkpoint for select")
    nimbus.add_argument("--target", type=Path, help="Absolute checkpoint filename to install or inspect")
    nimbus.add_argument("--consent-download", action="store_true")
    detector = commands.add_parser("setup-segmentation", help="Install or check an optional Cellpose or StarDist model")
    detector.add_argument("method", choices=("cellpose", "stardist"))
    detector.add_argument("action", choices=("inspect", "install"))
    detector.add_argument("--directory", type=Path, help="Absolute model directory on this processing host")
    detector.add_argument("--consent-download", action="store_true")
    warpy = commands.add_parser("setup-warpy", help="Install or check the Warpy 0.5.0 extension")
    warpy.add_argument("action", choices=("inspect", "install"))
    warpy.add_argument("--consent-download", action="store_true")
    commands.add_parser("prepare-stdin")
    commands.add_parser("describe-analysis").add_argument("analysis_folder", type=Path)
    for name in ("validate", "plan", "run"):
        commands.add_parser(name).add_argument("manifest", type=Path)
    for name in (
        "status",
        "runtime-capabilities",
        "resume",
        "continue",
        "stage-options",
        "preview-stage-revision",
        "revise-stage",
        "interactions",
        "respond",
        "align",
        "list-retries",
        "retry",
        "review-page",
        "respond-stdin",
        "annotations-export",
        "stage-annotation-preview",
        "stage-annotations",
        "annotations-import",
        "stage-alignment",
        "align-staged",
        "review-figures",
        "stage-landmark-review",
        "landmark-export",
        "stage-landmarks",
        "landmark-import",
    ):
        command = commands.add_parser(name)
        command.add_argument("output_root", type=Path)
        command.add_argument("batch_id")
        if name == "stage-options":
            command.add_argument("--node-id")
            command.add_argument("--expected-head-sha256")
            command.add_argument("--source-image", type=Path)
        elif name in {"preview-stage-revision", "revise-stage", "continue"}:
            command.add_argument("--expected-head-sha256", required=True)
            if name != "continue":
                command.add_argument("--node-id", required=True)
        elif name == "review-figures":
            command.add_argument("request_id")
            command.add_argument("expected_head")
            command.add_argument("--umap-settings", type=json.loads)
            command.add_argument("--attribute", default="cluster_id")
        elif name == "annotations-export":
            command.add_argument("request_id")
            command.add_argument("expected_head")
            command.add_argument("--entry-id")
            command.add_argument("--preview-id")
            command.add_argument("--reference-role", type=json.loads)
        elif name in {"annotations-import", "align-staged", "landmark-export", "landmark-import"}:
            command.add_argument("submission_id")
        elif name in {"interactions", "review-page"}:
            command.add_argument("--offset", type=int, default=0)
            command.add_argument("--limit", type=int, default=50)
            command.add_argument("--domain", action="append", default=[])
            command.add_argument("--reason", action="append", default=[])
        elif name in {"respond", "align"}:
            command.add_argument("response_file", type=Path)
            if name == "align":
                command.add_argument("settings_file", type=Path)
        elif name == "retry":
            command.add_argument("--node-id", required=True)
            command.add_argument("--expected-head-sha256", required=True)
            selection = command.add_mutually_exclusive_group(required=True)
            selection.add_argument("--terminal-record-sha256")
            selection.add_argument("--pending-authorization-sha256")
    return parser


def _json_ready(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _json_ready(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise TypeError(f"unsupported public result value: {type(value).__name__}")


def _response_file(path: Path) -> BatchInteractionResponseRequest:
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("response file exceeds 1 MiB")
    return _response_text(path.read_text(encoding="utf-8"))


def _response_text(text: str) -> BatchInteractionResponseRequest:
    def unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate response field {key!r}")
            result[key] = value
        return result

    if not isinstance(text, str) or len(text.encode("utf-8")) > 1024 * 1024:
        raise ValueError("response exceeds 1 MiB")
    payload = json.loads(
        text,
        object_pairs_hook=unique_pairs,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"nonfinite response value {value}")
        ),
    )
    required = {
        "group_id",
        "group_sha256",
        "selection",
        "action",
        "response",
        "expected_state_head_sha256",
    }
    if type(payload) is not dict or set(payload) != required:
        raise ValueError("response file requires the exact service request fields")
    selection = payload["selection"]
    if type(selection) is not dict or not set(selection) <= {
        "target_ids",
        "validation_domain_ids",
        "all_compatible",
    }:
        raise ValueError("response selection contains unsupported fields")
    return BatchInteractionResponseRequest(
        group_id=payload["group_id"],
        group_sha256=payload["group_sha256"],
        selection=BatchTargetSelection(**selection),
        action=payload["action"],
        response=payload["response"],
        expected_state_head_sha256=payload["expected_state_head_sha256"],
    )


def _group_document(
    group: BatchInteractionGroup | BatchAggregateInteractionGroup,
) -> dict[str, object]:
    if isinstance(group, BatchAggregateInteractionGroup):
        actions = (InteractionAction.ACCEPT_UNLABELED_CLUSTERS.value,)
        evidence = _json_ready(group.evidence)
    else:
        actions = group.targets[0].stage_result.allowed_actions
        evidence = None
    return {
        "group_id": group.group_id,
        "group_sha256": group.group_sha256,
        "stage": group.stage,
        "interaction_reason": group.interaction_reason,
        "action_schema_version": group.action_schema_version,
        "context_version": group.context_version,
        "bulk_application_allowed": group.bulk_application_allowed,
        "allowed_actions": _json_ready(actions),
        "targets": _json_ready(group.targets),
        "evidence": evidence,
    }


def _apply_runtime_environment(args, environment=None):
    """Fill missing declared installations; explicit options always take priority."""
    import shutil

    environment = os.environ if environment is None else environment
    names = {
        "qupath": "MIF_QUPATH_EXECUTABLE",
        "qupath_profile": "MIF_QUPATH_PROFILE",
        "instanseg_jar": "MIF_INSTANSEG_JAR",
        "djl_jar": "MIF_DJL_JAR",
        "pytorch_engine_jar": "MIF_PYTORCH_ENGINE_JAR",
        "cpu_native_root": "MIF_CPU_NATIVE_ROOT",
        "qupath_cache_root": "MIF_QUPATH_CACHE_ROOT",
    }
    for name, variable in names.items():
        if getattr(args, name, None) is not None:
            continue
        raw = environment.get(variable)
        if raw:
            path = Path(raw)
            if not path.is_absolute() or ".." in path.parts:
                raise ValueError(f"{variable} must name an absolute installed location.")
            setattr(args, name, path)
    if getattr(args, "qupath", None) is None:
        found = shutil.which("QuPath", path=environment.get("PATH", ""))
        if found is not None:
            setattr(args, "qupath", Path(found).absolute())


def _image_paths(args: argparse.Namespace) -> tuple[Path | None, Path | None, Path | None]:
    """Resolve explicit, environment, and previously selected software locations."""

    def chosen(option: Path | None, variable: str) -> Path | None:
        raw = option if option is not None else os.environ.get(variable)
        if raw is None:
            return None
        path = Path(raw)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError(f"{variable} selection must be an absolute path")
        return path

    java = chosen(args.java_home, "JAVA_HOME")
    bioformats = chosen(args.bioformats_root, "MIF_BIOFORMATS_PAYLOAD_ROOT")
    frozen = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else None
    return java, bioformats, frozen


def _managed_image_root(platform: str) -> Path:
    if platform == "win-x86_64":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    root = Path(base)
    if not root.is_absolute():
        raise ValueError("application data root must be absolute")
    return root / "mif-pipeline" / "image-runtime"


def _setup_path(path: Path, label: str) -> Path:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{label} must be an absolute path without traversal")
    return path


def _instanseg_setup(args: argparse.Namespace, platform: str) -> tuple[object, int]:
    if args.qupath is None:
        raise ValueError("--qupath is required for InstanSeg setup")
    executable = _setup_path(args.qupath, "QuPath executable")
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("QuPath executable must be a local executable file")
    managed_root = default_instanseg_model_target(platform)
    extension_directory = (
        _setup_path(args.extension_directory, "QuPath extension directory")
        if args.extension_directory is not None
        else None
    )
    bundled_root = (
        _setup_path(Path(sys._MEIPASS) / "qupath-assets", "bundled QuPath assets")
        if getattr(sys, "frozen", False)
        else None
    )
    launch_binding = (
        load_managed_qupath_profile(args.qupath_profile, expected_executable=executable)
        if args.qupath_profile is not None
        else None
    )
    service = InstanSegSetupService(
        executable=executable,
        managed_payload_root=managed_root,
        extension_directory=extension_directory,
        bundled_payload_root=bundled_root,
        origin_probe=(
            (lambda path: probe_qupath_class_origins(path, launch_binding=launch_binding))
            if launch_binding is not None
            else None
        ),
    )
    if args.action == "inspect":
        observation = service.inspect()
        installation = getattr(observation, args.component)
    elif args.component == "extension":
        installation = service.install_extension(consent=args.consent_download)
        observation = service.inspect()
    else:
        installation = service.install_model(consent=args.consent_download)
        observation = None
    document: dict[str, object] = {
        "component": args.component,
        "installation": installation,
    }
    if args.component == "extension":
        assert observation is not None
        document["loaded_status"] = observation.origins.instanseg.status
        document["loaded_observation"] = observation.origins.instanseg
        document["extension_profile"] = observation.origins.extensions
    return document, 0 if installation.state == "installed" else 1


def _complete_landmark_review(service, batch_id, application):
    """Run the authorized import/QC retry in the same cluster allocation."""
    result = _json_ready(application)
    rows = result.get("applications", ())
    if not rows or any(
        row.get("status") not in {"accepted", "accepted_for_retry", "accepted_for_application"}
        for row in rows
    ):
        return result
    return {**result, "continuation": _json_ready(service.resume(batch_id))}


def _nimbus_setup(args: argparse.Namespace) -> tuple[dict, int]:
    """Checks or obtains the model on this computer without opening the analysis runtime."""

    from mif_pipeline.pipeline_core.software_preferences import (
        desktop_preferences_path,
        load_desktop_preferences,
    )

    if args.action == "select" and args.location is None:
        raise ValueError("select requires an absolute Nimbus checkpoint location")
    if args.action != "select" and args.location is not None:
        raise ValueError("location is only valid for select")
    target = _setup_path(args.target, "Nimbus target") if args.target is not None else None
    location = _setup_path(args.location, "Nimbus checkpoint") if args.location is not None else None
    platform = "win-x86_64" if sys.platform == "win32" else "linux-x86_64"
    preference_path = desktop_preferences_path()
    preferences = load_desktop_preferences(preference_path)
    service = NimbusSetupService(
        platform=platform,
        managed_target=target,
        checkpoint_path=target if args.action == "inspect" else None,
        preferences_path=preference_path if preferences is not None else None,
    )
    if args.action == "select":
        result = service.select(location)
    elif args.action == "install":
        result = service.install(consent=args.consent_download)
    else:
        result = service.inspect()
    document = _json_ready(result)
    document["preference_saved"] = preferences is not None and args.action in {"install", "select"}
    if result.state == "installed" and preferences is None and result.location != default_nimbus_model_target(platform):
        document["next_step"] = (
            "No software preferences exist on this computer. Enter this location as "
            "Nimbus model checkpoint in marker setup: " + str(result.location)
        )
    return document, 0 if result.state == "installed" else 1


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    prerequisite_inputs: OwnerInputs | None = None,
) -> int:
    """Run one command and print one JSON result or a bounded diagnostic."""
    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    args = None
    try:
        args = _parser().parse_args(argv)
        if args.command == "setup-workspace":
            from mif_pipeline.pipeline_core.authority_provisioning import (
                provision_application_authority_layout_v2,
            )

            provision_application_authority_layout_v2()
            output.write(json.dumps({"status": "workspace_ready"}) + "\n")
            return 0
        if args.command == "runtime-capabilities":
            from mif_pipeline._devices import runtime_capabilities

            output.write(json.dumps(runtime_capabilities(), sort_keys=True, allow_nan=False) + "\n")
            return 0
        if args.command == "setup-nimbus":
            document, code = _nimbus_setup(args)
            output.write(json.dumps(document, sort_keys=True, allow_nan=False) + "\n")
            return code
        if args.command == "setup-segmentation":
            from .setup.alternative_segmentation_setup import AlternativeSegmentationSetupService
            directory = _setup_path(args.directory, "model directory") if args.directory else None
            service = AlternativeSegmentationSetupService(args.method, directory=directory)
            status = service.install(consent=args.consent_download) if args.action == "install" else service.inspect()
            output.write(json.dumps({"state": status.state, "location": str(status.location), "detail": status.detail}, sort_keys=True) + "\n")
            return 0 if status.state == "installed" else 1
        if args.command in {"setup-elastix", "setup-warpy"}:
            from mif_pipeline.pipeline_core.software_preferences import (
                load_desktop_preferences,
                desktop_preferences_path,
            )
            from .setup.installed_runtime import apply_saved_software

            preference = desktop_preferences_path()
            saved = load_desktop_preferences(preference)
            if args.command == "setup-elastix":
                from .setup.elastix_setup import ElastixSetupService

                service = ElastixSetupService(
                    platform="win-x86_64" if sys.platform == "win32" else "linux-x86_64",
                    target=args.target,
                    preferences_path=preference if saved is not None else None,
                )
                if args.action == "select":
                    if args.location is None:
                        raise ValueError("Supply the extracted Elastix folder or its bin folder.")
                    result = service.select(args.location)
                elif args.action == "install":
                    result = service.install(consent=args.consent_download)
                else:
                    result = service.inspect()
            else:
                from .setup.warpy_setup import WarpySetupService

                apply_saved_software(args, saved)
                _apply_runtime_environment(args)
                if args.qupath is None:
                    raise ValueError("Supply --qupath or select QuPath in Software setup first.")
                binding = (
                    load_managed_qupath_profile(
                        args.qupath_profile, expected_executable=args.qupath
                    )
                    if args.qupath_profile
                    else None
                )
                service = WarpySetupService(executable=args.qupath, launch_binding=binding)
                result = (
                    service.inspect()
                    if args.action == "inspect"
                    else service.install(consent=args.consent_download)
                )
            document = _json_ready(result)
            if args.command == "setup-elastix" and result.state == "ready":
                document["environment_variable"] = "MIF_ELASTIX_DIRECTORY"
            output.write(json.dumps(document, sort_keys=True, allow_nan=False) + "\n")
            return 0 if result.state in {"ready", "installed"} else 1
        if args.command == "describe-analysis":
            from .cluster.cluster_reopen import describe_analysis

            output.write(
                json.dumps(describe_analysis(args.analysis_folder), sort_keys=True, allow_nan=False)
                + "\n"
            )
            return 0
        if args.command == "prepare-stdin":
            from .setup.cluster_setup import prepare_cluster_analysis
            from .setup.cluster_setup import parse_setup

            result = prepare_cluster_analysis(parse_setup(sys.stdin.read(1024 * 1024 + 1)))
            output.write(json.dumps(result, sort_keys=True, allow_nan=False) + "\n")
            return 0
        if args.selective_compute_profile is not None:
            import os
            from mif_pipeline._remote_transport import kernel_command

            profile_path = args.selective_compute_profile.expanduser().resolve(strict=True)
            if profile_path.stat().st_size > 65536:
                raise ValueError("Selective-compute profile exceeds its limit")
            kernel_command(json.loads(profile_path.read_text()))
            os.environ["MIF_SELECTIVE_COMPUTE_PROFILE"] = str(profile_path)
        platform = "win-x86_64" if sys.platform == "win32" else "linux-x86_64"
        if args.command == "setup-qupath-profile":
            if args.qupath is None:
                raise ValueError("--qupath is required for extension setup")
            if args.qupath_profile is not None:
                raise ValueError("Omit --qupath-profile when creating a separate extension setup")
            executable = _setup_path(args.qupath, "QuPath executable")
            binding = ManagedQuPathSetupService(
                executable=executable,
                profiles_root=args.profiles_root,
                platform=platform,
            ).install(consent=args.consent_download)
            output.write(
                json.dumps(
                    {
                        "qupath_profile_manifest": str(binding.profile_root / "manifest.json"),
                        "next_command_option": "--qupath-profile",
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            return 0
        if args.command == "setup-cpu":
            if not args.target.is_absolute():
                raise ValueError("CPU setup target must be absolute")
            result = setup_cpu_payload(
                args.target,
                platform,
                consent=args.consent_download,
            )
            output.write(json.dumps(_json_ready(result), sort_keys=True) + "\n")
            return 0
        if args.command == "setup-instanseg":
            document, code = _instanseg_setup(args, platform)
            output.write(json.dumps(_json_ready(document), sort_keys=True) + "\n")
            return code
        from mif_pipeline.pipeline_core.software_preferences import (
            load_desktop_preferences,
            desktop_preferences_path,
        )
        from .setup.installed_runtime import apply_saved_software

        apply_saved_software(args, load_desktop_preferences(desktop_preferences_path()))
        _apply_runtime_environment(args)
        java_home, bioformats_root, frozen_root = _image_paths(args)
        if args.command == "setup-image":
            if args.action == "select" and args.location is None:
                raise ValueError("select requires an absolute location")
            if args.action != "select" and args.location is not None:
                raise ValueError("location is only valid for select")
            if args.action == "install" and not args.consent_download:
                raise ValueError("installation requires --consent-download")
            managed_root = args.managed_root or _managed_image_root(platform)
            service = ImageRuntimeSetupService(
                platform,
                java_home=java_home,
                bioformats_root=bioformats_root,
                managed_root=managed_root,
                frozen_root=frozen_root,
            )
            if args.action == "inspect":
                result = service.inspect()[0 if args.component == "java" else 1]
            elif args.action == "select":
                result = service.select(args.component, args.location)
            else:
                result = service.install(args.component, consent=True)
            document = _json_ready(result)
            if (
                result.location is not None
                and not result.included
                and result.state in {"ready", "not_checked"}
            ):
                document["next_command_path"] = str(result.location)
                document["next_command_option"] = (
                    "--java-home" if args.component == "java" else "--bioformats-root"
                )
            output.write(json.dumps(document, sort_keys=True) + "\n")
            return 0 if result.state in {"ready", "not_checked"} else 1
        # Bind one process-local choice before any image inventory is selected.
        configure_offline_runtime_selection(
            java_home=java_home,
            bioformats_root=bioformats_root,
        )
        if args.qupath is None:
            raise ValueError("--qupath is required for batch commands")
        qupath = args.qupath.expanduser().resolve(strict=True)
        if not qupath.is_file():
            raise ValueError("QuPath executable must be a local file")
        launch_binding = (
            load_managed_qupath_profile(args.qupath_profile, expected_executable=qupath)
            if args.qupath_profile is not None
            else None
        )
        payload_paths = (
            args.instanseg_jar,
            args.djl_jar,
            args.pytorch_engine_jar,
            args.cpu_native_root,
            args.qupath_cache_root,
        )
        if any(path is not None for path in payload_paths):
            if any(path is None for path in payload_paths):
                raise ValueError("QuPath payload paths must be supplied together")
            if any(not path.is_absolute() for path in payload_paths):
                raise ValueError("QuPath payload paths must be absolute")
            try:
                inventory = selected_offline_bioformats_inventory()
            except InventoryMissing:
                inventory = None
            configured_inputs = prototype_owner_inputs_supplier(
                qupath_executable=qupath,
                instanseg_jar=args.instanseg_jar,
                djl_jar=args.djl_jar,
                pytorch_engine_jar=args.pytorch_engine_jar,
                native_root=args.cpu_native_root,
                cache_root=args.qupath_cache_root,
                platform="win-x86_64" if sys.platform == "win32" else "linux-x86_64",
                bioformats_inventory=inventory,
                launch_binding=launch_binding,
            )
        else:
            configured_inputs = None
        response = (
            _response_file(args.response_file) if args.command in {"respond", "align"} else None
        )
        if args.command == "respond-stdin":
            response = _response_text(sys.stdin.read(1024 * 1024 + 1))
        if args.command in {"validate", "plan", "run"}:
            manifest_path = args.manifest.expanduser().resolve(strict=True)
            # We read the workspace location, then let its service distinguish a
            # new project from a retained run using the saved manifest records.
            manifest = BatchManifest.from_json_bytes(manifest_path.read_bytes())
            layout = BatchWorkspaceLayout.resolve(manifest.output_root, manifest.batch_id)
        else:
            layout = BatchWorkspaceLayout.resolve(args.output_root, args.batch_id)
        from contextlib import ExitStack
        from .setup.installed_runtime import installed_runtime_inputs

        with ExitStack() as resources:
            if configured_inputs is None:
                configured_inputs = resources.enter_context(
                    installed_runtime_inputs(
                        qupath, platform=platform, launch_binding=launch_binding
                    )
                )
            provider = resources.enter_context(
                open_batch_service_provider(
                    workspace_layout=layout,
                    qupath_executable=qupath,
                    linux_containment_profile=(
                        "prototype-local-process-group" if sys.platform == "linux" else None
                    ),
                    prerequisite_inputs=prerequisite_inputs or configured_inputs,
                    launch_binding=launch_binding,
                    cpu_native_root=args.cpu_native_root,
                )
            )
            if args.command in {"validate", "plan", "run"}:
                service = provider.for_manifest(manifest_path)
                if args.command == "validate":
                    result = service.validate(manifest_path)
                    document: object = {
                        "batch_id": result.manifest.batch_id,
                        "original_manifest_sha256": result.original_manifest_sha256,
                        "normalized_manifest_sha256": result.normalized_manifest_sha256,
                        "enabled_sample_ids": [item.sample_id for item in result.enabled_samples],
                    }
                elif args.command == "plan":
                    document = service.plan(manifest_path).to_mapping()
                else:
                    document = service.run(manifest_path)
            else:
                service = provider.for_workspace(layout.output_root, layout.batch_id)
                if args.command == "status":
                    document = service.status(args.batch_id)
                elif args.command == "resume":
                    document = service.resume(args.batch_id)
                elif args.command == "continue":
                    document = service.continue_run(args.batch_id,
                        expected_head_sha256=args.expected_head_sha256)
                elif args.command == "stage-options":
                    from .batch_stage_options_transport import encode_options, revision_choices
                    if args.node_id is None:
                        if args.source_image is not None:
                            raise ValueError("Select a source-correction task before inspecting its image.")
                        status = service.status(args.batch_id)
                        if args.expected_head_sha256 is not None and args.expected_head_sha256 != status.state_head_sha256:
                            raise ValueError("The analysis changed. Refresh its stage options.")
                        document = revision_choices(status)
                    else:
                        if args.expected_head_sha256 is None:
                            raise ValueError("Select the saved head displayed with these stage options.")
                        options = service.stage_options(args.batch_id, node_id=args.node_id,
                            expected_head_sha256=args.expected_head_sha256)
                        status = service.status(args.batch_id)
                        if status.state_head_sha256 != options.state_head_sha256:
                            raise ValueError("The analysis changed. Refresh its stage options.")
                        document = {"options": encode_options(options), "workflow_nodes": status.workflow_nodes}
                        if args.source_image is not None:
                            from mif_pipeline.io import inspect_source_series_inventory
                            if options.kind not in {"inspection", "registration"}:
                                raise ValueError("Image inspection belongs to source or paired-image options.")
                            inventory = inspect_source_series_inventory(args.source_image,
                                require_planar_channels=options.kind != "registration")
                            document = {**document,
                                "source_mif_path": str(inventory.source_identity.resolved_path),
                                "source_file_sha256": inventory.source_identity.file_sha256,
                                "candidates": inventory.candidates}
                            if options.kind == "registration":
                                from .batch_settled_review_revision import paired_reference_from_inventory
                                document["paired_image_inspection"] = paired_reference_from_inventory(
                                    inventory, reference_id=options.sample_id + "-paired", modality="other").to_mapping()
                elif args.command in {"preview-stage-revision", "revise-stage"}:
                    from .batch_stage_options_transport import parse_revision, revision_settings, encode_preview
                    options = service.stage_options(args.batch_id, node_id=args.node_id,
                        expected_head_sha256=args.expected_head_sha256)
                    settings = revision_settings(options, parse_revision(sys.stdin.read(1024 * 1024 + 1)))
                    if args.command == "preview-stage-revision":
                        document = encode_preview(service.preview_stage_revision(args.batch_id,
                            node_id=args.node_id, expected_head_sha256=args.expected_head_sha256, settings=settings))
                    else:
                        document = service.revise_stage(args.batch_id, node_id=args.node_id,
                            expected_head_sha256=args.expected_head_sha256, settings=settings)
                elif args.command == "align":
                    if args.settings_file.stat().st_size > 65536:
                        raise ValueError("Alignment settings exceed the size limit")
                    options = json.loads(args.settings_file.read_text())
                    if not isinstance(options, dict):
                        raise ValueError("Alignment settings must be a JSON object")
                    document = service.align_paired_image(args.batch_id, response, options)
                elif args.command == "list-retries":
                    current_head = service.status(args.batch_id).state_head_sha256
                    choices = service.retry_choices(
                        args.batch_id, expected_head_sha256=current_head
                    )
                    document = {
                        "batch_id": args.batch_id,
                        "state_head_sha256": current_head,
                        "choices": choices,
                    }
                elif args.command == "retry":
                    document = service.retry(
                        args.batch_id,
                        node_id=args.node_id,
                        expected_head_sha256=args.expected_head_sha256,
                        selected_failed_terminal_record_sha256=(args.terminal_record_sha256),
                        selected_pending_authorization_sha256=args.pending_authorization_sha256,
                    )
                elif args.command == "stage-landmark-review":
                    from .cluster.cluster_landmarks import stage_review

                    document = stage_review(service, args.batch_id, sys.stdin.read(1024 * 1024 + 1))
                elif args.command == "landmark-export":
                    from .cluster.cluster_landmarks import export_review

                    document = export_review(service, args.batch_id, args.submission_id)
                elif args.command == "stage-landmarks":
                    from .cluster.cluster_landmarks import stage_landmarks

                    document = stage_landmarks(
                        service, args.batch_id, sys.stdin.read(1024 * 1024 + 1)
                    )
                elif args.command == "landmark-import":
                    from .cluster.cluster_landmarks import import_landmarks

                    document = _complete_landmark_review(
                        service,
                        args.batch_id,
                        import_landmarks(service, args.batch_id, args.submission_id),
                    )
                elif args.command == "stage-alignment":
                    from .cluster.cluster_alignment import stage_alignment

                    document = stage_alignment(
                        service, args.batch_id, sys.stdin.read(1024 * 1024 + 1)
                    )
                elif args.command == "align-staged":
                    from .cluster.cluster_alignment import run_alignment

                    document = run_alignment(service, args.batch_id, args.submission_id)
                elif args.command == "annotations-export":
                    from .cluster.cluster_annotations import export_annotations

                    options = {}
                    if args.reference_role is not None:
                        options["reference_role"] = args.reference_role
                    if args.preview_id:
                        from .annotation_preview import read_preview_selection

                        options["preview_selection"] = read_preview_selection(
                            service, args.batch_id, args.preview_id
                        )
                    document = export_annotations(
                        service,
                        args.batch_id,
                        args.request_id,
                        args.expected_head,
                        args.entry_id,
                        **options,
                    )
                elif args.command == "stage-annotation-preview":
                    from .annotation_preview import stage_preview_selection

                    document = stage_preview_selection(
                        service, args.batch_id, sys.stdin.read(1024 * 1024 + 1)
                    )
                elif args.command == "stage-annotations":
                    from .cluster.cluster_annotations import stage_annotations
                    from .cluster.cluster_annotations import parse_submission

                    document = stage_annotations(
                        service,
                        args.batch_id,
                        parse_submission(sys.stdin.read(32 * 1024 * 1024 + 1)),
                    )
                elif args.command == "annotations-import":
                    from .cluster.cluster_annotations import import_staged_annotations

                    document = import_staged_annotations(service, args.batch_id, args.submission_id)
                elif args.command == "review-figures":
                    from .cluster.cluster_review_figures import encode_figures

                    document = encode_figures(
                        service.review_plots(
                            args.batch_id,
                            interaction_request_id=args.request_id,
                            expected_state_head_sha256=args.expected_head,
                            umap_settings=args.umap_settings, attribute=args.attribute,
                        )
                    )
                elif args.command == "review-page":
                    from .cluster.cluster_review_pages import current_page

                    document = current_page(
                        service,
                        args.batch_id,
                        BatchInteractionQuery(
                            offset=args.offset,
                            limit=args.limit,
                            validation_domain_ids=tuple(args.domain),
                            interaction_reasons=tuple(args.reason),
                        ),
                    )
                elif args.command == "interactions":
                    before_head = service.status(args.batch_id).state_head_sha256
                    page = service.list_interactions(
                        args.batch_id,
                        BatchInteractionQuery(
                            offset=args.offset,
                            limit=args.limit,
                            validation_domain_ids=tuple(args.domain),
                            interaction_reasons=tuple(args.reason),
                        ),
                    )
                    after_head = service.status(args.batch_id).state_head_sha256
                    if before_head != after_head:
                        raise ValueError("interaction state changed during listing; retry")
                    document = {
                        "state_head_sha256": before_head,
                        "total_count": page.total_count,
                        "offset": page.offset,
                        "limit": page.limit,
                        "next_offset": page.next_offset,
                        "groups": [_group_document(group) for group in page.groups],
                    }
                else:
                    assert response is not None
                    document = service.apply_interaction_response(args.batch_id, response)
        output.write(json.dumps(_json_ready(document), sort_keys=True, allow_nan=False) + "\n")
        if isinstance(document, BatchRunReport) and document.outcome in {"failed", "incomplete"}:
            return 1
        if isinstance(document, BatchInteractionApplicationResult) and any(
            item.status == "rejected" for item in document.applications
        ):
            return 1
        return 0
    except SystemExit as exc:
        return int(exc.code)
    except Exception as exc:
        from mif_pipeline.pipeline_core.authority_provisioning import (
            AuthorityProvisioningRequiredError,
        )

        message = str(exc)
        if (
            isinstance(exc, AuthorityProvisioningRequiredError)
            and getattr(args, "command", None) != "setup-workspace"
        ):
            message += (
                ". Run mif-batch setup-workspace once using the same processing account "
                "and environment, then retry."
            )
        errors.write(json.dumps({"error": type(exc).__name__, "message": message}) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
