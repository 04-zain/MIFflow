"""Open saved images with independent, temporary QuPath project data."""
from contextlib import contextmanager
import errno
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

try:
    from fcntl import FICLONE, ioctl
except ImportError:
    FICLONE = None


def _copy_project_file(source, target):
    # A whole-slide hierarchy can be large. Filesystem clones share its disk
    # blocks until QuPath writes to the inspection copy.
    if FICLONE is not None and Path(source).stat().st_size >= 1024 * 1024:
        with open(source, "rb") as original, open(target, "wb") as copy:
            try:
                ioctl(copy.fileno(), FICLONE, original.fileno())
                return target
            except OSError as error:
                if error.errno not in {errno.EOPNOTSUPP, errno.EXDEV, errno.EINVAL, errno.ENOTTY, errno.ENOSYS}:
                    raise
    return shutil.copyfile(source, target)


@contextmanager
def inspection_project(source: Path):
    from .scripts import registered_script_bundle_sha256

    if not source.is_absolute() or not source.is_file():
        raise ValueError("Select an existing QuPath project for inspection.")
    # Keep relative image locators at the same depth. The pixel data stay at
    # their existing locations. Only QuPath project resources are writable here.
    with TemporaryDirectory(prefix=".inspection-", dir=source.parent.parent) as folder:
        destination = Path(folder)
        project = destination / source.name
        _copy_project_file(source, project)
        for name in ("data", "classifiers", "thumbnails"):
            member = source.parent / name
            if member.is_dir():
                shutil.copytree(member, destination / name, copy_function=_copy_project_file)
        scripts = source.parent / "scripts"
        if scripts.is_dir():
            current = scripts / ("bundle-" + registered_script_bundle_sha256())
            if current.is_dir():
                shutil.copytree(current, destination / "scripts" / current.name)
            else:
                # The bootstrap verifies older inventories before installing the
                # current helpers inside this inspection project.
                shutil.copytree(scripts, destination / "scripts")
        yield project
