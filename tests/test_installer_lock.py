from __future__ import annotations

import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from hydra_codex.installer_lock import (
    InstallerLockError,
    acquire_installer_lock,
    release_installer_lock,
)


class InstallerLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.lock = self.home / ".hydra-installer-lock"
        self.current_start = "darwin:Wed Jul 23 12:34:56 2026"

    def process_query(self, pid: int) -> str | None:
        return self.current_start if pid == os.getpid() else None

    def write_lock(
        self,
        *,
        pid: int,
        start: str,
        nonce: str = "c" * 64,
        directory_mode: int = 0o700,
        owner_mode: int = 0o600,
    ) -> Path:
        self.lock.mkdir(mode=directory_mode)
        owner = self.lock / "owner-v1"
        owner.write_text(
            "hydra-installer-lock/v1\n"
            f"pid={pid}\n"
            f"start={start}\n"
            f"nonce={nonce}\n",
            encoding="ascii",
        )
        owner.chmod(owner_mode)
        return owner

    def test_acquire_writes_exact_private_nonce_bound_receipt_and_releases(
        self,
    ) -> None:
        nonce = "a" * 64
        held = acquire_installer_lock(
            self.lock,
            nonce=nonce,
            process_query=self.process_query,
        )

        owner = self.lock / "owner-v1"
        self.assertEqual(self.lock.stat().st_mode & 0o777, 0o700)
        self.assertEqual(owner.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            owner.read_text(encoding="ascii"),
            "hydra-installer-lock/v1\n"
            f"pid={os.getpid()}\n"
            f"start={self.current_start}\n"
            f"nonce={nonce}\n",
        )

        release_installer_lock(held)
        self.assertFalse(self.lock.exists())

    def test_dead_owner_is_reclaimed_but_live_reuse_and_unavailable_fail_closed(
        self,
    ) -> None:
        self.write_lock(pid=999_999_999, start="darwin:dead-owner")
        held = acquire_installer_lock(
            self.lock,
            nonce="a" * 64,
            process_query=self.process_query,
        )
        release_installer_lock(held)
        self.assertFalse(self.lock.exists())

        self.write_lock(pid=os.getpid(), start=self.current_start)
        with self.assertRaises(InstallerLockError):
            acquire_installer_lock(
                self.lock,
                nonce="a" * 64,
                process_query=self.process_query,
            )
        self.assertTrue(self.lock.exists())

        (self.lock / "owner-v1").write_text(
            "hydra-installer-lock/v1\n"
            f"pid={os.getpid()}\n"
            "start=darwin:wrong-start\n"
            f"nonce={'d' * 64}\n",
            encoding="ascii",
        )
        with self.assertRaises(InstallerLockError):
            acquire_installer_lock(
                self.lock,
                nonce="a" * 64,
                process_query=self.process_query,
            )
        self.assertTrue(self.lock.exists())

        def unavailable(_pid: int) -> str | None:
            raise InstallerLockError("process identity unavailable")

        with self.assertRaises(InstallerLockError):
            acquire_installer_lock(
                self.lock,
                nonce="a" * 64,
                process_query=unavailable,
            )
        self.assertTrue(self.lock.exists())

    def test_unsafe_or_ambiguous_lock_state_is_preserved(self) -> None:
        owner = self.write_lock(
            pid=999_999_999,
            start="darwin:dead-owner",
            owner_mode=0o644,
        )
        with self.assertRaises(InstallerLockError):
            acquire_installer_lock(
                self.lock,
                nonce="a" * 64,
                process_query=self.process_query,
            )
        self.assertEqual(owner.stat().st_mode & 0o777, 0o644)
        owner.unlink()
        self.lock.rmdir()

        owner = self.write_lock(
            pid=999_999_999,
            start="darwin:dead-owner",
            directory_mode=0o755,
        )
        with self.assertRaises(InstallerLockError):
            acquire_installer_lock(
                self.lock,
                nonce="a" * 64,
                process_query=self.process_query,
            )
        self.assertEqual(self.lock.stat().st_mode & 0o777, 0o755)
        self.lock.chmod(0o700)
        owner.unlink()
        self.lock.rmdir()

        target = self.home / "foreign"
        target.write_text("foreign\n", encoding="utf-8")
        self.lock.mkdir(mode=0o700)
        (self.lock / "owner-v1").symlink_to(target)
        with self.assertRaises(InstallerLockError):
            acquire_installer_lock(
                self.lock,
                nonce="a" * 64,
                process_query=self.process_query,
            )
        self.assertTrue((self.lock / "owner-v1").is_symlink())
        (self.lock / "owner-v1").unlink()
        self.lock.rmdir()

        self.write_lock(pid=999_999_999, start="darwin:dead-owner")
        extra = self.lock / "unexpected"
        extra.write_text("preserve\n", encoding="utf-8")
        with self.assertRaises(InstallerLockError):
            acquire_installer_lock(
                self.lock,
                nonce="a" * 64,
                process_query=self.process_query,
            )
        self.assertTrue(extra.exists())

    def test_release_preserves_changed_owner_and_directory_identity(self) -> None:
        held = acquire_installer_lock(
            self.lock,
            nonce="a" * 64,
            process_query=self.process_query,
        )
        owner = self.lock / "owner-v1"
        owner.write_text(
            "hydra-installer-lock/v1\n"
            f"pid={os.getpid()}\n"
            f"start={self.current_start}\n"
            f"nonce={'e' * 64}\n",
            encoding="ascii",
        )

        with self.assertRaises(InstallerLockError):
            release_installer_lock(held)
        self.assertTrue(self.lock.is_dir())
        self.assertIn("nonce=" + "e" * 64, owner.read_text(encoding="ascii"))

    def test_release_crashes_leave_only_inert_private_retirements(self) -> None:
        real_unlink = os.unlink

        held = acquire_installer_lock(
            self.lock,
            nonce="a" * 64,
            process_query=self.process_query,
        )

        def crash_before_owner_unlink(path, *args, **options):
            if path == "owner-v1" and options.get("dir_fd") is not None:
                raise OSError("injected crash after retirement")
            return real_unlink(path, *args, **options)

        with patch(
            "hydra_codex.installer_lock.os.unlink",
            side_effect=crash_before_owner_unlink,
        ):
            with self.assertRaises(InstallerLockError):
                release_installer_lock(held)
        self.assertFalse(self.lock.exists())
        retirements = list(self.home.glob(".hydra-installer-lock.retired.*"))
        self.assertEqual(len(retirements), 1)
        self.assertTrue((retirements[0] / "owner-v1").is_file())

        replacement = acquire_installer_lock(
            self.lock,
            nonce="b" * 64,
            process_query=self.process_query,
        )
        release_installer_lock(replacement)

        held = acquire_installer_lock(
            self.lock,
            nonce="c" * 64,
            process_query=self.process_query,
        )
        real_rmdir = os.rmdir

        def crash_after_owner_unlink(path, *args, **options):
            if ".hydra-installer-lock.retired." in os.fspath(path):
                raise OSError("injected crash before retirement removal")
            return real_rmdir(path, *args, **options)

        with patch(
            "hydra_codex.installer_lock.os.rmdir",
            side_effect=crash_after_owner_unlink,
        ):
            with self.assertRaises(InstallerLockError):
                release_installer_lock(held)
        self.assertFalse(self.lock.exists())
        retirements = list(self.home.glob(".hydra-installer-lock.retired.*"))
        self.assertEqual(len(retirements), 2)
        self.assertEqual(
            sum((retirement / "owner-v1").exists() for retirement in retirements),
            1,
        )

        replacement = acquire_installer_lock(
            self.lock,
            nonce="d" * 64,
            process_query=self.process_query,
        )
        release_installer_lock(replacement)

    def test_concurrent_dead_reclaim_has_exactly_one_owner(self) -> None:
        self.write_lock(pid=999_999_999, start="darwin:dead-owner")
        entered = threading.Event()
        release = threading.Event()
        outcomes: list[str] = []

        def worker() -> None:
            try:
                held = acquire_installer_lock(
                    self.lock,
                    nonce="a" * 64,
                    process_query=self.process_query,
                )
            except InstallerLockError:
                outcomes.append("busy")
                return
            outcomes.append("success")
            entered.set()
            release.wait(2)
            release_installer_lock(held)

        first = threading.Thread(target=worker)
        second = threading.Thread(target=worker)
        first.start()
        self.assertTrue(entered.wait(1))
        second.start()
        second.join(1)
        release.set()
        first.join(2)
        second.join(2)

        self.assertEqual(sorted(outcomes), ["busy", "success"])
        self.assertFalse(self.lock.exists())


if __name__ == "__main__":
    unittest.main()
