"""Prepare explicitly described novel markers through the downloaded KRONOS API.

The base CSV supplies existing categories and normalization. Authored novel rows
carry biological descriptions; fitting supplies their measured statistics and
provenance before preparation. Existing resolved rows retain their values. Preparation uses a pinned
local BioLinkBERT snapshot on CPU, then the official marker builder; completed
native encoders retain those buffers and need neither the text model nor this
preparation step during image inference.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from collections.abc import Mapping
import gc
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import tempfile

TEXT_REPOSITORY = "michiyasunaga/BioLinkBERT-large"
TEXT_FILES = (
    "config.json", "pytorch_model.bin", "special_tokens_map.json",
    "tokenizer.json", "tokenizer_config.json", "vocab.txt",
)


def marker_key(name: str) -> str:
    """Match marker spellings with the existing vocabulary convention.

    Convert letters to lowercase, alpha to a, and remove the supported separators:
    spaces, hyphens, underscores, colons, parentheses, slashes and periods.
    PD-L1 and PDL1 therefore share one key; selection rejects two rows with that
    same key rather than choosing an ambiguous identity.
    """
    table = str.maketrans({"-": "_", " ": "_", ":": "_", "α": "a",
                          "(": "_", ")": "_", "/": ""})
    return name.lower().translate(table).strip().replace("_", "").replace(".", "")


@dataclass(frozen=True, slots=True)
class KronosAdditionalMarkerDefinition:
    """Authored biological identity awaiting training-only normalization."""
    marker_name: str
    marker_full_name: str
    compartment: str
    family: str
    family_desc: str

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if type(value) is not str or not value.strip() or "\0" in value or len(value) > 4096:
                raise ValueError(f"Additional marker {name} requires explicit nonempty text")
        if not marker_key(self.marker_name):
            raise ValueError("Additional marker identity is empty after separator normalization")

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, Mapping) or set(value) != set(cls.__dataclass_fields__):
            raise ValueError("Pending additional marker metadata requires its exact five biological fields")
        return cls(**value)

    def to_mapping(self):
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


def additional_marker_from_mapping(value):
    """Admit only the closed authored or resolved record; never invent statistics."""
    if isinstance(value, Mapping) and set(value) == set(KronosAdditionalMarkerDefinition.__dataclass_fields__):
        return KronosAdditionalMarkerDefinition.from_mapping(value)
    return KronosAdditionalMarker.from_mapping(value)


def has_pending_marker_statistics(settings):
    """Inspect a checked encoder, source/configuration, or ordered marker rows."""
    if isinstance(settings, Mapping):
        if "encoder" in settings:
            return has_pending_marker_statistics(settings["encoder"])
        rows = settings.get("additional_markers", ())
    elif hasattr(settings, "encoder"):
        return has_pending_marker_statistics(settings.encoder)
    else:
        rows = getattr(settings, "additional_markers", settings)
    if not isinstance(rows, (tuple, list)):
        raise ValueError("Additional markers must be an ordered array")
    checked = tuple(additional_marker_from_mapping(row) if isinstance(row, Mapping) else row for row in rows)
    if any(type(row) not in (KronosAdditionalMarkerDefinition, KronosAdditionalMarker) for row in checked):
        raise ValueError("Additional markers require checked authored or resolved metadata")
    return any(type(row) is KronosAdditionalMarkerDefinition for row in checked)


@dataclass(frozen=True, slots=True)
class KronosAdditionalMarker:
    """One novel biological identity and its declared reference normalization."""
    marker_name: str
    marker_full_name: str
    compartment: str
    family: str
    family_desc: str
    mean: float
    std: float
    statistics_source: str

    def __post_init__(self):
        for name in ("marker_name", "marker_full_name", "compartment", "family",
                     "family_desc", "statistics_source"):
            value = getattr(self, name)
            if type(value) is not str or not value.strip() or "\0" in value or len(value) > 4096:
                raise ValueError(f"Additional marker {name} requires explicit nonempty text")
        if not marker_key(self.marker_name):
            raise ValueError("Additional marker identity is empty after separator normalization")
        for name in ("mean", "std"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f"Additional marker {name} must be a finite number")
        if self.std <= 0:
            raise ValueError("Additional marker std must be positive")
        for name in ("mean", "std"):
            object.__setattr__(self, name, float(getattr(self, name)))

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, Mapping) or set(value) != set(cls.__dataclass_fields__):
            raise ValueError("Additional marker metadata requires its exact eight fields")
        return cls(**value)

    def to_mapping(self):
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class KronosMarkerTextModel:
    """Saved local helper identity; inspected instances also retain file facts."""
    directory: str
    repository: str
    revision: str
    sha256: str
    _members: tuple = field(default=(), init=False, repr=False, compare=False)

    def __post_init__(self):
        if (type(self.directory) is not str or "\0" in self.directory
                or not (PurePosixPath(self.directory).is_absolute()
                        or PureWindowsPath(self.directory).is_absolute())):
            raise ValueError("Select an absolute local BioLinkBERT-large snapshot directory")
        if self.repository != TEXT_REPOSITORY:
            raise ValueError("Additional markers require the official BioLinkBERT-large model")
        for name, length in (("revision", 40), ("sha256", 64)):
            value = getattr(self, name)
            if type(value) is not str or len(value) != length or any(c not in "0123456789abcdef" for c in value):
                raise ValueError(f"BioLinkBERT {name} must be its complete lowercase content identity")

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, Mapping) or set(value) != {"directory", "repository", "revision", "sha256"}:
            raise ValueError("BioLinkBERT selection requires directory, repository, revision and sha256")
        return cls(**value)

    def to_mapping(self):
        return {name: getattr(self, name) for name in ("directory", "repository", "revision", "sha256")}

    def verify_current(self):
        """Check the inspected helper around its one preparation load."""
        if not self._members:
            raise ValueError("Inspect the selected BioLinkBERT files before preparation")
        for name, identity in self._members:
            path = Path(self.directory) / name
            if path.is_symlink() or not path.is_file() or _file_identity(path) != identity:
                raise ValueError("The selected BioLinkBERT snapshot changed: " + name)


def _file_identity(path):
    stat = path.stat()
    return (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_dev, stat.st_ino)


def inspect_marker_text_model(directory: str | Path, *, revision: str) -> KronosMarkerTextModel:
    """Inspect the official local snapshot without importing or downloading a model.

    The selected source revision accompanies a digest of the exact configuration,
    tokenizer and weight bytes. Require ordinary local files so later preparation
    cannot follow a global-cache link to a different snapshot.
    """
    root = Path(directory)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ValueError("Prepare/select an existing absolute BioLinkBERT-large directory in Software setup")
    # Transformers can prefer an additional safetensors/adaptor/tokenizer file.
    # Admit this official snapshot's complete loading inventory, so no unhashed
    # alternative silently replaces the recorded PyTorch weights or tokenizer.
    allowed = set(TEXT_FILES) | {"README.md", ".gitattributes"}
    if any(path.name not in allowed for path in root.iterdir()):
        raise ValueError("Select a clean BioLinkBERT-large snapshot with its official loading files")
    members, digests = [], []
    for name in TEXT_FILES:
        path = root / name
        limit = 2 * 1024**3 if name == "pytorch_model.bin" else 32 * 1024**2
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= limit:
            raise ValueError("BioLinkBERT snapshot file is missing, linked or exceeds its bound: " + name)
        before = _file_identity(path)
        with path.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if _file_identity(path) != before:
            raise ValueError("BioLinkBERT snapshot changed during inspection: " + name)
        members.append((name, before))
        digests.append((name, digest))
    config = json.loads((root / "config.json").read_bytes())
    if config.get("model_type") != "bert" or config.get("hidden_size") != 1024:
        raise ValueError("Select the official 1024-feature BioLinkBERT-large snapshot")
    digest = hashlib.sha256(json.dumps(digests, separators=(",", ":")).encode()).hexdigest()
    result = KronosMarkerTextModel(str(root), TEXT_REPOSITORY, revision, digest)
    object.__setattr__(result, "_members", tuple(members))
    result.verify_current()
    return result


def marker_vocabulary(raw: bytes):
    """Read the bounded base vocabulary once for both selection and categories."""
    if type(raw) is not bytes or len(raw) > 4 * 1024**2:
        raise ValueError("Marker vocabulary exceeds the bounded CSV limit")
    index, compartments, families = {}, set(), set()
    descriptions = {}
    for row in csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))):
        name = row.get("marker_name", "").strip().lower()
        key = marker_key(name)
        if not key or key in index:
            raise ValueError("Unnamed or ambiguous marker vocabulary identity: " + key)
        mean, std = float(row["mean"]), float(row["std"])
        if not math.isfinite(mean) or not math.isfinite(std) or std <= 0:
            raise ValueError("Marker statistics must be finite with positive standard deviations")
        index[key] = (name, mean, std)
        compartments.add(row["compartment"])
        families.add(row["family"])
        description = row.get("family_desc", "").strip()
        if description:
            descriptions.setdefault(row["family"], set()).add(description)
    return {"marker_count": len(index), "markers": index,
            "compartments": tuple(sorted(compartments)), "families": tuple(sorted(families)),
            "family_descriptions": {
                family: next(iter(descriptions[family])) if len(descriptions.get(family, ())) == 1
                else family.replace("_", " ") for family in sorted(families)}}


def validate_marker_selection(raw, marker_names, nuclear_channel, additional_markers):
    """Validate ordered biological selection without inventing normalization."""
    if any(type(name) is not str or not name.strip() for name in marker_names):
        raise ValueError("Select explicit nonempty marker names")
    vocabulary = marker_vocabulary(raw)
    index = vocabulary["markers"]
    selected_keys = tuple("dapi" if i == nuclear_channel else marker_key(name)
                          for i, name in enumerate(marker_names))
    if len(set(selected_keys)) != len(selected_keys):
        raise ValueError("Selected marker names have duplicate separator-normalized identities")
    if type(additional_markers) is not tuple or any(type(row) not in (KronosAdditionalMarker, KronosAdditionalMarkerDefinition) for row in additional_markers):
        raise ValueError("Additional markers must be ordered checked metadata rows")
    novel_keys = tuple(marker_key(row.marker_name) for row in additional_markers)
    if len(set(novel_keys)) != len(novel_keys):
        raise ValueError("Additional marker names have duplicate normalized identities")
    for row, key in zip(additional_markers, novel_keys):
        if key in index:
            raise ValueError(f"Marker {row.marker_name!r} is already in the base vocabulary; use its saved statistics")
        if row.compartment not in vocabulary["compartments"] or row.family not in vocabulary["families"]:
            raise ValueError(f"Marker {row.marker_name!r} must use an existing compartment and family")
    unknown = tuple(key for key in selected_keys if key not in index)
    if unknown != novel_keys:
        raise ValueError("Supply metadata for every unknown selected marker in channel order; omit unselected rows")
    return vocabulary, selected_keys, novel_keys


def resolve_marker_selection(raw, marker_names, nuclear_channel, additional_markers):
    """Runtime resolution accepts measured rows only, including legacy provenance."""
    vocabulary, selected_keys, novel_keys = validate_marker_selection(
        raw, marker_names, nuclear_channel, additional_markers)
    if has_pending_marker_statistics(additional_markers):
        raise ValueError("Resolve additional-marker statistics from this fit's training subjects before opening the model")
    index = vocabulary["markers"]
    for row, key in zip(additional_markers, novel_keys):
        index[key] = (row.marker_name.lower(), row.mean, row.std)
    return tuple(index[key] for key in selected_keys)


def prepare_additional_markers(backbone, rows, local_text_model, owned_directory, *, base_csv):
    """Build ordered text embeddings, then publish the official returned buffers.

    Only the freshly generated matrix in this private temporary directory can be
    consumed by the official builder. The helper is released before registration
    returns, while the encoder retains the extended buffers for export. This does
    not initialize the permissive upstream wrapper or load another backbone.
    """
    import numpy as np
    import torch
    from dinov2.models.markers.builder import build_prompt
    from dinov2.models.markers.marker_text import BioLinkBERTTextEncoder

    if owned_directory is None:
        raise ValueError("Additional-marker preparation requires its owned local worker directory")
    # i) Bind the local helper and create private preparation inputs.
    selected = inspect_marker_text_model(local_text_model.directory, revision=local_text_model.revision)
    if selected != local_text_model:
        raise ValueError("The selected BioLinkBERT snapshot differs from its saved identity; correct Software setup")
    directory = Path(owned_directory)
    if not directory.is_absolute() or directory.is_symlink() or not directory.is_dir():
        raise ValueError("Additional-marker preparation requires its owned local worker directory")
    staging = Path(tempfile.mkdtemp(prefix=".additional-markers-", dir=directory))
    builder = backbone.marker_builder
    original_cache = builder.cache_dir
    text_encoder = None
    try:
        fields = tuple(KronosAdditionalMarker.__dataclass_fields__)
        additional_csv = staging / "markers.csv"
        with additional_csv.open("x", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            writer.writerows(row.to_mapping() for row in rows)
        prompts = [build_prompt(row.marker_name, row.marker_full_name, row.compartment, row.family_desc)
                   for row in rows]
        # ii) Use official prompts and CPU text pooling, then release the helper.
        text_encoder = BioLinkBERTTextEncoder(model_id=selected.directory,
                                             device=torch.device("cpu"), fp16_on_cuda=False)
        # One prompt at a time bounds attention scratch independently of the
        # number of novel markers; the official pooling/normalization is retained.
        with torch.inference_mode():
            matrix = torch.cat([text_encoder([prompt]).detach().cpu().float() for prompt in prompts])
        if tuple(matrix.shape) != (len(rows), 1024) or not bool(matrix.isfinite().all()):
            raise ValueError("BioLinkBERT returned invalid additional-marker text embeddings")
        selected.verify_current()
        text_encoder = None
        gc.collect()
        np.save(staging / "additional_marker_text_embeddings.npy", matrix.numpy(), allow_pickle=False)
        # iii) Register only this ordered fresh matrix and publish returned buffers.
        builder.cache_dir = staging
        weights = {name: getattr(builder, name) for name in
                   ("compIdPerMarker", "famIdPerMarker", "precomputedMarkerTextEmb")}
        comp, family, text, indices = builder.update_markers_dynamic(base_csv, additional_csv, weights=weights)
        # These are the same buffer/index assignments used by the official
        # Kronos2Model.register_additional_markers operation.
        builder.compIdPerMarker = comp
        builder.famIdPerMarker = family
        builder.precomputedMarkerTextEmb = text
        builder.markerNameToIndex.update(indices)
    finally:
        builder.cache_dir = original_cache
        text_encoder = None
        shutil.rmtree(staging)
