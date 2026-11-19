"""Offline, exact local inventory contract for the Bio-Formats prerequisite.

The `BioFormatsRuntimeEvidence` record carries `inventory_sha256`, `java_version`,
`java_feature`, `supported_java_features`, `java_home` and its remaining declared fields."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Literal

FROZEN_BIOFORMATS_PROBE_SWITCH = "--mif-internal-bioformats-probe-v1"
_PROBE_SCHEMA = "bioformats-offline-probe/v1"
_REQUEST_SCHEMA = "bioformats-offline-probe-request/v1"
_REQUIRED_CLASSES = (
    "loci.formats.FormatTools",
    "loci.formats.in.VectraReader",
    "loci.formats.tools.ImageConverter",
)


# ==============================================================================
#                            Bioformats prerequisite
# ==============================================================================


@dataclass(frozen=True, slots=True)
class RuntimeJar:
    relative_path: PurePosixPath
    sha256: str


@dataclass(frozen=True, slots=True)
class BioFormatsRuntimeInventory:
    payload_root: Path
    jars: tuple[RuntimeJar, ...]
    supported_java_features: tuple[int, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "jars", tuple(self.jars))
        object.__setattr__(self, "supported_java_features", tuple(self.supported_java_features))


@dataclass(frozen=True, slots=True)
class LoadedJavaClass:
    name: str
    jar_relative_path: PurePosixPath
    jar_sha256: str


@dataclass(frozen=True, slots=True)
class BioFormatsRuntimeEvidence:
    inventory_sha256: str
    java_version: str
    java_feature: int
    supported_java_features: tuple[int, ...]
    java_home: str
    bffile_version: str
    bioformats_maven_coordinate: str
    bioformats_runtime_version: str
    tools_maven_coordinate: str
    loaded_classes: tuple[LoadedJavaClass, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "supported_java_features", tuple(self.supported_java_features))
        object.__setattr__(self, "loaded_classes", tuple(self.loaded_classes))


@dataclass(frozen=True, slots=True)
class BioFormatsPrerequisiteResult:
    status: Literal["ready", "missing", "incompatible", "error"]
    code: str
    detail: str
    evidence: BioFormatsRuntimeEvidence | None


class InventoryMissing(Exception):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"{code}: {detail}")


class InventoryIncompatible(Exception):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"{code}: {detail}")


_LOWER_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _safe_regular_file(path: Path) -> os.stat_result:
    try:
        details = path.lstat()
    except FileNotFoundError as exc:
        raise InventoryMissing("jar_absent", f"Missing runtime JAR: {path}") from exc
    except OSError as exc:
        raise InventoryIncompatible("jar_unreadable", f"Cannot inspect {path}: {exc}") from exc
    if not stat.S_ISREG(details.st_mode):
        raise InventoryIncompatible("jar_not_regular", f"Unsafe runtime entry: {path}")
    return details


def _validate_inventory(
    inventory: BioFormatsRuntimeInventory | None,
) -> tuple[tuple[Path, ...], str]:
    """Check the declared local closure and return its ordered classpath and digest.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""
    if inventory is None:
        raise InventoryMissing("payload_absent", "No Bio-Formats payload inventory")
    if not inventory.supported_java_features:
        raise InventoryIncompatible("java_policy_absent", "No authenticated Java feature policy")
    if any(
        type(feature) is not int or feature <= 0 for feature in inventory.supported_java_features
    ):
        raise InventoryIncompatible("java_policy_invalid", "Invalid Java feature policy")
    if not inventory.jars:
        raise InventoryIncompatible("classpath_empty", "No runtime JARs declared")

    root = Path(inventory.payload_root).absolute()
    for component in (*reversed(root.parents), root):
        try:
            details = component.lstat()
        except FileNotFoundError as exc:
            raise InventoryMissing("payload_absent", f"Missing payload root: {root}") from exc
        except OSError as exc:
            raise InventoryIncompatible(
                "payload_unreadable", f"Cannot inspect {component}: {exc}"
            ) from exc
        if stat.S_ISLNK(details.st_mode):
            raise InventoryIncompatible("unsafe_path", f"Symlink in payload root: {component}")
    if not root.is_dir():
        raise InventoryIncompatible("payload_not_directory", f"Not a payload directory: {root}")

    paths: list[Path] = []
    declared: set[PurePosixPath] = set()
    for jar in inventory.jars:
        relative = jar.relative_path
        if (
            not isinstance(relative, PurePosixPath)
            or relative.is_absolute()
            or relative == PurePosixPath(".")
            or any(part in ("..", "") or "\\" in part or ":" in part for part in relative.parts)
        ):
            raise InventoryIncompatible("unsafe_path", f"Unsafe runtime path: {relative}")
        if relative.suffix != ".jar":
            raise InventoryIncompatible("jar_extension_invalid", f"Not a JAR: {relative}")
        if relative in declared:
            raise InventoryIncompatible("jar_duplicate", f"Duplicate runtime JAR: {relative}")
        declared.add(relative)
        if not isinstance(jar.sha256, str) or _LOWER_SHA256.fullmatch(jar.sha256) is None:
            raise InventoryIncompatible("jar_hash_invalid", f"Invalid SHA-256 for {relative}")
        path = root.joinpath(*relative.parts)
        for directory in path.parents:
            if directory == root:
                break
            try:
                details = directory.lstat()
            except FileNotFoundError as exc:
                raise InventoryMissing("jar_absent", f"Missing runtime path: {directory}") from exc
            if not stat.S_ISDIR(details.st_mode):
                raise InventoryIncompatible("unsafe_path", f"Unsafe runtime path: {directory}")
        before = _safe_regular_file(path)
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise InventoryIncompatible("unsafe_path", f"Cannot resolve {path}: {exc}") from exc
        if not resolved.is_relative_to(root):
            raise InventoryIncompatible("unsafe_path", f"Runtime path escapes payload: {path}")
        digest = sha256()
        try:
            with path.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    digest.update(block)
                opened = os.fstat(stream.fileno())
        except OSError as exc:
            raise InventoryIncompatible("jar_unreadable", f"Cannot hash {path}: {exc}") from exc
        after = _safe_regular_file(path)
        before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        opened_identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if before_identity != opened_identity or before_identity != after_identity:
            raise InventoryIncompatible("jar_changed", f"Runtime JAR changed while hashing: {path}")
        if digest.hexdigest() != jar.sha256:
            raise InventoryIncompatible("jar_digest_mismatch", f"SHA-256 differs for {relative}")
        paths.append(path)

    discovered: set[PurePosixPath] = set()

    def walk_error(error: OSError) -> None:
        raise InventoryIncompatible(
            "payload_unreadable", f"Cannot scan payload: {error}"
        ) from error

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        for name in (*dirs, *files):
            entry = Path(directory) / name
            try:
                details = entry.lstat()
            except OSError as exc:
                raise InventoryIncompatible(
                    "payload_unreadable", f"Cannot inspect {entry}: {exc}"
                ) from exc
            if stat.S_ISLNK(details.st_mode) or not (
                stat.S_ISREG(details.st_mode) or stat.S_ISDIR(details.st_mode)
            ):
                raise InventoryIncompatible("unsafe_path", f"Unsafe payload entry: {entry}")
            if stat.S_ISREG(details.st_mode) and entry.suffix == ".jar":
                discovered.add(PurePosixPath(entry.relative_to(root).as_posix()))
    if discovered != declared:
        raise InventoryIncompatible(
            "classpath_closure_mismatch", "Payload JAR closure differs from inventory"
        )

    canonical = json.dumps(
        {
            "jars": [[jar.relative_path.as_posix(), jar.sha256] for jar in inventory.jars],
            "supported_java_features": list(inventory.supported_java_features),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return tuple(paths), sha256(canonical).hexdigest()


def _probe_error(code: str, detail: str) -> BioFormatsPrerequisiteResult:
    return BioFormatsPrerequisiteResult("error", code, detail, None)


def _invalid_result(detail: str) -> BioFormatsPrerequisiteResult:
    return _probe_error("invalid_probe_result", detail)


def _parse_result(
    raw: bytes,
    returncode: int,
    paths: tuple[Path, ...],
    inventory_sha256: str,
    supported_java_features: tuple[int, ...],
    expected_hashes: tuple[str, ...],
    expected_java_home: Path | None = None,
) -> BioFormatsPrerequisiteResult:
    try:

        def unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"Duplicate JSON key: {key}")
                result[key] = value
            return result

        record = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_pairs)
        if (
            not isinstance(record, dict)
            or set(record)
            != {"schema_version", "status", "code", "detail", "inventory_sha256", "evidence"}
            or record["schema_version"] != _PROBE_SCHEMA
        ):
            return _invalid_result("Invalid Bio-Formats probe schema")
        status, code, detail = record["status"], record["code"], record["detail"]
        if not isinstance(code, str) or not code or not isinstance(detail, str) or not detail:
            return _invalid_result("Invalid Bio-Formats probe diagnostic")
        if (
            status not in ("ready", "incompatible", "error")
            or returncode
            != {
                "ready": 0,
                "incompatible": 2,
                "error": 3,
            }[status]
        ):
            return _invalid_result("Bio-Formats probe exit/status mismatch")
        if record["inventory_sha256"] != inventory_sha256:
            return BioFormatsPrerequisiteResult(
                "incompatible", "inventory_digest_mismatch", "Probe inventory digest differs", None
            )
        evidence = record["evidence"]
        if status != "ready":
            if evidence is not None:
                return _invalid_result("Non-ready probe supplied evidence")
            return BioFormatsPrerequisiteResult(status, code, detail, None)
        if (
            code != "loaded_runtime_verified"
            or not isinstance(evidence, dict)
            or set(evidence)
            != {
                "java_version",
                "java_feature",
                "supported_java_features",
                "java_home",
                "bffile_version",
                "bioformats_maven_coordinate",
                "bioformats_runtime_version",
                "tools_maven_coordinate",
                "loaded_classes",
            }
        ):
            return _invalid_result("Invalid ready probe evidence")
        if evidence["supported_java_features"] != list(supported_java_features):
            return BioFormatsPrerequisiteResult(
                "incompatible", "java_policy_mismatch", "Probe Java policy differs", None
            )
        if (
            type(evidence["java_feature"]) is not int
            or evidence["java_feature"] not in supported_java_features
        ):
            return BioFormatsPrerequisiteResult(
                "incompatible",
                "java_feature_unsupported",
                "Probe Java feature is unsupported",
                None,
            )
        expected_versions = {
            "bffile_version": "0.1.1",
            "bioformats_maven_coordinate": "ome:formats-gpl:8.5.0",
            "bioformats_runtime_version": "8.5.0",
            "tools_maven_coordinate": "ome:bio-formats-tools:8.5.0",
        }
        if any(evidence[key] != value for key, value in expected_versions.items()):
            return BioFormatsPrerequisiteResult(
                "incompatible", "runtime_version_mismatch", "Probe runtime version differs", None
            )
        if any(
            not isinstance(evidence[key], str) or not evidence[key]
            for key in ("java_version", "java_home")
        ):
            return _invalid_result("Invalid Java evidence")
        if (
            expected_java_home is not None
            and Path(evidence["java_home"]).resolve() != expected_java_home.resolve()
        ):
            return BioFormatsPrerequisiteResult(
                "incompatible", "java_home_mismatch", "Probe loaded a different Java home", None
            )
        loaded = evidence["loaded_classes"]
        if not isinstance(loaded, list) or len(loaded) != len(_REQUIRED_CLASSES):
            return _invalid_result("Invalid loaded class inventory")
        path_hashes = dict(zip((str(path) for path in paths), expected_hashes))
        classes = []
        for item, name in zip(loaded, _REQUIRED_CLASSES):
            if not isinstance(item, dict) or set(item) != {"name", "jar_path", "jar_sha256"}:
                return _invalid_result("Invalid loaded class entry")
            if item["name"] != name:
                return _invalid_result("Loaded class names differ")
            jar_path, jar_hash = item["jar_path"], item["jar_sha256"]
            if (
                not isinstance(jar_path, str)
                or jar_path not in path_hashes
                or not isinstance(jar_hash, str)
                or _LOWER_SHA256.fullmatch(jar_hash) is None
                or path_hashes[jar_path] != jar_hash
            ):
                return BioFormatsPrerequisiteResult(
                    "incompatible",
                    "class_origin_outside_inventory",
                    "Loaded class JAR differs",
                    None,
                )
            classes.append(LoadedJavaClass(name, PurePosixPath(jar_path), jar_hash))
        return BioFormatsPrerequisiteResult(
            "ready",
            code,
            detail,
            BioFormatsRuntimeEvidence(
                inventory_sha256,
                evidence["java_version"],
                evidence["java_feature"],
                supported_java_features,
                evidence["java_home"],
                evidence["bffile_version"],
                evidence["bioformats_maven_coordinate"],
                evidence["bioformats_runtime_version"],
                evidence["tools_maven_coordinate"],
                tuple(classes),
            ),
        )
    except (UnicodeError, ValueError, TypeError, KeyError, AttributeError, OSError):
        return _invalid_result("Malformed Bio-Formats probe result")


