"""Publish the fitted classifier records and retain the accepted evaluation decisions.

The record set binds model files, features, reference evidence, subject roles and training
configuration. The freeze decision is joined to any subsequent independent test results
before final acceptance. Reopening checks that this history names the same fitted model and
exact permitted review actions."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
from typing import Mapping

from mif_pipeline.compartment_analysis.classifiers import (
    ClassifierDefinition,
    TrainingEvidenceSnapshot,
)
from mif_pipeline.pipeline_core import SecondaryStructureSpec
from mif_pipeline.pipeline_core.durable_records import (
    DurableDirectoryAuthority,
    DurableRecordError,
)
from mif_pipeline.pipeline_core.stage_result import InteractionReason, StageResult

from ..batch_analysis_preflight import MIF_PIPELINE_BATCH_RUNTIME_VERSION
from ..batch_artifacts import (
    AnalysisDomainBinding,
    ArtifactConfigurationBinding,
    ArtifactOrigin,
    ArtifactScope,
    BatchArtifactError,
    BatchArtifactRef,
    BatchCommittedArtifactAuthority,
    BatchNodeExecutionResult,
    StageAdapterRegistry,
    validate_artifact_ref,
    validate_committed_artifact_authority_set,
)
from ..batch_state import (
    BatchRawNodeOutcome,
    BatchStateError,
    BatchStateStore,
    CurrentNativeFitSetAuthority,
    ScheduledNodeResult,
    _classifier_record_native_fit_inventory_sha256,
    _classifier_record_native_fit_projection,
)
from ._shared import (
    BatchTrainingError,
    ClassifierArtifactKind,
    ClassifierKind,
    EvaluationIntent,
    _SPLIT_RANK,
    _array,
    _canonical_bytes,
    _closed_mapping,
    _digest,
    _export_json,
    _sha256,
    _text,
    _version_inventory,
)
from .evaluation import (
    HeldOutClassifierEvaluation,
    _consume_held_out_classifier_evaluations,
)
from .evidence import (
    MultiImagePixelFitEvidence,
    MultiImageSupervisedFitEvidence,
    MultiImageSupervisedTrainingAggregate,
    MultiImageTrainingEvidence,
)
from .engines import validate_authorized_training_interaction_response
from .native_session import (
    QuPathTrainingMutationSession,
    _claim_finalizable_training_session,
    _require_finalizable_training_session,
)
from .native_projection import (
    QuPathPixelEntryChannelFeatures,
    project_pixel_channel_features,
)
from .parent_execution import open_current_training_parent_execution
from .planning import TrainingPlan, TrainingPlanSet, ValidatedTrainingPlanSetProjection
from .plan_selection import classifier_estimator_settings
from .records import (
    ClassifierEvaluationMetrics,
    ClassifierRecord,
    ClassifierRecordSet,
    PixelClassifierOwnerPayload,
    SupervisedClassifierOwnerPayload,
    _qupath_executable_version_label,
    _validate_record_metrics,
    _validate_record_owner_plan,
    _validate_record_training_evidence,
    _require_accepted_classifier_record_set,
)
from .runtime import (
    HeldOutEvaluationTargetAuthority,
    _reopen_held_out_evaluation_target,
)
from .stage_adapters import (
    TRAINING_PARENT_NODE_KINDS,
    build_training_stage_adapters,
    classifier_record_set_identity_contract,
)

MAX_CLASSIFIER_RECORD_SET_BYTES = 16 * 1024 * 1024
_OBJECTS_DIRECTORY = "objects"
_PRODUCER_VERSION = "mif-pipeline-classifier-record-set/v1"
_PARENT_KIND_BY_CLASSIFIER = {
    ClassifierKind.PRIMARY_COMPARTMENT: "primary_classifier_training_review",
    ClassifierKind.SECONDARY_STRUCTURE: "secondary_classifier_training_review",
    ClassifierKind.SUPERVISED_PHENOTYPE: "supervised_phenotype_training_review",
}


# ==============================================================================
#                               Record publication
# ==============================================================================


def training_fit_review_actions(context: Mapping[str, object]) -> tuple[str, ...]:
    """Post-search estimator-only changes would sever the selected-candidate evidence."""
    if context["phase"] != "freeze_settings" or any(
        row.get("model_selection") for row in context["classifiers"]
    ):
        return ("accept",)
    return ("accept", "retrain_classifier")


def validate_training_fit_review_context(value: object) -> Mapping[str, object]:
    """Validate compact review data; this grants no acceptance authority.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    if isinstance(value, Mapping) and value.get("phase") == "configure_outputs":
        from .output_review import validate_output_policy_review
        return validate_output_policy_review(value)

    context = _closed_mapping(
        value,
        fields=frozenset(
            {
                "schema_version",
                "fit_review_sha256",
                "pending_classifier_record_set_sha256",
                "training_plan_set_sha256",
                "checkpoint_set_sha256",
                "classifiers",
                "evaluation_evidence",
                "phase",
            }
        ),
        field="saved classifier review",
    )
    if context["schema_version"] not in {"training-fit-review/v4", "training-fit-review/v5"}:
        raise BatchTrainingError("saved classifier review version is unsupported")
    phase = context["phase"]
    if phase not in ("freeze_settings", "final_results"):
        raise BatchTrainingError("saved classifier review phase is unsupported")
    digest = _sha256(context["fit_review_sha256"], "fit_review_sha256")
    hashes = {
        field: _sha256(context[field], field)
        for field in (
            "pending_classifier_record_set_sha256",
            "training_plan_set_sha256",
            "checkpoint_set_sha256",
        )
    }
    classifiers = []
    for value in _array(context["classifiers"], "review classifiers"):
        row = _closed_mapping(
            value,
            fields=frozenset(
                {
                    "classifier_id",
                    "evaluation_intent",
                    "training_patient_count",
                    "validation_patient_count",
                    "test_patient_count",
                    "split_metrics",
                    "estimator_settings",
                    "adjusted_after_validation",
                }
                | ({"training_observations"} if isinstance(value, Mapping) and "training_observations" in value else set())
                | (
                    {"model_selection"}
                    if context["schema_version"] == "training-fit-review/v5"
                    else set()
                )
            ),
            field="review classifier",
        )
        classifier_id = _text(row["classifier_id"], "classifier_id")
        settings = row["estimator_settings"]
        if not isinstance(settings, Mapping):
            raise BatchTrainingError("classifier estimator settings must be an object")
        estimator = classifier_estimator_settings(settings)
        if set(settings) != set(estimator) or type(row["adjusted_after_validation"]) is not bool:
            raise BatchTrainingError("classifier review settings or revision label are invalid")
        try:
            intent = EvaluationIntent(row["evaluation_intent"])
        except (TypeError, ValueError) as exc:
            raise BatchTrainingError("review evaluation intent is invalid") from exc
        counts = {}
        for split in ("training", "validation", "test"):
            count = row[f"{split}_patient_count"]
            if type(count) is not int or count < (1 if split == "training" else 0):
                raise BatchTrainingError(f"{split} patient count is invalid")
            counts[split] = count
        selection = None
        if (
            context["schema_version"] == "training-fit-review/v5"
            and row["model_selection"] is not None
        ):
            from .cross_validation import validate_compact_selection_summary

            selection = validate_compact_selection_summary(
                row["model_selection"], counts["training"]
            )
            if counts["validation"]:
                raise BatchTrainingError(
                    "Grouped CV review has development subjects, not an extra validation cohort"
                )
        metric_rows = []
        for raw_metric in _array(row["split_metrics"], "review split_metrics"):
            metric = _closed_mapping(
                raw_metric,
                fields=frozenset(
                    {
                        "split",
                        "patient_count",
                        "accuracy",
                        "macro_precision",
                        "macro_recall",
                        "macro_f1",
                    }
                ),
                field="review split metric",
            )
            split = metric["split"]
            if split not in ("validation", "test") or type(metric["patient_count"]) is not int:
                raise BatchTrainingError("review split metric scope is invalid")
            if counts[split] <= 0 or metric["patient_count"] != counts[split]:
                raise BatchTrainingError("review split metric patient count differs from summary")
            parsed_metric = {"split": split, "patient_count": counts[split]}
            for name in ("accuracy", "macro_precision", "macro_recall", "macro_f1"):
                score = metric[name]
                if (
                    type(score) not in (int, float)
                    or not math.isfinite(score)
                    or not 0 <= score <= 1
                ):
                    raise BatchTrainingError("review split metric score is invalid")
                parsed_metric[name] = score
            metric_rows.append(parsed_metric)
        splits = [metric["split"] for metric in metric_rows]
        visible_splits = ("validation",) if phase == "freeze_settings" else ("validation", "test")
        if splits != [split for split in visible_splits if counts[split]]:
            raise BatchTrainingError("review metrics must use exact nonempty split order")
        if intent is EvaluationIntent.TRAINING_ONLY:
            if counts["validation"] or counts["test"] or metric_rows:
                raise BatchTrainingError("training-only review cannot show held-out results")
        elif intent is EvaluationIntent.INDEPENDENT_PERFORMANCE and not counts["test"]:
            raise BatchTrainingError("independent review requires test patients")
        elif intent is EvaluationIntent.EXPLORATORY and not counts["validation"]:
            raise BatchTrainingError("exploratory review requires validation patients")
        observations = None
        if "training_observations" in row:
            from mif_pipeline.qupath_runtime.native_classifier_fit import validate_training_observations
            observations = validate_training_observations(row["training_observations"])
        classifiers.append(
            {
                "classifier_id": classifier_id,
                "evaluation_intent": intent.value,
                **{
                    f"{split}_patient_count": counts[split]
                    for split in ("training", "validation", "test")
                },
                "split_metrics": metric_rows,
                "estimator_settings": estimator,
                "adjusted_after_validation": row["adjusted_after_validation"],
                **({"training_observations": observations} if observations is not None else {}),
                **(
                    {"model_selection": selection}
                    if context["schema_version"] == "training-fit-review/v5"
                    else {}
                ),
            }
        )
    identifiers = [row["classifier_id"] for row in classifiers]
    if not identifiers or identifiers != sorted(set(identifiers)):
        raise BatchTrainingError("review classifiers must be nonempty, unique and ordered")
    evidence = []
    for raw_evidence in _array(context["evaluation_evidence"], "review evaluation_evidence"):
        row = _closed_mapping(
            raw_evidence,
            fields=frozenset(
                {"classifier_id", "split", "request_identity_sha256", "result_sha256"}
            ),
            field="review evaluation evidence",
        )
        evidence.append(
            {
                "classifier_id": _text(row["classifier_id"], "classifier_id"),
                "split": row["split"],
                "request_identity_sha256": _sha256(
                    row["request_identity_sha256"], "request_identity_sha256"
                ),
                "result_sha256": _sha256(row["result_sha256"], "result_sha256"),
            }
        )
    expected_evaluated = [
        (row["classifier_id"], metric["split"])
        for row in classifiers
        for metric in row["split_metrics"]
    ]
    if [(row["classifier_id"], row["split"]) for row in evidence] != expected_evaluated:
        raise BatchTrainingError("review evaluation evidence lacks exact evaluated coverage")
    if phase == "final_results" and not any(row["test_patient_count"] for row in classifiers):
        raise BatchTrainingError("final-results review requires test patients")
    result = {
        "schema_version": context["schema_version"],
        "phase": phase,
        "fit_review_sha256": digest,
        **hashes,
        "classifiers": classifiers,
        "evaluation_evidence": evidence,
    }
    if digest != _digest({key: item for key, item in result.items() if key != "fit_review_sha256"}):
        raise BatchTrainingError("fit_review_sha256 is not canonical")
    return result


