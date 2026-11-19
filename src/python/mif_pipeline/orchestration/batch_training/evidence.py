"""Validate and combine the reference evidence produced by participating training images.

Each record retains its source image, selected regions, labels and configuration identity.
The aggregate checks the declared participant inventory and compatible features before it
can supply a fit. Missing references and cross-subject inconsistencies are reported without
dropping samples from the plan."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
import secrets
from threading import RLock
from weakref import ReferenceType, ref

from mif_pipeline.cell_analysis.supervised import (
    MappedPointEvidence,
    SupervisedFeatureDefinition,
    SupervisedPhenotypingError,
    map_point_evidence,
    validate_training_holdout,
)
from mif_pipeline.pipeline_core import AnalysisDomainKind, CellRecord, ROIAnalysisError
from mif_pipeline.pipeline_core.config import (
    PrimaryClassifierSettings, require_primary_settings_compatible,
)
from .primary_policy import primary_training_required_classes

from mif_pipeline.compartment_analysis.classifiers import (
    ClassifierCompatibilityError,
    TrainingEvidenceSnapshot,
)

from ..batch_artifacts import (
    AnalysisDomainBinding,
    BatchArtifactError,
    BatchCommittedArtifactAuthority,
    validate_committed_artifact_authority_set,
)
from ._shared import (
    BatchTrainingError,
    ClassifierKind,
    EvaluationIntent,
    _SPLITS,
    _SPLIT_RANK,
    _array,
    _binding_identity,
    _binding_row_identity,
    _binding_sort_key,
    _canonical_bytes,
    _closed_mapping,
    _digest,
    _export_json,
    _freeze_json,
    _sha256,
    _sha256_tuple,
    _string_tuple,
    _text,
)
from .planning import TrainingPlan, TrainingPlanEntry
from .deferred_cell_evidence import (
    EVIDENCE_FIELD, pending_marker_names, validate_deferred_evidence,
    validate_snapshot_inputs, project_deferred_evidence,
)

_PIXEL_HELD_OUT_ISSUER = object()
_PIXEL_ENTRY_ISSUER = object()
_SUPERVISED_SOURCE_ISSUER = object()
_SUPERVISED_ENTRY_ISSUER = object()
_SUPERVISED_HELD_OUT_ISSUER = object()
_SUPERVISED_PAYLOAD_FIELDS = frozenset(
    {
        "section_id",
        "pipeline_run_id",
        "segmentation_generation_id",
        "measurement_generation_id",
        "coordinate_system",
        "point_selection_radius_level0",
        "class_ids",
        "feature_schema",
        "point_evidence",
        "eligible_cells",
    }
)
_SUPERVISED_COMPACT_CELL_FIELDS = frozenset(
    {
        "cell_id",
        "slide_id",
        "section_id",
        "analysis_domain_id",
        "analysis_domain_kind",
        "x_level0",
        "y_level0",
        "primary_compartment",
        "marker_measurements",
        "cell_geometry",
        "nucleus_geometry",
    }
)


# ==============================================================================
#                                    Evidence
# ==============================================================================


class _FitAuthorityIssuer:
    """Own exact live identities for process-local provider reservations."""

    def __init__(self, family: str) -> None:
        self._family = family
        self._lock = RLock()
        self._reservations: dict[str, tuple[ReferenceType[object], str]] = {}
        self._claims: dict[str, tuple[ReferenceType[object], str]] = {}

    def issue(
        self,
        authority: object,
        initialize: Callable[[str, int], None],
    ) -> None:
        with self._lock:
            reservation_nonce = secrets.token_hex(32)
            while reservation_nonce in self._reservations:
                reservation_nonce = secrets.token_hex(32)
            initialize(reservation_nonce, id(authority))
            issuance_seal = getattr(authority, "_seal_sha256", None)
            if type(issuance_seal) is not str:
                raise BatchTrainingError(f"{self._family} fit evidence authority seal is invalid")

            def remove_expired(
                expired: ReferenceType[object],
                *,
                nonce: str = reservation_nonce,
                sealed_at_issuance: str = issuance_seal,
            ) -> None:
                with self._lock:
                    registered = self._reservations.get(nonce)
                    if (
                        registered is not None
                        and registered[0] is expired
                        and registered[1] == sealed_at_issuance
                    ):
                        self._reservations.pop(nonce, None)

            authority_ref = ref(authority, remove_expired)
            self._reservations[reservation_nonce] = (
                authority_ref,
                issuance_seal,
            )

    def assert_reserved(self, authority: object) -> None:
        reservation_nonce = getattr(authority, "_reservation_nonce", None)
        with self._lock:
            if type(reservation_nonce) is not str:
                raise BatchTrainingError(
                    f"{self._family} fit evidence authority is foreign or consumed"
                )
            registered = self._reservations.get(reservation_nonce)
            if (
                registered is None
                or registered[0]() is not authority
                or getattr(authority, "_authority_object_identity", None) != id(authority)
                or getattr(authority, "_seal_sha256", None) != registered[1]
            ):
                raise BatchTrainingError(
                    f"{self._family} fit evidence authority is foreign or consumed"
                )

    def consume(
        self,
        authority: object,
        reopen_current: Callable[[], object],
    ) -> object:
        """Reauthenticate and consume one exact reservation atomically."""

        reservation_nonce = getattr(authority, "_reservation_nonce", None)
        with self._lock:
            if type(reservation_nonce) is str and reservation_nonce in self._claims:
                raise BatchTrainingError(
                    f"{self._family} fit evidence authority is claimed by a reservation"
                )
            try:
                self.assert_reserved(authority)
                return reopen_current()
            except Exception as exc:
                raise BatchTrainingError(
                    f"{self._family} fit evidence authority was consumed after "
                    "failed reauthentication; a fresh partition/attempt is required"
                ) from exc
            finally:
                if type(reservation_nonce) is str:
                    registered = self._reservations.get(reservation_nonce)
                    if registered is not None and registered[0]() is authority:
                        self._reservations.pop(reservation_nonce, None)

    def revoke(self, authority: object) -> bool:
        """Revoke only the exact live object for one issuer reservation."""

        try:
            reservation_nonce = getattr(authority, "_reservation_nonce", None)
            authority_object_identity = getattr(authority, "_authority_object_identity", None)
        except Exception:
            return False
        if type(reservation_nonce) is not str:
            return False
        with self._lock:
            if reservation_nonce in self._claims:
                return False
            registered = self._reservations.get(reservation_nonce)
            if (
                registered is None
                or registered[0]() is not authority
                or authority_object_identity != id(authority)
            ):
                return False
            self._reservations.pop(reservation_nonce, None)
            return True

    def claim(self, authority: object, owner: object, owner_seal: str) -> None:
        """Atomically reserve one live fit authority for a weak session owner."""

        nonce = getattr(authority, "_reservation_nonce", None)
        if type(nonce) is not str or type(owner_seal) is not str:
            raise BatchTrainingError(f"{self._family} fit reservation claim is invalid")
        with self._lock:
            self.assert_reserved(authority)
            if nonce in self._claims:
                raise BatchTrainingError(
                    f"{self._family} fit evidence authority is already claimed"
                )

            def release(
                expired: ReferenceType[object], *, claim_nonce: str = nonce, seal: str = owner_seal
            ) -> None:
                with self._lock:
                    current = self._claims.get(claim_nonce)
                    if current is not None and current[0] is expired and current[1] == seal:
                        self._claims.pop(claim_nonce, None)

            self._claims[nonce] = (ref(owner, release), owner_seal)

    def release_claim(self, authority: object, owner: object, owner_seal: str) -> bool:
        nonce = getattr(authority, "_reservation_nonce", None)
        if type(nonce) is not str:
            return False
        with self._lock:
            current = self._claims.get(nonce)
            if current is None or current[0]() is not owner or current[1] != owner_seal:
                return False
            self._claims.pop(nonce, None)
            return True


_PIXEL_FIT_ISSUER = _FitAuthorityIssuer("pixel")
_SUPERVISED_FIT_ISSUER = _FitAuthorityIssuer("supervised")


class EvidenceStatus(str, Enum):
    CURRENT = "current"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class TrainingEvidenceEntryState:
    project_entry_id: str
    sample_id: str
    slide_id: str
    subject_id: str
    split: str
    image_identity_sha256: str
    analysis_domain_binding_sha256s: tuple[str, ...]
    analysis_domain_binding_set_sha256: str
    source_predecessor_artifact_ref_sha256s: tuple[str, ...]
    saved_state_before_sha256: str
    saved_state_after_sha256: str
    post_binding_saved_state_sha256: str
    entry_snapshot_set_sha256: str
    entry_state_sha256: str

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
        for name in (
            "analysis_domain_binding_sha256s",
            "source_predecessor_artifact_ref_sha256s",
        ):
            object.__setattr__(
                self,
                name,
                _sha256_tuple(getattr(self, name), name, nonempty=True),
            )
        for name in (
            "analysis_domain_binding_set_sha256",
            "saved_state_before_sha256",
            "saved_state_after_sha256",
            "post_binding_saved_state_sha256",
            "entry_snapshot_set_sha256",
        ):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        state_sha = _sha256(self.entry_state_sha256, "entry_state_sha256")
        if state_sha != _digest(self._unsigned_mapping()):
            raise BatchTrainingError("entry_state_sha256 is not canonical")
        object.__setattr__(self, "entry_state_sha256", state_sha)

    @classmethod
    def create(
        cls,
        *,
        plan_entry: "TrainingPlanEntry",
        analysis_domain_bindings: tuple[AnalysisDomainBinding, ...],
        rows: tuple[TrainingEvidenceSnapshot, ...],
        source_predecessor_artifact_ref_sha256s: tuple[str, ...],
        saved_state_before_sha256: str,
        saved_state_after_sha256: str,
        post_binding_saved_state_sha256: str,
        image_identity_sha256: str | None = None,
    ) -> "TrainingEvidenceEntryState":
        if not isinstance(plan_entry, TrainingPlanEntry):
            raise BatchTrainingError("plan_entry must be a TrainingPlanEntry")
        bindings = tuple(analysis_domain_bindings)
        if not bindings or not all(isinstance(item, AnalysisDomainBinding) for item in bindings):
            raise BatchTrainingError("analysis_domain_bindings must contain typed bindings")
        bindings = tuple(sorted(bindings, key=_binding_sort_key))
        if len({_binding_identity(item) for item in bindings}) != len(bindings):
            raise BatchTrainingError("analysis_domain_bindings must be unique")
        for binding in bindings:
            if (
                binding.project_entry_id,
                binding.sample_id,
                binding.slide_id,
            ) != (
                plan_entry.project_entry_id,
                plan_entry.sample_id,
                plan_entry.slide_id,
            ):
                raise BatchTrainingError("analysis_domain_bindings are foreign to the plan entry")
        owner_rows = tuple(rows)
        if not all(type(item) is TrainingEvidenceSnapshot for item in owner_rows):
            raise BatchTrainingError("entry-state rows must be typed owner rows")
        owner_rows = tuple(sorted(owner_rows, key=_evidence_row_sort_key))
        binding_row_identities = {_binding_row_identity(item) for item in bindings}
        for row in owner_rows:
            if (row.project_entry_id, row.split) != (
                plan_entry.project_entry_id,
                plan_entry.split,
            ):
                raise BatchTrainingError("entry-state row is foreign to its plan role")
            if _evidence_row_binding_identity(row) not in binding_row_identities:
                raise BatchTrainingError(
                    "entry-state row has no exact analysis-domain binding contribution"
                )
        binding_mappings = [item.to_mapping() for item in bindings]
        unsigned = {
            "project_entry_id": plan_entry.project_entry_id,
            "sample_id": plan_entry.sample_id,
            "slide_id": plan_entry.slide_id,
            "subject_id": plan_entry.subject_id,
            "split": plan_entry.split,
            "image_identity_sha256": (
                plan_entry.image_identity_sha256
                if image_identity_sha256 is None
                else image_identity_sha256
            ),
            "analysis_domain_binding_sha256s": tuple(
                sorted(_digest(item) for item in binding_mappings)
            ),
            "analysis_domain_binding_set_sha256": _digest(binding_mappings),
            "source_predecessor_artifact_ref_sha256s": tuple(
                sorted(source_predecessor_artifact_ref_sha256s)
            ),
            "saved_state_before_sha256": saved_state_before_sha256,
            "saved_state_after_sha256": saved_state_after_sha256,
            "post_binding_saved_state_sha256": post_binding_saved_state_sha256,
            "entry_snapshot_set_sha256": _digest([item.to_mapping() for item in owner_rows]),
        }
        return cls(**unsigned, entry_state_sha256=_digest(unsigned))

    @classmethod
    def from_mapping(cls, value: object) -> "TrainingEvidenceEntryState":
        fields = frozenset(cls.__dataclass_fields__)
        mapping = _closed_mapping(value, fields=fields, field="training_entry_state")
        return cls(
            project_entry_id=mapping["project_entry_id"],  # type: ignore[arg-type]
            sample_id=mapping["sample_id"],  # type: ignore[arg-type]
            slide_id=mapping["slide_id"],  # type: ignore[arg-type]
            subject_id=mapping["subject_id"],  # type: ignore[arg-type]
            split=mapping["split"],  # type: ignore[arg-type]
            image_identity_sha256=mapping["image_identity_sha256"],  # type: ignore[arg-type]
            analysis_domain_binding_sha256s=tuple(
                _array(
                    mapping["analysis_domain_binding_sha256s"],
                    "analysis_domain_binding_sha256s",
                )
            ),  # type: ignore[arg-type]
            analysis_domain_binding_set_sha256=mapping[
                "analysis_domain_binding_set_sha256"
            ],  # type: ignore[arg-type]
            source_predecessor_artifact_ref_sha256s=tuple(
                _array(
                    mapping["source_predecessor_artifact_ref_sha256s"],
                    "source_predecessor_artifact_ref_sha256s",
                )
            ),  # type: ignore[arg-type]
            saved_state_before_sha256=mapping["saved_state_before_sha256"],  # type: ignore[arg-type]
            saved_state_after_sha256=mapping["saved_state_after_sha256"],  # type: ignore[arg-type]
            post_binding_saved_state_sha256=mapping[
                "post_binding_saved_state_sha256"
            ],  # type: ignore[arg-type]
            entry_snapshot_set_sha256=mapping[
                "entry_snapshot_set_sha256"
            ],  # type: ignore[arg-type]
            entry_state_sha256=mapping["entry_state_sha256"],  # type: ignore[arg-type]
        )

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            "project_entry_id": self.project_entry_id,
            "sample_id": self.sample_id,
            "slide_id": self.slide_id,
            "subject_id": self.subject_id,
            "split": self.split,
            "image_identity_sha256": self.image_identity_sha256,
            "analysis_domain_binding_sha256s": list(self.analysis_domain_binding_sha256s),
            "analysis_domain_binding_set_sha256": (self.analysis_domain_binding_set_sha256),
            "source_predecessor_artifact_ref_sha256s": list(
                self.source_predecessor_artifact_ref_sha256s
            ),
            "saved_state_before_sha256": self.saved_state_before_sha256,
            "saved_state_after_sha256": self.saved_state_after_sha256,
            "post_binding_saved_state_sha256": self.post_binding_saved_state_sha256,
            "entry_snapshot_set_sha256": self.entry_snapshot_set_sha256,
        }

    def to_mapping(self) -> dict[str, object]:
        return {**self._unsigned_mapping(), "entry_state_sha256": self.entry_state_sha256}


@dataclass(frozen=True, slots=True)
class MultiImageTrainingEvidence:
    training_plan_id: str
    normalized_manifest_sha256: str
    entry_states: tuple[TrainingEvidenceEntryState, ...]
    rows: tuple[TrainingEvidenceSnapshot, ...]
    material_evidence_sha256: str
    publication_audit_sha256: str | None
    evidence_set_sha256: str

    def __post_init__(self) -> None:
        """Validate the MultiImageTrainingEvidence fields when the record is constructed.

        The checks cover ``training_plan_id``, ``normalized_manifest_sha256``,
        ``entry_states``, ``rows``, ``evidence_set_sha256``, ``material_evidence_sha256``.
        Invalid field types, values or combinations raise the declared validation error
        before this instance can be used by its caller."""

        plan_id = _sha256(self.training_plan_id, "training_plan_id")
        manifest_sha = _sha256(self.normalized_manifest_sha256, "normalized_manifest_sha256")
        states = tuple(self.entry_states)
        if not states or not all(type(item) is TrainingEvidenceEntryState for item in states):
            raise BatchTrainingError("entry_states must contain typed entry states")
        if states != tuple(sorted(states, key=_evidence_state_sort_key)):
            raise BatchTrainingError("entry_states must use canonical order")
        state_roles = tuple((item.project_entry_id, item.split) for item in states)
        if len(set(state_roles)) != len(state_roles):
            raise BatchTrainingError("entry_states contain duplicate role coverage")
        rows = tuple(self.rows)
        if not all(type(item) is TrainingEvidenceSnapshot for item in rows):
            raise BatchTrainingError("rows must contain typed owner rows")
        if rows != tuple(sorted(rows, key=_evidence_row_sort_key)):
            raise BatchTrainingError("rows must use canonical order")
        row_mappings = [item.to_mapping() for item in rows]
        if len({_canonical_bytes(item) for item in row_mappings}) != len(rows):
            raise BatchTrainingError("training evidence contains a duplicate complete row")
        annotation_roles = tuple(
            (item.project_entry_id, item.split, item.annotation_uuid) for item in rows
        )
        if len(set(annotation_roles)) != len(annotation_roles):
            raise BatchTrainingError(
                "training evidence contains a duplicate entry-scoped annotation"
            )
        states_by_role = dict(zip(state_roles, states, strict=True))
        for row in rows:
            if (row.project_entry_id, row.split) not in states_by_role:
                raise BatchTrainingError("row has no exact entry-state contribution")
        for role, state in states_by_role.items():
            role_rows = [
                item.to_mapping() for item in rows if (item.project_entry_id, item.split) == role
            ]
            if state.entry_snapshot_set_sha256 != _digest(role_rows):
                raise BatchTrainingError(
                    "entry_snapshot_set_sha256 does not match its exact owner rows"
                )
        evidence_set_sha = _sha256(self.evidence_set_sha256, "evidence_set_sha256")
        if evidence_set_sha != _digest(row_mappings):
            raise BatchTrainingError("evidence_set_sha256 is not canonical")
        publication_sha = (
            None
            if self.publication_audit_sha256 is None
            else _sha256(self.publication_audit_sha256, "publication_audit_sha256")
        )
        object.__setattr__(self, "training_plan_id", plan_id)
        object.__setattr__(self, "normalized_manifest_sha256", manifest_sha)
        object.__setattr__(self, "entry_states", states)
        object.__setattr__(self, "rows", rows)
        object.__setattr__(self, "publication_audit_sha256", publication_sha)
        object.__setattr__(self, "evidence_set_sha256", evidence_set_sha)
        material_sha = _sha256(self.material_evidence_sha256, "material_evidence_sha256")
        if material_sha != _digest(self._material_mapping()):
            raise BatchTrainingError("material_evidence_sha256 is not canonical")
        object.__setattr__(self, "material_evidence_sha256", material_sha)

    @classmethod
    def create(
        cls,
        *,
        plan: "TrainingPlan",
        entry_states: tuple[TrainingEvidenceEntryState, ...],
        rows: tuple[TrainingEvidenceSnapshot, ...],
        publication_audit_sha256: str | None,
    ) -> "MultiImageTrainingEvidence":
        if not isinstance(plan, TrainingPlan):
            raise BatchTrainingError("plan must be a TrainingPlan")
        states = tuple(entry_states)
        if not all(type(item) is TrainingEvidenceEntryState for item in states):
            raise BatchTrainingError("entry_states must contain typed entry states")
        states = tuple(sorted(states, key=_evidence_state_sort_key))
        state_roles = tuple((item.project_entry_id, item.split) for item in states)
        if len(set(state_roles)) != len(state_roles):
            raise BatchTrainingError("entry_states contain duplicate role coverage")
        plan_roles = {(item.project_entry_id, item.split) for item in plan.request.entries}
        if set(state_roles) != plan_roles:
            raise BatchTrainingError("entry states do not provide exact plan role coverage")
        owner_rows = tuple(rows)
        if not all(type(item) is TrainingEvidenceSnapshot for item in owner_rows):
            raise BatchTrainingError("rows must contain typed owner rows")
        owner_rows = tuple(sorted(owner_rows, key=_evidence_row_sort_key))
        evidence_set_sha = _digest([item.to_mapping() for item in owner_rows])
        unsigned = {
            "training_plan_id": plan.training_plan_id,
            "normalized_manifest_sha256": plan.normalized_manifest_sha256,
            "entry_states": states,
            "rows": owner_rows,
            "evidence_set_sha256": evidence_set_sha,
            "publication_audit_sha256": publication_audit_sha256,
        }
        prototype = object.__new__(cls)
        for name, item in unsigned.items():
            object.__setattr__(prototype, name, item)
        material_sha = _digest(prototype._material_mapping())
        result = cls(**unsigned, material_evidence_sha256=material_sha)
        _validate_evidence_plan(result, plan)
        return result

    @classmethod
    def from_mapping(
        cls,
        value: object,
        *,
        plan: "TrainingPlan | None" = None,
    ) -> "MultiImageTrainingEvidence":
        fields = frozenset(cls.__dataclass_fields__)
        mapping = _closed_mapping(value, fields=fields, field="training_evidence")
        result = cls(
            training_plan_id=mapping["training_plan_id"],  # type: ignore[arg-type]
            normalized_manifest_sha256=mapping[
                "normalized_manifest_sha256"
            ],  # type: ignore[arg-type]
            entry_states=tuple(
                TrainingEvidenceEntryState.from_mapping(item)
                for item in _array(mapping["entry_states"], "entry_states")
            ),
            rows=tuple(_training_evidence_row(item) for item in _array(mapping["rows"], "rows")),
            material_evidence_sha256=mapping["material_evidence_sha256"],  # type: ignore[arg-type]
            publication_audit_sha256=mapping["publication_audit_sha256"],  # type: ignore[arg-type]
            evidence_set_sha256=mapping["evidence_set_sha256"],  # type: ignore[arg-type]
        )
        if plan is not None:
            _validate_evidence_plan(result, plan)
        return result

    def _material_mapping(self) -> dict[str, object]:
        return {
            "training_plan_id": self.training_plan_id,
            "normalized_manifest_sha256": self.normalized_manifest_sha256,
            "entry_states": [item.to_mapping() for item in self.entry_states],
            "evidence_set_sha256": self.evidence_set_sha256,
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            "training_plan_id": self.training_plan_id,
            "normalized_manifest_sha256": self.normalized_manifest_sha256,
            "entry_states": [item.to_mapping() for item in self.entry_states],
            "rows": [item.to_mapping() for item in self.rows],
            "material_evidence_sha256": self.material_evidence_sha256,
            "publication_audit_sha256": self.publication_audit_sha256,
            "evidence_set_sha256": self.evidence_set_sha256,
        }


@dataclass(frozen=True, slots=True)
class PixelTrainingPlanEntryEvidence:
    """One plan's complete evidence contribution from one project entry."""

    training_plan_id: str
    classifier_role: str
    entry_state: TrainingEvidenceEntryState
    rows: tuple[TrainingEvidenceSnapshot, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "training_plan_id", _sha256(self.training_plan_id, "training_plan_id")
        )
        if self.classifier_role not in {
            ClassifierKind.PRIMARY_COMPARTMENT.value,
            ClassifierKind.SECONDARY_STRUCTURE.value,
        }:
            raise BatchTrainingError("pixel entry evidence has a non-pixel role")
        if type(self.entry_state) is not TrainingEvidenceEntryState:
            raise BatchTrainingError("pixel entry evidence requires one typed entry state")
        rows = tuple(sorted(tuple(self.rows), key=_evidence_row_sort_key))
        if not all(type(item) is TrainingEvidenceSnapshot for item in rows):
            raise BatchTrainingError("pixel entry evidence rows must be owner rows")
        if any(
            (item.project_entry_id, item.split)
            != (self.entry_state.project_entry_id, self.entry_state.split)
            for item in rows
        ):
            raise BatchTrainingError("pixel entry evidence rows cross their entry role")
        if self.entry_state.entry_snapshot_set_sha256 != _digest(
            [item.to_mapping() for item in rows]
        ):
            raise BatchTrainingError("pixel entry snapshot inventory differs from its state")
        object.__setattr__(self, "rows", rows)

    def to_mapping(self) -> dict[str, object]:
        return {
            "training_plan_id": self.training_plan_id,
            "classifier_role": self.classifier_role,
            "entry_state": self.entry_state.to_mapping(),
            "rows": [item.to_mapping() for item in self.rows],
        }


