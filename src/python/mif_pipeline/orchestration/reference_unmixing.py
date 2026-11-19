"""Canonical single-sample reference-workflow to unmixing operation.

The `ReferenceUnmixingCompletion` record carries `run_id`, `config_sha256`,
`target_image_sha256`, `checkpoint_sha256`, `final_authority_kind` and its remaining
declared fields.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass, replace
from contextlib import contextmanager
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
from uuid import uuid4
from typing import Mapping

from ome_types import OME, from_xml, to_xml

from mif_pipeline.io import (
    InspectedSourceAuthority,
)
from mif_pipeline.preprocessing.unmixing.validation import (
    AfBehavior,
    SolverKind,
    SpectralState,
    UnmixingConfig,
)
from mif_pipeline.preprocessing.unmixing.compatibility import BasisSourceChoice
from mif_pipeline.preprocessing.unmixing.interactions import SelectedIdentifier
from mif_pipeline.preprocessing.unmixing.reference_service_roi_purification import (
    ReferenceRoiPairingSelection,
)
from mif_pipeline.preprocessing.unmixing.reference_service import (
    ReferenceServiceAdvance,
    ReferenceServiceAdvanceStatus,
    ReferenceServiceSnapshot,
    ReferenceServiceView,
    ReferenceWorkflowService,
)
from mif_pipeline.preprocessing.unmixing.reference_service_storage import (
    ReferenceCheckpointStore,
)
from mif_pipeline.preprocessing.unmixing.reference_workflow import (
    ReferenceWorkflowStatus,
)
from mif_pipeline.preprocessing.unmixing.application import ResolvedBasisApplication
from mif_pipeline.preprocessing.unmixing.workflow import (
    UnmixingResult,
    run_unmixing,
)
from mif_pipeline.qupath_runtime import ReferenceProjectLayout

from .exports import OutputArtifact
from .reference_unmixing_resources import ReferenceUnmixingResourceRegistry

_REQUEST_SCHEMA_NAME = "mif_pipeline.reference_unmixing_request"
_REQUEST_SCHEMA_VERSION = 2
_MANIFEST_SCHEMA_NAME = "mif_pipeline.reference_unmixing_operation_manifest"
_MANIFEST_SCHEMA_VERSION = 2
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_FRONTEND_FORM_LIMITS_VERSION = "reference-unmixing-form-limits-v1"
_MAX_FRONTEND_SEQUENCE_ITEMS = 4096
_MAX_FRONTEND_MAPPING_ITEMS = 1024
_MAX_FRONTEND_JSON_DEPTH = 12
_MAX_FRONTEND_JSON_NODES = 16384
_MAX_FRONTEND_TEXT_CHARS = 4096


# ==============================================================================
#                               Reference unmixing
# ==============================================================================


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(value: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _file_sha256(path: str | Path, label: str) -> str:
    candidate = Path(path)
    candidate = candidate / "zarr.json" if candidate.is_dir() else candidate
    try:
        digest = hashlib.sha256()
        with candidate.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ValueError(f"{label} is missing or unreadable") from exc
    return digest.hexdigest()


class ReferenceUnmixingStatus(str, Enum):
    """Persisted lifecycle state exposed without opening canonical images."""

    AWAITING_OPERATION = "awaiting_operation"
    READY_FOR_UNMIXING = "ready_for_unmixing"
    COMPLETED = "completed"
    ABORTED = "aborted"


class ReferenceScientificOperationRequiredError(RuntimeError):
    """A generic interaction attempted to replace service-owned science."""


class ReferenceUnmixingCancelledError(RuntimeError):
    """A requested cancellation prevented a future atomic job from starting."""


class ReferenceUnmixingCancellationStatus(str, Enum):
    REQUESTED_BEFORE_NEXT_JOB = "requested_before_next_job"
    DECLINED_ACTIVE_ATOMIC_JOB = "declined_active_atomic_job"


_SAFE_ACTIONS_BY_CHECKPOINT = {
    "shared_control_preparation": frozenset({"decide"}),
    "basis_source": frozenset({"decide", "choose_basis_source", "choose_curated_basis"}),
    "control_strategy": frozenset({"decide"}),
    "reference_control_files": frozenset(
        {"decide", "register_canonical_input", "complete_reference_input", "bind_reference_controls"}
    ),
    "positive_reference_rois": frozenset({"decide", "advance_reference_purification"}),
    "af_reference_rois": frozenset({"decide", "advance_reference_purification"}),
    "negative_reference_rois": frozenset({"decide", "advance_reference_purification"}),
    "background_reference_rois": frozenset({"decide", "advance_reference_purification"}),
    "signal_dark_measurements": frozenset({"decide", "advance_reference_purification"}),
    "extraction_qc": frozenset({"decide", "advance_reference_purification"}),
    "fluorophore_consensus": frozenset({"decide", "accept_live_consensus"}),
    "target_af": frozenset({"decide", "propose_control_target_af", "propose_target_af_groups"}),
    "target_af_control_review": frozenset({"decide", "accept_control_target_af", "review_control_final_basis"}),
    "final_run_basis": frozenset({"decide", "propose_final_basis", "review_final_basis"}),
}


_FORBIDDEN_ACTION_CONFIGURATION_FIELDS = frozenset(
    {
        "application",
        "algorithm_id",
        "aggregation_id",
        "artifact_id",
        "artifact_pointer",
        "authority",
        "authority_id",
        "basis_application",
        "candidate",
        "candidate_id",
        "created_at",
        "distribution_id",
        "definition_id",
        "full_slide_background_semantic",
        "generator_id",
        "learned_column_canonicalization",
        "learned_scale_convention",
        "method_id",
        "metric_id",
        "metric_definitions",
        "objective_id",
        "open_interval_conversion_id",
        "pointer",
        "policy_id",
        "policy_version",
        "policy_versions",
        "publication",
        "publication_metadata",
        "ranking_policy",
        "ready_application",
        "ready_record",
        "reference_response",
        "reference_roi_export",
        "response",
        "reviewed_at",
        "restart_matching",
        "roi_export",
        "roi_selections",
        "runtime_version",
        "runtime_versions",
        "seed_derivation_id",
        "software_version",
        "software_versions",
        "solver_id",
        "tissue_mask",
        "tissue_mask_binding",
        "update_sweep_id",
        "version_record",
        "version_records",
        "winner_rule",
    }
)


def _reject_caller_authored_scientific_fields(value: object, *, label: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if type(key) is not str:
                continue
            normalized = key.strip().lower().replace("-", "_")
            if (
                normalized in _FORBIDDEN_ACTION_CONFIGURATION_FIELDS
                or normalized == "sha256"
                or normalized.endswith("_sha256")
                or normalized.endswith("_hash")
                or normalized.endswith("_policy_version")
                or normalized.endswith("_policy_id")
                or normalized.endswith("_runtime_version")
                or normalized == "version"
                or normalized.endswith("_version")
                or normalized.endswith("_versions")
            ):
                raise ValueError(f"{label} cannot carry caller-authored scientific field {key!r}")
            _reject_caller_authored_scientific_fields(item, label=label)
    elif type(value) is list:
        for item in value:
            _reject_caller_authored_scientific_fields(item, label=label)


def _freeze_json_value(value: object, *, label: str) -> object:
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{label} contains a nonfinite number")
        return value
    if type(value) is list:
        return (
            "__strict_json_array__",
            tuple(_freeze_json_value(item, label=label) for item in value),
        )
    if isinstance(value, Mapping):
        if any(type(key) is not str or not key for key in value):
            raise ValueError(f"{label} keys must be non-empty text")
        return tuple((key, _freeze_json_value(value[key], label=label)) for key in sorted(value))
    raise TypeError(f"{label} must contain only strict JSON primitives")


def _validate_bounded_json_value(value: object, *, label: str, spectral_table: bool = False) -> None:
    """Bound action JSON, allowing only the declared numerical table its 2 MiB limit.

    The numerical-source action may carry one top-level CSV string. Other text,
    nested values and unrelated actions retain the ordinary frontend limits.
    """

    if spectral_table and isinstance(value, Mapping) and "spectral_csv" in value:
        text = value["spectral_csv"]
        if text is not None and (type(text) is not str or len(text.encode("utf-8")) > 2 * 1024 * 1024):
            raise ValueError("Enter a spectral table smaller than 2 MiB.")
        value = {key: item for key, item in value.items() if key != "spectral_csv"}
    stack = [(value, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_FRONTEND_JSON_NODES:
            raise ValueError(f"{label} exceeds the {_FRONTEND_FORM_LIMITS_VERSION} maximum size")
        if depth > _MAX_FRONTEND_JSON_DEPTH:
            raise ValueError(f"{label} exceeds the {_FRONTEND_FORM_LIMITS_VERSION} maximum depth")
        if type(item) is str:
            if len(item) > _MAX_FRONTEND_TEXT_CHARS:
                raise ValueError(
                    f"{label} exceeds the {_FRONTEND_FORM_LIMITS_VERSION} maximum text length"
                )
            continue
        if item is None or type(item) in {bool, int}:
            continue
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError(f"{label} contains a nonfinite number")
            continue
        if type(item) is list:
            if len(item) > _MAX_FRONTEND_SEQUENCE_ITEMS:
                raise ValueError(
                    f"{label} exceeds the {_FRONTEND_FORM_LIMITS_VERSION} maximum array length"
                )
            stack.extend((child, depth + 1) for child in item)
            continue
        if isinstance(item, Mapping):
            if len(item) > _MAX_FRONTEND_MAPPING_ITEMS:
                raise ValueError(
                    f"{label} exceeds the {_FRONTEND_FORM_LIMITS_VERSION} maximum object size"
                )
            if any(
                type(key) is not str or not key or len(key) > _MAX_FRONTEND_TEXT_CHARS
                for key in item
            ):
                raise ValueError(f"{label} keys must be bounded non-empty text")
            stack.extend((child, depth + 1) for child in item.values())
            continue
        raise TypeError(f"{label} must contain only strict JSON primitives")


def _thaw_json_value(value: object) -> object:
    # Service summaries may mix dictionaries with retained immutable metadata.
    if isinstance(value, Mapping):
        return {key: _thaw_json_value(item) for key, item in value.items()}
    if type(value) is list:
        return [_thaw_json_value(item) for item in value]
    if type(value) is tuple:
        if len(value) == 2 and value[0] == "__strict_json_array__" and type(value[1]) is tuple:
            return [_thaw_json_value(item) for item in value[1]]
        if all(type(item) is tuple and len(item) == 2 and type(item[0]) is str for item in value):
            return {item[0]: _thaw_json_value(item[1]) for item in value}
        return [_thaw_json_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class ReferenceUnmixingConfiguredAction:
    """One checkpoint-bound action containing primitives and offered IDs only."""

    checkpoint_token: str
    action: str
    configuration: tuple[tuple[str, object], ...]

    def __post_init__(self) -> None:
        _nonempty_text(self.checkpoint_token, "checkpoint token")
        _nonempty_text(self.action, "configured action")
        if type(self.configuration) is not tuple:
            raise TypeError("configured action configuration must be immutable")
        thawed = _thaw_json_value(self.configuration)
        if type(thawed) is not dict:
            raise TypeError("configured action configuration must be an object")
        _validate_bounded_json_value(thawed, label="configured action", spectral_table=self.action == "choose_curated_basis")
        _reject_caller_authored_scientific_fields(thawed, label="configured action")
        if _freeze_json_value(thawed, label="configured action") != self.configuration:
            raise ValueError("configured action configuration is not canonical")

    @classmethod
    def from_mapping(
        cls,
        *,
        checkpoint_token: str,
        action: str,
        configuration: Mapping[str, object],
    ) -> "ReferenceUnmixingConfiguredAction":
        _validate_bounded_json_value(configuration, label="configured action", spectral_table=action == "choose_curated_basis")
        _reject_caller_authored_scientific_fields(configuration, label="configured action")
        frozen = _freeze_json_value(configuration, label="configured action")
        assert type(frozen) is tuple
        return cls(checkpoint_token, action, frozen)

    def configuration_mapping(self) -> dict[str, object]:
        value = _thaw_json_value(self.configuration)
        assert type(value) is dict
        return value


@dataclass(frozen=True, slots=True)
class ReferenceUnmixingCancellation:
    run_id: str
    status: ReferenceUnmixingCancellationStatus

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_name": "mif_pipeline.reference_unmixing_cancellation",
            "schema_version": 1,
            "run_id": self.run_id,
            "status": self.status.value,
        }


def _nonempty_text(value: object, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _exact_tuple(value: object, expected: type, label: str) -> None:
    if type(value) is not tuple or any(type(item) is not expected for item in value):
        raise TypeError(f"{label} must be an exact tuple of {expected.__name__}")


def _identifier_tuple(value: object, label: str, *, allow_empty: bool) -> None:
    if (
        type(value) is not tuple
        or (not allow_empty and not value)
        or any(type(item) is not str or not item.strip() for item in value)
        or len(value) != len(set(value))
        or len(value) > _MAX_FRONTEND_SEQUENCE_ITEMS
        or any(len(item) > _MAX_FRONTEND_TEXT_CHARS for item in value)
    ):
        qualifier = "possibly empty" if allow_empty else "non-empty"
        raise ValueError(f"{label} must be a {qualifier} unique tuple of text")


def _configured_text_array(
    value: object, label: str, *, allow_empty: bool = True
) -> tuple[str, ...]:
    if type(value) is not list:
        raise TypeError(f"{label} must be a JSON array")
    result = tuple(value)
    _identifier_tuple(result, label, allow_empty=allow_empty)
    return result


def _configured_mapping(value: object, label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError(f"{label} must be a JSON object")
    return value


@dataclass(frozen=True, slots=True)
class ReferenceUnmixingRequest:
    """Exact immutable configuration and source binding for one slide run."""

    run_id: str
    input_path: str
    output_path: str
    source_kind: str
    inspected_authority_json: str
    reviewed_ome_xml: str
    solver: str
    spectral_state_override: str | None
    af_behavior: str
    chunk_size: tuple[int, int]
    output_dtype: str
    nnls_tolerance: float
    nnls_max_iterations: int
    overwrite: bool
    replace_channel_names_with_biomarkers: bool
    calculation_precision: str | None = None
    measurement_band_components: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if type(self.run_id) is not str or not self.run_id.strip():
            raise ValueError("reference unmixing run ID must be non-empty text")
        for value, label in (
            (self.input_path, "input path"),
            (self.output_path, "output path"),
        ):
            if type(value) is not str or not value or not Path(value).is_absolute():
                raise ValueError(f"reference unmixing {label} must be absolute")
        self.reviewed_source_binding()
        if type(self.chunk_size) is not tuple or len(self.chunk_size) != 2:
            raise ValueError("reference unmixing chunk size must be an exact pair")
        # Reconstructing the owning config is the single validation authority.
        self.to_unmixing_config()
        if type(self.replace_channel_names_with_biomarkers) is not bool:
            raise ValueError("channel-name replacement choice must be boolean")


    @classmethod
    def from_reviewed_source(
        cls,
        *,
        run_id: str,
        authority: InspectedSourceAuthority,
        reviewed_ome: OME,
        output_path: str | Path,
        config: UnmixingConfig,
        replace_channel_names_with_biomarkers: bool,
    ) -> "ReferenceUnmixingRequest":
        """Build one request from the reviewed image and numerical settings.

        We retain the selected source inspection, reviewed OME description and
        output settings together. Constructing the final record checks their
        image association and numerical bounds once; its mapping is the request
        saved by batch execution for later review and resumption.
        """
        if type(authority) is not InspectedSourceAuthority or not isinstance(reviewed_ome, OME):
            raise TypeError("reviewed source requires exact inspection and OME authority")
        if type(config) is not UnmixingConfig:
            raise TypeError("reference unmixing requires exact UnmixingConfig")
        return cls(
            run_id=run_id,
            input_path=str(authority.source_identity.resolved_path),
            output_path=str(output_path),
            source_kind="reviewed_source",
            inspected_authority_json=authority.to_json_bytes().decode("utf-8"),
            reviewed_ome_xml=to_xml(reviewed_ome),
            solver=config.solver.value,
            spectral_state_override=(
                None
                if config.spectral_state_override is None
                else config.spectral_state_override.value
            ),
            af_behavior=config.af_behavior.value,
            chunk_size=tuple(config.chunk_size),
            output_dtype=config.output_dtype,
            calculation_precision=config.calculation_precision,
            measurement_band_components=config.measurement_band_components,
            nnls_tolerance=float(config.nnls_tolerance),
            nnls_max_iterations=int(config.nnls_max_iterations),
            overwrite=config.overwrite,
            replace_channel_names_with_biomarkers=replace_channel_names_with_biomarkers,
        )

    def reviewed_source_binding(self) -> tuple[InspectedSourceAuthority, OME]:
        """Recover the saved source inspection and its reviewed image metadata."""
        if self.source_kind != "reviewed_source":
            raise ValueError("reference unmixing source kind is unsupported")
        if type(self.inspected_authority_json) is not str or type(self.reviewed_ome_xml) is not str:
            raise ValueError("reviewed source request requires inspection and OME authority")
        try:
            authority = InspectedSourceAuthority.from_json_bytes(
                self.inspected_authority_json.encode("utf-8")
            )
            reviewed = from_xml(self.reviewed_ome_xml)
        except (TypeError, ValueError) as exc:
            raise ValueError("reviewed source request authority is invalid") from exc
        if (
            authority.to_json_bytes().decode("utf-8") != self.inspected_authority_json
            or to_xml(reviewed) != self.reviewed_ome_xml
            or str(authority.source_identity.resolved_path) != self.input_path
        ):
            raise ValueError("reviewed source request binding differs")
        return authority, reviewed

    def to_unmixing_config(self) -> UnmixingConfig:
        try:
            return UnmixingConfig(
                solver=SolverKind(self.solver),
                spectral_state_override=(
                    None
                    if self.spectral_state_override is None
                    else SpectralState(self.spectral_state_override)
                ),
                af_behavior=AfBehavior(self.af_behavior),
                chunk_size=self.chunk_size,
                output_dtype=self.output_dtype,
                calculation_precision=self.calculation_precision,
                measurement_band_components=self.measurement_band_components,
                nnls_tolerance=self.nnls_tolerance,
                nnls_max_iterations=self.nnls_max_iterations,
                overwrite=self.overwrite,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("reference unmixing configuration is invalid") from exc

    def to_mapping(self) -> dict[str, object]:
        value = {
            "schema_name": _REQUEST_SCHEMA_NAME,
            "schema_version": _REQUEST_SCHEMA_VERSION,
            "run_id": self.run_id,
            "input_path": self.input_path,
            "output_path": self.output_path,
            "source_kind": self.source_kind,
            "inspected_authority_json": self.inspected_authority_json,
            "reviewed_ome_xml": self.reviewed_ome_xml,
            "solver": self.solver,
            "spectral_state_override": self.spectral_state_override,
            "af_behavior": self.af_behavior,
            "chunk_size": list(self.chunk_size),
            "output_dtype": self.output_dtype,
            "nnls_tolerance": self.nnls_tolerance,
            "nnls_max_iterations": self.nnls_max_iterations,
            "overwrite": self.overwrite,
            "replace_channel_names_with_biomarkers": (self.replace_channel_names_with_biomarkers),
        }
        if self.calculation_precision is not None:
            value["calculation_precision"] = self.calculation_precision
        if self.measurement_band_components:
            value["measurement_band_components"] = list(self.measurement_band_components)
        return value

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "ReferenceUnmixingRequest":
        """Decode the serialized fields into a validated ReferenceUnmixingRequest record.

        The mapping is checked against the declared field inventory, including
        ``schema_name``, ``schema_version``, ``run_id``, ``input_path``, ``output_path``,
        ``source_kind``. Nested values are decoded through the corresponding field
        validators before the record is constructed."""

        if not isinstance(value, Mapping):
            raise TypeError("reference unmixing request must be a mapping")
        expected = {
            "schema_name",
            "schema_version",
            "run_id",
            "input_path",
            "output_path",
            "source_kind",
            "inspected_authority_json",
            "reviewed_ome_xml",
            "solver",
            "spectral_state_override",
            "af_behavior",
            "chunk_size",
            "output_dtype",
            "nnls_tolerance",
            "nnls_max_iterations",
            "overwrite",
            "replace_channel_names_with_biomarkers",
        }
        if not expected <= set(value) <= expected | {"calculation_precision", "measurement_band_components"}:
            raise ValueError("reference unmixing request schema fields differ")
        if (value["schema_name"], value["schema_version"]) != (
            _REQUEST_SCHEMA_NAME,
            _REQUEST_SCHEMA_VERSION,
        ) or type(value["schema_version"]) is not int:
            raise ValueError("reference unmixing request schema is unsupported")
        chunk_size = value["chunk_size"]
        if (
            type(chunk_size) is not list
            or len(chunk_size) != 2
            or any(type(item) is not int for item in chunk_size)
        ):
            raise ValueError("reference unmixing request chunk size is invalid")
        tolerance = value["nnls_tolerance"]
        if type(tolerance) not in {int, float} or not math.isfinite(tolerance):
            raise ValueError("reference unmixing request tolerance is invalid")
        return cls(
            run_id=value["run_id"],
            input_path=value["input_path"],
            output_path=value["output_path"],
            source_kind=value["source_kind"],
            inspected_authority_json=value["inspected_authority_json"],
            reviewed_ome_xml=value["reviewed_ome_xml"],
            solver=value["solver"],
            spectral_state_override=value["spectral_state_override"],
            af_behavior=value["af_behavior"],
            chunk_size=tuple(chunk_size),
            output_dtype=value["output_dtype"],
            calculation_precision=value.get("calculation_precision"),
            measurement_band_components=tuple(value.get("measurement_band_components", ())),
            nnls_tolerance=float(tolerance),
            nnls_max_iterations=value["nnls_max_iterations"],
            overwrite=value["overwrite"],
            replace_channel_names_with_biomarkers=value["replace_channel_names_with_biomarkers"],
        )

    @property
    def config_sha256(self) -> str:
        encoded = json.dumps(
            self.to_mapping(),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ReferenceUnmixingArtifactRequirement:
    """Bounded artifact role/subject requirement exposed to frontends."""

    role: str
    subject_id: str | None

    def __post_init__(self) -> None:
        _nonempty_text(self.role, "operation artifact requirement role")
        if self.subject_id is not None:
            _nonempty_text(self.subject_id, "operation artifact requirement subject")

    def to_mapping(self) -> dict[str, object]:
        return {"role": self.role, "subject_id": self.subject_id}


class ReferenceUnmixingFormValueKind(str, Enum):
    TEXT = "text"
    ENUM = "enum"
    LOCATOR = "locator"
    IDENTIFIER = "identifier"
    ORDERED_IDENTIFIER_SEQUENCE = "ordered_identifier_sequence"
    IDENTIFIER_MAPPING = "identifier_mapping"
    IDENTIFIER_MULTIMAPPING = "identifier_multimapping"
    IDENTIFIER_DISPOSITION_MAPPING = "identifier_disposition_mapping"
    ENUM_MAPPING = "enum_mapping"
    REASON_CODE_SEQUENCE = "reason_code_sequence"
    PRIMITIVE_MAPPING = "primitive_mapping"
    PRIMITIVE_LIST = "primitive_list"


@dataclass(frozen=True, slots=True)
class ReferenceUnmixingFormField:
    """One presentation-neutral user-owned field for an offered operation."""

    name: str
    label: str
    help_text: str
    value_kind: ReferenceUnmixingFormValueKind
    required: bool
    minimum_count: int
    identifier_choices: tuple[str, ...]
    value_choices: tuple[str, ...]
    mapping_key_choices: tuple[str, ...] = ()
    mapping_value_choices: tuple[str, ...] = ()
    mapping_value_source_field: str | None = None

    def __post_init__(self) -> None:
        """Validate the ReferenceUnmixingFormField fields when the record is constructed.

        The checks cover ``identifier_choices``, ``value_choices``, ``mapping_key_choices``,
        ``mapping_value_choices``, ``mapping_value_source_field``, ``name``. Invalid field
        types, values or combinations raise the declared validation error before this
        instance can be used by its caller."""

        for value, label in (
            (self.name, "form field name"),
            (self.label, "form field label"),
            (self.help_text, "form field help"),
        ):
            _nonempty_text(value, label)
        if not isinstance(self.value_kind, ReferenceUnmixingFormValueKind):
            raise TypeError("form field value kind is invalid")
        if type(self.required) is not bool:
            raise TypeError("form field required flag must be exact bool")
        if type(self.minimum_count) is not int or self.minimum_count < 0:
            raise ValueError("form field minimum count must be nonnegative")
        _identifier_tuple(
            self.identifier_choices,
            "form field identifier choices",
            allow_empty=True,
        )
        _identifier_tuple(
            self.value_choices,
            "form field value choices",
            allow_empty=True,
        )
        _identifier_tuple(
            self.mapping_key_choices,
            "form field mapping key choices",
            allow_empty=True,
        )
        _identifier_tuple(
            self.mapping_value_choices,
            "form field mapping value choices",
            allow_empty=True,
        )
        if self.mapping_value_source_field is not None:
            _nonempty_text(
                self.mapping_value_source_field,
                "form field mapping value source",
            )
        if self.value_kind not in {
            ReferenceUnmixingFormValueKind.IDENTIFIER_MAPPING,
            ReferenceUnmixingFormValueKind.IDENTIFIER_MULTIMAPPING,
            ReferenceUnmixingFormValueKind.ENUM_MAPPING,
        } and (
            self.mapping_key_choices
            or self.mapping_value_choices
            or self.mapping_value_source_field is not None
        ):
            raise ValueError("mapping authorities apply only to identifier-mapping fields")
        if self.value_kind is ReferenceUnmixingFormValueKind.ENUM_MAPPING and (
            not self.mapping_key_choices
            or not self.value_choices
            or self.mapping_value_choices
            or self.mapping_value_source_field is not None
        ):
            raise ValueError("enum-mapping fields require exact keys and value choices")

    def to_mapping(self) -> dict[str, object]:
        return {
            "name": self.name,
            "label": self.label,
            "help_text": self.help_text,
            "value_kind": self.value_kind.value,
            "required": self.required,
            "minimum_count": self.minimum_count,
            "identifier_choices": list(self.identifier_choices),
            "value_choices": list(self.value_choices),
            "mapping_key_choices": list(self.mapping_key_choices),
            "mapping_value_choices": list(self.mapping_value_choices),
            "mapping_value_source_field": self.mapping_value_source_field,
        }


@dataclass(frozen=True, slots=True)
class ReferenceUnmixingOperationRequirement:
    """One exact offered option and the safe operation it currently requires."""

    option_value: str
    label: str
    description: str
    operation_action: str
    minimum_selected_ids: int
    minimum_artifacts: int
    artifact_requirements: tuple[ReferenceUnmixingArtifactRequirement, ...]
    form_fields: tuple[ReferenceUnmixingFormField, ...] = ()

    def __post_init__(self) -> None:
        """Validate the ReferenceUnmixingOperationRequirement fields when the record is
        constructed.

        The checks cover ``artifact_requirements``, ``form_fields``, ``option_value``,
        ``label``, ``description``, ``operation_action``. Invalid field types, values or
        combinations raise the declared validation error before this instance can be used by
        its caller."""

        for value, label in (
            (self.option_value, "operation option value"),
            (self.label, "operation option label"),
            (self.description, "operation option description"),
            (self.operation_action, "operation action"),
        ):
            _nonempty_text(value, label)
        if (
            type(self.minimum_selected_ids) is not int
            or self.minimum_selected_ids < 0
            or type(self.minimum_artifacts) is not int
            or self.minimum_artifacts < 0
        ):
            raise ValueError("operation minimum counts must be nonnegative integers")
        _exact_tuple(
            self.artifact_requirements,
            ReferenceUnmixingArtifactRequirement,
            "operation artifact requirements",
        )
        keys = tuple((item.role, item.subject_id) for item in self.artifact_requirements)
        if len(keys) != len(set(keys)):
            raise ValueError("operation artifact requirements must be unique")
        _exact_tuple(
            self.form_fields,
            ReferenceUnmixingFormField,
            "operation form fields",
        )
        names = tuple(item.name for item in self.form_fields)
        if len(names) != len(set(names)):
            raise ValueError("operation form field names must be unique")
        if any(
            item.mapping_value_source_field is not None
            and item.mapping_value_source_field not in names
            for item in self.form_fields
        ):
            raise ValueError("operation mapping value source is not a declared form field")

    def to_mapping(self) -> dict[str, object]:
        return {
            "option_value": self.option_value,
            "label": self.label,
            "description": self.description,
            "operation_action": self.operation_action,
            "minimum_selected_ids": self.minimum_selected_ids,
            "minimum_artifacts": self.minimum_artifacts,
            "artifact_requirements": [item.to_mapping() for item in self.artifact_requirements],
            "form_fields": [item.to_mapping() for item in self.form_fields],
        }


_OPERATION_ACTION_BY_CHECKPOINT_OPTION = {
    "basis_source": {
        "curated_reference_library": "choose_curated_basis",
        "phenoimager_motif_profile": "choose_basis_source",
        "import_reference_library": "choose_basis_source",
        "construct_reference_library": "choose_basis_source",
        "abort": "decide",
    },
    "control_strategy": {
        "shared_unstained_control": "decide",
        "per_stain_negative_controls": "decide",
        "abort": "decide",
    },
    "reference_control_files": {
        "accept_reference_control_files_with_source_preparation": "bind_reference_controls",
        "accept_reference_control_files": "bind_reference_controls",
        "reselect_reference_control_files": "bind_reference_controls",
        "abort": "decide",
    },
    "shared_control_preparation": {
        "accept_shared_control_preparation": "accept_shared_control",
        "retry_shared_control_preparation": "decide", "abort": "decide",
    },
    "positive_reference_rois": {
        "accept_positive_reference_rois": "advance_reference_purification",
        "retry_positive_reference_rois": "advance_reference_purification",
        "abort": "decide",
    },
    "af_reference_rois": {
        "reselect_reference_rois": "decide",
        "accept_af_reference_rois": "advance_reference_purification",
        "retry_af_reference_rois": "advance_reference_purification",
        "abort": "decide",
    },
    "negative_reference_rois": {
        "reselect_reference_rois": "decide",
        "accept_negative_reference_rois": "advance_reference_purification",
        "retry_negative_reference_rois": "advance_reference_purification",
        "abort": "decide",
    },
    "background_reference_rois": {
        "reselect_reference_rois": "decide",
        "accept_background_reference_rois": "advance_reference_purification",
        "retry_background_reference_rois": "advance_reference_purification",
        "abort": "decide",
    },
    "signal_dark_measurements": {
        "accept_signal_dark_measurements": "advance_reference_purification",
        "reselect_reference_rois": "decide",
        "abort": "decide",
    },
    "extraction_qc": {
        "accept_extraction_qc": "advance_reference_purification",
        "retry_extraction": "advance_reference_purification",
        "revise_signal_dark_measurements": "decide",
        "reselect_reference_rois": "decide",
        "abort": "decide",
    },
    "fluorophore_consensus": {
        "accept_fluorophore_consensus": "accept_live_consensus",
        "revise_signal_dark_measurements": "decide",
        "reselect_reference_rois": "decide",
        "abort": "decide",
    },
    "target_af": {
        "review_reference_control_groups": "propose_target_af_groups",
        "reuse_reference_unstained_control": "propose_control_target_af",
        "select_target_unstained_control": "propose_control_target_af",
        "retry_target_af_source": "decide",
        "abort": "decide",
    },
    "target_af_control_review": {
        "accept_control_target_af": "review_control_final_basis",
        "retry_control_target_af": "decide",
        "reselect_target_af_source": "decide",
        "abort": "decide",
    },
    "final_run_basis": {"abort": "decide", "reselect_basis_source": "decide"},
}


def _operation_action_for_option(
    *,
    checkpoint: str,
    option_value: str,
    proposed_artifacts_present: bool,
) -> str:
    if option_value == "revise_shared_control":
        return "decide"
    if checkpoint == "final_run_basis" and option_value == "accept_final_run_basis":
        return "review_final_basis" if proposed_artifacts_present else "propose_final_basis"
    try:
        return _OPERATION_ACTION_BY_CHECKPOINT_OPTION[checkpoint][option_value]
    except KeyError as exc:
        raise ValueError("offered reference option has no safe configured operation") from exc


def _form_field(
    name: str,
    label: str,
    help_text: str,
    value_kind: ReferenceUnmixingFormValueKind,
    *,
    minimum_count: int = 1,
    required: bool = True,
    identifier_choices: tuple[str, ...] = (),
    value_choices: tuple[str, ...] = (),
    mapping_key_choices: tuple[str, ...] = (),
    mapping_value_choices: tuple[str, ...] = (),
    mapping_value_source_field: str | None = None,
) -> ReferenceUnmixingFormField:
    return ReferenceUnmixingFormField(
        name=name,
        label=label,
        help_text=help_text,
        value_kind=value_kind,
        required=required,
        minimum_count=minimum_count,
        identifier_choices=identifier_choices,
        value_choices=value_choices,
        mapping_key_choices=mapping_key_choices,
        mapping_value_choices=mapping_value_choices,
        mapping_value_source_field=mapping_value_source_field,
    )


def _visible_form_fields(
    *,
    checkpoint: str,
    option_value: str,
    operation_action: str,
    measurement_ids: tuple[str, ...],
    offered_ids: tuple[str, ...],
    minimum_selected_ids: int,
    artifact_requirements: tuple[ReferenceUnmixingArtifactRequirement, ...],
    correction_mode: str | None = None,
    component_names: tuple[str, ...] = (),
    af_control_set_review_sha256s: tuple[str, ...] = (),
) -> tuple[ReferenceUnmixingFormField, ...]:
    reviewer = _form_field(
        "reviewer_id",
        "Reviewer ID",
        "Enter the identity of the reviewer making this decision.",
        ReferenceUnmixingFormValueKind.TEXT,
    )

    def locator(
        name: str,
        label: str,
        help_text: str,
    ) -> ReferenceUnmixingFormField:
        return _form_field(
            name,
            label,
            help_text,
            ReferenceUnmixingFormValueKind.LOCATOR,
        )

    def identifiers(
        name: str,
        label: str,
        help_text: str,
        minimum: int = 1,
    ) -> ReferenceUnmixingFormField:
        return _form_field(
            name,
            label,
            help_text,
            ReferenceUnmixingFormValueKind.ORDERED_IDENTIFIER_SEQUENCE,
            minimum_count=minimum,
            identifier_choices=offered_ids,
        )

    if operation_action == "decide":
        if minimum_selected_ids and option_value not in {
            "reselect_reference_rois",
            "revise_signal_dark_measurements",
            "reselect_target_af_source",
        }:
            return (
                reviewer,
                _form_field(
                    "selected_ids",
                    "Selected identifiers",
                    "Select the exact ordered identifier bindings for this option.",
                    ReferenceUnmixingFormValueKind.IDENTIFIER_MAPPING,
                    minimum_count=minimum_selected_ids,
                    mapping_value_choices=offered_ids,
                ),
            )
        return (reviewer,)
    if operation_action == "choose_curated_basis":
        return (
            reviewer,
            _form_field(
                "library_locator", "Spectral library file",
                "Choose a .miflib.zip package, reviewed library JSON or measurement-by-component CSV.",
                ReferenceUnmixingFormValueKind.LOCATOR, required=False,
            ),
            _form_field(
                "spectral_csv", "Spectral values",
                "Enter a reference value for every acquired band and component.",
                ReferenceUnmixingFormValueKind.TEXT, required=False,
            ),
            _form_field(
                "normalization",
                "Supplied spectrum normalization",
                "Select the normalization already used by the supplied spectra.",
                ReferenceUnmixingFormValueKind.ENUM,
                value_choices=("l1", "l2"),
                required=False,
            ),
            _form_field(
                "measurement_convention",
                "Reference intensities before normalization",
                "Select the correction applied before the reference spectra were normalized.",
                ReferenceUnmixingFormValueKind.ENUM,
                value_choices={
                    "full": ("acquisition_normalized", "target_raw_acquisition"),
                    "exposure_only": ("exposure_normalized", "target_raw_acquisition"),
                    "as_stored": ("stored_calibrated",),
                }[correction_mode],
                required=False,
            ),
        )
    if operation_action == "choose_basis_source":
        return (
            reviewer,
            *((identifiers(
                "component_names", "Fluorophore components",
                "Enter one fluorophore or stain name per reference image, in output order. "
                "Do not enter detector bands or autofluorescence; autofluorescence is reviewed separately.",
            ),) if option_value == "construct_reference_library" and not component_names else ()),
            *(
                (
                    locator(
                        "library_locator",
                        "Reference library",
                        "Select a reviewed library JSON or .miflib.zip package. It retains the measurement axis, spectra and calibration provenance; reference images need not be sampled again. Bare CSV or unannotated third-party matrices are not accepted here.",
                    ),
                )
                if option_value == "import_reference_library"
                else ()
            ),
        )
    if operation_action == "bind_reference_controls":
        roles = {item.role for item in artifact_requirements}
        common = (
            identifiers(
                "positive_input_ids",
                "Positive reference inputs",
                "Select the ordered positive-reference input inventory.",
            ),
        )
        if "unstained_reference_image" in roles:
            controls = (
                _form_field(
                    "shared_unstained_input_id",
                    "Shared unstained control",
                    "Select the shared unstained-control input.",
                    ReferenceUnmixingFormValueKind.IDENTIFIER,
                    identifier_choices=offered_ids,
                ),
            )
        else:
            controls = (
                identifiers(
                    "negative_input_ids",
                    "Negative-control inputs",
                    "Select the ordered per-stain negative-control inventory.",
                ),
            )
        return (*common, *controls, reviewer)
    if (
        checkpoint == "signal_dark_measurements"
        and operation_action == "advance_reference_purification"
    ):
        fields = (
            reviewer,
            _form_field(
                "signal_measurement_id",
                "Signal measurement",
                "Select the signal measurement for this fluorophore.",
                ReferenceUnmixingFormValueKind.IDENTIFIER,
                identifier_choices=measurement_ids,
            ),
            _form_field(
                "dark_measurement_ids",
                "Dark measurements",
                "Select the ordered dark-measurement inventory.",
                ReferenceUnmixingFormValueKind.ORDERED_IDENTIFIER_SEQUENCE,
                identifier_choices=measurement_ids,
            ),
        )
        if af_control_set_review_sha256s:
            fields += (_form_field(
                "af_control_set_review_sha256s", "Reviewed AF control spectra",
                "Review the exact pooled control spectra before extracting the fluorophore.",
                ReferenceUnmixingFormValueKind.ORDERED_IDENTIFIER_SEQUENCE,
                identifier_choices=af_control_set_review_sha256s,
                minimum_count=len(af_control_set_review_sha256s),
            ),)
        return fields
    if (
        checkpoint
        in {
            "positive_reference_rois",
            "background_reference_rois",
        }
        and operation_action == "advance_reference_purification"
    ):
        return (
            reviewer,
            locator(
                "roi_export_locator",
                "Reviewed ROI export",
                "Select the registered project-owned reviewed ROI export.",
            ),
            identifiers(
                "selected_roi_ids",
                "Reviewed ROI identifiers",
                "Select the ordered ROI inventory for this checkpoint.",
            ),
        )
    if (
        checkpoint in {"af_reference_rois", "negative_reference_rois"}
        and operation_action == "advance_reference_purification"
    ):
        return (
            reviewer,
            locator(
                "roi_export_locator",
                "Reviewed ROI export",
                "Select the registered project-owned reviewed ROI export.",
            ),
            _form_field(
                "selected_roi_ids",
                "Control ROI identifiers",
                "Select the ordered reviewed control ROI inventory.",
                ReferenceUnmixingFormValueKind.ORDERED_IDENTIFIER_SEQUENCE,
            ),
            _form_field(
                "pairings",
                "AF control sets" if checkpoint == "af_reference_rois" else "Matched controls",
                "Choose the compatible control regions used for each stained region.",
                (ReferenceUnmixingFormValueKind.IDENTIFIER_MULTIMAPPING if checkpoint == "af_reference_rois"
                 else ReferenceUnmixingFormValueKind.IDENTIFIER_MAPPING),
                mapping_key_choices=offered_ids,
                mapping_value_source_field="selected_roi_ids",
            ),
        )
    if checkpoint == "extraction_qc" and operation_action == "advance_reference_purification":
        return (
            reviewer,
            identifiers(
                "included_roi_ids",
                "Included ROI identifiers",
                "Select the ordered ROI inventory included in consensus.",
            ),
            identifiers(
                "excluded_roi_ids",
                "Excluded ROI identifiers",
                "Select the ordered ROI inventory excluded from consensus.",
                minimum=0,
            ),
        )
    if operation_action == "accept_live_consensus":
        return (reviewer, _form_field(
            "consensus_roi_review", "Compare reference regions",
            "Inspect each region and the combined spectrum. Explicitly retain or exclude flagged differences.",
            ReferenceUnmixingFormValueKind.PRIMITIVE_MAPPING,
        ))
    if operation_action in {"accept_control_target_af", "review_control_final_basis"}:
        return (reviewer,)
    if operation_action == "propose_target_af_groups":
        return (reviewer, _form_field(
            "target_af_groups", "Compare AF regions",
            "Review target suitability and group the included control regions.",
            ReferenceUnmixingFormValueKind.PRIMITIVE_MAPPING,
        ))
    if operation_action == "propose_control_target_af":
        control = (
            ()
            if option_value == "reuse_reference_unstained_control"
            else (
                _form_field(
                    "control_input_id",
                    "Target unstained control",
                    "Select the target-matched unstained-control input.",
                    ReferenceUnmixingFormValueKind.IDENTIFIER,
                    identifier_choices=offered_ids,
                ),
            )
        )
        background_minimum = 0 if option_value == "reuse_reference_unstained_control" else 1
        return (
            *control,
            locator(
                "roi_export_locator",
                "Reviewed ROI export",
                "Select the registered project-owned reviewed ROI export.",
            ),
            identifiers(
                "af_roi_ids",
                "Target-AF ROI identifiers",
                "Select the ordered target-AF ROI inventory.",
            ),
            identifiers(
                "background_roi_ids",
                "Background ROI identifiers",
                "Select the ordered target-background ROI inventory.",
                minimum=background_minimum,
            ),
            _form_field(
                "roi_reviews",
                "Target-AF ROI decisions",
                "Review every selected target-AF ROI and its component identity.",
                ReferenceUnmixingFormValueKind.IDENTIFIER_DISPOSITION_MAPPING,
                identifier_choices=offered_ids,
                value_choices=("included", "excluded"),
            ),
            _form_field(
                "control_review",
                "Target/control compatibility review",
                "Review preparation and tissue-context compatibility.",
                ReferenceUnmixingFormValueKind.ENUM_MAPPING,
                mapping_key_choices=(
                    "preparation_disposition",
                    "tissue_context_disposition",
                ),
                value_choices=("accepted", "rejected"),
            ),
            reviewer,
        )
    if operation_action == "propose_final_basis":
        return ()
    if operation_action == "review_final_basis":
        return (reviewer,)
    return ()


@dataclass(frozen=True, slots=True)
class ReferenceUnmixingState:
    """Bounded path/pointer/record-free view of persisted workflow state."""

    run_id: str
    status: ReferenceUnmixingStatus
    configuration_id: str
    target_id: str
    start_authority_id: str
    checkpoint_token: str
    checkpoint_revision: int
    checkpoint: str | None
    fluorophore: str | None
    measurement_ids: tuple[str, ...]
    offered_actions: tuple[str, ...]
    operation_requirements: tuple[ReferenceUnmixingOperationRequirement, ...]
    offered_ids: tuple[str, ...]
    review_summary: tuple[tuple[str, object], ...]

    def __post_init__(self) -> None:
        for value in (
            self.configuration_id,
            self.target_id,
            self.start_authority_id,
            self.checkpoint_token,
        ):
            if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
                raise ValueError("reference unmixing state contains an invalid identity")
        _identifier_tuple(self.offered_actions, "offered actions", allow_empty=True)
        _identifier_tuple(self.measurement_ids, "measurement IDs", allow_empty=False)
        _exact_tuple(
            self.operation_requirements,
            ReferenceUnmixingOperationRequirement,
            "operation requirements",
        )
        if tuple(item.option_value for item in self.operation_requirements) != self.offered_actions:
            raise ValueError("operation requirements differ from offered actions")
        _identifier_tuple(self.offered_ids, "offered IDs", allow_empty=True)
        frozen = _freeze_json_value(_thaw_json_value(self.review_summary), label="review summary")
        if frozen != self.review_summary:
            raise ValueError("review summary is not canonical")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_name": "mif_pipeline.reference_unmixing_state",
            "schema_version": 3,
            "run_id": self.run_id,
            "status": self.status.value,
            "configuration_id": self.configuration_id,
            "target_id": self.target_id,
            "start_authority_id": self.start_authority_id,
            "checkpoint_token": self.checkpoint_token,
            "checkpoint_revision": self.checkpoint_revision,
            "checkpoint": self.checkpoint,
            "fluorophore": self.fluorophore,
            "measurement_ids": list(self.measurement_ids),
            "offered_actions": list(self.offered_actions),
            "operation_requirements": [item.to_mapping() for item in self.operation_requirements],
            "offered_ids": list(self.offered_ids),
            "review_summary": _thaw_json_value(self.review_summary),
        }


def _same_strict_json(left: object, right: object) -> bool:
    return _canonical_json({"value": left}) == _canonical_json({"value": right})


def _semantic_configuration_for_option(
    operation_action: str,
    option_value: str,
    declared_form_fields: set[str] | frozenset[str] = frozenset(),
) -> dict[str, object]:
    if operation_action == "decide":
        return {
            "option": option_value,
            **({} if "selected_ids" in declared_form_fields else {"selected_ids": []}),
        }
    if operation_action == "choose_curated_basis":
        return {}
    if operation_action == "choose_basis_source":
        return {"choice": option_value}
    if operation_action == "review_final_basis":
        decisions = {
            "accept_final_run_basis": "accepted",
        }
        try:
            return {"decision": decisions[option_value]}
        except KeyError as exc:
            raise ValueError("configured final-basis action differs from option intent") from exc
    if operation_action == "bind_reference_controls":
        values = (
            {"negative_input_ids": []}
            if "shared_unstained_input_id" in declared_form_fields
            else {"shared_unstained_input_id": None}
        )
        if option_value == "accept_reference_control_files_with_source_preparation":
            values["prepare_shared_control"] = True
        return values
    if (
        operation_action == "propose_control_target_af"
        and option_value == "reuse_reference_unstained_control"
    ):
        return {"control_input_id": None}
    return {}


def _canonical_registration_semantics(
    requirement: ReferenceUnmixingOperationRequirement,
) -> dict[str, object]:
    if (
        requirement.operation_action != "register_canonical_input"
        or len(requirement.artifact_requirements) != 1
    ):
        raise ValueError("canonical-input registration lacks one exact artifact requirement")
    artifact = requirement.artifact_requirements[0]
    if artifact.role == "positive_reference_image":
        if artifact.subject_id is None:
            raise ValueError("positive reference registration lacks fluorophore")
        return {
            "input_role": "single_component_reference",
            "fluorophore_id": artifact.subject_id,
        }
    if artifact.role == "negative_reference_image":
        if artifact.subject_id is None:
            raise ValueError("negative reference registration lacks fluorophore")
        return {
            "input_role": "single_stain_negative_control",
            "fluorophore_id": artifact.subject_id,
        }
    if artifact.role == "unstained_reference_image":
        return {"input_role": "unstained_control"}
    raise ValueError("canonical-input registration role is unsupported")


def _pending_reference_semantics(state: ReferenceUnmixingState) -> dict[str, object]:
    pending = _thaw_json_value(state.review_summary).get("pending_reference_input")
    if not isinstance(pending, dict):
        raise ValueError("The pending reference review is no longer available")
    return {key: pending[key] for key in ("pending_id", "input_role", "fluorophore_id")}


def _validate_declared_form_value(
    field: ReferenceUnmixingFormField,
    value: object,
) -> None:
    if field.name == "spectral_csv":
        _validate_bounded_json_value({"spectral_csv": value}, label="spectral table", spectral_table=True)
    else:
        _validate_bounded_json_value(value, label=f"form field {field.name!r}")
    if not field.required and (value is None or value == ""):
        return
    kind = field.value_kind
    if kind is ReferenceUnmixingFormValueKind.ENUM:
        if type(value) is not str or value not in field.value_choices:
            raise ValueError(f"Choose one of the offered values for {field.label}")
        return
    if kind in {
        ReferenceUnmixingFormValueKind.TEXT,
        ReferenceUnmixingFormValueKind.LOCATOR,
        ReferenceUnmixingFormValueKind.IDENTIFIER,
    }:
        if type(value) is not str or not value.strip():
            raise ValueError(f"form field {field.name!r} requires non-empty text")
        if (
            kind is ReferenceUnmixingFormValueKind.IDENTIFIER
            and field.identifier_choices
            and value not in field.identifier_choices
        ):
            raise ValueError(f"form field {field.name!r} identifier is not offered")
        return
    if kind in {
        ReferenceUnmixingFormValueKind.ORDERED_IDENTIFIER_SEQUENCE,
        ReferenceUnmixingFormValueKind.REASON_CODE_SEQUENCE,
    }:
        if (
            type(value) is not list
            or len(value) < field.minimum_count
            or any(type(item) is not str or not item.strip() for item in value)
            or len(value) != len(set(value))
        ):
            raise ValueError(f"form field {field.name!r} requires an ordered unique text list")
        if field.identifier_choices and any(item not in field.identifier_choices for item in value):
            raise ValueError(f"form field {field.name!r} identifier is not offered")
        if kind is ReferenceUnmixingFormValueKind.ORDERED_IDENTIFIER_SEQUENCE:
            maximum = (
                len(field.identifier_choices)
                if field.identifier_choices
                else _MAX_FRONTEND_SEQUENCE_ITEMS
            )
            if len(value) > maximum:
                raise ValueError(f"form field {field.name!r} exceeds its maximum count")
            if field.identifier_choices:
                selected = set(value)
                canonical = tuple(item for item in field.identifier_choices if item in selected)
                if tuple(value) != canonical:
                    raise ValueError(f"form field {field.name!r} must retain canonical order")
        if field.value_choices and any(item not in field.value_choices for item in value):
            raise ValueError(f"form field {field.name!r} value is not offered")
        return
    if kind in {ReferenceUnmixingFormValueKind.IDENTIFIER_MAPPING,
                ReferenceUnmixingFormValueKind.IDENTIFIER_MULTIMAPPING}:
        if (
            type(value) is not list
            or len(value) < field.minimum_count
            or any(
                type(item) is not list
                or len(item) != 2
                or type(item[0]) is not str or not item[0].strip()
                or item[1] is None
                or (item[1] is not None and (type(item[1]) is not str or not item[1].strip()))
                for item in value
            )
        ):
            raise ValueError(f"form field {field.name!r} requires ordered identifier pairs")
        pairs = tuple((item[0], item[1]) for item in value)
        if len(pairs) != len(set(pairs)):
            raise ValueError(f"form field {field.name!r} contains duplicate pairs")
        maximum = (
            len(field.mapping_key_choices)
            if field.mapping_key_choices
            else (
                len(field.mapping_value_choices)
                if field.mapping_value_choices
                else _MAX_FRONTEND_SEQUENCE_ITEMS
            )
        )
        if kind is ReferenceUnmixingFormValueKind.IDENTIFIER_MULTIMAPPING:
            maximum *= _MAX_FRONTEND_SEQUENCE_ITEMS
        if len(pairs) > maximum:
            raise ValueError(f"form field {field.name!r} exceeds its maximum count")
        if field.mapping_key_choices:
            keys = tuple(item[0] for item in pairs)
            if kind is ReferenceUnmixingFormValueKind.IDENTIFIER_MULTIMAPPING:
                keys = tuple(key for index, key in enumerate(keys) if index == 0 or key != keys[index - 1])
            elif len(keys) != len(set(keys)):
                raise ValueError(f"form field {field.name!r} requires unique mapping keys")
            if keys != field.mapping_key_choices:
                raise ValueError(
                    f"form field {field.name!r} must cover mapping keys in canonical order"
                )
        else:
            values = tuple(item[1] for item in pairs)
            if len(values) != len(set(values)):
                raise ValueError(f"form field {field.name!r} requires unique identifiers")
        values = tuple(item[1] for item in pairs)
        if field.mapping_value_choices and any(
            item not in field.mapping_value_choices for item in values
        ):
            raise ValueError(f"form field {field.name!r} identifier is not offered")
        return
    if kind is ReferenceUnmixingFormValueKind.IDENTIFIER_DISPOSITION_MAPPING:
        if (
            type(value) is not list
            or len(value) < field.minimum_count
            or any(type(item) is not dict for item in value)
        ):
            raise ValueError(f"form field {field.name!r} requires identifier decision mappings")
        for item in value:
            if set(item) != {"roi_id", "component_identity", "included"}:
                raise ValueError(f"form field {field.name!r} decision fields differ")
            identifier = item.get("roi_id")
            if type(identifier) is not str or not identifier.strip():
                raise ValueError(f"form field {field.name!r} decision lacks an identifier")
            if field.identifier_choices and identifier not in field.identifier_choices:
                raise ValueError(f"form field {field.name!r} identifier is not offered")
            disposition = item.get("included")
            if type(disposition) is not bool:
                raise ValueError(f"form field {field.name!r} decision lacks a disposition")
            literal = "included" if disposition else "excluded"
            if field.value_choices and literal not in field.value_choices:
                raise ValueError(f"form field {field.name!r} value is not offered")
            component_identity = item.get("component_identity")
            if type(component_identity) is not str or not component_identity.strip():
                raise ValueError(f"form field {field.name!r} decision lacks a component identity")
        identifiers = tuple(item["roi_id"] for item in value)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError(f"form field {field.name!r} requires unique identifiers")
        maximum = (
            len(field.identifier_choices)
            if field.identifier_choices
            else _MAX_FRONTEND_SEQUENCE_ITEMS
        )
        if len(identifiers) > maximum:
            raise ValueError(f"form field {field.name!r} exceeds its maximum count")
        if field.identifier_choices:
            selected = set(identifiers)
            canonical = tuple(item for item in field.identifier_choices if item in selected)
            if identifiers != canonical:
                raise ValueError(f"form field {field.name!r} must retain canonical order")
        return
    if kind is ReferenceUnmixingFormValueKind.ENUM_MAPPING:
        if type(value) is not dict or set(value) != set(field.mapping_key_choices):
            raise ValueError(f"form field {field.name!r} requires its exact mapping keys")
        if any(type(item) is not str or item not in field.value_choices for item in value.values()):
            raise ValueError(f"form field {field.name!r} contains an unsupported mapping value")
        return
    if kind is ReferenceUnmixingFormValueKind.PRIMITIVE_MAPPING:
        if type(value) is not dict:
            raise ValueError(f"form field {field.name!r} requires an object")
    elif kind is ReferenceUnmixingFormValueKind.PRIMITIVE_LIST:
        if type(value) is not list or len(value) < field.minimum_count:
            raise ValueError(f"form field {field.name!r} requires an array")
    else:
        raise ValueError(f"form field {field.name!r} has an unsupported value kind")
    _freeze_json_value(value, label=f"form field {field.name!r}")


def _validate_cross_field_form_intent(
    state: ReferenceUnmixingState,
    requirement: ReferenceUnmixingOperationRequirement,
    form_values: Mapping[str, object],
) -> None:
    for field in requirement.form_fields:
        source_name = field.mapping_value_source_field
        if source_name is None:
            continue
        pairs = form_values.get(field.name)
        offered_values = form_values.get(source_name)
        if (
            type(pairs) is list
            and type(offered_values) is list
            and any(item[1] not in offered_values for item in pairs)
        ):
            raise ValueError(f"form field {field.name!r} uses an unselected mapping value")
        if (field.value_kind is ReferenceUnmixingFormValueKind.IDENTIFIER_MULTIMAPPING
                and type(pairs) is list and type(offered_values) is list):
            for key in field.mapping_key_choices:
                members = [item[1] for item in pairs if item[0] == key]
                if members != [value for value in offered_values if value in members]:
                    raise ValueError("AF control-set members must retain selected control order")
            if set(offered_values) != {item[1] for item in pairs}:
                raise ValueError("Every selected AF control must belong to a used control set")
    if state.checkpoint == "signal_dark_measurements":
        signal_measurement_id = form_values.get("signal_measurement_id")
        dark_measurement_ids = form_values.get("dark_measurement_ids")
        if (
            type(signal_measurement_id) is str
            and type(dark_measurement_ids) is list
            and signal_measurement_id in dark_measurement_ids
        ):
            raise ValueError("signal and dark measurement IDs must be distinct")
    if state.checkpoint == "extraction_qc":
        included = form_values.get("included_roi_ids")
        excluded = form_values.get("excluded_roi_ids")
        if type(included) is list and type(excluded) is list:
            if set(included) & set(excluded) or set(included) | set(excluded) != set(
                state.offered_ids
            ):
                raise ValueError(
                    "extraction ROI decisions must form one complete disjoint inventory"
                )
    if state.checkpoint == "target_af":
        af_ids = form_values.get("af_roi_ids")
        background_ids = form_values.get("background_roi_ids")
        reviews = form_values.get("roi_reviews")
        if type(af_ids) is list and type(reviews) is list:
            if tuple(item["roi_id"] for item in reviews) != tuple(af_ids):
                raise ValueError("target-AF ROI reviews must cover the exact selected inventory")
        if (
            type(af_ids) is list
            and type(background_ids) is list
            and set(af_ids) & set(background_ids)
        ):
            raise ValueError("target-AF and background ROI inventories must be disjoint")


def configure_reference_unmixing_action(
    state: ReferenceUnmixingState,
    *,
    option_value: str,
    form_values: Mapping[str, object],
    injected_configuration: Mapping[str, object],
) -> ReferenceUnmixingConfiguredAction:
    """Build one canonical action without making frontends encode semantics."""

    if type(state) is not ReferenceUnmixingState:
        raise TypeError("action configuration requires exact live bounded state")
    if not isinstance(form_values, Mapping) or not isinstance(injected_configuration, Mapping):
        raise TypeError("action form and injected configuration must be mappings")
    _validate_bounded_json_value(form_values, label="configured action form", spectral_table=option_value == "curated_reference_library")
    _validate_bounded_json_value(injected_configuration, label="injected action configuration")
    matches = tuple(
        item for item in state.operation_requirements if item.option_value == option_value
    )
    if len(matches) != 1:
        raise ValueError("configured action intent option is not offered")
    requirement = matches[0]
    declared_form_fields = {item.name for item in requirement.form_fields}
    if set(form_values) != declared_form_fields:
        raise ValueError("configured action values differ from declared form fields")
    for field in requirement.form_fields:
        _validate_declared_form_value(field, form_values[field.name])
    semantic = (
        _canonical_registration_semantics(requirement)
        if requirement.operation_action == "register_canonical_input"
        else _pending_reference_semantics(state)
        if requirement.operation_action == "complete_reference_input"
        else _semantic_configuration_for_option(
            requirement.operation_action, option_value, declared_form_fields
        )
    )
    form_keys = set(form_values)
    injected_keys = set(injected_configuration)
    semantic_keys = set(semantic)
    if form_keys & injected_keys or (semantic_keys & (form_keys | injected_keys)):
        raise ValueError("injected action configuration overlaps frontend-owned intent")
    _reject_caller_authored_scientific_fields(form_values, label="configured action form")
    _reject_caller_authored_scientific_fields(
        injected_configuration, label="injected action configuration"
    )
    action = ReferenceUnmixingConfiguredAction.from_mapping(
        checkpoint_token=state.checkpoint_token,
        action=requirement.operation_action,
        configuration={
            **dict(form_values),
            **dict(injected_configuration),
            **semantic,
        },
    )
    return validate_reference_unmixing_action_intent(
        state,
        option_value=option_value,
        form_values=form_values,
        action=action,
    )


def validate_reference_unmixing_action_intent(
    state: ReferenceUnmixingState,
    *,
    option_value: str,
    form_values: Mapping[str, object],
    action: ReferenceUnmixingConfiguredAction,
) -> ReferenceUnmixingConfiguredAction:
    """Bind a frontend's clicked option and visible values to one returned action."""

    if type(state) is not ReferenceUnmixingState:
        raise TypeError("action intent requires exact live bounded state")
    if type(action) is not ReferenceUnmixingConfiguredAction:
        raise TypeError("action intent requires exact configured action")
    if action.checkpoint_token != state.checkpoint_token:
        raise ValueError("configured action intent checkpoint token is stale")
    _nonempty_text(option_value, "action intent option")
    matches = tuple(
        item for item in state.operation_requirements if item.option_value == option_value
    )
    if len(matches) != 1:
        raise ValueError("configured action intent option is not offered")
    requirement = matches[0]
    declared_form_fields = {item.name for item in requirement.form_fields}
    if action.action != requirement.operation_action:
        raise ValueError("configured action intent changed the operation action")
    if not isinstance(form_values, Mapping):
        raise TypeError("configured action form values must be a mapping")
    _reject_caller_authored_scientific_fields(form_values, label="configured action form")
    if set(form_values) != declared_form_fields:
        raise ValueError("configured action form intent fields differ")
    for field in requirement.form_fields:
        _validate_declared_form_value(field, form_values[field.name])
    _validate_cross_field_form_intent(state, requirement, form_values)
    if action.action == "decide":
        required_form_fields = {"reviewer_id", "selected_ids"}
        injected_fields = set()
        semantic_fields = _semantic_configuration_for_option(
            action.action, option_value, declared_form_fields
        )
    elif action.action == "choose_basis_source":
        required_form_fields = {"reviewer_id"}
        if option_value == "import_reference_library":
            required_form_fields.add("library_locator")
        if option_value == "construct_reference_library" and "component_names" in declared_form_fields:
            required_form_fields.add("component_names")
        injected_fields = set()
        semantic_fields = {"choice": option_value}
    elif action.action == "choose_curated_basis":
        required_form_fields = {
            "reviewer_id",
            "library_locator",
            "spectral_csv",
            "normalization",
            "measurement_convention",
        }
        injected_fields = set()
        semantic_fields = {}
    elif action.action == "select_reference_regions":
        from .reference_region_selection import FORM_FIELDS, POLICY_FIELDS
        required_form_fields = FORM_FIELDS
        injected_fields = POLICY_FIELDS
        semantic_fields = {}
    elif action.action in {"prepare_shared_control", "accept_shared_control", "prepare_stained_reference", "assign_shared_control_sets", "review_control_sets", "revise_component_preparation"}:
        from .reference_source_preparation import form_fields, POLICIES
        required_form_fields = form_fields(action.action)
        injected_fields = POLICIES[action.action]
        semantic_fields = {}
    elif action.action == "register_canonical_input":
        required_form_fields = {"locator", "input_id"}
        injected_fields = set()
        semantic_fields = _canonical_registration_semantics(requirement)
    elif action.action == "complete_reference_input":
        required_form_fields = {"calibration", "acquisition_confirmation", "reviewer_id"}
        injected_fields = set()
        semantic_fields = _pending_reference_semantics(state)
    elif action.action == "bind_reference_controls":
        required_form_fields = {
            "positive_input_ids",
            "shared_unstained_input_id",
            "negative_input_ids",
            "reviewer_id",
        }
        injected_fields = set()
        semantic_fields = _semantic_configuration_for_option(
            action.action, option_value, declared_form_fields
        )
    elif (
        state.checkpoint == "signal_dark_measurements"
        and action.action == "advance_reference_purification"
    ):
        required_form_fields = {"reviewer_id", "signal_measurement_id", "dark_measurement_ids"}
        injected_fields = {"purification_policy"}
        semantic_fields: dict[str, object] = {}
    elif (
        state.checkpoint
        in {
            "positive_reference_rois",
            "background_reference_rois",
        }
        and action.action == "advance_reference_purification"
    ):
        required_form_fields = {"reviewer_id", "roi_export_locator", "selected_roi_ids"}
        injected_fields = {"sampling_policy"}
        if state.checkpoint == "background_reference_rois":
            injected_fields.add("background_policy")
        semantic_fields = {}
    elif (
        state.checkpoint in {"af_reference_rois", "negative_reference_rois"}
        and action.action == "advance_reference_purification"
    ):
        required_form_fields = {"reviewer_id", "roi_export_locator", "selected_roi_ids", "pairings"}
        injected_fields = {"sampling_policy"}
        semantic_fields = {}
    elif state.checkpoint == "extraction_qc" and action.action == "advance_reference_purification":
        required_form_fields = {"reviewer_id", "included_roi_ids", "excluded_roi_ids"}
        injected_fields = set()
        semantic_fields = {}
    elif action.action == "accept_live_consensus":
        required_form_fields = {"reviewer_id", "consensus_roi_review"}
        injected_fields = {"consensus_policy", "qc_policy"}
        semantic_fields = {}
    elif action.action == "propose_target_af_groups":
        required_form_fields = {"reviewer_id", "target_af_groups"}
        injected_fields = set()
        semantic_fields = {}
        if option_value != "review_reference_control_groups":
            raise ValueError("AF grouping action differs from option intent")
        from mif_pipeline.preprocessing.unmixing.reference_service_af_groups import _selection
        inventory = _thaw_json_value(state.review_summary).get("target_af_groups")
        if not isinstance(inventory, dict):
            raise ValueError("AF grouping action lacks its offered candidate inventory")
        _selection(inventory, form_values.get("target_af_groups"))
    elif action.action == "propose_control_target_af":
        required_form_fields = {
            "control_input_id",
            "roi_export_locator",
            "af_roi_ids",
            "background_roi_ids",
            "roi_reviews",
            "control_review",
            "reviewer_id",
        }
        injected_fields = {
            "sampling_policy",
            "background_policy",
            "estimation_policy",
            "qc_policy",
        }
        semantic_fields = {}
        control_input_id = form_values.get("control_input_id")
        if option_value == "reuse_reference_unstained_control":
            if "control_input_id" in declared_form_fields:
                raise ValueError("reuse option must not expose a separate control input")
            semantic_fields = _semantic_configuration_for_option(
                action.action, option_value, declared_form_fields
            )
        elif option_value == "select_target_unstained_control":
            if type(control_input_id) is not str or not control_input_id.strip():
                raise ValueError("configured control input differs from selection option intent")
        else:
            raise ValueError("configured control target-AF action differs from option intent")
    elif action.action in {"accept_control_target_af", "review_control_final_basis"}:
        required_form_fields = {"reviewer_id"}
        injected_fields = set()
        semantic_fields = {}
    elif state.checkpoint == "final_run_basis" and action.action == "propose_final_basis":
        required_form_fields = set()
        injected_fields = {"qc_policy"}
        semantic_fields = {}
    elif state.checkpoint == "final_run_basis" and action.action == "review_final_basis":
        required_form_fields = {"reviewer_id"}
        injected_fields = set()
        semantic_fields = _semantic_configuration_for_option(
            action.action, option_value, declared_form_fields
        )
    else:
        raise ValueError("configured action intent has no closed frontend field binding")
    required_form_fields = declared_form_fields
    if set(form_values) != required_form_fields:
        raise ValueError("configured action form intent fields differ")
    configuration = action.configuration_mapping()
    if set(configuration) != (required_form_fields | injected_fields | set(semantic_fields)):
        raise ValueError("configured action fields differ from closed form intent")
    for key, value in form_values.items():
        if not _same_strict_json(configuration[key], value):
            raise ValueError(f"configured action changed form intent field {key!r}")
    for key, value in semantic_fields.items():
        if not _same_strict_json(configuration[key], value):
            raise ValueError(f"configured action changed option intent field {key!r}")
    return action


