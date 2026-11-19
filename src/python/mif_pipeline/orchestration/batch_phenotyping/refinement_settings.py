"""Selected-cluster settings carried by the existing stage revision."""
from dataclasses import dataclass
from collections.abc import Mapping

from mif_pipeline.cell_analysis.clustering import _validate_graph_configuration
from mif_pipeline.cell_analysis.clustering_methods import validate_method_settings, validate_method_seed


@dataclass(frozen=True, slots=True)
class ClusterRefitSettings:
    cluster_ids: tuple[str, ...] = ()
    method: str = 'phenograph'
    k: int = 15
    resolution: float = 1.
    seed: int = 0
    method_settings: Mapping | None = None
    restore_parent: bool = False

    def __post_init__(self):
        if (type(self.cluster_ids) is not tuple
                or any(type(value) is not str or not value.strip() for value in self.cluster_ids)
                or tuple(sorted(set(self.cluster_ids))) != self.cluster_ids):
            raise ValueError('Select distinct current clusters in their recorded order.')
        if self.method not in {'phenograph','flowsom'}:
            raise ValueError('Choose PhenoGraph or FlowSOM for the selected-cluster refit.')
        if type(self.restore_parent) is not bool or (self.restore_parent and self.cluster_ids):
            raise ValueError('Restoring parent memberships cannot also select a new subset.')
        _validate_graph_configuration(k=self.k,resolution=self.resolution,seed=self.seed)
        validate_method_seed(self.method,self.seed)
        object.__setattr__(self,'resolution',float(self.resolution))
        object.__setattr__(self,'method_settings',validate_method_settings(self.method,self.method_settings or {}))

    def to_mapping(self):
        return dict(cluster_ids=list(self.cluster_ids),method=self.method,k=self.k,resolution=self.resolution,
                    seed=self.seed,method_settings=dict(self.method_settings),restore_parent=self.restore_parent)

    @classmethod
    def from_mapping(cls,value):
        if not isinstance(value,Mapping) or set(value)!=set(cls.__dataclass_fields__):
            raise ValueError('Refit settings differ from the current selection contract.')
        if not isinstance(value['cluster_ids'],(list,tuple)):
            raise ValueError('Select current clusters for the refit.')
        return cls(**{**value,'cluster_ids':tuple(value['cluster_ids'])})
