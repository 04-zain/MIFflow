"""Identity, persistence, and request composition shared by preprocessing jobs.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version as distribution_version
import hashlib
import json
import math
from pathlib import Path

from ome_types import OME
from mif_pipeline.io import InspectedSourceAuthority
from mif_pipeline.preprocessing.unmixing.validation import (
    AfBehavior,
    SolverKind,
    SpectralState,
    UnmixingConfig,
)

from .batch_artifacts import (
    ArtifactConfigurationBinding,
    ArtifactOrigin,
    ArtifactScope,
    BatchArtifactRef,
    BatchNodeExecutionContext,
)
from .batch_dag import BatchNode
from .batch_manifest import ResolvedBatchSample
from .batch_preprocessing_contracts import (
    OUTPUT_BY_NODE,
    REQUIRED_INPUTS,
    SCHEMA_BY_ARTIFACT,
    preprocessing_identity_contract,
    reusable_input_identity_contract,
)
from .batch_preprocessing_wire import (
    PREPROCESSING_JOB_REQUEST_SCHEMA_NAME,
    PREPROCESSING_JOB_SCHEMA_VERSION,
    PreprocessingJobRequest,
    canonical_bytes,
    canonical_digest,
    decode_json,
    require_sha256,
    require_text,
)
from .reference_unmixing import ReferenceUnmixingRequest

REFERENCE_UNMIXING_CALIBRATION_RESPONSE_VERSION = "mif-batch-reference-unmixing-start/v4"
REFERENCE_UNMIXING_START_RESPONSE_VERSION = "mif-batch-reference-unmixing-start/v3"


# ==============================================================================
#                          Batch preprocessing support
# ==============================================================================


def unmixing_start_defaults() -> dict[str, object]:
    """Single owner-defined numerical defaults; no biological identities inferred."""
    c = UnmixingConfig(solver=SolverKind.NNLS)
    from mif_pipeline._compute_preferences import preferred_precision
    import os
    precision = preferred_precision() or ("float32" if os.environ.get("MIF_COMPUTE_DEVICE") == "mps" else "float64")
    return {
        "solver": c.solver.value,
        "spectral_state_override": None,
        "af_behavior": c.af_behavior.value,
        "chunk_size": list(c.chunk_size),
        "output_dtype": c.output_dtype,
        "calculation_precision": precision,
        "nnls_tolerance": c.nnls_tolerance,
        "nnls_max_iterations": c.nnls_max_iterations,
        "overwrite": c.overwrite,
    }


def unmixing_af_behavior(ome: OME | None) -> str:
    """Omit AF only when the image metadata records prior AF subtraction."""
    from mif_pipeline.io.metadata.calibration import recorded_autofluorescence_subtracted

    return ("already_removed" if ome is not None
            and recorded_autofluorescence_subtracted(ome) is True else "retain")


def unmixing_source_state(ome: OME | None) -> tuple[str, str]:
    """Read the selected image's spectral declarations for the preparation form.

    We compare the processing annotation, completed acquisition record and retained
    acquisition inventory. Agreement identifies raw measurements or
    unmixed components. An image with no recorded declaration returns ``unknown``
    so the researcher can select its preparation route. Exposure and detector-range
    completion belong to the acquisition form shown for the applicable route.

    Returns:
        The spectral state and the metadata source supplying it.

    Raises:
        ValueError: Recorded declarations identify conflicting processing states.
    """
    if ome is None:
        return "unknown", "awaiting image-state review"
    from mif_pipeline.io import (
        MetadataConflictError, resolve_spectral_state, resolve_unmixing_input_metadata,
    )
    from mif_pipeline.io.metadata.spectral import _resolve_source_spectral_state

    spectral = resolve_spectral_state(ome, 0)
    acquisition = resolve_unmixing_input_metadata(ome, 0)
    try:
        state, source = _resolve_source_spectral_state(
            ome, 0, spectral=spectral, acquisition=acquisition
        )
    except MetadataConflictError as exc:
        raise ValueError(str(exc)) from exc
    return (state, source) if state != "unknown" else (state, "awaiting image-state review")


def reference_unmixing_start_response_to_request(
    response: Mapping[str, object],
    *,
    run_id: str,
    authority: InspectedSourceAuthority,
    reviewed_ome: OME,
    output_path: Path,
    accepted_name_choice: bool | None = None,
) -> ReferenceUnmixingRequest:
    """Owner-validate explicit scientific choices and derive operational paths."""

    if not isinstance(response, Mapping):
        raise TypeError("reference-unmixing start response must be a mapping")
    expected = {
        "schema_version",
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
    version = response.get("schema_version")
    if version in {
        REFERENCE_UNMIXING_START_RESPONSE_VERSION,
        REFERENCE_UNMIXING_CALIBRATION_RESPONSE_VERSION,
    }:
        expected.remove("replace_channel_names_with_biomarkers")
        if type(accepted_name_choice) is not bool:
            raise ValueError("The accepted Channels and markers naming decision is required")
    else:
        raise ValueError("reference-unmixing start response schema is unsupported")
    if version == REFERENCE_UNMIXING_CALIBRATION_RESPONSE_VERSION:
        expected.add("calibration")
    if not expected <= set(response) <= expected | {"calculation_precision", "measurement_band_components"}:
        raise ValueError("reference-unmixing start response fields differ")
    observed, _source = unmixing_source_state(reviewed_ome)
    chosen = response["spectral_state_override"]
    if chosen == "abort":
        raise ValueError(
            "Unmixing was cancelled; use Save and pause to leave this decision pending"
        )
    if observed != "unknown" and chosen not in {None, observed, "isolated"}:
        raise ValueError("The selected spectral state conflicts with the recorded image metadata")
    if (
        version == REFERENCE_UNMIXING_START_RESPONSE_VERSION
        and observed == "unknown"
        and chosen not in {"raw", "unmixed", "isolated"}
    ):
        raise ValueError("Choose whether this image still needs spectral unmixing")
    chunk = response["chunk_size"]
    if type(chunk) is not list or len(chunk) != 2 or any(type(item) is not int for item in chunk):
        raise ValueError("reference-unmixing chunk size is invalid")
    tolerance = response["nnls_tolerance"]
    if type(tolerance) not in {int, float} or not math.isfinite(float(tolerance)):
        raise ValueError("reference-unmixing tolerance is invalid")
    mapping = response.get("measurement_band_components", [])
    if type(mapping) is not list:
        raise ValueError("Measurement-band mapping must be a list of component row indices")
    if chosen == "isolated":
        from mif_pipeline.io.metadata.measurement_band_copy import prepare_measurement_band_copy
        # Validate without changing the reviewed source binding retained by the request.
        prepare_measurement_band_copy(reviewed_ome, tuple(mapping))
    config = UnmixingConfig(
        solver=SolverKind(response["solver"]),
        spectral_state_override=(
            None
            if response["spectral_state_override"] is None
            else SpectralState(response["spectral_state_override"])
        ),
        af_behavior=AfBehavior(unmixing_af_behavior(reviewed_ome)),
        chunk_size=tuple(chunk),
        output_dtype=response["output_dtype"],
        calculation_precision=response.get("calculation_precision"),
        nnls_tolerance=float(tolerance),
        nnls_max_iterations=response["nnls_max_iterations"],
        overwrite=response["overwrite"],
        measurement_band_components=tuple(mapping),
    )
    if version == REFERENCE_UNMIXING_CALIBRATION_RESPONSE_VERSION:
        calibration = response["calibration"]
        if chosen in {"unmixed", "isolated"}:
            if calibration is not None:
                raise ValueError("Images copied without spectral fitting do not use raw calibration entry")
        else:
            if not isinstance(calibration, Mapping) or set(calibration) != {
                "values",
                "source_note",
                "correction_mode",
            }:
                raise ValueError("Complete the missing imaging settings and their source")
            import getpass
            from mif_pipeline.io.metadata.calibration import complete_calibration

            reviewed_ome = complete_calibration(
                reviewed_ome,
                calibration["values"],
                source_note=calibration["source_note"],
                reviewer="local-account:" + getpass.getuser(),
                spectral_state=chosen,
                correction_mode=calibration["correction_mode"],
            )
    return ReferenceUnmixingRequest.from_reviewed_source(
        run_id=require_text(run_id, "run_id"),
        authority=authority,
        reviewed_ome=reviewed_ome,
        output_path=output_path,
        config=config,
        replace_channel_names_with_biomarkers=(
            accepted_name_choice
            if version
            in {
                REFERENCE_UNMIXING_START_RESPONSE_VERSION,
                REFERENCE_UNMIXING_CALIBRATION_RESPONSE_VERSION,
            }
            else accepted_name_choice
        ),
    )


def package_version(name: str, fallback: str) -> str:
    try:
        return distribution_version(name)
    except PackageNotFoundError:
        return fallback


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def publish_exact(path: Path, payload: bytes) -> None:
    """Create immutable bytes or prove an existing resume artifact is exact."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise ValueError(f"existing preprocessing artifact differs: {path}")
        return
    path.write_bytes(payload)