@dataclass(frozen=True, slots=True, init=False)
class PixelTrainingEvidenceSet:
    """Issuer-only complete plan-by-domain evidence for one project entry."""

    project_entry_id: str
    sample_id: str
    slide_id: str
    validation_domain_id: str
    training_plan_set_sha256: str
    plan_evidence: tuple[PixelTrainingPlanEntryEvidence, ...]
    analysis_domain_bindings: tuple[AnalysisDomainBinding, ...]
    object_output: Mapping[str, object]
    execution_exchange_sha256: str
    validated_exchange_sha256: str
    _plan_set: object
    _state_head_sha256: str
    _active_plan_sha256: str
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("PixelTrainingEvidenceSet is issuer-only")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("PixelTrainingEvidenceSet cannot be subclassed")

    def __copy__(self) -> object:
        raise TypeError("PixelTrainingEvidenceSet is nonserializable")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("PixelTrainingEvidenceSet is nonserializable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("PixelTrainingEvidenceSet is nonserializable")


def _validate_pixel_training_evidence_set(
    value: object,
    *,
    expected_plan_set: object | None = None,
) -> PixelTrainingEvidenceSet:
    """Validate pixel training evidence set for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    from .planning import ValidatedTrainingPlanSetProjection

    if (
        type(value) is not PixelTrainingEvidenceSet
        or value._issuer is not _PIXEL_ENTRY_ISSUER
        or type(value._plan_set) is not ValidatedTrainingPlanSetProjection
    ):
        raise BatchTrainingError("pixel entry evidence authority is invalid")
    plan_set = value._plan_set
    if expected_plan_set is not None and plan_set is not expected_plan_set:
        raise BatchTrainingError("pixel entry evidence uses a foreign plan authority")
    if (
        value.training_plan_set_sha256 != plan_set.plan_set.training_plan_set_sha256
        or value.validation_domain_id != plan_set.validation_domain_id
        or value._state_head_sha256 != plan_set.state_head_sha256
        or value._active_plan_sha256 != plan_set.active_plan_sha256
    ):
        raise BatchTrainingError("pixel entry evidence plan authority was mutated")
    selected: dict[
        tuple[str, str],
        tuple[
            TrainingPlan,
            TrainingPlanEntry,
            tuple[AnalysisDomainBinding, ...],
        ],
    ] = {}
    for plan in plan_set.plan_set.plans:
        entries = tuple(
            item for item in plan.request.entries if item.project_entry_id == value.project_entry_id
        )
        if not entries:
            continue
        bindings = tuple(
            item
            for item in plan.request.analysis_domain_bindings
            if item.project_entry_id == value.project_entry_id
        )
        for entry in entries:
            key = (plan.training_plan_id, entry.split)
            if key in selected:
                raise BatchTrainingError("pixel entry evidence has duplicate plan role")
            selected[key] = (plan, entry, bindings)
    values = tuple(value.plan_evidence)
    value_keys = tuple((item.training_plan_id, item.entry_state.split) for item in values)
    if not all(type(item) is PixelTrainingPlanEntryEvidence for item in values) or len(
        set(value_keys)
    ) != len(values):
        raise BatchTrainingError("pixel entry evidence plan values are invalid")
    roles = {item.classifier_role for item in values}
    expected = {
        key: item
        for key, item in selected.items()
        if item[0].request.classifier_kind.value in roles
    }
    if not values or set(expected) != set(value_keys):
        raise BatchTrainingError("pixel entry evidence plan coverage is not exact")
    for item in values:
        plan, entry, bindings = expected[(item.training_plan_id, item.entry_state.split)]
        if (
            item.classifier_role != plan.request.classifier_kind.value
            or (
                item.entry_state.project_entry_id,
                item.entry_state.sample_id,
                item.entry_state.slide_id,
                item.entry_state.subject_id,
                item.entry_state.split,
                item.entry_state.image_identity_sha256,
            )
            != (
                entry.project_entry_id,
                entry.sample_id,
                entry.slide_id,
                entry.subject_id,
                entry.split,
                entry.image_identity_sha256,
            )
            or item.entry_state.analysis_domain_binding_sha256s
            != tuple(sorted(_digest(binding.to_mapping()) for binding in bindings))
        ):
            raise BatchTrainingError("pixel entry evidence differs from its exact plan role")
    expected_bindings = tuple(
        sorted(
            {binding for _plan, _entry, bindings in expected.values() for binding in bindings},
            key=_binding_sort_key,
        )
    )
    if value.analysis_domain_bindings != expected_bindings:
        raise BatchTrainingError("pixel entry evidence lacks exact planned domains")
    output = _closed_mapping(
        value.object_output,
        fields=frozenset({"schema_version", "project_entry_id", "plan_domain_results"}),
        field="pixel_training_evidence_entry",
    )
    members = tuple(
        _closed_mapping(
            item,
            fields=frozenset({"training_plan_id", "classifier_role", "snapshot_set"}),
            field="pixel plan-domain result",
        )
        for item in _array(output["plan_domain_results"], "pixel plan-domain results")
    )
    member_keys = tuple(
        (
            item["training_plan_id"],
            item["classifier_role"],
            (
                item["snapshot_set"].get("split")
                if isinstance(item["snapshot_set"], Mapping)
                else None
            ),
            (
                item["snapshot_set"].get("analysis_domain_id")
                if isinstance(item["snapshot_set"], Mapping)
                else None
            ),
            (
                item["snapshot_set"].get("analysis_domain_revision_sha256")
                if isinstance(item["snapshot_set"], Mapping)
                else None
            ),
        )
        for item in members
    )
    expected_member_keys = {
        (
            item.training_plan_id,
            item.classifier_role,
            item.entry_state.split,
            binding.analysis_domain_id,
            binding.analysis_domain_revision_sha256,
        )
        for item in values
        for binding in expected[(item.training_plan_id, item.entry_state.split)][2]
    }
    if (
        output["schema_version"] != "pixel-training-evidence-entry/v1"
        or output["project_entry_id"] != value.project_entry_id
        or not members
        or len(member_keys) != len(set(member_keys))
        or set(member_keys) != expected_member_keys
    ):
        raise BatchTrainingError("pixel entry output plan coverage is invalid")
    return value


def _issue_pixel_training_evidence_set(
    *,
    plan_set: object,
    project_entry_id: str,
    sample_id: str,
    slide_id: str,
    plan_evidence: tuple[PixelTrainingPlanEntryEvidence, ...],
    analysis_domain_bindings: tuple[AnalysisDomainBinding, ...],
    object_output: Mapping[str, object],
    execution_exchange_sha256: str,
    validated_exchange_sha256: str,
) -> PixelTrainingEvidenceSet:
    from .planning import ValidatedTrainingPlanSetProjection

    if type(plan_set) is not ValidatedTrainingPlanSetProjection:
        raise BatchTrainingError("pixel entry evidence requires a plan authority")
    values = tuple(
        sorted(
            plan_evidence,
            key=lambda item: (
                item.training_plan_id,
                _SPLIT_RANK[item.entry_state.split],
            ),
        )
    )
    bindings = tuple(sorted(analysis_domain_bindings, key=_binding_sort_key))
    frozen_output = _freeze_json(object_output, "pixel_training_evidence_entry")
    if not isinstance(frozen_output, Mapping):
        raise BatchTrainingError("pixel entry output must be an object")
    authority = object.__new__(PixelTrainingEvidenceSet)
    fields = {
        "project_entry_id": _text(project_entry_id, "project_entry_id"),
        "sample_id": _text(sample_id, "sample_id"),
        "slide_id": _text(slide_id, "slide_id"),
        "validation_domain_id": plan_set.validation_domain_id,
        "training_plan_set_sha256": plan_set.plan_set.training_plan_set_sha256,
        "plan_evidence": values,
        "analysis_domain_bindings": bindings,
        "object_output": frozen_output,
        "execution_exchange_sha256": _sha256(
            execution_exchange_sha256, "execution_exchange_sha256"
        ),
        "validated_exchange_sha256": _sha256(
            validated_exchange_sha256, "validated_exchange_sha256"
        ),
        "_plan_set": plan_set,
        "_state_head_sha256": plan_set.state_head_sha256,
        "_active_plan_sha256": plan_set.active_plan_sha256,
        "_issuer": _PIXEL_ENTRY_ISSUER,
    }
    for name, item in fields.items():
        object.__setattr__(authority, name, item)
    return _validate_pixel_training_evidence_set(authority, expected_plan_set=plan_set)


def parse_pixel_training_snapshot_set(
    value: object,
    *,
    project_entry_id: str,
    analysis_domain_id: str,
    analysis_domain_revision: int,
    analysis_domain_revision_sha256: str,
    domain_set_sha256: str,
    split: str,
    expected_evidence_sha256s: tuple[str, ...] = (),
) -> tuple[TrainingEvidenceSnapshot, ...]:
    """Parse the existing v2 per-domain body for both workflow owners."""

    fields = frozenset(
        {
            "schema_version",
            "project_entry_id",
            "analysis_domain_id",
            "analysis_domain_revision",
            "analysis_domain_revision_sha256",
            "domain_set_sha256",
            "split",
            "snapshots",
            "snapshot_count",
            "snapshot_set_sha256",
            "live_geometry_authority",
            "biological_sufficiency_claimed",
        }
    )
    diagnostic_fields = (
        {"observed_annotation_class_counts"}
        if isinstance(value, Mapping) and "observed_annotation_class_counts" in value
        else set()
    )
    output = _closed_mapping(value, fields=fields | diagnostic_fields, field="pixel snapshot set")
    if diagnostic_fields:
        counts = output["observed_annotation_class_counts"]
        if not isinstance(counts, Mapping) or any(
            not isinstance(name, str) or not name.strip()
            or type(count) is not int or count <= 0
            for name, count in counts.items()
        ):
            raise BatchTrainingError("Observed annotation class counts are invalid.")
    rows = _array(output["snapshots"], "pixel snapshot rows")
    if (
        output["schema_version"] != "training-evidence-snapshot-set/v2"
        or output["project_entry_id"] != project_entry_id
        or output["analysis_domain_id"] != analysis_domain_id
        or output["analysis_domain_revision"] != analysis_domain_revision
        or output["analysis_domain_revision_sha256"] != analysis_domain_revision_sha256
        or output["domain_set_sha256"] != domain_set_sha256
        or output["split"] != split
        or output["snapshot_count"] != len(rows)
        or output["snapshot_set_sha256"] != _digest(list(rows))
        or output["live_geometry_authority"] != "qpdata"
        or output["biological_sufficiency_claimed"] is not False
    ):
        raise BatchTrainingError("pixel snapshot-set identity is invalid")
    parsed = tuple(
        sorted(
            (_training_evidence_row(item) for item in rows),
            key=_evidence_row_sort_key,
        )
    )
    if any(
        item.project_entry_id != project_entry_id
        or item.split != split
        or item.analysis_domain_id != analysis_domain_id
        or item.analysis_domain_revision_sha256 != analysis_domain_revision_sha256
        or item.domain_set_sha256 != domain_set_sha256
        for item in parsed
    ):
        raise BatchTrainingError("pixel snapshot row escaped its exact entry/domain")
    expected = tuple(expected_evidence_sha256s)
    if expected and tuple(sorted(item.evidence_sha256 for item in parsed)) != expected:
        raise BatchTrainingError("live pixel evidence differs from its requested inventory")
    return parsed


def pixel_training_request_projection(
    resolved_config: object,
    *,
    accepted_primary_settings: PrimaryClassifierSettings | None = None,
) -> dict[str, tuple[str, tuple[dict[str, str], ...], tuple[str, ...]]]:
    """Return pixel request scopes from admission and accepted primary settings."""

    from mif_pipeline.pipeline_core import AnalysisConfig

    if type(resolved_config) is not AnalysisConfig:
        raise BatchTrainingError("pixel evidence requires one resolved AnalysisConfig")
    if (
        accepted_primary_settings is not None
        and type(accepted_primary_settings) is not PrimaryClassifierSettings
    ):
        raise BatchTrainingError("accepted primary settings are invalid")
    original = (
        None
        if resolved_config.primary_classifier is None
        else PrimaryClassifierSettings(
            resolved_config.primary_classes,
            resolved_config.primary_classifier,
        )
    )
    if accepted_primary_settings is not None and original is not None:
        try:
            require_primary_settings_compatible(original, accepted_primary_settings)
        except ValueError as exc:
            raise BatchTrainingError("accepted primary settings differ from admission") from exc
    primary = accepted_primary_settings or original
    result: dict[str, tuple[str, tuple[dict[str, str], ...], tuple[str, ...]]] = {}
    if primary is not None:
        primary_labels = tuple(
            {"class_id": item.class_id, "path_class_id": item.display_name}
            for item in primary.primary_classes
        )
        if primary.primary_classifier.require_ignore_training:
            primary_labels = (*primary_labels, {"class_id": "Ignore", "path_class_id": "Ignore"})
        result[primary.primary_classifier.classifier_id] = (
            ClassifierKind.PRIMARY_COMPARTMENT.value,
            primary_labels,
            ("AnalysisDomain",),
        )
    # An unanswered structure choice is later than primary reference capture.
    # Secondary references use the accepted plan's exact output/parent scope.
    from mif_pipeline.pipeline_core.config import secondary_classifier_groups, secondary_classifier_output_classes
    for classifier_id, structures in secondary_classifier_groups(resolved_config.secondary_structures or ()).items():
        if classifier_id in result:
            raise BatchTrainingError("pixel classifier configuration is ambiguous")
        labels = (structures[0].classifier.output_classes or secondary_classifier_output_classes((structures[0].structure_id,), joint=False))
        result[classifier_id] = (
            ClassifierKind.SECONDARY_STRUCTURE.value,
            tuple({"class_id": label, "path_class_id": label} for label in labels),
            tuple(structures[0].eligible_parent_ids),
        )
    return result


def project_pixel_training_evidence_request(
    plan: TrainingPlan,
    resolved_config: object,
) -> tuple[str, tuple[dict[str, str], ...], tuple[str, ...]]:
    """Project the saved reference classes required by the accepted pixel plan."""

    if (plan.request.classifier_kind is ClassifierKind.SECONDARY_STRUCTURE
            and resolved_config.secondary_structures is None):
        classes = plan.request.required_classes
        candidates = tuple(item.removeprefix("Candidate_") for item in classes if item.startswith("Candidate_"))
        if (not candidates or not plan.request.eligible_parent_classes):
            raise BatchTrainingError("secondary reference scope differs from its accepted plan")
        return (
            ClassifierKind.SECONDARY_STRUCTURE.value,
            tuple({"class_id": item, "path_class_id": item} for item in classes),
            plan.request.eligible_parent_classes,
        )

    configured = pixel_training_request_projection(
        resolved_config,
        accepted_primary_settings=plan.request.primary_settings,
    )
    projection = configured.get(plan.request.classifier_id)
    if projection is None:
        raise BatchTrainingError("pixel training plan classifier is not configured")
    role, labels, parents = projection
    if plan.request.classifier_kind is ClassifierKind.PRIMARY_COMPARTMENT:
        required = primary_training_required_classes(plan.request.primary_settings)
        if tuple(sorted(item["class_id"] for item in labels)) != required or required != plan.request.required_classes:
            raise BatchTrainingError("primary training labels differ from the plan")
        if role != ClassifierKind.PRIMARY_COMPARTMENT.value:
            raise BatchTrainingError("primary training plan differs from resolved config")
        return role, labels, parents
    if plan.request.classifier_kind is ClassifierKind.SECONDARY_STRUCTURE:
        if tuple(sorted(item["class_id"] for item in labels)) != plan.request.required_classes:
            raise BatchTrainingError("secondary training labels differ from the plan")
        if role != ClassifierKind.SECONDARY_STRUCTURE.value:
            raise BatchTrainingError("secondary training plan differs from resolved config")
        return role, labels, parents
    raise BatchTrainingError("supervised plans cannot open pixel evidence")


# Kept private alias for the already-accepted focused owner tests.
_pixel_plan_request_projection = project_pixel_training_evidence_request


def _pixel_revision_by_binding(
    accepted_domains: tuple[object, ...],
) -> dict[AnalysisDomainBinding, object]:
    from ..accepted_analysis_domain_records import (
        ValidatedAcceptedAnalysisDomainProjection,
    )

    result: dict[AnalysisDomainBinding, object] = {}
    for accepted in accepted_domains:
        if type(accepted) is not ValidatedAcceptedAnalysisDomainProjection:
            raise BatchTrainingError("pixel evidence domains must be owner projections")
        bindings = accepted.artifact_ref.scope.analysis_domain_bindings
        authorities = accepted.domain_authorities
        if len(bindings) != len(authorities):
            raise BatchTrainingError("accepted domain bindings and authorities differ")
        authorities_by_identity = {
            (
                item.project_entry_id,
                item.slide_id,
                item.analysis_domain_id,
                item.analysis_domain_revision,
                item.analysis_domain_revision_sha256,
                accepted.domain_set.domain_set_sha256,
            ): item
            for item in authorities
        }
        for binding in bindings:
            key = (
                binding.project_entry_id,
                binding.slide_id,
                binding.analysis_domain_id,
                binding.analysis_domain_revision,
                binding.analysis_domain_revision_sha256,
                binding.domain_set_sha256,
            )
            authority = authorities_by_identity.get(key)
            if authority is None or binding in result:
                raise BatchTrainingError("accepted domain coverage is missing or duplicate")
            result[binding] = authority
    return result


def _pixel_primary_coverage(classifier_kind, expected_bindings, primary_results, primary_skips):
    """Each secondary reference domain needs its accepted primary choice."""
    from .planning import validate_primary_skip_artifact_ref

    covered = [binding for item in primary_results for binding in item.analysis_domain_bindings]
    for item in primary_skips:
        selected = tuple(binding for binding in expected_bindings
                         if binding in item.artifact_ref.scope.analysis_domain_bindings)
        validate_primary_skip_artifact_ref(item.artifact_ref, domain_bindings=selected)
        covered.extend(selected)
    if (classifier_kind is ClassifierKind.PRIMARY_COMPARTMENT and covered) or (
            classifier_kind is ClassifierKind.SECONDARY_STRUCTURE
            and tuple(sorted(covered, key=_binding_sort_key)) != expected_bindings):
        raise BatchTrainingError("pixel evidence primary-choice coverage differs from its plan domains")


def open_pixel_training_evidence_set(
    *,
    plan_set: object,
    accepted_domains: tuple[object, ...],
    execution_evidence: object,
    primary_results: tuple[object, ...] = (),
    primary_skips: tuple[object, ...] = (),
) -> PixelTrainingEvidenceSet:
    """Open one exact project-entry plan-by-domain pixel evidence envelope."""

    from mif_pipeline.analysis_domain import AnalysisDomainRevision
    from mif_pipeline.pipeline_core import AnalysisConfig, StageStatus

    from ..accepted_analysis_domain_records import (
        ValidatedAcceptedAnalysisDomainProjection,
    )
    from ..batch_classifier_inference_artifacts import (
        ValidatedPrimaryClassifierInferenceResultProjection,
    )
    from ..project_stage_plan import ProjectStageTargetExecutionEvidence
    from .planning import ValidatedTrainingPlanSetProjection

    if (
        type(plan_set) is not ValidatedTrainingPlanSetProjection
        or type(accepted_domains) is not tuple
        or not accepted_domains
        or not all(
            type(item) is ValidatedAcceptedAnalysisDomainProjection for item in accepted_domains
        )
        or type(primary_results) is not tuple
        or not all(
            type(item) is ValidatedPrimaryClassifierInferenceResultProjection
            for item in primary_results
        )
        or type(primary_skips) is not tuple
        or any(type(item) is not BatchCommittedArtifactAuthority for item in primary_skips)
        or type(execution_evidence) is not ProjectStageTargetExecutionEvidence
    ):
        raise BatchTrainingError("pixel evidence requires exact owner-issued projections")
    try:
        validation_inputs = execution_evidence.reopen_validation_inputs()
        config = AnalysisConfig.from_mapping(_export_json(validation_inputs.resolved_config))
    except Exception as exc:
        raise BatchTrainingError("pixel evidence validation inputs are invalid") from exc
    authorized = execution_evidence.authorized
    observation = execution_evidence.observation
    result = execution_evidence.wire_stage_result
    entry_id = authorized.project_entry_id
    expected_kind = authorized.terminal_stage_id
    classifier_kind = {
        "primary_training_evidence_snapshot": ClassifierKind.PRIMARY_COMPARTMENT,
        "secondary_training_evidence_snapshot": ClassifierKind.SECONDARY_STRUCTURE,
    }.get(expected_kind)
    if classifier_kind is None:
        raise BatchTrainingError("pixel evidence has a foreign terminal owner")
    plans = tuple(
        sorted(
            (
                plan
                for plan in plan_set.plan_set.plans
                if plan.request.classifier_kind is classifier_kind
                and any(item.project_entry_id == entry_id for item in plan.request.entries)
            ),
            key=lambda item: item.training_plan_id,
        )
    )
    if not plans:
        raise BatchTrainingError("pixel evidence entry has no selected training plan")
    entries_by_plan: dict[str, tuple[TrainingPlanEntry, ...]] = {}
    bindings_by_plan: dict[str, tuple[AnalysisDomainBinding, ...]] = {}
    for plan in plans:
        entries = tuple(item for item in plan.request.entries if item.project_entry_id == entry_id)
        if not entries or len({item.split for item in entries}) != len(entries):
            raise BatchTrainingError("pixel evidence requires unique roles per plan/entry")
        bindings = tuple(
            item
            for item in plan.request.analysis_domain_bindings
            if item.project_entry_id == entry_id
        )
        if not bindings:
            raise BatchTrainingError("pixel evidence plan lacks entry domains")
        entries_by_plan[plan.training_plan_id] = entries
        bindings_by_plan[plan.training_plan_id] = bindings
    plan_entries = tuple(entry for entries in entries_by_plan.values() for entry in entries)
    if any(
        (item.sample_id, item.slide_id) != (authorized.sample_id, authorized.slide_id)
        for item in plan_entries
    ):
        raise BatchTrainingError("pixel evidence execution crosses its plan entry")
    expected_bindings = tuple(
        sorted(
            set(binding for values in bindings_by_plan.values() for binding in values),
            key=_binding_sort_key,
        )
    )
    _pixel_primary_coverage(classifier_kind, expected_bindings, primary_results, primary_skips)
    revisions = _pixel_revision_by_binding(accepted_domains)
    if tuple(sorted(revisions, key=_binding_sort_key)) != expected_bindings:
        raise BatchTrainingError("pixel evidence lacks exact accepted domain coverage")
    owners = (*accepted_domains, *primary_results, *primary_skips)
    if any(
        item.state_head_sha256 != plan_set.state_head_sha256
        or item.active_plan_sha256 != plan_set.active_plan_sha256
        for item in owners
    ):
        raise BatchTrainingError("pixel evidence owners use foreign state")
    source_refs = tuple(
        sorted(
            {
                plan_set.artifact_ref.artifact_ref_sha256,
                *(item.artifact_ref.artifact_ref_sha256 for item in owners),
            }
        )
    )
    if authorized.prerequisite_artifact_ref_sha256s != source_refs:
        raise BatchTrainingError("pixel evidence prerequisites differ from owners")
    if (
        authorized.analysis_domain_bindings != expected_bindings
        or authorized.image_role != "primary_mif"
        or authorized.wire_stage_id != "training_evidence_snapshot"
        or authorized.request_codec_id
        != "qupath/scripts/compartment_analysis/01_snapshot_training_annotations.groovy"
        or authorized.request_codec_version != "1"
        or observation.saved_state_disposition != "authenticated_unchanged"
        or observation.saved_state_after_sha256 != authorized.saved_state_before_sha256
        or result.status is not StageStatus.COMPLETED
        or result.stage != authorized.wire_stage_id
        or result.slide_id != authorized.slide_id
    ):
        raise BatchTrainingError("pixel evidence execution differs from its read-only codec")
    body = _closed_mapping(
        validation_inputs.scientific_request_body,
        fields=frozenset({"schema_version", "sample_id", "pixel_training_evidence_requests"}),
        field="pixel evidence request body",
    )
    if (
        body["schema_version"] != "roi-workflow-invocation/v1"
        or body["sample_id"] != authorized.sample_id
        or validation_inputs.scientific_request_body_sha256 != _digest(_export_json(body))
    ):
        raise BatchTrainingError("pixel evidence request body identity is invalid")
    raw_requests = _array(
        body["pixel_training_evidence_requests"],
        "pixel_training_evidence_requests",
    )
    request_fields = frozenset(
        {
            "training_plan_id",
            "classifier_role",
            "section_id",
            "generation_id",
            "generation_ids",
            "run_id",
            "analysis_domain_id",
            "analysis_domain_revision",
            "analysis_domain_revision_sha256",
            "analysis_domain_path_object_id",
            "analysis_domain_revision_evidence",
            "domain_set_sha256",
            "split",
            "training_labels",
            "eligible_parent_ids",
            "expected_evidence_sha256s",
        }
    )
    requests = tuple(
        _closed_mapping(item, fields=request_fields, field="pixel evidence request member")
        for item in raw_requests
    )
    request_keys = tuple(
        (
            item["training_plan_id"],
            item["classifier_role"],
            item["split"],
            item["analysis_domain_id"],
            item["analysis_domain_revision_sha256"],
        )
        for item in requests
    )
    if request_keys != tuple(sorted(request_keys)) or len(set(request_keys)) != len(request_keys):
        raise BatchTrainingError("pixel evidence request matrix is not canonical and unique")
    expected_request_by_key: dict[
        tuple[object, ...], tuple[TrainingPlan, TrainingPlanEntry, AnalysisDomainBinding]
    ] = {}
    for plan in plans:
        role, labels, parents = _pixel_plan_request_projection(plan, config)
        for entry in entries_by_plan[plan.training_plan_id]:
            for binding in bindings_by_plan[plan.training_plan_id]:
                key = (
                    plan.training_plan_id,
                    role,
                    entry.split,
                    binding.analysis_domain_id,
                    binding.analysis_domain_revision_sha256,
                )
                expected_request_by_key[key] = (plan, entry, binding)
                matches = tuple(
                    item
                    for item in requests
                    if (
                        item["training_plan_id"],
                        item["classifier_role"],
                        item["split"],
                        item["analysis_domain_id"],
                        item["analysis_domain_revision_sha256"],
                    )
                    == key
                )
                if len(matches) != 1:
                    raise BatchTrainingError("pixel evidence request coverage is not exact")
                member = matches[0]
                authority = revisions[binding]
                try:
                    revision = AnalysisDomainRevision.from_mapping(
                        _export_json(member["analysis_domain_revision_evidence"])
                    )
                except Exception as exc:
                    raise BatchTrainingError("pixel evidence domain revision is invalid") from exc
                expected_hashes = tuple(member["expected_evidence_sha256s"])
                if (
                    member["section_id"] != authority.section_id
                    or member["generation_id"] != authority.tissue_generation_id
                    or tuple(member["generation_ids"]) != (authority.tissue_generation_id,)
                    or member["run_id"] != authorized.pipeline_run_id
                    or member["analysis_domain_id"] != binding.analysis_domain_id
                    or member["analysis_domain_revision"] != binding.analysis_domain_revision
                    or member["analysis_domain_path_object_id"] != authority.source_path_object_id
                    or member["domain_set_sha256"] != binding.domain_set_sha256
                    or revision.content_sha256 != binding.analysis_domain_revision_sha256
                    or revision.analysis_domain_id != binding.analysis_domain_id
                    or revision.project_entry_id != entry_id
                    or tuple(_export_json(member["training_labels"])) != labels
                    or tuple(member["eligible_parent_ids"]) != parents
                    or expected_hashes != tuple(sorted(set(expected_hashes)))
                ):
                    raise BatchTrainingError("pixel evidence request differs from its owners")
    if set(request_keys) != set(expected_request_by_key):
        raise BatchTrainingError(
            "pixel evidence has a missing, duplicate, foreign, or extra member"
        )
    output = _closed_mapping(
        result.outputs,
        fields=frozenset({"schema_version", "project_entry_id", "plan_domain_results"}),
        field="pixel evidence entry output",
    )
    raw_results = _array(output["plan_domain_results"], "plan_domain_results")
    if (
        output["schema_version"] != "pixel-training-evidence-entry/v1"
        or output["project_entry_id"] != entry_id
        or len(raw_results) != len(requests)
    ):
        raise BatchTrainingError("pixel evidence entry output identity is invalid")
    rows_by_plan_role: dict[tuple[str, str], list[TrainingEvidenceSnapshot]] = {
        (plan.training_plan_id, entry.split): []
        for plan in plans
        for entry in entries_by_plan[plan.training_plan_id]
    }
    output_keys: list[tuple[object, ...]] = []
    for request_member, raw_output in zip(requests, raw_results, strict=True):
        member = _closed_mapping(
            raw_output,
            fields=frozenset({"training_plan_id", "classifier_role", "snapshot_set"}),
            field="pixel evidence entry result member",
        )
        key = (
            member["training_plan_id"],
            member["classifier_role"],
            request_member["split"],
            request_member["analysis_domain_id"],
            request_member["analysis_domain_revision_sha256"],
        )
        if (
            member["training_plan_id"] != request_member["training_plan_id"]
            or member["classifier_role"] != request_member["classifier_role"]
            or key not in expected_request_by_key
        ):
            raise BatchTrainingError("pixel evidence output member differs from its request")
        _plan, entry, binding = expected_request_by_key[key]
        rows_by_plan_role[(str(member["training_plan_id"]), entry.split)].extend(
            parse_pixel_training_snapshot_set(
                member["snapshot_set"],
                project_entry_id=entry_id,
                analysis_domain_id=binding.analysis_domain_id,
                analysis_domain_revision=binding.analysis_domain_revision,
                analysis_domain_revision_sha256=(binding.analysis_domain_revision_sha256),
                domain_set_sha256=binding.domain_set_sha256,
                split=entry.split,
                expected_evidence_sha256s=tuple(request_member["expected_evidence_sha256s"]),
            )
        )
        output_keys.append(key)
    if tuple(output_keys) != request_keys:
        raise BatchTrainingError("pixel evidence output coverage is not exact")
    plan_evidence = []
    for plan in plans:
        for entry in entries_by_plan[plan.training_plan_id]:
            rows = tuple(rows_by_plan_role[(plan.training_plan_id, entry.split)])
            state = TrainingEvidenceEntryState.create(
                plan_entry=entry,
                analysis_domain_bindings=bindings_by_plan[plan.training_plan_id],
                rows=rows,
                source_predecessor_artifact_ref_sha256s=source_refs,
                saved_state_before_sha256=authorized.saved_state_before_sha256,
                saved_state_after_sha256=observation.saved_state_after_sha256,
                post_binding_saved_state_sha256=observation.saved_state_after_sha256,
            )
            plan_evidence.append(
                PixelTrainingPlanEntryEvidence(
                    training_plan_id=plan.training_plan_id,
                    classifier_role=plan.request.classifier_kind.value,
                    entry_state=state,
                    rows=rows,
                )
            )
    return _issue_pixel_training_evidence_set(
        plan_set=plan_set,
        project_entry_id=entry_id,
        sample_id=authorized.sample_id,
        slide_id=authorized.slide_id,
        plan_evidence=tuple(plan_evidence),
        analysis_domain_bindings=expected_bindings,
        object_output=output,
        execution_exchange_sha256=execution_evidence.execution_exchange_sha256,
        validated_exchange_sha256=execution_evidence.validated_exchange_sha256,
    )


def _build_training_entry_execution_result(
    *,
    context: object,
    artifact_kind: str,
    project_entry_id: str,
    sample_id: str,
    slide_id: str,
    validation_domain_id: str,
    training_plan_set_sha256: str,
    analysis_domain_bindings: tuple[AnalysisDomainBinding, ...],
    entry_state_sha256: str,
    entry_snapshot_set_sha256: str,
    plan_role_rows: tuple[object, ...],
    saved_state_before_sha256: str,
    saved_state_after_sha256: str,
    post_binding_saved_state_sha256: str,
    object_output_sha256: str,
    object_output_count: int,
    pixel_plan_commitments: tuple[tuple[str, str], ...] | None = None,
) -> object:
    """Publish our admitted evidence commitments under the exact result context.

    i) We retain source, snapshot, role and saved-state identities as compact
    scalar metadata. Scientific rows stay with their source owners and exchange.
    ii) We bind the output to its attempt, domain scope and configuration before
    constructing the immutable artifact; retained-byte decoding remains separate.
    """

    from ..batch_analysis_preflight import MIF_PIPELINE_BATCH_RUNTIME_VERSION
    from ..batch_artifacts import (
        ArtifactOrigin,
        ArtifactScope,
        BatchArtifactRef,
        BatchNodeExecutionContext,
        BatchNodeExecutionResult,
    )
    from .stage_adapters import training_evidence_identity_contract

    if (
        type(context) is not BatchNodeExecutionContext
        or context.adapter_spec.node_kind != artifact_kind
        or tuple(context.adapter_spec.declared_outputs) != (artifact_kind,)
    ):
        raise BatchTrainingError("training evidence result requires its exact context")
    bindings = tuple(sorted(analysis_domain_bindings, key=_binding_sort_key))
    identity = {
        "analysis_domain_binding_set_sha256": _digest([item.to_mapping() for item in bindings]),
        "entry_snapshot_set_sha256": _sha256(
            entry_snapshot_set_sha256, "entry_snapshot_set_sha256"
        ),
        "entry_state_sha256": _sha256(entry_state_sha256, "entry_state_sha256"),
        "object_output_count": object_output_count,
        "object_output_sha256": _sha256(object_output_sha256, "object_output_sha256"),
        "plan_role_set_sha256": _digest(list(plan_role_rows)),
        "post_binding_saved_state_sha256": _sha256(
            post_binding_saved_state_sha256, "post_binding_saved_state_sha256"
        ),
        "project_entry_id": _text(project_entry_id, "project_entry_id"),
        "sample_id": _text(sample_id, "sample_id"),
        "saved_state_after_sha256": _sha256(saved_state_after_sha256, "saved_state_after_sha256"),
        "saved_state_before_sha256": _sha256(
            saved_state_before_sha256, "saved_state_before_sha256"
        ),
        "slide_id": _text(slide_id, "slide_id"),
        "training_plan_set_sha256": _sha256(training_plan_set_sha256, "training_plan_set_sha256"),
    }
    if type(object_output_count) is not int or object_output_count < 0:
        raise BatchTrainingError("training evidence object count is invalid")
    is_pixel = artifact_kind in {
        "primary_training_evidence_snapshot",
        "secondary_training_evidence_snapshot",
    }
    if is_pixel:
        commitments = tuple(pixel_plan_commitments or ())
        plan_ids = tuple(item[0] for item in commitments)
        commitment_sha256s = tuple(item[1] for item in commitments)
        if (
            not commitments
            or plan_ids != tuple(sorted(set(plan_ids)))
            or len(commitment_sha256s) != len(plan_ids)
        ):
            raise BatchTrainingError("pixel evidence requires canonical per-plan commitments")
        identity.update(
            training_plan_ids=plan_ids,
            plan_evidence_commitment_sha256s=commitment_sha256s,
        )
    elif pixel_plan_commitments is not None:
        raise BatchTrainingError("supervised evidence cannot declare pixel plan commitments")
    contract = training_evidence_identity_contract(artifact_kind)
    authorization = context.authorization
    configuration_bindings = tuple(
        item.configuration_bindings[0]
        for item in context.inputs
        if item.scope.sample_id == sample_id and len(item.configuration_bindings) == 1
    )
    configuration_bindings = tuple(
        sorted(set(configuration_bindings), key=lambda item: item.sample_id)
    )
    if len(configuration_bindings) != 1:
        raise BatchTrainingError("training evidence configuration binding is ambiguous")
    ref = BatchArtifactRef(
        artifact_id="training-evidence-" + _digest(identity)[:48],
        artifact_kind=artifact_kind,
        schema_name="roi-generation-manifest",
        schema_version=1,
        origin=ArtifactOrigin.INTERNAL_NODE,
        producer_node_id=context.node_id,
        producer_attempt_id=authorization.attempt_id,
        producer_attempt_authorization_sha256=authorization.authorization_sha256,
        external_declaration_sha256=None,
        external_validation_receipt_sha256=None,
        locator=None,
        sha256=None,
        byte_count=None,
        scope=ArtifactScope(
            validation_domain_id=validation_domain_id,
            sample_id=sample_id,
            slide_id=slide_id,
            project_entry_id=project_entry_id,
            analysis_domain_bindings=bindings,
        ),
        owning_identity=identity,
        identity_contract=contract,
        owning_identity_contract_sha256=contract.contract_sha256,
        producer_version="mif-pipeline-batch-training-evidence/v1",
        runtime_versions={"mif_pipeline": MIF_PIPELINE_BATCH_RUNTIME_VERSION},
        compatibility_signature_sha256=context.compatibility_signature_sha256,
        configuration_bindings=configuration_bindings,
    )
    return BatchNodeExecutionResult(
        authorization.authorization_sha256,
        (ref,),
    )


def _pixel_entry_state_set_sha256(
    states: tuple[TrainingEvidenceEntryState, ...],
) -> str:
    if not states:
        raise BatchTrainingError("pixel evidence child has no entry states")
    if len(states) == 1:
        return states[0].entry_state_sha256
    return _digest([item.to_mapping() for item in states])


def _pixel_plan_evidence_commitment_sha256(
    *,
    training_plan_id: str,
    classifier_role: str,
    states: tuple[TrainingEvidenceEntryState, ...],
    rows: tuple[TrainingEvidenceSnapshot, ...],
) -> str:
    owner_states = tuple(sorted(states, key=_evidence_state_sort_key))
    owner_rows = tuple(sorted(rows, key=_evidence_row_sort_key))
    role_rows = [
        {
            "training_plan_id": training_plan_id,
            "classifier_role": classifier_role,
            "split": item.split,
        }
        for item in owner_states
    ]
    return _digest(
        {
            "training_plan_id": _sha256(training_plan_id, "training_plan_id"),
            "classifier_role": classifier_role,
            "entry_state_sha256": _pixel_entry_state_set_sha256(owner_states),
            "entry_snapshot_set_sha256": _digest([item.to_mapping() for item in owner_rows]),
            "plan_role_set_sha256": _digest(role_rows),
        }
    )


def _pixel_plan_evidence_commitments(
    values: tuple[PixelTrainingPlanEntryEvidence, ...],
) -> tuple[tuple[str, str], ...]:
    by_plan: dict[str, list[PixelTrainingPlanEntryEvidence]] = {}
    for item in values:
        by_plan.setdefault(item.training_plan_id, []).append(item)
    commitments = []
    for plan_id in sorted(by_plan):
        members = tuple(by_plan[plan_id])
        roles = {item.classifier_role for item in members}
        if len(roles) != 1:
            raise BatchTrainingError("pixel plan evidence mixes classifier roles")
        commitments.append(
            (
                plan_id,
                _pixel_plan_evidence_commitment_sha256(
                    training_plan_id=plan_id,
                    classifier_role=next(iter(roles)),
                    states=tuple(item.entry_state for item in members),
                    rows=tuple(row for item in members for row in item.rows),
                ),
            )
        )
    return tuple(commitments)


def build_pixel_training_evidence_execution_result(
    *,
    context: object,
    evidence: PixelTrainingEvidenceSet,
) -> object:
    """Publish the compact per-entry identity; the exchange retains bytes."""

    evidence = _validate_pixel_training_evidence_set(evidence)
    roles_in_entry = {item.classifier_role for item in evidence.plan_evidence}
    artifact_kinds = {
        ClassifierKind.PRIMARY_COMPARTMENT.value: "primary_training_evidence_snapshot",
        ClassifierKind.SECONDARY_STRUCTURE.value: "secondary_training_evidence_snapshot",
    }
    if len(roles_in_entry) != 1 or next(iter(roles_in_entry)) not in artifact_kinds:
        raise BatchTrainingError("pixel entry evidence mixes classifier owners")
    saved_state_triples = {
        (
            item.entry_state.saved_state_before_sha256,
            item.entry_state.saved_state_after_sha256,
            item.entry_state.post_binding_saved_state_sha256,
        )
        for item in evidence.plan_evidence
    }
    if len(saved_state_triples) != 1:
        raise BatchTrainingError("pixel entry evidence mixes saved states")
    saved_before, saved_after, post_binding = next(iter(saved_state_triples))
    roles = tuple(
        {
            "training_plan_id": item.training_plan_id,
            "classifier_role": item.classifier_role,
            "split": item.entry_state.split,
        }
        for item in evidence.plan_evidence
    )
    return _build_training_entry_execution_result(
        context=context,
        artifact_kind=artifact_kinds[next(iter(roles_in_entry))],
        project_entry_id=evidence.project_entry_id,
        sample_id=evidence.sample_id,
        slide_id=evidence.slide_id,
        validation_domain_id=evidence.validation_domain_id,
        training_plan_set_sha256=evidence.training_plan_set_sha256,
        analysis_domain_bindings=evidence.analysis_domain_bindings,
        entry_state_sha256=_pixel_entry_state_set_sha256(
            tuple(item.entry_state for item in evidence.plan_evidence)
        ),
        entry_snapshot_set_sha256=_digest([
            row.to_mapping() for item in evidence.plan_evidence for row in item.rows
        ]),
        plan_role_rows=roles,
        saved_state_before_sha256=saved_before,
        saved_state_after_sha256=saved_after,
        post_binding_saved_state_sha256=post_binding,
        object_output_sha256=_digest(_export_json(evidence.object_output)),
        object_output_count=len(evidence.object_output["plan_domain_results"]),
        pixel_plan_commitments=_pixel_plan_evidence_commitments(evidence.plan_evidence),
    )


def assemble_pixel_training_evidence_for_plan(
    *,
    plan_set: object,
    training_plan_id: str,
    entry_evidence: tuple[PixelTrainingEvidenceSet, ...],
    publication_audit_sha256: str | None = None,
) -> MultiImageTrainingEvidence:
    """Assemble exact entry contributions through the existing plan validator."""

    from .planning import ValidatedTrainingPlanSetProjection

    if type(plan_set) is not ValidatedTrainingPlanSetProjection:
        raise BatchTrainingError("pixel evidence assembly requires a plan authority")
    plan = plan_set.require_plan(training_plan_id)
    if plan.request.classifier_kind is ClassifierKind.SUPERVISED_PHENOTYPE:
        raise BatchTrainingError("pixel evidence assembly requires a pixel plan")
    if type(entry_evidence) is not tuple or not entry_evidence:
        raise BatchTrainingError("pixel evidence assembly requires entry authorities")
    contributions: list[PixelTrainingPlanEntryEvidence] = []
    for authority in entry_evidence:
        value = _validate_pixel_training_evidence_set(
            authority,
            expected_plan_set=plan_set,
        )
        matches = tuple(
            item for item in value.plan_evidence if item.training_plan_id == plan.training_plan_id
        )
        if not matches:
            continue
        contributions.extend(matches)
    roles = tuple(
        (item.entry_state.project_entry_id, item.entry_state.split) for item in contributions
    )
    expected_roles = {(item.project_entry_id, item.split) for item in plan.request.entries}
    if len(set(roles)) != len(roles) or set(roles) != expected_roles:
        raise BatchTrainingError("pixel entry contributions lack exact plan role coverage")
    rows = tuple(row for item in contributions for row in item.rows)
    _require_pixel_training_classes(plan, rows, entry_evidence)
    return MultiImageTrainingEvidence.create(
        plan=plan,
        entry_states=tuple(item.entry_state for item in contributions),
        rows=rows,
        publication_audit_sha256=publication_audit_sha256,
    )


class MissingPixelTrainingClassesError(BatchTrainingError):
    """Valid saved evidence lacks references for one or more required classes."""


def _require_pixel_training_classes(plan, rows, entry_evidence) -> None:
    """Check pooled training coverage and explain exact saved-label mismatches."""
    present = {row.class_id for row in rows if row.split == "training"}
    missing = set(plan.request.required_classes) - present
    if not missing:
        return
    labels = {class_id: class_id for class_id in plan.request.required_classes}
    if plan.request.primary_settings is not None:
        labels.update({
            item.class_id: item.display_name
            for item in plan.request.primary_settings.primary_classes
        })
    observed = {}
    for entry in entry_evidence:
        entry_counts = {}
        for member in entry.object_output["plan_domain_results"]:
            snapshot = member["snapshot_set"]
            if member["training_plan_id"] != plan.training_plan_id or snapshot["split"] != "training":
                continue
            # Each domain reports the same saved entry inventory; count it once.
            for name, count in snapshot.get("observed_annotation_class_counts", {}).items():
                entry_counts[name] = max(entry_counts.get(name, 0), count)
        for name, count in entry_counts.items():
            observed[name] = observed.get(name, 0) + count
    expected_text = ", ".join(repr(labels[item]) for item in sorted(missing))
    observed_text = ", ".join(f"{name!r} ({count})" for name, count in sorted(observed.items()))
    if not observed_text:
        observed_text = "none reported"
    setup = "primary" if plan.request.primary_settings is not None else "secondary"
    if any(labels[item] not in observed for item in missing):
        guidance = (
            "The expected reference names were not reported among the saved annotations. "
            "The annotation palette and classifier settings must use the same exact names, "
            "including capitalization. "
        )
    else:
        guidance = (
            "Matching class names were found, but those annotations were not included in fitting. "
            "Check the selected training images and whether the annotations lie inside "
            "the parent regions used for fitting. "
        )
    raise MissingPixelTrainingClassesError(
        f"Saved training references are missing for {expected_text}.\n"
        f"Observed saved annotation classes: {observed_text}.\n"
        + guidance
        + f"Open Change {setup} classifier setup, then References to review the annotations "
        "and selected class names before fitting again."
    )


def _pixel_training_projection_sha256(plan: TrainingPlan) -> str:
    """Hash only the pixel fitter's training-role plan and configuration."""

    if type(plan) is not TrainingPlan:
        raise BatchTrainingError("plan must be a TrainingPlan")
    if plan.request.classifier_kind is ClassifierKind.SUPERVISED_PHENOTYPE:
        raise BatchTrainingError("pixel fit evidence requires a pixel plan")
    from .engines import TrainingFitPlanProjection

    return TrainingFitPlanProjection.create(plan).fit_plan_sha256


