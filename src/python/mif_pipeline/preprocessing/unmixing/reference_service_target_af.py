"""Closed service records and bounded sampling for control-derived target AF.

The `TargetAfSamplingPlan` record carries `source_checkpoint_sha256`, `selection_kind`,
`control_input_id`, `accepted_library_sha256`, `export` and its remaining declared fields.

The functions retain the selected spectral basis, component order and reviewed acquisition
facts through the operation. Numerical work is bounded by the configured pixel blocks, while
published references and results keep their input and settings identities."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass, field

from mif_pipeline.io import resolve_source_image_facts

from .background import BackgroundEquivalencePolicy, BackgroundModelResolution, SampledReferenceRoi
from .canonical_reference_input import (
    CanonicalReferenceInput,
)
from .reference_models import VersionRecord
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
from .target_af import FixedBasisQcPolicy, TargetAfEstimationPolicy, _json_primitive
from .typed_checksum import typed_checksum_sha256

# ==============================================================================
#                          Reference service target af
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
class TargetAfRoiReviewSelection:
    """One reviewer disposition without sampled values or authority hashes."""

    roi_id: str
    component_identity: str
    included: bool

    def __post_init__(self) -> None:
        _text(self.roi_id, "target-AF ROI ID")
        if self.included or self.component_identity:
            _text(self.component_identity, "target-AF component identity")
        if type(self.included) is not bool:
            raise TypeError("target-AF ROI inclusion must be an exact bool")


@dataclass(frozen=True, slots=True)
class TargetControlReviewResponse:
    """Explicit human control-context dispositions without derived reports."""

    preparation_disposition: str
    tissue_context_disposition: str
    reviewer_id: str
    policy_id: str
    policy_version: str
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        for value, label in (
            (self.preparation_disposition, "preparation disposition"),
            (self.tissue_context_disposition, "tissue-context disposition"),
        ):
            if value not in {"accepted", "rejected"}:
                raise ValueError(f"target-control {label} is unsupported")
        _text(self.reviewer_id, "target-control reviewer ID")
        _text(self.policy_id, "target-control review policy ID")
        _text(self.policy_version, "target-control review policy version")
        object.__setattr__(
            self,
            "content_sha256",
            typed_checksum_sha256(
                {
                    "semantic": "target_control_review_response_v1",
                    "preparation_disposition": self.preparation_disposition,
                    "tissue_context_disposition": self.tissue_context_disposition,
                    "reviewer_id": self.reviewer_id,
                    "policy_id": self.policy_id,
                    "policy_version": self.policy_version,
                }
            ),
        )

    @property
    def accepted(self) -> bool:
        return (
            self.preparation_disposition == "accepted"
            and self.tissue_context_disposition == "accepted"
        )


@dataclass(frozen=True, slots=True)
class TargetAfSharedRoiAuthority:
    """Live accepted Task-4/5/6 lineage for one selected shared-control group."""

    fluorophore: str
    roi_ids: tuple[str, ...]
    af_decision_sha256: str
    af_plan_sha256: str
    af_bundle_sha256: str
    background_decision_sha256: str
    background_plan_sha256: str
    background_bundle_sha256: str
    background_resolution_sha256s: tuple[str, ...]
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the TargetAfSharedRoiAuthority fields when the record is constructed.

        The checks cover ``background_resolution_sha256s``, ``fluorophore``, ``roi_ids``,
        ``af_decision_sha256``, ``af_plan_sha256``, ``af_bundle_sha256``. Invalid field
        types, values or combinations raise the declared validation error before this
        instance can be used by its caller."""

        _text(self.fluorophore, "shared target-AF fluorophore")
        if (
            type(self.roi_ids) is not tuple
            or not self.roi_ids
            or any(type(item) is not str or not item for item in self.roi_ids)
            or len(self.roi_ids) != len(set(self.roi_ids))
        ):
            raise ValueError("shared target-AF ROI IDs must be a unique exact tuple")
        for name in (
            "af_decision_sha256",
            "af_plan_sha256",
            "af_bundle_sha256",
            "background_decision_sha256",
            "background_plan_sha256",
            "background_bundle_sha256",
        ):
            _sha256(getattr(self, name), name.replace("_", " "))
        if type(self.background_resolution_sha256s) is not tuple or len(
            self.background_resolution_sha256s
        ) != len(self.roi_ids):
            raise ValueError("shared background resolutions must align with selected ROI IDs")
        for value in self.background_resolution_sha256s:
            _sha256(value, "shared background resolution SHA-256")
        object.__setattr__(
            self,
            "content_sha256",
            typed_checksum_sha256(
                {
                    "semantic": "target_af_shared_roi_authority_v1",
                    "fluorophore": self.fluorophore,
                    "roi_ids": list(self.roi_ids),
                    "af_decision_sha256": self.af_decision_sha256,
                    "af_plan_sha256": self.af_plan_sha256,
                    "af_bundle_sha256": self.af_bundle_sha256,
                    "background_decision_sha256": self.background_decision_sha256,
                    "background_plan_sha256": self.background_plan_sha256,
                    "background_bundle_sha256": self.background_bundle_sha256,
                    "background_resolution_sha256s": list(self.background_resolution_sha256s),
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class TargetAfSourceReviewSelection:
    """Target-use dispositions for every displayed source, including exclusions."""

    input_id: str
    preparation_disposition: str
    tissue_context_disposition: str

    def __post_init__(self) -> None:
        _text(self.input_id, "target-AF source ID")
        for value in (self.preparation_disposition, self.tissue_context_disposition):
            if value not in {"accepted", "rejected", "unreviewed"}:
                raise ValueError("unsupported target-use source disposition")

    @property
    def accepted(self) -> bool:
        return self.preparation_disposition == self.tissue_context_disposition == "accepted"


@dataclass(frozen=True, slots=True)
class TargetAfRetainedRoiAuthority:
    """One unchanged measured region and every live extraction use of it."""

    source_roi: SampledReferenceRoi
    sampling_policy: CompleteVectorQcPolicy
    background: BackgroundModelResolution
    extraction_authorities: tuple[TargetAfSharedRoiAuthority, ...]
    source_preparation_sha256: str | None = field(default=None, metadata={"omit_empty": True})

    def __post_init__(self) -> None:
        if type(self.source_roi) is not SampledReferenceRoi:
            raise TypeError("retained target-AF region requires exact sampled evidence")
        if type(self.sampling_policy) is not CompleteVectorQcPolicy:
            raise TypeError("retained target-AF region requires its sampling policy")
        attestation = self.source_roi.sampling.attestation
        if (attestation.processing_chunk_width != self.sampling_policy.processing_chunk_shape.width
                or attestation.processing_chunk_height != self.sampling_policy.processing_chunk_shape.height):
            raise ValueError("retained target-AF sampling geometry differs from its original policy")
        if type(self.background) is not BackgroundModelResolution or self.background.status != "resolved":
            raise ValueError("retained target-AF region requires its resolved background")
        if type(self.extraction_authorities) is not tuple or any(
                type(item) is not TargetAfSharedRoiAuthority for item in self.extraction_authorities):
            raise TypeError("retained extraction authorities must be an exact tuple")
        if any(self.source_roi.selection.roi_id not in item.roi_ids for item in self.extraction_authorities):
            raise ValueError("retained region differs from extraction lineage")
        if self.source_preparation_sha256 is not None:
            _sha256(self.source_preparation_sha256, "retained shared source preparation")


@dataclass(frozen=True, slots=True)
class TargetAfSamplingPlan:
    """Exact Task-4 selections and explicit policies for one sampling transaction."""

    source_checkpoint_sha256: str
    selection_kind: str
    control_input_id: str
    accepted_library_sha256: str
    export: ReferenceRoiExport
    af_selections: tuple[AcceptedReferenceRoiSelection, ...]
    background_selections: tuple[AcceptedReferenceRoiSelection, ...]
    roi_reviews: tuple[TargetAfRoiReviewSelection, ...]
    sampling_policy: CompleteVectorQcPolicy
    background_policy: BackgroundEquivalencePolicy
    estimation_policy: TargetAfEstimationPolicy
    qc_policy: FixedBasisQcPolicy
    artifact_id: str
    created_at: str
    reviewer_id: str
    software_versions: tuple[VersionRecord, ...]
    policy_versions: tuple[VersionRecord, ...]
    shared_authorities: tuple[TargetAfSharedRoiAuthority, ...]
    additional_control_input_ids: tuple[str, ...] = field(default=(), metadata={"omit_empty": True})
    retained_authorities: tuple[TargetAfRetainedRoiAuthority, ...] = field(default=(), metadata={"omit_empty": True})
    source_reviews: tuple[TargetAfSourceReviewSelection, ...] = field(default=(), metadata={"omit_empty": True})
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate the TargetAfSamplingPlan fields when the record is constructed.

        The checks cover ``source_checkpoint_sha256``, ``selection_kind``,
        ``control_input_id``, ``accepted_library_sha256``, ``af_selections``,
        ``artifact_id``. Invalid field types, values or combinations raise the declared
        validation error before this instance can be used by its caller."""

        _sha256(self.source_checkpoint_sha256, "target-AF source checkpoint")
        if self.selection_kind not in {
            "reuse_reference_unstained_control",
            "select_target_unstained_control",
            "review_reference_control_groups",
        }:
            raise ValueError("target-AF control selection is unsupported")
        _text(self.control_input_id, "target-AF control input ID")
        _sha256(self.accepted_library_sha256, "accepted target-AF library")
        if type(self.export) is not ReferenceRoiExport or (
            loads_reference_roi_export(dumps_reference_roi_export(self.export)) != self.export
        ):
            raise ValueError("target-AF sampling requires an exact accepted ROI export")
        for value, label in (
            (self.af_selections, "target-AF ROI selections"),
            (self.background_selections, "target-AF background selections"),
        ):
            if type(value) is not tuple or any(
                type(item) is not AcceptedReferenceRoiSelection for item in value
            ):
                raise TypeError(f"{label} must be an exact tuple")
        if not self.af_selections:
            raise ValueError("target-AF sampling requires at least one AF ROI")
        selections = (*self.af_selections, *self.background_selections)
        retained = self.selection_kind == "review_reference_control_groups"
        if not retained and any(
            AcceptedReferenceRoiSelection(self.export, item.roi_id) != item for item in selections
        ):
            raise ValueError("target-AF selections differ from the accepted export")
        if any(item.roi.roi_role not in ({"af", "negative"} if retained else {"af"}) for item in self.af_selections):
            raise ValueError("target-AF selections must contain only AF ROIs")
        if any(item.roi.roi_role != "background" for item in self.background_selections):
            raise ValueError("target-AF background selections must contain only background ROIs")
        roi_ids = tuple(item.roi_id for item in selections)
        if len(roi_ids) != len(set(roi_ids)):
            raise ValueError("target-AF sampling selections contain duplicate ROIs")
        if not retained and any(item.entry.image.reference_input_id != self.control_input_id for item in selections):
            raise ValueError("target-AF ROI selection uses a foreign control input")
        if type(self.roi_reviews) is not tuple or any(
            type(item) is not TargetAfRoiReviewSelection for item in self.roi_reviews
        ):
            raise TypeError("target-AF ROI reviews must be an exact tuple")
        if tuple(item.roi_id for item in self.roi_reviews) != tuple(
            item.roi_id for item in self.af_selections
        ):
            raise ValueError("target-AF ROI reviews must match exact AF selection order")
        identities = tuple(item.component_identity for item in self.roi_reviews if item.included)
        if not identities:
            raise ValueError("at least one reviewed target-AF component must be included")
        for value, expected, label in (
            (self.sampling_policy, CompleteVectorQcPolicy, "sampling policy"),
            (self.background_policy, BackgroundEquivalencePolicy, "background policy"),
            (self.estimation_policy, TargetAfEstimationPolicy, "estimation policy"),
            (self.qc_policy, FixedBasisQcPolicy, "QC policy"),
        ):
            if type(value) is not expected:
                raise TypeError(f"target-AF {label} has the wrong exact type")
        _text(self.artifact_id, "target-AF artifact ID")
        _text(self.created_at, "target-AF created_at")
        _text(self.reviewer_id, "target-AF ROI reviewer ID")
        for value, label in (
            (self.software_versions, "software versions"),
            (self.policy_versions, "policy versions"),
        ):
            if (
                type(value) is not tuple
                or not value
                or any(type(item) is not VersionRecord for item in value)
            ):
                raise TypeError(f"target-AF {label} must be a nonempty exact tuple")
        if type(self.shared_authorities) is not tuple or any(
            type(item) is not TargetAfSharedRoiAuthority for item in self.shared_authorities
        ):
            raise TypeError("shared target-AF authorities must be an exact tuple")
        if self.selection_kind == "reuse_reference_unstained_control":
            flattened = tuple(roi_id for item in self.shared_authorities for roi_id in item.roi_ids)
            if flattened != tuple(item.roi_id for item in self.af_selections):
                raise ValueError("shared target-AF authorities must cover exact AF selection order")
        elif self.shared_authorities:
            raise ValueError("separate target blank cannot carry shared ROI authority")
        if retained:
            if type(self.additional_control_input_ids) is not tuple or any(
                    type(item) is not str or not item for item in self.additional_control_input_ids):
                raise TypeError("additional target-AF controls must be an exact ID tuple")
            if (type(self.retained_authorities) is not tuple or not self.retained_authorities
                    or any(type(item) is not TargetAfRetainedRoiAuthority for item in self.retained_authorities)
                    or tuple(item.source_roi.selection for item in self.retained_authorities) != self.af_selections
                    or self.background_selections):
                raise ValueError("retained target-AF authorities must match the complete region inventory")
            source_ids = tuple(dict.fromkeys(item.entry.image.reference_input_id for item in self.af_selections))
            if (type(self.source_reviews) is not tuple
                    or any(type(item) is not TargetAfSourceReviewSelection for item in self.source_reviews)
                    or tuple(item.input_id for item in self.source_reviews) != source_ids):
                raise ValueError("target-use reviews must cover the exact source inventory")
            included_source_ids = tuple(dict.fromkeys(selection.entry.image.reference_input_id
                for selection, review in zip(self.af_selections, self.roi_reviews, strict=True) if review.included))
            if (self.control_input_id, *self.additional_control_input_ids) != included_source_ids:
                raise ValueError("target-AF control inputs must match included sources in order")
            by_id = {item.input_id: item for item in self.source_reviews}
            if any(not by_id[item].accepted for item in included_source_ids):
                raise ValueError("included target-AF sources require explicit target-use acceptance")
        elif self.additional_control_input_ids or self.retained_authorities or self.source_reviews:
            raise ValueError("legacy sampling plan cannot carry retained-control group authority")
        object.__setattr__(
            self,
            "content_sha256",
            typed_checksum_sha256(
                {
                    "semantic": "target_af_sampling_plan_v1",
                    "source_checkpoint_sha256": self.source_checkpoint_sha256,
                    "selection_kind": self.selection_kind,
                    "control_input_id": self.control_input_id,
                    "accepted_library_sha256": self.accepted_library_sha256,
                    "export_sha256": self.export.content_sha256,
                    "af_selection_sha256s": [item.selection_sha256 for item in self.af_selections],
                    "background_selection_sha256s": [
                        item.selection_sha256 for item in self.background_selections
                    ],
                    "roi_reviews": [
                        {
                            "roi_id": item.roi_id,
                            "component_identity": item.component_identity,
                            "included": item.included,
                        }
                        for item in self.roi_reviews
                    ],
                    "sampling_policy": {
                        "width": self.sampling_policy.processing_chunk_shape.width,
                        "height": self.sampling_policy.processing_chunk_shape.height,
                        "saturation_fraction": self.sampling_policy.saturation_fraction,
                        "near_zero_l2_threshold": self.sampling_policy.near_zero_l2_threshold,
                    },
                    "background_policy_sha256": self.background_policy.content_sha256,
                    "estimation_policy_sha256": self.estimation_policy.content_sha256,
                    "qc_policy_sha256": self.qc_policy.content_sha256,
                    "artifact_id": self.artifact_id,
                    "created_at": self.created_at,
                    "reviewer_id": self.reviewer_id,
                    "software_versions": [
                        {"name": item.name, "version": item.version}
                        for item in self.software_versions
                    ],
                    "policy_versions": [
                        {"name": item.name, "version": item.version}
                        for item in self.policy_versions
                    ],
                    "shared_authority_sha256s": [
                        item.content_sha256 for item in self.shared_authorities
                    ],
                    **({"additional_control_input_ids": list(self.additional_control_input_ids)}
                       if self.additional_control_input_ids else {}),
                    **({"retained_authorities": _json_primitive(self.retained_authorities)} if self.retained_authorities else {}),
                    **({"source_reviews": _json_primitive(self.source_reviews)} if self.source_reviews else {}),
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class TargetAfSampleBundle:
    plan: TargetAfSamplingPlan
    samples: tuple[SampledReferenceRoi, ...]
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.plan) is not TargetAfSamplingPlan:
            raise TypeError("target-AF sample bundle requires its exact plan")
        expected = (*self.plan.af_selections, *self.plan.background_selections)
        if (
            type(self.samples) is not tuple
            or len(self.samples) != len(expected)
            or any(type(item) is not SampledReferenceRoi for item in self.samples)
        ):
            raise TypeError("target-AF sample bundle has the wrong sample inventory")
        rebuilt = tuple(
            SampledReferenceRoi(item.selection, item.input_evidence, item.sampling)
            for item in self.samples
        )
        if rebuilt != self.samples or tuple(item.selection for item in self.samples) != expected:
            raise ValueError("target-AF sample bundle differs from exact Task-4/5 evidence")
        object.__setattr__(
            self,
            "content_sha256",
            typed_checksum_sha256(
                {
                    "semantic": "target_af_sample_bundle_v1",
                    "plan_sha256": self.plan.content_sha256,
                    "sample_sha256s": [item.evidence_sha256 for item in self.samples],
                }
            ),
        )


def sample_target_af_plan(
    backend,
    target: CanonicalReferenceInput,
    control: CanonicalReferenceInput,
    plan: TargetAfSamplingPlan,
) -> TargetAfSampleBundle:
    """Sample all target-AF/control-background ROIs under one same-handle fence."""

    if type(target) is not CanonicalReferenceInput or target.input_role != "target_image":
        raise TypeError("target-AF sampling requires an exact canonical target")
    if type(control) is not CanonicalReferenceInput:
        raise TypeError("target-AF control must be an exact canonical input")
    if type(plan) is not TargetAfSamplingPlan:
        raise TypeError("target-AF sampling requires an exact plan")
    if control.input_id != plan.control_input_id:
        raise ValueError("target-AF plan does not bind the canonical control")
    selections = (*plan.af_selections, *plan.background_selections)
    with target.open_source(backend):
        pass
    with control.open_source(backend, compatibility_target=target) as source:
        planes = tuple(
            MeasurementPlane(item.measurement_id, item.channel_id, item.channel_index)
            for item in control.measurement_planes
        )
        facts = resolve_source_image_facts(source)
        samples = tuple(
            SampledReferenceRoi(
                selection,
                control.evidence,
                sample_reference_roi(
                    source,
                    facts,
                    control.evidence,
                    planes,
                    selection,
                    plan.sampling_policy,
                ),
            )
            for selection in selections
        )
    with target.open_source(backend):
        pass
    return TargetAfSampleBundle(plan, samples)


__all__ = [
    "TargetAfRoiReviewSelection",
    "TargetAfSampleBundle",
    "TargetAfSamplingPlan",
    "TargetAfSharedRoiAuthority",
    "TargetControlReviewResponse",
    "sample_target_af_plan",
]
