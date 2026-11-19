"""Headless, immutable view models shared by desktop presentation adapters.

The `FormModel` record carries `form_id`, `title`, `message`, `allowed_actions`, `controls`
and its remaining declared fields.

The interface keeps editable researcher choices associated with the current image or
analysis request. Values are passed to the stage-specific validator before they become
accepted processing settings."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Any

from mif_pipeline.preprocessing.inspection.workflow import (
    Stage1Workflow,
    apply_stage1_biomarker_values,
)
from mif_pipeline.pipeline_core.stage_result import (
    InteractionAction,
    InteractionReason,
    StageResult,
    StageStatus,
)
from mif_pipeline.pipeline_core.interactions import (
    resolve_resume_envelope,
    validate_interaction_response,
)

# ==============================================================================
#                                  Interactions
# ==============================================================================


class ControlKind(str, Enum):
    LOCAL_FILE = "local_file"
    CHOICE = "choice"
    TEXT = "text"
    CHECKBOX = "checkbox"
    INTEGER = "integer"
    NUMBER = "number"
    INTEGER_MAPPING = "integer_mapping"
    OBJECT_MAPPING = "object_mapping"
    OBJECT_SEQUENCE = "object_sequence"
    JSON_OBJECT = "json_object"


MAX_INVENTORY_PAGE_SIZE = 100


_SCOPED_INTERACTION_REASONS = frozenset(
    {
        InteractionReason.ANALYSIS_DOMAIN_CHOICE_REQUIRED,
        InteractionReason.ANALYSIS_DOMAIN_ROIS_REQUIRED,
        InteractionReason.ANALYSIS_DOMAIN_REVIEW_REQUIRED,
        InteractionReason.STRUCTURE_COUNT_POLICY_ACKNOWLEDGEMENT_REQUIRED,
        InteractionReason.PRIMARY_STRUCTURE_COUNT_REVIEW_REQUIRED,
        InteractionReason.SECONDARY_STRUCTURE_SELECTION_REQUIRED,
        InteractionReason.SEMANTIC_REGION_DECISION_REQUIRED,
    }
)


@dataclass(frozen=True, slots=True)
class FormControl:
    name: str
    label: str
    kind: ControlKind
    required: bool = True
    choices: tuple[str, ...] = ()
    value: object = None
    help_text: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.label.strip():
            raise ValueError("form control name and label must be non-empty")
        object.__setattr__(self, "choices", tuple(self.choices))
        object.__setattr__(self, "value", _freeze_json(self.value))


@dataclass(frozen=True, slots=True)
class FormTableRow:
    row_id: str
    values: tuple[object, ...]

    def __post_init__(self) -> None:
        if not self.row_id.strip():
            raise ValueError("form table row ID must be non-empty")
        object.__setattr__(self, "values", tuple(self.values))


@dataclass(frozen=True, slots=True)
class FormTable:
    """Present rows under an optional caption while retaining a stable table ID."""

    table_id: str
    columns: tuple[str, ...]
    rows: tuple[FormTableRow, ...]
    title: str | None = None

    def __post_init__(self) -> None:
        if not self.table_id.strip() or not self.columns:
            raise ValueError("form table ID and columns are required")
        object.__setattr__(self, "columns", tuple(self.columns))
        object.__setattr__(self, "rows", tuple(self.rows))
        if any(len(row.values) != len(self.columns) for row in self.rows):
            raise ValueError("form table row width does not match its columns")


@dataclass(frozen=True, slots=True)
class ArtifactDescriptor:
    """A bounded pointer to a persisted inventory, never embedded inventory data."""

    artifact_id: str
    schema_version: str
    path: str
    sha256: str
    record_count: int

    def __post_init__(self) -> None:
        for field in ("artifact_id", "schema_version", "path", "sha256"):
            _nonblank(getattr(self, field), field)
        if len(self.sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.sha256
        ):
            raise ValueError("artifact descriptor sha256 must be a lowercase SHA-256")
        if isinstance(self.record_count, bool) or not isinstance(self.record_count, int):
            raise ValueError("artifact descriptor record_count must be an integer")
        if self.record_count < 0:
            raise ValueError("artifact descriptor record_count must be non-negative")


def build_paged_inventory_table(
    descriptor: ArtifactDescriptor,
    offset: int,
    limit: int,
    *,
    expected_bindings: Mapping[str, object] | None = None,
) -> FormTable:
    """Read one checksum-bound page without retaining the full inventory."""
    if not isinstance(descriptor, ArtifactDescriptor):
        raise TypeError("inventory page requires an ArtifactDescriptor")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("inventory page offset must be a non-negative integer")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 0 < limit <= MAX_INVENTORY_PAGE_SIZE
    ):
        raise ValueError(f"inventory page limit must be between 1 and {MAX_INVENTORY_PAGE_SIZE}")
    rows = _read_inventory_page(
        descriptor,
        offset=offset,
        limit=limit,
        expected_bindings=expected_bindings,
    )
    return FormTable(
        f"inventory_page:{descriptor.artifact_id}:{offset}:{limit}",
        ("content_key", "roi_id"),
        tuple(
            FormTableRow(
                f"{descriptor.artifact_id}:{item['content_key']}",
                (item["content_key"], item["roi_id"]),
            )
            for item in rows
        ),
    )


class _IncrementalJSON:
    """Decode JSON values incrementally so a page never retains the artifact."""

    def __init__(self, path: Path, *, chunk_size: int = 64 * 1024) -> None:
        self._stream = path.open(encoding="utf-8")
        self._chunk_size = chunk_size
        self._buffer = ""
        self._position = 0
        self._eof = False
        self._decoder = json.JSONDecoder(
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )

    def close(self) -> None:
        self._stream.close()

    def _fill(self) -> bool:
        if self._position:
            self._buffer = self._buffer[self._position :]
            self._position = 0
        chunk = self._stream.read(self._chunk_size)
        if not chunk:
            self._eof = True
            return False
        self._buffer += chunk
        return True

    def _skip_space(self) -> None:
        while True:
            while self._position < len(self._buffer) and self._buffer[self._position].isspace():
                self._position += 1
            if self._position < len(self._buffer) or not self._fill():
                return

    def peek(self) -> str:
        self._skip_space()
        if self._position >= len(self._buffer):
            raise ValueError("inventory artifact JSON ended unexpectedly")
        return self._buffer[self._position]

    def expect(self, token: str) -> None:
        if self.peek() != token:
            raise ValueError(f"inventory artifact JSON expected {token!r}")
        self._position += 1

    def value(self) -> object:
        self._skip_space()
        while True:
            try:
                value, end = self._decoder.raw_decode(self._buffer, self._position)
            except json.JSONDecodeError as exc:
                if not self._eof and self._fill():
                    continue
                raise ValueError("inventory artifact is not strict JSON") from exc
            self._position = end
            return value

    def array(self, consume: Callable[[object], None]) -> None:
        self.expect("[")
        if self.peek() == "]":
            self._position += 1
            return
        while True:
            consume(self.value())
            delimiter = self.peek()
            if delimiter == "]":
                self._position += 1
                return
            self.expect(",")

    def finish(self) -> None:
        self._skip_space()
        while self._position >= len(self._buffer) and not self._eof:
            if not self._fill():
                break
            self._skip_space()
        if self._position < len(self._buffer):
            raise ValueError("inventory artifact JSON has trailing content")


_INVENTORY_BINDING_FIELDS = (
    "slide_id",
    "section_id",
    "analysis_domain_id",
    "analysis_domain_revision",
    "analysis_domain_revision_sha256",
    "tissue_generation_id",
    "pipeline_run_id",
    "config_sha256",
    "semantic_result_set_sha256",
    "structure_id",
    "maximum",
    "observed_count",
    "inventory_sha256",
    "selection_algorithm",
)


def _read_inventory_page(
    descriptor: ArtifactDescriptor,
    *,
    offset: int,
    limit: int,
    expected_bindings: Mapping[str, object] | None,
) -> tuple[dict[str, str], ...]:
    try:
        path = Path(descriptor.path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ValueError("inventory artifact is missing") from exc
    if not path.is_file():
        raise ValueError("inventory artifact is missing")
    _verify_inventory_artifact_sha256(path, descriptor.sha256)

    page: list[dict[str, str]] = []
    item_count = 0
    previous_content_key: str | None = None

    def consume(raw: object) -> None:
        nonlocal item_count, previous_content_key
        if not isinstance(raw, Mapping) or set(raw) != {
            "content_key",
            "roi_id",
            "parent_content_key",
            "geometry_sha256",
        }:
            raise ValueError("inventory artifact row fields are invalid")
        content_key = _sha256_text(raw.get("content_key"), "inventory content key")
        _sha256_text(raw.get("parent_content_key"), "inventory parent content key")
        _sha256_text(raw.get("geometry_sha256"), "inventory geometry SHA-256")
        roi_id = _nonblank(raw.get("roi_id"), "inventory ROI ID")
        if previous_content_key is not None and content_key <= previous_content_key:
            raise ValueError("inventory artifact is not in canonical content-key order")
        previous_content_key = content_key
        if offset <= item_count < offset + limit:
            page.append({"content_key": content_key, "roi_id": roi_id})
        item_count += 1

    reader = _IncrementalJSON(path)
    scalars: dict[str, object] = {}
    seen: set[str] = set()
    try:
        reader.expect("{")
        if reader.peek() != "}":
            while True:
                key = reader.value()
                if not isinstance(key, str) or key in seen:
                    raise ValueError("inventory artifact object keys are invalid")
                seen.add(key)
                reader.expect(":")
                if key == "items":
                    reader.array(consume)
                else:
                    scalars[key] = reader.value()
                delimiter = reader.peek()
                if delimiter == "}":
                    break
                reader.expect(",")
        reader.expect("}")
        reader.finish()
    finally:
        reader.close()
    _verify_inventory_artifact_sha256(path, descriptor.sha256)

    expected_fields = {"schema_version", *_INVENTORY_BINDING_FIELDS, "items"}
    if seen != expected_fields:
        raise ValueError("inventory artifact fields are invalid")
    if (
        scalars.get("schema_version") != descriptor.schema_version
        or descriptor.schema_version != "structure-count-inventory/v1"
        or scalars.get("selection_algorithm") != "manual-select/v1"
    ):
        raise ValueError("inventory artifact schema or algorithm is invalid")
    if (
        isinstance(scalars.get("observed_count"), bool)
        or scalars.get("observed_count") != item_count
        or descriptor.record_count != item_count
    ):
        raise ValueError("inventory artifact record count does not match descriptor")
    if expected_bindings is not None and any(
        expected_bindings.get(field) != scalars.get(field) for field in _INVENTORY_BINDING_FIELDS
    ):
        raise ValueError("inventory artifact binding is stale")
    return tuple(page)


def _verify_inventory_artifact_sha256(path: Path, expected: str) -> None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ValueError("inventory artifact is unreadable") from exc
    if digest.hexdigest() != expected:
        raise ValueError("inventory artifact SHA-256 does not match descriptor")


def _strict_json_object(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    parsed: dict[str, object] = {}
    for key, value in pairs:
        if key in parsed:
            raise ValueError(f"duplicate JSON key: {key}")
        parsed[key] = value
    return parsed


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _sha256_text(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256")
    return value


@dataclass(frozen=True, slots=True)
class PlotArtifact:
    """One immutable, local visualization descriptor; never plot data or science."""

    artifact_id: str
    title: str
    path: str
    sha256: str | None
    status: str

    def __post_init__(self) -> None:
        for field in ("artifact_id", "title", "path", "status"):
            _nonblank(getattr(self, field), field)
        if self.sha256 is not None and (
            len(self.sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.sha256)
        ):
            raise ValueError("plot artifact sha256 must be a lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class FormModel:
    form_id: str
    title: str
    message: str
    allowed_actions: tuple[str, ...]
    controls: tuple[FormControl, ...] = ()
    tables: tuple[FormTable, ...] = ()
    plot_artifacts: tuple[PlotArtifact, ...] = ()
    context: Mapping[str, Any] | None = None
    open_qupath_path: str | None = None

    def __post_init__(self) -> None:
        if not self.form_id.strip() or not self.title.strip():
            raise ValueError("form ID and title must be non-empty")
        object.__setattr__(self, "allowed_actions", tuple(self.allowed_actions))
        object.__setattr__(self, "controls", tuple(self.controls))
        object.__setattr__(self, "tables", tuple(self.tables))
        object.__setattr__(self, "plot_artifacts", tuple(self.plot_artifacts))
        frozen = _freeze_json(dict(self.context or {}))
        object.__setattr__(self, "context", frozen)


def applicable_control_names(
    form: FormModel,
    action: str,
    raw_values: Mapping[str, object] | None = None,
) -> frozenset[str]:
    """Return controls used by the offered action and, when supplied, its source."""
    if not isinstance(form, FormModel):
        raise TypeError("applicable controls require a FormModel")
    if action not in form.allowed_actions:
        raise ValueError("selected action was not offered")
    available = frozenset(control.name for control in form.controls)
    context = form.context or {}
    adapter = context.get("adapter")
    if (
        adapter == "training_plan_selection"
        and context.get("primary")
        and action == "select_training_plan"
        and raw_values is not None
    ):
        if not isinstance(raw_values, Mapping):
            raise ValueError("raw form values must be a mapping")
        source_control = next(
            (control for control in form.controls if control.name == "classifier_source"), None
        )
        source = raw_values.get("classifier_source")
        if (
            source_control is None
            or not isinstance(source, str)
            or source not in source_control.choices
        ):
            raise ValueError("classifier_source must be one of the offered choices")
        if source == "skip":
            return available & {"classifier_source"}
        if source == "saved":
            return available & {"classifier_source", "saved_classifier_export"}
        return available - {"saved_classifier_export"}
    if context.get("reason") == "semantic_region_decision_required":
        return (available & {"candidate_roi_id"} if action == "select_semantic_region"
                else available - {"candidate_roi_id"})
    if adapter == "reference_unmixing":
        action_fields = context.get("reference_action_fields")
        if not isinstance(action_fields, Mapping) or action not in action_fields:
            raise ValueError("reference form lacks fields for the selected action")
        names = action_fields[action]
        if (
            isinstance(names, (str, bytes))
            or not isinstance(names, (tuple, list))
            or any(type(name) is not str for name in names)
        ):
            raise ValueError("reference action form fields are invalid")
        if any(name not in available for name in names):
            raise ValueError("reference action form field is unavailable")
        return frozenset(names)
    if adapter == "spectral_reference":
        names = {"reviewer_id"}
        if action == "accept_signal_dark_measurements":
            names.update({"signal_measurement", "dark_measurements"})
        elif action == "accept_extraction_qc":
            names.add("roi_dispositions")
        return frozenset(names) & available
    if action == InteractionAction.ABORT.value and adapter in {"roi", "marker_setup", "marker_gates"}:
        return frozenset()
    if adapter != "roi":
        return available

    reason = context.get("reason")
    resume_context = context.get("resume_context")
    resume_context = resume_context if isinstance(resume_context, Mapping) else {}

    if reason == InteractionReason.TISSUE_SECTION_REVIEW_REQUIRED.value:
        return available if action == "accept" else available - {"excluded_roi_ids"}

    if reason == InteractionReason.REGISTRATION_REVIEW_REQUIRED.value:
        names = {
            InteractionAction.ACCEPT_REGISTRATION.value: {"reviewed_by"},
            InteractionAction.ACCEPT_REGISTRATION_AND_TRANSFER.value: {
                "reviewed_by",
                "source_object_ids",
            },
            InteractionAction.REJECT.value: {"reviewed_by"},
        }.get(action, set())
        return frozenset(names) & available

    if reason == InteractionReason.OTHER_EXPLICIT_REASON.value and isinstance(
        resume_context.get("moving_reference_availability_decision"), Mapping
    ):
        return (
            available
            if action == InteractionAction.MOVING_REFERENCE_AVAILABLE.value
            else frozenset()
        )

    if reason == InteractionReason.OTHER_EXPLICIT_REASON.value and isinstance(
        resume_context.get("moving_assistance_scope_decision"), Mapping
    ):
        names = {"decided_by"}
        if action == InteractionAction.SET_MOVING_ASSISTANCE_SCOPES.value:
            names.add("moving_assistance_scopes")
        return frozenset(names) & available

    return available


def decode_control_value(control: FormControl, raw_value: object) -> object:
    """Decode one disposable widget value without applying scientific rules."""
    if not isinstance(control, FormControl):
        raise TypeError("control value decoding requires a FormControl")
    if control.kind is ControlKind.INTEGER:
        if isinstance(raw_value, str):
            stripped = raw_value.strip()
            if not stripped and not control.required:
                return None
            try:
                return int(stripped)
            except ValueError as exc:
                raise ValueError(f"{control.name} must be an integer") from exc
        if isinstance(raw_value, bool) or not isinstance(raw_value, int):
            raise ValueError(f"{control.name} must be an integer")
        return raw_value
    if control.kind is ControlKind.NUMBER:
        if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
            if not control.required:
                return None
            raise ValueError(f"{control.name} is required")
        if isinstance(raw_value, bool):
            raise ValueError(f"{control.name} must be a finite number")
        try:
            number = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{control.name} must be a finite number") from exc
        if not math.isfinite(number):
            raise ValueError(f"{control.name} must be a finite number")
        return number
    if control.kind is ControlKind.CHECKBOX:
        if not isinstance(raw_value, bool):
            raise ValueError(f"{control.name} must be a boolean")
        return raw_value
    if control.kind in {
        ControlKind.INTEGER_MAPPING,
        ControlKind.OBJECT_MAPPING,
        ControlKind.OBJECT_SEQUENCE,
        ControlKind.JSON_OBJECT,
    }:
        value = _decode_json_control(control, raw_value)
        if control.kind is ControlKind.OBJECT_SEQUENCE:
            if not isinstance(value, list):
                raise ValueError(f"{control.name} must contain a JSON array")
        elif not isinstance(value, dict):
            raise ValueError(f"{control.name} must contain a JSON object")
        return value
    if control.kind is ControlKind.LOCAL_FILE:
        if isinstance(raw_value, Path):
            raw_value = str(raw_value)
        if not isinstance(raw_value, str) or not raw_value.strip():
            return raw_value
        if control.name.endswith(("_nimbus_checkpoint_path", "_nimbus_score_path")):
            value = raw_value.strip()
            if not (PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()):
                raise ValueError(f"{control.label} requires an absolute path on the processing computer")
            # A streaming review can enter a different platform's path. Resolving
            # here would reinterpret the remote path on the local computer.
            return value
        return str(Path(raw_value.strip()).expanduser().resolve())
    return raw_value


def decode_form_values(
    form: FormModel,
    action: str,
    raw_values: Mapping[str, object],
) -> dict[str, object]:
    """Decode only controls applicable to the selected offered action."""
    if not isinstance(raw_values, Mapping):
        raise ValueError("raw form values must be a mapping")
    applicable = applicable_control_names(form, action, raw_values)
    return {
        control.name: decode_control_value(control, raw_values[control.name])
        for control in form.controls
        if control.name in applicable and control.name in raw_values
    }


def _decoded_action_form(
    form: FormModel,
    action: str,
    raw_values: Mapping[str, object],
) -> tuple[FormModel, dict[str, object]]:
    """

    Decodes the active editor values and limits the form to the selected action's fields.

    Some actions share a page but require different inputs. We determine the field
    names used by this action, decode their editor values, and omit blank optional
    values. The reduced form and decoded values then enter the same field checks
    for both standalone and batch submissions.

    Args:
        form: Complete current form, including all action-specific controls.
        action: Offered action selected by the page.
        raw_values: Text and structured values currently held by the editors.

    Returns:
        Pair containing the action-specific form and its decoded value dictionary.

    Raises:
        ValueError: An active value cannot be decoded into its required type.
    """

    applicable = applicable_control_names(form, action, raw_values)
    controls = {control.name: control for control in form.controls if control.name in applicable}
    decoded = {
        name: value
        for name, value in decode_form_values(form, action, raw_values).items()
        if not (value is None and not controls[name].required)
    }
    action_form = replace(
        form,
        controls=tuple(control for control in form.controls if control.name in applicable),
    )
    return action_form, decoded


def submit_form_values(
    form: FormModel, action: str, raw_values: Mapping[str, object]
) -> dict[str, object] | None:
    """

    Prepares a standalone form's validated continuation from its editor values.

    We decode the fields used by the action and pass them through submit_form(),
    which constructs the continuation needed by the standalone caller. Batch
    pages use submit_batch_form_values() because their processing step constructs
    that continuation when the decision is saved.

    Args:
        form: Current form and its action-specific controls.
        action: Offered action selected for submission.
        raw_values: Current editor values.

    Returns:
        Validated continuation returned by submit_form(), or None after cancellation.
    """

    action_form, decoded = _decoded_action_form(form, action, raw_values)
    return submit_form(action_form, action, decoded)


def submit_batch_form_values(
    form: FormModel, action: str, raw_values: Mapping[str, object]
) -> dict[str, object] | None:
    """

    Checks the active form fields and sends only the values selected for this action.

    The image-analysis validator reads the original pending request when the
    decision is saved. We therefore keep its image lists and completed review
    fields out of this response. Sending an expanded continuation here would
    make a valid selection fail the processing-side field check.

    Args:
        form: Current image-analysis form.
        action: Offered action selected by the Save button.
        raw_values: Current editor values, including inactive controls that
            are removed by the shared field decoder.

    Returns:
        Checked response fields to send with the selected action.

    Raises:
        ValueError: A required field is absent or a choice is incompatible
            with the current pending request.

    """

    action_form, decoded = _decoded_action_form(form, action, raw_values)
    selected = _validated_response_fields(action_form, action, decoded)
    if action_form.context.get("adapter") == "roi":
        context = action_form.context["resume_context"]
        if (
            action_form.context.get("reason")
            == InteractionReason.SUPERVISED_CLASSIFIER_REVIEW_REQUIRED.value
            and context.get("phase") == "prediction_review"
        ):
            if action == "abort":
                return {}
            if action != "review_supervised_classifier":
                raise ValueError("supervised prediction review action is invalid")
            from mif_pipeline.orchestration.batch_phenotyping.supervised_records import (
                AcceptedSupervisedPredictionReview,
            )

            _, binary_svm = _supervised_review_score(context)
            import getpass
            return AcceptedSupervisedPredictionReview(
                context["sha256"],
                0.0 if binary_svm else selected["confidence_threshold"],
                f"localaccount:{getpass.getuser()}",
            ).to_mapping()
        result = StageResult.from_mapping(_mutable_json(action_form.context["stage_result"]))
        result = replace(result, resume_context=action_form.context["resume_context"])
        validate_interaction_response(result, action, selected)
    return selected


def _decode_json_control(control: FormControl, raw_value: object) -> object:
    return _decode_json_value(control.name, raw_value)


def _decode_json_value(field_name: str, raw_value: object) -> object:
    value = raw_value
    if isinstance(raw_value, str):
        try:
            value = json.loads(
                raw_value,
                object_pairs_hook=_strict_json_object,
                parse_constant=_reject_json_constant,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"{field_name} must contain valid JSON with finite numbers " "and no duplicate keys"
            ) from exc
    try:
        return _editable_json_value(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must contain finite JSON-like values") from exc


def _editable_json_value(value: object) -> object:
    if value is None or type(value) in {str, int, bool}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ValueError("JSON object keys must be strings")
        return {key: _editable_json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_editable_json_value(item) for item in value]
    raise ValueError("value is not JSON-like")


def _json_editable_value(value: object) -> object:
    """Return a mutable JSON-compatible copy of an immutable form value."""
    return _editable_json_value(value)


def build_local_file_form(
    *,
    input_path: str = "",
    output_path: str = "",
    library_path: str = "",
    image_index: int | None = None,
    series_index: int | None = None,
) -> FormModel:
    """Return the local-only input/output/library selection model."""
    return FormModel(
        form_id="pipeline_local_files",
        title="Local pipeline files",
        message=(
            "Select local image, output, and spectral-library paths. If "
            "Bio-Formats reports multiple analytical images, enter the exact "
            "image and series indices shown by the inspection error."
        ),
        allowed_actions=("continue",),
        controls=(
            FormControl(
                "input_path", "Input whole-slide image", ControlKind.LOCAL_FILE, value=input_path
            ),
            FormControl(
                "output_path", "Output OME-TIFF", ControlKind.LOCAL_FILE, value=output_path
            ),
            FormControl(
                "library_path", "Spectral library", ControlKind.LOCAL_FILE, value=library_path
            ),
            FormControl(
                "image_index",
                "Bio-Formats image index (optional)",
                ControlKind.INTEGER,
                required=False,
                value=image_index,
            ),
            FormControl(
                "series_index",
                "Bio-Formats series index (optional)",
                ControlKind.INTEGER,
                required=False,
                value=series_index,
            ),
        ),
        context={"local_files_only": True},
    )


def build_stage1_form(workflow: Stage1Workflow) -> FormModel:
    """Project detached stage-one state into editable biomarker rows."""
    if not isinstance(workflow, Stage1Workflow):
        raise ValueError("stage-one form requires a Stage1Workflow")
    from mif_pipeline.io.metadata.spectral import resolve_spectral_state, _resolve_source_spectral_state
    from mif_pipeline.io.metadata.canonical import resolve_unmixing_input_metadata
    state, _ = _resolve_source_spectral_state(workflow.metadata, 0,
        spectral=resolve_spectral_state(workflow.metadata, 0),
        acquisition=resolve_unmixing_input_metadata(workflow.metadata, 0))
    if state == "raw":
        from mif_pipeline.io.metadata.calibration import recorded_autofluorescence_subtracted
        return FormModel(
            form_id="stage1_components", title="Channels and markers",
            message=f"Review one row per output component. The source has {len(workflow.biomarker_rows)} acquired measurement bands.",
            allowed_actions=("apply_biomarkers",),
            controls=(FormControl("component_values", "Output components and markers", ControlKind.OBJECT_SEQUENCE,
                value=[{"component_name": row.component_name, "assignment": row.assignment, "biomarker": row.biomarker}
                       for row in workflow.component_rows]),),
            tables=(),
            context={"adapter": "stage1_components", "workflow_status": workflow.status,
                "component_af_retained": recorded_autofluorescence_subtracted(workflow.metadata) is not True},
        )
    rows = tuple(
        FormTableRow(
            f"{row.image_index}:{row.channel_index}",
            (
                row.channel_id,
                row.channel_name,
                row.fluorophore,
                row.component_name,
                row.biomarker,
                row.biomarker_assignment,
            ),
        )
        for row in workflow.biomarker_rows
    )
    return FormModel(
        form_id="stage1_biomarkers",
        title="Review channel biomarkers",
        message=(
            "Assign each channel an explicit biomarker and, when Bio-Formats did "
            "not preserve it, enter the fluorophore/component name. Component and "
            "biomarker identity remain separate. Every component is required. "
            "Existing OME Channel.Fluor values are prefilled; reviewed values fill "
            "only missing Channel.Fluor fields in the new processed image."
        ),
        allowed_actions=("apply_biomarkers",),
        controls=(
            FormControl(
                "source_path",
                "Local whole-slide image",
                ControlKind.LOCAL_FILE,
                value=str(workflow.source_path),
            ),
        ),
        tables=(
            FormTable(
                "biomarker_rows",
                (
                    "channel_id",
                    "channel_name",
                    "source_fluorophore",
                    "component_name",
                    "biomarker",
                    "assignment",
                ),
                rows,
            ),
        ),
        context={"workflow_status": workflow.status, "local_files_only": True},
    )


def apply_stage1_form(
    workflow: Stage1Workflow,
    values: Mapping[str, Mapping[str, object]],
) -> Stage1Workflow:
    """Submit explicit stage-one row values through the domain validator."""
    from mif_pipeline.io.metadata.spectral import resolve_spectral_state, _resolve_source_spectral_state
    from mif_pipeline.io.metadata.canonical import resolve_unmixing_input_metadata
    state, _ = _resolve_source_spectral_state(workflow.metadata, 0,
        spectral=resolve_spectral_state(workflow.metadata, 0),
        acquisition=resolve_unmixing_input_metadata(workflow.metadata, 0))
    if state == "raw":
        from mif_pipeline.preprocessing.inspection.workflow import apply_stage1_component_values, apply_stage1_channel_content
        return apply_stage1_component_values(apply_stage1_channel_content(workflow, "raw"), values["component_values"])
    return apply_stage1_biomarker_values(workflow, values)


def _supervised_review_score(context: Mapping[str, object]) -> tuple[str, bool]:
    """Name the native score without treating vote agreement as calibration."""
    labels = {
        "native_tree_vote_fraction": "Forest vote fraction",
        "native_svm_pairwise_vote_fraction": "SVM vote agreement",
        "native_logistic_ovr_normalized_score": "Normalized logistic score",
    }
    # Older RF reviews predate explicit score metadata.
    kind = context.get("score_kind", "native_tree_vote_fraction")
    count = context.get("class_count")
    if kind not in labels or (count is not None and (type(count) is not int or count < 2)):
        raise ValueError("supervised prediction score metadata is invalid")
    return labels[kind], kind == "native_svm_pairwise_vote_fraction" and count == 2


def present_manual_structure_selection(form: FormModel) -> FormModel:
    """Give current and retained selection forms readable copy without changing authority."""
    if form.context.get("reason") != "secondary_structure_selection_required":
        return form
    context = form.context["resume_context"]
    structure = str(context["structure_id"]).replace("_", " ")
    labels = {
        "selected_content_keys": "Structures to include",
        "selection_criterion": "Selection rule",
        "reviewer_id": "Reviewer name or ID",
        "rationale": "Reason for this selection",
    }
    return replace(
        form,
        title="Select structures for analysis",
        message=(
            f"Select exactly {context['maximum']} of the {context['observed_count']} accepted "
            f"{structure} structures for analysis."
        ),
        controls=tuple(replace(control, label=labels.get(control.name, control.label))
                       for control in form.controls),
    )


def build_roi_interaction_form(
    result: StageResult,
    *,
    qupath_project_path: str | None = None,
    tissue_section_summary_error: str | None = None,
    region_review: Mapping[str, object] | None = None,
    inventory_page_offset: int = 0,
    inventory_page_limit: int = MAX_INVENTORY_PAGE_SIZE,
) -> FormModel:
    """Render one pending ROI review and any separately authenticated tissue geometry.

    Tissue geometry comes from the selected saved generation. Exclusion choices
    bind to the ROI IDs offered by the pending request and apply on acceptance.
    """
    if not isinstance(result, StageResult) or result.status is not StageStatus.NEEDS_INTERACTION:
        raise ValueError("ROI form requires a needs_interaction StageResult")
    assert result.interaction_reason is not None
    context = _mutable_json(resolve_resume_envelope(result.resume_context or {}).service_context)
    controls: list[FormControl] = []
    tables: list[FormTable] = []
    reason = result.interaction_reason

    domain_decision = reason in {
        InteractionReason.ANALYSIS_DOMAIN_CHOICE_REQUIRED,
        InteractionReason.ANALYSIS_DOMAIN_ROIS_REQUIRED,
        InteractionReason.ANALYSIS_DOMAIN_REVIEW_REQUIRED,
    }
    if (reason in _SCOPED_INTERACTION_REASONS and not domain_decision
            and reason not in {InteractionReason.SEMANTIC_REGION_DECISION_REQUIRED,
                               InteractionReason.PRIMARY_STRUCTURE_COUNT_REVIEW_REQUIRED}):
        tables.extend(_v2_context_tables(context))
        tables.extend(_analysis_domain_assistance_tables(context))

    if reason is InteractionReason.PRIMARY_STRUCTURE_COUNT_REVIEW_REQUIRED:
        labels = context.get("primary_class_labels", {})
        rows = []
        for output in context["primary_count_outputs"]:
            key = output["output_id"]
            maximum = output["maximum"]
            observed = output["observed_count"]
            rows.append(FormTableRow(key, (
                "All tissue classes combined" if key == "primary_classifier" else labels.get(key, key),
                observed, "No maximum" if maximum is None else maximum,
            )))
        rows.sort(key=lambda row: row.row_id != "primary_classifier")
        tables.append(FormTable("primary_region_counts",
            ("Tissue class", "Detected regions", "Maximum"), tuple(rows),
            title=f"Region counts · {result.slide_id}"))
    elif reason is InteractionReason.SECONDARY_STRUCTURE_SELECTION_REQUIRED:
        controls.extend(
            (
                FormControl(
                    "selected_content_keys",
                    "Selected structures",
                    ControlKind.OBJECT_SEQUENCE,
                    value=(),
                    help_text=(
                        "Select inventory rows across the bounded pages. Exactly "
                        "the configured maximum is required."
                    ),
                ),
                FormControl(
                    "selection_criterion",
                    "Selection criterion",
                    ControlKind.TEXT,
                ),
                FormControl(
                    "reviewer_id",
                    "Reviewer/operator identifier",
                    ControlKind.TEXT,
                ),
                FormControl(
                    "rationale",
                    "Selection rationale",
                    ControlKind.TEXT,
                ),
            )
        )
    elif getattr(reason, "value", None) == "semantic_region_decision_required":
        region_assignment = context.get("schema_version") == "semantic-region-interaction/v2"
        if region_assignment and "select_semantic_region" in result.allowed_actions:
            controls.append(FormControl(
                "candidate_roi_id", "Region to review", ControlKind.CHOICE, required=False,
                value=context["roi_id"],
                choices=tuple(item["roi_id"] for item in context.get("review_candidates", ())),
            ))
        if region_assignment and context.get("declared_subtypes"):
            controls.append(FormControl(
                "subtype_choice", "Subtype", ControlKind.CHOICE, required=False,
                choices=tuple("subtype:" + name for name in context["declared_subtypes"]) + ("cannot_assess",),
            ))
        controls.append(FormControl(
            "rationale", "Observation (optional)" if region_assignment else "Reason for this answer",
            ControlKind.TEXT, required=not region_assignment,
        ))
        tables.extend(_semantic_scope_tables(context, region_review=region_review))
        tables.extend(_semantic_evidence_tables(context))
    elif reason is InteractionReason.TISSUE_SECTION_REVIEW_REQUIRED:
        controls.append(FormControl(
            "excluded_roi_ids", "Excluded tissue sections", ControlKind.OBJECT_SEQUENCE,
            required=False, value=(),
        ))
    elif reason is InteractionReason.TISSUE_NATIVE_LEVEL_REQUIRED:
        raw_inventory = context.get("native_pyramid_inventory")
        if (
            isinstance(raw_inventory, (str, bytes))
            or not isinstance(raw_inventory, Sequence)
            or not raw_inventory
        ):
            raise ValueError("tissue native-level interaction lacks an inventory")
        recommended = context.get("recommended_native_resolution_index")
        rows: list[FormTableRow] = []
        for raw in raw_inventory:
            if not isinstance(raw, Mapping) or set(raw) != {
                "native_resolution_index",
                "x_downsample",
                "y_downsample",
                "x_um_per_px",
                "y_um_per_px",
            }:
                raise ValueError("native pyramid inventory row is invalid")
            index = _nonnegative_int(raw["native_resolution_index"], "native resolution index")
            rows.append(
                FormTableRow(
                    f"native-level:{index}",
                    (
                        index,
                        raw["x_downsample"],
                        raw["y_downsample"],
                        raw["x_um_per_px"],
                        raw["y_um_per_px"],
                        index == recommended,
                    ),
                )
            )
        compatible = context.get("compatible_native_resolution_indices")
        offered = tuple(row for row in rows
                        if (compatible is None or row.values[0] in compatible)
                        and (row.values[0] != 0 or context.get("allow_level0_selection") is True))
        if not offered:
            raise ValueError("No permitted image resolution is available for this detector.")
        indices = tuple(str(row.values[0]) for row in offered)
        default = str(recommended) if str(recommended) in indices else indices[0]
        controls.append(FormControl(
            "native_resolution_index",
            "Saved detector resolution" if len(indices) == 1 else "Image resolution",
            ControlKind.CHOICE,
            choices=indices,
            value=default,
            help_text=(None if len(indices) == 1 else
                       "Smaller stored images usually reduce memory and processing time."),
        ))
    elif reason is InteractionReason.PHENOTYPING_MODE_REQUIRED:
        if "population_options" in context:
            controls.append(FormControl(
                "phenotyping_population", "Cells to phenotype", ControlKind.JSON_OBJECT,
                value=context["phenotyping_population"],
            ))
        controls.append(
            FormControl(
                "mode",
                "Phenotyping method",
                ControlKind.CHOICE,
                required=False,
                choices=_string_sequence(context.get("available_modes", ())),
            )
        )
    elif reason is InteractionReason.PHENOTYPE_DEFINITION_COUNT_REQUIRED:
        controls.append(
            FormControl(
                "phenotype_count",
                "Number of phenotype definitions",
                ControlKind.INTEGER,
                required=False,
            )
        )
    elif reason in {
        InteractionReason.PHENOTYPE_CLASSES_REQUIRED,
        InteractionReason.SUPERVISED_PHENOTYPE_CLASSES_REQUIRED,
    }:
        controls.append(
            FormControl(
                "phenotype_classes",
                "Ordered phenotype class definitions",
                ControlKind.OBJECT_SEQUENCE,
                required=False,
                help_text="JSON array of class_id and display_name objects in precedence order.",
            )
        )
    elif reason is InteractionReason.PHENOTYPING_MEASUREMENTS_REQUIRED:
        available = _string_sequence(context.get("available_feature_ids", ()))
        controls.append(
            FormControl(
                "feature_ids",
                "Selected cell measurements",
                ControlKind.OBJECT_SEQUENCE,
                required=False,
                choices=available,
                value=list(context.get("selected_feature_ids", ())),
            )
        )
        tables.append(
            FormTable(
                "phenotyping_measurements",
                ("feature_id",),
                tuple(
                    FormTableRow(f"feature:{index}", (feature_id,))
                    for index, feature_id in enumerate(available)
                ),
            )
        )
    elif reason is InteractionReason.MARKER_COMPOSITE_RULES_REQUIRED:
        classes = _phenotype_class_rows(context.get("phenotype_classes", ()))
        biomarkers = _string_sequence(context.get("known_biomarker_ids", ()))
        controls.extend(
            (
                FormControl(
                    "composite_rules",
                    "Ordered marker composite rules",
                    ControlKind.OBJECT_SEQUENCE,
                    required=False,
                ),
                FormControl(
                    "residual_phenotype_id",
                    "Residual phenotype",
                    ControlKind.CHOICE,
                    required=False,
                    choices=tuple(item[0] for item in classes),
                ),
            )
        )
        tables.append(
            FormTable(
                "marker_composite_matrix",
                ("phenotype", *biomarkers),
                tuple(
                    FormTableRow(
                        f"phenotype:{class_id}",
                        (display_name, *("positive | negative | ignore" for _ in biomarkers)),
                    )
                    for class_id, display_name in classes
                ),
            )
        )
    elif reason is InteractionReason.MARKER_THRESHOLD_REVIEW_REQUIRED:
        biomarkers = _string_sequence(context.get("known_biomarker_ids", ()))
        controls.append(
            FormControl(
                "accepted_thresholds",
                "Accepted thresholds",
                ControlKind.OBJECT_MAPPING,
                required=False,
                choices=biomarkers,
            )
        )
    elif reason is InteractionReason.MARKER_RULES_REVIEW_REQUIRED:
        controls.append(
            FormControl(
                "accepted_generation",
                "Accepted marker rules",
                ControlKind.JSON_OBJECT,
                required=False,
            )
        )
    elif reason is InteractionReason.SUPERVISED_TRAINING_ANNOTATIONS_REQUIRED:
        class_ids = _string_sequence(context.get("class_ids", ()))
        feature_schema = context.get("feature_schema", ())
        feature_count = (
            len(feature_schema)
            if isinstance(feature_schema, Sequence) and not isinstance(feature_schema, (str, bytes))
            else 0
        )
        tables.append(
            FormTable(
                "supervised_training_instructions",
                ("classes", "feature_count", "point_radius_level0"),
                (
                    FormTableRow(
                        "supervised:instructions",
                        (
                            ", ".join(class_ids),
                            feature_count,
                            context.get("point_selection_radius_level0"),
                        ),
                    ),
                ),
            )
        )
        controls.append(
            FormControl(
                "classifier",
                "Saved native QuPath classifier descriptor",
                ControlKind.JSON_OBJECT,
                required=False,
                value=context.get("classifier", {}),
            )
        )
    elif reason is InteractionReason.SUPERVISED_CLASSIFIER_REVIEW_REQUIRED:
        if context.get("phase") == "prediction_review":
            score_label, binary_svm = _supervised_review_score(context)
            tables.append(
                FormTable(
                    "supervised_prediction_summary",
                    ("Item", "Current predictions"),
                    (
                        FormTableRow("cells", ("Cells", context.get("cell_count"))),
                        FormTableRow("classes", ("Classes", context.get("class_count", "Unavailable"))),
                        FormTableRow("score", ("Score", score_label)),
                        FormTableRow(
                            "interpretation",
                            ("Interpretation", (
                                "With two classes, SVM vote agreement is 1 for every prediction, so a score cutoff cannot distinguish uncertain cells. Review the validation results to assess classification errors."
                                if binary_svm else
                                "Cells with a score below the threshold become Unclassified. A threshold of zero retains every predicted class."
                            )),
                        ),
                    ),
                )
            )
            if not binary_svm:
                controls.append(
                    FormControl(
                        "confidence_threshold", f"{score_label} threshold (0–1)",
                        ControlKind.NUMBER, value=context.get("confidence_threshold",
                            0.0 if context.get("score_kind") == "native_svm_pairwise_vote_fraction" else 0.5),
                    )
                )
        else:
            controls.append(
                FormControl(
                    "accepted_generation",
                    "Accepted supervised classifier generation",
                    ControlKind.JSON_OBJECT,
                    required=False,
                )
            )
    elif reason is InteractionReason.CLUSTERING_CONFIGURATION_REQUIRED:
        from mif_pipeline._defaults import default_section

        defaults = default_section("clustering")
        if (
            result.stage == "unsupervised_preparation"
            and context.get("schema_version") == "batch-clustering-setup/v2"
        ):
            features = _batch_clustering_features(context)
            initial = context.get("initial_clustering_config") or {}
            method = context.get("configured_method") or initial.get(
                "method", "snn_leiden" if initial else "phenograph"
            )
            methods = ("phenograph", "flowsom")
            if method == "snn_leiden":
                methods = ("snn_leiden", *methods)
            method_settings = context.get("configured_method_settings") or initial.get("method_settings") or {}
            initial_features = {item["feature_id"]: item for item in initial.get("feature_specs", ())}
            if context.get("clustering_mode") is None:
                controls.append(
                    FormControl(
                        "clustering_mode",
                        "Fit scope",
                        ControlKind.CHOICE,
                        choices=("pooled", "per_slide"),
                        value=initial.get("mode"),
                    )
                )
            controls.extend(
                (
                    FormControl(
                        "feature_ids",
                        "Cell measurements",
                        ControlKind.OBJECT_SEQUENCE,
                        choices=tuple(item["feature_id"] for item in features),
                        value=tuple(feature_id for feature_id in initial_features
                                    if feature_id in {item["feature_id"] for item in features}),
                        help_text="Choose the cell measurements used to compare cells during clustering.",
                    ),
                    FormControl(
                        "method", "Unsupervised method", ControlKind.CHOICE,
                        choices=methods,
                        value=method,
                    ),
                    FormControl(
                        "k",
                        "Nearest neighbours (k)",
                        ControlKind.INTEGER,
                        value=(
                            context.get("configured_k")
                            if context.get("configured_k") is not None
                            else defaults["k"]
                        ),
                        help_text="Each cell is compared with this many neighbours using the selected, scaled and weighted measurements. Larger values can connect populations that remain separate with fewer neighbours.",
                    ),
                    FormControl(
                        "resolution",
                        "Leiden resolution",
                        ControlKind.NUMBER,
                        value=(
                            context.get("configured_resolution")
                            if context.get("configured_resolution") is not None
                            else defaults["resolution"]
                        ),
                        help_text="Higher values usually produce more, smaller clusters; lower values favour broader groups.",
                    ),
                    FormControl(
                        "flowsom_clusters", "Final clusters", ControlKind.INTEGER,
                        value=method_settings.get("n_clusters",20),
                        help_text="Number of final groups (metaclusters) formed by combining map nodes.\nToo few groups can combine distinct cell types. Too many groups can divide one cell type into several clusters.",
                    ),
                    FormControl(
                        "flowsom_grid", "Map width and height", ControlKind.INTEGER,
                        value=method_settings.get("grid_size",10),
                        help_text="A width of 10 gives a 10 × 10 map with 100 nodes. Each node learns one value for every selected measurement.",
                    ),
                    FormControl(
                        "flowsom_iterations", "Training iterations", ControlKind.INTEGER,
                        value=method_settings.get("iterations",10),
                        help_text="Each iteration draws as many training examples as there are selected cells.",
                    ),
                    FormControl(
                        "seed",
                        "Random seed",
                        ControlKind.INTEGER,
                        value=(
                            context.get("configured_seed")
                            if context.get("configured_seed") is not None
                            else defaults["seed"]
                        ),
                        help_text="Use the same seed to reproduce the clustering run.",
                    ),
                )
            )
            for index, feature in enumerate(features):
                controls.append(
                    FormControl(
                        f"feature_weight_{index}",
                        f"{feature['measurement_name']} weight",
                        ControlKind.NUMBER,
                        value=initial_features.get(feature["feature_id"], {}).get(
                            "weight", defaults["initial_feature_weight"]),
                    )
                )
        else:
            controls.append(
                FormControl(
                    "clustering_config",
                    "Clustering configuration",
                    ControlKind.JSON_OBJECT,
                    required=False,
                    value=context.get("clustering_config", {}),
                )
            )
    elif reason is InteractionReason.TISSUE_DETECTION_CHANNEL_REQUIRED:
        channels = _string_sequence(context.get("available_channel_ids", ()))
        controls.append(
            FormControl(
                "reference_channel_id",
                "Reference channel",
                ControlKind.CHOICE,
                choices=channels,
            )
        )
    elif reason in {
        InteractionReason.PRIMARY_TRAINING_ANNOTATIONS_REQUIRED,
        InteractionReason.SECONDARY_TRAINING_ANNOTATIONS_REQUIRED,
    }:
        requirement = context.get("training_annotation_requirements")
        if not isinstance(requirement, Mapping):
            raise ValueError("training interaction lacks annotation requirements")
        class_values = requirement.get("classes")
        if isinstance(class_values, (str, bytes)) or not isinstance(class_values, Sequence):
            raise ValueError("training annotation classes must be a sequence")
        rows: list[FormTableRow] = []
        for value in class_values:
            if not isinstance(value, Mapping):
                raise ValueError("training annotation class must be a mapping")
            class_id = _nonblank(value.get("class_id"), "training class ID")
            parents = _string_sequence(value.get("eligible_parent_ids", ()))
            rows.append(
                FormTableRow(
                    class_id,
                    (
                        _nonblank(value.get("qupath_label"), "QuPath label"),
                        _nonnegative_int(value.get("required"), "required"),
                        _nonnegative_int(value.get("completed"), "completed"),
                        _nonnegative_int(value.get("remaining"), "remaining"),
                        ", ".join(parents),
                    ),
                )
            )
        tables.append(
            FormTable(
                "training_annotation_counts",
                ("qupath_label", "required", "completed", "remaining", "eligible_parent"),
                tuple(rows),
            )
        )
    elif reason is InteractionReason.BIOMARKER_THRESHOLD_REQUIRED:
        biomarkers = _string_sequence(context.get("biomarker_ids", result.affected_object_ids))
        controls.append(
            FormControl(
                "known_marker_rules",
                "Confirmed biomarker threshold rules",
                ControlKind.OBJECT_MAPPING,
                choices=biomarkers,
            )
        )
    elif reason is InteractionReason.OTHER_EXPLICIT_REASON and isinstance(
        context.get("moving_reference_availability_decision"), Mapping
    ):
        controls.extend(
            (
                FormControl(
                    "moving_image",
                    "Local moving-modality image",
                    ControlKind.LOCAL_FILE,
                    required=False,
                ),
                FormControl(
                    "moving_image_id", "Exact source image ID", ControlKind.TEXT, required=False
                ),
                FormControl("moving_modality", "Moving modality", ControlKind.TEXT, required=False),
                FormControl(
                    "source_metadata",
                    "Exact source metadata",
                    ControlKind.JSON_OBJECT,
                    required=False,
                ),
            )
        )
    elif reason is InteractionReason.OTHER_EXPLICIT_REASON and isinstance(
        context.get("moving_reference_usage_decision"), Mapping
    ):
        pass
    elif reason is InteractionReason.OTHER_EXPLICIT_REASON and isinstance(
        context.get("classifier_reuse_decision"), Mapping
    ):
        controls.append(
            FormControl(
                "decided_by",
                "Reviewer/operator identifier",
                ControlKind.TEXT,
            )
        )
    elif reason is InteractionReason.OTHER_EXPLICIT_REASON and isinstance(
        context.get("moving_assistance_scope_decision"), Mapping
    ):
        controls.extend(
            (
                FormControl(
                    "moving_assistance_scopes",
                    "Exact moving-assisted classifier scopes",
                    ControlKind.OBJECT_SEQUENCE,
                    required=False,
                ),
                FormControl(
                    "decided_by",
                    "Reviewer/operator identifier",
                    ControlKind.TEXT,
                ),
            )
        )
    elif reason is InteractionReason.REGISTRATION_REVIEW_REQUIRED:
        controls.append(
            FormControl(
                "reviewed_by",
                "Reviewer",
                ControlKind.TEXT,
                required=False,
            )
        )
        if "accept_registration_and_transfer" in result.allowed_actions:
            controls.append(
                FormControl(
                    "source_object_ids",
                    "Source area annotation IDs to transfer",
                    ControlKind.OBJECT_SEQUENCE,
                    required=False,
                )
            )
        pending = context.get("pending_registration")
        if isinstance(pending, Mapping):
            qc = pending.get("registration_qc")
            qc = qc if isinstance(qc, Mapping) else {}
            status = str(qc.get("computed_status", "Unavailable")).replace("_", " ").capitalize()
            tables.append(FormTable(
                "registration_status", ("Saved alignment", "Checks"),
                (FormTableRow("registration-status", ("Tissue coverage and local area change", status)),),
                title="Alignment decision",
            ))

    project_path = qupath_project_path
    if project_path is None:
        for key in ("qupath_project_path", "project_path", "project_file"):
            candidate = context.get(key)
            if candidate is not None:
                project_path = candidate
                break
    if project_path is not None and (not isinstance(project_path, str) or not project_path.strip()):
        raise ValueError("QuPath project path must be non-empty text")
    tissue_count = None
    if reason is InteractionReason.TISSUE_SECTION_REVIEW_REQUIRED and region_review is None:
        qc = context.get("tissue_detection_qc", {})
        tissue_count = qc.get("component_count")
        if result.affected_object_ids:
            tables.append(FormTable(
                "affected_objects", ("ROI ID",),
                tuple(FormTableRow(f"affected:{index}", (object_id,))
                      for index, object_id in enumerate(result.affected_object_ids)),
                title="Sections reported by this review",
            ))
    elif result.affected_object_ids and not domain_decision and reason not in {
        InteractionReason.TISSUE_SECTION_REVIEW_REQUIRED,
        InteractionReason.SEMANTIC_REGION_DECISION_REQUIRED,
        InteractionReason.PHENOTYPING_MODE_REQUIRED,
        InteractionReason.REGISTRATION_REVIEW_REQUIRED,
    }:
        tables.append(
            FormTable(
                "affected_objects",
                ("object_id",),
                tuple(
                    FormTableRow(f"affected:{index}", (object_id,))
                    for index, object_id in enumerate(result.affected_object_ids)
                ),
            ),
        )
    inventory_tables = _inventory_descriptor_tables(
        context,
        offset=inventory_page_offset,
        limit=inventory_page_limit,
    )
    tables.extend(inventory_tables)
    form_context = {
        "adapter": "roi",
        "reason": reason.value,
        "resume_context": context,
        "affected_object_ids": list(result.affected_object_ids),
        "stage_result": result.to_mapping(),
    }
    if context.get("schema_version") == "semantic-region-interaction/v2":
        form_context["choice_labels"] = {"subtype_choice": {
            **{"subtype:" + name: name for name in context.get("declared_subtypes", ())},
            "cannot_assess": "Cannot assess",
        }}
        form_context["choice_labels"]["candidate_roi_id"] = {
            item["roi_id"]: f"Region {item['region_index']}" + (
                f" · {item['area_um2']:,.0f} µm²" if type(item.get("area_um2")) in {int, float} else "")
            for item in context.get("review_candidates", ())
        }
    if region_review is not None and reason not in {
        InteractionReason.ANALYSIS_DOMAIN_CHOICE_REQUIRED,
    }:
        form_context["region_review"] = region_review
    if (
        reason is InteractionReason.CLUSTERING_CONFIGURATION_REQUIRED
        and result.stage == "unsupervised_preparation"
        and context.get("schema_version") == "batch-clustering-setup/v2"
    ):
        form_context["choice_labels"] = {
            "feature_ids": {
                item["feature_id"]: item["measurement_name"]
                for item in _batch_clustering_features(context)
            },
            "clustering_mode": {
                "per_slide": "Per slide",
                "pooled": "Pooled",
            },
            "method": {"snn_leiden":"Shared-neighbour Leiden", "phenograph":"PhenoGraph", "flowsom":"FlowSOM"},
        }
    if reason is InteractionReason.TISSUE_NATIVE_LEVEL_REQUIRED:
        form_context["choice_labels"] = {"native_resolution_index": {
            str(row.values[0]): f"Level {row.values[0]} · {row.values[3]:.3g} × {row.values[4]:.3g} µm/pixel"
            for row in offered}}
    if inventory_tables:
        form_context.update(
            {
                "inventory_page_offset": inventory_page_offset,
                "inventory_page_limit": inventory_page_limit,
            }
        )
    region_message = {
        InteractionReason.PRIMARY_STRUCTURE_COUNT_REVIEW_REQUIRED: (
            "Select a tissue class to inspect its outlines in QuPath. Excluding an analysis region removes it from further processing."
        ),
        InteractionReason.TISSUE_SECTION_REVIEW_REQUIRED: (
            (f"{tissue_count} tissue section{'s' if tissue_count != 1 else ''} in this image. "
             if tissue_count is not None else "")
            + (tissue_section_summary_error or "Review the outlines in QuPath.")
        ) if region_review is None else "",
        InteractionReason.ANALYSIS_DOMAIN_CHOICE_REQUIRED: "Choose the tissue area to use for classification, cell analysis and measurements.",
        InteractionReason.ANALYSIS_DOMAIN_ROIS_REQUIRED: (
            "Draw the areas to analyse inside the accepted tissue sections. "
            "Select the regions to carry forward. Each must lie inside a single section and may not overlap another selected region."
        ),
        InteractionReason.ANALYSIS_DOMAIN_REVIEW_REQUIRED: "Analysis will be limited to these regions.",
        InteractionReason.SEMANTIC_REGION_DECISION_REQUIRED: (
            ("Review the image and selected measurements, then choose a subtype and accept or reject the region."
             if context.get("declared_subtypes") else
             "Review the image and selected measurements, then accept or reject the region.")
            if context.get("schema_version") == "semantic-region-interaction/v2" else
            context["prompt"].strip()
            if isinstance(context.get("prompt"), str) and context["prompt"].strip()
            else "Inspect the region and its measurements, then choose whether to accept it."
        ),
    }.get(reason)
    form = FormModel(
        form_id=f"roi_{reason.value}",
        title={
            "tissue_native_level_required": "Tissue-detection resolution",
            "tissue_section_review_required": "Review detected tissue",
            "clustering_configuration_required": "Unsupervised phenotyping",
            "cluster_label_review_required": "Review cluster labels",
            "phenotyping_mode_required": "Choose a phenotyping method",
            "analysis_domain_choice_required": "Choose the analysis area",
            "analysis_domain_rois_required": "Select analysis regions",
            "analysis_domain_review_required": "Review analysis regions",
            "registration_review_required": "Review image alignment",
            "transfer_review_required": "Review transferred objects",
            "roi_review_required": "Review selected regions",
            "secondary_roi_overlap_policy_required": "Overlapping analysis regions",
            "primary_structure_count_review_required": "Review region counts",
            "structure_count_policy_acknowledgement_required": "Structure count review policy",
            "secondary_structure_selection_required": "Select secondary structures",
            "semantic_region_decision_required": "Review structure",
        }.get(reason.value, reason.value.replace("_", " ").title()),
        message=region_message if region_message is not None else (
            "Select the cell measurements and clustering settings."
            if reason is InteractionReason.CLUSTERING_CONFIGURATION_REQUIRED
            else result.message or (
                "The saved detector supports the resolution shown below."
                if len(offered) == 1
                else "Choose a resolution supported by the saved detector."
            )
            if reason is InteractionReason.TISSUE_NATIVE_LEVEL_REQUIRED
            else result.message or ""
        ),
        allowed_actions=result.allowed_actions,
        controls=tuple(controls),
        tables=tuple(tables),
        plot_artifacts=_plot_artifacts(context),
        context=form_context,
        open_qupath_path=project_path,
    )
    return present_manual_structure_selection(form)


def page_roi_inventory_form(
    form: FormModel,
    *,
    offset: int,
    control_values: Mapping[str, object] | None = None,
) -> FormModel:
    """Rebuild one bounded manual-inventory page while preserving form values."""
    if (
        not isinstance(form, FormModel)
        or form.context.get("adapter") != "roi"
        or form.context.get("reason")
        != InteractionReason.SECONDARY_STRUCTURE_SELECTION_REQUIRED.value
    ):
        raise ValueError("inventory paging requires a manual-selection ROI form")
    limit = form.context.get("inventory_page_limit")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("inventory page limit is invalid")
    result = StageResult.from_mapping(_mutable_json(form.context["stage_result"]))
    result = replace(result, resume_context=form.context["resume_context"])
    rebuilt = build_roi_interaction_form(
        result,
        qupath_project_path=form.open_qupath_path,
        region_review=form.context.get("region_review"),
        inventory_page_offset=offset,
        inventory_page_limit=limit,
    )
    values = {control.name: control.value for control in form.controls}
    if control_values is not None:
        unknown = set(control_values) - set(values)
        if unknown:
            raise ValueError("inventory page control values contain unknown fields")
        values.update(control_values)
    return replace(
        rebuilt,
        controls=tuple(
            replace(control, value=values[control.name]) for control in rebuilt.controls
        ),
    )


def selected_content_keys_from_inventory_page(
    form: FormModel,
    *,
    selected_row_ids: Sequence[str],
    current_content_keys: Sequence[str],
) -> tuple[str, ...]:
    """Replace this page's selections and return canonical inventory order."""
    if (
        isinstance(selected_row_ids, (str, bytes))
        or isinstance(current_content_keys, (str, bytes))
        or not isinstance(selected_row_ids, Sequence)
        or not isinstance(current_content_keys, Sequence)
    ):
        raise ValueError("manual inventory selections must be sequences")
    page = next(
        (table for table in form.tables if table.table_id.startswith("inventory_page:")),
        None,
    )
    if page is None or page.columns != ("content_key", "roi_id"):
        raise ValueError("manual inventory page is unavailable")
    row_keys = {
        row.row_id: _sha256_text(row.values[0], "inventory content key") for row in page.rows
    }
    if any(not isinstance(row_id, str) for row_id in selected_row_ids):
        raise ValueError("manual inventory row IDs must be text")
    selected_ids = tuple(selected_row_ids)
    if len(set(selected_ids)) != len(selected_ids) or not set(selected_ids) <= set(row_keys):
        raise ValueError("manual inventory selection contains an unknown row")
    current = tuple(_sha256_text(key, "selected content key") for key in current_content_keys)
    if len(set(current)) != len(current):
        raise ValueError("manual inventory selection duplicates a content key")
    page_keys = set(row_keys.values())
    combined = {key for key in current if key not in page_keys} | {
        row_keys[row_id] for row_id in selected_ids
    }
    return tuple(sorted(combined))


