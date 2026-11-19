"""Build shared classifier plans from the declared image, class and subject inventory.

Planning checks compatible channels/features, reference requirements, application scope and
subject separation. It records fixed parameters or the exact search candidates before
image-specific evidence collection. A shared plan names all participants rather than
replacing the group with a representative image."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import ClassVar, Literal

from ...pipeline_core.config import (
    ChannelSpec,
    ClassifierMode,
    PrimaryClassifierSettings,
    require_primary_settings_compatible,
    SecondaryStructureSpec,
)
from ..batch_artifacts import (
    AnalysisDomainBinding,
    ArtifactConfigurationBinding,
    ArtifactIdentityContract,
    BatchArtifactError,
    BatchArtifactRef,
    BatchCommittedArtifactAuthority,
    validate_artifact_ref,
    validate_committed_artifact_authority_set,
)
from ..batch_analysis_domain_artifacts import (
    decode_committed_accepted_analysis_domain,
)
from ..batch_analysis_preflight import (
    BatchAnalysisPreflightError,
    resolve_analysis_channels,
    validate_analysis_frame_artifact_ref,
)
from ..batch_manifest import (
    BatchManifestError,
    ReusableArtifactDeclaration,
    ResolvedBatchSample,
    ValidatedBatchManifest,
    revalidate_retained_manifest,
)
from ..batch_project import (
    BatchProjectError,
    reopen_project_catalogue_entry_artifact,
    validate_project_catalogue_selection_witness,
)

from ._shared import (
    BatchTrainingError,
    ClassifierKind,
    EvaluationIntent,
    _SPLITS,
    _SPLIT_RANK,
    _array,
    _binding_identity,
    _binding_owner_key,
    _binding_sort_key,
    _closed_mapping,
    _digest,
    _enum,
    _export_json,
    _integer,
    _json_object,
    _sha256,
    _string_tuple,
    _text,
)
from .primary_policy import primary_training_required_classes
from ..batch_training_settings import TrainingSettingsRevision

TRAINING_PLAN_SET_IDENTITY_FIELDS: tuple[str, ...] = tuple(
    sorted(
        (
            "analysis_domain_binding_set_sha256",
            "batch_id",
            "batch_plan_sha256",
            "body_byte_count",
            "body_sha256",
            "classifier_engines",
            "classifier_ids",
            "classifier_kind",
            "configuration_binding_set_sha256",
            "normalized_manifest_sha256",
            "participant_ids",
            "project_entry_ids",
            "source_artifact_ref_set_sha256",
            "store_identity_sha256",
            "store_relative_locator",
            "training_plan_ids",
            "training_plan_set_sha256",
            "training_plan_sha256s",
            "validation_domain_id",
        )
    )
)

DEFERRED_PRIMARY_CANDIDATE_ID = "primary-stage-settings"


# ==============================================================================
#                                    Planning
# ==============================================================================


def _primary_application_channels(sample, frame_ref: BatchArtifactRef) -> tuple[ChannelSpec, ...]:
    """Project processed channels from an already authenticated application frame."""
    try:
        return resolve_analysis_channels(
            sample.config,
            frame_ref,
            sample_id=sample.sample_id,
            validation_domain_id=sample.validation_domain_id,
        )
    except BatchAnalysisPreflightError as exc:
        raise BatchTrainingError("primary application analysis channels are invalid") from exc


def _require_primary_application_channels(
    sample, frame_ref: BatchArtifactRef, expected: tuple[ChannelSpec, ...]
) -> None:
    if _primary_application_channels(sample, frame_ref) != expected:
        raise BatchTrainingError("application channels differ from canonical analysis frame")


def _entry_sort_key(entry: "TrainingPlanEntry") -> tuple[object, ...]:
    return (
        _SPLIT_RANK[entry.split],
        entry.project_entry_id,
        entry.sample_id,
        entry.slide_id,
        entry.subject_id,
        entry.image_identity_sha256,
    )


@dataclass(frozen=True, slots=True)
class TrainingPlanEntryAuthority:
    """Typed in-process sources used only while authoring a training plan."""

    project_catalogue_entry: BatchArtifactRef
    analysis_frame: BatchArtifactRef
    accepted_analysis_domain_authorities: tuple[BatchCommittedArtifactAuthority, ...]
    additional_source_authorities: tuple[BatchCommittedArtifactAuthority, ...] = ()

    def __post_init__(self) -> None:
        try:
            reopen_project_catalogue_entry_artifact(self.project_catalogue_entry)
            validate_analysis_frame_artifact_ref(self.analysis_frame)
            domains = validate_committed_artifact_authority_set(
                self.accepted_analysis_domain_authorities
            )
            additional = validate_committed_artifact_authority_set(
                self.additional_source_authorities
            )
        except (BatchArtifactError, BatchProjectError, TypeError, ValueError) as exc:
            raise BatchTrainingError(
                "training entry sources require public owner and committed state authority"
            ) from exc
        object.__setattr__(self, "accepted_analysis_domain_authorities", domains)
        object.__setattr__(self, "additional_source_authorities", additional)


@dataclass(frozen=True, slots=True)
class TrainingPlanEntry:
    project_entry_id: str
    sample_id: str
    slide_id: str
    subject_id: str
    split: Literal["training", "validation", "test"]
    image_identity_sha256: str

    def __post_init__(self) -> None:
        for name in ("project_entry_id", "sample_id", "slide_id", "subject_id"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.split not in _SPLITS:
            raise BatchTrainingError("split must be training, validation, or test")
        object.__setattr__(
            self,
            "image_identity_sha256",
            _sha256(self.image_identity_sha256, "image_identity_sha256"),
        )

    @classmethod
    def from_mapping(cls, value: object) -> "TrainingPlanEntry":
        fields = frozenset(
            {
                "project_entry_id",
                "sample_id",
                "slide_id",
                "subject_id",
                "split",
                "image_identity_sha256",
            }
        )
        mapping = _closed_mapping(value, fields=fields, field="training_plan_entry")
        return cls(**{name: mapping[name] for name in fields})  # type: ignore[arg-type]

    def to_mapping(self) -> dict[str, object]:
        return {
            "project_entry_id": self.project_entry_id,
            "sample_id": self.sample_id,
            "slide_id": self.slide_id,
            "subject_id": self.subject_id,
            "split": self.split,
            "image_identity_sha256": self.image_identity_sha256,
        }


@dataclass(frozen=True, slots=True)
class TrainingPlanApplicationEntry:
    """Role-free identity and semantic channel inventory for one application image."""

    project_entry_id: str
    sample_id: str
    slide_id: str
    subject_id: str
    image_identity_sha256: str
    channels: tuple[ChannelSpec, ...]

    def __post_init__(self) -> None:
        for name in ("project_entry_id", "sample_id", "slide_id", "subject_id"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(
            self,
            "image_identity_sha256",
            _sha256(self.image_identity_sha256, "image_identity_sha256"),
        )
        channels = tuple(self.channels)
        if not channels or not all(type(item) is ChannelSpec for item in channels):
            raise BatchTrainingError("application channels require typed ChannelSpec rows")
        if len({item.channel_id for item in channels}) != len(channels):
            raise BatchTrainingError("application channel IDs must be unique")
        object.__setattr__(self, "channels", channels)

    @classmethod
    def from_mapping(cls, value: object) -> "TrainingPlanApplicationEntry":
        row = _closed_mapping(
            value, fields=frozenset(cls.__dataclass_fields__), field="application_entry"
        )
        try:
            channels = tuple(
                ChannelSpec.from_mapping(item, field="application_entry.channels")
                for item in _array(row["channels"], "application_entry.channels")
            )
        except ValueError as exc:
            raise BatchTrainingError("application channels are invalid") from exc
        return cls(
            row["project_entry_id"],
            row["sample_id"],
            row["slide_id"],
            row["subject_id"],
            row["image_identity_sha256"],
            channels,
        )  # type: ignore[arg-type]

    def to_mapping(self) -> dict[str, object]:
        return {
            "project_entry_id": self.project_entry_id,
            "sample_id": self.sample_id,
            "slide_id": self.slide_id,
            "subject_id": self.subject_id,
            "image_identity_sha256": self.image_identity_sha256,
            "channels": [item.to_mapping() for item in self.channels],
        }


@dataclass(frozen=True, slots=True)
class TrainingPlanRequest:
    batch_id: str
    normalized_manifest_sha256: str
    classifier_id: str
    classifier_kind: ClassifierKind
    classifier_engine: str
    evaluation_intent: EvaluationIntent
    validation_domain_id: str
    entries: tuple[TrainingPlanEntry, ...]
    analysis_domain_bindings: tuple[AnalysisDomainBinding, ...]
    required_classes: tuple[str, ...]
    eligible_parent_classes: tuple[str, ...]
    channel_requirements: Mapping[str, object] | None
    feature_requirements: Mapping[str, object] | None
    model_configuration: Mapping[str, object]
    random_seed: int
    primary_settings: PrimaryClassifierSettings | None = None
    application_entries: tuple[TrainingPlanApplicationEntry, ...] = ()
    application_analysis_domain_bindings: tuple[AnalysisDomainBinding, ...] = ()
    inherited_reference_selection_sha256: str | None = None

    def __post_init__(self) -> None:
        """Validate the TrainingPlanRequest fields when the record is constructed.

        The checks cover ``entries``, ``evaluation_intent``, ``analysis_domain_bindings``,
        ``required_classes``, ``eligible_parent_classes``, ``classifier_kind``. Invalid
        field types, values or combinations raise the declared validation error before this
        instance can be used by its caller."""

        for name in (
            "batch_id",
            "classifier_id",
            "classifier_engine",
            "validation_domain_id",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(
            self,
            "normalized_manifest_sha256",
            _sha256(self.normalized_manifest_sha256, "normalized_manifest_sha256"),
        )
        if not isinstance(self.classifier_kind, ClassifierKind):
            raise BatchTrainingError("classifier_kind must be a ClassifierKind")
        if self.inherited_reference_selection_sha256 is not None:
            _sha256(self.inherited_reference_selection_sha256, "inherited_reference_selection_sha256")
            if self.classifier_kind is not ClassifierKind.SUPERVISED_PHENOTYPE:
                raise BatchTrainingError("Only supervised cell plans can inherit reference selections")
        if not isinstance(self.evaluation_intent, EvaluationIntent):
            raise BatchTrainingError("evaluation_intent must be an EvaluationIntent")
        entries = tuple(self.entries)
        if not entries or not all(isinstance(item, TrainingPlanEntry) for item in entries):
            raise BatchTrainingError("entries must contain typed training plan entries")
        role_keys = tuple((item.project_entry_id, item.split) for item in entries)
        if len(set(role_keys)) != len(role_keys):
            raise BatchTrainingError("entries must contain unique project-entry role keys")
        identity_by_entry: dict[str, tuple[str, str, str, str]] = {}
        for entry in entries:
            identity = (
                entry.sample_id,
                entry.slide_id,
                entry.subject_id,
                entry.image_identity_sha256,
            )
            previous = identity_by_entry.setdefault(entry.project_entry_id, identity)
            if previous != identity:
                raise BatchTrainingError(
                    "project_entry_id roles must retain exact common entry identity"
                )
        splits = {item.split for item in entries}
        if self.evaluation_intent is EvaluationIntent.TRAINING_ONLY:
            if splits != {"training"}:
                raise BatchTrainingError(
                    "training-only entries require training and forbid validation or test"
                )
        elif self.evaluation_intent is EvaluationIntent.INDEPENDENT_PERFORMANCE:
            if not {"training", "test"} <= splits:
                raise BatchTrainingError("independent entries require training and test roles")
        elif not {"training", "validation"} <= splits:
            raise BatchTrainingError("entries require nonempty training and validation roles")
        bindings = tuple(self.analysis_domain_bindings)
        if not bindings or not all(isinstance(item, AnalysisDomainBinding) for item in bindings):
            raise BatchTrainingError("analysis_domain_bindings must contain typed bindings")
        binding_identities: dict[tuple[object, ...], bytes] = {}
        for binding in bindings:
            key = _binding_owner_key(binding)
            identity = _binding_identity(binding)
            previous = binding_identities.get(key)
            if previous is not None:
                if previous != identity:
                    raise BatchTrainingError(
                        "analysis_domain_bindings contain an ambiguous owner binding"
                    )
                raise BatchTrainingError("analysis_domain_bindings must be unique")
            binding_identities[key] = identity
        entries_by_id = {item.project_entry_id: item for item in entries}
        covered_entry_ids: set[str] = set()
        for binding in bindings:
            entry = entries_by_id.get(binding.project_entry_id)
            if entry is None:
                raise BatchTrainingError(
                    "analysis_domain_bindings contain an unowned project_entry_id"
                )
            if (binding.sample_id, binding.slide_id) != (
                entry.sample_id,
                entry.slide_id,
            ):
                raise BatchTrainingError(
                    "analysis domain binding sample_id or slide_id is not authoritative"
                )
            covered_entry_ids.add(binding.project_entry_id)
        if covered_entry_ids != set(entries_by_id):
            raise BatchTrainingError("every selected entry requires exact binding coverage")
        required = _string_tuple(
            self.required_classes,
            "required_classes",
            nonempty=True,
            canonical=False,
        )
        parents = _string_tuple(
            self.eligible_parent_classes,
            "eligible_parent_classes",
            nonempty=True,
            canonical=False,
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
                    "supervised phenotype requires feature_requirements and forbids "
                    "channel_requirements"
                )
        elif channels is None or features is not None:
            raise BatchTrainingError(
                "pixel classifier requires channel_requirements and forbids feature_requirements"
            )
        object.__setattr__(self, "entries", entries)
        object.__setattr__(self, "analysis_domain_bindings", bindings)
        object.__setattr__(self, "required_classes", required)
        object.__setattr__(self, "eligible_parent_classes", parents)
        object.__setattr__(self, "channel_requirements", channels)
        object.__setattr__(self, "feature_requirements", features)
        object.__setattr__(
            self,
            "model_configuration",
            _json_object(self.model_configuration, "model_configuration"),
        )
        object.__setattr__(self, "random_seed", _integer(self.random_seed, "random_seed"))
        if (
            self.primary_settings is not None
            and type(self.primary_settings) is not PrimaryClassifierSettings
        ):
            raise BatchTrainingError("primary_settings must be concrete typed settings")
        application_entries = tuple(self.application_entries)
        application_bindings = tuple(self.application_analysis_domain_bindings)
        if not all(type(item) is TrainingPlanApplicationEntry for item in application_entries):
            raise BatchTrainingError("application_entries require role-free typed identities")
        if not all(type(item) is AnalysisDomainBinding for item in application_bindings):
            raise BatchTrainingError("application domain bindings require typed identities")
        if len({item.project_entry_id for item in application_entries}) != len(application_entries):
            raise BatchTrainingError("application entries must be unique")
        if len({_binding_identity(item) for item in application_bindings}) != len(
            application_bindings
        ):
            raise BatchTrainingError("application domain bindings must be unique")
        application_by_id = {item.project_entry_id: item for item in application_entries}
        for binding in application_bindings:
            application = application_by_id.get(binding.project_entry_id)
            if application is None or (binding.sample_id, binding.slide_id) != (
                application.sample_id,
                application.slide_id,
            ):
                raise BatchTrainingError("application domain binding owner differs")
        if application_entries:
            selected_by_id = {item.project_entry_id: item for item in entries}
            if not set(selected_by_id) <= set(application_by_id):
                raise BatchTrainingError("selected training entries must be application members")
            if any(
                (item.sample_id, item.slide_id, item.subject_id, item.image_identity_sha256)
                != (
                    application_by_id[item.project_entry_id].sample_id,
                    application_by_id[item.project_entry_id].slide_id,
                    application_by_id[item.project_entry_id].subject_id,
                    application_by_id[item.project_entry_id].image_identity_sha256,
                )
                for item in entries
            ):
                raise BatchTrainingError(
                    "selected training entry identity differs from application"
                )
            if not set(application_by_id) <= {
                item.project_entry_id for item in application_bindings
            }:
                raise BatchTrainingError("every application entry requires domain coverage")
            if not {_binding_identity(item) for item in bindings} <= {
                _binding_identity(item) for item in application_bindings
            }:
                raise BatchTrainingError(
                    "selected domain bindings must belong to application bindings"
                )
        if self.classifier_kind is not ClassifierKind.PRIMARY_COMPARTMENT and (
            self.primary_settings is not None or application_entries or application_bindings
        ):
            raise BatchTrainingError(
                "non-primary plans forbid primary settings and application inventory"
            )
        if self.primary_settings is not None:
            if self.classifier_kind is not ClassifierKind.PRIMARY_COMPARTMENT:
                raise BatchTrainingError("primary settings require primary classifier kind")
            if self.primary_settings.primary_classifier.classifier_id != self.classifier_id:
                raise BatchTrainingError("primary classifier ID differs from selected settings")
            if self.primary_settings.primary_classifier.mode is not ClassifierMode.TRAIN_NEW:
                raise BatchTrainingError("primary training plan requires train_new settings")
            if (
                self.primary_settings.primary_classifier.validation_domain_id
                != self.validation_domain_id
            ):
                raise BatchTrainingError("primary settings validation domain differs from plan")
            if (
                primary_training_required_classes(self.primary_settings)
                != required
            ):
                raise BatchTrainingError("required classes differ from primary settings")
            if channels != {
                "channel_roles": dict(self.primary_settings.primary_classifier.channel_roles)
            }:
                raise BatchTrainingError("channel requirements differ from primary settings")
            for application in application_entries:
                try:
                    self.primary_settings.validate_channels(application.channels)
                except ValueError as exc:
                    raise BatchTrainingError(
                        "primary settings reference an unknown application channel"
                    ) from exc
        object.__setattr__(self, "application_entries", application_entries)
        object.__setattr__(self, "application_analysis_domain_bindings", application_bindings)

    @classmethod
    def from_mapping(cls, value: object) -> "TrainingPlanRequest":
        """Decode the serialized fields into a validated TrainingPlanRequest record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""

        fields = frozenset(cls.__dataclass_fields__) - {"inherited_reference_selection_sha256"}
        if isinstance(value, Mapping) and "inherited_reference_selection_sha256" in value:
            fields |= {"inherited_reference_selection_sha256"}
        mapping = _closed_mapping(value, fields=fields, field="training_plan_request")
        request = cls(
            batch_id=mapping["batch_id"],  # type: ignore[arg-type]
            normalized_manifest_sha256=mapping[
                "normalized_manifest_sha256"
            ],  # type: ignore[arg-type]
            classifier_id=mapping["classifier_id"],  # type: ignore[arg-type]
            classifier_kind=_enum(mapping["classifier_kind"], ClassifierKind, "classifier_kind"),
            classifier_engine=mapping["classifier_engine"],  # type: ignore[arg-type]
            evaluation_intent=_enum(
                mapping["evaluation_intent"], EvaluationIntent, "evaluation_intent"
            ),
            validation_domain_id=mapping["validation_domain_id"],  # type: ignore[arg-type]
            entries=tuple(
                TrainingPlanEntry.from_mapping(item)
                for item in _array(mapping["entries"], "entries")
            ),
            analysis_domain_bindings=_bindings_from_mapping(mapping["analysis_domain_bindings"]),
            required_classes=tuple(
                _array(mapping["required_classes"], "required_classes")
            ),  # type: ignore[arg-type]
            eligible_parent_classes=tuple(
                _array(mapping["eligible_parent_classes"], "eligible_parent_classes")
            ),  # type: ignore[arg-type]
            channel_requirements=mapping["channel_requirements"],  # type: ignore[arg-type]
            feature_requirements=mapping["feature_requirements"],  # type: ignore[arg-type]
            model_configuration=mapping["model_configuration"],  # type: ignore[arg-type]
            random_seed=mapping["random_seed"],  # type: ignore[arg-type]
            primary_settings=(
                None
                if mapping["primary_settings"] is None
                else PrimaryClassifierSettings.from_mapping(mapping["primary_settings"])
            ),
            application_entries=tuple(
                TrainingPlanApplicationEntry.from_mapping(item)
                for item in _array(mapping["application_entries"], "application_entries")
            ),
            application_analysis_domain_bindings=_bindings_from_mapping(
                mapping["application_analysis_domain_bindings"]
            ),
            inherited_reference_selection_sha256=mapping.get("inherited_reference_selection_sha256"),
        )
        if not _request_is_canonical(request):
            raise BatchTrainingError("training_plan_request must use canonical order")
        return request

    def to_mapping(self) -> dict[str, object]:
        return {
            "batch_id": self.batch_id,
            "normalized_manifest_sha256": self.normalized_manifest_sha256,
            "classifier_id": self.classifier_id,
            "classifier_kind": self.classifier_kind.value,
            "classifier_engine": self.classifier_engine,
            "evaluation_intent": self.evaluation_intent.value,
            "validation_domain_id": self.validation_domain_id,
            "entries": [item.to_mapping() for item in self.entries],
            "analysis_domain_bindings": [
                item.to_mapping() for item in self.analysis_domain_bindings
            ],
            "required_classes": list(self.required_classes),
            "eligible_parent_classes": list(self.eligible_parent_classes),
            "channel_requirements": _export_json(self.channel_requirements),
            "feature_requirements": _export_json(self.feature_requirements),
            "model_configuration": _export_json(self.model_configuration),
            "random_seed": self.random_seed,
            "primary_settings": (
                None if self.primary_settings is None else self.primary_settings.to_mapping()
            ),
            "application_entries": [item.to_mapping() for item in self.application_entries],
            "application_analysis_domain_bindings": [
                item.to_mapping() for item in self.application_analysis_domain_bindings
            ],
            **({"inherited_reference_selection_sha256": self.inherited_reference_selection_sha256}
               if self.inherited_reference_selection_sha256 is not None else {}),
        }