@dataclass(frozen=True, slots=True)
class ClassifierRecordFinalizationInput:
    """Non-wire reviewer facts joined to one exact current native fit."""

    owner_payload: PixelClassifierOwnerPayload | SupervisedClassifierOwnerPayload | None
    complete_training_evidence: MultiImageTrainingEvidence | MultiImageSupervisedTrainingAggregate
    held_out_target: HeldOutEvaluationTargetAuthority | None
    held_out_evaluation: HeldOutClassifierEvaluation | None
    retained_record: ClassifierRecord | None
    freeze_review_sha256: str | None
    feature_requirements: Mapping[str, object]
    runtime_versions: tuple[tuple[str, str], ...]
    extension_versions: tuple[tuple[str, str], ...]
    model_versions: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        """Validate the ClassifierRecordFinalizationInput fields when the record is
        constructed.

        The checks cover ``complete_training_evidence``, ``freeze_review_sha256``,
        ``owner_payload``, ``held_out_target``, ``held_out_evaluation``,
        ``retained_record``. Invalid field types, values or combinations raise the declared
        validation error before this instance can be used by its caller."""

        if self.owner_payload is not None and type(self.owner_payload) not in {
            PixelClassifierOwnerPayload,
            SupervisedClassifierOwnerPayload,
        }:
            raise BatchTrainingError("record finalization requires one exact typed owner payload")
        if type(self.complete_training_evidence) not in {
            MultiImageTrainingEvidence,
            MultiImageSupervisedTrainingAggregate,
        }:
            raise BatchTrainingError(
                "record finalization requires exact complete training evidence"
            )
        if (self.held_out_target is None) != (self.held_out_evaluation is None):
            raise BatchTrainingError("record input requires both evaluation and target")
        if (
            self.held_out_target is not None
            and type(self.held_out_target) is not HeldOutEvaluationTargetAuthority
        ):
            raise BatchTrainingError("record finalization requires one exact held-out target")
        if (
            self.held_out_evaluation is not None
            and type(self.held_out_evaluation) is not HeldOutClassifierEvaluation
        ):
            raise BatchTrainingError("record finalization requires one exact held-out evaluation")
        training_plan_id = self.complete_training_evidence.training_plan_id
        if self.held_out_target is not None and (
            self.held_out_target.training_plan_id != training_plan_id
            or self.held_out_evaluation.training_plan_id != training_plan_id
            or self.held_out_target.classifier_id != self.held_out_evaluation.classifier_id
            or self.held_out_target.classifier_kind is not self.held_out_evaluation.classifier_kind
            or self.held_out_target.validation_domain_id
            != self.held_out_evaluation.validation_domain_id
        ):
            raise BatchTrainingError("record finalization input carries mismatched plan identity")
        if self.freeze_review_sha256 is None:
            if self.owner_payload is not None or self.retained_record is not None:
                raise BatchTrainingError(
                    "pre-test review cannot carry a finalized owner or previous review"
                )
        else:
            _sha256(self.freeze_review_sha256, "freeze_review_sha256")
            if self.owner_payload is None or type(self.retained_record) is not ClassifierRecord:
                raise BatchTrainingError(
                    "finalized settings require their pending record and full owner"
                )
        for field in (
            "runtime_versions",
            "extension_versions",
            "model_versions",
        ):
            object.__setattr__(
                self,
                field,
                _version_inventory(
                    getattr(self, field),
                    field,
                    nonempty=field == "runtime_versions",
                    require_canonical=False,
                ),
            )


def _require_training_only_evidence(complete: object) -> None:
    if type(complete) is MultiImageTrainingEvidence:
        if any(item.split != "training" for item in complete.entry_states) or any(
            item.split != "training" for item in complete.rows
        ):
            raise BatchTrainingError("training-only pixel evidence contains held-out roles")
    elif type(complete) is MultiImageSupervisedTrainingAggregate:
        if any(item.split != "training" for item in complete.entries) or any(
            (
                complete.validation_cells,
                complete.validation_regions,
                complete.test_cells,
                complete.test_regions,
            )
        ):
            raise BatchTrainingError("training-only supervised evidence contains held-out roles")
    else:
        raise BatchTrainingError("training-only input requires exact complete evidence")


def _store_root(root: Path) -> Path:
    if not isinstance(root, Path) or not root.is_absolute() or root == Path(root.anchor):
        raise BatchTrainingError(
            "classifier record-set store root must be an absolute non-root Path"
        )
    if ".." in root.parts or root != root.resolve(strict=False):
        raise BatchTrainingError("classifier record-set store root must be canonical")
    return root


def provision_classifier_record_set_store_root(root: Path) -> Path:
    """Provision the Windows retained directory closure before store open."""

    canonical = _store_root(root)
    if os.name == "nt":
        from mif_pipeline.pipeline_core.win32_authority_provisioning import (
            provision_windows_current_user_directory_closure,
        )
        from mif_pipeline.pipeline_core.win32_file_authority import (
            Win32FileAuthorityError,
        )

        try:
            provisioned = provision_windows_current_user_directory_closure(
                canonical, child_names=(_OBJECTS_DIRECTORY,)
            )
        except (Win32FileAuthorityError, OSError) as exc:
            raise BatchTrainingError(
                "classifier record-set store root cannot be provisioned safely"
            ) from exc
        if provisioned != canonical:
            raise BatchTrainingError(
                "classifier record-set store provisioning changed its canonical root"
            )
    return canonical


class ClassifierRecordSetStore:
    """Retained no-follow owner for immutable canonical record-set bodies."""

    def __init__(self, root: Path) -> None:
        self._root = _store_root(root)
        self._root_authority: DurableDirectoryAuthority | None = None
        self._objects_authority: DurableDirectoryAuthority | None = None
        try:
            create_directories = os.name == "posix"
            root_authority = DurableDirectoryAuthority(self._root, create=create_directories)
            self._root_authority = root_authority
            self._objects_authority = root_authority.retain_child(
                Path(_OBJECTS_DIRECTORY), create=create_directories
            )
        except (DurableRecordError, OSError) as exc:
            self._close_after_failure()
            raise BatchTrainingError(
                "classifier record-set store cannot be retained safely"
            ) from exc
        except BaseException:
            self._close_after_failure()
            raise

    @property
    def root(self) -> Path:
        return self._root

    def _objects(self) -> DurableDirectoryAuthority:
        if self._root_authority is None or self._objects_authority is None:
            raise BatchTrainingError("classifier record-set store is closed")
        return self._objects_authority

    @staticmethod
    def _leaf(digest: object) -> str:
        return f"{_sha256(digest, 'classifier_record_set_sha256')}.json"

    def publish(self, record_set: ClassifierRecordSet) -> str:
        if type(record_set) is not ClassifierRecordSet:
            raise BatchTrainingError("classifier record-set store requires an exact record set")
        body = _canonical_bytes(record_set.to_mapping())
        if not 0 < len(body) <= MAX_CLASSIFIER_RECORD_SET_BYTES:
            raise BatchTrainingError(
                "classifier record-set body exceeds the durable store size bound"
            )
        leaf = self._leaf(record_set.classifier_record_set_sha256)
        try:
            self._objects().publish_immutable(
                leaf,
                body,
                max_bytes=MAX_CLASSIFIER_RECORD_SET_BYTES,
                label="classifier record-set body",
            )
        except (DurableRecordError, OSError) as exc:
            raise BatchTrainingError(
                "classifier record-set body cannot be published immutably"
            ) from exc
        self.reopen(record_set.classifier_record_set_sha256)
        return f"{_OBJECTS_DIRECTORY}/{leaf}"

    def reopen(self, classifier_record_set_sha256: str) -> ClassifierRecordSet:
        digest = _sha256(classifier_record_set_sha256, "classifier_record_set_sha256")
        try:
            body = self._objects().read_immutable(
                self._leaf(digest),
                max_bytes=MAX_CLASSIFIER_RECORD_SET_BYTES,
                label="classifier record-set body",
            )
        except (DurableRecordError, OSError) as exc:
            raise BatchTrainingError("classifier record-set body is missing or unsafe") from exc
        assert body is not None
        try:
            decoded = json.loads(
                body.decode("utf-8"),
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"invalid constant {value}")
                ),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise BatchTrainingError("classifier record-set body is not finite UTF-8 JSON") from exc
        record_set = ClassifierRecordSet.from_mapping(decoded)
        if (
            record_set.classifier_record_set_sha256 != digest
            or _canonical_bytes(record_set.to_mapping()) != body
        ):
            raise BatchTrainingError("classifier record-set body differs from its semantic digest")
        return record_set

    def _close_after_failure(self) -> None:
        try:
            self.close()
        except BaseException:
            pass

    def close(self) -> None:
        objects = self._objects_authority
        root = self._root_authority
        self._objects_authority = None
        self._root_authority = None
        failure: BaseException | None = None
        for authority in (objects, root):
            if authority is None:
                continue
            try:
                authority.close()
            except BaseException as exc:
                if failure is None:
                    failure = exc
        if failure is not None:
            if isinstance(failure, (DurableRecordError, OSError)):
                raise BatchTrainingError(
                    "classifier record-set store cannot be closed safely"
                ) from failure
            raise failure

    def __enter__(self) -> "ClassifierRecordSetStore":
        self._objects()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except (AttributeError, OSError, BatchTrainingError):
            pass


def _fit_material_by_classifier(current_fit_set: CurrentNativeFitSetAuthority):
    entries = current_fit_set.checkpoint_set.entries
    runtimes = current_fit_set.runtime_authorities
    result = {}
    for entry in entries:
        matches = tuple(
            runtime
            for runtime in runtimes
            if runtime.material_record.classifier_id == entry.classifier_id
            and runtime.material_record.fit_plan_sha256 == entry.fit_plan_sha256
            and runtime.material_record.fit_execution_sha256 == entry.fit_execution_sha256
        )
        if len(matches) != 1:
            raise BatchTrainingError("current fit lacks exact checkpoint/runtime correspondence")
        result[entry.classifier_id] = (entry, matches[0])
    if len(result) != len(entries) or len(runtimes) != len(entries):
        raise BatchTrainingError("current fit contains duplicate or extra runtime material")
    return result


