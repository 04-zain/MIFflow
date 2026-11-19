"""Closed, locatorless composite evidence for batch analysis preflight.

The `BatchAnalysisPreflightEvidence` record carries `spatial_frame`, `channels`,
`ordered_channel_ids`, `channel_inventory_sha256`, `channel_semantics_sha256` and its
remaining declared fields.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Final

from mif_pipeline.cell_analysis import CanonicalSpatialFrameEvidence
from mif_pipeline.orchestration.input_validation import (
    InputValidationReport,
    analysis_channel_specs,
)
from mif_pipeline.pipeline_core.config import AnalysisConfig, ChannelSpec
from mif_pipeline.pipeline_core.models import ROIAnalysisError
from mif_pipeline.qupath_runtime.runner import QuPathImagePreflight

from .batch_artifacts import (
    ArtifactIdentityContract,
    ArtifactOrigin,
    ArtifactScope,
    BatchArtifactError,
    BatchArtifactRef,
    BatchNodeExecutionContext,
    validate_artifact_ref,
)
from .batch_preprocessing_contracts import preprocessing_identity_contract

MAX_ANALYSIS_PREFLIGHT_CHANNELS: Final[int] = 1024
MIF_PIPELINE_BATCH_RUNTIME_VERSION: Final[str] = "0.1.0"
_SCHEMA_VERSION: Final[str] = "mif-batch-analysis-preflight/v2"
_SCHEMA_NAME: Final[str] = "mif-batch-analysis-preflight"
_PRODUCER_VERSION: Final[str] = "mif-pipeline-batch-analysis-preflight/v2"
_QUPATH_PROBE_SCHEMA_VERSION: Final[str] = "qupath-image-openability-probe/v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

ANALYSIS_PREFLIGHT_IDENTITY_FIELDS: tuple[str, ...] = (
    "schema_version",
    "spatial_frame.schema_version",
    "spatial_frame.canonical_image_id",
    "spatial_frame.canonical_image_sha256",
    "spatial_frame.series_index",
    "spatial_frame.coordinate_system",
    "spatial_frame.level0_size_x_px",
    "spatial_frame.level0_size_y_px",
    "spatial_frame.calibration_status",
    "spatial_frame.physical_size_unit",
    "spatial_frame.x_um_per_px",
    "spatial_frame.y_um_per_px",
    "spatial_frame.spatial_frame_sha256",
    "ordered_channel_ids",
    "channels.channel_id",
    "channels.name",
    "channels.component",
    "channels.fluorophore",
    "channels.biomarker",
    "channel_inventory_sha256",
    "channel_semantics_sha256",
    "qupath_probe.executable_version",
    "qupath_probe.exit_status",
    "qupath_probe.qupath_probe_sha256",
    "analysis_preflight_sha256",
)


# ==============================================================================
#                            Batch analysis preflight
# ==============================================================================


class BatchAnalysisPreflightError(ValueError):
    """Raised when composite analysis-preflight evidence is not exact."""


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise BatchAnalysisPreflightError("value is not finite canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value.strip() or value != value.strip():
        raise BatchAnalysisPreflightError(f"{field} must be nonblank canonical text")
    return value


def _sha256(value: object, field: str) -> str:
    digest = _text(value, field)
    if _SHA256.fullmatch(digest) is None:
        raise BatchAnalysisPreflightError(f"{field} must be a lowercase SHA-256")
    return digest


def _closed_mapping(value: object, fields: frozenset[str], field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(type(key) is str for key in value):
        raise BatchAnalysisPreflightError(f"{field} must be a string-keyed object")
    unknown = sorted(set(value) - fields)
    missing = sorted(fields - set(value))
    if unknown:
        raise BatchAnalysisPreflightError(f"{field} has unknown field(s): {', '.join(unknown)}")
    if missing:
        raise BatchAnalysisPreflightError(f"{field} lacks required field(s): {', '.join(missing)}")
    return value


def _channel_ids(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise BatchAnalysisPreflightError("ordered_channel_ids must be an array")
    channel_ids = tuple(_text(item, "ordered_channel_ids item") for item in value)
    if not channel_ids:
        raise BatchAnalysisPreflightError("ordered_channel_ids must be nonempty")
    if len(channel_ids) > MAX_ANALYSIS_PREFLIGHT_CHANNELS:
        raise BatchAnalysisPreflightError(
            "ordered_channel_ids exceeds the analysis-preflight channel bound"
        )
    if len(set(channel_ids)) != len(channel_ids):
        raise BatchAnalysisPreflightError("ordered_channel_ids must be unique")
    return channel_ids


def _channels(value: object) -> tuple[ChannelSpec, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise BatchAnalysisPreflightError("channels must be an array")
    if not 1 <= len(value) <= MAX_ANALYSIS_PREFLIGHT_CHANNELS:
        raise BatchAnalysisPreflightError(
            "channels exceed the analysis-preflight channel bound or are empty"
        )
    channels = []
    for index, item in enumerate(value):
        raw = item.to_mapping() if isinstance(item, ChannelSpec) else item
        _closed_mapping(
            raw,
            frozenset({"channel_id", "name", "component", "fluorophore", "biomarker"}),
            f"channels[{index}]",
        )
        try:
            spec = ChannelSpec.from_mapping(
                raw,
                field=f"channels[{index}]",
            )
        except (ROIAnalysisError, TypeError, ValueError) as exc:
            raise BatchAnalysisPreflightError(f"channels[{index}] is invalid") from exc
        if spec.to_mapping() != raw or spec.name is None or spec.component is None:
            raise BatchAnalysisPreflightError("channels require explicit name and component")
        _text(spec.channel_id, f"channels[{index}].channel_id")
        channels.append(spec)
    if len({item.channel_id for item in channels}) != len(channels):
        raise BatchAnalysisPreflightError("channels must have unique canonical IDs")
    return tuple(channels)


def _probe_mapping(executable_version: object) -> dict[str, object]:
    version = _text(executable_version, "qupath_probe.executable_version")
    unsigned = {
        "schema_version": _QUPATH_PROBE_SCHEMA_VERSION,
        "executable_version": version,
        "exit_status": 0,
    }
    return {
        "executable_version": version,
        "exit_status": 0,
        "qupath_probe_sha256": _digest(unsigned),
    }


@dataclass(frozen=True, slots=True)
class BatchAnalysisPreflightEvidence:
    """The deterministic, path-free projection of a successful image probe."""

    spatial_frame: CanonicalSpatialFrameEvidence
    channels: tuple[ChannelSpec, ...]
    ordered_channel_ids: tuple[str, ...]
    channel_inventory_sha256: str
    channel_semantics_sha256: str
    qupath_executable_version: str
    qupath_probe_sha256: str
    analysis_preflight_sha256: str
    schema_version: str = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        """Validate the BatchAnalysisPreflightEvidence fields when the record is constructed.

        The checks cover ``schema_version``, ``ordered_channel_ids``, ``channels``,
        ``channel_inventory_sha256``, ``channel_semantics_sha256``,
        ``qupath_executable_version``. Invalid field types, values or combinations raise the
        declared validation error before this instance can be used by its caller."""

        if self.schema_version != _SCHEMA_VERSION:
            raise BatchAnalysisPreflightError("analysis preflight schema_version is invalid")
        if not isinstance(self.spatial_frame, CanonicalSpatialFrameEvidence):
            raise BatchAnalysisPreflightError("spatial_frame must be canonical frame evidence")
        channel_ids = _channel_ids(self.ordered_channel_ids)
        channels = _channels(self.channels)
        if channel_ids != tuple(item.channel_id for item in channels):
            raise BatchAnalysisPreflightError("ordered_channel_ids do not match channels")
        inventory = _sha256(self.channel_inventory_sha256, "channel_inventory_sha256")
        if inventory != _digest(list(channel_ids)):
            raise BatchAnalysisPreflightError(
                "channel_inventory_sha256 does not match ordered_channel_ids"
            )
        semantics = _sha256(self.channel_semantics_sha256, "channel_semantics_sha256")
        channel_mappings = [item.to_mapping() for item in channels]
        if semantics != _digest(channel_mappings):
            raise BatchAnalysisPreflightError("channel_semantics_sha256 does not match channels")
        probe = _probe_mapping(self.qupath_executable_version)
        probe_digest = _sha256(self.qupath_probe_sha256, "qupath_probe_sha256")
        if probe_digest != probe["qupath_probe_sha256"]:
            raise BatchAnalysisPreflightError(
                "qupath_probe_sha256 does not match successful QuPath probe"
            )
        expected = _digest(
            {
                "schema_version": self.schema_version,
                "spatial_frame": self.spatial_frame.to_mapping(),
                "channels": channel_mappings,
                "ordered_channel_ids": list(channel_ids),
                "channel_inventory_sha256": inventory,
                "channel_semantics_sha256": semantics,
                "qupath_probe": probe,
            }
        )
        actual = _sha256(self.analysis_preflight_sha256, "analysis_preflight_sha256")
        if actual != expected:
            raise BatchAnalysisPreflightError(
                "analysis_preflight_sha256 does not match composite evidence"
            )
        object.__setattr__(self, "ordered_channel_ids", channel_ids)
        object.__setattr__(self, "channels", channels)
        object.__setattr__(self, "channel_inventory_sha256", inventory)
        object.__setattr__(self, "channel_semantics_sha256", semantics)
        object.__setattr__(self, "qupath_executable_version", probe["executable_version"])
        object.__setattr__(self, "qupath_probe_sha256", probe_digest)
        object.__setattr__(self, "analysis_preflight_sha256", actual)

    @property
    def qupath_probe(self) -> dict[str, object]:
        return _probe_mapping(self.qupath_executable_version)

    @classmethod
    def create(
        cls,
        report: InputValidationReport,
        probe: QuPathImagePreflight,
    ) -> "BatchAnalysisPreflightEvidence":
        if not isinstance(report, InputValidationReport):
            raise BatchAnalysisPreflightError(
                "analysis preflight requires an InputValidationReport"
            )
        if not isinstance(probe, QuPathImagePreflight):
            raise BatchAnalysisPreflightError("analysis preflight requires a QuPathImagePreflight")
        if type(probe.exit_status) is not int or probe.exit_status != 0:
            raise BatchAnalysisPreflightError(
                "analysis preflight requires a successful QuPath probe"
            )
        if type(report.series_index) is not int or report.series_index != 0:
            raise BatchAnalysisPreflightError(
                "analysis preflight requires canonical report series_index 0"
            )
        frame = CanonicalSpatialFrameEvidence.create(
            canonical_image_id=report.canonical_image_id,
            canonical_image_sha256=report.canonical_image_sha256,
            level0_size_x_px=report.level0_size_x_px,
            level0_size_y_px=report.level0_size_y_px,
            x_um_per_px=report.physical_size_x_um,
            y_um_per_px=report.physical_size_y_um,
        )
        try:
            channels = _channels(analysis_channel_specs(report))
        except (ROIAnalysisError, ValueError) as exc:
            raise BatchAnalysisPreflightError("processed OME channels are invalid") from exc
        channel_ids = _channel_ids(tuple(channel.channel_id for channel in channels))
        channel_mappings = [item.to_mapping() for item in channels]
        probe_mapping = _probe_mapping(probe.executable_version)
        unsigned = {
            "schema_version": _SCHEMA_VERSION,
            "spatial_frame": frame.to_mapping(),
            "channels": channel_mappings,
            "ordered_channel_ids": list(channel_ids),
            "channel_inventory_sha256": _digest(list(channel_ids)),
            "channel_semantics_sha256": _digest(channel_mappings),
            "qupath_probe": probe_mapping,
        }
        return cls(
            spatial_frame=frame,
            channels=channels,
            ordered_channel_ids=channel_ids,
            channel_inventory_sha256=unsigned["channel_inventory_sha256"],
            channel_semantics_sha256=unsigned["channel_semantics_sha256"],
            qupath_executable_version=probe_mapping["executable_version"],
            qupath_probe_sha256=probe_mapping["qupath_probe_sha256"],
            analysis_preflight_sha256=_digest(unsigned),
        )

    @classmethod
    def from_mapping(cls, value: object) -> "BatchAnalysisPreflightEvidence":
        """Decode the serialized fields into a validated BatchAnalysisPreflightEvidence record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""

        mapping = _closed_mapping(
            value,
            frozenset(
                {
                    "schema_version",
                    "spatial_frame",
                    "channels",
                    "ordered_channel_ids",
                    "channel_inventory_sha256",
                    "channel_semantics_sha256",
                    "qupath_probe",
                    "analysis_preflight_sha256",
                }
            ),
            "analysis preflight evidence",
        )
        probe = _closed_mapping(
            mapping["qupath_probe"],
            frozenset({"executable_version", "exit_status", "qupath_probe_sha256"}),
            "qupath_probe",
        )
        if type(probe["exit_status"]) is not int or probe["exit_status"] != 0:
            raise BatchAnalysisPreflightError("qupath_probe.exit_status must be 0")
        return cls(
            schema_version=mapping["schema_version"],  # type: ignore[arg-type]
            spatial_frame=CanonicalSpatialFrameEvidence.from_mapping(mapping["spatial_frame"]),
            channels=_channels(mapping["channels"]),
            ordered_channel_ids=_channel_ids(mapping["ordered_channel_ids"]),
            channel_inventory_sha256=mapping["channel_inventory_sha256"],  # type: ignore[arg-type]
            channel_semantics_sha256=mapping["channel_semantics_sha256"],  # type: ignore[arg-type]
            qupath_executable_version=probe["executable_version"],  # type: ignore[arg-type]
            qupath_probe_sha256=probe["qupath_probe_sha256"],  # type: ignore[arg-type]
            analysis_preflight_sha256=mapping["analysis_preflight_sha256"],  # type: ignore[arg-type]
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "spatial_frame": self.spatial_frame.to_mapping(),
            "channels": [item.to_mapping() for item in self.channels],
            "ordered_channel_ids": list(self.ordered_channel_ids),
            "channel_inventory_sha256": self.channel_inventory_sha256,
            "channel_semantics_sha256": self.channel_semantics_sha256,
            "qupath_probe": self.qupath_probe,
            "analysis_preflight_sha256": self.analysis_preflight_sha256,
        }


