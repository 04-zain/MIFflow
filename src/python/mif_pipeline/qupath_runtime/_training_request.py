"""Strict native-training request records and training-material validation.

The runtime checks the declared project, image or process identity before exposing the
selected operation. Result records retain the native outcome and relevant file/saved-state
facts for the batch service to validate and publish."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import PurePosixPath
import struct
from uuid import UUID

from mif_pipeline.pipeline_core.authority_layout import (
    AuthorityLayoutError,
    AuthorityMemberRefV2,
)
from mif_pipeline.pipeline_core.durable_records import (
    DurableRecordError,
    canonical_json_bytes,
)

from ._training_codec import (
    _MAX_EXCHANGE_BYTES,
    QuPathNativeTrainingExchangeError,
    _array,
    _canonical_digest,
    _closed_mapping,
    _decode_canonical,
    _export_json,
    _finite,
    _freeze_json,
    _integer,
    _json_shape,
    _literal,
    _ordered_texts,
    _sha256,
    _text,
)

_REQUEST_CORE_FIELDS = frozenset(
    {
        "schema_name",
        "schema_version",
        "authority_layout_sha256",
        "workspace_storage_scope_sha256",
        "active_mutation_run_sha256",
        "project_identity_sha256",
        "acquisition_record_sha256",
        "acquisition_session_challenge_sha256",
        "artifact_inventory_sha256",
        "mutation_request_sha256",
        "mutation_authorization_sha256",
        "fencing_token_sha256",
        "fit_plan_sha256",
        "fit_execution_sha256",
        "classifier_id",
        "classifier_kind",
        "classifier_role",
        "classifier_engine",
        "native_classifier_type",
        "classifier_version",
        "resource_name",
        "project_relative_path",
        "required_classes",
        "eligible_parent_classes",
        "eligible_material_parent_type_ids",
        "training_entries",
        "training_analysis_domain_bindings",
        "training_material_evidence_sha256",
        "feature_spec",
        "feature_spec_sha256",
        "model_configuration",
        "model_configuration_sha256",
        "random_seed",
        "expected_qupath_version",
        "script_asset_id",
        "script_sha256",
        "launch_nonce_sha256",
    }
)
_REQUEST_FIELDS = _REQUEST_CORE_FIELDS | frozenset(
    {"request_member_ref", "result_member_ref", "request_envelope_sha256"}
)
_ENTRY_STATE_FIELDS = frozenset(
    {
        "project_entry_id",
        "sample_id",
        "slide_id",
        "subject_id",
        "split",
        "image_identity_sha256",
        "analysis_domain_binding_sha256s",
        "analysis_domain_binding_set_sha256",
        "source_predecessor_artifact_ref_sha256s",
        "saved_state_before_sha256",
        "saved_state_after_sha256",
        "post_binding_saved_state_sha256",
        "entry_snapshot_set_sha256",
        "entry_state_sha256",
    }
)
_PIXEL_EVIDENCE_FIELDS = frozenset(
    {
        "schema_version",
        "project_entry_id",
        "annotation_uuid",
        "class_id",
        "path_class_id",
        "normalized_roi_sha256",
        "material_parent_id",
        "material_parent_type",
        "material_parent_type_id",
        "material_parent_revision_sha256",
        "split",
        "analysis_domain_id",
        "analysis_domain_revision_sha256",
        "domain_set_sha256",
        "cell_ids",
        "cell_evidence_sha256s",
        "segmentation_generation_id",
        "measurement_generation_id",
        "cell_selection_rule",
        "cell_selection_sha256",
        "evidence_sha256",
    }
)
_DOMAIN_BINDING_FIELDS = frozenset(
    {
        "project_entry_id",
        "sample_id",
        "slide_id",
        "analysis_domain_id",
        "analysis_domain_revision",
        "analysis_domain_revision_sha256",
        "domain_set_sha256",
    }
)
_RTREES_FIELDS = frozenset(
    {
        "max_depth",
        "min_sample_count",
        "use_1se_rule",
        "active_var_count",
        "max_trees",
        "termination_epsilon",
        "calculate_variable_importance",
        "cv_folds",
        "use_surrogates",
    }
)
_PIXEL_LABEL_MAX = 255


# ==============================================================================
#                                Training request
# ==============================================================================


def _validate_domain_binding(value: object) -> Mapping[str, object]:
    binding = _closed_mapping(value, _DOMAIN_BINDING_FIELDS, "analysis-domain binding")
    for name in ("project_entry_id", "sample_id", "slide_id"):
        _text(binding[name], f"analysis-domain binding {name}")
    for name in (
        "analysis_domain_id",
        "analysis_domain_revision_sha256",
        "domain_set_sha256",
    ):
        _sha256(binding[name], f"analysis-domain binding {name}")
    _integer(binding["analysis_domain_revision"], "analysis-domain revision", minimum=1)
    return binding


def _binding_sort_key(value: Mapping[str, object]) -> tuple[object, ...]:
    return (
        value["sample_id"],
        value["slide_id"],
        value["analysis_domain_id"],
        value["analysis_domain_revision"],
        value["project_entry_id"],
    )


def _validate_feature_spec(value: object, native_type: str) -> Mapping[str, object]:
    if native_type == "pixel":
        spec = _closed_mapping(
            value, frozenset({"schema", "ordered_channels"}), "pixel feature spec"
        )
        _literal(spec["schema"], "qupath-pixel-channel-features/v1", "pixel feature schema")
        channels = _array(spec["ordered_channels"], "ordered_channels", nonempty=True)
        ids: list[str] = []
        indices: list[int] = []
        for ordinal, raw in enumerate(channels):
            row = _closed_mapping(
                raw,
                frozenset(
                    {
                        "canonical_channel_id",
                        "qupath_channel_index",
                        "observed_qupath_channel_name",
                    }
                ),
                f"ordered_channels[{ordinal}]",
            )
            ids.append(_text(row["canonical_channel_id"], "canonical channel ID"))
            indices.append(_integer(row["qupath_channel_index"], "QuPath channel index"))
            _text(row["observed_qupath_channel_name"], "observed QuPath channel name")
        if len(set(ids)) != len(ids) or len(set(indices)) != len(indices):
            raise QuPathNativeTrainingExchangeError(
                "pixel channel IDs and source indices must be unique"
            )
        return spec
    spec = _closed_mapping(
        value,
        frozenset(
            {
                "schema",
                "ordered_measurements",
                "normalization",
                "pca_retained_variance",
            } | ({"pca_normalize"} if isinstance(value, Mapping) and "pca_normalize" in value else set())
        ),
        "object feature spec",
    )
    _literal(spec["schema"], "qupath-object-feature-spec/v1", "object feature schema")
    rows = _array(spec["ordered_measurements"], "ordered_measurements", nonempty=True)
    names: list[str] = []
    for ordinal, raw in enumerate(rows):
        row = _closed_mapping(
            raw,
            frozenset({"measurement_name", "semantic_role"}),
            f"ordered_measurements[{ordinal}]",
        )
        names.append(_text(row["measurement_name"], "measurement name"))
        _text(row["semantic_role"], "measurement semantic role")
    if len(set(names)) != len(names):
        raise QuPathNativeTrainingExchangeError("measurement names must be unique")
    if type(spec.get("pca_normalize", True)) is not bool:
        raise QuPathNativeTrainingExchangeError("PCA whitening must be a boolean")
    if spec["normalization"] not in {"none", "mean_variance", "min_max"}:
        raise QuPathNativeTrainingExchangeError("object normalization is invalid")
    pca = _finite(spec["pca_retained_variance"], "pca_retained_variance", minimum=0.0)
    if pca > 1.0:
        raise QuPathNativeTrainingExchangeError("pca_retained_variance exceeds one")
    return spec


def _validate_output_labels(value: object, *, object_family: bool) -> tuple[str, ...]:
    labels = _closed_mapping(value, frozenset({"schema", "ordered_labels"}), "output labels")
    _literal(labels["schema"], "qupath-output-labels/v1", "output-label schema")
    rows = _array(labels["ordered_labels"], "ordered output labels", nonempty=True)
    class_ids: list[str] = []
    native_labels: list[int] = []
    for ordinal, raw in enumerate(rows):
        row = _closed_mapping(
            raw,
            frozenset({"canonical_class_id", "qupath_label"}),
            f"output label {ordinal}",
        )
        class_ids.append(_text(row["canonical_class_id"], "output class ID"))
        native_labels.append(_integer(row["qupath_label"], "QuPath output label"))
    if len(set(class_ids)) != len(class_ids) or len(set(native_labels)) != len(native_labels):
        raise QuPathNativeTrainingExchangeError("output labels must be unique")
    if not object_family and any(label > _PIXEL_LABEL_MAX for label in native_labels):
        raise QuPathNativeTrainingExchangeError(
            f"pixel output labels must be at most {_PIXEL_LABEL_MAX}"
        )
    if object_family and tuple(native_labels) != tuple(range(len(native_labels))):
        raise QuPathNativeTrainingExchangeError(
            "object output labels must be zero-based and contiguous in tuple order"
        )
    return tuple(class_ids)


def _validate_rtrees(value: object) -> None:
    rtrees = _closed_mapping(value, _RTREES_FIELDS, "RTrees configuration")
    _integer(rtrees["max_depth"], "max_depth", minimum=1)
    _integer(rtrees["min_sample_count"], "min_sample_count", minimum=1)
    for name in ("use_1se_rule", "calculate_variable_importance"):
        if type(rtrees[name]) is not bool:
            raise QuPathNativeTrainingExchangeError(f"{name} must be boolean")
    _integer(rtrees["active_var_count"], "active_var_count")
    max_trees = _integer(rtrees["max_trees"], "max_trees")
    epsilon = _finite(rtrees["termination_epsilon"], "termination_epsilon", minimum=0.0)
    if max_trees == 0 and epsilon == 0.0:
        raise QuPathNativeTrainingExchangeError("RTrees termination cannot be disabled")
    _literal(rtrees["cv_folds"], 0, "cv_folds")
    _literal(rtrees["use_surrogates"], False, "use_surrogates")


def _validate_svm(value: object) -> None:
    svm = _closed_mapping(
        value,
        frozenset(
            {
                "model_type",
                "kernel",
                "c",
                "gamma",
                "max_iterations",
                "termination_epsilon",
            }
        ),
        "SVM configuration",
    )
    _literal(svm["model_type"], "C_SVC", "SVM model type")
    if svm["kernel"] not in ("LINEAR", "RBF"):
        raise QuPathNativeTrainingExchangeError("SVM kernel must be LINEAR or RBF")
    _finite(svm["c"], "SVM C", minimum=0.0, positive=True)
    if svm["kernel"] == "LINEAR":
        if svm["gamma"] is not None:
            raise QuPathNativeTrainingExchangeError("linear SVM gamma must be null")
    else:
        _finite(svm["gamma"], "SVM gamma", minimum=0.0, positive=True)
    _integer(svm["max_iterations"], "SVM max iterations", minimum=1)
    _finite(svm["termination_epsilon"], "SVM termination tolerance", minimum=0.0, positive=True)


def _validate_logistic_regression(value: object) -> None:
    """Validate the exact batch OpenCV LR controls before invoking the JVM."""
    lr = _closed_mapping(value, frozenset({"learning_rate", "iterations", "regularization",
        "training_method", "termination_epsilon"}), "logistic regression configuration")
    _finite(lr["learning_rate"], "LR learning rate", minimum=0.0, positive=True)
    _integer(lr["iterations"], "LR iterations", minimum=1)
    if lr["regularization"] not in {"L2", "DISABLE"}:
        raise QuPathNativeTrainingExchangeError("LR regularization is unsupported")
    _literal(lr["training_method"], "BATCH", "LR training method")
    _finite(lr["termination_epsilon"], "LR termination tolerance", minimum=0.0, positive=True)


def _validate_model_configuration(
    value: object, *, native_type: str, required_classes: tuple[str, ...]
) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        raise QuPathNativeTrainingExchangeError("model configuration is invalid")
    schema = value.get("configuration_schema")
    if schema == "qupath-grouped-cross-validation/v1":
        from mif_pipeline.pipeline_core.model_selection import search_candidates

        try:
            candidates = search_candidates(value)
            labels = [
                _validate_model_configuration(
                    row, native_type=native_type, required_classes=required_classes
                )
                for row in candidates
            ]
        except ValueError as exc:
            raise QuPathNativeTrainingExchangeError(str(exc)) from exc
        return labels[0]
    if schema in {"qupath-kronos-rtrees/v2", "qupath-kronos-svm/v2"}:
        if native_type != "pixel":
            raise QuPathNativeTrainingExchangeError(
                "KRONOS is a tissue-patch classifier, not a cell-object estimator"
            )
        from ._kronos_wire import validate_kronos_configuration

        native = validate_kronos_configuration(value)
        return _validate_model_configuration(
            native, native_type=native_type, required_classes=required_classes
        )
    if schema == f"qupath-{native_type}-rtrees/v1":
        model_key, validate_model = "rtrees", _validate_rtrees
    elif schema == f"qupath-{native_type}-svm/v1":
        model_key, validate_model = "svm", _validate_svm
    elif native_type == "object" and schema == "qupath-object-logistic-regression/v1":
        model_key, validate_model = "logistic_regression", _validate_logistic_regression
    else:
        raise QuPathNativeTrainingExchangeError("native model configuration schema is unsupported")
    if native_type == "pixel":
        strategy = value.get("boundary_strategy")
        fields = {
            "configuration_schema",
            "resolution_um_per_pixel",
            "image_features",
            "maximum_training_samples",
            "reweight_samples",
            "normalization",
            "pca_retained_variance",
            "pca_normalize",
            "boundary_strategy",
            "boundary_thickness_px",
            "output_labels",
            model_key,
        }
        if strategy == "classify":
            fields.add("boundary_class_id")
        config = _closed_mapping(value, frozenset(fields), "pixel model configuration")
        _finite(
            config["resolution_um_per_pixel"], "resolution_um_per_pixel", minimum=0.0, positive=True
        )
        from mif_pipeline.orchestration.batch_training.qupath_feature_settings import PixelImageFeatures
        try:
            PixelImageFeatures.from_mapping(config["image_features"])
        except ValueError as exc:
            raise QuPathNativeTrainingExchangeError(str(exc)) from exc
        _integer(config["maximum_training_samples"], "maximum_training_samples")
        if (
            type(config["reweight_samples"]) is not bool
            or type(config["pca_normalize"]) is not bool
        ):
            raise QuPathNativeTrainingExchangeError("pixel boolean configuration is invalid")
        if config["normalization"] not in {"none", "mean_variance", "min_max"}:
            raise QuPathNativeTrainingExchangeError("pixel normalization is invalid")
        pca = _finite(config["pca_retained_variance"], "pca_retained_variance", minimum=0.0)
        if pca > 1.0 or (pca == 0.0 and config["pca_normalize"] is True):
            raise QuPathNativeTrainingExchangeError("pixel PCA configuration is invalid")
        if strategy not in {"skip", "classify"}:
            raise QuPathNativeTrainingExchangeError("pixel boundary strategy is invalid")
        thickness = _finite(config["boundary_thickness_px"], "boundary_thickness_px", minimum=0.0)
        class_ids = _validate_output_labels(config["output_labels"], object_family=False)
        if strategy == "skip":
            if (
                thickness != 0.0
                or set(class_ids) != set(required_classes)
                or len(class_ids) != len(required_classes)
            ):
                raise QuPathNativeTrainingExchangeError(
                    "skip labels/boundary differ from required classes"
                )
        else:
            if thickness <= 0.0:
                raise QuPathNativeTrainingExchangeError(
                    "classify boundary thickness must be positive"
                )
            boundary = _integer(config["boundary_class_id"], "boundary_class_id")
            rows = config["output_labels"]["ordered_labels"]  # type: ignore[index]
            ordinary = [
                row["canonical_class_id"] for row in rows if row["qupath_label"] != boundary
            ]
            if (
                sum(row["qupath_label"] == boundary for row in rows) != 1
                or set(ordinary) != set(required_classes)
                or len(ordinary) != len(required_classes)
            ):
                raise QuPathNativeTrainingExchangeError(
                    "classify labels differ from required classes"
                )
        validate_model(config[model_key])
        return class_ids
    config = _closed_mapping(
        value,
        frozenset(
            {
                "configuration_schema",
                "object_filter",
                "training_annotations",
                "output_classes",
                "normalization",
                "pca_retained_variance",
                "multiclass",
                "output_labels",
                model_key,
            } | ({"feature_source"} if "feature_source" in value else set())
            | ({"pca_normalize"} if "pca_normalize" in value else set())
        ),
        "object model configuration",
    )
    if type(config.get("pca_normalize", True)) is not bool:
        raise QuPathNativeTrainingExchangeError("PCA whitening must be a boolean")
    _literal(config["object_filter"], "cells", "object filter")
    _literal(config["training_annotations"], "points", "training annotations")
    _literal(config["output_classes"], "selected", "output classes")
    _literal(config["multiclass"], False, "multiclass")
    if config["normalization"] not in {"none", "mean_variance", "min_max"}:
        raise QuPathNativeTrainingExchangeError("object normalization is invalid")
    pca = _finite(config["pca_retained_variance"], "pca_retained_variance", minimum=0.0)
    if pca > 1.0:
        raise QuPathNativeTrainingExchangeError("object PCA configuration is invalid")
    class_ids = _validate_output_labels(config["output_labels"], object_family=True)
    if set(class_ids) != set(required_classes) or len(class_ids) != len(required_classes):
        raise QuPathNativeTrainingExchangeError("object output labels differ from required classes")
    validate_model(config[model_key])
    if "feature_source" in config:
        from mif_pipeline.orchestration.batch_training.qupath_kronos_cell import KronosCellFeatureSource
        try:
            KronosCellFeatureSource.from_mapping(config["feature_source"])
        except ValueError as exc:
            raise QuPathNativeTrainingExchangeError(str(exc)) from exc
    return class_ids


def _validate_pixel_row(
    value: object,
    *,
    entry_id: str,
    required_classes: tuple[str, ...],
    material_parent_type_ids: tuple[str, ...],
    bindings: tuple[Mapping[str, object], ...],
) -> Mapping[str, object]:
    """Require one training row inside the declared material and domain scope.

    Material type IDs are projected from the explicit classifier role; they are
    distinct from a primary plan's semantic Tissue eligibility. Digests, classes
    and exact domain revisions remain independently checked.
    """

    row = _closed_mapping(value, _PIXEL_EVIDENCE_FIELDS, "pixel evidence row")
    _literal(row["schema_version"], "training-evidence-snapshot/v2", "training evidence schema")
    _literal(row["project_entry_id"], entry_id, "evidence project entry")
    _literal(row["split"], "training", "evidence split")
    class_id = _text(row["class_id"], "evidence class ID")
    if class_id not in required_classes:
        raise QuPathNativeTrainingExchangeError("pixel evidence class is not required")
    _text(row["path_class_id"], "evidence PathClass ID")
    try:
        annotation = str(UUID(_text(row["annotation_uuid"], "annotation UUID")))
    except (TypeError, ValueError, AttributeError) as exc:
        raise QuPathNativeTrainingExchangeError("annotation UUID is invalid") from exc
    if annotation != row["annotation_uuid"]:
        raise QuPathNativeTrainingExchangeError("annotation UUID is not canonical")
    for name in (
        "normalized_roi_sha256",
        "material_parent_revision_sha256",
        "analysis_domain_id",
        "analysis_domain_revision_sha256",
        "domain_set_sha256",
        "cell_selection_sha256",
        "evidence_sha256",
    ):
        _sha256(row[name], name)
    _text(row["material_parent_id"], "material parent ID")
    if row["material_parent_type"] not in {"analysis_domain", "primary", "secondary"}:
        raise QuPathNativeTrainingExchangeError("material parent type is invalid")
    _text(row["material_parent_type_id"], "material parent type ID")
    if row["material_parent_type_id"] not in material_parent_type_ids:
        raise QuPathNativeTrainingExchangeError("pixel evidence material parent is not eligible")
    if row["material_parent_type"] == "analysis_domain":
        if (
            row["material_parent_type_id"] != "AnalysisDomain"
            or row["material_parent_revision_sha256"] != row["analysis_domain_revision_sha256"]
        ):
            raise QuPathNativeTrainingExchangeError("analysis-domain parent is stale")
    elif row["material_parent_type_id"] == "AnalysisDomain":
        raise QuPathNativeTrainingExchangeError("non-domain parent uses AnalysisDomain type")
    cells = tuple(
        _text(item, f"cell_ids[{index}]")
        for index, item in enumerate(_array(row["cell_ids"], "cell_ids"))
    )
    if cells != tuple(sorted(cells)) or len(set(cells)) != len(cells):
        raise QuPathNativeTrainingExchangeError("cell_ids must be a unique canonical inventory")
    evidence = tuple(
        _sha256(item, "cell evidence digest")
        for item in _array(row["cell_evidence_sha256s"], "cell evidence digests")
    )
    if len(evidence) != len(cells) or len(set(evidence)) != len(evidence):
        raise QuPathNativeTrainingExchangeError("cell evidence does not match stable cells")
    for name in ("segmentation_generation_id", "measurement_generation_id"):
        if cells:
            _text(row[name], name)
        elif row[name] is not None:
            raise QuPathNativeTrainingExchangeError("empty cell selection declares generation")
    _literal(
        row["cell_selection_rule"],
        "explicit-hierarchy-descendant-cells/v1",
        "cell selection rule",
    )
    selection = {
        "cell_ids": list(cells),
        "cell_evidence_sha256s": list(evidence),
        "segmentation_generation_id": row["segmentation_generation_id"],
        "measurement_generation_id": row["measurement_generation_id"],
        "cell_selection_rule": row["cell_selection_rule"],
    }
    if row["cell_selection_sha256"] != _canonical_digest(selection):
        raise QuPathNativeTrainingExchangeError("cell selection digest is invalid")
    unsigned = {key: row[key] for key in row if key != "evidence_sha256"}
    if row["evidence_sha256"] != _canonical_digest(unsigned):
        raise QuPathNativeTrainingExchangeError("pixel evidence digest is invalid")
    binding_keys = {
        (
            item["analysis_domain_id"],
            item["analysis_domain_revision_sha256"],
            item["domain_set_sha256"],
        )
        for item in bindings
    }
    if (
        row["analysis_domain_id"],
        row["analysis_domain_revision_sha256"],
        row["domain_set_sha256"],
    ) not in binding_keys:
        raise QuPathNativeTrainingExchangeError("pixel evidence has no domain binding")
    return row


def _validate_entry_state(
    value: object,
    *,
    rows: list[object],
    bindings: tuple[Mapping[str, object], ...],
) -> Mapping[str, object]:
    """Validate entry state for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    state = _closed_mapping(value, _ENTRY_STATE_FIELDS, "training entry state")
    for name in ("project_entry_id", "sample_id", "slide_id", "subject_id"):
        _text(state[name], f"entry state {name}")
    _literal(state["split"], "training", "entry state split")
    for name in (
        "image_identity_sha256",
        "analysis_domain_binding_set_sha256",
        "saved_state_before_sha256",
        "saved_state_after_sha256",
        "post_binding_saved_state_sha256",
        "entry_snapshot_set_sha256",
        "entry_state_sha256",
    ):
        _sha256(state[name], f"entry state {name}")
    state_binding_digests = tuple(
        _sha256(item, "entry binding digest")
        for item in _array(
            state["analysis_domain_binding_sha256s"],
            "entry binding digests",
            nonempty=True,
        )
    )
    predecessors = tuple(
        _sha256(item, "entry predecessor digest")
        for item in _array(
            state["source_predecessor_artifact_ref_sha256s"],
            "entry predecessor digests",
            nonempty=True,
        )
    )
    if state_binding_digests != tuple(sorted(set(state_binding_digests))) or predecessors != tuple(
        sorted(set(predecessors))
    ):
        raise QuPathNativeTrainingExchangeError("entry digest inventories are not canonical")
    expected_bindings = tuple(
        item for item in bindings if item["project_entry_id"] == state["project_entry_id"]
    )
    if not expected_bindings:
        raise QuPathNativeTrainingExchangeError("entry has no analysis-domain binding")
    if (
        any(
            (item["sample_id"], item["slide_id"]) != (state["sample_id"], state["slide_id"])
            for item in expected_bindings
        )
        or state_binding_digests
        != tuple(sorted(_canonical_digest(item) for item in expected_bindings))
        or state["analysis_domain_binding_set_sha256"] != _canonical_digest(list(expected_bindings))
    ):
        raise QuPathNativeTrainingExchangeError("entry binding inventory is invalid")
    if state["entry_snapshot_set_sha256"] != _canonical_digest(rows):
        raise QuPathNativeTrainingExchangeError("entry snapshot-set digest is invalid")
    unsigned = {key: state[key] for key in state if key != "entry_state_sha256"}
    if state["entry_state_sha256"] != _canonical_digest(unsigned):
        raise QuPathNativeTrainingExchangeError("entry-state digest is invalid")
    return state


