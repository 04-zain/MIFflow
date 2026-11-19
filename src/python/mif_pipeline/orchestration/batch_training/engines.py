"""Pure, injected execution-boundary contracts for training engines.

The `TrainingFitPlanProjection` record carries `classifier_id`, `classifier_kind`,
`classifier_engine`, `training_entries`, `training_analysis_domain_bindings` and its
remaining declared fields.

Training records keep subject roles, reference evidence, model settings and application
scope linked. Their validators and native adapters are called by the shared training service
before a fit, evaluation or accepted publication."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import PurePosixPath
from typing import Protocol, runtime_checkable
from weakref import ReferenceType, ref

from mif_pipeline.pipeline_core import InteractionReason

from ..batch_artifacts import (
    AnalysisDomainBinding,
    BatchCurrentExecutionAuthority,
    BatchNodeExecutionContext,
)
from ..batch_interaction_authority import (
    AuthorizedTrainingInteractionResponse,
    BatchInteractionAuthorityError,
    _validate_authorized_training_interaction_response,
)
from ._shared import (
    BatchTrainingError,
    ClassifierArtifactKind,
    ClassifierKind,
    JsonValue,
    _array,
    _closed_mapping,
    _digest,
    _export_json,
    _integer,
    _json_object,
    _sha256,
    _text,
)
from .evidence import (
    MultiImagePixelFitEvidence,
    MultiImageSupervisedFitEvidence,
    PixelFitEvidenceAuthority,
    SupervisedFitEvidenceAuthority,
    _consume_pixel_fit_evidence_authority,
    _consume_supervised_fit_evidence_authority,
    _revoke_pixel_fit_evidence_authority,
    _revoke_supervised_fit_evidence_authority,
    _PIXEL_FIT_ISSUER,
    _SUPERVISED_FIT_ISSUER,
    _validate_pixel_fit_evidence_authority,
    _validate_supervised_fit_evidence_authority,
)
from .planning import TrainingPlan, TrainingPlanEntry
from .native_projection import (
    QuPathOutputLabels,
    QuPathPixelChannelFeatures,
    QuPathPixelEntryChannelFeatures,
    project_pixel_channel_features,
)
from .qupath_rtrees import (
    QuPathNativeRTreesExecutionProjection,
    QuPathObjectRTreesConfiguration,
    QuPathPixelRTreesConfiguration,
)
from .qupath_kronos import QuPathKronosConfiguration, KRONOS_SCHEMAS, inspect_kronos_readiness
from .cross_validation import (
    QuPathGroupedCVConfiguration,
    QuPathGroupedCVExecutionProjection,
    base_configuration,
    native_estimator_configuration,
)
from .qupath_svm import (
    QuPathNativeSvmExecutionProjection,
    QuPathObjectSvmConfiguration,
    QuPathPixelSvmConfiguration,
)
from .qupath_logistic_regression import (
    QuPathNativeLogisticRegressionExecutionProjection,
    QuPathObjectLogisticRegressionConfiguration,
)
from .qupath_kronos_cell import inspect_kronos_cell_readiness

# ==============================================================================
#                                    Engines
# ==============================================================================


class TrainingExecutionMode(str, Enum):
    INTERACTIVE_NATIVE = "interactive_native"
    PROGRAMMATIC = "programmatic"


class TrainingEngineDisposition(str, Enum):
    COMPLETED = "completed"
    NEEDS_INTERACTION = "needs_interaction"
    FAILED = "failed"
    REJECTED = "rejected"


class TrainingEngineReadinessDisposition(str, Enum):
    READY = "ready"
    NEEDS_INTERACTION = "needs_interaction"
    REJECTED = "rejected"


def _version_pairs(value: object, field: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, (tuple, list)):
        raise BatchTrainingError(f"{field} must contain version pairs")
    pairs: list[tuple[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise BatchTrainingError(f"{field}[{index}] must be a pair of strings")
        pairs.append(
            (_text(item[0], f"{field}[{index}][0]"), _text(item[1], f"{field}[{index}][1]"))
        )
    result = tuple(pairs)
    if result != tuple(sorted(result)) or len({item[0] for item in result}) != len(result):
        raise BatchTrainingError(f"{field} must be canonical and use unique names")
    return result


@dataclass(frozen=True, slots=True)
class TrainingEngineCapabilities:
    engine_id: str
    engine_version: str
    classifier_kinds: tuple[ClassifierKind, ...]
    artifact_kind: ClassifierArtifactKind
    execution_mode: TrainingExecutionMode
    supports_interactive_launch: bool
    supports_headless_training: bool
    can_recover_evidence: bool
    can_report_training_diagnostics: bool
    engine_requirements: Mapping[str, JsonValue]
    engine_requirements_sha256: str
    capabilities_sha256: str

    def __post_init__(self) -> None:
        """Validate the TrainingEngineCapabilities fields when the record is constructed.

        The checks cover ``engine_id``, ``engine_version``, ``classifier_kinds``,
        ``engine_requirements``, ``engine_requirements_sha256``,
        ``supports_interactive_launch``. Invalid field types, values or combinations raise
        the declared validation error before this instance can be used by its caller."""

        engine_id = _text(self.engine_id, "engine_id")
        engine_version = _text(self.engine_version, "engine_version")
        kinds = tuple(self.classifier_kinds)
        if not kinds or not all(isinstance(item, ClassifierKind) for item in kinds):
            raise BatchTrainingError("classifier_kinds must contain classifier kinds")
        if kinds != tuple(sorted(kinds, key=lambda item: item.value)) or len(set(kinds)) != len(
            kinds
        ):
            raise BatchTrainingError("classifier_kinds must be canonical and unique")
        if not isinstance(self.artifact_kind, ClassifierArtifactKind):
            raise BatchTrainingError("artifact_kind must be a ClassifierArtifactKind")
        if not isinstance(self.execution_mode, TrainingExecutionMode):
            raise BatchTrainingError("execution_mode must be a TrainingExecutionMode")
        flags = (
            "supports_interactive_launch",
            "supports_headless_training",
            "can_recover_evidence",
            "can_report_training_diagnostics",
        )
        if any(type(getattr(self, field)) is not bool for field in flags):
            raise BatchTrainingError("engine capability flags must be booleans")
        if self.execution_mode is TrainingExecutionMode.INTERACTIVE_NATIVE and (
            self.artifact_kind is not ClassifierArtifactKind.QUPATH_NATIVE
            or not self.supports_interactive_launch
        ):
            raise BatchTrainingError(
                "interactive_native engines require a QuPath-native artifact and interactive support"
            )
        if (
            self.execution_mode is TrainingExecutionMode.PROGRAMMATIC
            and not self.supports_headless_training
        ):
            raise BatchTrainingError("programmatic engines require headless support")
        requirements = _json_object(self.engine_requirements, "engine_requirements")
        requirements_sha = _sha256(self.engine_requirements_sha256, "engine_requirements_sha256")
        if requirements_sha != _digest(requirements):
            raise BatchTrainingError("engine_requirements_sha256 is not canonical")
        unsigned = {
            "engine_id": engine_id,
            "engine_version": engine_version,
            "classifier_kinds": [item.value for item in kinds],
            "artifact_kind": self.artifact_kind.value,
            "execution_mode": self.execution_mode.value,
            "supports_interactive_launch": self.supports_interactive_launch,
            "supports_headless_training": self.supports_headless_training,
            "can_recover_evidence": self.can_recover_evidence,
            "can_report_training_diagnostics": self.can_report_training_diagnostics,
            "engine_requirements_sha256": requirements_sha,
        }
        capabilities_sha = _sha256(self.capabilities_sha256, "capabilities_sha256")
        if capabilities_sha != _digest(unsigned):
            raise BatchTrainingError("capabilities_sha256 is not canonical")
        object.__setattr__(self, "engine_id", engine_id)
        object.__setattr__(self, "engine_version", engine_version)
        object.__setattr__(self, "classifier_kinds", kinds)
        object.__setattr__(self, "engine_requirements", requirements)
        object.__setattr__(self, "engine_requirements_sha256", requirements_sha)
        object.__setattr__(self, "capabilities_sha256", capabilities_sha)

    @classmethod
    def create(
        cls,
        *,
        engine_id: str,
        engine_version: str,
        classifier_kinds: tuple[ClassifierKind, ...],
        artifact_kind: ClassifierArtifactKind,
        execution_mode: TrainingExecutionMode,
        supports_interactive_launch: bool,
        supports_headless_training: bool,
        can_recover_evidence: bool,
        can_report_training_diagnostics: bool,
        engine_requirements: Mapping[str, object],
    ) -> "TrainingEngineCapabilities":
        try:
            raw_kinds = tuple(classifier_kinds)
        except TypeError as exc:
            raise BatchTrainingError("classifier_kinds must contain classifier kinds") from exc
        if not raw_kinds or not all(isinstance(item, ClassifierKind) for item in raw_kinds):
            raise BatchTrainingError("classifier_kinds must contain classifier kinds")
        if not isinstance(artifact_kind, ClassifierArtifactKind):
            raise BatchTrainingError("artifact_kind must be a ClassifierArtifactKind")
        if not isinstance(execution_mode, TrainingExecutionMode):
            raise BatchTrainingError("execution_mode must be a TrainingExecutionMode")
        requirements = _json_object(engine_requirements, "engine_requirements")
        kinds = tuple(sorted(raw_kinds, key=lambda item: item.value))
        requirements_sha = _digest(requirements)
        unsigned = {
            "engine_id": engine_id,
            "engine_version": engine_version,
            "classifier_kinds": [item.value for item in kinds],
            "artifact_kind": artifact_kind.value,
            "execution_mode": execution_mode.value,
            "supports_interactive_launch": supports_interactive_launch,
            "supports_headless_training": supports_headless_training,
            "can_recover_evidence": can_recover_evidence,
            "can_report_training_diagnostics": can_report_training_diagnostics,
            "engine_requirements_sha256": requirements_sha,
        }
        return cls(
            engine_id,
            engine_version,
            kinds,
            artifact_kind,
            execution_mode,
            supports_interactive_launch,
            supports_headless_training,
            can_recover_evidence,
            can_report_training_diagnostics,
            requirements,
            requirements_sha,
            _digest(unsigned),
        )


@dataclass(frozen=True, slots=True)
class TrainingEngineInteractionRequest:
    fit_plan_sha256: str
    interaction_reason: InteractionReason
    message: str
    allowed_actions: tuple[str, ...]
    resume_context: Mapping[str, JsonValue]
    checkpoint_id: str
    interaction_sha256: str

    def __post_init__(self) -> None:
        fit_plan_sha256 = _sha256(self.fit_plan_sha256, "fit_plan_sha256")
        if not isinstance(self.interaction_reason, InteractionReason):
            raise BatchTrainingError("interaction_reason must be an InteractionReason")
        message = _text(self.message, "message")
        actions = tuple(
            _text(item, f"allowed_actions[{index}]")
            for index, item in enumerate(self.allowed_actions)
        )
        if not actions or actions != tuple(sorted(actions)) or len(set(actions)) != len(actions):
            raise BatchTrainingError("allowed_actions must be non-empty, canonical, and unique")
        resume_context = _json_object(self.resume_context, "resume_context")
        checkpoint = _text(self.checkpoint_id, "checkpoint_id")
        digest = _sha256(self.interaction_sha256, "interaction_sha256")
        if digest != _digest(
            {
                "fit_plan_sha256": fit_plan_sha256,
                "interaction_reason": self.interaction_reason.value,
                "message": message,
                "allowed_actions": list(actions),
                "resume_context": resume_context,
                "checkpoint_id": checkpoint,
            }
        ):
            raise BatchTrainingError("interaction_sha256 is not canonical")
        object.__setattr__(self, "fit_plan_sha256", fit_plan_sha256)
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "allowed_actions", actions)
        object.__setattr__(self, "resume_context", resume_context)
        object.__setattr__(self, "checkpoint_id", checkpoint)
        object.__setattr__(self, "interaction_sha256", digest)


@dataclass(frozen=True, slots=True)
class TrainingEngineReadinessResult:
    """Closed provider answer issued before any project acquisition."""

    fit_plan_sha256: str
    disposition: TrainingEngineReadinessDisposition
    interaction: TrainingEngineInteractionRequest | None
    error_code: str | None
    error_message: str | None

    def __post_init__(self) -> None:
        """Validate the TrainingEngineReadinessResult fields when the record is constructed.

        The checks cover ``fit_plan_sha256``, ``disposition``, ``interaction``,
        ``error_code``, ``error_message``. Invalid field types, values or combinations raise
        the declared validation error before this instance can be used by its caller."""

        fit_plan_sha256 = _sha256(self.fit_plan_sha256, "fit_plan_sha256")
        if not isinstance(self.disposition, TrainingEngineReadinessDisposition):
            raise BatchTrainingError("disposition must be a TrainingEngineReadinessDisposition")
        if (
            self.interaction is not None
            and type(self.interaction) is not TrainingEngineInteractionRequest
        ):
            raise BatchTrainingError("interaction must be a TrainingEngineInteractionRequest")
        error_code = None if self.error_code is None else _text(self.error_code, "error_code")
        error_message = (
            None if self.error_message is None else _text(self.error_message, "error_message")
        )
        if (error_code is not None and len(error_code) > 128) or (
            error_message is not None and len(error_message) > 2048
        ):
            raise BatchTrainingError("readiness rejection code and message must be bounded")
        if self.disposition is TrainingEngineReadinessDisposition.READY:
            if self.interaction is not None or error_code is not None or error_message is not None:
                raise BatchTrainingError("ready result carries no interaction or error")
        elif self.disposition is TrainingEngineReadinessDisposition.NEEDS_INTERACTION:
            if self.interaction is None or error_code is not None or error_message is not None:
                raise BatchTrainingError("needs_interaction result requires exactly an interaction")
            if self.interaction.fit_plan_sha256 != fit_plan_sha256:
                raise BatchTrainingError("readiness interaction fit plan differs from result")
        elif self.interaction is not None or error_code is None or error_message is None:
            raise BatchTrainingError("rejected result requires exactly an error")
        object.__setattr__(self, "fit_plan_sha256", fit_plan_sha256)
        object.__setattr__(self, "error_code", error_code)
        object.__setattr__(self, "error_message", error_message)


@dataclass(frozen=True, slots=True)
class TrainingFitPlanProjection:
    """Canonical fit-only plan facts safe to expose to a trainer."""

    classifier_id: str
    classifier_kind: ClassifierKind
    classifier_engine: str
    training_entries: tuple[TrainingPlanEntry, ...]
    training_analysis_domain_bindings: tuple[AnalysisDomainBinding, ...]
    required_classes: tuple[str, ...]
    eligible_parent_classes: tuple[str, ...]
    channel_requirements: Mapping[str, JsonValue] | None
    feature_requirements: Mapping[str, JsonValue] | None
    model_configuration: Mapping[str, JsonValue]
    random_seed: int
    fit_plan_sha256: str

    def __post_init__(self) -> None:
        """Validate the TrainingFitPlanProjection fields when the record is constructed.

        The checks cover ``classifier_id``, ``classifier_engine``, ``training_entries``,
        ``training_analysis_domain_bindings``, ``classifier_kind``, ``model_configuration``.
        Invalid field types, values or combinations raise the declared validation error
        before this instance can be used by its caller."""

        classifier_id = _text(self.classifier_id, "classifier_id")
        if not isinstance(self.classifier_kind, ClassifierKind):
            raise BatchTrainingError("classifier_kind must be a ClassifierKind")
        classifier_engine = _text(self.classifier_engine, "classifier_engine")
        entries = tuple(self.training_entries)
        if not entries or not all(type(item) is TrainingPlanEntry for item in entries):
            raise BatchTrainingError("training_entries must contain exact plan entries")
        if any(item.split != "training" for item in entries):
            raise BatchTrainingError("training_entries cannot contain held-out roles")
        if entries != tuple(sorted(entries, key=lambda item: (item.project_entry_id, item.split))):
            raise BatchTrainingError("training_entries must use canonical order")
        if len({(item.project_entry_id, item.split) for item in entries}) != len(entries):
            raise BatchTrainingError("training_entries must be unique")
        bindings = tuple(self.training_analysis_domain_bindings)
        if not bindings or not all(type(item) is AnalysisDomainBinding for item in bindings):
            raise BatchTrainingError(
                "training_analysis_domain_bindings must contain exact bindings"
            )
        if bindings != tuple(
            sorted(
                bindings,
                key=lambda item: (
                    item.sample_id,
                    item.slide_id,
                    item.analysis_domain_id,
                    item.analysis_domain_revision,
                    item.project_entry_id,
                ),
            )
        ):
            raise BatchTrainingError("training_analysis_domain_bindings must use canonical order")
        training_entry_ids = {item.project_entry_id for item in entries}
        if {item.project_entry_id for item in bindings} != training_entry_ids:
            raise BatchTrainingError(
                "training bindings do not provide exact training-entry coverage"
            )
        required = tuple(
            _text(item, f"required_classes[{index}]")
            for index, item in enumerate(self.required_classes)
        )
        parents = tuple(
            _text(item, f"eligible_parent_classes[{index}]")
            for index, item in enumerate(self.eligible_parent_classes)
        )
        if (
            not required
            or required != tuple(sorted(required))
            or len(set(required)) != len(required)
            or not parents
            or parents != tuple(sorted(parents))
            or len(set(parents)) != len(parents)
        ):
            raise BatchTrainingError(
                "required and eligible classes must be nonempty, canonical, and unique"
            )
        channels = (
            None
            if self.channel_requirements is None
            else _json_object(self.channel_requirements, "channel_requirements")
        )
        features = (
            None
            if self.feature_requirements is None
            else _json_object(self.feature_requirements, "feature_requirements")
        )
        if self.classifier_kind is ClassifierKind.SUPERVISED_PHENOTYPE:
            if features is None or channels is not None:
                raise BatchTrainingError(
                    "supervised fit plan requires features and forbids channels"
                )
        elif channels is None or features is not None:
            raise BatchTrainingError("pixel fit plan requires channels and forbids features")
        configuration = _json_object(self.model_configuration, "model_configuration")
        seed = _integer(self.random_seed, "random_seed")
        fit_sha = _sha256(self.fit_plan_sha256, "fit_plan_sha256")
        object.__setattr__(self, "classifier_id", classifier_id)
        object.__setattr__(self, "classifier_engine", classifier_engine)
        object.__setattr__(self, "training_entries", entries)
        object.__setattr__(self, "training_analysis_domain_bindings", bindings)
        object.__setattr__(self, "required_classes", required)
        object.__setattr__(self, "eligible_parent_classes", parents)
        object.__setattr__(self, "channel_requirements", channels)
        object.__setattr__(self, "feature_requirements", features)
        object.__setattr__(self, "model_configuration", configuration)
        object.__setattr__(self, "random_seed", seed)
        if fit_sha != _digest(self._unsigned_mapping()):
            raise BatchTrainingError("fit_plan_sha256 is not canonical")
        object.__setattr__(self, "fit_plan_sha256", fit_sha)

    @classmethod
    def create(cls, plan: TrainingPlan) -> "TrainingFitPlanProjection":
        if type(plan) is not TrainingPlan:
            raise BatchTrainingError("plan must be an exact TrainingPlan")
        request = plan.request
        entries = tuple(item for item in request.entries if item.split == "training")
        entry_ids = {item.project_entry_id for item in entries}
        bindings = tuple(
            item for item in request.analysis_domain_bindings if item.project_entry_id in entry_ids
        )
        unsigned = {
            "classifier_id": request.classifier_id,
            "classifier_kind": request.classifier_kind,
            "classifier_engine": request.classifier_engine,
            "training_entries": entries,
            "training_analysis_domain_bindings": bindings,
            "required_classes": request.required_classes,
            "eligible_parent_classes": request.eligible_parent_classes,
            "channel_requirements": request.channel_requirements,
            "feature_requirements": request.feature_requirements,
            "model_configuration": request.model_configuration,
            "random_seed": request.random_seed,
        }
        prototype = object.__new__(cls)
        for name, value in unsigned.items():
            object.__setattr__(prototype, name, value)
        return cls(**unsigned, fit_plan_sha256=_digest(prototype._unsigned_mapping()))

    @classmethod
    def from_mapping(cls, value: object) -> "TrainingFitPlanProjection":
        """Decode the serialized fields into a validated TrainingFitPlanProjection record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""

        mapping = _closed_mapping(
            value,
            fields=frozenset(cls.__dataclass_fields__),
            field="training_fit_plan_projection",
        )
        try:
            kind = ClassifierKind(mapping["classifier_kind"])
        except (TypeError, ValueError) as exc:
            raise BatchTrainingError("classifier_kind is invalid") from exc
        return cls(
            classifier_id=mapping["classifier_id"],  # type: ignore[arg-type]
            classifier_kind=kind,
            classifier_engine=mapping["classifier_engine"],  # type: ignore[arg-type]
            training_entries=tuple(
                TrainingPlanEntry.from_mapping(item)
                for item in _array(mapping["training_entries"], "training_entries")
            ),
            training_analysis_domain_bindings=tuple(
                AnalysisDomainBinding.from_mapping(item)
                for item in _array(
                    mapping["training_analysis_domain_bindings"],
                    "training_analysis_domain_bindings",
                )
            ),
            required_classes=tuple(
                _array(mapping["required_classes"], "required_classes")
            ),  # type: ignore[arg-type]
            eligible_parent_classes=tuple(
                _array(
                    mapping["eligible_parent_classes"],
                    "eligible_parent_classes",
                )
            ),  # type: ignore[arg-type]
            channel_requirements=mapping["channel_requirements"],  # type: ignore[arg-type]
            feature_requirements=mapping["feature_requirements"],  # type: ignore[arg-type]
            model_configuration=mapping["model_configuration"],  # type: ignore[arg-type]
            random_seed=mapping["random_seed"],  # type: ignore[arg-type]
            fit_plan_sha256=mapping["fit_plan_sha256"],  # type: ignore[arg-type]
        )

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            "classifier_id": self.classifier_id,
            "classifier_kind": self.classifier_kind.value,
            "classifier_engine": self.classifier_engine,
            "training_entries": [item.to_mapping() for item in self.training_entries],
            "training_analysis_domain_bindings": [
                item.to_mapping() for item in self.training_analysis_domain_bindings
            ],
            "required_classes": list(self.required_classes),
            "eligible_parent_classes": list(self.eligible_parent_classes),
            "channel_requirements": _export_json(self.channel_requirements),
            "feature_requirements": _export_json(self.feature_requirements),
            "model_configuration": _export_json(self.model_configuration),
            "random_seed": self.random_seed,
        }

    def to_mapping(self) -> dict[str, object]:
        return {**self._unsigned_mapping(), "fit_plan_sha256": self.fit_plan_sha256}