def analysis_preflight_identity_contract() -> ArtifactIdentityContract:
    """Return the registered exact owner identity contract for analysis frames."""

    return ArtifactIdentityContract(
        contract_id="mif-batch-analysis-preflight-identity/v2",
        artifact_kind="analysis_frame",
        schema_name=_SCHEMA_NAME,
        schema_version=2,
        required_fields=ANALYSIS_PREFLIGHT_IDENTITY_FIELDS,
        optional_fields=(),
        required_runtime_names=("mif_pipeline", "qupath"),
    )


def _validated_processed_image(ref: BatchArtifactRef) -> BatchArtifactRef:
    if not isinstance(ref, BatchArtifactRef):
        raise BatchAnalysisPreflightError(
            "analysis preflight requires a processed OME-TIFF artifact reference"
        )
    try:
        validated = validate_artifact_ref(ref)
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchAnalysisPreflightError(
            "processed OME-TIFF artifact reference is invalid"
        ) from exc
    contract = preprocessing_identity_contract("processed_image")
    if (
        validated.artifact_kind != "processed_image"
        or validated.identity_contract != contract
        or validated.owning_identity_contract_sha256 != contract.contract_sha256
        or validated.producer_version != "mif-pipeline-batch-preprocessing/v1"
        or validated.runtime_versions.get("mif_pipeline") != MIF_PIPELINE_BATCH_RUNTIME_VERSION
    ):
        raise BatchAnalysisPreflightError(
            "processed OME-TIFF artifact lacks its public preprocessing owner contract"
        )
    return validated