_OBJECT_SNAPSHOT_FIELDS = frozenset(
    {
        "project_entry_id",
        "sample_id",
        "slide_id",
        "subject_id",
        "split",
        "image_identity_sha256",
        "section_id",
        "pipeline_run_id",
        "segmentation_generation_id",
        "measurement_generation_id",
        "analysis_domain_binding_sha256s",
        "analysis_domain_binding_set_sha256",
        "coordinate_system",
        "point_selection_radius_level0",
        "training_cell_to_analysis_domain_mapping_sha256",
        "class_ids",
        "feature_schema",
        "point_evidence",
        "eligible_cells",
        "mapped_evidence",
        "snapshot_sha256",
    }
)
_POINT_EVIDENCE_FIELDS = frozenset(
    {
        "point_id",
        "cell_id",
        "slide_id",
        "section_id",
        "x_level0",
        "y_level0",
        "class_id",
        "evidence_set",
        "spatial_region_id",
    }
)
_MAPPED_POINT_FIELDS = _POINT_EVIDENCE_FIELDS | frozenset(
    {"cell_id", "cell_x_level0", "cell_y_level0", "distance_level0"}
)
_CELL_FIELDS = frozenset(
    {
        "cell_id",
        "slide_id",
        "section_id",
        "analysis_domain_id",
        "analysis_domain_kind",
        "x_level0",
        "y_level0",
        "x_um",
        "y_um",
        "nucleus_geometry",
        "cell_geometry",
        "primary_compartment",
        "secondary_roi_ids",
        "marker_measurements",
        "marker_states",
        "primary_phenotype",
        "phenotype_components",
        "phenotype_source",
        "cluster_id",
        "tissue_roi_id",
        "tissue_generation_id",
        "phenotyping_generation_id",
        "phenotype_confidence",
        "cluster_label",
    }
)
_CELL_MEMBERSHIP_FIELDS = frozenset(
    {"pipeline_run_id", "membership_run_id", "membership_generation_id"}
)
_TRAINING_CELL_FIELDS = frozenset(
    {
        "cell_id",
        "expected_class_id",
        "analysis_domain_id",
        "slide_id",
        "section_id",
        "measurement_values",
        "measurement_vector_sha256",
    }
)


