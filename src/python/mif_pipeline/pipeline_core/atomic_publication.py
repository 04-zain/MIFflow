"""Atomic publication of completed files and directories at unused names."""

import ctypes
import os
from pathlib import Path


class PublicationError(OSError):
    """The platform cannot safely publish without replacing an existing result."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(detail)


def publish_no_replace(source: Path, target: Path, platform: str) -> None:
    """

    Moves a verified file or directory to its final name only when that name remains unused.

    Windows os.rename() refuses an existing destination. On Linux, we call
    renameat2 with RENAME_NOREPLACE, which performs the same check as part of
    the filesystem rename. The caller therefore cannot overwrite an existing
    version that appeared after its earlier directory check.

    Args:
        source: Completed temporary file or directory on the destination filesystem.
        target: Final installation filename or directory.
        platform: win-x86_64 or linux-x86_64.

    Raises:
        PublicationError: The platform lacks the required no-replacement operation.
        OSError: The target exists or the filesystem cannot perform the rename.
    """

    if platform == "win-x86_64":
        os.rename(source, target)
        return
    if platform != "linux-x86_64":
        raise PublicationError("publication_unavailable", "Unsupported publication platform")
    try:
        call = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:
        raise PublicationError(
            "publication_unavailable", "Atomic publication is unavailable"
        ) from exc
    call.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    call.restype = ctypes.c_int
    if call(-100, os.fsencode(source), -100, os.fsencode(target), 1) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(target))
