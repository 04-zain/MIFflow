"""Closed marker definitions selected from committed measurement features.

The operation retains the selected stable-cell population, measurement generation and
phenotype view. Its records and stage adapters connect accepted numerical results to the
current batch target and reviewed application scope."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from math import isfinite
from typing import Final

from mif_pipeline.cell_analysis.clustering import FeatureRole
from mif_pipeline.cell_analysis.measurement_plan import FeatureKind
from mif_pipeline.orchestration.batch_measurement_artifacts import (
    ValidatedMeasurementsResultProjection,
)
from mif_pipeline.pipeline_core import (
    InteractionAction,
    InteractionReason,
    StageResult,
    StageStatus,
)

from mif_pipeline.cell_analysis.phenotyping_population import validate_population_settings
from ._common import BatchPhenotypingError, digest

SETUP_CONTEXT_VERSION: Final[str] = "batch-marker-setup/v1"
SETTINGS_VERSION: Final[str] = "marker-phenotype-settings/v4"
_CONTEXT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "validation_domain_id",
        "canonical_marker_ids",
        "measurement_authority_bindings",
        "offered_marker_features",
        "setup_evidence_sha256",
    }
)
_SETTINGS_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "marker_rules",
    }
)
_BINDING_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "project_entry_id",
        "sample_id",
        "slide_id",
        "section_id",
        "measurements_result_ref_sha256",
        "resolved_config_sha256",
        "analysis_domain_bindings",
        "authenticated_cell_count",
        "authenticated_cell_inventory_sha256",
    }
)
_DOMAIN_BINDING_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "sample_id",
        "slide_id",
        "project_entry_id",
        "analysis_domain_id",
        "analysis_domain_revision",
        "analysis_domain_revision_sha256",
        "domain_set_sha256",
    }
)
_FEATURE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "marker_id",
        "feature_id",
        "channel_id",
        "measurement",
        "compartment",
    }
)
_SHA256: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}\Z")


# ==============================================================================
#                                Marker settings
# ==============================================================================


def _mapping(value: object, fields: frozenset[str], label: str) -> Mapping[str, object]:
    if (
        not isinstance(value, Mapping)
        or set(value) != fields
        or any(type(key) is not str for key in value)
    ):
        raise BatchPhenotypingError(f"{label} has invalid fields")
    return value


def _sequence(value: object, label: str) -> tuple[object, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise BatchPhenotypingError(f"{label} must be an array")
    return tuple(value)


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BatchPhenotypingError(f"{label} must be canonical nonblank text")
    return value


def _sha(value: object, label: str) -> str:
    value = _text(value, label)
    if _SHA256.fullmatch(value) is None:
        raise BatchPhenotypingError(f"{label} must be lowercase SHA-256")
    return value


def marker_settings_locked(context: Mapping[str, object]) -> bool:
    """Keep shared choices fixed after confirmation, including an emptied revisit prefix."""
    workflow = context.get("marker_workflow") or {}
    return bool(workflow.get("completed") or workflow.get("settings_locked", False))


def validate_marker_setup_context(result: StageResult) -> Mapping[str, object]:
    """Validate the complete compact setup prompt without a fabricated response.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    if (
        type(result) is not StageResult
        or result.stage != "marker_gate_review"
        or result.status is not StageStatus.NEEDS_INTERACTION
        or result.interaction_reason is not InteractionReason.MARKER_COMPOSITE_RULES_REQUIRED
        or result.allowed_actions
        != (
            InteractionAction.DEFINE_MARKER_COMPOSITE_RULES.value,
            InteractionAction.ABORT.value,
        )
    ):
        raise BatchPhenotypingError("target is not a marker setup request")
    fields = _CONTEXT_FIELDS | (set(result.resume_context) & {"initial_marker_settings", "marker_workflow", "marker_distributions", "phenotyping_population", "population_options", "software_requirement"})
    context = _mapping(result.resume_context, fields, "marker setup context")
    if context["schema_version"] != SETUP_CONTEXT_VERSION:
        raise BatchPhenotypingError("marker setup context version is unsupported")
    _text(context["validation_domain_id"], "validation_domain_id")
    markers = _sequence(context["canonical_marker_ids"], "canonical_marker_ids")
    if not markers or tuple(_text(item, "marker_id") for item in markers) != tuple(
        sorted(set(markers))
    ):
        raise BatchPhenotypingError("marker setup canonical markers are invalid")
    bindings = _sequence(
        context["measurement_authority_bindings"], "measurement_authority_bindings"
    )
    keys = []
    for value in bindings:
        row = _mapping(value, _BINDING_FIELDS, "measurement authority binding")
        key = tuple(
            _text(row[field], field)
            for field in (
                "sample_id",
                "project_entry_id",
                "slide_id",
                "section_id",
                "measurements_result_ref_sha256",
            )
        )
        keys.append(key)
        for field in (
            "measurements_result_ref_sha256",
            "resolved_config_sha256",
            "authenticated_cell_inventory_sha256",
        ):
            _sha(row[field], field)
        if type(row["authenticated_cell_count"]) is not int or row["authenticated_cell_count"] <= 0:
            raise BatchPhenotypingError("marker setup cell count is invalid")
        domain_bindings = _sequence(row["analysis_domain_bindings"], "analysis_domain_bindings")
        if not domain_bindings:
            raise BatchPhenotypingError("marker setup lacks domain bindings")
        for domain_value in domain_bindings:
            domain = _mapping(domain_value, _DOMAIN_BINDING_FIELDS, "analysis domain binding")
            for field in ("sample_id", "slide_id", "project_entry_id", "analysis_domain_id"):
                _text(domain[field], field)
            for field in ("analysis_domain_revision_sha256", "domain_set_sha256"):
                _sha(domain[field], field)
            if (
                type(domain["analysis_domain_revision"]) is not int
                or domain["analysis_domain_revision"] < 1
            ):
                raise BatchPhenotypingError("analysis domain revision is invalid")
            if any(
                domain[field] != row[field]
                for field in ("sample_id", "slide_id", "project_entry_id")
            ):
                raise BatchPhenotypingError("marker setup domain binding is foreign")
    if not keys or keys != sorted(set(keys)):
        raise BatchPhenotypingError("marker setup measurement bindings are not ordered and unique")
    features = _sequence(context["offered_marker_features"], "offered_marker_features")
    feature_keys = []
    for value in features:
        row = _mapping(value, _FEATURE_FIELDS, "offered marker feature")
        feature_keys.append(
            (_text(row["marker_id"], "marker_id"), _text(row["feature_id"], "feature_id"))
        )
        for field in ("channel_id", "measurement", "compartment"):
            _text(row[field], field)
    if (
        not features
        or feature_keys != sorted(set(feature_keys))
        or {item[0] for item in feature_keys} != set(markers)
    ):
        raise BatchPhenotypingError("marker setup offered feature cover is invalid")
    evidence = _sha(context["setup_evidence_sha256"], "setup_evidence_sha256")
    if "initial_marker_settings" in context:
        selected, selected_features = configured_marker_catalog(context["initial_marker_settings"],
            available_marker_ids=markers, offered_marker_features=features)
        validate_marker_settings(context["initial_marker_settings"], canonical_marker_ids=selected, offered_marker_features=selected_features, allow_pending=True)
    workflow = context.get("marker_workflow") or {}
    if "settings_locked" in workflow and type(workflow["settings_locked"]) is not bool:
        raise BatchPhenotypingError("marker workflow settings lock must be Boolean")
    if marker_settings_locked(context) and "initial_marker_settings" not in context:
        raise BatchPhenotypingError("confirmed marker settings require their retained definition")
    if "software_requirement" in context:
        from mif_pipeline.orchestration.setup.nimbus_requirements import NimbusSoftwareRequirement

        NimbusSoftwareRequirement.from_mapping(context["software_requirement"])
    without_digest = {
        key: context[key] for key in fields if key != "setup_evidence_sha256"
    }
    if (
        evidence != digest(without_digest)
        or result.checkpoint_id != "marker-setup-" + evidence[:48]
    ):
        raise BatchPhenotypingError("marker setup evidence or checkpoint differs")
    return context