def build_training_fit_review_context(
    plan_set: TrainingPlanSet,
    current_fit_set: CurrentNativeFitSetAuthority,
    pending_record_set: ClassifierRecordSet,
) -> Mapping[str, object]:
    """Project the exact pending set after its evaluation proofs were consumed."""
    if type(plan_set) is not TrainingPlanSet or type(pending_record_set) is not ClassifierRecordSet:
        raise BatchTrainingError("fit review requires exact plans and pending records")
    return _project_training_fit_review_context(
        plan_set,
        current_fit_set,
        pending_record_set,
    )


def _project_training_fit_review_context(
    plan_set,
    current_fit_set,
    pending_record_set,
):
    if type(current_fit_set) is not CurrentNativeFitSetAuthority:
        raise BatchTrainingError("fit review requires an exact current fit")
    fits = _fit_material_by_classifier(current_fit_set)
    if {record.classifier_id for record in pending_record_set.classifier_records} != set(fits):
        raise BatchTrainingError("pending record coverage differs from current native fit")
    if set(fits) != {plan.request.classifier_id for plan in plan_set.plans}:
        raise BatchTrainingError("fit review lacks exact native fit coverage")
    for record in pending_record_set.classifier_records:
        entry, runtime = fits[record.classifier_id]
        material = runtime.material_record
        if (
            record.fit_plan_sha256 != entry.fit_plan_sha256
            or record.fit_execution_sha256 != entry.fit_execution_sha256
            or record.checkpoint_entry_sha256 != entry.checkpoint_entry_sha256
            or record.fit_material_sha256 != material.fit_material_sha256
            or record.training_material_evidence_sha256
            != material.training_material_evidence_sha256
            or record.native_artifact_sha256 != material.native_classifier_sha256
            or record.native_artifact_byte_count != material.native_classifier_byte_count
            or record.native_artifact_project_relative_path != material.project_relative_path
            or record.native_source_project_identity_sha256 != material.project_identity_sha256
            or record.classifier_version != material.classifier_version
        ):
            raise BatchTrainingError("pending record differs from current native fit")
    import json
    from .cross_validation import QuPathGroupedCVConfiguration, compact_selection_summary

    summaries = {}
    for plan in plan_set.plans:
        if (
            plan.request.model_configuration.get("configuration_schema")
            != "qupath-grouped-cross-validation/v1"
        ):
            continue
        search = QuPathGroupedCVConfiguration.from_mapping(plan.request.model_configuration)
        material = fits[plan.request.classifier_id][1].material_record
        if material.model_selection_json is None:
            raise BatchTrainingError("The grouped-CV fit has no retained selection report")
        import json

        summaries[plan.request.classifier_id] = compact_selection_summary(
            json.loads(material.model_selection_json),
            plan.split_subject_ids["training"],
            plan.request.required_classes,
            len(search.candidates),
            search.folds,
            plan.request.random_seed,
        )
    return _project_pending_training_review_summary(
        plan_set,
        pending_record_set,
        checkpoint_set_sha256=current_fit_set.checkpoint_set.checkpoint_set_sha256,
        model_selection_by_classifier=summaries,
        training_observations_by_classifier={
            key: json.loads(runtime.material_record.training_observations_json)
            for key, (_, runtime) in fits.items()
            if runtime.material_record.training_observations_json is not None
        },
    )


def _project_pending_training_review_summary(
    plan_set: TrainingPlanSet,
    pending_record_set: ClassifierRecordSet,
    *,
    checkpoint_set_sha256: str,
    model_selection_by_classifier: Mapping[str, object] | None = None,
    training_observations_by_classifier: Mapping[str, object] | None = None,
) -> Mapping[str, object]:
    """Summarize exact pending records and genuine evaluation provenance."""

    if type(plan_set) is not TrainingPlanSet or type(pending_record_set) is not ClassifierRecordSet:
        raise BatchTrainingError(
            "fit review requires exact plans, current fit, pending records and evaluations"
        )
    plans = {plan.request.classifier_id: plan for plan in plan_set.plans}
    records = {record.classifier_id: record for record in pending_record_set.classifier_records}
    if (
        set(records) != set(plans)
        or len(records) != len(pending_record_set.classifier_records)
        or pending_record_set.source_training_plan_set_sha256 != plan_set.training_plan_set_sha256
        or pending_record_set.classifier_kind is not plan_set.classifier_kind
        or pending_record_set.validation_domain_id != plan_set.validation_domain_id
    ):
        raise BatchTrainingError("fit review lacks exact plan/fit/pending-record coverage")
    if {output_id for output_id, _ in pending_record_set.output_inventory} != set(plans):
        raise BatchTrainingError("fit review output inventory differs from plans")
    checkpoint_set_sha256 = _sha256(
        checkpoint_set_sha256,
        "checkpoint_set_sha256",
    )
    from .cross_validation import QuPathGroupedCVConfiguration, validate_compact_selection_summary

    summaries = dict(model_selection_by_classifier or {})
    observations_by_classifier = dict(training_observations_by_classifier or {})
    if not set(observations_by_classifier).issubset(plans):
        raise BatchTrainingError("Fitting counts name an unrelated classifier")
    expected_search = {
        key
        for key, plan in plans.items()
        if plan.request.model_configuration.get("configuration_schema")
        == "qupath-grouped-cross-validation/v1"
    }
    if set(summaries) != expected_search:
        raise BatchTrainingError(
            "The current search review must retain exactly its model selection summaries"
        )
    visible_rows = []
    freeze_digests = {record.freeze_review_sha256 for record in records.values()}
    if len(freeze_digests) != 1:
        raise BatchTrainingError("classifier reviews must move through each phase together")
    phase = "freeze_settings" if None in freeze_digests else "final_results"
    evaluation_evidence = []
    for classifier_id in sorted(plans):
        plan = plans[classifier_id]
        record = records[classifier_id]
        if record.review_decision != "pending":
            raise BatchTrainingError("fit review requires pending classifier records")
        if (
            record.training_plan_id != plan.training_plan_id
            or record.evaluation_intent is not plan.request.evaluation_intent
        ):
            raise BatchTrainingError("pending record differs from current plan")
        if record.owner_payload is not None:
            _validate_record_owner_plan(
                plan,
                record.owner_payload,
                classifier_version=record.classifier_version,
                runtime_versions=record.runtime_versions,
            )
        _validate_record_metrics(
            plan, record.metrics, frozen=record.freeze_review_sha256 is not None
        )
        evaluation_evidence.extend(
            {"classifier_id": classifier_id, **dict(evidence)}
            for evidence in record.evaluation_evidence
        )
        counts = {
            split: len(plan.split_subject_ids[split])
            for split in ("training", "validation", "test")
        }
        patient_count = counts["training"]
        native_byte_count = record.native_artifact_byte_count
        if patient_count <= 0 or type(native_byte_count) is not int or native_byte_count <= 0:
            raise BatchTrainingError("fit review requires positive patient and native byte counts")
        split_metrics = []
        for metric in record.metrics:
            if set(metric.pooled) != {"accuracy", "macro_precision", "macro_recall", "macro_f1"}:
                raise BatchTrainingError("fit review requires exact four pooled scores")
            split_metrics.append(
                {
                    "split": metric.split,
                    "patient_count": counts[metric.split],
                    **{
                        name: metric.pooled[name]
                        for name in ("accuracy", "macro_precision", "macro_recall", "macro_f1")
                    },
                }
            )
        selected_config = plan.request.model_configuration
        selection = None
        if classifier_id in summaries:
            search = QuPathGroupedCVConfiguration.from_mapping(selected_config)
            selection = validate_compact_selection_summary(
                summaries[classifier_id], counts["training"]
            )
            if len(selection["candidate_scores"]) != len(search.candidates):
                raise BatchTrainingError(
                    "The retained candidate count differs from the current search"
                )
            selected_config = search.candidates[selection["selected_candidate_index"]].to_mapping()
        visible_rows.append(
            {
                "classifier_id": classifier_id,
                "evaluation_intent": plan.request.evaluation_intent.value,
                **{
                    f"{split}_patient_count": counts[split]
                    for split in ("training", "validation", "test")
                },
                "split_metrics": split_metrics,
                "estimator_settings": classifier_estimator_settings(selected_config),
                **({"training_observations": observations_by_classifier[classifier_id]}
                   if classifier_id in observations_by_classifier else {}),
                "adjusted_after_validation": bool(
                    plan_set.settings_revision
                    and plan_set.settings_revision.adjusted_after_validation
                ),
                **({"model_selection": selection} if expected_search else {}),
            }
        )
    unsigned = {
        "schema_version": "training-fit-review/v5" if expected_search else "training-fit-review/v4",
        "phase": phase,
        "pending_classifier_record_set_sha256": pending_record_set.classifier_record_set_sha256,
        "training_plan_set_sha256": plan_set.training_plan_set_sha256,
        "checkpoint_set_sha256": checkpoint_set_sha256,
        "classifiers": visible_rows,
        "evaluation_evidence": list(evaluation_evidence),
    }
    return validate_training_fit_review_context(
        {**unsigned, "fit_review_sha256": _digest(unsigned)}
    )


def _load_authorized_training_review(*, state_store, context):
    """Only the state-issued response selects a retained pending body."""
    try:
        authorized = state_store.load_authorized_interaction_response(context)
        checked = validate_authorized_training_interaction_response(
            authorized,
            expected_context=context,
        )
        request = state_store.load_interaction_request(
            checked.accepted_response["interaction_request_id"]
        )
    except (BatchStateError, BatchTrainingError) as exc:
        raise BatchTrainingError(
            "The response could not be confirmed. Reopen the classifier review."
        ) from exc
    review = validate_training_fit_review_context(request.resume_context)
    digest = review["fit_review_sha256"]
    if (
        checked.action != "accept"
        or checked.checkpoint_id != "training-fit-review-" + digest
        or request.checkpoint_id != checked.checkpoint_id
        or dict(checked.response) != {"fit_review_sha256": digest}
        or request.stage != context.adapter_spec.node_kind
        or request.allowed_actions != training_fit_review_actions(review)
        or request.interaction_reason is not InteractionReason.OTHER_EXPLICIT_REASON
    ):
        raise BatchTrainingError(
            "The saved classifier has changed. Review the current result before continuing."
        )
    return review


def _same_current_fit(
    left: CurrentNativeFitSetAuthority,
    right: CurrentNativeFitSetAuthority,
) -> bool:
    return (
        left.checkpoint_set == right.checkpoint_set
        and tuple(item.material_record for item in left.runtime_authorities)
        == tuple(item.material_record for item in right.runtime_authorities)
        and left.state_head_sha256 == right.state_head_sha256
        and left.active_plan_sha256 == right.active_plan_sha256
    )