def _native_material_parent_type_ids(
    classifier_kind: ClassifierKind,
    eligible_parent_classes: tuple[str, ...],
) -> tuple[str, ...]:
    """Project evidence scope without changing the accepted plan vocabulary.

    Primary plans declare tissue eligibility while their references belong to
    accepted analysis domains. Secondary IDs name their actual material types;
    object fitting uses a separate cell-evidence contract.
    """
    if classifier_kind is ClassifierKind.PRIMARY_COMPARTMENT:
        if eligible_parent_classes != ("Tissue",):
            raise BatchTrainingError("primary fit eligibility must be Tissue")
        return ("AnalysisDomain",)
    if classifier_kind is ClassifierKind.SECONDARY_STRUCTURE:
        return eligible_parent_classes
    if classifier_kind is ClassifierKind.SUPERVISED_PHENOTYPE:
        if eligible_parent_classes != ("Cell",):
            raise BatchTrainingError("supervised fit eligibility must be Cell")
        return ()
    raise BatchTrainingError("native training classifier role is unsupported")


def _parse_native_model_configuration(
    value: object, kind: ClassifierKind
) -> (
    QuPathPixelRTreesConfiguration
    | QuPathObjectRTreesConfiguration
    | QuPathPixelSvmConfiguration
    | QuPathObjectSvmConfiguration
    | QuPathObjectLogisticRegressionConfiguration
    | QuPathKronosConfiguration
    | QuPathGroupedCVConfiguration
):
    if not isinstance(value, Mapping):
        raise BatchTrainingError("native model configuration must be an object")
    is_object = kind is ClassifierKind.SUPERVISED_PHENOTYPE
    parsers = {
        "qupath-pixel-rtrees/v1": QuPathPixelRTreesConfiguration,
        "qupath-object-rtrees/v1": QuPathObjectRTreesConfiguration,
        "qupath-pixel-svm/v1": QuPathPixelSvmConfiguration,
        "qupath-object-svm/v1": QuPathObjectSvmConfiguration,
        "qupath-object-logistic-regression/v1": QuPathObjectLogisticRegressionConfiguration,
        "qupath-kronos-rtrees/v2": QuPathKronosConfiguration,
        "qupath-kronos-svm/v2": QuPathKronosConfiguration,
        "qupath-grouped-cross-validation/v1": QuPathGroupedCVConfiguration,
    }
    schema = value.get("configuration_schema")
    parser = parsers.get(schema) if isinstance(schema, str) else None
    if parser is None:
        raise BatchTrainingError("native configuration schema is unsupported for classifier kind")
    configuration = parser.from_mapping(value)
    estimator = native_estimator_configuration(configuration)
    if (type(estimator) in (
        QuPathObjectRTreesConfiguration,
        QuPathObjectSvmConfiguration,
        QuPathObjectLogisticRegressionConfiguration,
    )) != is_object:
        raise BatchTrainingError("native configuration schema is unsupported for classifier kind")
    return configuration


@dataclass(frozen=True, slots=True)
class QuPathNativeTrainingFitPlanProjection:
    """Closed native request facts projected before provider or mutation access."""

    fit_plan: TrainingFitPlanProjection
    pixel_channel_features: QuPathPixelChannelFeatures | None
    output_labels: QuPathOutputLabels
    model_configuration: (
        QuPathPixelRTreesConfiguration
        | QuPathObjectRTreesConfiguration
        | QuPathPixelSvmConfiguration
        | QuPathObjectSvmConfiguration
        | QuPathObjectLogisticRegressionConfiguration
        | QuPathKronosConfiguration
        | QuPathGroupedCVConfiguration
    )
    native_execution: (
        QuPathNativeRTreesExecutionProjection | QuPathNativeSvmExecutionProjection
        | QuPathNativeLogisticRegressionExecutionProjection | QuPathGroupedCVExecutionProjection
    )
    native_fit_plan_sha256: str

    @staticmethod
    def _required_pixel_channel_ids(
        channel_requirements: object,
    ) -> frozenset[str]:
        requirements = _closed_mapping(
            channel_requirements,
            fields=frozenset({"channel_roles"}),
            field="pixel_channel_requirements",
        )
        roles = requirements["channel_roles"]
        if not isinstance(roles, Mapping) or not roles:
            raise BatchTrainingError(
                "pixel channel_requirements.channel_roles must be a nonempty object"
            )
        canonical_ids: list[str] = []
        for role, canonical_id in roles.items():
            _text(role, "pixel channel role")
            canonical_ids.append(_text(canonical_id, f"pixel channel role {role}"))
        if len(set(canonical_ids)) != len(canonical_ids):
            raise BatchTrainingError(
                "pixel channel roles must use unique canonical channel identities"
            )
        return frozenset(canonical_ids)

    def __post_init__(self) -> None:
        """Validate the QuPathNativeTrainingFitPlanProjection fields when the record is
        constructed.

        The checks cover ``model_configuration``, ``output_labels``,
        ``native_fit_plan_sha256``, ``fit_plan``, ``native_execution``,
        ``pixel_channel_features``. Invalid field types, values or combinations raise the
        declared validation error before this instance can be used by its caller."""

        if type(self.fit_plan) is not TrainingFitPlanProjection:
            raise BatchTrainingError("fit_plan must be an exact fit projection")
        full_configuration = self.model_configuration
        configuration = native_estimator_configuration(full_configuration)
        if type(configuration) not in (
            QuPathPixelRTreesConfiguration,
            QuPathObjectRTreesConfiguration,
            QuPathPixelSvmConfiguration,
            QuPathObjectSvmConfiguration,
            QuPathObjectLogisticRegressionConfiguration,
        ):
            raise BatchTrainingError("native model configuration has an unsupported concrete type")
        schema_engine = {
            "qupath-pixel-rtrees/v1": "qupath-pixel-classifier",
            "qupath-object-rtrees/v1": "qupath-object-classifier",
            "qupath-pixel-svm/v1": "qupath-pixel-svm-classifier",
            "qupath-object-svm/v1": "qupath-object-svm-classifier",
            "qupath-object-logistic-regression/v1": "qupath-object-logistic-regression-classifier",
        }
        if (
            full_configuration.engine_id
            if type(full_configuration) in {QuPathKronosConfiguration, QuPathGroupedCVConfiguration}
            else schema_engine.get(configuration.configuration_schema)
        ) != self.fit_plan.classifier_engine:
            raise BatchTrainingError("native configuration schema differs from classifier engine")
        if self.fit_plan.classifier_kind is ClassifierKind.SUPERVISED_PHENOTYPE:
            if (
                type(configuration)
                not in (QuPathObjectRTreesConfiguration, QuPathObjectSvmConfiguration,
                        QuPathObjectLogisticRegressionConfiguration)
                or self.pixel_channel_features is not None
            ):
                raise BatchTrainingError(
                    "supervised native fit requires object configuration and no channels"
                )
        elif (
            type(configuration) not in (QuPathPixelRTreesConfiguration, QuPathPixelSvmConfiguration)
            or type(self.pixel_channel_features) is not QuPathPixelChannelFeatures
        ):
            raise BatchTrainingError(
                "pixel native fit requires pixel configuration and channel features"
            )
        if type(configuration) in (QuPathPixelRTreesConfiguration, QuPathPixelSvmConfiguration):
            required_channel_ids = self._required_pixel_channel_ids(
                self.fit_plan.channel_requirements
            )
            observed_channel_ids = frozenset(
                item.canonical_channel_id for item in self.pixel_channel_features.ordered_channels
            )
            if observed_channel_ids != required_channel_ids:
                raise BatchTrainingError(
                    "native channel identities differ from the fit plan channel roles"
                )
        if full_configuration.to_mapping() != _export_json(self.fit_plan.model_configuration):
            raise BatchTrainingError("native model configuration differs from the fit plan")
        if configuration.output_labels != self.output_labels:
            raise BatchTrainingError("native output labels differ from the model configuration")
        if type(full_configuration) is QuPathGroupedCVConfiguration:
            if self.native_execution != QuPathGroupedCVExecutionProjection.create(
                full_configuration, self.fit_plan.random_seed
            ):
                raise BatchTrainingError(
                    "Native grouped search execution differs from the accepted candidate set"
                )
        elif type(configuration) in (
            QuPathPixelRTreesConfiguration,
            QuPathObjectRTreesConfiguration,
        ):
            if (
                type(self.native_execution) is not QuPathNativeRTreesExecutionProjection
                or self.native_execution.random_seed != self.fit_plan.random_seed
                or self.native_execution
                != QuPathNativeRTreesExecutionProjection.create(
                    parameters=native_estimator_configuration(configuration).rtrees,
                    random_seed=self.fit_plan.random_seed,
                )
            ):
                raise BatchTrainingError(
                    "native execution changes accepted seed or RTrees controls"
                )
        elif type(configuration) is QuPathObjectLogisticRegressionConfiguration:
            if (
                type(self.native_execution) is not QuPathNativeLogisticRegressionExecutionProjection
                or self.native_execution != QuPathNativeLogisticRegressionExecutionProjection.create(
                    parameters=configuration.logistic_regression,
                    random_seed=self.fit_plan.random_seed,
                )
            ):
                raise BatchTrainingError("native execution changes accepted seed or logistic controls")
        elif (
            type(self.native_execution) is not QuPathNativeSvmExecutionProjection
            or self.native_execution.random_seed != self.fit_plan.random_seed
            or self.native_execution.parameters != configuration.svm
            or self.native_execution
            != QuPathNativeSvmExecutionProjection.create(
                parameters=native_estimator_configuration(configuration).svm,
                random_seed=self.fit_plan.random_seed,
            )
        ):
            raise BatchTrainingError("native execution changes accepted seed or SVM controls")
        base_model = base_configuration(full_configuration)
        if type(base_model) is QuPathKronosConfiguration:
            observed = tuple(
                row.canonical_channel_id for row in self.pixel_channel_features.ordered_channels
            )
            if base_model.encoder.channel_ids != observed:
                raise BatchTrainingError("KRONOS channel order differs from the native observation")
        self._validate_output_labels()
        digest = _sha256(self.native_fit_plan_sha256, "native_fit_plan_sha256")
        if digest != _digest(self._unsigned_mapping()):
            raise BatchTrainingError("native_fit_plan_sha256 is not canonical")
        object.__setattr__(self, "native_fit_plan_sha256", digest)

    def _validate_output_labels(self) -> None:
        configuration = native_estimator_configuration(self.model_configuration)
        required = set(self.fit_plan.required_classes)
        labels = self.output_labels.ordered_labels
        if type(configuration) in (QuPathPixelRTreesConfiguration, QuPathPixelSvmConfiguration):
            if configuration.boundary_strategy == "classify":
                boundary_id = configuration.boundary_class_id
                boundary_rows = tuple(item for item in labels if item.qupath_label == boundary_id)
                if boundary_id is None or len(boundary_rows) != 1:
                    raise BatchTrainingError(
                        "boundary_class_id is absent from the native output label map"
                    )
                boundary_class = boundary_rows[0].canonical_class_id
                if boundary_class in required:
                    raise BatchTrainingError(
                        "boundary_class_id collides with a required output class"
                    )
                ordinary = {
                    item.canonical_class_id for item in labels if item.qupath_label != boundary_id
                }
                if ordinary != required or len(labels) != len(required) + 1:
                    raise BatchTrainingError(
                        "classify output labels must contain exact required classes and one boundary class"
                    )
            elif {item.canonical_class_id for item in labels} != required:
                raise BatchTrainingError(
                    "skip output labels must contain exactly the required classes"
                )
        elif {item.canonical_class_id for item in labels} != required:
            raise BatchTrainingError(
                "object output labels must contain exactly the required classes"
            )

    @classmethod
    def create(
        cls,
        *,
        fit_plan: TrainingFitPlanProjection,
        pixel_entry_channel_features: tuple[QuPathPixelEntryChannelFeatures, ...] = (),
    ) -> "QuPathNativeTrainingFitPlanProjection":
        if type(fit_plan) is not TrainingFitPlanProjection:
            raise BatchTrainingError("fit_plan must be an exact fit projection")
        if fit_plan.classifier_kind is ClassifierKind.SUPERVISED_PHENOTYPE:
            if pixel_entry_channel_features:
                raise BatchTrainingError(
                    "supervised native fit cannot carry pixel channel mappings"
                )
            configuration = _parse_native_model_configuration(
                fit_plan.model_configuration, fit_plan.classifier_kind
            )
            channel_features = None
        else:
            configuration = _parse_native_model_configuration(
                fit_plan.model_configuration, fit_plan.classifier_kind
            )
            channel_features = project_pixel_channel_features(
                training_entry_ids=tuple(
                    item.project_entry_id for item in fit_plan.training_entries
                ),
                entry_features=pixel_entry_channel_features,
            )
            required_channel_ids = cls._required_pixel_channel_ids(fit_plan.channel_requirements)
            observed_channel_ids = frozenset(
                item.canonical_channel_id for item in channel_features.ordered_channels
            )
            if observed_channel_ids != required_channel_ids:
                raise BatchTrainingError(
                    "entry channel identities differ from the fit plan channel roles"
                )
        output_labels = configuration.output_labels
        native_execution = (
            QuPathGroupedCVExecutionProjection.create(configuration, fit_plan.random_seed)
            if type(configuration) is QuPathGroupedCVConfiguration
            else (
                QuPathNativeSvmExecutionProjection.create(
                    parameters=native_estimator_configuration(configuration).svm,
                    random_seed=fit_plan.random_seed,
                )
                if type(native_estimator_configuration(configuration))
                in (QuPathPixelSvmConfiguration, QuPathObjectSvmConfiguration)
                else (
                    QuPathNativeLogisticRegressionExecutionProjection.create(
                        parameters=native_estimator_configuration(configuration).logistic_regression,
                        random_seed=fit_plan.random_seed,
                    )
                    if type(native_estimator_configuration(configuration))
                    is QuPathObjectLogisticRegressionConfiguration
                    else QuPathNativeRTreesExecutionProjection.create(
                        parameters=native_estimator_configuration(configuration).rtrees,
                        random_seed=fit_plan.random_seed,
                    )
                )
            )
        )
        unsigned = {
            "fit_plan": fit_plan.to_mapping(),
            "pixel_channel_features": (
                None if channel_features is None else channel_features.to_mapping()
            ),
            "output_labels": output_labels.to_mapping(),
            "model_configuration": configuration.to_mapping(),
            "native_execution": native_execution.to_mapping(),
        }
        return cls(
            fit_plan=fit_plan,
            pixel_channel_features=channel_features,
            output_labels=output_labels,
            model_configuration=configuration,
            native_execution=native_execution,
            native_fit_plan_sha256=_digest(unsigned),
        )

    @classmethod
    def from_mapping(cls, value: object) -> "QuPathNativeTrainingFitPlanProjection":
        mapping = _closed_mapping(
            value,
            fields=frozenset(cls.__dataclass_fields__),
            field="qupath_native_training_fit_plan_projection",
        )
        fit_plan = TrainingFitPlanProjection.from_mapping(mapping["fit_plan"])
        configuration = _parse_native_model_configuration(
            mapping["model_configuration"], fit_plan.classifier_kind
        )
        return cls(
            fit_plan=fit_plan,
            pixel_channel_features=(
                None
                if mapping["pixel_channel_features"] is None
                else QuPathPixelChannelFeatures.from_mapping(mapping["pixel_channel_features"])
            ),
            output_labels=QuPathOutputLabels.from_mapping(mapping["output_labels"]),
            model_configuration=configuration,
            native_execution=(
                QuPathGroupedCVExecutionProjection.from_mapping(mapping["native_execution"])
                if type(configuration) is QuPathGroupedCVConfiguration
                else (
                    QuPathNativeSvmExecutionProjection.from_mapping(mapping["native_execution"])
                    if type(native_estimator_configuration(configuration))
                    in (QuPathPixelSvmConfiguration, QuPathObjectSvmConfiguration)
                    else (
                        QuPathNativeLogisticRegressionExecutionProjection.from_mapping(
                            mapping["native_execution"]
                        )
                        if type(native_estimator_configuration(configuration))
                        is QuPathObjectLogisticRegressionConfiguration
                        else QuPathNativeRTreesExecutionProjection.from_mapping(
                            mapping["native_execution"]
                        )
                    )
                )
            ),
            native_fit_plan_sha256=mapping["native_fit_plan_sha256"],  # type: ignore[arg-type]
        )

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            "fit_plan": self.fit_plan.to_mapping(),
            "pixel_channel_features": (
                None
                if self.pixel_channel_features is None
                else self.pixel_channel_features.to_mapping()
            ),
            "output_labels": self.output_labels.to_mapping(),
            "model_configuration": self.model_configuration.to_mapping(),
            "native_execution": self.native_execution.to_mapping(),
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            **self._unsigned_mapping(),
            "native_fit_plan_sha256": self.native_fit_plan_sha256,
        }


