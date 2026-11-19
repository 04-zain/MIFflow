"""Offer read-only measurement and composition figures for accepted cell selections."""
from contextlib import contextmanager
from urllib.parse import quote

from .batch_service_models import BatchSavedStageFigure


class _MeasuredCells:
    """Project the accepted component's applied rows, without another cell store."""
    def __init__(self, view, component, measurement, scope):
        self.view, self.component = view, component
        self.schema, self.dictionaries = view.schema, view.dictionaries
        self.row_count, self.scope = measurement.cell_count, scope
        self.canonical_channels = measurement.canonical_channels
        self._presence = "present:measurement:" + measurement.measurement_plan.features[0].measurement

    def iter_batches(self, *, columns):
        yield from self.view.iter_applied_batches(self.component, columns=columns)

    def lookup_ids(self, ids, *, columns):
        fields = tuple(dict.fromkeys((*columns, self._presence)))
        for batch in self.view.lookup_ids(ids, columns=fields):
            if not all(batch.column(self._presence).fill_null(False).to_pylist()):
                raise ValueError("A requested cell is outside the accepted measurement result.")
            yield batch.select(columns)

    def query(self, bounds, *, columns, predicate='intersects'):
        """Read current accepted measurement rows through the spatial index."""
        fields = tuple(dict.fromkeys((*columns, self._presence)))
        for batch in self.view.query(bounds, columns=fields, predicate=predicate):
            selected = batch.filter(batch.column(self._presence).fill_null(False))
            if selected.num_rows:
                yield selected.select(columns)


@contextmanager
def _selected_cells(authority, *, state_store=None, snapshot=None):
    if authority.artifact_ref.artifact_kind == "segmentation_result":
        from .saved_segmentation_cells import segmentation_cells
        cells = segmentation_cells(authority, state_store, snapshot=snapshot)
        try:
            yield cells
        finally:
            if hasattr(cells, '_edit_index'):
                cells._edit_index.close()
        return
    if authority.artifact_ref.artifact_kind == "measurements_result":
        from .batch_measurement_artifacts import decode_committed_measurements_result
        from .batch_phenotyping.marker_review_gallery import measurement_cells
        measurement = decode_committed_measurements_result(authority)
        with measurement_cells(measurement) as (view, component, scope):
            yield _MeasuredCells(view, component, measurement, scope)
        return
    with _accepted_handoff(authority) as handoff:
        from .measurement_region_review import FinalRegionCells
        yield FinalRegionCells(handoff, authority.artifact_ref.owning_identity)


@contextmanager
def _accepted_handoff(authority):
    """Open the same authenticated final cells and their accepted region evidence."""
    from .exports import load_spatial_handoff_with_artifacts
    if authority.artifact_ref.artifact_kind != 'spatial_cell_handoff':
        raise ValueError('Select a final accepted cell result.')
    identity = authority.artifact_ref.owning_identity
    handoff, artifacts = load_spatial_handoff_with_artifacts(authority.artifact_ref.locator)
    try:
        if (artifacts.descriptor.sha256 != identity["descriptor"]["sha256"]
                or artifacts.cells.sha256 != identity["cell_artifact"]["sha256"]
                or handoff.cell_count != identity["cell_count"]
                or handoff.selected_phenotype_view != identity["selected_phenotype_view"]
                or handoff.phenotype_generation_id != identity["phenotype_generation_id"]
                or handoff.phenotype_inventory_sha256 != identity["phenotype_inventory_sha256"]
                or handoff.cell_selection_sha256 != identity["cell_selection_sha256"]):
            raise ValueError("The saved cells differ from the selected accepted result.")
        yield handoff
    finally:
        handoff.cell_dataset.close()