def require_training_evaluation_phase(
    *,
    state_store,
    context,
    current_parent,
    current_fit_set,
    plan,
    evaluation_split,
) -> str | None:
    """Authenticate the selected review before exposing a held-out split."""
    if evaluation_split not in ("validation", "test"):
        raise BatchTrainingError("classifier evaluation split is unsupported")
    plan_set = current_parent.training_plan_set.plan_set
    if (
        type(current_fit_set) is not CurrentNativeFitSetAuthority
        or plan not in plan_set.plans
        or not plan.split_project_entry_ids[evaluation_split]
    ):
        raise BatchTrainingError("classifier evaluation lacks its current plan and fit")
    authorized = state_store.load_authorized_interaction_response(context)
    if evaluation_split == "validation":
        if authorized is not None:
            checked = validate_authorized_training_interaction_response(
                authorized,
                expected_context=context,
            )
            if checked.checkpoint_id.startswith("training-fit-review-"):
                raise BatchTrainingError(
                    "Classifier settings are already final. Validation cannot be repeated in this review."
                )
        return None
    review = _load_authorized_training_review(state_store=state_store, context=context)
    if (
        review["phase"] != "freeze_settings"
        or review["training_plan_set_sha256"] != plan_set.training_plan_set_sha256
        or review["checkpoint_set_sha256"] != current_fit_set.checkpoint_set.checkpoint_set_sha256
    ):
        raise BatchTrainingError("Confirm the current classifier settings before testing.")
    return review["fit_review_sha256"]


def _validate_evidence_join(
    *,
    plan,
    complete,
    held_out_evidence,
    runtime,
) -> None:
    _validate_training_fit_evidence_join(plan=plan, complete=complete, runtime=runtime)
    if type(complete) is MultiImageTrainingEvidence:
        if (
            type(held_out_evidence).__name__ != "_PixelHeldOutEvaluationEvidence"
            or held_out_evidence.entry_states
            != tuple(item for item in complete.entry_states if item.split != "training")
            or held_out_evidence.rows
            != tuple(item for item in complete.rows if item.split != "training")
        ):
            raise BatchTrainingError(
                "complete pixel evidence differs from held-out target evidence"
            )
    elif type(complete) is MultiImageSupervisedTrainingAggregate:
        if type(
            held_out_evidence
        ).__name__ != "_SupervisedHeldOutEvaluationEvidence" or held_out_evidence.entries != tuple(
            item for item in complete.entries if item.split != "training"
        ):
            raise BatchTrainingError(
                "complete supervised aggregate differs from held-out target evidence"
            )
    else:
        raise BatchTrainingError("complete training evidence type is invalid")


def _validate_training_fit_evidence_join(*, plan, complete, runtime) -> None:
    if complete.training_plan_id != plan.training_plan_id:
        raise BatchTrainingError("complete evidence differs from selected training plan")
    if type(complete) is MultiImageTrainingEvidence:
        fit = MultiImagePixelFitEvidence.create(
            plan=plan,
            entry_states=tuple(item for item in complete.entry_states if item.split == "training"),
            rows=tuple(item for item in complete.rows if item.split == "training"),
        )
    elif type(complete) is MultiImageSupervisedTrainingAggregate:
        fit = MultiImageSupervisedFitEvidence.create(
            plan=plan,
            entries=tuple(item for item in complete.entries if item.split == "training"),
        )
    else:
        raise BatchTrainingError("complete training evidence type is invalid")
    if fit.material_evidence_sha256 != runtime.material_record.training_material_evidence_sha256:
        raise BatchTrainingError("complete training evidence differs from native fit material")


def _output_id_by_classifier(plan_set):
    return {plan.request.classifier_id: plan.request.classifier_id for plan in plan_set.plans}



def _materialize_classifier_record_set(
    *,
    plan_set,
    candidates: tuple[tuple[str, Mapping[str, object]], ...],
    secondary_structure_specs: tuple[SecondaryStructureSpec, ...],
    native_fit_artifacts: Mapping[str, tuple[str, int]],
) -> ClassifierRecordSet:
    """Validate the complete prospective set before consuming any evaluation."""

    if type(candidates) is not tuple or not candidates:
        raise BatchTrainingError("classifier record acceptance requires one exact candidate tuple")
    prepared = []
    for output_id, arguments in candidates:
        if type(output_id) is not str or type(arguments) is not dict:
            raise BatchTrainingError("classifier record candidate is invalid")
        plan = arguments.get("plan")
        if type(plan) is not TrainingPlan or plan not in plan_set.plans:
            raise BatchTrainingError("classifier record candidate lacks its exact plan")
        acceptance = ClassifierRecord._prepare_record_data(**arguments)
        prepared.append((output_id, acceptance.record, acceptance.held_out_evaluations))
    prepared.sort(key=lambda item: item[1].classifier_id)
    if not isinstance(native_fit_artifacts, Mapping) or set(native_fit_artifacts) != {
        plan.request.classifier_id for plan in plan_set.plans
    }:
        raise BatchTrainingError("native fit artifact inventory differs from plans")
    for _output_id, record, _evaluation in prepared:
        expected = native_fit_artifacts[record.classifier_id]
        if (
            type(expected) is not tuple
            or len(expected) != 2
            or record.native_artifact_sha256 != expected[0]
            or record.native_artifact_byte_count != expected[1]
        ):
            raise BatchTrainingError("native artifact bytes differ from current fit")
    prospective = ClassifierRecordSet.create(
        plan_set,
        tuple((output_id, record) for output_id, record, _evaluation in prepared),
        secondary_structure_specs=secondary_structure_specs,
    )
    evaluated = tuple(
        (record, evaluation)
        for _output_id, record, evaluations in prepared
        for evaluation in evaluations
    )
    authenticated = (
        _consume_held_out_classifier_evaluations(
            tuple(evaluation for _record, evaluation in evaluated)
        )
        if evaluated
        else ()
    )
    if any(
        accepted_evaluation is not evaluation
        or tuple(metrics)
        != tuple(metric for metric in record.metrics if metric.split == evaluation.evaluation_split)
        for (record, evaluation), (accepted_evaluation, metrics) in zip(
            evaluated, authenticated, strict=True
        )
    ):
        raise BatchTrainingError("held-out classifier evaluation authentication changed identity")
    consumed = tuple(record for _output_id, record, _evaluation in prepared)
    if consumed != prospective.classifier_records:
        raise BatchTrainingError("consumed classifier records differ from the prospective set")
    return prospective


@dataclass(frozen=True, slots=True)
class PixelClassifierSampleFacts:
    """Resolved per-entry physical calibration facts."""

    project_entry_id: str
    pixel_scale_um: float

    def __post_init__(self) -> None:
        if not isinstance(self.project_entry_id, str) or not self.project_entry_id:
            raise BatchTrainingError("project_entry_id must be nonempty")
        if (
            type(self.pixel_scale_um) not in {int, float}
            or not math.isfinite(float(self.pixel_scale_um))
            or float(self.pixel_scale_um) <= 0
        ):
            raise BatchTrainingError("pixel_scale_um must be positive and finite")
        object.__setattr__(self, "pixel_scale_um", float(self.pixel_scale_um))


def _runtime_versions_for_fit(
    *,
    material: object,
    runtime_versions: tuple[tuple[str, str], ...],
    registered_script_bundle_sha256: str,
) -> tuple[tuple[str, str], ...]:
    """Join the native semantic version to the frame's canonical executable label.

    Preserve both recorded formats: the fit and owner payloads use 0.7.0,
    while runtime inventories and deployment observations use QuPath v0.7.0.
    """
    versions = _version_inventory(
        runtime_versions,
        "runtime_versions",
        nonempty=True,
        require_canonical=False,
    )
    registered = _sha256(
        registered_script_bundle_sha256,
        "registered_script_bundle_sha256",
    )
    by_name = dict(versions)
    if by_name.get("qupath") != _qupath_executable_version_label(
        getattr(material, "qupath_version", None)
    ):
        raise BatchTrainingError("runtime QuPath version differs from current fit")
    if by_name.get("script_bundle_sha256") != registered:
        raise BatchTrainingError(
            "runtime script-bundle identity differs from the registered bundle"
        )
    return versions


def _fit_for_plan(
    *,
    plan: TrainingPlan,
    current_fit_set: CurrentNativeFitSetAuthority,
):
    if type(plan) is not TrainingPlan:
        raise BatchTrainingError("payload construction requires one exact plan")
    if type(current_fit_set) is not CurrentNativeFitSetAuthority:
        raise BatchTrainingError("payload construction requires one exact current fit")
    try:
        entry, runtime = _fit_material_by_classifier(current_fit_set)[plan.request.classifier_id]
    except KeyError as exc:
        raise BatchTrainingError("current fit lacks the selected classifier") from exc
    if entry.fit_plan_sha256 != runtime.material_record.fit_plan_sha256:
        raise BatchTrainingError("current fit entry differs from runtime material")
    return entry, runtime


def _pixel_output_class_facts(
    *,
    plan: TrainingPlan,
    secondary_structure_specs: tuple[SecondaryStructureSpec, ...],
) -> dict[str, object]:
    """Translate configured output facts without inferring biological labels."""

    if plan.request.classifier_kind is ClassifierKind.PRIMARY_COMPARTMENT:
        from .primary_policy import primary_ignore_policy

        # Policy validation binds this ordered inventory to the actual model
        # output labels, including generated boundaries without adding references.
        primary_ignore_policy(plan.request.primary_settings, plan.request.model_configuration)
        return {"output_classes": list(plan.request.primary_settings.primary_classifier.output_classes)}
    if type(secondary_structure_specs) is not tuple or not all(
        type(item) is SecondaryStructureSpec for item in secondary_structure_specs
    ):
        raise BatchTrainingError("secondary pixel payload requires exact structure specs")
    from mif_pipeline.pipeline_core.config import secondary_classifier_groups
    matches = secondary_classifier_groups(secondary_structure_specs).get(plan.request.classifier_id)
    if not matches:
        raise BatchTrainingError("secondary pixel payload requires its exact structure owners")
    return {"output_classes": list(plan.request.required_classes)}



def _classifier_definition_evidence_rows(
    rows: tuple[TrainingEvidenceSnapshot, ...],
) -> tuple[TrainingEvidenceSnapshot, ...]:
    """Project aggregate rows into the saved-classifier canonical order."""

    return tuple(
        sorted(
            rows,
            key=lambda item: (
                _SPLIT_RANK[item.split],
                item.evidence_sha256,
            ),
        )
    )