@dataclass(frozen=True, slots=True)
class ReferenceUnmixingCompletion:
    """Persisted JSON-safe receipt for one authoritative unmixing publication."""

    run_id: str
    config_sha256: str
    target_image_sha256: str
    checkpoint_sha256: str
    final_authority_kind: str
    final_authority_id: str
    final_authority_sha256: str
    output_status: str
    output_path: str
    output_sha256: str
    provenance_path: str
    provenance_sha256: str
    regions_processed: int
    pyramid_levels: int
    warnings: tuple[str, ...]
    authority_pointer_path: str | None
    authority_pointer_sha256: str | None
    publication_manifest_sha256: str | None
    publication_generation_id: str | None
    publication_request_sha256: str | None
    publication_reused: bool
    publication_warnings: tuple[str, ...]
    clipping_evidence_path: str | None
    clipping_evidence_sha256: str | None
    content_sha256: str

    def __post_init__(self) -> None:
        for value in (
            self.config_sha256,
            self.target_image_sha256,
            self.checkpoint_sha256,
            self.final_authority_sha256,
            self.output_sha256,
            self.provenance_sha256,
            self.content_sha256,
        ):
            if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
                raise ValueError("completion receipt contains an invalid SHA-256")
        for value in (
            self.authority_pointer_sha256,
            self.clipping_evidence_sha256,
        ):
            if value is not None and (
                type(value) is not str or _SHA256_RE.fullmatch(value) is None
            ):
                raise ValueError("completion receipt contains an invalid artifact SHA-256")
        if _sha256(self._content_mapping()) != self.content_sha256:
            raise ValueError("completion receipt content SHA-256 is stale")

    def _content_mapping(self) -> dict[str, object]:
        return {
            "schema_name": "mif_pipeline.reference_unmixing_completion",
            "schema_version": 1,
            "run_id": self.run_id,
            "config_sha256": self.config_sha256,
            "target_image_sha256": self.target_image_sha256,
            "checkpoint_sha256": self.checkpoint_sha256,
            "final_authority_kind": self.final_authority_kind,
            "final_authority_id": self.final_authority_id,
            "final_authority_sha256": self.final_authority_sha256,
            "output_status": self.output_status,
            "output_path": self.output_path,
            "output_sha256": self.output_sha256,
            "provenance_path": self.provenance_path,
            "provenance_sha256": self.provenance_sha256,
            "regions_processed": self.regions_processed,
            "pyramid_levels": self.pyramid_levels,
            "warnings": list(self.warnings),
            "authority_pointer_path": self.authority_pointer_path,
            "authority_pointer_sha256": self.authority_pointer_sha256,
            "publication_manifest_sha256": self.publication_manifest_sha256,
            "publication_generation_id": self.publication_generation_id,
            "publication_request_sha256": self.publication_request_sha256,
            "publication_reused": self.publication_reused,
            "publication_warnings": list(self.publication_warnings),
            "clipping_evidence_path": self.clipping_evidence_path,
            "clipping_evidence_sha256": self.clipping_evidence_sha256,
        }

    def to_mapping(self) -> dict[str, object]:
        return {**self._content_mapping(), "content_sha256": self.content_sha256}

    @classmethod
    def create(
        cls,
        *,
        request: ReferenceUnmixingRequest,
        checkpoint_sha256: str,
        application: ResolvedBasisApplication,
        result: UnmixingResult,
    ) -> "ReferenceUnmixingCompletion":
        content = {
            "schema_name": "mif_pipeline.reference_unmixing_completion",
            "schema_version": 1,
            "run_id": request.run_id,
            "config_sha256": request.config_sha256,
            "target_image_sha256": application.target_image_sha256,
            "checkpoint_sha256": checkpoint_sha256,
            "final_authority_kind": application.authority_kind,
            "final_authority_id": application.authority_id,
            "final_authority_sha256": application.authority_sha256,
            "output_status": result.status,
            "output_path": str(result.output_path),
            "output_sha256": _file_sha256(result.output_path, "unmixed output"),
            "provenance_path": str(result.provenance_path),
            "provenance_sha256": _file_sha256(result.provenance_path, "unmixing provenance"),
            "regions_processed": result.regions_processed,
            "pyramid_levels": result.pyramid_levels,
            "warnings": list(result.warnings),
            "authority_pointer_path": (
                None
                if result.authority_pointer_path is None
                else str(result.authority_pointer_path)
            ),
            "authority_pointer_sha256": (
                None
                if result.authority_pointer_path is None
                else _file_sha256(result.authority_pointer_path, "unmixing publication pointer")
            ),
            "publication_manifest_sha256": result.publication_manifest_sha256,
            "publication_generation_id": result.publication_generation_id,
            "publication_request_sha256": result.publication_request_sha256,
            "publication_reused": result.publication_reused,
            "publication_warnings": list(result.publication_warnings),
            "clipping_evidence_path": (
                None
                if result.clipping_evidence_path is None
                else str(result.clipping_evidence_path)
            ),
            "clipping_evidence_sha256": (
                None
                if result.clipping_evidence_path is None
                else _file_sha256(result.clipping_evidence_path, "unmixing clipping evidence")
            ),
        }
        return cls(
            run_id=request.run_id,
            config_sha256=request.config_sha256,
            target_image_sha256=application.target_image_sha256,
            checkpoint_sha256=checkpoint_sha256,
            final_authority_kind=application.authority_kind,
            final_authority_id=application.authority_id,
            final_authority_sha256=application.authority_sha256,
            output_status=result.status,
            output_path=str(result.output_path),
            output_sha256=content["output_sha256"],
            provenance_path=str(result.provenance_path),
            provenance_sha256=content["provenance_sha256"],
            regions_processed=result.regions_processed,
            pyramid_levels=result.pyramid_levels,
            warnings=result.warnings,
            authority_pointer_path=(
                None
                if result.authority_pointer_path is None
                else str(result.authority_pointer_path)
            ),
            authority_pointer_sha256=content["authority_pointer_sha256"],
            publication_manifest_sha256=result.publication_manifest_sha256,
            publication_generation_id=result.publication_generation_id,
            publication_request_sha256=result.publication_request_sha256,
            publication_reused=result.publication_reused,
            publication_warnings=result.publication_warnings,
            clipping_evidence_path=(
                None
                if result.clipping_evidence_path is None
                else str(result.clipping_evidence_path)
            ),
            clipping_evidence_sha256=content["clipping_evidence_sha256"],
            content_sha256=_sha256(content),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "ReferenceUnmixingCompletion":
        if not isinstance(value, Mapping):
            raise TypeError("completion receipt must be a mapping")
        content_keys = set(cls.__dataclass_fields__) - {"content_sha256"}
        expected = content_keys | {"schema_name", "schema_version", "content_sha256"}
        if (
            set(value) != expected
            or (value["schema_name"], value["schema_version"])
            != ("mif_pipeline.reference_unmixing_completion", 1)
            or type(value["schema_version"]) is not int
        ):
            raise ValueError("completion receipt schema is invalid")
        kwargs = {key: value[key] for key in content_keys}
        for key in ("warnings", "publication_warnings"):
            if type(kwargs[key]) is not list or any(type(item) is not str for item in kwargs[key]):
                raise ValueError("completion receipt warning inventory is invalid")
            kwargs[key] = tuple(kwargs[key])
        return cls(**kwargs, content_sha256=value["content_sha256"])


@dataclass(frozen=True, slots=True)
class _OperationManifest:
    run_id: str
    config_sha256: str
    target_image_sha256: str
    start_authority_sha256: str
    content_sha256: str

    def __post_init__(self) -> None:
        _nonempty_text(self.run_id, "operation manifest run ID")
        for value in (
            self.config_sha256,
            self.target_image_sha256,
            self.start_authority_sha256,
            self.content_sha256,
        ):
            if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
                raise ValueError("operation manifest contains an invalid SHA-256")
        if _sha256(self._content_mapping()) != self.content_sha256:
            raise ValueError("operation manifest content SHA-256 is stale")

    def _content_mapping(self) -> dict[str, object]:
        return {
            "schema_name": _MANIFEST_SCHEMA_NAME,
            "schema_version": _MANIFEST_SCHEMA_VERSION,
            "run_id": self.run_id,
            "config_sha256": self.config_sha256,
            "target_image_sha256": self.target_image_sha256,
            "start_authority_sha256": self.start_authority_sha256,
        }

    def to_mapping(self) -> dict[str, object]:
        return {**self._content_mapping(), "content_sha256": self.content_sha256}

    @classmethod
    def create(
        cls,
        request: ReferenceUnmixingRequest,
        *,
        target_image_sha256: str,
        start_authority_sha256: str,
    ) -> "_OperationManifest":
        content = {
            "schema_name": _MANIFEST_SCHEMA_NAME,
            "schema_version": _MANIFEST_SCHEMA_VERSION,
            "run_id": request.run_id,
            "config_sha256": request.config_sha256,
            "target_image_sha256": target_image_sha256,
            "start_authority_sha256": start_authority_sha256,
        }
        return cls(
            request.run_id,
            request.config_sha256,
            target_image_sha256,
            start_authority_sha256,
            _sha256(content),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "_OperationManifest":
        expected = {
            "schema_name",
            "schema_version",
            "run_id",
            "config_sha256",
            "target_image_sha256",
            "start_authority_sha256",
            "content_sha256",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("operation manifest schema fields differ")
        if (value["schema_name"], value["schema_version"]) != (
            _MANIFEST_SCHEMA_NAME,
            _MANIFEST_SCHEMA_VERSION,
        ) or type(value["schema_version"]) is not int:
            raise ValueError("operation manifest schema is unsupported")
        return cls(
            value["run_id"],
            value["config_sha256"],
            value["target_image_sha256"],
            value["start_authority_sha256"],
            value["content_sha256"],
        )


class ReferenceUnmixingOperation:
    """Own the canonical service lifecycle and final level-0 publication."""

    def __init__(
        self,
        artifact_root: str | Path,
        *,
        backend=None,
        _unmixing_runner=run_unmixing,
    ) -> None:
        self._initialize(
            artifact_root,
            backend=backend,
            unmixing_runner=_unmixing_runner,
            create_layout=True,
        )

    @classmethod
    def open_existing(
        cls,
        artifact_root: str | Path,
        *,
        backend=None,
        _unmixing_runner=run_unmixing,
    ) -> "ReferenceUnmixingOperation":
        """Open a complete operation layout without creating filesystem state."""

        instance = cls.__new__(cls)
        instance._initialize(
            artifact_root,
            backend=backend,
            unmixing_runner=_unmixing_runner,
            create_layout=False,
        )
        return instance

    def _initialize(
        self,
        artifact_root: str | Path,
        *,
        backend,
        unmixing_runner,
        create_layout: bool,
    ) -> None:
        self._root = Path(artifact_root).resolve()
        self._manifest_root = self._root / ".reference-unmixing-operation"
        resources_root = self._manifest_root / "resources"
        if create_layout:
            self._root.mkdir(parents=True, exist_ok=True)
            self._manifest_root.mkdir(parents=True, exist_ok=True)
        elif any(
            path.is_symlink() or not path.is_dir()
            for path in (self._root, self._manifest_root, resources_root)
        ):
            raise ValueError("existing reference-unmixing operation layout is missing or invalid")
        elif (
            (self._root / ".reference-artifacts").is_symlink()
            or not (self._root / ".reference-artifacts").is_dir()
            or any(
                path.is_symlink() or not path.is_file()
                for path in (
                    self._root / "..reference-checkpoint-namespace.publication.lock",
                    self._root / "..reference-artifact-namespace.publication.lock",
                )
            )
        ):
            raise ValueError("existing reference-unmixing store layout is missing or invalid")
        self._store = ReferenceCheckpointStore(self._root)
        self._service = ReferenceWorkflowService(self._store, backend=backend)
        self._resources = (
            ReferenceUnmixingResourceRegistry(resources_root)
            if create_layout
            else ReferenceUnmixingResourceRegistry.open_existing(resources_root)
        )
        self._backend = backend
        self._unmixing_runner = unmixing_runner
        self._job_state_lock = threading.Lock()
        self._active_jobs: set[str] = set()
        self._cancel_before_next_job: set[str] = set()

    def _shared_coordinator(self):
        from mif_pipeline.preprocessing.unmixing.shared_reference_workflow import SharedReferenceCoordinator
        return SharedReferenceCoordinator(self._service)

    def reference_service_identity(self, run_id: str, checkpoint_token: str):
        """Resolve a displayed parent review to its checkpoint-bound cycle task."""
        coordinator = self._shared_coordinator()
        child = coordinator.persisted_child(run_id)
        if child is None:
            return run_id, checkpoint_token
        return child.run_id, coordinator.translate_token(run_id, checkpoint_token)

    def _shared_request(self, request, child):
        config = request.to_unmixing_config()
        if child.phase == "construction":
            config = replace(config, af_behavior=AfBehavior.ALREADY_REMOVED)
        return ReferenceUnmixingRequest.from_reviewed_source(
            run_id=child.run_id, authority=child.authority, reviewed_ome=child.reviewed_ome,
            output_path=self._manifest_root / (child.run_id + ".ome.zarr"), config=config,
            replace_channel_names_with_biomarkers=request.replace_channel_names_with_biomarkers)

    def _project_shared_state(self, manifest, child):
        try:
            child_manifest = self._load_manifest(child.run_id)
        except FileNotFoundError:
            # A child checkpoint can be saved just before its operation manifest.
            # Show that retained review; resume creates the exact child manifest.
            child_manifest = manifest
        view = self._service.load_persisted_view(child.run_id)
        state = self._state_from_view(child_manifest, view)
        summary = _thaw_json_value(state.review_summary)
        summary['shared_reference'] = {'phase': child.phase,
            'cycle': None if child.cycle is None else child.cycle + 1}
        return replace(state, run_id=manifest.run_id, configuration_id=manifest.config_sha256,
            target_id=manifest.target_image_sha256, start_authority_id=manifest.start_authority_sha256,
            checkpoint_token=child.parent_token,
            review_summary=_freeze_json_value(summary, label='review summary'))

    def _advance_shared_session(self, request):
        coordinator = self._shared_coordinator()
        if not coordinator.has_session(request.run_id):
            return None
        retained = coordinator.persisted_child(request.run_id)
        if retained is not None:
            self._resume_accepted_reference_publication(self._service.resume(retained.run_id))
        child = coordinator.active(request.run_id)
        if child is None:
            return None
        child_request = self._shared_request(request, child)
        self._start_with_request(child_request, lambda: self._service.resume(child.run_id))
        return self._project_shared_state(self._load_manifest(request.run_id), child)

    def export_library(self, run_id: str, destination: str | Path) -> Path:
        """Export the accepted fluorophore library and its L1/L2 numerical views."""
        return self._service.export_library(run_id, Path(destination))

    def reopen_stopped_review(self, run_id: str, checkpoint_token: str,
                             previous_checkpoint_token: str, *, publish: bool):
        """Validate the stopped review, optionally publishing its fresh pending request."""
        display_run = run_id
        coordinator = self._shared_coordinator()
        if coordinator.persisted_child(run_id) is not None:
            run_id, checkpoint_token, previous_checkpoint_token = coordinator.translate_stopped_tokens(
                run_id, checkpoint_token, previous_checkpoint_token)
        self._service.reopen_stopped_review(
            run_id, checkpoint_token, previous_checkpoint_token, publish=publish)
        return self.status(display_run) if publish else None

    def prepare_final_review(self, run_id: str, checkpoint_token: str) -> ReferenceUnmixingState:
        """Prepare the small spectral QC result using recorded application defaults."""
        from mif_pipeline.preprocessing.unmixing.reference_defaults import (
            fixed_basis_review_defaults,
        )

        state = self.status(run_id)
        if (
            type(state) is not ReferenceUnmixingState
            or state.checkpoint_token != checkpoint_token
            or state.checkpoint != "final_run_basis"
            or not any(
                r.operation_action == "propose_final_basis" for r in state.operation_requirements
            )
        ):
            raise ValueError("The spectral reference review has changed.")
        service_run, service_token = self.reference_service_identity(run_id, checkpoint_token)
        self._service.propose_final_run_basis_from_policy(
            service_run, service_token, qc_policy=fixed_basis_review_defaults()
        )
        return self.status(run_id)

    def validate_supplied_library(self, run_id, checkpoint_token, **values):
        run_id, checkpoint_token = self.reference_service_identity(run_id, checkpoint_token)
        return self._service.read_supplied_library(run_id, checkpoint_token, **values)

    def validate_basis_choice(
        self, run_id: str, checkpoint_token: str, choice: str, *, library_bytes: bytes | None = None
    ) -> None:
        """Validate a source selection before the surrounding form is consumed."""
        run_id, checkpoint_token = self.reference_service_identity(run_id, checkpoint_token)
        self._service.validate_basis_source_choice(
            run_id, checkpoint_token, choice, imported_library=library_bytes
        )

    def register_reference_roi_export(
        self,
        locator: str,
        *,
        layout: ReferenceProjectLayout,
        artifact_path: str | Path,
    ) -> None:
        """Register a project-owned ROI publication under an immutable locator."""

        self._resources.register_reference_roi_export(locator, layout, artifact_path)

    def register_tissue_generation(self, locator: str, *, artifact: OutputArtifact) -> None:
        """Register one trusted tissue output-inventory entry."""

        self._resources.register_tissue_generation(locator, artifact)

    @contextmanager
    def _atomic_job(self, run_id: str):
        with self._job_state_lock:
            if run_id in self._cancel_before_next_job:
                self._cancel_before_next_job.remove(run_id)
                raise ReferenceUnmixingCancelledError(
                    "reference unmixing was cancelled before the next atomic job"
                )
            if run_id in self._active_jobs:
                raise RuntimeError("reference unmixing atomic job is already active")
            self._active_jobs.add(run_id)
        try:
            yield
        finally:
            with self._job_state_lock:
                self._active_jobs.discard(run_id)

    def request_cancellation(self, run_id: str) -> ReferenceUnmixingCancellation:
        self._manifest_path(run_id)
        with self._job_state_lock:
            if run_id in self._active_jobs:
                status = ReferenceUnmixingCancellationStatus.DECLINED_ACTIVE_ATOMIC_JOB
            else:
                self._cancel_before_next_job.add(run_id)
                status = ReferenceUnmixingCancellationStatus.REQUESTED_BEFORE_NEXT_JOB
        return ReferenceUnmixingCancellation(run_id, status)

    def _manifest_path(self, run_id: str) -> Path:
        if type(run_id) is not str or not run_id.strip():
            raise ValueError("reference unmixing run ID must be non-empty text")
        return self._manifest_root / (hashlib.sha256(run_id.encode("utf-8")).hexdigest() + ".json")

    def _completion_path(self, run_id: str) -> Path:
        return self._manifest_path(run_id).with_suffix(".completion.json")

    def _write_manifest(self, manifest: _OperationManifest) -> None:
        path = self._manifest_path(manifest.run_id)
        payload = _canonical_json(manifest.to_mapping()) + b"\n"
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            existing = self._load_manifest(manifest.run_id)
            if existing != manifest:
                raise ValueError("reference unmixing run has conflicting configuration")
            return
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                path.unlink()
            except OSError:
                pass
            raise

    def _load_manifest(self, run_id: str) -> _OperationManifest:
        path = self._manifest_path(run_id)
        raw = path.read_bytes()

        def exact_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate operation manifest field: {key}")
                result[key] = value
            return result

        try:
            value = json.loads(
                raw,
                object_pairs_hook=exact_object,
                parse_constant=lambda item: (_ for _ in ()).throw(
                    ValueError(f"nonfinite operation manifest value: {item}")
                ),
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("operation manifest is not strict JSON") from exc
        manifest = _OperationManifest.from_mapping(value)
        if manifest.run_id != run_id:
            raise ValueError("operation manifest crosses run identity")
        if _canonical_json(manifest.to_mapping()) + b"\n" != raw:
            raise ValueError("operation manifest bytes are not canonical")
        return manifest

    def _write_completion(self, completion: ReferenceUnmixingCompletion) -> None:
        path = self._completion_path(completion.run_id)
        payload = _canonical_json(completion.to_mapping()) + b"\n"
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if self._load_completion(completion.run_id) != completion:
                raise ValueError("reference unmixing completion receipt conflicts")
            return
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                path.unlink()
            except OSError:
                pass
            raise

    def _load_completion(self, run_id: str) -> ReferenceUnmixingCompletion:
        path = self._completion_path(run_id)
        raw = path.read_bytes()

        def exact_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate completion receipt field: {key}")
                result[key] = value
            return result

        value = json.loads(
            raw,
            object_pairs_hook=exact_object,
            parse_constant=lambda item: (_ for _ in ()).throw(
                ValueError(f"nonfinite completion receipt value: {item}")
            ),
        )
        completion = ReferenceUnmixingCompletion.from_mapping(value)
        if completion.run_id != run_id:
            raise ValueError("completion receipt crosses run identity")
        if _canonical_json(completion.to_mapping()) + b"\n" != raw:
            raise ValueError("completion receipt bytes are not canonical")
        return completion

    def _existing_completion(
        self,
        manifest: _OperationManifest,
        checkpoint,
    ) -> ReferenceUnmixingCompletion | None:
        try:
            completion = self._load_completion(manifest.run_id)
        except FileNotFoundError:
            return None
        if (
            completion.config_sha256 != manifest.config_sha256
            or completion.target_image_sha256 != manifest.target_image_sha256
            or completion.checkpoint_sha256 != checkpoint.content_sha256
            or checkpoint.status is not ReferenceWorkflowStatus.COMPLETED
        ):
            raise ValueError("completion receipt differs from persisted operation authority")
        return completion

    def _pending_reference_path(self, run_id: str) -> Path:
        return self._manifest_path(run_id).with_suffix(".pending-reference.json")

    def _pending_reference(self, manifest, checkpoint_token, checkpoint):
        """Read the saved form inventory without opening a reader or parsing source OME."""
        path = self._pending_reference_path(manifest.run_id)
        if not path.exists() or checkpoint != "reference_control_files":
            return None
        pending = json.loads(path.read_text())
        binding = {
            "run_id": manifest.run_id, "configuration_id": manifest.config_sha256,
            "target_id": manifest.target_image_sha256,
            "start_authority_id": manifest.start_authority_sha256,
            "checkpoint_token": checkpoint_token,
        }
        if any(pending.get(key) != value for key, value in binding.items()):
            return None
        return pending

    def _inspect_reference_input(self, request, current, configuration):
        from mif_pipeline.io import get_default_backend
        from mif_pipeline.io.metadata.calibration import calibration_inventory, raw_intensity_error
        from mif_pipeline.io.metadata.canonical import resolve_unmixing_input_metadata
        from mif_pipeline.preprocessing.inspection.workflow import inspect_opened_wsi

        _, target_ome = request.reviewed_source_binding()
        target_acquisition = resolve_unmixing_input_metadata(target_ome, 0)
        if target_acquisition is None:
            raise ValueError(
                "Complete the sample’s imaging settings in Spectral unmixing before adding reference images."
            )
        mode = target_acquisition.correction_mode
        backend = self._backend or get_default_backend()
        with backend.open(Path(configuration["locator"])) as source:
            workflow, authority = inspect_opened_wsi(configuration["locator"], source)
        inventory = calibration_inventory(workflow.metadata)
        if inventory["spectral_state"] == "unmixed":
            raise ValueError("Reference inputs must contain acquired spectral bands, not unmixed components")
        error = raw_intensity_error(inventory["intensity_representation"])
        if error:
            raise ValueError(error)
        normalized = inventory["intensity_representation"] == "normalized_counts"
        if normalized != (mode == "as_stored"):
            raise ValueError("Reference and target must use the same stored-intensity correction convention")
        required = [row for row in inventory["missing"]
                    if mode == "full" or (mode == "exposure_only" and row["field"] == "exposure_seconds")]
        source_acquisition = resolve_unmixing_input_metadata(workflow.metadata, 0)
        optical_fields = ("instrument_compatibility_id", "optical_path_compatibility_id",
                          "detector_mode_compatibility_id")
        optical_comparison_available = source_acquisition is not None and all(
            getattr(source_acquisition, field) is not None and getattr(target_acquisition, field) is not None
            for field in optical_fields
        )
        if not required and inventory["spectral_state"] == "raw" and optical_comparison_available:
            self._service.register_reviewed_input(
                request.run_id, current.checkpoint_token, authority, workflow.metadata,
                input_id=configuration["input_id"], input_role=configuration["input_role"],
                fluorophore_identity=configuration.get("fluorophore_id"),
            )
            self._pending_reference_path(request.run_id).unlink(missing_ok=True)
            return self.status(request.run_id)
        condition = (
            "Confirm matching illumination, filters and detector response between this reference and the target. "
            "Full correction accounts for the recorded exposure, gain, binning and detector offset."
            if mode == "full" else
            "Confirm identical gain, binning, illumination, filters and detector configuration, including detector offset, "
            "between this reference and the target. Exposure times are corrected separately."
            if mode == "exposure_only" else
            "Confirm identical imaging settings, per-band exposure times and recorded intensity correction procedure "
            "between this reference and the target."
        )
        pending = {
            "run_id": request.run_id, "configuration_id": current.configuration_id,
            "target_id": current.target_id, "start_authority_id": current.start_authority_id,
            "checkpoint_token": current.checkpoint_token, "pending_id": uuid4().hex,
            "input_id": configuration["input_id"], "input_role": configuration["input_role"],
            "fluorophore_id": configuration.get("fluorophore_id"),
            "locator": str(authority.source_identity.resolved_path),
            "source_authority": authority.to_mapping(), "calibration_inventory": inventory,
            "correction_mode": mode, "acquisition_condition": condition,
        }
        path = self._pending_reference_path(request.run_id)
        temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(_canonical_json(pending) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return self.status(request.run_id)

    def _complete_reference_input(self, request, current, configuration):
        from mif_pipeline.io.canonical_ingestion import selected_canonical_ome
        from mif_pipeline.io.metadata.calibration import (
            calibration_inventory, complete_calibration, calibration_review, record_acquisition_confirmation,
        )
        from mif_pipeline.io.metadata.spectral import confirmed_raw_band_metadata

        manifest = self._load_manifest(request.run_id)
        pending = self._pending_reference(manifest, current.checkpoint_token, current.checkpoint)
        if pending is None or any(configuration.get(key) != pending[key]
                                  for key in ("pending_id", "input_role", "fluorophore_id")):
            raise ValueError("The uploaded reference review is stale or its role changed")
        fields = {"pending_id", "input_role", "fluorophore_id", "calibration", "reviewer_id", "acquisition_confirmation"}
        unknown = pending["calibration_inventory"]["spectral_state"] == "unknown"
        if unknown:
            fields.add("spectral_state")
        if set(configuration) != fields or configuration["acquisition_confirmation"] != "confirmed":
            raise ValueError("Complete the reference imaging-settings review and matching-conditions confirmation")
        if unknown and configuration["spectral_state"] != "raw":
            raise ValueError("Confirm that the reference contains acquired spectral bands")
        reviewer = _nonempty_text(configuration["reviewer_id"], "imaging-settings reviewer")
        authority = InspectedSourceAuthority.from_json_bytes(_canonical_json(pending["source_authority"]))
        ome = selected_canonical_ome(authority.canonical_ome, authority.selection)
        inventory = calibration_inventory(ome)
        if inventory != pending["calibration_inventory"]:
            raise ValueError("The retained reference imaging-settings inventory changed")
        calibration = configuration["calibration"]
        if not isinstance(calibration, dict) or set(calibration) != {"values", "source_note"}:
            raise ValueError("Complete only the declared missing imaging settings")
        source_note = _nonempty_text(calibration["source_note"], "imaging-settings source note")
        ome = confirmed_raw_band_metadata(ome)
        if inventory["missing"] or unknown:
            ome = complete_calibration(
                ome, calibration["values"], source_note=source_note + " " + pending["acquisition_condition"],
                reviewer=reviewer, spectral_state="raw", correction_mode=pending["correction_mode"],
            )
        elif calibration["values"]:
            raise ValueError("Detected imaging settings cannot be changed")
        else:
            earlier = calibration_review(ome)
            note = ((earlier["source_note"] + " ") if earlier is not None else "")
            note += source_note + " Imaging settings confirmed by " + reviewer + ". " + pending["acquisition_condition"]
            ome = record_acquisition_confirmation(ome, source_note=note, reviewer=reviewer)
        self._service.register_reviewed_input(
            request.run_id, current.checkpoint_token, authority, ome,
            input_id=pending["input_id"], input_role=pending["input_role"],
            fluorophore_identity=pending["fluorophore_id"],
        )
        self._pending_reference_path(request.run_id).unlink()
        return self.status(request.run_id)

    def _state(
        self,
        manifest: _OperationManifest,
        snapshot: ReferenceServiceSnapshot,
    ) -> ReferenceUnmixingState:
        return self._state_from_view(manifest, self._service.bounded_view(snapshot))

    def _state_from_view(
        self,
        manifest: _OperationManifest,
        view: ReferenceServiceView,
    ) -> ReferenceUnmixingState:
        if view.status == ReferenceWorkflowStatus.COMPLETED.value:
            status = ReferenceUnmixingStatus.READY_FOR_UNMIXING
        elif view.status == ReferenceWorkflowStatus.ABORTED.value:
            status = ReferenceUnmixingStatus.ABORTED
        else:
            status = ReferenceUnmixingStatus.AWAITING_OPERATION
        frozen_review = _freeze_json_value(
            _thaw_json_value(view.review_summary), label="review summary"
        )
        assert type(frozen_review) is tuple
        proposed_artifacts_present = bool(view.proposed_artifact_requirements)
        operation_requirement_items: list[ReferenceUnmixingOperationRequirement] = []
        if view.checkpoint is not None:
            for item in view.offered_options:
                operation_action = _operation_action_for_option(
                    checkpoint=view.checkpoint,
                    option_value=item.option_value,
                    proposed_artifacts_present=proposed_artifacts_present,
                )
                artifact_requirements = tuple(
                    ReferenceUnmixingArtifactRequirement(
                        requirement.role,
                        requirement.subject_id,
                    )
                    for requirement in item.artifact_requirements
                )
                operation_requirement_items.append(
                    ReferenceUnmixingOperationRequirement(
                        option_value=item.option_value,
                        label=item.label,
                        description=item.description,
                        operation_action=operation_action,
                        minimum_selected_ids=item.minimum_selected_ids,
                        minimum_artifacts=item.minimum_artifacts,
                        artifact_requirements=artifact_requirements,
                        form_fields=_visible_form_fields(
                            checkpoint=view.checkpoint,
                            option_value=item.option_value,
                            operation_action=operation_action,
                            measurement_ids=view.measurement_ids,
                            offered_ids=view.offered_ids,
                            minimum_selected_ids=item.minimum_selected_ids,
                            artifact_requirements=artifact_requirements,
                            component_names=tuple(
                                spectrum["component"]
                                for spectrum in _thaw_json_value(view.review_summary).get(
                                    "spectral_review", {}
                                ).get("spectra", ())
                                if spectrum["kind"] == "fluorophore"
                            ) or tuple(_thaw_json_value(view.review_summary).get(
                                "spectral_input", {}
                            ).get("component_names", ())),
                            correction_mode=_thaw_json_value(view.review_summary).get(
                                "spectral_input", {}
                            ).get("correction_mode"),
                            af_control_set_review_sha256s=tuple(
                                control_set["evidence_sha256"] for control_set in
                                _thaw_json_value(view.review_summary).get("af_control_set_review", {}).get("sets", ())
                            ) if not _thaw_json_value(view.review_summary).get('shared_control_preparation', {}).get('accepted') and all(control_set.get('status', 'pass') == 'pass' and control_set.get('evidence_sha256') for control_set in
                                _thaw_json_value(view.review_summary).get('af_control_set_review', {}).get('sets', ())) else (),
                        ),
                    )
                )
            if view.checkpoint == "reference_control_files":
                registration_requirements = tuple(
                    dict.fromkeys(
                        (requirement.role, requirement.subject_id)
                        for option in view.offered_options
                        for requirement in option.artifact_requirements
                        if requirement.role
                        in {
                            "positive_reference_image",
                            "negative_reference_image",
                            "unstained_reference_image",
                        }
                    )
                )
                for index, (role, subject_id) in enumerate(registration_requirements):
                    artifact_requirement = ReferenceUnmixingArtifactRequirement(role, subject_id)
                    image_label = {
                        "positive_reference_image": f"Single-stain image: {subject_id}",
                        "negative_reference_image": f"Negative control: {subject_id}",
                        "unstained_reference_image": "Unstained control image",
                    }[role]
                    image_help = (
                        f"Select the image stained with {subject_id}."
                        if role == "positive_reference_image" else
                        f"Select the negative-control image for {subject_id}."
                        if role == "negative_reference_image" else
                        "Select the unstained image acquired with matching settings."
                    )
                    operation_requirement_items.append(
                        ReferenceUnmixingOperationRequirement(
                            option_value=f"register_canonical_input:{index}",
                            label=image_label,
                            description=image_help,
                            operation_action="register_canonical_input",
                            minimum_selected_ids=0,
                            minimum_artifacts=0,
                            artifact_requirements=(artifact_requirement,),
                            form_fields=(
                                _form_field(
                                    "locator",
                                    image_label,
                                    image_help,
                                    ReferenceUnmixingFormValueKind.LOCATOR,
                                ),
                                _form_field(
                                    "input_id",
                                    "Input ID",
                                    "Enter the stable pseudonymous input identifier.",
                                    ReferenceUnmixingFormValueKind.TEXT,
                                ),
                            ),
                        )
                    )
        pending = self._pending_reference(manifest, view.checkpoint_token, view.checkpoint)
        if pending is not None:
            summary = _thaw_json_value(frozen_review)
            summary["pending_reference_input"] = {
                key: pending[key] for key in (
                    "pending_id", "input_id", "input_role", "fluorophore_id", "locator",
                    "calibration_inventory", "correction_mode", "acquisition_condition",
                )
            }
            frozen_review = _freeze_json_value(summary, label="review summary")
            fields = [
                _form_field("calibration", "Missing imaging settings",
                            "Complete only missing values under the target correction policy.",
                            ReferenceUnmixingFormValueKind.PRIMITIVE_MAPPING),
                _form_field("acquisition_confirmation", "Matching imaging settings",
                            pending["acquisition_condition"], ReferenceUnmixingFormValueKind.ENUM,
                            value_choices=("confirmed",)),
                _form_field("reviewer_id", "Reviewer", "Reviewer of reference imaging settings.",
                            ReferenceUnmixingFormValueKind.TEXT),
            ]
            if pending["calibration_inventory"]["spectral_state"] == "unknown":
                fields.append(_form_field(
                    "spectral_state", "Confirm the source contains acquired spectral bands",
                    "Use the original raw spectral image, before component unmixing.",
                    ReferenceUnmixingFormValueKind.ENUM, value_choices=("raw",)))
            operation_requirement_items.insert(0, ReferenceUnmixingOperationRequirement(
                option_value="complete_reference_input:" + pending["pending_id"],
                label="Review reference imaging settings", description=pending["acquisition_condition"],
                operation_action="complete_reference_input", minimum_selected_ids=0,
                minimum_artifacts=0, artifact_requirements=(), form_fields=tuple(fields),
            ))
        from .reference_region_selection import REGION_CHECKPOINTS, region_requirement
        from .reference_source_preparation import preparation_requirements, component_revision_requirement
        operation_requirement_items, source_route = preparation_requirements(view, operation_requirement_items)
        if view.checkpoint in REGION_CHECKPOINTS and not source_route:
            operation_requirement_items.insert(0, region_requirement())
        revision = component_revision_requirement(view)
        if revision is not None:
            operation_requirement_items.append(revision)
        operation_requirements = tuple(operation_requirement_items)
        return ReferenceUnmixingState(
            run_id=view.run_id,
            status=status,
            configuration_id=manifest.config_sha256,
            target_id=view.target_id,
            start_authority_id=manifest.start_authority_sha256,
            checkpoint_token=view.checkpoint_token,
            checkpoint_revision=view.revision,
            checkpoint=view.checkpoint,
            fluorophore=view.fluorophore,
            measurement_ids=view.measurement_ids,
            offered_actions=tuple(item.option_value for item in operation_requirements),
            operation_requirements=operation_requirements,
            offered_ids=view.offered_ids,
            review_summary=frozen_review,
        )

    @staticmethod
    def _require_request(
        manifest: _OperationManifest,
        request: ReferenceUnmixingRequest,
    ) -> None:
        if type(request) is not ReferenceUnmixingRequest:
            raise TypeError("reference unmixing resume requires exact request")
        if manifest.run_id != request.run_id or manifest.config_sha256 != request.config_sha256:
            raise ValueError("reference unmixing configuration identity changed")


    def start_from_reviewed_target(
        self,
        request: ReferenceUnmixingRequest,
    ) -> ReferenceUnmixingState:
        """Start from the exact inspected source and accepted OME in the v2 request."""
        if type(request) is not ReferenceUnmixingRequest:
            raise TypeError("reference unmixing start requires exact request")
        authority, reviewed_ome = request.reviewed_source_binding()
        return self._start_with_request(
            request,
            lambda: self._service.start_from_reviewed_target(
                request.run_id, authority, reviewed_ome, af_behavior=request.af_behavior
            ),
        )

    def _start_with_request(
        self,
        request: ReferenceUnmixingRequest,
        start,
    ) -> ReferenceUnmixingState:
        try:
            existing = self._load_manifest(request.run_id)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            self._require_request(existing, request)
        snapshot = start()
        manifest = _OperationManifest.create(
            request,
            target_image_sha256=snapshot.checkpoint.target_image_sha256,
            start_authority_sha256=snapshot.start_authority_sha256,
        )
        if existing is not None and existing != manifest:
            raise ValueError("reference unmixing target authority changed")
        self._write_manifest(manifest)
        return self._state(manifest, snapshot)

    def _resume_revalidated(
        self,
        request: ReferenceUnmixingRequest,
        *,
        publish_completed: bool = True,
    ) -> ReferenceUnmixingState | ReferenceUnmixingCompletion:
        manifest = self._load_manifest(request.run_id)
        self._require_request(manifest, request)
        snapshot = self._service.resume(request.run_id)
        if (
            snapshot.start_authority_sha256 != manifest.start_authority_sha256
            or snapshot.checkpoint.target_image_sha256 != manifest.target_image_sha256
        ):
            raise ValueError("reference unmixing target authority changed")
        snapshot = self._resume_accepted_reference_publication(snapshot)
        shared = self._advance_shared_session(request)
        if shared is not None:
            return shared
        snapshot = self._service.resume(request.run_id)
        completed = self._existing_completion(manifest, snapshot.checkpoint)
        if completed is not None:
            application = self._service.load_final_basis_application(request.run_id)
            if (
                application.run_id != request.run_id
                or application.target_image_sha256 != manifest.target_image_sha256
                or application.authority_id != completed.final_authority_id
                or application.authority_sha256 != completed.final_authority_sha256
            ):
                raise ValueError("completed final basis application authority changed")
            self._revalidate_completion_artifacts(request, completed)
            return completed
        if publish_completed and snapshot.checkpoint.status is ReferenceWorkflowStatus.COMPLETED:
            return self._run_final_unmixing(request, manifest, snapshot)
        return self._state(manifest, snapshot)

    def _resume_accepted_reference_publication(self, snapshot):
        """Finish an interrupted publication only after its review was accepted."""
        current = snapshot.checkpoint
        pending = current.pending_publication
        if (current.status is not ReferenceWorkflowStatus.AWAITING_PUBLICATION or pending is None
                or pending.publication_kind not in {"target_af", "final_basis"}):
            return snapshot
        decision = next(item for item in current.decisions
                        if item.request.request_id == pending.review_request_id)
        if pending.publication_kind == "target_af":
            result = self._service.accept_control_target_af_decision(
                current.run_id, decision.source_checkpoint_sha256,
                reviewer_id=decision.response.reviewer_id)
        else:
            result = self._service.review_final_run_basis_decision(
                current.run_id, decision.source_checkpoint_sha256, decision="accepted",
                reviewer_id=decision.response.reviewer_id)
        return self._advance_snapshot(result)

    @staticmethod
    def _revalidate_completion_artifacts(
        request: ReferenceUnmixingRequest,
        completion: ReferenceUnmixingCompletion,
    ) -> None:
        from mif_pipeline.preprocessing.unmixing.output_publication import authority_pointer_path

        if completion.authority_pointer_path != str(authority_pointer_path(request.output_path)):
            raise ValueError("completed publication pointer differs from configured output")
        checks = (
            (completion.output_path, completion.output_sha256, "unmixed output"),
            (
                completion.provenance_path,
                completion.provenance_sha256,
                "unmixing provenance",
            ),
            (
                completion.authority_pointer_path,
                completion.authority_pointer_sha256,
                "unmixing publication pointer",
            ),
            (
                completion.clipping_evidence_path,
                completion.clipping_evidence_sha256,
                "unmixing clipping evidence",
            ),
        )
        for path, expected, label in checks:
            if (path is None) != (expected is None):
                raise ValueError(f"completed {label} binding is incomplete")
            if path is not None and _file_sha256(path, label) != expected:
                raise ValueError(f"completed {label} bytes changed")

    def resume(
        self, request: ReferenceUnmixingRequest
    ) -> ReferenceUnmixingState | ReferenceUnmixingCompletion:
        with self._atomic_job(request.run_id):
            return self._resume_revalidated(request)

    def status(self, run_id: str) -> ReferenceUnmixingState | ReferenceUnmixingCompletion:
        """Read persisted records only; canonical sources are intentionally unopened."""

        manifest = self._load_manifest(run_id)
        child = self._shared_coordinator().persisted_child(run_id)
        if child is not None:
            return self._project_shared_state(manifest, child)
        view = self._service.load_persisted_view(run_id)
        try:
            completed = self._load_completion(run_id)
        except FileNotFoundError:
            completed = None
        if completed is not None:
            if (
                completed.config_sha256 != manifest.config_sha256
                or completed.target_image_sha256 != view.target_id
                or completed.checkpoint_sha256 != view.checkpoint_token
                or view.status != ReferenceWorkflowStatus.COMPLETED.value
            ):
                raise ValueError("completion receipt differs from persisted operation authority")
            return completed
        return self._state_from_view(manifest, view)

    def _run_final_unmixing(
        self,
        request: ReferenceUnmixingRequest,
        manifest: _OperationManifest,
        snapshot: ReferenceServiceSnapshot,
    ) -> ReferenceUnmixingCompletion:
        checkpoint = snapshot.checkpoint
        if checkpoint.status is not ReferenceWorkflowStatus.COMPLETED:
            raise ValueError("reference workflow is not completed")
        application = self._service.load_final_basis_application(manifest.run_id)
        if type(application) is not ResolvedBasisApplication or (
            application.run_id != manifest.run_id
            or application.target_image_sha256 != manifest.target_image_sha256
            or application.measurement_ids != checkpoint.measurement_ids
        ):
            raise ValueError(
                "final basis application differs from run, target, or measurement authority"
            )
        authority, reviewed_ome = request.reviewed_source_binding()
        result = self._unmixing_runner(
            request.input_path,
            request.output_path,
            None,
            request.to_unmixing_config(),
            backend=self._backend,
            replace_channel_names_with_biomarkers=(request.replace_channel_names_with_biomarkers),
            basis_application=application,
            reviewed_source_authority=authority,
            canonical_metadata_override=reviewed_ome,
        )
        if type(result) is not UnmixingResult:
            raise TypeError("unmixing runtime returned the wrong result type")
        completion = ReferenceUnmixingCompletion.create(
            request=request,
            checkpoint_sha256=checkpoint.content_sha256,
            application=application,
            result=result,
        )
        self._write_completion(completion)
        return completion

    @staticmethod
    def _advance_snapshot(advance: ReferenceServiceAdvance) -> ReferenceServiceSnapshot:
        if type(advance) is not ReferenceServiceAdvance:
            raise TypeError("scientific service operation returned the wrong result type")
        if advance.status is ReferenceServiceAdvanceStatus.NEEDS_SCIENTIFIC_RESULT:
            raise ReferenceScientificOperationRequiredError(
                advance.reason or "exact typed scientific operation required"
            )
        return advance.snapshot

    def advance(
        self,
        request: ReferenceUnmixingRequest,
        operation: ReferenceUnmixingConfiguredAction,
    ) -> ReferenceUnmixingState | ReferenceUnmixingCompletion:
        """Apply one exact response or typed scientific service operation."""

        if type(operation) is not ReferenceUnmixingConfiguredAction:
            raise TypeError("reference unmixing accepts only exact configured primitive actions")
        with self._atomic_job(request.run_id):
            from .reference_source_preparation import ACTIONS, advance_source_preparation
            if operation.action in ACTIONS:
                return advance_source_preparation(self, request, operation)
            if operation.action == 'select_reference_regions':
                from .reference_region_selection import advance_reference_regions
                return advance_reference_regions(self, request, operation)
            return self._advance_atomic(request, operation)

    def _advance_atomic(
        self,
        request: ReferenceUnmixingRequest,
        operation: ReferenceUnmixingConfiguredAction,
        *,
        publish_completed: bool = True,
    ) -> ReferenceUnmixingState | ReferenceUnmixingCompletion:
        child = self._shared_coordinator().persisted_child(request.run_id)
        if child is not None:
            manifest = self._load_manifest(request.run_id)
            self._require_request(manifest, request)
            _, token = self.reference_service_identity(request.run_id, operation.checkpoint_token)
            self._advance_atomic(self._shared_request(request, child),
                replace(operation, checkpoint_token=token), publish_completed=False)
            shared = self._advance_shared_session(request)
            return shared if shared is not None else self._resume_revalidated(request, publish_completed=False)
        if operation.action in {"choose_basis_source", "review_final_basis"}:
            # Library selection and acceptance use saved metadata. The service
            # checks file freshness before publication; the numerical reader
            # validates complete source bytes before it reads any pixels.
            manifest = self._load_manifest(request.run_id)
            self._require_request(manifest, request)
            checkpoint, start = self._store._load_service_checkpoint(request.run_id)
            if (start != manifest.start_authority_sha256
                    or checkpoint.target_image_sha256 != manifest.target_image_sha256):
                raise ValueError("reference unmixing target authority changed")
            current_token = checkpoint.content_sha256
            current_kind = (None if checkpoint.current_checkpoint is None
                            else checkpoint.current_checkpoint.value)
        else:
            current = self._resume_revalidated(request, publish_completed=False)
            if type(current) is ReferenceUnmixingCompletion:
                raise ValueError("completed reference unmixing run has unused operation")
            current_token = current.checkpoint_token
            current_kind = current.checkpoint
        expected = (
            operation.checkpoint_token
            if type(operation) is ReferenceUnmixingConfiguredAction
            else getattr(operation, "expected_checkpoint_sha256", None)
        )
        if expected != current_token:
            raise ValueError("configured operation checkpoint expectation is stale or reordered")
        if type(operation) is ReferenceUnmixingConfiguredAction:
            allowed_actions = _SAFE_ACTIONS_BY_CHECKPOINT.get(current_kind)
            if allowed_actions is None or operation.action not in allowed_actions:
                raise ValueError("configured action is invalid for the live workflow checkpoint")
            configuration = operation.configuration_mapping()
            if operation.action == "decide":
                if set(configuration) != {"option", "reviewer_id", "selected_ids"}:
                    raise ValueError("decision configuration fields differ")
                selected = configuration["selected_ids"]
                if type(selected) is not list or any(
                    type(item) is not list
                    or len(item) != 2
                    or any(type(part) is not str for part in item)
                    for item in selected
                ):
                    raise ValueError("decision selected IDs must be [kind, ID] pairs")
                snapshot = self._advance_snapshot(
                    self._service.respond_with_offered_ids(
                        request.run_id,
                        operation.checkpoint_token,
                        option=_nonempty_text(configuration["option"], "decision option"),
                        reviewer_id=_nonempty_text(
                            configuration["reviewer_id"], "decision reviewer ID"
                        ),
                        selected_ids=tuple(tuple(item) for item in selected),
                    )
                )
            elif operation.action == "choose_curated_basis":
                required = {
                    "reviewer_id",
                    "library_locator",
                    "spectral_csv",
                    "normalization",
                    "measurement_convention",
                }
                if set(configuration) != required:
                    raise ValueError("Complete the spectral library and its correction settings")
                snapshot = self._advance_snapshot(
                    self._service.accept_supplied_file(
                        request.run_id, operation.checkpoint_token, **configuration
                    )
                )
            elif operation.action == "choose_basis_source":
                allowed = {"choice", "reviewer_id", "library_locator"}
                if set(configuration) not in (
                    {"choice", "reviewer_id"},
                    {"choice", "reviewer_id", "component_names"},
                    allowed,
                ):
                    raise ValueError("basis-source configuration fields differ")
                try:
                    choice = BasisSourceChoice(configuration["choice"])
                except (TypeError, ValueError) as exc:
                    raise ValueError("basis-source choice is unsupported") from exc
                reviewer_id = _nonempty_text(
                    configuration["reviewer_id"], "basis-source reviewer ID"
                )
                locator = configuration.get("library_locator")
                if choice is BasisSourceChoice.IMPORTED_REFERENCE_LIBRARY:
                    if type(locator) is not str or not Path(locator).is_absolute():
                        raise ValueError("imported library locator must be absolute")
                    from mif_pipeline.preprocessing.unmixing.library_exchange import (
                        read_library_payload,
                    )

                    imported_library = read_library_payload(locator)
                elif locator is not None:
                    raise ValueError("non-imported source cannot carry a library locator")
                else:
                    imported_library = None
                snapshot = self._advance_snapshot(
                    self._service.accept_basis_source_choice(
                        request.run_id,
                        operation.checkpoint_token,
                        choice,
                        reviewer_id=reviewer_id,
                        imported_library=imported_library,
                        component_names=_configured_text_array(
                            configuration.get("component_names", []), "fluorophore components"
                        ),
                    )
                )
            elif operation.action == "register_canonical_input":
                allowed = {"locator", "input_id", "input_role", "fluorophore_id"}
                if set(configuration) not in (
                    {"locator", "input_id", "input_role"},
                    allowed,
                ):
                    raise ValueError("canonical-input configuration fields differ")
                locator = _nonempty_text(configuration["locator"], "canonical-input locator")
                if not Path(locator).is_absolute():
                    raise ValueError("canonical-input locator must be absolute")
                fluorophore = configuration.get("fluorophore_id")
                if fluorophore is not None:
                    fluorophore = _nonempty_text(fluorophore, "canonical-input fluorophore ID")
                # Role and dye are selected by the live component-specific picker.
                semantics = {"input_role": configuration["input_role"]}
                if fluorophore is not None:
                    semantics["fluorophore_id"] = fluorophore
                if not any(_canonical_registration_semantics(item) == semantics
                           for item in current.operation_requirements
                           if item.operation_action == "register_canonical_input"):
                    raise ValueError("The selected reference role or fluorophore is not offered")
                return self._inspect_reference_input(request, current, configuration)
            elif operation.action == "complete_reference_input":
                return self._complete_reference_input(request, current, configuration)
            elif operation.action == "bind_reference_controls":
                if set(configuration) - {"prepare_shared_control"} != {
                    "positive_input_ids",
                    "shared_unstained_input_id",
                    "negative_input_ids",
                    "reviewer_id",
                }:
                    raise ValueError("reference-control configuration fields differ")
                shared = configuration["shared_unstained_input_id"]
                if shared is not None:
                    shared = _nonempty_text(shared, "shared unstained input ID")
                snapshot = self._advance_snapshot(
                    self._service.accept_reference_control_files(
                        request.run_id,
                        operation.checkpoint_token,
                        _configured_text_array(
                            configuration["positive_input_ids"],
                            "positive input IDs",
                            allow_empty=False,
                        ),
                        reviewer_id=_nonempty_text(
                            configuration["reviewer_id"],
                            "reference-control reviewer ID",
                        ),
                        shared_unstained_input_id=shared,
                        negative_control_input_ids=_configured_text_array(
                            configuration["negative_input_ids"],
                            "negative input IDs",
                        ),
                        prepare_shared_control=configuration.get("prepare_shared_control", False),
                    )
                )
            elif operation.action == "advance_reference_purification":
                checkpoint = current.checkpoint
                reviewer_id = _nonempty_text(
                    configuration.get("reviewer_id"), "purification reviewer ID"
                )
                common = {"reviewer_id"}
                roi_export = None
                selections = ()
                pairings = ()
                sampling_policy = None
                background_policy = None
                signal_dark_selections = ()
                af_control_set_review_sha256s = ()
                purification_policy = None
                extraction_dispositions = ()
                if checkpoint in {
                    "positive_reference_rois",
                    "af_reference_rois",
                    "negative_reference_rois",
                    "background_reference_rois",
                }:
                    base = common | {"roi_export_locator", "selected_roi_ids", "sampling_policy"}
                    allowed = base | {"pairings", "background_policy"}
                    if not base.issubset(configuration) or not set(configuration).issubset(allowed):
                        raise ValueError("ROI-purification configuration fields differ")
                    selected_ids = _configured_text_array(
                        configuration["selected_roi_ids"],
                        "selected ROI IDs",
                        allow_empty=False,
                    )
                    locator = _nonempty_text(
                        configuration["roi_export_locator"], "ROI export locator"
                    )
                    roi_export = self._resources.resolve_reference_roi_export(locator)
                    selections = self._resources.resolve_reference_roi_selections(
                        locator, selected_ids
                    )
                    sampling_policy = _configured_mapping(
                        configuration["sampling_policy"],
                        "complete-vector policy",
                    )
                    pairing_values = configuration.get("pairings", [])
                    if type(pairing_values) is not list or any(
                        type(item) is not list
                        or len(item) != 2
                        or any(type(part) is not str for part in item)
                        for item in pairing_values
                    ):
                        raise ValueError("ROI pairings must be [positive, control] pairs")
                    pairings = tuple(ReferenceRoiPairingSelection(*item) for item in pairing_values)
                    if "background_policy" in configuration:
                        background_policy = _configured_mapping(
                            configuration["background_policy"],
                            "background policy",
                        )
                elif checkpoint == "signal_dark_measurements":
                    required = common | {
                        "signal_measurement_id",
                        "dark_measurement_ids",
                        "purification_policy",
                    }
                    if not required.issubset(configuration) or set(configuration) - required - {"af_control_set_review_sha256s"}:
                        raise ValueError("signal/dark configuration fields differ")
                    af_control_set_review_sha256s = _configured_text_array(
                        configuration.get("af_control_set_review_sha256s", []),
                        "reviewed AF control spectra", allow_empty=True)
                    signal_dark_selections = (
                        SelectedIdentifier(
                            "signal_measurement",
                            _nonempty_text(
                                configuration["signal_measurement_id"],
                                "signal measurement ID",
                            ),
                        ),
                        *(
                            SelectedIdentifier("dark_measurement", item)
                            for item in _configured_text_array(
                                configuration["dark_measurement_ids"],
                                "dark measurement IDs",
                                allow_empty=False,
                            )
                        ),
                    )
                    purification_policy = _configured_mapping(
                        configuration["purification_policy"],
                        "purification policy",
                    )
                elif checkpoint == "extraction_qc":
                    if set(configuration) != common | {"included_roi_ids", "excluded_roi_ids"}:
                        raise ValueError("extraction-QC configuration fields differ")
                    included = _configured_text_array(
                        configuration["included_roi_ids"],
                        "included ROI IDs",
                        allow_empty=False,
                    )
                    excluded = _configured_text_array(
                        configuration["excluded_roi_ids"], "excluded ROI IDs"
                    )
                    if set(included) & set(excluded):
                        raise ValueError("extraction ROI dispositions overlap")
                    included_set = set(included)
                    excluded_set = set(excluded)
                    if included_set | excluded_set != set(current.offered_ids):
                        raise ValueError(
                            "extraction ROI dispositions differ from the live inventory"
                        )
                    extraction_dispositions = tuple(
                        SelectedIdentifier(
                            (
                                "consensus_included_roi"
                                if item in included_set
                                else "consensus_excluded_roi"
                            ),
                            item,
                        )
                        for item in current.offered_ids
                    )
                else:
                    raise ValueError("purification action is invalid for the live checkpoint")
                snapshot = self._advance_snapshot(
                    self._service.advance_reference_purification_from_configuration(
                        request.run_id,
                        operation.checkpoint_token,
                        reviewer_id=reviewer_id,
                        roi_export=roi_export,
                        selections=selections,
                        pairings=pairings,
                        sampling_policy=sampling_policy,
                        background_policy=background_policy,
                        signal_dark_selections=signal_dark_selections,
                        purification_policy=purification_policy,
                        extraction_dispositions=extraction_dispositions,
                        af_control_set_review_sha256s=af_control_set_review_sha256s,
                    )
                )
            elif operation.action == "accept_live_consensus":
                if set(configuration) != {"reviewer_id", "consensus_policy", "qc_policy", "consensus_roi_review"}:
                    raise ValueError("live-consensus configuration fields differ")
                snapshot = self._advance_snapshot(
                    self._service.accept_live_fluorophore_consensus(
                        request.run_id,
                        operation.checkpoint_token,
                        reviewer_id=_nonempty_text(
                            configuration["reviewer_id"], "consensus reviewer ID"
                        ),
                        consensus_policy=configuration["consensus_policy"],
                        roi_review=configuration["consensus_roi_review"],
                        qc_policy=configuration["qc_policy"],
                    )
                )
            elif operation.action in {"accept_control_target_af", "review_control_final_basis"}:
                if set(configuration) != {"reviewer_id"}:
                    raise ValueError("control target-AF review fields differ")
                # Retained pre-change actions authorize only the AF they displayed.
                accept = (self._service.accept_control_final_basis_decision
                          if operation.action == "review_control_final_basis"
                          else self._service.accept_control_target_af_decision)
                snapshot = self._advance_snapshot(
                    accept(
                        request.run_id,
                        operation.checkpoint_token,
                        reviewer_id=_nonempty_text(
                            configuration["reviewer_id"],
                            "control target-AF reviewer ID",
                        ),
                    )
                )
            elif operation.action == "propose_target_af_groups":
                if set(configuration) != {"target_af_groups", "reviewer_id"}:
                    raise ValueError("AF grouping action fields differ")
                snapshot = self._advance_snapshot(self._service.propose_target_af_groups_from_ids(
                    request.run_id, operation.checkpoint_token,
                    target_af_groups=configuration["target_af_groups"],
                    reviewer_id=_nonempty_text(configuration["reviewer_id"], "AF grouping reviewer ID")))
            elif operation.action == "propose_control_target_af":
                if set(configuration) != {
                    "control_input_id",
                    "roi_export_locator",
                    "af_roi_ids",
                    "background_roi_ids",
                    "roi_reviews",
                    "sampling_policy",
                    "background_policy",
                    "estimation_policy",
                    "qc_policy",
                    "control_review",
                    "reviewer_id",
                }:
                    raise ValueError("control target-AF proposal fields differ")
                locator = _nonempty_text(configuration["roi_export_locator"], "ROI export locator")
                export = self._resources.resolve_reference_roi_export(locator)
                control_input_id = configuration["control_input_id"]
                if control_input_id is not None:
                    control_input_id = _nonempty_text(
                        control_input_id, "target-AF control input ID"
                    )
                review_values = configuration["roi_reviews"]
                if type(review_values) is not list or any(
                    type(item) is not dict for item in review_values
                ):
                    raise TypeError("control target-AF ROI reviews must be JSON objects")
                snapshot = self._advance_snapshot(
                    self._service.propose_control_target_af_from_ids(
                        request.run_id,
                        operation.checkpoint_token,
                        control_input_id=control_input_id,
                        roi_export=export,
                        af_roi_ids=_configured_text_array(
                            configuration["af_roi_ids"],
                            "target-AF ROI IDs",
                            allow_empty=False,
                        ),
                        background_roi_ids=_configured_text_array(
                            configuration["background_roi_ids"],
                            "target background ROI IDs",
                            allow_empty=control_input_id is None,
                        ),
                        roi_reviews=tuple(review_values),
                        sampling_policy=configuration["sampling_policy"],
                        background_policy=configuration["background_policy"],
                        estimation_policy=configuration["estimation_policy"],
                        qc_policy=configuration["qc_policy"],
                        control_review=configuration["control_review"],
                        reviewer_id=_nonempty_text(
                            configuration["reviewer_id"],
                            "control target-AF reviewer ID",
                        ),
                    )
                )
            elif operation.action == "propose_final_basis":
                if set(configuration) != {"qc_policy"}:
                    raise ValueError("final-basis proposal configuration fields differ")
                snapshot = self._advance_snapshot(
                    self._service.propose_final_run_basis_from_policy(
                        request.run_id,
                        operation.checkpoint_token,
                        qc_policy=configuration["qc_policy"],
                    )
                )
            elif operation.action == "review_final_basis":
                if set(configuration) != {"decision", "reviewer_id"}:
                    raise ValueError("final-basis review configuration fields differ")
                snapshot = self._advance_snapshot(
                    self._service.review_final_run_basis_decision(
                        request.run_id,
                        operation.checkpoint_token,
                        decision=_nonempty_text(configuration["decision"], "final-basis decision"),
                        reviewer_id=_nonempty_text(
                            configuration["reviewer_id"], "final-basis reviewer ID"
                        ),
                    )
                )
            else:
                raise ValueError("configured action is not valid for the live checkpoint")
        manifest = self._load_manifest(request.run_id)
        shared = self._advance_shared_session(request)
        if shared is not None:
            return shared
        if publish_completed and snapshot.checkpoint.status is ReferenceWorkflowStatus.COMPLETED:
            return self._run_final_unmixing(request, manifest, snapshot)
        return self._state(manifest, snapshot)


__all__ = [
    "ReferenceScientificOperationRequiredError",
    "ReferenceUnmixingArtifactRequirement",
    "ReferenceUnmixingCancellation",
    "ReferenceUnmixingCancellationStatus",
    "ReferenceUnmixingCancelledError",
    "ReferenceUnmixingCompletion",
    "ReferenceUnmixingConfiguredAction",
    "ReferenceUnmixingFormField",
    "ReferenceUnmixingFormValueKind",
    "ReferenceUnmixingOperationRequirement",
    "ReferenceUnmixingOperation",
    "ReferenceUnmixingRequest",
    "ReferenceUnmixingState",
    "ReferenceUnmixingStatus",
    "configure_reference_unmixing_action",
    "validate_reference_unmixing_action_intent",
]