def _measurements(authority, snapshot, *, state_store=None, dataset=None):
    from .batch_measurement_artifacts import decode_committed_measurements_result

    if authority.artifact_ref.artifact_kind == "segmentation_result":
        from .saved_segmentation_cells import segmentation_cells, shape_features
        return shape_features(dataset if dataset is not None else segmentation_cells(authority, state_store, snapshot=snapshot))
    if authority.artifact_ref.artifact_kind == "measurements_result":
        return decode_committed_measurements_result(authority).measurement_plan.features
    identity = authority.artifact_ref.owning_identity
    current = {item.artifact_ref.artifact_ref_sha256: item for item in snapshot.committed_artifact_authorities}
    features = {}
    for reference in identity["measurements_ref_sha256s"]:
        source = current.get(reference)
        if source is None or source.artifact_ref.artifact_kind != "measurements_result":
            raise ValueError("The selected cell measurement result is no longer current.")
        measurement = decode_committed_measurements_result(source)
        if measurement.sample_id != identity["sample_id"]:
            raise ValueError("The measurement result belongs to a different sample.")
        for feature in measurement.measurement_plan.features:
            previous = features.setdefault(feature.measurement, feature)
            if any(getattr(previous, name) != getattr(feature, name)
                   for name in ("units", "kind", "compartment", "biomarker", "channel_id")):
                raise ValueError("Saved sections disagree on the measurement definition.")
    return tuple(features.values())


def cell_frame(authority):
    """Resolve the selected cells' canonical image from their saved result."""
    if authority.artifact_ref.artifact_kind == 'segmentation_result':
        from .batch_segmentation_artifacts import decode_committed_segmentation_result
        return decode_committed_segmentation_result(authority)
    if authority.artifact_ref.artifact_kind == 'measurements_result':
        from .batch_measurement_artifacts import decode_committed_measurements_result
        return decode_committed_measurements_result(authority)
    from .exports import load_spatial_handoff
    handoff = load_spatial_handoff(authority.artifact_ref.locator)
    try:
        return handoff.spatial_frame
    finally:
        handoff.cell_dataset.close()


def _profile_groups(dataset, features):
    from mif_pipeline.cell_analysis.result_plots import PROFILE_MARKERS_PER_FIGURE

    groups = {}
    for feature in features:
        if (feature.kind == "expression" and feature.measurement.endswith(": Mean")
                and "measurement:" + feature.measurement in dataset.schema.names):
            groups.setdefault(feature.units, []).append(feature)
    return tuple(tuple(features[start:start + PROFILE_MARKERS_PER_FIGURE])
                 for features in groups.values()
                 for start in range(0, len(features), PROFILE_MARKERS_PER_FIGURE))