def _pixel_summary_metrics(
    plan: TrainingPlan,
    held_out_metrics: tuple[ClassifierEvaluationMetrics, ...],
) -> dict[str, float]:
    summary_split = "validation" if plan.split_project_entry_ids["validation"] else "test"
    summary_metrics = tuple(item for item in held_out_metrics if item.split == summary_split)
    if len(summary_metrics) != 1:
        raise BatchTrainingError(f"pixel payload requires one {summary_split} metric projection")
    return dict(summary_metrics[0].pooled)


def build_pixel_classifier_record_finalization_input(
    *,
    plan: TrainingPlan,
    complete_training_evidence: MultiImageTrainingEvidence,
    current_fit_set: CurrentNativeFitSetAuthority,
    held_out_target: HeldOutEvaluationTargetAuthority | None,
    held_out_evaluation: HeldOutClassifierEvaluation | None,
    retained_record: ClassifierRecord | None,
    freeze_review_sha256: str | None,
    sample_facts: tuple[PixelClassifierSampleFacts, ...],
    pixel_entry_channel_features: tuple[QuPathPixelEntryChannelFeatures, ...],
    pixel_scale_tolerance_um: float,
    registered_script_bundle_sha256: str,
    runtime_versions: tuple[tuple[str, str], ...],
    extension_versions: tuple[tuple[str, str], ...] = (),
    model_versions: tuple[tuple[str, str], ...] = (),
    secondary_structure_specs: tuple[SecondaryStructureSpec, ...] = (),
) -> ClassifierRecordFinalizationInput:
    """Translate exact pixel owner facts into one existing finalization input."""

    if type(complete_training_evidence) is not MultiImageTrainingEvidence:
        raise BatchTrainingError("pixel payload requires complete pixel evidence")
    if complete_training_evidence.training_plan_id != plan.training_plan_id:
        raise BatchTrainingError("pixel evidence differs from the selected plan")
    # Entry facts describe physical project entries, while one exploratory
    # single-image plan may assign more than one split role to the same entry.
    entry_ids = tuple(sorted({item.project_entry_id for item in plan.request.entries}))
    facts = tuple(sample_facts)
    if (
        type(sample_facts) is not tuple
        or not all(type(item) is PixelClassifierSampleFacts for item in facts)
        or tuple(item.project_entry_id for item in facts) != entry_ids
    ):
        raise BatchTrainingError("pixel sample facts require exact canonical entry coverage")
    scales = {item.pixel_scale_um for item in facts}
    if len(scales) != 1:
        raise BatchTrainingError("pixel calibration facts are ambiguous across entries")
    channels = project_pixel_channel_features(
        training_entry_ids=entry_ids,
        entry_features=pixel_entry_channel_features,
    )
    from .qupath_feature_settings import configured_image_features
    feature_signature = {
        "feature_policy": "pixel-channels/v1",
        "image_features": configured_image_features(plan.request.model_configuration).to_mapping(),
        "ordered_channel_ids": [item.canonical_channel_id for item in channels.ordered_channels],
    }
    entry, runtime = _fit_for_plan(plan=plan, current_fit_set=current_fit_set)
    versions = _runtime_versions_for_fit(
        material=runtime.material_record,
        runtime_versions=runtime_versions,
        registered_script_bundle_sha256=registered_script_bundle_sha256,
    )
    training_only = plan.request.evaluation_intent is EvaluationIntent.TRAINING_ONLY
    _validate_training_fit_evidence_join(
        plan=plan, complete=complete_training_evidence, runtime=runtime
    )
    common = dict(
        complete_training_evidence=complete_training_evidence,
        held_out_target=held_out_target,
        held_out_evaluation=held_out_evaluation,
        retained_record=retained_record,
        freeze_review_sha256=freeze_review_sha256,
        feature_requirements=feature_signature,
        runtime_versions=versions,
        extension_versions=extension_versions,
        model_versions=model_versions,
    )
    if training_only:
        if held_out_target is not None or held_out_evaluation is not None:
            raise BatchTrainingError("training-only pixel payload cannot carry held-out facts")
        _require_training_only_evidence(complete_training_evidence)
    if freeze_review_sha256 is None:
        return ClassifierRecordFinalizationInput(owner_payload=None, **common)
    if type(retained_record) is not ClassifierRecord:
        raise BatchTrainingError("finalized pixel settings require the reviewed record")
    all_metrics = retained_record.metrics + (
        () if held_out_evaluation is None else held_out_evaluation.metrics
    )
    _validate_record_metrics(plan, all_metrics, frozen=True)
    metrics = {} if training_only else _pixel_summary_metrics(plan, all_metrics)
    runtime_signature = {
        "qupath_version": runtime.material_record.qupath_version,
        "script_bundle_sha256": registered_script_bundle_sha256,
    }
    rows = complete_training_evidence.rows
    definition_rows = _classifier_definition_evidence_rows(rows)
    mapping: dict[str, object] = {
        "classifier_id": plan.request.classifier_id,
        "version": runtime.material_record.classifier_version,
        "kind": (
            "secondary"
            if plan.request.classifier_kind is ClassifierKind.SECONDARY_STRUCTURE
            else "primary"
        ),
        "channel_roles": dict((plan.request.channel_requirements or {})["channel_roles"]),
        "domain_id": plan.request.validation_domain_id,
        "pixel_scale_um": next(iter(scales)),
        "pixel_scale_tolerance_um": pixel_scale_tolerance_um,
        "evidence_snapshots": [item.to_mapping() for item in definition_rows],
        "project_entry_ids": entry_ids,
        "evidence_snapshot_set_sha256": _digest([item.to_mapping() for item in definition_rows]),
        "runtime_signature": runtime_signature,
        "runtime_signature_sha256": _digest(runtime_signature),
        "feature_signature": feature_signature,
        "feature_signature_sha256": _digest(feature_signature),
        "metrics": metrics,
        "validation_status": "not_evaluated" if training_only else "validated",
        "training_defaults": _export_json(plan.request.model_configuration),
    }
    for split in ("training", "validation", "test"):
        split_rows = tuple(item for item in rows if item.split == split)
        mapping[f"{split}_domain_ids"] = sorted({item.analysis_domain_id for item in split_rows})
        mapping[f"{split}_evidence_sha256s"] = sorted(item.evidence_sha256 for item in split_rows)
    mapping.update(
        _pixel_output_class_facts(
            plan=plan,
            secondary_structure_specs=secondary_structure_specs,
        )
    )
    try:
        owner = PixelClassifierOwnerPayload.create(ClassifierDefinition.from_mapping(mapping))
    except Exception as exc:
        raise BatchTrainingError("pixel owner payload facts are invalid") from exc
    if held_out_target is not None and (
        held_out_target.checkpoint_entry_sha256 != entry.checkpoint_entry_sha256
        or held_out_evaluation.checkpoint_entry_sha256 != entry.checkpoint_entry_sha256
    ):
        raise BatchTrainingError("pixel held-out facts differ from the current fit")
    return ClassifierRecordFinalizationInput(owner_payload=owner, **common)


def build_supervised_classifier_record_finalization_input(
    *,
    plan: TrainingPlan,
    complete_training_evidence: MultiImageSupervisedTrainingAggregate,
    current_fit_set: CurrentNativeFitSetAuthority,
    held_out_target: HeldOutEvaluationTargetAuthority | None,
    held_out_evaluation: HeldOutClassifierEvaluation | None,
    retained_record: ClassifierRecord | None,
    freeze_review_sha256: str | None,
    registered_script_bundle_sha256: str,
    runtime_versions: tuple[tuple[str, str], ...],
    extension_versions: tuple[tuple[str, str], ...] = (),
    model_versions: tuple[tuple[str, str], ...] = (),
) -> ClassifierRecordFinalizationInput:
    """Translate an accepted supervised aggregate into its owner payload."""

    if type(complete_training_evidence) is not MultiImageSupervisedTrainingAggregate:
        raise BatchTrainingError("supervised payload requires the complete accepted aggregate")
    if complete_training_evidence.training_plan_id != plan.training_plan_id:
        raise BatchTrainingError("supervised aggregate differs from the selected plan")
    entry, runtime = _fit_for_plan(plan=plan, current_fit_set=current_fit_set)
    versions = _runtime_versions_for_fit(
        material=runtime.material_record,
        runtime_versions=runtime_versions,
        registered_script_bundle_sha256=registered_script_bundle_sha256,
    )
    from .supervised_features import supervised_snapshot_source
    source_fields = supervised_snapshot_source(plan.request.model_configuration, [item.to_mapping() for item in complete_training_evidence.feature_schema])
    aggregate = complete_training_evidence
    training_only = plan.request.evaluation_intent is EvaluationIntent.TRAINING_ONLY
    _validate_training_fit_evidence_join(plan=plan, complete=aggregate, runtime=runtime)
    common = dict(
        complete_training_evidence=aggregate,
        held_out_target=held_out_target,
        held_out_evaluation=held_out_evaluation,
        retained_record=retained_record,
        freeze_review_sha256=freeze_review_sha256,
        feature_requirements={
            **source_fields,
            "feature_schema": [item.to_mapping() for item in aggregate.feature_schema]
        },
        runtime_versions=versions,
        extension_versions=extension_versions,
        model_versions=model_versions,
    )
    if training_only:
        if held_out_target is not None or held_out_evaluation is not None:
            raise BatchTrainingError("training-only supervised payload cannot carry held-out facts")
        _require_training_only_evidence(aggregate)
    if freeze_review_sha256 is None:
        return ClassifierRecordFinalizationInput(owner_payload=None, **common)
    owner = SupervisedClassifierOwnerPayload.create(
        classifier_id=plan.request.classifier_id,
        classifier_sha256=runtime.material_record.native_classifier_sha256,
        qupath_version=runtime.material_record.qupath_version,
        class_ids=aggregate.class_ids,
        feature_schema=aggregate.feature_schema,
        validation_domain_id=plan.request.validation_domain_id,
        training_cells=aggregate.training_cells,
        validation_cells=aggregate.validation_cells,
        test_cells=aggregate.test_cells,
        training_regions=aggregate.training_regions,
        validation_regions=aggregate.validation_regions,
        test_regions=aggregate.test_regions,
    )
    if held_out_target is not None and (
        held_out_target.checkpoint_entry_sha256 != entry.checkpoint_entry_sha256
        or held_out_evaluation.checkpoint_entry_sha256 != entry.checkpoint_entry_sha256
    ):
        raise BatchTrainingError("supervised held-out facts differ from the current fit")
    return ClassifierRecordFinalizationInput(owner_payload=owner, **common)