def _resolved_sample_mapping(sample: ResolvedBatchSample, node_kind: str) -> dict[str, object]:
    if not isinstance(sample, ResolvedBatchSample):
        raise TypeError("preprocessing request requires a resolved batch sample")
    node_kind = require_text(node_kind, "node_kind")
    if node_kind not in OUTPUT_BY_NODE:
        raise ValueError("preprocessing sample projection has unsupported node kind")
    result = {
        "sample_id": sample.sample_id,
        "slide_id": sample.slide_id,
        "validation_domain_id": sample.validation_domain_id,
        "sample_output_root": str(sample.sample_output_root),
        "analysis_ready_output_path": str(sample.config.input_image),
        "resolved_config_sha256": sample.resolved_config_sha256,
        "source_image_index": sample.source_image_index,
        "source_series_index": sample.source_series_index,
        "source_file_sha256": sample.source_file_sha256,
    }
    if node_kind == "inspection":
        result["source_mif_path"] = str(sample.source_mif_path)
    return result


def build_preprocessing_job_request(
    node: BatchNode,
    context: BatchNodeExecutionContext,
    sample: ResolvedBatchSample,
    accepted_response: Mapping[str, object] | None,
) -> PreprocessingJobRequest:
    """Encode one exact authorized node into the bounded spawn wire."""

    if not isinstance(node, BatchNode) or not isinstance(context, BatchNodeExecutionContext):
        raise TypeError("preprocessing request requires typed node and context")
    accepted_config_hashes = {item.resolved_config_sha256 for item in node.configuration_bindings}
    if (
        node.node_id != context.node_id
        or node.material_sha256 != context.authorization.node_material_sha256
        or node.adapter_spec_sha256 != context.adapter_spec.spec_sha256
        or node.kind not in OUTPUT_BY_NODE
        or node.participant_ids != (sample.sample_id,)
        or sample.resolved_config_sha256 not in accepted_config_hashes
    ):
        raise ValueError("preprocessing request material is foreign or stale")
    response_bytes = None if accepted_response is None else canonical_bytes(accepted_response)
    return PreprocessingJobRequest(
        schema_name=PREPROCESSING_JOB_REQUEST_SCHEMA_NAME,
        schema_version=PREPROCESSING_JOB_SCHEMA_VERSION,
        node_id=node.node_id,
        node_kind=node.kind,
        node_version=node.version,
        node_material_sha256=node.material_sha256,
        adapter_spec_sha256=context.adapter_spec.spec_sha256,
        authorization_sha256=context.authorization.authorization_sha256,
        resolved_config_sha256=sample.resolved_config_sha256,
        node_json=canonical_bytes(node.to_mapping()),
        authorization_json=canonical_bytes(context.authorization.to_mapping()),
        resolved_sample_json=canonical_bytes(_resolved_sample_mapping(sample, node.kind)),
        input_artifact_refs_json=canonical_bytes([item.to_mapping() for item in context.inputs]),
        accepted_interaction_response_json=response_bytes,
    )