def _native_object_feature_spec(
    evidence: MultiImageSupervisedFitEvidence,
    configuration: (
        QuPathObjectRTreesConfiguration | QuPathObjectSvmConfiguration
        | QuPathObjectLogisticRegressionConfiguration
    ),
) -> dict[str, object]:
    from .deferred_cell_evidence import pending_marker_names, validate_deferred_evidence
    from .supervised_features import require_kronos_cell_schema
    source = configuration.feature_source
    first = evidence.entries[0]
    if source is not None:
        require_kronos_cell_schema(source, [item.to_mapping() for item in first.feature_schema])
    for snapshot in evidence.entries:
        if snapshot.deferred_feature_evidence is not None:
            validate_deferred_evidence(snapshot.deferred_feature_evidence,
                (cell.cell_id for cell in snapshot.eligible_cells),
                source=source, require_pending=True)
        elif pending_marker_names(source):
            raise BatchTrainingError("Pending KRONOS statistics require deferred cell evidence")
    return {
        "schema": "qupath-object-feature-spec/v1",
        "ordered_measurements": [item.to_mapping() for item in first.feature_schema],
        "normalization": configuration.normalization,
        "pca_retained_variance": configuration.pca_retained_variance,
        **({"pca_normalize": False} if configuration.pca_retained_variance > 0 and not configuration.pca_normalize else {}),
    }


def _native_object_training_entry(snapshot: object) -> dict[str, object]:
    mapped_by_cell: dict[str, list[object]] = {}
    for item in snapshot.mapped_evidence:
        mapped_by_cell.setdefault(item.cell_id, []).append(item)
    names = tuple(item.measurement_name for item in snapshot.feature_schema)
    training_cells: list[dict[str, object]] = []
    for cell in snapshot.eligible_cells:
        labels = {item.class_id for item in mapped_by_cell[cell.cell_id]}
        if len(labels) != 1:
            raise BatchTrainingError("supervised native training cell has ambiguous class evidence")
        values = (None if snapshot.deferred_feature_evidence is not None else
                  [float(cell.marker_measurements[name]) for name in names])
        training_cells.append(
            {
                "cell_id": cell.cell_id,
                "expected_class_id": labels.pop(),
                "analysis_domain_id": cell.analysis_domain_id,
                "slide_id": cell.slide_id,
                "section_id": cell.section_id,
                "measurement_values": values,
                "measurement_vector_sha256": None if values is None else _digest(values),
            }
        )
    return {
        "entry_kind": "object",
        "snapshot": snapshot.to_mapping(),
        "training_cells": training_cells,
    }


@dataclass(frozen=True, slots=True)
class RawNativeClassifierArtifact:
    """Untrusted observed QuPath-native classifier output from a fitter."""

    classifier_id: str
    classifier_version: str
    classifier_kind: ClassifierKind
    native_classifier_type: str
    project_relative_path: str
    observed_sha256: str
    observed_byte_count: int
    source_project_identity_sha256: str
    qupath_version: str
    fit_observation_sha256: str

    def __post_init__(self) -> None:
        """Validate the RawNativeClassifierArtifact fields when the record is constructed.

        The checks cover ``classifier_id``, ``classifier_version``,
        ``native_classifier_type``, ``project_relative_path``, ``observed_sha256``,
        ``observed_byte_count``. Invalid field types, values or combinations raise the
        declared validation error before this instance can be used by its caller."""

        classifier_id = _text(self.classifier_id, "classifier_id")
        classifier_version = _text(self.classifier_version, "classifier_version")
        if not isinstance(self.classifier_kind, ClassifierKind):
            raise BatchTrainingError("classifier_kind must be a ClassifierKind")
        native_type = _text(self.native_classifier_type, "native_classifier_type")
        expected_native_type = (
            "object" if self.classifier_kind is ClassifierKind.SUPERVISED_PHENOTYPE else "pixel"
        )
        if native_type != expected_native_type:
            raise BatchTrainingError("native_classifier_type differs from the classifier family")
        locator = _text(self.project_relative_path, "project_relative_path")
        path = PurePosixPath(locator)
        expected_directory = (
            "object_classifiers" if native_type == "object" else "pixel_classifiers"
        )
        if (
            locator != path.as_posix()
            or path.is_absolute()
            or len(path.parts) != 3
            or path.parts[:2] != ("classifiers", expected_directory)
            or path.name in {"", ".", ".."}
            or path.suffix != ".json"
        ):
            raise BatchTrainingError(
                "project_relative_path is not a canonical family classifier locator"
            )
        observed_sha = _sha256(self.observed_sha256, "observed_sha256")
        observed_count = _integer(self.observed_byte_count, "observed_byte_count")
        if observed_count == 0:
            raise BatchTrainingError("observed_byte_count must be positive")
        project_sha = _sha256(
            self.source_project_identity_sha256,
            "source_project_identity_sha256",
        )
        qupath_version = _text(self.qupath_version, "qupath_version")
        fit_sha = _sha256(self.fit_observation_sha256, "fit_observation_sha256")
        object.__setattr__(self, "classifier_id", classifier_id)
        object.__setattr__(self, "classifier_version", classifier_version)
        object.__setattr__(self, "native_classifier_type", native_type)
        object.__setattr__(self, "project_relative_path", locator)
        object.__setattr__(self, "observed_sha256", observed_sha)
        object.__setattr__(self, "observed_byte_count", observed_count)
        object.__setattr__(self, "source_project_identity_sha256", project_sha)
        object.__setattr__(self, "qupath_version", qupath_version)
        if fit_sha != _digest(self._unsigned_mapping()):
            raise BatchTrainingError("fit_observation_sha256 is not canonical")
        object.__setattr__(self, "fit_observation_sha256", fit_sha)

    @classmethod
    def create(
        cls,
        *,
        classifier_id: str,
        classifier_version: str,
        classifier_kind: ClassifierKind,
        native_classifier_type: str,
        project_relative_path: str,
        observed_sha256: str,
        observed_byte_count: int,
        source_project_identity_sha256: str,
        qupath_version: str,
    ) -> "RawNativeClassifierArtifact":
        if not isinstance(classifier_kind, ClassifierKind):
            raise BatchTrainingError("classifier_kind must be a ClassifierKind")
        unsigned = {
            "classifier_id": classifier_id,
            "classifier_version": classifier_version,
            "classifier_kind": classifier_kind,
            "native_classifier_type": native_classifier_type,
            "project_relative_path": project_relative_path,
            "observed_sha256": observed_sha256,
            "observed_byte_count": observed_byte_count,
            "source_project_identity_sha256": source_project_identity_sha256,
            "qupath_version": qupath_version,
        }
        prototype = object.__new__(cls)
        for name, value in unsigned.items():
            object.__setattr__(prototype, name, value)
        return cls(
            **unsigned,
            fit_observation_sha256=_digest(prototype._unsigned_mapping()),
        )

    @classmethod
    def from_mapping(cls, value: object) -> "RawNativeClassifierArtifact":
        mapping = _closed_mapping(
            value,
            fields=frozenset(cls.__dataclass_fields__),
            field="raw_native_classifier_artifact",
        )
        try:
            kind = ClassifierKind(mapping["classifier_kind"])
        except (TypeError, ValueError) as exc:
            raise BatchTrainingError("classifier_kind is invalid") from exc
        return cls(
            classifier_id=mapping["classifier_id"],  # type: ignore[arg-type]
            classifier_version=mapping["classifier_version"],  # type: ignore[arg-type]
            classifier_kind=kind,
            native_classifier_type=mapping["native_classifier_type"],  # type: ignore[arg-type]
            project_relative_path=mapping["project_relative_path"],  # type: ignore[arg-type]
            observed_sha256=mapping["observed_sha256"],  # type: ignore[arg-type]
            observed_byte_count=mapping["observed_byte_count"],  # type: ignore[arg-type]
            source_project_identity_sha256=mapping[
                "source_project_identity_sha256"
            ],  # type: ignore[arg-type]
            qupath_version=mapping["qupath_version"],  # type: ignore[arg-type]
            fit_observation_sha256=mapping["fit_observation_sha256"],  # type: ignore[arg-type]
        )

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            "classifier_id": self.classifier_id,
            "classifier_version": self.classifier_version,
            "classifier_kind": self.classifier_kind.value,
            "native_classifier_type": self.native_classifier_type,
            "project_relative_path": self.project_relative_path,
            "observed_sha256": self.observed_sha256,
            "observed_byte_count": self.observed_byte_count,
            "source_project_identity_sha256": self.source_project_identity_sha256,
            "qupath_version": self.qupath_version,
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            **self._unsigned_mapping(),
            "fit_observation_sha256": self.fit_observation_sha256,
        }