def _reopen_finalization_correspondence(
    session,
    current_fit_set,
    complete_training_evidence,
    secondary_structure_specs,
):
    if type(session) is not QuPathTrainingMutationSession:
        raise BatchTrainingError("record publication requires an exact session")
    if type(current_fit_set) is not CurrentNativeFitSetAuthority:
        raise BatchTrainingError("record publication requires an exact current fit")
    if type(complete_training_evidence) is not tuple or not all(
        type(item) in {MultiImageTrainingEvidence, MultiImageSupervisedTrainingAggregate}
        for item in complete_training_evidence
    ):
        raise BatchTrainingError("complete evidence must be an exact aggregate tuple")
    if type(secondary_structure_specs) is not tuple or not all(
        type(item) is SecondaryStructureSpec for item in secondary_structure_specs
    ):
        raise BatchTrainingError("secondary structure specs must be an exact tuple")
    state_store, context = session._state_store, session._context
    parent = open_current_training_parent_execution(
        context,
        state_store=state_store,
        plan_set_store=session._plan_set_store,
    )
    reopened_fit = state_store.reopen_current_native_fit_set(
        context,
        current_parent=parent,
        engine_registry=session._engine_registry,
    )
    if reopened_fit is None or not _same_current_fit(reopened_fit, current_fit_set):
        raise BatchTrainingError("finalization current fit changed after reopen")
    plan_set = parent.training_plan_set.plan_set
    plans = {item.training_plan_id: item for item in plan_set.plans}
    fits = _fit_material_by_classifier(reopened_fit)
    complete_by_plan = {item.training_plan_id: item for item in complete_training_evidence}
    outputs = _output_id_by_classifier(plan_set)
    if (
        set(fits) != {item.request.classifier_id for item in plans.values()}
        or len(complete_by_plan) != len(complete_training_evidence)
        or set(complete_by_plan) != set(plans)
        or set(outputs) != set(fits)
    ):
        raise BatchTrainingError(
            "record finalization lacks exact plan/fit/evidence/output coverage"
        )
    head = state_store.load_head()
    try:
        designation = session._project_leases.discover_managed(Path(head.project_path))
    except Exception as exc:
        raise BatchTrainingError("current native source project cannot be revalidated") from exc
    for plan_id, plan in plans.items():
        runtime = fits[plan.request.classifier_id][1]
        material = runtime.material_record
        _validate_training_fit_evidence_join(
            plan=plan,
            complete=complete_by_plan[plan_id],
            runtime=runtime,
        )
        designation_head = designation.authority_head
        fit_head_is_current = (
            designation_head.authority_head_sha256
            == runtime.successor_project_authority_head_sha256
        )
        if not fit_head_is_current:
            predecessor_sha256 = designation_head.predecessor_authority_head_sha256
            try:
                predecessor = session._project_leases._load_head_version(
                    designation.identity.project_identity_sha256,
                    predecessor_sha256,
                )
            except Exception as exc:
                raise BatchTrainingError(
                    "native fit source project identity is no longer current"
                ) from exc
            fit_head_is_current = (
                predecessor_sha256 == runtime.successor_project_authority_head_sha256
                and session._project_leases._is_exact_locator_relocation_edge(
                    designation_head,
                    predecessor,
                )
            )
        if (
            designation.identity.project_identity_sha256 != material.project_identity_sha256
            or designation_head.authority_head_sha256 != head.project_authority_head_sha256
            or designation_head.qpproj_snapshot_sha256 != head.project_qpproj_snapshot_sha256
            or not fit_head_is_current
        ):
            raise BatchTrainingError("native fit source project identity is no longer current")
    return parent, reopened_fit, plans, fits, complete_by_plan, outputs, designation


def materialize_qupath_training_review(
    session: QuPathTrainingMutationSession,
    *,
    current_fit_set: CurrentNativeFitSetAuthority,
    finalization_inputs: tuple[ClassifierRecordFinalizationInput, ...],
    record_set_store: ClassifierRecordSetStore,
    secondary_structure_specs: tuple[SecondaryStructureSpec, ...] = (),
) -> ScheduledNodeResult:
    """Consume genuine evaluations once and pause on their retained pending set."""
    _require_finalizable_training_session(session)
    if type(finalization_inputs) is not tuple or not all(
        type(item) is ClassifierRecordFinalizationInput for item in finalization_inputs
    ):
        raise BatchTrainingError("finalization inputs must be one exact typed tuple")
    if type(record_set_store) is not ClassifierRecordSetStore:
        raise BatchTrainingError("record publication requires its exact owner store")
    if not finalization_inputs:
        raise BatchTrainingError("classifier finalization input coverage is empty")
    freeze_digests = {item.freeze_review_sha256 for item in finalization_inputs}
    if len(freeze_digests) != 1:
        raise BatchTrainingError("classifier settings must be finalized together")
    frozen = None not in freeze_digests
    authorized = session._state_store.load_authorized_interaction_response(session._context)
    if authorized is not None:
        checked = validate_authorized_training_interaction_response(
            authorized,
            expected_context=session._context,
        )
        if not frozen and checked.checkpoint_id.startswith("training-fit-review-"):
            raise BatchTrainingError(
                "Classifier settings are already confirmed. Continue with the saved result."
            )
    if frozen:
        review, retained, _ = _reopen_reviewed_training_records(
            session,
            current_fit_set=current_fit_set,
            complete_training_evidence=tuple(
                item.complete_training_evidence for item in finalization_inputs
            ),
            record_set_store=record_set_store,
            secondary_structure_specs=secondary_structure_specs,
        )
        if review["phase"] != "freeze_settings" or freeze_digests != {review["fit_review_sha256"]}:
            raise BatchTrainingError("classifier settings require the current confirmation")
        retained_by_plan = {item.training_plan_id: item for item in retained.classifier_records}
        if any(
            item.retained_record
            != retained_by_plan.get(item.complete_training_evidence.training_plan_id)
            for item in finalization_inputs
        ):
            raise BatchTrainingError(
                "classifier finalization differs from the accepted pending records"
            )
    session = _claim_finalizable_training_session(session, current_fit_set)
    parent, reopened_fit, plans, fits, _complete, output_by_classifier, designation = (
        _reopen_finalization_correspondence(
            session,
            current_fit_set,
            tuple(item.complete_training_evidence for item in finalization_inputs),
            secondary_structure_specs,
        )
    )
    context = session._context
    plan_set = parent.training_plan_set.plan_set
    inputs = {
        item.complete_training_evidence.training_plan_id: item for item in finalization_inputs
    }
    record_candidates = []
    for training_plan_id in sorted(plans):
        plan = plans[training_plan_id]
        item = inputs[training_plan_id]
        entry, runtime = fits[plan.request.classifier_id]
        training_only = plan.request.evaluation_intent is EvaluationIntent.TRAINING_ONLY
        material = runtime.material_record
        if training_only:
            _require_training_only_evidence(item.complete_training_evidence)
        evaluation = item.held_out_evaluation
        expected_split = "test" if frozen else "validation"
        if bool(plan.split_project_entry_ids[expected_split]) != (evaluation is not None):
            raise BatchTrainingError("classifier evaluation differs from the current review phase")
        if evaluation is not None:
            reopened_plan, held_out_evidence, target_entry, target_runtime = (
                _reopen_held_out_evaluation_target(item.held_out_target)
            )
            if (
                reopened_plan != plan
                or target_entry != entry
                or target_runtime.material_record != runtime.material_record
            ):
                raise BatchTrainingError("held-out target differs from selected current fit")
            if (
                evaluation.evaluation_split != expected_split
                or evaluation.freeze_review_sha256 != item.freeze_review_sha256
                or evaluation.training_plan_id != plan.training_plan_id
                or evaluation.classifier_id != entry.classifier_id
                or evaluation.classifier_kind is not plan.request.classifier_kind
                or evaluation.validation_domain_id != parent.validation_domain_id
                or evaluation.fit_plan_sha256 != entry.fit_plan_sha256
                or evaluation.fit_execution_sha256 != entry.fit_execution_sha256
                or evaluation.checkpoint_entry_sha256 != entry.checkpoint_entry_sha256
                or evaluation.fit_material_sha256 != material.fit_material_sha256
                or evaluation.native_classifier_sha256 != material.native_classifier_sha256
            ):
                raise BatchTrainingError("held-out evaluation differs from target and current fit")
            _validate_evidence_join(
                plan=plan,
                complete=item.complete_training_evidence,
                held_out_evidence=held_out_evidence,
                runtime=runtime,
            )
        owner_payload = item.owner_payload
        arguments = {
            "plan": plan,
            "plan_set": plan_set,
            "classifier_version": material.classifier_version,
            "artifact_kind": ClassifierArtifactKind.QUPATH_NATIVE,
            "native_artifact_project_relative_path": (material.project_relative_path),
            "native_source_project": designation,
            "python_model_authority": None,
            "python_model_identity_contract": None,
            "owner_payload": owner_payload,
            "feature_requirements": item.feature_requirements,
            "retained_record": item.retained_record,
            "freeze_review_sha256": item.freeze_review_sha256,
            "held_out_evaluations": () if evaluation is None else (evaluation,),
            "complete_training_evidence": item.complete_training_evidence,
            "runtime_versions": item.runtime_versions,
            "extension_versions": item.extension_versions,
            "model_versions": item.model_versions,
            "fit_plan_sha256": entry.fit_plan_sha256,
            "fit_execution_sha256": entry.fit_execution_sha256,
            "checkpoint_entry_sha256": entry.checkpoint_entry_sha256,
            "fit_material_sha256": material.fit_material_sha256,
            "training_material_evidence_sha256": (material.training_material_evidence_sha256),
            "review_decision": "pending",
        }
        record_candidates.append((output_by_classifier[entry.classifier_id], arguments))

    record_set = _materialize_classifier_record_set(
        plan_set=plan_set,
        candidates=tuple(record_candidates),
        secondary_structure_specs=secondary_structure_specs,
        native_fit_artifacts={
            classifier_id: (
                runtime.material_record.native_classifier_sha256,
                runtime.material_record.native_classifier_byte_count,
            )
            for classifier_id, (_entry, runtime) in fits.items()
        },
    )

    if frozen and not any(plan.split_project_entry_ids["test"] for plan in plan_set.plans):
        accepted = ClassifierRecordSet.create(
            plan_set,
            tuple(
                (
                    output_by_classifier[record.classifier_id],
                    _promote_pending_classifier_record(record),
                )
                for record in record_set.classifier_records
            ),
            secondary_structure_specs=secondary_structure_specs,
        )
        return _publish_completed_classifier_record_set(session, parent, accepted, record_set_store)
    review = build_training_fit_review_context(plan_set, reopened_fit, record_set)
    record_set_store.publish(record_set)
    return session._terminal_committer.commit_raw(
        (
            BatchRawNodeOutcome(
                context=context,
                stage_result=StageResult.needs_interaction(
                    stage=context.adapter_spec.node_kind,
                    slide_id=plan_set.plans[0].request.entries[0].slide_id,
                    reason=InteractionReason.OTHER_EXPLICIT_REASON,
                    message=(
                        "Confirm classifier settings."
                        if not frozen
                        else "Review classifier test results."
                    ),
                    allowed_actions=training_fit_review_actions(review),
                    resume_context=review,
                    checkpoint_id="training-fit-review-" + review["fit_review_sha256"],
                ),
                execution_result=BatchNodeExecutionResult(
                    context.authorization.authorization_sha256, ()
                ),
                execution_exchange_sha256=None,
            ),
        )
    )[0]