def _require_exact_context_input(
    context: BatchNodeExecutionContext, processed: BatchArtifactRef
) -> None:
    if not isinstance(context, BatchNodeExecutionContext):
        raise BatchAnalysisPreflightError("analysis preflight requires a typed execution context")
    if context.adapter_spec.node_kind != "analysis_preflight":
        raise BatchAnalysisPreflightError("analysis preflight context has the wrong node")
    if tuple(context.inputs) != (processed,):
        raise BatchAnalysisPreflightError(
            "analysis preflight requires the exact processed OME-TIFF context input"
        )
    contract = analysis_preflight_identity_contract()
    if tuple(context.adapter_spec.output_identity_contracts) != (contract,):
        raise BatchAnalysisPreflightError(
            "analysis preflight context lacks the owner-issued analysis-frame contract"
        )


def build_analysis_frame_artifact_ref(
    context: BatchNodeExecutionContext,
    processed_image: BatchArtifactRef,
    evidence: BatchAnalysisPreflightEvidence,
) -> BatchArtifactRef:
    """Publish a locatorless analysis-frame reference from real preflight evidence."""

    processed = _validated_processed_image(processed_image)
    if not isinstance(evidence, BatchAnalysisPreflightEvidence):
        raise BatchAnalysisPreflightError("analysis preflight requires typed composite evidence")
    _require_exact_context_input(context, processed)
    identity = processed.owning_identity
    try:
        output_sha256 = identity["publication"]["output_sha256"]  # type: ignore[index]
    except (KeyError, TypeError) as exc:
        raise BatchAnalysisPreflightError(
            "processed OME-TIFF owner identity lacks exact image publication hashes"
        ) from exc
    if (
        evidence.spatial_frame.canonical_image_sha256 != processed.sha256
        or evidence.spatial_frame.canonical_image_sha256 != output_sha256
    ):
        raise BatchAnalysisPreflightError(
            "analysis preflight spatial frame does not match processed OME-TIFF identity"
        )
    scope = processed.scope
    if scope.sample_id is None or scope.slide_id is None:
        raise BatchAnalysisPreflightError(
            "processed OME-TIFF lacks sample/slide scope for analysis preflight"
        )
    contract = analysis_preflight_identity_contract()
    return BatchArtifactRef(
        artifact_id="analysis-frame-" + evidence.analysis_preflight_sha256[:48],
        artifact_kind="analysis_frame",
        schema_name=_SCHEMA_NAME,
        schema_version=2,
        origin=ArtifactOrigin.INTERNAL_NODE,
        producer_node_id=context.node_id,
        producer_attempt_id=context.authorization.attempt_id,
        producer_attempt_authorization_sha256=context.authorization.authorization_sha256,
        external_declaration_sha256=None,
        external_validation_receipt_sha256=None,
        locator=None,
        sha256=None,
        byte_count=None,
        scope=ArtifactScope(
            scope.validation_domain_id,
            scope.sample_id,
            scope.slide_id,
            None,
            (),
        ),
        owning_identity=evidence.to_mapping(),
        identity_contract=contract,
        owning_identity_contract_sha256=contract.contract_sha256,
        producer_version=_PRODUCER_VERSION,
        runtime_versions={
            "mif_pipeline": MIF_PIPELINE_BATCH_RUNTIME_VERSION,
            "qupath": evidence.qupath_executable_version,
        },
        compatibility_signature_sha256=context.compatibility_signature_sha256,
        configuration_bindings=processed.configuration_bindings,
    )