def _validate_point(
    value: object,
    *,
    slide_id: str,
    section_id: str,
    required_classes: tuple[str, ...],
) -> Mapping[str, object]:
    point = _closed_mapping(value, _POINT_EVIDENCE_FIELDS, "point evidence")
    _text(point["point_id"], "point ID")
    _text(point["cell_id"], "point stable cell ID")
    _literal(point["slide_id"], slide_id, "point slide")
    _literal(point["section_id"], section_id, "point section")
    _finite(point["x_level0"], "point x")
    _finite(point["y_level0"], "point y")
    if _text(point["class_id"], "point class") not in required_classes:
        raise QuPathNativeTrainingExchangeError("point class is not required")
    _literal(point["evidence_set"], "training", "point evidence set")
    _text(point["spatial_region_id"], "point spatial region")
    return point


def _validate_cell(
    value: object,
    *,
    slide_id: str,
    section_id: str,
    bindings: tuple[Mapping[str, object], ...],
    measurement_names: tuple[str, ...],
    deferred: bool = False,
) -> Mapping[str, object]:
    """Validate cell for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    if not isinstance(value, Mapping):
        raise QuPathNativeTrainingExchangeError("eligible cell must be an object")
    fields = frozenset(value)
    if fields not in {_CELL_FIELDS, _CELL_FIELDS | _CELL_MEMBERSHIP_FIELDS}:
        raise QuPathNativeTrainingExchangeError("eligible cell fields are invalid")
    cell = value
    _text(cell["cell_id"], "stable cell ID")
    _literal(cell["slide_id"], slide_id, "cell slide")
    _literal(cell["section_id"], section_id, "cell section")
    domain_id = _sha256(cell["analysis_domain_id"], "cell analysis-domain ID")
    if domain_id not in {item["analysis_domain_id"] for item in bindings}:
        raise QuPathNativeTrainingExchangeError("cell belongs to a foreign domain")
    if cell["analysis_domain_kind"] not in {
        "selected_roi",
        "whole_tissue_section",
    }:
        raise QuPathNativeTrainingExchangeError("cell analysis-domain kind is invalid")
    for name in ("x_level0", "y_level0"):
        _finite(cell[name], f"cell {name}")
    for name in ("x_um", "y_um"):
        if cell[name] is not None:
            _finite(cell[name], f"cell {name}")
    for name in ("nucleus_geometry", "cell_geometry", "marker_states"):
        if not isinstance(cell[name], Mapping):
            raise QuPathNativeTrainingExchangeError(f"cell {name} must be an object")
    if (
        type(cell["secondary_roi_ids"]) is not list
        or type(cell["phenotype_components"]) is not list
    ):
        raise QuPathNativeTrainingExchangeError("cell tuple projections must be arrays")
    for item in cell["secondary_roi_ids"]:
        _text(item, "secondary ROI ID")
    for item in cell["phenotype_components"]:
        _text(item, "phenotype component")
    measurements = cell["marker_measurements"]
    if not isinstance(measurements, Mapping) or any(type(key) is not str for key in measurements):
        raise QuPathNativeTrainingExchangeError("cell marker measurements are invalid")
    for key, item in measurements.items():
        _text(key, "measurement name")
        _finite(item, f"measurement {key}")
    if deferred and measurements:
        raise QuPathNativeTrainingExchangeError("Deferred cells cannot contain embedding measurements")
    if not deferred and any(name not in measurements for name in measurement_names):
        raise QuPathNativeTrainingExchangeError("cell lacks an ordered training measurement")
    for name in (
        "primary_compartment",
        "primary_phenotype",
        "phenotype_source",
        "cluster_id",
        "tissue_roi_id",
        "tissue_generation_id",
        "phenotyping_generation_id",
        "cluster_label",
    ):
        if cell[name] is not None:
            _text(cell[name], f"cell {name}")
    confidence = cell["phenotype_confidence"]
    if confidence is not None:
        observed = _finite(confidence, "phenotype confidence", minimum=0.0)
        if observed > 1.0:
            raise QuPathNativeTrainingExchangeError("phenotype confidence exceeds one")
    if _CELL_MEMBERSHIP_FIELDS <= fields:
        values = tuple(cell[name] for name in _CELL_MEMBERSHIP_FIELDS)
        if any(item is None for item in values):
            raise QuPathNativeTrainingExchangeError("cell membership identity is partial")
        for name in _CELL_MEMBERSHIP_FIELDS:
            _text(cell[name], f"cell {name}")
    return cell


def _validate_mapped_point(
    value: object,
    *,
    raw_points: Mapping[str, Mapping[str, object]],
    cells: Mapping[str, Mapping[str, object]],
    maximum_distance_level0: float,
) -> Mapping[str, object]:
    mapped = _closed_mapping(value, _MAPPED_POINT_FIELDS, "mapped point evidence")
    point_id = _text(mapped["point_id"], "mapped point ID")
    cell_id = _text(mapped["cell_id"], "mapped stable cell ID")
    point = raw_points.get(point_id)
    cell = cells.get(cell_id)
    if point is None or cell is None:
        raise QuPathNativeTrainingExchangeError("mapped point is unselected")
    for name in _POINT_EVIDENCE_FIELDS:
        if mapped[name] != point[name]:
            raise QuPathNativeTrainingExchangeError("mapped point differs from label evidence")
    _literal(mapped["evidence_set"], "training", "mapped evidence set")
    for mapped_name, cell_name in (
        ("cell_x_level0", "x_level0"),
        ("cell_y_level0", "y_level0"),
    ):
        _finite(mapped[mapped_name], mapped_name)
        if float(mapped[mapped_name]) != float(cell[cell_name]):
            raise QuPathNativeTrainingExchangeError("mapped point cell coordinate differs")
    distance = _finite(mapped["distance_level0"], "mapped point distance", minimum=0.0)
    if distance > maximum_distance_level0:
        raise QuPathNativeTrainingExchangeError("mapped point exceeds the selection radius")
    return mapped


def _validate_object_entry(
    entry: Mapping[str, object],
    *,
    snapshot: Mapping[str, object],
    feature_spec: Mapping[str, object],
    required_classes: tuple[str, ...],
    bindings: tuple[Mapping[str, object], ...],
    feature_source=None,
) -> frozenset[str]:
    """Validate object entry for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    from mif_pipeline.orchestration.batch_training.deferred_cell_evidence import (
        EVIDENCE_FIELD, pending_marker_names, validate_deferred_evidence,
    )
    from mif_pipeline.orchestration.batch_training._shared import BatchTrainingError
    from mif_pipeline.orchestration.batch_training.qupath_kronos_cell import kronos_cell_measurements
    deferred = EVIDENCE_FIELD in snapshot
    try:
        if deferred:
            validate_deferred_evidence(snapshot[EVIDENCE_FIELD],
                (cell["cell_id"] for cell in _array(snapshot["eligible_cells"], "eligible cells")),
                source=feature_source, require_pending=True)
            if feature_spec["ordered_measurements"] != kronos_cell_measurements():
                raise BatchTrainingError("Deferred inputs require the exact KRONOS feature schema")
        elif pending_marker_names(feature_source):
            raise BatchTrainingError("Pending KRONOS statistics require deferred cell evidence")
    except (BatchTrainingError, KeyError, TypeError, ValueError) as exc:
        raise QuPathNativeTrainingExchangeError(str(exc)) from exc
    for name in (
        "project_entry_id",
        "sample_id",
        "slide_id",
        "subject_id",
        "section_id",
        "pipeline_run_id",
        "segmentation_generation_id",
        "measurement_generation_id",
    ):
        _text(snapshot[name], f"object snapshot {name}")
    _literal(snapshot["split"], "training", "object snapshot split")
    for name in (
        "image_identity_sha256",
        "analysis_domain_binding_set_sha256",
        "training_cell_to_analysis_domain_mapping_sha256",
        "snapshot_sha256",
    ):
        _sha256(snapshot[name], f"object snapshot {name}")
    entry_id = snapshot["project_entry_id"]
    binding_digests = tuple(
        _sha256(item, "object snapshot binding digest")
        for item in _array(
            snapshot["analysis_domain_binding_sha256s"],
            "object snapshot binding digests",
            nonempty=True,
        )
    )
    entry_bindings = tuple(item for item in bindings
                           if item["project_entry_id"] == entry_id
                           and _canonical_digest(item) in binding_digests)
    if (
        not entry_bindings
        or binding_digests != tuple(sorted(_canonical_digest(item) for item in entry_bindings))
        or snapshot["analysis_domain_binding_set_sha256"] != _canonical_digest(list(entry_bindings))
        or any(
            (item["sample_id"], item["slide_id"]) != (snapshot["sample_id"], snapshot["slide_id"])
            for item in entry_bindings
        )
    ):
        raise QuPathNativeTrainingExchangeError("object snapshot bindings are invalid")
    _literal(snapshot["coordinate_system"], "global_level0", "object coordinate system")
    point_selection_radius = _finite(
        snapshot["point_selection_radius_level0"],
        "point selection radius",
        minimum=0.0,
        positive=True,
    )
    if (
        tuple(_ordered_texts(snapshot["class_ids"], "snapshot class IDs", sorted_order=True))
        != required_classes
    ):
        raise QuPathNativeTrainingExchangeError("snapshot classes differ from required classes")
    feature_rows = _array(snapshot["feature_schema"], "snapshot feature schema", nonempty=True)
    if feature_rows != feature_spec["ordered_measurements"]:
        raise QuPathNativeTrainingExchangeError("snapshot feature order differs")
    for raw in feature_rows:
        _closed_mapping(raw, frozenset({"measurement_name", "semantic_role"}), "snapshot feature")
    measurement_names = tuple(row["measurement_name"] for row in feature_rows)
    slide_id = snapshot["slide_id"]
    section_id = snapshot["section_id"]
    points = tuple(
        _validate_point(
            item,
            slide_id=slide_id,  # type: ignore[arg-type]
            section_id=section_id,  # type: ignore[arg-type]
            required_classes=required_classes,
        )
        for item in _array(snapshot["point_evidence"], "point evidence")
    )
    point_ids = tuple(item["point_id"] for item in points)
    if point_ids != tuple(sorted(point_ids)) or len(set(point_ids)) != len(point_ids):
        raise QuPathNativeTrainingExchangeError("point evidence is not canonical")
    cells_tuple = tuple(
        _validate_cell(
            item,
            slide_id=slide_id,  # type: ignore[arg-type]
            section_id=section_id,  # type: ignore[arg-type]
            bindings=entry_bindings,
            measurement_names=measurement_names,  # type: ignore[arg-type]
            deferred=deferred,
        )
        for item in _array(snapshot["eligible_cells"], "eligible cells")
    )
    cell_ids = tuple(item["cell_id"] for item in cells_tuple)
    if cell_ids != tuple(sorted(cell_ids)) or len(set(cell_ids)) != len(cell_ids):
        raise QuPathNativeTrainingExchangeError("eligible cells are not canonical")
    points_by_id = {item["point_id"]: item for item in points}
    cells_by_id = {item["cell_id"]: item for item in cells_tuple}
    mapped = tuple(
        _validate_mapped_point(
            item,
            raw_points=points_by_id,
            cells=cells_by_id,
            maximum_distance_level0=point_selection_radius,
        )
        for item in _array(snapshot["mapped_evidence"], "mapped evidence")
    )
    mapped_point_ids = tuple(item["point_id"] for item in mapped)
    if (
        mapped_point_ids != tuple(sorted(mapped_point_ids))
        or len(set(mapped_point_ids)) != len(mapped_point_ids)
        or mapped_point_ids != point_ids
        or tuple(sorted({item["cell_id"] for item in mapped})) != cell_ids
    ):
        raise QuPathNativeTrainingExchangeError("mapped evidence coverage is invalid")
    domain_map = {item["cell_id"]: item["analysis_domain_id"] for item in cells_tuple}
    if snapshot["training_cell_to_analysis_domain_mapping_sha256"] != _canonical_digest(domain_map):
        raise QuPathNativeTrainingExchangeError("cell-to-domain digest is invalid")
    unsigned_snapshot = {key: snapshot[key] for key in snapshot if key != "snapshot_sha256"}
    if snapshot["snapshot_sha256"] != _canonical_digest(unsigned_snapshot):
        raise QuPathNativeTrainingExchangeError("object snapshot digest is invalid")
    training_cells = tuple(
        _closed_mapping(item, _TRAINING_CELL_FIELDS, "native training cell")
        for item in _array(entry["training_cells"], "native training cells")
    )
    training_cell_ids = tuple(
        _text(item["cell_id"], "native training cell ID") for item in training_cells
    )
    if training_cell_ids != cell_ids:
        raise QuPathNativeTrainingExchangeError("native training-cell coverage is invalid")
    mapped_by_cell: dict[str, list[Mapping[str, object]]] = {}
    for item in mapped:
        mapped_by_cell.setdefault(item["cell_id"], []).append(item)  # type: ignore[arg-type]
    observed_classes: set[str] = set()
    for row in training_cells:
        cell = cells_by_id[row["cell_id"]]
        labels = {item["class_id"] for item in mapped_by_cell[row["cell_id"]]}
        if len(labels) != 1:
            raise QuPathNativeTrainingExchangeError("one cell has conflicting point labels")
        expected_class = labels.pop()
        _literal(row["expected_class_id"], expected_class, "native cell class")
        observed_classes.add(expected_class)  # type: ignore[arg-type]
        for name in ("analysis_domain_id", "slide_id", "section_id"):
            _literal(row[name], cell[name], f"native cell {name}")
        if deferred:
            if row["measurement_values"] is not None or row["measurement_vector_sha256"] is not None:
                raise QuPathNativeTrainingExchangeError("Deferred training cells require null embedding values and digest")
            continue
        values = _array(row["measurement_values"], "measurement values", nonempty=True)
        if len(values) != len(measurement_names):
            raise QuPathNativeTrainingExchangeError("measurement vector width differs")
        expected_values = [cell["marker_measurements"][name] for name in measurement_names]  # type: ignore[index]
        for ordinal, value in enumerate(values):
            if type(value) is not float:
                raise QuPathNativeTrainingExchangeError(
                    f"measurement value {ordinal} must be one JSON double"
                )
            _finite(value, f"measurement value {ordinal}")
        if any(
            struct.pack(">d", float(actual)) != struct.pack(">d", float(expected))
            for actual, expected in zip(values, expected_values, strict=True)
        ):
            raise QuPathNativeTrainingExchangeError("measurement vector differs from live evidence")
        _sha256(row["measurement_vector_sha256"], "measurement vector digest")
        if row["measurement_vector_sha256"] != _canonical_digest(values):
            raise QuPathNativeTrainingExchangeError("measurement vector digest is invalid")
    return frozenset(observed_classes)