class MissingMarkerMeasurementsError(BatchPhenotypingError):
    """Mapped biomarkers lack the Mean or Median required for marker rules."""


def marker_feature_candidates(
    measurements: tuple[ValidatedMeasurementsResultProjection, ...],
) -> tuple[tuple[str, ...], tuple[Mapping[str, str], ...]]:
    """Offer measured marker means and medians shared by all participating images."""

    if not measurements:
        raise BatchPhenotypingError("marker setup requires committed measurements")
    common: tuple[Mapping[str, str], ...] | None = None
    markers: tuple[str, ...] | None = None
    known_inventory = None
    for measurement in measurements:
        channel_by_id = {item["channel_id"]: item for item in measurement.canonical_channels}
        known = tuple(
            sorted(
                item["biomarker"]
                for item in measurement.canonical_channels
                if item["biomarker"] is not None
            )
        )
        if not known or len(set(known)) != len(known):
            raise BatchPhenotypingError("processed marker biomarkers are absent or ambiguous")
        offered = []
        for feature in measurement.measurement_plan.features:
            if feature.kind != FeatureKind.EXPRESSION.value:
                continue
            if feature.measurement.rsplit(": ", 1)[-1] not in {"Mean", "Median"}:
                continue
            channel = channel_by_id.get(feature.channel_id)
            if (
                feature.role is not FeatureRole.EXPRESSION
                or feature.feature_id is None
                or channel is None
                or channel["biomarker"] is None
                or feature.biomarker != channel["biomarker"]
            ):
                raise BatchPhenotypingError(
                    "measured expression lacks an exact processed biomarker"
                )
            offered.append(
                {
                    "marker_id": feature.biomarker,
                    "feature_id": feature.feature_id,
                    "channel_id": feature.channel_id,
                    "measurement": feature.measurement,
                    "compartment": feature.compartment,
                }
            )
        offered.sort(key=lambda item: (item["marker_id"], item["feature_id"]))
        if len({item["feature_id"] for item in offered}) != len(offered):
            raise BatchPhenotypingError("marker measurements do not cover processed biomarkers")
        if not offered:
            raise MissingMarkerMeasurementsError(
                "Marker rules require at least one measured marker with Mean or Median. "
                "Open Cell measurements → Change cell measurements… and include Mean "
                "or Median for the markers to phenotype. Preview and apply the change, "
                "then continue."
            )
        projection = tuple(offered)
        if common is None:
            common, markers = projection, tuple(sorted({item['marker_id'] for item in offered}))
            known_inventory = known
        elif projection != common or known != known_inventory:
            raise BatchPhenotypingError("marker feature candidates differ across participants")
    assert common is not None and markers is not None
    return markers, common