def _validated_response_fields(
    form: FormModel,
    action: str | None,
    response: Mapping[str, object],
) -> dict[str, object] | None:
    """

    Checks the values supplied for the selected form action.

    The action-specific form contains only the controls used by that action.
    We check required fields, permitted choices and numerical or structured values,
    then build a dictionary from those checked fields. For cell clustering, the
    selected controls are arranged into the clustering request before return.

    Args:
        form: Form restricted to the controls used by this action.
        action: Offered action, or None when the form is cancelled.
        response: Decoded editor values to check.

    Returns:
        Checked response fields, or None after cancellation.

    Raises:
        ValueError: Required fields, types or choices do not match the form.
    """
    if action is None:
        return None
    if action not in form.allowed_actions:
        raise ValueError("selected action was not offered")
    if not isinstance(response, Mapping):
        raise ValueError("form response must be a mapping")
    expected = {control.name for control in form.controls if control.required}
    optional = {control.name for control in form.controls if not control.required}
    supplied = set(response)
    if supplied - expected - optional:
        raise ValueError("form response contains fields that were not offered")
    if expected - supplied:
        missing = sorted(expected - supplied)
        if "decided_by" in missing:
            raise ValueError("classifier reuse decision requires an operator identifier")
        raise ValueError(f"form response is missing required fields: {missing}")

    validated: dict[str, object] = {}
    for control in form.controls:
        if control.name not in response:
            continue
        value = response[control.name]
        if control.kind is ControlKind.CHOICE:
            if not isinstance(value, str) or value not in control.choices:
                if control.name == "reference_channel_id":
                    raise ValueError("reference channel must be an available channel")
                raise ValueError(f"{control.name} must be one of the offered choices")
        elif control.kind is ControlKind.TEXT:
            if (control.name == "rationale" and not control.required
                    and form.context.get("resume_context", {}).get("schema_version") == "semantic-region-interaction/v2"
                    and isinstance(value, str) and not value.strip()):
                continue
            if not isinstance(value, str) or not value.strip():
                if form.context.get("reason") == "secondary_structure_selection_required":
                    raise ValueError(f"{control.name} is required")
                if form.context.get("reason") == "semantic_region_decision_required":
                    if control.name == "rationale":
                        raise ValueError("Enter the reason for your answer.")
                raise ValueError("classifier reuse decision requires an operator identifier")
            value = value.strip()
        elif control.kind is ControlKind.INTEGER:
            if control.name == "seed" and _is_batch_clustering_form(form):
                if type(value) is not int:
                    raise ValueError("seed must be an integer")
            else:
                value = _nonnegative_int(value, control.name)
        elif control.kind is ControlKind.NUMBER:
            value = decode_control_value(control, value)
        elif control.kind is ControlKind.CHECKBOX:
            if not isinstance(value, bool):
                raise ValueError(f"{control.name} must be a boolean")
        elif control.kind is ControlKind.INTEGER_MAPPING:
            if not isinstance(value, Mapping) or set(value) != set(control.choices):
                raise ValueError("annotation counts must cover all displayed labels")
            value = {
                key: _nonnegative_int(item, f"annotation count for {key}")
                for key, item in value.items()
            }
        elif control.kind is ControlKind.OBJECT_MAPPING:
            if not isinstance(value, Mapping):
                raise ValueError(f"{control.name} must be an object")
            if set(value) != set(control.choices):
                raise ValueError("threshold rules must cover every displayed biomarker")
            value = {key: _mutable_json(item) for key, item in value.items()}
        elif control.kind is ControlKind.OBJECT_SEQUENCE:
            if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
                raise ValueError(f"{control.name} must be an array")
            value = [_mutable_json(item) for item in value]
        elif control.kind is ControlKind.JSON_OBJECT:
            if not isinstance(value, Mapping):
                raise ValueError(f"{control.name} must be an object")
            value = _mutable_json(value)
        elif control.kind is ControlKind.LOCAL_FILE:
            value = _nonblank(value, control.name)
        validated[control.name] = value

    if form.context.get("local_files_only") is True and (
        ("image_index" in validated) != ("series_index" in validated)
    ):
        raise ValueError("image_index and series_index must be supplied together")

    if form.context.get("reason") == InteractionReason.TISSUE_NATIVE_LEVEL_REQUIRED.value:
        index = int(validated["native_resolution_index"])
        validated = {"native_resolution_index": index, "level0_opt_in": index == 0}
    if form.context.get("adapter") == "roi" and _is_batch_clustering_form(form) and action == InteractionAction.CONFIGURE_CLUSTERING.value:
        validated = _batch_clustering_response(form, validated)
    if form.context.get("reason") == InteractionReason.SEMANTIC_REGION_DECISION_REQUIRED.value:
        import getpass

        if action == InteractionAction.SELECT_SEMANTIC_REGION.value:
            candidate = validated.get("candidate_roi_id")
            if candidate is None:
                raise ValueError("Choose a region to review.")
            return {"candidate_roi_id": candidate}
        validated.pop("candidate_roi_id", None)
        validated["reviewer_id"] = "local-account:" + getpass.getuser()
        context = form.context["resume_context"]
        if context.get("schema_version") == "semantic-region-interaction/v2":
            choice = validated.pop("subtype_choice", None)
            if action == InteractionAction.REJECT_SEMANTIC_REGION.value:
                validated.update(subtype_status="not_evaluated", semantic_class_id=None)
            elif not context.get("declared_subtypes"):
                validated.update(subtype_status="not_configured", semantic_class_id=None)
            elif choice == "cannot_assess":
                validated.update(subtype_status="cannot_assess", semantic_class_id=None)
            elif choice is not None:
                validated.update(subtype_status="assigned", semantic_class_id=choice.removeprefix("subtype:"))
            else:
                raise ValueError("Choose a subtype or Cannot assess before accepting the region.")
    return validated