def _request_is_canonical(request: TrainingPlanRequest) -> bool:
    return (
        request.entries == tuple(sorted(request.entries, key=_entry_sort_key))
        and request.analysis_domain_bindings
        == tuple(sorted(request.analysis_domain_bindings, key=_binding_sort_key))
        and request.required_classes == tuple(sorted(request.required_classes))
        and request.eligible_parent_classes == tuple(sorted(request.eligible_parent_classes))
        and request.application_entries
        == tuple(sorted(request.application_entries, key=lambda item: item.project_entry_id))
        and request.application_analysis_domain_bindings
        == tuple(sorted(request.application_analysis_domain_bindings, key=_binding_sort_key))
    )


def _bindings_from_mapping(value: object) -> tuple[AnalysisDomainBinding, ...]:
    try:
        return tuple(
            AnalysisDomainBinding.from_mapping(item)
            for item in _array(value, "analysis_domain_bindings")
        )
    except BatchArtifactError as exc:
        raise BatchTrainingError("analysis_domain_bindings are invalid") from exc


def _normalized_request(request: TrainingPlanRequest) -> TrainingPlanRequest:
    return replace(
        request,
        entries=tuple(sorted(request.entries, key=_entry_sort_key)),
        analysis_domain_bindings=tuple(
            sorted(request.analysis_domain_bindings, key=_binding_sort_key)
        ),
        required_classes=tuple(sorted(request.required_classes)),
        eligible_parent_classes=tuple(sorted(request.eligible_parent_classes)),
        application_entries=tuple(
            sorted(request.application_entries, key=lambda item: item.project_entry_id)
        ),
        application_analysis_domain_bindings=tuple(
            sorted(request.application_analysis_domain_bindings, key=_binding_sort_key)
        ),
    )


def _split_inventories(
    request: TrainingPlanRequest,
) -> tuple[Mapping[str, tuple[str, ...]], Mapping[str, tuple[str, ...]]]:
    entries = {
        split: tuple(
            sorted({item.project_entry_id for item in request.entries if item.split == split})
        )
        for split in _SPLITS
    }
    subjects = {
        split: tuple(sorted({item.subject_id for item in request.entries if item.split == split}))
        for split in _SPLITS
    }
    return MappingProxyType(entries), MappingProxyType(subjects)


def _coverage_limitations(request: TrainingPlanRequest) -> tuple[str, ...]:
    if request.evaluation_intent is EvaluationIntent.INDEPENDENT_PERFORMANCE:
        return ()
    entry_sets = {
        split: {item.project_entry_id for item in request.entries if item.split == split}
        for split in _SPLITS
    }
    subject_sets = {
        split: {item.subject_id for item in request.entries if item.split == split}
        for split in _SPLITS
    }
    limitations = []
    if len(entry_sets["training"]) == 1:
        limitations.append("single_training_image")
    if request.evaluation_intent is EvaluationIntent.TRAINING_ONLY:
        limitations.append("accuracy_not_evaluated")
        return tuple(limitations)
    if (
        entry_sets["training"] & entry_sets["validation"]
        or subject_sets["training"] & subject_sets["validation"]
    ):
        limitations.append("non_independent_validation")
    if entry_sets["test"] and (
        entry_sets["test"] & (entry_sets["training"] | entry_sets["validation"])
        or subject_sets["test"] & (subject_sets["training"] | subject_sets["validation"])
    ):
        limitations.append("non_independent_test")
    return tuple(limitations)


def _validate_independent_roles(request: TrainingPlanRequest) -> None:
    if request.evaluation_intent is not EvaluationIntent.INDEPENDENT_PERFORMANCE:
        return
    roles_by_entry: dict[str, set[str]] = {}
    roles_by_subject: dict[str, set[str]] = {}
    for entry in request.entries:
        roles_by_entry.setdefault(entry.project_entry_id, set()).add(entry.split)
        roles_by_subject.setdefault(entry.subject_id, set()).add(entry.split)
    if any(len(roles) != 1 for roles in roles_by_entry.values()):
        raise BatchTrainingError("independent-performance entries cannot occupy multiple splits")
    if any(len(roles) != 1 for roles in roles_by_subject.values()):
        raise BatchTrainingError(
            "independent-performance subject inventories must be split-disjoint"
        )
    if not any(entry.split == "test" for entry in request.entries):
        raise BatchTrainingError("independent performance requires a nonempty frozen test split")


def _split_mapping(value: object, field: str) -> Mapping[str, tuple[str, ...]]:
    mapping = _closed_mapping(value, fields=frozenset(_SPLITS), field=field)
    parsed = {
        split: _string_tuple(
            mapping[split],
            f"{field}.{split}",
            nonempty=False,
            canonical=True,
        )
        for split in _SPLITS
    }
    return MappingProxyType(parsed)


def _revalidate_manifest(manifest: ValidatedBatchManifest) -> ValidatedBatchManifest:
    if not isinstance(manifest, ValidatedBatchManifest):
        raise BatchTrainingError("manifest must be a ValidatedBatchManifest")
    try:
        current = revalidate_retained_manifest(
            manifest.original_manifest_bytes,
            manifest.normalized_manifest_bytes,
        )
    except (BatchManifestError, OSError) as exc:
        raise BatchTrainingError("manifest retained authority is invalid") from exc
    if (
        current.manifest != manifest.manifest
        or current.original_manifest_sha256 != manifest.original_manifest_sha256
        or current.normalized_manifest_sha256 != manifest.normalized_manifest_sha256
        or current.samples != manifest.samples
    ):
        raise BatchTrainingError("manifest retained authority is not exact")
    return current