def configured_marker_catalog(settings, *, available_marker_ids, offered_marker_features):
    """Select the existing measured catalog using the marker rules themselves."""
    if not isinstance(settings, Mapping):
        raise BatchPhenotypingError('marker settings must be an object')
    rules = _sequence(settings.get('marker_rules'), 'marker_rules')
    if any(not isinstance(rule, Mapping) for rule in rules):
        raise BatchPhenotypingError('marker rules must be objects')
    markers = tuple(_text(rule.get('marker_id'), 'marker_id') for rule in rules)
    available = tuple(available_marker_ids)
    if not markers or markers != tuple(sorted(set(markers))) or not set(markers) <= set(available):
        raise BatchPhenotypingError('Select one or more available markers in canonical order.')
    features = tuple(row for row in offered_marker_features if row['marker_id'] in markers)
    if {row['marker_id'] for row in features} != set(markers):
        raise BatchPhenotypingError('Each selected marker requires an available Mean or Median measurement.')
    return markers, features


def _location_constraints(value: object, method: str) -> dict[str, object]:
    """Retain shared component-location bounds, rejecting controls unused by the fit."""
    if not isinstance(value, Mapping) or set(value) - ({method} if method in {"gmm", "gamma"} else set()):
        raise BatchPhenotypingError("component constraints must belong to the selected mixture")
    result = {}
    for family, config in value.items():
        name = "mean_bounds" if family == "gmm" else "mode_bounds"
        config = _mapping(config, frozenset({name}), "component location constraint")
        bounds = _sequence(config[name], name)
        if len(bounds) != 2:
            raise BatchPhenotypingError("component location bounds require two components")
        retained = []
        for pair in bounds:
            pair = _sequence(pair, "component bounds")
            if len(pair) != 2:
                raise BatchPhenotypingError("component bounds require lower and upper endpoints")
            endpoints = []
            for endpoint in pair:
                if endpoint is None:
                    endpoints.append(None)
                elif isinstance(endpoint, Mapping):
                    endpoint = _mapping(endpoint, frozenset({"quantile"}), "quantile bound")
                    q = endpoint["quantile"]
                    if isinstance(q, bool) or not isinstance(q, (int, float)) or not isfinite(q) or not 0 <= q <= 1:
                        raise BatchPhenotypingError("bound quantile must lie between zero and one")
                    endpoints.append({"quantile": float(q)})
                elif isinstance(endpoint, bool) or not isinstance(endpoint, (int, float)) or not isfinite(endpoint):
                    raise BatchPhenotypingError("bound must be finite numeric, quantile, or null")
                else:
                    endpoints.append(float(endpoint))
            if all(isinstance(x, (int, float)) for x in endpoints) and endpoints[0] > endpoints[1]:
                raise BatchPhenotypingError("component lower bound exceeds upper bound")
            retained.append(endpoints)
        result[family] = {name: retained}
    return result