def parse_preprocessing_job_request(
    request: PreprocessingJobRequest,
) -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
    tuple[BatchArtifactRef, ...],
    Mapping[str, object] | None,
]:
    if not isinstance(request, PreprocessingJobRequest):
        raise TypeError("preprocessing worker requires exact request DTO")
    node = decode_json(request.node_json, "node_json")
    authorization = decode_json(request.authorization_json, "authorization_json")
    sample = decode_json(request.resolved_sample_json, "resolved_sample_json")
    raw_inputs = decode_json(request.input_artifact_refs_json, "input_artifact_refs_json")
    response = (
        None
        if request.accepted_interaction_response_json is None
        else decode_json(
            request.accepted_interaction_response_json,
            "accepted_interaction_response_json",
        )
    )
    if not all(isinstance(item, dict) for item in (node, authorization, sample)):
        raise ValueError("preprocessing request envelopes must be JSON objects")
    if (
        node.get("node_id") != request.node_id
        or node.get("kind") != request.node_kind
        or node.get("version") != request.node_version
        or node.get("material_sha256") != request.node_material_sha256
        or node.get("adapter_spec_sha256") != request.adapter_spec_sha256
        or authorization.get("node_id") != request.node_id
        or authorization.get("node_material_sha256") != request.node_material_sha256
        or authorization.get("authorization_sha256") != request.authorization_sha256
        or sample.get("resolved_config_sha256") != request.resolved_config_sha256
    ):
        raise ValueError("preprocessing wire identity is inconsistent")
    if not isinstance(raw_inputs, list):
        raise ValueError("preprocessing input refs must be a JSON array")
    inputs: list[BatchArtifactRef] = []
    for item in raw_inputs:
        if not isinstance(item, Mapping):
            raise ValueError("preprocessing input artifact is invalid")
        kind = item.get("artifact_kind")
        if kind in SCHEMA_BY_ARTIFACT:
            contract = preprocessing_identity_contract(kind)
        elif kind in {"reviewed_panel_mapping", "spectral_reference_library"}:
            contract = reusable_input_identity_contract(kind)
        else:
            raise ValueError("preprocessing input artifact kind is unsupported")
        inputs.append(
            BatchArtifactRef.from_mapping(
                item,
                identity_contract=contract,
            )
        )
    if response is not None and not isinstance(response, Mapping):
        raise ValueError("accepted preprocessing response must be an object")
    return node, authorization, sample, tuple(inputs), response


