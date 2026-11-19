"""Stage the pinned Ubuntu XCB libraries and notices inside a portable desktop."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
from urllib.request import urlopen


def _copy_library(source: Path, destination: Path) -> None:
    """Replace a collected library, retaining a contained SONAME link if present."""
    if destination.is_symlink():
        internal = destination.parent.resolve()
        target = destination.resolve(strict=True)
        if internal not in target.parents or not target.is_file():
            raise ValueError(f"collected library link leaves the bundle: {destination}")
        destination = target
    temporary = destination.with_name(f".{destination.name}.ubuntu.tmp")
    try:
        with source.open("rb") as incoming, temporary.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def stage_gui_libraries(lock_path: Path, bundle: Path, build_root: Path) -> None:
    """Copy the missing Qt XCB dependencies without installing system packages.

    We retain the downloaded package identities beside their copyright notices.
    The existing release writer includes these files and libraries in its final
    checksums. PyInstaller confines its runtime library search to this bundle.
    """
    lock_bytes = lock_path.read_bytes()
    packages = json.loads(lock_bytes)
    internal = bundle / "_internal"
    if not internal.is_dir():
        raise ValueError(f"portable bundle has no _internal directory: {bundle}")
    build_root.mkdir()
    notices = bundle / "licenses" / "ubuntu-xcb"
    notices.mkdir(parents=True)
    for package in packages:
        name = package["package"]
        archive = build_root / f"{name}.deb"
        digest = sha256()
        byte_count = 0
        with urlopen(package["url"], timeout=60) as response, archive.open("xb") as output:
            for block in iter(lambda: response.read(1024 * 1024), b""):
                byte_count += len(block)
                if byte_count > package["size_bytes"]:
                    raise ValueError(f"Ubuntu package exceeds its locked size: {name}")
                digest.update(block)
                output.write(block)
        if byte_count != package["size_bytes"] or digest.hexdigest() != package["sha256"]:
            raise ValueError(f"Ubuntu package differs from its locked identity: {name}")
        extracted = build_root / name
        subprocess.run(["dpkg-deb", "--extract", str(archive), str(extracted)], check=True)
        extracted = extracted.resolve()
        library = (extracted / "usr/lib/x86_64-linux-gnu" / package["library"]).resolve(strict=True)
        copyright_file = (extracted / "usr/share/doc" / name / "copyright").resolve(strict=True)
        for source in (library, copyright_file):
            if extracted not in source.parents or not source.is_file():
                raise ValueError(f"Ubuntu package member is not a contained file: {source}")
        notice_directory = notices / name
        notice_directory.mkdir()
        # PyInstaller may already collect this SONAME on a fuller build host.
        # A contained SONAME link keeps its target, avoiding an obsolete copy.
        _copy_library(library, internal / package["library"])
        with copyright_file.open("rb") as incoming, (notice_directory / "copyright").open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
    # The Ubuntu notices retain references to this common license. We reuse the
    # complete text already present in the source's release notice inventory.
    common_licenses = notices / "common-licenses"
    common_licenses.mkdir()
    gpl2 = Path(__file__).resolve().parents[1] / "packaging/bioformats/required-notices/ome-turbojpeg-8.5.0-GPL-2.0.txt"
    with gpl2.open("rb") as incoming, (common_licenses / "GPL-2").open("xb") as outgoing:
        shutil.copyfileobj(incoming, outgoing)
    with (notices / "README.txt").open("x", encoding="utf-8") as output:
        output.write("Package copyright files are retained verbatim. Their /usr/share/common-licenses/GPL-2 references are supplied by common-licenses/GPL-2 in this directory.\n")
    with (notices / "packages.lock.json").open("xb") as output:
        output.write(lock_bytes)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--build-root", required=True, type=Path)
    args = parser.parse_args()
    stage_gui_libraries(args.lock, args.bundle, args.build_root)


if __name__ == "__main__":
    main()