def _choices(dataset, features, *, phenotypes=True):
    from mif_pipeline.cell_analysis.result_plots import COMPOSITION_PAGE_SIZE, PROFILE_PAGE_SIZE

    choices = []
    for feature in features:
        if "measurement:" + feature.measurement not in dataset.schema.names:
            continue
        choices.append(BatchSavedStageFigure(
            "distribution/" + quote(feature.measurement, safe=""),
            "Measurement distribution · " + feature.measurement,
            f"Distribution of saved {feature.measurement} values. "
            "Cells without a usable measurement are counted separately. "
            "Uses 256 equal-width bins on the stored scale"
            + (f" ({feature.units})." if feature.units else "."), data_formats=("csv",)))
    if phenotypes and "primary_phenotype" in dataset.schema.names:
        dictionary = dataset.dictionaries.get("primary_phenotype")
        categories = max(1, len(dictionary.labels) if dictionary else 0)
        pages = (categories + COMPOSITION_PAGE_SIZE - 1) // COMPOSITION_PAGE_SIZE
        for page in range(pages):
            first = page * COMPOSITION_PAGE_SIZE + 1
            last = min((page + 1) * COMPOSITION_PAGE_SIZE, categories)
            choices.append(BatchSavedStageFigure(
                f"composition/{page}", "Phenotype composition" + (f" · Categories {first}–{last}" if pages > 1 else ""),
                "Saved phenotype counts and percentages of all exported cells in this sample. "
                "Declared categories with zero cells are retained; unavailable assignments are counted separately.", data_formats=("csv",)))
        if "primary_compartment" in dataset.schema.names:
            from mif_pipeline.cell_analysis.compartment_composition_plots import (
                PHENOTYPES_PER_PAGE, COMPARTMENTS_PER_PAGE, compartment_ids,
            )
            row_pages = (categories + PHENOTYPES_PER_PAGE - 1) // PHENOTYPES_PER_PAGE
            compartments = max(1, len(compartment_ids(dataset)))
            column_pages = (compartments + COMPARTMENTS_PER_PAGE - 1) // COMPARTMENTS_PER_PAGE
            for row_page in range(row_pages):
                for column_page in range(column_pages):
                    title = "Phenotypes by tissue compartment"
                    if row_pages > 1:
                        title += f" · Categories {row_page * PHENOTYPES_PER_PAGE + 1}–{min((row_page + 1) * PHENOTYPES_PER_PAGE, categories)}"
                    if column_pages > 1:
                        title += f" · Compartments {column_page * COMPARTMENTS_PER_PAGE + 1}–{min((column_page + 1) * COMPARTMENTS_PER_PAGE, compartments)}"
                    choices.append(BatchSavedStageFigure(
                        f"compartments/{row_page}/{column_page}", title,
                        "Phenotype percentages within each primary tissue compartment. "
                        "Each denominator includes all exported cells in that compartment; "
                        "column labels show the cell count. Unassigned compartments and unavailable "
                        "phenotypes are included when present. Figure data contains exact counts "
                        "and denominators for all categories and compartments.", data_formats=("csv",)))
        profile_pages = (categories + PROFILE_PAGE_SIZE - 1) // PROFILE_PAGE_SIZE
        groups = _profile_groups(dataset, features)
        for group, markers in enumerate(groups):
            for page in range(profile_pages):
                title = "Marker profiles"
                if len(groups) > 1:
                    title += f" · Measurement group {group + 1}"
                if profile_pages > 1:
                    title += f" · Categories {page * PROFILE_PAGE_SIZE + 1}–{min((page + 1) * PROFILE_PAGE_SIZE, categories)}"
                choices.append(BatchSavedStageFigure(
                    f"profiles/{group}/{page}", title,
                    "Arithmetic means of saved marker measurements by phenotype, on their recorded scale. "
                    "Row labels show total cells; entry counts identify incomplete measurements. "
                    + (f"Units: {markers[0].units}. " if markers[0].units else "")
                    + "Measurements: " + "; ".join(marker.measurement for marker in markers), data_formats=("csv",)))
    return tuple(choices)


def cell_figure_choices(authority, snapshot, *, state_store=None):
    """Discover saved fields/dictionaries from metadata, without reading cell rows."""
    with _selected_cells(authority, state_store=state_store, snapshot=snapshot) as dataset:
        features = _measurements(authority, snapshot, dataset=dataset)
        return _choices(dataset, features, phenotypes=authority.artifact_ref.artifact_kind == "spatial_cell_handoff")


