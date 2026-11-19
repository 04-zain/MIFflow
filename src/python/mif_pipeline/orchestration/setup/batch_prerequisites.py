"""Immutable, plan-bound batch prerequisite observations and owner composition.

The `BatchPrerequisiteItem` record carries `prerequisite_id`, `prerequisite_kind`, `state`,
`scope`, `sample_id` and its remaining declared fields.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from collections.abc import Mapping
from typing import Literal
from mif_pipeline.pipeline_core import ROIAnalysisError

from mif_pipeline.io.bioformats_prerequisite import BioFormatsRuntimeInventory, probe_bioformats_prerequisite
from mif_pipeline.qupath_runtime.prerequisite_probe import QuPathPrerequisiteProbeRequest, QuPathProbeFinding, probe_qupath_prerequisites
from mif_pipeline.qupath_runtime.warpy_probe import WarpyPrerequisiteRequest, probe_warpy_prerequisites

BatchPrerequisiteKind = Literal[
    "java_runtime",
    "bioformats_runtime",
    "segmentation_method",
    "instanseg_extension",
    "instanseg_model",
    "cellpose_model",
    "stardist_model",
    "classifier_artifact",
    "warpy_extension",
]
BatchPrerequisiteState = Literal["ready", "missing", "incompatible", "error"]
BatchPrerequisiteScope = Literal["batch", "sample", "validation_domain"]

_KINDS = (
    "java_runtime",
    "bioformats_runtime",
    "segmentation_method",
    "instanseg_extension",
    "instanseg_model",
    "cellpose_model",
    "stardist_model",
    "classifier_artifact",
    "warpy_extension",
)
_STATES = frozenset(("ready", "missing", "incompatible", "error"))
_SCOPES = frozenset(("batch", "sample", "validation_domain"))
_FIELDS = frozenset(
    (
        "prerequisite_id",
        "prerequisite_kind",
        "state",
        "scope",
        "sample_id",
        "validation_domain_id",
        "required_by_node_kinds",
        "owner",
        "summary",
        "detail",
        "external_locator",
    )
)


# ==============================================================================
#                              Batch prerequisites
# ==============================================================================


@dataclass(frozen=True, slots=True)
class BatchPrerequisiteOwnerInputs:
    """Caller supplied owner inputs; no caller supplied readiness decisions."""

    bioformats_inventory: BioFormatsRuntimeInventory | None
    qupath_requests: Mapping[
        tuple[Path | None, str | None, bool],
        QuPathPrerequisiteProbeRequest | WarpyPrerequisiteRequest,
    ]

    def __post_init__(self) -> None:
        from types import MappingProxyType

        object.__setattr__(self, "qupath_requests", MappingProxyType(dict(self.qupath_requests)))


def _finding_row(
    kind: BatchPrerequisiteKind,
    scope: BatchPrerequisiteScope,
    identity: str,
    required: set[str],
    owner: str,
    finding: QuPathProbeFinding,
    locator: str | None = None,
) -> BatchPrerequisiteItem:
    detail = finding.diagnostic
    if finding.status == "ready":
        detail = (
            f"{detail}; version {finding.observed_version or 'unreported'}; "
            f"evidence SHA-256 {finding.evidence_sha256}"
        )
    return BatchPrerequisiteItem(
        prerequisite_id=f"{kind}:{scope}:{identity}",
        prerequisite_kind=kind,
        state=finding.status,
        scope=scope,
        sample_id=identity if scope == "sample" else None,
        validation_domain_id=identity if scope == "validation_domain" else None,
        required_by_node_kinds=tuple(sorted(required)),
        owner=owner,
        summary=kind.replace("_", " "),
        detail=detail,
        external_locator=locator,
    )


def _dependent(*findings: QuPathProbeFinding) -> QuPathProbeFinding:
    """Keep the first underlying non-ready owner diagnostic."""
    return next((item for item in findings if item.status != "ready"), findings[-1])


def collect_batch_prerequisites(
    *,
    manifest: _batch_manifest.ValidatedBatchManifest,
    plan: _batch_dag.BatchPlan,
    owner_inputs: BatchPrerequisiteOwnerInputs,
    required_node_kinds: frozenset[str] | None = None,
) -> tuple[BatchPrerequisiteItem, ...]:
    """Probe active and forecast requirements, optionally for a bounded stage set.

    Filtering precedes all owner probes. A targeted recovery therefore checks
    its declared prerequisites without probing unrelated models or tools.
    """
    from ..batch_dag import BatchPlanError, _validate_plan_instance
    from ..batch_manifest import ValidatedBatchManifest

    if not isinstance(manifest, ValidatedBatchManifest):
        raise TypeError("manifest must be ValidatedBatchManifest")
    _validate_plan_instance(plan, "plan")
    if plan.normalized_manifest_sha256 != manifest.normalized_manifest_sha256:
        raise BatchPlanError("plan and manifest identity disagree")
    if not isinstance(owner_inputs, BatchPrerequisiteOwnerInputs):
        raise TypeError("owner_inputs must be BatchPrerequisiteOwnerInputs")
    participants: dict[str, set[str]] = {}
    for node in plan.nodes:
        participants.setdefault(node.kind, set()).update(node.participant_ids)
    for node in plan.deferred_nodes:
        participants.setdefault(node.node_kind, set()).update(node.participant_ids)
    if required_node_kinds is not None:
        if (type(required_node_kinds) is not frozenset or not required_node_kinds
                or not required_node_kinds <= participants.keys()):
            raise ValueError("selected prerequisites require current plan node kinds")
        participants = {kind: samples for kind, samples in participants.items()
                        if kind in required_node_kinds}
    enabled = {sample.sample_id: sample for sample in manifest.enabled_samples}
    rows: list[BatchPrerequisiteItem] = []

    io_kinds = set(participants) & {"inspection", "reference_unmixing"}
    if io_kinds:
        io = probe_bioformats_prerequisite(owner_inputs.bioformats_inventory)
        io_state = io.status
        if io.status == "ready" and (
            owner_inputs.bioformats_inventory is None or io.evidence is None
        ):
            io_state = "missing" if owner_inputs.bioformats_inventory is None else "error"
        locator = (
            str(owner_inputs.bioformats_inventory.payload_root)
            if owner_inputs.bioformats_inventory is not None
            else None
        )
        for kind in ("java_runtime", "bioformats_runtime"):
            detail = io.detail
            if io_state == "ready" and io.evidence is not None:
                detail = (
                    f"{detail}; Java {io.evidence.java_version}; "
                    f"Bio-Formats {io.evidence.bioformats_runtime_version}; "
                    f"inventory SHA-256 {io.evidence.inventory_sha256}"
                )
            rows.append(
                BatchPrerequisiteItem(
                    prerequisite_id=f"{kind}:batch:{plan.batch_id}",
                    prerequisite_kind=kind,
                    state=io_state,
                    scope="batch",
                    sample_id=None,
                    validation_domain_id=None,
                    required_by_node_kinds=tuple(sorted(io_kinds)),
                    owner="io",
                    summary=kind.replace("_", " "),
                    detail=detail,
                    external_locator=locator,
                )
            )

    model_samples = participants.get("segmentation", set()) & enabled.keys()
    from ..batch_settled_review_revision import requires_paired_alignment
    warpy_samples = {
        sid
        for sid in participants.get("registration", set()) & enabled.keys()
        if requires_paired_alignment(enabled[sid], plan)
    }
    request_groups: dict[tuple[Path | None, str | None, bool], set[str]] = {}
    from ..batch_segmentation_settings import current_segmentation_settings
    unselected_samples = set()
    for sid in model_samples:
        for model in current_segmentation_settings(manifest, plan, sid):
            if model is None:
                unselected_samples.add(sid)
                continue
            if model is not None and model.method != "instanseg":
                from .alternative_segmentation_setup import inspect_detector, detector_model_evidence
                status = inspect_detector(model.method, model.model_path)
                if status.state == "installed":
                    try:
                        evidence = detector_model_evidence(model)
                        finding = QuPathProbeFinding("ready", status.detail, model.model_version,
                                                     evidence["evidence_sha256"])
                    except ValueError as error:
                        finding = QuPathProbeFinding("incompatible", str(error))
                else:
                    finding = QuPathProbeFinding(status.state, status.detail)
                rows.append(_finding_row(model.method + "_model", "sample", sid, {"segmentation"},
                    "qupath_runtime", finding, str(model.model_path)))
                continue
            key = ((None, None, False) if model is None else (model.model_path, model.model_version, False))
            request_groups.setdefault(key, set()).add(sid)
    if unselected_samples:
        from .alternative_segmentation_setup import default_detector_directory, inspect_detector
        from .instanseg_setup import default_instanseg_model_target
        from mif_pipeline.qupath_runtime.payload_lock import MODEL_NAME, verify_instanseg_model_directory
        import sys

        available = []
        details = []
        root = (Path(sys._MEIPASS) / "qupath-assets" if getattr(sys, "frozen", False)
                else default_instanseg_model_target("win-x86_64" if sys.platform == "win32" else "linux-x86_64"))
        model_check = verify_instanseg_model_directory(root / "model" / MODEL_NAME)
        if model_check.status == "verified":
            available.append("InstanSeg")
        else:
            details.append("InstanSeg: " + model_check.diagnostic)
        for method, label in (("cellpose", "Cellpose-SAM"), ("stardist", "StarDist")):
            status = inspect_detector(method, default_detector_directory(method))
            if status.state == "installed":
                available.append(label)
            else:
                details.append(label + ": " + status.detail)
        summary = ("Installed: " + ", ".join(available) + "." if available
                   else "Install at least one segmentation model in Software setup.")
        rows.append(BatchPrerequisiteItem(
            prerequisite_id=f"segmentation_method:batch:{plan.batch_id}",
            prerequisite_kind="segmentation_method", state="ready" if available else "missing",
            scope="batch", sample_id=None, validation_domain_id=None,
            required_by_node_kinds=("segmentation",), owner="segmentation_setup",
            summary="Available segmentation models",
            detail=summary + " Choose the method during segmentation setup. Runtime and processor requirements depend on the selected method."
                + ("\n" + "\n".join(details) if details else ""),
            external_locator=None,
        ))
    if warpy_samples:
        request_groups[(None, None, True)] = warpy_samples
    findings: dict[tuple[Path | None, str | None, bool], object] = {}
    for key in sorted(request_groups, key=lambda item: (str(item[0]), item[1] or "", item[2])):
        request = owner_inputs.qupath_requests.get(key)
        if request is None:
            findings[key] = None
            continue
        if (request.model_root, request.model_version, request.require_warpy) != key:
            raise ValueError("QuPath request differs from exact model requirement")
        findings[key] = (
            probe_warpy_prerequisites(request)
            if type(request) is WarpyPrerequisiteRequest
            else probe_qupath_prerequisites(request)
        )
    missing = QuPathProbeFinding(
        "missing", "No reviewed QuPath request for exact model and registration requirement"
    )
    model_findings: dict[str, list[tuple[QuPathProbeFinding, str]]] = {}
    extension_findings: dict[str | None, list[QuPathProbeFinding]] = {}
    warpy_findings: dict[str | None, list[QuPathProbeFinding]] = {}
    for key in sorted(request_groups, key=lambda item: (str(item[0]), item[1] or "", item[2])):
        sample_ids = request_groups[key]
        result = findings[key]
        request = owner_inputs.qupath_requests.get(key)
        if result is None:
            extension = model = warpy = missing
        else:
            extension = _dependent(result.qupath, result.instanseg, result.cpu_engine)
            model = _dependent(
                result.qupath, result.instanseg, result.cpu_engine, result.model or missing
            )
            warpy = _dependent(result.qupath, result.warpy or missing)
        if not key[2] and sample_ids & model_samples:
            locator = str(request.instanseg_jar.path) if request is not None else None
            extension_findings.setdefault(locator, []).append(extension)
            for sid in sorted(sample_ids & model_samples) if key[0] is not None else ():
                model_findings.setdefault(sid, []).append((model, str(key[0])))
        if key[2] and sample_ids & warpy_samples:
            locator = (
                str(request.warpy_jar.path)
                if request is not None and request.warpy_jar is not None
                else None
            )
            warpy_findings.setdefault(locator, []).append(warpy)
    for sid, selected in sorted(model_findings.items()):
        rows.append(_finding_row("instanseg_model", "sample", sid, {"segmentation"},
            "qupath_runtime", _dependent(*(finding for finding, _path in selected)),
            "; ".join(path for _finding, path in selected)))
    for kind, required, grouped in (
        ("instanseg_extension", "segmentation", extension_findings),
        ("warpy_extension", "registration", warpy_findings),
    ):
        for locator, observed in sorted(grouped.items(), key=lambda item: item[0] or ""):
            identity = (
                f"{plan.batch_id}:{sha256(locator.encode()).hexdigest()}"
                if locator is not None
                else f"{plan.batch_id}:unconfigured"
            )
            rows.append(
                _finding_row(
                    kind,
                    "batch",
                    identity,
                    {required},
                    "qupath_runtime",
                    _dependent(*observed),
                    locator,
                )
            )

    refs = {
        item.artifact_ref_sha256: item
        for item in plan.external_inputs
        if item.artifact_kind == "classifier_record_set"
    }
    for ref_sha, ref in sorted(refs.items()):
        consumer_kind = {"primary_compartment": "primary_classifier_inference",
                         "secondary_structure": "secondary_classifier_inference"}.get(
                             ref.owning_identity.get("classifier_kind"))
        consumers = {
            node.kind
            for node in plan.nodes
            if node.kind == consumer_kind
            and any(
                edge.accepted_artifact_ref_sha256 == ref_sha
                and edge.consumer_node_id == node.node_id
                for edge in plan.edges
            )
        }
        ref_samples = {item.sample_id for item in ref.configuration_bindings}
        if any(
            node.node_kind == consumer_kind
            and ref_samples.intersection(node.participant_ids)
            for node in plan.deferred_nodes
        ):
            consumers.add(consumer_kind)
        if required_node_kinds is not None:
            consumers.intersection_update(required_node_kinds)
        if not consumers:
            continue
        from ..batch_training.accepted_model_reuse import validate_imported_classifier_ref

        try:
            validate_imported_classifier_ref(manifest, ref)
            state = "ready"
            detail = "Accepted saved classifier export and exact model bytes are available."
        except (OSError, ValueError, ROIAnalysisError) as exc:
            state = "incompatible"
            detail = str(exc)
        domain = ref.scope.validation_domain_id
        if domain is None:
            raise BatchPlanError("reusable classifier lacks validation domain scope")
        rows.append(
            BatchPrerequisiteItem(
                prerequisite_id=f"classifier_artifact:validation_domain:{domain}:{ref_sha}",
                prerequisite_kind="classifier_artifact",
                state=state,
                scope="validation_domain",
                sample_id=None,
                validation_domain_id=domain,
                required_by_node_kinds=tuple(sorted(consumers)),
                owner="batch_training",
                summary="classifier artifact",
                detail=detail,
                external_locator=str(ref.locator) if ref.locator is not None else None,
            )
        )
    return tuple(sorted(rows, key=lambda row: row.prerequisite_id))


def registered_batch_prerequisite_kinds() -> tuple[str, ...]:
    return _KINDS


def _nonblank(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be nonempty text")
    return value


@dataclass(frozen=True, slots=True)
class BatchPrerequisiteItem:
    prerequisite_id: str
    prerequisite_kind: BatchPrerequisiteKind
    state: BatchPrerequisiteState
    scope: BatchPrerequisiteScope
    sample_id: str | None
    validation_domain_id: str | None
    required_by_node_kinds: tuple[str, ...]
    owner: str
    summary: str
    detail: str
    external_locator: str | None

    def __post_init__(self) -> None:
        _nonblank(self.prerequisite_id, "prerequisite_id")
        if self.prerequisite_kind not in _KINDS:
            raise ValueError("unsupported prerequisite_kind")
        if self.state not in _STATES:
            raise ValueError("unsupported prerequisite state")
        if self.scope not in _SCOPES:
            raise ValueError("unsupported prerequisite scope")
        if self.scope == "batch":
            if self.sample_id is not None or self.validation_domain_id is not None:
                raise ValueError("batch prerequisite cannot carry narrower IDs")
        elif self.scope == "sample":
            _nonblank(self.sample_id, "sample_id")
            if self.validation_domain_id is not None:
                raise ValueError("sample prerequisite cannot carry domain ID")
        else:
            _nonblank(self.validation_domain_id, "validation_domain_id")
            if self.sample_id is not None:
                raise ValueError("domain prerequisite cannot carry sample ID")
        if (
            type(self.required_by_node_kinds) is not tuple
            or not self.required_by_node_kinds
            or any(
                not isinstance(kind, str) or not kind.strip()
                for kind in self.required_by_node_kinds
            )
            or self.required_by_node_kinds != tuple(sorted(set(self.required_by_node_kinds)))
        ):
            raise ValueError("required_by_node_kinds must be sorted, unique, and nonempty")
        _nonblank(self.owner, "owner")
        _nonblank(self.summary, "summary")
        _nonblank(self.detail, "detail")
        if self.external_locator is not None:
            _nonblank(self.external_locator, "external_locator")

    def to_mapping(self) -> dict[str, object]:
        return {
            "prerequisite_id": self.prerequisite_id,
            "prerequisite_kind": self.prerequisite_kind,
            "state": self.state,
            "scope": self.scope,
            "sample_id": self.sample_id,
            "validation_domain_id": self.validation_domain_id,
            "required_by_node_kinds": list(self.required_by_node_kinds),
            "owner": self.owner,
            "summary": self.summary,
            "detail": self.detail,
            "external_locator": self.external_locator,
        }

    @classmethod
    def from_mapping(cls, mapping: object) -> BatchPrerequisiteItem:
        from collections.abc import Mapping

        if not isinstance(mapping, Mapping) or set(mapping) != _FIELDS:
            raise ValueError("prerequisite mapping must have exact closed fields")
        kinds = mapping["required_by_node_kinds"]
        if not isinstance(kinds, list):
            raise ValueError("required_by_node_kinds must be an array")
        return cls(
            prerequisite_id=mapping["prerequisite_id"],
            prerequisite_kind=mapping["prerequisite_kind"],
            state=mapping["state"],
            scope=mapping["scope"],
            sample_id=mapping["sample_id"],
            validation_domain_id=mapping["validation_domain_id"],
            required_by_node_kinds=tuple(kinds),
            owner=mapping["owner"],
            summary=mapping["summary"],
            detail=mapping["detail"],
            external_locator=mapping["external_locator"],
        )


def bind_batch_prerequisites(
    plan: _batch_dag.BatchPlan, prerequisites: tuple[BatchPrerequisiteItem, ...]
) -> _batch_dag.BatchPlan:
    """Return a new plan whose digest includes sorted owner observation rows."""
    from dataclasses import replace
    from ..batch_dag import BatchPlan, BatchPlanError, _digest, _validate_plan_instance

    _validate_plan_instance(plan, "plan")
    if type(prerequisites) is not tuple or not all(
        isinstance(row, BatchPrerequisiteItem) for row in prerequisites
    ):
        raise BatchPlanError("prerequisites must be typed immutable rows")
    rows = tuple(sorted(prerequisites, key=lambda row: row.prerequisite_id))
    if len({row.prerequisite_id for row in rows}) != len(rows):
        raise BatchPlanError("duplicate prerequisite ID")
    present = {node.kind for node in plan.nodes} | {node.node_kind for node in plan.deferred_nodes}
    if any(not set(row.required_by_node_kinds) <= present for row in rows):
        raise BatchPlanError("prerequisite references foreign node kind")
    unsigned = plan._unsigned()
    unsigned["prerequisites"] = [row.to_mapping() for row in rows]
    return replace(plan, prerequisites=rows, plan_sha256=_digest(unsigned))


# Resolve the public annotation after BatchPrerequisiteItem has been defined,
# so importing batch_dag from either direction remains safe.
from .. import batch_dag as _batch_dag
from .. import batch_manifest as _batch_manifest


def equivalent_extension_location(
    observed: BatchPrerequisiteItem, retained: BatchPrerequisiteItem
) -> bool:
    """Recognize a location-only change after fresh native readiness checks.

    Require both original and selected local files to contain the same bytes.
    This does not admit a changed extension, a missing original, or another kind
    of prerequisite. No retained result or source file is rewritten.
    """
    import os
    import stat

    kinds = {"instanseg_extension", "warpy_extension"}
    fields = (
        "prerequisite_kind",
        "scope",
        "sample_id",
        "validation_domain_id",
        "required_by_node_kinds",
        "owner",
    )
    if (
        observed.state != "ready"
        or retained.state != "ready"
        or observed.prerequisite_kind not in kinds
        or any(getattr(observed, key) != getattr(retained, key) for key in fields)
        or observed.scope != "batch"
    ):
        return False

    def fingerprint(row):
        path = Path(row.external_locator or "")
        if not path.is_absolute() or path.is_symlink():
            raise ValueError("Extension must be a regular local file")
        prefix, separator, location_hash = row.prerequisite_id.rpartition(":")
        if not separator or location_hash != sha256(str(path).encode()).hexdigest():
            raise ValueError("The retained requirement does not bind this location")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= 128 * 1024 * 1024:
                raise ValueError("Extension is not a bounded regular file")
            digest = sha256()
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
            identity = lambda value: (
                value.st_dev,
                value.st_ino,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )
            if identity(before) != identity(after) or identity(after) != identity(path.stat()):
                raise ValueError("Extension changed during verification")
        return prefix, before.st_size, digest.hexdigest()

    try:
        return fingerprint(observed) == fingerprint(retained)
    except (OSError, ValueError):
        return False
