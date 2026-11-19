"""Public contracts loaded on demand so numerical workers stay independent.

Importing an individual encoder or score module must not initialize QuPath,
image readers, plotting or unrelated scientific backends. Public names retain
exactly their original owning modules and are cached on first access.
"""
from importlib import import_module
from typing import TYPE_CHECKING

# Static imports keep type checkers and frozen-build module analysis aware of
# the public API without importing unrelated runtime backends eagerly.
if TYPE_CHECKING:
    from .classifiers import (
        ChannelMappingError,
        ClassifierCompatibilityError,
        ClassifierDefinition,
        ClassifierRegistry,
        TrainingEvidenceSnapshot,
        resolve_channel_roles,
        validate_primary_classifier,
    )
    from .count_qc import (
        DeterministicSelectionLineage,
        DomainExclusion,
        DomainExclusionStatus,
        InclusionProbabilityStatus,
        InventoryDisposition,
        ManualSelectionLineage,
        ObjectInventoryItem,
        OutputViolation,
        PrimaryCountOutput,
        PrimaryCountDecision,
        OutputStatus,
        SelectionAlgorithm,
        SelectionContext,
        StructureCountDecision,
        StructureCountObservation,
        evaluate_final_secondary_counts,
        evaluate_primary_counts,
        select_capped_secondary,
    )

__all__ = [
    'ChannelMappingError',
    'ClassifierCompatibilityError',
    'ClassifierDefinition',
    'ClassifierRegistry',
    'TrainingEvidenceSnapshot',
    'DeterministicSelectionLineage',
    'DomainExclusion',
    'DomainExclusionStatus',
    'InclusionProbabilityStatus',
    'InventoryDisposition',
    'ManualSelectionLineage',
    'ObjectInventoryItem',
    'OutputViolation',
    'PrimaryCountOutput',
    'PrimaryCountDecision',
    'OutputStatus',
    'SelectionAlgorithm',
    'SelectionContext',
    'StructureCountDecision',
    'StructureCountObservation',
    'evaluate_final_secondary_counts',
    'evaluate_primary_counts',
    'resolve_channel_roles',
    'select_capped_secondary',
    'validate_primary_classifier',
]

_EXPORT_MODULES = {
    'ChannelMappingError': '.classifiers',
    'ClassifierCompatibilityError': '.classifiers',
    'ClassifierDefinition': '.classifiers',
    'ClassifierRegistry': '.classifiers',
    'TrainingEvidenceSnapshot': '.classifiers',
    'DeterministicSelectionLineage': '.count_qc',
    'DomainExclusion': '.count_qc',
    'DomainExclusionStatus': '.count_qc',
    'InclusionProbabilityStatus': '.count_qc',
    'InventoryDisposition': '.count_qc',
    'ManualSelectionLineage': '.count_qc',
    'ObjectInventoryItem': '.count_qc',
    'OutputViolation': '.count_qc',
    'PrimaryCountOutput': '.count_qc',
    'PrimaryCountDecision': '.count_qc',
    'OutputStatus': '.count_qc',
    'SelectionAlgorithm': '.count_qc',
    'SelectionContext': '.count_qc',
    'StructureCountDecision': '.count_qc',
    'StructureCountObservation': '.count_qc',
    'evaluate_final_secondary_counts': '.count_qc',
    'evaluate_primary_counts': '.count_qc',
    'resolve_channel_roles': '.classifiers',
    'select_capped_secondary': '.count_qc',
    'validate_primary_classifier': '.classifiers',
}

def __getattr__(name):
    """Resolve a public export from its unchanged implementation module."""
    module = _EXPORT_MODULES.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value


def __dir__():
    """Advertise the same public names before and after their first import."""
    return sorted(set(globals()) | set(__all__))