@dataclass(frozen=True, slots=True)
class MultiImagePixelFitEvidence:
    """Provider-visible pixel evidence containing training material only."""

    training_projection_sha256: str
    entry_states: tuple[TrainingEvidenceEntryState, ...]
    rows: tuple[TrainingEvidenceSnapshot, ...]
    material_evidence_sha256: str
    evidence_set_sha256: str

    def __post_init__(self) -> None:
        """Validate the MultiImagePixelFitEvidence fields when the record is constructed.

        The checks cover ``entry_states``, ``rows``, ``evidence_set_sha256``,
        ``material_evidence_sha256``, ``training_projection_sha256``. Invalid field types,
        values or combinations raise the declared validation error before this instance can
        be used by its caller."""

        object.__setattr__(
            self,
            "training_projection_sha256",
            _sha256(
                self.training_projection_sha256,
                "training_projection_sha256",
            ),
        )
        states = tuple(self.entry_states)
        if not states or not all(type(item) is TrainingEvidenceEntryState for item in states):
            raise BatchTrainingError("entry_states must contain typed entry states")
        if states != tuple(sorted(states, key=_evidence_state_sort_key)):
            raise BatchTrainingError("entry_states must use canonical order")
        state_roles = tuple((item.project_entry_id, item.split) for item in states)
        if len(set(state_roles)) != len(state_roles):
            raise BatchTrainingError("entry_states contain duplicate role coverage")
        if {item.split for item in states} != {"training"}:
            raise BatchTrainingError("pixel fit evidence requires exactly the training role")
        rows = tuple(self.rows)
        if not all(type(item) is TrainingEvidenceSnapshot for item in rows):
            raise BatchTrainingError("rows must contain typed owner rows")
        if rows != tuple(sorted(rows, key=_evidence_row_sort_key)):
            raise BatchTrainingError("rows must use canonical order")
        if any(item.split != "training" for item in rows):
            raise BatchTrainingError("pixel fit evidence cannot contain held-out rows")
        row_mappings = [item.to_mapping() for item in rows]
        if len({_canonical_bytes(item) for item in row_mappings}) != len(rows):
            raise BatchTrainingError("pixel fit evidence contains a duplicate complete row")
        states_by_role = dict(zip(state_roles, states, strict=True))
        for row in rows:
            if (row.project_entry_id, row.split) not in states_by_role:
                raise BatchTrainingError("row has no exact fit entry-state contribution")
        for role, state in states_by_role.items():
            role_rows = [
                item.to_mapping() for item in rows if (item.project_entry_id, item.split) == role
            ]
            if state.entry_snapshot_set_sha256 != _digest(role_rows):
                raise BatchTrainingError(
                    "entry_snapshot_set_sha256 does not match its exact fit rows"
                )
        evidence_set_sha = _sha256(self.evidence_set_sha256, "evidence_set_sha256")
        if evidence_set_sha != _digest(row_mappings):
            raise BatchTrainingError("evidence_set_sha256 is not canonical")
        object.__setattr__(self, "entry_states", states)
        object.__setattr__(self, "rows", rows)
        object.__setattr__(self, "evidence_set_sha256", evidence_set_sha)
        material = _sha256(self.material_evidence_sha256, "material_evidence_sha256")
        if material != _digest(self._material_mapping()):
            raise BatchTrainingError("material_evidence_sha256 is not canonical")
        object.__setattr__(self, "material_evidence_sha256", material)

    @classmethod
    def create(
        cls,
        *,
        plan: TrainingPlan,
        entry_states: tuple[TrainingEvidenceEntryState, ...],
        rows: tuple[TrainingEvidenceSnapshot, ...],
    ) -> "MultiImagePixelFitEvidence":
        if not isinstance(plan, TrainingPlan):
            raise BatchTrainingError("plan must be a TrainingPlan")
        states = tuple(sorted(entry_states, key=_evidence_state_sort_key))
        owner_rows = tuple(sorted(rows, key=_evidence_row_sort_key))
        unsigned = {
            "training_projection_sha256": _pixel_training_projection_sha256(plan),
            "entry_states": states,
            "rows": owner_rows,
            "evidence_set_sha256": _digest([item.to_mapping() for item in owner_rows]),
        }
        prototype = object.__new__(cls)
        for name, value in unsigned.items():
            object.__setattr__(prototype, name, value)
        result = cls(
            **unsigned,
            material_evidence_sha256=_digest(prototype._material_mapping()),
        )
        _validate_pixel_fit_plan(result, plan)
        return result

    @classmethod
    def from_mapping(
        cls,
        value: object,
        *,
        plan: TrainingPlan | None = None,
    ) -> "MultiImagePixelFitEvidence":
        mapping = _closed_mapping(
            value,
            fields=frozenset(cls.__dataclass_fields__),
            field="pixel_fit_evidence",
        )
        result = cls(
            training_projection_sha256=mapping[
                "training_projection_sha256"
            ],  # type: ignore[arg-type]
            entry_states=tuple(
                TrainingEvidenceEntryState.from_mapping(item)
                for item in _array(mapping["entry_states"], "entry_states")
            ),
            rows=tuple(_training_evidence_row(item) for item in _array(mapping["rows"], "rows")),
            material_evidence_sha256=mapping["material_evidence_sha256"],  # type: ignore[arg-type]
            evidence_set_sha256=mapping["evidence_set_sha256"],  # type: ignore[arg-type]
        )
        if plan is not None:
            _validate_pixel_fit_plan(result, plan)
        return result

    def _material_mapping(self) -> dict[str, object]:
        return {
            "training_projection_sha256": self.training_projection_sha256,
            "entry_states": [item.to_mapping() for item in self.entry_states],
            "evidence_set_sha256": self.evidence_set_sha256,
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            "training_projection_sha256": self.training_projection_sha256,
            "entry_states": [item.to_mapping() for item in self.entry_states],
            "rows": [item.to_mapping() for item in self.rows],
            "material_evidence_sha256": self.material_evidence_sha256,
            "evidence_set_sha256": self.evidence_set_sha256,
        }