def measurement_review(authority, figure_id, snapshot, options, *, summary=None, state_store=None, dependencies=None):
    """Resolve a distribution and its exact interval from the same accepted cells."""
    from dataclasses import asdict
    from mif_pipeline.cell_analysis.result_plots import (
        MeasurementDistribution, count_measurement_distribution,
        select_measurement_interval, render_measurement_distribution,
    )
    phenotype = options.get('phenotype')
    if phenotype is not None and authority.artifact_ref.artifact_kind != 'spatial_cell_handoff':
        raise ValueError('Phenotype selection requires the accepted cell results.')
    if options.get('cohort_artifact_refs'):
        return measurement_cohort_review(authority, figure_id, snapshot, options, state_store=state_store, dependencies=dependencies)
    from .measurement_region_review import filtered_cells, region_choices, region_selection, saved_primary_labels
    selected_region = region_selection(options.get('region'))
    with _selected_cells(authority, state_store=state_store, snapshot=snapshot) as dataset:
        features = _measurements(authority, snapshot, dataset=dataset)
        feature = next((item for item in features
            if "distribution/" + quote(item.measurement, safe="") == figure_id), None)
        if feature is None:
            raise ValueError("Select an available saved measurement.")
        image_channel_index = next((index for index, channel in enumerate(
            getattr(dataset, "canonical_channels", ())) if channel["channel_id"] == feature.channel_id), None)
        labels = (saved_primary_labels([authority], snapshot, dependencies) if dependencies is not None
                  and authority.artifact_ref.artifact_kind == 'measurements_result' else None)
        choices = region_choices([dataset], labels) if authority.artifact_ref.artifact_kind in {'measurements_result', 'spatial_cell_handoff'} else []
        if selected_region is not None and selected_region not in [item['selection'] for item in choices]:
            raise ValueError('Choose a region in the current saved measurements.')
        dataset = filtered_cells(dataset, selected_region)
        from .measurement_phenotype_review import filter_phenotype, phenotype_choices
        phenotypes = phenotype_choices(dataset) if authority.artifact_ref.artifact_kind == 'spatial_cell_handoff' else ()
        dataset = filter_phenotype(dataset, phenotype)
        if summary is not None and (summary.get('region') != selected_region or summary.get('phenotype') != phenotype):
            summary = None
        distribution = (count_measurement_distribution(dataset, feature.measurement, feature.units)
                        if summary is None else MeasurementDistribution(**{
                            key: summary[key] for key in
                            ("measurement", "units", "edges", "counts", "total", "missing", "nonfinite")}))
        interval = None
        examples = []
        if distribution.edges:
            interval = select_measurement_interval(dataset, feature.measurement,
                options.get("lower", distribution.edges[0]), options.get("upper", distribution.edges[-1]))
            columns = tuple(name for name in ("cell_id", "section_id", "analysis_domain_id",
                "analysis_domain_kind", "x_um", "y_um") if name in dataset.schema.names)
            rows = {row["cell_id"]: row for batch in dataset.lookup_ids(interval.cell_ids, columns=columns)
                    for row in batch.to_pylist()}
            kind_dictionary = dataset.dictionaries.get("analysis_domain_kind")
            for cell_id, value in zip(interval.cell_ids, interval.values):
                row = rows[cell_id]
                if kind_dictionary is not None and row.get("analysis_domain_kind") is not None:
                    row["analysis_domain_kind"] = kind_dictionary.labels[row["analysis_domain_kind"]]
                examples.append({**row, "value": value})
    resolved = {**asdict(distribution), "interval": asdict(interval) if interval is not None else None,
                "feature": {**{name: getattr(feature, name) for name in
                    ("measurement", "kind", "compartment", "channel_id", "biomarker", "units")},
                    "image_channel_index": image_channel_index},
                "examples": examples, "region_choices": choices, "region": selected_region,
                "phenotype_choices": phenotypes, "phenotype": phenotype}
    region = next((item for item in choices if item['selection'] == selected_region), None) if selected_region is not None else None
    if 'export_palette' in options:
        resolved['export_palette'] = options['export_palette']
    return render_measurement_distribution(distribution, interval=interval, region=region, phenotype=phenotype,
        export_palette=options.get('export_palette', 'default')), resolved


