from __future__ import annotations

import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import hydra_codex.archive_validation as archive_validation
from hydra_codex.archive_validation import UnsafeArchive, validate_tar_members


TOP_LEVEL = "hydra-codex-0.1.0"
TARGET = "darwin-arm64"
REQUIRED_FILES = {
    "VERSION": (b"0.1.0\n", 0o644),
    "TARGET": (TARGET.encode() + b"\n", 0o644),
    "LICENSE": (b"MIT\n", 0o644),
    "bin/hydra-codex": (b"#!/bin/sh\n", 0o755),
    "marketplace/.agents/plugins/marketplace.json": (b"{}\n", 0o644),
    "marketplace/plugins/hydra-codex/.codex-plugin/plugin.json": (
        b"{}\n",
        0o644,
    ),
}


def regular_member(
    name: str,
    payload: bytes = b"x",
    *,
    mode: int = 0o644,
) -> tuple[tarfile.TarInfo, bytes]:
    member = tarfile.TarInfo(name)
    member.size = len(payload)
    member.mode = mode
    return member, payload


def archive_with(
    destination: Path,
    *,
    replace: dict[str, tuple[bytes, int] | None] | None = None,
    extra: list[tuple[tarfile.TarInfo, bytes | None]] | None = None,
) -> Path:
    files = dict(REQUIRED_FILES)
    for relative, value in (replace or {}).items():
        if value is None:
            files.pop(relative, None)
        else:
            files[relative] = value
    with tarfile.open(destination, "w:gz", format=tarfile.PAX_FORMAT) as bundle:
        for relative, (payload, mode) in files.items():
            member, content = regular_member(
                f"{TOP_LEVEL}/{relative}",
                payload,
                mode=mode,
            )
            bundle.addfile(member, io.BytesIO(content))
        for member, payload in extra or []:
            bundle.addfile(
                member,
                None if payload is None else io.BytesIO(payload),
            )
    return destination


class ArchiveValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def validate(self, archive: Path):
        return validate_tar_members(
            archive,
            expected_top_level=TOP_LEVEL,
        )

    def test_accepts_canonical_bounded_archive_and_reads_markers(self) -> None:
        archive = archive_with(self.root / "valid.tar.gz")

        validated = self.validate(archive)

        self.assertEqual(validated.archive, archive)
        self.assertEqual(validated.top_level, TOP_LEVEL)
        self.assertEqual(validated.version, "0.1.0")
        self.assertEqual(validated.target, TARGET)

    def test_rejects_absolute_noncanonical_escaping_and_prefix_names(self) -> None:
        names = (
            "/absolute",
            "../escape",
            f"{TOP_LEVEL}/../escape",
            f"{TOP_LEVEL}/./VERSION-copy",
            f"{TOP_LEVEL}//VERSION-copy",
            f"{TOP_LEVEL}evil/VERSION",
            f"{TOP_LEVEL}/bad\\name",
            f"{TOP_LEVEL}/control\x01name",
            f"{TOP_LEVEL}/line\nbreak",
        )
        for index, name in enumerate(names):
            with self.subTest(name=repr(name)):
                member, payload = regular_member(name)
                archive = archive_with(
                    self.root / f"bad-name-{index}.tar.gz",
                    extra=[(member, payload)],
                )
                with self.assertRaises(UnsafeArchive):
                    self.validate(archive)

    def test_rejects_every_duplicate_member_name(self) -> None:
        duplicates = (
            f"{TOP_LEVEL}/VERSION",
            f"{TOP_LEVEL}/runtime/data.bin",
        )
        for index, name in enumerate(duplicates):
            with self.subTest(name=name):
                extras: list[tuple[tarfile.TarInfo, bytes | None]] = []
                if name.endswith("data.bin"):
                    first, payload = regular_member(name, b"first")
                    extras.append((first, payload))
                duplicate, payload = regular_member(name, b"duplicate")
                extras.append((duplicate, payload))
                archive = archive_with(
                    self.root / f"duplicate-{index}.tar.gz",
                    extra=extras,
                )
                with self.assertRaises(UnsafeArchive):
                    self.validate(archive)

    def test_rejects_links_devices_fifos_and_unknown_member_types(self) -> None:
        types = (
            (tarfile.SYMTYPE, "../../escape"),
            (tarfile.LNKTYPE, f"{TOP_LEVEL}/VERSION"),
            (tarfile.CHRTYPE, ""),
            (tarfile.BLKTYPE, ""),
            (tarfile.FIFOTYPE, ""),
            (b"X", ""),
        )
        for index, (member_type, linkname) in enumerate(types):
            with self.subTest(member_type=member_type):
                member = tarfile.TarInfo(f"{TOP_LEVEL}/runtime/special-{index}")
                member.type = member_type
                member.linkname = linkname
                archive = archive_with(
                    self.root / f"special-{index}.tar.gz",
                    extra=[(member, None)],
                )
                with self.assertRaises(UnsafeArchive):
                    self.validate(archive)

    def test_rejects_stored_setuid_setgid_and_sticky_modes(self) -> None:
        for index, mode in enumerate((0o4755, 0o2755, 0o1755)):
            with self.subTest(mode=oct(mode)):
                member, payload = regular_member(
                    f"{TOP_LEVEL}/runtime/mode-{index}",
                    mode=mode,
                )
                archive = archive_with(
                    self.root / f"mode-{index}.tar.gz",
                    extra=[(member, payload)],
                )
                with self.assertRaises(UnsafeArchive):
                    self.validate(archive)

    def test_rejects_owner_unreadable_files_and_untraversable_directories(self) -> None:
        unreadable, payload = regular_member(
            f"{TOP_LEVEL}/runtime/unreadable",
            mode=0o200,
        )
        unreadable_directory = tarfile.TarInfo(
            f"{TOP_LEVEL}/runtime/unreadable-directory/"
        )
        unreadable_directory.type = tarfile.DIRTYPE
        unreadable_directory.mode = 0o300
        untraversable_directory = tarfile.TarInfo(
            f"{TOP_LEVEL}/runtime/untraversable-directory/"
        )
        untraversable_directory.type = tarfile.DIRTYPE
        untraversable_directory.mode = 0o600
        for index, extra in enumerate(
            (
                (unreadable, payload),
                (unreadable_directory, None),
                (untraversable_directory, None),
            ),
        ):
            with self.subTest(index=index):
                archive = archive_with(
                    self.root / f"inaccessible-{index}.tar.gz",
                    extra=[extra],
                )
                with self.assertRaises(UnsafeArchive):
                    self.validate(archive)

    def test_rejects_corrupt_or_non_gzip_input(self) -> None:
        for name, content in (
            ("plain.tar.gz", b"not a gzip archive"),
            ("truncated.tar.gz", b"\x1f\x8b\x08\x00"),
        ):
            with self.subTest(name=name):
                archive = self.root / name
                archive.write_bytes(content)
                with self.assertRaises(UnsafeArchive):
                    self.validate(archive)

    def test_rejects_missing_or_malformed_bounded_markers(self) -> None:
        cases = (
            {"VERSION": None},
            {"TARGET": None},
            {"VERSION": (b"0.1.0\nsecond\n", 0o644)},
            {"TARGET": (b"linux-aarch64\n", 0o644)},
            {"VERSION": (b"9" * 257, 0o644)},
            {"TARGET": (b"\xff\n", 0o644)},
        )
        for index, replacement in enumerate(cases):
            with self.subTest(index=index):
                archive = archive_with(
                    self.root / f"marker-{index}.tar.gz",
                    replace=replacement,
                )
                with self.assertRaises(UnsafeArchive):
                    self.validate(archive)

    def test_rejects_wrong_version_or_top_level_identity(self) -> None:
        archive = archive_with(
            self.root / "wrong-version.tar.gz",
            replace={"VERSION": (b"0.2.0\n", 0o644)},
        )
        with self.assertRaises(UnsafeArchive):
            self.validate(archive)

        member, payload = regular_member("other-root/runtime/file")
        archive = archive_with(
            self.root / "wrong-top.tar.gz",
            extra=[(member, payload)],
        )
        with self.assertRaises(UnsafeArchive):
            self.validate(archive)

    def test_rejects_missing_required_inventory_and_nonexecutable_launcher(self) -> None:
        for index, relative in enumerate(REQUIRED_FILES):
            with self.subTest(relative=relative):
                archive = archive_with(
                    self.root / f"missing-{index}.tar.gz",
                    replace={relative: None},
                )
                with self.assertRaises(UnsafeArchive):
                    self.validate(archive)

        archive = archive_with(
            self.root / "not-executable.tar.gz",
            replace={"bin/hydra-codex": (b"#!/bin/sh\n", 0o644)},
        )
        with self.assertRaises(UnsafeArchive):
            self.validate(archive)

    def test_rejects_member_count_member_size_and_total_size_limits(self) -> None:
        member, payload = regular_member(
            f"{TOP_LEVEL}/runtime/large",
            b"12345",
        )
        with patch.object(archive_validation, "MAX_MEMBER_BYTES", 4):
            with self.assertRaises(UnsafeArchive):
                self.validate(
                    archive_with(
                        self.root / "large.tar.gz",
                        extra=[(member, payload)],
                    ),
                )

        extras = [
            regular_member(f"{TOP_LEVEL}/runtime/{index}", b"x")
            for index in range(3)
        ]
        with patch.object(archive_validation, "MAX_MEMBERS", len(REQUIRED_FILES) + 2):
            with self.assertRaises(UnsafeArchive):
                self.validate(
                    archive_with(
                        self.root / "many.tar.gz",
                        extra=extras,
                    ),
                )
        with patch.object(
            archive_validation,
            "MAX_TOTAL_MEMBER_BYTES",
            sum(len(payload) for payload, _ in REQUIRED_FILES.values()),
        ):
            with self.assertRaises(UnsafeArchive):
                self.validate(
                    archive_with(
                        self.root / "total.tar.gz",
                        extra=[regular_member(f"{TOP_LEVEL}/runtime/x", b"x")],
                    ),
                )


if __name__ == "__main__":
    unittest.main()
