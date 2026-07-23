#!/usr/bin/env python3
"""Build and archive a native one-folder Hydra runtime."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable
import zipfile


_EXECUTABLES = frozenset(
    (
        "install.sh",
        "bin/hydra-codex",
        "runtime/hydra-codex/hydra-codex",
    ),
)
_SUPPORTED_TARGETS = frozenset(
    ("darwin-arm64", "darwin-x86_64", "linux-x86_64"),
)
_VERSION_PATTERN = re.compile(
    r"\A(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z",
)
_PLUGIN_FILES = (
    Path(".codex-plugin/plugin.json"),
    Path(".mcp.json"),
    Path("README.md"),
    Path("hooks/hooks.json"),
    Path("skills/hydra-report/SKILL.md"),
    Path("skills/hydra-report/agents/openai.yaml"),
    Path("skills/hydra-report/references/report-schema.md"),
)
_LAUNCHER = """#!/bin/sh
set -eu
launcher=$0
if [ -L "$launcher" ]; then
    relation=$(readlink "$launcher")
    case "$relation" in
        /*) launcher=$relation ;;
        *) launcher=$(dirname -- "$launcher")/$relation ;;
    esac
fi
script_dir=$(CDPATH= cd -- "$(dirname -- "$launcher")" && pwd -P)
bundle_root=$(CDPATH= cd -- "$script_dir/.." && pwd -P)
exec "$bundle_root/runtime/hydra-codex/hydra-codex" "$@"
"""

PyInstallerRunner = Callable[..., None]


def _publication_directory(output: Path) -> Path:
    selected = Path(output)
    if selected.exists():
        if not selected.is_dir() or any(selected.iterdir()):
            raise FileExistsError("publication directory must be empty")
    else:
        selected.mkdir(parents=True)
    return selected


def _native_target() -> str:
    key = (platform.system().lower(), platform.machine().lower())
    targets = {
        ("darwin", "arm64"): "darwin-arm64",
        ("darwin", "x86_64"): "darwin-x86_64",
        ("linux", "x86_64"): "linux-x86_64",
        ("linux", "amd64"): "linux-x86_64",
    }
    try:
        return targets[key]
    except KeyError as error:
        raise RuntimeError("standalone builds are unsupported on this platform") from error


def _git(
    source_root: Path,
    *arguments: str,
    text: bool = True,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=source_root,
            check=True,
            capture_output=True,
            text=text,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("standalone source verification failed") from error


def _require_clean_tracked_source(source_root: Path) -> tuple[str, ...]:
    source = source_root.resolve(strict=True)
    top = _git(source, "rev-parse", "--show-toplevel").stdout.strip()
    if Path(top).resolve() != source:
        raise RuntimeError("source root must be a Git repository root")
    _git(source, "rev-parse", "--verify", "HEAD")
    dirty = _git(
        source,
        "status",
        "--porcelain=v1",
        "--untracked-files=no",
    ).stdout
    if dirty:
        raise RuntimeError("tracked source must be clean")
    listing = _git(source, "ls-files", "-z", text=False).stdout
    assert isinstance(listing, bytes)
    paths = tuple(
        os.fsdecode(item)
        for item in listing.split(b"\0")
        if item
    )
    if not paths:
        raise RuntimeError("tracked source inventory is empty")
    return paths


def _copy_tracked_source(
    source_root: Path,
    destination: Path,
    tracked: tuple[str, ...],
) -> None:
    for relative_text in tracked:
        relative = Path(relative_text)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("tracked source inventory is unsafe")
        source = source_root / relative
        target = destination / relative
        try:
            mode = source.lstat().st_mode
        except OSError as error:
            raise RuntimeError("tracked source is unavailable") from error
        if not stat.S_ISREG(mode):
            raise RuntimeError("tracked source must contain only regular files")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)


def _version_from_source(source_root: Path) -> str:
    init = (source_root / "src" / "hydra_codex" / "__init__.py").read_text(
        encoding="utf-8",
    )
    match = re.search(
        r'^__version__\s*=\s*"([^"]+)"\s*$',
        init,
        re.MULTILINE,
    )
    if match is None or _VERSION_PATTERN.fullmatch(match.group(1)) is None:
        raise RuntimeError("canonical source version is invalid")
    return match.group(1)


def _write_distribution_metadata(source_root: Path, version: str) -> None:
    metadata = source_root / "src" / f"hydra_codex-{version}.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.4\n"
        "Name: hydra-codex\n"
        f"Version: {version}\n"
        "License-Expression: MIT\n"
        "Requires-Python: >=3.12\n",
        encoding="utf-8",
    )
    shutil.copy2(source_root / "LICENSE", metadata / "LICENSE")
    (source_root / "packaging" / "_frozen_main.py").write_text(
        "from hydra_codex.__main__ import main\n"
        "raise SystemExit(main())\n",
        encoding="utf-8",
    )


def _run_pyinstaller(
    *,
    source_root: Path,
    workpath: Path,
    distpath: Path,
) -> None:
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment["PYINSTALLER_CONFIG_DIR"] = str(workpath.parent / "cache")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--workpath",
            str(workpath),
            "--distpath",
            str(distpath),
            str(source_root / "packaging" / "hydra-codex.spec"),
        ],
        cwd=source_root,
        check=True,
        timeout=600,
        env=environment,
    )


def _copy_release_inputs(source_root: Path, bundle: Path, version: str, target: str) -> None:
    bundle.mkdir(parents=True)
    (bundle / "VERSION").write_text(version + "\n", encoding="utf-8")
    (bundle / "TARGET").write_text(target + "\n", encoding="utf-8")
    shutil.copy2(source_root / "LICENSE", bundle / "LICENSE")
    shutil.copy2(source_root / "install.sh", bundle / "install.sh")
    (bundle / "install.sh").chmod(0o755)

    launcher = bundle / "bin" / "hydra-codex"
    launcher.parent.mkdir()
    launcher.write_text(_LAUNCHER, encoding="utf-8")
    launcher.chmod(0o755)

    marketplace = bundle / "marketplace"
    manifest = marketplace / ".agents" / "plugins" / "marketplace.json"
    manifest.parent.mkdir(parents=True)
    shutil.copy2(source_root / ".agents" / "plugins" / "marketplace.json", manifest)
    plugin_source = source_root / "plugins" / "hydra-codex"
    plugin_target = marketplace / "plugins" / "hydra-codex"
    for relative in _PLUGIN_FILES:
        destination = plugin_target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(plugin_source / relative, destination)

    try:
        plugin_manifest = json.loads(
            (plugin_target / ".codex-plugin" / "plugin.json").read_text(
                encoding="utf-8",
            ),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("plugin manifest is invalid") from error
    if plugin_manifest.get("version") != version:
        raise RuntimeError("plugin version does not match canonical version")


def _normalize_zip_archive(path: Path) -> None:
    """Rewrite PyInstaller's base library with canonical ordering and metadata."""
    selected = Path(path)
    try:
        with zipfile.ZipFile(selected, "r") as source:
            infos = source.infolist()
            if (
                any(info.is_dir() for info in infos)
                or len({info.filename for info in infos}) != len(infos)
            ):
                raise RuntimeError("PyInstaller base library inventory is invalid")
            entries = {
                info.filename: source.read(info)
                for info in infos
            }
    except (OSError, zipfile.BadZipFile) as error:
        raise RuntimeError("PyInstaller base library is invalid") from error

    buffer = io.BytesIO()
    with zipfile.ZipFile(
        buffer,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as destination:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            destination.writestr(info, entries[name], compress_type=zipfile.ZIP_DEFLATED)
    selected.write_bytes(buffer.getvalue())


def build_bundle(
    source_root: Path,
    output: Path,
    target: str,
    *,
    _pyinstaller: PyInstallerRunner | None = None,
) -> Path:
    """Build one native standalone release into an empty publication directory."""
    publication = _publication_directory(output).resolve()
    if target not in _SUPPORTED_TARGETS:
        raise ValueError("unsupported standalone target")
    if target != _native_target():
        raise ValueError("PyInstaller cannot cross-compile standalone targets")
    source = Path(source_root).resolve(strict=True)
    tracked = _require_clean_tracked_source(source)
    runner = _run_pyinstaller if _pyinstaller is None else _pyinstaller

    with tempfile.TemporaryDirectory(
        prefix=".hydra-standalone-",
        dir=publication.parent,
    ) as temporary:
        private = Path(temporary)
        staged_source = private / "source"
        workpath = private / "work"
        distpath = private / "dist"
        staged_source.mkdir()
        _copy_tracked_source(source, staged_source, tracked)
        version = _version_from_source(staged_source)
        _write_distribution_metadata(staged_source, version)
        workpath.mkdir()
        distpath.mkdir()
        runner(
            source_root=staged_source,
            workpath=workpath,
            distpath=distpath,
        )
        frozen = distpath / "hydra-codex"
        runtime_executable = frozen / "hydra-codex"
        if (
            not frozen.is_dir()
            or not runtime_executable.is_file()
            or runtime_executable.is_symlink()
            or runtime_executable.stat().st_mode & 0o111 == 0
        ):
            raise RuntimeError("PyInstaller did not produce a one-folder runtime")
        _normalize_zip_archive(frozen / "_internal" / "base_library.zip")

        staged_bundle = private / "bundle" / f"hydra-codex-{version}"
        _copy_release_inputs(staged_source, staged_bundle, version, target)
        runtime = staged_bundle / "runtime" / "hydra-codex"
        shutil.copytree(frozen, runtime, symlinks=False)
        runtime_executable = runtime / "hydra-codex"
        runtime_executable.chmod(runtime_executable.stat().st_mode | 0o755)

        destination = publication / staged_bundle.name
        if destination.exists():
            raise FileExistsError("bundle destination already exists")
        shutil.copytree(staged_bundle, destination, symlinks=False)
    return destination


def _archive_info(name: str, *, directory: bool, executable: bool) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name + ("/" if directory else ""))
    info.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
    info.mode = 0o755 if directory or executable else 0o644
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def create_archive(bundle_root: Path, output: Path) -> Path:
    """Create a byte-reproducible gzip tar archive for one staged bundle."""
    root = Path(bundle_root)
    if not root.is_dir():
        raise FileNotFoundError("bundle root is unavailable")
    try:
        target = (root / "TARGET").read_text(encoding="utf-8").rstrip("\n")
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError("bundle target is unavailable") from error
    if target not in _SUPPORTED_TARGETS:
        raise ValueError("bundle target is unsupported")
    publication = Path(output)
    publication.mkdir(parents=True, exist_ok=True)
    archive = publication / f"{root.name}-{target}.tar.gz"
    if archive.exists():
        raise FileExistsError("archive already exists")

    members = [root, *sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())]
    with archive.open("xb") as destination:
        with gzip.GzipFile(filename="", mode="wb", fileobj=destination, mtime=0) as compressed:
            with tarfile.open(
                mode="w",
                fileobj=compressed,
                format=tarfile.GNU_FORMAT,
            ) as bundle:
                for path in members:
                    relative = path.relative_to(root).as_posix()
                    member_name = root.name if relative == "." else f"{root.name}/{relative}"
                    details = path.lstat()
                    if stat.S_ISLNK(details.st_mode):
                        raise ValueError("bundle must not contain symbolic links")
                    if stat.S_ISDIR(details.st_mode):
                        bundle.addfile(_archive_info(
                            member_name,
                            directory=True,
                            executable=True,
                        ))
                        continue
                    if not stat.S_ISREG(details.st_mode):
                        raise ValueError("bundle contains an unsupported file type")
                    payload = path.read_bytes()
                    info = _archive_info(
                        member_name,
                        directory=False,
                        executable=relative in _EXECUTABLES,
                    )
                    info.size = len(payload)
                    bundle.addfile(info, io.BytesIO(payload))
    return archive


def sha256_file(path: Path) -> str:
    """Return the lowercase SHA-256 digest for one regular file."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=Path(__file__).parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    target = arguments.target or _native_target()
    bundle = build_bundle(arguments.source_root, arguments.output, target)
    try:
        archive = create_archive(bundle, arguments.output)
    finally:
        shutil.rmtree(bundle)
    print(f"{archive}  {sha256_file(archive)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