def submit_form(
    form: FormModel,
    action: str | None,
    response: Mapping[str, object],
) -> dict[str, object] | None:
    """

    Checks a standalone form and builds the continuation used by its caller.

    We first check the entered fields. For an image-analysis form, the shared
    validator then adds the selected action to the saved request information.
    Batch submissions use submit_batch_form_values() to send only the choices,
    because their processing step performs that second operation when saving.

    Args:
        form: Current form and the actions it offers.
        action: Selected action, or None when cancelled.
        response: Entered field values after control decoding.

    Returns:
        Validated continuation dictionary, or None after cancellation.

    """

    validated = _validated_response_fields(form, action, response)
    if validated is None:
        return None
    if form.context.get("adapter") == "roi":
        result = StageResult.from_mapping(_mutable_json(form.context["stage_result"]))
        result = replace(result, resume_context=form.context["resume_context"])
        return validate_interaction_response(result, action, validated)
    return {"action": action, "response": validated}


def _is_batch_clustering_form(form: FormModel) -> bool:
    context = form.context or {}
    resume = context.get("resume_context")
    return (
        context.get("adapter") == "roi"
        and context.get("reason") == InteractionReason.CLUSTERING_CONFIGURATION_REQUIRED.value
        and isinstance(resume, Mapping)
        and resume.get("schema_version") == "batch-clustering-setup/v2"
    )