def marker_population_from_modes(modes):
    """Project each accepted image selection into the existing population settings."""
    from mif_pipeline.cell_analysis.phenotyping_population import slide_population_key
    selections = {}
    for mode in modes:
        key = slide_population_key(mode.sample_id, mode.project_entry_id, mode.slide_id)
        selected = dict(mode.population_selection)
        if key in selections and selections[key] != selected:
            raise BatchPhenotypingError("Analysis regions in one image have different phenotyping cell selections.")
        selections[key] = selected
    if not selections:
        raise BatchPhenotypingError("Marker fitting requires an accepted cell selection.")
    default = selections[sorted(selections)[0]]
    return validate_population_settings({'default':default,'slide_overrides':{key:value for key,value in selections.items() if value!=default}})


def validate_marker_settings(
    value: object,
    *,
    canonical_marker_ids: Sequence[str],
    offered_marker_features: Sequence[Mapping[str, object]],
    allow_pending: bool = False,
) -> dict[str, object]:
    """Validate the current marker definition without inferring a fitting method.

    The current schema records a deliberate method and fit scope for every
    configured marker. Pending sequential-review markers can omit their method,
    feature and polarity only while the owner explicitly admits pending choices.
    Earlier settings lack these decisions and are not converted at this boundary.
    """

    if not isinstance(value, Mapping):
        raise BatchPhenotypingError("marker settings must be an object")
    version = value.get("schema_version")
    if version != SETTINGS_VERSION:
        raise BatchPhenotypingError("marker settings version is unsupported")
    fields = _SETTINGS_FIELDS | {"fit_scope"} | ({"phenotyping_population"} if "phenotyping_population" in value else set())
    raw = _mapping(value, fields, "marker settings")
    if raw["fit_scope"] not in {"pooled", "per_slide"}:
        raise BatchPhenotypingError("marker fit_scope must be pooled or per_slide")
    markers = tuple(canonical_marker_ids)
    if not markers or markers != tuple(sorted(set(markers))):
        raise BatchPhenotypingError("marker setup marker IDs are invalid")
    offers: dict[tuple[str, str], Mapping[str, object]] = {}
    for candidate in offered_marker_features:
        row = _mapping(
            candidate,
            frozenset({"marker_id", "feature_id", "channel_id", "measurement", "compartment"}),
            "offered marker feature",
        )
        key = (_text(row["marker_id"], "marker_id"), _text(row["feature_id"], "feature_id"))
        if key in offers or key[0] not in markers:
            raise BatchPhenotypingError("offered marker features are ambiguous")
        offers[key] = row
    selected = []
    for item in _sequence(raw["marker_rules"], "marker_rules"):
        required = {"marker_id", "feature_id", "polarity", "method", "gaussian_variance", "component_location_constraints"}
        optional = {"nimbus_score_path", "nimbus_checkpoint_path", "nimbus_magnification", "nimbus_cutoff", "nimbus_distill"}
        if not isinstance(item, Mapping) or not required <= set(item) or set(item) - required - optional:
            raise BatchPhenotypingError("marker rule has invalid fields")
        row = item
        marker_id = _text(row["marker_id"], "marker_id")
        pending = allow_pending and row["method"] is None
        feature_id = None if pending and row["feature_id"] is None else _text(row["feature_id"], "feature_id")
        polarity = None if pending and row["polarity"] is None else _text(row["polarity"], "polarity")
        if feature_id is not None and (marker_id, feature_id) not in offers:
            raise BatchPhenotypingError("Choose an offered measurement for this marker")
        if polarity is not None and polarity not in {"positive", "above"}:
            raise BatchPhenotypingError("Marker positivity requires an intensity at or above the threshold")
        rule = {"marker_id": marker_id, "feature_id": feature_id, "polarity": polarity}
        method = row["method"]
        if not pending and method not in {"gmm", "gamma", "triangle", "nimbus", "manual"}:
            raise BatchPhenotypingError("marker method is unsupported")
        if row["gaussian_variance"] not in {"shared", "unshared"}:
            raise BatchPhenotypingError("Gaussian variance must be shared or unshared")
        constraints = _location_constraints(row["component_location_constraints"], method)
        rule.update(method=method, gaussian_variance=row["gaussian_variance"], component_location_constraints=constraints)
        nimbus_keys = set(row) & {"nimbus_score_path", "nimbus_checkpoint_path", "nimbus_magnification", "nimbus_cutoff", "nimbus_distill"}
        if nimbus_keys and method != "nimbus":
            raise BatchPhenotypingError("Nimbus settings require the Nimbus method")
        if method == "nimbus":
            if polarity not in {"positive", "above"}:
                raise BatchPhenotypingError("Nimbus positivity follows scores at or above the accepted cutoff")
            path = row.get("nimbus_score_path", "")
            if type(path) is not str:
                raise BatchPhenotypingError("Nimbus score path must be text")
            checkpoint = row.get("nimbus_checkpoint_path", "")
            if type(checkpoint) is not str or (path and checkpoint):
                raise BatchPhenotypingError("Nimbus model selection and saved-score import are separate input routes")
            cutoff = row.get("nimbus_cutoff", 0.5)
            if isinstance(cutoff, bool) or not isinstance(cutoff, (int, float)) or not isfinite(cutoff) or not 0 <= cutoff <= 1:
                raise BatchPhenotypingError("Nimbus cutoff must be between zero and one")
            distill = row.get("nimbus_distill", False)
            if type(distill) is not bool or distill:
                raise BatchPhenotypingError("Nimbus distillation is requested after reviewing direct scores")
            rule.update(nimbus_score_path=path, nimbus_cutoff=float(cutoff), nimbus_distill=distill)
            if not path:
                if checkpoint:
                    rule["nimbus_checkpoint_path"] = checkpoint
            magnification = row.get("nimbus_magnification")
            if magnification is not None:
                if isinstance(magnification, bool) or not isinstance(magnification, (int, float)) or not isfinite(magnification) or magnification <= 0:
                    raise BatchPhenotypingError("Nimbus magnification must be positive and finite")
                rule["nimbus_magnification"] = float(magnification)
        selected.append(rule)
    if tuple(item["marker_id"] for item in selected) != markers:
        raise BatchPhenotypingError("marker rules must exactly cover canonical markers in order")
    result = {
        "schema_version": version,
        "marker_rules": selected,
    }
    result["fit_scope"] = raw["fit_scope"]
    result["phenotyping_population"] = validate_population_settings(raw.get("phenotyping_population"))
    return result