def response_body(
    value: Mapping[str, object] | None,
) -> Mapping[str, object] | None:
    if value is None:
        return None
    if "response" in value:
        if value.get("action") != "accept":
            raise ValueError("preprocessing interaction action must be accept")
        body = value["response"]
        if not isinstance(body, Mapping):
            raise ValueError("accepted preprocessing response body is invalid")
        return body
    return value


def artifact_path(sample: Mapping[str, object], relative: str) -> Path:
    root = Path(require_text(sample.get("sample_output_root"), "sample_output_root"))
    if not root.is_absolute():
        raise ValueError("sample output root must be absolute")
    path = (root / "preprocessing" / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("preprocessing artifact escapes sample output root") from exc
    return path


def source_path(sample: Mapping[str, object]) -> Path:
    path = Path(require_text(sample.get("source_mif_path"), "source_mif_path"))
    if not path.is_absolute():
        raise ValueError("source image path must be absolute")
    return path


def _scope_and_binding(
    sample: Mapping[str, object],
) -> tuple[ArtifactScope, tuple[ArtifactConfigurationBinding, ...]]:
    sample_id = require_text(sample.get("sample_id"), "sample_id")
    slide_id = require_text(sample.get("slide_id"), "slide_id")
    domain = require_text(sample.get("validation_domain_id"), "validation_domain_id")
    resolved = require_sha256(sample.get("resolved_config_sha256"), "resolved_config_sha256")
    return (
        ArtifactScope(domain, sample_id, slide_id, None, ()),
        (ArtifactConfigurationBinding(sample_id, resolved),),
    )


def runtime_versions(**additional: str) -> dict[str, str]:
    result = {"mif_pipeline": package_version("mif-pipeline", "0.1.0")}
    result.update({key: require_text(value, key) for key, value in additional.items()})
    return result


def artifact_ref(
    *,
    request: PreprocessingJobRequest,
    authorization: Mapping[str, object],
    sample: Mapping[str, object],
    artifact_kind: str,
    locator: Path,
    file_sha256_value: str,
    byte_count: int,
    owning_identity: Mapping[str, object],
    runtime_versions_value: Mapping[str, str],
    compatibility_material: object,
) -> BatchArtifactRef:
    output_kind, schema_name = OUTPUT_BY_NODE[request.node_kind]
    if artifact_kind != output_kind:
        raise ValueError("worker output kind differs from node declaration")
    scope, bindings = _scope_and_binding(sample)
    contract = preprocessing_identity_contract(artifact_kind)
    return BatchArtifactRef(
        artifact_id="artifact-"
        + canonical_digest({"node": request.node_id, "kind": artifact_kind})[:48],
        artifact_kind=artifact_kind,
        schema_name=schema_name,
        schema_version=contract.schema_version,
        origin=ArtifactOrigin.INTERNAL_NODE,
        producer_node_id=request.node_id,
        producer_attempt_id=require_text(authorization.get("attempt_id"), "attempt_id"),
        producer_attempt_authorization_sha256=request.authorization_sha256,
        external_declaration_sha256=None,
        external_validation_receipt_sha256=None,
        locator=locator,
        sha256=require_sha256(file_sha256_value, "artifact file SHA-256"),
        byte_count=byte_count,
        scope=scope,
        owning_identity=owning_identity,
        identity_contract=contract,
        owning_identity_contract_sha256=contract.contract_sha256,
        producer_version="mif-pipeline-batch-preprocessing/v1",
        runtime_versions=runtime_versions_value,
        compatibility_signature_sha256=canonical_digest(compatibility_material),
        configuration_bindings=bindings,
    )


def equivalence(
    *,
    canonical: object,
    inventory: object,
    scientific: object,
    qc: object,
    configuration_sha256: str,
    runtime_versions_value: Mapping[str, str],
) -> dict[str, str]:
    configuration = require_sha256(configuration_sha256, "equivalence configuration SHA-256")
    if not isinstance(runtime_versions_value, Mapping) or not runtime_versions_value:
        raise ValueError("equivalence runtime versions must be a non-empty mapping")
    versions = {
        require_text(name, "runtime name"): require_text(value, "runtime version")
        for name, value in sorted(runtime_versions_value.items())
    }
    return {
        "canonical_identity_sha256": canonical_digest(canonical),
        "stable_inventory_sha256": canonical_digest(inventory),
        "scientific_values_sha256": canonical_digest(
            {
                "configuration_sha256": configuration,
                "runtime_versions": versions,
                "scientific_values": scientific,
            }
        ),
        "qc_evidence_sha256": canonical_digest(qc),
    }


__all__ = [
    "REFERENCE_UNMIXING_START_RESPONSE_VERSION",
    "artifact_path",
    "artifact_ref",
    "build_preprocessing_job_request",
    "equivalence",
    "file_sha256",
    "package_version",
    "parse_preprocessing_job_request",
    "publish_exact",
    "reference_unmixing_start_response_to_request",
    "response_body",
    "runtime_versions",
    "source_path",
]