def _validate_training_entries(
    value: object,
    *,
    native_type: str,
    feature_spec: Mapping[str, object],
    required_classes: tuple[str, ...],
    material_parent_type_ids: tuple[str, ...],
    bindings: tuple[Mapping[str, object], ...],
    feature_source=None,
) -> tuple[str, ...]:
    """Close each entry over its declared family, material scope and bindings."""
    entries = _array(value, "training_entries", nonempty=True)
    entry_ids: list[str] = []
    object_keys: list[tuple[str, str]] = []
    object_binding_digests: list[str] = []
    observed_object_classes: set[str] = set()
    for ordinal, raw in enumerate(entries):
        if native_type == "pixel":
            entry = _closed_mapping(
                raw,
                frozenset({"entry_kind", "entry_state", "evidence_rows", "channel_features"}),
                f"pixel training entry {ordinal}",
            )
            _literal(entry["entry_kind"], "pixel", "entry kind")
            if entry["channel_features"] != feature_spec:
                raise QuPathNativeTrainingExchangeError("entry channel features differ")
            rows = _array(entry["evidence_rows"], "pixel evidence rows", nonempty=True)
            state = _closed_mapping(
                entry["entry_state"], _ENTRY_STATE_FIELDS, "training entry state"
            )
            entry_id = _text(state["project_entry_id"], "project entry ID")
            entry_bindings = tuple(
                item for item in bindings if item["project_entry_id"] == entry_id
            )
            validated_rows = [
                _validate_pixel_row(
                    item,
                    entry_id=entry_id,
                    required_classes=required_classes,
                    material_parent_type_ids=material_parent_type_ids,
                    bindings=entry_bindings,
                )
                for item in rows
            ]
            row_keys = tuple(
                (item["annotation_uuid"], item["evidence_sha256"]) for item in validated_rows
            )
            if row_keys != tuple(sorted(row_keys)) or len({item[0] for item in row_keys}) != len(
                row_keys
            ):
                raise QuPathNativeTrainingExchangeError("pixel evidence rows are not canonical")
            _validate_entry_state(state, rows=rows, bindings=bindings)
        else:
            entry = _closed_mapping(
                raw,
                frozenset({"entry_kind", "snapshot", "training_cells"}),
                f"object training entry {ordinal}",
            )
            _literal(entry["entry_kind"], "object", "entry kind")
            snapshot = _closed_mapping(
                entry["snapshot"], _OBJECT_SNAPSHOT_FIELDS |
                ({"deferred_feature_evidence"} if isinstance(entry["snapshot"], Mapping)
                 and "deferred_feature_evidence" in entry["snapshot"] else set()), "object fit snapshot"
            )
            entry_id = _text(snapshot["project_entry_id"], "project entry ID")
            object_keys.append((entry_id, _text(snapshot["section_id"], "section ID")))
            observed_object_classes.update(
                _validate_object_entry(
                    entry,
                    snapshot=snapshot,
                    feature_spec=feature_spec,
                    required_classes=required_classes,
                    bindings=bindings,
                    feature_source=feature_source,
                )
            )
            object_binding_digests.extend(snapshot["analysis_domain_binding_sha256s"])
        entry_ids.append(entry_id)
    keys = object_keys if native_type == "object" else entry_ids
    if keys != sorted(keys) or len(set(keys)) != len(keys):
        raise QuPathNativeTrainingExchangeError("training entries are not canonical and unique")
    if native_type == "object" and sorted(object_binding_digests) != sorted(
        _canonical_digest(item) for item in bindings
    ):
        raise QuPathNativeTrainingExchangeError("object sections lack exact pooled domain bindings")
    if native_type == "object" and observed_object_classes != set(required_classes):
        raise QuPathNativeTrainingExchangeError("pooled training cells lack a required class")
    return tuple(sorted(set(entry_ids)))