def _validate_authorities(
    request: TrainingPlanRequest,
    manifest: ValidatedBatchManifest,
    authorities: tuple[TrainingPlanEntryAuthority, ...],
    project_catalogue_witness: BatchArtifactRef,
    application_authorities: tuple[TrainingPlanEntryAuthority, ...] = (),
) -> tuple[str, ...]:
    samples = {sample.sample_id: sample for sample in manifest.samples}
    selected_entries: dict[str, TrainingPlanEntry] = {}
    for role in request.entries:
        previous = selected_entries.get(role.project_entry_id)
        if previous is not None and (
            previous.sample_id,
            previous.slide_id,
            previous.subject_id,
            previous.image_identity_sha256,
        ) != (
            role.sample_id,
            role.slide_id,
            role.subject_id,
            role.image_identity_sha256,
        ):
            raise BatchTrainingError(
                "project_entry_id roles must retain exact common entry identity"
            )
        selected_entries[role.project_entry_id] = role
    if len(authorities) != len(selected_entries) or not all(
        isinstance(item, TrainingPlanEntryAuthority) for item in authorities
    ):
        raise BatchTrainingError(
            "entry_authorities must cover every selected project entry exactly once"
        )
    if request.classifier_kind is ClassifierKind.PRIMARY_COMPARTMENT:
        try:
            validate_committed_artifact_authority_set(
                tuple(
                    committed
                    for item in authorities
                    for committed in item.accepted_analysis_domain_authorities
                )
            )
        except (BatchArtifactError, TypeError, ValueError) as exc:
            raise BatchTrainingError(str(exc)) from exc
        unique_entries = {item.project_entry_id: item for item in request.application_entries}
        if len(application_authorities) != len(unique_entries):
            raise BatchTrainingError("application authorities must cover every group member")
        selected_authorities = {
            item.project_catalogue_entry.scope.project_entry_id: item for item in authorities
        }
        application_by_id = {
            item.project_catalogue_entry.scope.project_entry_id: item
            for item in application_authorities
        }
        if any(application_by_id.get(key) != item for key, item in selected_authorities.items()):
            raise BatchTrainingError(
                "selected training authorities differ from application sources"
            )
        authorities = application_authorities
        domain_bindings = request.application_analysis_domain_bindings
    else:
        if application_authorities:
            raise BatchTrainingError("non-primary plan has application authorities")
        unique_entries = selected_entries
        domain_bindings = request.analysis_domain_bindings

    flattened = tuple(
        item for authority in authorities for item in authority.accepted_analysis_domain_authorities
    )
    try:
        committed = validate_committed_artifact_authority_set(flattened)
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchTrainingError(str(exc)) from exc
    try:
        decoded = tuple(decode_committed_accepted_analysis_domain(item) for item in committed)
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchTrainingError("accepted analysis-domain committed authority is invalid") from exc
    decoded_by_ref = {item.artifact_ref.artifact_ref_sha256: item for item in decoded}

    project_refs = tuple(item.project_catalogue_entry for item in authorities)
    try:
        witness_catalogue = validate_project_catalogue_selection_witness(
            project_catalogue_witness, project_refs
        )
    except (BatchProjectError, TypeError, ValueError) as exc:
        raise BatchTrainingError(f"project catalogue witness is invalid: {exc}") from exc
    if witness_catalogue.batch_id != request.batch_id:
        raise BatchTrainingError("project catalogue witness batch_id is not authoritative")

    request_bindings: dict[str, set[bytes]] = {}
    for binding in domain_bindings:
        role = unique_entries.get(binding.project_entry_id)
        if role is None:
            raise BatchTrainingError(
                "analysis domain request contains an unowned project_entry_id binding"
            )
        if (binding.sample_id, binding.slide_id) != (role.sample_id, role.slide_id):
            raise BatchTrainingError(
                "analysis domain binding sample_id or slide_id is not authoritative"
            )
        request_bindings.setdefault(binding.project_entry_id, set()).add(_binding_identity(binding))
    if any(not request_bindings.get(entry_id) for entry_id in unique_entries):
        raise BatchTrainingError("every selected entry requires exact binding coverage")

    authorities_by_entry: dict[str, TrainingPlanEntryAuthority] = {}
    source_ref_sha256s = {project_catalogue_witness.artifact_ref_sha256}
    for authority in authorities:
        project_ref = authority.project_catalogue_entry
        try:
            catalogue, catalogue_entry = reopen_project_catalogue_entry_artifact(project_ref)
        except (BatchProjectError, TypeError, ValueError) as exc:
            raise BatchTrainingError("project_catalogue_entry is invalid") from exc
        entry_id = catalogue_entry.project_entry_id
        if entry_id is None or entry_id in authorities_by_entry:
            raise BatchTrainingError("entry_authorities must have unique project_entry_id scopes")
        role = unique_entries.get(entry_id)
        if role is None:
            raise BatchTrainingError("entry_authorities contain an unowned project_entry_id")
        sample = samples.get(role.sample_id)
        if sample is None:
            raise BatchTrainingError("sample_id is absent from the manifest")
        if not sample.enabled:
            raise BatchTrainingError("training entries must be enabled manifest samples")
        if sample.slide_id != role.slide_id:
            raise BatchTrainingError("slide_id differs from the manifest")
        if sample.subject_id != role.subject_id:
            raise BatchTrainingError("subject_id differs from the manifest")
        if sample.validation_domain_id != request.validation_domain_id:
            raise BatchTrainingError("validation_domain_id differs from the manifest")
        if request.classifier_kind is ClassifierKind.PRIMARY_COMPARTMENT:
            admitted = (
                None
                if sample.config.primary_classifier is None
                else PrimaryClassifierSettings(
                    sample.config.primary_classes, sample.config.primary_classifier
                )
            )
            if admitted is not None:
                try:
                    require_primary_settings_compatible(admitted, request.primary_settings)
                except ValueError as exc:
                    raise BatchTrainingError("stage selected primary settings differ from admission") from exc
        if catalogue.batch_id != request.batch_id:
            raise BatchTrainingError("project catalogue batch_id is not authoritative")
        if catalogue_entry.image_role != "primary_mif":
            raise BatchTrainingError("training requires a primary_mif project entry")
        for name in ("sample_id", "slide_id", "validation_domain_id"):
            if getattr(catalogue_entry, name) != getattr(
                role if name != "validation_domain_id" else request, name
            ):
                raise BatchTrainingError(f"project catalogue {name} is not authoritative")
        if catalogue_entry.image_sha256 != role.image_identity_sha256:
            raise BatchTrainingError("image_identity_sha256 differs from the project catalogue")
        expected_project_config = (
            ArtifactConfigurationBinding(sample.sample_id, sample.resolved_config_sha256),
        )
        if project_ref.configuration_bindings != expected_project_config:
            raise BatchTrainingError("project catalogue configuration binding is not current")

        frame_ref = authority.analysis_frame
        try:
            frame = validate_analysis_frame_artifact_ref(frame_ref)
        except (TypeError, ValueError) as exc:
            raise BatchTrainingError("analysis_frame is invalid") from exc
        expected_config = (sample.sample_id, sample.resolved_config_sha256)
        if (
            frame_ref.scope.validation_domain_id,
            frame_ref.scope.sample_id,
            frame_ref.scope.slide_id,
            frame_ref.scope.project_entry_id,
            frame_ref.scope.analysis_domain_bindings,
        ) != (
            request.validation_domain_id,
            sample.sample_id,
            sample.slide_id,
            None,
            (),
        ):
            raise BatchTrainingError("analysis_frame scope is not authoritative")
        if (
            len(frame_ref.configuration_bindings) != 1
            or (
                frame_ref.configuration_bindings[0].sample_id,
                frame_ref.configuration_bindings[0].resolved_config_sha256,
            )
            != expected_config
        ):
            raise BatchTrainingError("analysis_frame configuration is not current")
        if frame.spatial_frame.canonical_image_sha256 != catalogue_entry.image_sha256:
            raise BatchTrainingError("analysis_frame image identity is not authoritative")
        if request.classifier_kind is ClassifierKind.PRIMARY_COMPARTMENT:
            _require_primary_application_channels(sample, frame_ref, role.channels)

        owned_bindings: set[bytes] = set()
        for committed_authority in authority.accepted_analysis_domain_authorities:
            domain_ref = committed_authority.artifact_ref
            projection = decoded_by_ref.get(domain_ref.artifact_ref_sha256)
            if projection is None:
                raise BatchTrainingError(
                    "accepted analysis-domain authority is outside the authoring snapshot"
                )
            if (
                projection.project_entry_id != entry_id
                or projection.canonical_image_sha256 != catalogue_entry.image_sha256
                or domain_ref.scope.validation_domain_id != request.validation_domain_id
                or domain_ref.scope.sample_id != sample.sample_id
                or domain_ref.scope.slide_id != sample.slide_id
                or domain_ref.scope.project_entry_id != entry_id
                or len(domain_ref.configuration_bindings) != 1
                or (
                    domain_ref.configuration_bindings[0].sample_id,
                    domain_ref.configuration_bindings[0].resolved_config_sha256,
                )
                != expected_config
            ):
                raise BatchTrainingError(
                    "accepted analysis-domain project, sample, slide, image, or config is not authoritative"
                )
            if len(projection.domain_authorities) != len(domain_ref.scope.analysis_domain_bindings):
                raise BatchTrainingError(
                    "accepted analysis-domain member coverage is not authoritative"
                )
            for member in projection.domain_authorities:
                matches = tuple(
                    binding
                    for binding in domain_bindings
                    if (
                        binding.project_entry_id == member.project_entry_id
                        and binding.sample_id == sample.sample_id
                        and binding.slide_id == member.slide_id
                        and binding.analysis_domain_id == member.analysis_domain_id
                        and binding.analysis_domain_revision == member.analysis_domain_revision
                        and binding.analysis_domain_revision_sha256
                        == member.analysis_domain_revision_sha256
                        and binding.domain_set_sha256 == projection.domain_set.domain_set_sha256
                    )
                )
                if len(matches) != 1:
                    requested_for_domain = tuple(
                        binding
                        for binding in domain_bindings
                        if binding.project_entry_id == entry_id
                    )
                    if any(
                        item.analysis_domain_id == member.analysis_domain_id
                        for item in requested_for_domain
                    ):
                        raise BatchTrainingError(
                            "accepted analysis-domain revision or set is not authoritative"
                        )
                    raise BatchTrainingError(
                        "accepted analysis-domain domain identity is not authoritative"
                    )
                binding = matches[0]
                identity = _binding_identity(binding)
                if identity in owned_bindings:
                    raise BatchTrainingError(
                        "accepted_analysis_domain binding coverage is duplicated"
                    )
                owned_bindings.add(identity)
            source_ref_sha256s.add(domain_ref.artifact_ref_sha256)
        if owned_bindings != request_bindings[entry_id]:
            raise BatchTrainingError(
                "accepted_analysis_domain does not provide exact binding coverage"
            )
        source_ref_sha256s.update((project_ref.artifact_ref_sha256, frame_ref.artifact_ref_sha256))
        additional = authority.additional_source_authorities
        if request.classifier_kind is ClassifierKind.SUPERVISED_PHENOTYPE:
            from ..batch_measurement_artifacts import (
                decode_committed_measurements_result,
            )
            from ..batch_phenotyping_mode_artifacts import (
                decode_committed_phenotyping_mode,
            )

            measurements = tuple(
                decode_committed_measurements_result(item)
                for item in additional
                if item.artifact_ref.artifact_kind == "measurements_result"
            )
            modes = tuple(
                decode_committed_phenotyping_mode(item)
                for item in additional
                if item.artifact_ref.artifact_kind == "phenotyping_mode"
            )
            if len(measurements) + len(modes) != len(additional):
                raise BatchTrainingError(
                    "supervised plan additional sources contain a foreign owner"
                )
            measurement_bindings = tuple(
                binding for item in measurements for binding in item.analysis_domain_bindings
            )
            mode_bindings = tuple(item.analysis_domain_binding for item in modes)
            expected = tuple(
                sorted(
                    (
                        binding
                        for binding in request.analysis_domain_bindings
                        if binding.project_entry_id == entry_id
                    ),
                    key=_binding_sort_key,
                )
            )
            if (
                tuple(sorted(measurement_bindings, key=_binding_sort_key)) != expected
                or tuple(sorted(mode_bindings, key=_binding_sort_key)) != expected
                or len(measurement_bindings) != len(set(measurement_bindings))
                or len(mode_bindings) != len(set(mode_bindings))
                or any(item.project_entry_id != entry_id for item in measurements)
                or any(item.project_entry_id != entry_id for item in modes)
            ):
                raise BatchTrainingError(
                    "supervised plan additional sources lack exact selected coverage"
                )
            source_ref_sha256s.update(item.artifact_ref.artifact_ref_sha256 for item in additional)
        elif additional:
            raise BatchTrainingError("pixel plan entry cannot carry supervised additional sources")
        authorities_by_entry[entry_id] = authority

    if set(authorities_by_entry) != set(unique_entries):
        raise BatchTrainingError("entry_authorities have incomplete project-entry coverage")
    return tuple(sorted(source_ref_sha256s))


def _plan_id_material(
    request: TrainingPlanRequest,
    source_refs: tuple[str, ...],
    binding_set_sha256: str,
    split_entries: Mapping[str, tuple[str, ...]],
    split_subjects: Mapping[str, tuple[str, ...]],
    limitations: tuple[str, ...],
) -> dict[str, object]:
    return {
        "request": request.to_mapping(),
        "authoring_source_artifact_ref_sha256s": list(source_refs),
        "analysis_domain_binding_set_sha256": binding_set_sha256,
        "split_project_entry_ids": dict(split_entries),
        "split_subject_ids": dict(split_subjects),
        "coverage_limitations": list(limitations),
    }