def _promote_pending_classifier_record(record):
    if record.review_decision != "pending":
        raise BatchTrainingError("promotion requires pending classifier records")
    original = record.to_mapping()
    promoted = record.to_mapping()
    promoted["review_decision"] = "accepted"
    if (
        record.evaluation_intent is EvaluationIntent.TRAINING_ONLY
        and type(record.owner_payload) is PixelClassifierOwnerPayload
    ):
        definition = record.owner_payload.classifier_definition.to_mapping()
        definition["unattended_review_override"] = True
        promoted["owner_payload"] = PixelClassifierOwnerPayload.create(
            ClassifierDefinition.from_mapping(definition)
        ).to_mapping()
    promoted["classifier_record_sha256"] = _digest(
        {key: value for key, value in promoted.items() if key != "classifier_record_sha256"}
    )
    result = ClassifierRecord.from_mapping(promoted)
    checked = result.to_mapping()
    allowed = {"review_decision", "classifier_record_sha256", "owner_payload"}
    if any(original[key] != checked[key] for key in original.keys() - allowed):
        raise BatchTrainingError("promotion changed scientific record fields")
    before_owner, after_owner = original["owner_payload"], checked["owner_payload"]
    if before_owner != after_owner:
        if (
            record.evaluation_intent is not EvaluationIntent.TRAINING_ONLY
            or type(record.owner_payload) is not PixelClassifierOwnerPayload
        ):
            raise BatchTrainingError("promotion changed an evaluated owner")
        expected = dict(before_owner["classifier_definition"])
        expected["unattended_review_override"] = True
        if after_owner["classifier_definition"] != expected:
            raise BatchTrainingError("promotion changed scientific owner fields")
    return result


def _reopen_reviewed_training_records(
    session: QuPathTrainingMutationSession,
    *,
    current_fit_set: CurrentNativeFitSetAuthority,
    complete_training_evidence: tuple[
        MultiImageTrainingEvidence | MultiImageSupervisedTrainingAggregate, ...
    ],
    record_set_store: ClassifierRecordSetStore,
    secondary_structure_specs: tuple[SecondaryStructureSpec, ...] = (),
):
    """Reopen the response-selected records against the unchanged native fit."""
    if (
        type(session) is not QuPathTrainingMutationSession
        or type(record_set_store) is not ClassifierRecordSetStore
    ):
        raise BatchTrainingError("review promotion requires its exact session and store")
    review = _load_authorized_training_review(
        state_store=session._state_store, context=session._context
    )
    pending = record_set_store.reopen(review["pending_classifier_record_set_sha256"])
    parent, reopened_fit, plans, fits, complete, outputs, _designation = (
        _reopen_finalization_correspondence(
            session,
            current_fit_set,
            complete_training_evidence,
            secondary_structure_specs,
        )
    )
    plan_set = parent.training_plan_set.plan_set
    regenerated = _project_training_fit_review_context(
        plan_set,
        reopened_fit,
        pending,
    )
    if regenerated != review:
        raise BatchTrainingError("pending classifier summary differs from authenticated review")
    expected_inventory = tuple(
        sorted(
            (outputs[record.classifier_id], record.classifier_record_sha256)
            for record in pending.classifier_records
        )
    )
    if expected_inventory != pending.output_inventory:
        raise BatchTrainingError(
            "pending classifier output inventory differs from configured outputs"
        )
    for record in pending.classifier_records:
        plan = plans[record.training_plan_id]
        aggregate = complete[record.training_plan_id]
        if record.accepted_training_evidence_sha256 != aggregate.material_evidence_sha256:
            raise BatchTrainingError("pending classifier differs from complete current evidence")
        _validate_record_training_evidence(plan, record.owner_payload, aggregate)
    validate_training_review_freeze_lineage(
        state_store=session._state_store,
        producing_attempt_sha256=session._context.authorization.interaction_predecessor_terminal_record_sha256,
        review=review,
        pending_record_set=pending,
    )
    return review, pending, parent


def validate_training_review_freeze_lineage(
    *,
    state_store,
    producing_attempt_sha256,
    review,
    pending_record_set,
) -> None:
    """Follow the exact final-review producer to its accepted settings review."""
    if review["phase"] == "freeze_settings":
        if any(
            record.freeze_review_sha256 is not None
            for record in pending_record_set.classifier_records
        ):
            raise BatchTrainingError("pre-test review contains finalized classifier records")
        return
    producer = state_store.load_attempt(producing_attempt_sha256)
    if _export_json(producer.stage_result.resume_context) != _export_json(review):
        raise BatchTrainingError("classifier results differ from their saved review")
    authorization = state_store.load_authorization(producer.authorization_sha256)
    if authorization.node_id != producer.node_id or authorization.attempt_id != producer.attempt_id:
        raise BatchTrainingError("classifier review producer differs from its saved authorization")
    response = state_store.load_validated_interaction_response(authorization)
    if response is None:
        raise BatchTrainingError("classifier test results lack a settings confirmation")
    request = state_store.load_interaction_request(response["interaction_request_id"])
    freeze = validate_training_fit_review_context(request.resume_context)
    digest = freeze["fit_review_sha256"]
    if (
        response["action"] != "accept"
        or dict(response["response"]) != {"fit_review_sha256": digest}
        or request.checkpoint_id != "training-fit-review-" + digest
        or request.stage != producer.stage_result.stage
        or request.allowed_actions != training_fit_review_actions(freeze)
        or request.interaction_reason is not InteractionReason.OTHER_EXPLICIT_REASON
        or freeze["phase"] != "freeze_settings"
        or freeze["training_plan_set_sha256"] != review["training_plan_set_sha256"]
        or freeze["checkpoint_set_sha256"] != review["checkpoint_set_sha256"]
        or any(
            record.freeze_review_sha256 != digest
            for record in pending_record_set.classifier_records
        )
    ):
        raise BatchTrainingError("classifier test results differ from the confirmed settings")


def finalize_reviewed_qupath_training_session(
    session: QuPathTrainingMutationSession,
    *,
    current_fit_set: CurrentNativeFitSetAuthority,
    complete_training_evidence: tuple[
        MultiImageTrainingEvidence | MultiImageSupervisedTrainingAggregate, ...
    ],
    record_set_store: ClassifierRecordSetStore,
    secondary_structure_specs: tuple[SecondaryStructureSpec, ...] = (),
) -> ScheduledNodeResult:
    """Accept saved final results without fitting or evaluating again."""
    review, pending, parent = _reopen_reviewed_training_records(
        session,
        current_fit_set=current_fit_set,
        complete_training_evidence=complete_training_evidence,
        record_set_store=record_set_store,
        secondary_structure_specs=secondary_structure_specs,
    )
    if review["phase"] != "final_results":
        raise BatchTrainingError("confirm classifier settings before final acceptance")
    plan_set = parent.training_plan_set.plan_set
    outputs = _output_id_by_classifier(plan_set)
    accepted = ClassifierRecordSet.create(
        plan_set,
        tuple(
            (outputs[record.classifier_id], _promote_pending_classifier_record(record))
            for record in pending.classifier_records
        ),
        secondary_structure_specs=secondary_structure_specs,
    )
    session = _claim_finalizable_training_session(session, current_fit_set)
    return _publish_completed_classifier_record_set(session, parent, accepted, record_set_store)


def _publish_completed_classifier_record_set(session, parent, record_set, record_set_store):
    context = session._context
    plan_set = parent.training_plan_set.plan_set
    _require_accepted_classifier_record_set(record_set)
    record_set_store.publish(record_set)
    evidence_inventory = sorted(
        item.artifact_ref.artifact_ref_sha256 for item in parent.evidence_child_authorities
    )
    identity = {
        "classifier_kind": record_set.classifier_kind.value,
        "classifier_record_set_sha256": (record_set.classifier_record_set_sha256),
        "source_training_plan_set_sha256": (record_set.source_training_plan_set_sha256),
        "output_inventory": [list(item) for item in record_set.output_inventory],
        "evidence_child_inventory_sha256": _digest(evidence_inventory),
        "material_evidence_sha256": (
            _classifier_record_native_fit_inventory_sha256(
                [
                    _classifier_record_native_fit_projection(
                        training_plan_id=record.training_plan_id,
                        classifier_id=record.classifier_id,
                        training_material_evidence_sha256=(
                            record.training_material_evidence_sha256
                        ),
                        fit_plan_sha256=record.fit_plan_sha256,
                        fit_execution_sha256=record.fit_execution_sha256,
                        fit_material_sha256=record.fit_material_sha256,
                        classifier_version=record.classifier_version,
                        project_relative_path=(record.native_artifact_project_relative_path),
                        native_classifier_sha256=(record.native_artifact_sha256),
                        native_classifier_byte_count=(record.native_artifact_byte_count),
                        project_identity_sha256=(record.native_source_project_identity_sha256),
                    )
                    for record in record_set.classifier_records
                ]
            )
        ),
        "validation_domain_id": record_set.validation_domain_id,
    }
    contract = classifier_record_set_identity_contract()
    output = BatchArtifactRef(
        artifact_id="classifier-record-set-" + record_set.classifier_record_set_sha256[:48],
        artifact_kind="classifier_record_set",
        schema_name="mif-classifier-record-set",
        schema_version=1,
        origin=ArtifactOrigin.INTERNAL_NODE,
        producer_node_id=context.node_id,
        producer_attempt_id=context.authorization.attempt_id,
        producer_attempt_authorization_sha256=(context.authorization.authorization_sha256),
        external_declaration_sha256=None,
        external_validation_receipt_sha256=None,
        locator=None,
        sha256=None,
        byte_count=None,
        scope=ArtifactScope(
            parent.validation_domain_id,
            None,
            None,
            None,
            parent.analysis_domain_bindings,
        ),
        owning_identity=identity,
        identity_contract=contract,
        owning_identity_contract_sha256=contract.contract_sha256,
        producer_version=_PRODUCER_VERSION,
        runtime_versions={
            "mif_pipeline": MIF_PIPELINE_BATCH_RUNTIME_VERSION,
        },
        compatibility_signature_sha256=context.compatibility_signature_sha256,
        configuration_bindings=parent.configuration_bindings,
    )
    try:
        (validated_output,) = StageAdapterRegistry((context.adapter_spec,)).validate_outputs(
            context.adapter_spec,
            (output,),
            authorization=context.authorization,
        )
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchTrainingError(
            "classifier record-set output is not authorized by its context"
        ) from exc
    scheduled = session._terminal_committer.commit_raw(
        (
            BatchRawNodeOutcome(
                context=context,
                stage_result=StageResult.completed(
                    stage=context.adapter_spec.node_kind,
                    slide_id=plan_set.plans[0].request.entries[0].slide_id,
                ),
                execution_result=BatchNodeExecutionResult(
                    context.authorization.authorization_sha256,
                    (validated_output,),
                ),
                execution_exchange_sha256=None,
            ),
        )
    )
    return scheduled[0]


