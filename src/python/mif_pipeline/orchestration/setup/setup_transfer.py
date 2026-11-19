"""

Downloads the selected software files and installs a completed file or directory at a new location.

After a setup action confirms its download, we read the recorded HTTPS
address, expected byte count and SHA-256. Downloaded bytes are written to a
new temporary file in one-megabyte blocks while we calculate their checksum.
The setup operation checks any extracted files and required program execution
before calling the final no-replacement move.

These functions are shared by the separate program installers. Their release
definitions and extraction rules remain in each program's setup code and
config/software file.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from hashlib import sha256
from http.client import HTTPException
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

# ==============================================================================
#                 Download verification and final installation
# ==============================================================================


class SetupTransferError(RuntimeError):
    """

    Reports the failed download or installation operation with a stable code and explanation.

    Attributes:
        code: Failure category used by the setup interface.
        detail: Text explaining the failed operation and corrective action.
    """

    def __init__(self, code: str, detail: str):
        """

        Records the failure category and readable setup explanation.

        Args:
            code: Stable failure category.
            detail: Explanation shown with the operation's technical details.
        """

        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def validate_https_url(url: str, hosts: frozenset[str], *, allow_query: bool = False) -> None:
    """

    Checks that a selected download address uses HTTPS and one of the permitted servers.

    We require an absolute URL path, no embedded username/password, and no
    fragment. The explicit port must be 443 when present. Query fields are
    accepted only when the caller permits them, as needed for a GitHub release's
    signed download redirect.

    Args:
        url: Download or redirected URL to inspect.
        hosts: Server names permitted for this step of the download.
        allow_query: Whether the selected redirect can contain query fields.

    Raises:
        ValueError: The address fails its scheme, server, credentials, port,
            path or query/fragment checks.
    """

    if type(url) is not str:
        raise ValueError("URL must be text")
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname not in hosts
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
        or not parts.path.startswith("/")
        or parts.fragment
        or (parts.query and not allow_query)
    ):
        raise ValueError("URL is outside pinned HTTPS origins")


def _download_origins(origin_host: str) -> tuple[frozenset[str], bool]:
    """Retains each publisher's explicit release-storage hosts for signed redirects."""

    if origin_host == "github.com":
        return frozenset({"github.com", "release-assets.githubusercontent.com"}), True
    if origin_host == "huggingface.co":
        return frozenset({
            "huggingface.co", "cas-bridge.xethub.hf.co", "us.aws.cdn.hf.co",
        }), True
    return frozenset({origin_host}), False


class PinnedRedirects(HTTPRedirectHandler):
    """

    Restricts redirects to the selected server and its permitted release-download service.

    GitHub and Hugging Face releases can redirect to their explicit release
    storage hosts. Other sources must remain on their original server. The final response URL
    is checked again while downloading, so both redirect handling and the
    resulting connection use the same permitted locations.
    """

    def __init__(self, origin_host: str):
        """

        Retains the original server used to determine the permitted redirect locations.

        Args:
            origin_host: Hostname from the recorded software download address.
        """

        self.origin_host = origin_host

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """

        Checks the redirect location before urllib constructs its next HTTP request.

        Args:
            req: Original urllib request.
            fp: Current response stream supplied by urllib.
            code: HTTP redirect status.
            msg: HTTP status description.
            headers: Response headers containing the redirect information.
            newurl: Proposed redirected address.

        Returns:
            Redirect request constructed by urllib after the destination checks pass.

        Raises:
            SetupTransferError: The redirect leaves the permitted HTTPS servers.
        """

        allowed, signed = _download_origins(self.origin_host)
        try:
            validate_https_url(newurl, allowed, allow_query=signed)
        except ValueError as exc:
            raise SetupTransferError(
                "unsafe_redirect", "Download redirected outside approved hosts"
            ) from exc
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_pinned_file(
    url: str, destination: Path, expected_hash: str, expected_size: int | None, *, opener=None, maximum_bytes: int = 512 * 1024 * 1024
) -> None:
    """

    Writes one software download while checking its total size and complete SHA-256.

    We open the recorded HTTPS address with a 60-second network timeout, check
    the final server, and create the destination exclusively. Each one-megabyte
    block updates the checksum and running byte count. Reading stops with an
    error if the selected size is exceeded, or if the transfer exceeds the
    selected upper limit (512 MiB for ordinary program downloads).

    After the response ends, we flush and synchronize the written bytes, then
    require the selected total size and checksum. The caller's temporary-directory
    context removes a partial file when an error prevents installation.

    Args:
        url: Recorded HTTPS download address.
        destination: New temporary filename selected by the program installer.
        expected_hash: Complete expected SHA-256 of the download.
        expected_size: Exact expected byte count, or None when only the shared
            upper limit and complete checksum are available.
        opener: Optional HTTP opener used by the setup operation or its tests.
            Otherwise we construct one with the permitted redirect rules.
        maximum_bytes: Fixed installer-specific ceiling. Large pinned model files
            can declare up to 2 GiB without raising other installers' limits.

    Raises:
        SetupTransferError: Redirect, size or checksum checks fail, the server
            returns an HTTP error, or writing/reading is interrupted.
    """

    if type(maximum_bytes) is not int or not 0 < maximum_bytes <= 2 * 1024**3:
        raise ValueError("Pinned download ceiling must be a positive integer no larger than 2 GiB")
    host = urlsplit(url).hostname
    validate_https_url(url, frozenset({host}))
    opener = opener or build_opener(PinnedRedirects(host))
    digest = sha256()
    count = 0
    try:
        request = Request(url, headers={"User-Agent": "mif-pipeline-dependency-setup"})
        with opener.open(request, timeout=60.0) as response, destination.open("xb") as output:
            allowed, signed = _download_origins(host)
            validate_https_url(response.geturl(), allowed, allow_query=signed)
            while block := response.read(1024 * 1024):
                count += len(block)
                if (
                    expected_size is not None and count > expected_size
                ) or count > maximum_bytes:
                    raise SetupTransferError(
                        "download_size_mismatch", "Download exceeds pinned size"
                    )
                digest.update(block)
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        if (
            expected_size is not None and count != expected_size
        ) or digest.hexdigest() != expected_hash:
            raise SetupTransferError("download_digest_mismatch", "Downloaded bytes differ from pin")
    except SetupTransferError:
        raise
    except HTTPError as exc:
        detail = (
            f"HTTP {exc.code}: Server temporarily unavailable. Try later or select existing files."
            if 500 <= exc.code <= 599
            else f"HTTP {exc.code}: The server did not provide the requested file."
        )
        raise SetupTransferError("download_http_error", detail) from exc
    except (OSError, HTTPException, ValueError) as exc:
        raise SetupTransferError(
            "download_interrupted",
            "Could not download the file. Check the connection or select existing files.",
        ) from exc