@dataclass(frozen=True, slots=True)
class TrainingPlan:
    schema_name: ClassVar[str] = "mif-training-plan"
    schema_version: ClassVar[int] = 2

    training_plan_id: str
    normalized_manifest_sha256: str
    classifier_engine: str
    request: TrainingPlanRequest
    authoring_source_artifact_ref_sha256s: tuple[str, ...]
    analysis_domain_binding_set_sha256: str
    split_project_entry_ids: Mapping[str, tuple[str, ...]]
    split_subject_ids: Mapping[str, tuple[str, ...]]
    coverage_limitations: tuple[str, ...]
    training_plan_sha256: str

    def __post_init__(self) -> None:
        """Validate the TrainingPlan fields when the record is constructed.

        The checks cover ``request``, ``training_plan_id``, ``normalized_manifest_sha256``,
        ``classifier_engine``, ``authoring_source_artifact_ref_sha256s``,
        ``analysis_domain_binding_set_sha256``. Invalid field types, values or combinations
        raise the declared validation error before this instance can be used by its caller."""

        if not isinstance(self.request, TrainingPlanRequest):
            raise BatchTrainingError("request must be a TrainingPlanRequest")
        if not _request_is_canonical(self.request):
            raise BatchTrainingError("training plan request must use canonical order")
        if self.request.classifier_kind is ClassifierKind.PRIMARY_COMPARTMENT and (
            self.request.primary_settings is None
            or not self.request.application_entries
            or not self.request.application_analysis_domain_bindings
        ):
            raise BatchTrainingError(
                "primary plan requires concrete settings and full application inventory"
            )
        _validate_independent_roles(self.request)
        plan_id = _sha256(self.training_plan_id, "training_plan_id")
        manifest_sha = _sha256(self.normalized_manifest_sha256, "normalized_manifest_sha256")
        engine = _text(self.classifier_engine, "classifier_engine")
        source_refs = tuple(self.authoring_source_artifact_ref_sha256s)
        for index, digest in enumerate(source_refs):
            _sha256(digest, f"authoring_source_artifact_ref_sha256s[{index}]")
        if not source_refs:
            raise BatchTrainingError("authoring_source_artifact_ref_sha256s must not be empty")
        if source_refs != tuple(sorted(source_refs)) or len(set(source_refs)) != len(source_refs):
            raise BatchTrainingError(
                "authoring_source_artifact_ref_sha256s must be canonical and unique"
            )
        binding_set_sha = _sha256(
            self.analysis_domain_binding_set_sha256,
            "analysis_domain_binding_set_sha256",
        )
        expected_binding_set = _digest(
            [item.to_mapping() for item in self.request.analysis_domain_bindings]
        )
        if binding_set_sha != expected_binding_set:
            raise BatchTrainingError("analysis_domain_binding_set_sha256 is not canonical")
        split_entries = _split_mapping(self.split_project_entry_ids, "split_project_entry_ids")
        split_subjects = _split_mapping(self.split_subject_ids, "split_subject_ids")
        expected_entries, expected_subjects = _split_inventories(self.request)
        if dict(split_entries) != dict(expected_entries):
            raise BatchTrainingError("split_project_entry_ids is not canonical")
        if dict(split_subjects) != dict(expected_subjects):
            raise BatchTrainingError("split_subject_ids is not canonical")
        limitations = tuple(self.coverage_limitations)
        if not all(isinstance(item, str) for item in limitations):
            raise BatchTrainingError("coverage_limitations must contain strings")
        if limitations != _coverage_limitations(self.request):
            raise BatchTrainingError("coverage_limitations is not canonical")
        if manifest_sha != self.request.normalized_manifest_sha256:
            raise BatchTrainingError("normalized_manifest_sha256 differs from request")
        if engine != self.request.classifier_engine:
            raise BatchTrainingError("classifier_engine differs from request")
        expected_plan_id = _digest(
            _plan_id_material(
                self.request,
                source_refs,
                binding_set_sha,
                split_entries,
                split_subjects,
                limitations,
            )
        )
        if plan_id != expected_plan_id:
            raise BatchTrainingError("training_plan_id is not canonical")
        object.__setattr__(self, "training_plan_id", plan_id)
        object.__setattr__(self, "normalized_manifest_sha256", manifest_sha)
        object.__setattr__(self, "classifier_engine", engine)
        object.__setattr__(self, "authoring_source_artifact_ref_sha256s", source_refs)
        object.__setattr__(self, "analysis_domain_binding_set_sha256", binding_set_sha)
        object.__setattr__(self, "split_project_entry_ids", split_entries)
        object.__setattr__(self, "split_subject_ids", split_subjects)
        object.__setattr__(self, "coverage_limitations", limitations)
        plan_sha = _sha256(self.training_plan_sha256, "training_plan_sha256")
        if plan_sha != _digest(self._unsigned_mapping()):
            raise BatchTrainingError("training_plan_sha256 is not canonical")
        object.__setattr__(self, "training_plan_sha256", plan_sha)

    @classmethod
    def create(
        cls,
        request: TrainingPlanRequest,
        manifest: ValidatedBatchManifest,
        entry_authorities: tuple[TrainingPlanEntryAuthority, ...],
        *,
        project_catalogue_witness: BatchArtifactRef,
        application_entry_authorities: tuple[TrainingPlanEntryAuthority, ...] = (),
    ) -> "TrainingPlan":
        if not isinstance(request, TrainingPlanRequest):
            raise BatchTrainingError("request must be a TrainingPlanRequest")
        current_manifest = _revalidate_manifest(manifest)
        normalized = _normalized_request(request)
        if normalized.batch_id != current_manifest.manifest.batch_id:
            raise BatchTrainingError("batch_id differs from the manifest")
        if normalized.normalized_manifest_sha256 != current_manifest.normalized_manifest_sha256:
            raise BatchTrainingError("normalized_manifest_sha256 differs from the manifest")
        if normalized.validation_domain_id not in current_manifest.manifest.validation_domains:
            raise BatchTrainingError("validation_domain_id is absent from the manifest")
        _validate_independent_roles(normalized)
        authorities = tuple(entry_authorities)
        source_refs = _validate_authorities(
            normalized,
            current_manifest,
            authorities,
            project_catalogue_witness,
            tuple(application_entry_authorities),
        )
        binding_set_sha = _digest(
            [item.to_mapping() for item in normalized.analysis_domain_bindings]
        )
        split_entries, split_subjects = _split_inventories(normalized)
        limitations = _coverage_limitations(normalized)
        plan_id = _digest(
            _plan_id_material(
                normalized,
                source_refs,
                binding_set_sha,
                split_entries,
                split_subjects,
                limitations,
            )
        )
        unsigned = {
            "training_plan_id": plan_id,
            "normalized_manifest_sha256": normalized.normalized_manifest_sha256,
            "classifier_engine": normalized.classifier_engine,
            "request": normalized,
            "authoring_source_artifact_ref_sha256s": source_refs,
            "analysis_domain_binding_set_sha256": binding_set_sha,
            "split_project_entry_ids": split_entries,
            "split_subject_ids": split_subjects,
            "coverage_limitations": limitations,
        }
        prototype = object.__new__(cls)
        for name, item in unsigned.items():
            object.__setattr__(prototype, name, item)
        return cls(
            **unsigned,
            training_plan_sha256=_digest(prototype._unsigned_mapping()),
        )

    @classmethod
    def from_mapping(cls, value: object) -> "TrainingPlan":
        """Decode the serialized fields into a validated TrainingPlan record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""

        fields = frozenset(
            {
                "schema_name",
                "schema_version",
                *cls.__dataclass_fields__,
            }
        )
        mapping = _closed_mapping(value, fields=fields, field="training_plan")
        if (
            mapping["schema_name"] != cls.schema_name
            or mapping["schema_version"] != cls.schema_version
        ):
            raise BatchTrainingError("training plan schema is unsupported")
        return cls(
            training_plan_id=mapping["training_plan_id"],  # type: ignore[arg-type]
            normalized_manifest_sha256=mapping[
                "normalized_manifest_sha256"
            ],  # type: ignore[arg-type]
            classifier_engine=mapping["classifier_engine"],  # type: ignore[arg-type]
            request=TrainingPlanRequest.from_mapping(mapping["request"]),
            authoring_source_artifact_ref_sha256s=tuple(
                _array(
                    mapping["authoring_source_artifact_ref_sha256s"],
                    "authoring_source_artifact_ref_sha256s",
                )
            ),  # type: ignore[arg-type]
            analysis_domain_binding_set_sha256=mapping[
                "analysis_domain_binding_set_sha256"
            ],  # type: ignore[arg-type]
            split_project_entry_ids=_split_mapping(
                mapping["split_project_entry_ids"], "split_project_entry_ids"
            ),
            split_subject_ids=_split_mapping(mapping["split_subject_ids"], "split_subject_ids"),
            coverage_limitations=tuple(
                _array(mapping["coverage_limitations"], "coverage_limitations")
            ),  # type: ignore[arg-type]
            training_plan_sha256=mapping["training_plan_sha256"],  # type: ignore[arg-type]
        )

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "training_plan_id": self.training_plan_id,
            "normalized_manifest_sha256": self.normalized_manifest_sha256,
            "classifier_engine": self.classifier_engine,
            "request": self.request.to_mapping(),
            "authoring_source_artifact_ref_sha256s": list(
                self.authoring_source_artifact_ref_sha256s
            ),
            "analysis_domain_binding_set_sha256": self.analysis_domain_binding_set_sha256,
            "split_project_entry_ids": dict(self.split_project_entry_ids),
            "split_subject_ids": dict(self.split_subject_ids),
            "coverage_limitations": list(self.coverage_limitations),
        }

    def to_mapping(self) -> dict[str, object]:
        return {**self._unsigned_mapping(), "training_plan_sha256": self.training_plan_sha256}


def _plan_set_sort_key(plan: TrainingPlan) -> tuple[str, str]:
    return plan.request.classifier_id, plan.training_plan_id


def training_plan_candidate_entry_ids(
    *,
    node_kind: str,
    participant_ids: tuple[str, ...],
    project_entry_ids: tuple[str, ...],
    manifest: ValidatedBatchManifest,
    secondary_selections: Mapping[str, object] | None = None,
) -> Mapping[str, tuple[str, ...]]:
    """Project candidate IDs without authoring user-selectable identity facts."""

    entries = tuple(sorted(project_entry_ids))
    if not entries:
        raise BatchTrainingError("training plan selection has no candidate entries")
    by_id = {item.sample_id: item for item in manifest.enabled_samples}
    try:
        samples = tuple(by_id[item] for item in participant_ids)
    except KeyError as exc:
        raise BatchTrainingError("training plan selection participant is not enabled") from exc
    if not samples or len(set(participant_ids)) != len(participant_ids):
        raise BatchTrainingError("training plan selection has invalid participants")
    if node_kind == "primary_classifier_training_plan_review":
        settings = tuple(
            (
                None
                if item.config.primary_classifier is None
                else PrimaryClassifierSettings(
                    item.config.primary_classes, item.config.primary_classifier
                )
            )
            for item in samples
        )
        concrete_settings = tuple(item for item in settings if item is not None)
        if concrete_settings and any(item != concrete_settings[0] for item in concrete_settings):
            raise BatchTrainingError("primary candidate settings differ across the group")
        ids = (
            {concrete_settings[0].primary_classifier.classifier_id}
            if concrete_settings
            else {DEFERRED_PRIMARY_CANDIDATE_ID}
        )
    elif node_kind == "secondary_classifier_training_plan_review":
        from ..batch_secondary_definitions import resolve_secondary_structures

        ids = {
            structure.classifier.classifier_id
            for item in samples
            for structure in resolve_secondary_structures(item, (secondary_selections or {}).get(item.sample_id))
        }
    elif node_kind == "supervised_phenotype_training_plan_review":
        ids = {"supervised-phenotype"}
    else:
        raise BatchTrainingError("training plan selection stage is foreign")
    if not ids:
        raise BatchTrainingError("training plan selection has no classifier candidate")
    return MappingProxyType({candidate_id: entries for candidate_id in sorted(ids)})


def secondary_output_class_inventory(
    samples: tuple[ResolvedBatchSample, ...],
    candidate_ids: tuple[str, ...],
    *,
    secondary_selections: Mapping[str, object] | None = None,
) -> Mapping[str, dict[str, object]]:
    """Project Candidate/Other labels from configured or accepted structures."""
    from ..batch_secondary_definitions import resolve_secondary_structures
    from mif_pipeline.pipeline_core.config import secondary_classifier_groups, secondary_classifier_output_classes

    if not samples or not candidate_ids or candidate_ids != tuple(sorted(set(candidate_ids))):
        raise BatchTrainingError("secondary class inventory has invalid candidates or participants")
    all_ids: set[str] = set()
    for sample in samples:
        for structure in resolve_secondary_structures(sample, (secondary_selections or {}).get(sample.sample_id)):
            if type(structure) is not SecondaryStructureSpec:
                raise BatchTrainingError(
                    "secondary class inventory requires typed structure settings"
                )
            all_ids.add(structure.classifier.classifier_id)
    if all_ids != set(candidate_ids):
        raise BatchTrainingError("secondary class inventory candidate coverage differs")
    inventory: dict[str, dict[str, object]] = {}
    for candidate_id in candidate_ids:
        record: dict[str, object] | None = None
        for sample in samples:
            resolved_structures = resolve_secondary_structures(
                sample, (secondary_selections or {}).get(sample.sample_id)
            )
            structures = tuple(
                item
                for item in resolved_structures
                if item.classifier.classifier_id == candidate_id
            )
            secondary_classifier_groups(resolved_structures)
            if not structures:
                raise BatchTrainingError("Secondary classifier is missing from a participating image")
            structure = structures[0]
            parent_names = {
                "AnalysisDomain": "Analysis region",
                **{item.class_id: item.display_name for item in sample.config.primary_classes or ()},
                **{item.structure_id: item.display_name for item in resolved_structures},
            }
            projected = {
                "structures": [{"structure_id": item.structure_id, "display_name": item.display_name} for item in structures],
                "channel_roles": dict(structure.classifier.channel_roles),
                "eligible_parents": [
                    {"parent_id": parent_id, "display_name": parent_names[parent_id]}
                    for parent_id in structure.eligible_parent_ids
                ],
                "output_classes": [
                    {"canonical_class_id": label, "display_name": label}
                    for label in (structure.classifier.output_classes or secondary_classifier_output_classes((structure.structure_id,), joint=False))
                ],
            }
            if record is not None and record != projected:
                raise BatchTrainingError(
                    "secondary classifier maps to conflicting structures or class labels"
                )
            record = projected
        assert record is not None
        inventory[candidate_id] = record
    return MappingProxyType(inventory)


def project_training_plan_candidate_participants(
    *,
    manifest: ValidatedBatchManifest,
    project_entry_refs: tuple[BatchArtifactRef, ...],
    candidate_entry_ids: Mapping[str, tuple[str, ...]],
    participant_ids: tuple[str, ...],
    validation_domain_id: str,
) -> Mapping[str, Mapping[str, str]]:
    """Bind owner-authenticated project scopes to admitted subject identities."""

    project_refs = tuple(validate_artifact_ref(ref) for ref in project_entry_refs)
    if any(ref.artifact_kind != "project_catalogue_entry" for ref in project_refs):
        raise BatchTrainingError("training review requires project entry sources")
    expected_entries = {
        entry_id for entries in candidate_entry_ids.values() for entry_id in entries
    }
    if not expected_entries or len(project_refs) != len(expected_entries):
        raise BatchTrainingError("training review project entry coverage differs")
    samples = {sample.sample_id: sample for sample in manifest.enabled_samples}
    projected: dict[str, Mapping[str, str]] = {}
    seen_samples: set[str] = set()
    for ref in project_refs:
        scope = ref.scope
        entry_id = scope.project_entry_id
        sample = samples.get(scope.sample_id)
        if (
            entry_id is None
            or entry_id not in expected_entries
            or entry_id in projected
            or ref.owning_identity.get("project_entry_id") != entry_id
            or sample is None
            or sample.sample_id in seen_samples
            or scope.slide_id != sample.slide_id
            or scope.validation_domain_id != validation_domain_id
            or sample.validation_domain_id != validation_domain_id
            or ref.configuration_bindings
            != (ArtifactConfigurationBinding(sample.sample_id, sample.resolved_config_sha256),)
        ):
            raise BatchTrainingError("training review project identity differs from admission")
        seen_samples.add(sample.sample_id)
        projected[entry_id] = MappingProxyType(
            {
                "sample_id": sample.sample_id,
                "subject_id": sample.subject_id,
            }
        )
    if (
        set(projected) != expected_entries
        or seen_samples != set(participant_ids)
        or len(seen_samples) != len(participant_ids)
    ):
        raise BatchTrainingError("training review participants differ from dispatch")
    return MappingProxyType({entry_id: projected[entry_id] for entry_id in sorted(projected)})


def primary_plan_review_application_inventory(
    *,
    manifest: ValidatedBatchManifest,
    input_authorities: tuple[BatchCommittedArtifactAuthority, ...],
) -> tuple[
    tuple[TrainingPlanApplicationEntry, ...],
    tuple[AnalysisDomainBinding, ...],
    tuple[TrainingPlanEntryAuthority, ...],
]:
    """Open each exact authorized source for the primary application's full group."""

    authorities = validate_committed_artifact_authority_set(input_authorities)
    refs = tuple(item.artifact_ref for item in authorities)
    if any(
        item.artifact_kind
        not in {"project_catalogue_entry", "analysis_frame", "accepted_analysis_domain"}
        for item in refs
    ):
        raise BatchTrainingError("primary application review contains a foreign source")
    project_refs: dict[str, BatchArtifactRef] = {}
    catalogue_entries = {}
    for ref in refs:
        if ref.artifact_kind != "project_catalogue_entry":
            continue
        _catalogue, entry = reopen_project_catalogue_entry_artifact(ref)
        if entry.project_entry_id in project_refs:
            raise BatchTrainingError("primary application has duplicate project entry")
        project_refs[entry.project_entry_id] = ref
        catalogue_entries[entry.project_entry_id] = entry
    frames: dict[str, BatchArtifactRef] = {}
    for ref in refs:
        if ref.artifact_kind != "analysis_frame":
            continue
        if ref.scope.sample_id in frames:
            raise BatchTrainingError("primary application has duplicate analysis frame")
        frames[ref.scope.sample_id] = ref
    domain_by_entry: dict[str, list[BatchCommittedArtifactAuthority]] = {}
    for authority in authorities:
        if authority.artifact_ref.artifact_kind != "accepted_analysis_domain":
            continue
        entry_id = authority.artifact_ref.scope.project_entry_id
        if entry_id is None:
            raise BatchTrainingError("primary application domain lacks project entry")
        domain_by_entry.setdefault(entry_id, []).append(authority)
    if not project_refs or set(domain_by_entry) != set(project_refs):
        raise BatchTrainingError("primary application requires exact project/domain coverage")
    samples = {item.sample_id: item for item in manifest.enabled_samples}
    rows = []
    bindings = []
    entry_sources = []
    seen_samples = set()
    for entry_id in sorted(project_refs):
        entry = catalogue_entries[entry_id]
        sample = samples.get(entry.sample_id)
        if sample is None or sample.sample_id in seen_samples:
            raise BatchTrainingError("primary application entry is foreign or duplicate")
        if (
            entry.slide_id != sample.slide_id
            or entry.validation_domain_id != sample.validation_domain_id
            or entry.image_role != "primary_mif"
            or project_refs[entry_id].configuration_bindings
            != (ArtifactConfigurationBinding(sample.sample_id, sample.resolved_config_sha256),)
        ):
            raise BatchTrainingError("primary application project identity differs from admission")
        seen_samples.add(sample.sample_id)
        frame_ref = frames.get(sample.sample_id)
        if frame_ref is None:
            raise BatchTrainingError("primary application lacks analysis frame")
        frame = validate_analysis_frame_artifact_ref(frame_ref)
        if (
            frame_ref.scope.slide_id != sample.slide_id
            or frame_ref.scope.validation_domain_id != sample.validation_domain_id
            or frame_ref.configuration_bindings
            != (ArtifactConfigurationBinding(sample.sample_id, sample.resolved_config_sha256),)
        ):
            raise BatchTrainingError("primary application frame identity differs from admission")
        channels = _primary_application_channels(sample, frame_ref)
        if frame.spatial_frame.canonical_image_sha256 != entry.image_sha256:
            raise BatchTrainingError("primary application image differs from canonical frame")
        rows.append(
            TrainingPlanApplicationEntry(
                entry_id,
                sample.sample_id,
                sample.slide_id,
                sample.subject_id,
                entry.image_sha256,
                channels,
            )
        )
        owned = tuple(domain_by_entry[entry_id])
        for authority in owned:
            projection = decode_committed_accepted_analysis_domain(authority)
            ref = authority.artifact_ref
            if (
                projection.project_entry_id != entry_id
                or projection.canonical_image_sha256 != entry.image_sha256
                or ref.scope.sample_id != sample.sample_id
                or ref.scope.slide_id != sample.slide_id
                or ref.scope.validation_domain_id != sample.validation_domain_id
                or ref.configuration_bindings
                != (ArtifactConfigurationBinding(sample.sample_id, sample.resolved_config_sha256),)
            ):
                raise BatchTrainingError(
                    "primary application domain identity differs from admission"
                )
        bindings.extend(
            binding
            for authority in owned
            for binding in authority.artifact_ref.scope.analysis_domain_bindings
        )
        entry_sources.append(TrainingPlanEntryAuthority(project_refs[entry_id], frame_ref, owned))
    if set(frames) != seen_samples:
        raise BatchTrainingError("primary application frames differ from group members")
    sorted_bindings = tuple(sorted(bindings, key=_binding_sort_key))
    if len({_binding_identity(item) for item in sorted_bindings}) != len(sorted_bindings):
        raise BatchTrainingError("primary application domains are duplicated")
    return tuple(rows), sorted_bindings, tuple(entry_sources)


