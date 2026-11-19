"""Presentation-neutral inspection and setup of desktop prerequisites.

The `FirstRunPrerequisiteItem` record carries `prerequisite_id`, `state`, `summary`,
`detail`, `action_id`.

The interface keeps editable researcher choices associated with the current image or
analysis request. Values are passed to the stage-specific validator before they become
accepted processing settings."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from mif_pipeline.compartment_analysis.kronos_marker_preparation import (
    KronosMarkerTextModel,
)
from mif_pipeline.pipeline_core.authority_provisioning import (
    AuthorityProvisioningRequiredError,
    open_application_authority_layout_v2,
    provision_application_authority_layout_v2,
)
from mif_pipeline.qupath_runtime import QuPathExecutableInfo, resolve_and_probe_qupath
from mif_pipeline.qupath_runtime.native_image_reader import BundledNativeReaderError

from .desktop_preferences import (
    DesktopPreferencesV1,
    desktop_preferences_path,
    load_desktop_preferences,
    write_desktop_preferences,
)
from .qupath_discovery import qupath_installation_candidates

_PrerequisiteState = Literal["ready", "missing", "invalid", "error"]
_Action = Literal["select-qupath", "install-qupath-extensions", "initialize-authority"]


# ==============================================================================
#                                   First run
# ==============================================================================


class _RetainedAuthority(Protocol):
    def validate(self) -> None: ...
    def close(self) -> None: ...
    def __enter__(self) -> "_RetainedAuthority": ...
    def __exit__(self, *exc: object) -> None: ...


@dataclass(frozen=True, slots=True)
class FirstRunPrerequisiteItem:
    """One immutable prerequisite state and its only offered repair action."""

    prerequisite_id: Literal["qupath", "authority-v2"]
    state: _PrerequisiteState
    summary: str
    detail: str
    action_id: _Action | None

    def __post_init__(self) -> None:
        if self.prerequisite_id not in {"qupath", "authority-v2"}:
            raise ValueError("prerequisite_id is invalid")
        if self.state not in {"ready", "missing", "invalid", "error"}:
            raise ValueError("prerequisite state is invalid")
        if type(self.summary) is not str or not self.summary.strip():
            raise ValueError("prerequisite summary must be non-empty")
        if type(self.detail) is not str:
            raise TypeError("prerequisite detail must be text")
        expected_action: _Action | None
        if self.state == "ready":
            expected_action = None
        elif self.prerequisite_id == "qupath":
            expected_action = (None if self.state == "error" and self.action_id is None else "install-qupath-extensions"
                if self.action_id == "install-qupath-extensions" else "select-qupath")
        else:
            expected_action = "initialize-authority"
        if self.action_id != expected_action:
            raise ValueError("prerequisite action does not match its state")


@dataclass(frozen=True, slots=True)
class FirstRunPrerequisiteState:
    """Complete current readiness for local desktop construction."""

    qupath: FirstRunPrerequisiteItem
    authority_v2: FirstRunPrerequisiteItem
    ready: bool

    def __post_init__(self) -> None:
        if self.qupath.prerequisite_id != "qupath":
            raise ValueError("qupath item identity is invalid")
        if self.authority_v2.prerequisite_id != "authority-v2":
            raise ValueError("authority item identity is invalid")
        expected = self.qupath.state == self.authority_v2.state == "ready"
        if type(self.ready) is not bool or self.ready is not expected:
            raise ValueError("prerequisite ready flag does not match its items")


def _state(
    qupath: FirstRunPrerequisiteItem,
    authority: FirstRunPrerequisiteItem,
) -> FirstRunPrerequisiteState:
    return FirstRunPrerequisiteState(
        qupath=qupath,
        authority_v2=authority,
        ready=qupath.state == authority.state == "ready",
    )


def _ready_qupath(info: QuPathExecutableInfo) -> FirstRunPrerequisiteItem:
    return FirstRunPrerequisiteItem(
        "qupath",
        "ready",
        "QuPath is ready",
        f"{info.path} ({info.version})",
        None,
    )


def _unready_qupath(state: _PrerequisiteState, detail: str, *, extensions: bool = False) -> FirstRunPrerequisiteItem:
    return FirstRunPrerequisiteItem(
        "qupath",
        state,
        "Install the extensions used by this application" if extensions else "QuPath requires attention",
        detail,
        "install-qupath-extensions" if extensions else "select-qupath",
    )


def _ready_authority() -> FirstRunPrerequisiteItem:
    return FirstRunPrerequisiteItem(
        "authority-v2",
        "ready",
        "Local workspace setup is ready",
        "The app's local setup passed its checks.",
        None,
    )


def _broken_application(error: BundledNativeReaderError) -> FirstRunPrerequisiteItem:
    return FirstRunPrerequisiteItem(
        "qupath", "error", "Application files need repair", str(error), None,
    )


def _unready_authority(
    state: _PrerequisiteState,
    detail: str,
) -> FirstRunPrerequisiteItem:
    return FirstRunPrerequisiteItem(
        "authority-v2",
        state,
        "Local workspace setup needs attention",
        detail,
        "initialize-authority",
    )


def _error_detail(error: BaseException) -> str:
    try:
        return str(error)
    except BaseException:
        return f"{type(error).__name__} details unavailable"


class FirstRunPrerequisiteService:
    """Inspect or explicitly initialize non-scientific desktop prerequisites."""

    def __init__(
        self,
        *,
        preferences_path: Path | None = None,
        load_preferences: Callable[[Path], DesktopPreferencesV1 | None] = load_desktop_preferences,
        write_preferences: Callable[[Path, DesktopPreferencesV1], None] = write_desktop_preferences,
        probe_qupath: Callable[[str | Path], QuPathExecutableInfo] = resolve_and_probe_qupath,
        qupath_candidates: Callable[[], tuple[Path, ...]] = qupath_installation_candidates,
        open_authority_v2: Callable[[], _RetainedAuthority] = open_application_authority_layout_v2,
        provision_authority_v2: Callable[[], object] = provision_application_authority_layout_v2,
        check_extensions: Callable[[QuPathExecutableInfo], None] | None = None,
        install_extensions: Callable[[QuPathExecutableInfo], None] | None = None,
    ) -> None:
        self._preferences_path = (
            desktop_preferences_path()
            if preferences_path is None
            else Path(preferences_path).expanduser().resolve(strict=False)
        )
        self._load_preferences = load_preferences
        self._write_preferences = write_preferences
        self._probe_qupath = probe_qupath
        self._qupath_candidates = qupath_candidates
        self._open_authority_v2 = open_authority_v2
        self._provision_authority_v2 = provision_authority_v2
        self._qupath_executable_info: QuPathExecutableInfo | None = None
        self._check_extensions = check_extensions
        self._install_extensions = install_extensions

    @property
    def qupath_executable_info(self) -> QuPathExecutableInfo | None:
        """The latest successful probe, cleared before each new inspection."""
        return self._qupath_executable_info

    def _inspect_qupath(self) -> FirstRunPrerequisiteItem:
        self._qupath_executable_info = None
        try:
            preferences = self._load_preferences(self._preferences_path)
        except (OSError, TypeError, ValueError) as exc:
            return _unready_qupath("invalid", _error_detail(exc))
        except BaseException as exc:
            return _unready_qupath("error", _error_detail(exc))
        if preferences is None:
            return _unready_qupath("missing", "Choose the QuPath app on this computer.")
        try:
            info = self._probe_qupath(preferences.qupath_executable)
        except (OSError, TypeError, ValueError) as exc:
            return _unready_qupath("invalid", _error_detail(exc))
        except BaseException as exc:
            return _unready_qupath("error", _error_detail(exc))
        self._qupath_executable_info = info
        if self._check_extensions is not None:
            try:
                self._check_extensions(info)
            except BundledNativeReaderError as exc:
                return _broken_application(exc)
            except Exception as exc:
                return _unready_qupath("invalid", _error_detail(exc), extensions=True)
        return _ready_qupath(info)

    def install_qupath_extensions(self) -> FirstRunPrerequisiteState:
        """Prepare this application's extensions for the selected QuPath program.

        The setup button runs this operation in the background. Failure leaves
        the program selected and offers the same installation action again.
        """
        current = self._inspect_qupath()
        info = self._qupath_executable_info
        if info is None or self._install_extensions is None or (
            current.state != "ready" and current.action_id is None
        ):
            return _state(current, self._inspect_authority())
        try:
            self._install_extensions(info)
        except BundledNativeReaderError as exc:
            return _state(_broken_application(exc), self._inspect_authority())
        except Exception as exc:
            return _state(_unready_qupath("error", _error_detail(exc), extensions=True),
                          self._inspect_authority())
        return self.inspect()

    def _inspect_authority(self) -> FirstRunPrerequisiteItem:
        try:
            retained = self._open_authority_v2()
        except AuthorityProvisioningRequiredError as exc:
            return _unready_authority("missing", _error_detail(exc))
        except BaseException as exc:
            return _unready_authority("error", _error_detail(exc))

        item: FirstRunPrerequisiteItem
        try:
            retained.validate()
            item = _ready_authority()
        except AuthorityProvisioningRequiredError as exc:
            item = _unready_authority("invalid", _error_detail(exc))
        except BaseException as exc:
            item = _unready_authority("error", _error_detail(exc))
        finally:
            try:
                retained.close()
            except BaseException as exc:
                item = _unready_authority("error", _error_detail(exc))
        return item

    def inspect(self) -> FirstRunPrerequisiteState:
        """Reprobe stored QuPath and reopen authority without provisioning."""

        return _state(self._inspect_qupath(), self._inspect_authority())

    def discover_qupath(self) -> tuple[QuPathExecutableInfo, ...]:
        """Offer freshly probed local installations without selecting any of them."""
        seen: set[Path] = set()
        discovered: list[QuPathExecutableInfo] = []
        for candidate in self._qupath_candidates():
            try:
                resolved = candidate.resolve(strict=False)
                if resolved in seen:
                    continue
                seen.add(resolved)
                info = self._probe_qupath(resolved)
            except (OSError, ValueError):
                # Unsupported versions and unavailable/wrong-host executables
                # are rejected by the owner; they never become selectable.
                continue
            discovered.append(info)
        return tuple(discovered)

    def select_qupath(self, path: Path) -> FirstRunPrerequisiteState:
        """Persist a candidate only after a successful current probe."""

        if not isinstance(path, Path):
            raise TypeError("QuPath selection must be a Path")
        self._qupath_executable_info = None
        info = self._probe_qupath(path)
        preferences = DesktopPreferencesV1(
            "mif-desktop-preferences",
            1,
            str(info.path),
            *(self._runtime_preferences(info.path)),
        )
        self._write_preferences(self._preferences_path, preferences)
        self._qupath_executable_info = info
        return _state(self._inspect_qupath(), self._inspect_authority())

    def _runtime_preferences(
        self, executable: Path
    ) -> tuple[
        str | None,
        str | None,
        str | None,
        str | None,
        str | None,
        str | None,
        KronosMarkerTextModel | None,
    ]:
        try:
            current = self._load_preferences(self._preferences_path)
        except ValueError:
            # Explicit QuPath selection replaces a rejected preference record.
            return None, None, None, None, None, None, None
        if current is None:
            return None, None, None, None, None, None, None
        return (
            current.java_home,
            current.bioformats_root,
            current.qupath_profile_manifest if Path(current.qupath_executable) == executable else None,
            current.elastix_directory,
            current.nimbus_checkpoint_path,
            current.kronos_package_directory,
            current.kronos_marker_text_model,
        )

    def save_image_runtime_selection(self, component: str, location: Path) -> None:
        """Retain an accepted explicit choice alongside the selected QuPath."""
        if component not in {"java", "bioformats"}:
            raise ValueError("unknown image runtime component")
        if not isinstance(location, Path) or not location.is_absolute():
            raise ValueError("image runtime selection must be absolute")
        current = self._load_preferences(self._preferences_path)
        if current is None:
            raise ValueError("Select QuPath before saving image-reading setup")
        self._write_preferences(
            self._preferences_path,
            DesktopPreferencesV1(
                current.schema_name,
                current.schema_version,
                current.qupath_executable,
                str(location) if component == "java" else current.java_home,
                str(location) if component == "bioformats" else current.bioformats_root,
                current.qupath_profile_manifest,
                current.elastix_directory,
                current.nimbus_checkpoint_path,
                current.kronos_package_directory,
                current.kronos_marker_text_model,
            ),
        )

    def initialize_authority_v2(self) -> FirstRunPrerequisiteState:
        """Provision via the platform resolver, then prove one retained reopen."""

        qupath = self._inspect_qupath()
        try:
            self._provision_authority_v2()
        except BaseException as exc:
            return _state(qupath, _unready_authority("error", _error_detail(exc)))
        try:
            retained = self._open_authority_v2()
            with retained:
                pass
        except AuthorityProvisioningRequiredError as exc:
            return _state(qupath, _unready_authority("invalid", _error_detail(exc)))
        except BaseException as exc:
            return _state(qupath, _unready_authority("error", _error_detail(exc)))
        return _state(qupath, _ready_authority())


__all__ = [
    "FirstRunPrerequisiteItem",
    "FirstRunPrerequisiteService",
    "FirstRunPrerequisiteState",
]
