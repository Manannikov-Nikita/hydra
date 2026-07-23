"""Dependency-free validation for public Hydra release archives."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import tarfile


MAX_MEMBERS = 4096
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_TOTAL_MEMBER_BYTES = 512 * 1024 * 1024
MAX_MARKER_BYTES = 256
SUPPORTED_TARGETS = frozenset(
    ("darwin-arm64", "darwin-x86_64", "linux-x86_64"),
)
_VERSION_PATTERN = re.compile(
    r"\A(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z",
)
_REQUIRED_FILES = frozenset(
    (
        "VERSION",
        "TARGET",
        "LICENSE",
        "install.sh",
        "bin/hydra-codex",
        "marketplace/.agents/plugins/marketplace.json",
        "marketplace/plugins/hydra-codex/.codex-plugin/plugin.json",
    ),
)


class UnsafeArchive(ValueError):
    """Raised when a release archive cannot be extracted safely."""


@dataclass(frozen=True)
class ValidatedArchive:
    archive: Path
    top_level: str
    version: str
    target: str


def _safe_relative_name(name: str, *, expected_top_level: str) -> str:
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or "//" in name
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        raise UnsafeArchive("archive member name is unsafe")
    canonical = name[:-1] if name.endswith("/") else name
    components = canonical.split("/")
    if (
        not canonical
        or any(component in {"", ".", ".."} for component in components)
        or (
            canonical != expected_top_level
            and not canonical.startswith(expected_top_level + "/")
        )
    ):
        raise UnsafeArchive("archive member name is unsafe")
    return canonical


def _read_marker(
    bundle: tarfile.TarFile,
    member: tarfile.TarInfo,
) -> str:
    if member.size > MAX_MARKER_BYTES:
        raise UnsafeArchive("archive marker is invalid")
    stream = bundle.extractfile(member)
    if stream is None:
        raise UnsafeArchive("archive marker is invalid")
    try:
        content = stream.read(MAX_MARKER_BYTES + 1)
    except (OSError, EOFError) as error:
        raise UnsafeArchive("archive marker is invalid") from error
    if len(content) > MAX_MARKER_BYTES or b"\0" in content:
        raise UnsafeArchive("archive marker is invalid")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise UnsafeArchive("archive marker is invalid") from error
    if text.endswith("\n"):
        text = text[:-1]
    if not text or "\n" in text or "\r" in text:
        raise UnsafeArchive("archive marker is invalid")
    return text


def validate_tar_members(
    archive: Path,
    *,
    expected_top_level: str,
) -> ValidatedArchive:
    """Validate a gzip tarball without extracting any archive member."""
    if (
        not expected_top_level.startswith("hydra-codex-")
        or "/" in expected_top_level
        or "\\" in expected_top_level
    ):
        raise UnsafeArchive("archive identity is invalid")
    expected_version = expected_top_level.removeprefix("hydra-codex-")
    if _VERSION_PATTERN.fullmatch(expected_version) is None:
        raise UnsafeArchive("archive identity is invalid")

    selected = Path(archive)
    members: dict[str, tarfile.TarInfo] = {}
    total_size = 0
    try:
        with tarfile.open(selected, mode="r:gz") as bundle:
            for count, member in enumerate(bundle, start=1):
                if count > MAX_MEMBERS:
                    raise UnsafeArchive("archive has too many members")
                name = _safe_relative_name(
                    member.name,
                    expected_top_level=expected_top_level,
                )
                if name in members:
                    raise UnsafeArchive("archive contains a duplicate member")
                if member.type not in {
                    tarfile.REGTYPE,
                    tarfile.AREGTYPE,
                    tarfile.DIRTYPE,
                }:
                    raise UnsafeArchive("archive member type is unsafe")
                if member.mode & 0o7000:
                    raise UnsafeArchive("archive member mode is unsafe")
                if (
                    member.type in {tarfile.REGTYPE, tarfile.AREGTYPE}
                    and member.mode & 0o400 == 0
                ):
                    raise UnsafeArchive("archive member mode is unsafe")
                if (
                    member.type == tarfile.DIRTYPE
                    and member.mode & 0o500 != 0o500
                ):
                    raise UnsafeArchive("archive member mode is unsafe")
                if member.size < 0 or member.size > MAX_MEMBER_BYTES:
                    raise UnsafeArchive("archive member is too large")
                total_size += member.size
                if total_size > MAX_TOTAL_MEMBER_BYTES:
                    raise UnsafeArchive("archive is too large")
                members[name] = member

            relative_files = {
                name.removeprefix(expected_top_level + "/")
                for name, member in members.items()
                if member.type in {tarfile.REGTYPE, tarfile.AREGTYPE}
                and name.startswith(expected_top_level + "/")
            }
            if not _REQUIRED_FILES.issubset(relative_files):
                raise UnsafeArchive("archive inventory is incomplete")
            launcher = members[f"{expected_top_level}/bin/hydra-codex"]
            if launcher.mode & 0o111 == 0:
                raise UnsafeArchive("archive launcher is not executable")
            version = _read_marker(
                bundle,
                members[f"{expected_top_level}/VERSION"],
            )
            target = _read_marker(
                bundle,
                members[f"{expected_top_level}/TARGET"],
            )
    except UnsafeArchive:
        raise
    except (OSError, EOFError, tarfile.TarError) as error:
        raise UnsafeArchive("archive is invalid") from error

    if version != expected_version or _VERSION_PATTERN.fullmatch(version) is None:
        raise UnsafeArchive("archive version does not match")
    if target not in SUPPORTED_TARGETS:
        raise UnsafeArchive("archive target is unsupported")
    return ValidatedArchive(selected, expected_top_level, version, target)