def _run_worker(
    paths: tuple[Path, ...],
    inventory_sha256: str,
    supported_java_features: tuple[int, ...],
    *,
    timeout_seconds: float,
    expected_hashes: tuple[str, ...],
    java_home: Path | None = None,
) -> BioFormatsPrerequisiteResult:
    with TemporaryDirectory(prefix="mif-bioformats-probe-") as directory:
        request_path = Path(directory) / "request.json"
        result_path = Path(directory) / "result.json"
        # The caller verifies the inventory once; the worker checks the JVM
        # and the classes loaded from this ordered classpath.
        request = {
            "schema_version": _REQUEST_SCHEMA,
            "paths": [str(path) for path in paths],
            "supported_java_features": list(supported_java_features),
            "inventory_sha256": inventory_sha256,
            "result_path": str(result_path),
        }
        request_path.write_text(json.dumps(request, sort_keys=True), encoding="utf-8")
        java_arguments = ("--java-home", str(java_home)) if java_home is not None else ()
        command = (
            (
                sys.executable,
                FROZEN_BIOFORMATS_PROBE_SWITCH,
                *java_arguments,
                "--request-json",
                str(request_path),
            )
            if bool(getattr(sys, "frozen", False))
            else (
                sys.executable,
                "-m",
                "mif_pipeline.io.backends._bioformats_probe_worker",
                *java_arguments,
                "--request-json",
                str(request_path),
            )
        )
        environment = os.environ.copy()
        for name in ("CLASSPATH", "JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS"):
            environment.pop(name, None)
        environment["BFF_JAVA_FETCH"] = "never"
        environment["BIOFORMATS_VERSION"] = "ome:formats-gpl:8.5.0"
        try:
            completed = subprocess.run(
                command,
                timeout=timeout_seconds,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
            )
        except subprocess.TimeoutExpired:
            return _probe_error("probe_timeout", "Bio-Formats probe timed out")
        except OSError as exc:
            return _probe_error("probe_launch_failed", f"Bio-Formats probe could not start: {exc}")
        try:
            with result_path.open("rb") as handle:
                raw = handle.read(65_537)
        except OSError:
            return _probe_error("probe_result_missing", "Bio-Formats probe result missing")
        if len(raw) > 65_536:
            return _invalid_result("Bio-Formats probe result exceeded 64 KiB")
        return _parse_result(
            raw,
            completed.returncode,
            paths,
            inventory_sha256,
            supported_java_features,
            expected_hashes,
            java_home,
        )