def validate_analysis_frame_artifact_ref(
    ref: BatchArtifactRef,
) -> BatchAnalysisPreflightEvidence:
    """Validate the complete persisted audit projection without reopening locators.

    Structured inputs are decoded through their typed ``from_mapping`` constructors. The
    remaining comparisons check the relationships required by this operation before the
    caller can use those records."""

    if not isinstance(ref, BatchArtifactRef):
        raise BatchAnalysisPreflightError(
            "analysis-frame artifact reference must be a BatchArtifactRef"
        )
    try:
        validated = validate_artifact_ref(ref)
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchAnalysisPreflightError("analysis-frame artifact reference is invalid") from exc
    contract = analysis_preflight_identity_contract()
    if (
        validated.artifact_kind != "analysis_frame"
        or (validated.schema_name, validated.schema_version) != (_SCHEMA_NAME, 2)
        or validated.identity_contract != contract
        or validated.owning_identity_contract_sha256 != contract.contract_sha256
        or validated.producer_version != _PRODUCER_VERSION
        or validated.locator is not None
        or validated.sha256 is not None
        or validated.byte_count is not None
    ):
        raise BatchAnalysisPreflightError(
            "analysis-frame artifact lacks the locatorless public owner contract"
        )
    scope = validated.scope
    if (
        scope.sample_id is None
        or scope.slide_id is None
        or scope.project_entry_id is not None
        or scope.analysis_domain_bindings
    ):
        raise BatchAnalysisPreflightError(
            "analysis-frame artifact has forbidden project or analysis-domain claims"
        )
    evidence = BatchAnalysisPreflightEvidence.from_mapping(validated.owning_identity)
    if dict(validated.runtime_versions) != {
        "mif_pipeline": MIF_PIPELINE_BATCH_RUNTIME_VERSION,
        "qupath": evidence.qupath_executable_version,
    }:
        raise BatchAnalysisPreflightError(
            "analysis-frame artifact runtime bindings do not match the successful probe"
        )
    return evidence