def supervised_plan_review_options(
    *,
    manifest: ValidatedBatchManifest,
    input_authorities: tuple[BatchCommittedArtifactAuthority, ...],
) -> dict[str, object]:
    """Reopen committed measurements and project one common supervised choice set."""

    from ..batch_measurement_artifacts import decode_committed_measurements_result

    authorities = validate_committed_artifact_authority_set(input_authorities)
    project_refs = tuple(
        item.artifact_ref
        for item in authorities
        if item.artifact_ref.artifact_kind == "project_catalogue_entry"
    )
    measurement_authorities = tuple(
        item for item in authorities if item.artifact_ref.artifact_kind == "measurements_result"
    )
    if not project_refs or not measurement_authorities:
        raise BatchTrainingError("supervised review lacks project entries or measurements")
    samples = {item.sample_id: item for item in manifest.enabled_samples}
    entries_by_id = {}
    for ref in project_refs:
        _catalogue, entry = reopen_project_catalogue_entry_artifact(ref)
        if entry.project_entry_id in entries_by_id:
            raise BatchTrainingError("supervised review project entry is duplicated")
        sample = samples.get(entry.sample_id)
        if (
            sample is None
            or entry.slide_id != sample.slide_id
            or entry.validation_domain_id != sample.validation_domain_id
            or entry.image_role != "primary_mif"
            or ref.configuration_bindings
            != (ArtifactConfigurationBinding(sample.sample_id, sample.resolved_config_sha256),)
        ):
            raise BatchTrainingError("supervised review project identity differs from admission")
        entries_by_id[entry.project_entry_id] = entry
    measurements = tuple(
        decode_committed_measurements_result(item) for item in measurement_authorities
    )
    measured_entries = set()
    feature_rows = None
    for item in measurements:
        entry = entries_by_id.get(item.project_entry_id)
        if (
            entry is None
            or item.sample_id != entry.sample_id
            or item.slide_id != entry.slide_id
            or item.canonical_image_sha256 != entry.image_sha256
            or item.artifact_ref.configuration_bindings
            != (
                ArtifactConfigurationBinding(
                    entry.sample_id, samples[entry.sample_id].resolved_config_sha256
                ),
            )
        ):
            raise BatchTrainingError("supervised measurement identity differs from group")
        measured_entries.add(item.project_entry_id)
        rows = tuple(item.measurement_plan.to_mapping()["features"])
        if feature_rows is None:
            feature_rows = rows
        elif rows != feature_rows:
            raise BatchTrainingError(
                "supervised measurement feature semantics differ across participants"
            )
    if measured_entries != set(entries_by_id) or not feature_rows:
        raise BatchTrainingError("supervised measurements lack exact project-entry coverage")
    class_rows = tuple(
        tuple(
            sorted(
                item.class_id
                for item in samples[entry.sample_id].config.phenotyping.phenotype_classes
            )
        )
        for entry in entries_by_id.values()
    )
    if any(row != class_rows[0] for row in class_rows):
        raise BatchTrainingError("supervised configured classes differ across participants")
    frames = {item.artifact_ref.scope.sample_id: item.artifact_ref for item in authorities
              if item.artifact_ref.artifact_kind == "analysis_frame"}
    channel_rows = None
    sampling = [0.0, 0.0]
    for entry in entries_by_id.values():
        frame_ref = frames.get(entry.sample_id)
        if frame_ref is None:
            raise BatchTrainingError("supervised review lacks a current analysis frame")
        frame = validate_analysis_frame_artifact_ref(frame_ref).spatial_frame
        # Use one physical sampling across the group without upsampling any image.
        sampling[0] = max(sampling[0], frame.x_um_per_px)
        sampling[1] = max(sampling[1], frame.y_um_per_px)
        rows = tuple(item.to_mapping() for item in _primary_application_channels(samples[entry.sample_id], frame_ref))
        if channel_rows is None:
            channel_rows = rows
        elif rows != channel_rows:
            # Offer only exact channel identities shared by every participating image.
            channel_rows = tuple(row for row in channel_rows if row in rows)
    return {
        "configured_class_ids": list(class_rows[0]) if class_rows[0] else None,
        "available_features": list(feature_rows),
        "available_channels": list(channel_rows or ()),
        "recommended_pixel_size_um": sampling,
    }


def _selected_project_catalogue_witness(
    *,
    selected_entry_ids: tuple[str, ...],
    project_ref_by_id: Mapping[str, BatchArtifactRef],
) -> BatchArtifactRef:
    """Choose a selected entry ref that witnesses every selected generation."""

    try:
        selected_refs = tuple(project_ref_by_id[entry_id] for entry_id in selected_entry_ids)
    except KeyError as exc:
        raise BatchTrainingError("selected project catalogue witness lacks an entry") from exc
    for witness in reversed(selected_refs):
        try:
            validate_project_catalogue_selection_witness(witness, selected_refs)
        except (BatchProjectError, TypeError, ValueError):
            continue
        return witness
    raise BatchTrainingError("selected project entries lack a common catalogue witness")