def _parent_input_refs(state_store: BatchStateStore, authority):
    head = state_store.load_head()
    snapshot = state_store._load_plan_snapshot(head.active_plan_sha256)
    node_id = authority.artifact_ref.producer_node_id
    edges = tuple(item for item in snapshot.edges if item.get("consumer_node_id") == node_id)
    refs = []
    for edge in edges:
        producer_node_id = edge.get("producer_node_id")
        if type(producer_node_id) is not str:
            raise BatchTrainingError("classifier record-set parent input has no internal producer")
        attempt_ids = head.attempt_ids_by_node.get(producer_node_id, ())
        if not attempt_ids:
            raise BatchTrainingError("classifier record-set parent input producer is absent")
        terminal = head.terminal_attempt_sha256_by_attempt_id[attempt_ids[-1]]
        attempt = state_store.load_attempt(terminal)
        matches = tuple(
            item
            for item in attempt.output_artifacts
            if item.artifact_id == edge.get("artifact_id")
            and item.artifact_kind == edge.get("artifact_kind")
            and item.producer_node_id == producer_node_id
        )
        if len(matches) != 1:
            raise BatchTrainingError(
                "classifier record-set parent input cannot be reopened exactly"
            )
        refs.append(matches[0])
    return tuple(refs), snapshot, head


def decode_current_committed_classifier_record_set(
    authority: BatchCommittedArtifactAuthority,
    *,
    store: ClassifierRecordSetStore,
    state_store: BatchStateStore,
) -> ClassifierRecordSet:
    """Decode only the current state-authenticated committed record set."""

    if type(authority) is not BatchCommittedArtifactAuthority:
        raise BatchTrainingError(
            "classifier record-set decoding requires committed state authority"
        )
    if type(store) is not ClassifierRecordSetStore:
        raise BatchTrainingError("classifier record-set decoding requires its exact owner store")
    if type(state_store) is not BatchStateStore:
        raise BatchTrainingError("classifier record-set decoding requires the exact state owner")
    try:
        validate_committed_artifact_authority_set(
            (authority,),
            expected_active_plan_sha256=authority.active_plan_sha256,
        )
        ref = validate_artifact_ref(authority.artifact_ref)
        current = state_store.authenticate_available_artifact(ref)
        if current != authority:
            raise BatchStateError("classifier record-set authority is stale for the current head")
    except (BatchArtifactError, BatchStateError, TypeError, ValueError) as exc:
        raise BatchTrainingError(
            "current classifier record-set authority is not authentic"
        ) from exc
    contract = classifier_record_set_identity_contract()
    if (
        ref.artifact_kind != "classifier_record_set"
        or (ref.schema_name, ref.schema_version) != ("mif-classifier-record-set", 1)
        or ref.identity_contract != contract
        or ref.owning_identity_contract_sha256 != contract.contract_sha256
        or ref.producer_version != _PRODUCER_VERSION
        or ref.origin is not ArtifactOrigin.INTERNAL_NODE
        or ref.locator is not None
        or ref.sha256 is not None
        or ref.byte_count is not None
        or dict(ref.runtime_versions) != {"mif_pipeline": MIF_PIPELINE_BATCH_RUNTIME_VERSION}
        or authority.execution_exchange_sha256 is not None
        or set(ref.owning_identity) != set(contract.required_fields)
    ):
        raise BatchTrainingError("classifier record-set ref lacks its exact seven-field contract")
    identity = ref.owning_identity
    record_set = store.reopen(identity["classifier_record_set_sha256"])
    _require_accepted_classifier_record_set(record_set)
    parent_refs, snapshot, head = _parent_input_refs(state_store, authority)
    plan_refs = tuple(item for item in parent_refs if item.artifact_kind == "training_plan_set")
    evidence_refs = tuple(item for item in parent_refs if item.artifact_kind != "training_plan_set")
    attempt = state_store.load_attempt(authority.terminal_record_sha256)
    authorization = state_store.load_authorization(attempt.authorization_sha256)
    if (
        len(plan_refs) != 1
        or tuple(sorted(item.artifact_ref_sha256 for item in parent_refs))
        != attempt.execution_context_binding.input_artifact_ref_sha256s
        or authorization.authorization_sha256 != attempt.authorization_sha256
        or authorization.admission_plan_sha256 not in head.plan_history_sha256s
    ):
        raise BatchTrainingError(
            "classifier record-set producer lineage cannot be reopened exactly"
        )
    expected_identity = {
        "classifier_kind": record_set.classifier_kind.value,
        "classifier_record_set_sha256": (record_set.classifier_record_set_sha256),
        "source_training_plan_set_sha256": (record_set.source_training_plan_set_sha256),
        "output_inventory": [list(item) for item in record_set.output_inventory],
        "evidence_child_inventory_sha256": _digest(
            sorted(item.artifact_ref_sha256 for item in evidence_refs)
        ),
        "material_evidence_sha256": (
            _classifier_record_native_fit_inventory_sha256(
                [
                    _classifier_record_native_fit_projection(
                        training_plan_id=item.training_plan_id,
                        classifier_id=item.classifier_id,
                        training_material_evidence_sha256=(item.training_material_evidence_sha256),
                        fit_plan_sha256=item.fit_plan_sha256,
                        fit_execution_sha256=item.fit_execution_sha256,
                        fit_material_sha256=item.fit_material_sha256,
                        classifier_version=item.classifier_version,
                        project_relative_path=(item.native_artifact_project_relative_path),
                        native_classifier_sha256=item.native_artifact_sha256,
                        native_classifier_byte_count=(item.native_artifact_byte_count),
                        project_identity_sha256=(item.native_source_project_identity_sha256),
                    )
                    for item in record_set.classifier_records
                ]
            )
        ),
        "validation_domain_id": record_set.validation_domain_id,
    }
    node = snapshot.node_by_id.get(ref.producer_node_id or "")
    if node is None:
        raise BatchTrainingError("classifier record-set producer node is absent")
    bindings = tuple(
        AnalysisDomainBinding.from_mapping(item) for item in node["analysis_domain_bindings"]
    )
    configurations = tuple(
        ArtifactConfigurationBinding.from_mapping(item) for item in node["configuration_bindings"]
    )
    expected_kind = _PARENT_KIND_BY_CLASSIFIER[record_set.classifier_kind]
    expected_spec = {item.node_kind: item for item in build_training_stage_adapters()}[
        expected_kind
    ]
    try:
        producer = state_store.authenticate_available_artifact_producer_spec(
            ref,
            expected_adapter_spec=expected_spec,
        )
    except (BatchStateError, KeyError, TypeError, ValueError) as exc:
        raise BatchTrainingError("classifier record-set producer spec is not authentic") from exc
    if (
        producer != current
        or authority.producer_node_kind not in TRAINING_PARENT_NODE_KINDS
        or authority.producer_node_kind != expected_kind
        or _canonical_bytes(_export_json(identity)) != _canonical_bytes(expected_identity)
        or plan_refs[0].owning_identity.get("training_plan_set_sha256")
        != record_set.source_training_plan_set_sha256
        or ref.artifact_id
        != "classifier-record-set-" + record_set.classifier_record_set_sha256[:48]
        or ref.scope.validation_domain_id != record_set.validation_domain_id
        or ref.scope.sample_id is not None
        or ref.scope.slide_id is not None
        or ref.scope.project_entry_id is not None
        or ref.scope.analysis_domain_bindings != bindings
        or ref.configuration_bindings != configurations
        or ref.compatibility_signature_sha256
        != attempt.execution_context_binding.compatibility_signature_sha256
    ):
        raise BatchTrainingError(
            "classifier record-set compact identity or producer binding differs"
        )
    return record_set


def decode_current_committed_classifier_record_plan_set(
    authority: BatchCommittedArtifactAuthority,
    *,
    record_store: ClassifierRecordSetStore,
    plan_store: object,
    state_store: BatchStateStore,
) -> tuple[ClassifierRecordSet, ValidatedTrainingPlanSetProjection]:
    """Reopen the exact accepted plan that authorized a current record set."""

    from .runtime import TrainingPlanSetStore, decode_current_committed_training_plan_set

    if type(plan_store) is not TrainingPlanSetStore:
        raise BatchTrainingError("classifier record plan lookup requires its exact plan store")
    record_set = decode_current_committed_classifier_record_set(
        authority,
        store=record_store,
        state_store=state_store,
    )
    parent_refs, _snapshot, _head = _parent_input_refs(state_store, authority)
    plan_refs = tuple(item for item in parent_refs if item.artifact_kind == "training_plan_set")
    if len(plan_refs) != 1:
        raise BatchTrainingError("classifier record lacks one exact accepted plan")
    try:
        plan_authority = state_store.authenticate_available_artifact(plan_refs[0])
        projection = decode_current_committed_training_plan_set(
            plan_authority,
            store=plan_store,
            state_store=state_store,
        )
    except (BatchStateError, BatchTrainingError, TypeError, ValueError) as exc:
        raise BatchTrainingError("classifier record accepted plan cannot be reopened") from exc
    plan_set = projection.plan_set
    plan_by_id = {item.request.classifier_id: item for item in plan_set.plans}
    if (
        plan_set.training_plan_set_sha256 != record_set.source_training_plan_set_sha256
        or plan_set.classifier_kind is not record_set.classifier_kind
        or plan_set.validation_domain_id != record_set.validation_domain_id
        or set(plan_by_id) != {item.classifier_id for item in record_set.classifier_records}
        or any(
            plan_by_id[item.classifier_id].training_plan_id != item.training_plan_id
            for item in record_set.classifier_records
        )
    ):
        raise BatchTrainingError("classifier record and accepted plan identities differ")
    return record_set, projection


__all__ = [
    "ClassifierRecordFinalizationInput",
    "ClassifierRecordSetStore",
    "provision_classifier_record_set_store_root",
    "PixelClassifierSampleFacts",
    "build_pixel_classifier_record_finalization_input",
    "build_supervised_classifier_record_finalization_input",
    "decode_current_committed_classifier_record_set",
    "decode_current_committed_classifier_record_plan_set",
    "materialize_qupath_training_review",
    "finalize_reviewed_qupath_training_session",
]
