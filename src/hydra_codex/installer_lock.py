"""Crash-recoverable ownership for the per-user installer lock."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys


OWNER_NAME = "owner-v1"
_RECORD_LIMIT = 256
_NONCE_PATTERN = re.compile(r"[0-9a-f]{64}")
_RECORD_PATTERN = re.compile(
    rb"\Ahydra-installer-lock/v1\n"
    rb"pid=([1-9][0-9]{0,19})\n"
    rb"start=((?:darwin:[ -~]{1,72})|(?:linux:[0-9]{1,32}))\n"
    rb"nonce=([0-9a-f]{64})\n\Z",
)


class InstallerLockError(RuntimeError):
    """Raised when installer lock ownership cannot be proven exactly."""


@dataclass(frozen=True)
class LockOwner:
    pid: int
    start: str
    nonce: str

    def render(self) -> bytes:
        return (
            "hydra-installer-lock/v1\n"
            f"pid={self.pid}\n"
            f"start={self.start}\n"
            f"nonce={self.nonce}\n"
        ).encode("ascii")


@dataclass(frozen=True)
class HeldInstallerLock:
    path: Path
    owner: LockOwner
    directory_identity: tuple[int, int]
    owner_identity: tuple[int, int]


ProcessStartQuery = Callable[[int], str | None]


def _process_start_query(pid: int) -> str | None:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise InstallerLockError("installer lock owner is invalid")
    if sys.platform.startswith("linux"):
        stat_path = Path("/proc") / str(pid) / "stat"
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(stat_path, flags)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise InstallerLockError(
                "process identity inspection failed",
            ) from error
        try:
            content = os.read(descriptor, 4097)
            if len(content) > 4096 or os.read(descriptor, 1):
                raise InstallerLockError("process identity inspection failed")
        finally:
            os.close(descriptor)
        try:
            suffix = content.rsplit(b") ", 1)[1].split()
            value = suffix[19]
        except (IndexError, ValueError) as error:
            raise InstallerLockError(
                "process identity inspection failed",
            ) from error
        if not value.isdigit() or len(value) > 32:
            raise InstallerLockError("process identity inspection failed")
        return "linux:" + value.decode("ascii")
    if sys.platform != "darwin":
        raise InstallerLockError("process identity inspection is unavailable")
    ps = next(
        (
            path
            for path in (Path("/bin/ps"), Path("/usr/bin/ps"))
            if path.is_file()
        ),
        None,
    )
    if ps is None:
        raise InstallerLockError("process identity inspection is unavailable")
    try:
        result = subprocess.run(
            (str(ps), "-o", "lstart=", "-p", str(pid)),
            env={
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "LANG": "C",
                "LC_ALL": "C",
            },
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise InstallerLockError("process identity inspection failed") from error
    if len(result.stdout) > 256 or len(result.stderr) > 256:
        raise InstallerLockError("process identity inspection failed")
    if result.returncode == 1 and not result.stdout and not result.stderr:
        return None
    if result.returncode != 0 or result.stderr:
        raise InstallerLockError("process identity inspection failed")
    try:
        value = result.stdout.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise InstallerLockError("process identity inspection failed") from error
    if (
        not value
        or "\n" in value
        or len(value) > 72
        or any(ord(character) < 32 or ord(character) > 126 for character in value)
    ):
        raise InstallerLockError("process identity inspection failed")
    return "darwin:" + value


def process_start_identity(
    pid: int,
    *,
    process_query: ProcessStartQuery | None = None,
) -> str:
    query = _process_start_query if process_query is None else process_query
    value = query(pid)
    if value is None:
        raise InstallerLockError("process identity inspection failed")
    return value


def _parse_owner(content: bytes) -> LockOwner:
    if len(content) > _RECORD_LIMIT:
        raise InstallerLockError("installer lock owner is invalid")
    match = _RECORD_PATTERN.fullmatch(content)
    if match is None:
        raise InstallerLockError("installer lock owner is invalid")
    try:
        owner = LockOwner(
            int(match.group(1)),
            match.group(2).decode("ascii"),
            match.group(3).decode("ascii"),
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise InstallerLockError("installer lock owner is invalid") from error
    if owner.render() != content:
        raise InstallerLockError("installer lock owner is invalid")
    return owner


def _open_lock(
    path: Path,
) -> tuple[
    int,
    int,
    bytes,
    LockOwner,
    tuple[int, int],
    tuple[int, int],
]:
    directory_flags = (
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        directory = os.open(path, directory_flags)
    except OSError as error:
        raise InstallerLockError("installer lock is invalid") from error
    owner_descriptor = -1
    try:
        directory_status = os.fstat(directory)
        if (
            not stat.S_ISDIR(directory_status.st_mode)
            or stat.S_IMODE(directory_status.st_mode) != 0o700
            or (
                hasattr(os, "getuid")
                and directory_status.st_uid != os.getuid()
            )
        ):
            raise InstallerLockError("installer lock is invalid")
        if os.listdir(directory) != [OWNER_NAME]:
            raise InstallerLockError("installer lock has unexpected entries")
        owner_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        owner_descriptor = os.open(
            OWNER_NAME,
            owner_flags,
            dir_fd=directory,
        )
        owner_status = os.fstat(owner_descriptor)
        if (
            not stat.S_ISREG(owner_status.st_mode)
            or stat.S_IMODE(owner_status.st_mode) != 0o600
            or (
                hasattr(os, "getuid")
                and owner_status.st_uid != os.getuid()
            )
            or owner_status.st_size > _RECORD_LIMIT
        ):
            raise InstallerLockError("installer lock owner is invalid")
        content = os.read(owner_descriptor, _RECORD_LIMIT + 1)
        if os.read(owner_descriptor, 1):
            raise InstallerLockError("installer lock owner is invalid")
        owner = _parse_owner(content)
        return (
            directory,
            owner_descriptor,
            content,
            owner,
            (directory_status.st_dev, directory_status.st_ino),
            (owner_status.st_dev, owner_status.st_ino),
        )
    except Exception as error:
        if owner_descriptor >= 0:
            os.close(owner_descriptor)
        os.close(directory)
        if isinstance(error, InstallerLockError):
            raise
        raise InstallerLockError("installer lock owner is invalid") from error


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise InstallerLockError("installer lock owner creation failed")
        offset += written


def _owner_entry_identity(directory: int) -> tuple[int, int]:
    try:
        status = os.stat(
            OWNER_NAME,
            dir_fd=directory,
            follow_symlinks=False,
        )
    except OSError as error:
        raise InstallerLockError("installer lock ownership changed") from error
    return status.st_dev, status.st_ino


def _require_exact_open_owner(
    directory: int,
    owner_descriptor: int,
    *,
    content: bytes,
    owner_identity: tuple[int, int],
) -> None:
    if os.listdir(directory) != [OWNER_NAME]:
        raise InstallerLockError("installer lock ownership changed")
    if _owner_entry_identity(directory) != owner_identity:
        raise InstallerLockError("installer lock ownership changed")
    status = os.fstat(owner_descriptor)
    if (status.st_dev, status.st_ino) != owner_identity:
        raise InstallerLockError("installer lock ownership changed")
    try:
        os.lseek(owner_descriptor, 0, os.SEEK_SET)
        observed = os.read(owner_descriptor, _RECORD_LIMIT + 1)
        if os.read(owner_descriptor, 1):
            raise InstallerLockError("installer lock ownership changed")
    except OSError as error:
        raise InstallerLockError("installer lock ownership changed") from error
    if observed != content or _owner_entry_identity(directory) != owner_identity:
        raise InstallerLockError("installer lock ownership changed")


def _create_owner(
    path: Path,
    *,
    nonce: str,
    process_query: ProcessStartQuery,
) -> HeldInstallerLock:
    if _NONCE_PATTERN.fullmatch(nonce) is None:
        raise InstallerLockError("installer lock capability is invalid")
    owner = LockOwner(
        os.getpid(),
        process_start_identity(os.getpid(), process_query=process_query),
        nonce,
    )
    content = owner.render()
    directory_flags = (
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    )
    directory = os.open(path, directory_flags)
    descriptor = -1
    try:
        directory_status = os.fstat(directory)
        if (
            not stat.S_ISDIR(directory_status.st_mode)
            or stat.S_IMODE(directory_status.st_mode) != 0o700
            or (
                hasattr(os, "getuid")
                and directory_status.st_uid != os.getuid()
            )
        ):
            raise InstallerLockError("installer lock is invalid")
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(OWNER_NAME, flags, 0o600, dir_fd=directory)
        _write_all(descriptor, content)
        os.fsync(descriptor)
        owner_status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(owner_status.st_mode)
            or stat.S_IMODE(owner_status.st_mode) != 0o600
            or (
                hasattr(os, "getuid")
                and owner_status.st_uid != os.getuid()
            )
        ):
            raise InstallerLockError("installer lock owner creation failed")
        os.fsync(directory)
        return HeldInstallerLock(
            path,
            owner,
            (directory_status.st_dev, directory_status.st_ino),
            (owner_status.st_dev, owner_status.st_ino),
        )
    except Exception:
        if descriptor >= 0:
            try:
                status = os.fstat(descriptor)
                identity = (status.st_dev, status.st_ino)
                if _owner_entry_identity(directory) == identity:
                    os.unlink(OWNER_NAME, dir_fd=directory)
            except (OSError, InstallerLockError):
                pass
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory)


def _reclaim_dead_lock(
    path: Path,
    *,
    process_query: ProcessStartQuery,
) -> None:
    (
        directory,
        owner_descriptor,
        content,
        owner,
        directory_identity,
        owner_identity,
    ) = _open_lock(path)
    os.close(owner_descriptor)
    os.close(directory)
    live_start = process_query(owner.pid)
    if live_start is not None:
        if live_start != owner.start:
            raise InstallerLockError("installer lock PID was reused")
        raise InstallerLockError("another installation is in progress")

    claimed = path.with_name(f"{path.name}.reclaim.{secrets.token_hex(16)}")
    try:
        os.rename(path, claimed)
    except FileNotFoundError:
        return
    except OSError as error:
        raise InstallerLockError("installer lock reclaim failed") from error
    restore = True
    claimed_directory = -1
    claimed_owner = -1
    try:
        (
            claimed_directory,
            claimed_owner,
            claimed_content,
            claimed_record,
            claimed_directory_identity,
            claimed_owner_identity,
        ) = _open_lock(claimed)
        if (
            claimed_content != content
            or claimed_record != owner
            or claimed_directory_identity != directory_identity
            or claimed_owner_identity != owner_identity
            or process_query(owner.pid) is not None
        ):
            raise InstallerLockError("installer lock changed during reclaim")
        _require_exact_open_owner(
            claimed_directory,
            claimed_owner,
            content=content,
            owner_identity=owner_identity,
        )
        os.unlink(OWNER_NAME, dir_fd=claimed_directory)
        os.fsync(claimed_directory)
        os.close(claimed_owner)
        claimed_owner = -1
        os.close(claimed_directory)
        claimed_directory = -1
        os.rmdir(claimed)
        restore = False
    finally:
        if claimed_owner >= 0:
            os.close(claimed_owner)
        if claimed_directory >= 0:
            os.close(claimed_directory)
        if restore and claimed.exists() and not path.exists():
            try:
                os.rename(claimed, path)
            except OSError:
                pass


def acquire_installer_lock(
    path: Path,
    *,
    nonce: str,
    process_query: ProcessStartQuery | None = None,
) -> HeldInstallerLock:
    """Acquire one lock, reclaiming one definitively dead owned predecessor."""
    query = _process_start_query if process_query is None else process_query
    created = False
    for attempt in range(2):
        try:
            path.mkdir(mode=0o700)
            created = True
            break
        except FileExistsError:
            if attempt != 0:
                raise InstallerLockError(
                    "another installation is in progress",
                ) from None
            _reclaim_dead_lock(path, process_query=query)
        except OSError as error:
            raise InstallerLockError(
                "another installation is in progress",
            ) from error
    if not created:
        raise InstallerLockError("another installation is in progress")
    try:
        return _create_owner(path, nonce=nonce, process_query=query)
    except Exception:
        try:
            status = path.stat(follow_symlinks=False)
            if stat.S_ISDIR(status.st_mode):
                path.rmdir()
        except OSError:
            pass
        raise


def release_installer_lock(held: HeldInstallerLock) -> None:
    """Retire and release only the unchanged lock and exact receipt we created."""
    (
        directory,
        owner_descriptor,
        content,
        owner,
        directory_identity,
        owner_identity,
    ) = _open_lock(held.path)
    try:
        if (
            owner != held.owner
            or content != held.owner.render()
            or directory_identity != held.directory_identity
            or owner_identity != held.owner_identity
        ):
            raise InstallerLockError("installer lock ownership changed")
        _require_exact_open_owner(
            directory,
            owner_descriptor,
            content=content,
            owner_identity=owner_identity,
        )
    finally:
        os.close(owner_descriptor)
        os.close(directory)

    retired = held.path.with_name(
        f"{held.path.name}.retired.{secrets.token_hex(16)}",
    )
    try:
        if retired.exists():
            raise InstallerLockError("installer lock retirement collision")
        current = held.path.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != held.directory_identity
        ):
            raise InstallerLockError("installer lock ownership changed")
        os.rename(held.path, retired)
    except OSError as error:
        raise InstallerLockError("installer lock cleanup failed") from error

    retired_directory = -1
    retired_owner = -1
    try:
        (
            retired_directory,
            retired_owner,
            retired_content,
            retired_record,
            retired_directory_identity,
            retired_owner_identity,
        ) = _open_lock(retired)
        if (
            retired_record != held.owner
            or retired_content != held.owner.render()
            or retired_directory_identity != held.directory_identity
            or retired_owner_identity != held.owner_identity
        ):
            raise InstallerLockError("installer lock ownership changed")
        _require_exact_open_owner(
            retired_directory,
            retired_owner,
            content=retired_content,
            owner_identity=retired_owner_identity,
        )
    except Exception:
        if retired_owner >= 0:
            os.close(retired_owner)
        if retired_directory >= 0:
            os.close(retired_directory)
        if retired.exists() and not held.path.exists():
            try:
                os.rename(retired, held.path)
            except OSError:
                pass
        raise

    try:
        os.unlink(OWNER_NAME, dir_fd=retired_directory)
        os.fsync(retired_directory)
    except OSError as error:
        raise InstallerLockError("installer lock cleanup failed") from error
    finally:
        os.close(retired_owner)
        os.close(retired_directory)
    try:
        os.rmdir(retired)
    except OSError as error:
        raise InstallerLockError("installer lock cleanup failed") from error