def resolve_analysis_channels(
    config: AnalysisConfig,
    frame_ref: BatchArtifactRef,
    *,
    sample_id: str,
    validation_domain_id: str,
) -> tuple[ChannelSpec, ...]:
    """Read channels from an already authorized frame under exact admission scope.

    This validates frame shape and configuration lineage; the caller supplies
    current execution or committed-state authority for the reference itself.
    """
    if not isinstance(config, AnalysisConfig):
        raise BatchAnalysisPreflightError("analysis channels require typed admitted config")
    evidence = validate_analysis_frame_artifact_ref(frame_ref)
    scope = frame_ref.scope
    if (
        scope.sample_id != _text(sample_id, "sample_id")
        or scope.slide_id != config.slide_id
        or scope.validation_domain_id != _text(validation_domain_id, "validation_domain_id")
    ):
        raise BatchAnalysisPreflightError(
            "analysis-frame sample, slide, or validation-domain scope differs"
        )
    bindings = frame_ref.configuration_bindings
    if (
        len(bindings) != 1
        or bindings[0].sample_id != sample_id
        or bindings[0].resolved_config_sha256 != _digest(config.to_mapping())
    ):
        raise BatchAnalysisPreflightError("analysis-frame original configuration binding differs")
    if config.channels is not None and tuple(config.channels) != evidence.channels:
        raise BatchAnalysisPreflightError(
            "admitted concrete channel semantics differ from analysis frame"
        )
    return evidence.channels


__all__ = [
    "ANALYSIS_PREFLIGHT_IDENTITY_FIELDS",
    "MAX_ANALYSIS_PREFLIGHT_CHANNELS",
    "MIF_PIPELINE_BATCH_RUNTIME_VERSION",
    "BatchAnalysisPreflightEvidence",
    "BatchAnalysisPreflightError",
    "analysis_preflight_identity_contract",
    "build_analysis_frame_artifact_ref",
    "resolve_analysis_channels",
    "validate_analysis_frame_artifact_ref",
]