def _expected_runtime_exchange_ref(
    *,
    authority_layout_sha256: str,
    scope: Mapping[str, object],
    direction: str,
) -> AuthorityMemberRefV2:
    key_sha256 = _canonical_digest(
        {
            "authority_layout_sha256": authority_layout_sha256,
            "family": "runtime_exchange",
            "scope": scope,
            "natural_key": {"direction": direction},
        }
    )
    try:
        return AuthorityMemberRefV2(
            authority_layout_sha256=authority_layout_sha256,
            family="runtime_exchange",
            key_sha256=key_sha256,
            suffix="json",
            media_type="application/json",
        )
    except AuthorityLayoutError as exc:  # pragma: no cover - closed constants
        raise QuPathNativeTrainingExchangeError("runtime exchange ref is invalid") from exc


def _member_ref(
    value: object, *, layout: str, scope: Mapping[str, object], direction: str
) -> AuthorityMemberRefV2:
    try:
        parsed = AuthorityMemberRefV2.from_mapping(value, expected_layout_sha256=layout)
    except (AuthorityLayoutError, TypeError, ValueError) as exc:
        raise QuPathNativeTrainingExchangeError("runtime exchange member ref is invalid") from exc
    if parsed != _expected_runtime_exchange_ref(
        authority_layout_sha256=layout, scope=scope, direction=direction
    ):
        raise QuPathNativeTrainingExchangeError("runtime exchange member ref is foreign")
    return parsed


