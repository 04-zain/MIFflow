"""Image-free, fresh-JVM attestation of a local Bio-Formats classpath.

The backend uses the selected Java/Bio-Formats installation and image-series identity. Image
access is exposed through pipeline-owned records so callers retain the reviewed metadata and
regional coordinate frame."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Callable, Sequence

from ._offline_runtime import (
    _attest_validated_classpath,
    _loaded_origin,
    _start_verified_jvm,
)
from .errors import DependencyVersionError

_SCHEMA = "bioformats-offline-probe/v1"


# ==============================================================================
#                            Bioformats probe worker
# ==============================================================================


class _ProbeIncompatible(Exception):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(detail)


def _result(
    status: str, code: str, detail: str, digest: str, evidence: object = None
) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA,
        "status": status,
        "code": code,
        "detail": detail,
        "inventory_sha256": digest,
        "evidence": evidence,
    }


def attest_loaded_runtime(
    paths: tuple[Path, ...],
    inventory_sha256: str,
    supported_java_features: tuple[int, ...],
    *,
    start_jvm: Callable[[tuple[Path, ...]], None],
    class_origin: Callable[[str], Path],
    java_home: Path | None = None,
) -> dict[str, object]:
    """Adapt the shared owner evidence to the existing probe wire format."""
    try:
        attested = _attest_validated_classpath(
            paths,
            inventory_sha256,
            supported_java_features,
            start_jvm=start_jvm,
            class_origin=class_origin,
            expected_java_home=java_home,
        )
        common_root = Path(os.path.commonpath([str(path.parent) for path in paths]))
        by_relative = {path.relative_to(common_root).as_posix(): path for path in paths}
        evidence = {
            "java_version": attested.java_version,
            "java_feature": attested.java_feature,
            "supported_java_features": list(supported_java_features),
            "java_home": attested.java_home,
            "bffile_version": attested.bffile_version,
            "bioformats_maven_coordinate": attested.bioformats_maven_coordinate,
            "bioformats_runtime_version": attested.bioformats_runtime_version,
            "tools_maven_coordinate": attested.tools_maven_coordinate,
            "loaded_classes": [
                {
                    "name": item.name,
                    "jar_path": str(by_relative[item.jar_relative_path.as_posix()]),
                    "jar_sha256": item.jar_sha256,
                }
                for item in attested.loaded_classes
            ],
        }
        return _result(
            "ready",
            "loaded_runtime_verified",
            "Loaded offline runtime verified",
            inventory_sha256,
            evidence,
        )
    except DependencyVersionError as exc:
        detail = str(exc)
        code = detail.split(":", 1)[0] if ":" in detail else "runtime_error"
        return _result("incompatible", code, detail, inventory_sha256)


def _read_request(path: Path) -> tuple[tuple[Path, ...], str, tuple[int, ...], Path]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            request = json.load(stream)
        if not isinstance(request, dict) or set(request) != {
            "schema_version",
            "paths",
            "supported_java_features",
            "inventory_sha256",
            "result_path",
        }:
            raise ValueError("Invalid offline inventory request fields")
        if request["schema_version"] != "bioformats-offline-probe-request/v1":
            raise ValueError("Invalid request schema")
        requested_paths = request["paths"]
        if not isinstance(requested_paths, list) or not requested_paths:
            raise ValueError("Empty JAR inventory")
        paths = []
        for item in requested_paths:
            if not isinstance(item, str):
                raise ValueError("Invalid JAR path")
            jar_path = Path(item)
            if not jar_path.is_absolute():
                raise ValueError("Invalid JAR path")
            paths.append(jar_path)
        policy = tuple(request["supported_java_features"])
        if not policy or any(type(feature) is not int or feature <= 0 for feature in policy):
            raise ValueError("Invalid Java policy")
        digest = request["inventory_sha256"]
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("Invalid inventory digest")
        result_path = Path(request["result_path"])
        if not result_path.is_absolute():
            raise ValueError("Invalid result path")
        return tuple(paths), digest, policy, result_path
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise _ProbeIncompatible("worker_request_invalid", str(exc)) from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request-json", required=True, type=Path)
    parser.add_argument("--result-json", type=Path)
    parser.add_argument("--java-home", type=Path)
    args = parser.parse_args(argv)
    digest = ""
    result_path = args.result_json
    try:
        paths, digest, policy, result_path = _read_request(args.request_json)
        result = attest_loaded_runtime(
            paths,
            digest,
            policy,
            start_jvm=(lambda ordered: _start_verified_jvm(ordered, java_home=args.java_home)),
            class_origin=_loaded_origin,
            java_home=args.java_home,
        )
    except _ProbeIncompatible as exc:
        result = _result("incompatible", exc.code, str(exc), digest)
    except Exception as exc:
        result = _result("error", "worker_error", f"{type(exc).__name__}: {exc}", digest)
    if result_path is not None:
        with result_path.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, sort_keys=True, allow_nan=False)
    return 0 if result["status"] == "ready" else 2 if result["status"] == "incompatible" else 3


if __name__ == "__main__":
    sys.exit(main())