def author_training_plan_set_from_selection(
    *,
    node_kind: str,
    manifest: ValidatedBatchManifest,
    input_authorities: tuple[BatchCommittedArtifactAuthority, ...],
    selection: object,
    secondary_selections: Mapping[str, object] | None = None,
    supervised_reference_predecessor: "TrainingPlanSet | None" = None,
) -> "TrainingPlanSet":
    """Author plans from authenticated owners plus role/config choices only."""

    from .plan_selection import TrainingPlanSelectionResponse
    from ..batch_measurement_artifacts import decode_committed_measurements_result

    if type(selection) is not TrainingPlanSelectionResponse:
        raise BatchTrainingError("training plan authoring requires a validated selection")
    authorities = validate_committed_artifact_authority_set(input_authorities)
    refs = tuple(item.artifact_ref for item in authorities)
    project_refs = tuple(item for item in refs if item.artifact_kind == "project_catalogue_entry")
    frames = {item.scope.sample_id: item for item in refs if item.artifact_kind == "analysis_frame"}
    domains = tuple(
        item
        for item in authorities
        if item.artifact_ref.artifact_kind == "accepted_analysis_domain"
    )
    additional = tuple(
        item
        for item in authorities
        if item.artifact_ref.artifact_kind in {"measurements_result", "phenotyping_mode"}
    )
    if not project_refs or not frames or not domains:
        raise BatchTrainingError("training plan authoring lacks exact current sources")
    entries_by_id = {}
    project_ref_by_id = {}
    for ref in project_refs:
        _catalogue, entry = reopen_project_catalogue_entry_artifact(ref)
        if entry.project_entry_id in entries_by_id:
            raise BatchTrainingError("training plan project entries are duplicated")
        entries_by_id[entry.project_entry_id] = entry
        project_ref_by_id[entry.project_entry_id] = ref
    samples = {item.sample_id: item for item in manifest.enabled_samples}
    domain_by_entry: dict[str, list[BatchCommittedArtifactAuthority]] = {}
    for authority in domains:
        entry_id = authority.artifact_ref.scope.project_entry_id
        if entry_id is None:
            raise BatchTrainingError("training plan domain source lacks an entry")
        domain_by_entry.setdefault(entry_id, []).append(authority)
    additional_by_entry: dict[str, list[BatchCommittedArtifactAuthority]] = {}
    for authority in additional:
        entry_id = authority.artifact_ref.scope.project_entry_id
        if entry_id is None:
            raise BatchTrainingError("training plan additional source lacks an entry")
        additional_by_entry.setdefault(entry_id, []).append(authority)
    measurements = tuple(
        decode_committed_measurements_result(item)
        for item in additional
        if item.artifact_ref.artifact_kind == "measurements_result"
    )
    primary_application = (
        primary_plan_review_application_inventory(
            manifest=manifest,
            input_authorities=authorities,
        )
        if node_kind == "primary_classifier_training_plan_review"
        else None
    )
    supervised_options = (
        supervised_plan_review_options(
            manifest=manifest,
            input_authorities=authorities,
        )
        if node_kind == "supervised_phenotype_training_plan_review"
        else None
    )
    secondary_channels_by_entry = (
        {entry_id: {channel.channel_id for channel in _primary_application_channels(samples[entry.sample_id], frames[entry.sample_id])}
         for entry_id, entry in entries_by_id.items()}
        if node_kind == "secondary_classifier_training_plan_review" else {}
    )
    plans = []
    for selected in selection.selections:
        selected_entry_ids = tuple(sorted({item.project_entry_id for item in selected.roles}))
        try:
            selected_entries = tuple(entries_by_id[item] for item in selected_entry_ids)
            selected_samples = tuple(samples[item.sample_id] for item in selected_entries)
        except KeyError as exc:
            raise BatchTrainingError("training plan selection contains a foreign entry") from exc
        configs = tuple(item.config for item in selected_samples)
        first = configs[0]
        if node_kind == "primary_classifier_training_plan_review":
            if (
                selected.supervised_class_ids is not None
                or selected.supervised_feature_ids is not None
            ):
                raise BatchTrainingError("pixel training forbids supervised selections")
            settings = selected.primary_settings
            if settings is None:
                raise BatchTrainingError("primary training selection requires concrete settings")
            candidate_ids = training_plan_candidate_entry_ids(
                node_kind=node_kind,
                participant_ids=tuple(item.sample_id for item in primary_application[0]),
                project_entry_ids=tuple(item.project_entry_id for item in primary_application[0]),
                manifest=manifest,
            )
            if selected.candidate_id not in candidate_ids:
                raise BatchTrainingError("primary training candidate identity differs")
            if (
                selected.candidate_id != DEFERRED_PRIMARY_CANDIDATE_ID
                and settings.primary_classifier.classifier_id != selected.candidate_id
            ):
                raise BatchTrainingError("configured primary classifier identity differs")
            for item in configs:
                if item.primary_classifier is not None:
                    try:
                        require_primary_settings_compatible(
                            PrimaryClassifierSettings(item.primary_classes, item.primary_classifier),
                            settings,
                        )
                    except ValueError as exc:
                        raise BatchTrainingError("stage selected primary settings differ from admission") from exc
            classifier_kind = ClassifierKind.PRIMARY_COMPARTMENT
            required_classes = primary_training_required_classes(settings)
            eligible_parents = ("Tissue",)
            channel_requirements = {
                "channel_roles": dict(settings.primary_classifier.channel_roles)
            }
            feature_requirements = None
        elif node_kind == "secondary_classifier_training_plan_review":
            from ..batch_secondary_definitions import resolve_secondary_structures

            if (
                selected.supervised_class_ids is not None
                or selected.supervised_feature_ids is not None
            ):
                raise BatchTrainingError("pixel training forbids supervised selections")
            if selected.primary_settings is not None:
                raise BatchTrainingError("secondary selection forbids primary settings")
            from mif_pipeline.pipeline_core.config import secondary_classifier_groups, secondary_classifier_output_classes
            groups = tuple(
                secondary_classifier_groups(resolve_secondary_structures(sample, (secondary_selections or {}).get(sample.sample_id))).get(selected.candidate_id)
                for sample in selected_samples
            )
            if not groups or groups[0] is None or any(group != groups[0] for group in groups):
                raise BatchTrainingError("secondary training candidate configuration differs")
            structures = groups[0]
            structure = structures[0]
            if not selected.secondary_channel_roles:
                raise BatchTrainingError("Choose at least one input channel for the secondary classifier")
            selected_ids = set(selected.secondary_channel_roles.values())
            # Every application image must contain the explicitly selected channels.
            if any(not selected_ids <= offered for offered in secondary_channels_by_entry.values()):
                raise BatchTrainingError("A selected secondary input channel is absent from a participating image")
            classifier_kind = ClassifierKind.SECONDARY_STRUCTURE
            required_classes = tuple(sorted((structure.classifier.output_classes or secondary_classifier_output_classes((structure.structure_id,), joint=False))))
            eligible_parents = tuple(sorted(structure.eligible_parent_ids))
            channel_requirements = {"channel_roles": dict(selected.secondary_channel_roles)}
            feature_requirements = None
        elif node_kind == "supervised_phenotype_training_plan_review":
            if selected.primary_settings is not None:
                raise BatchTrainingError("supervised selection forbids primary settings")
            if selected.candidate_id != "supervised-phenotype":
                raise BatchTrainingError("supervised training candidate differs")
            configured_classes = supervised_options["configured_class_ids"]
            selected_classes = selected.supervised_class_ids
            selected_feature_ids = selected.supervised_feature_ids
            if selected_classes is None or selected_feature_ids is None:
                raise BatchTrainingError(
                    "supervised phenotype requires selected classes and features"
                )
            if configured_classes is not None and set(selected_classes) != set(configured_classes):
                raise BatchTrainingError(
                    "supervised phenotype classes differ from configured classes"
                )
            classifier_kind = ClassifierKind.SUPERVISED_PHENOTYPE
            required_classes = tuple(sorted(selected_classes))
            eligible_parents = ("Cell",)
            channel_requirements = None
            from .supervised_features import supervised_feature_source, require_kronos_cell_channels
            from .qupath_kronos_cell import kronos_cell_measurements
            source = supervised_feature_source(selected.model_configuration)
            if source is not None:
                require_kronos_cell_channels(source, supervised_options["available_channels"])
                for entry_id in selected_entry_ids:
                    sample = samples[entries_by_id[entry_id].sample_id]
                    require_kronos_cell_channels(source, _primary_application_channels(sample, frames[sample.sample_id]))
                feature_schema = kronos_cell_measurements()
                if selected_feature_ids != tuple(item["measurement_name"] for item in feature_schema):
                    raise BatchTrainingError("KRONOS selection must contain all 768 ordered generated features")
            else:
                available = {item["feature_id"]: item for item in supervised_options["available_features"]}
                if any(feature_id not in available for feature_id in selected_feature_ids):
                    raise BatchTrainingError("supervised training selected a foreign feature")
                observed_by_id = {
                    item.feature_id: item
                    for measurement in measurements
                    if measurement.project_entry_id in selected_entry_ids
                    for item in measurement.measurement_plan.features
                }
                if any(feature_id not in observed_by_id for feature_id in selected_feature_ids):
                    raise BatchTrainingError("supervised training selected an unavailable feature")
                feature_schema = [{"measurement_name": observed_by_id[feature_id].measurement,
                                   "semantic_role": observed_by_id[feature_id].role.value}
                                  for feature_id in selected_feature_ids]
            radius = selected.model_configuration.get("point_selection_radius_level0")
            if isinstance(radius, bool) or not isinstance(radius, (int, float)) or radius <= 0:
                raise BatchTrainingError("supervised training lacks point selection radius")
            feature_requirements = {
                "feature_schema": feature_schema,
                "point_selection_radius_level0": float(radius),
            }
        else:
            raise BatchTrainingError("training plan authoring stage is foreign")
        plan_entries = tuple(
            TrainingPlanEntry(
                project_entry_id=entry.project_entry_id,
                sample_id=entry.sample_id,
                slide_id=entry.slide_id,
                subject_id=samples[entry.sample_id].subject_id,
                split=role.split,
                image_identity_sha256=entry.image_sha256,
            )
            for role in selected.roles
            for entry in (entries_by_id[role.project_entry_id],)
        )
        selected_bindings = tuple(
            sorted(
                (
                    binding
                    for entry_id in selected_entry_ids
                    for authority in domain_by_entry.get(entry_id, ())
                    for binding in authority.artifact_ref.scope.analysis_domain_bindings
                ),
                key=_binding_sort_key,
            )
        )
        model_configuration = dict(selected.model_configuration)
        model_configuration.pop("point_selection_radius_level0", None)
        from .qupath_rtrees import (
            QuPathObjectRTreesConfiguration,
            QuPathPixelRTreesConfiguration,
        )
        from .qupath_svm import QuPathPixelSvmConfiguration, QuPathObjectSvmConfiguration
        from .qupath_logistic_regression import QuPathObjectLogisticRegressionConfiguration
        from .qupath_kronos import QuPathKronosConfiguration

        is_object = classifier_kind is ClassifierKind.SUPERVISED_PHENOTYPE
        configuration_schema = model_configuration.get("configuration_schema")
        routes = {
            "qupath-pixel-rtrees/v1": (
                QuPathPixelRTreesConfiguration,
                "qupath-pixel-classifier",
                False,
            ),
            "qupath-object-rtrees/v1": (
                QuPathObjectRTreesConfiguration,
                "qupath-object-classifier",
                True,
            ),
            "qupath-object-svm/v1": (QuPathObjectSvmConfiguration, "qupath-object-svm-classifier", True),
            "qupath-object-logistic-regression/v1": (QuPathObjectLogisticRegressionConfiguration, "qupath-object-logistic-regression-classifier", True),
            "qupath-pixel-svm/v1": (
                QuPathPixelSvmConfiguration,
                "qupath-pixel-svm-classifier",
                False,
            ),
            "qupath-kronos-rtrees/v2": (
                QuPathKronosConfiguration,
                "qupath-kronos-rtrees-classifier",
                False,
            ),
            "qupath-kronos-svm/v2": (
                QuPathKronosConfiguration,
                "qupath-kronos-svm-classifier",
                False,
            ),
        }
        route = routes.get(configuration_schema) if isinstance(configuration_schema, str) else None
        if configuration_schema == "qupath-grouped-cross-validation/v1":
            from .cross_validation import QuPathGroupedCVConfiguration

            search = QuPathGroupedCVConfiguration.from_mapping(model_configuration)
            route = (QuPathGroupedCVConfiguration, search.engine_id, isinstance(search.base_configuration, (QuPathObjectRTreesConfiguration, QuPathObjectSvmConfiguration, QuPathObjectLogisticRegressionConfiguration)))
            if any(role.split == "validation" for role in selected.roles):
                raise BatchTrainingError(
                    "Grouped tuning uses development subjects and optional test subjects, not a separate validation cohort"
                )
            from mif_pipeline.pipeline_core.model_selection import subject_folds

            subject_folds(
                [entry.subject_id for entry in plan_entries if entry.split == "training"],
                search.folds,
                selected.random_seed,
            )
        if route is None or route[2] is not is_object:
            raise BatchTrainingError(
                "model configuration schema is unsupported for classifier kind"
            )
        if classifier_kind is ClassifierKind.PRIMARY_COMPARTMENT:
            required_classes = primary_training_required_classes(settings, model_configuration)
        else:
            configuration = route[0].from_mapping(model_configuration)
            if classifier_kind is ClassifierKind.SECONDARY_STRUCTURE and {
                item.canonical_class_id for item in configuration.output_labels.ordered_labels
            } != set(required_classes):
                raise BatchTrainingError("Secondary model labels differ from the selected structures")
        request = TrainingPlanRequest(
            batch_id=manifest.manifest.batch_id,
            normalized_manifest_sha256=manifest.normalized_manifest_sha256,
            classifier_id=(
                settings.primary_classifier.classifier_id
                if classifier_kind is ClassifierKind.PRIMARY_COMPARTMENT
                else selected.candidate_id
            ),
            classifier_kind=classifier_kind,
            classifier_engine=route[1],
            evaluation_intent=selected.evaluation_intent,
            validation_domain_id=selected_samples[0].validation_domain_id,
            entries=plan_entries,
            analysis_domain_bindings=selected_bindings,
            required_classes=required_classes,
            eligible_parent_classes=eligible_parents,
            channel_requirements=channel_requirements,
            feature_requirements=feature_requirements,
            model_configuration=model_configuration,
            random_seed=selected.random_seed,
            primary_settings=(
                settings if classifier_kind is ClassifierKind.PRIMARY_COMPARTMENT else None
            ),
            application_entries=(primary_application[0] if primary_application is not None else ()),
            application_analysis_domain_bindings=(
                primary_application[1] if primary_application is not None else ()
            ),
        )
        if classifier_kind is ClassifierKind.SUPERVISED_PHENOTYPE and supervised_reference_predecessor is not None:
            from .supervised_references import inherited_reference_selection_sha256

            previous = next((item for item in supervised_reference_predecessor.plans
                             if item.request.classifier_id == request.classifier_id), None)
            if previous is not None:
                request = replace(request, inherited_reference_selection_sha256=
                    inherited_reference_selection_sha256(_normalized_request(request), previous))
        entry_authorities = tuple(
            TrainingPlanEntryAuthority(
                project_catalogue_entry=project_ref_by_id[entry_id],
                analysis_frame=frames[entries_by_id[entry_id].sample_id],
                accepted_analysis_domain_authorities=tuple(domain_by_entry.get(entry_id, ())),
                additional_source_authorities=(
                    tuple(additional_by_entry.get(entry_id, ()))
                    if classifier_kind is ClassifierKind.SUPERVISED_PHENOTYPE
                    else ()
                ),
            )
            for entry_id in selected_entry_ids
        )
        plans.append(
            TrainingPlan.create(
                request,
                manifest,
                entry_authorities,
                project_catalogue_witness=(
                    _selected_project_catalogue_witness(
                        selected_entry_ids=(
                            tuple(item.project_entry_id for item in primary_application[0])
                            if primary_application is not None
                            else selected_entry_ids
                        ),
                        project_ref_by_id=project_ref_by_id,
                    )
                ),
                application_entry_authorities=(
                    primary_application[2] if primary_application is not None else ()
                ),
            )
        )
    return TrainingPlanSet.create(tuple(plans))


