"""

Reads frozen Nimbus marker scores using the existing image and cell identities.

Nimbus predicts expression from a marker image and its cell-instance mask. Its
per-cell score is an expression confidence, not a measured intensity or a
calibrated probability. We retain that score independently of the later score
cutoff and optional intensity distillation. A score file contains one marker
over one or more image sections; its first NDJSON record binds every section
to the canonical image and segmentation population and records the frozen
weights. Subsequent records identify cells by the existing five-part identity.

The reader aligns records to the requested cell order once. An explicit JSON
null becomes NaN (unknown); an absent cell is an incomplete result and fails.
It neither rematches coordinates nor substitutes intensity-derived scores.
The file checksum is calculated during the same read so a reviewed result can
be checked when applying it without rerunning Nimbus.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from math import isfinite
import os
from pathlib import Path
import re
import tempfile
from types import MappingProxyType

import numpy as np

NIMBUS_SCORES_SCHEMA = "mif-nimbus-scores/v1"
NIMBUS_SCORE_KIND = "nimbus_cell_expression_confidence"
NimbusScope = tuple[str, str, str, str]
NimbusCellIdentity = tuple[str, str, str, str, str]
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_LINE_BYTES = 1024 * 1024


# ==============================================================================
#                          Frozen per-cell score records
# ==============================================================================


class NimbusScoreError(ValueError):
    """Reports incomplete, changed or incorrectly associated Nimbus evidence."""


@dataclass(frozen=True, slots=True)
class NimbusScoreResult:
    """

    Retains scores aligned to the requested population and their source details.

    The one-dimensional read-only scores array contains finite [0,1] values or
    NaN for explicitly unknown predictions. Provenance includes the actual
    score-file checksum, model identifier, package version and weights checksum.
    No classification cutoff is part of the pretrained model's identity.
    """

    scores: np.ndarray
    provenance: Mapping[str, object]


def _text(value: object, name: str) -> str:
    """Requires an exact nonblank identifier, preserving case and punctuation."""

    if not isinstance(value, str) or not value or value != value.strip():
        raise NimbusScoreError(f"Nimbus {name} must be nonblank text without padding")
    return value


def _identity(value: object, length: int) -> tuple[str, ...]:
    """Checks a native scope/cell key without coercing labels or coordinates."""

    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise NimbusScoreError(f"Nimbus identity must contain {length} text fields")
    return tuple(_text(part, "identity field") for part in value)


def _score(value: object) -> float:
    """Converts explicit null to unknown and rejects malformed numerical scores."""

    if value is None:
        return float("nan")
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise NimbusScoreError("Nimbus score must be a number in [0,1] or null")
    result = float(value)
    if not isfinite(result) or not 0.0 <= result <= 1.0:
        raise NimbusScoreError("Nimbus score must be finite and in [0,1]; use null for unknown")
    return result


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Rejects repeated JSON fields instead of silently retaining the last one."""

    result = {}
    for key, value in pairs:
        if key in result:
            raise NimbusScoreError(f"Nimbus record contains duplicate field {key!r}")
        result[key] = value
    return result


def _header_details(header: object, marker: str) -> tuple[dict, dict]:
    """

    Checks the source/model declaration and builds one exact section lookup.

    Population and image identifiers belong to the section, not to individual
    score rows. Frozen inference settings may be included as additional header
    provenance; they are covered by the score-file checksum.
    """

    if not isinstance(header, dict) or header.get("schema_version") != NIMBUS_SCORES_SCHEMA:
        raise NimbusScoreError(f"Nimbus scores require schema {NIMBUS_SCORES_SCHEMA}")
    if header.get("record_type") != "header" or header.get("score_kind") != NIMBUS_SCORE_KIND:
        raise NimbusScoreError("Nimbus score header has an unsupported record or score kind")
    if header.get("marker") != marker:
        raise NimbusScoreError("Nimbus score marker differs from the selected marker")
    provenance = {
        key: _text(header.get(key), key)
        for key in ("model_id", "package_version", "weights_sha256")
    }
    if not _SHA256.fullmatch(provenance["weights_sha256"]):
        raise NimbusScoreError("Nimbus weights_sha256 must be a lowercase SHA-256")
    provenance.update(marker=marker, score_kind=NIMBUS_SCORE_KIND)
    if "inference" in header:
        if not isinstance(header["inference"], dict):
            raise NimbusScoreError("Nimbus inference provenance must be an object")
        provenance["inference"] = header["inference"]
    if not isinstance(header.get("scopes"), list):
        raise NimbusScoreError("Nimbus header must declare its image-section scopes")
    scopes = {}
    for scope in header["scopes"]:
        if not isinstance(scope, dict):
            raise NimbusScoreError("Nimbus scope must be an object")
        identity = _identity(scope.get("identity"), 4)
        if identity in scopes:
            raise NimbusScoreError("Nimbus header contains a duplicate scope")
        scopes[identity] = (
            _text(scope.get("segmentation_population_id"), "population"),
            _text(scope.get("canonical_image_id"), "image"),
        )
    return scopes, provenance