def _validate_material_parent_scope(
    request: Mapping[str, object], *, native_type: str
) -> tuple[str, ...]:
    """Validate role and material authority without inferring role from names."""
    role = _text(request["classifier_role"], "classifier role")
    parents = _ordered_texts(
        request["eligible_parent_classes"], "eligible_parent_classes", sorted_order=True
    )
    if native_type == "object":
        if (
            role != "supervised_phenotype"
            or parents != ("Cell",)
            or _array(request["eligible_material_parent_type_ids"], "material parent type IDs")
        ):
            raise QuPathNativeTrainingExchangeError("object training role or material scope differs")
        return ()
    if native_type != "pixel" or role not in {"primary_compartment", "secondary_structure"}:
        raise QuPathNativeTrainingExchangeError("pixel training classifier role is invalid")
    material = _ordered_texts(
        request["eligible_material_parent_type_ids"],
        "eligible_material_parent_type_ids",
        sorted_order=True,
    )
    expected = ("AnalysisDomain",) if role == "primary_compartment" else parents
    if material != expected or (role == "primary_compartment" and parents != ("Tissue",)):
        raise QuPathNativeTrainingExchangeError("pixel training role or material scope differs")
    return material


def _validate_request_mapping(
    value: object,
) -> tuple[Mapping[str, object], AuthorityMemberRefV2, AuthorityMemberRefV2]:
    """Close the current request over runtime identity and declared fit scope.

    The authenticated role determines material eligibility before any entry is
    accepted; family, numerical configuration and request digests are checked
    independently. Missing current fields are rejected.
    """

    request = _closed_mapping(value, _REQUEST_FIELDS, "native-training request")
    _literal(request["schema_name"], "mif-qupath-native-training-request", "request schema")
    _literal(request["schema_version"], 1, "request schema version")
    digest_fields = (
        "authority_layout_sha256",
        "workspace_storage_scope_sha256",
        "active_mutation_run_sha256",
        "project_identity_sha256",
        "acquisition_record_sha256",
        "acquisition_session_challenge_sha256",
        "artifact_inventory_sha256",
        "mutation_request_sha256",
        "mutation_authorization_sha256",
        "fencing_token_sha256",
        "fit_plan_sha256",
        "fit_execution_sha256",
        "training_material_evidence_sha256",
        "model_configuration_sha256",
        "script_sha256",
        "launch_nonce_sha256",
        "request_envelope_sha256",
    )
    for name in digest_fields:
        _sha256(request[name], name)
    _text(request["classifier_id"], "classifier ID")
    if request["classifier_kind"] not in {"pixel", "supervised_phenotype"}:
        raise QuPathNativeTrainingExchangeError("classifier kind is invalid")
    _literal(request["classifier_engine"], "qupath_native", "classifier engine")
    native_type = request["native_classifier_type"]
    expected_native = "object" if request["classifier_kind"] == "supervised_phenotype" else "pixel"
    _literal(native_type, expected_native, "native classifier type")
    fit_execution = request["fit_execution_sha256"]
    version = f"mif-fit-{fit_execution}"
    _literal(request["classifier_version"], version, "classifier version")
    expected_path = (
        f"classifiers/object_classifiers/mif-{fit_execution}.json"
        if native_type == "object"
        else f"classifiers/pixel_classifiers/mif-{fit_execution}.json"
    )
    _literal(request["project_relative_path"], expected_path, "classifier target")
    path = PurePosixPath(expected_path)
    if path.is_absolute() or len(path.parts) != 3 or path.suffix != ".json":
        raise QuPathNativeTrainingExchangeError("classifier target is invalid")
    _literal(request["resource_name"], path.stem, "resource name")
    required = _ordered_texts(request["required_classes"], "required_classes", sorted_order=True)
    material_parent_type_ids = _validate_material_parent_scope(request, native_type=native_type)
    bindings = tuple(
        _validate_domain_binding(item)
        for item in _array(
            request["training_analysis_domain_bindings"],
            "training analysis-domain bindings",
            nonempty=True,
        )
    )
    if bindings != tuple(sorted(bindings, key=_binding_sort_key)) or len(
        {_canonical_digest(item) for item in bindings}
    ) != len(bindings):
        raise QuPathNativeTrainingExchangeError("analysis-domain bindings are not canonical")
    feature_spec = _validate_feature_spec(request["feature_spec"], native_type)
    _sha256(request["feature_spec_sha256"], "feature specification digest")
    if request["feature_spec_sha256"] != _canonical_digest(feature_spec):
        raise QuPathNativeTrainingExchangeError("feature specification digest differs")
    _validate_model_configuration(
        request["model_configuration"],
        native_type=native_type,
        required_classes=required,
    )
    full = request["model_configuration"]
    candidates = (
        full["candidates"]
        if full.get("configuration_schema") == "qupath-grouped-cross-validation/v1"
        else (full,)
    )
    for configuration in candidates:
        if configuration.get("configuration_schema") in {
            "qupath-kronos-rtrees/v2",
            "qupath-kronos-svm/v2",
        }:
            ordered = tuple(row["canonical_channel_id"] for row in feature_spec["ordered_channels"])
            if tuple(configuration["encoder"]["channel_ids"]) != ordered:
                raise QuPathNativeTrainingExchangeError(
                    "KRONOS channel order differs from the native feature observation"
                )
            if configuration["graph"]["random_seed"] != request["random_seed"]:
                raise QuPathNativeTrainingExchangeError(
                    "The graph training seed differs from the accepted model seed"
                )
    if request["model_configuration_sha256"] != _canonical_digest(request["model_configuration"]):
        raise QuPathNativeTrainingExchangeError("model configuration digest is invalid")
    if native_type == "object" and (
        feature_spec.get("pca_normalize", True) != candidates[0].get("pca_normalize", True)
        or feature_spec["normalization"] != candidates[0]["normalization"]  # type: ignore[index]
        or _canonical_digest(feature_spec["pca_retained_variance"])
        != _canonical_digest(
            candidates[0]["pca_retained_variance"]  # type: ignore[index]
        )
    ):
        raise QuPathNativeTrainingExchangeError("object feature preprocessing differs")
    entry_ids = _validate_training_entries(
        request["training_entries"],
        feature_source=candidates[0].get("feature_source") if native_type == "object" else None,
        native_type=native_type,
        feature_spec=feature_spec,
        required_classes=required,
        material_parent_type_ids=material_parent_type_ids,
        bindings=bindings,
    )
    if {item["project_entry_id"] for item in bindings} != set(entry_ids):
        raise QuPathNativeTrainingExchangeError("domain bindings lack exact entry coverage")
    _integer(request["random_seed"], "random seed")
    _literal(request["expected_qupath_version"], "0.7.0", "expected QuPath version")
    _literal(
        request["script_asset_id"],
        "qupath/scripts/general/02_train_native_classifier.groovy",
        "training script asset",
    )
    core = {
        key: request[key]
        for key in request
        if key not in {"request_member_ref", "result_member_ref", "request_envelope_sha256"}
    }
    request_identity = _canonical_digest(core)
    scope = {
        "workspace_storage_scope_sha256": request["workspace_storage_scope_sha256"],
        "active_mutation_run_sha256": request["active_mutation_run_sha256"],
        "role": "classifier_training",
        "request_identity_sha256": request_identity,
    }
    request_ref = _member_ref(
        request["request_member_ref"],
        layout=request["authority_layout_sha256"],  # type: ignore[arg-type]
        scope=scope,
        direction="request",
    )
    result_ref = _member_ref(
        request["result_member_ref"],
        layout=request["authority_layout_sha256"],  # type: ignore[arg-type]
        scope=scope,
        direction="result",
    )
    if request_ref == result_ref:
        raise QuPathNativeTrainingExchangeError("request and result refs must differ")
    unsigned = {key: request[key] for key in request if key != "request_envelope_sha256"}
    if request["request_envelope_sha256"] != _canonical_digest(unsigned):
        raise QuPathNativeTrainingExchangeError("request envelope digest is invalid")
    return request, request_ref, result_ref


