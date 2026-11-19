"""An explicit assignment cutoff bound to a target processing group and fitted model."""
from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
import re


@dataclass(frozen=True, slots=True)
class SupervisedCutoffSelection:
    scope: str
    processing_group_id: str
    image_identity: tuple[str, str, str] | None
    classifier_record_sha256: str
    cutoff: float
    reviewer_id: str

    def __post_init__(self):
        if self.scope not in {'image', 'group'}:
            raise ValueError('Choose an image or processing group for assignment.')
        if any(not isinstance(value, str) or not value.strip()
               for value in (self.processing_group_id, self.reviewer_id)):
            raise ValueError('The cutoff requires its processing group and reviewer.')
        if self.scope == 'image':
            if (type(self.image_identity) is not tuple or len(self.image_identity) != 3
                    or any(not isinstance(value, str) or not value.strip() for value in self.image_identity)):
                raise ValueError('An image cutoff requires its complete image identity.')
        elif self.image_identity is not None:
            raise ValueError('A processing-group cutoff must not select one image.')
        if (type(self.classifier_record_sha256) is not str
                or re.fullmatch('[0-9a-f]{64}', self.classifier_record_sha256) is None):
            raise ValueError('The cutoff requires its exact fitted classifier record.')
        if (isinstance(self.cutoff, bool) or not isinstance(self.cutoff, (int, float))
                or not isfinite(self.cutoff) or not 0 <= self.cutoff <= 1):
            raise ValueError('The assignment cutoff must be between zero and one.')
        object.__setattr__(self, 'cutoff', float(self.cutoff))

    @property
    def sort_key(self):
        return (self.processing_group_id, self.scope, self.image_identity or (), self.classifier_record_sha256)

    def to_mapping(self):
        return dict(scope=self.scope, processing_group_id=self.processing_group_id,
            image_identity=list(self.image_identity) if self.image_identity is not None else None,
            classifier_record_sha256=self.classifier_record_sha256, cutoff=self.cutoff, reviewer_id=self.reviewer_id)

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, Mapping) or set(value) != {
                'scope','processing_group_id','image_identity','classifier_record_sha256','cutoff','reviewer_id'}:
            raise ValueError('The saved assignment cutoff fields are invalid.')
        values = dict(value)
        if values['image_identity'] is not None:
            if not isinstance(values['image_identity'], (list, tuple)):
                raise ValueError('The saved image identity is invalid.')
            values['image_identity'] = tuple(values['image_identity'])
        return cls(**values)


def selected_cutoff(selections, processing_group_id, image_identity, classifier_record_sha256):
    """Image choices override their group; a changed model never inherits a cutoff."""
    matching = [item for item in selections if item.processing_group_id == processing_group_id
                and item.classifier_record_sha256 == classifier_record_sha256
                and (item.scope == 'group' or item.image_identity == tuple(image_identity))]
    images = [item for item in matching if item.scope == 'image']
    if len(images) > 1 or len([item for item in matching if item.scope == 'group']) > 1:
        raise ValueError('The current plan has competing assignment cutoffs.')
    return images[0] if images else matching[0] if matching else None


def merge_selection(selections, selection):
    """An explicit group assignment replaces its previous image overrides."""
    kept = [item for item in selections if item.processing_group_id != selection.processing_group_id
            or (selection.scope == 'image' and
                (item.scope == 'group' or item.image_identity != selection.image_identity))]
    return tuple(sorted((*kept, selection), key=lambda item: item.sort_key))
