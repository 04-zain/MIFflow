"""Exchange bounded image views and registration commands with the native session.

The managed QuPath runner owns the process. This object only writes private
requests, reads their completed responses and signals close; no Qt callback
waits for an image read, fit or process exit. Saved candidates still enter the
ordinary registration import and acceptance operations.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import base64
import json
import math
from pathlib import Path
import shutil
from tempfile import mkdtemp
from threading import Event, Lock
import time

import numpy as np


def decode_registration_frame(root: Path, result: dict) -> dict:
    """Read bounded float channel buffers from this session's private directory.

    The native reader supplies channels × rows × columns and little-endian
    float32 values. We validate dimensions and exact byte lengths before reading;
    supplied filenames cannot address another directory. Pixel buffers are
    removed after loading, so panning does not retain every previous viewport.
    """
    output = dict(result)
    arrays = []
    for item in result.get("images", ()):
        name, shape = item.get("file"), item.get("shape")
        if (not isinstance(name, str) or Path(name).name != name
                or not isinstance(shape, list) or len(shape) != 3
                or any(type(v) is not int or v < 1 for v in shape)
                or shape[0] > 64 or max(shape[1:]) > 2048
                or math.prod(shape) > 16_777_216):
            raise ValueError("The registration viewport has invalid pixel bounds.")
        path = root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size != math.prod(shape) * 4:
            raise ValueError("The registration viewport pixels are incomplete.")
        arrays.append(np.fromfile(path, dtype="<f4").reshape(shape))
        path.unlink()
    output["arrays"] = arrays
    return output


@dataclass
class RegistrationEditorSession:
    """Private exchange retained until the managed native process has drained.

    ``close`` is a nonblocking marker write. ``worker_finished`` is called by
    the controller after its process job completes. ``cleanup`` must run in a
    background job: it waits only for any response reader to release the files,
    then removes this session's temporary directory.
    """
    root: Path
    project: Path | None
    script_path: Path
    request_path: Path
    display: dict
    appearance_path: Path | None = None
    _counter: int = 0
    _lock: Lock = field(default_factory=Lock, repr=False)
    _closing: Event = field(default_factory=Event, repr=False)
    _finished: Event = field(default_factory=Event, repr=False)
    _error: str | None = None
    _draft_revision: int = 0
    _draft_global_changed: bool = False
    draft_error: str | None = None

    def retain_remote_draft(self, *, global_changed: bool, **values) -> None:
        """Retain current controls without waiting for the image-reader request lock.

        The native worker consumes the latest atomic marker before its next
        operation and after draining on close. Changing global inputs invalidates
        the returned affine for the rest of this editing session.
        """
        if self.closing or self._finished.is_set():
            return
        self._draft_revision += 1
        self._draft_global_changed |= global_changed
        target = self.root / "draft-inputs.json"
        temporary = target.with_suffix(".tmp")
        invalid = self.root / "draft-invalid"
        try:
            invalid.touch(exist_ok=True)
            temporary.write_text(json.dumps(dict(values, revision=self._draft_revision,
                global_changed=self._draft_global_changed), allow_nan=False), encoding="utf-8")
            temporary.replace(target)
            invalid.unlink(missing_ok=True)
        except (OSError, ValueError) as error:
            self.draft_error = "The current alignment edit could not be retained. Restore write access and edit the controls again before saving. " + str(error)
            raise RuntimeError(self.draft_error) from error
        self.draft_error = None

    @property
    def log_directory(self) -> Path:
        return self.project.parent / "logs" if self.project is not None else self.root

    @property
    def closing(self) -> bool:
        return self._closing.is_set()

    def _read_when_ready(self, path: Path, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while not path.is_file():
            if self._finished.wait(.025):
                if path.is_file():
                    break
                raise RuntimeError(self._error or "The registration editor has closed.")
            if time.monotonic() > deadline:
                raise TimeoutError("The registration operation did not return. Close the editor and inspect its native log.")
        if path.is_symlink() or path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("The registration response is invalid.")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("The registration response is invalid.")
        if value.get("error"):
            raise RuntimeError(str(value["error"]))
        return value

    def wait_ready(self) -> dict:
        """Wait in a worker thread for image metadata and initial native state."""
        from urllib.parse import unquote, urlparse
        from mif_pipeline.gui.image_appearance import load_image_appearance
        metadata = self._read_when_ready(self.root / "ready.json", 180.)
        for source in ('fixed', 'moving'):
            image = metadata[source]
            uri = urlparse(image['image_uri'])
            if uri.scheme == 'file':
                image['image_uri'] = Path(unquote(uri.path)).absolute().as_uri().rstrip('/')
            image['appearance'] = load_image_appearance(self.appearance_path, image['image_uri'])
        return metadata

    def save_appearance(self, image_uri: str, descriptor: dict) -> None:
        """Persist a display recipe through the shared appearance store off-thread."""
        from mif_pipeline.gui.image_appearance import save_image_appearance
        save_image_appearance(self.appearance_path, image_uri, descriptor)

    def request(self, operation: str, **values) -> dict:
        """Run one native operation; callers use a background job for this wait.

        Requests are serialized for the session's readers and transform state.
        A fit can take twenty minutes; bounded rendering has a shorter timeout.
        Completed exchange files and pixel buffers are removed after consumption.
        """
        with self._lock:
            if self.closing or self._finished.is_set():
                raise RuntimeError("The registration editor is closing.")
            if self.draft_error is not None and operation in {"affine", "save"}:
                raise RuntimeError(self.draft_error)
            number = self._counter
            self._counter += 1
            request = self.root / f"request-{number}.json"
            temporary = request.with_suffix(".tmp")
            temporary.write_text(json.dumps(dict(id=number, operation=operation, **values),
                                             allow_nan=False), encoding="utf-8")
            temporary.replace(request)
            response = self.root / f"response-{number}.json"
            try:
                returned = self._read_when_ready(response, 1500. if operation in {"affine", "local"} else 120.)
                if returned.get("id") != number or not isinstance(returned.get("result"), dict):
                    raise ValueError("The registration response belongs to another request.")
                result = returned["result"]
                return decode_registration_frame(self.root, result) if "images" in result else result
            finally:
                request.unlink(missing_ok=True)
                response.unlink(missing_ok=True)

    def close(self) -> None:
        """Request cancellation and reader cleanup without waiting on the caller."""
        if self._closing.is_set():
            return
        self._closing.set()
        if not self._finished.is_set():
            (self.root / "close").touch(exist_ok=True)

    def worker_finished(self, error: str | None = None) -> None:
        """Wake waiting requests after the controller confirms process completion."""
        self._error = error
        self._finished.set()

    def cleanup(self) -> None:
        """Remove only this exchange, after its native process has stopped."""
        if not self._finished.is_set():
            raise RuntimeError("Wait for the registration process before removing its exchange.")
        with self._lock:
            shutil.rmtree(self.root, ignore_errors=True)


def create_registration_session(project: Path, display: dict, *, paired_views=(),
                                appearance_path=None, remote_ticket_sha256=None) -> RegistrationEditorSession:
    """Prepare the registered native script and explicit settings for one pair.

    The controller supplies an authenticated pending or accepted display request.
    DesktopBootstrap validates its mode, IDs and candidate checksum. Accepted
    overlays are additionally matched against the passed approval records before
    the native worker opens their saved transformation.
    """
    from .desktop_bootstrap import prepare_desktop_bootstrap
    from .scripts import QuPathScriptAsset

    if display.get("mode") not in {"alignment", "candidate_overlay", "overlay"}:
        raise ValueError("Select an alignment editor or saved alignment review.")
    if display["mode"] == "overlay" and not any(
            item.get("fixed_entry_id") == display.get("fixed_entry_id")
            and item.get("moving_entry_id") == display.get("moving_entry_id")
            for item in paired_views):
        raise ValueError("This alignment is not an approved paired-image view.")
    desktop = prepare_desktop_bootstrap(project)
    if desktop is None:
        raise ValueError("Open registration from the current pipeline analysis.")
    encoded = desktop.encoded_request(tuple(paired_views), reference_display=display,
                                      image_appearance_path=appearance_path)
    request = json.loads(base64.urlsafe_b64decode(encoded))
    if remote_ticket_sha256 is not None:
        import re
        if display.get("mode") != "alignment" or not re.fullmatch("[0-9a-f]{64}", remote_ticket_sha256):
            raise ValueError("Remote alignment requires the verified manual image-pair ticket.")
        request["remote_ticket_sha256"] = remote_ticket_sha256
    logs = project.parent / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    root = Path(mkdtemp(prefix="registration-editor-", dir=logs))
    request["exchange_directory"] = str(root)
    request_path = root / "setup.json"
    request_path.write_text(json.dumps(request, allow_nan=False), encoding="utf-8")
    # prepare_desktop_bootstrap authenticated the complete registered bundle.
    script = desktop.bundle_root / Path(QuPathScriptAsset.REGISTRATION_EDITOR.value).relative_to("qupath/scripts")
    return RegistrationEditorSession(root, project, script, request_path, dict(display),
                                     Path(appearance_path) if appearance_path is not None else None)
