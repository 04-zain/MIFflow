"""Service-owned authority bridge from reviewed ROIs to purification inputs.

The `ReferenceRoiSelectionPlan` record carries `source_checkpoint_sha256`, `checkpoint`,
`fluorophore`, `export`, `selections` and its remaining declared fields.

The functions retain the selected spectral basis, component order and reviewed acquisition
facts through the operation. Numerical work is bounded by the configured pixel blocks, while
published references and results keep their input and settings identities."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass, field

from mif_pipeline.io import resolve_source_image_facts

from .background import SampledReferenceRoi
from .background import BackgroundEquivalencePolicy
from .canonical_reference_input import (
    CanonicalReferenceInput,
)
from .reference_rois import (
    AcceptedReferenceRoiSelection,
    ReferenceRoiExport,
    dumps_reference_roi_export,
    loads_reference_roi_export,
)
from .reference_sampling import (
    CompleteVectorQcPolicy,
    MeasurementPlane,
    sample_reference_roi,
)
from .interactions import SelectedIdentifier
from .purification import PurificationPolicy
from .reference_review_bindings import NegativeFallbackReviewPolicy
from .typed_checksum import typed_checksum_sha256

_ROI_CHECKPOINTS = frozenset(
    {
        "positive_reference_rois",
        "af_reference_rois",
        "negative_reference_rois",
        "background_reference_rois",
    }
)


# ==============================================================================
#                       Reference service roi purification
# ==============================================================================


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{label} must be nonempty literal text")
    return value


def _sha256(value: object, label: str) -> str:
    result = _text(value, label)
    if len(result) != 64 or any(item not in "0123456789abcdef" for item in result):
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return result


@dataclass(frozen=True, slots=True)
class ReferenceRoiPairingSelection:
    """Reviewer-selected positive/control ROI pairing; values are never accepted."""

    positive_roi_id: str
    control_roi_id: str

    def __post_init__(self) -> None:
        _text(self.positive_roi_id, "positive ROI ID")
        _text(self.control_roi_id, "control ROI ID")
        if self.positive_roi_id == self.control_roi_id:
            raise ValueError("positive and control ROI IDs must be distinct")


@dataclass(frozen=True, slots=True)
class ReferenceRoiSelectionPlan:
    """Exact accepted Task-4 export, ordered selections, and sampling policy."""

    source_checkpoint_sha256: str
    checkpoint: str
    fluorophore: str
    export: ReferenceRoiExport
    selections: tuple[AcceptedReferenceRoiSelection, ...]
    pairings: tuple[ReferenceRoiPairingSelection, ...]
    sampling_policy: CompleteVectorQcPolicy
    reviewer_id: str
    source_preparation_sha256: str | None = field(default=None, metadata={"omit_empty": True})
    content_sha256: str = field(init=False)

    @classmethod
    def create(cls, **values) -> "ReferenceRoiSelectionPlan":
        return cls(**values)

    def __post_init__(self) -> None:
        """Validate the ReferenceRoiSelectionPlan fields when the record is constructed.

        The checks cover ``source_checkpoint_sha256``, ``checkpoint``, ``fluorophore``,
        ``reviewer_id``, ``selections``, ``export``. Invalid field types, values or
        combinations raise the declared validation error before this instance can be used by
        its caller."""

        _sha256(self.source_checkpoint_sha256, "ROI operation checkpoint")
        if self.checkpoint not in _ROI_CHECKPOINTS:
            raise ValueError("ROI selection plan checkpoint is unsupported")
        _text(self.fluorophore, "ROI selection fluorophore")
        _text(self.reviewer_id, "ROI selection reviewer ID")
        if self.source_preparation_sha256 is not None:
            _sha256(self.source_preparation_sha256, "ROI source preparation")
        if type(self.export) is not ReferenceRoiExport or (
            loads_reference_roi_export(dumps_reference_roi_export(self.export)) != self.export
        ):
            raise ValueError("ROI selection plan requires an exact accepted export")
        if (
            type(self.selections) is not tuple
            or not self.selections
            or any(type(item) is not AcceptedReferenceRoiSelection for item in self.selections)
        ):
            raise TypeError("ROI selections must be a nonempty exact tuple")
        rebuilt = tuple(
            AcceptedReferenceRoiSelection(self.export, item.roi_id) for item in self.selections
        )
        if rebuilt != self.selections:
            raise ValueError("ROI selections differ from the exact accepted export")
        roi_ids = tuple(item.roi_id for item in self.selections)
        if len(roi_ids) != len(set(roi_ids)):
            raise ValueError("ROI selection plan contains duplicate ROIs")
        accepted_order = tuple(
            item.roi_id for item in self.export.rois if item.roi_id in set(roi_ids)
        )
        if roi_ids != accepted_order:
            raise ValueError("ROI selections must retain deterministic export order")
        if type(self.pairings) is not tuple or any(
            type(item) is not ReferenceRoiPairingSelection for item in self.pairings
        ):
            raise TypeError("ROI pairings must be an exact tuple")
        pairing_keys = tuple((item.positive_roi_id, item.control_roi_id) for item in self.pairings)
        if len(pairing_keys) != len(set(pairing_keys)):
            raise ValueError("ROI selection plan contains duplicate pairings")
        if type(self.sampling_policy) is not CompleteVectorQcPolicy:
            raise TypeError("sampling policy must be exact CompleteVectorQcPolicy")
        object.__setattr__(
            self,
            "content_sha256",
            typed_checksum_sha256(
                {
                    "semantic": "reference_roi_selection_plan_v1",
                    "source_checkpoint_sha256": self.source_checkpoint_sha256,
                    "checkpoint": self.checkpoint,
                    "fluorophore": self.fluorophore,
                    "export_content_sha256": self.export.content_sha256,
                    "selection_sha256s": [item.selection_sha256 for item in self.selections],
                    "pairings": [
                        {
                            "positive_roi_id": item.positive_roi_id,
                            "control_roi_id": item.control_roi_id,
                        }
                        for item in self.pairings
                    ],
                    "sampling_policy": {
                        "processing_chunk_width": (
                            self.sampling_policy.processing_chunk_shape.width
                        ),
                        "processing_chunk_height": (
                            self.sampling_policy.processing_chunk_shape.height
                        ),
                        "saturation_fraction": self.sampling_policy.saturation_fraction,
                        "near_zero_l2_threshold": (self.sampling_policy.near_zero_l2_threshold),
                    },
                    "reviewer_id": self.reviewer_id,
                    **({"source_preparation_sha256": self.source_preparation_sha256}
                       if self.source_preparation_sha256 is not None else {}),
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class SampledReferenceRoiBundle:
    """Complete-vector samples in the exact deterministic selection-plan order."""

    plan: ReferenceRoiSelectionPlan
    samples: tuple[SampledReferenceRoi, ...]
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.plan) is not ReferenceRoiSelectionPlan:
            raise TypeError("sample bundle plan has the wrong exact type")
        if (
            type(self.samples) is not tuple
            or len(self.samples) != len(self.plan.selections)
            or any(type(item) is not SampledReferenceRoi for item in self.samples)
        ):
            raise TypeError("sample bundle must match the exact selection inventory")
        rebuilt = tuple(
            SampledReferenceRoi(item.selection, item.input_evidence, item.sampling)
            for item in self.samples
        )
        if (
            rebuilt != self.samples
            or tuple(item.selection for item in self.samples) != self.plan.selections
        ):
            raise ValueError("sample bundle differs from its selection plan")
        object.__setattr__(
            self,
            "content_sha256",
            typed_checksum_sha256(
                {
                    "semantic": "sampled_reference_roi_bundle_v1",
                    "plan_sha256": self.plan.content_sha256,
                    "sample_evidence_sha256s": [item.evidence_sha256 for item in self.samples],
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class ReferenceBackgroundOperation:
    """Replay binding for service-derived image-specific background resolution."""

    source_checkpoint_sha256: str
    reviewer_id: str
    positive_bundle_sha256: str
    control_bundle_sha256: str
    background_bundle_sha256: str
    policy: BackgroundEquivalencePolicy
    negative_fallback_policy: NegativeFallbackReviewPolicy | None
    resolution_sha256s: tuple[str, ...]
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the ReferenceBackgroundOperation fields when the record is constructed.

        The checks cover ``resolution_sha256s``, ``reviewer_id``,
        ``source_checkpoint_sha256``, ``positive_bundle_sha256``, ``control_bundle_sha256``,
        ``background_bundle_sha256``. Invalid field types, values or combinations raise the
        declared validation error before this instance can be used by its caller."""

        for value, label in (
            (self.source_checkpoint_sha256, "background source checkpoint"),
            (self.positive_bundle_sha256, "positive sample bundle"),
            (self.control_bundle_sha256, "control sample bundle"),
            (self.background_bundle_sha256, "background sample bundle"),
        ):
            _sha256(value, label)
        _text(self.reviewer_id, "background reviewer ID")
        if type(self.policy) is not BackgroundEquivalencePolicy:
            raise TypeError("background policy must be exact")
        if (
            self.negative_fallback_policy is not None
            and type(self.negative_fallback_policy) is not NegativeFallbackReviewPolicy
        ):
            raise TypeError("negative-fallback policy must be exact")
        if type(self.resolution_sha256s) is not tuple or not self.resolution_sha256s:
            raise TypeError("background resolutions must be a nonempty tuple")
        for item in self.resolution_sha256s:
            _sha256(item, "background resolution SHA-256")
        object.__setattr__(
            self,
            "content_sha256",
            typed_checksum_sha256(
                {
                    "semantic": "reference_background_operation_v1",
                    "source_checkpoint_sha256": self.source_checkpoint_sha256,
                    "reviewer_id": self.reviewer_id,
                    "positive_bundle_sha256": self.positive_bundle_sha256,
                    "control_bundle_sha256": self.control_bundle_sha256,
                    "background_bundle_sha256": self.background_bundle_sha256,
                    "policy_sha256": self.policy.content_sha256,
                    "negative_fallback_policy_sha256": (
                        None
                        if self.negative_fallback_policy is None
                        else self.negative_fallback_policy.content_sha256
                    ),
                    "resolution_sha256s": list(self.resolution_sha256s),
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class ReferenceSignalPurificationOperation:
    """Exact reviewer selection and internally derived purification replay binding."""

    source_checkpoint_sha256: str
    reviewer_id: str
    selections: tuple[SelectedIdentifier, ...]
    background_operation_sha256: str
    purification_policy: PurificationPolicy
    purification_evidence_sha256s: tuple[str, ...]
    af_control_set_review_sha256s: tuple[str, ...] = field(default=(), metadata={"omit_empty": True})
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _sha256(self.source_checkpoint_sha256, "signal source checkpoint")
        _text(self.reviewer_id, "signal reviewer ID")
        _sha256(self.background_operation_sha256, "background operation")
        if type(self.selections) is not tuple or any(
            type(item) is not SelectedIdentifier for item in self.selections
        ):
            raise TypeError("signal selections must be an exact tuple")
        if type(self.purification_policy) is not PurificationPolicy:
            raise TypeError("purification policy must be exact")
        if (
            type(self.purification_evidence_sha256s) is not tuple
            or not self.purification_evidence_sha256s
        ):
            raise TypeError("purification evidence must be a nonempty tuple")
        for item in self.purification_evidence_sha256s:
            _sha256(item, "purification evidence SHA-256")
        if type(self.af_control_set_review_sha256s) is not tuple:
            raise TypeError("AF control set reviews must be an exact tuple")
        for item in self.af_control_set_review_sha256s:
            _sha256(item, "AF control set review")
        object.__setattr__(
            self,
            "content_sha256",
            typed_checksum_sha256(
                {
                    "semantic": "reference_signal_purification_operation_v1",
                    "source_checkpoint_sha256": self.source_checkpoint_sha256,
                    "reviewer_id": self.reviewer_id,
                    "selections": [item.to_mapping() for item in self.selections],
                    "background_operation_sha256": self.background_operation_sha256,
                    "purification_policy_sha256": self.purification_policy.content_sha256,
                    "purification_evidence_sha256s": list(self.purification_evidence_sha256s),
                    **({"af_control_set_review_sha256s": list(self.af_control_set_review_sha256s)}
                       if self.af_control_set_review_sha256s else {}),
                }
            ),
        )


def _revalidate_live_target(backend, target: CanonicalReferenceInput) -> None:
    with target.open_source(backend):
        pass


def sample_reference_roi_selection_plan(
    backend,
    canonical_inputs: tuple[CanonicalReferenceInput, ...],
    plan: ReferenceRoiSelectionPlan,
) -> SampledReferenceRoiBundle:
    """Sample one plan with one same-handle fenced open per distinct source."""

    if type(plan) is not ReferenceRoiSelectionPlan:
        raise TypeError("sampling requires an exact ROI selection plan")
    if type(canonical_inputs) is not tuple or any(
        type(item) is not CanonicalReferenceInput for item in canonical_inputs
    ):
        raise TypeError("canonical input inventory must be an exact tuple")
    by_id = {item.input_id: item for item in canonical_inputs}
    if len(by_id) != len(canonical_inputs):
        raise ValueError("canonical input inventory contains duplicate IDs")
    targets = tuple(item for item in canonical_inputs if item.input_role == "target_image")
    if len(targets) != 1:
        raise ValueError("ROI sampling requires one exact canonical target")
    target = targets[0]
    _revalidate_live_target(backend, target)
    grouped: dict[str, list[tuple[int, AcceptedReferenceRoiSelection]]] = {}
    for index, selection in enumerate(plan.selections):
        grouped.setdefault(selection.entry.image.reference_input_id, []).append((index, selection))
    if any(input_id not in by_id for input_id in grouped):
        raise ValueError("ROI selection names an unregistered canonical input")
    sampled: list[SampledReferenceRoi | None] = [None] * len(plan.selections)
    for input_id, selections in grouped.items():
        record = by_id[input_id]
        with record.open_source(
            backend,
            compatibility_target=(None if record.input_role == "target_image" else target),
        ) as source:
            facts = resolve_source_image_facts(source)
            planes = tuple(
                MeasurementPlane(item.measurement_id, item.channel_id, item.channel_index)
                for item in record.measurement_planes
            )
            for index, selection in selections:
                result = sample_reference_roi(
                    source,
                    facts,
                    record.evidence,
                    planes,
                    selection,
                    plan.sampling_policy,
                )
                sampled[index] = SampledReferenceRoi(selection, record.evidence, result)
    _revalidate_live_target(backend, target)
    return SampledReferenceRoiBundle(plan, tuple(sampled))  # type: ignore[arg-type]


__all__ = [
    "ReferenceRoiPairingSelection",
    "ReferenceRoiSelectionPlan",
    "ReferenceBackgroundOperation",
    "ReferenceSignalPurificationOperation",
    "SampledReferenceRoiBundle",
    "sample_reference_roi_selection_plan",
]
