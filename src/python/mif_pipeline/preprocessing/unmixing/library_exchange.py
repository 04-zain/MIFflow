"""Portable reviewed-library exchange with exact native evidence and L1/L2 CSV views.

CSV views improve interoperability; they do not invent the provenance needed for an
arbitrary external matrix to become an accepted empirical reference library.

The functions retain the selected spectral basis, component order and reviewed acquisition
facts through the operation. Numerical work is bounded by the configured pixel blocks, while
published references and results keep their input and settings identities."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
import zipfile
from .reference_models import FluorophoreReferenceLibrary
from .reference_catalogue import SharedReferenceCatalogue, dumps_reference_source, loads_reference_source

ReferenceLibrary = FluorophoreReferenceLibrary | SharedReferenceCatalogue

_MAX_BYTES = 32 * 1024 * 1024
_NAMES = frozenset(
    {"library.json", "spectra-l1.csv", "spectra-l2.csv", "manifest.json", "README.txt"}
)
_README = (
    "Reviewed spectral library exchange\n\n"
    "library.json is the complete unchanged portable library, including measurement identities, "
    "intensity-correction and detector-response conventions, accepted spectra, QC and review evidence. The same library "
    "can be reused without reprocessing its source reference images when target compatibility is established.\n\n"
    "spectra-l1.csv contains nonnegative spectral fractions summing to one in each component column. "
    "spectra-l2.csv contains unit-L2 columns. The native convention is recorded in library.json. Rows follow the exact measurement axis; "
    "columns are fluorophores, not inferred biological markers. Target-specific autofluorescence is not added to this portable fluorophore library.\n\n"
    "If S1 = S2 / column_sum(S2), then a1 = column_sum(S2) * a2. "
    "Multiply, do not divide. This is an exact coefficient change of basis for the nonnegative linear model, "
    "up to numerical tolerance. The pipeline applies its reporting conversion once and also retains "
    "any explicitly recorded abundance rescaling. Neither L1 nor L2 normalization establishes instrument calibration.\n\n"
    "Import this complete package or library.json. CSV files are inspection/interchange views, "
    "not stand-alone proof of calibration, source compatibility or accepted ROI evidence. "
    "Editing a CSV invalidates this package; it does not revise the accepted source library.\n"
)


# ==============================================================================
#                                Library exchange
# ==============================================================================


def _files(library: ReferenceLibrary) -> dict[str, bytes]:
    if type(library) not in {FluorophoreReferenceLibrary, SharedReferenceCatalogue}:
        raise ValueError("Only a complete reviewed fluorophore library can be exported")
    canonical = dumps_reference_source(library).encode("utf-8")
    if loads_reference_source(canonical) != library:
        raise ValueError("The native library differs after validation")
    sums = tuple(math.fsum(spectrum.values) for spectrum in library.spectra)
    l2_divisors = tuple(
        math.hypot(*spectrum.values) if spectrum.normalization_convention == "unit_l1" else 1.0
        for spectrum in library.spectra
    )
    files = {"library.json": canonical, "README.txt": _README.encode()}
    for convention in ("l1", "l2"):
        out = io.StringIO(newline="")
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(["measurement_id", *[s.component_identity for s in library.spectra]])
        for row, measurement in enumerate(library.axis.measurement_ids):
            writer.writerow(
                [
                    measurement,
                    *[
                        format(s.values[row] / (sums[col] if convention == "l1" else l2_divisors[col]), ".17g")
                        for col, s in enumerate(library.spectra)
                    ],
                ]
            )
        files["spectra-" + convention + ".csv"] = out.getvalue().encode("utf-8")
    manifest = {
        "schema": ("mif-reviewed-cycle-catalogue-exchange/v1" if type(library) is SharedReferenceCatalogue
                   else "mif-reviewed-library-exchange/v1"),
        "library_content_sha256": library.content_sha256,
        "native_normalization": library.normalization_convention,
        "measurement_ids": list(library.axis.measurement_ids),
        "component_ids": [s.component_identity for s in library.spectra],
        "l2_coefficient_to_l1_multipliers": [total / norm for total, norm in zip(sums, l2_divisors)],
        "pipeline_reporting_multipliers": [
            sums[i] * s.abundance_rescaling_factor for i, s in enumerate(library.spectra)
        ],
        "members": {
            name: hashlib.sha256(payload).hexdigest() for name, payload in sorted(files.items())
        },
    }
    files["manifest.json"] = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()
    if sum(map(len, files.values())) > _MAX_BYTES:
        raise ValueError("The portable library exceeds the supported exchange size")
    return files


def build_library_exchange(library: ReferenceLibrary) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, content in sorted(_files(library).items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o100600 << 16
            archive.writestr(info, content)
    return out.getvalue()


def load_library_exchange(payload: bytes) -> ReferenceLibrary:
    """Read a native canonical library or bounded archive without extracting files."""
    if type(payload) is not bytes or not payload or len(payload) > _MAX_BYTES:
        raise ValueError("The imported library is empty or exceeds the supported size")
    if not payload.startswith(b"PK"):
        try:
            library = loads_reference_source(payload)
        except Exception as error:
            raise ValueError(
                "Select a reviewed library JSON or .miflib.zip package, not a bare matrix"
            ) from error
        if (
            type(library) not in {FluorophoreReferenceLibrary, SharedReferenceCatalogue}
            or dumps_reference_source(library).encode() != payload
        ):
            raise ValueError(
                "The imported library must contain exact canonical reviewed-library bytes"
            )
        return library
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            infos = archive.infolist()
            if (
                len(infos) != len(_NAMES)
                or {i.filename for i in infos} != _NAMES
                or sum(i.file_size for i in infos) > _MAX_BYTES
            ):
                raise ValueError("The library exchange member inventory or size differs")
            if any(
                i.is_dir() or i.flag_bits & 1 or (i.external_attr >> 16) & 0o170000 == 0o120000
                for i in infos
            ):
                raise ValueError(
                    "The library exchange contains unsupported linked or encrypted members"
                )
            members = {name: archive.read(name) for name in _NAMES}
        if members["library.json"].startswith(b"PK"):
            raise ValueError(
                "The library.json member must contain native JSON, not another archive"
            )
        library = load_library_exchange(members["library.json"])
        expected = _files(library)
        manifest = json.loads(members["manifest.json"])
        if type(manifest) is not dict:
            raise ValueError("The library exchange manifest must be an object")
        expected_manifest = json.loads(expected["manifest.json"])
        member_hashes = {
            name: hashlib.sha256(content).hexdigest()
            for name, content in members.items() if name != "manifest.json"
        }
        if manifest.get("members") != member_hashes:
            raise ValueError("The library exchange member hashes differ")
        manifest["members"] = expected_manifest["members"]
        if manifest != expected_manifest or any(
            members[name] != expected[name]
            for name in ("library.json", "spectra-l1.csv", "spectra-l2.csv")
        ):
            raise ValueError("The library exchange content differs from its reviewed native source")
        return library
    except (zipfile.BadZipFile, RuntimeError, OSError) as error:
        raise ValueError("The library exchange is invalid or unreadable") from error


def write_library_exchange(library: ReferenceLibrary, destination: Path) -> Path:
    """Export a new deterministic package without replacing an existing file."""
    target = Path(destination)
    if not target.is_absolute() or not str(target).endswith(".miflib.zip"):
        raise ValueError("Choose an absolute export path ending in .miflib.zip")
    if target.exists() or target.is_symlink():
        raise FileExistsError("The library export already exists")
    payload = build_library_exchange(library)
    temporary = None
    try:
        with NamedTemporaryFile(
            dir=target.parent, prefix="." + target.name + ".", delete=False
        ) as out:
            temporary = Path(out.name)
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
        os.link(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


def read_library_payload(path: str | Path) -> bytes:
    """Read a selected library without allocating memory for an accidental WSI."""
    import stat

    source = Path(path)
    if not source.is_absolute() or source.is_symlink():
        raise ValueError("Select the original spectral library file.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        with os.fdopen(os.open(source, flags), "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= _MAX_BYTES:
                raise ValueError(
                    "Select a spectral library file up to 32 MiB. Check that an image was not selected instead."
                )
            payload = stream.read(_MAX_BYTES + 1)
            after = os.fstat(stream.fileno())
            identity = lambda value: (
                value.st_dev,
                value.st_ino,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )
            if (
                len(payload) != before.st_size
                or identity(before) != identity(after)
                or identity(after) != identity(source.stat())
            ):
                raise ValueError(
                    "The spectral library changed while it was being read. Select it again."
                )
    except OSError as error:
        raise ValueError(
            "The spectral library could not be opened. Check its location and file permissions."
        ) from error
    return payload