def validate_marker_setup_response(
    result: StageResult,
    response: Mapping[str, object],
) -> dict[str, object]:
    """Normalize one batch marker setup response against offered features."""

    context = validate_marker_setup_context(result)
    if "revisit_marker_id" in response:
        _mapping(response, frozenset({"revisit_marker_id"} | ({"marker_settings"} if "marker_settings" in response else set())), "marker revisit response")
        marker = _text(response["revisit_marker_id"], "revisit_marker_id")
        workflow = context.get("marker_workflow") or {}
        markers = tuple(context["canonical_marker_ids"])
        completed = tuple(item["marker_rule"]["marker_id"] for item in workflow.get("completed", ()))
        current = workflow.get("current_marker_id")
        if current not in markers or completed != markers[:markers.index(current)] or marker not in completed:
            raise BatchPhenotypingError("choose a previously confirmed marker from this current sequence")
        if 'marker_settings' in response:
            from .marker_preparation import validate_preparation_change
            return {'revisit_marker_id': marker, 'marker_settings': validate_preparation_change(result, response['marker_settings'], marker)}
        return {"revisit_marker_id": marker}
    if response.get("preview_population") is True and set(response) == {"preview_population", "phenotyping_population"}:
        selected = validate_population_settings(response["phenotyping_population"])
        if selected != context.get("phenotyping_population"):
            raise BatchPhenotypingError("Marker fitting uses the cell selection accepted before choosing the phenotyping method.")
        return {"preview_population": True, "phenotyping_population": selected}
    root = _mapping(response, frozenset({"marker_settings"}) | ({"preview_population"} if "preview_population" in response else set()), "marker setup response")
    preview_population = root.get("preview_population", False)
    if type(preview_population) is not bool:
        raise BatchPhenotypingError("preview_population must be Boolean")
    workflow = context.get("marker_workflow")
    selected, selected_features = configured_marker_catalog(root['marker_settings'],
        available_marker_ids=_sequence(context['canonical_marker_ids'], 'canonical_marker_ids'),
        offered_marker_features=_sequence(context['offered_marker_features'], 'offered_marker_features'))
    settings = validate_marker_settings(root["marker_settings"],
        canonical_marker_ids=selected,
        offered_marker_features=selected_features,
        allow_pending=workflow is not None)
    if workflow is not None:
        current = workflow["current_marker_id"] if marker_settings_locked(context) else selected[0]
        completed = tuple(workflow["completed"])
        by_marker = {item["marker_id"]: item for item in settings["marker_rules"]}
        if current not in by_marker or (by_marker[current]["method"] is None and not preview_population):
            raise BatchPhenotypingError("choose the current marker's feature, direction, and method before fitting")
        if not preview_population:
            distribution = next((item for item in context.get("marker_distributions", ())
                if item["marker_id"] == current and item["feature_id"] == by_marker[current]["feature_id"]), None)
            if distribution is not None and distribution["pooled"]["eligible_count"] + distribution["pooled"]["unknown_count"] == 0:
                raise BatchPhenotypingError("No cells occur in the selected region. Change the cells selected for phenotyping before fitting.")
        if not preview_population and settings["phenotyping_population"] != validate_population_settings(context.get("phenotyping_population")):
            raise BatchPhenotypingError("refresh population distributions before choosing a method")
        if preview_population and any(item["method"] is not None for item in settings["marker_rules"] if item["marker_id"] not in {entry["marker_rule"]["marker_id"] for entry in completed}):
            raise BatchPhenotypingError("population preview cannot fit an unconfirmed marker")
        if marker_settings_locked(context):
            initial = context["initial_marker_settings"]
            if selected != tuple(rule['marker_id'] for rule in initial['marker_rules']):
                raise BatchPhenotypingError('Accepted marker reviews retain the selected marker sequence.')
            if any(digest(settings[key]) != digest(initial[key]) for key in ("fit_scope", "phenotyping_population")):
                raise BatchPhenotypingError("accepted marker reviews retain the same population and fitting scope")
        for item in completed:
            rule = item["marker_rule"]
            if digest(by_marker.get(rule["marker_id"])) != digest(rule):
                raise BatchPhenotypingError("a completed marker's configuration cannot change while reviewing the next marker")
        configured = {item["marker_rule"]["marker_id"] for item in completed} | {current}
        retained = {item['marker_rule']['marker_id']: item['marker_rule']
                    for item in workflow.get('retained_completed', ())}
        if any(rule["method"] is not None and rule["marker_id"] not in configured
               and digest(rule) != digest(retained.get(rule['marker_id'])) for rule in settings["marker_rules"]):
            raise BatchPhenotypingError("configure one marker at a time after inspecting its distribution")
    return {"marker_settings": settings, **({"preview_population": preview_population} if "preview_population" in root else {})}