def load_nimbus_scores(
    path: str | Path,
    *,
    expected_identities: Sequence[NimbusCellIdentity],
    expected_population_by_scope: Mapping[NimbusScope, str],
    expected_image_by_scope: Mapping[NimbusScope, str],
    marker: str,
    allow_superset: bool = False,
    expected_sha256: str | None = None,
) -> NimbusScoreResult:
    """

    Reads one marker's scores and aligns them to the exact requested cell order.

    Every expected cell must occur exactly once and its header scope must match
    the selected segmentation population and canonical image. Extra cells are
    rejected unless allow_superset is explicitly selected for applying part of
    an already-reviewed aggregate. Even then all rows are checked for duplicates,
    declared scopes and valid scores. expected_sha256 binds application to the
    file reviewed earlier. No partial array is returned on any failure.

    Raises NimbusScoreError for malformed, duplicate, missing, foreign or stale
    evidence, and OSError for an unreadable file. Only one file pass is made;
    memory consists of the requested scores and cell identity indexes.
    """

    marker = _text(marker, "marker")
    identities = [_identity(value, 5) for value in expected_identities]
    index = {identity: row for row, identity in enumerate(identities)}
    if len(index) != len(identities):
        raise NimbusScoreError("Requested Nimbus population contains duplicate cell identities")
    requested_scopes = {identity[:4] for identity in identities}
    scores = np.full(len(identities), np.nan, dtype=np.float64)
    seen: set[tuple[str, ...]] = set()
    matched = 0
    digest = hashlib.sha256()
    path = Path(path)
    with path.open("rb") as stream:
        def read_record() -> object | None:
            """Reads and hashes a bounded complete line from the opened file."""
            line = stream.readline(_MAX_LINE_BYTES + 1)
            if not line:
                return None
            if len(line) > _MAX_LINE_BYTES:
                raise NimbusScoreError("Nimbus score record exceeds one megabyte")
            digest.update(line)
            try:
                record = json.loads(line, object_pairs_hook=_unique_object)
            except (ValueError, UnicodeError) as exc:
                raise NimbusScoreError(f"Cannot read Nimbus JSON record: {exc}") from exc
            if not isinstance(record, dict):
                raise NimbusScoreError("Nimbus score records must be JSON objects")
            return record

        scopes, provenance = _header_details(read_record(), marker)
        for scope in requested_scopes:
            if scope not in scopes:
                raise NimbusScoreError(f"Nimbus scores are missing requested scope {scope!r}")
            population, image = scopes[scope]
            if population != expected_population_by_scope.get(scope):
                raise NimbusScoreError(f"Nimbus segmentation population changed for {scope!r}")
            if image != expected_image_by_scope.get(scope):
                raise NimbusScoreError(f"Nimbus canonical image changed for {scope!r}")
        if not allow_superset and set(scopes) != requested_scopes:
            raise NimbusScoreError("Nimbus scores contain unexpected image-section scopes")
        while (record := read_record()) is not None:
            if set(record) != {"cell_identity", "score"}:
                raise NimbusScoreError("Nimbus score row requires cell_identity and score")
            identity = _identity(record["cell_identity"], 5)
            if identity in seen:
                raise NimbusScoreError(f"Nimbus scores contain duplicate cell {identity!r}")
            seen.add(identity)
            if identity[:4] not in scopes:
                raise NimbusScoreError("Nimbus row refers to an undeclared image-section scope")
            score = _score(record["score"])
            row = index.get(identity)
            if row is None:
                if not allow_superset:
                    raise NimbusScoreError(f"Nimbus scores contain unexpected cell {identity!r}")
                continue
            scores[row] = score
            matched += 1
    if matched != len(identities):
        raise NimbusScoreError(f"Nimbus scores are missing {len(identities) - matched} requested cells")
    actual_sha256 = digest.hexdigest()
    if expected_sha256 is not None and actual_sha256 != expected_sha256:
        raise NimbusScoreError("Nimbus score file changed after review; review the new scores")
    provenance.update(path=str(path.resolve()), sha256=actual_sha256, row_count=len(seen))
    scores.flags.writeable = False
    return NimbusScoreResult(scores, MappingProxyType(provenance))


def write_nimbus_scores(
    path: str | Path,
    *,
    marker: str,
    identities: Sequence[NimbusCellIdentity],
    scores: Sequence[float | None],
    population_by_scope: Mapping[NimbusScope, str],
    image_by_scope: Mapping[NimbusScope, str],
    provenance: Mapping[str, object],
) -> Path:
    """

    Publishes a new identity-bound score file without replacing prior evidence.

    Callers supply the explicit segmentation-label to native-cell association;
    this function never derives cell identity from centroids or row positions.
    NaN and None are written as JSON null, while infinities and out-of-range
    scores fail. A temporary file is closed and synchronized before its completed
    contents are linked to the unused destination; failures remove the temporary
    file and preserve any existing result.
    """

    marker = _text(marker, "marker")
    identities = [_identity(value, 5) for value in identities]
    if len(set(identities)) != len(identities):
        raise NimbusScoreError("Nimbus output contains duplicate cell identities")
    if len(identities) != len(scores):
        raise NimbusScoreError("Nimbus output cell and score counts differ")
    header = {
        "schema_version": NIMBUS_SCORES_SCHEMA,
        "record_type": "header",
        "score_kind": NIMBUS_SCORE_KIND,
        "marker": marker,
        **{key: provenance.get(key) for key in ("model_id", "package_version", "weights_sha256")},
        "scopes": [
            {"identity": scope, "segmentation_population_id": population_by_scope.get(scope),
             "canonical_image_id": image_by_scope.get(scope)}
            for scope in sorted({identity[:4] for identity in identities})
        ],
    }
    if "inference" in provenance:
        header["inference"] = provenance["inference"]
    _header_details(header, marker)
    path = Path(path)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, prefix=f".{path.name}.",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(header, allow_nan=False, separators=(",", ":")) + "\n")
            for identity, value in zip(identities, scores):
                if value is None or (isinstance(value, (float, np.floating)) and np.isnan(value)):
                    score = None
                else:
                    score = _score(value.item() if isinstance(value, np.generic) else value)
                stream.write(json.dumps({"cell_identity": identity, "score": score},
                                        allow_nan=False, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path

