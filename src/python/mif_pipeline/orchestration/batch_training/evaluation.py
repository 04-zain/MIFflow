"""Owner-authenticated composition for native held-out classifier evaluation.

The `HeldOutClassifierEvaluation` record carries `training_plan_id`, `evaluation_split`,
`freeze_review_sha256`, `classifier_id`, `classifier_kind` and its remaining declared
fields.

Training records keep subject roles, reference evidence, model settings and application
scope linked. Their validators and native adapters are called by the shared training service
before a fit, evaluation or accepted publication."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import secrets
from threading import RLock
from types import MappingProxyType
from typing import Literal
from weakref import ReferenceType, ref

from mif_pipeline.cell_analysis.supervised import (
    SupervisedDeploymentClassifierIdentity,
)
from mif_pipeline.cell_analysis.supervised_qc import evaluate_predictions
from mif_pipeline.compartment_analysis.classifier_qc import (
    ClassifierObservationIdentity,
    ClassifierPredictionObservation,
    reduce_classifier_confusion,
)
from mif_pipeline.qupath_runtime import held_out_evaluation
from mif_pipeline.qupath_runtime._held_out_evaluation_request import (
    _evaluation_digest,
    _expected_runtime_exchange_ref,
)
from mif_pipeline.qupath_runtime.catalogue import ProjectMutationLeaseProvider
from mif_pipeline.qupath_runtime.native_classifier_fit import (
    QuPathNativeClassifierFitAuthority,
    QuPathNativeClassifierReadOnlyEvidence,
)
from mif_pipeline.qupath_runtime.runner import QuPathRunner

from ..batch_training_checkpoint import BatchNativeFitCheckpointEntry
from ._shared import BatchTrainingError, ClassifierKind, _digest
from .evidence import (
    _PixelHeldOutEvaluationEvidence,
    _SupervisedHeldOutEvaluationEvidence,
)
from .engines import (
    QuPathNativeTrainingFitPlanProjection,
    TrainingFitPlanProjection,
    _parse_native_model_configuration,
)
from .native_projection import (
    QuPathPixelEntryChannelFeatures,
    project_pixel_channel_features,
)
from .planning import TrainingPlan
from .qupath_rtrees import (
    QuPathObjectRTreesConfiguration,
    QuPathPixelRTreesConfiguration,
)
from .qupath_svm import QuPathObjectSvmConfiguration, QuPathPixelSvmConfiguration
from .qupath_logistic_regression import QuPathObjectLogisticRegressionConfiguration
from .records import (
    ClassifierEvaluationMetrics, ScopedMetricSet,
    _PixelAnnotationObservation, _PixelConfusionEvidence,
)
from .runtime import (
    HeldOutEvaluationTargetAuthority,
    _consume_held_out_evaluation_target,
    _revalidate_consumed_held_out_evaluation_target,
)

_METRIC_NAMES = ("accuracy", "macro_precision", "macro_recall", "macro_f1")


# ==============================================================================
#                                   Evaluation
# ==============================================================================


def _evaluation_fields(value: "HeldOutClassifierEvaluation") -> dict[str, object]:
    return {
        "training_plan_id": value.training_plan_id,
        "evaluation_split": value.evaluation_split,
        "freeze_review_sha256": value.freeze_review_sha256,
        "classifier_id": value.classifier_id,
        "classifier_kind": value.classifier_kind.value,
        "validation_domain_id": value.validation_domain_id,
        "fit_plan_sha256": value.fit_plan_sha256,
        "fit_execution_sha256": value.fit_execution_sha256,
        "checkpoint_entry_sha256": value.checkpoint_entry_sha256,
        "fit_material_sha256": value.fit_material_sha256,
        "native_classifier_sha256": value.native_classifier_sha256,
        "request_identity_sha256": value.request_identity_sha256,
        "result_sha256": value.result_sha256,
        "metrics": [item.to_mapping() for item in value.metrics],
    }


def _evaluation_seal(value: "HeldOutClassifierEvaluation", nonce: str, identity: int) -> str:
    return _digest(
        {
            "fields": _evaluation_fields(value),
            "issuer_nonce": nonce,
            "object_identity": identity,
        }
    )


class _HeldOutClassifierEvaluationIssuer:
    def __init__(self) -> None:
        self._lock = RLock()
        self._live: dict[int, tuple[ReferenceType[HeldOutClassifierEvaluation], str, str]] = {}

    def issue(self, values: Mapping[str, object]) -> "HeldOutClassifierEvaluation":
        evaluation = object.__new__(HeldOutClassifierEvaluation)
        for name in HeldOutClassifierEvaluation._PUBLIC_FIELDS:
            object.__setattr__(evaluation, name, values[name])
        nonce = secrets.token_hex(32)
        identity = id(evaluation)
        object.__setattr__(evaluation, "_issuer_nonce", nonce)
        object.__setattr__(evaluation, "_object_identity", identity)
        object.__setattr__(evaluation, "_issuer", self)
        seal = _evaluation_seal(evaluation, nonce, identity)
        object.__setattr__(evaluation, "_seal_sha256", seal)

        def discard(expired: ReferenceType[HeldOutClassifierEvaluation]) -> None:
            with self._lock:
                current = self._live.get(identity)
                if current is not None and current[0] is expired:
                    self._live.pop(identity, None)

        with self._lock:
            self._live[identity] = (ref(evaluation, discard), nonce, seal)
        return evaluation

    def consume_many(self, values: tuple[object, ...]) -> tuple[
        tuple[
            "HeldOutClassifierEvaluation",
            tuple[ClassifierEvaluationMetrics, ...],
        ],
        ...,
    ]:
        if type(values) is not tuple or not values:
            raise BatchTrainingError("held-out classifier evaluation consumption requires a tuple")
        with self._lock:
            identities = tuple(id(value) for value in values)
            if len(set(identities)) != len(identities):
                raise BatchTrainingError("held-out classifier evaluation authentication failed")
            authenticated = []
            for value, identity in zip(values, identities, strict=True):
                issued = self._live.get(identity)
                if (
                    type(value) is not HeldOutClassifierEvaluation
                    or issued is None
                    or issued[0]() is not value
                    or value._issuer is not self
                    or value._object_identity != identity
                    or value._issuer_nonce != issued[1]
                    or value._seal_sha256 != issued[2]
                    or _evaluation_seal(value, issued[1], identity) != issued[2]
                ):
                    raise BatchTrainingError("held-out classifier evaluation authentication failed")
                authenticated.append((value, value.metrics))
            for identity in identities:
                self._live.pop(identity, None)
            return tuple(authenticated)

_EVALUATION_ISSUER = _HeldOutClassifierEvaluationIssuer()


@dataclass(frozen=True, slots=True, init=False, repr=False, weakref_slot=True)
class HeldOutClassifierEvaluation:
    """Opaque process-local metrics from one exact native held-out run."""

    training_plan_id: str
    evaluation_split: Literal["validation", "test"]
    freeze_review_sha256: str | None
    classifier_id: str
    classifier_kind: ClassifierKind
    validation_domain_id: str
    fit_plan_sha256: str
    fit_execution_sha256: str
    checkpoint_entry_sha256: str
    fit_material_sha256: str
    native_classifier_sha256: str
    request_identity_sha256: str
    result_sha256: str
    metrics: tuple[ClassifierEvaluationMetrics, ...]
    _issuer_nonce: str
    _object_identity: int
    _seal_sha256: str
    _issuer: object

    _PUBLIC_FIELDS = (
        "training_plan_id",
        "evaluation_split",
        "freeze_review_sha256",
        "classifier_id",
        "classifier_kind",
        "validation_domain_id",
        "fit_plan_sha256",
        "fit_execution_sha256",
        "checkpoint_entry_sha256",
        "fit_material_sha256",
        "native_classifier_sha256",
        "request_identity_sha256",
        "result_sha256",
        "metrics",
    )

    def __new__(cls, *args: object, **kwargs: object):
        del cls, args, kwargs
        raise TypeError("HeldOutClassifierEvaluation is issuer-only")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("HeldOutClassifierEvaluation cannot be subclassed")

    def __copy__(self) -> object:
        raise TypeError("HeldOutClassifierEvaluation is noncopyable")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("HeldOutClassifierEvaluation is noncopyable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("HeldOutClassifierEvaluation is nonserializable")


def _consume_held_out_classifier_evaluations(
    values: tuple[object, ...],
) -> tuple[
    tuple[HeldOutClassifierEvaluation, tuple[ClassifierEvaluationMetrics, ...]],
    ...,
]:
    """Atomically authenticate and consume a complete prospective record set."""

    return _EVALUATION_ISSUER.consume_many(values)


def _native_configuration(plan: TrainingPlan):
    from .cross_validation import base_configuration

    return base_configuration(
        _parse_native_model_configuration(
            plan.request.model_configuration, plan.request.classifier_kind
        )
    )


def _native_output_labels(
    *, plan: TrainingPlan, configuration: object, evidence: object
) -> list[dict[str, object]]:
    labels = configuration.output_labels.ordered_labels
    truth_path_classes: dict[str, str] = {}
    if type(evidence) is _PixelHeldOutEvaluationEvidence:
        for row in evidence.rows:
            previous = truth_path_classes.setdefault(row.class_id, row.path_class_id)
            if previous != row.path_class_id:
                raise BatchTrainingError(
                    "held-out pixel truth has ambiguous native PathClass identity"
                )
    result: list[dict[str, object]] = []
    for label in labels:
        ignored = (
            type(configuration) in (QuPathPixelRTreesConfiguration, QuPathPixelSvmConfiguration)
            and configuration.boundary_strategy == "classify"
            and label.qupath_label == configuration.boundary_class_id
        )
        if ignored:
            native_path_class_id = label.canonical_class_id
        elif type(configuration) in (
            QuPathObjectRTreesConfiguration, QuPathObjectSvmConfiguration,
            QuPathObjectLogisticRegressionConfiguration,
        ):
            native_path_class_id = label.canonical_class_id
        else:
            native_path_class_id = truth_path_classes.get(
                label.canonical_class_id, label.canonical_class_id
            )
        result.append(
            {
                "canonical_class_id": label.canonical_class_id,
                "qupath_label": label.qupath_label,
                "native_path_class_id": native_path_class_id,
                "ignored": ignored,
            }
        )
    ordinary = tuple(item["canonical_class_id"] for item in result if not item["ignored"])
    if set(ordinary) != set(plan.request.required_classes) or len(ordinary) != len(
        plan.request.required_classes
    ):
        raise BatchTrainingError("native output labels differ from declared classes")
    return result


def _pixel_request_entries(
    plan: TrainingPlan,
    evidence: _PixelHeldOutEvaluationEvidence,
    features: tuple[QuPathPixelEntryChannelFeatures, ...],
) -> tuple[list[dict[str, object]], dict[tuple[str, str, str], object]]:
    states = tuple(sorted(evidence.entry_states, key=lambda item: item.project_entry_id))
    entry_ids = tuple(item.project_entry_id for item in states)
    shared_features = project_pixel_channel_features(
        training_entry_ids=entry_ids, entry_features=features
    )
    fit_plan = TrainingFitPlanProjection.create(plan)
    native_fit_plan = QuPathNativeTrainingFitPlanProjection.create(
        fit_plan=fit_plan,
        pixel_entry_channel_features=tuple(
            QuPathPixelEntryChannelFeatures(
                project_entry_id=item.project_entry_id,
                channel_features=shared_features,
            )
            for item in fit_plan.training_entries
        ),
    )
    authenticated_features = native_fit_plan.pixel_channel_features
    if authenticated_features is None:
        raise BatchTrainingError("pixel evaluation lacks authenticated channels")
    rows_by_entry: dict[str, list[object]] = {entry_id: [] for entry_id in entry_ids}
    truth: dict[tuple[str, str, str], object] = {}
    subject_by_entry = {item.project_entry_id: item.subject_id for item in states}
    for row in evidence.rows:
        if row.split not in {"validation", "test"}:
            raise BatchTrainingError("training truth cannot enter held-out evaluation")
        if row.project_entry_id not in rows_by_entry:
            raise BatchTrainingError("held-out pixel row has no exact entry owner")
        identity = (
            row.project_entry_id,
            subject_by_entry[row.project_entry_id],
            row.annotation_uuid,
        )
        if identity in truth:
            raise BatchTrainingError("held-out pixel truth is ambiguous")
        truth[identity] = row
        rows_by_entry[row.project_entry_id].append(row)
    entries: list[dict[str, object]] = []
    for state in states:
        rows = tuple(
            sorted(
                rows_by_entry[state.project_entry_id],
                key=lambda item: item.annotation_uuid,
            )
        )
        if not rows or any(row.split != state.split for row in rows):
            raise BatchTrainingError("held-out pixel entry lacks exact truth coverage")
        entries.append(
            {
                "entry_kind": "pixel",
                "project_entry_id": state.project_entry_id,
                "subject_id": state.subject_id,
                "split": state.split,
                "image_identity_sha256": state.image_identity_sha256,
                "saved_state_sha256": state.post_binding_saved_state_sha256,
                "channel_features": authenticated_features.to_mapping(),
                "targets": [
                    {
                        "project_entry_id": row.project_entry_id,
                        "subject_id": state.subject_id,
                        "annotation_uuid": row.annotation_uuid,
                        "expected_class_id": row.class_id,
                        "path_class_id": row.path_class_id,
                        "normalized_roi_sha256": row.normalized_roi_sha256,
                        "material_parent_id": row.material_parent_id,
                        "material_parent_type": row.material_parent_type,
                        "material_parent_type_id": row.material_parent_type_id,
                        "material_parent_revision_sha256": row.material_parent_revision_sha256,
                        "analysis_domain_id": row.analysis_domain_id,
                        "analysis_domain_revision_sha256": row.analysis_domain_revision_sha256,
                        "domain_set_sha256": row.domain_set_sha256,
                        "segmentation_generation_id": row.segmentation_generation_id,
                        "measurement_generation_id": row.measurement_generation_id,
                        "owner_evidence_sha256": row.evidence_sha256,
                    }
                    for row in rows
                ],
            }
        )
    return entries, truth


def _json_copy(value: object) -> object:
    return json.loads(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    )


def _supervised_request_entries(
    plan: TrainingPlan,
    evidence: _SupervisedHeldOutEvaluationEvidence,
) -> tuple[
    list[dict[str, object]],
    dict[tuple[str, str], tuple[object, object, str]],
    tuple[object, ...],
]:
    entries = tuple(sorted(evidence.entries, key=lambda item: (item.project_entry_id, item.section_id)))
    if not entries:
        raise BatchTrainingError("supervised held-out evidence is empty")
    from .deferred_cell_evidence import (pending_marker_names, validate_deferred_evidence,
                                         project_deferred_evidence)
    from .supervised_features import supervised_feature_source
    source = supervised_feature_source(plan.request.model_configuration)
    feature_schema = entries[0].feature_schema
    if any(
        item.class_ids != plan.request.required_classes or item.feature_schema != feature_schema
        for item in entries
    ):
        raise BatchTrainingError("supervised held-out class or feature identity differs")
    bindings = {
        (item.project_entry_id, item.analysis_domain_id): item
        for item in plan.request.analysis_domain_bindings
    }
    owner_truth: dict[tuple[str, str], tuple[object, object, str]] = {}
    result: list[dict[str, object]] = []
    expected_roles = {(item.subject_id, item.split) for item in entries}
    labelled_roles: set[tuple[str, str]] = set()
    for snapshot in entries:
        if snapshot.deferred_feature_evidence is not None:
            validate_deferred_evidence(snapshot.deferred_feature_evidence,
                (cell.cell_id for cell in snapshot.eligible_cells), source=source, require_pending=True)
        elif pending_marker_names(source):
            raise BatchTrainingError("Pending KRONOS statistics require deferred cell evidence")
        if snapshot.split not in {"validation", "test"}:
            raise BatchTrainingError("training truth cannot enter held-out evaluation")
        holdout = tuple(item for item in snapshot.mapped_evidence if item.evidence_set == "holdout")
        if not holdout:
            # The authenticated section remains in plan coverage. Numerical
            # evaluation consumes only labelled cells, pooled by subject/split.
            continue
        labelled_roles.add((snapshot.subject_id, snapshot.split))
        truth_by_cell: dict[str, str] = {}
        for mapped in holdout:
            if mapped.cell_id in truth_by_cell:
                raise BatchTrainingError("supervised held-out truth is ambiguous")
            truth_by_cell[mapped.cell_id] = mapped.class_id
        cells_by_id = {item.cell_id: item for item in snapshot.eligible_cells}
        if not set(truth_by_cell).issubset(cells_by_id):
            raise BatchTrainingError("mapped holdout truth lacks a live cell")
        cells = tuple(cells_by_id[cell_id] for cell_id in sorted(truth_by_cell))
        domain_ids = {item.analysis_domain_id for item in cells}
        evaluation_cells: list[dict[str, object]] = []
        for cell in cells:
            exported = cell.to_export_mapping()
            cell_geometry = _json_copy(exported["cell_geometry"])
            nucleus_geometry = _json_copy(exported["nucleus_geometry"])
            measurement_values = (None if snapshot.deferred_feature_evidence is not None else [
                float(cell.marker_measurements[item.measurement_name]) for item in feature_schema
            ])
            identity = (snapshot.project_entry_id, cell.cell_id)
            if identity in owner_truth:
                raise BatchTrainingError("supervised held-out truth repeats an entry-scoped cell")
            owner_truth[identity] = (
                snapshot,
                cell,
                truth_by_cell[cell.cell_id],
            )
            evaluation_cells.append(
                {
                    "project_entry_id": snapshot.project_entry_id,
                    "cell_id": cell.cell_id,
                    "expected_class_id": truth_by_cell[cell.cell_id],
                    "cell_geometry": cell_geometry,
                    "cell_geometry_sha256": _evaluation_digest(cell_geometry),
                    "nucleus_geometry": nucleus_geometry,
                    "nucleus_geometry_sha256": _evaluation_digest(nucleus_geometry),
                    "measurement_values": measurement_values,
                    "measurement_vector_sha256": (None if measurement_values is None else
                                                  _evaluation_digest(measurement_values)),
                    "owner_evidence_sha256": snapshot.snapshot_sha256,
                }
            )
        for domain_id in sorted(domain_ids):
            binding = bindings.get((snapshot.project_entry_id, domain_id))
            if binding is None:
                raise BatchTrainingError("supervised cell domain lacks exact plan binding")
            result.append({
                **project_deferred_evidence(snapshot.deferred_feature_evidence,
                    (cell.cell_id for cell in cells if cell.analysis_domain_id == domain_id)),
                "entry_kind": "object",
                "project_entry_id": snapshot.project_entry_id,
                "subject_id": snapshot.subject_id,
                "split": snapshot.split,
                "image_identity_sha256": snapshot.image_identity_sha256,
                "saved_state_sha256": snapshot.post_binding_saved_state_sha256,
                "section_id": snapshot.section_id,
                "analysis_domain_id": domain_id,
                "analysis_domain_revision_sha256": binding.analysis_domain_revision_sha256,
                "domain_set_sha256": binding.domain_set_sha256,
                "segmentation_generation_id": snapshot.segmentation_generation_id,
                "measurement_generation_id": snapshot.measurement_generation_id,
                "source_snapshot_sha256": snapshot.snapshot_sha256,
                "evaluation_cells": [row for row in evaluation_cells
                                     if cells_by_id[row["cell_id"]].analysis_domain_id == domain_id],
            })
    if labelled_roles != expected_roles:
        raise BatchTrainingError(
            "Every held-out subject needs saved reference cells in its selected split. "
            "Label cells in any included section, then freeze references again.")
    return result, owner_truth, feature_schema


def _request_core(
    *,
    plan: TrainingPlan,
    evidence: object,
    evaluation_split: Literal["validation", "test"],
    checkpoint_entry: object,
    fit_authority: QuPathNativeClassifierFitAuthority,
    pixel_entry_channel_features: tuple[QuPathPixelEntryChannelFeatures, ...],
) -> tuple[dict[str, object], object, tuple[object, ...] | None]:
    if evaluation_split not in ("validation", "test"):
        raise BatchTrainingError("held-out evaluation split must be validation or test")
    material = fit_authority.material_record
    configuration = _native_configuration(plan)
    if type(evidence) is _PixelHeldOutEvaluationEvidence:
        request_entries, truth = _pixel_request_entries(
            plan, evidence, pixel_entry_channel_features
        )
        feature_spec = request_entries[0]["channel_features"]
        feature_schema = None
    elif type(evidence) is _SupervisedHeldOutEvaluationEvidence:
        if pixel_entry_channel_features:
            raise BatchTrainingError("supervised evaluation cannot accept pixel channel facts")
        request_entries, truth, feature_schema = _supervised_request_entries(plan, evidence)
        feature_spec = {
            "schema": "qupath-object-feature-spec/v1",
            "ordered_measurements": [item.to_mapping() for item in feature_schema],
        }
    else:
        raise BatchTrainingError("held-out evidence family is unsupported")
    # Project only after the complete authenticated held-out evidence has
    # passed its existing entry, class, feature and patient correspondence.
    request_entries = [entry for entry in request_entries if entry["split"] == evaluation_split]
    if not request_entries:
        raise BatchTrainingError("selected evaluation split has no held-out entries")
    selected_entries = {entry["project_entry_id"] for entry in request_entries}
    truth = {identity: row for identity, row in truth.items() if identity[0] in selected_entries}
    core = {
        "schema_name": "mif-qupath-held-out-classifier-evaluation-request",
        "schema_version": 2,
        "authority_layout_sha256": material.authority_layout_sha256,
        "workspace_storage_scope_sha256": material.workspace_storage_scope_sha256,
        "active_mutation_run_sha256": material.active_mutation_run_sha256,
        "project_identity_sha256": material.project_identity_sha256,
        "fit_plan_sha256": material.fit_plan_sha256,
        "fit_execution_sha256": material.fit_execution_sha256,
        "checkpoint_entry_sha256": checkpoint_entry.checkpoint_entry_sha256,
        "fit_material_sha256": material.fit_material_sha256,
        "classifier_id": material.classifier_id,
        "classifier_kind": material.classifier_kind,
        "classifier_engine": "qupath_native",
        "native_classifier_type": material.native_classifier_type,
        "pixel_sampling_method": (
            (
                "kronos-complete-patch-grid/v1"
                if plan.request.classifier_engine
                in {"qupath-kronos-rtrees-classifier", "qupath-kronos-svm-classifier"}
                else "qupath-native-classification-grid/v1"
            )
            if material.native_classifier_type == "pixel"
            else None
        ),
        "classifier_version": material.classifier_version,
        "project_relative_path": material.project_relative_path,
        "native_classifier_sha256": material.native_classifier_sha256,
        "native_classifier_byte_count": material.native_classifier_byte_count,
        "declared_class_order": list(plan.request.required_classes),
        "native_output_labels": _native_output_labels(
            plan=plan, configuration=configuration, evidence=evidence
        ),
        "feature_spec": feature_spec,
        "evaluation_entries": request_entries,
        "expected_qupath_version": material.qupath_version,
        "script_asset_id": ("qupath/scripts/general/04_evaluate_held_out_classifiers.groovy"),
        "launch_nonce_sha256": hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
    }
    return core, truth, feature_schema


def _require_consumed_correspondence(
    target: HeldOutEvaluationTargetAuthority,
    plan: TrainingPlan,
    checkpoint_entry: object,
    fit_authority: QuPathNativeClassifierFitAuthority,
) -> None:
    fit_plan = TrainingFitPlanProjection.create(plan)
    material = fit_authority.material_record
    expected_kind = (
        "supervised_phenotype"
        if plan.request.classifier_kind is ClassifierKind.SUPERVISED_PHENOTYPE
        else "pixel"
    )
    if (
        target.training_plan_id != plan.training_plan_id
        or target.classifier_id != plan.request.classifier_id
        or target.classifier_kind is not plan.request.classifier_kind
        or target.required_classes != plan.request.required_classes
        or target.fit_plan_sha256 != fit_plan.fit_plan_sha256
        or target.fit_plan_sha256 != checkpoint_entry.fit_plan_sha256
        or target.fit_execution_sha256 != checkpoint_entry.fit_execution_sha256
        or target.checkpoint_entry_sha256 != checkpoint_entry.checkpoint_entry_sha256
        or material.classifier_id != plan.request.classifier_id
        or material.classifier_kind != expected_kind
        or material.fit_plan_sha256 != fit_plan.fit_plan_sha256
        or material.fit_execution_sha256 != checkpoint_entry.fit_execution_sha256
        or material.fit_material_sha256 != checkpoint_entry.runtime_fit_material_sha256
        or material.native_classifier_sha256 != checkpoint_entry.native_classifier_sha256
        or material.native_classifier_byte_count != checkpoint_entry.native_classifier_byte_count
        or material.project_relative_path != checkpoint_entry.project_relative_path
        or material.project_identity_sha256 != checkpoint_entry.project_identity_sha256
        or fit_authority.mutation_attestation_sha256 != checkpoint_entry.mutation_attestation_sha256
        or fit_authority.successor_project_authority_head_sha256
        != checkpoint_entry.successor_project_authority_head_sha256
    ):
        raise BatchTrainingError("held-out target differs from its selected classifier fit")


def _require_transport_correspondence(
    *,
    transport: QuPathNativeClassifierReadOnlyEvidence,
    request: object,
    fit_authority: QuPathNativeClassifierFitAuthority,
) -> None:
    material = fit_authority.material_record
    request_bytes = request.to_bytes()
    result_bytes = transport.result_bytes
    expected = (
        (transport.authority_layout_sha256, request.authority_layout_sha256),
        (
            transport.workspace_storage_scope_sha256,
            request.workspace_storage_scope_sha256,
        ),
        (transport.active_mutation_run_sha256, request.active_mutation_run_sha256),
        (transport.project_identity_sha256, request.project_identity_sha256),
        (transport.fit_material_sha256, request.fit_material_sha256),
        (transport.classifier_id, request.classifier_id),
        (transport.classifier_kind, request.classifier_kind),
        (transport.request_identity_sha256, request.request_identity_sha256),
        (transport.request_envelope_sha256, request.request_envelope_sha256),
        (transport.request_sha256, hashlib.sha256(request_bytes).hexdigest()),
        (transport.result_byte_count, len(result_bytes)),
        (transport.result_sha256, hashlib.sha256(result_bytes).hexdigest()),
        (transport.launch_nonce_sha256, request.launch_nonce_sha256),
        (transport.request_ref, request.request_member_ref),
        (transport.result_ref, request.result_member_ref),
        (
            transport.project_authority_head_before_sha256,
            fit_authority.successor_project_authority_head_sha256,
        ),
        (
            transport.project_authority_head_after_sha256,
            transport.project_authority_head_before_sha256,
        ),
        (transport.qpproj_before_sha256, material.qpproj_snapshot_sha256),
        (transport.qpproj_after_sha256, transport.qpproj_before_sha256),
        (transport.native_classifier_before_sha256, material.native_classifier_sha256),
        (
            transport.native_classifier_after_sha256,
            transport.native_classifier_before_sha256,
        ),
        (
            transport.native_classifier_byte_count,
            material.native_classifier_byte_count,
        ),
    )
    if any(actual != wanted for actual, wanted in expected):
        raise BatchTrainingError("read-only evaluation transport proof differs")


def _pixel_metrics(
    *, request: object, result: object, truth: Mapping[tuple[str, str, str], object]
) -> tuple[ClassifierEvaluationMetrics, ...]:
    scores_by_split = {item["split"]: item for item in (result.pixel_scores or ())}
    split_by_entry = {
        entry["project_entry_id"]: entry["split"] for entry in request.evaluation_entries
    }
    observations_by_split: dict[str, list[ClassifierPredictionObservation]] = {}
    locations_by_split: dict[str, list[_PixelAnnotationObservation]] = {}
    expected_by_split: dict[str, list[ClassifierObservationIdentity]] = {}
    for prediction in result.predictions:
        key = (
            prediction["project_entry_id"],
            prediction["subject_id"],
            prediction["annotation_uuid"],
        )
        row = truth.get(key)
        if row is None or (
            prediction["owner_evidence_sha256"] != row.evidence_sha256
            or prediction["normalized_roi_sha256"] != row.normalized_roi_sha256
        ):
            raise BatchTrainingError("pixel result differs from consumed truth")
        split = split_by_entry[key[0]]
        identity = ClassifierObservationIdentity(*key)
        expected_by_split.setdefault(split, []).append(identity)
        observations_by_split.setdefault(split, []).append(
            ClassifierPredictionObservation(
                identity=identity,
                truth_class_id=row.class_id,
                predicted_class_id=prediction["predicted_class_id"],
            )
        )
        locations_by_split.setdefault(split, []).append(_PixelAnnotationObservation(
            project_entry_id=key[0], subject_id=key[1], annotation_uuid=key[2],
            truth_class_id=row.class_id, predicted_class_id=prediction["predicted_class_id"],
            normalized_roi_sha256=prediction["normalized_roi_sha256"],
            roi_bounds=prediction["roi_bounds"],
        ))
    return tuple(
        _pixel_split_metrics(
            split=split,
            classes=tuple(request.declared_class_order),
            expected=expected_by_split.get(split, ()),
            observations=observations_by_split.get(split, ()),
            locations=locations_by_split.get(split, ()),
            pixel_scores=scores_by_split.get(split),
        )
        for split in ("validation", "test")
        if split in expected_by_split
    )


def _pixel_split_metrics(
    *,
    split: str,
    classes: tuple[str, ...],
    expected: Sequence[ClassifierObservationIdentity],
    observations: Sequence[ClassifierPredictionObservation],
    locations: Sequence[_PixelAnnotationObservation],
    pixel_scores: Mapping[str, object] | None = None,
) -> ClassifierEvaluationMetrics:
    reduction = reduce_classifier_confusion(
        class_ids=classes,
        expected_identities=expected,
        observations=observations,
    )
    return ClassifierEvaluationMetrics.create(
        split=split,
        pooled=reduction.pooled.metrics,
        per_project_entry=tuple(
            ScopedMetricSet(scope, summary.metrics)
            for scope, summary in reduction.per_project_entry.items()
        ),
        per_subject=tuple(
            ScopedMetricSet(scope, summary.metrics)
            for scope, summary in reduction.per_subject.items()
        ),
        pixel_confusion=_PixelConfusionEvidence(
            class_ids=reduction.class_ids,
            observation_unit="annotation_majority",
            zero_denominator_policy=reduction.pooled.zero_denominator_policy,
            pooled=reduction.pooled.confusion_matrix,
            per_project_entry={
                scope: summary.confusion_matrix
                for scope, summary in reduction.per_project_entry.items()
            },
            per_subject={
                scope: summary.confusion_matrix for scope, summary in reduction.per_subject.items()
            },
            observations=tuple(sorted(locations, key=lambda item: item.identity)),
        ),
        pixel_scores=pixel_scores,
    )


def _cell_key(project_entry_id: str, cell_id: str) -> str:
    return json.dumps(
        {"cell_id": cell_id, "project_entry_id": project_entry_id},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _supervised_qc_metrics(qc: object, expected_keys: tuple[str, ...]) -> Mapping[str, float]:
    if (
        qc.holdout_cell_ids != expected_keys
        or qc.rejected_count != 0
        or qc.rejected_cell_ids
        or qc.zero_denominator_policy != "zero"
    ):
        raise BatchTrainingError("supervised QC scope differs from exact predictions")
    return MappingProxyType({name: float(getattr(qc, name)) for name in _METRIC_NAMES})


def _supervised_metrics(
    *,
    target: HeldOutEvaluationTargetAuthority,
    fit_authority: QuPathNativeClassifierFitAuthority,
    request: object,
    result: object,
    truth: Mapping[tuple[str, str], tuple[object, object, str]],
    feature_schema: tuple[object, ...],
) -> tuple[ClassifierEvaluationMetrics, ...]:
    identity = SupervisedDeploymentClassifierIdentity(
        classifier_id=request.classifier_id,
        classifier_sha256=fit_authority.material_record.native_classifier_sha256,
        qupath_version=request.expected_qupath_version,
        class_ids=tuple(request.declared_class_order),
        feature_schema=feature_schema,
        validation_domain_id=target.validation_domain_id,
    )
    split_by_entry = {
        entry["project_entry_id"]: entry["split"] for entry in request.evaluation_entries
    }
    subject_by_entry = {
        entry["project_entry_id"]: entry["subject_id"] for entry in request.evaluation_entries
    }
    by_split: dict[str, list[tuple[str, str, str, str]]] = {}
    for prediction in result.predictions:
        key = (prediction["project_entry_id"], prediction["cell_id"])
        owner = truth.get(key)
        if owner is None:
            raise BatchTrainingError("supervised result has foreign cell identity")
        snapshot, cell, truth_class = owner
        if (
            prediction["source_snapshot_sha256"] != snapshot.snapshot_sha256
            or prediction["owner_evidence_sha256"] != snapshot.snapshot_sha256
            or prediction["section_id"] != snapshot.section_id
            or prediction["segmentation_generation_id"] != snapshot.segmentation_generation_id
            or prediction["measurement_generation_id"] != snapshot.measurement_generation_id
            or prediction["cell_geometry_sha256"]
            != _evaluation_digest(_json_copy(cell.to_export_mapping()["cell_geometry"]))
            or prediction["nucleus_geometry_sha256"]
            != _evaluation_digest(_json_copy(cell.to_export_mapping()["nucleus_geometry"]))
        ):
            raise BatchTrainingError("supervised result differs from live cell truth")
        entry_id, cell_id = key
        split = split_by_entry[entry_id]
        by_split.setdefault(split, []).append(
            (
                entry_id,
                subject_by_entry[entry_id],
                _cell_key(entry_id, cell_id),
                truth_class,
                prediction["predicted_class_id"],
            )
        )
    metrics: list[ClassifierEvaluationMetrics] = []
    for split in ("validation", "test"):
        rows = by_split.get(split)
        if not rows:
            continue
        pooled = _evaluate_supervised_scope(identity, rows)
        entry_metrics = tuple(
            ScopedMetricSet(
                scope,
                _evaluate_supervised_scope(identity, tuple(row for row in rows if row[0] == scope)),
            )
            for scope in sorted({row[0] for row in rows})
        )
        subject_metrics = tuple(
            ScopedMetricSet(
                scope,
                _evaluate_supervised_scope(identity, tuple(row for row in rows if row[1] == scope)),
            )
            for scope in sorted({row[1] for row in rows})
        )
        metrics.append(
            ClassifierEvaluationMetrics.create(
                split=split,
                pooled=pooled,
                per_project_entry=entry_metrics,
                per_subject=subject_metrics,
            )
        )
    return tuple(metrics)


def _evaluate_supervised_scope(
    identity: SupervisedDeploymentClassifierIdentity,
    rows: Sequence[tuple[str, str, str, str, str]],
) -> Mapping[str, float]:
    keys = tuple(sorted(row[2] for row in rows))
    truth = {row[2]: row[3] for row in rows}
    predicted = {row[2]: row[4] for row in rows}
    if len(truth) != len(rows) or len(predicted) != len(rows):
        raise BatchTrainingError("entry-scoped supervised cell identity collapsed")
    qc = evaluate_predictions(
        truth,
        predicted,
        classifier_identity=identity,
        holdout_cell_ids=keys,
    )
    if (
        qc.artifact_sha256 != identity.classifier_sha256
        or qc.classifier_id != identity.classifier_id
        or qc.validation_domain_id != identity.validation_domain_id
        or qc.class_ids != identity.class_ids
        or qc.feature_ids != tuple(item.measurement_name for item in identity.feature_schema)
    ):
        raise BatchTrainingError("supervised QC identity differs")
    return _supervised_qc_metrics(qc, keys)


def evaluate_held_out_native_classifier(
    target: HeldOutEvaluationTargetAuthority,
    *,
    project_provider: ProjectMutationLeaseProvider,
    runner: object,
    pixel_entry_channel_features: tuple[QuPathPixelEntryChannelFeatures, ...] = (),
) -> HeldOutClassifierEvaluation:
    """Evaluate one consumed target without publishing any durable record."""

    if (
        type(target) is not HeldOutEvaluationTargetAuthority
        or type(project_provider) is not ProjectMutationLeaseProvider
        or type(runner) is not QuPathRunner
        or type(pixel_entry_channel_features) is not tuple
        or not all(
            type(item) is QuPathPixelEntryChannelFeatures for item in pixel_entry_channel_features
        )
    ):
        raise BatchTrainingError("held-out evaluation requires exact argument types")
    try:
        plan, evidence, checkpoint_entry, fit_authority = _consume_held_out_evaluation_target(
            target
        )
        if (
            type(plan) is not TrainingPlan
            or type(checkpoint_entry) is not BatchNativeFitCheckpointEntry
            or type(fit_authority) is not QuPathNativeClassifierFitAuthority
            or type(evidence)
            not in {
                _PixelHeldOutEvaluationEvidence,
                _SupervisedHeldOutEvaluationEvidence,
            }
            or fit_authority._provider is not project_provider
        ):
            raise BatchTrainingError("held-out evaluation consumed foreign owner material")
        _require_consumed_correspondence(target, plan, checkpoint_entry, fit_authority)
        core, truth, feature_schema = _request_core(
            plan=plan,
            evidence=evidence,
            evaluation_split=target.evaluation_split,
            checkpoint_entry=checkpoint_entry,
            fit_authority=fit_authority,
            pixel_entry_channel_features=pixel_entry_channel_features,
        )
        retained = project_provider.project_authority._retained
        request = held_out_evaluation._issue_qupath_held_out_evaluation_request(
            core_fields=core,
            member_ref_issuer=lambda *, family, scope, natural_key: (
                _expected_runtime_exchange_ref(
                    authority_layout_sha256=retained.layout.layout_sha256,
                    scope=scope,
                    direction=natural_key["direction"],
                )
            ),
        )
        reparsed_request = held_out_evaluation.decode_qupath_held_out_evaluation_request(
            request.to_bytes()
        )
        if reparsed_request != request:
            raise BatchTrainingError("held-out evaluation request does not reparse")
        transport = project_provider.run_native_classifier_read_only_evaluation(
            fit_authority, runner=runner, request=request
        )
        transport = project_provider._authenticate_native_classifier_read_only_evidence(transport)
        if type(transport) is not QuPathNativeClassifierReadOnlyEvidence:
            raise BatchTrainingError("held-out evaluation transport is foreign")
        _require_transport_correspondence(
            transport=transport,
            request=reparsed_request,
            fit_authority=fit_authority,
        )
        _revalidate_consumed_held_out_evaluation_target(
            target, plan, evidence, checkpoint_entry, fit_authority
        )
        result = held_out_evaluation.decode_qupath_held_out_evaluation_result(
            transport.result_bytes, request=reparsed_request
        )
        if (
            result.to_bytes() != transport.result_bytes
            or hashlib.sha256(result.to_bytes()).hexdigest() != transport.result_sha256
        ):
            raise BatchTrainingError("held-out evaluation result bytes differ")
        if type(evidence) is _PixelHeldOutEvaluationEvidence:
            metrics = _pixel_metrics(request=reparsed_request, result=result, truth=truth)
        else:
            assert feature_schema is not None
            metrics = _supervised_metrics(
                target=target,
                fit_authority=fit_authority,
                request=reparsed_request,
                result=result,
                truth=truth,
                feature_schema=feature_schema,
            )
        expected_splits = tuple(
            split
            for split in ("validation", "test")
            if any(entry["split"] == split for entry in reparsed_request.evaluation_entries)
        )
        if expected_splits != (target.evaluation_split,) or tuple(
            item.split for item in metrics
        ) != (target.evaluation_split,):
            raise BatchTrainingError("held-out metrics differ from exact split truth")
        return _EVALUATION_ISSUER.issue(
            {
                "training_plan_id": plan.training_plan_id,
                "evaluation_split": target.evaluation_split,
                "freeze_review_sha256": target.freeze_review_sha256,
                "classifier_id": plan.request.classifier_id,
                "classifier_kind": plan.request.classifier_kind,
                "validation_domain_id": target.validation_domain_id,
                "fit_plan_sha256": fit_authority.material_record.fit_plan_sha256,
                "fit_execution_sha256": fit_authority.material_record.fit_execution_sha256,
                "checkpoint_entry_sha256": checkpoint_entry.checkpoint_entry_sha256,
                "fit_material_sha256": fit_authority.material_record.fit_material_sha256,
                "native_classifier_sha256": fit_authority.material_record.native_classifier_sha256,
                "request_identity_sha256": reparsed_request.request_identity_sha256,
                "result_sha256": transport.result_sha256,
                "metrics": metrics,
            }
        )
    except BatchTrainingError:
        raise
    except Exception as exc:
        raise BatchTrainingError("held-out native classifier evaluation failed") from exc


__all__ = [
    "HeldOutClassifierEvaluation",
    "evaluate_held_out_native_classifier",
]