def validate_marker_definition(value: object, *, canonical_marker_ids: Sequence[str]) -> dict[str, object]:
    """Validate ordered phenotype rules against markers with accepted gates."""
    raw = _mapping(value, frozenset({"phenotype_classes", "composite_rules", "residual_phenotype_id"}), "phenotype definition")
    markers = tuple(canonical_marker_ids)
    classes = []
    class_ids: set[str] = set()
    displays: set[str] = set()
    for index, item in enumerate(_sequence(raw["phenotype_classes"], "phenotype_classes")):
        if not isinstance(item, Mapping) or set(item) not in (
            {"class_id", "display_name"},
            {"class_id", "display_name", "precedence"},
        ):
            raise BatchPhenotypingError("phenotype class has invalid fields")
        row = item
        if "precedence" in row and (
            type(row["precedence"]) is not int or row["precedence"] != index
        ):
            raise BatchPhenotypingError("phenotype class precedence differs from order")
        class_id = _text(row["class_id"], "class_id")
        display = _text(row["display_name"], "display_name")
        if display.casefold() == "not phenotyped":
            raise BatchPhenotypingError("Not phenotyped is reserved for cells outside the selected regions")
        if class_id in class_ids or display.casefold() in displays:
            raise BatchPhenotypingError("marker phenotype classes are ambiguous")
        class_ids.add(class_id)
        displays.add(display.casefold())
        classes.append({"class_id": class_id, "display_name": display, "precedence": index})
    if len(classes) < 2:
        raise BatchPhenotypingError("marker settings require a composite and residual class")
    residual = _text(raw["residual_phenotype_id"], "residual_phenotype_id")
    if residual not in class_ids:
        raise BatchPhenotypingError("marker residual phenotype is undeclared")
    composites = []
    targeted: set[str] = set()
    for item in _sequence(raw["composite_rules"], "composite_rules"):
        row = _mapping(item, frozenset({"phenotype_id", "marker_states"}), "composite rule")
        phenotype = _text(row["phenotype_id"], "phenotype_id")
        if phenotype not in class_ids or phenotype == residual or phenotype in targeted:
            raise BatchPhenotypingError("composite phenotype is unknown, residual, or duplicated")
        states = row["marker_states"]
        if not isinstance(states, Mapping) or not states:
            raise BatchPhenotypingError("composite marker states are empty")
        normalized_states = {}
        for marker, state in states.items():
            if marker not in markers or state not in {"positive", "negative"}:
                raise BatchPhenotypingError("composite predicate is unknown or contradictory")
            normalized_states[marker] = state
        targeted.add(phenotype)
        composites.append(
            {"phenotype_id": phenotype, "marker_states": dict(sorted(normalized_states.items()))}
        )
    if targeted != class_ids - {residual}:
        raise BatchPhenotypingError("marker definition requires exactly one residual class")
    if tuple(row["phenotype_id"] for row in composites) != tuple(row["class_id"] for row in classes if row["class_id"] != residual):
        raise BatchPhenotypingError("phenotype rule order differs from class precedence")
    return {"phenotype_classes": classes, "composite_rules": composites, "residual_phenotype_id": residual}



__all__ = [
    "SETTINGS_VERSION",
    "SETUP_CONTEXT_VERSION",
    "marker_feature_candidates",
    "marker_settings_locked",
    "validate_marker_settings",
    "validate_marker_setup_response",
    "validate_marker_setup_context",
    "validate_marker_definition",
]
