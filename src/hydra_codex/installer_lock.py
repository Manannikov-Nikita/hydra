"""Kernel-released ownership for the per-user installer lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
import errno
import fcntl
import os
from pathlib import Path
import re
import secrets
import stat


LOCK_RECORD = b"hydra-installer-lock/v2\n"
CAPABILITY_RECORD = b"hydra-installer-capability/v1\n"
_NONCE_PATTERN = re.compile(r"[0-9a-f]{64}")
_READ_LIMIT = 128


class InstallerLockError(RuntimeError):
    """Raised when exact installer lifecycle ownership cannot be established."""


@dataclass(frozen=True)
class HeldInstallerLock:
    path: Path
    descriptor: int
    identity: tuple[int, int]
    nonce: str
    capability_path: Path
    capability_identity: tuple[int, int]


def _identity(status: os.stat_result) -> tuple[int, int]:
    return status.st_dev, status.st_ino


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _file_flags(read_write: bool = False) -> int:
    access = os.O_RDWR if read_write else os.O_RDONLY
    return (
        access
        | os.O_NONBLOCK
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _open_private_parent(path: Path) -> int:
    try:
        descriptor = os.open(path.parent, _directory_flags())
    except OSError as error:
        raise InstallerLockError("installer lock parent is invalid") from error
    try:
        status = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(status.st_mode)
            or stat.S_IMODE(status.st_mode) & 0o022
            or status.st_uid != os.getuid()
        ):
            raise InstallerLockError("installer lock parent is invalid")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _read_all(descriptor: int) -> bytes:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        content = os.read(descriptor, _READ_LIMIT + 1)
        if len(content) > _READ_LIMIT or os.read(descriptor, 1):
            raise InstallerLockError("installer lock file is invalid")
        return content
    except OSError as error:
        raise InstallerLockError("installer lock file is invalid") from error


def _validate_file(
    descriptor: int,
    *,
    expected: bytes,
    label: str,
) -> tuple[int, int]:
    try:
        status = os.fstat(descriptor)
    except OSError as error:
        raise InstallerLockError(f"{label} is invalid") from error
    if (
        not stat.S_ISREG(status.st_mode)
        or stat.S_IMODE(status.st_mode) != 0o600
        or status.st_uid != os.getuid()
        or status.st_size != len(expected)
        or _read_all(descriptor) != expected
    ):
        raise InstallerLockError(f"{label} is invalid")
    return _identity(status)


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        try:
            written = os.write(descriptor, content[offset:])
        except OSError as error:
            raise InstallerLockError("installer lock publication failed") from error
        if written <= 0:
            raise InstallerLockError("installer lock publication failed")
        offset += written


def _unlink_exact(
    parent: int,
    name: str,
    identity: tuple[int, int],
) -> None:
    try:
        status = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if _identity(status) != identity:
            return
        os.unlink(name, dir_fd=parent)
    except OSError:
        return


def _publish_complete_file(
    parent: int,
    *,
    name: str,
    content: bytes,
    collision_ok: bool,
) -> None:
    temporary = f".{name}.create.{secrets.token_hex(16)}"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = -1
    temporary_identity: tuple[int, int] | None = None
    try:
        descriptor = os.open(temporary, flags, 0o600, dir_fd=parent)
        _write_all(descriptor, content)
        os.fsync(descriptor)
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or stat.S_IMODE(status.st_mode) != 0o600
            or status.st_uid != os.getuid()
        ):
            raise InstallerLockError("installer lock publication failed")
        temporary_identity = _identity(status)
        try:
            os.link(
                temporary,
                name,
                src_dir_fd=parent,
                dst_dir_fd=parent,
                follow_symlinks=False,
            )
            os.fsync(parent)
        except FileExistsError:
            if not collision_ok:
                raise InstallerLockError(
                    "installer acquisition capability already exists",
                ) from None
        except OSError as error:
            raise InstallerLockError(
                "installer lock publication failed",
            ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_identity is not None:
            _unlink_exact(parent, temporary, temporary_identity)


def _open_canonical(path: Path) -> tuple[int, tuple[int, int]]:
    parent = _open_private_parent(path)
    try:
        try:
            descriptor = os.open(path.name, _file_flags(), dir_fd=parent)
        except FileNotFoundError:
            _publish_complete_file(
                parent,
                name=path.name,
                content=LOCK_RECORD,
                collision_ok=True,
            )
            descriptor = os.open(path.name, _file_flags(), dir_fd=parent)
        except OSError as error:
            raise InstallerLockError("installer lock file is invalid") from error
        try:
            identity = _validate_file(
                descriptor,
                expected=LOCK_RECORD,
                label="installer lock file",
            )
            current = os.stat(
                path.name,
                dir_fd=parent,
                follow_symlinks=False,
            )
            if _identity(current) != identity:
                raise InstallerLockError("installer lock file changed")
            return descriptor, identity
        except Exception:
            os.close(descriptor)
            raise
    finally:
        os.close(parent)


def _publish_capability(
    path: Path,
    nonce: str,
) -> tuple[Path, tuple[int, int]]:
    capability = path.with_name(f".hydra-installer-capability.{nonce}")
    parent = _open_private_parent(path)
    descriptor = -1
    try:
        _publish_complete_file(
            parent,
            name=capability.name,
            content=CAPABILITY_RECORD,
            collision_ok=False,
        )
        descriptor = os.open(capability.name, _file_flags(), dir_fd=parent)
        identity = _validate_file(
            descriptor,
            expected=CAPABILITY_RECORD,
            label="installer acquisition capability",
        )
        current = os.stat(
            capability.name,
            dir_fd=parent,
            follow_symlinks=False,
        )
        if _identity(current) != identity:
            raise InstallerLockError("installer acquisition capability changed")
        return capability, identity
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
            descriptor = -1
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


def acquire_installer_lock(
    path: Path,
    *,
    nonce: str,
) -> HeldInstallerLock:
    """Acquire the immutable canonical file with a nonblocking kernel lock."""
    if _NONCE_PATTERN.fullmatch(nonce) is None:
        raise InstallerLockError("installer lock capability is invalid")
    descriptor, identity = _open_canonical(path)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                raise InstallerLockError(
                    "another installation is in progress",
                ) from None
            raise InstallerLockError("installer kernel lock failed") from error
        capability, capability_identity = _publish_capability(path, nonce)
        return HeldInstallerLock(
            path,
            descriptor,
            identity,
            nonce,
            capability,
            capability_identity,
        )
    except Exception:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(descriptor)
        raise


def _remove_capability(held: HeldInstallerLock) -> None:
    parent = _open_private_parent(held.path)
    descriptor = -1
    try:
        try:
            descriptor = os.open(
                held.capability_path.name,
                _file_flags(),
                dir_fd=parent,
            )
        except OSError as error:
            raise InstallerLockError(
                "installer acquisition capability changed",
            ) from error
        identity = _validate_file(
            descriptor,
            expected=CAPABILITY_RECORD,
            label="installer acquisition capability",
        )
        if identity != held.capability_identity:
            raise InstallerLockError(
                "installer acquisition capability changed",
            )
        current = os.stat(
            held.capability_path.name,
            dir_fd=parent,
            follow_symlinks=False,
        )
        if _identity(current) != held.capability_identity:
            raise InstallerLockError(
                "installer acquisition capability changed",
            )
        os.unlink(held.capability_path.name, dir_fd=parent)
        os.fsync(parent)
    except OSError as error:
        raise InstallerLockError(
            "installer acquisition capability cleanup failed",
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


def release_installer_lock(held: HeldInstallerLock) -> None:
    """Remove only the exact capability and release kernel ownership."""
    cleanup_error: Exception | None = None
    try:
        _remove_capability(held)
    except Exception as error:
        cleanup_error = error
    try:
        fcntl.flock(held.descriptor, fcntl.LOCK_UN)
    except OSError as error:
        cleanup_error = cleanup_error or error
    try:
        os.close(held.descriptor)
    except OSError as error:
        cleanup_error = cleanup_error or error
    if cleanup_error is not None:
        if isinstance(cleanup_error, InstallerLockError):
            raise cleanup_error
        raise InstallerLockError("installer kernel lock cleanup failed") from cleanup_error