def measurement_cohort_members(authority, figure_id, snapshot, references):
    """Resolve current owners with the same feature semantics and producer recipe."""
    from .batch_measurement_artifacts import decode_committed_measurements_result
    selected = decode_committed_measurements_result(authority)
    feature = next((item for item in selected.measurement_plan.features
        if 'distribution/' + quote(item.measurement, safe='') == figure_id), None)
    if feature is None:
        raise ValueError('Select an available saved measurement.')
    fields = ('measurement', 'kind', 'compartment', 'channel_id', 'biomarker', 'units',
              'producer', 'producer_version')
    def semantic(measurement, candidate):
        channel = next((dict(c) for c in measurement.canonical_channels
                        if c['channel_id'] == candidate.channel_id), None)
        domain = None
        if candidate.channel_id is not None:
            source = next((a.artifact_ref for a in snapshot.committed_artifact_authorities
                if a.artifact_ref.artifact_kind == 'processed_image'
                and a.artifact_ref.scope.sample_id == measurement.sample_id
                and a.artifact_ref.sha256 == measurement.canonical_image_sha256), None)
            # The accepted basis establishes an unmixed intensity domain. The
            # legacy normalization digest includes image-specific publication
            # IDs, so it is not a comparison of measurement scale or units.
            domain = (('unavailable', measurement.canonical_image_sha256) if source is None else
                      ('unmixed', source.owning_identity['final_basis_sha256'])
                      if source.owning_identity.get('final_basis_sha256') is not None else ('as_stored',))
        return tuple(getattr(candidate, key) for key in fields), channel, domain
    expected = semantic(selected, feature)
    current = {item.artifact_ref.artifact_ref_sha256: item for item in snapshot.committed_artifact_authorities
               if item.artifact_ref.artifact_kind == 'measurements_result'}
    if (not isinstance(references, (list, tuple)) or len(set(references)) != len(references)
            or authority.artifact_ref.artifact_ref_sha256 not in references
            or any(reference not in current for reference in references)):
        raise ValueError('The measurement image selection is no longer current.')
    order = {sample.sample_id: i for i,sample in enumerate(snapshot.manifest.enabled_samples)}
    nodes = {node.node_id:i for i,node in enumerate(snapshot.plan.nodes)}
    sources = sorted((current[reference] for reference in references), key=lambda item: (
        order[item.artifact_ref.scope.sample_id], nodes.get(item.artifact_ref.producer_node_id, len(nodes))))
    members = []
    for source in sources:
        measurement = decode_committed_measurements_result(source)
        candidate = next((item for item in measurement.measurement_plan.features
                          if item.measurement == feature.measurement), None)
        if candidate is not None and semantic(measurement, candidate) == expected:
            members.append((source, measurement, candidate))
    return members


