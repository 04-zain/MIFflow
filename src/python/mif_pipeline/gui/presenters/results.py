"""Presentation-only projection of authoritative final batch results.

The `ResultsView` record carries `output_table`, `domain_summary_table`, `provenance_table`,
`spatial_handoff_table`, `subject_table` and its remaining declared fields.

The presenter converts the supplied typed results into display rows and action values. The
current stage or batch service remains responsible for validating the response and
publishing the accepted scientific operation."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from mif_pipeline.gui.interactions import FormTable, FormTableRow
from mif_pipeline.gui.presenters.run import (
    BatchRunView,
    SUMMARY_COLUMNS,
    build_batch_run_view,
)
from mif_pipeline.orchestration.batch_artifacts import (
    BatchArtifactRef,
)
from mif_pipeline.orchestration.batch_service_models import BatchResultsSnapshot
from mif_pipeline.orchestration.batch_phenotyping import (
    final_non_spatial_export_result_identity_contract,
    spatial_cell_handoff_identity_contract,
)

_FINAL_CONTRACT = final_non_spatial_export_result_identity_contract()
_SPATIAL_CONTRACT = spatial_cell_handoff_identity_contract()
FINAL_EXPORT_KIND = _FINAL_CONTRACT.artifact_kind
FINAL_EXPORT_SCHEMA = (_FINAL_CONTRACT.schema_name, _FINAL_CONTRACT.schema_version)
SPATIAL_HANDOFF_KIND = _SPATIAL_CONTRACT.artifact_kind
SPATIAL_HANDOFF_SCHEMA = (_SPATIAL_CONTRACT.schema_name, _SPATIAL_CONTRACT.schema_version)
DOMAIN_STATUS_COLUMNS = ("validation_domain_id", "status", "sample_ids")

OUTPUT_COLUMNS = (
    "sample_id",
    "processed_image_path",
    "qupath_project_entry",
    "final_non_spatial_export_id",
    "artifact_kind",
    "schema_name",
    "schema_version",
    "path",
    "sha256",
    "byte_count",
    "resolved_config_sha256",
    "artifact_ref_sha256",
)
PROVENANCE_COLUMNS = (
    "sample_id",
    "resolved_config_hash",
    "provenance_hash",
)
SPATIAL_HANDOFF_COLUMNS = (
    "sample_id",
    "artifact_id",
    "artifact_kind",
    "schema_name",
    "schema_version",
    "path",
    "sha256",
    "byte_count",
    "resolved_config_sha256",
    "artifact_ref_sha256",
)
SUBJECT_COLUMNS = ("sample_id", "subject_id")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


# ==============================================================================
#                                    Results
# ==============================================================================


@dataclass(frozen=True, slots=True)
class ResultsView:
    """Finite final-result evidence rendered by the Results page."""

    output_table: FormTable
    domain_summary_table: FormTable
    provenance_table: FormTable
    spatial_handoff_table: FormTable
    subject_table: FormTable
    handoff_ready: bool

    def __post_init__(self) -> None:
        if not all(
            type(table) is FormTable
            for table in (
                self.output_table,
                self.domain_summary_table,
                self.provenance_table,
                self.spatial_handoff_table,
                self.subject_table,
            )
        ):
            raise TypeError("results view tables must be FormTable values")
        if type(self.handoff_ready) is not bool:
            raise TypeError("results handoff readiness must be a bool")


def _summary_values(row: FormTableRow) -> dict[str, object]:
    if len(row.values) != len(SUMMARY_COLUMNS):
        raise ValueError("result summary row width does not match its schema")
    return dict(zip(SUMMARY_COLUMNS, row.values, strict=True))


def _sha256(value: object, *, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a canonical lowercase SHA-256")
    return value


def _required_text(value: object, *, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _safe_output_path(value: object, *, label: str) -> str:
    text = _required_text(value, label=label)
    path = Path(text)
    if (
        not path.is_absolute()
        or path == Path(path.anchor)
        or ".." in path.parts
        or any(ord(character) < 32 for character in text)
    ):
        raise ValueError(f"{label} must be a safe absolute non-root path")
    return text


def _validate_completed_summary(summary: dict[str, object]) -> None:
    if summary["status"] != "completed":
        return
    sample_id = str(summary["sample_id"])
    _safe_output_path(
        summary["processed_image_path"],
        label=f"sample {sample_id} prepared OME-Zarr path",
    )
    _required_text(
        summary["qupath_project_entry"],
        label=f"sample {sample_id} QuPath project entry",
    )
    for field in (
        "node_status_inventory_sha256",
        "domain_status_inventory_sha256",
        "resolved_config_hash",
        "provenance_hash",
    ):
        _sha256(summary[field], label=f"sample {sample_id} {field}")
    if summary["pending_interaction_reason"] is not None or summary["failure_reason"] is not None:
        raise ValueError(f"completed sample {sample_id} cannot retain pending or failure reason")


def _validated_artifacts(
    values: object,
    *,
    label: str,
    expected_kind: str,
    expected_schema: tuple[str, int],
    summaries_by_sample: dict[str, dict[str, object]],
    domain_by_sample: dict[str, str],
) -> tuple[BatchArtifactRef, ...]:
    if type(values) is not tuple:
        raise TypeError(f"{label} must be an immutable BatchArtifactRef tuple")
    artifacts: list[BatchArtifactRef] = []
    for artifact in values:
        if type(artifact) is not BatchArtifactRef:
            raise TypeError(f"{label} must contain only BatchArtifactRef values")
        if artifact.artifact_kind != expected_kind:
            raise ValueError(f"{label} artifact kind is invalid")
        if (artifact.schema_name, artifact.schema_version) != expected_schema:
            raise ValueError(f"{label} artifact schema is invalid")
        if artifact.locator is None or artifact.sha256 is None or artifact.byte_count is None:
            raise ValueError(f"{label} must be materialized with byte identity")
        if not artifact.locator.is_absolute():
            raise ValueError(f"{label} locator must be absolute")
        sample_id = artifact.scope.sample_id
        if sample_id is None or sample_id not in summaries_by_sample:
            raise ValueError(f"{label} artifact has an unknown sample scope")
        domain_id = artifact.scope.validation_domain_id
        if domain_id != domain_by_sample[sample_id]:
            raise ValueError(f"{label} artifact does not use its sample's validation domain")
        summary = summaries_by_sample[sample_id]
        if artifact.scope.slide_id != summary["slide_id"]:
            raise ValueError(f"{label} artifact slide scope differs from the result")
        if artifact.scope.project_entry_id != summary["qupath_project_entry"]:
            raise ValueError(f"{label} artifact project entry differs from the result")
        if expected_kind == FINAL_EXPORT_KIND and artifact.sha256 != summary["provenance_hash"]:
            raise ValueError("final export provenance hash differs from the result")
        if len(artifact.configuration_bindings) != 1:
            raise ValueError(f"{label} requires one sample configuration binding")
        binding = artifact.configuration_bindings[0]
        if (
            binding.sample_id != sample_id
            or binding.resolved_config_sha256 != summary["resolved_config_hash"]
        ):
            raise ValueError(f"{label} resolved configuration differs from the result")
        artifacts.append(artifact)
    return tuple(artifacts)


def _by_sample(
    artifacts: tuple[BatchArtifactRef, ...],
    *,
    label: str,
) -> dict[str, BatchArtifactRef]:
    result: dict[str, BatchArtifactRef] = {}
    for artifact in artifacts:
        sample_id = artifact.scope.sample_id
        assert sample_id is not None
        if sample_id in result:
            raise ValueError(f"{label} has a duplicate sample scope")
        result[sample_id] = artifact
    return result


def _reject_duplicate_identities(
    artifacts: tuple[BatchArtifactRef, ...],
) -> None:
    artifact_ids = tuple(artifact.artifact_id for artifact in artifacts)
    if len(set(artifact_ids)) != len(artifact_ids):
        raise ValueError("result inventory contains a duplicate artifact ID")
    locators = tuple(artifact.locator for artifact in artifacts)
    if len(set(locators)) != len(locators):
        raise ValueError("result inventory contains a duplicate artifact locator")


def _artifact_values(artifact: BatchArtifactRef) -> tuple[object, ...]:
    assert artifact.scope.sample_id is not None
    assert artifact.locator is not None
    assert artifact.sha256 is not None
    assert artifact.byte_count is not None
    return (
        artifact.scope.sample_id,
        artifact.artifact_id,
        artifact.artifact_kind,
        artifact.schema_name,
        artifact.schema_version,
        str(artifact.locator),
        artifact.sha256,
        artifact.byte_count,
        artifact.configuration_bindings[0].resolved_config_sha256,
        artifact.artifact_ref_sha256,
    )


def _completed_run(
    run_view: BatchRunView,
    summaries: tuple[dict[str, object], ...],
) -> bool:
    return (
        run_view.resume_disposition == "completed"
        and run_view.completed_count == len(summaries)
        and run_view.interaction_count == 0
        and run_view.failure_count == 0
        and run_view.active_operation is None
        and all(
            summary["status"] == "completed"
            and summary["progress_kind"] == "terminal"
            and summary["current_or_final_stage"] == "export"
            and summary["pending_interaction_reason"] is None
            and summary["failure_reason"] is None
            for summary in summaries
        )
    )


def build_results_view(
    snapshot: BatchResultsSnapshot,
) -> ResultsView:
    """Display completed publication evidence without reading artifact bytes."""

    if type(snapshot) is not BatchResultsSnapshot:
        raise TypeError("results projection requires a BatchResultsSnapshot")
    run_view = build_batch_run_view(snapshot.status, active_operation=None, log_lines=())

    summaries = tuple(_summary_values(row) for row in run_view.summary_table.rows)
    summaries = tuple(sorted(summaries, key=lambda item: str(item["sample_id"])))
    sample_ids = tuple(summary["sample_id"] for summary in summaries)
    if not all(type(sample_id) is str and sample_id.strip() for sample_id in sample_ids):
        raise ValueError("result sample IDs must be non-empty text")
    if len(set(sample_ids)) != len(sample_ids):
        raise ValueError("result sample IDs must be unique")
    summaries_by_sample = {str(summary["sample_id"]): summary for summary in summaries}
    for summary in summaries:
        _validate_completed_summary(summary)
    known_samples = set(str(sample_id) for sample_id in sample_ids)
    domain_by_sample = {
        sample_id: domain_id
        for domain_id, sample_ids in snapshot.sample_ids_by_validation_domain.items()
        for sample_id in sample_ids
    }

    final_artifacts = _validated_artifacts(
        snapshot.final_non_spatial_exports,
        label="final exports",
        expected_kind=FINAL_EXPORT_KIND,
        expected_schema=FINAL_EXPORT_SCHEMA,
        summaries_by_sample=summaries_by_sample,
        domain_by_sample=domain_by_sample,
    )
    handoff_artifacts = _validated_artifacts(
        snapshot.spatial_handoffs,
        label="spatial handoffs",
        expected_kind=SPATIAL_HANDOFF_KIND,
        expected_schema=SPATIAL_HANDOFF_SCHEMA,
        summaries_by_sample=summaries_by_sample,
        domain_by_sample=domain_by_sample,
    )
    _reject_duplicate_identities(final_artifacts + handoff_artifacts)
    final_by_sample = _by_sample(final_artifacts, label="final exports")
    handoff_by_sample = _by_sample(handoff_artifacts, label="spatial handoffs")
    for sample_id in set(final_by_sample) & set(handoff_by_sample):
        if (
            final_by_sample[sample_id].scope.validation_domain_id
            != handoff_by_sample[sample_id].scope.validation_domain_id
        ):
            raise ValueError("final export and spatial handoff must use the same validation domain")

    output_rows: list[FormTableRow] = []
    provenance_rows: list[FormTableRow] = []
    subject_rows: list[FormTableRow] = []
    for summary in summaries:
        sample_id = summary["sample_id"]
        assert isinstance(sample_id, str)
        final_values = _artifact_values(final_by_sample[sample_id])[1:]
        output_rows.append(
            FormTableRow(
                f"sample:{sample_id}",
                (
                    sample_id,
                    summary["processed_image_path"],
                    summary["qupath_project_entry"],
                    *final_values,
                ),
            )
        )
        provenance_rows.append(
            FormTableRow(
                f"provenance:{sample_id}",
                (
                    sample_id,
                    summary["resolved_config_hash"],
                    summary["provenance_hash"],
                ),
            )
        )
        subject_rows.append(
            FormTableRow(
                f"subject:{sample_id}",
                (sample_id, summary["subject_id"]),
            )
        )

    handoff_ready = (
        bool(known_samples)
        and set(final_by_sample) == known_samples
        and set(handoff_by_sample) == known_samples
        and _completed_run(run_view, summaries)
    )
    handoff_rows = tuple(
        FormTableRow(
            f"handoff:{sample_id}",
            _artifact_values(handoff_by_sample[sample_id]),
        )
        for sample_id in sorted(handoff_by_sample)
    )

    return ResultsView(
        output_table=FormTable("outputs", OUTPUT_COLUMNS, tuple(output_rows)),
        domain_summary_table=FormTable(
            "domain_summaries",
            DOMAIN_STATUS_COLUMNS,
            tuple(
                FormTableRow(f"domain:{domain_id}", (domain_id, "completed", sample_ids))
                for domain_id, sample_ids in snapshot.sample_ids_by_validation_domain.items()
            ),
        ),
        provenance_table=FormTable("provenance", PROVENANCE_COLUMNS, tuple(provenance_rows)),
        spatial_handoff_table=FormTable("spatial_handoffs", SPATIAL_HANDOFF_COLUMNS, handoff_rows),
        subject_table=FormTable("subjects", SUBJECT_COLUMNS, tuple(subject_rows)),
        handoff_ready=handoff_ready,
    )


__all__ = [
    "FINAL_EXPORT_KIND",
    "FINAL_EXPORT_SCHEMA",
    "OUTPUT_COLUMNS",
    "PROVENANCE_COLUMNS",
    "ResultsView",
    "SPATIAL_HANDOFF_COLUMNS",
    "SPATIAL_HANDOFF_KIND",
    "SPATIAL_HANDOFF_SCHEMA",
    "SUBJECT_COLUMNS",
    "build_results_view",
]