def _batch_clustering_features(context: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    ids = context.get("available_feature_ids")
    features = context.get("available_features")
    if (
        isinstance(ids, (str, bytes))
        or not isinstance(ids, Sequence)
        or isinstance(features, (str, bytes))
        or not isinstance(features, Sequence)
        or not ids
        or len(ids) != len(features)
    ):
        raise ValueError("batch clustering requires an ordered measured feature inventory")
    if any(
        not isinstance(item, Mapping)
        or item.get("kind") != "expression"
        or type(item.get("measurement_name")) is not str
        or not item["measurement_name"]
        for item in features
    ):
        raise ValueError("batch clustering feature inventory is invalid")
    if (
        any(type(item) is not str or not item for item in ids)
        or len(set(ids)) != len(ids)
        or tuple(item.get("feature_id") for item in features) != tuple(ids)
    ):
        raise ValueError("batch clustering feature inventory IDs do not match")
    return tuple(features)


def _batch_clustering_response(form: FormModel, values: Mapping[str, object]) -> dict[str, object]:
    resume = form.context["resume_context"]
    assert isinstance(resume, Mapping)
    features = _batch_clustering_features(resume)
    mode = resume.get("clustering_mode")
    if mode is None:
        mode = values.get("clustering_mode")
    if mode not in {"per_slide", "pooled"}:
        raise ValueError("Choose Pooled or Per slide.")
    selected = values["feature_ids"]
    if (
        not isinstance(selected, list)
        or not selected
        or any(type(item) is not str for item in selected)
        or len(set(selected)) != len(selected)
    ):
        raise ValueError("Select at least one distinct cell measurement")
    offered = {item["feature_id"]: (index, item) for index, item in enumerate(features)}
    if not set(selected) <= set(offered):
        raise ValueError("selected clustering feature was not offered")
    specs = []
    for feature_id in selected:
        index, item = offered[feature_id]
        spec = {
            "feature_id": feature_id,
            "source": item["measurement_name"],
            "kind": item["kind"],
            "channel_id": item["channel_id"],
            "biomarker": item["biomarker"],
            "transform": "robust_scale",
            "weight": values[f"feature_weight_{index}"],
        }
        for name in ("compartment", "units", "producer", "producer_version", "opt_in"):
            if name in item:
                spec[name] = item[name]
        specs.append(spec)
    from mif_pipeline.cell_analysis.clustering_methods import validate_method_seed
    validate_method_seed(values.get("method", "snn_leiden"), values["seed"])
    return {
        "feature_ids": selected,
        "clustering_config": {
            "enabled": True,
            "mode": mode,
            "method": values.get("method", "snn_leiden"),
            "method_settings": ({"grid_size":values["flowsom_grid"], "n_clusters":values["flowsom_clusters"],
                                 "iterations":values["flowsom_iterations"]} if values.get("method")=="flowsom" else {}),
            "feature_specs": specs,
            "k": values["k"],
            "resolution": values["resolution"],
            "seed": values["seed"],
            "exclude_channel_roles": ["nuclear", "autofluorescence", "dapi"],
        },
    }


def _nonblank(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    return value.strip()


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _string_sequence(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError("choices must be a sequence")
    return tuple(_nonblank(item, "choice") for item in value)


def _phenotype_class_rows(value: object) -> tuple[tuple[str, str], ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError("phenotype classes must be a sequence")
    rows: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("phenotype class must be an object")
        rows.append(
            (
                _nonblank(item.get("class_id"), "phenotype class ID"),
                _nonblank(item.get("display_name"), "phenotype display name"),
            )
        )
    return tuple(rows)


def _plot_artifacts(context: Mapping[str, object]) -> tuple[PlotArtifact, ...]:
    artifacts: list[PlotArtifact] = []
    for key in ("cluster_review_artifact", "umap_artifact", "threshold_review_artifact"):
        raw = context.get(key)
        if not isinstance(raw, Mapping):
            continue
        path = raw.get("artifact_path", raw.get("path"))
        if path is None:
            continue
        digest = raw.get("artifact_sha256", raw.get("sha256"))
        artifacts.append(
            PlotArtifact(
                artifact_id=key,
                title=key.removesuffix("_artifact").replace("_", " ").title(),
                path=_nonblank(path, f"{key} path"),
                sha256=digest if isinstance(digest, str) else None,
                status=_nonblank(raw.get("status", "available"), f"{key} status"),
            )
        )
    return tuple(artifacts)


def _semantic_evidence_tables(context: Mapping[str, object]) -> tuple[FormTable, ...]:
    """Project only explicitly supplied semantic evidence fields for review."""
    from .structure_criteria import criterion_review_values
    raw = context.get("semantic_evidence", context.get("evidence_values"))
    if raw is None:
        return ()
    rows_source: Sequence[object]
    if isinstance(raw, Mapping):
        rows_source = (raw,)
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        rows_source = raw
    else:
        raise ValueError("semantic evidence must be an object or array")
    region_assignment = context.get("schema_version") == "semantic-region-interaction/v2"
    groups: dict[str | None, list[FormTableRow]] = {None: []}
    if region_assignment:
        groups.update({name: [] for name in context.get("declared_subtypes", ())})
    rows: list[FormTableRow] = []
    for index, item in enumerate(rows_source):
        if not isinstance(item, Mapping):
            raise ValueError("semantic evidence row must be an object")
        field = item.get("field", item.get("criterion_id", f"evidence:{index}"))
        field = _nonblank(field, "semantic evidence field")
        values = criterion_review_values({**item, "semantic_class_id": None, "subtype_id": None} if region_assignment else item)
        row = FormTableRow(f"semantic-evidence:{index}:{field}", values)
        if region_assignment:
            groups.setdefault(item.get("subtype_id"), []).append(row)
        else:
            rows.append(row)
    if region_assignment:
        return tuple(FormTable(
            f"semantic_evidence:{index}",
            ("measurement", "value", "threshold", "result", "details"), tuple(items),
            title="Measurements to review" if subtype is None else f"Measurements for {subtype}",
        ) for index, (subtype, items) in enumerate(groups.items()) if items)
    return (
        FormTable(
            "semantic_evidence",
            ("criterion", "measured_value", "condition", "result", "details"),
            tuple(rows),
            title="Criterion results",
        ),
    )


def _semantic_scope_tables(context: Mapping[str, object], *,
                           region_review: Mapping[str, object] | None = None) -> tuple[FormTable, ...]:
    fields = (
        ("slide_id", "Image"),
        ("structure_id", "Structure"),
        ("region_label", "Region") if context.get("schema_version") == "semantic-region-interaction/v2"
        else ("roi_id", "Candidate region"),
    )
    rows = tuple(
        FormTableRow(f"semantic-scope:{field}", (label,
            (region_review or {}).get("structure_display_name", context.get("structure_display_name", context[field]))
            if field == "structure_id" else context[field]))
        for field, label in fields
        if field in context
    )
    return () if not rows else (FormTable("semantic_scope", ("field", "value"), rows, title="Region being reviewed"),)


def _v2_context_tables(context: Mapping[str, object]) -> tuple[FormTable, ...]:
    """Show bounded identity and handoff fields common to every V2 action."""
    fields = (
        "schema_version",
        "slide_id",
        "section_id",
        "analysis_domain_id",
        "roi_id",
        "analysis_domain_revision",
        "analysis_domain_revision_sha256",
        "tissue_generation_id",
        "pipeline_run_id",
        "config_sha256",
        "semantic_result_set_sha256",
        "structure_id",
        "maximum",
        "observed_count",
        "inventory_sha256",
        "selection_algorithm",
        "criterion_id",
        "canonical_project_entry_id",
        "saved_selection_path",
        "profile_content_sha256",
        "evidence_snapshot_sha256",
        "geometry_sha256",
    )
    rows = tuple(
        FormTableRow(f"v2-context:{field}", (field, context[field]))
        for field in fields
        if field in context and isinstance(context[field], (str, int, float, bool))
    )
    return () if not rows else (FormTable("v2_context", ("field", "value"), rows),)


def _analysis_domain_assistance_tables(
    context: Mapping[str, object],
) -> tuple[FormTable, ...]:
    raw = context.get("analysis_domain_assistance")
    if raw is None:
        return ()
    if not isinstance(raw, Mapping):
        raise ValueError("analysis-domain assistance must be an object")
    rows = tuple(
        FormTableRow(
            f"analysis-domain-assistance:{key}",
            (
                key,
                (
                    value
                    if isinstance(value, (str, int, float, bool))
                    else json.dumps(_mutable_json(value), sort_keys=True)
                ),
            ),
        )
        for key, value in raw.items()
    )
    return () if not rows else (FormTable("analysis_domain_assistance", ("field", "value"), rows),)


def _inventory_descriptor_tables(
    context: Mapping[str, object],
    *,
    offset: int,
    limit: int,
) -> tuple[FormTable, ...]:
    raw = context.get("inventory_descriptor")
    if raw is None:
        return ()
    if not isinstance(raw, Mapping):
        raise ValueError("inventory descriptor must be an object")
    descriptor = ArtifactDescriptor(
        artifact_id=raw.get("artifact_id"),
        schema_version=raw.get("schema_version"),
        path=raw.get("path"),
        sha256=raw.get("sha256"),
        record_count=raw.get("record_count"),
    )
    table = build_paged_inventory_table(
        descriptor,
        offset,
        limit,
        expected_bindings=context,
    )
    return (
        FormTable(
            "inventory_descriptor",
            ("artifact_id", "schema_version", "path", "sha256", "record_count"),
            (
                FormTableRow(
                    descriptor.artifact_id,
                    (
                        descriptor.artifact_id,
                        descriptor.schema_version,
                        descriptor.path,
                        descriptor.sha256,
                        descriptor.record_count,
                    ),
                ),
            ),
        ),
        table,
    )


def _freeze_json(value: object) -> object:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("form context numbers must be finite")
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("form context keys must be strings")
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(_freeze_json(item) for item in value)
    raise ValueError("form context must contain JSON-like values")


def _mutable_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _mutable_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_mutable_json(item) for item in value]
    return value