@dataclass(frozen=True, init=False, repr=False)
class PixelFitEvidenceAuthority:
    """Current-parent-bound authorization for one pixel provider dispatch."""

    _training_plan_id: str
    _training_plan_set_sha256: str
    _state_head_sha256: str
    _active_plan_sha256: str
    _parent_execution_context_sha256: str
    _parent_node_id: str
    _parent_authorization_sha256: str
    _parent_node_material_sha256: str
    _parent_current_execution_seal_sha256: str
    _parent_execution_seal_sha256: str
    _plan_entries: tuple[TrainingPlanEntry, ...]
    _child_authorities: tuple[BatchCommittedArtifactAuthority, ...]
    _evidence: MultiImagePixelFitEvidence
    _parent_execution: object
    _expected_context: object
    _state_store: object
    _state_store_identity: int
    _reservation_nonce: str
    _authority_object_identity: int
    _seal_sha256: str
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("PixelFitEvidenceAuthority is issuer-only")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("PixelFitEvidenceAuthority cannot be subclassed")

    def __copy__(self) -> object:
        raise TypeError("PixelFitEvidenceAuthority is nonserializable")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("PixelFitEvidenceAuthority is nonserializable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("PixelFitEvidenceAuthority is nonserializable")


def _pixel_fit_authority_seal_material(
    *,
    parent: object,
    plan: TrainingPlan,
    child_authorities: tuple[BatchCommittedArtifactAuthority, ...],
    evidence: MultiImagePixelFitEvidence,
    state_store_identity: int,
    reservation_nonce: str,
    authority_object_identity: int,
) -> dict[str, object]:
    return {
        "training_plan_id": plan.training_plan_id,
        "training_plan_set_sha256": (parent.training_plan_set.plan_set.training_plan_set_sha256),
        "state_head_sha256": parent.training_plan_set.state_head_sha256,
        "active_plan_sha256": parent.training_plan_set.active_plan_sha256,
        "parent_execution_context_sha256": parent.execution_context_sha256,
        "parent_node_id": parent.node_id,
        "parent_authorization_sha256": parent.authorization_sha256,
        "parent_node_material_sha256": (parent._current_execution.node_material_sha256),
        "parent_current_execution_seal_sha256": (parent._current_execution._seal_sha256),
        "parent_execution_seal_sha256": parent._seal_sha256,
        "plan_entries": [
            item.to_mapping() for item in plan.request.entries if item.split == "training"
        ],
        "child_authority_seal_sha256s": [item._seal_sha256 for item in child_authorities],
        "provider_evidence": evidence.to_mapping(),
        "state_store_identity": state_store_identity,
        "reservation_nonce": reservation_nonce,
        "authority_object_identity": authority_object_identity,
    }


def _validate_pixel_fit_evidence_authority(
    authority: object,
    *,
    require_current: bool,
) -> tuple[TrainingPlan, MultiImagePixelFitEvidence]:
    """Validate pixel fit evidence authority for the current operation.

    We reuse our issuer's immutable evidence after its initial scientific row
    admission and retain the current parent, plan, child and reservation checks."""

    from ..batch_artifacts import BatchNodeExecutionContext
    from ..batch_state import BatchStateStore
    from .parent_execution import (
        CurrentTrainingParentExecution,
        validate_current_training_parent_execution,
    )

    if (
        type(authority) is not PixelFitEvidenceAuthority
        or getattr(authority, "_issuer", None) is not _PIXEL_FIT_ISSUER
        or type(authority._parent_execution) is not CurrentTrainingParentExecution
        or type(authority._expected_context) is not BatchNodeExecutionContext
        or type(authority._state_store) is not BatchStateStore
        or authority._state_store_identity != id(authority._state_store)
    ):
        raise BatchTrainingError("pixel fit evidence authority issuer is invalid")
    _PIXEL_FIT_ISSUER.assert_reserved(authority)
    parent = validate_current_training_parent_execution(
        authority._parent_execution,
        expected_context=authority._expected_context,
        state_store=authority._state_store if require_current else None,
    )
    plan_set = parent.training_plan_set
    plan = plan_set.require_plan(authority._training_plan_id)
    expected_entries = tuple(item for item in plan.request.entries if item.split == "training")
    training_entry_ids = {item.project_entry_id for item in expected_entries}
    expected_children = tuple(
        sorted(
            (
                item
                for item in parent.evidence_child_authorities
                if item.artifact_ref.scope.project_entry_id in training_entry_ids
            ),
            key=lambda item: item.artifact_ref.artifact_ref_sha256,
        )
    )
    try:
        child_authorities = validate_committed_artifact_authority_set(
            authority._child_authorities,
            expected_active_plan_sha256=authority._active_plan_sha256,
        )
        evidence = authority._evidence
        if type(evidence) is not MultiImagePixelFitEvidence:
            raise BatchTrainingError("pixel fit requires its typed evidence owner")
        _validate_pixel_fit_plan(evidence, plan)
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchTrainingError("pixel fit evidence authority is invalid") from exc
    if (
        parent.classifier_kind is ClassifierKind.SUPERVISED_PHENOTYPE
        or plan_set.state_head_sha256 != authority._state_head_sha256
        or plan_set.active_plan_sha256 != authority._active_plan_sha256
        or plan_set.plan_set.training_plan_set_sha256 != authority._training_plan_set_sha256
        or parent.execution_context_sha256 != authority._parent_execution_context_sha256
        or parent.node_id != authority._parent_node_id
        or parent.authorization_sha256 != authority._parent_authorization_sha256
        or parent._current_execution.node_material_sha256 != authority._parent_node_material_sha256
        or parent._current_execution._seal_sha256 != authority._parent_current_execution_seal_sha256
        or parent._seal_sha256 != authority._parent_execution_seal_sha256
        or authority._plan_entries != expected_entries
        or child_authorities != authority._child_authorities
        or child_authorities != expected_children
        or any(item.state_head_sha256 != authority._state_head_sha256 for item in child_authorities)
    ):
        raise BatchTrainingError("pixel fit evidence authority uses foreign state")
    return plan, evidence


def _consume_pixel_fit_evidence_authority(
    authority: object,
) -> tuple[TrainingPlan, MultiImagePixelFitEvidence]:
    """Atomically reauthenticate and consume one pixel fit reservation."""

    result = _PIXEL_FIT_ISSUER.consume(
        authority,
        lambda: _validate_pixel_fit_evidence_authority(authority, require_current=True),
    )
    if (
        type(result) is not tuple
        or len(result) != 2
        or type(result[0]) is not TrainingPlan
        or type(result[1]) is not MultiImagePixelFitEvidence
    ):
        raise BatchTrainingError("pixel fit evidence consumption returned invalid evidence")
    return result


def _revoke_pixel_fit_evidence_authority(authority: object) -> bool:
    """Release one exact unused pixel provider reservation, if live."""

    return _PIXEL_FIT_ISSUER.revoke(authority)


@dataclass(frozen=True, slots=True)
class _PixelHeldOutEvaluationEvidence:
    entry_states: tuple[TrainingEvidenceEntryState, ...]
    rows: tuple[TrainingEvidenceSnapshot, ...]


@dataclass(frozen=True, slots=True, init=False)
class PixelHeldOutEvaluationAuthority:
    """State-bound validation/test capability that is never provider input."""

    _training_plan_id: str
    _training_plan_set_sha256: str
    _state_head_sha256: str
    _active_plan_sha256: str
    _parent_execution_context_sha256: str
    _parent_node_id: str
    _parent_authorization_sha256: str
    _parent_node_material_sha256: str
    _parent_current_execution_seal_sha256: str
    _parent_execution_seal_sha256: str
    _plan_entries: tuple[TrainingPlanEntry, ...]
    _child_authorities: tuple[BatchCommittedArtifactAuthority, ...]
    _evidence: _PixelHeldOutEvaluationEvidence
    _seal_sha256: str
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("PixelHeldOutEvaluationAuthority is issuer-only")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("PixelHeldOutEvaluationAuthority cannot be subclassed")

    def __copy__(self) -> object:
        raise TypeError("PixelHeldOutEvaluationAuthority is nonserializable")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("PixelHeldOutEvaluationAuthority is nonserializable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("PixelHeldOutEvaluationAuthority is nonserializable")

    def reopen(
        self,
        *,
        parent_execution: object,
        expected_context: object,
        state_store: object,
    ) -> _PixelHeldOutEvaluationEvidence:
        from ..batch_artifacts import BatchNodeExecutionContext
        from ..batch_state import BatchStateStore
        from .parent_execution import (
            CurrentTrainingParentExecution,
            validate_current_training_parent_execution,
        )

        if (
            type(self) is not PixelHeldOutEvaluationAuthority
            or self._issuer is not _PIXEL_HELD_OUT_ISSUER
            or type(parent_execution) is not CurrentTrainingParentExecution
            or type(expected_context) is not BatchNodeExecutionContext
            or type(state_store) is not BatchStateStore
        ):
            raise BatchTrainingError("pixel held-out authority issuer is invalid")
        parent = validate_current_training_parent_execution(
            parent_execution,
            expected_context=expected_context,
            state_store=state_store,
        )
        plan_set = parent.training_plan_set
        if (
            plan_set.state_head_sha256 != self._state_head_sha256
            or plan_set.active_plan_sha256 != self._active_plan_sha256
            or plan_set.plan_set.training_plan_set_sha256 != self._training_plan_set_sha256
            or parent.execution_context_sha256 != self._parent_execution_context_sha256
            or parent.node_id != self._parent_node_id
            or parent.authorization_sha256 != self._parent_authorization_sha256
            or parent._current_execution.node_material_sha256 != self._parent_node_material_sha256
            or parent._current_execution._seal_sha256 != self._parent_current_execution_seal_sha256
            or parent._seal_sha256 != self._parent_execution_seal_sha256
        ):
            raise BatchTrainingError("pixel held-out authority uses foreign state")
        plan = plan_set.require_plan(self._training_plan_id)
        expected_entries = tuple(item for item in plan.request.entries if item.split != "training")
        held_out_entry_ids = {item.project_entry_id for item in expected_entries}
        expected_children = tuple(
            sorted(
                (
                    item
                    for item in parent.evidence_child_authorities
                    if item.artifact_ref.scope.project_entry_id in held_out_entry_ids
                ),
                key=lambda item: item.artifact_ref.artifact_ref_sha256,
            )
        )
        if self._plan_entries != expected_entries:
            raise BatchTrainingError("pixel held-out plan entries were mutated")
        try:
            child_authorities = validate_committed_artifact_authority_set(
                self._child_authorities,
                expected_active_plan_sha256=self._active_plan_sha256,
            )
        except BatchArtifactError as exc:
            raise BatchTrainingError("pixel held-out child authority is invalid") from exc
        if (
            child_authorities != self._child_authorities
            or child_authorities != expected_children
            or any(item.state_head_sha256 != self._state_head_sha256 for item in child_authorities)
        ):
            raise BatchTrainingError("pixel held-out child authority uses foreign state")
        return self._evidence


def partition_pixel_training_evidence(
    *,
    parent_execution: object,
    expected_context: object,
    state_store: object,
    evidence: MultiImageTrainingEvidence,
) -> tuple[PixelFitEvidenceAuthority, PixelHeldOutEvaluationAuthority | None]:
    """Partition a current complete pixel aggregate before provider dispatch."""

    from ..batch_artifacts import BatchNodeExecutionContext
    from ..batch_state import BatchStateStore
    from .parent_execution import (
        CurrentTrainingParentExecution,
        validate_current_training_parent_execution,
    )

    if (
        type(parent_execution) is not CurrentTrainingParentExecution
        or type(expected_context) is not BatchNodeExecutionContext
        or type(state_store) is not BatchStateStore
    ):
        raise BatchTrainingError("pixel evidence partition requires a current training parent")
    parent = validate_current_training_parent_execution(
        parent_execution,
        expected_context=expected_context,
        state_store=state_store,
    )
    if parent.classifier_kind is ClassifierKind.SUPERVISED_PHENOTYPE:
        raise BatchTrainingError("pixel evidence parent has a supervised classifier kind")
    plan_set = parent.training_plan_set
    if not isinstance(evidence, MultiImageTrainingEvidence):
        raise BatchTrainingError("pixel evidence must be MultiImageTrainingEvidence")
    plan = plan_set.require_plan(evidence.training_plan_id)
    _validate_evidence_plan(evidence, plan)
    if evidence.normalized_manifest_sha256 != plan_set.normalized_manifest_sha256:
        raise BatchTrainingError("pixel evidence manifest differs from current state")
    authenticated_children = _validate_pixel_evidence_children(
        plan_set=plan_set,
        plan=plan,
        evidence=evidence,
        child_authorities=parent.evidence_child_authorities,
    )
    fit_states = tuple(item for item in evidence.entry_states if item.split == "training")
    fit_rows = tuple(item for item in evidence.rows if item.split == "training")
    provider = MultiImagePixelFitEvidence.create(
        plan=plan,
        entry_states=fit_states,
        rows=fit_rows,
    )
    training_entries = tuple(item for item in plan.request.entries if item.split == "training")
    training_entry_ids = {item.project_entry_id for item in training_entries}
    training_children = tuple(
        item
        for item in authenticated_children
        if item.artifact_ref.scope.project_entry_id in training_entry_ids
    )
    state_store_identity = id(state_store)
    provider_authority = object.__new__(PixelFitEvidenceAuthority)

    def initialize_provider_authority(
        reservation_nonce: str,
        authority_object_identity: int,
    ) -> None:
        provider_values = {
            "_training_plan_id": plan.training_plan_id,
            "_training_plan_set_sha256": (plan_set.plan_set.training_plan_set_sha256),
            "_state_head_sha256": plan_set.state_head_sha256,
            "_active_plan_sha256": plan_set.active_plan_sha256,
            "_parent_execution_context_sha256": (parent.execution_context_sha256),
            "_parent_node_id": parent.node_id,
            "_parent_authorization_sha256": parent.authorization_sha256,
            "_parent_node_material_sha256": (parent._current_execution.node_material_sha256),
            "_parent_current_execution_seal_sha256": (parent._current_execution._seal_sha256),
            "_parent_execution_seal_sha256": parent._seal_sha256,
            "_plan_entries": training_entries,
            "_child_authorities": training_children,
            "_evidence": provider,
            "_parent_execution": parent,
            "_expected_context": expected_context,
            "_state_store": state_store,
            "_state_store_identity": state_store_identity,
            "_reservation_nonce": reservation_nonce,
            "_authority_object_identity": authority_object_identity,
            "_issuer": _PIXEL_FIT_ISSUER,
        }
        provider_seal_material = _pixel_fit_authority_seal_material(
            parent=parent,
            plan=plan,
            child_authorities=training_children,
            evidence=provider,
            state_store_identity=state_store_identity,
            reservation_nonce=reservation_nonce,
            authority_object_identity=authority_object_identity,
        )
        provider_values["_seal_sha256"] = _digest(provider_seal_material)
        for name, value in provider_values.items():
            object.__setattr__(provider_authority, name, value)

    _PIXEL_FIT_ISSUER.issue(provider_authority, initialize_provider_authority)
    if plan.request.evaluation_intent is EvaluationIntent.TRAINING_ONLY:
        return provider_authority, None
    held_out = _PixelHeldOutEvaluationEvidence(
        tuple(item for item in evidence.entry_states if item.split != "training"),
        tuple(item for item in evidence.rows if item.split != "training"),
    )
    held_out_entries = tuple(item for item in plan.request.entries if item.split != "training")
    held_out_entry_ids = {item.project_entry_id for item in held_out.entry_states}
    held_out_children = tuple(
        item
        for item in authenticated_children
        if item.artifact_ref.owning_identity["project_entry_id"] in held_out_entry_ids
    )
    seal_material = {
        "training_plan_id": plan.training_plan_id,
        "training_plan_set_sha256": plan_set.plan_set.training_plan_set_sha256,
        "state_head_sha256": plan_set.state_head_sha256,
        "active_plan_sha256": plan_set.active_plan_sha256,
        "parent_execution_context_sha256": parent.execution_context_sha256,
        "parent_node_id": parent.node_id,
        "parent_authorization_sha256": parent.authorization_sha256,
        "parent_node_material_sha256": (parent._current_execution.node_material_sha256),
        "parent_current_execution_seal_sha256": (parent._current_execution._seal_sha256),
        "parent_execution_seal_sha256": parent._seal_sha256,
        "plan_entries": [item.to_mapping() for item in held_out_entries],
        "child_authority_seal_sha256s": [item._seal_sha256 for item in held_out_children],
        "entry_state_sha256s": [item.entry_state_sha256 for item in held_out.entry_states],
        "row_sha256s": [item.evidence_sha256 for item in held_out.rows],
    }
    authority = object.__new__(PixelHeldOutEvaluationAuthority)
    values = {
        "_training_plan_id": plan.training_plan_id,
        "_training_plan_set_sha256": plan_set.plan_set.training_plan_set_sha256,
        "_state_head_sha256": plan_set.state_head_sha256,
        "_active_plan_sha256": plan_set.active_plan_sha256,
        "_parent_execution_context_sha256": parent.execution_context_sha256,
        "_parent_node_id": parent.node_id,
        "_parent_authorization_sha256": parent.authorization_sha256,
        "_parent_node_material_sha256": (parent._current_execution.node_material_sha256),
        "_parent_current_execution_seal_sha256": (parent._current_execution._seal_sha256),
        "_parent_execution_seal_sha256": parent._seal_sha256,
        "_plan_entries": held_out_entries,
        "_child_authorities": held_out_children,
        "_evidence": held_out,
        "_seal_sha256": _digest(seal_material),
        "_issuer": _PIXEL_HELD_OUT_ISSUER,
    }
    for name, value in values.items():
        object.__setattr__(authority, name, value)
    return provider_authority, authority


def _validate_pixel_evidence_children(
    *,
    plan_set: object,
    plan: TrainingPlan,
    evidence: MultiImageTrainingEvidence,
    child_authorities: tuple[BatchCommittedArtifactAuthority, ...],
) -> tuple[BatchCommittedArtifactAuthority, ...]:
    """Bind every decoded pixel entry state to one committed child artifact.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    if type(child_authorities) is not tuple or not child_authorities:
        raise BatchTrainingError(
            "pixel evidence partition requires authenticated child authorities"
        )
    try:
        authorities = validate_committed_artifact_authority_set(
            child_authorities,
            expected_active_plan_sha256=plan_set.active_plan_sha256,
        )
    except BatchArtifactError as exc:
        raise BatchTrainingError(
            "pixel evidence partition requires authenticated child authorities"
        ) from exc
    expected_kind = {
        "primary_compartment": "primary_training_evidence_snapshot",
        "secondary_structure": "secondary_training_evidence_snapshot",
    }.get(plan.request.classifier_kind.value)
    if expected_kind is None:
        raise BatchTrainingError("pixel evidence plan has a non-pixel classifier kind")
    states_by_entry: dict[str, list[TrainingEvidenceEntryState]] = {}
    for state in evidence.entry_states:
        states_by_entry.setdefault(state.project_entry_id, []).append(state)
    rows_by_entry: dict[str, list[TrainingEvidenceSnapshot]] = {}
    for row in evidence.rows:
        rows_by_entry.setdefault(row.project_entry_id, []).append(row)
    authorities_by_entry: dict[str, BatchCommittedArtifactAuthority] = {}
    observed_child_entries: set[str] = set()
    for authority in authorities:
        ref = authority.artifact_ref
        identity = ref.owning_identity
        entry_id = identity.get("project_entry_id")
        child_plans = tuple(
            item
            for item in plan_set.plan_set.plans
            if item.request.classifier_kind is plan.request.classifier_kind
            and any(member.project_entry_id == entry_id for member in item.request.entries)
        )
        expected_plan_ids = tuple(sorted(item.training_plan_id for item in child_plans))
        claimed_plan_ids = tuple(
            _sha256(item, "training_plan_ids")
            for item in _array(
                identity.get("training_plan_ids"),
                "training_plan_ids",
            )
        )
        claimed_commitments = tuple(
            _sha256(item, "plan_evidence_commitment_sha256s")
            for item in _array(
                identity.get("plan_evidence_commitment_sha256s"),
                "plan_evidence_commitment_sha256s",
            )
        )
        if (
            not isinstance(entry_id, str)
            or not child_plans
            or claimed_plan_ids != expected_plan_ids
            or len(claimed_commitments) != len(claimed_plan_ids)
        ):
            raise BatchTrainingError(
                "committed pixel evidence child has invalid plan contribution coverage"
            )
        child_entries = tuple(
            member
            for item in child_plans
            for member in item.request.entries
            if member.project_entry_id == entry_id
        )
        child_facts = {(item.sample_id, item.slide_id) for item in child_entries}
        expected_bindings = tuple(
            sorted(
                {
                    binding
                    for item in child_plans
                    for binding in item.request.analysis_domain_bindings
                    if binding.project_entry_id == entry_id
                },
                key=_binding_sort_key,
            )
        )
        if (
            entry_id in observed_child_entries
            or len(child_facts) != 1
            or authority.state_head_sha256 != plan_set.state_head_sha256
            or authority.producer_node_kind != expected_kind
            or ref.artifact_kind != expected_kind
            or (ref.schema_name, ref.schema_version) != ("roi-generation-manifest", 1)
            or ref.scope.validation_domain_id != plan.request.validation_domain_id
            or ref.scope.project_entry_id != entry_id
            or (ref.scope.sample_id, ref.scope.slide_id) != next(iter(child_facts))
            or ref.scope.analysis_domain_bindings != expected_bindings
            or identity.get("training_plan_set_sha256")
            != plan_set.plan_set.training_plan_set_sha256
        ):
            raise BatchTrainingError(
                "committed pixel evidence child differs from its decoded state"
            )
        observed_child_entries.add(entry_id)
        if plan.training_plan_id not in claimed_plan_ids:
            continue
        states = states_by_entry.get(entry_id)
        entry_rows = rows_by_entry.get(entry_id, ())
        if not states or entry_id in authorities_by_entry:
            raise BatchTrainingError(
                "committed pixel evidence child has invalid plan contribution coverage"
            )
        selected_index = claimed_plan_ids.index(plan.training_plan_id)
        expected_commitment = _pixel_plan_evidence_commitment_sha256(
            training_plan_id=plan.training_plan_id,
            classifier_role=plan.request.classifier_kind.value,
            states=tuple(states),
            rows=tuple(entry_rows),
        )
        if (
            claimed_commitments[selected_index] != expected_commitment
            or any(ref.scope.sample_id != state.sample_id for state in states)
            or any(ref.scope.slide_id != state.slide_id for state in states)
            or (
                len(claimed_plan_ids) == 1
                and (
                    identity.get("entry_state_sha256")
                    != _pixel_entry_state_set_sha256(tuple(states))
                    or identity.get("entry_snapshot_set_sha256")
                    != _digest([item.to_mapping() for item in entry_rows])
                )
            )
        ):
            raise BatchTrainingError(
                "committed pixel evidence child has an invalid plan contribution"
            )
        authorities_by_entry[entry_id] = authority
    if set(authorities_by_entry) != set(states_by_entry):
        raise BatchTrainingError("committed pixel evidence children lack exact entry coverage")
    return tuple(authorities_by_entry.values())


@dataclass(frozen=True, slots=True, init=False)
class SupervisedTrainingEvidenceSourceAuthority:
    """Issuer-only authenticated inputs for one supervised evidence snapshot."""

    _training_plan_id: str
    _training_plan_set_sha256: str
    _state_head_sha256: str
    _active_plan_sha256: str
    _plan: TrainingPlan
    _plan_entry: TrainingPlanEntry
    _analysis_domain_bindings: tuple[AnalysisDomainBinding, ...]
    _source_predecessor_artifact_ref_sha256s: tuple[str, ...]
    _saved_state_before_sha256: str
    _saved_state_after_sha256: str
    _scientific_request_body_sha256: str
    _validation_input_authority_sha256: str
    _execution_exchange_sha256: str
    _validated_exchange_sha256: str
    _content_sha256: str
    _snapshot: SupervisedTrainingEvidenceSnapshot
    _seal_sha256: str
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("SupervisedTrainingEvidenceSourceAuthority is issuer-only")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("SupervisedTrainingEvidenceSourceAuthority cannot be subclassed")

    def __copy__(self) -> object:
        raise TypeError("SupervisedTrainingEvidenceSourceAuthority is nonserializable")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("SupervisedTrainingEvidenceSourceAuthority is nonserializable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("SupervisedTrainingEvidenceSourceAuthority is nonserializable")


def _issue_supervised_training_evidence_source(
    *,
    plan_set: object,
    training_plan_id: str,
    project_entry_id: str,
    split: str,
    analysis_domain_bindings: tuple[AnalysisDomainBinding, ...],
    source_predecessor_artifact_ref_sha256s: tuple[str, ...],
    saved_state_before_sha256: str,
    saved_state_after_sha256: str,
    scientific_request_body_sha256: str,
    validation_input_authority_sha256: str,
    execution_exchange_sha256: str,
    validated_exchange_sha256: str,
    payload: object,
) -> SupervisedTrainingEvidenceSourceAuthority:
    """Issue the source after an adapter has authenticated all durable owners."""

    from .planning import ValidatedTrainingPlanSetProjection

    if type(plan_set) is not ValidatedTrainingPlanSetProjection:
        raise BatchTrainingError("supervised evidence source requires a decoded training plan set")
    plan = plan_set.require_plan(training_plan_id)
    if plan.request.classifier_kind.value != "supervised_phenotype":
        raise BatchTrainingError("supervised evidence requires a supervised plan")
    entry = plan_set.require_entry(
        training_plan_id=plan.training_plan_id,
        project_entry_id=project_entry_id,
        split=split,  # type: ignore[arg-type]
    )
    bindings = tuple(sorted(analysis_domain_bindings, key=_binding_sort_key))
    if not bindings or not all(isinstance(item, AnalysisDomainBinding) for item in bindings):
        raise BatchTrainingError(
            "supervised evidence source requires typed analysis-domain bindings"
        )
    expected_bindings = tuple(
        item
        for item in plan.request.analysis_domain_bindings
        if item.project_entry_id == entry.project_entry_id
    )
    if len(set(bindings)) != len(bindings) or any(
        item not in expected_bindings for item in bindings
    ):
        raise BatchTrainingError("supervised evidence source crosses its planned domain coverage")
    source_refs = _sha256_tuple(
        tuple(sorted(source_predecessor_artifact_ref_sha256s)),
        "source_predecessor_artifact_ref_sha256s",
        nonempty=True,
    )
    before = _sha256(saved_state_before_sha256, "saved_state_before_sha256")
    after = _sha256(saved_state_after_sha256, "saved_state_after_sha256")
    if before != after:
        raise BatchTrainingError("supervised evidence snapshot must be read-only")
    frozen_payload = _freeze_json(payload, "supervised_training_evidence_payload")
    if not isinstance(frozen_payload, Mapping):
        raise BatchTrainingError("supervised evidence payload must be an object")
    request_body_sha256 = _sha256(
        scientific_request_body_sha256,
        "scientific_request_body_sha256",
    )
    validation_authority_sha256 = _sha256(
        validation_input_authority_sha256,
        "validation_input_authority_sha256",
    )
    execution_sha256 = _sha256(
        execution_exchange_sha256,
        "execution_exchange_sha256",
    )
    validated_sha256 = _sha256(
        validated_exchange_sha256,
        "validated_exchange_sha256",
    )
    material = {
        "training_plan_id": plan.training_plan_id,
        "training_plan_set_sha256": plan_set.plan_set.training_plan_set_sha256,
        "state_head_sha256": plan_set.state_head_sha256,
        "active_plan_sha256": plan_set.active_plan_sha256,
        "training_plan_sha256": plan.training_plan_sha256,
        "project_entry_id": entry.project_entry_id,
        "split": entry.split,
        "analysis_domain_bindings": [item.to_mapping() for item in bindings],
        "source_predecessor_artifact_ref_sha256s": list(source_refs),
        "saved_state_before_sha256": before,
        "saved_state_after_sha256": after,
        "scientific_request_body_sha256": request_body_sha256,
        "validation_input_authority_sha256": validation_authority_sha256,
        "execution_exchange_sha256": execution_sha256,
        "validated_exchange_sha256": validated_sha256,
        "payload": _export_json(frozen_payload),
    }
    authority = object.__new__(SupervisedTrainingEvidenceSourceAuthority)
    values = {
        "_training_plan_id": plan.training_plan_id,
        "_training_plan_set_sha256": plan_set.plan_set.training_plan_set_sha256,
        "_state_head_sha256": plan_set.state_head_sha256,
        "_active_plan_sha256": plan_set.active_plan_sha256,
        "_plan": plan,
        "_plan_entry": entry,
        "_analysis_domain_bindings": bindings,
        "_source_predecessor_artifact_ref_sha256s": source_refs,
        "_saved_state_before_sha256": before,
        "_saved_state_after_sha256": after,
        "_scientific_request_body_sha256": request_body_sha256,
        "_validation_input_authority_sha256": validation_authority_sha256,
        "_execution_exchange_sha256": execution_sha256,
        "_validated_exchange_sha256": validated_sha256,
        "_content_sha256": _digest({
            key: value for key, value in material.items()
            if key not in {"state_head_sha256", "active_plan_sha256"}
        }),
        "_seal_sha256": _digest(material),
        "_issuer": _SUPERVISED_SOURCE_ISSUER,
    }
    for name, value in values.items():
        object.__setattr__(authority, name, value)
    # i) We compile the admitted scientific payload once while issuing its owner.
    # ii) Entry publication, fitting and held-out consumers reuse this immutable
    # snapshot; their current state, plan and role checks remain separate.
    object.__setattr__(
        authority, "_snapshot",
        _build_supervised_training_evidence_snapshot(authority, frozen_payload),
    )
    return authority


def open_supervised_training_evidence_source(
    *,
    plan_set: object,
    training_plan_id: str,
    project_entry_id: str,
    split: str,
    section_id: str,
    measurements: object,
    phenotyping_modes: object,
    accepted_domains: object,
    execution_evidence: object,
) -> SupervisedTrainingEvidenceSourceAuthority:
    """Cross-check current owner projections and issue one snapshot source.

    All arguments are decoder- or runtime-issued authorities.  Raw mappings,
    repeated digests, and paths are deliberately absent from this boundary.
    """

    from mif_pipeline.pipeline_core import PhenotypingMode, StageStatus

    from ..batch_analysis_domain_artifacts import (
        ValidatedAcceptedAnalysisDomainProjection,
    )
    from ..batch_measurement_artifacts import ValidatedMeasurementsResultProjection
    from ..batch_phenotyping_mode_artifacts import ValidatedPhenotypingModeProjection
    from ..project_stage_plan import ProjectStageTargetExecutionEvidence
    from .planning import ValidatedTrainingPlanSetProjection

    if (
        type(plan_set) is not ValidatedTrainingPlanSetProjection
        or type(measurements) is not tuple
        or not measurements
        or not all(type(item) is ValidatedMeasurementsResultProjection for item in measurements)
        or type(phenotyping_modes) is not tuple
        or not phenotyping_modes
        or not all(type(item) is ValidatedPhenotypingModeProjection for item in phenotyping_modes)
        or type(accepted_domains) is not tuple
        or not accepted_domains
        or not all(
            type(item) is ValidatedAcceptedAnalysisDomainProjection for item in accepted_domains
        )
        or type(execution_evidence) is not ProjectStageTargetExecutionEvidence
    ):
        raise BatchTrainingError(
            "supervised evidence source requires exact owner-issued projections"
        )
    try:
        validation_inputs = execution_evidence.reopen_validation_inputs()
    except Exception as exc:
        raise BatchTrainingError("supervised evidence execution authority is invalid") from exc
    plan = plan_set.require_plan(training_plan_id)
    entry = plan_set.require_entry(
        training_plan_id=plan.training_plan_id,
        project_entry_id=project_entry_id,
        split=split,  # type: ignore[arg-type]
    )
    heads = {
        *(item.state_head_sha256 for item in measurements),
        *(item.state_head_sha256 for item in phenotyping_modes),
        *(item.state_head_sha256 for item in accepted_domains),
        plan_set.state_head_sha256,
    }
    plans = {
        *(item.active_plan_sha256 for item in measurements),
        *(item.active_plan_sha256 for item in phenotyping_modes),
        *(item.active_plan_sha256 for item in accepted_domains),
        plan_set.active_plan_sha256,
    }
    if len(heads) != 1 or len(plans) != 1:
        raise BatchTrainingError("supervised evidence owners use foreign state")
    expected_bindings = tuple(
        item
        for item in plan.request.analysis_domain_bindings
        if item.project_entry_id == entry.project_entry_id
    )
    measurements_by_binding: dict[AnalysisDomainBinding, object] = {}
    for item in measurements:
        if (
            item.project_entry_id != entry.project_entry_id
            or item.sample_id != entry.sample_id
            or item.slide_id != entry.slide_id
            or item.canonical_image_sha256 != entry.image_identity_sha256
        ):
            raise BatchTrainingError("measurements owner crosses the planned entry")
        for binding in item.analysis_domain_bindings:
            if binding in measurements_by_binding:
                raise BatchTrainingError("measurements owner duplicates domain coverage")
            measurements_by_binding[binding] = item
    modes_by_binding: dict[AnalysisDomainBinding, object] = {}
    for item in phenotyping_modes:
        if (
            item.project_entry_id != entry.project_entry_id
            or item.sample_id != entry.sample_id
            or item.slide_id != entry.slide_id
            or item.validation_domain_id != plan.request.validation_domain_id
            or item.selected_mode is not PhenotypingMode.SUPERVISED_CLASSIFIER
            or item.analysis_domain_binding in modes_by_binding
        ):
            raise BatchTrainingError("phenotyping-mode owner crosses supervised scope")
        modes_by_binding[item.analysis_domain_binding] = item
    domain_bindings = tuple(
        binding
        for item in accepted_domains
        for binding in item.artifact_ref.scope.analysis_domain_bindings
    )
    if (
        tuple(sorted(measurements_by_binding, key=_binding_sort_key)) != expected_bindings
        or tuple(sorted(modes_by_binding, key=_binding_sort_key)) != expected_bindings
        or tuple(sorted(set(domain_bindings), key=_binding_sort_key)) != expected_bindings
        or len(domain_bindings) != len(set(domain_bindings))
        or any(
            item.project_entry_id != entry.project_entry_id
            or item.canonical_image_sha256 != entry.image_identity_sha256
            for item in accepted_domains
        )
    ):
        raise BatchTrainingError("supervised evidence owners lack exact analysis-domain coverage")
    for binding, mode in modes_by_binding.items():
        measurement = measurements_by_binding[binding]
        if mode.measurements_result_ref_sha256 != measurement.artifact_ref.artifact_ref_sha256:
            raise BatchTrainingError(
                "phenotyping-mode owner references a different measurements result"
            )

    section_groups: dict[tuple[str, str, str], list[AnalysisDomainBinding]] = {}
    for binding, measurement in measurements_by_binding.items():
        key = (
            measurement.section_id,
            measurement.segmentation_generation_id,
            measurement.measurement_generation_id,
        )
        section_groups.setdefault(key, []).append(binding)
    sections = [
        {
            "section_id": key[0],
            "segmentation_generation_id": key[1],
            "measurement_generation_id": key[2],
            "analysis_domain_ids": sorted(item.analysis_domain_id for item in bindings),
        }
        for key, bindings in sorted(section_groups.items())
    ]
    if len({item["section_id"] for item in sections}) != len(sections):
        raise BatchTrainingError("measurements owners conflict on section generation identity")
    request_body = _closed_mapping(
        validation_inputs.scientific_request_body,
        fields=frozenset(
            {
                "schema_version",
                "sample_id",
                "supervised_training_evidence_requests",
            }
        ),
        field="supervised snapshot request body",
    )
    specification_fields = frozenset(
        {
            "training_plan_id",
            "split",
            "sections",
            "class_ids",
            "feature_schema",
            "point_selection_radius_level0",
        }
    )
    specifications = tuple(
        _closed_mapping(
            item,
            fields=specification_fields | ({"feature_source", "reference_selection_sha256"}
                & item.keys() if isinstance(item, Mapping) else set()),
            field="supervised snapshot request specification",
        )
        for item in _array(
            request_body["supervised_training_evidence_requests"],
            "supervised_training_evidence_requests",
        )
    )
    roles = tuple(
        item for item in plan.request.entries if item.project_entry_id == entry.project_entry_id
    )
    planned_radius = (plan.request.feature_requirements or {}).get("point_selection_radius_level0")
    radius = planned_radius
    if isinstance(radius, bool) or not isinstance(radius, (int, float)) or radius <= 0:
        raise BatchTrainingError("supervised snapshot request body has an invalid point radius")
    from .supervised_features import supervised_snapshot_source, supervised_feature_source
    from .supervised_references import reference_selection_sha256
    source_fields = supervised_snapshot_source(plan.request.model_configuration, (plan.request.feature_requirements or {}).get("feature_schema", ()))
    reference_fields = ({"reference_selection_sha256": reference_selection_sha256(plan)}
        if any("reference_selection_sha256" in item for item in specifications) else {})
    expected_body = {
        "schema_version": "roi-workflow-invocation/v1",
        "sample_id": entry.sample_id,
        "supervised_training_evidence_requests": [
            {
                **source_fields,
                **reference_fields,
                "training_plan_id": plan.training_plan_id,
                "split": role.split,
                "sections": sections,
                "class_ids": list(plan.request.required_classes),
                "feature_schema": _export_json(
                    tuple((plan.request.feature_requirements or {}).get("feature_schema", ()))
                ),
                "point_selection_radius_level0": float(radius),
            }
            for role in roles
        ],
    }
    plain_body = _export_json(request_body)
    if plain_body != expected_body or validation_inputs.scientific_request_body_sha256 != _digest(
        plain_body
    ):
        raise BatchTrainingError(
            "supervised snapshot request body differs from authenticated owners"
        )
    matching_specifications = tuple(
        item
        for item in specifications
        if item["training_plan_id"] == plan.training_plan_id and item["split"] == entry.split
    )
    if len(matching_specifications) != 1:
        raise BatchTrainingError("supervised snapshot request lacks one exact plan role")
    specification = matching_specifications[0]
    authorized = execution_evidence.authorized
    observation = execution_evidence.observation
    result = execution_evidence.wire_stage_result
    if result.generation_id != plan.training_plan_id:
        raise BatchTrainingError("supervised snapshot generation differs from its training plan")
    if (
        authorized.project_entry_id != entry.project_entry_id
        or authorized.sample_id != entry.sample_id
        or authorized.slide_id != entry.slide_id
        or authorized.image_role != "primary_mif"
        or authorized.analysis_domain_bindings != expected_bindings
        or authorized.terminal_stage_id != "supervised_training_evidence_snapshot"
        or authorized.wire_stage_id != "supervised_training_evidence_snapshot"
        or authorized.request_codec_id
        != "qupath/scripts/cell_analysis/05_snapshot_supervised_training_evidence.groovy"
        or authorized.request_codec_version != "1"
        or observation.saved_state_disposition != "authenticated_unchanged"
        or observation.saved_state_after_sha256 != authorized.saved_state_before_sha256
        or result.status is not StageStatus.COMPLETED
        or result.stage != authorized.wire_stage_id
        or result.slide_id != entry.slide_id
        or not isinstance(result.outputs, Mapping)
        or set(result.outputs) != {"supervised_training_evidence_snapshot_sets"}
    ):
        raise BatchTrainingError(
            "supervised snapshot execution differs from its registered read-only codec"
        )
    source_refs = tuple(
        sorted(
            {
                plan_set.artifact_ref.artifact_ref_sha256,
                *(item.artifact_ref.artifact_ref_sha256 for item in measurements),
                *(item.artifact_ref.artifact_ref_sha256 for item in phenotyping_modes),
                *(item.artifact_ref.artifact_ref_sha256 for item in accepted_domains),
            }
        )
    )
    if authorized.prerequisite_artifact_ref_sha256s != source_refs:
        raise BatchTrainingError(
            "supervised snapshot prerequisites differ from authenticated owners"
        )
    requested_section = _text(section_id, "section_id")
    raw_sets = tuple(
        _closed_mapping(
            item,
            fields=frozenset({"training_plan_id", "split", "snapshots"}),
            field="supervised training evidence snapshot set",
        )
        for item in _array(
            result.outputs["supervised_training_evidence_snapshot_sets"],
            "supervised_training_evidence_snapshot_sets",
        )
    )
    expected_role_keys = tuple((plan.training_plan_id, item.split) for item in roles)
    actual_role_keys = tuple(
        (
            _text(item["training_plan_id"], "training_plan_id"),
            _text(item["split"], "split"),
        )
        for item in raw_sets
    )
    if actual_role_keys != expected_role_keys:
        raise BatchTrainingError("supervised snapshot sets differ from exact plan roles")
    matching_sets = tuple(
        item
        for item in raw_sets
        if item["training_plan_id"] == plan.training_plan_id and item["split"] == entry.split
    )
    if len(matching_sets) != 1:
        raise BatchTrainingError("supervised snapshot output lacks one exact plan role")
    raw_snapshots = _array(
        matching_sets[0]["snapshots"],
        "supervised training evidence role snapshots",
    )
    matching_snapshots = tuple(
        item
        for item in raw_snapshots
        if isinstance(item, Mapping) and item.get("section_id") == requested_section
    )
    if len(matching_snapshots) != 1:
        raise BatchTrainingError("source opening requires one exact section snapshot")
    snapshot_section_ids = tuple(
        sorted(
            item.get("section_id")
            for item in raw_snapshots
            if isinstance(item, Mapping) and isinstance(item.get("section_id"), str)
        )
    )
    if len(snapshot_section_ids) != len(raw_snapshots) or snapshot_section_ids != tuple(
        sorted(item["section_id"] for item in sections)
    ):
        raise BatchTrainingError("supervised snapshots require exact unique section coverage")
    payload = _closed_mapping(
        matching_snapshots[0],
        fields=_SUPERVISED_PAYLOAD_FIELDS | ({EVIDENCE_FIELD} if EVIDENCE_FIELD in matching_snapshots[0] else set()),
        field="supervised training evidence snapshot output",
    )
    if (
        payload.get("section_id") != requested_section
        or payload.get("coordinate_system") != "global_level0"
        or isinstance(payload.get("point_selection_radius_level0"), bool)
        or not isinstance(payload.get("point_selection_radius_level0"), (int, float))
        or payload.get("point_selection_radius_level0") != float(radius)
        or _export_json(payload.get("class_ids")) != _export_json(specification["class_ids"])
        or _export_json(payload.get("feature_schema"))
        != _export_json(specification["feature_schema"])
    ):
        raise BatchTrainingError(
            "supervised snapshot output differs from its authenticated request body"
        )
    matching_measurements = tuple(
        item for item in measurements if item.section_id == requested_section
    )
    matching_bindings = tuple(
        sorted(
            (
                binding
                for item in matching_measurements
                for binding in item.analysis_domain_bindings
            ),
            key=_binding_sort_key,
        )
    )
    if (
        not matching_measurements
        or payload.get("pipeline_run_id") != authorized.pipeline_run_id
        or len({item.segmentation_generation_id for item in matching_measurements}) != 1
        or payload.get("segmentation_generation_id")
        != matching_measurements[0].segmentation_generation_id
        or len({item.measurement_generation_id for item in matching_measurements}) != 1
        or payload.get("measurement_generation_id")
        != matching_measurements[0].measurement_generation_id
    ):
        raise BatchTrainingError(
            "supervised snapshot section or generation differs from measurements"
        )
    source = _issue_supervised_training_evidence_source(
        plan_set=plan_set,
        training_plan_id=plan.training_plan_id,
        project_entry_id=entry.project_entry_id,
        split=entry.split,
        analysis_domain_bindings=matching_bindings,
        source_predecessor_artifact_ref_sha256s=source_refs,
        saved_state_before_sha256=authorized.saved_state_before_sha256,
        saved_state_after_sha256=observation.saved_state_after_sha256,
        scientific_request_body_sha256=(validation_inputs.scientific_request_body_sha256),
        validation_input_authority_sha256=validation_inputs.authority_sha256,
        execution_exchange_sha256=execution_evidence.execution_exchange_sha256,
        validated_exchange_sha256=execution_evidence.validated_exchange_sha256,
        payload=payload,
    )
    # Reference annotations are saved after measurement. Their new hierarchy
    # bytes do not change the accepted cell population or measured values;
    # authenticate those scientific inputs directly against their stored owner.
    population = phenotyping_modes[0].population_selection
    if any(mode.population_selection != population for mode in phenotyping_modes):
        raise BatchTrainingError("Choose the same phenotyping population for every analysis region in this image")
    _validate_supervised_measurement_cells(
        cells=source._snapshot.eligible_cells,
        population_selection=population,
        measurements=matching_measurements,
        planned_feature_schema=tuple(
            (plan.request.feature_requirements or {}).get("feature_schema", ())
        ),
        feature_source=supervised_feature_source(plan.request.model_configuration),
        deferred_feature_evidence=source._snapshot.deferred_feature_evidence,
    )
    return source


def _validate_supervised_measurement_cells(
    *,
    cells: tuple[CellRecord, ...],
    population_selection: Mapping[str, object],
    measurements: tuple[object, ...],
    planned_feature_schema: tuple[object, ...],
    feature_source=None,
    deferred_feature_evidence=None,
) -> None:
    """Compare saved reference cells with committed native measurements.

    Initial snapshot admission owns cell construction, scope and unique stable IDs.
    This external comparison owns the selected feature and stored-value match.
    Its existing row-stream reader authenticates complete population commitments;
    reference cells need not cover every measured domain."""

    if feature_source is not None:
        from .supervised_features import require_kronos_cell_schema
        require_kronos_cell_schema(feature_source, planned_feature_schema)
    requested_features: dict[str, str] = {}
    for raw in planned_feature_schema:
        feature = _closed_mapping(
            raw,
            fields=frozenset({"measurement_name", "semantic_role"}),
            field="planned supervised feature",
        )
        name = _text(feature["measurement_name"], "measurement_name")
        role = _text(feature["semantic_role"], "semantic_role")
        if name in requested_features:
            raise BatchTrainingError("planned supervised feature names must be unique")
        requested_features[name] = role
    if deferred_feature_evidence is not None:
        validate_deferred_evidence(
            deferred_feature_evidence, (cell.cell_id for cell in cells),
            source=feature_source, require_pending=True,
        )
        if any(cell.marker_measurements for cell in cells):
            raise BatchTrainingError("Deferred cells cannot contain embedding measurements")
    elif pending_marker_names(feature_source):
        raise BatchTrainingError("Pending KRONOS statistics require deferred cell evidence")
    elif any(set(item.marker_measurements) != set(requested_features) for item in cells):
        raise BatchTrainingError(
            "supervised snapshot eligible cells differ from the planned features"
        )

    if feature_source is not None:
        import math
        if any(type(value) not in (int, float) or not math.isfinite(value)
               for cell in cells for value in cell.marker_measurements.values()):
            raise BatchTrainingError("KRONOS snapshot measurements must be finite numeric values")

    for measurement in measurements:
        if feature_source is None:
            owner_features = {
                item.measurement: item.kind for item in measurement.measurement_plan.features
            }
            if any(owner_features.get(name) != role for name, role in requested_features.items()):
                raise BatchTrainingError(
                    "supervised feature schema differs from the measurements owner"
                )
            inventory = {item["measurement_name"]: item for item in measurement.measurement_inventory}
            if any(
                name not in inventory
                or inventory[name].get("kind") != role
                or inventory[name].get("missing_rate") != 0.0
                or inventory[name].get("non_finite_rate") != 0.0
                for name, role in requested_features.items()
            ):
                raise BatchTrainingError(
                    "supervised feature inventory differs from the measurements owner"
                )
        domain_ids = {item.analysis_domain_id for item in measurement.analysis_domain_bindings}
        _authenticate_supervised_measurement_rows(
            measurement=measurement,
            population_selection=population_selection,
            scoped_cells=tuple(item for item in cells if item.analysis_domain_id in domain_ids),
            requested_features={} if deferred_feature_evidence is not None else requested_features,
        )


def _authenticate_supervised_measurement_rows(
    *,
    measurement: object,
    population_selection: Mapping[str, object],
    scoped_cells: tuple[CellRecord, ...],
    requested_features: Mapping[str, str],
) -> None:
    """Authenticate the full MR2 stream and retain only selected reference rows."""

    from mif_pipeline.cell_analysis.measurement_rows import (
        MarkerIntensityRowsDescriptor,
        MeasurementRowStreamError,
        MeasurementRowStreamExpectation,
        validate_measurement_row_stream,
    )

    marker_features = tuple(
        item for item in measurement.measurement_plan.features if item.kind == "expression"
    )
    if not marker_features:
        raise BatchTrainingError(
            "supervised evidence requires authenticated marker measurement rows"
        )
    marker_names = tuple(item.measurement for item in marker_features)
    requested_marker_indices = tuple(
        index for index, name in enumerate(marker_names) if name in requested_features
    )
    requested_marker_names = tuple(marker_names[index] for index in requested_marker_indices)
    reference = measurement.measurement_rows
    path = reference.path
    if path.parent.name != "measurement-rows" or path.parent.parent.name != "batch":
        raise BatchTrainingError(
            "supervised measurement rows are outside their canonical owner location"
        )
    provisional: list[tuple[str, str, tuple[float, ...]]] = []
    selected_ids = {item.cell_id for item in scoped_cells}
    outside_ids = set()
    from mif_pipeline.cell_analysis.phenotyping_population import population_includes

    def receive(
        cell_id: str,
        analysis_domain_id: str,
        values: tuple[float, ...],
        membership,
    ) -> None:
        if cell_id in selected_ids:
            if not population_includes(population_selection, membership):
                outside_ids.add(cell_id)
            provisional.append((cell_id, analysis_domain_id, values))

    try:
        rows = validate_measurement_row_stream(
            descriptor=MarkerIntensityRowsDescriptor(
                path=path,
                sha256=reference.sha256,
                byte_count=reference.byte_count,
                row_count=reference.row_count,
                content_schema_version=reference.content_schema_version,
            ),
            authenticated_output_root=path.parent.parent.parent,
            expected=MeasurementRowStreamExpectation(
                project_entry_id=measurement.project_entry_id,
                slide_id=measurement.slide_id,
                section_id=measurement.section_id,
                segmentation_generation_id=(measurement.measurement_generation_id),
                measurement_generation_id=(measurement.measurement_generation_id),
                segmentation_population_id=(measurement.segmentation_population_id),
                analysis_domain_bindings=tuple(
                    {
                        "analysis_domain_id": item.analysis_domain_id,
                        "analysis_domain_revision_sha256": (item.analysis_domain_revision_sha256),
                    }
                    for item in measurement.analysis_domain_bindings
                ),
                feature_ids=tuple(item.feature_id for item in marker_features),
                measurement_names=marker_names,
            ),
            provisional_population_callback=receive,
            population_selection=population_selection,
        )
    except (MeasurementRowStreamError, TypeError, ValueError) as exc:
        provisional.clear()
        raise BatchTrainingError("supervised measurement rows are invalid") from exc
    if outside_ids:
        raise BatchTrainingError(f"{len(outside_ids)} reference cells are outside the selected phenotyping population. Move or remove those references, or revise Cells to phenotype before fitting.")
    checks = (
        (rows.cell_count, measurement.cell_count),
        (rows.cell_count, reference.row_count),
        (rows.cell_ids_sha256, measurement.cell_ids_sha256),
        (rows.cell_ids_sha256, reference.cell_ids_sha256),
        (
            rows.cell_to_domain_membership_sha256,
            measurement.cell_to_domain_membership_sha256,
        ),
        (
            rows.cell_to_domain_membership_sha256,
            reference.cell_to_domain_membership_sha256,
        ),
        (
            rows.domain_cell_commitments_sha256,
            measurement.domain_cell_commitments_sha256,
        ),
        (
            rows.domain_cell_commitments_sha256,
            reference.domain_cell_commitments_sha256,
        ),
        (rows.row_inventory_sha256, reference.row_inventory_sha256),
        (rows.feature_ids, tuple(item.feature_id for item in marker_features)),
        (rows.measurement_names, marker_names),
    )
    if any(actual != expected for actual, expected in checks) or tuple(
        item.to_mapping() for item in rows.domain_cell_commitments
    ) != tuple(dict(item) for item in measurement.domain_cell_commitments):
        provisional.clear()
        raise BatchTrainingError("supervised measurement rows differ from their owner commitments")
    expected_values = tuple(
        (
            cell.cell_id,
            cell.analysis_domain_id,
            tuple(float(cell.marker_measurements[name]) for name in requested_marker_names),
        )
        for cell in sorted(scoped_cells, key=lambda item: item.cell_id)
    )
    actual_values = tuple(
        (
            cell_id,
            domain_id,
            tuple(values[index] for index in requested_marker_indices),
        )
        for cell_id, domain_id, values in provisional
    )
    if actual_values != expected_values:
        provisional.clear()
        raise BatchTrainingError("supervised eligible-cell values differ from measurement rows")


@dataclass(frozen=True, slots=True)
class SupervisedTrainingEvidenceSnapshot:
    project_entry_id: str
    sample_id: str
    slide_id: str
    subject_id: str
    split: str
    image_identity_sha256: str
    section_id: str
    pipeline_run_id: str
    segmentation_generation_id: str
    measurement_generation_id: str
    analysis_domain_binding_sha256s: tuple[str, ...]
    analysis_domain_binding_set_sha256: str
    saved_state_before_sha256: str
    saved_state_after_sha256: str
    post_binding_saved_state_sha256: str
    coordinate_system: str
    point_selection_radius_level0: float
    cell_to_analysis_domain_mapping_sha256: str
    class_ids: tuple[str, ...]
    feature_schema: tuple[SupervisedFeatureDefinition, ...]
    point_evidence: tuple[object, ...]
    eligible_cells: tuple[CellRecord, ...]
    mapped_evidence: tuple[MappedPointEvidence, ...]
    snapshot_sha256: str
    deferred_feature_evidence: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        """Validate the SupervisedTrainingEvidenceSnapshot fields when the record is
        constructed.

        The checks cover ``point_selection_radius_level0``, ``split``,
        ``coordinate_system``, ``class_ids``, ``feature_schema``, ``eligible_cells``.
        Invalid field types, values or combinations raise the declared validation error
        before this instance can be used by its caller."""

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
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.split not in _SPLITS:
            raise BatchTrainingError("split must be training, validation, or test")
        for name in (
            "image_identity_sha256",
            "analysis_domain_binding_set_sha256",
            "saved_state_before_sha256",
            "saved_state_after_sha256",
            "post_binding_saved_state_sha256",
            "cell_to_analysis_domain_mapping_sha256",
        ):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        object.__setattr__(
            self,
            "analysis_domain_binding_sha256s",
            _sha256_tuple(
                self.analysis_domain_binding_sha256s,
                "analysis_domain_binding_sha256s",
                nonempty=True,
            ),
        )
        if self.coordinate_system != "global_level0":
            raise BatchTrainingError("coordinate_system must be global_level0")
        radius = self.point_selection_radius_level0
        if isinstance(radius, bool) or not isinstance(radius, (int, float)) or radius <= 0:
            raise BatchTrainingError("point_selection_radius_level0 must be positive")
        object.__setattr__(self, "point_selection_radius_level0", float(radius))
        class_ids = _string_tuple(self.class_ids, "class_ids", nonempty=True, canonical=False)
        object.__setattr__(self, "class_ids", class_ids)
        schema = tuple(self.feature_schema)
        if not schema or not all(type(item) is SupervisedFeatureDefinition for item in schema):
            raise BatchTrainingError("feature_schema must contain typed definitions")
        if len({item.measurement_name for item in schema}) != len(schema):
            raise BatchTrainingError("feature_schema measurement names must be unique")
        object.__setattr__(self, "feature_schema", schema)
        points = tuple(
            _freeze_json(item, f"point_evidence[{index}]")
            for index, item in enumerate(self.point_evidence)
        )
        object.__setattr__(self, "point_evidence", points)
        cells = tuple(self.eligible_cells)
        if not all(type(item) is CellRecord for item in cells):
            raise BatchTrainingError("eligible_cells must contain CellRecord values")
        if len({item.cell_id for item in cells}) != len(cells):
            raise BatchTrainingError("eligible_cells require unique stable IDs")
        object.__setattr__(self, "eligible_cells", cells)
        mapped = tuple(self.mapped_evidence)
        if not all(type(item) is MappedPointEvidence for item in mapped):
            raise BatchTrainingError("mapped_evidence must contain owner mappings")
        if len({item.point_id for item in mapped}) != len(mapped):
            raise BatchTrainingError("mapped_evidence requires unique points")
        if any(
            item.slide_id != self.slide_id or item.section_id != self.section_id for item in cells
        ):
            raise BatchTrainingError("eligible cells cross the snapshot scope")
        if self.cell_to_analysis_domain_mapping_sha256 != _digest(
            {
                item.cell_id: item.analysis_domain_id
                for item in sorted(cells, key=lambda item: item.cell_id)
            }
        ):
            raise BatchTrainingError("cell-to-analysis-domain mapping digest is not canonical")
        if self.split == "test" and any(item.evidence_set != "holdout" for item in mapped):
            raise BatchTrainingError("test-role supervised evidence must remain held out")
        object.__setattr__(self, "mapped_evidence", mapped)
        object.__setattr__(self, "deferred_feature_evidence", validate_snapshot_inputs(
            cells, schema, self.deferred_feature_evidence,
        ))
        supplied = _sha256(self.snapshot_sha256, "snapshot_sha256")
        if supplied != _digest(self._unsigned_mapping()):
            raise BatchTrainingError("snapshot_sha256 is not canonical")
        object.__setattr__(self, "snapshot_sha256", supplied)

    @classmethod
    def from_mapping(cls, value: object) -> "SupervisedTrainingEvidenceSnapshot":
        """Decode the serialized fields into a validated SupervisedTrainingEvidenceSnapshot
        record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""

        if isinstance(value, Mapping) and EVIDENCE_FIELD in value and value[EVIDENCE_FIELD] is None:
            raise BatchTrainingError("Optional deferred feature evidence cannot be null")
        mapping = _closed_mapping(
            value,
            fields=(frozenset(cls.__dataclass_fields__) - {EVIDENCE_FIELD})
                | ({EVIDENCE_FIELD} if isinstance(value, Mapping) and EVIDENCE_FIELD in value else set()),
            field="supervised_training_evidence_snapshot",
        )
        try:
            cells = tuple(
                CellRecord.from_mapping(item)  # type: ignore[arg-type]
                for item in _array(mapping["eligible_cells"], "eligible_cells")
            )
            features = tuple(
                SupervisedFeatureDefinition.from_mapping(item)
                for item in _array(mapping["feature_schema"], "feature_schema")
            )
            mapped = tuple(
                _mapped_point_from_mapping(item)
                for item in _array(mapping["mapped_evidence"], "mapped_evidence")
            )
        except (ROIAnalysisError, SupervisedPhenotypingError, TypeError, ValueError) as exc:
            raise BatchTrainingError("supervised snapshot owner values are invalid") from exc
        return cls(
            project_entry_id=mapping["project_entry_id"],  # type: ignore[arg-type]
            sample_id=mapping["sample_id"],  # type: ignore[arg-type]
            slide_id=mapping["slide_id"],  # type: ignore[arg-type]
            subject_id=mapping["subject_id"],  # type: ignore[arg-type]
            split=mapping["split"],  # type: ignore[arg-type]
            image_identity_sha256=mapping["image_identity_sha256"],  # type: ignore[arg-type]
            section_id=mapping["section_id"],  # type: ignore[arg-type]
            pipeline_run_id=mapping["pipeline_run_id"],  # type: ignore[arg-type]
            segmentation_generation_id=mapping[
                "segmentation_generation_id"
            ],  # type: ignore[arg-type]
            measurement_generation_id=mapping[
                "measurement_generation_id"
            ],  # type: ignore[arg-type]
            analysis_domain_binding_sha256s=tuple(
                _array(
                    mapping["analysis_domain_binding_sha256s"],
                    "analysis_domain_binding_sha256s",
                )
            ),  # type: ignore[arg-type]
            analysis_domain_binding_set_sha256=mapping[
                "analysis_domain_binding_set_sha256"
            ],  # type: ignore[arg-type]
            saved_state_before_sha256=mapping[
                "saved_state_before_sha256"
            ],  # type: ignore[arg-type]
            saved_state_after_sha256=mapping["saved_state_after_sha256"],  # type: ignore[arg-type]
            post_binding_saved_state_sha256=mapping[
                "post_binding_saved_state_sha256"
            ],  # type: ignore[arg-type]
            coordinate_system=mapping["coordinate_system"],  # type: ignore[arg-type]
            point_selection_radius_level0=mapping[
                "point_selection_radius_level0"
            ],  # type: ignore[arg-type]
            cell_to_analysis_domain_mapping_sha256=mapping[
                "cell_to_analysis_domain_mapping_sha256"
            ],  # type: ignore[arg-type]
            class_ids=tuple(_array(mapping["class_ids"], "class_ids")),  # type: ignore[arg-type]
            feature_schema=features,
            point_evidence=tuple(_array(mapping["point_evidence"], "point_evidence")),
            eligible_cells=cells,
            mapped_evidence=mapped,
            deferred_feature_evidence=mapping.get(EVIDENCE_FIELD),
            snapshot_sha256=mapping["snapshot_sha256"],  # type: ignore[arg-type]
        )

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            **project_deferred_evidence(self.deferred_feature_evidence,
                                        (cell.cell_id for cell in self.eligible_cells)),
            "project_entry_id": self.project_entry_id,
            "sample_id": self.sample_id,
            "slide_id": self.slide_id,
            "subject_id": self.subject_id,
            "split": self.split,
            "image_identity_sha256": self.image_identity_sha256,
            "section_id": self.section_id,
            "pipeline_run_id": self.pipeline_run_id,
            "segmentation_generation_id": self.segmentation_generation_id,
            "measurement_generation_id": self.measurement_generation_id,
            "analysis_domain_binding_sha256s": list(self.analysis_domain_binding_sha256s),
            "analysis_domain_binding_set_sha256": self.analysis_domain_binding_set_sha256,
            "saved_state_before_sha256": self.saved_state_before_sha256,
            "saved_state_after_sha256": self.saved_state_after_sha256,
            "post_binding_saved_state_sha256": self.post_binding_saved_state_sha256,
            "coordinate_system": self.coordinate_system,
            "point_selection_radius_level0": self.point_selection_radius_level0,
            "cell_to_analysis_domain_mapping_sha256": (self.cell_to_analysis_domain_mapping_sha256),
            "class_ids": list(self.class_ids),
            "feature_schema": [item.to_mapping() for item in self.feature_schema],
            "point_evidence": [_export_json(item) for item in self.point_evidence],
            "eligible_cells": [item.to_export_mapping() for item in self.eligible_cells],
            "mapped_evidence": [_mapped_point_to_mapping(item) for item in self.mapped_evidence],
        }

    def to_mapping(self) -> dict[str, object]:
        return {**self._unsigned_mapping(), "snapshot_sha256": self.snapshot_sha256}


def build_supervised_training_evidence_snapshot(
    source: SupervisedTrainingEvidenceSourceAuthority,
) -> SupervisedTrainingEvidenceSnapshot:
    """Reuse our source-issued immutable snapshot without rematching its points."""
    return _validate_supervised_source_authority(source)._snapshot


def _build_supervised_training_evidence_snapshot(
    source: SupervisedTrainingEvidenceSourceAuthority,
    payload: Mapping[str, object],
) -> SupervisedTrainingEvidenceSnapshot:
    """Map one authenticated current-image payload through scientific owners."""

    if (
        type(source) is not SupervisedTrainingEvidenceSourceAuthority
        or source._issuer is not _SUPERVISED_SOURCE_ISSUER
    ):
        raise BatchTrainingError("supervised evidence source authority is invalid")
    payload = _closed_mapping(
        payload,
        fields=_SUPERVISED_PAYLOAD_FIELDS | ({EVIDENCE_FIELD} if EVIDENCE_FIELD in payload else set()),
        field="supervised_training_evidence_payload",
    )
    entry = source._plan_entry
    section_id = _text(payload["section_id"], "section_id")
    run_id = _text(payload["pipeline_run_id"], "pipeline_run_id")
    segmentation_id = _text(payload["segmentation_generation_id"], "segmentation_generation_id")
    measurement_id = _text(payload["measurement_generation_id"], "measurement_generation_id")
    if payload["coordinate_system"] != "global_level0":
        raise BatchTrainingError("supervised evidence must use global_level0")
    radius = payload["point_selection_radius_level0"]
    if isinstance(radius, bool) or not isinstance(radius, (int, float)) or radius <= 0:
        raise BatchTrainingError("point selection radius must be positive")
    class_ids = _string_tuple(payload["class_ids"], "class_ids", nonempty=True, canonical=False)
    if class_ids != source._plan.request.required_classes:
        raise BatchTrainingError("supervised snapshot classes differ from its plan")
    try:
        schema = tuple(
            SupervisedFeatureDefinition.from_mapping(item)
            for item in _array(payload["feature_schema"], "feature_schema")
        )
        expected_feature_schema = tuple(
            (source._plan.request.feature_requirements or {}).get("feature_schema", ())
        )
        if tuple(item.to_mapping() for item in schema) != expected_feature_schema:
            raise BatchTrainingError("supervised snapshot feature schema differs from its plan")
        cells = tuple(
            _supervised_cell_from_payload(item)
            for item in _array(payload["eligible_cells"], "eligible_cells")
        )
        points = tuple(_array(payload["point_evidence"], "point_evidence"))
        mapped = map_point_evidence(
            points,
            cells,
            class_ids=class_ids,
            radius_level0=float(radius),
        )
        expected_role = "training" if entry.split == "training" else "holdout"
        if any(item.evidence_set != expected_role for item in mapped):
            raise BatchTrainingError("Supervised references must match their selected subject role.")
        if len({item.cell_id for item in mapped}) != len(mapped):
            raise BatchTrainingError("Each stable cell can have only one reference label in this role.")
        # Subject/entry role leakage is checked across the complete aggregate;
        # a single training-only image does not need unused held-out labels.
    except BatchTrainingError:
        raise
    except (ROIAnalysisError, SupervisedPhenotypingError, TypeError, ValueError) as exc:
        raise BatchTrainingError("supervised evidence scientific validation failed") from exc
    if any(
        cell.slide_id != entry.slide_id
        or cell.section_id != section_id
        or cell.analysis_domain_id
        not in {item.analysis_domain_id for item in source._analysis_domain_bindings}
        for cell in cells
    ):
        raise BatchTrainingError("eligible cells cross the authenticated entry scope")
    from .supervised_features import supervised_feature_source
    feature_source = supervised_feature_source(source._plan.request.model_configuration)
    deferred = payload.get(EVIDENCE_FIELD)
    if EVIDENCE_FIELD in payload:
        validate_deferred_evidence(deferred, (cell.cell_id for cell in cells),
                                   source=feature_source, require_pending=True)
    elif pending_marker_names(feature_source):
        raise BatchTrainingError("Pending KRONOS statistics require deferred cell evidence")
    bindings = source._analysis_domain_bindings
    binding_mappings = [item.to_mapping() for item in bindings]
    cell_domain_mapping = {
        item.cell_id: item.analysis_domain_id
        for item in sorted(cells, key=lambda item: item.cell_id)
    }
    unsigned = {
        "deferred_feature_evidence": deferred,
        "project_entry_id": entry.project_entry_id,
        "sample_id": entry.sample_id,
        "slide_id": entry.slide_id,
        "subject_id": entry.subject_id,
        "split": entry.split,
        "image_identity_sha256": entry.image_identity_sha256,
        "section_id": section_id,
        "pipeline_run_id": run_id,
        "segmentation_generation_id": segmentation_id,
        "measurement_generation_id": measurement_id,
        "analysis_domain_binding_sha256s": tuple(
            sorted(_digest(item) for item in binding_mappings)
        ),
        "analysis_domain_binding_set_sha256": _digest(binding_mappings),
        "saved_state_before_sha256": source._saved_state_before_sha256,
        "saved_state_after_sha256": source._saved_state_after_sha256,
        "post_binding_saved_state_sha256": source._saved_state_after_sha256,
        "coordinate_system": "global_level0",
        "point_selection_radius_level0": float(radius),
        "cell_to_analysis_domain_mapping_sha256": _digest(cell_domain_mapping),
        "class_ids": class_ids,
        "feature_schema": schema,
        "point_evidence": points,
        "eligible_cells": cells,
        "mapped_evidence": mapped,
    }
    prototype = object.__new__(SupervisedTrainingEvidenceSnapshot)
    for name, value in unsigned.items():
        object.__setattr__(prototype, name, value)
    return SupervisedTrainingEvidenceSnapshot(
        **unsigned,
        snapshot_sha256=_digest(prototype._unsigned_mapping()),
    )


@dataclass(frozen=True, slots=True, init=False)
class SupervisedTrainingEvidenceEntryAuthority:
    """Issuer-only proof of one complete supervised entry request."""

    _plan_set: object
    _plan: TrainingPlan
    _plan_entries: tuple[TrainingPlanEntry, ...]
    _sections: tuple[Mapping[str, object], ...]
    _analysis_domain_bindings: tuple[AnalysisDomainBinding, ...]
    _sources: tuple[SupervisedTrainingEvidenceSourceAuthority, ...]
    _snapshots: tuple[SupervisedTrainingEvidenceSnapshot, ...]
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("SupervisedTrainingEvidenceEntryAuthority is issuer-only")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("SupervisedTrainingEvidenceEntryAuthority cannot be subclassed")

    def __copy__(self) -> object:
        raise TypeError("SupervisedTrainingEvidenceEntryAuthority is nonserializable")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("SupervisedTrainingEvidenceEntryAuthority is nonserializable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("SupervisedTrainingEvidenceEntryAuthority is nonserializable")


def _validate_supervised_source_authority(
    source: object,
) -> SupervisedTrainingEvidenceSourceAuthority:
    if (
        type(source) is not SupervisedTrainingEvidenceSourceAuthority
        or source._issuer is not _SUPERVISED_SOURCE_ISSUER
    ):
        raise BatchTrainingError("supervised evidence source authority is invalid")
    return source


def _validate_supervised_training_evidence_entry(
    authority: object,
    *,
    expected_plan_set: object | None = None,
) -> SupervisedTrainingEvidenceEntryAuthority:
    """Validate supervised training evidence entry for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    from .planning import ValidatedTrainingPlanSetProjection

    if (
        type(authority) is not SupervisedTrainingEvidenceEntryAuthority
        or authority._issuer is not _SUPERVISED_ENTRY_ISSUER
        or type(authority._plan_set) is not ValidatedTrainingPlanSetProjection
    ):
        raise BatchTrainingError("supervised entry evidence authority is invalid")
    plan_set = authority._plan_set
    if expected_plan_set is not None and plan_set is not expected_plan_set:
        raise BatchTrainingError("supervised entry evidence uses a foreign plan authority")
    plan = plan_set.require_plan(authority._plan.training_plan_id)
    entries = tuple(
        plan_set.require_entry(
            training_plan_id=plan.training_plan_id,
            project_entry_id=item.project_entry_id,
            split=item.split,
        )
        for item in authority._plan_entries
    )
    if (
        plan != authority._plan
        or entries != authority._plan_entries
        or not entries
        or len({item.project_entry_id for item in entries}) != 1
    ):
        raise BatchTrainingError("supervised entry evidence plan role was mutated")
    sections = authority._sections
    section_ids = tuple(_text(item["section_id"], "section_id") for item in sections)
    if not section_ids or section_ids != tuple(sorted(set(section_ids))):
        raise BatchTrainingError("supervised entry section coverage is not canonical")
    sources = tuple(_validate_supervised_source_authority(item) for item in authority._sources)
    snapshots = authority._snapshots
    expected_role_sections = tuple(
        (entry.project_entry_id, entry.split, section_id)
        for entry in entries
        for section_id in section_ids
    )
    actual_role_sections = tuple(
        (
            item._plan_entry.project_entry_id,
            item._plan_entry.split,
            item._snapshot.section_id,
        )
        for item in sources
    )
    if (
        actual_role_sections != expected_role_sections
        or tuple((item.project_entry_id, item.split, item.section_id) for item in snapshots)
        != expected_role_sections
        or any(
            item._plan != plan
            or item._training_plan_set_sha256 != plan_set.plan_set.training_plan_set_sha256
            or item._state_head_sha256 != plan_set.state_head_sha256
            or item._active_plan_sha256 != plan_set.active_plan_sha256
            for item in sources
        )
    ):
        raise BatchTrainingError("supervised entry sources differ from their plan role")
    expected_bindings = tuple(
        item
        for item in plan.request.analysis_domain_bindings
        if item.project_entry_id == entries[0].project_entry_id
    )
    role_bindings = tuple(
        tuple(
            sorted(
                (
                    binding
                    for source in sources
                    if source._plan_entry == entry
                    for binding in source._analysis_domain_bindings
                ),
                key=_binding_sort_key,
            )
        )
        for entry in entries
    )
    if (
        any(
            len(set(actual)) != len(actual) or actual != expected_bindings
            for actual in role_bindings
        )
        or authority._analysis_domain_bindings != expected_bindings
    ):
        raise BatchTrainingError("supervised entry sources lack exact domain coverage")
    return authority


def _project_supervised_training_evidence_sources(
    authority: SupervisedTrainingEvidenceEntryAuthority,
) -> tuple[SupervisedTrainingEvidenceSourceAuthority, ...]:
    """Project exact source capabilities from one authenticated entry owner."""

    checked = _validate_supervised_training_evidence_entry(authority)
    return checked._sources


def _issue_supervised_training_evidence_entry(
    *,
    plan_set: object,
    plan: TrainingPlan,
    entries: tuple[TrainingPlanEntry, ...],
    sections: tuple[Mapping[str, object], ...],
    sources: tuple[SupervisedTrainingEvidenceSourceAuthority, ...],
) -> SupervisedTrainingEvidenceEntryAuthority:
    frozen_sections = tuple(_freeze_json(item, "supervised entry section") for item in sections)
    if not all(isinstance(item, Mapping) for item in frozen_sections):
        raise BatchTrainingError("supervised entry sections must be objects")
    snapshots = tuple(build_supervised_training_evidence_snapshot(item) for item in sources)
    roles = tuple(entries)
    if not roles or len({item.project_entry_id for item in roles}) != 1:
        raise BatchTrainingError("supervised entry authority requires exact entry roles")
    bindings = tuple(
        item
        for item in plan.request.analysis_domain_bindings
        if item.project_entry_id == roles[0].project_entry_id
    )
    authority = object.__new__(SupervisedTrainingEvidenceEntryAuthority)
    values = {
        "_plan_set": plan_set,
        "_plan": plan,
        "_plan_entries": roles,
        "_sections": frozen_sections,
        "_analysis_domain_bindings": bindings,
        "_sources": tuple(sources),
        "_snapshots": snapshots,
        "_issuer": _SUPERVISED_ENTRY_ISSUER,
    }
    for name, value in values.items():
        object.__setattr__(authority, name, value)
    return _validate_supervised_training_evidence_entry(
        authority,
        expected_plan_set=plan_set,
    )


def open_supervised_training_evidence_entry(
    *,
    plan_set: object,
    measurements: tuple[object, ...],
    phenotyping_modes: tuple[object, ...],
    accepted_domains: tuple[object, ...],
    execution_evidence: object,
) -> SupervisedTrainingEvidenceEntryAuthority:
    """Open every authenticated section requested for one supervised entry."""

    from ..project_stage_plan import ProjectStageTargetExecutionEvidence
    from .planning import ValidatedTrainingPlanSetProjection

    if (
        type(plan_set) is not ValidatedTrainingPlanSetProjection
        or type(execution_evidence) is not ProjectStageTargetExecutionEvidence
    ):
        raise BatchTrainingError("supervised entry evidence requires owner authorities")
    try:
        validation_inputs = execution_evidence.reopen_validation_inputs()
    except Exception as exc:
        raise BatchTrainingError("supervised entry execution authority is invalid") from exc
    body = _closed_mapping(
        validation_inputs.scientific_request_body,
        fields=frozenset(
            {
                "schema_version",
                "sample_id",
                "supervised_training_evidence_requests",
            }
        ),
        field="supervised entry request body",
    )
    specification_fields = frozenset(
        {
            "training_plan_id",
            "split",
            "sections",
            "class_ids",
            "feature_schema",
            "point_selection_radius_level0",
        }
    )
    specifications = tuple(
        _closed_mapping(
            item,
            fields=specification_fields | ({"feature_source", "reference_selection_sha256"}
                & item.keys() if isinstance(item, Mapping) else set()),
            field="supervised entry request specification",
        )
        for item in _array(
            body["supervised_training_evidence_requests"],
            "supervised_training_evidence_requests",
        )
    )
    if not specifications:
        raise BatchTrainingError("supervised entry request has no plan roles")
    plan_ids = tuple(_text(item["training_plan_id"], "training_plan_id") for item in specifications)
    if len(set(plan_ids)) != 1:
        raise BatchTrainingError("supervised entry request crosses training plans")
    plan = plan_set.require_plan(plan_ids[0])
    entries = tuple(
        plan_set.require_entry(
            training_plan_id=plan.training_plan_id,
            project_entry_id=execution_evidence.authorized.project_entry_id,
            split=_text(item["split"], "split"),  # type: ignore[arg-type]
        )
        for item in specifications
    )
    section_fields = frozenset(
        {
            "section_id",
            "segmentation_generation_id",
            "measurement_generation_id",
            "analysis_domain_ids",
        }
    )
    section_rows = tuple(
        tuple(
            _closed_mapping(
                item,
                fields=section_fields,
                field="supervised entry section",
            )
            for item in _array(specification["sections"], "supervised entry sections")
        )
        for specification in specifications
    )
    sections = section_rows[0]
    if any(item != sections for item in section_rows[1:]):
        raise BatchTrainingError("supervised entry roles use different section coverage")
    section_ids = tuple(_text(item["section_id"], "section_id") for item in sections)
    if not section_ids or section_ids != tuple(sorted(set(section_ids))):
        raise BatchTrainingError("supervised entry section coverage is not canonical")
    sources = tuple(
        source
        for entry in entries
        for section_id in section_ids
        for source in (
            open_supervised_training_evidence_source(
                plan_set=plan_set,
                training_plan_id=plan.training_plan_id,
                project_entry_id=entry.project_entry_id,
                split=entry.split,
                section_id=section_id,
                measurements=measurements,
                phenotyping_modes=phenotyping_modes,
                accepted_domains=accepted_domains,
                execution_evidence=execution_evidence,
            ),
        )
    )
    snapshots = tuple(build_supervised_training_evidence_snapshot(item) for item in sources)
    if tuple((item.project_entry_id, item.split, item.section_id) for item in snapshots) != tuple(
        (entry.project_entry_id, entry.split, section_id)
        for entry in entries
        for section_id in section_ids
    ) or len({item.snapshot_sha256 for item in snapshots}) != len(snapshots):
        raise BatchTrainingError("supervised entry snapshots lack exact section coverage")
    expected_bindings = tuple(
        item
        for item in plan.request.analysis_domain_bindings
        if item.project_entry_id == entries[0].project_entry_id
    )
    actual_by_role = tuple(
        tuple(
            sorted(
                (
                    binding
                    for source in sources
                    if source._plan_entry == entry
                    for binding in source._analysis_domain_bindings
                ),
                key=_binding_sort_key,
            )
        )
        for entry in entries
    )
    if any(
        len(set(actual)) != len(actual) or actual != expected_bindings for actual in actual_by_role
    ):
        raise BatchTrainingError("supervised entry sources lack exact domain coverage")
    return _issue_supervised_training_evidence_entry(
        plan_set=plan_set,
        plan=plan,
        entries=entries,
        sections=sections,
        sources=sources,
    )


def _supervised_entry_commitments(
    sources: tuple[SupervisedTrainingEvidenceSourceAuthority, ...],
) -> tuple[str, str]:
    """Join our admitted content identities without copying scientific rows."""

    return (
        _digest([source._content_sha256 for source in sources]),
        _digest([source._snapshot.snapshot_sha256 for source in sources]),
    )


def build_supervised_training_evidence_execution_result(
    *,
    context: object,
    evidence: SupervisedTrainingEvidenceEntryAuthority,
) -> object:
    """Publish one compact entry artifact around existing source/snapshot owners."""

    authority = _validate_supervised_training_evidence_entry(evidence)
    values = authority._sources
    first = values[0]
    snapshots = authority._snapshots
    bindings = authority._analysis_domain_bindings
    # i) Our admitted sources retain their stable content commitments; scientific
    # rows remain in the immutable snapshots and original execution exchange.
    # ii) Publication joins those commitments without rebuilding full row trees.
    source_state_sha256, snapshot_set_sha256 = _supervised_entry_commitments(values)
    return _build_training_entry_execution_result(
        context=context,
        artifact_kind="supervised_training_evidence_snapshot",
        project_entry_id=first._plan_entry.project_entry_id,
        sample_id=first._plan_entry.sample_id,
        slide_id=first._plan_entry.slide_id,
        validation_domain_id=first._plan.request.validation_domain_id,
        training_plan_set_sha256=first._training_plan_set_sha256,
        analysis_domain_bindings=bindings,
        entry_state_sha256=source_state_sha256,
        entry_snapshot_set_sha256=snapshot_set_sha256,
        plan_role_rows=tuple(
            {
                "training_plan_id": authority._plan.training_plan_id,
                "classifier_role": authority._plan.request.classifier_kind.value,
                "split": entry.split,
            }
            for entry in authority._plan_entries
        ),
        saved_state_before_sha256=first._saved_state_before_sha256,
        saved_state_after_sha256=first._saved_state_after_sha256,
        post_binding_saved_state_sha256=first._saved_state_after_sha256,
        object_output_sha256=snapshot_set_sha256,
        object_output_count=len(snapshots),
    )


def _reopen_training_evidence_exchange(
    *,
    child: object,
    exchange_reopener: object,
    codec_binding: object,
    plan_set: object,
    expected_kinds: frozenset[str],
) -> object:
    """Reopen one current compact evidence child through its retained body."""

    from ..project_stage_codecs import ProjectStageCodecBinding
    from ..project_stage_plan import (
        ProjectStageCommittedExchangeReopener,
        ProjectStageRecordError,
    )
    from .planning import ValidatedTrainingPlanSetProjection

    if (
        type(child) is not BatchCommittedArtifactAuthority
        or type(exchange_reopener) is not ProjectStageCommittedExchangeReopener
        or type(codec_binding) is not ProjectStageCodecBinding
        or type(plan_set) is not ValidatedTrainingPlanSetProjection
    ):
        raise BatchTrainingError("training evidence reopen requires exact authorities")
    try:
        validate_committed_artifact_authority_set(
            (child,),
            expected_active_plan_sha256=plan_set.active_plan_sha256,
        )
    except BatchArtifactError as exc:
        raise BatchTrainingError("training evidence child authority is invalid") from exc
    ref = child.artifact_ref
    if (
        child.state_head_sha256 != plan_set.state_head_sha256
        or child.producer_node_kind not in expected_kinds
        or ref.artifact_kind != child.producer_node_kind
        or ref.producer_node_id is None
        or ref.producer_attempt_id is None
        or ref.producer_attempt_authorization_sha256 is None
        or child.execution_exchange_sha256 is None
        or ref.owning_identity.get("training_plan_set_sha256")
        != plan_set.plan_set.training_plan_set_sha256
        or codec_binding.terminal_stage_id != child.producer_node_kind
    ):
        raise BatchTrainingError("training evidence child differs from its plan or codec")
    try:
        reopened = exchange_reopener._reopen_for_owner(
            child,
            producer_artifact_ref_sha256=ref.artifact_ref_sha256,
            producer_node_id=ref.producer_node_id,
            producer_attempt_id=ref.producer_attempt_id,
            producer_attempt_authorization_sha256=(ref.producer_attempt_authorization_sha256),
            terminal_record_sha256=child.terminal_record_sha256,
            execution_exchange_sha256=child.execution_exchange_sha256,
            expected_binding=(
                codec_binding.adapter_spec_sha256,
                codec_binding.target_script_asset_id,
                codec_binding.wire_stage_id,
                codec_binding.terminal_stage_id,
                codec_binding.request_codec_version,
            ),
        )
    except (ProjectStageRecordError, TypeError, ValueError) as exc:
        raise BatchTrainingError("training evidence retained exchange cannot be reopened") from exc
    return reopened.evidence


def reopen_pixel_training_evidence_set(
    *,
    child: object,
    exchange_reopener: object,
    codec_binding: object,
    plan_set: object,
    accepted_domains: tuple[object, ...],
    primary_results: tuple[object, ...] = (),
    primary_skips: tuple[object, ...] = (),
) -> PixelTrainingEvidenceSet:
    """Reauthenticate one complete pixel entry body and its compact identity."""

    evidence = _reopen_training_evidence_exchange(
        child=child,
        exchange_reopener=exchange_reopener,
        codec_binding=codec_binding,
        plan_set=plan_set,
        expected_kinds=frozenset(
            {
                "primary_training_evidence_snapshot",
                "secondary_training_evidence_snapshot",
            }
        ),
    )
    opened = open_pixel_training_evidence_set(
        plan_set=plan_set,
        accepted_domains=accepted_domains,
        execution_evidence=evidence,
        primary_results=primary_results,
        primary_skips=primary_skips,
    )
    rebuilt = build_pixel_training_evidence_execution_result(
        context=evidence.context,
        evidence=opened,
    )
    if (
        opened.execution_exchange_sha256 != child.execution_exchange_sha256
        or len(rebuilt.outputs) != 1
        or rebuilt.outputs[0] != child.artifact_ref
    ):
        raise BatchTrainingError(
            "pixel evidence compact identity differs from its retained exchange"
        )
    return opened


def reopen_supervised_training_evidence_entry(
    *,
    child: object,
    exchange_reopener: object,
    codec_binding: object,
    plan_set: object,
    accepted_domains: tuple[object, ...],
    measurements: tuple[object, ...],
    phenotyping_modes: tuple[object, ...],
) -> SupervisedTrainingEvidenceEntryAuthority:
    """Reauthenticate all supervised sections and their compact entry identity."""

    evidence = _reopen_training_evidence_exchange(
        child=child,
        exchange_reopener=exchange_reopener,
        codec_binding=codec_binding,
        plan_set=plan_set,
        expected_kinds=frozenset({"supervised_training_evidence_snapshot"}),
    )
    entry_evidence = open_supervised_training_evidence_entry(
        plan_set=plan_set,
        measurements=measurements,
        phenotyping_modes=phenotyping_modes,
        accepted_domains=accepted_domains,
        execution_evidence=evidence,
    )
    rebuilt = build_supervised_training_evidence_execution_result(
        context=evidence.context,
        evidence=entry_evidence,
    )
    if (
        any(
            source._execution_exchange_sha256 != child.execution_exchange_sha256
            for source in entry_evidence._sources
        )
        or len(rebuilt.outputs) != 1
        or rebuilt.outputs[0] != child.artifact_ref
    ):
        raise BatchTrainingError(
            "supervised evidence compact identity differs from its retained exchange"
        )
    return entry_evidence


def _supervised_cell_from_payload(value: object) -> CellRecord:
    """Expand the measured projection while retaining its exact native geometry."""

    mapping = _closed_mapping(
        value,
        fields=_SUPERVISED_COMPACT_CELL_FIELDS,
        field="eligible_cell",
    )
    x = mapping["x_level0"]
    y = mapping["y_level0"]
    if (
        isinstance(x, bool)
        or not isinstance(x, (int, float))
        or isinstance(y, bool)
        or not isinstance(y, (int, float))
    ):
        raise BatchTrainingError("eligible-cell centroids must be numeric")
    try:
        return CellRecord(
            cell_id=mapping["cell_id"],  # type: ignore[arg-type]
            slide_id=mapping["slide_id"],  # type: ignore[arg-type]
            section_id=mapping["section_id"],  # type: ignore[arg-type]
            x_level0=float(x),
            y_level0=float(y),
            x_um=None,
            y_um=None,
            nucleus_geometry=mapping["nucleus_geometry"],
            cell_geometry=mapping["cell_geometry"],
            primary_compartment=mapping["primary_compartment"],  # type: ignore[arg-type]
            secondary_roi_ids=(),
            marker_measurements=mapping["marker_measurements"],  # type: ignore[arg-type]
            marker_states={},
            analysis_domain_id=mapping["analysis_domain_id"],  # type: ignore[arg-type]
            analysis_domain_kind=AnalysisDomainKind(mapping["analysis_domain_kind"]),
        )
    except (ROIAnalysisError, TypeError, ValueError) as exc:
        raise BatchTrainingError("eligible-cell projection is invalid") from exc


def _supervised_training_projection_sha256(plan: TrainingPlan) -> str:
    """Hash only the supervised fitter's training-role plan material."""

    if type(plan) is not TrainingPlan:
        raise BatchTrainingError("plan must be a TrainingPlan")
    if plan.request.classifier_kind is not ClassifierKind.SUPERVISED_PHENOTYPE:
        raise BatchTrainingError("supervised fit evidence requires a supervised plan")
    from .engines import TrainingFitPlanProjection

    return TrainingFitPlanProjection.create(plan).fit_plan_sha256


def _supervised_snapshot_schema(
    entries: tuple[SupervisedTrainingEvidenceSnapshot, ...],
) -> tuple[tuple[str, ...], tuple[SupervisedFeatureDefinition, ...]]:
    first = entries[0]
    class_ids = first.class_ids
    feature_schema = first.feature_schema
    if any(
        item.class_ids != class_ids or item.feature_schema != feature_schema for item in entries[1:]
    ):
        raise BatchTrainingError(
            "supervised evidence class or feature coverage differs across snapshots"
        )
    return class_ids, feature_schema


def _supervised_fit_snapshot_schema(
    entries: tuple[SupervisedFitEvidenceSnapshot, ...],
) -> tuple[tuple[str, ...], tuple[SupervisedFeatureDefinition, ...]]:
    first = entries[0]
    class_ids = first.class_ids
    feature_schema = first.feature_schema
    if any(
        item.class_ids != class_ids or item.feature_schema != feature_schema for item in entries[1:]
    ):
        raise BatchTrainingError(
            "supervised fit class or feature coverage differs across snapshots"
        )
    return class_ids, feature_schema


def _entry_scoped_cells(
    entries: tuple[SupervisedTrainingEvidenceSnapshot, ...],
    split: str,
) -> tuple[object, ...]:
    from .records import EntryScopedCellIdentity

    return tuple(
        EntryScopedCellIdentity(project_entry_id, cell_id)
        for project_entry_id, cell_id in sorted(
            {
                (snapshot.project_entry_id, item.cell_id)
                for snapshot in entries
                if snapshot.split == split
                for item in snapshot.mapped_evidence
            }
        )
    )


def _entry_scoped_regions(
    entries: tuple[SupervisedTrainingEvidenceSnapshot, ...],
    split: str,
) -> tuple[object, ...]:
    from .records import EntryScopedRegionIdentity

    return tuple(
        EntryScopedRegionIdentity(project_entry_id, spatial_region_id)
        for project_entry_id, spatial_region_id in sorted(
            {
                (snapshot.project_entry_id, item.spatial_region_id)
                for snapshot in entries
                if snapshot.split == split
                for item in snapshot.mapped_evidence
            }
        )
    )


def _validate_supervised_aggregate_splits(
    entries: tuple[SupervisedTrainingEvidenceSnapshot, ...],
) -> None:
    training = tuple(item for item in entries if item.split == "training")
    validation = tuple(item for item in entries if item.split == "validation")
    test = tuple(item for item in entries if item.split == "test")
    if not training:
        raise BatchTrainingError("complete supervised evidence requires training")
    if not validation and not test:
        seen_cells: set[tuple[str, str]] = set()
        for snapshot in training:
            for item in snapshot.mapped_evidence:
                cell = (snapshot.project_entry_id, item.cell_id)
                if cell in seen_cells:
                    raise BatchTrainingError(
                        "duplicate entry-scoped training cell in supervised evidence"
                    )
                seen_cells.add(cell)
        return
    if validation:
        _validate_supervised_outer_splits(training, validation)
    if test:
        _validate_supervised_outer_splits((*training, *validation), test)


@dataclass(frozen=True, slots=True)
class MultiImageSupervisedTrainingAggregate:
    """Complete immutable all-role supervised evidence for audit and acceptance."""

    training_plan_id: str
    normalized_manifest_sha256: str
    entries: tuple[SupervisedTrainingEvidenceSnapshot, ...]
    material_evidence_sha256: str
    publication_audit_sha256: str | None
    evidence_set_sha256: str

    def __post_init__(self) -> None:
        """Validate the MultiImageSupervisedTrainingAggregate fields when the record is
        constructed.

        The checks cover ``entries``, ``evidence_set_sha256``, ``material_evidence_sha256``,
        ``training_plan_id``, ``normalized_manifest_sha256``, ``publication_audit_sha256``.
        Invalid field types, values or combinations raise the declared validation error
        before this instance can be used by its caller."""

        object.__setattr__(
            self,
            "training_plan_id",
            _sha256(self.training_plan_id, "training_plan_id"),
        )
        object.__setattr__(
            self,
            "normalized_manifest_sha256",
            _sha256(
                self.normalized_manifest_sha256,
                "normalized_manifest_sha256",
            ),
        )
        entries = tuple(self.entries)
        if not entries or not all(
            type(item) is SupervisedTrainingEvidenceSnapshot for item in entries
        ):
            raise BatchTrainingError("entries must contain supervised snapshots")
        if entries != tuple(sorted(entries, key=_supervised_snapshot_sort_key)):
            raise BatchTrainingError("supervised evidence entries must use canonical order")
        _supervised_snapshot_schema(entries)
        _validate_supervised_aggregate_splits(entries)
        evidence_set_sha256 = _sha256(self.evidence_set_sha256, "evidence_set_sha256")
        if evidence_set_sha256 != _digest([item.to_mapping() for item in entries]):
            raise BatchTrainingError("evidence_set_sha256 is not canonical")
        object.__setattr__(self, "entries", entries)
        object.__setattr__(self, "evidence_set_sha256", evidence_set_sha256)
        publication = (
            None
            if self.publication_audit_sha256 is None
            else _sha256(self.publication_audit_sha256, "publication_audit_sha256")
        )
        object.__setattr__(self, "publication_audit_sha256", publication)
        material = _sha256(self.material_evidence_sha256, "material_evidence_sha256")
        if material != _digest(self._material_mapping()):
            raise BatchTrainingError("material_evidence_sha256 is not canonical")
        object.__setattr__(self, "material_evidence_sha256", material)

    @classmethod
    def create(
        cls,
        *,
        plan: TrainingPlan,
        entries: tuple[SupervisedTrainingEvidenceSnapshot, ...],
        publication_audit_sha256: str | None,
    ) -> "MultiImageSupervisedTrainingAggregate":
        owner_entries = tuple(sorted(entries, key=_supervised_snapshot_sort_key))
        _validate_supervised_plan_coverage(owner_entries, plan, include_test=True)
        _validate_supervised_aggregate_splits(owner_entries)
        evidence_set_sha256 = _digest([item.to_mapping() for item in owner_entries])
        material = {
            "training_plan_id": plan.training_plan_id,
            "normalized_manifest_sha256": plan.normalized_manifest_sha256,
            "evidence_set_sha256": evidence_set_sha256,
        }
        return cls(
            training_plan_id=plan.training_plan_id,
            normalized_manifest_sha256=plan.normalized_manifest_sha256,
            entries=owner_entries,
            material_evidence_sha256=_digest(material),
            publication_audit_sha256=publication_audit_sha256,
            evidence_set_sha256=evidence_set_sha256,
        )

    @classmethod
    def from_mapping(
        cls,
        value: object,
        *,
        plan: TrainingPlan | None = None,
    ) -> "MultiImageSupervisedTrainingAggregate":
        """Decode the serialized fields into a validated MultiImageSupervisedTrainingAggregate
        record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""

        mapping = _closed_mapping(
            value,
            fields=frozenset(cls.__dataclass_fields__),
            field="multi_image_supervised_training_aggregate",
        )
        result = cls(
            training_plan_id=mapping["training_plan_id"],  # type: ignore[arg-type]
            normalized_manifest_sha256=mapping[
                "normalized_manifest_sha256"
            ],  # type: ignore[arg-type]
            entries=tuple(
                SupervisedTrainingEvidenceSnapshot.from_mapping(item)
                for item in _array(mapping["entries"], "entries")
            ),
            material_evidence_sha256=mapping["material_evidence_sha256"],  # type: ignore[arg-type]
            publication_audit_sha256=mapping["publication_audit_sha256"],  # type: ignore[arg-type]
            evidence_set_sha256=mapping["evidence_set_sha256"],  # type: ignore[arg-type]
        )
        if plan is not None:
            if (
                result.training_plan_id != plan.training_plan_id
                or result.normalized_manifest_sha256 != plan.normalized_manifest_sha256
            ):
                raise BatchTrainingError("complete supervised evidence differs from its plan")
            _validate_supervised_plan_coverage(result.entries, plan, include_test=True)
        return result

    @property
    def class_ids(self) -> tuple[str, ...]:
        return _supervised_snapshot_schema(self.entries)[0]

    @property
    def feature_schema(self) -> tuple[SupervisedFeatureDefinition, ...]:
        return _supervised_snapshot_schema(self.entries)[1]

    @property
    def training_cells(self) -> tuple[object, ...]:
        return _entry_scoped_cells(self.entries, "training")

    @property
    def validation_cells(self) -> tuple[object, ...]:
        return _entry_scoped_cells(self.entries, "validation")

    @property
    def test_cells(self) -> tuple[object, ...]:
        return _entry_scoped_cells(self.entries, "test")

    @property
    def training_regions(self) -> tuple[object, ...]:
        return _entry_scoped_regions(self.entries, "training")

    @property
    def validation_regions(self) -> tuple[object, ...]:
        return _entry_scoped_regions(self.entries, "validation")

    @property
    def test_regions(self) -> tuple[object, ...]:
        return _entry_scoped_regions(self.entries, "test")

    def _material_mapping(self) -> dict[str, object]:
        return {
            "training_plan_id": self.training_plan_id,
            "normalized_manifest_sha256": self.normalized_manifest_sha256,
            "evidence_set_sha256": self.evidence_set_sha256,
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            "training_plan_id": self.training_plan_id,
            "normalized_manifest_sha256": self.normalized_manifest_sha256,
            "entries": [item.to_mapping() for item in self.entries],
            "material_evidence_sha256": self.material_evidence_sha256,
            "publication_audit_sha256": self.publication_audit_sha256,
            "evidence_set_sha256": self.evidence_set_sha256,
        }


@dataclass(frozen=True, slots=True)
class SupervisedFitEvidenceSnapshot:
    """Canonical provider value projected from one training-role owner snapshot."""

    project_entry_id: str
    sample_id: str
    slide_id: str
    subject_id: str
    split: str
    image_identity_sha256: str
    section_id: str
    pipeline_run_id: str
    segmentation_generation_id: str
    measurement_generation_id: str
    analysis_domain_binding_sha256s: tuple[str, ...]
    analysis_domain_binding_set_sha256: str
    coordinate_system: str
    point_selection_radius_level0: float
    training_cell_to_analysis_domain_mapping_sha256: str
    class_ids: tuple[str, ...]
    feature_schema: tuple[SupervisedFeatureDefinition, ...]
    point_evidence: tuple[object, ...]
    eligible_cells: tuple[CellRecord, ...]
    mapped_evidence: tuple[MappedPointEvidence, ...]
    snapshot_sha256: str
    deferred_feature_evidence: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        """Validate the SupervisedFitEvidenceSnapshot fields when the record is constructed.

        The checks cover ``point_selection_radius_level0``, ``split``,
        ``coordinate_system``, ``class_ids``, ``feature_schema``, ``eligible_cells``.
        Invalid field types, values or combinations raise the declared validation error
        before this instance can be used by its caller."""

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
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.split != "training":
            raise BatchTrainingError("supervised fit snapshots require exactly the training role")
        for name in (
            "image_identity_sha256",
            "analysis_domain_binding_set_sha256",
            "training_cell_to_analysis_domain_mapping_sha256",
        ):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        object.__setattr__(
            self,
            "analysis_domain_binding_sha256s",
            _sha256_tuple(
                self.analysis_domain_binding_sha256s,
                "analysis_domain_binding_sha256s",
                nonempty=True,
            ),
        )
        if self.coordinate_system != "global_level0":
            raise BatchTrainingError("coordinate_system must be global_level0")
        radius = self.point_selection_radius_level0
        if isinstance(radius, bool) or not isinstance(radius, (int, float)) or radius <= 0:
            raise BatchTrainingError("point_selection_radius_level0 must be positive")
        object.__setattr__(self, "point_selection_radius_level0", float(radius))
        class_ids = _string_tuple(self.class_ids, "class_ids", nonempty=True, canonical=False)
        object.__setattr__(self, "class_ids", class_ids)
        schema = tuple(self.feature_schema)
        if not schema or not all(type(item) is SupervisedFeatureDefinition for item in schema):
            raise BatchTrainingError("feature_schema must contain typed definitions")
        if len({item.measurement_name for item in schema}) != len(schema):
            raise BatchTrainingError("feature_schema measurement names must be unique")
        object.__setattr__(self, "feature_schema", schema)
        points = tuple(
            _freeze_json(item, f"point_evidence[{index}]")
            for index, item in enumerate(self.point_evidence)
        )
        cells = tuple(self.eligible_cells)
        mapped = tuple(self.mapped_evidence)
        if (
            points != tuple(sorted(points, key=lambda item: item["point_id"]))
            or not all(type(item) is CellRecord for item in cells)
            or cells != tuple(sorted(cells, key=lambda item: item.cell_id))
            or not all(type(item) is MappedPointEvidence for item in mapped)
            or mapped != tuple(sorted(mapped, key=lambda item: item.point_id))
        ):
            raise BatchTrainingError(
                "supervised fit snapshot values must be typed and canonical"
            )
        point_ids = tuple(item["point_id"] for item in points)
        cell_ids = tuple(item.cell_id for item in cells)
        mapped_point_ids = tuple(item.point_id for item in mapped)
        mapped_cell_ids = tuple(sorted({item.cell_id for item in mapped}))
        if (
            len(set(point_ids)) != len(point_ids)
            or len(set(cell_ids)) != len(cell_ids)
            or any(item.evidence_set != "training" for item in mapped)
            or tuple(sorted(point_ids)) != tuple(sorted(mapped_point_ids))
            or cell_ids != mapped_cell_ids
            or any(
                item.slide_id != self.slide_id or item.section_id != self.section_id
                for item in cells
            )
        ):
            raise BatchTrainingError("supervised fit snapshot contains non-training evidence")
        mapping_sha256 = _digest({item.cell_id: item.analysis_domain_id for item in cells})
        if self.training_cell_to_analysis_domain_mapping_sha256 != mapping_sha256:
            raise BatchTrainingError("training cell-to-domain mapping digest is not canonical")
        object.__setattr__(self, "point_evidence", points)
        object.__setattr__(self, "eligible_cells", cells)
        object.__setattr__(self, "mapped_evidence", mapped)
        object.__setattr__(self, "deferred_feature_evidence", validate_snapshot_inputs(
            cells, schema, self.deferred_feature_evidence,
        ))
        supplied = _sha256(self.snapshot_sha256, "snapshot_sha256")
        if supplied != _digest(self._unsigned_mapping()):
            raise BatchTrainingError("supervised fit snapshot_sha256 is not canonical")
        object.__setattr__(self, "snapshot_sha256", supplied)

    @classmethod
    def create(
        cls,
        owner: SupervisedTrainingEvidenceSnapshot,
    ) -> "SupervisedFitEvidenceSnapshot":
        if type(owner) is not SupervisedTrainingEvidenceSnapshot or owner.split != "training":
            raise BatchTrainingError("supervised fit projection requires a training owner snapshot")
        mapped = tuple(
            sorted(
                (item for item in owner.mapped_evidence if item.evidence_set == "training"),
                key=lambda item: item.point_id,
            )
        )
        point_ids = {item.point_id for item in mapped}
        cell_ids = {item.cell_id for item in mapped}
        points = tuple(
            sorted(
                (
                    _export_json(item)
                    for item in owner.point_evidence
                    if isinstance(item, Mapping) and item.get("point_id") in point_ids
                ),
                key=lambda item: item["point_id"],
            )
        )
        cells = tuple(
            sorted(
                (item for item in owner.eligible_cells if item.cell_id in cell_ids),
                key=lambda item: item.cell_id,
            )
        )
        unsigned = {
            "deferred_feature_evidence": project_deferred_evidence(
                owner.deferred_feature_evidence, (cell.cell_id for cell in cells),
            ).get(EVIDENCE_FIELD),
            "project_entry_id": owner.project_entry_id,
            "sample_id": owner.sample_id,
            "slide_id": owner.slide_id,
            "subject_id": owner.subject_id,
            "split": "training",
            "image_identity_sha256": owner.image_identity_sha256,
            "section_id": owner.section_id,
            "pipeline_run_id": owner.pipeline_run_id,
            "segmentation_generation_id": owner.segmentation_generation_id,
            "measurement_generation_id": owner.measurement_generation_id,
            "analysis_domain_binding_sha256s": (owner.analysis_domain_binding_sha256s),
            "analysis_domain_binding_set_sha256": (owner.analysis_domain_binding_set_sha256),
            "coordinate_system": owner.coordinate_system,
            "point_selection_radius_level0": (owner.point_selection_radius_level0),
            "training_cell_to_analysis_domain_mapping_sha256": _digest(
                {item.cell_id: item.analysis_domain_id for item in cells}
            ),
            "class_ids": owner.class_ids,
            "feature_schema": owner.feature_schema,
            "point_evidence": points,
            "eligible_cells": cells,
            "mapped_evidence": mapped,
        }
        prototype = object.__new__(cls)
        for name, value in unsigned.items():
            object.__setattr__(prototype, name, value)
        return cls(
            **unsigned,
            snapshot_sha256=_digest(prototype._unsigned_mapping()),
        )

    @classmethod
    def from_mapping(cls, value: object) -> "SupervisedFitEvidenceSnapshot":
        """Decode the serialized fields into a validated SupervisedFitEvidenceSnapshot record.

        The parser checks the supplied mapping and converts its nested values through the
        declared constructors before constructing the record."""

        if isinstance(value, Mapping) and EVIDENCE_FIELD in value and value[EVIDENCE_FIELD] is None:
            raise BatchTrainingError("Optional deferred feature evidence cannot be null")
        mapping = _closed_mapping(
            value,
            fields=(frozenset(cls.__dataclass_fields__) - {EVIDENCE_FIELD})
                | ({EVIDENCE_FIELD} if isinstance(value, Mapping) and EVIDENCE_FIELD in value else set()),
            field="supervised_fit_evidence_snapshot",
        )
        return cls(
            project_entry_id=mapping["project_entry_id"],  # type: ignore[arg-type]
            sample_id=mapping["sample_id"],  # type: ignore[arg-type]
            slide_id=mapping["slide_id"],  # type: ignore[arg-type]
            subject_id=mapping["subject_id"],  # type: ignore[arg-type]
            split=mapping["split"],  # type: ignore[arg-type]
            image_identity_sha256=mapping["image_identity_sha256"],  # type: ignore[arg-type]
            section_id=mapping["section_id"],  # type: ignore[arg-type]
            pipeline_run_id=mapping["pipeline_run_id"],  # type: ignore[arg-type]
            segmentation_generation_id=mapping[
                "segmentation_generation_id"
            ],  # type: ignore[arg-type]
            measurement_generation_id=mapping[
                "measurement_generation_id"
            ],  # type: ignore[arg-type]
            analysis_domain_binding_sha256s=tuple(
                _array(
                    mapping["analysis_domain_binding_sha256s"],
                    "analysis_domain_binding_sha256s",
                )
            ),  # type: ignore[arg-type]
            analysis_domain_binding_set_sha256=mapping[
                "analysis_domain_binding_set_sha256"
            ],  # type: ignore[arg-type]
            coordinate_system=mapping["coordinate_system"],  # type: ignore[arg-type]
            point_selection_radius_level0=mapping[
                "point_selection_radius_level0"
            ],  # type: ignore[arg-type]
            training_cell_to_analysis_domain_mapping_sha256=mapping[
                "training_cell_to_analysis_domain_mapping_sha256"
            ],  # type: ignore[arg-type]
            class_ids=tuple(_array(mapping["class_ids"], "class_ids")),  # type: ignore[arg-type]
            feature_schema=tuple(
                SupervisedFeatureDefinition.from_mapping(item)
                for item in _array(mapping["feature_schema"], "feature_schema")
            ),
            point_evidence=tuple(_array(mapping["point_evidence"], "point_evidence")),
            eligible_cells=tuple(
                CellRecord.from_mapping(item)  # type: ignore[arg-type]
                for item in _array(mapping["eligible_cells"], "eligible_cells")
            ),
            mapped_evidence=tuple(
                _mapped_point_from_mapping(item)
                for item in _array(mapping["mapped_evidence"], "mapped_evidence")
            ),
            deferred_feature_evidence=mapping.get(EVIDENCE_FIELD),
            snapshot_sha256=mapping["snapshot_sha256"],  # type: ignore[arg-type]
        )

    def _unsigned_mapping(self) -> dict[str, object]:
        return {
            **project_deferred_evidence(self.deferred_feature_evidence,
                                        (cell.cell_id for cell in self.eligible_cells)),
            "project_entry_id": self.project_entry_id,
            "sample_id": self.sample_id,
            "slide_id": self.slide_id,
            "subject_id": self.subject_id,
            "split": self.split,
            "image_identity_sha256": self.image_identity_sha256,
            "section_id": self.section_id,
            "pipeline_run_id": self.pipeline_run_id,
            "segmentation_generation_id": self.segmentation_generation_id,
            "measurement_generation_id": self.measurement_generation_id,
            "analysis_domain_binding_sha256s": list(self.analysis_domain_binding_sha256s),
            "analysis_domain_binding_set_sha256": (self.analysis_domain_binding_set_sha256),
            "coordinate_system": self.coordinate_system,
            "point_selection_radius_level0": (self.point_selection_radius_level0),
            "training_cell_to_analysis_domain_mapping_sha256": (
                self.training_cell_to_analysis_domain_mapping_sha256
            ),
            "class_ids": list(self.class_ids),
            "feature_schema": [item.to_mapping() for item in self.feature_schema],
            "point_evidence": [_export_json(item) for item in self.point_evidence],
            "eligible_cells": [item.to_export_mapping() for item in self.eligible_cells],
            "mapped_evidence": [_mapped_point_to_mapping(item) for item in self.mapped_evidence],
        }

    def to_mapping(self) -> dict[str, object]:
        return {**self._unsigned_mapping(), "snapshot_sha256": self.snapshot_sha256}


@dataclass(frozen=True, slots=True)
class MultiImageSupervisedFitEvidence:
    """Provider-visible supervised evidence containing training material only."""

    training_projection_sha256: str
    entries: tuple[SupervisedFitEvidenceSnapshot, ...]
    material_evidence_sha256: str
    evidence_set_sha256: str

    def __post_init__(self) -> None:
        """Validate the MultiImageSupervisedFitEvidence fields when the record is constructed.

        The checks cover ``entries``, ``evidence_set_sha256``, ``material_evidence_sha256``,
        ``training_projection_sha256``. Invalid field types, values or combinations raise
        the declared validation error before this instance can be used by its caller."""

        object.__setattr__(
            self,
            "training_projection_sha256",
            _sha256(
                self.training_projection_sha256,
                "training_projection_sha256",
            ),
        )
        entries = tuple(self.entries)
        if not entries or not all(
            isinstance(item, SupervisedFitEvidenceSnapshot) for item in entries
        ):
            raise BatchTrainingError("entries must contain supervised snapshots")
        if entries != tuple(sorted(entries, key=_supervised_snapshot_sort_key)):
            raise BatchTrainingError("supervised fit entries must use canonical order")
        if {item.split for item in entries} != {"training"}:
            raise BatchTrainingError("supervised fit evidence requires exactly the training role")
        _supervised_fit_snapshot_schema(entries)
        evidence_set_sha256 = _sha256(self.evidence_set_sha256, "evidence_set_sha256")
        if evidence_set_sha256 != _digest([item.to_mapping() for item in entries]):
            raise BatchTrainingError("evidence_set_sha256 is not canonical")
        object.__setattr__(self, "entries", entries)
        object.__setattr__(self, "evidence_set_sha256", evidence_set_sha256)
        material = _sha256(self.material_evidence_sha256, "material_evidence_sha256")
        if material != _digest(self._material_mapping()):
            raise BatchTrainingError("material_evidence_sha256 is not canonical")
        object.__setattr__(self, "material_evidence_sha256", material)

    @classmethod
    def create(
        cls,
        *,
        plan: TrainingPlan,
        entries: tuple[SupervisedTrainingEvidenceSnapshot, ...],
    ) -> "MultiImageSupervisedFitEvidence":
        owner_entries = tuple(
            sorted(
                (SupervisedFitEvidenceSnapshot.create(item) for item in entries),
                key=_supervised_fit_snapshot_sort_key,
            )
        )
        result = cls(
            training_projection_sha256=_supervised_training_projection_sha256(plan),
            entries=owner_entries,
            material_evidence_sha256=_digest(
                {
                    "training_projection_sha256": (_supervised_training_projection_sha256(plan)),
                    "evidence_set_sha256": _digest([item.to_mapping() for item in owner_entries]),
                }
            ),
            evidence_set_sha256=_digest([item.to_mapping() for item in owner_entries]),
        )
        _validate_supervised_fit_plan(result, plan)
        return result

    @classmethod
    def from_mapping(
        cls,
        value: object,
        *,
        plan: TrainingPlan | None = None,
    ) -> "MultiImageSupervisedFitEvidence":
        mapping = _closed_mapping(
            value,
            fields=frozenset(cls.__dataclass_fields__),
            field="multi_image_supervised_fit_evidence",
        )
        result = cls(
            training_projection_sha256=mapping[
                "training_projection_sha256"
            ],  # type: ignore[arg-type]
            entries=tuple(
                SupervisedFitEvidenceSnapshot.from_mapping(item)
                for item in _array(mapping["entries"], "entries")
            ),
            material_evidence_sha256=mapping["material_evidence_sha256"],  # type: ignore[arg-type]
            evidence_set_sha256=mapping["evidence_set_sha256"],  # type: ignore[arg-type]
        )
        if plan is not None:
            _validate_supervised_fit_plan(result, plan)
        return result

    def _material_mapping(self) -> dict[str, object]:
        return {
            "training_projection_sha256": self.training_projection_sha256,
            "evidence_set_sha256": self.evidence_set_sha256,
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            "training_projection_sha256": self.training_projection_sha256,
            "entries": [item.to_mapping() for item in self.entries],
            "material_evidence_sha256": self.material_evidence_sha256,
            "evidence_set_sha256": self.evidence_set_sha256,
        }


@dataclass(frozen=True, init=False, repr=False)
class SupervisedFitEvidenceAuthority:
    """Current-parent-bound authorization for supervised provider dispatch."""

    _training_plan_id: str
    _training_plan_set_sha256: str
    _state_head_sha256: str
    _active_plan_sha256: str
    _parent_execution_context_sha256: str
    _parent_node_id: str
    _parent_authorization_sha256: str
    _parent_node_material_sha256: str
    _parent_current_execution_seal_sha256: str
    _parent_execution_seal_sha256: str
    _plan_entries: tuple[TrainingPlanEntry, ...]
    _child_authorities: tuple[BatchCommittedArtifactAuthority, ...]
    _sources: tuple[SupervisedTrainingEvidenceSourceAuthority, ...]
    _evidence: MultiImageSupervisedFitEvidence
    _parent_execution: object
    _expected_context: object
    _state_store: object
    _state_store_identity: int
    _reservation_nonce: str
    _authority_object_identity: int
    _seal_sha256: str
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("SupervisedFitEvidenceAuthority is issuer-only")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("SupervisedFitEvidenceAuthority cannot be subclassed")

    def __copy__(self) -> object:
        raise TypeError("SupervisedFitEvidenceAuthority is nonserializable")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("SupervisedFitEvidenceAuthority is nonserializable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("SupervisedFitEvidenceAuthority is nonserializable")


def _supervised_fit_authority_seal_material(
    *,
    parent: object,
    plan: TrainingPlan,
    child_authorities: tuple[BatchCommittedArtifactAuthority, ...],
    sources: tuple[SupervisedTrainingEvidenceSourceAuthority, ...],
    evidence: MultiImageSupervisedFitEvidence,
    state_store_identity: int,
    reservation_nonce: str,
    authority_object_identity: int,
) -> dict[str, object]:
    return {
        "training_plan_id": plan.training_plan_id,
        "training_plan_set_sha256": (parent.training_plan_set.plan_set.training_plan_set_sha256),
        "state_head_sha256": parent.training_plan_set.state_head_sha256,
        "active_plan_sha256": parent.training_plan_set.active_plan_sha256,
        "parent_execution_context_sha256": parent.execution_context_sha256,
        "parent_node_id": parent.node_id,
        "parent_authorization_sha256": parent.authorization_sha256,
        "parent_node_material_sha256": (parent._current_execution.node_material_sha256),
        "parent_current_execution_seal_sha256": (parent._current_execution._seal_sha256),
        "parent_execution_seal_sha256": parent._seal_sha256,
        "plan_entries": [
            item.to_mapping() for item in plan.request.entries if item.split == "training"
        ],
        "child_authority_seal_sha256s": [item._seal_sha256 for item in child_authorities],
        "source_seal_sha256s": [item._seal_sha256 for item in sources],
        "provider_evidence": evidence.to_mapping(),
        "state_store_identity": state_store_identity,
        "reservation_nonce": reservation_nonce,
        "authority_object_identity": authority_object_identity,
    }


def _validate_supervised_fit_evidence_authority(
    authority: object,
    *,
    require_current: bool,
) -> tuple[TrainingPlan, MultiImageSupervisedFitEvidence]:
    """Validate supervised fit evidence authority for the current operation.

    We reuse the issuer's immutable scientific evidence and check its current
    parent, plan roles, child artifacts and reservation before dispatch. External
    retained evidence is decoded by its storage owner."""

    from ..batch_artifacts import BatchNodeExecutionContext
    from ..batch_state import BatchStateStore
    from .parent_execution import (
        CurrentTrainingParentExecution,
        validate_current_training_parent_execution,
    )

    if (
        type(authority) is not SupervisedFitEvidenceAuthority
        or getattr(authority, "_issuer", None) is not _SUPERVISED_FIT_ISSUER
        or type(authority._parent_execution) is not CurrentTrainingParentExecution
        or type(authority._expected_context) is not BatchNodeExecutionContext
        or type(authority._state_store) is not BatchStateStore
        or authority._state_store_identity != id(authority._state_store)
    ):
        raise BatchTrainingError("supervised fit evidence authority issuer is invalid")
    _SUPERVISED_FIT_ISSUER.assert_reserved(authority)
    parent = validate_current_training_parent_execution(
        authority._parent_execution,
        expected_context=authority._expected_context,
        state_store=authority._state_store if require_current else None,
    )
    plan_set = parent.training_plan_set
    current_plan = plan_set.require_plan(authority._training_plan_id)
    expected_entries = tuple(
        item for item in current_plan.request.entries if item.split == "training"
    )
    training_entry_ids = {item.project_entry_id for item in expected_entries}
    expected_children = tuple(
        sorted(
            (
                item
                for item in parent.evidence_child_authorities
                if item.artifact_ref.scope.project_entry_id in training_entry_ids
            ),
            key=lambda item: item.artifact_ref.artifact_ref_sha256,
        )
    )
    try:
        child_authorities = validate_committed_artifact_authority_set(
            authority._child_authorities,
            expected_active_plan_sha256=authority._active_plan_sha256,
        )
        evidence = authority._evidence
        if type(evidence) is not MultiImageSupervisedFitEvidence:
            raise BatchTrainingError("supervised fit requires its typed evidence owner")
        _validate_supervised_fit_plan(evidence, current_plan)
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchTrainingError("supervised fit evidence authority is invalid") from exc
    if (
        parent.classifier_kind is not ClassifierKind.SUPERVISED_PHENOTYPE
        or plan_set.state_head_sha256 != authority._state_head_sha256
        or plan_set.active_plan_sha256 != authority._active_plan_sha256
        or plan_set.plan_set.training_plan_set_sha256 != authority._training_plan_set_sha256
        or parent.execution_context_sha256 != authority._parent_execution_context_sha256
        or parent.node_id != authority._parent_node_id
        or parent.authorization_sha256 != authority._parent_authorization_sha256
        or parent._current_execution.node_material_sha256 != authority._parent_node_material_sha256
        or parent._current_execution._seal_sha256 != authority._parent_current_execution_seal_sha256
        or parent._seal_sha256 != authority._parent_execution_seal_sha256
        or authority._plan_entries != expected_entries
        or child_authorities != authority._child_authorities
        or child_authorities != expected_children
        or any(item.state_head_sha256 != authority._state_head_sha256 for item in child_authorities)
        or not authority._sources
        or any(
            type(item) is not SupervisedTrainingEvidenceSourceAuthority
            or item._issuer is not _SUPERVISED_SOURCE_ISSUER
            or item._training_plan_id != current_plan.training_plan_id
            or item._plan_entry.split != "training"
            for item in authority._sources
        )
    ):
        raise BatchTrainingError("supervised fit evidence authority uses foreign state")
    return current_plan, evidence


def _consume_supervised_fit_evidence_authority(
    authority: object,
) -> tuple[TrainingPlan, MultiImageSupervisedFitEvidence]:
    """Atomically reauthenticate and consume one provider reservation."""

    result = _SUPERVISED_FIT_ISSUER.consume(
        authority,
        lambda: _validate_supervised_fit_evidence_authority(
            authority,
            require_current=True,
        ),
    )
    if (
        type(result) is not tuple
        or len(result) != 2
        or type(result[0]) is not TrainingPlan
        or type(result[1]) is not MultiImageSupervisedFitEvidence
    ):
        raise BatchTrainingError("supervised fit evidence consumption returned invalid evidence")
    return result


def _revoke_supervised_fit_evidence_authority(authority: object) -> bool:
    """Release one exact unused provider reservation, if it is still live."""

    return _SUPERVISED_FIT_ISSUER.revoke(authority)


@dataclass(frozen=True, slots=True)
class _SupervisedHeldOutEvaluationEvidence:
    entries: tuple[SupervisedTrainingEvidenceSnapshot, ...]


@dataclass(frozen=True, slots=True, init=False)
class SupervisedHeldOutEvaluationAuthority:
    """Current-parent-bound validation/test supervised evidence capability."""

    _training_plan_id: str
    _training_plan_set_sha256: str
    _state_head_sha256: str
    _active_plan_sha256: str
    _parent_execution_context_sha256: str
    _parent_node_id: str
    _parent_authorization_sha256: str
    _parent_node_material_sha256: str
    _parent_current_execution_seal_sha256: str
    _parent_execution_seal_sha256: str
    _plan_entries: tuple[TrainingPlanEntry, ...]
    _child_authorities: tuple[BatchCommittedArtifactAuthority, ...]
    _sources: tuple[SupervisedTrainingEvidenceSourceAuthority, ...]
    _evidence: _SupervisedHeldOutEvaluationEvidence
    _seal_sha256: str
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("SupervisedHeldOutEvaluationAuthority is issuer-only")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("SupervisedHeldOutEvaluationAuthority cannot be subclassed")

    def __copy__(self) -> object:
        raise TypeError("SupervisedHeldOutEvaluationAuthority is nonserializable")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("SupervisedHeldOutEvaluationAuthority is nonserializable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("SupervisedHeldOutEvaluationAuthority is nonserializable")

    def reopen(
        self,
        *,
        parent_execution: object,
        expected_context: object,
        state_store: object,
    ) -> _SupervisedHeldOutEvaluationEvidence:
        from ..batch_artifacts import BatchNodeExecutionContext
        from ..batch_state import BatchStateStore
        from .parent_execution import (
            CurrentTrainingParentExecution,
            validate_current_training_parent_execution,
        )

        if (
            type(self) is not SupervisedHeldOutEvaluationAuthority
            or self._issuer is not _SUPERVISED_HELD_OUT_ISSUER
            or type(parent_execution) is not CurrentTrainingParentExecution
            or type(expected_context) is not BatchNodeExecutionContext
            or type(state_store) is not BatchStateStore
        ):
            raise BatchTrainingError("supervised held-out authority issuer is invalid")
        parent = validate_current_training_parent_execution(
            parent_execution,
            expected_context=expected_context,
            state_store=state_store,
        )
        plan_set = parent.training_plan_set
        if (
            parent.classifier_kind is not ClassifierKind.SUPERVISED_PHENOTYPE
            or plan_set.state_head_sha256 != self._state_head_sha256
            or plan_set.active_plan_sha256 != self._active_plan_sha256
            or plan_set.plan_set.training_plan_set_sha256 != self._training_plan_set_sha256
            or parent.execution_context_sha256 != self._parent_execution_context_sha256
            or parent.node_id != self._parent_node_id
            or parent.authorization_sha256 != self._parent_authorization_sha256
            or parent._current_execution.node_material_sha256 != self._parent_node_material_sha256
            or parent._current_execution._seal_sha256 != self._parent_current_execution_seal_sha256
            or parent._seal_sha256 != self._parent_execution_seal_sha256
        ):
            raise BatchTrainingError("supervised held-out authority uses foreign state")
        plan = plan_set.require_plan(self._training_plan_id)
        expected_entries = tuple(item for item in plan.request.entries if item.split != "training")
        held_out_entry_ids = {item.project_entry_id for item in expected_entries}
        expected_children = tuple(
            sorted(
                (
                    item
                    for item in parent.evidence_child_authorities
                    if item.artifact_ref.scope.project_entry_id in held_out_entry_ids
                ),
                key=lambda item: item.artifact_ref.artifact_ref_sha256,
            )
        )
        if self._plan_entries != expected_entries:
            raise BatchTrainingError("supervised held-out plan entries were mutated")
        try:
            child_authorities = validate_committed_artifact_authority_set(
                self._child_authorities,
                expected_active_plan_sha256=self._active_plan_sha256,
            )
        except BatchArtifactError as exc:
            raise BatchTrainingError("supervised held-out child authority is invalid") from exc
        if (
            child_authorities != self._child_authorities
            or child_authorities != expected_children
            or any(item.state_head_sha256 != self._state_head_sha256 for item in child_authorities)
            or any(
                type(item) is not SupervisedTrainingEvidenceSourceAuthority
                or item._issuer is not _SUPERVISED_SOURCE_ISSUER
                for item in self._sources
            )
        ):
            raise BatchTrainingError("supervised held-out source authority is invalid")
        return self._evidence


def _supervised_held_out_seal_material(
    *,
    parent: object,
    plan: TrainingPlan,
    child_authorities: tuple[BatchCommittedArtifactAuthority, ...],
    sources: tuple[SupervisedTrainingEvidenceSourceAuthority, ...],
    evidence: _SupervisedHeldOutEvaluationEvidence,
) -> dict[str, object]:
    return {
        "training_plan_id": plan.training_plan_id,
        "training_plan_set_sha256": (parent.training_plan_set.plan_set.training_plan_set_sha256),
        "state_head_sha256": parent.training_plan_set.state_head_sha256,
        "active_plan_sha256": parent.training_plan_set.active_plan_sha256,
        "parent_execution_context_sha256": parent.execution_context_sha256,
        "parent_node_id": parent.node_id,
        "parent_authorization_sha256": parent.authorization_sha256,
        "parent_node_material_sha256": (parent._current_execution.node_material_sha256),
        "parent_current_execution_seal_sha256": (parent._current_execution._seal_sha256),
        "parent_execution_seal_sha256": parent._seal_sha256,
        "plan_entries": [
            item.to_mapping() for item in plan.request.entries if item.split != "training"
        ],
        "child_authority_seal_sha256s": [item._seal_sha256 for item in child_authorities],
        "source_seal_sha256s": [item._seal_sha256 for item in sources],
        "snapshot_sha256s": [item.snapshot_sha256 for item in evidence.entries],
    }


def _validate_supervised_evidence_children(
    *,
    parent: object,
    plan: TrainingPlan,
    source_snapshots: tuple[
        tuple[
            SupervisedTrainingEvidenceSourceAuthority,
            SupervisedTrainingEvidenceSnapshot,
        ],
        ...,
    ],
) -> tuple[BatchCommittedArtifactAuthority, ...]:
    """Bind decoded supervised sources to exact committed entry children.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    plan_set = parent.training_plan_set
    try:
        authorities = validate_committed_artifact_authority_set(
            parent.evidence_child_authorities,
            expected_active_plan_sha256=plan_set.active_plan_sha256,
        )
    except BatchArtifactError as exc:
        raise BatchTrainingError(
            "supervised partition requires authenticated child authorities"
        ) from exc
    pairs_by_entry: dict[
        str,
        list[
            tuple[
                SupervisedTrainingEvidenceSourceAuthority,
                SupervisedTrainingEvidenceSnapshot,
            ]
        ],
    ] = {}
    for source, snapshot in source_snapshots:
        if snapshot is not source._snapshot:
            raise BatchTrainingError("supervised child snapshot has a foreign source owner")
        pairs_by_entry.setdefault(snapshot.project_entry_id, []).append((source, snapshot))
    authorities_by_entry: dict[str, BatchCommittedArtifactAuthority] = {}
    for authority in authorities:
        ref = authority.artifact_ref
        entry_id = ref.scope.project_entry_id
        pairs = pairs_by_entry.get(entry_id or "")
        if not pairs:
            raise BatchTrainingError("committed supervised evidence child has no decoded snapshots")
        pairs = sorted(
            pairs,
            key=lambda item: _supervised_snapshot_sort_key(item[1]),
        )
        sources = tuple(item[0] for item in pairs)
        snapshots = tuple(item[1] for item in pairs)
        plan_entries = tuple(
            item for item in plan.request.entries if item.project_entry_id == entry_id
        )
        expected_bindings = tuple(
            item
            for item in plan.request.analysis_domain_bindings
            if item.project_entry_id == entry_id
        )
        identity = ref.owning_identity
        source_state_sha256, snapshot_set_sha256 = _supervised_entry_commitments(sources)
        saved_before = {item._saved_state_before_sha256 for item in sources}
        saved_after = {item._saved_state_after_sha256 for item in sources}
        if (
            entry_id is None
            or entry_id in authorities_by_entry
            or authority.state_head_sha256 != plan_set.state_head_sha256
            or authority.producer_node_kind != "supervised_training_evidence_snapshot"
            or ref.artifact_kind != "supervised_training_evidence_snapshot"
            or (ref.schema_name, ref.schema_version) != ("roi-generation-manifest", 1)
            or ref.scope.validation_domain_id != plan.request.validation_domain_id
            or not plan_entries
            or ref.scope.sample_id != plan_entries[0].sample_id
            or ref.scope.slide_id != plan_entries[0].slide_id
            or ref.scope.analysis_domain_bindings != expected_bindings
            or identity.get("training_plan_set_sha256")
            != plan_set.plan_set.training_plan_set_sha256
            or identity.get("project_entry_id") != entry_id
            or identity.get("sample_id") != plan_entries[0].sample_id
            or identity.get("slide_id") != plan_entries[0].slide_id
            or identity.get("entry_snapshot_set_sha256") != snapshot_set_sha256
            or identity.get("object_output_sha256") != snapshot_set_sha256
            or identity.get("object_output_count") != len(snapshots)
            or identity.get("entry_state_sha256") != source_state_sha256
            or identity.get("plan_role_set_sha256")
            != _digest(
                [
                    {
                        "training_plan_id": plan.training_plan_id,
                        "classifier_role": plan.request.classifier_kind.value,
                        "split": item.split,
                    }
                    for item in plan_entries
                ]
            )
            or len(saved_before) != 1
            or len(saved_after) != 1
            or saved_before != saved_after
            or identity.get("saved_state_before_sha256") != next(iter(saved_before))
            or identity.get("saved_state_after_sha256") != next(iter(saved_after))
            or identity.get("post_binding_saved_state_sha256") != next(iter(saved_after))
        ):
            raise BatchTrainingError(
                "committed supervised evidence child differs from its decoded sources"
            )
        authorities_by_entry[entry_id] = authority
    if set(authorities_by_entry) != set(pairs_by_entry):
        raise BatchTrainingError("committed supervised evidence children lack exact entry coverage")
    return authorities


def partition_supervised_training_evidence(
    *,
    parent_execution: object,
    expected_context: object,
    state_store: object,
    sources: tuple[SupervisedTrainingEvidenceSourceAuthority, ...],
    publication_audit_sha256: str | None = None,
) -> tuple[
    MultiImageSupervisedTrainingAggregate,
    SupervisedFitEvidenceAuthority,
    SupervisedHeldOutEvaluationAuthority | None,
]:
    """Partition current all-role supervised evidence before provider dispatch."""

    from ..batch_artifacts import BatchNodeExecutionContext
    from ..batch_state import BatchStateStore
    from .parent_execution import (
        CurrentTrainingParentExecution,
        validate_current_training_parent_execution,
    )

    if (
        type(parent_execution) is not CurrentTrainingParentExecution
        or type(expected_context) is not BatchNodeExecutionContext
        or type(state_store) is not BatchStateStore
    ):
        raise BatchTrainingError("supervised evidence partition requires a current training parent")
    parent = validate_current_training_parent_execution(
        parent_execution,
        expected_context=expected_context,
        state_store=state_store,
    )
    if parent.classifier_kind is not ClassifierKind.SUPERVISED_PHENOTYPE:
        raise BatchTrainingError("supervised evidence parent has a non-supervised classifier kind")
    plan_set = parent.training_plan_set
    source_values = tuple(sources)
    if (
        type(sources) is not tuple
        or not source_values
        or not all(
            type(item) is SupervisedTrainingEvidenceSourceAuthority for item in source_values
        )
    ):
        raise BatchTrainingError("supervised partition requires issuer-authenticated sources")
    training_plan_ids = {item._training_plan_id for item in source_values}
    if len(training_plan_ids) != 1:
        raise BatchTrainingError("supervised evidence sources do not name one training plan")
    plan = plan_set.require_plan(next(iter(training_plan_ids)))
    if any(
        item._training_plan_id != plan.training_plan_id
        or item._training_plan_set_sha256 != plan_set.plan_set.training_plan_set_sha256
        or item._plan != plan
        for item in source_values
    ):
        raise BatchTrainingError(
            "supervised evidence source authority differs from the current plan"
        )
    for entry in plan.request.entries:
        expected_bindings = tuple(
            item
            for item in plan.request.analysis_domain_bindings
            if item.project_entry_id == entry.project_entry_id
        )
        role_sources = tuple(
            item
            for item in source_values
            if (
                item._plan_entry.project_entry_id,
                item._plan_entry.split,
            )
            == (entry.project_entry_id, entry.split)
        )
        actual_bindings = tuple(
            binding for item in role_sources for binding in item._analysis_domain_bindings
        )
        if (
            len(actual_bindings) != len(set(actual_bindings))
            or tuple(sorted(actual_bindings, key=_binding_sort_key)) != expected_bindings
        ):
            raise BatchTrainingError("supervised sources lack exact plan-role coverage of domains")
    source_snapshots = tuple(
        sorted(
            ((item, build_supervised_training_evidence_snapshot(item)) for item in source_values),
            key=lambda item: _supervised_snapshot_sort_key(item[1]),
        )
    )
    values = tuple(item[1] for item in source_snapshots)
    _validate_supervised_plan_coverage(values, plan, include_test=True)
    authenticated_children = _validate_supervised_evidence_children(
        parent=parent,
        plan=plan,
        source_snapshots=source_snapshots,
    )
    aggregate = MultiImageSupervisedTrainingAggregate.create(
        plan=plan,
        entries=values,
        publication_audit_sha256=publication_audit_sha256,
    )
    provider = MultiImageSupervisedFitEvidence.create(
        plan=plan,
        entries=tuple(item for item in values if item.split == "training"),
    )
    training_sources = tuple(
        source for source, snapshot in source_snapshots if snapshot.split == "training"
    )
    training_entry_ids = {
        item.project_entry_id for item in plan.request.entries if item.split == "training"
    }
    training_children = tuple(
        item
        for item in authenticated_children
        if item.artifact_ref.scope.project_entry_id in training_entry_ids
    )
    state_store_identity = id(state_store)
    provider_authority = object.__new__(SupervisedFitEvidenceAuthority)

    def initialize_provider_authority(
        reservation_nonce: str,
        authority_object_identity: int,
    ) -> None:
        provider_seal_material = _supervised_fit_authority_seal_material(
            parent=parent,
            plan=plan,
            child_authorities=training_children,
            sources=training_sources,
            evidence=provider,
            state_store_identity=state_store_identity,
            reservation_nonce=reservation_nonce,
            authority_object_identity=authority_object_identity,
        )
        provider_values = {
            "_training_plan_id": plan.training_plan_id,
            "_training_plan_set_sha256": (plan_set.plan_set.training_plan_set_sha256),
            "_state_head_sha256": plan_set.state_head_sha256,
            "_active_plan_sha256": plan_set.active_plan_sha256,
            "_parent_execution_context_sha256": (parent.execution_context_sha256),
            "_parent_node_id": parent.node_id,
            "_parent_authorization_sha256": parent.authorization_sha256,
            "_parent_node_material_sha256": (parent._current_execution.node_material_sha256),
            "_parent_current_execution_seal_sha256": (parent._current_execution._seal_sha256),
            "_parent_execution_seal_sha256": parent._seal_sha256,
            "_plan_entries": tuple(
                item for item in plan.request.entries if item.split == "training"
            ),
            "_child_authorities": training_children,
            "_sources": training_sources,
            "_evidence": provider,
            "_parent_execution": parent,
            "_expected_context": expected_context,
            "_state_store": state_store,
            "_state_store_identity": state_store_identity,
            "_reservation_nonce": reservation_nonce,
            "_authority_object_identity": authority_object_identity,
            "_seal_sha256": _digest(provider_seal_material),
            "_issuer": _SUPERVISED_FIT_ISSUER,
        }
        for name, value in provider_values.items():
            object.__setattr__(provider_authority, name, value)

    _SUPERVISED_FIT_ISSUER.issue(
        provider_authority,
        initialize_provider_authority,
    )
    if plan.request.evaluation_intent is EvaluationIntent.TRAINING_ONLY:
        return aggregate, provider_authority, None
    held_out_evidence = _SupervisedHeldOutEvaluationEvidence(
        entries=tuple(item for item in values if item.split != "training")
    )
    held_out_sources = tuple(
        source for source, snapshot in source_snapshots if snapshot.split != "training"
    )
    held_out_entry_ids = {item.project_entry_id for item in held_out_evidence.entries}
    held_out_children = tuple(
        item
        for item in authenticated_children
        if item.artifact_ref.scope.project_entry_id in held_out_entry_ids
    )
    seal_material = _supervised_held_out_seal_material(
        parent=parent,
        plan=plan,
        child_authorities=held_out_children,
        sources=held_out_sources,
        evidence=held_out_evidence,
    )
    authority = object.__new__(SupervisedHeldOutEvaluationAuthority)
    values = {
        "_training_plan_id": plan.training_plan_id,
        "_training_plan_set_sha256": plan_set.plan_set.training_plan_set_sha256,
        "_state_head_sha256": plan_set.state_head_sha256,
        "_active_plan_sha256": plan_set.active_plan_sha256,
        "_parent_execution_context_sha256": parent.execution_context_sha256,
        "_parent_node_id": parent.node_id,
        "_parent_authorization_sha256": parent.authorization_sha256,
        "_parent_node_material_sha256": (parent._current_execution.node_material_sha256),
        "_parent_current_execution_seal_sha256": (parent._current_execution._seal_sha256),
        "_parent_execution_seal_sha256": parent._seal_sha256,
        "_plan_entries": tuple(item for item in plan.request.entries if item.split != "training"),
        "_child_authorities": held_out_children,
        "_sources": held_out_sources,
        "_evidence": held_out_evidence,
        "_seal_sha256": _digest(seal_material),
        "_issuer": _SUPERVISED_HELD_OUT_ISSUER,
    }
    for name, value in values.items():
        object.__setattr__(authority, name, value)
    return aggregate, provider_authority, authority


def _mapped_point_to_mapping(value: MappedPointEvidence) -> dict[str, object]:
    return {
        "point_id": value.point_id,
        "cell_id": value.cell_id,
        "slide_id": value.slide_id,
        "section_id": value.section_id,
        "x_level0": value.x_level0,
        "y_level0": value.y_level0,
        "cell_x_level0": value.cell_x_level0,
        "cell_y_level0": value.cell_y_level0,
        "distance_level0": value.distance_level0,
        "class_id": value.class_id,
        "evidence_set": value.evidence_set,
        "spatial_region_id": value.spatial_region_id,
    }


def _mapped_point_from_mapping(value: object) -> MappedPointEvidence:
    fields = frozenset(MappedPointEvidence.__dataclass_fields__)
    mapping = _closed_mapping(value, fields=fields, field="mapped_point_evidence")
    try:
        return MappedPointEvidence(
            **{name: mapping[name] for name in fields}  # type: ignore[arg-type]
        )
    except (TypeError, ValueError) as exc:
        raise BatchTrainingError("mapped point evidence is invalid") from exc


def _supervised_snapshot_sort_key(
    value: SupervisedTrainingEvidenceSnapshot,
) -> tuple[object, ...]:
    return (
        _SPLIT_RANK[value.split],
        value.project_entry_id,
        value.section_id,
        value.measurement_generation_id,
        value.snapshot_sha256,
    )


def _supervised_fit_snapshot_sort_key(
    value: SupervisedFitEvidenceSnapshot,
) -> tuple[object, ...]:
    return (
        value.project_entry_id,
        value.section_id,
        value.measurement_generation_id,
        value.snapshot_sha256,
    )


def _validate_supervised_plan_coverage(
    snapshots: tuple[SupervisedTrainingEvidenceSnapshot, ...],
    plan: TrainingPlan,
    *,
    include_test: bool,
) -> None:
    """Validate supervised plan coverage for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    if plan.request.classifier_kind.value != "supervised_phenotype":
        raise BatchTrainingError("supervised evidence requires a supervised plan")
    allowed = _SPLITS if include_test else ("training", "validation")
    expected = {
        (item.project_entry_id, item.split): item
        for item in plan.request.entries
        if item.split in allowed
    }
    actual_roles: dict[tuple[str, str], list[SupervisedTrainingEvidenceSnapshot]] = {}
    for snapshot in snapshots:
        if snapshot.split not in allowed:
            raise BatchTrainingError("supervised provider evidence contains a test snapshot")
        actual_roles.setdefault((snapshot.project_entry_id, snapshot.split), []).append(snapshot)
    if set(actual_roles) != set(expected):
        raise BatchTrainingError("supervised snapshots do not provide exact plan-role coverage")
    for role, values in actual_roles.items():
        entry = expected[role]
        section_keys = {(item.section_id, item.measurement_generation_id) for item in values}
        expected_binding_sha256s = tuple(
            sorted(
                _digest(item.to_mapping())
                for item in plan.request.analysis_domain_bindings
                if item.project_entry_id == entry.project_entry_id
            )
        )
        actual_binding_sha256s = tuple(
            sorted(
                sha256 for snapshot in values for sha256 in snapshot.analysis_domain_binding_sha256s
            )
        )
        if len(section_keys) != len(values):
            raise BatchTrainingError("supervised snapshots duplicate one section generation")
        if actual_binding_sha256s != expected_binding_sha256s:
            raise BatchTrainingError("supervised snapshots lack exact plan-role binding coverage")
        for snapshot in values:
            if (
                snapshot.sample_id,
                snapshot.slide_id,
                snapshot.subject_id,
                snapshot.image_identity_sha256,
                snapshot.class_ids,
                tuple(item.to_mapping() for item in snapshot.feature_schema),
            ) != (
                entry.sample_id,
                entry.slide_id,
                entry.subject_id,
                entry.image_identity_sha256,
                plan.request.required_classes,
                tuple((plan.request.feature_requirements or {}).get("feature_schema", ())),
            ):
                raise BatchTrainingError("supervised snapshot identity differs from its plan role")


def _validate_supervised_fit_plan(
    evidence: MultiImageSupervisedFitEvidence,
    plan: TrainingPlan,
) -> None:
    """Validate supervised fit plan for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    if plan.request.classifier_kind is not ClassifierKind.SUPERVISED_PHENOTYPE:
        raise BatchTrainingError("supervised fit evidence requires a supervised plan")
    expected = {
        (item.project_entry_id, item.split): item
        for item in plan.request.entries
        if item.split == "training"
    }
    actual: dict[tuple[str, str], list[SupervisedFitEvidenceSnapshot]] = {}
    for snapshot in evidence.entries:
        actual.setdefault((snapshot.project_entry_id, snapshot.split), []).append(snapshot)
    if set(actual) != set(expected):
        raise BatchTrainingError(
            "supervised fit snapshots do not provide exact training-role coverage"
        )
    expected_features = tuple((plan.request.feature_requirements or {}).get("feature_schema", ()))
    for role, snapshots in actual.items():
        entry = expected[role]
        binding_sha256s = tuple(
            sorted(
                _digest(item.to_mapping())
                for item in plan.request.analysis_domain_bindings
                if item.project_entry_id == entry.project_entry_id
            )
        )
        actual_binding_sha256s = tuple(
            sorted(
                sha256
                for snapshot in snapshots
                for sha256 in snapshot.analysis_domain_binding_sha256s
            )
        )
        if (
            len({(item.section_id, item.measurement_generation_id) for item in snapshots})
            != len(snapshots)
            or actual_binding_sha256s != binding_sha256s
            or any(
                (
                    snapshot.sample_id,
                    snapshot.slide_id,
                    snapshot.subject_id,
                    snapshot.image_identity_sha256,
                    snapshot.class_ids,
                    tuple(item.to_mapping() for item in snapshot.feature_schema),
                )
                != (
                    entry.sample_id,
                    entry.slide_id,
                    entry.subject_id,
                    entry.image_identity_sha256,
                    plan.request.required_classes,
                    expected_features,
                )
                for snapshot in snapshots
            )
        ):
            raise BatchTrainingError("supervised fit snapshot identity differs from its plan role")
    if evidence.training_projection_sha256 != _supervised_training_projection_sha256(plan):
        raise BatchTrainingError(
            "supervised fit evidence differs from the plan's training projection"
        )


def _validate_supervised_outer_splits(
    fit: tuple[SupervisedTrainingEvidenceSnapshot, ...],
    holdout: tuple[SupervisedTrainingEvidenceSnapshot, ...],
) -> str:
    """Validate supervised outer splits for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    projected: list[MappedPointEvidence] = []
    for evidence_set, snapshots in (("training", fit), ("holdout", holdout)):
        for snapshot in snapshots:
            for item in snapshot.mapped_evidence:
                prefix = snapshot.project_entry_id + "\x1f"
                projected.append(
                    MappedPointEvidence(
                        point_id=prefix + item.point_id,
                        cell_id=prefix + item.cell_id,
                        slide_id=prefix + item.slide_id,
                        section_id=item.section_id,
                        x_level0=item.x_level0,
                        y_level0=item.y_level0,
                        cell_x_level0=item.cell_x_level0,
                        cell_y_level0=item.cell_y_level0,
                        distance_level0=item.distance_level0,
                        class_id=item.class_id,
                        evidence_set=evidence_set,
                        spatial_region_id=prefix + item.spatial_region_id,
                    )
                )
    try:
        validated = validate_training_holdout(projected)
    except SupervisedPhenotypingError as exc:
        raise BatchTrainingError("supervised evidence has entry-scoped split leakage") from exc
    return _digest(
        {
            "training_cell_ids": list(validated.training_cell_ids),
            "holdout_cell_ids": list(validated.holdout_cell_ids),
            "training_region_ids": list(validated.training_region_ids),
            "holdout_region_ids": list(validated.holdout_region_ids),
            "evidence": [_mapped_point_to_mapping(item) for item in validated.evidence],
        }
    )


@dataclass(frozen=True, slots=True)
class EvidenceValidation:
    status: EvidenceStatus
    changed_evidence_sha256s: tuple[str, ...]
    reason: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.status, EvidenceStatus):
            raise BatchTrainingError("status must be an EvidenceStatus")
        changes = _sha256_tuple(
            self.changed_evidence_sha256s,
            "changed_evidence_sha256s",
            nonempty=self.status is EvidenceStatus.STALE,
        )
        reason = None if self.reason is None else _text(self.reason, "reason")
        if self.status is EvidenceStatus.CURRENT and (changes or reason is not None):
            raise BatchTrainingError("current evidence cannot carry changes or a reason")
        if self.status is EvidenceStatus.STALE and reason is None:
            raise BatchTrainingError("stale evidence requires a reason")
        object.__setattr__(self, "changed_evidence_sha256s", changes)
        object.__setattr__(self, "reason", reason)


def validate_training_evidence(
    accepted: MultiImageTrainingEvidence,
    current: MultiImageTrainingEvidence,
) -> EvidenceValidation:
    if not isinstance(accepted, MultiImageTrainingEvidence) or not isinstance(
        current, MultiImageTrainingEvidence
    ):
        raise BatchTrainingError("accepted and current must be MultiImageTrainingEvidence values")
    if accepted.material_evidence_sha256 == current.material_evidence_sha256:
        return EvidenceValidation(EvidenceStatus.CURRENT, (), None)

    changes: set[str] = set()
    accepted_states = {(item.project_entry_id, item.split): item for item in accepted.entry_states}
    current_states = {(item.project_entry_id, item.split): item for item in current.entry_states}
    for role in set(accepted_states) | set(current_states):
        before = accepted_states.get(role)
        after = current_states.get(role)
        if before != after:
            if before is not None:
                changes.add(before.entry_state_sha256)
            if after is not None:
                changes.add(after.entry_state_sha256)

    def rows_by_identity(evidence: MultiImageTrainingEvidence):
        return {
            (item.project_entry_id, item.split, item.annotation_uuid): item
            for item in evidence.rows
        }

    accepted_rows = rows_by_identity(accepted)
    current_rows = rows_by_identity(current)
    for identity in set(accepted_rows) | set(current_rows):
        before = accepted_rows.get(identity)
        after = current_rows.get(identity)
        if before != after:
            if before is not None:
                changes.add(before.evidence_sha256)
            if after is not None:
                changes.add(after.evidence_sha256)
    if not changes:
        changes.update((accepted.material_evidence_sha256, current.material_evidence_sha256))
    if accepted.training_plan_id != current.training_plan_id:
        reason = "training plan changed"
    elif accepted.normalized_manifest_sha256 != current.normalized_manifest_sha256:
        reason = "normalized manifest changed"
    elif accepted.rows != current.rows:
        reason = "training evidence rows changed"
    else:
        reason = "training evidence entry state changed"
    return EvidenceValidation(EvidenceStatus.STALE, tuple(sorted(changes)), reason)


def _evidence_row_binding_identity(
    row: TrainingEvidenceSnapshot,
) -> tuple[str, str, str, str]:
    return (
        row.project_entry_id,
        row.analysis_domain_id,
        row.analysis_domain_revision_sha256,
        row.domain_set_sha256,
    )


def _evidence_row_sort_key(
    row: TrainingEvidenceSnapshot,
) -> tuple[object, ...]:
    return (
        row.project_entry_id,
        _SPLIT_RANK[row.split],
        row.annotation_uuid,
        row.evidence_sha256,
    )


def _evidence_state_sort_key(
    state: TrainingEvidenceEntryState,
) -> tuple[object, ...]:
    return state.project_entry_id, _SPLIT_RANK[state.split], state.entry_state_sha256


def _training_evidence_row(value: object) -> TrainingEvidenceSnapshot:
    try:
        return TrainingEvidenceSnapshot.from_mapping(value)
    except ClassifierCompatibilityError as exc:
        raise BatchTrainingError("training evidence contains an invalid owner row") from exc


def _validate_evidence_plan(
    evidence: MultiImageTrainingEvidence,
    plan: "TrainingPlan",
) -> None:
    """Validate evidence plan for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    if not isinstance(plan, TrainingPlan):
        raise BatchTrainingError("plan must be a TrainingPlan")
    if evidence.training_plan_id != plan.training_plan_id:
        raise BatchTrainingError("training_plan_id differs from the plan")
    if evidence.normalized_manifest_sha256 != plan.normalized_manifest_sha256:
        raise BatchTrainingError("normalized_manifest_sha256 differs from the plan")
    plan_entries = {(item.project_entry_id, item.split): item for item in plan.request.entries}
    states = {(item.project_entry_id, item.split): item for item in evidence.entry_states}
    if set(states) != set(plan_entries):
        raise BatchTrainingError("entry states do not provide exact plan role coverage")
    bindings_by_entry: dict[str, list[AnalysisDomainBinding]] = {}
    for binding in plan.request.analysis_domain_bindings:
        bindings_by_entry.setdefault(binding.project_entry_id, []).append(binding)
    for role, entry in plan_entries.items():
        state = states[role]
        if (state.sample_id, state.slide_id, state.subject_id) != (
            entry.sample_id,
            entry.slide_id,
            entry.subject_id,
        ):
            raise BatchTrainingError("entry state identity differs from its plan role")
        if state.image_identity_sha256 != entry.image_identity_sha256:
            raise BatchTrainingError("entry state image identity differs from its plan role")
        binding_mappings = [
            item.to_mapping()
            for item in sorted(
                bindings_by_entry.get(entry.project_entry_id, []),
                key=_binding_sort_key,
            )
        ]
        expected_binding_sha256s = tuple(sorted(_digest(item) for item in binding_mappings))
        if (
            state.analysis_domain_binding_sha256s != expected_binding_sha256s
            or state.analysis_domain_binding_set_sha256 != _digest(binding_mappings)
        ):
            raise BatchTrainingError(
                "entry state does not provide exact analysis-domain binding coverage"
            )
    expected_row_bindings = {
        _binding_row_identity(binding)
        for bindings in bindings_by_entry.values()
        for binding in bindings
    }
    _validate_pixel_cross_split_leakage(evidence.rows)
    for row in evidence.rows:
        if _evidence_row_binding_identity(row) not in expected_row_bindings:
            raise BatchTrainingError(
                "row has no exact planned analysis-domain binding contribution"
            )


def _validate_pixel_cross_split_leakage(
    rows: tuple[TrainingEvidenceSnapshot, ...],
) -> None:
    """Reject entry-local geometry or stable-cell material reused by two roles."""

    observed: dict[tuple[str, str, str], str] = {}
    for row in rows:
        identities = (
            ("annotation_uuid", row.annotation_uuid),
            ("normalized_roi_sha256", row.normalized_roi_sha256),
            *(("cell_id", item) for item in row.cell_ids),
            *(("cell_evidence_sha256", item) for item in row.cell_evidence_sha256s),
        )
        for kind, identity in identities:
            key = (row.project_entry_id, kind, identity)
            prior_split = observed.setdefault(key, row.split)
            if prior_split != row.split:
                raise BatchTrainingError("pixel evidence has entry-scoped split leakage")


def _validate_pixel_fit_plan(
    evidence: MultiImagePixelFitEvidence,
    plan: TrainingPlan,
) -> None:
    """Validate pixel fit plan for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    if not isinstance(plan, TrainingPlan):
        raise BatchTrainingError("plan must be a TrainingPlan")
    if evidence.training_projection_sha256 != _pixel_training_projection_sha256(plan):
        raise BatchTrainingError(
            "training_projection_sha256 differs from the plan training projection"
        )
    expected = {
        (item.project_entry_id, item.split): item
        for item in plan.request.entries
        if item.split == "training"
    }
    actual = {(item.project_entry_id, item.split): item for item in evidence.entry_states}
    if set(actual) != set(expected):
        raise BatchTrainingError("pixel fit entry states do not provide exact plan role coverage")
    for role, entry in expected.items():
        state = actual[role]
        if (
            state.sample_id,
            state.slide_id,
            state.subject_id,
            state.image_identity_sha256,
        ) != (
            entry.sample_id,
            entry.slide_id,
            entry.subject_id,
            entry.image_identity_sha256,
        ):
            raise BatchTrainingError("pixel fit entry state differs from its plan role")