def measurement_cohort_review(authority, figure_id, snapshot, options, *, state_store=None, dependencies=None):
    """Count compatible accepted images; retain each image's original field owner."""
    from dataclasses import asdict
    from mif_pipeline.cell_analysis.result_plots import count_measurement_cohort, render_measurement_distribution, _csv_output
    if options.get('plot_scope','all') not in {'all','image'}:
        raise ValueError('Choose All images or Selected image for the measurement plot.')
    members = measurement_cohort_members(authority, figure_id, snapshot, options['cohort_artifact_refs'])
    active = next(item for item in members if item[0].artifact_ref.artifact_ref_sha256 == authority.artifact_ref.artifact_ref_sha256)
    feature = active[2]
    from .measurement_region_review import filtered_cells, region_choices, region_selection, saved_primary_labels
    selected_region = region_selection(options.get('region'))
    def datasets():
        for source, _, _ in members:
            with _selected_cells(source, state_store=state_store) as dataset:
                yield dataset
    labels = saved_primary_labels([source for source,_,_ in members], snapshot, dependencies) if dependencies is not None else None
    choices = region_choices(datasets(), labels)
    if selected_region is not None and selected_region not in [item['selection'] for item in choices]:
        raise ValueError('Choose a region in the current saved measurements.')
    @contextmanager
    def source_cells(source):
        with _selected_cells(source, state_store=state_store) as dataset:
            yield filtered_cells(dataset, selected_region)
    sources = [lambda source=source: source_cells(source) for source,_,_ in members]
    active_image = (active[1].sample_id, active[1].project_entry_id, active[1].slide_id)
    plot_indices = ([index for index,(_source,measurement,_feature) in enumerate(members)
        if (measurement.sample_id, measurement.project_entry_id, measurement.slide_id) == active_image]
        if options.get('plot_scope') == 'image' else None)
    distribution, interval, counts = count_measurement_cohort(sources, feature.measurement, feature.units,
        lower=options.get('lower'), upper=options.get('upper'), plot_indices=plot_indices)
    images = {}
    for (source, measurement, _feature), count in zip(members, counts, strict=True):
        key = (measurement.sample_id, measurement.project_entry_id, measurement.slide_id)
        row = images.setdefault(key, dict(key=list(key), label=' · '.join(dict.fromkeys((key[0], key[2]))),
            artifact_refs=[], included_cells=0, matching_cells=0, missing_cells=0, nonfinite_cells=0,
            image_id=measurement.canonical_image_id, image_sha256=measurement.canonical_image_sha256))
        if (row['image_id'], row['image_sha256']) != (measurement.canonical_image_id, measurement.canonical_image_sha256):
            raise ValueError('The selected sections disagree on their canonical image frame.')
        row['artifact_refs'].append(source.artifact_ref.artifact_ref_sha256)
        for field in count:
            row[field] += count[field]
    resolved = {**asdict(distribution), 'interval': asdict(interval) if interval is not None else None,
        'feature': {**{name:getattr(feature,name) for name in
            ('measurement','kind','compartment','channel_id','biomarker','units')},
            'image_channel_index': next((i for i,c in enumerate(active[1].canonical_channels)
                                       if c['channel_id'] == feature.channel_id), None)},
        'examples': [], 'cohort_images': list(images.values()), 'region_choices': choices, 'region': selected_region,
        'cohort_artifact_refs': list(options['cohort_artifact_refs']), 'plot_scope': options.get('plot_scope','all')}
    region = next((item for item in choices if item['selection'] == selected_region), None) if selected_region is not None else None
    region_columns, region_values = (), ()
    if region is not None:
        import json
        region_columns = ('region_label', 'region_selection')
        region_values = (region['label'], json.dumps(selected_region, sort_keys=True))
    image_counts = _csv_output('measurement-image-counts', 'Image counts in selected interval',
        ('sample_id','project_entry_id','slide_id','measurement','units','lower','upper',
         'matching_cells','eligible_cells','matching_percent','missing_cells','nonfinite_cells','measurement_artifact_refs',*region_columns),
        [(*row['key'], feature.measurement, feature.units,
          interval.lower if interval else None, interval.upper if interval else None,
          row['matching_cells'], row['included_cells'],
          100*row['matching_cells']/row['included_cells'] if row['included_cells'] else None,
          row['missing_cells'], row['nonfinite_cells'], ';'.join(row['artifact_refs']), *region_values) for row in images.values()])
    if 'export_palette' in options:
        resolved['export_palette'] = options['export_palette']
    return (*render_measurement_distribution(distribution, interval=interval, region=region,
        export_palette=options.get('export_palette', 'default')), image_counts), resolved