@dataclass(frozen=True, slots=True, init=False, repr=False)
class TrainingEngineRequest:
    """Sanitized scientific and launch projection visible to one fitter."""

    fit_plan: TrainingFitPlanProjection
    native_fit_plan: QuPathNativeTrainingFitPlanProjection | None
    pixel_evidence: MultiImagePixelFitEvidence | None
    supervised_evidence: MultiImageSupervisedFitEvidence | None
    requested_launch_mode: str
    fit_execution_sha256: str | None

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("TrainingEngineRequest is registry-created")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("TrainingEngineRequest cannot be subclassed")

    def __copy__(self) -> object:
        raise TypeError("TrainingEngineRequest is nonserializable")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("TrainingEngineRequest is nonserializable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("TrainingEngineRequest is nonserializable")

    def _validate_projection(self) -> None:
        """Validate projection for the current operation.

        Structured inputs are decoded through their typed ``from_mapping`` constructors. The
        remaining comparisons check the relationships required by this operation before the
        caller can use those records."""

        if (
            type(self) is not TrainingEngineRequest
            or type(self.fit_plan) is not TrainingFitPlanProjection
        ):
            raise BatchTrainingError("training engine request projection is invalid")
        if self.native_fit_plan is not None:
            try:
                canonical_native = QuPathNativeTrainingFitPlanProjection.from_mapping(
                    self.native_fit_plan.to_mapping()
                )
            except Exception as exc:
                raise BatchTrainingError("native fit request is not canonical") from exc
            if (
                type(self.native_fit_plan) is not QuPathNativeTrainingFitPlanProjection
                or canonical_native != self.native_fit_plan
                or self.native_fit_plan.fit_plan != self.fit_plan
            ):
                raise BatchTrainingError("native fit request differs from the fit plan projection")
        pixel = self.pixel_evidence
        supervised = self.supervised_evidence
        if (pixel is None) == (supervised is None):
            raise BatchTrainingError("exactly one fit evidence value is required")
        if self.fit_plan.classifier_kind is ClassifierKind.SUPERVISED_PHENOTYPE:
            if type(supervised) is not MultiImageSupervisedFitEvidence or pixel is not None:
                raise BatchTrainingError("supervised fit request requires supervised evidence")
            evidence_sha = supervised.training_projection_sha256
        else:
            if type(pixel) is not MultiImagePixelFitEvidence or supervised is not None:
                raise BatchTrainingError("pixel fit request requires pixel evidence")
            evidence_sha = pixel.training_projection_sha256
        if evidence_sha != self.fit_plan.fit_plan_sha256:
            raise BatchTrainingError("fit evidence differs from the fit plan projection")
        mode = _text(self.requested_launch_mode, "requested_launch_mode")
        if mode not in {"interactive", "headless"}:
            raise BatchTrainingError("requested_launch_mode must be interactive or headless")
        object.__setattr__(self, "requested_launch_mode", mode)
        fit_execution_sha256 = (
            None
            if self.fit_execution_sha256 is None
            else _sha256(self.fit_execution_sha256, "fit_execution_sha256")
        )
        if self.native_fit_plan is not None and fit_execution_sha256 is None:
            raise BatchTrainingError("native fit request requires a reserved fit execution")
        object.__setattr__(self, "fit_execution_sha256", fit_execution_sha256)


@dataclass(frozen=True, slots=True)
class TrainingEngineArtifactResult:
    """Provider completion containing only one untrusted raw fit artifact."""

    raw_native_artifact: RawNativeClassifierArtifact

    def __post_init__(self) -> None:
        if type(self.raw_native_artifact) is not RawNativeClassifierArtifact:
            raise BatchTrainingError("raw_native_artifact must be a RawNativeClassifierArtifact")


@dataclass(frozen=True, slots=True)
class TrainingEngineResult:
    fit_plan_sha256: str
    disposition: TrainingEngineDisposition
    artifact: TrainingEngineArtifactResult | None
    interaction: TrainingEngineInteractionRequest | None
    error_code: str | None
    error_message: str | None
    channel_requirements: Mapping[str, JsonValue]
    feature_requirements: Mapping[str, JsonValue]
    runtime_versions: tuple[tuple[str, str], ...]
    extension_versions: tuple[tuple[str, str], ...]
    model_versions: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        fit_plan_sha256 = _sha256(self.fit_plan_sha256, "fit_plan_sha256")
        if not isinstance(self.disposition, TrainingEngineDisposition):
            raise BatchTrainingError("disposition must be a TrainingEngineDisposition")
        if self.artifact is not None and type(self.artifact) is not TrainingEngineArtifactResult:
            raise BatchTrainingError("artifact must be a TrainingEngineArtifactResult")
        if (
            self.interaction is not None
            and type(self.interaction) is not TrainingEngineInteractionRequest
        ):
            raise BatchTrainingError("interaction must be a TrainingEngineInteractionRequest")
        error_code = None if self.error_code is None else _text(self.error_code, "error_code")
        error_message = (
            None if self.error_message is None else _text(self.error_message, "error_message")
        )
        if self.disposition is TrainingEngineDisposition.COMPLETED:
            if (
                self.artifact is None
                or self.interaction is not None
                or error_code is not None
                or error_message is not None
            ):
                raise BatchTrainingError("completed result requires exactly an artifact")
        elif self.disposition is TrainingEngineDisposition.NEEDS_INTERACTION:
            if (
                self.artifact is not None
                or self.interaction is None
                or error_code is not None
                or error_message is not None
            ):
                raise BatchTrainingError("needs_interaction result requires exactly an interaction")
            if self.interaction.fit_plan_sha256 != fit_plan_sha256:
                raise BatchTrainingError("interaction fit_plan_sha256 differs from result")
        elif (
            self.artifact is not None
            or self.interaction is not None
            or error_code is None
            or error_message is None
        ):
            raise BatchTrainingError("failed or rejected result requires exactly an error")
        object.__setattr__(self, "fit_plan_sha256", fit_plan_sha256)
        object.__setattr__(self, "error_code", error_code)
        object.__setattr__(self, "error_message", error_message)
        object.__setattr__(
            self,
            "channel_requirements",
            _json_object(self.channel_requirements, "channel_requirements"),
        )
        object.__setattr__(
            self,
            "feature_requirements",
            _json_object(self.feature_requirements, "feature_requirements"),
        )
        object.__setattr__(
            self, "runtime_versions", _version_pairs(self.runtime_versions, "runtime_versions")
        )
        object.__setattr__(
            self,
            "extension_versions",
            _version_pairs(self.extension_versions, "extension_versions"),
        )
        object.__setattr__(
            self, "model_versions", _version_pairs(self.model_versions, "model_versions")
        )

    @property
    def stage_result_checkpoint_id(self) -> str:
        """Checkpoint F4e must publish for a `NEEDS_INTERACTION` result."""

        if (
            self.disposition is not TrainingEngineDisposition.NEEDS_INTERACTION
            or self.interaction is None
        ):
            raise BatchTrainingError("only needs_interaction results have a stage checkpoint")
        return self.interaction.interaction_sha256


@dataclass(slots=True)
class _TrainingEngineReservationRecord:
    reservation_ref: ReferenceType[object]
    reservation_seal_sha256: str
    authority_seal_sha256: str
    registry_identity: int
    provider_identity: int
    capabilities_identity: int
    capabilities_sha256: str
    state: str = "active"
    readiness_ref: ReferenceType[object] | None = None
    readiness_seal_sha256: str | None = None


def _add_bounded_cleanup_note(
    primary: BaseException,
    cleanup: BaseException,
    *,
    context: str,
) -> None:
    """Retain bounded cleanup evidence without replacing the primary error."""

    try:
        try:
            detail = str(cleanup)[:240]
        except BaseException:
            detail = "diagnostic unavailable"
        primary.add_note(f"{context}: {type(cleanup).__name__}: {detail}"[:512])
    except BaseException:
        pass


class _TrainingEngineReservationIssuer:
    """Own the single coordinated lifecycle for reserved native fitting."""

    def __init__(self) -> None:
        from threading import RLock

        self._lock = RLock()
        self._reservations: dict[int, _TrainingEngineReservationRecord] = {}

    def issue(
        self,
        reservation: "TrainingEngineExecutionReservation",
        *,
        authority: object,
        registry: object,
        provider: object,
    ) -> "TrainingEngineExecutionReservation":
        with self._lock:
            key = id(reservation)

            def release(expired: ReferenceType[object], *, reservation_key: int = key) -> None:
                with self._lock:
                    current = self._reservations.get(reservation_key)
                    if current is not None and current.reservation_ref is expired:
                        self._reservations.pop(reservation_key, None)

            self._reservations[key] = _TrainingEngineReservationRecord(
                reservation_ref=ref(reservation, release),
                reservation_seal_sha256=reservation._seal_sha256,
                authority_seal_sha256=authority._seal_sha256,
                registry_identity=id(registry),
                provider_identity=id(provider),
                capabilities_identity=id(reservation._capabilities),
                capabilities_sha256=(reservation._capabilities.capabilities_sha256),
            )
        return reservation

    def _validate_issued_identity(
        self,
        reservation: object,
        *,
        registry: object | None = None,
        required_state: str | None = None,
    ) -> "TrainingEngineExecutionReservation":
        """Validate issued identity for the current operation.

        The implementation compares the supplied record identities and content digests with
        the operation context. A mismatch is reported at this boundary before the record is
        accepted by its caller."""

        if type(reservation) is not TrainingEngineExecutionReservation:
            raise BatchTrainingError("training engine reservation is invalid")
        with self._lock:
            current = self._reservations.get(id(reservation))
            if (
                current is None
                or current.reservation_ref() is not reservation
                or current.reservation_seal_sha256 != reservation._seal_sha256
            ):
                raise BatchTrainingError("training engine reservation is foreign or abandoned")
            if registry is not None and (
                current.registry_identity != id(registry) or reservation._registry is not registry
            ):
                raise BatchTrainingError(
                    "training engine reservation belongs to a foreign registry"
                )
            if required_state is not None and current.state != required_state:
                raise BatchTrainingError(
                    "training engine reservation lifecycle transition requires "
                    f"{required_state}, found {current.state}"
                )
            if (
                current.authority_seal_sha256 != reservation._authority_seal_sha256
                or current.authority_seal_sha256
                != getattr(reservation._authority, "_seal_sha256", None)
                or current.provider_identity != id(reservation._provider)
                or current.capabilities_identity != id(reservation._capabilities)
                or current.capabilities_sha256 != reservation._capabilities_sha256
                or reservation._registry_identity != current.registry_identity
            ):
                raise BatchTrainingError("training engine reservation was mutated")
        if reservation._seal_sha256 != _digest(_reservation_seal_material(reservation)):
            raise BatchTrainingError("training engine reservation was mutated")
        return reservation

    @staticmethod
    def _canonical_capabilities(
        value: object,
    ) -> TrainingEngineCapabilities:
        if type(value) is not TrainingEngineCapabilities:
            raise BatchTrainingError("training engine reservation capabilities are not exact")
        try:
            return TrainingEngineCapabilities(
                engine_id=value.engine_id,
                engine_version=value.engine_version,
                classifier_kinds=value.classifier_kinds,
                artifact_kind=value.artifact_kind,
                execution_mode=value.execution_mode,
                supports_interactive_launch=value.supports_interactive_launch,
                supports_headless_training=value.supports_headless_training,
                can_recover_evidence=value.can_recover_evidence,
                can_report_training_diagnostics=value.can_report_training_diagnostics,
                engine_requirements=value.engine_requirements,
                engine_requirements_sha256=value.engine_requirements_sha256,
                capabilities_sha256=value.capabilities_sha256,
            )
        except (AttributeError, BatchTrainingError, TypeError, ValueError) as exc:
            raise BatchTrainingError(
                "training engine reservation capabilities are not canonical"
            ) from exc

    def validate(
        self,
        reservation: object,
        *,
        registry: object | None = None,
        required_state: str | None = None,
    ) -> "TrainingEngineExecutionReservation":
        return self._validate(
            reservation,
            registry=registry,
            required_state=required_state,
            require_current=True,
        )

    def validate_after_project_commit(
        self,
        reservation: object,
        *,
        registry: object,
        required_state: str,
    ) -> "TrainingEngineExecutionReservation":
        """Validate the sealed predecessor after its expected project mutation."""

        return self._validate(
            reservation,
            registry=registry,
            required_state=required_state,
            require_current=False,
        )

    def _validate(
        self,
        reservation: object,
        *,
        registry: object | None,
        required_state: str | None,
        require_current: bool,
    ) -> "TrainingEngineExecutionReservation":
        if type(require_current) is not bool:
            raise BatchTrainingError("training reservation validation mode is invalid")
        reservation = self._validate_issued_identity(
            reservation,
            registry=registry,
            required_state=required_state,
        )
        native_projection = reservation._native_fit_plan_projection
        if (native_projection is None) != (reservation._native_fit_plan_sha256 is None):
            raise BatchTrainingError("training engine reservation was mutated")
        if native_projection is not None:
            try:
                canonical_native = QuPathNativeTrainingFitPlanProjection.from_mapping(
                    native_projection.to_mapping()
                )
            except Exception as exc:
                raise BatchTrainingError(
                    "training engine reservation native projection was mutated"
                ) from exc
            if (
                type(native_projection) is not QuPathNativeTrainingFitPlanProjection
                or canonical_native != native_projection
                or native_projection.native_fit_plan_sha256 != reservation._native_fit_plan_sha256
                or native_projection.fit_plan.fit_plan_sha256 != reservation._fit_plan_sha256
            ):
                raise BatchTrainingError(
                    "training engine reservation native projection was mutated"
                )
        retained = self._canonical_capabilities(reservation._capabilities)
        if retained.capabilities_sha256 != reservation._capabilities_sha256:
            raise BatchTrainingError("training engine reservation capabilities drifted")
        if (
            retained.artifact_kind is ClassifierArtifactKind.QUPATH_NATIVE
            and native_projection is None
        ):
            raise BatchTrainingError(
                "QuPath-native reservation is missing its native fit projection"
            )
        current = reservation._provider.capabilities
        current_canonical = self._canonical_capabilities(current)
        if current_canonical.capabilities_sha256 != reservation._capabilities_sha256:
            raise BatchTrainingError("training engine reservation capabilities drifted")
        if (
            retained.artifact_kind is not ClassifierArtifactKind.QUPATH_NATIVE
            or (
                reservation._requested_launch_mode == "headless"
                and not retained.supports_headless_training
            )
            or (
                reservation._requested_launch_mode == "interactive"
                and not retained.supports_interactive_launch
            )
        ):
            raise BatchTrainingError("training engine reservation capabilities drifted")
        try:
            if type(reservation._authority) is PixelFitEvidenceAuthority:
                plan, _evidence = _validate_pixel_fit_evidence_authority(
                    reservation._authority,
                    require_current=require_current,
                )
            elif type(reservation._authority) is SupervisedFitEvidenceAuthority:
                plan, _evidence = _validate_supervised_fit_evidence_authority(
                    reservation._authority,
                    require_current=require_current,
                )
            else:
                raise BatchTrainingError("training engine reservation authority is invalid")
        except Exception as exc:
            raise BatchTrainingError(
                "training engine reservation authority is stale or mutated"
            ) from exc
        if TrainingFitPlanProjection.create(plan).fit_plan_sha256 != reservation._fit_plan_sha256:
            raise BatchTrainingError("training engine reservation fit plan is stale")
        if required_state is not None:
            self._validate_issued_identity(
                reservation,
                registry=registry,
                required_state=required_state,
            )
        return reservation

    def begin_assessment(
        self,
        reservation: object,
        *,
        registry: object,
    ) -> "TrainingEngineExecutionReservation":
        checked = self._validate_issued_identity(
            reservation,
            registry=registry,
            required_state="active",
        )
        with self._lock:
            current = self._reservations.get(id(checked))
            if (
                current is None
                or current.reservation_ref() is not checked
                or current.state != "active"
            ):
                raise BatchTrainingError("training engine reservation is abandoned or not active")
            current.state = "assessing"
        return checked

    def lifecycle_state(
        self,
        reservation: object,
        *,
        registry: object | None = None,
    ) -> str:
        checked = self._validate_issued_identity(
            reservation,
            registry=registry,
        )
        with self._lock:
            current = self._reservations.get(id(checked))
            if current is None or current.reservation_ref() is not checked:
                raise BatchTrainingError("training engine reservation is foreign or abandoned")
            return current.state

    def complete_ready(
        self,
        reservation: object,
        readiness: object,
        *,
        registry: object,
    ) -> object:
        checked = self._validate_issued_identity(
            reservation,
            registry=registry,
            required_state="assessing",
        )
        with self._lock:
            current = self._reservations.get(id(checked))
            if (
                current is None
                or current.reservation_ref() is not checked
                or current.state != "assessing"
            ):
                raise BatchTrainingError("training engine reservation is not assessing")
            try:
                issued = _READINESS_ISSUER.issue(readiness)
                current.readiness_ref = ref(issued)
                current.readiness_seal_sha256 = issued._seal_sha256
                current.state = "ready"
            except BaseException as primary:
                try:
                    _READINESS_ISSUER.retire(readiness)
                except BaseException as cleanup:
                    _add_bounded_cleanup_note(
                        primary,
                        cleanup,
                        context="partial readiness issuance cleanup also failed",
                    )
                raise
        return issued

    def validate_ready_binding(
        self,
        reservation: object,
        readiness: object,
        *,
        registry: object,
    ) -> "TrainingEngineExecutionReservation":
        return self._validate_ready_binding(
            reservation,
            readiness,
            registry=registry,
            after_project_commit=False,
        )

    def validate_ready_binding_after_project_commit(
        self,
        reservation: object,
        readiness: object,
        *,
        registry: object,
    ) -> "TrainingEngineExecutionReservation":
        return self._validate_ready_binding(
            reservation,
            readiness,
            registry=registry,
            after_project_commit=True,
        )

    def _validate_ready_binding(
        self,
        reservation: object,
        readiness: object,
        *,
        registry: object,
        after_project_commit: bool,
    ) -> "TrainingEngineExecutionReservation":
        validate_reservation = (
            self.validate_after_project_commit if after_project_commit else self.validate
        )
        checked = validate_reservation(
            reservation,
            registry=registry,
            required_state="ready",
        )
        with self._lock:
            current = self._reservations.get(id(checked))
            if (
                current is None
                or current.reservation_ref() is not checked
                or current.state != "ready"
                or current.readiness_ref is None
                or current.readiness_ref() is not readiness
                or current.readiness_seal_sha256 != getattr(readiness, "_seal_sha256", None)
            ):
                raise BatchTrainingError(
                    "training engine readiness has no live ready reservation claim"
                )
        return checked

    def consume_ready_fit_commit(
        self,
        reservation: object,
        readiness: object,
        *,
        registry: object,
    ) -> "TrainingEngineExecutionReservation":
        """Retire one exact ready claim after its runtime commit is authenticated."""

        checked = self.validate_ready_binding_after_project_commit(
            reservation,
            readiness,
            registry=registry,
        )
        with self._lock:
            current = self._reservations.get(id(checked))
            if (
                current is None
                or current.reservation_ref() is not checked
                or current.state != "ready"
                or current.readiness_ref is None
                or current.readiness_ref() is not readiness
            ):
                raise BatchTrainingError("native fit commit lost its exact ready reservation")
            current.state = "committing"
        cleanup: BaseException | None = None
        try:
            issuer = self._fit_issuer(checked)
            if not issuer.release_claim(checked._authority, checked, checked._seal_sha256):
                cleanup = BatchTrainingError("native fit commit could not retire its fit claim")
        except BaseException as exc:
            cleanup = exc
        try:
            _READINESS_ISSUER.retire(readiness)
        except BaseException as exc:
            if cleanup is None:
                cleanup = exc
            else:
                _add_bounded_cleanup_note(
                    cleanup,
                    exc,
                    context="native fit readiness retirement also failed",
                )
        finally:
            with self._lock:
                retained = self._reservations.get(id(checked))
                if retained is current:
                    self._reservations.pop(id(checked), None)
        if cleanup is not None:
            raise BatchTrainingError("native fit commit reservation retirement failed") from cleanup
        return checked

    def terminalize_assessment(self, reservation: object) -> None:
        checked = self._validate_issued_identity(
            reservation,
            required_state="assessing",
        )
        with self._lock:
            current = self._reservations.get(id(checked))
            if (
                current is None
                or current.reservation_ref() is not checked
                or current.state != "assessing"
            ):
                raise BatchTrainingError("training engine reservation is not assessing")
            current.state = "abandoning"
        cleanup: BaseException | None = None
        try:
            issuer = self._fit_issuer(checked)
            if not issuer.release_claim(checked._authority, checked, checked._seal_sha256):
                cleanup = BatchTrainingError(
                    "training engine assessment could not release its fit claim"
                )
        except BaseException as exc:
            cleanup = exc
        finally:
            with self._lock:
                retained = self._reservations.get(id(checked))
                if retained is current:
                    self._reservations.pop(id(checked), None)
        if cleanup is not None:
            raise BatchTrainingError(
                "training engine assessment terminalization failed"
            ) from cleanup

    @staticmethod
    def _fit_issuer(reservation: "TrainingEngineExecutionReservation"):
        if type(reservation._authority) is PixelFitEvidenceAuthority:
            return _PIXEL_FIT_ISSUER
        if type(reservation._authority) is SupervisedFitEvidenceAuthority:
            return _SUPERVISED_FIT_ISSUER
        raise BatchTrainingError("training engine reservation authority is invalid")

    def abandon(self, reservation: object) -> bool:
        checked = self._validate_issued_identity(reservation)
        with self._lock:
            current = self._reservations.get(id(checked))
            if current is None or current.reservation_ref() is not checked:
                raise BatchTrainingError("training engine reservation is foreign or abandoned")
            if current.state == "assessing":
                return False
            if current.state not in {"active", "ready"}:
                return False
            current.state = "abandoning"
            readiness = None if current.readiness_ref is None else current.readiness_ref()
        cleanup: BaseException | None = None
        try:
            issuer = self._fit_issuer(checked)
            if not issuer.release_claim(checked._authority, checked, checked._seal_sha256):
                cleanup = BatchTrainingError(
                    "training engine abandonment could not release its fit claim"
                )
        except BaseException as exc:
            cleanup = exc
        try:
            if readiness is not None:
                _READINESS_ISSUER.retire(readiness)
        except BaseException as exc:
            if cleanup is None:
                cleanup = exc
            else:
                _add_bounded_cleanup_note(
                    cleanup,
                    exc,
                    context="readiness retirement also failed",
                )
        finally:
            with self._lock:
                retained = self._reservations.get(id(checked))
                if retained is current:
                    self._reservations.pop(id(checked), None)
        if cleanup is not None:
            raise BatchTrainingError("training engine reservation abandonment failed") from cleanup
        return True


_RESERVATION_ISSUER = _TrainingEngineReservationIssuer()


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False, repr=False)
class TrainingEngineExecutionReservation:
    """Issuer-only pre-dispatch reservation for a later training session."""

    _fit_execution_sha256: str
    _fit_plan_sha256: str
    _native_classifier_type: str
    _project_relative_path: str
    _classifier_version: str
    _requested_launch_mode: str
    _capabilities_sha256: str
    _parent_execution_context_sha256: str
    _parent_authorization_sha256: str
    _parent_current_execution_seal_sha256: str
    _parent_execution_seal_sha256: str
    _state_head_sha256: str
    _active_plan_sha256: str
    _native_fit_plan_sha256: str | None
    _native_fit_plan_projection: QuPathNativeTrainingFitPlanProjection | None
    _authority: object
    _provider: object
    _capabilities: TrainingEngineCapabilities
    _authority_seal_sha256: str
    _registry: object
    _registry_identity: int
    _seal_sha256: str

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("TrainingEngineExecutionReservation is registry-issued")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("TrainingEngineExecutionReservation cannot be subclassed")

    def __copy__(self) -> object:
        raise TypeError("TrainingEngineExecutionReservation is nonserializable")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("TrainingEngineExecutionReservation is nonserializable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("TrainingEngineExecutionReservation is nonserializable")

    @property
    def fit_execution_sha256(self) -> str:
        return self._fit_execution_sha256

    @property
    def native_classifier_type(self) -> str:
        return self._native_classifier_type

    @property
    def project_relative_path(self) -> str:
        return self._project_relative_path

    @property
    def classifier_version(self) -> str:
        return self._classifier_version

    def abandon(self) -> bool:
        return _RESERVATION_ISSUER.abandon(self)


def _readiness_result_material(
    result: TrainingEngineReadinessResult,
) -> dict[str, object]:
    interaction = result.interaction
    return {
        "fit_plan_sha256": result.fit_plan_sha256,
        "disposition": result.disposition.value,
        "interaction": (
            None
            if interaction is None
            else {
                "fit_plan_sha256": interaction.fit_plan_sha256,
                "interaction_reason": interaction.interaction_reason.value,
                "message": interaction.message,
                "allowed_actions": list(interaction.allowed_actions),
                "resume_context": _export_json(interaction.resume_context),
                "checkpoint_id": interaction.checkpoint_id,
                "interaction_sha256": interaction.interaction_sha256,
            }
        ),
        "error_code": result.error_code,
        "error_message": result.error_message,
    }


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False, repr=False)
class TrainingEngineReadinessAuthority:
    """Issuer-only proof that one exact reserved fit passed native preflight."""

    _fit_execution_sha256: str
    _fit_plan_sha256: str
    _native_classifier_type: str
    _project_relative_path: str
    _classifier_version: str
    _requested_launch_mode: str
    _capabilities_sha256: str
    _training_evidence_material_sha256: str
    _reservation_seal_sha256: str
    _reservation: TrainingEngineExecutionReservation
    _registry: object
    _provider: object
    _capabilities: TrainingEngineCapabilities
    _fit_plan: TrainingFitPlanProjection
    _training_evidence: object
    _readiness_result: TrainingEngineReadinessResult
    _seal_sha256: str

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("TrainingEngineReadinessAuthority is registry-issued")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("TrainingEngineReadinessAuthority cannot be subclassed")

    def __copy__(self) -> object:
        raise TypeError("TrainingEngineReadinessAuthority is nonserializable")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("TrainingEngineReadinessAuthority is nonserializable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("TrainingEngineReadinessAuthority is nonserializable")

    @property
    def readiness_result(self) -> TrainingEngineReadinessResult:
        return self._readiness_result

    @property
    def fit_execution_sha256(self) -> str:
        return self._fit_execution_sha256

    @property
    def native_classifier_type(self) -> str:
        return self._native_classifier_type

    @property
    def project_relative_path(self) -> str:
        return self._project_relative_path

    @property
    def classifier_version(self) -> str:
        return self._classifier_version


@dataclass(frozen=True, slots=True, init=False, repr=False)
class NativeFitRegistryCommitAuthority:
    """One-use registry authentication of a provider-owned native fit commit."""

    fit_execution_sha256: str
    fit_plan_sha256: str
    training_material_evidence_sha256: str
    engine_id: str
    engine_version: str
    engine_capabilities_sha256: str
    _reservation: TrainingEngineExecutionReservation
    _readiness: TrainingEngineReadinessAuthority
    _completion: object
    _project_commit: object
    _registry: "TrainingEngineProviderRegistry"
    _issuer: object
    _seal_sha256: str

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("native fit registry authorities are issuer-only")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("native fit registry authorities cannot be subclassed")

    def __copy__(self) -> object:
        raise TypeError("native fit registry authorities are nonserializable")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("native fit registry authorities are nonserializable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("native fit registry authorities are nonserializable")


_NATIVE_FIT_REGISTRY_ISSUER = object()


def _native_fit_registry_authority_material(
    authority: NativeFitRegistryCommitAuthority,
) -> dict[str, object]:
    return {
        "authority_object_identity": id(authority),
        "fit_execution_sha256": authority.fit_execution_sha256,
        "fit_plan_sha256": authority.fit_plan_sha256,
        "training_material_evidence_sha256": (authority.training_material_evidence_sha256),
        "engine_id": authority.engine_id,
        "engine_version": authority.engine_version,
        "engine_capabilities_sha256": authority.engine_capabilities_sha256,
        "reservation_object_identity": id(authority._reservation),
        "readiness_object_identity": id(authority._readiness),
        "completion_object_identity": id(authority._completion),
        "project_commit_object_identity": id(authority._project_commit),
        "registry_object_identity": id(authority._registry),
    }


def _readiness_authority_seal_material(
    authority: TrainingEngineReadinessAuthority,
) -> dict[str, object]:
    return {
        "authority_object_identity": id(authority),
        "fit_execution_sha256": authority._fit_execution_sha256,
        "fit_plan_sha256": authority._fit_plan_sha256,
        "native_classifier_type": authority._native_classifier_type,
        "project_relative_path": authority._project_relative_path,
        "classifier_version": authority._classifier_version,
        "requested_launch_mode": authority._requested_launch_mode,
        "capabilities_sha256": authority._capabilities_sha256,
        "training_evidence_material_sha256": (authority._training_evidence_material_sha256),
        "reservation_object_identity": id(authority._reservation),
        "reservation_seal_sha256": authority._reservation_seal_sha256,
        "registry_object_identity": id(authority._registry),
        "provider_object_identity": id(authority._provider),
        "capabilities_object_identity": id(authority._capabilities),
        "fit_plan_object_identity": id(authority._fit_plan),
        "training_evidence_object_identity": id(authority._training_evidence),
        "readiness_result_object_identity": id(authority._readiness_result),
        "readiness_result": _readiness_result_material(authority._readiness_result),
    }


class _TrainingEngineReadinessIssuer:
    def __init__(self) -> None:
        from threading import RLock

        self._lock = RLock()
        self._authorities: dict[int, tuple[object, ...]] = {}

    def issue(
        self,
        authority: TrainingEngineReadinessAuthority,
    ) -> TrainingEngineReadinessAuthority:
        with self._lock:
            key = id(authority)

            def release(
                expired: ReferenceType[TrainingEngineReadinessAuthority],
                *,
                authority_key: int = key,
            ) -> None:
                with self._lock:
                    current = self._authorities.get(authority_key)
                    if current is not None and current[0] is expired:
                        self._authorities.pop(authority_key, None)

            self._authorities[key] = (
                ref(authority, release),
                authority._seal_sha256,
                id(authority._reservation),
                authority._reservation_seal_sha256,
                id(authority._registry),
                id(authority._provider),
                id(authority._capabilities),
                authority._capabilities_sha256,
                id(authority._fit_plan),
                id(authority._training_evidence),
                authority._training_evidence_material_sha256,
                id(authority._readiness_result),
                _digest(_readiness_result_material(authority._readiness_result)),
            )
        return authority

    def retire(self, authority: object) -> None:
        with self._lock:
            current = self._authorities.get(id(authority))
            if current is not None and current[0]() is authority:
                self._authorities.pop(id(authority), None)

    def validate(
        self,
        authority: object,
        *,
        reservation: object,
        registry: object,
    ) -> TrainingEngineReadinessAuthority:
        return self._validate(
            authority,
            reservation=reservation,
            registry=registry,
            after_project_commit=False,
        )

    def validate_after_project_commit(
        self,
        authority: object,
        *,
        reservation: object,
        registry: object,
    ) -> TrainingEngineReadinessAuthority:
        """Validate an exact readiness envelope after its durable mutation."""

        return self._validate(
            authority,
            reservation=reservation,
            registry=registry,
            after_project_commit=True,
        )

    def _validate(
        self,
        authority: object,
        *,
        reservation: object,
        registry: object,
        after_project_commit: bool,
    ) -> TrainingEngineReadinessAuthority:
        if type(authority) is not TrainingEngineReadinessAuthority:
            raise BatchTrainingError("training engine readiness authority is invalid")
        with self._lock:
            current = self._authorities.get(id(authority))
            if current is None or current[0]() is not authority:
                raise BatchTrainingError("training engine readiness authority is foreign")
            try:
                snapshot = (
                    authority._seal_sha256,
                    id(authority._reservation),
                    authority._reservation_seal_sha256,
                    id(authority._registry),
                    id(authority._provider),
                    id(authority._capabilities),
                    authority._capabilities_sha256,
                    id(authority._fit_plan),
                    id(authority._training_evidence),
                    authority._training_evidence_material_sha256,
                    id(authority._readiness_result),
                    _digest(_readiness_result_material(authority._readiness_result)),
                )
            except Exception as exc:
                raise BatchTrainingError("training engine readiness authority was mutated") from exc
            if snapshot != current[1:]:
                raise BatchTrainingError("training engine readiness authority was mutated")
        try:
            seal_is_current = authority._seal_sha256 == _digest(
                _readiness_authority_seal_material(authority)
            )
        except Exception as exc:
            raise BatchTrainingError("training engine readiness authority was mutated") from exc
        if not seal_is_current:
            raise BatchTrainingError("training engine readiness authority was mutated")
        if authority._reservation is not reservation or authority._registry is not registry:
            raise BatchTrainingError(
                "training engine readiness authority has a foreign reservation or registry"
            )
        validate_binding = (
            _RESERVATION_ISSUER.validate_ready_binding_after_project_commit
            if after_project_commit
            else _RESERVATION_ISSUER.validate_ready_binding
        )
        checked = validate_binding(reservation, authority, registry=registry)
        try:
            current_provider, current_capabilities = authority._registry._resolve_registration(
                authority._fit_plan.classifier_engine,
                authority._fit_plan.classifier_kind,
            )
        except Exception as exc:
            raise BatchTrainingError(
                "training engine readiness provider registration drifted"
            ) from exc
        if (
            current_provider is not authority._provider
            or current_capabilities is not authority._capabilities
            or current_capabilities.capabilities_sha256 != authority._capabilities_sha256
        ):
            raise BatchTrainingError("training engine readiness provider registration drifted")
        try:
            if type(checked._authority) is PixelFitEvidenceAuthority:
                current_plan, current_evidence = _validate_pixel_fit_evidence_authority(
                    checked._authority,
                    require_current=not after_project_commit,
                )
            elif type(checked._authority) is SupervisedFitEvidenceAuthority:
                current_plan, current_evidence = _validate_supervised_fit_evidence_authority(
                    checked._authority,
                    require_current=not after_project_commit,
                )
            else:
                raise BatchTrainingError(
                    "training engine readiness reservation authority is invalid"
                )
            current_fit_plan = TrainingFitPlanProjection.create(current_plan)
        except Exception as exc:
            raise BatchTrainingError(
                "training engine readiness authority uses stale state"
            ) from exc
        if (
            authority._reservation_seal_sha256 != checked._seal_sha256
            or authority._provider is not checked._provider
            or authority._capabilities is not checked._capabilities
            or authority._capabilities_sha256 != checked._capabilities_sha256
            or authority._fit_execution_sha256 != checked._fit_execution_sha256
            or authority._fit_plan_sha256 != checked._fit_plan_sha256
            or authority._native_classifier_type != checked._native_classifier_type
            or authority._project_relative_path != checked._project_relative_path
            or authority._classifier_version != checked._classifier_version
            or authority._requested_launch_mode != "headless"
            or authority._training_evidence != current_evidence
            or authority._training_evidence_material_sha256
            != checked._authority._evidence.material_evidence_sha256
            or authority._fit_plan != current_fit_plan
            or authority._fit_plan.fit_plan_sha256 != checked._fit_plan_sha256
            or type(authority._readiness_result) is not TrainingEngineReadinessResult
            or authority._readiness_result.disposition
            is not TrainingEngineReadinessDisposition.READY
            or authority._readiness_result.fit_plan_sha256 != checked._fit_plan_sha256
        ):
            raise BatchTrainingError(
                "training engine readiness authority differs from its reservation"
            )
        validate_binding(reservation, authority, registry=registry)
        return authority


_READINESS_ISSUER = _TrainingEngineReadinessIssuer()


@dataclass(frozen=True, slots=True)
class _TrainingReservationProjectAcquisitionProjection:
    fit_execution_sha256: str
    fit_plan_sha256: str
    project_relative_path: str
    classifier_version: str
    parent_execution_context_sha256: str
    parent_authorization_sha256: str
    parent_current_execution_seal_sha256: str
    parent_execution_seal_sha256: str
    state_head_sha256: str
    active_plan_sha256: str
    review_resource_paths: tuple[str, ...]


def _authenticate_training_reservation_for_project_acquisition(
    reservation: object,
    *,
    expected_context: BatchNodeExecutionContext,
    current_execution: BatchCurrentExecutionAuthority,
    state_store: object,
    state_head_sha256: str,
    active_plan_sha256: str,
) -> _TrainingReservationProjectAcquisitionProjection:
    """Reopen the exact reserved fit for the state-owned project boundary."""

    if (
        type(expected_context) is not BatchNodeExecutionContext
        or type(current_execution) is not BatchCurrentExecutionAuthority
    ):
        raise BatchTrainingError("training reservation project acquisition context is invalid")
    checked = _RESERVATION_ISSUER.validate(reservation)
    authority = checked._authority
    if (
        authority._state_store is not state_store
        or authority._state_store_identity != id(state_store)
        or authority._expected_context is not expected_context
        or checked._parent_execution_context_sha256 != current_execution.execution_context_sha256
        or checked._parent_authorization_sha256 != current_execution.authorization_sha256
        or checked._parent_current_execution_seal_sha256 != current_execution._seal_sha256
        or authority._parent_node_material_sha256 != current_execution.node_material_sha256
        or checked._state_head_sha256 != state_head_sha256
        or checked._state_head_sha256 != current_execution.state_head_sha256
        or checked._active_plan_sha256 != active_plan_sha256
        or checked._active_plan_sha256 != current_execution.active_plan_sha256
    ):
        raise BatchTrainingError(
            "training reservation differs from current state acquisition authority"
        )
    from mif_pipeline.pipeline_core.model_selection import cv_review_resource_paths
    native = checked._native_fit_plan_projection
    review_paths = () if native is None else cv_review_resource_paths(
        checked._fit_execution_sha256, native.fit_plan.model_configuration,
        [entry.subject_id for entry in native.fit_plan.training_entries],
        native.fit_plan.random_seed, checked._native_classifier_type)
    return _TrainingReservationProjectAcquisitionProjection(
        fit_execution_sha256=checked._fit_execution_sha256,
        fit_plan_sha256=checked._fit_plan_sha256,
        project_relative_path=checked._project_relative_path,
        classifier_version=checked._classifier_version,
        parent_execution_context_sha256=(checked._parent_execution_context_sha256),
        parent_authorization_sha256=checked._parent_authorization_sha256,
        parent_current_execution_seal_sha256=(checked._parent_current_execution_seal_sha256),
        parent_execution_seal_sha256=checked._parent_execution_seal_sha256,
        state_head_sha256=checked._state_head_sha256,
        active_plan_sha256=checked._active_plan_sha256,
        review_resource_paths=review_paths,
    )


def _reservation_material(
    *,
    authority: object,
    fit_plan: TrainingFitPlanProjection,
    capabilities: TrainingEngineCapabilities,
    requested_launch_mode: str,
    native_fit_plan_sha256: str | None = None,
) -> dict[str, object]:
    material = {
        "authority_type": type(authority).__name__,
        "authority_object_identity": id(authority),
        "authority_reservation_nonce": authority._reservation_nonce,
        "authority_seal_sha256": authority._seal_sha256,
        "fit_plan_sha256": fit_plan.fit_plan_sha256,
        "training_evidence_material_sha256": authority._evidence.material_evidence_sha256,
        "capabilities_sha256": capabilities.capabilities_sha256,
        "requested_launch_mode": requested_launch_mode,
        "parent_execution_context_sha256": authority._parent_execution_context_sha256,
        "parent_authorization_sha256": authority._parent_authorization_sha256,
        "parent_current_execution_seal_sha256": authority._parent_current_execution_seal_sha256,
        "parent_execution_seal_sha256": authority._parent_execution_seal_sha256,
        "state_head_sha256": authority._state_head_sha256,
        "active_plan_sha256": authority._active_plan_sha256,
    }
    if native_fit_plan_sha256 is not None:
        material["native_fit_plan_sha256"] = native_fit_plan_sha256
    return material


def _reservation_seal_material(
    reservation: TrainingEngineExecutionReservation,
) -> dict[str, object]:
    return {
        field: getattr(reservation, field)
        for field in TrainingEngineExecutionReservation.__dataclass_fields__
        if field
        not in {
            "_seal_sha256",
            "_authority",
            "_provider",
            "_capabilities",
            "_registry",
            "_native_fit_plan_projection",
        }
    }


def _native_classifier_target_family(classifier_kind: ClassifierKind) -> str:
    if classifier_kind is ClassifierKind.SUPERVISED_PHENOTYPE:
        return "object"
    if classifier_kind in {
        ClassifierKind.PRIMARY_COMPARTMENT,
        ClassifierKind.SECONDARY_STRUCTURE,
    }:
        return "pixel"
    raise BatchTrainingError("classifier kind has no native target family")


@runtime_checkable
class TrainingEngineProvider(Protocol):
    @property
    def capabilities(self) -> TrainingEngineCapabilities: ...

    def assess_readiness(self, request: TrainingEngineRequest) -> TrainingEngineReadinessResult: ...

    def execute(self, request: TrainingEngineRequest) -> TrainingEngineResult: ...


_QUPATH_NATIVE_RTREES_REQUIREMENTS_BY_ENGINE = {
    "qupath-kronos-rtrees-classifier": {
        "configuration_schema": "qupath-kronos-rtrees/v2",
        "native_model_family": "opencv_ml.RTrees",
        "training_path": "raw_rtrees_train_then_qupath_public_wrap",
        "feature_pipeline": "frozen_kronos_mean_self_gnn/v1",
    },
    "qupath-pixel-classifier": {
        "configuration_schema": "qupath-pixel-rtrees/v1",
        "native_model_family": "opencv_ml.RTrees",
        "training_path": "raw_rtrees_train_then_qupath_public_wrap",
    },
    "qupath-object-classifier": {
        "configuration_schema": "qupath-object-rtrees/v1",
        "native_model_family": "opencv_ml.RTrees",
        "training_path": "raw_rtrees_train_then_qupath_public_wrap",
        "prediction_confidence": "native_tree_vote_fraction",
    },
}
_QUPATH_NATIVE_RTREES_ENGINE_VERSION = "qupath-native-rtrees/v1"
_QUPATH_NATIVE_OBJECT_RTREES_ENGINE_VERSION = "qupath-native-rtrees/v2"
_QUPATH_NATIVE_SVM_ENGINE_VERSION = "qupath-native-svm/v1"
_QUPATH_NATIVE_SVM_REQUIREMENTS_BY_ENGINE = {
    "qupath-object-svm-classifier": {
        "configuration_schema": "qupath-object-svm/v1",
        "native_model_family": "opencv_ml.SVM",
        "training_path": "raw_svm_train_then_qupath_public_wrap",
        "prediction_confidence": "native_svm_pairwise_vote_fraction",
    },
    "qupath-kronos-svm-classifier": {
        "configuration_schema": "qupath-kronos-svm/v2",
        "native_model_family": "opencv_ml.SVM",
        "training_path": "raw_svm_train_then_qupath_public_wrap",
        "feature_pipeline": "frozen_kronos_mean_self_gnn/v1",
    },
    "qupath-pixel-svm-classifier": {
        "configuration_schema": "qupath-pixel-svm/v1",
        "native_model_family": "opencv_ml.SVM",
        "training_path": "raw_svm_train_then_qupath_public_wrap",
    },
}
_QUPATH_NATIVE_LOGISTIC_REGRESSION_ENGINE_VERSION = "qupath-native-logistic-regression/v1"
_QUPATH_NATIVE_LOGISTIC_REGRESSION_REQUIREMENTS_BY_ENGINE = {
    "qupath-object-logistic-regression-classifier": {
        "configuration_schema": "qupath-object-logistic-regression/v1",
        "native_model_family": "opencv_ml.LogisticRegression",
        "training_path": "raw_logistic_regression_train_then_native_object_wrap",
        "prediction_confidence": "native_logistic_ovr_normalized_score",
    },
}
_QUPATH_NATIVE_REQUIREMENTS_BY_ENGINE = {
    **_QUPATH_NATIVE_RTREES_REQUIREMENTS_BY_ENGINE,
    **_QUPATH_NATIVE_SVM_REQUIREMENTS_BY_ENGINE,
    **_QUPATH_NATIVE_LOGISTIC_REGRESSION_REQUIREMENTS_BY_ENGINE,
}
_QUPATH_NATIVE_VERSION_BY_ENGINE = {
    **{key: _QUPATH_NATIVE_RTREES_ENGINE_VERSION for key in _QUPATH_NATIVE_RTREES_REQUIREMENTS_BY_ENGINE},
    "qupath-object-classifier": _QUPATH_NATIVE_OBJECT_RTREES_ENGINE_VERSION,
    **{key: _QUPATH_NATIVE_SVM_ENGINE_VERSION for key in _QUPATH_NATIVE_SVM_REQUIREMENTS_BY_ENGINE},
    **{key: _QUPATH_NATIVE_LOGISTIC_REGRESSION_ENGINE_VERSION
       for key in _QUPATH_NATIVE_LOGISTIC_REGRESSION_REQUIREMENTS_BY_ENGINE},
}


@dataclass(frozen=True, slots=True)
class QuPathNativeTrainingEngineProvider:
    """Capability-only adapter for Task 11's retained native-session owner."""

    capabilities: TrainingEngineCapabilities

    def __post_init__(self) -> None:
        capabilities = self.capabilities
        engine_id = getattr(capabilities, "engine_id", None)
        expected_requirements = _QUPATH_NATIVE_REQUIREMENTS_BY_ENGINE.get(engine_id)
        expected_version = _QUPATH_NATIVE_VERSION_BY_ENGINE.get(engine_id)
        expected_kinds = {
            "qupath-kronos-rtrees-classifier": (
                ClassifierKind.PRIMARY_COMPARTMENT,
                ClassifierKind.SECONDARY_STRUCTURE,
            ),
            "qupath-kronos-svm-classifier": (
                ClassifierKind.PRIMARY_COMPARTMENT,
                ClassifierKind.SECONDARY_STRUCTURE,
            ),
            "qupath-pixel-classifier": (
                ClassifierKind.PRIMARY_COMPARTMENT,
                ClassifierKind.SECONDARY_STRUCTURE,
            ),
            "qupath-object-classifier": (ClassifierKind.SUPERVISED_PHENOTYPE,),
            "qupath-object-svm-classifier": (ClassifierKind.SUPERVISED_PHENOTYPE,),
            "qupath-object-logistic-regression-classifier": (ClassifierKind.SUPERVISED_PHENOTYPE,),
            "qupath-pixel-svm-classifier": (
                ClassifierKind.PRIMARY_COMPARTMENT,
                ClassifierKind.SECONDARY_STRUCTURE,
            ),
        }.get(engine_id)
        if (
            type(capabilities) is not TrainingEngineCapabilities
            or expected_requirements is None
            or capabilities.engine_version != expected_version
            or capabilities.classifier_kinds != expected_kinds
            or capabilities.artifact_kind is not ClassifierArtifactKind.QUPATH_NATIVE
            or capabilities.execution_mode is not TrainingExecutionMode.INTERACTIVE_NATIVE
            or capabilities.supports_interactive_launch is not True
            or capabilities.supports_headless_training is not True
            or capabilities.can_recover_evidence is not False
            or capabilities.can_report_training_diagnostics is not True
            or capabilities.engine_requirements != expected_requirements
        ):
            raise BatchTrainingError(
                "QuPath native provider capabilities differ from the closed route"
            )

    def assess_readiness(self, request: TrainingEngineRequest) -> TrainingEngineReadinessResult:
        fit_plan_sha256 = "0" * 64
        exact = type(request) is TrainingEngineRequest
        if exact:
            try:
                request._validate_projection()
                fit_plan_sha256 = request.fit_plan.fit_plan_sha256
            except (AttributeError, BatchTrainingError, TypeError, ValueError):
                exact = False
        configuration_schema = None
        if exact and request.native_fit_plan is not None:
            configuration_schema = (
                base_configuration(request.native_fit_plan.model_configuration)
                .to_mapping()
                .get("configuration_schema")
            )
        requirements = self.capabilities.engine_requirements
        exact = bool(
            exact
            and request.fit_plan.classifier_engine == self.capabilities.engine_id
            and request.fit_plan.classifier_kind in self.capabilities.classifier_kinds
            and request.native_fit_plan is not None
            and configuration_schema == requirements["configuration_schema"]
            and (
                request.requested_launch_mode != "interactive"
                or self.capabilities.supports_interactive_launch
            )
            and (
                request.requested_launch_mode != "headless"
                or self.capabilities.supports_headless_training
            )
        )
        if exact:
            base_model = base_configuration(request.native_fit_plan.model_configuration)
            try:
                if type(base_model) is QuPathKronosConfiguration:
                    inspect_kronos_readiness(base_model)
                elif getattr(base_model, "feature_source", None) is not None:
                    inspect_kronos_cell_readiness(base_model.feature_source)
            except (OSError, TypeError, ValueError, BatchTrainingError) as error:
                return TrainingEngineReadinessResult(
                    fit_plan_sha256=fit_plan_sha256,
                    disposition=TrainingEngineReadinessDisposition.REJECTED,
                    interaction=None,
                    error_code="kronos_resources_unavailable",
                    error_message=f"The selected KRONOS resources are unavailable: {error}",
                )
        if exact:
            return TrainingEngineReadinessResult(
                fit_plan_sha256=fit_plan_sha256,
                disposition=TrainingEngineReadinessDisposition.READY,
                interaction=None,
                error_code=None,
                error_message=None,
            )
        return TrainingEngineReadinessResult(
            fit_plan_sha256=fit_plan_sha256,
            disposition=TrainingEngineReadinessDisposition.REJECTED,
            interaction=None,
            error_code="native_provider_capability_mismatch",
            error_message=("The native training request does not match this provider capability."),
        )

    def execute(self, request: TrainingEngineRequest) -> TrainingEngineResult:
        del request
        raise BatchTrainingError("QuPath-native fitting is owned by the retained native session")


@dataclass(frozen=True, slots=True)
class TrainingEngineProviderRegistry:
    providers: tuple[TrainingEngineProvider, ...]
    _registrations: tuple[tuple[TrainingEngineProvider, TrainingEngineCapabilities], ...] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        providers = tuple(self.providers)
        if not all(callable(getattr(item, "execute", None)) for item in providers):
            raise BatchTrainingError("providers must implement training execution")
        keys: set[tuple[str, ClassifierKind]] = set()
        registrations: list[tuple[TrainingEngineProvider, TrainingEngineCapabilities]] = []
        for provider in providers:
            capabilities = provider.capabilities
            if type(capabilities) is not TrainingEngineCapabilities:
                raise BatchTrainingError("provider capabilities must be TrainingEngineCapabilities")
            for kind in capabilities.classifier_kinds:
                key = (capabilities.engine_id, kind)
                if key in keys:
                    raise BatchTrainingError("duplicate training engine registration")
                keys.add(key)
            registrations.append((provider, capabilities))
        object.__setattr__(self, "providers", providers)
        object.__setattr__(self, "_registrations", tuple(registrations))

    def resolve(self, engine_id: str, classifier_kind: ClassifierKind) -> TrainingEngineProvider:
        requested_engine = _text(engine_id, "engine_id")
        if not isinstance(classifier_kind, ClassifierKind):
            raise BatchTrainingError("classifier_kind must be a ClassifierKind")
        for provider, capabilities in self._registrations:
            current_capabilities = provider.capabilities
            if (
                type(current_capabilities) is not TrainingEngineCapabilities
                or current_capabilities != capabilities
                or current_capabilities.capabilities_sha256 != capabilities.capabilities_sha256
            ):
                raise BatchTrainingError("registered training engine capabilities drifted")
            if (
                capabilities.engine_id == requested_engine
                and classifier_kind in capabilities.classifier_kinds
            ):
                return provider
        raise BatchTrainingError("training engine is not registered for the classifier kind")

    def project_native_fit_authority(
        self,
        authority: PixelFitEvidenceAuthority | SupervisedFitEvidenceAuthority,
        *,
        current_parent: object,
    ) -> TrainingFitPlanProjection:
        """Authenticate one fit authority inside its exact current parent."""

        from .parent_execution import (
            CurrentTrainingParentExecution,
            validate_current_training_parent_execution,
        )

        if type(authority) is PixelFitEvidenceAuthority:
            validate_authority = _validate_pixel_fit_evidence_authority
        elif type(authority) is SupervisedFitEvidenceAuthority:
            validate_authority = _validate_supervised_fit_evidence_authority
        else:
            raise BatchTrainingError("native fit projection requires an exact fit authority")
        if type(current_parent) is not CurrentTrainingParentExecution:
            raise BatchTrainingError("native fit projection requires an exact current parent")
        plan, _evidence = validate_authority(authority, require_current=True)
        validate_current_training_parent_execution(
            current_parent,
            expected_context=authority._expected_context,
            state_store=authority._state_store,
        )
        if (
            plan.request.classifier_kind is not current_parent.classifier_kind
            or plan not in current_parent.training_plan_set.plan_set.plans
        ):
            raise BatchTrainingError("native training fit is outside its current aggregate parent")
        return TrainingFitPlanProjection.create(plan)

    def reserve(
        self,
        authority: PixelFitEvidenceAuthority | SupervisedFitEvidenceAuthority,
        *,
        requested_launch_mode: str,
        _native_fit_plan_projection: QuPathNativeTrainingFitPlanProjection | None = None,
    ) -> TrainingEngineExecutionReservation:
        """Preflight one fit without consuming its authority or dispatching a provider."""

        mode = _text(requested_launch_mode, "requested_launch_mode")
        if mode != "headless":
            raise BatchTrainingError("reserved native fitting requires headless launch")
        if type(authority) is PixelFitEvidenceAuthority:
            validate_authority = _validate_pixel_fit_evidence_authority
        elif type(authority) is SupervisedFitEvidenceAuthority:
            validate_authority = _validate_supervised_fit_evidence_authority
        else:
            raise BatchTrainingError("engine reservation requires an exact fit evidence authority")
        plan, _evidence = validate_authority(authority, require_current=True)
        if _native_fit_plan_projection is None:
            raise BatchTrainingError(
                "QuPath-native training requires reserve_native with an exact native fit projection"
            )
        fit_plan = TrainingFitPlanProjection.create(plan)
        if type(_native_fit_plan_projection) is not QuPathNativeTrainingFitPlanProjection:
            raise BatchTrainingError("native fit projection must have the exact supported type")
        try:
            canonical_native_projection = QuPathNativeTrainingFitPlanProjection.from_mapping(
                _native_fit_plan_projection.to_mapping()
            )
        except Exception as exc:
            raise BatchTrainingError("native fit projection is not canonical") from exc
        if (
            canonical_native_projection != _native_fit_plan_projection
            or canonical_native_projection.native_fit_plan_sha256
            != _native_fit_plan_projection.native_fit_plan_sha256
            or canonical_native_projection.fit_plan != fit_plan
        ):
            raise BatchTrainingError("native fit projection differs from the reserved fit plan")
        provider, capabilities = self._resolve_registration(
            plan.classifier_engine,
            plan.request.classifier_kind,
        )
        if capabilities.artifact_kind is not ClassifierArtifactKind.QUPATH_NATIVE:
            raise BatchTrainingError("training reservation requires a QuPath-native engine")
        if not capabilities.supports_headless_training:
            raise BatchTrainingError("training engine does not support headless launch")
        material = _reservation_material(
            authority=authority,
            fit_plan=fit_plan,
            capabilities=capabilities,
            requested_launch_mode=mode,
            native_fit_plan_sha256=(
                None
                if _native_fit_plan_projection is None
                else _native_fit_plan_projection.native_fit_plan_sha256
            ),
        )
        fit_execution_sha256 = _digest(material)
        native_type = _native_classifier_target_family(plan.request.classifier_kind)
        values = {
            "_fit_execution_sha256": fit_execution_sha256,
            "_fit_plan_sha256": fit_plan.fit_plan_sha256,
            "_native_classifier_type": native_type,
            "_project_relative_path": (
                f"classifiers/{native_type}_classifiers/" f"mif-{fit_execution_sha256}.json"
            ),
            "_classifier_version": f"mif-fit-{fit_execution_sha256}",
            "_requested_launch_mode": mode,
            "_capabilities_sha256": capabilities.capabilities_sha256,
            "_parent_execution_context_sha256": authority._parent_execution_context_sha256,
            "_parent_authorization_sha256": authority._parent_authorization_sha256,
            "_parent_current_execution_seal_sha256": authority._parent_current_execution_seal_sha256,
            "_parent_execution_seal_sha256": authority._parent_execution_seal_sha256,
            "_state_head_sha256": authority._state_head_sha256,
            "_active_plan_sha256": authority._active_plan_sha256,
            "_native_fit_plan_sha256": (
                None
                if _native_fit_plan_projection is None
                else _native_fit_plan_projection.native_fit_plan_sha256
            ),
            "_native_fit_plan_projection": _native_fit_plan_projection,
            "_authority": authority,
            "_provider": provider,
            "_capabilities": capabilities,
            "_authority_seal_sha256": authority._seal_sha256,
            "_registry": self,
            "_registry_identity": id(self),
        }
        reservation = object.__new__(TrainingEngineExecutionReservation)
        for name, value in values.items():
            object.__setattr__(reservation, name, value)
        object.__setattr__(
            reservation, "_seal_sha256", _digest(_reservation_seal_material(reservation))
        )
        authority_issuer = (
            _PIXEL_FIT_ISSUER
            if type(authority) is PixelFitEvidenceAuthority
            else _SUPERVISED_FIT_ISSUER
        )
        authority_issuer.claim(authority, reservation, reservation._seal_sha256)
        try:
            return _RESERVATION_ISSUER.issue(
                reservation,
                authority=authority,
                registry=self,
                provider=provider,
            )
        except Exception:
            authority_issuer.release_claim(authority, reservation, reservation._seal_sha256)
            raise

    def reserve_native(
        self,
        authority: PixelFitEvidenceAuthority | SupervisedFitEvidenceAuthority,
        *,
        requested_launch_mode: str,
        pixel_entry_channel_features: tuple[QuPathPixelEntryChannelFeatures, ...] = (),
    ) -> TrainingEngineExecutionReservation:
        """Close native configuration before resolving or claiming a provider."""

        if type(authority) is PixelFitEvidenceAuthority:
            validate_authority = _validate_pixel_fit_evidence_authority
        elif type(authority) is SupervisedFitEvidenceAuthority:
            validate_authority = _validate_supervised_fit_evidence_authority
        else:
            raise BatchTrainingError(
                "native engine reservation requires an exact fit evidence authority"
            )
        plan, _evidence = validate_authority(authority, require_current=True)
        projection = QuPathNativeTrainingFitPlanProjection.create(
            fit_plan=TrainingFitPlanProjection.create(plan),
            pixel_entry_channel_features=pixel_entry_channel_features,
        )
        return self.reserve(
            authority,
            requested_launch_mode=requested_launch_mode,
            _native_fit_plan_projection=projection,
        )

    def prepare_native_training_dispatch(
        self,
        *,
        reservation: TrainingEngineExecutionReservation,
        readiness: TrainingEngineReadinessAuthority,
        runner: object,
    ) -> object:
        """Prepare the authenticated scientific request before acquiring a project lease.

        Keep the accepted plan's semantic eligibility unchanged. Declare its
        role and projected material type IDs separately so a secondary parent
        named Tissue cannot select primary training or review behavior.
        """

        from mif_pipeline.qupath_runtime.runner import (
            QuPathRunner,
            _issue_native_training_dispatch_authority,
        )

        if type(runner) is not QuPathRunner:
            raise BatchTrainingError("native fit dispatch requires an exact QuPath runner")
        checked_readiness = _READINESS_ISSUER.validate(
            readiness,
            reservation=reservation,
            registry=self,
        )
        checked = _RESERVATION_ISSUER.validate_ready_binding(
            reservation,
            checked_readiness,
            registry=self,
        )
        native_projection = checked._native_fit_plan_projection
        evidence = checked_readiness._training_evidence
        fit_plan = checked_readiness._fit_plan
        material_parent_type_ids = _native_material_parent_type_ids(
            fit_plan.classifier_kind, fit_plan.eligible_parent_classes
        )
        if type(native_projection) is not QuPathNativeTrainingFitPlanProjection:
            raise BatchTrainingError("native fit dispatch lacks its exact native projection")
        if type(evidence) is MultiImagePixelFitEvidence:
            feature_spec = native_projection.pixel_channel_features
            if feature_spec is None:
                raise BatchTrainingError("pixel native training lacks channel features")
            feature_mapping = feature_spec.to_mapping()
            training_entries = [
                {
                    "entry_kind": "pixel",
                    "entry_state": state.to_mapping(),
                    "evidence_rows": [
                        row.to_mapping()
                        for row in evidence.rows
                        if row.project_entry_id == state.project_entry_id
                    ],
                    "channel_features": feature_mapping,
                }
                for state in evidence.entry_states
            ]
            classifier_kind = "pixel"
        elif type(evidence) is MultiImageSupervisedFitEvidence:
            configuration = native_estimator_configuration(native_projection.model_configuration)
            if type(configuration) not in (
                QuPathObjectRTreesConfiguration,
                QuPathObjectSvmConfiguration,
                QuPathObjectLogisticRegressionConfiguration,
            ):
                raise BatchTrainingError("supervised native training configuration is invalid")
            feature_mapping = _native_object_feature_spec(evidence, configuration)
            training_entries = [
                _native_object_training_entry(snapshot) for snapshot in evidence.entries
            ]
            classifier_kind = "supervised_phenotype"
        else:
            raise BatchTrainingError("native training evidence projection is invalid")
        model = native_projection.model_configuration.to_mapping()
        request_core = _json_object(
            {
                "fit_plan_sha256": fit_plan.fit_plan_sha256,
                "fit_execution_sha256": checked.fit_execution_sha256,
                "classifier_id": fit_plan.classifier_id,
                "classifier_kind": classifier_kind,
                "classifier_role": fit_plan.classifier_kind.value,
                "classifier_engine": "qupath_native",
                "native_classifier_type": checked.native_classifier_type,
                "classifier_version": checked.classifier_version,
                # QuPath appends .json to this resource name; the logical
                # classifier version is independent of the reserved leaf.
                "resource_name": PurePosixPath(checked.project_relative_path).stem,
                "project_relative_path": checked.project_relative_path,
                "required_classes": list(fit_plan.required_classes),
                "eligible_parent_classes": list(fit_plan.eligible_parent_classes),
                "eligible_material_parent_type_ids": list(material_parent_type_ids),
                "training_entries": training_entries,
                "training_analysis_domain_bindings": [
                    item.to_mapping() for item in fit_plan.training_analysis_domain_bindings
                ],
                "training_material_evidence_sha256": (evidence.material_evidence_sha256),
                "feature_spec": feature_mapping,
                "feature_spec_sha256": _digest(feature_mapping),
                "model_configuration": model,
                "model_configuration_sha256": _digest(model),
                "random_seed": fit_plan.random_seed,
            },
            "native fit dispatch request core",
        )
        return _issue_native_training_dispatch_authority(
            registry=self,
            runner=runner,
            reservation=checked,
            readiness=checked_readiness,
            request=request_core,
        )

    def authenticate_native_training_dispatch_authority(
        self,
        *,
        reservation: object,
        readiness: object,
        runner: object,
        request_core_fields: Mapping[str, object],
    ) -> object:
        """Authenticate the live registry-issued dispatch envelope."""
        from mif_pipeline.qupath_runtime.runner import QuPathRunner

        if type(runner) is not QuPathRunner:
            raise BatchTrainingError("native-training dispatch authority is foreign")
        _READINESS_ISSUER.validate(
            readiness,
            reservation=reservation,
            registry=self,
        )
        _RESERVATION_ISSUER.validate_ready_binding(
            reservation,
            readiness,
            registry=self,
        )
        if not isinstance(request_core_fields, Mapping):
            raise BatchTrainingError("native-training dispatch projection is invalid")
        return self

    def assess_readiness(
        self,
        reservation: TrainingEngineExecutionReservation,
    ) -> TrainingEngineReadinessAuthority | TrainingEngineReadinessResult:
        """Assess one current headless reservation before project acquisition."""

        try:
            checked = _RESERVATION_ISSUER.validate(
                reservation,
                registry=self,
                required_state="active",
            )
        except Exception as exc:
            try:
                issued = _RESERVATION_ISSUER._validate_issued_identity(reservation, registry=self)
                state = _RESERVATION_ISSUER.lifecycle_state(issued, registry=self)
            except Exception as invalid:
                raise BatchTrainingError(
                    "training engine readiness reservation is invalid"
                ) from invalid
            if state != "active":
                raise BatchTrainingError(
                    "training engine reservation was already assessed; replay rejected"
                ) from exc
            try:
                abandoned = _RESERVATION_ISSUER.abandon(issued)
            except BaseException as cleanup:
                _add_bounded_cleanup_note(
                    exc,
                    cleanup,
                    context="pre-assessment reservation cleanup also failed",
                )
                raise exc
            if not abandoned:
                raise BatchTrainingError(
                    "training engine reservation assessment transition was lost"
                ) from exc
            return TrainingEngineReadinessResult(
                fit_plan_sha256=issued._fit_plan_sha256,
                disposition=TrainingEngineReadinessDisposition.REJECTED,
                interaction=None,
                error_code="provider_readiness_drift",
                error_message=("The provider or reservation changed before readiness assessment."),
            )
        if checked._requested_launch_mode != "headless":
            raise BatchTrainingError("reserved native fitting requires headless launch")
        checked = _RESERVATION_ISSUER.begin_assessment(checked, registry=self)

        def rejected(code: str, message: str) -> TrainingEngineReadinessResult:
            return TrainingEngineReadinessResult(
                fit_plan_sha256=checked._fit_plan_sha256,
                disposition=TrainingEngineReadinessDisposition.REJECTED,
                interaction=None,
                error_code=code,
                error_message=message,
            )

        terminalization_attempted = False
        try:
            result: object
            request_built = False
            evidence_authority = checked._authority
            try:
                if type(evidence_authority) is PixelFitEvidenceAuthority:
                    plan, evidence = _validate_pixel_fit_evidence_authority(
                        evidence_authority, require_current=True
                    )
                    is_supervised = False
                elif type(evidence_authority) is SupervisedFitEvidenceAuthority:
                    plan, evidence = _validate_supervised_fit_evidence_authority(
                        evidence_authority, require_current=True
                    )
                    is_supervised = True
                else:
                    raise BatchTrainingError("training engine reservation authority is invalid")
                provider, capabilities = self._resolve_registration(
                    plan.classifier_engine,
                    plan.request.classifier_kind,
                )
                if provider is not checked._provider or capabilities is not checked._capabilities:
                    raise BatchTrainingError(
                        "training engine reservation provider registration drifted"
                    )
                fit_plan = TrainingFitPlanProjection.create(plan)
                if fit_plan.fit_plan_sha256 != checked._fit_plan_sha256:
                    raise BatchTrainingError("training engine reservation fit plan is stale")
                provider_request = object.__new__(TrainingEngineRequest)
                for name, value in {
                    "fit_plan": fit_plan,
                    "native_fit_plan": checked._native_fit_plan_projection,
                    "pixel_evidence": None if is_supervised else evidence,
                    "supervised_evidence": evidence if is_supervised else None,
                    "requested_launch_mode": "headless",
                    "fit_execution_sha256": checked._fit_execution_sha256,
                }.items():
                    object.__setattr__(provider_request, name, value)
                provider_request._validate_projection()
                request_built = True
                result = None
            except Exception:
                result = rejected(
                    "provider_readiness_drift",
                    "The reserved training evidence changed before readiness assessment.",
                )

            if result is None:
                try:
                    result = checked._provider.assess_readiness(provider_request)
                except Exception:
                    result = rejected(
                        "provider_readiness_failed",
                        "Training provider readiness assessment failed.",
                    )

            if request_built:
                try:
                    _RESERVATION_ISSUER.validate(
                        checked,
                        registry=self,
                        required_state="assessing",
                    )
                    current_provider, current_capabilities = self._resolve_registration(
                        plan.classifier_engine,
                        plan.request.classifier_kind,
                    )
                    if (
                        current_provider is not checked._provider
                        or current_capabilities is not checked._capabilities
                    ):
                        raise BatchTrainingError(
                            "training engine reservation provider registration drifted"
                        )
                except Exception:
                    result = rejected(
                        "provider_readiness_drift",
                        "The provider or reservation changed during readiness assessment.",
                    )

            try:
                if type(result) is not TrainingEngineReadinessResult:
                    raise BatchTrainingError("readiness result is not exact")
                canonical_result = TrainingEngineReadinessResult(
                    fit_plan_sha256=result.fit_plan_sha256,
                    disposition=result.disposition,
                    interaction=result.interaction,
                    error_code=result.error_code,
                    error_message=result.error_message,
                )
                if canonical_result != result or result.fit_plan_sha256 != checked._fit_plan_sha256:
                    raise BatchTrainingError("readiness result differs from request")
            except Exception:
                result = rejected(
                    "provider_readiness_malformed",
                    "Training provider returned an invalid readiness result.",
                )

            if result.disposition is TrainingEngineReadinessDisposition.READY:
                values = {
                    "_fit_execution_sha256": checked._fit_execution_sha256,
                    "_fit_plan_sha256": checked._fit_plan_sha256,
                    "_native_classifier_type": checked._native_classifier_type,
                    "_project_relative_path": checked._project_relative_path,
                    "_classifier_version": checked._classifier_version,
                    "_requested_launch_mode": checked._requested_launch_mode,
                    "_capabilities_sha256": checked._capabilities_sha256,
                    "_training_evidence_material_sha256": (evidence.material_evidence_sha256),
                    "_reservation_seal_sha256": checked._seal_sha256,
                    "_reservation": checked,
                    "_registry": self,
                    "_provider": checked._provider,
                    "_capabilities": checked._capabilities,
                    "_fit_plan": fit_plan,
                    "_training_evidence": evidence,
                    "_readiness_result": result,
                }
                readiness = object.__new__(TrainingEngineReadinessAuthority)
                for name, value in values.items():
                    object.__setattr__(readiness, name, value)
                object.__setattr__(
                    readiness,
                    "_seal_sha256",
                    _digest(_readiness_authority_seal_material(readiness)),
                )
                return _RESERVATION_ISSUER.complete_ready(
                    checked,
                    readiness,
                    registry=self,
                )

            terminalization_attempted = True
            _RESERVATION_ISSUER.terminalize_assessment(checked)
            return result
        except BaseException as primary:
            if not terminalization_attempted:
                try:
                    _RESERVATION_ISSUER.terminalize_assessment(checked)
                except BaseException as cleanup:
                    _add_bounded_cleanup_note(
                        primary,
                        cleanup,
                        context="readiness terminalization also failed",
                    )
            raise

    def execute(
        self,
        authority: PixelFitEvidenceAuthority | SupervisedFitEvidenceAuthority,
        *,
        requested_launch_mode: str,
    ) -> TrainingEngineResult:
        """Consume one current fit authority and dispatch a sanitized request."""

        mode = _text(requested_launch_mode, "requested_launch_mode")
        if mode not in {"interactive", "headless"}:
            raise BatchTrainingError("requested_launch_mode must be interactive or headless")
        authority_type = type(authority)
        if authority_type is PixelFitEvidenceAuthority:
            validate_authority = _validate_pixel_fit_evidence_authority
            consume_authority = _consume_pixel_fit_evidence_authority
            revoke_authority = _revoke_pixel_fit_evidence_authority
            is_supervised = False
        elif authority_type is SupervisedFitEvidenceAuthority:
            validate_authority = _validate_supervised_fit_evidence_authority
            consume_authority = _consume_supervised_fit_evidence_authority
            revoke_authority = _revoke_supervised_fit_evidence_authority
            is_supervised = True
        else:
            raise BatchTrainingError("engine execution requires an exact fit evidence authority")

        def revoke_without_masking() -> None:
            try:
                revoke_authority(authority)
            except Exception:
                # Revocation is best-effort cleanup and must never replace the
                # terminal pre-dispatch error that denied provider access.
                pass

        try:
            plan, evidence = validate_authority(authority, require_current=True)
        except Exception:
            revoke_without_masking()
            raise
        try:
            _retained_provider, retained_capabilities = self._retained_registration(
                plan.classifier_engine,
                plan.request.classifier_kind,
            )
        except Exception:
            revoke_without_masking()
            raise
        if retained_capabilities.artifact_kind is ClassifierArtifactKind.QUPATH_NATIVE:
            raise BatchTrainingError(
                "QuPath-native training requires reserve_native followed by readiness and reserved execution"
            )
        try:
            provider, capabilities = self._resolve_registration(
                plan.classifier_engine,
                plan.request.classifier_kind,
            )
        except Exception:
            revoke_without_masking()
            raise
        if mode == "headless" and not capabilities.supports_headless_training:
            revoke_without_masking()
            raise BatchTrainingError("training engine does not support headless launch")
        if mode == "interactive" and not capabilities.supports_interactive_launch:
            revoke_without_masking()
            raise BatchTrainingError("training engine does not support interactive launch")

        # The scheduler retains the exclusive workspace writer lease across
        # dispatch. This issuer lock repeats current-state authentication and
        # atomically consumes the reservation immediately before construction
        # of the capability-free provider value.
        current_plan, provider_evidence = consume_authority(authority)
        if current_plan != plan or provider_evidence != evidence:
            raise BatchTrainingError(
                "fit authority changed between preflight and consumption; "
                "a fresh partition/attempt is required"
            )
        fit_plan = TrainingFitPlanProjection.create(current_plan)
        provider_request = object.__new__(TrainingEngineRequest)
        provider_values = {
            "fit_plan": fit_plan,
            "native_fit_plan": None,
            "pixel_evidence": None if is_supervised else provider_evidence,
            "supervised_evidence": provider_evidence if is_supervised else None,
            "requested_launch_mode": mode,
            "fit_execution_sha256": None,
        }
        for name, value in provider_values.items():
            object.__setattr__(provider_request, name, value)
        provider_request._validate_projection()
        try:
            result = provider.execute(provider_request)
        except Exception as exc:
            raise BatchTrainingError(
                "training provider failed after evidence reservation "
                "consumption; a fresh partition/attempt is required"
            ) from exc
        if type(result) is not TrainingEngineResult:
            raise BatchTrainingError("training engine provider must return TrainingEngineResult")
        if result.fit_plan_sha256 != fit_plan.fit_plan_sha256:
            raise BatchTrainingError("provider result fit plan differs from request")
        if (
            result.disposition is TrainingEngineDisposition.COMPLETED
            and result.artifact is not None
        ):
            artifact = result.artifact.raw_native_artifact
            if capabilities.artifact_kind is not ClassifierArtifactKind.QUPATH_NATIVE:
                raise BatchTrainingError("completed artifact kind differs from engine capabilities")
            if (
                artifact.classifier_id != fit_plan.classifier_id
                or artifact.classifier_kind is not fit_plan.classifier_kind
            ):
                raise BatchTrainingError("completed raw artifact differs from fit plan identity")
        return result

    def authenticate_native_fit_commit(
        self,
        *,
        reservation: TrainingEngineExecutionReservation,
        readiness: TrainingEngineReadinessAuthority,
        completion: object,
        project_commit: object,
    ) -> NativeFitRegistryCommitAuthority:
        """Consume one ready claim only after its exact runtime fit is durable."""

        from mif_pipeline.qupath_runtime.native_classifier_fit import (
            QuPathNativeClassifierFitCommit,
        )
        from mif_pipeline.qupath_runtime.runner import (
            QuPathNativeTrainingProcessCompletion,
        )

        if (
            type(reservation) is not TrainingEngineExecutionReservation
            or type(readiness) is not TrainingEngineReadinessAuthority
            or type(completion) is not QuPathNativeTrainingProcessCompletion
            or type(project_commit) is not QuPathNativeClassifierFitCommit
        ):
            raise BatchTrainingError(
                "native fit commit requires exact registry and runtime authorities"
            )
        checked_readiness = _READINESS_ISSUER.validate_after_project_commit(
            readiness,
            reservation=reservation,
            registry=self,
        )
        checked = _RESERVATION_ISSUER.validate_ready_binding_after_project_commit(
            reservation,
            checked_readiness,
            registry=self,
        )
        request = completion.request
        material = project_commit.material_record
        expected_runtime_kind = (
            "supervised_phenotype"
            if checked_readiness._fit_plan.classifier_kind is ClassifierKind.SUPERVISED_PHENOTYPE
            else "pixel"
        )
        if (
            completion.disposition != "completed"
            or completion.result is None
            or project_commit._lease._provider is not project_commit._provider
            or project_commit._issuer
            is not getattr(project_commit._provider, "_provider_issuer", None)
            or material.fit_plan_sha256 != checked._fit_plan_sha256
            or material.fit_execution_sha256 != checked._fit_execution_sha256
            or material.training_material_evidence_sha256
            != checked_readiness._training_evidence_material_sha256
            or material.classifier_id != checked_readiness._fit_plan.classifier_id
            or material.classifier_kind != expected_runtime_kind
            or material.native_classifier_type != checked._native_classifier_type
            or material.classifier_version != checked._classifier_version
            or material.project_relative_path != checked._project_relative_path
            or request.fit_plan_sha256 != material.fit_plan_sha256
            or request.fit_execution_sha256 != material.fit_execution_sha256
            or request.training_material_evidence_sha256
            != material.training_material_evidence_sha256
            or request.classifier_id != material.classifier_id
            or request.classifier_kind != material.classifier_kind
            or request.native_classifier_type != material.native_classifier_type
            or request.classifier_version != material.classifier_version
            or request.project_relative_path != material.project_relative_path
            or material.request_envelope_sha256 != request.request_envelope_sha256
            or material.result_envelope_sha256 != completion.result.result_envelope_sha256
            or completion.spawn_attempt is None
            or material.spawn_attempt_sha256 != completion.spawn_attempt.spawn_attempt_sha256
            or completion.process_launch is None
            or material.process_launch_sha256 != completion.process_launch.process_launch_sha256
            or completion.process_drain is None
            or material.process_drain_sha256 != completion.process_drain.process_drain_sha256
            or completion.result.native_classifier_sha256 != material.native_classifier_sha256
            or completion.result.native_classifier_byte_count
            != material.native_classifier_byte_count
        ):
            raise BatchTrainingError("native fit runtime commit differs from its ready reservation")
        capabilities = checked._capabilities
        values = {
            "fit_execution_sha256": checked._fit_execution_sha256,
            "fit_plan_sha256": checked._fit_plan_sha256,
            "training_material_evidence_sha256": (
                checked_readiness._training_evidence_material_sha256
            ),
            "engine_id": capabilities.engine_id,
            "engine_version": capabilities.engine_version,
            "engine_capabilities_sha256": capabilities.capabilities_sha256,
            "_reservation": checked,
            "_readiness": checked_readiness,
            "_completion": completion,
            "_project_commit": project_commit,
            "_registry": self,
            "_issuer": _NATIVE_FIT_REGISTRY_ISSUER,
        }
        authority = object.__new__(NativeFitRegistryCommitAuthority)
        for name, value in values.items():
            object.__setattr__(authority, name, value)
        object.__setattr__(
            authority,
            "_seal_sha256",
            _digest(_native_fit_registry_authority_material(authority)),
        )
        _RESERVATION_ISSUER.consume_ready_fit_commit(
            checked,
            checked_readiness,
            registry=self,
        )
        return authority

    def validate_native_fit_commit_authority(
        self,
        authority: NativeFitRegistryCommitAuthority,
        *,
        project_commit: object,
    ) -> NativeFitRegistryCommitAuthority:
        """Revalidate the same in-process commit authority before state CAS."""

        if (
            type(authority) is not NativeFitRegistryCommitAuthority
            or authority._issuer is not _NATIVE_FIT_REGISTRY_ISSUER
            or authority._registry is not self
            or authority._project_commit is not project_commit
            or authority._seal_sha256 != _digest(_native_fit_registry_authority_material(authority))
        ):
            raise BatchTrainingError("native fit registry commit authority is invalid")
        material = authority._project_commit.material_record
        if (
            authority.fit_execution_sha256 != material.fit_execution_sha256
            or authority.fit_plan_sha256 != material.fit_plan_sha256
            or authority.training_material_evidence_sha256
            != material.training_material_evidence_sha256
        ):
            raise BatchTrainingError("native fit registry commit authority differs")
        return authority

    def authenticate_checkpointed_native_fit(
        self,
        entry: object,
        *,
        current_parent: object,
    ) -> None:
        """Rebuild and validate a current plan/provider projection without fitting."""

        from ..batch_training_checkpoint import BatchNativeFitCheckpointEntry
        from .parent_execution import CurrentTrainingParentExecution

        if (
            type(entry) is not BatchNativeFitCheckpointEntry
            or type(current_parent) is not CurrentTrainingParentExecution
        ):
            raise BatchTrainingError(
                "checkpointed native fit requires exact state and parent records"
            )
        plans = tuple(
            plan
            for plan in current_parent.training_plan_set.plan_set.plans
            if plan.request.classifier_id == entry.classifier_id
        )
        if len(plans) != 1:
            raise BatchTrainingError("checkpointed native fit is outside the current training plan")
        fit_plan = TrainingFitPlanProjection.create(plans[0])
        _provider, capabilities = self._resolve_registration(
            fit_plan.classifier_engine,
            fit_plan.classifier_kind,
        )
        expected_type = _native_classifier_target_family(fit_plan.classifier_kind)
        expected_path = (
            "classifiers/object_classifiers/"
            if expected_type == "object"
            else "classifiers/pixel_classifiers/"
        ) + f"mif-{entry.fit_execution_sha256}.json"
        if (
            entry.classifier_kind != fit_plan.classifier_kind.value
            or entry.fit_plan_sha256 != fit_plan.fit_plan_sha256
            or entry.engine_id != capabilities.engine_id
            or entry.engine_version != capabilities.engine_version
            or entry.engine_capabilities_sha256 != capabilities.capabilities_sha256
            or entry.native_classifier_type != expected_type
            or entry.classifier_version != f"mif-fit-{entry.fit_execution_sha256}"
            or entry.project_relative_path != expected_path
        ):
            raise BatchTrainingError("checkpointed native fit plan or provider projection changed")

    def _retained_registration(
        self,
        engine_id: str,
        classifier_kind: ClassifierKind,
    ) -> tuple[TrainingEngineProvider, TrainingEngineCapabilities]:
        """Classify an exact registered route without live provider access."""

        requested_engine = _text(engine_id, "engine_id")
        if not isinstance(classifier_kind, ClassifierKind):
            raise BatchTrainingError("classifier_kind must be a ClassifierKind")
        for provider, capabilities in self._registrations:
            if (
                capabilities.engine_id == requested_engine
                and classifier_kind in capabilities.classifier_kinds
            ):
                return provider, capabilities
        raise BatchTrainingError("training engine is not registered for the classifier kind")

    def _resolve_registration(
        self,
        engine_id: str,
        classifier_kind: ClassifierKind,
    ) -> tuple[TrainingEngineProvider, TrainingEngineCapabilities]:
        requested_engine = _text(engine_id, "engine_id")
        if not isinstance(classifier_kind, ClassifierKind):
            raise BatchTrainingError("classifier_kind must be a ClassifierKind")
        for provider, capabilities in self._registrations:
            current_capabilities = provider.capabilities
            if (
                type(current_capabilities) is not TrainingEngineCapabilities
                or current_capabilities != capabilities
                or current_capabilities.capabilities_sha256 != capabilities.capabilities_sha256
            ):
                raise BatchTrainingError("registered training engine capabilities drifted")
            if (
                capabilities.engine_id == requested_engine
                and classifier_kind in capabilities.classifier_kinds
            ):
                return provider, capabilities
        raise BatchTrainingError("training engine is not registered for the classifier kind")


def build_prototype_training_engine_provider_registry() -> TrainingEngineProviderRegistry:
    """Return the closed pixel RF/SVM and supervised RF/SVM/LR native registry."""

    providers = tuple(
        QuPathNativeTrainingEngineProvider(
            TrainingEngineCapabilities.create(
                engine_id=engine_id,
                engine_version=_QUPATH_NATIVE_VERSION_BY_ENGINE[engine_id],
                classifier_kinds=kinds,
                artifact_kind=ClassifierArtifactKind.QUPATH_NATIVE,
                execution_mode=TrainingExecutionMode.INTERACTIVE_NATIVE,
                supports_interactive_launch=True,
                supports_headless_training=True,
                can_recover_evidence=False,
                can_report_training_diagnostics=True,
                engine_requirements=_QUPATH_NATIVE_REQUIREMENTS_BY_ENGINE[engine_id],
            )
        )
        for engine_id, kinds in (
            (
                "qupath-kronos-rtrees-classifier",
                (ClassifierKind.PRIMARY_COMPARTMENT, ClassifierKind.SECONDARY_STRUCTURE),
            ),
            (
                "qupath-kronos-svm-classifier",
                (ClassifierKind.PRIMARY_COMPARTMENT, ClassifierKind.SECONDARY_STRUCTURE),
            ),
            (
                "qupath-pixel-classifier",
                (
                    ClassifierKind.PRIMARY_COMPARTMENT,
                    ClassifierKind.SECONDARY_STRUCTURE,
                ),
            ),
            (
                "qupath-object-classifier",
                (ClassifierKind.SUPERVISED_PHENOTYPE,),
            ),
            (
                "qupath-object-svm-classifier",
                (ClassifierKind.SUPERVISED_PHENOTYPE,),
            ),
            (
                "qupath-object-logistic-regression-classifier",
                (ClassifierKind.SUPERVISED_PHENOTYPE,),
            ),
            (
                "qupath-pixel-svm-classifier",
                (ClassifierKind.PRIMARY_COMPARTMENT, ClassifierKind.SECONDARY_STRUCTURE),
            ),
        )
    )
    return TrainingEngineProviderRegistry(providers)


def validate_authorized_training_interaction_response(
    authority: AuthorizedTrainingInteractionResponse,
    *,
    expected_context: BatchNodeExecutionContext,
) -> AuthorizedTrainingInteractionResponse:
    """Validate one state-issued capability with training-domain failures."""

    try:
        return _validate_authorized_training_interaction_response(
            authority, expected_context=expected_context
        )
    except (AttributeError, BatchInteractionAuthorityError, TypeError, ValueError) as exc:
        raise BatchTrainingError(str(exc)) from exc


__all__ = [
    "AuthorizedTrainingInteractionResponse",
    "QuPathNativeTrainingFitPlanProjection",
    "RawNativeClassifierArtifact",
    "NativeFitRegistryCommitAuthority",
    "TrainingEngineArtifactResult",
    "TrainingEngineCapabilities",
    "TrainingEngineDisposition",
    "TrainingEngineExecutionReservation",
    "TrainingEngineInteractionRequest",
    "TrainingEngineProvider",
    "TrainingEngineProviderRegistry",
    "QuPathNativeTrainingEngineProvider",
    "build_prototype_training_engine_provider_registry",
    "TrainingEngineReadinessAuthority",
    "TrainingEngineReadinessDisposition",
    "TrainingEngineReadinessResult",
    "TrainingEngineRequest",
    "TrainingEngineResult",
    "TrainingExecutionMode",
    "TrainingFitPlanProjection",
    "validate_authorized_training_interaction_response",
]