@dataclass(frozen=True, slots=True)
class SavedClassifierSelection:
    """Retain the accepted model and the images selected for its application.

    We keep source fitting provenance in the export. These application entries
    and domains describe only the destination images; they assign no training roles.
    """

    batch_id: str
    declaration: ReusableArtifactDeclaration
    application_entries: tuple[TrainingPlanApplicationEntry, ...]
    analysis_domain_bindings: tuple[AnalysisDomainBinding, ...]
    source_artifact_ref_sha256s: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.batch_id, "saved classifier batch")
        if type(self.declaration) is not ReusableArtifactDeclaration or not self.declaration.target_sample_ids:
            raise BatchTrainingError("Saved classifier selection requires its explicit destination declaration")
        entries = tuple(self.application_entries)
        bindings = tuple(self.analysis_domain_bindings)
        if (not entries or any(type(item) is not TrainingPlanApplicationEntry for item in entries)
                or len({item.project_entry_id for item in entries}) != len(entries)
                or entries != tuple(sorted(entries, key=lambda item: item.project_entry_id))
                or tuple(sorted(item.sample_id for item in entries)) != self.declaration.target_sample_ids):
            raise BatchTrainingError("Saved classifier application images differ from its declared targets")
        by_entry = {item.project_entry_id: item for item in entries}
        if (not bindings or any(type(item) is not AnalysisDomainBinding for item in bindings)
                or bindings != tuple(sorted(set(bindings), key=_binding_sort_key))
                or {item.project_entry_id for item in bindings} != set(by_entry)
                or any((item.sample_id, item.slide_id) !=
                    (by_entry[item.project_entry_id].sample_id, by_entry[item.project_entry_id].slide_id)
                    for item in bindings)):
            raise BatchTrainingError("Saved classifier domains differ from its application images")
        settings = PrimaryClassifierSettings.from_mapping(self.declaration.primary_settings)
        for entry in entries:
            settings.validate_channels(entry.channels)
        sources = _string_tuple(self.source_artifact_ref_sha256s, "saved classifier input refs", nonempty=True, canonical=True)
        for digest in sources:
            _sha256(digest, "saved classifier input ref")
        object.__setattr__(self, "application_entries", entries)
        object.__setattr__(self, "analysis_domain_bindings", bindings)
        object.__setattr__(self, "source_artifact_ref_sha256s", sources)

    def to_mapping(self) -> dict[str, object]:
        return {"batch_id": self.batch_id, "declaration": self.declaration.to_mapping(),
                "application_entries": [item.to_mapping() for item in self.application_entries],
                "analysis_domain_bindings": [item.to_mapping() for item in self.analysis_domain_bindings],
                "source_artifact_ref_sha256s": list(self.source_artifact_ref_sha256s)}

    @classmethod
    def from_mapping(cls, value: object) -> "SavedClassifierSelection":
        row = _closed_mapping(value, fields=frozenset(cls.__dataclass_fields__), field="saved classifier selection")
        return cls(row["batch_id"], ReusableArtifactDeclaration.from_mapping(row["declaration"], index=0),
                   tuple(TrainingPlanApplicationEntry.from_mapping(item) for item in _array(row["application_entries"], "application entries")),
                   _bindings_from_mapping(row["analysis_domain_bindings"]),
                   tuple(_array(row["source_artifact_ref_sha256s"], "source refs")))


@dataclass(frozen=True, slots=True)
class SkippedPrimarySelection:
    """An explicit primary skip for the existing accepted images and regions."""

    batch_id: str
    application_entries: tuple[TrainingPlanApplicationEntry, ...]
    analysis_domain_bindings: tuple[AnalysisDomainBinding, ...]
    source_artifact_ref_sha256s: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.batch_id, "primary skip batch")
        entries = tuple(self.application_entries)
        bindings = tuple(self.analysis_domain_bindings)
        if (not entries or any(type(item) is not TrainingPlanApplicationEntry for item in entries)
                or len({item.project_entry_id for item in entries}) != len(entries)
                or entries != tuple(sorted(entries, key=lambda item: item.project_entry_id))):
            raise BatchTrainingError("Primary skip requires the exact application images")
        by_entry = {item.project_entry_id: item for item in entries}
        if (not bindings or any(type(item) is not AnalysisDomainBinding for item in bindings)
                or bindings != tuple(sorted(set(bindings), key=_binding_sort_key))
                or {item.project_entry_id for item in bindings} != set(by_entry)
                or any((item.sample_id, item.slide_id) !=
                    (by_entry[item.project_entry_id].sample_id, by_entry[item.project_entry_id].slide_id)
                    for item in bindings)):
            raise BatchTrainingError("Primary skip regions differ from its application images")
        sources = _string_tuple(self.source_artifact_ref_sha256s, "primary skip input refs", nonempty=True, canonical=True)
        for digest in sources:
            _sha256(digest, "primary skip input ref")
        object.__setattr__(self, "application_entries", entries)
        object.__setattr__(self, "analysis_domain_bindings", bindings)
        object.__setattr__(self, "source_artifact_ref_sha256s", sources)

    def to_mapping(self) -> dict[str, object]:
        return {"batch_id": self.batch_id,
                "application_entries": [item.to_mapping() for item in self.application_entries],
                "analysis_domain_bindings": [item.to_mapping() for item in self.analysis_domain_bindings],
                "source_artifact_ref_sha256s": list(self.source_artifact_ref_sha256s)}

    @classmethod
    def from_mapping(cls, value: object) -> "SkippedPrimarySelection":
        row = _closed_mapping(value, fields=frozenset(cls.__dataclass_fields__), field="primary skip selection")
        return cls(row["batch_id"],
                   tuple(TrainingPlanApplicationEntry.from_mapping(item) for item in _array(row["application_entries"], "application entries")),
                   _bindings_from_mapping(row["analysis_domain_bindings"]),
                   tuple(_array(row["source_artifact_ref_sha256s"], "source refs")))


@dataclass(frozen=True, slots=True)
class TrainingPlanSet:
    schema_name: ClassVar[str] = "mif-training-plan-set"
    schema_version: ClassVar[int] = 2

    plans: tuple[TrainingPlan, ...]
    normalized_manifest_sha256: str
    validation_domain_id: str
    classifier_kind: ClassifierKind
    settings_revision: TrainingSettingsRevision | None
    training_plan_set_sha256: str
    saved_selection: SavedClassifierSelection | None = None
    skipped_primary: SkippedPrimarySelection | None = None

    def __post_init__(self) -> None:
        """Validate the TrainingPlanSet fields when the record is constructed.

        The checks cover ``plans``, ``normalized_manifest_sha256``,
        ``validation_domain_id``, ``training_plan_set_sha256``, ``classifier_kind``,
        ``settings_revision``. Invalid field types, values or combinations raise the
        declared validation error before this instance can be used by its caller."""

        plans = tuple(self.plans)
        if sum((bool(plans), self.saved_selection is not None, self.skipped_primary is not None)) != 1:
            raise BatchTrainingError("Classifier setup requires training, a saved model, or an explicit primary skip")
        if self.skipped_primary is not None and (
                type(self.skipped_primary) is not SkippedPrimarySelection
                or self.classifier_kind is not ClassifierKind.PRIMARY_COMPARTMENT
                or self.settings_revision is not None):
            raise BatchTrainingError("Only primary classification can be skipped without model settings")
        if self.saved_selection is not None and (type(self.saved_selection) is not SavedClassifierSelection
                or self.classifier_kind is not ClassifierKind.PRIMARY_COMPARTMENT
                or self.saved_selection.declaration.validation_domain_id != self.validation_domain_id):
            raise BatchTrainingError("Saved classifier setup role or validation domain differs")
        if not all(isinstance(item, TrainingPlan) for item in plans):
            raise BatchTrainingError("training plan set plans must be typed")
        if plans != tuple(sorted(plans, key=_plan_set_sort_key)):
            raise BatchTrainingError("training plan set plans must use canonical order")
        if len({item.training_plan_id for item in plans}) != len(plans):
            raise BatchTrainingError("training plan set plans must be unique")
        manifest_sha = _sha256(self.normalized_manifest_sha256, "normalized_manifest_sha256")
        validation_domain_id = _text(self.validation_domain_id, "validation_domain_id")
        if not isinstance(self.classifier_kind, ClassifierKind):
            raise BatchTrainingError("classifier_kind must be a ClassifierKind")
        if any(plan.normalized_manifest_sha256 != manifest_sha for plan in plans):
            raise BatchTrainingError("training plans have different normalized manifests")
        if any(plan.request.validation_domain_id != validation_domain_id for plan in plans):
            raise BatchTrainingError("training plans have different validation domains")
        if any(plan.request.classifier_kind is not self.classifier_kind for plan in plans):
            raise BatchTrainingError("training plans have a different classifier kind")
        if len({item.request.classifier_id for item in plans}) != len(plans):
            raise BatchTrainingError("training plan set classifier IDs must be unique")
        if self.saved_selection is None and self.skipped_primary is None and self.classifier_kind is not ClassifierKind.SECONDARY_STRUCTURE and len(plans) != 1:
            raise BatchTrainingError(
                "primary and supervised training plan sets require exactly one plan"
            )
        if (
            self.settings_revision is not None
            and type(self.settings_revision) is not TrainingSettingsRevision
        ):
            raise BatchTrainingError("training plan set settings revision is invalid")
        object.__setattr__(self, "plans", plans)
        object.__setattr__(self, "normalized_manifest_sha256", manifest_sha)
        object.__setattr__(self, "validation_domain_id", validation_domain_id)
        set_sha = _sha256(self.training_plan_set_sha256, "training_plan_set_sha256")
        if set_sha != _digest(self._unsigned_mapping()):
            raise BatchTrainingError("training_plan_set_sha256 is not canonical")
        object.__setattr__(self, "training_plan_set_sha256", set_sha)

    @classmethod
    def create(
        cls,
        plans: tuple[TrainingPlan, ...],
        *,
        settings_revision: TrainingSettingsRevision | None = None,
    ) -> "TrainingPlanSet":
        supplied = tuple(plans)
        if (
            settings_revision is not None
            and type(settings_revision) is not TrainingSettingsRevision
        ):
            raise BatchTrainingError("training plan set settings revision is invalid")
        if not supplied:
            raise BatchTrainingError("training plan set plans must not be empty")
        if not all(isinstance(item, TrainingPlan) for item in supplied):
            raise BatchTrainingError("training plan set plans must be typed")
        ordered = tuple(sorted(supplied, key=_plan_set_sort_key))
        first = ordered[0]
        unsigned = {
            "plans": ordered,
            "normalized_manifest_sha256": first.normalized_manifest_sha256,
            "validation_domain_id": first.request.validation_domain_id,
            "classifier_kind": first.request.classifier_kind,
            "settings_revision": settings_revision,
            "saved_selection": None,
            "skipped_primary": None,
        }
        prototype = object.__new__(cls)
        for name, item in unsigned.items():
            object.__setattr__(prototype, name, item)
        return cls(
            **unsigned,
            training_plan_set_sha256=_digest(prototype._unsigned_mapping()),
        )

    @classmethod
    def from_saved_selection(cls, selection: SavedClassifierSelection, *, normalized_manifest_sha256: str) -> "TrainingPlanSet":
        """Publish a saved-model choice through the existing classifier setup store."""
        unsigned = dict(plans=(), normalized_manifest_sha256=normalized_manifest_sha256,
                        validation_domain_id=selection.declaration.validation_domain_id,
                        classifier_kind=ClassifierKind.PRIMARY_COMPARTMENT,
                        settings_revision=None, saved_selection=selection, skipped_primary=None)
        prototype = object.__new__(cls)
        for name, item in unsigned.items():
            object.__setattr__(prototype, name, item)
        return cls(**unsigned, training_plan_set_sha256=_digest(prototype._unsigned_mapping()))

    @classmethod
    def from_primary_skip(cls, selection: SkippedPrimarySelection, *, normalized_manifest_sha256: str,
                          validation_domain_id: str) -> "TrainingPlanSet":
        unsigned = dict(plans=(), normalized_manifest_sha256=normalized_manifest_sha256,
                        validation_domain_id=validation_domain_id,
                        classifier_kind=ClassifierKind.PRIMARY_COMPARTMENT,
                        settings_revision=None, saved_selection=None, skipped_primary=selection)
        prototype = object.__new__(cls)
        for name, item in unsigned.items():
            object.__setattr__(prototype, name, item)
        return cls(**unsigned, training_plan_set_sha256=_digest(prototype._unsigned_mapping()))

    @classmethod
    def from_mapping(cls, value: object) -> "TrainingPlanSet":
        fields = frozenset({"schema_name", "schema_version", *cls.__dataclass_fields__})
        if isinstance(value, Mapping) and "saved_selection" not in value:
            fields = fields - {"saved_selection"}
        if isinstance(value, Mapping) and "skipped_primary" not in value:
            fields = fields - {"skipped_primary"}
        mapping = _closed_mapping(value, fields=fields, field="training_plan_set")
        if (
            mapping["schema_name"] != cls.schema_name
            or mapping["schema_version"] != cls.schema_version
        ):
            raise BatchTrainingError("training plan set schema is unsupported")
        try:
            settings_revision = (
                None
                if mapping["settings_revision"] is None
                else TrainingSettingsRevision.from_mapping(mapping["settings_revision"])
            )
        except BatchArtifactError as exc:
            raise BatchTrainingError("training plan set settings revision is invalid") from exc
        return cls(
            plans=tuple(
                TrainingPlan.from_mapping(item) for item in _array(mapping["plans"], "plans")
            ),
            normalized_manifest_sha256=mapping[
                "normalized_manifest_sha256"
            ],  # type: ignore[arg-type]
            validation_domain_id=mapping["validation_domain_id"],  # type: ignore[arg-type]
            classifier_kind=_enum(mapping["classifier_kind"], ClassifierKind, "classifier_kind"),
            settings_revision=settings_revision,
            saved_selection=(None if mapping.get("saved_selection") is None else SavedClassifierSelection.from_mapping(mapping["saved_selection"])),
            skipped_primary=(None if mapping.get("skipped_primary") is None else SkippedPrimarySelection.from_mapping(mapping["skipped_primary"])),
            training_plan_set_sha256=mapping["training_plan_set_sha256"],  # type: ignore[arg-type]
        )

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            **({"saved_selection": self.saved_selection.to_mapping()} if self.saved_selection is not None else {}),
            **({"skipped_primary": self.skipped_primary.to_mapping()} if self.skipped_primary is not None else {}),
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "plans": [item.to_mapping() for item in self.plans],
            "normalized_manifest_sha256": self.normalized_manifest_sha256,
            "validation_domain_id": self.validation_domain_id,
            "classifier_kind": self.classifier_kind.value,
            "settings_revision": (
                None if self.settings_revision is None else self.settings_revision.to_mapping()
            ),
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            **self._unsigned_mapping(),
            "training_plan_set_sha256": self.training_plan_set_sha256,
        }


