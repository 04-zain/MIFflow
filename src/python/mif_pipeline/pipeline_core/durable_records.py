"""Exact durable publication primitives for canonical immutable JSON records.

The `RuntimeAuthorityBindingV2` record carries `authority_layout_sha256`,
`workspace_storage_scope_sha256`, `script_ref`, `request_ref`, `evidence_ref` and its
remaining declared fields.

These records and helpers define shared values used by several stages. Construction and
parsing validate the declared fields before the values are used to coordinate scientific
inputs, results or runtime resources."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Callable, ContextManager, Mapping, Protocol

from .errors import ROIAnalysisError

# ==============================================================================
#                                Durable records
# ==============================================================================


class DurableRecordError(ROIAnalysisError):
    """A durable record is unsafe, noncanonical, changed, or not synchronized."""


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class RuntimeAuthorityBindingV2:
    """Closed authority-v2 inputs for one external QuPath invocation."""

    authority_layout_sha256: str
    workspace_storage_scope_sha256: str
    script_ref: object
    request_ref: object
    evidence_ref: object
    stdout_ref: object
    stderr_ref: object

    def __post_init__(self) -> None:
        # Local import avoids the intentional authority-layout -> durable-record
        # dependency from becoming a module import cycle.
        """Validate the RuntimeAuthorityBindingV2 fields when the record is constructed.

        The checks cover ``script_ref``, ``request_ref``, ``evidence_ref``, ``stdout_ref``,
        ``stderr_ref``, ``authority_layout_sha256``. Invalid field types, values or
        combinations raise the declared validation error before this instance can be used by
        its caller."""

        from .authority_layout import AuthorityMemberRefV2

        if (
            type(self.authority_layout_sha256) is not str
            or _SHA256.fullmatch(self.authority_layout_sha256) is None
            or type(self.workspace_storage_scope_sha256) is not str
            or _SHA256.fullmatch(self.workspace_storage_scope_sha256) is None
        ):
            raise DurableRecordError("runtime authority binding digest is invalid")
        references = (
            self.script_ref,
            self.request_ref,
            self.evidence_ref,
            self.stdout_ref,
            self.stderr_ref,
        )
        if any(type(ref) is not AuthorityMemberRefV2 for ref in references):
            raise DurableRecordError(
                "runtime authority binding contains a foreign member reference"
            )
        if any(ref.authority_layout_sha256 != self.authority_layout_sha256 for ref in references):
            raise DurableRecordError("runtime authority binding spans multiple authority layouts")
        if (
            self.script_ref.family != "runtime_asset"
            or self.request_ref.family != "runtime_exchange"
            or self.evidence_ref.family != "runtime_exchange"
            or self.stdout_ref.family != "runtime_log"
            or self.stderr_ref.family != "runtime_log"
            or self.request_ref == self.evidence_ref
            or self.stdout_ref == self.stderr_ref
        ):
            raise DurableRecordError("runtime authority binding member families are invalid")


class _ReservedAuthorityLogStream:
    """Caller-owned log writer with an authenticated bounded read view."""

    def __init__(
        self,
        stream: object,
        reader: Callable[[], ContextManager[object]],
    ) -> None:
        self._stream = stream
        self._reader = reader

    def __getattr__(self, name: str) -> object:
        return getattr(self._stream, name)

    def fileno(self) -> int:
        return self._stream.fileno()  # type: ignore[no-any-return,attr-defined]

    def write(self, value: bytes) -> int:
        return self._stream.write(value)  # type: ignore[no-any-return,attr-defined]

    def flush(self) -> None:
        self._stream.flush()  # type: ignore[attr-defined]

    def close(self) -> None:
        self._stream.close()  # type: ignore[attr-defined]

    def open_reader(self) -> ContextManager[object]:
        self.flush()
        return self._reader()


@dataclass(slots=True)
class ReservedAuthorityLogPair:
    """Two pre-reserved log streams whose close is exhaustive and ordered."""

    stdout: object
    stderr: object
    _closed: bool = field(default=False, init=False, repr=False)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        failure: BaseException | None = None
        for stream in (self.stderr, self.stdout):
            try:
                stream.close()  # type: ignore[attr-defined]
            except BaseException as cleanup:
                if failure is None:
                    failure = cleanup
                elif isinstance(failure, Exception) and not isinstance(cleanup, Exception):
                    _preserve_primary_failure(
                        cleanup,
                        failure,
                        context="earlier authority log close failure",
                    )
                    failure = cleanup
                else:
                    _preserve_primary_failure(
                        failure,
                        cleanup,
                        context="additional authority log close failure",
                    )
        if failure is not None:
            raise failure

    def __enter__(self) -> "ReservedAuthorityLogPair":
        return self

    def __exit__(self, *_exc: object) -> None:
        active = _exc[1] if len(_exc) > 1 else None
        try:
            self.close()
        except BaseException as cleanup:
            _handle_context_cleanup_failure(
                active,
                cleanup,
                context="authority log-pair cleanup failure",
            )


def _preserve_primary_failure(
    primary: BaseException,
    secondary: BaseException,
    *,
    context: str,
) -> None:
    """Best-effort annotate without requiring Python 3.11 ``add_note``."""

    add_note = getattr(primary, "add_note", None)
    if not callable(add_note):
        return
    try:
        add_note(f"{context}: {secondary!r}"[:512])
    except BaseException:
        # Diagnostic annotation can never replace the operational primary.
        return


def _handle_context_cleanup_failure(
    active: object,
    cleanup: BaseException,
    *,
    context: str,
) -> None:
    """Keep cancellation-like failures primary across context cleanup."""

    if isinstance(active, BaseException):
        if isinstance(active, Exception) and not isinstance(cleanup, Exception):
            _preserve_primary_failure(cleanup, active, context=context)
            raise cleanup from active
        _preserve_primary_failure(active, cleanup, context=context)
        return
    raise cleanup


def canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise DurableRecordError("durable record is not canonical JSON") from exc


def _validate_canonical_payload(
    payload: bytes, *, max_bytes: int, label: str
) -> Mapping[str, object]:
    if not isinstance(payload, bytes):
        raise DurableRecordError(f"{label} payload must be bytes")
    if len(payload) > max_bytes:
        raise DurableRecordError(f"{label} exceeds its {max_bytes}-byte maximum")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DurableRecordError(f"{label} is invalid JSON") from exc
    if not isinstance(value, Mapping) or canonical_json_bytes(value) != payload:
        raise DurableRecordError(f"{label} is not canonical JSON")
    return value


class _PinnedDirectoryAuthority:
    """Retain every POSIX ancestor and address children from the parent fd."""

    def __init__(self, path: Path, *, create: bool) -> None:
        if os.name != "posix":
            raise DurableRecordError("descriptor-relative directory authority requires POSIX")
        original = Path(path)
        if ".." in original.parts:
            raise DurableRecordError("durable record path contains traversal")
        absolute = original if original.is_absolute() else Path.cwd() / original
        self.path = absolute
        self._descriptors: list[int] = []
        self._paths: list[Path] = []
        self._identities: list[tuple[int, int]] = []
        self._parent_authority: _PinnedDirectoryAuthority | None = None
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(Path(absolute.anchor), flags)
            self._retain(Path(absolute.anchor), descriptor)
            current = Path(absolute.anchor)
            for component in absolute.parts[1:]:
                if component in {"", ".", ".."}:
                    raise DurableRecordError("durable record authority path is invalid")
                try:
                    child = os.open(component, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                    child = os.open(component, flags, dir_fd=descriptor)
                current = current / component
                descriptor = child
                self._retain(current, descriptor)
            self.validate()
        except DurableRecordError:
            self.close()
            raise
        except OSError as exc:
            self.close()
            raise DurableRecordError("durable record directory authority is unavailable") from exc

    def _retain(self, path: Path, descriptor: int) -> None:
        try:
            metadata = os.fstat(descriptor)
        except OSError:
            os.close(descriptor)
            raise
        if not stat.S_ISDIR(metadata.st_mode):
            os.close(descriptor)
            raise DurableRecordError("durable record authority ancestor is not a directory")
        self._descriptors.append(descriptor)
        self._paths.append(path)
        self._identities.append((metadata.st_dev, metadata.st_ino))

    @property
    def descriptor(self) -> int:
        if not self._descriptors:
            raise DurableRecordError("durable record directory authority is closed")
        return self._descriptors[-1]

    @property
    def identity(self) -> tuple[int, int]:
        if not self._identities:
            raise DurableRecordError("durable record directory authority is closed")
        return self._identities[-1]

    def validate(self) -> None:
        if self._parent_authority is not None:
            self._parent_authority.validate()
        if len(self._descriptors) != len(self._paths) or not self._descriptors:
            raise DurableRecordError("durable record directory authority is closed")
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        for path, descriptor, identity in zip(
            self._paths,
            self._descriptors,
            self._identities,
            strict=True,
        ):
            try:
                retained = os.fstat(descriptor)
                current = path.lstat()
            except OSError as exc:
                raise DurableRecordError("durable record authority ancestor was replaced") from exc
            if (
                not stat.S_ISDIR(retained.st_mode)
                or not stat.S_ISDIR(current.st_mode)
                or stat.S_ISLNK(current.st_mode)
                or getattr(current, "st_file_attributes", 0) & reparse_flag
                or (retained.st_dev, retained.st_ino) != identity
                or (current.st_dev, current.st_ino) != identity
            ):
                raise DurableRecordError("durable record authority ancestor was replaced")

    def retain_child(
        self,
        relative_path: Path,
        *,
        create: bool,
    ) -> "_PinnedDirectoryAuthority":
        """Retain a descendant relative to this already pinned authority."""

        self.validate()
        relative = Path(relative_path)
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise DurableRecordError("durable record child authority path is invalid")
        authority = object.__new__(type(self))
        authority.path = self.path.joinpath(relative)
        authority._descriptors = []
        authority._paths = []
        authority._identities = []
        authority._parent_authority = self
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = self.descriptor
        current = self.path
        try:
            for component in relative.parts:
                try:
                    child = os.open(component, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                    child = os.open(component, flags, dir_fd=descriptor)
                current = current / component
                descriptor = child
                authority._retain(current, descriptor)
            self.validate()
            authority.validate()
            return authority
        except DurableRecordError:
            authority.close()
            raise
        except OSError as exc:
            authority.close()
            raise DurableRecordError(
                "durable record child directory authority is unavailable"
            ) from exc

    def synchronize(self) -> None:
        try:
            os.fsync(self.descriptor)
        except OSError as exc:
            raise DurableRecordError("durable record parent could not be synchronized") from exc

    def close(self) -> None:
        failure: BaseException | None = None
        while self._descriptors:
            descriptor = self._descriptors.pop()
            try:
                os.close(descriptor)
            except BaseException as exc:
                if failure is None:
                    failure = exc
                elif isinstance(failure, Exception) and not isinstance(exc, Exception):
                    _preserve_primary_failure(
                        exc,
                        failure,
                        context="earlier durable directory close failure",
                    )
                    failure = exc
                else:
                    _preserve_primary_failure(
                        failure,
                        exc,
                        context="additional durable directory close failure",
                    )
        if failure is not None:
            raise failure

    def __del__(self) -> None:
        try:
            self.close()
        except (AttributeError, OSError, DurableRecordError):
            pass

    def __enter__(self) -> "_PinnedDirectoryAuthority":
        return self

    def __exit__(self, *_exc: object) -> None:
        active = _exc[1] if len(_exc) > 1 else None
        try:
            self.close()
        except BaseException as cleanup:
            _handle_context_cleanup_failure(
                active,
                cleanup,
                context="durable directory authority cleanup failure",
            )


class DurableFilesystemStrategy(Protocol):
    """Cone-visible retained cross-platform file-authority strategy.

    Callers use :class:`DurableDirectoryAuthority`; they never reach into a
    strategy's retained implementation object.  The path bridge is bounded to
    one validated leaf, and paired log reservation retains both exact writers.
    """

    def retain_directory(self, path: Path, *, create: bool) -> object: ...

    def retain_child_directory(
        self, retained: object, relative_path: Path, *, create: bool
    ) -> object: ...

    def validate_directory(self, retained: object) -> None: ...

    def directory_identity(self, retained: object) -> object: ...

    def directory_descriptor(self, retained: object) -> int: ...

    def synchronize_directory(self, retained: object) -> None: ...

    def close_directory(self, retained: object) -> None: ...

    def read_immutable(
        self,
        retained: object,
        name: str,
        *,
        max_bytes: int,
        label: str,
        optional: bool = False,
    ) -> bytes | None: ...

    def publish_immutable(
        self,
        retained: object,
        name: str,
        payload: bytes,
        *,
        max_bytes: int,
        label: str,
    ) -> None: ...

    def read_bytes(
        self,
        retained: object,
        name: str,
        *,
        max_bytes: int,
        label: str,
        optional: bool = False,
    ) -> bytes | None: ...

    def publish_bytes(
        self,
        retained: object,
        name: str,
        payload: bytes,
        *,
        max_bytes: int,
        label: str,
    ) -> None: ...

    def replace_pointer_cas(
        self,
        pointer_retained: object,
        pointer_name: str,
        immutable_retained: object,
        immutable_name: str,
        *,
        expected_predecessor_bytes: bytes | None,
        max_bytes: int,
        label: str,
    ) -> bytes: ...

    def remove_durable(self, retained: object, name: str) -> None: ...

    def _member_path(self, retained: object, name: str) -> Path: ...

    def reserve_log_pair(
        self,
        retained: object,
        stdout_name: str,
        stderr_name: str,
    ) -> ReservedAuthorityLogPair: ...


class DurableDirectoryAuthority:
    """Strategy-neutral owner-retained directory capability.

    The injected strategy is private construction infrastructure.  Existing
    POSIX callers retain the same public constructor and descriptor surface;
    Win32 callers retain an opaque HANDLE-backed authority instead.
    """

    def __init__(
        self,
        path: Path,
        *,
        create: bool,
        _strategy: DurableFilesystemStrategy | None = None,
    ) -> None:
        strategy = _strategy if _strategy is not None else _default_strategy()
        original = Path(path)
        try:
            retained = strategy.retain_directory(original, create=create)
        except DurableRecordError:
            raise
        except Exception as exc:
            raise DurableRecordError("durable record directory authority is unavailable") from exc
        self._strategy = strategy
        self._retained: object | None = retained
        self.path = Path(getattr(retained, "path", original))
        self._parent_authority: DurableDirectoryAuthority | None = None

    @classmethod
    def _from_retained(
        cls,
        *,
        path: Path,
        strategy: DurableFilesystemStrategy,
        retained: object,
        parent: "DurableDirectoryAuthority",
    ) -> "DurableDirectoryAuthority":
        authority = object.__new__(cls)
        authority._strategy = strategy
        authority._retained = retained
        authority.path = Path(getattr(retained, "path", path))
        authority._parent_authority = parent
        return authority

    def _require_retained(self) -> object:
        if self._retained is None:
            raise DurableRecordError("durable record directory authority is closed")
        return self._retained

    @property
    def descriptor(self) -> int:
        retained = self._require_retained()
        try:
            return self._strategy.directory_descriptor(retained)
        except DurableRecordError:
            raise
        except Exception as exc:
            raise DurableRecordError("durable record strategy has no directory descriptor") from exc

    @property
    def identity(self) -> object:
        retained = self._require_retained()
        try:
            return self._strategy.directory_identity(retained)
        except DurableRecordError:
            raise
        except Exception as exc:
            raise DurableRecordError("durable record directory identity is unavailable") from exc

    def validate(self) -> None:
        retained = self._require_retained()
        try:
            self._strategy.validate_directory(retained)
        except DurableRecordError:
            raise
        except Exception as exc:
            raise DurableRecordError("durable record authority ancestor was replaced") from exc

    def retain_child(self, relative_path: Path, *, create: bool) -> "DurableDirectoryAuthority":
        self.validate()
        relative = Path(relative_path)
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise DurableRecordError("durable record child authority path is invalid")
        try:
            retained = self._strategy.retain_child_directory(
                self._require_retained(), relative, create=create
            )
        except DurableRecordError:
            raise
        except Exception as exc:
            raise DurableRecordError(
                "durable record child directory authority is unavailable"
            ) from exc
        return type(self)._from_retained(
            path=self.path / relative,
            strategy=self._strategy,
            retained=retained,
            parent=self,
        )

    def synchronize(self) -> None:
        retained = self._require_retained()
        try:
            self._strategy.synchronize_directory(retained)
        except DurableRecordError:
            raise
        except Exception as exc:
            raise DurableRecordError("durable record parent could not be synchronized") from exc

    def read_immutable(
        self,
        name: str,
        *,
        max_bytes: int,
        label: str,
        optional: bool = False,
    ) -> bytes | None:
        retained = self._require_retained()
        try:
            payload = self._strategy.read_immutable(
                retained,
                name,
                max_bytes=max_bytes,
                label=label,
                optional=optional,
            )
        except DurableRecordError:
            raise
        except Exception as exc:
            raise DurableRecordError(f"{label} cannot be read safely") from exc
        if payload is None:
            if optional:
                return None
            raise DurableRecordError(f"{label} is unavailable")
        if not isinstance(payload, bytes):
            raise DurableRecordError(f"{label} payload must be bytes")
        if len(payload) > max_bytes:
            raise DurableRecordError(f"{label} exceeds its {max_bytes}-byte maximum")
        return payload

    def publish_immutable(
        self,
        name: str,
        payload: bytes,
        *,
        max_bytes: int,
        label: str,
    ) -> None:
        if not isinstance(payload, bytes):
            raise DurableRecordError(f"{label} payload must be bytes")
        if len(payload) > max_bytes:
            raise DurableRecordError(f"{label} exceeds its {max_bytes}-byte maximum")
        retained = self._require_retained()
        try:
            self._strategy.publish_immutable(
                retained,
                name,
                payload,
                max_bytes=max_bytes,
                label=label,
            )
        except DurableRecordError:
            raise
        except Exception as exc:
            raise DurableRecordError(f"{label} cannot be published safely") from exc

    def read_bytes(
        self,
        name: str,
        *,
        max_bytes: int,
        label: str,
        optional: bool = False,
    ) -> bytes | None:
        retained = self._require_retained()
        try:
            payload = self._strategy.read_bytes(
                retained,
                name,
                max_bytes=max_bytes,
                label=label,
                optional=optional,
            )
        except DurableRecordError:
            raise
        except Exception as exc:
            raise DurableRecordError(f"{label} cannot be read safely") from exc
        if payload is None:
            if optional:
                return None
            raise DurableRecordError(f"{label} is unavailable")
        if type(payload) is not bytes or len(payload) > max_bytes:
            raise DurableRecordError(f"{label} payload is invalid")
        return payload

    def publish_bytes(
        self,
        name: str,
        payload: bytes,
        *,
        max_bytes: int,
        label: str,
    ) -> None:
        if type(payload) is not bytes or len(payload) > max_bytes:
            raise DurableRecordError(f"{label} payload is invalid")
        retained = self._require_retained()
        try:
            self._strategy.publish_bytes(
                retained,
                name,
                payload,
                max_bytes=max_bytes,
                label=label,
            )
        except DurableRecordError:
            raise
        except Exception as exc:
            raise DurableRecordError(f"{label} cannot be published safely") from exc

    def replace_pointer_cas(
        self,
        pointer_name: str,
        immutable_authority: "DurableDirectoryAuthority",
        immutable_name: str,
        *,
        expected_predecessor_bytes: bytes | None,
        max_bytes: int,
        label: str,
    ) -> bytes:
        if (
            not isinstance(immutable_authority, DurableDirectoryAuthority)
            or self._strategy is not immutable_authority._strategy
        ):
            raise DurableRecordError(f"{label} retained directory authorities do not match")
        try:
            result = self._strategy.replace_pointer_cas(
                self._require_retained(),
                pointer_name,
                immutable_authority._require_retained(),
                immutable_name,
                expected_predecessor_bytes=expected_predecessor_bytes,
                max_bytes=max_bytes,
                label=label,
            )
        except DurableRecordError:
            raise
        except Exception as exc:
            raise DurableRecordError(f"{label} could not be replaced atomically") from exc
        if not isinstance(result, bytes):
            raise DurableRecordError(f"{label} payload must be bytes")
        _validate_canonical_payload(result, max_bytes=max_bytes, label=label)
        return result

    def remove_durable(self, name: str) -> None:
        """Fail closed unless a strategy can authenticate physical removal."""

        try:
            self._strategy.remove_durable(self._require_retained(), name)
        except DurableRecordError:
            raise
        except Exception as exc:
            raise DurableRecordError(
                "durable physical removal could not be authenticated; "
                "publish a logical retirement record"
            ) from exc

    def _member_path(self, name: str) -> Path:
        """Expose one bounded member path only for an external-process bridge."""

        try:
            path = self._strategy._member_path(self._require_retained(), name)
        except DurableRecordError:
            raise
        except Exception as exc:
            raise DurableRecordError("durable authority member path is unavailable") from exc
        if not isinstance(path, Path):
            raise DurableRecordError("durable authority member path is invalid")
        return path

    def reserve_log_pair(
        self,
        stdout_name: str,
        stderr_name: str,
    ) -> ReservedAuthorityLogPair:
        """Exclusively reserve both caller-owned external-process log leaves."""

        if stdout_name == stderr_name:
            raise DurableRecordError("authority log members must be distinct")
        try:
            pair = self._strategy.reserve_log_pair(
                self._require_retained(), stdout_name, stderr_name
            )
        except DurableRecordError:
            raise
        except Exception as exc:
            raise DurableRecordError("authority log pair cannot be reserved safely") from exc
        if type(pair) is not ReservedAuthorityLogPair:
            raise DurableRecordError("authority log strategy returned an invalid reservation")
        return pair

    def close(self) -> None:
        retained = getattr(self, "_retained", None)
        if retained is None:
            return
        self._retained = None
        try:
            self._strategy.close_directory(retained)
        except DurableRecordError:
            raise
        except Exception as exc:
            raise DurableRecordError(
                "durable record directory authority could not be closed"
            ) from exc

    def __del__(self) -> None:
        try:
            self.close()
        except (AttributeError, OSError, DurableRecordError):
            pass

    def __enter__(self) -> "DurableDirectoryAuthority":
        return self

    def __exit__(self, *_exc: object) -> None:
        active = _exc[1] if len(_exc) > 1 else None
        try:
            self.close()
        except BaseException as cleanup:
            _handle_context_cleanup_failure(
                active,
                cleanup,
                context="durable directory authority cleanup failure",
            )


def _relative_file_metadata(
    authority: _PinnedDirectoryAuthority,
    name: str,
    *,
    label: str,
) -> os.stat_result:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise DurableRecordError(f"{label} filename is invalid")
    try:
        return os.stat(
            name,
            dir_fd=authority.descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise DurableRecordError(f"{label} is unavailable") from exc


def _relative_leaf(name: str, *, label: str) -> str:
    if (
        not isinstance(name, str)
        or not name
        or Path(name).name != name
        or name in {".", ".."}
        or "\0" in name
    ):
        raise DurableRecordError(f"{label} filename is invalid")
    return name


@contextmanager
def _open_reserved_log_reader_posix(
    authority: _PinnedDirectoryAuthority,
    leaf: str,
    writer: object,
):
    descriptor: int | None = None
    reader: object | None = None
    try:
        authority.validate()
        descriptor = os.open(
            leaf,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            dir_fd=authority.descriptor,
        )
        writer_identity = os.fstat(writer.fileno())  # type: ignore[attr-defined]
        reader_identity = os.fstat(descriptor)
        if (
            not stat.S_ISREG(reader_identity.st_mode)
            or reader_identity.st_nlink != 1
            or (reader_identity.st_dev, reader_identity.st_ino)
            != (writer_identity.st_dev, writer_identity.st_ino)
        ):
            raise DurableRecordError("authority log member changed")
        reader = os.fdopen(descriptor, "rb")
        descriptor = None
        yield reader
    finally:
        if reader is not None:
            reader.close()  # type: ignore[attr-defined]
        if descriptor is not None:
            os.close(descriptor)


def _read_bounded_file_relative(
    authority: _PinnedDirectoryAuthority,
    name: str,
    *,
    max_bytes: int,
    label: str,
) -> bytes:
    authority.validate()
    before = _relative_file_metadata(authority, name, label=label)
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size > max_bytes
    ):
        raise DurableRecordError(f"{label} is not one bounded regular file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=authority.descriptor)
    except OSError as exc:
        raise DurableRecordError(f"{label} cannot be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_size > max_bytes
        ):
            raise DurableRecordError(f"{label} changed before bounded read")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(payload) > max_bytes
            or len(payload) != opened.st_size
            or (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_nlink,
            )
            != (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_nlink,
            )
            or after.st_nlink != 1
        ):
            raise DurableRecordError(f"{label} changed during bounded read")
    except OSError as exc:
        raise DurableRecordError(f"{label} cannot be read safely") from exc
    finally:
        os.close(descriptor)
    authority.validate()
    return payload


def _read_canonical_record_relative(
    authority: _PinnedDirectoryAuthority,
    name: str,
    *,
    max_bytes: int,
    label: str,
) -> tuple[bytes, Mapping[str, object]]:
    payload = _read_bounded_file_relative(
        authority,
        name,
        max_bytes=max_bytes,
        label=label,
    )
    return payload, _validate_canonical_payload(payload, max_bytes=max_bytes, label=label)


def _read_optional_pointer_relative(
    authority: _PinnedDirectoryAuthority,
    name: str,
    *,
    max_bytes: int,
    label: str,
) -> bytes | None:
    authority.validate()
    try:
        os.stat(
            name,
            dir_fd=authority.descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        authority.validate()
        return None
    except OSError as exc:
        raise DurableRecordError(f"{label} cannot be inspected") from exc
    payload, _ = _read_canonical_record_relative(authority, name, max_bytes=max_bytes, label=label)
    return payload


def _create_relative_temporary(
    authority: _PinnedDirectoryAuthority,
    pointer_name: str,
) -> tuple[int, str]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    for _ in range(100):
        name = f".{pointer_name}.{secrets.token_hex(16)}"
        try:
            return (
                os.open(
                    name,
                    flags,
                    0o600,
                    dir_fd=authority.descriptor,
                ),
                name,
            )
        except FileExistsError:
            continue
        except OSError as exc:
            raise DurableRecordError("durable record temporary cannot be created safely") from exc
    raise DurableRecordError("durable record temporary name space is exhausted")


class _PosixDurableFilesystemStrategy:
    """The existing openat/fstat/fsync implementation behind the strategy seam."""

    def retain_directory(self, path: Path, *, create: bool) -> object:
        return _PinnedDirectoryAuthority(path, create=create)

    def retain_child_directory(
        self, retained: object, relative_path: Path, *, create: bool
    ) -> object:
        if not isinstance(retained, _PinnedDirectoryAuthority):
            raise DurableRecordError("POSIX directory authority is invalid")
        return retained.retain_child(relative_path, create=create)

    def validate_directory(self, retained: object) -> None:
        if not isinstance(retained, _PinnedDirectoryAuthority):
            raise DurableRecordError("POSIX directory authority is invalid")
        retained.validate()

    def directory_identity(self, retained: object) -> object:
        if not isinstance(retained, _PinnedDirectoryAuthority):
            raise DurableRecordError("POSIX directory authority is invalid")
        return retained.identity

    def directory_descriptor(self, retained: object) -> int:
        if not isinstance(retained, _PinnedDirectoryAuthority):
            raise DurableRecordError("POSIX directory authority is invalid")
        return retained.descriptor

    def synchronize_directory(self, retained: object) -> None:
        if not isinstance(retained, _PinnedDirectoryAuthority):
            raise DurableRecordError("POSIX directory authority is invalid")
        retained.synchronize()

    def close_directory(self, retained: object) -> None:
        if not isinstance(retained, _PinnedDirectoryAuthority):
            raise DurableRecordError("POSIX directory authority is invalid")
        retained.close()

    def read_immutable(
        self,
        retained: object,
        name: str,
        *,
        max_bytes: int,
        label: str,
        optional: bool = False,
    ) -> bytes | None:
        if not isinstance(retained, _PinnedDirectoryAuthority):
            raise DurableRecordError("POSIX directory authority is invalid")
        if optional:
            return _read_optional_pointer_relative(retained, name, max_bytes=max_bytes, label=label)
        payload, _ = _read_canonical_record_relative(
            retained, name, max_bytes=max_bytes, label=label
        )
        return payload

    def publish_immutable(
        self,
        retained: object,
        name: str,
        payload: bytes,
        *,
        max_bytes: int,
        label: str,
    ) -> None:
        self._publish_bytes(
            retained,
            name,
            payload,
            max_bytes=max_bytes,
            label=label,
            canonical_json=True,
        )

    def read_bytes(
        self,
        retained: object,
        name: str,
        *,
        max_bytes: int,
        label: str,
        optional: bool = False,
    ) -> bytes | None:
        if not isinstance(retained, _PinnedDirectoryAuthority):
            raise DurableRecordError("POSIX directory authority is invalid")
        if optional:
            retained.validate()
            try:
                os.stat(
                    _relative_leaf(name, label=label),
                    dir_fd=retained.descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                retained.validate()
                return None
            except OSError as exc:
                raise DurableRecordError(f"{label} cannot be inspected") from exc
        return _read_bounded_file_relative(retained, name, max_bytes=max_bytes, label=label)

    def publish_bytes(
        self,
        retained: object,
        name: str,
        payload: bytes,
        *,
        max_bytes: int,
        label: str,
    ) -> None:
        self._publish_bytes(
            retained,
            name,
            payload,
            max_bytes=max_bytes,
            label=label,
            canonical_json=False,
        )

    def _publish_bytes(
        self,
        retained: object,
        name: str,
        payload: bytes,
        *,
        max_bytes: int,
        label: str,
        canonical_json: bool,
    ) -> None:
        if not isinstance(retained, _PinnedDirectoryAuthority):
            raise DurableRecordError("POSIX directory authority is invalid")
        _relative_leaf(name, label=label)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(
                name,
                flags,
                0o600,
                dir_fd=retained.descriptor,
            )
        except FileExistsError:
            if canonical_json:
                existing, _ = _read_canonical_record_relative(
                    retained,
                    name,
                    max_bytes=max_bytes,
                    label=label,
                )
            else:
                existing = _read_bounded_file_relative(
                    retained,
                    name,
                    max_bytes=max_bytes,
                    label=label,
                )
            if existing != payload:
                raise DurableRecordError(f"{label} immutable bytes conflict")
            return
        except OSError as exc:
            raise DurableRecordError(f"{label} cannot be created safely") from exc
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise DurableRecordError(f"{label} could not be synchronized") from exc
        # A failed write/fsync deliberately leaves a conflicting record.
        retained.synchronize()
        if canonical_json:
            reopened, _ = _read_canonical_record_relative(
                retained,
                name,
                max_bytes=max_bytes,
                label=label,
            )
        else:
            reopened = _read_bounded_file_relative(
                retained,
                name,
                max_bytes=max_bytes,
                label=label,
            )
        if reopened != payload:
            raise DurableRecordError(f"{label} immutable bytes changed")
        retained.validate()

    def replace_pointer_cas(
        self,
        pointer_retained: object,
        pointer_name: str,
        immutable_retained: object,
        immutable_name: str,
        *,
        expected_predecessor_bytes: bytes | None,
        max_bytes: int,
        label: str,
    ) -> bytes:
        if not isinstance(pointer_retained, _PinnedDirectoryAuthority) or not isinstance(
            immutable_retained, _PinnedDirectoryAuthority
        ):
            raise DurableRecordError("POSIX directory authority is invalid")
        return _replace_pointer_with_immutable_posix(
            Path(pointer_retained.path) / pointer_name,
            Path(immutable_retained.path) / immutable_name,
            pointer_authority=pointer_retained,
            immutable_authority=immutable_retained,
            expected_predecessor_bytes=expected_predecessor_bytes,
            max_bytes=max_bytes,
            label=label,
        )

    def remove_durable(self, retained: object, name: str) -> None:
        if not isinstance(retained, _PinnedDirectoryAuthority):
            raise DurableRecordError("POSIX directory authority is invalid")
        raise DurableRecordError(
            "POSIX name-only unlink cannot authenticate the object removed; "
            "publish a logical retirement record"
        )

    def _member_path(self, retained: object, name: str) -> Path:
        if not isinstance(retained, _PinnedDirectoryAuthority):
            raise DurableRecordError("POSIX directory authority is invalid")
        leaf = _relative_leaf(name, label="authority member")
        retained.validate()
        return retained.path / leaf

    def reserve_log_pair(
        self,
        retained: object,
        stdout_name: str,
        stderr_name: str,
    ) -> ReservedAuthorityLogPair:
        if not isinstance(retained, _PinnedDirectoryAuthority):
            raise DurableRecordError("POSIX directory authority is invalid")
        stdout_leaf = _relative_leaf(stdout_name, label="stdout log")
        stderr_leaf = _relative_leaf(stderr_name, label="stderr log")
        if stdout_leaf == stderr_leaf:
            raise DurableRecordError("authority log members must be distinct")
        retained.validate()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        streams: list[_ReservedAuthorityLogStream] = []
        try:
            for leaf in (stdout_leaf, stderr_leaf):
                descriptor = os.open(
                    leaf,
                    flags,
                    0o600,
                    dir_fd=retained.descriptor,
                )
                stream: object | None = None
                try:
                    opened = os.fstat(descriptor)
                    named = os.stat(
                        leaf,
                        dir_fd=retained.descriptor,
                        follow_symlinks=False,
                    )
                    if (
                        not stat.S_ISREG(opened.st_mode)
                        or opened.st_nlink != 1
                        or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
                    ):
                        raise DurableRecordError(
                            "authority log member is not one bounded regular file"
                        )
                    stream = os.fdopen(descriptor, "wb")
                    descriptor = -1
                    streams.append(
                        _ReservedAuthorityLogStream(
                            stream,
                            lambda leaf=leaf, stream=stream: (
                                _open_reserved_log_reader_posix(retained, leaf, stream)
                            ),
                        )
                    )
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
            retained.synchronize()
            retained.validate()
            return ReservedAuthorityLogPair(stdout=streams[0], stderr=streams[1])
        except BaseException as primary:
            for stream in reversed(streams):
                try:
                    stream.close()
                except BaseException as cleanup:
                    _preserve_primary_failure(
                        primary,
                        cleanup,
                        context="authority log reservation cleanup failure",
                    )
            raise


_POSIX_STRATEGY = _PosixDurableFilesystemStrategy()


def _default_strategy() -> DurableFilesystemStrategy:
    if os.name == "posix":
        return _POSIX_STRATEGY
    if os.name == "nt":
        # Keep ctypes, WinDLL, and msvcrt absent from Linux imports.
        from .win32_file_authority import Win32FileAuthority

        return Win32FileAuthority()
    raise DurableRecordError("durable record filesystem authority is unsupported")


def read_canonical_record(
    path: Path,
    *,
    max_bytes: int,
    label: str,
    _strategy: DurableFilesystemStrategy | None = None,
) -> tuple[bytes, Mapping[str, object]]:
    """No-follow reopen one bounded, canonical, single-link regular file."""
    path = Path(path)
    with DurableDirectoryAuthority(path.parent, create=False, _strategy=_strategy) as authority:
        try:
            payload = authority.read_immutable(
                path.name,
                max_bytes=max_bytes,
                label=label,
            )
        except DurableRecordError:
            raise
        except Exception as exc:
            raise DurableRecordError(f"{label} cannot be read safely") from exc
    if not isinstance(payload, bytes):
        raise DurableRecordError(f"{label} payload must be bytes")
    return payload, _validate_canonical_payload(payload, max_bytes=max_bytes, label=label)


def publish_immutable_record(
    path: Path,
    payload: bytes,
    *,
    max_bytes: int,
    label: str,
    _strategy: DurableFilesystemStrategy | None = None,
) -> None:
    """Exclusively publish and synchronize one exact canonical record."""
    _validate_canonical_payload(payload, max_bytes=max_bytes, label=label)
    path = Path(path)
    with DurableDirectoryAuthority(path.parent, create=True, _strategy=_strategy) as authority:
        try:
            authority.publish_immutable(
                path.name,
                payload,
                max_bytes=max_bytes,
                label=label,
            )
        except DurableRecordError:
            raise
        except Exception as exc:
            raise DurableRecordError(f"{label} cannot be published safely") from exc


def _restore_pointer_predecessor_relative(
    authority: _PinnedDirectoryAuthority,
    pointer_name: str,
    predecessor_bytes: bytes | None,
    *,
    label: str,
) -> None:
    """Best-effort rollback through the retained directory, never a pathname."""

    temporary_name: str | None = None
    try:
        if predecessor_bytes is None:
            try:
                os.unlink(pointer_name, dir_fd=authority.descriptor)
            except FileNotFoundError:
                pass
        else:
            descriptor, temporary_name = _create_relative_temporary(authority, pointer_name)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(predecessor_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(
                temporary_name,
                pointer_name,
                src_dir_fd=authority.descriptor,
                dst_dir_fd=authority.descriptor,
            )
            temporary_name = None
        os.fsync(authority.descriptor)
    except OSError as exc:
        raise DurableRecordError(f"{label} unsafe publication could not be rolled back") from exc
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=authority.descriptor)
            except FileNotFoundError:
                pass


def _replace_pointer_with_immutable_posix(
    pointer_path: Path,
    immutable_path: Path,
    *,
    pointer_authority: _PinnedDirectoryAuthority,
    immutable_authority: _PinnedDirectoryAuthority,
    expected_predecessor_bytes: bytes | None,
    max_bytes: int,
    label: str,
) -> bytes:
    replaced = False
    try:
        pointer_authority.validate()
        immutable_authority.validate()
        immutable_bytes, _ = _read_canonical_record_relative(
            immutable_authority,
            immutable_path.name,
            max_bytes=max_bytes,
            label=f"{label} immutable generation",
        )
        observed = _read_optional_pointer_relative(
            pointer_authority,
            pointer_path.name,
            max_bytes=max_bytes,
            label=label,
        )
        if observed != expected_predecessor_bytes:
            raise DurableRecordError(f"{label} predecessor bytes changed")
        descriptor, temporary_name = _create_relative_temporary(
            pointer_authority, pointer_path.name
        )
        try:
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(immutable_bytes)
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError as exc:
                raise DurableRecordError(f"{label} temporary could not be synchronized") from exc
            temporary_bytes, _ = _read_canonical_record_relative(
                pointer_authority,
                temporary_name,
                max_bytes=max_bytes,
                label=f"{label} temporary",
            )
            if temporary_bytes != immutable_bytes:
                raise DurableRecordError(f"{label} temporary differs from immutable generation")
            # The live caller lock serializes cooperative writers; this exact
            # descriptor-relative reread closes a stale-predecessor race.
            if (
                _read_optional_pointer_relative(
                    pointer_authority,
                    pointer_path.name,
                    max_bytes=max_bytes,
                    label=label,
                )
                != expected_predecessor_bytes
            ):
                raise DurableRecordError(f"{label} predecessor bytes changed")
            pointer_authority.validate()
            immutable_authority.validate()
            try:
                os.replace(
                    temporary_name,
                    pointer_path.name,
                    src_dir_fd=pointer_authority.descriptor,
                    dst_dir_fd=pointer_authority.descriptor,
                )
                replaced = True
                pointer_authority.synchronize()
            except OSError as exc:
                raise DurableRecordError(f"{label} could not be replaced atomically") from exc
            # Even though descriptor-relative replace cannot be redirected,
            # report a concurrent ancestor swap as a failed publication.
            pointer_authority.validate()
            immutable_authority.validate()
        finally:
            try:
                os.unlink(
                    temporary_name,
                    dir_fd=pointer_authority.descriptor,
                )
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise DurableRecordError(f"{label} temporary could not be removed safely") from exc
        current_bytes, _ = _read_canonical_record_relative(
            pointer_authority,
            pointer_path.name,
            max_bytes=max_bytes,
            label=label,
        )
        immutable_after, _ = _read_canonical_record_relative(
            immutable_authority,
            immutable_path.name,
            max_bytes=max_bytes,
            label=f"{label} immutable generation",
        )
        if current_bytes != immutable_bytes or immutable_after != immutable_bytes:
            raise DurableRecordError(f"{label} is not an exact immutable-generation copy")
        pointer_authority.validate()
        immutable_authority.validate()
        return current_bytes
    except BaseException as publication_error:
        if replaced:
            try:
                _restore_pointer_predecessor_relative(
                    pointer_authority,
                    pointer_path.name,
                    expected_predecessor_bytes,
                    label=label,
                )
            except DurableRecordError as rollback_error:
                raise rollback_error from publication_error
        raise


def replace_pointer_with_immutable(
    pointer_path: Path,
    immutable_path: Path,
    *,
    expected_predecessor_bytes: bytes | None,
    max_bytes: int,
    label: str,
    pointer_authority: DurableDirectoryAuthority | None = None,
    immutable_authority: DurableDirectoryAuthority | None = None,
) -> bytes:
    """CAS-replace a pointer with an exact copy of a named immutable record."""
    pointer_path = Path(pointer_path)
    immutable_path = Path(immutable_path)
    if (
        not isinstance(pointer_authority, DurableDirectoryAuthority)
        or pointer_authority.path not in {pointer_path.parent, pointer_path.parent.absolute()}
        or not isinstance(immutable_authority, DurableDirectoryAuthority)
        or immutable_authority.path not in {immutable_path.parent, immutable_path.parent.absolute()}
    ):
        raise DurableRecordError(f"{label} retained directory authority does not match")
    try:
        result = pointer_authority.replace_pointer_cas(
            pointer_path.name,
            immutable_authority,
            immutable_path.name,
            expected_predecessor_bytes=expected_predecessor_bytes,
            max_bytes=max_bytes,
            label=label,
        )
    except DurableRecordError:
        raise
    except Exception as exc:
        raise DurableRecordError(f"{label} could not be replaced atomically") from exc
    return result


__all__ = [
    "DurableRecordError",
    "DurableDirectoryAuthority",
    "DurableFilesystemStrategy",
    "ReservedAuthorityLogPair",
    "RuntimeAuthorityBindingV2",
    "canonical_json_bytes",
    "publish_immutable_record",
    "read_canonical_record",
    "replace_pointer_with_immutable",
]