class QuPathNativeTrainingRequest:
    """Deeply immutable validated native-training request bytes."""

    __slots__ = ("_mapping", "_request_member_ref", "_result_member_ref")

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("native-training requests are parser/issuer-created")

    @classmethod
    def _from_mapping(cls, value: object) -> "QuPathNativeTrainingRequest":
        mapping, request_ref, result_ref = _validate_request_mapping(value)
        _json_shape(mapping)
        try:
            encoded = canonical_json_bytes(mapping)
        except DurableRecordError as exc:  # pragma: no cover - validator invariant
            raise QuPathNativeTrainingExchangeError("request cannot be encoded") from exc
        if len(encoded) > _MAX_EXCHANGE_BYTES:
            raise QuPathNativeTrainingExchangeError("request exceeds its byte bound")
        instance = object.__new__(cls)
        object.__setattr__(instance, "_mapping", _freeze_json(dict(mapping)))
        object.__setattr__(instance, "_request_member_ref", request_ref)
        object.__setattr__(instance, "_result_member_ref", result_ref)
        return instance

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("native-training request is immutable")

    def __getattr__(self, name: str) -> object:
        if name in _REQUEST_FIELDS:
            if name == "request_member_ref":
                return self._request_member_ref
            if name == "result_member_ref":
                return self._result_member_ref
            return self._mapping[name]
        raise AttributeError(name)

    def __eq__(self, other: object) -> bool:
        return (
            type(other) is QuPathNativeTrainingRequest and self.to_mapping() == other.to_mapping()
        )

    @property
    def request_identity_sha256(self) -> str:
        """Return the codec-owned identity that scopes this request's refs."""

        return _canonical_digest(
            {key: _export_json(self._mapping[key]) for key in _REQUEST_CORE_FIELDS}
        )

    def to_mapping(self) -> dict[str, object]:
        return _export_json(self._mapping)

    def to_bytes(self) -> bytes:
        try:
            payload = canonical_json_bytes(self.to_mapping())
        except DurableRecordError as exc:  # pragma: no cover - construction invariant
            raise QuPathNativeTrainingExchangeError("request cannot be encoded") from exc
        if len(payload) > _MAX_EXCHANGE_BYTES:
            raise QuPathNativeTrainingExchangeError("request exceeds its byte bound")
        return payload


