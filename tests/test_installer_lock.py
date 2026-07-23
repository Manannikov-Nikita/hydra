from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from hydra_codex.installer_lock import (
    InstallerLockError,
    acquire_installer_lock,
    release_installer_lock,
)


LOCK_RECORD = b"hydra-installer-lock/v2\n"
CAPABILITY_RECORD = b"hydra-installer-capability/v1\n"


class InstallerLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.lock = self.home / ".hydra-installer-lock"

    def publish_lock(self, content: bytes = LOCK_RECORD, mode: int = 0o600) -> None:
        self.lock.write_bytes(content)
        self.lock.chmod(mode)

    def run_lock_subprocess(
        self,
        source: str,
        *arguments: Path,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        source_root = str(Path(__file__).resolve().parents[1] / "src")
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            source_root if not existing else os.pathsep.join((source_root, existing))
        )
        return subprocess.run(
            [sys.executable, "-c", source, *(str(argument) for argument in arguments)],
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
            env=environment,
        )

    def test_canonical_fifo_fails_closed_without_blocking(self) -> None:
        os.mkfifo(self.lock, mode=0o600)

        result = self.run_lock_subprocess(
            """
import sys
from pathlib import Path
from hydra_codex.installer_lock import InstallerLockError, acquire_installer_lock
try:
    acquire_installer_lock(Path(sys.argv[1]), nonce="a" * 64)
except InstallerLockError:
    print("rejected")
else:
    raise SystemExit("FIFO lock was accepted")
""",
            self.lock,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "rejected")
        self.assertTrue(self.lock.is_fifo())

    def test_replaced_capability_fifo_cleanup_fails_closed_without_blocking(
        self,
    ) -> None:
        result = self.run_lock_subprocess(
            """
import os
import sys
from pathlib import Path
from hydra_codex.installer_lock import (
    InstallerLockError,
    acquire_installer_lock,
    release_installer_lock,
)
lock = Path(sys.argv[1])
held = acquire_installer_lock(lock, nonce="a" * 64)
held.capability_path.unlink()
os.mkfifo(held.capability_path, mode=0o600)
try:
    release_installer_lock(held)
except InstallerLockError:
    print("rejected")
else:
    raise SystemExit("FIFO capability replacement was accepted")
""",
            self.lock,
        )

        capability = self.home / (".hydra-installer-capability." + "a" * 64)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "rejected")
        self.assertTrue(capability.is_fifo())
        self.assertEqual(self.lock.read_bytes(), LOCK_RECORD)

    def test_crash_before_owner_receipt_leaves_reusable_canonical_lock(self) -> None:
        # The immutable canonical file is the complete atomic publication. A
        # crash before any acquisition-specific state must not poison it.
        self.publish_lock()

        try:
            held = acquire_installer_lock(self.lock, nonce="a" * 64)
        except InstallerLockError as error:
            self.fail(f"complete canonical publication was poisoned: {error}")
        capability = self.home / (
            ".hydra-installer-capability." + "a" * 64
        )
        self.assertEqual(self.lock.read_bytes(), LOCK_RECORD)
        self.assertEqual(self.lock.stat().st_mode & 0o777, 0o600)
        self.assertEqual(capability.read_bytes(), CAPABILITY_RECORD)
        self.assertEqual(capability.stat().st_mode & 0o777, 0o600)

        release_installer_lock(held)
        self.assertEqual(self.lock.read_bytes(), LOCK_RECORD)
        self.assertFalse(capability.exists())

    def test_crash_before_canonical_link_leaves_only_an_inert_private_temp(
        self,
    ) -> None:
        abandoned = self.home / "..hydra-installer-lock.create.abandoned"
        abandoned.write_bytes(LOCK_RECORD)
        abandoned.chmod(0o600)

        held = acquire_installer_lock(self.lock, nonce="a" * 64)
        release_installer_lock(held)

        self.assertEqual(self.lock.read_bytes(), LOCK_RECORD)
        self.assertEqual(abandoned.read_bytes(), LOCK_RECORD)

    def test_exact_three_contenders_have_only_one_kernel_lock_owner(self) -> None:
        barrier = threading.Barrier(3)
        release = threading.Event()
        condition = threading.Condition()
        outcomes: list[str] = []

        def worker(index: int) -> None:
            barrier.wait()
            try:
                held = acquire_installer_lock(
                    self.lock,
                    nonce=f"{index + 1:064x}",
                )
            except InstallerLockError:
                with condition:
                    outcomes.append("busy")
                    condition.notify_all()
                return
            with condition:
                outcomes.append("success")
                condition.notify_all()
            release.wait(2)
            release_installer_lock(held)

        workers = [
            threading.Thread(target=worker, args=(index,))
            for index in range(3)
        ]
        for worker_thread in workers:
            worker_thread.start()
        deadline = time.monotonic() + 2
        with condition:
            while len(outcomes) < 3 and time.monotonic() < deadline:
                condition.wait(deadline - time.monotonic())

        self.assertEqual(sorted(outcomes), ["busy", "busy", "success"])
        release.set()
        for worker_thread in workers:
            worker_thread.join(2)
        self.assertTrue(all(not worker_thread.is_alive() for worker_thread in workers))
        self.assertEqual(self.lock.read_bytes(), LOCK_RECORD)

    def test_lock_has_no_pid_identity_and_repeated_acquisition_cannot_pid_block(
        self,
    ) -> None:
        try:
            held = acquire_installer_lock(self.lock, nonce="a" * 64)
        except InstallerLockError as error:
            self.fail(f"process identity still controls the lock: {error}")
        try:
            self.assertTrue(self.lock.is_file())
            self.assertEqual(self.lock.read_bytes(), LOCK_RECORD)
            self.assertNotIn(b"pid=", self.lock.read_bytes())
            self.assertNotIn(b"start=", self.lock.read_bytes())
        finally:
            release_installer_lock(held)

        for value in ("b", "c", "d"):
            held = acquire_installer_lock(self.lock, nonce=value * 64)
            release_installer_lock(held)
        self.assertEqual(self.lock.read_bytes(), LOCK_RECORD)

    def test_malformed_foreign_or_symlinked_canonical_files_fail_closed(
        self,
    ) -> None:
        cases = (
            ("empty", b"", 0o600),
            ("foreign", b"not-hydra\n", 0o600),
            ("public-mode", LOCK_RECORD, 0o644),
        )
        for label, content, mode in cases:
            with self.subTest(label=label):
                selected = self.home / label
                selected.write_bytes(content)
                selected.chmod(mode)
                with self.assertRaises(InstallerLockError):
                    acquire_installer_lock(selected, nonce="a" * 64)
                self.assertEqual(selected.read_bytes(), content)

        target = self.home / "target"
        target.write_bytes(LOCK_RECORD)
        target.chmod(0o600)
        linked = self.home / "linked"
        linked.symlink_to(target)
        with self.assertRaises(InstallerLockError):
            acquire_installer_lock(linked, nonce="a" * 64)
        self.assertTrue(linked.is_symlink())

        directory = self.home / "directory"
        directory.mkdir(mode=0o700)
        with self.assertRaises(InstallerLockError):
            acquire_installer_lock(directory, nonce="a" * 64)
        self.assertTrue(directory.is_dir())

    def test_stale_capabilities_are_inert_and_exact_collisions_fail_closed(
        self,
    ) -> None:
        stale = self.home / (".hydra-installer-capability." + "a" * 64)
        stale.write_bytes(CAPABILITY_RECORD)
        stale.chmod(0o600)

        with self.assertRaises(InstallerLockError):
            acquire_installer_lock(self.lock, nonce="a" * 64)
        self.assertEqual(stale.read_bytes(), CAPABILITY_RECORD)

        held = acquire_installer_lock(self.lock, nonce="b" * 64)
        release_installer_lock(held)
        self.assertEqual(stale.read_bytes(), CAPABILITY_RECORD)
        self.assertEqual(self.lock.read_bytes(), LOCK_RECORD)


if __name__ == "__main__":
    unittest.main()