def probe_bioformats_prerequisite(
    inventory: BioFormatsRuntimeInventory | None,
    *,
    timeout_seconds: float = 20.0,
    java_home: Path | None = None,
) -> BioFormatsPrerequisiteResult:
    from .backends._offline_runtime import _explicit_java_library, selected_java_home
    from .backends.errors import DependencyVersionError

    chosen_java = java_home if java_home is not None else selected_java_home()
    if chosen_java is not None:
        try:
            _explicit_java_library(chosen_java)
        except DependencyVersionError as exc:
            code = str(exc).split(":", 1)[0]
            return BioFormatsPrerequisiteResult("incompatible", code, str(exc), None)
    try:
        paths, digest = _validate_inventory(inventory)
    except InventoryMissing as exc:
        return BioFormatsPrerequisiteResult("missing", exc.code, str(exc), None)
    except InventoryIncompatible as exc:
        return BioFormatsPrerequisiteResult("incompatible", exc.code, str(exc), None)
    assert inventory is not None
    result = _run_worker(
        paths,
        digest,
        inventory.supported_java_features,
        timeout_seconds=timeout_seconds,
        expected_hashes=tuple(jar.sha256 for jar in inventory.jars),
        java_home=chosen_java,
    )
    if result.evidence is None:
        return result
    relatives = {str(path): jar.relative_path for path, jar in zip(paths, inventory.jars)}
    classes = tuple(
        replace(item, jar_relative_path=relatives[str(item.jar_relative_path)])
        for item in result.evidence.loaded_classes
    )
    return replace(result, evidence=replace(result.evidence, loaded_classes=classes))
