"""Shared stage contracts, imported only when their owning operation needs them.

Small filesystem/model workers can use a core utility without importing
unrelated analysis configuration, stage validation and lifecycle contracts.
"""

from importlib import import_module

_EXPORTS = {
    'AnalysisConfig': ('config', 'AnalysisConfig'),
    'AnalysisDomainConfig': ('config', 'AnalysisDomainConfig'),
    'AnalysisDomainKind': ('domain', 'AnalysisDomainKind'),
    'AnalysisDomainRef': ('domain', 'AnalysisDomainRef'),
    'CellRecord': ('models', 'CellRecord'),
    'ChannelSpec': ('config', 'ChannelSpec'),
    'ClassifierMode': ('config', 'ClassifierMode'),
    'ClassifierSpec': ('config', 'ClassifierSpec'),
    'ClusteringConfig': ('config', 'ClusteringConfig'),
    'ClusteringMode': ('config', 'ClusteringMode'),
    'MovingAssistanceScope': ('config', 'MovingAssistanceScope'),
    'NativeLevelSelectionMode': ('config', 'NativeLevelSelectionMode'),
    'PhenotypeClassSpec': ('config', 'PhenotypeClassSpec'),
    'InteractionAction': ('stage_result', 'InteractionAction'),
    'INTERACTION_ACTION_SCHEMA_VERSION': ('stage_result', 'INTERACTION_ACTION_SCHEMA_VERSION'),
    'InteractionReason': ('stage_result', 'InteractionReason'),
    'PhenotypingConfig': ('config', 'PhenotypingConfig'),
    'PhenotypingMode': ('config', 'PhenotypingMode'),
    'PrimaryClassSpec': ('config', 'PrimaryClassSpec'),
    'ProjectPreparationError': ('errors', 'ProjectPreparationError'),
    'ROIAnalysisError': ('errors', 'ROIAnalysisError'),
    'ROIRecord': ('models', 'ROIRecord'),
    'RegistrationConfig': ('config', 'RegistrationConfig'),
    'ReviewPolicy': ('config', 'ReviewPolicy'),
    'ReviewStatus': ('models', 'ReviewStatus'),
    'RuntimeCompositionBinding': ('runtime_lifecycle', 'RuntimeCompositionBinding'),
    'RuntimeLifecycleBorrower': ('runtime_lifecycle', 'RuntimeLifecycleBorrower'),
    'RuntimeLifecycleError': ('runtime_lifecycle', 'RuntimeLifecycleError'),
    'RuntimeLifecycleOwner': ('runtime_lifecycle', 'RuntimeLifecycleOwner'),
    'RuntimeLifecycleState': ('runtime_lifecycle', 'RuntimeLifecycleState'),
    'RuntimeTeardownPermit': ('runtime_lifecycle', 'RuntimeTeardownPermit'),
    'RuntimeUseKind': ('runtime_lifecycle', 'RuntimeUseKind'),
    'RuntimeUseToken': ('runtime_lifecycle', 'RuntimeUseToken'),
    'SecondaryExcessPolicy': ('config', 'SecondaryExcessPolicy'),
    'SecondaryStructureSpec': ('config', 'SecondaryStructureSpec'),
    'SegmentationConfig': ('config', 'SegmentationConfig'),
    'SemanticConfig': ('config', 'SemanticConfig'),
    'SemanticProfileReference': ('config', 'SemanticProfileReference'),
    'SemanticStatus': ('models', 'SemanticStatus'),
    'SourceKind': ('models', 'SourceKind'),
    'StageResult': ('stage_result', 'StageResult'),
    'StageStatus': ('stage_result', 'StageStatus'),
    'WorkflowCompletionOutcome': ('stage_result', 'WorkflowCompletionOutcome'),
    'TissueDetectionConfig': ('config', 'TissueDetectionConfig'),
    'TissueMatchingConfig': ('config', 'TissueMatchingConfig'),
    'TissueSection': ('models', 'TissueSection'),
    'StructureCountBounds': ('config', 'StructureCountBounds'),
    'ReleaseCompatibilityManifest': ('schema_registry', 'ReleaseCompatibilityManifest'),
    'SchemaCompatibilityError': ('schema_registry', 'SchemaCompatibilityError'),
    'assert_current_writer': ('schema_registry', 'assert_current_writer'),
    'analysis_domain_id': ('domain', 'analysis_domain_id'),
    'next_phenotyping_interaction': ('interactions', 'next_phenotyping_interaction'),
    'requires_generation_manifest': ('stage_result', '_requires_generation_manifest'),
    'validate_interaction_actions': ('stage_result', 'validate_interaction_actions'),
    'validate_interaction_response': ('interactions', 'validate_interaction_response'),
    'validate_review_action': ('stage_result', 'validate_review_action'),
    'validate_safe_path_segment': ('errors', 'validate_safe_path_segment'),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> object:
    """Resolve and retain one current public contract from its owning module."""
    try:
        module, attribute = _EXPORTS[name]
    except KeyError:
        raise AttributeError(name) from None
    value = getattr(import_module(f"{__name__}.{module}"), attribute)
    globals()[name] = value
    return value
