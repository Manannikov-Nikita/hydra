"""Safe and idempotent Hydra project initialization lifecycle."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import tempfile

from .project import normalize_project_display_name
from .project_config import (
    PROJECT_CONFIG_SCHEMA_VERSION,
    ProjectConfig,
    generate_project_id,
    read_project_config,
    render_project_config,
)


_CONFIRMATION = "remove hydra project"


class UnsafeProjectTarget(ValueError):
    """Raised when a lifecycle target is too broad or redirects through a link."""


class ProjectConfirmationError(ValueError):
    """Raised when destructive confirmation is not exact."""


@dataclass(frozen=True)
class ProjectMutationResult:
    project_root: Path
    project_id: str
    changed: bool


def _directory(path: Path | str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.exists() or not candidate.is_dir():
        raise UnsafeProjectTarget("project target must be an existing directory")
    return candidate.resolve()


def _nearest_root(start: Path, marker: Callable[[Path], bool]) -> Path | None:
    for directory in (start, *start.parents):
        if marker(directory):
            return directory
    return None


def canonical_project_target(
    path: Path | str = ".",
    *,
    home: Path | None = None,
) -> Path:
    """Resolve the nearest Hydra/Git root or retain the exact directory."""
    exact = _directory(path)
    hydra_root = _nearest_root(
        exact,
        lambda directory: (directory / ".hydra" / "project.toml").exists(),
    )
    git_root = _nearest_root(
        exact,
        lambda directory: (directory / ".git").exists(),
    )
    target = hydra_root or git_root or exact
    protected_home = (Path.home() if home is None else Path(home)).expanduser().resolve()
    if target == Path(target.anchor) or target == protected_home:
        raise UnsafeProjectTarget("unsafe project target")
    hydra = target / ".hydra"
    if hydra.is_symlink():
        raise UnsafeProjectTarget("symlinked .hydra is not supported")
    return target


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_exclusively(path: Path, content: bytes) -> bool:
    """Publish bytes only if *path* is absent, without replacing another writer."""
    directory = path.parent
    directory.mkdir(mode=0o700, parents=False, exist_ok=True)
    if directory.is_symlink():
        raise UnsafeProjectTarget("symlinked .hydra is not supported")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".project.toml-",
        dir=directory,
    )
    temporary = Path(temporary_name)
    descriptor_open = True
    published = False
    try:
        os.fchmod(descriptor, 0o600)
        handle = os.fdopen(descriptor, "wb")
        descriptor_open = False
        try:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        try:
            os.link(temporary, path)
            published = True
        except FileExistsError:
            published = False
        return published
    finally:
        if descriptor_open:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        finally:
            _fsync_directory(directory)


def _normalized_name(name: str | None, root: Path) -> str | None:
    selected: object = root.name if name is None else name
    return normalize_project_display_name(selected)


def initialize_project(
    path: Path | str = ".",
    *,
    name: str | None = None,
    project_id_factory: Callable[[], str] = generate_project_id,
    home: Path | None = None,
) -> ProjectMutationResult:
    """Initialize the selected project exactly once."""
    root = canonical_project_target(path, home=home)
    config_path = root / ".hydra" / "project.toml"
    if config_path.exists():
        current = read_project_config(config_path)
        return ProjectMutationResult(root, current.project_id, False)
    config = ProjectConfig(
        PROJECT_CONFIG_SCHEMA_VERSION,
        project_id_factory(),
        _normalized_name(name, root),
        "hybrid",
    )
    published = publish_exclusively(config_path, render_project_config(config))
    current = read_project_config(config_path)
    return ProjectMutationResult(
        root,
        current.project_id,
        published and current.project_id == config.project_id,
    )


def uninitialize_project(
    path: Path | str = ".",
    *,
    confirmation: str,
    home: Path | None = None,
) -> ProjectMutationResult:
    """Remove only a valid project config, retaining all unrelated files."""
    if confirmation != _CONFIRMATION:
        raise ProjectConfirmationError("exact confirmation required")
    root = canonical_project_target(path, home=home)
    hydra = root / ".hydra"
    config_path = hydra / "project.toml"
    if not config_path.exists():
        return ProjectMutationResult(root, "", False)
    if config_path.is_symlink():
        raise UnsafeProjectTarget("symlinked project configuration is not supported")
    current = read_project_config(config_path)
    try:
        config_path.unlink()
    except FileNotFoundError:
        return ProjectMutationResult(root, current.project_id, False)
    _fsync_directory(hydra)
    try:
        hydra.rmdir()
    except OSError as error:
        if error.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
            raise
    else:
        _fsync_directory(root)
    return ProjectMutationResult(root, current.project_id, True)
