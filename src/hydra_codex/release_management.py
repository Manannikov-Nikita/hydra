"""Owned local release activation, rollback, and removal."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
from typing import TextIO

from .install_layout import BundleLayout, InvalidBundle, validate_bundle
from .platform_paths import default_data_directory

_COORDINATION_LOCK_NAME = "release-lifecycle.lock"
_JOURNAL_NAME = "release-journal.json"
_JOURNAL_LIMIT = 4096


class InstallOwnershipError(RuntimeError):
    """Raised when local CLI ownership cannot be proven exactly."""


class LifecycleBusyError(RuntimeError):
    """Raised when another local release mutation holds the lifecycle lock."""


@dataclass(frozen=True)
class InstallRoots:
    home: Path
    versions: Path
    current: Path
    launcher: Path


@dataclass(frozen=True)
class UpgradeStatus:
    current_version: str
    latest_version: str
    update_available: bool


@dataclass(frozen=True)
class _LinkState:
    current_version: str | None
    current_target: Path | None
    launcher_present: bool


@dataclass(frozen=True)
class _Journal:
    phase: str
    previous_version: str | None
    new_version: str


def default_install_roots(home: Path | None = None) -> InstallRoots:
    """Return the fixed per-user version tree without creating it."""
    selected = Path.home() if home is None else Path(home).expanduser()
    hydra = selected / ".hydra"
    return InstallRoots(
        home=hydra,
        versions=hydra / "versions",
        current=hydra / "current",
        launcher=selected / ".local" / "bin" / "hydra-codex",
    )


def _safe_version(version: str) -> str:
    if (
        not isinstance(version, str)
        or not version
        or version in {".", ".."}
        or "/" in version
        or "\\" in version
        or "\0" in version
        or any(ord(character) < 32 for character in version)
        or Path(version).name != version
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", version) is None
    ):
        raise ValueError("invalid release version")
    return version


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_directory(path: Path, *, mode: int) -> None:
    try:
        current_mode = path.lstat().st_mode
    except FileNotFoundError:
        path.mkdir(parents=True, mode=mode)
        try:
            path.chmod(mode)
        except OSError:
            pass
        return
    if (
        not stat.S_ISDIR(current_mode)
        or stat.S_ISLNK(current_mode)
        or path.stat().st_uid != os.getuid()
        or stat.S_IMODE(current_mode) & 0o077
    ):
        raise InstallOwnershipError("invalid release directory")


def _lexists(path: Path) -> bool:
    try:
        path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    return True


def _validate_owned_directory(path: Path, *, private: bool) -> bool:
    if not _lexists(path):
        return False
    try:
        status = path.lstat()
    except OSError as error:
        raise InstallOwnershipError("invalid release directory") from error
    permissions = stat.S_IMODE(status.st_mode)
    unsafe = permissions & (0o077 if private else 0o022)
    if (
        not stat.S_ISDIR(status.st_mode)
        or stat.S_ISLNK(status.st_mode)
        or status.st_uid != os.getuid()
        or unsafe
    ):
        raise InstallOwnershipError("invalid release directory")
    return True


def _validate_directory_ancestors(
    base: Path,
    target: Path,
    *,
    private_final: bool,
) -> None:
    anchor = base.absolute()
    selected = target.absolute()
    try:
        relative = selected.relative_to(anchor)
    except ValueError as error:
        raise InstallOwnershipError("release path escapes selected home") from error
    current = anchor
    parts = relative.parts
    for index, component in enumerate(parts):
        current = current / component
        _validate_owned_directory(
            current,
            private=bool(private_final and index == len(parts) - 1),
        )


def _validate_root_ownership(roots: InstallRoots) -> None:
    user_home = roots.home.parent
    _validate_owned_directory(user_home, private=False)
    _validate_directory_ancestors(
        user_home,
        roots.home,
        private_final=True,
    )
    _validate_directory_ancestors(
        user_home,
        roots.launcher.parent,
        private_final=False,
    )


def _validate_versions_root(roots: InstallRoots) -> bool:
    if not _lexists(roots.versions):
        return False
    return _validate_owned_directory(roots.versions, private=True)


def _owned_current(roots: InstallRoots) -> tuple[str, Path] | None:
    if not _lexists(roots.current):
        return None
    try:
        mode = roots.current.lstat().st_mode
        relation = os.readlink(roots.current)
    except OSError as error:
        raise InstallOwnershipError("invalid active release link") from error
    if not stat.S_ISLNK(mode):
        raise InstallOwnershipError("invalid active release link")
    target = Path(relation)
    versions = roots.versions.absolute()
    if not target.is_absolute() or target.parent != versions:
        raise InstallOwnershipError("foreign active release link")
    try:
        version = _safe_version(target.name)
    except ValueError as error:
        raise InstallOwnershipError("invalid active release link") from error
    try:
        target_mode = target.lstat().st_mode
    except OSError as error:
        raise InstallOwnershipError("dangling active release link") from error
    if not stat.S_ISDIR(target_mode) or stat.S_ISLNK(target_mode):
        raise InstallOwnershipError("invalid active release target")
    try:
        validate_bundle(target, expected_version=version)
    except InvalidBundle as error:
        raise InstallOwnershipError("invalid active release target") from error
    return version, target


def _validate_link_state(roots: InstallRoots) -> _LinkState:
    _validate_root_ownership(roots)
    _validate_versions_root(roots)
    current = _owned_current(roots)
    launcher_present = _lexists(roots.launcher)
    if launcher_present:
        try:
            mode = roots.launcher.lstat().st_mode
            relation = os.readlink(roots.launcher)
        except OSError as error:
            raise InstallOwnershipError("invalid Hydra launcher") from error
        expected = str(roots.current / "bin" / "hydra-codex")
        if not stat.S_ISLNK(mode) or relation != expected or current is None:
            raise InstallOwnershipError("foreign Hydra launcher")
        try:
            executable_mode = roots.launcher.stat().st_mode
        except OSError as error:
            raise InstallOwnershipError("dangling Hydra launcher") from error
        if not stat.S_ISREG(executable_mode) or executable_mode & 0o111 == 0:
            raise InstallOwnershipError("invalid Hydra launcher")
    return _LinkState(
        None if current is None else current[0],
        None if current is None else current[1],
        launcher_present,
    )


def _coordination_root(
    roots: InstallRoots,
    *,
    environ: Mapping[str, str],
) -> Path:
    user_home = roots.home.parent
    root = default_data_directory(user_home, environ=environ)
    xdg_data_home = environ.get("XDG_DATA_HOME")
    linux_fallback = user_home / ".local" / "share" / "hydra"
    if (
        isinstance(xdg_data_home, str)
        and xdg_data_home
        and not Path(xdg_data_home).is_absolute()
        and root == linux_fallback
    ):
        raise InstallOwnershipError("invalid release data directory")
    return root


def _validate_coordination_root(user_home: Path, root: Path) -> None:
    try:
        root.relative_to(user_home)
    except ValueError:
        base = root.parent
        _validate_owned_directory(base, private=False)
    else:
        base = user_home
    _validate_directory_ancestors(
        base,
        root,
        private_final=True,
    )


@contextmanager
def _lifecycle_lock(
    roots: InstallRoots,
    *,
    environ: Mapping[str, str],
) -> Iterator[None]:
    _validate_root_ownership(roots)
    user_home = roots.home.parent
    coordination_root = _coordination_root(roots, environ=environ)
    _validate_coordination_root(user_home, coordination_root)
    _ensure_directory(coordination_root, mode=0o700)
    _validate_coordination_root(user_home, coordination_root)
    lock_path = coordination_root / _COORDINATION_LOCK_NAME
    if _lexists(lock_path):
        status = lock_path.lstat()
        if (
            not stat.S_ISREG(status.st_mode)
            or stat.S_ISLNK(status.st_mode)
            or stat.S_IMODE(status.st_mode) != 0o600
            or status.st_uid != os.getuid()
        ):
            raise InstallOwnershipError("invalid release lifecycle lock")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_uid != os.getuid()
        ):
            raise InstallOwnershipError("invalid release lifecycle lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                raise LifecycleBusyError("release lifecycle is busy") from None
            raise
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _atomic_symlink(link: Path, target: Path) -> None:
    _ensure_directory(link.parent, mode=0o700)
    temporary = link.parent / f".{link.name}.tmp-{secrets.token_hex(8)}"
    os.symlink(str(target), temporary)
    try:
        os.replace(temporary, link)
        _fsync_directory(link.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _unlink_owned(path: Path) -> None:
    path.unlink()
    _fsync_directory(path.parent)


def _journal_path(roots: InstallRoots) -> Path:
    return roots.home / _JOURNAL_NAME


def _write_private_json(path: Path, value: Mapping[str, object]) -> None:
    _ensure_directory(path.parent, mode=0o700)
    temporary = path.parent / f".{path.name}.tmp-{secrets.token_hex(8)}"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_journal(roots: InstallRoots, journal: _Journal) -> None:
    _write_private_json(
        _journal_path(roots),
        {
            "new_version": journal.new_version,
            "phase": journal.phase,
            "previous_version": journal.previous_version,
            "schema_version": 1,
        },
    )


def _read_journal(roots: InstallRoots) -> _Journal | None:
    path = _journal_path(roots)
    if not _lexists(path):
        return None
    status = path.lstat()
    if (
        not stat.S_ISREG(status.st_mode)
        or stat.S_ISLNK(status.st_mode)
        or status.st_uid != os.getuid()
    ):
        raise InstallOwnershipError("invalid release journal")
    if (
        stat.S_IMODE(status.st_mode) != 0o600
        or status.st_size > _JOURNAL_LIMIT
    ):
        raise InstallOwnershipError("invalid release journal")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InstallOwnershipError("invalid release journal") from error
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("phase")
        not in {
            "prepared",
            "bundle_moved",
            "current_switched",
            "links_switched",
            "refreshing",
            "refresh_committed",
        }
        or not isinstance(value.get("new_version"), str)
        or (
            value.get("previous_version") is not None
            and not isinstance(value.get("previous_version"), str)
        )
    ):
        raise InstallOwnershipError("invalid release journal")
    new_version = _safe_version(value["new_version"])
    previous = value.get("previous_version")
    if previous is not None:
        previous = _safe_version(previous)
    return _Journal(value["phase"], previous, new_version)


def _clear_journal(roots: InstallRoots) -> None:
    path = _journal_path(roots)
    if _lexists(path):
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
            raise InstallOwnershipError("invalid release journal")
        _unlink_owned(path)


def _restore_previous_links(
    roots: InstallRoots,
    journal: _Journal,
    *,
    clear: bool,
) -> None:
    state = _validate_link_state(roots)
    if journal.previous_version is None:
        if state.launcher_present:
            _unlink_owned(roots.launcher)
        if state.current_target is not None:
            _unlink_owned(roots.current)
    else:
        previous = roots.versions / journal.previous_version
        validate_bundle(previous, expected_version=journal.previous_version)
        _atomic_symlink(roots.current, previous)
        _atomic_symlink(
            roots.launcher,
            roots.current / "bin" / "hydra-codex",
        )
    if clear:
        _clear_journal(roots)


def _recover_pending(
    roots: InstallRoots,
    *,
    reconcile_integration: Callable[[BundleLayout], None] | None = None,
) -> None:
    journal = _read_journal(roots)
    if journal is None:
        return
    _validate_link_state(roots)
    if journal.phase == "refreshing":
        if reconcile_integration is None:
            raise InstallOwnershipError(
                "Codex integration recovery is required",
            )
        candidate = validate_bundle(
            roots.versions / journal.new_version,
            expected_version=journal.new_version,
        )
        try:
            reconcile_integration(candidate)
        except Exception:
            _restore_previous_links(roots, journal, clear=False)
            raise
        _atomic_symlink(roots.current, candidate.root)
        _atomic_symlink(
            roots.launcher,
            roots.current / "bin" / "hydra-codex",
        )
        _write_journal(
            roots,
            _Journal(
                "refresh_committed",
                journal.previous_version,
                journal.new_version,
            ),
        )
        _clear_journal(roots)
        return
    if journal.phase == "refresh_committed":
        committed = roots.versions / journal.new_version
        validate_bundle(committed, expected_version=journal.new_version)
        _atomic_symlink(roots.current, committed)
        _atomic_symlink(
            roots.launcher,
            roots.current / "bin" / "hydra-codex",
        )
        _clear_journal(roots)
        return
    _restore_previous_links(roots, journal, clear=True)


def _validated_candidate(layout: BundleLayout) -> BundleLayout:
    _safe_version(layout.version)
    validated = validate_bundle(
        layout.root,
        expected_version=layout.version,
        expected_target=layout.target,
    )
    if validated != layout:
        raise InvalidBundle("bundle layout does not match")
    return validated


def _activate_locked(
    layout: BundleLayout,
    *,
    roots: InstallRoots,
    retain_journal: bool,
) -> Path:
    candidate = _validated_candidate(layout)
    state = _validate_link_state(roots)
    _ensure_directory(roots.home, mode=0o700)
    _ensure_directory(roots.versions, mode=0o700)
    version_root = roots.versions / candidate.version
    if _lexists(version_root):
        mode = version_root.lstat().st_mode
        if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
            raise InvalidBundle("installed release is invalid")
        validate_bundle(
            version_root,
            expected_version=candidate.version,
            expected_target=candidate.target,
        )
    journal = _Journal("prepared", state.current_version, candidate.version)
    _write_journal(roots, journal)
    try:
        if not _lexists(version_root):
            os.replace(candidate.root, version_root)
            _fsync_directory(roots.versions)
        _write_journal(
            roots,
            _Journal("bundle_moved", state.current_version, candidate.version),
        )
        _atomic_symlink(roots.current, version_root)
        _write_journal(
            roots,
            _Journal("current_switched", state.current_version, candidate.version),
        )
        _atomic_symlink(
            roots.launcher,
            roots.current / "bin" / "hydra-codex",
        )
        _write_journal(
            roots,
            _Journal("links_switched", state.current_version, candidate.version),
        )
        if not retain_journal:
            _clear_journal(roots)
        return version_root
    except Exception:
        _recover_pending(roots)
        raise


def activate_version(
    layout: BundleLayout,
    *,
    roots: InstallRoots,
    environ: Mapping[str, str],
) -> Path:
    """Activate a verified bundle without overwriting any installed version."""
    _validate_link_state(roots)
    with _lifecycle_lock(roots, environ=environ):
        _recover_pending(roots)
        _validate_link_state(roots)
        return _activate_locked(layout, roots=roots, retain_journal=False)


def inspect_active_installation(roots: InstallRoots) -> tuple[str, str] | None:
    """Validate active CLI relations and return only path-free release identity."""
    state = _validate_link_state(roots)
    if state.current_target is None or state.current_version is None:
        return None
    if not state.launcher_present:
        raise InstallOwnershipError("Hydra launcher is missing")
    layout = validate_bundle(
        state.current_target,
        expected_version=state.current_version,
    )
    return layout.version, layout.target


def _home(environ: Mapping[str, str]) -> Path:
    value = environ.get("HOME")
    return (
        Path(value).expanduser()
        if isinstance(value, str) and value
        else Path.home()
    )


def _status_for(
    roots: InstallRoots,
    candidate: BundleLayout | None,
) -> UpgradeStatus:
    state = _validate_link_state(roots)
    current = state.current_version or ""
    latest = current if candidate is None else candidate.version
    return UpgradeStatus(current, latest, bool(latest and latest != current))


def upgrade(
    *,
    check: bool,
    environ: Mapping[str, str],
    stdout: TextIO,
    verified_candidate: BundleLayout | None = None,
    refresh_integration: Callable[[BundleLayout], None] | None = None,
    roots: InstallRoots | None = None,
) -> UpgradeStatus:
    """Check or activate one already-verified candidate supplied by the caller."""
    _ = stdout
    selected = default_install_roots(_home(environ)) if roots is None else roots
    candidate = (
        None
        if verified_candidate is None
        else _validated_candidate(verified_candidate)
    )
    status = _status_for(selected, candidate)
    if check:
        return status
    if candidate is None:
        raise ValueError("verified release candidate is unavailable")

    _validate_link_state(selected)
    with _lifecycle_lock(selected, environ=environ):
        _recover_pending(
            selected,
            reconcile_integration=refresh_integration,
        )
        _validate_link_state(selected)
        installed = _activate_locked(
            candidate,
            roots=selected,
            retain_journal=True,
        )
        active = validate_bundle(
            installed,
            expected_version=candidate.version,
            expected_target=candidate.target,
        )
        journal = _read_journal(selected)
        if journal is None:
            raise InstallOwnershipError("release journal is missing")
        if refresh_integration is None:
            _clear_journal(selected)
            return UpgradeStatus(
                candidate.version,
                candidate.version,
                False,
            )
        _write_journal(
            selected,
            _Journal(
                "refreshing",
                journal.previous_version,
                journal.new_version,
            ),
        )
        try:
            refresh_integration(active)
        except Exception:
            _restore_previous_links(selected, journal, clear=True)
            raise
        _write_journal(
            selected,
            _Journal(
                "refresh_committed",
                journal.previous_version,
                journal.new_version,
            ),
        )
        _clear_journal(selected)
        return UpgradeStatus(
            candidate.version,
            candidate.version,
            False,
        )


def _remove_owned_versions(roots: InstallRoots) -> None:
    if not _lexists(roots.versions):
        return
    mode = roots.versions.lstat().st_mode
    if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
        raise InstallOwnershipError("invalid versions directory")
    for entry in roots.versions.iterdir():
        try:
            version = _safe_version(entry.name)
            entry_mode = entry.lstat().st_mode
            if not stat.S_ISDIR(entry_mode) or stat.S_ISLNK(entry_mode):
                continue
            validate_bundle(entry, expected_version=version)
        except (InvalidBundle, InstallOwnershipError, OSError, ValueError):
            continue
        shutil.rmtree(entry)
        _fsync_directory(roots.versions)


def _uninstall_preflight(roots: InstallRoots) -> None:
    _validate_link_state(roots)
    _read_journal(roots)


def _discard_pending_for_uninstall(roots: InstallRoots) -> None:
    if _read_journal(roots) is not None:
        _clear_journal(roots)


def uninstall(
    *,
    keep_cli: bool,
    environ: Mapping[str, str],
    detach_integration: Callable[[], None],
    roots: InstallRoots | None = None,
) -> None:
    """Detach Codex first, then remove only individually proven-owned CLI state."""
    selected = default_install_roots(_home(environ)) if roots is None else roots
    _uninstall_preflight(selected)
    with _lifecycle_lock(selected, environ=environ):
        _uninstall_preflight(selected)
        detach_integration()
        if keep_cli:
            return
        _discard_pending_for_uninstall(selected)
        state = _validate_link_state(selected)
        if state.launcher_present:
            _unlink_owned(selected.launcher)
        if state.current_target is not None:
            _unlink_owned(selected.current)
        _remove_owned_versions(selected)