def training_plan_set_identity_contract() -> ArtifactIdentityContract:
    """Return the compact current owner contract for a durable plan-set body."""

    return ArtifactIdentityContract(
        contract_id="mif-batch-training-plan-set-identity/v2",
        artifact_kind="training_plan_set",
        schema_name="mif-training-plan-set",
        schema_version=TrainingPlanSet.schema_version,
        required_fields=TRAINING_PLAN_SET_IDENTITY_FIELDS,
        optional_fields=(),
        required_runtime_names=("mif_pipeline",),
    )


def _training_plan_set_analysis_domain_bindings(
    plan_set: TrainingPlanSet,
) -> tuple[AnalysisDomainBinding, ...]:
    if plan_set.saved_selection is not None:
        return plan_set.saved_selection.analysis_domain_bindings
    if plan_set.skipped_primary is not None:
        return plan_set.skipped_primary.analysis_domain_bindings
    by_identity: dict[bytes, AnalysisDomainBinding] = {}
    for plan in plan_set.plans:
        for binding in (
            plan.request.application_analysis_domain_bindings
            if plan.request.classifier_kind is ClassifierKind.PRIMARY_COMPARTMENT
            else plan.request.analysis_domain_bindings
        ):
            identity = _binding_identity(binding)
            by_identity.setdefault(identity, binding)
    return tuple(sorted(by_identity.values(), key=_binding_sort_key))


def validate_primary_skip_artifact_ref(
    ref: BatchArtifactRef, *, domain_bindings: tuple[AnalysisDomainBinding, ...],
) -> BatchArtifactRef:
    """Check the compact explicit choice supplied by a committed input owner.

    This validates a reference, not its current-state authority. The caller
    must already authenticate it as an input of the current operation.
    """
    ref = validate_artifact_ref(ref)
    identity = ref.owning_identity
    if (ref.identity_contract != training_plan_set_identity_contract()
            or identity.get("classifier_kind") != ClassifierKind.PRIMARY_COMPARTMENT.value
            or any(identity.get(key) != () for key in (
                "classifier_ids", "classifier_engines", "training_plan_ids", "training_plan_sha256s"))
            or not domain_bindings
            or not set(domain_bindings) <= set(ref.scope.analysis_domain_bindings)):
        raise BatchArtifactError("Primary classification has not been explicitly skipped for these analysis regions")
    return ref


def _training_plan_set_compact_facts(
    plan_set: TrainingPlanSet,
) -> dict[str, object]:
    if type(plan_set) is not TrainingPlanSet:
        raise BatchTrainingError("plan_set must be an exact TrainingPlanSet")
    if plan_set.skipped_primary is not None:
        selected = plan_set.skipped_primary
        bindings = selected.analysis_domain_bindings
        sources = selected.source_artifact_ref_sha256s
        return {
            "batch_id": selected.batch_id, "normalized_manifest_sha256": plan_set.normalized_manifest_sha256,
            "validation_domain_id": plan_set.validation_domain_id, "classifier_kind": plan_set.classifier_kind.value,
            "classifier_ids": (), "classifier_engines": (), "training_plan_ids": (), "training_plan_sha256s": (),
            "training_plan_set_sha256": plan_set.training_plan_set_sha256,
            "participant_ids": tuple(sorted({item.sample_id for item in selected.application_entries})),
            "project_entry_ids": tuple(item.project_entry_id for item in selected.application_entries),
            "analysis_domain_bindings": bindings,
            "analysis_domain_binding_set_sha256": _digest([item.to_mapping() for item in bindings]),
            "source_artifact_ref_sha256s": sources,
            "source_artifact_ref_set_sha256": _digest({"artifact_ref_sha256s": list(sources)}),
        }
    if plan_set.saved_selection is not None:
        selected = plan_set.saved_selection
        settings = PrimaryClassifierSettings.from_mapping(selected.declaration.primary_settings)
        bindings = selected.analysis_domain_bindings
        sources = selected.source_artifact_ref_sha256s
        return {
            "batch_id": selected.batch_id, "normalized_manifest_sha256": plan_set.normalized_manifest_sha256,
            "validation_domain_id": plan_set.validation_domain_id, "classifier_kind": plan_set.classifier_kind.value,
            "classifier_ids": (settings.primary_classifier.classifier_id,), "classifier_engines": (),
            "training_plan_ids": (), "training_plan_sha256s": (),
            "training_plan_set_sha256": plan_set.training_plan_set_sha256,
            "participant_ids": selected.declaration.target_sample_ids,
            "project_entry_ids": tuple(item.project_entry_id for item in selected.application_entries),
            "analysis_domain_bindings": bindings,
            "analysis_domain_binding_set_sha256": _digest([item.to_mapping() for item in bindings]),
            "source_artifact_ref_sha256s": sources,
            "source_artifact_ref_set_sha256": _digest({"artifact_ref_sha256s": list(sources)}),
        }
    # i) Our plan constructors validate and freeze their scientific scope.
    # ii) Publication reuses that current typed owner without rebuilding its
    # complete nested plans; retained external bytes use the decoder on reopen.
    batch_ids = {item.request.batch_id for item in plan_set.plans}
    if len(batch_ids) != 1:
        raise BatchTrainingError("training plan set must name one exact batch_id")
    participants = tuple(
        sorted(
            {
                entry.sample_id
                for plan in plan_set.plans
                for entry in (
                    plan.request.application_entries
                    if plan.request.classifier_kind is ClassifierKind.PRIMARY_COMPARTMENT
                    else plan.request.entries
                )
            }
        )
    )
    project_entries = tuple(
        sorted(
            {
                entry.project_entry_id
                for plan in plan_set.plans
                for entry in (
                    plan.request.application_entries
                    if plan.request.classifier_kind is ClassifierKind.PRIMARY_COMPARTMENT
                    else plan.request.entries
                )
            }
        )
    )
    source_refs = tuple(
        sorted(
            {
                digest
                for plan in plan_set.plans
                for digest in plan.authoring_source_artifact_ref_sha256s
            }
        )
    )
    bindings = _training_plan_set_analysis_domain_bindings(plan_set)
    return {
        "batch_id": next(iter(batch_ids)),
        "normalized_manifest_sha256": plan_set.normalized_manifest_sha256,
        "validation_domain_id": plan_set.validation_domain_id,
        "classifier_kind": plan_set.classifier_kind.value,
        "classifier_ids": tuple(item.request.classifier_id for item in plan_set.plans),
        "classifier_engines": tuple(item.classifier_engine for item in plan_set.plans),
        "training_plan_ids": tuple(item.training_plan_id for item in plan_set.plans),
        "training_plan_sha256s": tuple(item.training_plan_sha256 for item in plan_set.plans),
        "training_plan_set_sha256": plan_set.training_plan_set_sha256,
        "participant_ids": participants,
        "project_entry_ids": project_entries,
        "analysis_domain_bindings": bindings,
        "analysis_domain_binding_set_sha256": _digest([item.to_mapping() for item in bindings]),
        "source_artifact_ref_sha256s": source_refs,
        "source_artifact_ref_set_sha256": _digest({"artifact_ref_sha256s": list(source_refs)}),
    }


@dataclass(frozen=True, slots=True, init=False)
class ValidatedTrainingPlanSetProjection:
    """Decoder-issued plan-set value plus compact authenticated owner facts."""

    artifact_ref: BatchArtifactRef
    plan_set: TrainingPlanSet
    state_head_sha256: str
    active_plan_sha256: str
    terminal_record_sha256: str
    producer_node_kind: str
    batch_id: str
    normalized_manifest_sha256: str
    validation_domain_id: str
    classifier_kind: ClassifierKind
    participant_ids: tuple[str, ...]
    project_entry_ids: tuple[str, ...]
    configuration_bindings: tuple[ArtifactConfigurationBinding, ...]
    analysis_domain_bindings: tuple[AnalysisDomainBinding, ...]
    analysis_domain_binding_set_sha256: str
    source_artifact_ref_set_sha256: str
    classifier_ids: tuple[str, ...]
    classifier_engines: tuple[str, ...]
    training_plan_ids: tuple[str, ...]
    training_plan_sha256s: tuple[str, ...]
    store_relative_locator: str
    body_sha256: str
    body_byte_count: int

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("ValidatedTrainingPlanSetProjection is decoder-only")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("ValidatedTrainingPlanSetProjection cannot be subclassed")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("ValidatedTrainingPlanSetProjection is nonserializable")

    def require_plan(self, training_plan_id: str) -> TrainingPlan:
        requested = _sha256(training_plan_id, "training_plan_id")
        matches = tuple(item for item in self.plan_set.plans if item.training_plan_id == requested)
        if len(matches) != 1:
            raise BatchTrainingError("training plan selection must resolve exactly one plan")
        return matches[0]

    def require_entry(
        self,
        *,
        training_plan_id: str,
        project_entry_id: str,
        split: Literal["training", "validation", "test"],
    ) -> TrainingPlanEntry:
        plan = self.require_plan(training_plan_id)
        entry_id = _text(project_entry_id, "project_entry_id")
        if type(split) is not str or split not in _SPLITS:
            raise BatchTrainingError("split must be training, validation, or test")
        matches = tuple(
            item
            for item in plan.request.entries
            if item.project_entry_id == entry_id and item.split == split
        )
        if len(matches) != 1:
            raise BatchTrainingError(
                "training plan entry selection must resolve exactly one entry role"
            )
        return matches[0]


def _issue_validated_training_plan_set_projection(
    **values: object,
) -> ValidatedTrainingPlanSetProjection:
    projection = object.__new__(ValidatedTrainingPlanSetProjection)
    fields = ValidatedTrainingPlanSetProjection.__dataclass_fields__
    if set(values) != set(fields):
        raise BatchTrainingError("validated training plan set projection fields are incomplete")
    for name in fields:
        object.__setattr__(projection, name, values[name])
    return projection
