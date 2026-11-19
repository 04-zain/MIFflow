"""Scientific composition for accepted marker gates on one physical section.

The operation retains the selected stable-cell population, measurement generation and
phenotype view. Its records and stage adapters connect accepted numerical results to the
current batch target and reviewed application scope."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations


from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
import json
from pathlib import Path
from math import isfinite

from mif_pipeline.cell_analysis import (
    AcceptedThreshold,
    CompositePhenotypeRule,
    MeasurementSemantic,
    SectionThresholdOverride,
    ThresholdRule,
    apply_ordered_composites,
    apply_threshold_rules,
)
from mif_pipeline.cell_analysis.marker_rules import MarkerCellValues
from mif_pipeline.cell_analysis.thresholds import (
    SlideThresholdOverride, ThresholdServiceError, resolve_accepted_threshold,
    threshold_intensity_domain,
)
from mif_pipeline.cell_analysis.nimbus import load_nimbus_scores
from mif_pipeline.cell_analysis.measurement_plan import FeatureKind
from mif_pipeline.cell_analysis.measurement_rows import (
    MarkerIntensityRowsDescriptor,
    MeasurementRowStreamExpectation,
    validate_measurement_row_stream,
)
from mif_pipeline.orchestration.batch_artifacts import (
    ArtifactOrigin,
    BatchCommittedArtifactAuthority,
    validate_committed_artifact_authority_set,
)
from mif_pipeline.orchestration.batch_manifest import ResolvedBatchSample
from mif_pipeline.orchestration.batch_measurement_artifacts import (
    decode_committed_measurements_result,
)
from mif_pipeline.orchestration.batch_phenotyping_mode_artifacts import (
    decode_committed_phenotyping_mode,
)
from mif_pipeline.orchestration.phenotype_views import PhenotypeViewStore
from mif_pipeline.pipeline_core import AnalysisConfig
from mif_pipeline.pipeline_core.config import PhenotypingMode
from mif_pipeline.orchestration.workflow import CurrentImageScientificInvocation

from mif_pipeline.cell_analysis.phenotyping_population import resolve_population_selection, population_includes, NOT_PHENOTYPED
from ._common import BatchPhenotypingError, canonical_bytes, digest, sha256
from .marker_application_records import (
    MarkerRuleSectionApplication,
    ValidatedMarkerGateProjection,
)
from .marker_execution import (
    marker_gate_domain_identity_contract,
)
from .marker_settings import marker_feature_candidates, configured_marker_catalog, validate_marker_settings, marker_population_from_modes
from .marker_combinations import decode_marker_combination

# ==============================================================================
#                          Marker application execution
# ==============================================================================


def _threshold(value: Mapping[str, object]) -> AcceptedThreshold:
    """Decode the complete current accepted-decision record without defaults."""
    def current_fields(record, record_type):
        if not isinstance(record, Mapping) or set(record) != {item.name for item in fields(record_type)}:
            raise ValueError("accepted marker decision fields differ from the current schema")
        return record


    try:
        current_fields(value, AcceptedThreshold)
        overrides = tuple(
            SectionThresholdOverride(**current_fields(item, SectionThresholdOverride))
            for item in value["section_overrides"]
        )
        provenance = value["score_provenance"]
        if type(provenance) is not str:
            raise ValueError("score provenance must be canonical JSON text")
        parsed_provenance = json.loads(provenance)
        if not isinstance(parsed_provenance, Mapping) or canonical_bytes(parsed_provenance).decode("utf-8") != provenance:
            raise ValueError("score provenance must be a canonical JSON object")
        return AcceptedThreshold(
            marker_id=value["marker_id"],
            validation_domain=value["validation_domain"],
            threshold=value["threshold"],
            outcome=value["outcome"],
            fit_scope=value["fit_scope"],
            **{field: value[field] for field in (
                "proposed_threshold", "proposed_outcome", "proposed_score_cutoff", "score_cutoff", "adjustment_source")},
            score_provenance=parsed_provenance,
            slide_thresholds=tuple(SlideThresholdOverride(**current_fields(item, SlideThresholdOverride)) for item in value["slide_thresholds"]),
            selection_source=value["selection_source"],
            reviewer_id=value["reviewer_id"],
            review_record_id=value["review_record_id"],
            section_overrides=overrides,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("committed marker threshold is invalid") from exc


def decode_committed_marker_gate(
    authority: BatchCommittedArtifactAuthority,
) -> ValidatedMarkerGateProjection:
    """Decode only the exact locatorless C1.3 gate owner."""

    if type(authority) is not BatchCommittedArtifactAuthority:
        raise TypeError("marker application requires committed gate authority")
    validate_committed_artifact_authority_set(
        (authority,), expected_active_plan_sha256=authority.active_plan_sha256
    )
    ref = authority.artifact_ref
    identity = ref.owning_identity
    contract = marker_gate_domain_identity_contract()
    if (
        authority.producer_node_kind != "marker_gate_review"
        or ref.artifact_kind != "marker_gate"
        or ref.schema_name != "mif-batch-marker-gate-domain-authority"
        or ref.schema_version != 6
        or ref.identity_contract != contract
        or ref.owning_identity_contract_sha256 != contract.contract_sha256
        or ref.origin is not ArtifactOrigin.INTERNAL_NODE
        or ref.locator is not None
        or ref.sha256 is not None
        or ref.byte_count is not None
        or identity.get("source_kind") != "accepted_interaction"
    ):
        raise BatchPhenotypingError("committed marker gate owner is invalid")
    thresholds = tuple(_threshold(item) for item in identity["accepted_thresholds"])
    markers = tuple(identity["canonical_marker_ids"])
    if tuple(item.marker_id for item in thresholds) != markers:
        raise BatchPhenotypingError("committed marker gate marker cover differs")
    try:
        settings_json = identity["marker_settings"]
        if type(settings_json) is not str:
            raise ValueError("settings are not canonical JSON")
        settings = json.loads(settings_json)
        if (
            canonical_bytes(settings).decode("utf-8") != settings_json
            or digest(settings) != identity["marker_settings_sha256"]
        ):
            raise ValueError("settings digest differs")
        lineage_json = identity["setup_lineage"]
        if lineage_json is None:
            setup_lineage = None
        elif type(lineage_json) is str:
            setup_lineage = json.loads(lineage_json)
            if canonical_bytes(setup_lineage).decode("utf-8") != lineage_json:
                raise ValueError("setup lineage is not canonical")
        else:
            raise ValueError("setup lineage is invalid")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BatchPhenotypingError("committed marker settings are invalid") from exc
    return ValidatedMarkerGateProjection(
        artifact_ref=ref,
        validation_domain_id=str(identity["validation_domain_id"]),
        canonical_marker_ids=markers,
        marker_settings=settings,
        marker_settings_sha256=str(identity["marker_settings_sha256"]),
        setup_lineage=setup_lineage,
        measurement_authority_bindings=tuple(identity["measurement_authority_bindings"]),
        accepted_thresholds=thresholds,
        evidence_sha256=str(identity["evidence_sha256"]),
        domain_gate_sha256=str(identity["domain_gate_sha256"]),
    )


def _section_identity(sample_id: str, entry_id: str, slide_id: str, section_id: str) -> str:
    return canonical_bytes((sample_id, entry_id, slide_id, section_id)).decode("utf-8")


def _composites(
    definition: Mapping[str, object], markers: tuple[str, ...]
) -> tuple[tuple[CompositePhenotypeRule, ...], str, frozenset[str]]:
    """Map the canonical definition's stable IDs to accepted display names."""

    try:
        classes = {
            str(item["class_id"]): str(item["display_name"])
            for item in definition["phenotype_classes"]
        }
        rules = tuple(
            CompositePhenotypeRule(
                classes[str(raw["phenotype_id"])],
                {marker: dict(raw["marker_states"]).get(marker, "ignore") for marker in markers},
                precedence,
            )
            for precedence, raw in enumerate(definition["composite_rules"])
        )
        residual = classes[str(definition["residual_phenotype_id"])]
    except (KeyError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("canonical marker phenotype definition is invalid") from exc
    return rules, residual, frozenset(classes.values())


def build_marker_rule_section_application(
    *,
    gate_authority: BatchCommittedArtifactAuthority,
    combination_authority: BatchCommittedArtifactAuthority,
    measurement_authority: BatchCommittedArtifactAuthority,
    mode_authorities: tuple[BatchCommittedArtifactAuthority, ...],
    resolved_sample: ResolvedBatchSample,
    authenticated_output_root: Path,
    phenotype_view_store: PhenotypeViewStore,
    source_run_id: str,
    expected_saved_state_before_sha256: str | None = None,
    expected_population_source_ref_sha256s: frozenset[str] | None = None,
    completed_evidence=None,
) -> MarkerRuleSectionApplication:
    """Reopen current rows, apply accepted thresholds/composites, and prepare a view."""

    authorities = validate_committed_artifact_authority_set(
        (gate_authority, combination_authority, measurement_authority, *mode_authorities),
        expected_active_plan_sha256=measurement_authority.active_plan_sha256,
    )
    if len({item.state_head_sha256 for item in authorities}) != 1:
        raise BatchPhenotypingError("marker application sources are not from one state head")
    gate = decode_committed_marker_gate(gate_authority)
    phenotype_definition = decode_marker_combination(combination_authority, gate)
    measurement = decode_committed_measurements_result(measurement_authority)
    modes = tuple(decode_committed_phenotyping_mode(item) for item in mode_authorities)
    resolved_config_sha256 = resolved_sample.resolved_config_sha256
    configuration_bindings = measurement.artifact_ref.configuration_bindings
    if (
        resolved_sample.sample_id != measurement.sample_id
        or resolved_sample.validation_domain_id != gate.validation_domain_id
        or len(configuration_bindings) != 1
        or configuration_bindings[0].sample_id != resolved_sample.sample_id
        or configuration_bindings[0].resolved_config_sha256 != resolved_config_sha256
        or set(item.analysis_domain_binding for item in modes)
        != set(measurement.analysis_domain_bindings)
        or any(
            item.selected_mode is not PhenotypingMode.MARKER_RULES
            or item.resolved_config_sha256 != resolved_config_sha256
            or item.measurements_result_ref_sha256 != measurement.artifact_ref.artifact_ref_sha256
            for item in modes
        )
    ):
        raise BatchPhenotypingError("marker application mode or domain cover differs")
    expected_binding = next(
        (
            item
            for item in gate.measurement_authority_bindings
            if item.get("measurements_result_ref_sha256")
            == measurement.artifact_ref.artifact_ref_sha256
        ),
        None,
    )
    if (
        expected_binding is None
        or expected_binding.get("resolved_config_sha256") != resolved_config_sha256
        or tuple(expected_binding["analysis_domain_bindings"])
        != tuple(item.to_mapping() for item in measurement.analysis_domain_bindings)
    ):
        raise BatchPhenotypingError("marker gate does not cover this exact section")
    if completed_evidence is not None:
        # The failed-attempt reopener has authenticated these exact native bytes
        # and their unchanged inputs. Reuse the prepared result without fitting
        # or applying thresholds again.
        request = completed_evidence.reopen_validation_inputs().scientific_request_body
        prepared = phenotype_view_store.reopen_recorded(request["phenotype_view_artifact"])
        metadata = prepared.payload
        source_refs = tuple(sorted(item.artifact_ref.artifact_ref_sha256 for item in authorities))
        expected_metadata = {
            "mode": "marker_rules", "slide_id": measurement.slide_id,
            "canonical_image_sha256": measurement.canonical_image_sha256,
            "tissue_generation_id": measurement.tissue_generation_id,
            "section_id": measurement.section_id,
            "source_run_id": completed_evidence.authorized.attempt_id,
            "cell_count": measurement.cell_count,
            "cell_ids_sha256": measurement.cell_ids_sha256,
            "cell_to_domain_membership_sha256": measurement.cell_to_domain_membership_sha256,
        }
        if (any(metadata.get(key) != value for key, value in expected_metadata.items())
                or tuple(metadata["lineage"]["source_ref_sha256s"]) != source_refs
                or tuple(metadata["evidence"]["source_ref_sha256s"]) != source_refs
                or metadata["evidence"]["domain_gate_sha256"] != gate.domain_gate_sha256
                or metadata["evidence"]["evidence_sha256"] != gate.evidence_sha256
                or request["phenotype_assignment_source"]["sha256"] != metadata["assignments_sha256"]):
            raise BatchPhenotypingError("The completed marker result differs from its current inputs")
        return MarkerRuleSectionApplication(
            sample_id=measurement.sample_id, project_entry_id=measurement.project_entry_id,
            slide_id=measurement.slide_id, canonical_image_id=measurement.canonical_image_id,
            canonical_image_sha256=measurement.canonical_image_sha256,
            section_id=measurement.section_id, tissue_generation_id=measurement.tissue_generation_id,
            segmentation_population_id=measurement.segmentation_population_id,
            measurement_generation_id=measurement.measurement_generation_id,
            validation_domain_id=gate.validation_domain_id,
            phenotype_generation_id=metadata["phenotype_generation_id"],
            cell_ids_sha256=measurement.cell_ids_sha256,
            cell_to_domain_membership_sha256=measurement.cell_to_domain_membership_sha256,
            source_ref_sha256s=source_refs,
            expected_saved_state_before_sha256=completed_evidence.authorized.saved_state_before_sha256,
            prepared=prepared, view_store=phenotype_view_store,
        )
    # A revised gate retains measurements while the saved project advances.
    # Bind its mutation to the current target already issued by the scheduler.
    saved_before = sha256(expected_saved_state_before_sha256, "expected_saved_state_before_sha256")
    marker_ids, offered = marker_feature_candidates((measurement,))
    marker_ids, offered = configured_marker_catalog(gate.marker_settings,
        available_marker_ids=marker_ids, offered_marker_features=offered)
    if marker_ids != gate.canonical_marker_ids:
        raise BatchPhenotypingError("marker gate processed marker cover differs")
    settings = validate_marker_settings(
        gate.marker_settings,
        canonical_marker_ids=marker_ids,
        offered_marker_features=offered,
    )
    if digest(settings) != gate.marker_settings_sha256:
        raise BatchPhenotypingError("marker gate accepted settings digest differs")
    definition_sha256 = digest(phenotype_definition)
    from mif_pipeline.cell_analysis.phenotyping_population import resolve_population_selection
    accepted_population = marker_population_from_modes(modes)
    scope = dict(sample_id=measurement.sample_id, project_entry_id=measurement.project_entry_id, slide_id=measurement.slide_id)
    if resolve_population_selection(settings["phenotyping_population"], **scope) != resolve_population_selection(accepted_population, **scope):
        raise BatchPhenotypingError("The accepted marker gates use a different cell selection from this phenotyping method.")
    selected_rules = {item["marker_id"]: item for item in settings["marker_rules"]}
    offered_features = {(item["marker_id"], item["feature_id"]): item for item in offered}
    semantics = []
    threshold_rules = []
    direct_score_rules = []
    applied_thresholds = []
    section_key = _section_identity(
        measurement.sample_id,
        measurement.project_entry_id,
        measurement.slide_id,
        measurement.section_id,
    )
    image_key = canonical_bytes((measurement.sample_id, measurement.project_entry_id,
                                 measurement.slide_id)).decode("utf-8")
    marker_features = tuple(
        item
        for item in measurement.measurement_plan.features
        if item.kind == FeatureKind.EXPRESSION.value
    )
    cells = []
    included_ids = set()
    selection = resolve_population_selection(settings.get("phenotyping_population"), sample_id=measurement.sample_id,
        project_entry_id=measurement.project_entry_id, slide_id=measurement.slide_id)
    if selection["kind"] != "all_segmented" and expected_population_source_ref_sha256s is None:
        raise BatchPhenotypingError("restricted phenotyping requires current accepted region-source authority")

    def retain(cell_id: str, domain_id: str, values: tuple[float, ...], membership) -> None:
        if population_includes(selection, membership):
            included_ids.add(cell_id)
        cells.append(
            MarkerCellValues(
                cell_id,
                measurement.slide_id,
                measurement.section_id,
                domain_id,
                dict(zip((item.measurement for item in marker_features), values, strict=True)),
                {marker: None for marker in gate.canonical_marker_ids},
            )
        )

    stream = validate_measurement_row_stream(
        descriptor=MarkerIntensityRowsDescriptor(
            path=measurement.measurement_rows.path,
            content_schema_version=measurement.measurement_rows.content_schema_version,
            sha256=measurement.measurement_rows.sha256,
            byte_count=measurement.measurement_rows.byte_count,
            row_count=measurement.measurement_rows.row_count,
        ),
        authenticated_output_root=authenticated_output_root,
        expected=MeasurementRowStreamExpectation(
            project_entry_id=measurement.project_entry_id,
            slide_id=measurement.slide_id,
            section_id=measurement.section_id,
            segmentation_generation_id=measurement.measurement_generation_id,
            measurement_generation_id=measurement.measurement_generation_id,
            segmentation_population_id=measurement.segmentation_population_id,
            analysis_domain_bindings=tuple(
                {
                    "analysis_domain_id": item.analysis_domain_id,
                    "analysis_domain_revision_sha256": item.analysis_domain_revision_sha256,
                }
                for item in measurement.analysis_domain_bindings
            ),
            feature_ids=tuple(item.feature_id for item in marker_features),
            measurement_names=tuple(item.measurement for item in marker_features),
        ),
        population_selection=selection,
        expected_population_source_ref_sha256s=expected_population_source_ref_sha256s if selection["kind"] != "all_segmented" else None,
        provisional_population_callback=retain,
    )
    if stream.cell_ids_sha256 != measurement.cell_ids_sha256:
        raise BatchPhenotypingError("marker application cell inventory differs")
    for accepted in gate.accepted_thresholds:
        if not included_ids:
            continue
        raw = selected_rules[accepted.marker_id]
        feature = offered_features[(accepted.marker_id, raw["feature_id"])]
        try:
            selected, selection_scope = resolve_accepted_threshold(
                accepted, image_id=image_key, section_id=section_key)
        except ThresholdServiceError as error:
            raise BatchPhenotypingError(str(error)) from error
        cutoff, outcome = selected.threshold, selected.outcome
        score_cutoff = getattr(selected, "score_cutoff", None)
        intensity_domain = threshold_intensity_domain(selected)
        applied_thresholds.append(
            {
                "marker_id": accepted.marker_id,
                "threshold": cutoff,
                "outcome": outcome,
                "selection_scope": selection_scope,
                "intensity_domain": intensity_domain,
            }
        )
        semantics.append(
            MeasurementSemantic(
                channel_id=feature["channel_id"],
                measurement=feature["measurement"],
                compartment=feature["compartment"],
                biomarker=accepted.marker_id,
            )
        )
        if outcome == "direct_scores":
            direct_score_rules.append((accepted, raw, score_cutoff))
            continue
        threshold_rules.append(
            ThresholdRule(
                biomarker=accepted.marker_id,
                measurement=feature["measurement"],
                compartment=feature["compartment"],
                polarity=raw["polarity"],
                cutoff=cutoff,
                confirmed=True,
                outcome=outcome,
                intensity_domain=intensity_domain,
            )
        )
    thresholded = apply_threshold_rules(
        tuple(cells), tuple(threshold_rules), measurement_semantics=tuple(semantics)
    )
    for accepted, raw, score_cutoff in direct_score_rules:
        provenance = accepted.score_provenance
        scope = (measurement.sample_id, measurement.project_entry_id, measurement.slide_id, measurement.section_id)
        scores = load_nimbus_scores(provenance["path"],
            expected_identities=tuple((*scope, cell.cell_id) for cell in thresholded),
            expected_population_by_scope={scope: measurement.segmentation_population_id},
            expected_image_by_scope={scope: measurement.canonical_image_id}, marker=accepted.marker_id,
            allow_superset=True, expected_sha256=provenance["sha256"])
        cutoff = float(provenance["cutoff"] if score_cutoff is None else score_cutoff)
        thresholded = tuple(replace(cell, marker_states={**cell.marker_states,
            accepted.marker_id: None if not isfinite(score) else bool(score >= cutoff)})
            for cell, score in zip(thresholded, scores.scores, strict=True))
    thresholded = tuple(cell if cell.cell_id in included_ids else replace(cell,
        marker_states={marker: None for marker in gate.canonical_marker_ids}) for cell in thresholded)
    composites, residual, display_names = _composites(
        phenotype_definition, gate.canonical_marker_ids
    )
    result = apply_ordered_composites(
        thresholded,
        composites,
        residual=residual,
        accepted_thresholds=gate.accepted_thresholds,
        selected_display_view="marker_rules",
    )
    import_rows = tuple(dict(row) if row["cell_id"] in included_ids else {
        **dict(row), "primary_phenotype": NOT_PHENOTYPED, "phenotype_components": [],
        "marker_states": {marker: "unknown" for marker in gate.canonical_marker_ids}}
        for row in result.import_rows)
    preview_id = result.preview_id if len(included_ids) == len(cells) else digest({
        "preview_id": result.preview_id, "included_cell_ids": sorted(included_ids),
        "phenotyping_population": settings.get("phenotyping_population")})
    if {str(row["primary_phenotype"]) for row in import_rows} - display_names - {NOT_PHENOTYPED}:
        raise BatchPhenotypingError(
            "marker application emitted an undeclared phenotype display name"
        )
    assignments = tuple(
        {
            "cell_id": row["cell_id"],
            "analysis_domain_id": row["analysis_domain_id"],
            "primary_phenotype": row["primary_phenotype"],
            "phenotype_source": row["phenotype_source"],
            "phenotype_components": list(row["phenotype_components"]),
            "marker_states": dict(row["marker_states"]),
            "phenotype_confidence": None,
            "cluster_id": None,
            "cluster_label": None,
        }
        for row in import_rows
    )
    source_refs = tuple(sorted(item.artifact_ref.artifact_ref_sha256 for item in authorities))
    artifact = {
        "schema_version": "phenotype-view-artifact/v1",
        "mode": "marker_rules",
        "slide_id": measurement.slide_id,
        "canonical_image_sha256": measurement.canonical_image_sha256,
        "tissue_generation_id": measurement.tissue_generation_id,
        "phenotype_generation_id": preview_id,
        "section_id": measurement.section_id,
        "source_run_id": source_run_id,
        "cell_ids": [row["cell_id"] for row in import_rows],
        "cell_ids_sha256": measurement.cell_ids_sha256,
        "assignments": list(assignments),
        "evidence": {
            "schema_version": "mif-batch-marker-gate-application-evidence/v1",
            "domain_gate_sha256": gate.domain_gate_sha256,
            "evidence_sha256": gate.evidence_sha256,
            "applied_thresholds": applied_thresholds,
            "phenotyping_population": settings.get("phenotyping_population"),
            "selected_count": len(included_ids), "excluded_count": len(cells) - len(included_ids),
            "source_ref_sha256s": list(source_refs),
        },
        "lineage": {
            "schema_version": "mif-batch-phenotype-view-lineage/v1",
            "source_run_id": source_run_id,
            "source_ref_sha256s": list(source_refs),
        },
    }
    prepared = phenotype_view_store.prepare(artifact)
    return MarkerRuleSectionApplication(
        sample_id=measurement.sample_id,
        project_entry_id=measurement.project_entry_id,
        slide_id=measurement.slide_id,
        canonical_image_id=measurement.canonical_image_id,
        canonical_image_sha256=measurement.canonical_image_sha256,
        section_id=measurement.section_id,
        tissue_generation_id=measurement.tissue_generation_id,
        segmentation_population_id=measurement.segmentation_population_id,
        measurement_generation_id=measurement.measurement_generation_id,
        validation_domain_id=gate.validation_domain_id,
        phenotype_generation_id=preview_id,
        cell_ids_sha256=measurement.cell_ids_sha256,
        cell_to_domain_membership_sha256=measurement.cell_to_domain_membership_sha256,
        source_ref_sha256s=source_refs,
        expected_saved_state_before_sha256=saved_before,
        prepared=prepared,
        view_store=phenotype_view_store,
    )


def build_marker_rule_application_invocation(
    application: MarkerRuleSectionApplication,
    *,
    config: AnalysisConfig,
    run_id: str,
) -> CurrentImageScientificInvocation:
    if type(application) is not MarkerRuleSectionApplication:
        raise BatchPhenotypingError("marker invocation requires exact application")
    return CurrentImageScientificInvocation(
        stage="phenotyping",
        config=config,
        run_id=run_id,
        project_entry_id=application.project_entry_id,
        section_id=application.section_id,
        scientific_state={
            "preflight": {
                "canonical_image_id": application.canonical_image_id,
                "canonical_image_sha256": application.canonical_image_sha256,
            },
            "tissue_acceptance": {
                "generation_id": application.tissue_generation_id,
            },
            "segmentation": {
                application.section_id: {
                    "segmentation_population_id": application.segmentation_population_id,
                }
            },
            "marker_rule_application": {
                "assignment_schema_version": "marker-rule-assignments/v2",
                "phenotype_assignment_source": dict(application.prepared.assignment_source),
                "selected_display_view": "marker_rules",
                "phenotype_generation_id": application.phenotype_generation_id,
                "materialize_display_classes": True,
                "phenotype_view_artifact": application.prepared.to_request_mapping(),
            },
        },
    )


__all__ = [
    "build_marker_rule_application_invocation",
    "build_marker_rule_section_application",
    "decode_committed_marker_gate",
]
