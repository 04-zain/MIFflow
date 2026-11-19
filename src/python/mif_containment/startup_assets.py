"""Deterministic production builders and fixed Linux startup install assets.

The `BuiltStartupZipapp` record carries `install_path`, `content`, `sha256`, `modules`,
`owner_uid` and its remaining declared fields.

The implementation exchanges explicit process, scope and operation records through the
declared host interfaces. Native lifecycle observations are checked before the corresponding
process-control result is accepted."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import dataclass
from pathlib import Path

STARTUP_PROBE_INSTALL_PATH = "/usr/libexec/mif-pipeline/mif-containment-startup-probe"
STARTUP_CERTIFICATION_RUNNER_INSTALL_PATH = (
    "/usr/libexec/mif-pipeline/mif-containment-startup-certification"
)
INSTALL_ASSET_NAMES = (
    "mif-containment-startup-certification.service",
    "mif-containment-service-hardening.conf",
    "mif-pipeline-qupath.slice",
    "mif-pipeline-containment.sysusers",
    "containment-broker.example.json",
    "linux-containment-broker-requirements.txt",
)

_SHEBANG = b"#!/usr/bin/python3 -I\n"
_MODULE_ROOT = Path(__file__).resolve().parent
_INSTALL_ASSET_ROOT = _MODULE_ROOT / "install_assets"
_RUNNER_MODULES = (
    "__init__.py",
    "codec.py",
    "protocol.py",
    "startup_certification.py",
    "startup_certification_runner.py",
    "startup_probe.py",
    "systemd_adapter.py",
)
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


# ==============================================================================
#                                 Startup assets
# ==============================================================================


@dataclass(frozen=True, slots=True)
class BuiltStartupZipapp:
    """Complete bytes and root-install identity for one fixed zipapp."""

    install_path: str
    content: bytes
    sha256: str
    modules: tuple[str, ...]
    owner_uid: int = 0
    owner_gid: int = 0
    mode: int = 0o755


def build_startup_probe_zipapp() -> BuiltStartupZipapp:
    """Build the inert isolated startup probe from its sole production source."""

    entries = (("__main__.py", (_MODULE_ROOT / "startup_probe.py").read_bytes()),)
    return _build_zipapp(STARTUP_PROBE_INSTALL_PATH, entries)


def build_startup_certification_runner_zipapp() -> BuiltStartupZipapp:
    """Build the fixed root-only runner from the closed production subset."""

    entrypoint = (
        b"from mif_containment.startup_certification_runner import "
        b"startup_certification_main\n"
        b"raise SystemExit(startup_certification_main())\n"
    )
    entries = [("__main__.py", entrypoint)]
    entries.extend(
        (f"mif_containment/{name}", (_MODULE_ROOT / name).read_bytes()) for name in _RUNNER_MODULES
    )
    return _build_zipapp(STARTUP_CERTIFICATION_RUNNER_INSTALL_PATH, tuple(entries))


def install_asset_bytes(name: str) -> bytes:
    """Read one exact reviewed install asset by its closed basename."""

    if type(name) is not str or name not in INSTALL_ASSET_NAMES:
        raise ValueError("unknown containment install asset")
    return (_INSTALL_ASSET_ROOT / name).read_bytes()


def _build_zipapp(
    install_path: str,
    entries: tuple[tuple[str, bytes], ...],
) -> BuiltStartupZipapp:
    ordered = tuple(sorted(entries))
    output = io.BytesIO()
    with zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_STORED,
        strict_timestamps=True,
    ) as archive:
        for name, content in ordered:
            information = zipfile.ZipInfo(name, date_time=_ZIP_TIMESTAMP)
            information.compress_type = zipfile.ZIP_STORED
            information.create_system = 3
            information.external_attr = (0o100644 & 0xFFFF) << 16
            information.flag_bits = 0
            archive.writestr(information, content)
    content = _SHEBANG + output.getvalue()
    return BuiltStartupZipapp(
        install_path=install_path,
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
        modules=tuple(name for name, _ in ordered),
    )