def decode_qupath_native_training_request(payload: bytes) -> QuPathNativeTrainingRequest:
    return QuPathNativeTrainingRequest._from_mapping(
        _decode_canonical(payload, "native-training request")
    )


def _issue_qupath_native_training_request(
    *,
    core_fields: Mapping[str, object],
    member_ref_issuer: Callable[..., AuthorityMemberRefV2],
) -> QuPathNativeTrainingRequest:
    core = _closed_mapping(core_fields, _REQUEST_CORE_FIELDS, "native-training request core")
    _json_shape(core)
    request_identity = _canonical_digest(core)
    scope = {
        "workspace_storage_scope_sha256": core["workspace_storage_scope_sha256"],
        "active_mutation_run_sha256": core["active_mutation_run_sha256"],
        "role": "classifier_training",
        "request_identity_sha256": request_identity,
    }
    if not callable(member_ref_issuer):
        raise QuPathNativeTrainingExchangeError("member-ref issuer is unavailable")
    try:
        request_ref = member_ref_issuer(
            family="runtime_exchange",
            scope=scope,
            natural_key={"direction": "request"},
        )
        result_ref = member_ref_issuer(
            family="runtime_exchange",
            scope=scope,
            natural_key={"direction": "result"},
        )
    except Exception as exc:
        raise QuPathNativeTrainingExchangeError("member-ref issuance failed") from exc
    if (
        type(request_ref) is not AuthorityMemberRefV2
        or type(result_ref) is not AuthorityMemberRefV2
    ):
        raise QuPathNativeTrainingExchangeError("member-ref issuer returned a foreign value")
    unsigned = {
        **dict(core),
        "request_member_ref": request_ref.to_mapping(),
        "result_member_ref": result_ref.to_mapping(),
    }
    return QuPathNativeTrainingRequest._from_mapping(
        {**unsigned, "request_envelope_sha256": _canonical_digest(unsigned)}
    )