def _compartment_labels(authority, snapshot, dependencies):
    """Reopen the selected sample's accepted primary class names for figure labels."""
    from .batch_artifacts import ArtifactOrigin
    from .batch_training.accepted_model_reuse import decode_imported_classifier_record_set
    from .batch_training.runtime import decode_current_committed_training_plan_set
    from mif_pipeline.pipeline_core.config import PrimaryClassifierSettings

    sample_id = authority.artifact_ref.owning_identity["sample_id"]
    sample = next(item for item in snapshot.manifest.enabled_samples if item.sample_id == sample_id)
    definitions = [sample.config.primary_classes] if sample.config.primary_classes is not None else []
    current = {item.artifact_ref.artifact_ref_sha256: item for item in snapshot.committed_artifact_authorities}
    accepted = {item.artifact_ref_sha256: item for item in snapshot.accepted_artifacts}
    imported = {}
    selected_plan_hashes = set()
    for reference in authority.artifact_ref.owning_identity["segmentation_ref_sha256s"]:
        segmentation = current[reference].artifact_ref.owning_identity
        for primary_reference in segmentation["primary_inference_result_ref_sha256s"]:
            primary = current[primary_reference].artifact_ref.owning_identity
            record = current.get(primary["classifier_record_set_ref_sha256"])
            if record is not None:
                selected_plan_hashes.add(record.artifact_ref.owning_identity["source_training_plan_set_sha256"])
            else:
                # Saved-model imports are admitted external inputs, not
                # internally committed classifier-record authorities.
                reference = primary["classifier_record_set_ref_sha256"]
                if reference not in imported:
                    ref = accepted.get(reference)
                    if ref is None or ref.origin is not ArtifactOrigin.MANIFEST_EXTERNAL:
                        raise ValueError("The classifier used for these compartments is unavailable.")
                    store = dependencies.runtime.state_store
                    external = store.authenticate_available_manifest_external_artifact(ref)
                    imported[reference] = decode_imported_classifier_record_set(external, state_store=store)
                material = imported[reference]
                if (material.primary_settings is None or not any(
                        item.classifier_record_sha256 == primary["selected_classifier_record_sha256"]
                        for item in material.record_set.classifier_records)):
                    raise ValueError("The imported classifier differs from these compartments.")
                definitions.append(material.primary_settings.primary_classes)
    for source in snapshot.committed_artifact_authorities:
        ref = source.artifact_ref
        identity = ref.owning_identity
        if (ref.artifact_kind != "training_plan_set"
                or identity.get("classifier_kind") != "primary_compartment"
                or identity["training_plan_set_sha256"] not in selected_plan_hashes
                or sample_id not in identity["participant_ids"]):
            continue
        plan_set = decode_current_committed_training_plan_set(
            source, store=dependencies.training.training_plan_set_store,
            state_store=dependencies.runtime.state_store).plan_set
        if plan_set.saved_selection is not None:
            definitions.append(PrimaryClassifierSettings.from_mapping(
                plan_set.saved_selection.declaration.primary_settings).primary_classes)
        else:
            definitions.extend(plan.request.primary_settings.primary_classes
                               for plan in plan_set.plans if plan.request.primary_settings is not None)
    labels = {}
    for classes in definitions:
        for item in classes:
            if labels.setdefault(item.class_id, item.display_name) != item.display_name:
                raise ValueError("Saved primary compartment names disagree for this sample.")
    return labels


def render_cell_figure(authority, figure_id, snapshot, dependencies, *, figure_options=None):
    from mif_pipeline.cell_analysis.result_plots import (
        count_measurement_distribution, count_phenotype_composition, count_marker_profiles,
        render_measurement_distribution, render_phenotype_composition, render_marker_profiles,
    )

    export_palette = (figure_options or {}).get('export_palette', 'default')
    features = _measurements(authority, snapshot)
    with _selected_cells(authority) as dataset:
        if figure_id not in {choice.figure_id for choice in _choices(dataset, features)}:
            raise ValueError("This figure is not available for the selected cell result.")
        if figure_id.startswith("distribution/"):
            feature = next(item for item in features if "distribution/" + quote(item.measurement, safe="") == figure_id)
            result = count_measurement_distribution(dataset, feature.measurement, feature.units)
            return render_measurement_distribution(result, export_palette=export_palette)
        if figure_id.startswith("compartments/"):
            from mif_pipeline.cell_analysis.compartment_composition_plots import (
                count_compartment_composition, render_compartment_composition,
            )
            _prefix, row_page, column_page = figure_id.split("/")
            result = count_compartment_composition(
                dataset, _compartment_labels(authority, snapshot, dependencies),
                sample_id=authority.artifact_ref.owning_identity["sample_id"])
            return render_compartment_composition(result, row_page=int(row_page), column_page=int(column_page),
                                                  export_palette=export_palette)
        if figure_id.startswith("profiles/"):
            _prefix, group, page = figure_id.split("/")
            markers = _profile_groups(dataset, features)[int(group)]
            result = count_marker_profiles(dataset, tuple(marker.measurement for marker in markers), markers[0].units)
            return render_marker_profiles(result, page=int(page), export_palette=export_palette)
        result = count_phenotype_composition(dataset)
        return render_phenotype_composition(result, page=int(figure_id.split("/")[1]), export_palette=export_palette)
