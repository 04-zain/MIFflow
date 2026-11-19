"""Default feature choices use explicit component identities, not guessed cell biology."""

from collections import Counter
from collections.abc import Mapping


# ==============================================================================
#                                Feature channels
# ==============================================================================


def default_feature_channels(channels, *, retained_ids=()):
    retained = set(retained_ids)

    def keep(channel):
        if channel.channel_id in retained:
            return True
        # These are explicit registered AF identities; unknown biomarkers are not treated as AF.
        return not (
            channel.component in {"AF", "Sample AF", "Autofluorescence"}
            and channel.biomarker is None
        )

    return tuple(channel for channel in channels if keep(channel))


def channel_choice_labels(channels):
    """Display saved channel identities consistently in classifier selectors."""
    labels = {}
    for channel in channels:
        field = channel.get if isinstance(channel, Mapping) else lambda key: getattr(channel, key, None)
        identifier = field("channel_id")
        labels[identifier] = " · ".join(dict.fromkeys(part for part in (
            field("name") or field("biomarker") or field("component") or field("fluorophore") or identifier,
            field("component") or field("fluorophore"),
        ) if part))
    counts = Counter(labels.values())
    return {identifier: f"{label} ({identifier})" if counts[label] > 1 else label
            for identifier, label in labels.items()}
